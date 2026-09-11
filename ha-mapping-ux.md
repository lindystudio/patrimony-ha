# Mapping UI (compose the pass)

Status: 0.3.0 panel layout shipped. Contract still `card-schema-v0.md`. Integrator surface only — the principal never uses this.

The job is to **compose the boarding pass**, not to configure Home Assistant.

## Layout 0.3 (iframe)

The HA sidebar iframe is a single column. Do **not** use a three-column grid. That is why the panel feels like a settings form: the pass and the card list fall off-screen.

**Top:** compact boarding-pass preview, full width. Glance grid follows featuredRank, else legacy first 6. Open-house blocks under it, compact. This is always visible without horizontal scroll.

**Below:** one composer. Each card is a section (title, count, chevrons to reorder cards, rename, delete). Open section shows its lines:

`[☑ On the pass]  Label          23.7°C   ↑ ↓`

- Checkbox is the pass-face control. Max 6 house-wide; further checks disabled.
- Click the label to edit it inline. Do not navigate to a full-page inspector.
- ↑ ↓ on the row (or a drag handle). Mapping array order.
- Live value on the right, already in house units.
- Tiny “…” or Advanced on the row, closed by default. Source identity lives only there.
- Remove is inside Advanced, confirm “Remove from the pass?”

Footer of the open card, not a mode switch:
- Search field: “Add from the house” — friendly-name hits, tap to add (weather still Outdoor+Condition, guest wifi still Guest Wi-Fi).
- “Add a line” — placeholder: two inline fields (label, value) and Add. No Entity/Placeholder toggle.

New card: one control at the bottom of the composer (“New card”), title prompt. Empty house: pass + that control only.

Copy: sentence case. Checkbox “On the pass”. Label field “Label” (never “On the pass as”). Header “Compose the pass”. No lecture subtitle. Labels readable (cream on ink, not muted-on-black). Autosave stays; Save can stay quiet in the header.

New card / rename / remove never use window.prompt, window.confirm, or window.alert (Safari iframes). New card is an inline title field + Add (Return submits). Rename edits the section title in place. Remove uses an in-panel confirm. UUID v4 falls back to crypto.getRandomValues if randomUUID is missing.

Do not expand Advanced on select. Do not show entity_id unless Advanced is open.

Wire unchanged (featuredRank, placeholders, °C, union). This is a panel-only UX bump (0.3.0).


Locked 24 Aug 2026: cards are **labels**, not types. Card lines are Entity or Placeholder. The UI splits into (1) create / browse / rename / reorder / delete cards and (2) populate the selected card. On the wire every card still sends `kind: "custom"` so iOS schema 1.0 does not bump. Do not show kind in the panel.

## Who

The integrator (or household operator) on the house HA.

## Surface

One panel. Same visual language in both places (ink / cream / gold, dark pass face, serif house name). Different API bases:

- Sidebar iframe: `/api/patrimony_collection/editor/...` with the parent HA Bearer token (`hass.auth.data.access_token`).
- Add-on page: relative `api/house`, `api/entities`, `api/mapping`.

**Left:** live **boarding-pass preview** from the same document the phone renders (or a local stand-in on the add-on). Glance grid: items with featuredRank 1–6, else the legacy first 6 (cards by priority then title). Then titled card blocks in card order. Badge: All secure / Needs a look / Alert. Format number+unit, bool Yes/No, null as an em dash. Nothing identifying a source appears on the preview.

**Middle — Cards:** the house's sections. Create a card (prompt for a title only). Browse the list. Rename. Reorder (priority). Delete (confirm: “Remove this card and its lines from the pass?”). No type picker. Empty house starts with no cards; do not seed Alarm/Network/Climate/etc.

**Right — Fill this card:** only enabled when a card is selected. The selected card’s lines list label, a quiet **On the pass** checkbox, Up, and Down. Mapping-array order is the open-house order; placeholders and entities both move. Two add actions, clearly split: **Entity** (search by friendly name) or **Placeholder** (typed title + value, no source). Entity: tap a name → it appears on the **selected** card with label = friendly name. Defaults: type auto, how it warns auto. **Advanced** (source identity, attribute, value type, how it warns, unit) stays behind a closed disclosure. Placeholder: fields are pass label and value only; optional unit if the value is a number. No source, no search. Confirmed remove asks: “Remove from the pass?” One quiet Save, with debounce autosave.

## Cards (labels)

A card is `{ id, title, priority, items }`. Title is free text. Kind is not a product field: always write `kind: "custom"` (schema still requires the key). Energy is still not a kind. Do not offer security/climate/network/cellar/arrivals as types.

Existing mappings: keep items and titles (Weather stays). Coerce kind to `custom` on save if anything else is on disk.



## Item order and the pass face

Two independent orders.

1. **In the card.** Fill this card lists the lines of the selected card. Up / Down (or equivalent) changes their order in that card. That is the order for the open-house blocks. Persist as mapping array order. Placeholders and entities both move.

2. **On the pass face.** Each line has a quiet checkbox, copy: **On the pass**. At most 6 checked across the whole house. Further checks are disabled until one is cleared. Checked lines are the 3-column glance (the handful at the top). Unchecked lines still appear in their card when the house is opened. Order on the face is `featuredRank` 1–6 among the checked set; Up / Down while focused on the face, or rank from check order. Do not use the words expose, pin, or featured in the UI.

If zero lines are checked, keep the legacy glance: first 6 items after cards sorted by priority, then title, items in card order. So the pass does not go blank.

Wire (schema 1.0, additive, unknown keys ignored): optional item field `featuredRank` integer 1–6. Omit when not on the face. Never send entity_id. Presentation cards still list every line in mapping-array order. HA preview (panel pass + REST/WS snapshot helper) uses featured_items: featured 1–6, else legacy first 6. Card blocks below still show all lines.

Storage (mapping.json / config-entry, never on the wire): `on_pass` bool, or implied by `featured_rank`. Store `featured_rank` 1–6 only on checked lines, unique in the house. A seventh check is refused. Reorder among checked rewrites ranks 1–6. `normalize_editor_payload` keeps these fields. Title collapse remaps `card_id` and keeps ranks and mapping-array order.

iOS must start honoring `featuredRank` for the wallet grid. Until then the phone still uses the legacy first six. Do not bump card-schema-v0.md; unknown keys are ignored.

## Placeholders

A card line is either live (`entity`) or typed (`placeholder`). Product words: Entity / Placeholder, never “fake” in the UI.

Placeholder mapping: no `entity_id`. Required non-empty `label` and a `value` (empty string allowed → show em dash). Default `value_type` `text`; if the typed value is a number and a unit is set, emit `number`. Severity `ok` unless Advanced says otherwise. `updatedAt` is save time. Never copy placeholder text that looks like an entity_id onto the wire as a source.

On the PresentationDocument it is a normal item (`label`, `value`, `valueType`, `unit`, `severity`). iOS unchanged.

Empty “Item — —” rows are broken placeholders: do not emit them; delete or require a label.

## Adding from search

Tap a name → it appears on the selected card. Keep stable card and item ids on edit.

- A weather source: one tap adds **both** Outdoor (temperature) and Condition (state) on the **selected** card. Do not auto-create a Weather card or retarget another card.
- A name that looks like guest wifi / WLAN / guest network, and a yes/no thing: label **Guest Wi-Fi**, warn when off — still on the selected card.

If no card is selected, do not add; prompt to select or create one.

The pass follows the house temperature unit, not the entity native unit.

## Copy

Default UI never uses: kind, type, expose, entity, ingress, entity_id, UUID, domain, severity_mode, value_type, property.id. Advanced may show the source identity.

## Save

Writes config-entry options and `mapping.json`. `on_pass` / `featured_rank` persist on mapping rows. Duplicate card titles collapse on load and save (one card per title; keep the one with items, union extras, drop empty duplicates; a unique empty card may stay). Empty or partial `mapping.json` is **unioned** on load so Weather Outdoor + Condition on `weather.home` cannot be wiped by an empty file. Editor POST: if load failed, or the payload has empty mappings while the house already has mappings, **union** and do not replace. After a confirmed remove (item or whole card), the full current UI state is posted and that replace is allowed.

## Out of scope

Writes/control, deltas, Energy kind, coordinates, tokens on the wire, phone calling weather APIs, iOS schema bump.

## Phone surfaces (unchanged schema)

1. **Wallet pass:** still + displayName + locationLabel + house badge + first 6 items flattened (cards already sorted priority, then title) in a 3-column grid + UPDATED. Kind and card title are not on the pass.
2. **Open house:** same pass, then one block per card title, items as label | value.

Item formatting (they format, they do not re-derive):
- number + unit → `18°C`
- bool → Yes / No
- enum/text → string as sent
- null → em dash
- severity colors: ok cream, attention gold, alert red
- Badge: All secure / Needs a look / Alert from max item severity

Every PresentationDocument `property` includes additive `localTime` (`HH:mm` 24-hour) in the house IANA timezone from HA's clock. `generatedAt` stays UTC. Not on items.

## In-place expand (0.4.3)

Locked 28 Aug 2026 (user): clicking a pass card in the stacked list expands that card in place and shows its mapping editor inside it. Other cards collapse (accordion, one open). Do not keep a second editor pane below the list that you have to scroll to. Same editor contents (On the pass, item up/down, add from the house, add a line, rename, delete). Visual language unchanged.

## Add disclosure (0.4.4)

Locked 28 Aug 2026 (user): expanded card shows existing lines only. “Add from the house” and “Add a line” are hidden until the integrator clicks Add. Then those two expand inside that card.

## Add control (0.4.4)

Locked 28 Aug 2026 (user): an expanded card shows existing lines by default (On the pass, reorder, advanced, rename, delete). One Add control. Clicking it reveals Add from the house and Add a line inside that card. Clicking Add again, or Done, hides them. Do not show those two search/fields on first expand. Visual language unchanged. No schema bump. No window.prompt.

## Cache-bust and header Add (0.4.5)

Locked 28 Aug 2026: the sidebar iframe URL includes `?v=<component version>` so Safari/HA does not keep a stale panel. Add sits on the expanded card header next to Rename/Delete. Clicking a stacked pass card (title or live rows) goes through `toggleCard`, which sets `addingLines` false, so first expand shows existing lines only.

## Expanded live value (0.4.6)

Locked 28 Aug 2026: an expanded card line must show the same live value as the collapsed pass preview (number+unit, already in house °C). Do not render an em dash when the preview has a value. The live column uses the preview item when lastEntities cannot coerce a value.

## Auth gate (0.4.7)

Locked 29 Aug 2026: do not call preview or any editor API unless Authorization is actually set. Iframe token from parent/top `home-assistant`/`hc-main` hass.auth, else same-origin `hassTokens`. 4s live preview continues once authed. No unauthenticated fetch (HA 401 / login-attempt spam).

## House subnav memory + checked-at (0.4.11)

Locked 29 Aug 2026: House tab remembers Home settings vs Connections in sessionStorage across panel reloads. Opening Connections still auto-runs Check links once; status shows “Checked · HH:MM” in the house timezone; Check links is disabled while a check is in flight.

## Panel place memory (0.4.13)

Session remembers the last main tab (Cards / People / House) and which mapping card was expanded, so an integrator can leave and come back without re-opening.

## Pass local time (0.4.12)

Locked 30 Aug 2026: the stacked boarding-pass preview shows the house local time on the right of the house name (HH:mm, 24-hour, property.timezone, else snapshot localTime). Ticks every 15s between preview polls so the integrator sees the same clock as the iOS wallet. Schema 1.0, kinds custom. No version bump.

## Session expired (0.4.14)

Locked 30 Aug 2026: a 401/403 from the editor API is a dead session (Home Assistant restarted, leftover iframe auth). Stop the 4s preview poll. Do not retry load, preview, mapping, entities, or check-links until Retry. Copy: “Session ended — reload Home Assistant”. Retry re-reads parent auth and calls load(); success clears expired and restarts the poll. One-shot parent re-read on the first 401. Non-auth load failures also wait for Retry instead of hammering load every 4s.

## Pass glance rank (0.4.16)

Locked 30 Aug 2026: checked lines show · N (featured_rank 1–6) after On the pass. Dedicated ↑↓ next to that checkbox reorder featured_rank house-wide among on-pass lines. In-card ↑↓ still change open-house (mapping-array) order within the card. Schema 1.0, kinds custom. No window.prompt.
Locked 30 Aug 2026: People edits debounce-autosave (1.2s) like Cards; no separate Save people control needed. Schema 1.0, kinds custom.
## Component version on Connections (0.4.18)

Locked 30 Aug 2026: Connections Component is the loaded panel version from the iframe `?v=` query (manifest at last setup), never a hardcoded string. Schema 1.0, kinds custom.

## Return to add (0.4.19)

Locked 30 Aug 2026: in an open Add, Return on Search by name adds the first hit (after flushing the 180ms search). Return on Label or Value adds the placeholder line. After either add, Add stays open and focus returns to search or Label so the next line is one keystroke. Schema 1.0, kinds custom. No window.prompt.


## Escape dismiss (0.4.20)

Locked 30 Aug 2026: Escape dismisses the current mapping overlay, innermost first: pending Keep/remove confirm, then cancel rename (no commit), then cancel new card, then close Add while keeping the card expanded. Does not collapse the open mapping card. Schema 1.0, kinds custom. No window.prompt.


## Search pick + skip duplicate (0.4.21)

Locked 30 Aug 2026: Search by name is keyboard-complete. ArrowDown/ArrowUp move a highlight among current hits (wrap; class `is-active`; scrollIntoView nearest). Return adds the highlighted hit (hitIndex), not always the first; if lastHits is empty, flush the 180ms search first and restore hitIndex when the entity_id list is unchanged, else reset to 0. Hits already mapped on the open card show a quiet gold `On this card` mark. addFromSearch skips duplicates: weather only adds missing Outdoor/Condition (both present → status `Already on this card`); guest Wi-Fi bool already on card selects it and status; default line already on card with no state_attribute selects it and status. afterAdd clears search, keeps Add open, refocuses #q, resets hitIndex. Schema 1.0, kinds custom. No window.prompt.
