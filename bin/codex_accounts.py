#!/usr/bin/env python3
"""Codex subscription-account registry, quota poller, and routing controls."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("CODEX_ACCOUNTS_HOME", Path.home() / ".codex-accounts"))
REGISTRY_PATH = ROOT / "accounts.json"
USAGE_PATH = ROOT / "usage.json"
MODE_PATH = ROOT / "mode.json"
LOCK_PATH = ROOT / "accounts.lock"
PROFILE_ROOT = ROOT / "profiles"
THREAD_DIR = ROOT / "thread-accounts"
USAGE_MAX_AGE_S = 300
LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
PRIVATE_HOME_NAMES = {
    "auth.json",
    "cache",
    "ipc",
    "log",
    "models_cache.json",
    ".tmp",
}


class AccountsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    email: str
    account_id: str = field(repr=False)
    default_label: str


def shared_home() -> Path:
    return Path(os.environ.get("CODEX_SHARED_HOME", Path.home() / ".codex")).expanduser()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


@contextmanager
def locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except (ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def clean_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    if not LABEL_PATTERN.fullmatch(label):
        raise AccountsError("account labels must use 1-32 letters, numbers, dots, dashes, or underscores")
    return label


def identity_from_auth(auth: dict[str, Any]) -> Identity:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    claims = decode_jwt_payload(str(tokens.get("id_token", "")))
    email = next(
        (str(claims[key]) for key in ("email", "preferred_username") if claims.get(key)),
        "",
    )
    account_id = str(tokens.get("account_id") or claims.get("chatgpt_account_id") or "")
    basis = email.partition("@")[0] if email else account_id[:12]
    if not basis:
        raise AccountsError("Codex auth.json does not contain a recognizable ChatGPT identity")
    return Identity(email=email, account_id=account_id, default_label=clean_label(basis))


def account_identity(home: Path) -> Identity:
    auth = read_json(home / "auth.json", {})
    if not isinstance(auth, dict):
        raise AccountsError(f"{home}/auth.json is unreadable")
    return identity_from_auth(auth)


def ensure_profile(profile: Path, source: Path | None = None) -> None:
    source = source or shared_home()
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    if profile.resolve() == source.resolve():
        return
    if not source.is_dir():
        raise AccountsError(f"shared Codex home does not exist: {source}")
    for item in source.iterdir():
        if item.name in PRIVATE_HOME_NAMES:
            continue
        target = profile / item.name
        if target.is_symlink() and target.resolve(strict=False) == item.resolve(strict=False):
            continue
        if target.exists() or target.is_symlink():
            raise AccountsError(f"profile entry blocks shared state: {target}")
        target.symlink_to(item)


def load_registry() -> dict[str, dict[str, str]]:
    value = read_json(REGISTRY_PATH, {"version": 1, "accounts": {}})
    accounts = value.get("accounts") if isinstance(value, dict) else {}
    return accounts if isinstance(accounts, dict) else {}


def save_registry(accounts: dict[str, dict[str, str]]) -> None:
    write_json(REGISTRY_PATH, {"version": 1, "accounts": accounts})


def load_usage() -> dict[str, dict[str, Any]]:
    value = read_json(USAGE_PATH, {})
    return value if isinstance(value, dict) else {}


def load_mode() -> dict[str, str]:
    value = read_json(MODE_PATH, {"mode": "auto"})
    return value if isinstance(value, dict) else {"mode": "auto"}


def save_mode(mode: str, label: str = "") -> None:
    value = {"mode": mode}
    if label:
        value["label"] = label
    write_json(MODE_PATH, value)


def _usable_binary(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        if not os.access(resolved, os.X_OK):
            return False
        head = resolved.read_bytes()[:512]
    except OSError:
        return False
    return not (head.startswith(b"#!") and b"codex-router" in head)


def codex_binary() -> str:
    explicit = os.environ.get("CODEX_REAL_BIN")
    if explicit and _usable_binary(Path(explicit)):
        return explicit
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory or ".") / "codex"
        if _usable_binary(candidate):
            return str(candidate)
    binary = shutil.which("codex")
    if binary and _usable_binary(Path(binary)):
        return binary
    raise AccountsError("native Codex binary not found; set CODEX_REAL_BIN")


def _read_response(process: subprocess.Popen[str], request_id: int, timeout: float) -> dict[str, Any]:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            events = selector.select(max(0.0, deadline - time.monotonic()))
            if not events:
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                if message.get("error"):
                    raise AccountsError(str(message["error"]))
                result = message.get("result")
                return result if isinstance(result, dict) else {}
    finally:
        selector.close()
    raise AccountsError("Codex app-server did not return rate limits")


def read_rate_limits(home: Path, binary: str, timeout: float = 10.0) -> dict[str, Any]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    process = subprocess.Popen(
        [binary, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
    )
    try:
        assert process.stdin is not None
        initialize = {
            "id": 0,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "codex-account-router",
                    "title": "Codex Account Router",
                    "version": "1.0.0",
                }
            },
        }
        process.stdin.write(json.dumps(initialize) + "\n")
        process.stdin.flush()
        _read_response(process, 0, timeout)
        process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        process.stdin.write(json.dumps({"id": 1, "method": "account/rateLimits/read"}) + "\n")
        process.stdin.flush()
        return _read_response(process, 1, timeout)
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def normalize_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {snake_case(str(key)): normalize_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_keys(item) for item in value]
    return value


def poll_account(label: str, account: dict[str, str], binary: str) -> dict[str, Any]:
    home = Path(account["home"])
    if not (home / "auth.json").is_file():
        return {"fetched_at": time.time(), "error": "login required"}
    try:
        result = read_rate_limits(home, binary)
        limits = result.get("rateLimits") or result.get("rate_limits") or result
        if not isinstance(limits, dict):
            raise AccountsError("Codex app-server returned malformed rate limits")
        return {"fetched_at": time.time(), "rate_limits": normalize_keys(limits)}
    except (AccountsError, OSError, subprocess.SubprocessError) as exc:
        return {"fetched_at": time.time(), "error": str(exc)}


def poll_all(accounts: dict[str, dict[str, str]], binary: str | None = None) -> dict[str, dict[str, Any]]:
    if not accounts:
        return {}
    binary = binary or codex_binary()
    rows: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(accounts))) as executor:
        futures = {
            executor.submit(poll_account, label, account, binary): label
            for label, account in accounts.items()
        }
        for future in as_completed(futures):
            rows[futures[future]] = future.result()
    with locked():
        current = load_usage()
        current.update(rows)
        write_json(USAGE_PATH, current)
    return rows


def binding_usage(row: dict[str, Any]) -> float:
    limits = row.get("rate_limits") if isinstance(row.get("rate_limits"), dict) else {}
    values = []
    for key in ("primary", "secondary"):
        limit = limits.get(key)
        if isinstance(limit, dict) and limit.get("used_percent") is not None:
            values.append(float(limit["used_percent"]))
    if limits.get("rate_limit_reached_type") or limits.get("spend_control_reached"):
        values.append(100.0)
    return max(values, default=101.0)


def pick_account(
    accounts: dict[str, dict[str, str]],
    usage: dict[str, dict[str, Any]],
    mode: dict[str, str],
    *,
    now: float | None = None,
    avoid: set[str] | None = None,
) -> str | None:
    avoid = avoid or set()
    if mode.get("mode") == "set":
        label = mode.get("label", "")
        return label if label in accounts and label not in avoid else None
    now = now or time.time()
    fresh = [
        label
        for label in accounts
        if label not in avoid
        and label in usage
        and not usage[label].get("error")
        and now - float(usage[label].get("fetched_at", 0)) <= USAGE_MAX_AGE_S
        and binding_usage(usage[label]) < 100
    ]
    return min(fresh, key=lambda label: (binding_usage(usage[label]), label), default=None)


def register_home(home: Path, requested_label: str = "") -> str:
    identity = account_identity(home)
    label = clean_label(requested_label) if requested_label else identity.default_label
    with locked():
        accounts = load_registry()
        existing = accounts.get(label)
        if existing and existing.get("account_id") != identity.account_id:
            raise AccountsError(f"account label already exists: {label}")
        accounts[label] = {
            "home": str(home.resolve()),
            "email": identity.email,
            "account_id": identity.account_id,
        }
        save_registry(accounts)
    return label


def cmd_register(args: argparse.Namespace) -> None:
    home = Path(args.home or os.environ.get("CODEX_HOME") or shared_home()).expanduser()
    label = register_home(home, args.label or "")
    print(f"REGISTERED → {label}")


def cmd_login(args: argparse.Namespace) -> None:
    provisional = (
        clean_label(args.label)
        if args.label
        else f"account-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
    )
    profile = PROFILE_ROOT / provisional
    ensure_profile(profile)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(profile)
    command = [codex_binary(), "-c", 'cli_auth_credentials_store="file"', "login"]
    if args.device_auth:
        command.append("--device-auth")
    completed = subprocess.run(command, env=env, check=False)
    if completed.returncode:
        raise AccountsError("Codex login did not complete")
    identity = account_identity(profile)
    label = clean_label(args.label) if args.label else identity.default_label
    final_profile = PROFILE_ROOT / label
    if final_profile != profile:
        if final_profile.exists():
            raise AccountsError(f"account profile already exists: {label}")
        profile.rename(final_profile)
    register_home(final_profile, label)
    poll_all(load_registry())
    print(f"LOGGED IN → {label}")


def cmd_poll(_args: argparse.Namespace) -> None:
    accounts = load_registry()
    if not accounts:
        raise AccountsError("no Codex accounts registered")
    rows = poll_all(accounts)
    for label in sorted(rows):
        row = rows[label]
        result = f"error: {row['error']}" if row.get("error") else f"{binding_usage(row):.0f}% used"
        print(f"{label}: {result}")


def cmd_auto(_args: argparse.Namespace) -> None:
    save_mode("auto")
    accounts = load_registry()
    if accounts:
        poll_all(accounts)
    selected = pick_account(accounts, load_usage(), load_mode())
    print(f"AUTO → {selected or '(none available)'}")


def cmd_set(args: argparse.Namespace) -> None:
    accounts = load_registry()
    if args.label not in accounts:
        raise AccountsError(f"unknown Codex account: {args.label}")
    save_mode("set", args.label)
    print(f"SET → {args.label}")


def format_reset(epoch: Any) -> str:
    try:
        value = datetime.fromtimestamp(float(epoch)).astimezone()
    except (TypeError, ValueError, OSError):
        return "—"
    return value.strftime("%a %-I:%M%p %Z")


def cmd_status(_args: argparse.Namespace) -> None:
    accounts = load_registry()
    usage = load_usage()
    mode = load_mode()
    mode_text = f"SET → {mode.get('label', '?')}" if mode.get("mode") == "set" else "AUTO"
    selected = pick_account(accounts, usage, mode)
    print(f"mode: {mode_text}   next: {selected or '(none available)'}")
    for label in sorted(accounts):
        row = usage.get(label, {})
        if row.get("error"):
            detail = f"⚠ {row['error']}"
        elif not row:
            detail = "not polled"
        else:
            limits = row.get("rate_limits", {})
            parts = []
            for key in ("primary", "secondary"):
                limit = limits.get(key)
                if isinstance(limit, dict) and limit.get("used_percent") is not None:
                    parts.append(
                        f"{key} {float(limit['used_percent']):.0f}% reset {format_reset(limit.get('resets_at'))}"
                    )
            detail = " · ".join(parts) or "no quota windows"
        marker = "*" if label == selected else " "
        print(f" {marker} {label:<12} {detail}")


def cmd_pick(args: argparse.Namespace) -> None:
    accounts = load_registry()
    rows = poll_all(accounts) if args.poll else load_usage()
    selected = pick_account(accounts, rows, load_mode(), avoid=set(args.avoid))
    if not selected:
        raise AccountsError("no Codex account has fresh quota headroom")
    print(selected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-accounts")
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register", help="register an already authenticated Codex home")
    register.add_argument("label", nargs="?")
    register.add_argument("--home")
    register.set_defaults(fn=cmd_register)
    login = sub.add_parser("login", help="authenticate and register another ChatGPT account")
    login.add_argument("label", nargs="?")
    login.add_argument("--device-auth", action="store_true")
    login.set_defaults(fn=cmd_login)
    sub.add_parser("poll", help="read every account's quota without making an inference").set_defaults(fn=cmd_poll)
    sub.add_parser("auto", help="route new and supervised sessions by quota headroom").set_defaults(fn=cmd_auto)
    set_parser = sub.add_parser("set", help="pin supervised sessions to one account")
    set_parser.add_argument("label")
    set_parser.set_defaults(fn=cmd_set)
    sub.add_parser("status", help="show routing mode and account quota windows").set_defaults(fn=cmd_status)
    pick = sub.add_parser("pick", help="print the account selected by the current policy")
    pick.add_argument("--poll", action="store_true")
    pick.add_argument("--avoid", action="append", default=[])
    pick.set_defaults(fn=cmd_pick)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except AccountsError as exc:
        print(f"codex-accounts: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
