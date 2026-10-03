"""Attention-only push decisions. Default is QUIET.

FIRE is a transition into a class that should interrupt. Card glance severity
may still change without a push. No secrets, no entity ids, no second events schema.
"""

from __future__ import annotations

from typing import Any

# Inside the 15–30 minute cooldown from the freeze.
DEBOUNCE_SECONDS = 20 * 60

_UNAVAILABLE = frozenset({"unavailable", "unknown", "none", ""})
_QUIET_DOMAINS = frozenset(
    {
        "light",
        "switch",
        "fan",
        "media_player",
        "weather",
        "climate",
        "sun",
        "person",
        "device_tracker",
        "zone",
        "automation",
        "script",
        "scene",
    }
)
_LEAK_CLASSES = frozenset({"moisture", "leak", "water"})
_SAFETY_CLASSES = frozenset({"safety", "carbon_monoxide", "heat"})
_FIXED_TITLES = {
    "security.alarm": "Alarm triggered",
    "security.smoke": "Smoke detected",
    "security.gas": "Gas detected",
    "security.leak": "Leak detected",
    "security.safety": "Safety alert",
    "security.lock": "Lock alert",
    "presentation": "House presentation blocked",
}


def title_is_safe(title: str) -> bool:
    """Reject titles that would put a key or token on the events path."""
    lowered = title.lower()
    if "hek_" in lowered or "eyj" in lowered:
        return False
    return True


def classify_fact(fact: dict[str, Any] | None) -> str | None:
    """Return a FIRE class if this row is currently in a FIRE state, else None.

    None means QUIET. ok↔attention without a FIRE class stays None.
    Entity unavailable / unknown is QUIET even if glance severity is attention.
    """
    if not isinstance(fact, dict):
        return None
    raw = str(fact.get("raw") or "").strip().lower()
    if raw in _UNAVAILABLE:
        return None
    kind = str(fact.get("kind") or "").strip().lower()
    if kind == "energy":
        return None
    domain = str(fact.get("domain") or "").strip().lower()
    device = str(fact.get("device_class") or "").strip().lower()
    severity = str(fact.get("severity") or "").strip().lower()
    on = bool(fact.get("on"))

    if domain == "alarm_control_panel" and raw == "triggered":
        return "security.alarm"
    if domain == "binary_sensor" and on and device == "smoke":
        return "security.smoke"
    if domain == "binary_sensor" and on and device == "gas":
        return "security.gas"
    if domain == "binary_sensor" and on and device in _LEAK_CLASSES:
        return "security.leak"
    if domain == "binary_sensor" and on and device in _SAFETY_CLASSES:
        return "security.safety"
    # Auto lock severity is ok for both locked and unlocked. Alert means the
    # mapping treats the lock as needing action.
    if domain == "lock" and severity == "alert":
        return "security.lock"

    if kind == "network" or device == "connectivity" or domain in _QUIET_DOMAINS:
        return None
    if severity == "alert":
        return "alert"
    return None


def local_failure_class(reason: str | None) -> str | None:
    """Rare local presentation failure only.

    Entity unavailable, iOS offline, and lastHeard are not push triggers.
    Snapshot exceptions are not this class: the product does not surface them
    as an urgent principal state.
    """
    if reason == "presentation_blocked":
        return "presentation"
    return None


def title_for(fire_class: str, label: Any = None) -> str:
    fixed = _FIXED_TITLES.get(fire_class)
    if fixed:
        return fixed
    text = "" if label is None else str(label).strip()
    if not text or not title_is_safe(text):
        return "Needs attention"
    if text.lower().endswith(" alert"):
        title = text
    else:
        title = f"{text} alert"
    return title[:120]


def wire_severity(fire_class: str) -> str:
    """Manual Soon Notify stays attention. Security and mapped alert use alert."""
    if fire_class == "manual":
        return "attention"
    return "alert"


def manual_notify_decision(key_usable: bool, title: Any) -> dict[str, str] | None:
    """Explicit Soon Notify. Missing key, empty title, or a secret-shaped title → no push."""
    if not key_usable:
        return None
    text = "" if title is None else str(title).strip()
    if not text:
        return None
    if len(text) > 120:
        text = text[:120]
    if not title_is_safe(text):
        return None
    return {"fire_class": "manual", "severity": "attention", "title": text}


def plan_automatic_pushes(
    store: dict[str, Any],
    facts: list[dict[str, Any]] | None,
    now: float,
    *,
    window: float = DEBOUNCE_SECONDS,
    commit_debounce: bool = True,
) -> list[dict[str, str]]:
    """Transitions into a new FIRE class. First sight seeds and does not push.

    Debounce key is presentation card id + FIRE class. A different class on the
    same card still fires (escalation). Repeats inside `window` do not.
    `commit_debounce` is false when the house cannot send (no hek_): the baseline
    still updates so a later key does not replay the current state.
    """
    prev = dict(store.get("classes") or {})
    seeded = bool(store.get("seeded"))
    current: dict[str, str] = {}
    transitions: list[tuple[dict[str, Any], str]] = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        item_id = str(fact.get("item_id") or "").strip()
        if not item_id:
            continue
        fire = classify_fact(fact) or ""
        current[item_id] = fire
        if seeded and fire and fire != prev.get(item_id, ""):
            transitions.append((fact, fire))
    store["seeded"] = True
    store["classes"] = {key: value for key, value in current.items() if value}
    pushed = dict(store.get("pushed") or {})
    if not seeded:
        store["pushed"] = pushed
        return []

    allowed: list[dict[str, str]] = []
    for fact, fire in transitions:
        card = str(fact.get("card_id") or "").strip() or str(fact.get("item_id"))
        stamp_key = f"{card}|{fire}"
        last = pushed.get(stamp_key)
        if isinstance(last, (int, float)) and (float(now) - float(last)) < window:
            continue
        if commit_debounce:
            pushed[stamp_key] = float(now)
        allowed.append(
            {
                "fire_class": fire,
                "severity": wire_severity(fire),
                "title": title_for(fire, fact.get("label")),
                "card_id": card,
            }
        )
    if commit_debounce:
        horizon = max(window * 4, 6 * 3600)
        pushed = {
            key: value
            for key, value in pushed.items()
            if isinstance(value, (int, float)) and float(now) - float(value) < horizon
        }
    store["pushed"] = pushed
    return allowed
