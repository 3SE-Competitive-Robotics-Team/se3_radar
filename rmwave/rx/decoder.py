"""空口包 → 裁判帧 → 0x0A 命令；以及带频偏跟踪的单信道接收器。"""

from dataclasses import dataclass, field

import numpy as np

from .. import phy
from ..dsp import ChannelFrontend
from ..protocol import air, cmds
from ..protocol.referee import FrameReassembler
from .demod import AirPacket, PacketDemodulator


@dataclass(frozen=True)
class DecodedFrame:
    cmd_id: int
    seq: int
    message: object       # cmds 里的数据类；未知命令或长度不符时为 None
    raw: bytes
    sample_index: int     # 帧首字节所在空口包的起点


@dataclass
class WaveDecoder:
    """一路信号（信息波或某一级干扰波）的完整解析。输入为鉴频输出。

    相邻两包起点间隔超过 1.5 个包长时视为丢包，清掉未完成的半帧，
    避免把不连续的字节拼成一帧。
    """

    kind: air.WaveKind
    demod: PacketDemodulator = None
    reassembler: FrameReassembler = field(default_factory=FrameReassembler)

    def __post_init__(self):
        if self.demod is None:
            self.demod = PacketDemodulator(self.kind)
        self._last_start = None
        self._packet_len = air.PACKET_BITS * self.demod.sps
        self.last_packets: list[AirPacket] = []

    def feed(self, samples: np.ndarray) -> list[DecodedFrame]:
        return self._feed_packets(self.demod.feed(samples))

    def flush(self) -> list[DecodedFrame]:
        return self._feed_packets(self.demod.flush())

    def _feed_packets(self, packets: list[AirPacket]) -> list[DecodedFrame]:
        self.last_packets = packets
        frames = []
        for pkt in packets:
            frames += self.feed_packet(pkt)
        return frames

    def feed_packet(self, pkt: AirPacket) -> list[DecodedFrame]:
        if self._last_start is not None and pkt.start - self._last_start > 1.5 * self._packet_len:
            self.reassembler.reset()
        self._last_start = pkt.start
        return [
            DecodedFrame(f.cmd_id, f.seq, cmds.decode(f.cmd_id, f.data), f.data, pkt.start)
            for f in self.reassembler.feed(pkt.payload)
        ]


@dataclass
class FrequencyTracker:
    """载波频偏跟踪（AFC）。

    锁定时：用每包的直流估计（dc × 标称频偏）慢慢修正前端偏移。
    失锁时（lost_after 个样本内没有可信估计）：在 search 列表里轮流试偏移，每个停留 dwell 个样本。
    NanoSDR 这类 Pluto 兼容板晶振约 ±25 ppm，433 MHz 下收发合计可达 ±20 kHz 以上，
    而接收滤波器为了挡住相邻的强干扰波必须很窄，所以需要这个环节。

    估计只在残余频偏小于约一个符号率时可信：接收低通截止 = 带宽/2 = 频偏 + 符号率，
    残余频偏再大，一个音就落进过渡带被压低，鉴频结果会偏得很远、甚至反号（包可能照样解得出）。
    所以超过 max_estimate_hz 的估计一律不用；只有这种估计时按失锁处理，靠网格搜索把残余拉回可信范围。
    """

    deviation_hz: float
    gain: float = 0.5
    deadband_hz: float = 200.0
    max_estimate_hz: float = 0.9 * phy.SYMBOL_RATE   # ≈19 kHz，超过的估计不可信
    max_step_hz: float = 5e3        # 每次最多修正这么多，防止个别坏包把前端带跑
    search_hz: tuple = (0.0, 15e3, -15e3, 30e3, -30e3, 45e3, -45e3)
    dwell: int = 200_000            # 每个搜索偏移停留 0.2 s（约 20 包）
    lost_after: int = 300_000       # 0.3 s（约 30 包）没有可信估计即视为失锁
    offset_hz: float = 0.0
    locked: bool = False
    _since_packet: int = 0
    _since_step: int = 0
    _search_idx: int = 0
    _base_hz: float = 0.0

    def __post_init__(self):
        self._base_hz = self.offset_hz

    def update(self, packets: list[AirPacket], n_samples: int) -> float:
        """返回新的前端偏移（Hz）。"""
        est = [p.dc * self.deviation_hz for p in packets]
        est = [e for e in est if abs(e) <= self.max_estimate_hz]
        if est:
            self.locked = True
            self._since_packet = 0
            self._since_step = 0
            cfo = float(np.median(est))
            if abs(cfo) > self.deadband_hz:
                self.offset_hz += float(np.clip(self.gain * cfo, -self.max_step_hz, self.max_step_hz))
            self._base_hz = self.offset_hz
            self._search_idx = 0
            return self.offset_hz
        self._since_packet += n_samples
        if self._since_packet < self.lost_after:
            return self.offset_hz
        self.locked = False
        self._since_step += n_samples
        if self._since_step >= self.dwell:
            self._since_step = 0
            self._search_idx = (self._search_idx + 1) % len(self.search_hz)
            self.offset_hz = self._base_hz + self.search_hz[self._search_idx]
        return self.offset_hz


@dataclass
class ChannelReceiver:
    """离线 / 无 GNU Radio 时的单信道接收：前端（numpy）+ 解析 + AFC。

    实时链路里前端在 GNU Radio，AFC 算出的偏移经 XMLRPC 下发，逻辑相同。
    """

    spec: phy.WaveSpec
    offset_hz: float = 0.0
    afc: bool = True

    def __post_init__(self):
        self.frontend = ChannelFrontend(self.spec, self.offset_hz)
        self.decoder = WaveDecoder(self.spec.kind)
        self.tracker = FrequencyTracker(self.spec.deviation_hz, offset_hz=self.offset_hz) if self.afc else None

    def process(self, iq: np.ndarray) -> list[DecodedFrame]:
        frames = self.decoder.feed(self.frontend.process(iq))
        self._track(len(iq))
        return frames

    def flush(self) -> list[DecodedFrame]:
        """流结束：先用零把低通滤波器里延迟的样本推出来，再清空解调缓冲。"""
        tail = self.decoder.feed(self.frontend.process(np.zeros(len(self.frontend.taps), np.complex64)))
        return tail + self.decoder.flush()

    def _track(self, n: int) -> None:
        if self.tracker is not None:
            self.frontend.offset_hz = self.tracker.update(self.decoder.last_packets, n)


def decode_iq(iq: np.ndarray, spec: phy.WaveSpec, offset_hz: float = 0.0, chunk: int = 100_000,
              afc: bool = True) -> tuple[list[DecodedFrame], ChannelReceiver]:
    """离线：复基带 IQ → 帧列表。"""
    rx = ChannelReceiver(spec, offset_hz, afc)
    frames = []
    for i in range(0, len(iq), chunk):
        frames += rx.process(iq[i:i + chunk])
    frames += rx.flush()
    return frames, rx
