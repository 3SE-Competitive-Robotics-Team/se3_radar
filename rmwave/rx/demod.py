"""鉴频输出 → 空口包。

每个包都用 64 bit 接入码重新同步（参考 T-DT），不用定时恢复环：
1. 每个起点 n 对其后一个符号的中段（默认 70%）求均值，得到 D[n]；
2. 用接入码对 D 按符号间隔做相关，归一化后过门限、取局部峰值，即为包起点和最佳采样相位；
3. 在该相位逐符号取 D，用接入码段的高/低电平均值做自适应判决门限（抵消载波频偏造成的直流）；
4. 接入码、头部的错误比特数在容限内才输出 payload。

一个包只有 216 符号，收发两端几十 ppm 的时钟差在一包内累积不到 1 个样本，所以不需要跟踪环。
"""

from dataclasses import dataclass, field

import numpy as np

from .. import phy
from ..dsp import bytes_to_bits
from ..protocol import air

_AC_BITS = 64
_HEADER_BITS = len(air.HEADER) * 8
_PAYLOAD_BITS = air.PAYLOAD_LEN * 8
_PACKET_SYMBOLS = air.PACKET_BITS


@dataclass(frozen=True)
class AirPacket:
    payload: bytes
    start: int            # 包起点的绝对样本序号
    corr: float           # 归一化接入码相关值，负数表示极性反转
    ac_errors: int
    header_errors: int
    dc: float = 0.0       # 判决门限（鉴频输出单位，1 ≙ 标称频偏）；乘以频偏 Hz 即为载波频偏估计


@dataclass
class DemodStats:
    candidates: int = 0
    packets: int = 0
    rejected_ac: int = 0
    rejected_header: int = 0
    inverted: int = 0


@dataclass
class PacketDemodulator:
    kind: air.WaveKind
    sps: int = phy.SPS
    corr_threshold: float = 0.5
    max_ac_errors: int = 4
    max_header_errors: int = 2
    window_fraction: float = 0.7
    stats: DemodStats = field(default_factory=DemodStats)

    def __post_init__(self):
        bits = bytes_to_bits(air.access_code_bytes(self.kind))
        self._ac = bits.astype(bool)
        self._kernel = bits.astype(np.float32) * 2 - 1
        self._header = bytes_to_bits(air.HEADER).astype(bool)
        # 电平 / 频偏估计只用接入码里「前后各一位都相同」的符号：这些符号不受高斯码间干扰，幅度满 ±1。
        # 孤立位幅度会被压低，而接入码里 1 和 0 的连续段分布不对称，全用会让门限偏掉（约标称频偏的 1%）；
        # 开头 GAUSSIAN_SPAN 位还会受上一包末尾数据影响，也不用。
        ac = self._ac
        steady = np.zeros(_AC_BITS, dtype=bool)
        steady[1:-1] = (ac[:-2] == ac[1:-1]) & (ac[1:-1] == ac[2:])
        steady[:phy.GAUSSIAN_SPAN] = False
        self._level_one = steady & ac
        self._level_zero = steady & ~ac
        self._win = max(1, int(round(self.sps * self.window_fraction)))
        self._win_off = (self.sps - self._win) // 2
        self._packet_len = _PACKET_SYMBOLS * self.sps
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = 0          # 缓冲区第一个样本的绝对序号
        self._last_start = -10**18   # 上一个已输出包的起点，防止重叠区重复输出

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._last_start = -10**18

    def flush(self) -> list[AirPacket]:
        """流结束时补几个符号的零，让缓冲区末尾的最后一个包也能被处理。"""
        return self.feed(np.zeros(3 * self.sps, dtype=np.float32))

    def feed(self, samples: np.ndarray) -> list[AirPacket]:
        """输入鉴频输出（float，理想为 ±1），返回本次新解出的包。"""
        self._buf = np.concatenate([self._buf, np.asarray(samples, dtype=np.float32)])
        packets = self._scan()
        keep = self._packet_len + 2 * self.sps
        if len(self._buf) > keep:
            drop = len(self._buf) - keep
            self._buf = self._buf[drop:]
            self._buf_start += drop
        return packets

    def _symbol_means(self) -> np.ndarray:
        c = np.concatenate([[0.0], np.cumsum(self._buf, dtype=np.float64)])
        n = len(self._buf) - self.sps + 1
        if n <= 0:
            return np.zeros(0)
        lo = np.arange(n) + self._win_off
        return (c[lo + self._win] - c[lo]) / self._win

    def _scan(self) -> list[AirPacket]:
        d = self._symbol_means()
        n_cand = len(d) - (_PACKET_SYMBOLS - 1) * self.sps
        if n_cand <= 0:
            return []
        score = np.zeros(n_cand)
        norm = np.zeros(n_cand)
        for b in range(_AC_BITS):
            seg = d[b * self.sps: b * self.sps + n_cand]
            score += self._kernel[b] * seg
            norm += np.abs(seg)
        corr = score / np.maximum(norm, 1e-12)

        packets = []
        for p in self._peaks(np.abs(corr), np.abs(score)):
            start = self._buf_start + p
            # 包与包不会重叠；只有被接受的包才往后跳，被拒的假峰不挡后面的真峰
            if start < self._last_start + self._packet_len - 2 * self.sps:
                continue
            self.stats.candidates += 1
            pkt = self._slice(d, p, corr[p], start)
            if pkt is not None:
                packets.append(pkt)
                self._last_start = start
        return packets

    def _peaks(self, mag: np.ndarray, strength: np.ndarray) -> list[int]:
        """归一化相关过门限的样本按间隔分组（间隔超过半个符号即为新组），按时间排序。

        归一化相关只看符号是否一致，在峰附近会饱和成一段平顶 1.0，取它的最大值会落在眼图边缘；
        所以组内用未归一化的相关能量（在眼图中心最大）选采样相位。
        碰到候选区末尾的组可能还没到峰顶，留给下一次（尾部数据会保留）。
        """
        idx = np.flatnonzero(mag > self.corr_threshold)
        if len(idx) == 0:
            return []
        groups = np.split(idx, np.flatnonzero(np.diff(idx) > self.sps // 2) + 1)
        if groups[-1][-1] == len(mag) - 1:
            groups = groups[:-1]
        return [int(g[np.argmax(strength[g])]) for g in groups]

    def _slice(self, d: np.ndarray, p: int, corr: float, start: int) -> AirPacket | None:
        vals = d[p + np.arange(_PACKET_SYMBOLS) * self.sps]
        if corr < 0:
            vals = -vals
            self.stats.inverted += 1
        ac_vals = vals[:_AC_BITS]
        threshold = (ac_vals[self._level_one].mean() + ac_vals[self._level_zero].mean()) / 2
        bits = vals > threshold
        ac_err = int(np.count_nonzero(bits[:_AC_BITS] != self._ac))
        if ac_err > self.max_ac_errors:
            self.stats.rejected_ac += 1
            return None
        hdr_err = int(np.count_nonzero(bits[_AC_BITS:_AC_BITS + _HEADER_BITS] != self._header))
        if hdr_err > self.max_header_errors:
            self.stats.rejected_header += 1
            return None
        payload = np.packbits(bits[_AC_BITS + _HEADER_BITS:]).tobytes()
        self.stats.packets += 1
        dc = float(threshold) if corr >= 0 else -float(threshold)
        return AirPacket(payload, start, float(corr), ac_err, hdr_err, dc)
