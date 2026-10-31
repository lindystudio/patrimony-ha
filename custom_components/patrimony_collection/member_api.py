"""0.4.102 member read + write: photo, contacts, notes, chat post. 0.4.103: chat image read.

Product decision: everyone invited can read and write anything. No owner switch.

Every route: pmt_ member token (401 unauthorized / member_token_expired / member_token_revoked),
then the same PQ rule as /m/state (shield On + member_pq_required + no session -> 428
pq_session_required). Responses are sealed with THIS member's session key. Request bodies are
sealed too: the body is the owner envelope shape {pqShield, kem, aead, nonce, ct}; a plain JSON
body is accepted only while the shield is Off or member_pq_required is False (transition), the
same cases in which responses go out plain.

GETs carry an ETag computed on the plaintext (calendars rule) and answer 304 only after the PQ
rule passed. Error bodies are plain {"error": {code, message}} like every other member error,
except 409 notes_conflict, which carries the current note and is therefore sealed.

Member writes are logged (who and when, never content) in ios_session.json "memberWriteLog",
readable by the owner at GET /api/patrimony_collection/members/write_log.
Never log photo bytes, contact rows, note text, chat text or tokens.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from . import clients as house_clients
from . import contacts as house_contacts
from . import members as house_members
from . import notes as house_notes
from . import photo as house_photo
from .const import MAX_CONTACTS, PHOTO_MAX_BYTES

_LOGGER = logging.getLogger(__name__)

MEMBER_PHOTO_PATH = "/api/patrimony_collection/m/photo"
MEMBER_CONTACTS_PATH = "/api/patrimony_collection/m/contacts"
MEMBER_CONTACT_PATH = "/api/patrimony_collection/m/contacts/{contact_id}"
MEMBER_NOTES_PATH = "/api/patrimony_collection/m/notes"
MEMBER_WRITE_LOG_PATH = "/api/patrimony_collection/members/write_log"
MEMBER_CHAT_READ_PATH = "/api/patrimony_collection/m/chat/read"  # 0.4.116
MEMBER_CHAT_IMAGE_PATH = "/api/patrimony_collection/m/chat/{message_id}/image"  # 0.4.103, mirrors /chat/{id}/image

WRITE_LOG_STORE = "memberWriteLog"
WRITE_LOG_MAX = 100

# Sealed request caps (envelope JSON bytes). A sealed body is base64 of the plaintext JSON,
# which may itself hold base64 image bytes: ~1.8x the raw image.
BODY_MAX_DEFAULT = 512 * 1024
BODY_MAX_PHOTO = 2 * PHOTO_MAX_BYTES + 256 * 1024  # 2 MB image -> ~3.6 MB sealed
BODY_MAX_CHAT = 8 * 1024 * 1024 + 512 * 1024  # 4 MB chat image -> ~7.2 MB sealed

_CONTACTS_LOCK = threading.Lock()
_NOTES_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()

try:
    from homeassistant.components.http import HomeAssistantView
except ImportError:  # offline tests
    class HomeAssistantView:  # type: ignore[no-redef]
        url = ""
        name = ""
        requires_auth = True


def _utc_z(ts: float | None = None) -> str:
    moment = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- write log

def record_member_write(hass, row: dict[str, Any], scope: str, action: str) -> dict[str, Any]:
    """Blocking. Who and when only (memberId, clientId, scope, action). Never content."""
    mid = str(row.get("memberId") or "")
    entry = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "memberId": mid[:64] or None,
        "clientId": house_members.client_id_for(mid) if mid else None,
        "scope": str(scope)[:16],
        "action": str(action)[:24],
    }
    with _LOG_LOCK:
        try:
            from .ios_session import update_store

            def mutate(data):
                rows = data.get(WRITE_LOG_STORE)
                rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
                rows.append(entry)
                data[WRITE_LOG_STORE] = rows[-WRITE_LOG_MAX:]

            # 0.4.112: same lock as owner session posts; unreadable file left alone.
            if update_store(hass, mutate) is None:
                raise RuntimeError("unreadable")
        except Exception:
            _LOGGER.warning("patrimony_member_write_log_failed")
    _LOGGER.info("patrimony_member_write scope=%s action=%s member=%s", entry["scope"], entry["action"], mid[:8])
    return entry


def write_log(hass) -> list[dict[str, Any]]:
    rows = house_clients._read_store(hass).get(WRITE_LOG_STORE)
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)][-WRITE_LOG_MAX:]


# ---------------------------------------------------------------- PQ in / out

def _hh():
    from . import http as hh

    return hh


def _mgr(hass):
    from .pq_shield.session import get_manager

    return get_manager(hass)


def _pq_gate(hass, row):
    """428 before anything else when the member must seal and has no session."""
    from aiohttp import web

    hh = _hh()
    mgr = _mgr(hass)
    mid = str(row.get("memberId") or "")
    if mgr.member_shield_on and hh.member_pq_required(hass) and not mgr.member_session_present(mid):
        return web.json_response(
            {
                "error": {"code": "pq_session_required", "message": "PQ Shield session required"},
                "pqShield": mgr.member_capability(mid),
            },
            status=428,
            headers={"Cache-Control": "no-store"},
        )
    return None


def _is_envelope(body: Any) -> bool:
    return isinstance(body, dict) and "ct" in body and "nonce" in body and "pqShield" in body


async def open_body(hass, request, row, *, max_bytes: int = BODY_MAX_DEFAULT):
    """Returns (payload_dict, None) or (None, error_response). Call after auth + _pq_gate."""
    hh = _hh()
    headers = getattr(request, "headers", None) or {}
    try:
        length = int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        length = 0
    if length > max_bytes:
        return None, hh._json_error("too_large", "Request body is too large", 413, headers={"Cache-Control": "no-store"})
    try:
        body = await request.json()
    except Exception:
        return None, hh._json_error("bad_json", "body must be JSON", 400, headers={"Cache-Control": "no-store"})
    mgr = _mgr(hass)
    mid = str(row.get("memberId") or "")
    sealed_mode = mgr.member_shield_on and mgr.member_session_present(mid)
    if _is_envelope(body):
        if not mgr.member_shield_on:
            return None, hh._json_error("pq_shield_off", "PQ Shield is off; send plain JSON", 400,
                                        headers={"Cache-Control": "no-store"})
        try:
            opened = await hh._run(hass, mgr.open_from_member, mid, body)
        except Exception:
            return None, hh._json_error("pq_open_failed", "Could not open the sealed body", 400,
                                        headers={"Cache-Control": "no-store"})
        if opened is None:
            return None, hh._json_error("pq_session_required", "PQ Shield session required", 428,
                                        headers={"Cache-Control": "no-store"})
        return opened, None
    if sealed_mode:
        # A member with a live session must seal its writes (both directions).
        return None, hh._json_error("pq_envelope_required", "Body must be a PQ envelope", 400,
                                    headers={"Cache-Control": "no-store"})
    if not isinstance(body, dict):
        return None, hh._json_error("bad_json", "body must be a JSON object", 400, headers={"Cache-Control": "no-store"})
    return body, None


async def sealed(hass, row, document, status: int = 200, headers: dict | None = None):
    """Seal with THIS member's key (plain when shield Off / transition), any status."""
    from aiohttp import web

    hh = _hh()
    mgr = _mgr(hass)
    mid = str(row.get("memberId") or "")
    out_headers = {"Cache-Control": "no-store", **(headers or {})}
    try:
        payload = await hh._run(hass, mgr.wrap_for_member, mid, document)
    except Exception:
        _LOGGER.error("pq_shield_member_wrap_failed")
        return hh._json_error("pq_wrap_failed", "Could not seal the response", 500, headers={"Cache-Control": "no-store"})
    if payload is None:
        if hh.member_pq_required(hass):
            return web.json_response(
                {"error": {"code": "pq_session_required", "message": "PQ Shield session required"},
                 "pqShield": mgr.member_capability(mid)},
                status=428, headers={"Cache-Control": "no-store"},
            )
        payload = document
    return web.json_response(payload, status=status, headers=out_headers)


async def sealed_get(hass, request, row, document):
    """GET: ETag on plaintext + 304 after the PQ rule (calendars._member_response)."""
    from .calendars import _member_response

    return await _member_response(hass, request, row, document)


async def _auth(hass, request, *, configured: bool = True):
    hh = _hh()
    row, deny = await hh._member_auth(hass, request)
    if deny:
        return None, deny
    if configured and hh._first_entry(hass) is None:
        return None, hh._not_configured()
    gate = _pq_gate(hass, row)
    if gate is not None:
        return None, gate
    return row, None


# ---------------------------------------------------------------- photo

def photo_document(hass) -> dict[str, Any] | None:
    """Blocking. {contentType, data(base64), updatedAt, isDefault} or None (no photo at all)."""
    resolved = house_photo.resolve_photo(hass)
    if resolved is None:
        return None
    data, ctype, source = resolved
    updated = None
    if source == "custom":
        try:
            updated = _utc_z(os.stat(house_photo.photo_path(hass)).st_mtime)
        except OSError:
            updated = None
    return {
        "contentType": ctype,
        "data": base64.b64encode(data).decode("ascii"),
        "updatedAt": updated,
        "isDefault": source != "custom",
    }


def _photo_meta(hass) -> dict[str, Any]:
    doc = photo_document(hass)
    if doc is None:
        return {"ok": True, "contentType": None, "updatedAt": None, "isDefault": True, "present": False}
    return {"ok": True, "contentType": doc["contentType"], "updatedAt": doc["updatedAt"],
            "isDefault": doc["isDefault"], "present": True}


class PatrimonyMemberPhotoView(HomeAssistantView):
    url = MEMBER_PHOTO_PATH
    name = "api:patrimony_collection:member_photo"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request):
        hh = _hh()
        row, deny = await _auth(self.hass, request, configured=False)
        if deny:
            return deny
        doc = await hh._run(self.hass, photo_document, self.hass)
        if doc is None:
            return hh._json_error("no_photo", "No house photograph", 404, headers={"Cache-Control": "no-store"})
        return await sealed_get(self.hass, request, row, doc)

    async def _upload(self, request, payload, row):
        hh = _hh()
        raw_type = payload.get("contentType")
        raw_data = payload.get("data")
        if not isinstance(raw_type, str) or not isinstance(raw_data, str) or raw_data.strip().lower().startswith("data:"):
            return hh._json_error("bad_type", "image/jpeg or image/webp", 400, headers={"Cache-Control": "no-store"})
        if len(raw_data) > (PHOTO_MAX_BYTES * 4) // 3 + 8:
            return hh._json_error("too_large", "max 2 MB", 400, headers={"Cache-Control": "no-store"})
        try:
            blob = base64.b64decode(raw_data.strip(), validate=True)
        except (binascii.Error, ValueError):
            return hh._json_error("bad_type", "image/jpeg or image/webp", 400, headers={"Cache-Control": "no-store"})
        reason = house_photo.reject_put(raw_type, blob)  # owner validation, unchanged
        if reason:
            code = "too_large" if "2 MB" in reason else "bad_type"
            return hh._json_error(code, reason, 400, headers={"Cache-Control": "no-store"})
        await hh._run(self.hass, house_photo.save_photo, self.hass, blob)
        await hh._run(self.hass, record_member_write, self.hass, row, "photo", "set")
        return await sealed(self.hass, row, await hh._run(self.hass, _photo_meta, self.hass))

    async def put(self, request):
        row, deny = await _auth(self.hass, request, configured=False)
        if deny:
            return deny
        payload, err = await open_body(self.hass, request, row, max_bytes=BODY_MAX_PHOTO)
        if err:
            return err
        return await self._upload(request, payload, row)

    async def post(self, request):
        """{contentType, data} = same as PUT; {restoreDefault: true} = owner POST (bundled default)."""
        hh = _hh()
        row, deny = await _auth(self.hass, request, configured=False)
        if deny:
            return deny
        payload, err = await open_body(self.hass, request, row, max_bytes=BODY_MAX_PHOTO)
        if err:
            return err
        if payload.get("restoreDefault") is True:
            ok = await hh._run(self.hass, house_photo.restore_default_photo, self.hass)
            if not ok:
                return hh._json_error("no_default", "No bundled default photograph", 404,
                                      headers={"Cache-Control": "no-store"})
            await hh._run(self.hass, record_member_write, self.hass, row, "photo", "restore_default")
            return await sealed(self.hass, row, await hh._run(self.hass, _photo_meta, self.hass))
        return await self._upload(request, payload, row)

    async def delete(self, request):
        hh = _hh()
        row, deny = await _auth(self.hass, request, configured=False)
        if deny:
            return deny
        if not await hh._run(self.hass, house_photo.delete_photo, self.hass):
            return hh._json_error("no_photo", "No custom house photograph", 404, headers={"Cache-Control": "no-store"})
        await hh._run(self.hass, record_member_write, self.hass, row, "photo", "delete")
        return await sealed(self.hass, row, await hh._run(self.hass, _photo_meta, self.hass))


# ---------------------------------------------------------------- contacts

_ALIASES = {"role": "function", "phone": "tel"}


def _apply_aliases(src: dict[str, Any]) -> dict[str, Any]:
    """Frontend names (role, phone) map onto the owner's stored names (function, tel)."""
    out = dict(src)
    for alias, field in _ALIASES.items():
        if alias in src and field not in src:
            out[field] = src[alias]
        out.pop(alias, None)
    return out


def contacts_document(hass) -> dict[str, Any]:
    return house_contacts.document(hass, _hh()._property_id(hass))


def replace_contacts(hass, raw: Any) -> list[dict[str, str]]:
    rows = [_apply_aliases(r) if isinstance(r, dict) else r for r in raw] if isinstance(raw, list) else raw
    with _CONTACTS_LOCK:
        return house_contacts.save_contacts(hass, rows)


def add_contact(hass, item: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    candidate = _apply_aliases(item)
    candidate.pop("id", None)  # the house assigns the opaque id
    with _CONTACTS_LOCK:
        rows = house_contacts.load_contacts(hass)
        if len(rows) >= MAX_CONTACTS:
            return None, "too_many"
        norm = house_contacts.normalize_contact(candidate)
        if norm is None:
            return None, "empty_contact"
        saved = house_contacts.save_contacts(hass, rows + [norm])
    return next((r for r in saved if r["id"] == norm["id"]), norm), None


def patch_contact(hass, contact_id: str, patch: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    changes = _apply_aliases(patch)
    changes.pop("id", None)
    if "app" not in changes and "method" in changes:
        changes["app"] = changes["method"]
    with _CONTACTS_LOCK:
        rows = house_contacts.load_contacts(hass)
        idx = next((i for i, r in enumerate(rows) if r["id"] == contact_id), None)
        if idx is None:
            return None, "contact_not_found"
        merged = {**rows[idx], **{k: v for k, v in changes.items() if k in ("function", "name", "tel", "app", "method")}}
        if house_contacts.normalize_contact(merged) is None:
            return None, "empty_contact"
        rows[idx] = merged
        saved = house_contacts.save_contacts(hass, rows)
    return next((r for r in saved if r["id"] == contact_id), None), None


def delete_contact(hass, contact_id: str) -> bool:
    with _CONTACTS_LOCK:
        rows = house_contacts.load_contacts(hass)
        kept = [r for r in rows if r["id"] != contact_id]
        if len(kept) == len(rows):
            return False
        house_contacts.save_contacts(hass, kept)
    return True


_CONTACT_ERR = {
    "too_many": ("too_many", f"At most {MAX_CONTACTS} contacts", 400),
    "empty_contact": ("empty_contact", "A contact needs a name, role or phone", 400),
    "contact_not_found": ("contact_not_found", "Contact not found", 404),
}


def _contact_error(code: str):
    c, m, s = _CONTACT_ERR[code]
    return _hh()._json_error(c, m, s, headers={"Cache-Control": "no-store"})


class PatrimonyMemberContactsView(HomeAssistantView):
    url = MEMBER_CONTACTS_PATH
    name = "api:patrimony_collection:member_contacts"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request):
        hh = _hh()
        row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        return await sealed_get(self.hass, request, row, await hh._run(self.hass, contacts_document, self.hass))

    async def put(self, request):
        """Owner semantics: the full ordered list replaces the stored one."""
        hh = _hh()
        row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        payload, err = await open_body(self.hass, request, row)
        if err:
            return err
        raw = payload.get("contacts")
        if not isinstance(raw, list):
            return hh._json_error("bad_json", "contacts list required", 400, headers={"Cache-Control": "no-store"})
        await hh._run(self.hass, replace_contacts, self.hass, raw)
        await hh._run(self.hass, record_member_write, self.hass, row, "contacts", "replace")
        return await sealed(self.hass, row, await hh._run(self.hass, contacts_document, self.hass))

    async def post(self, request):
        hh = _hh()
        row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        payload, err = await open_body(self.hass, request, row)
        if err:
            return err
        item, code = await hh._run(self.hass, add_contact, self.hass, payload)
        if code:
            return _contact_error(code)
        await hh._run(self.hass, record_member_write, self.hass, row, "contacts", "add")
        return await sealed(self.hass, row, item, status=201)


class PatrimonyMemberContactView(HomeAssistantView):
    url = MEMBER_CONTACT_PATH
    name = "api:patrimony_collection:member_contact"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def patch(self, request, contact_id):
        hh = _hh()
        row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        payload, err = await open_body(self.hass, request, row)
        if err:
            return err
        item, code = await hh._run(self.hass, patch_contact, self.hass, str(contact_id), payload)
        if code:
            return _contact_error(code)
        await hh._run(self.hass, record_member_write, self.hass, row, "contacts", "edit")
        return await sealed(self.hass, row, item)

    async def delete(self, request, contact_id):
        hh = _hh()
        row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        if not await hh._run(self.hass, delete_contact, self.hass, str(contact_id)):
            return _contact_error("contact_not_found")
        await hh._run(self.hass, record_member_write, self.hass, row, "contacts", "delete")
        return await sealed(self.hass, row, {"ok": True, "id": str(contact_id)})


# ---------------------------------------------------------------- notes

def notes_document(hass) -> dict[str, Any]:
    return house_notes.document(hass, _hh()._property_id(hass))


def save_notes_checked(hass, text: Any, if_match: Any) -> tuple[dict[str, Any], bool]:
    """(document, conflict). ifMatch (optional) must equal the stored updatedAt; null/"" means
    'no note yet'. The owner path stays last-write-wins."""
    with _NOTES_LOCK:
        current = notes_document(hass)
        if if_match is not None:
            raw = str(if_match).strip()
            want = raw[2:] if raw.startswith("W/") else raw
            have = str(current.get("updatedAt") or "")
            from .calendars import etag_for

            # The stored updatedAt (null/"" = no note yet), or the GET ETag, both accepted.
            if want.strip('"') != have and want != etag_for(current):
                return current, True
        return house_notes.save_notes(hass, text, _hh()._property_id(hass)), False


class PatrimonyMemberNotesView(HomeAssistantView):
    url = MEMBER_NOTES_PATH
    name = "api:patrimony_collection:member_notes"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request):
        hh = _hh()
        row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        return await sealed_get(self.hass, request, row, await hh._run(self.hass, notes_document, self.hass))

    async def put(self, request):
        hh = _hh()
        row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        payload, err = await open_body(self.hass, request, row)
        if err:
            return err
        text = payload.get("text")
        if text is not None and not isinstance(text, str):
            return hh._json_error("bad_text", "text must be a string", 400, headers={"Cache-Control": "no-store"})
        headers = getattr(request, "headers", None) or {}
        if_match = payload["ifMatch"] if "ifMatch" in payload else headers.get("If-Match")
        doc, conflict = await hh._run(self.hass, save_notes_checked, self.hass, text or "", if_match)
        if conflict:
            body = {"error": {"code": "notes_conflict", "message": "Notes changed since ifMatch"}, **doc}
            return await sealed(self.hass, row, body, status=409)
        await hh._run(self.hass, record_member_write, self.hass, row, "notes", "set")
        return await sealed(self.hass, row, doc)


# ---------------------------------------------------------------- chat post (GET stays in http.py)

async def member_chat_post(hass, request):
    from . import chat as house_chat

    hh = _hh()
    row, deny = await _auth(hass, request, configured=False)
    if deny:
        return deny
    payload, err = await open_body(hass, request, row, max_bytes=BODY_MAX_CHAT)
    if err:
        return err
    cid = house_members.client_id_for(row["memberId"])
    try:
        body, status = await hh._run(hass, house_chat.handle_member_post, hass, row, cid, payload)
    except Exception:
        _LOGGER.error("patrimony_chat_store_failed")
        return hh._json_error("store_failed", "Could not store the message", 500, headers={"Cache-Control": "no-store"})
    if status != 201:
        return hh._json_error(body["error"]["code"], body["error"]["message"], status, headers={"Cache-Control": "no-store"})
    action = "post_image" if body.get("hasImage") else "post"
    await hh._run(hass, record_member_write, hass, row, "chat", action)
    return await sealed(hass, row, body, status=201)


async def member_chat_read(hass, request):
    """0.4.116: member read cursor. Sealed body {lastSeenMessageId}; sealed {ok:true}."""
    from . import chat as house_chat

    hh = _hh()
    row, deny = await _auth(hass, request, configured=False)
    if deny:
        return deny
    payload, err = await open_body(hass, request, row, max_bytes=4096)
    if err:
        return err
    try:
        body, status = await hh._run(hass, house_chat.handle_member_read, hass, row, payload)
    except Exception:
        _LOGGER.error("patrimony_chat_read_failed")
        return hh._json_error("store_failed", "Could not store the read cursor", 500, headers={"Cache-Control": "no-store"})
    if status != 200:
        return hh._json_error(body["error"]["code"], body["error"]["message"], status, headers={"Cache-Control": "no-store"})
    return await sealed(hass, row, body)


class PatrimonyMemberChatReadView(HomeAssistantView):
    """0.4.116 POST /m/chat/read (member token, PQ-sealed like POST /m/chat)."""

    url = MEMBER_CHAT_READ_PATH
    name = "api:patrimony_collection:member_chat_read"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def post(self, request):
        return await member_chat_read(self.hass, request)


# ---------------------------------------------------------------- chat image read (0.4.103)

def chat_image_document(hass, message_id: str):
    """Blocking. (doc, None) with {messageId, contentType, data(base64)} or (None, (code, message, status))."""
    from . import chat as house_chat

    blob, status, ctype = house_chat.read_image_blob(hass, message_id)
    if status != 200:
        err = blob.get("error") or {}
        return None, (err.get("code") or "not_found", err.get("message") or "Message not found", status)
    return {"messageId": str(message_id), "contentType": ctype,
            "data": base64.b64encode(blob).decode("ascii")}, None


class PatrimonyMemberChatImageView(HomeAssistantView):
    """Member GET of one chat image (owner path: raw bytes; member: sealed base64 JSON). Images are
    posted inline in POST /m/chat (imageBase64 + imageContentType), exactly like the owner flow."""

    url = MEMBER_CHAT_IMAGE_PATH
    name = "api:patrimony_collection:member_chat_image"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request, message_id):
        hh = _hh()
        row, deny = await _auth(self.hass, request, configured=False)
        if deny:
            return deny
        doc, err = await hh._run(self.hass, chat_image_document, self.hass, str(message_id))
        if err:
            return hh._json_error(err[0], err[1], err[2], headers={"Cache-Control": "no-store"})
        return await sealed_get(self.hass, request, row, doc)


# ---------------------------------------------------------------- owner: write log

class PatrimonyMemberWriteLogView(HomeAssistantView):
    """Owner/admin: who wrote what scope when (no content)."""

    url = MEMBER_WRITE_LOG_PATH
    name = "api:patrimony_collection:member_write_log"
    requires_auth = True

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        hh = _hh()
        if not house_clients.owner_or_admin(self.hass, request):
            return hh._json_error("forbidden", "Owner or admin only", 403)
        rows = await hh._run(self.hass, write_log, self.hass)
        return web.json_response({"memberWriteLog": list(reversed(rows)), "max": WRITE_LOG_MAX},
                                 headers={"Cache-Control": "no-store"})


def register_views(hass) -> None:
    for view in (PatrimonyMemberPhotoView, PatrimonyMemberContactsView, PatrimonyMemberContactView,
                 PatrimonyMemberNotesView, PatrimonyMemberWriteLogView, PatrimonyMemberChatImageView,
                 PatrimonyMemberChatReadView):
        hass.http.register_view(view(hass))
