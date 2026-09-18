"""Constants for Patrimony Collection (HA custom integration)."""

from typing import Final

DOMAIN: Final = "patrimony_collection"

SCHEMA_VERSION: Final = 1
EVENT_STATE: Final = "patrimony/state"
WS_TYPE_GET_STATE: Final = "patrimony/get_state"
REST_PATH: Final = "/api/patrimony_collection/state"
CONTACTS_PATH: Final = "/api/patrimony_collection/contacts"
CONTACTS_FILE: Final = "patrimony_collection/contacts.json"
CONTACTS_METHODS: Final = ("cellular", "viber", "whatsapp")
MAX_CONTACTS: Final = 40

PHOTO_PATH: Final = "/api/patrimony_collection/photo"
PHOTO_FILE: Final = "patrimony_collection/face.jpg"
PHOTO_MAX_BYTES: Final = 2 * 1024 * 1024
PHOTO_SOURCE_HEADER: Final = "X-Patrimony-Photo-Source"
NOTES_PATH: Final = "/api/patrimony_collection/notes"
NOTES_FILE: Final = "patrimony_collection/notes.json"
NOTES_MAX_CHARS: Final = 8000
PAIR_PATH: Final = "/api/patrimony_collection/pair"
PAIR_CLAIM_PATH: Final = "/api/patrimony_collection/p/{code}"
PAIR_CLIENT_NAME: Final = "Patrimony iOS"
PAIR_TICKET_TTL_SECONDS: Final = 15 * 60
NOTIFY_PATH: Final = "/api/patrimony_collection/notify"
NOTIFY_FILE: Final = "patrimony_collection/notify.json"
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
CONF_CARDS: Final = "cards"
CONF_MAPPINGS: Final = "mappings"
CONF_HOUSE_EVENT_KEY: Final = "house_event_key"

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
DEFAULT_PRIORITY: Final = 100
SNAPSHOT_DEBOUNCE_SECONDS: Final = 0.3

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
        "access_token",
        "refresh_token",
        "ha_token",
        "long_lived_token",
        "long_lived_access_token",
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
