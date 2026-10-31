"""0.4.109: (1) owner POST /pq_shield/disable keeps keypair + member sessions while member PQ is
required (same rule as 0.4.108 flag Off); (2) the house panel's zoneinfo list is loaded in the
executor, never in the event loop. Runs without ML-KEM."""

from __future__ import annotations

import asyncio
import json
import types
import zoneinfo

import pytest

from custom_components.patrimony_collection import house_identity as hi
from custom_components.patrimony_collection import panel
from custom_components.patrimony_collection.const import CONF_MEMBER_PQ_REQUIRED
from custom_components.patrimony_collection.pq_shield import views as pv

from tests.test_member_pq_flag_off import pq, _disk  # noqa: F401  (fixture + helper)
from tests.test_members import _Req, _fake_web  # noqa: F401  (autouse fixture)

run = asyncio.run


# ---- (1) owner Disable -------------------------------------------------------------------

def test_owner_disable_keeps_keypair_and_member_sessions(pq):
    hass, _opts, mgr = pq
    r = run(pv.PQShieldDisableView(hass).post(_Req()))
    assert r.status == 200 and r.json() == {"ok": True, "session": "absent"}
    assert mgr._aead_key is None  # owner session wiped
    assert mgr._recipient is not None and mgr._member_keys == {"mem_s": b"s" * 32}
    on_disk = _disk(hass)
    assert {"mlkemPrivate", "x25519Private", "memberSessions"} <= set(on_disk) and "aeadKey" not in on_disk
    assert mgr.wrap_for_member("mem_s", {"a": 1})["ct"] == "sealed:s"  # guest still sealed
    cap = mgr.member_capability("mem_s")
    assert cap["session"] == "present" and cap["ownerSession"] == "absent"


def test_owner_disable_transition_full_wipe_as_before(pq):
    hass, opts, mgr = pq
    opts[CONF_MEMBER_PQ_REQUIRED] = False
    r = run(pv.PQShieldDisableView(hass).post(_Req()))
    assert r.status == 200
    assert mgr._recipient is None and mgr._member_keys == {} and mgr._aead_key is None
    assert _disk(hass) == {"version": 1}


def test_owner_disable_and_flag_off_share_one_rule(pq):
    _hass, _opts, mgr = pq
    a = mgr.disable_owner()
    assert a == mgr.flag_off() == {"ok": True, "session": "absent"}
    assert mgr._recipient is not None and mgr._member_keys


# ---- (2) zoneinfo off the event loop -------------------------------------------------------

class _Loop:
    """Fake hass: the executor flag is set only while a job runs 'in the executor'."""

    def __init__(self):
        self.in_executor = False
        self.jobs = 0
        self.config = types.SimpleNamespace(time_zone="Europe/Lisbon", path=lambda p: "/nonexistent/" + p)

    async def async_add_executor_job(self, func, *args):
        self.jobs += 1
        self.in_executor = True
        try:
            return func(*args)
        finally:
            self.in_executor = False


@pytest.fixture
def cold_tz(monkeypatch):
    monkeypatch.setattr(hi, "_TZ_NAMES", None)
    hass = _Loop()
    real = zoneinfo.available_timezones

    def guarded():
        assert hass.in_executor, "available_timezones() called in the event loop"
        return real()

    monkeypatch.setattr(zoneinfo, "available_timezones", guarded)
    entry = types.SimpleNamespace(data={"display_name": "H", "timezone": "Europe/Lisbon"}, options={})
    monkeypatch.setattr(panel, "_first_entry", lambda _h: entry)
    return hass, entry


def test_house_get_loads_timezones_in_executor_once(cold_tz):
    hass, _entry = cold_tz
    view = panel.PatrimonyHouseView(hass)
    r = run(view.get(_Req()))
    body = r.json()
    assert r.status == 200 and "Europe/Paris" in body["timezones"] and "UTC" in body["timezones"]
    assert hass.jobs == 1
    run(view.get(_Req()))
    assert hass.jobs == 1  # cached: no second zoneinfo walk


def test_house_post_loads_timezones_in_executor(cold_tz, monkeypatch):
    hass, entry = cold_tz
    monkeypatch.setattr(hi, "_persist_entry_data", lambda *_a: None)
    r = run(panel.PatrimonyHouseView(hass).post(_Req(body={"timezone": "Not-a-zone"})))
    assert r.status == 400 and r.json()["error"]["code"] == "invalid_timezone"
    assert hass.jobs == 1


def test_async_names_match_sync_list(monkeypatch):
    monkeypatch.setattr(hi, "_TZ_NAMES", None)
    names = run(hi.async_iana_timezone_names(_Loop()))
    assert names == sorted(set(zoneinfo.available_timezones()) | {"UTC"}) == hi.iana_timezone_names()
