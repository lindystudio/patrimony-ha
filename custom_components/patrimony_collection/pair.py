"""Mint a Patrimony iOS long-lived token. Never persist it to disk."""

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


def https_house_url(hass) -> str:
    raw = None
    cfg = getattr(hass, "config", None)
    if cfg is not None:
        raw = getattr(cfg, "external_url", None) or getattr(cfg, "internal_url", None)
    raw = str(raw or DEFAULT_HOUSE_URL).strip().rstrip("/")
    if raw.startswith("http://"):
        raw = "https://" + raw[7:]
    if not raw.startswith("https://"):
        raw = "https://" + raw.lstrip("/")
    return raw


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
    return f"patrimony://pair?url={quote(str(url), safe='')}&token={quote(str(token), safe='')}"


def claim_url(hass, code: str) -> str:
    return f"{https_house_url(hass)}/api/patrimony_collection/p/{code}"


def _tickets(hass) -> dict:
    data = getattr(hass, "data", None)
    if data is None:
        hass.data = {}
        data = hass.data
    bucket = data.setdefault(DOMAIN, {})
    return bucket.setdefault("pair_tickets", {})


def put_ticket(hass, house_url: str, token: str) -> str:
    """Memory only. Short code for a Camera-scannable https QR."""
    _purge(hass)
    code = secrets.token_hex(5)
    _tickets(hass)[code] = {
        "url": house_url,
        "token": token,
        "exp": time.time() + PAIR_TICKET_TTL_SECONDS,
    }
    return code


def _purge(hass) -> None:
    now = time.time()
    tickets = _tickets(hass)
    dead = [k for k, v in tickets.items() if float(v.get("exp") or 0) < now]
    for k in dead:
        tickets.pop(k, None)


def get_ticket(hass, code: str) -> dict[str, str] | None:
    _purge(hass)
    row = _tickets(hass).get(str(code or "").strip().lower())
    if not row:
        # codes are hex; accept as stored
        row = _tickets(hass).get(str(code or "").strip())
    if not row:
        return None
    return {"url": str(row["url"]), "token": str(row["token"])}


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
        f"<p><a href='{safe}'>Open Properties Wallet</a></p>"
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
    except Exception as err:
        _LOGGER.warning("pairing mint failed: %s", type(err).__name__)
        return 501, UNAVAILABLE
    if not token or not isinstance(token, str):
        return 501, UNAVAILABLE
    house = https_house_url(hass)
    if not house_url_is_usable(house):
        _LOGGER.warning("pairing mint refused: Home Assistant External URL is missing or invalid")
        return 503, {
            "error": {
                "code": "external_url_required",
                "message": "Set Home Assistant External URL (Settings → System → Network) to your public https host, then show a pairing code again.",
            }
        }
    code = put_ticket(hass, house, token)
    return 200, {"pairing": claim_url(hass, code)}
