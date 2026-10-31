"""0.4.116 guest chat receipts: guests have a read cursor (member:<memberId>), guests count as
'other' for delivered/read, senders matched by stable key only, revoked guests never count,
labels unchanged, is_mine unchanged."""

from __future__ import annotations

import time

from custom_components.patrimony_collection import chat as chat_mod
from custom_components.patrimony_collection import clients as clients_mod
from custom_components.patrimony_collection import members as members_mod
from custom_components.patrimony_collection.chat import (
    create_member_message,
    create_message,
    handle_member_read,
    handle_read,
    list_document,
    member_caller,
)

from tests.test_chat import T0, _Entry, _Hass, _pair
from tests.test_chat_mine import _member_req


def _mint(hass, principal, name="iPhone", used=True):
    row, _tok = members_mod.mint(hass, name, None, None, principal, now=time.time())
    if used:  # 0.4.117: only guests who used their invite count as 'other'
        members_mod.authenticate(hass, _tok)
    rec = members_mod.load_rows(hass)[row["memberId"]]
    cid = members_mod.client_id_for(rec["memberId"])
    clients_mod.upsert_client(hass, cid, touch_access=True, member_name=name, principal_id=principal)
    return rec, cid


def _view(hass, g):
    doc = list_document(hass, _member_req(g[1]), now=T0, caller=member_caller(g[0], g[1]))
    return {m["id"]: m for m in doc["messages"]}


def _house(tmp_path):
    hass = _Hass(tmp_path, _Entry())
    phone = _pair(hass, "rt-owner", "Owner Phone")
    return hass, phone, _mint(hass, "prn-g1"), _mint(hass, "prn-g3")


def _post(hass, g, text):
    body, status = create_member_message(hass, g[0], g[1], {"text": text, "deviceName": "iPhone"}, now=T0)
    assert status == 201
    return body["id"]


def test_two_iphone_guests_fallback_labels_but_ids_distinct(tmp_path):
    hass, _p, g1, g3 = _house(tmp_path)
    a, b = _post(hass, g1, "a"), _post(hass, g3, "b")
    v = _view(hass, g1)
    # 0.4.118: a device-named guest reads as 'Guest · D Mon' (never 'iPhone').
    assert v[a]["sender"].startswith("Guest \u00b7 ") and v[b]["sender"].startswith("Guest \u00b7 ")
    assert v[a]["senderId"] != v[b]["senderId"]
    assert v[a]["mine"] is True and v[b]["mine"] is False
    assert _view(hass, g3)[b]["mine"] is True and _view(hass, g3)[a]["mine"] is False


def test_guest_get_marks_delivered_guest_to_guest(tmp_path):
    hass, _p, g1, g3 = _house(tmp_path)
    a = _post(hass, g1, "a")
    _view(hass, g1)  # sender's own GET never delivers to "other"
    assert _view(hass, g1)[a]["deliveredToOther"] is False
    _view(hass, g3)
    assert _view(hass, g1)[a]["deliveredToOther"] is True
    stored = chat_mod._read_store(hass)["messages"][0]["deliveredClientIds"]
    assert stored == ["member:" + g3[0]["memberId"]]


def test_guest_read_receipts_both_ways_and_own_read_ignored(tmp_path):
    hass, phone, g1, g3 = _house(tmp_path)
    a = _post(hass, g1, "a")
    assert handle_member_read(hass, g1[0], {"lastSeenMessageId": a}, now=T0) == ({"ok": True}, 200)
    v = _view(hass, g1)[a]
    assert v["readByOther"] is False and v["readBy"] == []
    assert handle_member_read(hass, g3[0], {"lastSeenMessageId": a}, now=T0)[1] == 200
    b = _post(hass, g3, "b")
    v = _view(hass, g1)
    assert v[a]["readByOther"] is True and v[a]["readBy"] == [v[b]["senderId"]]
    # owner message read by a guest; guest message read by the owner
    o, _ = create_message(hass, phone, {"text": "o"}, now=T0)
    assert handle_member_read(hass, g1[0], {"lastSeenMessageId": o["id"]}, now=T0)[1] == 200
    owner_doc = {m["id"]: m for m in list_document(hass, phone, now=T0)["messages"]}
    assert owner_doc[o["id"]]["readByOther"] is True
    assert owner_doc[b]["readByOther"] is True  # g1 cursor past b too
    assert handle_read(hass, phone, {"lastSeenMessageId": o["id"]}, now=T0)[1] == 200
    v = _view(hass, g3)
    assert v[b]["readByOther"] is True and len(v[b]["readBy"]) == 2


def test_member_read_bad_or_unknown_id_400_no_state_change(tmp_path):
    hass, _p, g1, _g3 = _house(tmp_path)
    _post(hass, g1, "a")
    before = chat_mod._read_store(hass)
    for body in ({"lastSeenMessageId": "nope"}, {"lastSeenMessageId": ""}, {}, "x", {"lastSeenMessageId": 5}):
        assert handle_member_read(hass, g1[0], body, now=T0)[1] == 400
    assert chat_mod._read_store(hass) == before


def test_member_cursor_is_monotonic(tmp_path):
    hass, _p, g1, g3 = _house(tmp_path)
    a, b = _post(hass, g1, "a"), _post(hass, g1, "b")
    handle_member_read(hass, g3[0], {"lastSeenMessageId": b}, now=T0)
    handle_member_read(hass, g3[0], {"lastSeenMessageId": a}, now=T0)
    key = "member:" + g3[0]["memberId"]
    assert chat_mod._read_store(hass)["readCursors"][key] == b


def test_revoked_guest_cursor_and_delivery_do_not_count(tmp_path):
    hass, _p, g1, g3 = _house(tmp_path)
    a = _post(hass, g1, "a")
    _view(hass, g3)
    handle_member_read(hass, g3[0], {"lastSeenMessageId": a}, now=T0)
    assert _view(hass, g1)[a]["readByOther"] is True
    def mut(r):
        r[g3[0]["memberId"]]["revokedAt"] = time.time()
    members_mod._update(hass, mut)
    v = _view(hass, g1)[a]
    assert v["readByOther"] is False and v["deliveredToOther"] is False and v["readBy"] == []


def test_legacy_label_rows_not_matched_by_label(tmp_path):
    hass, phone, g1, _g3 = _house(tmp_path)
    o, _ = create_message(hass, phone, {"text": "o"}, now=T0)
    store = chat_mod._read_store(hass)
    for r in store["messages"]:
        r.pop("senderKey", None)
        r.pop("senderClientId", None)
        r["senderLabel"] = "Owner Phone"  # equals the owner's display name
    chat_mod._write_store(hass, store)
    assert handle_read(hass, phone, {"lastSeenMessageId": o["id"]}, now=T0)[1] == 200
    v = _view(hass, g1)[o["id"]]
    # unattributed legacy row: the owner's read counts (no label match), never mine for anyone
    assert v["readByOther"] is True and v["mine"] is False
