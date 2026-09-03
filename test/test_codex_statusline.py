from __future__ import annotations

import base64
import dataclasses
import importlib.util
import io
import json
import os
import re
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
        payload = {"email": "developer@example.invalid"}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        self.assertEqual(codex_statusline.decode_jwt_payload(f"x.{encoded}.y"), payload)

    def test_account_label_uses_thread_router_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            thread_dir = Path(tmpdir)
            thread_id = "019f0000-0000-7000-8000-000000000000"
            (thread_dir / f"{thread_id}.json").write_text('{"label":"personal"}\n')
            with mock.patch.dict(
                os.environ,
                {"CODEX_ACCOUNTS_THREAD_DIR": str(thread_dir)},
            ):
                codex_statusline.account_label.cache_clear()
                self.assertEqual(codex_statusline.account_label(thread_id), "personal")

    def test_paths_related(self) -> None:
        self.assertTrue(codex_statusline.paths_related("/tmp/project", "/tmp/project/src"))
        self.assertTrue(codex_statusline.paths_related("/tmp/project/src", "/tmp/project"))
        self.assertFalse(codex_statusline.paths_related("/tmp/project-a", "/tmp/project-b"))

    def test_query_pull_request_resolves_branch(self) -> None:
        payload = json.dumps(
            {
                "number": 314,
                "state": "OPEN",
                "title": "Show Pull Request Linkage",
                "url": "https://github.com/acme/widget-app/pull/314",
            }
        )
        with mock.patch.object(
            codex_statusline.subprocess,
            "check_output",
            return_value=payload,
        ) as check_output:
            pull_request = codex_statusline.query_pull_request(
                "/work/logistics-app",
                "andrew/pr-linkage",
                "a1379d1",
            )

        self.assertEqual(pull_request["number"], 314)
        self.assertEqual(pull_request["title"], "Show Pull Request Linkage")
        command = check_output.call_args.args[0]
        self.assertEqual(command[:3], ["gh", "pr", "view"])
        self.assertEqual(command[3], "andrew/pr-linkage")

    def test_query_pull_request_resolves_detached_head(self) -> None:
        payload = json.dumps(
            {
                "number": 314,
                "state": "OPEN",
                "title": "Show Pull Request Linkage",
                "url": "https://github.com/acme/widget-app/pull/314",
            }
        )
        with mock.patch.object(
            codex_statusline.subprocess,
            "check_output",
            side_effect=["acme/widget-app\n", "314\n", payload],
        ) as check_output:
            pull_request = codex_statusline.query_pull_request(
                "/work/logistics-app",
                "",
                "a1379d1309d15399bb8ca20dbc2fb8a1ce6a8065",
            )

        self.assertEqual(pull_request["number"], 314)
        commands = [call.args[0] for call in check_output.call_args_list]
        self.assertEqual(commands[0][:3], ["gh", "repo", "view"])
        self.assertEqual(commands[1][:2], ["gh", "api"])
        self.assertEqual(commands[2][:3], ["gh", "pr", "view"])

    def test_pr_refresh_lock_can_be_reused_after_owner_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "statusline-pr.json"
            lock_file = Path(f"{cache_file}.lock")
            descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            codex_statusline.fcntl.flock(
                descriptor,
                codex_statusline.fcntl.LOCK_EX | codex_statusline.fcntl.LOCK_NB,
            )
            try:
                with mock.patch.object(codex_statusline.subprocess, "Popen") as popen:
                    codex_statusline.start_pr_cache_refresh(
                        cache_file,
                        "/work/logistics-app",
                        "andrew/pr-linkage",
                        "a1379d1",
                    )
                popen.assert_not_called()
            finally:
                os.close(descriptor)

            child = mock.Mock()
            child.poll.return_value = None
            with mock.patch.object(codex_statusline.subprocess, "Popen", return_value=child) as popen:
                codex_statusline.start_pr_cache_refresh(
                    cache_file,
                    "/work/logistics-app",
                    "andrew/pr-linkage",
                    "a1379d1",
                )

            popen.assert_called_once()
            self.assertEqual(len(popen.call_args.kwargs["pass_fds"]), 1)
            codex_statusline.PR_REFRESH_PROCESSES.clear()

    def test_reap_pr_cache_refreshes_drops_completed_children(self) -> None:
        completed = mock.Mock()
        completed.poll.return_value = 0
        running = mock.Mock()
        running.poll.return_value = None
        codex_statusline.PR_REFRESH_PROCESSES[:] = [completed, running]

        codex_statusline.reap_pr_cache_refreshes()

        self.assertEqual(codex_statusline.PR_REFRESH_PROCESSES, [running])
        completed.poll.assert_called_once_with()
        running.poll.assert_called_once_with()
        codex_statusline.PR_REFRESH_PROCESSES.clear()

    def test_claude_statusline_renders_pr_below_repo_for_branch_and_detached_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pr_number = 314
            tmp = Path(tmpdir)
            home = tmp / "home"
            cache = tmp / "cache"
            project = tmp / "logistics-app"
            fake_bin = tmp / "bin"
            for path in (home / ".claude", cache, project, fake_bin):
                path.mkdir(parents=True, exist_ok=True)
            (home / ".claude/statusline.conf").write_text("MAX_COLS=100\n")

            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=project,
                check=True,
                capture_output=True,
            )
            (project / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Statusline Test",
                    "-c",
                    "user.email=statusline@example.com",
                    "commit",
                    "-m",
                    "fixture",
                ],
                cwd=project,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "-b", "andrew/pr-linkage"],
                cwd=project,
                check=True,
                capture_output=True,
            )

            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == pr && \"$2\" == view ]]; then\n"
                "  printf '%s\\n' \"$FAKE_PR_JSON\"\n"
                "elif [[ \"$1\" == repo && \"$2\" == view ]]; then\n"
                "  printf '%s\\n' 'acme/widget-app'\n"
                "elif [[ \"$1\" == api ]]; then\n"
                f"  printf '%s\\n' '{pr_number}'\n"
                "fi\n"
            )
            fake_gh.chmod(0o755)

            script = tmp / "statusline.sh"
            script.write_text(
                (MODULE_PATH.with_name("statusline.sh"))
                .read_text()
                .replace("/tmp/claude", str(cache))
            )
            fixture = json.loads(
                (MODULE_PATH.parents[1] / "test/fixtures/input.json").read_text()
            )
            fixture["workspace"]["current_dir"] = str(project)
            env = {
                **os.environ,
                "FAKE_PR_JSON": json.dumps(
                    {
                        "number": pr_number,
                        "state": "OPEN",
                        "isDraft": False,
                        "reviewDecision": "REVIEW_REQUIRED",
                        "statusCheckRollup": [],
                        "title": "Show Pull Request Linkage",
                        "url": "https://github.com/acme/widget-app/pull/314",
                    }
                ),
                "HOME": str(home),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "STATUSLINE_FORMAT": "default",
                "TZ": "America/Los_Angeles",
            }

            def render() -> list[str]:
                result = subprocess.run(
                    ["bash", str(script)],
                    cwd=project,
                    env=env,
                    input=json.dumps(fixture),
                    text=True,
                    capture_output=True,
                    check=True,
                )
                return re.sub(r"\x1b(?:\[[0-9;]*m|\][^\a]*\a)", "", result.stdout).splitlines()

            render()
            deadline = time.monotonic() + 10
            while not list(cache.glob("statusline-pr-*.json")) and time.monotonic() < deadline:
                time.sleep(0.02)
            branch_lines = render()
            branch_repo_index = next(i for i, line in enumerate(branch_lines) if line.startswith("repo"))
            self.assertTrue(branch_lines[branch_repo_index + 1].startswith("pr"))
            self.assertIn(f"#{pr_number}", branch_lines[branch_repo_index + 1])
            self.assertIn("Pull Request Linkage", branch_lines[branch_repo_index + 1])

            (home / ".claude/statusline.conf").write_text("MAX_COLS=50\n")
            narrow_lines = render()
            narrow_repo_index = next(
                i for i, line in enumerate(narrow_lines) if line.startswith(project.name)
            )
            self.assertTrue(narrow_lines[narrow_repo_index + 1].startswith("pr"))
            self.assertIn(f"#{pr_number}", narrow_lines[narrow_repo_index + 1])
            (home / ".claude/statusline.conf").write_text("MAX_COLS=100\n")

            subprocess.run(
                ["git", "checkout", "--detach"],
                cwd=project,
                check=True,
                capture_output=True,
            )
            render()
            deadline = time.monotonic() + 10
            while len(list(cache.glob("statusline-pr-*.json"))) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            detached_lines = render()
            detached_repo_index = next(
                i for i, line in enumerate(detached_lines) if line.startswith("repo")
            )
            self.assertTrue(detached_lines[detached_repo_index + 1].startswith("pr"))
            self.assertIn(f"#{pr_number}", detached_lines[detached_repo_index + 1])

    def test_claude_statusline_infers_only_live_executed_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir).resolve()
            home = tmp / "home"
            cache = tmp / "cache"
            project = tmp / "project"
            worktree = tmp / "feature tree"
            fake_bin = tmp / "bin"
            for path in (home / ".claude", cache, project, fake_bin):
                path.mkdir(parents=True, exist_ok=True)
            (home / ".claude/statusline.conf").write_text("MAX_COLS=100\n")
            (fake_bin / "gh").write_text("#!/usr/bin/env bash\nexit 1\n")
            (fake_bin / "gh").chmod(0o755)

            subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
            (project / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=project, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Statusline Test",
                    "-c",
                    "user.email=statusline@example.invalid",
                    "commit",
                    "-m",
                    "fixture",
                ],
                cwd=project,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "-b", "feature/worktree", str(worktree)],
                cwd=project,
                check=True,
                capture_output=True,
            )

            script = tmp / "statusline.sh"
            script.write_text(
                (MODULE_PATH.with_name("statusline.sh"))
                .read_text()
                .replace("/tmp/claude", str(cache))
            )
            fixture = json.loads(
                (MODULE_PATH.parents[1] / "test/fixtures/input.json").read_text()
            )
            fixture["workspace"]["current_dir"] = str(project)
            fixture["session_id"] = "physical-session"
            project_dir = str(project).replace("/", "-")
            transcript_dir = home / ".claude/projects" / project_dir
            transcript_dir.mkdir(parents=True)
            transcript = transcript_dir / "physical-session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": f"Do not run git -C {worktree} status",
                        },
                    }
                )
                + "\n"
            )
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "STATUSLINE_FORMAT": "default",
                "TZ": "America/Los_Angeles",
            }

            def repo_line() -> str:
                result = subprocess.run(
                    ["bash", str(script)],
                    cwd=project,
                    env=env,
                    input=json.dumps(fixture),
                    text=True,
                    capture_output=True,
                    check=True,
                )
                lines = re.sub(r"\x1b(?:\[[0-9;]*m|\][^\a]*\a)", "", result.stdout).splitlines()
                return next(line for line in lines if line.startswith("repo"))

            self.assertIn("main", repo_line())
            with transcript.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "name": "Bash",
                                        "input": {
                                            "command": f'printf %s "do not switch: git -C {worktree} status"'
                                        },
                                    }
                                ],
                            },
                        }
                    )
                    + "\n"
                )
            self.assertIn("main", repo_line())
            with transcript.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "name": "Bash",
                                        "input": {"command": f'git -C "{worktree}" status'},
                                    }
                                ],
                            },
                        }
                    )
                    + "\n"
                )
            self.assertIn("⌥ project worktree (worktree)", repo_line())

            logical_root = tmp / "logical"
            logical_root.symlink_to(tmp, target_is_directory=True)
            fixture["workspace"]["current_dir"] = str(logical_root / "project")
            fixture["session_id"] = "logical-session"
            logical_project_dir = str(logical_root / "project").replace("/", "-")
            logical_transcript_dir = home / ".claude/projects" / logical_project_dir
            logical_transcript_dir.mkdir(parents=True)
            logical_transcript = logical_transcript_dir / "logical-session.jsonl"
            logical_transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {
                                        "command": f'git -C "{logical_root / "feature tree"}" status'
                                    },
                                }
                            ],
                        },
                    }
                )
                + "\n"
            )
            self.assertIn("⌥ project worktree (worktree)", repo_line())

            fixture["workspace"]["current_dir"] = str(project)
            fixture["session_id"] = "physical-session"
            worktree.rename(tmp / "deleted-worktree")
            self.assertIn("main", repo_line())

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

    def test_rollout_consumers_decode_large_history_once_then_only_appends(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = [
            {
                "timestamp": f"2026-07-29T12:00:0{index}Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": index + 1}},
                },
            }
            for index in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "large.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            thread = codex_statusline.Thread(
                id="thread",
                source="cli",
                rollout_path=str(path),
                created_at=0,
                updated_at=int(datetime(2026, 7, 29, 12, 0, 4).timestamp()),
                cwd="/tmp",
                title="",
                tokens_used=5,
                model="",
                reasoning_effort="",
                sandbox_policy="",
                approval_mode="",
                git_branch="",
                archived=0,
            )
            original_decode = codex_statusline.decode_rollout_line
            decoded_lines = 0

            def counted_decode(line: bytes):
                nonlocal decoded_lines
                decoded_lines += 1
                return original_decode(line)

            with mock.patch.object(
                codex_statusline,
                "decode_rollout_line",
                side_effect=counted_decode,
            ):
                codex_statusline.rollout_activity(
                    thread,
                    datetime(2026, 7, 29, 12, 0, 5),
                )
                self.assertEqual(decoded_lines, len(events))

                self.assertEqual(
                    codex_statusline.latest_token_count(str(path))["info"]["total_token_usage"]["total_tokens"],
                    5,
                )
                codex_statusline.rollout_activity(
                    thread,
                    datetime(2026, 7, 29, 12, 0, 6),
                )
                self.assertEqual(decoded_lines, len(events))

                appended = {
                    "timestamp": "2026-07-29T12:00:05Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 6}},
                    },
                }
                with path.open("a") as stream:
                    stream.write(json.dumps(appended) + "\n")

                self.assertEqual(
                    codex_statusline.latest_token_count(str(path))["info"]["total_token_usage"]["total_tokens"],
                    6,
                )
                codex_statusline.rollout_activity(
                    thread,
                    datetime(2026, 7, 29, 12, 0, 7),
                )

        self.assertEqual(decoded_lines, len(events) + 1)

    def test_rollout_state_streams_without_unbounded_reads(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        record = (
            b'{"timestamp":"2026-07-29T12:00:00Z","type":"compacted",'
            b'"payload":{"text":"' + b"x" * (2 * 1024 * 1024) + b'"}}\n'
        )
        read_sizes: list[int] = []

        class BoundedReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def __iter__(self):
                raise AssertionError("rollout iteration can materialize an entire record")

            def readline(self, _size=-1):
                raise AssertionError("rollout readline can materialize an entire record")

            def read(self, size=-1):
                self.assert_bounded(size)
                read_sizes.append(size)
                return self.stream.read(size)

            def seek(self, *args):
                return self.stream.seek(*args)

            def fileno(self):
                return self.stream.fileno()

            @staticmethod
            def assert_bounded(size):
                if size < 0:
                    raise AssertionError("unbounded rollout read")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "streamed.jsonl"
            path.write_bytes(record)
            original_open = codex_statusline.Path.open

            def bounded_open(target, *args, **kwargs):
                return BoundedReader(original_open(target, *args, **kwargs))

            with mock.patch.object(
                codex_statusline.Path,
                "open",
                autospec=True,
                side_effect=bounded_open,
            ):
                state = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(state.activity.compactions, 1)
        self.assertTrue(read_sizes)
        self.assertLessEqual(max(read_sizes), codex_statusline.ROLLOUT_READ_BYTES)

    def test_rollout_state_keeps_repeated_completed_turn_closed(self) -> None:
        state = codex_statusline.new_rollout_state(1, None)
        for payload in (
            {"type": "task_started", "turn_id": "turn", "started_at": 100},
            {"type": "task_complete", "turn_id": "turn"},
            {"type": "task_started", "turn_id": "turn", "started_at": 200},
        ):
            codex_statusline.update_activity_state(
                state,
                {"type": "event_msg", "payload": payload},
            )

        self.assertEqual(state.activity.turns_started, 2)
        self.assertEqual(state.activity.turns_completed, 1)
        self.assertEqual(state.activity.started_turns, {})
        self.assertIn("turn", state.activity.recent_closed_turns)

    def test_rollout_state_cache_stable_257_path_sweep_replays_one_path(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        original_decode = codex_statusline.decode_rollout_line
        decoded_lines = 0

        def counted_decode(line: bytes):
            nonlocal decoded_lines
            decoded_lines += 1
            return original_decode(line)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = []
            for index in range(codex_statusline.MAX_ROLLOUT_STATE_CACHE + 1):
                path = Path(tmpdir) / f"{index}.jsonl"
                path.write_text(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {
                                        "total_tokens": index,
                                    }
                                },
                            },
                        }
                    )
                    + "\n"
                )
                paths.append(str(path))

            with mock.patch.object(
                codex_statusline,
                "decode_rollout_line",
                side_effect=counted_decode,
            ):
                with codex_statusline.rollout_state_sweep():
                    for path in paths:
                        codex_statusline.read_rollout_state(path)
                        codex_statusline.read_rollout_state(path)
                first_sweep_decodes = decoded_lines
                with codex_statusline.rollout_state_sweep():
                    for path in paths:
                        codex_statusline.read_rollout_state(path)
                        codex_statusline.read_rollout_state(path)

        self.assertEqual(first_sweep_decodes, len(paths))
        self.assertEqual(decoded_lines - first_sweep_decodes, 1)
        self.assertEqual(
            len(codex_statusline.ROLLOUT_STATE_CACHE),
            codex_statusline.MAX_ROLLOUT_STATE_CACHE,
        )

    def test_rollout_state_sweep_bounds_encountered_paths(self) -> None:
        with codex_statusline.rollout_state_sweep():
            for index in range(20_000):
                codex_statusline.cached_rollout_state(f"/tmp/{index}.jsonl")
            self.assertEqual(
                len(codex_statusline.ACTIVE_ROLLOUT_STATE_SWEEP.encountered),
                codex_statusline.MAX_ROLLOUT_STATE_CACHE,
            )

    def test_rollout_append_reads_only_delta_and_fixed_probes(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        history = (
            b'{"timestamp":"2026-07-29T12:00:00Z","type":"compacted",'
            b'"payload":{"text":"' + b"x" * (2 * 1024 * 1024) + b'"}}\n'
        )
        appended = (
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "total_tokens": 7,
                            }
                        },
                    },
                }
            ).encode()
            + b"\n"
        )
        bytes_read = 0

        class CountingReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, size=-1):
                nonlocal bytes_read
                if size < 0:
                    raise AssertionError("unbounded rollout read")
                chunk = self.stream.read(size)
                bytes_read += len(chunk)
                return chunk

            def seek(self, *args):
                return self.stream.seek(*args)

            def fileno(self):
                return self.stream.fileno()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "append.jsonl"
            path.write_bytes(history)
            codex_statusline.read_rollout_state(str(path))
            with path.open("ab") as stream:
                stream.write(appended)

            original_open = codex_statusline.Path.open

            def counting_open(target, *args, **kwargs):
                return CountingReader(original_open(target, *args, **kwargs))

            with mock.patch.object(
                codex_statusline.Path,
                "open",
                autospec=True,
                side_effect=counting_open,
            ):
                state = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(
            state.latest_token["info"]["total_token_usage"]["total_tokens"],
            7,
        )
        self.assertLessEqual(
            bytes_read,
            len(appended) + 4 * codex_statusline.ROLLOUT_PROBE_BYTES,
        )

    def test_streamed_compacted_payload_shapes_and_malformed_records(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        valid_payloads = (
            b"{}",
            b"[]",
            b'"text"',
            b"-12.5e+3",
            b"true",
            b"false",
            b"null",
            '{"emoji":"😀","escape":"\\u263a"}'.encode(),
        )
        malformed_payloads = (
            b'{"bad_escape":"\\q"}',
            b'{"bad_number":01}',
            b'{"trailing":[1,]}',
            b'{"unfinished":"x}',
            b'{"raw":"\xff"}',
            b"truX",
            b"[" * (codex_statusline.MAX_JSON_DEPTH + 1)
            + b"0"
            + b"]" * (codex_statusline.MAX_JSON_DEPTH + 1),
        )
        prefix = (
            b'{"timestamp":"2026-07-29T12:00:00Z","type":"compacted",'
            b'"payload":'
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(
                    codex_statusline,
                    "MAX_BUFFERED_ROLLOUT_LINE_BYTES",
                    len(prefix),
                ),
                mock.patch.object(codex_statusline, "ROLLOUT_READ_BYTES", 1),
            ):
                for index, payload in enumerate(valid_payloads):
                    with self.subTest(valid=payload):
                        path = Path(tmpdir) / f"valid-{index}.jsonl"
                        path.write_bytes(prefix + payload + b"}" + b" " * 200 + b"\r\n")
                        codex_statusline.ROLLOUT_STATE_CACHE.clear()
                        state = codex_statusline.read_rollout_state(str(path))
                        self.assertEqual(state.activity.compactions, 1)
                for index, payload in enumerate(malformed_payloads):
                    with self.subTest(invalid=payload):
                        path = Path(tmpdir) / f"invalid-{index}.jsonl"
                        path.write_bytes(prefix + payload + b"}" + b" " * 200 + b"\n")
                        codex_statusline.ROLLOUT_STATE_CACHE.clear()
                    state = codex_statusline.read_rollout_state(str(path))
                    self.assertEqual(state.activity.compactions, 0)

    def test_rollout_state_streams_oversized_tool_call(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        event = {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-1",
                "name": "exec_command",
                "arguments": json.dumps(
                    {
                        "padding": "x" * (2 * 1024 * 1024),
                        "cmd": "printf done",
                        "nested": {"session_id": "session-1"},
                    }
                ),
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "large-tool-call.jsonl"
            path.write_text(json.dumps(event) + "\n")
            state = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(state.activity.tool_calls, 1)
        self.assertEqual(state.activity.shell_calls, 1)
        self.assertEqual(state.activity.last_command, "printf done")
        self.assertEqual(state.activity.pending_tools, {"call-1": "exec_command"})
        self.assertEqual(state.activity.tool_sessions, {"call-1": "session-1"})

    def test_rollout_state_streams_oversized_tool_output(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = (
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "start",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "sleep 30"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "start",
                    "output": json.dumps({"session_id": "session-1"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "poll",
                    "name": "write_stdin",
                    "arguments": json.dumps({"session_id": "session-1"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "poll",
                    "output": json.dumps(
                        {
                            "padding": "x" * (2 * 1024 * 1024),
                            "exit_code": 0,
                        }
                    ),
                },
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "large-tool-output.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            state = codex_statusline.read_rollout_state(str(path))

        activity = codex_statusline.activity_from_state(
            state.activity,
            mock.Mock(updated_at=int(datetime.now().timestamp())),
            datetime.now(),
            900,
        )
        self.assertEqual(state.activity.tool_calls, 2)
        self.assertEqual(activity.active_tools, 0)
        self.assertEqual(activity.active_shells, 0)

    def test_rollout_state_streams_session_from_malformed_oversized_arguments(
        self,
    ) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = (
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "start",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "sleep 30"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "start",
                    "output": json.dumps({"session_id": "session-1"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "poll",
                    "name": "write_stdin",
                    "arguments": (
                        '{"session_id":"session-1",' + "x" * (2 * 1024 * 1024)
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "poll",
                    "output": json.dumps({"exit_code": 0}),
                },
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "large-malformed-arguments.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            state = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(state.activity.pending_tools, {})
        self.assertEqual(state.activity.running_shells, {})

    def test_rollout_state_streams_spaced_session_from_malformed_arguments(
        self,
    ) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = (
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "start",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "sleep 30"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "start",
                    "output": json.dumps({"session_id": "session-1"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "poll",
                    "name": "write_stdin",
                    "arguments": (
                        '{"session_id"'
                        + " " * 2048
                        + ":"
                        + " " * 2048
                        + '"session-1",'
                        + "x" * (2 * 1024 * 1024)
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "poll",
                    "output": json.dumps({"exit_code": 0}),
                },
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "large-spaced-malformed-arguments.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            state = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(state.activity.pending_tools, {})
        self.assertEqual(state.activity.running_shells, {})

    def test_streaming_tool_session_handles_escaped_value_across_chunks(
        self,
    ) -> None:
        nested = (
            '{"session_id"'
            + " " * 2048
            + ":"
            + " " * 2048
            + '"session\\u002d1",'
        )
        encoded = json.dumps(nested)[1:-1].encode()
        capture = codex_statusline.EmbeddedJsonStringCapture(
            session_any_depth=True,
        )

        for value in encoded:
            capture.feed(bytes((value,)))

        self.assertEqual(capture.finish()["session_id"], "session-1")

    def test_rollout_state_streams_oversized_token_count(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        event = {
            "type": "event_msg",
            "payload": {
                "padding": "x" * (2 * 1024 * 1024),
                "type": "token_count",
                "info": {
                    "model_context_window": 1_000_000,
                    "total_token_usage": {"total_tokens": 123},
                },
                "rate_limits": {
                    "plan_type": "pro",
                    "credits": {
                        "balance": "2500.0000000000",
                        "has_credits": True,
                        "unlimited": False,
                    },
                },
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "large-token-count.jsonl"
            path.write_text(json.dumps(event) + "\n")
            state = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(
            state.latest_token["info"]["total_token_usage"]["total_tokens"],
            123,
        )
        self.assertEqual(state.latest_token["info"]["model_context_window"], 1_000_000)
        self.assertEqual(state.latest_token["rate_limits"]["plan_type"], "pro")
        self.assertEqual(
            state.latest_token["rate_limits"]["credits"],
            {
                "balance": "2500.0000000000",
                "has_credits": True,
                "unlimited": False,
            },
        )

    def test_rollout_state_rejects_oversized_malformed_record_and_continues(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        malformed = (
            b'{"type":"response_item","payload":{"type":"custom_tool_call",'
            b'"arguments":"' + b"x" * (2 * 1024 * 1024) + b'\\q"}}\n'
        )
        valid = (
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": "valid",
                        "started_at": 1,
                    },
                }
            ).encode()
            + b"\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "malformed-then-valid.jsonl"
            path.write_bytes(malformed + valid)
            state = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(state.activity.turns_started, 1)
        self.assertEqual(state.activity.started_turns, {"valid": 1})
        self.assertEqual(state.activity.tool_calls, 0)

    def test_compacted_canonical_record_validates_without_materializing_payload(self) -> None:
        line = (
            b'{"timestamp":"2026-07-29T12:00:00Z","type":"compacted",'
            b'"payload":{"message":"large"}}'
        )

        with mock.patch.object(
            codex_statusline.json,
            "loads",
            side_effect=AssertionError("canonical compacted line reached JSON"),
        ):
            decoded = codex_statusline.decode_rollout_line(line)

        self.assertEqual(
            decoded,
            {"type": "compacted", "timestamp": "2026-07-29T12:00:00Z"},
        )

    def test_compacted_records_reject_nested_and_accept_alternate_shapes(self) -> None:
        nested = json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": (
                        '{"timestamp":"2026-07-29T12:00:00Z",'
                        '"type":"compacted","payload":{}}'
                    ),
                },
            },
            separators=(",", ":"),
        ).encode()
        alternate = (
            b'{"type":"compacted","timestamp":"2026-07-29T12:00:00Z",'
            b'"payload":{}}'
        )

        with mock.patch.object(
            codex_statusline.json,
            "loads",
            wraps=json.loads,
        ) as loads:
            nested_decoded = codex_statusline.decode_rollout_line(nested)
            alternate_decoded = codex_statusline.decode_rollout_line(alternate)

        self.assertEqual(loads.call_count, 2)
        self.assertEqual(nested_decoded["payload"]["type"], "agent_message")
        self.assertEqual(
            alternate_decoded,
            {"type": "compacted", "timestamp": "2026-07-29T12:00:00Z"},
        )

    def test_compacted_prefix_does_not_accept_malformed_json(self) -> None:
        malformed_payloads = (
            b'{"unfinished":}',
            b'{"bad_escape":"\\q"}',
            b'{"bad_number":01}',
            b'{"unfinished_string":"x}',
        )

        for payload in malformed_payloads:
            line = (
                b'{"timestamp":"2026-07-29T12:00:00Z","type":"compacted",'
                b'"payload":' + payload + b"}"
            )
            with self.subTest(payload=payload):
                self.assertIsNone(codex_statusline.decode_rollout_line(line))

    def test_rollout_consumers_preserve_trailing_line_without_reapplying_it(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        event = {
            "timestamp": "2026-07-29T12:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 7}},
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "partial.jsonl"
            path.write_text(json.dumps(event))
            original_decode = codex_statusline.decode_rollout_line
            decoded_lines = 0

            def counted_decode(line: bytes):
                nonlocal decoded_lines
                decoded_lines += 1
                return original_decode(line)

            with mock.patch.object(
                codex_statusline,
                "decode_rollout_line",
                side_effect=counted_decode,
            ):
                self.assertEqual(
                    codex_statusline.latest_token_count(str(path))["info"]["total_token_usage"]["total_tokens"],
                    7,
                )

                with path.open("a") as stream:
                    stream.write("\n")

                self.assertEqual(
                    codex_statusline.latest_token_count(str(path))["info"]["total_token_usage"]["total_tokens"],
                    7,
                )

        self.assertEqual(decoded_lines, 1)

    def test_rollout_consumers_finish_incomplete_trailing_line_incrementally(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        event = {
            "timestamp": "2026-07-29T12:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 7}},
            },
        }
        encoded = json.dumps(event).encode()
        split_at = len(encoded) // 2
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "partial.jsonl"
            path.write_bytes(encoded[:split_at])

            self.assertEqual(codex_statusline.latest_token_count(str(path)), {})

            with path.open("ab") as stream:
                stream.write(encoded[split_at:])

            self.assertEqual(
                codex_statusline.latest_token_count(str(path))["info"]["total_token_usage"]["total_tokens"],
                7,
            )

    def test_token_boundary_state_decodes_only_new_lines(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = [
            {
                "timestamp": datetime.fromtimestamp(timestamp).astimezone().isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": total}},
                },
            }
            for timestamp, total in ((100, 100), (200, 200))
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tokens.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            original_decode = codex_statusline.decode_rollout_line
            decoded_lines = 0

            def counted_decode(line: bytes):
                nonlocal decoded_lines
                decoded_lines += 1
                return original_decode(line)

            with mock.patch.object(
                codex_statusline,
                "decode_rollout_line",
                side_effect=counted_decode,
            ):
                self.assertEqual(codex_statusline.tokens_since_boundary(str(path), 250, 150), 150)
                self.assertEqual(codex_statusline.tokens_since_boundary(str(path), 250, 150), 150)
                self.assertEqual(decoded_lines, 2)

                appended = {
                    "timestamp": datetime.fromtimestamp(125).astimezone().isoformat(),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 120}},
                    },
                }
                with path.open("a") as stream:
                    stream.write(json.dumps(appended) + "\n")

                self.assertEqual(codex_statusline.tokens_since_boundary(str(path), 260, 150), 140)

        self.assertEqual(decoded_lines, 3)

    def test_token_boundary_pruning_preserves_file_order(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = [
            {
                "timestamp": datetime.fromtimestamp(timestamp).astimezone().isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": total}},
                },
            }
            for timestamp, total in ((500, 1), (100, 2))
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "out-of-order-tokens.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            codex_statusline.tokens_since_boundary(str(path), 2, 400)
            value = codex_statusline.tokens_since_boundary(str(path), 2, 600)

        self.assertEqual(value, 0)

    def test_rollout_state_resets_after_truncation_and_replacement(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout.jsonl"
            started = {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "one", "started_at": 100},
            }
            path.write_text(json.dumps(started) + "\n")
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

            initial = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(110))
            path.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": "different"},
                    }
                )
                + "\n"
            )
            truncated = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(111))

            replacement = Path(tmpdir) / "replacement.jsonl"
            replacement.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "replacement"},
                    }
                )
                + "\n"
            )
            replacement.replace(path)
            replaced = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(112))

        self.assertEqual(initial.turns_started, 1)
        self.assertEqual(truncated.turns_started, 0)
        self.assertEqual(truncated.turns_completed, 1)
        self.assertEqual(replaced.turns_completed, 0)
        self.assertEqual(replaced.last_agent_message, "replacement")

    def test_rollout_state_resets_same_size_rewrite_with_preserved_tail(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        old_token = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 111}},
            },
        }
        new_token = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 222}},
            },
        }
        preserved_tail = json.dumps({"ignored": "x" * 600}) + "\n"
        old_content = json.dumps(old_token) + "\n" + preserved_tail
        new_content = json.dumps(new_token) + "\n" + preserved_tail
        self.assertEqual(len(old_content), len(new_content))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rewritten.jsonl"
            path.write_text(old_content)
            first = codex_statusline.latest_token_count(str(path))
            original_inode = path.stat().st_ino

            path.write_text(new_content)
            self.assertEqual(path.stat().st_ino, original_inode)
            second = codex_statusline.latest_token_count(str(path))

        self.assertEqual(first["info"]["total_token_usage"]["total_tokens"], 111)
        self.assertEqual(second["info"]["total_token_usage"]["total_tokens"], 222)

    def test_rollout_state_resets_after_truncate_and_regrow(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        old_token = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 111}},
            },
        }
        new_token = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 222}},
            },
        }
        old_content = json.dumps(old_token) + "\n" + json.dumps({"ignored": "x" * 600}) + "\n"
        new_content = json.dumps(new_token) + "\n" + json.dumps({"ignored": "y" * 800}) + "\n"

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "regrown.jsonl"
            path.write_text(old_content)
            codex_statusline.latest_token_count(str(path))
            original_inode = path.stat().st_ino

            path.write_text(new_content)
            self.assertEqual(path.stat().st_ino, original_inode)
            refreshed = codex_statusline.latest_token_count(str(path))

        self.assertEqual(refreshed["info"]["total_token_usage"]["total_tokens"], 222)

    def test_rollout_state_resets_growth_after_prior_tail_rewrite(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        old_token = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 111}},
            },
        }
        new_token = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 222}},
            },
        }
        prefix = json.dumps({"ignored": "a" * 4096}) + "\n"
        old_suffix = json.dumps({"ignored": "b" * 4096}) + "\n"
        new_suffix = json.dumps({"ignored": "c" * 4096}) + "\n"
        old_content = prefix + json.dumps(old_token) + "\n" + old_suffix
        new_content = (
            prefix
            + json.dumps(new_token)
            + "\n"
            + new_suffix
            + json.dumps({"ignored": "growth"})
            + "\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "middle-rewrite.jsonl"
            path.write_text(old_content)
            first = codex_statusline.latest_token_count(str(path))
            original_inode = path.stat().st_ino

            path.write_text(new_content)
            self.assertEqual(path.stat().st_ino, original_inode)
            refreshed = codex_statusline.latest_token_count(str(path))

        self.assertEqual(first["info"]["total_token_usage"]["total_tokens"], 111)
        self.assertEqual(refreshed["info"]["total_token_usage"]["total_tokens"], 222)

    def test_rollout_state_returns_fresh_state_when_reset_open_fails(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        event = {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"total_tokens": 111}},
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unreadable.jsonl"
            path.write_text(json.dumps(event) + "\n")
            self.assertTrue(codex_statusline.latest_token_count(str(path)))

            path.write_text("")
            with mock.patch.object(
                codex_statusline.Path,
                "open",
                side_effect=OSError("unreadable"),
            ):
                refreshed = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(refreshed.latest_token, {})
        self.assertEqual(refreshed.activity.turns_started, 0)

    def test_rollout_state_replays_when_subagent_boundary_changes(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = [
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "first", "started_at": 10},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "first"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "second", "started_at": 20},
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "shared.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            base = codex_statusline.Thread(
                id="first-child",
                source='{"subagent":{}}',
                rollout_path=str(path),
                created_at=10,
                updated_at=20,
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
            renamed = dataclasses.replace(base, id="second-child")
            later = dataclasses.replace(renamed, created_at=20)
            original_decode = codex_statusline.decode_rollout_line
            decoded_lines = 0

            def counted_decode(line: bytes):
                nonlocal decoded_lines
                decoded_lines += 1
                return original_decode(line)

            with mock.patch.object(
                codex_statusline,
                "decode_rollout_line",
                side_effect=counted_decode,
            ):
                first = codex_statusline.rollout_activity(base, datetime.fromtimestamp(30))
                same_boundary = codex_statusline.rollout_activity(
                    renamed,
                    datetime.fromtimestamp(30),
                )
                later_boundary = codex_statusline.rollout_activity(
                    later,
                    datetime.fromtimestamp(30),
                )
                repeated = codex_statusline.rollout_activity(
                    later,
                    datetime.fromtimestamp(31),
                )

        self.assertEqual(first.turns_started, 2)
        self.assertEqual(first.turns_completed, 1)
        self.assertEqual(same_boundary.turns_started, 2)
        self.assertEqual(later_boundary.turns_started, 1)
        self.assertEqual(later_boundary.turns_completed, 0)
        self.assertEqual(repeated.turns_started, 1)
        self.assertEqual(decoded_lines, len(events) * 3)

    def test_rollout_state_bounds_completed_turn_history(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = []
        for index in range(2000):
            turn_id = f"turn-{index}"
            events.extend(
                [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "turn_id": turn_id,
                            "started_at": index + 1,
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": turn_id},
                    },
                ]
            )
        events.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "open",
                    "started_at": 3000,
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "turns.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            thread = codex_statusline.Thread(
                id="thread",
                source="cli",
                rollout_path=str(path),
                created_at=0,
                updated_at=3000,
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

            activity = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(3010))
            state = codex_statusline.ROLLOUT_STATE_CACHE[str(path)].activity

        self.assertEqual(activity.turns_started, 2001)
        self.assertEqual(activity.turns_completed, 2000)
        self.assertEqual(activity.active_turn_seconds, 10)
        self.assertEqual(state.started_turns, {"open": 3000})
        self.assertEqual(
            len(state.recent_closed_turns),
            codex_statusline.MAX_RECENT_CLOSED_TURN_IDS,
        )
        self.assertNotIn("turn-0", state.recent_closed_turns)
        self.assertIn("turn-1999", state.recent_closed_turns)

    def test_rollout_state_consumes_out_of_order_turn_close(self) -> None:
        state = codex_statusline.new_rollout_state(1, None)

        codex_statusline.update_activity_state(
            state,
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn"},
            },
        )
        self.assertIn("turn", state.activity.recent_closed_turns)

        codex_statusline.update_activity_state(
            state,
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "turn",
                    "started_at": 100,
                },
            },
        )

        self.assertEqual(state.activity.turns_started, 1)
        self.assertEqual(state.activity.turns_completed, 1)
        self.assertEqual(state.activity.started_turns, {})
        self.assertIn("turn", state.activity.recent_closed_turns)

    def test_rollout_state_bounds_twenty_thousand_unmatched_turn_closures(self) -> None:
        state = codex_statusline.new_rollout_state(1, None)
        for index in range(20_000):
            codex_statusline.update_activity_state(
                state,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": f"turn-{index}",
                    },
                },
            )

        self.assertEqual(state.activity.turns_completed, 20_000)
        self.assertEqual(
            len(state.activity.recent_closed_turns),
            codex_statusline.MAX_RECENT_CLOSED_TURN_IDS,
        )
        self.assertNotIn("turn-0", state.activity.recent_closed_turns)
        self.assertIn("turn-19999", state.activity.recent_closed_turns)

    def test_rollout_state_bounds_twenty_thousand_unmatched_turn_starts(self) -> None:
        state = codex_statusline.new_rollout_state(1, None)
        for index in range(20_000):
            codex_statusline.update_activity_state(
                state,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": f"turn-{index}",
                        "started_at": index + 1,
                    },
                },
            )

        self.assertEqual(state.activity.turns_started, 20_000)
        self.assertEqual(
            len(state.activity.started_turns),
            codex_statusline.MAX_OPEN_TURN_IDS,
        )
        self.assertNotIn("turn-0", state.activity.started_turns)
        self.assertIn("turn-19999", state.activity.started_turns)

    def test_rollout_state_bounds_unmatched_tool_and_shell_state(self) -> None:
        state = codex_statusline.new_rollout_state(1, None)
        for index in range(20_000):
            call_id = f"call-{index}"
            codex_statusline.update_activity_state(
                state,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": "exec_command",
                        "arguments": json.dumps({"session_id": f"session-{index}"}),
                    },
                },
            )
            codex_statusline.update_activity_state(
                state,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"missing-{index}",
                        "output": json.dumps({"session_id": f"running-{index}"}),
                    },
                },
            )

        activity = state.activity
        self.assertEqual(len(activity.pending_tools), codex_statusline.MAX_ACTIVE_TOOL_CALLS)
        self.assertEqual(len(activity.tool_sessions), codex_statusline.MAX_ACTIVE_TOOL_CALLS)
        self.assertEqual(len(activity.pending_shells), 0)
        self.assertEqual(len(activity.running_shells), codex_statusline.MAX_RUNNING_SHELLS)
        self.assertNotIn("call-0", activity.pending_tools)
        self.assertIn("call-19999", activity.pending_tools)
        self.assertNotIn("running-0", activity.running_shells)
        self.assertIn("running-19999", activity.running_shells)

    def test_rollout_state_falls_back_from_malformed_started_at(self) -> None:
        root_state = codex_statusline.new_rollout_state(1, None)
        codex_statusline.update_activity_state(
            root_state,
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "root",
                    "started_at": "bad",
                },
            },
        )

        subagent_state = codex_statusline.new_rollout_state(
            1,
            ("thread", True, 100),
        )
        codex_statusline.update_activity_state(
            subagent_state,
            {
                "timestamp": datetime.fromtimestamp(120).astimezone().isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "subagent",
                    "started_at": "bad",
                },
            },
        )

        self.assertEqual(root_state.activity.started_turns, {"root": 0})
        self.assertTrue(subagent_state.activity.boundary_found)
        self.assertEqual(subagent_state.activity.started_turns, {"subagent": 120})

    def test_rollout_state_prunes_token_points_behind_cached_boundary(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = [
            {
                "timestamp": datetime.fromtimestamp(index + 1).astimezone().isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": index + 1}},
                },
            }
            for index in range(5000)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tokens.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            value = codex_statusline.tokens_since_boundary(str(path), 5000, 10_000)
            state = codex_statusline.ROLLOUT_STATE_CACHE[str(path)]

        self.assertEqual(value, 0)
        self.assertEqual(state.token_points, [(5000, 5000)])
        self.assertEqual(len(state.token_baselines), 1)

    def test_rollout_state_bounds_post_boundary_tokens_and_replays_old_boundary(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        events = [
            {
                "timestamp": datetime.fromtimestamp(index).astimezone().isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": index}},
                },
            }
            for index in range(1, 10_001)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bounded-tokens.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            recent = codex_statusline.tokens_since_boundary(str(path), 10_000, 5_000)
            old = codex_statusline.tokens_since_boundary(str(path), 10_000, 100)
            state = codex_statusline.ROLLOUT_STATE_CACHE[str(path)]

        self.assertEqual(recent, 5_001)
        self.assertEqual(old, 9_901)
        self.assertLessEqual(
            len(state.token_points),
            codex_statusline.MAX_TOKEN_POINTS,
        )
        self.assertEqual(len(state.token_baselines), 2)

    def test_rollout_state_does_not_retain_oversized_valid_partial(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        payload = (
            b'{"timestamp":"2026-07-29T12:00:00Z","type":"compacted",'
            b'"payload":{"text":"' + b"x" * (2 * 1024 * 1024) + b'"}}'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "valid-partial.jsonl"
            path.write_bytes(payload)
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

            first = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(1))
            repeated = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(2))
            with path.open("ab") as stream:
                stream.write(b"\n")
            completed = codex_statusline.rollout_activity(thread, datetime.fromtimestamp(3))
            state = codex_statusline.ROLLOUT_STATE_CACHE[str(path)]

        self.assertEqual(first.compactions, 1)
        self.assertEqual(repeated.compactions, 1)
        self.assertEqual(completed.compactions, 1)
        self.assertLess(len(state.pending.buffer), 1024)

    def test_rollout_state_resets_when_applied_partial_content_grows(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        payload = (
            b'{"timestamp":"2026-07-29T12:00:00Z","type":"compacted",'
            b'"payload":{"text":"x"}}'
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "changed-valid-partial.jsonl"
            path.write_bytes(payload)
            first = codex_statusline.read_rollout_state(str(path))

            with path.open("ab") as stream:
                stream.write(b" ")
            refreshed = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(first.activity.compactions, 1)
        self.assertEqual(refreshed.activity.compactions, 1)
        self.assertIs(first, refreshed)

    def test_rollout_state_continues_oversized_partial_only_after_growth(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        payload = (
            b'{"timestamp":"2026-07-29T12:00:00Z","type":"compacted",'
            b'"payload":{"text":"' + b"x" * (2 * 1024 * 1024) + b'"}'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "incomplete-partial.jsonl"
            path.write_bytes(payload)
            self.assertEqual(codex_statusline.latest_token_count(str(path)), {})
            first_state = codex_statusline.ROLLOUT_STATE_CACHE[str(path)]
            self.assertEqual(codex_statusline.latest_token_count(str(path)), {})
            with path.open("ab") as stream:
                stream.write(b"}")
            codex_statusline.latest_token_count(str(path))
            state = codex_statusline.ROLLOUT_STATE_CACHE[str(path)]

        self.assertEqual(state.activity.compactions, 1)
        self.assertIs(first_state, state)
        self.assertLess(len(state.pending.buffer), 1024)

    def test_rollout_state_replays_invalid_continuation_after_applied_partial(self) -> None:
        codex_statusline.ROLLOUT_STATE_CACHE.clear()
        payload = (
            b'{"timestamp":"2026-07-29T12:00:00Z","type":"compacted",'
            b'"payload":{"text":"' + b"x" * (2 * 1024 * 1024) + b'"}}'
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "invalid-continuation.jsonl"
            path.write_bytes(payload)
            applied = codex_statusline.read_rollout_state(str(path))
            with path.open("ab") as stream:
                stream.write(b"x\n")
            replayed = codex_statusline.read_rollout_state(str(path))

        self.assertEqual(applied.activity.compactions, 1)
        self.assertEqual(replayed.activity.compactions, 0)
        self.assertIsNot(applied, replayed)

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
                ("launcher-root", "cli", str(root_rollout), 100),
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

        self.assertEqual(selected, "launcher-root")

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
            assert child.stdout is not None
            stdout = child.stdout
            try:
                self.assertEqual(stdout.readline().strip(), "ready")
                found = codex_statusline.process_rollout_paths(os.getpid())
            finally:
                child.kill()
                child.wait()
                stdout.close()
            self.assertTrue(stdout.closed)

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
            "fresh": {"primary": {}, "secondary": {}, "credits": {}},
            "limited": {
                "primary": {},
                "secondary": {},
                "credits": {"balance": "2500.0000000000"},
            },
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

    def test_multi_session_renderers_show_credits_below_weekly(self) -> None:
        data = {
            "account": "andrew@example.com",
            "sessions": [],
            "rate_limits": {
                "primary": {
                    "used_percent": 100.0,
                    "window_minutes": 10_080,
                },
                "secondary": {},
                "credits": {"balance": "2089.4740750000"},
            },
        }

        rendered = codex_statusline.render_all_sessions(
            data, 100, codex_statusline.Palette(False)
        ).splitlines()
        weekly_index = next(i for i, line in enumerate(rendered) if "weekly" in line)
        self.assertEqual(rendered[weekly_index + 1], "        credits 2,089 remaining")

        args = codex_statusline.parse_args(["--top", "--width", "100"])
        rendered = codex_statusline.render_top(
            data, args, codex_statusline.Palette(False)
        ).splitlines()
        weekly_index = next(i for i, line in enumerate(rendered) if line.startswith("wk "))
        self.assertEqual(rendered[weekly_index + 1], "credits 2,089 remaining")

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

    def test_descendant_activity_summary_skips_stale_rollouts_but_keeps_total(self) -> None:
        base = codex_statusline.Thread(
            id="recent",
            source='{"subagent":{}}',
            rollout_path="/tmp/recent.jsonl",
            created_at=900,
            updated_at=950,
            cwd="/tmp",
            title="",
            tokens_used=0,
            model="",
            reasoning_effort="",
            sandbox_policy="",
            approval_mode="",
            git_branch="",
            archived=0,
            agent_path="/root/release_train/solei_local",
        )
        stale = dataclasses.replace(base, id="stale", rollout_path="/tmp/stale.jsonl", updated_at=100)
        activity = codex_statusline.RolloutActivity(
            turns_started=1,
            turns_completed=0,
            turns_aborted=0,
            compactions=0,
            tool_calls=1,
            shell_calls=1,
            patch_calls=0,
            active_tools=1,
            active_shells=1,
            active_turn_seconds=50,
            last_event="function_call",
            last_command="sleep 30",
            last_user_message="-",
            last_agent_message="-",
            active_tool="exec",
            last_tool="exec",
        )

        with mock.patch.object(
            codex_statusline,
            "rollout_activity",
            return_value=activity,
        ) as rollout_activity:
            summary = codex_statusline.descendant_activity_summary(
                [base, stale],
                datetime.fromtimestamp(1000),
                100,
            )

        rollout_activity.assert_called_once_with(base, datetime.fromtimestamp(1000), 100)
        self.assertEqual(
            summary,
            {
                "total": 2,
                "active": 1,
                "active_tools": 1,
                "active_shells": 1,
                "running": ["release-train solei-local"],
            },
        )

    def test_render_footer_shows_live_limits_and_workers_within_width(self) -> None:
        data = {
            "model_display": "GPT-5.6",
            "reasoning_effort": "max",
            "session_age_seconds": 180,
            "account": "andrew@example.com",
            "repo": "statusline",
            "branch": "feat/codex-top",
            "pull_request": None,
            "context_window": 1000,
            "tokens": {"today": 4000, "week": 6000, "lifetime": 9000},
            "usage": {
                "context_window": 1000,
                "context_used": 450,
                "session_total": 5000,
                "rate_limits": {
                    "primary": {"used_percent": 67.0, "resets_at": 1_900_000_000},
                    "secondary": {
                        "used_percent": 24.0,
                        "window_minutes": 10_080,
                        "resets_at": 1_900_604_800,
                    },
                },
            },
            "activity": {"active_tools": 1, "active_shells": 1},
            "agents": {
                "total": 3,
                "active": 1,
                "active_tools": 2,
                "active_shells": 2,
                "running": ["release-train solei-local"],
            },
            "sandbox": "disabled",
            "approval_mode": "never",
        }
        board = {
            "current_label": "andrew",
            "mode": "auto",
            "selected": "personal",
            "rows": [
                {
                    "label": "andrew",
                    "weekly": {"used_percent": 67.0, "resets_at": 1_900_000_000},
                },
                {
                    "label": "personal",
                    "weekly": {"used_percent": 13.0, "resets_at": 1_900_604_800},
                },
            ],
        }

        with mock.patch.object(codex_statusline, "codex_account_board", return_value=board):
            rendered = codex_statusline.render_footer(data, 80, codex_statusline.Palette(False))

        self.assertIn("model   GPT-5.6 · max", rendered)
        self.assertIn("account andrew · auto → personal", rendered)
        self.assertIn("repo    statusline", rendered)
        self.assertIn("branch  feat/codex-top", rendered)
        self.assertIn("  context ●●●●●●○○○○○○○○○ 45%", rendered)
        self.assertIn("  weekly  ●●●○○○○○○○○○○○○ 24%", rendered)
        self.assertNotIn("450/1.0k", rendered)
        self.assertNotIn("5-hour", rendered)
        self.assertIn("usage   today 4.0k · session 5.0k · lifetime 9.0k", rendered)
        self.assertIn("acct", rendered)
        self.assertIn("* andrew", rendered)
        self.assertIn("· personal", rendered)
        self.assertIn("mode    ⏵⏵ bypass permissions on", rendered)
        lines = rendered.splitlines()
        account_header = next(line for line in lines if line.strip().startswith("acct"))
        account_row = next(line for line in lines if line.startswith("  * andrew"))
        reset_text = codex_statusline.limit_display(board["rows"][0]["weekly"])[1]
        reset_value = reset_text.removeprefix("resets ")
        self.assertEqual(account_header.index("week") + 4, account_row.index("67%") + 3)
        self.assertEqual(account_header.index("reset"), account_row.index(reset_value))
        self.assertNotIn("resets", account_row)
        self.assertNotIn("left", account_header)
        self.assertNotIn("33%", account_row)
        self.assertEqual(rendered.splitlines()[-1], "◯ release-train solei-local 0/1 agents done")
        expected_labels = [
            "model",
            "time",
            "account",
            "repo",
            "branch",
            "context",
            "weekly",
            "usage",
            "mode",
        ]
        self.assertEqual(
            [line.strip().split(maxsplit=1)[0] for line in lines[: len(expected_labels)]],
            expected_labels,
        )
        self.assertTrue(all(len(line) <= 80 for line in rendered.splitlines()))

        palette = codex_statusline.Palette(True)
        with mock.patch.object(codex_statusline, "codex_account_board", return_value=board):
            colored = codex_statusline.render_footer(data, 80, palette)
        self.assertIn(
            f"  {palette.white}{'model':<7}{palette.reset} "
            f"{palette.blue}GPT-5.6{palette.reset}{palette.red} · max{palette.reset}",
            colored,
        )
        self.assertIn(
            f"  {palette.white}{'account':<7}{palette.reset} "
            f"{palette.orange}andrew · auto → personal{palette.reset}",
            colored,
        )

        unavailable = {
            **data,
            "pull_request": None,
            "agents": {"running": []},
            "usage": {
                **data["usage"],
                "context_window": 0,
                "rate_limits": {},
            },
        }
        with mock.patch.object(codex_statusline, "codex_account_board", return_value={"rows": []}):
            unavailable_lines = codex_statusline.render_footer(
                unavailable, 80, codex_statusline.Palette(False)
            ).splitlines()
        self.assertEqual(
            [line.strip().split(maxsplit=1)[0] for line in unavailable_lines],
            expected_labels,
        )
        self.assertEqual(
            next(line for line in unavailable_lines if line.startswith("  context")),
            "  context -",
        )
        self.assertEqual(
            next(line for line in unavailable_lines if line.startswith("  weekly")),
            "  weekly  -",
        )
        self.assertEqual(unavailable_lines[-1], "  mode    ⏵⏵ bypass permissions on")

        with mock.patch.object(codex_statusline, "codex_account_board", return_value=board):
            wide_lines = codex_statusline.render_footer(data, 140, codex_statusline.Palette(False)).splitlines()
        self.assertTrue(wide_lines[0].startswith("  model"))
        self.assertFalse(any(line.startswith("─") for line in wide_lines))

        with mock.patch.object(codex_statusline, "codex_account_board", return_value=board):
            narrow_lines = codex_statusline.render_footer(data, 40, codex_statusline.Palette(False)).splitlines()
        self.assertTrue(all(len(line) <= 40 for line in narrow_lines))
        with mock.patch.object(codex_statusline, "codex_account_board", return_value=board):
            tiny_lines = codex_statusline.render_footer(data, 8, codex_statusline.Palette(False)).splitlines()
        self.assertTrue(all(len(line) <= 8 for line in tiny_lines))

    def test_footer_shows_weekly_without_reset_while_default_keeps_it(self) -> None:
        data = {
            "model_display": "GPT-5.6",
            "reasoning_effort": "max",
            "session_age_seconds": 180,
            "account": "andrew@example.com",
            "repo": "statusline",
            "branch": "main",
            "context_window": 0,
            "tokens": {"today": 0, "week": 0, "lifetime": 0},
            "usage": {
                "context_window": 0,
                "session_total": 0,
                "rate_limits": {
                    "primary": {
                        "used_percent": 96.0,
                        "window_minutes": 10_080,
                        "resets_at": 1_900_000_000,
                    },
                    "secondary": {},
                    "credits": {
                        "balance": "2089.4740750000",
                        "has_credits": True,
                        "unlimited": False,
                    },
                },
            },
            "activity": {"active_tools": 0, "active_shells": 0},
        }

        with mock.patch.object(codex_statusline, "codex_account_board", return_value={"rows": []}):
            rendered = codex_statusline.render_footer(data, 100, codex_statusline.Palette(False))

        weekly_line = next(line for line in rendered.splitlines() if line.startswith("  weekly"))
        self.assertEqual(weekly_line, "  weekly  ●●●●●●●●●●●●●●○ 96%")
        self.assertNotIn("reset", weekly_line)
        self.assertNotIn("5-hour", rendered)
        lines = rendered.splitlines()
        self.assertEqual(len(lines), 9)
        self.assertIn("  usage   today 0 · session 0 · lifetime 0", lines)

        data.update(
            {
                "idle_seconds": 0,
                "sandbox": "disabled",
                "approval_mode": "never",
            }
        )
        data["usage"].update(
            {
                "context_used": 0,
                "turn_total": 0,
                "turn_cached": 0,
                "session_output": 0,
                "session_reasoning": 0,
            }
        )
        data["activity"].update(
            {
                "turns_completed": 0,
                "turns_started": 0,
                "turns_aborted": 0,
                "compactions": 0,
                "tool_calls": 0,
                "shell_calls": 0,
                "patch_calls": 0,
                "last_event": "-",
                "last_command": "-",
                "last_user_message": "-",
                "active_turn_seconds": 0,
            }
        )
        rendered = codex_statusline.render_default(
            data, 100, codex_statusline.Palette(False)
        ).splitlines()
        weekly_index = next(i for i, line in enumerate(rendered) if "weekly" in line)
        self.assertEqual(rendered[weekly_index + 1], "        credits 2,089 remaining")

    def test_codex_account_board_maps_email_and_reports_weekly_headroom(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": {
                            "work": {"email": "dev@example.invalid", "home": "/tmp/work"},
                            "personal": {"email": "me@example.invalid", "home": "/tmp/personal"},
                        }
                    }
                )
            )
            (root / "mode.json").write_text('{"mode":"auto"}')
            (root / "usage.json").write_text(
                json.dumps(
                    {
                        "work": {
                            "fetched_at": time.time(),
                            "rate_limits": {
                                "primary": {"used_percent": 40, "window_duration_mins": 10_080}
                            },
                        },
                        "personal": {
                            "fetched_at": time.time(),
                            "rate_limits": {
                                "primary": {"used_percent": 0, "window_duration_mins": 10_080}
                            },
                        },
                    }
                )
            )

            with mock.patch.dict(os.environ, {"CODEX_ACCOUNTS_HOME": str(root)}):
                board = codex_statusline.codex_account_board("dev@example.invalid")

        self.assertEqual(board["current_label"], "work")
        self.assertEqual(board["mode"], "auto")
        self.assertEqual(board["selected"], "personal")
        self.assertEqual(board["rows"][0]["label"], "work")
        self.assertEqual(board["rows"][0]["weekly"]["used_percent"], 40)

    def test_credit_balance_text_handles_unlimited_and_invalid_balances(self) -> None:
        self.assertEqual(
            codex_statusline.credit_balance_text({"credits": {"balance": "0"}}),
            "0",
        )
        self.assertEqual(
            codex_statusline.credit_balance_text({"credits": {"unlimited": True}}),
            "unlimited",
        )
        for balance in (None, "not-a-number", "NaN", "Infinity"):
            with self.subTest(balance=balance):
                self.assertEqual(
                    codex_statusline.credit_balance_text(
                        {"credits": {"balance": balance}}
                    ),
                    "",
                )

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

    def test_live_state_ignores_stale_agent_metrics_quota(self) -> None:
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
                                    "used_percent": 99.0,
                                    "window_minutes": 300,
                                }
                            }
                        }
                    }
                )
            )
            metrics = home / "metrics.sqlite3"
            connection = sqlite3.connect(metrics)
            connection.execute(
                "create table events(provider text, event_kind text, tool_name text, "
                "quota_window_minutes integer, quota_used_percent real, quota_resets_at integer, "
                "timestamp integer, session_id text)"
            )
            connection.execute(
                "insert into events values('codex','quota','primary',10080,44,1788272012000,1,'session')"
            )
            connection.commit()
            connection.close()

            result = subprocess.run(
                [sys.executable, str(live_state), "--render"],
                text=True,
                capture_output=True,
                check=True,
                env={**os.environ, "HOME": str(home), "AGENT_METRICS_DB": str(metrics)},
            )

        self.assertIn("codex 5h: 99%", result.stdout)
        self.assertNotIn("codex 7d: 44%", result.stdout)

    def test_live_state_prefers_fresh_agent_metrics_quota(self) -> None:
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
                                    "used_percent": 99.0,
                                    "window_minutes": 300,
                                }
                            }
                        }
                    }
                )
            )
            metrics = home / "metrics.sqlite3"
            connection = sqlite3.connect(metrics)
            connection.execute(
                "create table events(provider text, event_kind text, tool_name text, "
                "quota_window_minutes integer, quota_used_percent real, quota_resets_at integer, "
                "timestamp integer, session_id text)"
            )
            connection.execute(
                "insert into events values('codex','quota','primary',10080,44,1788272012000,?,"
                "'session')",
                (int(time.time() * 1000),),
            )
            connection.commit()
            connection.close()

            result = subprocess.run(
                [sys.executable, str(live_state), "--render"],
                text=True,
                capture_output=True,
                check=True,
                env={**os.environ, "HOME": str(home), "AGENT_METRICS_DB": str(metrics)},
            )

        self.assertIn("codex 7d: 44%", result.stdout)
        self.assertNotIn("codex 5h: 99%", result.stdout)

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

    def test_footer_watch_never_resizes_the_codex_pane(self) -> None:
        args = codex_statusline.parse_args(["--footer", "--watch", "1"])
        body = "model\ntime\naccount\nrepo\ncontext\nweekly\ncredits\nusage\nagents\nmode"

        with (
            mock.patch.dict(os.environ, {"TMUX_PANE": "%42"}),
            mock.patch.object(codex_statusline, "terminal_size", return_value=os.terminal_size((137, 12))),
            mock.patch.object(codex_statusline, "snapshot", return_value={}),
            mock.patch.object(codex_statusline, "render", return_value=body),
            mock.patch.object(codex_statusline.subprocess, "run") as run,
            mock.patch.object(codex_statusline.time, "sleep", side_effect=RuntimeError("stop")),
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            codex_statusline.watch_loop(args, codex_statusline.Palette(False))

        run.assert_not_called()

    def test_footer_render_does_not_scroll_past_mode(self) -> None:
        args = codex_statusline.parse_args(["--footer", "--watch", "1"])
        output = io.StringIO()

        with (
            mock.patch.object(codex_statusline, "snapshot", return_value={}),
            mock.patch.object(codex_statusline, "render", return_value="model\nmode"),
            mock.patch.object(codex_statusline.time, "sleep", side_effect=RuntimeError("stop")),
            mock.patch.object(codex_statusline.sys, "stdout", output),
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            codex_statusline.watch_loop(args, codex_statusline.Palette(False))

        self.assertTrue(output.getvalue().endswith("mode"))

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

    def test_watch_stops_when_the_owner_file_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "owner.pid"
            pid_file.write_text(str(os.getpid()))
            args = codex_statusline.parse_args(
                ["--footer", "--watch", "1", "--owner-pid-file", str(pid_file)]
            )
            snapshots = mock.Mock(return_value={})

            def remove_owner(_data, _args, _palette):
                pid_file.unlink()
                return "status"

            with (
                mock.patch.object(codex_statusline, "snapshot", snapshots),
                mock.patch.object(codex_statusline, "render", side_effect=remove_owner),
                mock.patch.object(
                    codex_statusline.time,
                    "sleep",
                    side_effect=[None, KeyboardInterrupt],
                ),
            ):
                self.assertEqual(
                    codex_statusline.watch(args, codex_statusline.Palette(False)),
                    0,
                )

        snapshots.assert_called_once()

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
        launcher = MODULE_PATH.with_name("codex-statusline")
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
                    "CODEX_STATUSLINE_CODEX_BIN": str(fake_codex),
                    "CODEX_STATUSLINE_NATIVE": "0",
                    "PATH": f"{tmp}:{env['PATH']}",
                    "TMUX": "test",
                }
            )

            result = subprocess.run(
                [str(launcher), "--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-test"],
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

            subprocess.run([str(launcher)], check=True, env=env)
            self.assertEqual(
                capture.read_text().splitlines(),
                ["-c", "tui.status_line=[]", "--dangerously-bypass-approvals-and-sandbox"],
            )

            subprocess.run([str(launcher), "--sandbox", "read-only"], check=True, env=env)
            self.assertEqual(
                capture.read_text().splitlines(),
                ["-c", "tui.status_line=[]", "--sandbox", "read-only"],
            )

            subprocess.run([str(launcher), "--yolo"], check=True, env=env)
            self.assertEqual(
                capture.read_text().splitlines(),
                ["-c", "tui.status_line=[]", "--yolo"],
            )

            for permissions in (("-sread-only",), ("-s=read-only",), ("-anever",), ("-a=never",)):
                with self.subTest(permissions=permissions):
                    subprocess.run([str(launcher), *permissions], check=True, env=env)
                    captured = capture.read_text().splitlines()
                    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", captured)
                    self.assertEqual(captured[-1], permissions[0])

            subprocess.run([str(launcher), "--", "-summarize"], check=True, env=env)
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

    def test_launcher_native_mode_uses_copy_friendly_statusline(self) -> None:
        launcher = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            capture = tmp / "args"
            fake_codex = tmp / "codex"
            fake_tmux = tmp / "tmux"
            fake_codex.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$CAPTURE"\n')
            fake_tmux.write_text("#!/usr/bin/env bash\nexit 99\n")
            fake_codex.chmod(0o755)
            fake_tmux.chmod(0o755)
            env = {
                **os.environ,
                "CAPTURE": str(capture),
                "CODEX_STATUSLINE_CODEX_BIN": str(fake_codex),
                "CODEX_STATUSLINE_HEIGHT": "1",
                "CODEX_STATUSLINE_HISTORY_LIMIT": "0",
                "CODEX_STATUSLINE_INTERVAL": "0",
                "CODEX_STATUSLINE_MANAGE_APPROVALS": "0",
                "CODEX_STATUSLINE_NATIVE": "1",
                "PATH": f"{tmp}:{os.environ['PATH']}",
            }

            subprocess.run([str(launcher), "--model", "gpt-test"], check=True, env=env)

            self.assertEqual(
                capture.read_text().splitlines(),
                [
                    "--no-alt-screen",
                    "-c",
                    'tui.status_line=["model-with-reasoning","project-name","git-branch","context-used","weekly-limit","used-tokens","permissions","approval-mode","task-progress"]',
                    "-c",
                    "tui.status_line_use_colors=true",
                    "--model",
                    "gpt-test",
                ],
            )

    def test_launcher_loads_config_file(self) -> None:
        launcher = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            codex_home = tmp / ".codex"
            capture = tmp / "capture"
            fake_codex = tmp / "codex"
            fake_tmux = tmp / "tmux"
            codex_home.mkdir()
            (codex_home / "statusline.conf").write_text(
                "CODEX_STATUSLINE_INTERVAL=3\n"
                "CODEX_STATUSLINE_MANAGE_APPROVALS=0\n"
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
                "CODEX_STATUSLINE_HEIGHT",
                "CODEX_STATUSLINE_INTERVAL",
                "CODEX_STATUSLINE_MANAGE_APPROVALS",
                "CODEX_STATUSLINE_NATIVE",
            ):
                env.pop(name, None)
            env.update(
                {
                    "CAPTURE": str(capture),
                    "CODEX_STATUSLINE_CODEX_BIN": str(fake_codex),
                    "CODEX_HOME": str(codex_home),
                    "PATH": f"{tmp}:{env['PATH']}",
                    "TMUX": "test",
                }
            )

            subprocess.run([str(launcher)], check=True, env=env)

            captured = capture.read_text().splitlines()
            split_window = next(line for line in captured if line.startswith("split-window "))
            self.assertIn("-l 14", split_window)
            self.assertIn("--watch 3", split_window)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", captured)
            self.assertIn("set-option mouse off", captured)
            self.assertIn("set-option -w history-limit 100000", captured)

            subprocess.run(
                [str(launcher), "resume", "-s", "read-only", "019f0000-0000-7000-8000-000000000000"],
                check=True,
                env=env,
            )
            latest_split = [
                line for line in capture.read_text().splitlines() if line.startswith("split-window ")
            ][-1]
            self.assertNotIn("--bind-after-ms", latest_split)
            self.assertNotIn("--thread-id", latest_split)

    def test_launcher_inside_tmux_owns_indexed_resize_hooks(self) -> None:
        launcher = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            capture = tmp / "tmux-args"
            fake_codex = tmp / "codex"
            fake_tmux = tmp / "tmux"
            fake_codex.write_text("#!/usr/bin/env bash\nexit 0\n")
            fake_tmux.write_text(
                '#!/usr/bin/env bash\n'
                'printf "%s\\n" "$*" >> "$CAPTURE"\n'
                'if [[ "$1" == "display-message" && "$*" == *"#{session_id}"* ]]; then\n'
                '    printf "@9\\n"\n'
                'elif [[ "$1" == "split-window" ]]; then\n'
                '    printf "%%1\\n"\n'
                'elif [[ "$1" == "show-hooks" ]]; then\n'
                '    case "${@: -1}" in\n'
                '        client-attached) printf "client-attached\\n" ;;\n'
                '        client-resized) printf "client-resized[42] keep-indexed\\n" ;;\n'
                '        window-resized) printf "window-resized[0] keep-unindexed\\n" ;;\n'
                '    esac\n'
                'fi\n'
            )
            fake_codex.chmod(0o755)
            fake_tmux.chmod(0o755)
            env = {
                **os.environ,
                "CAPTURE": str(capture),
                "CODEX_STATUSLINE_CODEX_BIN": str(fake_codex),
                "CODEX_STATUSLINE_NATIVE": "0",
                "PATH": f"{tmp}:{os.environ['PATH']}",
                "TMUX": "test",
            }

            subprocess.run([str(launcher)], check=True, env=env)

            commands = capture.read_text().splitlines()
            hook_sets = [
                line
                for line in commands
                if line.startswith("set-hook -t @9 ") and "resize-pane -t '%1' -y 14" in line
            ]
            self.assertEqual(len(hook_sets), 3)
            hook_names = {
                match.group(1): match.group(2)
                for line in hook_sets
                if (match := re.search(r"(client-attached|client-resized|window-resized)\[([0-9]+)\]", line))
            }
            self.assertEqual(set(hook_names), {"client-attached", "client-resized", "window-resized"})
            self.assertEqual(len(set(hook_names.values())), 1)
            hook_index = next(iter(hook_names.values()))
            self.assertNotEqual(hook_index, "42")
            self.assertEqual(
                {
                    line
                    for line in commands
                    if line.startswith("set-hook -u -t @9 ")
                },
                {
                    f"set-hook -u -t @9 client-attached[{hook_index}]",
                    f"set-hook -u -t @9 client-resized[{hook_index}]",
                    f"set-hook -u -t @9 window-resized[{hook_index}]",
                    "set-hook -u -t @9 client-attached",
                },
            )
            self.assertNotIn("set-hook -u -t @9 client-resized", commands)
            self.assertNotIn("set-hook -u -t @9 window-resized", commands)
            self.assertEqual(
                {
                    line for line in commands if line.startswith("show-hooks -t @9 ")
                },
                {
                    "show-hooks -t @9 client-attached",
                    "show-hooks -t @9 client-resized",
                    "show-hooks -t @9 window-resized",
                },
            )

    def test_launcher_rejects_zero_interval_and_short_footer(self) -> None:
        launcher = MODULE_PATH.with_name("codex-statusline")
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
                    "CODEX_STATUSLINE_CODEX_BIN": str(fake_codex),
                    "CODEX_STATUSLINE_NATIVE": "0",
                    "PATH": f"{tmp}:{base_env['PATH']}",
                    "TMUX": "test",
                }
            )
            cases = (
                ({"CODEX_STATUSLINE_INTERVAL": "0"}, "positive number"),
                ({"CODEX_STATUSLINE_HEIGHT": "10"}, "at least 11"),
                ({"CODEX_STATUSLINE_HEIGHT": "08"}, "at least 11"),
                ({"CODEX_STATUSLINE_HISTORY_LIMIT": "0"}, "positive integer"),
            )

            for overrides, expected in cases:
                with self.subTest(overrides=overrides):
                    env = {**base_env, **overrides}
                    result = subprocess.run(
                        [str(launcher)],
                        capture_output=True,
                        env=env,
                        text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stderr)

    def test_launcher_sizes_detached_session_before_split(self) -> None:
        launcher = MODULE_PATH.with_name("codex-statusline")
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
                    "CODEX_STATUSLINE_CODEX_BIN": str(fake_codex),
                    "CODEX_STATUSLINE_NATIVE": "0",
                    "COLUMNS": "117",
                    "LINES": "83",
                    "PATH": f"{tmp}:{env['PATH']}",
                    "TMPDIR": str(tmp),
                }
            )
            env.pop("TMUX", None)

            subprocess.run([str(launcher), f"-C={project}"], check=True, env=env)

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
            self.assertIn("-l 14", split_window)
            resize_hooks = {
                line
                for line in capture.read_text().splitlines()
                if line.startswith("set-hook ")
            }
            self.assertEqual(
                resize_hooks,
                {
                    next(line for line in resize_hooks if "client-attached" in line),
                    next(line for line in resize_hooks if "client-resized" in line),
                    next(line for line in resize_hooks if "window-resized" in line),
                },
            )
            self.assertTrue(all("resize-pane -t '%1' -y 14" in line for line in resize_hooks))
            self.assertIn("mouse off", "\n".join(capture.read_text().splitlines()))
            self.assertIn("history-limit 100000", "\n".join(capture.read_text().splitlines()))

    def launcher_detached_session_env(self, tmp: Path, capture: Path) -> dict[str, str]:
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
                "CODEX_STATUSLINE_CODEX_BIN": str(fake_codex),
                "CODEX_STATUSLINE_NATIVE": "0",
                "COLUMNS": "100",
                "LINES": "40",
                "PATH": f"{tmp}:{env['PATH']}",
                "TMPDIR": str(tmp),
            }
        )
        env.pop("TMUX", None)
        return env

    def test_launcher_detach_leaves_running_session_alive(self) -> None:
        launcher = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            capture = tmp / "tmux-args"
            env = self.launcher_detached_session_env(tmp, capture)
            env["FAKE_HAS_SESSION"] = "0"

            result = subprocess.run(
                [str(launcher)],
                capture_output=True,
                check=True,
                env=env,
                text=True,
            )

            self.assertIn("Reattach with: tmux attach -t '=codex-statusline-", result.stdout)
            self.assertNotIn("kill-session", capture.read_text())
            self.assertEqual(len(list(tmp.glob("codex-statusline.*"))), 1)

    def test_launcher_cleans_up_when_session_ends_without_status(self) -> None:
        launcher = MODULE_PATH.with_name("codex-statusline")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            capture = tmp / "tmux-args"
            env = self.launcher_detached_session_env(tmp, capture)
            env["FAKE_HAS_SESSION"] = "1"

            result = subprocess.run(
                [str(launcher)],
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
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )

            subprocess.run([str(installer)], check=True, env=env)

            self.assertTrue((codex_home / "statusline.conf").is_file())
            self.assertIn(
                "CODEX_STATUSLINE_NATIVE=0",
                (codex_home / "statusline.conf").read_text(),
            )
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
