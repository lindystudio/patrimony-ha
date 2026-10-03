"""House screen display name and timezone. No phone rename, no auto-migrate."""
from __future__ import annotations

import json
from pathlib import Path

from custom_components.patrimony_collection.config_flow import (
    PatrimonyCollectionConfigFlow,
    _timezone_ok,
)
from custom_components.patrimony_collection.const import (
    CONF_DISPLAY_NAME,
    CONF_PROPERTY_ID,
    CONF_TIMEZONE,
    HOUSE_PATH,
)
from custom_components.patrimony_collection.house_identity import (
    apply_house_identity,
    ha_instance_timezone,
    house_identity_document,
)
from custom_components.patrimony_collection import house_identity as house_identity_mod
from custom_components.patrimony_collection.mapping import build_presentation_document
from custom_components.patrimony_collection.panel import PatrimonyHouseView

import asyncio

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "patrimony_collection"
PROPERTY_ID = "550e8400-e29b-41d4-a716-446655440000"
STORED_NAME = "QA Reconnect Home"


def _run(coro):
    return asyncio.run(coro)


class _Bus:
    def __init__(self):
        self.fired = []

    def fire(self, *args, **kwargs):
        self.fired.append((args, kwargs))

    async def async_fire(self, *args, **kwargs):
        self.fired.append((args, kwargs))


class _Entry:
    def __init__(self, data):
        self.data = dict(data)
        self.options = {}
        self.entry_id = "entry-1"


class _Hass:
    def __init__(self, root: Path, time_zone="Europe/Athens"):
        self.root = root
        self.bus = _Bus()
        self.notifies = []
        self.config = type(
            "C",
            (),
            {
                "time_zone": time_zone,
                "path": lambda _self, rel, root=root: str(root / rel),
            },
        )()
        self.config_entries = type(
            "CE",
            (),
            {"async_update_entry": staticmethod(self._update)},
        )()

    def _update(self, entry, data=None, options=None):
        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)

    def now(self):
        from datetime import datetime, timezone
        return datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _entry():
    return _Entry(
        {
            CONF_PROPERTY_ID: PROPERTY_ID,
            CONF_DISPLAY_NAME: STORED_NAME,
            CONF_TIMEZONE: "UTC",
        }
    )


def _schema_default(schema, key: str):
    for marker in schema.schema:
        raw = getattr(marker, "schema", marker)
        if str(raw) == key:
            return getattr(marker, "default", None)
    raise AssertionError(f"{key} missing")


def test_first_setup_defaults_timezone_to_home_assistant_not_utc() -> None:
    flow = PatrimonyCollectionConfigFlow()
    flow.hass = _Hass(Path("/tmp"), time_zone="Europe/Athens")
    result = _run(flow.async_step_user())
    default = _schema_default(result["data_schema"], CONF_TIMEZONE)
    assert default == "Europe/Athens"
    assert default != "UTC"


def test_first_setup_timezone_falls_back_to_utc_only_when_missing() -> None:
    flow = PatrimonyCollectionConfigFlow()
    flow.hass = _Hass(Path("/tmp"), time_zone="")
    result = _run(flow.async_step_user())
    assert _schema_default(result["data_schema"], CONF_TIMEZONE) == "UTC"
    assert ha_instance_timezone(None) == "UTC"
    assert ha_instance_timezone(type("H", (), {"config": object()})()) == "UTC"


def test_existing_timezone_is_left_alone_until_save_or_button(tmp_path) -> None:
    hass = _Hass(tmp_path, time_zone="Europe/Athens")
    entry = _entry()
    assert ha_instance_timezone(hass) == "Europe/Athens"
    assert entry.data[CONF_TIMEZONE] == "UTC"
    document = build_presentation_document(hass, dict(entry.data), {})
    assert document["property"]["timezone"] == "UTC"
    assert document["property"]["displayName"] == STORED_NAME
    body, status = apply_house_identity(hass, entry, {})
    assert status == 200
    assert entry.data[CONF_TIMEZONE] == "UTC"
    assert entry.data[CONF_DISPLAY_NAME] == STORED_NAME
    assert body["timezone"] == "UTC"
    assert hass.bus.fired == []


def test_save_display_name_updates_entry_and_does_not_notify_or_read_ios(tmp_path) -> None:
    hass = _Hass(tmp_path, time_zone="Europe/Athens")
    session = tmp_path / "patrimony_collection" / "ios_session.json"
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"deviceName": "Phone Local Name"}), encoding="utf-8")
    entry = _entry()
    body, status = apply_house_identity(
        hass,
        entry,
        {
            "displayName": "North House",
            "deviceName": "Phone Local Name",
            "ios_session": {"deviceName": "Phone Local Name"},
        },
    )
    assert status == 200
    assert body["displayName"] == "North House"
    assert entry.data[CONF_DISPLAY_NAME] == "North House"
    assert entry.data[CONF_TIMEZONE] == "UTC"
    assert json.loads(session.read_text())["deviceName"] == "Phone Local Name"
    assert "Phone Local Name" != entry.data[CONF_DISPLAY_NAME]
    document = build_presentation_document(hass, dict(entry.data), {})
    assert document["property"]["displayName"] == "North House"
    assert hass.bus.fired == []
    assert hass.notifies == []
    src = Path(house_identity_mod.__file__).read_text(encoding="utf-8")
    assert "ios_session" not in src
    assert "import notify" not in src
    assert "from .notify" not in src
    assert "async_fire" not in src


def test_phone_name_alone_does_not_change_display_name(tmp_path) -> None:
    hass = _Hass(tmp_path)
    entry = _entry()
    body, status = apply_house_identity(hass, entry, {"deviceName": "Phone Local Name"})
    assert status == 200
    assert entry.data[CONF_DISPLAY_NAME] == STORED_NAME
    assert body["displayName"] == STORED_NAME


def test_save_timezone_updates_presentation_document(tmp_path) -> None:
    hass = _Hass(tmp_path, time_zone="Europe/Athens")
    entry = _entry()
    body, status = apply_house_identity(hass, entry, {"timezone": "Europe/Paris"})
    assert status == 200
    assert entry.data[CONF_TIMEZONE] == "Europe/Paris"
    assert entry.data[CONF_DISPLAY_NAME] == STORED_NAME
    assert body["timezone"] == "Europe/Paris"
    document = build_presentation_document(hass, dict(entry.data), {})
    assert document["property"]["timezone"] == "Europe/Paris"
    assert document["property"]["displayName"] == STORED_NAME
    assert hass.bus.fired == []


def test_copy_from_home_assistant_button_updates_timezone_only(tmp_path) -> None:
    hass = _Hass(tmp_path, time_zone="Europe/Athens")
    entry = _entry()
    body, status = apply_house_identity(
        hass,
        entry,
        {"copyTimezoneFromHomeAssistant": True, "deviceName": "Phone Local Name"},
    )
    assert status == 200
    assert body["timezone"] == "Europe/Athens"
    assert body["homeAssistantTimezone"] == "Europe/Athens"
    assert entry.data[CONF_TIMEZONE] == "Europe/Athens"
    assert entry.data[CONF_DISPLAY_NAME] == STORED_NAME
    document = build_presentation_document(hass, dict(entry.data), {})
    assert document["property"]["timezone"] == "Europe/Athens"
    assert document["property"]["displayName"] == STORED_NAME
    assert hass.bus.fired == []


def test_invalid_timezone_does_not_write(tmp_path) -> None:
    hass = _Hass(tmp_path)
    entry = _entry()
    body, status = apply_house_identity(hass, entry, {"displayName": "North House", "timezone": "notzone"})
    assert status == 400
    assert body["error"]["code"] == "invalid_timezone"
    assert entry.data[CONF_DISPLAY_NAME] == STORED_NAME
    assert entry.data[CONF_TIMEZONE] == "UTC"
    assert not _timezone_ok("notzone")


def test_field_document_is_stored_name_not_a_phone_or_file_name(tmp_path) -> None:
    hass = _Hass(tmp_path)
    entry = _entry()
    mapping = tmp_path / "patrimony_collection" / "mapping.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(
        json.dumps({"display_name": "Other Name", "timezone": "Europe/Paris", "cards": []}),
        encoding="utf-8",
    )
    doc = house_identity_document(entry)
    assert doc["displayName"] == STORED_NAME
    assert doc["timezone"] == "UTC"


def test_house_screen_has_display_name_timezone_and_copy_button() -> None:
    html = (COMPONENT / "www" / "index.html").read_text(encoding="utf-8")
    assert 'id="house_display_name"' in html
    assert 'aria-label="Display name"' in html
    assert 'id="house_timezone"' in html
    assert 'aria-label="Timezone"' in html
    assert 'id="house_tz_from_ha"' in html
    assert "Use this Home Assistant's timezone" in html
    assert HOUSE_PATH in html
    start = html.find("async function saveHouseIdentity")
    assert start > 0
    chunk = html[start:start + 1600]
    assert "NOTIFY_PATH" not in chunk
    assert "ios_session" not in chunk
    assert "deviceName" not in chunk
    assert "copyTimezoneFromHomeAssistant" in chunk
    view = PatrimonyHouseView(None)
    assert view.url == HOUSE_PATH
