"""House chat. On this Home Assistant only.

Not on the presentation document. Not a Patrimony cloud store.

HTTP (same HA Bearer as /api/patrimony_collection/state):

- GET  /api/patrimony_collection/chat
- POST /api/patrimony_collection/chat
    {"text": "<plain text>", "retention": "keep"|"1h"|"1d"|"7d"}
    retention is optional and defaults to keep.
- DELETE /api/patrimony_collection/chat/{message_id}

Message text is encrypted at rest. The key file stays on the HA host.
Push reuses the existing events path and sends only
"New chat message in {display_name}", or "New chat message" when the
stored display name is empty. The events body is not given the message
text, the sender, or a preview. This is not end-to-end: the house can
read messages so the panel can show them. TLS covers the phone link.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .attention import title_is_safe
from .const import (
    CHAT_FILE,
    CHAT_KEY_FILE,
    CHAT_MESSAGE_PATH,
    CHAT_PATH,
    CHAT_TEXT_MAX,
    CONF_DISPLAY_NAME,
    CONF_HOUSE_EVENT_KEY,
    CONF_PROPERTY_ID,
    DOMAIN,
    PAIR_CLIENT_NAME,
    SCHEMA_VERSION,
)
from .ios_session import load_ios_session
from .notify import (
    _iter_refresh_tokens,
    _safe_name,
    is_usable_house_event_key,
    load_card_id_and_post,
    registered_ios_clients,
)

_LOGGER = logging.getLogger(__name__)

RETENTIONS = {
    "keep": None,
    "1h": 3600,
    "1d": 86400,
    "7d": 7 * 86400,
}
CHAT_PUSH_GENERIC = "New chat message"
CHAT_PUSH_IN = "New chat message in "
NO_IOS_CLIENT = "No iOS client registered"
PANEL_SENDER_FALLBACK = "Home Assistant"
_MAX_STORED = 200

try:
    from homeassistant.components.http import HomeAssistantView
    from homeassistant.core import HomeAssistant
except ImportError:

    class HomeAssistantView:  # type: ignore[no-redef]
        url = ""
        name = ""
        requires_auth = True

        def __init__(self, *args, **kwargs):
            pass

    HomeAssistant = Any  # type: ignore[misc,assignment]


def chat_path(hass) -> Path:
    try:
        return Path(hass.config.path(CHAT_FILE))
    except Exception:
        return Path("/config") / CHAT_FILE


def key_path(hass) -> Path:
    try:
        return Path(hass.config.path(CHAT_KEY_FILE))
    except Exception:
        return Path("/config") / CHAT_KEY_FILE


def chat_push_title(display_name: Any) -> str:
    """Fixed push sentence. Never a message body."""
    name = "" if display_name is None else str(display_name).strip()
    if not name:
        return CHAT_PUSH_GENERIC
    title = CHAT_PUSH_IN + name
    if not title_is_safe(title):
        return CHAT_PUSH_GENERIC
    return title


def _utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    return _utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _keystream(key: bytes, nonce: bytes, nbytes: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < nbytes:
        block = hmac.new(
            key,
            b"ks" + nonce + counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:nbytes])


def _enc_key(key: bytes) -> bytes:
    return hmac.new(key, b"enc", hashlib.sha256).digest()


def _mac_key(key: bytes) -> bytes:
    return hmac.new(key, b"mac", hashlib.sha256).digest()


def _seal(key: bytes, message_id: str, text: str) -> tuple[str, str]:
    nonce = secrets.token_bytes(16)
    plain = text.encode("utf-8")
    stream = _keystream(_enc_key(key), nonce, len(plain))
    ct = bytes(a ^ b for a, b in zip(plain, stream))
    mac = hmac.new(
        _mac_key(key),
        b"patrimony-chat-v1\0" + message_id.encode("utf-8") + nonce + ct,
        hashlib.sha256,
    ).digest()
    return base64.b64encode(nonce).decode("ascii"), base64.b64encode(mac + ct).decode("ascii")


def _open(key: bytes, message_id: str, nonce_b64: Any, blob_b64: Any) -> str | None:
    try:
        nonce = base64.b64decode(str(nonce_b64), validate=True)
        blob = base64.b64decode(str(blob_b64), validate=True)
    except Exception:
        return None
    if len(nonce) != 16 or len(blob) < 32:
        return None
    mac, ct = blob[:32], blob[32:]
    expect = hmac.new(
        _mac_key(key),
        b"patrimony-chat-v1\0" + str(message_id).encode("utf-8") + nonce + ct,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(mac, expect):
        return None
    stream = _keystream(_enc_key(key), nonce, len(ct))
    plain = bytes(a ^ b for a, b in zip(ct, stream))
    try:
        return plain.decode("utf-8")
    except Exception:
        return None


def load_chat_key(hass) -> bytes:
    """32-byte host key. Created on first use. Never logged."""
    path = key_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        data = path.read_bytes()
        if len(data) == 32:
            return data
        raise OSError("chat_key_unreadable")
    key = secrets.token_bytes(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return key


def _read_store(hass) -> dict[str, Any]:
    path = chat_path(hass)
    try:
        if not path.is_file():
            return {"schemaVersion": SCHEMA_VERSION, "messages": []}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schemaVersion": SCHEMA_VERSION, "messages": []}
    if not isinstance(data, dict):
        return {"schemaVersion": SCHEMA_VERSION, "messages": []}
    rows = data.get("messages")
    if not isinstance(rows, list):
        rows = []
    data["messages"] = [row for row in rows if isinstance(row, dict)]
    data["schemaVersion"] = SCHEMA_VERSION
    return data


def _write_store(hass, store: dict[str, Any]) -> None:
    path = chat_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "messages": list(store.get("messages") or []),
    }
    text = json.dumps(payload, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _expired(row: dict[str, Any], now: datetime) -> bool:
    moment = _parse_stamp(row.get("expiresAt"))
    if moment is None:
        return False
    return _utc(now) >= moment


def _purge(store: dict[str, Any], now: datetime) -> bool:
    rows = store.get("messages") or []
    kept = [row for row in rows if not _expired(row, now)]
    if len(kept) == len(rows):
        return False
    store["messages"] = kept
    return True


def _first_entry(hass):
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:
        return None
    return entries[0] if entries else None


def house_display_name(hass) -> str:
    entry = _first_entry(hass)
    if entry is None:
        return ""
    data = getattr(entry, "data", None) or {}
    return str(data.get(CONF_DISPLAY_NAME) or "").strip()


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


def _token_client_name(tok: Any) -> str:
    if isinstance(tok, dict):
        return str(tok.get("client_name") or "").strip()
    return str(getattr(tok, "client_name", "") or "").strip()


def _token_id(tok: Any) -> str:
    if isinstance(tok, dict):
        return str(tok.get("id") or "").strip()
    return str(getattr(tok, "id", "") or "").strip()


def _refresh_token(hass, token_id: str):
    if not token_id:
        return None
    auth = getattr(hass, "auth", None)
    direct = getattr(auth, "refresh_tokens", None) if auth is not None else None
    if isinstance(direct, dict):
        found = direct.get(token_id)
        if found is not None:
            return found
    try:
        tokens = _iter_refresh_tokens(hass)
    except Exception:
        return None
    for tok in tokens:
        if _token_id(tok) == token_id:
            return tok
    return None


def request_user(request):
    if request is None:
        return None
    user = None
    getter = getattr(request, "get", None)
    if callable(getter):
        try:
            user = getter("hass_user")
        except Exception:
            user = None
    if user is None:
        user = getattr(request, "hass_user", None)
    return user


def caller_authorized(request) -> bool:
    return request_user(request) is not None


def is_ios_client(hass, request) -> bool:
    iss = _jwt_iss(_bearer(request))
    if not iss:
        return False
    tok = _refresh_token(hass, iss)
    return _token_client_name(tok) == PAIR_CLIENT_NAME


def _user_name(user) -> str:
    name = user.get("name") if isinstance(user, dict) else getattr(user, "name", None)
    return _safe_name(name) or PANEL_SENDER_FALLBACK


def _user_id(user) -> str:
    raw = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    text = "" if raw is None else str(raw).strip()
    if not text or "hek_" in text.lower() or "eyj" in text.lower():
        return ""
    return text[:80]


def ios_sender_label(hass) -> str:
    """Stored device name, else the client name the push log already uses."""
    raw = load_ios_session(hass)
    if isinstance(raw, dict):
        name = _safe_name(raw.get("deviceName"))
        if name:
            return name
    try:
        labels = registered_ios_clients(hass)
    except Exception:
        labels = []
    for label in labels:
        safe = _safe_name(label)
        if safe and safe != NO_IOS_CLIENT:
            return safe
    return PAIR_CLIENT_NAME


def caller_identity(hass, request) -> dict[str, str]:
    user = request_user(request)
    if is_ios_client(hass, request):
        label = ios_sender_label(hass)
        return {"kind": "ios", "label": label, "key": "ios:" + label}
    label = _user_name(user)
    uid = _user_id(user)
    key = "panel:" + (uid or label)
    return {"kind": "panel", "label": label, "key": key}


def _can_delete(caller: dict[str, str], row: dict[str, Any]) -> bool:
    if caller.get("kind") == "panel":
        return True
    return (
        caller.get("kind") == "ios"
        and row.get("senderKind") == "ios"
        and row.get("senderKey") == caller.get("key")
    )


def _public_row(caller: dict[str, str], row: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "sender": row.get("senderLabel") or "",
        "senderKind": row.get("senderKind") or "",
        "text": text,
        "createdAt": row.get("createdAt"),
        "retention": row.get("retention") or "keep",
        "expiresAt": row.get("expiresAt"),
        "canDelete": _can_delete(caller, row),
    }


def _error(code: str, message: str, status: int) -> tuple[dict[str, Any], int]:
    return {"error": {"code": code, "message": message}}, status


def list_document(hass, request, now: datetime | None = None) -> dict[str, Any]:
    moment = _utc(now)
    store = _read_store(hass)
    changed = _purge(store, moment)
    key = load_chat_key(hass)
    caller = caller_identity(hass, request)
    public: list[dict[str, Any]] = []
    for row in store.get("messages") or []:
        text = _open(key, str(row.get("id") or ""), row.get("nonce"), row.get("ciphertext"))
        if text is None:
            continue
        public.append(_public_row(caller, row, text))
    if changed:
        _write_store(hass, store)
    return {"schemaVersion": SCHEMA_VERSION, "messages": public}


def handle_get(hass, request, now: datetime | None = None) -> tuple[dict[str, Any], int]:
    if not caller_authorized(request):
        return _error("unauthorized", "Unauthorized", 401)
    try:
        return list_document(hass, request, now=now), 200
    except Exception:
        _LOGGER.error("patrimony_chat_list_failed")
        return _error("store_failed", "Could not read chat", 500)


def _normalize_retention(raw: Any) -> str | None:
    if raw is None:
        return "keep"
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text not in RETENTIONS:
        return None
    return text


def create_message(hass, request, payload: Any, now: datetime | None = None) -> tuple[dict[str, Any], int]:
    if not caller_authorized(request):
        return _error("unauthorized", "Unauthorized", 401)
    if not isinstance(payload, dict) or "text" not in payload:
        return _error("empty", "Message is empty", 400)
    raw_text = payload.get("text")
    if not isinstance(raw_text, str):
        return _error("bad_text", "text must be a string", 400)
    text = raw_text.strip()
    if not text:
        return _error("empty", "Message is empty", 400)
    if len(text) > CHAT_TEXT_MAX:
        return _error("too_long", "Message is too long", 400)
    retention = _normalize_retention(payload.get("retention"))
    if retention is None:
        return _error("bad_retention", "retention must be keep, 1h, 1d, or 7d", 400)
    moment = _utc(now)
    seconds = RETENTIONS[retention]
    expires = None if seconds is None else _stamp(moment + timedelta(seconds=seconds))
    try:
        key = load_chat_key(hass)
        store = _read_store(hass)
        _purge(store, moment)
        caller = caller_identity(hass, request)
        message_id = str(uuid4())
        nonce, ciphertext = _seal(key, message_id, text)
        row = {
            "id": message_id,
            "senderKind": caller["kind"],
            "senderKey": caller["key"],
            "senderLabel": caller["label"],
            "createdAt": _stamp(moment),
            "retention": retention,
            "expiresAt": expires,
            "nonce": nonce,
            "ciphertext": ciphertext,
        }
        rows = list(store.get("messages") or [])
        rows.append(row)
        if len(rows) > _MAX_STORED:
            rows = rows[-_MAX_STORED:]
        store["messages"] = rows
        _write_store(hass, store)
    except Exception:
        _LOGGER.error("patrimony_chat_store_failed")
        return _error("store_failed", "Could not store the message", 500)
    return _public_row(caller, row, text), 201


def notify_house_chat(hass) -> int | None:
    """Existing events POST. Fixed sentence only. Does not take message text."""
    entry = _first_entry(hass)
    if entry is None:
        return None
    data = getattr(entry, "data", None) or {}
    options = getattr(entry, "options", None) or {}
    key = options.get(CONF_HOUSE_EVENT_KEY)
    property_id = data.get(CONF_PROPERTY_ID)
    if not property_id or not is_usable_house_event_key(key):
        return None
    title = chat_push_title(data.get(CONF_DISPLAY_NAME))
    try:
        status = load_card_id_and_post(
            hass,
            str(property_id),
            str(key),
            title,
            severity="attention",
            manual=False,
        )
    except Exception:
        _LOGGER.error("patrimony_chat_notice_failed")
        return None
    if 200 <= int(status) < 300:
        _LOGGER.info("patrimony_chat_notified")
    else:
        _LOGGER.warning("patrimony_chat_notice_failed status=%s", int(status))
    return int(status)


def handle_post(hass, request, payload: Any, now: datetime | None = None) -> tuple[dict[str, Any], int]:
    body, status = create_message(hass, request, payload, now=now)
    if status == 201:
        try:
            notify_house_chat(hass)
        except Exception:
            _LOGGER.error("patrimony_chat_notice_failed")
    return body, status


def delete_message(hass, request, message_id: str, now: datetime | None = None) -> tuple[dict[str, Any], int]:
    if not caller_authorized(request):
        return _error("unauthorized", "Unauthorized", 401)
    wanted = str(message_id or "").strip()
    if not wanted:
        return _error("not_found", "Message not found", 404)
    moment = _utc(now)
    try:
        store = _read_store(hass)
        _purge(store, moment)
        caller = caller_identity(hass, request)
        kept: list[dict[str, Any]] = []
        found = False
        allowed = False
        for row in store.get("messages") or []:
            if str(row.get("id") or "") != wanted:
                kept.append(row)
                continue
            found = True
            if _can_delete(caller, row):
                allowed = True
                continue
            kept.append(row)
        store["messages"] = kept
        _write_store(hass, store)
        if not found:
            return _error("not_found", "Message not found", 404)
        if not allowed:
            return _error("forbidden", "You can only delete your own message", 403)
    except Exception:
        _LOGGER.error("patrimony_chat_delete_failed")
        return _error("store_failed", "Could not delete the message", 500)
    return {"ok": True}, 200


class PatrimonyChatView(HomeAssistantView):
    """List and create. Same HA Bearer as state. No message text in notify."""

    url = CHAT_PATH
    name = "api:patrimony_collection:chat"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        body, status = handle_get(self.hass, request)
        return web.json_response(body, status=status)

    async def post(self, request):
        from aiohttp import web

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"code": "bad_json", "message": "chat body must be JSON"}},
                status=400,
            )
        job = getattr(self.hass, "async_add_executor_job", None)

        def _run():
            return handle_post(self.hass, request, payload)

        try:
            if job:
                body, status = await job(_run)
            else:
                body, status = _run()
        except Exception:
            _LOGGER.error("patrimony_chat_store_failed")
            return web.json_response(
                {"error": {"code": "store_failed", "message": "Could not store the message"}},
                status=500,
            )
        return web.json_response(body, status=status)


class PatrimonyChatMessageView(HomeAssistantView):
    """Delete one message. Panel may delete any. Sender may delete their own."""

    url = CHAT_MESSAGE_PATH
    name = "api:patrimony_collection:chat_message"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def delete(self, request, message_id=""):
        from aiohttp import web

        job = getattr(self.hass, "async_add_executor_job", None)

        def _run():
            return delete_message(self.hass, request, message_id)

        try:
            if job:
                body, status = await job(_run)
            else:
                body, status = _run()
        except Exception:
            _LOGGER.error("patrimony_chat_delete_failed")
            return web.json_response(
                {"error": {"code": "store_failed", "message": "Could not delete the message"}},
                status=500,
            )
        return web.json_response(body, status=status)


def register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(PatrimonyChatView(hass))
    hass.http.register_view(PatrimonyChatMessageView(hass))
