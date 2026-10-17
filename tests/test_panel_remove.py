"""0.4.101: panel re-register removes the old panel with warn_if_unknown=False (no 'Removing unknown panel')."""

from __future__ import annotations

import asyncio
import sys
import types

from custom_components.patrimony_collection import panel


class _Http:
    def __init__(self):
        self.views = []

    def register_view(self, v):
        self.views.append(v)

    async def async_register_static_paths(self, _paths):
        raise RuntimeError("no static in tests")

    def register_static_path(self, *a, **k):
        pass


def _stub_frontend(monkeypatch, remove):
    calls = {"remove": [], "register": []}
    fe = types.ModuleType("homeassistant.components.frontend")

    def async_remove_panel(hass, url_path, **kw):
        calls["remove"].append((url_path, kw))
        return remove(url_path, **kw)

    fe.async_remove_panel = async_remove_panel
    fe.async_register_built_in_panel = lambda hass, **kw: calls["register"].append(kw)
    comps = sys.modules.get("homeassistant.components") or types.ModuleType("homeassistant.components")
    comps.frontend = fe
    monkeypatch.setitem(sys.modules, "homeassistant", sys.modules.get("homeassistant") or types.ModuleType("homeassistant"))
    monkeypatch.setitem(sys.modules, "homeassistant.components", comps)
    monkeypatch.setitem(sys.modules, "homeassistant.components.frontend", fe)
    return calls


def _hass():
    return types.SimpleNamespace(http=_Http(), data={}, config=types.SimpleNamespace(path=lambda *p: "/tmp/x"))


def test_remove_panel_passes_warn_if_unknown_false(monkeypatch):
    calls = _stub_frontend(monkeypatch, lambda url, **kw: None)
    asyncio.run(panel.async_setup_panel(_hass()))
    assert calls["remove"] == [("patrimony", {"warn_if_unknown": False})]
    assert calls["register"] and calls["register"][0]["frontend_url_path"] == "patrimony"


def test_remove_panel_falls_back_on_old_ha(monkeypatch):
    def old(url, **kw):
        if kw:
            raise TypeError("unexpected keyword argument 'warn_if_unknown'")

    calls = _stub_frontend(monkeypatch, old)
    asyncio.run(panel.async_setup_panel(_hass()))
    assert calls["remove"] == [("patrimony", {"warn_if_unknown": False}), ("patrimony", {})]
    assert len(calls["register"]) == 1
