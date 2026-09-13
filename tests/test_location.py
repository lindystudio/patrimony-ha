"""Single free-text Location: migrate parts, emit location, never structured address."""
from custom_components.patrimony_collection.const import (
    CONF_DISPLAY_NAME,
    CONF_LOCATION,
    CONF_LOCATION_LABEL,
    CONF_PROPERTY_ID,
    CONF_TIMEZONE,
    FORBIDDEN_WIRE_KEYS,
)
from custom_components.patrimony_collection.mapping import (
    apply_location_from_editor,
    build_presentation_document,
    join_address_parts,
    migrate_house_data,
    redact_document,
    resolve_location,
)


def test_resolve_prefers_location_over_label_and_parts():
    assert resolve_location({"location": "  Example  ", "city": "Athens"}) == "Example"
    assert resolve_location({"location_label": "Harbour", "street": "1 Road"}) == "Harbour"
    assert resolve_location({"locationLabel": "Hill"}) == "Hill"


def test_resolve_joins_nonempty_address_parts():
    assert (
        resolve_location({"street": "1 Road", "city": "Athens", "country": "Greece"})
        == "1 Road, Athens, Greece"
    )
    assert resolve_location({"street": "", "city": "Athens", "country": "  "}) == "Athens"
    assert join_address_parts({"street": "Same", "address": "Same", "city": "Town"}) == "Same, Town"
    assert resolve_location({}) == ""
    assert resolve_location({"street": "   "}) == ""


def test_resolve_scans_sources_explicit_then_parts():
    assert (
        resolve_location({"street": "1 Road"}, {CONF_LOCATION_LABEL: "Harbour"})
        == "Harbour"
    )
    assert resolve_location({"city": "Athens"}, {"country": "Greece"}) == "Athens"


def test_migrate_house_data_drops_structured_keys():
    out = migrate_house_data(
        {
            CONF_PROPERTY_ID: "00000000-0000-4000-8000-000000000001",
            CONF_DISPLAY_NAME: "Demo Home",
            CONF_LOCATION_LABEL: "Example",
            "street": "1 Road",
            "city": "Athens",
            "country": "Greece",
            CONF_TIMEZONE: "UTC",
        }
    )
    assert out[CONF_LOCATION] == "Example"
    assert CONF_LOCATION_LABEL not in out
    assert "street" not in out and "city" not in out and "country" not in out


def test_migrate_joins_when_no_single_field():
    out = migrate_house_data({"street": "1 Road", "city": "Athens", "country": ""})
    assert out[CONF_LOCATION] == "1 Road, Athens"
    assert "street" not in out


def test_editor_location_update_and_clear():
    base = {CONF_LOCATION: "Old"}
    updated = apply_location_from_editor(base, {"location": "  New  "})
    assert updated[CONF_LOCATION] == "New"
    cleared = apply_location_from_editor(updated, {"location": ""})
    assert CONF_LOCATION not in cleared
    untouched = apply_location_from_editor(base, {"cards": []})
    assert untouched[CONF_LOCATION] == "Old"
    joined = apply_location_from_editor({}, {"street": "1 Road", "city": "Athens"})
    assert joined[CONF_LOCATION] == "1 Road, Athens"


def test_presentation_emits_location_not_address_parts():
    data = {
        CONF_PROPERTY_ID: "00000000-0000-4000-8000-000000000001",
        CONF_DISPLAY_NAME: "Demo Home",
        "street": "1 Road",
        "city": "Athens",
        "country": "Greece",
        CONF_TIMEZONE: "UTC",
    }
    doc = build_presentation_document(None, data, {"cards": [], "mappings": []})
    prop = doc["property"]
    assert prop["location"] == "1 Road, Athens, Greece"
    assert prop["locationLabel"] == "1 Road, Athens, Greece"
    assert "street" not in prop
    assert "city" not in prop
    assert "country" not in prop


def test_redact_strips_structured_address_keeps_location():
    raw = {
        "schemaVersion": 1,
        "property": {
            "id": "00000000-0000-4000-8000-000000000001",
            "displayName": "Demo Home",
            "location": "Example",
            "usesFahrenheit": True,
            "street": "1 Road",
            "city": "Athens",
            "country": "Greece",
        },
        "cards": [],
    }
    clean = redact_document(raw)
    assert clean["property"]["location"] == "Example"
    assert "usesFahrenheit" not in clean["property"]
    assert "street" not in clean["property"]
    assert "city" not in clean["property"]
    assert "country" not in clean["property"]
    for key in ("street", "city", "country", "address", "postal_code", "usesFahrenheit"):
        assert key in FORBIDDEN_WIRE_KEYS
    assert "location" not in FORBIDDEN_WIRE_KEYS


if __name__ == "__main__":
    test_resolve_prefers_location_over_label_and_parts()
    test_resolve_joins_nonempty_address_parts()
    test_resolve_scans_sources_explicit_then_parts()
    test_migrate_house_data_drops_structured_keys()
    test_migrate_joins_when_no_single_field()
    test_editor_location_update_and_clear()
    test_presentation_emits_location_not_address_parts()
    test_redact_strips_structured_address_keeps_location()
    print("ok")
