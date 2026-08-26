#!/usr/bin/env python3
"""Local-only metrics ingestion and dashboard for Claude Code and Codex."""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import fnmatch
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import mimetypes
import os
from pathlib import Path
import secrets
import shlex
import sqlite3
import sys
import time
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse
import webbrowser

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None


SCHEMA_VERSION = 5
ATTRIBUTION_VERSION = 3
IDENTIFIER_VERSION = 1
TOKEN_COLUMNS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_create_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)
DEFAULT_PORT = 8765
DEFAULT_WATCH_INTERVAL = 60
DEFAULT_WATCH_MAX_LINES = 5000
SOURCE_LINE_QUANTUM = 1000
MAX_TIMELINE_POINTS = 2000
MAX_FILTER_VALUES = 500
MAX_ANALYSIS_ROWS = 10_000
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "share" / "agent-metrics"
CONFIG_EXAMPLE = REPO_ROOT / "config" / "agent-metrics.toml.example"


def default_data_dir() -> Path:
    override = os.environ.get("AGENT_METRICS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/statusline/agent-metrics"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local/share"
    return base / "statusline/agent-metrics"


def default_paths() -> dict[str, str]:
    return {
        "claude_projects": str(Path.home() / ".claude/projects"),
        "claude_account_spans": str(Path.home() / ".claude/session-accounts.json"),
        "codex_sessions": str(Path.home() / ".codex/sessions"),
        "codex_auth": str(Path.home() / ".codex/auth.json"),
        "claude_statusline_config": str(Path.home() / ".claude/statusline.conf"),
        "claude_utilization_history": str(Path.home() / ".claude/utilization-history.jsonl"),
        "shared_account_snapshot": str(Path.home() / ".accounts/statusline-snapshot.json"),
    }


@dataclass(frozen=True)
class Config:
    data_dir: Path
    database: Path
    claude_projects: Path
    claude_account_spans: Path
    codex_sessions: Path
    codex_auth: Path
    account_aliases: dict[str, str]
    pricing: dict[str, Any]
    retention_days: int
    bind: str
    port: int
    claude_statusline_config: Path = Path.home() / ".claude/statusline.conf"
    reuse_statusline_account_labels: bool = True
    claude_utilization_history: Path = Path.home() / ".claude/utilization-history.jsonl"
    shared_account_snapshot: Path = Path.home() / ".accounts/statusline-snapshot.json"
    account_tiers: dict[str, str] = field(default_factory=dict)


def load_config(data_dir: Path, config_path: Path | None = None) -> Config:
    path = config_path or data_dir / "config.toml"
    raw: dict[str, Any] = {}
    if path.is_file():
        if tomllib is None:
            raise RuntimeError("Agent Metrics configuration requires Python 3.11+")
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    paths = {**default_paths(), **dict(raw.get("paths") or {})}
    storage = dict(raw.get("storage") or {})
    ui = dict(raw.get("ui") or {})
    accounts = dict(raw.get("accounts") or {})
    database = Path(storage.get("database") or data_dir / "metrics.sqlite3").expanduser()
    return Config(
        data_dir=data_dir,
        database=database,
        claude_projects=Path(paths["claude_projects"]).expanduser(),
        claude_account_spans=Path(paths["claude_account_spans"]).expanduser(),
        codex_sessions=Path(paths["codex_sessions"]).expanduser(),
        codex_auth=Path(paths["codex_auth"]).expanduser(),
        account_aliases={str(k): str(v) for k, v in (raw.get("account_aliases") or {}).items()},
        pricing=dict(raw.get("pricing") or {}),
        retention_days=max(1, int(storage.get("retention_days", 365))),
        bind=str(ui.get("bind", "127.0.0.1")),
        port=int(ui.get("port", DEFAULT_PORT)),
        claude_statusline_config=Path(paths["claude_statusline_config"]).expanduser(),
        reuse_statusline_account_labels=bool(
            accounts.get("reuse_claude_statusline_labels", True)
        ),
        claude_utilization_history=Path(paths["claude_utilization_history"]).expanduser(),
        shared_account_snapshot=Path(paths["shared_account_snapshot"]).expanduser(),
        account_tiers={
            str(label): str(tier)
            for label, tier in (raw.get("account_tiers") or {}).items()
            if str(tier) in {"5x", "20x"}
        },
    )


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    stat = path.stat()
    if stat.st_uid != os.getuid():
        raise PermissionError(f"Agent Metrics directory is not owned by the current user: {path}")
    path.chmod(0o700)
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"Agent Metrics directory is not private: {path}")


def validate_private_dir(path: Path) -> None:
    stat = path.stat()
    if not path.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise PermissionError(f"Agent Metrics directory must be user-owned and mode 0700: {path}")


def secure_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if not candidate.exists():
            continue
        stat = candidate.stat()
        if stat.st_uid != os.getuid():
            raise PermissionError(f"Agent Metrics database file is not user-owned: {candidate}")
        candidate.chmod(0o600)
        if candidate.stat().st_mode & 0o077:
            raise PermissionError(f"Agent Metrics database file is not private: {candidate}")


def validate_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if not candidate.exists():
            continue
        stat = candidate.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise PermissionError(
                f"Agent Metrics database file must be user-owned and mode 0600: {candidate}"
            )


def load_or_create_salt(data_dir: Path) -> bytes:
    ensure_private_dir(data_dir)
    path = data_dir / "identity.salt"
    try:
        value = path.read_bytes()
    except OSError:
        value = secrets.token_bytes(32)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
    path.chmod(0o600)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise PermissionError(f"Agent Metrics identity salt is not private: {path}")
    if len(value) < 16:
        raise RuntimeError(f"invalid Agent Metrics identity salt: {path}")
    return value


def load_or_create_ui_token(data_dir: Path) -> str:
    ensure_private_dir(data_dir)
    path = data_dir / "ui.token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = secrets.token_urlsafe(32)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            token = path.read_text(encoding="utf-8").strip()
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
    path.chmod(0o600)
    if not token or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise PermissionError(f"Agent Metrics UI token is not private: {path}")
    return token


def keyed_hash(salt: bytes, namespace: str, value: str) -> str:
    digest = hashlib.blake2b(key=salt, digest_size=20)
    digest.update(namespace.encode("utf-8"))
    digest.update(b"\0")
    digest.update(value.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def safe_identifier(value: Any, salt: bytes, namespace: str) -> str:
    text = str(value or "")
    return keyed_hash(salt, namespace, text) if text else ""


@contextmanager
def ingestion_lock(data_dir: Path):
    ensure_private_dir(data_dir)
    path = data_dir / "ingest.lock"
    handle = path.open("a+")
    path.chmod(0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Agent Metrics collector is already running") from exc
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_ms(value: Any) -> int | None:
    parsed = parse_timestamp(value)
    return int(parsed.timestamp() * 1000) if parsed else None


def number(value: Any, *, integer: bool = True) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value) if integer else float(value)


def token_value(mapping: dict[str, Any], *names: str) -> int:
    for name in names:
        value = number(mapping.get(name))
        if value is not None:
            return int(value)
    return 0


def minute_epoch(timestamp: int) -> int:
    return timestamp - timestamp % 60_000


def validate_loopback(host: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"Agent Metrics bind must be a loopback IP, got {host!r}") from exc
    if address.version != 4 or not address.is_loopback:
        raise ValueError(f"Agent Metrics bind must be an IPv4 loopback address, got {host!r}")


def connect_database(path: Path) -> sqlite3.Connection:
    ensure_private_dir(path.parent)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma busy_timeout = 5000")
    connection.execute("pragma journal_mode = wal")
    connection.execute("pragma synchronous = normal")
    connection.execute("pragma wal_autocheckpoint = 1000")
    connection.execute("pragma foreign_keys = on")
    create_schema(connection, load_or_create_salt(path.parent))
    secure_sqlite_files(path)
    return connection


def connect_database_readonly(path: Path) -> sqlite3.Connection:
    validate_private_dir(path.parent)
    if not path.is_file():
        raise FileNotFoundError(path)
    validate_sqlite_files(path)
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma query_only = on")
    connection.execute("pragma busy_timeout = 5000")
    return connection


def rehash_stored_identifiers(connection: sqlite3.Connection, salt: bytes) -> None:
    for namespace in ("session", "agent", "turn", "request", "call"):
        connection.create_function(
            f"agent_hash_{namespace}",
            1,
            lambda value, kind=namespace: safe_identifier(value, salt, kind),
        )
    connection.executescript(
        """
        update events set
            session_id=agent_hash_session(session_id),
            parent_session_id=agent_hash_session(parent_session_id),
            agent_id=agent_hash_agent(agent_id),
            turn_id=agent_hash_turn(turn_id),
            request_id=agent_hash_request(request_id),
            call_id=agent_hash_call(call_id);
        update parser_state set
            session_id=agent_hash_session(session_id),
            parent_session_id=agent_hash_session(parent_session_id),
            agent_id=agent_hash_agent(agent_id),
            current_turn_id=agent_hash_turn(current_turn_id);
        update open_turns set turn_id=agent_hash_turn(turn_id);
        update open_tools set call_id=agent_hash_call(call_id);
        """
    )
    rebuild_minutes(connection)


def create_schema(connection: sqlite3.Connection, salt: bytes) -> None:
    rebuild_minute_metrics = False
    connection.executescript(
        """
        create table if not exists metadata (
            key text primary key,
            value text not null
        );
        create table if not exists sources (
            source_id text primary key,
            provider text not null,
            account_id text not null default '',
            inode integer not null,
            size integer not null,
            mtime_ns integer not null,
            offset integer not null,
            updated_at integer not null
        );
        create table if not exists parser_state (
            source_id text primary key,
            session_id text not null default '',
            parent_session_id text not null default '',
            agent_id text not null default '',
            model text not null default '',
            reasoning_effort text not null default '',
            current_turn_id text not null default '',
            latest_user_at integer
        );
        create table if not exists open_turns (
            source_id text not null,
            turn_id text not null,
            started_at integer not null,
            primary key(source_id, turn_id)
        );
        create table if not exists open_tools (
            source_id text not null,
            call_id text not null,
            started_at integer not null,
            tool_name text not null default '',
            primary key(source_id, call_id)
        );
        create table if not exists accounts (
            account_id text primary key,
            provider text not null,
            org_id_hash text not null default '',
            label text not null default ''
        );
        create table if not exists events (
            event_key text primary key,
            provider text not null,
            source_id text not null,
            timestamp integer not null,
            minute integer not null,
            event_kind text not null,
            account_id text not null default '',
            session_id text not null default '',
            parent_session_id text not null default '',
            agent_id text not null default '',
            turn_id text not null default '',
            request_id text not null default '',
            call_id text not null default '',
            model text not null default '',
            reasoning_effort text not null default '',
            input_tokens integer not null default 0,
            cached_input_tokens integer not null default 0,
            cache_create_tokens integer not null default 0,
            output_tokens integer not null default 0,
            reasoning_tokens integer not null default 0,
            total_tokens integer not null default 0,
            context_window integer,
            context_used integer,
            compaction_kind text not null default '',
            tool_name text not null default '',
            tool_status text not null default '',
            tool_duration_ms integer,
            turn_latency_ms integer,
            quota_window_minutes integer,
            quota_used_percent real,
            quota_resets_at integer,
            cost_usd real
        );
        create index if not exists events_time on events(timestamp);
        create index if not exists events_source on events(source_id);
        create index if not exists events_dimensions
            on events(provider, account_id, model, reasoning_effort, session_id);
        create table if not exists minute_metrics (
            source_id text not null,
            minute integer not null,
            provider text not null,
            account_id text not null,
            model text not null,
            reasoning_effort text not null,
            session_id text not null,
            agent_id text not null,
            input_tokens integer not null,
            cached_input_tokens integer not null,
            cache_create_tokens integer not null,
            output_tokens integer not null,
            reasoning_tokens integer not null,
            total_tokens integer not null,
            cost_usd real,
            primary key (
                source_id, minute, provider, account_id, model, reasoning_effort, session_id, agent_id
            )
        );
        create table if not exists quota_observations (
            account_id text not null,
            account_label text not null,
            plan_cohort text not null default '',
            observed_minute integer not null,
            quota_name text not null,
            window_minutes integer not null,
            used_percent real not null,
            resets_at integer,
            stale integer not null,
            pending_reset integer not null,
            source_kind text not null,
            primary key(account_id, quota_name, observed_minute)
        );
        create index if not exists quota_observations_window
            on quota_observations(account_id, quota_name, resets_at, observed_minute);
        create index if not exists minute_metrics_provider_time
            on minute_metrics(provider, minute);
        """
    )
    minute_columns = {
        row[1] for row in connection.execute("pragma table_info(minute_metrics)")
    }
    if "source_id" not in minute_columns:
        connection.execute("drop table minute_metrics")
        connection.execute(
            """
            create table minute_metrics (
                source_id text not null,
                minute integer not null,
                provider text not null,
                account_id text not null,
                model text not null,
                reasoning_effort text not null,
                session_id text not null,
                agent_id text not null,
                input_tokens integer not null,
                cached_input_tokens integer not null,
                cache_create_tokens integer not null,
                output_tokens integer not null,
                reasoning_tokens integer not null,
                total_tokens integer not null,
                cost_usd real,
                primary key (
                    source_id, minute, provider, account_id, model,
                    reasoning_effort, session_id, agent_id
                )
            )
            """
        )
        rebuild_minute_metrics = True
    source_columns = {row[1] for row in connection.execute("pragma table_info(sources)")}
    if "account_id" not in source_columns:
        connection.execute("alter table sources add column account_id text not null default ''")
    event_columns = {row[1] for row in connection.execute("pragma table_info(events)")}
    for column, definition in (
        ("quota_window_minutes", "integer"),
        ("quota_used_percent", "real"),
        ("quota_resets_at", "integer"),
    ):
        if column not in event_columns:
            connection.execute(f"alter table events add column {column} {definition}")
    identifier_row = connection.execute(
        "select value from metadata where key='identifier_version'"
    ).fetchone()
    identifier_changed = identifier_row is None or identifier_row[0] != str(IDENTIFIER_VERSION)
    if identifier_changed and connection.execute(
        "select exists(select 1 from events)"
    ).fetchone()[0]:
        rehash_stored_identifiers(connection, salt)
        rebuild_minute_metrics = False
    if rebuild_minute_metrics:
        rebuild_minutes(connection)
    connection.execute(
        "insert into metadata(key, value) values('identifier_version', ?) "
        "on conflict(key) do update set value=excluded.value where value!=excluded.value",
        (str(IDENTIFIER_VERSION),),
    )
    connection.execute(
        "insert into metadata(key, value) values('schema_version', ?) "
        "on conflict(key) do update set value=excluded.value where value!=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()


@dataclass
class AccountSpan:
    start_ms: int | None
    end_ms: int | None
    account_id: str
    org_id_hash: str
    label: str


@dataclass(frozen=True)
class AccountLabelRule:
    label: str
    identity_pattern: str
    org_id: str | None


def load_statusline_account_labels(path: Path) -> list[AccountLabelRule]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    raw_value = ""
    for raw_line in lines:
        assignment = raw_line.strip()
        if assignment.startswith("export "):
            assignment = assignment[7:].lstrip()
        if not assignment.startswith("ACCOUNT_LABELS="):
            continue
        try:
            raw_value = " ".join(shlex.split(assignment.split("=", 1)[1], comments=True))
        except ValueError:
            return []
    rules = []
    label_chars = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    for pair in raw_value.split():
        if ":" not in pair:
            continue
        label, pattern = pair.split(":", 1)
        if not label or len(label) > 32 or any(char not in label_chars for char in label):
            continue
        identity_pattern, separator, org_id = pattern.partition("|")
        if identity_pattern:
            rules.append(
                AccountLabelRule(
                    label=label,
                    identity_pattern=identity_pattern.lower(),
                    org_id=org_id if separator else None,
                )
            )
    return rules


def statusline_account_alias(
    identity: str, org_id: str, rules: Iterable[AccountLabelRule]
) -> str:
    bare = ""
    for rule in rules:
        if not fnmatch.fnmatchcase(identity.lower(), rule.identity_pattern):
            continue
        if rule.org_id is not None:
            if org_id == rule.org_id:
                return rule.label
        elif not bare:
            bare = rule.label
    return bare


def account_alias(
    config: Config,
    provider: str,
    identity: str,
    org_id: str = "",
    statusline_rules: Iterable[AccountLabelRule] = (),
) -> str:
    canonical = f"{provider}:{identity}|{org_id}"
    explicit = config.account_aliases.get(
        canonical, config.account_aliases.get(f"{provider}:{identity}", "")
    )
    if explicit or provider != "claude":
        return explicit
    return statusline_account_alias(identity, org_id, statusline_rules)


def hash_account(salt: bytes, provider: str, identity: str, org_id: str = "") -> str:
    canonical = f"{identity.strip().lower()}\0{org_id.strip()}"
    return keyed_hash(salt, f"account:{provider}", canonical)


def load_claude_account_spans(
    path: Path,
    config: Config,
    salt: bytes,
    connection: sqlite3.Connection,
    statusline_rules: Iterable[AccountLabelRule] = (),
) -> dict[str, list[AccountSpan]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    result: dict[str, list[AccountSpan]] = {}
    if not isinstance(raw, dict):
        return result
    for raw_session, details in raw.items():
        if not isinstance(details, dict):
            continue
        session_id = safe_identifier(raw_session, salt, "session")
        spans: list[AccountSpan] = []
        for item in details.get("spans") or []:
            if not isinstance(item, dict):
                continue
            identity = str(item.get("email") or item.get("account_id") or "").strip().lower()
            org_id = str(
                item.get("org_uuid")
                or item.get("organization_id")
                or item.get("orgId")
                or ""
            ).strip()
            if not identity:
                continue
            account_id = hash_account(salt, "claude", identity, org_id)
            org_hash = keyed_hash(salt, "claude-org", org_id) if org_id else ""
            label = account_alias(config, "claude", identity, org_id, statusline_rules)
            connection.execute(
                "insert into accounts(account_id, provider, org_id_hash, label) values(?, 'claude', ?, ?) "
                "on conflict(account_id) do update set org_id_hash=excluded.org_id_hash, "
                "label=excluded.label where org_id_hash!=excluded.org_id_hash or label!=excluded.label",
                (account_id, org_hash, label),
            )
            spans.append(
                AccountSpan(
                    timestamp_ms(item.get("from")),
                    timestamp_ms(item.get("to")),
                    account_id,
                    org_hash,
                    label,
                )
            )
        if spans:
            spans.sort(key=lambda span: span.start_ms if span.start_ms is not None else -1)
            result[session_id] = spans
    return result


def account_for_timestamp(spans: Iterable[AccountSpan], at_ms: int) -> str:
    for span in spans:
        if span.start_ms is not None and at_ms < span.start_ms:
            continue
        if span.end_ms is not None and at_ms >= span.end_ms:
            continue
        return span.account_id
    return ""


def account_spans_fingerprint(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        payload = b"<missing>"
    return f"{ATTRIBUTION_VERSION}:{hashlib.sha256(payload).hexdigest()}"


def insert_quota_observation(
    connection: sqlite3.Connection,
    account_id: str,
    label: str,
    tier: str,
    observed_at_ms: int,
    used_percent: float,
    resets_at_ms: int | None,
    stale: bool,
    pending_reset: bool,
    source_kind: str,
) -> int:
    before = connection.total_changes
    connection.execute(
        "insert into quota_observations(account_id, account_label, plan_cohort, "
        "observed_minute, quota_name, window_minutes, used_percent, resets_at, stale, "
        "pending_reset, source_kind) values(?, ?, ?, ?, 'five_hour', 300, ?, ?, ?, ?, ?) "
        "on conflict(account_id, quota_name, observed_minute) do update set "
        "account_label=excluded.account_label, plan_cohort=excluded.plan_cohort, "
        "used_percent=excluded.used_percent, resets_at=excluded.resets_at, "
        "stale=excluded.stale, pending_reset=excluded.pending_reset, source_kind=excluded.source_kind "
        "where account_label!=excluded.account_label or plan_cohort!=excluded.plan_cohort or "
        "used_percent!=excluded.used_percent or resets_at is not excluded.resets_at or "
        "stale!=excluded.stale or pending_reset!=excluded.pending_reset or source_kind!=excluded.source_kind",
        (
            account_id,
            label,
            tier,
            minute_epoch(observed_at_ms),
            used_percent,
            resets_at_ms,
            int(stale),
            int(pending_reset),
            source_kind,
        ),
    )
    return connection.total_changes - before


def ingest_shared_quota_snapshot(
    path: Path,
    config: Config,
    connection: sqlite3.Connection,
) -> int:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return 0
    accounts = raw.get("accounts") if isinstance(raw.get("accounts"), dict) else {}
    generated_at = number(raw.get("generated_at"))
    inserted = 0
    for label, account in accounts.items():
        if not isinstance(label, str) or not label or len(label) > 32 or not isinstance(account, dict):
            continue
        matches = connection.execute(
            "select account_id from accounts where provider='claude' and label=?",
            (label,),
        ).fetchall()
        if len(matches) != 1:
            continue
        window = account.get("five_hour") if isinstance(account.get("five_hour"), dict) else {}
        used = number(window.get("used_pct"), integer=False)
        observed = number(window.get("observed_at")) or generated_at
        if used is None or observed is None:
            continue
        inserted += insert_quota_observation(
            connection,
            matches[0]["account_id"],
            label,
            config.account_tiers.get(label, ""),
            int(observed) * 1000,
            float(used),
            timestamp_ms(window.get("resets_at")),
            bool(window.get("stale", True)),
            bool(window.get("pending_reset", False)),
            "shared_snapshot",
        )
    return inserted


def ingest_claude_quota_history(
    path: Path,
    config: Config,
    salt: bytes,
    connection: sqlite3.Connection,
    statusline_rules: Iterable[AccountLabelRule],
) -> int:
    try:
        stat = path.stat()
    except OSError:
        return 0
    previous_inode = metadata_value(connection, "quota_history_inode")
    previous_offset = int(metadata_value(connection, "quota_history_offset") or 0)
    offset = previous_offset if previous_inode == str(stat.st_ino) and previous_offset <= stat.st_size else 0
    inserted = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        while True:
            position = handle.tell()
            raw_line = handle.readline()
            if not raw_line:
                break
            if not raw_line.endswith("\n"):
                handle.seek(position)
                break
            try:
                item = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            identity = str(item.get("email") or "").strip().lower()
            org_id = str(item.get("org_uuid") or "").strip()
            label = account_alias(config, "claude", identity, org_id, statusline_rules)
            observed = number(item.get("ts"))
            used = number(item.get("five_hour_pct"), integer=False)
            if not identity or not label or observed is None or used is None:
                continue
            account_id = hash_account(salt, "claude", identity, org_id)
            org_hash = keyed_hash(salt, "claude-org", org_id) if org_id else ""
            connection.execute(
                "insert into accounts(account_id, provider, org_id_hash, label) values(?, 'claude', ?, ?) "
                "on conflict(account_id) do update set org_id_hash=excluded.org_id_hash, label=excluded.label",
                (account_id, org_hash, label),
            )
            resets_at = timestamp_ms(item.get("five_hour_reset"))
            observed_ms = int(observed) * 1000
            inserted += insert_quota_observation(
                connection,
                account_id,
                label,
                config.account_tiers.get(label, ""),
                observed_ms,
                float(used),
                resets_at,
                False,
                resets_at is not None and resets_at <= observed_ms,
                "utilization_history",
            )
        offset = handle.tell()
    set_metadata_value(connection, "quota_history_inode", str(stat.st_ino))
    set_metadata_value(connection, "quota_history_offset", str(offset))
    return inserted


def ingest_quota_observations(
    config: Config,
    salt: bytes,
    connection: sqlite3.Connection,
    statusline_rules: Iterable[AccountLabelRule],
) -> int:
    changed = ingest_claude_quota_history(
        config.claude_utilization_history,
        config,
        salt,
        connection,
        statusline_rules,
    ) + ingest_shared_quota_snapshot(config.shared_account_snapshot, config, connection)
    before = connection.total_changes
    connection.execute(
        "update quota_observations set account_label=(select label from accounts "
        "where accounts.account_id=quota_observations.account_id) where exists "
        "(select 1 from accounts where accounts.account_id=quota_observations.account_id "
        "and accounts.label!=quota_observations.account_label)"
    )
    labels = {
        row[0]
        for row in connection.execute("select distinct account_label from quota_observations")
    }
    for label in labels:
        tier = config.account_tiers.get(label, "")
        connection.execute(
            "update quota_observations set plan_cohort=? where account_label=? and plan_cohort!=?",
            (tier, label, tier),
        )
    return changed + connection.total_changes - before


def load_codex_account(
    path: Path, config: Config, salt: bytes, connection: sqlite3.Connection
) -> str:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    identity = raw.get("account_id") or tokens.get("account_id")
    if not isinstance(identity, str) or not identity:
        return ""
    account_id = hash_account(salt, "codex", identity)
    label = account_alias(config, "codex", identity)
    connection.execute(
        "insert into accounts(account_id, provider, label) values(?, 'codex', ?) "
        "on conflict(account_id) do update set label=excluded.label where label!=excluded.label",
        (account_id, label),
    )
    return account_id


@dataclass
class ParseState:
    provider: str
    source_id: str
    salt: bytes
    account_id: str = ""
    account_spans_by_session: dict[str, list[AccountSpan]] = field(default_factory=dict)
    session_id: str = ""
    parent_session_id: str = ""
    agent_id: str = ""
    model: str = ""
    reasoning_effort: str = ""
    current_turn_id: str = ""
    turn_started: dict[str, int] = field(default_factory=dict)
    tool_started: dict[str, tuple[int, str]] = field(default_factory=dict)
    latest_user_at: int | None = None


def base_event(state: ParseState, at_ms: int, kind: str) -> dict[str, Any]:
    account_id = state.account_id
    if state.provider == "claude":
        account_id = account_for_timestamp(
            state.account_spans_by_session.get(state.session_id, []), at_ms
        )
    return {
        "provider": state.provider,
        "source_id": state.source_id,
        "timestamp": at_ms,
        "minute": minute_epoch(at_ms),
        "event_kind": kind,
        "account_id": account_id,
        "session_id": state.session_id,
        "parent_session_id": state.parent_session_id,
        "agent_id": state.agent_id,
        "turn_id": state.current_turn_id,
        "model": state.model,
        "reasoning_effort": state.reasoning_effort,
    }


def usage_fields(raw: Any) -> dict[str, Any]:
    usage = raw if isinstance(raw, dict) else {}
    input_tokens = token_value(usage, "input_tokens")
    cached = token_value(usage, "cached_input_tokens", "cache_read_input_tokens")
    cache_create = token_value(usage, "cache_creation_input_tokens", "cache_write_input_tokens")
    output = token_value(usage, "output_tokens")
    reasoning = token_value(usage, "reasoning_output_tokens", "reasoning_tokens")
    total = token_value(usage, "total_tokens")
    if not total:
        total = input_tokens + cached + cache_create + output
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_create_tokens": cache_create,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }


def claude_events(record: dict[str, Any], state: ParseState) -> list[dict[str, Any]]:
    at_ms = timestamp_ms(record.get("timestamp"))
    if at_ms is None:
        return []
    raw_session = record.get("sessionId") or record.get("session_id")
    if raw_session:
        state.session_id = safe_identifier(raw_session, state.salt, "session")
    raw_agent = record.get("agentId") or record.get("agent_id")
    if raw_agent:
        state.agent_id = safe_identifier(raw_agent, state.salt, "agent")
        state.parent_session_id = state.session_id
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    record_type = str(record.get("type") or "")
    results: list[dict[str, Any]] = []
    if record_type in {"user", "human"}:
        state.latest_user_at = at_ms
        turn_id = record.get("uuid") or record.get("requestId")
        state.current_turn_id = safe_identifier(turn_id, state.salt, "turn")
        if state.current_turn_id:
            state.turn_started[state.current_turn_id] = at_ms
        event = base_event(state, at_ms, "turn_start")
        event["turn_id"] = state.current_turn_id
        results.append(event)
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call_id = safe_identifier(block.get("tool_use_id"), state.salt, "call")
                started_at, tool_name = state.tool_started.pop(call_id, (at_ms, ""))
                event = base_event(state, at_ms, "tool")
                event.update(
                    call_id=call_id,
                    tool_name=tool_name,
                    tool_status="error" if block.get("is_error") is True else "ok",
                    tool_duration_ms=max(0, at_ms - started_at),
                )
                results.append(event)
        return results
    subtype = str(record.get("subtype") or "")
    if record_type in {"system", "summary"} and subtype in {
        "compact_boundary",
        "context_compacted",
        "compaction",
    }:
        event = base_event(state, at_ms, "compaction")
        event["compaction_kind"] = subtype
        return [event]
    if record_type != "assistant":
        return []
    model = message.get("model")
    if isinstance(model, str):
        state.model = model[:160]
    effort = (
        record.get("effort")
        or record.get("reasoningEffort")
        or message.get("effort")
        or message.get("reasoning_effort")
    )
    if isinstance(effort, str):
        state.reasoning_effort = effort[:32]
    raw_request_id = record.get("requestId") or message.get("id")
    request_id = (
        safe_identifier(raw_request_id, state.salt, "request")
        if raw_request_id
        else state.current_turn_id
    )
    usage = message.get("usage")
    if isinstance(usage, dict):
        event = base_event(state, at_ms, "tokens")
        event.update(usage_fields(usage))
        event["request_id"] = request_id
        context_window = number(record.get("contextWindow") or message.get("context_window"))
        if context_window is not None:
            event["context_window"] = context_window
            event["context_used"] = (
                event["input_tokens"] + event["cached_input_tokens"] + event["cache_create_tokens"]
            )
        explicit_cost = number(record.get("costUSD") or record.get("cost_usd"), integer=False)
        if explicit_cost is not None:
            event["cost_usd"] = explicit_cost
        if state.latest_user_at is not None:
            event["turn_latency_ms"] = max(0, at_ms - state.latest_user_at)
            state.latest_user_at = None
        state.turn_started.pop(state.current_turn_id, None)
        results.append(event)
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            call_id = safe_identifier(block.get("id"), state.salt, "call")
            tool_name = str(block.get("name") or "")[:160]
            state.tool_started[call_id] = (at_ms, tool_name)
            event = base_event(state, at_ms, "tool")
            event.update(call_id=call_id, tool_name=tool_name, tool_status="started")
            results.append(event)
    return results


def nested_parent_session(source: Any) -> str:
    if not isinstance(source, dict):
        return ""
    subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
    spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
    return str(spawn.get("parent_thread_id") or subagent.get("parent_thread_id") or "")


def codex_events(record: dict[str, Any], state: ParseState) -> list[dict[str, Any]]:
    at_ms = timestamp_ms(record.get("timestamp"))
    if at_ms is None:
        return []
    record_type = str(record.get("type") or "")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "")
    if record_type == "session_meta":
        state.session_id = safe_identifier(payload.get("id"), state.salt, "session")
        state.agent_id = state.session_id
        parent = payload.get("parent_thread_id") or nested_parent_session(payload.get("source"))
        state.parent_session_id = safe_identifier(parent, state.salt, "session")
        event = base_event(state, at_ms, "session")
        return [event]
    if record_type == "turn_context":
        model = payload.get("model")
        if isinstance(model, str):
            state.model = model[:160]
        effort = payload.get("effort") or payload.get("reasoning_effort")
        if isinstance(effort, str):
            state.reasoning_effort = effort[:32]
        return []
    if record_type == "compacted" or payload_type == "context_compacted":
        event = base_event(state, at_ms, "compaction")
        event["compaction_kind"] = payload_type or "compacted"
        return [event]
    if record_type == "event_msg" and payload_type == "task_started":
        turn_id = safe_identifier(payload.get("turn_id"), state.salt, "turn")
        state.current_turn_id = turn_id
        state.turn_started[turn_id] = at_ms
        event = base_event(state, at_ms, "turn_start")
        event["turn_id"] = turn_id
        return [event]
    if record_type == "event_msg" and payload_type in {"task_complete", "turn_aborted"}:
        raw_turn_id = payload.get("turn_id")
        turn_id = (
            safe_identifier(raw_turn_id, state.salt, "turn")
            if raw_turn_id
            else state.current_turn_id
        )
        started_at = state.turn_started.pop(turn_id, at_ms)
        event = base_event(state, at_ms, "turn_end")
        event.update(
            turn_id=turn_id,
            tool_status="aborted" if payload_type == "turn_aborted" else "ok",
            turn_latency_ms=max(0, at_ms - started_at),
        )
        return [event]
    if record_type == "event_msg" and payload_type == "token_count":
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
        if not usage:
            return quota_events(payload, state, at_ms)
        event = base_event(state, at_ms, "tokens")
        event.update(usage_fields(usage))
        raw_request_id = payload.get("request_id")
        event["request_id"] = (
            safe_identifier(raw_request_id, state.salt, "request")
            if raw_request_id
            else state.current_turn_id
        )
        context_window = number(info.get("model_context_window"))
        if context_window is not None:
            event["context_window"] = context_window
            event["context_used"] = event["input_tokens"] + event["cached_input_tokens"]
        explicit_cost = number(usage.get("cost_usd"), integer=False)
        if explicit_cost is not None:
            event["cost_usd"] = explicit_cost
        return [event, *quota_events(payload, state, at_ms)]
    if record_type in {"response_item", "event_msg"} and payload_type in {
        "function_call",
        "custom_tool_call",
    }:
        call_id = safe_identifier(payload.get("call_id"), state.salt, "call")
        tool_name = str(payload.get("name") or "")[:160]
        state.tool_started[call_id] = (at_ms, tool_name)
        event = base_event(state, at_ms, "tool")
        event.update(call_id=call_id, tool_name=tool_name, tool_status="started")
        return [event]
    if record_type in {"response_item", "event_msg"} and payload_type in {
        "function_call_output",
        "custom_tool_call_output",
        "exec_command_end",
        "patch_apply_end",
    }:
        call_id = safe_identifier(payload.get("call_id"), state.salt, "call")
        started_at, tool_name = state.tool_started.pop(call_id, (at_ms, ""))
        status = payload.get("status")
        if status not in {"ok", "error", "cancelled"}:
            status = "error" if number(payload.get("exit_code")) not in {None, 0} else "ok"
        event = base_event(state, at_ms, "tool")
        event.update(
            call_id=call_id,
            tool_name=tool_name,
            tool_status=status,
            tool_duration_ms=max(0, at_ms - started_at),
        )
        return [event]
    return []


def quota_events(payload: dict[str, Any], state: ParseState, at_ms: int) -> list[dict[str, Any]]:
    rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
    results = []
    for name in ("primary", "secondary"):
        quota = rate_limits.get(name) if isinstance(rate_limits.get(name), dict) else {}
        used = number(quota.get("used_percent"), integer=False)
        if used is None:
            continue
        event = base_event(state, at_ms, "quota")
        event.update(
            tool_name=name,
            quota_window_minutes=number(quota.get("window_minutes")),
            quota_used_percent=used,
            quota_resets_at=(
                int(quota["resets_at"] * 1000)
                if isinstance(quota.get("resets_at"), (int, float))
                else timestamp_ms(quota.get("resets_at"))
            ),
        )
        results.append(event)
    return results


EVENT_COLUMNS = (
    "event_key",
    "provider",
    "source_id",
    "timestamp",
    "minute",
    "event_kind",
    "account_id",
    "session_id",
    "parent_session_id",
    "agent_id",
    "turn_id",
    "request_id",
    "call_id",
    "model",
    "reasoning_effort",
    *TOKEN_COLUMNS,
    "context_window",
    "context_used",
    "compaction_kind",
    "tool_name",
    "tool_status",
    "tool_duration_ms",
    "turn_latency_ms",
    "quota_window_minutes",
    "quota_used_percent",
    "quota_resets_at",
    "cost_usd",
)


def insert_event(connection: sqlite3.Connection, event: dict[str, Any]) -> bool:
    values = []
    text_defaults = {
        "account_id",
        "session_id",
        "parent_session_id",
        "agent_id",
        "turn_id",
        "request_id",
        "call_id",
        "model",
        "reasoning_effort",
        "compaction_kind",
        "tool_name",
        "tool_status",
    }
    for column in EVENT_COLUMNS:
        if column in text_defaults:
            values.append(event.get(column) or "")
        elif column in TOKEN_COLUMNS:
            values.append(event.get(column) or 0)
        else:
            values.append(event.get(column))
    placeholders = ",".join("?" for _ in EVENT_COLUMNS)
    cursor = connection.execute(
        f"insert or ignore into events({','.join(EVENT_COLUMNS)}) values({placeholders})",
        values,
    )
    return cursor.rowcount > 0


def source_state(connection: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return connection.execute("select * from sources where source_id=?", (source_id,)).fetchone()


@dataclass(frozen=True)
class SourceSnapshot:
    stat: os.stat_result
    source_id: str
    previous: sqlite3.Row | None


@dataclass(frozen=True)
class InventorySource:
    path: Path
    provider: str
    source_id: str
    fallback_session_id: str
    path_agent_id: str


@dataclass
class SourceInventory:
    entries: dict[tuple[str, Path], InventorySource] = field(default_factory=dict)

    def discover(self, config: Config, salt: bytes) -> list[InventorySource]:
        seen: set[tuple[str, Path]] = set()
        sources = []
        for provider, root in (
            ("claude", config.claude_projects),
            ("codex", config.codex_sessions),
        ):
            for path in iter_jsonl(root):
                key = (provider, path)
                seen.add(key)
                source = self.entries.get(key)
                if source is None:
                    resolved = str(path.resolve())
                    source = InventorySource(
                        path=path,
                        provider=provider,
                        source_id=keyed_hash(salt, f"source:{provider}", resolved),
                        fallback_session_id=keyed_hash(salt, "path-session", resolved),
                        path_agent_id=(
                            keyed_hash(salt, "path-agent", resolved)
                            if provider == "claude" and path.name.startswith("agent-")
                            else ""
                        ),
                    )
                    self.entries[key] = source
                sources.append(source)
        for key in self.entries.keys() - seen:
            del self.entries[key]
        return sources


@dataclass(frozen=True)
class SourceCandidate:
    path: Path
    provider: str
    account_id: str
    snapshot: SourceSnapshot
    category: str
    fallback_session_id: str
    path_agent_id: str


@dataclass(frozen=True)
class ScanResult:
    lines: int
    events: int
    source_id: str
    bytes_read: int
    complete: bool
    stat: os.stat_result


def inspect_source(
    path: Path,
    provider: str,
    salt: bytes,
    known_sources: dict[str, sqlite3.Row] | None = None,
    connection: sqlite3.Connection | None = None,
    source_id: str | None = None,
) -> SourceSnapshot:
    stat = path.stat()
    source_id = source_id or keyed_hash(salt, f"source:{provider}", str(path.resolve()))
    previous = known_sources.get(source_id) if known_sources is not None else None
    if known_sources is None and connection is not None:
        previous = source_state(connection, source_id)
    return SourceSnapshot(stat=stat, source_id=source_id, previous=previous)


def source_is_unchanged(snapshot: SourceSnapshot) -> bool:
    previous = snapshot.previous
    return bool(
        previous
        and previous["inode"] == snapshot.stat.st_ino
        and previous["offset"] == snapshot.stat.st_size
        and previous["mtime_ns"] == snapshot.stat.st_mtime_ns
    )


def source_category(snapshot: SourceSnapshot, live_pending: bool = False) -> str:
    previous = snapshot.previous
    if (
        previous
        and previous["inode"] == snapshot.stat.st_ino
        and snapshot.stat.st_size > previous["offset"]
        and (previous["offset"] == previous["size"] or live_pending)
    ):
        return "live"
    return "backfill"


def source_remaining_bytes(snapshot: SourceSnapshot) -> int:
    previous = snapshot.previous
    if (
        previous
        and previous["inode"] == snapshot.stat.st_ino
        and snapshot.stat.st_size >= previous["offset"]
    ):
        return snapshot.stat.st_size - previous["offset"]
    return snapshot.stat.st_size


def hydrate_state(connection: sqlite3.Connection, state: ParseState) -> None:
    row = connection.execute(
        "select session_id, parent_session_id, agent_id, model, reasoning_effort, "
        "current_turn_id, latest_user_at from parser_state where source_id=?",
        (state.source_id,),
    ).fetchone()
    if row:
        state.session_id = row["session_id"]
        state.parent_session_id = row["parent_session_id"]
        state.agent_id = row["agent_id"]
        state.model = row["model"]
        state.reasoning_effort = row["reasoning_effort"]
        state.current_turn_id = row["current_turn_id"]
        state.latest_user_at = row["latest_user_at"]
    for row in connection.execute(
        "select turn_id, started_at from open_turns where source_id=?",
        (state.source_id,),
    ):
        state.turn_started[row["turn_id"]] = row["started_at"]
    for row in connection.execute(
        "select call_id, started_at, tool_name from open_tools where source_id=?",
        (state.source_id,),
    ):
        state.tool_started[row["call_id"]] = (row["started_at"], row["tool_name"])


def persist_state(connection: sqlite3.Connection, state: ParseState) -> None:
    connection.execute(
        "insert into parser_state(source_id, session_id, parent_session_id, agent_id, "
        "model, reasoning_effort, current_turn_id, latest_user_at) values(?, ?, ?, ?, ?, ?, ?, ?) "
        "on conflict(source_id) do update set session_id=excluded.session_id, "
        "parent_session_id=excluded.parent_session_id, agent_id=excluded.agent_id, "
        "model=excluded.model, reasoning_effort=excluded.reasoning_effort, "
        "current_turn_id=excluded.current_turn_id, latest_user_at=excluded.latest_user_at",
        (
            state.source_id,
            state.session_id,
            state.parent_session_id,
            state.agent_id,
            state.model,
            state.reasoning_effort,
            state.current_turn_id,
            state.latest_user_at,
        ),
    )
    connection.execute("delete from open_turns where source_id=?", (state.source_id,))
    connection.executemany(
        "insert into open_turns(source_id, turn_id, started_at) values(?, ?, ?)",
        [(state.source_id, turn_id, started_at) for turn_id, started_at in state.turn_started.items()],
    )
    connection.execute("delete from open_tools where source_id=?", (state.source_id,))
    connection.executemany(
        "insert into open_tools(source_id, call_id, started_at, tool_name) values(?, ?, ?, ?)",
        [
            (state.source_id, call_id, started_at, tool_name)
            for call_id, (started_at, tool_name) in state.tool_started.items()
        ],
    )


def rebuild_source_minutes(connection: sqlite3.Connection, source_id: str) -> None:
    connection.execute("delete from minute_metrics where source_id=?", (source_id,))
    connection.execute(
        """
        insert into minute_metrics(
            source_id, minute, provider, account_id, model, reasoning_effort, session_id, agent_id,
            input_tokens, cached_input_tokens, cache_create_tokens, output_tokens,
            reasoning_tokens, total_tokens, cost_usd
        )
        with ranked as (
            select *, row_number() over (
                partition by provider, session_id, request_id
                order by total_tokens desc, timestamp desc
            ) as request_rank
            from events
            where source_id=? and event_kind='tokens' and request_id!=''
        ), deduplicated as (
            select * from ranked where request_rank=1
            union all
            select *, 1 as request_rank from events
            where source_id=? and event_kind='tokens' and request_id=''
        )
        select source_id, minute, provider, account_id, model, reasoning_effort, session_id, agent_id,
            sum(input_tokens), sum(cached_input_tokens), sum(cache_create_tokens),
            sum(output_tokens), sum(reasoning_tokens), sum(total_tokens), sum(cost_usd)
        from deduplicated
        group by source_id, minute, provider, account_id, model, reasoning_effort, session_id, agent_id
        """,
        (source_id, source_id),
    )


def requests_span_sources(
    connection: sqlite3.Connection,
    request_keys: set[tuple[str, str, str]],
) -> bool:
    if not request_keys:
        return False
    connection.execute(
        "create temp table if not exists dirty_request_keys("
        "provider text, session_id text, request_id text, "
        "primary key(provider, session_id, request_id)) without rowid"
    )
    connection.execute("delete from dirty_request_keys")
    connection.executemany(
        "insert or ignore into dirty_request_keys values(?, ?, ?)",
        request_keys,
    )
    row = connection.execute(
        "select 1 from events e join dirty_request_keys d "
        "on d.provider=e.provider and d.session_id=e.session_id "
        "and d.request_id=e.request_id "
        "group by e.provider, e.session_id, e.request_id "
        "having count(distinct e.source_id)>1 limit 1"
    ).fetchone()
    return row is not None


def scan_file(
    path: Path,
    provider: str,
    connection: sqlite3.Connection,
    salt: bytes,
    account_id: str,
    account_spans_by_session: dict[str, list[AccountSpan]],
    snapshot: SourceSnapshot | None = None,
    max_lines: int | None = None,
    fallback_session_id: str = "",
    path_agent_id: str = "",
) -> ScanResult:
    if max_lines is not None and max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    snapshot = snapshot or inspect_source(path, provider, salt, connection=connection)
    stat = snapshot.stat
    source_id = snapshot.source_id
    previous = snapshot.previous
    offset = 0
    if previous and previous["inode"] == stat.st_ino and stat.st_size >= previous["offset"]:
        offset = previous["offset"]
    state = ParseState(provider=provider, source_id=source_id, salt=salt, account_id=account_id)
    hydrate_state(connection, state)
    if not state.session_id:
        state.session_id = fallback_session_id or keyed_hash(
            salt, "path-session", str(path.resolve())
        )
    if provider == "claude":
        if path.name.startswith("agent-"):
            state.agent_id = path_agent_id or keyed_hash(
                salt, "path-agent", str(path.resolve())
            )
        state.account_spans_by_session = account_spans_by_session
    inserted = 0
    inserted_request_keys: set[tuple[str, str, str]] = set()
    parsed_lines = 0
    new_offset = offset
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                if max_lines is not None and parsed_lines >= max_lines:
                    break
                line_start = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    new_offset = line_start
                    break
                new_offset = handle.tell()
                parsed_lines += 1
                try:
                    record = json.loads(raw_line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                parsed = claude_events(record, state) if provider == "claude" else codex_events(record, state)
                for index, event in enumerate(parsed):
                    digest = hashlib.sha256(raw_line).hexdigest()
                    event["event_key"] = keyed_hash(
                        salt,
                        "event",
                        f"{source_id}:{line_start}:{index}:{digest}",
                    )
                    if insert_event(connection, event):
                        inserted += 1
                        request_id = event.get("request_id") or ""
                        if event.get("event_kind") == "tokens" and request_id:
                            inserted_request_keys.add(
                                (
                                    event.get("provider") or "",
                                    event.get("session_id") or "",
                                    request_id,
                                )
                            )
    except OSError:
        raise
    end_stat = path.stat()
    if end_stat.st_ino != stat.st_ino:
        raise RuntimeError(f"Agent Metrics source changed identity while being read: {path}")
    connection.execute(
        "insert into sources(source_id, provider, account_id, inode, size, mtime_ns, offset, updated_at) "
        "values(?, ?, ?, ?, ?, ?, ?, ?) on conflict(source_id) do update set "
        "account_id=case when sources.account_id='' then excluded.account_id else sources.account_id end, "
        "inode=excluded.inode, size=excluded.size, mtime_ns=excluded.mtime_ns, "
        "offset=excluded.offset, updated_at=excluded.updated_at",
        (
            source_id,
            provider,
            account_id,
            end_stat.st_ino,
            end_stat.st_size,
            end_stat.st_mtime_ns,
            new_offset,
            int(time.time() * 1000),
        ),
    )
    persist_state(connection, state)
    rebuild_source_minutes(connection, source_id)
    if requests_span_sources(connection, inserted_request_keys):
        set_metadata_value(connection, "minutes_dirty", "1")
    return ScanResult(
        lines=parsed_lines,
        events=inserted,
        source_id=source_id,
        bytes_read=new_offset - offset,
        complete=new_offset == end_stat.st_size,
        stat=end_stat,
    )


def iter_jsonl(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    return sorted(path for path in root.rglob("*.jsonl") if path.is_file())


def rebuild_minutes(connection: sqlite3.Connection) -> None:
    connection.execute("delete from minute_metrics")
    connection.execute(
        """
        insert into minute_metrics(
            source_id, minute, provider, account_id, model, reasoning_effort, session_id, agent_id,
            input_tokens, cached_input_tokens, cache_create_tokens, output_tokens,
            reasoning_tokens, total_tokens, cost_usd
        )
        with ranked as (
            select *, row_number() over (
                partition by provider, session_id, request_id
                order by total_tokens desc, timestamp desc
            ) as request_rank
            from events
            where event_kind='tokens' and request_id!=''
        ), deduplicated as (
            select * from ranked where request_rank=1
            union all
            select *, 1 as request_rank from events
            where event_kind='tokens' and request_id=''
        )
        select source_id, minute, provider, account_id, model, reasoning_effort, session_id, agent_id,
            sum(input_tokens), sum(cached_input_tokens), sum(cache_create_tokens),
            sum(output_tokens), sum(reasoning_tokens), sum(total_tokens), sum(cost_usd)
        from deduplicated
        group by source_id, minute, provider, account_id, model, reasoning_effort, session_id, agent_id
        """
    )


def reattribute_claude_events(
    connection: sqlite3.Connection, spans_by_session: dict[str, list[AccountSpan]]
) -> int:
    sessions = [
        row[0]
        for row in connection.execute(
            "select distinct session_id from events where provider='claude'"
        )
    ]
    changed = 0
    for session_id in sessions:
        connection.execute("begin")
        try:
            spans = spans_by_session.get(session_id, [])
            updates = []
            for row in connection.execute(
                "select event_key, timestamp, account_id from events "
                "where provider='claude' and session_id=?",
                (session_id,),
            ):
                expected = account_for_timestamp(spans, row["timestamp"])
                if expected != row["account_id"]:
                    updates.append((expected, row["event_key"]))
            connection.executemany(
                "update events set account_id=? where event_key=?",
                updates,
            )
            changed += len(updates)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return changed


def metadata_value(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("select value from metadata where key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def set_metadata_value(connection: sqlite3.Connection, key: str, value: str) -> bool:
    if metadata_value(connection, key) == value:
        return False
    connection.execute(
        "insert into metadata(key, value) values(?, ?) "
        "on conflict(key) do update set value=excluded.value",
        (key, value),
    )
    return True


def collect_source_candidates(
    config: Config,
    salt: bytes,
    known_sources: dict[str, sqlite3.Row],
    codex_account: str,
    live_pending_source_ids: set[str],
    inventory: SourceInventory,
) -> tuple[int, list[SourceCandidate]]:
    candidates = []
    sources = inventory.discover(config, salt)
    for source in sources:
        snapshot = inspect_source(
            source.path,
            source.provider,
            salt,
            known_sources,
            source_id=source.source_id,
        )
        if source_is_unchanged(snapshot):
            continue
        source_account = ""
        if source.provider == "codex":
            source_account = codex_account
            if snapshot.previous is not None and snapshot.previous["account_id"]:
                source_account = snapshot.previous["account_id"]
        candidates.append(
            SourceCandidate(
                path=source.path,
                provider=source.provider,
                account_id=source_account,
                snapshot=snapshot,
                category=source_category(
                    snapshot, snapshot.source_id in live_pending_source_ids
                ),
                fallback_session_id=source.fallback_session_id,
                path_agent_id=source.path_agent_id,
            )
        )
    return len(sources), candidates


def rotate_after_cursor(
    candidates: list[SourceCandidate], cursor: str | None
) -> list[SourceCandidate]:
    ordered = sorted(candidates, key=lambda candidate: candidate.snapshot.source_id)
    if not cursor:
        return ordered
    for index, candidate in enumerate(ordered):
        if candidate.snapshot.source_id > cursor:
            return ordered[index:] + ordered[:index]
    return ordered


def rotate_names_after_cursor(names: list[str], cursor: str | None) -> list[str]:
    ordered = sorted(names)
    if not cursor:
        return ordered
    for index, name in enumerate(ordered):
        if name > cursor:
            return ordered[index:] + ordered[:index]
    return ordered


def process_source_candidate(
    connection: sqlite3.Connection,
    candidate: SourceCandidate,
    salt: bytes,
    spans: dict[str, list[AccountSpan]],
    max_lines: int | None,
    cursors: dict[str, str],
) -> ScanResult:
    connection.execute("begin")
    try:
        result = scan_file(
            candidate.path,
            candidate.provider,
            connection,
            salt,
            candidate.account_id,
            spans,
            candidate.snapshot,
            max_lines,
            candidate.fallback_session_id,
            candidate.path_agent_id,
        )
        live_pending_key = f"live_pending:{result.source_id}"
        if candidate.category == "live" and not result.complete:
            set_metadata_value(connection, live_pending_key, "1")
        else:
            connection.execute("delete from metadata where key=?", (live_pending_key,))
        for key, value in cursors.items():
            set_metadata_value(connection, key, value)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return result


def process_candidate_group(
    connection: sqlite3.Connection,
    candidates: list[SourceCandidate],
    salt: bytes,
    spans: dict[str, list[AccountSpan]],
    line_budget: int,
    category: str,
    processed_source_ids: set[str],
    pending_by_source: dict[str, SourceCandidate],
) -> tuple[dict[str, int], list[SourceCandidate]]:
    counts = {"lines": 0, "events": 0, "files": 0}
    deferred = []
    by_provider = {
        provider: rotate_after_cursor(
            [candidate for candidate in candidates if candidate.provider == provider],
            metadata_value(connection, f"bounded_source_cursor:{category}:{provider}"),
        )
        for provider in {candidate.provider for candidate in candidates}
    }
    providers = rotate_names_after_cursor(
        [provider for provider, items in by_provider.items() if items],
        metadata_value(connection, f"bounded_provider_cursor:{category}"),
    )
    if not providers or line_budget < 1:
        return counts, candidates
    quotas = {provider: line_budget // len(providers) for provider in providers}
    for provider in providers[: line_budget % len(providers)]:
        quotas[provider] += 1

    def process_provider(provider: str, budget: int) -> int:
        used = 0
        queue = by_provider[provider]
        while queue and used < budget:
            candidate = queue.pop(0)
            remaining = budget - used
            source_budget = remaining if not queue else min(remaining, SOURCE_LINE_QUANTUM)
            result = process_source_candidate(
                connection,
                candidate,
                salt,
                spans,
                source_budget,
                {
                    f"bounded_source_cursor:{category}:{provider}": candidate.snapshot.source_id,
                    f"bounded_provider_cursor:{category}": provider,
                    "bounded_class_cursor": category,
                },
            )
            used += result.lines
            counts["lines"] += result.lines
            counts["events"] += result.events
            if result.source_id not in processed_source_ids:
                processed_source_ids.add(result.source_id)
                counts["files"] += 1
            secure_sqlite_files(connection_database_path(connection))
            if counts["files"] and counts["files"] % 25 == 0:
                connection.execute("pragma wal_checkpoint(passive)")
            refreshed = update_pending_candidate(
                connection, candidate, result, pending_by_source
            )
            if result.lines and refreshed is not None:
                deferred.append(
                    refreshed
                )
        return used

    for provider in providers:
        process_provider(provider, quotas[provider])
    remaining_budget = line_budget - counts["lines"]
    while remaining_budget > 0:
        available = [provider for provider in providers if by_provider[provider]]
        if not available:
            break
        progress = 0
        for provider in available:
            used = process_provider(provider, remaining_budget)
            progress += used
            remaining_budget -= used
            if remaining_budget < 1:
                break
        if not progress:
            break
    remaining = [candidate for items in by_provider.values() for candidate in items]
    remaining.extend(deferred)
    return counts, remaining


def update_pending_candidate(
    connection: sqlite3.Connection,
    candidate: SourceCandidate,
    result: ScanResult,
    pending_by_source: dict[str, SourceCandidate],
) -> SourceCandidate | None:
    if result.complete:
        pending_by_source.pop(result.source_id, None)
        return None
    current = source_state(connection, result.source_id)
    refreshed = SourceCandidate(
        path=candidate.path,
        provider=candidate.provider,
        account_id=candidate.account_id,
        snapshot=SourceSnapshot(result.stat, result.source_id, current),
        category=candidate.category,
        fallback_session_id=candidate.fallback_session_id,
        path_agent_id=candidate.path_agent_id,
    )
    pending_by_source[result.source_id] = refreshed
    return refreshed


def connection_database_path(connection: sqlite3.Connection) -> Path:
    row = connection.execute("pragma database_list").fetchone()
    return Path(row[2])


def pending_counts(candidates: Iterable[SourceCandidate]) -> dict[str, int]:
    result = {
        "live_files": 0,
        "live_bytes": 0,
        "backfill_files": 0,
        "backfill_bytes": 0,
    }
    for candidate in candidates:
        result[f"{candidate.category}_files"] += 1
        result[f"{candidate.category}_bytes"] += source_remaining_bytes(candidate.snapshot)
    result["files"] = result["live_files"] + result["backfill_files"]
    result["bytes"] = result["live_bytes"] + result["backfill_bytes"]
    return result


def live_pending_source_ids(connection: sqlite3.Connection) -> set[str]:
    prefix = "live_pending:"
    return {
        row[0][len(prefix):]
        for row in connection.execute(
            "select key from metadata where key like 'live_pending:%'"
        )
    }


def bounded_class_budgets(
    connection: sqlite3.Connection,
    candidates: list[SourceCandidate],
    max_lines: int,
) -> dict[str, int]:
    categories = [
        category for category in ("live", "backfill")
        if any(candidate.category == category for candidate in candidates)
    ]
    if len(categories) < 2:
        return {category: max_lines for category in categories}
    if max_lines == 1:
        ordered = rotate_names_after_cursor(
            categories, metadata_value(connection, "bounded_class_cursor")
        )
        return {ordered[0]: 1, ordered[1]: 0}
    live = max(1, max_lines // 5)
    return {"live": live, "backfill": max_lines - live}


def sync(
    config: Config,
    max_lines: int | None = None,
    inventory: SourceInventory | None = None,
    *,
    collector_locked: bool = False,
) -> dict[str, int]:
    if not collector_locked:
        with ingestion_lock(config.data_dir):
            return sync(
                config,
                max_lines=max_lines,
                inventory=inventory,
                collector_locked=True,
            )
    if max_lines is not None and max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    salt = load_or_create_salt(config.data_dir)
    connection = connect_database(config.database)
    try:
        statusline_rules = (
            load_statusline_account_labels(config.claude_statusline_config)
            if config.reuse_statusline_account_labels
            else []
        )
        changes_before_accounts = connection.total_changes
        spans = load_claude_account_spans(
            config.claude_account_spans, config, salt, connection, statusline_rules
        )
        codex_account = load_codex_account(config.codex_auth, config, salt, connection)
        quota_observations = ingest_quota_observations(
            config, salt, connection, statusline_rules
        )
        account_updates = connection.total_changes - changes_before_accounts
        connection.commit()
        secure_sqlite_files(config.database)
        spans_fingerprint = account_spans_fingerprint(config.claude_account_spans)
        previous_fingerprint = metadata_value(connection, "claude_account_spans_fingerprint")
        spans_changed = spans_fingerprint != previous_fingerprint
        reattributed = 0
        if spans_changed:
            reattributed = reattribute_claude_events(connection, spans)
            connection.execute(
                "insert into metadata(key, value) values('claude_account_spans_fingerprint', ?) "
                "on conflict(key) do update set value=excluded.value",
                (spans_fingerprint,),
            )
            connection.commit()
        known_sources = {
            row["source_id"]: row for row in connection.execute("select * from sources")
        }
        source_inventory = inventory or SourceInventory()
        files, candidates = collect_source_candidates(
            config,
            salt,
            known_sources,
            codex_account,
            live_pending_source_ids(connection),
            source_inventory,
        )
        pending_by_source = {
            candidate.snapshot.source_id: candidate for candidate in candidates
        }
        totals = {
            "lines": 0,
            "events": 0,
            "files": 0,
            "live_lines": 0,
            "live_files": 0,
            "backfill_lines": 0,
            "backfill_files": 0,
        }
        processed_source_ids: set[str] = set()
        if max_lines is None:
            for candidate in candidates:
                result = process_source_candidate(
                    connection, candidate, salt, spans, None, {}
                )
                totals["lines"] += result.lines
                totals["events"] += result.events
                processed_source_ids.add(result.source_id)
                totals["files"] = len(processed_source_ids)
                totals[f"{candidate.category}_lines"] += result.lines
                totals[f"{candidate.category}_files"] += 1
                update_pending_candidate(
                    connection, candidate, result, pending_by_source
                )
                secure_sqlite_files(config.database)
                if totals["files"] % 25 == 0:
                    connection.execute("pragma wal_checkpoint(passive)")
        else:
            budgets = bounded_class_budgets(connection, candidates, max_lines)
            remaining_by_category = {
                category: [
                    candidate for candidate in candidates if candidate.category == category
                ]
                for category in ("live", "backfill")
            }
            for category in ("live", "backfill"):
                counts, remaining = process_candidate_group(
                    connection,
                    remaining_by_category[category],
                    salt,
                    spans,
                    budgets.get(category, 0),
                    category,
                    processed_source_ids,
                    pending_by_source,
                )
                remaining_by_category[category] = remaining
                totals["lines"] += counts["lines"]
                totals["events"] += counts["events"]
                totals["files"] += counts["files"]
                totals[f"{category}_lines"] += counts["lines"]
                totals[f"{category}_files"] += counts["files"]
            unused = max_lines - totals["lines"]
            for category in ("live", "backfill"):
                if unused < 1 or not remaining_by_category[category]:
                    continue
                counts, remaining = process_candidate_group(
                    connection,
                    remaining_by_category[category],
                    salt,
                    spans,
                    unused,
                    category,
                    processed_source_ids,
                    pending_by_source,
                )
                remaining_by_category[category] = remaining
                unused -= counts["lines"]
                totals["lines"] += counts["lines"]
                totals["events"] += counts["events"]
                totals["files"] += counts["files"]
                totals[f"{category}_lines"] += counts["lines"]
                totals[f"{category}_files"] += counts["files"]

        pending = pending_counts(pending_by_source.values())
        backlog_metadata_changed = False
        for key, value in pending.items():
            backlog_metadata_changed |= set_metadata_value(
                connection, f"pending_{key}", str(value)
            )
        now_ms = int(time.time() * 1000)
        last_retention = metadata_value(connection, "last_retention_at")
        retention_due = last_retention is None or now_ms - int(last_retention) >= 86_400_000
        deleted = 0
        if retention_due:
            cutoff = int(
                (datetime.now(timezone.utc) - timedelta(days=config.retention_days)).timestamp()
                * 1000
            )
            deleted = connection.execute("delete from events where timestamp < ?", (cutoff,)).rowcount
            connection.execute(
                "delete from quota_observations where observed_minute < ?", (cutoff,)
            )
            connection.execute(
                "insert into metadata(key, value) values('last_retention_at', ?) "
                "on conflict(key) do update set value=excluded.value",
                (str(now_ms),),
            )
        minutes_dirty = metadata_value(connection, "minutes_dirty") == "1"
        if reattributed or deleted or minutes_dirty:
            rebuild_minutes(connection)
            connection.execute("delete from metadata where key='minutes_dirty'")
        if (
            totals["files"]
            or reattributed
            or deleted
            or account_updates
            or spans_changed
            or retention_due
            or backlog_metadata_changed
        ):
            connection.execute(
                "insert into metadata(key, value) values('last_sync_at', ?) "
                "on conflict(key) do update set value=excluded.value",
                (str(now_ms),),
            )
            connection.commit()
            connection.execute("pragma wal_checkpoint(passive)")
            secure_sqlite_files(config.database)
        return {
            "files": files,
            "updated_files": totals["files"],
            "lines": totals["lines"],
            "events": totals["events"],
            "live_files": totals["live_files"],
            "live_lines": totals["live_lines"],
            "backfill_files": totals["backfill_files"],
            "backfill_lines": totals["backfill_lines"],
            "pending_files": pending["files"],
            "pending_bytes": pending["bytes"],
            "pending_live_files": pending["live_files"],
            "pending_live_bytes": pending["live_bytes"],
            "pending_backfill_files": pending["backfill_files"],
            "pending_backfill_bytes": pending["backfill_bytes"],
            "expired": deleted,
            "reattributed": reattributed,
            "quota_observations": quota_observations,
        }
    finally:
        connection.close()


def filters_from_query(
    query: dict[str, list[str]], time_column: str = "minute", prefix: str = ""
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    mapping = {
        "provider": "provider",
        "account": "account_id",
        "model": "model",
        "effort": "reasoning_effort",
        "session": "session_id",
        "agent": "agent_id",
    }
    for key, column in mapping.items():
        value = (query.get(key) or [""])[0]
        if value:
            clauses.append(f"{prefix}{column}=?")
            values.append(value)
    since = (query.get("since") or [""])[0]
    if since:
        try:
            clauses.append(f"{prefix}{time_column}>=?")
            values.append(int(since))
        except ValueError:
            pass
    return (" and " + " and ".join(clauses) if clauses else "", values)


def percentile(values: list[int], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))]


def quota_capacity_rows(
    connection: sqlite3.Connection, query: dict[str, list[str]]
) -> list[dict[str, Any]]:
    provider = (query.get("provider") or [""])[0]
    if provider and provider != "claude":
        return []
    account = (query.get("account") or [""])[0]
    since = (query.get("since") or [""])[0]
    clauses = ["quota_name='five_hour'"]
    values: list[Any] = []
    if account:
        clauses.append("account_id=?")
        values.append(account)
    if since:
        try:
            clauses.append("observed_minute>=?")
            values.append(int(since))
        except ValueError:
            pass
    samples: dict[tuple[str, str, str, str, str], list[tuple[float, int]]] = defaultdict(list)
    token_clauses = ["model!=''", "reasoning_effort!=''"]
    token_values: list[Any] = []
    for key, column in {"model": "model", "effort": "reasoning_effort"}.items():
        selected = (query.get(key) or [""])[0]
        if selected:
            token_clauses.append(f"{column}=?")
            token_values.append(selected)
    for key, minimum, maximum in (
        ("session", "minimum_session", "maximum_session"),
        ("agent", "minimum_agent", "maximum_agent"),
    ):
        selected = (query.get(key) or [""])[0]
        if selected:
            token_clauses.extend((f"{minimum}={maximum}", f"{minimum}=?"))
            token_values.append(selected)
    rows = connection.execute(
        "with ordered as (select account_id, account_label, plan_cohort, observed_minute, "
        "used_percent, resets_at, stale, pending_reset, "
        "lag(observed_minute) over account_window previous_minute, "
        "lag(used_percent) over account_window previous_percent, "
        "lag(resets_at) over account_window previous_reset, "
        "lag(stale) over account_window previous_stale, "
        "lag(pending_reset) over account_window previous_pending "
        "from quota_observations where "
        + " and ".join(clauses)
        + " window account_window as (partition by account_id order by observed_minute)), "
        "eligible as (select * from ordered where previous_minute is not null and stale=0 "
        "and previous_stale=0 and pending_reset=0 and previous_pending=0 and resets_at is not null "
        "and resets_at=previous_reset and used_percent>previous_percent), "
        "intervals as (select eligible.account_id, eligible.account_label, eligible.plan_cohort, "
        "eligible.observed_minute, eligible.used_percent-eligible.previous_percent utilization_delta, "
        "min(metrics.model) model, min(metrics.reasoning_effort) reasoning_effort, "
        "min(metrics.session_id) minimum_session, max(metrics.session_id) maximum_session, "
        "min(metrics.agent_id) minimum_agent, max(metrics.agent_id) maximum_agent, "
        "sum(metrics.total_tokens) tracked_tokens "
        "from eligible join minute_metrics metrics on metrics.account_id=eligible.account_id "
        "and metrics.minute>eligible.previous_minute and metrics.minute<=eligible.observed_minute "
        "where metrics.provider='claude' group by eligible.account_id, eligible.observed_minute "
        "having min(metrics.model)=max(metrics.model) and "
        "min(metrics.reasoning_effort)=max(metrics.reasoning_effort)) "
        "select * from intervals where " + " and ".join(token_clauses),
        [*values, *token_values],
    ).fetchall()
    for row in rows:
        tracked_tokens = int(row["tracked_tokens"] or 0)
        if tracked_tokens <= 0:
            continue
        equivalent = tracked_tokens * 100.0 / float(row["utilization_delta"])
        key = (
            row["account_id"],
            row["account_label"],
            row["plan_cohort"],
            row["model"],
            row["reasoning_effort"],
        )
        samples[key].append((equivalent, tracked_tokens))
    result = []
    for (account_id, label, cohort, model, effort), values_by_group in samples.items():
        equivalents = [value[0] for value in values_by_group]
        tracked = [value[1] for value in values_by_group]
        average = sum(equivalents) / len(equivalents)
        variance = sum((value - average) ** 2 for value in equivalents) / len(equivalents)
        result.append(
            {
                "account_id": account_id,
                "account_label": label,
                "plan_cohort": cohort,
                "model": model,
                "reasoning_effort": effort,
                "sample_count": len(equivalents),
                "estimated_tokens_at_100_pct": average,
                "standard_deviation": math.sqrt(variance),
                "minimum_estimate": min(equivalents),
                "maximum_estimate": max(equivalents),
                "minimum_tracked_tokens": min(tracked),
                "maximum_tracked_tokens": max(tracked),
            }
        )
    return sorted(
        result,
        key=lambda row: (
            row["plan_cohort"],
            row["account_label"],
            -row["sample_count"],
            row["model"],
            row["reasoning_effort"],
        ),
    )[:MAX_FILTER_VALUES]


def analysis_rows(
    connection: sqlite3.Connection,
    query: dict[str, list[str]],
) -> dict[str, Any]:
    dimension_query = {key: value for key, value in query.items() if key != "since"}
    clause, values = filters_from_query(dimension_query)
    now = datetime.now().astimezone()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    yesterday_end = yesterday_start + (now - today_start)
    daily_start = today_start - timedelta(days=13)
    sums = ", ".join(f"sum({column}) {column}" for column in TOKEN_COLUMNS)

    def totals(start: datetime, end: datetime) -> dict[str, int]:
        row = connection.execute(
            f"select {sums} from minute_metrics where minute>=? and minute<?" + clause,
            [int(start.timestamp() * 1000), int(end.timestamp() * 1000), *values],
        ).fetchone()
        return {column: int(row[column] or 0) for column in TOKEN_COLUMNS}

    daily_rows = {
        row["day"]: dict(row)
        for row in connection.execute(
            "select date(minute/1000, 'unixepoch', 'localtime') day, "
            + sums
            + " from minute_metrics where minute>=?"
            + clause
            + " group by day order by day",
            [int(daily_start.timestamp() * 1000), *values],
        )
    }
    daily = []
    for offset in range(14):
        day = (daily_start + timedelta(days=offset)).date().isoformat()
        daily.append(
            daily_rows.get(
                day,
                {"day": day, **{column: 0 for column in TOKEN_COLUMNS}},
            )
        )
    daily_accounts = [
        dict(row)
        for row in connection.execute(
            "select date(minute/1000, 'unixepoch', 'localtime') day, account_id, "
            "sum(input_tokens) input_tokens, sum(cached_input_tokens) cached_input_tokens, "
            "sum(cache_create_tokens) cache_create_tokens, sum(output_tokens) output_tokens, "
            "sum(total_tokens) total_tokens from minute_metrics where minute>=?"
            + clause
            + " group by day, account_id order by day desc, total_tokens desc limit ?",
            [int(daily_start.timestamp() * 1000), *values, MAX_ANALYSIS_ROWS],
        )
    ]
    quota_history = []
    provider = (query.get("provider") or [""])[0]
    if provider in ("", "claude"):
        selected_account = (query.get("account") or [""])[0]
        try:
            selected_since = int((query.get("since") or [0])[0] or 0)
        except ValueError:
            selected_since = 0
        quota_since = max(
            selected_since,
            int((now - timedelta(days=7)).timestamp() * 1000),
        )
        quota_values: list[Any] = [quota_since]
        account_clause = ""
        if selected_account:
            account_clause = " and account_id=?"
            quota_values.append(selected_account)
        quota_history = sorted([
            dict(row)
            for row in connection.execute(
                "with bucketed as (select account_id, account_label, observed_minute, "
                "used_percent, resets_at, (observed_minute/600000)*600000 bucket, "
                "row_number() over (partition by account_id, observed_minute/600000 "
                "order by observed_minute desc) rank from quota_observations "
                "where quota_name='five_hour' and used_percent is not null "
                "and stale=0 and pending_reset=0 "
                "and observed_minute>=?"
                + account_clause
                + ") select account_id, account_label, observed_minute, used_percent, "
                "100-used_percent remaining_percent, resets_at from bucketed "
                "where rank=1 order by observed_minute desc, account_label limit ?",
                [*quota_values, MAX_ANALYSIS_ROWS],
            )
        ], key=lambda row: (row["observed_minute"], row["account_label"]))

    today_by_account = {
        row["account_id"]: dict(row)
        for row in connection.execute(
            "select account_id, sum(input_tokens) input_tokens, "
            "sum(cached_input_tokens) cached_input_tokens, "
            "sum(cache_create_tokens) cache_create_tokens, sum(output_tokens) output_tokens, "
            "sum(total_tokens) total_tokens from minute_metrics where minute>=? and minute<?"
            + clause
            + " group by account_id order by total_tokens desc limit ?",
            [
                int(today_start.timestamp() * 1000),
                int(now.timestamp() * 1000),
                *values,
                MAX_FILTER_VALUES,
            ],
        )
    }
    history_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in quota_history:
        history_by_account[row["account_id"]].append(row)
    account_status = []
    for account_id in sorted(set(today_by_account) | set(history_by_account)):
        history = history_by_account.get(account_id, [])
        latest = history[-1] if history else {}
        current_window = history
        for index in range(len(history) - 1, 0, -1):
            if float(history[index]["used_percent"]) < float(history[index - 1]["used_percent"]):
                current_window = history[index:]
                break
        first_used = float(current_window[0]["used_percent"]) if current_window else None
        latest_used = float(latest["used_percent"]) if history else None
        tokens = today_by_account.get(account_id, {})
        account_status.append(
            {
                "account_id": account_id,
                "account_label": latest.get("account_label") or "",
                "today_tokens": int(tokens.get("total_tokens") or 0),
                "today_fresh_tokens": int(tokens.get("input_tokens") or 0)
                + int(tokens.get("output_tokens") or 0),
                "remaining_percent": 100 - latest_used if latest_used is not None else None,
                "drawdown_percent": latest_used - first_used
                if latest_used is not None and first_used is not None
                else None,
                "resets_at": latest.get("resets_at"),
            }
        )
    return {
        "today": totals(today_start, now),
        "yesterday_same_time": totals(yesterday_start, yesterday_end),
        "comparison_at": int(now.timestamp() * 1000),
        "daily": daily,
        "daily_accounts": daily_accounts,
        "quota_history": quota_history,
        "account_status": account_status[:MAX_FILTER_VALUES],
    }


def dashboard_payload(connection: sqlite3.Connection, query: dict[str, list[str]]) -> dict[str, Any]:
    clause, values = filters_from_query(query)
    event_clause, event_values = filters_from_query(query, "timestamp")
    metric_clause, metric_values = filters_from_query(query, prefix="metrics.")
    now_minute = minute_epoch(int(time.time() * 1000))
    first_minute = connection.execute(
        "select min(minute) from minute_metrics where 1=1" + clause,
        values,
    ).fetchone()[0]
    try:
        requested_start = int((query.get("since") or [""])[0])
    except ValueError:
        requested_start = None
    timeline_start = (
        minute_epoch(requested_start)
        if requested_start
        else int(first_minute if first_minute is not None else now_minute)
    )
    timeline_start = min(timeline_start, now_minute)
    minute_count = ((now_minute - timeline_start) // 60_000) + 1
    bucket_minutes = max(1, math.ceil(minute_count / MAX_TIMELINE_POINTS))
    bucket_ms = bucket_minutes * 60_000
    aggregated = {
        int(row["minute"]): dict(row)
        for row in connection.execute(
            "select (minute / ?) * ? minute, sum(input_tokens) input_tokens, "
            "sum(cached_input_tokens) cached_input_tokens, "
            "sum(cache_create_tokens) cache_create_tokens, sum(output_tokens) output_tokens, "
            "sum(reasoning_tokens) reasoning_tokens, sum(total_tokens) total_tokens, "
            "sum(cost_usd) cost_usd from minute_metrics where 1=1"
            + clause
            + " group by 1 order by 1",
            [bucket_ms, bucket_ms, *values],
        )
    }
    timeline_start = (timeline_start // bucket_ms) * bucket_ms
    timeline_end = (now_minute // bucket_ms) * bucket_ms
    timeline = []
    for minute in range(timeline_start, timeline_end + 1, bucket_ms):
        timeline.append(
            aggregated.get(
                minute,
                {
                    "minute": minute,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_create_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0,
                },
            )
        )
    dimensions = {}
    for name, column in {
        "providers": "provider",
        "accounts": "account_id",
        "models": "model",
        "efforts": "reasoning_effort",
        "sessions": "session_id",
        "agents": "agent_id",
    }.items():
        rows = connection.execute(
            f"select {column} value, max(minute) last_minute from minute_metrics "
            f"where {column}!=''" + clause + " group by value order by last_minute desc limit ?",
            [*values, MAX_FILTER_VALUES],
        )
        dimensions[name] = [row["value"] for row in rows]
    account_labels = {
        row["account_id"]: row["label"] or row["account_id"][:10]
        for row in connection.execute(
            "select account_id, label from accounts order by label, account_id limit ?",
            (MAX_FILTER_VALUES,),
        )
    }
    totals_by_account = [
        dict(row)
        for row in connection.execute(
            "select account_id, sum(total_tokens) total_tokens, sum(cost_usd) cost_usd "
            "from minute_metrics where 1=1" + clause
            + " group by account_id order by total_tokens desc limit ?",
            [*values, MAX_FILTER_VALUES],
        )
    ]
    totals_by_model = [
        dict(row)
        for row in connection.execute(
            "select model, sum(total_tokens) total_tokens, sum(cost_usd) cost_usd "
            "from minute_metrics where 1=1" + clause
            + " group by model order by total_tokens desc limit ?",
            [*values, MAX_FILTER_VALUES],
        )
    ]
    session_rows = [
        dict(row)
        for row in connection.execute(
            "with parents as (select provider, session_id, agent_id, max(parent_session_id) "
            "parent_session_id from events where event_kind='tokens' group by provider, "
            "session_id, agent_id) select metrics.session_id, metrics.agent_id, "
            "coalesce(parents.parent_session_id, '') parent_session_id, metrics.provider, "
            "metrics.model, metrics.reasoning_effort, sum(metrics.total_tokens) total_tokens, "
            "min(metrics.minute) started_at, max(metrics.minute) last_at from minute_metrics metrics "
            "left join parents on parents.provider=metrics.provider and "
            "parents.session_id=metrics.session_id and parents.agent_id=metrics.agent_id "
            "where 1=1" + metric_clause + " group by metrics.session_id, metrics.agent_id, "
            "parents.parent_session_id, metrics.provider, metrics.model, metrics.reasoning_effort "
            "order by last_at desc limit 200",
            metric_values,
        )
    ]
    tool_rows = [
        dict(row)
        for row in connection.execute(
            "select tool_name, tool_status, count(*) calls, avg(tool_duration_ms) average_duration_ms "
            "from events where event_kind='tool' and tool_status!='started'"
            + event_clause
            + " group by tool_name, tool_status order by calls desc limit ?",
            [*event_values, MAX_FILTER_VALUES],
        )
    ]
    latency_values = [
        row[0]
        for row in connection.execute(
            "select turn_latency_ms from events where turn_latency_ms is not null"
            + event_clause
            + " order by timestamp desc limit ?",
            [*event_values, MAX_ANALYSIS_ROWS],
        )
    ]
    compactions = connection.execute(
        "select count(*) from events where event_kind='compaction'" + event_clause,
        event_values,
    ).fetchone()[0]
    quota = [
        dict(row)
        for row in connection.execute(
            "select provider, account_id, tool_name quota_name, quota_window_minutes, "
            "quota_used_percent, quota_resets_at, timestamp from events where event_kind='quota' "
            + event_clause + " order by timestamp desc limit 20",
            event_values,
        )
    ]
    overview = {
        key: sum(int(row.get(key) or 0) for row in timeline)
        for key in TOKEN_COLUMNS
    }
    overview["cost_usd"] = sum(float(row.get("cost_usd") or 0) for row in timeline)
    overview["compactions"] = compactions
    backlog = {
        key: int(metadata_value(connection, f"pending_{key}") or 0)
        for key in (
            "files",
            "bytes",
            "live_files",
            "live_bytes",
            "backfill_files",
            "backfill_bytes",
        )
    }
    return {
        "overview": overview,
        "timeline": timeline,
        "timeline_bucket_minutes": bucket_minutes,
        "filters": dimensions,
        "account_labels": account_labels,
        "accounts": totals_by_account,
        "models": totals_by_model,
        "sessions": session_rows,
        "tools": tool_rows,
        "latency": {
            "count": len(latency_values),
            "average_ms": int(sum(latency_values) / len(latency_values)) if latency_values else None,
            "p95_ms": percentile(latency_values, 0.95),
        },
        "quota": quota,
        "quota_capacity": quota_capacity_rows(connection, query),
        "analysis": analysis_rows(connection, query),
        "backlog": backlog,
        "generated_at": int(time.time() * 1000),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    database: Path
    configured_host: str
    configured_port: int
    capability_token: str

    def request_allowed(self) -> bool:
        expected_host = f"{self.configured_host}:{self.configured_port}"
        if self.headers.get("Host") != expected_host:
            self.send_error(HTTPStatus.MISDIRECTED_REQUEST, "invalid Host")
            return False
        expected_origin = f"http://{expected_host}"
        origin = self.headers.get("Origin")
        if origin and origin != expected_origin:
            self.send_error(HTTPStatus.FORBIDDEN, "cross-site Origin rejected")
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self.send_error(HTTPStatus.FORBIDDEN, "cross-site request rejected")
            return False
        return True

    def do_GET(self) -> None:
        if not self.request_allowed():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/dashboard":
            authorization = self.headers.get("Authorization", "")
            expected = f"Bearer {self.capability_token}"
            if not hmac.compare_digest(authorization, expected):
                self.send_error(HTTPStatus.UNAUTHORIZED, "dashboard capability required")
                return
            connection = connect_database_readonly(self.database)
            try:
                self.send_json(dashboard_payload(connection, parse_qs(parsed.query)))
            finally:
                connection.close()
            return
        relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        candidate = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in candidate.parents and candidate != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'none'; object-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        super().end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            f"agent-metrics: {self.command} {urlparse(self.path).path}",
            file=sys.stderr,
        )


def serve(config: Config, bind: str | None = None, port: int | None = None) -> None:
    host = bind or config.bind
    listen_port = port if port is not None else config.port
    validate_loopback(host)
    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {
            "database": config.database,
            "configured_host": host,
            "configured_port": listen_port,
            "capability_token": load_or_create_ui_token(config.data_dir),
        },
    )
    server = ThreadingHTTPServer((host, listen_port), handler)
    handler.configured_port = server.server_port
    print(f"Agent Metrics serving http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def initialize(config: Config) -> None:
    ensure_private_dir(config.data_dir)
    load_or_create_salt(config.data_dir)
    load_or_create_ui_token(config.data_dir)
    connection = connect_database(config.database)
    connection.close()
    target = config.data_dir / "config.toml"
    if not target.exists() and CONFIG_EXAMPLE.is_file():
        text = CONFIG_EXAMPLE.read_text(encoding="utf-8")
        target.write_text(text, encoding="utf-8")
        target.chmod(0o600)
    print(f"Agent Metrics initialized at {config.data_dir}")


def status(config: Config) -> None:
    if not config.database.is_file():
        print(f"Agent Metrics is not initialized: {config.data_dir}")
        return
    connection = connect_database_readonly(config.database)
    try:
        events = connection.execute("select count(*) from events").fetchone()[0]
        minutes = connection.execute("select count(*) from minute_metrics").fetchone()[0]
        providers = [row[0] for row in connection.execute("select distinct provider from events order by provider")]
        last_sync = connection.execute("select value from metadata where key='last_sync_at'").fetchone()
        pending_files = int(metadata_value(connection, "pending_files") or 0)
        pending_bytes = int(metadata_value(connection, "pending_bytes") or 0)
        pending_live_files = int(metadata_value(connection, "pending_live_files") or 0)
        pending_backfill_files = int(
            metadata_value(connection, "pending_backfill_files") or 0
        )
    finally:
        connection.close()
    last = "never"
    if last_sync:
        last = datetime.fromtimestamp(int(last_sync[0]) / 1000, tz=timezone.utc).astimezone().isoformat()
    print(f"data: {config.data_dir}")
    print(f"events: {events}; minute rows: {minutes}; providers: {', '.join(providers) or 'none'}")
    print(
        f"pending: {pending_files} files / {pending_bytes} bytes "
        f"({pending_live_files} live, {pending_backfill_files} backfill)"
    )
    print(f"last sync: {last}")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def watch_interval(value: str) -> float:
    parsed = float(value)
    if parsed < 10:
        raise argparse.ArgumentTypeError("must be at least 10 seconds")
    return parsed


def sync_summary(summary: dict[str, int]) -> str:
    return (
        "Agent Metrics synced "
        f"{summary['files']} files ({summary['updated_files']} updated), "
        f"{summary['lines']} new lines ({summary['live_lines']} live, "
        f"{summary['backfill_lines']} backfill), {summary['events']} new events, "
        f"{summary['expired']} expired events; pending {summary['pending_files']} files / "
        f"{summary['pending_bytes']} bytes"
        f"; {summary.get('quota_observations', 0)} quota observations"
    )


def watch(config: Config, interval: float, max_lines: int) -> None:
    if interval < 10:
        raise ValueError("interval must be at least 10 seconds")
    if max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    with ingestion_lock(config.data_dir):
        inventory = SourceInventory()
        try:
            while True:
                started = time.monotonic()
                summary = sync(
                    config,
                    max_lines=max_lines,
                    inventory=inventory,
                    collector_locked=True,
                )
                elapsed = time.monotonic() - started
                print(f"{sync_summary(summary)}; elapsed {elapsed:.2f}s", flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("Agent Metrics watch stopped", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-metrics", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="create the private data directory and database")
    sync_parser = subparsers.add_parser(
        "sync", help="incrementally ingest local Claude Code and Codex logs"
    )
    sync_parser.add_argument(
        "--max-lines", type=positive_int, help="limit newly parsed JSONL lines"
    )
    watch_parser = subparsers.add_parser(
        "watch", help="run bounded sync cycles in the foreground"
    )
    watch_parser.add_argument(
        "--interval", type=watch_interval, default=DEFAULT_WATCH_INTERVAL
    )
    watch_parser.add_argument(
        "--max-lines", type=positive_int, default=DEFAULT_WATCH_MAX_LINES
    )
    serve_parser = subparsers.add_parser("serve", help="serve the local dashboard")
    serve_parser.add_argument("--bind")
    serve_parser.add_argument("--port", type=int)
    subparsers.add_parser("open", help="open the dashboard URL in the default browser")
    subparsers.add_parser("status", help="show local database status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir.expanduser()
    config = load_config(data_dir, args.config.expanduser() if args.config else None)
    if args.command == "init":
        initialize(config)
    elif args.command == "sync":
        print(sync_summary(sync(config, max_lines=args.max_lines)))
    elif args.command == "watch":
        watch(config, args.interval, args.max_lines)
    elif args.command == "serve":
        serve(config, args.bind, args.port)
    elif args.command == "open":
        validate_loopback(config.bind)
        token = load_or_create_ui_token(config.data_dir)
        webbrowser.open(f"http://{config.bind}:{config.port}/#token={token}")
    elif args.command == "status":
        status(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
