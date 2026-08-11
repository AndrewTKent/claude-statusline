#!/usr/bin/env python3
"""Build a redacted archive of Claude Code session JSONLs for external sharing.

Reads `~/.claude/token-scan-overrides.json` for redact ranges. Copies every
main-session JSONL under `~/.claude/projects/` into an output directory,
replacing any message whose timestamp falls inside a redact range with a
placeholder. Local `~/.claude/projects/` is never modified.

Usage:
    scan-tokens-redact-archive.py <output-dir> [--window-start ISO] [--window-end ISO]

Output layout mirrors input:
    <output-dir>/sessions/<project-dir>/<session>.jsonl
    <output-dir>/manifest.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make sibling scan-tokens-core importable (classifier lives there)
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

OVERRIDES_FILE = Path.home() / ".claude" / "token-scan-overrides.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
STATUSLINE_CONF = Path.home() / ".claude" / "statusline.conf"


def load_classifier_env() -> None:
    """Source ~/.claude/statusline.conf so scan_tokens_core sees WORK_PATHS etc."""
    import os
    import re
    if not STATUSLINE_CONF.exists():
        return
    try:
        with open(STATUSLINE_CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line)
                if not m:
                    continue
                k, v = m.group(1), m.group(2)
                # Strip matching surrounding quotes
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                if k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


# Source classifier config at import time
load_classifier_env()

PLACEHOLDER_USER = "[REDACTED]"
PLACEHOLDER_ASSISTANT = "[REDACTED]"

# Content-level patterns that force a line to be redacted regardless of timestamp.
# Belt-and-suspenders: catches sensitive tokens that leak outside explicit redact ranges.
import re as _re
CONTENT_SENSITIVE_PATTERNS = [
    (_re.compile(r"sk-ant-api03-[A-Za-z0-9_-]{20,}"), "api-key"),
    (_re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"), "api-key"),
    (_re.compile(r"sk-or-v1-[a-f0-9]{40,}"), "api-key"),
    (_re.compile(r"AIzaSy[A-Za-z0-9_-]{30,}"), "api-key"),
    (_re.compile(r"cfut_[A-Za-z0-9]{20,}"), "api-token"),
    (_re.compile(r"ghp_[A-Za-z0-9]{30,}"), "github-token"),
    (_re.compile(r"xox[bpars]-[A-Za-z0-9-]{10,}"), "slack-token"),
    (_re.compile(r"-----BEGIN [A-Z ]+-----"), "private-key"),
    (_re.compile(r"\bdidge\b", _re.IGNORECASE), "prior-employer"),
    (_re.compile(r"didge-nx-dispute"), "prior-employer"),
    (_re.compile(r"nextera[-_]?robotics", _re.IGNORECASE), "prior-employer"),
    (_re.compile(r"nextera(?:[-_\s]robotics|[-_]ws|[-_]org)", _re.IGNORECASE), "prior-employer"),
    (_re.compile(r"\bnextera\b", _re.IGNORECASE), "prior-employer"),
]


def content_has_sensitive(text: str) -> str | None:
    """Return reason code if text matches a sensitive pattern, else None."""
    if not isinstance(text, str):
        return None
    for pat, reason in CONTENT_SENSITIVE_PATTERNS:
        if pat.search(text):
            return reason
    return None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def load_overrides_raw() -> dict:
    try:
        with open(OVERRIDES_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def load_redact_map() -> dict[str, list[tuple[datetime, datetime, str]]]:
    overrides = load_overrides_raw()
    out: dict[str, list[tuple[datetime, datetime, str]]] = {}
    for sid, info in overrides.items():
        if not isinstance(info, dict):
            continue
        for r in info.get("ranges", []):
            if not isinstance(r, dict) or not r.get("redact"):
                continue
            frm = parse_iso(r.get("from"))
            to = parse_iso(r.get("to"))
            if frm is None or to is None:
                continue
            reason = r.get("redact", "redacted")
            out.setdefault(sid, []).append((frm, to, reason))
    return out


def explicit_tag(overrides: dict, sid: str) -> str | None:
    """Return the explicit 'tag' field for a session, or None if untagged."""
    info = overrides.get(sid)
    if isinstance(info, dict):
        tag = info.get("tag")
        if tag in ("work", "personal"):
            return tag
    return None


def session_has_work_ranges(overrides: dict, sid: str) -> bool:
    """True if the session has any `tag: work` range — means part of it is work
    even if the session default is personal."""
    info = overrides.get(sid)
    if not isinstance(info, dict):
        return False
    for r in info.get("ranges") or []:
        if isinstance(r, dict) and r.get("tag") == "work":
            return True
    return False


def work_ranges_for(overrides: dict, sid: str) -> list[tuple[datetime, datetime]]:
    """Return list of (from, to) datetime tuples for tag='work' ranges in the session."""
    info = overrides.get(sid)
    if not isinstance(info, dict):
        return []
    out = []
    for r in info.get("ranges") or []:
        if not isinstance(r, dict) or r.get("tag") != "work":
            continue
        frm = parse_iso(r.get("from"))
        to = parse_iso(r.get("to"))
        if frm is not None and to is not None:
            out.append((frm, to))
    return out


def classify_session(src: Path, overrides: dict) -> str:
    """Return 'work' | 'personal' | 'unknown' for a session file.

    Priority: explicit override tag > path-based > content-based.
    """
    sid = src.stem
    tag = explicit_tag(overrides, sid)
    if tag:
        return tag
    # Try the full classifier from scan_tokens_core
    try:
        import scan_tokens_core as core
        cfg = core.load_config()
        signals, _ = core.parse_jsonl_signals(src)
        # Check each cwd and path for explicit work/personal match
        for p in list(signals.cwds) + list(signals.paths):
            hit = core.classify_path(p, cfg)
            if hit:
                return hit
        return core.classify_by_content(signals, cfg)
    except Exception:
        # If core import fails, fall back to cwd-based heuristic
        try:
            for line in open(src):
                d = json.loads(line)
                cwd = d.get("cwd", "")
                if isinstance(cwd, str):
                    cwd_l = cwd.lower()
                    if "/personal/" in cwd_l or "/desktop/" in cwd_l:
                        return "personal"
                    if "/work/" in cwd_l:
                        return "work"
                break
        except Exception:
            pass
        return "unknown"


def in_any_range(ts: datetime, ranges: list[tuple[datetime, datetime, str]]) -> str | None:
    for frm, to, reason in ranges:
        if frm <= ts <= to:
            return reason
    return None


def redact_line(line: str, ranges: list[tuple[datetime, datetime, str]]) -> tuple[str, bool, bool]:
    """Parse a JSONL line. Returns (output_line, was_redacted, drop_line).

    Behavior:
      - Regular message in a redact range → content replaced with placeholder.
      - `last-prompt` / `permission-mode` records in a session with any ranges → dropped entirely.
      - `file-history-snapshot` → timestamp pulled from `snapshot.timestamp`.
      - Anything else without a timestamp → passed through unchanged.
    """
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return line, False, False

    rec_type = d.get("type")

    # Metadata records with no top-level timestamp: drop them in any session that has
    # redactions. They duplicate / re-quote primary content (last-prompt), describe
    # backed-up file paths that may contain sensitive identifiers (file-history-snapshot),
    # or are pure session-state metadata (permission-mode).
    if rec_type in ("last-prompt", "permission-mode", "file-history-snapshot") and ranges:
        return "", False, True

    ts_str = d.get("timestamp")

    ts = parse_iso(ts_str) if ts_str else None
    if ts is None:
        return line, False, False
    reason = in_any_range(ts, ranges)
    if reason is None:
        return line, False, False

    # Replace message content with a placeholder. Preserve shape for downstream tools.
    msg = d.get("message")
    if isinstance(msg, dict):
        role = msg.get("role")
        placeholder = PLACEHOLDER_ASSISTANT if role == "assistant" else PLACEHOLDER_USER
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = f"{placeholder} ({reason})"
        elif isinstance(content, list):
            # Anthropic content blocks: replace each text block; keep structure
            new_content = []
            for block in content:
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue
                bt = block.get("type")
                if bt == "text":
                    new_content.append({"type": "text", "text": f"{placeholder} ({reason})"})
                elif bt == "tool_use":
                    # Drop tool-use params — they may contain the sensitive content
                    new_content.append({
                        "type": "tool_use",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": {"_redacted": reason},
                    })
                elif bt == "tool_result":
                    new_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": f"{placeholder} ({reason})",
                    })
                else:
                    # Unknown block type — stringify-redact to be safe
                    new_content.append({"type": bt or "unknown", "text": f"{placeholder} ({reason})"})
            msg["content"] = new_content
    # Strip ALL fields that can carry pasted content or path/branch names that
    # re-leak the redacted material. Keep only audit-safe metadata.
    if "toolUseResult" in d:
        d["toolUseResult"] = {"_redacted": reason}
    if "attachment" in d:
        d["attachment"] = {"_redacted": reason}
    if "snapshot" in d:
        d["snapshot"] = {"_redacted": reason}
    if "content" in d and isinstance(d.get("content"), str):
        # queue-operation / system / other top-level content fields
        d["content"] = f"[REDACTED] ({reason})"
    if "text" in d and isinstance(d.get("text"), str):
        d["text"] = f"[REDACTED] ({reason})"
    if "summary" in d and isinstance(d.get("summary"), str):
        d["summary"] = f"[REDACTED] ({reason})"
    if "data" in d:
        # `progress` records wrap a nested message payload here; we don't want to
        # preserve its content, only the fact that something happened.
        d["data"] = {"_redacted": reason}
    if "output" in d:
        d["output"] = {"_redacted": reason}
    for path_field in ("cwd", "gitBranch", "slug", "lastPrompt"):
        if path_field in d:
            d[path_field] = "[REDACTED]"
    d["_redacted"] = reason
    return json.dumps(d, ensure_ascii=False) + "\n", True, False


def extract_session_id(jsonl_path: Path) -> str:
    # Session files are named <uuid>.jsonl; subagent files live in a subdir
    return jsonl_path.stem


def session_tokens(jsonl_path: Path) -> tuple[int, int]:
    """Rough i+o token total for the manifest. Dedup by requestId."""
    seen: dict[str, tuple[int, int]] = {}
    try:
        with open(jsonl_path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = d.get("requestId")
                msg = d.get("message", {})
                usage = msg.get("usage", {}) if isinstance(msg, dict) else {}
                i_tok = usage.get("input_tokens", 0) or 0
                o_tok = usage.get("output_tokens", 0) or 0
                if not rid:
                    continue
                prev = seen.get(rid, (0, 0))
                # Keep max out tokens (streaming writes multiple rows)
                if o_tok >= prev[1]:
                    seen[rid] = (i_tok, o_tok)
    except OSError:
        return 0, 0
    return (sum(i for i, _ in seen.values()), sum(o for _, o in seen.values()))


def session_meta(jsonl_path: Path) -> dict:
    """First-line metadata for manifest."""
    meta = {"first_timestamp": None, "last_timestamp": None, "cwd": None, "branch": None}
    try:
        with open(jsonl_path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = d.get("timestamp")
                if ts:
                    if meta["first_timestamp"] is None:
                        meta["first_timestamp"] = ts
                    meta["last_timestamp"] = ts
                if meta["cwd"] is None and d.get("cwd"):
                    meta["cwd"] = d.get("cwd")
                if meta["branch"] is None and d.get("gitBranch"):
                    meta["branch"] = d.get("gitBranch")
    except OSError:
        pass
    return meta


def has_branch_leaking_reason(ranges: list[tuple[datetime, datetime, str]]) -> bool:
    """True if any redact range is for content where the branch/cwd/slug could re-leak it."""
    # Any redacted session gets its cwd/gitBranch/slug scrubbed on every record — these
    # fields persist across the whole session file regardless of timestamp.
    return bool(ranges)


BRANCH_SCRUB_FIELDS = ("gitBranch", "slug")
# cwd is less sensitive (usually just `~/personal/code/trips`) but the user explicitly
# said "didge absolutely needs to be redacted" and trips' branch often embeds it.
# We scrub cwd too, since it also appears in last-prompt records and is not useful
# for audit (manifest records session→project→path separately).
CWD_SCRUB_FIELD = "cwd"


def scrub_session_wide_fields(d: dict) -> None:
    """Remove branch/slug that might contain a redacted identifier.
    cwd is preserved (it's the project directory, not content) but only the bare path —
    any branch encoded in a cwd like '/worktrees/didge-*' would leak, so scrub if needed.
    """
    for f in BRANCH_SCRUB_FIELDS:
        if f in d:
            d[f] = "[REDACTED]"
    cwd = d.get(CWD_SCRUB_FIELD)
    if isinstance(cwd, str) and ("didge" in cwd.lower() or "worktrees" in cwd.lower()):
        d[CWD_SCRUB_FIELD] = "[REDACTED]"


def force_redact_line_by_content(line: str) -> tuple[str, bool]:
    """Scan a serialized JSONL line for sensitive content. If present, replace
    the record's message/content/data fields with a redacted placeholder.
    Returns (new_line, was_forced).
    """
    reason = content_has_sensitive(line)
    if not reason:
        return line, False
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return line, False
    # Scrub everything that could carry the sensitive content
    if "message" in d and isinstance(d.get("message"), dict):
        role = d["message"].get("role", "user")
        d["message"]["content"] = f"[REDACTED] ({reason})"
    if "content" in d and isinstance(d.get("content"), str):
        d["content"] = f"[REDACTED] ({reason})"
    if "text" in d and isinstance(d.get("text"), str):
        d["text"] = f"[REDACTED] ({reason})"
    if "data" in d:
        d["data"] = {"_redacted": reason}
    if "attachment" in d:
        d["attachment"] = {"_redacted": reason}
    if "snapshot" in d:
        d["snapshot"] = {"_redacted": reason}
    if "toolUseResult" in d:
        d["toolUseResult"] = {"_redacted": reason}
    if "output" in d:
        d["output"] = {"_redacted": reason}
    for pf in ("cwd", "gitBranch", "slug", "lastPrompt"):
        if pf in d:
            d[pf] = "[REDACTED]"
    d["_redacted"] = d.get("_redacted") or reason
    return json.dumps(d, ensure_ascii=False) + "\n", True


def line_in_work_window(line: str, work_windows: list[tuple[datetime, datetime]]) -> bool:
    """Return True if the record's timestamp falls in any work window."""
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return False
    ts_str = d.get("timestamp")
    if ts_str is None and d.get("type") == "file-history-snapshot":
        snap = d.get("snapshot") or {}
        ts_str = snap.get("timestamp") if isinstance(snap, dict) else None
    ts = parse_iso(ts_str) if ts_str else None
    if ts is None:
        return False
    for frm, to in work_windows:
        if frm <= ts <= to:
            return True
    return False


def copy_and_redact(
    src: Path,
    dst: Path,
    ranges: list[tuple[datetime, datetime, str]],
    work_windows: list[tuple[datetime, datetime]] | None = None,
) -> int:
    """Copy src to dst applying redactions. If work_windows is given, only
    emit records whose timestamp falls within one of those windows (used for
    personal-tagged sessions that have carved-out work stretches)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    redacted_count = 0
    scrub_session_fields = has_branch_leaking_reason(ranges)
    with open(src) as rh, open(dst, "w") as wh:
        for line in rh:
            # Work-window filter (for personal sessions with work carve-outs):
            # drop any record that isn't inside a work range.
            if work_windows and not line_in_work_window(line, work_windows):
                continue

            if ranges:
                new_line, was_redacted, drop = redact_line(line, ranges)
                if drop:
                    redacted_count += 1
                    continue
                if not new_line:
                    continue
                # Session-wide scrub for branch/slug even on un-redacted records
                if scrub_session_fields and not was_redacted:
                    try:
                        d = json.loads(new_line)
                        scrub_session_wide_fields(d)
                        new_line = json.dumps(d, ensure_ascii=False) + "\n"
                    except json.JSONDecodeError:
                        pass
            else:
                new_line = line
                was_redacted = False

            # Defense-in-depth: content-pattern scan for sensitive tokens/identifiers
            # even in sessions without explicit redact ranges.
            if not was_redacted:
                new_line, was_forced = force_redact_line_by_content(new_line)
                if was_forced:
                    redacted_count += 1

            wh.write(new_line if new_line.endswith("\n") else new_line + "\n")
            if was_redacted:
                redacted_count += 1
    return redacted_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("output_dir", help="Target directory for redacted archive")
    ap.add_argument("--window-start", help="ISO timestamp; skip sessions with last_timestamp before this")
    ap.add_argument("--window-end", help="ISO timestamp; skip sessions with first_timestamp after this")
    ap.add_argument("--only-tag", choices=["work", "personal"], help="Only archive sessions classified as this tag")
    ap.add_argument("--dry-run", action="store_true", help="Report what would be written but don't write")
    args = ap.parse_args()

    out_dir = Path(args.output_dir).expanduser().resolve()
    sessions_out = out_dir / "sessions"
    manifest_path = out_dir / "manifest.json"
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        sessions_out.mkdir(parents=True, exist_ok=True)

    win_start = parse_iso(args.window_start)
    win_end = parse_iso(args.window_end)
    redact_map = load_redact_map()
    overrides_raw = load_overrides_raw()
    print(f"Loaded {sum(len(v) for v in redact_map.values())} redact ranges across {len(redact_map)} sessions")
    if args.only_tag:
        print(f"Filtering to tag={args.only_tag}")

    manifest = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "window_start": args.window_start,
        "window_end": args.window_end,
        "only_tag": args.only_tag,
        "redact_ranges_total": sum(len(v) for v in redact_map.values()),
        "sessions": {},
    }

    total_files = 0
    total_redacted_msgs = 0
    skipped_window = 0
    skipped_tag = 0

    for src in PROJECTS_DIR.glob("*/[a-z0-9]*.jsonl"):
        # Skip subagents
        if "subagents" in src.parts:
            continue
        sid = extract_session_id(src)
        meta = session_meta(src)
        first = parse_iso(meta["first_timestamp"])
        last = parse_iso(meta["last_timestamp"])
        if win_start and last and last < win_start:
            skipped_window += 1
            continue
        if win_end and first and first > win_end:
            skipped_window += 1
            continue

        # Tag-based filter. If the session default is the opposite tag but the
        # session has carve-out ranges matching the requested tag, include it
        # and emit only records within those windows.
        session_tag = classify_session(src, overrides_raw) if args.only_tag else None
        work_windows: list[tuple[datetime, datetime]] = []
        if args.only_tag == "work" and session_tag != "work":
            # Include if there are any tag:work ranges
            if session_has_work_ranges(overrides_raw, sid):
                work_windows = work_ranges_for(overrides_raw, sid)
            else:
                skipped_tag += 1
                continue
        elif args.only_tag and session_tag != args.only_tag:
            skipped_tag += 1
            continue

        ranges = redact_map.get(sid, [])
        project_dir = src.parent.name
        rel = f"{project_dir}/{src.name}"
        dst = sessions_out / project_dir / src.name

        if args.dry_run:
            # Count redactions without writing
            r_count = 0
            if ranges:
                with open(src) as fh:
                    for line in fh:
                        _, was, dropped = redact_line(line, ranges)
                        if was or dropped:
                            r_count += 1
        else:
            r_count = copy_and_redact(src, dst, ranges, work_windows or None)

        i_tok, o_tok = session_tokens(src)
        manifest["sessions"][sid] = {
            "path": rel,
            "project": project_dir,
            "first_timestamp": meta["first_timestamp"],
            "last_timestamp": meta["last_timestamp"],
            "cwd": meta["cwd"],
            "branch": meta["branch"],
            "tag": session_tag or classify_session(src, overrides_raw),
            "input_tokens": i_tok,
            "output_tokens": o_tok,
            "redact_ranges": len(ranges),
            "messages_redacted": r_count,
            "work_carve_out_windows": [
                {"from": frm.isoformat(), "to": to.isoformat()}
                for frm, to in work_windows
            ] if work_windows else None,
        }
        total_files += 1
        total_redacted_msgs += r_count

    if not args.dry_run:
        tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(manifest, f, indent=2)
        tmp.rename(manifest_path)

    print(f"Processed {total_files} session files")
    print(f"Skipped (outside window): {skipped_window}")
    print(f"Skipped (wrong tag): {skipped_tag}")
    print(f"Messages redacted: {total_redacted_msgs}")
    if args.dry_run:
        print("(dry-run — no files written)")
    else:
        print(f"Archive: {sessions_out}")
        print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
