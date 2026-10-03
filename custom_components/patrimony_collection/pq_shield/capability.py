"""PQ shield floor detection (cryptography ML-KEM / version >= 47)."""

from __future__ import annotations

from typing import Any

from .hybrid import AEAD_NAME, KEM_NAME, PQ_SHIELD_VERSION

REASON_CRYPTO_BELOW_FLOOR = "cryptography_below_floor"
REASON_DISABLED = "disabled"
REASON_ERROR = "error"

_CRYPTO_FLOOR = (47, 0, 0)


def _parse_version(text: str) -> tuple[int, int, int]:
    parts: list[int] = []
    for token in str(text).split("."):
        digits = ""
        for ch in token:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def probe_mlkem() -> tuple[bool, str | None]:
    """Return (available, reason). Fail closed on any import/API error."""
    try:
        import cryptography

        if _parse_version(cryptography.__version__) < _CRYPTO_FLOOR:
            return False, REASON_CRYPTO_BELOW_FLOOR
    except Exception:
        return False, REASON_CRYPTO_BELOW_FLOOR

    try:
        from cryptography.hazmat.primitives.asymmetric.mlkem import (  # noqa: F401
            MLKEM768PrivateKey,
            MLKEM768PublicKey,
        )
        from cryptography.hazmat.primitives.asymmetric.x25519 import (  # noqa: F401
            X25519PrivateKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: F401
    except Exception:
        return False, REASON_CRYPTO_BELOW_FLOOR

    return True, None


def capability_payload(
    *,
    feature_enabled: bool,
    session_present: bool,
    available: bool | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Shape frozen in pq-shield-phase1/api-freeze.md."""
    if available is None:
        available, reason = probe_mlkem()

    if not available:
        out_reason = reason or REASON_CRYPTO_BELOW_FLOOR
    elif not feature_enabled:
        out_reason = REASON_DISABLED
    else:
        out_reason = None

    return {
        "pqShield": PQ_SHIELD_VERSION,
        "available": bool(available),
        "kem": KEM_NAME,
        "aead": AEAD_NAME,
        "session": "present"
        if (bool(available) and feature_enabled and session_present)
        else "absent",
        "reason": out_reason,
    }
