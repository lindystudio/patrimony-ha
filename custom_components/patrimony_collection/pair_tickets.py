"""Persistent, single-use pair tickets for /p/{code} (0.4.95).

At rest we keep only:
  id   = sha256("patrimony-pair-id:" + code)       (hex, the dict key)
  box  = AEAD(key=sha256("patrimony-pair-key:" + code), AAD=id, {url, token})
         (stdlib: HMAC-SHA256-CTR + encrypt-then-HMAC-SHA256, 128-bit nonce)
  exp / createdAt / usedAt
The raw code is never written. The token is never written in the clear; it can be
recovered only by someone holding the code (which is what the claim requires anyway).
The code has 192 bits of entropy, so neither the id nor the box is brute-forceable.
Writes are atomic (tmp + os.replace) under a lock; an unreadable file is never
overwritten (claims fail closed until it is fixed). Codes/tokens are never logged.
"""

from __future__ import annotations

from . import fileio

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .const import (
    PAIR_CLAIM_GLOBAL_PER_MIN,
    PAIR_CLAIM_PER_IP_PER_MIN,
    PAIR_CLAIM_REPLAY_GRACE_SECONDS,
    PAIR_CODE_BYTES,
    PAIR_TICKET_FILE,
    PAIR_TICKET_TTL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()
_USED_KEEP_SECONDS = 0  # used tickets are pruned on next write after being marked
_MAX_CODE_LEN = 128


class TicketStoreUnreadable(Exception):
    """pair_tickets.json exists but is not a JSON object. Do not overwrite it."""


def ticket_path(hass) -> Path:
    try:
        return Path(hass.config.path(PAIR_TICKET_FILE))
    except Exception:
        return Path("/config") / PAIR_TICKET_FILE


def new_code() -> str:
    return secrets.token_urlsafe(PAIR_CODE_BYTES)


def _id(code: str) -> str:
    return hashlib.sha256(b"patrimony-pair-id:" + code.encode()).hexdigest()


def _key(code: str) -> bytes:
    return hashlib.sha256(b"patrimony-pair-key:" + code.encode()).digest()


# Stdlib AEAD (no extra dependency): HMAC-SHA256 in counter mode as the stream
# cipher + encrypt-then-MAC with HMAC-SHA256 over id||nonce||ct (separate subkeys).
def _subkey(key: bytes, label: bytes) -> bytes:
    return hmac.new(key, b"patrimony-pair-v1:" + label, hashlib.sha256).digest()


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def _seal(code: str, ticket_id: str, payload: dict[str, str]) -> str:
    key = _key(code)
    nonce = secrets.token_bytes(16)
    pt = json.dumps(payload).encode()
    ct = bytes(a ^ b for a, b in zip(pt, _stream(_subkey(key, b"enc"), nonce, len(pt))))
    tag = hmac.new(_subkey(key, b"mac"), ticket_id.encode() + nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + ct).decode()


def _open(code: str, ticket_id: str, box: str) -> dict[str, str] | None:
    try:
        key = _key(code)
        raw = base64.urlsafe_b64decode(box.encode())
        nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
        want = hmac.new(_subkey(key, b"mac"), ticket_id.encode() + nonce + ct, hashlib.sha256).digest()
        if len(nonce) != 16 or not hmac.compare_digest(tag, want):
            return None
        pt = bytes(a ^ b for a, b in zip(ct, _stream(_subkey(key, b"enc"), nonce, len(ct))))
        out = json.loads(pt)
        return {"url": str(out["url"]), "token": str(out["token"])}
    except Exception:
        return None


def _read(path: Path) -> dict[str, Any]:
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
    raise TicketStoreUnreadable(str(path))


def _write(path: Path, data: dict[str, Any]) -> None:
    # Atomic (tmp + fsync + os.replace), 0600, serialized, cache write-through.
    fileio.write_text(path, json.dumps(data, indent=2) + "\n")


def _prune(tickets: dict[str, Any], now: float) -> None:
    for k in [k for k, v in tickets.items()
              if not isinstance(v, dict) or v.get("usedAt") or float(v.get("exp") or 0) < now]:
        tickets.pop(k, None)


def put_ticket(hass, house_url: str, token: str) -> str:
    """Create a ticket; return the raw code (only ever in the QR/claim URL)."""
    code = new_code()
    tid = _id(code)
    now = time.time()
    path = ticket_path(hass)
    with _LOCK:
        doc = _read(path)  # raises on unreadable: never overwrite
        tickets = doc.get("tickets") if isinstance(doc.get("tickets"), dict) else {}
        _prune(tickets, now)
        tickets[tid] = {
            "box": _seal(code, tid, {"url": house_url, "token": token}),
            "createdAt": now,
            "exp": now + PAIR_TICKET_TTL_SECONDS,
            "usedAt": None,
        }
        doc["version"] = 1
        doc["tickets"] = tickets
        _write(path, doc)
    return code


def claim_ticket(hass, code: str) -> dict[str, str] | None:
    """Atomically consume a ticket. Second claim, expired, or unknown -> None."""
    code = str(code or "").strip()
    if not code or len(code) > _MAX_CODE_LEN:
        return None
    tid = _id(code)
    now = time.time()
    path = ticket_path(hass)
    with _LOCK:
        try:
            doc = _read(path)
        except TicketStoreUnreadable:
            _LOGGER.error("patrimony_pair_tickets_unreadable_not_overwritten")
            return None
        tickets = doc.get("tickets") if isinstance(doc.get("tickets"), dict) else {}
        row = None
        for k in list(tickets):
            if hmac.compare_digest(k, tid):
                row = tickets[k]
                break
        payload = None
        if isinstance(row, dict) and not row.get("usedAt") and float(row.get("exp") or 0) >= now:
            payload = _open(code, tid, str(row.get("box") or ""))
        if row is not None:
            # consume (single-use) and drop: nothing left to replay
            tickets.pop(tid, None)
        before = len(tickets)
        _prune(tickets, now)
        if row is not None or len(tickets) != before:
            doc["version"] = 1
            doc["tickets"] = tickets
            _write(path, doc)
        return payload


class ClaimRateLimiter:
    """In-memory sliding 60 s window, per IP and global."""

    def __init__(self, per_ip: int = PAIR_CLAIM_PER_IP_PER_MIN,
                 global_limit: int = PAIR_CLAIM_GLOBAL_PER_MIN, window: float = 60.0):
        self.per_ip = per_ip
        self.global_limit = global_limit
        self.window = window
        self._ips: dict[str, deque] = {}
        self._all: deque = deque()
        self._lock = threading.Lock()

    def allow(self, ip: str | None, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        with self._lock:
            while self._all and self._all[0] <= cutoff:
                self._all.popleft()
            for k in list(self._ips):
                q = self._ips[k]
                while q and q[0] <= cutoff:
                    q.popleft()
                if not q:
                    del self._ips[k]
            key = str(ip or "unknown")
            q = self._ips.get(key)
            if len(self._all) >= self.global_limit or (q is not None and len(q) >= self.per_ip):
                return False
            self._ips.setdefault(key, deque()).append(now)
            self._all.append(now)
            return True


def claim_limiter(hass) -> ClaimRateLimiter:
    from .const import DOMAIN

    data = getattr(hass, "data", None)
    if data is None:
        hass.data = {}
        data = hass.data
    bucket = data.setdefault(DOMAIN, {})
    lim = bucket.get("pair_claim_limiter")
    if lim is None:
        lim = bucket["pair_claim_limiter"] = ClaimRateLimiter()
    return lim


def _grace(hass) -> dict:
    from .const import DOMAIN

    data = getattr(hass, "data", None)
    if data is None:
        hass.data = {}
        data = hass.data
    return data.setdefault(DOMAIN, {}).setdefault("pair_claim_grace", {})


def claim_with_grace(hass, code: str, ip: str | None, *, now: float | None = None
                     ) -> dict[str, str] | None:
    """Single-use claim with a short replay grace (0.4.96).

    For PAIR_CLAIM_REPLAY_GRACE_SECONDS after a successful claim, the SAME IP
    re-claiming the same code gets the identical payload (iOS/Safari double-fetch).
    Another IP, or after the window, gets None (410). Grace state is memory only.
    """
    now = time.monotonic() if now is None else now
    code = str(code or "").strip()
    if not code or len(code) > _MAX_CODE_LEN:
        return None
    tid = _id(code)
    ip = str(ip or "")
    with _LOCK:
        grace = _grace(hass)
        for k in [k for k, v in grace.items() if v[1] <= now]:
            grace.pop(k, None)
        hit = grace.get(tid)
        if hit is not None:
            return dict(hit[2]) if (ip and hmac.compare_digest(hit[0], ip)) else None
        payload = claim_ticket(hass, code)
        if payload is not None:
            grace[tid] = (ip, now + PAIR_CLAIM_REPLAY_GRACE_SECONDS, dict(payload))
        return payload
