"""C9: adopt an existing (offline-home) property id on a never-claimed HA.

The phone POSTs the offline homeId here, then claims it on Backend with
``upgrade: true``. HA never calls or probes the backend. Once the entry has
any sign of a backend claim it is latched (``claimed_at``) and a different id
is refused with 409 ``property_id_conflict``. Responses and the adopt log
never carry keys or tokens.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from .const import (
    ADOPT_LOG_MAX,
    ADOPT_RATE_MAX,
    ADOPT_RATE_WINDOW_SECONDS,
    CONF_ADOPT_LOCK_MIGRATED,
    CONF_CLAIMED_AT,
    CONF_DISPLAY_NAME,
    CONF_HOUSE_EVENT_KEY,
    CONF_PROPERTY_ID,
    CONF_PROPERTY_ID_HISTORY,
    CONF_PROPERTY_ID_SOURCE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

ADOPT_LOG_STORE = "adoptLog"
KEY_INSTALL_CLIENTS_STORE = "keyInstallClients"
HISTORY_MAX = 5

OUTCOME_ADOPTED = "adopted"
OUTCOME_UNCHANGED = "unchanged"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _is_uuid(value: Any) -> bool:
    from .mapping import is_uuid

    return isinstance(value, str) and is_uuid(value)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# ---------------- store (existing ios_session.json store) ----------------

def _read_store(hass) -> dict[str, Any]:
    from .clients import _read_store as read

    return read(hass)


def _write_store(hass, data: dict[str, Any]) -> None:
    from .clients import _write_store as write

    write(hass, data)


def adopt_log(hass) -> list[dict[str, Any]]:
    rows = _read_store(hass).get(ADOPT_LOG_STORE)
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)][-ADOPT_LOG_MAX:]


def record_adopt_event(
    hass,
    *,
    previous_id: str | None,
    new_id: str | None,
    client_id: str | None,
    outcome: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Append one adopt event (success or refusal). Ids only; never tokens/keys."""
    row = {
        "at": at or _now_iso(),
        "previousPropertyId": previous_id if _is_uuid(previous_id) else None,
        # Only a well-formed uuid is stored, so arbitrary body text never lands here.
        "newPropertyId": new_id if _is_uuid(new_id) else None,
        "clientId": (str(client_id)[:64] if client_id else None),
        "outcome": str(outcome)[:40],
    }
    try:
        data = _read_store(hass)
        rows = data.get(ADOPT_LOG_STORE)
        rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        rows.append(row)
        data[ADOPT_LOG_STORE] = rows[-ADOPT_LOG_MAX:]
        _write_store(hass, data)
    except Exception:
        _LOGGER.warning("patrimony_adopt_log_write_failed")
    return row


def note_key_install_client(hass, client_id: str | None) -> None:
    """Remember which paired client completed a hek_ install (claim evidence)."""
    if not client_id:
        return
    data = _read_store(hass)
    rows = data.get(KEY_INSTALL_CLIENTS_STORE)
    rows = [str(r) for r in rows if isinstance(r, str)] if isinstance(rows, list) else []
    if client_id not in rows:
        rows.append(str(client_id)[:64])
    data[KEY_INSTALL_CLIENTS_STORE] = rows[-20:]
    _write_store(hass, data)


# ---------------- claim evidence / latch ----------------

def claim_evidence(hass, entry, *, include_paired_clients: bool = False) -> list[str]:
    """Every sign that this entry's id was already claimed on Backend."""
    signs: list[str] = []
    options = dict(getattr(entry, "options", None) or {})
    if options.get(CONF_CLAIMED_AT):
        signs.append("claimed_at")
    if options.get(CONF_HOUSE_EVENT_KEY):
        signs.append("house_event_key")
    try:
        from .notify import EVENT_KEY_STATUS_STORE, _load_notify_doc

        doc = _load_notify_doc(hass)
        block = doc.get(EVENT_KEY_STATUS_STORE)
        if isinstance(block, dict):
            if block.get("installedAt"):
                signs.append("event_key_installed_at")
            if block.get("lastPushStatus"):
                signs.append("event_key_push_status")
        if isinstance(doc.get("pushLog"), list) and doc["pushLog"]:
            signs.append("push_log")
    except Exception:
        signs.append("notify_store_unreadable")  # fail closed
    try:
        store = _read_store(hass)
        if isinstance(store.get(KEY_INSTALL_CLIENTS_STORE), list) and store[KEY_INSTALL_CLIENTS_STORE]:
            signs.append("key_install_client")
        if include_paired_clients:
            rows = store.get("clients")
            if isinstance(rows, list) and any(isinstance(r, dict) and r.get("clientId") for r in rows):
                signs.append("paired_client")
    except Exception:
        signs.append("client_store_unreadable")
    return signs


def is_claimed(hass, entry) -> bool:
    return bool(claim_evidence(hass, entry))


def _update_entry(hass, entry, **kwargs) -> None:
    updater = getattr(hass.config_entries, "async_update_entry", None)
    if updater:
        updater(entry, **kwargs)
        return
    for key, value in kwargs.items():
        setattr(entry, key, value)


def migrate_adopt_lock(hass, entry) -> bool:
    """One-shot at setup, before any adopt is possible: latch already-claimed entries.

    Conservative: on the first run after upgrade, a pre-existing paired client
    also counts as evidence. Returns True when the entry is (now) latched.
    """
    options = dict(getattr(entry, "options", None) or {})
    if options.get(CONF_ADOPT_LOCK_MIGRATED):
        return bool(options.get(CONF_CLAIMED_AT))
    signs = claim_evidence(hass, entry, include_paired_clients=True)
    options[CONF_ADOPT_LOCK_MIGRATED] = True
    if signs and not options.get(CONF_CLAIMED_AT):
        options[CONF_CLAIMED_AT] = _now_iso()
    _update_entry(hass, entry, options=options)
    return bool(signs)


def stamp_claimed(hass, entry, client_id: str | None = None) -> None:
    """POST /event_key success → set the latch (never cleared)."""
    try:
        note_key_install_client(hass, client_id)
    except Exception:
        _LOGGER.warning("patrimony_key_install_client_write_failed")
    options = dict(getattr(entry, "options", None) or {})
    if options.get(CONF_CLAIMED_AT):
        return
    options[CONF_CLAIMED_AT] = _now_iso()
    options[CONF_ADOPT_LOCK_MIGRATED] = True
    _update_entry(hass, entry, options=options)


# ---------------- documents ----------------

def property_document(hass, entry) -> dict[str, Any]:
    options = dict(getattr(entry, "options", None) or {})
    source = options.get(CONF_PROPERTY_ID_SOURCE)
    if source not in ("minted", "adopted", "manual"):
        source = "minted"
    return {
        "propertyId": (entry.data or {}).get(CONF_PROPERTY_ID),
        "adoptable": not is_claimed(hass, entry),
        "source": source,
    }


def adopt_log_document(hass) -> dict[str, Any]:
    return {"adoptLog": list(reversed(adopt_log(hass))), "max": ADOPT_LOG_MAX}


# ---------------- rate limit (per client, in memory) ----------------

def _rate_ok(hass, client_id: str, now: float | None = None) -> bool:
    bucket = hass.data.setdefault(DOMAIN, {}).setdefault("_adopt_rate", {}) if isinstance(
        getattr(hass, "data", None), dict
    ) else _FALLBACK_RATE
    stamp = time.monotonic() if now is None else now
    hits = [t for t in bucket.get(client_id, []) if stamp - t < ADOPT_RATE_WINDOW_SECONDS]
    if len(hits) >= ADOPT_RATE_MAX:
        bucket[client_id] = hits
        return False
    hits.append(stamp)
    bucket[client_id] = hits
    return True


_FALLBACK_RATE: dict[str, list[float]] = {}


# ---------------- adopt ----------------

def _other_entry_has(hass, entry, property_id: str) -> bool:
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:
        return False
    for other in entries:
        if other is entry:
            continue
        if getattr(other, "unique_id", None) == property_id or (other.data or {}).get(CONF_PROPERTY_ID) == property_id:
            return True
    return False


def adopt(hass, entry, client_id: str | None, client_active: bool, payload: Any) -> tuple[int, dict[str, Any]]:
    """Validate + apply. Returns (status, body). Every outcome is logged."""
    previous = (entry.data or {}).get(CONF_PROPERTY_ID) if entry is not None else None
    raw_new = payload.get("propertyId") if isinstance(payload, dict) else None
    new_id = raw_new.strip().lower() if isinstance(raw_new, str) else None

    def refuse(status: int, code: str, message: str, extra: dict | None = None):
        record_adopt_event(
            hass, previous_id=previous, new_id=new_id, client_id=client_id, outcome=code
        )
        body = _error(code, message)
        if extra:
            body.update(extra)
        return status, body

    if not client_id or not client_active:
        return refuse(403, "not_patrimony_client", "Only an active paired Patrimony phone can set the property id")
    if not _rate_ok(hass, client_id):
        return refuse(429, "rate_limited", "Too many requests; retry later")
    if not isinstance(payload, dict) or not _is_uuid(new_id) or payload.get("upgrade") is not True:
        return refuse(400, "validation", "Body must be {\"propertyId\": <uuid>, \"upgrade\": true}")
    if new_id == previous:
        record_adopt_event(
            hass, previous_id=previous, new_id=new_id, client_id=client_id, outcome=OUTCOME_UNCHANGED
        )
        return 200, {"propertyId": new_id, "adopted": False}
    if is_claimed(hass, entry):
        return refuse(409, "property_id_conflict", "This Home Assistant already belongs to a house")
    if _other_entry_has(hass, entry, new_id):
        return refuse(409, "already_configured", "Another Patrimony entry already uses this property id")

    stamp = _now_iso()
    data = dict(entry.data or {})
    data[CONF_PROPERTY_ID] = new_id
    options = dict(entry.options or {})
    history = options.get(CONF_PROPERTY_ID_HISTORY)
    history = [h for h in history if isinstance(h, dict)] if isinstance(history, list) else []
    history.append({"id": previous, "at": stamp, "source": options.get(CONF_PROPERTY_ID_SOURCE) or "minted"})
    options[CONF_PROPERTY_ID_HISTORY] = history[-HISTORY_MAX:]
    options[CONF_PROPERTY_ID_SOURCE] = "adopted"
    name = str(data.get(CONF_DISPLAY_NAME) or "").strip() or "House"
    _update_entry(
        hass,
        entry,
        data=data,
        options=options,
        unique_id=new_id,
        title=f"{name} ({new_id})",
    )
    record_adopt_event(
        hass, previous_id=previous, new_id=new_id, client_id=client_id, outcome=OUTCOME_ADOPTED, at=stamp
    )
    return 200, {"propertyId": new_id, "adopted": True, "previousPropertyId": previous}
