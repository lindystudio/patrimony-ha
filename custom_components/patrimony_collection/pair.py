"""Mint a Patrimony iOS long-lived token. Never persist it in the clear."""

from __future__ import annotations

import html
import inspect
import logging
import secrets
import time
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlparse

from .const import (
    DEFAULT_HOUSE_URL,
    DOMAIN,
    PAIR_CLIENT_NAME,
    PAIR_TICKET_TTL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

UNAVAILABLE = {
    "error": {"code": "unavailable", "message": "Pairing is unavailable"}
}

_HOSTLESS_HTTPS = frozenset({"", "https://", "http://", "https:///", "http:///"})


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


CONF_HOUSE_URL_OVERRIDE = "house_url"

NO_PUBLIC_URL_CODE = "no_public_url"
NO_PUBLIC_URL_MESSAGE = (
    "No public house URL. Set External URL in Home Assistant "
    "(Settings → System → Network) or enable Home Assistant Cloud remote access."
)


def _host_is_public(host: str) -> bool:
    """Only publicly routable DNS names or public IPs."""
    import ipaddress

    h = str(host or "").strip().lower().strip("[]").rstrip(".")
    if not h or "." not in h and ":" not in h:
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None:
        nets = (
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
            "169.254.0.0/16", "100.64.0.0/10", "0.0.0.0/8", "fc00::/7", "fe80::/10", "::1/128",
        )
        return not any(ip in ipaddress.ip_network(n) for n in nets) and not ip.is_unspecified
    if h in {"localhost", "homeassistant", "supervisor", "hassio"}:
        return False
    for suffix in (".local", ".lan", ".localdomain", ".internal", ".home.arpa", ".localhost", ".home"):
        if h.endswith(suffix):
            return False
    if h.startswith(("homeassistant.", "supervisor.", "hassio.")) and h.count(".") == 1:
        return False
    return True


def is_public_url(url) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port == 8123 and (parsed.hostname or "").lower().startswith("homeassistant"):
        return False
    if "/api/hassio_ingress" in (parsed.path or ""):
        return False
    return _host_is_public(parsed.hostname or "")


def _override_url(hass) -> str | None:
    try:
        from .const import DOMAIN as _D

        entries = hass.config_entries.async_entries(_D)
    except Exception:
        return None
    for entry in entries or []:
        for src in (getattr(entry, "options", None) or {}, getattr(entry, "data", None) or {}):
            raw = str(src.get(CONF_HOUSE_URL_OVERRIDE) or "").strip()
            if raw:
                return raw
    return None


def _cloud_url(hass) -> str | None:
    try:
        from homeassistant.components import cloud
    except Exception:
        return None
    try:
        raw = cloud.async_remote_ui_url(hass)
    except Exception:
        return None
    text = str(raw or "").strip()
    return text or None


def _external_config_url(hass) -> str | None:
    cfg = getattr(hass, "config", None)
    text = str(getattr(cfg, "external_url", None) or "").strip() if cfg is not None else ""
    return text or None


def public_url_candidates(hass) -> list[tuple[str, str]]:
    """Override, then HA external_url, then HA Cloud. internal_url never."""
    out = []
    for source, fn in (("override", _override_url), ("external_url", _external_config_url), ("cloud", _cloud_url)):
        try:
            value = fn(hass)
        except Exception:
            value = None
        if value:
            out.append((source, value))
    return out


def resolve_public_house_url(hass) -> tuple[str | None, str | None]:
    for source, value in public_url_candidates(hass):
        norm = _normalize_https(value)
        if is_public_url(norm) and house_url_is_usable(norm):
            return norm, source
    return None, None


def resolve_house_url(hass) -> str:
    """Public house URL or empty. Never internal_url or a local host."""
    url, _ = resolve_public_house_url(hass)
    return url or ""


def _normalize_https(raw) -> str:
    text = str(raw or "").strip().rstrip("/")
    if not text:
        return ""
    if text.startswith("http://"):
        text = "https://" + text[7:]
    if not text.startswith("https://"):
        text = "https://" + text.lstrip("/")
    return text


def https_house_url(hass) -> str:
    return resolve_house_url(hass)


def house_url_status(hass) -> dict[str, Any]:
    """Same resolved house URL pairing uses, plus whether it is usable."""
    url, source = resolve_public_house_url(hass)
    out: dict[str, Any] = {
        "externalUrl": url or "",
        "houseUrl": url or "",
        "usable": bool(url),
        "houseUrlSource": source,
    }
    if not url:
        out["error"] = {"code": NO_PUBLIC_URL_CODE, "message": NO_PUBLIC_URL_MESSAGE}
    return out


def _display_resolved_url(url: str) -> str:
    shown = str(url or "").strip()
    if shown in _HOSTLESS_HTTPS:
        return "(empty)"
    return shown


def house_url_error_message(hass, house: str) -> str:
    """Concrete diagnostic for an unusable house URL. Short tip only as suffix."""
    cfg = getattr(hass, "config", None)
    ext = getattr(cfg, "external_url", None) if cfg is not None else None
    return (
        f"House URL unusable: {_display_resolved_url(house)}. "
        f"hass.config.external_url={ext!r}. " + NO_PUBLIC_URL_MESSAGE
    )


def mint_error_message(err: BaseException) -> str:
    detail = str(err).strip()
    name = type(err).__name__
    return f"{name}: {detail}" if detail else name


def house_url_is_usable(url: str) -> bool:
    """Reject hostless bases that become https://api/... in Camera."""
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme != "https" or not host:
        return False
    # Single-label junk like "api" from https:///api/... normalization
    if "." not in host and host not in {"localhost"}:
        return False
    return True


def pairing_uri(url: str, token: str) -> str:
    u = quote(str(url), safe="")
    return f"patrimony://pair?url={u}&public_url={u}&token={quote(str(token), safe='')}"


def no_public_url_body() -> dict[str, Any]:
    return _error(NO_PUBLIC_URL_CODE, NO_PUBLIC_URL_MESSAGE)


def claim_url(hass, code: str) -> str:
    return f"{https_house_url(hass)}/api/patrimony_collection/p/{code}"


# 0.4.95: tickets live in pair_tickets.py (hashed id, payload sealed with a
# key derived from the code, persisted atomically, single-use).
from .pair_tickets import (  # noqa: E402
    claim_ticket,
    put_ticket,
)


def get_ticket(hass, code: str) -> dict[str, str] | None:
    """Single-use claim. Kept for callers; same as claim_ticket."""
    return claim_ticket(hass, code)


def claim_html(deep: str) -> str:
    import json

    safe = html.escape(deep, quote=True)
    js = json.dumps(deep)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Patrimony</title>"
        "<style>body{margin:0;background:#1a1714;color:#f4efe6;font:18px/1.4 Georgia,serif;"
        "padding:48px 24px}a{color:#c4a574}</style></head><body>"
        "<p>Opening the house on your phone.</p>"
        f"<p><a href='{safe}'>Open Patrimony Collection</a></p>"
        f"<script>location.replace({js})</script>"
        "</body></html>"
    )


def _long_lived_type():
    try:
        from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN

        return TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    except ImportError:
        try:
            from homeassistant.auth.const import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN

            return TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
        except ImportError:
            return "long_lived_access_token"


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _drop_existing(auth, user, token_type) -> None:
    tokens = getattr(user, "refresh_tokens", None)
    if not tokens:
        return
    values = list(tokens.values()) if hasattr(tokens, "values") else list(tokens)
    remove = getattr(auth, "async_remove_refresh_token", None)
    if remove is None:
        remove = getattr(auth, "async_delete_refresh_token", None)
    for tok in values:
        if getattr(tok, "client_name", None) != PAIR_CLIENT_NAME:
            continue
        tok_type = getattr(tok, "token_type", None)
        if tok_type not in (None, token_type, "long_lived_access_token"):
            continue
        if remove is None:
            continue
        await _maybe_await(remove(tok))


async def mint_pairing(hass, user) -> tuple[int, dict[str, Any]]:
    """Return (status, json). QR payload is a short https claim, not the token."""
    if user is None:
        return 401, UNAVAILABLE
    token_type = _long_lived_type()
    auth = getattr(hass, "auth", None)
    if auth is None:
        return 501, UNAVAILABLE
    create_refresh = getattr(auth, "async_create_refresh_token", None)
    create_access = getattr(auth, "async_create_access_token", None)
    if create_refresh is None or create_access is None:
        return 501, UNAVAILABLE
    house = https_house_url(hass)
    if not house_url_is_usable(house):
        message = house_url_error_message(hass, house)
        _LOGGER.warning("pairing mint refused: %s", message)
        return 503, _error(NO_PUBLIC_URL_CODE, message)
    try:
        auth_user = user
        get_user = getattr(auth, "async_get_user", None)
        uid = getattr(user, "id", None)
        if get_user is not None and uid:
            loaded = await _maybe_await(get_user(uid))
            if loaded is not None:
                auth_user = loaded
        await _drop_existing(auth, auth_user, token_type)
        refresh = await create_refresh(
            auth_user,
            client_name=PAIR_CLIENT_NAME,
            token_type=token_type,
            access_token_expiration=timedelta(days=3650),
        )
        token = await _maybe_await(create_access(refresh))
        try:
            from .clients import upsert_client

            rid = getattr(refresh, "id", None) or (refresh.get("id") if isinstance(refresh, dict) else None)
            if rid:
                ha_name = getattr(auth_user, "name", None)
                upsert_client(
                    hass,
                    str(rid),
                    ha_username=str(ha_name) if ha_name else None,
                    touch_access=True,
                )
        except Exception:
            _LOGGER.warning("patrimony_pair_client_register_failed")
    except Exception as err:
        message = mint_error_message(err)
        _LOGGER.warning("pairing mint failed: %s", message)
        return 501, _error("unavailable", message)
    if not token or not isinstance(token, str):
        return 501, _error(
            "unavailable",
            f"Access token was empty or not a string ({type(token).__name__})",
        )
    try:
        add_job = getattr(hass, "async_add_executor_job", None)
        if add_job is not None:
            code = await add_job(put_ticket, hass, house, token)
        else:
            code = put_ticket(hass, house, token)
    except Exception as err:
        _LOGGER.warning("pairing ticket store failed: %s", type(err).__name__)
        return 503, _error("unavailable", "Pairing ticket store is unavailable")
    return 200, {"pairing": claim_url(hass, code)}
