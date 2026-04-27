#!/usr/bin/env python3
"""Build a metadata-only archive of Claude Code sessions for external submission.

Strips every content-carrying field (user/assistant message text, tool inputs/outputs,
attachments, hook content, snapshots, task notifications) and keeps only the metadata
needed to reproduce token counts and verify bounty eligibility.

Output per record:
  - type, sessionId, uuid, requestId, timestamp, parentUuid
  - message.model, message.role, message.usage (for user/assistant)
  - pr-link records kept intact (PR numbers + repos are public on GitHub)
  - custom-title / ai-title / agent-name kept (not sensitive — they're derived titles)
  - project_dir derived from the source path (replaces cwd/gitBranch/slug)

Classification + redaction ranges from overrides.json still apply:
  - Session-level tag filter (`--only-tag work`)
  - Carve-out work ranges (personal sessions with some work content)
  - Redact ranges drop the record entirely from the metadata archive

Local `~/.claude/projects/` is never modified.

Usage:
  scan-tokens-metadata-archive.py <output-dir> [--only-tag TAG] [--window-start ISO] [--window-end ISO]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

OVERRIDES_FILE = Path.home() / ".claude" / "token-scan-overrides.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
STATUSLINE_CONF = Path.home() / ".claude" / "statusline.conf"


def load_classifier_env() -> None:
    if not STATUSLINE_CONF.exists():
        return
    try:
        with open(STATUSLINE_CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
                if not m:
                    continue
                k, v = m.group(1), m.group(2)
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                if k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


load_classifier_env()


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


def load_redact_map() -> dict[str, list[tuple[datetime, datetime]]]:
    overrides = load_overrides_raw()
    out: dict[str, list[tuple[datetime, datetime]]] = {}
    for sid, info in overrides.items():
        if not isinstance(info, dict):
            continue
        for r in info.get("ranges", []):
            if not isinstance(r, dict) or not r.get("redact"):
                continue
            frm = parse_iso(r.get("from"))
            to = parse_iso(r.get("to"))
            if frm and to:
                out.setdefault(sid, []).append((frm, to))
    return out


def explicit_tag(overrides: dict, sid: str) -> str | None:
    info = overrides.get(sid)
    if isinstance(info, dict):
        tag = info.get("tag")
        if tag in ("work", "personal"):
            return tag
    return None


def session_work_ranges(overrides: dict, sid: str) -> list[tuple[datetime, datetime]]:
    info = overrides.get(sid)
    if not isinstance(info, dict):
        return []
    out = []
    for r in info.get("ranges") or []:
        if isinstance(r, dict) and r.get("tag") == "work":
            frm = parse_iso(r.get("from"))
            to = parse_iso(r.get("to"))
            if frm and to:
                out.append((frm, to))
    return out


def classify_session(src: Path, overrides: dict) -> str:
    sid = src.stem
    tag = explicit_tag(overrides, sid)
    if tag:
        return tag
    try:
        import scan_tokens_core as core
        cfg = core.load_config()
        signals, _ = core.parse_jsonl_signals(src)
        for p in list(signals.cwds) + list(signals.paths):
            hit = core.classify_path(p, cfg)
            if hit:
                return hit
        return core.classify_by_content(signals, cfg)
    except Exception:
        return "unknown"


def classify_by_project_path(project_dir: str) -> str:
    """Fallback classifier for orphan subagents — classify by the project directory name alone."""
    try:
        import scan_tokens_core as core
        cfg = core.load_config()
        hit = core.classify_path(project_dir, cfg)
        return hit or "unknown"
    except Exception:
        return "unknown"


def in_windows(ts: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    for frm, to in windows:
        if frm <= ts <= to:
            return True
    return False


def extract_metadata(rec: dict) -> dict | None:
    """Return a metadata-only copy of the record, or None to drop entirely."""
    t = rec.get("type")
    # Universal metadata
    out = {"type": t}
    for k in ("sessionId", "uuid", "requestId", "timestamp", "parentUuid"):
        if k in rec and rec[k] is not None:
            out[k] = rec[k]

    # Per-type handling
    if t in ("user", "assistant"):
        msg = rec.get("message")
        if isinstance(msg, dict):
            meta_msg = {}
            for k in ("role", "model", "id", "stop_reason"):
                if k in msg and msg[k] is not None:
                    meta_msg[k] = msg[k]
            usage = msg.get("usage")
            if isinstance(usage, dict):
                # Keep only numeric usage fields
                meta_usage = {
                    k: v for k, v in usage.items()
                    if isinstance(v, (int, float))
                }
                if meta_usage:
                    meta_msg["usage"] = meta_usage
            # Content-block count (proves there was content, without the content)
            content = msg.get("content")
            if isinstance(content, list):
                block_types = [b.get("type") for b in content if isinstance(b, dict)]
                if block_types:
                    meta_msg["content_block_count"] = len(block_types)
                    meta_msg["content_block_types"] = list(sorted(set(block_types)))
            elif isinstance(content, str):
                meta_msg["content_length"] = len(content)
            if meta_msg:
                out["message"] = meta_msg

    elif t == "pr-link":
        # Public GitHub info — keep as-is
        for k in ("prNumber", "prRepository", "prUrl"):
            if k in rec:
                out[k] = rec[k]

    elif t in ("custom-title", "ai-title", "agent-name"):
        # Derived titles — not user-authored, but can still leak (e.g. "Didge NX dispute")
        # Scrub aggressively instead of keeping
        for k in ("customTitle", "aiTitle", "agentName"):
            if k in rec:
                out[k] = "[title-redacted]"

    elif t == "permission-mode":
        for k in ("permissionMode",):
            if k in rec:
                out[k] = rec[k]

    elif t == "worktree-state":
        # Could leak branch names like 'didge-nx-dispute' — drop the nested worktreeSession
        # dict entirely, just record that a worktree transition happened.
        out["has_worktree_transition"] = True

    elif t == "file-history-snapshot":
        # Snapshot contains file paths which can reveal branch/project info.
        # Pull just the timestamp.
        snap = rec.get("snapshot")
        if isinstance(snap, dict):
            if snap.get("timestamp"):
                out["snapshot_timestamp"] = snap["timestamp"]
            # Count tracked files without exposing paths
            files = snap.get("trackedFileBackups")
            if isinstance(files, dict):
                out["tracked_file_count"] = len(files)

    elif t == "attachment":
        # attachment field can contain pasted file content — drop the payload, keep the type
        att = rec.get("attachment")
        if isinstance(att, dict):
            out["attachment_type"] = att.get("type", "unknown")

    elif t == "progress":
        # progress records wrap nested tool output — drop data, keep tool ids
        for k in ("toolUseID", "parentToolUseID"):
            if k in rec:
                out[k] = rec[k]

    elif t == "queue-operation":
        # content field is the actual prompt text — drop it, keep operation
        for k in ("operation",):
            if k in rec:
                out[k] = rec[k]

    elif t == "system":
        # system records can contain hook output. Keep structural fields only.
        for k in ("subtype", "level", "hookCount", "preventedContinuation", "stopReason", "hasOutput", "toolUseID"):
            if k in rec and rec[k] is not None:
                out[k] = rec[k]

    elif t == "last-prompt":
        # lastPrompt contains user-typed text — drop entirely
        return None

    # All other types: keep only the universal metadata already in `out`
    return out


def extract_record_timestamp(rec: dict) -> datetime | None:
    ts_str = rec.get("timestamp")
    if ts_str is None and rec.get("type") == "file-history-snapshot":
        snap = rec.get("snapshot") or {}
        ts_str = snap.get("timestamp") if isinstance(snap, dict) else None
    return parse_iso(ts_str) if ts_str else None


def process_session(
    src: Path,
    dst: Path,
    redact_windows: list[tuple[datetime, datetime]],
    work_windows: list[tuple[datetime, datetime]] | None,
) -> tuple[int, int]:
    """Write metadata-only copy. Returns (records_written, records_dropped)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    dropped = 0
    with open(src) as rh, open(dst, "w") as wh:
        for line in rh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = extract_record_timestamp(rec)

            # Work-window filter: drop records outside work ranges (personal session carve-out)
            if work_windows is not None:
                if ts is None or not in_windows(ts, work_windows):
                    dropped += 1
                    continue

            # Redact-range filter: drop records entirely (metadata-only — no placeholder needed)
            if ts and in_windows(ts, redact_windows):
                dropped += 1
                continue

            meta = extract_metadata(rec)
            if meta is None:
                dropped += 1
                continue

            wh.write(json.dumps(meta, ensure_ascii=False) + "\n")
            written += 1
    return written, dropped


def session_meta(jsonl_path: Path) -> dict:
    meta = {"first_timestamp": None, "last_timestamp": None}
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
    except OSError:
        pass
    return meta


def session_tokens(
    jsonl_path: Path,
    redact_windows: list[tuple[datetime, datetime]] | None = None,
    work_windows: list[tuple[datetime, datetime]] | None = None,
) -> tuple[int, int, int]:
    """Dedupe by requestId, return (input, output, cache_read) totals.

    Only counts records that would be emitted to the archive:
    - Inside any work_window (if provided — carve-out mode)
    - Outside all redact_windows
    """
    seen: dict[str, tuple[int, int, int]] = {}
    try:
        with open(jsonl_path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = d.get("requestId")
                if not rid:
                    continue
                # Timestamp gating
                ts = extract_record_timestamp(d)
                if ts is None:
                    continue
                if work_windows is not None and not in_windows(ts, work_windows):
                    continue
                if redact_windows and in_windows(ts, redact_windows):
                    continue
                msg = d.get("message", {})
                usage = msg.get("usage", {}) if isinstance(msg, dict) else {}
                i_tok = usage.get("input_tokens", 0) or 0
                o_tok = usage.get("output_tokens", 0) or 0
                cr = usage.get("cache_read_input_tokens", 0) or 0
                prev = seen.get(rid, (0, 0, 0))
                if o_tok >= prev[1]:
                    seen[rid] = (i_tok, o_tok, cr)
    except OSError:
        return 0, 0, 0
    return (
        sum(i for i, _, _ in seen.values()),
        sum(o for _, o, _ in seen.values()),
        sum(c for _, _, c in seen.values()),
    )


def load_scan_tokens_cache() -> dict:
    """Load the scan-tokens cache if present — used as canonical source of per-request tokens."""
    try:
        with open(Path.home() / ".claude" / "token-scan-cache.json") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("output_dir", help="Target directory for metadata archive")
    ap.add_argument("--window-start", help="ISO timestamp")
    ap.add_argument("--window-end", help="ISO timestamp")
    ap.add_argument("--only-tag", choices=["work", "personal"])
    args = ap.parse_args()

    out_dir = Path(args.output_dir).expanduser().resolve()
    sessions_out = out_dir / "sessions"
    manifest_path = out_dir / "manifest.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    sessions_out.mkdir(parents=True, exist_ok=True)

    win_start = parse_iso(args.window_start)
    win_end = parse_iso(args.window_end)
    redact_map = load_redact_map()
    overrides_raw = load_overrides_raw()

    # Load scan-tokens cache — treated as the canonical source for token math.
    # Per-session manifest totals reconcile to scan-tokens' aggregate.
    try:
        import scan_tokens_core as core
        core_cfg = core.load_config()
    except Exception:
        core_cfg = None
    st_cache = load_scan_tokens_cache()
    st_files = st_cache.get("files", {}) if isinstance(st_cache, dict) else {}
    print(f"Loaded {sum(len(v) for v in redact_map.values())} redact ranges across {len(redact_map)} sessions")
    if args.only_tag:
        print(f"Filtering to tag={args.only_tag}")

    manifest = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "window_start": args.window_start,
        "window_end": args.window_end,
        "only_tag": args.only_tag,
        "redact_ranges_total": sum(len(v) for v in redact_map.values()),
        "format": "metadata-only",
        "format_notes": "All message content, tool inputs/outputs, attachments, and user-authored text have been stripped. Each record retains only token-usage metadata (requestId, timestamp, usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens) required to reproduce the bounty math.",
        "sessions": {},
    }

    total_files = 0
    total_written = 0
    total_dropped = 0
    skipped_window = 0
    skipped_tag = 0

    # Build parent-session map for subagents: sid → parent main session tag
    session_tag_cache: dict[str, str] = {}
    session_work_ranges_cache: dict[str, list[tuple[datetime, datetime]]] = {}

    # First pass: classify every main session
    main_sessions = []
    for src in PROJECTS_DIR.glob("*/[a-z0-9]*.jsonl"):
        if "subagents" in src.parts:
            continue
        main_sessions.append(src)
        sid = src.stem
        session_tag_cache[sid] = classify_session(src, overrides_raw) if args.only_tag else ""

    def process_one(src: Path, is_subagent: bool, parent_sid: str | None = None):
        nonlocal total_files, total_written, total_dropped, skipped_window, skipped_tag
        sid = src.stem
        meta = session_meta(src)
        first = parse_iso(meta["first_timestamp"])
        last = parse_iso(meta["last_timestamp"])
        if win_start and last and last < win_start:
            skipped_window += 1
            return
        if win_end and first and first > win_end:
            skipped_window += 1
            return

        # Determine tag (and carve-out windows) for this file.
        # Main sessions: their own classification.
        # Subagents: inherit parent session's classification + parent's work carve-out.
        # Orphan subagents (parent session file missing): fall back to path-based classification.
        lookup_sid = parent_sid or sid
        project_dir_name = ""
        if is_subagent:
            # Subagent path: <PROJECTS_DIR>/<project-slug>/<parent_sid>/subagents/<file>
            project_dir_name = src.parent.parent.parent.name
        else:
            project_dir_name = src.parent.name

        if args.only_tag:
            session_tag = session_tag_cache.get(lookup_sid)
            if session_tag is None:
                if is_subagent:
                    # Orphan subagent — classify by project path
                    session_tag = classify_by_project_path(project_dir_name)
                else:
                    session_tag = classify_session(src, overrides_raw)
                session_tag_cache[lookup_sid] = session_tag
        else:
            session_tag = None

        work_windows: list[tuple[datetime, datetime]] | None = None
        if args.only_tag == "work" and session_tag != "work":
            wws = session_work_ranges(overrides_raw, lookup_sid)
            if wws:
                work_windows = wws
            else:
                skipped_tag += 1
                return
        elif args.only_tag and session_tag != args.only_tag:
            skipped_tag += 1
            return

        redact_windows = redact_map.get(lookup_sid, [])

        # Date-based output layout: sessions/<YYYY-MM-DD>/<type>/<sid>.jsonl
        # Use the first-timestamp date; fall back to "unknown-date" if no timestamp.
        if first:
            date_str = first.strftime("%Y-%m-%d")
        else:
            date_str = "unknown-date"
        rec_type = "subagents" if is_subagent else "main"
        dst = sessions_out / date_str / rec_type / src.name
        rel_archive_path = f"{date_str}/{rec_type}/{src.name}"

        written, dropped = process_session(src, dst, redact_windows, work_windows)

        i_tok, o_tok, cr = session_tokens(src, redact_windows, work_windows)

        if is_subagent:
            manifest.setdefault("subagent_sessions", {})
            manifest["subagent_sessions"][sid] = {
                "path": rel_archive_path,
                "parent_sid": parent_sid,
                "parent_tag": session_tag,
                "original_project_dir": project_dir_name,
                "first_timestamp": meta["first_timestamp"],
                "last_timestamp": meta["last_timestamp"],
                "input_tokens": i_tok,
                "output_tokens": o_tok,
                "cache_read_tokens": cr,
                "records_written": written,
                "records_dropped": dropped,
            }
        else:
            manifest["sessions"][sid] = {
                "path": rel_archive_path,
                "original_project_dir": project_dir_name,
                "first_timestamp": meta["first_timestamp"],
                "last_timestamp": meta["last_timestamp"],
                "tag": session_tag or "",
                "input_tokens": i_tok,
                "output_tokens": o_tok,
                "cache_read_tokens": cr,
                "records_written": written,
                "records_dropped": dropped,
                "redact_ranges": len(redact_windows),
                "work_carve_out_windows": [
                    {"from": frm.isoformat(), "to": to.isoformat()}
                    for frm, to in (work_windows or [])
                ] if work_windows else None,
            }
        total_files += 1
        total_written += written
        total_dropped += dropped

    # Process main sessions
    for src in main_sessions:
        process_one(src, is_subagent=False)

    # Process subagent sessions. Find parent sid from path: .../projects/<proj>/<parent_sid>/subagents/<sid>.jsonl
    # The parent_sid is the second-to-last directory's name.
    for src in PROJECTS_DIR.glob("*/*/subagents/*.jsonl"):
        parent_sid = src.parent.parent.name
        process_one(src, is_subagent=True, parent_sid=parent_sid)

    # Reconcile per-session token totals with scan-tokens cache.
    # One-pass, O(n) in requests. Produces the exact same number as scan-tokens.
    if st_files and core_cfg is not None and args.only_tag == "work":
        manifest["reconciled_from_scan_tokens_cache"] = True

        # Single-pass: dedup by rid with _remember_max_out semantics, carry
        # (inp, out, cache_read, sid_for_range, ts_str). Skip non-work via
        # resolve_override. Apply redact ranges after dedup.
        challenge_start = getattr(core_cfg, "challenge_start", "")

        # seen[rid] = (inp, out, cr, sid_for_range, ts_str, work_type)
        seen: dict[str, tuple] = {}

        # Pre-build parent-acct lookup: sid → acct for main sessions
        main_acct: dict[str, str] = {}
        for filepath, entry in st_files.items():
            if isinstance(entry, dict) and "/subagents/" not in filepath:
                main_acct[Path(filepath).stem] = entry.get("acct", "unknown")

        for filepath, entry in st_files.items():
            if not isinstance(entry, dict):
                continue
            acct = entry.get("acct", "unknown")
            own_sid = Path(filepath).stem
            parent_sid = own_sid
            is_sa = "/subagents/" in filepath
            if is_sa:
                try:
                    parts = filepath.split("/")
                    parent_sid = parts[parts.index("subagents") - 1]
                    acct = main_acct.get(parent_sid, acct)
                except (ValueError, IndexError):
                    pass
            # Match scan-tokens exactly: resolve_override keys on the subagent's OWN sid,
            # not the parent. But the acct we pass is inherited from the parent (for subagents).
            resolve_sid = own_sid
            # For attribution of tokens to a session bucket in the manifest, we use parent_sid.
            attribution_sid = parent_sid
            for row in entry.get("requests", []):
                if not row or not row[0]:
                    continue
                rid = row[0]
                ts_str = row[1]
                inp = row[2] if len(row) > 2 else 0
                out = row[3] if len(row) > 3 else 0
                cr = row[4] if len(row) > 4 else 0
                if challenge_start and ts_str < challenge_start:
                    continue
                if win_end and parse_iso(ts_str) and parse_iso(ts_str) > win_end:
                    continue
                wt = core.resolve_override(resolve_sid, ts_str, acct, overrides_raw)
                prev = seen.get(rid)
                if prev is None or out > prev[1]:
                    seen[rid] = (inp, out, cr, attribution_sid, ts_str, wt)

        # Aggregate work-tagged entries. Redaction is content-level — it does not
        # affect token counts for bounty math. scan-tokens' canonical total includes
        # all work requests regardless of redact status.
        sid_tokens: dict[str, dict[str, int]] = {}
        total_i = total_o = 0
        total_cr = 0
        unique_requests = 0
        redacted_count = 0
        for rid, (inp, out, cr, sid, ts_str, wt) in seen.items():
            if wt != "work":
                continue
            ts = parse_iso(ts_str)
            rrs = redact_map.get(sid, [])
            is_redacted = bool(ts and any(frm <= ts <= to for frm, to in rrs))
            if is_redacted:
                redacted_count += 1
            acc = sid_tokens.setdefault(sid, {"input": 0, "output": 0, "cache_read": 0, "requests": 0, "redacted_requests": 0})
            acc["input"] += inp
            acc["output"] += out
            acc["cache_read"] += cr
            acc["requests"] += 1
            if is_redacted:
                acc["redacted_requests"] += 1
            total_i += inp
            total_o += out
            total_cr += cr
            unique_requests += 1

        # Patch manifest
        for sid, entry in manifest["sessions"].items():
            t = sid_tokens.get(sid, {"input": 0, "output": 0, "cache_read": 0, "requests": 0})
            entry["input_tokens"] = t["input"]
            entry["output_tokens"] = t["output"]
            entry["cache_read_tokens"] = t["cache_read"]
            entry["unique_requests_work"] = t["requests"]

        for sid, entry in manifest.get("subagent_sessions", {}).items():
            entry["input_tokens"] = 0
            entry["output_tokens"] = 0
            entry["cache_read_tokens"] = 0
            entry["unique_requests_work"] = 0
            entry["note"] = "tokens rolled into parent session per scan-tokens global dedup"

        # Orphan subagents (parent not in main manifest — usually because parent session's JSONL is gone)
        unattributed_sids = [sid for sid in sid_tokens if sid not in manifest["sessions"]]
        unattr_i = sum(sid_tokens[sid]["input"] for sid in unattributed_sids)
        unattr_o = sum(sid_tokens[sid]["output"] for sid in unattributed_sids)
        unattr_cr = sum(sid_tokens[sid]["cache_read"] for sid in unattributed_sids)
        unattr_reqs = sum(sid_tokens[sid]["requests"] for sid in unattributed_sids)

        if unattributed_sids:
            manifest["orphan_subagent_tokens"] = {
                "session_count": len(unattributed_sids),
                "note": "Tokens from subagents whose parent main-session JSONL is no longer on disk. Classified as work by parent session's cached tag. Counted here for full reconciliation with scan-tokens canonical total.",
                "input_tokens": unattr_i,
                "output_tokens": unattr_o,
                "cache_read_tokens": unattr_cr,
                "unique_requests": unattr_reqs,
                "sample_sids": unattributed_sids[:10],
            }

        manifest["archive_totals"] = {
            "input_tokens": total_i,
            "output_tokens": total_o,
            "io_tokens": total_i + total_o,
            "cache_read_tokens": total_cr,
            "unique_requests": unique_requests,
            "redacted_requests": redacted_count,
            "attributed_sessions": len(sid_tokens) - len(unattributed_sids),
            "orphan_subagent_sessions": len(unattributed_sids),
            "note": "Redaction is content-level only — redacted requests are counted in the totals (their token usage is unchanged by content redaction). Archive matches scan-tokens canonical total.",
        }

    tmp = str(manifest_path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    Path(tmp).rename(manifest_path)

    print(f"Processed {total_files} sessions")
    print(f"Skipped (outside window): {skipped_window}")
    print(f"Skipped (wrong tag): {skipped_tag}")
    print(f"Records written: {total_written:,}")
    print(f"Records dropped: {total_dropped:,}")
    print(f"Archive: {sessions_out}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
