"""Mapped-only activity ring buffer for PresentationDocument.events.

Appends when a mapped item's presented value/label would change on the pass.
Never copies entity_id, area ids, IPs, hostnames, tokens, or raw attributes.
"""

from __future__ import annotations

import re
from typing import Any

from .const import DOMAIN, MAX_EVENT_MESSAGE_CHARS, MAX_EVENTS
from .mapping import humanize_object_id, iso_z, looks_like_entity_id, utc_now_iso

_IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)
_HEK_RE = re.compile(r"hek_[A-Za-z0-9_-]+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}")
_HOST_RE = re.compile(
    r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.(?:local|lan|home|internal|intranet|localdomain))\b",
    re.I,
)
_GENERIC_ENUM_LABELS = frozenset({"condition", "state", "status", "mode"})


def runtime_from_hass(hass: Any) -> dict[str, Any] | None:
    """Return the integration runtime dict, if present."""
    if hass is None:
        return None
    try:
        domain = (getattr(hass, "data", None) or {}).get(DOMAIN) or {}
    except Exception:
        return None
    if not isinstance(domain, dict):
        return None
    for runtime in domain.values():
        if not isinstance(runtime, dict):
            continue
        if "events" in runtime or "presented" in runtime or "unsub" in runtime:
            return runtime
    return None


def runtime_events(hass: Any) -> list[dict[str, str]]:
    runtime = runtime_from_hass(hass)
    if runtime is None:
        return []
    return normalize_events(runtime.get("events"))


def normalize_events(events: Any) -> list[dict[str, str]]:
    """Keep at most 8 sanitized {at, message} rows, newest first."""
    out: list[dict[str, str]] = []
    for row in events or []:
        if not isinstance(row, dict):
            continue
        message = sanitize_message(row.get("message"))
        at = _iso_or_none(row.get("at"))
        if not message or not at:
            continue
        out.append({"at": at, "message": message})
        if len(out) >= MAX_EVENTS:
            break
    return out


def presented_index(document: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """item.id → presented fields used on the pass."""
    out: dict[str, dict[str, Any]] = {}
    for card in (document or {}).get("cards") or []:
        if not isinstance(card, dict):
            continue
        for item in card.get("items") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            out[str(item["id"])] = {
                "label": item.get("label"),
                "value": item.get("value"),
                "valueType": item.get("valueType"),
                "unit": item.get("unit"),
            }
    return out


def _presented_equal(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    return (
        previous.get("label") == current.get("label")
        and previous.get("unit") == current.get("unit")
        and _values_equal(previous.get("value"), current.get("value"))
    )


def _values_equal(left: Any, right: Any) -> bool:
    if left == right:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return False


def record_mapped_activity(
    runtime: dict[str, Any],
    document: dict[str, Any],
    *,
    at: str | None = None,
) -> list[dict[str, str]]:
    """Diff presented items, prepend changes, cap at 8. First snapshot is baseline only."""
    stamp = _iso_or_none(at) or _iso_or_none(document.get("generatedAt")) or utc_now_iso()
    previous = runtime.get("presented") or {}
    current = presented_index(document)
    buffer = normalize_events(runtime.get("events"))
    if previous:
        fresh: list[dict[str, str]] = []
        for item_id, item in current.items():
            prior = previous.get(item_id)
            if prior is None or _presented_equal(prior, item):
                continue
            message = format_activity_message(item)
            if not message:
                continue
            fresh.append({"at": stamp, "message": message})
        if fresh:
            buffer = normalize_events(fresh + buffer)
    runtime["presented"] = current
    runtime["events"] = buffer
    document["events"] = list(buffer)
    return buffer


def format_activity_message(item: dict[str, Any]) -> str | None:
    """Quiet principal-facing line from a presented card item."""
    raw_label = str(item.get("label") or "").strip()
    label = _display_label(raw_label)
    value = item.get("value")
    value_type = item.get("valueType")
    unit = item.get("unit")
    if value is None:
        return None

    if value_type == "bool":
        phrase = _bool_phrase(raw_label, label, value)
    elif value_type == "number":
        phrase = _number_phrase(label, value, unit)
    elif value_type == "enum":
        phrase = _enum_phrase(label, value)
    else:
        phrase = _text_phrase(label, value)
    return sanitize_message(phrase)


def sanitize_message(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split()).strip()
    if not text:
        return None
    if _leaks_private(text):
        return None
    if len(text) > MAX_EVENT_MESSAGE_CHARS:
        text = text[: MAX_EVENT_MESSAGE_CHARS - 1].rstrip() + "…"
    return text


def _display_label(label: str) -> str | None:
    text = (label or "").strip()
    if not text:
        return None
    if looks_like_entity_id(text):
        human = humanize_object_id(text)
        return human if human and not _leaks_private(human) else None
    if _leaks_private(text):
        return None
    return text


def _bool_kind(raw_label: str) -> str:
    if looks_like_entity_id(raw_label):
        domain = raw_label.split(".", 1)[0]
        if domain == "lock":
            return "lock"
        if domain in {"cover", "binary_sensor", "switch"}:
            object_id = raw_label.split(".", 1)[-1]
            return _words_kind(object_id.replace("_", " "))
    return _words_kind(raw_label)


def _words_kind(text: str) -> str:
    lowered = (text or "").casefold()
    if any(word in lowered for word in ("lock", "deadbolt", "latch")):
        return "lock"
    if any(word in lowered for word in ("door", "gate", "garage", "window", "cover")):
        # "Front door" on a pass is usually a lock; match product copy.
        if "door" in lowered and "window" not in lowered and "garage" not in lowered:
            return "lock"
        return "door"
    return "generic"


def _bool_phrase(raw_label: str, label: str | None, value: Any) -> str | None:
    if not isinstance(value, bool):
        return None
    kind = _bool_kind(raw_label)
    if kind == "lock":
        word = "unlocked" if value else "locked"
    elif kind == "door":
        word = "open" if value else "closed"
    else:
        word = "on" if value else "off"
    return f"{label} {word}" if label else word.capitalize()


def _format_number(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number.is_integer():
        return str(int(number))
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or None


def _number_phrase(label: str | None, value: Any, unit: Any) -> str | None:
    formatted = _format_number(value)
    if formatted is None:
        return None
    unit_text = str(unit).strip() if unit not in (None, "") else ""
    if unit_text and _leaks_private(unit_text):
        unit_text = ""
    if label and unit_text in {"°C", "°F", "K"}:
        return f"{label} {formatted}{unit_text}"
    if label and unit_text:
        return f"{label} {formatted} {unit_text}"
    if label:
        return f"{label} {formatted}"
    return f"{formatted} {unit_text}".strip() if unit_text else formatted


def _enum_phrase(label: str | None, value: Any) -> str | None:
    text = str(value).strip()
    if not text or _leaks_private(text):
        return None
    lowered = text.casefold()
    if lowered.startswith("armed "):
        rest = text[6:].strip()
        core = f"armed · {rest}" if rest else "armed"
        return f"{label} {core}" if label else core
    if lowered == "armed":
        return f"{label} armed" if label else "Armed"
    if lowered == "disarmed":
        return f"{label} disarmed" if label else "Disarmed"
    if lowered == "triggered":
        return f"{label} triggered" if label else "Triggered"
    if not label or label.casefold() in _GENERIC_ENUM_LABELS:
        return text
    if label.casefold() == lowered:
        return text
    return f"{label} · {text}"


def _text_phrase(label: str | None, value: Any) -> str | None:
    text = str(value).strip()
    if not text or _leaks_private(text):
        return label
    if not label or label.casefold() == text.casefold():
        return text
    return f"{label} · {text}"


def _leaks_private(text: str) -> bool:
    if not text:
        return False
    if looks_like_entity_id(text):
        return True
    for part in re.split(r"[\s,;|/]+", text):
        token = part.strip().strip(".,:()[]")
        if token and looks_like_entity_id(token):
            return True
    if _IP_RE.search(text) or _HOST_RE.search(text):
        return True
    if _HEK_RE.search(text) or _JWT_RE.search(text):
        return True
    lowered = text.casefold()
    for needle in (
        "entity_id",
        "area_id",
        "device_id",
        "unique_id",
        "access_token",
        "refresh_token",
        "bearer ",
        "hek_",
    ):
        if needle in lowered:
            return True
    return False


def _iso_or_none(raw: Any) -> str | None:
    if raw is None:
        return None
    if hasattr(raw, "tzinfo"):
        try:
            return iso_z(raw)
        except Exception:
            return None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return iso_z(dt)
