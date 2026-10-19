"""软件回环：发射字节流 → GFSK 调制 → 信道（噪声 / 频偏 / 邻道干扰）→ 接收解析。

调制与接收前端用 numpy 实现（与 GNU Radio 算法一致），不需要硬件。
"""

import random
from pathlib import Path

import numpy as np
import pytest

from rmwave import dsp, phy
from rmwave.io.iqfile import read_c64
from rmwave.protocol import air, cmds
from rmwave.rx.decoder import decode_iq
from rmwave.rx.demod import PacketDemodulator
from rmwave.tx import source

RED_INFO = phy.wave_spec(phy.Team.RED, 0)
LEAD_IN = 3000  # 真实流不会正好从包边界开始，前面垫一段噪声


def modulate(kind: air.WaveKind, stream_packets: list[bytes], spec: phy.WaveSpec) -> np.ndarray:
    bits = np.concatenate([dsp.bytes_to_bits(p) for p in stream_packets])
    return dsp.gfsk_modulate(bits, spec.sensitivity)


def info_signal(n_cycles: int, seed: int = 0):
    rng = random.Random(seed)
    states = [source.random_info_state(rng) for _ in range(n_cycles)]
    stream = b"".join(source.info_cycle(s, seq=5 * i) for i, s in enumerate(states))
    iq = modulate(air.WaveKind.INFO, air.packetize(air.WaveKind.INFO, stream), RED_INFO)
    return iq, [m for s in states for m in s.messages()]


def jam_signal(level: int, key: str, n: int):
    spec = phy.wave_spec(phy.Team.RED, level)
    packets = [air.build_packet(air.WaveKind.JAM, source.jam_frame(key, i)) for i in range(n)]
    return modulate(air.WaveKind.JAM, packets, spec), spec


def channel(iq: np.ndarray, snr_db: float, seed: int = 0, lead_in: int = LEAD_IN) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = np.concatenate([np.zeros(lead_in, np.complex64), iq])
    p = 10 ** (-snr_db / 10) / 2
    return (x + (rng.normal(size=len(x)) + 1j * rng.normal(size=len(x))) * np.sqrt(p)).astype(np.complex64)


def test_info_loopback_with_noise():
    iq, expected = info_signal(10)
    frames, _ = decode_iq(channel(iq, snr_db=6), RED_INFO)
    assert [f.message for f in frames] == expected


@pytest.mark.parametrize("level", [1, 2, 3])
def test_jam_key_all_levels(level):
    iq, spec = jam_signal(level, "Ab3dE9", 20)
    frames, _ = decode_iq(channel(iq, snr_db=10), spec)
    assert len(frames) >= 19
    assert {f.message.key for f in frames} == {"Ab3dE9"}


def test_inverted_spectrum():
    iq, expected = info_signal(4)
    frames, _ = decode_iq(np.conj(channel(iq, snr_db=20)), RED_INFO)
    assert [f.message for f in frames] == expected


def test_l3_jammer_400khz_away_40db_stronger():
    """三级干扰离信息波只有 400 kHz，标称功率高 50 dB；接收滤波器必须挡住它。"""
    info, expected = info_signal(10)
    jam, _ = jam_signal(3, "zzzzzz", 120)
    jam = dsp.freq_shift(jam[:len(info)], -400e3)  # 红方：432.8 MHz 对 433.2 MHz
    rx = channel(info, snr_db=30) + np.concatenate([np.zeros(LEAD_IN, np.complex64), jam]) * 100
    frames, _ = decode_iq(rx, RED_INFO)
    assert len(frames) >= 0.95 * len(expected)
    assert all(f.message in expected for f in frames)


@pytest.mark.parametrize("cfo", [30e3, -30e3])
def test_afc_acquires_large_offset(cfo):
    """超过约一个符号率的频偏估计不可信，要靠网格搜索捕获再跟踪（实测约 0.6 s 锁定）。"""
    iq, expected = info_signal(40)
    frames, rx = decode_iq(channel(dsp.freq_shift(iq, cfo), snr_db=15), RED_INFO)
    assert abs(rx.frontend.offset_hz - cfo) < 2e3
    assert len(frames) >= 0.8 * len(expected)


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_cfo_estimate_unbiased(level):
    """没有频偏时估计应接近 0：接入码里孤立位受码间干扰、开头几位受上一包影响，都不能参与估计。"""
    if level == 0:
        iq, _ = info_signal(20)
        spec = RED_INFO
    else:
        iq, spec = jam_signal(level, "Ab3dE9", 200)
    packets = PacketDemodulator(spec.kind).feed(dsp.ChannelFrontend(spec).process(channel(iq, snr_db=40)))
    assert len(packets) > 100
    assert abs(np.median([p.dc for p in packets]) * spec.deviation_hz) < 200


# ---- TCR 公开的赛场实录（官方发射源，独立参照），没下载时跳过 ----

TCR = Path(__file__).resolve().parents[1] / "data/tcr/tcr_rm2026_single_channel_iq_test_pack_v1"
needs_tcr = pytest.mark.skipif(not TCR.exists(), reason="未下载 TCR 赛场 IQ，见 data/README.md")


@needs_tcr
def test_tcr_field_info_wave():
    """录于对方三级干扰期间。TCR 自己的验收线：≥90 帧，0x0A01 ≥18。"""
    frames, rx = decode_iq(read_c64(TCR / "info_wave_2s.c64"), RED_INFO)
    assert abs(rx.frontend.offset_hz) < 5e3  # 实测频偏约 1 kHz，AFC 不能被带跑
    assert len(frames) >= 90
    assert sum(f.cmd_id == cmds.Cmd.POSITION for f in frames) >= 18
    assert all(f.message is not None for f in frames)


@needs_tcr
@pytest.mark.parametrize("name,level,key,min_unique", [
    ("jam_level1_2s.c64", 1, "1SCmbe", 110),   # TCR：110 个不同序号
    ("jam_level2_1s.c64", 2, "2gL8W5", 57),    # TCR：57 个不同序号
])
def test_tcr_field_jam_key(name, level, key, min_unique):
    frames, rx = decode_iq(read_c64(TCR / name), phy.wave_spec(phy.Team.RED, level))
    assert abs(rx.frontend.offset_hz) < 5e3
    assert {f.message.key for f in frames} == {key}
    assert len({f.seq for f in frames}) >= min_unique
