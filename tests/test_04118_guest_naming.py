"""0.4.118: owner rename per principal (PATCH /members/{id}), read-time guest and
owner labels, m_ label-leak fix, GET /members invitedAt/connectedAt, owner self-rename on
POST /clients/{id}. Names in this file are fixtures, never real member names."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from custom_components.patrimony_collection import chat as chat_mod
from custom_components.patrimony_collection import clients as hc
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import members as hm
from custom_components.patrimony_collection.chat import create_message, list_document
from tests.test_chat import T0, _Entry, _pair
from tests.test_chat import _Hass as _ChatHass
from tests.test_chat_guest_identity import _mint as _chat_mint, _post, _view
from tests.test_members import (  # noqa: F401  (autouse fixtures re-exported)
    _Admin, _Hass, _NonAdmin, _Req, _fake_web, _owner_jwt,
    _pq_unavailable_member_lifecycle, run,
)

OWNER = "6a7b8caa11bb22cc33dd44ee55ff6677"
OWNER2 = "5c1a7e9988776655443322110000aaaa"
BOX = "a0b1c20011223344556677889900aabb"


def _patch(hass, ident, body, req=None):
    req = req or _Req(user=_Admin(), body=body)
    if req._body is None:
        req._body = body
    r = run(hh.PatrimonyMemberRemoveView(hass).patch(req, ident))
    return r.status, r.json()


def _members(hass):
    r = run(hh.PatrimonyMembersView(hass).get(_Req(user=_Admin())))
    assert r.status == 200
    doc = r.json()
    return doc["members"] + doc["pending"] + doc["inactive"]


def _mint_row(hass, principal, name="iPhone", used=True, now=None):
    row, tok = hm.mint(hass, name, None, None, principal, now=now or time.time())
    if used:
        hm.authenticate(hass, tok)
    return row["memberId"]


# ---------------- PATCH /members/{id} ----------------

def test_patch_by_principal_and_by_member_id_applies_to_all_rows(tmp_path):
    hass = _Hass(tmp_path)
    m1 = _mint_row(hass, "prn_a")
    hm.revoke_principal(hass, "prn_a")
    m2 = _mint_row(hass, "prn_a")
    status, out = _patch(hass, "prn_a", {"memberName": "Fixture A"})
    assert status == 200 and out == {"ok": True, "principalId": "prn_a", "memberId": m2,
                                     "memberName": "Fixture A", "updatedRows": 2}
    status, out = _patch(hass, m1, {"memberName": "Fixture B"})
    assert status == 200 and out["principalId"] == "prn_a" and out["memberId"] == m2
    assert {r["memberName"] for r in hm.load_rows(hass).values()} == {"Fixture B"}


def test_patch_unknown_id_404(tmp_path):
    hass = _Hass(tmp_path)
    _mint_row(hass, "prn_a")
    status, out = _patch(hass, "nope", {"memberName": "Fixture"})
    assert status == 404 and out["error"]["code"] == "member_not_found"


def test_patch_null_and_empty_revert_to_null(tmp_path):
    hass = _Hass(tmp_path)
    _mint_row(hass, "prn_a", name="Fixture")
    for body in ({"memberName": None}, {"memberName": "  "}, {}):
        status, out = _patch(hass, "prn_a", body)
        assert status == 200 and out["memberName"] is None and out["updatedRows"] == 1
        assert all(r["memberName"] is None for r in hm.load_rows(hass).values())
        hm.rename_principal(hass, "prn_a", "Fixture")


def test_patch_rejections(tmp_path):
    hass = _Hass(tmp_path)
    _mint_row(hass, "prn_a", name="Keep")
    cases = [
        ("x" * 81, "invalid_member_name", "too_long"),
        ("Bad\nName", "invalid_member_name", "control_characters"),
        ("Bad\u0007", "invalid_member_name", "control_characters"),
        ("a\u2028b", "invalid_member_name", "control_characters"),
        ("pmt_abc", "invalid_member_name", "not_allowed"),
        (42, "invalid_member_name", "not_a_string"),
        ("iPhone", "reserved_member_name", "reserved"),
        ("Guest", "reserved_member_name", "reserved"),
        ("Home Assistant", "reserved_member_name", "reserved"),
    ]
    for value, code, reason in cases:
        status, out = _patch(hass, "prn_a", {"memberName": value})
        assert status == 400, value
        err = out["error"]
        assert err["code"] == code and err["reason"] == reason and err["field"] == "memberName"
        if reason == "too_long":
            assert err["maxLength"] == 80
    assert _patch(hass, "prn_a", {"memberName": "x" * 80})[0] == 200
    assert _patch(hass, "nope", {"memberName": "iPhone"})[1]["error"]["code"] == "reserved_member_name"


def test_patch_invalid_json_and_credentials(tmp_path):
    hass = _Hass(tmp_path)
    _mint_row(hass, "prn_a")
    req = _Req(user=_Admin())
    r = run(hh.PatrimonyMemberRemoveView(hass).patch(req, "prn_a"))
    assert r.status == 400 and r.json()["error"]["code"] == "invalid_json"
    hass.auth.add_llat(BOX)
    st, out = _patch(hass, "prn_a", {"memberName": "X"}, _Req(_owner_jwt(BOX), user=_Admin(), body={"memberName": "X"}))
    assert st == 403 and out["error"]["code"] == "llat_not_allowed"
    st, out = _patch(hass, "prn_a", {"memberName": "X"}, _Req(user=_NonAdmin(), body={"memberName": "X"}))
    assert st == 403 and out["error"]["code"] == "forbidden"
    greq = _Req(user=_Admin(), body={"memberName": "X"})
    greq[hc.MEMBER_CLIENT_KEY] = "m_x"
    assert _patch(hass, "prn_a", None, greq)[0] == 403
    hass.auth.add_llat(OWNER)
    hc.upsert_client(hass, OWNER, device_name="Owner Phone")
    st, out = _patch(hass, "prn_a", None, _Req(_owner_jwt(OWNER), user=_NonAdmin(), body={"memberName": "Fixture"}))
    assert st == 200 and out["memberName"] == "Fixture"


def test_patch_audit_log_never_contains_the_name(tmp_path, caplog):
    hass = _Hass(tmp_path)
    _mint_row(hass, "prn_a")
    with caplog.at_level(logging.DEBUG):
        assert _patch(hass, "prn_a", {"memberName": "Zyxwv Fixture"})[0] == 200
    assert "patrimony_member_renamed" in caplog.text
    assert "Zyxwv" not in caplog.text


# ---------------- GET /members dates ----------------

def test_members_invited_and_connected_for_rejoined_and_never_connected(tmp_path):
    hass = _Hass(tmp_path)
    t1 = time.time() - 3 * 86400
    _mint_row(hass, "prn_a", now=t1)
    first_used = hm.load_rows(hass)
    hm.revoke_principal(hass, "prn_a")
    _mint_row(hass, "prn_a", now=t1 + 86400)
    _mint_row(hass, "prn_p", used=False)
    rows = {r["memberId"]: r for r in _members(hass)}
    a = [r for r in rows.values() if r["principalId"] == "prn_a"]
    assert len(a) == 2
    expect = datetime.fromtimestamp(t1, timezone.utc).astimezone(ZoneInfo("Europe/Lisbon"))
    for r in a:
        assert r["invitedAt"] == expect.isoformat(timespec="seconds")
        assert r["connectedAt"] is not None and r["connectedAt"][-6] in "+-"
    used = min(float(x["firstUsedAt"]) for x in first_used.values())
    assert a[0]["connectedAt"] == datetime.fromtimestamp(used, timezone.utc).astimezone(
        ZoneInfo("Europe/Lisbon")).isoformat(timespec="seconds")
    p = [r for r in rows.values() if r["principalId"] == "prn_p"][0]
    assert p["invitedAt"] and p["connectedAt"] is None


# ---------------- read-time labels ----------------

def _chat_house(tmp_path):
    hass = _ChatHass(tmp_path, _Entry())
    phone = _pair(hass, "rt-owner", "Owner Phone")
    return hass, phone


def test_rename_shows_in_old_history_for_every_member_id(tmp_path):
    hass, _phone = _chat_house(tmp_path)
    g_old = _chat_mint(hass, "prn-g1")
    a = _post(hass, g_old, "before")
    hm.revoke_principal(hass, "prn-g1")
    g_new = _chat_mint(hass, "prn-g1")
    b = _post(hass, g_new, "after")
    v = _view(hass, g_new)
    assert v[a]["sender"].startswith("Guest \u00b7 ") and v[a]["sender"] == v[b]["sender"]
    hm.rename_principal(hass, g_old[0]["memberId"], "Fixture G")
    v = _view(hass, g_new)
    assert v[a]["sender"] == "Fixture G" and v[b]["sender"] == "Fixture G"
    owner_view = {m["id"]: m for m in list_document(hass, _phone, now=T0)["messages"]}
    assert owner_view[a]["sender"] == "Fixture G"
    hm.rename_principal(hass, "prn-g1", None)
    assert _view(hass, g_new)[a]["sender"].startswith("Guest \u00b7 ")
    # Storage is not rewritten.
    stored = {m["id"]: m for m in chat_mod._read_store(hass)["messages"]}
    assert stored[a]["senderLabel"] == "iPhone"


def test_guest_label_date_first_invite_house_tz_year_only_when_different(tmp_path):
    hass = _ChatHass(tmp_path, _Entry())
    zone = ZoneInfo("Europe/Paris")
    late = datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc).timestamp()  # 1 Nov in Paris
    now = datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()
    assert hm.guest_fallback_label(hass, late, now=now, zone=zone) == "Guest \u00b7 1 Nov"
    old = datetime(2025, 10, 3, 12, tzinfo=timezone.utc).timestamp()
    assert hm.guest_fallback_label(hass, old, now=now, zone=zone) == "Guest \u00b7 3 Oct 2025"
    assert hm.guest_fallback_label(hass, None) == "Guest"
    rows = {"a": {"memberId": "a", "principalId": "p", "memberName": None, "createdAt": old + 86400,
                  "exp": now + 9e6, "firstUsedAt": old + 86400},
            "b": {"memberId": "b", "principalId": "p", "memberName": "Member", "createdAt": old,
                  "exp": now - 1, "revokedAt": old + 10}}
    assert hm.principal_label(hass, rows, "p", now=now, zone=zone) == "Guest \u00b7 3 Oct 2025"


def test_owner_rename_shows_in_old_history(tmp_path):
    hass, phone = _chat_house(tmp_path)
    body, status = create_message(hass, phone, {"text": "hi", "deviceName": "Owner Phone"}, now=T0)
    assert status == 201
    hc.rename_client(hass, "rt-owner", "Fixture Owner")
    g = _chat_mint(hass, "prn-g1")
    assert _view(hass, g)[body["id"]]["sender"] == "Fixture Owner"


def test_unedited_owner_keeps_stored_label(tmp_path):
    hass = _ChatHass(tmp_path, _Entry())
    hc.upsert_client(hass, "rt-x", device_name="Stored Phone", now=T0)
    assert hc.owner_read_label(hass, "rt-x") is None
    assert hc.owner_read_label(hass, "m_abc") is None


def test_no_guest_name_leak_into_owner_labels(tmp_path):
    hass = _ChatHass(tmp_path, _Entry())
    _chat_mint(hass, "prn-g1", name="Fixture Guest")
    assert "Fixture Guest" not in hc.active_display_names(hass)
    assert chat_mod.ios_sender_label(hass) != "Fixture Guest"
    _pair(hass, "rt-owner", "Owner Phone")
    assert hc.active_display_names(hass) == ["Owner Phone"]
    assert chat_mod.ios_sender_label(hass) == "Owner Phone"


# ---------------- POST /clients/{id} ----------------

def _rename(hass, req, cid):
    r = run(hh.PatrimonyClientView(hass).post(req, cid))
    return r.status, r.json()


def test_owner_renames_own_row_only(tmp_path):
    hass = _Hass(tmp_path)
    hass.auth.add_llat(OWNER)
    hass.auth.add_llat(OWNER2)
    hc.upsert_client(hass, OWNER, device_name="Owner Phone")
    hc.upsert_client(hass, OWNER2, device_name="Other Phone")
    for user in (_NonAdmin(), _Admin()):
        st, out = _rename(hass, _Req(_owner_jwt(OWNER), user=user, body={"displayName": "Fixture Me"}), OWNER)
        assert st == 200 and out["client"]["displayName"] == "Fixture Me"
        st, out = _rename(hass, _Req(_owner_jwt(OWNER), user=user, body={"displayName": "Nope"}), OWNER2)
        assert st == 403 and out["error"]["code"] == "forbidden"
    assert hc.get_client(hass, OWNER2)["displayName"] == "Other Phone"
    assert hc.get_client(hass, OWNER)["nameEdited"] is True


def test_client_rename_guest_and_llat_and_admin_paths(tmp_path):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, OWNER2, device_name="Other Phone")
    greq = _Req(user=_NonAdmin(), body={"displayName": "X"})
    greq[hc.MEMBER_CLIENT_KEY] = "m_abc"
    assert _rename(hass, greq, OWNER2)[0] == 403
    gadm = _Req(user=_Admin(), body={"displayName": "X"})
    gadm[hc.MEMBER_CLIENT_KEY] = "m_abc"
    assert _rename(hass, gadm, OWNER2)[0] == 403
    hass.auth.add_llat(BOX)
    st, out = _rename(hass, _Req(_owner_jwt(BOX), user=_NonAdmin(), body={"displayName": "X"}), OWNER2)
    assert st == 403 and out["error"]["code"] == "llat_not_allowed"
    st, out = _rename(hass, _Req(_owner_jwt("abcdef0011"), user=_NonAdmin(), body={"displayName": "X"}), OWNER2)
    assert st == 403
    # Admin path unchanged: any row, 404 for unknown.
    st, out = _rename(hass, _Req(user=_Admin(), body={"displayName": "Fixture Admin"}), OWNER2)
    assert st == 200 and out["client"]["displayName"] == "Fixture Admin"
    assert _rename(hass, _Req(user=_Admin(), body={"displayName": "X"}), "unknown")[0] == 404
