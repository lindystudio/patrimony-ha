"""0.4.122: no re-invite supersede (TL veto). Member rows carry no device identity
(member clients are keyed m_<memberId>, PQ sessions by memberId, chat senderDevice is
never stored), so duplicates of one principal stay live and are only cosmetic.

An old or unknown token never yields member_token_revoked or unpaired; only a real
revoke does, as before. Names and tokens are fixtures.
"""

from __future__ import annotations

import json

from custom_components.patrimony_collection import fileio
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import member_api as ma
from custom_components.patrimony_collection import members as hm
from tests.test_members import (  # noqa: F401  (autouse fixtures re-exported)
    _Admin, _Hass, _Req, _fake_web, _mint, _pq_unavailable_member_lifecycle, run,
)

ROUTES = [
    lambda h, r: hh.PatrimonyMemberStateView(h).get(r),
    lambda h, r: hh.PatrimonyMemberPairingView(h).get(r),
    lambda h, r: ma.PatrimonyMemberNotesView(h).get(r),
    lambda h, r: ma.PatrimonyMemberNotesView(h).put(r),
    lambda h, r: ma.PatrimonyMemberContactsView(h).get(r),
    lambda h, r: ma.PatrimonyMemberPhotoView(h).get(r),
    lambda h, r: hh.PatrimonyMemberChatView(h).post(r),
]


def _hass(tmp_path):
    fileio.reset_cache()
    (tmp_path / "patrimony_collection").mkdir(exist_ok=True)
    return _Hass(tmp_path)


def _ok(hass, tok):
    return run(hh.PatrimonyMemberStateView(hass).get(_Req(tok))).status == 200


def test_no_supersede_path_exists():
    assert not hasattr(hm, "MIGRATION_SUPERSEDE_DUPES")
    assert hm.MIGRATIONS == (hm.MIGRATION_NO_DEFAULT_EXPIRY, hm.MIGRATION_INACTIVITY_365)
    src = open(hm.__file__).read() + open(hh.__file__).read() + open(ma.__file__).read()
    assert "supersed" not in src.lower()


def test_phone_and_ipad_on_separate_invites_both_stay_live(tmp_path):
    hass = _hass(tmp_path)
    phone = _mint(hass, memberName="Anna phone")[1]
    ipad = _mint(hass, memberName="Anna ipad")[1]
    assert _ok(hass, phone["token"]) and _ok(hass, ipad["token"])  # both first uses
    assert _ok(hass, phone["token"]) and _ok(hass, ipad["token"])
    listed = hm.list_document(hass)
    assert {p["memberId"] for p in listed["members"]} == {phone["member"]["memberId"], ipad["member"]["memberId"]}
    rows = json.loads(hm.members_path(hass).read_text())["members"]
    assert all("supersededAt" not in r for r in rows.values())


def test_reinvite_first_use_leaves_old_row_live_and_store_unchanged(tmp_path):
    hass = _hass(tmp_path)
    old = _mint(hass)[1]
    assert _ok(hass, old["token"])
    new = _mint(hass)[1]
    assert _ok(hass, new["token"])
    assert _ok(hass, old["token"])  # duplicates are cosmetic, nothing ends
    doc = json.loads(hm.members_path(hass).read_text())
    assert doc["migrations"] == [hm.MIGRATION_NO_DEFAULT_EXPIRY, hm.MIGRATION_INACTIVITY_365]
    raw = hm.members_path(hass).read_bytes()
    fileio.reset_cache()
    hm.load_rows(hass)  # second load: no migration write
    assert hm.members_path(hass).read_bytes() == raw


def test_unknown_old_token_never_revoked_or_unpaired(tmp_path):
    hass = _hass(tmp_path)
    gone = _mint(hass)[1]
    assert _ok(hass, gone["token"])
    # The row disappears (e.g. pruned): the old phone's token is simply unknown.
    path = hm.members_path(hass)
    doc = json.loads(path.read_text())
    doc["members"].pop(gone["member"]["memberId"])
    path.write_text(json.dumps(doc))
    fileio.reset_cache()
    for tok in (gone["token"], "pmt_never_minted_fixture"):
        for call in ROUTES:
            r = run(call(hass, _Req(tok, body={})))
            assert r.status == 401
            body = r.json()
            assert body["error"]["code"] == "unauthorized" and body.get("status") != "unpaired"


def test_revoked_for_other_reason_behaves_as_before(tmp_path):
    hass = _hass(tmp_path)
    doc = _mint(hass)[1]
    assert _ok(hass, doc["token"])
    hm.revoke_member(hass, doc["member"]["memberId"])
    r = run(hh.PatrimonyMemberStateView(hass).get(_Req(doc["token"])))
    assert r.status == 401 and r.json()["error"]["code"] == "member_token_revoked"
    p = run(hh.PatrimonyMemberPairingView(hass).get(_Req(doc["token"])))
    assert p.status == 200 and p.json()["status"] == "unpaired" and p.json()["reason"] == "revoked"
