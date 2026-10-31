"""0.4.112: owner session POST merges into ios_session.json.

Before: persist_ios_session rebuilt the file and kept only `clients`, wiping
member_api's memberWriteLog and every other top-level key.
"""

from __future__ import annotations

import json

from custom_components.patrimony_collection import clients as hc
from custom_components.patrimony_collection import ios_session as ios
from custom_components.patrimony_collection import member_api as ma
from custom_components.patrimony_collection import fileio


class _Hass:
    def __init__(self, root):
        self.data = {}
        self.config = type("c", (), {"path": lambda _s, p, root=root: str(root / p),
                                     "time_zone": "Europe/Paris"})()


SESSION = {"deviceName": "Owner iPhone", "appVersion": "1.2.3", "iosVersion": "19.0", "model": "iPhone17,1"}


def _doc(hass):
    return json.loads(ios.ios_session_path(hass).read_text())


def _seed(hass, doc):
    path = ios.ios_session_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    fileio.write_text(path, json.dumps(doc))


def test_member_write_log_and_unknown_keys_survive_owner_session_post(tmp_path):
    hass = _Hass(tmp_path)
    hc.upsert_client(hass, "owner-rt-1", device_name="Study iPhone")
    ma.record_member_write(hass, {"memberId": "aaaa1111bbbb2222"}, "contacts", "put")
    doc = _doc(hass)
    doc["futureKey"] = {"keep": [1, 2]}
    _seed(hass, doc)
    before_log = list(doc["memberWriteLog"])
    assert len(before_log) == 1
    body, status = ios.apply_ios_session_payload(hass, dict(SESSION), "192.168.1.5")
    assert status == 200 and body == {"ok": True}
    after = _doc(hass)
    assert after["memberWriteLog"] == before_log
    assert after["futureKey"] == {"keep": [1, 2]}
    assert [c["clientId"] for c in after["clients"]] == ["owner-rt-1"]
    assert after["deviceName"] == "Owner iPhone" and after["appVersion"] == "1.2.3"
    # A second post keeps it too, and the log keeps growing afterwards.
    ios.apply_ios_session_payload(hass, dict(SESSION, appVersion="1.2.4"), "192.168.1.5")
    ma.record_member_write(hass, {"memberId": "aaaa1111bbbb2222"}, "notes", "put")
    after = _doc(hass)
    assert len(after["memberWriteLog"]) == 2 and after["futureKey"] == {"keep": [1, 2]}
    assert after["appVersion"] == "1.2.4"


def test_session_fields_are_replaced_not_accumulated(tmp_path):
    hass = _Hass(tmp_path)
    ios.persist_ios_session(hass, device_name="A", app_version="1", ip="10.0.0.2",
                            ios_version="18", model="M", hostname="h.lan")
    ios.persist_ios_session(hass, device_name="B", app_version="2", ip="10.0.0.3")
    doc = _doc(hass)
    assert doc["deviceName"] == "B" and doc["ip"] == "10.0.0.3"
    assert "iosVersion" not in doc and "model" not in doc and "hostname" not in doc


def test_ptr_update_keeps_other_keys(tmp_path):
    hass = _Hass(tmp_path)
    _seed(hass, {"memberWriteLog": [{"at": "x"}], "futureKey": 1, "clients": []})
    ios.persist_ios_session(hass, device_name="A", app_version="1", ip="10.0.0.2")
    ios.store_resolved_hostname(hass, "10.0.0.2", "phone.lan.")
    doc = _doc(hass)
    assert doc["hostname"] == "phone.lan" and doc["memberWriteLog"] == [{"at": "x"}] and doc["futureKey"] == 1
    ios.store_resolved_hostname(hass, "10.0.0.9", "other.lan")  # stale IP: no write
    assert _doc(hass)["hostname"] == "phone.lan"


def test_unreadable_file_is_never_overwritten(tmp_path, monkeypatch):
    hass = _Hass(tmp_path)
    monkeypatch.setattr(fileio, "maybe_sleep", lambda *_a: None)
    path = ios.ios_session_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    fileio.write_text(path, "{not json")
    ios.persist_ios_session(hass, device_name="A", app_version="1", ip="10.0.0.2")
    assert path.read_text() == "{not json"
    ma.record_member_write(hass, {"memberId": "aaaa1111bbbb2222"}, "contacts", "put")
    assert path.read_text() == "{not json"
