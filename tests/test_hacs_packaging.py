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
    # Offline job is pytest-only; config_flow must stay importable without voluptuous.
    assert ("pip install -q pytest" in text) or ("pip install pytest" in text)
    assert "voluptuous" not in text


def test_manifest_hacs_required_keys() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "patrimony_collection"
    assert manifest["name"] == "Patrimony Collection"
    assert manifest["version"] == "0.4.63"
    addon_config = (ROOT / "addons" / "patrimony_collection" / "config.yaml").read_text(
        encoding="utf-8"
    )
    addon_version = next(
        line for line in addon_config.splitlines() if line.startswith("version:")
    ).split(":", 1)[1].strip().strip('"')
    assert addon_version == manifest["version"]
    assert "house_event_key:" not in addon_config
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    assert "house_event_key" not in strings["options"]["step"]["init"]["menu_options"]
    assert "house_event_key" not in strings["options"]["step"]
    assert manifest["documentation"] == PUBLIC_REPO
    assert manifest["issue_tracker"] == PUBLIC_ISSUES
    assert manifest["codeowners"] == ["@lindystudio"]
    owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "@lindystudio" in owners
    assert "@" + "petros" + "beli" not in owners
    panel = (COMPONENT / "panel.py").read_text(encoding="utf-8")
    assert 'sidebar_icon="mdi:key"' in panel
    assert "mdi:wallet-travel" not in panel


def test_connections_panel_shows_latest_ios_connection() -> None:
    html = (COMPONENT / "www" / "index.html").read_text(encoding="utf-8")
    assert ">Latest iOS connection<" in html
    assert ">Device<" in html
    assert ">IP / host<" in html
    assert ">App version<" in html
    assert ">Last interaction<" in html
    assert 'id="conn_ios_ip"' in html
    assert 'id="conn_ios_name"' in html
    assert 'id="conn_ios_ver"' in html
    assert 'id="conn_ios_at"' in html
    assert "function applyIosSession" in html
    assert "function formatIosDevice" in html
    assert "function formatIosHost" in html
    assert "function isGenericDeviceName" in html
    assert "doc.displayDevice" in html
    assert "doc.displayHost" in html
    assert "doc.iosVersion" in html
    assert "doc.model" in html
    assert "modelIdentifier" not in html
    assert "/api/patrimony_collection/ios_session" in html
    assert 'setConn("conn_ios_ip", "Missing", "warn")' in html
    assert "street address" not in html.lower()
    name = html.find('id="conn_ios_name"')
    host = html.find('id="conn_ios_ip"')
    ver = html.find('id="conn_ios_ver"')
    at = html.find('id="conn_ios_at"')
    assert 0 <= name < host < ver < at


def test_connections_panel_shows_external_url() -> None:
    html = (COMPONENT / "www" / "index.html").read_text(encoding="utf-8")
    assert ">External URL<" in html
    assert 'id="conn_url"' in html
    ha = html.find('id="conn_ha"')
    url = html.find('id="conn_url"')
    hek = html.find('id="conn_hek"')
    assert 0 <= ha < url < hek
    assert "function applyHouseUrl" in html
    assert "doc.externalUrl || doc.houseUrl" in html
    assert 'setConn("conn_url", ok ? url : "Missing"' in html


def test_panel_show_pairing_reads_error_message() -> None:
    """Soon Invitation must surface API error.message (e.g. 503 External URL)."""
    for rel in (
        COMPONENT / "www" / "index.html",
        ROOT / "addons" / "patrimony_collection" / "static" / "index.html",
    ):
        html = rel.read_text(encoding="utf-8")
        assert "error.message" in html
        assert "r.status === 501 || !r.ok" not in html
        assert "Pairing is unavailable." in html


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
