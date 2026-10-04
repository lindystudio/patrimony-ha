"""Version for GET /api/patrimony_collection/version.

Read from manifest.json once at import (Home Assistant imports integrations off the
event loop), so it always matches what HACS shows and requests never touch disk.
The add-on's config.yaml is bumped in lockstep with the manifest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_manifest_version() -> str:
    try:
        data = json.loads((Path(__file__).parent / "manifest.json").read_text())
    except Exception:
        return ""
    version = data.get("version")
    return version if isinstance(version, str) else ""


INTEGRATION_VERSION: str = _read_manifest_version()


def document() -> dict[str, Any]:
    return {"schemaVersion": 1, "version": INTEGRATION_VERSION}
