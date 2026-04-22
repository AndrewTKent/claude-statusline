#!/usr/bin/env python3
"""Derive a plan-agnostic "work unit" measure from utilization history.

Model
-----
Anthropic's 5-hour utilization follows a single linear formula across all
plan tiers (Pro / Max / Max-5×). Only the denominator (cap) changes by
plan; the numerator weights on token types are universal.

    util_pct = 100 × (w_in·in + w_out·out + w_cr·cr + w_cc·cc) / cap_plan

So a *plan-agnostic* "work unit" is:

    1 wu ≡ (1 / w_out) of the weighted numerator
         = an output-token-equivalent

We fit weights ONCE from whichever account has the most / best data
(typically the heavy-use account), then apply those same weights to ALL
accounts to compute per-account work-unit spend. Each account's cap in
work units = 100 / (w_out × plan_cap_seen).

Why fit once
------------
The weights describe Anthropic's billing formula — they don't depend on
which account you're logged into. Fitting per account would waste light
accounts' data (too collinear for stable NNLS), so we take the best-fit
weights and apply them universally. Each account still gets its own cap,
computed from its own observed (util, spend) pairs once the weights are
known.

Files
-----
Reads:
    ~/.claude/utilization-history.jsonl
    ~/.claude/projects/**/*.jsonl
    ~/.claude/account-resets.json        (current util per account)
    ~/.claude/session-accounts.json      (per-session email span)

Writes:
    ~/.claude/work-unit-weights.json     (fit weights + provenance)
    ~/.claude/account-caps.json          (per-account work-unit caps + current spend)

Quality gates
-------------
- Weights marked "calibrating" until R² ≥ 0.85 and n ≥ 30.
- Per-account caps marked "calibrating" until the source weights are "ok" AND
  at least one window has been observed to >60% utilization on that account.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
PROJECTS_DIR = CLAUDE_DIR / "projects"
HISTORY_FILE = CLAUDE_DIR / "utilization-history.jsonl"
WEIGHTS_FILE = CLAUDE_DIR / "work-unit-weights.json"
CAPS_FILE = CLAUDE_DIR / "account-caps.json"
RESETS_FILE = CLAUDE_DIR / "account-resets.json"
SESSION_ACCOUNTS_FILE = CLAUDE_DIR / "session-accounts.json"

# Quality gates
MIN_WEIGHT_SAMPLES = 30
MIN_WEIGHT_R2 = 0.85
MIN_CAP_WINDOW_UTIL = 60.0  # need a window observed near-capacity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt(n: int | float) -> str:
    n = float(n)
    if n >= 1e9: return f"{n/1e9:.2f}B"
    if n >= 1e6: return f"{n/1e6:.1f}M"
    if n >= 1e3: return f"{n/1e3:.0f}K"
    return f"{n:.0f}"


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows = []
    with open(HISTORY_FILE) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def window_key(reset_iso: str) -> str:
    """Snap reset ISO to the nearest 5-hour slot so near-hour-boundary jitter
    doesn't split a single 5h window into two keys.

    The 5h reset time is fixed once a window starts, but Anthropic reports it
    with microsecond precision that drifts by up to a second across polls.
    When the drift crosses an hour boundary (e.g. 10:59:59.7 vs 11:00:00.1),
    naive hour-rounding splits samples from the SAME window into two keys —
    which cuts Δutil pairs in half and hurts the regression fit.

    Solution: divide the reset epoch by 5h and round to the nearest 5h slot,
    giving us a stable window key immune to sub-minute jitter.
    """
    dt = parse_iso(reset_iso)
    if not dt:
        return reset_iso
    slot = round(dt.timestamp() / (5 * 3600)) * (5 * 3600)
    slot_dt = datetime.fromtimestamp(slot, tz=timezone.utc)
    return slot_dt.strftime("%Y-%m-%dT%H:00")


# ---------------------------------------------------------------------------
# Per-window token enumeration (flat event list sorted by ts)
# ---------------------------------------------------------------------------
def load_session_email_spans() -> dict[str, list[tuple[str, str | None, str]]]:
    """session-accounts.json → {sid: [(from_iso, to_iso_or_None, email), ...]}."""
    if not SESSION_ACCOUNTS_FILE.exists():
        return {}
    try:
        raw = json.loads(SESSION_ACCOUNTS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, list] = {}
    for sid, info in raw.items():
        if not isinstance(info, dict):
            continue
        spans = []
        for s in info.get("spans") or []:
            if not isinstance(s, dict):
                continue
            email = (s.get("email") or "").lower()
            if not email:
                continue
            spans.append((s.get("from"), s.get("to"), email))
        if spans:
            spans.sort(key=lambda x: x[0] or "")
            out[sid] = spans
    return out


def email_for(spans: list, ts_iso: str) -> str | None:
    """Return the email whose span covers ts_iso. None if no span matches."""
    for frm, to, email in spans:
        if frm and ts_iso < frm:
            continue
        if to and ts_iso > to:
            continue
        return email
    return None


def bucket_window_events(window_start: datetime, window_end: datetime
                         ) -> list[tuple[float, int, int, int, int, str | None]]:
    """Return (ts_epoch, inp, out, cr, cc, email) for every dedup'd request.

    Email is resolved via session-accounts.json — the account logged in at
    the exact request timestamp. None if session has no recorded spans.
    """
    seen = set()
    events = []
    start_ts = window_start.timestamp()
    email_spans = load_session_email_spans()
    for jf in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            st = jf.stat()
        except OSError:
            continue
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
                    email = email_for(spans, ts_str)
                    events.append((
                        tsv.timestamp(),
                        usage.get("input_tokens", 0) or 0,
                        usage.get("output_tokens", 0) or 0,
                        usage.get("cache_read_input_tokens", 0) or 0,
                        usage.get("cache_creation_input_tokens", 0) or 0,
                        email,
                    ))
        except OSError:
            continue
    events.sort(key=lambda e: e[0])
    return events


def tokens_in_range(events: list, t0: float, t1: float,
                    email_filter: str | None = None) -> tuple[int, int, int, int]:
    inp = out = cr = cc = 0
    for row in events:
        ts, i, o, r, c = row[0], row[1], row[2], row[3], row[4]
        em = row[5] if len(row) > 5 else None
        if email_filter is not None and em != email_filter:
            continue
        if ts < t0:
            continue
        if ts > t1:
            break
        inp += i; out += o; cr += r; cc += c
    return inp, out, cr, cc


# ---------------------------------------------------------------------------
# Weight fit (once, across all accounts' observations)
# ---------------------------------------------------------------------------
def collect_samples(history: list[dict]) -> dict[str, list[tuple[float, int, int, int, int]]]:
    """Return {email: [(Δutil, Δin, Δout, Δcr, Δcc), ...]}."""
    window_map: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    window_reset: dict[tuple[str, str], datetime] = {}
    for row in history:
        email = row.get("email")
        reset_iso = row.get("five_hour_reset")
        ts = row.get("ts")
        util = row.get("five_hour_pct")
        if not (email and reset_iso and ts is not None and util is not None):
            continue
        key = (email, window_key(reset_iso))
        window_map[key].append((int(ts), float(util)))
        if key not in window_reset:
            dt = parse_iso(reset_iso)
            if dt:
                window_reset[key] = dt

    samples: dict[str, list] = defaultdict(list)
    for key, points in window_map.items():
        email = key[0]
        reset_dt = window_reset.get(key)
        if not reset_dt:
            continue
        start = datetime.fromtimestamp(reset_dt.timestamp() - 5*3600, tz=timezone.utc)
        events = bucket_window_events(start, reset_dt)
        if not events:
            continue
        # For regression, include events attributed to THIS account OR
        # unattributed (None). Other accounts' events are excluded so their
        # spend doesn't pollute this account's Δutil signal. Unattributed
        # events are kept because during a window the only token-producing
        # account is whichever one the OAuth history pairs with the Δutil —
        # dropping them would lose most of the training signal, and they
        # can't actually belong to a different account (the util we observed
        # is for this account's plan).
        pts = sorted(points)
        for (t0, u0), (t1, u1) in zip(pts, pts[1:]):
            du = u1 - u0
            inp = out = cr = cc = 0
            for row in events:
                ts, i, o, r, c = row[0], row[1], row[2], row[3], row[4]
                em = row[5] if len(row) > 5 else None
                if em is not None and em != email:
                    continue  # belongs to a different account
                if ts < float(t0) or ts > float(t1):
                    continue
                inp += i; out += o; cr += r; cc += c
            if du == 0 and (inp + out + cr + cc) == 0:
                continue  # idle poll, no signal
            samples[email].append((du, inp, out, cr, cc))
    return dict(samples)


def fit_weights(samples_by_email: dict) -> dict:
    """Fit Δutil = Σ w_i · Δtokens_i via NNLS across ALL accounts' samples.

    Anthropic's formula is universal — combining accounts' samples only
    improves the fit, never hurts it.
    """
    all_samples = []
    for email, rows in samples_by_email.items():
        all_samples.extend(rows)

    if len(all_samples) < 4:
        return {
            "status": "calibrating",
            "n_samples": len(all_samples),
            "min_required": MIN_WEIGHT_SAMPLES,
            "last_updated": int(datetime.now().timestamp()),
        }

    Y = np.array([s[0] for s in all_samples], dtype=float)
    X = np.array([[s[1], s[2], s[3], s[4]] for s in all_samples], dtype=float)

    try:
        from scipy.optimize import nnls
        w, _ = nnls(X, Y)
    except ImportError:
        w, *_ = np.linalg.lstsq(X, Y, rcond=None)
        w = np.clip(w, 0, None)

    pred = X @ w
    ss_res = float(np.sum((Y - pred) ** 2))
    ss_tot = float(np.sum((Y - Y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    r2 = max(0.0, min(1.0, r2))

    status = "ok" if (r2 >= MIN_WEIGHT_R2 and len(all_samples) >= MIN_WEIGHT_SAMPLES) else "calibrating"

    return {
        "status": status,
        "weights": {
            "in": float(w[0]),
            "out": float(w[1]),
            "cr": float(w[2]),
            "cc": float(w[3]),
        },
        "r_squared": round(r2, 3),
        "n_samples": len(all_samples),
        "min_samples": MIN_WEIGHT_SAMPLES,
        "min_r_squared": MIN_WEIGHT_R2,
        "last_updated": int(datetime.now().timestamp()),
    }


# ---------------------------------------------------------------------------
# Work-unit conversion + per-account caps
# ---------------------------------------------------------------------------
def work_units(inp: int, out: int, cr: int, cc: int, w: dict) -> float:
    """Convert raw tokens to output-token-equivalents (work units).

    wu = (w_in·in + w_out·out + w_cr·cr + w_cc·cc) / w_out
       = in·(w_in/w_out) + out + cr·(w_cr/w_out) + cc·(w_cc/w_out)
    """
    w_out = w.get("out", 0) or 0
    if w_out <= 0:
        # If output weight is zero, anchor to the largest nonzero weight
        anchor = max(w.get("in", 0), w.get("cr", 0), w.get("cc", 0))
        if anchor <= 0:
            return 0.0
        w_out = anchor
    return (
        inp * (w.get("in", 0) or 0) +
        out * w_out +
        cr  * (w.get("cr", 0) or 0) +
        cc  * (w.get("cc", 0) or 0)
    ) / w_out


def current_window_spend(email: str, reset_iso: str) -> tuple[int, int, int, int]:
    """Sum tokens in the current 5h window for this email.

    Attributes each request to the account that was logged in at the exact
    request timestamp (via session-accounts.json spans). Requests with no
    recorded span fall to the active account as a best-effort fallback.
    """
    reset_dt = parse_iso(reset_iso)
    if not reset_dt:
        return 0, 0, 0, 0
    start = datetime.fromtimestamp(reset_dt.timestamp() - 5*3600, tz=timezone.utc)
    events = bucket_window_events(start, reset_dt)
    total = [0, 0, 0, 0]
    for row in events:
        i, o, r, c = row[1], row[2], row[3], row[4]
        em = row[5] if len(row) > 5 else None
        # Only attribute tokens to THIS account if the span says so.
        # None spans (unknown attribution) are dropped — safer to under-count
        # than to pile all unattributed spend onto one account.
        if em != email:
            continue
        total[0] += i; total[1] += o; total[2] += r; total[3] += c
    return tuple(total)


def build_account_caps(weights_rec: dict, samples: dict) -> dict:
    """For each account: compute current window wu spend and cap in wu.

    Cap in wu = observed_wu_at_observed_util × (100 / observed_util).
    Take the max-util observation per account as the anchor.
    """
    out: dict[str, dict] = {}
    if not weights_rec or "weights" not in weights_rec:
        return out
    w = weights_rec["weights"]
    weights_ok = weights_rec.get("status") == "ok"

    # Load current resets file for live util
    resets = {}
    if RESETS_FILE.exists():
        try:
            resets = json.loads(RESETS_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    # For cap anchor, walk samples per account and compute cumulative wu
    # per window, pairing with observed util at end of that window.
    history = load_history()
    anchor_by_email: dict[str, tuple[float, float]] = {}  # email -> (util, wu)
    window_map: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
    window_reset: dict[tuple[str, str], datetime] = {}
    for row in history:
        email = row.get("email")
        reset_iso = row.get("five_hour_reset")
        ts = row.get("ts")
        util = row.get("five_hour_pct")
        if not (email and reset_iso and ts is not None and util is not None):
            continue
        key = (email, window_key(reset_iso))
        window_map[key].append((int(ts), float(util)))
        if key not in window_reset:
            dt = parse_iso(reset_iso)
            if dt:
                window_reset[key] = dt

    for key, points in window_map.items():
        email = key[0]
        reset_dt = window_reset.get(key)
        if not reset_dt:
            continue
        start = datetime.fromtimestamp(reset_dt.timestamp() - 5*3600, tz=timezone.utc)
        events = bucket_window_events(start, reset_dt)
        if not events:
            continue
        # Attribute to THIS account only (same as current_window_spend).
        inp = sum(e[1] for e in events if (len(e) > 5 and e[5] == email))
        o = sum(e[2] for e in events if (len(e) > 5 and e[5] == email))
        cr = sum(e[3] for e in events if (len(e) > 5 and e[5] == email))
        cc = sum(e[4] for e in events if (len(e) > 5 and e[5] == email))
        wu = work_units(inp, o, cr, cc, w)
        # Anchor util = max observed util in the window
        max_util = max(u for _, u in points)
        if max_util <= 0:
            continue
        cur = anchor_by_email.get(email)
        if not cur or max_util > cur[0]:
            anchor_by_email[email] = (max_util, wu)

    # Build per-account caps
    for email, cur in resets.items():
        util_now = cur.get("five_hour_pct") or 0
        reset_iso = cur.get("five_hour_reset")
        inp, outv, cr, cc = current_window_spend(email, reset_iso) if reset_iso else (0, 0, 0, 0)
        wu_now = work_units(inp, outv, cr, cc, w)

        anchor = anchor_by_email.get(email)
        cap_wu = None
        if anchor and anchor[0] >= MIN_CAP_WINDOW_UTIL:
            # cap × (util/100) = wu_at_util  →  cap = wu / (util/100)
            cap_wu = anchor[1] / (anchor[0] / 100.0)

        rec: dict = {
            "current_util": util_now,
            "current_wu": round(wu_now, 1),
            "weights_status": weights_rec.get("status", "calibrating"),
            "last_updated": int(datetime.now().timestamp()),
        }
        if cap_wu and weights_ok:
            rec["status"] = "ok"
            rec["cap_wu"] = round(cap_wu, 1)
            rec["remaining_wu"] = max(0.0, round(cap_wu - wu_now, 1))
            rec["anchor_util"] = anchor[0]
            rec["anchor_wu"] = round(anchor[1], 1)
        else:
            rec["status"] = "calibrating"
            if anchor:
                rec["best_observed_util"] = anchor[0]
                rec["best_observed_wu"] = round(anchor[1], 1)
        out[email] = rec

    # Also include accounts with samples but no current-resets record
    for email in samples:
        if email in out:
            continue
        anchor = anchor_by_email.get(email)
        rec = {
            "current_util": None,
            "current_wu": None,
            "weights_status": weights_rec.get("status", "calibrating"),
            "status": "calibrating",
            "last_updated": int(datetime.now().timestamp()),
        }
        if anchor:
            rec["best_observed_util"] = anchor[0]
            rec["best_observed_wu"] = round(anchor[1], 1)
        out[email] = rec

    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    history = load_history()
    samples = collect_samples(history)
    weights_rec = fit_weights(samples)

    # Persist weights
    WEIGHTS_FILE.write_text(json.dumps(weights_rec, indent=2, sort_keys=True))

    caps = build_account_caps(weights_rec, samples)
    CAPS_FILE.write_text(json.dumps(caps, indent=2, sort_keys=True))

    if "--quiet" in sys.argv:
        return

    # Human-readable summary
    status = weights_rec.get("status", "unknown")
    r2 = weights_rec.get("r_squared")
    n = weights_rec.get("n_samples", 0)
    if status == "ok":
        w = weights_rec["weights"]
        w_out = w["out"] or 1
        print(f"weights OK  (r²={r2}, n={n})")
        print(f"  1 wu = 1 output-token-equivalent")
        print(f"    in  = {w['in']/w_out:.4f} wu each")
        print(f"    out = 1.0000 wu each")
        print(f"    cr  = {w['cr']/w_out:.4f} wu each  ({w_out/w['cr'] if w['cr']>0 else float('inf'):.0f}× less than out)")
        print(f"    cc  = {w['cc']/w_out:.4f} wu each")
    else:
        print(f"weights calibrating  (r²={r2}, n={n}/{MIN_WEIGHT_SAMPLES})")

    print()
    for email, rec in caps.items():
        util = rec.get("current_util")
        util_s = f"{util:.0f}%" if isinstance(util, (int, float)) else "?"
        cur_wu = rec.get("current_wu", 0) or 0
        if rec.get("status") == "ok":
            cap = rec.get("cap_wu", 0)
            rem = rec.get("remaining_wu", 0)
            print(f"  {email:<35} {util_s:>5} util  {cur_wu:.0f}wu / {cap:.0f}wu cap  ({rem:.0f}wu left)")
        else:
            bow = rec.get("best_observed_wu")
            bou = rec.get("best_observed_util")
            tail = ""
            if bow is not None and bou is not None:
                tail = f"  (best seen: {bow:.0f}wu @ {bou:.0f}%)"
            print(f"  {email:<35} {util_s:>5} util  {cur_wu:.0f}wu  [calibrating]{tail}")


if __name__ == "__main__":
    main()
