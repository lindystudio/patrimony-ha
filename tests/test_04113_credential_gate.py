"""0.4.113 member-mutation credential gate (D1), fresh token (D2), real confirm (D3), audit (D4).

Ported from the 0.4.108 security review (scratch tests/test_sec_0413_revoke_credentials.py):
the same credential classes, with the 0.4.113 expectations. hass_user and
hass_refresh_token_id are what HA's auth middleware sets after validating the bearer;
hass.auth.async_get_refresh_token returns the token METADATA (token_type) only.
Classes: a0b1c2/d3e4f5 = admin-owned box LLATs, 6a7b8c/5c1a7e = owner pair bearers,
f5978b = admin browser session, fd7096/c2cbd1 = Guest kiosk LLATs, 14c546 = Guest session.
"""
from __future__ import annotations

import ast
import json
import logging
import pathlib
import re
import time
import types

import pytest

from custom_components.patrimony_collection import clients as hc
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import members as hm
from tests.test_members import (  # noqa: F401  (autouse fixtures re-exported)
    _Admin, _Hass, _NonAdmin, _Req, _fake_web, _mint, _owner_jwt,
    _pq_unavailable_member_lifecycle, _revoke, run,
)

PKG = pathlib.Path(hh.__file__).parent
HTML = (PKG / "www" / "index.html").read_text()
OWNER = "6a7b8caa11bb22cc33dd44ee55ff6677"      # owner pair bearer (refresh-token id = client row)
OWNER2 = "5c1a7e9988776655443322110000aaaa"
BOX = "a0b1c20011223344556677889900aabb"       # admin-owned LLAT, no client row
BOX_CPH = "d3e4f5ffeeddccbbaa99887766554433"
KIOSK = "fd7096aabbccddeeff00112233445566"
BROWSER = "f5978b00112233445566778899aabbcc"
GUEST_SESSION = "14c546aabbccddeeff00112233445566"


def _llat(hass, *ids):
    for i in ids:
        hass.auth.add_llat(i)


def _member(hass, principal="prn_guest"):
    doc = _mint(hass, user=_Admin(), memberName="Guest", principalId=principal)[1]
    cid = run(hh.PatrimonyMemberPairingView(hass).get(_Req(doc["token"]))).json()["clientId"]
    return doc["token"], cid


def _owner_row(hass, cid=OWNER, name="Study iPhone"):
    hc.upsert_client(hass, cid, device_name=name)


def _rows(hass, principal):
    return [r for r in hm.load_rows(hass).values() if r.get("principalId") == principal]


def _unpair(hass, req, cid):
    return run(hh.PatrimonyClientUnpairView(hass).post(req, cid))


def _err(resp):
    return resp.json()["error"]["code"]


# ---------------- D1: classification ----------------

def test_owner_pair_llat_bound_to_active_owner_row_is_owner_pair_even_if_admin(tmp_path):
    hass = _Hass(tmp_path)
    _llat(hass, OWNER)
    _owner_row(hass)
    for user in (_Admin(), _NonAdmin()):
        a = hc.mutation_actor(hass, _Req(_owner_jwt(OWNER), user=user))
        assert a["allowed"] and a["credentialType"] == "owner_pair" and a["credentialIdPrefix"] == OWNER[:6]


def test_hand_made_llat_bound_to_owner_row_is_owner_pair(tmp_path):
    """Keys only on the active owner row bound to the refresh-token id, not on the LLAT's
    name or how it was created (pair flow 'Patrimony iOS' or a hand-made profile LLAT)."""
    hass = _Hass(tmp_path)
    hand = "287a04aabbccddeeff00112233445566"
    _llat(hass, hand)
    _owner_row(hass, hand, "Study iPhone 15")
    assert hc.mutation_actor(hass, _Req(_owner_jwt(hand), user=_Admin()))["credentialType"] == "owner_pair"


def test_owner_pair_needs_binding_refresh_token_id_must_match_bearer(tmp_path):
    hass = _Hass(tmp_path)
    _llat(hass, OWNER, BOX)
    _owner_row(hass)
    # Bearer claims the owner iss but HA validated another refresh token: not bound.
    a = hc.mutation_actor(hass, _Req(_owner_jwt(OWNER), user=_Admin(), rt_id=BOX))
    assert not a["allowed"] and a["credentialType"] != "owner_pair" and a["status"] == 403


def test_admin_llat_without_owner_row_refused_llat_not_allowed(tmp_path):
    hass = _Hass(tmp_path)
    _member(hass)
    _llat(hass, BOX, BOX_CPH)
    for tid in (BOX, BOX_CPH):
        status, out = _revoke(hass, "prn_guest", _Req(_owner_jwt(tid), user=_Admin()))
        assert status == 403 and out["error"]["code"] == "llat_not_allowed"
    assert all(not r.get("revokedAt") for r in _rows(hass, "prn_guest"))


def test_admin_llat_refused_on_every_mutation_route(tmp_path):
    hass = _Hass(tmp_path)
    _, cid = _member(hass)
    _llat(hass, BOX)
    req = lambda body=None: _Req(_owner_jwt(BOX), user=_Admin(), body=body)  # noqa: E731
    assert _err(run(hh.PatrimonyMembersMintView(hass).post(req({"memberName": "X", "principalId": "prn_x"})))) == "llat_not_allowed"
    assert _err(run(hh.PatrimonyMemberRevokeView(hass).post(req(), "prn_guest"))) == "llat_not_allowed"
    assert _err(_unpair(hass, req(), cid)) == "llat_not_allowed"
    assert _err(run(hh.PatrimonyMemberRemoveView(hass).delete(req(), "x"))) == "llat_not_allowed"
    assert _err(run(hh.PatrimonyMembersClearInactiveView(hass).post(req()))) == "llat_not_allowed"


def test_admin_browser_session_allowed(tmp_path):
    hass = _Hass(tmp_path)
    _member(hass)
    status, out = _revoke(hass, "prn_guest", _Req(_owner_jwt(BROWSER), user=_Admin()))
    assert status == 200 and out["revoked"] == 1


def test_user_none_refused_fail_closed(tmp_path):
    hass = _Hass(tmp_path)
    _member(hass)
    r = run(hh.PatrimonyMemberRevokeView(hass).post(_Req(_owner_jwt(BROWSER)), "prn_guest"))
    assert r.status == 403  # (not via _revoke: an empty _Req dict is falsy)
    assert hc.mutation_actor(hass, _Req())["allowed"] is False
    assert all(not r.get("revokedAt") for r in _rows(hass, "prn_guest"))


def test_nonadmin_kiosk_llat_and_guest_session_refused_as_before(tmp_path):
    hass = _Hass(tmp_path)
    _, cid = _member(hass)
    _llat(hass, KIOSK, "c2cbd1aabbccddeeff00112233445566")
    for tid in (KIOSK, "c2cbd1aabbccddeeff00112233445566", GUEST_SESSION):
        assert _revoke(hass, "prn_guest", _Req(_owner_jwt(tid), user=_NonAdmin()))[0] == 403
        assert _unpair(hass, _Req(_owner_jwt(tid), user=_NonAdmin()), cid).status == 403
    assert all(not r.get("revokedAt") for r in _rows(hass, "prn_guest"))


def test_member_pmt_token_refused_as_before(tmp_path):
    hass = _Hass(tmp_path)
    token, cid = _member(hass)
    req = _Req(token); req[hc.MEMBER_CLIENT_KEY] = cid
    assert _revoke(hass, "prn_guest", req)[0] == 403
    req2 = _Req(token); req2[hc.MEMBER_CLIENT_KEY] = cid
    assert _unpair(hass, req2, cid).status == 403
    # Member reads are unchanged.
    assert run(hh.PatrimonyMemberStateView(hass).get(_Req(token))).status == 200


def test_revoked_or_unpaired_owner_row_is_not_owner_pair(tmp_path):
    hass = _Hass(tmp_path)
    _llat(hass, OWNER)
    _owner_row(hass)
    _member(hass)
    hc.mark_unpaired(hass, OWNER)
    assert _revoke(hass, "prn_guest", _Req(_owner_jwt(OWNER), user=_NonAdmin()))[0] == 403
    status, out = _revoke(hass, "prn_guest", _Req(_owner_jwt(OWNER), user=_Admin()))
    assert status == 403 and out["error"]["code"] == "llat_not_allowed"


# ---------------- HARD: the owner app keeps working ----------------

@pytest.mark.parametrize("user", [_Admin, _NonAdmin])
def test_owner_app_can_mint_revoke_and_unpair_members(tmp_path, user):
    hass = _Hass(tmp_path)
    _llat(hass, OWNER)
    _owner_row(hass)
    owner = lambda body=None: _Req(_owner_jwt(OWNER), user=user(), body=body)  # noqa: E731
    r = run(hh.PatrimonyMembersMintView(hass).post(owner({"memberName": "Anna", "principalId": "prn_anna"})))
    assert r.status == 201 and r.json()["clientId"].startswith("m_")
    token = r.json()["token"]
    cid = run(hh.PatrimonyMemberPairingView(hass).get(_Req(token))).json()["clientId"]
    assert _unpair(hass, owner(), cid).status == 200
    r2 = run(hh.PatrimonyMembersMintView(hass).post(owner({"memberName": "Bo", "principalId": "prn_bo"})))
    assert r2.status == 201
    out = run(hh.PatrimonyMemberRevokeView(hass).post(owner(), "prn_bo"))
    assert out.status == 200 and out.json()["cancelledPending"] == 1
    assert run(hh.PatrimonyMembersClearInactiveView(hass).post(owner())).status == 200
    # Owner reads unchanged.
    assert run(hh.PatrimonyMembersView(hass).get(owner())).status == 200


def test_owner_pair_cannot_unpair_other_owner_phone_even_if_admin(tmp_path):
    hass = _Hass(tmp_path)
    _llat(hass, OWNER, OWNER2)
    _owner_row(hass)
    _owner_row(hass, OWNER2, "Other iPhone")
    assert hc.OWNER_PAIR_MEMBER_ONLY_UNPAIR is True
    for user in (_Admin(), _NonAdmin()):
        assert _unpair(hass, _Req(_owner_jwt(OWNER), user=user), OWNER2).status == 403
    assert hc.get_client(hass, OWNER2)["status"] == "active"
    # An admin panel session still can.
    assert _unpair(hass, _Req(_owner_jwt(BROWSER), user=_Admin()), OWNER2).status == 200


def test_variant_switch_restores_0412_owner_unpair(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    _llat(hass, OWNER, OWNER2)
    _owner_row(hass)
    _owner_row(hass, OWNER2, "Other iPhone")
    monkeypatch.setattr(hc, "OWNER_PAIR_MEMBER_ONLY_UNPAIR", False)
    assert _unpair(hass, _Req(_owner_jwt(OWNER), user=_NonAdmin()), OWNER2).status == 403
    assert _unpair(hass, _Req(_owner_jwt(OWNER), user=_Admin()), OWNER2).status == 200


# ---------------- dev option ----------------

def test_dev_option_off_by_default_and_opt_in_allows_admin_llat(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    _member(hass)
    _llat(hass, BOX, KIOSK)
    assert hc._allow_llat_option(hass) is False
    entry = types.SimpleNamespace(data={}, options={})
    monkeypatch.setattr(hc, "_first_entry", lambda _h: entry)
    assert hc.mutation_actor(hass, _Req(_owner_jwt(BOX), user=_Admin()))["code"] == "llat_not_allowed"
    entry.options = {hc.OPT_ALLOW_LLAT_MEMBER_MUTATIONS: "true"}  # only a real True counts
    assert hc.mutation_actor(hass, _Req(_owner_jwt(BOX), user=_Admin()))["allowed"] is False
    entry.options = {hc.OPT_ALLOW_LLAT_MEMBER_MUTATIONS: True}
    a = hc.mutation_actor(hass, _Req(_owner_jwt(BOX), user=_Admin()))
    assert a["allowed"] and a["credentialType"] == "llat_dev"
    # Never for a non-admin LLAT.
    assert hc.mutation_actor(hass, _Req(_owner_jwt(KIOSK), user=_NonAdmin()))["allowed"] is False


def test_dev_option_not_in_defaults_or_options_flow():
    for py in PKG.rglob("*.py"):
        src = py.read_text()
        if py.name != "clients.py":
            assert "allow_llat_member_mutations" not in src, py


# ---------------- D2: expired panel token ----------------

def test_expired_admin_session_refused_no_leeway(tmp_path):
    hass = _Hass(tmp_path)
    _member(hass)
    now = time.time()
    tok = _owner_jwt(BROWSER, exp=int(now) - 1)  # HA would still accept it (10 s leeway)
    status, out = _revoke(hass, "prn_guest", _Req(tok, user=_Admin()))
    assert status == 401 and out["error"]["code"] == "token_expired"
    a = hc.mutation_actor(hass, _Req(_owner_jwt(BROWSER, exp=int(now)), user=_Admin()), now=now)
    assert a["code"] == "token_expired"
    a = hc.mutation_actor(hass, _Req(_owner_jwt(BROWSER, exp=int(now) + 2), user=_Admin()), now=now)
    assert a["allowed"]
    assert all(not r.get("revokedAt") for r in _rows(hass, "prn_guest"))


def test_owner_pair_long_exp_and_old_exp_never_trip_expiry(tmp_path):
    """The owner pair LLAT is checked by its row, not by exp (HA validates its 10-year exp)."""
    hass = _Hass(tmp_path)
    _llat(hass, OWNER)
    _owner_row(hass)
    far = int(time.time()) + 3650 * 86400
    assert hc.mutation_actor(hass, _Req(_owner_jwt(OWNER, exp=far), user=_Admin()))["allowed"]
    assert hc.mutation_actor(hass, _Req(_owner_jwt(OWNER, exp=1), user=_Admin()))["allowed"]


def test_session_without_exp_refused(tmp_path):
    hass = _Hass(tmp_path)
    import base64
    tok = "x." + base64.urlsafe_b64encode(json.dumps({"iss": BROWSER}).encode()).decode().rstrip("=") + ".s"
    assert hc.mutation_actor(hass, _Req(tok, user=_Admin()))["code"] == "token_expired"


def test_panel_refreshes_before_mutation_and_uses_mutate_for_all_member_writes():
    assert "refreshAccessToken" in HTML and "MUTATION_MIN_TOKEN_MS = 60000" in HTML
    fn = HTML[HTML.index("async function ensureFreshToken"):]
    fn = fn[: fn.index("\n    }\n")]
    assert "tokenMsLeft() > MUTATION_MIN_TOKEN_MS" in fn and "auth.refreshAccessToken()" in fn
    for path in ('"/revoke"', '"/unpair"', '"/clear_inactive"', "encodeURIComponent(memberId), { method: \"DELETE\" }"):
        i = HTML.index(path)
        line = HTML[HTML.rfind("\n", 0, i): i]
        assert "await mutate(" in line, path


# ---------------- D3: real confirm ----------------

def test_panel_confirm_is_a_dialog_with_arm_delay_and_double_click_guard():
    assert "CONFIRM_ARM_MS = 600" in HTML and "DOUBLE_CLICK_MS = 400" in HTML
    assert "membersRevokeStep[principalId] = 1;\n        if (confirm)" not in HTML
    assert "Tap Revoke again" not in HTML and "Tap Unpair again" not in HTML
    rv = HTML[HTML.index("async function revokeMember"):]
    rv = rv[: rv.index("\n    }\n")]
    assert "ignoreQuickRepeat(" in rv and "confirmDialog(" in rv and "if (!ok) return;" in rv
    assert rv.index("confirmDialog(") < rv.index("mutate(")
    up = HTML[HTML.index("async function unpairPhone"):]
    up = up[: up.index("\n    }\n")]
    assert "typeToConfirm: isMember ? \"\" : label" in up and up.index("confirmDialog(") < up.index("mutate(")
    dlg = HTML[HTML.index("function confirmDialog"):]
    dlg = dlg[: dlg.index("\n    }\n")]
    assert "ok.disabled = true" in dlg and "setTimeout(function () { armed = true" in dlg
    assert "innerHTML" not in dlg  # names go in as textContent


# ---------------- D4: audit ----------------

def test_audit_records_credential_type_and_rt_prefix(tmp_path, caplog):
    hass = _Hass(tmp_path)
    _llat(hass, OWNER)
    _owner_row(hass)
    _member(hass, "prn_a")
    _member(hass, "prn_b")
    caplog.set_level(logging.INFO)
    _revoke(hass, "prn_a", _Req(_owner_jwt(OWNER), user=_Admin()))
    _revoke(hass, "prn_b", _Req(_owner_jwt(BROWSER), user=_Admin()))
    a = _rows(hass, "prn_a")[0]
    b = _rows(hass, "prn_b")[0]
    assert (a["credentialType"], a["credentialIdPrefix"], a["revokeSource"]) == ("owner_pair", OWNER[:6], "owner_app")
    assert (b["credentialType"], b["credentialIdPrefix"], b["revokeSource"]) == ("admin_session", BROWSER[:6], "panel")
    lines = [r.getMessage() for r in caplog.records if "patrimony_member_revoked" in r.getMessage()]
    assert len(lines) == 2
    assert lines[0].endswith(f"cred=owner_pair rt={OWNER[:6]}") and lines[1].endswith(f"cred=admin_session rt={BROWSER[:6]}")
    for line in lines:
        assert OWNER not in line and BROWSER not in line and "eyJ" not in line
        assert not re.search(r"\d+\.\d+\.\d+\.\d+", line)
    listed = run(hh.PatrimonyMembersView(hass).get(_Req(user=_Admin()))).json()
    past = {r["principalId"]: r for r in listed["inactive"]}
    assert past["prn_a"]["credentialType"] == "owner_pair"


def test_audit_fields_survive_tombstone_and_old_rows_load(tmp_path):
    hass = _Hass(tmp_path)
    _, cid = _member(hass)
    _unpair(hass, _Req(_owner_jwt(BROWSER), user=_Admin()), cid)
    mid = cid[2:]
    run(hh.PatrimonyMemberRemoveView(hass).delete(_Req(user=_Admin()), mid))
    tomb = hm.load_rows(hass)[mid]
    assert tomb["tombstone"] and tomb["credentialType"] == "admin_session" and tomb["credentialIdPrefix"] == BROWSER[:6]
    # 0.4.112 row without the new fields loads as null.
    row, _t = hm.mint(hass, "Old", principal_id="prn_old")
    assert hm.public_row(hm.load_rows(hass)[row["memberId"]])["credentialType"] is None


def test_bad_credential_audit_values_degrade():
    out = hm._clean_audit({"credentialType": "root", "credentialIdPrefix": "eyJhbGciOi"}, "other")
    assert out["credentialType"] == "other" and out["credentialIdPrefix"] is None
    assert hm._clean_audit({"credentialIdPrefix": "10.0.0"}, "other")["credentialIdPrefix"] is None


# ---------------- HARD: reads and box paths unaffected ----------------

def test_box_llat_still_reads_state_members_and_clients(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    _member(hass)
    _llat(hass, BOX_CPH)
    req = lambda: _Req(_owner_jwt(BOX_CPH), user=_Admin())  # noqa: E731
    assert run(hh.PatrimonyMembersView(hass).get(req())).status == 200
    assert run(hh.PatrimonyClientsView(hass).get(req())).status == 200
    monkeypatch.setattr(hh, "build_presentation_document", lambda *_a: {"schemaVersion": 1, "cards": []})
    assert run(hh.PatrimonyStateView(hass).get(req())).status == 200
    # ...but a member mutation with the same token is refused before anything is written.
    status, out = _revoke(hass, "prn_guest", req())
    assert status == 403 and out["error"]["code"] == "llat_not_allowed"


def test_only_the_mutation_views_use_the_gate():
    src = (PKG / "http.py").read_text()
    tree = ast.parse(src)
    users = set()
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)]:
            if any(isinstance(x, ast.Name) and x.id == "_mutation_gate" for x in ast.walk(fn)):
                users.add((cls.name, fn.name))
    assert users == {
        ("PatrimonyMembersMintView", "post"), ("PatrimonyMemberRevokeView", "post"),
        ("PatrimonyClientUnpairView", "post"), ("PatrimonyMemberRemoveView", "delete"),
        ("PatrimonyMembersClearInactiveView", "post"),
        # 0.4.118: member rename and owner self-rename of its client row.
        ("PatrimonyMemberRemoveView", "patch"), ("PatrimonyClientView", "post"),
    }
    ws = (PKG / "websocket.py").read_text()
    assert "mutation_actor" not in ws and "_mutation_gate" not in ws


def test_gate_never_revokes_or_rotates_tokens(tmp_path):
    """Gate reads token metadata only: no async_remove_refresh_token, no create."""
    hass = _Hass(tmp_path)
    calls = []

    class _A:
        def async_get_refresh_token(self, tid):
            calls.append(tid)
            return types.SimpleNamespace(token_type="long_lived_access_token")

        def __getattr__(self, name):
            raise AssertionError("gate touched hass.auth." + name)
    hass.auth = _A()
    _ = hc.mutation_actor(hass, _Req(_owner_jwt(BOX), user=_Admin()))
    assert calls == [BOX]
    src = (PKG / "clients.py").read_text()
    gate = src[src.index("def mutation_actor"): src.index("def may_unpair")]
    assert "async_remove_refresh_token" not in gate and "async_create" not in gate
