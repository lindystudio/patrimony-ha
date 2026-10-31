"""0.4.112 member revoke audit: revokedBy / revokeSource / revokedVia on rows and tombstones.

Source rule (members.py docstring, clients.revoke_actor):
  owner_app        bearer JWT iss is an active owner paired client -> revokedBy = clientId
  panel            HA admin, not a paired owner client -> revokedBy = "admin:<HA user id>"
  principal_revoke revoke_principal with no route caller -> revokedBy = null
  other            anything else -> revokedBy = null
revokedVia is the route name (client_unpair | member_revoke) or "internal".
"""

from __future__ import annotations

import json
import logging

import pytest

from custom_components.patrimony_collection import clients as hc
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import members as hm

# Re-use the member-lifecycle harness (fake aiohttp, _Hass, _Req, ML-KEM unavailable).
from tests.test_members import (  # noqa: F401
    _Hass,
    _NonAdmin,
    _Req,
    _fake_web,
    _mint,
    _owner_jwt,
    _pq_unavailable_member_lifecycle,
    run,
)

ADMIN_ID = "6a7b8ce1f2c3d4b5a6978877665544aa"
OWNER_CID = "7c0ffee1234567890abcdef0123456789"


class _AdminUser:
    is_admin = True
    name = "Ownerton Secret Name"
    id = ADMIN_ID


def _stored(hass, member_id):
    return hm.load_rows(hass)[member_id]


def _client_of(hass, token):
    return run(hh.PatrimonyMemberPairingView(hass).get(_Req(token))).json()["clientId"]


def _unpair(hass, client_id, req):
    return run(hh.PatrimonyClientUnpairView(hass).post(req, client_id))


def _principal_revoke(hass, principal, req):
    return run(hh.PatrimonyMemberRevokeView(hass).post(req, principal))


AUDIT_0412 = ("revokedBy", "revokeSource", "revokedVia")  # 0.4.113 appends cred fields (own tests)


def _audit(row):
    return {k: row.get(k) for k in AUDIT_0412}


# ---------------------------------------------------------------- each path x source mapping


def test_panel_unpair_stores_admin_actor(tmp_path):
    hass = _Hass(tmp_path)
    doc = _mint(hass, user=_AdminUser())[1]
    cid = _client_of(hass, doc["token"])
    r = _unpair(hass, cid, _Req(user=_AdminUser()))
    assert r.status == 200 and r.json()["status"] == "unpaired"
    row = _stored(hass, doc["member"]["memberId"])
    assert row["revokedAt"]
    assert _audit(row) == {"revokedBy": "admin:" + ADMIN_ID, "revokeSource": "panel",
                           "revokedVia": "client_unpair"}


def test_panel_principal_revoke_stores_admin_actor(tmp_path):
    hass = _Hass(tmp_path)
    a = _mint(hass)[1]
    p = _mint(hass, memberName="Anna new")[1]  # pending stays pending -> cancelled
    run(hh.PatrimonyMemberStateView(hass).get(_Req(a["token"])))
    r = _principal_revoke(hass, "prn_anna", _Req(user=_AdminUser()))
    assert r.json()["revoked"] == 1 and r.json()["cancelledPending"] == 1
    for d in (a, p):
        assert _audit(_stored(hass, d["member"]["memberId"])) == {
            "revokedBy": "admin:" + ADMIN_ID, "revokeSource": "panel", "revokedVia": "member_revoke"}


def test_owner_app_unpair_and_principal_revoke(tmp_path):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, OWNER_CID, device_name="Owner iPhone")
    owner = _owner_jwt(OWNER_CID)
    d1 = _mint(hass, owner, user=_NonAdmin(), memberName="Kid", principalId="prn_kid")[1]
    cid = _client_of(hass, d1["token"])
    assert _unpair(hass, cid, _Req(owner, user=_NonAdmin())).status == 200
    assert _audit(_stored(hass, d1["member"]["memberId"])) == {
        "revokedBy": OWNER_CID, "revokeSource": "owner_app", "revokedVia": "client_unpair"}
    d2 = _mint(hass, owner, user=_NonAdmin(), memberName="Kid2", principalId="prn_kid")[1]
    assert _principal_revoke(hass, "prn_kid", _Req(owner, user=_NonAdmin())).status == 200
    assert _audit(_stored(hass, d2["member"]["memberId"])) == {
        "revokedBy": OWNER_CID, "revokeSource": "owner_app", "revokedVia": "member_revoke"}


def test_owner_app_wins_over_admin_user(tmp_path):
    """The owner pairing token belongs to an HA admin user; the paired client decides."""
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, OWNER_CID, device_name="Owner iPhone")
    owner = _owner_jwt(OWNER_CID)
    d = _mint(hass)[1]
    _principal_revoke(hass, "prn_anna", _Req(owner, user=_AdminUser()))
    assert _audit(_stored(hass, d["member"]["memberId"]))["revokeSource"] == "owner_app"


def test_unpaired_owner_client_is_not_owner_app(tmp_path):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, OWNER_CID, device_name="Owner iPhone")
    hc.mark_unpaired(hass, OWNER_CID)
    actor = hc.revoke_actor(hass, _Req(_owner_jwt(OWNER_CID), user=_AdminUser()))
    assert actor == {"revokedBy": "admin:" + ADMIN_ID, "revokeSource": "panel"}


def test_other_and_internal_sources(tmp_path):
    hass = _Hass(tmp_path)
    # No HA user on the request: 0.4.113 fails closed (D1), nothing is revoked.
    d = _mint(hass)[1]
    _principal_revoke(hass, "prn_anna", _Req())
    assert _audit(_stored(hass, d["member"]["memberId"])) == {
        "revokedBy": None, "revokeSource": None, "revokedVia": None}
    # Internal calls without a route caller.
    row, _t = hm.mint(hass, "Bo", principal_id="prn_bo")
    hm.revoke_member(hass, row["memberId"])
    assert _audit(_stored(hass, row["memberId"])) == {
        "revokedBy": None, "revokeSource": "other", "revokedVia": "internal"}
    row2, _t = hm.mint(hass, "Cy", principal_id="prn_cy")
    hm.revoke_principal(hass, "prn_cy")
    assert _audit(_stored(hass, row2["memberId"])) == {
        "revokedBy": None, "revokeSource": "principal_revoke", "revokedVia": "internal"}
    # Admin user without a usable id: panel, null actor.
    class _NoId:
        is_admin = True
    assert hc.revoke_actor(hass, _Req(user=_NoId())) == {"revokedBy": None, "revokeSource": "panel"}
    # Member-marked request: other.
    req = _Req(user=_AdminUser())
    req[hc.MEMBER_CLIENT_KEY] = "m_x"
    assert hc.revoke_actor(hass, req) == {"revokedBy": None, "revokeSource": "other"}


def test_bad_audit_values_degrade(tmp_path):
    hass = _Hass(tmp_path)
    row, _t = hm.mint(hass, "Bo", principal_id="prn_bo")
    hm.revoke_member(hass, row["memberId"], audit={
        "revokedBy": "has space; 10.0.0.1", "revokeSource": "hacker", "revokedVia": "../x"})
    assert _audit(_stored(hass, row["memberId"])) == {
        "revokedBy": None, "revokeSource": "other", "revokedVia": "internal"}


def test_first_audit_is_kept_on_repeat_revoke(tmp_path):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, OWNER_CID, device_name="Owner iPhone")
    d = _mint(hass)[1]
    _principal_revoke(hass, "prn_anna", _Req(_owner_jwt(OWNER_CID), user=_NonAdmin()))
    first = _audit(_stored(hass, d["member"]["memberId"]))
    at = _stored(hass, d["member"]["memberId"])["revokedAt"]
    hm.revoke_member(hass, d["member"]["memberId"], audit={"revokedBy": "admin:zz",
                                                           "revokeSource": "panel", "revokedVia": "client_unpair"})
    _principal_revoke(hass, "prn_anna", _Req(user=_AdminUser()))
    assert _audit(_stored(hass, d["member"]["memberId"])) == first
    assert _stored(hass, d["member"]["memberId"])["revokedAt"] == at


def test_owner_non_member_unpair_writes_no_member_audit(tmp_path):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, OWNER_CID, device_name="Owner iPhone")
    d = _mint(hass)[1]
    r = _unpair(hass, OWNER_CID, _Req(user=_AdminUser()))
    assert r.status == 200
    row = _stored(hass, d["member"]["memberId"])
    assert not row.get("revokedAt") and _audit(row) == {k: None for k in AUDIT_0412}


# ---------------------------------------------------------------- API (additive) + tombstones


def test_list_carries_audit_fields_additively(tmp_path):
    hass = _Hass(tmp_path)
    d = _mint(hass)[1]
    active = _mint(hass, memberName="Bo", principalId="prn_bo")[1]
    _principal_revoke(hass, "prn_anna", _Req(user=_AdminUser()))
    listed = run(hh.PatrimonyMembersView(hass).get(_Req(user=_AdminUser()))).json()
    inactive = [p for p in listed["inactive"] if p["memberId"] == d["member"]["memberId"]][0]
    assert inactive["revokedBy"] == "admin:" + ADMIN_ID
    assert inactive["revokeSource"] == "panel" and inactive["revokedVia"] == "member_revoke"
    pending = [p for p in listed["pending"] if p["memberId"] == active["member"]["memberId"]][0]
    assert pending["revokedBy"] is None and pending["revokeSource"] is None
    # Pre-0.4.112 keys are all still there.
    for key in ("memberId", "clientId", "principalId", "memberName", "inviteId", "role", "status",
                "createdAt", "expiresAt", "lastUsedAt", "revokedAt", "cancelled"):
        assert key in inactive


@pytest.mark.parametrize("how", ["remove", "clear"])
def test_tombstone_keeps_revoke_audit(tmp_path, how):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, OWNER_CID, device_name="Owner iPhone")
    d = _mint(hass)[1]
    run(hh.PatrimonyMemberStateView(hass).get(_Req(d["token"])))
    _unpair(hass, _client_of(hass, d["token"]), _Req(_owner_jwt(OWNER_CID), user=_NonAdmin()))
    mid = d["member"]["memberId"]
    before = _stored(hass, mid)
    if how == "remove":
        assert hm.remove_inactive(hass, mid)["removed"] is True
    else:
        assert hm.clear_inactive(hass)["cleared"] == 1
    tomb = _stored(hass, mid)
    assert tomb["tombstone"] is True
    assert tomb["revokedAt"] == before["revokedAt"]
    assert _audit(tomb) == {"revokedBy": OWNER_CID, "revokeSource": "owner_app",
                            "revokedVia": "client_unpair"}
    assert "memberName" not in tomb
    # Still denied, still hidden.
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(d["token"])))
    assert r.json()["error"]["code"] == "member_token_revoked"
    assert all(p["memberId"] != mid for b in hm.list_document(hass).values() for p in b)


# ---------------------------------------------------------------- INFO log: prefixes only


def test_info_log_one_line_per_revoke_prefixes_only(tmp_path, caplog):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, OWNER_CID, device_name="Owner iPhone")
    a = _mint(hass, memberName="Anna Secret")[1]
    b = _mint(hass, memberName="Anna Other")[1]
    req = _Req(user=_AdminUser())
    req.headers["User-Agent"] = "Mozilla/5.0 UA-MARKER"
    req.headers["X-Forwarded-For"] = "203.0.113.77"
    req.remote = "198.51.100.9"
    with caplog.at_level(logging.INFO, logger=hm.__name__):
        _principal_revoke(hass, "prn_anna", req)
    lines = [r.getMessage().split(" cred=")[0] for r in caplog.records if "patrimony_member_revoked" in r.getMessage()]
    assert len(lines) == 2
    for d in (a, b):
        mid = d["member"]["memberId"]
        want = f"patrimony_member_revoked member={mid[:8]} by=admin:{ADMIN_ID[:6]} source=panel via=member_revoke reason=-"
        assert want in lines
        assert all(mid not in ln for ln in lines)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (ADMIN_ID, "Anna", "Secret", "Ownerton", "prn_anna", "UA-MARKER", "Mozilla",
                   "203.0.113.77", "198.51.100.9", "pmt_", "hek_", a["token"], b["token"]):
        assert secret not in blob, secret
    assert all(r.levelno == logging.INFO for r in caplog.records if "patrimony_member_revoked" in r.getMessage())

    # Owner app line: owner client id prefix only; repeat revoke logs nothing.
    c = _mint(hass, memberName="Kid", principalId="prn_kid")[1]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=hm.__name__):
        _unpair(hass, _client_of(hass, c["token"]), _Req(_owner_jwt(OWNER_CID), user=_NonAdmin()))
        _unpair(hass, "m_" + c["member"]["memberId"],
                _Req(_owner_jwt(OWNER_CID), user=_NonAdmin()))
    lines = [r.getMessage().split(" cred=")[0] for r in caplog.records if "patrimony_member_revoked" in r.getMessage()]
    assert lines == [f"patrimony_member_revoked member={c['member']['memberId'][:8]} "
                     f"by={OWNER_CID[:6]} source=owner_app via=client_unpair reason=-"]
    assert OWNER_CID not in "\n".join(lines)


def test_no_ip_or_ua_stored(tmp_path):
    hass = _Hass(tmp_path)
    d = _mint(hass)[1]
    req = _Req(user=_AdminUser())
    req.headers["User-Agent"] = "Mozilla/5.0 UA-MARKER"
    req.headers["X-Forwarded-For"] = "203.0.113.77"
    _principal_revoke(hass, "prn_anna", req)
    hm.clear_inactive(hass)
    raw = hm.members_path(hass).read_text()
    for bad in ("UA-MARKER", "Mozilla", "203.0.113.77", "Ownerton Secret Name"):
        assert bad not in raw
    assert d["member"]["memberId"] in raw


# ---------------------------------------------------------------- old rows


def test_pre_04112_rows_load_list_and_tombstone(tmp_path):
    hass = _Hass(tmp_path)
    now = 2_000_000_000.0
    old = {
        "version": 1,
        "members": {
            "aaaa1111bbbb2222": {"memberId": "aaaa1111bbbb2222", "tokenHash": "h1", "principalId": "p1",
                                 "memberName": "Old", "inviteId": None, "role": "member",
                                 "createdAt": now - 100, "exp": now + 86400, "firstUsedAt": now - 50,
                                 "lastUsedAt": now - 50, "revokedAt": now - 10, "cancelledAt": None},
            "cccc3333dddd4444": {"memberId": "cccc3333dddd4444", "tokenHash": "h2", "principalId": "p2",
                                 "exp": now + 86400, "revokedAt": now - 20, "firstUsedAt": now - 60,
                                 "tombstone": True, "tombstonedAt": now - 5},
            "eeee5555ffff6666": {"memberId": "eeee5555ffff6666", "tokenHash": "h3", "principalId": "p3",
                                 "memberName": "Live", "role": "member", "createdAt": now - 100,
                                 "exp": now + 86400, "firstUsedAt": now - 50, "revokedAt": None},
        },
    }
    path = hm.members_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old))
    doc = hm.list_document(hass, now=now)
    assert [p["memberId"] for p in doc["inactive"]] == ["aaaa1111bbbb2222"]
    assert doc["inactive"][0]["revokedBy"] is None and doc["inactive"][0]["revokeSource"] is None
    assert [p["memberId"] for p in doc["members"]] == ["eeee5555ffff6666"]
    assert hm.clear_inactive(hass, now=now) == {"cleared": 1}
    tomb = hm.load_rows(hass)["aaaa1111bbbb2222"]
    assert tomb["revokedAt"] == now - 10 and _audit(tomb) == {k: None for k in AUDIT_0412}
    # Old live row can still be revoked and gains the audit.
    hm.revoke_principal(hass, "p3", now=now, audit={"revokedBy": "admin:u1", "revokeSource": "panel",
                                                     "revokedVia": "member_revoke"})
    assert _audit(hm.load_rows(hass)["eeee5555ffff6666"])["revokedBy"] == "admin:u1"
    assert hm.load_rows(hass)["cccc3333dddd4444"]["tombstone"] is True


# ---------------------------------------------------------------- mint clientId (additive)


def test_mint_response_carries_client_id(tmp_path):
    hass = _Hass(tmp_path)
    status, doc = _mint(hass)
    assert status == 201
    mid = doc["member"]["memberId"]
    assert doc["clientId"] == "m_" + mid == doc["member"]["clientId"]
    for key in ("ok", "member", "token", "url", "public_url"):
        assert key in doc


# ---------------------------------------------------------------- X-Patrimony-Revoke-Reason

VALID_REASONS = ["user_remove", "courier_rotation", "courier_put_failed",
                 "pending_revocation:sig_01-AB", "pending_revocation:" + "a" * 64]
INVALID_REASONS = ["", "USER_REMOVE", "user_remove ", "nope", "pending_revocation:",
                   "pending_revocation:" + "a" * 65, "pending_revocation:bad id", "pending_revocation:x/../y",
                   "203.0.113.77", "Mozilla/5.0", "pmt_secretvalue", "x" * 5000, "user_remove\nINJECT"]


def _with_reason(req, reason):
    if reason is not None:
        req.headers[hm.REVOKE_REASON_HEADER] = reason
    return req


@pytest.mark.parametrize("route", ["member_revoke", "client_unpair"])
@pytest.mark.parametrize("reason", VALID_REASONS + INVALID_REASONS + [None])
def test_revoke_reason_header(tmp_path, caplog, route, reason):
    hass = _Hass(tmp_path)
    d = _mint(hass)[1]
    mid = d["member"]["memberId"]
    req = _with_reason(_Req(user=_AdminUser()), reason)
    with caplog.at_level(logging.INFO, logger=hm.__name__):
        if route == "member_revoke":
            r = _principal_revoke(hass, "prn_anna", req)
        else:
            r = _unpair(hass, "m_" + mid, req)
    assert r.status == 200
    valid = reason in VALID_REASONS
    row = _stored(hass, mid)
    assert row["revokedVia"] == (reason if valid else route)
    assert row["revokeSource"] == "panel"
    lines = [x.getMessage().split(" cred=")[0] for x in caplog.records if "patrimony_member_revoked" in x.getMessage()]
    assert lines == [f"patrimony_member_revoked member={mid[:8]} by=admin:{ADMIN_ID[:6]} source=panel "
                     f"via={reason if valid else route} reason={reason if valid else '-'}"]
    if reason and not valid:
        blob = "\n".join(x.getMessage() for x in caplog.records) + hm.members_path(hass).read_text()
        bad = reason.strip()
        if bad and bad not in (route, "-") and len(bad) > 3:
            assert bad not in blob


def test_revoke_reason_survives_tombstone(tmp_path):
    hass = _Hass(tmp_path)
    d = _mint(hass)[1]
    _principal_revoke(hass, "prn_anna", _with_reason(_Req(user=_AdminUser()), "pending_revocation:abc123"))
    hm.clear_inactive(hass)
    assert _stored(hass, d["member"]["memberId"])["revokedVia"] == "pending_revocation:abc123"


def test_clean_revoke_reason_unit():
    for ok in VALID_REASONS:
        assert hm.clean_revoke_reason(ok) == ok
    for bad in INVALID_REASONS + [None, 7, b"user_remove"]:
        assert hm.clean_revoke_reason(bad) is None


def test_version_is_at_least_04112():
    from custom_components.patrimony_collection.version import INTEGRATION_VERSION

    assert tuple(int(x) for x in INTEGRATION_VERSION.split(".")) >= (0, 4, 112)
