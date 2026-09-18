"""Ingress UI: filter/select entities to expose. No HA tokens persisted."""

from __future__ import annotations

import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

PORT = int(os.environ.get("INGRESS_PORT", "8099"))
HA = os.environ.get("SUPERVISOR_URL", "http://supervisor/core")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
OPTIONS_PATH = Path("/data/options.json")
MAPPING_PATH = Path("/config/patrimony_collection/mapping.json")
CONTACTS_PATH = Path("/config/patrimony_collection/contacts.json")
PHOTO_PATH = Path("/config/patrimony_collection/face.jpg")
DEFAULT_PHOTO_PATH = Path(__file__).resolve().parent / "default.jpg"
NOTES_PATH = Path("/config/patrimony_collection/notes.json")
NOTIFY_PATH = Path("/config/patrimony_collection/notify.json")
CONTACTS_METHODS = ("cellular", "viber", "whatsapp")
MAX_CONTACTS = 40
PHOTO_MAX_BYTES = 2 * 1024 * 1024
NOTES_MAX_CHARS = 8000
NOTIFY_TITLE_MAX = 120
BACKEND_BASE = "https://api.patrimonycollection.com"
KINDS = ("security", "climate", "network", "cellar", "arrivals", "custom")

STATIC = Path(__file__).resolve().parent / "static"


ADDRESS_PART_KEYS = (
    "street",
    "address_line",
    "address_line1",
    "addressLine",
    "address",
    "house_number",
    "houseNumber",
    "city",
    "state",
    "province",
    "postal_code",
    "postalCode",
    "zip",
    "zip_code",
    "zipcode",
    "country",
    "county",
    "district",
    "address_line2",
)
LOCATION_VALUE_KEYS = ("location", "location_label", "locationLabel")
LOCATION_MAX_CHARS = 200


def _first_text(*values):
    for val in values:
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def join_address_parts(src):
    if not isinstance(src, dict):
        return ""
    parts, seen = [], set()
    for key in ADDRESS_PART_KEYS:
        val = src.get(key)
        if not isinstance(val, str):
            continue
        text = val.strip()
        if not text:
            continue
        folded = text.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        parts.append(text)
    return ", ".join(parts)


def resolve_location(*sources):
    for src in sources:
        if not isinstance(src, dict):
            continue
        explicit = _first_text(*(src.get(key) for key in LOCATION_VALUE_KEYS))
        if explicit:
            return explicit[:LOCATION_MAX_CHARS]
    for src in sources:
        if not isinstance(src, dict):
            continue
        joined = join_address_parts(src)
        if joined:
            return joined[:LOCATION_MAX_CHARS]
    return ""


def load_options() -> dict:
    raw = {}
    if OPTIONS_PATH.is_file():
        try:
            loaded = json.loads(OPTIONS_PATH.read_text())
            if isinstance(loaded, dict):
                raw = loaded
        except Exception:
            raw = {}
    return {
        "property_id": raw.get("property_id") or "",
        "display_name": raw.get("display_name") or "",
        "location": resolve_location(raw),
        "timezone": raw.get("timezone") or "UTC",
        "house_event_key": raw.get("house_event_key") or "",
    }


def _rows(val):
    return list(val) if isinstance(val, list) else []


def _union(base, extra, key):
    if not extra:
        return list(base)
    out, order = {}, []
    for row in list(base) + list(extra):
        if not isinstance(row, dict):
            continue
        k = row.get(key) or row.get("item_id") or row.get("entity_id") or str(len(order))
        if k not in out:
            order.append(k)
            out[k] = dict(row)
        else:
            merged = dict(out[k])
            merged.update({a: b for a, b in row.items() if b not in (None, "", [])})
            out[k] = merged
    return [out[k] for k in order]


PROTECTED_WEATHER_ITEM_IDS = frozenset(
    {
        "cb1a007e-ed80-498f-ba16-396c13235ee3",
        "d6d9147d-6018-48de-9a29-71a4a5744421",
    }
)


def collapse_cards_by_title(cards, mappings):
    """One card per title; keep the one with items; union extras. Unique empty may stay."""
    cards_in = [dict(row) for row in (cards or []) if isinstance(row, dict)]
    maps_in = [dict(row) for row in (mappings or []) if isinstance(row, dict)]

    def title_key(card):
        return (card.get("title") or "").strip().casefold()

    def item_count(card_id):
        return sum(1 for row in maps_in if str(row.get("card_id") or "") == card_id)

    groups, title_order = {}, []
    for index, card in enumerate(cards_in):
        key = title_key(card)
        if key not in groups:
            groups[key] = []
            title_order.append(key)
        groups[key].append(index)

    survivor_index = {}
    for key in title_order:
        idxs = groups[key]
        best = idxs[0]
        best_count = item_count(str(cards_in[best].get("id") or ""))
        for index in idxs[1:]:
            count = item_count(str(cards_in[index].get("id") or ""))
            if count > best_count:
                best, best_count = index, count
        survivor_index[key] = best

    id_remap, kept = {}, []
    for index, card in enumerate(cards_in):
        key = title_key(card)
        surv = survivor_index[key]
        surv_id = str(cards_in[surv].get("id") or "")
        cid = str(card.get("id") or "")
        if index == surv:
            row = dict(card)
            row["kind"] = "custom"
            kept.append(row)
        elif cid and surv_id:
            id_remap[cid] = surv_id

    remapped = []
    for row in maps_in:
        out = dict(row)
        cid = str(out.get("card_id") or "")
        if cid in id_remap:
            out["card_id"] = id_remap[cid]
        remapped.append(out)

    by_item, item_order, anon = {}, [], 0
    for row in remapped:
        iid = str(row.get("item_id") or "")
        if not iid:
            iid = f"anon-{anon}"
            anon += 1
        if iid not in by_item:
            by_item[iid] = row
            item_order.append(iid)
        elif str(row.get("item_id") or "") in PROTECTED_WEATHER_ITEM_IDS:
            by_item[iid] = row

    def source_key(row):
        attr = row.get("state_attribute")
        if attr in (None, ""):
            attr = ""
        return (str(row.get("entity_id") or ""), str(attr), (row.get("label") or "").strip())

    seen_source, out_order = {}, []
    for iid in item_order:
        row = by_item[iid]
        sk = source_key(row)
        if sk not in seen_source:
            seen_source[sk] = iid
            out_order.append(iid)
        elif str(row.get("item_id") or "") in PROTECTED_WEATHER_ITEM_IDS:
            prev = seen_source[sk]
            if prev in out_order:
                out_order.remove(prev)
            seen_source[sk] = iid
            out_order.append(iid)
    return kept, assign_featured_ranks([by_item[i] for i in out_order])


MAX_ON_PASS = 6


def _row_rank(row):
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


def _row_on_pass(row):
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


def assign_featured_ranks(mappings):
    rows = [row for row in (mappings or []) if isinstance(row, dict)]
    checked = [(i, row) for i, row in enumerate(rows) if _row_on_pass(row)]
    checked.sort(key=lambda pair: (_row_rank(pair[1]) or 99, pair[0]))
    kept = set()
    for n, (_i, row) in enumerate(checked[:MAX_ON_PASS], start=1):
        row["on_pass"] = True
        row["featured_rank"] = n
        kept.add(id(row))
    for row in rows:
        if id(row) not in kept:
            row.pop("featured_rank", None)
            row.pop("on_pass", None)
    return rows


def load_mapping() -> dict:
    if MAPPING_PATH.is_file():
        try:
            data = json.loads(MAPPING_PATH.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"cards": [], "mappings": []}


def seed_mapping_if_empty() -> None:
    current = load_mapping()
    if _rows(current.get("cards")) or _rows(current.get("mappings")):
        return
    seed = Path(__file__).resolve().parent / "seed_demo.json"
    if not seed.is_file():
        return
    MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAPPING_PATH.write_text(seed.read_text())


def save_mapping(doc: dict) -> None:
    existing = load_mapping()
    incoming_cards = _rows(doc.get("cards"))
    incoming_maps = _rows(doc.get("mappings"))
    if not incoming_maps and _rows(existing.get("mappings")):
        mappings = _union(_rows(existing.get("mappings")), incoming_maps, "item_id")
        cards = _union(_rows(existing.get("cards")), incoming_cards, "id") if incoming_cards else _rows(existing.get("cards"))
    else:
        # Full current UI state: replace so a confirmed remove sticks.
        mappings = incoming_maps
        cards = incoming_cards or _rows(existing.get("cards"))
    coerced = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        row = dict(card)
        row["kind"] = "custom"
        coerced.append(row)
    cards = coerced
    cards, mappings = collapse_cards_by_title(cards, mappings)
    MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
    opts = load_options()
    incoming = dict(doc or {})
    loc = resolve_location(incoming, incoming.get("property") if isinstance(incoming.get("property"), dict) else {}, existing, opts)
    if any(k in incoming for k in LOCATION_VALUE_KEYS + ADDRESS_PART_KEYS):
        loc = resolve_location(incoming, incoming.get("property") if isinstance(incoming.get("property"), dict) else {})
    out = {
        "property_id": doc.get("property_id") or existing.get("property_id") or opts.get("property_id"),
        "display_name": doc.get("display_name") or existing.get("display_name") or opts.get("display_name"),
        "location": loc,
        "timezone": doc.get("timezone") or existing.get("timezone") or opts.get("timezone"),
        "cards": cards,
        "mappings": mappings,
    }
    MAPPING_PATH.write_text(json.dumps(out, indent=2) + "\n")


def ha_get(path: str):
    req = Request(
        f"{HA}{path}",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


TEMP_UNITS = {
    "°c": "°C",
    "c": "°C",
    "celsius": "°C",
    "°f": "°F",
    "f": "°F",
    "fahrenheit": "°F",
}
TEMP_ATTRS = frozenset({"temperature", "current_temperature"})


def _norm_temp(unit):
    if unit is None:
        return None
    return TEMP_UNITS.get(str(unit).strip().lower().replace(" ", ""))


def _to_c(value, unit):
    if unit == "°F":
        return (value - 32.0) * 5.0 / 9.0
    if unit == "K":
        return value - 273.15
    return value


def _convert_temp(value, from_u, to_u):
    src, dst = _norm_temp(from_u), _norm_temp(to_u)
    if src is None or dst is None or src == dst:
        return float(value)
    c = _to_c(float(value), src)
    if dst == "°F":
        return c * 9.0 / 5.0 + 32.0
    if dst == "K":
        return c + 273.15
    return c


def house_temp_unit() -> str:
    try:
        cfg = ha_get("/api/config")
        unit = ((cfg or {}).get("unit_system") or {}).get("temperature")
        return _norm_temp(unit) or (str(unit) if unit else "°C")
    except Exception:
        return "°C"


def _parse_num(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def live_entity_fields(item: dict, house: str) -> dict:
    attrs = item.get("attributes") or {}
    raw_state = item.get("state")
    unit = attrs.get("unit_of_measurement") or attrs.get("temperature_unit")
    sensor_native = _norm_temp(attrs.get("unit_of_measurement"))
    weather_native = _norm_temp(attrs.get("temperature_unit"))
    attr_native = sensor_native or weather_native
    out_state, out_unit = raw_state, unit
    is_temp_sensor = attrs.get("device_class") == "temperature" or sensor_native is not None
    if is_temp_sensor and house:
        native = sensor_native or attr_native
        parsed = _parse_num(raw_state)
        if parsed is not None and native:
            out_state = round(_convert_temp(parsed, native, house), 1) if native != house else parsed
            out_unit = house
        elif native:
            out_unit = house
    elif weather_native and house:
        out_unit = house
    live_attrs = {}
    for key in ("temperature", "current_temperature", "humidity", "hvac_mode", "unit_of_measurement"):
        if key not in attrs:
            continue
        val = attrs[key]
        if key in TEMP_ATTRS and house and attr_native:
            parsed_attr = _parse_num(val)
            if parsed_attr is not None:
                live_attrs[key] = (
                    round(_convert_temp(parsed_attr, attr_native, house), 1)
                    if attr_native != house
                    else parsed_attr
                )
                continue
        if key == "unit_of_measurement" and house and _norm_temp(val):
            live_attrs[key] = house
            continue
        live_attrs[key] = val
    return {"state": out_state, "unit": out_unit, "attributes": live_attrs}


def list_entities() -> list[dict]:
    if not TOKEN:
        return []
    states = ha_get("/api/states")
    house = house_temp_unit()
    rows = []
    for item in states:
        eid = item.get("entity_id") or ""
        attrs = item.get("attributes") or {}
        live = live_entity_fields(item, house)
        rows.append(
            {
                "entity_id": eid,
                "domain": eid.split(".", 1)[0] if "." in eid else "",
                "name": attrs.get("friendly_name") or eid,
                "state": live["state"],
                "unit": live["unit"],
                "attributes": live["attributes"],
            }
        )
    rows.sort(key=lambda r: (r["domain"], r["name"]))
    return rows




def normalize_contacts(raw) -> list:
    rows = raw if isinstance(raw, list) else []
    out, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        function = str(row.get("function") or row.get("title") or "").strip()[:80]
        name = str(row.get("name") or "").strip()[:80]
        tel = str(row.get("tel") or row.get("phone") or "").strip()[:40]
        method = str(row.get("method") or "cellular").strip().lower()
        if method not in CONTACTS_METHODS:
            method = "cellular"
        cid = str(row.get("id") or "").strip() or str(uuid.uuid4())
        digits = "".join(ch for ch in tel if ch.isdigit() or ch == "+")
        if not function and not name and not digits:
            continue
        if cid in seen:
            cid = str(uuid.uuid4())
        seen.add(cid)
        out.append({"id": cid, "function": function, "name": name, "tel": tel, "method": method})
        if len(out) >= MAX_CONTACTS:
            break
    return out


def load_contacts() -> dict:
    try:
        if CONTACTS_PATH.is_file():
            data = json.loads(CONTACTS_PATH.read_text())
            rows = data.get("contacts") if isinstance(data, dict) else data
            return {"schemaVersion": 1, "contacts": normalize_contacts(rows)}
    except Exception:
        pass
    return {"schemaVersion": 1, "contacts": []}


def save_contacts(raw) -> dict:
    rows = raw.get("contacts") if isinstance(raw, dict) else raw
    contacts = normalize_contacts(rows)
    CONTACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schemaVersion": 1, "contacts": contacts}
    CONTACTS_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    return payload



def sniff_photo_type(data: bytes):
    if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def load_default_photo():
    try:
        if not DEFAULT_PHOTO_PATH.is_file():
            return None
        data = DEFAULT_PHOTO_PATH.read_bytes()
    except Exception:
        return None
    if not data or len(data) > PHOTO_MAX_BYTES:
        return None
    if sniff_photo_type(data) is None:
        return None
    return data


def resolve_photo():
    if PHOTO_PATH.is_file():
        data = PHOTO_PATH.read_bytes()
        return data, sniff_photo_type(data) or "image/jpeg", "custom"
    bundled = load_default_photo()
    if bundled:
        return bundled, sniff_photo_type(bundled) or "image/jpeg", "default"
    return None


def restore_default_photo() -> bool:
    data = load_default_photo()
    if not data:
        return False
    PHOTO_PATH.parent.mkdir(parents=True, exist_ok=True)
    PHOTO_PATH.write_bytes(data)
    return True


def load_notes_doc():
    opts = load_options()
    pid = opts.get("property_id")
    try:
        if NOTES_PATH.is_file():
            data = json.loads(NOTES_PATH.read_text())
            if isinstance(data, dict):
                text = str(data.get("text") if data.get("text") is not None else "")[:NOTES_MAX_CHARS]
                updated = data.get("updatedAt")
                return {
                    "schemaVersion": 1,
                    "propertyId": pid,
                    "text": text,
                    "updatedAt": str(updated) if updated is not None else None,
                }
    except Exception:
        pass
    return {"schemaVersion": 1, "propertyId": pid, "text": "", "updatedAt": None}


def save_notes_doc(raw) -> dict:
    from datetime import datetime, timezone
    text = ""
    if isinstance(raw, dict):
        text = "" if raw.get("text") is None else str(raw.get("text"))
    if len(text) > NOTES_MAX_CHARS:
        text = text[:NOTES_MAX_CHARS]
    payload = {
        "schemaVersion": 1,
        "propertyId": load_options().get("property_id"),
        "text": text,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTES_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def house_event_key():
    key = str(load_options().get("house_event_key") or "").strip()
    if not key.startswith("hek_") or len(key) < 5:
        return None
    if key.startswith("eyJ") or "eyJ" in key or "token" in key.lower():
        return None
    return key


def load_notify_card_id() -> str:
    try:
        if NOTIFY_PATH.is_file():
            data = json.loads(NOTIFY_PATH.read_text())
            if isinstance(data, dict):
                cid = str(data.get("cardId") or "").strip()
                if cid:
                    return cid
    except Exception:
        pass
    cid = str(uuid.uuid4())
    NOTIFY_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTIFY_PATH.write_text(json.dumps({"cardId": cid}, indent=2) + "\n")
    return cid


def post_house_event(property_id, key, card_id, title):
    from urllib.error import HTTPError, URLError
    payload = json.dumps({"cardId": card_id, "severity": "attention", "title": title}).encode()
    req = Request(
        f"{BACKEND_BASE}/v1/properties/{property_id}/events",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "X-House-Event-Key": key},
    )
    try:
        with urlopen(req, timeout=8) as resp:
            return int(getattr(resp, "status", 200) or 200)
    except HTTPError as exc:
        return int(exc.code or 502)
    except (URLError, TimeoutError, OSError):
        return 502


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _json(self, code: int, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html", "text/html; charset=utf-8")
        if path == "/api/house":
            opts = load_options()
            mapping = load_mapping()
            cards, mappings = collapse_cards_by_title(mapping.get("cards") or [], mapping.get("mappings") or [])
            loc = resolve_location(mapping, opts)
            house = {
                "property_id": mapping.get("property_id") or opts.get("property_id"),
                "display_name": mapping.get("display_name") or opts.get("display_name"),
                "location": loc,
                "timezone": mapping.get("timezone") or opts.get("timezone"),
                "cards": cards,
                "mappings": mappings,
            }
            return self._json(200, house)
        if path in ("/api/contacts", "api/contacts"):
            opts = load_options()
            doc = load_contacts()
            doc["propertyId"] = opts.get("property_id")
            return self._json(200, doc)
        if path.startswith("/api/entities"):
            q = ""
            if "?" in self.path:
                from urllib.parse import parse_qs, urlparse
                q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0].strip().lower()
            rows = list_entities()
            if q:
                rows = [r for r in rows if q in str(r.get("name") or "").lower()]
            return self._json(200, {"entities": rows, "kinds": list(KINDS)})
        if path in ("/api/photo", "api/photo"):
            resolved = resolve_photo()
            if resolved is None:
                self.send_error(404)
                return
            data, ctype, source = resolved
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Patrimony-Photo-Source", source)
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/api/notes", "api/notes"):
            return self._json(200, load_notes_doc())
        if path in ("/api/notify", "api/notify"):
            return self._json(200, {"configured": house_event_key() is not None})
        self.send_error(404)

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        if path in ("/api/photo", "api/photo"):
            if length > PHOTO_MAX_BYTES:
                return self._json(400, {"error": {"code": "too_large", "message": "max 2 MB"}})
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            sniffed = sniff_photo_type(raw)
            if ctype not in ("image/jpeg", "image/webp") or sniffed != ctype or not raw or len(raw) > PHOTO_MAX_BYTES:
                code = "too_large" if raw and len(raw) > PHOTO_MAX_BYTES else "bad_type"
                msg = "max 2 MB" if code == "too_large" else "image/jpeg or image/webp"
                return self._json(400, {"error": {"code": code, "message": msg}})
            PHOTO_PATH.parent.mkdir(parents=True, exist_ok=True)
            PHOTO_PATH.write_bytes(raw)
            self.send_response(204)
            self.end_headers()
            return
        if path in ("/api/notes", "api/notes"):
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode())
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid_json"})
            return self._json(200, save_notes_doc(payload))
        if path not in ("/api/contacts", "api/contacts"):
            self.send_error(404)
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode())
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid_json"})
        doc = save_contacts(payload)
        doc["propertyId"] = load_options().get("property_id")
        return self._json(200, doc)

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/photo", "api/photo"):
            self.send_error(404)
            return
        if not PHOTO_PATH.is_file():
            self.send_error(404)
            return
        PHOTO_PATH.unlink()
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path in ("/api/photo", "api/photo"):
            if not restore_default_photo():
                return self._json(404, {
                    "error": {"code": "no_default", "message": "No bundled default photograph"}
                })
            self.send_response(204)
            self.end_headers()
            return
        if path in ("/api/notify", "api/notify"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            key = house_event_key()
            if key is None:
                return self._json(400, {
                    "error": {"code": "missing_house_event_key", "message": "Push needs the house event key"}
                })
            try:
                payload = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid_json"})
            title = str((payload or {}).get("title") or "").strip()
            if not title:
                return self._json(400, {"error": {"code": "bad_title", "message": "title must be 1–120 characters"}})
            if len(title) > NOTIFY_TITLE_MAX:
                title = title[:NOTIFY_TITLE_MAX]
            pid = load_options().get("property_id")
            if not pid:
                return self._json(404, {"error": {"code": "not_configured"}})
            card_id = load_notify_card_id()
            status = post_house_event(pid, key, card_id, title)
            if 200 <= int(status) < 300:
                return self._json(200, {"ok": True})
            return self._json(502, {"error": {"code": "backend_error", "message": "Push failed"}})
        if self.path != "/api/mapping":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            doc = json.loads(raw.decode())
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid_json"})
        for row in doc.get("mappings") or []:
            if not row.get("item_id"):
                row["item_id"] = str(uuid.uuid4())
            if not row.get("card_id"):
                return self._json(400, {"error": "card_id_required"})
        for card in doc.get("cards") or []:
            if not card.get("id"):
                card["id"] = str(uuid.uuid4())
            card["kind"] = "custom"
        save_mapping(doc)
        return self._json(200, {"ok": True})


if __name__ == "__main__":
    seed_mapping_if_empty()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
