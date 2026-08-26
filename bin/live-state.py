#!/usr/bin/env python3
"""Live-state probe across Claude, Codex, and remote autobuild agents.

The "live state" half of the two-layer architecture (see bin/ARCHITECTURE.md).
Snapshot only — for historical attribution, see scan_tokens_core.py.

Reads (in this preference order):
  - ~/.claude/token-scan-summary.json  → engine-derived Codex rate_limit (auth)
  - ~/.claude/account-resets.json      → Claude 5h/7d utilization per account
  - ~/.codex/state_N.sqlite (highest)  → Codex token-count fallback when no engine summary
  - $AGENT_SESSIONS_PATH               → remote agent session activity (opt-in)

Modes:
  default    KEY=VALUE pairs, one per line, for `eval $(...)` in bash
  --render   single human-readable statusline row
  --json     JSON object

When the historical engine has scanned Codex sessions, codex utilization comes
from the actual rate_limits payload Codex emits (used_percent + resets_at).
Without it, falls back to a raw 5h token sum from the sqlite state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path


HOME = Path(os.environ.get("HOME", os.path.expanduser("~")))
CLAUDE_RESETS = HOME / ".claude" / "account-resets.json"
CLAUDE_TOKEN_SUMMARY = HOME / ".claude" / "token-scan-summary.json"
AGENT_METRICS_DB = Path(
    os.environ.get(
        "AGENT_METRICS_DB",
        str(HOME / "Library/Application Support/statusline/agent-metrics/metrics.sqlite3"),
    )
)


def resolve_state_db(codex_home: Path) -> Path:
    """Highest-versioned state_N.sqlite; falls back to state_5 if none exist yet."""
    best_version, best_path = -1, codex_home / "state_5.sqlite"
    for path in codex_home.glob("state_*.sqlite"):
        try:
            version = int(path.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if version > best_version:
            best_version, best_path = version, path
    return best_path


CODEX_STATE = resolve_state_db(HOME / ".codex")
# Opt-in: point at a remote agent runner's session log to get the agents row.
_agent_sessions = os.environ.get("AGENT_SESSIONS_PATH", "").strip()
AGENT_SESSIONS = Path(_agent_sessions).expanduser() if _agent_sessions else None

WINDOW_5H_SECS = 5 * 60 * 60


def rate_limit_window_label(window_minutes: object, fallback: str) -> str:
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


def labeled_rate_limits(rate_limits: dict) -> list[tuple[str, dict]]:
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


def codex_state_from_metrics(path: Path, max_age_s: int = 900) -> dict | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1)
        rows = connection.execute(
            "select tool_name, quota_window_minutes, quota_used_percent, "
            "quota_resets_at, timestamp from events "
            "where provider='codex' and event_kind='quota' "
            "order by timestamp desc limit 20"
        ).fetchall()
        session_count = connection.execute(
            "select count(distinct session_id) from events "
            "where provider='codex' and session_id!=''"
        ).fetchone()[0]
        connection.close()
    except sqlite3.Error:
        return None
    if not rows or int(time.time() * 1000) - int(rows[0][4]) > max_age_s * 1000:
        return None
    limits = []
    seen = set()
    for name, window_minutes, used_percent, resets_at, _timestamp in rows:
        if name in seen or used_percent is None:
            continue
        seen.add(name)
        label = rate_limit_window_label(window_minutes, "5-hour" if name == "primary" else "weekly")
        limit = {
            "label": label,
            "used_percent": used_percent,
            "resets_at": resets_at / 1000 if resets_at is not None else None,
        }
        limits.append(limit)
    if not limits:
        return None
    result = {"rate_limits": limits, "session_count": session_count}
    for limit in limits:
        if limit["label"] == "5-hour":
            result["rate_limit_5h_pct"] = limit["used_percent"]
            result["rate_limit_5h_resets_at"] = limit["resets_at"]
        elif limit["label"] == "weekly":
            result["rate_limit_7d_pct"] = limit["used_percent"]
            result["rate_limit_7d_resets_at"] = limit["resets_at"]
    return result


def emit(key: str, value: object) -> None:
    """Print a shell-safe KEY=VALUE line. Skips empty values."""
    if value is None or value == "":
        return
    s = str(value).replace("'", "")  # crude shell quoting; values here are numeric/iso
    print(f"{key}={s}")


def collect() -> dict:
    """Return all tracking data as a single dict; empty values omitted."""
    state: dict = {}
    # Claude
    if CLAUDE_RESETS.is_file():
        try:
            data = json.loads(CLAUDE_RESETS.read_text())
            worst = max(
                ((float(v.get("five_hour_pct", 0)), k, v) for k, v in data.items()),
                default=None,
                key=lambda t: t[0],
            )
            if worst:
                pct, email, acct = worst
                state["claude"] = {
                    "worst_5h_pct": int(pct),
                    "worst_5h_account": email,
                    "worst_5h_reset": str(acct.get("five_hour_reset", "")),
                    "account_count": len(data),
                }
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    # Codex — prefer the engine's authoritative rate_limit payload when available.
    codex_state = codex_state_from_metrics(AGENT_METRICS_DB)
    if codex_state is None and CLAUDE_TOKEN_SUMMARY.is_file():
        try:
            summary = json.loads(CLAUDE_TOKEN_SUMMARY.read_text())
            codex_block = summary.get("codex") or {}
            rl = codex_block.get("rate_limit") or {}
            limits = labeled_rate_limits(rl)
            if limits:
                codex_state = {
                    "rate_limits": [
                        {
                            "label": label,
                            "used_percent": limit.get("used_percent"),
                            "resets_at": limit.get("resets_at"),
                        }
                        for label, limit in limits
                    ],
                    "plan_type": rl.get("plan_type"),
                    "session_count": codex_block.get("session_count", 0),
                }
                for label, limit in limits:
                    if label == "5-hour":
                        codex_state["rate_limit_5h_pct"] = limit.get("used_percent")
                        codex_state["rate_limit_5h_resets_at"] = limit.get("resets_at")
                    elif label == "weekly":
                        codex_state["rate_limit_7d_pct"] = limit.get("used_percent")
                        codex_state["rate_limit_7d_resets_at"] = limit.get("resets_at")
        except (OSError, json.JSONDecodeError):
            pass
    # Fallback: raw 5h token sum from sqlite when engine summary is missing.
    if codex_state is None and CODEX_STATE.is_file():
        cutoff = int(time.time()) - WINDOW_5H_SECS
        try:
            conn = sqlite3.connect(f"file:{CODEX_STATE}?mode=ro", uri=True, timeout=2)
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(tokens_used),0), COALESCE(MAX(updated_at),0) "
                "FROM threads WHERE updated_at >= ?",
                (cutoff,),
            ).fetchone()
            conn.close()
            threads, tokens, last_update = row or (0, 0, 0)
            codex_state = {
                "threads_5h": threads,
                "tokens_5h": tokens,
                "last_active_epoch": last_update,
            }
        except sqlite3.Error:
            pass
    if codex_state is not None:
        state["codex"] = codex_state
    # Remote agents
    if AGENT_SESSIONS is not None and AGENT_SESSIONS.is_file():
        try:
            size = AGENT_SESSIONS.stat().st_size
            with AGENT_SESSIONS.open("rb") as f:
                if size > 65536:
                    f.seek(size - 65536)
                    f.readline()
                tail = f.read().decode("utf-8", errors="replace")
            latest: dict[str, dict] = {}
            for line in tail.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                agent = ev.get("agent")
                if agent:
                    latest[agent] = ev
            if latest:
                state["agents"] = {
                    agent: {
                        "session": ev.get("session_num"),
                        "event": ev.get("event"),
                        "ts": ev.get("ts"),
                        "runtime": ev.get("runtime"),
                    }
                    for agent, ev in latest.items()
                }
        except OSError:
            pass
    return state


def render_kv(state: dict) -> None:
    if "claude" in state:
        c = state["claude"]
        emit("claude_worst_5h_pct", c.get("worst_5h_pct"))
        emit("claude_worst_5h_account", c.get("worst_5h_account"))
        emit("claude_worst_5h_reset", c.get("worst_5h_reset"))
        emit("claude_account_count", c.get("account_count"))
    if "codex" in state:
        c = state["codex"]
        for index, limit in enumerate(c.get("rate_limits") or [], start=1):
            emit(f"codex_rate_limit_{index}_label", limit.get("label"))
            emit(f"codex_rate_limit_{index}_pct", limit.get("used_percent"))
            emit(f"codex_rate_limit_{index}_resets_at", limit.get("resets_at"))
        # Engine-derived (preferred)
        emit("codex_rate_limit_5h_pct", c.get("rate_limit_5h_pct"))
        emit("codex_rate_limit_5h_resets_at", c.get("rate_limit_5h_resets_at"))
        emit("codex_rate_limit_7d_pct", c.get("rate_limit_7d_pct"))
        emit("codex_rate_limit_7d_resets_at", c.get("rate_limit_7d_resets_at"))
        emit("codex_plan_type", c.get("plan_type"))
        emit("codex_session_count", c.get("session_count"))
        # Sqlite fallback
        emit("codex_5h_threads", c.get("threads_5h"))
        emit("codex_5h_tokens", c.get("tokens_5h"))
        if c.get("last_active_epoch"):
            emit("codex_last_active_epoch", c["last_active_epoch"])
    if "agents" in state:
        for agent, ev in state["agents"].items():
            prefix = f"agent_{agent}"
            emit(f"{prefix}_session", ev.get("session"))
            emit(f"{prefix}_event", ev.get("event"))
            emit(f"{prefix}_ts", ev.get("ts"))
            emit(f"{prefix}_runtime", ev.get("runtime"))


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def render_line(state: dict) -> str:
    parts: list[str] = []
    if "claude" in state:
        c = state["claude"]
        parts.append(f"claude 5h: {c['worst_5h_pct']}%")
    if "codex" in state:
        c = state["codex"]
        limits = c.get("rate_limits") or []
        if limits:
            rendered_limits = []
            for limit in limits:
                label = {"5-hour": "5h", "weekly": "7d"}.get(limit["label"], limit["label"])
                used_percent = int(round(float(limit["used_percent"])))
                rendered_limits.append(f"{label}: {used_percent}%")
            parts.append("codex " + " · ".join(rendered_limits))
        elif c.get("rate_limit_5h_pct") is not None:
            five = int(round(float(c["rate_limit_5h_pct"])))
            week = c.get("rate_limit_7d_pct")
            if week is not None:
                parts.append(f"codex 5h: {five}% · 7d: {int(round(float(week)))}%")
            else:
                parts.append(f"codex 5h: {five}%")
        else:
            parts.append(
                f"codex 5h: {c.get('threads_5h', 0)}t / {fmt_tokens(int(c.get('tokens_5h', 0)))}"
            )
    if "agents" in state:
        agents_in_order = ["build", "strategist", "hardware", "infra"]
        agent_parts: list[str] = []
        for a in agents_in_order:
            ev = state["agents"].get(a)
            if not ev or ev.get("session") is None:
                continue
            marker = "•" if ev.get("event") == "start" else " "
            short = {"build": "b", "strategist": "s", "hardware": "hw", "infra": "i"}[a]
            agent_parts.append(f"{marker}{short}#{ev['session']}")
        if agent_parts:
            parts.append("agents: " + " ".join(agent_parts))
    return "  ·  ".join(parts)


def main() -> int:
    state = collect()
    if "--json" in sys.argv:
        print(json.dumps(state, separators=(",", ":")))
    elif "--render" in sys.argv:
        line = render_line(state)
        if line:
            print(line)
    else:
        render_kv(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
