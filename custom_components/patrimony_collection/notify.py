"""House attention push. hek_ only. Never an HA token."""

from __future__ import annotations

import json
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
    path = notify_path(hass)
    try:
        if path.is_file():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                cid = str(data.get("cardId") or "").strip()
                if cid:
                    return cid
    except Exception:
        pass
    cid = str(uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cardId": cid}, indent=2) + "\n")
    return cid


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
) -> int:
    """Disk load/create cardId then POST events — safe to run in an executor.

    Manual Soon Notify always uses severity attention. Automatic FIRE may pass
    severity alert with manual=False. Any other severity is attention.
    """
    if manual or severity != "alert":
        severity = "attention"
    card_id = load_card_id(hass)
    _event_tls.devices = None
    status = post_house_event(
        property_id, key, card_id, title, timeout=timeout, severity=severity
    )
    # Gold Send shows Sent only on 2xx. FIRE logs itself; do not double-write.
    if manual and 200 <= int(status) < 300:
        record_manual_send(hass, devices=getattr(_event_tls, "devices", None))
    return status


def manual_event(key: Any, title: Any) -> dict[str, str] | None:
    """Gate for POST /notify. Explicit Soon only. No client severity."""
    return manual_notify_decision(is_usable_house_event_key(key), title)


def post_house_event(
    property_id: str,
    key: str,
    card_id: str,
    title: str,
    timeout: int = 8,
    severity: str = "attention",
) -> int:
    if severity != "alert":
        severity = "attention"
    if not title_is_safe(title):
        return 400
    payload = json.dumps(
        {"cardId": card_id, "severity": severity, "title": title}
    ).encode()
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
            return status
    except HTTPError as exc:
        _event_tls.devices = None
        return int(exc.code or 502)
    except (URLError, TimeoutError, OSError):
        _event_tls.devices = None
        return 502


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
        if path.is_file():
            data = json.loads(path.read_text())
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
    path = notify_path(hass)
    data: dict[str, Any] = {}
    try:
        if path.is_file():
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        data = {}
    if not str(data.get("cardId") or "").strip():
        data["cardId"] = str(uuid4())
    classes = store.get("classes") if isinstance(store.get("classes"), dict) else {}
    pushed = store.get("pushed") if isinstance(store.get("pushed"), dict) else {}
    data["attention"] = {
        "seeded": bool(store.get("seeded")),
        "classes": {str(k): str(v) for k, v in classes.items() if v},
        "pushed": {
            str(k): float(v)
            for k, v in pushed.items()
            if isinstance(v, (int, float))
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")



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

    ios_session.json is one latest device. Refresh tokens are the list: when
    more than one Patrimony iOS token is stored, each is listed. One client
    still shows that one stored name.
    """
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
    """Append one FIRE push row. Caller must not use this for QUIET."""
    fire = _public_text(fire_class)
    if not fire:
        return
    stamp = at or datetime.now().astimezone().isoformat(timespec="seconds")
    stamp = _public_text(stamp) or ""
    path = notify_path(hass)
    data: dict[str, Any] = {}
    try:
        if path.is_file():
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        data = {}
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
    rows.append(
        {
            "at": stamp,
            "fire_class": fire,
            "devices": _devices_field(devices, local=registered_ios_clients(hass)),
        }
    )
    data["pushLog"] = rows[-PUSH_LOG_MAX:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def push_log_document(hass) -> dict[str, Any]:
    """Panel payload. Last 50. Never includes hek_ or tokens."""
    path = notify_path(hass)
    rows: list[Any] = []
    loaded: dict[str, Any] | None = None
    try:
        if path.is_file():
            parsed = json.loads(path.read_text())
            if isinstance(parsed, dict):
                loaded = parsed
                if isinstance(parsed.get("pushLog"), list):
                    rows = parsed["pushLog"]
    except Exception:
        rows = []
        loaded = None
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
        public.append(
            {
                "at": at,
                "fire_class": fire,
                "devices": devices,
            }
        )
    if changed and isinstance(loaded, dict):
        try:
            loaded["pushLog"] = rows[-PUSH_LOG_MAX:]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(loaded, indent=2) + "\n")
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
        )
        if 200 <= int(status) < 300:
            sent += 1
            _LOGGER.info("patrimony_attention_push class=%s", push.get("fire_class"))
            # Events body rarely names devices (deviceCount only). Record who we targeted.
            # QUIET never reaches here.
            record_attention_push(
                hass,
                str(push.get("fire_class") or ""),
                devices=getattr(_event_tls, "devices", None),
                at=None,
            )
        else:
            _LOGGER.warning(
                "patrimony_attention_push_failed class=%s status=%s",
                push.get("fire_class"),
                status,
            )
    return sent
