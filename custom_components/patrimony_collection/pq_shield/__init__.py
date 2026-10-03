"""Patrimony Collection PQ shield (Phase 1 private beta).

Wire: raw X-Wing + labeled HKDF + AES-256-GCM (see pq-shield-wire-v1-freeze.md).
API: /workspace/patrimony/pq-shield-phase1/api-freeze.md
"""

from .capability import capability_payload, probe_mlkem
from .hybrid import (
    AEAD_INFO,
    AEAD_NAME,
    KEM_NAME,
    PQ_SHIELD_VERSION,
    PQShieldRecipient,
    SessionEnvelope,
    encapsulate_to,
    open_document,
    seal_document,
)
from .session import PQShieldManager, async_setup_pq_shield, get_manager
from .views import register_pq_shield_views

__all__ = [
    "AEAD_INFO",
    "AEAD_NAME",
    "KEM_NAME",
    "PQ_SHIELD_VERSION",
    "PQShieldManager",
    "PQShieldRecipient",
    "SessionEnvelope",
    "async_setup_pq_shield",
    "capability_payload",
    "encapsulate_to",
    "get_manager",
    "open_document",
    "probe_mlkem",
    "register_pq_shield_views",
    "seal_document",
]
