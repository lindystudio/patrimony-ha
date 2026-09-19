# Patrimony Collection — Home Assistant integration (design)

Status: design for v0 sketch. The live-state contract lives in `card-schema-v0.md`. This file does not change that contract.

The iOS app is a thin renderer. This integration is the only place raw Home Assistant entities exist. It **emits** `PresentationDocument`. It does not invent kinds, fields, or product claims.

## Packaging: Supervisor add-on + custom component

Customers on HA OS / Supervised install **Patrimony Collection** from the Add-on store. That is the filter/select UI (Ingress) for what the app may see. It writes `/config/patrimony_collection/mapping.json` (`entity_id` stays on the house).

The **custom component** still emits `PresentationDocument` on this HA (REST + WS). The iOS app does not talk to the add-on.

HA Container / Core (no Supervisor): use the custom component options flow only.

The add-on is not a second live-state server and does not store HA tokens.

`mapping.json` is merged as a **union** with config-entry options. Empty or partial file keys do not replace existing cards/items. On component setup (and add-on start) a missing/empty file is seeded from current options (demo includes Weather on `weather.home`). Seed from `seed_demo.json` when empty.


**Ship a custom component** (`custom_components/patrimony_collection`) that runs **inside** the house Home Assistant instance.

| Need | Custom integration | Add-on (sidecar container) |
|---|---|---|
| Entity registry, states, `state_changed` | Native (`hass.states`, bus) | Must scrape HA APIs as a client |
| REST on the same origin the iOS app already talks to | `hass.http` view under `/api/patrimony_collection/…` | Extra port / reverse proxy; second auth surface |
| WebSocket on the existing HA socket | Bus event + `websocket_api` | Separate socket; app would grow a second client |
| Integrator UI | Config flow + options flow in HA frontend | Separate UI to build and host |
| Tokens | Reuses HA’s own auth (long-lived token stays on house + Keychain) | Tempting to mint a second secret |

An add-on alone is the wrong live-state path (sidecar REST/WS). Mapping UI may live in the add-on; emit stays in the custom component.

One HA instance = one house = one config entry. Principals with homes worldwide run one integration per house instance. The Patrimony backend never sees this process.

## Architecture

```
[HA entity registry / states]
        │  integrator mapping (add-on UI → mapping.json and/or options)
        ▼
[patrimony_collection]
  mapping.py  → PresentationDocument (severity decided here; entity_id stripped)
        │
        ├─ REST  GET /api/patrimony_collection/state     (connect / reconnect)
        └─ WS    event_type patrimony/state              (full snapshot, not a delta)
                │
                ▼
        [iOS Keychain credential → this house HA]
                │
                ▼
        [iOS renderer]     Patrimony backend is not on this path
```

Optional, not on the card document: if the integrator pastes a **house event key** (`hek_…`) minted by the backend operator CLI, the integration may POST `{ cardId, severity, title }` to `/v1/properties/{id}/events`. That key is not an HA token. The HA long-lived token is never attached to that request.

## Config flow (integrator, not principal)

The principal does not map entities. The integrator (or household operator at the HA panel) does.

### Install

HA OS: Add-on store → Patrimony Collection (filter/select). Also keep the custom component. Core-only: add `https://github.com/lindystudio/patrimony-ha` as a HACS custom repository (Integration), or copy the custom component, then use its options flow.

### Step `user` (create entry)

| Field | Storage | Wire |
|---|---|---|
| `property_id` | UUID; **must equal** backend registry `properties.id` and iOS Keychain account | `property.id` |
| `display_name` | non-empty | `property.displayName` |
| `location_label` | optional short place (“Example”), not an address | `property.locationLabel` |
| `timezone` | IANA | `property.timezone` |

Abort `already_configured` if an entry exists (v0: one house per HA). Do not ask for latitude/longitude. Do not ask for the iOS token (HA already authenticates its own API).

### Options flow (mapping)

After setup: **Configure**.

1. **Add item** — pick an HA entity, a card `kind` from the frozen set, a card (existing card UUID or “new card”), `label`, `priority` (card-level), optional `value_type` override, optional severity mode.
2. **Remove item** — by opaque item UUID (UI shows label + entity for the integrator only).
3. **House event key** (optional) — `hek_…` stored in entry options on the house. Never copied into `PresentationDocument`, never logged, never sent to iOS.

Card UUIDs and item UUIDs are minted once in HA storage and stay stable for the life of that mapping row. The app uses them as identity; they are not entity ids.

## Mapping UI and kinds

Closed kinds (schema §5): `security`, `climate`, `network`, `cellar`, `arrivals`, `custom`.

- Unknown kind on disk → emit `custom` (same as the renderer).
- `energy` is **not** a kind. If an integrator still wants an energy sensor on a boarding-pass card, map it as `custom`. Do not alias.
- Do not alias `alarm` → kind is `security`.
- Multiple items share a card when they share `card_id`. Typical: one `network` card with Guest Wi-Fi + WAN; one `climate` card with indoor temp + HVAC mode.
- Caps (schema): 24 cards (priority asc, then title; drop the rest), 12 items per card.

Principal never sees this UI. Integrator grants in the Patrimony backend (`/v1/properties/{id}/integrators`) are a **registry of people**, not HA login. HA login is still HA users / trusted network.

## Entity → card/item rules

Internal mapping row (HA storage only) is specified in `ha-integration-spec.md`. On the wire, only schema fields exist.

### Which domains are suggested (not exclusive)

| Kind | Suggested HA domains / device classes |
|---|---|
| `network` | `binary_sensor` (connectivity), UniFi/Omada SSID sensors, `switch` / `input_boolean` for guest WLAN, template sensors |
| `security` | `alarm_control_panel`, `binary_sensor` door/window/motion/safety/smoke/gas, lock (state only) |
| `climate` | `climate` (current temp, hvac_mode — **read**), `sensor` temperature/humidity, optional outdoor from `weather.*` |
| `cellar` | `sensor` temperature / humidity |
| `arrivals` | `binary_sensor` garage/gate, `cover` (open/closed as enum/bool — no `cover.open` service in v0), calendar/text “expected” |
| `custom` | anything else, including energy sensors, or a card titled **Weather** (outdoor temp + condition) |

Unavailable entities still produce an item: `value` is `null`, `severity` is `attention`.

### Labels

1. Integrator `label` if non-empty after trim.
2. Else `friendly_name` from attributes (never the entity_id string).
3. Else humanized `object_id` (`guest_wifi` → “Guest wifi”).

Never put `entity_id`, device_id, or area_id in `label`.

### Values and `valueType`

| `valueType` | Value JSON | Source |
|---|---|---|
| `bool` | `true` / `false` / `null` | `on`/`off`, `true`/`false`, `unlocked`/`locked` (lock: locked→true only if integrator chose that polarity — default lock `locked` = ok bool true), connectivity `on` = connected |
| `number` | JSON number / `null` | `float(state)` or climate `current_temperature` / `temperature` as configured `attribute` (internal) |
| `enum` | string / `null` | Humanized state (`armed_away` → `Armed away`); HVAC mode as title case |
| `text` | string / `null` | Raw state string after redaction (reject if it looks like an entity id or URL) |

Mismatch (schema): do not emit the item.

`unit`: only when `valueType` is `number` (e.g. `"°C"`, `"%"`). Otherwise `null`. Prefer HA `unit_of_measurement` unless the integrator set one.

`updatedAt`: last HA `last_changed`/`last_updated` for that entity, ISO-8601 UTC with `Z`. Document `generatedAt` is snapshot time (`utcnow`).

### Outdoor weather (house HA, not the phone)

Source is the house Home Assistant weather stack — typically **Met.no** (`weather.home` / `weather.forecast_home`) or another HA weather integration. The integrator maps that entity (or a temperature sensor + a condition sensor). The iOS app never calls a weather API; it only renders items on the snapshot.

Two mapping rows can share one `weather.*` entity:

| Item | Internal `state_attribute` | `valueType` | Notes |
|---|---|---|---|
| Outdoor | `temperature` | `number` | Unit `°C` (or HA `temperature_unit`). Do not copy lat/lon from weather attributes. |
| Condition | (entity `state`) | `enum` | Humanized HA condition (`sunny` → `Sunny`). |

Put the items on the existing `climate` card **or** a `custom` card titled **Weather**. The demo fixture uses the Weather custom card so indoor HVAC and outdoor alerts do not share one card severity.

Severity (auto, decided here): `exceptional` / `lightning` / `lightning-rainy` / `hail` → `alert`; `pouring` / `snowy` / `snowy-rainy` / `windy` / `windy-variant` → `attention`; else `ok` if a value exists. Numeric outdoor temp without ranges → `ok`.

### Attributes used (internal)

Allowed to *read* in HA: `friendly_name`, `unit_of_measurement`, `temperature_unit`, `device_class`, climate `current_temperature`, `hvac_mode`, `temperature`, weather `state` (condition) and `temperature`. Forbidden to copy onto the wire: `entity_id`, `latitude`, `longitude`, `gps`, `source`, `access_token`, any URL attribute, `entity_picture` remote URLs.

Default attribute: entity `state`. Integrator may set `state_attribute` (e.g. climate current temp) in the mapping row.

## Severity (decided here)

Schema enum: `ok` | `attention` | `alert`. Unknown would be the app’s problem; we only emit those three. **Card severity is not sent** (schema: max of items, `alert` > `attention` > `ok`; no items → `ok`).

`value: null` → `attention` (unknown).

Integrator `severity_mode` on the mapping row:

| Mode | Rule |
|---|---|
| `ok_when_on` | bool true → `ok`, false → `attention` (Guest Wi-Fi default) |
| `ok_when_off` | bool false → `ok`, true → `attention` (problem/leak inverted) |
| `binary_alert_on` | true → `alert`, false → `ok` (smoke, gas, alarm triggered as bool) |
| `enum_map` | lookup tables `ok_states` / `attention_states` / `alert_states` (HA states, internal) |
| `number_range` | inside `[ok_min, ok_max]` → `ok`; inside wider `[attention_min, attention_max]` → `attention`; else `alert`. Missing bounds → `attention` if unparsable |
| `fixed` | integrator constant |
| `auto` (default) | domain heuristics below |

**Auto heuristics** (smallest set):

- Entity `unavailable` / `unknown` / empty → `attention`, value `null`.
- `alarm_control_panel`: `triggered` → `alert`; `pending`/`arming`/`disarming` → `attention`; `armed_*` → `ok`; `disarmed` → `attention` (unarmed house is visible, not silent).
- `binary_sensor` device_class `smoke`/`gas`/`heat`/`safety`/`problem`/`tamper`: `on` → `alert`.
- `binary_sensor` `door`/`window`/`garage_door`: `on` → `attention`.
- `binary_sensor` `connectivity` / network kind: `on` → `ok`, `off` → `attention`.
- `climate` / numeric sensors: `auto` without ranges → `ok` if numeric, else `attention`. Integrator should set ranges for cellar.
- `weather` condition: `exceptional`/`lightning`/`lightning-rainy`/`hail` → `alert`; `pouring`/`snowy`/`snowy-rainy`/`windy`/`windy-variant` → `attention`; else `ok`.
- Everything else with a usable value → `ok`.

The app must not re-derive severity from numbers (schema §8).

## Transport

Frozen:

- WebSocket event `patrimony/state` — **full** `PresentationDocument`, not a delta. On success the app **replaces** the in-memory snapshot.
- REST `GET /api/patrimony_collection/state` — same document. **Connect and reconnect only.**

Implementation:

1. **REST** — `HomeAssistantView` at `/api/patrimony_collection/state`, `requires_auth=True`. Body is the document. No query parameters (none that take entity ids).
2. **WS** — `hass.bus.async_fire("patrimony/state", document)` so a standard HA `subscribe_events` with `event_type: patrimony/state` delivers it. The HA envelope is `{ type: event, event: { event_type, data } }`; **`event.data` is the PresentationDocument** (no extra wrapper keys). Also register websocket command `patrimony/get_state` that returns the same document as `result` for clients that prefer a call over REST on reconnect.

### When snapshots are emitted

Full rebuild + emit (REST always rebuilds on GET):

- HA start / integration `async_setup_entry` (after mapping load).
- Mapping options change (options flow save).
- `state_changed` for an entity_id that appears in the mapping (ignore unmapped chatter).
- Debounce ~300ms so a climate burst is one snapshot.

Never emit patches. Never emit on unrelated entities. Never write to HA entities in v0 (no `hass.services.async_call` for mapped domains).

## Privacy — what is allowed on the wire

**Allowed:** schema fields plus additive unknown-key fields: `schemaVersion`, `generatedAt`, root `lastHeard` (same instant as `generatedAt` at emit), `property.{id,displayName,locationLabel,timezone,localTime,lastHeard}`, `cards[]` with `id`, `kind` (always `custom`), `title`, `priority`, `lastUpdated`, `items[]` with `id,label,value,valueType,unit,severity,updatedAt`, root `events[]` with `{ at, message }` (mapped activity only; cap 8, newest first).

**Strip before send / never persist in the document / never log:**

- `entity_id`, keys ending in `_entity_id`, `device_id`, `area_id`, `unique_id`
- `latitude`, `longitude`, `lat`, `lon`, `coordinate`, `coordinates`, `gps`, `gps_accuracy`, `location`, `region`, `map`, `street`, `city`, `country`, `address`
- HA tokens, `access_token`, `refresh_token`, `Authorization`, long-lived token strings
- Internal URLs (`internal_url`, `external_url`, `base_url`, webhook URLs, camera still URLs)
- Pass-kit / Wallet keys
- Raw `attributes` / `context` blobs

`locationLabel` is a short human place, not geodata. House identity is `property.id` (UUID) + `displayName`. Timezone is IANA for display clocks only.

Logs: item/card UUIDs and kind/title/severity only. No entity ids in log lines at info+.

## Auth

### iOS → this house HA

The app authenticates to **this HA** with a per-house credential in iOS Keychain (`collection.patrimony.house` / account = `property.id`). v0: HA **long-lived access token** (or equivalent HA user token) sent as `Authorization: Bearer` on REST and on the HA WebSocket auth handshake.

- Created in HA (**Profile → Long-lived access tokens**) by the integrator/operator.
- Stored on the house (HA user store) and in Keychain. **Never UserDefaults, never source, never Patrimony backend.**
- Backend may store optional `ha_hostname` (hostname only) as a connection hint. It must not receive the token. HTTP to the backend refuses HA-shaped field names (`ha_token`, `long_lived_token`, …).

If Keychain is empty: app fails closed (attention on the house card). This integration does not embed a fallback secret.

### Integration → backend (optional push ingest)

House event key `hek_…` only. The integration **never** forwards the HA long-lived token, never puts it in JSON toward Patrimony, never writes it into `PresentationDocument`.

### Integration itself

No outbound copy of `hass.data` tokens. Snapshot builder takes `State` objects and mapping rows, returns a dict already redacted.

## Demo fixture

Canonical fixture: `fixtures/demo-state.json`

- property.id `00000000-0000-4000-8000-000000000001`
- displayName Demo Home, locationLabel Example, timezone UTC
- Network: Guest Wi-Fi bool, ok_when_on
- Weather custom card: Outdoor number C, Condition enum (house weather.*, typically Met.no)

Do not store the HA token in this repo or the Patrimony backend. House hostname comes from HA `external_url` only.

## Guest Wi-Fi (network example)

Network item: guest Wi-Fi as a network card item.

### Integrator mapping (example)

1. In HA, expose a boolean the house already has, for example:
   - UniFi/Omada: `switch.guest_wifi` or `binary_sensor.ssid_guest_up`
   - or `input_boolean.demo_guest_wifi` / a template `binary_sensor` over controller data
2. Options flow: kind `network`, new or existing Network card (`priority` 10), label **Guest Wi-Fi**, `value_type` `bool`, `severity_mode` `ok_when_on`.
3. Integration reads `on`/`off` → `value: true|false`, `valueType: "bool"`, `unit: null`.
4. Snapshot item UUID is stable; entity_id never leaves HA.

Demo fixture: `fixtures/demo-state.json`.

### Generalize (still kind `network`)

Add more **items** on the same card (or another `network` card if the integrator wants a split): WAN, LAN, cameras uplink, staff SSID. Same mapping row shape. Do **not** add kinds `wan`, `wifi`, `omada`. Schema already lists “WAN, guest Wi-Fi” as typical `network` items.

## House identity

`property.id` is the stable UUID shared with:

- iOS Keychain account
- Backend `GET /v1/properties` `id`
- This config entry

`displayName` is the wallet title. `locationLabel` is optional (“Example”). No coordinates. Integrator types the UUID to match the operator-created registry row (operator CLI `create-property --id`). v0 does not mint a second id.

## Out of scope (do not implement)

- Writes / control / `hass.services` against mapped entities
- Deltas / patches
- `energy` as a first-class kind
- Apple Wallet / pkpass
- Backend storing HA tokens or serving this document
- Maps, history, “intelligent alerts”, partner portal
