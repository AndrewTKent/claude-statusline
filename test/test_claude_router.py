import importlib.util
import json
import os
import select
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))
SPEC = importlib.util.spec_from_file_location(
    "claude_router",
    REPO / "bin" / "claude-router.py",
)
assert SPEC and SPEC.loader
claude_router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(claude_router)


def read_exact(fd, size):
    data = bytearray()
    deadline = time.monotonic() + 1
    while len(data) < size:
        timeout = deadline - time.monotonic()
        assert timeout > 0
        ready, _, _ = select.select([fd], [], [], timeout)
        assert ready
        chunk = os.read(fd, size - len(data))
        assert chunk
        data.extend(chunk)
    return bytes(data)


def test_router_uses_native_binary_when_path_points_to_the_launcher(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    native = home / ".local/bin/claude"
    launcher_dir = tmp_path / "launcher"
    native.parent.mkdir(parents=True)
    launcher_dir.mkdir()
    native.write_text("")
    (launcher_dir / "claude").write_text("")
    native.chmod(0o755)
    (launcher_dir / "claude").chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(launcher_dir))
    monkeypatch.delenv("CLAUDE_REAL_BIN", raising=False)

    assert claude_router.claude_binary() == str(native)


def test_supervised_launcher_executes_the_router(tmp_path):
    home = tmp_path / "home"
    router = home / ".local/bin/claude-router"
    output = tmp_path / "args.json"
    router.parent.mkdir(parents=True)
    router.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {output}\n"
    )
    router.chmod(0o755)

    result = subprocess.run(
        [REPO / "shell/claude-supervised", "--resume", "session-id"],
        env={**os.environ, "HOME": str(home)},
        check=False,
    )

    assert result.returncode == 0
    assert output.read_text().splitlines() == ["--resume", "session-id"]


def _blob(token):
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": f"refresh-{token}",
                "expiresAt": 3_000_000_000_000,
                "refreshTokenExpiresAt": 3_000_000_000_000,
            }
        }
    )


def test_new_session_gets_a_stable_session_id():
    args, session_id = claude_router.initial_session_args(
        ["--dangerously-skip-permissions"]
    )

    assert args[-2:] == ["--session-id", session_id]
    assert str(uuid.UUID(session_id)) == session_id


def test_interactive_session_defaults_to_ultracode():
    assert claude_router.with_default_effort(
        ["--dangerously-skip-permissions"]
    ) == [
        "--dangerously-skip-permissions",
        "--effort",
        "ultracode",
    ]


@pytest.mark.parametrize(
    "args",
    [
        ["--effort", "max"],
        ["--effort=high"],
    ],
)
def test_explicit_effort_overrides_the_ultracode_default(args):
    assert claude_router.with_default_effort(args) == args


def test_default_session_name_is_invisible_but_not_trimmed_empty():
    args = claude_router.with_default_session_name(["--resume", "session-id"])

    assert claude_router.option_value(args, "--name") == "\u200b"


def test_legacy_invisible_session_name_is_replaced():
    args = claude_router.with_default_session_name(
        ["--resume", "session-id", "--name", "\u2063"]
    )

    assert claude_router.option_value(args, "--name") == "\u200b"


def test_explicit_session_name_is_preserved():
    args = ["--name", "incident-review"]

    assert claude_router.with_default_session_name(args) == args


def test_explicit_short_session_name_is_preserved():
    args = ["-n", "incident-review"]

    assert claude_router.with_default_session_name(args) == args


def test_handoff_resumes_exact_session_without_reforking():
    session_id = str(uuid.uuid4())

    args = claude_router.resume_session_args(
        [
            "--dangerously-skip-permissions",
            "--resume",
            str(uuid.uuid4()),
            "--fork-session",
            "--model",
            "fable",
        ],
        session_id,
    )

    assert args == [
        "--dangerously-skip-permissions",
        "--model",
        "fable",
        "--resume",
        session_id,
    ]


@pytest.mark.parametrize("selector", ["-r", "--resume", "--from-pr"])
def test_handoff_preserves_flags_after_a_bare_optional_selector(selector):
    session_id = str(uuid.uuid4())
    launch_args = claude_router.with_default_session_name(
        claude_router.with_default_effort([selector])
    )

    args = claude_router.resume_session_args(
        launch_args,
        session_id,
        effort_override="xhigh",
    )

    assert args == [
        "--name",
        "\u200b",
        "--effort",
        "xhigh",
        "--resume",
        session_id,
    ]


@pytest.mark.parametrize(
    ("selector", "value"),
    [
        ("-r", "named-session"),
        ("--resume", "named-session"),
        ("--from-pr", "123"),
    ],
)
def test_handoff_strips_valued_optional_selectors_without_losing_flags(
    selector,
    value,
):
    session_id = str(uuid.uuid4())

    args = claude_router.resume_session_args(
        [
            selector,
            value,
            "--effort",
            "ultracode",
            "--name",
            "incident",
        ],
        session_id,
        effort_override="xhigh",
    )

    assert args == [
        "--name",
        "incident",
        "--effort",
        "xhigh",
        "--resume",
        session_id,
    ]


def test_fable_handoff_can_resume_on_opus():
    session_id = str(uuid.uuid4())

    args = claude_router.resume_session_args(
        ["--model", "fable", "--dangerously-skip-permissions"],
        session_id,
        "opus",
    )

    assert args == [
        "--dangerously-skip-permissions",
        "--model",
        "opus",
        "--resume",
        session_id,
    ]


def test_handoff_replaces_launch_effort_with_live_effort():
    session_id = str(uuid.uuid4())

    args = claude_router.resume_session_args(
        ["--model", "fable", "--effort", "ultracode"],
        session_id,
        "opus",
        "high",
    )

    assert claude_router.option_value(args, "--effort") == "high"
    assert args.count("--effort") == 1


def test_ultracode_launch_clears_legacy_effort_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    selected = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }

    env = claude_router.routed_environment(
        selected,
        tmp_path / "router-state.json",
        ["--effort", "ultracode"],
    )

    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env
    assert env["CLAUDE_ROUTER_ULTRACODE"] == "1"


def test_explicit_effort_clears_an_inherited_effort_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    selected = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }

    env = claude_router.routed_environment(
        selected,
        tmp_path / "router-state.json",
        ["--effort", "high"],
    )

    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env
    assert "CLAUDE_ROUTER_ULTRACODE" not in env


def test_transcript_cursor_only_accepts_a_new_structured_session_limit(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"content": "You've hit your session limit"},
            }
        )
        + "\n"
    )
    offset = transcript.stat().st_size
    with transcript.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 429,
                    "error": "rate_limit",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "You've hit your session limit · resets later",
                            }
                        ]
                    },
                }
            )
            + "\n"
        )

    limit_kind, next_offset = claude_router.read_new_session_limit(
        transcript,
        offset,
    )

    assert limit_kind == "session"
    assert next_offset == transcript.stat().st_size


def test_transcript_cursor_distinguishes_a_fable_limit(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "apiErrorStatus": 429,
                "error": "rate_limit",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "You've reached your Fable 5 limit. "
                                "Run /usage-credits to continue or switch models with /model."
                            ),
                        }
                    ]
                },
            }
        )
        + "\n"
    )

    limit_kind, next_offset = claude_router.read_new_session_limit(transcript, 0)

    assert limit_kind == "fable"
    assert next_offset == transcript.stat().st_size


def test_transcript_cursor_does_not_consume_a_partial_record(tmp_path):
    transcript = tmp_path / "session.jsonl"
    partial = b'{"type":"assistant","isApiErrorMessage":true'
    transcript.write_bytes(partial)

    limit_kind, next_offset = claude_router.read_new_session_limit(transcript, 0)

    assert limit_kind is None
    assert next_offset == 0


def test_fable_limit_marks_only_the_fable_window(monkeypatch):
    selected = {
        "email": "work@example.com",
        "org_uuid": "org-work",
    }
    marked = []
    monkeypatch.setattr(
        claude_router.accounts,
        "mark_fable_limit",
        lambda email, org_uuid: marked.append(("fable", email, org_uuid)),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "mark_session_limit",
        lambda email, org_uuid: marked.append(("session", email, org_uuid)),
    )

    claude_router.mark_detected_limit(selected, "fable")

    assert marked == [("fable", "work@example.com", "org-work")]


def test_fable_limit_can_fallback_to_opus_on_the_same_account(monkeypatch):
    selected = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    selections = []

    def select_profile(**kwargs):
        selections.append(kwargs)
        if kwargs.get("require_fable"):
            return None
        if kwargs.get("force_label") == "first":
            return selected
        return None

    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        select_profile,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "profile_general_exhausted",
        lambda _label: False,
    )

    route = claude_router.session_limit_route(
        selected,
        "fable",
        "fable",
        123,
    )

    assert route == (selected, "opus")
    assert selections[0]["avoid_labels"] == {"first"}
    assert selections[0]["require_fable"] is True
    assert selections[1]["avoid_labels"] == {"first"}
    assert selections[1]["require_fable"] is False
    assert selections[2]["force_label"] == "first"


def test_fable_limit_does_not_fallback_to_known_exhausted_general_quota(
    monkeypatch,
):
    selected = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    selections = []

    def select_profile(**kwargs):
        selections.append(kwargs)
        return selected if kwargs.get("force_label") else None

    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        select_profile,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "profile_general_exhausted",
        lambda _label: True,
    )

    assert (
        claude_router.session_limit_route(
            selected,
            "fable",
            "fable",
            123,
        )
        is None
    )
    assert not any("force_label" in selection for selection in selections)


def test_generic_session_limit_on_fable_falls_back_to_general(monkeypatch):
    selected = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    second = {
        "profile": "/profiles/second",
        "label": "second",
        "email": "second@example.com",
        "org_uuid": "org-second",
    }
    selections = []

    def select_profile(**kwargs):
        selections.append(kwargs)
        return None if kwargs.get("require_fable") else second

    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        select_profile,
    )

    route = claude_router.session_limit_route(
        selected,
        "fable",
        "session",
        123,
    )

    assert route == (second, "opus")
    assert selections == [
        {
            "avoid_labels": {"first"},
            "require_fable": True,
            "lease_pid": 123,
        },
        {
            "avoid_labels": {"first"},
            "require_fable": False,
            "prefer_fable": False,
            "lease_pid": 123,
        },
    ]


def test_generic_limit_does_not_restart_the_same_forced_account(monkeypatch):
    selected = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **_kwargs: selected,
    )

    assert (
        claude_router.session_limit_route(
            selected,
            "fable",
            "session",
            123,
        )
        is None
    )


def test_fable_limit_does_not_reroute_an_opus_session(monkeypatch):
    selected = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **_kwargs: pytest.fail("spent general quota for a Fable-only limit"),
    )

    assert (
        claude_router.session_limit_route(
            selected,
            "general",
            "fable",
            123,
        )
        is None
    )


@pytest.mark.parametrize(
    "first_child_stays_open",
    [True, False],
    ids=["prompt-remains-open", "child-exits-after-error"],
)
def test_api_session_limit_hands_off_the_same_conversation(
    first_child_stays_open,
    tmp_path,
    monkeypatch,
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    first = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    second = {
        "profile": "/profiles/second",
        "label": "second",
        "email": "second@example.com",
        "org_uuid": "org-second",
    }
    launches = []
    limited = []
    route_families = []
    session_id = str(uuid.uuid4())

    class Child:
        def __init__(self, running):
            self.running = running

        def poll(self):
            return None if self.running else 0

        def wait(self):
            return 0

    def launch(command, **kwargs):
        launches.append((command, kwargs))
        if len(launches) == 1:
            with transcript.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "isApiErrorMessage": True,
                            "apiErrorStatus": 429,
                            "error": "rate_limit",
                            "message": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "You've hit your session limit · "
                                            "resets later"
                                        ),
                                    }
                                ]
                            },
                        }
                    )
                    + "\n"
                )
        return Child(
            running=len(launches) == 1 and first_child_stays_open
        )

    monkeypatch.setattr(
        claude_router,
        "initial_session_args",
        lambda args: ([*args, "--session-id", session_id], session_id),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: ({"mode": "auto", "label": None}, (1, 1)),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **kwargs: second if kwargs.get("avoid_labels") else first,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "mark_session_limit",
        lambda email, org_uuid: limited.append((email, org_uuid)),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "handoff_target",
        lambda *_args, **_kwargs: "second" if limited else None,
    )
    monkeypatch.setattr(
        claude_router,
        "session_limit_route",
        lambda _selected, family, _kind, _pid: (
            route_families.append(family) or (second, None)
        ),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "upsert_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "remove_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router,
        "session_transcript_path",
        lambda candidate: transcript if candidate == session_id else None,
    )
    monkeypatch.setattr(
        claude_router,
        "read_router_state",
        lambda _path: {
            "session_id": session_id,
            "model": "Fable 5",
            "effort": "high",
        },
    )
    monkeypatch.setattr(claude_router.subprocess, "Popen", launch)
    monkeypatch.setattr(claude_router.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(claude_router, "stop_for_handoff", lambda _child: None)
    monkeypatch.setattr(
        claude_router,
        "set_synchronized_output",
        lambda _enabled: False,
    )

    assert claude_router.run_supervised(
        "/real/claude",
        ["--model", "opus"],
    ) == 0

    assert limited == [("first@example.com", "org-first")]
    assert route_families == ["fable"]
    assert [launch[1]["env"]["ACCOUNTS_ROUTED_LABEL"] for launch in launches] == [
        "first",
        "second",
    ]
    assert claude_router.option_value(launches[1][0], "--effort") == "high"
    assert launches[1][0][-2:] == ["--resume", session_id]


def test_session_limit_route_is_recomputed_until_a_later_set_target_exists(
    tmp_path,
    monkeypatch,
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    first = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    second = {
        "profile": "/profiles/second",
        "label": "second",
        "email": "second@example.com",
        "org_uuid": "org-second",
    }
    session_id = str(uuid.uuid4())
    mode = {"mode": "auto", "label": None}
    launches = []
    limited = []
    route_modes = []
    sleep_count = 0

    class Child:
        def __init__(self, first_launch):
            self.first_launch = first_launch
            self.polls = 0

        def poll(self):
            self.polls += 1
            if self.first_launch and self.polls <= 2:
                return None
            return 0

        def wait(self):
            return 0

    def sleep(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            mode.update({"mode": "set", "label": "second"})

    def route(_selected, _family, _kind, _pid):
        route_modes.append(mode["mode"])
        return (second, None) if mode["mode"] == "set" else None

    def launch(command, **kwargs):
        launches.append((command, kwargs))
        if len(launches) == 1:
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "isApiErrorMessage": True,
                        "apiErrorStatus": 429,
                        "error": "rate_limit",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "You've hit your session limit · "
                                        "resets later"
                                    ),
                                }
                            ]
                        },
                    }
                )
                + "\n"
            )
        return Child(first_launch=len(launches) == 1)

    monkeypatch.setattr(
        claude_router,
        "initial_session_args",
        lambda args: ([*args, "--session-id", session_id], session_id),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: (dict(mode), (1, 1)),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **_kwargs: first,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "mark_session_limit",
        lambda email, org_uuid: limited.append((email, org_uuid)),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "upsert_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "remove_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router,
        "session_transcript_path",
        lambda candidate: transcript if candidate == session_id else None,
    )
    monkeypatch.setattr(
        claude_router,
        "read_router_state",
        lambda _path: {
            "session_id": session_id,
            "model": "Opus 5",
            "effort": "max",
        },
    )
    monkeypatch.setattr(claude_router, "session_limit_route", route)
    monkeypatch.setattr(
        claude_router.subprocess,
        "Popen",
        launch,
    )
    monkeypatch.setattr(claude_router.time, "sleep", sleep)
    monkeypatch.setattr(claude_router, "stop_for_handoff", lambda _child: None)
    monkeypatch.setattr(
        claude_router,
        "set_synchronized_output",
        lambda _enabled: False,
    )

    assert claude_router.run_supervised("/real/claude", []) == 0

    assert route_modes == ["auto", "set"]
    assert limited == [("first@example.com", "org-first")]
    assert [launch[1]["env"]["ACCOUNTS_ROUTED_LABEL"] for launch in launches] == [
        "first",
        "second",
    ]
    assert claude_router.option_value(launches[1][0], "--effort") == "max"


@pytest.mark.parametrize(
    "first_child_enters_loop",
    [True, False],
    ids=["alive-for-first-poll", "exits-before-first-poll"],
)
def test_continue_catches_a_limit_written_before_the_session_id_is_known(
    first_child_enters_loop,
    tmp_path,
    monkeypatch,
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2001-09-09T01:46:39+00:00",
                "isApiErrorMessage": True,
                "apiErrorStatus": 429,
                "error": "rate_limit",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "You've hit your session limit · old",
                        }
                    ]
                },
            }
        )
        + "\n"
    )
    first = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    second = {
        "profile": "/profiles/second",
        "label": "second",
        "email": "second@example.com",
        "org_uuid": "org-second",
    }
    launches = []
    limited = []
    session_id = str(uuid.uuid4())

    class Child:
        def __init__(self, running_once):
            self.running_once = running_once

        def poll(self):
            if self.running_once:
                self.running_once = False
                return None
            return 0

        def wait(self):
            return 0

    def launch(command, **kwargs):
        launches.append((command, kwargs))
        if len(launches) == 1:
            with transcript.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "timestamp": "2001-09-09T01:46:41+00:00",
                            "isApiErrorMessage": True,
                            "apiErrorStatus": 429,
                            "error": "rate_limit",
                            "message": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "You've hit your session limit · "
                                            "new"
                                        ),
                                    }
                                ]
                            },
                        }
                    )
                    + "\n"
                )
        return Child(
            running_once=len(launches) == 1 and first_child_enters_loop
        )

    monkeypatch.setattr(claude_router.time, "time", lambda: 1_000_000_000.0)
    monkeypatch.setattr(claude_router.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: ({"mode": "auto", "label": None}, (1, 1)),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **kwargs: second if kwargs.get("avoid_labels") else first,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "mark_session_limit",
        lambda email, org_uuid: limited.append((email, org_uuid)),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "handoff_target",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "upsert_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "remove_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router,
        "session_transcript_path",
        lambda candidate: transcript if candidate == session_id else None,
    )
    monkeypatch.setattr(
        claude_router,
        "read_router_state",
        lambda _path: {"session_id": session_id, "model": "Fable 5"},
    )
    monkeypatch.setattr(claude_router.subprocess, "Popen", launch)
    monkeypatch.setattr(claude_router, "stop_for_handoff", lambda _child: None)
    monkeypatch.setattr(
        claude_router,
        "set_synchronized_output",
        lambda _enabled: False,
    )

    assert claude_router.run_supervised(
        "/real/claude",
        ["--continue", "--model", "fable"],
    ) == 0

    assert limited == [("first@example.com", "org-first")]
    assert [launch[1]["env"]["ACCOUNTS_ROUTED_LABEL"] for launch in launches] == [
        "first",
        "second",
    ]
    assert launches[1][0][-2:] == ["--resume", session_id]


def test_rendered_model_name_maps_to_a_cli_alias():
    assert claude_router.model_name([], "Opus 5 (1M context)") == "opus"
    assert claude_router.model_name([], "Fable 5.max") == "fable"


def test_handoff_finishes_synchronized_output_before_returning(monkeypatch):
    events = []

    class Child:
        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append(("wait", timeout))

    monkeypatch.setattr(
        claude_router,
        "set_synchronized_output",
        lambda enabled: events.append("sync-on" if enabled else "sync-off") or True,
    )

    claude_router.stop_for_handoff(Child())

    assert events == ["sync-on", "terminate", ("wait", 5), "sync-off"]


def test_synchronized_output_emits_dec_control_bytes_on_a_pty(monkeypatch):
    master_fd, slave_fd = os.openpty()
    stdout = os.fdopen(slave_fd, "w", closefd=False)
    try:
        monkeypatch.setattr(sys, "stdout", stdout)

        assert claude_router.set_synchronized_output(True) is True
        assert claude_router.set_synchronized_output(False) is True

        expected = claude_router.SYNC_OUTPUT_ON + claude_router.SYNC_OUTPUT_OFF
        assert read_exact(master_fd, len(expected)) == expected
    finally:
        stdout.close()
        os.close(slave_fd)
        os.close(master_fd)


def test_handoff_brackets_timeout_kill_cleanup_on_a_pty(monkeypatch):
    master_fd, slave_fd = os.openpty()
    stdout = os.fdopen(slave_fd, "w", closefd=False)

    class Child:
        def terminate(self):
            os.write(slave_fd, b"terminate")

        def wait(self, timeout=None):
            os.write(slave_fd, b"wait")
            if timeout is not None:
                raise subprocess.TimeoutExpired("claude", timeout)

        def kill(self):
            os.write(slave_fd, b"kill")

    try:
        monkeypatch.setattr(sys, "stdout", stdout)

        claude_router.stop_for_handoff(Child())

        expected = (
            claude_router.SYNC_OUTPUT_ON
            + b"terminatewaitkillwait"
            + claude_router.SYNC_OUTPUT_OFF
        )
        assert read_exact(master_fd, len(expected)) == expected
    finally:
        stdout.close()
        os.close(slave_fd)
        os.close(master_fd)


def test_running_supervisor_ignores_advisory_quota_changes(monkeypatch):
    session_id = str(uuid.uuid4())
    selected = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    launches = []

    class Child:
        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else 0

        def wait(self):
            return 0

    monkeypatch.setattr(
        claude_router,
        "initial_session_args",
        lambda _args: (["--resume", session_id], session_id),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **_kwargs: selected,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "upsert_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "remove_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: ({"mode": "auto", "label": None}, (1, 1)),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "handoff_target",
        lambda *_args, **_kwargs: pytest.fail(
            "quota-board refresh must not reroute an auto-mode live session"
        ),
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "Popen",
        lambda command, **_kwargs: launches.append(command) or Child(),
    )
    monkeypatch.setattr(claude_router.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        claude_router,
        "read_router_state",
        lambda _path: {
            "session_id": session_id,
            "model": "Opus 5",
            "effort": "max",
        },
    )

    assert claude_router.run_supervised("/real/claude", []) == 0
    assert len(launches) == 1


@pytest.mark.parametrize("requested_mode", ["fable", "set"])
def test_running_supervisor_retries_an_unavailable_human_route(
    requested_mode,
    monkeypatch,
):
    session_id = str(uuid.uuid4())
    current = {
        "profile": "/profiles/current",
        "label": "current",
        "email": "current@example.com",
        "org_uuid": "org-current",
    }
    target = {
        "profile": "/profiles/target",
        "label": "target",
        "email": "target@example.com",
        "org_uuid": "org-target",
    }
    mode = {"mode": "auto", "label": None}
    generation = {"value": (1, 1)}
    launches = []
    route_attempts = 0

    class Child:
        def __init__(self, first_launch):
            self.first_launch = first_launch
            self.polls = 0

        def poll(self):
            if not self.first_launch:
                return 0
            self.polls += 1
            return None if self.polls <= 2 else 0

        def wait(self):
            return 0

    def select_profile(**_kwargs):
        nonlocal route_attempts
        if not launches:
            return current
        route_attempts += 1
        return target if route_attempts == 2 else None

    def launch(command, **kwargs):
        launches.append((command, kwargs))
        if len(launches) == 1:
            mode.update(
                {
                    "mode": requested_mode,
                    "label": "target" if requested_mode == "set" else None,
                }
            )
            generation["value"] = (2, 2)
        return Child(first_launch=len(launches) == 1)

    monkeypatch.setattr(
        claude_router,
        "initial_session_args",
        lambda _args: (["--resume", session_id], session_id),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: (dict(mode), generation["value"]),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        select_profile,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "handoff_target",
        lambda *_args, **_kwargs: "target",
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "upsert_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "remove_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router,
        "read_router_state",
        lambda _path: {
            "session_id": session_id,
            "model": "Fable 5" if len(launches) > 1 else "Opus 5",
            "effort": "max",
        },
    )
    monkeypatch.setattr(claude_router.subprocess, "Popen", launch)
    monkeypatch.setattr(claude_router.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(claude_router, "stop_for_handoff", lambda _child: None)

    assert claude_router.run_supervised("/real/claude", []) == 0
    assert route_attempts == 2
    assert [
        launch[1]["env"]["ACCOUNTS_ROUTED_LABEL"]
        for launch in launches
    ] == ["current", "target"]


def test_fable_mode_launches_directly_on_fable(monkeypatch, tmp_path):
    # A real ~/.claude/settings.json naming a fable model would hide the
    # --model injection this test asserts on.
    monkeypatch.setenv("HOME", str(tmp_path))
    selected = {
        "profile": "/profiles/fable",
        "label": "fable",
        "email": "fable@example.com",
        "org_uuid": "org-fable",
    }
    selections = []
    launches = []

    class Child:
        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: ({"mode": "fable", "label": None}, (1, 1)),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **kwargs: selections.append(kwargs) or selected,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "upsert_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "remove_session_lease",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append((command, kwargs)) or Child(),
    )
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")

    # Bare launch: an explicit non-fable --model is honored instead (see
    # test_fable_mode_honors_an_explicit_opus_launch).
    assert claude_router.run_supervised("/real/claude", []) == 0
    assert selections[0]["require_fable"] is True
    assert launches[0][0][-2:] == ["--model", "fable"]
    assert claude_router.option_value(launches[0][0], "--effort") == "ultracode"
    assert claude_router.option_value(launches[0][0], "--name") == "\u200b"
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in launches[0][1]["env"]
    assert launches[0][1]["env"]["CLAUDE_ROUTER_ULTRACODE"] == "1"


def test_passthrough_preserves_an_explicit_profile(monkeypatch):
    calls = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/profiles/explicit")
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **kwargs: calls.append(("select", kwargs)),
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "call",
        lambda command, **kwargs: calls.append(("call", command, kwargs)) or 0,
    )

    assert claude_router.run_passthrough("/real/claude", ["auth", "status"]) == 0
    assert calls == [("call", ["/real/claude", "auth", "status"], {})]


def test_print_inference_defaults_to_ultracode(monkeypatch):
    calls = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/profiles/explicit")
    monkeypatch.setattr(
        claude_router.subprocess,
        "call",
        lambda command, **kwargs: calls.append((command, kwargs)) or 0,
    )

    assert claude_router.run_passthrough(
        "/real/claude",
        ["--print", "hello"],
    ) == 0

    assert claude_router.option_value(calls[0][0], "--effort") == "ultracode"


def test_print_explicit_effort_clears_an_inherited_override(monkeypatch):
    calls = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/profiles/explicit")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setattr(
        claude_router.subprocess,
        "call",
        lambda command, **kwargs: calls.append((command, kwargs)) or 0,
    )

    assert claude_router.run_passthrough(
        "/real/claude",
        ["--print", "hello", "--effort", "high"],
    ) == 0

    assert claude_router.option_value(calls[0][0], "--effort") == "high"
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in calls[0][1]["env"]


def test_fable_print_requires_fable_headroom(monkeypatch):
    calls = []
    selected = {
        "profile": "/profiles/fable",
        "label": "fable",
        "email": "fable@example.com",
        "org_uuid": "org-fable",
    }
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **kwargs: calls.append(("select", kwargs)) or selected,
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "call",
        lambda command, **kwargs: calls.append(("call", command, kwargs)) or 0,
    )

    assert (
        claude_router.run_passthrough(
            "/real/claude",
            ["--model", "fable", "--print", "hello"],
        )
        == 0
    )
    assert calls[0] == ("select", {"require_fable": True})
    assert calls[1][1][:5] == [
        "/real/claude",
        "--model",
        "fable",
        "--print",
        "hello",
    ]
    assert claude_router.option_value(calls[1][1], "--effort") == "ultracode"
    assert calls[1][2]["env"]["CLAUDE_CONFIG_DIR"] == "/profiles/fable"


def test_fable_print_falls_back_to_opus(monkeypatch):
    calls = []
    general = {
        "profile": "/profiles/general",
        "label": "general",
        "email": "general@example.com",
        "org_uuid": "org-general",
    }
    selections = iter([None, general])
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **kwargs: calls.append(("select", kwargs)) or next(selections),
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "call",
        lambda command, **kwargs: calls.append(("call", command, kwargs)) or 0,
    )

    assert (
        claude_router.run_passthrough(
            "/real/claude",
            ["--model", "fable", "--print", "hello"],
        )
        == 0
    )
    assert calls[:2] == [
        ("select", {"require_fable": True}),
        (
            "select",
            {
                "require_fable": False,
                "prefer_fable": False,
            },
        ),
    ]
    assert calls[2][1][:3] == [
        "/real/claude",
        "--print",
        "hello",
    ]
    assert claude_router.option_value(calls[2][1], "--model") == "opus"
    assert claude_router.option_value(calls[2][1], "--effort") == "ultracode"
    assert calls[2][2]["env"]["CLAUDE_CONFIG_DIR"] == "/profiles/general"


def test_passthrough_fails_closed_without_a_safe_profile(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "call",
        lambda *_args, **_kwargs: pytest.fail("used ambient credentials"),
    )

    assert claude_router.run_passthrough("/real/claude", ["--print", "hello"]) == 1
    assert "no account has enough quota" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("reported_effort", "router_marker", "expected_effort"),
    [
        ("xhigh", "1", "ultracode"),
        ("max", "1", "max"),
        ("xhigh", None, "xhigh"),
    ],
)
def test_statusline_publishes_the_live_session_identity_and_effort(
    reported_effort,
    router_marker,
    expected_effort,
    tmp_path,
):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    state_path = Path(
        f"/tmp/claude/account-router-{os.getpid()}-{uuid.uuid4().hex}.json"
    )
    fixture = json.loads((REPO / "test" / "fixtures" / "input.json").read_text())
    fixture["workspace"]["current_dir"] = str(project)
    fixture["effort"] = {"level": reported_effort}
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "TZ": "America/Los_Angeles",
            "ACCOUNTS_ROUTER_STATE": str(state_path),
            "ACCOUNTS_ROUTED_LABEL": "first",
            "ACCOUNTS_ROUTED_EMAIL": "first@example.com",
            "ACCOUNTS_ROUTED_ORG_UUID": "org-first",
        }
    )
    if router_marker is None:
        env.pop("CLAUDE_ROUTER_ULTRACODE", None)
    else:
        env["CLAUDE_ROUTER_ULTRACODE"] = router_marker

    subprocess.run(
        ["bash", str(REPO / "bin" / "statusline.sh")],
        input=json.dumps(fixture),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    try:
        state = json.loads(state_path.read_text())
        assert state["session_id"] == fixture["session_id"]
        assert state["model"] == fixture["model"]["display_name"]
        assert state["effort"] == expected_effort
        assert state["label"] == "first"
    finally:
        state_path.unlink(missing_ok=True)


def test_statusline_labels_an_ultracode_session(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    fixture = json.loads((REPO / "test" / "fixtures" / "input.json").read_text())
    fixture["workspace"]["current_dir"] = str(project)
    fixture["effort"] = {"level": "xhigh"}
    env = {
        key: value
        for key, value in os.environ.items()
        # A supervised session injects ACCOUNTS_*/CLAUDE_* vars that change
        # what the statusline renders; the test env must not inherit them.
        if not key.startswith(("ACCOUNTS_", "CLAUDE_", "STATUSLINE_", "FORMAT"))
    }
    env.update(
        {
            "HOME": str(home),
            "TZ": "America/Los_Angeles",
            "CLAUDE_ROUTER_ULTRACODE": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(REPO / "bin" / "statusline.sh")],
        input=json.dumps(fixture),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    assert ".ultracode" in result.stdout
    assert ".xhigh" not in result.stdout


def test_statusline_keeps_plain_xhigh_distinct_from_ultracode(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    fixture = json.loads((REPO / "test" / "fixtures" / "input.json").read_text())
    fixture["workspace"]["current_dir"] = str(project)
    fixture["effort"] = {"level": "xhigh"}
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "TZ": "America/Los_Angeles",
        }
    )
    env.pop("CLAUDE_ROUTER_ULTRACODE", None)

    result = subprocess.run(
        ["bash", str(REPO / "bin" / "statusline.sh")],
        input=json.dumps(fixture),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    assert ".xhigh" in result.stdout
    assert ".ultracode" not in result.stdout


def test_statusline_uses_the_live_effort_when_ultracode_is_not_active(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    fixture = json.loads((REPO / "test" / "fixtures" / "input.json").read_text())
    fixture["workspace"]["current_dir"] = str(project)
    fixture["effort"] = {"level": "max"}
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "TZ": "America/Los_Angeles",
            "CLAUDE_ROUTER_ULTRACODE": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(REPO / "bin" / "statusline.sh")],
        input=json.dumps(fixture),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    assert ".max" in result.stdout
    assert ".ultracode" not in result.stdout


def test_statusline_tracks_effort_transitions_in_router_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    state_path = Path(
        f"/tmp/claude/account-router-{os.getpid()}-{uuid.uuid4().hex}.json"
    )
    fixture = json.loads((REPO / "test" / "fixtures" / "input.json").read_text())
    fixture["workspace"]["current_dir"] = str(project)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "TZ": "America/Los_Angeles",
            "ACCOUNTS_ROUTER_STATE": str(state_path),
            "ACCOUNTS_ROUTED_LABEL": "first",
            "ACCOUNTS_ROUTED_EMAIL": "first@example.com",
            "ACCOUNTS_ROUTED_ORG_UUID": "org-first",
            "CLAUDE_ROUTER_ULTRACODE": "1",
        }
    )

    try:
        outputs = []
        for reported, expected in (
            ("xhigh", "ultracode"),
            ("max", "max"),
            ("xhigh", "xhigh"),
        ):
            fixture["effort"] = {"level": reported}
            result = subprocess.run(
                ["bash", str(REPO / "bin" / "statusline.sh")],
                input=json.dumps(fixture),
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            outputs.append(result.stdout)
            assert json.loads(state_path.read_text())["effort"] == expected

        assert ".ultracode" in outputs[0]
        assert ".max" in outputs[1]
        assert ".xhigh" in outputs[2]
        assert ".ultracode" not in outputs[2]

        handoff_args = claude_router.resume_session_args(
            ["--effort", "ultracode"],
            fixture["session_id"],
            effort_override=json.loads(state_path.read_text())["effort"],
        )
        monkeypatch.setenv("CLAUDE_ROUTER_ULTRACODE", "1")
        handoff_env = claude_router.routed_environment(
            {
                "profile": "/profiles/second",
                "label": "second",
                "email": "second@example.com",
                "org_uuid": "org-second",
            },
            tmp_path / "router-state.json",
            handoff_args,
        )
        assert claude_router.option_value(handoff_args, "--effort") == "xhigh"
        assert "CLAUDE_ROUTER_ULTRACODE" not in handoff_env
    finally:
        state_path.unlink(missing_ok=True)


def test_statusline_says_when_a_forced_target_is_pending(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".accounts").mkdir()
    (home / ".accounts" / "mode.json").write_text(
        '{"mode":"set","label":"preferred"}'
    )
    project = tmp_path / "project"
    project.mkdir()
    profile = home / ".accounts" / "profiles" / "safe"
    profile.mkdir(parents=True)
    fixture = json.loads((REPO / "test" / "fixtures" / "input.json").read_text())
    fixture["workspace"]["current_dir"] = str(project)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "TZ": "America/Los_Angeles",
            "CLAUDE_CONFIG_DIR": str(profile),
            "ACCOUNTS_ROUTED_LABEL": "safe",
            "ACCOUNTS_ROUTED_EMAIL": "safe@example.com",
        }
    )

    result = subprocess.run(
        ["bash", str(REPO / "bin" / "statusline.sh")],
        input=json.dumps(fixture),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    assert "safe" in result.stdout
    assert "set preferred pending" in result.stdout


def test_statusline_says_when_an_env_pin_is_bypassed(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    profile = home / ".accounts" / "profiles" / "safe"
    profile.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    fixture = json.loads((REPO / "test" / "fixtures" / "input.json").read_text())
    fixture["workspace"]["current_dir"] = str(project)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "TZ": "America/Los_Angeles",
            "CLAUDE_CONFIG_DIR": str(profile),
            "ACCOUNTS_PIN": "preferred",
            "ACCOUNTS_ROUTED_LABEL": "safe",
            "ACCOUNTS_ROUTED_EMAIL": "safe@example.com",
        }
    )

    result = subprocess.run(
        ["bash", str(REPO / "bin" / "statusline.sh")],
        input=json.dumps(fixture),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    assert "pin preferred bypassed" in result.stdout


def test_advisory_usage_at_the_wall_hands_off_a_running_session(tmp_path):
    # Inverts the rule this test previously pinned. Holding a live session until
    # a real 429 meant riding an account to exhaustion with idle accounts on the
    # board, and the 429 costs more than the stop-and-resume it was avoiding: it
    # ends the turn AND locks the account out for hours. Departure is gated at
    # DEPART_PCT, well above the bar for merely being unfit to receive work, so
    # mid-range advisory noise still moves nothing.
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    accounts_dir = home / ".accounts"
    claude_dir.mkdir(parents=True)
    accounts_dir.mkdir()
    (home / ".claude.json").write_text('{"hasCompletedOnboarding":true}')
    (claude_dir / "settings.json").write_text('{"model":"opus"}')
    blobs = {
        "version": 1,
        "accounts": {
            "first": {
                "blob": _blob("first"),
                "email": "first@example.com",
                "org_uuid": "org-first",
            },
            "second": {
                "blob": _blob("second"),
                "email": "second@example.com",
                "org_uuid": "org-second",
            },
        },
    }
    (accounts_dir / "blobs.json").write_text(json.dumps(blobs))
    (accounts_dir / "mode.json").write_text('{"mode":"auto","label":null}')
    resets_path = claude_dir / "account-resets.json"
    resets_path.write_text(
        json.dumps(
            {
                "first@example.com|org-first": {
                    "five_hour_pct": 10,
                    "seven_day_pct": 10,
                    "last_seen": time.time(),
                },
                "second@example.com|org-second": {
                    "five_hour_pct": 20,
                    "seven_day_pct": 20,
                    "last_seen": time.time(),
                },
            }
        )
    )
    log_path = tmp_path / "launches.jsonl"
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, sys, time\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "sid = None\n"
        "for flag in ('--session-id', '--resume'):\n"
        "    if flag in args:\n"
        "        sid = args[args.index(flag) + 1]\n"
        "with Path(os.environ['ROUTER_TEST_LOG']).open('a') as f:\n"
        "    f.write(json.dumps({'args': args, 'label': os.environ['ACCOUNTS_ROUTED_LABEL']}) + '\\n')\n"
        "Path(os.environ['ACCOUNTS_ROUTER_STATE']).write_text(\n"
        "    json.dumps({'session_id': sid, 'model': 'Opus', 'effort': 'high'})\n"
        ")\n"
        "count = len(Path(os.environ['ROUTER_TEST_LOG']).read_text().splitlines())\n"
        "if count == 1:\n"
        "    time.sleep(0.5)\n"
    )
    fake_claude.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CLAUDE_REAL_BIN": str(fake_claude),
            "ACCOUNTS_ROUTER_INTERVAL": "0.05",
            "ROUTER_TEST_LOG": str(log_path),
            "PYTHONPATH": str(REPO / "bin"),
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(REPO / "bin" / "claude-router.py")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 5
    while time.time() < deadline and not log_path.exists():
        time.sleep(0.02)
    assert log_path.exists()
    resets_path.write_text(
        json.dumps(
            {
                "first@example.com|org-first": {
                    "five_hour_pct": 90,
                    "seven_day_pct": 10,
                    "last_seen": time.time(),
                },
                "second@example.com|org-second": {
                    "five_hour_pct": 20,
                    "seven_day_pct": 20,
                    "last_seen": time.time(),
                },
            }
        )
    )

    stdout, stderr = process.communicate(timeout=10)
    launches = [json.loads(line) for line in log_path.read_text().splitlines()]

    assert process.returncode == 0
    assert stdout == ""
    assert stderr == ""
    assert [launch["label"] for launch in launches] == ["first", "second"]
    assert launches[0]["args"][-2] == "--session-id"
    # Same session, new account. A transcript-less session is re-issued by id
    # rather than resumed, so identity is the property worth pinning here.
    assert launches[1]["args"][-1] == launches[0]["args"][-1]


def test_mode_change_before_first_transcript_reuses_the_new_session_id(tmp_path):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    accounts_dir = home / ".accounts"
    claude_dir.mkdir(parents=True)
    accounts_dir.mkdir()
    (home / ".claude.json").write_text('{"hasCompletedOnboarding":true}')
    (claude_dir / "settings.json").write_text('{"model":"opus"}')
    (accounts_dir / "blobs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    "general": {
                        "blob": _blob("general"),
                        "email": "general@example.com",
                        "org_uuid": "org-general",
                    },
                    "fable": {
                        "blob": _blob("fable"),
                        "email": "fable@example.com",
                        "org_uuid": "org-fable",
                    },
                },
            }
        )
    )
    mode_path = accounts_dir / "mode.json"
    mode_path.write_text('{"mode":"auto","label":null}')
    (claude_dir / "account-resets.json").write_text(
        json.dumps(
            {
                "general@example.com|org-general": {
                    "five_hour_pct": 10,
                    "seven_day_pct": 10,
                    "fable_pct": 100,
                    "last_seen": time.time(),
                },
                "fable@example.com|org-fable": {
                    "five_hour_pct": 20,
                    "seven_day_pct": 20,
                    "fable_pct": 20,
                    "last_seen": time.time(),
                },
            }
        )
    )
    log_path = tmp_path / "launches.jsonl"
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, sys, time\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "sid = None\n"
        "for flag in ('--session-id', '--resume'):\n"
        "    if flag in args:\n"
        "        sid = args[args.index(flag) + 1]\n"
        "label = os.environ['ACCOUNTS_ROUTED_LABEL']\n"
        "with Path(os.environ['ROUTER_TEST_LOG']).open('a') as f:\n"
        "    f.write(json.dumps({'args': args, 'label': label}) + '\\n')\n"
        "model = 'Fable 5.max' if 'fable' in args else 'Opus 5.max'\n"
        "Path(os.environ['ACCOUNTS_ROUTER_STATE']).write_text(\n"
        "    json.dumps({'session_id': sid, 'model': model, 'effort': 'high'})\n"
        ")\n"
        "count = len(Path(os.environ['ROUTER_TEST_LOG']).read_text().splitlines())\n"
        "if count == 1:\n"
        "    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "    deadline = time.time() + 2\n"
        "    while time.time() < deadline: time.sleep(0.05)\n"
    )
    fake_claude.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CLAUDE_REAL_BIN": str(fake_claude),
            "ACCOUNTS_ROUTER_INTERVAL": "0.05",
            "ROUTER_TEST_LOG": str(log_path),
            "PYTHONPATH": str(REPO / "bin"),
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(REPO / "bin" / "claude-router.py")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 5
    while time.time() < deadline and not log_path.exists():
        time.sleep(0.02)
    assert log_path.exists()
    mode_path.write_text('{"mode":"fable","label":null}')

    stdout, stderr = process.communicate(timeout=10)
    launches = [json.loads(line) for line in log_path.read_text().splitlines()]

    assert process.returncode == 0
    assert stdout == ""
    assert stderr == ""
    assert [launch["label"] for launch in launches] == ["general", "fable"]
    first_session = launches[0]["args"][-1]
    assert launches[1]["args"][
        launches[1]["args"].index("--effort") + 1
    ] == "high"
    assert launches[1]["args"][-4:] == [
        "--model",
        "fable",
        "--session-id",
        first_session,
    ]


def test_fable_ignores_general_ceilings_until_fable_usage_is_full(tmp_path):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    accounts_dir = home / ".accounts"
    claude_dir.mkdir(parents=True)
    accounts_dir.mkdir()
    (home / ".claude.json").write_text('{"hasCompletedOnboarding":true}')
    (claude_dir / "settings.json").write_text('{"model":"fable"}')
    (accounts_dir / "blobs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    "first": {
                        "blob": _blob("first"),
                        "email": "first@example.com",
                        "org_uuid": "org-first",
                    },
                    "second": {
                        "blob": _blob("second"),
                        "email": "second@example.com",
                        "org_uuid": "org-second",
                    },
                },
            }
        )
    )
    (accounts_dir / "mode.json").write_text('{"mode":"auto","label":null}')
    resets_path = claude_dir / "account-resets.json"
    resets_path.write_text(
        json.dumps(
            {
                "first@example.com|org-first": {
                    "five_hour_pct": 100,
                    "seven_day_pct": 100,
                    "fable_pct": 10,
                    "last_seen": time.time(),
                },
                "second@example.com|org-second": {
                    "five_hour_pct": 20,
                    "seven_day_pct": 20,
                    "fable_pct": 100,
                    "last_seen": time.time(),
                },
            }
        )
    )
    log_path = tmp_path / "launches.jsonl"
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, sys, time\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "sid = None\n"
        "for flag in ('--session-id', '--resume'):\n"
        "    if flag in args:\n"
        "        sid = args[args.index(flag) + 1]\n"
        "label = os.environ['ACCOUNTS_ROUTED_LABEL']\n"
        "with Path(os.environ['ROUTER_TEST_LOG']).open('a') as f:\n"
        "    f.write(json.dumps({'args': args, 'label': label}) + '\\n')\n"
        "model = 'Opus' if 'opus' in args else 'Fable 5.max'\n"
        "Path(os.environ['ACCOUNTS_ROUTER_STATE']).write_text(\n"
        "    json.dumps({'session_id': sid, 'model': model, 'effort': 'max'})\n"
        ")\n"
        "count = len(Path(os.environ['ROUTER_TEST_LOG']).read_text().splitlines())\n"
        "if count == 1:\n"
        "    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "    while True: time.sleep(0.05)\n"
    )
    fake_claude.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CLAUDE_REAL_BIN": str(fake_claude),
            "ACCOUNTS_ROUTER_INTERVAL": "0.05",
            "ROUTER_TEST_LOG": str(log_path),
            "PYTHONPATH": str(REPO / "bin"),
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(REPO / "bin" / "claude-router.py")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 5
    while time.time() < deadline and not log_path.exists():
        time.sleep(0.02)
    assert log_path.exists()
    resets_path.write_text(
        json.dumps(
            {
                "first@example.com|org-first": {
                    "five_hour_pct": 100,
                    "seven_day_pct": 100,
                    "fable_pct": 100,
                    "last_seen": time.time(),
                },
                "second@example.com|org-second": {
                    "five_hour_pct": 20,
                    "seven_day_pct": 20,
                    "fable_pct": 100,
                    "last_seen": time.time(),
                },
            }
        )
    )

    stdout, stderr = process.communicate(timeout=10)
    launches = [json.loads(line) for line in log_path.read_text().splitlines()]

    assert process.returncode == 0
    assert stdout == ""
    assert stderr == ""
    assert [launch["label"] for launch in launches] == ["first", "second"]
    first_session = launches[0]["args"][-1]
    assert launches[1]["args"][
        launches[1]["args"].index("--effort") + 1
    ] == "max"
    assert launches[1]["args"][-4:] == [
        "--model",
        "opus",
        "--session-id",
        first_session,
    ]


def _pin_test_harness(monkeypatch, session_id, selections):
    monkeypatch.setattr(
        claude_router,
        "initial_session_args",
        lambda args: ([*args, "--session-id", session_id], session_id),
    )
    monkeypatch.setattr(
        claude_router.accounts,
        "select_profile",
        lambda **kwargs: selections.append(kwargs)
        or {
            "profile": "/profiles/first",
            "label": "first",
            "email": "first@example.com",
            "org_uuid": "org-first",
        },
    )
    monkeypatch.setattr(
        claude_router.accounts, "upsert_session_lease", lambda *_args: None
    )
    monkeypatch.setattr(
        claude_router.accounts, "remove_session_lease", lambda *_args: None
    )
    monkeypatch.setattr(
        claude_router, "session_transcript_path", lambda _candidate: None
    )
    monkeypatch.setattr(claude_router.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        claude_router, "set_synchronized_output", lambda _enabled: False
    )


def test_fable_mode_honors_an_explicit_opus_launch(monkeypatch):
    session_id = str(uuid.uuid4())
    selections = []
    launches = []

    class Child:
        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: ({"mode": "fable", "label": None}, (1, 1)),
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append(command) or Child(),
    )
    monkeypatch.setattr(claude_router, "read_router_state", lambda _path: {})
    _pin_test_harness(monkeypatch, session_id, selections)

    assert claude_router.run_supervised("/real/claude", ["--model", "opus"]) == 0

    assert len(launches) == 1
    assert claude_router.option_value(launches[0], "--model") == "opus"
    assert selections[0].get("require_fable") is False


def test_fable_mode_keeps_a_live_opus_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    session_id = str(uuid.uuid4())
    selections = []
    launches = []
    handoffs = []
    polls = []

    class Child:
        def poll(self):
            polls.append(True)
            return None if len(polls) < 4 else 0

        def wait(self):
            return 0

    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: ({"mode": "fable", "label": None}, (1, 1)),
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append(command) or Child(),
    )
    monkeypatch.setattr(
        claude_router,
        "read_router_state",
        lambda _path: {"session_id": session_id, "model": "Opus 4.5"},
    )
    monkeypatch.setattr(
        claude_router, "stop_for_handoff", lambda child: handoffs.append(child)
    )
    _pin_test_harness(monkeypatch, session_id, selections)

    assert claude_router.run_supervised("/real/claude", []) == 0

    assert claude_router.option_value(launches[0], "--model") == "fable"
    assert len(launches) == 1
    assert handoffs == []


def test_reissued_fable_mode_promotes_a_pinned_session(monkeypatch):
    session_id = str(uuid.uuid4())
    selections = []
    launches = []
    generations = []

    class Child:
        def __init__(self, running):
            self.running = running

        def poll(self):
            return None if self.running else 0

        def wait(self):
            return 0

    def load_mode_snapshot():
        generations.append(True)
        generation = (1, 1) if len(generations) < 3 else (2, 2)
        return {"mode": "fable", "label": None}, generation

    monkeypatch.setattr(
        claude_router.accounts, "load_mode_snapshot", load_mode_snapshot
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append(command)
        or Child(running=len(launches) == 1),
    )
    monkeypatch.setattr(
        claude_router,
        "read_router_state",
        lambda _path: {"session_id": session_id, "model": "Opus 4.5"},
    )
    monkeypatch.setattr(claude_router, "stop_for_handoff", lambda _child: None)
    _pin_test_harness(monkeypatch, session_id, selections)

    assert claude_router.run_supervised("/real/claude", []) == 0

    assert len(launches) == 2
    assert claude_router.option_value(launches[1], "--model") == "fable"
    assert launches[1][-2:] == ["--session-id", session_id]


def test_router_imposed_opus_fallback_still_recovers_to_fable(monkeypatch):
    # Fable exhausted at launch -> the router falls back to Opus itself. That is
    # NOT a user pin: when a Fable account frees up it must return to Fable, with
    # no mode-generation bump. Guards the load-bearing auto-recovery invariant.
    session_id = str(uuid.uuid4())
    selections = []
    launches = []
    fable = {"profile": "/p/fable", "label": "fable",
             "email": "fable@example.com", "org_uuid": "org-fable"}
    opus = {"profile": "/p/opus", "label": "opus",
            "email": "opus@example.com", "org_uuid": "org-opus"}

    class Child:
        def __init__(self, running):
            self.running = running

        def poll(self):
            return None if self.running else 0

        def wait(self):
            return 0

    fable_asks = []

    def select_profile(**kwargs):
        selections.append(kwargs)
        if kwargs.get("require_fable"):
            fable_asks.append(True)
            return None if len(fable_asks) == 1 else fable
        return opus

    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: ({"mode": "fable", "label": None}, (1, 1)),
    )
    monkeypatch.setattr(claude_router.accounts, "select_profile", select_profile)
    monkeypatch.setattr(
        claude_router.accounts, "upsert_session_lease", lambda *_a: None
    )
    monkeypatch.setattr(
        claude_router.accounts, "remove_session_lease", lambda *_a: None
    )
    monkeypatch.setattr(
        claude_router,
        "initial_session_args",
        lambda args: ([*args, "--session-id", session_id], session_id),
    )
    monkeypatch.setattr(
        claude_router, "session_transcript_path", lambda _c: None
    )
    monkeypatch.setattr(claude_router.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        claude_router, "set_synchronized_output", lambda _e: False
    )
    monkeypatch.setattr(claude_router, "stop_for_handoff", lambda _c: None)
    monkeypatch.setattr(
        claude_router.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append(command)
        or Child(running=len(launches) == 1),
    )
    monkeypatch.setattr(
        claude_router,
        "read_router_state",
        lambda _path: {
            "session_id": session_id,
            "model": "Opus 4.5" if len(launches) <= 1 else "Fable 5",
        },
    )

    assert claude_router.run_supervised("/real/claude", []) == 0

    assert len(launches) == 2
    assert claude_router.option_value(launches[0], "--model") == "opus"
    assert claude_router.option_value(launches[1], "--model") == "fable"
    assert launches[1][-2:] == ["--session-id", session_id]


def test_live_switch_back_to_fable_clears_the_pin(monkeypatch):
    # Opus pins the session; switching back to /model fable clears the pin so
    # normal Fable account handoff resumes.
    session_id = str(uuid.uuid4())
    selections = []
    launches = []
    handoffs = []
    polls = []
    first = {"profile": "/p/first", "label": "first",
             "email": "first@example.com", "org_uuid": "org-first"}
    second = {"profile": "/p/second", "label": "second",
              "email": "second@example.com", "org_uuid": "org-second"}

    class Child:
        def poll(self):
            polls.append(True)
            return None if len(polls) < 4 else 0

        def wait(self):
            return 0

    def select_profile(**kwargs):
        selections.append(kwargs)
        return second if kwargs.get("avoid_labels") else first

    # poll 1 renders Opus (pins); poll 2+ renders Fable (clears pin)
    def read_state(_path):
        model = "Opus 4.5" if len(polls) < 2 else "Fable 5"
        return {"session_id": session_id, "model": model}

    monkeypatch.setattr(
        claude_router.accounts,
        "load_mode_snapshot",
        lambda: ({"mode": "fable", "label": None}, (1, 1)),
    )
    monkeypatch.setattr(claude_router.accounts, "select_profile", select_profile)
    # hand off once (first -> second), then settle
    monkeypatch.setattr(
        claude_router.accounts,
        "handoff_target",
        lambda label, *_a, **_k: "second" if label == "first" else None,
    )
    monkeypatch.setattr(
        claude_router.accounts, "profile_fable_exhausted", lambda _l: False
    )
    monkeypatch.setattr(
        claude_router.accounts, "upsert_session_lease", lambda *_a: None
    )
    monkeypatch.setattr(
        claude_router.accounts, "remove_session_lease", lambda *_a: None
    )
    monkeypatch.setattr(
        claude_router,
        "initial_session_args",
        lambda args: ([*args, "--session-id", session_id], session_id),
    )
    monkeypatch.setattr(
        claude_router, "session_transcript_path", lambda _c: None
    )
    monkeypatch.setattr(claude_router.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        claude_router, "set_synchronized_output", lambda _e: False
    )
    monkeypatch.setattr(
        claude_router, "stop_for_handoff", lambda c: handoffs.append(c)
    )
    monkeypatch.setattr(
        claude_router.subprocess,
        "Popen",
        lambda command, **kwargs: launches.append(command)
        or Child(),
    )
    monkeypatch.setattr(claude_router, "read_router_state", read_state)

    assert claude_router.run_supervised("/real/claude", []) == 0

    # handoff fired only after the switch back to fable, and carried fable
    assert len(handoffs) == 1
    assert claude_router.option_value(launches[-1], "--model") == "fable"
    assert launches[-1][-2:] == ["--session-id", session_id]
