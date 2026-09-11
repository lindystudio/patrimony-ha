"""House attention push. hek_ only. Never an HA token."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from .const import BACKEND_BASE, NOTIFY_FILE, NOTIFY_TITLE_MAX, NOTIFY_USER_AGENT

MISSING_KEY = {
    "error": {
        "code": "missing_house_event_key",
        "message": "Push needs the house event key",
    }
}


def notify_path(hass) -> Path:
    try:
        return Path(hass.config.path(NOTIFY_FILE))
    except Exception:
        return Path("/config") / NOTIFY_FILE


def is_usable_house_event_key(key: Any) -> bool:
    if key is None:
        return False
    text = str(key).strip()
    if not text.startswith("hek_") or len(text) < 5:
        return False
    if text.startswith("eyJ") or "eyJ" in text:
        return False
    if "token" in text.lower():
        return False
    return True


def clip_title(title: Any) -> str | None:
    text = "" if title is None else str(title).strip()
    if not text:
        return None
    if len(text) > NOTIFY_TITLE_MAX:
        text = text[:NOTIFY_TITLE_MAX]
    return text


def load_card_id(hass) -> str:
    path = notify_path(hass)
    try:
        if path.is_file():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                cid = str(data.get("cardId") or "").strip()
                if cid:
                    return cid
    except Exception:
        pass
    cid = str(uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cardId": cid}, indent=2) + "\n")
    return cid


def events_url(property_id: str) -> str:
    return f"{BACKEND_BASE}/v1/properties/{property_id}/events"


def post_house_event(property_id: str, key: str, card_id: str, title: str, timeout: int = 8) -> int:
    payload = json.dumps(
        {"cardId": card_id, "severity": "attention", "title": title}
    ).encode()
    req = Request(
        events_url(property_id),
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": NOTIFY_USER_AGENT,
            "X-House-Event-Key": key,
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200)
    except HTTPError as exc:
        return int(exc.code or 502)
    except (URLError, TimeoutError, OSError):
        return 502


def ingest_host() -> str:
    return urlparse(BACKEND_BASE).netloc or "collection ingest"


def probe_ingest(timeout: int = 6) -> bool:
    req = Request(
        BACKEND_BASE,
        method="GET",
        headers={"User-Agent": NOTIFY_USER_AGENT},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return True
    except HTTPError:
        return True
    except (URLError, TimeoutError, OSError):
        return False
