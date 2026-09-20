"""lock.* entity state is enum Locked/Unlocked, never bool Yes/No."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.patrimony_collection.const import (  # noqa: E402
    FORBIDDEN_WIRE_KEYS,
    LOCK_ENUM_VALUES,
    VALUE_TYPES,
)
from custom_components.patrimony_collection.mapping import (  # noqa: E402
    build_presentation_document,
    infer_value_type,
    lock_item_value,
    redact_document,
)

CARD_ID = "43ed4b93-19a9-46d8-8ff7-bf896a8954f9"
ITEM_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
LOCK_EID = "lock.front_door"


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


def _entry():
    return {
        "property_id": "00000000-0000-4000-8000-000000000001",
        "display_name": "Demo Home",
        "location_label": "Example",
        "timezone": "UTC",
    }


def _options(*, value_type="auto", severity_mode="auto"):
    return {
        "cards": [
            {"id": CARD_ID, "kind": "security", "title": "Alarm", "priority": 5},
        ],
        "mappings": [
            {
                "item_id": ITEM_ID,
                "card_id": CARD_ID,
                "entity_id": LOCK_EID,
                "label": "Front Door Smart Lock",
                "value_type": value_type,
                "severity_mode": severity_mode,
            }
        ],
    }


def _doc(tmp_path, ha_state, **opts):
    hass = _Hass(tmp_path, {LOCK_EID: _State(LOCK_EID, ha_state, {}, datetime.now(timezone.utc))})
    return build_presentation_document(
        hass, _entry(), _options(**opts), generated_at="2026-09-20T16:00:00Z"
    )


def _lock_item(doc):
    items = [item for card in doc["cards"] for item in card["items"]]
    assert len(items) == 1
    return items[0]


def test_infer_lock_domain_is_enum_not_bool():
    assert infer_value_type(LOCK_EID, None, None) == "enum"
    assert infer_value_type(LOCK_EID, None, "bool") == "enum"
    assert infer_value_type(LOCK_EID, None, "auto") == "enum"
    assert infer_value_type("binary_sensor.door", None, None) == "bool"


def test_lock_item_value_maps_locked_unlocked():
    assert lock_item_value("locked") == ("Locked", True)
    assert lock_item_value("unlocked") == ("Unlocked", True)
    assert lock_item_value("LOCKED") == ("Locked", True)
    assert lock_item_value("unknown") == (None, True)
    assert lock_item_value("unavailable") == (None, True)
    assert lock_item_value("jammed") == (None, True)
    assert lock_item_value("locking") == (None, True)
    assert lock_item_value("unlocking") == (None, True)
    assert LOCK_ENUM_VALUES == {"locked": "Locked", "unlocked": "Unlocked"}
    assert VALUE_TYPES == ("enum", "number", "bool", "text")


def test_front_door_locked_emits_enum_locked(tmp_path):
    item = _lock_item(_doc(tmp_path, "locked"))
    assert item["valueType"] == "enum"
    assert item["value"] == "Locked"
    assert item["severity"] == "ok"
    assert item["unit"] is None
    assert "entity_id" not in item
    assert item["id"] == ITEM_ID


def test_front_door_unlocked_emits_enum_unlocked(tmp_path):
    item = _lock_item(_doc(tmp_path, "unlocked"))
    assert item["valueType"] == "enum"
    assert item["value"] == "Unlocked"
    assert item["severity"] == "ok"
    assert "entity_id" not in item


def test_declared_bool_still_emits_lock_enum(tmp_path):
    item = _lock_item(_doc(tmp_path, "locked", value_type="bool"))
    assert item["valueType"] == "enum"
    assert item["value"] == "Locked"
    assert item["value"] is not True
    assert item["value"] is not False


def test_unavailable_and_jammed_are_null_attention(tmp_path):
    for state in ("unknown", "unavailable", "jammed", "locking"):
        item = _lock_item(_doc(tmp_path, state))
        assert item["value"] is None
        assert item["valueType"] == "enum"
        assert item["severity"] == "attention"


def test_ok_when_off_treats_locked_as_ok(tmp_path):
    locked = _lock_item(_doc(tmp_path, "locked", severity_mode="ok_when_off"))
    unlocked = _lock_item(_doc(tmp_path, "unlocked", severity_mode="ok_when_off"))
    assert locked["severity"] == "ok"
    assert unlocked["severity"] == "attention"


def _collect_keys(obj):
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, val in obj.items():
            keys.add(str(key))
            keys.update(_collect_keys(val))
    elif isinstance(obj, list):
        for item in obj:
            keys.update(_collect_keys(item))
    return keys


def test_wire_document_has_no_entity_id(tmp_path):
    doc = redact_document(_doc(tmp_path, "locked"))
    blob = json.dumps(doc)
    assert LOCK_EID not in blob
    keys = _collect_keys(doc)
    assert "entity_id" not in keys
    assert not (FORBIDDEN_WIRE_KEYS & keys)


def test_panel_preview_js_maps_lock_to_enum():
    for rel in (
        ROOT / "custom_components" / "patrimony_collection" / "www" / "index.html",
        ROOT / "addons" / "patrimony_collection" / "static" / "index.html",
    ):
        html = rel.read_text(encoding="utf-8")
        assert "function lockEnumValue" in html
        assert 'if (tok === "locked") return "Locked"' in html
        assert 'if (tok === "unlocked") return "Unlocked"' in html
        assert 'if (d === "lock" && !m.state_attribute) valueType = "enum"' in html
        assert '["binary_sensor","switch","input_boolean","light","lock"]' not in html
        assert "looksLockLine" in html
        assert 'return item.value ? "Unlocked" : "Locked"' in html


if __name__ == "__main__":
    import tempfile

    test_infer_lock_domain_is_enum_not_bool()
    test_lock_item_value_maps_locked_unlocked()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        test_front_door_locked_emits_enum_locked(p)
        test_front_door_unlocked_emits_enum_unlocked(p)
        test_declared_bool_still_emits_lock_enum(p)
        test_unavailable_and_jammed_are_null_attention(p)
        test_ok_when_off_treats_locked_as_ok(p)
        test_wire_document_has_no_entity_id(p)
    test_panel_preview_js_maps_lock_to_enum()
    print("ok")
