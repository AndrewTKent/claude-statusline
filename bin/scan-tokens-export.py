#!/usr/bin/env python3
"""Export sanitized copies of session JSONLs for submission.

Non-destructive: reads from ~/.claude/projects/, writes to <output-dir>/.
Source files are never modified.

For each redact range in the overrides file, user messages falling inside the
range have their text content replaced with "[REDACTED:<category>:<id>]".
Original content is written to <output-dir>/_vault/<id>.json for reversibility.

Per-message redaction IDs: each user message in a range gets its own short ID.

Usage:
    scan-tokens-export.py <output-dir>              # export all flagged sessions
    scan-tokens-export.py <output-dir> <sid>...     # export specific sessions
"""

import json
import secrets
import shutil
import sys
from pathlib import Path

OVERRIDES_FILE = Path.home() / ".claude" / "token-scan-overrides.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

REDACT_CATEGORIES = {
    "rude-to-claude",
    "personal-aside",
    "personal-comms",
    "sensitive-data",
    "other",
}


def load_overrides() -> dict:
    try:
        with open(OVERRIDES_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def find_session_file(sid: str) -> Path | None:
    for p in PROJECTS_DIR.rglob(f"{sid}.jsonl"):
        if "subagent" not in str(p):
            return p
    return None


def gen_redact_id() -> str:
    return "r_" + secrets.token_hex(2)


def find_matching_range(ts: str, ranges: list) -> dict | None:
    """Return the last range with a `redact` field that matches ts, or None."""
    match = None
    for rng in ranges:
        if not rng.get("redact"):
            continue
        frm = rng.get("from")
        to = rng.get("to")
        if frm and ts < frm:
            continue
        if to and ts > to:
            continue
        match = rng
    return match


def should_redact_message(entry: dict) -> str | None:
    """Return the text content if this user message should be redacted, else None.

    Skips tool results, command wrappers, and empty content.
    """
    msg = entry.get("message", {})
    content = msg.get("content", "")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                break

    if not text or not text.strip():
        return None
    if text.startswith("<command-") or "<local-command-stdout>" in text:
        return None
    if "tool_use_id" in text and len(text) < 300:
        return None
    return text


def rewrite_user_message(entry: dict, category: str, rid: str, original_text: str) -> dict:
    """Mutate entry's message.content to a redaction placeholder. Returns entry."""
    msg = entry.get("message", {})
    content = msg.get("content", "")

    if isinstance(content, str):
        msg["content"] = f"[REDACTED:{category}:{rid}]"
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                c["text"] = f"[REDACTED:{category}:{rid}]"
                break

    return entry


def export_session(source: Path, dest: Path, vault_dir: Path, ranges: list) -> dict:
    """Copy source JSONL to dest, rewriting flagged user messages.

    Returns {redacted_count, vault_entries} summary.
    """
    vault_entries = []

    with open(source) as fh_in, open(dest, "w") as fh_out:
        for line in fh_in:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                fh_out.write(line)
                continue

            if d.get("type") != "user":
                fh_out.write(line)
                continue

            ts = d.get("timestamp", "")
            rng = find_matching_range(ts, ranges)
            if not rng:
                fh_out.write(line)
                continue

            # Only redact messages with actual user-authored/pasted text.
            # Tool results and command wrappers pass through unchanged.
            original = should_redact_message(d)
            if original is None:
                fh_out.write(line)
                continue

            category = rng.get("redact", "other")
            rid = gen_redact_id()
            modified = rewrite_user_message(d, category, rid, original)

            # Write vault entry
            vault_path = vault_dir / f"{rid}.json"
            with open(vault_path, "w") as vf:
                json.dump({
                    "redact_id": rid,
                    "category": category,
                    "session_id": source.stem,
                    "source_file": str(source),
                    "timestamp": ts,
                    "uuid": d.get("uuid"),
                    "note": rng.get("note", ""),
                    "original_text": original,
                }, vf, indent=2)
            vault_entries.append({"id": rid, "category": category, "ts": ts})

            fh_out.write(json.dumps(modified) + "\n")

    return {"redacted_count": len(vault_entries), "entries": vault_entries}


def main():
    if len(sys.argv) < 2:
        print("Usage: scan-tokens-export.py <output-dir> [sid...]")
        sys.exit(1)

    output_dir = Path(sys.argv[1]).expanduser().resolve()
    sid_filter = set(sys.argv[2:]) if len(sys.argv) > 2 else None

    if output_dir.exists():
        print(f"ERROR: output directory already exists: {output_dir}")
        print("Pick a new path or delete the existing one.")
        sys.exit(1)

    output_dir.mkdir(parents=True)
    vault_dir = output_dir / "_vault"
    vault_dir.mkdir()

    overrides = load_overrides()
    exports = []

    for sid, entry in overrides.items():
        if not isinstance(entry, dict):
            continue
        ranges = entry.get("ranges", [])
        redact_ranges = [r for r in ranges if r.get("redact")]
        if not redact_ranges:
            continue
        if sid_filter and not any(sid.startswith(s) for s in sid_filter):
            continue

        source = find_session_file(sid)
        if not source:
            print(f"  SKIP {sid}: session file not found")
            continue

        # Mirror source path structure under output_dir
        rel = source.relative_to(PROJECTS_DIR)
        dest = output_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        summary = export_session(source, dest, vault_dir, redact_ranges)
        exports.append({
            "sid": sid,
            "source": str(source),
            "dest": str(dest),
            **summary,
        })
        print(f"  {sid}: {summary['redacted_count']} messages redacted → {dest.relative_to(output_dir)}")

    manifest = {
        "output_dir": str(output_dir),
        "session_count": len(exports),
        "total_redactions": sum(e["redacted_count"] for e in exports),
        "exports": exports,
    }
    with open(output_dir / "MANIFEST.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Exported {manifest['session_count']} sessions")
    print(f"Total redactions:   {manifest['total_redactions']}")
    print(f"Output:             {output_dir}")
    print(f"Vault (originals):  {vault_dir}")
    print(f"Manifest:           {output_dir}/MANIFEST.json")

    if manifest["session_count"] == 0:
        print("\nNo sessions exported. Add redact ranges to ~/.claude/token-scan-overrides.json.")


if __name__ == "__main__":
    main()
