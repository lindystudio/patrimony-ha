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


def test_normalize_unknown_method_becomes_cellular():
    rows = normalize_list([{"function": "X", "name": "Y", "tel": "1", "method": "telegram"}])
    assert rows[0]["method"] == "cellular"


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        test_order_and_methods(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_empty_file_is_empty_list(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_not_on_presentation_document(Path(d))
    test_normalize_unknown_method_becomes_cellular()
    print("ok")
