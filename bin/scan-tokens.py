#!/usr/bin/env python3
from __future__ import annotations
"""Scan Claude Code session JSONLs and bucket tokens into two dimensions.

Walks ~/.claude/projects/, parses every session, deduplicates by requestId,
and writes:

  ~/.claude/token-scan-cache.json   — full per-file cache (several MB)
  ~/.claude/token-scan-summary.json — small aggregate read by the statusline

For streaming updates during active Claude sessions, use scan-tokens-daemon.py
(wired up by scan-tokens-watch.sh). This script remains the canonical source
of truth for periodic rebuilds (cron, launchd StartInterval, manual sanity
checks) and post-import consumers like derive-cap.py.

Output format is stable: both the daemon and this script emit the same
schemas for cache and summary JSON.

Env (see config/statusline.conf.example):
  CLAUDE_DIR, CHALLENGE_START, WORK_PATHS, PERSONAL_PATHS,
  WORK_KEYWORDS, PERSONAL_KEYWORDS, EMAIL_PAYER_MAP,
  BOUNTY_TARGET_TOKENS, BOUNTY_LOOKBACK_DAYS, BOUNTY_SESSION_GAP_MIN
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan_tokens_core as core  # noqa: E402


def _print_human_summary(cache_payload: dict, cfg: core.Config) -> None:
    files = cache_payload.get("files_scanned", 0)
    rescanned = cache_payload.get("files_rescanned", 0)
    duration = cache_payload.get("scan_duration_s", 0.0)
    print(f"Scanned {files} files ({rescanned} rescanned) in {duration:.1f}s")

    blocks = [("Global (all-time)", cache_payload["global"])]
    if "challenge" in cache_payload:
        blocks.append((f"Challenge (since {cfg.challenge_start})", cache_payload["challenge"]))

    for label, data in blocks:
        total = data["total_tokens"]
        if total == 0:
            continue
        print(f"\n=== {label} ===")
        print(f"Unique requests: {data['unique_requests']:,}")
        print(f"Work:     {data['work_tokens']/1e6:>8.2f}M  ({data['work_tokens']/total*100:.1f}%)")
        print(f"Personal: {data['personal_tokens']/1e6:>8.2f}M  ({data['personal_tokens']/total*100:.1f}%)")
        if data["unknown_tokens"] > 0:
            print(f"Unknown:  {data['unknown_tokens']/1e6:>8.2f}M  ({data['unknown_tokens']/total*100:.1f}%)")
        print(f"Total:    {total/1e6:>8.2f}M")
        bp = data.get("by_payer") or {}
        if any(v for v in bp.values()):
            print("Payer breakdown:")
            for payer, tok in sorted(bp.items(), key=lambda kv: -kv[1]):
                if tok == 0:
                    continue
                print(f"  {payer:<16} {tok/1e6:>8.2f}M  ({tok/total*100:.1f}%)")


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot token scan + summary.")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress human-readable output.")
    args = parser.parse_args()

    cfg = core.load_config()
    start = time.time()
    prev_cache = core.load_cache(cfg)
    new_files_raw, aggregates, rescanned = core.full_scan(cfg, prev_cache)
    codex_aggs = core.codex_full_scan(cfg)
    elapsed = time.time() - start

    cache_payload = core.build_cache_payload(
        cfg, new_files_raw, aggregates, rescanned, codex_aggs,
    )
    cache_payload["scan_duration_s"] = round(elapsed, 2)
    core.atomic_write_json(cfg.cache_file, cache_payload)

    summary = core.build_summary_payload(cfg, cache_payload, core.load_overrides(cfg))
    core.atomic_write_json(cfg.summary_file, summary)

    if not args.quiet:
        _print_human_summary(cache_payload, cfg)

    return 0


if __name__ == "__main__":
    sys.exit(main())
