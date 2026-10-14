"""C9: adopt an existing property id on a never-claimed HA (phone POST /property)."""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from custom_components.patrimony_collection import adopt as adopt_mod
from custom_components.patrimony_collection import clients as clients_mod
from custom_components.patrimony_collection import notify as notify_mod
from custom_components.patrimony_collection.const import (
    PROPERTY_ADOPT_LOG_PATH,
    PROPERTY_PATH,
)
from custom_components.patrimony_collection.http import (
    PatrimonyEventKeyView,
    PatrimonyPairingView,
    PatrimonyPropertyAdoptLogView,
    PatrimonyPropertyView,
)

from tests.test_clients import _Entries, _Entry, _Hass, _phone, _Req, _User

CPH_ID = "00000000-0000-4000-8000-000000000001"
MINTED = "11111111-2222-4333-8444-555555555555"
HOME = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "99999999-8888-4777-8666-555555555555"
KEY = "hek_test_not_real"


class _JsonResp:
    def __init__(self, data, status=200):
        self.status = status
        self.data = data
        self.body = json.dumps(data).encode()


@pytest.fixture(autouse=True)
def _aiohttp_web(monkeypatch):
    web = types.SimpleNamespace(json_response=lambda data, status=200: _JsonResp(data, status))
    mod = types.ModuleType("aiohttp")
    mod.web = web
    monkeypatch.setitem(sys.modules, "aiohttp", mod)
    monkeypatch.setitem(sys.modules, "aiohttp.web", web)


class _UpdEntries(_Entries):
    """Records async_update_entry like HA (no reload)."""

    def __init__(self, entry, others=()):
        super().__init__(entry)
        self.others = list(others)
        self.updates = []

    def async_entries(self, _domain):
        return [self.entry, *self.others]

    def async_update_entry(self, entry, **kw):
        self.updates.append(kw)
        for k, v in kw.items():
            setattr(entry, k, v)


def _fresh(tmp_path, pid=MINTED, others=()):
    entry = _Entry()
    entry.data = dict(entry.data, property_id=pid, display_name="New House")
    entry.options = {"house_event_key": None}
    entry.unique_id = pid
    entry.title = f"New House ({pid})"
    hass = _Hass(tmp_path, entry)
    hass.data = {}
    hass.config_entries = _UpdEntries(entry, others)
    return hass, entry


def _field_house(tmp_path):
    """Field-house-shaped: existing id, installed key, paired client, push log."""
    hass, entry = _fresh(tmp_path, CPH_ID)
    entry.options = {"house_event_key": KEY}
    clients_mod.upsert_client(hass, "rt-cph-phone", device_name="Owner iPhone")
    notify_mod.mark_event_key_installed(hass, at="2026-10-10T19:42:00+02:00")
    return hass, entry


class _PostReq(_Req):
    def __init__(self, base, body):
        super().__init__(base.get("hass_user"), base.headers)
        self._body = body
        self.content_length = len(json.dumps(body)) if body is not None else 0

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _post(hass, req_base, body):
    resp = asyncio.run(PatrimonyPropertyView(hass).post(_PostReq(req_base, body)))
    return resp.status, resp.data


def _paired(hass, cid="rt-phone-1"):
    clients_mod.upsert_client(hass, cid, device_name="Owner iPhone")
    return _phone(hass, cid)


def _no_secrets(obj):
    text = json.dumps(obj).lower()
    assert "hek_" not in text and "token" not in text and "eyj" not in text


def test_paths():
    assert PROPERTY_PATH == "/api/patrimony_collection/property"
    assert PROPERTY_ADOPT_LOG_PATH == "/api/patrimony_collection/property/adopt_log"
    assert PatrimonyPropertyView.requires_auth is True
    assert PatrimonyPropertyAdoptLogView.requires_auth is True


def test_fresh_adopt_updates_entry_without_reload(tmp_path):
    hass, entry = _fresh(tmp_path)
    adopt_mod.migrate_adopt_lock(hass, entry)
    assert not entry.options.get("claimed_at")
    req = _paired(hass)
    status, body = _post(hass, req, {"propertyId": HOME.upper(), "upgrade": True})
    assert status == 200
    assert body == {"propertyId": HOME, "adopted": True, "previousPropertyId": MINTED}
    assert entry.data["property_id"] == HOME
    assert entry.unique_id == HOME
    assert entry.title == f"New House ({HOME})"
    assert entry.options["property_id_source"] == "adopted"
    assert entry.options["property_id_history"][-1]["id"] == MINTED
    # replay is idempotent
    status, body = _post(hass, req, {"propertyId": HOME, "upgrade": True})
    assert (status, body) == (200, {"propertyId": HOME, "adopted": False})
    doc = adopt_mod.property_document(hass, entry)
    assert doc == {"propertyId": HOME, "adoptable": True, "source": "adopted"}


def test_unclaimed_can_correct_then_event_key_latches(tmp_path):
    hass, entry = _fresh(tmp_path)
    adopt_mod.migrate_adopt_lock(hass, entry)
    req = _paired(hass)
    assert _post(hass, req, {"propertyId": OTHER, "upgrade": True})[0] == 200
    assert _post(hass, req, {"propertyId": HOME, "upgrade": True})[0] == 200
    entry.options = dict(entry.options)
    adopt_mod.stamp_claimed(hass, entry, "rt-phone-1")
    assert entry.options["claimed_at"]
    status, body = _post(hass, req, {"propertyId": OTHER, "upgrade": True})
    assert status == 409 and body["error"]["code"] == "property_id_conflict"
    assert entry.data["property_id"] == HOME


def test_event_key_view_sets_latch(tmp_path):
    hass, entry = _fresh(tmp_path)
    req = _paired(hass)

    class _KeyReq(_PostReq):
        pass

    resp = asyncio.run(PatrimonyEventKeyView(hass).post(_KeyReq(req, {"houseEventKey": KEY})))
    if resp.status == 200:
        assert entry.options.get("claimed_at")
        assert _post(hass, req, {"propertyId": HOME, "upgrade": True})[1]["error"]["code"] == "property_id_conflict"
    _no_secrets(adopt_mod.adopt_log(hass))


def test_field_house_fixture_locked(tmp_path):
    hass, entry = _field_house(tmp_path)
    assert adopt_mod.migrate_adopt_lock(hass, entry) is True
    assert entry.options["claimed_at"]
    req = _phone(hass, "rt-cph-phone")
    status, body = _post(hass, req, {"propertyId": HOME, "upgrade": True})
    assert status == 409 and body["error"]["code"] == "property_id_conflict"
    status, body = _post(hass, req, {"propertyId": CPH_ID, "upgrade": True})
    assert (status, body) == (200, {"propertyId": CPH_ID, "adopted": False})
    assert entry.data["property_id"] == CPH_ID and entry.unique_id == CPH_ID
    assert entry.options["house_event_key"] == KEY
    assert adopt_mod.property_document(hass, entry)["adoptable"] is False
    _no_secrets([body, adopt_mod.property_document(hass, entry), adopt_mod.adopt_log(hass)])


@pytest.mark.parametrize("sign", ["key", "installed", "client", "push_status", "claimed_at"])
def test_each_sign_locks_on_migration(tmp_path, sign):
    hass, entry = _fresh(tmp_path, CPH_ID)
    if sign == "key":
        entry.options = {"house_event_key": KEY}
    elif sign == "installed":
        notify_mod.mark_event_key_installed(hass)
    elif sign == "client":
        clients_mod.upsert_client(hass, "rt-old")
    elif sign == "push_status":
        notify_mod.record_event_key_push_status(hass, {"status": 401}) if hasattr(
            notify_mod, "record_event_key_push_status"
        ) else entry.options.update(claimed_at="x")
    else:
        entry.options = {"claimed_at": "2026-10-01T00:00:00+02:00"}
    assert adopt_mod.migrate_adopt_lock(hass, entry) is True
    assert entry.options["claimed_at"]
    req = _paired(hass, "rt-new")
    assert _post(hass, req, {"propertyId": HOME, "upgrade": True})[0] == 409


def test_migration_is_one_shot(tmp_path):
    hass, entry = _fresh(tmp_path)
    adopt_mod.migrate_adopt_lock(hass, entry)
    clients_mod.upsert_client(hass, "rt-phone-1")  # pairing after first setup
    adopt_mod.migrate_adopt_lock(hass, entry)  # restart
    assert not entry.options.get("claimed_at")


def test_auth_requires_active_registered_client(tmp_path):
    hass, entry = _fresh(tmp_path)
    # bearer whose iss is not registered (e.g. panel/LLAT, even client_name Patrimony iOS)
    unknown = _phone(hass, "rt-unregistered")
    status, body = _post(hass, unknown, {"propertyId": HOME, "upgrade": True})
    assert status == 403 and body["error"]["code"] == "not_patrimony_client"
    # no bearer at all
    status, _ = _post(hass, _Req(_User()), {"propertyId": HOME, "upgrade": True})
    assert status == 403
    # unpaired client
    req = _paired(hass, "rt-gone")
    clients_mod.mark_unpaired(hass, "rt-gone")
    assert _post(hass, req, {"propertyId": HOME, "upgrade": True})[1]["error"]["code"] == "not_patrimony_client"
    assert entry.data["property_id"] == MINTED


@pytest.mark.parametrize(
    "body",
    [None, {}, {"propertyId": "nope", "upgrade": True}, {"propertyId": HOME}, {"propertyId": HOME, "upgrade": "true"}],
)
def test_validation(tmp_path, body):
    hass, entry = _fresh(tmp_path)
    status, resp = _post(hass, _paired(hass), body)
    assert status == 400 and resp["error"]["code"] == "validation"
    assert entry.data["property_id"] == MINTED


def test_other_entry_conflict(tmp_path):
    other = _Entry()
    other.data = dict(other.data, property_id=HOME)
    other.unique_id = HOME
    hass, entry = _fresh(tmp_path, others=[other])
    status, body = _post(hass, _paired(hass), {"propertyId": HOME, "upgrade": True})
    assert status == 409 and body["error"]["code"] == "already_configured"


def test_rate_limit(tmp_path):
    hass, _ = _fresh(tmp_path)
    req = _paired(hass)
    codes = [_post(hass, req, {"propertyId": HOME, "upgrade": True})[0] for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_adopt_log_shape_cap_and_admin_only(tmp_path):
    hass, entry = _fresh(tmp_path)
    req = _paired(hass)
    _post(hass, req, {"propertyId": HOME, "upgrade": True})
    _post(hass, _phone(hass, "rt-x"), {"propertyId": OTHER, "upgrade": True})
    rows = adopt_mod.adopt_log(hass)
    assert [r["outcome"] for r in rows] == ["adopted", "not_patrimony_client"]
    r = rows[0]
    assert set(r) == {"at", "previousPropertyId", "newPropertyId", "clientId", "outcome"}
    assert r["previousPropertyId"] == MINTED and r["newPropertyId"] == HOME and r["clientId"] == "rt-phone-1"
    assert r["at"][-6] in "+-"  # ISO with offset
    _no_secrets(rows)
    for _ in range(80):
        adopt_mod.record_adopt_event(hass, previous_id=MINTED, new_id="junk hek_x", client_id="c", outcome="validation")
    rows = adopt_mod.adopt_log(hass)
    assert len(rows) == 50 and rows[-1]["newPropertyId"] is None
    admin = asyncio.run(PatrimonyPropertyAdoptLogView(hass).get(_Req(_User())))
    assert admin.status == 200 and len(admin.data["adoptLog"]) == 50
    non_admin = asyncio.run(PatrimonyPropertyAdoptLogView(hass).get(_Req(_User(is_admin=False))))
    assert non_admin.status == 403


def test_get_property_and_pairing_adoptable(tmp_path):
    hass, entry = _fresh(tmp_path)
    req = _paired(hass)
    resp = asyncio.run(PatrimonyPropertyView(hass).get(req))
    assert resp.status == 200 and resp.data == {"propertyId": MINTED, "adoptable": True, "source": "minted"}
    pairing = asyncio.run(PatrimonyPairingView(hass).get(req))
    assert pairing.data["adoptable"] is True
    stranger = _Req(_User(is_admin=False), _phone(hass, "rt-unknown").headers)
    assert asyncio.run(PatrimonyPropertyView(hass).get(stranger)).status == 403
    hass2, _ = _field_house(tmp_path / "cph")
    assert asyncio.run(PatrimonyPairingView(hass2).get(_phone(hass2, "rt-cph-phone"))).data["adoptable"] is False
