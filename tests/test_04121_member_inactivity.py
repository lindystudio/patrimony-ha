"""0.4.121: members lapse after 365 days without use (no guest expires if used within 365 days).

Each row stores lastUsedDay (UTC "YYYY-MM-DD"). Mint sets it to the mint day; a
successful member auth (/m/*, including /m/pairing) sets it to today, writing the
store at most once per UTC day per row. (today - lastUsedDay).days > 365 fails with
401 member_token_expired; /m/pairing answers unpaired, reason expired. A one-time
migration (inactivity_365_04121) stamps the migration day on live rows lacking one.
Names and tokens here are fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from custom_components.patrimony_collection import fileio
from custom_components.patrimony_collection import members as hm
from custom_components.patrimony_collection import http as hh
from tests.test_members import (  # noqa: F401  (autouse fixtures re-exported)
    _Admin, _Hass, _Req, _fake_web, _mint, _pq_unavailable_member_lifecycle, _revoke, run,
)

_DAY = 86400.0
# 15 Nov 2026 12:00 UTC.
T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc).timestamp()


class _Clock:
    def __init__(self, monkeypatch, t):
        self.t = t
        monkeypatch.setattr(hm.time, "time", lambda: self.t)


def _stored(hass):
    fileio.reset_cache()
    return json.loads(hm.members_path(hass).read_text())


def _row(hass, mid):
    return _stored(hass)["members"][mid]


def _state(hass, token):
    return run(hh.PatrimonyMemberStateView(hass).get(_Req(token)))


def _pairing(hass, token):
    return run(hh.PatrimonyMemberPairingView(hass).get(_Req(token)))


def _legacy(member_id, token, *, created_at, **extra):
    row = {
        "memberId": member_id, "tokenHash": hm.token_hash(token), "principalId": "prn_" + member_id,
        "memberName": "Fixture Member", "inviteId": None, "role": hm.ROLE_MEMBER,
        "createdAt": created_at, "exp": None, "firstUsedAt": created_at + 10,
        "lastUsedAt": created_at + 10, "revokedAt": None, "cancelledAt": None,
    }
    row.update(extra)
    return row


def _write(hass, rows, migrations=(hm.MIGRATION_NO_DEFAULT_EXPIRY,)):
    path = hm.members_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "migrations": list(migrations),
                                "members": {r["memberId"]: r for r in rows}}, indent=2) + "\n")
    fileio.reset_cache()


def test_mint_sets_last_used_day_and_auth_updates_it(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    status, doc = _mint(hass)
    assert status == 201
    mid = doc["member"]["memberId"]
    assert _row(hass, mid)["lastUsedDay"] == "2026-11-15"
    assert doc["member"]["lastUsedDay"] == "2026-11-15"
    assert hm.MIGRATION_INACTIVITY_365 in _stored(hass)["migrations"]
    clock.t = T0 + 100 * _DAY
    assert _state(hass, doc["token"]).status == 200
    assert _row(hass, mid)["lastUsedDay"] == hm.utc_day(T0 + 100 * _DAY)


def test_boundary_365_passes_366_fails(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    doc = _mint(hass)[1]
    token = doc["token"]
    assert _state(hass, token).status == 200  # lastUsedDay = 2026-11-15
    # 365 days later (any time of that UTC day): still fine, and it renews the day.
    clock.t = T0 + 365 * _DAY + 11 * 3600  # 15 Nov 2027 23:00 UTC
    found, err = hm.authenticate(hass, token, touch=False)
    assert err is None and found is not None
    # 366 days later: lapsed.
    clock.t = T0 + 366 * _DAY - 12 * 3600  # 16 Nov 2027 00:00 UTC
    _, err = hm.authenticate(hass, token, touch=False)
    assert err == "member_token_expired"
    r = _state(hass, token)
    assert r.status == 401 and r.json()["error"]["code"] == "member_token_expired"
    assert r.headers["WWW-Authenticate"] == 'Bearer realm="patrimony-member"'
    # A lapsed auth never renews the day.
    assert _row(hass, doc["member"]["memberId"])["lastUsedDay"] == "2026-11-15"
    assert hm.list_document(hass)["inactive"][0]["status"] == "expired"


def test_use_within_365_renews(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    token = _mint(hass)[1]["token"]
    for _ in range(4):  # used every 300 days: never lapses
        clock.t += 300 * _DAY
        assert _state(hass, token).status == 200


def test_once_per_day_write_throttle(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    doc = _mint(hass)[1]
    token, mid = doc["token"], doc["member"]["memberId"]
    writes = []
    real_write = hm._write
    monkeypatch.setattr(hm, "_write", lambda p, d: (writes.append(1), real_write(p, d)))
    # First use on the mint day: writes once (firstUsedAt).
    assert hm.authenticate(hass, token)[1] is None
    assert len(writes) == 1
    first = _row(hass, mid)
    assert first["firstUsedAt"] == T0 and first["lastUsedDay"] == "2026-11-15"
    # More uses the same UTC day: no store write.
    for dt in (60, 3600, 11 * 3600):
        clock.t = T0 + dt
        assert hm.authenticate(hass, token)[1] is None
        assert _pairing(hass, token).json()["status"] == "active"
    assert len(writes) == 1
    assert _row(hass, mid)["lastUsedAt"] == T0
    # Next UTC day: exactly one more write.
    clock.t = T0 + 13 * 3600  # 16 Nov 01:00 UTC
    assert hm.authenticate(hass, token)[1] is None
    assert hm.authenticate(hass, token)[1] is None
    assert len(writes) == 2
    assert _row(hass, mid)["lastUsedDay"] == "2026-11-16"


def test_pairing_route_counts_as_use(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    doc = _mint(hass)[1]
    clock.t = T0 + 200 * _DAY
    assert _pairing(hass, doc["token"]).json()["status"] == "active"
    assert _row(hass, doc["member"]["memberId"])["lastUsedDay"] == hm.utc_day(clock.t)


def test_pairing_reason_expired_on_inactivity(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    doc = _mint(hass)[1]
    clock.t = T0 + 400 * _DAY
    r = _pairing(hass, doc["token"])
    assert r.status == 200
    body = r.json()
    assert body["status"] == "unpaired" and body["reason"] == "expired"
    assert body["clientId"] == "m_" + doc["member"]["memberId"]


def test_unused_pending_mint_lapses_after_365(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    doc = _mint(hass)[1]
    clock.t = T0 + 365 * _DAY
    assert hm.list_document(hass)["pending"][0]["memberId"] == doc["member"]["memberId"]
    clock.t = T0 + 366 * _DAY
    assert hm.list_document(hass)["inactive"][0]["status"] == "expired"


def test_explicit_ttl_vs_inactivity_whichever_first(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    # Short ttl: exp wins long before inactivity.
    short = _mint(hass, ttlDays=2, principalId="prn_short")[1]["token"]
    clock.t = T0 + 1 * _DAY
    assert hm.authenticate(hass, short)[1] is None
    clock.t = T0 + 2 * _DAY + 5
    assert hm.authenticate(hass, short)[1] == "member_token_expired"
    # Max ttl (365) unused: inactivity (day 366) and exp (day 365) — exp comes first.
    clock.t = T0
    full = _mint(hass, ttlDays=365, principalId="prn_full")[1]["token"]
    clock.t = T0 + 365 * _DAY - 60
    assert hm.authenticate(hass, full, touch=False)[1] is None
    clock.t = T0 + 365 * _DAY + 60
    assert hm.authenticate(hass, full, touch=False)[1] == "member_token_expired"
    # Inactivity first: a row whose exp is far away but last used > 365 days ago.
    tok = "pmt_" + "i" * 43
    row = _legacy("m_inact", tok, created_at=T0, exp=T0 + 900 * _DAY, lastUsedDay="2026-11-15")
    _write(hass, [row], migrations=hm.MIGRATIONS)
    clock.t = T0 + 366 * _DAY
    assert hm.authenticate(hass, tok, touch=False)[1] == "member_token_expired"
    clock.t = T0 + 365 * _DAY
    assert hm.authenticate(hass, tok, touch=False)[1] is None


def test_revoked_still_beats_expired(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    doc = _mint(hass)[1]
    assert _state(hass, doc["token"]).status == 200
    assert _revoke(hass, "prn_anna")[0] == 200
    clock.t = T0 + 500 * _DAY
    _, err = hm.authenticate(hass, doc["token"], touch=False)
    assert err == "member_token_revoked"
    assert _pairing(hass, doc["token"]).json()["reason"] == "revoked"
    # Cancelled-only and tombstoned rows also stay revoked past inactivity.
    t1, t2 = "pmt_" + "c" * 43, "pmt_" + "t" * 43
    rows = [
        _legacy("m_cancel", t1, created_at=T0, cancelledAt=T0 + 1, lastUsedDay="2026-11-15"),
        {"memberId": "m_tomb", "tokenHash": hm.token_hash(t2), "principalId": "p", "exp": None,
         "revokedAt": T0 + 1, "tombstone": True, "tombstonedAt": clock.t - 1, "lastUsedDay": "2026-11-15"},
    ]
    _write(hass, rows, migrations=hm.MIGRATIONS)
    assert hm.authenticate(hass, t1, touch=False)[1] == "member_token_revoked"
    assert hm.authenticate(hass, t2, touch=False)[1] == "member_token_revoked"
    assert hm.authenticate(hass, "pmt_" + "u" * 43, touch=False)[1] == "unauthorized"


def test_row_without_last_used_day_is_not_inactive(tmp_path, monkeypatch):
    _Clock(monkeypatch, T0 + 5000 * _DAY)
    tok = "pmt_" + "n" * 43
    row = _legacy("m_noday", tok, created_at=T0)
    assert not hm.is_inactive(row)
    assert hm.row_status(row) == hm.STATUS_ACTIVE
    assert hm.row_status(dict(row, lastUsedDay="garbage")) == hm.STATUS_ACTIVE


def test_migration_stamps_live_rows_once(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    toks = {k: "pmt_" + k * 43 for k in "abcde"}
    old = T0 - 800 * _DAY  # used long ago: no backfill from history
    rows = [
        _legacy("m_live", toks["a"], created_at=old),
        _legacy("m_pending", toks["b"], created_at=old, firstUsedAt=None, lastUsedAt=None),
        _legacy("m_revoked", toks["c"], created_at=old, revokedAt=old + 5),
        _legacy("m_cancel", toks["d"], created_at=old, cancelledAt=old + 5),
        {"memberId": "m_tomb", "tokenHash": hm.token_hash(toks["e"]), "principalId": "p",
         "exp": None, "revokedAt": T0 - 5, "tombstone": True, "tombstonedAt": T0 - 5},
        _legacy("m_has", "pmt_" + "f" * 43, created_at=old, lastUsedDay="2026-10-01"),
    ]
    _write(hass, rows)  # pre-0.4.121 store: only the 0.4.120 marker
    hm.load_rows(hass)
    doc = _stored(hass)
    m = doc["members"]
    assert m["m_live"]["lastUsedDay"] == "2026-11-15"
    assert m["m_pending"]["lastUsedDay"] == "2026-11-15"
    assert m["m_has"]["lastUsedDay"] == "2026-10-01"
    for k in ("m_revoked", "m_cancel", "m_tomb"):
        assert "lastUsedDay" not in m[k]
    assert doc["migrations"] == [hm.MIGRATION_NO_DEFAULT_EXPIRY, hm.MIGRATION_INACTIVITY_365]
    assert m["m_live"]["tokenHash"] == hm.token_hash(toks["a"])
    assert hm.authenticate(hass, toks["a"], touch=False)[1] is None
    # Second run (later day, a row losing its day by hand): no-op, no write.
    snapshot = hm.members_path(hass).read_text()
    clock.t = T0 + 30 * _DAY
    writes = []
    monkeypatch.setattr(hm, "_write", lambda p, d: writes.append(1))
    hm.load_rows(hass)
    hm.load_rows(hass)
    assert writes == []
    assert hm.members_path(hass).read_text() == snapshot


def test_migration_runs_both_on_a_pre_04120_store(tmp_path, monkeypatch):
    _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    tok = "pmt_" + "z" * 43
    _write(hass, [_legacy("m_old", tok, created_at=T0 - 40 * _DAY, exp=T0 - 10 * _DAY)], migrations=())
    r = hm.load_rows(hass)["m_old"]
    assert r["exp"] is None and r["lastUsedDay"] == "2026-11-15"
    assert _stored(hass)["migrations"] == list(hm.MIGRATIONS)


def test_missing_store_not_created_by_migration(tmp_path, monkeypatch):
    _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    assert hm.load_rows(hass) == {}
    assert not hm.members_path(hass).exists()


def test_prune_inactivity_lapsed_row_after_keep_window(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    doc = _mint(hass)[1]
    mid = doc["member"]["memberId"]
    lapse = hm.inactivity_lapse_ts(_row(hass, mid))
    assert hm.utc_day(lapse) == "2027-11-16"
    # Lapsed but inside the 30-day keep window: kept (shows as expired, token denied).
    clock.t = lapse + 29 * _DAY
    _mint(hass, principalId="prn_other")
    assert mid in _stored(hass)["members"]
    assert hm.authenticate(hass, doc["token"], touch=False)[1] == "member_token_expired"
    # Past the keep window: pruned like any ended row.
    clock.t = lapse + 31 * _DAY
    _mint(hass, principalId="prn_third")
    assert mid not in _stored(hass)["members"]


def test_owner_route_unaffected(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch, T0)
    hass = _Hass(tmp_path)
    doc = _mint(hass)[1]
    clock.t = T0 + 400 * _DAY
    assert _state(hass, doc["token"]).status == 401
    # Owner/admin GET /members (HA auth) still works and lists the lapsed member as inactive.
    r = run(hh.PatrimonyMembersView(hass).get(_Req(user=_Admin())))
    assert r.status == 200
    listed = r.json()
    assert [p["memberId"] for p in listed["inactive"]] == [doc["member"]["memberId"]]
    assert listed["inactive"][0]["lastUsedDay"] == "2026-11-15"
    assert listed["inactive"][0]["expiresAt"] is None
    # Owner can still mint a fresh invite (re-invite restores access).
    status, fresh = _mint(hass, principalId="prn_anna")
    assert status == 201
    assert _state(hass, fresh["token"]).status == 200
