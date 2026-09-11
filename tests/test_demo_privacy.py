"""Offline privacy check for the demo PresentationDocument fixture."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.patrimony_collection.const import FORBIDDEN_WIRE_KEYS  # noqa: E402

FIXTURE = ROOT / "fixtures" / "demo-state.json"


def _walk(obj, found: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_WIRE_KEYS or str(k).endswith("_entity_id"):
                found.append(str(k))
            _walk(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, found)


def test_demo_fixture_has_no_entity_geo_or_token_keys() -> None:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    found: list[str] = []
    _walk(doc, found)
    assert not found, found
    assert doc["schemaVersion"] == 1
    assert doc["property"]["id"] == "00000000-0000-4000-8000-000000000001"
    assert doc["property"]["displayName"] == "Demo Home"
    blob = json.dumps(doc)
    assert "entity_id" not in blob


if __name__ == "__main__":
    test_demo_fixture_has_no_entity_geo_or_token_keys()
    print("ok")
