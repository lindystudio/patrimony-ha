"""0.4.96 member access: scoped opaque token, hashed store, TTL, principal revoke, roles."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import sys
import types

import pytest

from custom_components.patrimony_collection import clients as hc
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import members as hm

URL = "https://ha.example.com"


class _Resp:
    def __init__(self, text="", status=200, content_type=None, charset=None, headers=None):
        self.text, self.status, self.headers = text, status, headers or {}

    def json(self):
        return json.loads(self.text)


@pytest.fixture(autouse=True)
def _fake_web(monkeypatch):
    web = types.SimpleNamespace(
        Response=_Resp,
        json_response=lambda data, status=200, headers=None: _Resp(json.dumps(data), status, headers=headers),
    )
    mod = types.ModuleType("aiohttp")
    mod.web = web
    monkeypatch.setitem(sys.modules, "aiohttp", mod)
    monkeypatch.setitem(sys.modules, "aiohttp.web", web)
    monkeypatch.setattr(hh, "_first_entry", lambda _h: types.SimpleNamespace(data={}, options={}))
    monkeypatch.setattr(hh, "build_presentation_document", lambda *_a: {"schemaVersion": 1, "cards": []})
    monkeypatch.setattr(hc, "property_id", lambda _h: "prop-1")
    monkeypatch.setattr("custom_components.patrimony_collection.pair._cloud_url", lambda _h: None, raising=False)


class _Hass:
    def __init__(self, root, external_url=URL):
        self.data = {}
        self.config = type("c", (), {"path": lambda _s, p, root=root: str(root / p),
                                     "external_url": external_url, "internal_url": None,
                                     "time_zone": "Europe/Lisbon"})()

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class _Req(dict):
    def __init__(self, token=None, user=None, body=None):
        super().__init__()
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        if user is not None:
            self["hass_user"] = user
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _Admin:
    is_admin = True
    name = "Owner"


class _NonAdmin:
    is_admin = False
    name = "Guest"


def run(coro):
    return asyncio.run(coro)


def _owner_jwt(iss):
    body = base64.urlsafe_b64encode(json.dumps({"iss": iss}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + body + ".sig"


def _mint(hass, req_token=None, user=None, **body):
    body.setdefault("memberName", "Anna")
    body.setdefault("principalId", "prn_anna")
    req = _Req(req_token, user=user or _Admin(), body=body)
    r = run(hh.PatrimonyMembersMintView(hass).post(req))
    return r.status, r.json()


def _revoke(hass, principal, req=None):
    r = run(hh.PatrimonyMemberRevokeView(hass).post(req or _Req(user=_Admin()), principal))
    return r.status, r.json()


def test_mint_returns_token_once_and_stores_hash_only(tmp_path):
    hass = _Hass(tmp_path)
    status, doc = _mint(hass, inviteId="inv_123", ttlDays=7)
    assert status == 201
    token = doc["token"]
    assert token.startswith("pmt_") and len(token) >= 40
    assert doc["url"] == URL and doc["public_url"] == URL
    m = doc["member"]
    assert m["role"] == "member" and m["status"] == "pending"
    assert m["inviteId"] == "inv_123" and m["principalId"] == "prn_anna" and m["memberName"] == "Anna"
    path = hm.members_path(hass)
    raw = path.read_text()
    assert token not in raw and token[4:] not in raw
    assert hm.token_hash(token) in raw
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    listed = run(hh.PatrimonyMembersView(hass).get(_Req(user=_Admin()))).json()
    assert token not in json.dumps(listed) and "tokenHash" not in json.dumps(listed)
    assert [p["memberId"] for p in listed["pending"]] == [m["memberId"]]


def test_no_public_url_returns_503_and_mints_nothing(tmp_path):
    hass = _Hass(tmp_path, external_url="http://homeassistant.local:8123")
    status, doc = _mint(hass)
    assert status == 503 and doc["error"]["code"] == "no_public_url"
    assert not hm.members_path(hass).exists()
    status, doc = _mint(_Hass(tmp_path, external_url=None))
    assert status == 503 and doc["error"]["code"] == "no_public_url"


def test_mint_input_validation(tmp_path):
    hass = _Hass(tmp_path)
    assert _mint(hass, memberName="")[1]["error"]["code"] == "invalid_member_name"
    assert _mint(hass, principalId="")[1]["error"]["code"] == "invalid_principal_id"
    assert _mint(hass, principalId=None)[1]["error"]["code"] == "invalid_principal_id"
    assert _mint(hass, ttlDays=0)[1]["error"]["code"] == "invalid_ttl"
    assert _mint(hass, ttlDays=9999)[1]["error"]["code"] == "invalid_ttl"
    assert _mint(hass, inviteId="bad id!")[1]["error"]["code"] == "invalid_invite_id"
    assert run(hh.PatrimonyMembersMintView(hass).post(_Req(user=_Admin(), body=None))).status == 400


def test_member_scope_reads_and_role_fields(tmp_path):
    hass = _Hass(tmp_path)
    token = _mint(hass, inviteId="inv_9")[1]["token"]
    st = run(hh.PatrimonyMemberStateView(hass).get(_Req(token)))
    assert st.status == 200 and st.json()["pairing"]["role"] == "member"
    pr = run(hh.PatrimonyMemberPairingView(hass).get(_Req(token))).json()
    assert pr["status"] == "active" and pr["memberName"] == "Anna" and pr["principalId"] == "prn_anna"
    assert pr["clientId"].startswith("m_")
    ch = run(hh.PatrimonyMemberChatView(hass).get(_Req(token)))
    assert ch.status == 200 and "messages" in ch.json()
    listed = run(hh.PatrimonyMembersView(hass).get(_Req(user=_Admin()))).json()
    assert len(listed["members"]) == 1 and listed["pending"] == []
    row = hc.get_client(hass, pr["clientId"])
    assert (row["role"], row["memberName"], row["inviteId"], row["principalId"]) == (
        "member", "Anna", "inv_9", "prn_anna")
    assert hc.panel_clients_document(hass)["clients"][0]["role"] == "member"


def test_member_views_reject_missing_wrong_and_ha_tokens(tmp_path):
    hass = _Hass(tmp_path)
    _mint(hass)
    for view in (hh.PatrimonyMemberStateView, hh.PatrimonyMemberPairingView, hh.PatrimonyMemberChatView):
        for tok in (None, _owner_jwt("abc"), "pmt_wrong", "hek_x"):
            r = run(view(hass).get(_Req(tok)))
            assert r.status == 401, (view, tok)
            assert r.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_member_views_are_read_only():
    for view in (hh.PatrimonyMemberStateView, hh.PatrimonyMemberPairingView, hh.PatrimonyMemberChatView):
        assert not any(hasattr(view, m) for m in ("post", "put", "delete", "patch"))


def test_only_member_and_claim_views_skip_ha_auth():
    """A pmt_ token is unknown to HA auth, so it can only ever reach requires_auth=False views."""
    from custom_components.patrimony_collection import chat

    views = [v for v in vars(hh).values() if isinstance(v, type) and issubclass(v, hh.HomeAssistantView)
             and v is not hh.HomeAssistantView]
    views += [v for v in vars(chat).values() if isinstance(v, type) and issubclass(v, chat.HomeAssistantView)
              and v is not chat.HomeAssistantView]
    assert {v.__name__ for v in views if not v.requires_auth} == {
        "PatrimonyPairClaimView",
        "PatrimonyMemberStateView",
        "PatrimonyMemberPairingView",
        "PatrimonyMemberChatView",
    }


def test_member_bearer_gets_403_on_owner_apis(tmp_path):
    hass = _Hass(tmp_path)
    token = _mint(hass)[1]["token"]
    for view_req in (
        lambda r: hh.PatrimonyMemberRevokeView(hass).post(r, "prn_anna"),
        lambda r: hh.PatrimonyMembersView(hass).get(r),
        lambda r: hh.PatrimonyMembersMintView(hass).post(r),
        lambda r: hh.PatrimonyClientUnpairView(hass).post(r, "m_x"),
    ):
        req = _Req(token, body={"memberName": "X", "principalId": "p"})
        req[hc.MEMBER_CLIENT_KEY] = "m_x"  # as if a member request reached the view
        assert run(view_req(req)).status == 403
    assert run(hh.PatrimonyMemberStateView(hass).get(_Req(token))).status == 200


def test_ttl_expiry_and_default(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    row, _tok = hm.mint(hass, "Bo", principal_id="prn_bo", now=1_000_000.0)
    assert row["expiresAt"] == hm._iso(1_000_000.0 + 30 * 86400)
    token = _mint(hass, ttlDays=1)[1]["token"]
    real = hm.time.time
    monkeypatch.setattr(hm.time, "time", lambda: real() + 86400 + 5)
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(token)))
    assert r.status == 401 and r.json()["error"]["code"] == "member_token_expired"
    pr = run(hh.PatrimonyMemberPairingView(hass).get(_Req(token))).json()
    assert pr["status"] == "unpaired" and pr["reason"] == "expired"


def test_revoke_principal_all_tokens_and_cancels_pending(tmp_path):
    hass = _Hass(tmp_path)
    a1 = _mint(hass, memberName="Anna phone")[1]
    a2 = _mint(hass, memberName="Anna ipad")[1]
    pend = _mint(hass, memberName="Anna new")[1]
    other = _mint(hass, memberName="Bo", principalId="prn_bo")[1]
    for doc in (a1, a2, other):
        assert run(hh.PatrimonyMemberStateView(hass).get(_Req(doc["token"]))).status == 200
    status, out = _revoke(hass, "prn_anna")
    assert status == 200
    assert out == {"ok": True, "principalId": "prn_anna", "revoked": 2, "cancelledPending": 1}
    for doc in (a1, a2, pend):
        s = run(hh.PatrimonyMemberStateView(hass).get(_Req(doc["token"])))
        assert s.status == 401 and s.json()["error"]["code"] == "member_token_revoked"
        p = run(hh.PatrimonyMemberPairingView(hass).get(_Req(doc["token"]))).json()
        assert p["status"] == "unpaired" and p["reason"] == "revoked"
        assert hc.get_client(hass, doc["member"]["clientId"]) is None
    assert run(hh.PatrimonyMemberStateView(hass).get(_Req(other["token"]))).status == 200
    listed = hm.list_document(hass)
    assert {p["memberId"] for p in listed["inactive"]} == {d["member"]["memberId"] for d in (a1, a2, pend)}
    assert [p for p in listed["inactive"] if p["cancelled"]][0]["memberId"] == pend["member"]["memberId"]


def test_revoke_idempotent_and_unknown(tmp_path):
    hass = _Hass(tmp_path)
    _mint(hass)
    assert _revoke(hass, "prn_anna")[1]["cancelledPending"] == 1
    assert _revoke(hass, "prn_anna") == (200, {"ok": True, "principalId": "prn_anna", "revoked": 0, "cancelledPending": 0})
    assert _revoke(hass, "nobody") == (200, {"ok": True, "principalId": "nobody", "revoked": 0, "cancelledPending": 0})
    assert _revoke(_Hass(tmp_path / "empty"), "x")[0] == 200


def test_owner_pair_bearer_can_revoke_and_unpair_members_only(tmp_path):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, "owner-rt-1", device_name="Study iPhone")
    hc.upsert_client(hass, "owner-rt-2", device_name="Other iPhone")
    owner = _owner_jwt("owner-rt-1")
    doc = _mint(hass, owner, user=_NonAdmin(), memberName="Kid", principalId="prn_kid")[1]
    token = doc["token"]
    cid = run(hh.PatrimonyMemberPairingView(hass).get(_Req(token))).json()["clientId"]
    view = hh.PatrimonyClientUnpairView(hass)
    assert run(view.post(_Req(owner, user=_NonAdmin()), "owner-rt-2")).status == 403
    r = run(view.post(_Req(owner, user=_NonAdmin()), cid))
    assert r.status == 200 and r.json()["status"] == "unpaired"
    assert run(hh.PatrimonyMemberStateView(hass).get(_Req(token))).status == 401
    doc2 = _mint(hass, owner, user=_NonAdmin(), memberName="Kid2", principalId="prn_kid")[1]
    assert _revoke(hass, "prn_kid", _Req(owner, user=_NonAdmin()))[1]["cancelledPending"] == 1
    assert run(hh.PatrimonyMemberStateView(hass).get(_Req(doc2["token"]))).status == 401
    for cid_ in ("owner-rt-1", "owner-rt-2"):
        row = hc.get_client(hass, cid_)
        assert row["status"] == "active" and row["role"] == "owner"
    # a plain non-admin HA user who is not an owner-paired phone is refused
    assert _revoke(hass, "prn_kid", _Req(user=_NonAdmin()))[0] == 403
    assert _mint(hass, user=_NonAdmin())[0] == 403


def test_member_flows_never_touch_ha_auth_or_owner(tmp_path):
    """_drop_existing / HA refresh tokens are not involved in member mint, use or revoke."""
    hass = _Hass(tmp_path)

    class _ExplodingAuth:
        def __getattr__(self, name):
            raise AssertionError(f"member flow touched hass.auth.{name}")

    hass.auth = _ExplodingAuth()
    hc.upsert_client(hass, "owner-rt-1", device_name="Study iPhone")
    doc = _mint(hass)[1]
    run(hh.PatrimonyMemberStateView(hass).get(_Req(doc["token"])))
    _revoke(hass, "prn_anna")
    owner = hc.get_client(hass, "owner-rt-1")
    assert owner["status"] == "active" and owner["role"] == "owner"


def test_owner_pair_mint_keeps_member_tokens(tmp_path):
    from custom_components.patrimony_collection.pair import mint_pairing

    hass = _Hass(tmp_path)
    doc = _mint(hass)[1]
    removed = []

    class _Tok:
        client_name = "Patrimony iOS"
        token_type = None

    class _User:
        id = "u1"
        is_admin = True
        refresh_tokens = {"old": _Tok()}

    class _Auth:
        async def async_get_user(self, uid):
            return _User()

        async def async_remove_refresh_token(self, tok):
            removed.append(tok)

        async def async_create_refresh_token(self, *a, **k):
            return object()

        async def async_create_access_token(self, refresh):
            return "llat-owner"

    hass.auth = _Auth()
    status, _ = run(mint_pairing(hass, _User()))
    assert status == 200 and len(removed) == 1  # only the owner's previous HA pair token
    assert run(hh.PatrimonyMemberStateView(hass).get(_Req(doc["token"]))).status == 200


def test_unreadable_store_not_overwritten(tmp_path):
    hass = _Hass(tmp_path)
    path = hm.members_path(hass)
    path.parent.mkdir(parents=True)
    path.write_text("{torn")
    status, doc = _mint(hass)
    assert status == 503 and doc["error"]["code"] == "store_unavailable"
    assert _revoke(hass, "prn_anna")[0] == 503
    assert path.read_text() == "{torn"


def test_legacy_client_rows_default_owner(tmp_path):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, "legacy-1", device_name="Old iPhone")
    row = hc.get_client(hass, "legacy-1")
    assert row["role"] == "owner" and row["memberName"] is None and row["principalId"] is None


def test_panel_version_has_no_disk_read(monkeypatch):
    from pathlib import Path

    from custom_components.patrimony_collection import panel
    from custom_components.patrimony_collection.version import INTEGRATION_VERSION

    manifest = json.loads((Path(panel.__file__).parent / "manifest.json").read_text())["version"]

    def boom(*_a, **_k):
        raise AssertionError("blocking read in event loop")

    monkeypatch.setattr(Path, "read_text", boom)
    assert panel._component_version() == INTEGRATION_VERSION == manifest
