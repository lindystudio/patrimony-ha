"""0.4.99: no blocking file I/O on the HA event-loop thread for any Patrimony store."""

from __future__ import annotations

import builtins
import json
import os
import pathlib
import threading
import time
import types

import pytest

from custom_components.patrimony_collection import chat, contacts, fileio, mapping, members, notes, notify, photo
from custom_components.patrimony_collection.pq_shield import session as pqs


class _Hass:
    def __init__(self, root):
        self.data = {}
        self.config = types.SimpleNamespace(path=lambda p, root=root: str(root / p))


def _flush():
    fileio._WRITER.submit(lambda: None).result()


@pytest.fixture
def loop_guard(monkeypatch):
    """Pretend THIS thread is HA's loop; any blocking call on it fails the test."""
    me = threading.get_ident()
    calls = []

    def guard(name, orig):
        def wrapped(*a, **k):
            if threading.get_ident() == me:
                calls.append(name)
                raise AssertionError(f"blocking {name} on event loop")
            return orig(*a, **k)
        return wrapped

    def arm():
        fileio.set_loop_thread(me)
        monkeypatch.setattr(builtins, "open", guard("open", builtins.open))
        for n in ("read_text", "read_bytes", "write_text", "write_bytes", "open"):
            monkeypatch.setattr(pathlib.Path, n, guard("Path." + n, getattr(pathlib.Path, n)))
        monkeypatch.setattr(time, "sleep", guard("sleep", time.sleep))

    yield arm
    fileio.set_loop_thread(None)
    _flush()
    assert calls == []


def _seed(root):
    d = root / "patrimony_collection"
    d.mkdir(parents=True)
    (d / "mapping.json").write_text(json.dumps({"cards": [{"id": "c1"}], "mappings": []}))
    (d / "notify.json").write_text(json.dumps({"cardId": "x", "pushLog": []}))
    (d / "members.json").write_text(json.dumps({"version": 1, "members": {}}))
    (d / "chat.json").write_text(json.dumps({"schemaVersion": 1, "messages": []}))
    return d


def test_loop_reads_and_writes_never_block(tmp_path, loop_guard):
    hass = _Hass(tmp_path)
    d = _seed(tmp_path)
    fileio.warm_paths([str(d)])
    loop_guard()
    # mapping.json: every reader + the seed writer
    assert mapping.load_shared_mapping(hass)["cards"] == [{"id": "c1"}]
    # notify / members / chat / contacts / notes / photo / pq store, read + write on loop
    assert notify._read_notify_file(d / "notify.json")["cardId"] == "x"
    notify._write_notify_file(d / "notify.json", {"cardId": "x", "pushLog": [1]})
    assert notify._read_notify_file(d / "notify.json")["pushLog"] == [1]
    members._write(d / "members.json", {"version": 1, "members": {"a": {}}})
    assert members._read(d / "members.json")["members"] == {"a": {}}
    st = chat._read_store(hass)
    chat._write_store(hass, st)
    contacts.save_contacts(hass, [])
    assert contacts.load_contacts(hass) == []
    notes.save_notes(hass, "hi", "prop")
    assert notes.load_notes(hass)["text"] == "hi"
    photo.save_photo(hass, b"\xff\xd8\xff" + b"0" * 10)
    assert photo.load_photo(hass).startswith(b"\xff\xd8")
    assert photo.delete_photo(hass) is True
    mapping.seed_shared_mapping(hass, {}, {"cards": [{"id": "c2"}], "mappings": []})  # existing -> no write
    mgr = pqs.PQShieldManager.__new__(pqs.PQShieldManager)
    mgr.hass = hass
    mgr._write_disk({"version": 1})
    assert mgr._read_disk() == {"version": 1}


def test_loop_writes_land_atomically_in_order_0600(tmp_path, loop_guard):
    hass = _Hass(tmp_path)
    d = _seed(tmp_path)
    fileio.warm_paths([str(d)])
    loop_guard()
    for i in range(20):
        notify._write_notify_file(d / "notify.json", {"n": i})
    fileio.set_loop_thread(None)
    _flush()
    on_disk = fileio._WRITER.submit((d / "notify.json").read_text).result()  # read off this thread
    assert json.loads(on_disk) == {"n": 19}
    assert (os.stat(d / "notify.json").st_mode & 0o777) == 0o600
    assert [p.name for p in d.iterdir() if p.name.endswith(".tmp")] == []


def test_external_mapping_change_refreshes_off_loop(tmp_path, loop_guard):
    hass = _Hass(tmp_path)
    d = _seed(tmp_path)
    fileio.warm_paths([str(d)])
    # The add-on rewrites mapping.json behind our back (different size => new signature).
    (d / "mapping.json").write_text(json.dumps({"cards": [{"id": "c1"}, {"id": "c9"}], "mappings": []}))
    loop_guard()
    first = mapping.load_shared_mapping(hass)  # stale, never blocks; refresh scheduled
    assert first["cards"][0]["id"] == "c1"
    for _ in range(100):
        if len(mapping.load_shared_mapping(hass)["cards"]) == 2:
            break
        threading.Event().wait(0.01)
    assert [c["id"] for c in mapping.load_shared_mapping(hass)["cards"]] == ["c1", "c9"]


def test_missing_file_on_loop_is_none_without_open(tmp_path, loop_guard):
    hass = _Hass(tmp_path)
    loop_guard()
    assert notes.load_notes(hass) is None
    assert mapping.load_shared_mapping(hass) is None


def test_off_loop_write_is_durable_before_return(tmp_path):
    p = tmp_path / "x.json"
    fileio.write_text(p, "{}\n")
    assert p.read_text() == "{}\n" and (os.stat(p).st_mode & 0o777) == 0o600
