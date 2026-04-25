#!/usr/bin/env python3
from __future__ import annotations
"""Scan Claude Code session JSONLs and bucket tokens into two dimensions.

Walks ~/.claude/projects/, reads assistant-message usage from every JSONL
(main sessions + subagents), deduplicates by requestId (matching /stats),
and classifies each request along TWO independent dimensions:

  1. work-type (what were you doing?) — primary dimension.
     Values: "work" / "personal" / "unknown".
     Classified by (a) path substrings, (b) keyword hits in user messages,
     (c) per-session manual overrides. NEVER derived from email login.

  2. payer (which Claude plan was logged in?) — secondary dimension.
     Values: any label from EMAIL_PAYER_MAP, or "unknown".
     Classified by looking up the session+timestamp in ~/.claude/session-accounts.json
     and mapping the email to a payer label.

Work-type is what a bounty like "did you do $X of work this month" cares
about. Payer is useful for "which plan paid for this" — e.g. "I did work
on my personal Max plan." The two are orthogonal.

Configuration is entirely via environment variables (see bin/statusline-config.sh
and config/statusline.conf.example). This script ships with NO hard-coded
employers, emails, or personal topics.

Token metric: input_tokens + output_tokens (matches /stats).

Incremental: stores per-file mtime + classification cache so subsequent runs
only re-parse changed files.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", str(Path.home() / ".claude")))
PROJECTS_DIR = CLAUDE_DIR / "projects"
CACHE_FILE = CLAUDE_DIR / "token-scan-cache.json"
SUMMARY_FILE = CLAUDE_DIR / "token-scan-summary.json"
OVERRIDES_FILE = CLAUDE_DIR / "token-scan-overrides.json"
SESSION_ACCOUNTS_FILE = CLAUDE_DIR / "session-accounts.json"


# ---------------------------------------------------------------------------
# Configuration (all via env — no hardcoded employer/topic keywords)
# ---------------------------------------------------------------------------
def _split_csv(val: str) -> list[str]:
    return [s.strip().lower() for s in val.split(",") if s.strip()]


# Challenge window (for the optional bounty tracker). "" disables.
CHALLENGE_START = os.environ.get("CHALLENGE_START", "")

# Path substrings that signal work vs personal. Empty defaults — the user
# supplies these via statusline.conf.
WORK_PATHS = _split_csv(os.environ.get("WORK_PATHS", ""))
PERSONAL_PATHS = _split_csv(os.environ.get("PERSONAL_PATHS", ""))

# Keywords in the user prompt that signal work vs personal.
WORK_KEYWORDS = _split_csv(os.environ.get("WORK_KEYWORDS", ""))
PERSONAL_KEYWORDS = _split_csv(os.environ.get("PERSONAL_KEYWORDS", ""))

# Email → payer label map. Format: "work:me@company.com personal:me@gmail.com".
# Values not in the map fall to "unknown".
EMAIL_PAYER_MAP = {}
for pair in (os.environ.get("EMAIL_PAYER_MAP") or "").split():
    if ":" in pair:
        label, email = pair.split(":", 1)
        EMAIL_PAYER_MAP[email.strip().lower()] = label.strip()

# Bounty tracker (optional). 0 disables.
BOUNTY_TARGET_TOKENS = int(os.environ.get("BOUNTY_TARGET_TOKENS", "0") or "0")
BOUNTY_LOOKBACK_DAYS = int(os.environ.get("BOUNTY_LOOKBACK_DAYS", "3") or "3")
BOUNTY_SESSION_GAP_MIN = int(os.environ.get("BOUNTY_SESSION_GAP_MIN", "30") or "30")


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PATH_RE = re.compile(r"/Users/[^\s\"'`]+")


# ---------------------------------------------------------------------------
# Overrides (manual session-level reclassification)
# ---------------------------------------------------------------------------
REDACT_CATEGORIES = {
    "rude-to-claude",
    "personal-aside",
    "personal-comms",
    "sensitive-data",
    "other",
}


def load_overrides() -> dict:
    """Manual session classification overrides (sid -> str or object).

    Two forms:
      "sid": "work"            — whole session tagged.
      "sid": {                 — object form with optional time ranges.
          "tag": "work",
          "ranges": [
              {"from": "...Z", "to": "...Z", "tag": "personal"},
              {"from": "...Z", "to": "...Z", "redact": "sensitive-data"}
          ]
      }
    """
    try:
        with open(OVERRIDES_FILE) as f:
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
def load_session_payer_spans() -> dict:
    """Load ~/.claude/session-accounts.json and return {sid: [(from, to, payer)]}.

    The hook that writes session-accounts.json is user-supplied; see the repo's
    hooks/ directory for an example implementation. Email→payer mapping is
    controlled by EMAIL_PAYER_MAP.
    """
    try:
        with open(SESSION_ACCOUNTS_FILE) as f:
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
            payer = EMAIL_PAYER_MAP.get(email, "unknown")
            norm.append((span.get("from"), span.get("to"), payer))
        if norm:
            norm.sort(key=lambda s: s[0] or "")
            result[sid] = norm
    return result


def payer_for(spans: list, ts: str) -> str:
    """Find the payer whose span covers ts. Returns 'unknown' if no span matches."""
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
# Work-type classification (path + content heuristics; NOT email)
# ---------------------------------------------------------------------------
def classify_path(path: str) -> str | None:
    """Return 'work' | 'personal' | None based on path substrings."""
    p = path.lower()
    # Personal first (more specific in typical setups).
    for needle in PERSONAL_PATHS:
        if needle in p:
            return "personal"
    for needle in WORK_PATHS:
        if needle in p:
            return "work"
    return None


def extract_session_signals(filepath: str) -> tuple[set, set, str | None, list]:
    """Parse a JSONL once, returning cwds, tool-paths, user-text, and requests."""
    cwds: set = set()
    paths: set = set()
    user_parts: list[str] = []
    user_chars = 0
    USER_TEXT_CAP = 20_000
    requests: list = []

    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                cwd = entry.get("cwd")
                if isinstance(cwd, str):
                    cwds.add(cwd)

                etype = entry.get("type")
                if etype == "user" and user_chars < USER_TEXT_CAP:
                    content = entry.get("message", {}).get("content", "")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                text = c.get("text", "")
                                break
                    if text:
                        snippet = text[:2000]
                        user_parts.append(snippet)
                        user_chars += len(snippet)
                elif etype == "assistant":
                    msg = entry.get("message", {})
                    usage = msg.get("usage") or {}
                    rid = entry.get("requestId") or msg.get("id", "")
                    ts = entry.get("timestamp", "")
                    if rid and ts and usage:
                        requests.append([
                            rid, ts,
                            usage.get("input_tokens", 0) or 0,
                            usage.get("output_tokens", 0) or 0,
                        ])
                    for block in msg.get("content", []) or []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        inp = block.get("input", {})
                        for k in ("file_path", "path", "cwd"):
                            v = inp.get(k)
                            if isinstance(v, str) and v.startswith("/"):
                                paths.add(v)
                        cmd = inp.get("command", "")
                        if isinstance(cmd, str):
                            for p in PATH_RE.findall(cmd):
                                paths.add(p)
    except OSError:
        pass

    user_text = " ".join(user_parts) if user_parts else None
    return cwds, paths, user_text, requests


def classify_by_content(cwds: set, paths: set, user_text: str | None) -> str:
    """Fallback classifier by path + keyword hits. Returns work/personal/unknown."""
    all_paths = cwds | paths
    work = 0
    personal = 0
    for p in all_paths:
        pl = p.lower()
        if any(n in pl for n in WORK_PATHS):
            work += 1
        if any(n in pl for n in PERSONAL_PATHS):
            personal += 1
    if user_text:
        lower = user_text.lower()
        work += 3 * sum(1 for k in WORK_KEYWORDS if k in lower)
        personal += 3 * sum(1 for k in PERSONAL_KEYWORDS if k in lower)
    if work > personal:
        return "work"
    if personal > work:
        return "personal"
    return "unknown"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def load_cache() -> dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def scan(prev_cache: dict) -> dict:
    """Walk all JSONLs, classify, dedup by requestId.

    Per-file cache format: {mtime, acct, requests: [[rid, ts, inp, out], ...]}.
    Per-request classification attaches TWO labels: work-type and payer.
    """
    prev_files = prev_cache.get("files", {})
    overrides = load_overrides()
    payer_spans = load_session_payer_spans()

    session_classifications: dict[str, str] = {}  # sid -> work-type fallback

    # seen[rid] = (inp, out, work_type, payer)
    seen_global: dict[str, tuple] = {}
    seen_challenge: dict[str, tuple] = {}
    files_rescanned = 0
    new_files: dict = {}

    main_files: list[str] = []
    subagent_files: list[str] = []
    for root, _dirs, files in os.walk(PROJECTS_DIR):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            fp = os.path.join(root, fname)
            if "subagent" in root:
                subagent_files.append(fp)
            else:
                main_files.append(fp)

    def ingest(requests, base_acct, sid, spans_for_sid):
        for rid, ts, inp, out in requests:
            # Work-type (path+content+overrides — NEVER email)
            work_type = resolve_override(sid, ts, base_acct, overrides)
            if work_type not in ("work", "personal"):
                work_type = "unknown"
            # Payer (email span lookup)
            payer = payer_for(spans_for_sid, ts)

            prev_seen = seen_global.get(rid)
            if prev_seen is None or out > prev_seen[1]:
                seen_global[rid] = (inp, out, work_type, payer)
            if CHALLENGE_START and ts >= CHALLENGE_START:
                prev_seen = seen_challenge.get(rid)
                if prev_seen is None or out > prev_seen[1]:
                    seen_challenge[rid] = (inp, out, work_type, payer)

    # Main sessions
    for filepath in main_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue
        mtime_int = int(mtime)
        sid = os.path.basename(filepath).replace(".jsonl", "")

        acct = classify_path(filepath)
        prev = prev_files.get(filepath)
        if prev and prev.get("mtime") == mtime_int and "requests" in prev:
            requests = prev["requests"]
            if acct is None:
                acct = prev.get("acct") or "unknown"
        else:
            cwds, paths, user_text, raw_reqs = extract_session_signals(filepath)
            requests = [[r, t, i, o] for (r, t, i, o) in raw_reqs]
            if acct is None:
                acct = classify_by_content(cwds, paths, user_text)
            files_rescanned += 1

        if sid in overrides:
            session_tag, _ = parse_override(overrides[sid])
            if session_tag:
                acct = session_tag

        session_classifications[sid] = acct
        new_files[filepath] = {"mtime": mtime_int, "acct": acct, "requests": requests}
        ingest(requests, acct, sid, payer_spans.get(sid, []))

    # Subagents (inherit work-type from parent; payer looked up against parent sid)
    for filepath in subagent_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue
        mtime_int = int(mtime)
        sid = os.path.basename(filepath).replace(".jsonl", "")

        parent_sid = None
        for uuid in UUID_RE.findall(filepath):
            if uuid != sid and uuid in session_classifications:
                parent_sid = uuid
                break

        if parent_sid:
            acct = session_classifications[parent_sid]
        else:
            acct = classify_path(filepath) or "unknown"
        if sid in overrides:
            session_tag, _ = parse_override(overrides[sid])
            if session_tag:
                acct = session_tag

        prev = prev_files.get(filepath)
        if prev and prev.get("mtime") == mtime_int and "requests" in prev:
            requests = prev["requests"]
        else:
            _, _, _, raw_reqs = extract_session_signals(filepath)
            requests = [[r, t, i, o] for (r, t, i, o) in raw_reqs]
            files_rescanned += 1

        new_files[filepath] = {"mtime": mtime_int, "acct": acct, "requests": requests}
        lookup_sid = parent_sid if parent_sid else sid
        ingest(requests, acct, lookup_sid, payer_spans.get(lookup_sid, []))

    # Aggregate
    payer_labels = set(EMAIL_PAYER_MAP.values()) | {"unknown"}

    def build_block(seen):
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

    challenge_block = build_block(seen_challenge) if CHALLENGE_START else None
    result = {
        "timestamp": int(time.time()),
        "challenge_start": CHALLENGE_START or None,
        "global": build_block(seen_global),
        "files_scanned": len(new_files),
        "files_rescanned": files_rescanned,
        "files": new_files,
    }
    if challenge_block:
        result["challenge"] = challenge_block
        # Back-compat: statusline can read top-level work_tokens/personal_tokens
        # as the challenge totals when CHALLENGE_START is set.
        result["work_tokens"] = challenge_block["work_tokens"]
        result["personal_tokens"] = challenge_block["personal_tokens"]
        result["unknown_tokens"] = challenge_block["unknown_tokens"]
        result["total_tokens"] = challenge_block["total_tokens"]
        result["unique_requests"] = challenge_block["unique_requests"]
    return result


# ---------------------------------------------------------------------------
# Bounty tracker (active-minute rate → ETA)
# ---------------------------------------------------------------------------
def compute_bounty_eta(result: dict) -> dict:
    from datetime import datetime, timedelta, timezone

    target = BOUNTY_TARGET_TOKENS
    block = result.get("challenge") or result.get("global") or {}
    current_work = block.get("work_tokens", 0)

    if not target:
        return {"target": 0}

    if current_work >= target:
        return {
            "current_work_tokens": current_work,
            "target": target,
            "active_minutes": 0,
            "tokens_per_min": 0,
            "eta_hours": 0.0,
            "cleared": True,
        }

    cutoff = datetime.now(timezone.utc) - timedelta(days=BOUNTY_LOOKBACK_DAYS)
    events = []
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

    if len(events) < 2:
        return {
            "current_work_tokens": current_work,
            "target": target,
            "active_minutes": 0,
            "tokens_per_min": 0,
            "eta_hours": None,
            "cleared": False,
        }

    events.sort(key=lambda e: e[0])
    gap = timedelta(minutes=BOUNTY_SESSION_GAP_MIN)
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
        return {
            "current_work_tokens": current_work,
            "target": target,
            "active_minutes": round(total_active, 1),
            "tokens_per_min": 0,
            "eta_hours": None,
            "cleared": False,
        }

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
# main()
# ---------------------------------------------------------------------------
def main():
    start = time.time()
    prev_cache = load_cache()
    result = scan(prev_cache)
    result["scan_duration_s"] = round(time.time() - start, 2)

    with open(CACHE_FILE, "w") as f:
        json.dump(result, f)
        f.write("\n")

    overrides = load_overrides()
    r_sessions, r_ranges = count_redactions(overrides)
    summary = {
        "timestamp": result.get("timestamp"),
        "scan_duration_s": result.get("scan_duration_s"),
        "global": result["global"],
        "redactions": {"sessions": r_sessions, "ranges": r_ranges},
    }
    if "challenge" in result:
        summary["challenge"] = result["challenge"]
    if BOUNTY_TARGET_TOKENS:
        summary["bounty"] = compute_bounty_eta(result)

    with open(SUMMARY_FILE, "w") as f:
        json.dump(summary, f)
        f.write("\n")

    if "--quiet" not in sys.argv:
        print(f"Scanned {result['files_scanned']} files "
              f"({result['files_rescanned']} rescanned) in {result['scan_duration_s']:.1f}s")
        blocks = [("Global (all-time)", result["global"])]
        if "challenge" in result:
            blocks.append((f"Challenge (since {CHALLENGE_START})", result["challenge"]))
        for label, data in blocks:
            total = data["total_tokens"]
            if total == 0:
                continue
            print(f"\n=== {label} ===")
            print(f"Unique requests: {data['unique_requests']:,}")
            print(f"Work:     {data['work_tokens']/1e6:>8.2f}M  ({data['work_tokens']/total*100:.1f}%)")
            print(f"Personal: {data['personal_tokens']/1e6:>8.2f}M  ({data['personal_tokens']/total*100:.1f}%)")
            if data["unknown_tokens"] > 0:
                print(f"Unknown:  {data['unknown_tokens']/1e6:>8.2f}M  ({data['unknown_tokens']/total*100:.1f}%)")
            print(f"Total:    {total/1e6:>8.2f}M")
            bp = data.get("by_payer") or {}
            if bp and any(v for v in bp.values()):
                print("Payer breakdown:")
                for payer, tok in sorted(bp.items(), key=lambda kv: -kv[1]):
                    if tok == 0:
                        continue
                    print(f"  {payer:<16} {tok/1e6:>8.2f}M  ({tok/total*100:.1f}%)")


if __name__ == "__main__":
    main()
