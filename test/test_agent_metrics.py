from __future__ import annotations

import importlib.util
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Iterator
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "agent_metrics.py"
SPEC = importlib.util.spec_from_file_location("agent_metrics", MODULE_PATH)
assert SPEC is not None
agent_metrics = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = agent_metrics
SPEC.loader.exec_module(agent_metrics)


def line(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":")) + "\n"


class AgentMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "runtime"
        self.claude_projects = self.root / "claude/projects"
        self.codex_sessions = self.root / "codex/sessions"
        self.claude_projects.mkdir(parents=True)
        self.codex_sessions.mkdir(parents=True)
        self.account_spans = self.root / "claude/session-accounts.json"
        self.auth = self.root / "codex/auth.json"
        self.statusline_config = self.root / "claude/statusline.conf"
        self.config = agent_metrics.Config(
            data_dir=self.data_dir,
            database=self.data_dir / "metrics.sqlite3",
            claude_projects=self.claude_projects,
            claude_account_spans=self.account_spans,
            codex_sessions=self.codex_sessions,
            codex_auth=self.auth,
            account_aliases={
                "claude:developer@example.invalid|org-one": "Work",
                "claude:developer@example.invalid|org-two": "Research",
                "codex:acct-current-123": "Codex",
            },
            pricing={"example-model": {"input_per_million": 99}},
            retention_days=3650,
            bind="127.0.0.1",
            port=8765,
            claude_statusline_config=self.statusline_config,
            reuse_statusline_account_labels=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.config.database)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def identifier(self, value: str, namespace: str) -> str:
        return agent_metrics.keyed_hash(
            (self.data_dir / "identity.salt").read_bytes(), namespace, value
        )

    def write_claude_fixture(self) -> Path:
        session = "claude-session"
        self.account_spans.write_text(
            json.dumps(
                {
                    session: {
                        "spans": [
                            {
                                "from": "2026-08-25T17:00:00Z",
                                "to": "2026-08-25T17:01:00Z",
                                "email": "developer@example.invalid",
                                "org_uuid": "org-one",
                            },
                            {
                                "from": "2026-08-25T17:01:00Z",
                                "to": None,
                                "email": "developer@example.invalid",
                                "org_uuid": "org-two",
                            },
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        transcript = self.claude_projects / f"{session}.jsonl"
        records = [
            {
                "type": "user",
                "timestamp": "2026-08-25T17:00:05Z",
                "sessionId": session,
                "uuid": "turn-1",
                "message": {"content": "PROMPT-SECRET-NEVER-PERSIST"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-25T17:00:10Z",
                "sessionId": session,
                "requestId": "request-one",
                "message": {
                    "id": "message-one",
                    "model": "claude-test",
                    "usage": {
                        "input_tokens": 100,
                        "cache_read_input_tokens": 200,
                        "cache_creation_input_tokens": 50,
                        "output_tokens": 25,
                    },
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-one",
                            "name": "Read",
                            "input": {"path": "/Users/person/SOURCE-SECRET"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-08-25T17:00:12Z",
                "sessionId": session,
                "uuid": "turn-2",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-one",
                            "content": "TOOL-OUTPUT-SECRET",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-25T17:01:00Z",
                "sessionId": session,
                "requestId": "request-two",
                "reasoningEffort": "high",
                "message": {
                    "id": "message-two",
                    "model": "claude-test",
                    "usage": {"input_tokens": 40, "output_tokens": 10},
                },
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": "2026-08-25T17:01:20Z",
                "sessionId": session,
            },
        ]
        transcript.write_text("".join(line(record) for record in records), encoding="utf-8")
        return transcript

    def write_codex_fixture(self) -> Path:
        self.auth.write_text(
            json.dumps(
                {
                    "tokens": {
                        "account_id": "acct-current-123",
                        "access_token": "ACCESS-TOKEN-SECRET",
                        "refresh_token": "REFRESH-TOKEN-SECRET",
                        "id_token": "IDENTITY-TOKEN-SECRET",
                    }
                }
            ),
            encoding="utf-8",
        )
        transcript = self.codex_sessions / "2026/08/25/rollout-test.jsonl"
        transcript.parent.mkdir(parents=True)
        records = [
            {
                "timestamp": "2026-08-25T17:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "codex-child",
                    "parent_thread_id": "codex-parent",
                    "cwd": "/Users/person/PRIVATE-SOURCE-PATH",
                    "source": {
                        "subagent": {"thread_spawn": {"parent_thread_id": "wrong-parent"}}
                    },
                },
            },
            {
                "timestamp": "2026-08-25T17:00:01Z",
                "type": "turn_context",
                "payload": {"model": "gpt-test", "effort": "xhigh"},
            },
            {
                "timestamp": "2026-08-25T17:00:02Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "codex-turn"},
            },
            {
                "timestamp": "2026-08-25T17:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "codex-call",
                    "name": "exec_command",
                    "arguments": "COMMAND-ARGUMENT-SECRET",
                },
            },
            {
                "timestamp": "2026-08-25T17:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "codex-call",
                    "output": "CODEX-OUTPUT-SECRET",
                },
            },
            {
                "timestamp": "2026-08-25T17:00:05Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "model_context_window": 200000,
                        "last_token_usage": {
                            "input_tokens": 60,
                            "cached_input_tokens": 20,
                            "output_tokens": 15,
                            "reasoning_output_tokens": 5,
                            "total_tokens": 95,
                        },
                    },
                    "rate_limits": {
                        "primary": {
                            "used_percent": 12.5,
                            "window_minutes": 300,
                            "resets_at": 1787688000,
                        }
                    },
                },
            },
            {
                "timestamp": "2026-08-25T17:00:06Z",
                "type": "compacted",
                "payload": {"replacement_history": "COMPACTION-TEXT-SECRET"},
            },
            {
                "timestamp": "2026-08-25T17:00:08Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "codex-turn"},
            },
        ]
        transcript.write_text("".join(line(record) for record in records), encoding="utf-8")
        return transcript

    def test_both_parsers_capture_allowed_metrics(self) -> None:
        self.write_claude_fixture()
        self.write_codex_fixture()
        result = agent_metrics.sync(self.config)
        self.assertEqual(result["files"], 2)
        request_id = self.identifier("request-one", "request")
        with self.connection() as connection:
            claude = connection.execute(
                "select * from events where provider='claude' and event_kind='tokens' "
                "and request_id=?",
                (request_id,),
            ).fetchone()
            self.assertEqual(claude["input_tokens"], 100)
            self.assertEqual(claude["cached_input_tokens"], 200)
            self.assertEqual(claude["cache_create_tokens"], 50)
            self.assertEqual(claude["output_tokens"], 25)
            self.assertEqual(claude["turn_latency_ms"], 5000)

            codex = connection.execute(
                "select * from events where provider='codex' and event_kind='tokens'"
            ).fetchone()
            self.assertEqual(codex["model"], "gpt-test")
            self.assertEqual(codex["reasoning_effort"], "xhigh")
            self.assertEqual(codex["reasoning_tokens"], 5)
            self.assertEqual(codex["context_window"], 200000)
            self.assertEqual(codex["context_used"], 80)
            self.assertEqual(
                codex["parent_session_id"], self.identifier("codex-parent", "session")
            )

            tool = connection.execute(
                "select * from events where provider='codex' and event_kind='tool' "
                "and tool_status='ok'"
            ).fetchone()
            self.assertEqual(tool["tool_name"], "exec_command")
            self.assertEqual(tool["tool_duration_ms"], 1000)
            quota = connection.execute("select * from events where event_kind='quota'").fetchone()
            self.assertEqual(quota["quota_window_minutes"], 300)
            self.assertEqual(quota["quota_used_percent"], 12.5)
            self.assertEqual(
                connection.execute("select count(*) from events where event_kind='compaction'").fetchone()[0],
                2,
            )

    def test_sync_is_incremental_and_idempotent(self) -> None:
        transcript = self.write_claude_fixture()
        first = agent_metrics.sync(self.config)
        with self.connection() as connection:
            before = connection.execute("select count(*) from events").fetchone()[0]
        second = agent_metrics.sync(self.config)
        with self.connection() as connection:
            after = connection.execute("select count(*) from events").fetchone()[0]
        self.assertGreater(first["events"], 0)
        self.assertEqual(second["lines"], 0)
        self.assertEqual(second["events"], 0)
        self.assertEqual(after, before)

        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                line(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-25T17:02:00Z",
                        "sessionId": "claude-session",
                        "requestId": "request-three",
                        "message": {
                            "id": "message-three",
                            "model": "claude-test",
                            "usage": {"input_tokens": 1, "output_tokens": 2},
                        },
                    }
                )
            )
        third = agent_metrics.sync(self.config)
        self.assertEqual(third["lines"], 1)
        self.assertEqual(third["events"], 1)

    def test_bounded_sync_resumes_exactly_without_duplicates(self) -> None:
        transcript = self.claude_projects / "bounded.jsonl"
        records = [
            {
                "type": "assistant",
                "timestamp": f"2026-08-25T17:00:0{index}Z",
                "sessionId": "bounded-session",
                "requestId": f"bounded-request-{index}",
                "message": {
                    "model": "claude-test",
                    "usage": {"input_tokens": index, "output_tokens": 1},
                },
            }
            for index in range(1, 8)
        ]
        encoded = [line(record) for record in records]
        transcript.write_text("".join(encoded), encoding="utf-8")

        first = agent_metrics.sync(self.config, max_lines=3)
        with self.connection() as connection:
            first_offset = connection.execute("select offset from sources").fetchone()[0]
        second = agent_metrics.sync(self.config, max_lines=3)
        third = agent_metrics.sync(self.config, max_lines=3)
        fourth = agent_metrics.sync(self.config, max_lines=3)

        self.assertEqual(first["lines"], 3)
        self.assertEqual(first_offset, len("".join(encoded[:3]).encode()))
        self.assertEqual(second["lines"], 3)
        self.assertEqual(third["lines"], 1)
        self.assertEqual(third["pending_files"], 0)
        self.assertEqual(third["pending_bytes"], 0)
        self.assertEqual(fourth["lines"], 0)
        self.assertEqual(fourth["updated_files"], 0)
        with self.connection() as connection:
            requests = [
                row[0]
                for row in connection.execute(
                    "select request_id from events where event_kind='tokens' order by timestamp"
                )
            ]
            indexes = {
                row[1] for row in connection.execute("pragma index_list('events')")
            }
        self.assertEqual(
            requests,
            [self.identifier(f"bounded-request-{index}", "request") for index in range(1, 8)],
        )
        self.assertIn("events_source", indexes)

    def test_bounded_sync_balances_providers_during_claude_backfill(self) -> None:
        claude = self.claude_projects / "large-claude.jsonl"
        claude.write_text(
            "".join(
                line(
                    {
                        "type": "assistant",
                        "timestamp": f"2026-08-25T17:00:{index:02d}Z",
                        "sessionId": "large-claude",
                        "requestId": f"claude-{index}",
                        "message": {
                            "model": "claude-test",
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    }
                )
                for index in range(20)
            ),
            encoding="utf-8",
        )
        codex = self.codex_sessions / "current-codex.jsonl"
        codex.write_text(
            line(
                {
                    "timestamp": "2026-08-25T17:01:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"input_tokens": 2, "output_tokens": 1}
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        result = agent_metrics.sync(self.config, max_lines=5)

        self.assertEqual(result["lines"], 5)
        with self.connection() as connection:
            providers = {
                row[0]
                for row in connection.execute(
                    "select distinct provider from events where event_kind='tokens'"
                )
            }
        self.assertEqual(providers, {"claude", "codex"})

    def test_live_append_is_processed_before_older_backfill(self) -> None:
        live = self.claude_projects / "live.jsonl"
        live.write_text(
            line(
                {
                    "type": "assistant",
                    "timestamp": "2026-08-25T18:00:00Z",
                    "sessionId": "live-session",
                    "requestId": "live-initial",
                    "message": {
                        "model": "claude-test",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                }
            ),
            encoding="utf-8",
        )
        agent_metrics.sync(self.config)
        backlog = self.claude_projects / "old-backlog.jsonl"
        backlog.write_text(
            "".join(
                line(
                    {
                        "type": "assistant",
                        "timestamp": f"2026-08-24T18:00:{index:02d}Z",
                        "sessionId": "backlog-session",
                        "requestId": f"backlog-{index}",
                        "message": {
                            "model": "claude-test",
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    }
                )
                for index in range(10)
            ),
            encoding="utf-8",
        )
        with live.open("a", encoding="utf-8") as handle:
            handle.write(
                "".join(
                    line(
                        {
                            "type": "assistant",
                            "timestamp": f"2026-08-25T18:00:0{index + 1}Z",
                            "sessionId": "live-session",
                            "requestId": f"live-appended-{index}",
                            "message": {
                                "model": "claude-test",
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                        }
                    )
                    for index in range(3)
                )
            )

        result = agent_metrics.sync(self.config, max_lines=2)
        resumed = agent_metrics.sync(self.config, max_lines=2)

        self.assertEqual(result["live_lines"], 1)
        self.assertEqual(result["backfill_lines"], 1)
        self.assertEqual(resumed["live_lines"], 1)
        self.assertEqual(resumed["backfill_lines"], 1)
        with self.connection() as connection:
            live_event = connection.execute(
                "select count(*) from events where request_id in (?, ?)",
                (
                    self.identifier("live-appended-0", "request"),
                    self.identifier("live-appended-1", "request"),
                ),
            ).fetchone()[0]
            backlog_events = connection.execute(
                "select count(*) from events where request_id in (?, ?)",
                (
                    self.identifier("backlog-0", "request"),
                    self.identifier("backlog-1", "request"),
                ),
            ).fetchone()[0]
        self.assertEqual(live_event, 2)
        self.assertEqual(backlog_events, 2)

    def test_unchanged_sync_skips_all_parser_and_aggregation_writes(self) -> None:
        self.write_claude_fixture()
        self.write_codex_fixture()
        first = agent_metrics.sync(self.config)
        self.assertEqual(first["updated_files"], 2)
        with self.connection() as connection:
            minutes_before = connection.execute(
                "select * from minute_metrics order by source_id, minute"
            ).fetchall()
            last_sync_before = connection.execute(
                "select value from metadata where key='last_sync_at'"
            ).fetchone()[0]

        with (
            mock.patch.object(
                agent_metrics, "hydrate_state", side_effect=AssertionError("hydrated unchanged source")
            ),
            mock.patch.object(
                agent_metrics, "persist_state", side_effect=AssertionError("persisted unchanged source")
            ),
            mock.patch.object(
                agent_metrics,
                "rebuild_source_minutes",
                side_effect=AssertionError("rebuilt unchanged source"),
            ),
            mock.patch.object(
                agent_metrics,
                "reattribute_claude_events",
                side_effect=AssertionError("reattributed unchanged spans"),
            ),
            mock.patch.object(
                agent_metrics, "rebuild_minutes", side_effect=AssertionError("globally rebuilt minutes")
            ),
        ):
            second = agent_metrics.sync(self.config)

        self.assertEqual(second["files"], 2)
        self.assertEqual(second["updated_files"], 0)
        self.assertEqual(second["lines"], 0)
        self.assertEqual(second["events"], 0)
        self.assertEqual(second["reattributed"], 0)
        with self.connection() as connection:
            minutes_after = connection.execute(
                "select * from minute_metrics order by source_id, minute"
            ).fetchall()
            last_sync_after = connection.execute(
                "select value from metadata where key='last_sync_at'"
            ).fetchone()[0]
        self.assertEqual([tuple(row) for row in minutes_after], [tuple(row) for row in minutes_before])
        self.assertEqual(last_sync_after, last_sync_before)

    def test_source_file_commits_are_interrupt_resilient_and_readable(self) -> None:
        self.write_claude_fixture()
        second = self.claude_projects / "z-second.jsonl"
        second.write_text(
            line(
                {
                    "type": "assistant",
                    "timestamp": "2026-08-25T17:03:00Z",
                    "sessionId": "second-session",
                    "requestId": "second-request",
                    "message": {
                        "id": "second-message",
                        "model": "claude-test",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                }
            ),
            encoding="utf-8",
        )
        original = agent_metrics.scan_file
        calls = 0

        def interrupt_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return original(*args, **kwargs)

        with mock.patch.object(agent_metrics, "scan_file", side_effect=interrupt_second):
            with self.assertRaises(KeyboardInterrupt):
                agent_metrics.sync(self.config)
        connection = agent_metrics.connect_database_readonly(self.config.database)
        try:
            self.assertGreater(connection.execute("select count(*) from events").fetchone()[0], 0)
            self.assertGreater(
                connection.execute("select count(*) from minute_metrics").fetchone()[0], 0
            )
            self.assertEqual(connection.execute("select count(*) from sources").fetchone()[0], 1)
        finally:
            connection.close()

    def test_status_uses_readonly_connection_during_writer_transaction(self) -> None:
        self.write_claude_fixture()
        agent_metrics.sync(self.config)
        writer = agent_metrics.connect_database(self.config.database)
        writer.execute("begin immediate")
        try:
            with mock.patch.object(
                agent_metrics, "create_schema", side_effect=AssertionError("schema write")
            ):
                with redirect_stdout(io.StringIO()) as output:
                    agent_metrics.status(self.config)
            self.assertIn("events:", output.getvalue())
        finally:
            writer.rollback()
            writer.close()

    def test_claude_split_sync_preserves_turn_request_latency_and_tool_start(self) -> None:
        self.account_spans.write_text("{}", encoding="utf-8")
        transcript = self.claude_projects / "split-claude.jsonl"
        transcript.write_text(
            line(
                {
                    "type": "user",
                    "timestamp": "2026-08-25T18:00:00Z",
                    "sessionId": "split-claude",
                    "uuid": "split-turn",
                    "message": {"content": "SECRET-SPLIT-PROMPT"},
                }
            )
            + line(
                {
                    "type": "assistant",
                    "timestamp": "2026-08-25T18:00:01Z",
                    "sessionId": "split-claude",
                    "message": {
                        "model": "claude-test",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "split-tool",
                                "name": "Read",
                                "input": {"path": "/SECRET/SPLIT/PATH"},
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        agent_metrics.sync(self.config)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                line(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-25T18:00:05Z",
                        "sessionId": "split-claude",
                        "message": {
                            "model": "claude-test",
                            "usage": {"input_tokens": 4, "output_tokens": 2},
                        },
                    }
                )
                + line(
                    {
                        "type": "user",
                        "timestamp": "2026-08-25T18:00:07Z",
                        "sessionId": "split-claude",
                        "uuid": "tool-result-event",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "split-tool",
                                    "content": "SECRET-SPLIT-OUTPUT",
                                }
                            ]
                        },
                    }
                )
            )
        agent_metrics.sync(self.config)
        with self.connection() as connection:
            tokens = connection.execute(
                "select turn_id, request_id, turn_latency_ms from events "
                "where event_kind='tokens'"
            ).fetchone()
            tool = connection.execute(
                "select tool_duration_ms from events where event_kind='tool' and tool_status='ok'"
            ).fetchone()
        self.assertEqual(tokens["turn_id"], self.identifier("split-turn", "turn"))
        self.assertEqual(tokens["request_id"], self.identifier("split-turn", "turn"))
        self.assertEqual(tokens["turn_latency_ms"], 5000)
        self.assertEqual(tool["tool_duration_ms"], 6000)

    def test_codex_split_sync_preserves_turn_request_and_latencies(self) -> None:
        self.auth.write_text('{"tokens":{"account_id":"acct-current-123"}}', encoding="utf-8")
        transcript = self.codex_sessions / "split-codex.jsonl"
        transcript.write_text(
            line(
                {
                    "timestamp": "2026-08-25T19:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "split-codex", "parent_thread_id": "semantic-parent"},
                }
            )
            + line(
                {
                    "timestamp": "2026-08-25T19:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "codex-split-turn"},
                }
            )
            + line(
                {
                    "timestamp": "2026-08-25T19:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "codex-split-call",
                        "name": "exec_command",
                        "arguments": "SECRET-CODEX-ARG",
                    },
                }
            ),
            encoding="utf-8",
        )
        agent_metrics.sync(self.config)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                line(
                    {
                        "timestamp": "2026-08-25T19:00:05Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {"input_tokens": 2, "output_tokens": 1}
                            },
                        },
                    }
                )
                + line(
                    {
                        "timestamp": "2026-08-25T19:00:06Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "codex-split-call",
                            "output": "SECRET-CODEX-OUTPUT",
                        },
                    }
                )
                + line(
                    {
                        "timestamp": "2026-08-25T19:00:07Z",
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": "codex-split-turn"},
                    }
                )
            )
        agent_metrics.sync(self.config)
        with self.connection() as connection:
            tokens = connection.execute(
                "select turn_id, request_id from events where event_kind='tokens'"
            ).fetchone()
            turn = connection.execute(
                "select turn_latency_ms from events where event_kind='turn_end'"
            ).fetchone()
            tool = connection.execute(
                "select tool_duration_ms from events where event_kind='tool' and tool_status='ok'"
            ).fetchone()
        self.assertEqual(tokens["turn_id"], self.identifier("codex-split-turn", "turn"))
        self.assertEqual(
            tokens["request_id"], self.identifier("codex-split-turn", "turn")
        )
        self.assertEqual(turn["turn_latency_ms"], 6000)
        self.assertEqual(tool["tool_duration_ms"], 4000)

    def test_minute_aggregation_stacks_token_dimensions(self) -> None:
        self.write_claude_fixture()
        self.write_codex_fixture()
        agent_metrics.sync(self.config)
        with self.connection() as connection:
            row = connection.execute(
                "select sum(input_tokens) input_tokens, sum(cached_input_tokens) cached, "
                "sum(cache_create_tokens) created, sum(output_tokens) output, "
                "sum(reasoning_tokens) reasoning, sum(total_tokens) total "
                "from minute_metrics where minute=?",
                (agent_metrics.timestamp_ms("2026-08-25T17:00:00Z"),),
            ).fetchone()
        self.assertEqual(row["input_tokens"], 160)
        self.assertEqual(row["cached"], 220)
        self.assertEqual(row["created"], 50)
        self.assertEqual(row["output"], 40)
        self.assertEqual(row["reasoning"], 5)
        self.assertEqual(row["total"], 470)

    def test_account_handoff_boundary_is_half_open_and_org_specific(self) -> None:
        self.write_claude_fixture()
        agent_metrics.sync(self.config)
        salt = (self.data_dir / "identity.salt").read_bytes()
        first_id = agent_metrics.hash_account(
            salt, "claude", "developer@example.invalid", "org-one"
        )
        second_id = agent_metrics.hash_account(
            salt, "claude", "developer@example.invalid", "org-two"
        )
        self.assertNotEqual(first_id, second_id)
        with self.connection() as connection:
            rows = connection.execute(
                "select request_id, account_id from events where event_kind='tokens' order by timestamp"
            ).fetchall()
            labels = {
                row["account_id"]: row["label"]
                for row in connection.execute("select account_id, label from accounts")
            }
        self.assertEqual(rows[0]["account_id"], first_id)
        self.assertEqual(rows[1]["account_id"], second_id)
        self.assertEqual(labels[first_id], "Work")
        self.assertEqual(labels[second_id], "Research")

    def test_legacy_org_id_span_is_org_specific_and_labeled(self) -> None:
        self.write_claude_fixture()
        payload = json.loads(self.account_spans.read_text(encoding="utf-8"))
        for span in payload["claude-session"]["spans"]:
            span["orgId"] = span.pop("org_uuid")
        self.account_spans.write_text(json.dumps(payload), encoding="utf-8")

        agent_metrics.sync(self.config)

        with self.connection() as connection:
            labels = {
                row["label"] for row in connection.execute(
                    "select distinct accounts.label from events "
                    "join accounts using(account_id) where events.provider='claude'"
                )
            }
        self.assertEqual(labels, {"Work", "Research"})

    def test_late_claude_spans_reattribute_existing_events(self) -> None:
        self.write_claude_fixture()
        spans = self.account_spans.read_text(encoding="utf-8")
        self.account_spans.unlink()
        agent_metrics.sync(self.config)
        with self.connection() as connection:
            self.assertEqual(
                {row[0] for row in connection.execute("select account_id from events")}, {""}
            )
        self.account_spans.write_text(spans, encoding="utf-8")
        with (
            mock.patch.object(
                agent_metrics,
                "reattribute_claude_events",
                wraps=agent_metrics.reattribute_claude_events,
            ) as reattribute,
            mock.patch.object(
                agent_metrics, "rebuild_minutes", wraps=agent_metrics.rebuild_minutes
            ) as rebuild,
        ):
            result = agent_metrics.sync(self.config)
        reattribute.assert_called_once()
        rebuild.assert_called_once()
        self.assertGreater(result["reattributed"], 0)
        salt = (self.data_dir / "identity.salt").read_bytes()
        expected = [
            agent_metrics.hash_account(salt, "claude", "developer@example.invalid", "org-one"),
            agent_metrics.hash_account(salt, "claude", "developer@example.invalid", "org-two"),
        ]
        with self.connection() as connection:
            rows = connection.execute(
                "select account_id from events where event_kind='tokens' order by timestamp"
            ).fetchall()
            minute_accounts = {
                row[0] for row in connection.execute("select distinct account_id from minute_metrics")
            }
        self.assertEqual([row[0] for row in rows], expected)
        self.assertEqual(minute_accounts, set(expected))

    def test_attribution_version_change_rewrites_existing_accounts(self) -> None:
        self.write_claude_fixture()
        agent_metrics.sync(self.config)
        legacy_fingerprint = hashlib.sha256(self.account_spans.read_bytes()).hexdigest()
        with self.connection() as connection:
            connection.execute("update events set account_id='legacy-account'")
            connection.execute("update minute_metrics set account_id='legacy-account'")
            connection.execute(
                "update metadata set value=? where key='claude_account_spans_fingerprint'",
                (legacy_fingerprint,),
            )
            connection.commit()

        result = agent_metrics.sync(self.config)

        self.assertGreater(result["reattributed"], 0)
        with self.connection() as connection:
            event_accounts = {
                row[0] for row in connection.execute(
                    "select distinct account_id from events where provider='claude'"
                )
            }
            minute_accounts = {
                row[0] for row in connection.execute(
                    "select distinct account_id from minute_metrics where provider='claude'"
                )
            }
        self.assertNotIn("legacy-account", event_accounts)
        self.assertEqual(minute_accounts, event_accounts)

    def test_real_claude_fields_and_semantic_identifiers(self) -> None:
        self.account_spans.write_text("{}", encoding="utf-8")
        directory = self.claude_projects / "private-parent-directory" / "subagents"
        directory.mkdir(parents=True)
        transcript = directory / "agent-person-name.jsonl"
        transcript.write_text(
            line(
                {
                    "type": "assistant",
                    "timestamp": "2026-08-25T20:00:00Z",
                    "sessionId": "semantic-session",
                    "agentId": "semantic-agent",
                    "parentUuid": "event-uuid-not-a-session",
                    "effort": "ultracode",
                    "message": {
                        "model": "claude-test",
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    },
                }
            ),
            encoding="utf-8",
        )
        fallback = self.claude_projects / "path-derived-session-name.jsonl"
        fallback.write_text(
            line(
                {
                    "type": "user",
                    "timestamp": "2026-08-25T20:01:00Z",
                    "uuid": "fallback-turn",
                    "message": {"content": "SECRET-FALLBACK-PROMPT"},
                }
            ),
            encoding="utf-8",
        )
        agent_metrics.sync(self.config)
        with self.connection() as connection:
            row = connection.execute(
                "select session_id, parent_session_id, agent_id, reasoning_effort "
                "from events where event_kind='tokens'"
            ).fetchone()
            fallback_id = connection.execute(
                "select session_id from events where turn_id=?",
                (self.identifier("fallback-turn", "turn"),),
            ).fetchone()[0]
        self.assertEqual(row["session_id"], self.identifier("semantic-session", "session"))
        self.assertEqual(
            row["parent_session_id"], self.identifier("semantic-session", "session")
        )
        self.assertEqual(row["agent_id"], self.identifier("semantic-agent", "agent"))
        self.assertEqual(row["reasoning_effort"], "ultracode")
        self.assertNotEqual(fallback_id, "path-derived-session-name")
        persisted = self.config.database.read_bytes()
        self.assertNotIn(b"private-parent-directory", persisted)
        self.assertNotIn(b"path-derived-session-name", persisted)

    def test_sensitive_source_material_is_never_persisted(self) -> None:
        self.write_claude_fixture()
        self.write_codex_fixture()
        agent_metrics.sync(self.config)
        database_bytes = self.config.database.read_bytes()
        forbidden = (
            b"PROMPT-SECRET-NEVER-PERSIST",
            b"SOURCE-SECRET",
            b"TOOL-OUTPUT-SECRET",
            b"ACCESS-TOKEN-SECRET",
            b"REFRESH-TOKEN-SECRET",
            b"IDENTITY-TOKEN-SECRET",
            b"COMMAND-ARGUMENT-SECRET",
            b"CODEX-OUTPUT-SECRET",
            b"COMPACTION-TEXT-SECRET",
            b"developer@example.invalid",
            b"PRIVATE-SOURCE-PATH",
            b"claude-session",
            b"request-one",
            b"call-one",
        )
        for value in forbidden:
            self.assertNotIn(value, database_bytes)
        with self.connection() as connection:
            columns = {
                row[1] for row in connection.execute("pragma table_info(events)")
            }
        self.assertFalse(
            {"prompt", "text", "arguments", "output", "source_path", "email", "name"} & columns
        )

    def test_codex_reads_only_explicit_current_account_id(self) -> None:
        self.write_codex_fixture()
        agent_metrics.sync(self.config)
        salt = (self.data_dir / "identity.salt").read_bytes()
        expected = agent_metrics.hash_account(salt, "codex", "acct-current-123")
        with self.connection() as connection:
            values = {
                row[0]
                for row in connection.execute(
                    "select distinct account_id from events where provider='codex'"
                )
            }
        self.assertEqual(values, {expected})
        self.assertNotEqual(expected, "acct-current-123")

    def test_bounded_codex_backfill_keeps_first_account(self) -> None:
        self.write_codex_fixture()
        agent_metrics.sync(self.config, max_lines=2)
        salt = (self.data_dir / "identity.salt").read_bytes()
        expected = agent_metrics.hash_account(salt, "codex", "acct-current-123")
        self.auth.write_text(json.dumps({"tokens": {"account_id": "changed-account"}}))
        for _ in range(5):
            agent_metrics.sync(self.config, max_lines=2)
        with self.connection() as connection:
            values = {
                row[0]
                for row in connection.execute(
                    "select distinct account_id from events where provider='codex'"
                )
            }
        self.assertEqual(values, {expected})

    def test_cost_is_not_inferred_from_configured_pricing(self) -> None:
        self.write_claude_fixture()
        agent_metrics.sync(self.config)
        with self.connection() as connection:
            costs = [row[0] for row in connection.execute("select cost_usd from events")]
        self.assertTrue(all(cost is None for cost in costs))

    def test_reasoning_is_detail_not_added_to_total_when_explicit_total_is_absent(self) -> None:
        usage = agent_metrics.usage_fields(
            {
                "input_tokens": 10,
                "cached_input_tokens": 20,
                "output_tokens": 7,
                "reasoning_output_tokens": 3,
            }
        )
        self.assertEqual(usage["reasoning_tokens"], 3)
        self.assertEqual(usage["total_tokens"], 37)

    def test_streamed_request_rows_are_raw_but_deduplicated_in_minutes(self) -> None:
        transcript = self.write_claude_fixture()
        records = transcript.read_text(encoding="utf-8").splitlines()
        duplicate = {
            "type": "assistant",
            "timestamp": "2026-08-25T17:00:09Z",
            "sessionId": "claude-session",
            "requestId": "request-one",
            "message": {
                "id": "message-one",
                "model": "claude-test",
                "usage": {"input_tokens": 100, "output_tokens": 5},
            },
        }
        transcript.write_text(
            "\n".join([records[0], json.dumps(duplicate), *records[1:]]) + "\n",
            encoding="utf-8",
        )
        agent_metrics.sync(self.config)
        with self.connection() as connection:
            raw = connection.execute(
                "select count(*) from events where event_kind='tokens' and request_id=?",
                (self.identifier("request-one", "request"),),
            ).fetchone()[0]
            derived = connection.execute(
                "select sum(total_tokens) from minute_metrics where minute=?",
                (agent_metrics.timestamp_ms("2026-08-25T17:00:00Z"),),
            ).fetchone()[0]
        self.assertEqual(raw, 2)
        self.assertEqual(derived, 375)

    def test_duplicate_request_across_sources_is_globally_deduplicated(self) -> None:
        record = {
            "type": "assistant",
            "timestamp": "2026-08-25T17:00:09Z",
            "sessionId": "copied-session",
            "requestId": "copied-request",
            "message": {
                "id": "copied-message",
                "model": "claude-test",
                "usage": {"input_tokens": 90, "output_tokens": 10},
            },
        }
        for name in ("original.jsonl", "copy.jsonl"):
            (self.claude_projects / name).write_text(line(record), encoding="utf-8")
        agent_metrics.sync(self.config)
        with self.connection() as connection:
            derived = connection.execute(
                "select sum(total_tokens) from minute_metrics"
            ).fetchone()[0]
            payload = agent_metrics.dashboard_payload(connection, {})
        self.assertEqual(derived, 100)
        self.assertEqual(sum(row["total_tokens"] for row in payload["sessions"]), 100)

    def test_unique_append_does_not_rebuild_all_minutes(self) -> None:
        transcript = self.claude_projects / "append.jsonl"
        transcript.write_text(
            line(
                {
                    "type": "assistant",
                    "sessionId": "append-session",
                    "timestamp": "2026-08-25T18:00:00Z",
                    "requestId": "first",
                    "message": {
                        "id": "first-message",
                        "model": "claude-test",
                        "usage": {"input_tokens": 90, "output_tokens": 10},
                    },
                }
            ),
            encoding="utf-8",
        )
        agent_metrics.sync(self.config)
        original = agent_metrics.rebuild_minutes
        rebuilds = []

        def track_rebuild(connection):
            rebuilds.append(True)
            original(connection)

        agent_metrics.rebuild_minutes = track_rebuild
        try:
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(
                    line(
                        {
                            "type": "assistant",
                            "sessionId": "append-session",
                            "timestamp": "2026-08-25T18:01:00Z",
                            "requestId": "second",
                            "message": {
                                "id": "second-message",
                                "model": "claude-test",
                                "usage": {"input_tokens": 180, "output_tokens": 20},
                            },
                        }
                    )
                )
            agent_metrics.sync(self.config)
        finally:
            agent_metrics.rebuild_minutes = original
        with self.connection() as connection:
            total = connection.execute(
                "select sum(total_tokens) from minute_metrics"
            ).fetchone()[0]
        self.assertEqual(total, 300)
        self.assertEqual(rebuilds, [])

    def test_schema_repairs_incomplete_v4_database(self) -> None:
        connection = agent_metrics.connect_database(self.config.database)
        connection.execute("alter table events drop column quota_window_minutes")
        connection.execute("alter table events drop column quota_used_percent")
        connection.execute("alter table events drop column quota_resets_at")
        connection.execute("alter table sources drop column account_id")
        connection.commit()
        connection.close()
        upgraded = agent_metrics.connect_database(self.config.database)
        try:
            event_columns = {
                row[1] for row in upgraded.execute("pragma table_info(events)")
            }
            source_columns = {
                row[1] for row in upgraded.execute("pragma table_info(sources)")
            }
        finally:
            upgraded.close()
        self.assertTrue(
            {"quota_window_minutes", "quota_used_percent", "quota_resets_at"}
            <= event_columns
        )
        self.assertIn("account_id", source_columns)

    def test_identifier_migration_preserves_events_and_rebuilds_minutes(self) -> None:
        connection = agent_metrics.connect_database(self.config.database)
        connection.execute(
            "insert into events(event_key, provider, source_id, timestamp, minute, event_kind, "
            "session_id, request_id, input_tokens, total_tokens) values("
            "'event', 'claude', 'source', 60000, 60000, 'tokens', "
            "'raw-session', 'raw-request', 7, 7)"
        )
        connection.execute(
            "update metadata set value='0' where key='identifier_version'"
        )
        connection.commit()
        connection.close()

        upgraded = agent_metrics.connect_database(self.config.database)
        try:
            event = upgraded.execute(
                "select session_id, request_id from events where event_key='event'"
            ).fetchone()
            total = upgraded.execute("select sum(total_tokens) from minute_metrics").fetchone()[0]
        finally:
            upgraded.close()
        self.assertNotEqual(event["session_id"], "raw-session")
        self.assertNotEqual(event["request_id"], "raw-request")
        self.assertEqual(total, 7)

    def test_minute_table_migration_rebuilds_existing_events(self) -> None:
        connection = agent_metrics.connect_database(self.config.database)
        connection.execute(
            "insert into events(event_key, provider, source_id, timestamp, minute, event_kind, "
            "session_id, request_id, input_tokens, total_tokens) values("
            "'event', 'claude', 'source', 60000, 60000, 'tokens', "
            "'session', 'request', 7, 7)"
        )
        connection.execute("drop table minute_metrics")
        connection.execute(
            "create table minute_metrics("
            "minute integer, provider text, account_id text, model text, "
            "reasoning_effort text, session_id text, agent_id text, "
            "input_tokens integer, cached_input_tokens integer, "
            "cache_create_tokens integer, output_tokens integer, "
            "reasoning_tokens integer, total_tokens integer, cost_usd real)"
        )
        connection.execute(
            "insert into minute_metrics values(60000, 'claude', '', '', '', "
            "'session', '', 7, 0, 0, 0, 0, 7, null)"
        )
        connection.commit()
        connection.close()

        upgraded = agent_metrics.connect_database(self.config.database)
        try:
            total = upgraded.execute(
                "select sum(total_tokens) from minute_metrics"
            ).fetchone()[0]
        finally:
            upgraded.close()
        self.assertEqual(total, 7)

    def test_sync_rejects_a_second_collector(self) -> None:
        with agent_metrics.ingestion_lock(self.config.data_dir):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                agent_metrics.sync(self.config)

    def test_loopback_binding_policy(self) -> None:
        for host in ("127.0.0.1", "127.23.4.5", "localhost"):
            agent_metrics.validate_loopback(host)
        for host in ("0.0.0.0", "192.168.1.2", "::", "::1", "example.invalid"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                agent_metrics.validate_loopback(host)

    def test_dashboard_filters_sessions_and_quota_consistently(self) -> None:
        self.write_claude_fixture()
        self.write_codex_fixture()
        agent_metrics.sync(self.config)
        connection = agent_metrics.connect_database_readonly(self.config.database)
        try:
            claude = agent_metrics.dashboard_payload(connection, {"provider": ["claude"]})
            codex = agent_metrics.dashboard_payload(connection, {"provider": ["codex"]})
            agent_id = codex["filters"]["agents"][0]
            agent = agent_metrics.dashboard_payload(connection, {"agent": [agent_id]})
        finally:
            connection.close()
        self.assertTrue(claude["sessions"])
        self.assertEqual({row["provider"] for row in claude["sessions"]}, {"claude"})
        self.assertEqual(claude["quota"], [])
        self.assertEqual({row["provider"] for row in codex["sessions"]}, {"codex"})
        self.assertEqual({row["provider"] for row in codex["quota"]}, {"codex"})
        self.assertTrue(agent["timeline"])
        self.assertEqual({row["agent_id"] for row in agent["sessions"]}, {agent_id})

    def test_dashboard_filter_options_refresh_and_reasoning_is_not_double_stacked(self) -> None:
        script = (MODULE_PATH.parents[1] / "share/agent-metrics/app.js").read_text()
        self.assertIn("select.replaceChildren", script)
        self.assertNotIn("filterOptionsReady", script)
        self.assertIn("value - Number(row.reasoning_tokens", script)
        self.assertIn("if (!document.hidden) refresh()", script)
        self.assertIn("}, 60000)", script)

    def test_dashboard_moving_average_uses_only_present_minute_buckets(self) -> None:
        script_path = MODULE_PATH.parents[1] / "share/agent-metrics/app.js"
        javascript = """
const app = require(process.argv[1]);
const rows = [
  {minute: 0, input_tokens: 10, cached_input_tokens: 0, cache_create_tokens: 0, output_tokens: 4, reasoning_tokens: 2, total_tokens: 14},
  {minute: 60000, input_tokens: 20, cached_input_tokens: 0, cache_create_tokens: 0, output_tokens: 6, reasoning_tokens: 2, total_tokens: 26},
  {minute: 300000, input_tokens: 50, cached_input_tokens: 0, cache_create_tokens: 0, output_tokens: 8, reasoning_tokens: 3, total_tokens: 58},
  {minute: 720000, input_tokens: 70, cached_input_tokens: 0, cache_create_tokens: 0, output_tokens: 10, reasoning_tokens: 4, total_tokens: 80}
];
const smooth = app.movingAverageRows(rows, 10);
const raw = app.movingAverageRows(rows, 1);
const normalized = app.normalizeTimelineRows([
  {total_tokens: 100, cached_input_tokens: 50, cache_create_tokens: 0}
], 5)[0];
process.stdout.write(JSON.stringify({smooth, raw, normalized, output: app.seriesValue(rows[0], "output_tokens"), hours: app.hourlyDayRows(rows)}));
"""
        completed = subprocess.run(
            ["node", "-e", javascript, str(script_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual([row["input_tokens"] for row in result["raw"]], [10, 20, 50, 70])
        self.assertAlmostEqual(result["smooth"][2]["input_tokens"], 80 / 3)
        self.assertEqual(result["smooth"][3]["input_tokens"], 60)
        self.assertEqual(result["output"], 2)
        self.assertEqual(result["hours"][-1]["cumulative_tokens"], 178)
        self.assertEqual(result["normalized"]["total_tokens"], 20)
        self.assertEqual(result["normalized"]["cached_input_tokens"], 10)

    def test_all_time_timeline_is_bounded_without_changing_totals(self) -> None:
        connection = agent_metrics.connect_database(self.config.database)
        try:
            connection.executemany(
                "insert into minute_metrics(source_id, minute, provider, account_id, model, "
                "reasoning_effort, session_id, agent_id, input_tokens, cached_input_tokens, "
                "cache_create_tokens, output_tokens, reasoning_tokens, total_tokens, cost_usd) "
                "values(?, ?, 'claude', '', 'model', 'high', 'session', '', 1, 0, 0, 0, 0, 1, null)",
                [(f"source-{minute}", minute * 60000) for minute in range(4001)],
            )
            connection.commit()
            payload = agent_metrics.dashboard_payload(connection, {})
        finally:
            connection.close()
        self.assertLessEqual(len(payload["timeline"]), agent_metrics.MAX_TIMELINE_POINTS)
        self.assertGreater(payload["timeline_bucket_minutes"], 1)
        self.assertEqual(payload["overview"]["total_tokens"], 4001)

    def test_dashboard_caps_high_cardinality_filter_values(self) -> None:
        connection = agent_metrics.connect_database(self.config.database)
        try:
            connection.executemany(
                "insert into minute_metrics(source_id, minute, provider, account_id, model, "
                "reasoning_effort, session_id, agent_id, input_tokens, cached_input_tokens, "
                "cache_create_tokens, output_tokens, reasoning_tokens, total_tokens, cost_usd) "
                "values(?, ?, 'claude', ?, ?, 'high', ?, '', 1, 0, 0, 0, 0, 1, null)",
                [
                    (
                        f"source-{index}",
                        index * 60000,
                        f"account-{index}",
                        f"model-{index}",
                        f"session-{index}",
                    )
                    for index in range(600)
                ],
            )
            connection.executemany(
                "insert into accounts(account_id, provider, label) values(?, 'claude', ?)",
                [(f"account-{index}", f"Account {index}") for index in range(600)],
            )
            connection.executemany(
                "insert into events(event_key, provider, source_id, timestamp, minute, event_kind, "
                "tool_name, tool_status, turn_latency_ms) values(?, 'claude', ?, ?, ?, 'tool', ?, 'ok', 1)",
                [
                    (
                        f"event-{index}",
                        f"source-{index}",
                        index * 60000,
                        index * 60000,
                        f"tool-{index}",
                    )
                    for index in range(600)
                ],
            )
            connection.executemany(
                "insert into quota_observations(account_id, account_label, plan_cohort, "
                "observed_minute, quota_name, window_minutes, used_percent, stale, "
                "pending_reset, source_kind) values(?, ?, '5x', ?, 'five_hour', 300, 10, 0, 0, 'test')",
                [
                    (f"account-{index}", f"Account {index}", index * 60000)
                    for index in range(600)
                ],
            )
            connection.commit()
            with mock.patch.object(agent_metrics, "MAX_ANALYSIS_ROWS", 500):
                payload = agent_metrics.dashboard_payload(connection, {})
        finally:
            connection.close()
        self.assertEqual(len(payload["filters"]["sessions"]), agent_metrics.MAX_FILTER_VALUES)
        self.assertLessEqual(len(payload["accounts"]), agent_metrics.MAX_FILTER_VALUES)
        self.assertLessEqual(len(payload["models"]), agent_metrics.MAX_FILTER_VALUES)
        self.assertLessEqual(len(payload["account_labels"]), agent_metrics.MAX_FILTER_VALUES)
        self.assertLessEqual(len(payload["tools"]), agent_metrics.MAX_FILTER_VALUES)
        self.assertLessEqual(len(payload["analysis"]["daily_accounts"]), 500)
        self.assertLessEqual(len(payload["analysis"]["quota_history"]), 500)
        self.assertLessEqual(
            len(payload["analysis"]["account_status"]), agent_metrics.MAX_FILTER_VALUES
        )
        self.assertLessEqual(payload["latency"]["count"], 500)

    def test_timeline_preserves_idle_minute_gaps(self) -> None:
        now_minute = agent_metrics.minute_epoch(int(time.time() * 1000))
        connection = agent_metrics.connect_database(self.config.database)
        try:
            connection.executemany(
                "insert into minute_metrics(source_id, minute, provider, account_id, model, "
                "reasoning_effort, session_id, agent_id, input_tokens, cached_input_tokens, "
                "cache_create_tokens, output_tokens, reasoning_tokens, total_tokens, cost_usd) "
                "values(?, ?, 'claude', '', 'model', 'high', 'session', '', 10, 0, 0, 0, 0, 10, null)",
                [("early", now_minute - 10 * 60_000), ("latest", now_minute)],
            )
            connection.commit()
            payload = agent_metrics.dashboard_payload(
                connection,
                {"since": [str(now_minute - 15 * 60_000)]},
            )
        finally:
            connection.close()
        self.assertEqual(len(payload["timeline"]), 16)
        self.assertEqual(payload["timeline_bucket_minutes"], 1)
        self.assertEqual(payload["timeline"][6]["total_tokens"], 0)
        self.assertEqual(payload["overview"]["total_tokens"], 20)

    def test_account_status_drawdown_uses_latest_utilization_window(self) -> None:
        now_minute = agent_metrics.minute_epoch(int(time.time() * 1000))
        connection = agent_metrics.connect_database(self.config.database)
        try:
            connection.executemany(
                "insert into quota_observations(account_id, account_label, observed_minute, "
                "quota_name, window_minutes, used_percent, resets_at, stale, pending_reset, "
                "source_kind) values('account-work', 'Work', ?, 'five_hour', 300, ?, ?, 0, 0, 'test')",
                [
                    (now_minute - 30 * 60_000, 80, now_minute - 20 * 60_000),
                    (now_minute - 20 * 60_000, 5, now_minute + 280 * 60_000),
                    (now_minute - 10 * 60_000, 15, now_minute + 280 * 60_000 - 1000),
                    (now_minute, 25, now_minute + 280 * 60_000),
                ],
            )
            connection.commit()
            payload = agent_metrics.dashboard_payload(
                connection,
                {"since": [str(now_minute - 60 * 60_000)]},
            )
        finally:
            connection.close()
        account = next(
            row for row in payload["analysis"]["account_status"]
            if row["account_id"] == "account-work"
        )
        self.assertEqual(account["remaining_percent"], 75)
        self.assertEqual(account["drawdown_percent"], 20)

    def test_quota_capacity_excludes_stale_resets_and_groups_filtered_usage(self) -> None:
        connection = agent_metrics.connect_database(self.config.database)
        try:
            connection.execute(
                "insert into accounts(account_id, provider, label) values('account-work', 'claude', 'Work')"
            )
            observations = [
                (0, 10, 900000, 0, 0),
                (60000, 20, 900000, 0, 0),
                (120000, 30, 900000, 1, 0),
                (180000, 40, 900000, 0, 0),
                (240000, 50, 1800000, 0, 0),
                (300000, 60, 1800000, 0, 0),
                (360000, 55, 1800000, 0, 0),
            ]
            connection.executemany(
                "insert into quota_observations(account_id, account_label, plan_cohort, "
                "observed_minute, quota_name, window_minutes, used_percent, resets_at, stale, "
                "pending_reset, source_kind) values('account-work', 'Work', '20x', ?, "
                "'five_hour', 300, ?, ?, ?, ?, 'test')",
                [(minute, used, reset, stale, pending) for minute, used, reset, stale, pending in observations],
            )
            connection.executemany(
                "insert into minute_metrics(source_id, minute, provider, account_id, model, "
                "reasoning_effort, session_id, agent_id, input_tokens, cached_input_tokens, "
                "cache_create_tokens, output_tokens, reasoning_tokens, total_tokens, cost_usd) "
                "values(?, ?, 'claude', 'account-work', ?, ?, 'session', 'agent', ?, 0, 0, 0, 0, ?, null)",
                [
                    ("source-a", 60000, "model-a", "high", 100, 100),
                    ("source-b", 300000, "model-b", "low", 200, 200),
                    ("source-c", 360000, "model-c", "high", 999, 999),
                ],
            )
            connection.commit()
            all_rows = agent_metrics.quota_capacity_rows(connection, {})
            filtered = agent_metrics.quota_capacity_rows(connection, {"model": ["model-a"]})
            connection.execute(
                "insert into minute_metrics(source_id, minute, provider, account_id, model, "
                "reasoning_effort, session_id, agent_id, input_tokens, cached_input_tokens, "
                "cache_create_tokens, output_tokens, reasoning_tokens, total_tokens, cost_usd) "
                "values('mixed', 60000, 'claude', 'account-work', 'model-mixed', 'low', "
                "'session', 'agent', 100, 0, 0, 0, 0, 100, null)"
            )
            mixed_rows = agent_metrics.quota_capacity_rows(connection, {})
        finally:
            connection.close()
        self.assertEqual(
            {(row["model"], row["reasoning_effort"], row["sample_count"]) for row in all_rows},
            {("model-a", "high", 1), ("model-b", "low", 1)},
        )
        self.assertEqual({row["plan_cohort"] for row in all_rows}, {"20x"})
        self.assertEqual({row["estimated_tokens_at_100_pct"] for row in all_rows}, {1000, 2000})
        self.assertEqual([row["model"] for row in filtered], ["model-a"])
        self.assertEqual(filtered[0]["minimum_tracked_tokens"], 100)
        self.assertEqual(filtered[0]["maximum_tracked_tokens"], 100)
        self.assertEqual({row["model"] for row in mixed_rows}, {"model-b"})

    def test_shared_snapshot_quota_observation_uses_declared_tier_and_hash(self) -> None:
        account_id = "salted-account-id"
        now = 1_787_000_000
        snapshot = self.root / "accounts/statusline-snapshot.json"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(
            json.dumps(
                {
                    "version": 1,
                    "generated_at": now,
                    "health": {"last_success_at": now, "error": None},
                    "mode": {"mode": "auto", "label": None, "global_generation": 1},
                    "accounts": {
                        "Work": {
                            "five_hour": {
                                "used_pct": 12.5,
                                "resets_at": "2026-08-25T20:00:00Z",
                                "observed_at": now,
                                "stale": False,
                                "pending_reset": False,
                            },
                            "seven_day": {},
                            "scoped": [],
                            "expired": False,
                            "live_leases": 1,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        config = replace(
            self.config,
            shared_account_snapshot=snapshot,
            account_tiers={"Work": "20x"},
        )
        connection = agent_metrics.connect_database(config.database)
        try:
            connection.execute(
                "insert into accounts(account_id, provider, label) values(?, 'claude', 'Work')",
                (account_id,),
            )
            inserted = agent_metrics.ingest_shared_quota_snapshot(snapshot, config, connection)
            repeated = agent_metrics.ingest_shared_quota_snapshot(snapshot, config, connection)
            row = connection.execute("select * from quota_observations").fetchone()
        finally:
            connection.close()
        self.assertEqual(inserted, 1)
        self.assertEqual(repeated, 0)
        self.assertEqual(row["account_id"], account_id)
        self.assertEqual(row["account_label"], "Work")
        self.assertEqual(row["plan_cohort"], "20x")
        self.assertEqual(row["used_percent"], 12.5)

    def test_quota_observation_labels_and_tiers_refresh_without_history(self) -> None:
        connection = agent_metrics.connect_database(self.config.database)
        try:
            connection.execute(
                "insert into accounts(account_id, provider, label) values('account-work', 'claude', 'Current')"
            )
            connection.execute(
                "insert into quota_observations(account_id, account_label, plan_cohort, "
                "observed_minute, quota_name, window_minutes, used_percent, stale, pending_reset, "
                "source_kind) values('account-work', 'Old', '5x', 0, 'five_hour', 300, 10, 0, 0, 'test')"
            )
            config = replace(
                self.config,
                claude_utilization_history=self.root / "missing-history.jsonl",
                shared_account_snapshot=self.root / "missing-snapshot.json",
                account_tiers={"Current": "20x"},
            )
            salt = agent_metrics.load_or_create_salt(config.data_dir)
            changed = agent_metrics.ingest_quota_observations(config, salt, connection, [])
            row = connection.execute(
                "select account_label, plan_cohort from quota_observations"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(changed, 2)
        self.assertEqual(dict(row), {"account_label": "Current", "plan_cohort": "20x"})

    def test_declared_statusline_label_is_reused_without_persisting_identity(self) -> None:
        self.statusline_config.write_text(
            'ACCOUNT_LABELS="declared:*@label.invalid|private-org "\n'
            'ACCOUNT_LABELS="declared:*@label.invalid|private-org override:*@example.invalid"\n',
            encoding="utf-8",
        )
        self.account_spans.write_text(
            json.dumps(
                {
                    "label-session": {
                        "spans": [
                            {
                                "from": "2026-08-25T17:00:00Z",
                                "to": None,
                                "email": "person@label.invalid",
                                "org_uuid": "private-org",
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        transcript = self.claude_projects / "labels.jsonl"
        transcript.write_text(
            line(
                {
                    "type": "assistant",
                    "timestamp": "2026-08-25T17:00:01Z",
                    "sessionId": "label-session",
                    "requestId": "label-request",
                    "message": {
                        "model": "claude-test",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                }
            ),
            encoding="utf-8",
        )

        agent_metrics.sync(self.config)

        with self.connection() as connection:
            label = connection.execute("select label from accounts").fetchone()[0]
        self.assertEqual(label, "declared")
        persisted = self.config.database.read_bytes()
        self.assertNotIn(b"person@label.invalid", persisted)
        self.assertNotIn(b"private-org", persisted)
        self.assertNotIn(b"*@label.invalid", persisted)

        explicit_config = replace(
            self.config,
            account_aliases={"claude:person@label.invalid|private-org": "Explicit"},
        )
        agent_metrics.sync(explicit_config)
        with self.connection() as connection:
            label = connection.execute("select label from accounts").fetchone()[0]
        self.assertEqual(label, "Explicit")

    def test_sync_and_watch_arguments_are_bounded(self) -> None:
        parser = agent_metrics.build_parser()
        for arguments in (
            ["sync", "--max-lines", "0"],
            ["watch", "--max-lines", "0"],
            ["watch", "--interval", "9"],
        ):
            with self.subTest(arguments=arguments):
                with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(arguments)

    def test_watch_reports_each_cycle_and_stops_cleanly(self) -> None:
        summary = {
            "files": 2,
            "updated_files": 2,
            "lines": 5,
            "events": 4,
            "expired": 0,
            "live_lines": 1,
            "backfill_lines": 4,
            "pending_files": 3,
            "pending_bytes": 900,
        }
        with (
            mock.patch.object(agent_metrics, "sync", return_value=summary) as run_sync,
            mock.patch.object(agent_metrics.time, "monotonic", side_effect=[10.0, 10.25]),
            mock.patch.object(agent_metrics.time, "sleep", side_effect=KeyboardInterrupt) as sleep,
            redirect_stdout(io.StringIO()) as output,
        ):
            agent_metrics.watch(self.config, interval=60, max_lines=5000)
        run_sync.assert_called_once()
        self.assertEqual(run_sync.call_args.args, (self.config,))
        self.assertEqual(run_sync.call_args.kwargs["max_lines"], 5000)
        self.assertIsInstance(
            run_sync.call_args.kwargs["inventory"], agent_metrics.SourceInventory
        )
        sleep.assert_called_once_with(60)
        self.assertIn("elapsed 0.25s", output.getvalue())
        self.assertIn("Agent Metrics watch stopped", output.getvalue())

    def test_watch_reuses_inventory_and_detects_new_and_appended_sources(self) -> None:
        existing = self.claude_projects / "existing.jsonl"
        existing.write_text(
            line(
                {
                    "type": "assistant",
                    "timestamp": "2026-08-25T20:00:00Z",
                    "sessionId": "existing-session",
                    "requestId": "existing-initial",
                    "message": {
                        "model": "claude-test",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                }
            ),
            encoding="utf-8",
        )
        sleep_calls = 0

        def between_cycles(_interval: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 2:
                raise KeyboardInterrupt
            with existing.open("a", encoding="utf-8") as handle:
                handle.write(
                    line(
                        {
                            "type": "assistant",
                            "timestamp": "2026-08-25T20:00:01Z",
                            "sessionId": "existing-session",
                            "requestId": "existing-appended",
                            "message": {
                                "model": "claude-test",
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                        }
                    )
                )
            created = self.codex_sessions / "created.jsonl"
            created.write_text(
                line(
                    {
                        "timestamp": "2026-08-25T20:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "request_id": "created-request",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 1,
                                    "output_tokens": 1,
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

        original_hash = agent_metrics.keyed_hash
        with (
            mock.patch.object(
                agent_metrics,
                "inspect_source",
                wraps=agent_metrics.inspect_source,
            ) as inspect,
            mock.patch.object(
                agent_metrics,
                "iter_jsonl",
                wraps=agent_metrics.iter_jsonl,
            ) as discover,
            mock.patch.object(
                agent_metrics,
                "keyed_hash",
                wraps=original_hash,
            ) as hashed,
            mock.patch.object(
                agent_metrics.time,
                "monotonic",
                side_effect=[10.0, 10.1, 20.0, 20.2],
            ),
            mock.patch.object(
                agent_metrics.time,
                "sleep",
                side_effect=between_cycles,
            ),
            redirect_stdout(io.StringIO()),
        ):
            agent_metrics.watch(self.config, interval=60, max_lines=5000)

        self.assertEqual(discover.call_count, 4)
        self.assertEqual(inspect.call_count, 3)
        source_hashes = [
            call
            for call in hashed.call_args_list
            if len(call.args) > 1 and str(call.args[1]).startswith("source:")
        ]
        self.assertEqual(len(source_hashes), 2)
        with self.connection() as connection:
            requests = {
                row[0]
                for row in connection.execute(
                    "select request_id from events where event_kind='tokens'"
                )
            }
        self.assertEqual(
            requests,
            {
                self.identifier("existing-initial", "request"),
                self.identifier("existing-appended", "request"),
                self.identifier("created-request", "request"),
            },
        )

    def test_http_rejects_cross_site_requests_and_sets_security_headers(self) -> None:
        self.write_claude_fixture()
        agent_metrics.sync(self.config)
        handler = type(
            "TestDashboardHandler",
            (agent_metrics.DashboardHandler,),
            {
                "database": self.config.database,
                "configured_host": "127.0.0.1",
                "configured_port": 0,
                "capability_token": "test-capability",
            },
        )
        server = agent_metrics.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        handler.configured_port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        expected_host = f"127.0.0.1:{server.server_port}"
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", "/", headers={"Host": expected_host})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request(
                "GET", "/api/dashboard", headers={"Host": expected_host}
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 401)
            connection.close()

            access_log = io.StringIO()
            with redirect_stderr(access_log):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request(
                    "GET",
                    "/api/dashboard?session=private-session-id",
                    headers={
                        "Host": expected_host,
                        "Authorization": "Bearer test-capability",
                    },
                )
                response = connection.getresponse()
                response.read()
                connection.close()
            self.assertIn("GET /api/dashboard", access_log.getvalue())
            self.assertNotIn("private-session-id", access_log.getvalue())

            for headers, expected_status in (
                ({"Host": "evil.invalid"}, 421),
                ({"Host": expected_host, "Origin": "https://evil.invalid"}, 403),
                ({"Host": expected_host, "Sec-Fetch-Site": "cross-site"}, 403),
            ):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", "/api/dashboard", headers=headers)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, expected_status)
                self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_storage_permissions_and_unsafe_parent_rejection(self) -> None:
        self.write_claude_fixture()
        agent_metrics.sync(self.config)
        agent_metrics.load_or_create_ui_token(self.data_dir)
        self.assertEqual(self.data_dir.stat().st_mode & 0o777, 0o700)
        for path in (
            self.config.database,
            Path(f"{self.config.database}-wal"),
            Path(f"{self.config.database}-shm"),
            self.data_dir / "identity.salt",
            self.data_dir / "ui.token",
        ):
            if path.exists():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        self.data_dir.chmod(0o755)
        with self.assertRaises(PermissionError):
            agent_metrics.connect_database_readonly(self.config.database)
        self.data_dir.chmod(0o700)
        with mock.patch.object(agent_metrics.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(PermissionError):
                agent_metrics.validate_private_dir(self.data_dir)

        chmod_target = self.root / "chmod-failure"
        chmod_target.mkdir()
        with mock.patch.object(Path, "chmod", side_effect=OSError("chmod denied")):
            with self.assertRaises(OSError):
                agent_metrics.ensure_private_dir(chmod_target)


if __name__ == "__main__":
    unittest.main()
