"""Paired iOS clients: list, rename, unpair signal, chat display name."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from custom_components.patrimony_collection import clients as clients_mod
from custom_components.patrimony_collection.chat import create_message
from custom_components.patrimony_collection.const import (
    CLIENT_PATH,
    CLIENT_UNPAIR_PATH,
    CLIENTS_PATH,
    PAIRING_PATH,
)
from custom_components.patrimony_collection.http import (
    PatrimonyClientUnpairView,
    PatrimonyClientView,
    PatrimonyClientsView,
    PatrimonyPairingView,
)
from custom_components.patrimony_collection.ios_session import (
    apply_ios_session_payload,
    persist_ios_session,
    store_resolved_hostname,
)

PROPERTY_ID = "00000000-0000-4000-8000-000000000099"
T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


class _User:
    def __init__(self, name="admin", uid="user-1", is_admin=True):
        self.name = name
        self.id = uid
        self.is_admin = is_admin


class _Req(dict):
    def __init__(self, user=None, headers=None):
        super().__init__()
        if user is not None:
            self["hass_user"] = user
        self.headers = headers or {}


class _Tok:
    def __init__(self, ident, client_name="Long-Lived Access Token"):
        self.id = ident
        self.client_name = client_name


class _Entry:
    def __init__(self):
        self.data = {
            "property_id": PROPERTY_ID,
            "display_name": "QA Reconnect Home",
            "location_label": "example",
            "timezone": "Europe/Copenhagen",
        }
        self.options = {"house_event_key": "hek_test_not_real"}
        self.entry_id = "entry-1"


class _Entries:
    def __init__(self, entry):
        self.entry = entry

    def async_entries(self, _domain):
        return [self.entry] if self.entry is not None else []


class _Hass:
    def __init__(self, root: Path, entry=None):
        self.root = root
        self.entry = entry or _Entry()
        self.config = type(
            "C",
            (),
            {
                "path": lambda _self, rel, root=root: str(root / rel),
                "time_zone": "Europe/Copenhagen",
            },
        )()
        self.config_entries = _Entries(self.entry)
        self.auth = type("A", (), {"refresh_tokens": {}})()
        self._revoked = []

        def remove(tok):
            self._revoked.append(getattr(tok, "id", None))

        self.auth.async_remove_refresh_token = remove


def _b64(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _phone(hass, client_id="rt-phone-1", user_name="admin"):
    hass.auth.refresh_tokens[client_id] = _Tok(client_id)
    header = "Bearer " + _b64({"alg": "none"}) + "." + _b64({"iss": client_id}) + "."
    return _Req(_User(user_name), {"Authorization": header})


def test_paths_and_views() -> None:
    assert PAIRING_PATH == "/api/patrimony_collection/pairing"
    assert CLIENTS_PATH == "/api/patrimony_collection/clients"
    assert CLIENT_PATH == "/api/patrimony_collection/clients/{client_id}"
    assert CLIENT_UNPAIR_PATH == "/api/patrimony_collection/clients/{client_id}/unpair"
    assert PatrimonyPairingView.requires_auth is True
    assert PatrimonyClientsView.requires_auth is True
    assert PatrimonyClientView.requires_auth is True
    assert PatrimonyClientUnpairView.requires_auth is True


def test_list_clients_and_rename_rejects_blank(tmp_path) -> None:
    hass = _Hass(tmp_path)
    clients_mod.upsert_client(
        hass,
        "rt-phone-1",
        device_name="Petros iPhone 17 Pro",
        ha_username="admin",
        now=T0,
    )
    doc = clients_mod.panel_clients_document(hass)
    assert len(doc["clients"]) == 1
    assert doc["clients"][0]["displayName"] == "Petros iPhone 17 Pro"
    assert doc["clients"][0]["clientId"] == "rt-phone-1"
    assert "Europe/Copenhagen" in doc["clients"][0]["pairedAtDisplay"]
    assert doc["timezone"] == "Europe/Copenhagen"

    row, err = clients_mod.rename_client(hass, "rt-phone-1", "   ")
    assert err is None
    assert row["displayName"] == "Petros iPhone 17 Pro"
    assert row["nameEdited"] is False

    row, err = clients_mod.rename_client(hass, "rt-phone-1", "  Kitchen Phone  ")
    assert err is None
    assert row["displayName"] == "Kitchen Phone"
    assert row["nameEdited"] is True

    # Reserved seeds never become the stored name via upsert after edit.
    clients_mod.upsert_client(
        hass, "rt-phone-1", device_name="iPhone", ha_username="admin", now=T0
    )
    assert clients_mod.get_client(hass, "rt-phone-1")["displayName"] == "Kitchen Phone"


def test_rename_does_not_become_admin_or_iphone(tmp_path) -> None:
    hass = _Hass(tmp_path)
    clients_mod.upsert_client(
        hass, "rt-a", device_name="iPhone", ha_username="admin", now=T0
    )
    row = clients_mod.get_client(hass, "rt-a")
    assert row["displayName"] == "Paired phone"
    assert row["displayName"].casefold() != "iphone"
    assert row["displayName"].casefold() != "admin"

    clients_mod.upsert_client(
        hass, "rt-a", device_name="Studio iPhone", ha_username="admin", now=T0
    )
    row = clients_mod.get_client(hass, "rt-a")
    assert row["displayName"] == "Studio iPhone"


def test_unpair_sets_reason_and_keeps_house(tmp_path) -> None:
    hass = _Hass(tmp_path)
    clients_mod.upsert_client(
        hass, "rt-phone-1", device_name="Studio iPhone", now=T0
    )
    clients_mod.upsert_client(
        hass, "rt-phone-2", device_name="Guest Phone", now=T0
    )
    before_name = hass.entry.data["display_name"]
    before_tz = hass.entry.data["timezone"]
    before_hek = hass.entry.options["house_event_key"]

    marked = clients_mod.mark_unpaired(hass, "rt-phone-1", now=T0)
    assert marked["status"] == "unpaired"
    listed = clients_mod.panel_clients_document(hass)["clients"]
    assert [c["clientId"] for c in listed] == ["rt-phone-2"]

    phone = _phone(hass, "rt-phone-1")
    body = clients_mod.pairing_document(hass, phone)
    assert body == {
        "status": "unpaired",
        "propertyId": PROPERTY_ID,
        "clientId": "rt-phone-1",
        "reason": "unpaired",
    }
    other = clients_mod.pairing_document(hass, _phone(hass, "rt-phone-2"))
    assert other["status"] == "active"
    assert other["displayName"] == "Guest Phone"
    assert "reason" not in other

    asyncio.run(clients_mod.note_unpair_served(hass, "rt-phone-1"))
    assert "rt-phone-1" in hass._revoked
    assert clients_mod.get_client(hass, "rt-phone-1") is None
    assert clients_mod.get_client(hass, "rt-phone-2") is not None

    assert hass.entry.data["display_name"] == before_name
    assert hass.entry.data["timezone"] == before_tz
    assert hass.entry.options["house_event_key"] == before_hek
    assert before_name == "QA Reconnect Home"
    assert before_tz == "Europe/Copenhagen"


def test_chat_uses_edited_display_name(tmp_path) -> None:
    hass = _Hass(tmp_path)
    clients_mod.upsert_client(
        hass, "rt-phone-1", device_name="Petros iPhone 17 Pro", ha_username="admin", now=T0
    )
    clients_mod.rename_client(hass, "rt-phone-1", "House Phone")
    phone = _phone(hass, "rt-phone-1", user_name="admin")
    # Later POST deviceName must not overwrite the edited name.
    body, status = create_message(
        hass,
        phone,
        {"text": "hello-from-phone", "deviceName": "  Something Else  "},
        now=T0,
    )
    assert status == 201
    assert body["sender"] == "House Phone"
    assert body["senderKind"] == "ios"
    assert body["sender"] != "admin"
    assert body["sender"] != "iPhone"
    assert body["sender"] != "Something Else"
    store = json.loads(
        (tmp_path / "patrimony_collection" / "chat.json").read_text(encoding="utf-8")
    )
    assert store["messages"][0]["senderLabel"] == "House Phone"

    # Panel without deviceName stays HA username.
    panel = _Req(_User("admin"))
    panel_body, status = create_message(
        hass, panel, {"text": "from-panel"}, now=T0
    )
    assert status == 201
    assert panel_body["sender"] == "admin"
    assert panel_body["senderKind"] == "panel"


def test_active_pairing_document(tmp_path) -> None:
    hass = _Hass(tmp_path)
    clients_mod.upsert_client(
        hass, "rt-phone-1", device_name="Studio iPhone", now=T0
    )
    body = clients_mod.pairing_document(hass, _phone(hass, "rt-phone-1"))
    assert body["status"] == "active"
    assert body["propertyId"] == PROPERTY_ID
    assert body["clientId"] == "rt-phone-1"
    assert body["displayName"] == "Studio iPhone"
    assert "reason" not in body


def test_ios_session_registers_non_patrimony_client_name(tmp_path) -> None:
    """example phone bearer is not client_name Patrimony iOS."""
    hass = _Hass(tmp_path)
    persist_ios_session(
        hass, device_name="Legacy Last Write", app_version="1.0", ip="127.0.0.1"
    )
    clients_mod.upsert_client(
        hass,
        "rt-ha-user",
        device_name="Legacy Last Write",
        ha_username="admin",
        now=T0,
    )
    doc = clients_mod.panel_clients_document(hass)
    assert len(doc["clients"]) == 1
    assert doc["clients"][0]["clientId"] == "rt-ha-user"
    assert doc["clients"][0]["displayName"] == "Legacy Last Write"


def test_edited_name_survives_ios_session_and_chat(tmp_path) -> None:
    """A saved Phones name sticks across heartbeat and chat deviceName."""
    hass = _Hass(tmp_path)
    before_name = hass.entry.data["display_name"]
    before_tz = hass.entry.data["timezone"]
    before_hek = hass.entry.options["house_event_key"]
    # Bearer is a long-lived token, not client_name Patrimony iOS.
    phone = _phone(hass, "rt-phone-1", user_name="admin")
    assert hass.auth.refresh_tokens["rt-phone-1"].client_name != "Patrimony iOS"

    clients_mod.upsert_client(
        hass, "rt-phone-1", device_name="iPhone", ha_username="admin", now=T0
    )
    assert clients_mod.get_client(hass, "rt-phone-1")["displayName"] == "Paired phone"
    assert clients_mod.get_client(hass, "rt-phone-1")["nameEdited"] is False

    # Not-yet-edited may upgrade from the fallback once. Never the reverse.
    body, status = apply_ios_session_payload(
        hass,
        {"deviceName": "Studio Handset", "appVersion": "38"},
        "203.0.113.10",
        now=T0,
    )
    assert status == 200
    assert body == {"ok": True}
    clients_mod.upsert_client(
        hass,
        "rt-phone-1",
        device_name="Studio Handset",
        app_version="38",
        ha_username="admin",
        now=T0,
    )
    upgraded = clients_mod.get_client(hass, "rt-phone-1")
    assert upgraded["displayName"] == "Studio Handset"
    assert upgraded["nameEdited"] is False

    row, err = clients_mod.rename_client(hass, "rt-phone-1", "House Phone")
    assert err is None
    assert row["displayName"] == "House Phone"
    assert row["nameEdited"] is True

    body, status = apply_ios_session_payload(
        hass,
        {"deviceName": "Other Handset", "appVersion": "39"},
        "203.0.113.10",
        now=T0,
    )
    assert status == 200
    stored = json.loads(
        (tmp_path / "patrimony_collection" / "ios_session.json").read_text(encoding="utf-8")
    )
    assert stored["deviceName"] == "Other Handset"
    assert stored["clients"][0]["displayName"] == "House Phone"
    assert stored["clients"][0]["nameEdited"] is True
    # Same follow-up the ios_session view does after persist.
    clients_mod.upsert_client(
        hass,
        "rt-phone-1",
        device_name="Other Handset",
        app_version="39",
        ha_username="admin",
        now=T0,
    )
    store_resolved_hostname(hass, "203.0.113.10", "phone.example")
    kept = clients_mod.get_client(hass, "rt-phone-1")
    assert kept["displayName"] == "House Phone"
    assert kept["nameEdited"] is True
    assert kept["deviceName"] == "Other Handset"
    assert kept["appVersion"] == "39"
    stored = json.loads(
        (tmp_path / "patrimony_collection" / "ios_session.json").read_text(encoding="utf-8")
    )
    assert stored["hostname"] == "phone.example"
    assert stored["clients"][0]["displayName"] == "House Phone"
    assert stored["clients"][0]["nameEdited"] is True

    # A reserved deviceName must not walk an edited name back to the fallback.
    apply_ios_session_payload(
        hass,
        {"deviceName": "iPhone", "appVersion": "39"},
        "203.0.113.10",
        now=T0,
    )
    clients_mod.upsert_client(
        hass,
        "rt-phone-1",
        device_name="iPhone",
        app_version="39",
        ha_username="admin",
        now=T0,
    )
    assert clients_mod.get_client(hass, "rt-phone-1")["displayName"] == "House Phone"
    assert clients_mod.get_client(hass, "rt-phone-1")["nameEdited"] is True

    msg, status = create_message(
        hass,
        phone,
        {"text": "after-rename", "deviceName": "Third Name"},
        now=T0,
    )
    assert status == 201
    assert msg["sender"] == "House Phone"
    assert msg["senderKind"] == "ios"
    assert msg["sender"] != "Third Name"
    assert msg["sender"] != "Paired phone"
    assert msg["sender"] != "admin"
    assert msg["sender"] != "iPhone"
    chat = json.loads(
        (tmp_path / "patrimony_collection" / "chat.json").read_text(encoding="utf-8")
    )
    assert chat["messages"][-1]["senderLabel"] == "House Phone"
    assert clients_mod.get_client(hass, "rt-phone-1")["displayName"] == "House Phone"
    assert clients_mod.get_client(hass, "rt-phone-1")["nameEdited"] is True

    panel = _Req(_User("admin"))
    panel_body, status = create_message(hass, panel, {"text": "from-panel"}, now=T0)
    assert status == 201
    assert panel_body["sender"] == "admin"
    assert panel_body["senderKind"] == "panel"

    assert hass.entry.data["display_name"] == before_name
    assert hass.entry.data["timezone"] == before_tz
    assert hass.entry.options["house_event_key"] == before_hek
