"""0.4.94 regression: notify.json writers must never wipe the push log.

A field house lost its push log (and eventKeyStatus.installedAt) because writers
did non-atomic read-modify-write and treated an unreadable/torn file as {}.
"""
from __future__ import annotations

import json
import threading

from custom_components.patrimony_collection import notify as notify_mod

from tests.test_clients import _Hass


def _seed(hass):
    path = notify_mod.notify_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "cardId": "card-1",
        "attention": {"seeded": True, "classes": {}, "pushed": {}},
        "eventKeyStatus": {"lastPushStatus": None, "installedAt": "2026-10-10T19:42:37+02:00"},
        "pushLog": [{"at": "2026-10-10T19:42:40+02:00", "fire_class": "manual.send", "kind": "manual", "status": 202, "ok": True}],
    }
    path.write_text(json.dumps(doc))
    return path


def _no_sleep(monkeypatch):
    monkeypatch.setattr(notify_mod.time, "sleep", lambda _s: None)


def test_unreadable_file_is_not_overwritten_by_any_writer(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    hass = _Hass(tmp_path)
    path = _seed(hass)
    torn = path.read_text()[:40]  # what a reader saw mid-write before 0.4.94
    path.write_text(torn)
    notify_mod.save_attention_store(hass, {"seeded": True, "classes": {}, "pushed": {}})
    notify_mod.note_event_key_attempt(hass, {"status": 202, "ok": True})
    notify_mod.mark_event_key_installed(hass)
    notify_mod.record_push(hass, kind="attention", rule="security.leak", outcome={"status": 202, "ok": True})
    notify_mod.load_card_id(hass)
    notify_mod.push_log_document(hass)
    assert path.read_text() == torn


def test_writers_preserve_log_card_and_installed_at(tmp_path):
    hass = _Hass(tmp_path)
    path = _seed(hass)
    notify_mod.save_attention_store(hass, {"seeded": True, "classes": {}, "pushed": {"x": 1.0}})
    notify_mod.note_event_key_attempt(hass, {"status": 202, "ok": True})
    assert notify_mod.load_card_id(hass) == "card-1"
    notify_mod.record_push(hass, kind="attention", rule="security.leak", outcome={"status": 202, "ok": True})
    doc = json.loads(path.read_text())
    assert doc["cardId"] == "card-1"
    assert doc["eventKeyStatus"]["installedAt"] == "2026-10-10T19:42:37+02:00"
    assert [r["fire_class"] for r in doc["pushLog"]] == ["manual.send", "security.leak"]
    assert not list(path.parent.glob(".*.tmp"))


def test_concurrent_writers_never_lose_rows(tmp_path):
    hass = _Hass(tmp_path)
    path = _seed(hass)
    errors: list[BaseException] = []

    def pusher(n):
        try:
            for i in range(10):
                notify_mod.record_push(hass, kind="attention", rule=f"r.{n}.{i}", outcome={"status": 202, "ok": True})
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def churn():
        try:
            for _ in range(40):
                notify_mod.save_attention_store(hass, {"seeded": True, "classes": {}, "pushed": {}})
                notify_mod.note_event_key_attempt(hass, {"status": 202, "ok": True})
                notify_mod.push_log_document(hass)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=pusher, args=(n,)) for n in range(4)] + [threading.Thread(target=churn) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    doc = json.loads(path.read_text())
    assert len(doc["pushLog"]) == 41  # 1 seeded + 40 new, under PUSH_LOG_MAX
    assert doc["cardId"] == "card-1"
    assert doc["eventKeyStatus"]["installedAt"]


def test_retention_is_last_50_rows(tmp_path):
    hass = _Hass(tmp_path)
    _seed(hass)
    for i in range(60):
        notify_mod.record_push(hass, kind="attention", rule=f"r.{i}", outcome={"status": 202, "ok": True})
    rows = notify_mod.push_log_document(hass)["rows"]
    assert len(rows) == notify_mod.PUSH_LOG_MAX == 50
    assert rows[-1]["fire_class"] == "r.59"
