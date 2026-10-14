"""0.4.98 member PQ shield: per-member session, owner envelope shape, 428 transition, revoke wipes."""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection.const import CONF_MEMBER_PQ_REQUIRED, DOMAIN
from custom_components.patrimony_collection.pq_shield import hybrid as hy
from custom_components.patrimony_collection.pq_shield import member_views as mv
from custom_components.patrimony_collection.pq_shield import session as pqs

from tests.test_members import _Admin, _Hass, _Req, _mint, _revoke, _fake_web  # noqa: F401

ok, _why = pqs.probe_mlkem()
pytestmark = pytest.mark.skipif(not ok, reason="ML-KEM unavailable")

DOC = {"schemaVersion": 1, "generatedAt": "2026-10-13T00:00:00Z", "property": {"id": "prop-1"}, "cards": []}


def run(c):
    return asyncio.run(c)


@pytest.fixture
def house(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    opts = {}
    entry = types.SimpleNamespace(data={}, options=opts)
    monkeypatch.setattr(hh, "_first_entry", lambda _h: entry)
    monkeypatch.setattr(mv, "_require_configured", lambda _h: None)
    monkeypatch.setattr(hh, "build_presentation_document", lambda *_a: dict(DOC))
    monkeypatch.setattr(pqs.PQShieldManager, "feature_enabled", property(lambda self: True))
    run(pqs.async_setup_pq_shield(hass))
    return hass, opts


def _handshake(hass, token):
    pub = run(mv.MemberPQPublicView(hass).get(_Req(token)))
    assert pub.status == 200
    material = pub.json()
    assert material["kem"] == "xwing-mlkem768-x25519" and material["aead"] == "aes-256-gcm"
    ss, encap = hy.encapsulate_to(hy.b64d(material["publicKey"]))
    r = run(mv.MemberPQEncapView(hass).post(_Req(token, body={"encap": hy.b64e(encap)})))
    assert r.status == 200 and r.json() == {"ok": True, "session": "present"}
    return hy.derive_aead_key(ss)


def test_member_encap_then_wrapped_state_and_chat_decrypt(house):
    hass, _ = house
    tok = _mint(hass)[1]["token"]
    key = _handshake(hass, tok)
    st = run(hh.PatrimonyMemberStateView(hass).get(_Req(tok)))
    env = st.json()
    assert st.status == 200 and {"pqShield", "kem", "aead", "nonce", "ct"} <= set(env)
    assert "schemaVersion" not in env and env["pairing"]["role"] == "member"
    doc = hy.open_document(key, env)
    assert doc["schemaVersion"] == 1 and doc["property"] == {"id": "prop-1"} and "generatedAt" in doc
    ch = run(hh.PatrimonyMemberChatView(hass).get(_Req(tok)))
    assert "messages" in hy.open_document(key, ch.json())
    pr = run(hh.PatrimonyMemberPairingView(hass).get(_Req(tok))).json()
    assert "ct" not in pr and pr["pqShield"]["session"] == "present" and pr["pqSessionRequired"] is True


def test_no_session_428_when_required_plain_when_not(house):
    hass, opts = house
    tok = _mint(hass)[1]["token"]
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(tok)))
    assert r.status == 428 and r.json()["error"]["code"] == "pq_session_required"
    assert r.json()["pqShield"]["session"] == "absent"
    assert run(hh.PatrimonyMemberChatView(hass).get(_Req(tok))).status == 428
    pr = run(hh.PatrimonyMemberPairingView(hass).get(_Req(tok)))
    assert pr.status == 200 and pr.json()["pqShield"]["session"] == "absent"
    opts[CONF_MEMBER_PQ_REQUIRED] = False
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(tok)))
    assert r.status == 200 and r.json()["schemaVersion"] == 1
    assert run(hh.PatrimonyMemberPairingView(hass).get(_Req(tok))).json()["pqSessionRequired"] is False


def test_revoked_member_401_and_session_deleted(house):
    hass, _ = house
    status, doc = _mint(hass)
    tok, mid = doc["token"], doc["member"]["memberId"]
    _handshake(hass, tok)
    mgr = pqs.get_manager(hass)
    assert mgr.member_session_present(mid)
    assert _revoke(hass, "prn_anna")[0] == 200
    assert not mgr.member_session_present(mid)
    disk = json.loads(pqs._store_path(hass).read_text())
    assert mid not in (disk.get("memberSessions") or {})
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(tok)))
    assert r.status == 401 and r.json()["error"]["code"] == "member_token_revoked"
    assert run(mv.MemberPQEncapView(hass).post(_Req(tok, body={"encap": "x"}))).status == 401


def test_owner_session_unaffected_by_member_sessions(house):
    hass, _ = house
    mgr = pqs.get_manager(hass)
    ss, encap = hy.encapsulate_to(hy.b64d(mgr.public_material()["publicKey"]))
    mgr.accept_encap(encap)
    owner_key = mgr.session_aead_key_for_tests()
    tok = _mint(hass)[1]["token"]
    mkey = _handshake(hass, tok)
    assert mkey != owner_key and mgr.session_aead_key_for_tests() == owner_key
    assert hy.open_document(owner_key, mgr.maybe_wrap_document(DOC)) == DOC
    run(mv.MemberPQRotateView(hass).post(_Req(tok)))
    assert mgr.session_aead_key_for_tests() == owner_key
    _revoke(hass, "prn_anna")
    assert mgr.session_aead_key_for_tests() == owner_key
    reloaded = pqs._build_manager(hass)
    assert reloaded.session_aead_key_for_tests() == owner_key


def test_member_a_cannot_use_member_b_session(house):
    hass, _ = house
    a = _mint(hass, memberName="A", principalId="prn_a")[1]
    b = _mint(hass, memberName="B", principalId="prn_b")[1]
    key_b = _handshake(hass, b["token"])
    ra = run(hh.PatrimonyMemberStateView(hass).get(_Req(a["token"])))
    assert ra.status == 428
    key_a = _handshake(hass, a["token"])
    env_a = run(hh.PatrimonyMemberStateView(hass).get(_Req(a["token"]))).json()
    with pytest.raises(Exception):
        hy.open_document(key_b, env_a)
    assert hy.open_document(key_a, env_a)["schemaVersion"] == 1


def test_member_rotate_drops_only_own_session(house):
    hass, _ = house
    tok = _mint(hass)[1]["token"]
    _handshake(hass, tok)
    r = run(mv.MemberPQRotateView(hass).post(_Req(tok)))
    assert r.status == 200 and "publicKey" in r.json()
    assert run(hh.PatrimonyMemberStateView(hass).get(_Req(tok))).status == 428


def test_shield_off_members_plain(house, monkeypatch):
    hass, _ = house
    monkeypatch.setattr(pqs.PQShieldManager, "feature_enabled", property(lambda self: False))
    tok = _mint(hass)[1]["token"]
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(tok)))
    assert r.status == 200 and r.json()["schemaVersion"] == 1
    assert run(mv.MemberPQPublicView(hass).get(_Req(tok))).status == 409


def test_chat_key_cached_after_first_load(tmp_path, monkeypatch):
    from pathlib import Path

    from custom_components.patrimony_collection import chat

    hass = _Hass(tmp_path)
    run(chat.async_warm_chat_key(hass))
    k = chat.key_path(hass)
    assert len(k.read_bytes()) == 32 and (k.stat().st_mode & 0o777) == 0o600
    first = chat.load_chat_key(hass)

    def boom(*_a, **_k):
        raise AssertionError("disk read after warm")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert chat.load_chat_key(hass) == first
    assert not [p for p in k.parent.iterdir() if ".tmp" in p.name]
