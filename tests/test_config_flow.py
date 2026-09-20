"""Config flow: new house mints property_id; existing UUID is optional.

CI offline tests install pytest only. config_flow must import without voluptuous.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from custom_components.patrimony_collection.config_flow import (
    PatrimonyCollectionConfigFlow,
    _timezone_ok,
)
from custom_components.patrimony_collection.const import (
    CONF_ALREADY_HAVE_PROPERTY_ID,
    CONF_CARDS,
    CONF_DISPLAY_NAME,
    CONF_HOUSE_EVENT_KEY,
    CONF_LOCATION_LABEL,
    CONF_MAPPINGS,
    CONF_PROPERTY_ID,
    CONF_TIMEZONE,
)
from custom_components.patrimony_collection.mapping import is_uuid

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "patrimony_collection"

KNOWN_ID = "550e8400-e29b-41d4-a716-446655440000"


def _run(coro):
    return asyncio.run(coro)


def _schema_keys(schema) -> list[str]:
    keys: list[str] = []
    for key in schema.schema:
        raw = getattr(key, "schema", key)
        keys.append(str(raw))
    return keys


def test_user_form_omits_property_id() -> None:
    flow = PatrimonyCollectionConfigFlow()
    result = _run(flow.async_step_user())
    assert result["step_id"] == "user"
    keys = _schema_keys(result["data_schema"])
    assert CONF_PROPERTY_ID not in keys
    assert CONF_DISPLAY_NAME in keys
    assert CONF_TIMEZONE in keys
    assert CONF_LOCATION_LABEL in keys
    assert CONF_ALREADY_HAVE_PROPERTY_ID in keys


def test_new_house_mints_uuid_and_shows_confirm() -> None:
    flow = PatrimonyCollectionConfigFlow()
    result = _run(
        flow.async_step_user(
            {
                CONF_DISPLAY_NAME: "Demo Home",
                CONF_TIMEZONE: "UTC",
                CONF_LOCATION_LABEL: "Example",
            }
        )
    )
    assert result["step_id"] == "confirm"
    minted = result["description_placeholders"][CONF_PROPERTY_ID]
    assert is_uuid(minted)
    assert minted == minted.lower()

    created = _run(flow.async_step_confirm({}))
    assert created["title"] == f"Demo Home ({minted})"
    assert created["data"][CONF_PROPERTY_ID] == minted
    assert created["data"][CONF_DISPLAY_NAME] == "Demo Home"
    assert created["data"][CONF_TIMEZONE] == "UTC"
    assert created["data"][CONF_LOCATION_LABEL] == "Example"
    assert created["options"] == {
        CONF_CARDS: [],
        CONF_MAPPINGS: [],
        CONF_HOUSE_EVENT_KEY: None,
    }


def test_new_house_reuses_minted_id_if_confirm_is_reshown() -> None:
    flow = PatrimonyCollectionConfigFlow()
    first = _run(
        flow.async_step_user({CONF_DISPLAY_NAME: "Demo Home", CONF_TIMEZONE: "UTC"})
    )
    minted = first["description_placeholders"][CONF_PROPERTY_ID]
    second = _run(flow.async_step_user({CONF_DISPLAY_NAME: "Demo Home", CONF_TIMEZONE: "UTC"}))
    assert second["description_placeholders"][CONF_PROPERTY_ID] == minted


def test_existing_uuid_path() -> None:
    flow = PatrimonyCollectionConfigFlow()
    result = _run(
        flow.async_step_user(
            {
                CONF_DISPLAY_NAME: "Demo Home",
                CONF_TIMEZONE: "Europe/Paris",
                CONF_ALREADY_HAVE_PROPERTY_ID: True,
            }
        )
    )
    assert result["step_id"] == "existing"
    assert CONF_PROPERTY_ID in _schema_keys(result["data_schema"])

    created = _run(flow.async_step_existing({CONF_PROPERTY_ID: KNOWN_ID.upper()}))
    assert created["data"][CONF_PROPERTY_ID] == KNOWN_ID
    assert created["title"] == f"Demo Home ({KNOWN_ID})"
    assert CONF_LOCATION_LABEL not in created["data"]


def test_existing_invalid_uuid() -> None:
    flow = PatrimonyCollectionConfigFlow()
    _run(
        flow.async_step_user(
            {
                CONF_DISPLAY_NAME: "Demo Home",
                CONF_TIMEZONE: "UTC",
                CONF_ALREADY_HAVE_PROPERTY_ID: True,
            }
        )
    )
    result = _run(flow.async_step_existing({CONF_PROPERTY_ID: "not-a-uuid"}))
    assert result["step_id"] == "existing"
    assert result["errors"][CONF_PROPERTY_ID] == "invalid_uuid"


def test_user_validates_name_and_timezone() -> None:
    flow = PatrimonyCollectionConfigFlow()
    result = _run(
        flow.async_step_user({CONF_DISPLAY_NAME: "   ", CONF_TIMEZONE: "notzone"})
    )
    assert result["step_id"] == "user"
    assert result["errors"][CONF_DISPLAY_NAME] == "empty"
    assert result["errors"][CONF_TIMEZONE] == "invalid_timezone"
    assert CONF_PROPERTY_ID not in _schema_keys(result["data_schema"])


def test_timezone_utc_is_accepted() -> None:
    assert _timezone_ok("UTC")
    assert _timezone_ok("utc")
    assert _timezone_ok("Europe/Paris")
    assert not _timezone_ok("notzone")


def test_already_configured_aborts(monkeypatch) -> None:
    from custom_components.patrimony_collection import config_flow as cf

    monkeypatch.setattr(
        cf.config_entries.ConfigFlow,
        "_async_current_entries",
        lambda self: [object()],
        raising=False,
    )
    flow = PatrimonyCollectionConfigFlow()
    result = _run(flow.async_step_user())
    assert result["reason"] == "already_configured"


def test_strings_do_not_tell_first_time_installers_to_invent_a_uuid() -> None:
    for name in ("strings.json", "translations/en.json"):
        payload = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        user = payload["config"]["step"]["user"]
        assert CONF_PROPERTY_ID not in user["data"]
        assert "already_have_property_id" in user["data"]
        lowered = user["description"].lower()
        assert "we create the property id" in lowered
        assert "must match the backend" not in lowered
        assert "existing" in payload["config"]["step"]
        assert "confirm" in payload["config"]["step"]
        assert "{property_id}" in payload["config"]["step"]["confirm"]["description"]
