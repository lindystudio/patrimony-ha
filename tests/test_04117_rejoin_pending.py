"""0.4.117: guest identity is the owner-assigned principalId (survives remove + re-invite of the
same person; a different principal never inherits); pending (never used) invites do not count as
'other' for receipts; owner behaviour unchanged."""

from __future__ import annotations

import json

from custom_components.patrimony_collection import chat as chat_mod
from custom_components.patrimony_collection import members as members_mod
from custom_components.patrimony_collection.chat import (
    create_member_message, create_message, handle_member_read, handle_read, list_document,
)

from tests.test_chat import T0, _Entry, _Hass, _pair
from tests.test_chat_guest_identity import _mint, _post, _view


def _house(tmp_path):
    hass = _Hass(tmp_path, _Entry())
    return hass, _pair(hass, "rt-owner", "Owner Phone")


def test_same_person_rejoin_keeps_mine_sender_id_and_receipt(tmp_path):
    hass, _p = _house(tmp_path)
    g1, g3 = _mint(hass, "prn-g1"), _mint(hass, "prn-g3")
    m = _post(hass, g3, "hello 13")
    _view(hass, g1)
    handle_member_read(hass, g1[0], {"lastSeenMessageId": m}, now=T0)
    before = _view(hass, g3)[m]
    assert before["mine"] is True and before["readByOther"] is True
    members_mod.revoke_principal(hass, "prn-g3")
    g3b = _mint(hass, "prn-g3")
    after = _view(hass, g3b)[m]
    assert after["mine"] is True
    assert after["senderId"] == before["senderId"]
    assert after["readByOther"] is True and after["readBy"] == before["readBy"]
    # new message by the re-joined person carries the same senderId
    n = _post(hass, g3b, "again")
    assert _view(hass, g3b)[n]["senderId"] == before["senderId"]
    # G1 still sees it as not-mine, same sender
    v1 = _view(hass, g1)
    assert v1[m]["mine"] is False and v1[n]["senderId"] == v1[m]["senderId"]


def test_rejoin_reader_cursor_carries_over(tmp_path):
    hass, _p = _house(tmp_path)
    g1, g3 = _mint(hass, "prn-g1"), _mint(hass, "prn-g3")
    a = _post(hass, g1, "a")
    _view(hass, g3)
    handle_member_read(hass, g3[0], {"lastSeenMessageId": a}, now=T0)
    rb = _view(hass, g1)[a]["readBy"]
    members_mod.revoke_principal(hass, "prn-g3")
    _mint(hass, "prn-g3")
    assert _view(hass, g1)[a]["readBy"] == rb and _view(hass, g1)[a]["readByOther"] is True


def test_different_principal_does_not_inherit(tmp_path):
    hass, _p = _house(tmp_path)
    g3 = _mint(hass, "prn-g3")
    m = _post(hass, g3, "x")
    members_mod.revoke_principal(hass, "prn-g3")
    other = _mint(hass, "prn-other")  # same 'iPhone' label, different person
    v = _view(hass, other)[m]
    assert v["mine"] is False
    y = _post(hass, other, "y")
    assert v["senderId"] != _view(hass, other)[y]["senderId"]


def test_senderprincipal_persisted_and_survives_member_prune(tmp_path):
    hass, _p = _house(tmp_path)
    g3 = _mint(hass, "prn-g3")
    m = _post(hass, g3, "x")
    stored = json.loads(chat_mod.chat_path(hass).read_text())
    row = next(r for r in stored["messages"] if r["id"] == m)
    assert row["senderPrincipal"] == "prn-g3"
    sid = _view(hass, g3)[m]["senderId"]
    members_mod.revoke_principal(hass, "prn-g3")
    # simulate the 30-day prune: old member row gone entirely
    path = members_mod.members_path(hass)
    doc = json.loads(path.read_text())
    rows = doc["members"] if isinstance(doc, dict) and "members" in doc else doc
    old = g3[0]["memberId"]
    if isinstance(rows, dict):
        rows.pop(old, None)
    else:
        rows[:] = [r for r in rows if r.get("memberId") != old]
    path.write_text(json.dumps(doc))
    g3b = _mint(hass, "prn-g3")
    v = _view(hass, g3b)[m]
    assert v["mine"] is True and v["senderId"] == sid


def test_legacy_row_without_senderprincipal_backfilled(tmp_path):
    hass, _p = _house(tmp_path)
    g3 = _mint(hass, "prn-g3")
    m = _post(hass, g3, "x")
    p = chat_mod.chat_path(hass)
    stored = json.loads(p.read_text())
    for r in stored["messages"]:
        r.pop("senderPrincipal", None)
    p.write_text(json.dumps(stored))
    members_mod.revoke_principal(hass, "prn-g3")
    g3b = _mint(hass, "prn-g3")
    assert _view(hass, g3b)[m]["mine"] is True
    again = json.loads(p.read_text())
    assert next(r for r in again["messages"] if r["id"] == m)["senderPrincipal"] == "prn-g3"


def test_pending_guest_does_not_count_as_other(tmp_path):
    hass, phone = _house(tmp_path)
    _mint(hass, "prn-pending", used=False)  # invited, never redeemed
    body, status = create_message(hass, phone, {"text": "hi"}, now=T0)
    assert status == 201
    doc = list_document(hass, phone, now=T0)
    row = next(r for r in doc["messages"] if r["id"] == body["id"])
    assert row["deliveredToOther"] is False and row["readByOther"] is False
    keys = chat_mod._active_keys(hass, chat_mod._active_clients(hass))
    assert not any(k.startswith("member:") for k in keys)


def test_used_guest_counts_and_owner_mine_unchanged(tmp_path):
    hass, phone = _house(tmp_path)
    g = _mint(hass, "prn-g1")
    body, _ = create_message(hass, phone, {"text": "hi"}, now=T0)
    _view(hass, g)
    handle_member_read(hass, g[0], {"lastSeenMessageId": body["id"]}, now=T0)
    doc = list_document(hass, phone, now=T0)
    row = next(r for r in doc["messages"] if r["id"] == body["id"])
    assert row["mine"] is True and row["deliveredToOther"] is True and row["readByOther"] is True
    assert _view(hass, g)[body["id"]]["mine"] is False
