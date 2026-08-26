#!/usr/bin/env python3
"""Long-running daemon that keeps token aggregates fresh from fswatch events.

This replaces the fork-per-event model of scan-tokens-watch.sh. The old
watcher ran Python from scratch on every JSONL change, re-walked the whole
tree, and rebuilt the full deduped aggregate — fast per-run, catastrophic
at 5Hz during streaming.

Architecture:

  fswatch ──NUL-delimited paths──▶ this daemon (stdin)

  boot:      full_scan() → build in-memory Aggregates + per-file cache
  on event:  read only the new bytes of each changed file, update Aggregates
             incrementally, mark summary dirty
  timer:     ≤1/s flush summary JSON; ≤1/30s flush cache JSON
  midnight:  rebuild `today` bucket once per local day
  SIGTERM:   flush and exit cleanly

The summary file (read by statusline.sh every render) is kept small and
written atomically. The big cache file (read by derive-cap.py, the exporter)
is written on a lower cadence because those consumers run periodically, not
per-event.
"""
from __future__ import annotations

import argparse
import os
import select
import signal
import sys
import time
from pathlib import Path
from typing import Optional

# Import from the sibling core module (bin/scan_tokens_core.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_tokens_core as core  # noqa: E402


# Minimum interval between disk writes. Tuned so the statusline always has
# fresh-enough data without hammering the SSD or triggering fsevents loops.
SUMMARY_FLUSH_INTERVAL_S = 1.0
CACHE_FLUSH_INTERVAL_S = 30.0
CODEX_SCAN_INTERVAL_S = 60.0

# When the main loop is idle, we still need to wake periodically to check
# midnight rollover and perform deferred flushes. A 1s select timeout keeps
# flush latency tight without busy-looping.
IDLE_TICK_S = 1.0


# ---------------------------------------------------------------------------
# Daemon state
# ---------------------------------------------------------------------------
class Daemon:
    """Holds all mutable state for the lifetime of the process."""

    def __init__(self, cfg: core.Config, verbose: bool = False):
        self.cfg = cfg
        self.verbose = verbose

        # Per-file state keyed by absolute path.
        self.files: dict[str, core.FileCacheEntry] = {}
        # Session-id → work-type classification (used for subagent inheritance).
        self.session_classifications: dict[str, str] = {}
        # In-memory aggregate state.
        self.aggregates = core.Aggregates()
        # Cached external config files; reloaded lazily.
        self.overrides: dict = {}
        self.payer_spans: dict = {}

        # Dirty flags + last-flush timestamps.
        self.summary_dirty = False
        self.cache_dirty = False
        self.last_summary_flush = 0.0
        self.last_cache_flush = 0.0
        self.last_codex_scan = 0.0
        self.last_midnight_check = 0.0

    # -----------------------------------------------------------------------
    # Boot
    # -----------------------------------------------------------------------
    def boot(self) -> None:
        """Do the initial full scan and write summary + cache."""
        self._sweep_stale_tmp_files()
        t0 = time.time()
        prev_cache = core.load_cache(self.cfg)
        self.overrides = core.load_overrides(self.cfg)
        self.payer_spans = core.load_session_payer_spans(self.cfg)

        new_files_raw, aggregates, rescanned = core.full_scan(self.cfg, prev_cache)
        self.files = {fp: core.FileCacheEntry.from_dict(d) for fp, d in new_files_raw.items()}
        self.aggregates = aggregates
        self.codex_aggregates = core.codex_full_scan(self.cfg)
        self.last_codex_scan = time.monotonic()

        # Prime session_classifications for subagent inheritance.
        for fp, entry in self.files.items():
            sid = core._sid_for(fp)
            self.session_classifications[sid] = entry.acct

        elapsed = time.time() - t0
        self._log(f"boot: {len(self.files)} files ({rescanned} rescanned) in {elapsed:.2f}s")

        self.summary_dirty = True
        self.cache_dirty = True
        self.flush(force=True, elapsed_s=elapsed)

    # -----------------------------------------------------------------------
    # Event handling
    # -----------------------------------------------------------------------
    def handle_event(self, path: str) -> None:
        """Process one JSONL path reported by fswatch."""
        if not path.endswith(".jsonl"):
            return
        # Skip files outside our watch root (defensive — fswatch should already filter).
        try:
            abspath = str(Path(path).resolve())
        except OSError:
            return
        if not abspath.startswith(str(self.cfg.projects_dir)):
            return

        try:
            stat = os.stat(abspath)
        except FileNotFoundError:
            # File deleted — drop from cache. Aggregates intentionally not
            # recomputed: requests already-seen persist. Full rebuild on next
            # restart corrects any drift.
            if abspath in self.files:
                del self.files[abspath]
                self.cache_dirty = True
                self._log(f"removed: {abspath}")
            return
        except OSError:
            return

        is_subagent = "subagent" in abspath
        if is_subagent:
            self._handle_subagent_event(abspath, stat)
        else:
            self._handle_main_event(abspath, stat)

    def _handle_main_event(self, abspath: str, stat: os.stat_result) -> None:
        mtime = int(stat.st_mtime)
        size = stat.st_size
        sid = core._sid_for(abspath)
        prev = self.files.get(abspath)

        start_offset = self._resolve_start_offset(prev, size)
        signals, new_offset = core.parse_jsonl_signals(abspath, start_offset)

        acct = self._resolve_main_acct(abspath, prev, signals, start_offset)
        self.session_classifications[sid] = acct

        if prev is None:
            prev_requests: list = []
        else:
            prev_requests = prev.requests if start_offset > 0 else []
        merged_requests = prev_requests + signals.requests

        self.files[abspath] = core.FileCacheEntry(
            mtime=mtime, acct=acct, size=size,
            offset=new_offset, requests=merged_requests,
        )

        if signals.requests:
            self.aggregates.ingest_with_resolver(
                signals.requests, sid, acct, self.overrides,
                self.payer_spans.get(sid, []), self.cfg.challenge_start,
            )
            self.summary_dirty = True
        self.cache_dirty = True
        if signals.requests:
            self._log(f"main {sid[:8]} +{len(signals.requests)} reqs")

    def _handle_subagent_event(self, abspath: str, stat: os.stat_result) -> None:
        mtime = int(stat.st_mtime)
        size = stat.st_size
        sid = core._sid_for(abspath)
        prev = self.files.get(abspath)

        start_offset = self._resolve_start_offset(prev, size)
        signals, new_offset = core.parse_jsonl_signals(abspath, start_offset)

        parent_sid = core._find_parent_sid(abspath, sid, self.session_classifications)
        if parent_sid:
            acct = self.session_classifications[parent_sid]
        else:
            acct = core.classify_path(abspath, self.cfg) or "unknown"
        if sid in self.overrides:
            tag, _ = core.parse_override(self.overrides[sid])
            if tag:
                acct = tag

        if prev is None:
            prev_requests: list = []
        else:
            prev_requests = prev.requests if start_offset > 0 else []
        merged_requests = prev_requests + signals.requests

        self.files[abspath] = core.FileCacheEntry(
            mtime=mtime, acct=acct, size=size,
            offset=new_offset, requests=merged_requests,
        )

        if signals.requests:
            lookup_sid = parent_sid or sid
            self.aggregates.ingest_with_resolver(
                signals.requests, sid, acct, self.overrides,
                self.payer_spans.get(lookup_sid, []), self.cfg.challenge_start,
            )
            self.summary_dirty = True
        self.cache_dirty = True

    def _resolve_start_offset(self, prev: Optional[core.FileCacheEntry], size: int) -> int:
        """Decide whether to resume at prev.offset or re-read from zero.

        File was truncated or rotated (size < prev.size) → re-read everything.
        """
        if prev is None:
            return 0
        if size < prev.size:
            return 0
        return prev.offset

    def _resolve_main_acct(self, abspath: str, prev: Optional[core.FileCacheEntry],
                           signals: core.SessionSignals, start_offset: int) -> str:
        """Pick work-type for a main-session file.

        Precedence: explicit override > path classifier > content classifier
        > previous classification > 'unknown'.
        """
        sid = core._sid_for(abspath)
        if sid in self.overrides:
            tag, _ = core.parse_override(self.overrides[sid])
            if tag:
                return tag

        acct = core.classify_path(abspath, self.cfg)
        if acct is not None:
            return acct

        # Only run the content classifier on a full-file parse; incremental
        # deltas don't carry enough context (they skip cwds/paths/user_text).
        if start_offset == 0:
            return core.classify_by_content(signals, self.cfg) or "unknown"

        return prev.acct if prev else "unknown"

    # -----------------------------------------------------------------------
    # Midnight rollover
    # -----------------------------------------------------------------------
    def maybe_roll_midnight(self) -> None:
        now = time.time()
        if now - self.last_midnight_check < 30:
            return
        self.last_midnight_check = now
        if not self.aggregates.roll_over_midnight_if_needed():
            return
        # Rebuild today's bucket from the in-memory cache. Cheap because we
        # already have all parsed requests — just re-ingest those with
        # ts >= new today_start.
        for fp, entry in self.files.items():
            sid = core._sid_for(fp)
            if "subagent" in fp:
                parent_sid = core._find_parent_sid(fp, sid, self.session_classifications)
                lookup_sid = parent_sid or sid
            else:
                lookup_sid = sid
            self.aggregates.ingest_with_resolver(
                entry.requests, sid, entry.acct, self.overrides,
                self.payer_spans.get(lookup_sid, []), self.cfg.challenge_start,
            )
        self.summary_dirty = True
        self._log("midnight: today bucket rebuilt")

    def maybe_scan_codex(self) -> None:
        now = time.monotonic()
        if now - self.last_codex_scan < CODEX_SCAN_INTERVAL_S:
            return
        previous = self.codex_aggregates
        self.codex_aggregates = core.codex_full_scan(self.cfg)
        self.last_codex_scan = now
        if self.codex_aggregates == previous:
            return
        self.summary_dirty = True
        self.cache_dirty = True

    # -----------------------------------------------------------------------
    # Periodic external-config reload
    # -----------------------------------------------------------------------
    def reload_external_config_if_stale(self) -> None:
        """Re-read overrides / session-accounts JSON if they changed on disk.

        These files can be edited by the user or written by other hooks while
        the daemon runs. Reload is cheap (small JSONs) and only triggers a
        full aggregate rebuild when content actually changes — detected via
        (mtime, size) pair to avoid false positives from atomic writes that
        preserve mtime.
        """
        # Periodic, not per-event. Skip unless at least SUMMARY_FLUSH_INTERVAL_S
        # has elapsed since last check (reuses last_midnight_check's cadence).
        new_overrides = core.load_overrides(self.cfg)
        new_spans = core.load_session_payer_spans(self.cfg)
        if new_overrides == self.overrides and new_spans == self.payer_spans:
            return
        self.overrides = new_overrides
        self.payer_spans = new_spans
        # Override change can reclassify historical requests — rebuild from
        # the in-memory request cache without re-parsing any JSONLs.
        self._rebuild_aggregates_from_cache()
        self.summary_dirty = True
        self._log("overrides/accounts changed; aggregates rebuilt")

    def _rebuild_aggregates_from_cache(self) -> None:
        self.aggregates = core.Aggregates()
        for fp, entry in self.files.items():
            sid = core._sid_for(fp)
            if "subagent" in fp:
                parent_sid = core._find_parent_sid(fp, sid, self.session_classifications)
                lookup_sid = parent_sid or sid
            else:
                lookup_sid = sid
            self.aggregates.ingest_with_resolver(
                entry.requests, sid, entry.acct, self.overrides,
                self.payer_spans.get(lookup_sid, []), self.cfg.challenge_start,
            )

    # -----------------------------------------------------------------------
    # Flush
    # -----------------------------------------------------------------------
    def flush(self, force: bool = False, elapsed_s: float = 0.0) -> None:
        now = time.time()
        cache_payload: Optional[dict] = None

        if self.summary_dirty and (force or now - self.last_summary_flush >= SUMMARY_FLUSH_INTERVAL_S):
            cache_payload = self._current_cache_payload(elapsed_s)
            summary = core.build_summary_payload(self.cfg, cache_payload, self.overrides)
            core.atomic_write_json(self.cfg.summary_file, summary)
            self.summary_dirty = False
            self.last_summary_flush = now

        if self.cache_dirty and (force or now - self.last_cache_flush >= CACHE_FLUSH_INTERVAL_S):
            if cache_payload is None:
                cache_payload = self._current_cache_payload(elapsed_s)
            core.atomic_write_json(self.cfg.cache_file, cache_payload)
            self.cache_dirty = False
            self.last_cache_flush = now

    def _current_cache_payload(self, elapsed_s: float) -> dict:
        new_files_raw = {fp: entry.to_dict() for fp, entry in self.files.items()}
        payload = core.build_cache_payload(
            self.cfg, new_files_raw, self.aggregates, files_rescanned=0,
            codex_aggregates=self.codex_aggregates,
        )
        if elapsed_s:
            payload["scan_duration_s"] = round(elapsed_s, 2)
        return payload

    # -----------------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[scan-tokens-daemon] {msg}", file=sys.stderr, flush=True)

    # -----------------------------------------------------------------------
    # Startup hygiene
    # -----------------------------------------------------------------------
    def _sweep_stale_tmp_files(self) -> None:
        """Remove any leftover *.{PID}.tmp files from a previous crash.

        atomic_write_json() writes to a PID-scoped tmp path and renames on
        top of the target. If the daemon is killed between create and
        rename, the .tmp file is orphaned. Sweeping on boot keeps the
        directory tidy and avoids confusion when the user looks at ~/.claude.

        Live tmp files written by a CURRENTLY-RUNNING process are skipped —
        we only want to delete orphans, not race a live writer.
        """
        import glob
        import re

        alive = _current_pids()
        pid_re = re.compile(r"\.(\d+)\.tmp$")
        for base in (self.cfg.summary_file, self.cfg.cache_file):
            pattern = f"{base}.*.tmp"
            for tmp in glob.glob(pattern):
                m = pid_re.search(tmp)
                owner = int(m.group(1)) if m else None
                if owner and owner in alive and owner != os.getpid():
                    continue
                try:
                    os.unlink(tmp)
                    self._log(f"swept stale tmp: {os.path.basename(tmp)}")
                except FileNotFoundError:
                    pass
                except OSError as e:
                    self._log(f"sweep failed: {tmp} ({e})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _current_pids() -> set[int]:
    """Best-effort: return a set-like that answers "is this PID alive"."""

    class _LivePIDs(set):
        def __contains__(self, pid: object) -> bool:
            if not isinstance(pid, int) or pid <= 0:
                return False
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False
            except OSError:
                return False

    return _LivePIDs()


_EOF_SENTINEL = object()


def _read_nul_paths_nonblocking(fd: int, buffer: bytearray):
    """Drain available bytes from fd, split on NUL, return complete paths.

    Returns a list of paths. Returns _EOF_SENTINEL (a singleton, not a list)
    if the pipe reached EOF — callers should shut down cleanly.
    """
    try:
        chunk = os.read(fd, 65536)
    except BlockingIOError:
        return []
    if chunk == b"":
        return _EOF_SENTINEL
    buffer.extend(chunk)
    parts = buffer.split(b"\0")
    # Last part may be incomplete — put it back in the buffer.
    buffer[:] = parts[-1]
    return [p.decode("utf-8", errors="replace") for p in parts[:-1] if p]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log events to stderr.")
    parser.add_argument("--once", action="store_true",
                        help="Do a single full scan and exit (no daemon loop).")
    args = parser.parse_args()

    cfg = core.load_config()
    daemon = Daemon(cfg, verbose=args.verbose)
    daemon.boot()

    if args.once:
        return 0

    # Install signal handlers for clean shutdown.
    shutdown = {"requested": False}

    def _shutdown(signum, _frame):
        shutdown["requested"] = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGHUP, _shutdown)

    # Switch stdin to non-blocking so select controls the cadence.
    os.set_blocking(sys.stdin.fileno(), False)
    buf = bytearray()

    while not shutdown["requested"]:
        try:
            rlist, _, _ = select.select([sys.stdin], [], [], IDLE_TICK_S)
        except InterruptedError:
            continue

        if rlist:
            paths = _read_nul_paths_nonblocking(sys.stdin.fileno(), buf)
            if paths is _EOF_SENTINEL:
                daemon._log("stdin EOF — fswatch exited; shutting down")
                break
            for path in paths:
                daemon.handle_event(path)

        daemon.maybe_roll_midnight()
        daemon.maybe_scan_codex()
        daemon.reload_external_config_if_stale()
        daemon.flush()

    daemon.flush(force=True)
    daemon._log("shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
