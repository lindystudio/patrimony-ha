"""House notepad. Off PresentationDocument.

0.4.122: the notes document grows an additive list of items:

    {schemaVersion: 1, propertyId, text, items: [{id, title, body, order, updatedAt,
     updatedBy: {memberId, kind} | null}], updatedAt, etag, textWritable}

* ``items`` is always present on the wire (possibly []); array order == ``order``.
* ``text`` is a derived projection of the items for old phones and the HA panel.
* ``etag`` is the document-level concurrency token (changes on every effective write).
* Migration (marker notes_items_04122): stored text without items becomes one item with
  an empty title and the whole text as body. Idempotent.
* Legacy ``{text}`` PUT stays accepted while the list was never written through
  ``items`` (``textWritable``); afterwards it is a 409 notes_items_exist, items untouched.
* List PUT is document-level, validated (title 80, body 8000, 50 items), and needs
  ifMatch on both the owner and member paths.

Stored on the house as plaintext JSON, 0600, atomic writes (fileio). Never log note
titles or bodies.
"""

from __future__ import annotations

from . import fileio

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .const import (
    NOTES_FILE,
    NOTES_MAX_CHARS,
    NOTES_MAX_ITEMS,
    NOTES_TITLE_MAX,
    SCHEMA_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# One lock for the owner and member paths (read-check-write is atomic per process).
NOTES_LOCK = threading.RLock()
MIGRATION_ITEMS = "notes_items_04122"
_UUID_LEN = 36


class NotesInputError(ValueError):
    """400 on both paths. ``index`` is the offending item (or None)."""

    def __init__(self, code: str, message: str, index: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.index = index


class NotesConflict(Exception):
    """409 on both paths, carries the current wire document."""

    def __init__(self, code: str, current: dict[str, Any]):
        super().__init__(code)
        self.code = code
        self.current = current


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
        raw = fileio.read_text(path)
        if raw is None:
            return None
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write(hass, stored: dict[str, Any]) -> None:
    fileio.write_text(notes_path(hass), json.dumps(stored, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- items helpers

def _is_uuid(raw: Any) -> bool:
    if not isinstance(raw, str) or len(raw) != _UUID_LEN:
        return False
    try:
        return str(uuid.UUID(raw)) == raw.lower()
    except (ValueError, AttributeError):
        return False


def _new_id() -> str:
    return str(uuid.uuid4())


def _clean_by(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind not in ("owner", "member"):
        return None
    mid = raw.get("memberId")
    return {"memberId": str(mid)[:64] if (kind == "member" and mid) else None, "kind": kind}


def _stored_items(stored: dict[str, Any]) -> list[dict[str, Any]]:
    """Stored items, defensively cleaned, sorted by order (stable), order rewritten."""
    raw = stored.get("items")
    if not isinstance(raw, list):
        return []
    out = []
    for pos, it in enumerate(raw):
        if not isinstance(it, dict) or not _is_uuid(it.get("id")):
            continue
        try:
            order = int(it.get("order", pos))
        except (TypeError, ValueError):
            order = pos
        out.append((order, pos, {
            "id": str(it["id"]).lower(),
            "title": str(it.get("title") or "")[:NOTES_TITLE_MAX],
            "body": str(it.get("body") or "")[:NOTES_MAX_CHARS],
            "order": 0,
            "updatedAt": str(it["updatedAt"]) if it.get("updatedAt") else None,
            "updatedBy": _clean_by(it.get("updatedBy")),
        }))
    out.sort(key=lambda t: (t[0], t[1]))
    items = [t[2] for t in out][:NOTES_MAX_ITEMS]
    for i, it in enumerate(items):
        it["order"] = i
    return items


def projection(items: list[dict[str, Any]]) -> str:
    """Legacy text (read-only once the list is edited): each item is title + newline + body,
    the title alone when the body is empty, the body alone when untitled; items with
    neither are skipped; blank line between items; clipped to 8000. Never empty while
    any item has a title or body."""
    parts = []
    for it in items:
        title, body = it.get("title") or "", it.get("body") or ""
        if title and body:
            parts.append(f"{title}\n{body}")
        elif title or body:
            parts.append(title or body)
    return _clip("\n\n".join(parts))


def etag_of(text: str, items: list[dict[str, Any]], updated: str | None) -> str:
    canon = json.dumps({"text": text, "items": items, "updatedAt": updated},
                       sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return '"' + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32] + '"'


def _migrations(stored: dict[str, Any]) -> list[str]:
    raw = stored.get("migrations")
    return [m for m in raw if isinstance(m, str)] if isinstance(raw, list) else []


def _migrate(stored: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """(stored, changed). Text without items -> one untitled item. Marker makes it one-time."""
    done = _migrations(stored)
    if MIGRATION_ITEMS in done:
        return stored, False
    out = dict(stored)
    text = _clip(str(stored.get("text") if stored.get("text") is not None else ""))
    items = _stored_items(stored)
    created = 0
    if not items and text.strip():
        items = [{
            "id": _new_id(),
            "title": "",
            "body": text,
            "order": 0,
            "updatedAt": str(stored.get("updatedAt")) if stored.get("updatedAt") else _utc_z(),
            "updatedBy": None,
        }]
        created = 1
    out["items"] = items
    out["listEdited"] = bool(stored.get("listEdited"))
    out["migrations"] = done + [MIGRATION_ITEMS]
    _LOGGER.info("patrimony_notes_items_migrated created=%d", created)
    return out, True


def _load_migrated(hass) -> dict[str, Any] | None:
    """Stored dict after the one-time migration (persisted). None when no file. Lock held."""
    raw = load_notes(hass)
    if raw is None:
        return None
    stored, changed = _migrate(raw)
    if changed:
        _write(hass, stored)
    return stored


def _wire(stored: dict[str, Any] | None, property_id: str | None) -> dict[str, Any]:
    if stored is None:
        text, items, updated, edited = "", [], None, False
    else:
        items = _stored_items(stored)
        edited = bool(stored.get("listEdited"))
        # The projection is authoritative once the list exists; before that, stored text.
        text = projection(items) if items else _clip(
            str(stored.get("text") if stored.get("text") is not None else ""))
        updated = str(stored["updatedAt"]) if stored.get("updatedAt") is not None else None
    return {
        "schemaVersion": SCHEMA_VERSION,
        "propertyId": property_id,
        "text": text,
        "items": items,
        "updatedAt": updated,
        "etag": etag_of(text, items, updated),
        "textWritable": not edited,
    }


def document(hass, property_id: str | None) -> dict[str, Any]:
    with NOTES_LOCK:
        return _wire(_load_migrated(hass), property_id)


# ---------------------------------------------------------------- ifMatch

def _norm_tag(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    return raw.strip().strip('"')


def if_match_ok(current: dict[str, Any], if_match: Any, *, allow_updated_at: bool) -> bool:
    """True when if_match names the current document (etag, member GET ETag, or legacy updatedAt)."""
    if if_match is None:
        return False
    want = _norm_tag(str(if_match))
    if want == _norm_tag(current.get("etag") or ""):
        return True
    from .calendars import etag_for

    if want == _norm_tag(etag_for(current)):
        return True
    # The member GET ETag is computed on the document as sealed (same dict).
    if allow_updated_at:
        return want == str(current.get("updatedAt") or "")
    return False


# ---------------------------------------------------------------- legacy text PUT

def save_notes(hass, text: Any, property_id: str | None, *, if_match: Any = None,
               check: bool = False, actor: dict[str, Any] | None = None) -> dict[str, Any]:
    """Legacy {text} write. Raises NotesConflict (notes_conflict / notes_items_exist).

    ``check``: honour ``if_match`` (None/"" means 'no note yet', as before 0.4.122).
    The single migrated/legacy item follows the text until the list is written via items.
    """
    clipped = _clip("" if text is None else str(text))
    with NOTES_LOCK:
        stored = _load_migrated(hass)
        current = _wire(stored, property_id)
        if check and if_match is not None:
            raw = str(if_match).strip()
            if raw == "":
                ok = current.get("updatedAt") is None
            else:
                ok = if_match_ok(current, raw, allow_updated_at=True)
            if not ok:
                raise NotesConflict("notes_conflict", current)
        if not current["textWritable"]:
            raise NotesConflict("notes_items_exist", current)
        now = _utc_z()
        old = _stored_items(stored or {})
        if clipped.strip():
            keep_id = old[0]["id"] if old else _new_id()
            items = [{"id": keep_id, "title": "", "body": clipped, "order": 0,
                      "updatedAt": now, "updatedBy": _clean_by(actor)}]
        else:
            items = []
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "propertyId": property_id,
            "text": clipped,
            "updatedAt": now,
            "items": items,
            "listEdited": False,
            "migrations": _migrations(stored or {}) or [MIGRATION_ITEMS],
        }
        if MIGRATION_ITEMS not in payload["migrations"]:
            payload["migrations"] = payload["migrations"] + [MIGRATION_ITEMS]
        _write(hass, payload)
        return _wire(payload, property_id)


# ---------------------------------------------------------------- list PUT

def validate_items(raw: Any) -> list[dict[str, Any]]:
    """Request items -> [{id|None, title, body}] in array order. Raises NotesInputError."""
    if not isinstance(raw, list):
        raise NotesInputError("notes_bad_items", "items must be a list")
    if len(raw) > NOTES_MAX_ITEMS:
        raise NotesInputError("notes_too_many_items", f"at most {NOTES_MAX_ITEMS} notes")
    seen: set[str] = set()
    out = []
    for i, it in enumerate(raw):
        if not isinstance(it, dict):
            raise NotesInputError("notes_bad_items", "each item must be an object", i)
        title = it.get("title", "")
        body = it.get("body", "")
        if title is None:
            title = ""
        if body is None:
            body = ""
        if not isinstance(title, str) or not isinstance(body, str):
            raise NotesInputError("notes_bad_items", "title and body must be strings", i)
        if len(title) > NOTES_TITLE_MAX:
            raise NotesInputError("notes_title_too_long", f"title max {NOTES_TITLE_MAX} characters", i)
        if len(body) > NOTES_MAX_CHARS:
            raise NotesInputError("notes_body_too_long", f"body max {NOTES_MAX_CHARS} characters", i)
        nid = it.get("id")
        if nid is not None:
            if not _is_uuid(nid):
                raise NotesInputError("notes_bad_id", "id must be a UUID", i)
            nid = nid.lower()
            if nid in seen:
                raise NotesInputError("notes_duplicate_id", "duplicate id", i)
            seen.add(nid)
        out.append({"id": nid, "title": title, "body": body})
    return out


def save_items(hass, items_raw: Any, if_match: Any, property_id: str | None,
               actor: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Document-level list write. Returns (wire document, changed).

    Raises NotesInputError (400) before any ifMatch check, then NotesConflict (409).
    """
    wanted = validate_items(items_raw)
    if if_match is None or (isinstance(if_match, str) and not if_match.strip()):
        raise NotesInputError("notes_if_match_required", "ifMatch is required for an items write")
    by = _clean_by(actor)
    with NOTES_LOCK:
        stored = _load_migrated(hass)
        current = _wire(stored, property_id)
        if not if_match_ok(current, if_match, allow_updated_at=False):
            raise NotesConflict("notes_conflict", current)
        old = {it["id"]: it for it in current["items"]}
        now = _utc_z()
        items = []
        for i, w in enumerate(wanted):
            prev = old.get(w["id"]) if w["id"] else None
            if prev is not None and prev["title"] == w["title"] and prev["body"] == w["body"]:
                items.append({**prev, "order": i})
            else:
                items.append({"id": w["id"] or _new_id(), "title": w["title"], "body": w["body"],
                              "order": i, "updatedAt": now, "updatedBy": by})
        same = items == current["items"]
        if same and not current["textWritable"]:
            return current, False
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "propertyId": property_id,
            "text": projection(items),
            "updatedAt": current["updatedAt"] if same else now,
            "items": items,
            "listEdited": True,
            "migrations": _migrations(stored or {}) or [MIGRATION_ITEMS],
        }
        if MIGRATION_ITEMS not in payload["migrations"]:
            payload["migrations"] = payload["migrations"] + [MIGRATION_ITEMS]
        _write(hass, payload)
        return _wire(payload, property_id), not same


def conflict_body(exc: NotesConflict) -> dict[str, Any]:
    msg = ("Notes changed since ifMatch" if exc.code == "notes_conflict"
           else "Notes are a list now; send items")
    return {**exc.current, "error": {"code": exc.code, "message": msg}, "current": exc.current}


def input_error_body(exc: NotesInputError) -> dict[str, Any]:
    err: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.index is not None:
        err["index"] = exc.index
    return {"error": err}
