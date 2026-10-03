"""Paired iOS clients for this house.

One row per credential (JWT iss / refresh-token id). Lives in ios_session.json
under ``clients`` so Connections' last-write-wins fields stay intact.

Phone contract (same HA Bearer as GET /api/patrimony_collection/state):

  GET /api/patrimony_collection/pairing  → always 200 while auth works
    active:   {status, propertyId, clientId, displayName}
    unpaired: {status, propertyId, clientId, reason: "unpaired"}

  GET /api/patrimony_collection/state also includes the same object under
  the key ``pairing``.

Unpair marks THAT client only. The unpaired 200 is the wipe signal; the
credential is revoked only after that response has been served once.
Does not delete the server house, other clients, house display name,
timezone, or house event key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .const import (
    CONF_DISPLAY_NAME,
    CONF_PROPERTY_ID,
    CONF_TIMEZONE,
    DOMAIN,
    IOS_DEVICE_NAME_MAX,
    PAIR_CLIENT_NAME,
)
from .ios_session import (
    GENERIC_DEVICE_NAMES,
    ios_session_path,
    is_generic_device_name,
    load_ios_session,
)

_LOGGER = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_UNPAIRED = "unpaired"
FALLBACK_DISPLAY = "Paired phone"
RESERVED_NAMES = frozenset({*GENERIC_DEVICE_NAMES, "admin", "home assistant"})


def _utc_z(now: datetime | None = None) -> str:
    stamp = now if now is not None else datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_entry(hass):
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:
        return None
    return entries[0] if entries else None


def property_id(hass) -> str:
    entry = _first_entry(hass)
    if entry is None:
        return ""
    data = getattr(entry, "data", None) or {}
    return str(data.get(CONF_PROPERTY_ID) or "").strip()


def house_timezone_name(hass) -> str:
    """Stored house timezone, else HA timezone, else UTC (same idea as chat)."""
    entry = _first_entry(hass)
    if entry is not None:
        data = getattr(entry, "data", None) or {}
        zone = str(data.get(CONF_TIMEZONE) or "").strip()
        if zone:
            return zone
    cfg = getattr(hass, "config", None)
    ha = str(getattr(cfg, "time_zone", None) or "").strip() if cfg is not None else ""
    return ha or "UTC"


def format_in_house_tz(hass, iso_z: str | None) -> str:
    raw = str(iso_z or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    name = house_timezone_name(hass)
    try:
        zone = ZoneInfo(name)
    except Exception:
        zone = timezone.utc
        name = "UTC"
    local = dt.astimezone(zone)
    return local.strftime("%Y-%m-%d %H:%M") + f" ({name})"


def _bearer(request) -> str:
    headers = getattr(request, "headers", None) or {}
    try:
        header = headers.get("Authorization") or headers.get("authorization") or ""
    except Exception:
        header = ""
    text = str(header).strip()
    if text.lower().startswith("bearer "):
        return text.split(" ", 1)[1].strip()
    return ""


def _jwt_iss(token: str) -> str | None:
    import base64

    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    pad = "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    iss = payload.get("iss")
    if not isinstance(iss, str) or not iss.strip():
        return None
    return iss.strip()


def request_client_id(request) -> str | None:
    """Stable id for THIS credential: JWT iss (HA refresh-token id)."""
    return _jwt_iss(_bearer(request))


def _safe_display(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    if not text or len(text) > IOS_DEVICE_NAME_MAX:
        return None
    lowered = text.lower()
    if "hek_" in lowered or "eyj" in lowered or "secret" in lowered or "token" in lowered:
        return None
    return text


def is_reserved_display_name(name: str | None) -> bool:
    text = (name or "").strip().casefold()
    return not text or text in RESERVED_NAMES or is_generic_device_name(text)


def seed_display_name(*candidates: Any, ha_username: str | None = None) -> str:
    """First non-reserved real device name. Never iPhone / admin / HA user."""
    ha = (ha_username or "").strip().casefold()
    for raw in candidates:
        name = _safe_display(raw)
        if name is None:
            continue
        if is_reserved_display_name(name):
            continue
        if ha and name.casefold() == ha:
            continue
        return name
    return FALLBACK_DISPLAY


def _read_store(hass) -> dict[str, Any]:
    raw = load_ios_session(hass)
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _write_store(hass, data: dict[str, Any]) -> None:
    path = ios_session_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _clients_list(store: dict[str, Any]) -> list[dict[str, Any]]:
    rows = store.get("clients")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict) and str(row.get("clientId") or "").strip():
            out.append(row)
    return out


def _normalize_client(row: dict[str, Any]) -> dict[str, Any]:
    client_id = str(row.get("clientId") or "").strip()
    display = _safe_display(row.get("displayName")) or FALLBACK_DISPLAY
    status = str(row.get("status") or STATUS_ACTIVE).strip()
    if status not in (STATUS_ACTIVE, STATUS_UNPAIRED):
        status = STATUS_ACTIVE
    return {
        "clientId": client_id,
        "displayName": display,
        "nameEdited": bool(row.get("nameEdited")),
        "pairedAt": str(row.get("pairedAt") or "").strip() or _utc_z(),
        "lastAccessAt": str(row.get("lastAccessAt") or "").strip() or _utc_z(),
        "status": status,
        "unpairServed": bool(row.get("unpairServed")),
        "deviceName": _safe_display(row.get("deviceName")) or "",
        "appVersion": str(row.get("appVersion") or "").strip()[:40],
    }


def load_clients(hass) -> list[dict[str, Any]]:
    store = _read_store(hass)
    return [_normalize_client(row) for row in _clients_list(store)]


def get_client(hass, client_id: str | None) -> dict[str, Any] | None:
    ident = str(client_id or "").strip()
    if not ident:
        return None
    for row in load_clients(hass):
        if row["clientId"] == ident:
            return row
    return None


def _save_clients(hass, clients: list[dict[str, Any]], store: dict[str, Any] | None = None) -> None:
    data = dict(store) if store is not None else _read_store(hass)
    data["clients"] = [_normalize_client(row) for row in clients if str(row.get("clientId") or "").strip()]
    _write_store(hass, data)


def upsert_client(
    hass,
    client_id: str,
    *,
    device_name: str | None = None,
    app_version: str | None = None,
    ha_username: str | None = None,
    touch_access: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create or update a paired credential row. Does not overwrite an edited name."""
    ident = str(client_id or "").strip()
    if not ident:
        raise ValueError("clientId required")
    store = _read_store(hass)
    clients = [_normalize_client(row) for row in _clients_list(store)]
    stamp = _utc_z(now)
    found = None
    for row in clients:
        if row["clientId"] == ident:
            found = row
            break
    seed = seed_display_name(
        device_name,
        store.get("deviceName") if found is None else None,
        ha_username=ha_username,
    )
    if found is None:
        found = {
            "clientId": ident,
            "displayName": seed,
            "nameEdited": False,
            "pairedAt": stamp,
            "lastAccessAt": stamp,
            "status": STATUS_ACTIVE,
            "unpairServed": False,
            "deviceName": _safe_display(device_name) or "",
            "appVersion": str(app_version or "").strip()[:40],
        }
        clients.append(found)
    else:
        if found["status"] != STATUS_UNPAIRED:
            found["status"] = STATUS_ACTIVE
        if touch_access:
            found["lastAccessAt"] = stamp
        if app_version:
            found["appVersion"] = str(app_version).strip()[:40]
        if device_name:
            safe = _safe_display(device_name) or ""
            if safe:
                found["deviceName"] = safe
            if not found["nameEdited"]:
                # Seed once from a real device name; never replace with reserved.
                if is_reserved_display_name(found["displayName"]) or found["displayName"] == FALLBACK_DISPLAY:
                    better = seed_display_name(device_name, ha_username=ha_username)
                    if better != FALLBACK_DISPLAY:
                        found["displayName"] = better
        # Edited names are never overwritten by later deviceName posts.
    _save_clients(hass, clients, store)
    return _normalize_client(found)


def rename_client(hass, client_id: str, display_name: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Panel rename. Blank keeps the previous name. Max 80. Marks nameEdited."""
    ident = str(client_id or "").strip()
    row = get_client(hass, ident)
    if row is None or row["status"] != STATUS_ACTIVE:
        return None, "not_found"
    text = "" if display_name is None else str(display_name).strip()
    if not text:
        return row, None
    if len(text) > IOS_DEVICE_NAME_MAX:
        return None, "too_long"
    safe = _safe_display(text)
    if safe is None:
        return None, "invalid"
    store = _read_store(hass)
    clients = [_normalize_client(c) for c in _clients_list(store)]
    for c in clients:
        if c["clientId"] == ident:
            c["displayName"] = safe
            c["nameEdited"] = True
            _save_clients(hass, clients, store)
            return _normalize_client(c), None
    return None, "not_found"


def mark_unpaired(hass, client_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Panel unpair. Marks THAT client only. Does not revoke yet."""
    ident = str(client_id or "").strip()
    store = _read_store(hass)
    clients = [_normalize_client(c) for c in _clients_list(store)]
    found = None
    for c in clients:
        if c["clientId"] == ident:
            c["status"] = STATUS_UNPAIRED
            c["unpairServed"] = False
            c["lastAccessAt"] = _utc_z(now)
            found = c
            break
    if found is None:
        return None
    _save_clients(hass, clients, store)
    return _normalize_client(found)


def _token_by_id(hass, token_id: str):
    from .notify import _iter_refresh_tokens

    ident = str(token_id or "").strip()
    if not ident:
        return None
    auth = getattr(hass, "auth", None)
    direct = getattr(auth, "refresh_tokens", None) if auth is not None else None
    if isinstance(direct, dict) and ident in direct:
        return direct.get(ident)
    try:
        for tok in _iter_refresh_tokens(hass):
            tid = tok.get("id") if isinstance(tok, dict) else getattr(tok, "id", None)
            if str(tid or "").strip() == ident:
                return tok
    except Exception:
        return None
    return None


async def _revoke_refresh_token(hass, client_id: str) -> None:
    """Drop the HA refresh token for this client. Fail-open. Never logs secrets."""
    tok = _token_by_id(hass, client_id)
    if tok is None:
        return
    auth = getattr(hass, "auth", None)
    if auth is None:
        return
    remove = getattr(auth, "async_remove_refresh_token", None)
    if remove is None:
        remove = getattr(auth, "async_delete_refresh_token", None)
    if remove is None:
        return
    try:
        result = remove(tok)
        if hasattr(result, "__await__"):
            await result
    except Exception:
        _LOGGER.warning("patrimony_client_revoke_failed")


def _drop_client_row(hass, client_id: str) -> None:
    store = _read_store(hass)
    clients = [c for c in _clients_list(store) if str(c.get("clientId") or "").strip() != client_id]
    _save_clients(hass, clients, store)


async def note_unpair_served(hass, client_id: str) -> None:
    """After unpaired 200 is returned once: revoke credential and drop the row."""
    ident = str(client_id or "").strip()
    store = _read_store(hass)
    clients = [_normalize_client(c) for c in _clients_list(store)]
    found = None
    for c in clients:
        if c["clientId"] == ident and c["status"] == STATUS_UNPAIRED:
            if c["unpairServed"]:
                return
            c["unpairServed"] = True
            found = c
            break
    if found is None:
        return
    _save_clients(hass, clients, store)
    await _revoke_refresh_token(hass, ident)
    _drop_client_row(hass, ident)


def pairing_document(hass, request, *, now: datetime | None = None) -> dict[str, Any]:
    """Phone-facing pairing object. Does not register panel sessions."""
    pid = property_id(hass)
    client_id = request_client_id(request) or ""
    row = get_client(hass, client_id) if client_id else None
    if row is not None and row["status"] == STATUS_UNPAIRED:
        return {
            "status": STATUS_UNPAIRED,
            "propertyId": pid,
            "clientId": client_id,
            "reason": "unpaired",
        }
    display = ""
    if row is not None:
        display = row["displayName"]
        # Touch last access for known phones on pairing/state reads.
        upsert_client(hass, client_id, touch_access=True, now=now)
        row = get_client(hass, client_id) or row
        display = row["displayName"]
    return {
        "status": STATUS_ACTIVE,
        "propertyId": pid,
        "clientId": client_id,
        "displayName": display,
    }


def panel_clients_document(hass) -> dict[str, Any]:
    """Active paired phones for the panel Phones sub-tab."""
    zone = house_timezone_name(hass)
    rows = []
    for row in load_clients(hass):
        if row["status"] != STATUS_ACTIVE:
            continue
        rows.append(
            {
                "clientId": row["clientId"],
                "displayName": row["displayName"],
                "pairedAt": row["pairedAt"],
                "lastAccessAt": row["lastAccessAt"],
                "pairedAtDisplay": format_in_house_tz(hass, row["pairedAt"]),
                "lastAccessAtDisplay": format_in_house_tz(hass, row["lastAccessAt"]),
            }
        )
    rows.sort(key=lambda r: r.get("lastAccessAt") or "", reverse=True)
    return {"clients": rows, "timezone": zone}


def display_name_for_client(hass, client_id: str | None) -> str | None:
    row = get_client(hass, client_id)
    if row is None or row["status"] != STATUS_ACTIVE:
        return None
    name = _safe_display(row.get("displayName"))
    return name


def client_sender_label(hass, request, posted_device_name: Any = None) -> dict[str, str] | None:
    """Resolve chat sender from the paired client registry.

    Returns None when this credential is not a registered phone (panel path).
    Honors an edited display name over a later POST deviceName.
    """
    client_id = request_client_id(request)
    posted = _safe_display(posted_device_name)
    user = None
    getter = getattr(request, "get", None) if request is not None else None
    if callable(getter):
        try:
            user = getter("hass_user")
        except Exception:
            user = None
    if user is None and request is not None:
        user = getattr(request, "hass_user", None)
    ha_name = ""
    if user is not None:
        ha_name = str(
            user.get("name") if isinstance(user, dict) else getattr(user, "name", "") or ""
        ).strip()

    if client_id:
        row = get_client(hass, client_id)
        if row is not None and row["status"] == STATUS_ACTIVE:
            if row.get("nameEdited"):
                label = row["displayName"]
            else:
                # Not edited: keep stored name; never replace with reserved posted names.
                if posted and not is_reserved_display_name(posted) and (
                    posted.casefold() != ha_name.casefold() if ha_name else True
                ):
                    # First-seen / improve seed only when current is fallback/reserved.
                    if is_reserved_display_name(row["displayName"]) or row["displayName"] == FALLBACK_DISPLAY:
                        upsert_client(
                            hass,
                            client_id,
                            device_name=posted,
                            ha_username=ha_name,
                            touch_access=True,
                        )
                        row = get_client(hass, client_id) or row
                label = row["displayName"]
            return {"kind": "ios", "label": label, "key": "ios:" + client_id}
        if posted and not is_reserved_display_name(posted):
            # First chat contact with a real deviceName registers this credential.
            upsert_client(
                hass,
                client_id,
                device_name=posted,
                ha_username=ha_name,
                touch_access=True,
            )
            row = get_client(hass, client_id)
            if row is not None:
                return {
                    "kind": "ios",
                    "label": row["displayName"],
                    "key": "ios:" + client_id,
                }
    return None


def active_display_names(hass) -> list[str]:
    """Labels for push-log local clients. Active rows only."""
    names: list[str] = []
    for row in load_clients(hass):
        if row["status"] != STATUS_ACTIVE:
            continue
        name = _safe_display(row.get("displayName"))
        if name and name not in names:
            names.append(name)
    return names
