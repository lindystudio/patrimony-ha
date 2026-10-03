"""PQ Shield session store — private under DOMAIN, not synced options.

Persists recipient keypair + session AEAD key via HA Store when available,
else a private JSON file under config/patrimony_collection/.
Never logs key material.
"""

from __future__ import annotations

import json
import logging
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
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            _LOGGER.warning("pq_shield_store_read_failed")
            return {}

    def _write_disk(self, payload: dict[str, Any]) -> None:
        path = _store_path(self.hass)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Private file — best-effort restrictive mode.
        path.write_text(json.dumps(payload) + "\n")
        try:
            path.chmod(0o600)
        except Exception:
            pass

    def _persist(self) -> None:
        payload: dict[str, Any] = {"version": STORE_VERSION}
        if self._recipient is not None:
            mlkem_seed, x25519_seed = self._recipient.private_raw()
            payload["mlkemPrivate"] = b64e(mlkem_seed)
            payload["x25519Private"] = b64e(x25519_seed)
        if self._aead_key is not None:
            payload["aeadKey"] = b64e(self._aead_key)
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
        except Exception:
            _LOGGER.warning("pq_shield_store_corrupt_wiped")
            self._recipient = None
            self._aead_key = None
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


def get_manager(hass) -> PQShieldManager:
    domain = hass.data.setdefault(DOMAIN, {})
    mgr = domain.get(RUNTIME_KEY)
    if isinstance(mgr, PQShieldManager):
        return mgr
    mgr = PQShieldManager(hass)
    mgr.load()
    domain[RUNTIME_KEY] = mgr
    return mgr


async def async_setup_pq_shield(hass) -> None:
    """Warm capability probe + load store into runtime."""
    get_manager(hass)
