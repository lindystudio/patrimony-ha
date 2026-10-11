"""Push-state: mapped-only subscription, rebind, unload, coalesce/throttle."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import custom_components.patrimony_collection as pc  # noqa: E402
from custom_components.patrimony_collection.const import DOMAIN  # noqa: E402
from custom_components.patrimony_collection.push import SnapshotScheduler  # noqa: E402


class FakeLoop:
    def __init__(self):
        self.now = 0.0
        self.timers = []

    def call_later(self, delay, fn):
        loop = self

        class H:
            cancelled = False

            def cancel(self):
                self.cancelled = True

        h = H()
        self.timers.append((self.now + delay, fn, h))
        return h

    def advance(self, dt):
        end = self.now + dt
        while True:
            live = sorted((t for t in self.timers if not t[2].cancelled and t[0] <= end), key=lambda t: t[0])
            if not live:
                break
            at, fn, h = live[0]
            self.timers.remove(live[0])
            self.now = at
            fn()
        self.now = end


def _sched(**kw):
    loop = FakeLoop()
    fired = []
    s = SnapshotScheduler(loop, lambda: fired.append(loop.now), clock=lambda: loop.now, **kw)
    return loop, s, fired


def test_burst_coalesces_to_one_emit():
    loop, s, fired = _sched(debounce=0.3, max_wait=2.0, min_interval=1.0)
    for _ in range(10):
        s.request()
        loop.advance(0.05)
    loop.advance(1.0)
    assert len(fired) == 1


def test_continuous_stream_is_capped_by_max_wait_and_throttled():
    loop, s, fired = _sched(debounce=0.3, max_wait=2.0, min_interval=1.0)
    for _ in range(200):  # 20 s of a sensor updating every 0.1 s
        s.request()
        loop.advance(0.1)
    loop.advance(5)
    assert 5 <= len(fired) <= 11
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    assert all(g >= 1.0 - 1e-9 for g in gaps)


def test_throttle_spacing_after_emit():
    loop, s, fired = _sched(debounce=0.3, max_wait=2.0, min_interval=1.0)
    s.request(); loop.advance(0.5)
    s.request(); loop.advance(2)
    assert len(fired) == 2 and fired[1] - fired[0] >= 1.0


def test_cancel_drops_pending():
    loop, s, fired = _sched()
    s.request(); s.cancel(); loop.advance(5)
    assert fired == []


class _Entry:
    def __init__(self, ids):
        self.entry_id = "e1"
        self.data = {}
        self.options = {"mappings": [{"entity_id": i} for i in ids], "cards": []}


class _Hass:
    def __init__(self):
        self.data = {DOMAIN: {}}
        self.loop = FakeLoop()
        self.tasks = []

    def async_create_task(self, coro):
        coro.close()
        self.tasks.append(1)


def _patch(monkeypatch):
    subs = []

    def track(hass, ids, listener):
        rec = {"ids": list(ids), "listener": listener, "active": True}
        subs.append(rec)
        return lambda: rec.update(active=False)

    monkeypatch.setattr(pc, "async_track_state_change_event", track)
    monkeypatch.setattr(pc, "_entry_options", lambda hass, entry: entry.options)
    return subs


def test_subscribes_mapped_only_and_coalesces(monkeypatch):
    subs = _patch(monkeypatch)
    hass, entry = _Hass(), _Entry(["sensor.a", "binary_sensor.b"])
    runtime = {"unsub": []}
    hass.data[DOMAIN][entry.entry_id] = runtime
    pc._bind_mapped_listener(hass, entry, runtime)
    assert len(subs) == 1 and subs[0]["ids"] == ["sensor.a", "binary_sensor.b"]
    for _ in range(5):
        subs[0]["listener"](object())
    hass.loop.advance(3)
    assert len(hass.tasks) == 1


def test_resubscribe_on_mapping_change(monkeypatch):
    subs = _patch(monkeypatch)
    hass, entry = _Hass(), _Entry(["sensor.a"])
    runtime = {"unsub": []}
    hass.data[DOMAIN][entry.entry_id] = runtime
    pc._bind_mapped_listener(hass, entry, runtime)
    entry.options = {"mappings": [{"entity_id": "sensor.c"}], "cards": []}
    pc.async_rebind(hass, entry)
    assert subs[0]["active"] is False
    assert subs[1]["active"] is True and subs[1]["ids"] == ["sensor.c"]
    assert runtime["tracked"] == ["sensor.c"]


def test_unload_unsubscribes_and_cancels(monkeypatch):
    subs = _patch(monkeypatch)
    hass, entry = _Hass(), _Entry(["sensor.a"])
    runtime = {"unsub": []}
    hass.data[DOMAIN][entry.entry_id] = runtime
    pc._bind_mapped_listener(hass, entry, runtime)
    subs[0]["listener"](object())
    asyncio.run(pc.async_unload_entry(hass, entry))
    hass.loop.advance(5)
    assert subs[0]["active"] is False
    assert hass.tasks == []
    assert entry.entry_id not in hass.data[DOMAIN]
