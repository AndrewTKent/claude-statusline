"""Unit tests for bin/accounts.py — pure logic only (no keychain, no network)."""

import json
import os
import re
import subprocess
import sys
import threading
import time
import types
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import accounts  # noqa: E402

NOW = datetime(2026, 7, 16, 20, 0, 0, tzinfo=timezone.utc)

PAIRS = [
    ("acme-max", "andrew.kent@acme.ai", "e1c8"),
    ("acme-work", "andrew.kent@acme.ai", "52ae"),
    ("work", "*@acme.ai", None),
    ("gmail", "user@example.com", None),
]


class TestResolveLabel:
    def test_uuid_qualified_beats_bare(self):
        assert accounts.resolve_label("andrew.kent@acme.ai", "52ae", PAIRS) == "acme-work"

    def test_uuid_qualified_exact(self):
        assert accounts.resolve_label("andrew.kent@acme.ai", "e1c8", PAIRS) == "acme-max"

    def test_bare_fallback_when_uuid_unknown(self):
        assert accounts.resolve_label("andrew.kent@acme.ai", "zzzz", PAIRS) == "work"

    def test_bare_glob(self):
        assert accounts.resolve_label("other@acme.ai", None, PAIRS) == "work"

    def test_no_match_falls_back_to_localpart(self):
        assert accounts.resolve_label("x@nowhere.io", None, PAIRS) == "x"

    def test_no_email(self):
        assert accounts.resolve_label(None, None, PAIRS) == "?"


class TestEffectivePcts:
    def test_past_reset_zeroes(self):
        # A past reset zeroes only when confirmed by a poll after it
        # (last_seen newer than the reset).
        row = {
            "five_hour_pct": 97.0,
            "five_hour_reset": (NOW - timedelta(minutes=1)).isoformat(),
            "last_seen": NOW.timestamp(),
        }
        assert accounts.effective_pcts(row, NOW)["five_hour"] == 0.0

    def test_future_reset_keeps_pct(self):
        row = {
            "five_hour_pct": 55.0,
            "five_hour_reset": (NOW + timedelta(hours=2)).isoformat(),
        }
        assert accounts.effective_pcts(row, NOW)["five_hour"] == 55.0

    def test_missing_pct_is_none(self):
        assert accounts.effective_pcts({}, NOW)["five_hour"] is None

    def test_missing_reset_keeps_pct(self):
        row = {"seven_day_pct": 31.0}
        assert accounts.effective_pcts(row, NOW)["seven_day"] == 31.0


    def test_stale_passed_reset_does_not_show_false_headroom(self):
        # Access token lapsed → poller skipped this row → its reset slid into
        # the past WITHOUT a re-poll (last_seen older than the reset). The
        # window is NOT confirmed empty; effective must return the last-known
        # pct, not 0, or it strands a switch onto a spent account.
        now = datetime(2026, 7, 20, 19, 0, tzinfo=timezone.utc)
        reset_past = datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc)
        polled_before_reset = datetime(2026, 7, 20, 17, 30, tzinfo=timezone.utc).timestamp()
        row = {
            "fable_pct": 100.0,
            "fable_reset": reset_past.isoformat(),
            "last_seen": polled_before_reset,
        }
        assert accounts.effective_pcts(row, now)["fable"] == 100.0

    def test_confirmed_reset_shows_empty(self):
        # Polled AFTER the reset (last_seen newer) → the window really rolled
        # over → 0 is correct.
        now = datetime(2026, 7, 20, 19, 0, tzinfo=timezone.utc)
        reset_past = datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc)
        polled_after_reset = datetime(2026, 7, 20, 18, 30, tzinfo=timezone.utc).timestamp()
        row = {
            "fable_pct": 100.0,
            "fable_reset": reset_past.isoformat(),
            "last_seen": polled_after_reset,
        }
        assert accounts.effective_pcts(row, now)["fable"] == 0.0


class TestBlobAccessToken:
    def test_nested(self):
        blob = json.dumps({"claudeAiOauth": {"accessToken": "tok-1"}})
        assert accounts.blob_access_token(blob) == "tok-1"

    def test_flat(self):
        assert accounts.blob_access_token(json.dumps({"accessToken": "tok-2"})) == "tok-2"

    def test_invalid(self):
        assert accounts.blob_access_token("not json") is None
        assert accounts.blob_access_token(json.dumps(["nope"])) is None


class TestCredExpiry:
    def test_blob_refresh_expiry_ms_to_seconds(self):
        blob = json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": 1784337371728}})
        assert accounts.blob_refresh_expiry(blob) == 1784337371  # ms floored to s

    def test_blob_refresh_expiry_missing(self):
        assert accounts.blob_refresh_expiry(json.dumps({"claudeAiOauth": {}})) is None
        assert accounts.blob_refresh_expiry("not json") is None

    def test_blob_refresh_expiry_flat(self):
        assert accounts.blob_refresh_expiry(json.dumps({"refreshTokenExpiresAt": 2000000000000})) == 2000000000


class TestPickRoute:
    NOW = 1_784_000_000.0
    VAULT = {"tokens": {
        "gmail": {"token": "sk-ant-gmail-tok", "expires_at": NOW + 1000},
        "ymail": {"token": "sk-ant-ymail-tok", "expires_at": NOW - 1},  # expired token
    }}

    def test_best_with_token_wins(self):
        rows = [accounts_row("alumni", 5.0), accounts_row("gmail", 20.0), accounts_row("ymail", 1.0)]
        # alumni has no token, ymail's token is expired -> gmail
        assert accounts.pick_route(rows, self.VAULT, set(), self.NOW, None) == ("gmail", "sk-ant-gmail-tok")

    def test_pin_overrides_headroom(self):
        rows = [accounts_row("alumni", 5.0), accounts_row("gmail", 90.0)]
        assert accounts.pick_route(rows, self.VAULT, set(), self.NOW, "gmail")[0] == "gmail"

    def test_excluded_skipped(self):
        rows = [accounts_row("gmail", 5.0)]
        assert accounts.pick_route(rows, self.VAULT, {"gmail"}, self.NOW, None) is None

    def test_expired_cred_row_skipped(self):
        rows = [accounts_row("gmail", 5.0, expired=True)]
        assert accounts.pick_route(rows, self.VAULT, set(), self.NOW, None) is None

    def test_no_tokens_none(self):
        rows = [accounts_row("alumni", 5.0)]
        assert accounts.pick_route(rows, self.VAULT, set(), self.NOW, None) is None


class TestPickProfileRoute:
    def test_best_eligible_account_wins(self):
        rows = [accounts_row("gmail", 5.0), accounts_row("work", 20.0)]
        assert accounts.pick_profile_route(rows, set(), None) == "gmail"

    def test_capped_and_excluded_accounts_are_skipped(self):
        rows = [
            accounts_row("weekly-wall", 1.0, seven_day=100.0),
            accounts_row("excluded", 2.0),
            accounts_row("work", 20.0),
        ]
        assert accounts.pick_profile_route(rows, {"excluded"}, None) == "work"

    def test_pin_overrides_headroom_but_not_expired_login(self):
        rows = [accounts_row("gmail", 5.0), accounts_row("work", 90.0)]
        assert accounts.pick_profile_route(rows, set(), "work") == "work"
        rows[1]["expired"] = True
        assert accounts.pick_profile_route(rows, set(), "work") is None


class TestMergeTokenVaults:
    def test_union(self):
        a = {"tokens": {"gmail": {"token": "g", "minted_at": 1}}}
        b = {"tokens": {"ymail": {"token": "y", "minted_at": 2}}}
        assert set(accounts.merge_token_vaults(a, b)["tokens"]) == {"gmail", "ymail"}

    def test_newer_mint_wins(self):
        a = {"tokens": {"gmail": {"token": "old", "minted_at": 1}}}
        b = {"tokens": {"gmail": {"token": "new", "minted_at": 9}}}
        assert accounts.merge_token_vaults(a, b)["tokens"]["gmail"]["token"] == "new"
        assert accounts.merge_token_vaults(b, a)["tokens"]["gmail"]["token"] == "new"

    def test_empty_sides(self):
        assert accounts.merge_token_vaults({}, {})["tokens"] == {}


class TestTokenVault:
    def test_round_trip_and_perms(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "TOKEN_VAULT_PATH", tmp_path / "sub" / "vault.json")
        vault = {"version": 1, "tokens": {"x": {"token": "sk-ant-t", "expires_at": 2}}}
        accounts.save_token_vault(vault)
        assert accounts.load_token_vault() == vault
        assert (accounts.TOKEN_VAULT_PATH.stat().st_mode & 0o777) == 0o600
        assert (accounts.TOKEN_VAULT_PATH.parent.stat().st_mode & 0o777) == 0o700

    def test_missing_file_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "TOKEN_VAULT_PATH", tmp_path / "none.json")
        assert accounts.load_token_vault() == {"version": 1, "tokens": {}}


class TestAccountEligibility:
    NOW = 2_000_000_000.0

    def _blob(self, atok, rt_exp_ms):
        return json.dumps({"claudeAiOauth": {"accessToken": atok, "refreshToken": "r",
                                             "refreshTokenExpiresAt": rt_exp_ms}})

    def test_blob_expired_by_refresh_expiry(self):
        assert accounts.blob_expired(self._blob("a", 1_000_000_000_000), self.NOW) is True   # 2001, past
        assert accounts.blob_expired(self._blob("a", 3_000_000_000_000), self.NOW) is False  # 2065, future
        assert accounts.blob_expired('{"claudeAiOauth":{}}', self.NOW) is True    # no refresh token at all
        assert accounts.blob_expired("", self.NOW) is True                        # unparseable
        # regression: cc's rotation rewrites omit refreshTokenExpiresAt — alive, not dead
        rotation = json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}})
        assert accounts.blob_expired(rotation, self.NOW) is False

    def test_rate_eligible_needs_both_windows(self):
        assert accounts._rate_eligible(50.0, 50.0) is True
        assert accounts._rate_eligible(96.0, 50.0) is False   # 5h capped
        assert accounts._rate_eligible(50.0, 96.0) is False   # weekly capped
        assert accounts._rate_eligible(None, 50.0) is False   # unknown 5h
        assert accounts._rate_eligible(50.0, None) is False   # unknown weekly


def accounts_row(label, five_hour, expired=False, active=False, fable=0.0, seven_day=0.0):
    return {"label": label, "email": f"{label}@x", "five_hour": five_hour,
            "seven_day": seven_day, "fable": fable, "expired": expired, "active": active}


class TestUsageMapping:
    USAGE = {
        "five_hour": {"utilization": 42, "resets_at": "2026-07-17T22:00:00Z"},
        "seven_day": {"utilization": 18, "resets_at": "2026-07-23T10:00:00Z"},
        "limits": [
            {"kind": "weekly_scoped", "percent": 7, "resets_at": "2026-07-23T10:00:00Z",
             "scope": {"model": {"display_name": "Fable"}}},
            {"kind": "five_hour", "percent": 99},
        ],
    }

    def test_maps_all_fields(self):
        row = accounts.usage_to_reset_row("e@x.io", "org1", self.USAGE, 1784300000)
        assert row["email"] == "e@x.io" and row["org_uuid"] == "org1"
        assert row["five_hour_pct"] == 42
        assert row["five_hour_reset"] == "2026-07-17T22:00:00Z"
        assert row["seven_day_pct"] == 18
        assert row["fable_pct"] == 7
        assert row["fable_label"] == "Fable"
        assert row["last_seen"] == 1784300000

    def test_missing_weekly_is_none(self):
        row = accounts.usage_to_reset_row("e@x.io", "org1", {"five_hour": {}, "seven_day": {}}, 1)
        assert row["fable_pct"] is None and row["fable_label"] is None
        assert row["five_hour_pct"] == 0  # None utilization -> 0

    def test_access_expiry_ms_to_s(self):
        blob = json.dumps({"claudeAiOauth": {"expiresAt": 1784337371728}})
        assert accounts.blob_access_expiry(blob) == 1784337371

    def test_access_expiry_missing(self):
        assert accounts.blob_access_expiry(json.dumps({"claudeAiOauth": {}})) is None


def _live_blob(atok, future_ms=3_000_000_000_000):
    # access + refresh both far-future: poll won't skip it, cred isn't expired
    return json.dumps({"claudeAiOauth": {"accessToken": atok, "refreshToken": "r",
                                         "expiresAt": future_ms, "refreshTokenExpiresAt": future_ms}})


class TestLockedReentrant:
    def test_nested_locked_does_not_deadlock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "_lock_depth", 0)
        done = threading.Event()

        def run():
            with accounts.locked():
                with accounts.locked():  # nested acquire must return, not block
                    pass
            done.set()

        threading.Thread(target=run, daemon=True).start()
        assert done.wait(timeout=5), "nested locked() deadlocked"


class TestLegacyRouteCleanup:
    TARGET = f"gui/{os.getuid()}/com.claude-accounts-route"
    RUN_KWARGS = {"capture_output": True, "text": True, "timeout": 5}

    def _plist(self, tmp_path, monkeypatch, *, create=True):
        plist = tmp_path / "com.claude-accounts-route.plist"
        if create:
            plist.write_text("legacy")
        monkeypatch.setattr(accounts, "LEGACY_ROUTE_AGENT_PATH", plist)
        monkeypatch.setattr(accounts.sys, "platform", "darwin")
        return plist

    def test_removes_plist_before_bootout(self, tmp_path, monkeypatch):
        plist = self._plist(tmp_path, monkeypatch)
        calls = []

        def fake_run(command, **kwargs):
            assert not plist.exists()
            calls.append((command, kwargs))
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(accounts.subprocess, "run", fake_run)

        accounts.retire_legacy_route_agent()

        assert calls == [
            (["launchctl", "print", self.TARGET], self.RUN_KWARGS),
            (["launchctl", "bootout", self.TARGET], self.RUN_KWARGS),
        ]

    def test_loaded_agent_without_plist_is_still_stopped(self, tmp_path, monkeypatch):
        self._plist(tmp_path, monkeypatch, create=False)
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(accounts.subprocess, "run", fake_run)

        accounts.retire_legacy_route_agent()

        assert calls == [
            (["launchctl", "print", self.TARGET], self.RUN_KWARGS),
            (["launchctl", "bootout", self.TARGET], self.RUN_KWARGS),
        ]

    def test_absent_agent_only_probes(self, tmp_path, monkeypatch):
        self._plist(tmp_path, monkeypatch, create=False)
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return types.SimpleNamespace(returncode=113)

        monkeypatch.setattr(accounts.subprocess, "run", fake_run)

        accounts.retire_legacy_route_agent()

        assert calls == [(["launchctl", "print", self.TARGET], self.RUN_KWARGS)]

    def test_bootout_failure_is_retried(self, tmp_path, monkeypatch):
        self._plist(tmp_path, monkeypatch)
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return types.SimpleNamespace(returncode=0 if command[1] == "print" else 5)

        monkeypatch.setattr(accounts.subprocess, "run", fake_run)

        accounts.retire_legacy_route_agent()
        accounts.retire_legacy_route_agent()

        assert [command[1] for command, _ in calls] == [
            "print",
            "bootout",
            "print",
            "bootout",
        ]

    def test_probe_timeout_is_reported_and_retried(self, tmp_path, monkeypatch, capsys):
        self._plist(tmp_path, monkeypatch, create=False)
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        monkeypatch.setattr(accounts.subprocess, "run", fake_run)

        accounts.retire_legacy_route_agent()
        accounts.retire_legacy_route_agent()

        assert calls == [
            (["launchctl", "print", self.TARGET], self.RUN_KWARGS),
            (["launchctl", "print", self.TARGET], self.RUN_KWARGS),
        ]
        assert capsys.readouterr().err.count("could not inspect retired route agent") == 2

    def test_unlink_error_does_not_block_bootout(self, tmp_path, monkeypatch, capsys):
        plist = self._plist(tmp_path, monkeypatch)
        calls = []

        original_unlink = Path.unlink

        def fail_unlink(path, *args, **kwargs):
            if path == plist:
                raise PermissionError("denied")
            return original_unlink(path, *args, **kwargs)

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(Path, "unlink", fail_unlink)
        monkeypatch.setattr(accounts.subprocess, "run", fake_run)

        accounts.retire_legacy_route_agent()

        assert [command[1] for command, _ in calls] == ["print", "bootout"]
        assert "could not remove retired route agent" in capsys.readouterr().err

    def test_non_darwin_does_not_touch_plist(self, tmp_path, monkeypatch):
        plist = self._plist(tmp_path, monkeypatch)
        monkeypatch.setattr(accounts.sys, "platform", "linux")
        monkeypatch.setattr(
            accounts.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail("launchctl called"),
        )

        accounts.retire_legacy_route_agent()

        assert plist.exists()

    def test_main_retires_agent_before_parsing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            accounts,
            "retire_legacy_route_agent",
            lambda: calls.append(True),
            raising=False,
        )

        with pytest.raises(SystemExit):
            accounts.main(["route"])

        assert calls == [True]


class TestCmdSet:
    def _paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(accounts, "BLOBS_PATH", tmp_path / "blobs.json")
        monkeypatch.setattr(accounts, "PROFILES_PATH", tmp_path / "profiles")
        monkeypatch.setattr(accounts, "CLAUDE_HOME", tmp_path / "claude")
        monkeypatch.setattr(accounts, "CLAUDE_STATE_PATH", tmp_path / ".claude.json")
        monkeypatch.setattr(accounts, "_lock_depth", 0)
        accounts.CLAUDE_HOME.mkdir()
        accounts.CLAUDE_STATE_PATH.write_text('{"hasCompletedOnboarding": true}')

    def test_refuses_expired_blob(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        expired = _live_blob("a", future_ms=1_000_000_000_000)  # 2001, past
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {"B": {"blob": expired}}})
        with pytest.raises(SystemExit):
            accounts.cmd_set(types.SimpleNamespace(label="B"))
        assert not accounts.MODE_PATH.exists()

    def test_pins_native_profile_without_touching_global_credentials(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        live = _live_blob("a")
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {"B": {"blob": live}}})
        accounts.cmd_set(types.SimpleNamespace(label="B"))
        assert json.loads(accounts.MODE_PATH.read_text()) == {"mode": "set", "label": "B"}
        assert (accounts.PROFILES_PATH / "B" / ".credentials.json").read_text() == live
        assert not accounts.CRED_FILE.exists()


class TestCmdStatus:
    def test_captures_default_profile_login_before_rendering(self, monkeypatch, capsys):
        blobs = {"version": 1, "accounts": {}}
        captured = []
        monkeypatch.setattr(accounts, "locked", lambda blocking=True: nullcontext())
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        monkeypatch.setattr(
            accounts,
            "capture_live_to_blobs",
            lambda value: captured.append(value) or "gmail",
        )
        monkeypatch.setattr(accounts, "sync_profile_credentials", lambda value, persist: set())
        monkeypatch.setattr(accounts, "load_mode", lambda: {"mode": "auto", "label": None})
        monkeypatch.setattr(accounts, "route_rows", lambda value, active, now: [])
        monkeypatch.setattr(accounts, "excluded_labels", set)

        accounts.cmd_status(types.SimpleNamespace())

        assert captured == [blobs]
        assert "mode: AUTO" in capsys.readouterr().out

    def test_marks_excluded_accounts(self, monkeypatch, capsys):
        monkeypatch.setattr(accounts, "locked", lambda blocking=True: nullcontext())
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {}})
        monkeypatch.setattr(accounts, "capture_live_to_blobs", lambda blobs: None)
        monkeypatch.setattr(accounts, "sync_profile_credentials", lambda blobs, persist: set())
        monkeypatch.setattr(accounts, "load_mode", lambda: {"mode": "auto", "label": None})
        monkeypatch.setattr(
            accounts,
            "route_rows",
            lambda blobs, active, now: [accounts_row("gmail", 10.0, seven_day=20.0)],
        )
        monkeypatch.setattr(accounts, "excluded_labels", lambda: {"gmail"})

        accounts.cmd_status(types.SimpleNamespace())

        output = capsys.readouterr().out
        assert "gmail" in output
        assert "[excluded]" in output


class TestNativeProfiles:
    def _paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "PROFILES_PATH", tmp_path / "profiles")
        monkeypatch.setattr(accounts, "CLAUDE_HOME", tmp_path / "claude")
        monkeypatch.setattr(accounts, "CLAUDE_STATE_PATH", tmp_path / ".claude.json")
        accounts.CLAUDE_HOME.mkdir()
        (accounts.CLAUDE_HOME / "projects").mkdir()
        (accounts.CLAUDE_HOME / "settings.json").write_text("{}")
        accounts.CLAUDE_STATE_PATH.write_text(json.dumps({
            "hasCompletedOnboarding": True,
            "modelAccessCache": ["fable"],
            "oauthAccount": {"emailAddress": "wrong@example.com"},
            "projects": {"/repo": {"hasTrustDialogAccepted": True}},
            "userID": "wrong-user",
        }))

    def test_creates_isolated_native_profile_with_shared_session_state(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        blob = _live_blob("gmail")
        profile = accounts.ensure_native_profile("gmail", {"blob": blob})

        assert (profile / ".credentials.json").read_text() == blob
        assert (profile.stat().st_mode & 0o777) == 0o700
        assert ((profile / ".credentials.json").stat().st_mode & 0o777) == 0o600
        assert (profile / "projects").resolve() == (accounts.CLAUDE_HOME / "projects").resolve()
        state = json.loads((profile / ".claude.json").read_text())
        assert state["hasCompletedOnboarding"] is True
        assert state["projects"]["/repo"]["hasTrustDialogAccepted"] is True
        assert "oauthAccount" not in state
        assert "modelAccessCache" not in state
        assert "userID" not in state

    def test_existing_profile_credential_is_authoritative(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        first = _live_blob("first")
        second = _live_blob("second")
        profile = accounts.ensure_native_profile("gmail", {"blob": first})
        accounts._write_0600(profile / ".credentials.json", second)
        accounts.ensure_native_profile("gmail", {"blob": first})
        assert (profile / ".credentials.json").read_text() == second

    def test_invalid_profile_credential_is_restored_from_store(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        stored = _live_blob("stored")
        profile = accounts.ensure_native_profile("gmail", {"blob": stored})
        accounts._write_0600(profile / ".credentials.json", "truncated-json")

        accounts.ensure_native_profile("gmail", {"blob": stored})

        assert (profile / ".credentials.json").read_text() == stored

    def test_syncs_claude_rotated_profile_credential(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        old = _live_blob("old")
        new = _live_blob("new")
        profile = accounts.ensure_native_profile("gmail", {"blob": old})
        accounts._write_0600(profile / ".credentials.json", new)
        blobs = {
            "accounts": {
                "gmail": {
                    "blob": old,
                    "email": "same@example.com",
                    "org_uuid": "same-org",
                }
            }
        }
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda token: {
                "account": {"email": "same@example.com"},
                "organization": {"uuid": "same-org"},
            },
        )

        assert accounts.sync_profile_credentials(blobs, persist=False) == set()
        assert blobs["accounts"]["gmail"]["blob"] == new

    @pytest.mark.parametrize(
        ("actual_email", "actual_org"),
        [
            ("other@example.com", "same-org"),
            ("same@example.com", "other-org"),
        ],
    )
    def test_rejects_rotated_credential_from_a_different_identity(
        self,
        tmp_path,
        monkeypatch,
        capsys,
        actual_email,
        actual_org,
    ):
        self._paths(tmp_path, monkeypatch)
        old = _live_blob("old")
        new = _live_blob("new")
        profile = accounts.ensure_native_profile("gmail", {"blob": old})
        accounts._write_0600(profile / ".credentials.json", new)
        blobs = {
            "accounts": {
                "gmail": {
                    "blob": old,
                    "email": "same@example.com",
                    "org_uuid": "same-org",
                }
            }
        }
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda token: {
                "account": {"email": actual_email},
                "organization": {"uuid": actual_org},
            },
        )

        assert accounts.sync_profile_credentials(blobs, persist=False) == set()
        assert blobs["accounts"]["gmail"]["blob"] == old
        assert (profile / ".credentials.json").read_text() == old
        assert "gmail profile login does not match its stored identity" in capsys.readouterr().err

    def test_does_not_persist_an_unverified_rotation(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        old = _live_blob("old")
        new = _live_blob("new")
        profile = accounts.ensure_native_profile("gmail", {"blob": old})
        accounts._write_0600(profile / ".credentials.json", new)
        blobs = {
            "accounts": {
                "gmail": {
                    "blob": old,
                    "email": "same@example.com",
                    "org_uuid": "same-org",
                }
            }
        }
        monkeypatch.setattr(accounts, "fetch_profile", lambda token: None)

        assert accounts.sync_profile_credentials(blobs, persist=False) == {"gmail"}
        assert blobs["accounts"]["gmail"]["blob"] == old
        assert (profile / ".credentials.json").read_text() == new

    def test_establishes_missing_stored_identity_before_accepting_rotation(
        self, tmp_path, monkeypatch
    ):
        self._paths(tmp_path, monkeypatch)
        old = _live_blob("old")
        new = _live_blob("new")
        profile = accounts.ensure_native_profile("gmail", {"blob": old})
        accounts._write_0600(profile / ".credentials.json", new)
        blobs = {"accounts": {"gmail": {"blob": old}}}

        def profile_for(token):
            if token == "old":
                return {
                    "account": {"email": "old@example.com"},
                    "organization": {"uuid": "old-org"},
                }
            return {
                "account": {"email": "other@example.com"},
                "organization": {"uuid": "other-org"},
            }

        monkeypatch.setattr(accounts, "fetch_profile", profile_for)

        assert accounts.sync_profile_credentials(blobs, persist=False) == set()
        assert blobs["accounts"]["gmail"] == {"blob": old}
        assert (profile / ".credentials.json").read_text() == old

    @pytest.mark.parametrize("stored_blob", ["", "truncated-json"])
    def test_does_not_restore_an_unusable_stored_credential(
        self, tmp_path, monkeypatch, stored_blob
    ):
        self._paths(tmp_path, monkeypatch)
        new = _live_blob("new")
        profile = accounts.native_profile_path("gmail")
        profile.mkdir(parents=True)
        accounts._write_0600(profile / ".credentials.json", new)
        blobs = {
            "accounts": {
                "gmail": {
                    "blob": stored_blob,
                    "email": "old@example.com",
                    "org_uuid": "old-org",
                }
            }
        }
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda token: {
                "account": {"email": "other@example.com"},
                "organization": {"uuid": "other-org"},
            },
        )

        assert accounts.sync_profile_credentials(blobs, persist=False) == {"gmail"}
        assert (profile / ".credentials.json").read_text() == new


class TestLoadBlobs:
    def test_corrupt_store_is_preserved_not_destroyed(self, tmp_path, monkeypatch):
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text("NOT JSON {{{")
        monkeypatch.setattr(accounts, "BLOBS_PATH", blobs_path)
        monkeypatch.setattr(accounts, "MIRROR_LOG", tmp_path / "log")  # keep log_line off real paths
        assert accounts.load_blobs() == {"version": 1, "accounts": {}}
        backups = list(tmp_path.glob("blobs.json.corrupt.*"))
        assert len(backups) == 1 and backups[0].read_text() == "NOT JSON {{{"
        # still-corrupt on the next read: no second identical backup piles up
        assert accounts.load_blobs() == {"version": 1, "accounts": {}}
        assert len(list(tmp_path.glob("blobs.json.corrupt.*"))) == 1

    def test_missing_store_returns_empty_without_backup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "BLOBS_PATH", tmp_path / "nope.json")
        assert accounts.load_blobs() == {"version": 1, "accounts": {}}
        assert not list(tmp_path.glob("*.corrupt.*"))

    def test_unreadable_store_refuses_instead_of_empty(self, tmp_path, monkeypatch):
        # present-but-unreadable (perms) must NOT read as empty — the next capture
        # would overwrite the store with a single account.
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text('{"version": 1, "accounts": {"a": {}, "b": {}}}')
        blobs_path.chmod(0o000)
        monkeypatch.setattr(accounts, "BLOBS_PATH", blobs_path)
        try:
            with pytest.raises(accounts.AccountsError):
                accounts.load_blobs()
        finally:
            blobs_path.chmod(0o600)

    def test_non_dict_store_is_preserved_and_empty(self, tmp_path, monkeypatch):
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text("[1, 2, 3]")
        monkeypatch.setattr(accounts, "BLOBS_PATH", blobs_path)
        monkeypatch.setattr(accounts, "MIRROR_LOG", tmp_path / "log")
        assert accounts.load_blobs() == {"version": 1, "accounts": {}}
        assert len(list(tmp_path.glob("blobs.json.corrupt.*"))) == 1


class TestLiveCred:
    def test_live_cred_prefers_keychain_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        accounts.CRED_FILE.write_text("FILE-BLOB")
        monkeypatch.setattr(accounts, "kc_read", lambda service, account=None: "KC-BLOB")
        assert accounts.live_cred() == ("KC-BLOB", "keychain")

    def test_live_cred_falls_back_to_file_then_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        monkeypatch.setattr(accounts, "kc_read", lambda service, account=None: None)
        assert accounts.live_cred() == (None, None)
        accounts.CRED_FILE.write_text("FILE-BLOB")
        assert accounts.live_cred() == ("FILE-BLOB", "file")

    def test_capture_reads_the_keychain_source(self, tmp_path, monkeypatch):
        # cc refreshed into the keychain (file deleted): capture must still
        # attribute the live account without any profile fetch when unchanged.
        monkeypatch.setattr(accounts, "CRED_FILE", tmp_path / ".credentials.json")
        kc_blob = _live_blob("tok-kc")
        monkeypatch.setattr(accounts, "kc_read", lambda service, account=None: kc_blob)
        monkeypatch.setattr(accounts, "fetch_profile",
                            lambda tok: (_ for _ in ()).throw(AssertionError("no network")))
        blobs = {"accounts": {"gmail": {"blob": kc_blob}}}
        assert accounts.capture_live_to_blobs(blobs) == "gmail"



class TestLoadModeFable:
    def test_fable_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        accounts.save_mode("fable", None)
        assert accounts.load_mode() == {"mode": "fable", "label": None}

    def test_unknown_mode_defaults_to_auto(self, tmp_path, monkeypatch):
        p = tmp_path / "mode.json"
        p.write_text(json.dumps({"mode": "bogus", "label": None}))
        monkeypatch.setattr(accounts, "MODE_PATH", p)
        assert accounts.load_mode() == {"mode": "auto", "label": None}


class TestFableEligible:
    # signature: fable_eligible(five_hour, seven_day, fable)
    def test_all_under_cap(self):
        assert accounts.fable_eligible(30.0, 30.0, 40.0) is True

    def test_boundary_just_under_cap(self):
        assert accounts.fable_eligible(94.0, 94.0, 94.0) is True

    def test_fable_none_ineligible(self):
        assert accounts.fable_eligible(10.0, 10.0, None) is False

    def test_fable_at_cap_ineligible(self):
        assert accounts.fable_eligible(10.0, 10.0, 95.0) is False  # cap is exclusive

    def test_fable_over_cap_ineligible(self):
        assert accounts.fable_eligible(10.0, 10.0, 100.0) is False

    def test_five_hour_none_ineligible(self):
        assert accounts.fable_eligible(None, 10.0, 10.0) is False

    def test_five_hour_at_cap_ineligible(self):
        assert accounts.fable_eligible(95.0, 10.0, 10.0) is False  # 5h floor

    def test_seven_day_none_ineligible(self):
        assert accounts.fable_eligible(10.0, None, 10.0) is False

    def test_seven_day_at_cap_ineligible(self):
        # the trap: fresh fable but the weekly (all-models) window is maxed
        assert accounts.fable_eligible(10.0, 95.0, 10.0) is False

    def test_fable_headroom_useless_when_weekly_capped(self):
        # 0% fable used but weekly maxed → can't make requests → not usable
        assert accounts.fable_eligible(10.0, 100.0, 0.0) is False

    def test_fable_headroom_useless_when_5h_capped(self):
        assert accounts.fable_eligible(100.0, 10.0, 0.0) is False



class TestPickEnvFable:
    NOW = 1_784_000_000.0
    VAULT = {"tokens": {
        "gmail": {"token": "sk-gmail", "expires_at": NOW + 1000},
        "alumni": {"token": "sk-alumni", "expires_at": NOW + 1000},
        "ymail": {"token": "sk-ymail", "expires_at": NOW + 1000},
    }}

    def test_fable_first_orders_eligible_by_fable(self):
        rows = [accounts_row("gmail", 10.0, fable=80.0),
                accounts_row("alumni", 50.0, fable=10.0),
                accounts_row("ymail", 20.0, fable=40.0)]
        assert [r["label"] for r in accounts._fable_first(rows)] == ["alumni", "ymail", "gmail"]

    def test_prefers_fable_over_headroom(self):
        # gmail is best 5h (5) but fable-capped; alumni worse 5h (50) but fable @10.
        rows = [accounts_row("gmail", 5.0, fable=100.0), accounts_row("alumni", 50.0, fable=10.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), self.VAULT, set(), self.NOW, None)
        assert picked == ("alumni", "sk-alumni")

    def test_falls_back_to_headroom_when_none_eligible(self):
        # all fable-capped → _fable_first keeps headroom order → best 5h (gmail) wins
        rows = [accounts_row("gmail", 5.0, fable=100.0), accounts_row("alumni", 50.0, fable=100.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), self.VAULT, set(), self.NOW, None)
        assert picked == ("gmail", "sk-gmail")

    def test_skips_eligible_without_token(self):
        # alumni is the freshest fable but has NO token → fall through to the next
        # eligible tokened account (ymail).
        vault = {"tokens": {"ymail": {"token": "sk-ymail", "expires_at": self.NOW + 1000}}}
        rows = [accounts_row("alumni", 20.0, fable=10.0), accounts_row("ymail", 30.0, fable=40.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), vault, set(), self.NOW, None)
        assert picked == ("ymail", "sk-ymail")

    def test_skips_weekly_maxed_fable_account(self):
        # gmail: freshest fable but weekly (7d) maxed → _fable_first drops it below
        # alumni (headroom on fable AND weekly), so pick_route lands on alumni.
        rows = [accounts_row("gmail", 0.0, fable=8.0, seven_day=100.0),
                accounts_row("alumni", 34.0, fable=41.0, seven_day=52.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), self.VAULT, set(), self.NOW, None)
        assert picked == ("alumni", "sk-alumni")


class TestRouteRowsStale:
    def test_stale_flag_sourced_from_last_seen(self, monkeypatch):
        now = 2_000_000_000.0
        monkeypatch.setattr(accounts, "load_resets", lambda: {
            "fresh@x|o1": {"five_hour_pct": 10.0, "last_seen": now - 60},
            "old@x|o2": {"five_hour_pct": 20.0, "last_seen": now - accounts.STALE_AFTER_S - 1},
        })
        blobs = {"accounts": {
            "fresh": {"blob": _live_blob("f"), "email": "fresh@x", "org_uuid": "o1"},
            "old": {"blob": _live_blob("o"), "email": "old@x", "org_uuid": "o2"},
        }}
        rows = accounts.route_rows(blobs, None, now)
        by_label = {r["label"]: r for r in rows}
        assert by_label["fresh"]["stale"] is False
        assert by_label["old"]["stale"] is True

    def test_stale_false_when_never_seen(self, monkeypatch):
        monkeypatch.setattr(accounts, "load_resets", lambda: {})
        blobs = {"accounts": {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}}
        rows = accounts.route_rows(blobs, None, time.time())
        assert rows[0]["stale"] is False


class TestCmdPickEnv:
    """The shell hook emits an isolated native subscription profile."""

    RESET_ENV = (
        "unset CLAUDE_CODE_OAUTH_TOKEN\n"
        "unset CLAUDE_CONFIG_DIR\n"
        "unset ACCOUNTS_ROUTED_LABEL\n"
        "unset ACCOUNTS_ROUTED_EMAIL\n"
        "unset ACCOUNTS_ROUTED_ORG_UUID\n"
    )

    def _wire(
        self,
        tmp_path,
        monkeypatch,
        blobs,
        *,
        resets=None,
        excludes=frozenset(),
        mode="auto",
        mode_label=None,
    ):
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": blobs})
        monkeypatch.setattr(accounts, "load_resets", lambda: resets or {})
        monkeypatch.setattr(accounts, "excluded_labels", lambda: set(excludes))
        monkeypatch.setattr(accounts, "load_mode", lambda: {"mode": mode, "label": mode_label})
        monkeypatch.setattr(accounts, "PROFILES_PATH", tmp_path / "profiles")
        monkeypatch.setattr(accounts, "CLAUDE_HOME", tmp_path / "claude")
        monkeypatch.setattr(accounts, "CLAUDE_STATE_PATH", tmp_path / ".claude.json")
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "_lock_depth", 0)
        accounts.CLAUDE_HOME.mkdir()
        accounts.CLAUDE_STATE_PATH.write_text('{"hasCompletedOnboarding": true}')
        monkeypatch.delenv("ACCOUNTS_PIN", raising=False)

    def test_roster_from_blobs_store(self, tmp_path, monkeypatch, capsys):
        blobs = {
            "gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"},
            "work": {"blob": _live_blob("w"), "email": "w@x", "org_uuid": "o2"},
        }
        resets = {
            "g@x|o1": {"five_hour_pct": 60.0, "seven_day_pct": 20.0},
            "w@x|o2": {"five_hour_pct": 10.0, "seven_day_pct": 20.0},
        }
        self._wire(tmp_path, monkeypatch, blobs, resets=resets)
        accounts.cmd_pick_env(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "export CLAUDE_CODE_OAUTH_TOKEN" not in out
        assert f"export CLAUDE_CONFIG_DIR={tmp_path / 'profiles' / 'work'}" in out
        assert "export ACCOUNTS_ROUTED_LABEL=work" in out
        assert "export ACCOUNTS_ROUTED_EMAIL=w@x" in out

    def test_excluded_label_respected(self, tmp_path, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        resets = {"g@x|o1": {"five_hour_pct": 1.0, "seven_day_pct": 1.0}}
        self._wire(tmp_path, monkeypatch, blobs, resets=resets, excludes={"gmail"})
        accounts.cmd_pick_env(types.SimpleNamespace())
        assert capsys.readouterr().out == self.RESET_ENV

    def test_pin_overrides_headroom(self, tmp_path, monkeypatch, capsys):
        blobs = {
            "gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"},
            "work": {"blob": _live_blob("w"), "email": "w@x", "org_uuid": "o2"},
        }
        resets = {
            "g@x|o1": {"five_hour_pct": 5.0, "seven_day_pct": 5.0},
            "w@x|o2": {"five_hour_pct": 90.0, "seven_day_pct": 90.0},
        }
        self._wire(tmp_path, monkeypatch, blobs, resets=resets)
        monkeypatch.setenv("ACCOUNTS_PIN", "work")
        accounts.cmd_pick_env(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "export ACCOUNTS_ROUTED_LABEL=work" in out

    def test_set_mode_is_a_persistent_pin(self, tmp_path, monkeypatch, capsys):
        blobs = {
            "gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"},
            "work": {"blob": _live_blob("w"), "email": "w@x", "org_uuid": "o2"},
        }
        resets = {
            "g@x|o1": {"five_hour_pct": 5.0, "seven_day_pct": 5.0},
            "w@x|o2": {"five_hour_pct": 90.0, "seven_day_pct": 90.0},
        }
        self._wire(
            tmp_path,
            monkeypatch,
            blobs,
            resets=resets,
            mode="set",
            mode_label="work",
        )
        accounts.cmd_pick_env(types.SimpleNamespace())
        assert "export ACCOUNTS_ROUTED_LABEL=work" in capsys.readouterr().out

    def test_expired_blob_filtered(self, tmp_path, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g", future_ms=1_000_000_000_000),  # 2001, past
                           "email": "g@x", "org_uuid": "o1"}}
        self._wire(tmp_path, monkeypatch, blobs)
        accounts.cmd_pick_env(types.SimpleNamespace())
        assert capsys.readouterr().out == self.RESET_ENV

    def test_no_minted_setup_token_is_required(self, tmp_path, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        resets = {"g@x|o1": {"five_hour_pct": 1.0, "seven_day_pct": 1.0}}
        self._wire(tmp_path, monkeypatch, blobs, resets=resets)
        accounts.cmd_pick_env(types.SimpleNamespace())
        assert "export ACCOUNTS_ROUTED_LABEL=gmail" in capsys.readouterr().out

    def test_profile_creation_holds_accounts_lock(self, tmp_path, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        resets = {"g@x|o1": {"five_hour_pct": 1.0, "seven_day_pct": 1.0}}
        self._wire(tmp_path, monkeypatch, blobs, resets=resets)
        original = accounts.ensure_native_profile

        def checked_ensure(label, entry):
            assert accounts._lock_depth == 1
            return original(label, entry)

        monkeypatch.setattr(accounts, "ensure_native_profile", checked_ensure)
        accounts.cmd_pick_env(types.SimpleNamespace())

        assert "export ACCOUNTS_ROUTED_LABEL=gmail" in capsys.readouterr().out

    def test_persists_claude_rotated_profile_credential(self, tmp_path, monkeypatch, capsys):
        old = _live_blob("old")
        new = _live_blob("new")
        blobs = {"gmail": {"blob": old, "email": "g@x", "org_uuid": "o1"}}
        resets = {"g@x|o1": {"five_hour_pct": 1.0, "seven_day_pct": 1.0}}
        self._wire(tmp_path, monkeypatch, blobs, resets=resets)
        profile = accounts.ensure_native_profile("gmail", blobs["gmail"])
        accounts._write_0600(profile / ".credentials.json", new)
        saved = []
        monkeypatch.setattr(accounts, "save_blobs", lambda value: saved.append(value))
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda token: {
                "account": {"email": "g@x"},
                "organization": {"uuid": "o1"},
            },
        )

        accounts.cmd_pick_env(types.SimpleNamespace())

        assert saved[-1]["accounts"]["gmail"]["blob"] == new
        assert "export ACCOUNTS_ROUTED_LABEL=gmail" in capsys.readouterr().out

    def test_unverified_profile_is_not_exported(self, tmp_path, monkeypatch, capsys):
        old = _live_blob("old")
        new = _live_blob("new")
        blobs = {
            "gmail": {
                "blob": old,
                "email": "g@x",
                "org_uuid": "o1",
            }
        }
        resets = {"g@x|o1": {"five_hour_pct": 1.0, "seven_day_pct": 1.0}}
        self._wire(tmp_path, monkeypatch, blobs, resets=resets)
        profile = accounts.ensure_native_profile("gmail", blobs["gmail"])
        accounts._write_0600(profile / ".credentials.json", new)
        monkeypatch.setattr(accounts, "fetch_profile", lambda token: None)

        accounts.cmd_pick_env(types.SimpleNamespace())

        assert capsys.readouterr().out == self.RESET_ENV
        assert (profile / ".credentials.json").read_text() == new

    def test_save_failure_fails_closed(self, tmp_path, monkeypatch, capsys):
        old = _live_blob("old")
        new = _live_blob("new")
        blobs = {
            "gmail": {
                "blob": old,
                "email": "g@x",
                "org_uuid": "o1",
            }
        }
        resets = {"g@x|o1": {"five_hour_pct": 1.0, "seven_day_pct": 1.0}}
        self._wire(tmp_path, monkeypatch, blobs, resets=resets)
        profile = accounts.ensure_native_profile("gmail", blobs["gmail"])
        accounts._write_0600(profile / ".credentials.json", new)
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda token: {
                "account": {"email": "g@x"},
                "organization": {"uuid": "o1"},
            },
        )
        monkeypatch.setattr(
            accounts,
            "save_blobs",
            lambda value: (_ for _ in ()).throw(OSError("disk full")),
        )

        accounts.cmd_pick_env(types.SimpleNamespace())

        assert capsys.readouterr().out == self.RESET_ENV

    def test_internal_exception_clears_inherited_routing(self, monkeypatch, capsys):
        monkeypatch.setattr(accounts, "load_blobs",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        accounts.cmd_pick_env(types.SimpleNamespace())
        assert capsys.readouterr().out == self.RESET_ENV

    def test_fable_mode_uses_flat_row_shape(self, tmp_path, monkeypatch, capsys):
        blobs = {
            "gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"},
            "alumni": {"blob": _live_blob("b"), "email": "b@x", "org_uuid": "o2"},
        }
        resets = {
            "g@x|o1": {"five_hour_pct": 5.0, "seven_day_pct": 5.0, "fable_pct": 90.0},
            "b@x|o2": {"five_hour_pct": 50.0, "seven_day_pct": 50.0, "fable_pct": 10.0},
        }
        self._wire(tmp_path, monkeypatch, blobs, resets=resets, mode="fable")
        accounts.cmd_pick_env(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "export ACCOUNTS_ROUTED_LABEL=alumni" in out


class TestCmdLs:
    """The blobs-based board: same visual contract as the old vault board
    (marker, staleness, EXPIRED flag, footnotes), rebuilt from route_rows."""

    def _wire(
        self,
        monkeypatch,
        blobs,
        *,
        resets=None,
        active=None,
        excludes=frozenset(),
        blocked=frozenset(),
    ):
        monkeypatch.setattr(accounts, "locked", lambda blocking=True: nullcontext())
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": blobs})
        monkeypatch.setattr(accounts, "capture_live_to_blobs", lambda b: active)
        monkeypatch.setattr(accounts, "sync_profile_credentials", lambda b, persist: set(blocked))
        monkeypatch.setattr(accounts, "load_resets", lambda: resets or {})
        monkeypatch.setattr(accounts, "excluded_labels", lambda: set(excludes))

    def test_empty_store_prints_login_onboarding_not_enroll(self, monkeypatch, capsys):
        self._wire(monkeypatch, {})
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "enroll" not in out
        assert "vault" not in out.lower()
        assert "/login" in out

    def test_renders_header_and_account(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        resets = {"g@x|o1": {"five_hour_pct": 12.0, "seven_day_pct": 3.0, "fable_pct": 7.0}}
        self._wire(monkeypatch, blobs, resets=resets, active="gmail")
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "label" in out and "email" in out and "5h" in out and "7d" in out and "fable" in out
        assert "gmail" in out
        assert "* gmail" not in out
        assert "g@x" in out
        assert "12%" in out and "3%" in out and "7%" in out

    def test_expired_row_flagged_and_sorted_last(self, monkeypatch, capsys):
        blobs = {
            "dead": {"blob": _live_blob("d", future_ms=1_000_000_000_000), "email": "d@x", "org_uuid": "o1"},
            "gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o2"},
        }
        resets = {"d@x|o1": {"five_hour_pct": 1.0}, "g@x|o2": {"five_hour_pct": 50.0}}
        self._wire(monkeypatch, blobs, resets=resets)
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "EXPIRED" in out
        # dead has the lower 5h% (1 < 50) but must sort AFTER gmail (expired-last)
        assert out.index("gmail") < out.index("dead")

    def test_excluded_label_flagged(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        self._wire(monkeypatch, blobs, resets={"g@x|o1": {"five_hour_pct": 1.0}}, excludes={"gmail"})
        accounts.cmd_ls(types.SimpleNamespace())
        assert "[excluded]" in capsys.readouterr().out

    def test_unverified_label_flagged(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        self._wire(
            monkeypatch,
            blobs,
            resets={"g@x|o1": {"five_hour_pct": 1.0}},
            blocked={"gmail"},
        )
        accounts.cmd_ls(types.SimpleNamespace())
        assert "[unverified]" in capsys.readouterr().out

    def test_stale_row_marked(self, monkeypatch, capsys):
        stale_seen = time.time() - accounts.STALE_AFTER_S - 60
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        resets = {"g@x|o1": {"five_hour_pct": 20.0, "last_seen": stale_seen}}
        self._wire(monkeypatch, blobs, resets=resets)
        accounts.cmd_ls(types.SimpleNamespace())
        assert "20%~" in capsys.readouterr().out
    def test_footnotes_present(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}
        self._wire(monkeypatch, blobs, resets={"g@x|o1": {"five_hour_pct": 1.0}})
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "since that account was polled" in out
        assert "reset-aware; sorted best-first" in out

    def test_expired_footnote_wording_is_not_legacy_vault(self, monkeypatch, capsys):
        blobs = {"gmail": {"blob": _live_blob("g", future_ms=1_000_000_000_000),
                           "email": "g@x", "org_uuid": "o1"}}
        self._wire(monkeypatch, blobs, resets={"g@x|o1": {"five_hour_pct": 1.0}})
        accounts.cmd_ls(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "EXPIRED = stored refresh token dead" in out
        assert "vaulted" not in out


class TestStatuslineAccountBoard:
    def test_excluded_account_is_not_rendered(self, tmp_path):
        repo = Path(__file__).resolve().parent.parent
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "statusline.conf").write_text(
            'SHOW_ACCOUNT_RESETS=1\n'
            'MAX_COLS=200\n'
            'ACCOUNT_LABELS="acme-max:andrew.kent@acme.ai|acme-org '
            'alumni:*@alumni.example.edu"\n'
            'ACCOUNTS_EXCLUDE="alumni"\n'
        )
        resets = {
            "andrew.kent@acme.ai|acme-org": {
                "email": "andrew.kent@acme.ai",
                "org_uuid": "acme-org",
                "five_hour_pct": 18,
                "seven_day_pct": 20,
                "fable_pct": 24,
                "last_seen": time.time(),
            },
            "user@alumni.example.edu|alumni-org": {
                "email": "user@alumni.example.edu",
                "org_uuid": "alumni-org",
                "five_hour_pct": 0,
                "seven_day_pct": 0,
                "fable_pct": 0,
                "last_seen": time.time(),
            },
        }
        (claude_dir / "account-resets.json").write_text(json.dumps(resets))
        project = tmp_path / "project"
        project.mkdir()
        fixture = json.loads((repo / "test/fixtures/input.json").read_text())
        fixture["workspace"]["current_dir"] = str(project)
        script = tmp_path / "statusline.sh"
        script.write_text(
            (repo / "bin/statusline.sh")
            .read_text()
            .replace("/tmp/claude", str(tmp_path / "cache"))
        )
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "TZ": "UTC",
                "ACCOUNTS_ROUTED_LABEL": "acme-max",
                "ACCOUNTS_ROUTED_EMAIL": "andrew.kent@acme.ai",
                "ACCOUNTS_ROUTED_ORG_UUID": "acme-org",
            }
        )

        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(fixture),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        rendered = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)

        assert "Acme-Max" in rendered
        assert "alumni" not in rendered
        assert (tmp_path / "cache/statusline-usage-cache.json").is_symlink()
        assert (tmp_path / "cache/statusline-profile-cache.json").is_symlink()

    def test_in_profile_login_replaces_launch_identity_before_ledger_write(self, tmp_path):
        repo = Path(__file__).resolve().parent.parent
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "statusline.conf").write_text(
            'MAX_COLS=200\n'
            'ACCOUNT_LABELS="old:old@example.com|old-org new:new@example.com|new-org"\n'
        )
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "new-token"}})
        )
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$*\" in\n"
            "  *oauth/profile*) printf '%s' "
            "'{\"account\":{\"email\":\"new@example.com\"},"
            "\"organization\":{\"uuid\":\"new-org\"}}' ;;\n"
            "  *oauth/usage*) printf '%s' "
            "'{\"five_hour\":{\"utilization\":1},\"seven_day\":{\"utilization\":2}}' ;;\n"
            "esac\n"
        )
        fake_curl.chmod(0o755)
        project = tmp_path / "project"
        project.mkdir()
        fixture = json.loads((repo / "test/fixtures/input.json").read_text())
        fixture["workspace"]["current_dir"] = str(project)
        script = tmp_path / "statusline.sh"
        script.write_text(
            (repo / "bin/statusline.sh")
            .read_text()
            .replace("/tmp/claude", str(tmp_path / "cache"))
        )
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PATH": f"{fake_bin}:{env['PATH']}",
                "TZ": "UTC",
                "CLAUDE_CONFIG_DIR": str(profile),
                "ACCOUNTS_ROUTED_LABEL": "old",
                "ACCOUNTS_ROUTED_EMAIL": "old@example.com",
                "ACCOUNTS_ROUTED_ORG_UUID": "old-org",
            }
        )

        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(fixture),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        rendered = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
        ledger = json.loads((claude_dir / "daily-cost.json").read_text())
        session = ledger["sessions"][fixture["session_id"]]

        assert "new@example.com" in rendered
        assert "old@example.com" not in rendered
        assert session["acct"] == "new"

    def test_failed_first_profile_fetch_does_not_use_launch_identity(self, tmp_path):
        repo = Path(__file__).resolve().parent.parent
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "statusline.conf").write_text(
            'MAX_COLS=200\nACCOUNT_LABELS="old:old@example.com|old-org"\n'
        )
        profile = tmp_path / "profile"
        profile.mkdir()
        (profile / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "new-token"}})
        )
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_curl = fake_bin / "curl"
        fake_curl.write_text("#!/usr/bin/env bash\nexit 1\n")
        fake_curl.chmod(0o755)
        project = tmp_path / "project"
        project.mkdir()
        fixture = json.loads((repo / "test/fixtures/input.json").read_text())
        fixture["workspace"]["current_dir"] = str(project)
        script = tmp_path / "statusline.sh"
        script.write_text(
            (repo / "bin/statusline.sh")
            .read_text()
            .replace("/tmp/claude", str(tmp_path / "cache"))
        )
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PATH": f"{fake_bin}:{env['PATH']}",
                "TZ": "UTC",
                "CLAUDE_CONFIG_DIR": str(profile),
                "ACCOUNTS_ROUTED_LABEL": "old",
                "ACCOUNTS_ROUTED_EMAIL": "old@example.com",
                "ACCOUNTS_ROUTED_ORG_UUID": "old-org",
            }
        )

        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(fixture),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        rendered = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
        ledger = json.loads((claude_dir / "daily-cost.json").read_text())
        session = ledger["sessions"][fixture["session_id"]]

        assert "old@example.com" not in rendered
        assert "acct" not in session
