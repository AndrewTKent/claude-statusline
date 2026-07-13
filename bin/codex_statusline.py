#!/usr/bin/env python3
"""Local Codex session monitor."""

from __future__ import annotations

import argparse
import base64
from contextlib import closing
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
STATE_DB = CODEX_HOME / "state_5.sqlite"
CONFIG_FILE = CODEX_HOME / "statusline.conf"
DEFAULT_BAR_WIDTH = 15
MAX_ROLLOUT_CACHE = 64
MAX_ROLLOUT_CACHE_EVENTS = 20_000
MAX_ROLLOUT_CACHE_PARTIAL_BYTES = 1_048_576


def terminal_size() -> os.terminal_size:
    return shutil.get_terminal_size((120, 40))


def default_width() -> int:
    try:
        return int(os.environ.get("COLUMNS") or terminal_size().columns)
    except ValueError:
        return terminal_size().columns


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.blue = self._c("38;2;0;153;255")
        self.orange = self._c("38;2;255;176;85")
        self.green = self._c("38;2;0;175;80")
        self.cyan = self._c("38;2;86;182;194")
        self.red = self._c("38;2;255;85;85")
        self.yellow = self._c("38;2;230;200;0")
        self.white = self._c("38;2;220;220;220")
        self.magenta = self._c("38;2;180;140;255")
        self.dim = self._c("38;2;120;120;120")
        self.reset = "\033[0m" if enabled else ""

    def _c(self, code: str) -> str:
        return f"\033[{code}m" if self.enabled else ""


@dataclass
class Thread:
    id: str
    source: str
    rollout_path: str
    created_at: int
    updated_at: int
    cwd: str
    title: str
    tokens_used: int
    model: str
    reasoning_effort: str
    sandbox_policy: str
    approval_mode: str
    git_branch: str
    archived: int
    agent_path: str = ""
    agent_nickname: str = ""


@dataclass
class TokenSummary:
    session: int
    today: int
    week: int
    lifetime: int
    threads_today: int
    threads_total: int


@dataclass
class CodexUsage:
    context_window: int
    context_used: int
    turn_total: int
    turn_cached: int
    session_total: int
    session_input: int
    session_cached: int
    session_output: int
    session_reasoning: int
    rate_limits: dict[str, Any]


@dataclass
class RolloutActivity:
    turns_started: int
    turns_completed: int
    turns_aborted: int
    compactions: int
    tool_calls: int
    shell_calls: int
    patch_calls: int
    active_tools: int
    active_shells: int
    active_turn_seconds: int
    last_event: str
    last_command: str
    last_user_message: str
    last_agent_message: str
    active_tool: str
    last_tool: str


@dataclass
class RolloutCacheEntry:
    inode: int
    offset: int
    partial: bytes
    events: list[dict[str, Any]]


ROLLOUT_CACHE: OrderedDict[str, RolloutCacheEntry] = OrderedDict()


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def parse_shell_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value
    return values


def setting(name: str, config: dict[str, str], default: str = "") -> str:
    return os.environ.get(f"CODEX_{name}", os.environ.get(name, config.get(name, default)))


def int_setting(name: str, config: dict[str, str], default: int) -> int:
    try:
        return int(setting(name, config, str(default)))
    except ValueError:
        return default


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except (ValueError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def account_label() -> str:
    auth = read_json(CODEX_HOME / "auth.json")
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    claims = decode_jwt_payload(str(tokens.get("id_token", "")))
    for key in ("email", "preferred_username", "name"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id:
        return account_id[:12]
    mode = auth.get("auth_mode")
    return str(mode) if mode else "unknown"


@lru_cache(maxsize=1)
def read_codex_config() -> dict[str, Any]:
    path = CODEX_HOME / "config.toml"
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def sqlite_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
    conn.row_factory = sqlite3.Row
    return conn


IDLE_AFTER_SECONDS = 600
IDLE_POLL_SECONDS = 30.0
WAL_GUARD_BYTES = 128 * 1024 * 1024
WAL_GUARD_MIN_INTERVAL_SECONDS = 60.0


def next_sleep_seconds(interval: float, latest_activity_ms: int, now_ms: int) -> float:
    # Idle sessions back off so forgotten watchers can't starve WAL checkpoints.
    if latest_activity_ms and now_ms - latest_activity_ms > IDLE_AFTER_SECONDS * 1000:
        return max(IDLE_POLL_SECONDS, interval)
    return max(0.5, interval)


def owner_alive(owner_pid_file: str) -> bool:
    """False only for a readable pid whose process is gone; missing/partial files
    stay True because the launcher writes the pid after the footer pane starts."""
    try:
        pid = int(Path(owner_pid_file).read_text().strip())
    except (OSError, ValueError):
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def maybe_checkpoint_wal(
    db_path: Path,
    *,
    last_attempt: float,
    now: float,
    threshold_bytes: int = WAL_GUARD_BYTES,
) -> float:
    """Best-effort TRUNCATE checkpoint once the WAL passes the threshold.

    Codex's own passive checkpoints starve under overlapping readers; left alone
    the WAL grows without bound and every query slows (observed 727 MB)."""
    wal = Path(f"{db_path}-wal")
    try:
        size = wal.stat().st_size
    except OSError:
        return last_attempt
    if size < threshold_bytes or now - last_attempt < WAL_GUARD_MIN_INTERVAL_SECONDS:
        return last_attempt
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=0.2)
        try:
            conn.execute("PRAGMA busy_timeout=200")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return now


THREAD_COLUMNS = (
    "id, source, rollout_path, created_at, updated_at, cwd, title, tokens_used, model, "
    "reasoning_effort, sandbox_policy, approval_mode, git_branch, archived, "
    "agent_path, agent_nickname"
)


def row_to_thread(row: sqlite3.Row) -> Thread:
    return Thread(
        id=row["id"],
        source=row["source"] or "",
        rollout_path=row["rollout_path"],
        created_at=int(row["created_at"] or 0),
        updated_at=int(row["updated_at"] or 0),
        cwd=row["cwd"] or "",
        title=row["title"] or "",
        tokens_used=int(row["tokens_used"] or 0),
        model=row["model"] or "",
        reasoning_effort=row["reasoning_effort"] or "",
        sandbox_policy=row["sandbox_policy"] or "",
        approval_mode=row["approval_mode"] or "",
        git_branch=row["git_branch"] or "",
        archived=int(row["archived"] or 0),
        agent_path=row["agent_path"] or "",
        agent_nickname=row["agent_nickname"] or "",
    )


def paths_related(a: str, b: str) -> bool:
    if not a or not b:
        return False
    left = os.path.abspath(os.path.expanduser(a))
    right = os.path.abspath(os.path.expanduser(b))
    return left == right or left.startswith(right + os.sep) or right.startswith(left + os.sep)


def select_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    cwd: str,
    *,
    created_after_ms: int = 0,
    updated_after_ms: int = 0,
) -> Thread | None:
    if thread_id:
        row = conn.execute(f"select {THREAD_COLUMNS} from threads where id = ?", (thread_id,)).fetchone()
        return row_to_thread(row) if row else None

    if created_after_ms:
        rows = conn.execute(
            f"select {THREAD_COLUMNS} from threads where archived = 0 "
            "and coalesce(created_at_ms, created_at * 1000) >= ? "
            "order by coalesce(created_at_ms, created_at * 1000) limit 50",
            (created_after_ms,),
        ).fetchall()
    elif updated_after_ms:
        rows = conn.execute(
            f"select {THREAD_COLUMNS} from threads where archived = 0 "
            "and coalesce(updated_at_ms, updated_at * 1000) >= ? "
            "order by coalesce(updated_at_ms, updated_at * 1000) limit 50",
            (updated_after_ms,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"select {THREAD_COLUMNS} from threads where archived = 0 "
            "order by coalesce(updated_at_ms, updated_at * 1000) desc limit 50"
        ).fetchall()
    if created_after_ms or updated_after_ms:
        target_cwd = os.path.abspath(os.path.expanduser(cwd))
        related = [
            row_to_thread(row)
            for row in rows
            if os.path.abspath(os.path.expanduser(str(row["cwd"]))) == target_cwd
        ]
    else:
        related = [row_to_thread(row) for row in rows if paths_related(cwd, str(row["cwd"]))]
    for candidate in related:
        if not is_subagent_thread(candidate):
            return candidate
    if created_after_ms or updated_after_ms:
        return None
    if related:
        return related[0]
    return row_to_thread(rows[0]) if rows else None


def process_descendants(owner_pid: int) -> set[int]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {owner_pid}

    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent_pid = (int(field) for field in fields)
        except ValueError:
            continue
        children.setdefault(parent_pid, []).append(pid)

    descendants = {owner_pid}
    pending = [owner_pid]
    while pending:
        parent_pid = pending.pop()
        for child_pid in children.get(parent_pid, []):
            if child_pid not in descendants:
                descendants.add(child_pid)
                pending.append(child_pid)
    return descendants


def process_rollout_paths(owner_pid: int) -> set[str]:
    pids = process_descendants(owner_pid)
    proc_root = Path("/proc")
    if proc_root.is_dir():
        paths: set[str] = set()
        for pid in pids:
            try:
                descriptors = (proc_root / str(pid) / "fd").iterdir()
                for descriptor in descriptors:
                    try:
                        target = str(descriptor.resolve(strict=True))
                    except OSError:
                        continue
                    name = Path(target).name
                    if name.startswith("rollout-") and name.endswith(".jsonl"):
                        paths.add(target)
            except OSError:
                continue
        return paths

    try:
        result = subprocess.run(
            ["lsof", "-n", "-P", "-Fn", "-p", ",".join(str(pid) for pid in sorted(pids))],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    paths = set()
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        path = line[1:]
        name = Path(path).name
        if name.startswith("rollout-") and name.endswith(".jsonl"):
            paths.add(path)
    return paths


def select_owner_thread_id(conn: sqlite3.Connection, owner_pid_file: str) -> str:
    try:
        owner_pid = int(Path(owner_pid_file).read_text().strip())
    except (OSError, ValueError):
        return ""

    rollout_paths = process_rollout_paths(owner_pid)
    if not rollout_paths:
        return ""
    placeholders = ",".join("?" for _ in rollout_paths)
    rows = conn.execute(
        f"select {THREAD_COLUMNS} from threads where rollout_path in ({placeholders}) "
        "order by coalesce(updated_at_ms, updated_at * 1000) desc",
        tuple(rollout_paths),
    ).fetchall()
    if not rows:
        # Symlinked CODEX_HOME: fd scans report realpaths while the DB stores the opened path.
        recent = conn.execute(
            f"select {THREAD_COLUMNS} from threads "
            "order by coalesce(updated_at_ms, updated_at * 1000) desc limit 200"
        ).fetchall()
        rows = [row for row in recent if os.path.realpath(row["rollout_path"]) in rollout_paths]
    # Prefer the interactive root: a nested `codex exec` in the same tree is also a root.
    fallback = ""
    for row in rows:
        thread = row_to_thread(row)
        if is_subagent_thread(thread):
            continue
        if thread.source == "cli":
            return thread.id
        if not fallback:
            fallback = thread.id
    return fallback


def select_threads(conn: sqlite3.Connection, limit: int, include_archived: bool) -> list[Thread]:
    where = "" if include_archived else "where archived = 0"
    query_limit = max(50, max(1, limit) * 4)
    rows = conn.execute(
        f"select {THREAD_COLUMNS} from threads {where} "
        "order by coalesce(updated_at_ms, updated_at * 1000) desc limit ?",
        (query_limit,),
    ).fetchall()
    threads = [row_to_thread(row) for row in rows]
    return [thread for thread in threads if Path(thread.rollout_path).is_file()][:max(1, limit)]


def select_top_threads(conn: sqlite3.Connection, limit: int) -> list[Thread]:
    try:
        rows = conn.execute(
            f"""
            with recursive recent_roots as (
                select id from threads
                where source in ('cli', 'exec') and archived = 0
                order by coalesce(updated_at_ms, updated_at * 1000) desc
                limit ?
            ), selected(id) as (
                select id from recent_roots
                union
                select edge.child_thread_id from thread_spawn_edges edge
                join selected parent on edge.parent_thread_id = parent.id
                where edge.status = 'open'
            )
            select {THREAD_COLUMNS} from threads
            where archived = 0 and id in (select id from selected)
            order by coalesce(updated_at_ms, updated_at * 1000) desc
            """,
            (max(1, limit),),
        ).fetchall()
    except sqlite3.Error:
        return select_threads(conn, limit, False)
    threads = [row_to_thread(row) for row in rows]
    return [thread for thread in threads if Path(thread.rollout_path).is_file()][:max(1, limit)]


def select_descendant_threads(conn: sqlite3.Connection, parent_thread_id: str) -> list[Thread]:
    try:
        rows = conn.execute(
            f"""
            with recursive descendants(id) as (
                select child_thread_id from thread_spawn_edges where parent_thread_id = ?
                union
                select edge.child_thread_id from thread_spawn_edges edge
                join descendants parent on edge.parent_thread_id = parent.id
            )
            select {THREAD_COLUMNS} from threads where id in (select id from descendants) and id != ?
            """,
            (parent_thread_id, parent_thread_id),
        ).fetchall()
    except sqlite3.Error:
        return []
    threads = [row_to_thread(row) for row in rows]
    return [thread for thread in threads if Path(thread.rollout_path).is_file()]


def local_midnight(now: datetime) -> int:
    local_now = datetime.fromtimestamp(now.timestamp())
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def week_start(now: datetime) -> int:
    local_now = datetime.fromtimestamp(now.timestamp())
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=start.weekday())
    return int(start.timestamp())


def tokens_since_boundary(rollout_path: str, current_total: int, boundary: int) -> int:
    baseline = 0
    saw_token_count = False
    for item in read_rollout_events(rollout_path):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if item.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
        value = total.get("total_tokens")
        if not isinstance(value, int):
            continue
        saw_token_count = True
        if event_timestamp(item) < boundary:
            baseline = value
    if not saw_token_count:
        return 0
    return max(0, current_total - baseline)


def token_summary(conn: sqlite3.Connection, thread: Thread, now: datetime) -> TokenSummary:
    today_start = local_midnight(now)
    week_start_ts = week_start(now)
    row = conn.execute(
        """
        select
            coalesce(sum(case when created_at >= ? then tokens_used else 0 end), 0) as today,
            coalesce(sum(case when created_at >= ? then tokens_used else 0 end), 0) as week,
            coalesce(sum(tokens_used), 0) as lifetime,
            coalesce(sum(case when created_at >= ? then 1 else 0 end), 0) as threads_today,
            count(*) as threads_total
        from threads
        """,
        (today_start, week_start_ts, today_start),
    ).fetchone()
    today = int(row["today"] or 0)
    week = int(row["week"] or 0)
    older_active_threads = conn.execute(
        "select rollout_path, created_at, updated_at, tokens_used from threads "
        "where created_at < ? and updated_at >= ?",
        (today_start, week_start_ts),
    ).fetchall()
    for older_thread in older_active_threads:
        created_at = int(older_thread["created_at"] or 0)
        updated_at = int(older_thread["updated_at"] or 0)
        current_total = int(older_thread["tokens_used"] or 0)
        rollout_path = str(older_thread["rollout_path"] or "")
        if created_at < today_start and updated_at >= today_start:
            today += tokens_since_boundary(rollout_path, current_total, today_start)
        if created_at < week_start_ts:
            week += tokens_since_boundary(rollout_path, current_total, week_start_ts)
    return TokenSummary(
        session=thread.tokens_used,
        today=today,
        week=week,
        lifetime=int(row["lifetime"] or 0),
        threads_today=int(row["threads_today"] or 0),
        threads_total=int(row["threads_total"] or 0),
    )


def latest_token_count(rollout_path: str) -> dict[str, Any]:
    for item in reversed(read_rollout_events(rollout_path)):
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        if item.get("type") == "event_msg" and payload.get("type") == "token_count":
            return payload
    return {}


def compact_rollout_event(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = str(item.get("type") or "")
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "")
    compact: dict[str, Any] = {"type": item_type}
    if isinstance(item.get("timestamp"), str):
        compact["timestamp"] = item["timestamp"]

    if item_type == "compacted":
        return compact
    if item_type == "event_msg" and payload_type in {"exec_command_end", "patch_apply_end"}:
        compact["payload"] = {
            key: payload[key]
            for key in ("type", "call_id", "command")
            if key in payload
        }
        return compact
    if item_type == "event_msg":
        if payload_type not in {
            "agent_message",
            "context_compacted",
            "task_complete",
            "task_started",
            "token_count",
            "turn_aborted",
            "user_message",
        }:
            return None
        if payload_type == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            compact_info: dict[str, Any] = {}
            if isinstance(info.get("model_context_window"), int):
                compact_info["model_context_window"] = info["model_context_window"]
            for usage_key in ("last_token_usage", "total_token_usage"):
                usage = info.get(usage_key) if isinstance(info.get(usage_key), dict) else {}
                compact_info[usage_key] = {
                    key: value
                    for key, value in usage.items()
                    if isinstance(value, (int, float))
                }
            rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
            compact_limits: dict[str, Any] = {}
            for limit_key in ("primary", "secondary"):
                limit = rate_limits.get(limit_key) if isinstance(rate_limits.get(limit_key), dict) else {}
                compact_limits[limit_key] = {
                    key: value
                    for key, value in limit.items()
                    if isinstance(value, (int, float, str, bool)) or value is None
                }
            plan_type = rate_limits.get("plan_type")
            if isinstance(plan_type, str):
                compact_limits["plan_type"] = short_text(plan_type, 64)
            compact["payload"] = {
                "type": payload_type,
                "info": compact_info,
                "rate_limits": compact_limits,
            }
            return compact
        compact_payload: dict[str, Any] = {"type": payload_type}
        for key in ("turn_id", "started_at"):
            if key in payload:
                compact_payload[key] = payload[key]
        message = payload.get("message")
        if isinstance(message, str):
            compact_payload["message"] = short_text(message, 256)
        compact["payload"] = compact_payload
        return compact
    if payload_type in {"function_call", "custom_tool_call"}:
        compact_payload = {
            key: payload[key]
            for key in ("type", "call_id", "name")
            if key in payload
        }
        command = command_text(payload)
        if command:
            compact_payload["command"] = short_text(command, 512)
        session_id = tool_input_session(payload)
        if session_id:
            compact_payload["input"] = json.dumps({"session_id": session_id})
        compact["payload"] = compact_payload
        return compact
    if payload_type in {"function_call_output", "custom_tool_call_output"}:
        compact_payload = {
            key: payload[key]
            for key in ("type", "call_id")
            if key in payload
        }
        result = tool_result(payload)
        output = {key: result[key] for key in ("session_id", "exit_code") if key in result}
        if output:
            compact_payload["output"] = output
        compact["payload"] = compact_payload
        return compact
    return None


def decode_rollout_line(line: bytes) -> dict[str, Any] | None:
    try:
        item = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return compact_rollout_event(item) if isinstance(item, dict) else None


def cached_events(entry: RolloutCacheEntry) -> list[dict[str, Any]]:
    if not entry.partial:
        return entry.events
    item = decode_rollout_line(entry.partial)
    return [*entry.events, item] if item is not None else entry.events


def trim_rollout_cache(current_path: str) -> None:
    while len(ROLLOUT_CACHE) > MAX_ROLLOUT_CACHE:
        ROLLOUT_CACHE.popitem(last=False)
    retained_events = sum(len(entry.events) for entry in ROLLOUT_CACHE.values())
    while retained_events > MAX_ROLLOUT_CACHE_EVENTS and ROLLOUT_CACHE:
        path, entry = ROLLOUT_CACHE.popitem(last=False)
        retained_events -= len(entry.events)
        if path == current_path:
            break
    retained_partial_bytes = sum(len(entry.partial) for entry in ROLLOUT_CACHE.values())
    while retained_partial_bytes > MAX_ROLLOUT_CACHE_PARTIAL_BYTES and ROLLOUT_CACHE:
        path, entry = ROLLOUT_CACHE.popitem(last=False)
        retained_partial_bytes -= len(entry.partial)
        if path == current_path:
            break


def read_rollout_events(rollout_path: str) -> list[dict[str, Any]]:
    path = Path(rollout_path)
    try:
        stat = path.stat()
    except OSError:
        return []

    entry = ROLLOUT_CACHE.get(rollout_path)
    fresh = entry is None or entry.inode != stat.st_ino or stat.st_size < entry.offset
    if fresh:
        entry = RolloutCacheEntry(stat.st_ino, 0, b"", [])
        ROLLOUT_CACHE[rollout_path] = entry
    ROLLOUT_CACHE.move_to_end(rollout_path)

    try:
        with path.open("rb") as stream:
            stream.seek(entry.offset)
            chunk = stream.read()
            entry.offset = stream.tell()
    except OSError:
        if fresh:
            ROLLOUT_CACHE.pop(rollout_path, None)
        return cached_events(entry)

    if not chunk:
        events = cached_events(entry)
        trim_rollout_cache(rollout_path)
        return events

    data = entry.partial + chunk
    lines = data.split(b"\n")
    entry.partial = lines.pop()

    for line in lines:
        if not line:
            continue
        item = decode_rollout_line(line)
        if item is not None:
            entry.events.append(item)
    events = cached_events(entry)
    trim_rollout_cache(rollout_path)
    return events


def short_text(value: str, max_len: int = 90) -> str:
    text = " ".join(value.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def event_timestamp(item: dict[str, Any]) -> int:
    raw = item.get("timestamp")
    if not isinstance(raw, str):
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def tool_arguments(payload: dict[str, Any]) -> str:
    for key in ("input", "arguments"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def command_text(payload: dict[str, Any]) -> str:
    command = payload.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    if isinstance(command, str):
        return command
    raw_input = tool_arguments(payload)
    if not raw_input:
        return ""
    try:
        parsed = json.loads(raw_input)
    except json.JSONDecodeError:
        return raw_input
    if not isinstance(parsed, dict):
        return raw_input
    nested = parsed.get("cmd", parsed.get("command", ""))
    if isinstance(nested, list):
        return " ".join(str(part) for part in nested)
    if isinstance(nested, str):
        return nested
    return ""


def tool_input_session(payload: dict[str, Any]) -> str:
    raw_input = tool_arguments(payload)
    if not raw_input:
        return ""
    match = re.search(r"session_id[\"']?\s*:\s*[\"']?([A-Za-z0-9_-]+)", raw_input)
    return match.group(1) if match else ""


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    output = payload.get("output")
    if isinstance(output, dict):
        return output
    candidates = [output] if isinstance(output, str) else []
    if isinstance(output, list):
        candidates.extend(
            item.get("text")
            for item in output
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def is_subagent_thread(thread: Thread) -> bool:
    # Plain "cli"/"exec" sources are roots; spawned children carry JSON subagent sources.
    if thread.agent_path or thread.agent_nickname:
        return True
    return "subagent" in thread.source


def agent_label(thread: Thread) -> str:
    if thread.agent_path:
        return thread.agent_path.rstrip("/").rsplit("/", 1)[-1]
    if thread.agent_nickname:
        return thread.agent_nickname
    return "root"


def rollout_activity(thread: Thread, now: datetime, active_window_seconds: int = 900) -> RolloutActivity:
    events = read_rollout_events(thread.rollout_path)
    if is_subagent_thread(thread):
        child_start = next(
            (
                index
                for index, item in enumerate(events)
                if isinstance(item.get("payload"), dict)
                and item["payload"].get("type") == "task_started"
                and int(item["payload"].get("started_at") or 0) >= thread.created_at
            ),
            len(events),
        )
        events = events[child_start:]
    started_turns: dict[str, int] = {}
    closed_turns: set[str] = set()
    pending_tools: dict[str, str] = {}
    pending_shells: set[str] = set()
    running_shells: set[str] = set()
    tool_sessions: dict[str, str] = {}

    turns_started = 0
    turns_completed = 0
    turns_aborted = 0
    compactions = 0
    tool_calls = 0
    shell_calls = 0
    patch_calls = 0
    last_event = ""
    last_command = ""
    last_user_message = ""
    last_agent_message = ""
    last_tool = ""

    for item in events:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        item_type = str(item.get("type", ""))
        payload_type = str(payload.get("type", ""))
        last_event = payload_type or item_type

        if item_type == "compacted" or payload_type == "context_compacted":
            compactions += 1

        if payload_type == "task_started":
            turn_id = str(payload.get("turn_id") or "")
            turns_started += 1
            if turn_id:
                started_turns[turn_id] = int(payload.get("started_at") or event_timestamp(item) or 0)
            continue

        if payload_type == "task_complete":
            turn_id = str(payload.get("turn_id") or "")
            turns_completed += 1
            if turn_id:
                closed_turns.add(turn_id)
            continue

        if payload_type == "turn_aborted":
            turn_id = str(payload.get("turn_id") or "")
            turns_aborted += 1
            if turn_id:
                closed_turns.add(turn_id)
            continue

        if payload_type == "user_message":
            message = payload.get("message")
            if isinstance(message, str) and message:
                last_user_message = short_text(message)
            continue

        if payload_type == "agent_message":
            message = payload.get("message")
            if isinstance(message, str) and message:
                last_agent_message = short_text(message)
            continue

        if payload_type in {"function_call", "custom_tool_call"}:
            call_id = str(payload.get("call_id") or "")
            name = str(payload.get("name") or "")
            if call_id:
                pending_tools[call_id] = name
            tool_calls += 1
            last_tool = name
            session_id = tool_input_session(payload)
            if call_id and session_id:
                tool_sessions[call_id] = session_id
            if name in {"exec", "exec_command"}:
                shell_calls += 1
                if call_id and not session_id:
                    pending_shells.add(call_id)
            if name == "apply_patch":
                patch_calls += 1
            command = command_text(payload)
            if command:
                last_command = short_text(command)
            continue

        if payload_type in {"function_call_output", "custom_tool_call_output", "patch_apply_end"}:
            call_id = str(payload.get("call_id") or "")
            pending_tools.pop(call_id, None)
            pending_shells.discard(call_id)
            input_session = tool_sessions.pop(call_id, "")
            result = tool_result(payload)
            result_session = str(result.get("session_id") or "")
            if input_session and "exit_code" in result:
                running_shells.discard(input_session)
            elif result_session:
                running_shells.add(result_session)
            continue

        if payload_type == "exec_command_end":
            call_id = str(payload.get("call_id") or "")
            pending_tools.pop(call_id, None)
            pending_shells.discard(call_id)
            command = command_text(payload)
            if command:
                last_command = short_text(command)

    active_turn_seconds = 0
    active_starts = [started_at for turn_id, started_at in started_turns.items() if turn_id not in closed_turns and started_at > 0]
    if active_starts:
        active_turn_seconds = max(0, int(now.timestamp()) - max(active_starts))
    if int(now.timestamp()) - thread.updated_at > active_window_seconds:
        active_turn_seconds = 0
        pending_tools.clear()
        pending_shells.clear()
        running_shells.clear()

    return RolloutActivity(
        turns_started=turns_started,
        turns_completed=turns_completed,
        turns_aborted=turns_aborted,
        compactions=compactions,
        tool_calls=tool_calls,
        shell_calls=shell_calls,
        patch_calls=patch_calls,
        active_tools=len(pending_tools),
        active_shells=len(pending_shells) + len(running_shells),
        active_turn_seconds=active_turn_seconds,
        last_event=last_event or "-",
        last_command=last_command or "-",
        last_user_message=last_user_message or "-",
        last_agent_message=last_agent_message or "-",
        active_tool=next(reversed(pending_tools.values()), "-"),
        last_tool=last_tool or "-",
    )


def usage_from_rollout(thread: Thread) -> CodexUsage:
    event = latest_token_count(thread.rollout_path)
    info = event.get("info") if isinstance(event.get("info"), dict) else {}
    last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
    total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
    rate_limits = event.get("rate_limits") if isinstance(event.get("rate_limits"), dict) else {}

    session_total = int(total.get("total_tokens") or thread.tokens_used or 0)
    context_window_value = info.get("model_context_window")
    context_window = int(context_window_value) if isinstance(context_window_value, int) else 0

    return CodexUsage(
        context_window=context_window,
        context_used=int(last.get("input_tokens") or 0),
        turn_total=int(last.get("total_tokens") or 0),
        turn_cached=int(last.get("cached_input_tokens") or 0),
        session_total=session_total,
        session_input=int(total.get("input_tokens") or 0),
        session_cached=int(total.get("cached_input_tokens") or 0),
        session_output=int(total.get("output_tokens") or 0),
        session_reasoning=int(total.get("reasoning_output_tokens") or 0),
        rate_limits=rate_limits,
    )


@lru_cache(maxsize=32)
def model_info(model: str) -> dict[str, Any]:
    cache = read_json(CODEX_HOME / "models_cache.json")
    models = cache.get("models", [])
    if not isinstance(models, list):
        return {}
    for item in models:
        if not isinstance(item, dict):
            continue
        if item.get("slug") == model or item.get("id") == model:
            return item
    return {}


def context_window(model: str) -> int:
    info = model_info(model)
    for key in ("context_window", "max_context_window"):
        value = info.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def display_model(model: str) -> str:
    info = model_info(model)
    value = info.get("display_name")
    return str(value) if value else (model or "unknown")


GIT_INFO_TTL_SECONDS = 5


@lru_cache(maxsize=128)
def git_info(cwd: str, fallback_branch: str, bucket: int) -> tuple[str, str]:
    path = cwd or os.getcwd()
    try:
        root = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.4,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return Path(path).name or "unknown", fallback_branch or "-"

    repo = Path(root).name
    branch = fallback_branch
    if not branch:
        try:
            branch = subprocess.check_output(
                ["git", "-C", root, "branch", "--show-current"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.4,
            ).strip()
        except (subprocess.SubprocessError, OSError):
            branch = ""
    if not branch:
        try:
            branch = subprocess.check_output(
                ["git", "-C", root, "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.4,
            ).strip()
        except (subprocess.SubprocessError, OSError):
            branch = "-"

    try:
        dirty = subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.7,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        dirty = ""
    return repo, f"{branch}{'*' if dirty else ''}"


def color_for_pct(pct: float, p: Palette) -> str:
    if pct >= 90:
        return p.red
    if pct >= 70:
        return p.yellow
    if pct >= 50:
        return p.orange
    return p.green


def build_bar(value: float, goal: float, width: int, p: Palette) -> tuple[str, float]:
    if goal <= 0:
        return f"{p.dim}{'○' * width}{p.reset}", 0.0
    pct = min(100.0, max(0.0, value * 100.0 / goal))
    filled = int(pct * width / 100)
    if pct > 0 and filled == 0:
        filled = 1
    empty = width - filled
    return f"{color_for_pct(pct, p)}{'●' * filled}{p.dim}{'○' * empty}{p.reset}", pct


def build_pct_bar(pct: float, width: int, p: Palette) -> str:
    pct = min(100.0, max(0.0, pct))
    filled = int(pct * width / 100)
    if pct > 0 and filled == 0:
        filled = 1
    empty = width - filled
    return f"{color_for_pct(pct, p)}{'●' * filled}{p.dim}{'○' * empty}{p.reset}"


def format_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes = seconds // 60
    hours, mins = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}:{mins:02d}"
    return f"{mins}m"


def format_pct(pct: float) -> str:
    text = f"{pct:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def format_clock(dt: datetime) -> str:
    hour = dt.strftime("%I").lstrip("0") or "0"
    minute = dt.strftime("%M")
    meridiem = dt.strftime("%p").lower()
    zone = dt.tzname() or ""
    return f"{hour}:{minute}{meridiem} {zone}".strip()


def format_reset(timestamp: Any, now: datetime | None = None) -> str:
    try:
        reset_at = datetime.fromtimestamp(int(timestamp)).astimezone()
    except (TypeError, ValueError, OSError):
        return "reset n/a"

    local_now = (now or datetime.now(timezone.utc)).astimezone()
    if reset_at.date() == local_now.date():
        return f"resets {format_clock(reset_at)}"
    if reset_at.year == local_now.year:
        return f"resets {reset_at.strftime('%b').lower()} {reset_at.day} {format_clock(reset_at)}"
    return f"resets {reset_at.strftime('%b').lower()} {reset_at.day} {reset_at.year}"


def sandbox_label(policy: str) -> str:
    if not policy:
        return "-"
    try:
        value = json.loads(policy)
    except json.JSONDecodeError:
        return policy
    if isinstance(value, dict):
        return str(value.get("type", "-"))
    return "-"


def render_goal_row(label: str, value: int, goal: int, width: int, p: Palette, indent: int = 8) -> str:
    bar, pct = build_bar(value, goal, width, p)
    detail = f"{format_tokens(value)}/{format_tokens(goal)}" if goal > 0 else format_tokens(value)
    pct_text = format_pct(pct).ljust(7) if goal > 0 else "goal n/a"
    return f"{' ' * indent}{label:<7} {bar} {pct_text} {detail}"


def render_rate_limit_row(label: str, limit: dict[str, Any], width: int, p: Palette, indent: int = 8) -> str:
    used_percent = float(limit.get("used_percent") or 0.0)
    bar = build_pct_bar(used_percent, width, p)
    reset = format_reset(limit.get("resets_at"))
    return f"{' ' * indent}{label:<7} {bar} {format_pct(used_percent).ljust(7)} {reset}"


def rate_limit_window_label(window_minutes: Any, fallback: str) -> str:
    if window_minutes is None:
        return fallback
    if isinstance(window_minutes, bool):
        return "limit"
    if isinstance(window_minutes, int):
        minutes = window_minutes
    elif isinstance(window_minutes, float):
        if not window_minutes.is_integer():
            return "limit"
        minutes = int(window_minutes)
    elif isinstance(window_minutes, str):
        try:
            minutes = int(window_minutes)
        except ValueError:
            return "limit"
    else:
        return "limit"

    if minutes <= 0:
        return "limit"
    if minutes == 300:
        return "5-hour"
    if minutes == 10_080:
        return "weekly"
    if minutes % 1_440 == 0:
        return f"{minutes // 1_440}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute"


def labeled_rate_limits(rate_limits: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    limits = []
    seen_labels = set()
    for key, fallback_label in (("primary", "5-hour"), ("secondary", "weekly")):
        limit = rate_limits.get(key)
        if not isinstance(limit, dict) or limit.get("used_percent") is None:
            continue
        label = rate_limit_window_label(limit.get("window_minutes"), fallback_label)
        if label in seen_labels:
            continue
        seen_labels.add(label)
        limits.append((label, limit))
    return limits


def snapshot_for_thread(
    thread: Thread,
    tokens: TokenSummary,
    codex_config: dict[str, Any],
    cwd: str,
    now: datetime,
    *,
    prefer_thread_cwd: bool = False,
    active_window_seconds: int = 900,
) -> dict[str, Any]:
    model = thread.model or str(codex_config.get("model", ""))
    effort = thread.reasoning_effort or str(codex_config.get("model_reasoning_effort", ""))
    usage = usage_from_rollout(thread)
    activity = rollout_activity(thread, now, active_window_seconds)
    tokens.session = usage.session_total
    repo_cwd = thread.cwd if prefer_thread_cwd else (cwd if paths_related(cwd, thread.cwd) else thread.cwd)
    repo, branch = git_info(repo_cwd, thread.git_branch, int(now.timestamp() // GIT_INFO_TTL_SECONDS))
    window = usage.context_window or context_window(model)

    return {
        "thread_id": thread.id,
        "title": thread.title,
        "model": model,
        "model_display": display_model(model),
        "reasoning_effort": effort,
        "context_window": window,
        "account": account_label(),
        "repo": repo,
        "branch": branch,
        "cwd": thread.cwd,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
        "session_age_seconds": int(now.timestamp()) - thread.created_at,
        "idle_seconds": int(now.timestamp()) - thread.updated_at,
        "sandbox": sandbox_label(thread.sandbox_policy),
        "approval_mode": thread.approval_mode or "-",
        "tokens": tokens.__dict__,
        "usage": usage.__dict__,
        "activity": activity.__dict__,
        "agent": agent_label(thread),
        "is_subagent": is_subagent_thread(thread),
    }


def snapshot(args: argparse.Namespace) -> dict[str, Any]:
    account_label.cache_clear()
    model_info.cache_clear()
    read_codex_config.cache_clear()
    config = parse_shell_config(Path(args.config) if args.config else CONFIG_FILE)
    codex_config = read_codex_config()
    cwd = args.cwd or os.getcwd()
    thread_id = args.thread_id

    if not STATE_DB.exists():
        raise RuntimeError(f"missing Codex state database: {STATE_DB}")

    with closing(sqlite_connect(STATE_DB)) as conn:
        if not thread_id and args.owner_pid_file:
            thread_id = select_owner_thread_id(conn, args.owner_pid_file)
            if not thread_id:
                raise RuntimeError("no Codex threads found")
        created_after_ms = args.bind_after_ms or args.bind_after * 1000
        thread = select_thread(
            conn,
            thread_id,
            cwd,
            created_after_ms=created_after_ms,
            updated_after_ms=args.bind_updated_after_ms,
        )
        if thread is None:
            raise RuntimeError("no Codex threads found")
        now = datetime.now(timezone.utc)
        tokens = token_summary(conn, thread, now)
        agent_activities = [
            rollout_activity(agent, now, args.active_window)
            for agent in select_descendant_threads(conn, thread.id)
        ]

    data = snapshot_for_thread(thread, tokens, codex_config, cwd, now)
    data["agents"] = {
        "total": len(agent_activities),
        "active": sum(
            1
            for activity in agent_activities
            if activity.active_turn_seconds > 0 or activity.active_tools > 0
        ),
        "active_tools": sum(activity.active_tools for activity in agent_activities),
        "active_shells": sum(activity.active_shells for activity in agent_activities),
    }
    goals = {
        "session": int_setting("SESSION_TOKEN_GOAL", config, 1_000_000),
        "today": int_setting("DAILY_TOKEN_GOAL", config, 2_000_000),
        "week": int_setting("WEEKLY_TOKEN_GOAL", config, 10_000_000),
        "lifetime": int_setting("LIFETIME_TOKEN_GOAL", config, 100_000_000),
    }
    data["goals"] = goals
    return data


def all_sessions_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    account_label.cache_clear()
    model_info.cache_clear()
    read_codex_config.cache_clear()
    codex_config = read_codex_config()
    cwd = args.cwd or os.getcwd()
    if not STATE_DB.exists():
        raise RuntimeError(f"missing Codex state database: {STATE_DB}")

    now = datetime.now(timezone.utc)
    with closing(sqlite_connect(STATE_DB)) as conn:
        if args.top and not args.show_inactive and not args.include_archived:
            threads = select_top_threads(conn, args.sessions)
        else:
            threads = select_threads(conn, args.sessions, args.include_archived)
        if args.top and not args.include_agents:
            threads = [thread for thread in threads if not is_subagent_thread(thread)]
        totals = token_summary(conn, threads[0], now) if threads else TokenSummary(0, 0, 0, 0, 0, 0)
        sessions = []
        for thread in threads:
            thread_tokens = TokenSummary(
                session=thread.tokens_used,
                today=totals.today,
                week=totals.week,
                lifetime=totals.lifetime,
                threads_today=totals.threads_today,
                threads_total=totals.threads_total,
            )
            sessions.append(
                snapshot_for_thread(
                    thread,
                    thread_tokens,
                    codex_config,
                    cwd,
                    now,
                    prefer_thread_cwd=True,
                    active_window_seconds=args.active_window,
                )
            )

    newest_rate_limits: dict[str, Any] = {}
    for session in sessions:
        rate_limits = session["usage"].get("rate_limits") or {}
        # Fresh sessions compact null limits into empty primary/secondary placeholders.
        if rate_limits.get("primary") or rate_limits.get("secondary"):
            newest_rate_limits = rate_limits
            break

    return {
        "account": account_label(),
        "sessions": sessions,
        "session_count": len(sessions),
        "rate_limits": newest_rate_limits,
        "generated_at": int(now.timestamp()),
    }


def render_default(data: dict[str, Any], width: int, p: Palette) -> str:
    usage = data["usage"]
    activity = data["activity"]
    rate_limits = usage.get("rate_limits") or {}
    context = format_tokens(data["context_window"]) if data["context_window"] else "unknown"
    model_bits = [data["model_display"]]
    if data["reasoning_effort"]:
        model_bits.append(data["reasoning_effort"])
    model_bits.append(f"({context} context)")

    lines = [
        "─" * min(width, 120),
        f"        model   {' '.join(model_bits)}",
        f"        time    {format_duration(data['session_age_seconds'])} active · idle {format_duration(data['idle_seconds'])}",
        f"        account {data['account']}",
        f"        repo    {data['repo']} ({data['branch']})",
    ]

    if usage["context_window"] > 0:
        lines.append(render_goal_row("context", usage["context_used"], usage["context_window"], DEFAULT_BAR_WIDTH, p))

    for label, limit in labeled_rate_limits(rate_limits):
        lines.append(render_rate_limit_row(label, limit, DEFAULT_BAR_WIDTH, p))

    lines.extend(
        [
            f"        usage   session {format_tokens(usage['session_total'])} · turn {format_tokens(usage['turn_total'])} · cached {format_tokens(usage['turn_cached'])}",
            f"        output  total {format_tokens(usage['session_output'])} · reasoning {format_tokens(usage['session_reasoning'])}",
            f"        state   {data['sandbox']} · approvals {data['approval_mode']} · plan {rate_limits.get('plan_type', '-')}",
            f"        turns   {activity['turns_completed']}/{activity['turns_started']} done · aborted {activity['turns_aborted']} · compacted {activity['compactions']}",
            f"        tools   {activity['tool_calls']} calls · shell {activity['shell_calls']} · patch {activity['patch_calls']} · active {activity['active_tools']} ({activity['active_shells']} shells)",
            f"        last    {activity['last_event']} · {activity['last_command']}",
            f"        user    {activity['last_user_message']}",
        ]
    )
    if activity["active_turn_seconds"] > 0:
        lines.append(f"        active  current turn running {format_duration(activity['active_turn_seconds'])}")
    return "\n".join(lines)


def render_sigil(data: dict[str, Any], p: Palette) -> str:
    usage = data["usage"]
    activity = data["activity"]
    rate_limits = usage.get("rate_limits") or {}
    limits = labeled_rate_limits(rate_limits)
    limit_label, limit = limits[0] if limits else ("5-hour", {})
    short_limit_label = {"5-hour": "5h", "weekly": "wk"}.get(limit_label, limit_label)
    pct = float(limit.get("used_percent") or 0.0)
    bar = build_pct_bar(pct, 10, p)
    effort = f".{data['reasoning_effort']}" if data["reasoning_effort"] else ""
    context = "-"
    if usage["context_window"] > 0:
        context = f"{format_tokens(usage['context_used'])}/{format_tokens(usage['context_window'])}"
    return (
        f"◈ {data['model_display']}{effort} · {data['repo']} ({data['branch']}) · "
        f"{bar} {format_pct(pct)} {short_limit_label} · context {context} · session {format_tokens(usage['session_total'])} · "
        f"tools {activity['active_tools']} · ⏱ {format_duration(data['session_age_seconds'])}"
    )


def render_footer(data: dict[str, Any], width: int, p: Palette) -> str:
    usage = data["usage"]
    activity = data["activity"]
    agents = data.get("agents") or {}
    tokens = data["tokens"]
    rate_limits = usage.get("rate_limits") or {}

    def row(label: str, value: str) -> str:
        prefix = f"  {label:<7} "
        if width <= len(prefix):
            return short_text(f"{label} {value}", max(1, width))
        return prefix + short_text(value, max(1, width - len(prefix)))

    effort = f".{data['reasoning_effort']}" if data["reasoning_effort"] else ""
    lines = [
        row("model", f"{data['model_display']}{effort}"),
        row("time", format_duration(data["session_age_seconds"])),
        row("account", data["account"]),
        row("repo", f"{data['repo']} ({data['branch']})"),
    ]
    if usage["context_window"] > 0:
        if width >= 64:
            lines.append(render_goal_row("context", usage["context_used"], usage["context_window"], DEFAULT_BAR_WIDTH, p, 2))
        else:
            context_pct = usage["context_used"] * 100.0 / usage["context_window"]
            lines.append(
                row(
                    "context",
                    f"{format_pct(context_pct)} {format_tokens(usage['context_used'])}/{format_tokens(usage['context_window'])}",
                )
            )
    for label, limit in labeled_rate_limits(rate_limits):
        if width >= 64:
            lines.append(render_rate_limit_row(label, limit, DEFAULT_BAR_WIDTH, p, 2))
        else:
            lines.append(row(label, f"{format_pct(float(limit.get('used_percent') or 0.0))} {format_reset(limit.get('resets_at'))}"))

    lines.extend(
        [
            row(
                "usage",
                f"today {format_tokens(tokens['today'])} · session {format_tokens(usage['session_total'])} · lifetime {format_tokens(tokens['lifetime'])}",
            ),
            row(
                "agents",
                f"{int(agents.get('active', 0))}/{int(agents.get('total', 0))} running · "
                f"tools {activity['active_tools'] + int(agents.get('active_tools', 0))} · "
                f"shells {activity['active_shells'] + int(agents.get('active_shells', 0))}",
            ),
            row(
                "mode",
                f"{data.get('sandbox', '-')} · approvals {data.get('approval_mode', '-')}",
            ),
        ]
    )
    return "\n".join(lines)


def session_marker(session: dict[str, Any]) -> str:
    activity = session["activity"]
    if activity["active_tools"] or activity["active_turn_seconds"]:
        return "*"
    if session["idle_seconds"] < 300:
        return "·"
    return " "


def render_session_row(session: dict[str, Any], p: Palette, details: bool) -> list[str]:
    usage = session["usage"]
    activity = session["activity"]
    context = "-"
    if usage["context_window"] > 0:
        context_pct = usage["context_used"] * 100.0 / usage["context_window"]
        context = f"{build_pct_bar(context_pct, 8, p)} {format_pct(context_pct):>6}"
    effort = f".{session['reasoning_effort']}" if session["reasoning_effort"] else ""
    marker = session_marker(session)
    repo = short_text(session["repo"], 16)
    branch = short_text(session["branch"], 14)
    model = short_text(f"{session['model_display']}{effort}", 16)
    active = f"active {format_duration(activity['active_turn_seconds'])}" if activity["active_turn_seconds"] else f"idle {format_duration(session['idle_seconds'])}"
    line = (
        f"        {marker} {repo:<16} {branch:<14} {model:<16} "
        f"ctx {context:<17} ses {format_tokens(usage['session_total']):>7} "
        f"turn {format_tokens(usage['turn_total']):>7} "
        f"tool {activity['active_tools']}/{activity['active_shells']} · {active}"
    )
    if not details:
        return [line]
    return [
        line,
        f"          ask  {activity['last_user_message']}",
        f"          last {activity['last_event']} · {activity['last_command']}",
    ]


def render_all_sessions(data: dict[str, Any], width: int, p: Palette, details: bool = False) -> str:
    sessions = data["sessions"]
    rate_limits = data.get("rate_limits") or {}
    total_tokens = sum(session["usage"]["session_total"] for session in sessions)
    active_tools = sum(session["activity"]["active_tools"] for session in sessions)
    active_shells = sum(session["activity"]["active_shells"] for session in sessions)
    active_turns = sum(1 for session in sessions if session["activity"]["active_turn_seconds"] > 0)

    lines = [
        "─" * min(width, 120),
        f"        account  {data['account']}",
        f"        sessions {len(sessions)} shown · active {active_turns} · tools {active_tools} · shells {active_shells} · tokens {format_tokens(total_tokens)}",
    ]
    for label, limit in labeled_rate_limits(rate_limits):
        lines.append(render_rate_limit_row(label, limit, DEFAULT_BAR_WIDTH, p))
    lines.append("        ·")
    lines.append("        # repo             branch         model            context           session    turn    tools state")

    for session in sessions:
        lines.extend(render_session_row(session, p, details))
    return "\n".join(lines)


def top_status(activity: dict[str, Any], idle_seconds: int) -> str:
    if activity["active_tool"] in {"request_user_input", "mcp_elicitation"}:
        return "WAIT"
    if activity["active_shells"]:
        return "SHELL"
    if activity["active_tools"]:
        return "TOOL"
    if activity["active_turn_seconds"]:
        return "RUN"
    closed_turns = activity["turns_completed"] + activity["turns_aborted"]
    if activity["turns_started"] and closed_turns >= activity["turns_started"]:
        return "DONE"
    if idle_seconds < 300:
        return "IDLE"
    return "DONE"


def filter_top_sessions(
    sessions: list[dict[str, Any]],
    *,
    active_only: bool,
    hide_inactive: bool,
) -> list[dict[str, Any]]:
    active_statuses = {"WAIT", "SHELL", "TOOL", "RUN", "IDLE"}
    if active_only:
        return [
            session
            for session in sessions
            if top_status(session["activity"], session["idle_seconds"]) in active_statuses
        ]
    if hide_inactive:
        return [
            session
            for session in sessions
            if (
                top_status(session["activity"], session["idle_seconds"]) in active_statuses
                or session["is_subagent"]
            )
        ]
    return sessions


def top_sort_key(session: dict[str, Any], sort: str) -> tuple[Any, ...]:
    usage = session["usage"]
    activity = session["activity"]
    status = top_status(activity, session["idle_seconds"])
    status_rank = {"WAIT": 0, "SHELL": 1, "TOOL": 2, "RUN": 3, "IDLE": 4, "DONE": 5}.get(status, 9)
    context_pct = 0.0
    if usage["context_window"] > 0:
        context_pct = usage["context_used"] * 100.0 / usage["context_window"]
    if sort == "context":
        return (-context_pct, status_rank, session["idle_seconds"])
    if sort == "tokens":
        return (-usage["session_total"], status_rank, session["idle_seconds"])
    if sort == "idle":
        return (session["idle_seconds"], status_rank)
    return (status_rank, session["idle_seconds"], -usage["turn_total"])


def render_top(data: dict[str, Any], args: argparse.Namespace, p: Palette) -> str:
    width = args.width
    sessions = sorted(data["sessions"], key=lambda session: top_sort_key(session, args.sort))
    sessions = filter_top_sessions(
        sessions,
        active_only=args.active_only,
        hide_inactive=args.top and not args.show_inactive,
    )
    total_sessions = len(sessions)
    total_tokens = sum(session["usage"]["session_total"] for session in sessions)
    active_turns = sum(1 for session in sessions if session["activity"]["active_turn_seconds"] > 0)
    active_tools = sum(session["activity"]["active_tools"] for session in sessions)
    active_shells = sum(session["activity"]["active_shells"] for session in sessions)
    max_rows = args.rows
    if max_rows <= 0:
        max_rows = max(4, terminal_size().lines - 8)
    hidden = max(0, len(sessions) - max_rows)
    sessions = sessions[:max_rows]
    rate_limits = data.get("rate_limits") or {}
    limits = labeled_rate_limits(rate_limits)

    now_text = datetime.now().astimezone().strftime("%H:%M:%S %Z")
    wide = width >= 132
    medium = width >= 78
    rate_width = 24 if width >= 72 else 10

    if width >= 100:
        summary = (
            f"{p.cyan}codex-top{p.reset} {now_text}  account {data['account']}  "
            f"sessions {len(sessions)}/{total_sessions}  active {active_turns}  "
            f"tools {active_tools}  shells {active_shells}  tokens {format_tokens(total_tokens)}"
        )
    else:
        summary = (
            f"{p.cyan}codex-top{p.reset} {now_text}  sessions {len(sessions)}/{total_sessions}  "
            f"active {active_turns}  tokens {format_tokens(total_tokens)}"
        )

    if wide:
        columns = f"{'ST':<5} {'AGE':>6} {'IDLE':>6} {'CTX':<15} {'SESSION':>8} {'TURN':>7} {'OUT':>6} {'RSN':>6} {'ACTION':<12} {'AGENT':<18} {'REPO':<14} {'MODEL':<14}"
    elif medium:
        columns = f"{'ST':<5} {'CTX':<15} {'SESSION':>8} {'ACTION':<12} {'AGENT':<18} {'REPO':<14}"
    else:
        columns = f"{'ST':<5} {'AGENT':<14} {'CTX':>6} {'SESSION':>8} {'ACTION':<10}"

    lines = [summary]
    for label, limit in limits:
        short_label = {"5-hour": "5h", "weekly": "wk"}.get(label, label)
        used_percent = float(limit.get("used_percent") or 0.0)
        lines.append(
            f"{short_label} [{build_pct_bar(used_percent, rate_width, p)}] "
            f"{format_pct(used_percent):>6} {format_reset(limit.get('resets_at'))}"
        )
    lines.extend(["─" * min(width, 140), columns])

    for session in sessions:
        usage = session["usage"]
        activity = session["activity"]
        ctx_pct = 0.0
        if usage["context_window"] > 0:
            ctx_pct = usage["context_used"] * 100.0 / usage["context_window"]
        status = top_status(activity, session["idle_seconds"])
        status_color = p.green
        if status in {"SHELL", "TOOL", "RUN"}:
            status_color = p.orange
        elif status == "WAIT":
            status_color = p.yellow
        elif status == "DONE":
            status_color = p.dim
        model = short_text(f"{session['model_display']}.{session['reasoning_effort']}" if session["reasoning_effort"] else session["model_display"], 14)
        repo = short_text(session["repo"], 14)
        action = short_text(activity["active_tool"] if activity["active_tool"] != "-" else activity["last_tool"], 12)
        agent = short_text(session["agent"], 18)
        ctx_bar = build_pct_bar(ctx_pct, 8, p)
        ctx = f"{ctx_bar} {format_pct(ctx_pct):>6}"
        if wide:
            line = (
                f"{status_color}{status:<5}{p.reset} "
                f"{format_duration(session['session_age_seconds']):>6} "
                f"{format_duration(session['idle_seconds']):>6} "
                f"{ctx:<15} "
                f"{format_tokens(usage['session_total']):>8} "
                f"{format_tokens(usage['turn_total']):>7} "
                f"{format_tokens(usage['session_output']):>6} "
                f"{format_tokens(usage['session_reasoning']):>6} "
                f"{action:<12} "
                f"{agent:<18} "
                f"{repo:<14} "
                f"{model:<14}"
            )
        elif medium:
            line = (
                f"{status_color}{status:<5}{p.reset} "
                f"{ctx:<15} "
                f"{format_tokens(usage['session_total']):>8} "
                f"{action:<12} "
                f"{agent:<18} "
                f"{repo:<14}"
            )
        else:
            line = (
                f"{status_color}{status:<5}{p.reset} "
                f"{short_text(agent, 14):<14} "
                f"{format_pct(ctx_pct):>6} "
                f"{format_tokens(usage['session_total']):>8} "
                f"{short_text(action, 10):<10}"
            )
        lines.append(line)

    if hidden:
        lines.append(f"{p.dim}… {hidden} more sessions hidden; use --rows or --sessions to show more{p.reset}")
    filters = []
    if args.top and not args.show_inactive:
        filters.append("active")
    if args.top and not args.include_agents:
        filters.append("parents")
    filter_text = ",".join(filters) if filters else "none"
    lines.append(f"{p.dim}sort={args.sort} rows={max_rows} filters={filter_text} interval={args.watch or 0:g}s · Ctrl-C to stop live mode{p.reset}")
    return "\n".join(lines)


def render(data: dict[str, Any], args: argparse.Namespace, p: Palette) -> str:
    if args.json:
        return json.dumps(data, indent=2, sort_keys=True)
    if args.top:
        return render_top(data, args, p)
    if args.all:
        return render_all_sessions(data, args.width, p, args.details)
    if args.footer:
        return render_footer(data, args.width, p)
    fmt = args.format
    if fmt == "sigil":
        return render_sigil(data, p)
    return render_default(data, args.width, p)


def watch(args: argparse.Namespace, p: Palette) -> int:
    if args.footer:
        print("\033[?1049h", end="")
        sys.stdout.flush()
    try:
        return watch_loop(args, p)
    finally:
        if args.footer:
            print("\033[?1049l", end="")
            sys.stdout.flush()


def watch_loop(args: argparse.Namespace, p: Palette) -> int:
    wal_attempt = 0.0
    while True:
        latest_activity_ms = 0
        try:
            if args.owner_pid_file and not owner_alive(args.owner_pid_file):
                return 0
            if args.dynamic_width:
                args.width = terminal_size().columns
            data = all_sessions_snapshot(args) if args.all or args.top else snapshot(args)
            if args.all or args.top:
                latest_activity_ms = max(
                    (int(s.get("updated_at") or 0) for s in data.get("sessions") or []),
                    default=0,
                )
            else:
                latest_activity_ms = int(data.get("updated_at") or 0)
            if (
                (args.bind_after or args.bind_after_ms or args.bind_updated_after_ms)
                and not args.owner_pid_file
                and not args.thread_id
                and not args.all
                and not args.top
            ):
                args.thread_id = data["thread_id"]
            body = render(data, args, p)
            timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z")
            print("\033[2J\033[H", end="")
            print(body)
            if not args.footer:
                print(f"\n        refresh {timestamp} · Ctrl-C to stop")
            sys.stdout.flush()
        except KeyboardInterrupt:
            print()
            return 0
        except Exception as exc:
            print("\033[2J\033[H", end="")
            binding = args.owner_pid_file or args.bind_after or args.bind_after_ms or args.bind_updated_after_ms
            if args.footer and binding and str(exc) == "no Codex threads found":
                print("  status  Waiting for this Codex session…")
            else:
                print(f"Codex status unavailable: {exc}")
            sys.stdout.flush()
        wal_attempt = maybe_checkpoint_wal(
            STATE_DB, last_attempt=wal_attempt, now=time.monotonic()
        )
        try:
            time.sleep(
                next_sleep_seconds(
                    args.watch, latest_activity_ms, int(time.time() * 1000)
                )
            )
        except KeyboardInterrupt:
            print()
            return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", default="")
    config_args, _ = config_parser.parse_known_args(argv)
    config_path = Path(config_args.config) if config_args.config else CONFIG_FILE
    config = parse_shell_config(config_path)
    format_default = os.environ.get("CODEX_STATUSLINE_FORMAT") or config.get(
        "CODEX_STATUSLINE_FORMAT", "default"
    )

    parser = argparse.ArgumentParser(description="Render a Codex CLI statusline from local state.")
    parser.add_argument("--format", choices=("default", "sigil"), default=format_default)
    parser.add_argument("--json", action="store_true", help="print the raw computed snapshot")
    parser.add_argument("--thread-id", default="", help="Codex thread id to render; defaults to nearest recent thread")
    parser.add_argument("--bind-after", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--bind-after-ms", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--bind-updated-after-ms", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--owner-pid-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--cwd", default="", help="cwd used to choose the nearest recent Codex thread")
    parser.add_argument("--config", default="", help="config file path; defaults to ~/.codex/statusline.conf")
    parser.add_argument("--width", type=int, default=default_width())
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS", help="refresh in place every N seconds")
    parser.add_argument("--all", action="store_true", help="show a single dashboard for all recent Codex sessions")
    parser.add_argument("--top", action="store_true", help="show a btop/nvitop-style all-session monitor")
    parser.add_argument("--footer", action="store_true", help="show the compact dashboard used by the codex-statusline launcher")
    parser.add_argument("--sessions", type=int, default=30, help="number of sessions to load with --all/--top")
    parser.add_argument("--include-archived", action="store_true", help="include archived sessions in --all")
    parser.add_argument("--details", action="store_true", help="include last prompt and command under each session in --all")
    parser.add_argument("--active-window", type=int, default=900, help="seconds before an unclosed turn is treated as stale")
    parser.add_argument("--rows", type=int, default=0, help="visible row count for --top; defaults to terminal height")
    parser.add_argument("--active-only", action="store_true", help="hide completed/stale sessions in --top")
    parser.add_argument("--show-inactive", action="store_true", help="show completed/stale sessions in --top")
    parser.add_argument("--include-agents", action="store_true", help="include subagent sessions in --top")
    parser.add_argument("--sort", choices=("active", "context", "tokens", "idle"), default="active", help="sort mode for --top")
    args = parser.parse_args(argv)
    if (
        not args.thread_id
        and not args.owner_pid_file
        and not args.bind_after
        and not args.bind_after_ms
        and not args.bind_updated_after_ms
    ):
        args.thread_id = os.environ.get("CODEX_THREAD_ID") or config.get("CODEX_THREAD_ID", "")
    args.dynamic_width = not any(arg == "--width" or arg.startswith("--width=") for arg in argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    color_enabled = not args.no_color and not os.environ.get("NO_COLOR")
    if args.watch > 0:
        return watch(args, Palette(color_enabled))
    try:
        data = all_sessions_snapshot(args) if args.all or args.top else snapshot(args)
        print(render(data, args, Palette(color_enabled)))
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2))
        else:
            print(f"Codex status unavailable: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
