"""House attention push. hek_ only. Never an HA token."""

from __future__ import annotations

from . import fileio

import json
import os
import re
from datetime import datetime
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from .attention import manual_notify_decision, plan_automatic_pushes, title_is_safe
from .const import (
    BACKEND_BASE,
    CONF_HOUSE_EVENT_KEY,
    NOTIFY_FILE,
    NOTIFY_TITLE_MAX,
    NOTIFY_USER_AGENT,
    PAIR_CLIENT_NAME,
)
from .ios_session import load_ios_session

_LOGGER = logging.getLogger(__name__)

MISSING_KEY = {
    "error": {
        "code": "missing_house_event_key",
        "message": "Push needs the house event key",
    }
}

# Event-key POST: one 400 code. Never include the submitted value.
INVALID_EVENT_KEY = {
    "error": {
        "code": "invalid_house_event_key",
        "message": "House event key must start with hek_",
    }
}


def notify_path(hass) -> Path:
    try:
        return Path(hass.config.path(NOTIFY_FILE))
    except Exception:
        return Path("/config") / NOTIFY_FILE


# notify.json holds cardId, attention baseline, eventKeyStatus and pushLog. Several
# executor threads (and the add-on process) read-modify-write it. Writes are atomic
# (tmp + os.replace) so no reader ever sees a torn file, and a file that exists but
# cannot be parsed is never replaced by a fresh document (that used to wipe the log).
_NOTIFY_LOCK = threading.RLock()


class NotifyDocUnreadable(Exception):
    """notify.json exists but is not a JSON object. Do not overwrite it."""


def _read_notify_file(path: Path) -> dict[str, Any]:
    for attempt in range(3):
        try:
            raw = fileio.read_text(path)
            if raw is None:
                return {}
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        fileio.maybe_sleep(0.05 * (attempt + 1))
    raise NotifyDocUnreadable(str(path))


def _write_notify_file(path: Path, data: dict[str, Any]) -> None:
    # Atomic (tmp + fsync + os.replace), serialized, cache write-through.
    fileio.write_text(path, json.dumps(data, indent=2) + "\n")


def _update_notify_doc(hass, mutate) -> bool:
    """Locked read-modify-write. Unreadable file → leave it alone, return False."""
    path = notify_path(hass)
    with _NOTIFY_LOCK:
        try:
            data = _read_notify_file(path)
        except NotifyDocUnreadable:
            _LOGGER.error("patrimony_notify_unreadable_not_overwritten")
            return False
        if mutate(data) is False:
            return True
        _write_notify_file(path, data)
        return True


def looks_like_ha_token(key: Any) -> bool:
    """JWT-shaped or token-like. Never log the value."""
    if key is None:
        return False
    text = str(key).strip()
    if not text:
        return False
    if text.startswith("eyJ") or "eyJ" in text:
        return True
    if "token" in text.lower():
        return True
    return False


def is_usable_house_event_key(key: Any) -> bool:
    if key is None:
        return False
    text = str(key).strip()
    if not text.startswith("hek_") or len(text) < 5:
        return False
    if looks_like_ha_token(text):
        return False
    return True


def event_key_error(raw: Any) -> dict[str, Any] | None:
    """400 invalid_house_event_key if unusable. Never echoes the submitted value."""
    if is_usable_house_event_key(raw):
        return None
    return dict(INVALID_EVENT_KEY)


def persist_house_event_key(options: dict | None, key: str) -> dict[str, Any]:
    """Copy options with CONF_HOUSE_EVENT_KEY set. Never logs the key."""
    out = dict(options or {})
    out[CONF_HOUSE_EVENT_KEY] = str(key).strip()
    return out


def apply_event_key_payload(
    options: dict | None, payload: Any
) -> tuple[dict[str, Any] | None, dict[str, Any], int]:
    """Validate phone POST body and return (options, body, status).

    Success is 200 `{configured: true}`. Invalid is 400 `invalid_house_event_key`
    and does not persist — configured stays false so the app can retry/rotate.
    Never echoes the key.
    """
    if not isinstance(payload, dict):
        return None, dict(INVALID_EVENT_KEY), 400
    raw = payload.get("houseEventKey")
    err = event_key_error(raw)
    if err:
        return None, err, 400
    stored = persist_house_event_key(options, str(raw).strip())
    return stored, {"configured": True}, 200


def clip_title(title: Any) -> str | None:
    text = "" if title is None else str(title).strip()
    if not text:
        return None
    if len(text) > NOTIFY_TITLE_MAX:
        text = text[:NOTIFY_TITLE_MAX]
    return text


def load_card_id(hass) -> str:
    """Stable cardId. Added to the existing document; never replaces it."""
    found: dict[str, str] = {}

    def mutate(data: dict[str, Any]):
        cid = str(data.get("cardId") or "").strip()
        if cid:
            found["cid"] = cid
            return False
        found["cid"] = data["cardId"] = str(uuid4())
        return True

    if not _update_notify_doc(hass, mutate):
        return str(uuid4())
    return found["cid"]


def events_url(property_id: str) -> str:
    return f"{BACKEND_BASE}/v1/properties/{property_id}/events"


def load_card_id_and_post(
    hass,
    property_id: str,
    key: str,
    title: str,
    timeout: int = 8,
    severity: str = "attention",
    manual: bool = True,
    exclude_device: str | None = None,
    kind: str | None = None,
    rule: str | None = None,
    reason: str | None = None,
    exclude_client: str | None = None,
) -> int:
    """Disk load/create cardId then POST events — safe to run in an executor.

    Manual Soon Notify always uses severity attention. Automatic FIRE may pass
    severity alert with manual=False. Any other severity is attention.
    """
    if manual or severity != "alert":
        severity = "attention"
    card_id = load_card_id(hass)
    _event_tls.devices = None
    _event_tls.http_status = None
    _event_tls.error = None
    # Only chat passes a sender to skip; alerts and manual sends post exactly as before.
    extra = {"exclude_device": exclude_device} if exclude_device else {}
    status = post_house_event(
        property_id, key, card_id, title, timeout=timeout, severity=severity, **extra
    )
    outcome = last_post_outcome(status)
    _event_tls.outcome = outcome
    # Phone-readable last auth rejection; every kind of attempt counts.
    try:
        note_event_key_attempt(hass, outcome)
    except Exception:
        _LOGGER.error("patrimony_event_key_status_write_failed")
    # Every attempt is logged, success or failure. Manual sends always; others when tagged.
    log_kind = "manual" if manual else kind
    if log_kind:
        try:
            record_push(
                hass,
                kind=log_kind,
                rule=MANUAL_SEND_CLASS if manual else (rule or log_kind),
                outcome=outcome,
                reason=reason or ("Manual send from panel" if manual else None),
                severity=severity,
                devices=getattr(_event_tls, "devices", None),
                exclude_client=exclude_client if is_device_hash(exclude_device) else None,
            )
        except Exception:
            _LOGGER.error("patrimony_push_log_write_failed")
    return status


def manual_send(hass, property_id: str, key: str, title: str) -> tuple[int, dict[str, Any]]:
    """POST /notify worker: status plus the events-API outcome (same thread)."""
    status = load_card_id_and_post(hass, property_id, key, title)
    outcome = dict(getattr(_event_tls, "outcome", None) or last_post_outcome(status))
    return status, outcome


def manual_event(key: Any, title: Any) -> dict[str, str] | None:
    """Gate for POST /notify. Explicit Soon only. No client severity."""
    return manual_notify_decision(is_usable_house_event_key(key), title)


def is_device_hash(value: Any) -> bool:
    """SHA-256 hex of an APNs token, as the phone sends it. Never the token itself."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def post_house_event(
    property_id: str,
    key: str,
    card_id: str,
    title: str,
    timeout: int = 8,
    severity: str = "attention",
    exclude_device: str | None = None,
) -> int:
    if severity != "alert":
        severity = "attention"
    _event_tls.http_status = None
    _event_tls.error = None
    _event_tls.upstream_error = None
    if not title_is_safe(title):
        _event_tls.error = "unsafe_title"
        return 400
    body: dict[str, str] = {"cardId": card_id, "severity": severity, "title": title}
    # The phone that wrote a chat message does not need a push about it.
    if is_device_hash(exclude_device):
        body["excludeDevice"] = exclude_device
    payload = json.dumps(body).encode()
    req = Request(
        events_url(property_id),
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": NOTIFY_USER_AGENT,
            "X-House-Event-Key": key,
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            raw = b""
            reader = getattr(resp, "read", None)
            if callable(reader):
                try:
                    raw = reader() or b""
                except Exception:
                    raw = b""
            _event_tls.devices = devices_from_event_body(raw)
            _event_tls.http_status = status
            return status
    except HTTPError as exc:
        _event_tls.devices = None
        _event_tls.http_status = int(exc.code or 502)
        _event_tls.upstream_error = _upstream_error_code(exc)
        return int(exc.code or 502)
    except (URLError, TimeoutError, OSError) as exc:
        _event_tls.devices = None
        _event_tls.error = _error_kind(exc)
        return 502


def _error_kind(exc: BaseException) -> str:
    """Short fixed label for a POST with no HTTP reply. Never the exception text."""
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return "timeout"
    text = type(reason or exc).__name__.lower()
    if "timeout" in text:
        return "timeout"
    if "ssl" in text or "certificate" in text:
        return "tls_error"
    return "connection_error"


def last_post_outcome(status: int) -> dict[str, Any]:
    """Events-API status of the last post on this thread: number, or None + error kind."""
    http_status = getattr(_event_tls, "http_status", None)
    error = getattr(_event_tls, "error", None)
    if http_status is None and error is None:
        # post_house_event replaced (tests) or older path: trust the returned int.
        http_status = int(status)
    out = {"status": http_status, "error": error, "ok": http_status is not None and 200 <= int(http_status) < 300 and error is None}
    upstream = getattr(_event_tls, "upstream_error", None)
    if upstream:
        out["upstreamError"] = upstream
    return out


_SAFE_CODE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


def _upstream_error_code(exc: BaseException) -> str | None:
    """Events-API error code string from an HTTP error body. Never the body itself."""
    try:
        reader = getattr(exc, "read", None)
        raw = reader() if callable(reader) else b""
        data = json.loads(raw or b"{}")
    except Exception:
        return None
    code = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code")
        elif isinstance(err, str):
            code = err
        if code is None and isinstance(data.get("code"), str):
            code = data.get("code")
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code) and not code.startswith("hek_"):
        return code
    return None


EVENT_KEY_STATUS_STORE = "eventKeyStatus"
AUTH_REJECT_STATUSES = (401, 403)


def _load_notify_doc(hass) -> dict[str, Any]:
    """Read-only view; {} when missing or unreadable."""
    try:
        return _read_notify_file(notify_path(hass))
    except NotifyDocUnreadable:
        return {}


def auth_rejection_from_outcome(outcome: dict[str, Any] | None, at: str) -> dict[str, Any] | None:
    """401/403 → phone status object; anything else (2xx, 5xx, timeout) → None."""
    if not isinstance(outcome, dict):
        return None
    status = outcome.get("status")
    if outcome.get("error") or not isinstance(status, int) or isinstance(status, bool):
        return None
    if status not in AUTH_REJECT_STATUSES:
        return None
    upstream = outcome.get("upstreamError")
    code = upstream if isinstance(upstream, str) and _SAFE_CODE.fullmatch(upstream) else "invalid_house_event_key"
    return {"code": status, "error": code, "at": at}


def note_event_key_attempt(hass, outcome: dict[str, Any] | None, at: str | None = None) -> None:
    """Most recent events-API attempt decides eventKeyLastPushStatus. Persisted."""
    stamp = at or datetime.now().astimezone().isoformat(timespec="seconds")

    def mutate(data: dict[str, Any]) -> None:
        block = data.get(EVENT_KEY_STATUS_STORE)
        block = dict(block) if isinstance(block, dict) else {}
        block["lastPushStatus"] = auth_rejection_from_outcome(outcome, stamp)
        data[EVENT_KEY_STATUS_STORE] = block

    _update_notify_doc(hass, mutate)


def mark_event_key_installed(hass, at: str | None = None) -> None:
    """POST /event_key success: clear the rejection, stamp installedAt. No key stored here."""
    stamp = at or datetime.now().astimezone().isoformat(timespec="seconds")

    def mutate(data: dict[str, Any]) -> None:
        block = data.get(EVENT_KEY_STATUS_STORE)
        block = dict(block) if isinstance(block, dict) else {}
        block["lastPushStatus"] = None
        block["installedAt"] = stamp
        data[EVENT_KEY_STATUS_STORE] = block

    _update_notify_doc(hass, mutate)


def event_key_status_fields(hass) -> dict[str, Any]:
    """{eventKeyLastPushStatus, eventKeyInstalledAt}. Never the key or fingerprint."""
    block = _load_notify_doc(hass).get(EVENT_KEY_STATUS_STORE)
    block = block if isinstance(block, dict) else {}
    last = block.get("lastPushStatus")
    if isinstance(last, dict) and last.get("code") in AUTH_REJECT_STATUSES:
        last = {
            "code": int(last["code"]),
            "error": str(last.get("error") or "invalid_house_event_key"),
            "at": str(last.get("at") or ""),
        }
    else:
        last = None
    installed = block.get("installedAt")
    return {
        "eventKeyLastPushStatus": last,
        "eventKeyInstalledAt": installed if isinstance(installed, str) and installed else None,
    }


def ingest_host() -> str:
    return urlparse(BACKEND_BASE).netloc or "Push notification server"


def probe_ingest(timeout: int = 6) -> bool:
    req = Request(
        BACKEND_BASE,
        method="GET",
        headers={"User-Agent": NOTIFY_USER_AGENT},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return True
    except HTTPError:
        return True
    except (URLError, TimeoutError, OSError):
        return False


def load_attention_store(hass) -> dict[str, Any]:
    """Debounce + baseline next to the stable notify card. Never stores hek_."""
    path = notify_path(hass)
    try:
        raw = fileio.read_text(path)
        if raw is not None:
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("attention"), dict):
                att = data["attention"]
                classes = att.get("classes") if isinstance(att.get("classes"), dict) else {}
                pushed = att.get("pushed") if isinstance(att.get("pushed"), dict) else {}
                return {
                    "seeded": bool(att.get("seeded")),
                    "classes": {str(k): str(v) for k, v in classes.items() if v},
                    "pushed": {
                        str(k): float(v)
                        for k, v in pushed.items()
                        if isinstance(v, (int, float))
                    },
                }
    except Exception:
        pass
    return {"seeded": False, "classes": {}, "pushed": {}}


def save_attention_store(hass, store: dict[str, Any]) -> None:
    classes = store.get("classes") if isinstance(store.get("classes"), dict) else {}
    pushed = store.get("pushed") if isinstance(store.get("pushed"), dict) else {}
    block = {
        "seeded": bool(store.get("seeded")),
        "classes": {str(k): str(v) for k, v in classes.items() if v},
        "pushed": {
            str(k): float(v)
            for k, v in pushed.items()
            if isinstance(v, (int, float))
        },
    }

    def mutate(data: dict[str, Any]) -> None:
        if not str(data.get("cardId") or "").strip():
            data["cardId"] = str(uuid4())
        data["attention"] = block

    _update_notify_doc(hass, mutate)



NO_IOS_CLIENT = "No iOS client registered"
PUSH_LOG_MAX = 50
MANUAL_SEND_CLASS = "manual.send"
_DEVICE_LIST_KEYS = ("devices", "clients", "iosDevices", "deviceNames", "targets")
_NAME_KEYS = ("name", "deviceName", "label", "client", "clientName", "client_name")
_ID_KEYS = ("clientId", "client_id", "tokenId", "token_id", "id")
_event_tls = threading.local()


def _public_text(value: Any) -> str | None:
    """Drop blanks and anything that looks like a house key or token."""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if "hek_" in lowered or "eyj" in lowered or "token" in lowered or "secret" in lowered:
        return None
    compact = text.replace("-", "").replace(":", "")
    if len(compact) >= 32 and all(c in "0123456789abcdefABCDEF" for c in compact):
        return None
    return text


def _safe_name(value: Any) -> str | None:
    """A stored display name. Not a key, token, or long hex id."""
    text = "" if value is None else str(value).strip()
    if not text or len(text) > 80:
        return None
    lowered = text.lower()
    if "hek_" in lowered or "eyj" in lowered or "secret" in lowered or "token" in lowered:
        return None
    compact = text.replace("-", "").replace(":", "")
    if len(compact) >= 32 and all(c in "0123456789abcdefABCDEF" for c in compact):
        return None
    return text


def _id_label(value: Any) -> str | None:
    """Client or token id already stored. Long ids keep the last 4. Never a secret."""
    text = "" if value is None else str(value).strip()
    if not text or len(text) > 200:
        return None
    lowered = text.lower()
    if "hek_" in lowered or "eyj" in lowered or "secret" in lowered:
        return None
    compact = text.replace("-", "").replace(":", "")
    long_hex = len(compact) >= 32 and all(c in "0123456789abcdefABCDEF" for c in compact)
    if long_hex or len(text) > 24:
        tail = text[-4:]
        if len(tail) == 4 and tail.isalnum():
            return tail
        return None
    if "token" in lowered:
        return None
    return text


def label_for_client(item: Any) -> str | None:
    """Name if we have one, otherwise a client id / token id. Ignores APNs token fields."""
    if isinstance(item, str):
        return _safe_name(item) or _id_label(item)
    if not isinstance(item, dict):
        return None
    for key in _NAME_KEYS:
        if item.get(key) not in (None, ""):
            name = _safe_name(item.get(key))
            if name:
                return name
    for key in _ID_KEYS:
        label = _id_label(item.get(key))
        if label:
            return label
    return None


def _labels_from_items(items: list) -> list[str]:
    labels: list[str] = []
    for item in items:
        label = label_for_client(item)
        if label and label not in labels:
            labels.append(label)
    return labels


def devices_from_event_body(raw: Any) -> list[str] | None:
    """Public device/client labels from the events response, or None if it has no list.

    deviceCount is not a list. Names win. Otherwise a truncated client/token id.
    APNs token fields, hek_, and HA tokens are dropped.
    """
    if raw is None or raw == b"" or raw == "":
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in _DEVICE_LIST_KEYS:
        items = data.get(key)
        if isinstance(items, list):
            return _labels_from_items(items)
    return None


def _iter_refresh_tokens(hass):
    auth = getattr(hass, "auth", None)
    if auth is None:
        return
    seen: set[int] = set()

    def emit(tok):
        if tok is None:
            return
        ident = id(tok)
        if ident in seen:
            return
        seen.add(ident)
        yield_box.append(tok)

    yield_box: list = []
    direct = getattr(auth, "refresh_tokens", None)
    if isinstance(direct, dict):
        for tok in direct.values():
            emit(tok)
    elif isinstance(direct, list):
        for tok in direct:
            emit(tok)
    users = getattr(auth, "users", None) or []
    try:
        users = list(users)
    except Exception:
        users = []
    for user in users:
        tokens = getattr(user, "refresh_tokens", None)
        if isinstance(tokens, dict):
            for tok in tokens.values():
                emit(tok)
        elif isinstance(tokens, list):
            for tok in tokens:
                emit(tok)
    for tok in yield_box:
        yield tok


def _refresh_token_label(tok: Any) -> str | None:
    """Patrimony iOS refresh-token label. Never reads the token secret."""
    def grab(key: str):
        if isinstance(tok, dict):
            return tok.get(key)
        return getattr(tok, key, None)

    client_name = grab("client_name")
    if str(client_name or "").strip() != PAIR_CLIENT_NAME:
        return None
    return label_for_client(
        {
            "client_name": client_name,
            "client_id": grab("client_id"),
            "id": grab("id"),
        }
    )


def _token_field(tok: Any, key: str):
    if isinstance(tok, dict):
        return tok.get(key)
    return getattr(tok, key, None)


def _session_labels(session: Any) -> list[str]:
    """Names or ids actually stored. One ios_session object is one client.

    A list, or a clients/sessions list inside the file, is enumerated in full.
    """
    if isinstance(session, list):
        return _labels_from_items(session)
    if not isinstance(session, dict):
        return []
    for key in ("clients", "sessions"):
        items = session.get(key)
        if isinstance(items, list) and items:
            labels = _labels_from_items(items)
            if labels:
                return labels
    name = _safe_name(session.get("deviceName"))
    if name:
        return [name]
    label = label_for_client(session)
    return [label] if label else []


def _token_id_label(tok: Any) -> str | None:
    """Stored token id, last 4 when long. Never reads the token secret."""
    for key in ("id", "client_id"):
        label = _id_label(_token_field(tok, key))
        if label:
            return label
    return None


def _patrimony_refresh_tokens(hass) -> list[Any]:
    found: list[Any] = []
    try:
        tokens = _iter_refresh_tokens(hass)
    except Exception:
        return found
    for tok in tokens:
        if str(_token_field(tok, "client_name") or "").strip() != PAIR_CLIENT_NAME:
            continue
        found.append(tok)
    return found


def registered_ios_clients(hass) -> list[str]:
    """Every iOS client this house has registered. Name, else id. No secrets.

    Prefer the per-credential clients registry (display names). Fall back to
    ios_session.json last-write and Patrimony iOS refresh tokens.
    """
    try:
        from .clients import active_display_names

        named = active_display_names(hass)
        if named:
            return named
    except Exception:
        pass
    try:
        session = load_ios_session(hass)
    except Exception:
        session = None
    session_labels = _session_labels(session)
    tokens = _patrimony_refresh_tokens(hass)
    if len(tokens) <= 1:
        if session_labels:
            return session_labels
        if not tokens:
            return []
        label = _refresh_token_label(tokens[0])
        return [label] if label else []
    if len(session_labels) == len(tokens):
        return list(session_labels)
    labels: list[str] = []
    # One stored device name stands for one registration, not the whole list.
    named = session_labels[0] if len(session_labels) == 1 else None
    skip_named = named is not None
    if named:
        labels.append(named)
    for tok in tokens:
        if skip_named:
            skip_named = False
            continue
        label = _token_id_label(tok)
        if label and label not in labels:
            labels.append(label)
    if len(session_labels) > 1:
        for label in session_labels:
            if label not in labels:
                labels.append(label)
    return labels


def _devices_field(devices: Any, local: list[str] | None = None) -> Any:
    """Public devices cell. None means the events body had no list: use local clients."""
    if devices is None:
        found = [item for item in (local or []) if item]
        return found if found else NO_IOS_CLIENT
    if isinstance(devices, list):
        labels = _labels_from_items(devices)
        return labels if labels else NO_IOS_CLIENT
    if isinstance(devices, str) and devices.strip() == NO_IOS_CLIENT:
        return NO_IOS_CLIENT
    return NO_IOS_CLIENT


def record_manual_send(hass, devices: Any = None, at: str | None = None) -> None:
    """One row for a successful gold Send. Not an attention FIRE rule."""
    record_attention_push(hass, MANUAL_SEND_CLASS, devices=devices, at=at)


def record_attention_push(hass, fire_class: str, devices: Any = None, at: str | None = None) -> None:
    """Append one successful row (legacy helper). Caller must not use this for QUIET."""
    fire = _public_text(fire_class)
    if not fire:
        return
    kind = "manual" if fire == MANUAL_SEND_CLASS else "attention"
    record_push(hass, kind=kind, rule=fire, outcome=None, devices=devices, at=at)


PUSH_KINDS = ("attention", "manual", "chat", "pq")


def _client_names(hass, exclude_client: str | None = None) -> list[str]:
    """Saved display names of active paired clients, minus the excluded sender."""
    try:
        from .clients import active_display_names

        return active_display_names(hass, exclude_client_id=exclude_client)
    except Exception:
        return []


def record_push(
    hass,
    kind: str,
    rule: str | None,
    outcome: dict[str, Any] | None,
    reason: str | None = None,
    severity: str | None = None,
    devices: Any = None,
    at: str | None = None,
    exclude_client: str | None = None,
) -> None:
    """Append one push-log row, success or failure. No title text, keys or tokens."""
    fire = _public_text(rule) or _public_text(kind)
    if not fire:
        return
    kind_text = kind if kind in PUSH_KINDS else "attention"
    if outcome is None:
        outcome = {"status": None, "error": None, "ok": True}
    status = outcome.get("status")
    ok = bool(outcome.get("ok"))
    error = outcome.get("error") if outcome.get("error") in (
        "timeout", "connection_error", "tls_error", "unsafe_title"
    ) else None
    stamp = at or datetime.now().astimezone().isoformat(timespec="seconds")
    stamp = _public_text(stamp) or ""

    def mutate(data: dict[str, Any]) -> None:
        rows = data.get("pushLog")
        if not isinstance(rows, list):
            rows = []
        normalized: list[Any] = []
        for existing in rows:
            if not isinstance(existing, dict):
                continue
            kept = dict(existing)
            kept["devices"] = _devices_field(existing.get("devices"))
            normalized.append(kept)
        rows = normalized
        names = _client_names(hass, exclude_client)
        if names:
            devices_cell: Any = names
        elif exclude_client:
            devices_cell = NO_IOS_CLIENT
        else:
            devices_cell = _devices_field(devices, local=registered_ios_clients(hass))
        reason_text = _public_text(reason)
        if reason_text and len(reason_text) > 120:
            reason_text = reason_text[:120]
        sev = str(severity).strip().lower() if severity else None
        rows.append(
            {
                "at": stamp,
                "fire_class": fire,
                "devices": devices_cell,
                "kind": kind_text,
                "rule": fire,
                "status": int(status) if isinstance(status, int) else None,
                "error": error,
                "ok": ok,
                "reason": reason_text,
                "severity": sev if sev in ("attention", "alert", "info") else None,
            }
        )
        data["pushLog"] = rows[-PUSH_LOG_MAX:]

    _update_notify_doc(hass, mutate)


def push_log_document(hass) -> dict[str, Any]:
    """Panel payload. Last 50. Never includes hek_ or tokens."""
    rows: list[Any] = []
    loaded = _load_notify_doc(hass)
    if isinstance(loaded.get("pushLog"), list):
        rows = loaded["pushLog"]
    public: list[dict[str, Any]] = []
    changed = False
    for row in rows[-PUSH_LOG_MAX:]:
        if not isinstance(row, dict):
            continue
        devices = _devices_field(row.get("devices"))
        if row.get("devices") != devices:
            changed = True
            row["devices"] = devices
        fire = _public_text(row.get("fire_class"))
        if not fire:
            continue
        at = _public_text(row.get("at")) or ""
        status = row.get("status")
        kind = row.get("kind")
        if kind not in PUSH_KINDS:
            kind = "manual" if fire == MANUAL_SEND_CLASS else "attention"
        sev = row.get("severity")
        public.append(
            {
                "at": at,
                "fire_class": fire,
                "devices": devices,
                "kind": kind,
                "rule": _public_text(row.get("rule")) or fire,
                # Old rows were only written after a 2xx: status unknown, ok true.
                "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                "error": row.get("error") if isinstance(row.get("error"), str) else None,
                "ok": bool(row.get("ok", True)),
                "reason": _public_text(row.get("reason")),
                "severity": sev if sev in ("attention", "alert", "info") else None,
            }
        )
    if changed:
        # Re-read under the lock and only normalise devices; never write a stale copy.
        def mutate(data: dict[str, Any]):
            fresh = data.get("pushLog")
            if not isinstance(fresh, list):
                return False
            for row in fresh:
                if isinstance(row, dict):
                    row["devices"] = _devices_field(row.get("devices"))
            data["pushLog"] = fresh[-PUSH_LOG_MAX:]
            return True

        try:
            _update_notify_doc(hass, mutate)
        except Exception:
            pass
    return {"rows": public}


def dispatch_attention_pushes(hass, facts, property_id, key, now: float | None = None) -> int:
    """Persist the FIRE baseline, then POST events only for new FIRE classes.

    No usable hek_ → baseline updates, nothing is sent (fail closed).
    """
    moment = time.time() if now is None else float(now)
    store = load_attention_store(hass)
    usable = bool(property_id) and is_usable_house_event_key(key)
    pushes = plan_automatic_pushes(
        store, facts, moment, commit_debounce=usable
    )
    save_attention_store(hass, store)
    if not pushes or not usable:
        return 0
    sent = 0
    for push in pushes:
        title = push.get("title") or ""
        if not title_is_safe(title):
            continue
        status = load_card_id_and_post(
            hass,
            str(property_id),
            str(key),
            title,
            severity=push.get("severity") or "alert",
            manual=False,
            kind="attention",
            rule=str(push.get("fire_class") or ""),
            # The title is a fixed rule sentence plus the card label; never user text.
            reason=f"{push.get('fire_class')}: {title}",
        )
        if 200 <= int(status) < 300:
            sent += 1
            _LOGGER.info("patrimony_attention_push class=%s", push.get("fire_class"))
        else:
            _LOGGER.warning(
                "patrimony_attention_push_failed class=%s status=%s",
                push.get("fire_class"),
                status,
            )
    return sent
