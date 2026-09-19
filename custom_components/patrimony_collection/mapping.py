"""Entity → PresentationDocument mapping, severity, and redaction.

entity_id is consumed from HA storage and never copied onto the wire.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from .const import (
    BOOL_FALSE,
    BOOL_TRUE,
    CONF_CARDS,
    CONF_DISPLAY_NAME,
    CONF_LOCATION_LABEL,
    CONF_MAPPINGS,
    CONF_PROPERTY_ID,
    CONF_TIMEZONE,
    DEFAULT_PRIORITY,
    FORBIDDEN_WIRE_KEYS,
    KIND_DEFAULT_TITLES,
    KINDS,
    MAX_CARDS,
    MAX_ITEMS_PER_CARD,
    MAX_ON_PASS,
    SCHEMA_VERSION,
    SEVERITIES,
    UNAVAILABLE_STATES,
    VALUE_TYPES,
    WEATHER_ALERT_CONDITIONS,
    WEATHER_ATTENTION_CONDITIONS,
    WEATHER_LABELS,
)

try:
    from homeassistant.core import HomeAssistant, State
except ImportError:  # sketch environment without HA installed
    HomeAssistant = Any  # type: ignore[misc,assignment]
    State = Any  # type: ignore[misc,assignment]


def load_shared_mapping(hass: HomeAssistant | None) -> dict[str, Any] | None:
    """Add-on writes this file. entity_id never copied onto the wire."""
    if hass is None:
        return None
    try:
        path = hass.config.path("patrimony_collection/mapping.json")
    except Exception:
        path = "/config/patrimony_collection/mapping.json"
    try:
        from pathlib import Path
        p = Path(path)
        if not p.is_file():
            return None
        import json
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _list(val) -> list:
    return list(val) if isinstance(val, list) else []


def _card_key(row: dict[str, Any]) -> str:
    return str(row.get("id") or "")


def _mapping_key(row: dict[str, Any]) -> str:
    if row.get("item_id"):
        return str(row["item_id"])
    return "|".join(
        str(row.get(k) or "")
        for k in ("card_id", "entity_id", "state_attribute", "label")
    )


def _union_rows(base: list, extra: list, key_fn) -> list:
    """Keep base rows; overlay extra on the same key; append new extra keys. Empty extra is a no-op."""
    if not extra:
        return list(base)
    out: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in base:
        if not isinstance(row, dict):
            continue
        key = key_fn(row) or f"anon-{len(order)}"
        if key not in out:
            order.append(key)
        out[key] = dict(row)
    for row in extra:
        if not isinstance(row, dict):
            continue
        key = key_fn(row) or f"anon-{len(order)}"
        if key not in out:
            order.append(key)
            out[key] = dict(row)
        else:
            merged = dict(out[key])
            merged.update({k: v for k, v in row.items() if v not in (None, "", [])})
            out[key] = merged
    return [out[k] for k in order]


# Weather Outdoor + Condition — never drop if present in the working set.
PROTECTED_WEATHER_ITEM_IDS = frozenset(
    {
        "cb1a007e-ed80-498f-ba16-396c13235ee3",
        "d6d9147d-6018-48de-9a29-71a4a5744421",
    }
)


def _title_key(card: dict[str, Any]) -> str:
    return (card.get("title") or "").strip().casefold()


def _source_key(row: dict[str, Any]) -> tuple[str, str, str]:
    attr = row.get("state_attribute")
    if attr in (None, ""):
        attr = ""
    eid = str(row.get("entity_id") or "")
    if not eid:
        # Placeholders have no source. Collapse is by card title; items remap
        # by card_id. Keep each placeholder by item_id so they are not dropped.
        return (
            f"placeholder:{row.get('item_id') or ''}",
            str(row.get("card_id") or ""),
            (row.get("label") or "").strip(),
        )
    return (
        eid,
        str(attr),
        (row.get("label") or "").strip(),
    )


def _item_count_for(card_id: str, mappings: list[dict[str, Any]]) -> int:
    return sum(1 for row in mappings if str(row.get("card_id") or "") == card_id)


def _dedupe_mappings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per item_id, then one per entity_id+state_attribute+label. Prefer protected weather items."""
    by_item: dict[str, dict[str, Any]] = {}
    item_order: list[str] = []
    anon = 0
    for row in rows:
        iid = str(row.get("item_id") or "")
        if not iid:
            iid = f"anon-{anon}"
            anon += 1
        if iid not in by_item:
            by_item[iid] = row
            item_order.append(iid)
        elif str(row.get("item_id") or "") in PROTECTED_WEATHER_ITEM_IDS:
            by_item[iid] = row
    seen_source: dict[tuple[str, str, str], str] = {}
    out_order: list[str] = []
    for iid in item_order:
        row = by_item[iid]
        sk = _source_key(row)
        if sk not in seen_source:
            seen_source[sk] = iid
            out_order.append(iid)
            continue
        if str(row.get("item_id") or "") in PROTECTED_WEATHER_ITEM_IDS:
            prev = seen_source[sk]
            if prev in out_order:
                out_order.remove(prev)
            seen_source[sk] = iid
            out_order.append(iid)
    return [by_item[i] for i in out_order]


def _row_rank(row: dict[str, Any]) -> int | None:
    raw = row.get("featured_rank")
    if raw is None:
        return None
    try:
        rank = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= rank <= 6:
        return rank
    return None


def _row_on_pass(row: dict[str, Any]) -> bool:
    flag = row.get("on_pass")
    if flag is True:
        return True
    if flag is False:
        return False
    if isinstance(flag, str):
        lowered = flag.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return _row_rank(row) is not None


def assign_featured_ranks(mappings: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """featured_rank 1–6 unique among on_pass items. Seventh and later are ignored."""
    rows = [row for row in (mappings or []) if isinstance(row, dict)]
    checked = [(i, row) for i, row in enumerate(rows) if _row_on_pass(row)]
    checked.sort(key=lambda pair: (_row_rank(pair[1]) or 99, pair[0]))
    kept: set[int] = set()
    for n, (_i, row) in enumerate(checked[:MAX_ON_PASS], start=1):
        row["on_pass"] = True
        row["featured_rank"] = n
        kept.add(id(row))
    for row in rows:
        if id(row) not in kept:
            row.pop("featured_rank", None)
            row.pop("on_pass", None)
    return rows


def featured_items(cards_with_items) -> list:
    """Glance: items with featuredRank 1–6, else legacy first 6 (cards by priority then title)."""
    cards = [c for c in (cards_with_items or []) if isinstance(c, dict)]
    featured: list[dict[str, Any]] = []
    for card in cards:
        for item in card.get("items") or []:
            if not isinstance(item, dict):
                continue
            raw = item.get("featuredRank")
            try:
                rank = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= rank <= 6:
                featured.append(item)
    if featured:
        featured.sort(key=lambda i: int(i.get("featuredRank") or 99))
        return featured[:MAX_ON_PASS]

    def card_key(card: dict[str, Any]) -> tuple[int, str]:
        try:
            priority = int(card.get("priority"))
        except (TypeError, ValueError):
            priority = DEFAULT_PRIORITY
        return (priority, str(card.get("title") or ""))

    out: list[dict[str, Any]] = []
    for card in sorted(cards, key=card_key):
        for item in card.get("items") or []:
            if isinstance(item, dict):
                out.append(item)
            if len(out) >= MAX_ON_PASS:
                return out[:MAX_ON_PASS]
    return out[:MAX_ON_PASS]


def collapse_cards_by_title(
    cards: list | None, mappings: list | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One card per title (case-insensitive trim). Keep the one with more items, then existing order.

    Extra empties of a titled group are dropped. A unique empty card may stay.
    Items from dropped cards are remapped onto the survivor and unioned.
    """
    cards_in = [dict(row) for row in (cards or []) if isinstance(row, dict)]
    maps_in = [dict(row) for row in (mappings or []) if isinstance(row, dict)]

    groups: dict[str, list[int]] = {}
    title_order: list[str] = []
    for index, card in enumerate(cards_in):
        key = _title_key(card)
        if key not in groups:
            groups[key] = []
            title_order.append(key)
        groups[key].append(index)

    survivor_index: dict[str, int] = {}
    for key in title_order:
        idxs = groups[key]
        best = idxs[0]
        best_count = _item_count_for(str(cards_in[best].get("id") or ""), maps_in)
        for index in idxs[1:]:
            count = _item_count_for(str(cards_in[index].get("id") or ""), maps_in)
            if count > best_count:
                best = index
                best_count = count
        survivor_index[key] = best

    id_remap: dict[str, str] = {}
    kept: list[dict[str, Any]] = []
    for index, card in enumerate(cards_in):
        key = _title_key(card)
        surv = survivor_index[key]
        surv_id = str(cards_in[surv].get("id") or "")
        cid = str(card.get("id") or "")
        if index == surv:
            row = dict(card)
            row["kind"] = "custom"
            kept.append(row)
        elif cid and surv_id:
            id_remap[cid] = surv_id

    remapped: list[dict[str, Any]] = []
    for row in maps_in:
        out = dict(row)
        cid = str(out.get("card_id") or "")
        if cid in id_remap:
            out["card_id"] = id_remap[cid]
        remapped.append(out)
    return kept, assign_featured_ranks(_dedupe_mappings(remapped))


def merge_options(entry_data: dict[str, Any], options: dict[str, Any], shared: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    data = dict(entry_data)
    opts = dict(options or {})
    if shared:
        for key, dest in (
            ("property_id", CONF_PROPERTY_ID),
            ("display_name", CONF_DISPLAY_NAME),
            ("location_label", CONF_LOCATION_LABEL),
            ("timezone", CONF_TIMEZONE),
        ):
            val = shared.get(key) or shared.get(dest)
            if val:
                data[dest] = val
        file_cards = _list(shared.get(CONF_CARDS) or shared.get("cards"))
        file_maps = _list(shared.get(CONF_MAPPINGS) or shared.get("mappings"))
        opts[CONF_CARDS] = _union_rows(_list(opts.get(CONF_CARDS)), file_cards, _card_key)
        opts[CONF_MAPPINGS] = _union_rows(_list(opts.get(CONF_MAPPINGS)), file_maps, _mapping_key)
    cards, mappings = collapse_cards_by_title(_list(opts.get(CONF_CARDS)), _list(opts.get(CONF_MAPPINGS)))
    opts[CONF_CARDS] = cards
    opts[CONF_MAPPINGS] = mappings
    return data, opts


def coerce_cards_kind_custom(cards: list | None) -> list[dict[str, Any]]:
    """On save, every card is a label. Schema still requires kind; always write custom."""
    out: list[dict[str, Any]] = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        row = dict(card)
        row["kind"] = "custom"
        out.append(row)
    return out


def normalize_editor_payload(
    body: dict[str, Any] | None,
    existing_cards: list | None,
    existing_maps: list | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sanitize an editor POST. Empty cards/mappings union with what the house already has."""
    body = body or {}
    cards: list[dict[str, Any]] = []
    for card in body.get("cards") or []:
        if not isinstance(card, dict):
            continue
        try:
            priority = int(card.get("priority") or 100)
        except (TypeError, ValueError):
            priority = 100
        cards.append(
            {
                "id": card.get("id") or str(uuid4()),
                "kind": "custom",
                "title": (card.get("title") or "").strip() or "Card",
                "priority": priority,
            }
        )
    mappings: list[dict[str, Any]] = []
    card_ids = {c["id"] for c in cards}
    for row in body.get("mappings") or []:
        if not isinstance(row, dict):
            continue
        if row.get("card_id") not in card_ids:
            continue
        eid = str(row.get("entity_id") or "").strip()
        label = (row.get("label") or "").strip()
        if not eid and not label:
            continue
        rec: dict[str, Any] = {
            "item_id": row.get("item_id") or str(uuid4()),
            "card_id": row["card_id"],
            "entity_id": eid,
            "state_attribute": row.get("state_attribute") or None,
            "label": label,
            "value_type": row.get("value_type") or "auto",
            "unit": row.get("unit") or None,
            "severity_mode": row.get("severity_mode") or "auto",
            "ok_min": row.get("ok_min"),
            "ok_max": row.get("ok_max"),
            "attention_min": row.get("attention_min"),
            "attention_max": row.get("attention_max"),
            "ok_states": row.get("ok_states") or [],
            "attention_states": row.get("attention_states") or [],
            "alert_states": row.get("alert_states") or [],
            "fixed_severity": row.get("fixed_severity"),
        }
        if not eid:
            static_value = row.get("static_value")
            if static_value is None:
                static_value = ""
            rec["static_value"] = static_value
            rec["saved_at"] = row.get("saved_at") or utc_now_iso()
            rec["severity_mode"] = "fixed"
            rec["fixed_severity"] = (
                row.get("fixed_severity") if row.get("fixed_severity") in SEVERITIES else "ok"
            )
            unit = rec["unit"]
            if isinstance(unit, str):
                unit = unit.strip() or None
                rec["unit"] = unit
            raw = str(static_value).strip() if static_value is not None else ""
            parsed = _parse_number(raw) if raw else None
            if parsed is not None and unit:
                rec["value_type"] = "number"
            else:
                vt = rec["value_type"]
                rec["value_type"] = vt if vt in VALUE_TYPES else "text"
        if _row_on_pass(row):
            rec["on_pass"] = True
            rank = _row_rank(row)
            if rank is not None:
                rec["featured_rank"] = rank
        mappings.append(rec)
    if not mappings and existing_maps:
        mappings = [dict(row) for row in existing_maps if isinstance(row, dict)]
    if not cards and existing_cards:
        cards = [dict(row) for row in existing_cards if isinstance(row, dict)]
    cards = coerce_cards_kind_custom(cards)
    return collapse_cards_by_title(cards, mappings)


def seed_shared_mapping(hass: HomeAssistant | None, entry_data: dict[str, Any], options: dict[str, Any]) -> None:
    """Write mapping.json from config-entry options if the file is missing or empty. Never seed an empty wipe."""
    if hass is None:
        return
    cards = _list((options or {}).get(CONF_CARDS))
    mappings = _list((options or {}).get(CONF_MAPPINGS))
    if not cards and not mappings:
        return
    try:
        path = hass.config.path("patrimony_collection/mapping.json")
    except Exception:
        path = "/config/patrimony_collection/mapping.json"
    from pathlib import Path
    import json
    p = Path(path)
    existing = None
    if p.is_file():
        try:
            existing = json.loads(p.read_text())
        except Exception:
            existing = None
    if isinstance(existing, dict) and (_list(existing.get("cards")) or _list(existing.get("mappings"))):
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "property_id": entry_data.get(CONF_PROPERTY_ID),
        "display_name": entry_data.get(CONF_DISPLAY_NAME),
        "location_label": entry_data.get(CONF_LOCATION_LABEL),
        "timezone": entry_data.get(CONF_TIMEZONE),
        "cards": cards,
        "mappings": mappings,
    }
    p.write_text(json.dumps(payload, indent=2) + "\n")



def house_local_time(hass: HomeAssistant | None, tz_name: str | None) -> str:
    """24-hour HH:mm in the property IANA zone, from HA's clock."""
    from zoneinfo import ZoneInfo

    name = (tz_name or "").strip() or "UTC"
    try:
        zone = ZoneInfo(name)
    except Exception:
        zone = timezone.utc
    now = None
    if hass is not None:
        now_fn = getattr(hass, "now", None)
        if callable(now_fn):
            try:
                now = now_fn()
            except Exception:
                now = None
    if now is None:
        now = datetime.now(zone)
    elif getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=zone)
    else:
        now = now.astimezone(zone)
    return now.strftime("%H:%M")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_z(dt: datetime | None) -> str:
    if dt is None:
        return utc_now_iso()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def latest_iso(values) -> str | None:
    """Newest ISO-8601 UTC (`…Z`) among values. Invalid stamps are skipped."""
    stamps: list[str] = []
    for raw in values or []:
        if isinstance(raw, datetime):
            stamps.append(iso_z(raw))
            continue
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        stamps.append(iso_z(dt))
    if not stamps:
        return None
    return max(stamps)


def is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def looks_like_entity_id(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if "." not in value or " " in value:
        return False
    domain, _, object_id = value.partition(".")
    return bool(domain) and bool(object_id) and domain.isidentifier() and object_id.replace("_", "").isalnum()


def humanize_object_id(entity_id: str) -> str:
    object_id = entity_id.split(".", 1)[-1]
    text = object_id.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Item"


def humanize_enum(state: str) -> str:
    token = state.strip().lower().replace("_", "-")
    if token in WEATHER_LABELS:
        return WEATHER_LABELS[token]
    return state.replace("_", " ").replace("-", " ").strip().title()


def parse_iso_age_hours(raw: str) -> float | None:
    text = raw.strip()
    if "T" not in text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600.0
    if hours < 0:
        return None
    return round(hours, 1)


def redact_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop forbidden keys at every level. Does not log the dropped values."""

    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for key, val in obj.items():
                if key in FORBIDDEN_WIRE_KEYS or key.endswith("_entity_id"):
                    continue
                out[key] = scrub(val)
            return out
        if isinstance(obj, list):
            return [scrub(item) for item in obj]
        return obj

    return scrub(doc)


def _parse_bool(raw: str) -> bool | None:
    lowered = raw.strip().lower()
    if lowered in BOOL_TRUE:
        return True
    if lowered in BOOL_FALSE:
        return False
    return None


def _parse_number(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


TEMP_UNITS = {
    "°c": "°C",
    "c": "°C",
    "celsius": "°C",
    "°f": "°F",
    "f": "°F",
    "fahrenheit": "°F",
    "k": "K",
    "kelvin": "K",
    "°k": "K",
}
TEMP_ATTRS = frozenset({"temperature", "current_temperature"})


def normalize_temp_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    key = str(unit).strip().lower().replace(" ", "")
    return TEMP_UNITS.get(key)


def house_temperature_unit(hass: HomeAssistant | None) -> str | None:
    if hass is None:
        return None
    try:
        unit = hass.config.units.temperature_unit
    except Exception:
        return None
    return normalize_temp_unit(unit) or (str(unit) if unit else None)


def convert_temperature(value: float, from_u: str | None, to_u: str | None) -> float:
    src = normalize_temp_unit(from_u)
    dst = normalize_temp_unit(to_u)
    if src is None or dst is None or src == dst:
        return float(value)
    try:
        from homeassistant.util.unit_conversion import TemperatureConverter

        return float(TemperatureConverter.convert(float(value), src, dst))
    except Exception:
        return _fallback_convert_temperature(float(value), src, dst)


def _fallback_convert_temperature(value: float, src: str, dst: str) -> float:
    def to_c(v: float, unit: str) -> float:
        if unit == "°C":
            return v
        if unit == "°F":
            return (v - 32.0) * 5.0 / 9.0
        if unit == "K":
            return v - 273.15
        return v

    celsius = to_c(value, src)
    if dst == "°C":
        return celsius
    if dst == "°F":
        return celsius * 9.0 / 5.0 + 32.0
    if dst == "K":
        return celsius + 273.15
    return value


def _native_temp_unit(state: State | None, mapping: dict[str, Any] | None = None) -> str | None:
    if state is not None:
        attrs = state.attributes or {}
        native = normalize_temp_unit(attrs.get("unit_of_measurement") or attrs.get("temperature_unit"))
        if native:
            return native
    if mapping:
        return normalize_temp_unit(mapping.get("unit"))
    return None


def _is_temperature_item(mapping: dict[str, Any], state: State | None) -> bool:
    if (mapping.get("state_attribute") or "") in TEMP_ATTRS:
        return True
    if state is not None:
        attrs = state.attributes or {}
        if attrs.get("device_class") == "temperature":
            return True
        if normalize_temp_unit(attrs.get("unit_of_measurement") or attrs.get("temperature_unit")):
            return True
    return bool(normalize_temp_unit((mapping or {}).get("unit")))


def apply_house_temperature(
    value: Any,
    mapping: dict[str, Any],
    state: State | None,
    hass: HomeAssistant | None,
) -> tuple[Any, str | None]:
    """Convert a numeric temperature to the house unit. Returns (value, house_unit or None)."""
    if not isinstance(value, (int, float)):
        return value, None
    explicit = mapping.get("unit")
    if explicit and not normalize_temp_unit(str(explicit)):
        # Different quantity (%, h, d, W, …): keep raw number and that unit.
        return value, None
    if not _is_temperature_item(mapping, state):
        return value, None
    house = house_temperature_unit(hass)
    if not house:
        return value, None
    native = _native_temp_unit(state, mapping)
    if native is None:
        return value, None
    if native == house:
        return value, house
    return round(convert_temperature(float(value), native, house), 1), house


def live_entity_fields(hass: HomeAssistant | None, state: State | None) -> dict[str, Any]:
    """Editor live line: converted state/unit for temperature sensors and weather temperature."""
    if state is None:
        return {"state": None, "unit": None, "attributes": {}}
    attrs = dict(getattr(state, "attributes", None) or {})
    raw_state = getattr(state, "state", None)
    unit = attrs.get("unit_of_measurement") or attrs.get("temperature_unit")
    house = house_temperature_unit(hass)
    sensor_native = normalize_temp_unit(attrs.get("unit_of_measurement"))
    weather_native = normalize_temp_unit(attrs.get("temperature_unit"))
    attr_native = sensor_native or weather_native
    out_state: Any = raw_state
    out_unit: Any = unit

    is_temp_sensor = attrs.get("device_class") == "temperature" or sensor_native is not None
    if is_temp_sensor and house:
        native = sensor_native or attr_native
        parsed = _parse_number(str(raw_state) if raw_state is not None else "")
        if parsed is not None and native:
            if native != house:
                out_state = round(convert_temperature(parsed, native, house), 1)
            else:
                out_state = parsed
            out_unit = house
        elif native:
            out_unit = house
    elif weather_native and house:
        out_unit = house

    live_attrs: dict[str, Any] = {}
    for key in ("temperature", "current_temperature", "humidity", "hvac_mode", "unit_of_measurement"):
        if key not in attrs:
            continue
        val = attrs[key]
        if key in TEMP_ATTRS and house and attr_native:
            parsed_attr = _parse_number(str(val) if val is not None else "")
            if parsed_attr is not None:
                if attr_native != house:
                    live_attrs[key] = round(convert_temperature(parsed_attr, attr_native, house), 1)
                else:
                    live_attrs[key] = parsed_attr
                continue
        if key == "unit_of_measurement" and house and normalize_temp_unit(val):
            live_attrs[key] = house
            continue
        live_attrs[key] = val
    return {"state": out_state, "unit": out_unit, "attributes": live_attrs}


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if entity_id else ""


def infer_value_type(
    entity_id: str,
    state: State | None,
    declared: str | None,
    attribute: str | None = None,
) -> str:
    if declared in VALUE_TYPES:
        return declared
    if attribute in TEMP_ATTRS:
        return "number"
    domain = _domain(entity_id)
    if domain in {"binary_sensor", "switch", "input_boolean", "light", "lock"}:
        return "bool"
    if domain in {"alarm_control_panel", "climate", "cover"}:
        if domain == "climate":
            return "enum"
        return "enum"
    if domain == "weather":
        return "enum"
    if domain == "sensor" and state is not None:
        device_class = (state.attributes or {}).get("device_class")
        unit = (state.attributes or {}).get("unit_of_measurement")
        if device_class in {"timestamp"} or "uptime" in entity_id:
            return "number"
        if device_class in {"temperature", "humidity", "pressure", "power", "energy"} or unit:
            # energy sensors are still items; kind must stay custom if so.
            return "number"
    return "text"


def item_value(
    value_type: str, raw: str | None
) -> tuple[Any, bool]:
    """Return (json_value, ok_to_emit). ok_to_emit False → drop the item (schema mismatch)."""
    if raw is None or str(raw).strip().lower() in UNAVAILABLE_STATES:
        return None, True
    text = str(raw).strip()
    if looks_like_entity_id(text) or "://" in text:
        return None, True
    if value_type == "bool":
        parsed = _parse_bool(text)
        if parsed is None:
            return None, False
        return parsed, True
    if value_type == "number":
        parsed = _parse_number(text)
        if parsed is None:
            hours = parse_iso_age_hours(text)
            if hours is None:
                return None, False
            if hours >= 48:
                return round(hours / 24.0, 1), True
            return hours, True
        return parsed, True
    if value_type == "enum":
        return humanize_enum(text), True
    if value_type == "text":
        return text, True
    return None, False


def _auto_severity(
    *,
    entity_id: str,
    kind: str,
    state: State | None,
    raw: str | None,
    json_value: Any,
    device_class: str | None,
) -> str:
    if json_value is None or raw is None or str(raw).strip().lower() in UNAVAILABLE_STATES:
        return "attention"
    domain = _domain(entity_id)
    lowered = str(raw).strip().lower()
    if domain == "alarm_control_panel":
        if lowered == "triggered":
            return "alert"
        if lowered in {"pending", "arming", "disarming"}:
            return "attention"
        if lowered.startswith("armed"):
            return "ok"
        if lowered == "disarmed":
            return "attention"
    if domain == "binary_sensor" and device_class in {
        "smoke",
        "gas",
        "heat",
        "safety",
        "problem",
        "tamper",
        "carbon_monoxide",
    }:
        return "alert" if json_value is True else "ok"
    if domain == "binary_sensor" and device_class in {"door", "window", "garage_door"}:
        return "attention" if json_value is True else "ok"
    if kind == "network" or device_class == "connectivity":
        if isinstance(json_value, bool):
            return "ok" if json_value else "attention"
    if domain == "weather" or (kind in {"climate", "custom"} and not isinstance(json_value, (int, float))):
        token = lowered.replace(" ", "-")
        if token in WEATHER_ALERT_CONDITIONS:
            return "alert"
        if token in WEATHER_ATTENTION_CONDITIONS:
            return "attention"
        # sunny / cloudy / rainy / clear-night / fog / etc. → ok
        if domain == "weather":
            return "ok"
    return "ok"


def decide_severity(mapping: dict[str, Any], *, entity_id: str, kind: str, state: State | None, raw: str | None, json_value: Any) -> str:
    mode = mapping.get("severity_mode") or "auto"
    device_class = None
    if state is not None:
        device_class = (state.attributes or {}).get("device_class")

    if json_value is None or (raw is not None and str(raw).strip().lower() in UNAVAILABLE_STATES):
        if mode != "fixed":
            return "attention"

    if mode == "fixed":
        fixed = mapping.get("fixed_severity")
        return fixed if fixed in SEVERITIES else "attention"
    if mode == "ok_when_on":
        if isinstance(json_value, bool):
            return "ok" if json_value else "attention"
        return "attention"
    if mode == "ok_when_off":
        if isinstance(json_value, bool):
            return "ok" if not json_value else "attention"
        return "attention"
    if mode == "binary_alert_on":
        if isinstance(json_value, bool):
            return "alert" if json_value else "ok"
        return "attention"
    if mode == "enum_map":
        token = (raw or "").strip().lower()
        for bucket, key in (
            ("alert", "alert_states"),
            ("attention", "attention_states"),
            ("ok", "ok_states"),
        ):
            states = [str(s).strip().lower() for s in (mapping.get(key) or [])]
            if token in states:
                return bucket
        return "attention"
    if mode == "number_range":
        if not isinstance(json_value, (int, float)):
            return "attention"
        ok_min, ok_max = mapping.get("ok_min"), mapping.get("ok_max")
        if ok_min is not None and ok_max is not None and ok_min <= json_value <= ok_max:
            return "ok"
        att_min, att_max = mapping.get("attention_min"), mapping.get("attention_max")
        if att_min is not None and att_max is not None and att_min <= json_value <= att_max:
            return "attention"
        if ok_min is not None and ok_max is not None:
            return "alert"
        return "attention"
    return _auto_severity(
        entity_id=entity_id,
        kind=kind,
        state=state,
        raw=raw,
        json_value=json_value,
        device_class=device_class,
    )


def _raw_from_state(state: State | None, attribute: str | None) -> str | None:
    if state is None:
        return None
    if attribute:
        value = (state.attributes or {}).get(attribute)
        return None if value is None else str(value)
    return None if state.state is None else str(state.state)


def _label_for(mapping: dict[str, Any], entity_id: str, state: State | None) -> str:
    explicit = (mapping.get("label") or "").strip()
    if explicit:
        return explicit
    if state is not None:
        friendly = str((state.attributes or {}).get("friendly_name") or "").strip()
        if friendly and not looks_like_entity_id(friendly):
            return friendly
    return humanize_object_id(entity_id)


def _unit_for(
    value_type: str,
    mapping: dict[str, Any],
    state: State | None,
    hass: HomeAssistant | None = None,
    house_unit: str | None = None,
) -> str | None:
    if value_type != "number":
        return None
    explicit = mapping.get("unit")
    if explicit and not normalize_temp_unit(str(explicit)):
        return str(explicit)
    eid = str(mapping.get("entity_id") or "")
    if "uptime" in eid or (
        state is not None and (state.attributes or {}).get("device_class") == "timestamp"
    ):
        raw = None
        if state is not None and state.state is not None:
            raw = str(state.state)
        hours = parse_iso_age_hours(raw) if raw else None
        if hours is not None:
            return "d" if hours >= 48 else "h"
        if "uptime" in eid:
            return "h"
    if house_unit:
        return house_unit
    house = house_temperature_unit(hass)
    if house and _is_temperature_item(mapping, state):
        return house
    if explicit:
        return str(explicit)
    if state is not None:
        unit = (state.attributes or {}).get("unit_of_measurement") or (
            state.attributes or {}
        ).get("temperature_unit")
        if unit:
            return str(unit)
    return None


def _empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not str(value).strip():
        return True
    return False


def _skip_empty_generic_item(label: str | None, value: Any) -> bool:
    """Do not emit empty 'Item — —' rows (empty climate placeholders)."""
    lab = (label or "").strip()
    if not _empty_value(value):
        return False
    return (not lab) or lab.casefold() == "item"


def _placeholder_value(mapping: dict[str, Any]) -> tuple[Any, str, str | None]:
    """Typed placeholder: (json_value, value_type, unit). House mapping only."""
    raw = mapping.get("static_value")
    if raw is None:
        raw_text = None
    else:
        raw_text = str(raw).strip() or None
    unit = mapping.get("unit") or None
    if isinstance(unit, str):
        unit = unit.strip() or None

    if raw_text is None:
        return None, "text", None
    if looks_like_entity_id(raw_text) or "://" in raw_text:
        return None, "text", None

    parsed = _parse_number(raw_text)
    if parsed is not None and unit:
        return parsed, "number", str(unit)

    declared = mapping.get("value_type")
    if declared in VALUE_TYPES and declared not in {"text", "auto"}:
        json_value, ok = item_value(declared, raw_text)
        if ok:
            out_unit = str(unit) if declared == "number" and unit else None
            return json_value, declared, out_unit
    return raw_text, "text", None


def _updated_at_from_saved(mapping: dict[str, Any]) -> str:
    raw = mapping.get("saved_at")
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            return iso_z(dt)
        except ValueError:
            pass
    return utc_now_iso()


def _attach_featured_rank(item: dict[str, Any] | None, mapping: dict[str, Any]) -> dict[str, Any] | None:
    if item is None:
        return None
    if mapping.get("on_pass") is False:
        return item
    rank = _row_rank(mapping)
    if rank is None:
        return item
    item["featuredRank"] = rank
    return item


def _build_placeholder_item(mapping: dict[str, Any]) -> dict[str, Any] | None:
    item_id = mapping.get("item_id")
    if not item_id or not is_uuid(str(item_id)):
        return None
    label = (mapping.get("label") or "").strip()
    json_value, value_type, unit = _placeholder_value(mapping)
    if _skip_empty_generic_item(label, json_value):
        return None
    if not label:
        return None
    return {
        "id": str(item_id),
        "label": label,
        "value": json_value,
        "valueType": value_type,
        "unit": unit if value_type == "number" else None,
        "severity": "ok",
        "updatedAt": _updated_at_from_saved(mapping),
    }


def _build_item(
    mapping: dict[str, Any],
    card_kind: str,
    hass: HomeAssistant | None,
) -> dict[str, Any] | None:
    item_id = mapping.get("item_id")
    entity_id = mapping.get("entity_id") or ""
    if not item_id or not is_uuid(str(item_id)):
        return None
    if not str(entity_id).strip():
        return _attach_featured_rank(_build_placeholder_item(mapping), mapping)

    state = None
    if hass is not None and entity_id:
        try:
            state = hass.states.get(entity_id)
        except Exception:
            state = None

    value_type = infer_value_type(
        entity_id, state, mapping.get("value_type"), mapping.get("state_attribute")
    )
    raw = _raw_from_state(state, mapping.get("state_attribute"))
    json_value, emit_ok = item_value(value_type, raw)
    if not emit_ok:
        return None

    json_value, house_unit = apply_house_temperature(json_value, mapping, state, hass)

    updated = None
    if state is not None:
        updated = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)

    item = {
        "id": str(item_id),
        "label": _label_for(mapping, entity_id, state),
        "value": json_value,
        "valueType": value_type,
        "unit": _unit_for(value_type, mapping, state, hass, house_unit),
        "severity": decide_severity(
            mapping,
            entity_id=entity_id,
            kind=card_kind,
            state=state,
            raw=raw,
            json_value=json_value,
        ),
        "updatedAt": iso_z(updated),
    }
    if not item["label"]:
        return None
    if _skip_empty_generic_item(item["label"], item["value"]):
        return None
    if item["severity"] not in SEVERITIES:
        item["severity"] = "attention"
    return _attach_featured_rank(item, mapping)


def build_presentation_document(
    hass: HomeAssistant | None,
    entry_data: dict[str, Any],
    options: dict[str, Any],
    *,
    generated_at: str | None = None,
    events: list | None = None,
) -> dict[str, Any]:
    """Assemble a full PresentationDocument. Never includes entity_id."""
    shared = load_shared_mapping(hass)
    entry_data, options = merge_options(entry_data, options, shared)
    collapsed_cards, collapsed_maps = collapse_cards_by_title(
        _list(options.get(CONF_CARDS)), _list(options.get(CONF_MAPPINGS))
    )
    options = dict(options)
    options[CONF_CARDS] = collapsed_cards
    options[CONF_MAPPINGS] = collapsed_maps
    cards_meta = {row["id"]: row for row in (options.get(CONF_CARDS) or []) if row.get("id")}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for mapping in options.get(CONF_MAPPINGS) or []:
        card_id = mapping.get("card_id")
        if not card_id or card_id not in cards_meta:
            continue
        grouped.setdefault(card_id, []).append(mapping)

    generated = generated_at or utc_now_iso()
    cards: list[dict[str, Any]] = []
    for card_id, mappings in grouped.items():
        meta = cards_meta[card_id]
        # Title is the product label. Always emit kind custom so schema 1.0
        # does not bump. Stored kind is only used for item severity heuristics.
        stored_kind = meta.get("kind") if meta.get("kind") in KINDS else "custom"
        title = (meta.get("title") or "").strip() or KIND_DEFAULT_TITLES.get(
            stored_kind, "Custom"
        )
        try:
            priority = int(meta.get("priority"))
        except (TypeError, ValueError):
            priority = DEFAULT_PRIORITY

        items: list[dict[str, Any]] = []
        for mapping in mappings:
            item = _build_item(mapping, stored_kind, hass)
            if item:
                items.append(item)
        items = items[:MAX_ITEMS_PER_CARD]
        cards.append(
            {
                "id": str(card_id),
                "kind": "custom",
                "title": title,
                "priority": priority,
                "items": items,
                "lastUpdated": latest_iso(item.get("updatedAt") for item in items)
                or generated,
            }
        )

    cards.sort(key=lambda card: (card["priority"], card["title"]))
    cards = cards[:MAX_CARDS]

    location = (entry_data.get(CONF_LOCATION_LABEL) or "").strip()
    property_obj: dict[str, Any] = {
        "id": str(entry_data[CONF_PROPERTY_ID]),
        "displayName": str(entry_data[CONF_DISPLAY_NAME]).strip(),
        "timezone": str(entry_data[CONF_TIMEZONE]).strip(),
    }
    if location:
        property_obj["locationLabel"] = location
    property_obj["localTime"] = house_local_time(hass, entry_data.get(CONF_TIMEZONE))
    # House-health clock: when this snapshot was assembled. iOS treats a
    # cached lastHeard (or a failed fetch) as offline/stale — not a quiet ok.
    property_obj["lastHeard"] = generated

    # Activity ring buffer is additive (schema 1). Pull the current mapped-only
    # buffer from runtime when the caller does not pass one. Empty is valid.
    from .activity import normalize_events, runtime_events

    if events is None:
        events = runtime_events(hass)

    document = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated,
        # Root lastHeard is the iOS house-health clock (same instant as
        # generatedAt). property.lastHeard mirrors it. Additive; schema 1.
        "lastHeard": generated,
        "property": property_obj,
        "cards": cards,
        "events": normalize_events(events),
    }
    return redact_document(document)



# Demo helpers for offline tests and fixtures/demo-*.json. Not a live house.
DEMO_ENTRY_DATA = {
    CONF_PROPERTY_ID: "00000000-0000-4000-8000-000000000001",
    CONF_DISPLAY_NAME: "Demo Home",
    CONF_LOCATION_LABEL: "Example",
    CONF_TIMEZONE: "UTC",
}
DEMO_OPTIONS = {
    CONF_CARDS: [
        {"id": "bb2e5bc9-d780-4721-8884-71c7ad2de495", "kind": "network", "title": "Network", "priority": 10},
        {"id": "978e4347-d65d-4421-8f98-4d9ef5cc6398", "kind": "climate", "title": "Climate", "priority": 20},
        {"id": "43ed4b93-19a9-46d8-8ff7-bf896a8954f9", "kind": "security", "title": "Alarm", "priority": 5},
        {"id": "2cc3fed2-74b7-42fe-8e4f-4bed95b7fb46", "kind": "custom", "title": "Weather", "priority": 25},
    ],
    CONF_MAPPINGS: [
        {
            "item_id": "1497ff6e-f94b-466d-a1ae-ab5b2c2b8590",
            "card_id": "bb2e5bc9-d780-4721-8884-71c7ad2de495",
            "entity_id": "binary_sensor.demo_guest_wifi",
            "label": "Guest Wi-Fi",
            "value_type": "bool",
            "severity_mode": "ok_when_on",
        },
        {
            "item_id": "713a1316-a312-4132-bd42-4a0432ccf85a",
            "card_id": "978e4347-d65d-4421-8f98-4d9ef5cc6398",
            "entity_id": "sensor.demo_indoor_temp",
            "label": "Indoor",
            "value_type": "number",
            "unit": "°C",
            "severity_mode": "number_range",
            "ok_min": 18,
            "ok_max": 24,
            "attention_min": 10,
            "attention_max": 28,
        },
        {
            "item_id": "769ba028-4477-451c-8a2e-b6e3472ee898",
            "card_id": "43ed4b93-19a9-46d8-8ff7-bf896a8954f9",
            "entity_id": "alarm_control_panel.demo_home",
            "label": "Alarm",
            "value_type": "enum",
            "severity_mode": "auto",
        },
        {
            "item_id": "cb1a007e-ed80-498f-ba16-396c13235ee3",
            "card_id": "2cc3fed2-74b7-42fe-8e4f-4bed95b7fb46",
            "entity_id": "weather.home",
            "state_attribute": "temperature",
            "label": "Outdoor",
            "value_type": "number",
            "unit": "°C",
            "severity_mode": "auto",
        },
        {
            "item_id": "d6d9147d-6018-48de-9a29-71a4a5744421",
            "card_id": "2cc3fed2-74b7-42fe-8e4f-4bed95b7fb46",
            "entity_id": "weather.home",
            "state_attribute": None,
            "label": "Condition",
            "value_type": "enum",
            "severity_mode": "auto",
        },
    ],
}
