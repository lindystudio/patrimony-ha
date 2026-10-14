"""0.4.97/0.4.98: members are never wrapped with the OWNER session; sessionless members get plain when member_pq_required is False; owner /state still wrapped. pq_shield store I/O off-loop."""

from __future__ import annotations

import asyncio
import os
import stat
import types

from custom_components.patrimony_collection import clients as hc
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection.const import DOMAIN
from custom_components.patrimony_collection.pq_shield import session as pqs

from tests.test_members import _Hass, _Req, _mint, _fake_web  # noqa: F401  (autouse fixture)

DOC = {"schemaVersion": 1, "generatedAt": "2026-10-13T00:00:00Z", "property": {"id": "prop-1"}, "cards": []}


class _OnManager(pqs.PQShieldManager):
    """PQ Shield ON + session Present, without needing ML-KEM on the test box."""

    def __init__(self, hass):  # noqa: D401 - bypass probe
        self.hass = hass
        self._recipient = None
        self._aead_key = b"k" * 32
        self._available, self._probe_reason = True, None
        self._loaded = True
        self._member_keys = {}
        self.wrapped = 0

    @property
    def feature_enabled(self):
        return True

    def maybe_wrap_document(self, document):
        self.wrapped += 1
        return {"aead": "x", "ct": "c", "kem": "k", "nonce": "n"}


def _shield_on(hass, monkeypatch):
    mgr = _OnManager(hass)
    hass.data.setdefault(DOMAIN, {})[pqs.RUNTIME_KEY] = mgr
    monkeypatch.setattr(hh, "build_presentation_document", lambda *_a: dict(DOC))
    return mgr


def test_member_state_plain_with_pq_shield_on(tmp_path, monkeypatch):
    """Sessionless member, transition mode (member_pq_required False): plain, never owner-wrapped."""
    hass = _Hass(tmp_path)
    mgr = _shield_on(hass, monkeypatch)
    monkeypatch.setattr(hh, "_first_entry", lambda _h: types.SimpleNamespace(data={}, options={"member_pq_required": False}))
    token = _mint(hass)[1]["token"]
    r = hh.PatrimonyMemberStateView(hass).get(_Req(token))
    r = asyncio.run(r)
    body = r.json()
    assert r.status == 200
    for k in ("schemaVersion", "generatedAt", "property"):
        assert k in body
    assert not {"aead", "ct", "kem"} & set(body)
    assert body["pairing"]["role"] == "member"
    for view in (hh.PatrimonyMemberPairingView, hh.PatrimonyMemberChatView):
        b = asyncio.run(view(hass).get(_Req(token))).json()
        assert not {"aead", "ct", "kem"} & set(b)
    assert mgr.wrapped == 0


def test_owner_state_still_wrapped_with_pq_shield_on(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    mgr = _shield_on(hass, monkeypatch)
    monkeypatch.setattr(hc, "pairing_document", lambda _h, _r: {"status": "active", "clientId": "c1"})
    r = asyncio.run(hh.PatrimonyStateView(hass).get(_Req(user=types.SimpleNamespace(is_admin=True, name="o"))))
    body = r.json()
    assert r.status == 200 and {"aead", "ct", "kem"} <= set(body)
    assert "schemaVersion" not in body and body["pairing"]["status"] == "active"
    assert mgr.wrapped == 1


def test_pq_store_loaded_via_executor_and_atomic_private_write(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    calls = []
    orig = hass.async_add_executor_job

    async def spy(fn, *a):
        calls.append(fn)
        return await orig(fn, *a)

    hass.async_add_executor_job = spy
    asyncio.run(pqs.async_setup_pq_shield(hass))
    assert pqs._build_manager in calls
    mgr = hass.data[DOMAIN][pqs.RUNTIME_KEY]
    assert pqs.get_manager(hass) is mgr
    mgr._write_disk({"version": 1})
    path = pqs._store_path(hass)
    assert path.read_text().strip() == '{"version": 1}'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir() if ".tmp" in p.name] == []
