"""裁判系统串口帧（0xA5）编码与流式重组。

帧格式：SOF(0xA5) | data_length(u16 LE) | seq | CRC8 | cmd_id(u16 LE) | data | CRC16(LE)
雷达无线链路的 15 B 空口 payload 首尾相连就是这种帧组成的字节流。
"""

import struct
from dataclasses import dataclass, field

from .crc import crc8, crc16

SOF = 0xA5
HEADER_LEN = 5  # SOF + len + seq + CRC8
CMD_LEN = 2
TAIL_LEN = 2
OVERHEAD = HEADER_LEN + CMD_LEN + TAIL_LEN


@dataclass(frozen=True)
class RefereeFrame:
    cmd_id: int
    data: bytes
    seq: int = 0


def encode_frame(cmd_id: int, data: bytes, seq: int = 0) -> bytes:
    header = struct.pack("<BHB", SOF, len(data), seq & 0xFF)
    header += bytes([crc8(header)])
    body = header + struct.pack("<H", cmd_id) + data
    return body + struct.pack("<H", crc16(body))


@dataclass
class ReassemblerStats:
    frames: int = 0
    crc8_fail: int = 0
    crc16_fail: int = 0
    too_long: int = 0
    dropped_bytes: int = 0


@dataclass
class FrameReassembler:
    """把任意切分的字节流还原成通过 CRC 校验的帧。

    CRC8 或 CRC16 失败时只丢掉当前 SOF 字节，从下一个字节重新找 SOF，
    这样数据里恰好出现的 0xA5 不会吞掉后面的真帧。
    """

    max_data_len: int = 256
    max_buffer: int = 16384
    stats: ReassemblerStats = field(default_factory=ReassemblerStats)
    _buf: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> list[RefereeFrame]:
        self._buf += chunk
        frames = []
        while True:
            start = self._buf.find(SOF)
            if start < 0:
                self.stats.dropped_bytes += len(self._buf)
                self._buf.clear()
                break
            if start:
                self.stats.dropped_bytes += start
                del self._buf[:start]
            if len(self._buf) < HEADER_LEN:
                break
            if crc8(self._buf[:HEADER_LEN - 1]) != self._buf[HEADER_LEN - 1]:
                self.stats.crc8_fail += 1
                self._skip_sof()
                continue
            data_len = self._buf[1] | (self._buf[2] << 8)
            if data_len > self.max_data_len:
                self.stats.too_long += 1
                self._skip_sof()
                continue
            total = OVERHEAD + data_len
            if len(self._buf) < total:
                break
            frame = bytes(self._buf[:total])
            if crc16(frame[:-TAIL_LEN]) != struct.unpack_from("<H", frame, total - TAIL_LEN)[0]:
                self.stats.crc16_fail += 1
                self._skip_sof()
                continue
            cmd_id = struct.unpack_from("<H", frame, HEADER_LEN)[0]
            frames.append(RefereeFrame(cmd_id, frame[HEADER_LEN + CMD_LEN:-TAIL_LEN], frame[3]))
            self.stats.frames += 1
            del self._buf[:total]
        if len(self._buf) > self.max_buffer:
            self.stats.dropped_bytes += len(self._buf) - self.max_buffer
            del self._buf[:-self.max_buffer]
        return frames

    def _skip_sof(self) -> None:
        self.stats.dropped_bytes += 1
        del self._buf[:1]
