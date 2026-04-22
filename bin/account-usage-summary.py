#!/usr/bin/env python3
"""Summarize per-account rate-limit usage patterns to plan your day.

Reads: ~/.claude/utilization-history.jsonl (append-only log written by the
usage poller in statusline.sh).

What it answers
---------------
- "When does each account typically have headroom?"
- "Which account is best to use right now, and for how long?"
- "How fast does each account burn from empty → cap?"
- "How much daily 5h-window capacity does each account give me?"

Two sub-commands:

    summary  — per-account rollup: typical burn rate, typical cap hour, idle hours
    plan     — right-now recommendation: which account to use for next N hours
    hourly   — per-hour-of-day median utilization heatmap (weekday/weekend split)

All reads are safe against partial/malformed JSONL lines (just skipped).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Iterator

HIST_FILE = Path.home() / ".claude" / "utilization-history.jsonl"
RESETS_FILE = Path.home() / ".claude" / "account-resets.json"


def iter_history(path: Path = HIST_FILE, since_ts: int | None = None) -> Iterator[dict]:
    """Yield history records; skip malformed lines silently."""
    if not path.exists():
        return
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_ts is not None and rec.get("ts", 0) < since_ts:
                continue
            yield rec


def local_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()


def short_email(email: str) -> str:
    """user@example.edu → alumni, andrew.kent@acme.ai → acme."""
    domain = email.split("@", 1)[-1].lower()
    if "acme" in domain:
        return "acme"
    if "alumni" in domain or "alumni" in domain:
        return "alumni"
    if "gmail" in domain:
        return "personal"
    return domain.split(".")[0]


def cmd_summary(args: argparse.Namespace) -> int:
    since = int((datetime.now().timestamp() - args.days * 86400))
    by_email: dict[str, list[dict]] = defaultdict(list)
    for rec in iter_history(since_ts=since):
        by_email[rec.get("email", "?")].append(rec)

    if not by_email:
        print(f"No history in the last {args.days} days. Log file: {HIST_FILE}")
        return 1

    print(f"Per-account summary over the last {args.days} days")
    print(f"  source: {HIST_FILE} ({sum(len(v) for v in by_email.values())} polls)\n")

    rows = []
    for email, recs in by_email.items():
        recs.sort(key=lambda r: r["ts"])
        # per 5h-window pattern: find segments where five_hour_pct resets
        # (drops by >= 30 pct). Between resets we have a "burn curve."
        pcts = [r.get("five_hour_pct", 0) or 0 for r in recs]
        # hit_cap count: polls at ≥99%
        cap_hits = sum(1 for p in pcts if p >= 99)
        total = len(pcts)
        # burn rate: average pct/hr during non-cap segments
        burn_samples = []
        for a, b in zip(recs, recs[1:]):
            dt = b["ts"] - a["ts"]
            if dt < 30 or dt > 3600:
                continue
            dp = (b.get("five_hour_pct", 0) or 0) - (a.get("five_hour_pct", 0) or 0)
            if dp <= 0 or dp > 50:  # reset or noise
                continue
            burn_samples.append(dp / (dt / 3600.0))
        burn_median = median(burn_samples) if burn_samples else 0.0
        # hours spent capped
        capped_hours = sum(
            (b["ts"] - a["ts"]) / 3600.0
            for a, b in zip(recs, recs[1:])
            if (a.get("five_hour_pct", 0) or 0) >= 99
            and (b["ts"] - a["ts"]) <= 3600
        )
        # window coverage: what fraction of wall-clock did we have data?
        span_h = max(0.001, (recs[-1]["ts"] - recs[0]["ts"]) / 3600.0)
        coverage = min(100, int(100 * total * 60 / max(1, span_h * 3600)))  # rough — 60s poll

        rows.append(
            {
                "email": email,
                "tag": short_email(email),
                "polls": total,
                "cap_pct": round(100 * cap_hits / total, 0) if total else 0,
                "burn": burn_median,
                "capped_hours": capped_hours,
                "span_h": span_h,
                "coverage": coverage,
                "first_ts": recs[0]["ts"],
                "last_ts": recs[-1]["ts"],
            }
        )

    rows.sort(key=lambda r: -r["polls"])
    hdr = f"  {'account':<10} {'polls':>6} {'span':>7} {'@cap':>6} {'burn/hr':>9} {'capped':>9}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        print(
            f"  {r['tag']:<10} {r['polls']:>6} {r['span_h']:>6.1f}h {r['cap_pct']:>5.0f}%"
            f" {r['burn']:>7.1f}%/h {r['capped_hours']:>7.1f}h"
        )
    print()
    print("  @cap    = % of polls at ≥99% five-hour utilization")
    print("  burn/hr = median 5h-util gained per hour while actively burning")
    print("  capped  = cumulative hours spent at the 5h cap (gated)")
    return 0


def cmd_hourly(args: argparse.Namespace) -> int:
    """Per-hour-of-day median util, weekday vs weekend."""
    since = int((datetime.now().timestamp() - args.days * 86400))
    # email -> (weekday|weekend) -> hour -> [pcts]
    buckets: dict[str, dict[str, dict[int, list[float]]]] = defaultdict(
        lambda: {"weekday": defaultdict(list), "weekend": defaultdict(list)}
    )
    for rec in iter_history(since_ts=since):
        dt = local_dt(rec["ts"])
        day = "weekend" if dt.weekday() >= 5 else "weekday"
        buckets[rec.get("email", "?")][day][dt.hour].append(
            rec.get("five_hour_pct", 0) or 0
        )

    if not buckets:
        print(f"No history in the last {args.days} days. Log file: {HIST_FILE}")
        return 1

    for email, by_day in buckets.items():
        tag = short_email(email)
        for day_type in ("weekday", "weekend"):
            hours = by_day[day_type]
            if not hours:
                continue
            print(f"\n{tag:<10} {day_type} (last {args.days}d)")
            print("  hour  median 5h-util  samples  bar")
            for h in range(24):
                samples = hours.get(h, [])
                if not samples:
                    continue
                med = median(samples)
                bar_width = int(med / 5)
                bar = "█" * bar_width + "░" * (20 - bar_width)
                print(f"  {h:02d}:00 {med:>8.0f}%       {len(samples):>5d}   {bar}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Right-now recommendation: which account has the most headroom for next N hours."""
    now = datetime.now(timezone.utc)
    if not RESETS_FILE.exists():
        print(f"Need {RESETS_FILE} — run statusline a few times first.")
        return 1
    with RESETS_FILE.open() as fh:
        resets = json.load(fh)

    # Build per-account state: pct, reset, projected reset (in 5h increments)
    plan_rows = []
    for email, info in resets.items():
        pct = info.get("five_hour_pct", 0) or 0
        reset_iso = info.get("five_hour_reset")
        reset_dt = None
        if reset_iso:
            try:
                reset_dt = datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
                while reset_dt <= now:
                    reset_dt += timedelta(hours=5)
            except ValueError:
                pass
        hrs_to_reset = (reset_dt - now).total_seconds() / 3600.0 if reset_dt else None
        headroom_pct = max(0, 100 - pct)
        plan_rows.append(
            {
                "email": email,
                "tag": short_email(email),
                "pct": pct,
                "headroom": headroom_pct,
                "hrs_to_reset": hrs_to_reset,
                "reset_dt": reset_dt,
            }
        )

    # Score: prefer high headroom, but penalize accounts close to reset
    # (a reset soon means your "headroom" is about to refill anyway — use the
    # other account first, save this one for after its reset).
    for r in plan_rows:
        r["score"] = r["headroom"] * (
            1.0 if r["hrs_to_reset"] is None else max(0.1, min(1.0, r["hrs_to_reset"] / 5.0))
        )
    plan_rows.sort(key=lambda r: -r["score"])

    print(f"Right-now plan (for the next ~{args.hours}h)")
    print(f"  time: {now.astimezone().strftime('%a %H:%M %Z')}\n")
    hdr = f"  {'rank':>4}  {'account':<10} {'util':>6} {'headroom':>9} {'resets in':>10}  score"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for i, r in enumerate(plan_rows, 1):
        reset_s = f"{r['hrs_to_reset']:.1f}h" if r["hrs_to_reset"] is not None else "—"
        print(
            f"  {i:>4}  {r['tag']:<10} {r['pct']:>5.0f}% {r['headroom']:>8.0f}%"
            f" {reset_s:>10}   {r['score']:>5.1f}"
        )

    best = plan_rows[0] if plan_rows else None
    if best:
        print()
        if best["headroom"] >= 20:
            print(
                f"  → Use **{best['tag']}** next (headroom {best['headroom']:.0f}%, resets in"
                f" {best['hrs_to_reset']:.1f}h)." if best['hrs_to_reset'] is not None
                else f"  → Use **{best['tag']}** next (headroom {best['headroom']:.0f}%)."
            )
        else:
            print(f"  ⚠ All accounts near cap. Soonest reset: {plan_rows[0]['tag']}"
                  f" in {plan_rows[0]['hrs_to_reset']:.1f}h")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("summary", help="per-account rollup")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(func=cmd_summary)

    h = sub.add_parser("hourly", help="per-hour-of-day utilization heatmap")
    h.add_argument("--days", type=int, default=14)
    h.set_defaults(func=cmd_hourly)

    pl = sub.add_parser("plan", help="right-now recommendation")
    pl.add_argument("--hours", type=float, default=2.0)
    pl.set_defaults(func=cmd_plan)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
