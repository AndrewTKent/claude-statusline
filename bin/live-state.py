#!/usr/bin/env python3
"""Live-state probe across Claude, Codex, and hound autobuild agents.

The "live state" half of the two-layer architecture (see bin/ARCHITECTURE.md).
Snapshot only — for historical attribution, see scan_tokens_core.py.

Reads (when present):
  - ~/.claude/account-resets.json      → Claude 5h/7d utilization per account
  - ~/.codex/state_5.sqlite            → Codex token usage (last 5h)
  - ~/.hound-mcp/sessions.jsonl        → hound agent session activity

Modes:
  default    KEY=VALUE pairs, one per line, for `eval $(...)` in bash
  --render   single human-readable statusline row
  --json     JSON object

The 5h window is anchored to "now" (UTC), not to plan-reset boundaries.
ChatGPT-plan quotas don't map cleanly to a token count; codex_5h_tokens is
a raw activity gauge, not a quota percentage.
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
CODEX_STATE = HOME / ".codex" / "state_5.sqlite"
HOUND_SESSIONS = HOME / ".hound-mcp" / "sessions.jsonl"

WINDOW_5H_SECS = 5 * 60 * 60


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
    # Codex
    if CODEX_STATE.is_file():
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
            state["codex"] = {
                "threads_5h": threads,
                "tokens_5h": tokens,
                "last_active_epoch": last_update,
            }
        except sqlite3.Error:
            pass
    # Hound
    if HOUND_SESSIONS.is_file():
        try:
            size = HOUND_SESSIONS.stat().st_size
            with HOUND_SESSIONS.open("rb") as f:
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
                state["hound"] = {
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
        emit("codex_5h_threads", c.get("threads_5h"))
        emit("codex_5h_tokens", c.get("tokens_5h"))
        if c.get("last_active_epoch"):
            emit("codex_last_active_epoch", c["last_active_epoch"])
    if "hound" in state:
        for agent, ev in state["hound"].items():
            prefix = f"hound_{agent}"
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
        parts.append(f"codex 5h: {c['threads_5h']}t / {fmt_tokens(int(c['tokens_5h']))}")
    if "hound" in state:
        agents_in_order = ["build", "strategist", "hardware", "infra"]
        agent_parts: list[str] = []
        for a in agents_in_order:
            ev = state["hound"].get(a)
            if not ev or ev.get("session") is None:
                continue
            marker = "•" if ev.get("event") == "start" else " "
            short = {"build": "b", "strategist": "s", "hardware": "hw", "infra": "i"}[a]
            agent_parts.append(f"{marker}{short}#{ev['session']}")
        if agent_parts:
            parts.append("hound: " + " ".join(agent_parts))
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
