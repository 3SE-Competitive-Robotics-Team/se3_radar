"""与 GNU Radio 对齐的 numpy 信号处理。

实时链路里这些步骤由 GNU Radio 完成。这里的实现用于：
- 没有 GNU Radio / 硬件时的软件回环和 CI；
- 离线回放录下来的 IQ；
- 和 GNU Radio 的输出互相校验。

算法对照：
- gaussian_taps    ≙ firdes.gaussian
- gfsk_modulate    ≙ digital.gfsk_mod（高斯整形 + frequency_modulator_fc）
- lowpass_taps     ≙ firdes.low_pass
- quadrature_demod ≙ analog.quadrature_demod_cf
"""

import math

import numpy as np

from . import phy


def bytes_to_bits(data: bytes) -> np.ndarray:
    """MSB 先。"""
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def gaussian_taps(sps: int, bt: float, ntaps: int) -> np.ndarray:
    dt = 1.0 / sps
    s = 1.0 / (math.sqrt(math.log(2.0)) / (2 * math.pi * bt))
    t = (np.arange(ntaps) + 1 - 0.5 * ntaps) * s * dt
    taps = np.exp(-0.5 * t * t)
    return taps / taps.sum()


def gfsk_modulate(
    bits: np.ndarray,
    sensitivity: float,
    sps: int = phy.SPS,
    bt: float = phy.GAUSSIAN_BT,
    span: int = phy.GAUSSIAN_SPAN,
) -> np.ndarray:
    """比特（0/1）→ 复基带。bit 1 对应正频偏。"""
    nrz = np.asarray(bits, dtype=np.float32) * 2 - 1
    impulses = np.zeros(len(nrz) * sps, dtype=np.float32)
    impulses[::sps] = nrz
    pulse = np.convolve(np.ones(sps), gaussian_taps(sps, bt, span * sps))
    shaped = np.convolve(impulses, pulse)[: len(impulses)]
    phase = np.cumsum(sensitivity * shaped)
    return np.exp(1j * phase).astype(np.complex64)


def lowpass_taps(cutoff_hz: float, transition_hz: float, fs: float = phy.SAMPLE_RATE) -> np.ndarray:
    """Blackman 窗低通（约 74 dB 阻带），抽头数按 firdes 的经验公式估计。"""
    ntaps = int(74 * fs / (22 * transition_hz))
    ntaps |= 1
    n = np.arange(ntaps) - (ntaps - 1) / 2
    fc = cutoff_hz / fs
    taps = 2 * fc * np.sinc(2 * fc * n) * np.blackman(ntaps)
    return (taps / taps.sum()).astype(np.float32)


def freq_shift(iq: np.ndarray, shift_hz: float, fs: float = phy.SAMPLE_RATE, start: int = 0) -> np.ndarray:
    """乘 exp(j2π·shift·n/fs)。start 为样本绝对序号，分块处理时保持相位连续。"""
    n = np.arange(start, start + len(iq))
    return (iq * np.exp(2j * np.pi * shift_hz / fs * n)).astype(np.complex64)


def quadrature_demod(iq: np.ndarray, gain: float) -> np.ndarray:
    """gain·arg(x[n]·conj(x[n-1]))，输出比输入少 1 个样本。"""
    return (gain * np.angle(iq[1:] * np.conj(iq[:-1]))).astype(np.float32)


class ChannelFrontend:
    """单个信道：移频 → 低通 → 鉴频。和 GNU Radio 接收流图一致，可分块输入。

    offset_hz 是目标信号相对接收中心频率的偏移，鉴频增益取 1/sensitivity，
    使理想输出约为 ±1。
    """

    def __init__(self, spec: phy.WaveSpec, offset_hz: float = 0.0, transition_hz: float = 20e3,
                 fs: float = phy.SAMPLE_RATE):
        self.offset_hz = offset_hz
        self.fs = fs
        self.gain = 1.0 / spec.sensitivity
        self.taps = lowpass_taps(spec.bandwidth_hz / 2, transition_hz, fs).astype(np.complex64)
        self._n = 0
        self._hist = np.zeros(len(self.taps) - 1, dtype=np.complex64)
        self._last = None

    def process(self, iq: np.ndarray) -> np.ndarray:
        iq = np.asarray(iq, dtype=np.complex64)
        if self.offset_hz:
            iq = freq_shift(iq, -self.offset_hz, self.fs, self._n)
        self._n += len(iq)
        x = np.concatenate([self._hist, iq])
        y = np.convolve(x, self.taps, mode="valid").astype(np.complex64)
        self._hist = x[len(x) - len(self._hist):]
        if self._last is not None:
            y = np.concatenate([[self._last], y])
        if len(y) == 0:
            return np.zeros(0, dtype=np.float32)
        self._last = y[-1]
        return quadrature_demod(y, self.gain)
