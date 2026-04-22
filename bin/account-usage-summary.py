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


def _latest_extra(email: str) -> tuple[float, float]:
    """Return (extra_used_credits, extra_pct) from the most recent poll for this email.

    Extra-usage credits are a per-account paid bucket that kicks in once the
    5h window caps. extra_pct=100 means the bucket is fully spent — so hitting
    the 5h cap on that account is a hard wall until it resets.
    """
    latest = None
    for rec in iter_history():
        if rec.get("email") != email:
            continue
        if latest is None or rec["ts"] > latest["ts"]:
            latest = rec
    if not latest:
        return 0.0, 0.0
    return float(latest.get("extra_used", 0) or 0), float(latest.get("extra_pct", 0) or 0)


def cmd_plan(args: argparse.Namespace) -> int:
    """Right-now recommendation with paid-credit awareness.

    Scoring model (for the next N hours):

        effective_headroom =
            5h_window_free_pct                           # primary capacity
          + paid_credit_headroom                         # post-cap fallback
          + reset_windfall_if_reset_within_window        # soon-to-refill bonus
          - hard_wall_penalty_if_extra_exhausted         # nothing left post-cap

    This fixes two bugs in the naive model:
      1. An account that resets in 30min was penalized as "no headroom" —
         wrong, it's about to have 100% headroom within the planning window.
      2. An account at 6% 5h-headroom but ZERO paid credits left was ranked
         above an account at 0% 5h-headroom with $500 of credits — wrong,
         the first hits a wall, the second keeps working.
    """
    now = datetime.now(timezone.utc)
    if not RESETS_FILE.exists():
        print(f"Need {RESETS_FILE} — run statusline a few times first.")
        return 1
    with RESETS_FILE.open() as fh:
        resets = json.load(fh)

    plan_hours = args.hours
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

        window_headroom = max(0.0, 100.0 - pct)
        extra_used, extra_pct = _latest_extra(email)
        extra_headroom = max(0.0, 100.0 - extra_pct)  # 0..100 of paid bucket remaining

        # Reset windfall: if the 5h window resets within the planning horizon,
        # we effectively recover 100% of window capacity for the remainder.
        # Scale by how much of the window falls after the reset.
        reset_windfall = 0.0
        if hrs_to_reset is not None and 0 <= hrs_to_reset < plan_hours:
            # Fraction of the plan window that lands AFTER the reset
            post_reset_frac = (plan_hours - hrs_to_reset) / plan_hours
            reset_windfall = 100.0 * post_reset_frac

        # Hard-wall penalty: if paid credits are fully spent AND 5h window is
        # near cap, this account is about to stop working entirely. Heavy weight.
        hard_wall = 0.0
        if extra_pct >= 99 and pct >= 90 and (hrs_to_reset is None or hrs_to_reset > 1.0):
            hard_wall = 100.0  # zero this account out

        # Composite score, normalized to 0..100 scale (roughly).
        score = window_headroom + 0.5 * extra_headroom + reset_windfall - hard_wall

        plan_rows.append(
            {
                "email": email,
                "tag": short_email(email),
                "pct": pct,
                "headroom": window_headroom,
                "extra_pct": extra_pct,
                "extra_headroom": extra_headroom,
                "hrs_to_reset": hrs_to_reset,
                "reset_windfall": reset_windfall,
                "hard_wall": hard_wall > 0,
                "score": max(0.0, score),
            }
        )

    plan_rows.sort(key=lambda r: -r["score"])

    print(f"Right-now plan (for the next ~{plan_hours}h)")
    print(f"  time: {now.astimezone().strftime('%a %H:%M %Z')}\n")
    hdr = (
        f"  {'rank':>4}  {'account':<10} {'5h-util':>7} {'extra':>7}"
        f" {'resets in':>10} {'windfall':>9}  score  note"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for i, r in enumerate(plan_rows, 1):
        reset_s = f"{r['hrs_to_reset']:.1f}h" if r["hrs_to_reset"] is not None else "—"
        wind_s = f"+{r['reset_windfall']:.0f}%" if r["reset_windfall"] > 0 else "—"
        note = "⚠ hard wall" if r["hard_wall"] else ""
        print(
            f"  {i:>4}  {r['tag']:<10} {r['pct']:>6.0f}% {r['extra_pct']:>6.0f}%"
            f" {reset_s:>10} {wind_s:>9}   {r['score']:>5.1f}  {note}"
        )

    best = plan_rows[0] if plan_rows else None
    if best:
        print()
        parts = []
        if best["headroom"] > 0:
            parts.append(f"{best['headroom']:.0f}% 5h-headroom")
        if best["reset_windfall"] > 0:
            parts.append(f"resets in {best['hrs_to_reset']:.1f}h")
        if best["extra_headroom"] > 0 and best["extra_pct"] > 0:
            parts.append(f"{best['extra_headroom']:.0f}% extra credits left")
        detail = ", ".join(parts) or "no capacity signal"
        if best["score"] >= 20:
            print(f"  → Use **{best['tag']}** next ({detail}).")
        else:
            soonest = min(
                (r for r in plan_rows if r["hrs_to_reset"] is not None),
                key=lambda r: r["hrs_to_reset"],
                default=None,
            )
            if soonest:
                print(
                    f"  ⚠ All accounts constrained. Soonest reset: {soonest['tag']} "
                    f"in {soonest['hrs_to_reset']:.1f}h"
                )
            else:
                print("  ⚠ All accounts constrained and no reset times known.")
    print()
    print("  5h-util  = current 5-hour window utilization (100% = capped)")
    print("  extra    = paid extra-usage credits spent (100% = bucket exhausted)")
    print("  windfall = headroom recovered within this plan window via 5h reset")
    print("  hard wall= at 5h cap AND paid credits exhausted → stops working")
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
