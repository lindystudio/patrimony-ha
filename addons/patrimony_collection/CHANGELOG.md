# Changelog

## 0.4.122
- Notes: the house notes become a list. GET /api/patrimony_collection/notes and /m/notes always return items [{id, title, body, order, updatedAt, updatedBy}] (updatedBy is {memberId, kind}, never a name), a document etag and textWritable, next to the existing text, which is now derived from the items for older phones (title and body per note, title alone when the body is empty, never empty while a note has content). PUT {items, ifMatch} replaces the whole list in the order sent; ifMatch (the document etag) is required for a list write and checked on both the owner and member paths (409 notes_conflict with the current document on a mismatch). Limits are checked with 400 codes: title 80 characters, body 8000, 50 notes. On first load, existing notes text becomes one untitled note (one-time, marker notes_items_04122). A text-only save from an older phone or the panel keeps working until the list is edited in the app; after that it is refused with 409 notes_items_exist and the list is left untouched. The panel notepad becomes read-only at that point, and the add-on never overwrites a list. Member writes are logged as notes/items, never with content. The card schema is unchanged.

## 0.4.121
- Members: a guest's access no longer ends while it is in use. Each member row records the last day it was used (UTC date only, lastUsedDay; also in GET /members). Access lapses only after more than 365 days without use: the member routes then answer 401 member_token_expired and /m/pairing answers unpaired with reason expired; a re-invite restores access. Using any member route, including /m/pairing, renews it, with at most one store write per member per day. A new invite starts the clock on its mint day. On upgrade, every live member row is stamped with the upgrade day, so nobody can lapse before November 2027. An explicit ttlDays is still honoured (whichever comes first); revoke, cancel and removal are unchanged; owner access is unaffected.

## 0.4.120
- Members: member access no longer expires by default. A mint without ttlDays stores no expiry (GET /members shows expiresAt null). An explicit ttlDays (greater than 0, at most 365) is still honoured. On load, every live member row with a stored expiry is migrated to no expiry (the store cannot tell the old 30-day default from an explicit ttl), including recently lapsed and hand-backdated rows that were not revoked, cancelled, or removed; the same token keeps working, with no re-invite. Rows already marked cancelled (cancelledAt), even without revokedAt, fail authentication. Revoke remains how access ends.


## 0.4.119
- Paired phones: a guest renamed by the owner (or reset to no name) now shows the current name in the paired phones list (GET /api/patrimony_collection/clients), so owner Chat > Members follows renames. The list is no longer cached (Cache-Control: no-store).

## 0.4.118
- Members: the house owner can rename a member (PATCH /api/patrimony_collection/members/{id}, by principalId or memberId). An empty name goes back to the fallback. Names are never logged.
- Chat: guest names are resolved when chat is read, so a rename also shows on older messages, including messages sent before a guest re-joined. A guest without a name shows as "Guest · 31 Oct" (first invite date, house timezone).
- Chat: an owner phone with an edited name shows its current name on older messages.
- Fix: a guest's name is never used as the owner phone's label.
- Members list: optional invitedAt and connectedAt dates per member.
- Paired phones: the owner's phone can rename its own entry (POST /api/patrimony_collection/clients/{id}).

## 0.4.117
- Chat: a removed and re-invited guest keeps "mine", sender id and read receipts on older messages.
