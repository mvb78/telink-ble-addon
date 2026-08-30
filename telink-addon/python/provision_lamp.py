"""
provision_lamp.py — re-provision a Telink lamp using APK-style handshake.

Connects to a lamp by MAC, authenticates with current credentials,
then sets new mesh name, password, and mesh address.

Usage:
  python provision_lamp.py \
    --mac AA:BB:CC:DD:00:01 \
    --current_name Smart_nSpq --current_password 0000 \
    --new_name Smart_nSpq --new_password 0000 \
    --mesh_address 1
"""

import argparse
import asyncio
import os

from bleak import BleakClient, BleakScanner
from Crypto.Cipher import AES

from config import CHAR_PAIR_UUID, CHAR_COMMAND_UUID
from telink_crypto import derive_base_key, build_challenge, verify_sample_s, get_session_key, java_aes


def normalize_16(s: str) -> bytearray:
    raw = s.encode("utf-8")[:16]
    return bytearray(raw + b"\x00" * (16 - len(raw)))


def encrypt_pair_data(session_key: bytes, data: bytearray) -> bytearray:
    """Encrypt 16 bytes of pair data using java_aes(session_key, data)."""
    result = bytearray(java_aes(session_key, bytes(data)))
    result.reverse()
    return result


async def apk_login(client: BleakClient, name: str, password: str) -> bytes:
    base_key = derive_base_key(name, password)
    r1 = os.urandom(8)
    challenge = build_challenge(base_key, r1)

    payload = bytearray(17)
    payload[0] = 0x0C
    payload[1:9] = r1
    payload[9:17] = challenge

    await client.write_gatt_char(CHAR_PAIR_UUID, bytes(payload), response=True)
    await asyncio.sleep(0.5)

    rsp = await client.read_gatt_char(CHAR_PAIR_UUID)
    if not rsp or rsp[0] != 0x0D or len(rsp) < 17:
        raise Exception(f"Login failed: {rsp.hex() if rsp else 'no response'}")

    r2 = bytes(rsp[1:9])
    sample_s = bytes(rsp[9:17])

    if not verify_sample_s(name, password, r2, sample_s):
        raise Exception("sample_s verification failed — wrong password")

    session_key = get_session_key(name, password, r1, r2)
    print(f"  Authenticated OK (session_key={session_key.hex()})")
    return session_key


async def set_mesh_param(client: BleakClient, session_key: bytes, opcode: int, value: bytearray):
    """Send a pair characteristic write: [opcode] + encrypt(value)."""
    encrypted = encrypt_pair_data(session_key, value)
    payload = bytearray([opcode]) + encrypted
    await client.write_gatt_char(CHAR_PAIR_UUID, bytes(payload), response=True)
    await asyncio.sleep(0.3)


async def set_mesh_address(client: BleakClient, session_key: bytes, mac_bytes: bytes, mesh_address: int):
    """Send opcode 0xE0 via command characteristic to assign mesh address."""
    from telink_mesh import SequenceManager, build_mesh_packet
    from telink_crypto import encrypt_packet

    seq_mgr = SequenceManager()
    seq = seq_mgr.next()
    params = bytes([mesh_address & 0xFF, (mesh_address >> 8) & 0xFF])
    packet = build_mesh_packet(seq, 0, 0xE0, params)
    encrypted = encrypt_packet(session_key, packet, mac_bytes)
    await client.write_gatt_char(CHAR_COMMAND_UUID, encrypted, response=False)
    await asyncio.sleep(4.0)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mac", required=True)
    parser.add_argument("--current_name", default="out_of_mesh")
    parser.add_argument("--current_password", default="123")
    parser.add_argument("--new_name", required=True)
    parser.add_argument("--new_password", required=True)
    parser.add_argument("--mesh_address", type=int, required=True)
    args = parser.parse_args()

    mac = args.mac.upper()
    mac_bytes = bytes.fromhex(mac.replace(":", ""))

    print(f"Scanning for {mac} ...")
    device = await BleakScanner.find_device_by_address(mac, timeout=15)
    if not device:
        print("Device not found.")
        return

    print(f"Found {device.name} ({device.address}), connecting ...")
    async with BleakClient(device.address) as client:
        session_key = await apk_login(client, args.current_name, args.current_password)

        print(f"  Setting mesh address to {args.mesh_address} ...")
        await set_mesh_address(client, session_key, mac_bytes, args.mesh_address)

        print(f"  Setting mesh name to '{args.new_name}' ...")
        await set_mesh_param(client, session_key, 0x04, normalize_16(args.new_name))

        print(f"  Setting mesh password to '{args.new_password}' ...")
        await set_mesh_param(client, session_key, 0x05, normalize_16(args.new_password))

        # LTK
        default_ltk = bytearray([
            0xc0, 0xc1, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7,
            0xd8, 0xd9, 0xda, 0xdb, 0xdc, 0xdd, 0xde, 0xdf
        ])
        print("  Setting LTK ...")
        ltk_payload = encrypt_pair_data(session_key, default_ltk)
        ltk_cmd = bytearray([0x06]) + ltk_payload + bytearray([0x01])
        await client.write_gatt_char(CHAR_PAIR_UUID, bytes(ltk_cmd), response=True)
        await asyncio.sleep(0.3)

        result = await client.read_gatt_char(CHAR_PAIR_UUID)
        print(f"  Pair state after provisioning: 0x{result[0]:02x}")
        if result[0] in (0x07, 0x0F):
            print(f"  SUCCESS — lamp provisioned as '{args.new_name}' address {args.mesh_address}")
        else:
            print(f"  Unexpected state 0x{result[0]:02x} — may have failed")


if __name__ == "__main__":
    asyncio.run(main())
