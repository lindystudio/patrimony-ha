# Patrimony Collection — Home Assistant custom component

Read-only custom integration. It maps house entities to a `PresentationDocument` (`card-schema-v0.md`) and emits that document on the existing Home Assistant HTTP and WebSocket surfaces. The Patrimony backend is not on this path.

v0 does **not** write entities (`hass.services` is unused).

## Add-on (HA OS)

On supervised Home Assistant, install the **Patrimony Collection** add-on so it appears under Settings → Add-ons. Open it to filter/select entities. Mapping is written to `/config/patrimony_collection/mapping.json`. This component still serves the snapshot.

## Install

**HACS (preferred):** add `https://github.com/lindystudio/patrimony-ha` as a custom repository (type **Integration**), download, restart Home Assistant.

**Manual:** copy `custom_components/patrimony_collection` into `<config>/custom_components/patrimony_collection`, restart Home Assistant.

Then: **Settings → Devices & services → Add integration → Patrimony Collection**.

Enter:

- `display_name` — wallet title (e.g. Demo Home)
- `location_label` — optional short place (e.g. Example), not an address
- `timezone` — IANA (e.g. `UTC`)
- For a **new house**, we mint `property_id` on submit. Copy it from the confirmation screen (and the integration title) for the backend registry and the iOS Keychain account. Check **I already have a property_id (advanced)** only when reconnecting an existing registry house.

One instance per HA. A second add is aborted as already configured.

Do not enter latitude/longitude. Do not enter an iOS or HA token in this flow.

## iOS credential (never the backend)

Create a Home Assistant long-lived access token (**Profile → Long-lived access tokens**) for a user that can read the mapped entities.

Store it on the house and in the principal’s iOS Keychain:

- service: `collection.patrimony.house`
- account: the same `property.id` UUID (the ID this integration created, or the existing registry UUID you pasted)

**Never** paste that token into the Patrimony backend, UserDefaults, source control, or this integration’s options. Backend HTTP must refuse HA-shaped field names. If Keychain is empty, the app fails closed.

The app calls:

- REST `GET /api/patrimony_collection/state` — connect / reconnect only
- WebSocket `subscribe_events` with `event_type: patrimony/state` — full snapshot, replace in memory
- Optional WS command `patrimony/get_state` — same document as `result`

Auth is Home Assistant’s own Bearer / WS `auth` handshake.

House health (additive, schemaVersion 1): root `lastHeard` is the snapshot assembly time (same instant as `generatedAt`) — iOS binds Offline / Stale to this field. `property.lastHeard` mirrors it. `card.lastUpdated` is the newest `item.updatedAt` on that card. A failed fetch is Offline; a cached `lastHeard` older than the app’s stale threshold is Stale. Do not render either as a quiet healthy card.

Activity (additive, schemaVersion 1): root `events` is a mapped-only ring buffer — at most 8 `{ at, message }` rows, newest first. Appended when a mapped item’s presented value/label would change on the pass (`async_track_state_change_event`). REST `/api/patrimony_collection/state` and WS `patrimony/state` include the current buffer on every full snapshot. Not a Logbook dump. Messages are quiet principal-facing copy (card item label); never `entity_id` / IPs / hostnames / `hek_` / tokens. Empty is `[]`.

## Mapping panel

After reload, the HA sidebar has **Patrimony**. That is the mapping UI: live pass preview (first six items, then titled cards), search entities, edit in place, save. Config flow still works as a fallback.

Before **Soon → Invitation → Show a pairing code**, turn on Home Assistant Cloud or set **Settings → System → Network** External URL to the house public `https` host. Pairing errors show the resolved URL (or `(empty)`) rather than a generic line.

## Mapping (Configure)


**Settings → Devices & services → Patrimony Collection → Configure**.

1. **Add card** — kind from the frozen set: `security`, `climate`, `network`, `cellar`, `arrivals`, `custom`. `energy` is not a kind (map those sensors as `custom`). Title + priority (lower first).
2. **Add item** — HA entity, existing card, label, value type, optional attribute (e.g. climate `current_temperature`), severity mode.
3. **Remove item** — by item UUID (UI shows label + entity for the integrator only).

Card and item UUIDs are minted once and stay stable. They are not entity ids. The house event key is not pasted here — the phone POSTs it to `/api/patrimony_collection/event_key`.

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


## House chat

Members of one house. Stored on this Home Assistant only. Not on the presentation document and not in a Patrimony cloud store.

Same HA Bearer as `GET /api/patrimony_collection/state`.

- `GET /api/patrimony_collection/chat` — messages for an authorized caller.
- `POST /api/patrimony_collection/chat` — `{ "text": "<plain text>", "retention": "keep" | "1h" | "1d" | "7d" }`. `retention` is optional and defaults to `keep`.
- `DELETE /api/patrimony_collection/chat/{message_id}` — the sender may delete their own message. A Home Assistant panel user may delete any message in the house. Delete removes the row. It is not a tombstone.

Text is encrypted at rest with a key that stays on the HA host. That key is not in git, not in the presentation document, and not in push. The phone link is already TLS. This is not end-to-end: the house holds the plaintext so the panel can show it.

A new message reuses the existing events notify path. The title is exactly `New chat message in {display_name}`, or `New chat message` when the stored display name is empty. The events body stays `{ cardId, severity, title }` and does not include the message text, the sender, or a preview.

Sender label: from an iOS client, the stored device name, otherwise the registered client name. From the panel, the Home Assistant user making the request.

## Privacy

On the wire: schema fields plus additive `property.lastHeard`, `card.lastUpdated`, and root `events` (schemaVersion stays 1). Never `entity_id`, coordinates, street/city/country, tokens, or raw attributes. Location is `locationLabel` free text only.

Logs: card/item UUIDs, kind, title, severity. No entity ids at info+.

## Optional push ingest

The phone POSTs `{ "houseEventKey": "hek_…" }` to `/api/patrimony_collection/event_key` (HA Bearer). Success is 200 `{ "configured": true }`. Invalid is 400 `invalid_house_event_key` and does not persist (GET notify stays `{ "configured": false }`). The key is stored in config entry options only. Never logged, never on `patrimony/state`. If a usable `hek_…` is stored, notify POSTs `{ cardId, severity, title }` to the backend events path with `X-House-Event-Key`. That key is not an HA token. HA never mints `hek_` and never calls claim.

Automatic pushes use that same events POST and only on a transition into a FIRE class: alarm `triggered`; smoke, gas, leak, or safety `on`; a mapping-driven lock alert; or a mapped item reaching severity `alert`. Those use severity `alert`. Manual Soon Notify stays severity `attention`. Routine lights, climate, weather, and Wi-Fi, ok/attention churn, and entity `unavailable` do not call notify. The same card and FIRE class waits 20 minutes before a repeat, unless the class changes. iOS offline stays `lastHeard` and is not a push. No house event key means no push.
