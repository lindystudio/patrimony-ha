"""Simulated leak/smoke entities end to end: mapping facts -> scheduler -> FIRE -> push log."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.patrimony_collection.attention import DEBOUNCE_SECONDS  # noqa: E402
from custom_components.patrimony_collection.mapping import attention_facts  # noqa: E402
from custom_components.patrimony_collection.notify import (  # noqa: E402
    dispatch_attention_pushes,
    push_log_document,
)
from custom_components.patrimony_collection.push import SnapshotScheduler  # noqa: E402

from test_push_state import FakeLoop  # noqa: E402

CARD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LEAK_ITEM = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SMOKE_ITEM = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
LEAK = "binary_sensor.patrimony_test_leak"
SMOKE = "binary_sensor.kitchen_smoke"


class _Config:
    def __init__(self, root):
        self._root = root

    def path(self, name):
        return str(self._root / name)


class _State:
    def __init__(self, state, device_class):
        self.state = state
        self.attributes = {"device_class": device_class}
        self.last_updated = None
        self.last_changed = None


class _States(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _Hass:
    def __init__(self, root):
        self.config = _Config(root)
        self.states = _States()


OPTIONS = {
    "cards": [{"id": CARD, "kind": "security", "title": "Safety", "priority": 1}],
    "mappings": [
        {"item_id": LEAK_ITEM, "card_id": CARD, "entity_id": LEAK, "label": "Test leak",
         "value_type": "bool", "severity_mode": "auto"},
        {"item_id": SMOKE_ITEM, "card_id": CARD, "entity_id": SMOKE, "label": "Kitchen smoke",
         "value_type": "bool", "severity_mode": "auto"},
    ],
}


class _Rig:
    def __init__(self, tmp_path, monkeypatch, status=200, initial="off"):
        self.hass = _Hass(tmp_path)
        self.hass.states[LEAK] = _State(initial, "moisture")
        self.hass.states[SMOKE] = _State(initial, "smoke")
        self.loop = FakeLoop()
        self.wall = 1_000_000.0
        self.posts = []
        self.emits = []

        def fake_post(property_id, key, card_id, title, timeout=8, severity="attention", **kw):
            self.posts.append({"title": title, "severity": severity, "at": self.loop.now})
            return status

        monkeypatch.setattr("custom_components.patrimony_collection.notify.post_house_event", fake_post)

        def emit():
            self.emits.append(self.loop.now)
            facts = attention_facts(self.hass, {"property_id": "p"}, OPTIONS)
            assert all("entity_id" not in f for f in facts)
            dispatch_attention_pushes(self.hass, facts, "p", "hek_house", now=self.wall + self.loop.now)

        self.sched = SnapshotScheduler(self.loop, emit, debounce=0.3, max_wait=2.0,
                                       min_interval=1.0, clock=lambda: self.loop.now)
        # Seed baseline (setup emit), as async_setup_entry does.
        emit()
        self.sched.note_emitted()

    def set(self, entity, value):
        self.hass.states[entity].state = value
        self.sched.request()

    def run(self, seconds):
        self.loop.advance(seconds)


def test_leak_fire_flips_coalesce_clear_and_refire(tmp_path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch)
    rig.run(5)
    rig.set(LEAK, "on")
    rig.run(0.2)
    for value in ("off", "on", "off", "on"):  # rapid flips inside 1 s
        rig.set(LEAK, value)
        rig.run(0.1)
    rig.run(5)
    assert len(rig.posts) == 1
    assert rig.posts[0]["title"] == "TEST: Leak detected"
    assert rig.posts[0]["severity"] == "alert"
    # Burst coalesced: far fewer snapshots than state changes.
    assert len(rig.emits) - 1 <= 3
    # Clear, then re-fire inside the debounce: no push.
    rig.set(LEAK, "off"); rig.run(5)
    rig.set(LEAK, "on"); rig.run(5)
    assert len(rig.posts) == 1
    # Clear, then re-fire after the debounce: one push.
    rig.set(LEAK, "off"); rig.run(5)
    rig.run(DEBOUNCE_SECONDS)
    rig.set(LEAK, "on"); rig.run(5)
    assert len(rig.posts) == 2

    rows = push_log_document(rig.hass)["rows"]
    assert len(rows) == 2
    for row in rows:
        assert row["kind"] == "attention"
        assert row["rule"] == "security.leak"
        assert row["fire_class"] == "security.leak"
        assert row["reason"] == "security.leak: TEST: Leak detected"
        assert row["severity"] == "alert"
        assert row["status"] == 200 and row["ok"] is True
        assert row["devices"]
        assert row["at"]
    blob = json.dumps(rows)
    assert "hek_" not in blob and LEAK not in blob


def test_smoke_and_leak_same_burst_both_fire_once(tmp_path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch)
    rig.set(SMOKE, "on")
    rig.set(LEAK, "on")
    rig.run(3)
    assert sorted(p["title"] for p in rig.posts) == ["Smoke detected", "TEST: Leak detected"]
    assert len(rig.emits) == 2  # seed + one coalesced snapshot
    rows = push_log_document(rig.hass)["rows"]
    assert sorted(r["rule"] for r in rows) == ["security.leak", "security.smoke"]


def test_first_sight_on_does_not_push(tmp_path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch, initial="on")
    rig.set(LEAK, "on"); rig.run(3)
    assert rig.posts == []
    assert push_log_document(rig.hass)["rows"] == []


def test_unavailable_is_quiet_and_failed_send_is_logged(tmp_path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch, status=401)
    rig.set(LEAK, "unavailable"); rig.run(3)
    assert rig.posts == []
    rig.set(LEAK, "on"); rig.run(3)
    assert len(rig.posts) == 1
    rows = push_log_document(rig.hass)["rows"]
    assert rows[-1]["status"] == 401 and rows[-1]["ok"] is False
    assert rows[-1]["kind"] == "attention"


def test_continuous_flapping_never_pushes_more_than_once_per_window(tmp_path, monkeypatch):
    rig = _Rig(tmp_path, monkeypatch)
    for i in range(300):  # 30 s of flapping
        rig.set(LEAK, "on" if i % 2 == 0 else "off")
        rig.run(0.1)
    rig.run(5)
    assert len(rig.posts) == 1
    gaps = [b - a for a, b in zip(rig.emits[1:], rig.emits[2:])]
    assert all(g >= 1.0 - 1e-9 for g in gaps)
