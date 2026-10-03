# House tools 0.4 (photo, notes, pair, notify)

Integrator + principal. Lives on the house HA. Not on `patrimony/state`. schemaVersion 1 unchanged.

Tabs stay Cards | People | Soon. Soon is these four, not a "later" placeholder. Visual language unchanged (ink / cream / gold, Fraunces, sentence case). No window.prompt.

## Photograph

GET/PUT/DELETE/POST `/api/patrimony_collection/photo` (same HA Bearer as state). `image/jpeg` or `image/webp`, max 2 MB. Store `/config/patrimony_collection/face.jpg`.

- GET: stored still if present, else the bundled product still. Header `X-Patrimony-Photo-Source: custom|default`. 404 only if both are missing.
- PUT: replace with a house jpeg/webp.
- DELETE: remove the stored file. GET then falls back to the bundled still.
- POST: write the bundled still back to `face.jpg` (Restore default). No upload.

iOS: wallet still is this GET (full-bleed, dusk wash). Change from the house card (Photos picker) and from Soon. PUT then GET to confirm. Never base64 on the state document.

HA Soon: current still, Replace, Remove, and Restore default when there is no stored photograph.

## Notepad

GET/PUT `/api/patrimony_collection/notes`
`{ "schemaVersion": 1, "propertyId": "<uuid>", "text": "", "updatedAt": "<ISO Z>" }`
Plain text, cap 8k. Last write wins. Anyone with the house token sees the same page.

iOS: one quiet notes screen per house. HA Soon: same textarea, autosave.

## Pairing QR (HA long-lived token → iOS)

Admin-only on Soon. House URL comes from HA `get_url(prefer_external=True, allow_cloud=True)` so Nabu Casa Cloud counts, then config `external_url` / `internal_url`. Mint refuses a missing/hostless URL (503) before creating a token; `error.message` is the resolved URL (or `(empty)`) plus config snapshot, not canned Settings prose alone. Token/mint exceptions put `Type: str(err)` in `error.message`. Button "Show a pairing code" mints a long-lived HA token named `Patrimony iOS` (HA auth API / WS `auth/long_lived_access_token`). Draw a QR of:

`patrimony://pair?url=<https house URL>&token=<llat>`

Show the QR once. Never write the token into mapping.json, contacts.json, notes, logs, or git. iOS Settings camera: scan, store URL + token in Keychain (`collection.patrimony.house` / property.id), never UserDefaults.

This is not the `hek_` house event key and not the Grok Bot token.

## Notify

Soon: one field, 1–120 chars, Send. HA POSTs backend `POST /v1/properties/{propertyId}/events` with `X-House-Event-Key` and body `{ "cardId": "<stable notify uuid>", "severity": "attention", "title": "<text>" }`. APNs payload stays `{ propertyId, cardId, severity, title }`. App not running still gets the system banner.

If `hek_` is missing, do not mint an HA token as the key. Show that push needs the house event key. Never send live HA state in the payload.

iOS already registers APNs device tokens with the backend. Banner title is the house name; body is `title`. Tap opens that house.

## Out of scope

Energy, coordinates, tokens on patrimony/state, schema bump, deleting Weather/People cards.

## Chat

House members. On this HA only. Not on `patrimony/state`.

- `GET /api/patrimony_collection/chat`
- `POST /api/patrimony_collection/chat` with `{ "text", "retention" }` and optional `deviceName`. `retention` is `keep` (default), `1h`, `1d`, or `7d`. An iOS caller prefers a non-empty trimmed `deviceName` (max 80) as the sender label; otherwise it uses the stored iOS session device name. A panel caller uses the Home Assistant user name and ignores `deviceName`.
- `DELETE /api/patrimony_collection/chat/{message_id}` removes the row. The sender can delete their own. The HA panel user can delete any.

At rest the text and any photo are encrypted with a host key that is not in git, the presentation document, or push. Not end-to-end. A photo is optional `imageBase64` (standard base64, no `data:` prefix) plus `imageContentType` (`image/jpeg`, `image/png`, or `image/webp`). Text may be empty when a photo is present. Decoded photos are capped at 4 MB. The list sets `hasImage` and does not inline the bytes. `GET /api/patrimony_collection/chat/{message_id}/image` returns the raw bytes. Delete and expiry remove the photo with the message.

Push reuses the events POST and is not the 20-minute attention debounce. The title is only `New chat message in {display_name}` or `New chat message`. The events body is `{ cardId, severity, title }`. No message text, sender, preview, or image bytes on that path.

Panel: House → Chat, same screen as Home settings. Each row shows the message time in the stored house timezone (Home Assistant timezone, then UTC, if that is missing). The delete control stays on the right. A photo loads from the image URL.
