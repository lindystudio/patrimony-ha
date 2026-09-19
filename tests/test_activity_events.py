"""Root events is additive on PresentationDocument; schemaVersion stays 1."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.patrimony_collection.activity import (  # noqa: E402
    format_activity_message,
    normalize_events,
    record_mapped_activity,
)
from custom_components.patrimony_collection.const import DOMAIN, MAX_EVENTS  # noqa: E402
from custom_components.patrimony_collection.mapping import (  # noqa: E402
    DEMO_ENTRY_DATA,
    DEMO_OPTIONS,
    build_presentation_document,
)

ALARM_ITEM = "769ba028-4477-451c-8a2e-b6e3472ee898"
WIFI_ITEM = "1497ff6e-f94b-466d-a1ae-ab5b2c2b8590"
CLIENTS_ITEM = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class _State:
    def __init__(self, entity_id, state, attributes=None, last_updated=None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.last_updated = last_updated
        self.last_changed = last_updated


class _States:
    def __init__(self, mapping: dict):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class _Hass:
    def __init__(self, root: Path, states: dict | None = None, runtime=None):
        self.config = type("c", (), {"path": lambda _self, p, root=root: str(root / p)})()
        self.states = _States(states or {})
        self.data = {DOMAIN: {"entry": runtime or {"events": [], "presented": {}, "unsub": []}}}

    def now(self):
        return datetime.now(timezone.utc)


WIFI = "binary_sensor.demo_guest_wifi"
INDOOR = "sensor.demo_indoor_temp"
ALARM = "alarm_control_panel.demo_home"
WEATHER = "weather.home"
HEARD = datetime(2026, 8, 23, 14, 20, tzinfo=timezone.utc)


def _live_states(alarm: str = "armed_away", wifi: str = "on", indoor: str = "21.5") -> dict:
    return {
        WIFI: _State(WIFI, wifi, {"device_class": "connectivity"}, HEARD),
        INDOOR: _State(
            INDOOR,
            indoor,
            {"device_class": "temperature", "unit_of_measurement": "°C"},
            HEARD,
        ),
        ALARM: _State(ALARM, alarm, {}, HEARD),
        WEATHER: _State(WEATHER, "cloudy", {"temperature": 18, "temperature_unit": "°C"}, HEARD),
    }


def _item(item_id, label, value, value_type="enum", unit=None):
    return {
        "id": item_id,
        "label": label,
        "value": value,
        "valueType": value_type,
        "unit": unit,
    }


def _doc(*items, generated_at="2026-08-23T14:20:00Z"):
    return {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "cards": [{"id": "card", "kind": "custom", "title": "House", "items": list(items)}],
    }


def test_empty_when_no_activity():
    runtime: dict = {"events": [], "presented": {}}
    document = _doc(_item(ALARM_ITEM, "Alarm", "Armed Away"))
    events = record_mapped_activity(runtime, document, at="2026-08-23T14:20:00Z")
    assert events == []
    assert document["events"] == []
    assert runtime["events"] == []


def test_append_on_mapped_change():
    runtime: dict = {"events": [], "presented": {}}
    record_mapped_activity(
        runtime,
        _doc(_item(ALARM_ITEM, "Alarm", "Armed Away")),
        at="2026-08-23T14:20:00Z",
    )
    changed = _doc(
        _item(ALARM_ITEM, "Alarm", "Disarmed"),
        generated_at="2026-08-23T14:21:00Z",
    )
    events = record_mapped_activity(runtime, changed, at="2026-08-23T14:21:00Z")
    assert len(events) == 1
    assert events[0]["at"] == "2026-08-23T14:21:00Z"
    assert events[0]["message"] == "Alarm disarmed"


def test_skip_unmapped_and_unchanged():
    runtime: dict = {"events": [], "presented": {}}
    first = _doc(_item(ALARM_ITEM, "Alarm", "Armed Away"))
    record_mapped_activity(runtime, first, at="2026-08-23T14:20:00Z")
    same = _doc(_item(ALARM_ITEM, "Alarm", "Armed Away"))
    events = record_mapped_activity(runtime, same, at="2026-08-23T14:21:00Z")
    assert events == []
    extra = _doc(
        _item(ALARM_ITEM, "Alarm", "Armed Away"),
        _item(WIFI_ITEM, "Guest Wi-Fi", True, "bool"),
    )
    events = record_mapped_activity(runtime, extra, at="2026-08-23T14:22:00Z")
    assert events == []


def test_cap_8_newest_first():
    runtime: dict = {"events": [], "presented": {}}
    record_mapped_activity(
        runtime,
        _doc(_item(CLIENTS_ITEM, "Wi‑Fi clients", 0, "number")),
        at="2026-08-23T14:00:00Z",
    )
    for n in range(1, 12):
        stamp = f"2026-08-23T14:{n:02d}:00Z"
        record_mapped_activity(
            runtime,
            _doc(_item(CLIENTS_ITEM, "Wi‑Fi clients", n, "number"), generated_at=stamp),
            at=stamp,
        )
    events = runtime["events"]
    assert len(events) == MAX_EVENTS
    assert [row["message"] for row in events] == [f"Wi‑Fi clients {n}" for n in range(11, 3, -1)]
    assert events[0]["at"] == "2026-08-23T14:11:00Z"
    assert events[-1]["at"] == "2026-08-23T14:04:00Z"
    ats = [row["at"] for row in events]
    assert ats == sorted(ats, reverse=True)


def test_message_sanitization_no_entity_id():
    leaked = format_activity_message(
        _item("x", "lock.front_door", True, "bool")
    )
    assert leaked == "Front door unlocked"
    assert "lock.front_door" not in leaked
    assert "entity_id" not in leaked

    dirty_value = format_activity_message(
        _item("y", "Uplink", "binary_sensor.wan_up", "text")
    )
    assert dirty_value == "Uplink"
    assert dirty_value is None or "binary_sensor." not in dirty_value

    rejected = sanitize_via_normalize(
        "Front door unlocked via lock.front_door at 192.168.1.10"
    )
    assert rejected is None

    hek = sanitize_via_normalize("Notify hek_secretvalue")
    assert hek is None


def sanitize_via_normalize(message: str) -> str | None:
    rows = normalize_events([{"at": "2026-08-23T14:20:00Z", "message": message}])
    return rows[0]["message"] if rows else None


def test_quiet_copy_examples():
    assert format_activity_message(_item("a", "Front door", True, "bool")) == "Front door unlocked"
    assert (
        format_activity_message(_item("b", "Alarm", "Armed Away")) == "Alarm armed · Away"
    )
    assert format_activity_message(_item("c", "Wi‑Fi clients", 18, "number")) == "Wi‑Fi clients 18"
    assert format_activity_message(_item("d", "Indoor", 21.5, "number", "°C")) == "Indoor 21.5°C"
    assert format_activity_message(_item("e", "Condition", "Cloudy")) == "Cloudy"


def test_document_empty_events_schema_stays_1(tmp_path):
    doc = build_presentation_document(
        _Hass(tmp_path, _live_states()),
        DEMO_ENTRY_DATA,
        DEMO_OPTIONS,
        generated_at="2026-08-23T14:20:00Z",
    )
    assert doc["schemaVersion"] == 1
    assert doc["events"] == []
    blob = json.dumps(doc)
    assert "entity_id" not in blob
    assert "hek_" not in blob


def test_rest_snapshot_includes_current_buffer(tmp_path):
    runtime: dict = {"events": [], "presented": {}, "unsub": []}
    hass = _Hass(tmp_path, _live_states("armed_away"), runtime)
    baseline = build_presentation_document(
        hass, DEMO_ENTRY_DATA, DEMO_OPTIONS, generated_at="2026-08-23T14:20:00Z"
    )
    record_mapped_activity(runtime, baseline, at=baseline["generatedAt"])
    assert baseline["events"] == []

    hass.states._mapping[ALARM] = _State(ALARM, "disarmed", {}, HEARD)
    changed = build_presentation_document(
        hass, DEMO_ENTRY_DATA, DEMO_OPTIONS, generated_at="2026-08-23T14:21:00Z"
    )
    record_mapped_activity(runtime, changed, at=changed["generatedAt"])
    assert changed["events"][0]["message"] == "Alarm disarmed"
    assert changed["events"][0]["at"] == "2026-08-23T14:21:00Z"

    rest = build_presentation_document(
        hass, DEMO_ENTRY_DATA, DEMO_OPTIONS, generated_at="2026-08-23T14:22:00Z"
    )
    assert rest["schemaVersion"] == 1
    assert rest["events"][0]["message"] == "Alarm disarmed"
    assert "entity_id" not in json.dumps(rest)
    assert "alarm_control_panel" not in json.dumps(rest)


if __name__ == "__main__":
    import tempfile

    test_empty_when_no_activity()
    test_append_on_mapped_change()
    test_skip_unmapped_and_unchanged()
    test_cap_8_newest_first()
    test_message_sanitization_no_entity_id()
    test_quiet_copy_examples()
    with tempfile.TemporaryDirectory() as d:
        test_document_empty_events_schema_stays_1(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_rest_snapshot_includes_current_buffer(Path(d))
    print("ok")
