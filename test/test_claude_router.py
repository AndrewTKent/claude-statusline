import importlib.util
import json
import os
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


def test_rendered_model_name_maps_to_a_cli_alias():
    assert claude_router.model_name([], "Opus 5 (1M context)") == "opus"
    assert claude_router.model_name([], "Fable 5.max") == "fable"


def test_running_supervisor_reexecs_new_router_code(monkeypatch):
    session_id = str(uuid.uuid4())
    selected = {
        "profile": "/profiles/first",
        "label": "first",
        "email": "first@example.com",
        "org_uuid": "org-first",
    }
    mtimes = iter([1, 2])
    exec_call = {}

    class Reloaded(Exception):
        pass

    class Child:
        def poll(self):
            return None

    monkeypatch.setattr(claude_router, "source_mtime", lambda: next(mtimes))
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
    monkeypatch.setattr(claude_router.subprocess, "Popen", lambda *_args, **_kwargs: Child())
    monkeypatch.setattr(claude_router.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        claude_router,
        "read_router_state",
        lambda _path: {"session_id": session_id, "model": "Opus 5"},
    )
    monkeypatch.setattr(claude_router, "stop_for_handoff", lambda _child: None)
    monkeypatch.setattr(claude_router, "set_synchronized_output", lambda _enabled: True)

    def fake_execv(binary, argv):
        exec_call.update({"binary": binary, "argv": argv})
        raise Reloaded

    monkeypatch.setattr(claude_router.os, "execv", fake_execv)

    with pytest.raises(Reloaded):
        claude_router.run_supervised("/real/claude", [])

    assert exec_call["binary"] == sys.executable
    assert exec_call["argv"][-4:] == [
        "--model",
        "opus",
        "--resume",
        session_id,
    ]


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
    assert calls[1][1] == [
        "/real/claude",
        "--model",
        "fable",
        "--print",
        "hello",
    ]
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
        ("select", {"require_fable": False}),
    ]
    assert calls[2][1] == [
        "/real/claude",
        "--print",
        "hello",
        "--model",
        "opus",
    ]
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


def test_statusline_publishes_the_live_session_identity(tmp_path):
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
        }
    )

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
        assert state["label"] == "first"
    finally:
        state_path.unlink(missing_ok=True)


def test_statusline_says_when_a_quota_gate_bypasses_the_pin(tmp_path):
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
    assert "pin preferred bypassed" in result.stdout


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


def test_running_session_hands_off_without_returning_to_the_shell(tmp_path):
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
                },
                "second@example.com|org-second": {
                    "five_hour_pct": 20,
                    "seven_day_pct": 20,
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
        "    json.dumps({'session_id': sid, 'model': 'Opus'})\n"
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
                    "five_hour_pct": 90,
                    "seven_day_pct": 10,
                },
                "second@example.com|org-second": {
                    "five_hour_pct": 20,
                    "seven_day_pct": 20,
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
    assert launches[0]["args"][-2] == "--session-id"
    assert launches[1]["args"][-4:] == [
        "--model",
        "opus",
        "--resume",
        first_session,
    ]


def test_fable_session_falls_back_to_opus_when_no_fable_account_is_safe(tmp_path):
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
                    "five_hour_pct": 10,
                    "seven_day_pct": 10,
                    "fable_pct": 10,
                },
                "second@example.com|org-second": {
                    "five_hour_pct": 20,
                    "seven_day_pct": 20,
                    "fable_pct": 100,
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
        "    json.dumps({'session_id': sid, 'model': model})\n"
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
                    "five_hour_pct": 90,
                    "seven_day_pct": 10,
                    "fable_pct": 90,
                },
                "second@example.com|org-second": {
                    "five_hour_pct": 20,
                    "seven_day_pct": 20,
                    "fable_pct": 100,
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
    assert launches[1]["args"][-4:] == [
        "--model",
        "opus",
        "--resume",
        first_session,
    ]
