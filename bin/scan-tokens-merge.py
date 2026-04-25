#!/usr/bin/env python3
"""Merge agent-produced flag ranges into the live overrides file.

Reads all `~/.claude/bounty-redaction-output/batch-*-results.json` and
optionally `~/.claude/token-scan-autoflag-staging.json`, dedupes overlapping
ranges, preserves existing overrides, and writes to
`~/.claude/token-scan-overrides.json`.

Creates a timestamped backup of the overrides file before writing.
Each new range carries `source` metadata for traceability.

Usage:
    scan-tokens-merge.py              # dry-run summary only
    scan-tokens-merge.py --write      # actually write to overrides
    scan-tokens-merge.py --revert-source agent-batch-07  # remove ranges from one source
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

OVERRIDES = Path.home() / ".claude" / "token-scan-overrides.json"
AUTOFLAG_STAGING = Path.home() / ".claude" / "token-scan-autoflag-staging.json"
COWORKER_STAGING = Path.home() / ".claude" / "token-scan-coworker-staging.json"
AGENT_OUTPUT_DIR = Path.home() / ".claude" / "bounty-redaction-output"
VALID_CATEGORIES = {"rude-to-claude", "sensitive-data", "personal-aside", "personal-comms", "other"}


def load_overrides():
    try:
        with open(OVERRIDES) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def backup_overrides():
    if not OVERRIDES.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = OVERRIDES.with_suffix(f".json.bak-merge-{ts}")
    dst.write_bytes(OVERRIDES.read_bytes())
    return dst


def load_autoflag_staging():
    """Each session -> list of ranges with source='autoflag'."""
    return _load_staging(AUTOFLAG_STAGING, "autoflag")


def load_coworker_staging():
    """Each session -> list of ranges with source='coworker-scan'."""
    return _load_staging(COWORKER_STAGING, "coworker-scan")


def _load_staging(path: Path, source: str):
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for sid, entry in data.items():
        ranges = entry.get("ranges", [])
        tagged = []
        for r in ranges:
            r2 = dict(r)
            r2["source"] = source
            tagged.append(r2)
        if tagged:
            out[sid] = tagged
    return out


def load_agent_outputs():
    """Each session -> list of ranges, tagged with source='agent-batch-XX'."""
    out = defaultdict(list)
    if not AGENT_OUTPUT_DIR.exists():
        return out
    for p in sorted(AGENT_OUTPUT_DIR.glob("batch-*-results.json")):
        try:
            with open(p) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            print(f"  WARN: couldn't read {p.name}")
            continue
        source = p.stem.replace("-results", "")  # batch-07-results -> batch-07
        source = f"agent-{source}"
        by_session = data.get("by_session", {})
        for sid, ranges in by_session.items():
            for r in ranges:
                if not isinstance(r, dict):
                    continue
                cat = r.get("redact", "other")
                if cat not in VALID_CATEGORIES:
                    print(f"  WARN: {source} used invalid category '{cat}' — coercing to 'other'")
                    cat = "other"
                tagged = {
                    "from": r.get("from"),
                    "to": r.get("to"),
                    "redact": cat,
                    "note": r.get("note", ""),
                    "source": source,
                }
                if tagged["from"] and tagged["to"]:
                    out[sid].append(tagged)
    return out


def merge_ranges(existing: list, new: list) -> list:
    """Merge new ranges into existing. Do not dedupe (each range has provenance).

    Returns combined list.
    """
    return list(existing) + list(new)


def revert_source(source: str):
    """Remove all ranges from a given source from the overrides file."""
    overrides = load_overrides()
    backup = backup_overrides()
    print(f"Backup: {backup}")
    removed = 0
    for sid, entry in list(overrides.items()):
        if not isinstance(entry, dict):
            continue
        ranges = entry.get("ranges", [])
        kept = [r for r in ranges if r.get("source") != source]
        removed += len(ranges) - len(kept)
        entry["ranges"] = kept
        if not entry.get("ranges") and not entry.get("tag"):
            del overrides[sid]
    with open(OVERRIDES, "w") as f:
        json.dump(overrides, f, indent=2)
    print(f"Removed {removed} ranges from source '{source}'")


def main():
    write = "--write" in sys.argv
    revert = None
    for i, arg in enumerate(sys.argv):
        if arg == "--revert-source" and i + 1 < len(sys.argv):
            revert = sys.argv[i + 1]
            break

    if revert:
        revert_source(revert)
        return

    print("Loading sources...")
    existing = load_overrides()
    print(f"  Existing overrides: {len(existing)} sessions")

    autoflag = load_autoflag_staging()
    print(f"  Autoflag staging:   {len(autoflag)} sessions")

    coworker = load_coworker_staging()
    print(f"  Coworker staging:   {len(coworker)} sessions, {sum(len(v) for v in coworker.values())} ranges")

    agent = load_agent_outputs()
    print(f"  Agent outputs:      {len(agent)} sessions, {sum(len(v) for v in agent.values())} ranges")

    # Build combined view per session
    all_sessions = set(existing.keys()) | set(autoflag.keys()) | set(coworker.keys()) | set(agent.keys())
    combined = {}
    total_new_ranges = 0
    cat_counts = defaultdict(int)
    by_source = defaultdict(int)

    for sid in all_sessions:
        existing_ranges = existing.get(sid, {}).get("ranges", []) if isinstance(existing.get(sid), dict) else []
        autoflag_ranges = autoflag.get(sid, [])
        coworker_ranges = coworker.get(sid, [])
        agent_ranges = agent.get(sid, [])

        # De-dupe against existing: skip ranges with exact same (from, to, redact)
        existing_keys = {(r.get("from"), r.get("to"), r.get("redact")) for r in existing_ranges}
        new_agent = [r for r in agent_ranges if (r.get("from"), r.get("to"), r.get("redact")) not in existing_keys]
        new_autoflag = [r for r in autoflag_ranges if (r.get("from"), r.get("to"), r.get("redact")) not in existing_keys]
        new_coworker = [r for r in coworker_ranges if (r.get("from"), r.get("to"), r.get("redact")) not in existing_keys]

        merged = list(existing_ranges) + new_autoflag + new_coworker + new_agent
        total_new_ranges += len(new_autoflag) + len(new_coworker) + len(new_agent)

        for r in new_autoflag + new_coworker + new_agent:
            cat_counts[r.get("redact", "?")] += 1
            by_source[r.get("source", "?")] += 1

        if merged:
            entry = dict(existing.get(sid, {})) if isinstance(existing.get(sid), dict) else {}
            entry["ranges"] = merged
            combined[sid] = entry

    print(f"\nNew ranges to add: {total_new_ranges}")
    print(f"Sessions after merge: {len(combined)}")
    print(f"\nBy category (new only):")
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}")
    print(f"\nBy source (new only):")
    for src, n in sorted(by_source.items(), key=lambda x: -x[1])[:30]:
        print(f"  {src}: {n}")

    if not write:
        print("\n(dry run — pass --write to apply)")
        return

    backup = backup_overrides()
    print(f"\nBackup written: {backup}")
    with open(OVERRIDES, "w") as f:
        json.dump(combined, f, indent=2)
    print(f"Overrides written: {OVERRIDES}")
    print(f"Total sessions: {len(combined)}")
    print(f"Total ranges:   {sum(len(e.get('ranges', [])) for e in combined.values())}")


if __name__ == "__main__":
    main()
