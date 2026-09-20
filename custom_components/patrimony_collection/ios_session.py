"""Latest iOS connection. Off PresentationDocument. Last write wins.

Never logs the payload as a dump. Does not touch the house event key.
IP is the HA request peer only — never a client-supplied body field.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .const import (
    IOS_APP_VERSION_MAX,
    IOS_DEVICE_NAME_MAX,
    IOS_SESSION_FILE,
)

INVALID_IOS_SESSION = {
    "error": {
        "code": "bad_ios_session",
        "message": "deviceName and appVersion must be non-empty",
    }
}


def ios_session_path(hass) -> Path:
    try:
        return Path(hass.config.path(IOS_SESSION_FILE))
    except Exception:
        return Path("/config") / IOS_SESSION_FILE


def _utc_z(now: datetime | None = None) -> str:
    stamp = now if now is not None else datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def request_peer_ip(request: Any) -> str:
    """HA request peer. Never a body field."""
    remote = getattr(request, "remote", None)
    if remote is None:
        return ""
    return str(remote).strip()[:64]


def _field(value: Any, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_len:
        return None
    return text


def validate_ios_session_payload(payload: Any) -> tuple[str, str] | None:
    """Return (deviceName, appVersion) or None. Never echoes the body."""
    if not isinstance(payload, dict):
        return None
    device = _field(payload.get("deviceName"), IOS_DEVICE_NAME_MAX)
    version = _field(payload.get("appVersion"), IOS_APP_VERSION_MAX)
    if device is None or version is None:
        return None
    return device, version


def load_ios_session(hass) -> dict[str, Any] | None:
    path = ios_session_path(hass)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def document(hass) -> dict[str, Any]:
    raw = load_ios_session(hass)
    if raw is None:
        return {"seen": False}
    device = str(raw.get("deviceName") or "").strip()
    version = str(raw.get("appVersion") or "").strip()
    at = str(raw.get("lastInteractionAt") or "").strip()
    if not device or not version or not at:
        return {"seen": False}
    out: dict[str, Any] = {
        "seen": True,
        "deviceName": device,
        "appVersion": version,
        "lastInteractionAt": at,
    }
    ip = str(raw.get("ip") or "").strip()
    if ip:
        out["ip"] = ip
    return out


def persist_ios_session(
    hass,
    *,
    device_name: str,
    app_version: str,
    ip: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Overwrite last session. Callers must already have validated fields."""
    payload = {
        "deviceName": device_name,
        "appVersion": app_version,
        "ip": str(ip or "").strip(),
        "lastInteractionAt": at or _utc_z(),
    }
    path = ios_session_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def apply_ios_session_payload(
    hass,
    payload: Any,
    peer_ip: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], int]:
    """Validate and persist. IP is the request peer, not payload['ip']."""
    parsed = validate_ios_session_payload(payload)
    if parsed is None:
        return dict(INVALID_IOS_SESSION), 400
    device, version = parsed
    persist_ios_session(
        hass,
        device_name=device,
        app_version=version,
        ip=str(peer_ip or "").strip(),
        at=_utc_z(now),
    )
    return {"ok": True}, 200
