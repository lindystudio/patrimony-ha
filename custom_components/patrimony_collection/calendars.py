"""0.4.100/0.4.101 read-only house calendars (live from HA calendar.get_events; nothing stored off-house).

Config lives in mapping.json under "calendars": {entity_id: {expose, sharedWithMembers, role}}
(panel only; the owner phone cannot change it in v1). The phone never sees an entity_id: each
calendar has an opaque stable id = sha256(propertyId + entity_id)[:16].

0.4.101: every event carries a stable "uid": HA's uid when present, else
HMAC-SHA256(house key, calendarId|recurrenceId|start|title)[:16] over the original title
(before the member "Guests" rewrite), so owner and member see the same uid. Keyed with a
house-local secret so a member cannot brute-force a guest name from the uid.

Never log event titles, descriptions or locations.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import clients as house_clients
from . import fileio

_LOGGER = logging.getLogger(__name__)

CALENDARS_PATH = "/api/patrimony_collection/calendars"
CALENDAR_EVENTS_PATH = "/api/patrimony_collection/calendars/events"
MEMBER_CALENDARS_PATH = "/api/patrimony_collection/m/calendars"
MEMBER_CALENDAR_EVENTS_PATH = "/api/patrimony_collection/m/calendars/events"
EDITOR_CALENDARS_PATH = "/api/patrimony_collection/editor/calendars"

ROLES = ("stays", "birthdays")
MEMBER_STAYS_TITLE = "Guests"
DEFAULT_DAYS = 14
MAX_DAYS = 92
MAX_EVENTS = 500
CACHE_TTL_S = 60.0
FETCH_TIMEOUT_S = 8.0
_CACHE_KEY = "patrimony_collection_calendar_cache"

try:
    from homeassistant.components.http import HomeAssistantView
except ImportError:  # offline tests
    class HomeAssistantView:  # type: ignore[no-redef]
        url = ""
        name = ""
        requires_auth = True


# ---------------------------------------------------------------- config

def _mapping_path(hass) -> Path:
    try:
        return Path(hass.config.path("patrimony_collection/mapping.json"))
    except Exception:
        return Path("/config/patrimony_collection/mapping.json")


def _read_mapping(hass) -> dict[str, Any]:
    raw = fileio.read_text(_mapping_path(hass))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _norm_row(row: Any) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    role = row.get("role")
    return {
        "expose": bool(row.get("expose", False)),
        "sharedWithMembers": bool(row.get("sharedWithMembers", row.get("memberVisible", False))),
        "role": role if role in ROLES else None,
    }


def load_config(hass) -> dict[str, dict[str, Any]]:
    raw = _read_mapping(hass).get("calendars")
    if not isinstance(raw, dict):
        return {}
    return {str(k): _norm_row(v) for k, v in raw.items() if str(k).startswith("calendar.")}


def save_config(hass, rows: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read-modify-write mapping.json, keeping every other key. Call off the loop."""
    clean = {str(k): _norm_row(v) for k, v in (rows or {}).items() if str(k).startswith("calendar.")}
    data = _read_mapping(hass)
    data["calendars"] = clean
    fileio.write_text(_mapping_path(hass), json.dumps(data, indent=2) + "\n")
    cache_clear(hass)
    return clean


def calendar_id(property_id: str, entity_id: str) -> str:
    return hashlib.sha256(f"{property_id}{entity_id}".encode()).hexdigest()[:16]


def _calendar_states(hass) -> list:
    try:
        states = hass.states.async_all("calendar")
    except TypeError:
        states = hass.states.async_all()
    except Exception:
        return []
    return [s for s in states if str(getattr(s, "entity_id", "")).startswith("calendar.")]


def visible_calendars(hass, *, member: bool) -> list[dict[str, Any]]:
    """[{id, entityId(internal), name, color?, sharedWithMembers, role}] for exposed calendars."""
    cfg = load_config(hass)
    pid = house_clients.property_id(hass)
    out = []
    for st in _calendar_states(hass):
        eid = st.entity_id
        row = cfg.get(eid)
        if not row or not row["expose"]:
            continue
        if member and not row["sharedWithMembers"]:
            continue
        attrs = getattr(st, "attributes", None) or {}
        name = str(attrs.get("friendly_name") or "").strip() or eid.split(".", 1)[1].replace("_", " ").title()
        cal = {"id": calendar_id(pid, eid), "_entity": eid, "name": name,
               "sharedWithMembers": row["sharedWithMembers"], "role": row["role"]}
        color = attrs.get("color")
        if isinstance(color, str) and color.strip():
            cal["color"] = color.strip()
        out.append(cal)
    out.sort(key=lambda c: c["name"].lower())
    return out


def public_calendar(cal: dict[str, Any]) -> dict[str, Any]:
    body = {"id": cal["id"], "calendarId": cal["id"], "name": cal["name"]}
    if "color" in cal:
        body["color"] = cal["color"]
    body["sharedWithMembers"] = cal["sharedWithMembers"]
    body["role"] = cal["role"]
    return body


# ---------------------------------------------------------------- time window

class WindowError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def house_zone(hass) -> tuple[str, Any]:
    name = house_clients.house_timezone_name(hass)
    try:
        return name, ZoneInfo(name)
    except Exception:
        return "UTC", timezone.utc


def _parse_bound(raw: str, zone) -> datetime:
    raw = raw.strip()
    try:
        if len(raw) == 10:
            return datetime.combine(date.fromisoformat(raw), dtime(0), zone)
        # An unencoded "+" offset arrives as a space in the query string.
        dt = datetime.fromisoformat(raw.replace(" ", "+").replace("Z", "+00:00"))
    except ValueError:
        raise WindowError("bad_range") from None
    return dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)


def parse_window(query, zone, now: datetime | None = None) -> tuple[datetime, datetime]:
    """start/end ISO, or days=N from the start of today (house tz). Default days=14."""
    q = query or {}
    start_raw, end_raw, days_raw = q.get("start"), q.get("end"), q.get("days")
    if start_raw or end_raw:
        if days_raw:
            raise WindowError("bad_range")
        now = (now or datetime.now(timezone.utc)).astimezone(zone)
        start = _parse_bound(start_raw, zone) if start_raw else now
        end = _parse_bound(end_raw, zone) if end_raw else start + timedelta(days=30)
        # 0.4.101: compare real elapsed time in UTC. Same-tzinfo aware datetimes subtract by wall
        # clock, which is an hour off across a DST change (a 92-day UTC window looked like 91d23h
        # or 92d1h).
        span = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
        if span <= timedelta(0):
            raise WindowError("bad_range")
        if span > timedelta(days=MAX_DAYS):
            raise WindowError("window_too_large")
        return start, end
    if days_raw in (None, ""):
        days = DEFAULT_DAYS
    else:
        try:
            days = int(str(days_raw))
        except ValueError:
            raise WindowError("bad_range") from None
        if days < 1:
            raise WindowError("bad_range")
        if days > MAX_DAYS:
            raise WindowError("window_too_large")
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    start = datetime.combine(today, dtime(0), zone)
    end = datetime.combine(today + timedelta(days=days), dtime(0), zone)  # local midnight across DST
    return start, end


# ---------------------------------------------------------------- events

def _norm_point(raw: Any, zone) -> tuple[str, bool, datetime] | None:
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=zone)
        dt = dt.astimezone(zone)
        return dt.isoformat(), False, dt
    if isinstance(raw, date):
        return raw.isoformat(), True, datetime.combine(raw, dtime(0), zone)
    s = str(raw or "").strip()
    if not s:
        return None
    if len(s) == 10:
        try:
            d = date.fromisoformat(s)
        except ValueError:
            return None
        return d.isoformat(), True, datetime.combine(d, dtime(0), zone)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)
    return dt.isoformat(), False, dt


_UID_DOMAIN = b"patrimony.calendar.uid.v1"
_UID_FALLBACK_KEY = os.urandom(32)  # only if the house key is unreadable; stable until restart


def _uid_key_sync(hass) -> bytes:
    """Sub-key derived from the house chat.key (32 bytes, 0600, never logged). Blocking on first use."""
    try:
        from .chat import load_chat_key

        return hmac.new(load_chat_key(hass), _UID_DOMAIN, hashlib.sha256).digest()
    except Exception:
        _LOGGER.warning("patrimony_calendar_uid_key_unavailable")
        return _UID_FALLBACK_KEY


async def async_uid_key(hass) -> bytes:
    job = getattr(hass, "async_add_executor_job", None)
    if job is not None:
        return await job(_uid_key_sync, hass)
    return _uid_key_sync(hass)


def event_uid(key: bytes, cal_id: str, recurrence_id: str | None, start: str, title: str, ha_uid: Any = None) -> str:
    """HA's uid when present; else keyed sha256 of calendarId|recurrenceId|start|title, 16 hex."""
    if isinstance(ha_uid, str) and ha_uid.strip():
        return ha_uid.strip()
    msg = "|".join((cal_id, recurrence_id or "", start, title)).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:16]


def normalize_event(cal_id: str, ev: dict[str, Any], zone, uid_key: bytes | None = None) -> dict[str, Any] | None:
    s = _norm_point(ev.get("start"), zone)
    e = _norm_point(ev.get("end"), zone)
    if s is None:
        return None
    all_day = s[1]
    if e is None:
        if all_day:
            d = date.fromisoformat(s[0]) + timedelta(days=1)
            e = (d.isoformat(), True, datetime.combine(d, dtime(0), zone))
        else:
            e = s
    elif all_day and not e[1]:
        e = (e[2].date().isoformat(), True, e[2])
    rec = ev.get("recurrence_id") or ev.get("recurrenceId") or ev.get("uid")
    title = str(ev.get("summary") or ev.get("title") or "")
    rec_id = str(rec) if rec else None
    out = {
        "calendarId": cal_id,
        "uid": event_uid(uid_key if uid_key is not None else _UID_FALLBACK_KEY, cal_id, rec_id, s[0], title,
                         ev.get("uid")),
        "title": title,
        "start": s[0],
        "end": e[0],
        "allDay": all_day,
        "recurrenceId": rec_id,
        "_sort": s[2].timestamp(),
    }
    loc = ev.get("location")
    if isinstance(loc, str) and loc.strip():
        out["location"] = loc.strip()
    return out


def cache_clear(hass) -> None:
    try:
        hass.data.pop(_CACHE_KEY, None)
    except Exception:
        pass


async def _fetch_raw(hass, entity_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    resp = await hass.services.async_call(
        "calendar",
        "get_events",
        {"start_date_time": start.isoformat(), "end_date_time": end.isoformat()},
        target={"entity_id": [entity_id]},
        blocking=True,
        return_response=True,
    )
    block = (resp or {}).get(entity_id) or {}
    events = block.get("events") if isinstance(block, dict) else None
    return [e for e in (events or []) if isinstance(e, dict)]


async def _fetch_cached(hass, entity_id, start, end):
    cache = hass.data.setdefault(_CACHE_KEY, {})
    key = (entity_id, start.isoformat(), end.isoformat())
    hit = cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    events = await asyncio.wait_for(_fetch_raw(hass, entity_id, start, end), FETCH_TIMEOUT_S)
    for k in [k for k, v in cache.items() if now - v[0] >= CACHE_TTL_S]:
        cache.pop(k, None)
    cache[key] = (now, events)
    return events


async def collect_events(hass, cals, start, end, zone, *, member: bool) -> dict[str, Any]:
    async def one(cal):
        try:
            return cal, await _fetch_cached(hass, cal["_entity"], start, end), None
        except asyncio.TimeoutError:
            return cal, None, "timeout"
        except Exception:
            return cal, None, "fetch_failed"

    results = await asyncio.gather(*(one(c) for c in cals))
    uid_key = await async_uid_key(hass) if results else None
    events, errors = [], []
    for cal, raw, err in results:
        if err:
            _LOGGER.warning("patrimony_calendar_fetch_failed code=%s", err)
            errors.append({"calendarId": cal["id"], "code": err})
            continue
        for ev in raw:
            row = normalize_event(cal["id"], ev, zone, uid_key)  # uid from the original title
            if row is None:
                continue
            if member:
                row.pop("location", None)
                if cal["role"] == "stays":
                    row["title"] = MEMBER_STAYS_TITLE
            events.append(row)
    events.sort(key=lambda r: (r["_sort"], r["calendarId"], r["title"]))
    truncated = len(events) > MAX_EVENTS
    events = events[:MAX_EVENTS]
    for r in events:
        r.pop("_sort", None)
    body: dict[str, Any] = {"events": events}
    if truncated:
        body["truncated"] = True
    if errors:
        body["errors"] = errors
    return body


# ---------------------------------------------------------------- HTTP helpers

def etag_for(body: dict[str, Any]) -> str:
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return '"' + hashlib.sha256(canon.encode()).hexdigest()[:32] + '"'


def _if_none_match(request, etag: str) -> bool:
    headers = getattr(request, "headers", None) or {}
    raw = headers.get("If-None-Match") or ""
    if not raw:
        return False
    tags = [t.strip() for t in raw.split(",")]
    return "*" in tags or etag in tags or etag in [t[2:] if t.startswith("W/") else t for t in tags]


def _query(request):
    return getattr(request, "query", None) or {}


def _err(code, message, status, headers=None):
    from aiohttp import web

    return web.json_response({"error": {"code": code, "message": message}}, status=status, headers=headers)


async def build_list(hass, *, member: bool) -> dict[str, Any]:
    tzname, _ = house_zone(hass)
    cals = visible_calendars(hass, member=member)
    return {"calendars": [public_calendar(c) for c in cals], "timezone": tzname}


async def build_events(hass, request, *, member: bool):
    """Returns (body, None) or (None, error_response)."""
    tzname, zone = house_zone(hass)
    q = _query(request)
    try:
        start, end = parse_window(q, zone)
    except WindowError as exc:
        msg = "Window exceeds 92 days" if exc.code == "window_too_large" else "Invalid or reversed range"
        return None, _err(exc.code, msg, 400, {"Cache-Control": "no-store"})
    cals = visible_calendars(hass, member=member)
    ids_raw = str(q.get("ids") or "").strip()
    if ids_raw:
        wanted = [i.strip() for i in ids_raw.split(",") if i.strip()]
        by_id = {c["id"]: c for c in cals}
        unknown = [i for i in wanted if i not in by_id]
        if unknown:
            return None, _err("unknown_calendar", "Unknown calendar", 404, {"Cache-Control": "no-store"})
        cals = [by_id[i] for i in dict.fromkeys(wanted)]
    body = {"timezone": tzname, "start": start.isoformat(), "end": end.isoformat()}
    body.update(await collect_events(hass, cals, start, end, zone, member=member))
    return body, None


def _plain_response(request, body):
    from aiohttp import web

    tag = etag_for(body)
    headers = {"ETag": tag, "Cache-Control": "private, no-cache"}
    if _if_none_match(request, tag):
        return web.Response(status=304, headers=headers)
    return web.json_response(body, headers=headers)


async def _member_response(hass, request, row, body):
    """Same PQ rules as /m/state; ETag on plaintext, 304 (no body) when it matches."""
    from aiohttp import web

    from . import http as hh
    from .pq_shield.session import get_manager

    tag = etag_for(body)
    headers = {"ETag": tag, "Cache-Control": "no-store"}
    mgr = get_manager(hass)
    mid = str(row.get("memberId") or "")
    if _if_none_match(request, tag):
        # Still enforce the PQ session rule before revealing "unchanged".
        if not (mgr.shield_on and hh.member_pq_required(hass) and not mgr.member_session_present(mid)):
            return web.Response(status=304, headers=headers)
    resp = await hh._member_pq_response(hass, row, body)
    if getattr(resp, "status", 200) == 200:
        try:
            resp.headers["ETag"] = tag
        except Exception:
            pass
    return resp


def _owner_gate(hass, request):
    from . import http as hh

    if hh._first_entry(hass) is None:
        return hh._not_configured()
    if not house_clients.owner_or_admin(hass, request):
        return _err("forbidden", "Owner or admin required", 403)
    return None


class PatrimonyCalendarsView(HomeAssistantView):
    url = CALENDARS_PATH
    name = "api:patrimony_collection:calendars"
    requires_auth = True

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request):
        deny = _owner_gate(self.hass, request)
        if deny:
            return deny
        return _plain_response(request, await build_list(self.hass, member=False))


class PatrimonyCalendarEventsView(HomeAssistantView):
    url = CALENDAR_EVENTS_PATH
    name = "api:patrimony_collection:calendar_events"
    requires_auth = True

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request):
        deny = _owner_gate(self.hass, request)
        if deny:
            return deny
        body, err = await build_events(self.hass, request, member=False)
        return err or _plain_response(request, body)


class PatrimonyMemberCalendarsView(HomeAssistantView):
    url = MEMBER_CALENDARS_PATH
    name = "api:patrimony_collection:member_calendars"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request):
        from . import http as hh

        row, deny = await hh._member_auth(self.hass, request)
        if deny:
            return deny
        if hh._first_entry(self.hass) is None:
            return hh._not_configured()
        return await _member_response(self.hass, request, row, await build_list(self.hass, member=True))


class PatrimonyMemberCalendarEventsView(HomeAssistantView):
    url = MEMBER_CALENDAR_EVENTS_PATH
    name = "api:patrimony_collection:member_calendar_events"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request):
        from . import http as hh

        row, deny = await hh._member_auth(self.hass, request)
        if deny:
            return deny
        if hh._first_entry(self.hass) is None:
            return hh._not_configured()
        body, err = await build_events(self.hass, request, member=True)
        return err or await _member_response(self.hass, request, row, body)


class PatrimonyEditorCalendarsView(HomeAssistantView):
    """Panel (HA admin): every calendar.* entity with expose / sharedWithMembers / role."""

    url = EDITOR_CALENDARS_PATH
    name = "api:patrimony_collection:editor_calendars"
    requires_auth = True

    def __init__(self, hass) -> None:
        self.hass = hass

    def _doc(self, cfg):
        rows = []
        for st in _calendar_states(self.hass):
            attrs = getattr(st, "attributes", None) or {}
            r = cfg.get(st.entity_id) or _norm_row({})
            rows.append({"entity_id": st.entity_id, "name": str(attrs.get("friendly_name") or st.entity_id),
                         **r})
        rows.sort(key=lambda r: r["name"].lower())
        return {"calendars": rows, "roles": list(ROLES)}

    async def get(self, request):
        from aiohttp import web

        from .panel import _deny_if_not_admin

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        cfg = await self.hass.async_add_executor_job(load_config, self.hass)
        return web.json_response(self._doc(cfg))

    async def post(self, request):
        from aiohttp import web

        from .panel import _deny_if_not_admin

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        try:
            body = await request.json()
        except Exception:
            return _err("invalid_json", "Invalid JSON", 400)
        items = body.get("calendars") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return _err("invalid_body", "calendars list required", 400)
        rows = {}
        for it in items:
            if isinstance(it, dict) and str(it.get("entity_id") or "").startswith("calendar."):
                rows[str(it["entity_id"])] = it
        cfg = await self.hass.async_add_executor_job(save_config, self.hass, rows)
        return web.json_response({"ok": True, **self._doc(cfg)})


def register_views(hass) -> None:
    for view in (PatrimonyCalendarsView, PatrimonyCalendarEventsView, PatrimonyMemberCalendarsView,
                 PatrimonyMemberCalendarEventsView, PatrimonyEditorCalendarsView):
        hass.http.register_view(view(hass))
