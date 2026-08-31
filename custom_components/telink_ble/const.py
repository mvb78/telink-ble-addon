"""Constants for the Telink BLE Lights integration."""

from homeassistant.const import CONF_HOST, CONF_PORT

DOMAIN = "telink_ble"

# The add-on talks to the lamps over the same REST API regardless of how it is
# reached. In HAOS the Supervisor exposes it through Ingress (no port needed);
# a manual host/port is the fallback for non-add-on installs.
ADDON_SLUG = "telink_ble_cli"

CONF_ADDON = "addon"
CONF_ADDON_INSTALLED = "addon_installed"
CONF_ADDON_USE_INGRESS = "addon_use_ingress"
CONF_POLL_INTERVAL = "poll_interval"

DEFAULT_PORT = 8099
DEFAULT_POLL_INTERVAL_SECONDS = 30
MIN_POLL_INTERVAL_SECONDS = 5

# Add-on API
API_LAMPS = "/api/lamps"
API_GROUPS = "/api/groups"
API_STATUS_ALL = "/api/command/status"
API_CMD_ON = "/api/command/on"
API_CMD_OFF = "/api/command/off"
API_CMD_BRIGHTNESS = "/api/command/brightness"
API_CMD_COLORTEMP = "/api/command/colortemp"
API_DAEMON = "/api/daemon"

# Add-on brightness is 0-100; HA brightness is 0-255.
VAL_MIN = 0
VAL_MAX = 100

CONF_HOST = CONF_HOST
CONF_PORT = CONF_PORT
