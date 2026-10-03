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
    PatrimonyChatView,
    chat_path,
    chat_push_title,
    create_message,
    delete_message,
    handle_get,
    handle_post,
    key_path,
    notify_house_chat,
    read_chat_image,
)
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


def _ios_request(hass, device_name=None, user_name="Ada Lovelace"):
    ident = "rt-ios-1"
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
    ios = _ios_request(hass, "Kitchen iPad", user_name="Ada Lovelace")
    own, status = create_message(hass, ios, {"text": SECRET}, now=T0)
    assert status == 201
    assert own["sender"] == "Kitchen iPad"
    assert own["sender"] != "Ada Lovelace"
    other = _ios_request(hass, "Studio iPad", user_name="Ada Lovelace")
    # Last session write changed the stored device. Recreate the first phone's key by
    # a request whose refresh token is iOS but whose label is the other device.
    denied, code = delete_message(hass, other, own["id"], now=T0)
    assert code == 403
    again = _ios_request(hass, "Kitchen iPad", user_name="Ada Lovelace")
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
    assert panel["sender"] == "Ada Lovelace"
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
