from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN_DIR))
MODULE_PATH = BIN_DIR / "codex-router.py"
SPEC = importlib.util.spec_from_file_location("codex_router", MODULE_PATH)
assert SPEC is not None
codex_router = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = codex_router
SPEC.loader.exec_module(codex_router)


class CodexRouterTest(unittest.TestCase):
    def test_management_commands_bypass_account_routing(self) -> None:
        self.assertFalse(codex_router.should_route(["login"]))
        self.assertFalse(codex_router.should_route(["mcp", "list"]))
        self.assertTrue(codex_router.should_route([]))
        self.assertTrue(codex_router.should_route(["exec", "say hi"]))
        self.assertTrue(codex_router.should_route(["resume", "thread-id"]))

    def test_resume_args_preserve_global_configuration(self) -> None:
        args = [
            "--dangerously-bypass-approvals-and-sandbox",
            "-c",
            "tui.status_line=[]",
            "-C",
            "/tmp/project",
            "resume",
            "--last",
        ]

        self.assertEqual(
            codex_router.resume_args(args, "thread-id"),
            [
                "--dangerously-bypass-approvals-and-sandbox",
                "-c",
                "tui.status_line=[]",
                "-C",
                "/tmp/project",
                "resume",
                "thread-id",
            ],
        )

    def test_routed_launch_forces_file_backed_auth(self) -> None:
        self.assertEqual(
            codex_router.with_file_auth(["exec", "say hi"]),
            ["-c", 'cli_auth_credentials_store="file"', "exec", "say hi"],
        )

    def test_real_binary_skips_router_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wrappers = root / "wrappers"
            native = root / "native"
            wrappers.mkdir()
            native.mkdir()
            (wrappers / "codex").write_text("#!/bin/sh\nexec codex-router \"$@\"\n")
            (native / "codex").write_text("#!/bin/sh\nexit 0\n")
            (wrappers / "codex").chmod(0o755)
            (native / "codex").chmod(0o755)

            with mock.patch.dict(os.environ, {"PATH": f"{wrappers}:{native}"}, clear=False):
                self.assertEqual(codex_router.codex_binary(), str(native / "codex"))

    def test_child_environment_uses_selected_profile(self) -> None:
        env = codex_router.child_environment(
            "personal",
            {"home": "/tmp/personal", "email": "dev@example.invalid"},
            Path("/tmp/state.json"),
        )

        self.assertEqual(env["CODEX_HOME"], "/tmp/personal")
        self.assertEqual(env["CODEX_ROUTED_LABEL"], "personal")
        self.assertEqual(env["CODEX_ACCOUNT_ROUTER_STATE"], "/tmp/state.json")
        self.assertNotIn("dev@example.invalid", env.values())

    def test_auto_handoff_requires_threshold_and_margin(self) -> None:
        usage = {
            "work": {"fetched_at": 1, "rate_limits": {"primary": {"used_percent": 92}}},
            "personal": {"fetched_at": 1, "rate_limits": {"primary": {"used_percent": 40}}},
        }
        with (
            mock.patch.object(codex_router.codex_accounts, "load_registry", return_value={"work": {}, "personal": {}}),
            mock.patch.object(codex_router.codex_accounts, "load_mode", return_value={"mode": "auto"}),
            mock.patch.object(codex_router.codex_accounts, "load_usage", return_value=usage),
            mock.patch.object(codex_router.codex_accounts.time, "time", return_value=1),
        ):
            self.assertEqual(codex_router.handoff_target("work"), "personal")
            usage["work"]["rate_limits"]["primary"]["used_percent"] = 89
            self.assertIsNone(codex_router.handoff_target("work"))


if __name__ == "__main__":
    unittest.main()
