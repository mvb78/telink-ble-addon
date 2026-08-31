from config import VENDOR_ID as _VENDOR_ID


class SequenceManager:
    def __init__(self, initial: int | None = None):
        if initial is not None and 1 <= initial <= 0xFFFFFF:
            # resume after last persisted seq to avoid duplicate sno dedup (ble_hardware_reference.md:2110)
            self.seq = (initial + 1) & 0xFFFFFF
            if self.seq == 0:
                self.seq = 1
        else:
            self.seq = 0x000001

    def next(self) -> int:
        value = self.seq
        self.seq = (self.seq + 1) & 0xFFFFFF
        if self.seq == 0:
            self.seq = 1
        return value


def build_mesh_packet(seq: int, address: int, opcode: int, params: bytes) -> bytes:
    if len(params) > 10:
        raise ValueError("Parameters too long (max 10 bytes)")

    packet = bytearray(20)

    # Sequence (24-bit little endian)
    packet[0] = seq & 0xFF
    packet[1] = (seq >> 8) & 0xFF
    packet[2] = (seq >> 16) & 0xFF

    # Reserved
    packet[3] = 0x00
    packet[4] = 0x00

    # Address (little endian)
    packet[5] = address & 0xFF
    packet[6] = (address >> 8) & 0xFF

    # Opcode
    packet[7] = opcode

    # Vendor ID (little endian)
    packet[8] = _VENDOR_ID & 0xFF
    packet[9] = (_VENDOR_ID >> 8) & 0xFF

    # Parameters
    for i in range(len(params)):
        packet[10 + i] = params[i]

    return bytes(packet)


def build_redundant_packet(seq: int, address: int, opcode: int, params: bytes) -> tuple[bytes, bytes]:
    """
    Constructs two identical command packets to satisfy Telink's redundancy requirement.
    """
    packet1 = build_mesh_packet(seq, address, opcode, params)
    # The second packet is identical to the first one
    packet2 = bytes(packet1)
    return packet1, packet2