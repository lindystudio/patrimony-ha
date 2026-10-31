"""0.4.102 member read + write (photo, contacts, notes, chat post): pmt_ auth, PQ both ways, 428, ETag, write log."""

from __future__ import annotations

import asyncio
import base64
import json
import types

import pytest

from custom_components.patrimony_collection import chat as hchat
from custom_components.patrimony_collection import fileio
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import member_api as ma
from custom_components.patrimony_collection import photo as hphoto
from custom_components.patrimony_collection.calendars import etag_for
from custom_components.patrimony_collection.const import CONF_MEMBER_PQ_REQUIRED, PHOTO_MAX_BYTES
from custom_components.patrimony_collection.pq_shield import hybrid as hy
from custom_components.patrimony_collection.pq_shield import member_views as mv
from custom_components.patrimony_collection.pq_shield import session as pqs

from tests.test_members import _Admin, _Hass, _NonAdmin, _Req, _mint, _revoke, _fake_web  # noqa: F401

ok, _why = pqs.probe_mlkem()
pytestmark = pytest.mark.skipif(not ok, reason="ML-KEM unavailable")

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"house"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def run(c):
    return asyncio.run(c)


class _MReq(_Req):
    def __init__(self, token=None, user=None, body=None, inm=None, if_match=None):
        super().__init__(token, user, body)
        if inm:
            self.headers["If-None-Match"] = inm
        if if_match:
            self.headers["If-Match"] = if_match


@pytest.fixture
def house(tmp_path, monkeypatch):
    fileio.reset_cache()
    hass = _Hass(tmp_path)
    (tmp_path / "patrimony_collection").mkdir(exist_ok=True)
    opts = {}
    entry = types.SimpleNamespace(data={}, options=opts)
    monkeypatch.setattr(hh, "_first_entry", lambda _h: entry)
    monkeypatch.setattr(mv, "_require_configured", lambda _h: None)
    monkeypatch.setattr(pqs.PQShieldManager, "feature_enabled", property(lambda self: True))
    pushes = []
    monkeypatch.setattr(hchat, "notify_house_chat",
                        lambda h, exclude_device=None, sender_client=None: pushes.append((exclude_device, sender_client)))
    # 0.4.110: both chat handlers send through send_house_chat_push (returns {ok, status}).
    monkeypatch.setattr(hchat, "send_house_chat_push",
                        lambda h, exclude_device=None, sender_client=None, **_kw:
                        pushes.append((exclude_device, sender_client)) or {"ok": True, "status": 202})
    run(pqs.async_setup_pq_shield(hass))
    hass.pushes = pushes
    return hass, opts


def _hand(hass, tok):
    from tests.test_member_pq import _handshake

    return _handshake(hass, tok)


def _member(hass, **kw):
    status, doc = _mint(hass, **kw)
    assert status == 201
    tok = doc["token"]
    return tok, _hand(hass, tok), doc["member"]


def _seal(key, doc):
    return hy.seal_document(key, doc).to_dict()


def _open(key, resp):
    env = resp.json()
    assert {"pqShield", "kem", "aead", "nonce", "ct"} <= set(env), env
    return hy.open_document(key, env)


ALL_CALLS = [
    lambda h, r: ma.PatrimonyMemberPhotoView(h).get(r),
    lambda h, r: ma.PatrimonyMemberPhotoView(h).put(r),
    lambda h, r: ma.PatrimonyMemberPhotoView(h).post(r),
    lambda h, r: ma.PatrimonyMemberPhotoView(h).delete(r),
    lambda h, r: ma.PatrimonyMemberContactsView(h).get(r),
    lambda h, r: ma.PatrimonyMemberContactsView(h).put(r),
    lambda h, r: ma.PatrimonyMemberContactsView(h).post(r),
    lambda h, r: ma.PatrimonyMemberContactView(h).patch(r, "x"),
    lambda h, r: ma.PatrimonyMemberContactView(h).delete(r, "x"),
    lambda h, r: ma.PatrimonyMemberNotesView(h).get(r),
    lambda h, r: ma.PatrimonyMemberNotesView(h).put(r),
    lambda h, r: hh.PatrimonyMemberChatView(h).post(r),
    lambda h, r: ma.PatrimonyMemberChatImageView(h).get(r, "x"),
]


# ------------------------------------------------------------------ auth / PQ gates

def test_all_routes_401_without_or_with_bad_or_revoked_token(house):
    hass, _ = house
    tok, _key, _m = _member(hass)
    assert _revoke(hass, "prn_anna")[0] == 200
    for call in ALL_CALLS:
        for t in (None, "pmt_wrong", "hek_x"):
            r = run(call(hass, _MReq(t, body={})))
            assert r.status == 401 and r.headers["WWW-Authenticate"].startswith("Bearer")
        r = run(call(hass, _MReq(tok, body={})))
        assert r.status == 401 and r.json()["error"]["code"] == "member_token_revoked"


def test_all_routes_428_without_session_before_reading_body(house):
    hass, _ = house
    tok = _mint(hass)[1]["token"]
    for call in ALL_CALLS:
        r = run(call(hass, _MReq(tok, body=None)))  # body unreadable: 428 must come first
        assert r.status == 428 and r.json()["error"]["code"] == "pq_session_required"
        assert r.json()["pqShield"]["session"] == "absent"


def test_plain_body_rejected_when_member_has_session(house):
    hass, _ = house
    tok, key, _m = _member(hass)
    r = run(ma.PatrimonyMemberNotesView(hass).put(_MReq(tok, body={"text": "plain"})))
    assert r.status == 400 and r.json()["error"]["code"] == "pq_envelope_required"
    assert ma.notes_document(hass)["text"] == ""


def test_foreign_envelope_rejected(house):
    hass, _ = house
    tok_a, key_a, _ = _member(hass, memberName="A", principalId="prn_a")
    tok_b, key_b, _ = _member(hass, memberName="B", principalId="prn_b")
    r = run(ma.PatrimonyMemberNotesView(hass).put(_MReq(tok_a, body=_seal(key_b, {"text": "x"}))))
    assert r.status == 400 and r.json()["error"]["code"] == "pq_open_failed"


def test_oversized_body_413(house):
    hass, _ = house
    tok, key, _ = _member(hass)
    req = _MReq(tok, body={})
    req.headers["Content-Length"] = str(ma.BODY_MAX_DEFAULT + 1)
    r = run(ma.PatrimonyMemberNotesView(hass).put(req))
    assert r.status == 413 and r.json()["error"]["code"] == "too_large"


def test_flag_off_member_pq_required_sealed_both_ways(house, monkeypatch):
    """0.4.108: flag Off + member PQ required: writes still sealed, sessionless member 428."""
    hass, _ = house
    tok, key, _ = _member(hass)
    bare = _mint(hass)[1]["token"]
    monkeypatch.setattr(pqs.PQShieldManager, "feature_enabled", property(lambda self: False))
    r = run(ma.PatrimonyMemberNotesView(hass).put(_MReq(tok, body={"text": "hi"})))
    assert r.status == 400 and r.json()["error"]["code"] == "pq_envelope_required"
    r = run(ma.PatrimonyMemberNotesView(hass).put(_MReq(tok, body=_seal(key, {"text": "x"}))))
    assert r.status == 200 and _open(key, r)["text"] == "x"
    r = run(ma.PatrimonyMemberNotesView(hass).put(_MReq(bare, body={"text": "hi"})))
    assert r.status == 428 and r.json()["error"]["code"] == "pq_session_required"


def test_shield_off_plain_both_ways_and_envelope_refused(house, monkeypatch):
    """Transition (member_pq_required False) + flag Off: plain both ways, as before 0.4.108."""
    hass, opts = house
    tok, key, _ = _member(hass)
    opts[CONF_MEMBER_PQ_REQUIRED] = False
    monkeypatch.setattr(pqs.PQShieldManager, "feature_enabled", property(lambda self: False))
    r = run(ma.PatrimonyMemberNotesView(hass).put(_MReq(tok, body={"text": "hi"})))
    assert r.status == 200 and r.json()["text"] == "hi"
    r = run(ma.PatrimonyMemberNotesView(hass).put(_MReq(tok, body=_seal(key, {"text": "x"}))))
    assert r.status == 400 and r.json()["error"]["code"] == "pq_shield_off"


def test_transition_not_required_plain_without_session(house):
    hass, opts = house
    tok = _mint(hass)[1]["token"]
    opts[CONF_MEMBER_PQ_REQUIRED] = False
    r = run(ma.PatrimonyMemberContactsView(hass).post(_MReq(tok, body={"name": "Plumber", "phone": "+15550101"})))
    assert r.status == 201 and r.json()["tel"] == "+15550101"


def test_member_views_skip_ha_auth_owner_log_does_not():
    for v in (ma.PatrimonyMemberPhotoView, ma.PatrimonyMemberContactsView, ma.PatrimonyMemberContactView,
              ma.PatrimonyMemberNotesView):
        assert v.requires_auth is False and v.url.startswith("/api/patrimony_collection/m/")
    assert ma.PatrimonyMemberWriteLogView.requires_auth is True


def test_state_document_unchanged_no_new_fields(house, monkeypatch):
    """Frozen PresentationDocument: /m/state is untouched by 0.4.102."""
    hass, _ = house
    monkeypatch.setattr(hh, "build_presentation_document", lambda *_a: {"schemaVersion": 1, "cards": []})
    tok, key, _ = _member(hass)
    doc = _open(key, run(hh.PatrimonyMemberStateView(hass).get(_MReq(tok))))
    assert set(doc) == {"schemaVersion", "cards", "pairing"}


# ------------------------------------------------------------------ photo

def test_photo_get_default_sealed_etag_304(house):
    hass, _ = house
    tok, key, _ = _member(hass)
    r = run(ma.PatrimonyMemberPhotoView(hass).get(_MReq(tok)))
    doc = _open(key, r)
    assert r.status == 200 and doc["isDefault"] is True and doc["updatedAt"] is None
    assert base64.b64decode(doc["data"]) == hphoto.load_default_photo() and doc["contentType"] == "image/jpeg"
    assert r.headers["ETag"] == etag_for(doc)
    r304 = run(ma.PatrimonyMemberPhotoView(hass).get(_MReq(tok, inm=r.headers["ETag"])))
    assert r304.status == 304 and r304.text == ""


def test_photo_etag_never_bypasses_428(house):
    hass, _ = house
    tok = _mint(hass)[1]["token"]
    tag = etag_for(ma.photo_document(hass))
    assert run(ma.PatrimonyMemberPhotoView(hass).get(_MReq(tok, inm=tag))).status == 428


def test_photo_put_post_restore_delete_and_validation(house):
    hass, _ = house
    tok, key, mem = _member(hass)
    v = ma.PatrimonyMemberPhotoView(hass)
    r = run(v.put(_MReq(tok, body=_seal(key, {"contentType": "image/jpeg", "data": base64.b64encode(JPEG).decode()}))))
    meta = _open(key, r)
    assert r.status == 200 and meta["isDefault"] is False and meta["contentType"] == "image/jpeg" and "data" not in meta
    assert hphoto.load_photo(hass) == JPEG
    got = _open(key, run(v.get(_MReq(tok))))
    assert base64.b64decode(got["data"]) == JPEG and got["isDefault"] is False and got["updatedAt"].endswith("Z")
    # POST with an image = same as PUT
    r = run(v.post(_MReq(tok, body=_seal(key, {"contentType": "image/webp", "data": base64.b64encode(WEBP).decode()}))))
    assert r.status == 200 and hphoto.load_photo(hass) == WEBP
    # owner validation reused
    for body, code in (
        ({"contentType": "image/png", "data": base64.b64encode(JPEG).decode()}, "bad_type"),
        ({"contentType": "image/webp", "data": base64.b64encode(JPEG).decode()}, "bad_type"),
        ({"contentType": "image/jpeg", "data": "not base64!!"}, "bad_type"),
        ({"contentType": "image/jpeg", "data": "data:image/jpeg;base64,AAAA"}, "bad_type"),
        ({"contentType": "image/jpeg", "data": base64.b64encode(JPEG + b"\0" * PHOTO_MAX_BYTES).decode()}, "too_large"),
    ):
        r = run(v.put(_MReq(tok, body=_seal(key, body))))
        assert r.status == 400 and r.json()["error"]["code"] == code
    assert hphoto.load_photo(hass) == WEBP
    r = run(v.post(_MReq(tok, body=_seal(key, {"restoreDefault": True}))))
    assert r.status == 200 and hphoto.load_photo(hass) == hphoto.load_default_photo()
    r = run(v.delete(_MReq(tok)))
    assert r.status == 200 and _open(key, r)["isDefault"] is True
    assert run(v.delete(_MReq(tok))).status == 404
    log = ma.write_log(hass)
    assert [(e["scope"], e["action"]) for e in log] == [
        ("photo", "set"), ("photo", "set"), ("photo", "restore_default"), ("photo", "delete")]
    assert all(e["memberId"] == mem["memberId"] and e["clientId"].startswith("m_") for e in log)


# ------------------------------------------------------------------ contacts

def test_contacts_crud_frontend_shapes_and_owner_field_names(house):
    hass, _ = house
    tok, key, _ = _member(hass)
    lv, iv = ma.PatrimonyMemberContactsView(hass), ma.PatrimonyMemberContactView(hass)
    r = run(lv.post(_MReq(tok, body=_seal(key, {"name": "Alex", "role": "Cleaner", "phone": "+1 555 0100",
                                                 "id": "forced"}))))
    c = _open(key, r)
    assert r.status == 201 and c["id"] != "forced" and len(c["id"]) >= 16
    assert c == {"id": c["id"], "function": "Cleaner", "name": "Alex", "tel": "+1 555 0100",
                 "method": "cellular", "app": "cellular"}
    r = run(lv.post(_MReq(tok, body=_seal(key, {"name": "Sam", "function": "Gardener", "tel": "+15550102", "app": "whatsapp"}))))
    c2 = _open(key, r)
    assert c2["method"] == "whatsapp"
    r = run(iv.patch(_MReq(tok, body=_seal(key, {"phone": "+15550103"})), c["id"]))
    p = _open(key, r)
    assert r.status == 200 and p["tel"] == "+15550103" and p["name"] == "Alex" and p["function"] == "Cleaner"
    r = run(iv.patch(_MReq(tok, body=_seal(key, {"app": "telegram"})), c["id"]))
    assert _open(key, r)["app"] == "telegram" and _open(key, r)["method"] == "cellular"
    owner = run(hh.PatrimonyContactsView(hass).get(_MReq(user=_Admin()))).json()
    member = _open(key, run(lv.get(_MReq(tok))))
    assert owner == member and [x["name"] for x in member["contacts"]] == ["Alex", "Sam"]
    assert run(iv.patch(_MReq(tok, body=_seal(key, {"name": "x"})), "nope")).status == 404
    r = run(iv.patch(_MReq(tok, body=_seal(key, {"name": "", "function": "", "tel": ""})), c["id"]))
    assert r.status == 400 and r.json()["error"]["code"] == "empty_contact"
    assert run(lv.post(_MReq(tok, body=_seal(key, {"name": " "})))).json()["error"]["code"] == "empty_contact"
    r = run(iv.delete(_MReq(tok), c["id"]))
    assert r.status == 200 and _open(key, r) == {"ok": True, "id": c["id"]}
    assert run(iv.delete(_MReq(tok), c["id"])).status == 404
    # Owner semantics: full ordered list replace.
    r = run(lv.put(_MReq(tok, body=_seal(key, {"contacts": [{"name": "A", "phone": "+1"}, {"id": c2["id"], "name": "Sam"}]}))))
    rows = _open(key, r)["contacts"]
    assert [x["name"] for x in rows] == ["A", "Sam"] and rows[1]["id"] == c2["id"] and rows[0]["tel"] == "+1"
    assert [(e["scope"], e["action"]) for e in ma.write_log(hass)] == [
        ("contacts", "add"), ("contacts", "add"), ("contacts", "edit"), ("contacts", "edit"),
        ("contacts", "delete"), ("contacts", "replace")]


def test_contacts_etag_304(house):
    hass, _ = house
    tok, key, _ = _member(hass)
    lv = ma.PatrimonyMemberContactsView(hass)
    r = run(lv.get(_MReq(tok)))
    tag = r.headers["ETag"]
    assert tag == etag_for(_open(key, r))
    assert run(lv.get(_MReq(tok, inm=tag))).status == 304
    run(lv.post(_MReq(tok, body=_seal(key, {"name": "N", "phone": "+1"}))))
    assert run(lv.get(_MReq(tok, inm=tag))).status == 200


# ------------------------------------------------------------------ notes

def test_notes_put_ifmatch_conflict_409_sealed_and_etag(house):
    hass, _ = house
    tok, key, _ = _member(hass)
    v = ma.PatrimonyMemberNotesView(hass)
    first = _open(key, run(v.get(_MReq(tok))))
    assert first["text"] == "" and first["updatedAt"] is None
    r = run(v.put(_MReq(tok, body=_seal(key, {"text": "Wifi on the router", "ifMatch": None}))))
    doc = _open(key, r)
    assert r.status == 200 and doc["text"] == "Wifi on the router" and doc["updatedAt"].endswith("Z")
    r = run(v.put(_MReq(tok, body=_seal(key, {"text": "stale", "ifMatch": "1999-01-01T00:00:00Z"}))))
    assert r.status == 409
    body = _open(key, r)
    assert body["error"]["code"] == "notes_conflict" and body["text"] == "Wifi on the router"
    assert body["updatedAt"] == doc["updatedAt"]
    r = run(v.put(_MReq(tok, body=_seal(key, {"text": "fresh", "ifMatch": doc["updatedAt"]}))))
    assert r.status == 200 and _open(key, r)["text"] == "fresh"
    # If-Match header with the GET ETag also works
    g = run(v.get(_MReq(tok)))
    r = run(v.put(_MReq(tok, body=_seal(key, {"text": "via etag"}), if_match=g.headers["ETag"])))
    assert r.status == 200
    assert run(v.get(_MReq(tok, inm=g.headers["ETag"]))).status == 200  # changed
    g2 = run(v.get(_MReq(tok)))
    assert run(v.get(_MReq(tok, inm=g2.headers["ETag"]))).status == 304
    r = run(v.put(_MReq(tok, body=_seal(key, {"text": 5}))))
    assert r.status == 400 and r.json()["error"]["code"] == "bad_text"
    assert run(hh.PatrimonyNotesView(hass).get(_MReq(user=_Admin()))).json()["text"] == "via etag"
    assert [(e["scope"], e["action"]) for e in ma.write_log(hass)] == [("notes", "set")] * 3


# ------------------------------------------------------------------ chat

def test_member_chat_post_sealed_owner_sees_it_push_and_limits(house):
    hass, _ = house
    tok, key, mem = _member(hass)
    v = hh.PatrimonyMemberChatView(hass)
    r = run(v.post(_MReq(tok, body=_seal(key, {"text": "Arriving at 5", "retention": "1d",
                                                "deviceName": "Spoofed", "senderDevice": "devhash"}))))
    msg = _open(key, r)
    assert r.status == 201 and msg["text"] == "Arriving at 5" and msg["sender"] == "Anna"
    assert msg["push"] == {"ok": True, "status": 202}  # 0.4.110 S7, inside the PQ seal
    assert "push" not in r.json()  # nothing outside the sealed envelope
    assert msg["senderKind"] == "member" and msg["canDelete"] is False and msg["expiresAt"]
    cid = hh.house_members.client_id_for(mem["memberId"])
    assert hass.pushes == [("devhash", cid)]
    owner = run(hchat.PatrimonyChatView(hass).get(_MReq(user=_Admin()))).json()["messages"]
    assert [(m["text"], m["sender"], m["canDelete"]) for m in owner] == [("Arriving at 5", "Anna", True)]
    raw = hchat.chat_path(hass).read_text()
    assert "Arriving at 5" not in raw  # encrypted at rest like owner rows
    listed = _open(key, run(v.get(_MReq(tok))))["messages"]
    assert listed[0]["text"] == "Arriving at 5" and listed[0]["canDelete"] is False
    img = base64.b64encode(JPEG).decode()
    r = run(v.post(_MReq(tok, body=_seal(key, {"imageBase64": img, "imageContentType": "image/jpeg"}))))
    assert r.status == 201 and _open(key, r)["hasImage"] is True
    for body, code in (({"text": "x" * 2001}, "too_long"), ({"text": ""}, "empty"),
                       ({"text": "a", "retention": "2y"}, "bad_retention"),
                       ({"imageBase64": "!!", "imageContentType": "image/jpeg"}, "bad_image")):
        r = run(v.post(_MReq(tok, body=_seal(key, body))))
        assert r.status == 400 and r.json()["error"]["code"] == code
    assert len(hass.pushes) == 2
    assert [(e["scope"], e["action"]) for e in ma.write_log(hass)] == [("chat", "post"), ("chat", "post_image")]


def test_member_chat_mine_sealed_per_member_and_owner(house):
    """0.4.106: sealed /m/chat GET carries mine on the member's own rows only; owner list on its own."""
    hass, _ = house
    tok_a, key_a, mem_a = _member(hass, memberName="A", principalId="prn_a")
    tok_b, key_b, mem_b = _member(hass, memberName="B", principalId="prn_b")
    v = hh.PatrimonyMemberChatView(hass)
    assert run(v.post(_MReq(tok_a, body=_seal(key_a, {"text": "from-a"})))).status == 201
    assert run(v.post(_MReq(tok_b, body=_seal(key_b, {"text": "from-b"})))).status == 201
    hc = hh.house_clients
    hc.upsert_client(hass, "owner-rt-1", device_name="Owner Phone", touch_access=True)
    from tests.test_members import _owner_jwt

    owner_req = _MReq(_owner_jwt("owner-rt-1"), user=_Admin(), body={"text": "from-owner"})
    assert run(hchat.PatrimonyChatView(hass).post(owner_req)).status == 201
    list_a = _open(key_a, run(v.get(_MReq(tok_a))))["messages"]
    list_b = _open(key_b, run(v.get(_MReq(tok_b))))["messages"]
    owner = run(hchat.PatrimonyChatView(hass).get(_MReq(_owner_jwt("owner-rt-1"), user=_Admin()))).json()["messages"]
    assert [(m["text"], m["mine"]) for m in list_a] == [("from-a", True), ("from-b", False), ("from-owner", False)]
    assert [(m["text"], m["mine"]) for m in list_b] == [("from-a", False), ("from-b", True), ("from-owner", False)]
    assert [(m["text"], m["mine"]) for m in owner] == [("from-a", False), ("from-b", False), ("from-owner", True)]
    assert [m["senderKind"] for m in list_a] == ["member", "member", "ios"]
    blob = repr(list_a) + repr(owner)
    assert mem_a["memberId"] not in blob and mem_b["memberId"] not in blob and "owner-rt-1" not in blob


def test_member_owner_session_on_pairing_and_state(house):
    """0.4.107: ownerSession on /m/pairing and the /m/state pairing mirrors the owner's capability.session."""
    hass, _ = house
    tok, key, _m = _member(hass)
    mgr = pqs.get_manager(hass)
    for owner_key, want in ((None, "absent"), (b"k" * 32, "present")):
        mgr._aead_key = owner_key
        assert mgr.capability()["session"] == want
        pairing = run(hh.PatrimonyMemberPairingView(hass).get(_MReq(tok))).json()
        assert pairing["ownerSession"] == want and pairing["pqShield"]["ownerSession"] == want
        assert pairing["pqShield"]["session"] == "present"  # the member's own session is separate
        state = run(hh.PatrimonyMemberStateView(hass).get(_MReq(tok))).json()
        assert {"pqShield", "ct"} <= set(state)  # still sealed
        assert state["pairing"]["ownerSession"] == want


def test_member_chat_post_428_and_plain_rejected(house):
    hass, _ = house
    tok = _mint(hass)[1]["token"]
    v = hh.PatrimonyMemberChatView(hass)
    assert run(v.post(_MReq(tok, body={"text": "x"}))).status == 428
    key = _hand(hass, tok)
    r = run(v.post(_MReq(tok, body={"text": "x"})))
    assert r.status == 400 and r.json()["error"]["code"] == "pq_envelope_required"
    assert hass.pushes == []


# ------------------------------------------------------------------ write log (owner)

def test_write_log_owner_only_and_content_free(house):
    hass, _ = house
    tok, key, mem = _member(hass)
    run(ma.PatrimonyMemberNotesView(hass).put(_MReq(tok, body=_seal(key, {"text": "SECRET-NOTE"}))))
    run(ma.PatrimonyMemberContactsView(hass).post(_MReq(tok, body=_seal(key, {"name": "SECRET-NAME", "phone": "+15550104"}))))
    r = run(ma.PatrimonyMemberWriteLogView(hass).get(_MReq(user=_Admin())))
    text = r.text
    assert r.status == 200 and "SECRET" not in text and "+15550104" not in text and tok not in text
    rows = r.json()["memberWriteLog"]
    assert [x["scope"] for x in rows] == ["contacts", "notes"]  # newest first
    assert set(rows[0]) == {"at", "memberId", "clientId", "scope", "action"}
    assert run(ma.PatrimonyMemberWriteLogView(hass).get(_MReq(user=_NonAdmin()))).status == 403


def test_member_write_logs_never_carry_content(house, caplog):
    import logging

    hass, _ = house
    tok, key, _ = _member(hass)
    with caplog.at_level(logging.DEBUG):
        run(ma.PatrimonyMemberNotesView(hass).put(_MReq(tok, body=_seal(key, {"text": "SECRET-NOTE"}))))
        run(hh.PatrimonyMemberChatView(hass).post(_MReq(tok, body=_seal(key, {"text": "SECRET-CHAT"}))))
    assert "SECRET" not in caplog.text and tok not in caplog.text
    assert "patrimony_member_write scope=notes action=set" in caplog.text


# ------------------------------------------------------------------ 0.4.103 chat images

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def test_member_chat_image_roundtrip_sealed_etag_and_owner_parity(house):
    hass, _ = house
    tok, key, _ = _member(hass)
    v, iv = hh.PatrimonyMemberChatView(hass), ma.PatrimonyMemberChatImageView(hass)
    r = run(v.post(_MReq(tok, body=_seal(key, {"text": "", "imageBase64": base64.b64encode(PNG).decode(),
                                                "imageContentType": "image/png"}))))
    msg = _open(key, r)
    assert r.status == 201 and msg["hasImage"] is True and msg["imageContentType"] == "image/png"
    listed = _open(key, run(v.get(_MReq(tok))))["messages"]
    owner_rows = run(hchat.PatrimonyChatView(hass).get(_MReq(user=_Admin()))).json()["messages"]
    m_row = next(x for x in listed if x["id"] == msg["id"])
    o_row = next(x for x in owner_rows if x["id"] == msg["id"])
    assert (m_row["hasImage"], m_row["imageContentType"]) == (o_row["hasImage"], o_row["imageContentType"]) == (True, "image/png")
    r = run(iv.get(_MReq(tok), msg["id"]))
    doc = _open(key, r)
    assert r.status == 200 and doc == {"messageId": msg["id"], "contentType": "image/png",
                                       "data": base64.b64encode(PNG).decode()}
    assert r.headers["ETag"] == etag_for(doc)
    r304 = run(iv.get(_MReq(tok, inm=r.headers["ETag"]), msg["id"]))
    assert r304.status == 304 and r304.text == ""
    blob, status, ctype = hchat.read_chat_image(hass, _MReq(user=_Admin()), msg["id"])
    assert status == 200 and blob == PNG and ctype == "image/png"
    assert PNG not in hchat.chat_path(hass).read_bytes()  # encrypted at rest


def test_member_chat_image_owner_posted_visible_to_member(house):
    hass, _ = house
    tok, key, _ = _member(hass)
    body, status = hchat.create_message(hass, _MReq(user=_Admin()), {"imageBase64": base64.b64encode(JPEG).decode(),
                                                                     "imageContentType": "image/jpeg"})
    assert status == 201
    doc = _open(key, run(ma.PatrimonyMemberChatImageView(hass).get(_MReq(tok), body["id"])))
    assert base64.b64decode(doc["data"]) == JPEG and doc["contentType"] == "image/jpeg"


def test_member_chat_image_404s_and_delete_cascade(house):
    hass, _ = house
    tok, key, _ = _member(hass)
    iv = ma.PatrimonyMemberChatImageView(hass)
    r = run(iv.get(_MReq(tok), "nope"))
    assert r.status == 404 and r.json()["error"]["code"] == "not_found"
    text_only = _open(key, run(hh.PatrimonyMemberChatView(hass).post(_MReq(tok, body=_seal(key, {"text": "hi"})))))
    assert run(iv.get(_MReq(tok), text_only["id"])).status == 404
    img = _open(key, run(hh.PatrimonyMemberChatView(hass).post(_MReq(tok, body=_seal(key, {
        "imageBase64": base64.b64encode(JPEG).decode(), "imageContentType": "image/jpeg"})))))
    assert run(iv.get(_MReq(tok), img["id"])).status == 200
    assert hchat.delete_message(hass, _MReq(user=_Admin()), img["id"])[1] == 200  # owner delete removes the image
    assert run(iv.get(_MReq(tok), img["id"])).status == 404


def test_member_chat_image_expiry_cascade(house):
    from datetime import datetime, timedelta, timezone

    hass, _ = house
    tok, key, _ = _member(hass)
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    body, status = hchat.create_member_message(hass, {"memberId": "x", "memberName": "A"}, "m_x",
                                               {"imageBase64": base64.b64encode(JPEG).decode(),
                                                "imageContentType": "image/jpeg", "retention": "1h"}, now=past)
    assert status == 201
    assert run(ma.PatrimonyMemberChatImageView(hass).get(_MReq(tok), body["id"])).status == 404


def test_member_chat_image_limits_match_owner(house, monkeypatch):
    hass, _ = house
    tok, key, _ = _member(hass)
    monkeypatch.setattr(hchat, "CHAT_IMAGE_MAX", 16)
    v = hh.PatrimonyMemberChatView(hass)
    for body, code in (({"imageBase64": base64.b64encode(JPEG).decode(), "imageContentType": "image/jpeg"}, "too_large"),
                       ({"imageBase64": base64.b64encode(b"x").decode(), "imageContentType": "image/gif"}, "bad_image"),
                       ({"imageBase64": "data:image/jpeg;base64,AAAA", "imageContentType": "image/jpeg"}, "bad_image")):
        r = run(v.post(_MReq(tok, body=_seal(key, body))))
        assert r.status == 400 and r.json()["error"]["code"] == code
        owner = hchat.create_message(hass, _MReq(user=_Admin()), body)
        assert owner[1] == 400 and owner[0]["error"]["code"] == code
