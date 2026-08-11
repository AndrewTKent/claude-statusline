#!/usr/bin/env python3
"""Deterministic pattern-based autoflagging for bounty submission.

Scans all JSONLs since a cutoff date (both main sessions and subagents),
regex-matches sensitive patterns in user messages AND assistant text, and
writes proposed flag ranges to a staging file.

Does NOT write to the live overrides file. Review the staging output,
then merge into ~/.claude/token-scan-overrides.json manually.

Output: ~/.claude/token-scan-autoflag-staging.json
"""

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
OUT = Path.home() / ".claude" / "token-scan-autoflag-staging.json"
CUTOFF = "2026-03-01T00:00:00Z"

# Range-fuzzing window: merge matches within N seconds into one range
MERGE_WINDOW_SEC = 120

# Categories mapped to regex patterns.
# Each pattern is case-insensitive unless it starts with (?-i).
# Category must be one of the 5 existing categories in the export script.
PATTERNS = {
    "sensitive-data": [
        # API keys / tokens (tight — only well-known prefixes)
        (r"\b(sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|glpat-[A-Za-z0-9_-]{20})\b", "api token"),
        (r"Bearer\s+[A-Za-z0-9._-]{30,}\b", "bearer token"),
        # AWS keys
        (r"\b(AKIA[0-9A-Z]{16})\b", "aws access key"),
        (r"aws_secret_access_key\s*=\s*['\"]?[A-Za-z0-9/+=]{30,}", "aws secret"),
        # JWTs
        (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "jwt"),
        # Emails (exclude anthropic.com, common open-source addresses, and tool IDs)
        # Require surrounding whitespace/punctuation — not inside random strings
        (r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9._%+-]{1,}@(?!anthropic\.com\b)(?!example\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9_])", "email address"),
        # Phone numbers — require explicit formatting (parens or dashes) to exclude random digit runs
        (r"(?<![0-9])(?:\+1[-.\s]?)?\(?[2-9][0-9]{2}\)[-.\s]?[0-9]{3}[-.\s]?[0-9]{4}(?![0-9])", "phone number (parens)"),
        (r"(?<![0-9])(?:\+1[-.\s]?)?[2-9][0-9]{2}[-.][0-9]{3}[-.][0-9]{4}(?![0-9])", "phone number (dashed)"),
    ],
    "personal-aside": [
        # Legal matter entities
        (r"\b(Didge\s*NX|Nextera\s*Robotics|Nextera|didge-nx)\b", "Didge NX / Nextera"),
        (r"\bLana\s+(V\.?\s+)?Graf\b", "Lana Graf"),
        (r"\bAlex\s+Rand\b", "Alex Rand"),
        (r"\bBTA\b(?!\w)", "BTA"),
        (r"\b(Elena\s+Volkova|Elena\s+Kladova|Tristan\s+Davis|Nate\s+Price)\b", "legal-matter person"),
        (r"\b(Liebert\s+Cassidy\s+Whitmore|LCW|Lichten\s*&\s*Liss-Riordan|LLR|Fair\s+Work\s+P\.?C\.?|Sheff\s*&\s*Cook|Gordon\s+Law\s+Group|Conforto\s+Law|Ortiz\s*&\s*Moeslinger|Pasternak\s+Law|Ottinger|Rudy\s*,?\s*Exelrod|McGuinn\s*,?\s*Hillsman)\b", "law firm name"),
        (r"\b(phantom\s*stock|promissory\s*estoppel|W-?2C|Wage\s*Act|clawback|Reuter\s+v\.\s+Methuen|Segal\s+v\.\s+Genitrix|Cooke\s+v\.\s+Patient\s+Edu|Sutton\s+v\.\s+Jordan)\b", "legal terminology"),
        (r"§\s*(148B?|925|558\.1|221|150)\b", "legal statute"),
        (r"\$\s*(72,?152|103,?450|69,?500|12,?500|864,?000|1,?600,?000|320,?000,?000)\b", "legal-matter dollar amount"),
        (r"\b(IFC|World\s+Bank|Project\s+NEWTON)\b", "Didge counterparty org"),
        # Travel / races (tighter — only explicit race names)
        (r"\b(Ironman|Tarawera|Ironman\s+Nice|Ironman\s+California)\b", "race/ironman"),
    ],
    "personal-comms": [
        # Quoted emails typically start with "On <date>, <person> wrote:"
        (r"On\s+\w{3},?\s+\w{3}\s+\d+,?\s*\d{4}[^\n]{0,50}\s+wrote:", "quoted email header"),
        (r"\b(imessage|iMessage)\s+(from|with|to)\s+", "imessage reference"),
    ],
}

# Flatten to (category, pattern, note) tuples
FLAT_PATTERNS = []
for cat, pats in PATTERNS.items():
    # Remap placeholder categories to personal-aside (the 5 allowed categories)
    actual_cat = cat.replace("-maps-to-personal-aside", "").replace("external-counterparty", "personal-aside").replace("compensation", "personal-aside")
    if actual_cat not in {"rude-to-claude", "sensitive-data", "personal-aside", "personal-comms", "other"}:
        actual_cat = "personal-aside"
    for pat, note in pats:
        FLAT_PATTERNS.append((actual_cat, re.compile(pat, re.IGNORECASE | re.MULTILINE), note))


def extract_text(msg_content):
    """Extract ONLY user-authored or assistant-authored plain text.

    Skips tool_use, tool_result, and structured payloads — those contain
    tool IDs, base64 data, and JSON that produce regex false positives
    and aren't what a human reads in the transcript.
    """
    if isinstance(msg_content, str):
        return msg_content
    if isinstance(msg_content, list):
        parts = []
        for c in msg_content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts)
    return ""


def scan_jsonl(path: Path, cutoff: str) -> list[dict]:
    """Return list of {ts, category, note, text_preview, line_num} for matches in one file."""
    hits = []
    try:
        with open(path) as f:
            for line_num, line in enumerate(f, 1):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = d.get("timestamp", "")
                if not ts or ts < cutoff:
                    continue
                msg_type = d.get("type")
                if msg_type not in ("user", "assistant"):
                    continue
                text = extract_text(d.get("message", {}).get("content", ""))
                if not text:
                    continue
                # Skip pure tool-result wrappers (short, structured)
                if text.startswith("<command-") or "<local-command-stdout>" in text:
                    continue
                for cat, rx, note in FLAT_PATTERNS:
                    if rx.search(text):
                        hits.append({
                            "ts": ts,
                            "category": cat,
                            "note": note,
                            "msg_type": msg_type,
                            "line_num": line_num,
                            "preview": text[:200].replace("\n", " "),
                        })
    except OSError:
        pass
    return hits


def merge_hits_to_ranges(hits: list[dict], merge_window_sec: int) -> list[dict]:
    """Merge consecutive hits within merge_window_sec into single ranges.

    Returns list of {from, to, redact, note} range objects.
    """
    if not hits:
        return []
    # Sort by timestamp
    hits = sorted(hits, key=lambda h: h["ts"])
    ranges = []
    current = None
    for h in hits:
        ts_dt = datetime.fromisoformat(h["ts"].replace("Z", "+00:00"))
        if current is None:
            current = {
                "from_dt": ts_dt,
                "to_dt": ts_dt,
                "category": h["category"],
                "notes": {h["note"]},
            }
            continue
        # Merge if within window AND same category
        delta = (ts_dt - current["to_dt"]).total_seconds()
        if delta <= merge_window_sec and h["category"] == current["category"]:
            current["to_dt"] = ts_dt
            current["notes"].add(h["note"])
        else:
            ranges.append(current)
            current = {
                "from_dt": ts_dt,
                "to_dt": ts_dt,
                "category": h["category"],
                "notes": {h["note"]},
            }
    if current:
        ranges.append(current)
    # Expand each range by ±60 sec so we catch the surrounding conversation turn
    out = []
    for r in ranges:
        frm = (r["from_dt"] - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to = (r["to_dt"] + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({
            "from": frm,
            "to": to,
            "redact": r["category"],
            "note": "autoflag: " + ", ".join(sorted(r["notes"])),
        })
    return out


def main():
    cutoff = CUTOFF
    print(f"Scanning JSONLs since {cutoff}")
    print(f"Patterns: {len(FLAT_PATTERNS)} across {len(set(p[0] for p in FLAT_PATTERNS))} categories")

    # Collect all in-scope JSONLs
    all_files = []
    for p in PROJECTS.rglob("*.jsonl"):
        all_files.append(p)
    print(f"Total JSONLs to scan: {len(all_files)}")

    # Scan each, accumulate by session-id (main session's SID)
    by_sid = defaultdict(list)
    scanned = 0
    for p in all_files:
        # SID is filename without .jsonl
        sid = p.stem
        # For subagents, the parent session SID is in the parent dir name (which includes 'subagents')
        # or in the parent's parent dir. We flag per-file SID for now; merge step can handle.
        is_sub = "subagent" in str(p)
        if is_sub:
            # Subagent files live under .../subagents/agent-XXX.jsonl
            # The parent session SID is the dir above 'subagents'
            parent_sid = p.parent.parent.name
            sid_key = parent_sid  # attach subagent hits to parent session
        else:
            sid_key = sid
        hits = scan_jsonl(p, cutoff)
        if hits:
            by_sid[sid_key].extend(hits)
        scanned += 1
        if scanned % 500 == 0:
            print(f"  scanned {scanned}/{len(all_files)}...")

    # Build per-session range lists
    output = {}
    total_ranges = 0
    total_hits = 0
    cat_counts = defaultdict(int)
    for sid, hits in by_sid.items():
        ranges = merge_hits_to_ranges(hits, MERGE_WINDOW_SEC)
        if not ranges:
            continue
        output[sid] = {"ranges": ranges, "autoflag_hit_count": len(hits)}
        total_ranges += len(ranges)
        total_hits += len(hits)
        for r in ranges:
            cat_counts[r["redact"]] += 1

    # Write staging file
    with open(OUT, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Sessions scanned: {len(all_files)}")
    print(f"Sessions with hits: {len(output)}")
    print(f"Total pattern hits: {total_hits}")
    print(f"Total ranges proposed: {total_ranges}")
    print(f"Categories:")
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}")
    print(f"\nStaging file: {OUT}")
    print(f"Review, then merge into ~/.claude/token-scan-overrides.json")


if __name__ == "__main__":
    main()
