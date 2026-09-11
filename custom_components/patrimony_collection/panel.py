"""Integrator mapping panel. entity_id is allowed here; never on PresentationDocument."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from .const import (
    CONF_CARDS,
    CONF_DISPLAY_NAME,
    CONF_HOUSE_EVENT_KEY,
    CONF_LOCATION_LABEL,
    CONF_MAPPINGS,
    CONF_PROPERTY_ID,
    CONF_TIMEZONE,
    DOMAIN,
    KINDS,
    SEVERITY_MODES,
    VALUE_TYPES,
)
from .mapping import build_presentation_document, live_entity_fields, load_shared_mapping, merge_options, normalize_editor_payload, seed_shared_mapping

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

WWW = Path(__file__).resolve().parent / "www"


def _first_entry(hass: HomeAssistant):
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:
        return None
    return entries[0] if entries else None


def _deny_if_not_admin(request):
    from aiohttp import web

    user = request.get("hass_user")
    if user is not None and not getattr(user, "is_admin", True):
        return web.json_response({"error": {"code": "forbidden"}}, status=403)
    return None


class PatrimonyUiView(HomeAssistantView):
    url = "/api/patrimony_collection/ui"
    name = "api:patrimony_collection:ui"
    requires_auth = False

    async def get(self, request):
        from aiohttp import web

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        path = WWW / "index.html"
        if not path.is_file():
            return web.Response(text="missing ui", status=500)
        resp = web.FileResponse(path)
        resp.headers["Cache-Control"] = "no-store"
        return resp


class PatrimonyPreviewView(HomeAssistantView):
    url = "/api/patrimony_collection/editor/preview"
    name = "api:patrimony_collection:editor_preview"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        entry = _first_entry(self.hass)
        if entry is None:
            return web.json_response({"error": {"code": "not_configured"}}, status=404)
        document = build_presentation_document(self.hass, dict(entry.data), dict(entry.options or {}))
        return web.json_response(document)


class PatrimonyEntitiesView(HomeAssistantView):
    url = "/api/patrimony_collection/editor/entities"
    name = "api:patrimony_collection:editor_entities"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        q = (request.query.get("q") or "").strip().lower()
        mapped = _mapped_entity_ids(self.hass)
        rows = []
        extra = []
        try:
            states = self.hass.states.async_all()
        except Exception:
            states = []
        from .mapping import humanize_object_id
        for state in states:
            eid = getattr(state, "entity_id", "") or ""
            attrs = getattr(state, "attributes", None) or {}
            name = str(attrs.get("friendly_name") or "").strip() or humanize_object_id(eid)
            domain = eid.split(".", 1)[0] if "." in eid else ""
            live = live_entity_fields(self.hass, state)
            row = {
                "entity_id": eid,
                "domain": domain,
                "name": name,
                "state": live["state"],
                "unit": live["unit"],
                "attributes": live["attributes"],
            }
            if eid in mapped:
                extra.append(row)
            if q and q not in name.lower() and q not in eid.lower():
                continue
            if len(rows) < 200:
                rows.append(row)
        by_id = {r["entity_id"]: r for r in rows}
        for r in extra:
            by_id[r["entity_id"]] = r
        rows = sorted(by_id.values(), key=lambda r: (r["domain"], r["name"]))
        return web.json_response({"entities": rows, "kinds": list(KINDS), "valueTypes": list(VALUE_TYPES), "severityModes": list(SEVERITY_MODES)})


class PatrimonyMappingView(HomeAssistantView):
    url = "/api/patrimony_collection/editor/mapping"
    name = "api:patrimony_collection:editor_mapping"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request):
        from aiohttp import web

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        entry = _first_entry(self.hass)
        if entry is None:
            return web.json_response({"error": {"code": "not_configured"}}, status=404)
        data, opts = merge_options(dict(entry.data), dict(entry.options or {}), load_shared_mapping(self.hass))
        return web.json_response(
            {
                "property": {
                    "id": data.get(CONF_PROPERTY_ID),
                    "displayName": data.get(CONF_DISPLAY_NAME),
                    "locationLabel": data.get(CONF_LOCATION_LABEL),
                    "timezone": data.get(CONF_TIMEZONE),
                },
                "cards": opts.get(CONF_CARDS) or [],
                "mappings": opts.get(CONF_MAPPINGS) or [],
            }
        )

    async def post(self, request):
        from aiohttp import web

        deny = _deny_if_not_admin(request)
        if deny:
            return deny
        entry = _first_entry(self.hass)
        if entry is None:
            return web.json_response({"error": {"code": "not_configured"}}, status=404)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"code": "invalid_json"}}, status=400)
        shared = load_shared_mapping(self.hass)
        _data, existing_opts = merge_options(dict(entry.data), dict(entry.options or {}), shared)
        existing_cards = list(existing_opts.get(CONF_CARDS) or [])
        existing_maps = list(existing_opts.get(CONF_MAPPINGS) or [])
        cards, mappings = normalize_editor_payload(body, existing_cards, existing_maps)
        options = dict(entry.options or {})
        options[CONF_CARDS] = cards
        options[CONF_MAPPINGS] = mappings
        if CONF_HOUSE_EVENT_KEY in (entry.options or {}):
            options[CONF_HOUSE_EVENT_KEY] = entry.options.get(CONF_HOUSE_EVENT_KEY)
        self.hass.config_entries.async_update_entry(entry, options=options)
        seed_shared_mapping(self.hass, dict(entry.data), options)
        # Force rewrite mapping.json with the saved union
        try:
            path = Path(self.hass.config.path("patrimony_collection/mapping.json"))
            existing = {}
            if path.is_file():
                existing = json.loads(path.read_text())
            payload = {
                "property_id": entry.data.get(CONF_PROPERTY_ID),
                "display_name": entry.data.get(CONF_DISPLAY_NAME),
                "location_label": entry.data.get(CONF_LOCATION_LABEL),
                "timezone": entry.data.get(CONF_TIMEZONE),
                "cards": cards,
                "mappings": mappings,
            }
            if isinstance(existing, dict) and (existing.get("cards") or existing.get("mappings")):
                # explicit editor save replaces file contents with the edited set
                pass
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n")
        except Exception:
            pass
        document = build_presentation_document(self.hass, dict(entry.data), options)
        return web.json_response({"ok": True, "document": document})




def _mapped_entity_ids(hass: HomeAssistant) -> set:
    entry = _first_entry(hass)
    if entry is None:
        return set()
    _data, opts = merge_options(dict(entry.data), dict(entry.options or {}), load_shared_mapping(hass))
    ids = set()
    for row in opts.get(CONF_MAPPINGS) or []:
        if not isinstance(row, dict):
            continue
        eid = str(row.get("entity_id") or "").strip()
        if eid:
            ids.add(eid)
    return ids


def _component_version() -> str:
    try:
        return str(json.loads((Path(__file__).resolve().parent / "manifest.json").read_text()).get("version") or "0")
    except Exception:
        return "0"


async def async_setup_panel(hass: HomeAssistant) -> None:
    hass.http.register_view(PatrimonyUiView())
    hass.http.register_view(PatrimonyPreviewView(hass))
    hass.http.register_view(PatrimonyEntitiesView(hass))
    hass.http.register_view(PatrimonyMappingView(hass))
    ver = _component_version()
    iframe_url = f"/api/patrimony_collection/ui?v={ver}"
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig("/patrimony_collection_panel", str(WWW), False)]
        )
        iframe_url = f"/patrimony_collection_panel/index.html?v={ver}"
    except Exception:
        try:
            hass.http.register_static_path(
                "/patrimony_collection_panel",
                str(WWW),
                cache_headers=False,
            )
            iframe_url = f"/patrimony_collection_panel/index.html?v={ver}"
        except Exception:
            pass
    try:
        from homeassistant.components import frontend

        try:
            frontend.async_remove_panel(hass, "patrimony")
        except Exception:
            pass
        frontend.async_register_built_in_panel(
            hass,
            component_name="iframe",
            sidebar_title="Patrimony",
            sidebar_icon="mdi:wallet-travel",
            frontend_url_path="patrimony",
            config={"url": iframe_url},
            require_admin=True,
        )
    except Exception:
        pass
