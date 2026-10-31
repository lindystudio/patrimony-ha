# Changelog

## 0.4.118
- Members: the house owner can rename a member (PATCH /api/patrimony_collection/members/{id}, by principalId or memberId). An empty name goes back to the fallback. Names are never logged.
- Chat: guest names are resolved when chat is read, so a rename also shows on older messages, including messages sent before a guest re-joined. A guest without a name shows as "Guest · 31 Oct" (first invite date, house timezone).
- Chat: an owner phone with an edited name shows its current name on older messages.
- Fix: a guest's name is never used as the owner phone's label.
- Members list: optional invitedAt and connectedAt dates per member.
- Paired phones: the owner's phone can rename its own entry (POST /api/patrimony_collection/clients/{id}).

## 0.4.117
- Chat: a removed and re-invited guest keeps "mine", sender id and read receipts on older messages.
