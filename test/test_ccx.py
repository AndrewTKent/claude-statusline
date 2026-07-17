"""Unit tests for bin/ccx.py — pure logic only (no keychain, no network)."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import ccx  # noqa: E402

NOW = datetime(2026, 7, 16, 20, 0, 0, tzinfo=timezone.utc)

PAIRS = [
    ("acme-max", "andrew.kent@acme.ai", "e1c8"),
    ("acme-work", "andrew.kent@acme.ai", "52ae"),
    ("work", "*@acme.ai", None),
    ("gmail", "user@example.com", None),
]


class TestResolveLabel:
    def test_uuid_qualified_beats_bare(self):
        assert ccx.resolve_label("andrew.kent@acme.ai", "52ae", PAIRS) == "acme-work"

    def test_uuid_qualified_exact(self):
        assert ccx.resolve_label("andrew.kent@acme.ai", "e1c8", PAIRS) == "acme-max"

    def test_bare_fallback_when_uuid_unknown(self):
        assert ccx.resolve_label("andrew.kent@acme.ai", "zzzz", PAIRS) == "work"

    def test_bare_glob(self):
        assert ccx.resolve_label("other@acme.ai", None, PAIRS) == "work"

    def test_no_match_falls_back_to_localpart(self):
        assert ccx.resolve_label("x@nowhere.io", None, PAIRS) == "x"

    def test_no_email(self):
        assert ccx.resolve_label(None, None, PAIRS) == "?"


class TestEffectivePcts:
    def test_past_reset_zeroes(self):
        row = {
            "five_hour_pct": 97.0,
            "five_hour_reset": (NOW - timedelta(minutes=1)).isoformat(),
        }
        assert ccx.effective_pcts(row, NOW)["five_hour"] == 0.0

    def test_future_reset_keeps_pct(self):
        row = {
            "five_hour_pct": 55.0,
            "five_hour_reset": (NOW + timedelta(hours=2)).isoformat(),
        }
        assert ccx.effective_pcts(row, NOW)["five_hour"] == 55.0

    def test_missing_pct_is_none(self):
        assert ccx.effective_pcts({}, NOW)["five_hour"] is None

    def test_missing_reset_keeps_pct(self):
        row = {"seven_day_pct": 31.0}
        assert ccx.effective_pcts(row, NOW)["seven_day"] == 31.0


class TestHeadroomRank:
    def test_orders_by_five_hour_then_seven_day(self):
        a = {"five_hour": 10.0, "seven_day": 90.0, "fable": None}
        b = {"five_hour": 10.0, "seven_day": 5.0, "fable": None}
        c = {"five_hour": 0.0, "seven_day": 99.0, "fable": None}
        ranked = sorted([a, b, c], key=ccx.headroom_rank)
        assert ranked == [c, b, a]

    def test_unknown_ranks_last(self):
        known = {"five_hour": 99.0, "seven_day": 99.0, "fable": 99.0}
        unknown = {"five_hour": None, "seven_day": None, "fable": None}
        assert sorted([unknown, known], key=ccx.headroom_rank) == [known, unknown]


class TestBlobAccessToken:
    def test_nested(self):
        blob = json.dumps({"claudeAiOauth": {"accessToken": "tok-1"}})
        assert ccx.blob_access_token(blob) == "tok-1"

    def test_flat(self):
        assert ccx.blob_access_token(json.dumps({"accessToken": "tok-2"})) == "tok-2"

    def test_invalid(self):
        assert ccx.blob_access_token("not json") is None
        assert ccx.blob_access_token(json.dumps(["nope"])) is None


class TestSynthesizeOauthAccount:
    def test_identifiers_land(self):
        ident = {"uuid": "u1", "email": "e@x.y", "org_uuid": "o1", "org_type": "claude_max",
                 "rate_limit_tier": "default_claude_max_20x"}
        oa = ccx.synthesize_oauth_account(ident, None)
        assert oa["accountUuid"] == "u1"
        assert oa["emailAddress"] == "e@x.y"
        assert oa["organizationUuid"] == "o1"
        assert oa["organizationRateLimitTier"] == "default_claude_max_20x"


class TestCredExpiry:
    def test_blob_refresh_expiry_ms_to_seconds(self):
        blob = json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": 1784337371728}})
        assert ccx.blob_refresh_expiry(blob) == 1784337371  # ms floored to s

    def test_blob_refresh_expiry_missing(self):
        assert ccx.blob_refresh_expiry(json.dumps({"claudeAiOauth": {}})) is None
        assert ccx.blob_refresh_expiry("not json") is None

    def test_blob_refresh_expiry_flat(self):
        assert ccx.blob_refresh_expiry(json.dumps({"refreshTokenExpiresAt": 2000000000000})) == 2000000000

    def test_cred_expired_past(self):
        assert ccx.cred_expired(1) is True  # epoch 1 is long past

    def test_cred_expired_future(self):
        assert ccx.cred_expired(4000000000) is False  # year 2096

    def test_cred_expired_none_is_not_expired(self):
        assert ccx.cred_expired(None) is False


def _route_row(label, eff5, active=False, expired=False):
    return {
        "uuid": label,
        "label": label,
        "active": active,
        "expired": expired,
        "effs": {"five_hour": eff5, "seven_day": 0.0, "fable": 0.0},
    }


class TestPickRoute:
    NOW = 1_784_000_000.0
    VAULT = {"tokens": {
        "gmail": {"token": "sk-ant-gmail-tok", "expires_at": NOW + 1000},
        "ymail": {"token": "sk-ant-ymail-tok", "expires_at": NOW - 1},  # expired token
    }}

    def test_best_with_token_wins(self):
        rows = [_route_row("alumni", 5.0), _route_row("gmail", 20.0), _route_row("ymail", 1.0)]
        # alumni has no token, ymail's token is expired -> gmail
        assert ccx.pick_route(rows, self.VAULT, set(), self.NOW, None) == ("gmail", "sk-ant-gmail-tok")

    def test_pin_overrides_headroom(self):
        rows = [_route_row("alumni", 5.0), _route_row("gmail", 90.0)]
        assert ccx.pick_route(rows, self.VAULT, set(), self.NOW, "gmail")[0] == "gmail"

    def test_excluded_skipped(self):
        rows = [_route_row("gmail", 5.0)]
        assert ccx.pick_route(rows, self.VAULT, {"gmail"}, self.NOW, None) is None

    def test_expired_cred_row_skipped(self):
        rows = [_route_row("gmail", 5.0, expired=True)]
        assert ccx.pick_route(rows, self.VAULT, set(), self.NOW, None) is None

    def test_no_tokens_none(self):
        rows = [_route_row("alumni", 5.0)]
        assert ccx.pick_route(rows, self.VAULT, set(), self.NOW, None) is None


class TestMergeTokenVaults:
    def test_union(self):
        a = {"tokens": {"gmail": {"token": "g", "minted_at": 1}}}
        b = {"tokens": {"ymail": {"token": "y", "minted_at": 2}}}
        assert set(ccx.merge_token_vaults(a, b)["tokens"]) == {"gmail", "ymail"}

    def test_newer_mint_wins(self):
        a = {"tokens": {"gmail": {"token": "old", "minted_at": 1}}}
        b = {"tokens": {"gmail": {"token": "new", "minted_at": 9}}}
        assert ccx.merge_token_vaults(a, b)["tokens"]["gmail"]["token"] == "new"
        assert ccx.merge_token_vaults(b, a)["tokens"]["gmail"]["token"] == "new"

    def test_empty_sides(self):
        assert ccx.merge_token_vaults({}, {})["tokens"] == {}


class TestTokenVault:
    def test_round_trip_and_perms(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "TOKEN_VAULT_PATH", tmp_path / "sub" / "vault.json")
        vault = {"version": 1, "tokens": {"x": {"token": "sk-ant-t", "expires_at": 2}}}
        ccx.save_token_vault(vault)
        assert ccx.load_token_vault() == vault
        assert (ccx.TOKEN_VAULT_PATH.stat().st_mode & 0o777) == 0o600
        assert (ccx.TOKEN_VAULT_PATH.parent.stat().st_mode & 0o777) == 0o700

    def test_missing_file_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ccx, "TOKEN_VAULT_PATH", tmp_path / "none.json")
        assert ccx.load_token_vault() == {"version": 1, "tokens": {}}


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
        row = ccx.usage_to_reset_row("e@x.io", "org1", self.USAGE, 1784300000)
        assert row["email"] == "e@x.io" and row["org_uuid"] == "org1"
        assert row["five_hour_pct"] == 42
        assert row["five_hour_reset"] == "2026-07-17T22:00:00Z"
        assert row["seven_day_pct"] == 18
        assert row["fable_pct"] == 7
        assert row["fable_label"] == "Fable"
        assert row["last_seen"] == 1784300000

    def test_missing_weekly_is_none(self):
        row = ccx.usage_to_reset_row("e@x.io", "org1", {"five_hour": {}, "seven_day": {}}, 1)
        assert row["fable_pct"] is None and row["fable_label"] is None
        assert row["five_hour_pct"] == 0  # None utilization -> 0

    def test_access_expiry_ms_to_s(self):
        blob = json.dumps({"claudeAiOauth": {"expiresAt": 1784337371728}})
        assert ccx.blob_access_expiry(blob) == 1784337371

    def test_access_expiry_missing(self):
        assert ccx.blob_access_expiry(json.dumps({"claudeAiOauth": {}})) is None


class TestVaultKey:
    def test_composite_of_account_and_org(self):
        ident = {"uuid": "acct1", "org_uuid": "orgA"}
        assert ccx.vault_key(ident) == "acct1|orgA"

    def test_shared_account_two_orgs_distinct_keys(self):
        # acme-max and acme-work: same account, different org -> must not collide
        maxk = ccx.vault_key({"uuid": "d9eb", "org_uuid": "e1c8"})
        workk = ccx.vault_key({"uuid": "d9eb", "org_uuid": "52ae"})
        assert maxk != workk

    def test_missing_org_is_none(self):
        assert ccx.vault_key({"uuid": "acct1", "org_uuid": None}) is None
        assert ccx.vault_key({"uuid": None, "org_uuid": "orgA"}) is None

    def test_short_key_shows_both_parts(self):
        assert ccx.short_key("d9eb92c0-xxxx|52ae57ff-yyyy") == "d9eb92c0|52ae57ff"


class TestResolveTargetSharedEmail:
    META = {
        "accounts": {
            "d9eb|e1c8": {"email": "andrew.kent@acme.ai", "org_uuid": "e1c8"},
            "d9eb|52ae": {"email": "andrew.kent@acme.ai", "org_uuid": "52ae"},
        }
    }
    PAIRS = [
        ("acme-max", "andrew.kent@acme.ai", "e1c8"),
        ("acme-work", "andrew.kent@acme.ai", "52ae"),
    ]

    def test_label_disambiguates_shared_email(self):
        k, _ = ccx.resolve_target(self.META, "acme-work", self.PAIRS)
        assert k == "d9eb|52ae"

    def test_shared_email_is_ambiguous(self):
        with pytest.raises(SystemExit):
            ccx.resolve_target(self.META, "andrew.kent@acme.ai", self.PAIRS)


class TestResolveTarget:
    META = {
        "accounts": {
            "uuid-a": {"email": "andrew.kent@acme.ai", "org_uuid": "e1c8"},
            "uuid-b": {"email": "user@example.com", "org_uuid": "g1"},
        }
    }

    def test_by_label(self):
        uuid, _ = ccx.resolve_target(self.META, "acme-max", PAIRS)
        assert uuid == "uuid-a"

    def test_by_email(self):
        uuid, _ = ccx.resolve_target(self.META, "user@example.com", PAIRS)
        assert uuid == "uuid-b"

    def test_by_uuid(self):
        uuid, _ = ccx.resolve_target(self.META, "uuid-b", PAIRS)
        assert uuid == "uuid-b"

    def test_missing_dies(self):
        with pytest.raises(SystemExit):
            ccx.resolve_target(self.META, "nope", PAIRS)
