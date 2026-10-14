"""PQ Shield session store — private under DOMAIN, not synced options.

Persists recipient keypair + session AEAD key via HA Store when available,
else a private JSON file under config/patrimony_collection/.
Never logs key material.
"""

from __future__ import annotations

from .. import fileio

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from ..const import CONF_PQ_SHIELD, DOMAIN, PQ_SHIELD_STORE_FILE
from .capability import capability_payload, probe_mlkem
from .hybrid import (
    HYBRID_CT_LEN,
    PQShieldRecipient,
    b64d,
    b64e,
    derive_aead_key,
    seal_document,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
RUNTIME_KEY = "pq_shield"
_WRITE_LOCK = threading.Lock()


async def run_blocking(hass, func, *args):
    """Run a blocking store op off the event loop (inline when no executor, e.g. unit fakes)."""
    runner = getattr(hass, "async_add_executor_job", None)
    if runner is None:
        return func(*args)
    return await runner(func, *args)


def _store_path(hass) -> Path:
    try:
        return Path(hass.config.path(PQ_SHIELD_STORE_FILE))
    except Exception:
        return Path("/config") / PQ_SHIELD_STORE_FILE


def _feature_enabled(hass) -> bool:
    entry = _first_entry(hass)
    if entry is None:
        return False
    opts = entry.options or {}
    return bool(opts.get(CONF_PQ_SHIELD, True))


def _first_entry(hass):
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:
        return None
    return entries[0] if entries else None


class PQShieldManager:
    """In-memory + durable PQ shield state for one HA instance."""

    def __init__(self, hass) -> None:
        self.hass = hass
        self._recipient: PQShieldRecipient | None = None
        self._aead_key: bytes | None = None
        self._available, self._probe_reason = probe_mlkem()
        self._loaded = False
        # 0.4.98: per-member PQ sessions {memberId: aead_key(32)}. Never logged.
        self._member_keys: dict[str, bytes] = {}

    @property
    def available(self) -> bool:
        return bool(self._available)

    @property
    def feature_enabled(self) -> bool:
        return _feature_enabled(self.hass)

    @property
    def session_present(self) -> bool:
        return self._aead_key is not None and len(self._aead_key) == 32

    def capability(self) -> dict[str, Any]:
        return capability_payload(
            feature_enabled=self.feature_enabled,
            session_present=self.session_present,
            available=self.available,
            reason=None if self.available else self._probe_reason,
        )

    def _read_disk(self) -> dict[str, Any]:
        path = _store_path(self.hass)
        raw = fileio.read_text(path)
        if raw is None:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            _LOGGER.warning("pq_shield_store_read_failed")
            return {}

    def _write_disk(self, payload: dict[str, Any]) -> None:
        """Atomic (tmp + fsync + os.replace), 0600, serialized, cache write-through."""
        with _WRITE_LOCK:
            fileio.write_text(_store_path(self.hass), json.dumps(payload) + "\n")

    def _persist(self) -> None:
        payload: dict[str, Any] = {"version": STORE_VERSION}
        if self._recipient is not None:
            mlkem_seed, x25519_seed = self._recipient.private_raw()
            payload["mlkemPrivate"] = b64e(mlkem_seed)
            payload["x25519Private"] = b64e(x25519_seed)
        if self._aead_key is not None:
            payload["aeadKey"] = b64e(self._aead_key)
        if self._member_keys:
            payload["memberSessions"] = {m: b64e(k) for m, k in self._member_keys.items()}
        self._write_disk(payload)

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.available:
            return
        data = self._read_disk()
        try:
            mlkem_b64 = data.get("mlkemPrivate")
            x25519_b64 = data.get("x25519Private")
            if isinstance(mlkem_b64, str) and isinstance(x25519_b64, str):
                self._recipient = PQShieldRecipient.from_private_raw(
                    b64d(mlkem_b64), b64d(x25519_b64)
                )
            aead_b64 = data.get("aeadKey")
            if isinstance(aead_b64, str):
                key = b64d(aead_b64)
                if len(key) == 32:
                    self._aead_key = key
            members = data.get("memberSessions")
            if isinstance(members, dict):
                for mid, kb64 in members.items():
                    if isinstance(mid, str) and isinstance(kb64, str):
                        mk = b64d(kb64)
                        if len(mk) == 32:
                            self._member_keys[mid] = mk
        except Exception:
            _LOGGER.warning("pq_shield_store_corrupt_wiped")
            self._recipient = None
            self._aead_key = None
            self._member_keys = {}
            self._write_disk({"version": STORE_VERSION})

    def ensure_recipient(self) -> PQShieldRecipient:
        self.load()
        if not self.available:
            raise RuntimeError("pq_shield_unavailable")
        if self._recipient is None:
            self._recipient = PQShieldRecipient.generate()
            self._persist()
        return self._recipient

    def public_material(self) -> dict[str, Any]:
        return self.ensure_recipient().public_material()

    def accept_encap(self, encap: bytes) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("unavailable")
        if not self.feature_enabled:
            raise RuntimeError("disabled")
        if len(encap) != HYBRID_CT_LEN:
            raise ValueError("bad_encap")
        recipient = self.ensure_recipient()
        ss = recipient.decapsulate(encap)
        self._aead_key = derive_aead_key(ss)
        self._persist()
        return {"ok": True, "session": "present"}

    def rotate(self) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("unavailable")
        if not self.feature_enabled:
            raise RuntimeError("disabled")
        self._aead_key = None
        self._recipient = PQShieldRecipient.generate()
        self._persist()
        return self._recipient.public_material()

    def disable(self, *, wipe_keypair: bool = True) -> dict[str, Any]:
        self.load()
        self._aead_key = None
        self._member_keys = {}
        if wipe_keypair:
            self._recipient = None
        self._write_disk({"version": STORE_VERSION})
        return {"ok": True, "session": "absent"}

    def maybe_wrap_document(self, document: dict[str, Any]) -> dict[str, Any]:
        """Wrap when feature On + session Present; else cleartext (fail closed)."""
        self.load()
        if not self.available or not self.feature_enabled or not self.session_present:
            return document
        assert self._aead_key is not None
        try:
            return seal_document(self._aead_key, document).to_dict()
        except Exception:
            _LOGGER.error("pq_shield_wrap_failed")
            # Fail closed → cleartext TLS-only path rather than drop the house.
            return document

    def session_aead_key_for_tests(self) -> bytes | None:
        """Test-only accessor. Do not expose on HTTP."""
        return self._aead_key

    # ---- 0.4.98 member PQ sessions (same KEM/AEAD/envelope as the owner) ----

    @property
    def shield_on(self) -> bool:
        """Owner PQ shield ON for this house (feature enabled and ML-KEM available)."""
        return bool(self.available and self.feature_enabled)

    def member_session_present(self, member_id: str) -> bool:
        self.load()
        return len(self._member_keys.get(str(member_id), b"")) == 32

    def member_capability(self, member_id: str) -> dict[str, Any]:
        """Owner capability shape, with session = THIS member's session state."""
        return capability_payload(
            feature_enabled=self.feature_enabled,
            session_present=self.member_session_present(member_id),
            available=self.available,
            reason=None if self.available else self._probe_reason,
        )

    def accept_member_encap(self, member_id: str, encap: bytes) -> dict[str, Any]:
        """Blocking. Decapsulate a member's encap to the house recipient; store its own key."""
        if not self.available:
            raise RuntimeError("unavailable")
        if not self.feature_enabled:
            raise RuntimeError("disabled")
        if len(encap) != HYBRID_CT_LEN:
            raise ValueError("bad_encap")
        mid = str(member_id or "")
        if not mid:
            raise ValueError("bad_member")
        recipient = self.ensure_recipient()
        self._member_keys[mid] = derive_aead_key(recipient.decapsulate(encap))
        self._persist()
        return {"ok": True, "session": "present"}

    def drop_member_sessions(self, member_ids) -> int:
        """Blocking. Delete member PQ sessions (revoke / expiry / member rotate)."""
        self.load()
        n = 0
        for mid in {str(m) for m in (member_ids or ()) if m}:
            if self._member_keys.pop(mid, None) is not None:
                n += 1
        if n:
            self._persist()
        return n

    def wrap_for_member(self, member_id: str, document: dict[str, Any]) -> dict[str, Any] | None:
        """Envelope with THIS member's key; plain doc when shield Off; None = no session (caller decides 428/plain)."""
        self.load()
        if not self.shield_on:
            return document
        key = self._member_keys.get(str(member_id))
        if key is None or len(key) != 32:
            return None
        return seal_document(key, document).to_dict()

    def member_aead_key_for_tests(self, member_id: str) -> bytes | None:
        """Test-only accessor. Do not expose on HTTP."""
        return self._member_keys.get(str(member_id))


def _build_manager(hass) -> PQShieldManager:
    """Blocking: probes ML-KEM and reads pq_shield.json. Run in an executor."""
    mgr = PQShieldManager(hass)
    mgr.load()
    return mgr


def get_manager(hass) -> PQShieldManager:
    domain = hass.data.setdefault(DOMAIN, {})
    mgr = domain.get(RUNTIME_KEY)
    if isinstance(mgr, PQShieldManager):
        return mgr
    # Fallback only (setup normally pre-warms off-loop via async_setup_pq_shield).
    mgr = _build_manager(hass)
    domain[RUNTIME_KEY] = mgr
    return mgr


async def async_setup_pq_shield(hass) -> None:
    """Warm capability probe + load store into runtime, off the event loop."""
    domain = hass.data.setdefault(DOMAIN, {})
    if isinstance(domain.get(RUNTIME_KEY), PQShieldManager):
        return
    mgr = await run_blocking(hass, _build_manager, hass)
    domain.setdefault(RUNTIME_KEY, mgr)
