"""0.4.108 member-path PQ hardening: the house pq_shield flag governs the OWNER path only.

Members are gated on ML-KEM available AND member_pq_required, independent of the flag.
Flag Off keeps the recipient keypair + member sessions while member PQ is required (the owner
session is wiped as before). Transition (member_pq_required False) and flag On: unchanged.
Runs without ML-KEM (state forced, seal faked); real-crypto variants live in test_member_pq /
test_member_api.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import member_api as ma
from custom_components.patrimony_collection.const import CONF_MEMBER_PQ_REQUIRED, CONF_PQ_SHIELD
from custom_components.patrimony_collection.pq_shield import member_views as mv
from custom_components.patrimony_collection.pq_shield import session as pqs
from custom_components.patrimony_collection.pq_shield import views as pv

from tests.test_members import _Req, _fake_web  # noqa: F401  (autouse fixture)


def run(c):
    return asyncio.run(c)


class _Recipient:
    def private_raw(self):
        return b"r" * 32, b"x" * 32

    def public_material(self):
        return {"publicKey": "pub"}


class _Env:
    def __init__(self, key, doc):
        self.key, self.doc = key, doc

    def to_dict(self):
        return {"pqShield": 1, "kem": "k", "aead": "a", "nonce": "n", "ct": "sealed:" + self.key[:1].decode()}


@pytest.fixture
def pq(tmp_path, monkeypatch):
    """Manager with ML-KEM 'available', flag and member_pq_required switchable via opts."""
    opts: dict = {}
    entry = types.SimpleNamespace(data={}, options=opts)
    hass = types.SimpleNamespace(
        data={}, config=types.SimpleNamespace(path=lambda p: str(tmp_path / p)),
        config_entries=types.SimpleNamespace(async_entries=lambda _d: [entry]),
    )
    (tmp_path / "patrimony_collection").mkdir()
    monkeypatch.setattr(hh, "_first_entry", lambda _h: entry)
    monkeypatch.setattr(pqs, "seal_document", lambda key, doc: _Env(key, doc))
    mgr = pqs.PQShieldManager(hass)
    mgr._available, mgr._probe_reason = True, None
    mgr._loaded = True
    mgr._recipient = _Recipient()
    mgr._aead_key = b"o" * 32
    mgr._member_keys = {"mem_s": b"s" * 32}
    monkeypatch.setattr(pqs.PQShieldManager, "load", lambda self: None)
    monkeypatch.setattr(pqs, "get_manager", lambda _h: mgr)
    monkeypatch.setattr(mv, "get_manager", lambda _h: mgr)
    monkeypatch.setattr(pv, "get_manager", lambda _h: mgr)
    return hass, opts, mgr


def _flag(opts, on):
    opts[CONF_PQ_SHIELD] = on


# ---- gate ------------------------------------------------------------------------------

def test_default_flag_still_true_when_key_absent(pq):
    _hass, opts, mgr = pq
    assert CONF_PQ_SHIELD not in opts and mgr.feature_enabled is True  # default unchanged


@pytest.mark.parametrize("flag", [True, False])
def test_member_gate_on_when_required_regardless_of_flag(pq, flag):
    _hass, opts, mgr = pq
    _flag(opts, flag)
    assert mgr.member_pq_required is True
    assert mgr.member_shield_on is True
    assert mgr.shield_on is flag  # owner path still follows the flag


def test_member_gate_off_when_ml_kem_unavailable(pq):
    _hass, opts, mgr = pq
    _flag(opts, False)
    mgr._available = False
    assert mgr.member_shield_on is False


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("available", [True, False])
def test_transition_member_gate_equals_old_rule(pq, flag, available):
    _hass, opts, mgr = pq
    _flag(opts, flag)
    opts[CONF_MEMBER_PQ_REQUIRED] = False
    mgr._available = available
    assert mgr.member_shield_on is mgr.shield_on is (flag and available)


# ---- flag Off + member PQ required ---------------------------------------------------------

def test_flag_off_sessionless_member_428_everywhere(pq):
    hass, opts, mgr = pq
    _flag(opts, False)
    row = {"memberId": "mem_none"}
    assert mgr.wrap_for_member("mem_none", {"a": 1}) is None
    r = ma._pq_gate(hass, row)
    assert r.status == 428 and r.json()["error"]["code"] == "pq_session_required"
    r = run(hh._member_pq_response(hass, row, {"a": 1}))
    assert r.status == 428 and r.json()["error"]["code"] == "pq_session_required"
    r = run(ma.sealed(hass, row, {"a": 1}))
    assert r.status == 428


def test_flag_off_member_with_session_is_wrapped(pq):
    hass, opts, mgr = pq
    _flag(opts, False)
    env = mgr.wrap_for_member("mem_s", {"a": 1})
    assert env["ct"] == "sealed:s"  # THIS member's key, never the owner's
    r = run(hh._member_pq_response(hass, {"memberId": "mem_s"}, {"a": 1, "pairing": {"role": "member"}}))
    assert r.status == 200 and r.json()["ct"] == "sealed:s" and r.json()["pairing"] == {"role": "member"}
    assert ma._pq_gate(hass, {"memberId": "mem_s"}) is None


def test_flag_off_member_capability_and_owner_session(pq):
    _hass, opts, mgr = pq
    _flag(opts, False)
    cap = mgr.member_capability("mem_s")
    assert cap["session"] == "present" and cap["reason"] is None
    assert cap["ownerSession"] == "absent" == mgr.capability()["session"]  # owner path Off
    assert mgr.member_capability("mem_none")["session"] == "absent"


def test_flag_off_pairing_body_still_requires_member_session(pq, monkeypatch):
    hass, opts, mgr = pq
    _flag(opts, False)
    monkeypatch.setattr(hh.house_clients, "pairing_document", lambda _h, _r: {"schemaVersion": 1})
    row = {"memberId": "mem_none", "memberName": "A", "principalId": "p", "inviteId": "i"}
    monkeypatch.setattr(hh.house_members, "public_row", lambda r: {"expiresAt": None})
    body = hh._member_pairing_body(hass, None, row)
    assert body["pqSessionRequired"] is True and body["ownerSession"] == "absent"


def test_flag_off_member_handshake_gate_open(pq):
    _hass, opts, mgr = pq
    _flag(opts, False)
    assert mv._gate(mgr) is None
    opts[CONF_MEMBER_PQ_REQUIRED] = False
    assert mv._gate(mgr).status == 409  # transition: as before


def test_flag_off_accept_member_encap_not_refused_as_disabled(pq, monkeypatch):
    _hass, opts, mgr = pq
    _flag(opts, False)
    with pytest.raises(ValueError, match="bad_encap"):  # gets past the disabled gate
        mgr.accept_member_encap("mem_x", b"short")
    opts[CONF_MEMBER_PQ_REQUIRED] = False
    with pytest.raises(RuntimeError, match="disabled"):
        mgr.accept_member_encap("mem_x", b"short")


# ---- flag Off keeps keypair + member sessions --------------------------------------------

def _disk(hass):
    return json.loads(open(pqs._store_path(hass)).read())


def test_flag_off_keeps_keypair_and_member_sessions(pq):
    hass, opts, mgr = pq
    _flag(opts, False)
    mgr.flag_off()
    assert mgr._recipient is not None and mgr._member_keys == {"mem_s": b"s" * 32}
    assert mgr._aead_key is None  # owner session follows today's behaviour
    on_disk = _disk(hass)
    assert {"mlkemPrivate", "x25519Private", "memberSessions"} <= set(on_disk) and "aeadKey" not in on_disk
    assert len(on_disk["memberSessions"]) == 1


def test_feature_post_off_keeps_member_pq(pq, monkeypatch):
    hass, opts, mgr = pq
    _flag(opts, True)
    entry = hh._first_entry(hass)
    hass.config_entries.async_update_entry = lambda e, options: e.options.update(options)
    monkeypatch.setattr(pv, "_first_entry", lambda _h: entry)
    r = run(pv.PQShieldFeatureView(hass).post(_Req(body={"enabled": False})))
    assert r.status == 200 and r.json()["enabled"] is False  # 'off' is not refused
    assert opts[CONF_PQ_SHIELD] is False
    assert mgr._recipient is not None and mgr._member_keys and mgr._aead_key is None
    assert mgr.wrap_for_member("mem_s", {"a": 1})["ct"] == "sealed:s"


def test_options_listener_off_keeps_member_pq(pq, monkeypatch):
    import custom_components.patrimony_collection as integ

    hass, opts, mgr = pq
    _flag(opts, False)
    entry = hh._first_entry(hass)
    called = {}

    async def _emit(*_a):
        called["emit"] = True

    monkeypatch.setattr(integ, "_bind_mapped_listener", lambda *_a: None)
    monkeypatch.setattr(integ, "_emit_snapshot", _emit)
    hass.data[integ.DOMAIN] = {"e1": {}}
    entry.entry_id = "e1"
    run(integ._options_updated(hass, entry))
    assert called.get("emit") and mgr._recipient is not None and mgr._member_keys and mgr._aead_key is None


def test_transition_flag_off_full_wipe_as_before(pq):
    hass, opts, mgr = pq
    _flag(opts, False)
    opts[CONF_MEMBER_PQ_REQUIRED] = False
    mgr.flag_off()
    assert mgr._recipient is None and mgr._member_keys == {} and mgr._aead_key is None
    assert _disk(hass) == {"version": pqs.STORE_VERSION}


# ---- flag On: as today ---------------------------------------------------------------

def test_flag_on_as_today(pq):
    hass, opts, mgr = pq
    _flag(opts, True)
    assert mgr.wrap_for_member("mem_none", {"a": 1}) is None
    assert ma._pq_gate(hass, {"memberId": "mem_none"}).status == 428
    assert mgr.wrap_for_member("mem_s", {"a": 1})["ct"] == "sealed:s"
    cap = mgr.member_capability("mem_s")
    assert cap["session"] == "present" and cap["ownerSession"] == "present" == mgr.capability()["session"]


def test_transition_flag_on_sessionless_plain_as_today(pq):
    hass, opts, mgr = pq
    _flag(opts, True)
    opts[CONF_MEMBER_PQ_REQUIRED] = False
    r = run(hh._member_pq_response(hass, {"memberId": "mem_none"}, {"a": 1}))
    assert r.status == 200 and r.json() == {"a": 1}
    assert ma._pq_gate(hass, {"memberId": "mem_none"}) is None
