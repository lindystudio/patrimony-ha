"""House chat: at-rest encryption, delete, expiry, fixed push sentence, sender label."""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from custom_components.patrimony_collection.chat import (
    CHAT_PUSH_GENERIC,
    PatrimonyChatImageView,
    PatrimonyChatMessageView,
    PatrimonyChatReadView,
    PatrimonyChatView,
    chat_path,
    chat_push_title,
    create_message,
    delete_message,
    handle_get,
    handle_post,
    handle_read,
    key_path,
    notify_house_chat,
    read_chat_image,
)
from custom_components.patrimony_collection import clients as clients_mod
from custom_components.patrimony_collection import chat as chat_mod
from custom_components.patrimony_collection.const import PAIR_CLIENT_NAME
from custom_components.patrimony_collection.ios_session import persist_ios_session
from custom_components.patrimony_collection.mapping import build_presentation_document
from custom_components.patrimony_collection import notify as notify_mod

PROPERTY_ID = "550e8400-e29b-41d4-a716-446655440000"
SECRET = "lane-quartz-7719"
T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


class _User:
    def __init__(self, name, uid="user-1"):
        self.name = name
        self.id = uid


class _Req(dict):
    def __init__(self, user=None, headers=None):
        super().__init__()
        if user is not None:
            self["hass_user"] = user
        self.headers = headers or {}


class _Tok:
    def __init__(self, ident, client_name):
        self.id = ident
        self.client_name = client_name


class _Entry:
    def __init__(self, display_name="North House", key="hek_house"):
        self.data = {
            "property_id": PROPERTY_ID,
            "display_name": display_name,
            "location_label": "Example",
            "timezone": "UTC",
        }
        self.options = {"house_event_key": key}
        self.entry_id = "entry-1"


class _Hass:
    def __init__(self, root: Path, entry=None):
        self.root = root
        self.entry = entry
        self.config = type(
            "C",
            (),
            {"path": lambda _self, rel, root=root: str(root / rel)},
        )()
        self.config_entries = _Entries(entry)
        self.auth = type("A", (), {"refresh_tokens": {}})()


class _Entries:
    def __init__(self, entry):
        self.entry = entry

    def async_entries(self, _domain):
        return [self.entry] if self.entry is not None else []


def _b64(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _ios_request(hass, device_name=None, user_name="Ada Lovelace", client_id="rt-ios-1"):
    ident = client_id
    hass.auth.refresh_tokens[ident] = _Tok(ident, PAIR_CLIENT_NAME)
    if device_name:
        persist_ios_session(
            hass,
            device_name=device_name,
            app_version="1.0",
            ip="127.0.0.1",
        )
    header = "Bearer " + _b64({"alg": "none"}) + "." + _b64({"iss": ident}) + "."
    return _Req(_User(user_name, "user-1"), {"Authorization": header})


def _panel(name="Ada Lovelace", uid="user-ada"):
    return _Req(_User(name, uid))


def _raw(hass) -> str:
    return chat_path(hass).read_text(encoding="utf-8")


def test_push_title_is_fixed_sentence() -> None:
    assert chat_push_title("North House") == "New chat message in North House"
    assert chat_push_title("  ") == CHAT_PUSH_GENERIC
    assert chat_push_title(None) == CHAT_PUSH_GENERIC
    assert chat_push_title("prefix hek_nope") == CHAT_PUSH_GENERIC


def test_message_is_encrypted_at_rest_and_list_is_authorized_only(tmp_path) -> None:
    hass = _Hass(tmp_path, _Entry())
    body, status = create_message(hass, _panel(), {"text": SECRET, "retention": "keep"}, now=T0)
    assert status == 201
    assert body["text"] == SECRET
    raw = _raw(hass)
    assert SECRET not in raw
    assert '"text"' not in raw
    store = json.loads(raw)
    row = store["messages"][0]
    blob = base64.b64decode(row["ciphertext"])
    assert SECRET.encode() not in blob
    key = key_path(hass).read_bytes()
    assert len(key) == 32
    assert key not in raw.encode()
    assert SECRET.encode() not in key

    listed, code = handle_get(hass, _panel(), now=T0)
    assert code == 200
    assert listed["messages"][0]["text"] == SECRET

    denied, denied_code = handle_get(hass, _Req(), now=T0)
    assert denied_code == 401
    assert SECRET not in json.dumps(denied)
    assert "messages" not in denied

    doc = build_presentation_document(
        hass,
        dict(hass.entry.data),
        {"cards": [], "mappings": []},
    )
    assert SECRET not in json.dumps(doc)
    assert PatrimonyChatView.requires_auth is True
    assert PatrimonyChatMessageView.requires_auth is True
    assert PatrimonyChatView.url == "/api/patrimony_collection/chat"


def test_delete_removes_the_row(tmp_path) -> None:
    hass = _Hass(tmp_path, _Entry())
    created, status = create_message(hass, _panel("Ada Lovelace", "user-ada"), {"text": SECRET}, now=T0)
    assert status == 201
    ios = _ios_request(hass, "Kitchen iPad")
    denied, code = delete_message(hass, ios, created["id"], now=T0)
    assert code == 403
    assert SECRET not in _raw(hass)
    still, _ = handle_get(hass, _panel(), now=T0)
    assert still["messages"][0]["text"] == SECRET

    other = _panel("Blake House", "user-blake")
    deleted, code = delete_message(hass, other, created["id"], now=T0)
    assert code == 200
    assert deleted["ok"] is True
    listed, _ = handle_get(hass, _panel(), now=T0)
    assert listed["messages"] == []
    assert SECRET not in _raw(hass)
    assert created["id"] not in _raw(hass)


def test_ios_can_delete_own_message_only(tmp_path) -> None:
    hass = _Hass(tmp_path, _Entry())
    ios = _ios_request(hass, "Kitchen iPad", user_name="Ada Lovelace", client_id="rt-ios-kitchen")
    own, status = create_message(hass, ios, {"text": SECRET}, now=T0)
    assert status == 201
    assert own["sender"] == "Kitchen iPad"
    assert own["sender"] != "Ada Lovelace"
    other = _ios_request(hass, "Studio iPad", user_name="Ada Lovelace", client_id="rt-ios-studio")
    denied, code = delete_message(hass, other, own["id"], now=T0)
    assert code == 403
    again = _ios_request(hass, "Kitchen iPad", user_name="Ada Lovelace", client_id="rt-ios-kitchen")
    deleted, code = delete_message(hass, again, own["id"], now=T0)
    assert code == 200
    assert SECRET not in _raw(hass)


def test_expiry_removes_the_message(tmp_path) -> None:
    hass = _Hass(tmp_path, _Entry())
    created, status = create_message(
        hass, _panel(), {"text": SECRET, "retention": "1h"}, now=T0
    )
    assert status == 201
    assert created["retention"] == "1h"
    early, _ = handle_get(hass, _panel(), now=T0 + timedelta(minutes=30))
    assert early["messages"][0]["text"] == SECRET
    late, _ = handle_get(hass, _panel(), now=T0 + timedelta(hours=1))
    assert late["messages"] == []
    assert SECRET not in _raw(hass)
    assert created["id"] not in _raw(hass)

    kept, status = create_message(hass, _panel(), {"text": SECRET, "retention": "keep"}, now=T0)
    assert status == 201
    still, _ = handle_get(hass, _panel(), now=T0 + timedelta(days=30))
    assert still["messages"][0]["id"] == kept["id"]
    assert still["messages"][0]["text"] == SECRET


def test_sender_is_ha_user_or_device_name(tmp_path) -> None:
    hass = _Hass(tmp_path, _Entry())
    persist_ios_session(hass, device_name="Kitchen iPad", app_version="1.0", ip="127.0.0.1")
    panel, status = create_message(hass, _panel("Ada Lovelace"), {"text": "panel-line-441"}, now=T0)
    assert status == 201
    assert panel["sender"] == "HA (Ada Lovelace)"
    assert panel["senderKind"] == "panel"
    assert panel["sender"] != "Kitchen iPad"
    assert panel["sender"] != "North House"

    ios = _ios_request(hass, "Kitchen iPad", user_name="Ada Lovelace")
    phone, status = create_message(hass, ios, {"text": "phone-line-442"}, now=T0)
    assert status == 201
    assert phone["sender"] == "Kitchen iPad"
    assert phone["senderKind"] == "ios"
    assert phone["sender"] != "Ada Lovelace"

    nameless = _Hass(tmp_path / "noname", _Entry())
    bare = _ios_request(nameless, device_name=None, user_name="Ada Lovelace")
    fallback, status = create_message(nameless, bare, {"text": "phone-line-443"}, now=T0)
    assert status == 201
    assert fallback["sender"] == PAIR_CLIENT_NAME
    assert fallback["sender"] != "Ada Lovelace"



def test_ios_post_prefers_payload_device_name(tmp_path) -> None:
    """iOS body deviceName wins; blank on a paired token uses the session name."""
    hass = _Hass(tmp_path, _Entry())
    persist_ios_session(
        hass, device_name="Stored Session Phone", app_version="1.0", ip="127.0.0.1"
    )
    ios = _ios_request(hass, "Stored Session Phone", user_name="Ada Lovelace")
    preferred, status = create_message(
        hass,
        ios,
        {"text": "phone-line-device", "deviceName": "  Petros iPhone 17 Pro  "},
        now=T0,
    )
    assert status == 201
    assert preferred["sender"] == "Petros iPhone 17 Pro"
    assert preferred["senderKind"] == "ios"
    assert preferred["sender"] != "Stored Session Phone"
    assert preferred["sender"] != "Ada Lovelace"
    store = json.loads(_raw(hass))
    assert store["messages"][0]["senderLabel"] == "Petros iPhone 17 Pro"
    assert store["messages"][0]["senderKey"] == "ios:rt-ios-1"

    # First posted deviceName seeded the paired client; later blanks keep it.
    missing, status = create_message(
        hass, ios, {"text": "phone-line-fallback"}, now=T0
    )
    assert status == 201
    assert missing["sender"] == "Petros iPhone 17 Pro"
    assert missing["sender"] != "Ada Lovelace"

    blank, status = create_message(
        hass, ios, {"text": "phone-line-blank", "deviceName": "   "}, now=T0
    )
    assert status == 201
    assert blank["sender"] == "Petros iPhone 17 Pro"

    too_long, status = create_message(
        hass, ios, {"text": "phone-line-long", "deviceName": "x" * 81}, now=T0
    )
    assert status == 201
    assert too_long["sender"] == "Petros iPhone 17 Pro"

    # A posted deviceName is the label even when the caller is the panel
    # user. The panel window itself does not send deviceName.
    posted_panel, status = create_message(
        hass,
        _panel("Ada Lovelace"),
        {"text": "panel-line-with-name", "deviceName": "Petros iPhone 17 Pro"},
        now=T0,
    )
    assert status == 201
    assert posted_panel["sender"] == "Petros iPhone 17 Pro"
    assert posted_panel["senderKind"] == "ios"
    assert posted_panel["sender"] != "Ada Lovelace"
    assert posted_panel["sender"] != "Stored Session Phone"

    plain_panel, status = create_message(
        hass,
        _panel("Ada Lovelace"),
        {"text": "panel-line-no-name"},
        now=T0,
    )
    assert status == 201
    assert plain_panel["sender"] == "HA (Ada Lovelace)"
    assert plain_panel["senderKind"] == "panel"


def test_device_name_wins_when_bearer_is_not_paired_ios(tmp_path) -> None:
    """House phone bearer is an HA user token, not client_name Patrimony iOS.

    Posted deviceName is the sender. A missing name on that token is the
    HA user. A missing name on a known paired iOS call is not the HA user.
    """
    hass = _Hass(tmp_path, _Entry())
    persist_ios_session(
        hass, device_name="Stored Session Phone", app_version="1.0", ip="127.0.0.1"
    )
    ident = "rt-ha-user"
    hass.auth.refresh_tokens[ident] = _Tok(ident, "Long-Lived Access Token")
    header = "Bearer " + _b64({"alg": "none"}) + "." + _b64({"iss": ident}) + "."
    phone = _Req(_User("admin", "user-admin"), {"Authorization": header})
    assert chat_mod.is_ios_client(hass, phone) is False

    named, status = create_message(
        hass,
        phone,
        {"text": "phone-line-named", "deviceName": "  Kitchen iPhone  "},
        now=T0,
    )
    assert status == 201
    assert named["sender"] == "Kitchen iPhone"
    assert named["senderKind"] == "ios"
    assert named["sender"] != "admin"
    store = json.loads(_raw(hass))
    assert store["messages"][0]["senderLabel"] == "Kitchen iPhone"
    assert store["messages"][0]["senderKey"] == "ios:rt-ha-user"

    blank, status = create_message(
        hass,
        phone,
        {"text": "phone-line-blank-user", "deviceName": "   "},
        now=T0,
    )
    assert status == 201
    # Credential is now a registered client; blank keeps the stored display name.
    assert blank["sender"] == "Kitchen iPhone"
    assert blank["senderKind"] == "ios"
    assert blank["sender"] != "admin"

    missing_user, status = create_message(
        hass, phone, {"text": "phone-line-missing-user"}, now=T0
    )
    assert status == 201
    assert missing_user["sender"] == "Kitchen iPhone"
    assert missing_user["sender"] != "admin"

    ios = _ios_request(hass, "Stored Session Phone", user_name="admin")
    assert chat_mod.is_ios_client(hass, ios) is True
    # The session write must keep the other credential's row. This token has
    # no row of its own, so the label is that saved phone, not the HA user.
    missing_ios, status = create_message(
        hass, ios, {"text": "phone-line-missing-ios"}, now=T0
    )
    assert status == 201
    assert missing_ios["sender"] == "Kitchen iPhone"
    assert missing_ios["senderKind"] == "ios"
    assert missing_ios["sender"] != "admin"
    assert missing_ios["sender"] != "Paired phone"



def test_push_payload_is_the_fixed_sentence_not_the_message(tmp_path, monkeypatch, caplog) -> None:
    hass = _Hass(tmp_path, _Entry("North House"))
    captured = {}

    class _Resp:
        status = 202

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _urlopen(req, timeout=8):
        captured["data"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    monkeypatch.setattr(notify_mod, "urlopen", _urlopen)
    caplog.set_level(logging.DEBUG)
    body, status = handle_post(
        hass,
        _panel("Ada Lovelace"),
        {"text": SECRET, "retention": "1d"},
        now=T0,
    )
    assert status == 201
    assert body["text"] == SECRET
    raw = captured["data"].decode()
    payload = json.loads(raw)
    assert payload["title"] == "New chat message in North House"
    assert payload["severity"] == "attention"
    assert set(payload) == {"cardId", "severity", "title"}
    assert SECRET not in raw
    assert "Ada Lovelace" not in raw
    joined = " ".join(captured["headers"].values())
    assert SECRET not in joined
    notify_file = tmp_path / "patrimony_collection" / "notify.json"
    if notify_file.is_file():
        assert SECRET not in notify_file.read_text(encoding="utf-8")
    assert SECRET not in caplog.text
    assert SECRET not in _raw(hass)

    empty = _Hass(tmp_path / "empty-name", _Entry(""))
    captured.clear()
    body, status = handle_post(empty, _panel(), {"text": SECRET}, now=T0)
    assert status == 201
    payload = json.loads(captured["data"].decode())
    assert payload["title"] == "New chat message"
    assert SECRET not in captured["data"].decode()


def test_missing_key_still_stores_without_a_push(tmp_path, monkeypatch) -> None:
    hass = _Hass(tmp_path, _Entry("North House", key=""))
    called = {"n": 0}

    def _urlopen(req, timeout=8):
        called["n"] += 1
        raise AssertionError("push should not be attempted")

    monkeypatch.setattr(notify_mod, "urlopen", _urlopen)
    _body, status = handle_post(hass, _panel(), {"text": SECRET}, now=T0)
    assert status == 201
    assert called["n"] == 0
    assert notify_house_chat(hass) is None


def test_panel_has_chat_window() -> None:
    html = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "patrimony_collection"
        / "www"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert 'data-house-sub="chat"' in html
    assert 'id="soon_chat"' in html
    assert 'id="chat_text"' in html
    assert 'id="chat_retention"' in html
    assert 'id="chat_send"' in html
    assert "/api/patrimony_collection/chat" in html
    assert ">Keep<" in html
    assert ">1 hour<" in html
    assert ">1 day<" in html
    assert ">7 days<" in html
    assert "end-to-end" not in html.lower()
    assert "function chatWhen" in html
    assert "houseMeta.timezone" in html
    assert 'return "UTC"' in html
    assert "homeAssistantTimezone" in html
    assert "chat-time" in html
    assert "margin-left: auto" in html
    assert "chat-del" in html
    assert "hasImage" in html
    assert "/image" in html


PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG = base64.b64decode(PNG_B64)


def _capture_push(monkeypatch):
    captured = []

    class _Resp:
        status = 202

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _urlopen(req, timeout=8):
        captured.append(req.data)
        return _Resp()

    monkeypatch.setattr(notify_mod, "urlopen", _urlopen)

    def _boom(*_args, **_kwargs):
        raise AssertionError("attention debounce must not run for chat")

    monkeypatch.setattr(notify_mod, "plan_automatic_pushes", _boom)
    return captured


def test_second_chat_message_is_not_debounced(tmp_path, monkeypatch) -> None:
    hass = _Hass(tmp_path, _Entry("North House"))
    captured = _capture_push(monkeypatch)
    first, status = handle_post(hass, _panel(), {"text": SECRET}, now=T0)
    assert status == 201
    second, status = handle_post(
        hass, _panel(), {"text": SECRET + "-again"}, now=T0 + timedelta(minutes=1)
    )
    assert status == 201
    assert first["id"] != second["id"]
    assert len(captured) == 2
    for raw in captured:
        payload = json.loads(raw)
        assert payload["title"] == "New chat message in North House"
        assert payload["severity"] == "attention"
        assert set(payload) == {"cardId", "severity", "title"}
        assert SECRET not in raw.decode()
        assert "image" not in payload


def test_image_round_trip_hides_bytes_and_push(tmp_path, monkeypatch) -> None:
    hass = _Hass(tmp_path, _Entry("North House"))
    captured = _capture_push(monkeypatch)
    empty, status = handle_post(hass, _panel(), {}, now=T0)
    assert status == 400
    assert empty["error"]["code"] == "empty"
    prefixed, status = handle_post(
        hass,
        _panel(),
        {"imageBase64": "data:image/png;base64," + PNG_B64, "imageContentType": "image/png"},
        now=T0,
    )
    assert status == 400
    assert prefixed["error"]["code"] == "bad_image"
    bad_type, status = handle_post(
        hass,
        _panel(),
        {"imageBase64": PNG_B64, "imageContentType": "image/gif"},
        now=T0,
    )
    assert status == 400
    monkeypatch.setattr(chat_mod, "CHAT_IMAGE_MAX", 8)
    huge, status = handle_post(
        hass,
        _panel(),
        {"imageBase64": base64.b64encode(b"x" * 9).decode("ascii"), "imageContentType": "image/png"},
        now=T0,
    )
    assert status == 400
    assert huge["error"]["code"] == "too_large"
    monkeypatch.setattr(chat_mod, "CHAT_IMAGE_MAX", 4 * 1024 * 1024)

    created, status = handle_post(
        hass,
        _panel(),
        {"text": "", "imageBase64": PNG_B64, "imageContentType": "image/png", "retention": "1h"},
        now=T0,
    )
    assert status == 201
    assert created["text"] == ""
    assert created["hasImage"] is True
    assert created["imageContentType"] == "image/png"
    assert "imageBase64" not in created
    assert "imageCiphertext" not in created
    assert PNG_B64 not in json.dumps(created)
    raw = _raw(hass)
    assert PNG_B64 not in raw
    assert PNG not in chat_path(hass).read_bytes()
    listed, status = handle_get(hass, _panel(), now=T0)
    assert status == 200
    row = listed["messages"][0]
    assert row["hasImage"] is True
    assert row["imageContentType"] == "image/png"
    assert "imageBase64" not in row
    assert PNG_B64 not in json.dumps(listed)
    blob, status, ctype = read_chat_image(hass, _panel(), created["id"], now=T0)
    assert status == 200
    assert ctype == "image/png"
    assert blob == PNG
    missing, status, _ctype = read_chat_image(hass, _panel(), "no-such", now=T0)
    assert status == 404
    assert len(captured) == 1
    payload = json.loads(captured[0])
    assert set(payload) == {"cardId", "severity", "title"}
    assert PNG_B64 not in captured[0].decode()
    assert SECRET not in captured[0].decode()
    gone, status = delete_message(hass, _panel(), created["id"], now=T0)
    assert status == 200
    after, status, _ctype = read_chat_image(hass, _panel(), created["id"], now=T0)
    assert status == 404
    assert created["id"] not in _raw(hass)
    assert PNG not in chat_path(hass).read_bytes()

    again, status = create_message(
        hass,
        _panel(),
        {"imageBase64": PNG_B64, "imageContentType": "image/jpeg", "retention": "1h"},
        now=T0,
    )
    assert status == 201
    assert again["imageContentType"] == "image/jpeg"
    early, status, ctype = read_chat_image(hass, _panel(), again["id"], now=T0 + timedelta(minutes=30))
    assert status == 200
    assert early == PNG
    late, status, _ctype = read_chat_image(hass, _panel(), again["id"], now=T0 + timedelta(hours=1))
    assert status == 404
    assert again["id"] not in _raw(hass)

    plain, status = create_message(hass, _panel(), {"text": "words-only"}, now=T0)
    assert status == 201
    assert plain["hasImage"] is False
    assert "imageContentType" not in plain
    assert PatrimonyChatImageView.url == "/api/patrimony_collection/chat/{message_id}/image"
    assert PatrimonyChatImageView.requires_auth is True


def test_panel_sender_is_ha_username_and_old_rows_stay(tmp_path) -> None:
    """New panel posts are HA (username). A stored label is not rewritten."""
    hass = _Hass(tmp_path, _Entry())
    created, status = create_message(
        hass, _panel("admin", "user-admin"), {"text": "legacy-panel-line"}, now=T0
    )
    assert status == 201
    store = json.loads(_raw(hass))
    store["messages"][0]["senderLabel"] = "admin"
    store["messages"][0].pop("senderClientId", None)
    chat_path(hass).write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")

    listed, code = handle_get(hass, _panel("admin", "user-admin"), now=T0)
    assert code == 200
    assert listed["messages"][0]["sender"] == "admin"
    assert listed["messages"][0]["senderKind"] == "panel"
    kept = json.loads(_raw(hass))
    assert kept["messages"][0]["senderLabel"] == "admin"
    assert "senderClientId" not in kept["messages"][0]

    fresh, status = create_message(
        hass, _panel("admin", "user-admin"), {"text": "new-panel-line"}, now=T0
    )
    assert status == 201
    assert fresh["sender"] == "HA (admin)"
    assert fresh["senderKind"] == "panel"
    assert fresh["deliveredToOther"] is False
    assert fresh["readByOther"] is False
    after = json.loads(_raw(hass))
    assert after["messages"][0]["senderLabel"] == "admin"
    assert after["messages"][1]["senderLabel"] == "HA (admin)"
    assert "senderClientId" not in after["messages"][1]


def _pair(hass, client_id, display, user_name="admin"):
    clients_mod.upsert_client(
        hass, client_id, device_name=display, ha_username=user_name, now=T0
    )
    clients_mod.rename_client(hass, client_id, display)
    return _ios_request(hass, display, user_name=user_name, client_id=client_id)


def test_ios_sender_stays_saved_display_name(tmp_path) -> None:
    hass = _Hass(tmp_path, _Entry())
    phone = _pair(hass, "rt-saved", "House Phone")
    body, status = create_message(
        hass,
        phone,
        {"text": "from-saved-name", "deviceName": "Paired phone"},
        now=T0,
    )
    assert status == 201
    assert body["sender"] == "House Phone"
    assert body["senderKind"] == "ios"
    assert body["sender"] != "HA (admin)"
    assert body["sender"] != "Paired phone"
    assert body["sender"] != "admin"
    stored = clients_mod.get_client(hass, "rt-saved")
    assert stored["displayName"] == "House Phone"
    assert stored["nameEdited"] is True
    raw = json.loads(_raw(hass))
    assert raw["messages"][0]["senderLabel"] == "House Phone"
    assert raw["messages"][0]["senderClientId"] == "rt-saved"


def test_receipts_are_booleans_and_exclude_sender_and_panel(tmp_path) -> None:
    hass = _Hass(tmp_path, _Entry())
    phone_a = _pair(hass, "rt-a", "Phone A")
    phone_b = _pair(hass, "rt-b", "Phone B")
    panel = _panel("admin", "user-admin")

    first, status = create_message(hass, phone_a, {"text": "from-a"}, now=T0)
    assert status == 201
    assert first["deliveredToOther"] is False
    assert first["readByOther"] is False
    assert isinstance(first["deliveredToOther"], bool)
    assert isinstance(first["readByOther"], bool)

    own, code = handle_get(hass, phone_a, now=T0)
    assert code == 200
    assert own["messages"][0]["deliveredToOther"] is False
    assert own["messages"][0]["readByOther"] is False
    raw = json.loads(_raw(hass))
    assert raw["messages"][0].get("deliveredClientIds") in (None, [])

    panel_list, code = handle_get(hass, panel, now=T0)
    assert code == 200
    assert panel_list["messages"][0]["deliveredToOther"] is False
    raw = json.loads(_raw(hass))
    assert "rt-a" not in raw["messages"][0].get("deliveredClientIds", [])
    assert "panel" not in json.dumps(raw["messages"][0].get("deliveredClientIds", []))

    other, code = handle_get(hass, phone_b, now=T0)
    assert code == 200
    assert other["messages"][0]["deliveredToOther"] is True
    assert other["messages"][0]["readByOther"] is False
    keys = set(other["messages"][0])
    assert "deliveredClientIds" not in keys
    assert "senderClientId" not in keys
    assert "readCursors" not in keys
    blob = json.dumps(other)
    assert "hek_" not in blob
    assert "deliveredClientIds" not in blob

    again, _ = handle_get(hass, phone_a, now=T0)
    assert again["messages"][0]["deliveredToOther"] is True

    # Sender cursor does not count as read by other.
    marked, status = handle_read(
        hass, phone_a, {"lastSeenMessageId": first["id"]}, now=T0
    )
    assert status == 200
    assert marked == {"ok": True}
    after_own, _ = handle_get(hass, phone_b, now=T0)
    assert after_own["messages"][0]["readByOther"] is False

    second, status = create_message(
        hass, phone_a, {"text": "from-a-2"}, now=T0 + timedelta(minutes=1)
    )
    assert status == 201
    assert second["deliveredToOther"] is False
    assert second["readByOther"] is False

    # Phone B's GET delivers the later message. Their cursor is still on the first.
    seen_second, code = handle_get(hass, phone_b, now=T0 + timedelta(minutes=1))
    assert code == 200
    assert seen_second["messages"][1]["deliveredToOther"] is True
    assert seen_second["messages"][1]["readByOther"] is False

    # Cursor at the first message does not cover the later one.
    mid, status = handle_read(
        hass, phone_b, {"lastSeenMessageId": first["id"]}, now=T0
    )
    assert status == 200
    listed, _ = handle_get(hass, phone_a, now=T0)
    by_id = {row["id"]: row for row in listed["messages"]}
    assert by_id[first["id"]]["readByOther"] is True
    assert by_id[second["id"]]["readByOther"] is False
    assert by_id[second["id"]]["deliveredToOther"] is True

    # At or past the later message covers both. Older id does not move back.
    later, status = handle_read(
        hass, phone_b, {"lastSeenMessageId": second["id"]}, now=T0
    )
    assert status == 200
    stale, status = handle_read(
        hass, phone_b, {"lastSeenMessageId": first["id"]}, now=T0
    )
    assert status == 200
    assert stale == {"ok": True}
    listed, _ = handle_get(hass, panel, now=T0)
    by_id = {row["id"]: row for row in listed["messages"]}
    assert by_id[first["id"]]["readByOther"] is True
    assert by_id[second["id"]]["readByOther"] is True
    cursors = json.loads(_raw(hass))["readCursors"]
    assert cursors["rt-b"] == second["id"]

    unknown, status = handle_read(
        hass, phone_b, {"lastSeenMessageId": "not-a-message"}, now=T0
    )
    assert status == 400
    assert unknown["error"]["code"] == "unknown_message"
    missing, status = handle_read(hass, phone_b, {}, now=T0)
    assert status == 400
    assert missing["error"]["code"] == "unknown_message"
    assert json.loads(_raw(hass))["readCursors"]["rt-b"] == second["id"]

    denied, status = handle_read(
        hass, panel, {"lastSeenMessageId": second["id"]}, now=T0
    )
    assert status == 403
    assert json.loads(_raw(hass))["readCursors"]["rt-b"] == second["id"]
    assert "user-admin" not in json.dumps(json.loads(_raw(hass))["readCursors"])

    # The only phone does not flip either flag on its own message.
    solo = _Hass(tmp_path / "solo", _Entry())
    only = _pair(solo, "rt-only", "Only Phone")
    mine, status = create_message(solo, only, {"text": "solo-line"}, now=T0)
    assert status == 201
    seen, _ = handle_get(solo, only, now=T0)
    assert seen["messages"][0]["deliveredToOther"] is False
    cursor, status = handle_read(
        solo, only, {"lastSeenMessageId": mine["id"]}, now=T0
    )
    assert status == 200
    seen, _ = handle_get(solo, only, now=T0)
    assert seen["messages"][0]["deliveredToOther"] is False
    assert seen["messages"][0]["readByOther"] is False

    # Unpaired client does not count.
    clients_mod.mark_unpaired(hass, "rt-b", now=T0)
    listed, _ = handle_get(hass, phone_a, now=T0)
    by_id = {row["id"]: row for row in listed["messages"]}
    assert by_id[first["id"]]["deliveredToOther"] is False
    assert by_id[first["id"]]["readByOther"] is False

    assert PatrimonyChatReadView.url == "/api/patrimony_collection/chat/read"
    assert PatrimonyChatReadView.requires_auth is True


def test_old_row_without_sender_client_id_matches_display_name(tmp_path) -> None:
    hass = _Hass(tmp_path, _Entry())
    phone_a = _pair(hass, "rt-a", "Phone A")
    phone_b = _pair(hass, "rt-b", "Phone B")
    created, status = create_message(hass, phone_a, {"text": "legacy-ios"}, now=T0)
    assert status == 201
    store = json.loads(_raw(hass))
    store["messages"][0]["senderLabel"] = "Phone A"
    store["messages"][0].pop("senderClientId", None)
    # Second row is rewritten to a label no phone currently uses.
    chat_path(hass).write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    panel_msg, status = create_message(
        hass, _panel("admin"), {"text": "legacy-panel"}, now=T0
    )
    assert status == 201
    store = json.loads(_raw(hass))
    assert store["messages"][0]["senderLabel"] == "Phone A"
    store["messages"][1]["senderLabel"] = "Someone Else"
    store["messages"][1].pop("senderClientId", None)
    chat_path(hass).write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")

    # Phone A is the name-matched sender of the first row, so their GET
    # does not deliver it. It does deliver the unmatched panel row.
    listed, code = handle_get(hass, phone_a, now=T0)
    assert code == 200
    assert listed["messages"][0]["sender"] == "Phone A"
    assert listed["messages"][0]["deliveredToOther"] is False
    assert listed["messages"][1]["sender"] == "Someone Else"
    assert listed["messages"][1]["deliveredToOther"] is True
    raw = json.loads(_raw(hass))
    assert raw["messages"][0]["senderLabel"] == "Phone A"
    assert "senderClientId" not in raw["messages"][0]
    assert "rt-a" not in raw["messages"][0].get("deliveredClientIds", [])
    assert "rt-a" in raw["messages"][1]["deliveredClientIds"]

    # Phone A's cursor does not read their name-matched row. It does read the other.
    status_code = handle_read(hass, phone_a, {"lastSeenMessageId": panel_msg["id"]}, now=T0)[1]
    assert status_code == 200
    listed, _ = handle_get(hass, phone_b, now=T0)
    assert listed["messages"][0]["deliveredToOther"] is True
    assert listed["messages"][0]["readByOther"] is False
    assert listed["messages"][1]["readByOther"] is True
    assert listed["messages"][1]["deliveredToOther"] is True
