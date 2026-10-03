"""REST GET /api/patrimony_collection/state — connect/reconnect only."""

from __future__ import annotations

from typing import Any

from .const import (
    CLIENT_PATH,
    CLIENT_UNPAIR_PATH,
    CLIENTS_PATH,
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
    PAIR_CLAIM_PATH,
    PHOTO_MAX_BYTES,
    PHOTO_PATH,
    PHOTO_SOURCE_HEADER,
    REST_PATH,
)
from . import clients as house_clients
from . import contacts as house_contacts
from . import ios_session as house_ios_session
from . import notes as house_notes
from . import notify as house_notify
from . import pair as house_pair
from . import photo as house_photo
from .mapping import build_presentation_document
from .panel import _deny_if_not_admin

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

        ticket = house_pair.get_ticket(self.hass, code)
        if not ticket:
            return web.Response(text="This pairing code has expired.", status=404, content_type="text/plain")
        deep = house_pair.pairing_uri(ticket["url"], ticket["token"])
        want = (request.headers.get("Accept") or "").lower()
        if "application/json" in want:
            return web.json_response({"url": ticket["url"], "token": ticket["token"]})
        return web.Response(text=house_pair.claim_html(deep), content_type="text/html", charset="utf-8")


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
                status = await job(
                    house_notify.load_card_id_and_post,
                    self.hass,
                    property_id,
                    key,
                    title,
                )
            else:
                status = house_notify.load_card_id_and_post(
                    self.hass, property_id, key, title
                )
        except Exception:
            return web.json_response(
                {"ok": False, "error": {"code": "backend_error", "message": "Push failed"}},
                status=424,
            )
        if 200 <= int(status) < 300:
            return web.json_response({"ok": True})
        return web.json_response(
            {"ok": False, "error": {"code": "backend_error", "message": "Push failed"}},
            status=424,
        )


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

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        if _first_entry(self.hass) is None:
            return _not_configured()
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
    from .chat import register_views as register_chat_views
    register_chat_views(hass)
    from .pq_shield import async_setup_pq_shield, register_pq_shield_views
    await async_setup_pq_shield(hass)
    register_pq_shield_views(hass)
    from .panel import async_setup_panel
    await async_setup_panel(hass)
