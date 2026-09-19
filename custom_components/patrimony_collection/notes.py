"""House notepad. Off PresentationDocument. Last write wins."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .const import NOTES_FILE, NOTES_MAX_CHARS, SCHEMA_VERSION


def notes_path(hass) -> Path:
    try:
        return Path(hass.config.path(NOTES_FILE))
    except Exception:
        return Path("/config") / NOTES_FILE


def _utc_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: str) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) > NOTES_MAX_CHARS:
        return text[:NOTES_MAX_CHARS]
    return text


def load_notes(hass) -> dict[str, Any] | None:
    path = notes_path(hass)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def document(hass, property_id: str | None) -> dict[str, Any]:
    raw = load_notes(hass)
    if raw is None:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "propertyId": property_id,
            "text": "",
            "updatedAt": None,
        }
    text = _clip(str(raw.get("text") if raw.get("text") is not None else ""))
    updated = raw.get("updatedAt")
    if updated is not None:
        updated = str(updated)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "propertyId": property_id,
        "text": text,
        "updatedAt": updated,
    }


def save_notes(hass, text: Any, property_id: str | None) -> dict[str, Any]:
    clipped = _clip("" if text is None else str(text))
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "propertyId": property_id,
        "text": clipped,
        "updatedAt": _utc_z(),
    }
    path = notes_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return payload
