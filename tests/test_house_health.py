"""House health: lastHeard / lastUpdated, kind custom, no street/city/country."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.patrimony_collection.const import FORBIDDEN_WIRE_KEYS  # noqa: E402
from custom_components.patrimony_collection.mapping import (
    DEMO_ENTRY_DATA,
    DEMO_OPTIONS,
    build_presentation_document,
    latest_iso,
    redact_document,
)


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
    def __init__(self, root: Path, states: dict | None = None):
        self.config = type("c", (), {"path": lambda _self, p, root=root: str(root / p)})()
        self.states = _States(states or {})

    def now(self):
        return datetime.now(timezone.utc)


WIFI = "binary_sensor.demo_guest_wifi"
INDOOR = "sensor.demo_indoor_temp"
ALARM = "alarm_control_panel.demo_home"
WEATHER = "weather.home"

HEARD = datetime(2026, 8, 23, 14, 20, tzinfo=timezone.utc)
OLDER = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
NEWER = datetime(2026, 8, 23, 13, 45, tzinfo=timezone.utc)


def _live_states() -> dict:
    return {
        WIFI: _State(WIFI, "on", {"device_class": "connectivity"}, HEARD),
        INDOOR: _State(
            INDOOR,
            "21.5",
            {"device_class": "temperature", "unit_of_measurement": "°C"},
            OLDER,
        ),
        ALARM: _State(ALARM, "armed_away", {}, HEARD),
        WEATHER: _State(
            WEATHER,
            "cloudy",
            {"temperature": 18, "temperature_unit": "°C"},
            NEWER,
        ),
    }


def test_latest_iso_picks_newest():
    assert latest_iso(["2026-08-23T12:00:00Z", "2026-08-23T14:20:00Z"]) == "2026-08-23T14:20:00Z"
    assert latest_iso([None, "", "not-a-date"]) is None
    assert latest_iso([HEARD, OLDER]) == "2026-08-23T14:20:00Z"


def test_document_last_heard_and_card_last_updated(tmp_path):
    hass = _Hass(tmp_path, _live_states())
    generated = "2026-08-23T14:20:00Z"
    doc = build_presentation_document(
        hass, DEMO_ENTRY_DATA, DEMO_OPTIONS, generated_at=generated
    )
    assert doc["schemaVersion"] == 1
    assert doc["generatedAt"] == generated
    assert doc["lastHeard"] == generated
    prop = doc["property"]
    assert prop["lastHeard"] == generated
    assert prop["locationLabel"] == "Example"
    assert "street" not in prop
    assert "city" not in prop
    assert "country" not in prop
    assert all(card["kind"] == "custom" for card in doc["cards"])
    by_title = {card["title"]: card for card in doc["cards"]}
    assert by_title["Climate"]["lastUpdated"] == "2026-08-23T12:00:00Z"
    assert by_title["Weather"]["lastUpdated"] == "2026-08-23T13:45:00Z"
    assert by_title["Network"]["lastUpdated"] == "2026-08-23T14:20:00Z"
    assert by_title["Alarm"]["lastUpdated"] == "2026-08-23T14:20:00Z"


def test_unavailable_entity_is_attention_not_quiet_ok(tmp_path):
    hass = _Hass(tmp_path, {})
    doc = build_presentation_document(
        hass, DEMO_ENTRY_DATA, DEMO_OPTIONS, generated_at="2026-08-23T14:20:00Z"
    )
    items = [item for card in doc["cards"] for item in card["items"]]
    assert items
    assert all(item["value"] is None for item in items)
    assert all(item["severity"] == "attention" for item in items)
    assert doc["lastHeard"] == "2026-08-23T14:20:00Z"
    assert doc["property"]["lastHeard"] == "2026-08-23T14:20:00Z"
    assert all(card.get("lastUpdated") for card in doc["cards"])


def test_redact_strips_street_city_country():
    dirty = {
        "property": {
            "displayName": "Demo Home",
            "locationLabel": "Example",
            "usesFahrenheit": True,
            "street": "1 Example Street",
            "city": "Exampleton",
            "country": "XX",
        }
    }
    clean = redact_document(dirty)
    assert clean["property"] == {"displayName": "Demo Home", "locationLabel": "Example"}
    assert "usesFahrenheit" not in clean["property"]
    for key in ("street", "city", "country", "address", "postal_code", "usesFahrenheit"):
        assert key in FORBIDDEN_WIRE_KEYS


if __name__ == "__main__":
    import tempfile

    test_latest_iso_picks_newest()
    with tempfile.TemporaryDirectory() as d:
        test_document_last_heard_and_card_last_updated(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_unavailable_entity_is_attention_not_quiet_ok(Path(d))
    test_redact_strips_street_city_country()
    print("ok")
