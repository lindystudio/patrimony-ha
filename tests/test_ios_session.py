"""POST/GET /api/patrimony_collection/ios_session — last phone session.

Offline: validate, persist, GET. IP is the request peer, not the body.
Never logs the payload as a dump. No hek_ interaction. Off PresentationDocument.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from custom_components.patrimony_collection.const import (
    FORBIDDEN_WIRE_KEYS,
    IOS_APP_VERSION_MAX,
    IOS_DEVICE_NAME_MAX,
    IOS_SESSION_FILE,
    IOS_SESSION_PATH,
)
from custom_components.patrimony_collection.http import (
    PatrimonyIosSessionView,
    notify_status_payload,
)
from custom_components.patrimony_collection.ios_session import (
    INVALID_IOS_SESSION,
    apply_ios_session_payload,
    document,
    persist_ios_session,
    request_peer_ip,
    validate_ios_session_payload,
)
from custom_components.patrimony_collection.mapping import (
    DEMO_ENTRY_DATA,
    DEMO_OPTIONS,
    build_presentation_document,
)
from custom_components.patrimony_collection import ios_session as ios_session_mod

DEVICE = "Petros's iPhone"
VERSION = "1.2.3"
PEER = "203.0.113.10"
SPOOF = "198.51.100.9"
FIXED = datetime(2026, 9, 20, 15, 54, tzinfo=timezone.utc)


class _Hass:
    def __init__(self, root: Path):
        self.config = type("c", (), {"path": lambda _self, p, root=root: str(root / p)})()


class _Req:
    def __init__(self, remote):
        self.remote = remote


def _blob(*parts) -> str:
    return " ".join(str(p) for p in parts)


def test_ios_session_path_and_view_require_bearer() -> None:
    assert IOS_SESSION_PATH == "/api/patrimony_collection/ios_session"
    assert PatrimonyIosSessionView.requires_auth is True
    assert PatrimonyIosSessionView.url == IOS_SESSION_PATH


def test_validate_rejects_empty_and_overlong() -> None:
    assert validate_ios_session_payload({"deviceName": DEVICE, "appVersion": VERSION}) == (
        DEVICE,
        VERSION,
    )
    for payload in (
        None,
        [],
        {},
        {"deviceName": DEVICE},
        {"appVersion": VERSION},
        {"deviceName": "", "appVersion": VERSION},
        {"deviceName": "   ", "appVersion": VERSION},
        {"deviceName": DEVICE, "appVersion": ""},
        {"deviceName": DEVICE, "appVersion": "   "},
        {"deviceName": DEVICE, "appVersion": None},
        {"deviceName": None, "appVersion": VERSION},
        {"deviceName": 1, "appVersion": VERSION},
        {"deviceName": "x" * (IOS_DEVICE_NAME_MAX + 1), "appVersion": VERSION},
        {"deviceName": DEVICE, "appVersion": "x" * (IOS_APP_VERSION_MAX + 1)},
    ):
        assert validate_ios_session_payload(payload) is None


def test_validate_trims_and_accepts_max_length() -> None:
    name = "A" * IOS_DEVICE_NAME_MAX
    ver = "B" * IOS_APP_VERSION_MAX
    assert validate_ios_session_payload(
        {"deviceName": f"  {name}  ", "appVersion": f" {ver} "}
    ) == (name, ver)


def test_unseen_get_is_seen_false(tmp_path) -> None:
    hass = _Hass(tmp_path)
    assert document(hass) == {"seen": False}


def test_persist_and_get_uses_peer_ip_not_body(tmp_path) -> None:
    hass = _Hass(tmp_path)
    body, status = apply_ios_session_payload(
        hass,
        {"deviceName": f"  {DEVICE} ", "appVersion": f"{VERSION}  ", "ip": SPOOF},
        PEER,
        now=FIXED,
    )
    assert status == 200
    assert body == {"ok": True}
    assert SPOOF not in _blob(body)
    doc = document(hass)
    assert doc == {
        "seen": True,
        "ip": PEER,
        "deviceName": DEVICE,
        "appVersion": VERSION,
        "lastInteractionAt": "2026-09-20T15:54:00Z",
    }
    assert SPOOF not in json.dumps(doc)
    stored = json.loads((tmp_path / IOS_SESSION_FILE).read_text())
    assert stored["ip"] == PEER
    assert stored.get("ip") != SPOOF


def test_overwrite_last_session_only(tmp_path) -> None:
    hass = _Hass(tmp_path)
    apply_ios_session_payload(
        hass, {"deviceName": "Old Phone", "appVersion": "0.1"}, "192.0.2.1", now=FIXED
    )
    later = datetime(2026, 9, 20, 16, 10, tzinfo=timezone.utc)
    apply_ios_session_payload(
        hass, {"deviceName": DEVICE, "appVersion": VERSION}, PEER, now=later
    )
    doc = document(hass)
    assert doc["deviceName"] == DEVICE
    assert doc["appVersion"] == VERSION
    assert doc["ip"] == PEER
    assert doc["lastInteractionAt"] == "2026-09-20T16:10:00Z"
    assert "Old Phone" not in json.dumps(doc)


def test_invalid_post_does_not_persist(tmp_path) -> None:
    hass = _Hass(tmp_path)
    body, status = apply_ios_session_payload(
        hass, {"deviceName": "", "appVersion": VERSION, "ip": SPOOF}, PEER
    )
    assert status == 400
    assert body == INVALID_IOS_SESSION
    assert document(hass) == {"seen": False}
    assert not (tmp_path / IOS_SESSION_FILE).exists()
    assert DEVICE not in _blob(body)
    assert SPOOF not in _blob(body)
    assert PEER not in _blob(body)


def test_request_peer_ip_reads_remote_only() -> None:
    assert request_peer_ip(_Req(PEER)) == PEER
    assert request_peer_ip(_Req(None)) == ""
    assert request_peer_ip(_Req("  2001:db8::1  ")) == "2001:db8::1"


def test_notify_status_includes_ios_session(tmp_path) -> None:
    hass = _Hass(tmp_path)
    unseen = notify_status_payload(hass, None)
    assert unseen["iosSession"] == {"seen": False}
    apply_ios_session_payload(
        hass, {"deviceName": DEVICE, "appVersion": VERSION}, PEER, now=FIXED
    )
    seen = notify_status_payload(hass, None)
    assert seen["iosSession"]["seen"] is True
    assert seen["iosSession"]["ip"] == PEER
    assert seen["iosSession"]["deviceName"] == DEVICE


def test_persist_does_not_log_session_fields(tmp_path, caplog) -> None:
    hass = _Hass(tmp_path)
    with caplog.at_level(logging.DEBUG):
        persist_ios_session(
            hass, device_name=DEVICE, app_version=VERSION, ip=PEER, at="2026-09-20T15:54:00Z"
        )
        body, status = apply_ios_session_payload(
            hass, {"deviceName": DEVICE, "appVersion": VERSION}, PEER, now=FIXED
        )
    assert status == 200
    assert body == {"ok": True}
    log_blob = " ".join(record.getMessage() for record in caplog.records)
    assert DEVICE not in log_blob
    assert VERSION not in log_blob
    assert PEER not in log_blob
    assert "ios_session.json" not in log_blob


def test_module_has_no_hek_interaction() -> None:
    source = inspect.getsource(ios_session_mod)
    assert "hek_" not in source
    assert "house_event_key" not in source
    assert "houseEventKey" not in source
    assert "CONF_HOUSE_EVENT_KEY" not in source


def test_ios_session_never_on_presentation_document(tmp_path) -> None:
    hass = _Hass(tmp_path)
    apply_ios_session_payload(
        hass, {"deviceName": DEVICE, "appVersion": VERSION}, PEER, now=FIXED
    )
    doc = build_presentation_document(None, DEMO_ENTRY_DATA, DEMO_OPTIONS)
    blob = json.dumps(doc)
    assert "iosSession" not in blob
    assert "ios_session" not in blob
    assert "lastInteractionAt" not in blob
    assert PEER not in blob
    assert DEVICE not in blob
    assert VERSION not in blob
    assert "iosSession" in FORBIDDEN_WIRE_KEYS
    assert doc["schemaVersion"] == 1
