from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "codex_accounts.py"
SPEC = importlib.util.spec_from_file_location("codex_accounts", MODULE_PATH)
assert SPEC is not None
codex_accounts = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = codex_accounts
SPEC.loader.exec_module(codex_accounts)


def jwt(payload: dict[str, str]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"x.{encoded}.y"


class CodexAccountsTest(unittest.TestCase):
    def test_explicit_label_preserves_capitalization(self) -> None:
        self.assertEqual(codex_accounts.clean_label("Sol"), "Sol")
        self.assertEqual(codex_accounts.clean_label("My Personal"), "My-Personal")

    def test_identity_comes_from_id_token_without_exposing_token(self) -> None:
        auth = {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": jwt({"email": "dev@example.invalid"}),
                "account_id": "account-secret",
            },
        }

        identity = codex_accounts.identity_from_auth(auth)

        self.assertEqual(identity.email, "dev@example.invalid")
        self.assertEqual(identity.default_label, "dev")
        self.assertNotIn("id_token", repr(identity))
        self.assertNotIn("account-secret", repr(identity))

    def test_profile_shares_session_state_but_isolates_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shared = root / ".codex"
            profile = root / "profiles" / "personal"
            shared.mkdir()
            (shared / "config.toml").write_text('model = "gpt-5"\n')
            (shared / "state_5.sqlite").write_text("state")
            (shared / "auth.json").write_text('{"secret": true}\n')

            codex_accounts.ensure_profile(profile, shared)

            self.assertEqual((profile / "config.toml").resolve(), (shared / "config.toml").resolve())
            self.assertEqual((profile / "state_5.sqlite").resolve(), (shared / "state_5.sqlite").resolve())
            self.assertFalse((profile / "auth.json").exists())

    def test_pick_account_uses_lowest_binding_usage(self) -> None:
        now = time.time()
        accounts = {
            "work": {"home": "/tmp/work"},
            "personal": {"home": "/tmp/personal"},
        }
        usage = {
            "work": {
                "fetched_at": now,
                "rate_limits": {
                    "primary": {"used_percent": 35},
                    "secondary": {"used_percent": 80},
                },
            },
            "personal": {
                "fetched_at": now,
                "rate_limits": {
                    "primary": {"used_percent": 45},
                    "secondary": {"used_percent": 50},
                },
            },
        }

        self.assertEqual(
            codex_accounts.pick_account(accounts, usage, {"mode": "auto"}, now=now),
            "personal",
        )

    def test_set_mode_selects_the_pin_even_when_another_account_is_fresher(self) -> None:
        now = time.time()
        accounts = {"work": {"home": "/tmp/work"}, "personal": {"home": "/tmp/personal"}}
        usage = {
            "work": {"fetched_at": now, "rate_limits": {"primary": {"used_percent": 99}}},
            "personal": {"fetched_at": now, "rate_limits": {"primary": {"used_percent": 1}}},
        }

        self.assertEqual(
            codex_accounts.pick_account(
                accounts,
                usage,
                {"mode": "set", "label": "work"},
                now=now,
            ),
            "work",
        )

    def test_poll_account_records_app_server_rate_limits(self) -> None:
        result = {
            "rateLimits": {
                "planType": "pro",
                "primary": {"usedPercent": 41, "resetsAt": 2_000_000_000},
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            (home / "auth.json").write_text("{}\n")
            with mock.patch.object(codex_accounts, "read_rate_limits", return_value=result):
                row = codex_accounts.poll_account("work", {"home": str(home)}, "/usr/bin/codex")

        self.assertEqual(row["rate_limits"]["plan_type"], "pro")
        self.assertEqual(row["rate_limits"]["primary"]["used_percent"], 41)
        self.assertNotIn("home", row)

    def test_session_hook_binds_thread_to_routed_label(self) -> None:
        hook = MODULE_PATH.with_name("codex-account-session.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "router-state.json"
            mapping = root / "thread-accounts"
            env = os.environ.copy()
            env.update(
                {
                    "CODEX_ACCOUNT_ROUTER_STATE": str(state),
                    "CODEX_ACCOUNTS_THREAD_DIR": str(mapping),
                    "CODEX_ROUTED_LABEL": "personal",
                }
            )
            payload = {
                "session_id": "019f0000-0000-7000-8000-000000000000",
                "transcript_path": "/tmp/rollout.jsonl",
                "cwd": "/tmp/project",
                "model": "gpt-5.6",
            }

            subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(payload),
                text=True,
                check=True,
                env=env,
            )

            self.assertEqual(json.loads(state.read_text())["session_id"], payload["session_id"])
            thread = json.loads((mapping / f"{payload['session_id']}.json").read_text())
            self.assertEqual(thread["label"], "personal")
            self.assertNotIn("token", json.dumps(thread).lower())


if __name__ == "__main__":
    unittest.main()
