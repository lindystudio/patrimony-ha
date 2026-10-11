"""eventKeyLastPushStatus / eventKeyInstalledAt on GET /pairing and /state pairing."""
from __future__ import annotations

import asyncio
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from custom_components.patrimony_collection import clients as clients_mod
from custom_components.patrimony_collection import http as http_mod
from custom_components.patrimony_collection import notify as notify_mod

from tests.test_clients import _Hass, _phone

KEY = "hek_test_not_real"


class _JsonResp:
    def __init__(self, data, status=200):
        self.status = status
        self.body = json.dumps(data).encode()


@pytest.fixture(autouse=True)
def _aiohttp_web(monkeypatch):
    """Use a tiny json_response stub so the views run without aiohttp installed."""
    import sys
    import types

    web = types.SimpleNamespace(json_response=lambda data, status=200: _JsonResp(data, status))
    mod = types.ModuleType("aiohttp")
    mod.web = web
    monkeypatch.setitem(sys.modules, "aiohttp", mod)
    monkeypatch.setitem(sys.modules, "aiohttp.web", web)
PID = "00000000-0000-4000-8000-000000000001"


class _Resp:
    def __init__(self, status=202):
        self.status = status

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body=b""):
    return HTTPError("https://x/events", code, "err", {}, io.BytesIO(body))


def _post(monkeypatch, hass, behaviour, kind="manual"):
    def fake(req, timeout=8):
        if isinstance(behaviour, BaseException):
            raise behaviour
        return _Resp(behaviour)

    monkeypatch.setattr(notify_mod, "urlopen", fake)
    if kind == "manual":
        return notify_mod.load_card_id_and_post(hass, PID, KEY, "Push test")
    return notify_mod.load_card_id_and_post(
        hass, PID, KEY, "Push test", manual=False, kind=kind, rule=f"{kind}.x"
    )


def _fields(hass):
    body = clients_mod.pairing_document(hass, _phone(hass))
    return body["eventKeyLastPushStatus"], body["eventKeyInstalledAt"]


def test_no_attempt_is_null(tmp_path):
    assert _fields(_Hass(tmp_path)) == (None, None)


def test_set_on_401_default_code(monkeypatch, tmp_path):
    hass = _Hass(tmp_path)
    assert _post(monkeypatch, hass, _http_error(401)) == 401
    last, _ = _fields(hass)
    assert last["code"] == 401 and last["error"] == "invalid_house_event_key"
    assert "T" in last["at"] and ("+" in last["at"] or "-" in last["at"][10:])
    assert set(last) == {"code", "error", "at"}


def test_set_on_403_uses_upstream_code(monkeypatch, tmp_path):
    hass = _Hass(tmp_path)
    body = json.dumps({"error": {"code": "house_event_key_revoked"}}).encode()
    _post(monkeypatch, hass, _http_error(403, body), kind="chat")
    last, _ = _fields(hass)
    assert last["code"] == 403 and last["error"] == "house_event_key_revoked"


def test_null_after_2xx(monkeypatch, tmp_path):
    hass = _Hass(tmp_path)
    _post(monkeypatch, hass, _http_error(401))
    _post(monkeypatch, hass, 202, kind="attention")
    assert _fields(hass)[0] is None


@pytest.mark.parametrize(
    "failure", [_http_error(500), URLError(TimeoutError("t")), TimeoutError("t")]
)
def test_500_and_timeout_do_not_set(monkeypatch, tmp_path, failure):
    hass = _Hass(tmp_path)
    _post(monkeypatch, hass, failure)
    assert _fields(hass)[0] is None


def test_event_key_install_clears_and_stamps(monkeypatch, tmp_path):
    hass = _Hass(tmp_path)
    _post(monkeypatch, hass, _http_error(401))
    assert _fields(hass)[0] is not None

    class _R:
        async def json(self):
            return {"houseEventKey": "hek_new_not_real"}

    view = http_mod.PatrimonyEventKeyView(hass)
    resp = asyncio.run(view.post(_R()))
    assert resp.status == 200
    last, installed = _fields(hass)
    assert last is None
    assert isinstance(installed, str) and "T" in installed


def test_present_on_both_endpoints_without_key(monkeypatch, tmp_path):
    hass = _Hass(tmp_path)
    _post(monkeypatch, hass, _http_error(401))
    req = _phone(hass)
    hass.data = {}
    pairing = json.loads(asyncio.run(http_mod.PatrimonyPairingView(hass).get(req)).body)
    monkeypatch.setattr(http_mod, "build_presentation_document", lambda *a: {"cards": []})
    state = json.loads(asyncio.run(http_mod.PatrimonyStateView(hass).get(req)).body)
    for obj in (pairing, state["pairing"]):
        assert obj["eventKeyLastPushStatus"]["code"] == 401
        assert "eventKeyInstalledAt" in obj
    blob = json.dumps([pairing, state])
    assert KEY not in blob and "hek_" not in blob and "fingerprint" not in blob.lower()


def test_state_pairing_plaintext_beside_envelope(monkeypatch, tmp_path):
    hass = _Hass(tmp_path)
    _post(monkeypatch, hass, _http_error(401))

    class _Mgr:
        def maybe_wrap_document(self, doc):
            return {"pq": "sealed"}

    from custom_components.patrimony_collection.pq_shield import session as pq

    monkeypatch.setattr(pq, "get_manager", lambda h: _Mgr())
    monkeypatch.setattr(http_mod, "build_presentation_document", lambda *a: {"cards": []})
    state = json.loads(asyncio.run(http_mod.PatrimonyStateView(hass).get(_phone(hass))).body)
    assert state["pq"] == "sealed"
    assert state["pairing"]["eventKeyLastPushStatus"]["code"] == 401


def test_survives_reload(monkeypatch, tmp_path):
    hass = _Hass(tmp_path)
    _post(monkeypatch, hass, _http_error(401))
    notify_mod.mark_event_key_installed(hass, at="2026-10-10T19:42:00+02:00")
    _post(monkeypatch, hass, _http_error(403))
    fresh = _Hass(tmp_path)  # new process, same /config
    last, installed = _fields(fresh)
    assert last["code"] == 403
    assert installed == "2026-10-10T19:42:00+02:00"
    assert KEY not in (tmp_path / notify_mod.NOTIFY_FILE).read_text()
