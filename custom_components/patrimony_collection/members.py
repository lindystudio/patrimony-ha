"""Member access (0.4.96): opaque, scoped, hashed-at-rest member tokens.

A member token is OUR credential (``pmt_`` + 256-bit urlsafe), never an HA token.
HA's auth middleware does not know it, so every HA REST/WS endpoint and every
requires_auth view rejects it. It is accepted only by the member views
(/m/state, /m/pairing, /m/chat GET), which check it here.

Store: <config>/patrimony_collection/members.json, mode 0600, locked atomic writes
(tmp + fsync + os.replace), never overwrites an unreadable file. Rows are keyed by
memberId and hold sha256(token) only. Tokens and hashes are never logged.

Each row carries the backend principalId (from the owner's phone), memberName,
inviteId and role=member. Revoke is per principal: all of its tokens/clients.

Status: pending (minted, never used) | active (used) | expired | revoked.
Minting a member token never touches HA auth (owner pairing tokens are untouched).
"""

from __future__ import annotations

from . import fileio

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .const import (
    MEMBER_INACTIVE_KEEP_DAYS,
    MEMBER_NAME_MAX,
    MEMBER_TOKEN_PREFIX,
    MEMBER_TOKEN_TTL_DAYS_DEFAULT,
    MEMBER_TOKEN_TTL_DAYS_MAX,
    MEMBERS_FILE,
)

_LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()
_DAY = 86400.0
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
ROLE_MEMBER = "member"
CLIENT_PREFIX = "m_"


class MembersStoreUnreadable(Exception):
    """members.json exists but is not a JSON object. Do not overwrite it."""


class MemberInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def members_path(hass) -> Path:
    try:
        return Path(hass.config.path(MEMBERS_FILE))
    except Exception:
        return Path("/config") / MEMBERS_FILE


def token_hash(token: str) -> str:
    return hashlib.sha256(b"patrimony-member-token:" + token.encode()).hexdigest()


def client_id_for(member_id: str) -> str:
    return CLIENT_PREFIX + member_id


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(path: Path) -> dict[str, Any]:
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
    raise MembersStoreUnreadable(str(path))


def _write(path: Path, data: dict[str, Any]) -> None:
    # Atomic (tmp + fsync + os.replace), 0600, serialized, cache write-through.
    fileio.write_text(path, json.dumps(data, indent=2) + "\n")


def _rows(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = doc.get("members")
    if not isinstance(rows, dict):
        rows = {}
    return {k: v for k, v in rows.items() if isinstance(v, dict)}


def row_status(row: dict[str, Any], now: float | None = None) -> str:
    now = time.time() if now is None else now
    if row.get("revokedAt"):
        return STATUS_REVOKED
    if float(row.get("exp") or 0) <= now:
        return STATUS_EXPIRED
    if row.get("firstUsedAt"):
        return STATUS_ACTIVE
    return STATUS_PENDING


def _prune(rows: dict[str, dict[str, Any]], now: float) -> None:
    keep = MEMBER_INACTIVE_KEEP_DAYS * _DAY
    for k in list(rows):
        r = rows[k]
        st = row_status(r, now)
        ended = float(r.get("revokedAt") or 0) if st == STATUS_REVOKED else float(r.get("exp") or 0)
        if st in (STATUS_REVOKED, STATUS_EXPIRED) and ended + keep < now:
            rows.pop(k, None)


def _update(hass, mutate):
    path = members_path(hass)
    with _LOCK:
        doc = _read(path)  # raises on unreadable: never overwrite
        rows = _rows(doc)
        result = mutate(rows)
        doc["version"] = 1
        doc["members"] = rows
        _write(path, doc)
        return result


def load_rows(hass) -> dict[str, dict[str, Any]]:
    with _LOCK:
        return _rows(_read(members_path(hass)))


def _clean_name(raw: Any) -> str:
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise MemberInputError("invalid_member_name", "memberName is required")
    if len(text) > MEMBER_NAME_MAX:
        raise MemberInputError("invalid_member_name", f"memberName max {MEMBER_NAME_MAX}")
    low = text.lower()
    if "hek_" in low or "pmt_" in low or "eyj" in low:
        raise MemberInputError("invalid_member_name", "memberName rejected")
    return text


def _clean_principal(raw: Any) -> str:
    text = "" if raw is None else str(raw).strip()
    if not _ID_RE.match(text):
        raise MemberInputError("invalid_principal_id", "principalId is required: 1-128 of [A-Za-z0-9._:-]")
    return text


def _clean_invite(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    text = str(raw).strip()
    if not _ID_RE.match(text):
        raise MemberInputError("invalid_invite_id", "inviteId must be 1-128 of [A-Za-z0-9._:-]")
    return text


def _clean_ttl(raw: Any) -> float:
    if raw is None or raw == "":
        return float(MEMBER_TOKEN_TTL_DAYS_DEFAULT)
    if isinstance(raw, bool):
        raise MemberInputError("invalid_ttl", "ttlDays must be a number")
    try:
        days = float(raw)
    except (TypeError, ValueError):
        raise MemberInputError("invalid_ttl", "ttlDays must be a number") from None
    if not (0 < days <= MEMBER_TOKEN_TTL_DAYS_MAX):
        raise MemberInputError("invalid_ttl", f"ttlDays must be > 0 and <= {MEMBER_TOKEN_TTL_DAYS_MAX}")
    return days


def mint(hass, member_name: Any, invite_id: Any = None, ttl_days: Any = None,
         principal_id: Any = None, *, now: float | None = None) -> tuple[dict[str, Any], str]:
    """Internal mint. Returns (public row, raw token). Raw token is never stored."""
    principal = _clean_principal(principal_id)
    name = _clean_name(member_name)
    invite = _clean_invite(invite_id)
    days = _clean_ttl(ttl_days)
    now = time.time() if now is None else now
    token = MEMBER_TOKEN_PREFIX + secrets.token_urlsafe(32)
    member_id = secrets.token_hex(8)

    def mutate(rows):
        _prune(rows, now)
        rows[member_id] = {
            "memberId": member_id,
            "tokenHash": token_hash(token),
            "principalId": principal,
            "memberName": name,
            "inviteId": invite,
            "role": ROLE_MEMBER,
            "createdAt": now,
            "exp": now + days * _DAY,
            "firstUsedAt": None,
            "lastUsedAt": None,
            "revokedAt": None,
            "cancelledAt": None,
        }
        return dict(rows[member_id])

    row = _update(hass, mutate)
    return public_row(row, now), token


def public_row(row: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    mid = str(row.get("memberId") or "")
    return {
        "memberId": mid,
        "clientId": client_id_for(mid),
        "principalId": row.get("principalId"),
        "memberName": row.get("memberName") or "",
        "inviteId": row.get("inviteId"),
        "role": ROLE_MEMBER,
        "status": row_status(row, now),
        "createdAt": _iso(row.get("createdAt")),
        "expiresAt": _iso(row.get("exp")),
        "lastUsedAt": _iso(row.get("lastUsedAt")),
        "revokedAt": _iso(row.get("revokedAt")),
        "cancelled": bool(row.get("cancelledAt")),
    }


def bearer_token(request) -> str:
    headers = getattr(request, "headers", None) or {}
    try:
        header = headers.get("Authorization") or headers.get("authorization") or ""
    except Exception:
        header = ""
    text = str(header).strip()
    if text[:7].lower() == "bearer ":
        return text[7:].strip()
    return ""


def authenticate(hass, token: str, *, now: float | None = None, touch: bool = True
                 ) -> tuple[dict[str, Any] | None, str | None]:
    """(row, error_code). error_code in unauthorized|member_token_expired|member_token_revoked."""
    if not token or not token.startswith(MEMBER_TOKEN_PREFIX) or len(token) > 200:
        return None, "unauthorized"
    want = token_hash(token)
    now = time.time() if now is None else now
    try:
        rows = load_rows(hass)
    except MembersStoreUnreadable:
        _LOGGER.error("patrimony_members_unreadable_not_overwritten")
        return None, "unauthorized"
    found = None
    for r in rows.values():
        if hmac.compare_digest(str(r.get("tokenHash") or ""), want):
            found = r
    if found is None:
        return None, "unauthorized"
    st = row_status(found, now)
    if st == STATUS_REVOKED:
        return found, "member_token_revoked"
    if st == STATUS_EXPIRED:
        return found, "member_token_expired"
    if touch:
        mid = found["memberId"]

        def mutate(rows):
            r = rows.get(mid)
            if r is None:
                return None
            r["lastUsedAt"] = now
            if not r.get("firstUsedAt"):
                r["firstUsedAt"] = now
            return dict(r)

        try:
            found = _update(hass, mutate) or found
        except MembersStoreUnreadable:
            pass
    return found, None


def _drop_pq_sessions(hass, member_ids) -> None:
    """0.4.98: revoking a member deletes its PQ session (blocking; callers run off-loop)."""
    try:
        from .pq_shield.session import get_manager

        get_manager(hass).drop_member_sessions(member_ids)
    except Exception:
        _LOGGER.warning("pq_shield_member_session_drop_failed")


def _drop_clients(hass, member_ids: set[str], principal_id: str | None = None) -> None:
    _drop_pq_sessions(hass, member_ids)
    try:
        from .clients import _drop_client_row, load_clients

        cids = {client_id_for(i) for i in member_ids}
        for c in load_clients(hass):
            if c["clientId"] in cids or (principal_id and c.get("principalId") == principal_id):
                _drop_client_row(hass, c["clientId"])
    except Exception:
        _LOGGER.warning("patrimony_member_client_row_drop_failed")


def revoke_member(hass, member_id: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Revoke ONE member token (panel/owner unpair of a single member client)."""
    mid = str(member_id or "").strip()
    if mid.startswith(CLIENT_PREFIX):
        mid = mid[len(CLIENT_PREFIX):]
    now = time.time() if now is None else now

    def mutate(rows):
        r = rows.get(mid)
        if r is None:
            return None
        if not r.get("revokedAt"):
            r["revokedAt"] = now
        return dict(r)

    with _LOCK:
        if mid not in load_rows(hass):
            return None
        row = _update(hass, mutate)
    _drop_clients(hass, {mid})
    return public_row(row, now) if row else None


def revoke_principal(hass, principal_id: Any, *, now: float | None = None) -> dict[str, Any]:
    """Revoke every member token/client for a backend principal; cancel pending mints.

    Idempotent: unknown or already revoked -> zero counts. Pending (never used)
    rows count as cancelledPending; active/expired rows count as revoked.
    """
    pid = str(principal_id or "").strip()
    now = time.time() if now is None else now

    def mutate(rows):
        revoked, cancelled, ids = 0, 0, []
        for r in rows.values():
            if str(r.get("principalId") or "") != pid or r.get("revokedAt"):
                continue
            if row_status(r, now) == STATUS_PENDING:
                cancelled += 1
                r["cancelledAt"] = now
            else:
                revoked += 1
            r["revokedAt"] = now
            ids.append(r["memberId"])
        return revoked, cancelled, ids

    revoked, cancelled, ids = 0, 0, []
    if pid:
        with _LOCK:
            if any(str(r.get("principalId") or "") == pid for r in load_rows(hass).values()):
                revoked, cancelled, ids = _update(hass, mutate)
        _drop_clients(hass, set(ids), pid)
    return {"principalId": pid, "revoked": revoked, "cancelledPending": cancelled, "memberIds": ids}


def is_member_client_id(client_id: str | None) -> bool:
    return bool(client_id) and str(client_id).startswith(CLIENT_PREFIX)


def list_document(hass, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    try:
        rows = load_rows(hass)
    except MembersStoreUnreadable:
        rows = {}
    out: dict[str, list] = {"members": [], "pending": [], "inactive": []}
    for r in rows.values():
        pub = public_row(r, now)
        if pub["status"] == STATUS_ACTIVE:
            out["members"].append(pub)
        elif pub["status"] == STATUS_PENDING:
            out["pending"].append(pub)
        else:
            out["inactive"].append(pub)
    for k in out:
        out[k].sort(key=lambda p: p.get("createdAt") or "", reverse=True)
    return out
