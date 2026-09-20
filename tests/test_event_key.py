"""POST /api/patrimony_collection/event_key — phone pushes hek_. Never logs or echoes it."""
from __future__ import annotations

import logging

from custom_components.patrimony_collection.const import (
    CONF_CARDS,
    CONF_HOUSE_EVENT_KEY,
    CONF_MAPPINGS,
    EVENT_KEY_PATH,
    FORBIDDEN_WIRE_KEYS,
)
from custom_components.patrimony_collection.http import (
    PatrimonyEventKeyView,
    notify_status_payload,
)
from custom_components.patrimony_collection.mapping import (
    DEMO_ENTRY_DATA,
    DEMO_OPTIONS,
    build_presentation_document,
)
from custom_components.patrimony_collection.notify import (
    apply_event_key_payload,
    is_usable_house_event_key,
    looks_like_ha_token,
    persist_house_event_key,
)

USABLE = "hek_offline_ok_key"
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.sig"


def _codes(body: dict) -> str:
    err = body.get("error") or {}
    return str(err.get("code") or "")


def _blob(*parts) -> str:
    return " ".join(str(p) for p in parts)


def test_event_key_path_and_view_require_bearer() -> None:
    assert EVENT_KEY_PATH == "/api/patrimony_collection/event_key"
    assert PatrimonyEventKeyView.requires_auth is True
    assert PatrimonyEventKeyView.url == EVENT_KEY_PATH


def test_valid_hek_persists_without_echo() -> None:
    existing = {
        CONF_CARDS: [{"id": "card-1"}],
        CONF_MAPPINGS: [{"item_id": "item-1"}],
        CONF_HOUSE_EVENT_KEY: None,
    }
    options, body, status = apply_event_key_payload(existing, {"houseEventKey": USABLE})
    assert status == 200
    assert body == {"configured": True}
    assert options is not None
    assert options[CONF_HOUSE_EVENT_KEY] == USABLE
    assert options[CONF_CARDS] == existing[CONF_CARDS]
    assert options[CONF_MAPPINGS] == existing[CONF_MAPPINGS]
    assert USABLE not in _blob(body)
    assert is_usable_house_event_key(options[CONF_HOUSE_EVENT_KEY]) is True


def test_reject_jwt_shaped_without_echo() -> None:
    existing = {CONF_HOUSE_EVENT_KEY: None}
    options, body, status = apply_event_key_payload(existing, {"houseEventKey": JWT})
    assert status == 400
    assert options is None
    assert _codes(body) == "invalid_house_event_key"
    assert JWT not in _blob(body)
    assert looks_like_ha_token(JWT) is True


def test_reject_empty_and_missing() -> None:
    for payload in (
        {"houseEventKey": ""},
        {"houseEventKey": "   "},
        {},
        {"houseEventKey": None},
    ):
        options, body, status = apply_event_key_payload({}, payload)
        assert status == 400
        assert options is None
        assert _codes(body) == "invalid_house_event_key"
        assert USABLE not in _blob(body)
        assert JWT not in _blob(body)


def test_reject_non_hek_prefix() -> None:
    options, body, status = apply_event_key_payload({}, {"houseEventKey": "not-a-key"})
    assert status == 400
    assert options is None
    assert _codes(body) == "invalid_house_event_key"
    assert "not-a-key" not in _blob(body)


def test_reject_non_object_body() -> None:
    options, body, status = apply_event_key_payload({}, ["hek_offline_ok_key"])
    assert status == 400
    assert options is None
    assert _codes(body) == "invalid_house_event_key"
    assert USABLE not in _blob(body)


def test_failed_post_leaves_configured_false() -> None:
    existing = {CONF_HOUSE_EVENT_KEY: None, CONF_CARDS: [{"id": "card-1"}]}
    for payload in (
        {"houseEventKey": ""},
        {"houseEventKey": JWT},
        {"houseEventKey": "not-a-key"},
        {},
    ):
        options, body, status = apply_event_key_payload(existing, payload)
        assert status == 400
        assert options is None
        assert _codes(body) == "invalid_house_event_key"
        assert existing[CONF_HOUSE_EVENT_KEY] is None
        assert existing[CONF_CARDS] == [{"id": "card-1"}]
        assert is_usable_house_event_key(existing[CONF_HOUSE_EVENT_KEY]) is False
        assert notify_status_payload(_Hass(), existing[CONF_HOUSE_EVENT_KEY])["configured"] is False


class _Hass:
    def __init__(self):
        self.config = type("c", (), {"external_url": None, "internal_url": None})()


def test_persist_does_not_log_secret(caplog) -> None:
    secret = "hek_offline_ok_key"
    with caplog.at_level(logging.DEBUG):
        stored = persist_house_event_key({CONF_HOUSE_EVENT_KEY: None}, secret)
        options, body, status = apply_event_key_payload(
            {CONF_CARDS: []}, {"houseEventKey": secret}
        )
    assert stored[CONF_HOUSE_EVENT_KEY] == secret
    assert status == 200
    assert options[CONF_HOUSE_EVENT_KEY] == secret
    log_blob = " ".join(record.getMessage() for record in caplog.records)
    assert secret not in log_blob
    assert "offline_ok_key" not in log_blob
    assert secret not in _blob(body)


def test_rotate_replaces_existing_hek_without_wiping_mapping() -> None:
    previous = "hek_previous_ok_key"
    rotated = "hek_rotated_ok_key"
    existing = {
        CONF_CARDS: DEMO_OPTIONS[CONF_CARDS],
        CONF_MAPPINGS: DEMO_OPTIONS[CONF_MAPPINGS],
        CONF_HOUSE_EVENT_KEY: previous,
    }
    options, body, status = apply_event_key_payload(
        existing, {"houseEventKey": rotated}
    )
    assert status == 200
    assert body["configured"] is True
    assert options[CONF_HOUSE_EVENT_KEY] == rotated
    assert options[CONF_HOUSE_EVENT_KEY] != previous
    assert options[CONF_MAPPINGS] == existing[CONF_MAPPINGS]
    assert previous not in _blob(body)
    assert rotated not in _blob(body)


def test_hek_never_on_presentation_document() -> None:
    options = dict(DEMO_OPTIONS)
    options[CONF_HOUSE_EVENT_KEY] = USABLE
    doc = build_presentation_document(None, DEMO_ENTRY_DATA, options)
    blob = str(doc)
    assert USABLE not in blob
    assert "hek_" not in blob
    assert CONF_HOUSE_EVENT_KEY not in blob
    assert "houseEventKey" not in blob
    assert CONF_HOUSE_EVENT_KEY in FORBIDDEN_WIRE_KEYS
    assert "houseEventKey" in FORBIDDEN_WIRE_KEYS
