# House tools 0.4 (photo, notes, pair, notify)

Integrator + principal. Lives on the house HA. Not on `patrimony/state`. schemaVersion 1 unchanged.

Tabs stay Cards | People | Soon. Soon is these four, not a "later" placeholder. Home settings has one free-text **Location** field (not street / city / country). Visual language unchanged (ink / cream / gold, Fraunces, sentence case). No window.prompt.

## Photograph

GET/PUT `/api/patrimony_collection/photo` (same HA Bearer as state). `image/jpeg` or `image/webp`, max 2 MB. Store `/config/patrimony_collection/face.jpg`. GET 404 if missing.

iOS: wallet still is this file (full-bleed, dusk wash). Change from the house card (Photos picker) and from Soon. PUT then GET to confirm. Never base64 on the state document.

HA Soon: current still, Replace, Remove.

## Notepad

GET/PUT `/api/patrimony_collection/notes`
`{ "schemaVersion": 1, "propertyId": "<uuid>", "text": "", "updatedAt": "<ISO Z>" }`
Plain text, cap 8k. Last write wins. Anyone with the house token sees the same page.

iOS: one quiet notes screen per house. HA Soon: same textarea, autosave.

## Pairing QR (HA long-lived token → iOS)

Admin-only on Soon. Button "Show a pairing code" mints a long-lived HA token named `Patrimony iOS` (HA auth API / WS `auth/long_lived_access_token`). Draw a QR of:

`patrimony://pair?url=<https house URL>&token=<llat>`

Show the QR once. Never write the token into mapping.json, contacts.json, notes, logs, or git. iOS Settings camera: scan, store URL + token in Keychain (`collection.patrimony.house` / property.id), never UserDefaults.

This is not the `hek_` house event key and not the Grok Bot token.

## Notify

Soon: one field, 1–120 chars, Send. HA POSTs backend `POST /v1/properties/{propertyId}/events` with `X-House-Event-Key` and body `{ "cardId": "<stable notify uuid>", "severity": "attention", "title": "<text>" }`. APNs payload stays `{ propertyId, cardId, severity, title }`. App not running still gets the system banner.

If `hek_` is missing, do not mint an HA token as the key. Show that push needs the house event key. Never send live HA state in the payload.

iOS already registers APNs device tokens with the backend. Banner title is the house name; body is `title`. Tap opens that house.

## Out of scope

Energy, coordinates, tokens on patrimony/state, schema bump, deleting Weather/People cards.
