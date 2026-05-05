"""Core parsing, classification, and aggregation for Claude Code JSONL logs.

This module is the single source of truth for:

  * Parsing ~/.claude/projects/**/*.jsonl session files.
  * Classifying each request along two orthogonal dimensions:
      - work-type  ("work" | "personal" | "unknown")  — path/content/overrides
      - payer      (EMAIL_PAYER_MAP label | "unknown") — session-accounts.json span
  * Deduping requests by requestId, summing input+output tokens.
  * Building summary and cache JSON payloads.

Two consumers exist:

  * scan-tokens.py          — one-shot CLI. Walks the whole tree, writes cache
                              and summary, exits. Used by cron, launchd
                              periodic runs, and manual invocations.
  * scan-tokens-daemon.py   — long-running daemon. Boots once, then updates
                              aggregates incrementally from fswatch events.
                              Calls into this module for parsing and
                              classification; manages its own in-memory state.

Keep this module import-safe: no global I/O, no prints, no sys.exit. All
configuration is pulled from the environment via load_config() so callers can
override per-invocation if needed.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    """Runtime configuration resolved from environment variables."""

    claude_dir: Path
    projects_dir: Path
    cache_file: Path
    summary_file: Path
    overrides_file: Path
    session_accounts_file: Path
    challenge_start: str
    work_paths: tuple[str, ...]
    personal_paths: tuple[str, ...]
    work_keywords: tuple[str, ...]
    personal_keywords: tuple[str, ...]
    email_payer_map: dict[str, str]
    bounty_target_tokens: int
    bounty_lookback_days: int
    bounty_session_gap_min: int
    codex_sessions_dir: Path


def _split_csv(val: str) -> tuple[str, ...]:
    return tuple(s.strip().lower() for s in val.split(",") if s.strip())


def _parse_email_payer_map(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in raw.split():
        if ":" in pair:
            label, email = pair.split(":", 1)
            result[email.strip().lower()] = label.strip()
    return result


def load_config() -> Config:
    """Build a Config from environment variables. Safe to call multiple times."""
    claude_dir = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
    codex_dir = Path(os.environ.get("CODEX_DIR", str(Path.home() / ".codex")))
    return Config(
        claude_dir=claude_dir,
        projects_dir=claude_dir / "projects",
        cache_file=claude_dir / "token-scan-cache.json",
        summary_file=claude_dir / "token-scan-summary.json",
        overrides_file=claude_dir / "token-scan-overrides.json",
        session_accounts_file=claude_dir / "session-accounts.json",
        challenge_start=os.environ.get("CHALLENGE_START", ""),
        work_paths=_split_csv(os.environ.get("WORK_PATHS", "")),
        personal_paths=_split_csv(os.environ.get("PERSONAL_PATHS", "")),
        work_keywords=_split_csv(os.environ.get("WORK_KEYWORDS", "")),
        personal_keywords=_split_csv(os.environ.get("PERSONAL_KEYWORDS", "")),
        email_payer_map=_parse_email_payer_map(os.environ.get("EMAIL_PAYER_MAP", "")),
        bounty_target_tokens=int(os.environ.get("BOUNTY_TARGET_TOKENS", "0") or "0"),
        bounty_lookback_days=int(os.environ.get("BOUNTY_LOOKBACK_DAYS", "3") or "3"),
        bounty_session_gap_min=int(os.environ.get("BOUNTY_SESSION_GAP_MIN", "30") or "30"),
        codex_sessions_dir=codex_dir / "sessions",
    )


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PATH_RE = re.compile(r"/Users/[^\s\"'`]+")

REDACT_CATEGORIES = {
    "rude-to-claude",
    "personal-aside",
    "personal-comms",
    "sensitive-data",
    "other",
}


# ---------------------------------------------------------------------------
# Requests and session state
# ---------------------------------------------------------------------------
# A "request row" is [rid, ts, inp, out, cache_read, cache_create].
# We keep it as a list (not a dataclass) because it's stored in JSON caches
# and we want minimal overhead across thousands of rows.
Request = list


def normalize_request(r: list) -> Request:
    """Pad older 4-tuple cached rows up to 6-tuple [rid,ts,inp,out,cr,cc]."""
    return [
        r[0], r[1], r[2], r[3],
        r[4] if len(r) > 4 else 0,
        r[5] if len(r) > 5 else 0,
    ]


@dataclass
class SessionSignals:
    """What we extract in a single pass over a JSONL file."""

    cwds: set[str] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    user_text: str | None = None
    requests: list[Request] = field(default_factory=list)


USER_TEXT_CAP = 20_000
USER_SNIPPET_CAP = 2_000


def parse_jsonl_signals(filepath: str | Path, start_offset: int = 0) -> tuple[SessionSignals, int]:
    """Parse a JSONL file from start_offset to EOF.

    Returns (signals, new_offset). When start_offset is nonzero we only collect
    new requests — cwds/paths/user_text are left empty because classification
    uses the per-file snapshot, not per-event deltas.

    A partial line at the tail (likely a mid-write) is skipped and the offset
    is left at the start of that line so the next call re-reads it.
    """
    signals = SessionSignals()
    offset = start_offset

    try:
        with open(filepath, "rb") as f:
            f.seek(start_offset)
            # Track where each line starts so a partial tail rewinds correctly.
            line_start = f.tell()
            while True:
                raw = f.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    # Partial line at tail — caller comes back later.
                    return signals, line_start
                offset = f.tell()
                line_start = offset
                _ingest_line(raw, signals, collect_context=(start_offset == 0))
    except OSError:
        return signals, start_offset

    return signals, offset


def _ingest_line(raw: bytes, signals: SessionSignals, collect_context: bool) -> None:
    line = raw.strip()
    if not line:
        return
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return

    if collect_context:
        cwd = entry.get("cwd")
        if isinstance(cwd, str):
            signals.cwds.add(cwd)

    etype = entry.get("type")
    if etype == "user" and collect_context:
        _collect_user_text(entry, signals)
    elif etype == "assistant":
        _collect_assistant(entry, signals, collect_context)


def _collect_user_text(entry: dict, signals: SessionSignals) -> None:
    if signals.user_text is not None and len(signals.user_text) >= USER_TEXT_CAP:
        return
    content = entry.get("message", {}).get("content", "")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                break
    if not text:
        return
    snippet = text[:USER_SNIPPET_CAP]
    signals.user_text = (signals.user_text or "") + (" " if signals.user_text else "") + snippet


def _collect_assistant(entry: dict, signals: SessionSignals, collect_context: bool) -> None:
    msg = entry.get("message", {})
    usage = msg.get("usage") or {}
    rid = entry.get("requestId") or msg.get("id", "")
    ts = entry.get("timestamp", "")
    if rid and ts and usage:
        signals.requests.append([
            rid, ts,
            usage.get("input_tokens", 0) or 0,
            usage.get("output_tokens", 0) or 0,
            usage.get("cache_read_input_tokens", 0) or 0,
            usage.get("cache_creation_input_tokens", 0) or 0,
        ])
    if not collect_context:
        return
    for block in msg.get("content", []) or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        inp = block.get("input", {})
        for k in ("file_path", "path", "cwd"):
            v = inp.get(k)
            if isinstance(v, str) and v.startswith("/"):
                signals.paths.add(v)
        cmd = inp.get("command", "")
        if isinstance(cmd, str):
            for p in PATH_RE.findall(cmd):
                signals.paths.add(p)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify_path(path: str, cfg: Config) -> str | None:
    """Return 'work' | 'personal' | None based on path substrings."""
    p = path.lower()
    for needle in cfg.personal_paths:
        if needle in p:
            return "personal"
    for needle in cfg.work_paths:
        if needle in p:
            return "work"
    return None


def classify_by_content(signals: SessionSignals, cfg: Config) -> str:
    """Fallback classifier by path + keyword hits. Returns work/personal/unknown."""
    all_paths = signals.cwds | signals.paths
    work = 0
    personal = 0
    for p in all_paths:
        pl = p.lower()
        if any(n in pl for n in cfg.work_paths):
            work += 1
        if any(n in pl for n in cfg.personal_paths):
            personal += 1
    if signals.user_text:
        lower = signals.user_text.lower()
        work += 3 * sum(1 for k in cfg.work_keywords if k in lower)
        personal += 3 * sum(1 for k in cfg.personal_keywords if k in lower)
    if work > personal:
        return "work"
    if personal > work:
        return "personal"
    return "unknown"


# ---------------------------------------------------------------------------
# Overrides (per-session manual reclassification)
# ---------------------------------------------------------------------------
def load_overrides(cfg: Config) -> dict:
    try:
        with open(cfg.overrides_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def parse_override(entry):
    if isinstance(entry, str):
        return entry, []
    if isinstance(entry, dict):
        tag = entry.get("tag")
        ranges = entry.get("ranges", []) or []
        validated = []
        for r in ranges:
            if not isinstance(r, dict):
                continue
            validated.append({
                "from": r.get("from"),
                "to": r.get("to"),
                "tag": r.get("tag"),
                "redact": r.get("redact"),
                "note": r.get("note"),
            })
        return tag, validated
    return None, []


def count_redactions(overrides: dict) -> tuple[int, int]:
    sessions = 0
    ranges = 0
    for entry in overrides.values():
        if not isinstance(entry, dict):
            continue
        rs = [r for r in (entry.get("ranges") or []) if r.get("redact")]
        if rs:
            sessions += 1
            ranges += len(rs)
    return sessions, ranges


def resolve_override(sid: str, ts: str, base_acct: str, overrides: dict) -> str:
    """Apply per-session overrides; range tag beats session tag, last match wins."""
    entry = overrides.get(sid)
    if entry is None:
        return base_acct
    session_tag, ranges = parse_override(entry)
    effective = session_tag or base_acct
    for rng in ranges:
        if rng.get("tag") is None:
            continue
        frm, to = rng.get("from"), rng.get("to")
        if frm is not None and ts < frm:
            continue
        if to is not None and ts > to:
            continue
        effective = rng["tag"]
    return effective


# ---------------------------------------------------------------------------
# Payer dimension (email → label via session-accounts.json spans)
# ---------------------------------------------------------------------------
def load_session_payer_spans(cfg: Config) -> dict:
    """Load session-accounts.json and return {sid: [(from, to, payer)]}."""
    try:
        with open(cfg.session_accounts_file) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, list] = {}
    for sid, info in raw.items():
        if not isinstance(info, dict):
            continue
        spans = info.get("spans") or []
        norm = []
        for span in spans:
            if not isinstance(span, dict):
                continue
            email = (span.get("email") or "").lower()
            payer = cfg.email_payer_map.get(email, "unknown")
            norm.append((span.get("from"), span.get("to"), payer))
        if norm:
            norm.sort(key=lambda s: s[0] or "")
            result[sid] = norm
    return result


def payer_for(spans: list, ts: str) -> str:
    if not spans:
        return "unknown"
    for frm, to, payer in spans:
        if frm is not None and ts < frm:
            continue
        if to is not None and ts > to:
            continue
        return payer
    return "unknown"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def today_start_iso() -> str:
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Aggregates:
    """In-memory aggregation state, keyed by requestId."""

    seen_global: dict[str, tuple] = field(default_factory=dict)
    seen_challenge: dict[str, tuple] = field(default_factory=dict)
    seen_today: dict[str, tuple] = field(default_factory=dict)
    today_start: str = field(default_factory=today_start_iso)
    # rid → (earliest_date_seen, max_inp, max_out). Single bucket per rid even
    # if streaming chunks straddle midnight, so per-date totals don't
    # double-count any request when diffed against the /recover-stats snapshot.
    rid_date_tokens: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    earliest_global_ts: str | None = None

    def _note_ts_and_date(self, ts: str, rid: str, inp: int, out: int) -> None:
        if not ts or not rid:
            return
        if self.earliest_global_ts is None or ts < self.earliest_global_ts:
            self.earliest_global_ts = ts
        date = ts[:10]
        if len(date) != 10:
            return
        prev = self.rid_date_tokens.get(rid)
        if prev is None:
            self.rid_date_tokens[rid] = (date, inp, out)
            return
        new_date = min(prev[0], date)
        new_out = max(prev[2], out)
        new_inp = inp if out > prev[2] else prev[1]
        self.rid_date_tokens[rid] = (new_date, new_inp, new_out)

    def ingest(self, requests: Iterable[Request], work_type: str, payer: str,
               challenge_start: str) -> None:
        for row in requests:
            rid, ts, inp, out = row[0], row[1], row[2], row[3]
            tup = (inp, out, work_type, payer)
            _remember_max_out(self.seen_global, rid, tup)
            self._note_ts_and_date(ts, rid, inp, out)
            if challenge_start and ts >= challenge_start:
                _remember_max_out(self.seen_challenge, rid, tup)
            if ts >= self.today_start:
                _remember_max_out(self.seen_today, rid, tup)

    def ingest_with_resolver(self, requests: Iterable[Request], sid: str,
                             base_acct: str, overrides: dict,
                             payer_spans_for_sid: list,
                             challenge_start: str) -> None:
        """Ingest requests where work-type depends on (sid, ts) — i.e. overrides."""
        for row in requests:
            rid, ts, inp, out = row[0], row[1], row[2], row[3]
            wt = resolve_override(sid, ts, base_acct, overrides)
            if wt not in ("work", "personal"):
                wt = "unknown"
            payer = payer_for(payer_spans_for_sid, ts)
            tup = (inp, out, wt, payer)
            _remember_max_out(self.seen_global, rid, tup)
            self._note_ts_and_date(ts, rid, inp, out)
            if challenge_start and ts >= challenge_start:
                _remember_max_out(self.seen_challenge, rid, tup)
            if ts >= self.today_start:
                _remember_max_out(self.seen_today, rid, tup)

    def scanned_tokens_by_date(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for date, inp, oo in self.rid_date_tokens.values():
            out[date] = out.get(date, 0) + inp + oo
        return out

    def roll_over_midnight_if_needed(self) -> bool:
        """If the local day has changed, reset the 'today' bucket. Returns True on rollover."""
        current = today_start_iso()
        if current != self.today_start:
            self.today_start = current
            self.seen_today = {}
            return True
        return False

    def build_summary_block(self, seen: dict, payer_labels: set[str]) -> dict:
        work_type_totals = {"work": 0, "personal": 0, "unknown": 0}
        payer_totals = {p: 0 for p in payer_labels}
        for _rid, (inp, out, wt, pr) in seen.items():
            t = inp + out
            if wt not in work_type_totals:
                wt = "unknown"
            work_type_totals[wt] += t
            if pr not in payer_totals:
                pr = "unknown"
            payer_totals[pr] += t
        total = sum(work_type_totals.values())
        return {
            "work_tokens": work_type_totals["work"],
            "personal_tokens": work_type_totals["personal"],
            "unknown_tokens": work_type_totals["unknown"],
            "total_tokens": total,
            "unique_requests": len(seen),
            "by_payer": payer_totals,
        }


def _remember_max_out(store: dict, rid: str, tup: tuple) -> None:
    """Keep the request tuple with the highest output_tokens for each rid."""
    prev = store.get(rid)
    if prev is None or tup[1] > prev[1]:
        store[rid] = tup


# ---------------------------------------------------------------------------
# Codex session parsing & aggregation
#
# Codex stores rollouts at $CODEX_DIR/sessions/YYYY/MM/DD/rollout-*.jsonl.
# Each rollout begins with a session_meta event (cwd, model_provider, ...) and
# emits token_count events with cumulative + per-turn token usage. token_count
# events also carry rate_limits.{primary,secondary} with used_percent and
# resets_at — the *authoritative* signal for Codex 5h/7d utilization, which
# `codex exec` mode strips. The most-recent token_count across all rollouts
# is the live utilization snapshot.
# ---------------------------------------------------------------------------
@dataclass
class CodexSession:
    """One Codex rollout's extracted state."""

    sid: str
    cwd: str
    work_type: str  # work | personal | unknown
    # List of (ts_iso, last_total_tokens) per token_count event with non-null info.
    turn_tokens: list[tuple[str, int]]
    # Latest rate_limits payload observed in this rollout, plus its ISO ts.
    last_rate_limits: dict | None
    last_rate_limits_ts: str


def _classify_codex_cwd(cwd: str, cfg: Config) -> str:
    label = classify_path(cwd, cfg)
    return label if label in ("work", "personal") else "unknown"


def parse_codex_jsonl(filepath: str | Path, cfg: Config) -> CodexSession | None:
    """Parse one Codex rollout. Returns None if it can't be opened or has no meta."""
    sid = ""
    cwd = ""
    turn_tokens: list[tuple[str, int]] = []
    last_rl: dict | None = None
    last_rl_ts: str = ""
    try:
        with open(filepath, "rb") as f:
            for raw in f:
                try:
                    ev = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                ts = str(ev.get("timestamp", ""))
                ev_type = ev.get("type")
                payload = ev.get("payload") or {}
                if ev_type == "session_meta":
                    sid = str(payload.get("id", "") or "")
                    cwd = str(payload.get("cwd", "") or "")
                elif ev_type == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or None
                    if info:
                        last_total = (info.get("last_token_usage") or {}).get("total_tokens", 0)
                        turn_tokens.append((ts, int(last_total or 0)))
                    rl = payload.get("rate_limits") or None
                    if rl:
                        last_rl = rl
                        last_rl_ts = ts
    except OSError:
        return None
    if not sid:
        return None
    return CodexSession(
        sid=sid,
        cwd=cwd,
        work_type=_classify_codex_cwd(cwd, cfg),
        turn_tokens=turn_tokens,
        last_rate_limits=last_rl,
        last_rate_limits_ts=last_rl_ts,
    )


def iter_codex_jsonls(cfg: Config) -> list[str]:
    if not cfg.codex_sessions_dir.is_dir():
        return []
    files: list[str] = []
    for root, _dirs, names in os.walk(cfg.codex_sessions_dir):
        for n in names:
            if n.endswith(".jsonl"):
                files.append(os.path.join(root, n))
    return files


@dataclass
class CodexAggregates:
    """Aggregation state for Codex sessions, parallel to Aggregates."""

    today_start: str = field(default_factory=today_start_iso)
    # Per-window: {work: int, personal: int, unknown: int}
    global_tokens: dict[str, int] = field(
        default_factory=lambda: {"work": 0, "personal": 0, "unknown": 0}
    )
    today_tokens: dict[str, int] = field(
        default_factory=lambda: {"work": 0, "personal": 0, "unknown": 0}
    )
    challenge_tokens: dict[str, int] = field(
        default_factory=lambda: {"work": 0, "personal": 0, "unknown": 0}
    )
    session_count: int = 0
    # Most-recent rate_limits across all rollouts.
    latest_rate_limits: dict | None = None
    latest_rate_limits_ts: str = ""

    def ingest(self, session: CodexSession, challenge_start: str) -> None:
        self.session_count += 1
        wt = session.work_type
        for ts, tok in session.turn_tokens:
            self.global_tokens[wt] += tok
            if challenge_start and ts >= challenge_start:
                self.challenge_tokens[wt] += tok
            if ts >= self.today_start:
                self.today_tokens[wt] += tok
        if session.last_rate_limits and session.last_rate_limits_ts > self.latest_rate_limits_ts:
            self.latest_rate_limits = session.last_rate_limits
            self.latest_rate_limits_ts = session.last_rate_limits_ts

    def build_summary_block(self, totals: dict[str, int]) -> dict:
        return {
            "work_tokens": totals.get("work", 0),
            "personal_tokens": totals.get("personal", 0),
            "unknown_tokens": totals.get("unknown", 0),
            "total_tokens": sum(totals.values()),
        }

    def build_rate_limit_block(self) -> dict | None:
        if not self.latest_rate_limits:
            return None
        primary = self.latest_rate_limits.get("primary") or {}
        secondary = self.latest_rate_limits.get("secondary") or {}
        return {
            "observed_at": self.latest_rate_limits_ts,
            "plan_type": self.latest_rate_limits.get("plan_type"),
            "primary": {
                "used_percent": primary.get("used_percent"),
                "window_minutes": primary.get("window_minutes"),
                "resets_at": primary.get("resets_at"),
            } if primary else None,
            "secondary": {
                "used_percent": secondary.get("used_percent"),
                "window_minutes": secondary.get("window_minutes"),
                "resets_at": secondary.get("resets_at"),
            } if secondary else None,
        }


def codex_full_scan(cfg: Config) -> CodexAggregates:
    """Walk every Codex rollout JSONL and return aggregates. Empty if Codex absent."""
    aggs = CodexAggregates()
    for filepath in iter_codex_jsonls(cfg):
        session = parse_codex_jsonl(filepath, cfg)
        if session is None:
            continue
        aggs.ingest(session, cfg.challenge_start)
    return aggs


# ---------------------------------------------------------------------------
# Bounty tracker (active-minute rate → ETA)
# ---------------------------------------------------------------------------
def compute_bounty_eta(result: dict, cfg: Config) -> dict:
    target = cfg.bounty_target_tokens
    if not target:
        return {"target": 0}

    block = result.get("challenge") or result.get("global") or {}
    current_work = block.get("work_tokens", 0)

    if current_work >= target:
        return {
            "current_work_tokens": current_work,
            "target": target,
            "active_minutes": 0,
            "tokens_per_min": 0,
            "eta_hours": 0.0,
            "cleared": True,
        }

    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.bounty_lookback_days)
    events: list[tuple[datetime, int]] = []
    for fdata in result.get("files", {}).values():
        if fdata.get("acct") != "work":
            continue
        for req in fdata.get("requests", []):
            try:
                ts = datetime.fromisoformat(req[1].rstrip("Z")).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError, IndexError):
                continue
            if ts < cutoff:
                continue
            tokens = (req[2] or 0) + (req[3] or 0)
            if tokens <= 0:
                continue
            events.append((ts, tokens))

    empty = {
        "current_work_tokens": current_work,
        "target": target,
        "active_minutes": 0,
        "tokens_per_min": 0,
        "eta_hours": None,
        "cleared": False,
    }
    if len(events) < 2:
        return empty

    events.sort(key=lambda e: e[0])
    gap = timedelta(minutes=cfg.bounty_session_gap_min)
    burst_start = events[0][0]
    burst_tokens = events[0][1]
    prev_ts = events[0][0]
    total_active = 0.0
    total_tokens = 0

    def flush():
        nonlocal total_active, total_tokens
        dur = (prev_ts - burst_start).total_seconds() / 60.0
        if dur > 0:
            total_active += dur
            total_tokens += burst_tokens

    for ts, tokens in events[1:]:
        if ts - prev_ts >= gap:
            flush()
            burst_start = ts
            burst_tokens = tokens
        else:
            burst_tokens += tokens
        prev_ts = ts
    flush()

    if total_active <= 0 or total_tokens <= 0:
        empty["active_minutes"] = round(total_active, 1)
        return empty

    rate = total_tokens / total_active
    eta_minutes = (target - current_work) / rate
    return {
        "current_work_tokens": current_work,
        "target": target,
        "active_minutes": round(total_active, 1),
        "tokens_per_min": round(rate, 1),
        "eta_hours": round(eta_minutes / 60.0, 2),
        "cleared": False,
    }


# ---------------------------------------------------------------------------
# Disk I/O helpers
# ---------------------------------------------------------------------------
def load_cache(cfg: Config) -> dict:
    try:
        with open(cfg.cache_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically via temp-file + rename.

    The tmp path includes the writer PID so concurrent writers from different
    processes don't clobber each other's half-written files. A second process
    writing the same target is rare (we run one daemon + the one-shot CLI),
    but launchd can briefly overlap two instances during a reload — the
    PID-scoped tmp makes that safe instead of a FileNotFoundError race.
    """
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Full-tree scan (used by scan-tokens.py one-shot and daemon boot)
# ---------------------------------------------------------------------------
def iter_jsonls(cfg: Config) -> tuple[list[str], list[str]]:
    """Return (main_files, subagent_files) under PROJECTS_DIR."""
    main_files: list[str] = []
    subagent_files: list[str] = []
    for root, _dirs, files in os.walk(cfg.projects_dir):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            fp = os.path.join(root, fname)
            if "subagent" in root:
                subagent_files.append(fp)
            else:
                main_files.append(fp)
    return main_files, subagent_files


@dataclass
class FileCacheEntry:
    """Per-file cache: mtime, classification, offsets, and parsed requests."""

    mtime: int
    acct: str
    size: int
    offset: int
    requests: list[Request]

    @classmethod
    def from_dict(cls, d: dict) -> "FileCacheEntry":
        return cls(
            mtime=int(d.get("mtime", 0)),
            acct=d.get("acct") or "unknown",
            size=int(d.get("size", 0)),
            offset=int(d.get("offset", 0)),
            requests=[normalize_request(r) for r in d.get("requests", [])],
        )

    def to_dict(self) -> dict:
        return {
            "mtime": self.mtime,
            "acct": self.acct,
            "size": self.size,
            "offset": self.offset,
            "requests": self.requests,
        }


def full_scan(cfg: Config, prev_cache: dict) -> tuple[dict, Aggregates, int]:
    """Walk every JSONL and build file-level cache + aggregates.

    Returns (new_files_cache, aggregates, files_rescanned).
    """
    prev_files = prev_cache.get("files", {})
    overrides = load_overrides(cfg)
    payer_spans = load_session_payer_spans(cfg)

    aggregates = Aggregates()
    session_classifications: dict[str, str] = {}
    new_files: dict[str, dict] = {}
    files_rescanned = 0

    main_files, subagent_files = iter_jsonls(cfg)

    for filepath in main_files:
        entry, was_rescanned = _scan_main_file(
            filepath, prev_files.get(filepath), cfg, overrides,
        )
        if entry is None:
            continue
        if was_rescanned:
            files_rescanned += 1
        sid = _sid_for(filepath)
        session_classifications[sid] = entry.acct
        new_files[filepath] = entry.to_dict()
        aggregates.ingest_with_resolver(
            entry.requests, sid, entry.acct, overrides,
            payer_spans.get(sid, []), cfg.challenge_start,
        )

    for filepath in subagent_files:
        entry, was_rescanned = _scan_subagent_file(
            filepath, prev_files.get(filepath), cfg, overrides,
            session_classifications,
        )
        if entry is None:
            continue
        if was_rescanned:
            files_rescanned += 1
        sid = _sid_for(filepath)
        parent_sid = _find_parent_sid(filepath, sid, session_classifications)
        lookup_sid = parent_sid or sid
        new_files[filepath] = entry.to_dict()
        aggregates.ingest_with_resolver(
            entry.requests, sid, entry.acct, overrides,
            payer_spans.get(lookup_sid, []), cfg.challenge_start,
        )

    return new_files, aggregates, files_rescanned


def _sid_for(filepath: str) -> str:
    return os.path.basename(filepath).replace(".jsonl", "")


def _find_parent_sid(filepath: str, sid: str, session_classifications: dict[str, str]) -> str | None:
    for uuid in UUID_RE.findall(filepath):
        if uuid != sid and uuid in session_classifications:
            return uuid
    return None


def _scan_main_file(filepath: str, prev: dict | None, cfg: Config,
                    overrides: dict) -> tuple[FileCacheEntry | None, bool]:
    try:
        stat = os.stat(filepath)
    except OSError:
        return None, False
    mtime = int(stat.st_mtime)
    size = stat.st_size
    sid = _sid_for(filepath)
    acct = classify_path(filepath, cfg)

    cache_hit = (
        prev
        and prev.get("mtime") == mtime
        and int(prev.get("size", -1)) == size
        and "requests" in prev
    )
    if cache_hit:
        requests = [normalize_request(r) for r in prev["requests"]]
        if acct is None:
            acct = prev.get("acct") or "unknown"
        offset = int(prev.get("offset", size))
    else:
        signals, offset = parse_jsonl_signals(filepath, 0)
        requests = signals.requests
        if acct is None:
            acct = classify_by_content(signals, cfg)

    if sid in overrides:
        tag, _ = parse_override(overrides[sid])
        if tag:
            acct = tag

    return (
        FileCacheEntry(mtime=mtime, acct=acct or "unknown",
                       size=size, offset=offset, requests=requests),
        not cache_hit,
    )


def _scan_subagent_file(filepath: str, prev: dict | None, cfg: Config,
                        overrides: dict,
                        session_classifications: dict[str, str]
                        ) -> tuple[FileCacheEntry | None, bool]:
    try:
        stat = os.stat(filepath)
    except OSError:
        return None, False
    mtime = int(stat.st_mtime)
    size = stat.st_size
    sid = _sid_for(filepath)

    parent_sid = _find_parent_sid(filepath, sid, session_classifications)
    if parent_sid:
        acct = session_classifications[parent_sid]
    else:
        acct = classify_path(filepath, cfg) or "unknown"
    if sid in overrides:
        tag, _ = parse_override(overrides[sid])
        if tag:
            acct = tag

    cache_hit = (
        prev
        and prev.get("mtime") == mtime
        and int(prev.get("size", -1)) == size
        and "requests" in prev
    )
    if cache_hit:
        requests = [normalize_request(r) for r in prev["requests"]]
        offset = int(prev.get("offset", size))
    else:
        signals, offset = parse_jsonl_signals(filepath, 0)
        requests = signals.requests

    return (
        FileCacheEntry(mtime=mtime, acct=acct, size=size,
                       offset=offset, requests=requests),
        not cache_hit,
    )


# ---------------------------------------------------------------------------
# Pre-scan recovery
# ---------------------------------------------------------------------------
def _load_stats_cache_daily_totals(cfg: Config) -> dict[str, int]:
    """Read ~/.claude/stats-cache.json and return {date: input+output_tokens}."""
    stats_path = cfg.claude_dir / "stats-cache.json"
    try:
        with open(stats_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    daily = data.get("dailyModelTokens")
    if not isinstance(daily, list):
        return {}
    out: dict[str, int] = {}
    for day in daily:
        if not isinstance(day, dict):
            continue
        date = day.get("date")
        if not isinstance(date, str):
            continue
        total = 0
        for v in (day.get("tokensByModel") or {}).values():
            if isinstance(v, (int, float)):
                total += int(v)
        out[date] = out.get(date, 0) + total
    return out


def compute_pre_scan_recovery(cfg: Config, scanned_by_date: dict[str, int]) -> int:
    """Per-date diff against /recover-stats snapshot for dates scanner undercounts.

    Claude Code deleted most pre-April JSONLs during a March 2026 storage
    migration. Surviving subagent files leave per-date scanner data nonzero
    but incomplete. For each date in the recovered stats-cache.json, recover
    max(0, stats_cache_total - scanner_total). Yields zero on dates the
    scanner already covers in full.
    """
    cache_daily = _load_stats_cache_daily_totals(cfg)
    if not cache_daily:
        return 0
    delta = 0
    for date, cache_total in cache_daily.items():
        scanned = scanned_by_date.get(date, 0)
        if cache_total > scanned:
            delta += cache_total - scanned
    return delta


# ---------------------------------------------------------------------------
# Summary assembly
# ---------------------------------------------------------------------------
def build_cache_payload(cfg: Config, new_files: dict, aggregates: Aggregates,
                        files_rescanned: int,
                        codex_aggregates: "CodexAggregates | None" = None) -> dict:
    payer_labels = set(cfg.email_payer_map.values()) | {"unknown"}
    result: dict = {
        "timestamp": int(time.time()),
        "challenge_start": cfg.challenge_start or None,
        "global": aggregates.build_summary_block(aggregates.seen_global, payer_labels),
        "today": aggregates.build_summary_block(aggregates.seen_today, payer_labels),
        "earliest_scanned_ts": aggregates.earliest_global_ts,
        "scanned_tokens_by_date": aggregates.scanned_tokens_by_date(),
        "files_scanned": len(new_files),
        "files_rescanned": files_rescanned,
        "files": new_files,
    }
    if cfg.challenge_start:
        challenge = aggregates.build_summary_block(aggregates.seen_challenge, payer_labels)
        result["challenge"] = challenge
        # Back-compat top-level fields for statusline.sh.
        result["work_tokens"] = challenge["work_tokens"]
        result["personal_tokens"] = challenge["personal_tokens"]
        result["unknown_tokens"] = challenge["unknown_tokens"]
        result["total_tokens"] = challenge["total_tokens"]
        result["unique_requests"] = challenge["unique_requests"]
    if codex_aggregates is not None and codex_aggregates.session_count > 0:
        codex_block: dict = {
            "session_count": codex_aggregates.session_count,
            "global": codex_aggregates.build_summary_block(codex_aggregates.global_tokens),
            "today": codex_aggregates.build_summary_block(codex_aggregates.today_tokens),
        }
        if cfg.challenge_start:
            codex_block["challenge"] = codex_aggregates.build_summary_block(
                codex_aggregates.challenge_tokens
            )
        rl = codex_aggregates.build_rate_limit_block()
        if rl:
            codex_block["rate_limit"] = rl
        result["codex"] = codex_block
    return result


def build_summary_payload(cfg: Config, cache_payload: dict, overrides: dict) -> dict:
    r_sessions, r_ranges = count_redactions(overrides)
    scanned_by_date = cache_payload.get("scanned_tokens_by_date") or {}
    recovered = compute_pre_scan_recovery(cfg, scanned_by_date)
    summary = {
        "timestamp": cache_payload.get("timestamp"),
        "scan_duration_s": cache_payload.get("scan_duration_s"),
        "global": cache_payload["global"],
        "today": cache_payload.get("today"),
        "redactions": {"sessions": r_sessions, "ranges": r_ranges},
        "earliest_scanned_ts": cache_payload.get("earliest_scanned_ts"),
        "recovered_pre_scan_tokens": recovered,
    }
    if "challenge" in cache_payload:
        summary["challenge"] = cache_payload["challenge"]
    if "codex" in cache_payload:
        summary["codex"] = cache_payload["codex"]
    if cfg.bounty_target_tokens:
        summary["bounty"] = compute_bounty_eta(cache_payload, cfg)
    return summary
