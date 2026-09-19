"""House call list. Off PresentationDocument. Order is array order."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .const import CONTACTS_FILE, CONTACTS_METHODS, CONTACTS_PATH, MAX_CONTACTS


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
    method = str(row.get("method") or "cellular").strip().lower()
    if method not in CONTACTS_METHODS:
        method = "cellular"
    cid = str(row.get("id") or "").strip() or str(uuid4())
    if not function and not name and not _digits(tel):
        return None
    return {
        "id": cid,
        "function": function[:80],
        "name": name[:80],
        "tel": tel[:40],
        "method": method,
    }


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
    rows = normalize_list(contacts)
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
