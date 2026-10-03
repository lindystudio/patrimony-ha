"""FIRE transitions notify. QUIET paths do not. Debounce and escalation."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.patrimony_collection.attention import (  # noqa: E402
    DEBOUNCE_SECONDS,
    classify_fact,
    local_failure_class,
    manual_notify_decision,
    plan_automatic_pushes,
)
from custom_components.patrimony_collection.mapping import attention_facts  # noqa: E402
from custom_components.patrimony_collection.notify import (  # noqa: E402
    MANUAL_SEND_CLASS,
    NO_IOS_CLIENT,
    PUSH_LOG_MAX,
    devices_from_event_body,
    dispatch_attention_pushes,
    load_card_id_and_post,
    post_house_event,
    push_log_document,
    record_attention_push,
)

CARD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ITEM = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ITEM_2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _fact(**overrides):
    base = {
        "item_id": ITEM,
        "card_id": CARD,
        "label": "Kitchen",
        "kind": "custom",
        "domain": "binary_sensor",
        "device_class": "smoke",
        "raw": "off",
        "severity": "ok",
        "on": False,
    }
    base.update(overrides)
    return base


def test_fire_classes_and_quiet_paths():
    assert classify_fact(_fact(domain="alarm_control_panel", raw="triggered", device_class=None, on=False)) == "security.alarm"
    assert classify_fact(_fact(raw="on", on=True, severity="alert")) == "security.smoke"
    assert classify_fact(_fact(device_class="gas", raw="on", on=True)) == "security.gas"
    assert classify_fact(_fact(device_class="moisture", raw="on", on=True)) == "security.leak"
    assert classify_fact(_fact(device_class="safety", raw="on", on=True)) == "security.safety"
    assert classify_fact(_fact(domain="lock", device_class=None, raw="unlocked", on=True, severity="alert")) == "security.lock"

    # Mapped item reaching alert (not a routine domain).
    assert classify_fact(
        _fact(domain="sensor", device_class="temperature", raw="40", on=False, severity="alert", label="Cellar")
    ) == "alert"

    quiet = [
        _fact(domain="light", device_class=None, raw="on", on=True, severity="attention"),
        _fact(domain="light", device_class=None, raw="on", on=True, severity="alert"),
        _fact(domain="climate", device_class=None, raw="22", on=False, severity="attention"),
        _fact(domain="weather", device_class=None, raw="lightning", on=False, severity="alert"),
        _fact(domain="binary_sensor", device_class="connectivity", raw="off", on=False, severity="attention"),
        _fact(kind="network", domain="sensor", device_class=None, raw="off", on=False, severity="alert"),
        _fact(domain="binary_sensor", device_class="door", raw="on", on=True, severity="attention"),
        _fact(domain="alarm_control_panel", device_class=None, raw="disarmed", on=False, severity="attention"),
        _fact(domain="alarm_control_panel", device_class=None, raw="armed_away", on=False, severity="ok"),
        _fact(domain="lock", device_class=None, raw="unlocked", on=True, severity="ok"),
        _fact(raw="unavailable", on=False, severity="attention"),
        _fact(raw="unknown", on=False, severity="alert"),
        _fact(kind="energy", domain="sensor", device_class="energy", raw="10", on=False, severity="alert"),
        _fact(domain="sensor", device_class="temperature", raw="21", on=False, severity="ok"),
        _fact(domain="sensor", device_class="temperature", raw="21", on=False, severity="attention"),
    ]
    assert all(classify_fact(row) is None for row in quiet)


def test_local_failure_is_not_unavailable_or_last_heard():
    assert local_failure_class("unavailable") is None
    assert local_failure_class("unknown") is None
    assert local_failure_class("ios_offline") is None
    assert local_failure_class("last_heard") is None
    assert local_failure_class(None) is None
    assert local_failure_class("presentation_blocked") == "presentation"


def test_manual_notify_stays_attention_and_needs_a_key():
    sent = manual_notify_decision(True, "Guest arriving")
    assert sent == {"fire_class": "manual", "severity": "attention", "title": "Guest arriving"}
    assert manual_notify_decision(False, "Guest arriving") is None
    assert manual_notify_decision(True, "  ") is None
    assert manual_notify_decision(True, "prefix hek_secret") is None
    long = "n" * 200
    clipped = manual_notify_decision(True, long)
    assert clipped is not None
    assert len(clipped["title"]) == 120
    assert clipped["severity"] == "attention"


def test_seed_does_not_push_and_transition_does():
    store: dict = {}
    off = _fact()
    on = _fact(raw="on", on=True, severity="alert")
    assert plan_automatic_pushes(store, [on], 1_000) == []
    assert store["seeded"] is True
    # Already in FIRE at first sight: still quiet.
    assert plan_automatic_pushes(store, [on], 1_001) == []

    store = {}
    assert plan_automatic_pushes(store, [off], 1_000) == []
    fired = plan_automatic_pushes(store, [on], 1_010)
    assert [row["fire_class"] for row in fired] == ["security.smoke"]
    assert fired[0]["severity"] == "alert"
    assert fired[0]["title"] == "Smoke detected"
    assert plan_automatic_pushes(store, [on], 1_020) == []


def test_debounce_suppresses_repeat_and_escalation_fires():
    store: dict = {}
    calm = _fact(domain="sensor", device_class="temperature", raw="20", on=False, severity="ok", label="Cellar")
    hot = _fact(domain="sensor", device_class="temperature", raw="40", on=False, severity="alert", label="Cellar")
    alarm = _fact(
        domain="alarm_control_panel",
        device_class=None,
        raw="triggered",
        on=False,
        severity="alert",
        label="Alarm",
    )
    assert plan_automatic_pushes(store, [calm], 0) == []
    first = plan_automatic_pushes(store, [hot], 10)
    assert [row["fire_class"] for row in first] == ["alert"]
    assert first[0]["title"] == "Cellar alert"
    assert plan_automatic_pushes(store, [calm], 20) == []
    assert plan_automatic_pushes(store, [hot], 30) == []

    escalated = plan_automatic_pushes(store, [alarm], 40)
    assert [row["fire_class"] for row in escalated] == ["security.alarm"]
    assert escalated[0]["severity"] == "alert"
    assert escalated[0]["title"] == "Alarm triggered"

    assert plan_automatic_pushes(store, [calm], 50) == []
    assert plan_automatic_pushes(store, [alarm], 60) == []
    assert plan_automatic_pushes(store, [calm], 70) == []
    again = plan_automatic_pushes(store, [alarm], 40 + DEBOUNCE_SECONDS + 1)
    assert [row["fire_class"] for row in again] == ["security.alarm"]


def test_other_card_same_class_is_not_the_same_debounce_key():
    store: dict = {}
    off = _fact()
    on = _fact(raw="on", on=True, severity="alert")
    other = _fact(item_id=ITEM_2, card_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd", raw="on", on=True, severity="alert")
    assert plan_automatic_pushes(store, [off], 0) == []
    assert len(plan_automatic_pushes(store, [on], 5)) == 1
    assert plan_automatic_pushes(store, [off, _fact(item_id=ITEM_2, card_id=other["card_id"])], 6) == []
    fired = plan_automatic_pushes(store, [on, other], 7)
    assert [row["card_id"] for row in fired] == [other["card_id"]]


class _Config:
    def __init__(self, root: Path):
        self._root = root

    def path(self, name: str) -> str:
        return str(self._root / name)


class _State:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}
        self.last_updated = None
        self.last_changed = None


class _States:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class _Hass:
    def __init__(self, root: Path, states=None):
        self.config = _Config(root)
        self.states = _States(states or {})


def test_dispatch_posts_alert_only_after_transition(tmp_path, monkeypatch):
    calls = []

    def fake_post(property_id, key, card_id, title, timeout=8, severity="attention"):
        calls.append({"severity": severity, "title": title, "card_id": card_id, "property_id": property_id})
        assert "hek_" not in title
        assert key.startswith("hek_")
        return 200

    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.post_house_event",
        fake_post,
    )
    hass = _Hass(tmp_path)
    off = _fact()
    on = _fact(raw="on", on=True, severity="alert")
    assert dispatch_attention_pushes(hass, [off], "property-1", "hek_house", now=10) == 0
    assert calls == []
    assert dispatch_attention_pushes(hass, [on], "property-1", "hek_house", now=20) == 1
    assert calls == [
        {
            "severity": "alert",
            "title": "Smoke detected",
            "card_id": calls[0]["card_id"],
            "property_id": "property-1",
        }
    ]
    stored = json.loads((tmp_path / "patrimony_collection" / "notify.json").read_text())
    assert "hek_" not in json.dumps(stored)
    assert stored["attention"]["seeded"] is True
    assert dispatch_attention_pushes(hass, [on], "property-1", "hek_house", now=30) == 0
    assert len(calls) == 1


def test_missing_key_does_not_post_or_replay_later(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.post_house_event",
        lambda *args, **kwargs: calls.append(1) or 200,
    )
    hass = _Hass(tmp_path)
    off = _fact()
    on = _fact(raw="on", on=True, severity="alert")
    assert dispatch_attention_pushes(hass, [off], None, None, now=1) == 0
    assert dispatch_attention_pushes(hass, [on], None, None, now=2) == 0
    assert calls == []
    # Key arrives while smoke is already on: not a new transition.
    assert dispatch_attention_pushes(hass, [on], "property-1", "hek_house", now=3) == 0
    assert calls == []


def test_post_house_event_severity_and_secret_title(monkeypatch):
    captured = {}

    class _Resp:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=8):
        captured["body"] = json.loads(req.data.decode())
        captured["headers"] = dict(req.header_items())
        return _Resp()

    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.urlopen",
        fake_urlopen,
    )
    assert post_house_event("prop", "hek_house", CARD, "Alarm triggered", severity="alert") == 202
    assert captured["body"] == {"cardId": CARD, "severity": "alert", "title": "Alarm triggered"}
    assert captured["headers"]["X-house-event-key"] == "hek_house"
    assert post_house_event("prop", "hek_house", CARD, "Guest arriving") == 202
    assert captured["body"]["severity"] == "attention"
    assert post_house_event("prop", "hek_house", CARD, "hek_nope", severity="alert") == 400


def test_attention_facts_drop_entity_id(tmp_path):
    entity = "binary_sensor.kitchen_smoke"
    hass = _Hass(
        tmp_path,
        {entity: _State("on", {"device_class": "smoke"})},
    )
    options = {
        "cards": [{"id": CARD, "kind": "security", "title": "Alarm", "priority": 1}],
        "mappings": [
            {
                "item_id": ITEM,
                "card_id": CARD,
                "entity_id": entity,
                "label": "Kitchen smoke",
                "value_type": "bool",
                "severity_mode": "auto",
            }
        ],
    }
    facts = attention_facts(hass, {"property_id": "property-1"}, options)
    assert len(facts) == 1
    assert "entity_id" not in facts[0]
    assert entity not in json.dumps(facts)
    assert classify_fact(facts[0]) == "security.smoke"


def test_addon_manual_gate_never_alerts():
    spec = importlib.util.spec_from_file_location(
        "patrimony_addon_web",
        ROOT / "addons" / "patrimony_collection" / "web.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    decision = module.manual_notify_decision("hek_house", "Guest arriving")
    assert decision["severity"] == "attention"
    assert decision["fire_class"] == "manual"
    assert module.manual_notify_decision(None, "Guest arriving") is None
    assert module.manual_notify_decision("hek_house", "hek_secret") is None
    assert module.manual_notify_decision("hek_house", "") is None


def test_push_log_appends_fire_only_and_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.post_house_event",
        lambda *args, **kwargs: 200,
    )
    hass = _Hass(tmp_path)
    quiet = _fact(domain="light", device_class=None, raw="on", on=True, severity="attention")
    assert dispatch_attention_pushes(hass, [quiet], "property-1", "hek_house", now=1) == 0
    assert dispatch_attention_pushes(hass, [quiet], "property-1", "hek_house", now=2) == 0
    assert push_log_document(hass) == {"rows": []}

    off = _fact()
    on = _fact(raw="on", on=True, severity="alert")
    assert dispatch_attention_pushes(hass, [off], "property-1", "hek_house", now=10) == 0
    assert push_log_document(hass)["rows"] == []
    assert dispatch_attention_pushes(hass, [on], "property-1", "hek_house", now=20) == 1
    doc = push_log_document(hass)
    assert len(doc["rows"]) == 1
    assert doc["rows"][0]["fire_class"] == "security.smoke"
    assert doc["rows"][0]["devices"] == NO_IOS_CLIENT
    assert doc["rows"][0]["at"]
    blob = json.dumps(doc)
    assert "hek_" not in blob
    assert "eyJ" not in blob
    # Still on: QUIET relative to the baseline, no extra row.
    assert dispatch_attention_pushes(hass, [on], "property-1", "hek_house", now=30) == 0
    assert len(push_log_document(hass)["rows"]) == 1

    for i in range(PUSH_LOG_MAX + 5):
        record_attention_push(hass, "security.alarm", devices=["hek_secret", "Kitchen"])
    capped = push_log_document(hass)
    assert len(capped["rows"]) == PUSH_LOG_MAX
    assert all(row["devices"] == ["Kitchen"] or row["devices"] == NO_IOS_CLIENT for row in capped["rows"])
    assert "hek_" not in json.dumps(capped)


def test_manual_send_writes_one_row_fire_unchanged_quiet_silent(tmp_path, monkeypatch):
    """Gold Send success is a manual row. FIRE keeps its class. QUIET writes nothing."""
    statuses = {"code": 200}

    def fake_post(property_id, key, card_id, title, timeout=8, severity="attention"):
        assert key.startswith("hek_")
        assert "hek_" not in title
        return statuses["code"]

    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.post_house_event",
        fake_post,
    )
    hass = _Hass(tmp_path)
    quiet = _fact(domain="light", device_class=None, raw="on", on=True, severity="attention")
    assert dispatch_attention_pushes(hass, [quiet], "property-1", "hek_house", now=1) == 0
    assert dispatch_attention_pushes(hass, [quiet], "property-1", "hek_house", now=2) == 0
    assert push_log_document(hass)["rows"] == []

    statuses["code"] = 502
    assert load_card_id_and_post(hass, "property-1", "hek_house", "Guest arriving") == 502
    assert push_log_document(hass)["rows"] == []

    statuses["code"] = 200
    assert load_card_id_and_post(hass, "property-1", "hek_house", "Guest arriving") == 200
    doc = push_log_document(hass)
    assert len(doc["rows"]) == 1
    row = doc["rows"][0]
    assert row["fire_class"] == MANUAL_SEND_CLASS == "manual.send"
    assert row["fire_class"] != "security.smoke"
    assert row["devices"] == NO_IOS_CLIENT
    assert row["at"]
    blob = json.dumps(doc)
    assert "hek_" not in blob
    assert "eyJ" not in blob
    assert "token" not in blob.lower()

    off = _fact()
    on = _fact(raw="on", on=True, severity="alert")
    assert dispatch_attention_pushes(hass, [off], "property-1", "hek_house", now=10) == 0
    assert push_log_document(hass)["rows"][0]["fire_class"] == "manual.send"
    assert dispatch_attention_pushes(hass, [on], "property-1", "hek_house", now=20) == 1
    classes = [item["fire_class"] for item in push_log_document(hass)["rows"]]
    assert classes == ["manual.send", "security.smoke"]
    stored = json.loads((tmp_path / "patrimony_collection" / "notify.json").read_text())
    assert "hek_" not in json.dumps(stored)


def test_event_body_device_list_or_exact_sentence(monkeypatch):
    assert devices_from_event_body(b'{"accepted": true, "deviceCount": 2}') is None
    parsed = devices_from_event_body(json.dumps({
        "devices": [
            {"deviceName": "Study iPhone", "token": "ab" * 32},
            "hek_secret",
            "aa" * 32,
            {"clientId": "ios-client-77ab"},
            {"clientName": "Kitchen iPad"},
        ]
    }).encode())
    assert parsed == ["Study iPhone", "aaaa", "ios-client-77ab", "Kitchen iPad"]
    assert "ab" * 32 not in parsed

    class _Resp:
        status = 202

        def read(self):
            return b'{"accepted": true, "deviceCount": 1}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.urlopen",
        lambda req, timeout=8: _Resp(),
    )
    assert post_house_event("prop", "hek_house", CARD, "Guest arriving") == 202
    from custom_components.patrimony_collection import notify as notify_mod
    assert notify_mod._event_tls.devices is None


def test_addon_manual_send_writes_panel_log(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "patrimony_addon_web_log",
        ROOT / "addons" / "patrimony_collection" / "web.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "notify.json"
    path.write_text(json.dumps({"cardId": CARD}) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "NOTIFY_PATH", path)
    session = tmp_path / "ios_session.json"
    session.write_text(json.dumps({"clientId": "ios-client-77ab"}), encoding="utf-8")
    monkeypatch.setattr(module, "IOS_SESSION_PATH", session)
    monkeypatch.setattr(module, "AUTH_STORAGE_PATH", tmp_path / "no-auth.json")
    module.record_manual_send(["hek_secret", "Study iPhone"])
    module.record_manual_send(None)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["cardId"] == CARD
    assert [row["fire_class"] for row in stored["pushLog"]] == ["manual.send", "manual.send"]
    assert stored["pushLog"][0]["devices"] == ["Study iPhone"]
    assert stored["pushLog"][1]["devices"] == ["ios-client-77ab"]
    blob = json.dumps(stored)
    assert "hek_" not in blob
    assert "eyJ" not in blob
    assert "device list not returned by the events API" not in blob
    assert "device list not returned by the events API" not in Path(module.__file__).read_text(encoding="utf-8")


def test_panel_push_log_tab_copy():
    html = (
        ROOT
        / "custom_components"
        / "patrimony_collection"
        / "www"
        / "index.html"
    ).read_text(encoding="utf-8")
    button = 'data-house-sub="pushlog"'
    assert button in html
    assert ">Push log<" in html
    assert "No attention pushes yet." in html
    assert "No iOS client registered" in html
    assert "device list not returned by the events API" not in html
    assert "manual.send" in html
    assert "Manual send" in html
    help_text = (
        "PQ Shield adds further post-quantum (PQ) security beyond standard SSL/TLS "
        "between your phone and this house; requires iOS 26+."
    )
    assert help_text in html
    assert html.find('id="sub_conn"') < html.find(button)


def test_send_with_known_client_id_writes_id_or_name(tmp_path, monkeypatch):
    """Gold Send and FIRE record the client the house actually has, not the old sentence."""
    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.post_house_event",
        lambda *args, **kwargs: 202,
    )
    hass = _Hass(tmp_path)
    folder = tmp_path / "patrimony_collection"
    folder.mkdir()
    (folder / "ios_session.json").write_text(
        json.dumps({"clientId": "ios-client-77ab", "appVersion": "1.0"}),
        encoding="utf-8",
    )
    assert load_card_id_and_post(hass, "property-1", "hek_house", "Guest arriving") == 202
    row = push_log_document(hass)["rows"][0]
    assert row["fire_class"] == "manual.send"
    assert row["devices"] == ["ios-client-77ab"]

    (folder / "ios_session.json").write_text(
        json.dumps({
            "deviceName": "Study iPhone",
            "clientId": "ios-client-77ab",
            "appVersion": "1.0",
        }),
        encoding="utf-8",
    )
    off = _fact()
    on = _fact(raw="on", on=True, severity="alert")
    assert dispatch_attention_pushes(hass, [off], "property-1", "hek_house", now=10) == 0
    assert dispatch_attention_pushes(hass, [on], "property-1", "hek_house", now=20) == 1
    fire = push_log_document(hass)["rows"][-1]
    assert fire["fire_class"] == "security.smoke"
    assert fire["devices"] == ["Study iPhone"]
    blob = (folder / "notify.json").read_text(encoding="utf-8")
    assert "device list not returned by the events API" not in blob
    assert "hek_" not in blob
    assert "ios-client-77ab" not in fire["devices"]


def test_no_registered_client_is_honest_and_sentence_is_gone(tmp_path):
    hass = _Hass(tmp_path)
    record_attention_push(hass, "manual.send", devices=None)
    assert push_log_document(hass)["rows"][0]["devices"] == NO_IOS_CLIENT
    # A previously stored fallback sentence is not served or left on disk.
    path = tmp_path / "patrimony_collection" / "notify.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["pushLog"].append({
        "at": "2026-10-03T12:00:00+02:00",
        "fire_class": "security.smoke",
        "devices": "device list not returned by the events API",
    })
    path.write_text(json.dumps(stored), encoding="utf-8")
    doc = push_log_document(hass)
    assert all(row["devices"] != "device list not returned by the events API" for row in doc["rows"])
    assert doc["rows"][-1]["devices"] == NO_IOS_CLIENT
    assert "device list not returned by the events API" not in path.read_text(encoding="utf-8")
    root = ROOT / "custom_components" / "patrimony_collection"
    blob = []
    for file in root.rglob("*"):
        if file.suffix not in {".py", ".html", ".js", ".json"}:
            continue
        blob.append(file.read_text(encoding="utf-8", errors="ignore"))
    assert "device list not returned by the events API" not in "\n".join(blob)


def test_refresh_token_id_is_truncated_and_secret_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.post_house_event",
        lambda *args, **kwargs: 200,
    )
    hass = _Hass(tmp_path)

    class _Tok:
        client_name = "Patrimony iOS"
        client_id = None
        id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeffffab12"
        token = "super-secret-ha-token-value"

    class _Auth:
        refresh_tokens = {"one": _Tok()}

    hass.auth = _Auth()
    assert load_card_id_and_post(hass, "property-1", "hek_house", "Guest arriving") == 200
    row = push_log_document(hass)["rows"][0]
    assert row["devices"] == ["Patrimony iOS"]
    blob = json.dumps(row)
    assert "super-secret" not in blob
    assert "token" not in blob.lower()
    assert "hek_" not in blob


def test_one_registered_client_keeps_its_device_name(tmp_path, monkeypatch):
    """A single Patrimony iOS token still shows the stored device name."""
    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.post_house_event",
        lambda *args, **kwargs: 200,
    )
    hass = _Hass(tmp_path)
    folder = tmp_path / "patrimony_collection"
    folder.mkdir()
    (folder / "ios_session.json").write_text(
        json.dumps({"deviceName": "iPhone", "appVersion": "1.0"}),
        encoding="utf-8",
    )

    class _Tok:
        client_name = "Patrimony iOS"
        client_id = None
        id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeffffab12"
        token = "super-secret-ha-token-value"

    class _Auth:
        refresh_tokens = {"one": _Tok()}

    hass.auth = _Auth()
    assert load_card_id_and_post(hass, "property-1", "hek_house", "Guest arriving") == 200
    assert push_log_document(hass)["rows"][0]["devices"] == ["iPhone"]


def test_each_registered_client_is_listed_on_send_and_fire(tmp_path, monkeypatch):
    """More than one refresh token is not collapsed to the single session name."""
    monkeypatch.setattr(
        "custom_components.patrimony_collection.notify.post_house_event",
        lambda *args, **kwargs: 202,
    )
    hass = _Hass(tmp_path)
    folder = tmp_path / "patrimony_collection"
    folder.mkdir()
    (folder / "ios_session.json").write_text(
        json.dumps({"deviceName": "iPhone", "appVersion": "1.0"}),
        encoding="utf-8",
    )

    class _Tok:
        def __init__(self, ident):
            self.client_name = "Patrimony iOS"
            self.client_id = None
            self.id = ident
            self.token = "super-secret-ha-token-value"

    class _Auth:
        refresh_tokens = {
            "a": _Tok("aaaaaaaa-bbbb-4ccc-8ddd-eeeeffff1111"),
            "b": _Tok("bbbbbbbb-cccc-4ddd-8eee-ffff00002222"),
        }

    hass.auth = _Auth()
    assert load_card_id_and_post(hass, "property-1", "hek_house", "Guest arriving") == 202
    manual = push_log_document(hass)["rows"][0]
    assert manual["fire_class"] == "manual.send"
    assert manual["devices"] == ["iPhone", "2222"]
    off = _fact()
    on = _fact(raw="on", on=True, severity="alert")
    assert dispatch_attention_pushes(hass, [off], "property-1", "hek_house", now=10) == 0
    assert dispatch_attention_pushes(hass, [on], "property-1", "hek_house", now=20) == 1
    fire = push_log_document(hass)["rows"][-1]
    assert fire["fire_class"] == "security.smoke"
    assert fire["devices"] == ["iPhone", "2222"]
    blob = (folder / "notify.json").read_text(encoding="utf-8")
    assert "super-secret" not in blob
    assert "1111" not in blob
    assert "device list not returned by the events API" not in blob
    assert "hek_" not in blob
    html = (
        ROOT / "custom_components" / "patrimony_collection" / "www" / "index.html"
    ).read_text(encoding="utf-8")
    assert "function pushLogDeviceList" in html
    assert "Not only the first" in html


def test_refresh_tokens_without_a_session_name_use_last_four(tmp_path):
    hass = _Hass(tmp_path)

    class _Tok:
        def __init__(self, ident):
            self.client_name = "Patrimony iOS"
            self.client_id = None
            self.id = ident
            self.token = "super-secret-ha-token-value"

    class _Auth:
        refresh_tokens = {
            "a": _Tok("aaaaaaaa-bbbb-4ccc-8ddd-eeeeffff1111"),
            "b": _Tok("bbbbbbbb-cccc-4ddd-8eee-ffff00002222"),
        }

    hass.auth = _Auth()
    record_attention_push(hass, "manual.send", devices=None)
    assert push_log_document(hass)["rows"][0]["devices"] == ["1111", "2222"]


def test_addon_lists_each_refresh_token(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "patrimony_addon_web_multi",
        ROOT / "addons" / "patrimony_collection" / "web.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "notify.json"
    path.write_text(json.dumps({"cardId": CARD}) + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "NOTIFY_PATH", path)
    session = tmp_path / "ios_session.json"
    session.write_text(
        json.dumps({"deviceName": "iPhone", "appVersion": "1.0"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "IOS_SESSION_PATH", session)
    auth = tmp_path / "auth"
    auth.write_text(
        json.dumps({
            "data": {
                "refresh_tokens": [
                    {
                        "client_name": "Patrimony iOS",
                        "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeffff1111",
                        "token": "super-secret-ha-token-value",
                    },
                    {
                        "client_name": "Patrimony iOS",
                        "id": "bbbbbbbb-cccc-4ddd-8eee-ffff00002222",
                        "token": "super-secret-ha-token-value",
                    },
                    {
                        "client_name": "Other",
                        "id": "cccccccccccccccccccccccccccc3333",
                    },
                ]
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "AUTH_STORAGE_PATH", auth)
    module.record_manual_send(None)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["pushLog"][0]["devices"] == ["iPhone", "2222"]
    blob = path.read_text(encoding="utf-8")
    assert "super-secret" not in blob
    assert "3333" not in blob
