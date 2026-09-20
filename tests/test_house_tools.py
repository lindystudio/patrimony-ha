"""House tools stay off PresentationDocument. Photo, notes, pair, notify."""
from pathlib import Path

from custom_components.patrimony_collection.const import (
    BACKEND_BASE,
    NOTES_MAX_CHARS,
    PHOTO_MAX_BYTES,
)
from custom_components.patrimony_collection.mapping import (
    DEMO_ENTRY_DATA,
    DEMO_OPTIONS,
    build_presentation_document,
)
from custom_components.patrimony_collection.notes import document as notes_document
from custom_components.patrimony_collection.notes import load_notes, save_notes
from custom_components.patrimony_collection.notify import (
    MISSING_KEY,
    is_usable_house_event_key,
    load_card_id,
)
from custom_components.patrimony_collection.pair import (
    claim_html,
    house_url_error_message,
    house_url_is_usable,
    https_house_url,
    mint_error_message,
    mint_pairing,
    pairing_uri,
)
from custom_components.patrimony_collection.photo import (
    delete_photo,
    load_default_photo,
    load_photo,
    photo_path,
    reject_put,
    resolve_photo,
    restore_default_photo,
    save_photo,
    sniff_content_type,
)

JPEG = b"\xff\xd8\xff" + b"\x10" * 32
WEBP = b"RIFF" + (12).to_bytes(4, "little") + b"WEBP" + b"VP8 " 
SECRET_NOTE = "HOUSE_NOTE_SECRET_xyz"
SECRET_TOKEN = "llat-SECRET-pairing-token-not-hek"


class _Hass:
    def __init__(self, root: Path, external_url=None):
        self.config = type(
            "c",
            (),
            {
                "path": lambda _self, p, root=root: str(root / p),
                "external_url": external_url,
                "internal_url": None,
            },
        )()

    def now(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)


def test_photo_save_load_delete(tmp_path):
    hass = _Hass(tmp_path)
    assert load_photo(hass) is None
    assert sniff_content_type(JPEG) == "image/jpeg"
    assert sniff_content_type(WEBP) == "image/webp"
    save_photo(hass, JPEG)
    path = photo_path(hass)
    assert path.name == "face.jpg"
    assert path.is_file()
    assert load_photo(hass) == JPEG
    assert delete_photo(hass) is True
    assert load_photo(hass) is None
    assert delete_photo(hass) is False


def test_photo_get_falls_back_and_restore_writes_bundled_default(tmp_path):
    hass = _Hass(tmp_path)
    bundled = load_default_photo()
    assert bundled
    assert sniff_content_type(bundled) == "image/jpeg"
    assert len(bundled) <= PHOTO_MAX_BYTES

    resolved = resolve_photo(hass)
    assert resolved is not None
    data, ctype, source = resolved
    assert source == "default"
    assert ctype == "image/jpeg"
    assert data == bundled
    assert load_photo(hass) is None

    save_photo(hass, JPEG)
    data, ctype, source = resolve_photo(hass)
    assert source == "custom"
    assert data == JPEG

    assert delete_photo(hass) is True
    data, _ctype, source = resolve_photo(hass)
    assert source == "default"
    assert data == bundled
    assert load_photo(hass) is None

    assert restore_default_photo(hass) is True
    assert load_photo(hass) == bundled
    data, ctype, source = resolve_photo(hass)
    assert source == "custom"
    assert ctype == "image/jpeg"
    assert data == bundled

    save_photo(hass, JPEG)
    assert restore_default_photo(hass) is True
    assert load_photo(hass) == bundled


def test_photo_reject_too_big_and_wrong_type():
    assert reject_put("image/jpeg", JPEG) is None
    assert reject_put("image/webp", WEBP) is None
    assert reject_put("image/png", JPEG) is not None
    assert reject_put("text/plain", JPEG) is not None
    assert reject_put("image/jpeg", b"not-an-image") is not None
    huge = b"\xff\xd8\xff" + b"\x00" * PHOTO_MAX_BYTES
    assert len(huge) > PHOTO_MAX_BYTES
    assert reject_put("image/jpeg", huge) is not None


def test_notes_roundtrip_empty_and_cap(tmp_path):
    hass = _Hass(tmp_path)
    pid = DEMO_ENTRY_DATA["property_id"]
    missing = notes_document(hass, pid)
    assert missing == {"schemaVersion": 1, "propertyId": pid, "text": "", "updatedAt": None}
    saved = save_notes(hass, "", pid)
    assert saved["text"] == ""
    assert saved["schemaVersion"] == 1
    assert saved["updatedAt"]
    loaded = load_notes(hass)
    assert loaded["text"] == ""
    long_text = "n" * (NOTES_MAX_CHARS + 50)
    capped = save_notes(hass, long_text, pid)
    assert len(capped["text"]) == NOTES_MAX_CHARS
    again = notes_document(hass, pid)
    assert again["text"] == "n" * NOTES_MAX_CHARS
    assert again["propertyId"] == pid


def test_notes_photo_pairing_not_on_presentation_document(tmp_path):
    hass = _Hass(tmp_path)
    pid = DEMO_ENTRY_DATA["property_id"]
    save_notes(hass, SECRET_NOTE, pid)
    save_photo(hass, JPEG)
    uri = pairing_uri("https://ha.example.com", SECRET_TOKEN)
    doc = build_presentation_document(hass, DEMO_ENTRY_DATA, DEMO_OPTIONS)
    blob = str(doc)
    assert doc["schemaVersion"] == 1
    assert "notes" not in doc
    assert "photo" not in doc
    assert "pairing" not in doc
    assert "contacts" not in doc
    assert SECRET_NOTE not in blob
    assert SECRET_TOKEN not in blob
    assert "face.jpg" not in blob
    assert "data:image" not in blob
    assert uri not in blob
    assert "patrimony://pair" not in blob


def test_bundled_default_matches_addon_and_ui_copy_is_generic():
    bundled = load_default_photo()
    assert bundled
    root = Path(__file__).resolve().parents[1]
    addon = (root / "addons" / "patrimony_collection" / "default.jpg").read_bytes()
    assert addon == bundled
    for rel in (
        "custom_components/patrimony_collection/www/index.html",
        "addons/patrimony_collection/static/index.html",
    ):
        html = (root / rel).read_text(encoding="utf-8")
        assert "Restore default" in html
        low = html.lower()
        assert "copenhagen" not in low
        assert "nyhavn" not in low
        assert "soon_units" not in html
        assert "usesFahrenheit" not in html


def test_claim_html_opens_patrimony_collection() -> None:
    html = claim_html("patrimony://pair?url=https://ha.example.com")
    assert "Open Patrimony Collection" in html
    assert "Properties " + "Wallet" not in html


def test_house_url_is_usable_rejects_hostless_and_single_label():
    assert house_url_is_usable("") is False
    assert house_url_is_usable("https://") is False
    assert house_url_is_usable("https:///api/patrimony_collection/p/abc") is False
    assert house_url_is_usable("https://api/foo") is False
    assert house_url_is_usable("http://ha.example.com") is False
    assert house_url_is_usable("https://ha.example.com") is True
    assert house_url_is_usable("https://localhost") is True
    assert house_url_is_usable("https://127.0.0.1") is True


async def _mint_with_stub_auth(hass, calls=None):
    class _User:
        id = "u1"
        is_admin = True
        refresh_tokens = {}

    class _Auth:
        async def async_get_user(self, uid):
            return _User()

        async def async_create_refresh_token(self, *args, **kwargs):
            if calls is not None:
                calls["refresh"] = calls.get("refresh", 0) + 1
            return object()

        async def async_create_access_token(self, refresh):
            if calls is not None:
                calls["access"] = calls.get("access", 0) + 1
            return SECRET_TOKEN

    hass.auth = _Auth()
    return await mint_pairing(hass, _User())


def test_mint_refuses_missing_external_url(tmp_path):
    hass = _Hass(tmp_path, external_url=None)
    import asyncio

    calls = {}
    status, payload = asyncio.run(_mint_with_stub_auth(hass, calls))
    assert status == 503
    assert payload["error"]["code"] == "external_url_required"
    message = payload["error"]["message"]
    assert "House URL unusable: (empty)." in message
    assert "hass.config.external_url=None" in message
    assert "hass.config.internal_url=None" in message
    canned = (
        "Set Home Assistant External URL (Settings → System → Network) to your "
        "public https host, then show a pairing code again."
    )
    assert message != canned
    assert canned not in message
    assert calls.get("refresh", 0) == 0
    assert calls.get("access", 0) == 0


def test_mint_accepts_cloud_url_when_config_external_empty(tmp_path, monkeypatch):
    hass = _Hass(tmp_path, external_url=None)
    cloud = "https://abcd1234.ui.nabu.casa"
    monkeypatch.setattr(
        "custom_components.patrimony_collection.pair._try_ha_get_url",
        lambda _hass: cloud,
    )
    import asyncio

    calls = {}
    status, payload = asyncio.run(_mint_with_stub_auth(hass, calls))
    assert status == 200
    assert payload["pairing"].startswith(f"{cloud}/api/patrimony_collection/p/")
    assert calls.get("refresh", 0) == 1
    assert calls.get("access", 0) == 1


def test_try_ha_get_url_passes_prefer_external_and_allow_cloud(monkeypatch):
    import sys
    from types import ModuleType

    from custom_components.patrimony_collection import pair as pair_mod

    seen: dict[str, object] = {}

    def get_url(_hass, **kwargs):
        seen.update(kwargs)
        return "https://cloudstyle.ui.nabu.casa"

    net = ModuleType("homeassistant.helpers.network")
    net.get_url = get_url
    helpers = ModuleType("homeassistant.helpers")
    helpers.network = net
    ha = ModuleType("homeassistant")
    ha.helpers = helpers
    monkeypatch.setitem(sys.modules, "homeassistant", ha)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.network", net)

    assert pair_mod._try_ha_get_url(object()) == "https://cloudstyle.ui.nabu.casa"
    assert seen.get("prefer_external") is True
    assert seen.get("allow_cloud") is True


def test_https_house_url_falls_back_when_get_url_empty(tmp_path, monkeypatch):
    hass = _Hass(tmp_path, external_url="https://ha.example.com")
    monkeypatch.setattr(
        "custom_components.patrimony_collection.pair._try_ha_get_url",
        lambda _hass: None,
    )
    assert https_house_url(hass) == "https://ha.example.com"


def test_house_url_error_message_is_concrete(tmp_path):
    hass = _Hass(tmp_path, external_url=None)
    message = house_url_error_message(hass, "")
    assert message.startswith("House URL unusable: (empty).")
    assert "hass.config.external_url=None" in message
    canned = "Set Home Assistant External URL (Settings → System → Network)"
    assert canned not in message


def test_mint_includes_exception_type_and_text(tmp_path):
    hass = _Hass(tmp_path, external_url="https://ha.example.com")

    class _User:
        id = "u1"
        is_admin = True
        refresh_tokens = {}

    class _Auth:
        async def async_get_user(self, uid):
            return _User()

        async def async_create_refresh_token(self, *args, **kwargs):
            raise RuntimeError("refresh store locked")

        async def async_create_access_token(self, refresh):
            raise AssertionError("should not mint access")

    hass.auth = _Auth()
    import asyncio

    status, payload = asyncio.run(mint_pairing(hass, _User()))
    assert status == 501
    message = payload["error"]["message"]
    assert message == "RuntimeError: refresh store locked"
    assert message != "Pairing is unavailable"
    assert mint_error_message(RuntimeError("refresh store locked")) == message


def test_mint_accepts_example_https_host(tmp_path):
    hass = _Hass(tmp_path, external_url="https://ha.example.com")
    import asyncio

    calls = {}
    status, payload = asyncio.run(_mint_with_stub_auth(hass, calls))
    assert status == 200
    assert payload["pairing"].startswith("https://ha.example.com/api/patrimony_collection/p/")
    assert calls.get("refresh", 0) == 1
    assert calls.get("access", 0) == 1


def test_notify_refuses_missing_and_jwt_keys(tmp_path):
    assert is_usable_house_event_key(None) is False
    assert is_usable_house_event_key("") is False
    assert is_usable_house_event_key("hek_") is False
    assert is_usable_house_event_key("not-a-key") is False
    assert is_usable_house_event_key("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.sig") is False
    assert is_usable_house_event_key("hek_eyJhbGciOi") is False
    assert is_usable_house_event_key("Bearer token") is False
    assert is_usable_house_event_key("hek_house_ok") is True
    assert MISSING_KEY["error"]["code"] == "missing_house_event_key"
    hass = _Hass(tmp_path)
    first = load_card_id(hass)
    second = load_card_id(hass)
    assert first == second
    path = tmp_path / "patrimony_collection" / "notify.json"
    assert path.is_file()
    assert first in path.read_text()
    assert "mapping.json" not in path.name
    assert BACKEND_BASE == "https://api.patrimonycollection.com"


def test_pairing_helper_does_not_write_token(tmp_path):
    hass = _Hass(tmp_path, external_url="http://ha.example.com")
    url = https_house_url(hass)
    assert url.startswith("https://")
    uri = pairing_uri(url, SECRET_TOKEN)
    assert uri.startswith("patrimony://pair?")
    assert "url=" in uri and "token=" in uri
    import asyncio
    status, payload = asyncio.run(mint_pairing(hass, type("u", (), {"is_admin": True})()))
    assert status == 501
    root = tmp_path / "patrimony_collection"
    for name in ("mapping.json", "notes.json", "contacts.json"):
        path = root / name
        assert not path.exists()
        if path.exists():
            assert SECRET_TOKEN not in path.read_text()
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert SECRET_TOKEN.encode() not in data
                assert SECRET_TOKEN not in data.decode("utf-8", "ignore")


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        test_photo_save_load_delete(Path(d))
    test_photo_reject_too_big_and_wrong_type()
    with tempfile.TemporaryDirectory() as d:
        test_photo_get_falls_back_and_restore_writes_bundled_default(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_notes_roundtrip_empty_and_cap(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_notes_photo_pairing_not_on_presentation_document(Path(d))
    test_bundled_default_matches_addon_and_ui_copy_is_generic()
    test_house_url_is_usable_rejects_hostless_and_single_label()
    with tempfile.TemporaryDirectory() as d:
        test_mint_refuses_missing_external_url(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_mint_accepts_example_https_host(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_house_url_error_message_is_concrete(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_mint_includes_exception_type_and_text(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_notify_refuses_missing_and_jwt_keys(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_pairing_helper_does_not_write_token(Path(d))
    print("ok")
