"""WebSocket: command patrimony/get_state and event patrimony/state."""

from __future__ import annotations

from typing import Any

from .const import DOMAIN, EVENT_STATE, WS_TYPE_GET_STATE
from .mapping import build_presentation_document

try:
    import voluptuous as vol
    from homeassistant.components import websocket_api
    from homeassistant.core import HomeAssistant
except ImportError:  # sketch environment without HA installed

    class websocket_api:  # type: ignore[no-redef]
        @staticmethod
        def websocket_command(schema):
            def decorator(func):
                func._ws_schema = schema
                return func

            return decorator

        @staticmethod
        def async_response(func):
            return func

        @staticmethod
        def async_register_command(hass, handler):
            return None

        @staticmethod
        def result_message(msg_id, result):
            return {"id": msg_id, "type": "result", "success": True, "result": result}

        @staticmethod
        def error_message(msg_id, code, message):
            return {
                "id": msg_id,
                "type": "result",
                "success": False,
                "error": {"code": code, "message": message},
            }

    HomeAssistant = Any  # type: ignore[misc,assignment]

    try:
        import voluptuous as vol
    except ImportError:
        vol = None  # type: ignore[assignment]


def async_fire_state(hass: HomeAssistant, document: dict) -> None:
    """Fire event_type patrimony/state. event.data is the document (no wrapper keys)."""
    hass.bus.async_fire(EVENT_STATE, document)


# Name used by the first sketch draft
fire_state_event = async_fire_state


def _first_entry(hass: HomeAssistant):
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:
        return None
    return entries[0] if entries else None


def _send_result(connection, msg_id, document) -> None:
    if hasattr(connection, "send_result"):
        connection.send_result(msg_id, document)
        return
    connection.send_message(websocket_api.result_message(msg_id, document))


def _send_error(connection, msg_id, code: str, message: str) -> None:
    if hasattr(connection, "send_error"):
        connection.send_error(msg_id, code, message)
        return
    connection.send_message(websocket_api.error_message(msg_id, code, message))


_WS_SCHEMA = {vol.Required("type"): WS_TYPE_GET_STATE} if vol is not None else {"type": WS_TYPE_GET_STATE}


@websocket_api.websocket_command(_WS_SCHEMA)
@websocket_api.async_response
async def ws_get_state(hass: HomeAssistant, connection, msg) -> None:
    """Return PresentationDocument as result (reconnect helper)."""
    entry = _first_entry(hass)
    if entry is None:
        _send_error(
            connection,
            msg["id"],
            "not_configured",
            "Patrimony Collection is not configured",
        )
        return
    try:
        document = build_presentation_document(
            hass, dict(entry.data), dict(entry.options or {})
        )
    except Exception:
        _send_error(connection, msg["id"], "snapshot_failed", "patrimony_snapshot_failed")
        return
    _send_result(connection, msg["id"], document)


async def async_setup_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_get_state)
