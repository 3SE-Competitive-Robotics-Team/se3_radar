"""按空中时间生成发射用的空口包流。

GNU Radio 发射流图按硬件时钟向这里要数据（拉模式），所以这里不看墙上时间，
而是按「已经发出去的包数 × 每包时长」推算空中时间：
- 信息波：每 100 ms 空中时间追加一组 0x0A01~0x0A05（140 B）；
  信道容量约 1477 B/s，比 1400 B/s 多一点，空出来的 payload 用 0x00 填（解析端会跳过）。
- 干扰波：每包一帧 0x0A06，seq 逐包加 1。
"""

import random
from dataclasses import dataclass, field

import numpy as np

from .. import phy
from ..dsp import bytes_to_bits
from ..protocol import air
from . import source

PACKET_SECONDS = air.PACKET_BITS * phy.SPS / phy.SAMPLE_RATE
INFO_PERIOD_S = 0.1
FILL = 0x00


@dataclass
class InfoStream:
    """模拟信息波。state_factory 每 100 ms 被调用一次，返回要广播的 InfoState。"""

    state_factory: callable = None
    seed: int | None = None
    _queue: bytearray = field(default_factory=bytearray)
    _packets: int = 0
    _cycles: int = 0
    _seq: int = 0

    def __post_init__(self):
        if self.state_factory is None:
            rng = random.Random(self.seed)
            self.state_factory = lambda: source.random_info_state(rng)

    def next_packet(self) -> bytes:
        while self._cycles * INFO_PERIOD_S <= self._packets * PACKET_SECONDS:
            self._queue += source.info_cycle(self.state_factory(), self._seq)
            self._seq = (self._seq + 5) & 0xFF
            self._cycles += 1
        payload = bytes(self._queue[:air.PAYLOAD_LEN])
        del self._queue[:air.PAYLOAD_LEN]
        payload += bytes([FILL]) * (air.PAYLOAD_LEN - len(payload))
        self._packets += 1
        return air.build_packet(air.WaveKind.INFO, payload)


@dataclass
class JamStream:
    """模拟干扰波。key 可随时修改（对应裁判系统换密钥）。"""

    key: str = "000000"
    _seq: int = 0

    def next_packet(self) -> bytes:
        pkt = air.build_packet(air.WaveKind.JAM, source.jam_frame(self.key, self._seq))
        self._seq = (self._seq + 1) & 0xFF
        return pkt


def take_symbols(stream, max_symbols: int, max_packets: int) -> tuple[np.ndarray, int]:
    """取整包并转成 ±1 的 float32 符号（MSB 先）。

    包数 = min(max_symbols 能装下的包数, max_packets)，且至少 1 包，避免发射流图断流。
    返回 (符号, 包数)。
    """
    n = min(max(1, max_symbols // air.PACKET_BITS), max_packets)
    bits = bytes_to_bits(b"".join(stream.next_packet() for _ in range(n)))
    return bits.astype(np.float32) * 2 - 1, n
