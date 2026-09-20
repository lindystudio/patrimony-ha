# Patrimony Collection add-on

This is the Supervisor add-on so Patrimony appears under **Settings → Add-ons** (and the Add-on store once this repo is added).

Use it to **filter and select** which house entities the iOS app may see. The principal does not get raw `entity_id`s. The add-on never stores Home Assistant tokens (it uses the Supervisor API token at runtime).

The live snapshot is still emitted by the **custom component** on this HA (`GET /api/patrimony_collection/state` and WS `patrimony/state`). The add-on writes mapping to `/config/patrimony_collection/mapping.json`. The component reads that file.

## Install (HA OS / Supervised)

1. **Add-on store → ⋮ → Repositories** → add `https://github.com/lindystudio/patrimony-ha` (or copy `addons/patrimony_collection` into `/addons` for a local add-on).
2. Install **Patrimony Collection**. Start it. Open the UI (Ingress).
3. Confirm house fields (display name, location, timezone; property id is minted by the custom component on first install, or pasted when reconnecting). Search entities, pick a card kind, save.
4. Keep the custom component installed so the app can fetch the document on this HA.

Home Assistant Container / Core has no add-on store. Those houses use the custom component options flow only.

## What you can expose

Kinds: security, climate, network, cellar, arrivals, custom. Energy is not a kind (map a sensor as custom if needed). Weather: custom card titled Weather, source is a house weather integration.

v0 is read-only. No writes back to entities.
