"""0.4.122: notes list (items) on the existing notes document, owner + member paths.

Contract: the notes list API. Fixture text only.
Member path runs as CI does (ML-KEM unavailable -> plain bodies); sealing itself is the
unchanged member_api.sealed / open_body path covered by test_member_api.
"""

from __future__ import annotations

import json

import pytest

from custom_components.patrimony_collection import fileio
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection import member_api as ma
from custom_components.patrimony_collection import notes as hn
from tests.test_members import (  # noqa: F401  (autouse fixtures re-exported)
    _Admin, _Hass, _Req, _fake_web, _mint, _pq_unavailable_member_lifecycle, run,
)

PID = "prop-1"


@pytest.fixture
def hass(tmp_path):
    fileio.reset_cache()
    (tmp_path / "patrimony_collection").mkdir(exist_ok=True)
    return _Hass(tmp_path)


def _stored(hass):
    fileio.reset_cache()
    return json.loads(hn.notes_path(hass).read_text())


def _seed(hass, doc):
    hn.notes_path(hass).write_text(json.dumps(doc))
    fileio.reset_cache()


class _HReq(_Req):
    def __init__(self, token=None, user=None, body=None, if_match=None):
        super().__init__(token, user, body)
        if if_match:
            self.headers["If-Match"] = if_match


def owner_get(hass):
    r = run(hh.PatrimonyNotesView(hass).get(_HReq(user=_Admin())))
    assert r.status == 200
    return r.json()


def owner_put(hass, body, if_match=None):
    r = run(hh.PatrimonyNotesView(hass).put(_HReq(user=_Admin(), body=body, if_match=if_match)))
    return r.status, r.json()


def member(hass, principal="prn_guest"):
    status, doc = _mint(hass, memberName="Guest", principalId=principal)
    assert status == 201
    return doc["token"], doc["member"]["memberId"]


def member_get(hass, tok):
    r = run(ma.PatrimonyMemberNotesView(hass).get(_HReq(tok)))
    assert r.status == 200
    return r.json()


def member_put(hass, tok, body, if_match=None):
    r = run(ma.PatrimonyMemberNotesView(hass).put(_HReq(tok, body=body, if_match=if_match)))
    return r.status, r.json()


def items(*pairs, ids=None):
    out = []
    for i, (t, b) in enumerate(pairs):
        out.append({"id": (ids[i] if ids else None), "title": t, "body": b})
    return out


# ------------------------------------------------------------------ capability / shape

def test_get_always_has_items_etag_and_schema(hass):
    doc = owner_get(hass)
    assert doc["schemaVersion"] == 1 and doc["items"] == [] and doc["text"] == ""
    assert doc["updatedAt"] is None and doc["etag"].startswith('"') and doc["textWritable"] is True
    assert not hn.notes_path(hass).exists()  # a GET of a missing file writes nothing


# ------------------------------------------------------------------ migration

def test_migration_text_becomes_one_untitled_item_and_runs_once(hass):
    _seed(hass, {"schemaVersion": 1, "propertyId": PID, "text": "Boiler in the cellar\nKey on hook",
                 "updatedAt": "2026-11-01T10:00:00Z"})
    first = owner_get(hass)
    assert len(first["items"]) == 1
    it = first["items"][0]
    assert it["title"] == "" and it["body"] == "Boiler in the cellar\nKey on hook"
    assert it["order"] == 0 and it["updatedAt"] == "2026-11-01T10:00:00Z" and it["updatedBy"] is None
    assert hn._is_uuid(it["id"])
    assert first["text"] == "Boiler in the cellar\nKey on hook"  # projection == original text
    stored = _stored(hass)
    assert stored["migrations"] == [hn.MIGRATION_ITEMS]
    raw_after_first = hn.notes_path(hass).read_bytes()
    # Second run: no write, same id, same etag.
    second = owner_get(hass)
    assert second["items"][0]["id"] == it["id"] and second["etag"] == first["etag"]
    assert hn.notes_path(hass).read_bytes() == raw_after_first
    # Empty text migrates to [] with the marker and no item.
    _seed(hass, {"schemaVersion": 1, "text": "   ", "updatedAt": None})
    assert owner_get(hass)["items"] == [] and _stored(hass)["migrations"] == [hn.MIGRATION_ITEMS]


# ------------------------------------------------------------------ round trip + ordering

def test_owner_round_trip_ordering_and_projection(hass):
    doc = owner_get(hass)
    st, out = owner_put(hass, {"items": items(("Heating", "Thermostat in hall"), ("", "Bins on Tuesday"),
                                              ("Parking", "Spot 4")), "ifMatch": doc["etag"]})
    assert st == 200, out
    assert [i["order"] for i in out["items"]] == [0, 1, 2]
    assert [i["title"] for i in out["items"]] == ["Heating", "", "Parking"]
    assert all(i["updatedBy"] == {"memberId": None, "kind": "owner"} for i in out["items"])
    assert out["text"] == "Heating\nThermostat in hall\n\nBins on Tuesday\n\nParking\nSpot 4"
    assert out["textWritable"] is False and out["etag"] != doc["etag"]
    assert owner_get(hass) == out
    # Reorder: array order is authoritative, sent `order` ignored, items not bumped.
    ids = [i["id"] for i in out["items"]]
    before = {i["id"]: i["updatedAt"] for i in out["items"]}
    rev = [{"id": i["id"], "title": i["title"], "body": i["body"], "order": 99} for i in reversed(out["items"])]
    st, out2 = owner_put(hass, {"items": rev, "ifMatch": out["etag"]})
    assert st == 200 and [i["id"] for i in out2["items"]] == list(reversed(ids))
    assert [i["order"] for i in out2["items"]] == [0, 1, 2]
    assert {i["id"]: i["updatedAt"] for i in out2["items"]} == before
    assert out2["etag"] != out["etag"]
    # Delete by omission; identical PUT afterwards is a no-op (same etag).
    st, out3 = owner_put(hass, {"items": out2["items"][:1], "ifMatch": out2["etag"]})
    assert st == 200 and [i["id"] for i in out3["items"]] == [ids[2]]
    st, out4 = owner_put(hass, {"items": out3["items"], "ifMatch": out3["etag"]})
    assert st == 200 and out4["etag"] == out3["etag"]
    # A client-chosen UUID for a new item is kept.
    cid = "0b8e7f1e-2c3d-4e5f-8a9b-0c1d2e3f4a5b"
    st, out5 = owner_put(hass, {"items": out4["items"] + [{"id": cid, "title": "Wi-Fi", "body": "x"}],
                                "ifMatch": out4["etag"]})
    assert st == 200 and out5["items"][1]["id"] == cid


# ------------------------------------------------------------------ ifMatch

def test_if_match_owner_path_match_mismatch_missing(hass):
    doc = owner_get(hass)
    st, out = owner_put(hass, {"items": items(("A", "1"))})
    assert st == 400 and out["error"]["code"] == "notes_if_match_required"
    st, out = owner_put(hass, {"items": items(("A", "1")), "ifMatch": None})
    assert st == 400 and out["error"]["code"] == "notes_if_match_required"
    st, ok = owner_put(hass, {"items": items(("A", "1")), "ifMatch": doc["etag"]})
    assert st == 200
    st, out = owner_put(hass, {"items": items(("B", "2")), "ifMatch": doc["etag"]})  # stale
    assert st == 409 and out["error"]["code"] == "notes_conflict"
    assert out["current"] == ok and out["items"] == ok["items"]  # current + spread
    assert owner_get(hass) == ok  # nothing written
    # updatedAt is not an items ifMatch; the If-Match header and W/ / unquoted forms are.
    st, _ = owner_put(hass, {"items": items(("B", "2")), "ifMatch": ok["updatedAt"]})
    assert st == 409
    st, ok2 = owner_put(hass, {"items": items(("B", "2"))}, if_match="W/" + ok["etag"])
    assert st == 200
    st, _ = owner_put(hass, {"items": items(("C", "3")), "ifMatch": ok2["etag"].strip('"')})
    assert st == 200


def test_if_match_member_path_match_mismatch_and_guest_write(hass):
    tok, mid = member(hass)
    doc = member_get(hass, tok)
    assert doc["items"] == [] and "etag" in doc
    st, out = member_put(hass, tok, {"items": items(("Heating", "On"))})
    assert st == 400 and out["error"]["code"] == "notes_if_match_required"
    st, ok = member_put(hass, tok, {"items": items(("Heating", "On")), "ifMatch": doc["etag"]})
    assert st == 200, ok
    assert ok["items"][0]["updatedBy"] == {"memberId": mid, "kind": "member"}  # id, never a name
    st, out = member_put(hass, tok, {"items": items(("Heating", "Off")), "ifMatch": doc["etag"]})
    assert st == 409 and out["error"]["code"] == "notes_conflict" and out["current"]["items"] == ok["items"]
    # The member GET ETag header is also accepted.
    r = run(ma.PatrimonyMemberNotesView(hass).get(_HReq(tok)))
    st, ok2 = member_put(hass, tok, {"items": items(("Heating", "Off"))}, if_match=r.headers["ETag"])
    assert st == 200 and ok2["items"][0]["body"] == "Off"
    # Owner sees the guest's write; write log: who/when, action items, no content.
    assert owner_get(hass)["items"][0]["body"] == "Off"
    log = [(e["scope"], e["action"], e["memberId"]) for e in ma.write_log(hass)]
    assert log == [("notes", "items", mid)] * 2
    assert "Off" not in json.dumps(ma.write_log(hass))


def test_owner_and_member_share_the_same_etag(hass):
    tok, _ = member(hass)
    o = owner_get(hass)
    st, mine = member_put(hass, tok, {"items": items(("X", "1")), "ifMatch": o["etag"]})
    assert st == 200
    st, out = owner_put(hass, {"items": items(("Y", "2")), "ifMatch": o["etag"]})
    assert st == 409 and out["current"]["etag"] == mine["etag"]
    st, _ = owner_put(hass, {"items": items(("Y", "2")), "ifMatch": member_get(hass, tok)["etag"]})
    assert st == 200


# ------------------------------------------------------------------ limits

@pytest.mark.parametrize("body,code,index", [
    ({"items": "nope"}, "notes_bad_items", None),
    ({"items": [5]}, "notes_bad_items", 0),
    ({"items": [{"title": 1, "body": ""}]}, "notes_bad_items", 0),
    ({"items": [{"title": "t" * 81, "body": ""}]}, "notes_title_too_long", 0),
    ({"items": [{"title": "ok", "body": ""}, {"title": "", "body": "b" * 8001}]}, "notes_body_too_long", 1),
    ({"items": [{"title": "", "body": ""}] * 51}, "notes_too_many_items", None),
    ({"items": [{"id": "not-a-uuid", "title": "", "body": ""}]}, "notes_bad_id", 0),
    ({"items": [{"id": "0b8e7f1e-2c3d-4e5f-8a9b-0c1d2e3f4a5b", "title": "", "body": ""}] * 2},
     "notes_duplicate_id", 1),
    ({"items": [], "text": "x"}, "notes_bad_items", None),
])
def test_limits_400_on_both_paths_before_if_match(hass, body, code, index):
    tok, _ = member(hass)
    for put in (lambda b: owner_put(hass, b), lambda b: member_put(hass, tok, b)):
        st, out = put({**body, "ifMatch": '"stale"'})  # stale ifMatch: limits win, never 409
        assert st == 400 and out["error"]["code"] == code
        assert out["error"].get("index") == index
    assert owner_get(hass)["items"] == []


def test_limits_at_the_boundary_are_accepted(hass):
    doc = owner_get(hass)
    full = [{"title": "t" * 80, "body": "b" * 8000}] * 50
    st, out = owner_put(hass, {"items": full, "ifMatch": doc["etag"]})
    assert st == 200 and len(out["items"]) == 50 and len(out["text"]) == 8000  # projection clipped
    st, out = owner_put(hass, {"items": [{"title": "", "body": ""}], "ifMatch": out["etag"]})
    assert st == 200 and out["items"][0]["title"] == "" and out["items"][0]["body"] == ""


# ------------------------------------------------------------------ legacy text PUT

def test_legacy_text_put_follows_single_item_then_409_once_list_used(hass):
    tok, _ = member(hass)
    st, a = owner_put(hass, {"schemaVersion": 1, "text": "Old note"})  # HA panel, no ifMatch
    assert st == 200 and a["textWritable"] is True and len(a["items"]) == 1
    st, b = member_put(hass, tok, {"text": "Old note, edited"})  # 1.2.4 phone, no ifMatch
    assert st == 200 and b["items"][0]["id"] == a["items"][0]["id"]
    assert b["items"][0]["body"] == "Old note, edited" and b["text"] == "Old note, edited"
    st, c = owner_put(hass, {"text": ""})
    assert st == 200 and c["items"] == []
    st, d = owner_put(hass, {"text": "Again"})
    # A list write flips textWritable; legacy text is then refused on both paths.
    st, e = member_put(hass, tok, {"items": d["items"] + items(("Parking", "Spot 4")), "ifMatch": d["etag"]})
    assert st == 200 and e["textWritable"] is False
    raw = hn.notes_path(hass).read_bytes()
    st, out = owner_put(hass, {"text": "clobber"})
    assert st == 409 and out["error"]["code"] == "notes_items_exist" and out["current"]["items"] == e["items"]
    st, out = member_put(hass, tok, {"text": "clobber"})
    assert st == 409 and out["error"]["code"] == "notes_items_exist"
    assert hn.notes_path(hass).read_bytes() == raw  # items and text untouched
    assert [x["action"] for x in ma.write_log(hass)] == ["set", "items"]  # 409 not logged


def test_legacy_text_if_match_still_checked_on_member_and_now_owner(hass):
    st, a = owner_put(hass, {"text": "One"})
    st, out = owner_put(hass, {"text": "Two", "ifMatch": "1999-01-01T00:00:00Z"})
    assert st == 409 and out["error"]["code"] == "notes_conflict"
    st, _ = owner_put(hass, {"text": "Two", "ifMatch": a["updatedAt"]})
    assert st == 200


def test_notes_never_logged(hass, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    _seed(hass, {"schemaVersion": 1, "text": "SECRET-1234", "updatedAt": None})
    doc = owner_get(hass)
    owner_put(hass, {"items": items(("SECRET-TITLE", "SECRET-BODY")), "ifMatch": doc["etag"]})
    assert "SECRET" not in caplog.text
    assert "patrimony_notes_items_migrated created=1" in caplog.text


# ------------------------------------------------------------------ legacy projection after a list edit

@pytest.mark.parametrize("pairs,want", [
    ((("Heating", ""),), "Heating"),                                   # title only
    ((("Heating", ""), ("Parking", "")), "Heating\n\nParking"),        # titles only
    ((("", "Bins on Tuesday"),), "Bins on Tuesday"),                   # body only
    ((("", ""), ("Wi-Fi", "")), "Wi-Fi"),                              # empty item skipped
    ((("Heating", "On"), ("", ""), ("", "Spot 4")), "Heating\nOn\n\nSpot 4"),
])
def test_text_projection_read_only_and_never_empty_with_content(hass, pairs, want):
    tok, _ = member(hass)
    doc = owner_get(hass)
    st, out = owner_put(hass, {"items": items(*pairs), "ifMatch": doc["etag"]})
    assert st == 200
    for got in (owner_get(hass), member_get(hass, tok)):
        assert got["textWritable"] is False
        assert got["text"] == want and got["text"].strip()
    # read-only: a legacy text PUT cannot change it
    st, _ = owner_put(hass, {"text": "other"})
    assert st == 409 and owner_get(hass)["text"] == want


def test_text_projection_empty_only_when_no_item_has_content(hass):
    doc = owner_get(hass)
    st, out = owner_put(hass, {"items": items(("", ""), ("", "")), "ifMatch": doc["etag"]})
    assert st == 200 and out["text"] == "" and len(out["items"]) == 2
    st, out = owner_put(hass, {"items": [], "ifMatch": out["etag"]})
    assert st == 200 and out["text"] == "" and out["items"] == [] and out["textWritable"] is False


# ------------------------------------------------------------------ 0.4.123: stale owner list PUT

def test_69_stale_owner_list_put_after_member_edit_is_409_and_audited(hass, caplog):
    """G1 edits after the owner's GET; the owner's stale list PUT must 409 (body or header),
    a missing/null ifMatch is 400, a text PUT is 409 notes_items_exist. Items untouched.
    Each owner PUT logs one audit line with etag prefixes only."""
    import logging

    caplog.set_level(logging.INFO)
    tok, _ = member(hass)
    e0 = owner_get(hass)["etag"]
    s, d = owner_put(hass, {"items": items(("SECRET-T", "one")), "ifMatch": e0})
    assert s == 200
    e1, iid = d["etag"], d["items"][0]["id"]
    s, d = member_put(hass, tok, {"items": items(("SECRET-T", "G1-newer"), ids=[iid]), "ifMatch": e1})
    assert s == 200
    e2 = d["etag"]
    stale = items(("SECRET-T", "SECRET-stale"), ids=[iid])
    s, d = owner_put(hass, {"items": stale, "ifMatch": e1})
    assert s == 409 and d["error"]["code"] == "notes_conflict" and d["current"]["etag"] == e2
    s, _ = owner_put(hass, {"items": stale}, if_match=e1)
    assert s == 409
    s, d = owner_put(hass, {"items": stale})
    assert s == 400 and d["error"]["code"] == "notes_if_match_required"
    s, d = owner_put(hass, {"items": stale, "ifMatch": None}, if_match=e2)
    assert s == 400  # body key wins over the header, null is missing
    s, d = owner_put(hass, {"text": "SECRET-stale"})
    assert s == 409 and d["error"]["code"] == "notes_items_exist"
    assert owner_get(hass)["items"][0]["body"] == "G1-newer"
    t1, t2 = e1.strip('"')[:8], e2.strip('"')[:8]
    assert f"patrimony_owner_notes_put form=items status=409 code=notes_conflict ifmatch={t1} etag={t2}" in caplog.text
    assert "patrimony_owner_notes_put form=items status=400 code=notes_if_match_required ifmatch=-" in caplog.text
    assert "patrimony_owner_notes_put form=text status=409 code=notes_items_exist ifmatch=-" in caplog.text
    assert "SECRET" not in caplog.text
