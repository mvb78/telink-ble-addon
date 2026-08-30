"""
Persists human-friendly mesh group names in groups.json.

Schema per entry:
  {
    "name": "top-lights",
    "address": 32768,
    "lamps": ["AA:BB:CC:DD:EE:FF"]  # MACs of lamps subscribed to this group (optional)
  }

Address range:
  0x8000..0xFFFE (inclusive)
"""

from __future__ import annotations

import json
import os

from config import GROUPS_FILE


GROUP_ADDR_MIN = 0x8000
GROUP_ADDR_MAX = 0xFFFE


def _normalize_name(name: str) -> str:
    if not isinstance(name, str):
        raise Exception("Group name must be a string")
    normalized = name.strip()
    if not normalized:
        raise Exception("Group name must not be empty")
    return normalized


def _name_key(name: str) -> str:
    return _normalize_name(name).lower()


def _normalize_address(value) -> int:
    if isinstance(value, str):
        value = int(value, 0)
    if not isinstance(value, int):
        raise Exception("Group address must be an integer")
    if not (GROUP_ADDR_MIN <= value <= GROUP_ADDR_MAX):
        raise Exception(
            f"Group address {value} out of range ({GROUP_ADDR_MIN:#06x}..{GROUP_ADDR_MAX:#06x})"
        )
    return value


def _normalize_mac(value) -> str:
    if not isinstance(value, str):
        raise Exception(f"Invalid lamp MAC in group: {value!r}")
    mac = value.strip().upper()
    if len(mac) < 8 or ":" not in mac:
        raise Exception(f"Invalid lamp MAC in group: {value!r}")
    return mac


def _normalize_entry(entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise Exception(f"Invalid group entry: {entry!r}")
    name = _normalize_name(entry.get("name", ""))
    addr = _normalize_address(entry.get("address"))
    lamps_raw = entry.get("lamps") or []
    lamps = sorted({_normalize_mac(m) for m in lamps_raw}) if isinstance(lamps_raw, list) else []
    return {"name": name, "address": addr, "lamps": lamps}


def load() -> list[dict]:
    if not os.path.exists(GROUPS_FILE):
        return []
    with open(GROUPS_FILE) as f:
        groups = json.load(f)

    if isinstance(groups, dict):
        groups = [{"name": k, "address": v} for k, v in groups.items()]

    normalized = [_normalize_entry(entry) for entry in groups]

    # Validate uniqueness
    seen_names: set[str] = set()
    seen_addresses: set[int] = set()
    for group in normalized:
        key = _name_key(group["name"])
        addr = group["address"]
        if key in seen_names:
            raise Exception(f"Duplicate group name in {GROUPS_FILE}: {group['name']}")
        if addr in seen_addresses:
            raise Exception(f"Duplicate group address in {GROUPS_FILE}: {addr:#06x}")
        seen_names.add(key)
        seen_addresses.add(addr)

    return sorted(normalized, key=lambda g: g["address"])


def save(groups: list[dict]) -> None:
    canonical = sorted([_normalize_entry(entry) for entry in groups], key=lambda g: g["address"])
    with open(GROUPS_FILE, "w") as f:
        json.dump(canonical, f, indent=2)
    print(f"Saved {len(canonical)} group(s) to {GROUPS_FILE}")


def find_by_name(groups: list[dict], name: str) -> dict | None:
    key = _name_key(name)
    for group in groups:
        if _name_key(group["name"]) == key:
            return group
    return None


def _next_free_address(groups: list[dict]) -> int:
    used = {entry["address"] for entry in groups}
    for addr in range(GROUP_ADDR_MIN, GROUP_ADDR_MAX + 1):
        if addr not in used:
            return addr
    raise Exception("No free group addresses left in range 0x8000..0xFFFE")


def create(groups: list[dict], name: str) -> tuple[list[dict], dict]:
    normalized_name = _normalize_name(name)
    existing = find_by_name(groups, normalized_name)
    if existing:
        raise Exception(
            f"Group '{existing['name']}' already exists at address {existing['address']:#06x}"
        )

    addr = _next_free_address(groups)
    created = {"name": normalized_name, "address": addr, "lamps": []}
    return groups + [created], created


def delete(groups: list[dict], name: str) -> tuple[list[dict], dict]:
    key = _name_key(name)
    for i, group in enumerate(groups):
        if _name_key(group["name"]) == key:
            removed = group
            updated = groups[:i] + groups[i + 1:]
            return updated, removed
    raise Exception(f"Group '{name}' does not exist")


def print_table(groups: list[dict]) -> None:
    if not groups:
        print("  (no groups)")
        return
    print(f"  {'Name':<24} {'Address':<8} {'Hex':<6} {'Lamps'}")
    print(f"  {'-'*24} {'-'*8} {'-'*6} {'-'*20}")
    for group in sorted(groups, key=lambda g: g["address"]):
        lamps = ", ".join(group.get("lamps") or []) or "-"
        print(f"  {group['name']:<24} {group['address']:<8} {group['address']:#06x}  {lamps}")


def add_member(group: dict, mac: str) -> bool:
    normalized = _normalize_mac(mac)
    lamps = group.setdefault("lamps", [])
    if normalized in lamps:
        return False
    lamps.append(normalized)
    return True


def remove_member(group: dict, mac: str) -> bool:
    normalized = _normalize_mac(mac)
    lamps = group.setdefault("lamps", [])
    if normalized in lamps:
        lamps.remove(normalized)
        return True
    return False
