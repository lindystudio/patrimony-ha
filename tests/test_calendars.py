"""0.4.100/0.4.101 read-only house calendars: owner + member endpoints, opaque ids, window, PQ, ETag."""

from __future__ import annotations

import asyncio
import json
import logging
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.patrimony_collection import calendars as cal
from custom_components.patrimony_collection import http as hh
from custom_components.patrimony_collection.pq_shield import hybrid as hy
from custom_components.patrimony_collection.pq_shield import member_views as mv
from custom_components.patrimony_collection.pq_shield import session as pqs

from tests.test_members import _Admin, _Hass, _NonAdmin, _Req, _mint, _revoke, _fake_web  # noqa: F401

HOUSE_TZ = ZoneInfo("Europe/Paris")
ok_pq, _ = pqs.probe_mlkem()


def run(c):
    return asyncio.run(c)


class _State:
    def __init__(self, eid, name, **attrs):
        self.entity_id = eid
        self.attributes = {"friendly_name": name, **attrs}


class _States:
    def __init__(self, states):
        self._s = states

    def async_all(self, domain=None):
        return [s for s in self._s if domain is None or s.entity_id.startswith(domain + ".")]


EVENTS = {
    "calendar.rentals": [
        {"start": "2026-10-20", "end": "2026-10-23", "summary": "Visitor (4 adults)",
         "location": "Villa Secret Road 1", "uid": "stay-1"},
    ],
    "calendar.birthdays": [{"start": "2026-10-18", "end": "2026-10-19", "summary": "Anna"}],
    "calendar.irrigation": [
        {"start": "2026-10-17T06:00:00+02:00", "end": "2026-10-17T06:30:00+02:00", "summary": "Zone 1",
         "recurrence_id": "20261017T040000Z", "uid": "rb"},
    ],
    "calendar.hidden": [{"start": "2026-10-17", "end": "2026-10-18", "summary": "secret"}],
}


class _Services:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.slow = set()

    async def async_call(self, domain, service, data, target=None, blocking=False, return_response=False):
        assert (domain, service, blocking, return_response) == ("calendar", "get_events", True, True)
        eid = target["entity_id"][0]
        self.calls.append((eid, data["start_date_time"], data["end_date_time"]))
        if eid in self.fail:
            raise RuntimeError("boom")
        if eid in self.slow:
            await asyncio.sleep(1)
        return {eid: {"events": [dict(e) for e in EVENTS.get(eid, [])]}}


CONFIG = {
    "calendar.rentals": {"expose": True, "sharedWithMembers": True, "role": "stays"},
    "calendar.birthdays": {"expose": True},
    "calendar.irrigation": {"expose": True, "sharedWithMembers": True},
    "calendar.hidden": {"expose": False, "sharedWithMembers": True},
}


@pytest.fixture
def house(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    hass.config.time_zone = "Europe/Paris"
    hass.states = _States([
        _State("calendar.rentals", "Rentals", color="#C9A86A"),
        _State("calendar.birthdays", "Birthdays"),
        _State("calendar.irrigation", "Irrigation"),
        _State("calendar.hidden", "Hidden"),
        _State("sensor.temp", "Temp"),
    ])
    hass.services = _Services()
    (tmp_path / "patrimony_collection").mkdir(exist_ok=True)
    (tmp_path / "patrimony_collection" / "mapping.json").write_text(
        json.dumps({"cards": [{"id": "c1"}], "mappings": [], "calendars": CONFIG}))
    from custom_components.patrimony_collection import fileio
    fileio.reset_cache()
    return hass


def cid(eid):
    return cal.calendar_id("prop-1", eid)


class _Q(_Req):
    def __init__(self, token=None, user=None, query=None, inm=None):
        super().__init__(token, user)
        self.query = query or {}
        if inm:
            self.headers["If-None-Match"] = inm


WIN = {"start": "2026-10-16T00:00:00+02:00", "end": "2026-10-30T00:00:00+01:00"}


def test_list_shape_opaque_ids_only_exposed(house):
    r = run(cal.PatrimonyCalendarsView(house).get(_Q()))
    body = r.json()
    assert r.status == 200 and body["timezone"] == "Europe/Paris"
    names = [c["name"] for c in body["calendars"]]
    assert names == ["Birthdays", "Irrigation", "Rentals"]
    rentals = body["calendars"][2]
    assert rentals == {"id": cid("calendar.rentals"), "calendarId": cid("calendar.rentals"), "name": "Rentals",
                       "color": "#C9A86A", "sharedWithMembers": True, "role": "stays"}
    assert len(rentals["id"]) == 16 and "calendar." not in r.text
    bd = body["calendars"][0]
    assert bd["sharedWithMembers"] is False and bd["role"] is None and "color" not in bd
    assert cal.calendar_id("prop-2", "calendar.rentals") != rentals["id"]


def test_non_owner_forbidden(house):
    assert run(cal.PatrimonyCalendarsView(house).get(_Q(user=_NonAdmin()))).status == 403


def test_events_shape_all_day_timed_sorted(house):
    r = run(cal.PatrimonyCalendarEventsView(house).get(_Q(query=WIN)))
    b = r.json()
    assert r.status == 200 and b["timezone"] == "Europe/Paris"
    assert b["start"] == "2026-10-16T00:00:00+02:00" and b["end"] == "2026-10-30T00:00:00+01:00"
    assert [e["title"] for e in b["events"]] == ["Zone 1", "Anna", "Visitor (4 adults)"]
    rb, anna, stay = b["events"]
    assert rb == {"calendarId": cid("calendar.irrigation"), "uid": "rb", "title": "Zone 1", "start": "2026-10-17T06:00:00+02:00",
                  "end": "2026-10-17T06:30:00+02:00", "allDay": False, "recurrenceId": "20261017T040000Z"}
    assert stay["allDay"] is True and stay["start"] == "2026-10-20" and stay["end"] == "2026-10-23"
    assert stay["location"] == "Villa Secret Road 1" and stay["recurrenceId"] == "stay-1"
    assert anna["recurrenceId"] is None
    assert "secret" not in r.text and "truncated" not in b and "errors" not in b


def test_ids_filter_and_hidden_or_unknown_404(house):
    r = run(cal.PatrimonyCalendarEventsView(house).get(_Q(query={**WIN, "ids": cid("calendar.birthdays")})))
    assert [e["title"] for e in r.json()["events"]] == ["Anna"]
    for bad in (cid("calendar.hidden"), "deadbeefdeadbeef", "calendar.rentals"):
        r = run(cal.PatrimonyCalendarEventsView(house).get(_Q(query={**WIN, "ids": bad})))
        assert r.status == 404 and r.json()["error"]["code"] == "unknown_calendar"


def test_timed_converted_to_house_tz_across_dst(house):
    EVENTS["calendar.birthdays"].append({"start": "2026-10-25T01:30:00Z", "end": "2026-10-25T02:30:00Z", "summary": "DST"})
    try:
        b = run(cal.PatrimonyCalendarEventsView(house).get(_Q(query=WIN))).json()
    finally:
        EVENTS["calendar.birthdays"].pop()
    dst = next(e for e in b["events"] if e["title"] == "DST")
    # 01:30Z is after the 01:00Z switch: CET (+01:00) on the night clocks go back.
    assert dst["start"] == "2026-10-25T02:30:00+01:00" and dst["end"] == "2026-10-25T03:30:00+01:00"
    assert dst["allDay"] is False


def test_days_window_from_start_of_today_dst_safe():
    now = datetime(2026, 10, 20, 22, 30, tzinfo=timezone.utc)  # 21 Oct 00:30 in Paris
    s, e = cal.parse_window({"days": "14"}, HOUSE_TZ, now)
    assert s.isoformat() == "2026-10-21T00:00:00+02:00" and e.isoformat() == "2026-11-04T00:00:00+01:00"
    s, e = cal.parse_window({}, HOUSE_TZ, now)
    assert (e.date() - s.date()).days == 14
    assert cal.parse_window({"days": "92"}, HOUSE_TZ, now)
    for q, code in (({"days": "93"}, "window_too_large"), ({"days": "0"}, "bad_range"), ({"days": "x"}, "bad_range"),
                    ({"start": "2026-11-01", "end": "2026-10-01"}, "bad_range"), ({"start": "nope"}, "bad_range"),
                    ({"start": "2026-01-01", "end": "2026-05-01"}, "window_too_large"),
                    ({"days": "3", "start": "2026-01-01"}, "bad_range")):
        with pytest.raises(cal.WindowError) as ei:
            cal.parse_window(q, HOUSE_TZ, now)
        assert ei.value.code == code
    s, e = cal.parse_window({"start": "2026-10-01"}, HOUSE_TZ, now)
    assert (e - s).days == 30
    s, _ = cal.parse_window({"start": "2026-10-01T10:00:00 02:00", "end": "2026-10-02"}, HOUSE_TZ, now)
    assert s.utcoffset().total_seconds() == 7200


def test_window_errors_http(house):
    v = cal.PatrimonyCalendarEventsView(house)
    r = run(v.get(_Q(query={"days": "200"})))
    assert r.status == 400 and r.json()["error"]["code"] == "window_too_large"
    r = run(v.get(_Q(query={"start": "2026-10-10", "end": "2026-10-09"})))
    assert r.status == 400 and r.json()["error"]["code"] == "bad_range"
    assert not house.services.calls


def test_partial_errors_and_timeout(house, monkeypatch):
    monkeypatch.setattr(cal, "FETCH_TIMEOUT_S", 0.05)
    house.services.fail.add("calendar.birthdays")
    house.services.slow.add("calendar.irrigation")
    b = run(cal.PatrimonyCalendarEventsView(house).get(_Q(query=WIN))).json()
    assert [e["title"] for e in b["events"]] == ["Visitor (4 adults)"]
    assert sorted(b["errors"], key=lambda x: x["code"]) == sorted(
        [{"calendarId": cid("calendar.birthdays"), "code": "fetch_failed"},
         {"calendarId": cid("calendar.irrigation"), "code": "timeout"}], key=lambda x: x["code"])


def test_cache_60s(house, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cal.time, "monotonic", lambda: clock[0])
    v = cal.PatrimonyCalendarEventsView(house)
    run(v.get(_Q(query=WIN)))
    n = len(house.services.calls)
    run(v.get(_Q(query=WIN)))
    assert len(house.services.calls) == n == 3
    clock[0] += 61
    run(v.get(_Q(query=WIN)))
    assert len(house.services.calls) == 6
    cal.save_config(house, CONFIG)  # config save clears the cache
    run(v.get(_Q(query=WIN)))
    assert len(house.services.calls) == 9


def test_500_cap_truncated(house):
    many = [{"start": f"2026-10-{17 + i // 100:02d}T{(i % 100) // 10:02d}:{i % 10:02d}:00+02:00",
             "end": f"2026-10-{17 + i // 100:02d}T{(i % 100) // 10:02d}:{i % 10:02d}:30+02:00", "summary": f"e{i}"}
            for i in range(600)]
    EVENTS["calendar.irrigation"], saved = many, EVENTS["calendar.irrigation"]
    try:
        b = run(cal.PatrimonyCalendarEventsView(house).get(_Q(query=WIN))).json()
    finally:
        EVENTS["calendar.irrigation"] = saved
    assert len(b["events"]) == 500 and b["truncated"] is True
    starts = [e["start"] for e in b["events"]]
    assert starts == sorted(starts)


def test_etag_and_304_owner(house):
    for view, q in ((cal.PatrimonyCalendarsView(house), {}), (cal.PatrimonyCalendarEventsView(house), WIN)):
        r = run(view.get(_Q(query=q)))
        tag = r.headers["ETag"]
        assert tag.startswith('"') and tag == cal.etag_for(r.json())
        r2 = run(view.get(_Q(query=q, inm=tag)))
        assert r2.status == 304 and r2.text == "" and r2.headers["ETag"] == tag
        assert run(view.get(_Q(query=q, inm='"other"'))).status == 200


def test_titles_never_logged(house, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    house.services.fail.add("calendar.rentals")
    run(cal.PatrimonyCalendarEventsView(house).get(_Q(query=WIN)))
    run(cal.PatrimonyCalendarsView(house).get(_Q()))
    text = caplog.text
    for secret in ("Visitor", "Anna", "Zone 1", "Villa Secret", "Rentals"):
        assert secret not in text


def test_panel_editor_roundtrip_keeps_mapping(house):
    v = cal.PatrimonyEditorCalendarsView(house)
    doc = run(v.get(_Q(user=_Admin()))).json()
    rows = {r["entity_id"]: r for r in doc["calendars"]}
    assert set(rows) == {"calendar.rentals", "calendar.birthdays", "calendar.irrigation", "calendar.hidden"}
    assert doc["roles"] == ["stays", "birthdays"]
    req = _Q(user=_Admin())
    req._body = {"calendars": [{"entity_id": "calendar.birthdays", "expose": True, "sharedWithMembers": True,
                                "role": "bogus"}, {"entity_id": "sensor.x", "expose": True}]}
    out = run(v.post(req)).json()
    assert out["ok"] is True
    disk = json.loads((house.config.path("patrimony_collection/mapping.json") and
                       open(house.config.path("patrimony_collection/mapping.json")).read()))
    assert disk["cards"] == [{"id": "c1"}]
    assert disk["calendars"] == {"calendar.birthdays": {"expose": True, "sharedWithMembers": True, "role": None}}
    assert run(v.get(_Q(user=_NonAdmin()))).status in (401, 403)


def test_defaults_off_for_unconfigured(house):
    cal.save_config(house, {})
    assert run(cal.PatrimonyCalendarsView(house).get(_Q())).json()["calendars"] == []
    assert cal._norm_row({}) == {"expose": False, "sharedWithMembers": False, "role": None}


# ------------------------------------------------------------------ members

@pytest.fixture
def mhouse(house, monkeypatch):
    monkeypatch.setattr(mv, "_require_configured", lambda _h: None)
    return house


def _plain_member(monkeypatch):
    monkeypatch.setattr(pqs.PQShieldManager, "feature_enabled", property(lambda self: False))


def test_member_list_filtered_plain_when_shield_off(mhouse, monkeypatch):
    _plain_member(monkeypatch)
    run(pqs.async_setup_pq_shield(mhouse))
    tok = _mint(mhouse)[1]["token"]
    b = run(cal.PatrimonyMemberCalendarsView(mhouse).get(_Q(tok))).json()
    assert [c["name"] for c in b["calendars"]] == ["Irrigation", "Rentals"]  # Birthdays not shared


def test_member_events_no_location_stays_guests_and_birthdays_404(mhouse, monkeypatch):
    _plain_member(monkeypatch)
    run(pqs.async_setup_pq_shield(mhouse))
    tok = _mint(mhouse)[1]["token"]
    v = cal.PatrimonyMemberCalendarEventsView(mhouse)
    r = run(v.get(_Q(tok, query=WIN)))
    b = r.json()
    assert [e["title"] for e in b["events"]] == ["Zone 1", "Guests"]
    assert all("location" not in e for e in b["events"])
    assert "Visitor" not in r.text and "Villa" not in r.text and "Anna" not in r.text
    r = run(v.get(_Q(tok, query={**WIN, "ids": cid("calendar.birthdays")})))
    assert r.status == 404
    tag = run(v.get(_Q(tok, query=WIN))).headers["ETag"]
    assert run(v.get(_Q(tok, query=WIN, inm=tag))).status == 304


def test_member_default_none(mhouse, monkeypatch):
    _plain_member(monkeypatch)
    run(pqs.async_setup_pq_shield(mhouse))
    cal.save_config(mhouse, {k: {"expose": True} for k in CONFIG})
    tok = _mint(mhouse)[1]["token"]
    assert run(cal.PatrimonyMemberCalendarsView(mhouse).get(_Q(tok))).json()["calendars"] == []
    assert run(cal.PatrimonyMemberCalendarEventsView(mhouse).get(_Q(tok, query=WIN))).json()["events"] == []


def test_member_revoked_401_and_no_token_401(mhouse, monkeypatch):
    _plain_member(monkeypatch)
    run(pqs.async_setup_pq_shield(mhouse))
    tok = _mint(mhouse)[1]["token"]
    assert _revoke(mhouse, "prn_anna")[0] == 200
    for v in (cal.PatrimonyMemberCalendarsView(mhouse), cal.PatrimonyMemberCalendarEventsView(mhouse)):
        r = run(v.get(_Q(tok, query=WIN)))
        assert r.status == 401 and r.json()["error"]["code"] == "member_token_revoked"
        assert run(v.get(_Q(None, query=WIN))).status == 401


@pytest.mark.skipif(not ok_pq, reason="ML-KEM unavailable")
def test_member_pq_428_then_wrapped_and_etag_on_plaintext(mhouse, monkeypatch):
    monkeypatch.setattr(pqs.PQShieldManager, "feature_enabled", property(lambda self: True))
    run(pqs.async_setup_pq_shield(mhouse))
    tok = _mint(mhouse)[1]["token"]
    lv, ev = cal.PatrimonyMemberCalendarsView(mhouse), cal.PatrimonyMemberCalendarEventsView(mhouse)
    r = run(ev.get(_Q(tok, query=WIN)))
    assert r.status == 428 and r.json()["error"]["code"] == "pq_session_required"
    assert run(lv.get(_Q(tok))).status == 428
    # A matching If-None-Match must not bypass the PQ requirement.
    plain_tag = cal.etag_for(run(cal.build_list(mhouse, member=True)))
    assert run(lv.get(_Q(tok, inm=plain_tag))).status == 428
    from tests.test_member_pq import _handshake
    key = _handshake(mhouse, tok)
    r = run(ev.get(_Q(tok, query=WIN)))
    env = r.json()
    assert r.status == 200 and {"kem", "aead", "nonce", "ct"} <= set(env) and "events" not in env
    doc = hy.open_document(key, env)
    assert [e["title"] for e in doc["events"]] == ["Zone 1", "Guests"]
    assert r.headers["ETag"] == cal.etag_for(doc)
    r304 = run(ev.get(_Q(tok, query=WIN, inm=r.headers["ETag"])))
    assert r304.status == 304 and r304.text == ""
    lst = run(lv.get(_Q(tok)))
    assert hy.open_document(key, lst.json())["calendars"][0]["name"] == "Irrigation"
    assert lst.headers["ETag"] == plain_tag


# ------------------------------------------------------------------ 0.4.101

def test_uid_ha_uid_else_keyed_hash_16_hex(house):
    b = run(cal.PatrimonyCalendarEventsView(house).get(_Q(query=WIN))).json()
    rb, anna, stay = b["events"]
    assert rb["uid"] == "rb" and stay["uid"] == "stay-1"
    assert rb["recurrenceId"] == "20261017T040000Z" and stay["recurrenceId"] == "stay-1"  # unchanged
    assert len(anna["uid"]) == 16 and all(ch in "0123456789abcdef" for ch in anna["uid"])
    # Stable across requests and cache expiry.
    cal.cache_clear(house)
    again = run(cal.PatrimonyCalendarEventsView(house).get(_Q(query=WIN))).json()["events"]
    assert [e["uid"] for e in again] == [e["uid"] for e in b["events"]]
    # Keyed: not a plain sha256 a member could brute-force from a guessed title.
    import hashlib
    plain = hashlib.sha256(f"{cid('calendar.birthdays')}||2026-10-18|Anna".encode()).hexdigest()[:16]
    assert anna["uid"] != plain
    key = run(cal.async_uid_key(house))
    assert anna["uid"] == cal.event_uid(key, cid("calendar.birthdays"), None, "2026-10-18", "Anna")


def test_uid_distinct_per_event_and_title_sensitive():
    k = b"k" * 32
    a = cal.event_uid(k, "c", None, "2026-10-18", "Anna")
    assert a != cal.event_uid(k, "c", None, "2026-10-18", "Bob")
    assert a != cal.event_uid(k, "c", None, "2026-10-19", "Anna")
    assert a != cal.event_uid(k, "c", "r1", "2026-10-18", "Anna")
    assert a != cal.event_uid(b"j" * 32, "c", None, "2026-10-18", "Anna")
    assert cal.event_uid(k, "c", None, "x", "t", " ha-uid ") == "ha-uid"
    assert len(cal.event_uid(k, "c", None, "x", "t", "")) == 16


def test_uid_same_for_owner_and_member_on_original_title(mhouse, monkeypatch):
    EVENTS["calendar.rentals"].append({"start": "2026-10-24", "end": "2026-11-01", "summary": "Visitor (4 adults)"})
    try:
        owner = run(cal.PatrimonyCalendarEventsView(mhouse).get(_Q(query=WIN))).json()["events"]
        _plain_member(monkeypatch)
        run(pqs.async_setup_pq_shield(mhouse))
        tok = _mint(mhouse)[1]["token"]
        r = run(cal.PatrimonyMemberCalendarEventsView(mhouse).get(_Q(tok, query=WIN)))
        member = r.json()["events"]
    finally:
        EVENTS["calendar.rentals"].pop()
    o = next(e for e in owner if e["start"] == "2026-10-24")
    m = next(e for e in member if e["start"] == "2026-10-24")
    assert m["title"] == "Guests" and o["title"].startswith("Visitor")
    assert m["uid"] == o["uid"] and len(m["uid"]) == 16
    key = run(cal.async_uid_key(mhouse))
    assert m["uid"] != cal.event_uid(key, o["calendarId"], None, "2026-10-24", "Guests")
    assert "Visitor" not in r.text
    assert all("uid" in e for e in member)


def test_role_birthdays_allowed_and_bogus_dropped(house):
    assert cal._norm_row({"expose": True, "role": "birthdays"})["role"] == "birthdays"
    assert cal._norm_row({"role": "stays"})["role"] == "stays"
    assert cal._norm_row({"role": "bogus"})["role"] is None
    v = cal.PatrimonyEditorCalendarsView(house)
    req = _Q(user=_Admin())
    req._body = {"calendars": [{"entity_id": "calendar.birthdays", "expose": True, "role": "birthdays"}]}
    out = run(v.post(req)).json()
    assert out["calendars"][0]["role"] == "birthdays" and "birthdays" in out["roles"]
    lst = run(cal.PatrimonyCalendarsView(house).get(_Q())).json()["calendars"]
    assert [(c["name"], c["role"]) for c in lst] == [("Birthdays", "birthdays")]


def test_panel_html_offers_birthdays_role():
    from pathlib import Path
    html = (Path(cal.__file__).parent / "www" / "index.html").read_text()
    assert '["birthdays", "Birthdays"]' in html and '["stays", "Guest stays"]' in html


def test_utc_z_bounds_accepted_end_exclusive_and_92_day_limit():
    now = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
    s, e = cal.parse_window({"start": "2026-10-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"}, HOUSE_TZ, now)
    assert s == datetime(2026, 10, 1, tzinfo=timezone.utc) and e == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert s.isoformat() == "2026-10-01T02:00:00+02:00" and e.isoformat() == "2026-11-01T01:00:00+01:00"
    # Exactly 92 days with Z bounds is allowed; one second more is not.
    s, e = cal.parse_window({"start": "2026-10-01T00:00:00Z", "end": "2027-01-01T00:00:00Z"}, HOUSE_TZ, now)
    assert e.astimezone(timezone.utc) - s.astimezone(timezone.utc) == cal.timedelta(days=92)
    # 92 days in UTC across spring-forward (wall clock 92d+1h) is also allowed.
    assert cal.parse_window({"start": "2027-01-01T00:00:00Z", "end": "2027-04-03T00:00:00Z"}, HOUSE_TZ, now)
    for q in ({"start": "2026-10-01T00:00:00Z", "end": "2027-01-01T00:00:01Z"},
              {"start": "2026-09-01T00:00:00Z", "end": "2027-01-01T00:00:00Z"}):
        with pytest.raises(cal.WindowError) as ei:
            cal.parse_window(q, HOUSE_TZ, now)
        assert ei.value.code == "window_too_large"
    with pytest.raises(cal.WindowError) as ei:
        cal.parse_window({"start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"}, HOUSE_TZ, now)
    assert ei.value.code == "bad_range"


def test_utc_z_window_http_end_exclusive(house):
    EVENTS["calendar.birthdays"].append({"start": "2026-10-30", "end": "2026-10-31", "summary": "Edge"})
    v = cal.PatrimonyCalendarEventsView(house)
    q = {"start": "2026-10-15T22:00:00Z", "end": "2026-10-29T23:00:00Z"}  # HOUSE_TZ local midnights
    try:
        r = run(v.get(_Q(query=q)))
    finally:
        EVENTS["calendar.birthdays"].pop()
    b = r.json()
    assert r.status == 200
    assert b["start"] == "2026-10-16T00:00:00+02:00" and b["end"] == "2026-10-30T00:00:00+01:00"
    # Passed to HA as the same instants; HA get_events treats end as exclusive.
    _, sent_start, sent_end = house.services.calls[0]
    assert sent_start == "2026-10-16T00:00:00+02:00" and sent_end == "2026-10-30T00:00:00+01:00"
    r = run(v.get(_Q(query={"start": "2026-10-01T00:00:00Z", "end": "2027-01-01T00:00:01Z"})))
    assert r.status == 400 and r.json()["error"]["code"] == "window_too_large"
