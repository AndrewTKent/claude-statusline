"""End-to-end tests for scan-tokens core, one-shot, and daemon.

The tests build a synthetic CLAUDE_DIR in a tempdir so they never touch the
real ~/.claude. Key invariant: the one-shot scan and the daemon must produce
byte-for-byte identical summary.json payloads (minus transient timestamps)
for the same input.

Run: python3 -m pytest test/test_scan_tokens.py -v
Or:  python3 test/test_scan_tokens.py   # falls back to unittest main.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
sys.path.insert(0, str(BIN))
import scan_tokens_core as core  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
def make_assistant_line(rid: str, ts: str, inp: int, out: int,
                        cache_read: int = 0, cache_create: int = 0) -> str:
    entry = {
        "type": "assistant",
        "requestId": rid,
        "timestamp": ts,
        "message": {
            "id": rid,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            },
            "content": [],
        },
    }
    return json.dumps(entry)


def make_user_line(text: str, cwd: str | None = None) -> str:
    entry = {
        "type": "user",
        "message": {"content": text},
    }
    if cwd:
        entry["cwd"] = cwd
    return json.dumps(entry)


def write_session(projects_dir: Path, project_slug: str, sid: str,
                  lines: list[str]) -> Path:
    session_dir = projects_dir / project_slug
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{sid}.jsonl"
    with open(path, "w") as f:
        for line in lines:
            f.write(line + "\n")
    return path


def append_to_session(path: Path, lines: list[str]) -> None:
    with open(path, "a") as f:
        for line in lines:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------
class ScanTokensTestCase(unittest.TestCase):
    """Shared tempdir + environment setup."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.claude_dir = Path(self.tmp.name) / ".claude"
        self.projects_dir = self.claude_dir / "projects"
        self.projects_dir.mkdir(parents=True)
        self.codex_dir = Path(self.tmp.name) / ".codex"
        self.codex_sessions_dir = self.codex_dir / "sessions"

        # Isolated env so core.load_config sees only what we supply.
        self._orig_env = os.environ.copy()
        for k in list(os.environ):
            if k.startswith(("CLAUDE_", "CODEX_", "CHALLENGE_", "WORK_", "PERSONAL_",
                             "EMAIL_PAYER_MAP", "BOUNTY_")):
                os.environ.pop(k, None)
        os.environ["CLAUDE_DIR"] = str(self.claude_dir)
        os.environ["CODEX_DIR"] = str(self.codex_dir)
        os.environ["WORK_PATHS"] = "work-project,acme"
        os.environ["PERSONAL_PATHS"] = "personal-project"
        os.environ["WORK_KEYWORDS"] = "deploy prod,production"
        os.environ["PERSONAL_KEYWORDS"] = "birthday"
        os.environ["EMAIL_PAYER_MAP"] = "work:me@acme.com personal:me@gmail.com"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)
        self.tmp.cleanup()

    # -- Convenience -------------------------------------------------------
    def cfg(self) -> core.Config:
        return core.load_config()

    def run_oneshot(self) -> dict:
        result = subprocess.run(
            [sys.executable, str(BIN / "scan-tokens.py"), "--quiet"],
            env=os.environ.copy(),
            check=True,
            capture_output=True,
            text=True,
        )
        return self._load_summary()

    def run_daemon_once(self) -> dict:
        result = subprocess.run(
            [sys.executable, str(BIN / "scan-tokens-daemon.py"), "--once"],
            env=os.environ.copy(),
            check=True,
            capture_output=True,
            text=True,
        )
        return self._load_summary()

    def _load_summary(self) -> dict:
        with open(self.cfg().summary_file) as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# Core parsing tests
# ---------------------------------------------------------------------------
class TestCoreParsing(ScanTokensTestCase):

    def test_parse_jsonl_signals_collects_requests(self):
        path = write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000001", [
            make_user_line("Hello", cwd="/Users/me/work-project"),
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
            make_assistant_line("r2", "2026-04-23T10:01:00Z", 200, 75),
        ])
        signals, offset = core.parse_jsonl_signals(path, 0)
        self.assertEqual(len(signals.requests), 2)
        self.assertEqual(signals.requests[0][0], "r1")
        self.assertEqual(signals.requests[0][2], 100)
        self.assertIn("/Users/me/work-project", signals.cwds)
        self.assertEqual(offset, os.path.getsize(path))

    def test_parse_jsonl_signals_incremental_resume(self):
        path = write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000002", [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
        ])
        signals1, offset1 = core.parse_jsonl_signals(path, 0)
        self.assertEqual(len(signals1.requests), 1)

        append_to_session(path, [
            make_assistant_line("r2", "2026-04-23T10:01:00Z", 200, 75),
        ])
        signals2, offset2 = core.parse_jsonl_signals(path, offset1)
        self.assertEqual(len(signals2.requests), 1)
        self.assertEqual(signals2.requests[0][0], "r2")
        self.assertEqual(offset2, os.path.getsize(path))

    def test_parse_jsonl_handles_partial_trailing_line(self):
        path = write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000003", [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
        ])
        # Append a partial (unterminated) line — simulates mid-write.
        with open(path, "a") as f:
            f.write('{"type":"assistant","requestId":"r2"')
        size_before = os.path.getsize(path)
        signals, offset = core.parse_jsonl_signals(path, 0)
        self.assertEqual(len(signals.requests), 1)
        self.assertLess(offset, size_before)  # rewound to start of partial line

        # Now complete the line and resume.
        with open(path, "a") as f:
            f.write(',"timestamp":"2026-04-23T10:01:00Z","message":{"id":"r2","usage":{"input_tokens":200,"output_tokens":75},"content":[]}}\n')
        signals2, offset2 = core.parse_jsonl_signals(path, offset)
        self.assertEqual(len(signals2.requests), 1)
        self.assertEqual(signals2.requests[0][0], "r2")

    def test_classify_path_prefers_personal_then_work(self):
        cfg = self.cfg()
        self.assertEqual(core.classify_path("/Users/me/work-project/a", cfg), "work")
        self.assertEqual(core.classify_path("/Users/me/personal-project/a", cfg), "personal")
        self.assertIsNone(core.classify_path("/tmp/unknown", cfg))

    def test_classify_by_content_keywords(self):
        cfg = self.cfg()
        signals = core.SessionSignals(user_text="we need to deploy prod now")
        self.assertEqual(core.classify_by_content(signals, cfg), "work")
        signals = core.SessionSignals(user_text="happy birthday dinner plans")
        self.assertEqual(core.classify_by_content(signals, cfg), "personal")

    def test_resolve_override_ranges(self):
        overrides = {
            "s1": {
                "tag": "work",
                "ranges": [
                    {"from": "2026-04-23T10:00:00Z", "to": "2026-04-23T11:00:00Z",
                     "tag": "personal"},
                ],
            }
        }
        # Inside range → personal.
        self.assertEqual(
            core.resolve_override("s1", "2026-04-23T10:30:00Z", "unknown", overrides),
            "personal",
        )
        # Outside range → session-level tag wins.
        self.assertEqual(
            core.resolve_override("s1", "2026-04-23T12:00:00Z", "unknown", overrides),
            "work",
        )


# ---------------------------------------------------------------------------
# Full-tree scan tests
# ---------------------------------------------------------------------------
class TestFullScan(ScanTokensTestCase):

    def test_basic_aggregation(self):
        write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000010", [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
            make_assistant_line("r2", "2026-04-23T10:01:00Z", 200, 75),
        ])
        summary = self.run_oneshot()
        self.assertEqual(summary["global"]["total_tokens"], 100 + 50 + 200 + 75)
        # acme path → work
        self.assertEqual(summary["global"]["work_tokens"], 100 + 50 + 200 + 75)

    def test_dedup_by_request_id(self):
        # Same rid written twice (e.g., main + subagent referencing same stream)
        # with different output_tokens → we keep the max-out version.
        write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000020", [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 80),
        ])
        summary = self.run_oneshot()
        self.assertEqual(summary["global"]["total_tokens"], 100 + 80)

    def test_oneshot_and_daemon_once_produce_identical_summaries(self):
        write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000030", [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
            make_assistant_line("r2", "2026-04-23T10:01:00Z", 200, 75),
        ])
        write_session(self.projects_dir, "personal-project", "00000000-0000-0000-0000-000000000031", [
            make_assistant_line("r3", "2026-04-23T10:02:00Z", 300, 100),
        ])
        oneshot = self.run_oneshot()

        # Clear cache so daemon boots fresh — otherwise it'd reuse cache
        # written by the one-shot run.
        self.cfg().cache_file.unlink()
        self.cfg().summary_file.unlink()

        daemon = self.run_daemon_once()

        # Strip volatile fields before compare.
        for s in (oneshot, daemon):
            s.pop("timestamp", None)
            s.pop("scan_duration_s", None)

        self.assertEqual(oneshot, daemon,
                         "oneshot and daemon must emit identical summary payloads")

    def test_subagent_inherits_parent_classification(self):
        parent_sid = "00000000-0000-0000-0000-000000000040"
        write_session(self.projects_dir, "acme", parent_sid, [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
        ])
        # Subagent file lives under a subagent/ directory and embeds parent_sid
        # in its filename.
        subagent_dir = self.projects_dir / "acme" / "subagent"
        subagent_dir.mkdir()
        sub_path = subagent_dir / f"{parent_sid}-child-aaaaaaaaaaaa.jsonl"
        with open(sub_path, "w") as f:
            f.write(make_assistant_line("r_sub", "2026-04-23T10:00:30Z", 10, 5) + "\n")

        summary = self.run_oneshot()
        # Sub request inherits work-type work → rolls into work_tokens.
        self.assertEqual(summary["global"]["work_tokens"], 100 + 50 + 10 + 5)


# ---------------------------------------------------------------------------
# Incremental / cache-hit tests
# ---------------------------------------------------------------------------
class TestIncrementalScan(ScanTokensTestCase):

    def test_cache_hit_skips_rescan(self):
        path = write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000050", [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
        ])
        self.run_oneshot()

        # Second invocation with no changes — files_rescanned should be 0.
        with open(self.cfg().cache_file) as f:
            cache1 = json.load(f)
        self.run_oneshot()
        with open(self.cfg().cache_file) as f:
            cache2 = json.load(f)
        self.assertEqual(cache2["files_rescanned"], 0)

    def test_append_triggers_rescan_and_updates_totals(self):
        path = write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000060", [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
        ])
        s1 = self.run_oneshot()
        # Bump mtime explicitly (stat precision on macOS is 1s; sleep to cross a boundary).
        time.sleep(1.1)
        append_to_session(path, [
            make_assistant_line("r2", "2026-04-23T10:01:00Z", 200, 75),
        ])
        s2 = self.run_oneshot()
        self.assertEqual(s1["global"]["total_tokens"], 150)
        self.assertEqual(s2["global"]["total_tokens"], 150 + 275)


# ---------------------------------------------------------------------------
# Summary payload contract tests
# ---------------------------------------------------------------------------
class TestSummaryContract(ScanTokensTestCase):
    """Lock in the public schema consumed by statusline.sh."""

    def test_summary_has_expected_top_level_keys(self):
        write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000070", [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
        ])
        summary = self.run_oneshot()
        for key in ("timestamp", "global", "today", "redactions"):
            self.assertIn(key, summary)
        for key in ("work_tokens", "personal_tokens", "unknown_tokens",
                    "total_tokens", "unique_requests", "by_payer"):
            self.assertIn(key, summary["global"])

    def test_challenge_block_present_when_challenge_start_set(self):
        os.environ["CHALLENGE_START"] = "2026-04-01T00:00:00Z"
        write_session(self.projects_dir, "acme", "00000000-0000-0000-0000-000000000080", [
            make_assistant_line("r1", "2026-04-23T10:00:00Z", 100, 50),
        ])
        summary = self.run_oneshot()
        self.assertIn("challenge", summary)
        self.assertEqual(summary["challenge"]["total_tokens"], 150)


# ---------------------------------------------------------------------------
# Codex parsing tests
# ---------------------------------------------------------------------------
def write_codex_rollout(sessions_dir: Path, sid: str, cwd: str,
                         turns: list[tuple[str, int]],
                         rate_limits: dict | None = None) -> Path:
    """Build a minimal codex rollout JSONL with session_meta + token_count events."""
    day_dir = sessions_dir / "2026" / "04" / "29"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{sid}.jsonl"
    lines: list[str] = []
    lines.append(json.dumps({
        "timestamp": "2026-04-29T00:00:00.000Z",
        "type": "session_meta",
        "payload": {"id": sid, "cwd": cwd, "model_provider": "openai"},
    }))
    for ts, tokens in turns:
        payload = {
            "type": "token_count",
            "info": {
                "last_token_usage": {"total_tokens": tokens},
                "total_token_usage": {"total_tokens": tokens},
            },
        }
        if rate_limits is not None:
            payload["rate_limits"] = rate_limits
        lines.append(json.dumps({
            "timestamp": ts, "type": "event_msg", "payload": payload,
        }))
    path.write_text("\n".join(lines) + "\n")
    return path


class TestCodexScan(ScanTokensTestCase):

    def test_no_codex_dir_yields_empty_aggregates(self):
        # Codex dir doesn't exist — engine still works, just emits no codex block.
        summary = self.run_oneshot()
        self.assertNotIn("codex", summary)

    def test_codex_session_classifies_by_cwd(self):
        # work cwd matches WORK_PATHS=acme → tokens land in "work" bucket
        write_codex_rollout(
            self.codex_sessions_dir, "sess-work-1",
            cwd="/Users/me/acme/repo",
            turns=[("2026-04-29T01:00:00Z", 1500), ("2026-04-29T01:05:00Z", 500)],
        )
        # personal cwd matches PERSONAL_PATHS → "personal" bucket
        write_codex_rollout(
            self.codex_sessions_dir, "sess-personal-1",
            cwd="/Users/me/personal-project/x",
            turns=[("2026-04-29T01:10:00Z", 800)],
        )
        # unmatched cwd → "unknown"
        write_codex_rollout(
            self.codex_sessions_dir, "sess-unknown-1",
            cwd="/Users/me/random",
            turns=[("2026-04-29T01:20:00Z", 200)],
        )
        summary = self.run_oneshot()
        self.assertIn("codex", summary)
        cx = summary["codex"]
        self.assertEqual(cx["session_count"], 3)
        self.assertEqual(cx["global"]["work_tokens"], 2000)
        self.assertEqual(cx["global"]["personal_tokens"], 800)
        self.assertEqual(cx["global"]["unknown_tokens"], 200)
        self.assertEqual(cx["global"]["total_tokens"], 3000)

    def test_codex_rate_limit_picks_most_recent(self):
        write_codex_rollout(
            self.codex_sessions_dir, "sess-old",
            cwd="/Users/me/acme/repo",
            turns=[("2026-04-29T01:00:00Z", 100)],
            rate_limits={
                "plan_type": "pro",
                "primary": {"used_percent": 25.0, "window_minutes": 300, "resets_at": 1},
                "secondary": {"used_percent": 5.0, "window_minutes": 10080, "resets_at": 2},
            },
        )
        write_codex_rollout(
            self.codex_sessions_dir, "sess-new",
            cwd="/Users/me/acme/repo",
            turns=[("2026-04-29T03:00:00Z", 100)],
            rate_limits={
                "plan_type": "pro",
                "primary": {"used_percent": 73.0, "window_minutes": 300, "resets_at": 9},
                "secondary": {"used_percent": 18.0, "window_minutes": 10080, "resets_at": 99},
            },
        )
        summary = self.run_oneshot()
        rl = summary["codex"]["rate_limit"]
        self.assertEqual(rl["primary"]["used_percent"], 73.0)
        self.assertEqual(rl["primary"]["resets_at"], 9)
        self.assertEqual(rl["secondary"]["used_percent"], 18.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
