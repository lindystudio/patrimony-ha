"""House still. Off PresentationDocument. Always face.jpg."""

from __future__ import annotations

from pathlib import Path

from .const import PHOTO_FILE, PHOTO_MAX_BYTES

ALLOWED_TYPES = ("image/jpeg", "image/webp")


def photo_path(hass) -> Path:
    try:
        return Path(hass.config.path(PHOTO_FILE))
    except Exception:
        return Path("/config") / PHOTO_FILE


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


def load_photo(hass) -> bytes | None:
    path = photo_path(hass)
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except Exception:
        return None


def save_photo(hass, data: bytes) -> None:
    path = photo_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def delete_photo(hass) -> bool:
    path = photo_path(hass)
    try:
        if not path.is_file():
            return False
        path.unlink()
        return True
    except FileNotFoundError:
        return False
