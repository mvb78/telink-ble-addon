import os
from Crypto.Cipher import AES


def normalize_16_bytes(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > 16:
        return raw[:16]
    return raw + b"\x00" * (16 - len(raw))


def derive_base_key(mesh_name: str, password: str) -> bytes:
    name_bytes = normalize_16_bytes(mesh_name)
    pass_bytes = normalize_16_bytes(password)
    return bytes(a ^ b for a, b in zip(name_bytes, pass_bytes))


def java_aes(a: bytes, b: bytes) -> bytes:
    """
    Telink SDK AES quirk used by BT-Light APK:
      java_aes(a, b) = reverse(AES_ECB(reverse(a), reverse(b)))
    Both a and b are zero-padded to 16 bytes.
    """
    a16 = (a + b"\x00" * 16)[:16]
    b16 = (b + b"\x00" * 16)[:16]
    cipher = AES.new(bytes(reversed(a16)), AES.MODE_ECB)
    result = cipher.encrypt(bytes(reversed(b16)))
    return bytes(reversed(result))


def get_session_key(mesh_name: str, password: str, r1: bytes, r2: bytes) -> bytes:
    """
    SK = java_aes(base_key, r1 + r2)
    Confirmed against fresh HCI capture: full round-trip re-encryption matches.
    """
    base_key = derive_base_key(mesh_name, password)
    return java_aes(base_key, r1 + r2)


def build_challenge(base_key: bytes, r1: bytes) -> bytes:
    """
    8-byte challenge sent in 0x0C packet.
    challenge = java_aes(r1_padded16, base_key)[0:8]
    Confirmed: java_aes(r1+zeros8, bk)[0:8] == challenge from capture.
    """
    r1_padded = r1 + b"\x00" * 8
    return java_aes(r1_padded, base_key)[:8]


def verify_sample_s(mesh_name: str, password: str, r2: bytes, sample_s: bytes) -> bool:
    """
    Verify lamp's sample_s.
    sample_s = java_aes(r2_padded16, base_key)[0:8]
    """
    base_key = derive_base_key(mesh_name, password)
    r2_padded = r2 + b"\x00" * 8
    expected = java_aes(r2_padded, base_key)[:8]
    return expected == sample_s[:8]


def generate_random_8() -> bytes:
    return os.urandom(8)


def build_ivm(mac_bytes: bytes, seq: int) -> bytes:
    """
    IVM = getSecIVM(getMacBytes(), seq)
    getMacBytes() = reverse([0x68,0xEC,0x62,0x02,0x8A,0x54]) = [0x54,0x8A,0x02,0x62,0xEC,0x68]
    getSecIVM: copy 6 reversed MAC bytes, then ivm[4]=1, ivm[5..7]=seq LE
    -> IVM = [0x54, 0x8A, 0x02, 0x62, 0x01, seq0, seq1, seq2]
    mac_bytes must be in wire order (MSB first, as from MAC string parsing).
    """
    mac_rev = list(reversed(mac_bytes))
    ivm = mac_rev[:6] + [0, 0]
    ivm[4] = 0x01
    ivm[5] = seq & 0xFF
    ivm[6] = (seq >> 8) & 0xFF
    ivm[7] = (seq >> 16) & 0xFF
    return bytes(ivm)


def _aes_block(sk: bytes, data: bytes) -> bytes:
    return java_aes(sk, data)


def decrypt_notification(session_key: bytes, data: bytes, mac_bytes: bytes) -> bytes | None:
    """
    Decrypt a 20-byte ATT_NOTIFY packet from a Telink lamp.

    IVM = getSecIVS(mac)[0..2] + data[0..4]  (LightController.java onNotify)
    Decrypt range: bytes 7-19 (13 bytes, starting at the opcode field).
    """
    if len(data) < 20:
        return None
    mac_rev = list(reversed(mac_bytes))
    ivm = bytes([mac_rev[0], mac_rev[1], mac_rev[2],
                 data[0], data[1], data[2], data[3], data[4]])
    ctr = bytearray(b"\x00" + ivm + b"\x00" * 7)
    ks = _aes_block(session_key, bytes(ctr))
    pkt = bytearray(data)
    for i in range(13):
        pkt[7 + i] ^= ks[i]
    return bytes(pkt)


def encrypt_packet(session_key: bytes, packet: bytes, mac_bytes: bytes) -> bytes:
    """
    Encrypt a 20-byte Telink mesh command packet.

    From aes_att_encryption_packet in libTelinkCrypto_arm64_v8a.so:
      length = 15  (bytes 5..19)

      MIC phase (CBC-MAC over plaintext pkt[5..19]):
        nonce = ivm[0..7] + [length] + [0]*7
        nonce = aes_block(sk, nonce)
        for i in 0..14:
            nonce[i%16] ^= pkt[5+i]
            if (i%16)==15 or i==14: nonce = aes_block(sk, nonce)
        pkt[3], pkt[4] = nonce[0], nonce[1]

      CTR phase (encrypt pkt[5..19] in-place):
        ctr = [0x00] + ivm[0..7] + [0]*7
        for i in 0..14:
            if i%16==0: ks = aes_block(sk, ctr); ctr[0] += 1
            pkt[5+i] ^= ks[i%16]
    """
    pkt = bytearray(packet)
    seq = pkt[0] | (pkt[1] << 8) | (pkt[2] << 16)
    ivm = build_ivm(mac_bytes, seq)
    length = 15

    # MIC phase
    nonce = bytearray(ivm + bytes([length]) + b"\x00" * 7)
    nonce = bytearray(_aes_block(session_key, bytes(nonce)))
    for i in range(length):
        nonce[i % 16] ^= pkt[5 + i]
        if (i % 16) == 15 or i == length - 1:
            nonce = bytearray(_aes_block(session_key, bytes(nonce)))
    pkt[3] = nonce[0]
    pkt[4] = nonce[1]

    # CTR phase
    ctr = bytearray(b"\x00" + ivm + b"\x00" * 7)
    for i in range(length):
        if i % 16 == 0:
            ks = _aes_block(session_key, bytes(ctr))
            ctr[0] = (ctr[0] + 1) & 0xFF
        pkt[5 + i] ^= ks[i % 16]

    return bytes(pkt)
