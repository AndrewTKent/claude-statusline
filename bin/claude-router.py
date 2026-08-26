#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
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
DEFAULT_EFFORT = "high"
ULTRACODE_EFFORT = "ultracode"
DEFAULT_SESSION_NAME = "\u200b"
LEGACY_DEFAULT_SESSION_NAMES = {" ", "\u2063"}
ULTRACODE_ENV = "CLAUDE_ROUTER_ULTRACODE"
SESSION_LIMIT_TEXT = "You've hit your session limit"
FABLE_LIMIT_TEXT = "You've reached your Fable 5 limit."
SYNC_OUTPUT_ON = b"\x1b[?2026h"
SYNC_OUTPUT_OFF = b"\x1b[?2026l"


def _usable_claude(path: Path) -> bool:
    # ~/.local/bin/claude is now the router wrapper; treating it (or any
    # router shim) as the real binary would exec-loop back into the router.
    try:
        resolved = path.resolve(strict=True)
        if not os.access(resolved, os.X_OK):
            return False
        with resolved.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return False
    return not (head.startswith(b"#!") and b"claude-router" in head)


def claude_binary() -> str:
    explicit = os.environ.get("CLAUDE_REAL_BIN")
    if explicit and _usable_claude(Path(explicit)):
        return explicit
    native = Path.home() / ".local/bin/claude"
    if _usable_claude(native):
        return str(native)
    versions = Path.home() / ".local/share/claude/versions"
    try:
        newest = max(
            (p for p in versions.iterdir() if _usable_claude(p)),
            key=lambda p: p.stat().st_mtime,
            default=None,
        )
    except OSError:
        newest = None
    if newest is not None:
        return str(newest)
    binary = shutil.which("claude")
    if binary and _usable_claude(Path(binary)):
        return binary
    raise RuntimeError("claude binary not found")


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


def with_default_effort(args: list[str]) -> list[str]:
    if any(arg == "--effort" or arg.startswith("--effort=") for arg in args):
        return list(args)
    return [*args, "--effort", DEFAULT_EFFORT]


def with_default_session_name(args: list[str]) -> list[str]:
    updated = list(args)
    for index, arg in enumerate(updated):
        if arg in {"-n", "--name"}:
            if (
                index + 1 < len(updated)
                and updated[index + 1] in LEGACY_DEFAULT_SESSION_NAMES
            ):
                updated[index + 1] = DEFAULT_SESSION_NAME
            return updated
        if arg.startswith("--name="):
            if arg.partition("=")[2] in LEGACY_DEFAULT_SESSION_NAMES:
                updated[index] = f"--name={DEFAULT_SESSION_NAME}"
            return updated
    return [*updated, "--name", DEFAULT_SESSION_NAME]


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


def replace_effort_args(args: list[str], effort: str) -> list[str]:
    stripped: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--effort":
            index += 2
            continue
        if arg.startswith("--effort="):
            index += 1
            continue
        stripped.append(arg)
        index += 1
    return [*stripped, "--effort", effort]


def resume_session_args(
    args: list[str],
    session_id: str,
    model_override: str | None = None,
    effort_override: str | None = None,
) -> list[str]:
    stripped: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-c", "--continue", "--fork-session"}:
            index += 1
            continue
        if arg in {"-r", "--resume", "--from-pr"}:
            index += 1
            if index < len(args) and not args[index].startswith("-"):
                index += 1
            continue
        if arg == "--session-id":
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
    if effort_override:
        stripped = replace_effort_args(stripped, effort_override)
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


def routed_environment(
    selected: dict,
    state_path: Path,
    args: list[str],
    policy_scope: str = "global",
) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "ACCOUNTS_ROUTED_LABEL",
        "ACCOUNTS_ROUTED_EMAIL",
        "ACCOUNTS_ROUTED_ORG_UUID",
        "ACCOUNTS_POLICY_SCOPE",
        ULTRACODE_ENV,
    ):
        env.pop(key, None)
    effort = option_value(args, "--effort")
    if effort is not None:
        env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
    if effort == ULTRACODE_EFFORT:
        env[ULTRACODE_ENV] = "1"
    env.update(
        {
            "CLAUDE_CONFIG_DIR": selected["profile"],
            "ACCOUNTS_ROUTED_LABEL": selected["label"],
            "ACCOUNTS_ROUTED_EMAIL": selected["email"],
            "ACCOUNTS_ROUTED_ORG_UUID": selected["org_uuid"],
            "ACCOUNTS_POLICY_SCOPE": policy_scope,
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


def session_transcript_path(session_id: str | None) -> Path | None:
    if session_id is None:
        return None
    try:
        return next(
            (Path.home() / ".claude" / "projects").glob(
                f"*/{session_id}.jsonl"
            ),
            None,
        )
    except OSError:
        return None


def transcript_size(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def handoff_session_args(
    args: list[str],
    session_id: str,
    model_override: str | None = None,
    effort_override: str | None = None,
) -> list[str]:
    launch_args = resume_session_args(
        args,
        session_id,
        model_override,
        effort_override,
    )
    if transcript_size(session_transcript_path(session_id)) > 0:
        return launch_args
    return [*launch_args[:-2], "--session-id", session_id]


def record_limit_kind(record: dict) -> str | None:
    if (
        record.get("type") != "assistant"
        or record.get("isApiErrorMessage") is not True
        or record.get("apiErrorStatus") != 429
        or record.get("error") != "rate_limit"
    ):
        return None
    content = (record.get("message") or {}).get("content") or []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = str(block.get("text") or "")
        if FABLE_LIMIT_TEXT in text:
            return "fable"
        if SESSION_LIMIT_TEXT in text:
            return "session"
    return None


def record_timestamp(record: dict) -> float | None:
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_new_session_limit(
    path: Path,
    offset: int,
    *,
    not_before: float | None = None,
) -> tuple[str | None, int]:
    try:
        with path.open("rb") as transcript:
            transcript.seek(min(offset, path.stat().st_size))
            next_offset = transcript.tell()
            while True:
                record_offset = transcript.tell()
                line = transcript.readline()
                if not line:
                    return None, next_offset
                if not line.endswith(b"\n"):
                    return None, record_offset
                next_offset = transcript.tell()
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                limit_kind = record_limit_kind(record)
                if limit_kind is None:
                    continue
                timestamp = record_timestamp(record)
                if not_before is None or (
                    timestamp is not None and timestamp >= not_before
                ):
                    return limit_kind, next_offset
    except OSError:
        return None, offset


def mark_detected_limit(selected: dict, limit_kind: str) -> None:
    marker = (
        accounts.mark_fable_limit
        if limit_kind == "fable"
        else accounts.mark_session_limit
    )
    marker(selected["email"], selected["org_uuid"])


def session_limit_route(
    selected: dict,
    current_family: str,
    limit_kind: str,
    router_pid: int,
) -> tuple[dict, str | None] | None:
    if limit_kind == "fable" and current_family != "fable":
        return None
    next_profile = accounts.select_profile(
        avoid_labels={selected["label"]},
        require_fable=current_family == "fable",
        lease_pid=router_pid,
    )
    if (
        next_profile is not None
        and next_profile["label"] != selected["label"]
    ):
        return next_profile, None
    if current_family != "fable":
        return None
    fallback_model = os.environ.get(
        "ACCOUNTS_FABLE_FALLBACK_MODEL",
        FABLE_FALLBACK_MODEL,
    )
    next_profile = accounts.select_profile(
        avoid_labels={selected["label"]},
        require_fable=False,
        prefer_fable=False,
        lease_pid=router_pid,
    )
    if (
        next_profile is not None
        and next_profile["label"] == selected["label"]
    ):
        next_profile = None
    if (
        next_profile is None
        and limit_kind == "fable"
        and not accounts.profile_general_exhausted(selected["label"])
    ):
        next_profile = accounts.select_profile(
            require_fable=False,
            prefer_fable=False,
            lease_pid=router_pid,
            force_label=selected["label"],
        )
    if next_profile is None:
        return None
    return next_profile, fallback_model


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
    synchronized = set_synchronized_output(True)
    try:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    finally:
        if synchronized:
            set_synchronized_output(False)


def run_passthrough(binary: str, args: list[str]) -> int:
    is_inference = any(arg in {"-p", "--print"} for arg in args)
    launch_args = (
        with_default_effort(args)
        if is_inference
        else list(args)
    )
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        if not is_inference:
            return subprocess.call([binary, *launch_args])
        env = os.environ.copy()
        env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
        env.pop(ULTRACODE_ENV, None)
        if option_value(launch_args, "--effort") == ULTRACODE_EFFORT:
            env[ULTRACODE_ENV] = "1"
        return subprocess.call([binary, *launch_args], env=env)
    current_family = model_family(launch_args)
    selected = accounts.select_profile(require_fable=current_family == "fable")
    if selected is None and current_family == "fable":
        selected = accounts.select_profile(
            require_fable=False,
            prefer_fable=False,
        )
        if selected is not None:
            launch_args = replace_model_args(
                launch_args,
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
        env=routed_environment(selected, state_path, launch_args),
    )


def run_supervised(binary: str, args: list[str]) -> int:
    args = with_default_session_name(with_default_effort(args))
    launch_args, session_id = initial_session_args(args)
    router_pid = os.getpid()
    state_path = Path(f"/tmp/claude/account-router-{router_pid}.json")
    interval = float(os.environ.get("ACCOUNTS_ROUTER_INTERVAL", ROUTER_INTERVAL_S))
    mode, applied_mode_generation = accounts.load_mode_snapshot()
    current_model = model_name(args)
    current_effort = option_value(args, "--effort")
    current_family = "fable" if current_model == "fable" else "general"
    # An explicit non-fable --model is a user choice fable mode must honor.
    user_pinned_model = (
        current_model
        if option_value(args, "--model") and current_family != "fable"
        else None
    )
    if mode.get("mode") == "fable" and user_pinned_model is None:
        current_model = "fable"
        current_family = "fable"
        if option_value(launch_args, "--model") != current_model:
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
            prefer_fable=False,
            lease_pid=router_pid,
        )
        if selected is not None:
            launch_args = replace_model_args(launch_args, model_override)
            current_model = model_override
            current_family = "general"
    if selected is None:
        print("accounts: no account has enough quota for this model", file=sys.stderr)
        return 1
    os.environ.pop("ACCOUNTS_PIN", None)

    if os.environ.pop("ACCOUNTS_ROUTER_OUTPUT_FROZEN", "") == "1":
        set_synchronized_output(False)
    try:
        while True:
            state_path.unlink(missing_ok=True)
            env = routed_environment(
                selected,
                state_path,
                launch_args,
                mode.get("policy_scope", "global"),
            )
            accounts.upsert_session_lease(
                router_pid,
                session_id,
                selected["label"],
                current_family,
            )
            child_started_at = time.time()
            watched_session_id = session_id
            transcript_path = session_transcript_path(watched_session_id)
            transcript_offset = transcript_size(transcript_path)
            transcript_not_before = (
                child_started_at if transcript_path is None else None
            )
            child = subprocess.Popen([binary, *launch_args], env=env)
            last_heartbeat = time.monotonic()
            handoff = False
            limit_route = None
            limit_rejected = None
            while child.poll() is None:
                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    continue
                state = read_router_state(state_path)
                session_id = state.get("session_id") or session_id
                if session_id != watched_session_id:
                    watched_session_id = session_id
                    transcript_path = session_transcript_path(watched_session_id)
                    transcript_offset = 0
                    transcript_not_before = child_started_at
                elif transcript_path is None:
                    transcript_path = session_transcript_path(watched_session_id)
                rendered_model = state.get("model")
                if rendered_model:
                    mapped_model = model_name(args, rendered_model)
                    if (
                        current_model is not None
                        and mapped_model
                        and mapped_model != current_model
                    ):
                        # A live /model switch: pin the user's non-fable
                        # choice so fable mode stops reasserting over it.
                        user_pinned_model = (
                            mapped_model if mapped_model != "fable" else None
                        )
                        model_override = None
                    current_model = mapped_model
                    current_family = (
                        "fable" if current_model == "fable" else "general"
                    )
                    if (
                        model_override
                        and current_model
                        and current_model != model_override
                    ):
                        model_override = None
                rendered_effort = state.get("effort")
                if isinstance(rendered_effort, str) and rendered_effort:
                    current_effort = rendered_effort
                if transcript_path is not None:
                    detected_limit, transcript_offset = read_new_session_limit(
                        transcript_path,
                        transcript_offset,
                        not_before=transcript_not_before,
                    )
                    if detected_limit is not None and limit_rejected is None:
                        limit_rejected = detected_limit
                        mark_detected_limit(selected, detected_limit)
                if limit_rejected:
                    limit_route = session_limit_route(
                        selected,
                        current_family,
                        limit_rejected,
                        router_pid,
                    )
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
                if limit_rejected:
                    if limit_route is None:
                        continue
                    next_profile, next_override = limit_route
                    next_model = next_override or current_model
                else:
                    mode, mode_generation = accounts.load_mode_snapshot()
                    mode_changed = mode_generation != applied_mode_generation
                    if mode_changed and mode.get("mode") == "fable":
                        user_pinned_model = None
                        next_profile = accounts.select_profile(
                            require_fable=True,
                            lease_pid=router_pid,
                        )
                        if next_profile is None:
                            next_model = os.environ.get(
                                "ACCOUNTS_FABLE_FALLBACK_MODEL",
                                FABLE_FALLBACK_MODEL,
                            )
                            next_override = next_model
                            next_profile = accounts.select_profile(
                                require_fable=False,
                                prefer_fable=False,
                                lease_pid=router_pid,
                            )
                        else:
                            next_model = "fable"
                            next_override = None
                        if next_profile is None:
                            continue
                    elif mode_changed and mode.get("mode") in ("auto", "set"):
                        next_model = model_override or current_model
                        next_override = model_override
                        next_profile = accounts.select_profile(
                            require_fable=current_family == "fable",
                            lease_pid=router_pid,
                        )
                        if next_profile is None and current_family == "fable":
                            next_model = os.environ.get(
                                "ACCOUNTS_FABLE_FALLBACK_MODEL",
                                FABLE_FALLBACK_MODEL,
                            )
                            next_override = next_model
                            next_profile = accounts.select_profile(
                                require_fable=False,
                                prefer_fable=False,
                                lease_pid=router_pid,
                            )
                        if next_profile is None:
                            continue
                    elif (
                        mode.get("mode") == "fable"
                        and current_family != "fable"
                        and user_pinned_model is None
                    ):
                        next_profile = accounts.select_profile(
                            require_fable=True,
                            lease_pid=router_pid,
                        )
                        if next_profile is None:
                            continue
                        next_model = "fable"
                        next_override = None
                    elif current_family == "fable":
                        target = accounts.handoff_target(
                            selected["label"],
                            require_fable=True,
                        )
                        if target:
                            next_profile = accounts.select_profile(
                                avoid_labels={selected["label"]},
                                require_fable=True,
                                lease_pid=router_pid,
                            )
                            if (
                                next_profile is None
                                or next_profile["label"] != target
                            ):
                                continue
                            next_model = "fable"
                            next_override = None
                        elif accounts.profile_fable_exhausted(
                            selected["label"]
                        ):
                            next_model = os.environ.get(
                                "ACCOUNTS_FABLE_FALLBACK_MODEL",
                                FABLE_FALLBACK_MODEL,
                            )
                            next_override = next_model
                            next_profile = accounts.select_profile(
                                require_fable=False,
                                prefer_fable=False,
                                lease_pid=router_pid,
                            )
                            if next_profile is None:
                                continue
                        else:
                            continue
                    elif (
                        mode.get("mode") == "set"
                        and mode.get("label")
                        and mode.get("label") != selected["label"]
                    ):
                        target = accounts.handoff_target(
                            selected["label"],
                            require_fable=False,
                        )
                        if not target:
                            continue
                        next_model = model_override or current_model
                        next_override = model_override
                        next_profile = accounts.select_profile(
                            avoid_labels={selected["label"]},
                            require_fable=current_family == "fable",
                            lease_pid=router_pid,
                        )
                        if next_profile is None or next_profile["label"] != target:
                            continue
                    elif mode.get("mode") == "auto" and accounts.profile_near_wall(
                        selected["label"]
                    ):
                        # Fable has its own departure trigger above; general work
                        # had none, so an auto session rode its account to a hard
                        # 429 with idle accounts on the board.
                        target = accounts.handoff_target(
                            selected["label"],
                            require_fable=False,
                            margin_pct=accounts.HANDOFF_MARGIN_PCT,
                        )
                        if not target:
                            continue
                        next_profile = accounts.select_profile(
                            avoid_labels={selected["label"]},
                            require_fable=False,
                            lease_pid=router_pid,
                        )
                        if next_profile is None or next_profile["label"] != target:
                            continue
                        next_model = model_override or current_model
                        next_override = model_override
                    else:
                        continue
                    applied_mode_generation = mode_generation
                stop_for_handoff(child)
                launch_args = handoff_session_args(
                    args,
                    session_id,
                    next_model,
                    current_effort,
                )
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
                state = read_router_state(state_path)
                session_id = state.get("session_id") or session_id
                if session_id != watched_session_id:
                    watched_session_id = session_id
                    transcript_path = session_transcript_path(watched_session_id)
                    transcript_offset = 0
                    transcript_not_before = child_started_at
                elif transcript_path is None:
                    transcript_path = session_transcript_path(watched_session_id)
                rendered_model = state.get("model")
                if rendered_model:
                    mapped_model = model_name(args, rendered_model)
                    if (
                        current_model is not None
                        and mapped_model
                        and mapped_model != current_model
                    ):
                        user_pinned_model = (
                            mapped_model if mapped_model != "fable" else None
                        )
                        model_override = None
                    current_model = mapped_model
                    current_family = (
                        "fable" if current_model == "fable" else "general"
                    )
                rendered_effort = state.get("effort")
                if isinstance(rendered_effort, str) and rendered_effort:
                    current_effort = rendered_effort
                if transcript_path is not None:
                    detected_limit, transcript_offset = read_new_session_limit(
                        transcript_path,
                        transcript_offset,
                        not_before=transcript_not_before,
                    )
                    if detected_limit is not None and limit_rejected is None:
                        limit_rejected = detected_limit
                        mark_detected_limit(selected, detected_limit)
                if limit_rejected:
                    limit_route = session_limit_route(
                        selected,
                        current_family,
                        limit_rejected,
                        router_pid,
                    )
                if not limit_rejected or limit_route is None:
                    return child.wait()
                next_profile, next_override = limit_route
                next_model = next_override or current_model
                launch_args = handoff_session_args(
                    args,
                    session_id,
                    next_model,
                    current_effort,
                )
                selected = next_profile
                model_override = next_override
                current_model = next_model
                current_family = (
                    model_family(["--model", next_model])
                    if next_model
                    else current_family
                )
                handoff = True
    finally:
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
