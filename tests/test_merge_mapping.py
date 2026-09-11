"""Empty mapping.json must not wipe demo Weather items."""
from custom_components.patrimony_collection.const import CONF_CARDS, CONF_MAPPINGS
from custom_components.patrimony_collection.mapping import (
    DEMO_ENTRY_DATA,
    DEMO_OPTIONS,
    merge_options,
)

WEATHER_ITEM = "cb1a007e-ed80-498f-ba16-396c13235ee3"


def test_empty_file_keeps_entry_weather():
    data, opts = merge_options(
        DEMO_ENTRY_DATA,
        DEMO_OPTIONS,
        {"cards": [], "mappings": []},
    )
    ids = [row["item_id"] for row in opts[CONF_MAPPINGS]]
    assert WEATHER_ITEM in ids
    assert any(c.get("title") == "Weather" for c in opts[CONF_CARDS])
    assert data["property_id"] == DEMO_ENTRY_DATA["property_id"]


def test_partial_file_unions():
    extra = {
        "cards": [{"id": "new-card", "kind": "network", "title": "WAN", "priority": 11}],
        "mappings": [{
            "item_id": "new-item",
            "card_id": "bb2e5bc9-d780-4721-8884-71c7ad2de495",
            "entity_id": "binary_sensor.wan",
            "label": "WAN",
        }],
    }
    _data, opts = merge_options(DEMO_ENTRY_DATA, DEMO_OPTIONS, extra)
    ids = [row["item_id"] for row in opts[CONF_MAPPINGS]]
    assert WEATHER_ITEM in ids
    assert "new-item" in ids


def test_weather_home_is_demo_weather_source():
    eids = {row["entity_id"] for row in DEMO_OPTIONS[CONF_MAPPINGS]}
    assert "weather.home" in eids

if __name__ == "__main__":
    test_empty_file_keeps_entry_weather()
    test_partial_file_unions()
    test_weather_home_is_demo_weather_source()
    print("ok")
