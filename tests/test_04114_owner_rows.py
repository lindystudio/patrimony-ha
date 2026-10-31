"""0.4.114: owner client rows are created only for pair-issued tokens.

pair.mint_pairing records the issued refresh-token id in pair_issued.json (ids only) and
creates the row at issue time (as since 0.4.80). The ios_session heartbeat and the first
chat contact may create a row ONLY for an id in that record; for any other bearer (hand-made
LLAT, box tokens) they are a 200 no-op for the row, and member mutations stay 403
llat_not_allowed (0.4.113 gate) with a re-pair hint. Existing rows are never rewritten
except lastAccessAt/appVersion/deviceName as before.
"""
from __future__ import annotations

import ast
import json
import pathlib

from custom_components.patrimony_collection import chat as chat_mod
from custom_components.patrimony_collection import clients as hc
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection.pair import mint_pairing
from tests.test_members import (  # noqa: F401
    _Admin, _Hass, _Req, _fake_web, _mint, _owner_jwt, _pq_unavailable_member_lifecycle, _revoke, run,
)

PKG = pathlib.Path(hh.__file__).parent
HAND = "287a04aabbccddeeff00112233445566"
BOX = "d3e4f5ffeeddccbbaa99887766554433"
LEGACY = "5c1a7e9988776655443322110000aaaa"
BEAT = {"deviceName": "Study iPhone", "appVersion": "1.2.2 (121)", "iosVersion": "26.0"}


class _Tok:
    def __init__(self, i, name="Patrimony iOS", tt="long_lived_access_token"):
        self.id, self.client_name, self.token_type = i, name, tt


class _PairAuth:
    """Enough of hass.auth for mint_pairing + the 0.4.113 gate."""
    def __init__(self, user):
        self.user, self.n, self.removed, self.llats = user, 0, [], set()

    async def async_get_user(self, uid):
        return self.user

    async def async_remove_refresh_token(self, tok):
        self.removed.append(tok.id)
        self.user.refresh_tokens.pop(tok.id, None)

    async def async_create_refresh_token(self, user, **k):
        self.n += 1
        t = _Tok(f"pa1r{self.n:028d}", k.get("client_name"))
        user.refresh_tokens[t.id] = t
        self.llats.add(t.id)
        return t

    async def async_create_access_token(self, refresh):
        return "llat-value-never-printed"

    def async_get_refresh_token(self, tid):
        if tid in self.llats:
            return _Tok(tid)
        return type("T", (), {"token_type": "normal"})()


class _User:
    id = "u1"
    is_admin = True
    name = "Owner"

    def __init__(self):
        self.refresh_tokens = {}


def _hass(tmp_path):
    hass = _Hass(tmp_path)
    user = _User()
    hass.auth = _PairAuth(user)
    return hass, user


def _beat(hass, tid, body=BEAT):
    req = _Req(_owner_jwt(tid), user=_Admin(), body=dict(body))
    req.remote = "192.0.2.7"
    return run(hh.PatrimonyIosSessionView(hass).post(req))


def _clients_raw(hass):
    return json.loads(pathlib.Path(hass.config.path("patrimony_collection/ios_session.json")).read_text())["clients"]


def _pair(hass, user):
    status, body = run(mint_pairing(hass, user))
    assert status == 200 and "llat-value" not in json.dumps(body)
    return max(user.refresh_tokens)  # newest pa1r id


def test_post_upgrade_pair_token_gets_marker_and_row_then_heartbeat_keeps_it(tmp_path):
    hass, user = _hass(tmp_path)
    rid = _pair(hass, user)
    assert hc.is_pair_issued(hass, rid)
    raw = pathlib.Path(hass.config.path(hc.PAIR_ISSUED_FILE)).read_text()
    assert "llat-value" not in raw and rid in raw
    assert hc.get_client(hass, rid)["status"] == "active"
    # App flow: GET /state with the link token, then the heartbeat.
    assert hc.pairing_document(hass, _Req(_owner_jwt(rid), user=_Admin()))["clientId"] == rid
    assert _beat(hass, rid).status == 200
    assert hc.get_client(hass, rid)["appVersion"] == "1.2.2 (121)"
    assert hc.mutation_actor(hass, _Req(_owner_jwt(rid), user=_Admin()))["credentialType"] == "owner_pair"


def test_pair_token_whose_mint_row_failed_is_created_by_first_heartbeat(tmp_path, monkeypatch):
    hass, user = _hass(tmp_path)
    real = hc.upsert_client
    monkeypatch.setattr(hc, "upsert_client", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    rid = _pair(hass, user)
    monkeypatch.setattr(hc, "upsert_client", real)
    assert hc.get_client(hass, rid) is None and hc.is_pair_issued(hass, rid)
    assert _beat(hass, rid).status == 200
    assert hc.get_client(hass, rid)["status"] == "active"


def test_pre_upgrade_pair_token_with_row_keeps_working(tmp_path):
    """Issued by 0.4.80..0.4.113 (e.g. a test kit.s 0.4.112): row created at mint, no marker."""
    hass, _ = _hass(tmp_path)
    hass.auth.llats.add(LEGACY)
    hc.upsert_client(hass, LEGACY)  # what the old mint did
    assert not hc.is_pair_issued(hass, LEGACY)
    assert _beat(hass, LEGACY).status == 200
    row = hc.get_client(hass, LEGACY)
    assert row["status"] == "active" and row["deviceName"] == "Study iPhone"
    assert hc.mutation_actor(hass, _Req(_owner_jwt(LEGACY), user=_Admin()))["credentialType"] == "owner_pair"


def test_pre_080_pair_token_without_row_gets_no_row_and_a_repair_message(tmp_path):
    """Named 'Patrimony iOS' but never recorded: the name is not trusted (hand-made LLATs can
    use it). Heartbeat 200 no-op; member actions 403 with the re-pair hint."""
    hass, _ = _hass(tmp_path)
    hass.auth.llats.add(LEGACY)
    _mint(hass, user=_Admin(), memberName="G", principalId="prn_g")
    assert _beat(hass, LEGACY).status == 200
    assert hc.get_client(hass, LEGACY) is None
    status, out = _revoke(hass, "prn_g", _Req(_owner_jwt(LEGACY), user=_Admin()))
    assert status == 403 and out["error"]["code"] == "llat_not_allowed"
    assert "Re-pair" in out["error"]["message"]


def test_hand_made_llat_heartbeat_and_chat_create_no_row(tmp_path):
    hass, _ = _hass(tmp_path)
    hass.auth.llats.update({HAND, BOX})
    _mint(hass, user=_Admin(), memberName="G", principalId="prn_g")
    for tid in (HAND, BOX):
        assert _beat(hass, tid).status == 200
        req = _Req(_owner_jwt(tid), user=_Admin())
        label = hc.client_sender_label(hass, req, "Kitchen iPhone")
        assert label == {"kind": "ios", "label": "Kitchen iPhone", "key": "ios:" + tid}
        assert hc.get_client(hass, tid) is None
        status, out = _revoke(hass, "prn_g", _Req(_owner_jwt(tid), user=_Admin()))
        assert status == 403 and out["error"]["code"] == "llat_not_allowed"
    assert not pathlib.Path(hass.config.path(hc.PAIR_ISSUED_FILE)).exists()


def test_chat_post_by_hand_made_llat_creates_no_row(tmp_path):
    hass, _ = _hass(tmp_path)
    hass.auth.llats.add(HAND)
    req = _Req(_owner_jwt(HAND), user=_Admin())
    msg, status = chat_mod.create_message(hass, req, {"text": "hi", "deviceName": "Kitchen iPhone"})
    assert status == 201 and msg["sender"] == "Kitchen iPhone"
    assert hc.get_client(hass, HAND) is None


def test_chat_first_contact_of_pair_issued_token_registers(tmp_path):
    hass, user = _hass(tmp_path)
    rid = _pair(hass, user)
    # Row exists from mint; first chat keeps/labels it.
    msg, status = chat_mod.create_message(hass, _Req(_owner_jwt(rid), user=_Admin()),
                                          {"text": "hi", "deviceName": "Study iPhone"})
    assert status == 201 and msg["senderKind"] == "ios"
    assert hc.get_client(hass, rid)["status"] == "active"


def test_repair_after_reinstall_new_link_works_and_old_rows_untouched(tmp_path):
    hass, user = _hass(tmp_path)
    hass.auth.llats.add(LEGACY)
    hc.upsert_client(hass, LEGACY, device_name="Old iPhone")
    before = [r for r in _clients_raw(hass) if r["clientId"] == LEGACY]
    first = _pair(hass, user)
    second = _pair(hass, user)  # reinstall: a new pair link
    assert first in hass.auth.removed  # previous pair token dropped by mint (as before)
    assert hc.is_pair_issued(hass, second)
    assert _beat(hass, second).status == 200
    assert hc.mutation_actor(hass, _Req(_owner_jwt(second), user=_Admin()))["credentialType"] == "owner_pair"
    after = [r for r in _clients_raw(hass) if r["clientId"] == LEGACY]
    assert after == before


def test_existing_rows_byte_identical_after_other_llat_heartbeats(tmp_path):
    hass, _ = _hass(tmp_path)
    hass.auth.llats.update({LEGACY, BOX})
    hc.upsert_client(hass, LEGACY, device_name="Study iPhone")
    before = json.dumps([r for r in _clients_raw(hass)], sort_keys=True)
    for _ in range(3):
        assert _beat(hass, BOX).status == 200
    assert json.dumps([r for r in _clients_raw(hass)], sort_keys=True) == before


def test_unpaired_drop_forgets_the_marker(tmp_path):
    hass, user = _hass(tmp_path)
    rid = _pair(hass, user)
    hc._drop_client_row(hass, rid)
    assert not hc.is_pair_issued(hass, rid)
    assert _beat(hass, rid).status == 200 and hc.get_client(hass, rid) is None


def test_marker_rejects_member_ids_and_junk(tmp_path):
    hass, _ = _hass(tmp_path)
    for bad in ("m_abc123def", "", "x", "a b c d e f", "../../etc"):
        hc.note_pair_issued(hass, bad)
        assert not hc.is_pair_issued(hass, bad)


def test_only_pair_flow_and_member_auth_create_rows():
    """Static: every upsert_client call either passes create=... or is the pair mint /
    pmt_ member auth (m_ ids)."""
    allowed = {("pair.py", "mint_pairing"), ("http.py", "_member_auth")}
    found = set()
    for py in PKG.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for call in [c for c in ast.walk(fn) if isinstance(c, ast.Call)]:
                f = call.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name != "upsert_client" or fn.name == "upsert_client":
                    continue
                if any(k.arg == "create" for k in call.keywords):
                    continue
                found.add((py.name, fn.name))
    # client_sender_label's create path is guarded by is_pair_issued; pairing_document /
    # touch paths only run for an existing row.
    guarded = {("clients.py", "client_sender_label"), ("clients.py", "pairing_document")}
    assert found - guarded == allowed, found
    src = (PKG / "clients.py").read_text()
    csl = src[src.index("def client_sender_label"):]
    csl = csl[: csl.index("\ndef ")]
    assert "is_pair_issued(hass, client_id)" in csl


def test_pair_issued_io_goes_through_loop_safe_fileio():
    src = (PKG / "clients.py").read_text()
    block = src[src.index("def _read_pair_issued"): src.index("def note_pair_issued")]
    assert "read_text()" not in block and ".mkdir(" not in block and "open(" not in block
    assert "fileio.read_text(" in block and "fileio.write_text(" in block
