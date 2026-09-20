"""POST/GET /api/patrimony_collection/ios_session — last phone session.

Offline: validate, persist, GET, display helpers, DNS fail-open.
IP is the request peer, not the body. PTR must not block POST.
Never logs the payload as a dump. No hek_ interaction. Off PresentationDocument.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from custom_components.patrimony_collection.const import (
    FORBIDDEN_WIRE_KEYS,
    IOS_APP_VERSION_MAX,
    IOS_DEVICE_NAME_MAX,
    IOS_IOS_VERSION_MAX,
    IOS_MODEL_MAX,
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
    async_resolve_and_store_hostname,
    display_device,
    display_host,
    document,
    is_generic_device_name,
    lookup_hostname,
    persist_ios_session,
    request_peer_ip,
    schedule_reverse_lookup,
    store_resolved_hostname,
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
IOS = "iOS 18.6"
MODEL = "iPhone17,1"
PEER = "203.0.113.10"
SPOOF = "198.51.100.9"
HOST = "host.example.com"
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
    assert validate_ios_session_payload({"deviceName": DEVICE, "appVersion": VERSION}) == {
        "deviceName": DEVICE,
        "appVersion": VERSION,
    }
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
    ) == {"deviceName": name, "appVersion": ver}


def test_validate_accepts_additive_ios_and_model_ignores_extras() -> None:
    parsed = validate_ios_session_payload(
        {
            "deviceName": f"  {DEVICE} ",
            "appVersion": f"{VERSION}  ",
            "iosVersion": f"  {IOS} ",
            "model": f" {MODEL} ",
            "modelIdentifier": "iPhone15,2",
            "ip": SPOOF,
            "hostname": "evil.example",
            "displayDevice": "nope",
            "extra": "drop-me",
        }
    )
    assert parsed == {
        "deviceName": DEVICE,
        "appVersion": VERSION,
        "iosVersion": IOS,
        "model": MODEL,
    }
    assert "ip" not in parsed
    assert "modelIdentifier" not in parsed
    assert "hostname" not in parsed


def test_validate_omits_invalid_optional_without_rejecting() -> None:
    parsed = validate_ios_session_payload(
        {
            "deviceName": DEVICE,
            "appVersion": VERSION,
            "iosVersion": "   ",
            "model": "x" * (IOS_MODEL_MAX + 1),
            "modelIdentifier": MODEL,
            "ip": SPOOF,
        }
    )
    assert parsed == {"deviceName": DEVICE, "appVersion": VERSION}
    parsed = validate_ios_session_payload(
        {
            "deviceName": DEVICE,
            "appVersion": VERSION,
            "iosVersion": "x" * (IOS_IOS_VERSION_MAX + 1),
            "model": 17,
        }
    )
    assert parsed == {"deviceName": DEVICE, "appVersion": VERSION}


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
        "displayDevice": DEVICE,
        "displayHost": PEER,
    }
    assert SPOOF not in json.dumps(doc)
    stored = json.loads((tmp_path / IOS_SESSION_FILE).read_text())
    assert stored["ip"] == PEER
    assert stored.get("ip") != SPOOF
    assert "hostname" not in stored
    assert "modelIdentifier" not in stored


def test_persist_additive_fields_and_helpers(tmp_path) -> None:
    hass = _Hass(tmp_path)
    body, status = apply_ios_session_payload(
        hass,
        {
            "deviceName": "iPhone",
            "appVersion": VERSION,
            "iosVersion": IOS,
            "model": MODEL,
            "modelIdentifier": "iPhone15,2",
            "ip": SPOOF,
        },
        PEER,
        now=FIXED,
    )
    assert status == 200
    assert body == {"ok": True}
    doc = document(hass)
    assert doc["iosVersion"] == IOS
    assert doc["model"] == MODEL
    assert doc["displayDevice"] == "iPhone · iOS 18.6"
    assert doc["displayHost"] == PEER
    assert "modelIdentifier" not in doc
    stored = json.loads((tmp_path / IOS_SESSION_FILE).read_text())
    assert stored["model"] == MODEL
    assert "modelIdentifier" not in stored
    assert stored.get("ip") == PEER


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


def test_display_device_generic_name_combo() -> None:
    assert is_generic_device_name("iPhone") is True
    assert is_generic_device_name("IPHONE") is True
    assert is_generic_device_name(" iPad ") is True
    assert is_generic_device_name("") is True
    assert is_generic_device_name(DEVICE) is False
    assert is_generic_device_name("iPhone 15 Pro") is False
    assert display_device("iPhone", IOS, MODEL) == "iPhone · iOS 18.6"
    assert display_device("iphone", IOS, None) == "iphone · iOS 18.6"
    assert display_device("iPad", None, "iPad14,1") == "iPad14,1"
    assert display_device("", IOS, MODEL) == IOS
    assert display_device("", None, MODEL) == MODEL
    assert display_device("iPhone", None, None) == "iPhone"
    assert display_device(DEVICE, IOS, MODEL) == f"{DEVICE} · {IOS}"
    assert display_device(DEVICE, None, MODEL) == DEVICE


def test_display_host_hostname_then_ip() -> None:
    assert display_host(PEER, HOST) == f"{HOST} · {PEER}"
    assert display_host(PEER, f"{HOST}.") == f"{HOST} · {PEER}"
    assert display_host(PEER, None) == PEER
    assert display_host(PEER, "") == PEER
    assert display_host("", HOST) == HOST
    assert display_host("", None) == ""


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
    assert seen["iosSession"]["displayDevice"] == DEVICE
    assert seen["iosSession"]["displayHost"] == PEER


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


def test_http_post_schedules_dns_without_awaiting() -> None:
    source = inspect.getsource(PatrimonyIosSessionView.post)
    assert "schedule_reverse_lookup" in source
    assert "await house_ios_session.async_resolve" not in source
    assert "lookup_hostname" not in source
    assert "gethostbyaddr" not in source
    apply_src = inspect.getsource(apply_ios_session_payload)
    assert "lookup_hostname" not in apply_src
    assert "gethostbyaddr" not in apply_src
    assert "schedule_reverse_lookup" not in apply_src


def test_apply_does_not_call_dns(tmp_path, monkeypatch) -> None:
    hass = _Hass(tmp_path)

    def boom(*_args, **_kwargs):
        raise AssertionError("heartbeat must not call DNS")

    monkeypatch.setattr(ios_session_mod, "lookup_hostname", boom)
    monkeypatch.setattr(socket, "gethostbyaddr", boom)
    body, status = apply_ios_session_payload(
        hass,
        {"deviceName": DEVICE, "appVersion": VERSION, "iosVersion": IOS, "model": MODEL},
        PEER,
        now=FIXED,
    )
    assert status == 200
    assert body == {"ok": True}
    assert "hostname" not in document(hass)


def test_lookup_hostname_fail_open(monkeypatch) -> None:
    def boom(_ip):
        raise OSError("forced dns failure")

    monkeypatch.setattr(socket, "gethostbyaddr", boom)
    assert lookup_hostname(PEER) is None
    assert lookup_hostname("") is None


def test_lookup_hostname_timeout_fail_open(monkeypatch) -> None:
    def slow(_ip):
        time.sleep(2)
        return (HOST, [], [PEER])

    monkeypatch.setattr(socket, "gethostbyaddr", slow)
    started = time.monotonic()
    assert lookup_hostname(PEER, timeout=0.05) is None
    assert time.monotonic() - started < 1.0


def test_lookup_hostname_success_strips_dot(monkeypatch) -> None:
    monkeypatch.setattr(socket, "gethostbyaddr", lambda _ip: (HOST + ".", [], [PEER]))
    assert lookup_hostname(PEER) == HOST


def test_store_hostname_then_document(tmp_path) -> None:
    hass = _Hass(tmp_path)
    apply_ios_session_payload(
        hass,
        {"deviceName": "iPhone", "appVersion": VERSION, "iosVersion": IOS, "model": MODEL},
        PEER,
        now=FIXED,
    )
    store_resolved_hostname(hass, PEER, HOST + ".")
    doc = document(hass)
    assert doc["hostname"] == HOST
    assert doc["displayHost"] == f"{HOST} · {PEER}"
    assert doc["displayDevice"] == "iPhone · iOS 18.6"


def test_store_hostname_skips_if_ip_changed(tmp_path) -> None:
    hass = _Hass(tmp_path)
    apply_ios_session_payload(
        hass, {"deviceName": DEVICE, "appVersion": VERSION}, PEER, now=FIXED
    )
    later = datetime(2026, 9, 20, 16, 10, tzinfo=timezone.utc)
    apply_ios_session_payload(
        hass, {"deviceName": DEVICE, "appVersion": VERSION}, "192.0.2.9", now=later
    )
    store_resolved_hostname(hass, PEER, HOST)
    assert "hostname" not in document(hass)
    assert document(hass)["displayHost"] == "192.0.2.9"


def test_async_resolve_fail_open_does_not_raise(tmp_path, monkeypatch) -> None:
    hass = _Hass(tmp_path)
    apply_ios_session_payload(
        hass, {"deviceName": DEVICE, "appVersion": VERSION}, PEER, now=FIXED
    )
    monkeypatch.setattr(ios_session_mod, "lookup_hostname", lambda *_a, **_k: None)
    asyncio.run(async_resolve_and_store_hostname(hass, PEER))
    assert "hostname" not in document(hass)
    assert document(hass)["displayHost"] == PEER


def test_async_resolve_stores_when_lookup_works(tmp_path, monkeypatch) -> None:
    hass = _Hass(tmp_path)
    apply_ios_session_payload(
        hass, {"deviceName": DEVICE, "appVersion": VERSION}, PEER, now=FIXED
    )
    monkeypatch.setattr(ios_session_mod, "lookup_hostname", lambda *_a, **_k: HOST)
    asyncio.run(async_resolve_and_store_hostname(hass, PEER))
    assert document(hass)["hostname"] == HOST
    assert document(hass)["displayHost"] == f"{HOST} · {PEER}"


def test_schedule_reverse_lookup_is_fire_and_forget(tmp_path) -> None:
    created: list[object] = []

    class _TaskHass(_Hass):
        def async_create_task(self, coro):
            created.append(coro)
            coro.close()
            return None

    hass = _TaskHass(tmp_path)
    schedule_reverse_lookup(hass, PEER)
    assert len(created) == 1
    schedule_reverse_lookup(hass, "")
    assert len(created) == 1


def test_schedule_reverse_lookup_noops_without_task_helper(tmp_path) -> None:
    hass = _Hass(tmp_path)
    schedule_reverse_lookup(hass, PEER)


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
