# Token Scanning Architecture

## Three entry points, one engine

```
bin/
├── scan_tokens_core.py          module: parsing, classification, aggregation
├── scan-tokens.py               CLI:    one-shot full scan (cron / manual)
├── scan-tokens-daemon.py        CLI:    long-running, fed by fswatch events
└── scan-tokens-watch.sh         shell:  pipes fswatch → daemon.py
```

`scan_tokens_core.py` owns all domain logic. The two CLIs (`scan-tokens.py`
and `scan-tokens-daemon.py`) are both thin wrappers over the same core. They
produce byte-identical `summary.json` for the same input — this is a locked
invariant (see `test/test_scan_tokens.py::test_oneshot_and_daemon_once_produce_identical_summaries`).

## Why a daemon

The previous watcher re-exec'd Python on every JSONL change:

```
old: fswatch → bash → exec python → os.walk whole tree → rebuild aggregates → exit
```

Under active streaming (~5Hz per session), this pegged a core and filled
swap. The daemon flips it:

```
new: fswatch ──NUL-delim paths──► daemon
        (once)  boot: full_scan + in-memory Aggregates + per-file cache
        (event) read only appended bytes, incrementally update Aggregates
        (≤1/s)  atomic_write summary.json (small, hot)
        (≤1/30s) atomic_write cache.json (large, consumed by export/derive-cap)
```

Key mechanics:

- **Byte-offset incremental parsing.** Per file we track `(mtime, size, offset)`.
  On event, seek to last offset and parse only new lines. Partial trailing
  lines rewind the offset so the next event re-reads them.
- **Debounced writes.** Summary: ≥1s between writes. Cache: ≥30s. The cache
  is much bigger and only read periodically, so we amortize disk cost.
- **Atomic writes.** Every write is temp-file + `os.replace`. Readers never
  observe a half-written file. Orphaned `.tmp` files (from a crash mid-write)
  are swept on the next boot.
- **Midnight rollover.** `Aggregates` tracks `today_start`. A periodic check
  detects the local-day boundary and rebuilds the `today` bucket from the
  in-memory request cache (no reparse).
- **External config reload.** `token-scan-overrides.json` and
  `session-accounts.json` are re-read periodically. If they change, the
  aggregate is rebuilt from cached requests (again, no reparse).

## File layouts

### Cache: `~/.claude/token-scan-cache.json`

```json
{
  "timestamp": 1714000000,
  "scan_duration_s": 1.2,
  "challenge_start": "2026-03-23T00:00:00Z",
  "global":    { "work_tokens": ..., "personal_tokens": ..., "by_payer": {...}, ... },
  "today":     { ... },
  "challenge": { ... },
  "files": {
    "/Users/.../session.jsonl": {
      "mtime": 1714000000,
      "size":  123456,
      "offset": 123456,
      "acct": "work",
      "requests": [[rid, ts, inp, out, cache_read, cache_create], ...]
    }
  }
}
```

New fields vs. pre-daemon cache: `size`, `offset`. Old caches (without these
fields) trigger a full reparse once, then self-heal.

### Summary: `~/.claude/token-scan-summary.json`

```json
{
  "timestamp": 1714000000,
  "scan_duration_s": 1.2,
  "global":    {...},
  "today":     {...},
  "challenge": {...},
  "bounty":    { "target": 40000000, "current_work_tokens": ..., "eta_hours": ..., ... },
  "redactions": { "sessions": 68, "ranges": 96 }
}
```

Read by `statusline.sh` every render. Schema stable; adding fields is safe,
removing or renaming requires a bump.

## Classification precedence

Every request gets two labels:

1. **work-type** — `work | personal | unknown`. Derived (precedence, first match wins):
   1. Manual override entry for the session (optionally with a time range).
   2. Path classifier: cwd/tool-path substring matches against `WORK_PATHS` /
      `PERSONAL_PATHS` from `statusline.conf`.
   3. Content classifier: keyword hits in user prompts (`WORK_KEYWORDS` /
      `PERSONAL_KEYWORDS`), weighted 3× path hits.
   4. Parent-session inheritance (subagent files only).
   5. `unknown`.
2. **payer** — any label in `EMAIL_PAYER_MAP`, else `unknown`. Derived from
   `session-accounts.json` by matching the request timestamp against the
   session's span ranges. Payer is orthogonal to work-type.

## Failure modes

| Scenario                  | Behavior                                                   |
|---------------------------|------------------------------------------------------------|
| Partial trailing line     | Rewinds offset; retries on next event                     |
| File truncated / rotated  | `size < prev.size` → reparse from zero                    |
| Daemon crash mid-write    | Next boot sweeps stale `.tmp` files                       |
| Daemon crash / OOM        | launchd restarts after 10s (ThrottleInterval)             |
| fswatch stops             | stdin EOF → daemon exits → launchd respawns watcher       |
| Clock change / DST        | Midnight check reads local-time boundary each tick        |
| Stale cache schema        | Missing offset field → reparse once, cache heals          |
| File deleted              | Cache entry dropped; in-memory aggregates retain requests |

## Installation

```bash
macos/launchd/install-daemon.sh            # install / reload
macos/launchd/install-daemon.sh --remove   # unload and delete plist
```

## Testing

```bash
python3 test/test_scan_tokens.py           # unit + e2e
```

The e2e test shells out to both CLIs against a synthetic tempdir and asserts
identical output.
