"""0.4.119: GET /clients memberName follows the member row (PATCH /members rename and reset).
Names in this file are fixtures, never real member names."""
from __future__ import annotations

import time

from custom_components.patrimony_collection import clients as hc
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import members as hm
from tests.test_members import (  # noqa: F401  (autouse fixtures re-exported)
    _Admin, _Hass, _NonAdmin, _Req, _fake_web, _owner_jwt,
    _pq_unavailable_member_lifecycle, run,
)

OWNER = "5baa80aa11bb22cc33dd44ee55ff6677"


def _guest(hass, principal, name="Fixture Pair"):
    row, tok = hm.mint(hass, name, None, None, principal, now=time.time())
    hm.authenticate(hass, tok)
    cid = hm.client_id_for(row["memberId"])
    hc.upsert_client(hass, cid, member_name=name, principal_id=principal)
    return row["memberId"], cid


def _clients(hass, req=None):
    r = run(hh.PatrimonyClientsView(hass).get(req or _Req(user=_Admin())))
    assert r.status == 200
    return r, {c["clientId"]: c for c in r.json()["clients"]}


def _patch(hass, ident, name):
    r = run(hh.PatrimonyMemberRemoveView(hass).patch(_Req(user=_Admin(), body={"memberName": name}), ident))
    assert r.status == 200


def _members_name(hass, principal):
    doc = run(hh.PatrimonyMembersView(hass).get(_Req(user=_Admin()))).json()
    return [m["memberName"] for m in doc["members"] if m["principalId"] == principal][0]


def test_rename_reaches_clients(tmp_path):
    hass = _Hass(tmp_path)
    _mid, cid = _guest(hass, "prn_a")
    _patch(hass, "prn_a", "Fixture Duo")
    assert _clients(hass)[1][cid]["memberName"] == "Fixture Duo"
    assert hc.panel_clients_document(hass)["clients"][0]["memberName"] == "Fixture Duo"


def test_blank_and_whitespace_reset_match_get_members(tmp_path):
    hass = _Hass(tmp_path)
    _mid, cid = _guest(hass, "prn_a")
    for blank in ("Fixture Duo", "", "Fixture Duo", "   ", None):
        _patch(hass, "prn_a", blank)
        assert _clients(hass)[1][cid]["memberName"] == _members_name(hass, "prn_a")
    assert _clients(hass)[1][cid]["memberName"] == ""


def test_no_member_row_keeps_stored_value(tmp_path):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, "m_orphan", member_name="Fixture Stored", principal_id="prn_none")
    hc.upsert_client(hass, OWNER, device_name="Owner Phone")
    _r, rows = _clients(hass)
    assert rows["m_orphan"]["memberName"] == "Fixture Stored"
    assert rows[OWNER]["memberName"] is None


def test_revoked_member_row_not_used(tmp_path):
    hass = _Hass(tmp_path)
    _mid, cid = _guest(hass, "prn_a", name="Fixture Stored")
    _patch(hass, "prn_a", "Fixture Late")

    def mark(rows):  # revoked member row, client row still listed
        for r in rows.values():
            r["revokedAt"] = time.time()
    hm._update(hass, mark)
    assert _clients(hass)[1][cid]["memberName"] == "Fixture Stored"


def test_owner_pair_path_unchanged_and_resolved(tmp_path):
    hass = _Hass(tmp_path)
    hass.auth.add_llat(OWNER)
    hc.upsert_client(hass, OWNER, device_name="Owner Phone")
    _mid, cid = _guest(hass, "prn_a")
    _patch(hass, "prn_a", "Fixture Duo")
    _ra, admin_rows = _clients(hass)
    _ro, owner_rows = _clients(hass, _Req(_owner_jwt(OWNER), user=_NonAdmin()))
    assert owner_rows == admin_rows  # visibility unchanged by 0.4.119
    assert owner_rows[cid]["memberName"] == "Fixture Duo"
    r = run(hh.PatrimonyClientsView(hass).get(_Req(_owner_jwt("abcdef0011"), user=_NonAdmin())))
    assert r.status == 403


def test_clients_no_store(tmp_path):
    hass = _Hass(tmp_path)
    r, _rows = _clients(hass)
    assert r.headers.get("Cache-Control") == "no-store"
