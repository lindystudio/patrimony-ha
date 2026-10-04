"""Contacts stay off PresentationDocument. Order is array order."""
from pathlib import Path

from custom_components.patrimony_collection.contacts import (
    document,
    load_contacts,
    normalize_list,
    save_contacts,
)
from custom_components.patrimony_collection.mapping import (
    DEMO_ENTRY_DATA,
    DEMO_OPTIONS,
    build_presentation_document,
)


class _Hass:
    def __init__(self, root: Path):
        self.config = type("c", (), {"path": lambda _self, p, root=root: str(root / p)})()

    def now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)


def test_order_and_methods(tmp_path):
    hass = _Hass(tmp_path)
    rows = [
        {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "function": "Gardener", "name": "Mike Brown", "tel": "+4512345678", "method": "cellular"},
        {"function": "Pool", "name": "Ada", "tel": "123", "method": "whatsapp"},
        {"function": "", "name": "", "tel": "", "method": "fax"},
    ]
    saved = save_contacts(hass, rows)
    assert [r["function"] for r in saved] == ["Gardener", "Pool"]
    assert saved[0]["method"] == "cellular"
    assert saved[1]["method"] == "whatsapp"
    assert saved[1]["id"]
    loaded = load_contacts(hass)
    assert loaded == saved
    path = tmp_path / "patrimony_collection" / "contacts.json"
    assert path.is_file()
    assert "mapping.json" not in path.name


def test_empty_file_is_empty_list(tmp_path):
    hass = _Hass(tmp_path)
    assert load_contacts(hass) == []
    doc = document(hass, "00000000-0000-4000-8000-000000000001")
    assert doc == {"schemaVersion": 1, "propertyId": "00000000-0000-4000-8000-000000000001", "contacts": []}


def test_not_on_presentation_document(tmp_path):
    hass = _Hass(tmp_path)
    save_contacts(hass, [{"function": "Gardener", "name": "Mike", "tel": "+45", "method": "viber"}])
    doc = build_presentation_document(hass, DEMO_ENTRY_DATA, DEMO_OPTIONS)
    blob = str(doc)
    assert "contacts" not in doc
    assert "Gardener" not in blob
    assert "Mike" not in blob


def test_app_is_kept_and_method_stays_legacy():
    rows = normalize_list([
        {"function": "Boat", "tel": "1", "app": "telegram"},
        {"function": "Pool", "tel": "2", "method": "whatsapp"},
        {"function": "Gate", "tel": "3", "method": "telegram"},
        {"function": "Odd", "tel": "4", "app": "Not An App!"},
    ])
    assert [(r["method"], r["app"]) for r in rows] == [
        ("cellular", "telegram"),
        ("whatsapp", "whatsapp"),
        ("cellular", "telegram"),
        ("cellular", "cellular"),
    ]


def test_older_phone_save_keeps_a_newer_phones_app(tmp_path):
    hass = _Hass(tmp_path)
    cid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    save_contacts(hass, [{"id": cid, "function": "Boat", "tel": "1", "method": "cellular", "app": "signal"}])
    # An older app build round-trips only `method`.
    saved = save_contacts(hass, [{"id": cid, "function": "Boat captain", "tel": "1", "method": "cellular"}])
    assert saved[0]["function"] == "Boat captain"
    assert saved[0]["app"] == "signal"
    # Choosing a different legacy method on the older phone wins.
    saved = save_contacts(hass, [{"id": cid, "function": "Boat captain", "tel": "1", "method": "viber"}])
    assert (saved[0]["method"], saved[0]["app"]) == ("viber", "viber")


def test_cap_allows_long_lists():
    rows = normalize_list([{"function": f"Person {i}", "tel": str(i)} for i in range(250)])
    assert len(rows) == 200


def test_addon_web_contacts_match_the_integration(tmp_path):
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("patrimony_addon_web_contacts", root / "addons" / "patrimony_collection" / "web.py")
    web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web)
    web.CONTACTS_PATH = tmp_path / "contacts.json"
    cid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    doc = web.save_contacts({"contacts": [{"id": cid, "function": "Boat", "tel": "1", "app": "telegram"}]})
    assert (doc["contacts"][0]["method"], doc["contacts"][0]["app"]) == ("cellular", "telegram")
    doc = web.save_contacts({"contacts": [{"id": cid, "function": "Boat", "tel": "1", "method": "cellular"}]})
    assert doc["contacts"][0]["app"] == "telegram"
    assert web.load_contacts()["contacts"][0]["app"] == "telegram"


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        test_order_and_methods(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_empty_file_is_empty_list(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_not_on_presentation_document(Path(d))
    test_app_is_kept_and_method_stays_legacy()
    test_cap_allows_long_lists()
    print("ok")
