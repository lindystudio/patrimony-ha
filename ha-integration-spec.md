# Patrimony Collection — HA integration spec (v0)

Normative live-state shape is `/workspace/patrimony/card-schema-v0.md`. This spec is how the integration stores mapping and exposes that document. If this file and the schema disagree, the schema wins.

## 1. Events, paths, auth

| Channel | Name | Body | When |
|---|---|---|---|
| HA WebSocket event | `event_type`: `patrimony/state` | HA envelope; **`event.data` = PresentationDocument** | After each full snapshot rebuild (see design) |
| HA WebSocket command | `type`: `patrimony/get_state` | result = PresentationDocument | Optional reconnect helper; same document as REST |
| HTTP | `GET /api/patrimony_collection/state` | PresentationDocument | Connect / reconnect only |
| HTTP | any other method on that path | error | 405 |

Auth: Home Assistant’s API auth (`Authorization: Bearer <long-lived token>` or HA WebSocket `auth` message). Same token the iOS Keychain holds for `property.id`. No integration-specific API key on these paths.

The Patrimony backend is **not** a caller of these paths.

## 2. PresentationDocument (wire — exact schema fields)

Root:

| Field | Type | Required |
|---|---|---|
| `schemaVersion` | integer | yes — always `1` in v0 |
| `generatedAt` | string ISO-8601 UTC (`…Z`) | yes |
| `property` | object | yes |
| `cards` | array | yes (empty allowed) |

`property`:

| Field | Type | Required |
|---|---|---|
| `id` | string UUID | yes |
| `displayName` | string | yes |
| `location` | string | no |
| `locationLabel` | string | no (deprecated alias of `location`) |
| `timezone` | string IANA | yes |

Do not emit: `latitude`, `longitude`, `lat`, `lon`, `coordinate`, `coordinates`, `region`, `map`, `street`, `city`, `country`, `address`, `postal_code`, `entity_id`, `haHostname`.

Card:

| Field | Type | Required |
|---|---|---|
| `id` | string UUID | yes |
| `kind` | `security` \| `climate` \| `network` \| `cellar` \| `arrivals` \| `custom` | yes |
| `title` | string | yes |
| `priority` | integer | yes |
| `items` | array | yes |

Do not emit card-level `severity` (derived in the app). Do not emit `entity_id`, `entities`, `device_id`, `area_id`.

Item:

| Field | Type | Required |
|---|---|---|
| `id` | string UUID | yes |
| `label` | string | yes |
| `value` | string \| number \| boolean \| null | yes |
| `valueType` | `enum` \| `number` \| `bool` \| `text` | yes |
| `unit` | string \| null | no — only meaningful for `number` |
| `severity` | `ok` \| `attention` \| `alert` | yes |
| `updatedAt` | string ISO-8601 UTC | yes |

Caps: 24 cards (sort `priority` ascending, then `title`); 12 items per card (stable mapping order, then label). Extra dropped here so the phone does not have to.

## 3. REST and WS payloads

Success REST: `200`, `Content-Type: application/json`, body = document (see fixture).

HA WS event (illustrative envelope; document is `event.data`):

```json
{
  "id": 2,
  "type": "event",
  "event": {
    "event_type": "patrimony/state",
    "data": { "schemaVersion": 1, "generatedAt": "…", "property": {}, "cards": [] }
  }
}
```

WS command:

Request: `{ "id": 1, "type": "patrimony/get_state" }`  
Success: `{ "id": 1, "type": "result", "success": true, "result": { /* PresentationDocument */ } }`

## 4. Error responses

REST (HA-style JSON, no entity ids, no tokens):

| HTTP | When |
|---|---|
| 401 | missing/invalid HA token |
| 404 | integration not set up |
| 405 | method ≠ GET |
| 500 | snapshot assembly failed (message is generic: `"patrimony_snapshot_failed"`) |

Body for 404/500:

```json
{ "error": { "code": "not_configured", "message": "Patrimony Collection is not configured" } }
```

Codes: `not_configured`, `unauthorized` (if we return a body; HA may send its own 401), `snapshot_failed`, `method_not_allowed`.

WS command failure: `{ "success": false, "error": { "code": "not_configured", "message": "…" } }`.

Invalid documents are not sent. If mapping is empty, send a **valid** document with `"cards": []`.

## 5. Config entries and options (HA storage)

Domain: `patrimony_collection`. Config flow version: `2` (v2 stores `location`; leftover `location_label` / street / city / country are joined). Single instance.

**Config entry `data`** (created by config flow, rarely changed):

```json
{
  "property_id": "00000000-0000-4000-8000-000000000001",
  "display_name": "Demo Home",
  "location": "Example",
  "timezone": "UTC"
}
```

**Config entry `options`:**

```json
{
  "cards": [ { "id": "<uuid>", "kind": "network", "title": "Network", "priority": 10 } ],
  "mappings": [ { /* Mapping record */ } ],
  "house_event_key": null
}
```

`house_event_key`: optional `hek_…`. Never copied to wire. Empty/null = push ingest disabled.

## 6. Mapping record (internal only)

`entity_id` lives **only** here. Never on REST/WS.

```json
{
  "item_id": "66666666-7777-4888-8999-aaaaaaaaaaaa",
  "card_id": "11111111-2222-4333-8444-555555555555",
  "entity_id": "binary_sensor.demo_guest_wifi",
  "state_attribute": null,
  "label": "Guest Wi-Fi",
  "value_type": "bool",
  "unit": null,
  "severity_mode": "ok_when_on",
  "ok_states": [],
  "attention_states": [],
  "alert_states": [],
  "ok_min": null,
  "ok_max": null,
  "attention_min": null,
  "attention_max": null,
  "fixed_severity": null
}
```

| Field | Notes |
|---|---|
| `item_id` | Wire `item.id`. Minted once. |
| `card_id` | Wire `card.id`. Must match an options `cards[]` row. |
| `entity_id` | HA entity. **Storage only.** |
| `state_attribute` | If set, read that attribute instead of `state` (e.g. `current_temperature`). |
| `label` | Wire label; if empty, friendly_name / object_id. |
| `value_type` | `bool` \| `number` \| `enum` \| `text` \| `auto` |
| `severity_mode` | see design |
| `*_states` | HA state strings for `enum_map` (internal) |
| `ok_min` / `ok_max` / `attention_*` | numbers for `number_range` |
| `fixed_severity` | `ok` \| `attention` \| `alert` when mode is `fixed` |

Orphan mappings (card_id missing) are skipped. Broken entity_id → item with `value: null`, `severity: attention`.

## 7. Integrator UX steps

1. Install custom component, restart HA.
2. Add **Patrimony Collection**. Enter `property_id` (same UUID the operator used in `create-property`), display name, optional Location (free text), IANA timezone.
3. Create a long-lived token in HA for the iOS user (or a dedicated `patrimony` user with read access). Put that token in the principal’s iOS Keychain under `collection.patrimony.house` / `property.id`. Do not paste it into Patrimony backend.
4. **Configure** → add the Guest Wi-Fi entity to a `network` card, label `Guest Wi-Fi`, bool, `ok_when_on`. Add climate/security the same way. For outdoor weather: enable Met.no (or another HA weather integration) on the house, then map `weather.*` twice (attribute `temperature` → Outdoor number; state → Condition enum) onto a `custom` card titled Weather, or onto the climate card.
5. Confirm `GET /api/patrimony_collection/state` as that HA user returns schema JSON with no `entity_id`.
6. Optional: operator `mint-event-key`; paste `hek_…` into options for APNs ingest only.

Principal never performs steps 1–4.

## 8. Test fixtures

Canonical snapshot: `fixtures/demo-state.json`.

House: Demo Home, timezone `UTC`, `property.id` `00000000-0000-4000-8000-000000000001`. Includes:

- `network` / Guest Wi-Fi `true` bool `ok`
- `climate` / Indoor `21.5` `°C` `ok`
- `security` / Alarm `Armed away` enum `ok`
- `custom` / Weather: Outdoor `18` `°C` `ok`, Condition `Cloudy` enum `ok` (source: house `weather.*`, typically Met.no — not in the fixture)

Internal mapping that would produce it is **not** in the fixture (`fixtures/demo-mapping.json` is the offline mapping companion).


## Contacts (frozen, off the card schema)

House-only REST, same HA Bearer as `/api/patrimony_collection/state`. Not on `patrimony/state` or PresentationDocument.

- `GET` / `PUT` `/api/patrimony_collection/contacts`
- Body: `{ "schemaVersion": 1, "propertyId": "<uuid>", "contacts": [{ "id", "function", "name", "tel", "method": "cellular"|"viber"|"whatsapp" }] }`
- Order is array order. Store `config/patrimony_collection/contacts.json` (never mapping.json).
- Empty list is valid (iOS first Call). PUT replaces the list.

## House tools 0.4 (photo, notes, pair, notify)

Off `patrimony/state` and PresentationDocument. `schemaVersion` stays 1. Same HA Bearer as `GET /api/patrimony_collection/state`. Cards / People / contacts / mapping / °C / featuredRank unchanged.

| Method | Path | Auth | Body / notes |
|---|---|---|---|
| GET | `/api/patrimony_collection/photo` | Bearer | Raw jpeg/webp bytes. 404 if missing. Sniff magic for Content-Type. |
| PUT | `/api/patrimony_collection/photo` | Bearer (not admin-only) | Raw image bytes, `image/jpeg` or `image/webp`, max 2 MB. Store `config/patrimony_collection/face.jpg`. |
| DELETE | `/api/patrimony_collection/photo` | Bearer | 204, or 404 if already gone. |
| GET/PUT | `/api/patrimony_collection/notes` | Bearer | `{ "schemaVersion": 1, "propertyId": "<uuid>", "text": "", "updatedAt": "<ISO Z>" \| null }`. Cap 8k (truncate on save). Missing GET: empty text, `updatedAt` null. Store `config/patrimony_collection/notes.json`. |
| POST | `/api/patrimony_collection/pair` | Bearer + admin | `{ "pairing": "patrimony://pair?url=<https>&token=<llat>" }` once. Mints HA long-lived token named `Patrimony iOS`. Never persist or log the token. 403 if not admin; 501 if HA auth APIs missing. |
| GET | `/api/patrimony_collection/notify` | Bearer | `{ "configured": true\|false }` — true iff a usable `hek_…` house event key is in config entry options. |
| POST | `/api/patrimony_collection/notify` | Bearer | `{ "title": "<1–120 chars>" }`. POSTs `BACKEND_BASE` (`https://api.patrimonycollection.com`) `/v1/properties/{propertyId}/events` with `X-House-Event-Key: hek_…` and `{ "cardId", "severity": "attention", "title" }`. Stable `cardId` in `config/patrimony_collection/notify.json`. 400 `missing_house_event_key` if the key is missing, empty, not `hek_`, or looks like a JWT. Never send an HA token as the key. |

Soon tab: Photograph, Notepad, Pair (`Show a pairing code`), Notify. No `window.prompt` / `confirm` / `alert`. Pairing QR is drawn in-panel; never a third-party QR service.
