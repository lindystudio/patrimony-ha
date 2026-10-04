"""House call list. Off PresentationDocument. Order is array order.

Each row carries `method` (cellular, viber, or whatsapp, which every app build
understands) and `app` (how the person is really reached, e.g. telegram).
When `app` is not one of the methods, `method` is cellular.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from .const import CONTACT_APP_PATTERN, CONTACTS_FILE, CONTACTS_METHODS, CONTACTS_PATH, MAX_CONTACTS


def contacts_path(hass) -> Path:
    try:
        return Path(hass.config.path(CONTACTS_FILE))
    except Exception:
        return Path("/config") / CONTACTS_FILE


def _digits(tel: str) -> str:
    return "".join(ch for ch in tel if ch.isdigit() or ch == "+")


def normalize_contact(row: Any) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    function = str(row.get("function") or row.get("title") or "").strip()
    name = str(row.get("name") or "").strip()
    tel = str(row.get("tel") or row.get("phone") or "").strip()
    app = str(row.get("app") or row.get("method") or "cellular").strip().lower()
    if not re.fullmatch(CONTACT_APP_PATTERN, app):
        app = "cellular"
    method = app if app in CONTACTS_METHODS else "cellular"
    cid = str(row.get("id") or "").strip() or str(uuid4())
    if not function and not name and not _digits(tel):
        return None
    return {
        "id": cid,
        "function": function[:80],
        "name": name[:80],
        "tel": tel[:40],
        "method": method,
        "app": app,
    }


def keep_stored_apps(incoming: Any, stored: list[dict[str, str]]) -> Any:
    """An older phone sends only `method`. Keep the stored `app` for that row while
    it still maps to the same method, so its save does not erase a newer phone's choice."""
    if not isinstance(incoming, list):
        return incoming
    by_id = {row["id"]: row for row in stored}
    out = []
    for row in incoming:
        if isinstance(row, dict) and "app" not in row:
            previous = by_id.get(str(row.get("id") or "").strip())
            sent = str(row.get("method") or "cellular").strip().lower()
            if previous and previous.get("method") == sent:
                row = {**row, "app": previous.get("app", sent)}
        out.append(row)
    return out


def normalize_list(raw: Any) -> list[dict[str, str]]:
    rows = raw if isinstance(raw, list) else []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        item = normalize_contact(row)
        if item is None:
            continue
        if item["id"] in seen:
            item["id"] = str(uuid4())
        seen.add(item["id"])
        out.append(item)
        if len(out) >= MAX_CONTACTS:
            break
    return out


def load_contacts(hass) -> list[dict[str, str]]:
    path = contacts_path(hass)
    try:
        if not path.is_file():
            return []
        data = json.loads(path.read_text())
    except Exception:
        return []
    if isinstance(data, dict):
        return normalize_list(data.get("contacts"))
    return normalize_list(data)


def save_contacts(hass, contacts: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = normalize_list(keep_stored_apps(contacts, load_contacts(hass)))
    path = contacts_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schemaVersion": 1, "contacts": rows}
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return rows


def document(hass, property_id: str | None) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "propertyId": property_id,
        "contacts": load_contacts(hass),
    }
