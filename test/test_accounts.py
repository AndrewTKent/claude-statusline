"""Unit tests for bin/accounts.py — pure logic only (no keychain, no network)."""

import hashlib
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

# What the API returns for an account that turned out to be idle after all.
USAGE_OK = {
    "five_hour": {"utilization": 0, "resets_at": "2026-08-12T00:00:00Z"},
    "seven_day": {"utilization": 5, "resets_at": "2026-08-18T00:00:00Z"},
    "limits": [],
}

PAIRS = [
    ("acme-max", "jane.doe@acme.ai", "e1c8"),
    ("acme-work", "jane.doe@acme.ai", "52ae"),
    ("work", "*@acme.ai", None),
    ("gmail", "someone@gmail.com", None),
]


class TestResolveLabel:
    def test_uuid_qualified_beats_bare(self):
        assert accounts.resolve_label("jane.doe@acme.ai", "52ae", PAIRS) == "acme-work"

    def test_uuid_qualified_exact(self):
        assert accounts.resolve_label("jane.doe@acme.ai", "e1c8", PAIRS) == "acme-max"

    def test_bare_fallback_when_uuid_unknown(self):
        assert accounts.resolve_label("jane.doe@acme.ai", "zzzz", PAIRS) == "work"

    def test_bare_glob(self):
        assert accounts.resolve_label("other@acme.ai", None, PAIRS) == "work"

    def test_no_match_falls_back_to_localpart(self):
        assert accounts.resolve_label("x@nowhere.io", None, PAIRS) == "x"

    def test_no_email(self):
        assert accounts.resolve_label(None, None, PAIRS) == "?"


class TestKeychain:
    def test_read_handles_missing_security_binary(self, monkeypatch):
        def missing_binary(*_args, **_kwargs):
            raise FileNotFoundError

        monkeypatch.setattr(accounts.subprocess, "run", missing_binary)

        assert accounts.kc_read("profile-credential") is None


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


def test_hard_session_limit_setting_is_opt_in(tmp_path, monkeypatch):
    config = tmp_path / "statusline.conf"
    monkeypatch.setattr(accounts, "CONF_PATH", config)
    monkeypatch.delenv("ACCOUNTS_HARD_SESSION_LIMIT", raising=False)

    assert accounts.hard_session_limit_enabled() is False

    config.write_text("ACCOUNTS_HARD_SESSION_LIMIT=1\n")

    assert accounts.hard_session_limit_enabled() is True

    monkeypatch.setenv("ACCOUNTS_HARD_SESSION_LIMIT", "0")

    assert accounts.hard_session_limit_enabled() is False

    monkeypatch.setenv("ACCOUNTS_HARD_SESSION_LIMIT", "1")

    assert accounts.hard_session_limit_enabled() is True


@pytest.mark.parametrize(("usage", "reached"), [(99.9, False), (100.0, True)])
def test_profile_session_limit_uses_the_five_hour_boundary(
    usage,
    reached,
    monkeypatch,
):
    monkeypatch.setattr(accounts, "load_blobs", lambda: {})
    monkeypatch.setattr(
        accounts,
        "route_rows",
        lambda *_args: [{"label": "work", "five_hour": usage}],
    )

    assert accounts.profile_session_limit_reached("work") is reached


def test_hard_session_limit_bypasses_a_forced_exhausted_pin(monkeypatch):
    exhausted = {"label": "pinned"}
    safe = {"label": "safe"}
    selections = []

    def select_once(**kwargs):
        selections.append(kwargs)
        return exhausted if len(selections) == 1 else safe

    monkeypatch.setattr(accounts, "_select_profile_once", select_once)
    monkeypatch.setattr(accounts, "hard_session_limit_enabled", lambda: True)
    monkeypatch.setattr(
        accounts,
        "profile_session_limit_reached",
        lambda label: label == "pinned",
    )

    assert accounts.select_profile(force_label="pinned") == safe
    assert selections[1]["avoid_labels"] == {"pinned"}
    assert selections[1]["force_label"] is None
    assert selections[1]["ignore_policy"] is True


def test_mark_session_limit_quarantines_stale_usage_until_reset(
    tmp_path,
    monkeypatch,
):
    resets_path = tmp_path / "account-resets.json"
    limits_path = tmp_path / "session-limits.json"
    resets_path.write_text(
        json.dumps(
            {
                "work@example.com|org-work": {
                    "email": "work@example.com",
                    "org_uuid": "org-work",
                    "five_hour_pct": 15.0,
                    "five_hour_reset": "2026-07-30T04:20:00+00:00",
                    "seven_day_pct": 50.0,
                    "fable_pct": 65.0,
                    "last_seen": 100.0,
                }
            }
        )
    )
    monkeypatch.setattr(accounts, "RESETS_PATH", resets_path)
    monkeypatch.setattr(accounts, "SESSION_LIMITS_PATH", limits_path)
    monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
    monkeypatch.setattr(accounts, "_lock_depth", 0)

    accounts.mark_session_limit(
        "work@example.com",
        "org-work",
        now_ts=1_785_381_600.0,
    )

    reset_row = json.loads(resets_path.read_text())["work@example.com|org-work"]
    assert reset_row["five_hour_pct"] == 15.0
    block = json.loads(limits_path.read_text())["work@example.com|org-work"]
    assert block == {
        "detected_at": 1_785_381_600.0,
        "expires_at": 1_785_385_200.0,
    }

    reset_row["five_hour_pct"] = 20.0
    reset_row["last_seen"] = 1_785_381_700.0
    resets_path.write_text(
        json.dumps({"work@example.com|org-work": reset_row})
    )
    monkeypatch.setattr(accounts, "load_resets", lambda: json.loads(resets_path.read_text()))
    rows = accounts.route_rows(
        {
            "accounts": {
                "work": {
                    "blob": _live_blob("work"),
                    "email": "work@example.com",
                    "org_uuid": "org-work",
                }
            }
        },
        None,
        1_785_381_700.0,
    )

    assert rows[0]["five_hour"] == 100.0


def test_mark_fable_limit_only_quarantines_fable_until_its_reset(
    tmp_path,
    monkeypatch,
):
    resets_path = tmp_path / "account-resets.json"
    limits_path = tmp_path / "session-limits.json"
    resets_path.write_text(
        json.dumps(
            {
                "work@example.com|org-work": {
                    "email": "work@example.com",
                    "org_uuid": "org-work",
                    "five_hour_pct": 15.0,
                    "seven_day_pct": 50.0,
                    "fable_pct": 65.0,
                    "fable_reset": "2026-07-30T09:20:00+00:00",
                    "last_seen": 1_785_381_600.0,
                }
            }
        )
    )
    monkeypatch.setattr(accounts, "RESETS_PATH", resets_path)
    monkeypatch.setattr(accounts, "SESSION_LIMITS_PATH", limits_path)
    monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
    monkeypatch.setattr(accounts, "_lock_depth", 0)

    accounts.mark_fable_limit(
        "work@example.com",
        "org-work",
        now_ts=1_785_381_600.0,
    )

    block = json.loads(limits_path.read_text())[
        "work@example.com|org-work|fable"
    ]
    assert block == {
        "detected_at": 1_785_381_600.0,
        "expires_at": 1_785_403_200.0,
    }
    rows = accounts.route_rows(
        {
            "accounts": {
                "work": {
                    "blob": _live_blob("work"),
                    "email": "work@example.com",
                    "org_uuid": "org-work",
                }
            }
        },
        None,
        1_785_381_700.0,
    )

    assert rows[0]["five_hour"] == 15.0
    assert rows[0]["seven_day"] == 50.0
    assert rows[0]["fable"] == 100.0


def test_cli_help_describes_hard_force_and_live_supervision(monkeypatch, capsys):
    monkeypatch.setattr(accounts, "retire_legacy_route_agent", lambda: None)

    with pytest.raises(SystemExit) as exit_info:
        accounts.main(["--help"])

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "force every supervised session onto <label>" in output
    assert "route supervised sessions to the freshest account" in output
    assert "5h/7d/fable headroom" in output
    assert "prefer <label> while quota is safe" not in output
    assert "route new sessions" not in output


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

    def test_pin_is_preferred_while_it_has_headroom(self):
        rows = [accounts_row("gmail", 5.0), accounts_row("work", 79.0)]
        assert accounts.pick_profile_route(rows, set(), "work") == "work"
        rows[1]["expired"] = True
        assert accounts.pick_profile_route(rows, set(), "work") == "gmail"

    def test_pin_falls_back_before_quota_is_exhausted(self):
        rows = [accounts_row("gmail", 5.0), accounts_row("work", 90.0)]
        assert accounts.pick_profile_route(rows, set(), "work") == "gmail"

    def test_fable_pin_falls_back_when_fable_window_is_exhausted(self):
        rows = [
            accounts_row("gmail", 20.0, seven_day=20.0, fable=20.0),
            accounts_row("work", 10.0, seven_day=10.0, fable=100.0),
        ]
        assert accounts.pick_profile_route(
            rows,
            set(),
            "work",
            require_fable=True,
        ) == "gmail"

    def test_fable_route_ignores_general_advisory_ceilings(self):
        rows = [
            accounts_row(
                "fable-ready",
                100.0,
                seven_day=100.0,
                fable=99.0,
            )
        ]

        assert accounts.pick_profile_route(rows, set(), None) is None
        assert accounts.pick_profile_route(
            rows,
            set(),
            None,
            require_fable=True,
        ) == "fable-ready"

    def test_forced_pin_ignores_quota_and_exclusions(self):
        rows = [
            accounts_row("safe", 5.0, seven_day=5.0),
            accounts_row("work", 100.0, seven_day=100.0, fable=99.0),
        ]

        assert accounts.pick_profile_route(
            rows,
            {"work"},
            "work",
            require_fable=True,
            force_pin=True,
        ) == "work"

    def test_forced_fable_pin_rejects_a_full_fable_window(self):
        rows = [
            accounts_row("work", 10.0, seven_day=10.0, fable=100.0),
        ]

        assert accounts.pick_profile_route(
            rows,
            set(),
            "work",
            require_fable=True,
            force_pin=True,
        ) is None

    def test_forced_pin_rejects_an_expired_account(self):
        rows = [
            accounts_row(
                "work",
                10.0,
                expired=True,
                seven_day=10.0,
                fable=10.0,
            ),
        ]

        assert accounts.pick_profile_route(
            rows,
            {"work"},
            "work",
            require_fable=True,
            force_pin=True,
        ) is None

    def test_automatic_route_skips_stale_usage(self):
        rows = [
            accounts_row("stale", 1.0, stale=True),
            accounts_row("fresh", 20.0),
        ]

        assert accounts.pick_profile_route(rows, set(), None) == "fresh"

    def test_forced_pin_ignores_stale_usage(self):
        rows = [
            accounts_row("fresh", 5.0),
            accounts_row("stale", 100.0, stale=True),
        ]

        assert accounts.pick_profile_route(
            rows,
            set(),
            "stale",
            force_pin=True,
        ) == "stale"


class TestSessionRouting:
    def test_parallel_launches_share_the_freshest_account(self):
        rows = [
            accounts_row("safe", 10.0, seven_day=10.0),
            accounts_row("work", 20.0, seven_day=20.0),
        ]

        ranked = accounts.rank_profile_rows(rows)

        assert [row["label"] for row in ranked] == ["safe", "work"]

    def test_equal_general_rank_breaks_on_label(self):
        rows = [
            accounts_row("zulu", 10.0, seven_day=20.0),
            accounts_row("alpha", 10.0, seven_day=20.0),
        ]

        ranked = accounts.rank_profile_rows(rows)

        assert [row["label"] for row in ranked] == ["alpha", "zulu"]

    def test_parallel_profile_selection_uses_one_current_account(
        self, tmp_path, monkeypatch
    ):
        blobs = {
            "accounts": {
                "first": {
                    "blob": _live_blob("first"),
                    "email": "first@x",
                    "org_uuid": "org-first",
                },
                "second": {
                    "blob": _live_blob("second"),
                    "email": "second@x",
                    "org_uuid": "org-second",
                },
            }
        }
        rows = [
            accounts_row("first", 10.0, seven_day=10.0),
            accounts_row("second", 20.0, seven_day=20.0),
        ]
        leases = []
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "_lock_depth", 0)
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        monkeypatch.setattr(
            accounts,
            "sync_profile_credentials",
            lambda value, persist: set(),
        )
        monkeypatch.setattr(
            accounts,
            "load_mode",
            lambda: {"mode": "auto", "label": None},
        )
        monkeypatch.setattr(accounts, "route_rows", lambda *_args: list(rows))
        monkeypatch.setattr(
            accounts,
            "load_session_leases",
            lambda *_args: list(leases),
        )
        monkeypatch.setattr(
            accounts,
            "save_session_leases",
            lambda value: leases.__setitem__(slice(None), value),
        )
        monkeypatch.setattr(accounts, "verify_entry_auth", lambda *_args: "ok")
        monkeypatch.setattr(
            accounts,
            "ensure_native_profile",
            lambda label, _entry: tmp_path / label,
        )
        monkeypatch.setattr(accounts, "excluded_labels", set)

        first = accounts.select_profile(lease_pid=101)
        second = accounts.select_profile(lease_pid=102)

        assert first["label"] == "first"
        assert second["label"] == "first"
        assert {lease["pid"] for lease in leases} == {101, 102}

    def test_fable_selection_prefers_lowest_fable_utilization(self):
        rows = [
            accounts_row("low-fable", 75.0, seven_day=10.0, fable=10.0),
            accounts_row("low-general", 20.0, seven_day=20.0, fable=20.0),
        ]

        ranked = accounts.rank_profile_rows(rows, require_fable=True)

        assert [row["label"] for row in ranked] == ["low-fable", "low-general"]

    def test_general_fallback_ignores_fable_mode_ranking(self, tmp_path, monkeypatch):
        blobs = {
            "accounts": {
                "fable": {
                    "blob": _live_blob("fable"),
                    "email": "fable@x",
                    "org_uuid": "org-fable",
                },
                "general": {
                    "blob": _live_blob("general"),
                    "email": "general@x",
                    "org_uuid": "org-general",
                },
            }
        }
        rows = [
            accounts_row("fable", 70.0, seven_day=60.0, fable=10.0),
            accounts_row("general", 10.0, seven_day=20.0, fable=90.0),
        ]
        monkeypatch.setattr(accounts, "locked", nullcontext)
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        monkeypatch.setattr(
            accounts,
            "sync_profile_credentials",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            accounts,
            "load_mode",
            lambda: {"mode": "fable", "label": None},
        )
        monkeypatch.setattr(accounts, "route_rows", lambda *_args: list(rows))
        monkeypatch.setattr(accounts, "load_session_leases", lambda: [])
        monkeypatch.setattr(accounts, "verify_entry_auth", lambda *_args: "ok")
        monkeypatch.setattr(
            accounts,
            "ensure_native_profile",
            lambda label, _entry: tmp_path / label,
        )
        monkeypatch.setattr(accounts, "excluded_labels", set)

        selected = accounts.select_profile(prefer_fable=False)

        assert selected["label"] == "general"

    def test_forced_label_can_reuse_a_stale_live_profile(
        self,
        tmp_path,
        monkeypatch,
    ):
        blobs = {
            "accounts": {
                "current": {
                    "blob": _live_blob("current"),
                    "email": "current@x",
                    "org_uuid": "org-current",
                }
            }
        }
        rows = [
            accounts_row(
                "current",
                20.0,
                seven_day=20.0,
                stale=True,
            )
        ]
        monkeypatch.setattr(accounts, "locked", nullcontext)
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        monkeypatch.setattr(
            accounts,
            "sync_profile_credentials",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            accounts,
            "load_mode",
            lambda: {"mode": "fable", "label": None},
        )
        monkeypatch.setattr(accounts, "route_rows", lambda *_args: list(rows))
        monkeypatch.setattr(accounts, "load_session_leases", lambda: [])
        monkeypatch.setattr(accounts, "verify_entry_auth", lambda *_args: "ok")
        monkeypatch.setattr(
            accounts,
            "ensure_native_profile",
            lambda label, _entry: tmp_path / label,
        )
        monkeypatch.setattr(accounts, "excluded_labels", set)

        selected = accounts.select_profile(
            require_fable=False,
            prefer_fable=False,
            force_label="current",
        )

        assert selected["label"] == "current"


class TestHandoffTarget:
    def _wire(self, monkeypatch, rows, *, mode="auto", label=None):
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {}})
        monkeypatch.setattr(accounts, "route_rows", lambda *_args: rows)
        monkeypatch.setattr(
            accounts,
            "load_mode",
            lambda: {"mode": mode, "label": label},
        )
        monkeypatch.setattr(accounts, "load_session_leases", lambda: [])
        monkeypatch.setattr(accounts, "excluded_labels", set)
        monkeypatch.delenv("ACCOUNTS_PIN", raising=False)

    def test_live_session_follows_a_new_safe_pin(self, monkeypatch):
        rows = [
            accounts_row("first", 10.0, seven_day=10.0),
            accounts_row("second", 20.0, seven_day=20.0),
        ]
        self._wire(monkeypatch, rows, mode="set", label="second")

        assert accounts.handoff_target(
            "first",
            require_fable=False,
        ) == "second"

    def test_stale_current_fable_hands_off_to_a_fresh_candidate(
        self,
        monkeypatch,
    ):
        rows = [
            accounts_row(
                "current",
                10.0,
                seven_day=10.0,
                fable=10.0,
                stale=True,
            ),
            accounts_row(
                "fresh",
                20.0,
                seven_day=20.0,
                fable=20.0,
            ),
        ]
        self._wire(monkeypatch, rows)

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) == "fresh"

    def test_set_mode_moves_to_exhausted_target(self, monkeypatch):
        rows = [
            accounts_row("first", 100.0, seven_day=100.0),
            accounts_row("second", 20.0, seven_day=20.0),
        ]
        self._wire(monkeypatch, rows, mode="set", label="first")

        assert accounts.handoff_target(
            "second",
            require_fable=False,
        ) == "first"

    def test_auto_mode_converges_live_sessions_on_the_freshest_account(
        self, monkeypatch
    ):
        rows = [
            accounts_row("first", 10.0, seven_day=10.0),
            accounts_row("second", 20.0, seven_day=20.0),
        ]
        self._wire(monkeypatch, rows)

        assert accounts.handoff_target(
            "second",
            require_fable=False,
        ) == "first"

    def test_fable_session_stays_until_the_fable_window_is_full(self, monkeypatch):
        rows = [
            accounts_row("current", 10.0, seven_day=10.0, fable=99.0),
            accounts_row("other", 10.0, seven_day=10.0, fable=1.0),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) is None

    def test_fable_session_switches_when_the_fable_window_is_full(self, monkeypatch):
        rows = [
            accounts_row("current", 10.0, seven_day=10.0, fable=100.0),
            accounts_row("other", 10.0, seven_day=10.0, fable=82.0),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) == "other"

    # Reversal of "ignores general window ceilings": a rate-wall 429 costs the
    # turn plus an hours-long lockout, so near-wall fable sessions depart too.
    @pytest.mark.parametrize(
        ("five_hour", "seven_day"),
        [(100.0, 20.0), (20.0, 100.0)],
    )
    def test_fable_session_departs_a_rate_wall(
        self,
        five_hour,
        seven_day,
        monkeypatch,
    ):
        rows = [
            accounts_row(
                "current",
                five_hour,
                seven_day=seven_day,
                fable=99.0,
            ),
            accounts_row("other", 10.0, seven_day=20.0, fable=1.0),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) == "other"

    def test_fable_session_ignores_advisory_usage_below_the_threshold(
        self,
        monkeypatch,
    ):
        rows = [
            accounts_row(
                "current",
                accounts.DEPART_PCT - 0.1,
                seven_day=10.0,
                fable=99.0,
            ),
            accounts_row("other", 10.0, seven_day=20.0, fable=1.0),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) is None

    def test_fable_departure_leaves_the_lowest_fable_account(self, monkeypatch):
        # The incident shape: fable mode parks sessions on the lowest-fable
        # account, so the walled row ranks first and must not pick itself.
        rows = [
            accounts_row("current", 95.0, seven_day=10.0, fable=1.0),
            accounts_row("other", 10.0, seven_day=20.0, fable=50.0),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) == "other"

    def test_fable_departure_fires_exactly_at_the_threshold(self, monkeypatch):
        rows = [
            accounts_row(
                "current",
                accounts.DEPART_PCT,
                seven_day=10.0,
                fable=99.0,
            ),
            accounts_row("other", 10.0, seven_day=20.0, fable=1.0),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) == "other"

    def test_fable_departure_stays_without_enough_gain(self, monkeypatch):
        rows = [
            accounts_row("current", 95.0, seven_day=10.0, fable=99.0),
            accounts_row(
                "other",
                95.0 - accounts.HANDOFF_MARGIN_PCT + 1.0,
                seven_day=20.0,
                fable=1.0,
            ),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) is None

    def test_fable_departure_accepts_exactly_the_margin(self, monkeypatch):
        rows = [
            accounts_row("current", 95.0, seven_day=10.0, fable=99.0),
            accounts_row(
                "other",
                95.0 - accounts.HANDOFF_MARGIN_PCT,
                seven_day=20.0,
                fable=1.0,
            ),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) == "other"

    def test_fable_departure_never_lands_on_a_walled_peer(self, monkeypatch):
        rows = [
            accounts_row("current", 95.0, seven_day=10.0, fable=99.0),
            accounts_row("walled", 100.0, seven_day=10.0, fable=1.0),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=True,
        ) is None

    def test_general_fallback_uses_general_headroom_in_fable_mode(
        self,
        monkeypatch,
    ):
        rows = [
            accounts_row(
                "current",
                79.0,
                seven_day=79.0,
                fable=95.0,
            ),
            accounts_row(
                "other",
                10.0,
                seven_day=10.0,
                fable=100.0,
            ),
        ]
        self._wire(monkeypatch, rows, mode="fable")

        assert accounts.handoff_target(
            "current",
            require_fable=False,
        ) == "other"

    def test_margin_rejects_a_handoff_that_buys_no_runway(self, monkeypatch):
        rows = [
            accounts_row("current", 72.1, seven_day=10.0),
            accounts_row("barely", 72.0, seven_day=10.0),
        ]
        self._wire(monkeypatch, rows)

        assert accounts.handoff_target(
            "current",
            require_fable=False,
            margin_pct=accounts.HANDOFF_MARGIN_PCT,
        ) is None

    def test_margin_allows_a_handoff_that_buys_real_runway(self, monkeypatch):
        rows = [
            accounts_row("current", 72.0, seven_day=10.0),
            accounts_row("fresh", 5.0, seven_day=5.0),
        ]
        self._wire(monkeypatch, rows)

        assert accounts.handoff_target(
            "current",
            require_fable=False,
            margin_pct=accounts.HANDOFF_MARGIN_PCT,
        ) == "fresh"

    def test_margin_is_off_for_callers_that_do_not_ask(self, monkeypatch):
        rows = [
            accounts_row("current", 72.1, seven_day=10.0),
            accounts_row("barely", 72.0, seven_day=10.0),
        ]
        self._wire(monkeypatch, rows)

        assert accounts.handoff_target(
            "current",
            require_fable=False,
        ) == "barely"


class TestProfileNearWall:
    def _wire(self, monkeypatch, rows):
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {}})
        monkeypatch.setattr(accounts, "route_rows", lambda *_args: rows)

    def test_fires_at_the_departure_threshold(self, monkeypatch):
        self._wire(
            monkeypatch,
            [accounts_row("current", accounts.DEPART_PCT, seven_day=10.0)],
        )

        assert accounts.profile_near_wall("current") is True

    def test_quiet_below_the_threshold(self, monkeypatch):
        self._wire(
            monkeypatch,
            [accounts_row("current", accounts.DEPART_PCT - 0.1, seven_day=10.0)],
        )

        assert accounts.profile_near_wall("current") is False

    def test_the_weekly_axis_can_trigger_it(self, monkeypatch):
        self._wire(
            monkeypatch,
            [accounts_row("current", 5.0, seven_day=accounts.DEPART_PCT)],
        )

        assert accounts.profile_near_wall("current") is True

    def test_a_stale_row_never_triggers_it(self, monkeypatch):
        self._wire(
            monkeypatch,
            [accounts_row("current", 99.0, seven_day=99.0, stale=True)],
        )

        assert accounts.profile_near_wall("current") is False

    def test_holds_where_the_account_is_merely_unfit_to_receive(self, monkeypatch):
        # RATE_CAP_PCT means "don't send new sessions here", not "abandon ship".
        # Departure is the stricter condition, so the band between them stays put.
        self._wire(monkeypatch, [accounts_row("current", 85.0, seven_day=10.0)])

        assert accounts.profile_general_exhausted("current") is True
        assert accounts.profile_near_wall("current") is False


class TestConfirmStaleCandidate:
    """One request resolves the stale row that left selection with no candidate."""

    def _wire(self, monkeypatch, tmp_path, rows, *, usage=USAGE_OK):
        monkeypatch.setattr(
            accounts, "CONFIRM_POLL_LOCK_PATH", tmp_path / "confirm-poll.lock"
        )
        monkeypatch.setattr(
            accounts,
            "load_blobs",
            lambda: {
                "accounts": {
                    row["label"]: {
                        "blob": "blob",
                        "email": f"{row['label']}@x",
                        "org_uuid": f"org-{row['label']}",
                    }
                    for row in rows
                }
            },
        )
        monkeypatch.setattr(accounts, "route_rows", lambda *_args: rows)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: "blob")
        monkeypatch.setattr(accounts, "blob_access_expiry", lambda _blob: None)
        monkeypatch.setattr(accounts, "blob_access_token", lambda _blob: "token")
        merged = {}
        monkeypatch.setattr(accounts, "merge_reset_rows", merged.update)
        calls = []
        monkeypatch.setattr(
            accounts,
            "fetch_usage",
            lambda _token, timeout=None: (calls.append(timeout), usage)[1],
        )
        return calls, merged

    def test_confirms_the_stale_row_and_writes_the_board(self, monkeypatch, tmp_path):
        calls, merged = self._wire(
            monkeypatch, tmp_path, [accounts_row("idle", 99.0, stale=True)]
        )

        assert accounts.confirm_stale_candidate(excludes=set(), require_fable=False)
        assert len(calls) == 1
        assert merged

    def test_spends_no_request_when_nothing_is_stale(self, monkeypatch, tmp_path):
        calls, _ = self._wire(
            monkeypatch, tmp_path, [accounts_row("fresh", 10.0)]
        )

        assert not accounts.confirm_stale_candidate(excludes=set(), require_fable=False)
        assert calls == []

    def test_skips_a_stale_row_the_caller_excluded(self, monkeypatch, tmp_path):
        calls, _ = self._wire(
            monkeypatch, tmp_path, [accounts_row("idle", 99.0, stale=True)]
        )

        assert not accounts.confirm_stale_candidate(
            excludes={"idle"}, require_fable=False
        )
        assert calls == []

    def test_cooldown_blocks_a_second_confirm(self, monkeypatch, tmp_path):
        calls, _ = self._wire(
            monkeypatch, tmp_path, [accounts_row("idle", 99.0, stale=True)]
        )

        assert accounts.confirm_stale_candidate(excludes=set(), require_fable=False)
        assert not accounts.confirm_stale_candidate(excludes=set(), require_fable=False)
        assert len(calls) == 1

    def test_another_holder_of_the_lock_is_not_waited_on(self, monkeypatch, tmp_path):
        calls, _ = self._wire(
            monkeypatch, tmp_path, [accounts_row("idle", 99.0, stale=True)]
        )
        lock = tmp_path / "confirm-poll.lock"
        lock.touch()
        with open(lock, "a+") as held:
            accounts.fcntl.flock(held, accounts.fcntl.LOCK_EX | accounts.fcntl.LOCK_NB)

            assert not accounts.confirm_stale_candidate(
                excludes=set(), require_fable=False
            )
            assert calls == []

    def test_a_failed_request_changes_nothing(self, monkeypatch, tmp_path):
        _, merged = self._wire(
            monkeypatch,
            tmp_path,
            [accounts_row("idle", 99.0, stale=True)],
            usage=None,
        )

        assert not accounts.confirm_stale_candidate(excludes=set(), require_fable=False)
        assert merged == {}

    def test_the_request_carries_a_short_timeout(self, monkeypatch, tmp_path):
        calls, _ = self._wire(
            monkeypatch, tmp_path, [accounts_row("idle", 99.0, stale=True)]
        )

        accounts.confirm_stale_candidate(excludes=set(), require_fable=False)

        assert calls == [accounts.CONFIRM_POLL_TIMEOUT_S]

    def test_refreshes_an_expired_candidate_before_polling(self, monkeypatch, tmp_path):
        calls, _ = self._wire(
            monkeypatch, tmp_path, [accounts_row("idle", 10.0, stale=True)]
        )
        live = {"blob": "expired"}
        refreshed = []

        def refresh(labels=None):
            refreshed.append(labels)
            live["blob"] = "fresh"
            return 1

        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: live["blob"])
        monkeypatch.setattr(
            accounts,
            "blob_access_expiry",
            lambda blob: 1 if blob == "expired" else None,
        )
        monkeypatch.setattr(
            accounts,
            "blob_access_token",
            lambda blob: "fresh-token" if blob == "fresh" else None,
        )
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda token: (
                {
                    "account": {"email": "idle@x"},
                    "organization": {"uuid": "org-idle"},
                }
                if token == "fresh-token"
                else None
            ),
        )
        monkeypatch.setattr(
            accounts,
            "refresh_dormant_profiles",
            refresh,
        )

        assert accounts.confirm_stale_candidate(excludes=set(), require_fable=False)
        assert refreshed == [{"idle"}]
        assert len(calls) == 1

    def test_stops_if_the_candidate_disappears_during_refresh(
        self,
        monkeypatch,
        tmp_path,
    ):
        calls, merged = self._wire(
            monkeypatch, tmp_path, [accounts_row("idle", 10.0, stale=True)]
        )
        blob_sets = iter(
            [
                {
                    "accounts": {
                        "idle": {
                            "blob": "expired",
                            "email": "idle@x",
                            "org_uuid": "org-idle",
                        }
                    }
                },
                {"accounts": {}},
            ]
        )
        live_blobs = iter(["expired", "fresh"])
        monkeypatch.setattr(accounts, "load_blobs", lambda: next(blob_sets))
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: next(live_blobs))
        monkeypatch.setattr(
            accounts,
            "blob_access_expiry",
            lambda blob: 1 if blob == "expired" else None,
        )
        monkeypatch.setattr(accounts, "refresh_dormant_profiles", lambda _labels: 1)

        assert not accounts.confirm_stale_candidate(excludes=set(), require_fable=False)
        assert calls == []
        assert merged == {}

    def test_stops_if_the_refreshed_candidate_changed_identity(
        self,
        monkeypatch,
        tmp_path,
    ):
        calls, merged = self._wire(
            monkeypatch, tmp_path, [accounts_row("idle", 10.0, stale=True)]
        )
        live = {"blob": "expired"}

        def refresh(_labels):
            live["blob"] = "fresh"
            return 1

        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: live["blob"])
        monkeypatch.setattr(
            accounts,
            "blob_access_expiry",
            lambda blob: 1 if blob == "expired" else None,
        )
        monkeypatch.setattr(accounts, "blob_access_token", lambda _blob: "wrong-token")
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda _token: {
                "account": {"email": "other@x"},
                "organization": {"uuid": "org-other"},
            },
        )
        monkeypatch.setattr(accounts, "refresh_dormant_profiles", refresh)

        assert not accounts.confirm_stale_candidate(excludes=set(), require_fable=False)
        assert calls == []
        assert merged == {}


class TestSelectProfileRetriesAfterConfirm:
    def _wire(self, monkeypatch, results):
        seen = iter(results)
        monkeypatch.setattr(
            accounts, "_select_profile_once", lambda **_kwargs: next(seen)
        )
        confirms = []
        monkeypatch.setattr(
            accounts,
            "confirm_stale_candidate",
            lambda **kwargs: (confirms.append(kwargs), True)[1],
        )
        monkeypatch.setattr(accounts, "excluded_labels", set)
        return confirms

    def test_first_pass_success_never_touches_the_network(self, monkeypatch):
        confirms = self._wire(monkeypatch, [{"label": "fresh"}])

        assert accounts.select_profile()["label"] == "fresh"
        assert confirms == []

    def test_retries_once_after_a_confirm(self, monkeypatch):
        confirms = self._wire(monkeypatch, [None, {"label": "idle"}])

        assert accounts.select_profile()["label"] == "idle"
        assert len(confirms) == 1

    def test_a_forced_label_is_authoritative_and_skips_the_confirm(self, monkeypatch):
        confirms = self._wire(monkeypatch, [None])

        assert accounts.select_profile(force_label="pinned") is None
        assert confirms == []

    def test_avoided_labels_are_not_confirmed(self, monkeypatch):
        confirms = self._wire(monkeypatch, [None, {"label": "other"}])

        accounts.select_profile(avoid_labels={"spent"})

        assert confirms[0]["excludes"] == {"spent"}


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

    def test_route_rows_sorts_by_binding_window(self, monkeypatch):
        # A is freshest on 5h but nearly weekly-walled — its real runway is 6%.
        # Best-first must mean the binding window (worst axis), not the 5h axis.
        monkeypatch.setattr(accounts, "load_resets", lambda: {
            "a@x|o1": {"five_hour_pct": 9.0, "seven_day_pct": 94.0},
            "b@x|o2": {"five_hour_pct": 13.0, "seven_day_pct": 20.0},
        })
        blobs = {"accounts": {
            "A": {"blob": _live_blob("a"), "email": "a@x", "org_uuid": "o1"},
            "B": {"blob": _live_blob("b"), "email": "b@x", "org_uuid": "o2"},
        }}
        rows = accounts.route_rows(blobs, None, 2_000_000_000.0)
        assert [r["label"] for r in rows] == ["B", "A"]

    def test_route_rows_unknown_axis_sorts_last(self, monkeypatch):
        # C's weekly was never polled → _rate_eligible can't pick it; the board
        # must not show it above fully-known rows ("sorted best-first").
        monkeypatch.setattr(accounts, "load_resets", lambda: {
            "a@x|o1": {"five_hour_pct": 80.0, "seven_day_pct": 80.0},
            "c@x|o3": {"seven_day_pct": 50.0},
        })
        blobs = {"accounts": {
            "A": {"blob": _live_blob("a"), "email": "a@x", "org_uuid": "o1"},
            "C": {"blob": _live_blob("c"), "email": "c@x", "org_uuid": "o3"},
        }}
        rows = accounts.route_rows(blobs, None, 2_000_000_000.0)
        assert [r["label"] for r in rows] == ["A", "C"]

    def test_route_rows_equal_binding_breaks_on_5h(self, monkeypatch):
        monkeypatch.setattr(accounts, "load_resets", lambda: {
            "a@x|o1": {"five_hour_pct": 50.0, "seven_day_pct": 50.0},
            "b@x|o2": {"five_hour_pct": 30.0, "seven_day_pct": 50.0},
        })
        blobs = {"accounts": {
            "A": {"blob": _live_blob("a"), "email": "a@x", "org_uuid": "o1"},
            "B": {"blob": _live_blob("b"), "email": "b@x", "org_uuid": "o2"},
        }}
        rows = accounts.route_rows(blobs, None, 2_000_000_000.0)
        assert [r["label"] for r in rows] == ["B", "A"]


def accounts_row(
    label,
    five_hour,
    expired=False,
    active=False,
    fable=0.0,
    seven_day=0.0,
    stale=False,
):
    return {"label": label, "email": f"{label}@x", "five_hour": five_hour,
            "seven_day": seven_day, "fable": fable, "expired": expired,
            "active": active, "stale": stale}


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
        assert row["five_hour_pct"] is None
        assert row["seven_day_pct"] is None

    def test_access_expiry_ms_to_s(self):
        blob = json.dumps({"claudeAiOauth": {"expiresAt": 1784337371728}})
        assert accounts.blob_access_expiry(blob) == 1784337371

    def test_access_expiry_missing(self):
        assert accounts.blob_access_expiry(json.dumps({"claudeAiOauth": {}})) is None


class TestRefreshDormantProfiles:
    @staticmethod
    def _expired_access_blob(token):
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": "refresh",
                    "expiresAt": 1_000_000,
                    "refreshTokenExpiresAt": 3_000_000_000_000,
                }
            }
        )

    def test_refreshes_expired_dormant_profile_before_polling(self, monkeypatch):
        old = self._expired_access_blob("old")
        new = _live_blob("new")
        blobs = {
            "accounts": {
                "gmail": {"blob": old, "email": "g@x", "org_uuid": "o1"}
            }
        }
        writes = []
        monkeypatch.setattr(accounts.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(accounts, "load_session_leases", list)
        monkeypatch.setattr(accounts, "excluded_labels", set)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: old)
        monkeypatch.setattr(accounts, "kc_read", lambda *_args: None)
        monkeypatch.setattr(accounts, "refresh_blob_access", lambda blob: new)
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda _token: {
                "account": {"email": "g@x"},
                "organization": {"uuid": "o1"},
            },
        )
        monkeypatch.setattr(
            accounts,
            "write_profile_credentials",
            lambda label, blob: writes.append((label, blob)),
        )
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        monkeypatch.setattr(accounts, "save_blobs", lambda value: None)

        assert accounts.refresh_dormant_profiles() == 1
        assert blobs["accounts"]["gmail"]["blob"] == new
        assert writes == [("gmail", new)]

    def test_rejects_a_refreshed_profile_with_the_wrong_identity(self, monkeypatch):
        old = self._expired_access_blob("old")
        new = _live_blob("new")
        blobs = {
            "accounts": {
                "gmail": {"blob": old, "email": "g@x", "org_uuid": "o1"}
            }
        }
        monkeypatch.setattr(accounts.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(accounts, "load_session_leases", list)
        monkeypatch.setattr(accounts, "excluded_labels", set)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: old)
        monkeypatch.setattr(accounts, "kc_read", lambda *_args: None)
        monkeypatch.setattr(accounts, "refresh_blob_access", lambda _blob: new)
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda _token: {
                "account": {"email": "other@x"},
                "organization": {"uuid": "org-other"},
            },
        )
        monkeypatch.setattr(
            accounts,
            "write_profile_credentials",
            lambda *_args: pytest.fail("persisted a mismatched credential"),
        )
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        saved = []
        monkeypatch.setattr(accounts, "save_blobs", saved.append)

        assert accounts.refresh_dormant_profiles() == 0
        assert blobs["accounts"]["gmail"]["blob"] == old
        assert saved == []

    def test_refreshes_dormant_keychain_profiles_inside_native_claude(
        self,
        monkeypatch,
    ):
        old = self._expired_access_blob("old")
        blobs = {
            "accounts": {
                "work": {"blob": old, "email": "w@x", "org_uuid": "o1"}
            }
        }
        native_refreshes = []
        monkeypatch.setattr(accounts.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(accounts, "load_session_leases", list)
        monkeypatch.setattr(accounts, "excluded_labels", set)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: old)
        monkeypatch.setattr(accounts, "kc_read", lambda *_args: old)
        monkeypatch.setattr(
            accounts,
            "write_profile_credentials",
            lambda *_args: pytest.fail("rewrote a keychain-owned profile"),
        )
        monkeypatch.setattr(
            accounts,
            "refresh_blob_access",
            lambda _blob: pytest.fail("rotated a keychain-owned credential"),
        )
        monkeypatch.setattr(
            accounts,
            "refresh_keychain_profiles",
            lambda labels, _now: native_refreshes.append(labels) or len(labels),
        )
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)

        assert accounts.refresh_dormant_profiles() == 1
        assert native_refreshes == [["work"]]

    def test_native_binary_skips_the_router_wrapper(
        self,
        tmp_path,
        monkeypatch,
    ):
        local_bin = tmp_path / ".local" / "bin"
        versions = tmp_path / ".local" / "share" / "claude" / "versions"
        local_bin.mkdir(parents=True)
        versions.mkdir(parents=True)
        wrapper = local_bin / "claude"
        wrapper.write_text("#!/bin/sh\nexec claude-router \"$@\"\n")
        wrapper.chmod(0o755)
        older = versions / "2.1.246"
        newer = versions / "2.1.247"
        for binary in (older, newer):
            binary.write_text("native")
            binary.chmod(0o755)
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))
        monkeypatch.setattr(accounts, "HOME", tmp_path)
        monkeypatch.delenv("CLAUDE_REAL_BIN", raising=False)

        assert accounts.native_claude_binary() == str(newer)

    def test_native_refresh_uses_and_reaps_a_zero_worker_transient_daemon(
        self,
        tmp_path,
        monkeypatch,
    ):
        old = self._expired_access_blob("old")
        new = _live_blob("new")
        live = {"blob": old}
        launches = []
        json_paths = []
        process_events = []

        class Process:
            pid = 123

            def poll(self):
                process_events.append("poll")
                return None

            def terminate(self):
                process_events.append("terminate")

            def wait(self, timeout=None):
                process_events.append(("wait", timeout))
                return 0

            def kill(self):
                pytest.fail("killed Claude during native credential refresh")

        def launch(command, **kwargs):
            json_path = Path(command[command.index("--json-path") + 1])
            assert not json_path.exists()
            json_paths.append(json_path)
            launches.append((command, kwargs))
            live["blob"] = new
            return Process()

        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-leak")
        monkeypatch.setattr(
            accounts,
            "NATIVE_REFRESH_LOCK_PATH",
            tmp_path / "native-refresh.lock",
        )
        monkeypatch.setattr(
            accounts,
            "native_claude_binary",
            lambda: "/native/claude",
        )
        monkeypatch.setattr(
            accounts,
            "native_profile_refresh_supported",
            lambda _binary: True,
        )
        monkeypatch.setattr(
            accounts,
            "native_profile_path",
            lambda label: tmp_path / "profiles" / label,
        )
        monkeypatch.setattr(
            accounts,
            "NATIVE_REFRESH_LOCK_PATH",
            tmp_path / "native-refresh.lock",
        )
        monkeypatch.setattr(
            accounts,
            "profile_live_blob",
            lambda _label: live["blob"],
        )
        monkeypatch.setattr(accounts.subprocess, "Popen", launch)

        assert accounts.refresh_keychain_profiles(["work"]) == 1
        assert launches[0][0][:4] == [
            "/native/claude",
            "daemon",
            "run",
            "--origin",
        ]
        assert launches[0][1]["env"]["CLAUDE_CONFIG_DIR"] == str(
            tmp_path / "profiles" / "work"
        )
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in launches[0][1]["env"]
        assert launches[0][1]["stdin"] is subprocess.DEVNULL
        assert launches[0][1]["stdout"] is subprocess.DEVNULL
        assert launches[0][1]["stderr"] is subprocess.DEVNULL
        assert launches[0][1]["start_new_session"] is True
        assert len(launches[0][1]["pass_fds"]) == 1
        assert json_paths[0].parent.exists()
        assert process_events == ["poll", "terminate", ("wait", 1.0)]

    def test_native_refresh_lock_remains_held_by_the_daemon(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(
            accounts,
            "NATIVE_REFRESH_LOCK_PATH",
            tmp_path / "native-refresh.lock",
        )

        with accounts.try_native_refresh_lock() as lock:
            assert lock is not None
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(0.2)",
                ],
                pass_fds=(lock.fileno(),),
            )

        with accounts.try_native_refresh_lock() as overlap:
            assert overlap is None

        child.wait(timeout=2)
        with accounts.try_native_refresh_lock() as available:
            assert available is not None

    def test_dormant_refresh_reloads_the_store_before_saving(
        self,
        monkeypatch,
    ):
        old = self._expired_access_blob("old")
        current = {
            "accounts": {
                "work": {"blob": old, "email": "w@x", "org_uuid": "o1"},
                "added": {
                    "blob": _live_blob("added"),
                    "email": "a@x",
                    "org_uuid": "o2",
                },
            }
        }
        saved = []
        monkeypatch.setattr(accounts.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(accounts, "load_blobs", lambda: current)
        monkeypatch.setattr(accounts, "load_session_leases", list)
        monkeypatch.setattr(accounts, "excluded_labels", set)
        monkeypatch.setattr(
            accounts,
            "profile_live_blob",
            lambda label: old if label == "work" else _live_blob("added"),
        )
        monkeypatch.setattr(accounts, "kc_read", lambda *_args: None)
        monkeypatch.setattr(
            accounts,
            "refresh_blob_access",
            lambda _blob: _live_blob("new"),
        )
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda _token: {
                "account": {"email": "w@x"},
                "organization": {"uuid": "o1"},
            },
        )
        monkeypatch.setattr(accounts, "write_profile_credentials", lambda *_args: None)
        monkeypatch.setattr(
            accounts,
            "save_blobs",
            lambda blobs: saved.append(json.loads(json.dumps(blobs))),
        )

        assert accounts.refresh_dormant_profiles() == 1
        assert set(saved[0]["accounts"]) == {"work", "added"}

    @pytest.mark.parametrize(
        ("version", "help_text", "expected"),
        [
            (
                "2.1.219 (Claude Code)",
                "run [json-path] --json-path <p> --log-file <p>",
                False,
            ),
            ("2.1.220 (Claude Code)", "run [json-path] --json-path <p>", False),
            (
                "2.1.220 (Claude Code)",
                "run [json-path] --json-path <p> --log-file <p>",
                True,
            ),
        ],
    )
    def test_native_refresh_gate_checks_version_and_daemon_contract(
        self,
        version,
        help_text,
        expected,
        monkeypatch,
    ):
        responses = [
            types.SimpleNamespace(returncode=0, stdout=version, stderr=""),
            types.SimpleNamespace(returncode=0, stdout=help_text, stderr=""),
        ]
        monkeypatch.setattr(
            accounts.subprocess,
            "run",
            lambda *_args, **_kwargs: responses.pop(0),
        )

        assert accounts.native_profile_refresh_supported("/native/claude") is expected

    def test_native_refresh_is_disabled_when_the_daemon_contract_is_unknown(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            accounts,
            "native_claude_binary",
            lambda: "/native/claude",
        )
        monkeypatch.setattr(
            accounts,
            "native_profile_refresh_supported",
            lambda _binary: False,
        )
        monkeypatch.setattr(
            accounts.subprocess,
            "Popen",
            lambda *_args, **_kwargs: pytest.fail("launched an unsupported daemon"),
        )

        assert accounts.refresh_keychain_profiles(["work"]) == 0

    def test_manual_refresh_does_not_rotate_keychain_credentials(
        self,
        monkeypatch,
        capsys,
    ):
        old = self._expired_access_blob("old")
        blobs = {
            "accounts": {
                "work": {"blob": old, "email": "w@x", "org_uuid": "o1"}
            }
        }
        monkeypatch.setattr(accounts, "locked", lambda blocking=True: nullcontext())
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        monkeypatch.setattr(
            accounts,
            "sync_profile_credentials",
            lambda value, persist: set(),
        )
        monkeypatch.setattr(accounts, "kc_read", lambda *_args: old)
        monkeypatch.setattr(
            accounts,
            "refresh_blob_access",
            lambda _blob: pytest.fail("rotated a keychain-owned credential"),
        )

        accounts.cmd_refresh(types.SimpleNamespace(label="work"))

        assert "refreshes inside Claude" in capsys.readouterr().out

    def test_skips_active_and_excluded_profiles(self, monkeypatch):
        old = self._expired_access_blob("old")
        blobs = {
            "accounts": {
                "active": {"blob": old, "email": "a@x", "org_uuid": "o1"},
                "excluded": {"blob": old, "email": "e@x", "org_uuid": "o2"},
            }
        }
        monkeypatch.setattr(accounts.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(
            accounts,
            "load_session_leases",
            lambda: [{"label": "active"}],
        )
        monkeypatch.setattr(accounts, "excluded_labels", lambda: {"excluded"})
        monkeypatch.setattr(
            accounts,
            "refresh_blob_access",
            lambda _blob: pytest.fail("refreshed an unavailable profile"),
        )
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)

        assert accounts.refresh_dormant_profiles() == 0

    def test_poll_refreshes_dormant_profiles_first(self, monkeypatch, capsys):
        before = {"accounts": {"before": {}}}
        after = {"accounts": {"after": {}}}
        snapshots = iter([before, after])
        calls = []
        lock_depth = 0

        class TrackingLock:
            def __enter__(self):
                nonlocal lock_depth
                lock_depth += 1

            def __exit__(self, *_args):
                nonlocal lock_depth
                lock_depth -= 1

        monkeypatch.setattr(
            accounts,
            "locked",
            lambda blocking=True: TrackingLock(),
        )
        monkeypatch.setattr(accounts, "watch_lock", nullcontext)
        monkeypatch.setattr(accounts, "load_blobs", lambda: next(snapshots))
        monkeypatch.setattr(
            accounts,
            "sync_profile_credentials",
            lambda value, persist: set(),
        )
        monkeypatch.setattr(
            accounts,
            "refresh_dormant_profiles",
            lambda: (
                pytest.fail("poll held the accounts lock during native refresh")
                if lock_depth
                else calls.append(("refresh",)) or 2
            ),
        )
        monkeypatch.setattr(
            accounts,
            "poll_blobs_usage",
            lambda value: calls.append(("poll", value)) or 4,
        )
        monkeypatch.setattr(
            accounts,
            "write_statusline_snapshot",
            lambda value, error: calls.append(("snapshot", value, error)),
        )
        monkeypatch.setattr(accounts, "shared_snapshot_enabled", lambda: True)

        accounts.cmd_poll(types.SimpleNamespace())

        assert calls == [("refresh",), ("poll", after), ("snapshot", after, None)]
        assert "refreshed usage for 4 account(s)" in capsys.readouterr().out


class TestPollBlobsUsage:
    def test_polls_from_a_fresher_profile_keychain_blob(self, monkeypatch):
        stored = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "stored",
                    "refreshToken": "refresh",
                    "expiresAt": 1_000,
                    "refreshTokenExpiresAt": 3_000_000_000_000,
                }
            }
        )
        live = _live_blob("live")
        blobs = {
            "accounts": {
                "work": {
                    "blob": stored,
                    "email": "work@example.com",
                    "org_uuid": "org-work",
                }
            }
        }
        merged = []
        monkeypatch.setattr(accounts.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: live)
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda _token: {
                "account": {"email": "work@example.com"},
                "organization": {"uuid": "org-work"},
            },
        )
        monkeypatch.setattr(
            accounts,
            "fetch_usage",
            lambda token: {"five_hour": {"utilization": 12}}
            if token == "live"
            else pytest.fail("polled the stale stored token"),
        )
        monkeypatch.setattr(
            accounts,
            "usage_to_reset_row",
            lambda *_args: {"five_hour_pct": 12},
        )
        monkeypatch.setattr(accounts, "merge_reset_rows", merged.append)

        assert accounts.poll_blobs_usage(blobs) == 1
        assert merged == [
            {
                "work@example.com|org-work": {
                    "five_hour_pct": 12,
                }
            }
        ]

    def test_skips_a_live_profile_with_the_wrong_identity(self, monkeypatch):
        stored = _live_blob("stored")
        live = _live_blob("wrong")
        blobs = {
            "accounts": {
                "work": {
                    "blob": stored,
                    "email": "work@example.com",
                    "org_uuid": "org-work",
                }
            }
        }
        monkeypatch.setattr(accounts.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: live)
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda _token: {
                "account": {"email": "other@example.com"},
                "organization": {"uuid": "org-other"},
            },
        )
        monkeypatch.setattr(
            accounts,
            "fetch_usage",
            lambda _token: pytest.fail("polled a mismatched credential"),
        )
        merged = {}
        monkeypatch.setattr(accounts, "merge_reset_rows", merged.update)

        assert accounts.poll_blobs_usage(blobs) == 0
        assert merged == {}

    def test_one_dead_account_does_not_fail_the_poll(self, monkeypatch, capsys):
        blobs = {
            "accounts": {
                "work": {"blob": _live_blob("work"), "email": "work@example.com", "org_uuid": "org-work"},
                "dead": {"blob": _live_blob("dead"), "email": "dead@example.com", "org_uuid": "org-dead"},
            }
        }
        monkeypatch.setattr(accounts.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: None)
        monkeypatch.setattr(
            accounts,
            "fetch_usage",
            lambda token: {"five_hour": {"utilization": 12}} if token == "work" else None,
        )
        monkeypatch.setattr(accounts, "usage_to_reset_row", lambda *_args: {"five_hour_pct": 12})
        merged = {}
        monkeypatch.setattr(accounts, "merge_reset_rows", merged.update)

        assert accounts.poll_blobs_usage(blobs) == 1

        assert merged == {"work@example.com|org-work": {"five_hour_pct": 12}}
        assert "usage poll failed for 1 account(s)" in capsys.readouterr().err

    def test_every_account_failing_raises(self, monkeypatch):
        blobs = {
            "accounts": {
                "dead": {"blob": _live_blob("dead"), "email": "dead@example.com", "org_uuid": "org-dead"},
            }
        }
        monkeypatch.setattr(accounts.time, "time", lambda: 2_000.0)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _label: None)
        monkeypatch.setattr(accounts, "fetch_usage", lambda _token: None)
        monkeypatch.setattr(accounts, "merge_reset_rows", lambda _rows: None)

        with pytest.raises(accounts.AccountsError, match="usage poll failed for 1 account"):
            accounts.poll_blobs_usage(blobs)


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

    def test_refuses_a_server_rejected_login(self, tmp_path, monkeypatch, capsys):
        self._paths(tmp_path, monkeypatch)
        rejected = {"blob": _live_blob("a"), "auth_dead_at": 1}
        monkeypatch.setattr(
            accounts,
            "load_blobs",
            lambda: {"accounts": {"B": rejected}},
        )
        monkeypatch.setattr(
            accounts,
            "sync_profile_credentials",
            lambda *_args, **_kwargs: set(),
        )

        with pytest.raises(SystemExit):
            accounts.cmd_set(types.SimpleNamespace(label="B"))

        assert not accounts.MODE_PATH.exists()
        assert "login is unusable" in capsys.readouterr().err

    def test_pins_native_profile_without_touching_global_credentials(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        live = _live_blob("a")
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {"B": {"blob": live}}})
        accounts.cmd_set(types.SimpleNamespace(label="B"))
        assert json.loads(accounts.MODE_PATH.read_text()) == {
            "version": 2,
            "mode": "set",
            "label": "B",
            "global_generation": 1,
        }
        assert (accounts.PROFILES_PATH / "B" / ".credentials.json").read_text() == live
        assert not accounts.CRED_FILE.exists()


class TestCmdRoutingMode:
    @pytest.mark.parametrize(
        ("command", "mode"),
        [(accounts.cmd_auto, "auto"), (accounts.cmd_fable, "fable")],
    )
    def test_explicit_mode_wins_over_a_login_pin_during_sync(
        self,
        tmp_path,
        monkeypatch,
        command,
        mode,
    ):
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {}})

        def sync(_blobs, *, persist):
            assert not persist
            accounts.save_mode("set", "target")
            return set()

        monkeypatch.setattr(accounts, "sync_profile_credentials", sync)
        monkeypatch.setattr(accounts, "route_rows", lambda *_args: [])
        monkeypatch.setattr(accounts, "excluded_labels", set)

        command(types.SimpleNamespace())

        assert accounts.load_mode() == {"mode": mode, "label": None}


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

    def test_labels_pane_local_set_mode(self, monkeypatch, capsys):
        monkeypatch.setattr(accounts, "locked", lambda blocking=True: nullcontext())
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {}})
        monkeypatch.setattr(accounts, "capture_live_to_blobs", lambda blobs: None)
        monkeypatch.setattr(accounts, "sync_profile_credentials", lambda blobs, persist: set())
        monkeypatch.setattr(
            accounts,
            "load_mode",
            lambda: {"mode": "set", "label": "work", "policy_scope": "pane"},
        )
        monkeypatch.setattr(accounts, "route_rows", lambda blobs, active, now: [])
        monkeypatch.setattr(accounts, "excluded_labels", set)

        accounts.cmd_status(types.SimpleNamespace())

        assert "mode: PANE → work" in capsys.readouterr().out

    def test_set_mode_status_matches_the_forced_runtime_target(
        self,
        monkeypatch,
        capsys,
    ):
        monkeypatch.setattr(
            accounts,
            "locked",
            lambda blocking=True: nullcontext(),
        )
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": {}})
        monkeypatch.setattr(
            accounts,
            "capture_live_to_blobs",
            lambda blobs: None,
        )
        monkeypatch.setattr(
            accounts,
            "sync_profile_credentials",
            lambda blobs, persist: set(),
        )
        monkeypatch.setattr(
            accounts,
            "load_mode",
            lambda: {"mode": "set", "label": "work"},
        )
        monkeypatch.setattr(
            accounts,
            "route_rows",
            lambda blobs, active, now: [
                accounts_row("safe", 5.0, seven_day=5.0),
                accounts_row(
                    "work",
                    100.0,
                    seven_day=100.0,
                    fable=100.0,
                ),
            ],
        )
        monkeypatch.setattr(accounts, "excluded_labels", lambda: {"work"})

        accounts.cmd_status(types.SimpleNamespace())

        output = capsys.readouterr().out
        assert "general: work" in output
        assert "fable: (none free)" in output


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

    def test_sync_repairs_an_empty_profile_keychain_item(
        self,
        tmp_path,
        monkeypatch,
    ):
        self._paths(tmp_path, monkeypatch)
        stored = _live_blob("stored")
        profile = accounts.ensure_native_profile("gmail", {"blob": stored})
        (profile / ".credentials.json").unlink()
        invalid_item = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "",
                    "refreshToken": "",
                    "refreshTokenExpiresAt": 3_000_000_000_000,
                }
            }
        )
        service = accounts.profile_keychain_service("gmail")
        deleted = []
        monkeypatch.setattr(
            accounts,
            "kc_read",
            lambda requested, account=None: (
                invalid_item if requested == service and not deleted else None
            ),
        )
        monkeypatch.setattr(
            accounts,
            "kc_delete",
            lambda requested: deleted.append(requested) or True,
        )
        blobs = {
            "accounts": {
                "gmail": {
                    "blob": stored,
                    "email": "same@example.com",
                    "org_uuid": "same-org",
                    "auth_dead_at": 1,
                }
            }
        }

        assert accounts.sync_profile_credentials(blobs, persist=False) == set()
        assert deleted == [service]
        assert (profile / ".credentials.json").read_text() == stored
        assert "auth_dead_at" not in blobs["accounts"]["gmail"]

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
        assert "auth_dead_at" not in blobs["accounts"]["gmail"]
        assert (profile / ".credentials.json").read_text() == old
        assert "repaired gmail profile login" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("initial_mode", "initial_label"),
        [("auto", None), ("set", "current")],
    )
    def test_known_profile_login_pins_the_target(
        self,
        tmp_path,
        monkeypatch,
        initial_mode,
        initial_label,
    ):
        self._paths(tmp_path, monkeypatch)
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        current_blob = _live_blob("current")
        login_blob = _live_blob("login")
        target_blob = _live_blob("target")
        current_profile = accounts.ensure_native_profile(
            "current",
            {"blob": current_blob},
        )
        target_profile = accounts.ensure_native_profile(
            "target",
            {"blob": target_blob},
        )
        accounts._write_0600(current_profile / ".credentials.json", login_blob)
        blobs = {
            "accounts": {
                "current": {
                    "blob": current_blob,
                    "email": "current@example.com",
                    "org_uuid": "current-org",
                },
                "target": {
                    "blob": target_blob,
                    "email": "target@example.com",
                    "org_uuid": "target-org",
                },
            }
        }
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda _token: {
                "account": {"email": "target@example.com"},
                "organization": {"uuid": "target-org"},
            },
        )
        monkeypatch.setattr(
            accounts,
            "load_label_pairs",
            lambda: [("target", "target@example.com", "target-org")],
        )
        accounts.save_mode(initial_mode, initial_label)

        assert accounts.sync_profile_credentials(blobs, persist=False) == set()
        assert accounts.load_mode() == {"mode": "set", "label": "target"}
        assert (current_profile / ".credentials.json").read_text() == current_blob
        assert (target_profile / ".credentials.json").read_text() == target_blob

    def test_known_fallback_profile_login_preserves_pane_scope(
        self,
        tmp_path,
        monkeypatch,
    ):
        self._paths(tmp_path, monkeypatch)
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(accounts, "PANE_PINS_PATH", tmp_path / "pane-pins")
        monkeypatch.setattr(accounts, "PANE_SALT_PATH", tmp_path / "pane-salt")
        monkeypatch.setattr(accounts, "_lock_depth", 0)
        monkeypatch.setattr(accounts, "pane_key", lambda env=None: "a" * 64)
        monkeypatch.setattr(
            accounts,
            "load_label_pairs",
            lambda: [("current", "current@example.com", "current-org")],
        )
        current_blob = _live_blob("current")
        login_blob = _live_blob("login")
        target_blob = _live_blob("target")
        current_profile = accounts.ensure_native_profile("current", {"blob": current_blob})
        accounts.ensure_native_profile("target", {"blob": target_blob})
        accounts._write_0600(current_profile / ".credentials.json", login_blob)
        blobs = {
            "accounts": {
                "current": {
                    "blob": current_blob,
                    "email": "current@example.com",
                    "org_uuid": "current-org",
                },
                "target": {
                    "blob": target_blob,
                    "email": "target@example.com",
                    "org_uuid": "target-org",
                },
            }
        }
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda _token: {
                "account": {"email": "target@example.com"},
                "organization": {"uuid": "target-org"},
            },
        )
        accounts.save_mode("auto", None)
        accounts.save_pane_pin("current")
        generation = json.loads(accounts.MODE_PATH.read_text())["global_generation"]
        accounts._write_0600(
            accounts.PANE_PINS_PATH / f'{"b" * 64}.json',
            json.dumps(
                {
                    "version": 1,
                    "label": "current",
                    "base_global_generation": generation,
                }
            ),
        )

        assert accounts.sync_profile_credentials(blobs, persist=False) == set()

        assert accounts.load_mode() == {
            "mode": "set",
            "label": "target",
            "policy_scope": "pane",
        }
        assert json.loads(accounts.MODE_PATH.read_text())["mode"] == "auto"
        other_pin = json.loads(
            (accounts.PANE_PINS_PATH / f'{"b" * 64}.json').read_text()
        )
        assert other_pin["label"] == "current"

    def test_known_profile_login_revives_and_pins_an_expired_target(
        self,
        tmp_path,
        monkeypatch,
    ):
        self._paths(tmp_path, monkeypatch)
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(accounts, "BLOBS_PATH", tmp_path / "blobs.json")
        current_blob = _live_blob("current")
        login_blob = _live_blob("login")
        expired_target = _live_blob("target", future_ms=1_000_000_000_000)
        current_profile = accounts.ensure_native_profile(
            "current",
            {"blob": current_blob},
        )
        target_profile = accounts.ensure_native_profile(
            "target",
            {"blob": expired_target},
        )
        accounts._write_0600(current_profile / ".credentials.json", login_blob)
        blobs = {
            "accounts": {
                "current": {
                    "blob": current_blob,
                    "email": "current@example.com",
                    "org_uuid": "current-org",
                },
                "target": {
                    "blob": expired_target,
                    "email": "target@example.com",
                    "org_uuid": "target-org",
                    "auth_dead_at": 1,
                },
            }
        }
        target_service = accounts.profile_keychain_service("target")
        deleted = []
        monkeypatch.setattr(
            accounts,
            "kc_read",
            lambda service, account=None: (
                expired_target if service == target_service and not deleted else None
            ),
        )
        monkeypatch.setattr(
            accounts,
            "kc_delete",
            lambda service: deleted.append(service) or True,
        )
        monkeypatch.setattr(
            accounts,
            "fetch_profile",
            lambda _token: {
                "account": {"email": "target@example.com"},
                "organization": {"uuid": "target-org"},
            },
        )
        monkeypatch.setattr(
            accounts,
            "load_label_pairs",
            lambda: [("target", "target@example.com", "target-org")],
        )
        accounts.save_mode("auto", None)

        assert accounts.sync_profile_credentials(blobs, persist=False) == set()
        assert accounts.load_mode() == {"mode": "set", "label": "target"}
        assert deleted == [target_service]
        assert blobs["accounts"]["target"]["blob"] == login_blob
        assert "auth_dead_at" not in blobs["accounts"]["target"]
        assert accounts.load_blobs()["accounts"]["target"]["blob"] == login_blob
        assert (current_profile / ".credentials.json").read_text() == current_blob
        assert (target_profile / ".credentials.json").read_text() == login_blob

    def test_removes_only_the_mismatched_profile_keychain_item(
        self,
        tmp_path,
        monkeypatch,
    ):
        self._paths(tmp_path, monkeypatch)
        old = _live_blob("old")
        new = _live_blob("new")
        profile = accounts.ensure_native_profile("gmail", {"blob": old})
        (profile / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "other@example.com"},
                    "userID": "wrong-user",
                    "theme": "dark",
                }
            )
        )
        blobs = {
            "accounts": {
                "gmail": {
                    "blob": old,
                    "email": "same@example.com",
                    "org_uuid": "same-org",
                    "auth_dead_at": 1,
                }
            }
        }
        service = accounts.profile_keychain_service("gmail")
        deleted = []
        monkeypatch.setattr(
            accounts,
            "kc_read",
            lambda requested, account=None: (
                new if requested == service and not deleted else None
            ),
        )
        monkeypatch.setattr(
            accounts,
            "kc_delete",
            lambda requested: deleted.append(requested) or True,
        )

        def profile_for(token):
            if token == "new":
                return {
                    "account": {"email": "other@example.com"},
                    "organization": {"uuid": "other-org"},
                }
            return {
                "account": {"email": "same@example.com"},
                "organization": {"uuid": "same-org"},
            }

        monkeypatch.setattr(accounts, "fetch_profile", profile_for)

        assert accounts.sync_profile_credentials(blobs, persist=False) == set()
        assert deleted == [service]
        assert "auth_dead_at" not in blobs["accounts"]["gmail"]
        assert accounts.profile_live_blob("gmail") == old
        state = json.loads((profile / ".claude.json").read_text())
        assert "oauthAccount" not in state
        assert "userID" not in state
        assert state["theme"] == "dark"

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
        monkeypatch.setattr(accounts, "_notify_needs_login", lambda _l: None)

        assert accounts.sync_profile_credentials(blobs, persist=False) == set()
        entry = blobs["accounts"]["gmail"]
        # The wrong profile identity is discarded and the stored identity remains usable.
        assert entry["blob"] == old
        assert "auth_dead_at" not in entry
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
        with pytest.raises(accounts.AccountsError, match="corrupt"):
            accounts.load_blobs()
        backups = list(tmp_path.glob("blobs.json.corrupt.*"))
        assert len(backups) == 1 and backups[0].read_text() == "NOT JSON {{{"
        with pytest.raises(accounts.AccountsError, match="corrupt"):
            accounts.load_blobs()
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

    def test_non_dict_store_is_preserved_and_rejected(self, tmp_path, monkeypatch):
        blobs_path = tmp_path / "blobs.json"
        blobs_path.write_text("[1, 2, 3]")
        monkeypatch.setattr(accounts, "BLOBS_PATH", blobs_path)
        monkeypatch.setattr(accounts, "MIRROR_LOG", tmp_path / "log")
        with pytest.raises(accounts.AccountsError, match="invalid"):
            accounts.load_blobs()
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

    def test_v1_mode_migrates_without_changing_selection(self, tmp_path, monkeypatch):
        path = tmp_path / "mode.json"
        path.write_text(json.dumps({"mode": "fable", "label": None}))
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "MODE_PATH", path)
        monkeypatch.setattr(accounts, "PANE_PINS_PATH", tmp_path / "pane-pins")
        monkeypatch.setattr(accounts, "_lock_depth", 0)

        assert accounts.load_mode() == {"mode": "fable", "label": None}
        assert json.loads(path.read_text()) == {
            "version": 2,
            "mode": "fable",
            "label": None,
            "global_generation": 0,
        }


class TestPanePolicy:
    def _paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(accounts, "PANE_PINS_PATH", tmp_path / "pane-pins")
        monkeypatch.setattr(accounts, "PANE_SALT_PATH", tmp_path / "pane-salt")
        monkeypatch.setattr(accounts, "_lock_depth", 0)

    def test_identity_precedence_and_hashing(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        env = {
            "TMUX": "/private/tmp/tmux-501/default,123,0",
            "TMUX_PANE": "%7",
            "ITERM_SESSION_ID": "w0t0p0:other",
        }
        key = accounts.pane_key(env=env)

        assert key == accounts.pane_key(env=env)
        assert len(key) == 64
        assert "/private/tmp" not in key
        assert "%7" not in key
        assert (accounts.PANE_SALT_PATH.stat().st_mode & 0o777) == 0o600

        changed_tmux = {**env, "TMUX_PANE": "%8"}
        assert accounts.pane_key(env=changed_tmux) != key

    @pytest.mark.parametrize(
        ("env", "prefix"),
        [
            ({"ITERM_SESSION_ID": "iterm", "TERM_SESSION_ID": "term"}, "iterm"),
            ({"TERM_SESSION_ID": "term", "WEZTERM_PANE": "9", "TERM_PROGRAM": "WezTerm"}, "term"),
            ({"WEZTERM_PANE": "9", "TERM_PROGRAM": "WezTerm"}, "wezterm"),
            ({"KITTY_WINDOW_ID": "4", "TERM": "xterm-kitty"}, "kitty"),
        ],
    )
    def test_identity_sources_are_ordered(self, env, prefix):
        material = accounts.pane_identity_material(env=env, tty_path=None, boot_id=None)
        assert material.decode().startswith(f"{prefix}:")

    def test_unverified_terminal_ids_and_no_tty_are_ambiguous(self):
        with pytest.raises(accounts.AccountsError, match="terminal pane"):
            accounts.pane_identity_material(
                env={"WEZTERM_PANE": "9", "KITTY_WINDOW_ID": "4"},
                tty_path=None,
                boot_id=None,
            )

    def test_pin_survives_relaunch_and_isolated_by_pane(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        monkeypatch.setattr(accounts, "load_label_pairs", lambda: [("work", "*", None)])
        monkeypatch.setattr(accounts, "pane_key", lambda env=None: "a" * 64)
        accounts.save_mode("auto", None)
        accounts.save_pane_pin("work")

        mode, generation = accounts.load_mode_snapshot()
        assert mode["mode"] == "set"
        assert mode["label"] == "work"
        assert mode["policy_scope"] == "pane"
        assert generation[0] == mode["global_generation"]

        monkeypatch.setattr(accounts, "pane_key", lambda env=None: "b" * 64)
        other_mode, _ = accounts.load_mode_snapshot()
        assert other_mode["mode"] == "auto"
        assert other_mode["policy_scope"] == "global"

    def test_global_change_invalidates_all_pins_and_increments_generation(
        self, tmp_path, monkeypatch
    ):
        self._paths(tmp_path, monkeypatch)
        accounts.save_mode("auto", None)
        first_generation = accounts.load_mode_snapshot()[0]["global_generation"]
        accounts.PANE_PINS_PATH.mkdir()
        for key in ("a" * 64, "b" * 64):
            accounts._write_0600(
                accounts.PANE_PINS_PATH / f"{key}.json",
                json.dumps({"version": 1, "label": "work", "base_global_generation": first_generation}),
            )

        accounts.save_mode("set", "work")

        mode, _ = accounts.load_mode_snapshot()
        assert mode["global_generation"] == first_generation + 1
        assert list(accounts.PANE_PINS_PATH.glob("*.json")) == []
        assert json.loads(accounts.MODE_PATH.read_text())["version"] == 2

    def test_pane_pin_precedes_legacy_launch_pin(self, monkeypatch):
        monkeypatch.setenv("ACCOUNTS_PIN", "legacy")
        monkeypatch.setattr(
            accounts,
            "load_mode",
            lambda: {"mode": "set", "label": "pane", "policy_scope": "pane"},
        )

        mode, pin, hard = accounts._route_preferences()

        assert mode["policy_scope"] == "pane"
        assert pin == "pane"
        assert hard is True


class TestStatuslineSnapshot:
    def _paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(accounts, "SNAPSHOT_PATH", tmp_path / "statusline-snapshot.json")
        monkeypatch.setattr(accounts, "MODE_PATH", tmp_path / "mode.json")
        monkeypatch.setattr(accounts, "RESETS_PATH", tmp_path / "account-resets.json")
        monkeypatch.setattr(accounts, "SESSION_LIMITS_PATH", tmp_path / "session-limits.json")
        monkeypatch.setattr(accounts, "PANE_PINS_PATH", tmp_path / "pane-pins")
        monkeypatch.setattr(accounts, "LOCK_PATH", tmp_path / "accounts.lock")
        monkeypatch.setattr(accounts, "_lock_depth", 0)

    def test_schema_redaction_reset_semantics_and_hard_limit(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        now = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)
        observed = (now - timedelta(hours=2)).timestamp()
        reset = (now - timedelta(hours=1)).isoformat()
        monkeypatch.setattr(accounts.time, "time", lambda: now.timestamp())
        monkeypatch.setattr(accounts, "now_utc", lambda: now)
        monkeypatch.setattr(accounts, "load_label_pairs", lambda: [("work", "secret@example.com", "org-secret")])
        monkeypatch.setattr(accounts, "load_session_leases", lambda now_ts=None: [{"label": "work"}])
        accounts.save_mode("auto", None)
        accounts.RESETS_PATH.write_text(json.dumps({
            "secret@example.com|org-secret": {
                "five_hour_pct": 74,
                "five_hour_reset": reset,
                "seven_day_pct": 22,
                "seven_day_reset": (now + timedelta(days=2)).isoformat(),
                "last_seen": observed,
                "scoped_limits": [
                    {"kind": "weekly_scoped", "label": "Model X", "used_pct": 31,
                     "resets_at": (now + timedelta(days=3)).isoformat()}
                ],
            }
        }))
        accounts.SESSION_LIMITS_PATH.write_text(json.dumps({
            "secret@example.com|org-secret": {"expires_at": now.timestamp() + 100}
        }))
        blobs = {"accounts": {"work": {
            "blob": _live_blob("token-secret"),
            "email": "secret@example.com",
            "org_uuid": "org-secret",
            "account_uuid": "account-secret",
        }, "undeclared": {"blob": "credential-body"}}}

        snapshot = accounts.write_statusline_snapshot(blobs, error=None)
        encoded = accounts.SNAPSHOT_PATH.read_text()

        assert snapshot["version"] == 1
        assert snapshot["accounts"]["work"]["five_hour"] == {
            "used_pct": 100.0,
            "resets_at": reset,
            "observed_at": observed,
            "stale": False,
            "pending_reset": True,
        }
        assert snapshot["accounts"]["work"]["scoped"][0]["label"] == "Model X"
        assert snapshot["accounts"]["work"]["live_leases"] == 1
        assert "undeclared" not in snapshot["accounts"]
        for secret in ("token-secret", "secret@example.com", "org-secret", "account-secret"):
            assert secret not in encoded
        assert (accounts.SNAPSHOT_PATH.stat().st_mode & 0o777) == 0o600

    def test_error_preserves_last_success(self, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        monkeypatch.setattr(accounts, "load_label_pairs", list)
        monkeypatch.setattr(accounts, "load_session_leases", lambda now_ts=None: [])
        moments = iter([100.0, 200.0])
        monkeypatch.setattr(accounts.time, "time", lambda: next(moments))
        accounts.save_mode("auto", None)

        accounts.write_statusline_snapshot({"accounts": {}}, error=None)
        failed = accounts.write_statusline_snapshot({"accounts": {}}, error="RuntimeError")

        assert failed["generated_at"] == 200.0
        assert failed["health"] == {"last_success_at": 100.0, "error": "RuntimeError"}


class TestWatch:
    def test_rejects_nonpositive_interval(self):
        parser = accounts.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["watch", "--interval", "0"])

    def test_singleton_and_clean_ctrl_c(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(accounts, "WATCH_LOCK_PATH", tmp_path / "watch.lock")
        calls = []
        monkeypatch.setattr(
            accounts,
            "poll_and_write_snapshot",
            lambda *, collector_locked=False: calls.append(collector_locked) or 1,
        )
        monkeypatch.setattr(accounts.time, "sleep", lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt))

        accounts.cmd_watch(types.SimpleNamespace(interval=10.0))

        assert calls == [True]
        assert "stopped" in capsys.readouterr().out
        with accounts.watch_lock():
            with pytest.raises(accounts.AccountsError, match="already running"):
                with accounts.watch_lock():
                    pass

    def test_failed_poll_writes_error_health_without_repolling(self, monkeypatch):
        blobs = {"accounts": {}}
        polls = []
        snapshots = []
        monkeypatch.setattr(accounts, "locked", lambda blocking=True: nullcontext())
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        monkeypatch.setattr(accounts, "sync_profile_credentials", lambda *_args, **_kwargs: set())
        monkeypatch.setattr(accounts, "refresh_dormant_profiles", lambda: 0)
        monkeypatch.setattr(accounts, "shared_snapshot_enabled", lambda: True)

        def fail_poll(_blobs):
            polls.append(True)
            raise RuntimeError("secret credential body")

        monkeypatch.setattr(accounts, "poll_blobs_usage", fail_poll)
        monkeypatch.setattr(
            accounts,
            "write_statusline_snapshot",
            lambda value, error: snapshots.append((value, error)),
        )

        with pytest.raises(RuntimeError):
            accounts.poll_and_write_snapshot(collector_locked=True)

        assert polls == [True]
        assert snapshots == [(blobs, "RuntimeError")]

    def test_snapshot_is_not_written_without_opt_in(self, monkeypatch):
        blobs = {"accounts": {}}
        snapshots = []
        monkeypatch.setattr(accounts, "locked", lambda blocking=True: nullcontext())
        monkeypatch.setattr(accounts, "load_blobs", lambda: blobs)
        monkeypatch.setattr(accounts, "sync_profile_credentials", lambda *_args, **_kwargs: set())
        monkeypatch.setattr(accounts, "refresh_dormant_profiles", lambda: 0)
        monkeypatch.setattr(accounts, "poll_blobs_usage", lambda _blobs: 0)
        monkeypatch.setattr(accounts, "shared_snapshot_enabled", lambda: False)
        monkeypatch.setattr(
            accounts,
            "write_statusline_snapshot",
            lambda *_args, **_kwargs: snapshots.append(True),
        )

        assert accounts.poll_and_write_snapshot(collector_locked=True) == 0
        assert snapshots == []


class TestFableEligible:
    def test_fable_just_under_full_is_eligible(self):
        assert accounts.fable_eligible(100.0, 100.0, 99.0) is True

    def test_fable_none_ineligible(self):
        assert accounts.fable_eligible(10.0, 10.0, None) is False

    @pytest.mark.parametrize("fable", [100.0, 101.0])
    def test_full_fable_window_is_ineligible(self, fable):
        assert accounts.fable_eligible(10.0, 10.0, fable) is False

    @pytest.mark.parametrize(
        ("five_hour", "seven_day"),
        [(None, None), (100.0, 10.0), (10.0, 100.0)],
    )
    def test_general_windows_do_not_affect_fable_eligibility(
        self,
        five_hour,
        seven_day,
    ):
        assert accounts.fable_eligible(five_hour, seven_day, 10.0) is True


class TestBindingPct:
    def test_worst_axis_wins(self):
        assert accounts.binding_pct(21.0, 92.0, 12.0) == 92.0

    def test_none_axes_ignored(self):
        assert accounts.binding_pct(30.0, None) == 30.0

    def test_all_none_sorts_last(self):
        assert accounts.binding_pct(None, None) == float("inf")


class TestPickEnvFable:
    NOW = 1_784_000_000.0
    VAULT = {"tokens": {
        "alpha": {"token": "sk-alpha", "expires_at": NOW + 1000},
        "bravo": {"token": "sk-bravo", "expires_at": NOW + 1000},
        "charlie": {"token": "sk-charlie", "expires_at": NOW + 1000},
    }}

    def test_fable_first_orders_eligible_by_fable_utilization(self):
        rows = [accounts_row("alpha", 10.0, fable=80.0),
                accounts_row("bravo", 50.0, fable=10.0),
                accounts_row("charlie", 20.0, fable=40.0)]
        assert [r["label"] for r in accounts._fable_first(rows)] == ["bravo", "charlie", "alpha"]

    def test_equal_binding_breaks_on_fable(self):
        # Both bind at 50 (their 5h); the fresher fable axis wins the tie.
        rows = [accounts_row("A", 50.0, fable=40.0, seven_day=10.0),
                accounts_row("B", 50.0, fable=20.0, seven_day=10.0)]
        assert [r["label"] for r in accounts._fable_first(rows)] == ["B", "A"]

    def test_lowest_fable_wins_when_rate_windows_are_usable(self):
        rows = [accounts_row("alpha", 21.0, fable=12.0, seven_day=79.0),
                accounts_row("bravo", 30.0, fable=19.0, seven_day=19.0)]
        assert accounts.pick_profile_route(
            accounts._fable_first(rows),
            set(),
            None,
            require_fable=True,
        ) == "alpha"

    def test_prefers_fable_over_headroom(self):
        rows = [accounts_row("alpha", 5.0, fable=100.0), accounts_row("bravo", 50.0, fable=10.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), self.VAULT, set(), self.NOW, None)
        assert picked == ("bravo", "sk-bravo")

    def test_falls_back_to_headroom_when_none_eligible(self):
        rows = [accounts_row("alpha", 5.0, fable=100.0), accounts_row("bravo", 50.0, fable=100.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), self.VAULT, set(), self.NOW, None)
        assert picked == ("alpha", "sk-alpha")

    def test_skips_eligible_without_token(self):
        vault = {"tokens": {"bravo": {"token": "sk-bravo", "expires_at": self.NOW + 1000}}}
        rows = [accounts_row("alpha", 20.0, fable=10.0), accounts_row("bravo", 30.0, fable=40.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), vault, set(), self.NOW, None)
        assert picked == ("bravo", "sk-bravo")

    def test_weekly_ceiling_does_not_hide_fable_headroom(self):
        rows = [accounts_row("alpha", 0.0, fable=8.0, seven_day=100.0),
                accounts_row("bravo", 34.0, fable=41.0, seven_day=52.0)]
        picked = accounts.pick_route(accounts._fable_first(rows), self.VAULT, set(), self.NOW, None)
        assert picked == ("alpha", "sk-alpha")


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

    def test_missing_last_seen_is_stale(self, monkeypatch):
        monkeypatch.setattr(accounts, "load_resets", lambda: {})
        blobs = {"accounts": {"gmail": {"blob": _live_blob("g"), "email": "g@x", "org_uuid": "o1"}}}
        rows = accounts.route_rows(blobs, None, time.time())
        assert rows[0]["stale"] is True


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
        now = time.time()
        resets = {
            key: {"last_seen": now, **row}
            for key, row in (resets or {}).items()
        }
        monkeypatch.setattr(accounts, "load_blobs", lambda: {"accounts": blobs})
        monkeypatch.setattr(accounts, "load_resets", lambda: resets)
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

    def test_pin_falls_back_when_near_quota(self, tmp_path, monkeypatch, capsys):
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
        assert "export ACCOUNTS_ROUTED_LABEL=gmail" in out

    def test_set_mode_ignores_quota_and_exclusions(self, tmp_path, monkeypatch, capsys):
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
            excludes={"work"},
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
    def test_session_limit_markers_cap_only_the_matching_column(self, tmp_path):
        repo = Path(__file__).resolve().parent.parent
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        accounts_dir = home / ".accounts"
        claude_dir.mkdir(parents=True)
        accounts_dir.mkdir()
        (claude_dir / "statusline.conf").write_text(
            'SHOW_ACCOUNT_RESETS=1\n'
            'MAX_COLS=200\n'
            'ACCOUNT_LABELS="current:current@example.com|current-org '
            'fable-only:fable@example.com|fable-org '
            'session-only:session@example.com|session-org"\n'
        )
        now = time.time()
        (claude_dir / "account-resets.json").write_text(
            json.dumps(
                {
                    "current@example.com|current-org": {
                        "email": "current@example.com",
                        "org_uuid": "current-org",
                        "five_hour_pct": 1,
                        "seven_day_pct": 2,
                        "fable_pct": 3,
                        "last_seen": now,
                    },
                    "fable@example.com|fable-org": {
                        "email": "fable@example.com",
                        "org_uuid": "fable-org",
                        "five_hour_pct": 12,
                        "seven_day_pct": 34,
                        "fable_pct": 56,
                        "last_seen": now,
                    },
                    "session@example.com|session-org": {
                        "email": "session@example.com",
                        "org_uuid": "session-org",
                        "five_hour_pct": 23,
                        "seven_day_pct": 45,
                        "fable_pct": 67,
                        "last_seen": now,
                    },
                }
            )
        )
        (accounts_dir / "session-limits.json").write_text(
            json.dumps(
                {
                    "fable@example.com|fable-org|fable": {
                        "expires_at": now + 3600,
                    },
                    "session@example.com|session-org": {
                        "expires_at": now + 3600,
                    },
                }
            )
        )
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
                "ACCOUNTS_ROUTED_LABEL": "current",
                "ACCOUNTS_ROUTED_EMAIL": "current@example.com",
                "ACCOUNTS_ROUTED_ORG_UUID": "current-org",
            }
        )
        env.pop("CLAUDE_CONFIG_DIR", None)

        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(fixture),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )

        rendered = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
        fable_row = next(
            line for line in rendered.splitlines() if "Fable-only" in line
        )
        session_row = next(
            line for line in rendered.splitlines() if "Session-only" in line
        )
        assert re.search(r"Fable-only\s+12%.*34%\s+100%", fable_row)
        assert re.search(r"Session-only\s+100%.*45%\s+67%", session_row)

    def _render_reset_row(self, tmp_path, last_seen):
        repo = Path(__file__).resolve().parent.parent
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "statusline.conf").write_text(
            'SHOW_ACCOUNT_RESETS=1\n'
            'MAX_COLS=200\n'
            'ACCOUNT_LABELS="current:current@example.com|current-org '
            'forced:forced@example.com|forced-org"\n'
        )
        now = int(time.time())
        resets = {
            "current@example.com|current-org": {
                "email": "current@example.com",
                "org_uuid": "current-org",
                "five_hour_pct": 1,
                "seven_day_pct": 1,
                "last_seen": now,
            },
            "forced@example.com|forced-org": {
                "email": "forced@example.com",
                "org_uuid": "forced-org",
                "five_hour_pct": 100,
                "five_hour_reset": datetime.fromtimestamp(
                    now - 3600, tz=timezone.utc
                ).isoformat(),
                "seven_day_pct": 30,
                "last_seen": last_seen,
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
                "ACCOUNTS_ROUTED_LABEL": "current",
                "ACCOUNTS_ROUTED_EMAIL": "current@example.com",
                "ACCOUNTS_ROUTED_ORG_UUID": "current-org",
            }
        )
        env.pop("CLAUDE_CONFIG_DIR", None)
        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(fixture),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        rendered = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
        return next(line for line in rendered.splitlines() if "Forced" in line)

    def test_unconfirmed_reset_keeps_last_confirmed_utilization(self, tmp_path):
        row = self._render_reset_row(tmp_path, int(time.time()) - 7200)

        assert re.search(r"Forced\s+100%(?:\s|$)", row)
        assert "reset pending" in row

    def test_post_reset_poll_confirms_empty_window(self, tmp_path):
        row = self._render_reset_row(tmp_path, int(time.time()) - 1800)

        assert re.search(r"Forced\s+0%(?:\s|$)", row)
        assert "reset pending" not in row

    def test_fresher_account_ledger_replaces_stale_usage_cache(self, tmp_path):
        repo = Path(__file__).resolve().parent.parent
        now = int(time.time())
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "statusline.conf").write_text("MAX_COLS=200\n")
        (claude_dir / "account-resets.json").write_text(
            json.dumps(
                {
                    "current@example.com|current-org": {
                        "email": "current@example.com",
                        "org_uuid": "current-org",
                        "five_hour_pct": 90,
                        "five_hour_reset": datetime.fromtimestamp(
                            now + 14400, tz=timezone.utc
                        ).isoformat(),
                        "seven_day_pct": 40,
                        "fable_pct": 53,
                        "last_seen": now - 1800,
                    }
                }
            )
        )
        cache = tmp_path / "cache"
        cache.mkdir()
        usage_cache = cache / "statusline-usage-cache-current.json"
        usage_cache.write_text(
            json.dumps(
                {
                    "five_hour": {
                        "utilization": 85,
                        "resets_at": datetime.fromtimestamp(
                            now - 1800, tz=timezone.utc
                        ).isoformat(),
                    },
                    "seven_day": {"utilization": 37},
                }
            )
        )
        os.utime(usage_cache, (now - 3600, now - 3600))
        cache_clock = tmp_path / "cache-clock"
        cache_clock.touch()
        os.utime(cache_clock, (now - 1200, now - 1200))
        (cache / "statusline-usage-prev-current.json").write_text(
            json.dumps(
                {
                    "ts": now - 7200,
                    "five_hour": 80,
                    "seven_day": 30,
                }
            )
        )
        (cache / "statusline-profile-cache-current.json").write_text(
            json.dumps(
                {
                    "account": {"email": "current@example.com"},
                    "organization": {"uuid": "current-org"},
                }
            )
        )
        profile = tmp_path / "profile"
        profile.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        race_marker = tmp_path / "cache-replaced"
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_cat = fake_bin / "cat"
        fake_cat.write_text(
            """#!/usr/bin/env bash
if [ "$1" = "$RACE_CACHE_FILE" ] && [ ! -e "$RACE_MARKER" ]; then
    /bin/cat "$1"
    : > "$RACE_MARKER"
    /bin/cp "$1" "${RACE_CACHE_FILE}.replacement"
    /bin/mv "${RACE_CACHE_FILE}.replacement" "$RACE_CACHE_FILE"
    touch -r "$RACE_CACHE_CLOCK" "$RACE_CACHE_FILE"
    exit 0
fi
exec /bin/cat "$@"
"""
        )
        fake_cat.chmod(0o755)
        fixture = json.loads((repo / "test/fixtures/input.json").read_text())
        fixture["workspace"]["current_dir"] = str(project)
        script = tmp_path / "statusline.sh"
        script.write_text(
            (repo / "bin/statusline.sh")
            .read_text()
            .replace("/tmp/claude", str(cache))
        )
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "TZ": "UTC",
                "CLAUDE_CONFIG_DIR": str(profile),
                "ACCOUNTS_ROUTED_LABEL": "current",
                "ACCOUNTS_ROUTED_EMAIL": "current@example.com",
                "ACCOUNTS_ROUTED_ORG_UUID": "current-org",
                "PATH": f"{fake_bin}:{env['PATH']}",
                "RACE_CACHE_FILE": str(usage_cache),
                "RACE_CACHE_CLOCK": str(cache_clock),
                "RACE_MARKER": str(race_marker),
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
        session = next(line for line in rendered.splitlines() if line.startswith("session"))

        assert race_marker.exists()
        assert re.search(r"session\s+.*\s90%(?:\s|$)", session)
        assert "100%" not in session
        assert "85%" not in session

    def test_excluded_account_is_not_rendered(self, tmp_path):
        repo = Path(__file__).resolve().parent.parent
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "statusline.conf").write_text(
            'SHOW_ACCOUNT_RESETS=1\n'
            'MAX_COLS=200\n'
            'ACCOUNT_LABELS="acme-max:jane.doe@acme.ai|acme-org '
            'alumni:*@alumni.example.edu"\n'
            'ACCOUNTS_EXCLUDE="alumni"\n'
        )
        resets = {
            "jane.doe@acme.ai|acme-org": {
                "email": "jane.doe@acme.ai",
                "org_uuid": "acme-org",
                "five_hour_pct": 18,
                "seven_day_pct": 20,
                "fable_pct": 24,
                "last_seen": time.time(),
            },
            "jane@alumni.example.edu|alumni-org": {
                "email": "jane@alumni.example.edu",
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
                "ACCOUNTS_ROUTED_EMAIL": "jane.doe@acme.ai",
                "ACCOUNTS_ROUTED_ORG_UUID": "acme-org",
            }
        )
        # Hermetic: a real session exports its own profile dir, which would
        # replace the fixture launch identity and blank the board.
        env.pop("CLAUDE_CONFIG_DIR", None)

        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(fixture),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        rendered = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)

        assert "Acme-max" in rendered
        assert "alumni" not in rendered
        assert (tmp_path / "cache/statusline-usage-cache.json").is_symlink()
        assert (tmp_path / "cache/statusline-profile-cache.json").is_symlink()

    @pytest.mark.parametrize("credential_source", ["file", "keychain"])
    def test_in_profile_login_replaces_launch_identity_before_ledger_write(
        self,
        tmp_path,
        credential_source,
    ):
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
        credential_blob = json.dumps(
            {"claudeAiOauth": {"accessToken": "new-token"}}
        )
        if credential_source == "file":
            (profile / ".credentials.json").write_text(credential_blob)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        if credential_source == "keychain":
            fake_security = fake_bin / "security"
            fake_security.write_text(
                "#!/usr/bin/env bash\nprintf '%s' '" + credential_blob + "'\n"
            )
            fake_security.chmod(0o755)
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

    def test_best_next_marker_matches_route_mode_and_quarantine(
        self,
        tmp_path,
    ):
        repo = Path(__file__).resolve().parent.parent
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        accounts_dir = home / ".accounts"
        claude_dir.mkdir(parents=True)
        accounts_dir.mkdir()
        (claude_dir / "statusline.conf").write_text(
            'SHOW_ACCOUNT_RESETS=1\n'
            'MAX_COLS=200\n'
            'ACCOUNT_LABELS="current:current@example.com|cur-org '
            'walled:walled@example.com|walled-org '
            'quarantined:quarantined@example.com|quarantined-org '
            'fresh:fresh@example.com|fresh-org"\n'
        )
        resets = {
            "current@example.com|cur-org": {
                "email": "current@example.com",
                "org_uuid": "cur-org",
                "five_hour_pct": 75,
                "seven_day_pct": 30,
                "last_seen": time.time(),
            },
            # Fresh 5h window but weekly-walled — the router would never pick it.
            "walled@example.com|walled-org": {
                "email": "walled@example.com",
                "org_uuid": "walled-org",
                "five_hour_pct": 5,
                "seven_day_pct": 97,
                "last_seen": time.time(),
            },
            "quarantined@example.com|quarantined-org": {
                "email": "quarantined@example.com",
                "org_uuid": "quarantined-org",
                "five_hour_pct": 5,
                "seven_day_pct": 5,
                "last_seen": time.time(),
            },
            "fresh@example.com|fresh-org": {
                "email": "fresh@example.com",
                "org_uuid": "fresh-org",
                "five_hour_pct": 20,
                "seven_day_pct": 20,
                "last_seen": time.time(),
            },
        }
        (claude_dir / "account-resets.json").write_text(json.dumps(resets))
        (accounts_dir / "session-limits.json").write_text(
            json.dumps(
                {
                    "quarantined@example.com|quarantined-org": {
                        "expires_at": time.time() + 3600,
                    },
                    "fresh@example.com|fresh-org": {
                        "expires_at": time.time() - 1,
                    },
                }
            )
        )
        profile_payload = json.dumps(
            {"account": {"email": "current@example.com"}, "organization": {"uuid": "cur-org"}}
        )
        usage_payload = json.dumps(
            {"five_hour": {"utilization": 75}, "seven_day": {"utilization": 30}}
        )
        # Pre-seeded fresh caches + matching token hash: no refresh, no network —
        # the render reads exactly this identity/usage (marker needs current 5h ≥ 70).
        token = "statusline-test-token"
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "statusline-token-hash-current").write_text(
            hashlib.sha256(token.encode()).hexdigest()[:16] + "\n"
        )
        (cache / "statusline-profile-cache-current.json").write_text(profile_payload)
        (cache / "statusline-usage-cache-current.json").write_text(usage_payload)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            f"  *oauth/profile*) printf '%s' '{profile_payload}' ;;\n"
            f"  *oauth/usage*) printf '%s' '{usage_payload}' ;;\n"
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
            .replace("/tmp/claude", str(cache))
        )
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PATH": f"{fake_bin}:{env['PATH']}",
                "TZ": "UTC",
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "ACCOUNTS_ROUTED_LABEL": "current",
                "ACCOUNTS_ROUTED_EMAIL": "current@example.com",
                "ACCOUNTS_ROUTED_ORG_UUID": "cur-org",
            }
        )
        # Hermetic: a real session exports its own profile dir, which would
        # replace the fixture launch identity and blank the board.
        env.pop("CLAUDE_CONFIG_DIR", None)

        def render():
            result = subprocess.run(
                ["bash", str(script)],
                input=json.dumps(fixture),
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            return re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)

        rendered = render()
        marked = [line for line in rendered.splitlines() if "✓ best next" in line]
        quarantined = next(
            line for line in rendered.splitlines() if "Quarantined" in line
        )

        assert marked, rendered
        assert all("Fresh" in line for line in marked), rendered
        assert not any("Walled" in line for line in marked), rendered
        assert not any("Quarantined" in line for line in marked), rendered
        assert "100%" in quarantined

        resets["walled@example.com|walled-org"]["seven_day_pct"] = 85
        (claude_dir / "account-resets.json").write_text(
            json.dumps(
                {
                    key: value
                    for key, value in resets.items()
                    if key
                    in {
                        "current@example.com|cur-org",
                        "walled@example.com|walled-org",
                    }
                }
            )
        )
        (accounts_dir / "session-limits.json").write_text("{}")

        rendered = render()
        assert "✓ best next" not in rendered, rendered

        resets["walled@example.com|walled-org"].pop("seven_day_pct")
        (claude_dir / "account-resets.json").write_text(
            json.dumps(
                {
                    "current@example.com|cur-org": resets[
                        "current@example.com|cur-org"
                    ],
                    "walled@example.com|walled-org": resets[
                        "walled@example.com|walled-org"
                    ],
                }
            )
        )

        rendered = render()
        assert "✓ best next" not in rendered, rendered

        (claude_dir / "account-resets.json").write_text(
            json.dumps(
                {
                    "current@example.com|cur-org": resets[
                        "current@example.com|cur-org"
                    ],
                    "walled@example.com|walled-org": {
                        "email": "walled@example.com",
                        "org_uuid": "walled-org",
                        "five_hour_pct": 79.6,
                        "seven_day_pct": 79.6,
                        "last_seen": time.time(),
                    },
                }
            )
        )
        rendered = render()
        marked = [line for line in rendered.splitlines() if "✓ best next" in line]
        assert marked and all("Walled" in line for line in marked), rendered

        (accounts_dir / "mode.json").write_text('{"mode":"fable"}')
        (claude_dir / "account-resets.json").write_text(
            json.dumps(
                {
                    "current@example.com|cur-org": resets[
                        "current@example.com|cur-org"
                    ],
                    "fresh@example.com|fresh-org": {
                        "email": "fresh@example.com",
                        "org_uuid": "fresh-org",
                        "five_hour_pct": 100,
                        "seven_day_pct": 100,
                        "fable_pct": 99,
                        "last_seen": time.time(),
                    },
                }
            )
        )
        rendered = render()
        marked = [line for line in rendered.splitlines() if "✓ best next" in line]
        assert marked and all("Fresh" in line for line in marked), rendered

        resets["fresh@example.com|fresh-org"]["fable_pct"] = 100
        (claude_dir / "account-resets.json").write_text(json.dumps(resets))
        rendered = render()
        assert "✓ best next" not in rendered, rendered

        (claude_dir / "account-resets.json").write_text(
            json.dumps(
                {
                    "current@example.com|cur-org": resets[
                        "current@example.com|cur-org"
                    ],
                    "walled@example.com|walled-org": {
                        "email": "walled@example.com",
                        "org_uuid": "walled-org",
                        "five_hour_pct": 5,
                        "seven_day_pct": 5,
                        "fable_pct": 50,
                        "last_seen": time.time(),
                    },
                    "fresh@example.com|fresh-org": {
                        "email": "fresh@example.com",
                        "org_uuid": "fresh-org",
                        "five_hour_pct": 70,
                        "seven_day_pct": 70,
                        "fable_pct": 10,
                        "last_seen": time.time(),
                    },
                }
            )
        )
        rendered = render()
        marked = [line for line in rendered.splitlines() if "✓ best next" in line]
        assert marked and all("Fresh" in line for line in marked), rendered

        (claude_dir / "statusline.conf").write_text(
            'SHOW_ACCOUNT_RESETS=1\n'
            'MAX_COLS=200\n'
            'ACCOUNT_LABELS="current:current@example.com|cur-org '
            'zulu:a@example.com|a-org alpha:z@example.com|z-org"\n'
        )
        (accounts_dir / "mode.json").unlink()
        (claude_dir / "account-resets.json").write_text(
            json.dumps(
                {
                    "current@example.com|cur-org": resets[
                        "current@example.com|cur-org"
                    ],
                    "a@example.com|a-org": {
                        "email": "a@example.com",
                        "org_uuid": "a-org",
                        "five_hour_pct": 10,
                        "seven_day_pct": 20,
                        "last_seen": time.time(),
                    },
                    "z@example.com|z-org": {
                        "email": "z@example.com",
                        "org_uuid": "z-org",
                        "five_hour_pct": 10,
                        "seven_day_pct": 20,
                        "last_seen": time.time(),
                    },
                }
            )
        )

        rendered = render()
        marked = [line for line in rendered.splitlines() if "✓ best next" in line]
        assert marked and all("Alpha" in line for line in marked), rendered

        set_mode_config = (
            'SHOW_ACCOUNT_RESETS=1\n'
            'MAX_COLS=200\n'
            'ACCOUNT_LABELS="current:current@example.com|cur-org '
            'target:target@example.com|target-org '
            'safe:safe@example.com|safe-org"\n'
        )
        (claude_dir / "statusline.conf").write_text(
            set_mode_config + 'ACCOUNTS_EXCLUDE="target"\n'
        )
        (accounts_dir / "mode.json").write_text('{"mode":"set","label":"target"}')
        (claude_dir / "account-resets.json").write_text(
            json.dumps(
                {
                    "current@example.com|cur-org": resets[
                        "current@example.com|cur-org"
                    ],
                    "target@example.com|target-org": {
                        "email": "target@example.com",
                        "org_uuid": "target-org",
                        "five_hour_pct": 100,
                        "seven_day_pct": 100,
                        "last_seen": time.time(),
                    },
                    "safe@example.com|safe-org": {
                        "email": "safe@example.com",
                        "org_uuid": "safe-org",
                        "five_hour_pct": 10,
                        "seven_day_pct": 10,
                        "last_seen": time.time(),
                    },
                }
            )
        )
        target_blob = {
            "email": "target@example.com",
            "org_uuid": "target-org",
            "blob": json.dumps(
                {
                    "claudeAiOauth": {
                        "refreshToken": "rt",
                        "refreshTokenExpiresAt": int((time.time() + 3600) * 1000),
                    }
                }
            ),
        }
        (accounts_dir / "blobs.json").write_text(
            json.dumps({"accounts": {"target": target_blob}})
        )

        rendered = render()
        marked = [line for line in rendered.splitlines() if "✓ best next" in line]
        assert marked and all("Target" in line for line in marked), rendered
        assert not any("Safe" in line for line in marked), rendered

        (accounts_dir / "mode.json").write_text('{"mode":"set","label":"current"}')
        rendered = render()
        assert "✓ best next" not in rendered, rendered

        (claude_dir / "statusline.conf").write_text(set_mode_config)
        (accounts_dir / "mode.json").write_text('{"mode":"set","label":"target"}')
        target_blob["auth_dead_at"] = int(time.time())
        (accounts_dir / "blobs.json").write_text(
            json.dumps({"accounts": {"target": target_blob}})
        )

        rendered = render()
        assert "✓ best next" not in rendered, rendered
        assert "Target" in rendered and "needs reauth" in rendered, rendered

        del target_blob["auth_dead_at"]
        target_blob["blob"] = json.dumps(
            {
                "claudeAiOauth": {
                    "refreshToken": "rt",
                    "refreshTokenExpiresAt": int((time.time() - 60) * 1000),
                }
            }
        )
        (accounts_dir / "blobs.json").write_text(
            json.dumps({"accounts": {"target": target_blob}})
        )

        rendered = render()
        assert "✓ best next" not in rendered, rendered
        assert "Target" in rendered and "needs reauth" in rendered, rendered


# ---- Verified auth state (auth_dead_at) ----


def _blob(access_exp_ms: int | None = None, refresh: str | None = "rt") -> str:
    oauth: dict = {"accessToken": "at"}
    if access_exp_ms is not None:
        oauth["expiresAt"] = access_exp_ms
    if refresh is not None:
        oauth["refreshToken"] = refresh
    return json.dumps({"claudeAiOauth": oauth})


NOW_TS = 1_800_000_000.0
LIVE_MS = int((NOW_TS + 3600) * 1000)
PAST_MS = int((NOW_TS - 3600) * 1000)


class TestEntryNeedsLogin:
    def test_alive_blob_no_flag(self):
        assert accounts.entry_needs_login({"blob": _blob(LIVE_MS)}, NOW_TS) is False

    def test_auth_dead_flag_wins_over_alive_metadata(self):
        entry = {"blob": _blob(LIVE_MS), "auth_dead_at": int(NOW_TS) - 5}
        assert accounts.entry_needs_login(entry, NOW_TS) is True

    def test_metadata_dead_blob(self):
        assert accounts.entry_needs_login({"blob": _blob(LIVE_MS, refresh=None)}, NOW_TS) is True

    def test_set_entry_blob_clears_flag(self):
        entry = {"blob": _blob(PAST_MS), "auth_dead_at": 123}
        accounts.set_entry_blob(entry, _blob(LIVE_MS))
        assert "auth_dead_at" not in entry
        assert accounts.entry_needs_login(entry, NOW_TS) is False


class TestMarkAuthDead:
    def test_notifies_only_on_transition(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(accounts, "_notify_needs_login", calls.append)
        entry: dict = {}
        accounts.mark_auth_dead("gmail", entry, NOW_TS)
        accounts.mark_auth_dead("gmail", entry, NOW_TS + 60)
        assert calls == ["gmail"]
        assert entry["auth_dead_at"] == int(NOW_TS + 60)


class TestNotifyNeedsLogin:
    def test_silent_unless_opted_in(self, monkeypatch):
        runs: list[list[str]] = []
        monkeypatch.setattr(accounts.subprocess, "run", lambda cmd, **_kw: runs.append(cmd))
        monkeypatch.setattr(accounts, "_conf_var", lambda _name: "")
        monkeypatch.delenv("STATUSLINE_NOTIFY", raising=False)
        accounts._notify_needs_login("gmail")
        assert runs == []
        monkeypatch.setenv("STATUSLINE_NOTIFY", "1")
        accounts._notify_needs_login("gmail")
        assert len(runs) == 1 and runs[0][0] == "osascript"


class TestVerifyEntryAuth:
    @pytest.fixture(autouse=True)
    def _no_keychain(self, monkeypatch):
        # Hermetic: never read the real keychain; item-backed behavior has
        # its own tests below.
        monkeypatch.setattr(accounts, "kc_read", lambda *a, **k: None)

    def test_live_access_token_short_circuits(self, monkeypatch):
        def boom(_blob):
            raise AssertionError("refresh must not run for a live access token")

        monkeypatch.setattr(accounts, "refresh_blob_access", boom)
        entry = {"blob": _blob(LIVE_MS)}
        assert accounts.verify_entry_auth("gmail", entry, NOW_TS) == "ok"

    def test_rejected_refresh_marks_dead(self, monkeypatch):
        monkeypatch.setattr(accounts, "_notify_needs_login", lambda _l: None)

        def rejected(_blob):
            raise accounts.TokenRefreshError("401")

        monkeypatch.setattr(accounts, "refresh_blob_access", rejected)
        entry = {"blob": _blob(PAST_MS)}
        assert accounts.verify_entry_auth("gmail", entry, NOW_TS) == "dead"
        assert entry["auth_dead_at"] == int(NOW_TS)

    def test_throttle_is_unavailable_not_dead(self, monkeypatch):
        def throttled(_blob):
            raise accounts.TokenRefreshError("429")

        monkeypatch.setattr(accounts, "refresh_blob_access", throttled)
        entry = {"blob": _blob(PAST_MS)}
        assert accounts.verify_entry_auth("gmail", entry, NOW_TS) == "unavailable"
        assert "auth_dead_at" not in entry

    def test_no_usable_refresh_token_marks_dead(self, monkeypatch):
        monkeypatch.setattr(accounts, "_notify_needs_login", lambda _l: None)
        monkeypatch.setattr(accounts, "refresh_blob_access", lambda _b: None)
        entry = {"blob": _blob(PAST_MS)}
        assert accounts.verify_entry_auth("gmail", entry, NOW_TS) == "dead"

    def test_successful_rotation_persists_and_clears(self, monkeypatch):
        written: list[tuple[str, str]] = []
        rotated = _blob(LIVE_MS)
        monkeypatch.setattr(accounts, "refresh_blob_access", lambda _b: rotated)
        monkeypatch.setattr(
            accounts, "write_profile_credentials", lambda label, blob: written.append((label, blob))
        )
        entry = {"blob": _blob(PAST_MS), "auth_dead_at": 123}
        assert accounts.verify_entry_auth("gmail", entry, NOW_TS) == "ok_rotated"
        assert entry["blob"] == rotated
        assert "auth_dead_at" not in entry
        assert written == [("gmail", rotated)]


class TestAuthDeadRouting:
    def test_route_rows_marks_flagged_entry_expired_and_pick_skips(self, monkeypatch):
        monkeypatch.setattr(
            accounts,
            "load_resets",
            lambda: {
                "a@x|1": {"last_seen": NOW_TS},
                "b@x|2": {"last_seen": NOW_TS},
            },
        )
        blobs = {
            "accounts": {
                "dead": {
                    "blob": _blob(LIVE_MS),
                    "email": "a@x",
                    "org_uuid": "1",
                    "auth_dead_at": 5,
                },
                "alive": {"blob": _blob(LIVE_MS), "email": "b@x", "org_uuid": "2"},
            }
        }
        rows = accounts.route_rows(blobs, None, NOW_TS)
        by_label = {r["label"]: r for r in rows}
        assert by_label["dead"]["expired"] is True
        assert by_label["alive"]["expired"] is False
        for row in rows:
            row["five_hour"], row["seven_day"] = 10.0, 10.0
        assert accounts.pick_profile_route(rows, set(), None) == "alive"


class TestCaptureLiveClearsFlag:
    def test_matching_live_token_clears_auth_dead(self, monkeypatch, tmp_path):
        live = _blob(LIVE_MS)
        monkeypatch.setattr(accounts, "live_cred", lambda: (live, "keychain"))
        saved: list[dict] = []
        monkeypatch.setattr(accounts, "save_blobs", saved.append)
        blobs = {"accounts": {"acme-max": {"blob": live, "auth_dead_at": 99}}}
        assert accounts.capture_live_to_blobs(blobs) == "acme-max"
        assert "auth_dead_at" not in blobs["accounts"]["acme-max"]
        assert saved, "clear must persist"

    def test_matching_live_token_without_flag_saves_nothing(self, monkeypatch):
        live = _blob(LIVE_MS)
        monkeypatch.setattr(accounts, "live_cred", lambda: (live, "keychain"))
        monkeypatch.setattr(
            accounts, "save_blobs", lambda _b: (_ for _ in ()).throw(AssertionError("no write"))
        )
        blobs = {"accounts": {"acme-max": {"blob": live}}}
        assert accounts.capture_live_to_blobs(blobs) == "acme-max"


class TestProfileKeychain:
    def test_service_name_is_sha256_prefix_of_config_dir(self):
        import hashlib as _h

        svc = accounts.profile_keychain_service("gmail")
        digest = _h.sha256(str(accounts.native_profile_path("gmail")).encode()).hexdigest()[:8]
        assert svc == f"Claude Code-credentials-{digest}"

    def test_live_blob_prefers_item_over_file(self, monkeypatch):
        monkeypatch.setattr(accounts, "kc_read", lambda _svc: "ITEM")
        assert accounts.profile_live_blob("gmail") == "ITEM"

    def test_live_blob_falls_back_to_file_then_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(accounts, "kc_read", lambda _svc: None)
        monkeypatch.setattr(accounts, "PROFILES_PATH", tmp_path)
        assert accounts.profile_live_blob("gmail") is None
        prof = tmp_path / "gmail"
        prof.mkdir()
        (prof / ".credentials.json").write_text("FILE")
        assert accounts.profile_live_blob("gmail") == "FILE"

    def test_item_backed_profile_routes_without_refresh(self, monkeypatch):
        monkeypatch.setattr(accounts, "kc_read", lambda _svc: _blob(LIVE_MS))

        def boom(_blob):
            raise AssertionError("must not exercise cc-owned lineage")

        monkeypatch.setattr(accounts, "refresh_blob_access", boom)
        entry = {"blob": _blob(PAST_MS)}
        assert accounts.verify_entry_auth("gmail", entry, NOW_TS) == "ok"

    def test_metadata_dead_item_marks_needs_login(self, monkeypatch):
        # An item whose refresh expiry lapsed: cc keeps choosing it by
        # existence, so this is a true needs-/login, not a routable row.
        dead_item = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "at",
                    "refreshToken": "rt",
                    "refreshTokenExpiresAt": int((NOW_TS - 60) * 1000),
                }
            }
        )
        monkeypatch.setattr(accounts, "kc_read", lambda _svc: dead_item)
        monkeypatch.setattr(accounts, "_notify_needs_login", lambda _l: None)
        entry = {"blob": _blob(PAST_MS)}
        assert accounts.verify_entry_auth("gmail", entry, NOW_TS) == "dead"
        assert entry["auth_dead_at"] == int(NOW_TS)


class TestSyncReadsLiveLineage:
    def test_imports_rotation_from_item_and_clears_flag(self, monkeypatch):
        fresh = _blob(LIVE_MS)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _l: fresh)
        monkeypatch.setattr(accounts, "fetch_profile", lambda _t: {"ident": 1})
        monkeypatch.setattr(
            accounts,
            "identity_from_profile",
            lambda _p: {"email": "a@x", "org_uuid": "1", "org_type": "t"},
        )
        blobs = {
            "accounts": {
                "gmail": {
                    "blob": _blob(PAST_MS),
                    "email": "a@x",
                    "org_uuid": "1",
                    "auth_dead_at": 9,
                }
            }
        }
        blocked = accounts.sync_profile_credentials(blobs, persist=False)
        assert blocked == set()
        entry = blobs["accounts"]["gmail"]
        assert entry["blob"] == fresh
        assert "auth_dead_at" not in entry

    def test_expired_candidate_is_stale_not_blocked(self, monkeypatch):
        # sync compares against the real clock, so build a really-expired blob.
        really_past = int((time.time() - 3600) * 1000)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _l: _blob(really_past))
        monkeypatch.setattr(accounts, "fetch_profile", lambda _t: None)
        blobs = {"accounts": {"gmail": {"blob": _blob(LIVE_MS), "email": "a@x", "org_uuid": "1"}}}
        blocked = accounts.sync_profile_credentials(blobs, persist=False)
        assert blocked == set()
        # stored blob untouched
        assert accounts.blob_access_expiry(blobs["accounts"]["gmail"]["blob"]) is not None


class TestMismatchIsVisible:
    def test_identity_mismatch_repairs_without_false_login_warning(self, monkeypatch):
        fresh = _blob(LIVE_MS)
        monkeypatch.setattr(accounts, "profile_live_blob", lambda _l: fresh)
        monkeypatch.setattr(accounts, "fetch_profile", lambda _t: {"i": 1})
        monkeypatch.setattr(
            accounts,
            "identity_from_profile",
            lambda _p: {"email": "WRONG@x", "org_uuid": "9", "org_type": "t"},
        )
        notified: list[str] = []
        monkeypatch.setattr(accounts, "_notify_needs_login", notified.append)
        monkeypatch.setattr(accounts, "_write_0600", lambda _p, _b: None)
        monkeypatch.setattr(accounts, "save_blobs", lambda _b: None)
        blobs = {
            "accounts": {
                "gmail": {"blob": _blob(LIVE_MS, refresh="older"), "email": "a@x", "org_uuid": "1"}
            }
        }
        accounts.sync_profile_credentials(blobs, persist=True)
        assert "auth_dead_at" not in blobs["accounts"]["gmail"]
        assert notified == []
