# Architecture

## Two layers

```
                            ┌──────────────────────┐
                            │   statusline.sh      │
                            └─────────┬────────────┘
                                      │ reads
              ┌───────────────────────┴────────────────────────┐
              ▼                                                ▼
      HISTORICAL ATTRIBUTION                         LIVE STATE
      (engine + cache + summary)                     (probe on render)
                                                     bin/live-state.py
      bin/scan_tokens_core.py
      bin/scan-tokens*.py / .sh                      reads:
      bin/account-usage-summary.py                     ~/.claude/account-resets.json
      bin/derive-cap.py                                ~/.codex/state_5.sqlite
                                                       $AGENT_SESSIONS_PATH
      writes:
        ~/.claude/token-scan-summary.json            outputs (modes):
        ~/.claude/token-scan-cache.json                kv | --render | --json
        ~/.claude/usage-ledger.json (durable)

      knows: per-request work/personal split          knows: this-instant 5h%,
             across both backends, payer attribution,        codex rate_limit %,
             today/lifetime/challenge windows,               remote agent activity
             codex rate_limit per session

      doesn't know: remote agents (laptop only)      doesn't know: historical
                                                                    attribution
```

The two layers answer adjacent questions:

- **Historical** — "of all the tokens I've spent this week, how many were work tokens paid by my work plan?" Slow, accurate, attributes per-request, covers both Claude and Codex.
- **Live** — "right now, how close am I to the 5h cap, and which remote agents are running?" Fast, no attribution, snapshot only.

The engine's `summary.json` includes a `codex` block with the latest `rate_limit` payload Codex emits per turn — the *authoritative* 5h / 7d utilization that `codex exec` mode silently strips. `live-state.py` prefers that signal and falls back to a raw codex sqlite token sum when no engine summary exists. Planned: pull the remote host's sessions to the laptop so its work is also visible to scan-tokens.

## Token scan engine — three entry points, one core

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

### Durable ledger: `~/.claude/usage-ledger.json`

Written by `bin/usage-ledger.py`. The cache above follows the live transcript
set — a session file deleted by Claude Code's retention drops out of it. The
ledger is the layer that doesn't: per-(local day, model) token sums, deduped
by message id, merged monotonically (a row is only added or raised, never
removed), so usage history survives transcript cleanup and cache rebuilds.
Self-throttled to one scan per `USAGE_LEDGER_INTERVAL_H` hours (default 6),
so it can chain after `scan-tokens.py` in the 60s launchd poll for free.

```json
{
  "version": 1,
  "updated_at": "2026-06-09T20:00:00+00:00",
  "days": {
    "2026-06-08": {
      "claude-opus-4-7": {"input": ..., "output": ..., "cache_write": ...,
                           "cache_write_1h": ..., "cache_read": ..., "messages": ...}
    }
  }
}
```

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
macos/launchd/install-accounts-poll.sh     # account-board poller (30m); --remove to drop
```

## Testing

```bash
python3 test/test_scan_tokens.py           # unit + e2e
```

The e2e test shells out to both CLIs against a synthetic tempdir and asserts
identical output.
