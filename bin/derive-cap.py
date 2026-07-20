#!/usr/bin/env python3
# Manual tool — outputs account-caps.json for statusline cap columns (not scheduled).
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

Performance
-----------
Transcript events are read ONCE into a time-sorted, requestId-deduped
index (build_event_index); per-window lookups bisect that index instead of
re-walking ~/.claude/projects/**/*.jsonl on every call. The previous design
re-scanned the entire corpus once per (account, window) pair — O(windows ×
corpus) — which pegged a core for minutes as both grew.

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

import bisect
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
# Output paths honor env overrides so a dry run can write elsewhere for diffing.
WEIGHTS_FILE = Path(os.environ.get("WU_WEIGHTS_OUT") or (CLAUDE_DIR / "work-unit-weights.json"))
CAPS_FILE = Path(os.environ.get("WU_CAPS_OUT") or (CLAUDE_DIR / "account-caps.json"))
RESETS_FILE = CLAUDE_DIR / "account-resets.json"
SESSION_ACCOUNTS_FILE = CLAUDE_DIR / "session-accounts.json"

# Quality gates
MIN_WEIGHT_SAMPLES = 30
MIN_WEIGHT_R2 = 0.60
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


class EventIndex:
    """All transcript usage events as 7-tuples (ts, in, out, cr, cc, email, rid),
    sorted by ts. Built once per process (build_event_index); per-window queries
    bisect the parallel `ts` list rather than re-reading the corpus.

    Dedup is deferred to the per-window slice() — NOT done globally at build
    time — so it reproduces the old per-window dedup scope exactly. A single
    requestId emits multiple usage records with different timestamps; when those
    straddle a 5h boundary the old code counted the request once in EACH window
    it touched (fresh `seen` per window). Global dedup would keep only one copy
    and silently drop the others, so we keep every record here and dedup within
    the window in slice().
    """
    __slots__ = ("events", "ts")

    def __init__(self, events: list[tuple[float, int, int, int, int, str | None, str]]):
        self.events = events
        self.ts = [e[0] for e in events]

    def slice(self, t0: float, t1: float) -> list[tuple[float, int, int, int, int, str | None]]:
        """Events with t0 <= ts <= t1 (inclusive both ends), deduped by requestId
        within the window (first occurrence kept), returned as 6-tuples sorted by ts.
        Matches the old per-call bucket_window_events dedup scope.
        """
        lo = bisect.bisect_left(self.ts, t0)
        hi = bisect.bisect_right(self.ts, t1)
        seen: set = set()
        out: list[tuple[float, int, int, int, int, str | None]] = []
        for e in self.events[lo:hi]:
            rid = e[6]
            if rid in seen:
                continue
            seen.add(rid)
            out.append(e[:6])
        return out


def build_event_index(min_start_ts: float = 0.0) -> EventIndex:
    """Single pass over ~/.claude/projects/**/*.jsonl → time-sorted events.

    Each event is (ts_epoch, in, out, cr, cc, email, rid). Email is resolved via
    session-accounts.json — the account logged in at the request timestamp; None
    if the session has no recorded span. Events before min_start_ts (the earliest
    window start we'll ever query) are dropped to bound memory. Records with no
    requestId/message-id are dropped, matching the old scan.

    Dedup is NOT done here — it's per-window in EventIndex.slice() — so a request
    whose records straddle a 5h boundary is counted in each window, as before.
    """
    events: list[tuple[float, int, int, int, int, str | None, str]] = []
    email_spans = load_session_email_spans()
    for jf in PROJECTS_DIR.rglob("*.jsonl"):
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
                    if not tsv:
                        continue
                    te = tsv.timestamp()
                    if te < min_start_ts:
                        continue
                    msg = j.get("message") or {}
                    usage = msg.get("usage") if isinstance(msg, dict) else None
                    if not usage:
                        continue
                    rid = j.get("requestId") or msg.get("id")
                    if not rid:
                        continue
                    events.append((
                        te,
                        usage.get("input_tokens", 0) or 0,
                        usage.get("output_tokens", 0) or 0,
                        usage.get("cache_read_input_tokens", 0) or 0,
                        usage.get("cache_creation_input_tokens", 0) or 0,
                        email_for(spans, ts_str),
                        rid,
                    ))
        except OSError:
            continue
    events.sort(key=lambda e: e[0])
    return EventIndex(events)


def bucket_window_events(window_start: datetime, window_end: datetime,
                         index: EventIndex
                         ) -> list[tuple[float, int, int, int, int, str | None]]:
    """Events in [window_start, window_end], sliced from the prebuilt index."""
    return index.slice(window_start.timestamp(), window_end.timestamp())


def min_window_start(history: list[dict]) -> float:
    """Earliest 5h-window start we'll ever query (history + current resets).

    Used to bound the event index: events before this can't fall in any window.
    """
    starts: list[float] = []
    for row in history:
        dt = parse_iso(row.get("five_hour_reset"))
        if dt:
            starts.append(dt.timestamp() - 5 * 3600)
    if RESETS_FILE.exists():
        try:
            resets = json.loads(RESETS_FILE.read_text())
            for cur in resets.values():
                if not isinstance(cur, dict):
                    continue
                dt = parse_iso(cur.get("five_hour_reset"))
                if dt:
                    starts.append(dt.timestamp() - 5 * 3600)
        except (OSError, json.JSONDecodeError):
            pass
    return min(starts) if starts else 0.0


# ---------------------------------------------------------------------------
# Weight fit (once, across all accounts' observations)
# ---------------------------------------------------------------------------
def collect_samples(history: list[dict], index: EventIndex) -> dict[str, list[tuple[float, int, int, int, int]]]:
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
        events = bucket_window_events(start, reset_dt, index)
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
    """Fit utilization via a constrained 2-parameter model to sidestep collinearity.

    Problem with full 4-variable NNLS: in/out/cr/cc move together per request
    (cache_read and output_tokens had correlation 0.82 on real data), so NNLS
    can't distinguish their individual weights. It arbitrarily dumps all the
    credit on one variable.

    Solution: physically constrain the model by assuming a known coupling
    between output and cache_read — Anthropic's billing almost certainly uses
    a fixed cache-read discount ratio k, i.e. effective_tokens = out + cr/k.
    Input and cache_creation are absorbed into k (they're small anyway).

        util = a × (out + cr/k)     (2 unknowns)

    Fit procedure:
      1. Grid-search k ∈ {1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000}
      2. For each k, solve for a via weighted least-squares through origin
      3. Pick (a, k) with best R²

    This trades 2 degrees of freedom for 4, stabilizing under collinearity at
    the cost of a small bias if Anthropic's real formula isn't purely out+cr/k.
    In practice it tends to produce interpretable, stable numbers.
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

    Y = np.array([s[0] for s in all_samples], dtype=float)     # Δutil
    OUT = np.array([s[2] for s in all_samples], dtype=float)   # Δ output
    CR = np.array([s[3] for s in all_samples], dtype=float)    # Δ cache_read
    # in + cc are small and noisy — fold cc into the cr/k bucket, drop in entirely.
    CC = np.array([s[4] for s in all_samples], dtype=float)

    # Grid of candidate cache discount ratios
    k_grid = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000,
              10000, 20000, 50000, 100000, float('inf')]

    best = None  # (k, a, r2)
    for k in k_grid:
        # Effective tokens: out + cr/k  (fold cc into cr since it's also cache-ish)
        Xeff = OUT + (CR + CC) / k
        # Weighted least-squares through origin: a = Σ(y·x) / Σ(x²)
        sum_xy = float(np.sum(Y * Xeff))
        sum_xx = float(np.sum(Xeff * Xeff))
        if sum_xx <= 0:
            continue
        a = sum_xy / sum_xx
        if a <= 0:
            continue
        pred = a * Xeff
        ss_res = float(np.sum((Y - pred) ** 2))
        ss_tot = float(np.sum((Y - Y.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
        r2 = max(0.0, min(1.0, r2))
        if best is None or r2 > best[2]:
            best = (k, a, r2)

    if best is None:
        return {
            "status": "calibrating",
            "n_samples": len(all_samples),
            "min_required": MIN_WEIGHT_SAMPLES,
            "last_updated": int(datetime.now().timestamp()),
        }

    k_best, a_best, r2_best = best
    # Convert the 2-param model back to the 4-weight schema the rest of the
    # code expects. "in" weight is absorbed into cc (small effect either way).
    weights = {
        "in": 0.0,                  # dropped from the model
        "out": float(a_best),       # primary weight
        "cr": float(a_best / k_best),
        "cc": float(a_best / k_best),  # folded into cache bucket
    }

    status = "ok" if (r2_best >= MIN_WEIGHT_R2 and len(all_samples) >= MIN_WEIGHT_SAMPLES) else "calibrating"

    return {
        "status": status,
        "weights": weights,
        "model": "constrained-2param",
        "cache_discount_k": float(k_best),
        "r_squared": round(r2_best, 3),
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


def current_window_spend(email: str, reset_iso: str, index: EventIndex) -> tuple[int, int, int, int]:
    """Sum tokens in the current 5h window for this email.

    Attributes each request to the account that was logged in at the exact
    request timestamp (via session-accounts.json spans). Requests with no
    recorded span fall to the active account as a best-effort fallback.
    """
    reset_dt = parse_iso(reset_iso)
    if not reset_dt:
        return 0, 0, 0, 0
    start = datetime.fromtimestamp(reset_dt.timestamp() - 5*3600, tz=timezone.utc)
    events = bucket_window_events(start, reset_dt, index)
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


def build_account_caps(weights_rec: dict, samples: dict, index: EventIndex) -> dict:
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
        events = bucket_window_events(start, reset_dt, index)
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
        inp, outv, cr, cc = current_window_spend(email, reset_iso, index) if reset_iso else (0, 0, 0, 0)
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
    # Build the transcript event index ONCE, then reuse for every window query
    # (collect_samples, build_account_caps, current_window_spend).
    index = build_event_index(min_window_start(history))
    samples = collect_samples(history, index)
    weights_rec = fit_weights(samples)

    # Persist weights
    WEIGHTS_FILE.write_text(json.dumps(weights_rec, indent=2, sort_keys=True))

    caps = build_account_caps(weights_rec, samples, index)
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
