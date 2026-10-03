"""House display name and timezone stored on the config entry.

The panel and the presentation document already read these fields.
Saving here does not notify phones, does not emit a rename command,
and does not read an iOS session name back into the house.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config_flow import _timezone_ok
from .const import CONF_DISPLAY_NAME, CONF_TIMEZONE, MAPPING_FILE

DISPLAY_NAME_MAX = 120


def ha_instance_timezone(hass) -> str:
    """This Home Assistant instance's timezone. UTC only when it is missing."""
    config = getattr(hass, "config", None) if hass is not None else None
    raw = None
    if config is not None:
        raw = getattr(config, "time_zone", None)
        if not raw and isinstance(config, dict):
            raw = config.get("time_zone")
    name = str(raw or "").strip()
    if name and _timezone_ok(name):
        return name
    return "UTC"


def house_identity_document(entry) -> dict[str, str]:
    """Stored config-entry values only. Not a phone payload. Not mapping.json."""
    data = dict(getattr(entry, "data", None) or {})
    return {
        "displayName": str(data.get(CONF_DISPLAY_NAME) or ""),
        "timezone": str(data.get(CONF_TIMEZONE) or ""),
    }


def apply_house_identity(hass, entry, payload: dict | None) -> tuple[dict[str, Any], int]:
    """Update stored display name and/or timezone.

    ``copyTimezoneFromHomeAssistant`` writes this instance's timezone.
    Phone fields (device name, session) are ignored. Nothing is notified.
    """
    payload = payload if isinstance(payload, dict) else {}
    data = dict(getattr(entry, "data", None) or {})

    name_update = None
    if "displayName" in payload or "display_name" in payload:
        raw_name = payload["displayName"] if "displayName" in payload else payload["display_name"]
        name = str(raw_name or "").strip()
        if not name:
            return {"error": {"code": "empty", "message": "Display name is required"}}, 400
        if len(name) > DISPLAY_NAME_MAX:
            return {"error": {"code": "too_long", "message": "Display name is too long"}}, 400
        name_update = name

    copy = bool(
        payload.get("copyTimezoneFromHomeAssistant")
        or payload.get("copy_timezone_from_home_assistant")
    )
    zone_update = None
    if copy or "timezone" in payload:
        if copy:
            zone_update = ha_instance_timezone(hass)
        else:
            zone = str(payload.get("timezone") or "").strip()
            if not _timezone_ok(zone):
                return {
                    "error": {
                        "code": "invalid_timezone",
                        "message": "Use an IANA timezone such as UTC.",
                    }
                }, 400
            zone_update = zone

    if name_update is not None:
        data[CONF_DISPLAY_NAME] = name_update
    if zone_update is not None:
        data[CONF_TIMEZONE] = zone_update
    if name_update is not None or zone_update is not None:
        _persist_entry_data(hass, entry, data)
        _patch_mapping_identity(hass, data)

    body = {
        "ok": True,
        "displayName": str(data.get(CONF_DISPLAY_NAME) or ""),
        "timezone": str(data.get(CONF_TIMEZONE) or ""),
        "homeAssistantTimezone": ha_instance_timezone(hass),
    }
    return body, 200


def _persist_entry_data(hass, entry, data: dict) -> None:
    entries = getattr(hass, "config_entries", None)
    updater = getattr(entries, "async_update_entry", None) if entries is not None else None
    if updater:
        updater(entry, data=data)
        return
    entry.data = data


def _patch_mapping_identity(hass, data: dict) -> None:
    """Keep an existing mapping file aligned. Do not create one. Do not touch cards."""
    config = getattr(hass, "config", None)
    path_fn = getattr(config, "path", None) if config is not None else None
    if not callable(path_fn):
        return
    try:
        raw = path_fn(MAPPING_FILE)
    except Exception:
        return
    path = Path(raw)
    if not path.is_file():
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(existing, dict):
        return
    existing["display_name"] = data.get(CONF_DISPLAY_NAME)
    existing["timezone"] = data.get(CONF_TIMEZONE)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
