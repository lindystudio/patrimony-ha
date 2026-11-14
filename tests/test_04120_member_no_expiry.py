"""0.4.120: member access has no expiry by default.

A mint without ttlDays stores exp=None (GET /members expiresAt is null). An
explicit ttlDays still expires with member_token_expired. On load, a one-time
migration nulls exp on every live pre-marker row with a numeric exp (the store
has no ttl flag, so every such row is treated as default), including recently
lapsed and non-30-day-shaped rows, and leaves revoked, cancelled and tombstoned
rows alone. After the marker, a new explicit-ttl mint keeps its exp. Token
hashes stay unchanged. A cancelledAt-only row fails auth. Names here are fixtures.
"""

from __future__ import annotations

import json
import time

from custom_components.patrimony_collection import fileio
from custom_components.patrimony_collection import members as hm
from custom_components.patrimony_collection import http as hh
from tests.test_members import (  # noqa: F401  (autouse fixtures re-exported)
    _Admin, _Hass, _Req, _fake_web, _mint, _pq_unavailable_member_lifecycle, run,
)

_DAY = 86400.0
_NOW = 1_700_000_000.0


def _legacy_row(member_id, token, *, created_at, exp, **extra):
    row = {
        "memberId": member_id,
        "tokenHash": hm.token_hash(token),
        "principalId": extra.pop("principalId", "prn_" + member_id),
        "memberName": extra.pop("memberName", "Fixture Member"),
        "inviteId": None,
        "role": hm.ROLE_MEMBER,
        "createdAt": created_at,
        "exp": exp,
        "firstUsedAt": extra.pop("firstUsedAt", created_at + 10),
        "lastUsedAt": extra.pop("lastUsedAt", created_at + 10),
        "revokedAt": None,
        "cancelledAt": None,
    }
    row.update(extra)
    return row


def _write_legacy(hass, rows, **doc_extra):
    path = hm.members_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    members = {r["memberId"]: r for r in rows}
    doc = {"version": 1, "members": members, **doc_extra}
    path.write_text(json.dumps(doc, indent=2) + "\n")
    fileio.reset_cache()
    return path


def _stored(hass):
    fileio.reset_cache()
    return json.loads(hm.members_path(hass).read_text())


def test_default_mint_gives_none(tmp_path):
    hass = _Hass(tmp_path)
    row, tok = hm.mint(hass, "Fixture Ada", principal_id="prn_ada", now=_NOW)
    assert row["expiresAt"] is None
    raw = _stored(hass)
    stored = raw["members"][row["memberId"]]
    assert stored["exp"] is None
    assert stored["tokenHash"] == hm.token_hash(tok)
    listed = run(hh.PatrimonyMembersView(hass).get(_Req(user=_Admin()))).json()
    assert listed["pending"][0]["expiresAt"] is None
    # Still authenticates far past the old 30-day window.
    future = _NOW + 400 * _DAY
    found, err = hm.authenticate(hass, tok, now=future, touch=False)
    assert err is None and found["memberId"] == row["memberId"]
    assert hm.row_status(stored, future) == hm.STATUS_PENDING


def test_explicit_ttl_expires_and_returns_member_token_expired(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    status, doc = _mint(hass, ttlDays=1)
    assert status == 201
    token = doc["token"]
    mid = doc["member"]["memberId"]
    assert doc["member"]["expiresAt"] is not None
    found, err = hm.authenticate(hass, token, touch=False)
    assert err is None and found["memberId"] == mid
    real = hm.time.time
    monkeypatch.setattr(hm.time, "time", lambda: real() + _DAY + 5)
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(token)))
    assert r.status == 401 and r.json()["error"]["code"] == "member_token_expired"
    pr = run(hh.PatrimonyMemberPairingView(hass).get(_Req(token))).json()
    assert pr["status"] == "unpaired" and pr["reason"] == "expired"
    _, err = hm.authenticate(hass, token, now=real() + _DAY + 5, touch=False)
    assert err == "member_token_expired"


def test_migration_nulls_default_rows(tmp_path):
    hass = _Hass(tmp_path)
    token = "pmt_" + "a" * 43
    created = _NOW - 5 * _DAY
    row = _legacy_row("m_default", token, created_at=created, exp=created + 30 * _DAY)
    _write_legacy(hass, [row])
    loaded = hm.load_rows(hass)
    assert loaded["m_default"]["exp"] is None
    assert loaded["m_default"]["tokenHash"] == hm.token_hash(token)
    doc = _stored(hass)
    assert hm.MIGRATION_NO_DEFAULT_EXPIRY in doc["migrations"]
    listed = hm.list_document(hass, now=_NOW)
    assert listed["members"][0]["expiresAt"] is None
    assert listed["members"][0]["status"] == "active"


def test_migration_revives_recently_expired_non_revoked_and_authenticates(tmp_path):
    hass = _Hass(tmp_path)
    token = "pmt_" + "b" * 43
    created = _NOW - 35 * _DAY  # expired 5 days ago on the old 30-day default
    row = _legacy_row("m_lapsed", token, created_at=created, exp=created + 30 * _DAY)
    _write_legacy(hass, [row])
    assert hm.row_status(row, _NOW) == hm.STATUS_EXPIRED
    loaded = hm.load_rows(hass)
    assert loaded["m_lapsed"]["exp"] is None
    assert loaded["m_lapsed"]["tokenHash"] == hm.token_hash(token)
    found, err = hm.authenticate(hass, token, now=_NOW, touch=False)
    assert err is None and found["memberId"] == "m_lapsed"
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(token)))
    assert r.status == 200
    pr = run(hh.PatrimonyMemberPairingView(hass).get(_Req(token))).json()
    assert pr["status"] == "active" and pr.get("reason") is None


def test_migration_leaves_revoked_cancelled_and_tombstoned_rows_alone(tmp_path):
    hass = _Hass(tmp_path)
    tok_r, tok_c, tok_t, tok_cn = ("pmt_" + ch * 43 for ch in "rcnt")
    created = _NOW - 10 * _DAY
    exp = created + 30 * _DAY
    revoked = _legacy_row("m_revoked", tok_r, created_at=created, exp=exp, revokedAt=_NOW - _DAY)
    cancelled = _legacy_row(
        "m_cancelled", tok_c, created_at=created, exp=exp,
        cancelledAt=_NOW - _DAY, revokedAt=_NOW - _DAY, firstUsedAt=None, lastUsedAt=None,
    )
    # cancelledAt alone still must not be revived (contract), even without revokedAt.
    cancelled_only = _legacy_row(
        "m_cancelled_only", tok_cn, created_at=created, exp=exp, cancelledAt=_NOW - _DAY,
    )
    tomb = hm._to_tombstone(
        _legacy_row("m_tomb", tok_t, created_at=created, exp=exp, revokedAt=_NOW - 2 * _DAY),
        _NOW - _DAY,
    )
    _write_legacy(hass, [revoked, cancelled, cancelled_only, tomb])
    loaded = hm.load_rows(hass)
    for mid, tok in (
        ("m_revoked", tok_r),
        ("m_cancelled", tok_c),
        ("m_cancelled_only", tok_cn),
        ("m_tomb", tok_t),
    ):
        assert loaded[mid]["exp"] == exp, mid
        assert loaded[mid]["tokenHash"] == hm.token_hash(tok)
    assert loaded["m_tomb"]["tombstone"] is True
    for tok in (tok_r, tok_c, tok_cn, tok_t):
        _, err = hm.authenticate(hass, tok, now=_NOW, touch=False)
        assert err == "member_token_revoked"


def test_migration_second_run_is_a_noop(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    token = "pmt_" + "d" * 43
    created = _NOW - 2 * _DAY
    _write_legacy(hass, [_legacy_row("m_once", token, created_at=created, exp=created + 30 * _DAY)])
    hm.load_rows(hass)
    first = hm.members_path(hass).read_text()
    writes = {"n": 0}
    real_write = hm._write

    def counted(path, data):
        writes["n"] += 1
        return real_write(path, data)

    monkeypatch.setattr(hm, "_write", counted)
    again = hm.load_rows(hass)
    assert writes["n"] == 0
    assert again["m_once"]["exp"] is None
    assert hm.members_path(hass).read_text() == first
    # Marker already present: a third explicit ensure is still a no-op.
    hm.load_rows(hass)
    assert writes["n"] == 0


def test_migration_token_hashes_unchanged(tmp_path):
    hass = _Hass(tmp_path)
    live = "pmt_" + "e" * 43
    dead = "pmt_" + "f" * 43
    created = _NOW - 40 * _DAY
    exp_default = created + 30 * _DAY
    rows = [
        _legacy_row("m_live", live, created_at=created, exp=exp_default),
        _legacy_row("m_dead", dead, created_at=created, exp=exp_default, revokedAt=created + 31 * _DAY),
    ]
    want = {r["memberId"]: r["tokenHash"] for r in rows}
    _write_legacy(hass, rows)
    loaded = hm.load_rows(hass)
    for mid, digest in want.items():
        assert loaded[mid]["tokenHash"] == digest
    assert loaded["m_live"]["exp"] is None
    assert loaded["m_dead"]["exp"] == exp_default


def test_migration_cannot_tell_default_from_explicit_treats_as_default(tmp_path):
    """No createdAt, and exp is not 30-day shaped: still nulled (live rows only)."""
    hass = _Hass(tmp_path)
    token = "pmt_" + "g" * 43
    row = _legacy_row("m_notell", token, created_at=_NOW - 10 * _DAY, exp=_NOW + 5 * _DAY)
    del row["createdAt"]
    _write_legacy(hass, [row])
    loaded = hm.load_rows(hass)
    assert loaded["m_notell"]["exp"] is None
    _, err = hm.authenticate(hass, token, now=_NOW, touch=False)
    assert err is None


def test_migration_revives_hand_backdated_non_default_shaped_exp(tmp_path):
    """Lapsed row: exp is already past and is not createdAt+30d. Must revive."""
    hass = _Hass(tmp_path)
    token = "pmt_" + "h" * 43
    created = _NOW - 2 * _DAY
    exp = _NOW - 12 * 3600  # hand-backdated, not a 30-day default shape
    row = _legacy_row("m_g3", token, created_at=created, exp=exp)
    _write_legacy(hass, [row])
    assert hm.row_status(row, _NOW) == hm.STATUS_EXPIRED
    loaded = hm.load_rows(hass)
    assert loaded["m_g3"]["exp"] is None
    assert loaded["m_g3"]["tokenHash"] == hm.token_hash(token)
    _, err = hm.authenticate(hass, token, now=_NOW, touch=False)
    assert err is None
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(token)))
    assert r.status == 200
    pr = run(hh.PatrimonyMemberPairingView(hass).get(_Req(token))).json()
    assert pr["status"] == "active" and pr.get("reason") is None


def test_post_migration_explicit_ttl_mint_is_preserved(tmp_path, monkeypatch):
    """The marker keeps an explicit-ttl mint made after the migration."""
    hass = _Hass(tmp_path)
    status, doc = _mint(hass, ttlDays=7)
    assert status == 201
    mid = doc["member"]["memberId"]
    token = doc["token"]
    stored = _stored(hass)
    assert hm.MIGRATION_NO_DEFAULT_EXPIRY in stored["migrations"]
    exp = stored["members"][mid]["exp"]
    assert exp is not None
    hm.load_rows(hass)
    assert _stored(hass)["members"][mid]["exp"] == exp
    _, err = hm.authenticate(hass, token, touch=False)
    assert err is None
    real = hm.time.time
    monkeypatch.setattr(hm.time, "time", lambda: real() + 7 * _DAY + 5)
    _, err = hm.authenticate(hass, token, now=real() + 7 * _DAY + 5, touch=False)
    assert err == "member_token_expired"


def test_cancelled_at_only_fails_auth(tmp_path):
    """cancelledAt without revokedAt is not live: migration leaves exp, auth is revoked."""
    hass = _Hass(tmp_path)
    token = "pmt_" + "k" * 43
    created = _NOW - 3 * _DAY
    exp = created + 30 * _DAY  # still in the future, would have authenticated before
    row = _legacy_row("m_cancel_only", token, created_at=created, exp=exp,
                      cancelledAt=_NOW - 3600, firstUsedAt=None, lastUsedAt=None)
    assert not row.get("revokedAt")
    _write_legacy(hass, [row])
    loaded = hm.load_rows(hass)
    assert loaded["m_cancel_only"]["exp"] == exp
    assert loaded["m_cancel_only"]["cancelledAt"] == _NOW - 3600
    assert not loaded["m_cancel_only"].get("revokedAt")
    _, err = hm.authenticate(hass, token, now=_NOW, touch=False)
    assert err == "member_token_revoked"
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(token)))
    assert r.status == 401 and r.json()["error"]["code"] == "member_token_revoked"
    pr = run(hh.PatrimonyMemberPairingView(hass).get(_Req(token))).json()
    assert pr["status"] == "unpaired" and pr["reason"] == "revoked"
