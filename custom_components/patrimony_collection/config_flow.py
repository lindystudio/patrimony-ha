"""Config flow — integrator maps a house; principal does not use this UI."""

from __future__ import annotations

from uuid import uuid4

import voluptuous as vol

from .const import (
    CONF_CARDS,
    CONF_DISPLAY_NAME,
    CONF_HOUSE_EVENT_KEY,
    CONF_LOCATION_LABEL,
    CONF_MAPPINGS,
    CONF_PROPERTY_ID,
    CONF_TIMEZONE,
    DEFAULT_PRIORITY,
    DOMAIN,
    KIND_DEFAULT_TITLES,
    KINDS,
    SEVERITY_MODES,
    VALUE_TYPES,
)

try:
    from homeassistant import config_entries
    from homeassistant.core import callback
    from homeassistant.helpers import selector
except ImportError:  # sketch without HA: keep the class shape importable-ish

    class _ConfigFlowBase:
        def __init_subclass__(cls, **kwargs):
            return super().__init_subclass__()

        async def async_show_form(self, **kwargs):
            return kwargs

        async def async_create_entry(self, **kwargs):
            return kwargs

        async def async_abort(self, **kwargs):
            return kwargs

        @staticmethod
        def async_get_options_flow(config_entry):
            return OptionsFlowHandler()

    class config_entries:  # type: ignore[no-redef]
        ConfigFlow = _ConfigFlowBase
        OptionsFlow = object

        class ConfigEntry:
            data: dict = {}
            options: dict = {}

    def callback(func):
        return func

    class selector:  # type: ignore[no-redef]
        class EntitySelector:
            def __init__(self, *args, **kwargs):
                pass

        class EntitySelectorConfig:
            def __init__(self, *args, **kwargs):
                pass


class PatrimonyCollectionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Single house per HA instance."""

    VERSION = 1
    _async_abort_entries_match = getattr(
        config_entries.ConfigFlow, "_async_abort_entries_match", lambda self, *a, **k: None
    )

    async def async_step_user(self, user_input: dict | None = None):
        getter = getattr(super(), "_async_current_entries", None)
        current = getter() if getter else []
        if current:
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            property_id = str(user_input[CONF_PROPERTY_ID]).strip().lower()
            display_name = str(user_input[CONF_DISPLAY_NAME]).strip()
            timezone = str(user_input[CONF_TIMEZONE]).strip()
            location = str(user_input.get(CONF_LOCATION_LABEL) or "").strip()
            if not display_name:
                errors[CONF_DISPLAY_NAME] = "empty"
            if "/" not in timezone:
                errors[CONF_TIMEZONE] = "invalid_timezone"
            from .mapping import is_uuid

            if not is_uuid(property_id):
                errors[CONF_PROPERTY_ID] = "invalid_uuid"
            if not errors:
                await self.async_set_unique_id(property_id)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_PROPERTY_ID: property_id,
                    CONF_DISPLAY_NAME: display_name,
                    CONF_TIMEZONE: timezone,
                }
                if location:
                    data[CONF_LOCATION_LABEL] = location
                return self.async_create_entry(
                    title=display_name,
                    data=data,
                    options={CONF_CARDS: [], CONF_MAPPINGS: [], CONF_HOUSE_EVENT_KEY: None},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_PROPERTY_ID): str,
                vol.Required(CONF_DISPLAY_NAME): str,
                vol.Optional(CONF_LOCATION_LABEL, default=""): str,
                vol.Required(CONF_TIMEZONE, default="UTC"): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_set_unique_id(self, unique_id: str, **kwargs):
        setter = getattr(super(), "async_set_unique_id", None)
        if setter:
            return await setter(unique_id, **kwargs)
        return None

    def _abort_if_unique_id_configured(self, **kwargs):
        fn = getattr(super(), "_abort_if_unique_id_configured", None)
        if fn:
            return fn(**kwargs)
        return None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Integrator mapping UI."""

    def _options(self) -> dict:
        return dict(self.config_entry.options or {})

    async def async_step_init(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_card", "add_item", "remove_item", "house_event_key"],
        )

    async def async_step_add_card(self, user_input: dict | None = None):
        if user_input is not None:
            kind = user_input["kind"]
            if kind not in KINDS:
                kind = "custom"
            options = self._options()
            cards = list(options.get(CONF_CARDS) or [])
            cards.append(
                {
                    "id": str(uuid4()),
                    "kind": kind,
                    "title": (user_input.get("title") or "").strip() or KIND_DEFAULT_TITLES[kind],
                    "priority": int(user_input.get("priority") or DEFAULT_PRIORITY),
                }
            )
            options[CONF_CARDS] = cards
            return self.async_create_entry(title="", data=options)

        schema = vol.Schema(
            {
                vol.Required("kind"): vol.In(list(KINDS)),
                vol.Optional("title", default=""): str,
                vol.Optional("priority", default=DEFAULT_PRIORITY): int,
            }
        )
        return self.async_show_form(step_id="add_card", data_schema=schema)

    async def async_step_add_item(self, user_input: dict | None = None):
        options = self._options()
        cards = list(options.get(CONF_CARDS) or [])
        if not cards:
            return self.async_abort(reason="no_cards")

        card_ids = {row["id"]: f"{row.get('title')} ({row.get('kind')})" for row in cards}
        if user_input is not None:
            mappings = list(options.get(CONF_MAPPINGS) or [])
            entity_id = user_input["entity_id"]
            mappings.append(
                {
                    "item_id": str(uuid4()),
                    "card_id": user_input["card_id"],
                    "entity_id": entity_id,
                    "state_attribute": (user_input.get("state_attribute") or None) or None,
                    "label": (user_input.get("label") or "").strip(),
                    "value_type": user_input.get("value_type") or "auto",
                    "unit": user_input.get("unit") or None,
                    "severity_mode": user_input.get("severity_mode") or "auto",
                }
            )
            options[CONF_MAPPINGS] = mappings
            return self.async_create_entry(title="", data=options)

        schema_dict: dict = {
            vol.Required("card_id"): vol.In(card_ids),
            vol.Required("entity_id"): str,
            vol.Optional("label", default=""): str,
            vol.Optional("value_type", default="auto"): vol.In(["auto", *VALUE_TYPES]),
            vol.Optional("state_attribute", default=""): str,
            vol.Optional("severity_mode", default="auto"): vol.In(list(SEVERITY_MODES)),
            vol.Optional("unit", default=""): str,
        }
        try:
            schema_dict[vol.Required("entity_id")] = selector.EntitySelector(
                selector.EntitySelectorConfig()
            )
        except Exception:
            pass
        return self.async_show_form(step_id="add_item", data_schema=vol.Schema(schema_dict))

    async def async_step_remove_item(self, user_input: dict | None = None):
        options = self._options()
        mappings = list(options.get(CONF_MAPPINGS) or [])
        choices = {
            row["item_id"]: f"{row.get('label') or row.get('entity_id')} ({row.get('entity_id')})"
            for row in mappings
            if row.get("item_id")
        }
        if not choices:
            return self.async_abort(reason="no_items")
        if user_input is not None:
            options[CONF_MAPPINGS] = [
                row for row in mappings if row.get("item_id") != user_input["item_id"]
            ]
            return self.async_create_entry(title="", data=options)
        schema = vol.Schema({vol.Required("item_id"): vol.In(choices)})
        return self.async_show_form(step_id="remove_item", data_schema=schema)

    async def async_step_house_event_key(self, user_input: dict | None = None):
        """Optional hek_ key for backend APNs ingest. Never an HA token."""
        if user_input is not None:
            key = (user_input.get(CONF_HOUSE_EVENT_KEY) or "").strip() or None
            if key and (key.startswith("eyJ") or "token" in key.lower()):
                return self.async_show_form(
                    step_id="house_event_key",
                    data_schema=vol.Schema({vol.Optional(CONF_HOUSE_EVENT_KEY, default=""): str}),
                    errors={CONF_HOUSE_EVENT_KEY: "looks_like_ha_token"},
                )
            options = self._options()
            options[CONF_HOUSE_EVENT_KEY] = key
            return self.async_create_entry(title="", data=options)
        schema = vol.Schema({vol.Optional(CONF_HOUSE_EVENT_KEY, default=""): str})
        return self.async_show_form(step_id="house_event_key", data_schema=schema)

