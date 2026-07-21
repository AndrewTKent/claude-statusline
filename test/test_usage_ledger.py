"""Tests for usage-ledger.py — the durable per-day token ledger.

Builds a synthetic CLAUDE_DIR in a tempdir so the real ~/.claude is never
touched. The load-bearing invariant: rows survive deletion of the transcripts
they came from, and a rescan can never shrink history.

Run: python3 -m pytest test/test_usage_ledger.py -v
Or:  python3 test/test_usage_ledger.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_BIN = REPO_ROOT / "bin" / "usage-ledger.py"

TS = "2026-06-01T17:00:00.000Z"  # 10:00 PDT -> local day 2026-06-01 in US zones
MODEL = "claude-opus-4-7"


def assistant_row(mid: str, output: int, *, input_t: int = 10, cw: int = 100,
                  cw_1h: int = 40, cr: int = 1000, model: str = MODEL) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": TS,
        "message": {
            "id": mid,
            "model": model,
            "usage": {
                "input_tokens": input_t,
                "output_tokens": output,
                "cache_creation_input_tokens": cw,
                "cache_read_input_tokens": cr,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cw - cw_1h,
                    "ephemeral_1h_input_tokens": cw_1h,
                },
            },
        },
    })


class UsageLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude_dir = Path(self.tmp.name)
        self.project = self.claude_dir / "projects" / "-tmp-proj"
        (self.project / "session-a" / "subagents").mkdir(parents=True)
        self.ledger_path = self.claude_dir / "usage-ledger.json"

        # Main session: one message streamed as 3 rows (same id, growing
        # output — only the final snapshot should count) plus one more message.
        main = self.project / "session-a.jsonl"
        main.write_text("\n".join([
            assistant_row("msg_1", 100),
            assistant_row("msg_1", 250),
            assistant_row("msg_1", 400),
            assistant_row("msg_2", 50),
        ]) + "\n")

        # Subagent transcript: one message.
        sub = self.project / "session-a" / "subagents" / "agent-deadbeef.jsonl"
        sub.write_text(assistant_row("msg_3", 70) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_ledger(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(LEDGER_BIN), "--claude-dir", str(self.claude_dir),
             "--ledger", str(self.ledger_path), *extra],
            capture_output=True, text=True, env={**os.environ}, check=False,
        )

    def read_rows(self) -> dict:
        return json.loads(self.ledger_path.read_text())["days"]

    def day_key(self) -> str:
        days = self.read_rows()
        self.assertEqual(len(days), 1)
        return next(iter(days))

    def test_dedupes_streamed_rows_and_includes_subagents(self):
        result = self.run_ledger("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.read_rows()[self.day_key()][MODEL]
        # msg_1 counted once at its max snapshot (400), not 100+250+400.
        self.assertEqual(row["output"], 400 + 50 + 70)
        self.assertEqual(row["messages"], 3)
        self.assertEqual(row["input"], 30)
        self.assertEqual(row["cache_write"], 300)
        self.assertEqual(row["cache_write_1h"], 120)
        self.assertEqual(row["cache_read"], 3000)

    def test_rerun_is_idempotent(self):
        self.run_ledger("--force")
        first = self.read_rows()
        self.run_ledger("--force")
        self.assertEqual(self.read_rows(), first)

    def test_rows_survive_transcript_deletion(self):
        self.run_ledger("--force")
        before = self.read_rows()
        (self.project / "session-a.jsonl").unlink()
        (self.project / "session-a" / "subagents" / "agent-deadbeef.jsonl").unlink()
        result = self.run_ledger("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_rows(), before)

    def test_smaller_rescan_never_shrinks_a_row(self):
        self.run_ledger("--force")
        before = self.read_rows()
        # Replace the main session with a fragment (as if rows aged out).
        (self.project / "session-a.jsonl").write_text(assistant_row("msg_2", 50) + "\n")
        self.run_ledger("--force")
        self.assertEqual(self.read_rows(), before)

    def test_throttle_skips_when_fresh(self):
        self.run_ledger("--force")
        updated = json.loads(self.ledger_path.read_text())["updated_at"]
        result = self.run_ledger()  # no --force
        self.assertEqual(result.returncode, 0)
        self.assertIn("skipping", result.stdout)
        self.assertEqual(json.loads(self.ledger_path.read_text())["updated_at"], updated)


if __name__ == "__main__":
    unittest.main()
