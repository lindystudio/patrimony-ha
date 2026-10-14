"""0.4.98 member PQ shield handshake. Mirrors the owner flow, authenticated with Bearer pmt_.

GET  /m/pq/public        -> house public material (same as owner /pq_shield/public)
POST /m/pq/encap         -> {"encap": b64(1120)} -> {"ok": true, "session": "present"}
POST /m/pq/accept_encap  -> alias of /m/pq/encap (owner endpoint name)
POST /m/pq/rotate        -> drop THIS member's session, return public material
"""

from __future__ import annotations

from ..const import (
    MEMBER_PQ_ACCEPT_ENCAP_PATH,
    MEMBER_PQ_ENCAP_PATH,
    MEMBER_PQ_PUBLIC_PATH,
    MEMBER_PQ_ROTATE_PATH,
)
from .hybrid import HYBRID_CT_LEN, b64d
from .session import get_manager, run_blocking
from .views import HomeAssistantView, _json_error, _require_configured

_NO_STORE = {"Cache-Control": "no-store"}


async def _auth(hass, request):
    from ..http import _member_auth

    return await _member_auth(hass, request)


def _gate(mgr):
    if not mgr.available:
        return _json_error("unavailable", "PQ shield unavailable on this Home Assistant", 404)
    if not mgr.feature_enabled:
        return _json_error("disabled", "PQ shield feature is Off", 409)
    return None


class MemberPQPublicView(HomeAssistantView):
    url = MEMBER_PQ_PUBLIC_PATH
    name = "api:patrimony_collection:member_pq_public"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        _row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        deny = _require_configured(self.hass)
        if deny:
            return deny
        mgr = get_manager(self.hass)
        deny = _gate(mgr)
        if deny:
            return deny
        return web.json_response(await run_blocking(self.hass, mgr.public_material), headers=_NO_STORE)


class MemberPQEncapView(HomeAssistantView):
    url = MEMBER_PQ_ENCAP_PATH
    name = "api:patrimony_collection:member_pq_encap"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def post(self, request):
        from aiohttp import web

        row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        deny = _require_configured(self.hass)
        if deny:
            return deny
        mgr = get_manager(self.hass)
        deny = _gate(mgr)
        if deny:
            return deny
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
            body = await run_blocking(self.hass, mgr.accept_member_encap, row["memberId"], encap)
        except Exception:
            return _json_error("encap_failed", "decapsulation failed", 400)
        return web.json_response(body, headers=_NO_STORE)


class MemberPQAcceptEncapView(MemberPQEncapView):
    url = MEMBER_PQ_ACCEPT_ENCAP_PATH
    name = "api:patrimony_collection:member_pq_accept_encap"


class MemberPQRotateView(HomeAssistantView):
    url = MEMBER_PQ_ROTATE_PATH
    name = "api:patrimony_collection:member_pq_rotate"
    requires_auth = False

    def __init__(self, hass) -> None:
        self.hass = hass

    async def post(self, request):
        from aiohttp import web

        row, deny = await _auth(self.hass, request)
        if deny:
            return deny
        deny = _require_configured(self.hass)
        if deny:
            return deny
        mgr = get_manager(self.hass)
        deny = _gate(mgr)
        if deny:
            return deny
        await run_blocking(self.hass, mgr.drop_member_sessions, [row["memberId"]])
        return web.json_response(await run_blocking(self.hass, mgr.public_material), headers=_NO_STORE)


def register_member_pq_views(hass) -> None:
    hass.http.register_view(MemberPQPublicView(hass))
    hass.http.register_view(MemberPQEncapView(hass))
    hass.http.register_view(MemberPQAcceptEncapView(hass))
    hass.http.register_view(MemberPQRotateView(hass))
