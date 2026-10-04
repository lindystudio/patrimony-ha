"""House chat. On this Home Assistant only.

Not on the presentation document. Not a Patrimony cloud store.

HTTP (same HA Bearer as /api/patrimony_collection/state):

- GET  /api/patrimony_collection/chat
- POST /api/patrimony_collection/chat
    {"text": "<plain text>", "retention": "keep"|"1h"|"1d"|"7d",
     "deviceName": "<optional, trimmed, max 80>",
     "imageBase64": "<standard base64, no data: prefix>",
     "imageContentType": "image/jpeg"|"image/png"|"image/webp"}
    retention is optional and defaults to keep.
    A paired credential uses its panel-edited display name (seeded once
    from a real deviceName). Later POST deviceName values do not overwrite
    an edited name. A non-empty deviceName still registers / labels a
    phone bearer even when is_ios_client is false (0.4.79). The panel
    chat window does not send deviceName. A post that is not a paired
    client uses sender label "HA ({username})". Old stored labels are not rewritten.
    imageBase64 and imageContentType are optional. text may be empty
    when an image is present. Both empty is rejected.
- GET  /api/patrimony_collection/chat/{message_id}/image
    raw image bytes. 404 when that message has no image.
- DELETE /api/patrimony_collection/chat/{message_id}
    Any panel user or phone may delete any message, for everyone.
- POST /api/patrimony_collection/chat/read
    {"lastSeenMessageId": "<id>"}
    Paired client only. Cursor is per client and monotonic.
    200 {"ok": true}. Unknown id is 400 unknown_message.

Message text and image bytes are encrypted at rest. The key file stays
on the HA host. The list does not inline image bytes. Delete and expiry
remove the image with the message. Push reuses the existing events path
and is not the 20-minute attention debounce. It sends only
"New chat message in {display_name}", or "New chat message" when the
stored display name is empty. The events body is {cardId, severity, title}.
It is not given the message text, the sender, a preview, or image bytes.
This is not end-to-end: the house can read messages so the panel can
show them. TLS covers the phone link.
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
    CHAT_IMAGE_MAX,
    CHAT_IMAGE_PATH,
    CHAT_KEY_FILE,
    CHAT_MESSAGE_PATH,
    CHAT_PATH,
    CHAT_READ_PATH,
    CHAT_TEXT_MAX,
    CONF_DISPLAY_NAME,
    CONF_HOUSE_EVENT_KEY,
    CONF_PROPERTY_ID,
    DOMAIN,
    PAIR_CLIENT_NAME,
    SCHEMA_VERSION,
)
from .ios_session import load_ios_session
from . import clients as house_clients
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
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_TEXT_DOMAIN = b"patrimony-chat-v1\0"
_IMAGE_DOMAIN = b"patrimony-chat-img-v1\0"

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


def _seal_raw(key: bytes, message_id: str, plain: bytes, domain: bytes) -> tuple[str, str]:
    nonce = secrets.token_bytes(16)
    stream = _keystream(_enc_key(key), nonce, len(plain))
    ct = bytes(a ^ b for a, b in zip(plain, stream))
    mac = hmac.new(
        _mac_key(key),
        domain + message_id.encode("utf-8") + nonce + ct,
        hashlib.sha256,
    ).digest()
    return base64.b64encode(nonce).decode("ascii"), base64.b64encode(mac + ct).decode("ascii")


def _open_raw(key: bytes, message_id: str, nonce_b64: Any, blob_b64: Any, domain: bytes) -> bytes | None:
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
        domain + str(message_id).encode("utf-8") + nonce + ct,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(mac, expect):
        return None
    stream = _keystream(_enc_key(key), nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, stream))


def _seal(key: bytes, message_id: str, text: str) -> tuple[str, str]:
    return _seal_raw(key, message_id, text.encode("utf-8"), _TEXT_DOMAIN)


def _open(key: bytes, message_id: str, nonce_b64: Any, blob_b64: Any) -> str | None:
    plain = _open_raw(key, message_id, nonce_b64, blob_b64, _TEXT_DOMAIN)
    if plain is None:
        return None
    try:
        return plain.decode("utf-8")
    except Exception:
        return None


def _seal_image(key: bytes, message_id: str, blob: bytes) -> tuple[str, str]:
    return _seal_raw(key, message_id, blob, _IMAGE_DOMAIN)


def _open_image(key: bytes, message_id: str, nonce_b64: Any, blob_b64: Any) -> bytes | None:
    return _open_raw(key, message_id, nonce_b64, blob_b64, _IMAGE_DOMAIN)


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
    cursors = store.get("readCursors")
    if isinstance(cursors, dict) and cursors:
        payload["readCursors"] = {
            str(k): str(v)
            for k, v in cursors.items()
            if str(k).strip() and str(v).strip()
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
    """Stored paired-client display name, else ios_session / push-log label."""
    names = house_clients.active_display_names(hass)
    for name in names:
        safe = _safe_name(name)
        if safe:
            return safe
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


def panel_sender_label(user) -> str:
    """New panel posts only. Stored rows keep the label they were written with."""
    return "HA (" + _user_name(user) + ")"


def caller_identity(hass, request) -> dict[str, str]:
    user = request_user(request)
    client_id = house_clients.request_client_id(request)
    if client_id:
        row = house_clients.get_client(hass, client_id)
        if row is not None and row.get("status") == house_clients.STATUS_ACTIVE:
            label = row.get("displayName") or ios_sender_label(hass)
            return {"kind": "ios", "label": label, "key": "ios:" + client_id}
    if is_ios_client(hass, request):
        label = ios_sender_label(hass)
        return {"kind": "ios", "label": label, "key": "ios:" + (client_id or label)}
    label = panel_sender_label(user)
    uid = _user_id(user)
    key = "panel:" + (uid or label)
    return {"kind": "panel", "label": label, "key": key}


def _can_delete(caller: dict[str, str], row: dict[str, Any]) -> bool:
    """Any member of the house may delete any message: panel users and paired phones."""
    return caller.get("kind") in ("panel", "ios")


def _active_clients(hass) -> list[dict[str, Any]]:
    try:
        rows = house_clients.load_clients(hass)
    except Exception:
        return []
    return [row for row in rows if row.get("status") == house_clients.STATUS_ACTIVE]


def _active_paired_client_id(hass, request) -> str | None:
    """JWT iss only when it is a currently active paired client. Else panel."""
    client_id = house_clients.request_client_id(request)
    if not client_id:
        return None
    for row in _active_clients(hass):
        if row.get("clientId") == client_id:
            return client_id
    return None


def _index_by_id(rows: list[dict[str, Any]]) -> dict[str, int]:
    found: dict[str, int] = {}
    for index, row in enumerate(rows):
        mid = str(row.get("id") or "")
        if mid and mid not in found:
            found[mid] = index
    return found


def _sender_client_ids(row: dict[str, Any], active: list[dict[str, Any]]) -> set[str]:
    """Who must not count as other. New rows store senderClientId.

    Old rows are not rewritten. If the stored sender string equals a
    client's current display name, that client is the sender. No match
    means a panel-authored row: any paired client can count.
    """
    stored = row.get("senderClientId")
    if isinstance(stored, str) and stored.strip():
        return {stored.strip()}
    label = row.get("senderLabel")
    if not isinstance(label, str) or not label:
        return set()
    return {
        str(client.get("clientId") or "")
        for client in active
        if client.get("displayName") == label and client.get("clientId")
    }


def receipt_flags(
    row: dict[str, Any],
    rows: list[dict[str, Any]],
    active: list[dict[str, Any]],
    cursors: dict[str, Any],
) -> tuple[bool, bool]:
    """Booleans only. Sender never counts. Inactive clients never count."""
    index = _index_by_id(rows)
    senders = _sender_client_ids(row, active)
    active_ids = {str(client.get("clientId") or "") for client in active if client.get("clientId")}
    delivered = row.get("deliveredClientIds")
    if not isinstance(delivered, list):
        delivered = []
    delivered_to_other = any(
        isinstance(cid, str) and cid in active_ids and cid not in senders for cid in delivered
    )
    my_index = index.get(str(row.get("id") or ""))
    read_by_other = False
    if my_index is not None and isinstance(cursors, dict):
        for cid, seen in cursors.items():
            if not isinstance(cid, str) or cid not in active_ids or cid in senders:
                continue
            seen_index = index.get(str(seen))
            if seen_index is not None and seen_index >= my_index:
                read_by_other = True
                break
    return delivered_to_other, read_by_other


def _mark_delivered(store: dict[str, Any], client_id: str, active: list[dict[str, Any]]) -> bool:
    """This paired client received every message they did not send."""
    changed = False
    for row in store.get("messages") or []:
        if client_id in _sender_client_ids(row, active):
            continue
        delivered = row.get("deliveredClientIds")
        if not isinstance(delivered, list):
            delivered = []
        if client_id in delivered:
            continue
        delivered.append(client_id)
        row["deliveredClientIds"] = delivered
        changed = True
    return changed


def _cursors(store: dict[str, Any]) -> dict[str, Any]:
    raw = store.get("readCursors")
    return raw if isinstance(raw, dict) else {}


def _public_row(
    caller: dict[str, str],
    row: dict[str, Any],
    text: str,
    rows: list[dict[str, Any]] | None = None,
    active: list[dict[str, Any]] | None = None,
    cursors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """List shape. hasImage is a flag. Image bytes stay off this document.

    deliveredToOther and readByOther are booleans. No client-id lists.
    """
    ctype = row.get("imageContentType")
    has_image = isinstance(ctype, str) and ctype in IMAGE_TYPES and bool(row.get("imageCiphertext"))
    if rows is None:
        delivered, read = False, False
    else:
        delivered, read = receipt_flags(row, rows, active or [], cursors or {})
    out = {
        "id": row.get("id"),
        "sender": row.get("senderLabel") or "",
        "senderKind": row.get("senderKind") or "",
        "text": text,
        "createdAt": row.get("createdAt"),
        "retention": row.get("retention") or "keep",
        "expiresAt": row.get("expiresAt"),
        "canDelete": _can_delete(caller, row),
        "hasImage": has_image,
        "deliveredToOther": bool(delivered),
        "readByOther": bool(read),
    }
    if has_image:
        out["imageContentType"] = ctype
    return out


def _error(code: str, message: str, status: int) -> tuple[dict[str, Any], int]:
    return {"error": {"code": code, "message": message}}, status


def list_document(hass, request, now: datetime | None = None) -> dict[str, Any]:
    moment = _utc(now)
    store = _read_store(hass)
    changed = _purge(store, moment)
    active = _active_clients(hass)
    # Panel GETs must not mark delivery. Only a paired client's GET does,
    # and only for messages that client did not send.
    caller_id = _active_paired_client_id(hass, request)
    if caller_id and _mark_delivered(store, caller_id, active):
        changed = True
    key = load_chat_key(hass)
    caller = caller_identity(hass, request)
    rows = list(store.get("messages") or [])
    cursors = _cursors(store)
    public: list[dict[str, Any]] = []
    for row in rows:
        opened = _open(key, str(row.get("id") or ""), row.get("nonce"), row.get("ciphertext"))
        if opened is None:
            continue
        public.append(_public_row(caller, row, opened, rows, active, cursors))
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


def _decode_image(payload: dict) -> tuple[bytes | None, str | None, tuple | None]:
    """Return (bytes, content_type, error). No image is (None, None, None).

    Does not log the bytes. data: URLs are rejected. Decoded size is capped.
    """
    raw_b64 = payload.get("imageBase64")
    raw_type = payload.get("imageContentType")
    has_b64 = raw_b64 not in (None, "")
    has_type = raw_type not in (None, "")
    if not has_b64 and not has_type:
        return None, None, None
    if not isinstance(raw_b64, str) or not isinstance(raw_type, str):
        return None, None, _error("bad_image", "image must be standard base64", 400)
    ctype = raw_type.strip().lower()
    if ctype not in IMAGE_TYPES:
        return None, None, _error("bad_image", "image must be jpeg, png, or webp", 400)
    encoded = raw_b64.strip()
    if encoded.lower().startswith("data:"):
        return None, None, _error("bad_image", "image must be standard base64", 400)
    try:
        blob = base64.b64decode(encoded, validate=True)
    except Exception:
        return None, None, _error("bad_image", "image must be standard base64", 400)
    if not blob:
        return None, None, _error("bad_image", "image is empty", 400)
    if len(blob) > CHAT_IMAGE_MAX:
        return None, None, _error("too_large", "Image is too large", 400)
    return blob, ctype, None


def create_message(hass, request, payload: Any, now: datetime | None = None) -> tuple[dict[str, Any], int]:
    if not caller_authorized(request):
        return _error("unauthorized", "Unauthorized", 401)
    if not isinstance(payload, dict):
        return _error("empty", "Message is empty", 400)
    if "text" in payload:
        raw_text = payload.get("text")
        if raw_text is None:
            raw_text = ""
        if not isinstance(raw_text, str):
            return _error("bad_text", "text must be a string", 400)
    else:
        raw_text = ""
    text = raw_text.strip()
    if len(text) > CHAT_TEXT_MAX:
        return _error("too_long", "Message is too long", 400)
    image, image_type, image_error = _decode_image(payload)
    if image_error is not None:
        return image_error
    if not text and image is None:
        return _error("empty", "Message is empty", 400)
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
        posted = _safe_name(payload.get("deviceName"))
        # Prefer the paired-client registry (match by credential / JWT iss),
        # not client_name == "Patrimony iOS". Edited display names win over
        # a later POST deviceName. Panel posts without deviceName use HA (username).
        registered = house_clients.client_sender_label(hass, request, posted)
        if registered is not None:
            caller = registered
        elif posted:
            # 0.4.79: honor deviceName on a misclassified phone bearer.
            caller = {
                "kind": "ios",
                "label": posted,
                "key": "ios:" + posted,
            }
        else:
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
        if image is not None:
            image_nonce, image_ct = _seal_image(key, message_id, image)
            row["imageContentType"] = image_type
            row["imageNonce"] = image_nonce
            row["imageCiphertext"] = image_ct
        if caller.get("kind") == "ios":
            sender_client = house_clients.request_client_id(request)
            if sender_client:
                row["senderClientId"] = sender_client
        rows = list(store.get("messages") or [])
        rows.append(row)
        if len(rows) > _MAX_STORED:
            rows = rows[-_MAX_STORED:]
        store["messages"] = rows
        _write_store(hass, store)
    except Exception:
        _LOGGER.error("patrimony_chat_store_failed")
        return _error("store_failed", "Could not store the message", 500)
    return _public_row(caller, row, text, rows, _active_clients(hass), _cursors(store)), 201


def notify_house_chat(hass) -> int | None:
    """Existing events POST. Fixed sentence only. Not the attention debounce.

    Does not take message text or image bytes. One POST per call.
    """
    entry = _first_entry(hass)
    if entry is None:
        return None
    data = getattr(entry, "data", None) or {}
    options = getattr(entry, "options", None) or {}
    key = str(options.get(CONF_HOUSE_EVENT_KEY) or "").strip()
    property_id = data.get(CONF_PROPERTY_ID)
    if not property_id or not is_usable_house_event_key(key):
        return None
    title = chat_push_title(data.get(CONF_DISPLAY_NAME))
    try:
        status = load_card_id_and_post(
            hass,
            str(property_id),
            key,
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
            return _error("forbidden", "You cannot delete this message", 403)
    except Exception:
        _LOGGER.error("patrimony_chat_delete_failed")
        return _error("store_failed", "Could not delete the message", 500)
    return {"ok": True}, 200


def read_chat_image(hass, request, message_id: str, now: datetime | None = None):
    """Raw image bytes for one message. Same auth as chat. 404 if none."""
    if not caller_authorized(request):
        body, status = _error("unauthorized", "Unauthorized", 401)
        return body, status, None
    wanted = str(message_id or "").strip()
    if not wanted:
        body, status = _error("not_found", "Message not found", 404)
        return body, status, None
    moment = _utc(now)
    try:
        store = _read_store(hass)
        changed = _purge(store, moment)
        if changed:
            _write_store(hass, store)
        row = None
        for item in store.get("messages") or []:
            if str(item.get("id") or "") == wanted:
                row = item
                break
        if row is None or not row.get("imageCiphertext"):
            body, status = _error("not_found", "Message not found", 404)
            return body, status, None
        ctype = row.get("imageContentType")
        if not isinstance(ctype, str) or ctype not in IMAGE_TYPES:
            body, status = _error("not_found", "Message not found", 404)
            return body, status, None
        key = load_chat_key(hass)
        blob = _open_image(key, wanted, row.get("imageNonce"), row.get("imageCiphertext"))
        if not blob:
            body, status = _error("not_found", "Message not found", 404)
            return body, status, None
        return blob, 200, ctype
    except Exception:
        _LOGGER.error("patrimony_chat_image_failed")
        body, status = _error("store_failed", "Could not read chat", 500)
        return body, status, None


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


class PatrimonyChatImageView(HomeAssistantView):
    """One image. Same HA Bearer as chat. Raw bytes, not the list document."""

    url = CHAT_IMAGE_PATH
    name = "api:patrimony_collection:chat_image"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request, message_id=""):
        from aiohttp import web

        job = getattr(self.hass, "async_add_executor_job", None)

        def _run():
            return read_chat_image(self.hass, request, message_id)

        try:
            if job:
                body, status, content_type = await job(_run)
            else:
                body, status, content_type = _run()
        except Exception:
            _LOGGER.error("patrimony_chat_image_failed")
            return web.json_response(
                {"error": {"code": "store_failed", "message": "Could not read chat"}},
                status=500,
            )
        if content_type and status == 200 and isinstance(body, (bytes, bytearray)):
            return web.Response(body=bytes(body), status=200, content_type=content_type)
        return web.json_response(body, status=status)


def handle_read(hass, request, payload: Any, now: datetime | None = None) -> tuple[dict[str, Any], int]:
    """Paired-client read cursor. Monotonic. Unknown id does not move it."""
    if not caller_authorized(request):
        return _error("unauthorized", "Unauthorized", 401)
    client_id = _active_paired_client_id(hass, request)
    if not client_id:
        return _error("forbidden", "Only a paired phone can mark chat read", 403)
    if not isinstance(payload, dict):
        return _error("unknown_message", "Unknown message", 400)
    raw_id = payload.get("lastSeenMessageId")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return _error("unknown_message", "Unknown message", 400)
    wanted = raw_id.strip()
    moment = _utc(now)
    try:
        store = _read_store(hass)
        changed = _purge(store, moment)
        rows = list(store.get("messages") or [])
        index = _index_by_id(rows)
        if wanted not in index:
            return _error("unknown_message", "Unknown message", 400)
        cursors = dict(_cursors(store))
        prev = cursors.get(client_id)
        prev_index = index.get(str(prev)) if isinstance(prev, str) else None
        new_index = index[wanted]
        # Missing previous target is not a position, so a known id may land.
        # An older known id must not move the cursor backward.
        if prev_index is None or new_index >= prev_index:
            if cursors.get(client_id) != wanted:
                cursors[client_id] = wanted
                changed = True
        if changed:
            store["messages"] = rows
            if cursors:
                store["readCursors"] = cursors
            _write_store(hass, store)
    except Exception:
        _LOGGER.error("patrimony_chat_read_failed")
        return _error("store_failed", "Could not store the read cursor", 500)
    return {"ok": True}, 200


class PatrimonyChatReadView(HomeAssistantView):
    """Read cursor for one paired client. Not the panel."""

    url = CHAT_READ_PATH
    name = "api:patrimony_collection:chat_read"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request):
        from aiohttp import web

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"code": "unknown_message", "message": "Unknown message"}},
                status=400,
            )
        job = getattr(self.hass, "async_add_executor_job", None)

        def _run():
            return handle_read(self.hass, request, payload)

        try:
            if job:
                body, status = await job(_run)
            else:
                body, status = _run()
        except Exception:
            _LOGGER.error("patrimony_chat_read_failed")
            return web.json_response(
                {"error": {"code": "store_failed", "message": "Could not store the read cursor"}},
                status=500,
            )
        return web.json_response(body, status=status)


def register_views(hass: HomeAssistant) -> None:
    hass.http.register_view(PatrimonyChatView(hass))
    hass.http.register_view(PatrimonyChatReadView(hass))
    hass.http.register_view(PatrimonyChatImageView(hass))
    hass.http.register_view(PatrimonyChatMessageView(hass))
