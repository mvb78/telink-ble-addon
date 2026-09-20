SERVICE_UUID      = "00010203-0405-0607-0809-0a0b0c0d1910"
CHAR_NOTIFY_UUID  = "00010203-0405-0607-0809-0a0b0c0d1911"
CHAR_COMMAND_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
CHAR_STATUS_UUID  = "00010203-0405-0607-0809-0a0b0c0d1913"  # read + write-WoR, no notify
CHAR_PAIR_UUID    = "00010203-0405-0607-0809-0a0b0c0d1914"

VENDOR_ID = 0x0211

# Tried in order during discovery until one passes sample_s verification.
# Bench-validated creds (telink-ble-esp32 research, 2026-09): 8888 = our
# Smart_qXsx mesh; 1234/0000/123 = factory defaults (Smart_qXsx, Smart_nSpq,
# out_of_mesh/telink_mesh1) so unprovisioned and foreign-mesh lamps are found.
DEFAULT_KNOWN_PASSWORDS = ["8888", "1234", "0000", "123"]
KNOWN_PASSWORDS = list(DEFAULT_KNOWN_PASSWORDS)

from pathlib import Path
import os as _os

# Data dir: overridable for the Home Assistant add-on (persists in /data).
# Default stays next to this file so the CLI works unchanged on a desktop.
_DATA_DIR = _os.environ.get("TELINK_DATA_DIR", str(Path(__file__).parent))
LAMPS_FILE = str(Path(_DATA_DIR) / "lamps.json")
GROUPS_FILE = str(Path(_DATA_DIR) / "groups.json")

# Known passwords can be overridden via env (add-on option) without editing this file.
_pw_env = _os.environ.get("TELINK_KNOWN_PASSWORDS")
KNOWN_PASSWORDS = _pw_env.split(",") if _pw_env else list(DEFAULT_KNOWN_PASSWORDS)

# Scan duration in seconds during discovery (add-on option).
SCAN_TIMEOUT = int(_os.environ.get("TELINK_SCAN_TIMEOUT", "60"))

# Bluetooth adapter pinning: drive ONE adapter exclusively from all bleak
# scanner/client + raw-HCI-monitor paths. TELINK_HCI_ADAPTER accepts:
#   "hciN"       — kernel name (FRAGILE: enumeration order flips across
#                  reboots; observed hci0<->hci1 swap after a host reboot),
#   "usb:VVVV:PPPP" — USB VID:PID in hex (STABLE: follows the physical
#                  dongle regardless of enumeration, e.g. "usb:0b05:190e"
#                  for the ASUS USB-BT500),
#   AA:BB:CC:DD:EE:FF — adapter BD_ADDR (stable, needs `address` readable).
# Empty/unset = bleak default (old behavior). Typical layout: the dedicated
# USB dongle for the Telink mesh, HA's own Bluetooth on the internal adapter.
HCI_ADAPTER = (_os.environ.get("TELINK_HCI_ADAPTER") or "").strip() or None


def _sysfs_hci_entries() -> dict[str, dict[str, str]]:
    """Map hciN -> {product, address} from sysfs (best effort)."""
    out: dict[str, dict[str, str]] = {}
    try:
        import pathlib as _pl
        base = _pl.Path("/sys/class/bluetooth")
        if not base.is_dir():
            return out
        for entry in base.iterdir():
            name = entry.name
            if not name.startswith("hci") or ":" in name:
                continue
            info: dict[str, str] = {}
            try:
                uevent = (entry / "device" / "uevent").read_text()
                for line in uevent.splitlines():
                    if line.startswith("PRODUCT="):
                        info["product"] = line.split("=", 1)[1].strip()
            except OSError:
                pass
            try:
                info["address"] = (entry / "address").read_text().strip().upper()
            except OSError:
                pass
            out[name] = info
    except OSError:
        pass
    return out


def resolve_hci_adapter(selector: str | None = HCI_ADAPTER) -> str | None:
    """Resolve a stable adapter selector to the current kernel hciN name."""
    if not selector:
        return None
    sel = selector.strip()
    if sel.lower().startswith("hci"):
        return sel.lower()  # explicit kernel name (fragile across reboots)
    entries = _sysfs_hci_entries()
    if sel.lower().startswith("usb:"):
        want = sel[4:].lower().replace(":", "/")
        want_parts = [p.zfill(4) for p in want.split("/")]

        def _norm_product(raw: str) -> list[str]:
            return [p.zfill(4) for p in raw.lower().split("/")]

        for name in sorted(entries):
            prod = _norm_product(entries[name].get("product", ""))
            if len(prod) >= len(want_parts) and prod[:len(want_parts)] == want_parts:
                return name
        return None
    normalized = sel.upper().replace("-", ":")
    if len(normalized) == 17 and normalized.count(":") == 5:
        for name in sorted(entries):
            if entries[name].get("address", "").upper() == normalized:
                return name
        return None
    return None


def hci_adapter_index(name: str | None = None) -> int | None:
    """Map an adapter selector to its kernel index for the monitor bind."""
    resolved = resolve_hci_adapter(HCI_ADAPTER if name is None else name)
    if resolved is None:
        # Plain hciN without sysfs (or unset) — parse directly / all adapters.
        if name is None:
            return None
        try:
            return int(str(name).lower().replace("hci", ""))
        except (TypeError, ValueError, AttributeError):
            return None
    try:
        return int(resolved.lower().replace("hci", ""))
    except (TypeError, ValueError, AttributeError):
        return None
