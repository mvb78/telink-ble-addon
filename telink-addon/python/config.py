SERVICE_UUID      = "00010203-0405-0607-0809-0a0b0c0d1910"
CHAR_NOTIFY_UUID  = "00010203-0405-0607-0809-0a0b0c0d1911"
CHAR_COMMAND_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
CHAR_STATUS_UUID  = "00010203-0405-0607-0809-0a0b0c0d1913"  # read + write-WoR, no notify
CHAR_PAIR_UUID    = "00010203-0405-0607-0809-0a0b0c0d1914"

VENDOR_ID = 0x0211

# Tried in order during discovery until one passes sample_s verification
KNOWN_PASSWORDS = ["8888"]

from pathlib import Path
import os as _os

# Data dir: overridable for the Home Assistant add-on (persists in /data).
# Default stays next to this file so the CLI works unchanged on a desktop.
_DATA_DIR = _os.environ.get("TELINK_DATA_DIR", str(Path(__file__).parent))
LAMPS_FILE = str(Path(_DATA_DIR) / "lamps.json")
GROUPS_FILE = str(Path(_DATA_DIR) / "groups.json")

# Known passwords can be overridden via env (add-on option) without editing this file.
_pw_env = _os.environ.get("TELINK_KNOWN_PASSWORDS")
KNOWN_PASSWORDS = _pw_env.split(",") if _pw_env else ["8888"]

# Scan duration in seconds during discovery (add-on option).
SCAN_TIMEOUT = int(_os.environ.get("TELINK_SCAN_TIMEOUT", "45"))
