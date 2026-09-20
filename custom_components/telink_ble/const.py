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

DEFAULT_PORT = 8098
DEFAULT_POLL_INTERVAL_SECONDS = 30
MIN_POLL_INTERVAL_SECONDS = 5

# Split polling (fast truth, cheap BLE bill): the coordinator refreshes every
# FAST_POLL_SECONDS with cheap reads (lamp/group lists over REST, daemon
# state cache over TCP — all memory-speed, no BLE traffic). The expensive
# bulk BLE status query + daemon liveness run at most every
# SLOW_STATUS_SECONDS.
FAST_POLL_SECONDS = 10
SLOW_STATUS_SECONDS = 90

# Staleness watchdog: a lamp whose daemon push cache is older than this
# (while the daemon reports running) pages the user via a persistent
# notification instead of failing silently. Re-notify at most every hour
# per lamp; notifications auto-dismiss on recovery.
STALE_AFTER_SECONDS = 15 * 60
STALE_RENOTIFY_SECONDS = 60 * 60

# Add-on API
API_LAMPS = "/api/lamps"
API_GROUPS = "/api/groups"
API_STATUS_ALL = "/api/command/status"
API_CMD_ON = "/api/command/on"
API_CMD_OFF = "/api/command/off"
API_CMD_BRIGHTNESS = "/api/command/brightness"
API_CMD_COLORTEMP = "/api/command/colortemp"
API_CMD_SET = "/api/command/set"
API_DAEMON = "/api/daemon"

# Add-on brightness is 0-100; HA brightness is 0-255.
VAL_MIN = 0
VAL_MAX = 100

CONF_HOST = CONF_HOST
CONF_PORT = CONF_PORT

# Sidecar daemon (Variant B): direct TCP JSON protocol on 0.0.0.0:8097, used
# for cheap evented state reads (kind=state) that bypass the add-on's web app.
DEFAULT_DAEMON_PORT = 8097
DAEMON_STATE_TIMEOUT = 3.0
