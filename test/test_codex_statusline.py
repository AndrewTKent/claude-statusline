from __future__ import annotations

import base64
import dataclasses
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "codex_statusline.py"
SPEC = importlib.util.spec_from_file_location("codex_statusline", MODULE_PATH)
assert SPEC is not None
codex_statusline = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = codex_statusline
SPEC.loader.exec_module(codex_statusline)


class CodexStatuslineTest(unittest.TestCase):
    def test_format_tokens(self) -> None:
        self.assertEqual(codex_statusline.format_tokens(999), "999")
        self.assertEqual(codex_statusline.format_tokens(12_345), "12.3k")
        self.assertEqual(codex_statusline.format_tokens(1_900_000), "1.9M")

    def test_decode_jwt_payload(self) -> None:
        payload = {"email": "andrew@example.com"}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        self.assertEqual(codex_statusline.decode_jwt_payload(f"x.{encoded}.y"), payload)

    def test_paths_related(self) -> None:
        self.assertTrue(codex_statusline.paths_related("/tmp/project", "/tmp/project/src"))
        self.assertTrue(codex_statusline.paths_related("/tmp/project/src", "/tmp/project"))
        self.assertFalse(codex_statusline.paths_related("/tmp/project-a", "/tmp/project-b"))

    def test_parse_shell_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "statusline.conf"
            path.write_text("export DAILY_TOKEN_GOAL=42\nBAD-KEY=1\nNAME='codex'\n")
            self.assertEqual(
                codex_statusline.parse_shell_config(path),
                {"DAILY_TOKEN_GOAL": "42", "NAME": "codex"},
            )

    def test_parse_args_survives_non_numeric_columns_env(self) -> None:
        with mock.patch.dict(os.environ, {"COLUMNS": "abc"}):
            args = codex_statusline.parse_args(["--json"])

        self.assertGreater(args.width, 0)

    def test_parse_args_uses_explicit_config_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "statusline.conf"
            path.write_text(
                "CODEX_STATUSLINE_FORMAT=sigil\n"
                "CODEX_THREAD_ID=configured-thread\n"
            )
            with mock.patch.dict(
                os.environ,
                {"CODEX_STATUSLINE_FORMAT": "", "CODEX_THREAD_ID": ""},
            ):
                args = codex_statusline.parse_args(["--config", str(path)])

            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "environment-thread"}):
                environment_args = codex_statusline.parse_args(["--config", str(path)])

        self.assertEqual(args.format, "sigil")
        self.assertEqual(args.thread_id, "configured-thread")
        self.assertEqual(environment_args.thread_id, "environment-thread")

    def test_latest_token_count_reads_rollout_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {"model_context_window": 1000},
                                    "rate_limits": {"primary": {"used_percent": 6.0}},
                                },
                            }
                        ),
                    ]
                )
            )

            self.assertEqual(
                codex_statusline.latest_token_count(str(path))["rate_limits"]["primary"]["used_percent"],
                6.0,
            )

    def test_rollout_cache_finishes_partial_appends_without_duplicates(self) -> None:
        codex_statusline.ROLLOUT_CACHE.clear()
        first = {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "one"}}
        second = {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "one"}}
        encoded_second = json.dumps(second).encode()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout.jsonl"
            path.write_bytes(json.dumps(first).encode() + b"\n" + encoded_second[:20])
            self.assertEqual(codex_statusline.read_rollout_events(str(path)), [first])

            with path.open("ab") as stream:
                stream.write(encoded_second[20:] + b"\n")

            self.assertEqual(codex_statusline.read_rollout_events(str(path)), [first, second])
            self.assertEqual(codex_statusline.read_rollout_events(str(path)), [first, second])

    def test_rollout_cache_evicts_old_paths(self) -> None:
        codex_statusline.ROLLOUT_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmpdir:
            for index in range(codex_statusline.MAX_ROLLOUT_CACHE + 3):
                path = Path(tmpdir) / f"{index}.jsonl"
                path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
                codex_statusline.read_rollout_events(str(path))

        self.assertEqual(len(codex_statusline.ROLLOUT_CACHE), codex_statusline.MAX_ROLLOUT_CACHE)
        codex_statusline.ROLLOUT_CACHE.clear()

    def test_rollout_cache_discards_unread_entries_when_open_fails(self) -> None:
        codex_statusline.ROLLOUT_CACHE.clear()
        event = {"type": "event_msg", "payload": {"type": "task_started"}}
        partial_event = {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "one"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = []
            for index in range(codex_statusline.MAX_ROLLOUT_CACHE + 7):
                path = Path(tmpdir) / f"{index}.jsonl"
                path.write_text(json.dumps(event) + "\n")
                paths.append(str(path))
            seeded = paths[0]
            with Path(seeded).open("ab") as stream:
                stream.write(json.dumps(partial_event).encode())
            self.assertEqual(codex_statusline.read_rollout_events(seeded), [event, partial_event])

            with mock.patch.object(codex_statusline.Path, "open", side_effect=OSError):
                for path in paths[1:]:
                    self.assertEqual(codex_statusline.read_rollout_events(path), [])
                self.assertEqual(codex_statusline.read_rollout_events(seeded), [event, partial_event])

        self.assertEqual(list(codex_statusline.ROLLOUT_CACHE), [seeded])
        codex_statusline.ROLLOUT_CACHE.clear()

    def test_rollout_cache_does_not_retain_oversized_event_history(self) -> None:
        codex_statusline.ROLLOUT_CACHE.clear()
        events = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": str(index)}}
            for index in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "large.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            with mock.patch.object(codex_statusline, "MAX_ROLLOUT_CACHE_EVENTS", 3):
                first = codex_statusline.read_rollout_events(str(path))
                self.assertNotIn(str(path), codex_statusline.ROLLOUT_CACHE)
                second = codex_statusline.read_rollout_events(str(path))

        self.assertEqual(first, events)
        self.assertEqual(second, events)
        self.assertNotIn(str(path), codex_statusline.ROLLOUT_CACHE)

    def test_rollout_cache_recovers_oversized_partial_event_after_completion(self) -> None:
        codex_statusline.ROLLOUT_CACHE.clear()
        event = {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "x" * 200},
        }
        encoded = json.dumps(event).encode()
        split_at = len(encoded) - 10

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "partial.jsonl"
            path.write_bytes(encoded[:split_at])
            with mock.patch.object(codex_statusline, "MAX_ROLLOUT_CACHE_PARTIAL_BYTES", 64):
                self.assertEqual(codex_statusline.read_rollout_events(str(path)), [])
                self.assertNotIn(str(path), codex_statusline.ROLLOUT_CACHE)

                with path.open("ab") as stream:
                    stream.write(encoded[split_at:] + b"\n")

                completed = codex_statusline.read_rollout_events(str(path))
                repeated = codex_statusline.read_rollout_events(str(path))

        self.assertEqual(completed, [event])
        self.assertEqual(repeated, [event])
        self.assertEqual(completed.count(event), 1)

    def test_format_reset_same_day(self) -> None:
        now = datetime.fromtimestamp(1777428000).astimezone()
        rendered = codex_statusline.format_reset(1777433774, now)
        self.assertTrue(rendered.startswith("resets "))
        self.assertNotIn("apr", rendered.lower())

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone support")
    def test_local_usage_boundaries_follow_daylight_saving_transitions(self) -> None:
        original_timezone = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/Los_Angeles"
            time.tzset()
            zone = ZoneInfo("America/Los_Angeles")
            cases = (
                (
                    datetime(2026, 3, 8, 12, tzinfo=zone),
                    datetime(2026, 3, 8, 0, tzinfo=zone),
                    datetime(2026, 3, 2, 0, tzinfo=zone),
                ),
                (
                    datetime(2026, 11, 1, 12, tzinfo=zone),
                    datetime(2026, 11, 1, 0, tzinfo=zone),
                    datetime(2026, 10, 26, 0, tzinfo=zone),
                ),
            )
            for now, expected_midnight, expected_week in cases:
                with self.subTest(now=now):
                    self.assertEqual(codex_statusline.local_midnight(now), int(expected_midnight.timestamp()))
                    self.assertEqual(codex_statusline.week_start(now), int(expected_week.timestamp()))
        finally:
            if original_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_timezone
            time.tzset()

    def test_token_summary_counts_post_midnight_tokens_from_older_thread(self) -> None:
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        midnight = codex_statusline.local_midnight(now)
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout = Path(tmpdir) / "overnight.jsonl"
            events = [
                {
                    "timestamp": datetime.fromtimestamp(midnight - 10).astimezone().isoformat(),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 100}},
                    },
                },
                {
                    "timestamp": datetime.fromtimestamp(midnight + 10).astimezone().isoformat(),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 150}},
                    },
                },
            ]
            rollout.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "create table threads (id text, rollout_path text, created_at integer, updated_at integer, tokens_used integer)"
            )
            conn.execute(
                "insert into threads values ('overnight', ?, ?, ?, 150)",
                (str(rollout), midnight - 3600, midnight + 10),
            )
            conn.execute(
                "insert into threads values ('new', '/tmp/new.jsonl', ?, ?, 200)",
                (midnight + 1, midnight + 2),
            )
            thread = codex_statusline.Thread(
                id="overnight",
                source="cli",
                rollout_path=str(rollout),
                created_at=midnight - 3600,
                updated_at=midnight + 10,
                cwd="/tmp",
                title="",
                tokens_used=150,
                model="",
                reasoning_effort="",
                sandbox_policy="",
                approval_mode="",
                git_branch="",
                archived=0,
            )

            summary = codex_statusline.token_summary(conn, thread, now)
            conn.close()

        self.assertEqual(summary.today, 250)
        self.assertEqual(summary.lifetime, 350)

    def test_rollout_activity_counts_tools_and_active_shells(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "task_started", "turn_id": "turn-1", "started_at": 100},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call",
                                    "name": "exec",
                                    "call_id": "call-1",
                                    "input": json.dumps({"cmd": "git status --short"}),
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "user_message",
                                    "message": "run the thing",
                                },
                            }
                        ),
                    ]
                )
            )
            thread = codex_statusline.Thread(
                id="thread",
                source="cli",
                rollout_path=str(path),
                created_at=0,
                updated_at=0,
                cwd="/tmp",
                title="",
                tokens_used=0,
                model="",
                reasoning_effort="",
                sandbox_policy="",
                approval_mode="",
                git_branch="",
                archived=0,
            )

            activity = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(130))

        self.assertEqual(activity.turns_started, 1)
        self.assertEqual(activity.shell_calls, 1)
        self.assertEqual(activity.active_tools, 1)
        self.assertEqual(activity.active_shells, 1)
        self.assertEqual(activity.active_turn_seconds, 30)
        self.assertEqual(activity.active_tool, "exec")
        self.assertEqual(activity.last_command, "git status --short")

    def test_rollout_activity_keeps_yielded_shell_active_until_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout.jsonl"
            events = [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1", "started_at": 100},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "start",
                        "arguments": json.dumps({"cmd": "sleep 30"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "start",
                        "output": [
                            {
                                "type": "input_text",
                                "text": json.dumps({"session_id": 123, "output": ""}),
                            }
                        ],
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            thread = codex_statusline.Thread(
                id="thread",
                source="cli",
                rollout_path=str(path),
                created_at=100,
                updated_at=130,
                cwd="/tmp",
                title="",
                tokens_used=0,
                model="",
                reasoning_effort="",
                sandbox_policy="",
                approval_mode="",
                git_branch="",
                archived=0,
            )

            yielded = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(130))
            with path.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "custom_tool_call",
                                "name": "write_stdin",
                                "call_id": "poll",
                                "arguments": json.dumps({"session_id": 123}),
                            },
                        }
                    )
                    + "\n"
                )
                stream.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "custom_tool_call_output",
                                "call_id": "poll",
                                "output": [
                                    {
                                        "type": "input_text",
                                        "text": json.dumps({"exit_code": 0, "output": "done"}),
                                    }
                                ],
                            },
                        }
                    )
                    + "\n"
                )

            finished = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(131))

        self.assertEqual(yielded.active_shells, 1)
        self.assertEqual(finished.active_shells, 0)

    def test_rollout_activity_closes_exec_command_end_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout.jsonl"
            events = [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1", "started_at": 100},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "call-1",
                        "arguments": json.dumps({"cmd": "git status --short"}),
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "exec_command_end",
                        "call_id": "call-1",
                        "command": ["git", "status", "--short"],
                        "exit_code": 0,
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            thread = codex_statusline.Thread(
                id="thread",
                source="cli",
                rollout_path=str(path),
                created_at=100,
                updated_at=120,
                cwd="/tmp",
                title="",
                tokens_used=0,
                model="",
                reasoning_effort="",
                sandbox_policy="",
                approval_mode="",
                git_branch="",
                archived=0,
            )

            activity = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(120))

        self.assertEqual(activity.active_tools, 0)
        self.assertEqual(activity.active_shells, 0)
        self.assertEqual(activity.last_command, "git status --short")

    def test_rollout_activity_clears_stale_tools_after_aborted_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout.jsonl"
            events = [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1", "started_at": 100},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "call-1",
                        "arguments": json.dumps({"cmd": "sleep 30"}),
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "turn_id": "turn-1"},
                },
            ]
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            thread = codex_statusline.Thread(
                id="thread",
                source="cli",
                rollout_path=str(path),
                created_at=100,
                updated_at=100,
                cwd="/tmp",
                title="",
                tokens_used=0,
                model="",
                reasoning_effort="",
                sandbox_policy="",
                approval_mode="",
                git_branch="",
                archived=0,
            )

            activity = codex_statusline.rollout_activity(
                thread,
                datetime.fromtimestamp(2000),
                active_window_seconds=900,
            )

        self.assertEqual(activity.active_tools, 0)
        self.assertEqual(activity.active_shells, 0)
        self.assertEqual(activity.active_turn_seconds, 0)

    def test_subagent_activity_ignores_parent_turn_before_fork(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout.jsonl"
            events = [
                {
                    "timestamp": "2026-07-09T10:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "child"},
                },
                {
                    "timestamp": "2026-07-09T10:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "parent", "started_at": 1},
                },
                {
                    "timestamp": "2026-07-09T10:00:00.020Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "child", "started_at": 2},
                },
                {
                    "timestamp": "2026-07-09T10:00:02Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "child"},
                },
            ]
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            thread = codex_statusline.Thread(
                id="child",
                source='{"subagent":{}}',
                rollout_path=str(path),
                created_at=2,
                updated_at=0,
                cwd="/tmp",
                title="",
                tokens_used=0,
                model="",
                reasoning_effort="",
                sandbox_policy="",
                approval_mode="",
                git_branch="",
                archived=0,
            )

            activity = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(10))

        self.assertEqual(activity.turns_started, 1)
        self.assertEqual(activity.turns_completed, 1)
        self.assertEqual(activity.active_turn_seconds, 0)
        self.assertEqual(codex_statusline.top_status(activity.__dict__, 1), "DONE")

    def test_thread_selection_filters_missing_and_follows_nested_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout = Path(tmpdir) / "rollout.jsonl"
            rollout.write_text("{}\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    updated_at integer, updated_at_ms integer, cwd text, title text,
                    tokens_used integer, model text, reasoning_effort text,
                    sandbox_policy text, approval_mode text, git_branch text,
                    archived integer, agent_path text, agent_nickname text
                )
                """
            )
            conn.execute(
                "create table thread_spawn_edges (parent_thread_id text, child_thread_id text, status text)"
            )
            rows = [(f"root-{index}", "cli", str(rollout), 100 - index) for index in range(7)]
            rows.extend(
                [
                    ("current-agent", '{"subagent":{}}', str(rollout), 100),
                    ("grandchild", '{"subagent":{}}', str(rollout), 99),
                    ("old-agent", '{"subagent":{}}', str(rollout), 1),
                    ("missing", "cli", "/missing/rollout.jsonl", 200),
                ]
            )
            for thread_id, source, path, updated_at in rows:
                conn.execute(
                    "insert into threads values (?, ?, ?, 0, ?, ?, '/tmp', '', 0, '', '', '', '', '', 0, '', '')",
                    (thread_id, source, path, updated_at, updated_at * 1000),
                )
            conn.execute("insert into thread_spawn_edges values ('root-0', 'current-agent', 'open')")
            conn.execute("insert into thread_spawn_edges values ('current-agent', 'grandchild', 'open')")
            conn.execute("insert into thread_spawn_edges values ('root-6', 'old-agent', 'open')")

            threads = codex_statusline.select_threads(conn, 10, False)
            top_threads = codex_statusline.select_top_threads(conn, 30)
            conn.close()

        self.assertNotIn("missing", [thread.id for thread in threads])
        top_ids = [thread.id for thread in top_threads]
        self.assertIn("current-agent", top_ids)
        self.assertIn("grandchild", top_ids)
        self.assertIn("old-agent", top_ids)
        self.assertIn("root-6", top_ids)

    def test_top_thread_selection_includes_exec_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout = Path(tmpdir) / "rollout.jsonl"
            rollout.write_text("{}\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    updated_at integer, updated_at_ms integer, cwd text, title text,
                    tokens_used integer, model text, reasoning_effort text,
                    sandbox_policy text, approval_mode text, git_branch text,
                    archived integer, agent_path text, agent_nickname text
                )
                """
            )
            conn.execute(
                "create table thread_spawn_edges (parent_thread_id text, child_thread_id text, status text)"
            )
            rows = (
                ("exec-newest", "exec", 300),
                ("exec-newer", "exec", 200),
                ("root", "cli", 100),
            )
            for thread_id, source, updated_at in rows:
                conn.execute(
                    "insert into threads values (?, ?, ?, 0, ?, ?, '/tmp', '', 0, '', '', '', '', '', 0, '', '')",
                    (thread_id, source, str(rollout), updated_at, updated_at * 1000),
                )

            selected = codex_statusline.select_top_threads(conn, 3)
            conn.close()

        self.assertEqual(
            [thread.id for thread in selected],
            ["exec-newest", "exec-newer", "root"],
        )

    def test_owner_thread_selection_chooses_process_root_over_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            root_rollout = tmp / "rollout-root.jsonl"
            child_rollout = tmp / "rollout-child.jsonl"
            root_rollout.write_text("{}\n")
            child_rollout.write_text("{}\n")
            pid_file = tmp / "owner.pid"
            pid_file.write_text("123\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    updated_at integer, updated_at_ms integer, cwd text, title text,
                    tokens_used integer, model text, reasoning_effort text,
                    sandbox_policy text, approval_mode text, git_branch text,
                    archived integer, agent_path text, agent_nickname text
                )
                """
            )
            rows = (
                ("root", "cli", str(root_rollout), 100),
                ("child", '{"subagent":{}}', str(child_rollout), 200),
            )
            for thread_id, source, path, updated_at in rows:
                conn.execute(
                    "insert into threads values (?, ?, ?, 0, ?, ?, '/tmp', '', 0, '', '', '', '', '', 0, '', '')",
                    (thread_id, source, path, updated_at, updated_at * 1000),
                )

            with mock.patch.object(
                codex_statusline,
                "process_rollout_paths",
                return_value={str(root_rollout), str(child_rollout)},
            ):
                selected = codex_statusline.select_owner_thread_id(conn, str(pid_file))
            conn.close()

        self.assertEqual(selected, "root")

    def test_owner_thread_selection_binds_root_exec_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            root_rollout = tmp / "rollout-root.jsonl"
            child_rollout = tmp / "rollout-child.jsonl"
            root_rollout.write_text("{}\n")
            child_rollout.write_text("{}\n")
            pid_file = tmp / "owner.pid"
            pid_file.write_text("123\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    updated_at integer, updated_at_ms integer, cwd text, title text,
                    tokens_used integer, model text, reasoning_effort text,
                    sandbox_policy text, approval_mode text, git_branch text,
                    archived integer, agent_path text, agent_nickname text
                )
                """
            )
            rows = (
                ("exec-root", "exec", str(root_rollout), 100),
                ("child", '{"subagent":{}}', str(child_rollout), 200),
            )
            for thread_id, source, path, updated_at in rows:
                conn.execute(
                    "insert into threads values (?, ?, ?, 0, ?, ?, '/tmp', '', 0, '', '', '', '', '', 0, '', '')",
                    (thread_id, source, path, updated_at, updated_at * 1000),
                )

            with mock.patch.object(
                codex_statusline,
                "process_rollout_paths",
                return_value={str(root_rollout), str(child_rollout)},
            ):
                selected = codex_statusline.select_owner_thread_id(conn, str(pid_file))
            conn.close()

        self.assertEqual(selected, "exec-root")

    def test_owner_thread_selection_prefers_cli_root_over_nested_exec(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            root_rollout = tmp / "rollout-root.jsonl"
            nested_rollout = tmp / "rollout-nested.jsonl"
            root_rollout.write_text("{}\n")
            nested_rollout.write_text("{}\n")
            pid_file = tmp / "owner.pid"
            pid_file.write_text("123\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    updated_at integer, updated_at_ms integer, cwd text, title text,
                    tokens_used integer, model text, reasoning_effort text,
                    sandbox_policy text, approval_mode text, git_branch text,
                    archived integer, agent_path text, agent_nickname text
                )
                """
            )
            rows = (
                ("cockpit-root", "cli", str(root_rollout), 100),
                ("nested-exec", "exec", str(nested_rollout), 200),
            )
            for thread_id, source, path, updated_at in rows:
                conn.execute(
                    "insert into threads values (?, ?, ?, 0, ?, ?, '/tmp', '', 0, '', '', '', '', '', 0, '', '')",
                    (thread_id, source, path, updated_at, updated_at * 1000),
                )

            with mock.patch.object(
                codex_statusline,
                "process_rollout_paths",
                return_value={str(root_rollout), str(nested_rollout)},
            ):
                selected = codex_statusline.select_owner_thread_id(conn, str(pid_file))
            conn.close()

        self.assertEqual(selected, "cockpit-root")

    def test_owner_thread_selection_ignores_concurrent_root_outside_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            owned_rollout = tmp / "rollout-owned.jsonl"
            other_rollout = tmp / "rollout-other.jsonl"
            owned_rollout.write_text("{}\n")
            other_rollout.write_text("{}\n")
            pid_file = tmp / "owner.pid"
            pid_file.write_text("123\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    updated_at integer, updated_at_ms integer, cwd text, title text,
                    tokens_used integer, model text, reasoning_effort text,
                    sandbox_policy text, approval_mode text, git_branch text,
                    archived integer, agent_path text, agent_nickname text
                )
                """
            )
            rows = (
                ("owned-root", "cli", str(owned_rollout), 100),
                ("concurrent-root", "cli", str(other_rollout), 200),
            )
            for thread_id, source, path, updated_at in rows:
                conn.execute(
                    "insert into threads values (?, ?, ?, 0, ?, ?, '/tmp', '', 0, '', '', '', '', '', 0, '', '')",
                    (thread_id, source, path, updated_at, updated_at * 1000),
                )

            with mock.patch.object(
                codex_statusline,
                "process_rollout_paths",
                return_value={str(owned_rollout)},
            ):
                selected = codex_statusline.select_owner_thread_id(conn, str(pid_file))
            conn.close()

        self.assertEqual(selected, "owned-root")

    def test_owner_thread_selection_matches_realpath_of_symlinked_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            real_dir = tmp / "real"
            real_dir.mkdir()
            link_dir = tmp / "link"
            link_dir.symlink_to(real_dir)
            rollout = link_dir / "rollout-root.jsonl"
            rollout.write_text("{}\n")
            pid_file = tmp / "owner.pid"
            pid_file.write_text("123\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    updated_at integer, updated_at_ms integer, cwd text, title text,
                    tokens_used integer, model text, reasoning_effort text,
                    sandbox_policy text, approval_mode text, git_branch text,
                    archived integer, agent_path text, agent_nickname text
                )
                """
            )
            conn.execute(
                "insert into threads values (?, ?, ?, 0, ?, ?, '/tmp', '', 0, '', '', '', '', '', 0, '', '')",
                ("root", "cli", str(rollout), 100, 100_000),
            )

            resolved = os.path.realpath(str(rollout))
            with mock.patch.object(
                codex_statusline,
                "process_rollout_paths",
                return_value={resolved},
            ):
                selected = codex_statusline.select_owner_thread_id(conn, str(pid_file))
            conn.close()

        self.assertEqual(selected, "root")

    def test_process_rollout_paths_finds_open_rollout_unmocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout = Path(tmpdir) / "rollout-integration.jsonl"
            rollout.write_text("{}\n")
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys, time\nhandle = open(sys.argv[1])\nprint('ready', flush=True)\ntime.sleep(30)",
                    str(rollout),
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert child.stdout is not None
                self.assertEqual(child.stdout.readline().strip(), "ready")
                found = codex_statusline.process_rollout_paths(os.getpid())
            finally:
                child.kill()
                child.wait()

        self.assertIn(
            os.path.realpath(str(rollout)),
            {os.path.realpath(path) for path in found},
        )

    def test_snapshot_prefers_process_owner_over_inherited_thread_id(self) -> None:
        args = codex_statusline.parse_args(["--owner-pid-file", "/tmp/owner.pid"])
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False

        with (
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "inherited"}),
            mock.patch.object(codex_statusline, "STATE_DB", MODULE_PATH),
            mock.patch.object(codex_statusline, "sqlite_connect", return_value=connection),
            mock.patch.object(codex_statusline, "select_owner_thread_id", return_value="owned") as owner,
            mock.patch.object(codex_statusline, "select_thread", return_value=None) as selector,
            self.assertRaisesRegex(RuntimeError, "no Codex threads found"),
        ):
            codex_statusline.snapshot(args)

        owner.assert_called_once_with(connection, "/tmp/owner.pid")
        self.assertEqual(selector.call_args.args[1], "owned")

    def test_all_sessions_computes_global_token_totals_once(self) -> None:
        args = codex_statusline.parse_args(["--all"])
        thread = codex_statusline.Thread(
            id="one",
            source="cli",
            rollout_path="/tmp/one.jsonl",
            created_at=0,
            updated_at=0,
            cwd="/tmp",
            title="",
            tokens_used=1,
            model="",
            reasoning_effort="",
            sandbox_policy="",
            approval_mode="",
            git_branch="",
            archived=0,
        )
        second = codex_statusline.Thread(**{**thread.__dict__, "id": "two"})
        totals = codex_statusline.TokenSummary(0, 10, 20, 30, 1, 2)
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False

        with (
            mock.patch.object(codex_statusline, "STATE_DB", MODULE_PATH),
            mock.patch.object(codex_statusline, "sqlite_connect", return_value=connection),
            mock.patch.object(codex_statusline, "select_threads", return_value=[thread, second]),
            mock.patch.object(codex_statusline, "token_summary", return_value=totals) as summary,
            mock.patch.object(
                codex_statusline,
                "snapshot_for_thread",
                side_effect=lambda current, *_args, **_kwargs: {
                    "thread_id": current.id,
                    "usage": {},
                },
            ),
        ):
            data = codex_statusline.all_sessions_snapshot(args)

        self.assertEqual([session["thread_id"] for session in data["sessions"]], ["one", "two"])
        summary.assert_called_once()

    def test_all_sessions_rate_limits_skip_empty_placeholder_sessions(self) -> None:
        args = codex_statusline.parse_args(["--all"])
        fresh = codex_statusline.Thread(
            id="fresh",
            source="cli",
            rollout_path="/tmp/fresh.jsonl",
            created_at=0,
            updated_at=0,
            cwd="/tmp",
            title="",
            tokens_used=0,
            model="",
            reasoning_effort="",
            sandbox_policy="",
            approval_mode="",
            git_branch="",
            archived=0,
        )
        limited = codex_statusline.Thread(**{**fresh.__dict__, "id": "limited"})
        rate_limits = {
            "fresh": {"primary": {}, "secondary": {}},
            "limited": {"primary": {"used_percent": 100.0, "resets_at": 1783652044}, "secondary": {}},
        }
        totals = codex_statusline.TokenSummary(0, 10, 20, 30, 1, 2)
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.__exit__.return_value = False

        with (
            mock.patch.object(codex_statusline, "STATE_DB", MODULE_PATH),
            mock.patch.object(codex_statusline, "sqlite_connect", return_value=connection),
            mock.patch.object(codex_statusline, "select_threads", return_value=[fresh, limited]),
            mock.patch.object(codex_statusline, "token_summary", return_value=totals),
            mock.patch.object(
                codex_statusline,
                "snapshot_for_thread",
                side_effect=lambda current, *_args, **_kwargs: {
                    "thread_id": current.id,
                    "usage": {"rate_limits": rate_limits[current.id]},
                },
            ),
        ):
            data = codex_statusline.all_sessions_snapshot(args)

        self.assertEqual(data["rate_limits"], rate_limits["limited"])

    def test_select_thread_prefers_root_over_newer_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout = Path(tmpdir) / "rollout.jsonl"
            rollout.write_text("{}\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    updated_at integer, updated_at_ms integer, cwd text, title text,
                    tokens_used integer, model text, reasoning_effort text,
                    sandbox_policy text, approval_mode text, git_branch text,
                    archived integer, agent_path text, agent_nickname text
                )
                """
            )
            rows = [
                ("root", "cli", 100),
                ("agent", '{"subagent":{}}', 200),
            ]
            for thread_id, source, updated_at in rows:
                conn.execute(
                    "insert into threads values (?, ?, ?, 0, ?, ?, '/tmp/project', '', 0, '', '', '', '', '', 0, '', '')",
                    (thread_id, source, str(rollout), updated_at, updated_at * 1000),
                )

            thread = codex_statusline.select_thread(conn, "", "/tmp/project")
            conn.close()

        self.assertIsNotNone(thread)
        self.assertEqual(thread.id, "root")

    def test_select_thread_started_after_ignores_older_and_nested_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout = Path(tmpdir) / "rollout.jsonl"
            rollout.write_text("{}\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    created_at_ms integer, updated_at integer, updated_at_ms integer,
                    cwd text, title text, tokens_used integer, model text,
                    reasoning_effort text, sandbox_policy text, approval_mode text,
                    git_branch text, archived integer, agent_path text, agent_nickname text
                )
                """
            )
            conn.execute(
                "insert into threads values ('older', 'cli', ?, 100, 100000, 400, 400000, '/tmp/project', '', 0, '', '', '', '', '', 0, '', '')",
                (str(rollout),),
            )
            conn.execute(
                "insert into threads values ('nested', 'cli', ?, 200, 200100, 400, 400000, '/tmp/project/nested', '', 0, '', '', '', '', '', 0, '', '')",
                (str(rollout),),
            )
            conn.execute(
                "insert into threads values ('launched', 'cli', ?, 200, 200200, 200, 200200, '/tmp/project', '', 0, '', '', '', '', '', 0, '', '')",
                (str(rollout),),
            )

            launched = codex_statusline.select_thread(conn, "", "/tmp/project", created_after_ms=200150)
            sticky = codex_statusline.select_thread(
                conn,
                "launched",
                "/tmp/project",
                created_after_ms=200150,
            )
            conn.close()

        self.assertIsNotNone(launched)
        self.assertEqual(launched.id, "launched")
        self.assertIsNotNone(sticky)
        self.assertEqual(sticky.id, "launched")

    def test_select_thread_updated_after_finds_resumed_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout = Path(tmpdir) / "rollout.jsonl"
            rollout.write_text("{}\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    created_at_ms integer, updated_at integer, updated_at_ms integer,
                    cwd text, title text, tokens_used integer, model text,
                    reasoning_effort text, sandbox_policy text, approval_mode text,
                    git_branch text, archived integer, agent_path text, agent_nickname text
                )
                """
            )
            conn.execute(
                "insert into threads values ('resumed', 'cli', ?, 100, 100000, 300, 300200, '/tmp/project', '', 0, '', '', '', '', '', 0, '', '')",
                (str(rollout),),
            )

            resumed = codex_statusline.select_thread(
                conn,
                "",
                "/tmp/project",
                updated_after_ms=300100,
            )
            conn.close()

        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.id, "resumed")

    def test_select_descendant_threads_follows_nested_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout = Path(tmpdir) / "rollout.jsonl"
            rollout.write_text("{}\n")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                create table threads (
                    id text, source text, rollout_path text, created_at integer,
                    updated_at integer, updated_at_ms integer, cwd text, title text,
                    tokens_used integer, model text, reasoning_effort text,
                    sandbox_policy text, approval_mode text, git_branch text,
                    archived integer, agent_path text, agent_nickname text
                )
                """
            )
            conn.execute(
                "create table thread_spawn_edges (parent_thread_id text, child_thread_id text, status text)"
            )
            for thread_id in ("root", "child", "grandchild"):
                conn.execute(
                    "insert into threads values (?, ?, ?, 0, 0, 0, '/tmp', '', 0, '', '', '', '', '', 0, '', '')",
                    (thread_id, "cli" if thread_id == "root" else '{"subagent":{}}', str(rollout)),
                )
            conn.execute("insert into thread_spawn_edges values ('root', 'child', 'open')")
            conn.execute("insert into thread_spawn_edges values ('child', 'grandchild', 'open')")
            conn.execute("insert into thread_spawn_edges values ('grandchild', 'root', 'open')")

            descendants = codex_statusline.select_descendant_threads(conn, "root")
            conn.close()

        self.assertEqual({thread.id for thread in descendants}, {"child", "grandchild"})

    def test_render_footer_shows_live_limits_and_workers_within_width(self) -> None:
        data = {
            "model_display": "GPT-5.6",
            "reasoning_effort": "max",
            "session_age_seconds": 180,
            "account": "andrew@example.com",
            "repo": "statusline",
            "branch": "feat/codex-top",
            "context_window": 1000,
            "tokens": {"today": 4000, "lifetime": 9000},
            "usage": {
                "context_window": 1000,
                "context_used": 510,
                "session_total": 5000,
                "rate_limits": {
                    "primary": {"used_percent": 67.0, "resets_at": 1777433774},
                    "secondary": {"used_percent": 13.0, "resets_at": 1778038574},
                },
            },
            "activity": {"active_tools": 1, "active_shells": 1},
            "agents": {"total": 3, "active": 1, "active_tools": 2, "active_shells": 2},
        }

        rendered = codex_statusline.render_footer(data, 80, codex_statusline.Palette(False))

        self.assertIn("model   GPT-5.6.max", rendered)
        self.assertIn("context", rendered)
        self.assertIn("5-hour", rendered)
        self.assertIn("weekly", rendered)
        self.assertIn("agents  1/3 running", rendered)
        self.assertIn("shells 3", rendered)
        self.assertTrue(all(len(line) <= 80 for line in rendered.splitlines()))

        wide_lines = codex_statusline.render_footer(data, 140, codex_statusline.Palette(False)).splitlines()
        self.assertTrue(wide_lines[0].startswith("  model"))
        self.assertFalse(any(line.startswith("─") for line in wide_lines))

        narrow_lines = codex_statusline.render_footer(data, 40, codex_statusline.Palette(False)).splitlines()
        self.assertTrue(all(len(line) <= 40 for line in narrow_lines))
        tiny_lines = codex_statusline.render_footer(data, 8, codex_statusline.Palette(False)).splitlines()
        self.assertTrue(all(len(line) <= 8 for line in tiny_lines))

    def test_render_footer_labels_weekly_only_primary_by_window(self) -> None:
        data = {
            "model_display": "GPT-5.6",
            "reasoning_effort": "max",
            "session_age_seconds": 180,
            "account": "andrew@example.com",
            "repo": "statusline",
            "branch": "main",
            "tokens": {"today": 0, "lifetime": 0},
            "usage": {
                "context_window": 0,
                "session_total": 0,
                "rate_limits": {
                    "primary": {
                        "used_percent": 96.0,
                        "window_minutes": 10_080,
                        "resets_at": 1784487602,
                    },
                    "secondary": {},
                },
            },
            "activity": {"active_tools": 0, "active_shells": 0},
        }

        rendered = codex_statusline.render_footer(data, 100, codex_statusline.Palette(False))

        self.assertIn("weekly", rendered)
        self.assertNotIn("5-hour", rendered)

    def test_labeled_rate_limits_uses_reported_window(self) -> None:
        cases = (
            (300, "5-hour"),
            (10_080, "weekly"),
            ("10080", "weekly"),
            (1_440, "1-day"),
            (60, "1-hour"),
            (90, "90-minute"),
            (True, "limit"),
            (1_440.5, "limit"),
        )
        for window_minutes, expected in cases:
            with self.subTest(window_minutes=window_minutes):
                limits = codex_statusline.labeled_rate_limits(
                    {"primary": {"used_percent": 10, "window_minutes": window_minutes}}
                )
                self.assertEqual(limits[0][0], expected)

        fallback_limits = codex_statusline.labeled_rate_limits(
            {
                "primary": {"used_percent": 10},
                "secondary": {"used_percent": 20},
            }
        )
        self.assertEqual([label for label, _ in fallback_limits], ["5-hour", "weekly"])

    def test_labeled_rate_limits_skips_percentless_bucket(self) -> None:
        limits = codex_statusline.labeled_rate_limits(
            {
                "primary": {"used_percent": None, "window_minutes": 10_080},
                "secondary": {"used_percent": 50, "window_minutes": 10_080},
            }
        )

        self.assertEqual(limits, [("weekly", {"used_percent": 50, "window_minutes": 10_080})])

    def test_labeled_rate_limits_keeps_first_duplicate_window(self) -> None:
        limits = codex_statusline.labeled_rate_limits(
            {
                "primary": {"used_percent": 10, "window_minutes": 10_080},
                "secondary": {"used_percent": 20, "window_minutes": 10_080},
            }
        )

        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0][1]["used_percent"], 10)

    def test_live_state_labels_weekly_only_primary_by_window(self) -> None:
        live_state = MODULE_PATH.with_name("live-state.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            claude_home = home / ".claude"
            claude_home.mkdir()
            (claude_home / "token-scan-summary.json").write_text(
                json.dumps(
                    {
                        "codex": {
                            "rate_limit": {
                                "primary": {
                                    "used_percent": 96.0,
                                    "window_minutes": 10_080,
                                    "resets_at": 1_784_487_602,
                                },
                                "secondary": None,
                            }
                        }
                    }
                )
            )

            result = subprocess.run(
                [sys.executable, str(live_state), "--render"],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": str(home)},
            )

        self.assertIn("codex 7d: 96%", result.stdout)
        self.assertNotIn("codex 5h:", result.stdout)

    def test_watch_refreshes_footer_width_from_terminal(self) -> None:
        args = codex_statusline.parse_args(["--footer", "--watch", "1"])
        widths = []

        def stop_after_render(_data, current_args, _palette):
            widths.append(current_args.width)
            raise KeyboardInterrupt

        with (
            mock.patch.object(codex_statusline, "terminal_size", return_value=os.terminal_size((137, 40))),
            mock.patch.object(codex_statusline, "snapshot", return_value={}),
            mock.patch.object(codex_statusline, "render", side_effect=stop_after_render),
        ):
            self.assertEqual(codex_statusline.watch(args, codex_statusline.Palette(False)), 0)

        self.assertEqual(widths, [137])

    def test_watch_freezes_bound_thread_after_first_snapshot(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "ambient-thread"}):
            args = codex_statusline.parse_args(
                ["--footer", "--watch", "1", "--bind-after-ms", "150000"]
            )
        thread_ids = []

        def stop_after_render(_data, current_args, _palette):
            thread_ids.append(current_args.thread_id)
            raise KeyboardInterrupt

        with (
            mock.patch.object(codex_statusline, "snapshot", return_value={"thread_id": "launched"}),
            mock.patch.object(codex_statusline, "render", side_effect=stop_after_render),
        ):
            self.assertEqual(codex_statusline.watch(args, codex_statusline.Palette(False)), 0)

        self.assertEqual(thread_ids, ["launched"])

    def test_watch_rechecks_process_owner_instead_of_freezing_thread(self) -> None:
        args = codex_statusline.parse_args(
            ["--footer", "--watch", "1", "--owner-pid-file", "/tmp/owner.pid"]
        )
        thread_ids = []

        def render_twice(_data, current_args, _palette):
            thread_ids.append(current_args.thread_id)
            if len(thread_ids) == 2:
                raise KeyboardInterrupt
            return "status"

        with (
            mock.patch.object(
                codex_statusline,
                "snapshot",
                side_effect=({"thread_id": "first"}, {"thread_id": "second"}),
            ),
            mock.patch.object(codex_statusline, "render", side_effect=render_twice),
            mock.patch.object(codex_statusline.time, "sleep"),
        ):
            self.assertEqual(codex_statusline.watch(args, codex_statusline.Palette(False)), 0)

        self.assertEqual(thread_ids, ["", ""])

    def test_watch_keeps_footer_on_alternate_screen(self) -> None:
        args = codex_statusline.parse_args(["--footer", "--watch", "1"])
        output = io.StringIO()

        with (
            mock.patch.object(codex_statusline, "snapshot", return_value={}),
            mock.patch.object(codex_statusline, "render", side_effect=KeyboardInterrupt),
            mock.patch.object(codex_statusline.sys, "stdout", output),
        ):
            self.assertEqual(codex_statusline.watch(args, codex_statusline.Palette(False)), 0)

        self.assertTrue(output.getvalue().startswith("\033[?1049h"))
        self.assertTrue(output.getvalue().endswith("\033[?1049l"))

    def test_watch_renders_waiting_footer_before_bound_thread_exists(self) -> None:
        args = codex_statusline.parse_args(["--footer", "--watch", "1", "--bind-after-ms", "150000"])
        output = io.StringIO()

        with (
            mock.patch.object(codex_statusline, "snapshot", side_effect=RuntimeError("no Codex threads found")),
            mock.patch.object(codex_statusline.time, "sleep", side_effect=KeyboardInterrupt),
            mock.patch.object(codex_statusline.sys, "stdout", output),
        ):
            self.assertEqual(codex_statusline.watch(args, codex_statusline.Palette(False)), 0)

        self.assertIn("Waiting for this Codex session", output.getvalue())
        self.assertNotIn("status unavailable", output.getvalue())

    def test_is_subagent_thread(self) -> None:
        thread = codex_statusline.Thread(
            id="thread",
            source='{"subagent":{"thread_spawn":{"agent_nickname":"Curie"}}}',
            rollout_path="",
            created_at=0,
            updated_at=0,
            cwd="/tmp",
            title="",
            tokens_used=0,
            model="",
            reasoning_effort="",
            sandbox_policy="",
            approval_mode="",
            git_branch="",
            archived=0,
            agent_path="/root/codex_monitoring",
        )

        self.assertTrue(codex_statusline.is_subagent_thread(thread))
        self.assertEqual(codex_statusline.agent_label(thread), "codex_monitoring")

        exec_root = dataclasses.replace(thread, source="exec", agent_path="", agent_nickname="")
        self.assertFalse(codex_statusline.is_subagent_thread(exec_root))

        nicknamed_child = dataclasses.replace(exec_root, agent_nickname="Curie")
        self.assertTrue(codex_statusline.is_subagent_thread(nicknamed_child))

    def test_top_active_only_excludes_completed_agents(self) -> None:
        activity = {
            "active_tool": "-",
            "active_shells": 0,
            "active_tools": 0,
            "active_turn_seconds": 0,
            "turns_completed": 1,
            "turns_aborted": 0,
            "turns_started": 1,
        }
        completed_agent = {"activity": activity, "idle_seconds": 1, "agent": "child", "is_subagent": True}

        self.assertEqual(
            codex_statusline.filter_top_sessions([completed_agent], active_only=True, hide_inactive=True),
            [],
        )
        self.assertEqual(
            codex_statusline.filter_top_sessions([completed_agent], active_only=False, hide_inactive=True),
            [completed_agent],
        )

    def test_launcher_defaults_to_yolo_without_overriding_explicit_permissions(self) -> None:
        cockpit = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            capture = tmp / "args"
            fake_codex = tmp / "codex"
            fake_tmux = tmp / "tmux"
            fake_codex.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$CAPTURE"\n')
            fake_tmux.write_text(
                '#!/usr/bin/env bash\n'
                'if [[ "$1" == "split-window" ]]; then printf "%%1\\n"; fi\n'
            )
            fake_codex.chmod(0o755)
            fake_tmux.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "CAPTURE": str(capture),
                    "CODEX_COCKPIT_CODEX_BIN": str(fake_codex),
                    "PATH": f"{tmp}:{env['PATH']}",
                    "TMUX": "test",
                }
            )

            result = subprocess.run(
                [str(cockpit), "--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-test"],
                capture_output=True,
                check=True,
                env=env,
                text=True,
            )

            self.assertEqual(result.stderr, "")
            self.assertEqual(
                capture.read_text().splitlines(),
                [
                    "-c",
                    "tui.status_line=[]",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--model",
                    "gpt-test",
                ],
            )

            subprocess.run([str(cockpit)], check=True, env=env)
            self.assertEqual(
                capture.read_text().splitlines(),
                ["-c", "tui.status_line=[]", "--dangerously-bypass-approvals-and-sandbox"],
            )

            subprocess.run([str(cockpit), "--sandbox", "read-only"], check=True, env=env)
            self.assertEqual(
                capture.read_text().splitlines(),
                ["-c", "tui.status_line=[]", "--sandbox", "read-only"],
            )

            subprocess.run([str(cockpit), "--yolo"], check=True, env=env)
            self.assertEqual(
                capture.read_text().splitlines(),
                ["-c", "tui.status_line=[]", "--yolo"],
            )

            for permissions in (("-sread-only",), ("-s=read-only",), ("-anever",), ("-a=never",)):
                with self.subTest(permissions=permissions):
                    subprocess.run([str(cockpit), *permissions], check=True, env=env)
                    captured = capture.read_text().splitlines()
                    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", captured)
                    self.assertEqual(captured[-1], permissions[0])

            subprocess.run([str(cockpit), "--", "-summarize"], check=True, env=env)
            self.assertEqual(
                capture.read_text().splitlines(),
                [
                    "-c",
                    "tui.status_line=[]",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--",
                    "-summarize",
                ],
            )

    def test_cockpit_loads_config_file(self) -> None:
        cockpit = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            codex_home = tmp / ".codex"
            capture = tmp / "capture"
            fake_codex = tmp / "codex"
            fake_tmux = tmp / "tmux"
            codex_home.mkdir()
            (codex_home / "statusline.conf").write_text(
                "CODEX_COCKPIT_HEIGHT=14 \n"
                "CODEX_COCKPIT_INTERVAL=3\n"
                "CODEX_COCKPIT_MANAGE_APPROVALS=0\n"
            )
            fake_codex.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "$CAPTURE"\n')
            fake_tmux.write_text(
                '#!/usr/bin/env bash\n'
                'printf "%s\\n" "$*" >> "$CAPTURE"\n'
                'if [[ "$1" == "split-window" ]]; then printf "%%1\\n"; fi\n'
            )
            fake_codex.chmod(0o755)
            fake_tmux.chmod(0o755)
            env = os.environ.copy()
            for name in (
                "CODEX_COCKPIT_HEIGHT",
                "CODEX_COCKPIT_INTERVAL",
                "CODEX_COCKPIT_MANAGE_APPROVALS",
            ):
                env.pop(name, None)
            env.update(
                {
                    "CAPTURE": str(capture),
                    "CODEX_COCKPIT_CODEX_BIN": str(fake_codex),
                    "CODEX_HOME": str(codex_home),
                    "PATH": f"{tmp}:{env['PATH']}",
                    "TMUX": "test",
                }
            )

            subprocess.run([str(cockpit)], check=True, env=env)

            captured = capture.read_text().splitlines()
            split_window = next(line for line in captured if line.startswith("split-window "))
            self.assertIn("-l 14", split_window)
            self.assertIn("--watch 3", split_window)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", captured)
            self.assertIn("set-option mouse on", captured)
            self.assertIn("set-option -w history-limit 100000", captured)

            subprocess.run(
                [str(cockpit), "resume", "-s", "read-only", "019f0000-0000-7000-8000-000000000000"],
                check=True,
                env=env,
            )
            latest_split = [
                line for line in capture.read_text().splitlines() if line.startswith("split-window ")
            ][-1]
            self.assertNotIn("--bind-after-ms", latest_split)
            self.assertNotIn("--thread-id", latest_split)

    def test_cockpit_rejects_zero_interval_and_short_footer(self) -> None:
        cockpit = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fake_codex = tmp / "codex"
            fake_tmux = tmp / "tmux"
            fake_codex.write_text("#!/usr/bin/env bash\nexit 0\n")
            fake_tmux.write_text("#!/usr/bin/env bash\nexit 0\n")
            fake_codex.chmod(0o755)
            fake_tmux.chmod(0o755)
            base_env = os.environ.copy()
            base_env.update(
                {
                    "CODEX_COCKPIT_CODEX_BIN": str(fake_codex),
                    "PATH": f"{tmp}:{base_env['PATH']}",
                    "TMUX": "test",
                }
            )
            cases = (
                ({"CODEX_COCKPIT_INTERVAL": "0"}, "positive number"),
                ({"CODEX_COCKPIT_HEIGHT": "10"}, "at least 11"),
                ({"CODEX_COCKPIT_HEIGHT": "08"}, "at least 11"),
                ({"CODEX_COCKPIT_HISTORY_LIMIT": "0"}, "positive integer"),
            )

            for overrides, expected in cases:
                with self.subTest(overrides=overrides):
                    env = {**base_env, **overrides}
                    result = subprocess.run(
                        [str(cockpit)],
                        capture_output=True,
                        env=env,
                        text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stderr)

    def test_cockpit_sizes_detached_session_before_split(self) -> None:
        cockpit = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            capture = tmp / "tmux-args"
            project = tmp / "project"
            fake_codex = tmp / "codex"
            fake_tmux = tmp / "tmux"
            project.mkdir()
            fake_codex.write_text("#!/usr/bin/env bash\nexit 0\n")
            fake_tmux.write_text(
                '#!/usr/bin/env bash\n'
                'printf "%s\\n" "$*" >> "$CAPTURE"\n'
                'if [[ "$1" == "new-session" && " $* " == *" -P "* ]]; then\n'
                '    printf "%%0\\n"\n'
                'elif [[ "$1" == "split-window" && " $* " == *" -P "* ]]; then\n'
                '    printf "%%1\\n"\n'
                'elif [[ "$1" == "display-message" && " $* " == *" -t %1 "* ]]; then\n'
                '    printf "%%1\\n"\n'
                'elif [[ "$1" == "has-session" ]]; then\n'
                '    exit "${FAKE_HAS_SESSION:-1}"\n'
                'fi\n'
            )
            fake_codex.chmod(0o755)
            fake_tmux.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "CAPTURE": str(capture),
                    "CODEX_COCKPIT_CODEX_BIN": str(fake_codex),
                    "COLUMNS": "117",
                    "LINES": "83",
                    "PATH": f"{tmp}:{env['PATH']}",
                    "TMPDIR": str(tmp),
                }
            )
            env.pop("TMUX", None)

            subprocess.run([str(cockpit), f"-C={project}"], check=True, env=env)

            new_session = next(
                line for line in capture.read_text().splitlines() if "new-session -d" in line
            )
            split_window = next(
                line
                for line in capture.read_text().splitlines()
                if line.startswith("split-window ") and "--footer" in line
            )
            respawn_pane = next(
                line for line in capture.read_text().splitlines() if line.startswith("respawn-pane ")
            )
            self.assertIn("-x 117 -y 83", new_session)
            self.assertIn("sleep 86400", new_session)
            self.assertIn("runner.sh", respawn_pane)
            self.assertNotIn("--internal-run", new_session)
            self.assertIn("kill-pane -t %0", capture.read_text())
            self.assertNotIn("--bind-after-ms", split_window)
            self.assertIn("--owner-pid-file", split_window)
            self.assertIn(f"--cwd {project.resolve()}", split_window)
            self.assertIn("mouse on", "\n".join(capture.read_text().splitlines()))
            self.assertIn("history-limit 100000", "\n".join(capture.read_text().splitlines()))

    def cockpit_detached_session_env(self, tmp: Path, capture: Path) -> dict[str, str]:
        fake_codex = tmp / "codex"
        fake_tmux = tmp / "tmux"
        fake_codex.write_text("#!/usr/bin/env bash\nexit 0\n")
        fake_tmux.write_text(
            '#!/usr/bin/env bash\n'
            'printf "%s\\n" "$*" >> "$CAPTURE"\n'
            'if [[ "$1" == "new-session" && " $* " == *" -P "* ]]; then\n'
            '    printf "%%0\\n"\n'
            'elif [[ "$1" == "split-window" && " $* " == *" -P "* ]]; then\n'
            '    printf "%%1\\n"\n'
            'elif [[ "$1" == "display-message" && " $* " == *" -t %1 "* ]]; then\n'
            '    printf "%%1\\n"\n'
            'elif [[ "$1" == "has-session" ]]; then\n'
            '    exit "${FAKE_HAS_SESSION:-1}"\n'
            'fi\n'
        )
        fake_codex.chmod(0o755)
        fake_tmux.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CAPTURE": str(capture),
                "CODEX_COCKPIT_CODEX_BIN": str(fake_codex),
                "COLUMNS": "100",
                "LINES": "40",
                "PATH": f"{tmp}:{env['PATH']}",
                "TMPDIR": str(tmp),
            }
        )
        env.pop("TMUX", None)
        return env

    def test_cockpit_detach_leaves_running_session_alive(self) -> None:
        cockpit = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            capture = tmp / "tmux-args"
            env = self.cockpit_detached_session_env(tmp, capture)
            env["FAKE_HAS_SESSION"] = "0"

            result = subprocess.run(
                [str(cockpit)],
                capture_output=True,
                check=True,
                env=env,
                text=True,
            )

            self.assertIn("Reattach with: tmux attach -t '=codex-statusline-", result.stdout)
            self.assertNotIn("kill-session", capture.read_text())
            self.assertEqual(len(list(tmp.glob("codex-statusline.*"))), 1)

    def test_cockpit_cleans_up_when_session_ends_without_status(self) -> None:
        cockpit = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            capture = tmp / "tmux-args"
            env = self.cockpit_detached_session_env(tmp, capture)
            env["FAKE_HAS_SESSION"] = "1"

            result = subprocess.run(
                [str(cockpit)],
                capture_output=True,
                check=True,
                env=env,
                text=True,
            )

            self.assertNotIn("Reattach with", result.stdout)
            self.assertIn("kill-session", capture.read_text())
            self.assertEqual(list(tmp.glob("codex-statusline.*")), [])

    def test_install_codex_respects_custom_codex_home(self) -> None:
        installer = MODULE_PATH.parents[1] / "install-codex.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            home = tmp / "home"
            codex_home = tmp / "codex-home"
            fake_bin = tmp / "bin"
            home.mkdir()
            codex_home.mkdir()
            fake_bin.mkdir()
            for name in ("codex", "tmux"):
                path = fake_bin / name
                path.write_text("#!/usr/bin/env bash\nexit 0\n")
                path.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                }
            )

            subprocess.run([str(installer)], check=True, env=env)

            self.assertTrue((codex_home / "statusline.conf").is_file())
            self.assertEqual(
                (home / ".local/bin/codex-statusline").resolve(),
                MODULE_PATH.with_name("codex-statusline"),
            )


class WatchReliabilityTest(unittest.TestCase):
    def test_next_sleep_uses_interval_while_active(self) -> None:
        now_ms = 1_000_000_000_000
        self.assertEqual(codex_statusline.next_sleep_seconds(3.0, now_ms - 5_000, now_ms), 3.0)
        self.assertEqual(codex_statusline.next_sleep_seconds(0.1, now_ms - 5_000, now_ms), 0.5)
        self.assertEqual(codex_statusline.next_sleep_seconds(3.0, 0, now_ms), 3.0)

    def test_next_sleep_backs_off_when_thread_idle(self) -> None:
        now_ms = 1_000_000_000_000
        idle_ms = now_ms - (codex_statusline.IDLE_AFTER_SECONDS + 1) * 1000
        self.assertEqual(
            codex_statusline.next_sleep_seconds(3.0, idle_ms, now_ms),
            codex_statusline.IDLE_POLL_SECONDS,
        )
        self.assertEqual(
            codex_statusline.next_sleep_seconds(120.0, idle_ms, now_ms), 120.0
        )

    def test_snapshot_activity_ms_scales_seconds(self) -> None:
        # Regression: watch_loop fed threads.updated_at (epoch SECONDS) into
        # next_sleep_seconds, which compares against now_ms — so every session
        # read as idle and the loop slept the 30s floor regardless of interval.
        now_ms = 1_000_000_000_000
        now_s = now_ms // 1000
        recent_s = now_s - 5
        single = codex_statusline.snapshot_activity_ms({"updated_at": recent_s}, False)
        self.assertEqual(single, recent_s * 1000)
        multi = codex_statusline.snapshot_activity_ms(
            {"sessions": [{"updated_at": recent_s - 30}, {"updated_at": recent_s}]}, True
        )
        self.assertEqual(multi, recent_s * 1000)
        # Scaled value keeps the fast interval; the pre-fix seconds value tripped
        # the idle branch and forced the 30s floor.
        self.assertEqual(codex_statusline.next_sleep_seconds(2.0, single, now_ms), 2.0)
        self.assertEqual(
            codex_statusline.next_sleep_seconds(2.0, recent_s, now_ms),
            codex_statusline.IDLE_POLL_SECONDS,
        )

    def test_floor_multi_session_sleep(self) -> None:
        # Footer (single session) keeps the live interval; --top/--all is floored
        # to the idle cadence so the live refresh can't thrash the re-reading cache.
        self.assertEqual(codex_statusline.floor_multi_session_sleep(2.0, False), 2.0)
        self.assertEqual(
            codex_statusline.floor_multi_session_sleep(2.0, True),
            codex_statusline.IDLE_POLL_SECONDS,
        )
        self.assertEqual(codex_statusline.floor_multi_session_sleep(120.0, True), 120.0)

    def test_limit_display_is_reset_aware(self) -> None:
        now = datetime.fromtimestamp(1_770_000_000).astimezone()
        now_ts = int(now.timestamp())
        past, future = now_ts - 3600, now_ts + 7200
        pct, text = codex_statusline.limit_display(
            {"used_percent": 100.0, "resets_at": past}, now
        )
        self.assertEqual(pct, 0.0)
        self.assertEqual(text, "reset")
        pct, text = codex_statusline.limit_display(
            {"used_percent": 63.0, "resets_at": future}, now
        )
        self.assertEqual(pct, 63.0)
        self.assertTrue(text.startswith("resets"))
        pct, text = codex_statusline.limit_display({"used_percent": 42.0}, now)
        self.assertEqual(pct, 42.0)
        self.assertEqual(text, "reset n/a")

    def test_resolve_state_db_picks_highest_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            self.assertEqual(
                codex_statusline.resolve_state_db(home), home / "state_5.sqlite"
            )
            (home / "state_5.sqlite").touch()
            (home / "state_6.sqlite").touch()
            (home / "state_x.sqlite").touch()
            self.assertEqual(
                codex_statusline.resolve_state_db(home), home / "state_6.sqlite"
            )

    def test_owner_alive_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "owner.pid"
            self.assertTrue(codex_statusline.owner_alive(str(pid_file)))
            pid_file.write_text("not-a-pid")
            self.assertTrue(codex_statusline.owner_alive(str(pid_file)))
            pid_file.write_text(str(os.getpid()))
            self.assertTrue(codex_statusline.owner_alive(str(pid_file)))
            proc = subprocess.Popen(["sleep", "0.01"])
            proc.wait()
            pid_file.write_text(str(proc.pid))
            self.assertFalse(codex_statusline.owner_alive(str(pid_file)))

    def test_maybe_checkpoint_wal_truncates_oversized_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "state.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA journal_mode=WAL")
            # The writer stays open, matching production (codex holds its
            # connection); closing it would checkpoint and delete the WAL.
            conn.execute("CREATE TABLE t (v BLOB)")
            for _ in range(50):
                conn.execute("INSERT INTO t VALUES (?)", (b"x" * 65536,))
            conn.commit()
            wal = Path(f"{db}-wal")
            self.assertGreater(wal.stat().st_size, 0)
            result = codex_statusline.maybe_checkpoint_wal(
                db, last_attempt=0.0, now=1000.0, threshold_bytes=1
            )
            self.assertEqual(result, 1000.0)
            self.assertEqual(wal.stat().st_size, 0)
            conn.close()

    def test_maybe_checkpoint_wal_respects_threshold_and_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "state.sqlite"
            self.assertEqual(
                codex_statusline.maybe_checkpoint_wal(db, last_attempt=0.0, now=10.0),
                0.0,
            )
            wal = Path(f"{db}-wal")
            wal.write_bytes(b"x" * 10)
            self.assertEqual(
                codex_statusline.maybe_checkpoint_wal(
                    db, last_attempt=990.0, now=1000.0, threshold_bytes=1
                ),
                990.0,
            )


if __name__ == "__main__":
    unittest.main()
