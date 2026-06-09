#!/usr/bin/env python3
"""Fold per-day, per-model token usage into a durable ledger.

token-scan-cache.json follows the live transcript set: when Claude Code's
retention deletes a session file, its requests drop out of the cache. This
ledger is the durable layer underneath — rows are only ever added or raised,
never removed, so the usage record survives transcript cleanup, storage-format
migrations, and cache rebuilds.

Reads every session and subagent JSONL under CLAUDE_DIR/projects, dedupes
assistant rows by message id (streaming writes one row per content block,
each repeating the usage payload), buckets by local calendar day and model,
and merges into CLAUDE_DIR/usage-ledger.json:

  {
    "version": 1,
    "updated_at": "2026-06-09T20:00:00+00:00",
    "days": {
      "2026-06-08": {
        "claude-opus-4-7": {"input": ..., "output": ..., "cache_write": ...,
                             "cache_write_1h": ..., "cache_read": ..., "messages": ...}
      }
    }
  }

Merge is monotone per (day, model): an existing row is replaced only when the
new observation carries at least as many total tokens, so a rescan after
files age out can never shrink history. cache_write is the full
cache_creation total; cache_write_1h is the 1-hour-TTL slice (5-minute slice
= cache_write - cache_write_1h).

Runs are self-throttled: if the ledger was written less than
USAGE_LEDGER_INTERVAL_H hours ago (default 6), exit immediately. --force
bypasses. This makes it safe to chain after scan-tokens.py in the 60s
launchd poll.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LEDGER_VERSION = 1
DEFAULT_INTERVAL_H = 6.0
TOKEN_FIELDS = ("input", "output", "cache_write", "cache_write_1h", "cache_read")


def local_day(ts: str) -> str | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d")


def session_files(projects_dir: Path):
    if not projects_dir.is_dir():
        return
    for project in projects_dir.iterdir():
        if not project.is_dir():
            continue
        yield from project.glob("*.jsonl")
        yield from project.rglob("agent-*.jsonl")


def scan(projects_dir: Path) -> dict[tuple[str, str], dict[str, int]]:
    """Deduped per-(day, model) sums. Keeps the max-output row per message id."""
    by_mid: dict[str, tuple[int, str, str, dict[str, int]]] = {}
    for path in session_files(projects_dir):
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                model = msg.get("model") or ""
                if not model or model == "<synthetic>":
                    continue
                day = local_day(rec.get("timestamp"))
                if day is None:
                    continue
                usage = msg.get("usage") or {}
                row = {
                    "input": usage.get("input_tokens", 0) or 0,
                    "output": usage.get("output_tokens", 0) or 0,
                    "cache_write": usage.get("cache_creation_input_tokens", 0) or 0,
                    "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                    "cache_write_1h": (usage.get("cache_creation") or {}).get(
                        "ephemeral_1h_input_tokens", 0
                    )
                    or 0,
                }
                mid = msg.get("id") or rec.get("uuid")
                key = row["input"] + row["output"]
                prev = by_mid.get(mid)
                if prev is None or key > prev[0]:
                    by_mid[mid] = (key, day, model, row)

    agg: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: dict.fromkeys((*TOKEN_FIELDS, "messages"), 0)
    )
    for _, day, model, row in by_mid.values():
        bucket = agg[(day, model)]
        for field in TOKEN_FIELDS:
            bucket[field] += row[field]
        bucket["messages"] += 1
    return agg


def row_total(row: dict[str, int]) -> int:
    # cache_write_1h is a slice of cache_write, not additional volume.
    return sum(row.get(f, 0) for f in ("input", "output", "cache_write", "cache_read"))


def load_ledger(path: Path) -> dict:
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(ledger, dict) and isinstance(ledger.get("days"), dict):
            return ledger
    except (OSError, ValueError):
        pass
    return {"version": LEDGER_VERSION, "updated_at": None, "days": {}}


def is_fresh(ledger: dict, interval_h: float) -> bool:
    try:
        updated = datetime.fromisoformat(ledger["updated_at"])
    except (KeyError, TypeError, ValueError):
        return False
    age_h = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
    return 0 <= age_h < interval_h


def merge(ledger: dict, scanned: dict[tuple[str, str], dict[str, int]]) -> int:
    """Apply monotone merge; returns the number of rows added or raised."""
    changed = 0
    days = ledger["days"]
    for (day, model), row in scanned.items():
        existing = days.setdefault(day, {}).get(model)
        if existing is None or row_total(row) >= row_total(existing):
            if existing != row:
                days[day][model] = row
                changed += 1
    return changed


def atomic_write(path: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude-dir", default=os.environ.get("CLAUDE_DIR", "~/.claude"))
    parser.add_argument("--ledger", help="ledger path (default: CLAUDE_DIR/usage-ledger.json)")
    parser.add_argument("--force", action="store_true", help="ignore the freshness throttle")
    parser.add_argument("--dry-run", action="store_true", help="scan and report, write nothing")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    claude_dir = Path(args.claude_dir).expanduser()
    ledger_path = Path(args.ledger).expanduser() if args.ledger else claude_dir / "usage-ledger.json"
    interval_h = float(os.environ.get("USAGE_LEDGER_INTERVAL_H", DEFAULT_INTERVAL_H))

    ledger = load_ledger(ledger_path)
    if not args.force and not args.dry_run and is_fresh(ledger, interval_h):
        if not args.quiet:
            print(f"usage-ledger: fresh (<{interval_h:g}h), skipping")
        return 0

    scanned = scan(claude_dir / "projects")
    changed = merge(ledger, scanned)

    if args.dry_run:
        print(f"usage-ledger: dry run — {changed} row(s) would change, "
              f"{len(scanned)} (day, model) row(s) scanned")
        return 0

    ledger["version"] = LEDGER_VERSION
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write(ledger_path, ledger)
    if not args.quiet:
        days = ledger["days"]
        span = f"{min(days)} -> {max(days)}" if days else "empty"
        print(f"usage-ledger: {changed} row(s) updated, {len(days)} day(s) [{span}], {ledger_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
