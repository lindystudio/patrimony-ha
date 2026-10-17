"""REST GET /api/patrimony_collection/state — connect/reconnect only."""

from __future__ import annotations

import logging
from typing import Any

from .const import (
    CONF_MEMBER_PQ_REQUIRED,
    MEMBER_PQ_REQUIRED,
    CLIENT_PATH,
    CLIENT_UNPAIR_PATH,
    CLIENTS_PATH,
    MEMBER_CHAT_PATH,
    MEMBER_PAIRING_PATH,
    MEMBER_REVOKE_PATH,
    MEMBER_STATE_PATH,
    MEMBERS_MINT_PATH,
    MEMBERS_PATH,
    CONF_HOUSE_EVENT_KEY,
    CONF_PROPERTY_ID,
    CONTACTS_PATH,
    DOMAIN,
    EVENT_KEY_PATH,
    IOS_SESSION_PATH,
    NOTES_PATH,
    NOTIFY_PATH,
    PAIR_PATH,
    PAIRING_PATH,
    PUSH_LOG_PATH,
    PROPERTY_ADOPT_LOG_PATH,
    PROPERTY_PATH,
    PAIR_CLAIM_PATH,
    PHOTO_MAX_BYTES,
    PHOTO_PATH,
    PHOTO_SOURCE_HEADER,
    REST_PATH,
    VERSION_PATH,
)
from . import adopt as house_adopt
from . import clients as house_clients
from . import contacts as house_contacts
from . import ios_session as house_ios_session
from . import members as house_members
from . import notes as house_notes
from . import notify as house_notify
from . import pair as house_pair
from . import photo as house_photo
from . import version as house_version
from .mapping import build_presentation_document
from .panel import _deny_if_not_admin

_LOGGER = logging.getLogger(__name__)

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


def _not_configured():
    from aiohttp import web

    return web.json_response(
        {
            "error": {
                "code": "not_configured",
                "message": "Patrimony Collection is not configured",
            }
        },
        status=404,
    )


class PatrimonyStateView(HomeAssistantView):
    """Full PresentationDocument. Auth is HA's Bearer token (Keychain)."""

    url = REST_PATH
    name = "api:patrimony_collection:state"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        entry = _first_entry(self.hass)
        if entry is None:
            return _not_configured()
        try:
            document = build_presentation_document(self.hass, dict(entry.data), dict(entry.options))
        except Exception:
            return web.json_response(
                {
                    "error": {
                        "code": "snapshot_failed",
                        "message": "patrimony_snapshot_failed",
                    }
                },
                status=500,
            )
        # Phone pairing status for THIS credential. Same object as GET /pairing.
        pairing = house_clients.pairing_document(self.hass, request)
        document = dict(document)
        document["pairing"] = pairing
        # Session Present → AEAD envelope; else cleartext (default Off / fail closed).
        from .pq_shield.session import get_manager as _pq_get_manager
        payload = _pq_get_manager(self.hass).maybe_wrap_document(document)
        if payload is not document:
            # Plaintext pairing beside the envelope so the bearer reads key status.
            payload = dict(payload)
            payload["pairing"] = pairing
        if pairing.get("status") == house_clients.STATUS_UNPAIRED:
            client_id = str(pairing.get("clientId") or "")
            if client_id:
                await house_clients.note_unpair_served(self.hass, client_id)
        return web.json_response(payload)

    async def post(self, request):
        from aiohttp import web

        return web.json_response(
            {
                "error": {
                    "code": "method_not_allowed",
                    "message": "v0 is read-only",
                }
            },
            status=405,
        )


class PatrimonyContactsView(HomeAssistantView):
    """Call list. Off PresentationDocument. GET/PUT same HA Bearer as state."""

    url = CONTACTS_PATH
    name = "api:patrimony_collection:contacts"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _property_id(self) -> str | None:
        return _property_id(self.hass)

    async def get(self, request):
        from aiohttp import web

        if _first_entry(self.hass) is None:
            return _not_configured()
        return web.json_response(house_contacts.document(self.hass, self._property_id()))

    async def put(self, request):
        from aiohttp import web

        if _first_entry(self.hass) is None:
            return _not_configured()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"code": "bad_json", "message": "contacts body must be JSON"}},
                status=400,
            )
        raw = payload.get("contacts") if isinstance(payload, dict) else payload
        saved = house_contacts.save_contacts(self.hass, raw)
        return web.json_response(
            {"schemaVersion": 1, "propertyId": self._property_id(), "contacts": saved}
        )


class PatrimonyPhotoView(HomeAssistantView):
    """House still as raw jpeg/webp. Off PresentationDocument. Same Bearer as state."""

    url = PHOTO_PATH
    name = "api:patrimony_collection:photo"
    requires_auth = True
    cors_allowed = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        resolved = house_photo.resolve_photo(self.hass)
        if resolved is None:
            return web.Response(status=404)
        data, ctype, source = resolved
        return web.Response(
            body=data,
            content_type=ctype,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                PHOTO_SOURCE_HEADER: source,
                "Access-Control-Expose-Headers": PHOTO_SOURCE_HEADER,
            },
        )

    async def post(self, request):
        from aiohttp import web

        if not house_photo.restore_default_photo(self.hass):
            return web.json_response(
                {
                    "error": {
                        "code": "no_default",
                        "message": "No bundled default photograph",
                    }
                },
                status=404,
            )
        return web.Response(status=204)

    async def put(self, request):
        from aiohttp import web

        try:
            length = int(request.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > PHOTO_MAX_BYTES:
            return web.json_response(
                {"error": {"code": "too_large", "message": "max 2 MB"}},
                status=400,
            )
        body = await request.read()
        reason = house_photo.reject_put(request.headers.get("Content-Type"), body)
        if reason:
            code = "too_large" if "2 MB" in reason else "bad_type"
            return web.json_response(
                {"error": {"code": code, "message": reason}},
                status=400,
            )
        house_photo.save_photo(self.hass, body)
        return web.Response(status=204)

    async def delete(self, request):
        from aiohttp import web

        if house_photo.delete_photo(self.hass):
            return web.Response(status=204)
        return web.Response(status=404)


class PatrimonyVersionView(HomeAssistantView):
    """Integration version, for the app's home settings. Answers before setup too,
    so a phone can tell which version it reached even when the house is not configured."""

    url = VERSION_PATH
    name = "api:patrimony_collection:version"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        return web.json_response(house_version.document())


class PatrimonyNotesView(HomeAssistantView):
    """House notepad. Off PresentationDocument. Same Bearer as state."""

    url = NOTES_PATH
    name = "api:patrimony_collection:notes"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        if _first_entry(self.hass) is None:
            return _not_configured()
        return web.json_response(house_notes.document(self.hass, _property_id(self.hass)))

    async def put(self, request):
        from aiohttp import web

        if _first_entry(self.hass) is None:
            return _not_configured()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"code": "bad_json", "message": "notes body must be JSON"}},
                status=400,
            )
        text = payload.get("text") if isinstance(payload, dict) else ""
        saved = house_notes.save_notes(self.hass, text, _property_id(self.hass))
        return web.json_response(saved)


class PatrimonyPairView(HomeAssistantView):
    """Admin-only. Returns a short https claim URL for the QR. Token is not in the QR."""

    url = PAIR_PATH
    name = "api:patrimony_collection:pair"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request):
        from aiohttp import web

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        user = request.get("hass_user")
        status, payload = await house_pair.mint_pairing(self.hass, user)
        return web.json_response(payload, status=status)


class PatrimonyPairClaimView(HomeAssistantView):
    """Camera opens this https URL. No HA login. Code is the ticket."""

    url = PAIR_CLAIM_PATH
    name = "api:patrimony_collection:pair_claim"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request, code=""):
        from aiohttp import web

        from . import pair_tickets

        if not pair_tickets.claim_limiter(self.hass).allow(request.remote):
            return web.Response(
                text="Too many attempts. Wait a minute and try again.",
                status=429,
                content_type="text/plain",
                headers={"Retry-After": "60", "Cache-Control": "no-store"},
            )
        ticket = await self.hass.async_add_executor_job(
            pair_tickets.claim_with_grace, self.hass, code, request.remote
        )
        if not ticket:
            return web.Response(
                text="This pairing code has expired or was already used.",
                status=410,
                content_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )
        if not house_pair.is_public_url(ticket.get("url")):
            return web.json_response(house_pair.no_public_url_body(), status=503)
        deep = house_pair.pairing_uri(ticket["url"], ticket["token"])
        want = (request.headers.get("Accept") or "").lower()
        if "application/json" in want:
            return web.json_response(
                {"url": ticket["url"], "public_url": ticket["url"], "token": ticket["token"]},
                headers={"Cache-Control": "no-store"},
            )
        return web.Response(
            text=house_pair.claim_html(deep),
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )


class PatrimonyNotifyView(HomeAssistantView):
    """Attention event via hek_. Never an HA token as the house event key."""

    url = NOTIFY_PATH
    name = "api:patrimony_collection:notify"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _hek(self) -> str | None:
        entry = _first_entry(self.hass)
        if entry is None:
            return None
        key = (entry.options or {}).get(CONF_HOUSE_EVENT_KEY)
        if key is None:
            return None
        text = str(key).strip()
        return text or None

    async def get(self, request):
        from aiohttp import web

        return web.json_response(notify_status_payload(self.hass, self._hek()))

    async def post(self, request):
        from aiohttp import web

        entry = _first_entry(self.hass)
        if entry is None:
            return _not_configured()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"code": "bad_json", "message": "notify body must be JSON"}},
                status=400,
            )
        if isinstance(payload, dict) and payload.get("check") is True:
            job = getattr(self.hass, "async_add_executor_job", None)
            try:
                if job:
                    reachable = await job(house_notify.probe_ingest)
                else:
                    reachable = house_notify.probe_ingest()
            except Exception:
                reachable = False
            return web.json_response(
                notify_status_payload(self.hass, self._hek(), reachable=reachable)
            )
        key = self._hek()
        if not house_notify.is_usable_house_event_key(key):
            return web.json_response(house_notify.MISSING_KEY, status=400)
        # Manual Soon Notify only. Client severity is ignored. Secrets never go on the title.
        decision = house_notify.manual_event(
            key, (payload or {}).get("title") if isinstance(payload, dict) else None
        )
        if not decision:
            return web.json_response(
                {"error": {"code": "bad_title", "message": "title must be 1–120 characters"}},
                status=400,
            )
        title = decision["title"]
        property_id = _property_id(self.hass)
        if not property_id:
            return _not_configured()
        job = getattr(self.hass, "async_add_executor_job", None)
        try:
            if job:
                status, outcome = await job(
                    house_notify.manual_send,
                    self.hass,
                    property_id,
                    key,
                    title,
                )
            else:
                status, outcome = house_notify.manual_send(
                    self.hass, property_id, key, title
                )
        except Exception:
            return web.json_response(
                {"ok": False, "error": {"code": "backend_error", "message": "Push failed"}},
                status=424,
            )
        if 200 <= int(status) < 300:
            return web.json_response({"ok": True, "upstreamStatus": outcome.get("status")})
        error: dict[str, Any] = {
            "code": "backend_error",
            "message": "Push failed",
            "upstreamStatus": outcome.get("status"),
        }
        if outcome.get("error"):
            error["upstreamError"] = outcome.get("error")
        return web.json_response({"ok": False, "error": error}, status=424)


class PatrimonyEventKeyView(HomeAssistantView):
    """Phone pushes hek_ after claim/rotate. Never an HA token. Never on patrimony/state."""

    url = EVENT_KEY_PATH
    name = "api:patrimony_collection:event_key"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request):
        from aiohttp import web

        entry = _first_entry(self.hass)
        if entry is None:
            return _not_configured()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(house_notify.INVALID_EVENT_KEY, status=400)
        options, body, status = house_notify.apply_event_key_payload(
            dict(entry.options or {}), payload
        )
        if options is None:
            # Do not persist. configured stays false (Missing) for retry/rotate.
            return web.json_response(body, status=status)
        updater = getattr(self.hass.config_entries, "async_update_entry", None)
        if updater:
            updater(entry, options=options)
        else:
            entry.options = options
        try:
            house_notify.mark_event_key_installed(self.hass)
        except Exception:
            pass
        try:
            house_adopt.stamp_claimed(
                self.hass, entry, house_clients.request_client_id(request)
            )
        except Exception:
            _LOGGER.warning("patrimony_claim_latch_failed")
        return web.json_response(body, status=status)


class PatrimonyIosSessionView(HomeAssistantView):
    """Latest phone↔house session. Off PresentationDocument. Same Bearer as state."""

    url = IOS_SESSION_PATH
    name = "api:patrimony_collection:ios_session"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        if _first_entry(self.hass) is None:
            return _not_configured()
        return web.json_response(house_ios_session.document(self.hass))

    async def post(self, request):
        from aiohttp import web

        if _first_entry(self.hass) is None:
            return _not_configured()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"code": "bad_json", "message": "ios_session body must be JSON"}},
                status=400,
            )
        peer = house_ios_session.request_peer_ip(request)
        body, status = house_ios_session.apply_ios_session_payload(
            self.hass, payload, peer
        )
        # PTR is best-effort after accept. Never await DNS on the heartbeat.
        if status == 200:
            house_ios_session.schedule_reverse_lookup(self.hass, peer)
            client_id = house_clients.request_client_id(request)
            if client_id:
                user = request.get("hass_user") if hasattr(request, "get") else None
                ha_name = getattr(user, "name", None) if user is not None else None
                device = ""
                version = ""
                if isinstance(payload, dict):
                    device = payload.get("deviceName")
                    version = payload.get("appVersion")
                house_clients.upsert_client(
                    self.hass,
                    client_id,
                    device_name=device if isinstance(device, str) else None,
                    app_version=version if isinstance(version, str) else None,
                    ha_username=str(ha_name) if ha_name else None,
                    touch_access=True,
                )
        return web.json_response(body, status=status)


class PatrimonyPushLogView(HomeAssistantView):
    """Last attention pushes. No hek_ and no tokens."""

    url = PUSH_LOG_PATH
    name = "api:patrimony_collection:push_log"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        return web.json_response(house_notify.push_log_document(self.hass))



class PatrimonyPairingView(HomeAssistantView):
    """Phone pairing status. Same Bearer as state. Always 200 while auth works."""

    url = PAIRING_PATH
    name = "api:patrimony_collection:pairing"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        if _first_entry(self.hass) is None:
            return _not_configured()
        body = house_clients.pairing_document(self.hass, request)
        try:
            body["adoptable"] = not house_adopt.is_claimed(self.hass, _first_entry(self.hass))
        except Exception:
            body["adoptable"] = False
        if body.get("status") == house_clients.STATUS_UNPAIRED:
            client_id = str(body.get("clientId") or "")
            if client_id:
                await house_clients.note_unpair_served(self.hass, client_id)
        return web.json_response(body)


class PatrimonyClientsView(HomeAssistantView):
    """Paired iOS clients. Admin, or the calling bearer when it is an active paired client.

    JSON is panel_clients_document. Rename and unpair stay admin-only.
    """

    url = CLIENTS_PATH
    name = "api:patrimony_collection:clients"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        if not house_clients.clients_list_allowed(self.hass, request):
            return web.json_response({"error": {"code": "forbidden"}}, status=403)
        if _first_entry(self.hass) is None:
            return _not_configured()
        return web.json_response(house_clients.panel_clients_document(self.hass))


class PatrimonyClientView(HomeAssistantView):
    """Panel rename for one paired client. Admin only."""

    url = CLIENT_PATH
    name = "api:patrimony_collection:client"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request, client_id):
        from aiohttp import web

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        if _first_entry(self.hass) is None:
            return _not_configured()
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"code": "invalid_json", "message": "Body must be JSON"}},
                status=400,
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"error": {"code": "invalid_json", "message": "Body must be JSON"}},
                status=400,
            )
        row, err = house_clients.rename_client(
            self.hass, client_id, payload.get("displayName")
        )
        if err == "not_found" or row is None:
            return web.json_response(
                {"error": {"code": "not_found", "message": "Client not found"}},
                status=404,
            )
        if err == "too_long":
            return web.json_response(
                {"error": {"code": "too_long", "message": "displayName max 80"}},
                status=400,
            )
        if err == "invalid":
            return web.json_response(
                {"error": {"code": "invalid", "message": "displayName rejected"}},
                status=400,
            )
        doc = house_clients.panel_clients_document(self.hass)
        for item in doc.get("clients") or []:
            if item.get("clientId") == row["clientId"]:
                return web.json_response({"ok": True, "client": item})
        return web.json_response(
            {
                "ok": True,
                "client": {
                    "clientId": row["clientId"],
                    "displayName": row["displayName"],
                    "pairedAt": row["pairedAt"],
                    "lastAccessAt": row["lastAccessAt"],
                },
            }
        )


class PatrimonyClientUnpairView(HomeAssistantView):
    """Panel unpair. Admin only. Double confirm is UI-only; one POST here."""

    url = CLIENT_UNPAIR_PATH
    name = "api:patrimony_collection:client_unpair"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request, client_id):
        from aiohttp import web

        # Admin: any client. Owner pair bearer (0.4.96): role=member clients only.
        target = house_clients.get_client(self.hass, client_id)
        is_member = house_members.is_member_client_id(client_id) or (
            target is not None and target.get("role") == house_clients.ROLE_MEMBER
        )
        if not house_clients.is_admin_request(request):
            if not (is_member and house_clients.owner_or_admin(self.hass, request)):
                return web.json_response({"error": {"code": "forbidden"}}, status=403)
        if _first_entry(self.hass) is None:
            return _not_configured()
        if is_member:
            revoked = await _run(self.hass, house_members.revoke_member, self.hass, client_id)
            if revoked is None and target is None:
                return web.json_response(
                    {"error": {"code": "not_found", "message": "Client not found"}},
                    status=404,
                )
            return web.json_response(
                {"ok": True, "clientId": client_id, "status": "unpaired", "reason": "unpaired"}
            )
        row = house_clients.mark_unpaired(self.hass, client_id)
        if row is None:
            return web.json_response(
                {"error": {"code": "not_found", "message": "Client not found"}},
                status=404,
            )
        return web.json_response(
            {
                "ok": True,
                "clientId": row["clientId"],
                "status": row["status"],
                "reason": "unpaired",
            }
        )



async def _run(hass, fn, *args):
    add_job = getattr(hass, "async_add_executor_job", None)
    if add_job is not None:
        return await add_job(fn, *args)
    return fn(*args)


def _json_error(code: str, message: str, status: int, headers=None):
    from aiohttp import web

    return web.json_response(
        {"error": {"code": code, "message": message}}, status=status, headers=headers
    )


_MEMBER_401_MESSAGES = {
    "unauthorized": "Member token required",
    "member_token_expired": "Member access has expired",
    "member_token_revoked": "Member access was revoked",
}


async def _member_auth(hass, request):
    """Authenticate a pmt_ member token. Returns (row, None) or (row|None, error_response)."""
    token = house_members.bearer_token(request)
    row, err = await _run(hass, house_members.authenticate, hass, token)
    if err and row is not None and err in ("member_token_revoked", "member_token_expired"):
        try:
            from .pq_shield.session import get_manager as _pq_get_manager

            await _run(hass, _pq_get_manager(hass).drop_member_sessions, [row.get("memberId")])
        except Exception:
            _LOGGER.warning("pq_shield_member_session_drop_failed")
    if err:
        return row, _json_error(
            err,
            _MEMBER_401_MESSAGES.get(err, "Unauthorized"),
            401,
            headers={"WWW-Authenticate": 'Bearer realm="patrimony-member"', "Cache-Control": "no-store"},
        )
    cid = house_members.client_id_for(row["memberId"])
    try:
        house_clients.upsert_client(
            hass,
            cid,
            touch_access=True,
            member_name=row.get("memberName"),
            invite_id=row.get("inviteId"),
            principal_id=row.get("principalId"),
        )
    except Exception:
        _LOGGER.warning("patrimony_member_client_upsert_failed")
    try:
        request[house_clients.MEMBER_CLIENT_KEY] = cid
    except TypeError:
        setattr(request, house_clients.MEMBER_CLIENT_KEY, cid)
    return row, None


def _member_pairing_body(hass, request, row) -> dict[str, Any]:
    body = house_clients.pairing_document(hass, request)
    body["role"] = house_clients.ROLE_MEMBER
    body["memberId"] = row.get("memberId")
    body["memberName"] = row.get("memberName")
    body["principalId"] = row.get("principalId")
    body["inviteId"] = row.get("inviteId")
    body["expiresAt"] = house_members.public_row(row)["expiresAt"]
    try:
        from .pq_shield.session import get_manager as _pq_get_manager

        mgr = _pq_get_manager(hass)
        body["pqShield"] = mgr.member_capability(row.get("memberId"))
        body["pqSessionRequired"] = bool(mgr.shield_on and member_pq_required(hass))
    except Exception:
        body["pqShield"] = None
        body["pqSessionRequired"] = False
    return body


def member_pq_required(hass) -> bool:
    """MEMBER_PQ_REQUIRED const, overridable by config-entry option member_pq_required."""
    entry = _first_entry(hass)
    opts = dict(getattr(entry, "options", None) or {}) if entry is not None else {}
    if CONF_MEMBER_PQ_REQUIRED in opts:
        return bool(opts.get(CONF_MEMBER_PQ_REQUIRED))
    return bool(MEMBER_PQ_REQUIRED)


async def _member_pq_response(hass, row, document):
    """Member /m/state and /m/chat: wrap with THIS member's PQ session (owner envelope shape).

    Shield Off -> plain. Shield On + session -> envelope (+ plaintext pairing beside it on
    /m/state, like the owner). Shield On, no session -> 428 pq_session_required, or plain
    when member_pq_required is False (transition).
    """
    from aiohttp import web

    from .pq_shield.session import get_manager as _pq_get_manager

    headers = {"Cache-Control": "no-store"}
    mgr = _pq_get_manager(hass)
    mid = str(row.get("memberId") or "")
    try:
        payload = await _run(hass, mgr.wrap_for_member, mid, document)
    except Exception:
        _LOGGER.error("pq_shield_member_wrap_failed")
        return _json_error("pq_wrap_failed", "Could not seal the response", 500, headers=headers)
    if payload is None:
        if member_pq_required(hass):
            return web.json_response(
                {
                    "error": {"code": "pq_session_required", "message": "PQ Shield session required"},
                    "pqShield": mgr.member_capability(mid),
                },
                status=428,
                headers=headers,
            )
        payload = document
    elif payload is not document and "pairing" in document:
        payload = dict(payload)
        payload["pairing"] = document["pairing"]
    return web.json_response(payload, headers=headers)


class PatrimonyMemberStateView(HomeAssistantView):
    """Member scope: PresentationDocument read. Auth is our pmt_ token, not HA."""

    url = MEMBER_STATE_PATH
    name = "api:patrimony_collection:member_state"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        row, deny = await _member_auth(self.hass, request)
        if deny:
            return deny
        entry = _first_entry(self.hass)
        if entry is None:
            return _not_configured()
        try:
            document = build_presentation_document(self.hass, dict(entry.data), dict(entry.options))
        except Exception:
            return _json_error("snapshot_failed", "patrimony_snapshot_failed", 500)
        pairing = _member_pairing_body(self.hass, request, row)
        document = dict(document)
        document["pairing"] = pairing
        return await _member_pq_response(self.hass, row, document)


class PatrimonyMemberPairingView(HomeAssistantView):
    """Member scope: pairing status. Revoked/expired -> 200 unpaired (wipe signal)."""

    url = MEMBER_PAIRING_PATH
    name = "api:patrimony_collection:member_pairing"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        row, deny = await _member_auth(self.hass, request)
        if deny:
            if row is not None:
                reason = "revoked" if row.get("revokedAt") else "expired"
                return web.json_response(
                    {
                        "status": house_clients.STATUS_UNPAIRED,
                        "propertyId": house_clients.property_id(self.hass),
                        "clientId": house_members.client_id_for(row["memberId"]),
                        "role": house_clients.ROLE_MEMBER,
                        "principalId": row.get("principalId"),
                        "reason": reason,
                    },
                    headers={"Cache-Control": "no-store"},
                )
            return deny
        if _first_entry(self.hass) is None:
            return _not_configured()
        return web.json_response(
            _member_pairing_body(self.hass, request, row), headers={"Cache-Control": "no-store"}
        )


class PatrimonyMemberChatView(HomeAssistantView):
    """Member scope: chat list read only. No post/delete/read-cursor/image."""

    url = MEMBER_CHAT_PATH
    name = "api:patrimony_collection:member_chat"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        from . import chat as house_chat

        _row, deny = await _member_auth(self.hass, request)
        if deny:
            return deny
        try:
            doc = await _run(self.hass, house_chat.list_document, self.hass, request)
        except Exception:
            return _json_error("store_failed", "Could not read chat", 500)
        for msg in doc.get("messages") or []:
            msg["canDelete"] = False
        return await _member_pq_response(self.hass, _row, doc)


def _deny_unless_owner(hass, request):
    if not house_clients.owner_or_admin(hass, request):
        return _json_error("forbidden", "Owner or admin only", 403)
    return None


class PatrimonyMembersView(HomeAssistantView):
    """Owner/admin: list members, pending (minted, unused) and inactive."""

    url = MEMBERS_PATH
    name = "api:patrimony_collection:members"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        deny = _deny_unless_owner(self.hass, request)
        if deny:
            return deny
        doc = await _run(self.hass, house_members.list_document, self.hass)
        doc["timezone"] = house_clients.house_timezone_name(self.hass)
        for bucket in ("members", "pending", "inactive"):
            for item in doc[bucket]:
                item["expiresAtDisplay"] = house_clients.format_in_house_tz(self.hass, item.get("expiresAt"))
                item["lastUsedAtDisplay"] = house_clients.format_in_house_tz(self.hass, item.get("lastUsedAt"))
        return web.json_response(doc, headers={"Cache-Control": "no-store"})


class PatrimonyMembersMintView(HomeAssistantView):
    """Owner/admin: mint a member token. Token is returned once, never stored."""

    url = MEMBERS_MINT_PATH
    name = "api:patrimony_collection:members_mint"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request):
        from aiohttp import web

        deny = _deny_unless_owner(self.hass, request)
        if deny:
            return deny
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return _json_error("invalid_json", "Body must be a JSON object", 400)
        house = house_pair.https_house_url(self.hass)
        if not (house_pair.house_url_is_usable(house) and house_pair.is_public_url(house)):
            return web.json_response(house_pair.no_public_url_body(), status=503)
        try:
            row, token = await _run(
                self.hass,
                house_members.mint,
                self.hass,
                payload.get("memberName"),
                payload.get("inviteId"),
                payload.get("ttlDays"),
                payload.get("principalId"),
            )
        except house_members.MemberInputError as err:
            return _json_error(err.code, err.message, 400)
        except house_members.MembersStoreUnreadable:
            _LOGGER.error("patrimony_members_unreadable_not_overwritten")
            return _json_error("store_unavailable", "Member store is unreadable", 503)
        return web.json_response(
            {"ok": True, "member": row, "token": token, "url": house, "public_url": house},
            status=201,
            headers={"Cache-Control": "no-store"},
        )


class PatrimonyMemberRevokeView(HomeAssistantView):
    """Owner pair bearer (or admin): revoke all member access for a backend principal.

    Idempotent (unknown/already revoked -> 200 with zero counts). Cancels pending mints.
    """

    url = MEMBER_REVOKE_PATH
    name = "api:patrimony_collection:member_revoke"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request, principal_id=""):
        from aiohttp import web

        deny = _deny_unless_owner(self.hass, request)
        if deny:
            return deny
        try:
            out = await _run(self.hass, house_members.revoke_principal, self.hass, principal_id)
        except house_members.MembersStoreUnreadable:
            return _json_error("store_unavailable", "Member store is unreadable", 503)
        return web.json_response(
            {
                "ok": True,
                "principalId": out["principalId"],
                "revoked": out["revoked"],
                "cancelledPending": out["cancelledPending"],
            }
        )


def notify_status_payload(hass, hek, *, reachable: bool | None = None) -> dict[str, Any]:
    """Connections / Check links JSON. House URL is the pairing resolver."""
    payload: dict[str, Any] = {
        "configured": house_notify.is_usable_house_event_key(hek),
        "ingestHost": house_notify.ingest_host(),
        **house_pair.house_url_status(hass),
        "iosSession": house_ios_session.document(hass),
    }
    if reachable is not None:
        payload["ok"] = bool(reachable)
    return payload


def _active_client(hass: HomeAssistant, request) -> tuple[str | None, bool]:
    """(client_id, active) from the bearer's JWT iss + an active clients.py row."""
    client_id = house_clients.request_client_id(request)
    if not client_id:
        return None, False
    row = house_clients.get_client(hass, client_id)
    return client_id, bool(row is not None and row.get("status") == house_clients.STATUS_ACTIVE)


class PatrimonyPropertyView(HomeAssistantView):
    """C9: GET the property id + adoptable; POST adopt an existing id (paired phone only)."""

    url = PROPERTY_PATH
    name = "api:patrimony_collection:property"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        entry = _first_entry(self.hass)
        if entry is None:
            return _not_configured()
        user = request.get("hass_user") if hasattr(request, "get") else None
        client_id, active = _active_client(self.hass, request)
        if not active and user is not None and not getattr(user, "is_admin", True):
            return web.json_response(
                {"error": {"code": "forbidden", "message": "admin or paired phone only"}}, status=403
            )
        return web.json_response(house_adopt.property_document(self.hass, entry))

    async def post(self, request):
        from aiohttp import web

        entry = _first_entry(self.hass)
        if entry is None:
            return _not_configured()
        client_id, active = _active_client(self.hass, request)
        payload = None
        length = getattr(request, "content_length", None)
        if not (isinstance(length, int) and length > 1024):
            try:
                payload = await request.json()
            except Exception:
                payload = None
        status, body = house_adopt.adopt(self.hass, entry, client_id, active, payload)
        return web.json_response(body, status=status)


class PatrimonyPropertyAdoptLogView(HomeAssistantView):
    """Admin read-only adopt log (ids, client id, time, outcome)."""

    url = PROPERTY_ADOPT_LOG_PATH
    name = "api:patrimony_collection:property:adopt_log"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        user = request.get("hass_user") if hasattr(request, "get") else None
        if user is None:
            return web.json_response({"error": {"code": "forbidden"}}, status=403)
        return web.json_response(house_adopt.adopt_log_document(self.hass))


def _first_entry(hass: HomeAssistant):
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:
        return None
    return entries[0] if entries else None


def _property_id(hass: HomeAssistant) -> str | None:
    entry = _first_entry(hass)
    if entry is None:
        return None
    return entry.data.get(CONF_PROPERTY_ID)


async def async_setup_http(hass: HomeAssistant) -> None:
    hass.http.register_view(PatrimonyStateView(hass))
    hass.http.register_view(PatrimonyContactsView(hass))
    hass.http.register_view(PatrimonyPhotoView(hass))
    hass.http.register_view(PatrimonyNotesView(hass))
    hass.http.register_view(PatrimonyVersionView(hass))
    hass.http.register_view(PatrimonyPairView(hass))
    hass.http.register_view(PatrimonyPairClaimView(hass))
    hass.http.register_view(PatrimonyNotifyView(hass))
    hass.http.register_view(PatrimonyEventKeyView(hass))
    hass.http.register_view(PatrimonyIosSessionView(hass))
    hass.http.register_view(PatrimonyPairingView(hass))
    hass.http.register_view(PatrimonyClientsView(hass))
    hass.http.register_view(PatrimonyClientView(hass))
    hass.http.register_view(PatrimonyClientUnpairView(hass))
    hass.http.register_view(PatrimonyPushLogView(hass))
    hass.http.register_view(PatrimonyMembersView(hass))
    hass.http.register_view(PatrimonyMembersMintView(hass))
    hass.http.register_view(PatrimonyMemberRevokeView(hass))
    hass.http.register_view(PatrimonyMemberStateView(hass))
    hass.http.register_view(PatrimonyMemberPairingView(hass))
    hass.http.register_view(PatrimonyMemberChatView(hass))
    from .pq_shield.member_views import register_member_pq_views
    register_member_pq_views(hass)
    hass.http.register_view(PatrimonyPropertyView(hass))
    hass.http.register_view(PatrimonyPropertyAdoptLogView(hass))
    from .chat import async_warm_chat_key, register_views as register_chat_views
    register_chat_views(hass)
    await async_warm_chat_key(hass)
    from .pq_shield import async_setup_pq_shield, register_pq_shield_views
    await async_setup_pq_shield(hass)
    register_pq_shield_views(hass)
    from .calendars import register_views as register_calendar_views
    register_calendar_views(hass)
    from .panel import async_setup_panel
    await async_setup_panel(hass)
