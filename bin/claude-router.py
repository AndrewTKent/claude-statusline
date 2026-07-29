#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import accounts


PASSTHROUGH_COMMANDS = {
    "agents",
    "auth",
    "auto-mode",
    "doctor",
    "gateway",
    "install",
    "mcp",
    "plugin",
    "plugins",
    "project",
    "setup-token",
    "ultrareview",
    "update",
    "upgrade",
}
PASSTHROUGH_FLAGS = {"-h", "--help", "-p", "--print", "-v", "--version"}
ROUTER_INTERVAL_S = 2.0
LEASE_HEARTBEAT_S = 5.0
FABLE_FALLBACK_MODEL = "opus"
SYNC_OUTPUT_ON = b"\x1b[?2026h"
SYNC_OUTPUT_OFF = b"\x1b[?2026l"


def claude_binary() -> str:
    explicit = os.environ.get("CLAUDE_REAL_BIN")
    if explicit:
        return explicit
    native = Path.home() / ".local/bin/claude"
    if native.is_file() and os.access(native, os.X_OK):
        return str(native)
    binary = shutil.which("claude")
    if not binary:
        raise RuntimeError("claude binary not found")
    return binary


def is_interactive(args: list[str]) -> bool:
    if any(arg in PASSTHROUGH_FLAGS for arg in args):
        return False
    return not args or args[0] not in PASSTHROUGH_COMMANDS


def option_value(args: list[str], *names: str) -> str | None:
    for index, arg in enumerate(args):
        for name in names:
            if arg == name and index + 1 < len(args):
                return args[index + 1]
            prefix = f"{name}="
            if arg.startswith(prefix):
                return arg[len(prefix) :]
    return None


def explicit_session_id(args: list[str]) -> str | None:
    session_id = option_value(args, "--session-id")
    if session_id:
        return session_id
    if "--fork-session" in args:
        return None
    resumed = option_value(args, "--resume", "-r")
    try:
        return str(uuid.UUID(resumed)) if resumed else None
    except ValueError:
        return None


def initial_session_args(args: list[str]) -> tuple[list[str], str | None]:
    session_id = explicit_session_id(args)
    has_existing_selector = any(
        arg in {"-c", "--continue", "-r", "--resume", "--from-pr"}
        or arg.startswith("--resume=")
        or arg.startswith("--from-pr=")
        for arg in args
    )
    if session_id or has_existing_selector:
        return list(args), session_id
    session_id = str(uuid.uuid4())
    return [*args, "--session-id", session_id], session_id


def replace_model_args(args: list[str], model: str) -> list[str]:
    stripped: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--model":
            index += 2
            continue
        if arg.startswith("--model="):
            index += 1
            continue
        stripped.append(arg)
        index += 1
    return [*stripped, "--model", model]


def resume_session_args(
    args: list[str],
    session_id: str,
    model_override: str | None = None,
) -> list[str]:
    stripped: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-c", "--continue", "--fork-session"}:
            index += 1
            continue
        if arg in {"-r", "--resume", "--session-id", "--from-pr"}:
            index += 2
            continue
        if any(
            arg.startswith(prefix)
            for prefix in ("--resume=", "--session-id=", "--from-pr=")
        ):
            index += 1
            continue
        stripped.append(arg)
        index += 1
    if model_override:
        stripped = replace_model_args(stripped, model_override)
    return [*stripped, "--resume", session_id]


def model_name(args: list[str], rendered_model: str | None = None) -> str | None:
    model = rendered_model or option_value(args, "--model")
    if model is None:
        try:
            settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
            model = settings.get("model")
        except (OSError, json.JSONDecodeError):
            model = None
    if model is None:
        return None
    normalized = str(model).lower()
    for alias in ("fable", "opus", "sonnet", "haiku"):
        if alias in normalized:
            return alias
    return str(model)


def model_family(args: list[str], rendered_model: str | None = None) -> str:
    return "fable" if model_name(args, rendered_model) == "fable" else "general"


def routed_environment(selected: dict, state_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "ACCOUNTS_ROUTED_LABEL",
        "ACCOUNTS_ROUTED_EMAIL",
        "ACCOUNTS_ROUTED_ORG_UUID",
    ):
        env.pop(key, None)
    env.update(
        {
            "CLAUDE_CONFIG_DIR": selected["profile"],
            "ACCOUNTS_ROUTED_LABEL": selected["label"],
            "ACCOUNTS_ROUTED_EMAIL": selected["email"],
            "ACCOUNTS_ROUTED_ORG_UUID": selected["org_uuid"],
            "ACCOUNTS_ROUTER_STATE": str(state_path),
        }
    )
    return env


def read_router_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def source_mtime() -> int | None:
    try:
        return Path(__file__).stat().st_mtime_ns
    except OSError:
        return None


def set_synchronized_output(enabled: bool) -> bool:
    if not sys.stdout.isatty():
        return False
    try:
        os.write(
            sys.stdout.fileno(),
            SYNC_OUTPUT_ON if enabled else SYNC_OUTPUT_OFF,
        )
    except OSError:
        return False
    return True


def stop_for_handoff(child: subprocess.Popen) -> None:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def run_passthrough(binary: str, args: list[str]) -> int:
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return subprocess.call([binary, *args])
    current_family = model_family(args)
    launch_args = args
    selected = accounts.select_profile(require_fable=current_family == "fable")
    if selected is None and current_family == "fable":
        selected = accounts.select_profile(require_fable=False)
        if selected is not None:
            launch_args = replace_model_args(
                args,
                os.environ.get(
                    "ACCOUNTS_FABLE_FALLBACK_MODEL",
                    FABLE_FALLBACK_MODEL,
                ),
            )
    if selected is None:
        print("accounts: no account has enough quota", file=sys.stderr)
        return 1
    state_path = Path(f"/tmp/claude/account-router-{os.getpid()}.json")
    return subprocess.call(
        [binary, *launch_args],
        env=routed_environment(selected, state_path),
    )


def run_supervised(binary: str, args: list[str]) -> int:
    launch_args, session_id = initial_session_args(args)
    router_pid = os.getpid()
    state_path = Path(f"/tmp/claude/account-router-{router_pid}.json")
    interval = float(os.environ.get("ACCOUNTS_ROUTER_INTERVAL", ROUTER_INTERVAL_S))
    loaded_mtime = source_mtime()
    current_model = model_name(args)
    current_family = "fable" if current_model == "fable" else "general"
    if accounts.load_mode().get("mode") == "fable" and current_family != "fable":
        current_model = "fable"
        current_family = "fable"
        launch_args = replace_model_args(launch_args, current_model)
    model_override = None
    selected = accounts.select_profile(
        require_fable=current_family == "fable",
        lease_pid=router_pid,
    )
    if selected is None and current_family == "fable":
        model_override = os.environ.get(
            "ACCOUNTS_FABLE_FALLBACK_MODEL",
            FABLE_FALLBACK_MODEL,
        )
        selected = accounts.select_profile(
            require_fable=False,
            lease_pid=router_pid,
        )
        if selected is not None:
            launch_args = replace_model_args(launch_args, model_override)
            current_model = model_override
            current_family = "general"
    if selected is None:
        print("accounts: no account has enough quota for this model", file=sys.stderr)
        return 1

    output_frozen = os.environ.pop("ACCOUNTS_ROUTER_OUTPUT_FROZEN", "") == "1"
    freeze_started = time.monotonic() if output_frozen else 0.0
    try:
        while True:
            state_path.unlink(missing_ok=True)
            env = routed_environment(selected, state_path)
            accounts.upsert_session_lease(
                router_pid,
                session_id,
                selected["label"],
                current_family,
            )
            child = subprocess.Popen([binary, *launch_args], env=env)
            last_heartbeat = time.monotonic()
            handoff = False
            while child.poll() is None:
                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    continue
                state = read_router_state(state_path)
                if output_frozen and (
                    state or time.monotonic() - freeze_started >= 1.5
                ):
                    set_synchronized_output(False)
                    output_frozen = False
                session_id = state.get("session_id") or session_id
                rendered_model = state.get("model")
                if rendered_model:
                    current_model = model_name(args, rendered_model)
                    current_family = (
                        "fable" if current_model == "fable" else "general"
                    )
                    if (
                        model_override
                        and current_model
                        and current_model != model_override
                    ):
                        model_override = None
                now = time.monotonic()
                if now - last_heartbeat >= LEASE_HEARTBEAT_S:
                    accounts.upsert_session_lease(
                        router_pid,
                        session_id,
                        selected["label"],
                        current_family,
                    )
                    last_heartbeat = now
                if not session_id:
                    continue
                current_mtime = source_mtime()
                if (
                    loaded_mtime is not None
                    and current_mtime is not None
                    and current_mtime != loaded_mtime
                ):
                    if not output_frozen:
                        output_frozen = set_synchronized_output(True)
                        freeze_started = time.monotonic()
                    stop_for_handoff(child)
                    os.environ["ACCOUNTS_ROUTER_OUTPUT_FROZEN"] = "1"
                    os.execv(
                        sys.executable,
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            *resume_session_args(args, session_id, current_model),
                        ],
                    )
                if (
                    accounts.load_mode().get("mode") == "fable"
                    and current_family != "fable"
                ):
                    next_profile = accounts.select_profile(
                        require_fable=True,
                        lease_pid=router_pid,
                    )
                    if next_profile is None:
                        continue
                    next_model = "fable"
                    next_override = None
                else:
                    target = accounts.handoff_target(
                        selected["label"],
                        require_fable=current_family == "fable",
                    )
                    next_model = model_override or current_model
                    next_override = model_override
                    if target:
                        next_profile = accounts.select_profile(
                            avoid_labels={selected["label"]},
                            require_fable=current_family == "fable",
                            lease_pid=router_pid,
                        )
                        if next_profile is None or next_profile["label"] != target:
                            continue
                    elif (
                        current_family == "fable"
                        and not accounts.profile_has_headroom(
                            selected["label"],
                            require_fable=True,
                        )
                    ):
                        next_model = os.environ.get(
                            "ACCOUNTS_FABLE_FALLBACK_MODEL",
                            FABLE_FALLBACK_MODEL,
                        )
                        next_override = next_model
                        next_profile = accounts.select_profile(
                            require_fable=False,
                            lease_pid=router_pid,
                        )
                        if next_profile is None:
                            continue
                    else:
                        continue
                output_frozen = set_synchronized_output(True)
                freeze_started = time.monotonic()
                stop_for_handoff(child)
                launch_args = resume_session_args(args, session_id, next_model)
                selected = next_profile
                model_override = next_override
                current_model = next_model
                current_family = (
                    model_family(["--model", next_model])
                    if next_model
                    else current_family
                )
                handoff = True
                break
            if not handoff:
                return child.wait()
    finally:
        if output_frozen:
            set_synchronized_output(False)
        accounts.remove_session_lease(router_pid)
        state_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    binary = claude_binary()
    if not is_interactive(args):
        return run_passthrough(binary, args)
    return run_supervised(binary, args)


if __name__ == "__main__":
    raise SystemExit(main())
