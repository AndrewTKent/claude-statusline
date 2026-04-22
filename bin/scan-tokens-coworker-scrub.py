#!/usr/bin/env python3
"""Flag any message mentioning a coworker name. Range-based redaction.

For every JSONL in scope:
  - For each user/assistant message whose text mentions a coworker name,
    emit a range covering that message's timestamp ± 60s, category personal-aside.
  - Uncommon names match on bare first name (Ashesh, Hema, Tushar, etc.)
  - Common names require a work-context signal nearby (PR, merged, review,
    code, @, coram, slack, commit, ticket, sync, standup, etc.)
  - Full-name matches always count regardless of commonness.

Writes staging output to ~/.claude/token-scan-coworker-staging.json for
review + merge via scan-tokens-merge.py.
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
OUT = Path.home() / ".claude" / "token-scan-coworker-staging.json"
CUTOFF = "2026-03-01T00:00:00Z"
MERGE_WINDOW_SEC = 120

# Uncommon first names — safe to match on bare first name
UNCOMMON_FIRST = [
    "Ashesh", "Hema", "Tushar", "Garvit", "Piyush", "Manik", "Jatin",
    "Slava", "Bijoy", "Piet", "Shubham", "Balazs", "Hemanth",
]

# Common first names — require work-context signal nearby
COMMON_FIRST = [
    "Oliver", "Connor", "Kyle", "Trevor", "Matt", "Matthew", "Daniel",
    "Ted", "Tyler", "David", "Peter", "Eric", "Stu", "Stuart",
    "Caleb", "Jacob",
]

# Full names — match these whenever they appear (first + last)
FULL_NAMES = [
    "Ashesh Jain",
    "Hema Koppula",
    "Tushar Singla",
    "Kyle Boylan",
    "Trevor Robertson",
    "Matt Neilsen",
    "Matthew Neilsen",
    "Daniel Gonik",
    "Tyler Gregerson",
    "Tyler Carnathan",
    "Bijoy Saha",
    "David Lu",
    "Peter Ondruska",
    "Eric Carey",
    "Shubham Bahukhandi",
    "Stu Waters",
    "Stuart Waters",
    "Balazs Kovacs",
    "Hemanth Jonnalagadda",
]

# Last names standalone — uncommon enough to match bare
LAST_NAMES = [
    "Koppula", "Singla", "Boylan", "Gonik", "Gregerson", "Carnathan",
    "Ondruska", "Bahukhandi", "Jonnalagadda", "Neilsen",
]

# Slack / GitHub handles
HANDLES = [
    "@hema", "@tushar", "@ashesh", "@garvit", "@piyush", "@manik",
    "@jatin", "@slava", "@bijoy", "@piet", "@connor", "@kyle",
    "@trevor", "@oliver", "@daniel", "@ted", "@tyler", "@david",
    "@peter", "@eric", "@stu", "@shubham", "@balazs", "@hemanth",
    "@caleb", "@jacob",
]

# Work-context signals that qualify a common first name
WORK_CONTEXT = re.compile(
    r"\b(PR|pull\s+request|merged|review|approved?|commit|commits|ticket|"
    r"AGI-\d+|EDGE-\d+|slack|DM|sync|standup|coram|PIP|perf|@|"
    r"code|repo|branch|"
    r"coworker|teammate|engineer|manager|CEO|CTO|VP|"
    r"eng-updates|voice-agent|logistics-app|logistics-core|4d-view|kb-server)\b",
    re.IGNORECASE,
)

# Precompile uncommon-first and last-name patterns with word boundaries
UNCOMMON_RX = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in UNCOMMON_FIRST + LAST_NAMES) + r")\b",
    re.IGNORECASE,
)

FULL_NAME_RX = re.compile(
    r"(" + "|".join(re.escape(n) for n in FULL_NAMES) + r")",
    re.IGNORECASE,
)

COMMON_FIRST_RX = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in COMMON_FIRST) + r")\b",
    re.IGNORECASE,
)

HANDLE_RX = re.compile(
    r"(" + "|".join(re.escape(h) for h in HANDLES) + r")\b",
    re.IGNORECASE,
)


def extract_text(msg_content):
    """Only user-authored or assistant text blocks. No tool payloads."""
    if isinstance(msg_content, str):
        return msg_content
    if isinstance(msg_content, list):
        parts = []
        for c in msg_content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts)
    return ""


def detect_names(text: str) -> set[str]:
    """Return a set of matched name strings found in text."""
    if not text:
        return set()
    hits = set()
    for m in FULL_NAME_RX.finditer(text):
        hits.add(m.group(1).lower())
    for m in UNCOMMON_RX.finditer(text):
        hits.add(m.group(1).lower())
    for m in HANDLE_RX.finditer(text):
        hits.add(m.group(1).lower())
    # Common first names only count with work context
    for m in COMMON_FIRST_RX.finditer(text):
        # Look at a 200-char window around the match for work context
        start = max(0, m.start() - 200)
        end = min(len(text), m.end() + 200)
        window = text[start:end]
        if WORK_CONTEXT.search(window):
            hits.add(m.group(1).lower())
    return hits


def scan_jsonl(path: Path) -> list[dict]:
    """Return list of {ts, ...} for messages that mention coworkers."""
    hits = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = d.get("timestamp", "")
                if not ts or ts < CUTOFF:
                    continue
                if d.get("type") not in ("user", "assistant"):
                    continue
                text = extract_text(d.get("message", {}).get("content", ""))
                if not text or text.startswith("<command-") or "<local-command-stdout>" in text:
                    continue
                found = detect_names(text)
                if found:
                    hits.append({"ts": ts, "names": list(found)})
    except OSError:
        pass
    return hits


def merge_hits_to_ranges(hits: list[dict]) -> list[dict]:
    if not hits:
        return []
    hits = sorted(hits, key=lambda h: h["ts"])
    ranges = []
    current = None
    for h in hits:
        ts_dt = datetime.fromisoformat(h["ts"].replace("Z", "+00:00"))
        if current is None:
            current = {"from_dt": ts_dt, "to_dt": ts_dt}
            continue
        delta = (ts_dt - current["to_dt"]).total_seconds()
        if delta <= MERGE_WINDOW_SEC:
            current["to_dt"] = ts_dt
        else:
            ranges.append(current)
            current = {"from_dt": ts_dt, "to_dt": ts_dt}
    if current:
        ranges.append(current)
    out = []
    for r in ranges:
        frm = (r["from_dt"] - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to = (r["to_dt"] + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({
            "from": frm,
            "to": to,
            "redact": "personal-aside",
            "note": "coworker-mention",
        })
    return out


def main():
    print(f"Scanning for coworker mentions since {CUTOFF}")
    all_files = list(PROJECTS.rglob("*.jsonl"))
    print(f"Total JSONLs: {len(all_files)}")

    by_sid = defaultdict(list)
    scanned = 0
    for p in all_files:
        is_sub = "subagent" in str(p)
        # Attach subagent hits to parent session SID
        if is_sub:
            sid_key = p.parent.parent.name
        else:
            sid_key = p.stem
        hits = scan_jsonl(p)
        if hits:
            by_sid[sid_key].extend(hits)
        scanned += 1
        if scanned % 1000 == 0:
            print(f"  scanned {scanned}/{len(all_files)}...")

    output = {}
    total_ranges = 0
    for sid, hits in by_sid.items():
        ranges = merge_hits_to_ranges(hits)
        if not ranges:
            continue
        output[sid] = {"ranges": ranges, "coworker_hits": len(hits)}
        total_ranges += len(ranges)

    with open(OUT, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Sessions scanned: {len(all_files)}")
    print(f"Sessions with coworker mentions: {len(output)}")
    print(f"Total ranges proposed: {total_ranges}")
    print(f"Total hit messages: {sum(len(h) for h in by_sid.values())}")
    print(f"\nStaging file: {OUT}")


if __name__ == "__main__":
    main()
