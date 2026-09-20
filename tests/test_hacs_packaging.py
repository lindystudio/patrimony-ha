"""HACS custom-repository layout stays complete for the public Lindy Studio copy."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from custom_components.patrimony_collection.pair import claim_html

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "patrimony_collection"
PUBLIC_REPO = "https://github.com/lindystudio/patrimony-ha"
PUBLIC_ISSUES = "https://github.com/lindystudio/patrimony-ha/issues"
FORBIDDEN_HOUSE_NAMES = ("copenhagen", "halkidiki")
# Split so a repo-wide grep for the retired names stays empty.
FORBIDDEN_PUBLIC_NAMES = (
    "petros" + "beli",
    "open" + "claw",
    "properties" + "wallet",
    "properties " + "wallet",
)
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico"}


def test_hacs_json_and_brand_icon() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == "Patrimony Collection"
    assert hacs.get("homeassistant")
    assert (ROOT / "LICENSE").is_file()
    for icon in (
        ROOT / "brand" / "icon.png",
        COMPONENT / "brand" / "icon.png",
    ):
        assert icon.is_file()
        assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_validate_workflow_keeps_hacs_job_with_private_ignores() -> None:
    text = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    assert "hacs/action@" in text
    assert "category: integration" in text
    ignore_line = next(line for line in text.splitlines() if line.strip().startswith("ignore:"))
    for check in ("hacsjson", "integration_manifest", "license", "topics"):
        assert check in ignore_line


def test_manifest_hacs_required_keys() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "patrimony_collection"
    assert manifest["name"] == "Patrimony Collection"
    assert manifest["version"] == "0.4.52"
    assert manifest["documentation"] == PUBLIC_REPO
    assert manifest["issue_tracker"] == PUBLIC_ISSUES
    assert manifest["codeowners"] == ["@lindystudio"]
    owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "@lindystudio" in owners
    assert "@" + "petros" + "beli" not in owners
    panel = (COMPONENT / "panel.py").read_text(encoding="utf-8")
    assert 'sidebar_icon="mdi:key"' in panel
    assert "mdi:wallet-travel" not in panel


def test_customer_docs_point_at_public_org_and_stay_scrubbed() -> None:
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    component_readme = (COMPONENT / "README.md").read_text(encoding="utf-8")
    for text in (root_readme, component_readme):
        lowered = text.lower()
        assert PUBLIC_REPO in text
        assert "custom repository" in lowered or "custom repositories" in lowered
        assert "integration" in lowered
        assert "when published" not in lowered
        assert "until then" not in lowered
        for name in FORBIDDEN_HOUSE_NAMES:
            assert name not in lowered
        assert "demo home" in lowered
        assert "utc" in lowered
    assert PUBLIC_ISSUES in root_readme


def test_pairing_claim_page_uses_patrimony_collection() -> None:
    html = claim_html("patrimony://pair?url=https://ha.example.com")
    assert "Open Patrimony Collection" in html
    assert "Properties " + "Wallet" not in html
    assert "Properties" + "Wallet" not in html


def test_tracked_files_have_no_private_org_or_legacy_product_names() -> None:
    files = subprocess.check_output(
        ["git", "ls-files"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    hits: list[str] = []
    for rel in files:
        path = ROOT / rel
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lowered = text.lower()
        for token in FORBIDDEN_PUBLIC_NAMES:
            if token in lowered:
                hits.append(f"{rel}: {token}")
    assert hits == [], "forbidden public names remain:\n" + "\n".join(hits)
