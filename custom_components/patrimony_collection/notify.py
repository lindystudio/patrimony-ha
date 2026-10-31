"""House attention push. hek_ only. Never an HA token."""

from __future__ import annotations

from . import fileio

import json
import os
import re
from datetime import datetime
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from .attention import (
    commit_attention_success,
    manual_notify_decision,
    plan_automatic_pushes,
    title_is_safe,
)
from .const import (
    BACKEND_BASE,
    CONF_HOUSE_EVENT_KEY,
    NOTIFY_FILE,
    NOTIFY_TITLE_MAX,
    NOTIFY_USER_AGENT,
    PAIR_CLIENT_NAME,
)
from .ios_session import load_ios_session

_LOGGER = logging.getLogger(__name__)

MISSING_KEY = {
    "error": {
        "code": "missing_house_event_key",
        "message": "Push needs the house event key",
    }
}

# Event-key POST: one 400 code. Never include the submitted value.
INVALID_EVENT_KEY = {
    "error": {
        "code": "invalid_house_event_key",
        "message": "House event key must start with hek_",
    }
}


def notify_path(hass) -> Path:
    try:
        return Path(hass.config.path(NOTIFY_FILE))
    except Exception:
        return Path("/config") / NOTIFY_FILE


# notify.json holds cardId, attention baseline, eventKeyStatus and pushLog. Several
# executor threads (and the add-on process) read-modify-write it. Writes are atomic
# (tmp + os.replace) so no reader ever sees a torn file, and a file that exists but
# cannot be parsed is never replaced by a fresh document (that used to wipe the log).
_NOTIFY_LOCK = threading.RLock()


class NotifyDocUnreadable(Exception):
    """notify.json exists but is not a JSON object. Do not overwrite it."""


def _read_notify_file(path: Path) -> dict[str, Any]:
    for attempt in range(3):
        try:
            raw = fileio.read_text(path)
            if raw is None:
                return {}
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        fileio.maybe_sleep(0.05 * (attempt + 1))
    raise NotifyDocUnreadable(str(path))


def _write_notify_file(path: Path, data: dict[str, Any]) -> None:
    # Atomic (tmp + fsync + os.replace), serialized, cache write-through.
    fileio.write_text(path, json.dumps(data, indent=2) + "\n")


def _update_notify_doc(hass, mutate) -> bool:
    """Locked read-modify-write. Unreadable file → leave it alone, return False."""
    path = notify_path(hass)
    with _NOTIFY_LOCK:
        try:
            data = _read_notify_file(path)
        except NotifyDocUnreadable:
            _LOGGER.error("patrimony_notify_unreadable_not_overwritten")
            return False
        if mutate(data) is False:
            return True
        _write_notify_file(path, data)
        return True


def looks_like_ha_token(key: Any) -> bool:
    """JWT-shaped or token-like. Never log the value."""
    if key is None:
        return False
    text = str(key).strip()
    if not text:
        return False
    if text.startswith("eyJ") or "eyJ" in text:
        return True
    if "token" in text.lower():
        return True
    return False


def is_usable_house_event_key(key: Any) -> bool:
    if key is None:
        return False
    text = str(key).strip()
    if not text.startswith("hek_") or len(text) < 5:
        return False
    if looks_like_ha_token(text):
        return False
    return True


def event_key_error(raw: Any) -> dict[str, Any] | None:
    """400 invalid_house_event_key if unusable. Never echoes the submitted value."""
    if is_usable_house_event_key(raw):
        return None
    return dict(INVALID_EVENT_KEY)


def persist_house_event_key(options: dict | None, key: str) -> dict[str, Any]:
    """Copy options with CONF_HOUSE_EVENT_KEY set. Never logs the key."""
    out = dict(options or {})
    out[CONF_HOUSE_EVENT_KEY] = str(key).strip()
    return out


def apply_event_key_payload(
    options: dict | None, payload: Any
) -> tuple[dict[str, Any] | None, dict[str, Any], int]:
    """Validate phone POST body and return (options, body, status).

    Success is 200 `{configured: true}`. Invalid is 400 `invalid_house_event_key`
    and does not persist — configured stays false so the app can retry/rotate.
    Never echoes the key.
    """
    if not isinstance(payload, dict):
        return None, dict(INVALID_EVENT_KEY), 400
    raw = payload.get("houseEventKey")
    err = event_key_error(raw)
    if err:
        return None, err, 400
    stored = persist_house_event_key(options, str(raw).strip())
    return stored, {"configured": True}, 200


def clip_title(title: Any) -> str | None:
    text = "" if title is None else str(title).strip()
    if not text:
        return None
    if len(text) > NOTIFY_TITLE_MAX:
        text = text[:NOTIFY_TITLE_MAX]
    return text


def load_card_id(hass) -> str:
    """Stable cardId. Added to the existing document; never replaces it."""
    found: dict[str, str] = {}

    def mutate(data: dict[str, Any]):
        cid = str(data.get("cardId") or "").strip()
        if cid:
            found["cid"] = cid
            return False
        found["cid"] = data["cardId"] = str(uuid4())
        return True

    if not _update_notify_doc(hass, mutate):
        return str(uuid4())
    return found["cid"]


def events_url(property_id: str) -> str:
    return f"{BACKEND_BASE}/v1/properties/{property_id}/events"


def load_card_id_and_post(
    hass,
    property_id: str,
    key: str,
    title: str,
    timeout: int = 8,
    severity: str = "attention",
    manual: bool = True,
    exclude_device: str | None = None,
    kind: str | None = None,
    rule: str | None = None,
    reason: str | None = None,
    exclude_client: str | None = None,
    attempt: int = 1,
    max_attempts: int = 1,
    card_id: str | None = None,
    sender_role: str | None = None,
    sender_principal: str | None = None,
) -> int:
    """Disk load/create cardId then POST events — safe to run in an executor.

    Manual Soon Notify always uses severity attention. Automatic FIRE may pass
    severity alert with manual=False. Any other severity is attention.
    One attempt per call. 0.4.110: `attempt`/`max_attempts` only decide whether
    this row says another try follows (`_event_tls.retry`); callers schedule it.
    0.4.110: every POST carries `kind` (chat | manual | attention). `card_id` is
    the presentation card id of an attention push (a UUID, as in the
    PresentationDocument); anything else, and chat/manual, use the shared
    notify cardId.
    0.4.111: chat only. `sender_role` ("owner" | "member") and `sender_principal`
    turn into Backend 9197cfc's excludeRole / excludeClient / excludePrincipal,
    and only next to a valid excludeDevice (see sender_exclusion).
    """
    if manual or severity != "alert":
        severity = "attention"
    log_kind = "manual" if manual else kind
    wire_kind = log_kind if log_kind in EVENT_KINDS else None
    if wire_kind == "attention" and is_card_uuid(card_id):
        card_id = str(card_id).strip().lower()
    else:
        card_id = load_card_id(hass)
    _event_tls.devices = None
    _event_tls.http_status = None
    _event_tls.error = None
    _event_tls.summary = None
    _event_tls.retry = False
    # Only chat passes a sender to skip; alerts and manual sends post exactly as before.
    extra = {"exclude_device": exclude_device} if exclude_device else {}
    if exclude_device and wire_kind == "chat" and not manual:
        if sender_role in SENDER_ROLES:
            extra["exclude_role"] = sender_role
        if sender_role == "member" and exclude_client:
            extra["exclude_client"] = exclude_client
        if sender_principal:
            extra["exclude_principal"] = sender_principal
    if wire_kind:
        extra["kind"] = wire_kind
    status = post_house_event(
        property_id, key, card_id, title, timeout=timeout, severity=severity, **extra
    )
    outcome = last_post_outcome(status)
    _event_tls.outcome = outcome
    retry = (not manual) and int(attempt) < int(max_attempts) and is_retryable(outcome)
    _event_tls.retry = retry
    # Phone-readable last auth rejection; every kind of attempt counts.
    try:
        note_event_key_attempt(hass, outcome)
    except Exception:
        _LOGGER.error("patrimony_event_key_status_write_failed")
    # Every attempt is logged, success or failure. Manual sends always; others when tagged.
    if log_kind:
        try:
            record_push(
                hass,
                kind=log_kind,
                rule=MANUAL_SEND_CLASS if manual else (rule or log_kind),
                outcome=outcome,
                reason=reason or ("Manual send from panel" if manual else None),
                severity=severity,
                devices=getattr(_event_tls, "devices", None),
                exclude_client=exclude_client if is_device_hash(exclude_device) else None,
                attempt=attempt if max_attempts > 1 else None,
                retry=retry if max_attempts > 1 else None,
            )
        except Exception:
            _LOGGER.error("patrimony_push_log_write_failed")
    return status


def manual_send(hass, property_id: str, key: str, title: str) -> tuple[int, dict[str, Any]]:
    """POST /notify worker: status plus the events-API outcome (same thread)."""
    status = load_card_id_and_post(hass, property_id, key, title)
    outcome = dict(getattr(_event_tls, "outcome", None) or last_post_outcome(status))
    return status, outcome


def manual_event(key: Any, title: Any) -> dict[str, str] | None:
    """Gate for POST /notify. Explicit Soon only. No client severity."""
    return manual_notify_decision(is_usable_house_event_key(key), title)


EVENT_KINDS = ("chat", "manual", "attention")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_card_uuid(value: Any) -> bool:
    """A presentation card id as the app sees it. Never an entity id or anything else."""
    return isinstance(value, str) and _UUID_RE.fullmatch(value.strip().lower()) is not None


def is_device_hash(value: Any) -> bool:
    """SHA-256 hex of an APNs token, as the phone sends it. Never the token itself."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


SENDER_ROLES = ("owner", "member")
_MEMBER_CLIENT_RE = re.compile(r"m_[A-Za-z0-9_-]{1,64}")
_PRINCIPAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}")
_SECRET_PREFIXES = ("hek_", "pmt_", "eyj")


def is_member_client(value: Any) -> bool:
    """HA's own member client id (`m_<memberId>`), as Backend's excludeClient expects."""
    return isinstance(value, str) and _MEMBER_CLIENT_RE.fullmatch(value) is not None


def is_sender_principal(value: Any) -> bool:
    """Opaque backend principal id from the chat body: 8-64 chars of [A-Za-z0-9_-], never a secret."""
    if not isinstance(value, str) or _PRINCIPAL_RE.fullmatch(value) is None:
        return False
    return not value.lower().startswith(_SECRET_PREFIXES)


def sender_exclusion(
    exclude_device: Any,
    role: Any = None,
    client: Any = None,
    principal: Any = None,
) -> dict[str, str]:
    """0.4.111 wire fields that tell Backend whose message to skip (APNS.md, audit item 7).

    Nothing at all without a valid excludeDevice. Owner chat: excludeRole "owner".
    Member chat: excludeClient "m_<memberId>" (excludeRole "member" only if that id is
    unusable). excludePrincipal only for a conservatively valid opaque id.
    """
    if not is_device_hash(exclude_device):
        return {}
    fields: dict[str, str] = {"excludeDevice": exclude_device}
    if role == "owner":
        fields["excludeRole"] = "owner"
    elif role == "member":
        if is_member_client(client):
            fields["excludeClient"] = client
        else:
            fields["excludeRole"] = "member"
    if is_sender_principal(principal):
        fields["excludePrincipal"] = principal
    return fields


def post_house_event(
    property_id: str,
    key: str,
    card_id: str,
    title: str,
    timeout: int = 8,
    severity: str = "attention",
    exclude_device: str | None = None,
    kind: str | None = None,
    exclude_role: str | None = None,
    exclude_client: str | None = None,
    exclude_principal: str | None = None,
) -> int:
    if severity != "alert":
        severity = "attention"
    _event_tls.http_status = None
    _event_tls.error = None
    _event_tls.upstream_error = None
    _event_tls.summary = None
    if not title_is_safe(title):
        _event_tls.error = "unsafe_title"
        return 400
    body: dict[str, str] = {"cardId": card_id, "severity": severity, "title": title}
    # The phone that wrote a chat message does not need a push about it.
    # 0.4.111: plus whose message it was (role / member client / principal), only with a device.
    body.update(sender_exclusion(exclude_device, exclude_role, exclude_client, exclude_principal))
    # 0.4.110: Backend (57b58fd) collapses banners only for kind attention.
    if kind in EVENT_KINDS:
        body["kind"] = kind
    payload = json.dumps(body).encode()
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
            status = int(getattr(resp, "status", 200) or 200)
            raw = b""
            reader = getattr(resp, "read", None)
            if callable(reader):
                try:
                    raw = reader() or b""
                except Exception:
                    raw = b""
            _event_tls.devices = devices_from_event_body(raw)
            _event_tls.summary = event_summary_from_body(raw)
            _event_tls.http_status = status
            return status
    except HTTPError as exc:
        _event_tls.devices = None
        _event_tls.http_status = int(exc.code or 502)
        _event_tls.upstream_error = _upstream_error_code(exc)
        return int(exc.code or 502)
    except (URLError, TimeoutError, OSError) as exc:
        _event_tls.devices = None
        _event_tls.error = _error_kind(exc)
        return 502


def _error_kind(exc: BaseException) -> str:
    """Short fixed label for a POST with no HTTP reply. Never the exception text."""
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return "timeout"
    text = type(reason or exc).__name__.lower()
    if "timeout" in text:
        return "timeout"
    if "ssl" in text or "certificate" in text:
        return "tls_error"
    return "connection_error"


def last_post_outcome(status: int) -> dict[str, Any]:
    """Events-API status of the last post on this thread: number, or None + error kind."""
    http_status = getattr(_event_tls, "http_status", None)
    error = getattr(_event_tls, "error", None)
    if http_status is None and error is None:
        # post_house_event replaced (tests) or older path: trust the returned int.
        http_status = int(status)
    out = {"status": http_status, "error": error, "ok": http_status is not None and 200 <= int(http_status) < 300 and error is None}
    upstream = getattr(_event_tls, "upstream_error", None)
    if upstream:
        out["upstreamError"] = upstream
    # 0.4.110 (S2): what Backend said about a 2xx (recipients, event id, pushError).
    summary = getattr(_event_tls, "summary", None)
    if out["ok"] and isinstance(summary, dict):
        for name in ("recipients", "eventId", "pushError", "backendDevices"):
            if summary.get(name) is not None:
                out[name] = summary[name]
    return out


# 0.4.110 (S3/S4): bounded retry for automatic sends (attention, chat). Manual Soon
# Notify stays one attempt: its caller sees the 424 straight away.
PUSH_RETRY_DELAYS = (5.0, 30.0, 120.0)
PUSH_MAX_ATTEMPTS = 1 + len(PUSH_RETRY_DELAYS)
RETRYABLE_ERRORS = ("timeout", "connection_error", "tls_error")


def is_retryable(outcome: dict[str, Any] | None) -> bool:
    """Timeout, connection error, 5xx or 429. Never another 4xx (auth, validation)."""
    if not isinstance(outcome, dict) or outcome.get("ok"):
        return False
    if outcome.get("error") in RETRYABLE_ERRORS:
        return True
    if outcome.get("error"):
        return False
    status = outcome.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        return False
    return status == 429 or 500 <= status < 600


def retry_delay(attempt: int) -> float:
    """Delay before attempt `attempt + 1`."""
    index = max(0, min(int(attempt) - 1, len(PUSH_RETRY_DELAYS) - 1))
    return float(PUSH_RETRY_DELAYS[index])


# Tests replace this with a synchronous runner. Signature: (hass, delay, fn) -> bool.
_RETRY_SCHEDULER = None


def schedule_push_retry(hass, delay: float, fn) -> bool:
    """Run `fn` (blocking) in the executor after `delay` seconds. Never in the event loop.

    Safe to call from an executor thread. In HA the delay is a loop timer and the
    send itself an executor job; without a loop a daemon timer thread is used.
    """
    override = _RETRY_SCHEDULER
    if override is not None:
        return bool(override(hass, delay, fn))

    def _run() -> None:
        try:
            fn()
        except Exception:
            _LOGGER.error("patrimony_push_retry_failed")

    loop = getattr(hass, "loop", None)
    add_job = getattr(hass, "async_add_executor_job", None)
    if loop is not None and callable(add_job) and hasattr(loop, "call_soon_threadsafe"):
        try:
            if loop.is_closed():
                return False

            def _kick() -> None:
                add_job(_run)

            loop.call_soon_threadsafe(loop.call_later, float(delay), _kick)
            return True
        except Exception:
            _LOGGER.error("patrimony_push_retry_schedule_failed")
            return False
    try:
        timer = threading.Timer(float(delay), _run)
        timer.daemon = True
        timer.start()
        return True
    except Exception:
        _LOGGER.error("patrimony_push_retry_schedule_failed")
        return False


MISSING_KEY_OUTCOME = {"status": None, "error": "missing_key", "ok": False}


def push_result(outcome: dict[str, Any] | None) -> dict[str, Any]:
    """0.4.110 (S7): the additive `push` object on chat 201. {ok, status} only."""
    if not isinstance(outcome, dict):
        return {"ok": False, "status": None}
    status = outcome.get("status")
    return {
        "ok": bool(outcome.get("ok")),
        "status": int(status) if isinstance(status, int) and not isinstance(status, bool) else None,
    }


_SAFE_CODE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


def _upstream_error_code(exc: BaseException) -> str | None:
    """Events-API error code string from an HTTP error body. Never the body itself."""
    try:
        reader = getattr(exc, "read", None)
        raw = reader() if callable(reader) else b""
        data = json.loads(raw or b"{}")
    except Exception:
        return None
    code = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code")
        elif isinstance(err, str):
            code = err
        if code is None and isinstance(data.get("code"), str):
            code = data.get("code")
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code) and not code.startswith("hek_"):
        return code
    return None


EVENT_KEY_STATUS_STORE = "eventKeyStatus"
AUTH_REJECT_STATUSES = (401, 403)


def _load_notify_doc(hass) -> dict[str, Any]:
    """Read-only view; {} when missing or unreadable."""
    try:
        return _read_notify_file(notify_path(hass))
    except NotifyDocUnreadable:
        return {}


def auth_rejection_from_outcome(outcome: dict[str, Any] | None, at: str) -> dict[str, Any] | None:
    """401/403 → phone status object; anything else (2xx, 5xx, timeout) → None."""
    if not isinstance(outcome, dict):
        return None
    status = outcome.get("status")
    if outcome.get("error") or not isinstance(status, int) or isinstance(status, bool):
        return None
    if status not in AUTH_REJECT_STATUSES:
        return None
    upstream = outcome.get("upstreamError")
    code = upstream if isinstance(upstream, str) and _SAFE_CODE.fullmatch(upstream) else "invalid_house_event_key"
    return {"code": status, "error": code, "at": at}


def note_event_key_attempt(hass, outcome: dict[str, Any] | None, at: str | None = None) -> None:
    """eventKeyLastPushStatus. Persisted.

    0.4.110 (S8): a 401/403 sets it; only a 2xx (or a new key install) clears it.
    Timeout, connection errors, 5xx and other 4xx leave it unchanged, so a revoked
    key does not disappear from the phone after one network blip.
    """
    stamp = at or datetime.now().astimezone().isoformat(timespec="seconds")
    rejection = auth_rejection_from_outcome(outcome, stamp)
    accepted = isinstance(outcome, dict) and bool(outcome.get("ok"))
    if rejection is None and not accepted:
        return

    def mutate(data: dict[str, Any]):
        block = data.get(EVENT_KEY_STATUS_STORE)
        block = dict(block) if isinstance(block, dict) else {}
        if rejection is None and block.get("lastPushStatus") is None and EVENT_KEY_STATUS_STORE in data:
            return False
        block["lastPushStatus"] = rejection
        data[EVENT_KEY_STATUS_STORE] = block

    _update_notify_doc(hass, mutate)


def mark_event_key_installed(hass, at: str | None = None) -> None:
    """POST /event_key success: clear the rejection, stamp installedAt. No key stored here."""
    stamp = at or datetime.now().astimezone().isoformat(timespec="seconds")

    def mutate(data: dict[str, Any]) -> None:
        block = data.get(EVENT_KEY_STATUS_STORE)
        block = dict(block) if isinstance(block, dict) else {}
        block["lastPushStatus"] = None
        block["installedAt"] = stamp
        data[EVENT_KEY_STATUS_STORE] = block

    _update_notify_doc(hass, mutate)


def event_key_status_fields(hass) -> dict[str, Any]:
    """{eventKeyLastPushStatus, eventKeyInstalledAt}. Never the key or fingerprint."""
    block = _load_notify_doc(hass).get(EVENT_KEY_STATUS_STORE)
    block = block if isinstance(block, dict) else {}
    last = block.get("lastPushStatus")
    if isinstance(last, dict) and last.get("code") in AUTH_REJECT_STATUSES:
        last = {
            "code": int(last["code"]),
            "error": str(last.get("error") or "invalid_house_event_key"),
            "at": str(last.get("at") or ""),
        }
    else:
        last = None
    installed = block.get("installedAt")
    return {
        "eventKeyLastPushStatus": last,
        "eventKeyInstalledAt": installed if isinstance(installed, str) and installed else None,
    }


def ingest_host() -> str:
    return urlparse(BACKEND_BASE).netloc or "Push notification server"


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


def load_attention_store(hass) -> dict[str, Any]:
    """Debounce + baseline next to the stable notify card. Never stores hek_."""
    path = notify_path(hass)
    try:
        raw = fileio.read_text(path)
        if raw is not None:
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("attention"), dict):
                att = data["attention"]
                classes = att.get("classes") if isinstance(att.get("classes"), dict) else {}
                pushed = att.get("pushed") if isinstance(att.get("pushed"), dict) else {}
                return {
                    "seeded": bool(att.get("seeded")),
                    "classes": {str(k): str(v) for k, v in classes.items() if v},
                    "pushed": {
                        str(k): float(v)
                        for k, v in pushed.items()
                        if isinstance(v, (int, float))
                    },
                }
    except Exception:
        pass
    return {"seeded": False, "classes": {}, "pushed": {}}


def save_attention_store(hass, store: dict[str, Any]) -> None:
    classes = store.get("classes") if isinstance(store.get("classes"), dict) else {}
    pushed = store.get("pushed") if isinstance(store.get("pushed"), dict) else {}
    block = {
        "seeded": bool(store.get("seeded")),
        "classes": {str(k): str(v) for k, v in classes.items() if v},
        "pushed": {
            str(k): float(v)
            for k, v in pushed.items()
            if isinstance(v, (int, float))
        },
    }

    def mutate(data: dict[str, Any]) -> None:
        if not str(data.get("cardId") or "").strip():
            data["cardId"] = str(uuid4())
        data["attention"] = block

    _update_notify_doc(hass, mutate)



NO_IOS_CLIENT = "No iOS client registered"
PUSH_LOG_MAX = 50
MANUAL_SEND_CLASS = "manual.send"
_DEVICE_LIST_KEYS = ("devices", "clients", "iosDevices", "deviceNames", "targets")
_NAME_KEYS = ("name", "deviceName", "label", "client", "clientName", "client_name")
_ID_KEYS = ("clientId", "client_id", "tokenId", "token_id", "id")
_event_tls = threading.local()


def _public_text(value: Any) -> str | None:
    """Drop blanks and anything that looks like a house key or token."""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if "hek_" in lowered or "eyj" in lowered or "token" in lowered or "secret" in lowered:
        return None
    compact = text.replace("-", "").replace(":", "")
    if len(compact) >= 32 and all(c in "0123456789abcdefABCDEF" for c in compact):
        return None
    return text


def _safe_name(value: Any) -> str | None:
    """A stored display name. Not a key, token, or long hex id."""
    text = "" if value is None else str(value).strip()
    if not text or len(text) > 80:
        return None
    lowered = text.lower()
    if "hek_" in lowered or "eyj" in lowered or "secret" in lowered or "token" in lowered:
        return None
    compact = text.replace("-", "").replace(":", "")
    if len(compact) >= 32 and all(c in "0123456789abcdefABCDEF" for c in compact):
        return None
    return text


def _id_label(value: Any) -> str | None:
    """Client or token id already stored. Long ids keep the last 4. Never a secret."""
    text = "" if value is None else str(value).strip()
    if not text or len(text) > 200:
        return None
    lowered = text.lower()
    if "hek_" in lowered or "eyj" in lowered or "secret" in lowered:
        return None
    compact = text.replace("-", "").replace(":", "")
    long_hex = len(compact) >= 32 and all(c in "0123456789abcdefABCDEF" for c in compact)
    if long_hex or len(text) > 24:
        tail = text[-4:]
        if len(tail) == 4 and tail.isalnum():
            return tail
        return None
    if "token" in lowered:
        return None
    return text


def label_for_client(item: Any) -> str | None:
    """Name if we have one, otherwise a client id / token id. Ignores APNs token fields."""
    if isinstance(item, str):
        return _safe_name(item) or _id_label(item)
    if not isinstance(item, dict):
        return None
    for key in _NAME_KEYS:
        if item.get(key) not in (None, ""):
            name = _safe_name(item.get(key))
            if name:
                return name
    for key in _ID_KEYS:
        label = _id_label(item.get(key))
        if label:
            return label
    return None


def _labels_from_items(items: list) -> list[str]:
    labels: list[str] = []
    for item in items:
        label = label_for_client(item)
        if label and label not in labels:
            labels.append(label)
    return labels


def devices_from_event_body(raw: Any) -> list[str] | None:
    """Public device/client labels from the events response, or None if it has no list.

    deviceCount is not a list. Names win. Otherwise a truncated client/token id.
    APNs token fields, hek_, and HA tokens are dropped.
    """
    if raw is None or raw == b"" or raw == "":
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in _DEVICE_LIST_KEYS:
        items = data.get(key)
        if isinstance(items, list):
            return _labels_from_items(items)
    return None


_COUNT_KEYS = ("deviceCount", "recipients", "recipientCount", "devicesCount")
_EVENT_ID_KEYS = ("eventId", "event_id", "logId", "id")


def _safe_code(value: Any) -> str | None:
    """Short fixed-alphabet code or id. Never a key, token or free text."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _SAFE_CODE.fullmatch(text):
        return None
    lowered = text.lower()
    if lowered.startswith(("hek_", "pmt_", "eyj")) or "token" in lowered or "secret" in lowered:
        return None
    return text


def event_summary_from_body(raw: Any) -> dict[str, Any] | None:
    """0.4.110 (S2): sanitised facts from a 2xx events body.

    recipients: Backend's device count after excludeDevice (int; a list counts its
    length). eventId: Backend's event/log id if it sends one. pushError: Backend's
    synchronous APNs error code (e.g. apns_unconfigured) on an otherwise 202.
    Device lists keep only public labels. Never tokens or the body itself.
    """
    if raw is None or raw == b"" or raw == "":
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {}
    for key in _COUNT_KEYS:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 0 <= value < 100000:
            out["recipients"] = value
            break
        if isinstance(value, list):
            out["recipients"] = len(value)
            break
    for key in _EVENT_ID_KEYS:
        code = _safe_code(data.get(key))
        if code:
            out["eventId"] = code
            break
    push_error = _safe_code(data.get("pushError"))
    if push_error:
        out["pushError"] = push_error
    devices = devices_from_event_body(json.dumps(data))
    if devices is not None:
        out["backendDevices"] = devices
    return out or None


def _iter_refresh_tokens(hass):
    auth = getattr(hass, "auth", None)
    if auth is None:
        return
    seen: set[int] = set()

    def emit(tok):
        if tok is None:
            return
        ident = id(tok)
        if ident in seen:
            return
        seen.add(ident)
        yield_box.append(tok)

    yield_box: list = []
    direct = getattr(auth, "refresh_tokens", None)
    if isinstance(direct, dict):
        for tok in direct.values():
            emit(tok)
    elif isinstance(direct, list):
        for tok in direct:
            emit(tok)
    users = getattr(auth, "users", None) or []
    try:
        users = list(users)
    except Exception:
        users = []
    for user in users:
        tokens = getattr(user, "refresh_tokens", None)
        if isinstance(tokens, dict):
            for tok in tokens.values():
                emit(tok)
        elif isinstance(tokens, list):
            for tok in tokens:
                emit(tok)
    for tok in yield_box:
        yield tok


def _refresh_token_label(tok: Any) -> str | None:
    """Patrimony iOS refresh-token label. Never reads the token secret."""
    def grab(key: str):
        if isinstance(tok, dict):
            return tok.get(key)
        return getattr(tok, key, None)

    client_name = grab("client_name")
    if str(client_name or "").strip() != PAIR_CLIENT_NAME:
        return None
    return label_for_client(
        {
            "client_name": client_name,
            "client_id": grab("client_id"),
            "id": grab("id"),
        }
    )


def _token_field(tok: Any, key: str):
    if isinstance(tok, dict):
        return tok.get(key)
    return getattr(tok, key, None)


def _session_labels(session: Any) -> list[str]:
    """Names or ids actually stored. One ios_session object is one client.

    A list, or a clients/sessions list inside the file, is enumerated in full.
    """
    if isinstance(session, list):
        return _labels_from_items(session)
    if not isinstance(session, dict):
        return []
    for key in ("clients", "sessions"):
        items = session.get(key)
        if isinstance(items, list) and items:
            labels = _labels_from_items(items)
            if labels:
                return labels
    name = _safe_name(session.get("deviceName"))
    if name:
        return [name]
    label = label_for_client(session)
    return [label] if label else []


def _token_id_label(tok: Any) -> str | None:
    """Stored token id, last 4 when long. Never reads the token secret."""
    for key in ("id", "client_id"):
        label = _id_label(_token_field(tok, key))
        if label:
            return label
    return None


def _patrimony_refresh_tokens(hass) -> list[Any]:
    found: list[Any] = []
    try:
        tokens = _iter_refresh_tokens(hass)
    except Exception:
        return found
    for tok in tokens:
        if str(_token_field(tok, "client_name") or "").strip() != PAIR_CLIENT_NAME:
            continue
        found.append(tok)
    return found


def registered_ios_clients(hass) -> list[str]:
    """Every iOS client this house has registered. Name, else id. No secrets.

    Prefer the per-credential clients registry (display names). Fall back to
    ios_session.json last-write and Patrimony iOS refresh tokens.
    """
    try:
        from .clients import active_display_names

        named = active_display_names(hass)
        if named:
            return named
    except Exception:
        pass
    try:
        session = load_ios_session(hass)
    except Exception:
        session = None
    session_labels = _session_labels(session)
    tokens = _patrimony_refresh_tokens(hass)
    if len(tokens) <= 1:
        if session_labels:
            return session_labels
        if not tokens:
            return []
        label = _refresh_token_label(tokens[0])
        return [label] if label else []
    if len(session_labels) == len(tokens):
        return list(session_labels)
    labels: list[str] = []
    # One stored device name stands for one registration, not the whole list.
    named = session_labels[0] if len(session_labels) == 1 else None
    skip_named = named is not None
    if named:
        labels.append(named)
    for tok in tokens:
        if skip_named:
            skip_named = False
            continue
        label = _token_id_label(tok)
        if label and label not in labels:
            labels.append(label)
    if len(session_labels) > 1:
        for label in session_labels:
            if label not in labels:
                labels.append(label)
    return labels


def _devices_field(devices: Any, local: list[str] | None = None) -> Any:
    """Public devices cell. None means the events body had no list: use local clients."""
    if devices is None:
        found = [item for item in (local or []) if item]
        return found if found else NO_IOS_CLIENT
    if isinstance(devices, list):
        labels = _labels_from_items(devices)
        return labels if labels else NO_IOS_CLIENT
    if isinstance(devices, str) and devices.strip() == NO_IOS_CLIENT:
        return NO_IOS_CLIENT
    return NO_IOS_CLIENT


def record_manual_send(hass, devices: Any = None, at: str | None = None) -> None:
    """One row for a successful gold Send. Not an attention FIRE rule."""
    record_attention_push(hass, MANUAL_SEND_CLASS, devices=devices, at=at)


def record_attention_push(hass, fire_class: str, devices: Any = None, at: str | None = None) -> None:
    """Append one successful row (legacy helper). Caller must not use this for QUIET."""
    fire = _public_text(fire_class)
    if not fire:
        return
    kind = "manual" if fire == MANUAL_SEND_CLASS else "attention"
    record_push(hass, kind=kind, rule=fire, outcome=None, devices=devices, at=at)


PUSH_KINDS = ("attention", "manual", "chat", "pq")
PUSH_LOG_ERRORS = ("timeout", "connection_error", "tls_error", "unsafe_title", "missing_key")


def _client_names(hass, exclude_client: str | None = None) -> list[str]:
    """Saved display names of active paired clients, minus the excluded sender."""
    try:
        from .clients import active_display_names

        return active_display_names(hass, exclude_client_id=exclude_client)
    except Exception:
        return []


def record_push(
    hass,
    kind: str,
    rule: str | None,
    outcome: dict[str, Any] | None,
    reason: str | None = None,
    severity: str | None = None,
    devices: Any = None,
    at: str | None = None,
    exclude_client: str | None = None,
    attempt: int | None = None,
    retry: bool | None = None,
) -> None:
    """Append one push-log row, success or failure. No title text, keys or tokens.

    Columns (0.4.110):
      devices       legacy column, unchanged meaning: HA's *local* active paired
                    clients at send time (minus the chat sender), not Backend's
                    recipients. Kept for panel readers.
      localDevices  the same local list under an explicit name.
      recipients    Backend's deviceCount from the 2xx body (null if not sent).
      eventId       Backend's event id from the 2xx body (null if not sent).
      pushError     Backend's synchronous APNs error code on a 2xx (null if none).
      backendDevices public labels if Backend ever returns a device list.
      upstreamError Backend's sanitised error code on a non-2xx.
      error         timeout | connection_error | tls_error | unsafe_title | missing_key.
      attempt/retry automatic sends: try number, and whether another try follows.
    """
    fire = _public_text(rule) or _public_text(kind)
    if not fire:
        return
    kind_text = kind if kind in PUSH_KINDS else "attention"
    if outcome is None:
        outcome = {"status": None, "error": None, "ok": True}
    status = outcome.get("status")
    ok = bool(outcome.get("ok"))
    error = outcome.get("error") if outcome.get("error") in PUSH_LOG_ERRORS else None
    upstream_error = _safe_code(outcome.get("upstreamError"))
    recipients = outcome.get("recipients")
    if not isinstance(recipients, int) or isinstance(recipients, bool) or recipients < 0:
        recipients = None
    event_id = _safe_code(outcome.get("eventId"))
    push_error = _safe_code(outcome.get("pushError"))
    backend_devices = outcome.get("backendDevices")
    backend_devices = backend_devices if isinstance(backend_devices, list) else None
    if backend_devices is not None:
        backend_devices = _labels_from_items(backend_devices)
    stamp = at or datetime.now().astimezone().isoformat(timespec="seconds")
    stamp = _public_text(stamp) or ""

    def mutate(data: dict[str, Any]) -> None:
        rows = data.get("pushLog")
        if not isinstance(rows, list):
            rows = []
        normalized: list[Any] = []
        for existing in rows:
            if not isinstance(existing, dict):
                continue
            kept = dict(existing)
            kept["devices"] = _devices_field(existing.get("devices"))
            normalized.append(kept)
        rows = normalized
        names = _client_names(hass, exclude_client)
        if names:
            devices_cell: Any = names
        elif exclude_client:
            devices_cell = NO_IOS_CLIENT
        else:
            devices_cell = _devices_field(devices, local=registered_ios_clients(hass))
        reason_text = _public_text(reason)
        if reason_text and len(reason_text) > 120:
            reason_text = reason_text[:120]
        sev = str(severity).strip().lower() if severity else None
        row = {
            "at": stamp,
            "fire_class": fire,
            "devices": devices_cell,
            "kind": kind_text,
            "rule": fire,
            "status": int(status) if isinstance(status, int) else None,
            "error": error,
            "ok": ok,
            "reason": reason_text,
            "severity": sev if sev in ("attention", "alert", "info") else None,
            "localDevices": devices_cell,
            "recipients": recipients,
            "eventId": event_id,
            "pushError": push_error,
            "upstreamError": upstream_error,
        }
        if backend_devices is not None:
            row["backendDevices"] = backend_devices
        if attempt is not None:
            row["attempt"] = int(attempt)
            row["retry"] = bool(retry)
        rows.append(row)
        data["pushLog"] = rows[-PUSH_LOG_MAX:]

    _update_notify_doc(hass, mutate)


def _public_extras(row: dict[str, Any], devices: Any) -> dict[str, Any]:
    """0.4.110 columns. Old rows get localDevices = devices and nulls elsewhere."""
    recipients = row.get("recipients")
    if not isinstance(recipients, int) or isinstance(recipients, bool) or recipients < 0:
        recipients = None
    local = row.get("localDevices", devices)
    local = _devices_field(local) if local is not None else devices
    out: dict[str, Any] = {
        "localDevices": local,
        "recipients": recipients,
        "eventId": _safe_code(row.get("eventId")),
        "pushError": _safe_code(row.get("pushError")),
        "upstreamError": _safe_code(row.get("upstreamError")),
    }
    backend = row.get("backendDevices")
    if isinstance(backend, list):
        out["backendDevices"] = _labels_from_items(backend)
    attempt = row.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool):
        out["attempt"] = attempt
        out["retry"] = bool(row.get("retry"))
    return out


def push_log_document(hass) -> dict[str, Any]:
    """Panel payload. Last 50. Never includes hek_ or tokens."""
    rows: list[Any] = []
    loaded = _load_notify_doc(hass)
    if isinstance(loaded.get("pushLog"), list):
        rows = loaded["pushLog"]
    public: list[dict[str, Any]] = []
    changed = False
    for row in rows[-PUSH_LOG_MAX:]:
        if not isinstance(row, dict):
            continue
        devices = _devices_field(row.get("devices"))
        if row.get("devices") != devices:
            changed = True
            row["devices"] = devices
        fire = _public_text(row.get("fire_class"))
        if not fire:
            continue
        at = _public_text(row.get("at")) or ""
        status = row.get("status")
        kind = row.get("kind")
        if kind not in PUSH_KINDS:
            kind = "manual" if fire == MANUAL_SEND_CLASS else "attention"
        sev = row.get("severity")
        public.append(
            {
                "at": at,
                "fire_class": fire,
                "devices": devices,
                "kind": kind,
                "rule": _public_text(row.get("rule")) or fire,
                # Old rows were only written after a 2xx: status unknown, ok true.
                "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
                "error": row.get("error") if isinstance(row.get("error"), str) else None,
                "ok": bool(row.get("ok", True)),
                "reason": _public_text(row.get("reason")),
                "severity": sev if sev in ("attention", "alert", "info") else None,
                **_public_extras(row, devices),
            }
        )
    if changed:
        # Re-read under the lock and only normalise devices; never write a stale copy.
        def mutate(data: dict[str, Any]):
            fresh = data.get("pushLog")
            if not isinstance(fresh, list):
                return False
            for row in fresh:
                if isinstance(row, dict):
                    row["devices"] = _devices_field(row.get("devices"))
            data["pushLog"] = fresh[-PUSH_LOG_MAX:]
            return True

        try:
            _update_notify_doc(hass, mutate)
        except Exception:
            pass
    return {"rows": public}


# Stamp keys whose send (first try or a scheduled retry) is still running, per notify
# file. In memory only: after a restart nothing is in flight and the uncommitted
# baseline lets the next evaluation re-fire.
_ATTENTION_INFLIGHT: set[tuple[str, str]] = set()


def _inflight_release(path_key: str, stamp_key: str) -> None:
    with _NOTIFY_LOCK:
        _ATTENTION_INFLIGHT.discard((path_key, stamp_key))


def _commit_attention(hass, push: dict[str, Any], stamp: float) -> None:
    """2xx: commit the debounce stamp and the item's FIRE baseline (locked RMW)."""
    with _NOTIFY_LOCK:
        store = load_attention_store(hass)
        commit_attention_success(store, push, stamp)
        save_attention_store(hass, store)


def _attention_attempt(hass, property_id: str, key: str, push: dict[str, Any], base_now: float, attempt: int) -> bool:
    """One try of one FIRE push. 2xx commits; retryable failure schedules the next try."""
    path_key = str(notify_path(hass))
    stamp_key = str(push.get("stamp_key") or "")
    fire = push.get("fire_class")
    title = push.get("title") or ""
    try:
        status = load_card_id_and_post(
            hass,
            str(property_id),
            str(key),
            title,
            severity=push.get("severity") or "alert",
            manual=False,
            kind="attention",
            rule=str(fire or ""),
            # The title is a fixed rule sentence plus the card label; never user text.
            reason=f"{fire}: {title}",
            attempt=attempt,
            max_attempts=PUSH_MAX_ATTEMPTS,
            # Per-card banner at Backend (smoke and leak on different cards don't replace each other).
            card_id=push.get("presentation_card_id"),
        )
        outcome = dict(getattr(_event_tls, "outcome", None) or last_post_outcome(status))
        retry = bool(getattr(_event_tls, "retry", False))
    except Exception:
        _LOGGER.error("patrimony_attention_push_failed class=%s attempt=%s", fire, attempt)
        _inflight_release(path_key, stamp_key)
        return False
    if outcome.get("ok"):
        elapsed = sum(PUSH_RETRY_DELAYS[: max(0, attempt - 1)])
        try:
            _commit_attention(hass, push, float(base_now) + float(elapsed))
        except Exception:
            _LOGGER.error("patrimony_attention_commit_failed class=%s", fire)
        _inflight_release(path_key, stamp_key)
        _LOGGER.info("patrimony_attention_push class=%s attempt=%s", fire, attempt)
        return True
    if retry:
        delay = retry_delay(attempt)
        _LOGGER.warning(
            "patrimony_attention_push_retry class=%s status=%s attempt=%s next_in=%ss",
            fire, status, attempt, int(delay),
        )

        def _again() -> None:
            _attention_attempt(hass, property_id, key, push, base_now, attempt + 1)

        if schedule_push_retry(hass, delay, _again):
            return False
    # Final failure: nothing committed, so the next evaluation can fire again.
    _inflight_release(path_key, stamp_key)
    _LOGGER.warning(
        "patrimony_attention_push_failed class=%s status=%s attempts=%s",
        fire, status, attempt,
    )
    return False


def dispatch_attention_pushes(hass, facts, property_id, key, now: float | None = None) -> int:
    """Persist the FIRE baseline, then POST events only for new FIRE classes.

    0.4.110 (S3): with a usable key the debounce stamp and the pushed item's
    baseline are committed only after a 2xx. Timeout, connection error, 5xx or
    429 retry up to PUSH_MAX_ATTEMPTS (delays PUSH_RETRY_DELAYS) as scheduled
    executor jobs; a final failure keeps no stamp. Returns first-try 2xx count.
    (S6) No usable hek_/property_id: baseline updates (no replay later), nothing
    is sent, and each transition logs a WARNING and an ok=false missing_key row.
    """
    moment = time.time() if now is None else float(now)
    usable = bool(property_id) and is_usable_house_event_key(key)
    path_key = str(notify_path(hass))
    with _NOTIFY_LOCK:
        store = load_attention_store(hass)
        busy = {stamp for (owner, stamp) in _ATTENTION_INFLIGHT if owner == path_key}
        pushes = plan_automatic_pushes(
            store, facts, moment, commit_debounce=False if not usable else True,
            defer_commit=usable, inflight=busy,
        )
        save_attention_store(hass, store)
        if usable:
            for push in pushes:
                _ATTENTION_INFLIGHT.add((path_key, str(push.get("stamp_key") or "")))
    if not pushes:
        return 0
    if not usable:
        for push in pushes:
            fire = push.get("fire_class")
            _LOGGER.warning("patrimony_attention_push_skipped reason=missing_key class=%s", fire)
            try:
                record_push(
                    hass,
                    kind="attention",
                    rule=str(fire or ""),
                    outcome=dict(MISSING_KEY_OUTCOME),
                    reason=f"{fire}: {push.get('title') or ''}",
                    severity=push.get("severity") or "alert",
                )
            except Exception:
                _LOGGER.error("patrimony_push_log_write_failed")
        return 0
    sent = 0
    for push in pushes:
        title = push.get("title") or ""
        if not title_is_safe(title):
            # Never sendable: commit the baseline without a stamp so it does not loop.
            try:
                _commit_attention(hass, {**push, "stamp_key": ""}, moment)
            finally:
                _inflight_release(path_key, str(push.get("stamp_key") or ""))
            continue
        if _attention_attempt(hass, str(property_id), str(key), push, moment, 1):
            sent += 1
    return sent
