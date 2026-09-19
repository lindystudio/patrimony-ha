# Patrimony Collection — Card Presentation Schema 1.0 (frozen v0)

Status: frozen for the Patrimony Collection migration.
Changes require a schemaVersion bump.

This is the only live-state contract the iOS app is allowed to understand. Home Assistant maps entities to this JSON. The app renders it. The backend never serves it.

## 1. Transport

| Path | Who | What |
|---|---|---|
| WebSocket event `patrimony/state` | HA integration → app | Full presentation document (not a delta). v0 has no patch protocol. |
| REST `GET /api/patrimony_collection/state` | HA integration → app | Same document. Used on connect and reconnect only. |
| Backend HTTP | — | Does not carry this document. |

On any successful payload, replace the in-memory snapshot for that house. Do not merge.

Auth for both paths is a per-house credential in the iOS Keychain. Never UserDefaults, never source, never the Patrimony backend.

## 2. Document

Root object. Unknown keys: ignore. Missing required keys: reject the document, keep the last good snapshot, surface `attention` on the house card.

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-08-23T13:16:00Z",
  "lastHeard": "2026-08-23T13:16:00Z",
  "property": {
    "id": "3f1c0a2e-7c4b-4d91-9a2e-0b7d1c8e4a10",
    "displayName": "Demo Home",
    "locationLabel": "Example",
    "timezone": "Europe/Athens"
  },
  "cards": [],
  "events": []
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| schemaVersion | integer | yes | Must be `1`. `<1` or missing: reject. `>1`: render known fields, ignore the rest. |
| generatedAt | string (ISO-8601 UTC) | yes | Display clock; not a cache key. |
| lastHeard | string (ISO-8601 UTC) | no (additive) | When this snapshot was produced. Same instant as `generatedAt` at emit. iOS uses it for offline/stale. Unknown keys ignored; do not bump schemaVersion. |
| property | object | yes | See §3. |
| cards | array of Card | yes | Empty is valid (house exists, nothing to show). Sort by `priority` ascending, then `title`. Cap at 24; ignore the rest. |
| events | array of `{ at, message }` | no (additive) | Curated activity from **mapped** item changes only (not HA Logbook). Cap 8, newest first. `at` is ISO-8601 UTC. `message` is a short principal-facing line (card item label / friendly title). Omit or `[]` when empty. Unknown keys ignored; do not bump schemaVersion. |

## 3. Property

| Field | Type | Required | Rules |
|---|---|---|---|
| id | string (UUID) | yes | Stable per house. App identity for Keychain and backend registry. |
| displayName | string | yes | Non-empty after trim. Wallet card title. |
| locationLabel | string | no | Short human place ("Example"). Not an address. |
| timezone | string | yes | IANA name. Used to format `updatedAt` only. |

Forbidden on `property` (if present, ignore, never persist, never log, never map): `latitude`, `longitude`, `lat`, `lon`, `coordinate`, `coordinates`, `region`, `map`, `entity_id`, any HA object id.

## 4. Card

| Field | Type | Required | Rules |
|---|---|---|---|
| id | string (UUID) | yes | Stable for the life of that card on that house. |
| kind | string | yes | Enum §5. Unknown kind → render as `custom`. |
| title | string | yes | Non-empty. |
| priority | integer | yes | Lower first. Default `100` if unparsable. |
| items | array of Item | yes | Empty allowed. Cap at 12; ignore the rest. |

Card severity is derived, never sent: the max of its items (`alert` > `attention` > `ok`). No items → `ok`.

Forbidden on `card`: `entity_id`, `entities`, `device_id`, `area_id`, coordinates, pass-kit / Wallet fields.

## 5. Kind

Closed set in v0:

| kind | Product name | Typical items |
|---|---|---|
| security | Alarm | armed state, zones, last event |
| climate | Climate | indoor temp, setpoint, HVAC mode |
| network | Network | WAN, guest Wi-Fi |
| cellar | Cellar | temperature, humidity |
| arrivals | Arrivals | gate, driveway, expected |
| custom | — | anything not claimed above |

`energy` is not a kind. Send it as `custom` until product claims it. Do not alias `alarm` → the kind is `security`.

## 6. Item

| Field | Type | Required | Rules |
|---|---|---|---|
| id | string (UUID) | yes | Stable. |
| label | string | yes | Non-empty. |
| value | string \| number \| boolean \| null | yes | `null` means unknown; treat severity as `attention` if omitted. |
| valueType | string | yes | `enum` \| `number` \| `bool` \| `text`. |
| unit | string \| null | no | Only for `number` (e.g. `"°C"`). Ignore otherwise. |
| severity | string | yes | `ok` \| `attention` \| `alert`. Unknown → `attention` (fail visible). |
| updatedAt | string (ISO-8601 UTC) | yes | Per item. |

valueType vs value:

- `enum` / `text`: string (or null).
- `number`: JSON number (or null).
- `bool`: JSON boolean (or null).
- Mismatch: ignore the item.

Forbidden on `item`: `entity_id`, `state`, `attributes`, `device_class`, coordinates.

The app never writes state back in v0. This is a monitor, not a control surface.

## 7. Ignore rules (normative)

The renderer MUST:

1. Ignore unknown keys at every level.
2. Drop any field named `entity_id` or ending in `_entity_id` before persistence or logs.
3. Drop coordinates and Wallet/pass-kit keys (`passTypeIdentifier`, `serialNumber`, `pkpass`, etc.).
4. Drop extra cards/items past the caps.
5. Keep the last good snapshot if the new document fails required-field checks.
6. Never infer a card from raw HA entities. No entity in, no card out.

## 8. Severity

HA decides severity. The app does not re-derive it from values (a cellar at 18°C is not an alert unless HA says so).

House-level badge = max severity across cards.

## 9. Fixtures vs live

Until the HA integration exists:

- Bundled fixtures conform to this schema.
- Guest Wi-Fi may keep a live HA read, but the client must project it into this schema (one `network` card item). Raw HA states must not leak into SwiftUI.
- Simulated status objects in Patrimony Collection are replaced by these fixtures, not kept in parallel.

## 10. Out of v0

- Deltas / patches
- Coordinates or maps
- Apple Wallet pass export
- Partner portal
- Backend as live-state proxy
- Control / writes to HA
- History and “intelligent alerts”
- `energy` as a first-class kind

## 11. Minimal fixture

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-08-23T13:16:00Z",
  "lastHeard": "2026-08-23T13:16:00Z",
  "property": {
    "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    "displayName": "Demo Home",
    "locationLabel": "Example",
    "timezone": "Europe/Athens"
  },
  "cards": [
    {
      "id": "11111111-2222-4333-8444-555555555555",
      "kind": "network",
      "title": "Network",
      "priority": 10,
      "items": [
        {
          "id": "66666666-7777-4888-8999-aaaaaaaaaaaa",
          "label": "Guest Wi-Fi",
          "value": true,
          "valueType": "bool",
          "unit": null,
          "severity": "ok",
          "updatedAt": "2026-08-23T13:16:00Z"
        }
      ]
    }
  ]
}
```
