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
Removing a past row leaves a tombstone (tokenHash + revokedAt) so authenticate still
returns member_token_revoked; the list hides tombstones. Auto-prune after 30 days.
Minting a member token never touches HA auth (owner pairing tokens are untouched).

0.4.112 revoke audit: every revoke stores, on the row and on its tombstone, next to
revokedAt: revokedBy (actor id or null), revokeSource and revokedVia (route name).
revokeSource is decided from the authenticated caller (clients.revoke_actor):
  owner_app        bearer JWT iss is an active role=owner paired client (the owner
                   iOS app); revokedBy = that owner clientId (owner clients carry no
                   principal). Wins even when the paired HA user is an admin.
  panel            an HA admin session that is not a paired owner client (the house
                   panel); revokedBy = "admin:<HA user id>".
  principal_revoke revoke_principal called with no route caller (internal,
                   principal-wide revoke); revokedBy = null.
  other            anything else (no HA user, unknown caller, internal
                   revoke_member); revokedBy = null.
revokedVia is the HA view name without the "api:patrimony_collection:" prefix
(client_unpair, member_revoke) or "internal"; a valid X-Patrimony-Revoke-Reason
header (user_remove | courier_rotation | courier_put_failed |
pending_revocation:<1-64 of [A-Za-z0-9_-]>) overrides the route name. Invalid or
oversized header values are ignored (route name stored, raw value never logged). An already-revoked row keeps its
first audit. One INFO line per newly revoked row with id prefixes only; never IPs,
user agents, names or tokens. Rows without these fields load as null.

0.4.113 adds, from the mutation credential gate (clients.mutation_actor):
  credentialType      admin_session | owner_pair | llat_dev | other
  credentialIdPrefix  first 6 chars of the HA refresh-token id that made the call
                      (an id, never a token value); null when unknown.
Both are on the row, the tombstone, public_row and the INFO line. Old rows load as null.
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
    GUEST_DATED_LABEL,
    GUEST_FALLBACK_LABEL,
    GUEST_LABEL_MONTHS,
    STOCK_MEMBER_NAMES,
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


REVOKE_SOURCES = ("owner_app", "panel", "principal_revoke", "other")
_VIA_RE = re.compile(r"^[a-z0-9_]{1,64}$")
# 0.4.112: optional client hint X-Patrimony-Revoke-Reason; when valid it is stored as revokedVia.
REVOKE_REASON_HEADER = "X-Patrimony-Revoke-Reason"
_REASON_RE = re.compile(
    r"^(?:user_remove|courier_rotation|courier_put_failed|pending_revocation:[A-Za-z0-9_-]{1,64})$")
_REASON_MAX = len("pending_revocation:") + 64


def clean_revoke_reason(raw: Any) -> str | None:
    """The validated reason, or None (absent, invalid, oversized). Never echo the raw value."""
    if not isinstance(raw, str) or not raw or len(raw) > _REASON_MAX:
        return None
    return raw if _REASON_RE.match(raw) else None
_BY_RE = re.compile(r"^(admin:)?[A-Za-z0-9._-]{1,128}$")
AUDIT_FIELDS = ("revokedBy", "revokeSource", "revokedVia", "credentialType", "credentialIdPrefix")
CREDENTIAL_TYPES = ("admin_session", "owner_pair", "llat_dev", "other")
_CRED_PREFIX_RE = re.compile(r"^[A-Za-z0-9]{6}$")


def _clean_audit(audit: dict[str, Any] | None, default_source: str) -> dict[str, Any]:
    """Normalize an actor/source/via triple. Unknown values degrade to null / other."""
    audit = audit if isinstance(audit, dict) else {}
    by = audit.get("revokedBy")
    by = str(by).strip() if isinstance(by, str) else None
    if not by or not _BY_RE.match(by):
        by = None
    source = audit.get("revokeSource") or default_source
    if source not in REVOKE_SOURCES:
        source = "other"
    via = audit.get("revokedVia")
    via = via.strip() if isinstance(via, str) else ""
    if not (_VIA_RE.match(via) or clean_revoke_reason(via)):
        via = "internal"
    cred = audit.get("credentialType")
    if cred not in CREDENTIAL_TYPES:
        cred = "other"
    prefix = audit.get("credentialIdPrefix")
    if not (isinstance(prefix, str) and _CRED_PREFIX_RE.match(prefix)):
        prefix = None
    return {"revokedBy": by, "revokeSource": source, "revokedVia": via,
            "credentialType": cred, "credentialIdPrefix": prefix}


def _id_prefix(value: Any, n: int) -> str:
    text = "" if value is None else str(value)
    if not text:
        return "-"
    if text.startswith("admin:"):
        return "admin:" + text[6:6 + n]
    return text[:n]


def _log_revoke(member_id: str, audit: dict[str, Any]) -> None:
    _LOGGER.info(
        "patrimony_member_revoked member=%s by=%s source=%s via=%s reason=%s cred=%s rt=%s",
        _id_prefix(member_id, 8),
        _id_prefix(audit.get("revokedBy"), 6),
        audit.get("revokeSource"),
        audit.get("revokedVia"),
        clean_revoke_reason(audit.get("revokedVia")) or "-",
        audit.get("credentialType") or "other",
        _id_prefix(audit.get("credentialIdPrefix"), 6),
    )


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


def _is_tombstone(row: dict[str, Any]) -> bool:
    return bool(row.get("tombstone"))


def _to_tombstone(row: dict[str, Any], now: float) -> dict[str, Any]:
    """Keep hash + revoke/expiry so authenticate still denies; drop list fields."""
    return {
        "memberId": row.get("memberId"),
        "tokenHash": row.get("tokenHash"),
        "principalId": row.get("principalId"),
        "exp": row.get("exp"),
        "revokedAt": row.get("revokedAt") or now,
        # 0.4.112: who/how survives Remove / Clear ended.
        "revokedBy": row.get("revokedBy"),
        "revokeSource": row.get("revokeSource"),
        "revokedVia": row.get("revokedVia"),
        "credentialType": row.get("credentialType"),
        "credentialIdPrefix": row.get("credentialIdPrefix"),
        "firstUsedAt": row.get("firstUsedAt"),
        "tombstone": True,
        "tombstonedAt": now,
    }


def _prune(rows: dict[str, dict[str, Any]], now: float) -> None:
    keep = MEMBER_INACTIVE_KEEP_DAYS * _DAY
    for k in list(rows):
        r = rows[k]
        if _is_tombstone(r):
            ended = float(r.get("tombstonedAt") or r.get("revokedAt") or r.get("exp") or 0)
            if ended and ended + keep < now:
                rows.pop(k, None)
            continue
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
        "revokedBy": row.get("revokedBy"),
        "revokeSource": row.get("revokeSource"),
        "revokedVia": row.get("revokedVia"),
        "credentialType": row.get("credentialType"),
        "credentialIdPrefix": row.get("credentialIdPrefix"),
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


def revoke_member(hass, member_id: str, *, now: float | None = None,
                  audit: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Revoke ONE member token (panel/owner unpair of a single member client).

    ``audit``: {revokedBy, revokeSource, revokedVia} from the route (0.4.112).
    """
    mid = str(member_id or "").strip()
    if mid.startswith(CLIENT_PREFIX):
        mid = mid[len(CLIENT_PREFIX):]
    now = time.time() if now is None else now
    info = _clean_audit(audit, "other")
    fresh: list[str] = []

    def mutate(rows):
        r = rows.get(mid)
        if r is None:
            return None
        if not r.get("revokedAt"):
            r["revokedAt"] = now
            r.update(info)
            fresh.append(mid)
        return dict(r)

    with _LOCK:
        if mid not in load_rows(hass):
            return None
        row = _update(hass, mutate)
    for i in fresh:
        _log_revoke(i, info)
    _drop_clients(hass, {mid})
    return public_row(row, now) if row else None


def revoke_principal(hass, principal_id: Any, *, now: float | None = None,
                     audit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Revoke every member token/client for a backend principal; cancel pending mints.

    Idempotent: unknown or already revoked -> zero counts. Pending (never used)
    rows count as cancelledPending; active/expired rows count as revoked.
    ``audit`` as in revoke_member; without one the source is principal_revoke.
    """
    pid = str(principal_id or "").strip()
    now = time.time() if now is None else now
    info = _clean_audit(audit, "principal_revoke")

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
            r.update(info)
            ids.append(r["memberId"])
        return revoked, cancelled, ids

    revoked, cancelled, ids = 0, 0, []
    if pid:
        with _LOCK:
            if any(str(r.get("principalId") or "") == pid for r in load_rows(hass).values()):
                revoked, cancelled, ids = _update(hass, mutate)
        for i in ids:
            _log_revoke(i, info)
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
    zone = _house_zone(hass)
    for r in rows.values():
        if _is_tombstone(r):
            continue
        pub = public_row(r, now)
        # 0.4.118: per-principal dates (optional, additive), ISO 8601 with house offset.
        pid = r.get("principalId")
        invited, used = principal_times(rows, pid) if pid else (r.get("createdAt"), r.get("firstUsedAt"))
        pub["invitedAt"] = iso_in_house_tz(hass, invited, zone)
        pub["connectedAt"] = iso_in_house_tz(hass, used, zone)
        if pub["status"] == STATUS_ACTIVE:
            out["members"].append(pub)
        elif pub["status"] == STATUS_PENDING:
            out["pending"].append(pub)
        else:
            out["inactive"].append(pub)
    for k in out:
        out[k].sort(key=lambda p: p.get("createdAt") or "", reverse=True)
    return out


def remove_inactive(hass, member_id: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Hide one ended (revoked/expired) row from the list. Keeps a tombstone so the token stays denied.

    Active and pending are refused. Idempotent on already-tombstoned or missing ids.
    """
    mid = str(member_id or "").strip()
    if mid.startswith(CLIENT_PREFIX):
        mid = mid[len(CLIENT_PREFIX):]
    now = time.time() if now is None else now

    def mutate(rows):
        _prune(rows, now)
        r = rows.get(mid)
        if r is None:
            return None
        if _is_tombstone(r):
            return {"memberId": mid, "removed": False, "alreadyGone": True}
        st = row_status(r, now)
        if st not in (STATUS_REVOKED, STATUS_EXPIRED):
            raise MemberInputError("not_inactive", "Only ended access can be removed from the list")
        rows[mid] = _to_tombstone(r, now)
        return {"memberId": mid, "removed": True, "alreadyGone": False}

    return _update(hass, mutate)


def clear_inactive(hass, *, now: float | None = None) -> dict[str, Any]:
    """Tombstone every revoked/expired row. Active and pending stay. Access stays denied."""
    now = time.time() if now is None else now

    def mutate(rows):
        _prune(rows, now)
        n = 0
        for mid, r in list(rows.items()):
            if _is_tombstone(r):
                continue
            if row_status(r, now) in (STATUS_REVOKED, STATUS_EXPIRED):
                rows[mid] = _to_tombstone(r, now)
                n += 1
        return {"cleared": n}

    return _update(hass, mutate)


# ---------------------------------------------------------------------------
# 0.4.118: owner rename per principal, read-time guest labels, dates.
# Names are never logged. Audit lines carry id prefixes and counts only.

_CONTROL_EXTRA = {"\u2028", "\u2029", "\u0085"}


class MemberNameError(Exception):
    """400 for PATCH /members/{id}: code + reason (+ maxLength for too_long)."""

    def __init__(self, code: str, reason: str):
        super().__init__(code)
        self.code = code
        self.reason = reason


def _has_control(text: str) -> bool:
    import unicodedata

    for ch in text:
        if ch in _CONTROL_EXTRA or unicodedata.category(ch) in ("Cc", "Cf"):
            return True
    return False


def is_stock_member_name(name: Any) -> bool:
    """Null, empty, a device/reserved name, or a stock fallback: the guest counts as unnamed."""
    from .clients import is_reserved_display_name

    text = "" if name is None else str(name).strip()
    if not text:
        return True
    return is_reserved_display_name(text) or text.casefold() in STOCK_MEMBER_NAMES


def validate_member_name(raw: Any) -> str | None:
    """None (revert) for null/empty; else the cleaned name. Raises MemberNameError."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise MemberNameError("invalid_member_name", "not_a_string")
    if not raw.strip():
        return None
    if _has_control(raw):
        raise MemberNameError("invalid_member_name", "control_characters")
    text = raw.strip()
    if len(text) > MEMBER_NAME_MAX:
        raise MemberNameError("invalid_member_name", "too_long")
    try:
        text = _clean_name(text)
    except MemberInputError:
        raise MemberNameError("invalid_member_name", "not_allowed") from None
    if is_stock_member_name(text):
        raise MemberNameError("reserved_member_name", "reserved")
    return text


def resolve_principal(rows: dict[str, dict[str, Any]], ident: Any) -> str | None:
    """principalId (canonical) or a memberId -> principalId; None when unknown."""
    text = "" if ident is None else str(ident).strip()
    if not text:
        return None
    live = [r for r in rows.values() if not _is_tombstone(r)]
    if any(r.get("principalId") == text for r in live):
        return text
    rec = rows.get(text)
    if isinstance(rec, dict) and not _is_tombstone(rec):
        pid = rec.get("principalId")
        if isinstance(pid, str) and pid.strip():
            return pid.strip()
    return None


def principal_rows(rows: dict[str, dict[str, Any]], principal_id: str,
                   *, tombstones: bool = False) -> list[dict[str, Any]]:
    return [r for r in rows.values()
            if r.get("principalId") == principal_id and (tombstones or not _is_tombstone(r))]


def representative_row(prows: list[dict[str, Any]], now: float | None = None) -> dict[str, Any] | None:
    """The active row (latest firstUsedAt), else the latest by createdAt."""
    if not prows:
        return None
    active = [r for r in prows if row_status(r, now) == STATUS_ACTIVE]
    if active:
        return max(active, key=lambda r: float(r.get("firstUsedAt") or 0))
    return max(prows, key=lambda r: float(r.get("createdAt") or 0))


def principal_times(rows: dict[str, dict[str, Any]], principal_id: str) -> tuple[float | None, float | None]:
    """(first invite createdAt, earliest firstUsedAt) for a principal; tombstones count for firstUsedAt."""
    created = [float(r["createdAt"]) for r in principal_rows(rows, principal_id)
               if r.get("createdAt")]
    used = [float(r["firstUsedAt"]) for r in principal_rows(rows, principal_id, tombstones=True)
            if r.get("firstUsedAt")]
    return (min(created) if created else None), (min(used) if used else None)


def _house_zone(hass):
    from zoneinfo import ZoneInfo

    try:
        from .clients import house_timezone_name

        return ZoneInfo(house_timezone_name(hass))
    except Exception:
        return timezone.utc


def iso_in_house_tz(hass, ts: float | None, zone=None) -> str | None:
    if not ts:
        return None
    zone = zone or _house_zone(hass)
    return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(zone).isoformat(timespec="seconds")


def guest_fallback_label(hass, invited_ts: float | None, *, now: float | None = None, zone=None) -> str:
    """'Guest · 31 Oct' (year only when not the current year, house timezone), else 'Guest'."""
    if not invited_ts:
        return GUEST_FALLBACK_LABEL
    zone = zone or _house_zone(hass)
    day = datetime.fromtimestamp(float(invited_ts), timezone.utc).astimezone(zone)
    today = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).astimezone(zone)
    text = f"{day.day} {GUEST_LABEL_MONTHS[day.month - 1]}"
    if day.year != today.year:
        text += f" {day.year}"
    return GUEST_DATED_LABEL.format(date=text)


def principal_label(hass, rows: dict[str, dict[str, Any]], principal_id: str,
                    *, now: float | None = None, zone=None) -> str | None:
    """Current read-time label for a principal; None when the house has no live row for it."""
    prows = principal_rows(rows, principal_id)
    if not prows:
        return None
    rep = representative_row(prows, now)
    name = rep.get("memberName") if rep else None
    if not is_stock_member_name(name):
        return str(name).strip()
    invited, _used = principal_times(rows, principal_id)
    return guest_fallback_label(hass, invited, now=now, zone=zone)


def rename_principal(hass, ident: Any, raw_name: Any, *, now: float | None = None) -> dict[str, Any] | None:
    """PATCH /members/{id}. Validates first (MemberNameError), then None for unknown ids."""
    name = validate_member_name(raw_name)
    now = time.time() if now is None else now

    def mutate(rows):
        pid = resolve_principal(rows, ident)
        if pid is None:
            return None
        prows = principal_rows(rows, pid)
        for r in prows:
            r["memberName"] = name
        rep = representative_row(prows, now)
        return {"principalId": pid, "memberId": rep.get("memberId") if rep else None,
                "memberName": name, "updatedRows": len(prows)}

    out = _update(hass, mutate)
    if out is not None:
        _LOGGER.info("patrimony_member_renamed principal=%s rows=%d cleared=%s",
                     _id_prefix(out["principalId"], 6), out["updatedRows"], name is None)
    return out
