#!/usr/bin/env python3
"""Quota-aware Codex launcher with live account handoff."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import codex_accounts

PASSTHROUGH_COMMANDS = {
    "agents",
    "app",
    "app-server",
    "apply",
    "archive",
    "cloud",
    "completion",
    "debug",
    "delete",
    "doctor",
    "exec-server",
    "features",
    "help",
    "login",
    "logout",
    "mcp",
    "mcp-server",
    "migrate-rollouts",
    "plugin",
    "queue",
    "remote-control",
    "sandbox",
    "unarchive",
    "update",
}
ROUTED_COMMANDS = {"exec", "review"}
INTERACTIVE_COMMANDS = {"fork", "resume"}
GLOBAL_VALUE_OPTIONS = {
    "-a",
    "--ask-for-approval",
    "-c",
    "--config",
    "-C",
    "--cd",
    "--enable",
    "--disable",
    "-i",
    "--image",
    "-m",
    "--model",
    "--oss-provider",
    "-p",
    "--profile",
    "-s",
    "--sandbox",
}
POLL_INTERVAL_S = 60.0
SUPERVISOR_INTERVAL_S = 2.0
HANDOFF_THRESHOLD = 90.0
HANDOFF_MARGIN = 15.0
PASSTHROUGH_FLAGS = {"-h", "--help", "-V", "--version"}
AUTH_STORE_OVERRIDE = 'cli_auth_credentials_store="file"'


def command_name(args: list[str]) -> str:
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg == "--":
            return ""
        if arg in GLOBAL_VALUE_OPTIONS:
            skip_value = True
            continue
        if arg.startswith("-"):
            continue
        return arg if arg in PASSTHROUGH_COMMANDS | ROUTED_COMMANDS | INTERACTIVE_COMMANDS else ""
    return ""


def should_route(args: list[str]) -> bool:
    return not any(arg in PASSTHROUGH_FLAGS for arg in args) and command_name(args) not in PASSTHROUGH_COMMANDS


def is_supervisable(args: list[str]) -> bool:
    return command_name(args) not in ROUTED_COMMANDS


def codex_binary() -> str:
    return codex_accounts.codex_binary()


def global_args(args: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if arg in PASSTHROUGH_COMMANDS | ROUTED_COMMANDS | INTERACTIVE_COMMANDS:
            break
        if not arg.startswith("-"):
            break
        result.append(arg)
        if arg in GLOBAL_VALUE_OPTIONS and index + 1 < len(args):
            result.append(args[index + 1])
            index += 2
            continue
        index += 1
    return result


def resume_args(args: list[str], session_id: str) -> list[str]:
    return [*global_args(args), "resume", session_id]


def with_file_auth(args: list[str]) -> list[str]:
    return ["-c", AUTH_STORE_OVERRIDE, *args]


def explicit_session_id(args: list[str]) -> str:
    if command_name(args) not in INTERACTIVE_COMMANDS:
        return ""
    for index, arg in enumerate(args):
        if arg in INTERACTIVE_COMMANDS and index + 1 < len(args):
            candidate = args[index + 1]
            return "" if candidate.startswith("-") else candidate
    return ""


def child_environment(
    label: str,
    account: dict[str, str],
    state_path: Path | None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("CODEX_ACCOUNT_EMAIL", None)
    env["CODEX_HOME"] = account["home"]
    env["CODEX_ROUTED_LABEL"] = label
    env["CODEX_ACCOUNTS_THREAD_DIR"] = str(codex_accounts.THREAD_DIR)
    env["CODEX_ROUTER_ACTIVE"] = "1"
    if state_path is not None:
        env["CODEX_ACCOUNT_ROUTER_STATE"] = str(state_path)
    else:
        env.pop("CODEX_ACCOUNT_ROUTER_STATE", None)
    return env


def read_state(path: Path) -> dict[str, object]:
    value = codex_accounts.read_json(path, {})
    return value if isinstance(value, dict) else {}


def stop_child(child: subprocess.Popen[bytes]) -> None:
    child.send_signal(signal.SIGTERM)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def select_initial() -> tuple[str, dict[str, str]]:
    accounts = codex_accounts.load_registry()
    if not accounts:
        raise codex_accounts.AccountsError(
            "no accounts registered; run `codex-accounts register` for the current login"
        )
    codex_accounts.poll_all(accounts)
    label = codex_accounts.pick_account(accounts, codex_accounts.load_usage(), codex_accounts.load_mode())
    if not label:
        raise codex_accounts.AccountsError("no Codex account has fresh quota headroom")
    home = Path(accounts[label]["home"])
    codex_accounts.ensure_profile(home)
    return label, accounts[label]


def handoff_target(current: str) -> str | None:
    accounts = codex_accounts.load_registry()
    mode = codex_accounts.load_mode()
    usage = codex_accounts.load_usage()
    selected = codex_accounts.pick_account(accounts, usage, mode)
    if mode.get("mode") == "set":
        return selected if selected != current else None
    current_row = usage.get(current, {})
    current_usage = codex_accounts.binding_usage(current_row)
    if current_row.get("error"):
        current_usage = 101.0
    if current_usage < float(os.environ.get("CODEX_ACCOUNTS_HANDOFF_THRESHOLD", HANDOFF_THRESHOLD)):
        return None
    alternative = codex_accounts.pick_account(accounts, usage, mode, avoid={current})
    if not alternative:
        return None
    margin = float(os.environ.get("CODEX_ACCOUNTS_HANDOFF_MARGIN", HANDOFF_MARGIN))
    return alternative if codex_accounts.binding_usage(usage[alternative]) <= current_usage - margin else None


def run_supervised(binary: str, args: list[str], label: str, account: dict[str, str]) -> int:
    runtime = Path(tempfile.mkdtemp(prefix="codex-account-router."))
    state_path = runtime / "session.json"
    session_id = explicit_session_id(args)
    launch_args = list(args)
    last_poll = time.monotonic()
    try:
        while True:
            print(f"Codex account → {label}", file=sys.stderr)
            child = subprocess.Popen(
                [binary, *launch_args],
                env=child_environment(label, account, state_path),
            )
            try:
                while True:
                    try:
                        return child.wait(timeout=SUPERVISOR_INTERVAL_S)
                    except subprocess.TimeoutExpired:
                        pass
                    state = read_state(state_path)
                    session_id = str(state.get("session_id") or session_id)
                    if time.monotonic() - last_poll >= POLL_INTERVAL_S:
                        codex_accounts.poll_all(codex_accounts.load_registry())
                        last_poll = time.monotonic()
                    target = handoff_target(label)
                    if not target or not session_id:
                        continue
                    accounts = codex_accounts.load_registry()
                    stop_child(child)
                    label = target
                    account = accounts[label]
                    codex_accounts.ensure_profile(Path(account["home"]))
                    launch_args = resume_args(args, session_id)
                    break
            except KeyboardInterrupt:
                stop_child(child)
                return 130
    finally:
        try:
            state_path.unlink(missing_ok=True)
            runtime.rmdir()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    binary = codex_binary()
    if os.environ.get("CODEX_ROUTER_BYPASS") == "1" or not should_route(args):
        os.execv(binary, [binary, *args])
    try:
        label, account = select_initial()
        env = child_environment(label, account, None)
        routed_args = with_file_auth(args)
        if not is_supervisable(args):
            completed = subprocess.run([binary, *routed_args], env=env, check=False)
            raise SystemExit(completed.returncode)
        raise SystemExit(run_supervised(binary, routed_args, label, account))
    except codex_accounts.AccountsError as exc:
        print(f"codex-router: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
