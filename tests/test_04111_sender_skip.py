"""0.4.111: chat pushes say whose message to skip (Backend 9197cfc, APNS.md audit item 7).

Owner chat: excludeDevice + excludeRole "owner". Member /m/chat: excludeDevice +
excludeClient "m_<memberId>" (no excludeRole). Panel (no senderDevice), manual and
attention: no exclude fields. Role/client/principal never go out without a valid
excludeDevice. A valid senderPrincipal is forwarded as excludePrincipal.
"""
from __future__ import annotations

import pytest

from custom_components.patrimony_collection import notify as notify_mod
from custom_components.patrimony_collection import members as members_mod
from custom_components.patrimony_collection.chat import handle_member_post, handle_post

from test_04110_push_hardening import HEK, _Seq, retries  # noqa: F401,E402
from test_attention_push import _Hass as _AttHass, _fact  # noqa: E402
from test_chat import T0, _Entry, _Hass, _ios_request, _panel  # noqa: E402

DEVICE = "d" * 64  # placeholder sha256 hex, not a real token hash
MEMBER_ID = "mem0001"
MEMBER_CID = "m_" + MEMBER_ID
PRINCIPAL = "11111111-2222-4333-8444-555555555555"
TITLE = "New chat message in North House"
EXCLUDE_KEYS = {"excludeDevice", "excludeRole", "excludeClient", "excludePrincipal"}


def _owner(tmp_path, monkeypatch, payload, request=None):
    hass = _Hass(tmp_path, _Entry())
    seq = _Seq(202)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    req = request(hass) if request else _ios_request(hass, device_name="Owner iPhone")
    _, status = handle_post(hass, req, {"text": "hi", **payload}, now=T0)
    assert status == 201
    return hass, seq


def _member(tmp_path, monkeypatch, payload):
    hass = _Hass(tmp_path, _Entry())
    seq = _Seq(202)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    member = {"memberId": MEMBER_ID, "memberName": "Guest"}
    cid = members_mod.client_id_for(MEMBER_ID)
    assert cid == MEMBER_CID
    _, status = handle_member_post(hass, member, cid, {"text": "hi", **payload}, now=T0)
    assert status == 201
    return hass, seq


def test_owner_chat_payload_is_pinned(tmp_path, monkeypatch, retries):
    hass, seq = _owner(tmp_path, monkeypatch, {"senderDevice": DEVICE})
    assert seq.calls == [{
        "cardId": notify_mod.load_card_id(hass),
        "severity": "attention",
        "title": TITLE,
        "kind": "chat",
        "excludeDevice": DEVICE,
        "excludeRole": "owner",
    }]


def test_member_chat_payload_is_pinned(tmp_path, monkeypatch, retries):
    hass, seq = _member(tmp_path, monkeypatch, {"senderDevice": DEVICE})
    assert seq.calls == [{
        "cardId": notify_mod.load_card_id(hass),
        "severity": "attention",
        "title": TITLE,
        "kind": "chat",
        "excludeDevice": DEVICE,
        "excludeClient": MEMBER_CID,
    }]
    assert "excludeRole" not in seq.calls[0]


def test_panel_chat_sends_no_exclude_fields(tmp_path, monkeypatch, retries):
    hass, seq = _owner(tmp_path, monkeypatch, {}, request=lambda _h: _panel())
    assert seq.calls == [{
        "cardId": notify_mod.load_card_id(hass),
        "severity": "attention",
        "title": TITLE,
        "kind": "chat",
    }]


def test_manual_and_attention_send_no_exclude_fields(tmp_path, monkeypatch, retries):
    hass = _Hass(tmp_path, _Entry())
    seq = _Seq(202)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    notify_mod.manual_send(hass, "property-1", HEK, "Push test")
    att = _AttHass(tmp_path / "att")
    notify_mod.dispatch_attention_pushes(att, [_fact()], "p", HEK, now=1000)
    notify_mod.dispatch_attention_pushes(att, [_fact(raw="on", on=True, severity="alert")], "p", HEK, now=2000)
    assert [c["kind"] for c in seq.calls] == ["manual", "attention"]
    for call in seq.calls:
        assert not (set(call) & EXCLUDE_KEYS)
        assert set(call) == {"cardId", "severity", "title", "kind"}


@pytest.mark.parametrize("bad", ["", None, 42, "D" * 64, "d" * 63, "d" * 65, "not-a-hash", "hek_" + "a" * 60])
@pytest.mark.parametrize("path", ["owner", "member"])
def test_invalid_sender_device_sends_no_exclude_fields_at_all(tmp_path, monkeypatch, retries, bad, path):
    payload = {"senderDevice": bad, "senderPrincipal": PRINCIPAL}
    if path == "owner":
        _, seq = _owner(tmp_path, monkeypatch, payload)
    else:
        _, seq = _member(tmp_path, monkeypatch, payload)
    assert len(seq.calls) == 1
    assert not (set(seq.calls[0]) & EXCLUDE_KEYS)


@pytest.mark.parametrize("path", ["owner", "member"])
def test_valid_sender_principal_is_forwarded(tmp_path, monkeypatch, retries, path):
    payload = {"senderDevice": DEVICE, "senderPrincipal": PRINCIPAL}
    if path == "owner":
        _, seq = _owner(tmp_path, monkeypatch, payload)
        role = {"excludeRole": "owner"}
    else:
        _, seq = _member(tmp_path, monkeypatch, payload)
        role = {"excludeClient": MEMBER_CID}
    sent = {k: v for k, v in seq.calls[0].items() if k in EXCLUDE_KEYS}
    assert sent == {"excludeDevice": DEVICE, "excludePrincipal": PRINCIPAL, **role}


@pytest.mark.parametrize("bad", [
    "", None, 7, ["x" * 10], "short", "x" * 65, "has space inside", "bad/slash/chars",
    "semi;colon-12345", "hek_" + "a" * 20, "pmt_" + "a" * 20, "eyJhbGciOiJub25lIn0", "-leading-dash-1",
    "uni\u00e7ode-principal",
])
@pytest.mark.parametrize("path", ["owner", "member"])
def test_invalid_sender_principal_is_dropped(tmp_path, monkeypatch, retries, bad, path):
    payload = {"senderDevice": DEVICE, "senderPrincipal": bad}
    if path == "owner":
        _, seq = _owner(tmp_path, monkeypatch, payload)
    else:
        _, seq = _member(tmp_path, monkeypatch, payload)
    assert "excludePrincipal" not in seq.calls[0]
    assert seq.calls[0]["excludeDevice"] == DEVICE  # the rest still goes out


@pytest.mark.parametrize("path", ["owner", "member"])
def test_background_retries_carry_the_same_exclude_fields(tmp_path, monkeypatch, retries, path):
    hass = _Hass(tmp_path, _Entry())
    seq = _Seq(503, 503, 202)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    payload = {"text": "hi", "senderDevice": DEVICE, "senderPrincipal": PRINCIPAL}
    if path == "owner":
        handle_post(hass, _ios_request(hass, device_name="Owner iPhone"), payload, now=T0)
    else:
        handle_member_post(hass, {"memberId": MEMBER_ID, "memberName": "Guest"}, MEMBER_CID, payload, now=T0)
    retries.run_next()
    retries.run_next()
    assert len(seq.calls) == 3
    assert seq.calls[0] == seq.calls[1] == seq.calls[2]
    assert seq.calls[0]["excludePrincipal"] == PRINCIPAL


def test_sender_exclusion_never_sends_role_client_or_principal_alone():
    assert notify_mod.sender_exclusion(None, "owner", MEMBER_CID, PRINCIPAL) == {}
    assert notify_mod.sender_exclusion("x", "member", MEMBER_CID, PRINCIPAL) == {}
    # Member path with an unusable client id falls back to the role (APNS.md allows either).
    assert notify_mod.sender_exclusion(DEVICE, "member", "rt-ios-1") == {
        "excludeDevice": DEVICE, "excludeRole": "member"}
    assert notify_mod.sender_exclusion(DEVICE, "guest") == {"excludeDevice": DEVICE}
    assert notify_mod.sender_exclusion(DEVICE) == {"excludeDevice": DEVICE}


def test_version_is_at_least_04111():
    from custom_components.patrimony_collection.version import INTEGRATION_VERSION

    assert tuple(int(x) for x in INTEGRATION_VERSION.split(".")) >= (0, 4, 111)
