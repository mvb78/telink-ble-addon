"""
Persists discovered lamps in lamps.json.

Schema per entry:
  {
    "mac":          "AA:BB:CC:DD:00:01",
    "name":         "Smart_nSpq",
    "password":     "0000",
    "mesh":         "isolated",     # "isolated" | "shared"
    "mesh_address": 1               # optional; 1-63 if provisioned
  }

Mesh assignment:
  password "0000"  → "isolated"
  password "1234"  → "shared"

Backward compatibility:
  - old field "group" is accepted on load
  - old values "single"/"group" are mapped to "isolated"/"shared"
"""

import json
import os
from config import LAMPS_FILE


MESH_ISOLATED = "isolated"
MESH_SHARED = "shared"


def _mesh_for(password: str) -> str:
    return MESH_ISOLATED if password == "0000" else MESH_SHARED


def _normalize_mesh_value(value: str | None, password: str | None = None) -> str:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in (MESH_ISOLATED, "single"):
            return MESH_ISOLATED
        if v in (MESH_SHARED, "group"):
            return MESH_SHARED
    if isinstance(password, str):
        return _mesh_for(password)
    return MESH_SHARED


def _normalize_selector(selector: str) -> str:
    s = selector.strip().lower()
    if s == "all":
        return "all"
    if s in (MESH_ISOLATED, "single"):
        return MESH_ISOLATED
    if s in (MESH_SHARED, "group"):
        return MESH_SHARED
    raise Exception(f"Unknown selector '{selector}' (use: all, isolated/shared, single/group)")


def _normalize_entry(entry: dict) -> dict:
    normalized = dict(entry)
    mac = normalized.get("mac")
    if isinstance(mac, str):
        normalized["mac"] = mac.upper()
    password = normalized.get("password")
    mesh_raw = normalized.get("mesh", normalized.get("group"))
    normalized["mesh"] = _normalize_mesh_value(mesh_raw, password=password)
    normalized.pop("group", None)
    return normalized


def load() -> list[dict]:
    if not os.path.exists(LAMPS_FILE):
        return []
    with open(LAMPS_FILE) as f:
        lamps = json.load(f)
    return [_normalize_entry(entry) for entry in lamps]


def save(lamps: list[dict]) -> None:
    canonical = [_normalize_entry(entry) for entry in lamps]
    with open(LAMPS_FILE, "w") as f:
        json.dump(canonical, f, indent=2)
    print(f"Saved {len(canonical)} lamp(s) to {LAMPS_FILE}")


def upsert(lamps: list[dict], mac: str, name: str, password: str, mesh_address: int | None = None) -> list[dict]:
    mac = mac.upper()
    for entry in lamps:
        if str(entry["mac"]).upper() == mac:
            entry["name"] = name
            entry["password"] = password
            entry["mesh"] = _mesh_for(password)
            entry.pop("group", None)
            if mesh_address is not None:
                entry["mesh_address"] = mesh_address
            return lamps
    lamp = {"mac": mac, "name": name, "password": password, "mesh": _mesh_for(password)}
    if mesh_address is not None:
        lamp["mesh_address"] = mesh_address
    lamps.append(lamp)
    return lamps


def get_targets(lamps: list[dict], selector: str, mac: str | None = None, addr: int | None = None) -> list[dict]:
    """
    selector: "all" | "isolated" | "shared" (or legacy: "single" | "group")
    mac:      specific MAC address (overrides selector)
    addr:     specific mesh address (overrides selector)
    """
    # --- Optimized: Check address first/most specific/first loop iteration ---
    
    if addr is not None:
        # Check for exact address match
        for l in lamps:
            if l.get("mesh_address") == addr:
                return [l] # Return as a list, matching other return styles
        raise Exception(f"No lamp with mesh_address {addr} in lamps.json")
    
    # --- Optimized: Check MAC second ---
    if mac:
        mac = mac.upper()
        for l in lamps:
            if l["mac"].upper() == mac:
                return [l]
        raise Exception(f"Lamp {mac} not in lamps.json — run 'discover' first")

    # --- Optimized: Handle 'all' selector ---
    if selector == "all":
        return lamps

    # --- Optimized: Determine selector once ---
    normalized_selector = _normalize_selector(selector)
    
    if normalized_selector == "all":
        return lamps

    # --- Optimized: Final filter using a single loop ---
    matches = []
    for l in lamps:
        # Check if the normalized mesh type matches the required selector
        if _normalize_mesh_value(l.get("mesh", l.get("group")), password=l.get("password")) == normalized_selector:
            matches.append(l)
            
    return matches

    if addr is not None:
        matches = [l for l in lamps if l.get("mesh_address") == addr]
        if not matches:
            raise Exception(f"No lamp with mesh_address {addr} in lamps.json")
        return matches
    if mac:
        mac = mac.upper()
        matches = [l for l in lamps if l["mac"].upper() == mac]
        if not matches:
            raise Exception(f"Lamp {mac} not in lamps.json — run 'discover' first")
        return matches
    if selector == "all":
        return lamps
    normalized_selector = _normalize_selector(selector)
    if normalized_selector == "all":
        return lamps
    return [
        l
        for l in lamps
        if _normalize_mesh_value(l.get("mesh", l.get("group")), password=l.get("password")) == normalized_selector
    ]


def print_table(lamps: list[dict]) -> None:
    if not lamps:
        print("  (none)")
        return
    print(f"  {'MAC':<20} {'Name':<20} {'Password':<10} {'Addr'}")
    print(f"  {'-'*20} {'-'*20} {'-'*10} {'-'*4}")
    for l in lamps:
        addr = str(l.get("mesh_address", ""))
        print(f"  {l['mac']:<20} {l['name']:<20} {l['password']:<10} {addr}")
