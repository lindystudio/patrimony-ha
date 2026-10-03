"""Hybrid KEM + AEAD for Patrimony PQ shield (Phase 1 private beta).

Combiner: X-Wing (draft-connolly-cfrg-xwing-kem)
  ss = SHA3-256(ss_M || ss_X || ct_X || pk_X || XWingLabel)
  XWingLabel = ASCII "\\.//^\\" = hex 5c2e2f2f5e5c
  ct_encap = ct_M (1088) || ct_X (32)   # 1120 bytes
  pk_hybrid = pk_M (1184) || pk_X (32)  # 1216 bytes

AEAD key (application layer, not full HPKE):
  aead_key = HKDF-SHA256(
      ikm=ss, salt=empty, info=b"patrimony-pq-shield-v1/aes-256-gcm", length=32
  )
  AES-256-GCM, 12-byte nonce, AAD empty

Live wire (session Present): nonce + ct only — no encap.
Crypto imports are lazy so the integration loads when cryptography < 47
(capability reports available: false; live path stays cleartext).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

# draft-connolly-cfrg-xwing-kem XWingLabel = concat("\./", "/^\") = 5c2e2f2f5e5c
XWING_LABEL = bytes([0x5C, 0x2E, 0x2F, 0x2F, 0x5E, 0x5C])
KEM_NAME = "xwing-mlkem768-x25519"
COMBINER_NAME = "xwing-draft-connolly-sha3-256"
AEAD_NAME = "aes-256-gcm"
AEAD_INFO = b"patrimony-pq-shield-v1/aes-256-gcm"
PQ_SHIELD_VERSION = 1

MLKEM_PK_LEN = 1184
MLKEM_CT_LEN = 1088
X25519_LEN = 32
HYBRID_PK_LEN = MLKEM_PK_LEN + X25519_LEN  # 1216
HYBRID_CT_LEN = MLKEM_CT_LEN + X25519_LEN  # 1120
AEAD_KEY_LEN = 32
NONCE_LEN = 12


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _crypto():
    """Import ML-KEM + AEAD primitives. Raises ImportError / UnsupportedAlgorithm."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.mlkem import (
        MLKEM768PrivateKey,
        MLKEM768PublicKey,
    )
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return {
        "hashes": hashes,
        "MLKEM768PrivateKey": MLKEM768PrivateKey,
        "MLKEM768PublicKey": MLKEM768PublicKey,
        "X25519PrivateKey": X25519PrivateKey,
        "X25519PublicKey": X25519PublicKey,
        "AESGCM": AESGCM,
        "HKDF": HKDF,
    }


def xwing_combiner(ss_m: bytes, ss_x: bytes, ct_x: bytes, pk_x: bytes) -> bytes:
    if not (len(ss_m) == 32 and len(ss_x) == 32 and len(ct_x) == 32 and len(pk_x) == 32):
        raise ValueError("X-Wing combiner inputs must be 32 bytes each")
    return hashlib.sha3_256(ss_m + ss_x + ct_x + pk_x + XWING_LABEL).digest()


def derive_aead_key(shared_secret: bytes) -> bytes:
    c = _crypto()
    return c["HKDF"](
        algorithm=c["hashes"].SHA256(),
        length=AEAD_KEY_LEN,
        salt=None,
        info=AEAD_INFO,
    ).derive(shared_secret)


@dataclass
class SessionEnvelope:
    """Live snapshot envelope — session AEAD only (no encap)."""

    nonce: bytes
    ct: bytes
    kem: str = KEM_NAME
    aead: str = AEAD_NAME
    pq_shield: int = PQ_SHIELD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "pqShield": self.pq_shield,
            "kem": self.kem,
            "aead": self.aead,
            "nonce": b64e(self.nonce),
            "ct": b64e(self.ct),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionEnvelope":
        return cls(
            pq_shield=int(data["pqShield"]),
            kem=str(data["kem"]),
            aead=str(data["aead"]),
            nonce=b64d(data["nonce"]),
            ct=b64d(data["ct"]),
        )


class PQShieldRecipient:
    """HA (recipient) hybrid keypair."""

    def __init__(self, mlkem_private: Any, x25519_private: Any) -> None:
        self._mlkem_sk = mlkem_private
        self._x25519_sk = x25519_private
        self._mlkem_pk = mlkem_private.public_key()
        self._x25519_pk = x25519_private.public_key()

    @classmethod
    def generate(cls) -> "PQShieldRecipient":
        c = _crypto()
        return cls(c["MLKEM768PrivateKey"].generate(), c["X25519PrivateKey"].generate())

    @classmethod
    def from_private_raw(cls, mlkem_seed: bytes, x25519_seed: bytes) -> "PQShieldRecipient":
        if len(mlkem_seed) != 64:
            raise ValueError("ML-KEM seed must be 64 bytes")
        if len(x25519_seed) != 32:
            raise ValueError("X25519 seed must be 32 bytes")
        c = _crypto()
        return cls(
            c["MLKEM768PrivateKey"].from_seed_bytes(mlkem_seed),
            c["X25519PrivateKey"].from_private_bytes(x25519_seed),
        )

    def private_raw(self) -> tuple[bytes, bytes]:
        return (
            self._mlkem_sk.private_bytes_raw(),
            self._x25519_sk.private_bytes_raw(),
        )

    def hybrid_public_bytes(self) -> bytes:
        return self._mlkem_pk.public_bytes_raw() + self._x25519_pk.public_bytes_raw()

    def public_material(self) -> dict[str, Any]:
        return {
            "pqShield": PQ_SHIELD_VERSION,
            "kem": KEM_NAME,
            "aead": AEAD_NAME,
            "publicKey": b64e(self.hybrid_public_bytes()),
        }

    def decapsulate(self, encap: bytes) -> bytes:
        if len(encap) != HYBRID_CT_LEN:
            raise ValueError(f"encap must be {HYBRID_CT_LEN} bytes")
        c = _crypto()
        ct_m = encap[:MLKEM_CT_LEN]
        ct_x = encap[MLKEM_CT_LEN:]
        ss_m = self._mlkem_sk.decapsulate(ct_m)
        ss_x = self._x25519_sk.exchange(c["X25519PublicKey"].from_public_bytes(ct_x))
        pk_x = self._x25519_pk.public_bytes_raw()
        return xwing_combiner(ss_m, ss_x, ct_x, pk_x)


def encapsulate_to(hybrid_public_key: bytes) -> tuple[bytes, bytes]:
    """Sender (iOS) side: encapsulate to HA hybrid public key.

    Returns (shared_secret, encap_ciphertext).
    """
    if len(hybrid_public_key) != HYBRID_PK_LEN:
        raise ValueError(f"hybrid public key must be {HYBRID_PK_LEN} bytes")
    c = _crypto()
    pk_m = hybrid_public_key[:MLKEM_PK_LEN]
    pk_x = hybrid_public_key[MLKEM_PK_LEN:]
    mlkem_pk = c["MLKEM768PublicKey"].from_public_bytes(pk_m)
    ss_m, ct_m = mlkem_pk.encapsulate()
    eph = c["X25519PrivateKey"].generate()
    ct_x = eph.public_key().public_bytes_raw()
    ss_x = eph.exchange(c["X25519PublicKey"].from_public_bytes(pk_x))
    ss = xwing_combiner(ss_m, ss_x, ct_x, pk_x)
    return ss, ct_m + ct_x


def aead_seal(aead_key: bytes, plaintext: bytes, nonce: bytes | None = None) -> tuple[bytes, bytes]:
    if len(aead_key) != AEAD_KEY_LEN:
        raise ValueError("aead key must be 32 bytes")
    n = nonce if nonce is not None else os.urandom(NONCE_LEN)
    if len(n) != NONCE_LEN:
        raise ValueError("nonce must be 12 bytes")
    c = _crypto()
    ct = c["AESGCM"](aead_key).encrypt(n, plaintext, None)
    return n, ct


def aead_open(aead_key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    c = _crypto()
    return c["AESGCM"](aead_key).decrypt(nonce, ciphertext, None)


def seal_document(aead_key: bytes, document: dict[str, Any] | bytes) -> SessionEnvelope:
    if isinstance(document, dict):
        plaintext = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    else:
        plaintext = document
    nonce, ct = aead_seal(aead_key, plaintext)
    return SessionEnvelope(nonce=nonce, ct=ct)


def open_document(aead_key: bytes, envelope: SessionEnvelope | dict[str, Any]) -> dict[str, Any]:
    env = envelope if isinstance(envelope, SessionEnvelope) else SessionEnvelope.from_dict(envelope)
    if env.pq_shield != PQ_SHIELD_VERSION:
        raise ValueError("unsupported pqShield version")
    if env.kem != KEM_NAME or env.aead != AEAD_NAME:
        raise ValueError("unsupported kem/aead")
    plaintext = aead_open(aead_key, env.nonce, env.ct)
    return json.loads(plaintext.decode("utf-8"))


def lab_seed_pair(label: str) -> tuple[bytes, bytes]:
    """Fixture seeds only — never used for real houses."""
    mlkem_seed = hashlib.sha512(f"patrimony-pq-shield-lab/mlkem768/{label}".encode()).digest()
    x25519_seed = hashlib.sha256(f"patrimony-pq-shield-lab/x25519/{label}".encode()).digest()
    return mlkem_seed, x25519_seed


PQShieldLab = PQShieldRecipient
