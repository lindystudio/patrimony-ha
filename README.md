# Patrimony Collection (Home Assistant)

Custom integration that maps house Home Assistant entities to a `PresentationDocument` and serves it over Home Assistant’s own HTTP and WebSocket APIs for the Patrimony iOS app.

This integration is **read-only** toward HA entities (v0 does not call services). The Patrimony cloud backend is not on the live-state path.

## HACS install (recommended)

Add [`lindystudio/patrimony-ha`](https://github.com/lindystudio/patrimony-ha) as a HACS custom repository (**Integration**).

1. Install [HACS](https://hacs.xyz/) if you do not already have it.
2. HACS → **⋯** (top right) → **Custom repositories**.
3. Repository URL: `https://github.com/lindystudio/patrimony-ha`
4. Category: **Integration**.
5. Add → find **Patrimony Collection** → **Download**.
6. **Restart Home Assistant**.
7. **Settings → Devices & services → Add integration → Patrimony Collection**.

### My Home Assistant link

Generate a one-click HACS link at  
https://my.home-assistant.io/create-link/?redirect=hacs_repository  
(owner/repo: `lindystudio/patrimony-ha`, category: `integration`).

## Manual install

1. Copy `custom_components/patrimony_collection` into `<config>/custom_components/patrimony_collection`.
2. Restart Home Assistant.
3. Add the **Patrimony Collection** integration.

## Setup

When adding the integration, enter:

| Field | Notes |
| --- | --- |
| `property_id` | UUID that must equal the backend `properties.id` and the iOS Keychain account |
| `display_name` | Wallet title (e.g. Demo Home) |
| `location_label` | Optional short place label — not a street address |
| `timezone` | IANA timezone (e.g. `UTC`) |

One config entry per Home Assistant instance. Do **not** enter latitude/longitude or any iOS / HA token in this flow.

## After install

- Sidebar **Patrimony** panel: map entities to cards/items and preview the pass.
- Or **Settings → Devices & services → Patrimony Collection → Configure**.
- Cards are labels: every card emits `kind: "custom"`. Title is the product name.

## House health

Every PresentationDocument includes additive `lastHeard` at the **root** (ISO-8601 UTC, same instant as `generatedAt`) under `schemaVersion` 1. iOS uses the age of the last `lastHeard` to show Offline / Stale when the house is unreachable — not a quiet healthy card. `property.lastHeard` mirrors the same stamp. `card.lastUpdated` is per-card “as of.”

Root `events` is an optional curated Activity feed (schemaVersion stays 1): up to 8 `{ at, message }` rows, newest first, from **mapped** entity changes whose presented value/label would change on the pass. Not the HA Logbook. Empty is `[]`. Messages use the card item label; they never include `entity_id`, area ids, IPs, hostnames, or tokens.

## iOS credential (never the backend)

Create a Home Assistant **long-lived access token** for a user that can read the mapped entities. Store it in the principal’s iOS Keychain:

- service: `collection.patrimony.house`
- account: the same `property.id` UUID

**Never** paste that token into the Patrimony backend, UserDefaults, source control, or this integration’s options.

The app uses:

- REST `GET /api/patrimony_collection/state` — connect / reconnect
- WebSocket `subscribe_events` with `event_type: patrimony/state` — full snapshot
- Optional WS command `patrimony/get_state`

## Privacy

On the wire: schema fields plus additive `lastHeard` / `card.lastUpdated` / root `events`. Never `entity_id`, coordinates, street/city/country, tokens, or raw attributes. Location is `locationLabel` free text only (e.g. Example).

Demo fixtures use a placeholder property (`Demo Home`, Example, UTC).

## Add-on (HA OS, optional)

On Supervised / HA OS you may also install the **Patrimony Collection** add-on for filter/select mapping UI. Mapping is written under `/config/patrimony_collection/`. The custom component still emits the live snapshot.

## Support

- Issues: [lindystudio/patrimony-ha](https://github.com/lindystudio/patrimony-ha/issues)
- Documentation in-repo: this README and `custom_components/patrimony_collection/README.md`.
