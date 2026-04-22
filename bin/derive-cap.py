#!/usr/bin/env python3
"""Derive per-account 5-hour token caps from observed (util, token_spend) pairs.

Reads:
  ~/.claude/utilization-history.jsonl   (appended by statusline.sh per poll)
  ~/.claude/projects/**/*.jsonl         (for per-window token sums)

Writes:
  ~/.claude/account-caps.json  — {email: {cap, confidence, n_points, formula, ...}}

Method
------
For each account, group history entries by 5h window (keyed by five_hour_reset).
Within a window, utilization is strictly increasing (monotonic) until reset.
Each (util_delta, token_delta) pair between consecutive polls gives one
observation of "Δutil for Δtokens," which is linear in the cap:

    Δutil / 100 = Δtokens_effective / cap

We don't yet know which tokens count — Anthropic weights input/output/cache_read
differently. So we regress Δutil against multiple candidate "effective token"
formulas and pick the one with the tightest fit (lowest relative residual).

The OAuth utilization value is INTEGER % — ±0.5% quantization. We weight each
observation by its Δutil (larger deltas = less quantization noise) and skip
observations with Δutil == 0.

Output
------
Per account:
  {
    "cap_tokens": int,              # best-fit 5h cap in effective tokens
    "formula": "in+out+cr",         # which candidate won
    "r_squared": 0.87,              # fit quality
    "n_points": 42,                 # regression sample size
    "remaining_tokens_est": 98_000_000,  # cap × (1 - current_util/100)
    "last_updated": 1776838571
  }
"""
from __future__ import annotations

import json
import os
import sys
import math
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
PROJECTS_DIR = CLAUDE_DIR / "projects"
HISTORY_FILE = CLAUDE_DIR / "utilization-history.jsonl"
CAPS_FILE = CLAUDE_DIR / "account-caps.json"
RESETS_FILE = CLAUDE_DIR / "account-resets.json"
SESSION_ACCOUNTS_FILE = CLAUDE_DIR / "session-accounts.json"


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_history() -> list[dict]:
    rows = []
    if not HISTORY_FILE.exists():
        return rows
    with open(HISTORY_FILE) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_session_email_map() -> dict[str, list[tuple[str, str, str]]]:
    """session-accounts.json → {sid: [(from_ts, to_ts, email), ...]}."""
    if not SESSION_ACCOUNTS_FILE.exists():
        return {}
    try:
        raw = json.loads(SESSION_ACCOUNTS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    result = {}
    for sid, info in raw.items():
        if not isinstance(info, dict):
            continue
        spans = []
        for span in info.get("spans") or []:
            if not isinstance(span, dict):
                continue
            email = (span.get("email") or "").lower()
            spans.append((span.get("from"), span.get("to"), email))
        if spans:
            spans.sort(key=lambda s: s[0] or "")
            result[sid] = spans
    return result


def email_for(spans: list, ts: str) -> str | None:
    for frm, to, email in spans:
        if frm and ts < frm:
            continue
        if to and ts > to:
            continue
        return email
    return None


def scan_window_spend(window_start: datetime, window_end: datetime,
                      email_spans: dict) -> dict[str, dict[str, int]]:
    """Walk JSONLs; return {email: {inp,out,cr,cc,reqs}} for the window."""
    out = defaultdict(lambda: {"inp": 0, "out": 0, "cr": 0, "cc": 0, "reqs": 0})
    start_ts = window_start.timestamp()
    end_ts = window_end.timestamp()
    seen = set()
    for jf in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            st = jf.stat()
        except OSError:
            continue
        # Skip files clearly outside window (allow 1h buffer for clock skew)
        if st.st_mtime < start_ts - 3600:
            continue
        sid = jf.stem
        spans = email_spans.get(sid, [])
        try:
            with open(jf) as f:
                for line in f:
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = j.get("timestamp")
                    if not isinstance(ts_str, str):
                        continue
                    tsv = parse_iso(ts_str)
                    if not tsv or not (window_start <= tsv <= window_end):
                        continue
                    msg = j.get("message") or {}
                    usage = msg.get("usage") if isinstance(msg, dict) else None
                    if not usage:
                        continue
                    rid = j.get("requestId") or msg.get("id")
                    if not rid or rid in seen:
                        continue
                    seen.add(rid)
                    email = email_for(spans, ts_str) or "unknown"
                    bucket = out[email]
                    bucket["inp"] += usage.get("input_tokens", 0) or 0
                    bucket["out"] += usage.get("output_tokens", 0) or 0
                    bucket["cr"] += usage.get("cache_read_input_tokens", 0) or 0
                    bucket["cc"] += usage.get("cache_creation_input_tokens", 0) or 0
                    bucket["reqs"] += 1
        except OSError:
            continue
    return dict(out)


# Candidate formulas: (name, fn(inp, out, cr, cc))
FORMULAS = [
    ("in+out",          lambda i, o, cr, cc: i + o),
    ("in+out+cr",       lambda i, o, cr, cc: i + o + cr),
    ("in+out+cr/10",    lambda i, o, cr, cc: i + o + cr / 10),
    ("in+out+cr+cc",    lambda i, o, cr, cc: i + o + cr + cc),
    ("out+cr/10",       lambda i, o, cr, cc: o + cr / 10),
    ("out+cc+cr/10",    lambda i, o, cr, cc: o + cc + cr / 10),
    ("out*5+cr/10",     lambda i, o, cr, cc: o * 5 + cr / 10),
]


def fit_cap(samples: list[tuple[float, float]]) -> tuple[float, float]:
    """samples = [(delta_util_pct, delta_effective_tokens), ...].

    Model: delta_util / 100 = delta_tokens / cap
        → cap = delta_tokens / (delta_util / 100)

    Do weighted least-squares regression through origin. Weights = delta_util
    (larger deltas are less affected by integer-quantization noise).

    Returns (cap, r_squared).
    """
    if not samples:
        return 0.0, 0.0
    # y = x / cap_fraction; cap_fraction = delta_util/100 per token.
    # Better: regress delta_tokens = cap * (delta_util/100)
    # Let X = delta_util/100, Y = delta_tokens. Slope = cap.
    X = [du / 100.0 for du, _ in samples]
    Y = [dt for _, dt in samples]
    W = [du for du, _ in samples]  # weight by util delta

    sum_w = sum(W)
    if sum_w == 0:
        return 0.0, 0.0
    sum_wxy = sum(w * x * y for w, x, y in zip(W, X, Y))
    sum_wxx = sum(w * x * x for w, x in zip(W, X))
    if sum_wxx == 0:
        return 0.0, 0.0
    cap = sum_wxy / sum_wxx  # slope through origin (weighted)

    # R² through origin: 1 - SS_res / SS_tot_origin
    ss_res = sum(w * (y - cap * x) ** 2 for w, x, y in zip(W, X, Y))
    ss_tot = sum(w * y * y for w, y in zip(W, Y))
    r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return cap, max(0.0, min(1.0, r2))


def bucket_window_tokens_by_ts(window_start: datetime, window_end: datetime
                               ) -> list[tuple[float, int, int, int, int]]:
    """Return per-request (ts_epoch, inp, out, cr, cc) in the window, sorted by ts.

    This lets us compute exact (inp, out, cr, cc) sums between any two poll
    timestamps — crucial for the Δutil / Δtokens regression within a window.
    """
    seen = set()
    events = []
    start_ts = window_start.timestamp()
    end_ts = window_end.timestamp()
    for jf in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            st = jf.stat()
        except OSError:
            continue
        if st.st_mtime < start_ts - 3600:
            continue
        try:
            with open(jf) as f:
                for line in f:
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = j.get("timestamp")
                    if not isinstance(ts_str, str):
                        continue
                    tsv = parse_iso(ts_str)
                    if not tsv or not (window_start <= tsv <= window_end):
                        continue
                    msg = j.get("message") or {}
                    usage = msg.get("usage") if isinstance(msg, dict) else None
                    if not usage:
                        continue
                    rid = j.get("requestId") or msg.get("id")
                    if not rid or rid in seen:
                        continue
                    seen.add(rid)
                    events.append((
                        tsv.timestamp(),
                        usage.get("input_tokens", 0) or 0,
                        usage.get("output_tokens", 0) or 0,
                        usage.get("cache_read_input_tokens", 0) or 0,
                        usage.get("cache_creation_input_tokens", 0) or 0,
                    ))
        except OSError:
            continue
    events.sort(key=lambda e: e[0])
    return events


def tokens_in_range(events: list, t0: float, t1: float) -> tuple[int, int, int, int]:
    inp = out = cr = cc = 0
    for ts, i, o, r, c in events:
        if ts < t0:
            continue
        if ts > t1:
            break
        inp += i; out += o; cr += r; cc += c
    return inp, out, cr, cc


def derive_per_account() -> dict[str, dict]:
    history = load_history()
    if not history:
        return {}

    # Group history by (email, five_hour_reset_rounded_to_minute) → sorted
    # [(ts, util)]. The reset ISO has microsecond jitter per poll that
    # would otherwise split every poll into its own "window."
    def window_key(reset_iso: str) -> str:
        dt = parse_iso(reset_iso)
        if not dt:
            return reset_iso
        # Round to the nearest 5-minute boundary (resets are clocked at the
        # top of the hour ± a few seconds).
        return dt.strftime("%Y-%m-%dT%H:00")

    window_map: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    window_reset_dt: dict[tuple[str, str], datetime] = {}
    for row in history:
        email = row.get("email")
        reset_iso = row.get("five_hour_reset")
        ts = row.get("ts")
        util = row.get("five_hour_pct")
        if not (email and reset_iso and ts is not None and util is not None):
            continue
        key = window_key(reset_iso)
        window_map[(email, key)].append((int(ts), float(util)))
        # Track the actual reset datetime for this window (any row's is fine)
        if (email, key) not in window_reset_dt:
            rdt = parse_iso(reset_iso)
            if rdt:
                window_reset_dt[(email, key)] = rdt

    # For each window: walk consecutive poll pairs, emit (Δutil, Δtokens) per
    # candidate formula. Skip pairs with Δutil ≤ 0 (saturated / same integer).
    samples_per_formula: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))

    for key, points in window_map.items():
        email, _ = key
        reset_dt = window_reset_dt.get(key)
        if not reset_dt:
            continue
        window_start = datetime.fromtimestamp(reset_dt.timestamp() - 5 * 3600, tz=timezone.utc)
        window_end = reset_dt
        events = bucket_window_tokens_by_ts(window_start, window_end)
        if not events:
            continue

        pts = sorted(points, key=lambda p: p[0])
        # Walk consecutive polls
        for (t0, u0), (t1, u1) in zip(pts, pts[1:]):
            du = u1 - u0
            if du <= 0:
                continue
            inp, out, cr, cc = tokens_in_range(events, float(t0), float(t1))
            if inp + out + cr + cc == 0:
                continue
            for name, fn in FORMULAS:
                eff = fn(inp, out, cr, cc)
                if eff <= 0:
                    continue
                samples_per_formula[email][name].append((du, eff))

    # Require at least N=4 samples per email before emitting a cap (avoids
    # overfitting on single-window/single-poll noise). Pick formula by R².
    MIN_SAMPLES = 4
    out = {}
    for email, by_formula in samples_per_formula.items():
        candidates = []
        for name, samples in by_formula.items():
            if len(samples) < MIN_SAMPLES:
                continue
            cap, r2 = fit_cap(samples)
            if cap <= 0:
                continue
            candidates.append((name, cap, r2, len(samples)))
        if not candidates:
            # Emit a "pending" stub showing how much data we've got so far
            for name, samples in by_formula.items():
                cap, _ = fit_cap(samples)
                out[email] = {
                    "status": "insufficient_data",
                    "n_points": len(samples),
                    "min_required": MIN_SAMPLES,
                    "last_updated": int(datetime.now().timestamp()),
                }
                break
            continue
        # Pick highest R²; tie-break by sample size
        candidates.sort(key=lambda c: (c[2], c[3]), reverse=True)
        name, cap, r2, n = candidates[0]
        out[email] = {
            "status": "ok",
            "formula": name,
            "cap_tokens": int(cap),
            "r_squared": round(r2, 3),
            "n_points": n,
            "last_updated": int(datetime.now().timestamp()),
        }

    # Enrich with "remaining" = cap × (1 - current_util / 100) where we have a cap.
    if RESETS_FILE.exists():
        try:
            resets = json.loads(RESETS_FILE.read_text())
            for email, rec in out.items():
                cur = resets.get(email)
                if not cur:
                    continue
                util = cur.get("five_hour_pct") or 0
                rec["current_util"] = util
                if rec.get("status") == "ok" and rec.get("cap_tokens"):
                    rec["remaining_tokens_est"] = max(0, int(rec["cap_tokens"] * (1 - util / 100)))
        except (OSError, json.JSONDecodeError):
            pass

    return out


def main():
    caps = derive_per_account()
    if not caps:
        # Don't clobber an existing file with empty data.
        if not CAPS_FILE.exists():
            CAPS_FILE.write_text("{}")
        sys.exit(0)

    tmp = CAPS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(caps, indent=2, sort_keys=True))
    tmp.replace(CAPS_FILE)

    if "--quiet" not in sys.argv:
        for email, rec in caps.items():
            if rec.get("status") != "ok":
                print(f"{email:<35} (pending: {rec.get('n_points', 0)}/{rec.get('min_required', '?')} samples)")
                continue
            cap = rec["cap_tokens"]
            cap_s = f"{cap/1e9:.2f}B" if cap >= 1e9 else (
                    f"{cap/1e6:.1f}M" if cap >= 1e6 else f"{cap/1e3:.0f}K")
            rem = rec.get("remaining_tokens_est", 0)
            rem_s = f"{rem/1e9:.2f}B" if rem >= 1e9 else (
                    f"{rem/1e6:.1f}M" if rem >= 1e6 else f"{rem/1e3:.0f}K")
            print(f"{email:<35} cap≈{cap_s:>6}  remaining≈{rem_s:>6}  "
                  f"({rec['formula']}, r²={rec['r_squared']}, n={rec['n_points']})")


if __name__ == "__main__":
    main()
