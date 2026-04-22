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


import numpy as np


def fit_linear_model(samples: list[tuple[float, int, int, int, int]]
                     ) -> tuple[np.ndarray | None, float, int]:
    """Fit Δutil = a·Δin + b·Δout + c·Δcr + d·Δcc via non-negative least squares.

    samples = [(delta_util_pct, d_inp, d_out, d_cr, d_cc), ...]

    Non-negativity constraint: token weights can't be negative (adding tokens
    can't decrease utilization). Without this constraint, noise lets negative
    weights absorb residuals and produce absurd caps.

    Returns (weights_array, r_squared, n_samples) or (None, 0, 0) if under-determined.
    Weights are in units of %util per raw token (scale: ~1e-8 to 1e-6).
    """
    # Need at least 4 samples for 4 unknowns. Prefer 8+ for stability.
    if len(samples) < 4:
        return None, 0.0, len(samples)

    Y = np.array([s[0] for s in samples], dtype=float)             # Δutil %
    X = np.array([[s[1], s[2], s[3], s[4]] for s in samples], dtype=float)  # Δtoken counts

    # NNLS (non-negative least squares): scipy.optimize.nnls. If scipy isn't
    # available, fall back to OLS and clip.
    try:
        from scipy.optimize import nnls
        weights, _ = nnls(X, Y)
    except ImportError:
        weights, *_ = np.linalg.lstsq(X, Y, rcond=None)
        weights = np.clip(weights, 0, None)

    # R² vs the mean (not through origin — util has nonzero mean across windows)
    pred = X @ weights
    ss_res = float(np.sum((Y - pred) ** 2))
    ss_tot = float(np.sum((Y - Y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return weights, max(0.0, min(1.0, r2)), len(samples)


def cap_from_weights(weights: np.ndarray) -> tuple[int, dict]:
    """Given weights (%util per token type), return cap + normalized formula.

    Model: util_pct = sum(w_i × tokens_i); cap_i = 100 / w_i for each token type.
    Cap is multi-dimensional. Report a "mixed cap" in each token-type dimension:
      - at this weight, spending ONLY input tokens gives cap 100/w_in
      - same for output, cr, cc

    For a single user-friendly number, report the cap in the DOMINANT
    observed token type — the one that contributed most to historical spend.
    For most Claude Code users that's cache_read.
    """
    w_in, w_out, w_cr, w_cc = weights
    caps = {}
    formula_parts = []
    for name, w in [("in", w_in), ("out", w_out), ("cr", w_cr), ("cc", w_cc)]:
        if w > 0:
            caps[name] = int(100.0 / w)
            # Express as "X × type" where X is the relative weight vs output
            formula_parts.append((name, float(w)))
    return caps, formula_parts


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

    # For each window: walk consecutive poll pairs, emit a raw
    # (Δutil, Δin, Δout, Δcr, Δcc) observation. These become rows in a
    # multivariate regression per email.
    samples_per_email: dict[str, list[tuple[float, int, int, int, int]]] = defaultdict(list)

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
        for (t0, u0), (t1, u1) in zip(pts, pts[1:]):
            du = u1 - u0
            # Keep zero-delta pairs too — they're valid observations that
            # token spend in that interval produced <0.5% util change. This
            # helps pin down small weights.
            inp, out, cr, cc = tokens_in_range(events, float(t0), float(t1))
            if inp + out + cr + cc == 0 and du == 0:
                continue  # no signal at all
            samples_per_email[email].append((du, inp, out, cr, cc))

    MIN_SAMPLES = 4
    out = {}
    for email, samples in samples_per_email.items():
        if len(samples) < MIN_SAMPLES:
            out[email] = {
                "status": "insufficient_data",
                "n_points": len(samples),
                "min_required": MIN_SAMPLES,
                "last_updated": int(datetime.now().timestamp()),
            }
            continue

        weights, r2, n = fit_linear_model(samples)
        if weights is None or not np.any(weights > 0):
            out[email] = {
                "status": "insufficient_data",
                "n_points": n,
                "min_required": MIN_SAMPLES,
                "last_updated": int(datetime.now().timestamp()),
            }
            continue

        caps, formula = cap_from_weights(weights)
        # Normalize weights relative to the smallest non-zero weight so the
        # "formula" readout is interpretable (e.g., "1×in + 1×out + 0.1×cr").
        nonzero = [w for _, w in formula if w > 0]
        if nonzero:
            base = min(nonzero)
            formula_norm = {name: round(w / base, 3) for name, w in formula}
        else:
            formula_norm = {}

        out[email] = {
            "status": "ok",
            "weights": {
                "in": float(weights[0]),
                "out": float(weights[1]),
                "cr": float(weights[2]),
                "cc": float(weights[3]),
            },
            "formula_ratios": formula_norm,
            "caps_by_token_type": caps,
            "r_squared": round(r2, 3),
            "n_points": n,
            "last_updated": int(datetime.now().timestamp()),
        }

    # Enrich with current util and per-token-type remaining estimates.
    # For each token type that has a positive weight, we know cap = 100/w.
    # "Remaining" in that type = cap × (1 - util/100). The most informative
    # single number to surface is the DOMINANT type (where your usage skews).
    if RESETS_FILE.exists():
        try:
            resets = json.loads(RESETS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            resets = {}
        for email, rec in out.items():
            cur = resets.get(email)
            if not cur:
                continue
            util = cur.get("five_hour_pct") or 0
            rec["current_util"] = util
            if rec.get("status") != "ok":
                continue
            caps = rec.get("caps_by_token_type", {})
            rem = {}
            for name, cap in caps.items():
                rem[name] = max(0, int(cap * (1 - util / 100)))
            rec["remaining_by_token_type"] = rem

            # Pick "dominant" type = the one with the most weight × typical spend.
            # Use the account's historical per-token-type spend (sum across all
            # observations for this email) to weight importance.
            type_totals = {"in": 0, "out": 0, "cr": 0, "cc": 0}
            for _, i, o, r, c in samples_per_email.get(email, []):
                type_totals["in"] += i
                type_totals["out"] += o
                type_totals["cr"] += r
                type_totals["cc"] += c
            # Dominant = argmax of (weight × observed_spend). That's the type
            # most likely to be the binding constraint for this user.
            dominant = None
            best_score = -1
            w = rec["weights"]
            for name, total in type_totals.items():
                score = w.get(name, 0) * total
                if score > best_score:
                    best_score = score
                    dominant = name
            rec["dominant_type"] = dominant
            if dominant and rem.get(dominant):
                rec["remaining_tokens_est"] = rem[dominant]

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

    def fmt(n: int) -> str:
        if n >= 1e9: return f"{n/1e9:.2f}B"
        if n >= 1e6: return f"{n/1e6:.1f}M"
        if n >= 1e3: return f"{n/1e3:.0f}K"
        return str(n)

    if "--quiet" not in sys.argv:
        for email, rec in caps.items():
            if rec.get("status") != "ok":
                print(f"{email:<35} (pending: {rec.get('n_points', 0)}/{rec.get('min_required', '?')} samples)")
                continue
            dom = rec.get("dominant_type") or "?"
            rem = rec.get("remaining_tokens_est", 0)
            caps_by = rec.get("caps_by_token_type", {})
            ratios = rec.get("formula_ratios", {})
            formula_str = " + ".join(f"{r}×{k}" for k, r in ratios.items() if r > 0)
            caps_str = " ".join(f"{k}={fmt(v)}" for k, v in caps_by.items() if v)
            print(f"{email:<35} remaining≈{fmt(rem):>6} {dom:<3} "
                  f"caps: {caps_str}")
            print(f"{'':<35}   util={rec.get('current_util','?')}%  "
                  f"formula ~ {formula_str}  (r²={rec['r_squared']}, n={rec['n_points']})")


if __name__ == "__main__":
    main()
