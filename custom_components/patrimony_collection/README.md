# Patrimony Collection — Home Assistant custom component

Read-only custom integration. It maps house entities to a `PresentationDocument` (`card-schema-v0.md`) and emits that document on the existing Home Assistant HTTP and WebSocket surfaces. The Patrimony backend is not on this path.

v0 does **not** write entities (`hass.services` is unused).

## Add-on (HA OS)

On supervised Home Assistant, install the **Patrimony Collection** add-on so it appears under Settings → Add-ons. Open it to filter/select entities. Mapping is written to `/config/patrimony_collection/mapping.json`. This component still serves the snapshot.

## Install (integrator)


1. Copy `custom_components/patrimony_collection` into the house Home Assistant config directory (`<config>/custom_components/patrimony_collection`), or install via HACS if that repository is published.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → Patrimony Collection**.
4. Enter:
   - `property_id` — UUID that **must equal** backend `properties.id` and the iOS Keychain account
   - `display_name` — wallet title (e.g. Demo Home)
   - `location` — optional free-text place (e.g. Example). The principal chooses what to put here.
   - `timezone` — IANA (e.g. `UTC`)
5. One instance per HA. A second add is aborted as already configured.

Do not enter latitude/longitude. Do not enter an iOS or HA token in this flow.

## iOS credential (never the backend)

Create a Home Assistant long-lived access token (**Profile → Long-lived access tokens**) for a user that can read the mapped entities.

Store it on the house and in the principal’s iOS Keychain:

- service: `collection.patrimony.house`
- account: the same `property.id` UUID

**Never** paste that token into the Patrimony backend, UserDefaults, source control, or this integration’s options. Backend HTTP must refuse HA-shaped field names. If Keychain is empty, the app fails closed.

The app calls:

- REST `GET /api/patrimony_collection/state` — connect / reconnect only
- WebSocket `subscribe_events` with `event_type: patrimony/state` — full snapshot, replace in memory
- Optional WS command `patrimony/get_state` — same document as `result`

Auth is Home Assistant’s own Bearer / WS `auth` handshake.

## Mapping panel

After reload, the HA sidebar has **Patrimony**. That is the mapping UI: live pass preview (first six items, then titled cards), search entities, edit in place, save. Config flow still works as a fallback.

## Mapping (Configure)


**Settings → Devices & services → Patrimony Collection → Configure**.

1. **Add card** — kind from the frozen set: `security`, `climate`, `network`, `cellar`, `arrivals`, `custom`. `energy` is not a kind (map those sensors as `custom`). Title + priority (lower first).
2. **Add item** — HA entity, existing card, label, value type, optional attribute (e.g. climate `current_temperature`), severity mode.
3. **Remove item** — by item UUID (UI shows label + entity for the integrator only).
4. **House event key** (optional) — `hek_…` minted by the operator CLI for APNs ingest only. Never an HA token. Never copied into the document.

Card and item UUIDs are minted once and stay stable. They are not entity ids.

Caps: 24 cards (priority, then title); 12 items per card (mapping order, then label).

## Guest Wi-Fi (network example)

Guest Wi-Fi is a **network** card item, not a special kind. Do not store tokens.

1. Expose a boolean the house already has, for example:
   - UniFi/Omada: `switch.guest_wifi` or `binary_sensor.ssid_guest_up`
   - or `input_boolean.demo_guest_wifi` / a template `binary_sensor`
2. Add a `network` card (priority `10`, title `Network`) if you do not have one.
3. Add the entity: label **Guest Wi-Fi**, `value_type` `bool`, `severity_mode` `ok_when_on`.
4. Confirm `GET /api/patrimony_collection/state` returns schema JSON: `value` true/false, `valueType` `"bool"`, `unit` null, `severity` `ok` when on. **No `entity_id`.**

Canonical fixture: `fixtures/demo-state.json`.

Add WAN, staff SSID, etc. as more **items** on the same `network` card. Do not invent kinds `wan`, `wifi`, `omada`.

## Outdoor weather (house HA)

Do **not** call a weather API from the phone or from this component as a client of Met.no. Enable **Met.no** (or OpenWeatherMap, etc.) as a Home Assistant weather integration on the house. Then map:

1. Add a `custom` card titled **Weather** (or put items on `climate`).
2. Same `weather.*` entity, two items:
   - Outdoor — attribute `temperature`, `number`, `°C`
   - Condition — entity state, `enum`, severity `auto` (storm/extreme → `alert`/`attention`, else `ok`)

Demo fixture: Weather card with Outdoor 18°C and Condition Cloudy. Coordinates on the weather entity stay in HA.

Energy sensors, if shown, stay kind `custom`. Energy is not a kind.

## Privacy

On the wire: schema fields only. Never `entity_id`, coordinates, tokens, or raw attributes.

Logs: card/item UUIDs, kind, title, severity. No entity ids at info+.

## Optional push ingest

If an operator-minted `hek_…` is stored in options, the integration *may* later POST `{ cardId, severity, title }` to the backend events path. That key is not an HA token. The HA long-lived token is never attached to that request. v0 sketch does not implement the POST.
