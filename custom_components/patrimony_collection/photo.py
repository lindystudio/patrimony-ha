"""House still. Off PresentationDocument. Stored as face.jpg; bundled default if missing."""

from __future__ import annotations

from pathlib import Path

from .const import PHOTO_FILE, PHOTO_MAX_BYTES

ALLOWED_TYPES = ("image/jpeg", "image/webp")
DEFAULT_PHOTO_PATH = Path(__file__).resolve().parent / "assets" / "default.jpg"


def photo_path(hass) -> Path:
    try:
        return Path(hass.config.path(PHOTO_FILE))
    except Exception:
        return Path("/config") / PHOTO_FILE


def default_photo_path() -> Path:
    return DEFAULT_PHOTO_PATH


def sniff_content_type(data: bytes) -> str | None:
    if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def reject_put(content_type: str | None, data: bytes) -> str | None:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in ALLOWED_TYPES:
        return "image/jpeg or image/webp"
    if not data or len(data) > PHOTO_MAX_BYTES:
        return "max 2 MB"
    sniffed = sniff_content_type(data)
    if sniffed is None or sniffed != ctype:
        return "image/jpeg or image/webp"
    return None


def load_default_photo() -> bytes | None:
    path = default_photo_path()
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except Exception:
        return None
    if not data or len(data) > PHOTO_MAX_BYTES:
        return None
    if sniff_content_type(data) is None:
        return None
    return data


def load_photo(hass) -> bytes | None:
    path = photo_path(hass)
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except Exception:
        return None


def resolve_photo(hass) -> tuple[bytes, str, str] | None:
    """Return (bytes, content_type, source) for the stored still, else the bundled default."""
    stored = load_photo(hass)
    if stored:
        return stored, sniff_content_type(stored) or "image/jpeg", "custom"
    bundled = load_default_photo()
    if bundled:
        return bundled, sniff_content_type(bundled) or "image/jpeg", "default"
    return None


def save_photo(hass, data: bytes) -> None:
    path = photo_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def restore_default_photo(hass) -> bool:
    data = load_default_photo()
    if not data:
        return False
    save_photo(hass, data)
    return True


def delete_photo(hass) -> bool:
    path = photo_path(hass)
    try:
        if not path.is_file():
            return False
        path.unlink()
        return True
    except FileNotFoundError:
        return False
