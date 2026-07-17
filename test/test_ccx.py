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


class TestClaudeJsonSwap:
    def test_swaps_oauth_preserves_rest(self, tmp_path, monkeypatch):
        cj = tmp_path / ".claude.json"
        cj.write_text(json.dumps({"numStartups": 7, "oauthAccount": {"accountUuid": "old"}}))
        cj.chmod(0o600)
        monkeypatch.setattr(ccx, "CLAUDE_JSON", cj)
        monkeypatch.setattr(ccx, "BACKUP_DIR", tmp_path / "backups")

        ccx.write_claude_json_oauth({"accountUuid": "new", "emailAddress": "a@b.c"})

        data = json.loads(cj.read_text())
        assert data["numStartups"] == 7
        assert data["oauthAccount"]["accountUuid"] == "new"
        assert (cj.stat().st_mode & 0o777) == 0o600
        assert list((tmp_path / "backups").glob(".claude.json.backup.ccx.*"))

    def test_corrupt_file_hard_fails(self, tmp_path, monkeypatch):
        cj = tmp_path / ".claude.json"
        cj.write_text("{corrupt")
        monkeypatch.setattr(ccx, "CLAUDE_JSON", cj)
        monkeypatch.setattr(ccx, "BACKUP_DIR", tmp_path / "backups")
        with pytest.raises(json.JSONDecodeError):
            ccx.write_claude_json_oauth({"accountUuid": "new"})
        assert cj.read_text() == "{corrupt"


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
