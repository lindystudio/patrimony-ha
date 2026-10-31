"""0.4.106 chat ownership: `mine` per message on the stable principal id, never the name.

Frozen rule: a message is own only when its sender's stable id equals the viewer's
(senderKey; senderClientId fallback for rows without senderKey; neither -> false).
"""

from __future__ import annotations

import asyncio

from custom_components.patrimony_collection import chat as chat_mod
from custom_components.patrimony_collection import clients as clients_mod
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import members as members_mod
from custom_components.patrimony_collection.chat import (
    create_member_message,
    create_message,
    list_document,
    member_caller,
)
from custom_components.patrimony_collection.mapping import build_presentation_document

from tests.test_chat import T0, _Entry, _Hass, _Req, _pair, _panel
from tests.test_members import _fake_web  # noqa: F401  (aiohttp stub for the view test)


def _member(member_id, name):
    row = {"memberId": member_id, "memberName": name, "principalId": "prn_" + member_id}
    return row, members_mod.client_id_for(member_id)


def _member_req(client_id):
    req = _Req()
    req[clients_mod.MEMBER_CLIENT_KEY] = client_id
    return req


def _member_view(hass, member_row, client_id):
    return list_document(hass, _member_req(client_id), now=T0, caller=member_caller(member_row, client_id))


def _house(tmp_path):
    """Owner phone, panel admin, two members (with active client rows, like live auth)."""
    hass = _Hass(tmp_path, _Entry())
    phone = _pair(hass, "rt-owner", "Owner Phone")
    panel = _panel("admin", "user-admin")
    anna, anna_cid = _member("mem_anna", "Anna")
    ben, ben_cid = _member("mem_ben", "Ben")
    for cid, name in ((anna_cid, "Anna"), (ben_cid, "Ben")):
        clients_mod.upsert_client(hass, cid, touch_access=True, member_name=name)
    texts = {}
    for who, call in (
        ("owner", lambda: create_message(hass, phone, {"text": "o"}, now=T0)),
        ("panel", lambda: create_message(hass, panel, {"text": "p"}, now=T0)),
        ("anna", lambda: create_member_message(hass, anna, anna_cid, {"text": "a"}, now=T0)),
        ("ben", lambda: create_member_message(hass, ben, ben_cid, {"text": "b"}, now=T0)),
    ):
        body, status = call()
        assert status == 201 and body["mine"] is True  # the POST echo is the sender's own row
        texts[body["id"]] = who
    return hass, phone, panel, (anna, anna_cid), (ben, ben_cid), texts


def _mine(doc, texts):
    return {texts[m["id"]]: m["mine"] for m in doc["messages"] if m["id"] in texts}


def test_posts_store_stable_sender_keys(tmp_path):
    hass, _p, _pn, (_a, anna_cid), (_b, ben_cid), texts = _house(tmp_path)
    stored = {texts[r["id"]]: r for r in chat_mod._read_store(hass)["messages"]}
    assert stored["owner"]["senderKey"] == "ios:rt-owner" and stored["owner"]["senderClientId"] == "rt-owner"
    assert stored["panel"]["senderKey"] == "panel:user-admin" and "senderClientId" not in stored["panel"]
    assert stored["anna"]["senderKey"] == "member:mem_anna" and stored["anna"]["senderClientId"] == anna_cid
    assert stored["ben"]["senderKey"] == "member:mem_ben" and stored["ben"]["senderClientId"] == ben_cid


def test_member_sees_mine_only_on_its_own_rows(tmp_path):
    hass, _p, _pn, (anna, anna_cid), (ben, ben_cid), texts = _house(tmp_path)
    assert _mine(_member_view(hass, anna, anna_cid), texts) == {
        "owner": False, "panel": False, "anna": True, "ben": False}
    assert _mine(_member_view(hass, ben, ben_cid), texts) == {
        "owner": False, "panel": False, "anna": False, "ben": True}


def test_owner_phone_and_panel_see_mine_on_their_own_rows(tmp_path):
    hass, phone, panel, _a, _b, texts = _house(tmp_path)
    assert _mine(list_document(hass, phone, now=T0), texts) == {
        "owner": True, "panel": False, "anna": False, "ben": False}
    assert _mine(list_document(hass, panel, now=T0), texts) == {
        "owner": False, "panel": True, "anna": False, "ben": False}
    # Another admin, or a panel user without a stable id, owns nothing.
    assert not any(_mine(list_document(hass, _panel("other", "user-other"), now=T0), texts).values())
    assert not any(_mine(list_document(hass, _panel("admin", ""), now=T0), texts).values())


def test_member_request_without_explicit_viewer_is_not_treated_as_owner_phone(tmp_path):
    """The bug: a member's active m_ client row made it look like ios:m_<id>. Wired view passes member:<id>."""
    hass, _p, _pn, (anna, anna_cid), _b, texts = _house(tmp_path)
    viewer = member_caller(anna, anna_cid)
    assert viewer["key"] == "member:mem_anna" and viewer["kind"] == "member"
    import types

    async def _auth(_h, _r):
        return anna, None

    async def _plain(_h, _row, doc):
        return types.SimpleNamespace(status=200, doc=doc)

    orig_auth, orig_pq = hh._member_auth, hh._member_pq_response
    hh._member_auth, hh._member_pq_response = _auth, _plain
    try:
        hass.async_add_executor_job = lambda fn, *a: _done(fn(*a))
        r = asyncio.run(hh.PatrimonyMemberChatView(hass).get(_member_req(anna_cid)))
    finally:
        hh._member_auth, hh._member_pq_response = orig_auth, orig_pq
    assert _mine(r.doc, texts) == {"owner": False, "panel": False, "anna": True, "ben": False}
    assert all(m["canDelete"] is False for m in r.doc["messages"])


def _done(value):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(value)
    return fut


def test_legacy_rows_are_never_mine_and_client_id_fallback(tmp_path):
    hass, phone, panel, (anna, anna_cid), _b, texts = _house(tmp_path)
    key = chat_mod.load_chat_key(hass)
    store = chat_mod._read_store(hass)
    legacy = []
    for mid, extra in (
        ("legacy-none", {}),  # neither senderKey nor senderClientId
        ("legacy-owner-cid", {"senderClientId": "rt-owner"}),  # pre-senderKey phone row
        ("legacy-anna-cid", {"senderClientId": anna_cid}),
    ):
        nonce, ct = chat_mod._seal(key, mid, "x")
        row = {"id": mid, "senderKind": "ios", "senderLabel": "Owner Phone", "createdAt": "2026-10-03T12:00:00Z",
               "retention": "keep", "expiresAt": None, "nonce": nonce, "ciphertext": ct, **extra}
        legacy.append(row)
        texts[mid] = mid
    store["messages"] = list(store["messages"]) + legacy
    chat_mod._write_store(hass, store)
    owner = _mine(list_document(hass, phone, now=T0), texts)
    member = _mine(_member_view(hass, anna, anna_cid), texts)
    pnl = _mine(list_document(hass, panel, now=T0), texts)
    # Same label as the owner phone, but no stable id -> never guessed by name.
    assert owner["legacy-none"] is False and member["legacy-none"] is False and pnl["legacy-none"] is False
    assert owner["legacy-owner-cid"] is True and member["legacy-owner-cid"] is False
    assert member["legacy-anna-cid"] is True and owner["legacy-anna-cid"] is False
    assert pnl["legacy-owner-cid"] is False and pnl["legacy-anna-cid"] is False


def test_sender_id_is_opaque_and_stable_per_sender(tmp_path):
    hass, phone, _pn, (anna, anna_cid), _b, texts = _house(tmp_path)
    a = _member_view(hass, anna, anna_cid)["messages"]
    o = list_document(hass, phone, now=T0)["messages"]
    ids = {texts[m["id"]]: m["senderId"] for m in a}
    assert ids == {texts[m["id"]]: m["senderId"] for m in o}  # same tag whoever views
    assert len(set(ids.values())) == 4 and all(len(v) == 16 for v in ids.values())
    blob = repr(a) + repr(o)
    for raw in ("mem_anna", "mem_ben", anna_cid, "rt-owner", "user-admin", "member:", "ios:", "panel:"):
        assert raw not in blob


def test_display_name_is_served_from_the_config_entry(tmp_path):
    hass = _Hass(tmp_path, _Entry(display_name="North House"))
    doc = build_presentation_document(hass, dict(hass.entry.data), dict(hass.entry.options))
    assert doc["property"]["displayName"] == "North House"
