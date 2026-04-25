#!/usr/bin/env python3
from __future__ import annotations
"""Report all redact ranges across all sessions.

Reads ~/.claude/token-scan-overrides.json for entries with `ranges[]` containing
a `redact` field. For each matching range, locates the affected messages in the
session JSONL and prints a preview.

Read-only. Does not modify any files.

Usage:
    scan-tokens-redactions.py              # report across all sessions
    scan-tokens-redactions.py <sid>        # show one session's redactions in detail
"""

import json
import sys
from pathlib import Path

OVERRIDES_FILE = Path.home() / ".claude" / "token-scan-overrides.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"


def load_overrides() -> dict:
    try:
        with open(OVERRIDES_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def find_session_file(sid: str) -> Path | None:
    """Find main session JSONL by SID."""
    for p in PROJECTS_DIR.rglob(f"{sid}.jsonl"):
        if "subagent" not in str(p):
            return p
    return None


def extract_messages_in_range(path: Path, frm: str | None, to: str | None) -> list[dict]:
    """Return user messages whose timestamp falls in [frm, to]."""
    matches = []
    try:
        with open(path) as fh:
            for line_num, line in enumerate(fh, 1):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "user":
                    continue
                ts = d.get("timestamp", "")
                if frm and ts < frm:
                    continue
                if to and ts > to:
                    continue

                content = d.get("message", {}).get("content", "")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "")
                            break

                # Skip tool-result wrappers and empty content — only count
                # actual user-authored or user-pasted text.
                if not text or not text.strip():
                    continue
                if text.startswith("<command-") or "<local-command-stdout>" in text:
                    continue
                if "tool_use_id" in text and len(text) < 300:
                    continue

                matches.append({
                    "line": line_num,
                    "uuid": d.get("uuid", ""),
                    "timestamp": ts,
                    "text": text,
                })
    except OSError:
        pass
    return matches


def format_redaction_report(sid: str, rng: dict, messages: list[dict], detail: bool = False) -> str:
    lines = []
    category = rng.get("redact", "?")
    note = rng.get("note", "")
    frm = rng.get("from", "∞")
    to = rng.get("to", "∞")
    lines.append(f"  [{category}] {frm[:19] if frm != '∞' else '∞':19s} → {to[:19] if to != '∞' else '∞':19s}  ({len(messages)} msgs)")
    if note:
        lines.append(f"    note: {note}")
    if detail:
        for m in messages:
            preview = m["text"][:160].replace("\n", " / ")
            lines.append(f"    [{m['line']:>5d}] {m['timestamp'][:19]}  {preview}")
    else:
        for m in messages[:2]:
            preview = m["text"][:120].replace("\n", " / ")
            lines.append(f"    preview: {preview}")
        if len(messages) > 2:
            lines.append(f"    ... and {len(messages)-2} more")
    return "\n".join(lines)


def main():
    overrides = load_overrides()
    target_sid = sys.argv[1] if len(sys.argv) > 1 else None

    total_ranges = 0
    total_messages = 0
    sessions_with_redactions = 0

    for sid, entry in overrides.items():
        if not isinstance(entry, dict):
            continue
        ranges = entry.get("ranges", [])
        if not ranges:
            continue
        redact_ranges = [r for r in ranges if r.get("redact")]
        if not redact_ranges:
            continue

        # Match prefix if user gave a partial SID
        if target_sid and not sid.startswith(target_sid):
            continue

        path = find_session_file(sid)
        if not path:
            print(f"\n{sid}: session file not found")
            continue

        proj = path.parent.name
        print(f"\n{sid}  ({proj})")

        for rng in redact_ranges:
            messages = extract_messages_in_range(path, rng.get("from"), rng.get("to"))
            total_ranges += 1
            total_messages += len(messages)
            print(format_redaction_report(sid, rng, messages, detail=bool(target_sid)))

        sessions_with_redactions += 1

    if sessions_with_redactions == 0:
        print("No redaction ranges found." + (f" (filter: {target_sid})" if target_sid else ""))
        return

    print(f"\n{'='*60}")
    print(f"Sessions with redactions: {sessions_with_redactions}")
    print(f"Total ranges:             {total_ranges}")
    print(f"Total messages flagged:   {total_messages}")


if __name__ == "__main__":
    main()
