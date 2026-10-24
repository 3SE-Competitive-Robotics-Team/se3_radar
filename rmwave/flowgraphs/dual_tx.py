"""实验室用：一块板同时发信息波和一路干扰波，干扰波比信息波强 ratio_db（比赛时约 50 dB）。

  python3 scripts/lab_dual_tx.py --uri ip:192.168.2.1            # 红方信息波 + L3，干扰强 50 dB

两路各自 ZMQ（拉模式）取符号 → GFSK（1 MS/s）→ 上采样到 4 MS/s → 移到相对信息波的频点 → 按功率比加权相加 → NanoSDR。

发射本振放在信息波中心，只把干扰波数字移开：AD936x 发射会在本振频率漏出一个单音，放在两路中间时
它正好落进信息波的频带，信息波就解不出来了（实测）；放在信息波中心，和单路发射一样，漏出的单音
落在接收端的直流处，被接收芯片的直流校正去掉。为了把相距 0.4~1.0 MHz 的干扰波（L1 合起来跨约 2.9 MHz）也装下，发射用 4 MS/s。
写文件时也是 4 MS/s、中心为信息波频点（离线解码前要先降到 1 MS/s）。
"""

import numpy as np
from gnuradio import analog, blocks, gr, zeromq
from gnuradio import filter as grfilter

from .. import phy
from ..dsp import gaussian_taps
from .tx import BUFFER_ITEMS

TX_RATE = 4_000_000
MAX_SPAN_HZ = 0.8 * TX_RATE


def plan(team: phy.Team, level: int) -> tuple[phy.WaveSpec, phy.WaveSpec, float]:
    """(信息波, 干扰波, 发射中心频率 = 信息波中心)。两路放不进发射带宽时抛 ValueError。"""
    info, jam = phy.wave_spec(team, 0), phy.wave_spec(team, level)
    edges = [abs(s.center_hz - info.center_hz) + s.bandwidth_hz / 2 for s in (info, jam)]
    if 2 * max(edges) > MAX_SPAN_HZ:
        raise ValueError(f"信息波和 L{level} 干扰波放不进 {TX_RATE / 1e6:.1f} MS/s 的发射带宽")
    return info, jam, info.center_hz


class DualTxFlowgraph(gr.top_block):
    def __init__(self, team: phy.Team, level: int, info_addr: str, jam_addr: str, uri: str | None = None,
                 out_file: str | None = None, ratio_db: float = 50.0, attenuation_db: float = 20.0,
                 amplitude: float = 0.7, buffer_size: int = 32768, max_samples: int | None = None):
        gr.top_block.__init__(self, "rmwave dual tx", catch_exceptions=True)
        self.info, self.jam, self.center_hz = plan(team, level)
        rel = 10 ** (-ratio_db / 20)
        jam_amp = amplitude / (1 + rel)          # 两路峰值相加不超过 amplitude
        pulse = np.convolve(np.ones(phy.SPS), gaussian_taps(phy.SPS, phy.GAUSSIAN_BT, phy.GAUSSIAN_SPAN * phy.SPS))
        add = blocks.add_cc(1)
        for i, (spec, addr, amp) in enumerate(((self.info, info_addr, jam_amp * rel), (self.jam, jam_addr, jam_amp))):
            src = zeromq.req_source(gr.sizeof_float, 1, addr, 100, False, -1)
            shaping = grfilter.interp_fir_filter_fff(phy.SPS, pulse.tolist())
            fm = analog.frequency_modulator_fc(spec.sensitivity)
            mul = blocks.multiply_const_cc(amp)
            up = grfilter.rational_resampler_ccc(4, 1)                      # 1 → 4 MS/s
            rot = blocks.rotator_cc(2 * np.pi * (spec.center_hz - self.center_hz) / TX_RATE)
            for blk in (src, shaping, fm, mul):
                blk.set_max_output_buffer(BUFFER_ITEMS)
            self.connect(src, shaping, fm, mul, up, rot, (add, i))

        tail = add
        if max_samples is not None:
            head = blocks.head(gr.sizeof_gr_complex, max_samples)
            self.connect(tail, head)
            tail = head
        self.sdr = None
        if out_file:
            self.connect(tail, blocks.file_sink(gr.sizeof_gr_complex, out_file, False))
        else:
            from gnuradio import iio
            self.sdr = iio.fmcomms2_sink_fc32(uri, [True, True, False, False], buffer_size, False)
            self.sdr.set_samplerate(TX_RATE)
            self.sdr.set_bandwidth(int(MAX_SPAN_HZ))
            self.sdr.set_filter_params("Auto", "", 0, 0)
            self.sdr.set_frequency(int(self.center_hz))
            self.sdr.set_attenuation(0, float(attenuation_db))
            self.connect(tail, self.sdr)
