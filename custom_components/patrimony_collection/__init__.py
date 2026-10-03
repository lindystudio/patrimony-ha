"""Patrimony Collection — emit PresentationDocument from mapped HA entities.

v0 is read-only. No writes to HA. energy is not a kind.
"""

from __future__ import annotations

import logging
from typing import Any

from .activity import record_mapped_activity
from .const import CONF_HOUSE_EVENT_KEY, CONF_MAPPINGS, CONF_PQ_SHIELD, CONF_PROPERTY_ID, DOMAIN, SNAPSHOT_DEBOUNCE_SECONDS
from .http import async_setup_http
from .mapping import build_presentation_document, load_shared_mapping, merge_options, seed_shared_mapping
from .websocket import async_fire_state, async_setup_websocket

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant, callback
    from homeassistant.helpers.event import async_track_state_change_event
except ImportError:  # sketch environment without HA installed

    ConfigEntry = Any  # type: ignore[misc,assignment]
    Event = Any  # type: ignore[misc,assignment]
    HomeAssistant = Any  # type: ignore[misc,assignment]

    def callback(func):
        return func

    def async_track_state_change_event(hass, entity_ids, listener):
        return lambda: None


def _entry_options(hass: HomeAssistant | None, entry: ConfigEntry) -> dict:
    shared = load_shared_mapping(hass)
    _data, opts = merge_options(dict(entry.data), dict(entry.options or {}), shared)
    return opts


def _mapped_ids(entry: ConfigEntry, hass: HomeAssistant | None = None) -> list[str]:
    ids: list[str] = []
    options = _entry_options(hass, entry) if hass is not None else (entry.options or {})
    for row in options.get(CONF_MAPPINGS) or []:
        entity_id = row.get("entity_id")
        if isinstance(entity_id, str) and entity_id:
            ids.append(entity_id)
    return ids


def _cancel_debounce(runtime: dict[str, Any]) -> None:
    pending = runtime.pop("debounce", None)
    if pending:
        try:
            pending()
        except Exception:
            pass


async def _emit_snapshot(hass: HomeAssistant, entry: ConfigEntry) -> None:
    try:
        document = build_presentation_document(
            hass, dict(entry.data), dict(entry.options or {})
        )
    except Exception:
        _LOGGER.error("patrimony_snapshot_failed")
        return
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(runtime, dict):
        # Mapped presented-value changes only. Never log entity_id.
        record_mapped_activity(runtime, document, at=document.get("generatedAt"))
    cards = document.get("cards") or []
    _LOGGER.info(
        "Patrimony snapshot emitted cards=%s kinds=%s",
        len(cards),
        [card.get("kind") for card in cards],
    )
    async_fire_state(hass, document)
    await _consider_attention(hass, entry)


async def _consider_attention(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """FIRE transitions only. Glance severity can change without a push.

    Disk and the events POST run in an executor. Missing hek_ updates the
    baseline and does not send. Entity unavailable is not a push.
    """
    from .mapping import attention_facts
    from .notify import dispatch_attention_pushes, is_usable_house_event_key

    try:
        facts = attention_facts(hass, dict(entry.data or {}), dict(entry.options or {}))
    except Exception:
        _LOGGER.error("patrimony_attention_facts_failed")
        return
    key = (entry.options or {}).get(CONF_HOUSE_EVENT_KEY)
    property_id = (entry.data or {}).get(CONF_PROPERTY_ID)
    if not is_usable_house_event_key(key):
        key = None
        property_id = None

    def _send() -> None:
        dispatch_attention_pushes(hass, facts, property_id, key)

    job = getattr(hass, "async_add_executor_job", None)
    try:
        if job:
            await job(_send)
        else:
            _send()
    except Exception:
        _LOGGER.error("patrimony_attention_push_failed")


def _bind_mapped_listener(hass: HomeAssistant, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
    for unsub in runtime.get("unsub") or []:
        try:
            unsub()
        except Exception:
            pass
    runtime["unsub"] = []

    @callback
    def _on_state(event: Event) -> None:
        # Mapped-only via async_track_state_change_event. Never log entity_id.
        _cancel_debounce(runtime)
        loop = getattr(hass, "loop", None)
        create = getattr(hass, "async_create_task", None)
        if loop is None or create is None:
            create and create(_emit_snapshot(hass, entry))
            return

        handle = loop.call_later(
            SNAPSHOT_DEBOUNCE_SECONDS,
            lambda: create(_emit_snapshot(hass, entry)),
        )
        runtime["debounce"] = handle.cancel

    mapped = _mapped_ids(entry, hass)
    if mapped:
        runtime["unsub"].append(async_track_state_change_event(hass, mapped, _on_state))
    _LOGGER.info(
        "Patrimony mapping loaded cards=%s items=%s",
        len((entry.options or {}).get("cards") or []),
        len((entry.options or {}).get(CONF_MAPPINGS) or []),
    )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    await async_setup_http(hass)
    await async_setup_websocket(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    runtime: dict[str, Any] = {
        "entry": entry,
        "unsub": [],
        "events": [],
        "presented": {},
    }
    hass.data[DOMAIN][entry.entry_id] = runtime

    seed_shared_mapping(hass, dict(entry.data), dict(entry.options or {}))

    _bind_mapped_listener(hass, entry, runtime)

    add_listener = getattr(entry, "add_update_listener", None)
    on_unload = getattr(entry, "async_on_unload", None)
    if add_listener and on_unload:
        on_unload(add_listener(_options_updated))

    await _emit_snapshot(hass, entry)
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return
    runtime["entry"] = entry
    # Feature Off → cleartext live path; wipe session/keypair (fail closed).
    if not bool((entry.options or {}).get(CONF_PQ_SHIELD, True)):
        try:
            from .pq_shield.session import get_manager
            get_manager(hass).disable(wipe_keypair=True)
        except Exception:
            _LOGGER.debug("pq_shield_disable_on_options_off_skipped")
    _bind_mapped_listener(hass, entry, runtime)
    await _emit_snapshot(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None) or {}
    _cancel_debounce(runtime)
    for unsub in runtime.get("unsub") or []:
        unsub()
    return True
