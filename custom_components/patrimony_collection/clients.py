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

from . import fileio

import json
import re
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


MEMBER_CLIENT_KEY = "patrimony_member_client_id"


def _member_marker(request) -> str | None:
    getter = getattr(request, "get", None) if request is not None else None
    if callable(getter):
        try:
            value = getter(MEMBER_CLIENT_KEY)
        except Exception:
            value = None
        if value:
            return str(value)
    return None


def request_client_id(request) -> str | None:
    """Stable id for THIS credential: JWT iss (HA refresh-token id).

    Member views (0.4.96) authenticate a pmt_ token themselves and set
    request[MEMBER_CLIENT_KEY]; that wins (a pmt_ token has no JWT iss anyway).
    """
    return _member_marker(request) or _jwt_iss(_bearer(request))


def _request_user(request):
    user = None
    getter = getattr(request, "get", None) if request is not None else None
    if callable(getter):
        try:
            user = getter("hass_user")
        except Exception:
            user = None
    if user is None and request is not None:
        user = getattr(request, "hass_user", None)
    return user


def owner_or_admin(hass, request) -> bool:
    """Owner APIs (member mint/list/revoke): an HA admin, or the owner's pair bearer.

    A member pmt_ token is never accepted (HA auth rejects it on requires_auth views;
    a member-marked request is refused here as well).
    """
    if _member_marker(request):
        return False
    user = _request_user(request)
    if user is None or getattr(user, "is_admin", False):
        return True
    client_id = _jwt_iss(_bearer(request))
    row = get_client(hass, client_id) if client_id else None
    return bool(row and row["status"] == STATUS_ACTIVE and row.get("role") == ROLE_OWNER)


_HA_USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def revoke_actor(hass, request) -> dict[str, Any]:
    """0.4.112: {revokedBy, revokeSource} for a revoke request. Ids only (no IP/UA/names).

    owner_app: bearer JWT iss is an active role=owner paired client -> that clientId.
    panel: an HA admin user that is not a paired owner client -> "admin:<user id>".
    other: anything else -> null.
    """
    if _member_marker(request):
        return {"revokedBy": None, "revokeSource": "other"}
    client_id = _jwt_iss(_bearer(request))
    row = get_client(hass, client_id) if client_id else None
    if row and row["status"] == STATUS_ACTIVE and row.get("role") == ROLE_OWNER:
        return {"revokedBy": client_id, "revokeSource": "owner_app"}
    user = _request_user(request)
    if user is not None and getattr(user, "is_admin", False):
        uid = getattr(user, "id", None)
        uid = str(uid).strip() if uid is not None else ""
        by = "admin:" + uid if uid and _HA_USER_ID_RE.match(uid) else None
        return {"revokedBy": by, "revokeSource": "panel"}
    return {"revokedBy": None, "revokeSource": "other"}


# ---------- 0.4.113 member-mutation credential gate (D1/D2/D4) ----------
CRED_ADMIN_SESSION = "admin_session"
CRED_OWNER_PAIR = "owner_pair"
CRED_LLAT_DEV = "llat_dev"
CRED_OTHER = "other"
# Variant switch. True (shipped): the owner pair bearer may unpair member (m_) clients
# only, even when its HA user is an admin. False: as 0.4.112, an admin-owned owner pair
# bearer may also unpair owner (non-m_) clients.
OWNER_PAIR_MEMBER_ONLY_UNPAIR = True
# Opt-in, default False everywhere: re-allows admin-owned LLATs on member mutations (dev/testing).
OPT_ALLOW_LLAT_MEMBER_MUTATIONS = "allow_llat_member_mutations"
REFRESH_TOKEN_ID_KEY = "hass_refresh_token_id"
_TOKEN_TYPE_NORMAL = "normal"
_TOKEN_TYPE_LLAT = "long_lived_access_token"
_RT_ID_RE = re.compile(r"^[A-Za-z0-9]{6,128}$")
_PAIR_ID_RE = re.compile(r"^[A-Za-z0-9-]{6,128}$")  # HA ids are hex; never m_<memberId>


def _jwt_payload(token: str) -> dict[str, Any]:
    import base64

    parts = token.split(".") if token else []
    if len(parts) < 2 or not parts[1]:
        return {}
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _request_refresh_token_id(request) -> str | None:
    getter = getattr(request, "get", None) if request is not None else None
    value = None
    if callable(getter):
        try:
            value = getter(REFRESH_TOKEN_ID_KEY)
        except Exception:
            value = None
    text = str(value).strip() if value else ""
    return text or None


def _refresh_token_type(hass, token_id: str | None) -> str | None:
    """token_type of the validated refresh token (metadata only), or None when unknown."""
    if not token_id:
        return None
    auth = getattr(hass, "auth", None)
    getter = getattr(auth, "async_get_refresh_token", None) if auth is not None else None
    if getter is None:
        return None
    try:
        tok = getter(token_id)
    except Exception:
        return None
    if tok is None or hasattr(tok, "__await__"):
        if hasattr(tok, "close"):
            tok.close()
        return None
    value = getattr(tok, "token_type", None)
    return str(value) if value else None


def _allow_llat_option(hass) -> bool:
    entry = _first_entry(hass)
    options = getattr(entry, "options", None) or {}
    try:
        return options.get(OPT_ALLOW_LLAT_MEMBER_MUTATIONS) is True
    except Exception:
        return False


def mutation_actor(hass, request, *, now: float | None = None) -> dict[str, Any]:
    """0.4.113: who may change members/clients (revoke, unpair, remove-inactive, clear-ended, mint).

    The credential id is the HA refresh-token id HA validated for this request
    (request["hass_refresh_token_id"]); without it (older callers/tests) the bearer
    JWT iss, which HA has just validated. If both are present and differ: not bound.
      member pmt_ request / no HA user           -> refused (fail closed)
      id is an ACTIVE role=owner client row      -> owner_pair. Keys ONLY on that row,
                                                    never on who created the token or how
                                                    (pair flow or a hand-made LLAT).
      normal (browser/Companion) token, admin    -> admin_session; refused with
                                                    token_expired when the bearer JWT exp
                                                    has passed (no leeway)
      long-lived token, admin, option on         -> llat_dev
      any other long-lived token                 -> 403 llat_not_allowed
      anything else                              -> 403 forbidden
    Returns {allowed, credentialType, credentialIdPrefix, status, code, ownerUserAdmin}.
    Ids only: never token values, IPs or user agents.
    """
    import time as _time

    def out(allowed, cred, status=200, code=None, prefix=None, owner_admin=False):
        return {"allowed": allowed, "credentialType": cred, "credentialIdPrefix": prefix,
                "status": status, "code": code, "ownerUserAdmin": owner_admin}

    if _member_marker(request):
        return out(False, CRED_OTHER, 403, "forbidden")
    user = _request_user(request)
    if user is None:
        return out(False, CRED_OTHER, 403, "forbidden")
    bearer = _bearer(request)
    payload = _jwt_payload(bearer)
    iss = payload.get("iss") if isinstance(payload.get("iss"), str) else None
    rt_id = _request_refresh_token_id(request)
    if rt_id and iss and rt_id != iss:
        cred_id = None
    else:
        cred_id = rt_id or (iss.strip() if iss else None)
    prefix = cred_id[:6] if cred_id and _RT_ID_RE.match(cred_id) else None
    is_admin = bool(getattr(user, "is_admin", False))
    if cred_id and not cred_id.startswith("m_"):
        row = get_client(hass, cred_id)
        if row and row["status"] == STATUS_ACTIVE and row.get("role") == ROLE_OWNER:
            return out(True, CRED_OWNER_PAIR, prefix=prefix, owner_admin=is_admin)
    token_type = _refresh_token_type(hass, rt_id) if cred_id else None
    if token_type == _TOKEN_TYPE_NORMAL and is_admin:
        exp = payload.get("exp")
        now = _time.time() if now is None else now
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or now >= float(exp):
            return out(False, CRED_ADMIN_SESSION, 401, "token_expired", prefix)
        return out(True, CRED_ADMIN_SESSION, prefix=prefix)
    if token_type == _TOKEN_TYPE_LLAT:
        if is_admin and _allow_llat_option(hass):
            return out(True, CRED_LLAT_DEV, prefix=prefix)
        return out(False, CRED_OTHER, 403, "llat_not_allowed", prefix)
    return out(False, CRED_OTHER, 403, "forbidden", prefix)


def may_unpair(actor: dict[str, Any], target_is_member: bool) -> bool:
    """Unpair scope after mutation_actor allowed the caller."""
    if not actor.get("allowed"):
        return False
    if actor.get("credentialType") != CRED_OWNER_PAIR or target_is_member:
        return True
    return (not OWNER_PAIR_MEMBER_ONLY_UNPAIR) and bool(actor.get("ownerUserAdmin"))


def is_admin_request(request) -> bool:
    if _member_marker(request):
        return False
    user = _request_user(request)
    return user is None or bool(getattr(user, "is_admin", False))


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
    fileio.write_text(ios_session_path(hass), json.dumps(data, indent=2) + "\n")


# 0.4.114: server-side record of refresh-token ids that pair.mint_pairing issued. Only these
# (and pmt_ members) may get a client row created; a hand-made LLAT named "Patrimony iOS" is
# NOT in it. Ids only (never token values), newest last, capped.
PAIR_ISSUED_FILE = "patrimony_collection/pair_issued.json"
PAIR_ISSUED_MAX = 50


def _pair_issued_path(hass):
    from pathlib import Path

    try:
        return Path(hass.config.path(PAIR_ISSUED_FILE))
    except Exception:
        return Path("/config") / PAIR_ISSUED_FILE


def _read_pair_issued(hass) -> list[dict[str, Any]]:
    try:
        text = fileio.read_text(_pair_issued_path(hass))  # loop-safe cached read (0.4.115)
        raw = json.loads(text) if text else {}
    except Exception:
        return []
    rows = raw.get("issued") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and _PAIR_ID_RE.match(str(r.get("id") or ""))]


def _write_pair_issued(hass, rows: list[dict[str, Any]]) -> None:
    path = _pair_issued_path(hass)
    fileio.write_text(path, json.dumps({"issued": rows[-PAIR_ISSUED_MAX:]}, indent=2) + "\n")


def note_pair_issued(hass, refresh_token_id: str, now: datetime | None = None) -> None:
    rid = str(refresh_token_id or "").strip()
    if not _PAIR_ID_RE.match(rid):
        return
    rows = [r for r in _read_pair_issued(hass) if r.get("id") != rid]
    rows.append({"id": rid, "issuedAt": _utc_z(now)})
    _write_pair_issued(hass, rows)


def is_pair_issued(hass, refresh_token_id: str | None) -> bool:
    rid = str(refresh_token_id or "").strip()
    if not rid or rid.startswith("m_"):
        return False
    return any(r.get("id") == rid for r in _read_pair_issued(hass))


def forget_pair_issued(hass, refresh_token_id: str) -> None:
    rid = str(refresh_token_id or "").strip()
    rows = _read_pair_issued(hass)
    kept = [r for r in rows if r.get("id") != rid]
    if len(kept) != len(rows):
        _write_pair_issued(hass, kept)


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
        **_role_fields(row, client_id),
    }


ROLE_OWNER = "owner"
ROLE_MEMBER = "member"


def _role_fields(row: dict[str, Any], client_id: str) -> dict[str, Any]:
    """0.4.96: role owner|member plus memberName, inviteId, principalId. Legacy rows are owner.

    Member clients (id ``m_<memberId>``) are always role member, whatever the row says.
    """
    is_member = client_id.startswith("m_")
    name = _safe_display(row.get("memberName")) if is_member else None
    invite = str(row.get("inviteId") or "").strip()[:128] if is_member else ""
    principal = str(row.get("principalId") or "").strip()[:128] if is_member else ""
    return {
        "role": ROLE_MEMBER if is_member else ROLE_OWNER,
        "memberName": name or None,
        "inviteId": invite or None,
        "principalId": principal or None,
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
    member_name: str | None = None,
    invite_id: str | None = None,
    principal_id: str | None = None,
    create: bool = True,
) -> dict[str, Any] | None:
    """Create or update a paired credential row. Does not overwrite an edited name.

    0.4.114: ``create=False`` only updates an existing row and returns None when there is
    none. Owner rows are created only by the pair flow (pair.mint_pairing) and member
    rows only by pmt_ authentication; the ios_session heartbeat and chat never create.
    """
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
    if found is None and not create:
        return None
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
            "memberName": member_name,
            "inviteId": invite_id,
            "principalId": principal_id,
        }
        if member_name and not device_name:
            found["displayName"] = _safe_display(member_name) or seed
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
    try:
        forget_pair_issued(hass, client_id)  # 0.4.114: an unpaired id never re-registers
    except Exception:
        _LOGGER.warning("patrimony_pair_issued_forget_failed")


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
    from .notify import event_key_status_fields

    key_fields = event_key_status_fields(hass)
    if row is not None and row["status"] == STATUS_UNPAIRED:
        return {
            "status": STATUS_UNPAIRED,
            "propertyId": pid,
            "clientId": client_id,
            "reason": "unpaired",
            **key_fields,
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
        **key_fields,
    }


def clients_list_allowed(hass, request) -> bool:
    """Phones list for an admin, or for this bearer when it is an active paired client.

    Same gate shape as the panel: a missing hass_user is allowed. A present
    non-admin is allowed only when the JWT iss is an active paired client.
    Rename and unpair stay admin-only.
    """
    user = None
    if request is not None:
        getter = getattr(request, "get", None)
        if callable(getter):
            try:
                user = getter("hass_user")
            except Exception:
                user = None
        if user is None:
            user = getattr(request, "hass_user", None)
    if user is None or getattr(user, "is_admin", True):
        return True
    client_id = request_client_id(request)
    row = get_client(hass, client_id) if client_id else None
    return row is not None and row.get("status") == STATUS_ACTIVE


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
                "role": row.get("role") or ROLE_OWNER,
                "memberName": row.get("memberName"),
                "inviteId": row.get("inviteId"),
                "principalId": row.get("principalId"),
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
        if posted and not is_reserved_display_name(posted) and is_pair_issued(hass, client_id):
            # First chat contact registers ONLY a pair-issued credential (0.4.114).
            row = upsert_client(
                hass,
                client_id,
                device_name=posted,
                ha_username=ha_name,
                touch_access=True,
            )
            if row is not None:
                return {
                    "kind": "ios",
                    "label": row["displayName"],
                    "key": "ios:" + client_id,
                }
        if posted and not is_reserved_display_name(posted):
            # Not pair-issued (hand-made LLAT): same label/key as before, but NO row.
            label = seed_display_name(posted, ha_username=ha_name)
            return {"kind": "ios", "label": label, "key": "ios:" + client_id}
    return None


def active_display_names(hass, exclude_client_id: str | None = None) -> list[str]:
    """Labels for push-log local clients. Active rows only, minus an excluded sender."""
    names: list[str] = []
    for row in load_clients(hass):
        if row["status"] != STATUS_ACTIVE:
            continue
        if exclude_client_id and row.get("clientId") == exclude_client_id:
            continue
        if _is_member_row(row):
            continue  # 0.4.118: a guest's name never becomes an owner/push label
        name = _safe_display(row.get("displayName"))
        if name and name not in names:
            names.append(name)
    return names


def _is_member_row(row: dict[str, Any]) -> bool:
    """0.4.118: m_<memberId> client rows (and any row marked role=member) are guests."""
    return str(row.get("clientId") or "").startswith("m_") or row.get("role") == "member"


def owner_read_label(hass, client_id: str | None, clients: list[dict[str, Any]] | None = None) -> str | None:
    """0.4.118 read-time owner label: an ios:<clientId> sender whose row is active and
    nameEdited shows its CURRENT displayName (old history follows a rename). Else None."""
    ident = str(client_id or "").strip()
    if not ident or ident.startswith("m_"):
        return None
    for row in (clients if clients is not None else load_clients(hass)):
        if row.get("clientId") != ident:
            continue
        if _is_member_row(row) or row.get("status") != STATUS_ACTIVE or not row.get("nameEdited"):
            return None
        return _safe_display(row.get("displayName"))
    return None
