"""REST views for PQ shield Phase 1 (paths per api-freeze.md)."""

from __future__ import annotations

from typing import Any

from ..const import (
    CONF_PQ_SHIELD,
    DOMAIN,
    PQ_SHIELD_ACCEPT_ENCAP_PATH,
    PQ_SHIELD_CAPABILITY_PATH,
    PQ_SHIELD_DISABLE_PATH,
    PQ_SHIELD_FEATURE_PATH,
    PQ_SHIELD_PUBLIC_PATH,
    PQ_SHIELD_ROTATE_PATH,
)
from .hybrid import HYBRID_CT_LEN, b64d
from .session import get_manager

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


def _json_error(code: str, message: str, status: int):
    from aiohttp import web

    return web.json_response(
        {"error": {"code": code, "message": message}},
        status=status,
    )


def _first_entry(hass: HomeAssistant):
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:
        return None
    return entries[0] if entries else None


def _require_configured(hass: HomeAssistant):
    if _first_entry(hass) is None:
        return _json_error("not_configured", "Patrimony Collection is not configured", 404)
    return None


class PQShieldCapabilityView(HomeAssistantView):
    url = PQ_SHIELD_CAPABILITY_PATH
    name = "api:patrimony_collection:pq_shield_capability"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        deny = _require_configured(self.hass)
        if deny:
            return deny
        return web.json_response(get_manager(self.hass).capability())


class PQShieldPublicView(HomeAssistantView):
    url = PQ_SHIELD_PUBLIC_PATH
    name = "api:patrimony_collection:pq_shield_public"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        deny = _require_configured(self.hass)
        if deny:
            return deny
        mgr = get_manager(self.hass)
        if not mgr.available:
            return _json_error("unavailable", "PQ shield unavailable on this Home Assistant", 404)
        if not mgr.feature_enabled:
            return _json_error("disabled", "PQ shield feature is Off", 409)
        from aiohttp import web

        return web.json_response(mgr.public_material())


class PQShieldAcceptEncapView(HomeAssistantView):
    url = PQ_SHIELD_ACCEPT_ENCAP_PATH
    name = "api:patrimony_collection:pq_shield_accept_encap"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request):
        deny = _require_configured(self.hass)
        if deny:
            return deny
        mgr = get_manager(self.hass)
        if not mgr.available:
            return _json_error("unavailable", "PQ shield unavailable on this Home Assistant", 404)
        if not mgr.feature_enabled:
            return _json_error("disabled", "PQ shield feature is Off", 409)
        try:
            payload = await request.json()
        except Exception:
            return _json_error("bad_json", "accept_encap body must be JSON", 400)
        if not isinstance(payload, dict) or not isinstance(payload.get("encap"), str):
            return _json_error("bad_encap", "encap must be base64 string", 400)
        try:
            encap = b64d(payload["encap"])
        except Exception:
            return _json_error("bad_encap", "encap must be valid base64", 400)
        if len(encap) != HYBRID_CT_LEN:
            return _json_error("bad_encap", f"encap must be {HYBRID_CT_LEN} bytes", 400)
        try:
            body = mgr.accept_encap(encap)
        except Exception:
            return _json_error("encap_failed", "decapsulation failed", 400)
        from aiohttp import web

        return web.json_response(body)


class PQShieldRotateView(HomeAssistantView):
    url = PQ_SHIELD_ROTATE_PATH
    name = "api:patrimony_collection:pq_shield_rotate"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request):
        deny = _require_configured(self.hass)
        if deny:
            return deny
        mgr = get_manager(self.hass)
        if not mgr.available:
            return _json_error("unavailable", "PQ shield unavailable on this Home Assistant", 404)
        if not mgr.feature_enabled:
            return _json_error("disabled", "PQ shield feature is Off", 409)
        from aiohttp import web

        return web.json_response(mgr.rotate())


class PQShieldDisableView(HomeAssistantView):
    url = PQ_SHIELD_DISABLE_PATH
    name = "api:patrimony_collection:pq_shield_disable"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request):
        deny = _require_configured(self.hass)
        if deny:
            return deny
        mgr = get_manager(self.hass)
        from aiohttp import web

        return web.json_response(mgr.disable(wipe_keypair=True))



class PQShieldFeatureView(HomeAssistantView):
    """Integrator / options only — house-level PQ Shield gate (CONF_PQ_SHIELD). Not phone enable wire; not Connections UI."""

    url = PQ_SHIELD_FEATURE_PATH
    name = "api:patrimony_collection:pq_shield_feature"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        deny = _require_configured(self.hass)
        if deny:
            return deny
        mgr = get_manager(self.hass)
        return web.json_response(
            {
                "enabled": bool(mgr.feature_enabled),
                "capability": mgr.capability(),
            }
        )

    async def post(self, request):
        from aiohttp import web

        deny = _require_configured(self.hass)
        if deny:
            return deny
        entry = _first_entry(self.hass)
        if entry is None:
            return _json_error("not_configured", "Patrimony Collection is not configured", 404)
        try:
            payload = await request.json()
        except Exception:
            return _json_error("bad_json", "feature body must be JSON", 400)
        if not isinstance(payload, dict) or "enabled" not in payload:
            return _json_error("bad_body", "enabled boolean required", 400)
        enabled = bool(payload.get("enabled"))
        options = dict(entry.options or {})
        options[CONF_PQ_SHIELD] = enabled
        updater = getattr(self.hass.config_entries, "async_update_entry", None)
        if updater:
            updater(entry, options=options)
        else:
            entry.options = options
        # Options Off also wipes via _options_updated; when On, session stays until iOS Enable.
        if not enabled:
            try:
                get_manager(self.hass).disable(wipe_keypair=True)
            except Exception:
                pass
        mgr = get_manager(self.hass)
        return web.json_response(
            {
                "ok": True,
                "enabled": enabled,
                "capability": mgr.capability(),
            }
        )


def register_pq_shield_views(hass: HomeAssistant) -> None:
    hass.http.register_view(PQShieldCapabilityView(hass))
    hass.http.register_view(PQShieldPublicView(hass))
    hass.http.register_view(PQShieldAcceptEncapView(hass))
    hass.http.register_view(PQShieldRotateView(hass))
    hass.http.register_view(PQShieldDisableView(hass))
    hass.http.register_view(PQShieldFeatureView(hass))
