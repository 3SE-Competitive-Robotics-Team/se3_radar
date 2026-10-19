"""雷达无线链路空口包。

每包 27 B：Access Code(8, 大端) | 00 0F 00 0F（payload 长度 15，重复两次）| payload(15)
无前导、无白化、无 FEC，按 MSB 先发。
"""

from enum import Enum

PAYLOAD_LEN = 15
HEADER = bytes([0x00, PAYLOAD_LEN, 0x00, PAYLOAD_LEN])
AC_LEN = 8
PACKET_LEN = AC_LEN + len(HEADER) + PAYLOAD_LEN
PACKET_BITS = PACKET_LEN * 8


class WaveKind(Enum):
    INFO = "info"
    JAM = "jam"


ACCESS_CODES = {
    WaveKind.INFO: 0x2F6F4C74B914492E,
    WaveKind.JAM: 0x16E8D377151C712D,
}


def access_code_bytes(kind: WaveKind) -> bytes:
    return ACCESS_CODES[kind].to_bytes(AC_LEN, "big")


def build_packet(kind: WaveKind, payload: bytes) -> bytes:
    if len(payload) != PAYLOAD_LEN:
        raise ValueError(f"payload 必须是 {PAYLOAD_LEN} B，实际 {len(payload)} B")
    return access_code_bytes(kind) + HEADER + payload


def packetize(kind: WaveKind, stream: bytes, pad: int = 0x00) -> list[bytes]:
    """把字节流切成 15 B 一片，不足一片的尾部用 pad 补齐。"""
    rem = len(stream) % PAYLOAD_LEN
    if rem:
        stream += bytes([pad]) * (PAYLOAD_LEN - rem)
    return [build_packet(kind, stream[i:i + PAYLOAD_LEN]) for i in range(0, len(stream), PAYLOAD_LEN)]


def parse_packet(packet: bytes, kind: WaveKind) -> bytes | None:
    """字节已对齐的空口包 → payload；接入码或头不符返回 None。"""
    if len(packet) != PACKET_LEN:
        return None
    if packet[:AC_LEN] != access_code_bytes(kind) or packet[AC_LEN:AC_LEN + len(HEADER)] != HEADER:
        return None
    return packet[AC_LEN + len(HEADER):]
