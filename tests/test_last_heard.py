"""Root lastHeard is additive on PresentationDocument; schemaVersion stays 1."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.patrimony_collection.mapping import (  # noqa: E402
    DEMO_ENTRY_DATA,
    DEMO_OPTIONS,
    build_presentation_document,
)


def test_last_heard_matches_generated_at_and_schema_stays_1():
    doc = build_presentation_document(
        None, DEMO_ENTRY_DATA, DEMO_OPTIONS, generated_at="2026-08-23T14:20:00Z"
    )
    assert doc["schemaVersion"] == 1
    assert doc["lastHeard"] == doc["generatedAt"] == "2026-08-23T14:20:00Z"
    assert doc["lastHeard"].endswith("Z")
    assert doc["property"]["lastHeard"] == doc["lastHeard"]
    assert doc.get("events") == []
    for card in doc.get("cards") or []:
        assert card.get("kind") == "custom"
        assert "lastHeard" not in card
        assert card.get("lastUpdated")
        for item in card.get("items") or []:
            assert "lastHeard" not in item


if __name__ == "__main__":
    test_last_heard_matches_generated_at_and_schema_stays_1()
    print("ok")
