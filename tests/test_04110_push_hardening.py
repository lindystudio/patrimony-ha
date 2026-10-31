"""0.4.110 push hardening: S2 Backend 2xx facts in the push log, S3 attention commit on
2xx + bounded retry, S4 chat retry in the background, S5/S6 missing_key rows, S7 chat
201 push field, S8 lastPushStatus only cleared on 2xx/new key, S9 upstreamError kept."""
from __future__ import annotations

import io
import json
import logging
from urllib.error import HTTPError, URLError

import pytest

from custom_components.patrimony_collection import notify as notify_mod
from custom_components.patrimony_collection.attention import DEBOUNCE_SECONDS
from custom_components.patrimony_collection.chat import handle_member_post, handle_post, notify_house_chat

from test_attention_push import _Hass as _AttHass, _fact  # noqa: E402
from test_chat import SECRET, T0, _Entry, _Hass, _panel  # noqa: E402

HEK = "hek_" + "x" * 64


class _Resp:
    def __init__(self, status=202, body=b'{"accepted": true, "deviceCount": 1}'):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Seq:
    """urlopen stand-in: each call pops the next scripted result."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, req, timeout=8):
        self.calls.append(json.loads(req.data.decode()))
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            if 200 <= item < 300:
                return _Resp(item)
            raise HTTPError(req.full_url, item, "x", {}, io.BytesIO(b'{"error":{"code":"upstream_' + str(item).encode() + b'"}}'))
        return item


def _http(code, body=b"{}"):
    return HTTPError("https://api.example/v1", code, "x", {}, io.BytesIO(body))


TIMEOUT = URLError(TimeoutError("timed out"))


@pytest.fixture
def retries(monkeypatch):
    """Capture scheduled retries instead of sleeping. run() executes them in order."""
    queue = []

    def _sched(_hass, delay, fn):
        queue.append((delay, fn))
        return True

    monkeypatch.setattr(notify_mod, "_RETRY_SCHEDULER", _sched)
    notify_mod._ATTENTION_INFLIGHT.clear()

    class _Q(list):
        def run_next(self):
            delay, fn = queue.pop(0)
            fn()
            return delay

    q = _Q()
    q.queue = queue
    yield q
    notify_mod._ATTENTION_INFLIGHT.clear()


def _store(hass):
    return json.loads(notify_mod.notify_path(hass).read_text())


# ---------------------------------------------------------------- S2

def test_s2_backend_2xx_facts_are_stored_sanitised_and_local_names_kept(tmp_path, monkeypatch):
    hass = _Hass(tmp_path, _Entry())
    body = json.dumps({"accepted": True, "deviceCount": 0, "pushError": "apns_unconfigured",
                       "eventId": "evt_42", "devices": [{"deviceName": "Study iPhone", "token": "ab" * 32}]}).encode()
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(_Resp(202, body)))
    assert notify_house_chat(hass) == 202
    row = notify_mod.push_log_document(hass)["rows"][-1]
    assert row["ok"] is True and row["status"] == 202
    assert row["recipients"] == 0 and row["eventId"] == "evt_42" and row["pushError"] == "apns_unconfigured"
    assert row["backendDevices"] == ["Study iPhone"]
    assert row["localDevices"] == row["devices"]  # legacy column keeps its meaning
    assert row["upstreamError"] is None
    blob = json.dumps(_store(hass))
    assert "ab" * 32 not in blob and "hek_" not in blob

    # Unsafe values from Backend never land in the log.
    bad = json.dumps({"deviceCount": "7", "eventId": "hek_leak", "pushError": "token=abc def"}).encode()
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(_Resp(202, bad)))
    notify_house_chat(hass)
    row = notify_mod.push_log_document(hass)["rows"][-1]
    assert row["recipients"] is None and row["eventId"] is None and row["pushError"] is None
    assert "hek_leak" not in json.dumps(_store(hass))


def test_s2_old_rows_still_parse_with_new_columns(tmp_path):
    hass = _Hass(tmp_path, _Entry())
    path = notify_mod.notify_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pushLog": [
        {"at": "2026-10-23T21:43:24+02:00", "fire_class": "chat.message", "kind": "chat", "rule": "chat.message",
         "status": 202, "error": None, "ok": True, "reason": "New chat message", "severity": "attention",
         "devices": ["Study iPhone"]},
    ]}))
    row = notify_mod.push_log_document(hass)["rows"][0]
    assert row["devices"] == ["Study iPhone"] == row["localDevices"]
    assert row["recipients"] is None and row["eventId"] is None and row["upstreamError"] is None
    assert "attempt" not in row


# ---------------------------------------------------------------- S3

def _arm(hass, monkeypatch, *results):
    seq = _Seq(*results)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    assert notify_mod.dispatch_attention_pushes(hass, [_fact()], "p", HEK, now=1000) == 0  # seed
    return seq


def test_s3_stamp_and_baseline_only_after_2xx_with_backoff(tmp_path, monkeypatch, retries):
    hass = _AttHass(tmp_path)
    on = _fact(raw="on", on=True, severity="alert")
    seq = _arm(hass, monkeypatch, 503, TIMEOUT, 202)
    assert notify_mod.dispatch_attention_pushes(hass, [on], "p", HEK, now=2000) == 0
    att = _store(hass)["attention"]
    assert att["pushed"] == {} and att["classes"] == {}  # nothing committed on 503
    # Another evaluation while the retry is pending does not double-send.
    assert notify_mod.dispatch_attention_pushes(hass, [on], "p", HEK, now=2001) == 0
    assert len(seq.calls) == 1
    assert retries.run_next() == 5.0
    assert _store(hass)["attention"]["pushed"] == {}
    assert retries.run_next() == 30.0
    att = _store(hass)["attention"]
    assert list(att["classes"].values()) == ["security.smoke"]
    (stamp,) = att["pushed"].values()
    assert stamp == 2000 + 5 + 30
    assert not retries.queue and len(seq.calls) == 3
    rows = notify_mod.push_log_document(hass)["rows"]
    assert [(r["status"], r["error"], r["attempt"], r["retry"]) for r in rows] == [
        (503, None, 1, True), (None, "timeout", 2, True), (202, None, 3, False)]
    assert rows[0]["upstreamError"] == "upstream_503"
    # 20-minute window applies after the 2xx.
    off = _fact()
    notify_mod.dispatch_attention_pushes(hass, [off], "p", HEK, now=2100)
    assert notify_mod.dispatch_attention_pushes(hass, [on], "p", HEK, now=2200) == 0
    assert len(seq.calls) == 3
    notify_mod.dispatch_attention_pushes(hass, [off], "p", HEK, now=2300)
    assert notify_mod.dispatch_attention_pushes(hass, [on], "p", HEK, now=2035 + DEBOUNCE_SECONDS + 1) == 1


def test_s3_final_failure_keeps_no_stamp_and_next_evaluation_refires(tmp_path, monkeypatch, retries):
    hass = _AttHass(tmp_path)
    on = _fact(raw="on", on=True, severity="alert")
    seq = _arm(hass, monkeypatch, 500)
    notify_mod.dispatch_attention_pushes(hass, [on], "p", HEK, now=2000)
    delays = []
    while retries.queue:
        delays.append(retries.run_next())
    assert delays == [5.0, 30.0, 120.0]
    assert len(seq.calls) == notify_mod.PUSH_MAX_ATTEMPTS == 4
    att = _store(hass)["attention"]
    assert att["pushed"] == {} and att["classes"] == {}
    assert notify_mod._ATTENTION_INFLIGHT == set()
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(202))
    assert notify_mod.dispatch_attention_pushes(hass, [on], "p", HEK, now=2400) == 1


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_s3_4xx_is_never_retried(tmp_path, monkeypatch, retries, code):
    hass = _AttHass(tmp_path)
    seq = _arm(hass, monkeypatch, code)
    notify_mod.dispatch_attention_pushes(hass, [_fact(raw="on", on=True, severity="alert")], "p", HEK, now=2000)
    assert retries.queue == [] and len(seq.calls) == 1
    assert _store(hass)["attention"]["pushed"] == {}


def test_s3_429_is_retried(tmp_path, monkeypatch, retries):
    hass = _AttHass(tmp_path)
    _arm(hass, monkeypatch, 429, 202)
    notify_mod.dispatch_attention_pushes(hass, [_fact(raw="on", on=True, severity="alert")], "p", HEK, now=2000)
    assert len(retries.queue) == 1
    retries.run_next()
    assert len(_store(hass)["attention"]["pushed"]) == 1


def test_s3_retry_uses_loop_timer_and_executor_not_the_loop(tmp_path, monkeypatch):
    """Real HA path: call_soon_threadsafe(call_later(delay, kick)); kick adds an executor job."""
    events = []

    class _Loop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, fn, *args):
            events.append(("threadsafe", fn.__name__, args[0]))
            fn(*args)

        def call_later(self, delay, cb):
            events.append(("later", delay))
            cb()

    hass = _AttHass(tmp_path)
    hass.loop = _Loop()
    hass.async_add_executor_job = lambda fn: events.append(("executor",)) or None
    assert notify_mod.schedule_push_retry(hass, 30.0, lambda: None) is True
    assert events == [("threadsafe", "call_later", 30.0), ("later", 30.0), ("executor",)]


# ---------------------------------------------------------------- S4 / S7

def test_s4_s7_chat_first_try_sync_retries_in_background(tmp_path, monkeypatch, retries):
    hass = _Hass(tmp_path, _Entry())
    import custom_components.patrimony_collection.chat as chat_mod

    monkeypatch.setattr(chat_mod, "send_house_chat_push", lambda *a, **k: {})
    plain, _ = handle_post(_Hass(tmp_path / "ref", _Entry()), _panel(), {"text": "x"}, now=T0)
    monkeypatch.undo()
    monkeypatch.setattr(notify_mod, "_RETRY_SCHEDULER", lambda _h, d, fn: retries.queue.append((d, fn)) or True)
    seq = _Seq(503, 202)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    body, status = handle_post(hass, _panel(), {"text": SECRET}, now=T0)
    assert status == 201
    assert body["push"] == {"ok": False, "status": 503}
    assert set(body) - {"push"} == set(plain) - {"push"}  # existing fields unchanged
    assert body["text"] == SECRET
    assert len(seq.calls) == 1
    assert [d for d, _ in retries.queue] == [5.0]  # not waited for by the response
    retries.run_next()
    assert len(seq.calls) == 2
    rows = notify_mod.push_log_document(hass)["rows"]
    assert [(r["status"], r["attempt"], r["retry"]) for r in rows[-2:]] == [(503, 1, True), (202, 2, False)]
    assert retries.queue == []


@pytest.mark.parametrize("first,retry", [(TIMEOUT, True), (_http(429), True), (_http(502), True),
                                         (_http(400), False), (_http(401), False), (_http(404), False)])
def test_s4_chat_retry_only_on_transient(tmp_path, monkeypatch, retries, first, retry):
    hass = _Hass(tmp_path, _Entry())
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(first, 202))
    body, status = handle_post(hass, _panel(), {"text": "hi"}, now=T0)
    assert status == 201 and body["push"]["ok"] is False
    assert bool(retries.queue) is retry


def test_s7_ok_push_and_member_body(tmp_path, monkeypatch, retries):
    hass = _Hass(tmp_path, _Entry())
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(202))
    body, status = handle_post(hass, _panel(), {"text": "hi"}, now=T0)
    assert status == 201 and body["push"] == {"ok": True, "status": 202}
    member = {"memberId": "m1", "memberName": "Anna"}
    mbody, mstatus = handle_member_post(hass, member, "m_m1", {"text": "hi"}, now=T0)
    assert mstatus == 201 and mbody["push"] == {"ok": True, "status": 202}
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(TIMEOUT))
    mbody, _ = handle_member_post(hass, member, "m_m1", {"text": "hi"}, now=T0)
    assert mbody["push"] == {"ok": False, "status": None}


# ---------------------------------------------------------------- S5 / S6

def test_s5_chat_missing_key_warns_and_logs_row(tmp_path, monkeypatch, caplog, retries):
    hass = _Hass(tmp_path, _Entry(key=""))
    monkeypatch.setattr(notify_mod, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send")))
    caplog.set_level(logging.WARNING)
    body, status = handle_post(hass, _panel(), {"text": SECRET}, now=T0)
    assert status == 201 and body["push"] == {"ok": False, "status": None}
    assert "patrimony_chat_notice_skipped reason=missing_key" in caplog.text
    row = notify_mod.push_log_document(hass)["rows"][-1]
    assert (row["kind"], row["ok"], row["error"], row["status"]) == ("chat", False, "missing_key", None)
    assert SECRET not in json.dumps(_store(hass)) and SECRET not in caplog.text
    # Unusable property_id is the same.
    entry = _Entry()
    entry.data["property_id"] = ""
    assert notify_house_chat(_Hass(tmp_path / "b", entry)) is None
    assert notify_mod.push_log_document(_Hass(tmp_path / "b", entry))["rows"][-1]["error"] == "missing_key"


def test_s6_attention_missing_key_warns_and_logs_row_per_transition(tmp_path, monkeypatch, caplog):
    hass = _AttHass(tmp_path)
    monkeypatch.setattr(notify_mod, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send")))
    caplog.set_level(logging.WARNING)
    notify_mod.dispatch_attention_pushes(hass, [_fact()], None, None, now=1)
    assert notify_mod.dispatch_attention_pushes(hass, [_fact(raw="on", on=True, severity="alert")], None, None, now=2) == 0
    assert "patrimony_attention_push_skipped reason=missing_key class=security.smoke" in caplog.text
    (row,) = notify_mod.push_log_document(hass)["rows"]
    assert (row["kind"], row["rule"], row["ok"], row["error"]) == ("attention", "security.smoke", False, "missing_key")
    # Baseline still advances (no replay when the key arrives), as before.
    assert notify_mod.dispatch_attention_pushes(hass, [_fact(raw="on", on=True, severity="alert")], "p", HEK, now=3) == 0
    assert len(notify_mod.push_log_document(hass)["rows"]) == 1


# ---------------------------------------------------------------- S8

def test_s8_last_push_status_cleared_only_on_2xx_or_new_key(tmp_path, monkeypatch, retries):
    hass = _Hass(tmp_path, _Entry())

    def status():
        return notify_mod.event_key_status_fields(hass)["eventKeyLastPushStatus"]

    monkeypatch.setattr(notify_mod, "urlopen", _Seq(_http(401, b'{"error":{"code":"revoked_key"}}')))
    notify_house_chat(hass)
    assert status()["code"] == 401 and status()["error"] == "revoked_key"
    for transient in (TIMEOUT, _http(503), _http(500), _http(429), _http(400)):
        monkeypatch.setattr(notify_mod, "urlopen", _Seq(transient))
        notify_house_chat(hass)
        assert status() is not None and status()["code"] == 401
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(202))
    notify_house_chat(hass)
    assert status() is None
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(_http(403)))
    notify_house_chat(hass)
    assert status()["code"] == 403
    notify_mod.mark_event_key_installed(hass)
    assert status() is None


# ---------------------------------------------------------------- S9

def test_s9_upstream_error_code_persisted_and_sanitised(tmp_path, monkeypatch, retries):
    hass = _Hass(tmp_path, _Entry())
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(_http(422, b'{"error":{"code":"unknown_card"}}')))
    notify_house_chat(hass)
    monkeypatch.setattr(notify_mod, "urlopen", _Seq(_http(400, b'{"error":{"code":"hek_abcdef"}}')))
    notify_house_chat(hass)
    rows = notify_mod.push_log_document(hass)["rows"]
    assert rows[-2]["upstreamError"] == "unknown_card" and rows[-2]["status"] == 422
    assert rows[-1]["upstreamError"] is None
    assert "hek_abcdef" not in json.dumps(_store(hass))


def test_manual_send_is_still_one_attempt(tmp_path, monkeypatch, retries):
    hass = _Hass(tmp_path, _Entry())
    seq = _Seq(503)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    status, outcome = notify_mod.manual_send(hass, "property-1", HEK, "Push test")
    assert status == 503 and outcome["ok"] is False and retries.queue == [] and len(seq.calls) == 1
    assert "attempt" not in notify_mod.push_log_document(hass)["rows"][-1]


def test_version_is_at_least_04110():
    from custom_components.patrimony_collection.version import INTEGRATION_VERSION

    assert tuple(int(x) for x in INTEGRATION_VERSION.split(".")) >= (0, 4, 110)


# ---------------------------------------------------------------- steering: kind, per-card cardId, S1

from custom_components.patrimony_collection import clients as clients_mod  # noqa: E402
from custom_components.patrimony_collection import members as members_mod  # noqa: E402
from test_attention_push import CARD, ITEM  # noqa: E402


def test_kind_is_sent_on_every_events_post(tmp_path, monkeypatch, retries):
    hass = _Hass(tmp_path, _Entry())
    seq = _Seq(202)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    notify_house_chat(hass)
    notify_mod.manual_send(hass, "property-1", HEK, "Push test")
    att = _AttHass(tmp_path / "att")
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    notify_mod.dispatch_attention_pushes(att, [_fact()], "p", HEK, now=1000)
    notify_mod.dispatch_attention_pushes(att, [_fact(raw="on", on=True, severity="alert")], "p", HEK, now=2000)
    assert [c["kind"] for c in seq.calls] == ["chat", "manual", "attention"]
    # Retries keep the kind.
    seq2 = _Seq(503, 202)
    monkeypatch.setattr(notify_mod, "urlopen", seq2)
    notify_house_chat(hass)
    retries.run_next()
    assert [c["kind"] for c in seq2.calls] == ["chat", "chat"]


def test_attention_sends_the_per_card_card_id_chat_and_manual_keep_shared(tmp_path, monkeypatch, retries):
    att = _AttHass(tmp_path)
    seq = _Seq(202)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    shared = notify_mod.load_card_id(att)
    assert shared != CARD
    notify_mod.dispatch_attention_pushes(att, [_fact()], "p", HEK, now=1000)
    notify_mod.dispatch_attention_pushes(att, [_fact(raw="on", on=True, severity="alert")], "p", HEK, now=2000)
    assert seq.calls[-1]["cardId"] == CARD  # the PresentationDocument card id, not the item or entity
    assert seq.calls[-1]["cardId"] != ITEM
    # Chat and manual on the same house keep the shared notify cardId.
    hass = _Hass(tmp_path / "c", _Entry())
    notify_house_chat(hass)
    notify_mod.manual_send(hass, "property-1", HEK, "Push test")
    chat_shared = notify_mod.load_card_id(hass)
    assert [c["cardId"] for c in seq.calls[-2:]] == [chat_shared, chat_shared]


@pytest.mark.parametrize("bad", ["", None, "binary_sensor.kitchen_smoke", "sensor.x", "52.1,4.3", "not-a-uuid"])
def test_attention_never_sends_entity_id_or_non_uuid_card(tmp_path, monkeypatch, retries, bad):
    att = _AttHass(tmp_path)
    seq = _Seq(202)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    notify_mod.dispatch_attention_pushes(att, [_fact(card_id=bad)], "p", HEK, now=1000)
    notify_mod.dispatch_attention_pushes(
        att, [_fact(card_id=bad, raw="on", on=True, severity="alert")], "p", HEK, now=2000)
    sent = [c["cardId"] for c in seq.calls]
    assert sent == [notify_mod.load_card_id(att)] * len(sent)
    assert all(c != ITEM and "." not in c and "," not in c for c in sent)


def test_s1_member_exclude_never_resolves_to_owner_client_or_device(tmp_path, monkeypatch, retries):
    hass = _Hass(tmp_path, _Entry())
    owner_cid = "0f3c2a9b8e7d4c6a9b1e2d3c4b5a6978"  # HA refresh-token id shape
    clients_mod.upsert_client(hass, owner_cid, device_name="Study iPhone", ha_username="admin", now=T0)
    clients_mod.rename_client(hass, owner_cid, "Study iPhone")
    member = {"memberId": owner_cid, "memberName": "Anna"}  # even a colliding memberId
    member_cid = members_mod.client_id_for(member["memberId"])
    assert member_cid != owner_cid and member_cid.startswith("m_")
    assert members_mod.is_member_client_id(member_cid) and not members_mod.is_member_client_id(owner_cid)
    clients_mod.upsert_client(hass, member_cid, touch_access=True, member_name="Anna")

    seq = _Seq(202)
    monkeypatch.setattr(notify_mod, "urlopen", seq)
    sender_hash = "c" * 64
    _, status = handle_member_post(hass, member, member_cid, {"text": "hi", "senderDevice": sender_hash}, now=T0)
    assert status == 201
    # Exclude is exactly the member's own hashed token; HA never derives one from owner state.
    assert seq.calls[-1]["excludeDevice"] == sender_hash
    row = notify_mod.push_log_document(hass)["rows"][-1]
    assert "Study iPhone" in row["localDevices"]  # owner still counted as a recipient
    # No senderDevice / junk -> no excludeDevice at all (never falls back to the owner's).
    for payload in ({"text": "hi"}, {"text": "hi", "senderDevice": owner_cid}, {"text": "hi", "senderDevice": "Study iPhone"}):
        handle_member_post(hass, member, member_cid, payload, now=T0)
        assert "excludeDevice" not in seq.calls[-1]
        assert "Study iPhone" in notify_mod.push_log_document(hass)["rows"][-1]["localDevices"]
