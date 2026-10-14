"""Constants for Patrimony Collection (HA custom integration)."""

from typing import Final

DOMAIN: Final = "patrimony_collection"

SCHEMA_VERSION: Final = 1
EVENT_STATE: Final = "patrimony/state"
WS_TYPE_GET_STATE: Final = "patrimony/get_state"
REST_PATH: Final = "/api/patrimony_collection/state"
CONTACTS_PATH: Final = "/api/patrimony_collection/contacts"
CONTACTS_FILE: Final = "patrimony_collection/contacts.json"
# `method` stays one of these so phones on older app builds can still decode the list.
CONTACTS_METHODS: Final = ("cellular", "viber", "whatsapp")
# `app` is the real choice (facetime, sms, telegram, signal, ...). Any short slug is
# kept, so a newer phone can add an app without a house update.
CONTACT_APP_PATTERN: Final = r"[a-z0-9][a-z0-9-]{0,31}"
MAX_CONTACTS: Final = 200
# Role and name keep this many characters, the number CONTACT_TEL_MAX. The panel and
# the iPhone editor enforce the same limits so nothing is cut off silently.
CONTACT_TEXT_MAX: Final = 80
CONTACT_TEL_MAX: Final = 40

PHOTO_PATH: Final = "/api/patrimony_collection/photo"
PHOTO_FILE: Final = "patrimony_collection/face.jpg"
PHOTO_MAX_BYTES: Final = 2 * 1024 * 1024
PHOTO_SOURCE_HEADER: Final = "X-Patrimony-Photo-Source"
NOTES_PATH: Final = "/api/patrimony_collection/notes"
VERSION_PATH: Final = "/api/patrimony_collection/version"
HOUSE_PATH: Final = "/api/patrimony_collection/house"
NOTES_FILE: Final = "patrimony_collection/notes.json"
NOTES_MAX_CHARS: Final = 8000
CHAT_PATH: Final = "/api/patrimony_collection/chat"
CHAT_READ_PATH: Final = "/api/patrimony_collection/chat/read"
CHAT_MESSAGE_PATH: Final = "/api/patrimony_collection/chat/{message_id}"
CHAT_IMAGE_PATH: Final = "/api/patrimony_collection/chat/{message_id}/image"
CHAT_FILE: Final = "patrimony_collection/chat.json"
CHAT_KEY_FILE: Final = "patrimony_collection/chat.key"
CHAT_TEXT_MAX: Final = 2000
CHAT_IMAGE_MAX: Final = 4 * 1024 * 1024
PAIR_PATH: Final = "/api/patrimony_collection/pair"
PAIR_CLAIM_PATH: Final = "/api/patrimony_collection/p/{code}"
PAIR_CLIENT_NAME: Final = "Patrimony iOS"
PAIR_TICKET_TTL_SECONDS: Final = 15 * 60
PAIR_TICKET_FILE: Final = "patrimony_collection/pair_tickets.json"
PAIR_CODE_BYTES: Final = 24  # 192 bits; token_urlsafe -> 32 chars
PAIR_CLAIM_PER_IP_PER_MIN: Final = 10
PAIR_CLAIM_GLOBAL_PER_MIN: Final = 60
PAIR_CLAIM_REPLAY_GRACE_SECONDS: Final = 10.0
NOTIFY_PATH: Final = "/api/patrimony_collection/notify"
NOTIFY_FILE: Final = "patrimony_collection/notify.json"
EVENT_KEY_PATH: Final = "/api/patrimony_collection/event_key"
IOS_SESSION_PATH: Final = "/api/patrimony_collection/ios_session"
IOS_SESSION_FILE: Final = "patrimony_collection/ios_session.json"
PAIRING_PATH: Final = "/api/patrimony_collection/pairing"
CLIENTS_PATH: Final = "/api/patrimony_collection/clients"
CLIENT_PATH: Final = "/api/patrimony_collection/clients/{client_id}"
# 0.4.96 member access (opaque scoped token; not an HA token)
MEMBERS_FILE: Final = "patrimony_collection/members.json"
MEMBERS_PATH: Final = "/api/patrimony_collection/members"
MEMBERS_MINT_PATH: Final = "/api/patrimony_collection/members/mint"
MEMBER_REVOKE_PATH: Final = "/api/patrimony_collection/members/{principal_id}/revoke"
MEMBER_STATE_PATH: Final = "/api/patrimony_collection/m/state"
MEMBER_PAIRING_PATH: Final = "/api/patrimony_collection/m/pairing"
MEMBER_CHAT_PATH: Final = "/api/patrimony_collection/m/chat"
# 0.4.98 member PQ shield: per-member session, same scheme/envelope as the owner.
MEMBER_PQ_PUBLIC_PATH: Final = "/api/patrimony_collection/m/pq/public"
MEMBER_PQ_ENCAP_PATH: Final = "/api/patrimony_collection/m/pq/encap"
MEMBER_PQ_ACCEPT_ENCAP_PATH: Final = "/api/patrimony_collection/m/pq/accept_encap"  # alias of encap (mirrors owner name)
MEMBER_PQ_ROTATE_PATH: Final = "/api/patrimony_collection/m/pq/rotate"
# While owner PQ shield is ON, a member with no PQ session gets 428 pq_session_required.
# Override per house with config-entry option CONF_MEMBER_PQ_REQUIRED (bool).
MEMBER_PQ_REQUIRED: Final = True
CONF_MEMBER_PQ_REQUIRED: Final = "member_pq_required"
MEMBER_TOKEN_PREFIX: Final = "pmt_"
MEMBER_TOKEN_TTL_DAYS_DEFAULT: Final = 30
MEMBER_TOKEN_TTL_DAYS_MAX: Final = 365
MEMBER_NAME_MAX: Final = 80
MEMBER_INACTIVE_KEEP_DAYS: Final = 30
CLIENT_UNPAIR_PATH: Final = "/api/patrimony_collection/clients/{client_id}/unpair"
IOS_DEVICE_NAME_MAX: Final = 80
IOS_APP_VERSION_MAX: Final = 40
IOS_IOS_VERSION_MAX: Final = 40
IOS_MODEL_MAX: Final = 32
IOS_HOSTNAME_MAX: Final = 253
IOS_DNS_TIMEOUT_SECONDS: Final = 1.0
BACKEND_BASE: Final = "https://api.patrimonycollection.com"
NOTIFY_USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
NOTIFY_TITLE_MAX: Final = 120
DEFAULT_HOUSE_URL: Final = ""

CONF_PROPERTY_ID: Final = "property_id"
CONF_DISPLAY_NAME: Final = "display_name"
CONF_LOCATION_LABEL: Final = "location_label"
CONF_TIMEZONE: Final = "timezone"
CONF_ALREADY_HAVE_PROPERTY_ID: Final = "already_have_property_id"
CONF_CARDS: Final = "cards"
CONF_MAPPINGS: Final = "mappings"
CONF_HOUSE_EVENT_KEY: Final = "house_event_key"
CONF_PQ_SHIELD: Final = "pq_shield"

# Phase 1 PQ Shield. Paths match pq-shield-phase1/api-freeze.md.
PQ_SHIELD_CAPABILITY_PATH: Final = "/api/patrimony_collection/pq_shield/capability"
PQ_SHIELD_PUBLIC_PATH: Final = "/api/patrimony_collection/pq_shield/public"
PQ_SHIELD_ACCEPT_ENCAP_PATH: Final = "/api/patrimony_collection/pq_shield/accept_encap"
PQ_SHIELD_ROTATE_PATH: Final = "/api/patrimony_collection/pq_shield/rotate"
PQ_SHIELD_DISABLE_PATH: Final = "/api/patrimony_collection/pq_shield/disable"
# HA panel / options only — not phone enable wire.
PQ_SHIELD_FEATURE_PATH: Final = "/api/patrimony_collection/pq_shield/feature"
PQ_SHIELD_STORE_FILE: Final = "patrimony_collection/pq_shield.json"
PUSH_LOG_PATH: Final = "/api/patrimony_collection/push_log"

# Closed kind set from card-schema-v0.md section 5. energy is not a kind.
KINDS: Final = ("security", "climate", "network", "cellar", "arrivals", "custom")
KIND_DEFAULT_TITLES: Final = {
    "security": "Alarm",
    "climate": "Climate",
    "network": "Network",
    "cellar": "Cellar",
    "arrivals": "Arrivals",
    "custom": "Custom",
}

VALUE_TYPES: Final = ("enum", "number", "bool", "text")
SEVERITIES: Final = ("ok", "attention", "alert")
SEVERITY_RANK: Final = {"ok": 0, "attention": 1, "alert": 2}

SEVERITY_MODES: Final = (
    "auto",
    "ok_when_on",
    "ok_when_off",
    "binary_alert_on",
    "enum_map",
    "number_range",
    "fixed",
)

MAX_CARDS: Final = 24
MAX_ITEMS_PER_CARD: Final = 12
MAX_ON_PASS: Final = 6
MAX_EVENTS: Final = 8
MAX_EVENT_MESSAGE_CHARS: Final = 80
DEFAULT_PRIORITY: Final = 100
SNAPSHOT_DEBOUNCE_SECONDS: Final = 0.3
# Push-state coalescing: burst cap and minimum spacing between snapshot emits.
SNAPSHOT_MAX_WAIT_SECONDS: Final = 2.0
SNAPSHOT_MIN_INTERVAL_SECONDS: Final = 1.0

FORBIDDEN_WIRE_KEYS: Final = frozenset(
    {
        "entity_id",
        "entities",
        "device_id",
        "area_id",
        "unique_id",
        "latitude",
        "longitude",
        "lat",
        "lon",
        "coordinate",
        "coordinates",
        "gps",
        "gps_accuracy",
        "location",
        "region",
        "map",
        "street",
        "city",
        "country",
        "country_code",
        "countryCode",
        "address",
        "street_address",
        "streetAddress",
        "postal_code",
        "postalCode",
        "zip",
        "zip_code",
        "zipCode",
        "usesFahrenheit",
        "uses_fahrenheit",
        "access_token",
        "refresh_token",
        "ha_token",
        "long_lived_token",
        "long_lived_access_token",
        "house_event_key",
        "houseEventKey",
        "iosSession",
        "lastInteractionAt",
        "internal_url",
        "external_url",
        "base_url",
        "passTypeIdentifier",
        "serialNumber",
        "pkpass",
        "state",
        "attributes",
        "device_class",
        "static_value",
        "saved_at",
        "on_pass",
        "featured_rank",
    }
)

UNAVAILABLE_STATES: Final = frozenset({"unavailable", "unknown", "none", ""})

BOOL_TRUE: Final = frozenset({"on", "true", "1", "yes", "unlocked", "open", "connected"})
BOOL_FALSE: Final = frozenset({"off", "false", "0", "no", "locked", "closed", "disconnected"})

# lock.* entity state → schema enum display. Do not invent a fifth valueType.
LOCK_ENUM_VALUES: Final = {
    "locked": "Locked",
    "unlocked": "Unlocked",
}

# HA weather.* condition states. Severity decided here; phone does not call a weather API.
WEATHER_ALERT_CONDITIONS: Final = frozenset(
    {"exceptional", "lightning", "lightning-rainy", "hail"}
)
WEATHER_ATTENTION_CONDITIONS: Final = frozenset(
    {"pouring", "snowy", "snowy-rainy", "windy", "windy-variant"}
)

# Written by the Supervisor add-on Ingress UI; entity_id stays on disk only.
MAPPING_FILE: Final = "patrimony_collection/mapping.json"

WEATHER_LABELS: Final = {
    "clear-night": "Clear night",
    "cloudy": "Cloudy",
    "fog": "Fog",
    "hail": "Hail",
    "lightning": "Lightning",
    "lightning-rainy": "Lightning and rain",
    "partlycloudy": "Partly cloudy",
    "pouring": "Pouring rain",
    "rainy": "Rain",
    "snowy": "Snow",
    "snowy-rainy": "Snow and rain",
    "sunny": "Sunny",
    "windy": "Windy",
    "windy-variant": "Windy",
    "exceptional": "Severe weather",
}
PROPERTY_PATH: Final = "/api/patrimony_collection/property"
PROPERTY_ADOPT_LOG_PATH: Final = "/api/patrimony_collection/property/adopt_log"
CONF_CLAIMED_AT: Final = "claimed_at"
CONF_ADOPT_LOCK_MIGRATED: Final = "adopt_lock_migrated"
CONF_PROPERTY_ID_SOURCE: Final = "property_id_source"
CONF_PROPERTY_ID_HISTORY: Final = "property_id_history"
ADOPT_LOG_MAX: Final = 50
ADOPT_RATE_MAX: Final = 5
ADOPT_RATE_WINDOW_SECONDS: Final = 600
