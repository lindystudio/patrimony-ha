"""Latest iOS connection. Off PresentationDocument. Last write wins.

Never logs the payload as a dump. Does not touch the house event key.
IP is the HA request peer only — never a client-supplied body field.
Reverse DNS is best-effort and must never block the POST heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .const import (
    IOS_APP_VERSION_MAX,
    IOS_DEVICE_NAME_MAX,
    IOS_DNS_TIMEOUT_SECONDS,
    IOS_HOSTNAME_MAX,
    IOS_IOS_VERSION_MAX,
    IOS_MODEL_MAX,
    IOS_SESSION_FILE,
)

INVALID_IOS_SESSION = {
    "error": {
        "code": "bad_ios_session",
        "message": "deviceName and appVersion must be non-empty",
    }
}

GENERIC_DEVICE_NAMES = frozenset({"iphone", "ipad"})


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


def is_generic_device_name(name: str | None) -> bool:
    """True for empty / iPhone / iPad (case-insensitive)."""
    return (name or "").strip().casefold() in {"", *GENERIC_DEVICE_NAMES}


def display_device(
    device_name: str | None,
    ios_version: str | None = None,
    model: str | None = None,
) -> str:
    """Connections Device line. Generic names need iosVersion or model."""
    name = (device_name or "").strip()
    ios = (ios_version or "").strip()
    mdl = (model or "").strip()
    if not is_generic_device_name(name):
        if ios:
            return f"{name} · {ios}"
        return name
    if name and ios:
        return f"{name} · {ios}"
    if ios:
        return ios
    if mdl:
        return mdl
    return name


def display_host(ip: str | None, hostname: str | None = None) -> str:
    """Connections IP / host line: `hostname · IP`, or IP when PTR is missing."""
    addr = (ip or "").strip()
    host = (hostname or "").strip().rstrip(".")
    if host and addr:
        return f"{host} · {addr}"
    return addr or host


def validate_ios_session_payload(payload: Any) -> dict[str, str] | None:
    """Required deviceName+appVersion. Optional iosVersion+model. Ignore extras/ip."""
    if not isinstance(payload, dict):
        return None
    device = _field(payload.get("deviceName"), IOS_DEVICE_NAME_MAX)
    version = _field(payload.get("appVersion"), IOS_APP_VERSION_MAX)
    if device is None or version is None:
        return None
    out: dict[str, str] = {"deviceName": device, "appVersion": version}
    ios = _field(payload.get("iosVersion"), IOS_IOS_VERSION_MAX)
    if ios:
        out["iosVersion"] = ios
    model = _field(payload.get("model"), IOS_MODEL_MAX)
    if model:
        out["model"] = model
    return out


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
    ios = str(raw.get("iosVersion") or "").strip()
    model = str(raw.get("model") or "").strip()
    ip = str(raw.get("ip") or "").strip()
    hostname = str(raw.get("hostname") or "").strip().rstrip(".")
    out: dict[str, Any] = {
        "seen": True,
        "deviceName": device,
        "appVersion": version,
        "lastInteractionAt": at,
        "displayDevice": display_device(device, ios or None, model or None),
        "displayHost": display_host(ip, hostname or None),
    }
    if ip:
        out["ip"] = ip
    if hostname:
        out["hostname"] = hostname
    if ios:
        out["iosVersion"] = ios
    if model:
        out["model"] = model
    return out


def persist_ios_session(
    hass,
    *,
    device_name: str,
    app_version: str,
    ip: str,
    at: str | None = None,
    ios_version: str | None = None,
    model: str | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Overwrite last session. Callers must already have validated fields."""
    payload: dict[str, Any] = {
        "deviceName": device_name,
        "appVersion": app_version,
        "ip": str(ip or "").strip(),
        "lastInteractionAt": at or _utc_z(),
    }
    if ios_version:
        payload["iosVersion"] = ios_version
    if model:
        payload["model"] = model
    if hostname:
        payload["hostname"] = hostname
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
    """Validate and persist. IP is the request peer, not payload['ip']. No DNS."""
    parsed = validate_ios_session_payload(payload)
    if parsed is None:
        return dict(INVALID_IOS_SESSION), 400
    persist_ios_session(
        hass,
        device_name=parsed["deviceName"],
        app_version=parsed["appVersion"],
        ip=str(peer_ip or "").strip(),
        at=_utc_z(now),
        ios_version=parsed.get("iosVersion"),
        model=parsed.get("model"),
    )
    return {"ok": True}, 200


def lookup_hostname(ip: str, timeout: float = IOS_DNS_TIMEOUT_SECONDS) -> str | None:
    """Best-effort PTR. Never raises. Short timeout. None on fail/timeout."""
    text = str(ip or "").strip()
    if not text:
        return None
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        host, _aliases, _addrs = pool.submit(socket.gethostbyaddr, text).result(
            timeout=timeout
        )
    except Exception:
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    name = str(host or "").strip().rstrip(".")
    if not name or name == text or len(name) > IOS_HOSTNAME_MAX:
        return None
    return name


def store_resolved_hostname(hass, ip: str, hostname: str) -> None:
    """Attach PTR only if the last session still has this peer IP."""
    raw = load_ios_session(hass)
    if not isinstance(raw, dict):
        return
    if str(raw.get("ip") or "").strip() != str(ip or "").strip():
        return
    host = (hostname or "").strip().rstrip(".")
    if not host or host == str(ip or "").strip() or len(host) > IOS_HOSTNAME_MAX:
        return
    raw["hostname"] = host
    path = ios_session_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2) + "\n")


async def async_resolve_and_store_hostname(hass, ip: str) -> None:
    """Background PTR. Fail-open. Must not be awaited by POST."""
    text = str(ip or "").strip()
    if not text:
        return
    host: str | None = None
    job = getattr(hass, "async_add_executor_job", None)
    try:
        if job:
            host = await asyncio.wait_for(
                job(lookup_hostname, text),
                timeout=IOS_DNS_TIMEOUT_SECONDS + 0.25,
            )
        else:
            host = lookup_hostname(text)
    except Exception:
        return
    if host:
        store_resolved_hostname(hass, text, host)


def schedule_reverse_lookup(hass, ip: str) -> None:
    """Fire-and-forget PTR. Never blocks. No-op when HA has no task helper."""
    text = str(ip or "").strip()
    if not text:
        return
    coro = async_resolve_and_store_hostname(hass, text)
    create_bg = getattr(hass, "async_create_background_task", None)
    if callable(create_bg):
        try:
            create_bg(coro, "patrimony_ios_ptr")
            return
        except Exception:
            pass
    create = getattr(hass, "async_create_task", None)
    if callable(create):
        try:
            create(coro)
            return
        except Exception:
            pass
    try:
        coro.close()
    except Exception:
        pass
