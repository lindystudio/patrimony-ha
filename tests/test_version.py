"""GET /api/patrimony_collection/version reports the manifest version."""

import json
from pathlib import Path

from custom_components.patrimony_collection.const import VERSION_PATH
from custom_components.patrimony_collection.http import PatrimonyVersionView
from custom_components.patrimony_collection.version import INTEGRATION_VERSION, document

ROOT = Path(__file__).resolve().parents[1]


def test_version_matches_manifest_and_addon():
    manifest = json.loads((ROOT / "custom_components" / "patrimony_collection" / "manifest.json").read_text())
    assert INTEGRATION_VERSION == manifest["version"]
    assert document() == {"schemaVersion": 1, "version": manifest["version"]}
    config = (ROOT / "addons" / "patrimony_collection" / "config.yaml").read_text()
    assert f'version: "{manifest["version"]}"' in config


def test_version_view_requires_auth():
    assert PatrimonyVersionView.url == VERSION_PATH == "/api/patrimony_collection/version"
    assert PatrimonyVersionView.requires_auth is True
