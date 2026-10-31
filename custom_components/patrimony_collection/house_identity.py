"""House display name and timezone stored on the config entry.

The panel and the presentation document already read these fields.
Saving here does not notify phones, does not emit a rename command,
and does not read an iOS session name back into the house.
"""

from __future__ import annotations

from . import fileio

import json
from pathlib import Path
from typing import Any

from .config_flow import _timezone_ok
from .const import CONF_DISPLAY_NAME, CONF_TIMEZONE, MAPPING_FILE

DISPLAY_NAME_MAX = 120


_TZ_NAMES: list[str] | None = None


def _load_iana_timezone_names() -> list[str]:
    """Blocking: available_timezones() walks tzdata / /usr/share/zoneinfo. Executor only."""
    names: set[str] = set()
    try:
        from zoneinfo import available_timezones

        names = {str(name).strip() for name in available_timezones() if str(name).strip()}
    except Exception:
        names = set()
    names.add("UTC")
    return sorted(names)


def iana_timezone_names() -> list[str]:
    """The standard zoneinfo set already on this machine. Not a made-up list.

    0.4.109: cached. The async panel views warm it off the event loop first
    (async_iana_timezone_names); a cold sync call (tests, config flow) loads inline.
    """
    global _TZ_NAMES
    if _TZ_NAMES is None:
        _TZ_NAMES = _load_iana_timezone_names()
    return list(_TZ_NAMES)


async def async_iana_timezone_names(hass) -> list[str]:
    """0.4.109: load the zoneinfo list in the executor (no blocking call in the event loop)."""
    global _TZ_NAMES
    if _TZ_NAMES is None:
        runner = getattr(hass, "async_add_executor_job", None) if hass is not None else None
        names = await runner(_load_iana_timezone_names) if runner is not None else _load_iana_timezone_names()
        _TZ_NAMES = names
    return list(_TZ_NAMES)


def timezone_choices(stored: str | None, ha_zone: str | None) -> list[str]:
    """IANA names, plus the stored value and this instance's timezone if missing."""
    names = set(iana_timezone_names())
    for raw in (stored, ha_zone):
        name = str(raw or "").strip()
        if name:
            names.add(name)
    return sorted(names)


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


def house_identity_payload(hass, entry) -> dict[str, Any]:
    """Panel document: stored name and timezone, plus the timezone list."""
    body: dict[str, Any] = dict(house_identity_document(entry))
    ha = ha_instance_timezone(hass)
    body["homeAssistantTimezone"] = ha
    body["timezones"] = timezone_choices(body.get("timezone"), ha)
    return body


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
            stored = str(data.get(CONF_TIMEZONE) or "").strip()
            # The panel posts a list value. Keep an unusual stored name so the
            # selection is not blank, and still reject a typed string that is
            # neither stored nor a real zone.
            allowed = set(timezone_choices(stored, ha_instance_timezone(hass)))
            if not zone or (zone not in allowed and not _timezone_ok(zone)):
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

    ha = ha_instance_timezone(hass)
    body = {
        "ok": True,
        "displayName": str(data.get(CONF_DISPLAY_NAME) or ""),
        "timezone": str(data.get(CONF_TIMEZONE) or ""),
        "homeAssistantTimezone": ha,
        "timezones": timezone_choices(data.get(CONF_TIMEZONE), ha),
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
    text = fileio.read_text(path)
    if text is None:
        return
    try:
        existing = json.loads(text)
    except Exception:
        return
    if not isinstance(existing, dict):
        return
    existing["display_name"] = data.get(CONF_DISPLAY_NAME)
    existing["timezone"] = data.get(CONF_TIMEZONE)
    fileio.write_text(path, json.dumps(existing, indent=2) + "\n")
