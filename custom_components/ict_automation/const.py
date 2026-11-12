"""Constants for the ICT Protege integration."""

from __future__ import annotations

DOMAIN = "ict_automation"
MANUFACTURER = "Integrated Control Technology"

CONF_PIN = "pin"
CONF_RECORDS = "records"
CONF_EVENTS_PORT = "events_port"
CONF_EVENTS_PIN = "events_pin"
CONF_WX_USERNAME = "wx_username"
CONF_WX_PASSWORD = "wx_password"
CONF_WX_PORT = "wx_port"

DEFAULT_WX_PORT = 443

ARM_MODES = ["away", "home", "night"]

DEFAULT_PORT = 21000

EVENT_PANEL = f"{DOMAIN}_event"
EVENT_EMERGENCY = f"{DOMAIN}_emergency"
EVENT_DOOR = f"{DOMAIN}_door"

EVENT_TYPES = [
    "access_granted",
    "access_denied",
    "armed",
    "disarmed",
    "alarm",
    "door",
    "input",
    "output",
    "user",
    "trouble",
    "other",
]

# How the integration reaches the panel.
CONF_CONNECTION = "connection"
CONNECTION_ACS = "acs"  # Automation and Control service (needs the licence)
CONNECTION_WEB = "web"  # WX web interface only

# Web-only mode: how arming and disarming check a code.
CONF_CODE_MODE = "code_mode"
CONF_CONTROL_PORT = "control_port"
CONF_USE_WEB = "use_web"
CONF_NUMBERING = "numbering"
CODE_MODE_HA = "ha_codes"  # codes kept (hashed) by the integration
CODE_MODE_NONE = "none"  # no code; Home Assistant's own permissions only
CONF_CODES = "codes"

# Record IDs typed in the options flow. Discovery never removes these.
CONF_MANUAL_RECORDS = "manual_records"
# Missing IDs in a row before a scan of that record type stops. 0 scans the full range.
CONF_DISCOVERY_GAP = "discovery_gap"
# "door:0" keys whose entities must keep the 1.x unique IDs (ict_door_0, ict_area_1, ...).
CONF_LEGACY_KEYS = "legacy_keys"

# The 2.0–2.2 scan stopped after 3 misses and started at ID 1, so a door at ID 0 and
# anything after a short hole (empty inputs, outputs that start in the teens) disappeared.
DEFAULT_DISCOVERY_GAP = 64
