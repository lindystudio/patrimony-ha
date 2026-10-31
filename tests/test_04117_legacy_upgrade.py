"""0.4.117 upgrade invariants on a 0.4.116-written store: legacy keys only (senderKey
member:<memberId>, no senderPrincipal, readCursors and deliveredClientIds under
member:<memberId>), one principal with two memberIds at different cursor positions.
(1) Read/Delivered never drop: cursors/delivery carry to the principal, max over its memberIds,
    from the guest's, the other guest's and the owner's view.
(2) mine stays correct on pre-0.4.117 guest rows, with and without a re-join."""

from __future__ import annotations

import json

from custom_components.patrimony_collection import chat as chat_mod
from custom_components.patrimony_collection import members as members_mod
from custom_components.patrimony_collection.chat import create_member_message, create_message, list_document

from tests.test_chat import T0, _Entry, _Hass, _pair
from tests.test_chat_guest_identity import _mint, _view


def _owner_view(hass, phone):
    return {m["id"]: m for m in list_document(hass, phone, now=T0)["messages"]}


def _legacy_house(tmp_path, rejoin_active_second=True):
    """G3 = principal prn-g3 with memberIds A (first invite, later removed) and B (re-invite).
    Store as 0.4.116 left it: no senderPrincipal anywhere, legacy member:<id> keys."""
    hass = _Hass(tmp_path, _Entry())
    phone = _pair(hass, "rt-owner", "Owner Phone")
    g1 = _mint(hass, "prn-g1")
    g3a = _mint(hass, "prn-g3")
    ids = {}
    ids["o1"] = create_message(hass, phone, {"text": "o1"}, now=T0)[0]["id"]
    ids["g3a"] = create_member_message(hass, g3a[0], g3a[1], {"text": "hello 13"}, now=T0)[0]["id"]
    ids["o2"] = create_message(hass, phone, {"text": "o2"}, now=T0)[0]["id"]
    ids["g1"] = create_member_message(hass, g1[0], g1[1], {"text": "g1"}, now=T0)[0]["id"]
    ids["o3"] = create_message(hass, phone, {"text": "o3"}, now=T0)[0]["id"]
    members_mod.revoke_principal(hass, "prn-g3")
    g3b = _mint(hass, "prn-g3")
    A, B = "member:" + g3a[0]["memberId"], "member:" + g3b[0]["memberId"]
    p = chat_mod.chat_path(hass)
    st = json.loads(p.read_text())
    for r in st["messages"]:
        r.pop("senderPrincipal", None)
        r["deliveredClientIds"] = []
    by = {r["id"]: r for r in st["messages"]}
    # 0.4.116 delivery marks: A got everything up to o3 before removal; owner got guest rows; G1 got g3a.
    for k in ("o1", "o2", "g1", "o3"):
        by[ids[k]]["deliveredClientIds"].append(A)
    by[ids["g3a"]]["deliveredClientIds"] += ["rt-owner", "member:" + g1[0]["memberId"]]
    by[ids["g1"]]["deliveredClientIds"] += ["rt-owner"]
    # cursors: A (old) far ahead at o3, B (new) behind at o1 -> max must be o3.
    st["readCursors"] = {A: ids["o3"], B: ids["o1"], "rt-owner": ids["g1"],
                         "member:" + g1[0]["memberId"]: ids["g3a"]}
    p.write_text(json.dumps(st))
    assert "senderPrincipal" not in p.read_text()
    return hass, phone, g1, g3a, g3b, ids, A, B


def test_legacy_cursors_take_max_over_principal_memberids(tmp_path):
    hass, phone, g1, _a, g3b, ids, A, B = _legacy_house(tmp_path)
    ov = _owner_view(hass, phone)
    # owner's messages o2, o3 were read only by A's (old) cursor; B's cursor is at o1 -> max wins.
    for k in ("o1", "o2", "o3"):
        assert ov[ids[k]]["readByOther"] is True, k
        assert ov[ids[k]]["deliveredToOther"] is True, k
    g3_sid = _view(hass, g3b)[ids["g3a"]]["senderId"]
    assert g3_sid in ov[ids["o3"]]["readBy"]
    # stored cursors untouched (no rewrite of legacy keys)
    st = json.loads(chat_mod.chat_path(hass).read_text())
    assert st["readCursors"][A] == ids["o3"] and st["readCursors"][B] == ids["o1"]


def test_legacy_guest_read_and_delivered_from_other_guest_view(tmp_path):
    hass, _phone, g1, _a, g3b, ids, _A, _B = _legacy_house(tmp_path)
    v1 = _view(hass, g1)
    # G1's message was read + delivered by G3 via the old memberId A only.
    assert v1[ids["g1"]]["readByOther"] is True and v1[ids["g1"]]["deliveredToOther"] is True
    assert _view(hass, g3b)[ids["g3a"]]["senderId"] in v1[ids["g1"]]["readBy"]


def test_legacy_guest_own_old_message_keeps_read_and_mine_after_rejoin(tmp_path):
    hass, phone, g1, _a, g3b, ids, _A, _B = _legacy_house(tmp_path)
    v3 = _view(hass, g3b)[ids["g3a"]]
    assert v3["mine"] is True                       # invariant (2), with re-join
    assert v3["readByOther"] is True and v3["deliveredToOther"] is True  # read by G1 and owner
    assert _view(hass, g1)[ids["g3a"]]["mine"] is False
    assert _owner_view(hass, phone)[ids["g3a"]]["mine"] is False


def test_legacy_mine_without_rejoin(tmp_path):
    hass = _Hass(tmp_path, _Entry())
    phone = _pair(hass, "rt-owner", "Owner Phone")
    g3 = _mint(hass, "prn-g3")
    m = create_member_message(hass, g3[0], g3[1], {"text": "x"}, now=T0)[0]["id"]
    p = chat_mod.chat_path(hass)
    st = json.loads(p.read_text())
    for r in st["messages"]:
        r.pop("senderPrincipal", None)
    p.write_text(json.dumps(st))
    sid_owner = _owner_view(hass, phone)[m]["senderId"]
    v = _view(hass, g3)[m]
    assert v["mine"] is True and v["senderId"] == sid_owner  # invariant (2), no re-join
    assert _owner_view(hass, phone)[m]["mine"] is False


def test_legacy_delivered_not_lost_when_only_old_memberid_marked(tmp_path):
    hass, phone, _g1, _a, _b, ids, _A, _B = _legacy_house(tmp_path)
    # strip the owner read cursor's effect: only delivery to A remains for o2 besides cursors
    st = json.loads(chat_mod.chat_path(hass).read_text())
    st["readCursors"] = {}
    chat_mod.chat_path(hass).write_text(json.dumps(st))
    ov = _owner_view(hass, phone)
    assert ov[ids["o2"]]["deliveredToOther"] is True and ov[ids["o2"]]["readByOther"] is False
