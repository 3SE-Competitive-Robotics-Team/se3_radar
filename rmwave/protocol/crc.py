"""裁判系统 CRC，与通信协议附录一的查表实现等价。

CRC8 : Maxim，反射多项式 0x8C，初值 0xFF，无末尾异或。
CRC16: 反射多项式 0x8408，初值 0xFFFF，无末尾异或，按小端追加。
"""


def _make_table(poly: int, width: int) -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
        table.append(crc & ((1 << width) - 1))
    return table


_CRC8_TABLE = _make_table(0x8C, 8)
_CRC16_TABLE = _make_table(0x8408, 16)

CRC8_INIT = 0xFF
CRC16_INIT = 0xFFFF


def crc8(data: bytes, init: int = CRC8_INIT) -> int:
    crc = init
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc


def crc16(data: bytes, init: int = CRC16_INIT) -> int:
    crc = init
    for b in data:
        crc = (crc >> 8) ^ _CRC16_TABLE[(crc ^ b) & 0xFF]
    return crc
