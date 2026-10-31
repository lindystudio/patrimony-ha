"""0.4.107 bug 4 (PQ chip): members see exactly the owner's PQ session state as ownerSession.

Frozen rule: ownerSession == the owner's capability.session (same check). Only present/absent;
no client ids, no timestamps. Additive, no schema bump. Runs without ML-KEM (state is forced).
"""

from __future__ import annotations

import types

import pytest

from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection.pq_shield import session as pqs

BASE_KEYS = {"pqShield", "available", "kem", "aead", "session", "reason"}


def _mgr(tmp_path, monkeypatch, *, available=True, enabled=True, owner=False, member=False):
    hass = types.SimpleNamespace(
        data={}, config=types.SimpleNamespace(path=lambda p: str(tmp_path / p)),
        config_entries=types.SimpleNamespace(async_entries=lambda _d: []),
    )
    mgr = pqs.PQShieldManager(hass)
    mgr._available, mgr._probe_reason = available, None if available else "crypto_below_floor"
    mgr._loaded = True
    monkeypatch.setattr(pqs.PQShieldManager, "feature_enabled", property(lambda self: enabled))
    mgr._aead_key = b"o" * 32 if owner else None
    if member:
        mgr._member_keys["mem_a"] = b"m" * 32
    monkeypatch.setattr(pqs.PQShieldManager, "load", lambda self: None)
    return mgr


@pytest.mark.parametrize("owner", [True, False])
@pytest.mark.parametrize("member", [True, False])
def test_owner_session_mirrors_owner_capability_session(tmp_path, monkeypatch, owner, member):
    mgr = _mgr(tmp_path, monkeypatch, owner=owner, member=member)
    cap = mgr.member_capability("mem_a")
    assert cap["ownerSession"] == mgr.capability()["session"] == ("present" if owner else "absent")
    assert cap["session"] == ("present" if member else "absent")  # member's own session unchanged
    assert set(cap) == BASE_KEYS | {"ownerSession"}  # additive only, nothing else about the owner
    assert set(mgr.capability()) == BASE_KEYS  # owner capability shape untouched


@pytest.mark.parametrize("available,enabled", [(False, True), (True, False)])
def test_owner_session_absent_when_shield_unavailable_or_off(tmp_path, monkeypatch, available, enabled):
    mgr = _mgr(tmp_path, monkeypatch, available=available, enabled=enabled, owner=True)
    assert mgr.capability()["session"] == "absent"
    assert mgr.member_capability("mem_a")["ownerSession"] == "absent"


@pytest.mark.parametrize("owner", [True, False])
def test_member_pairing_body_carries_owner_session_only(tmp_path, monkeypatch, owner):
    mgr = _mgr(tmp_path, monkeypatch, owner=owner)
    monkeypatch.setattr(hh.house_clients, "pairing_document", lambda _h, _r: {"schemaVersion": 1})
    monkeypatch.setattr(pqs, "get_manager", lambda _h: mgr)
    monkeypatch.setattr(hh, "member_pq_required", lambda _h: True)
    row = {"memberId": "mem_a", "memberName": "A", "principalId": "prn_a", "inviteId": "inv"}
    body = hh._member_pairing_body(mgr.hass, None, row)
    want = "present" if owner else "absent"
    assert body["ownerSession"] == want and body["pqShield"]["ownerSession"] == want
    blob = repr(body)
    assert "clientId" not in blob and "At" not in repr(body["pqShield"])  # no owner ids/timestamps
