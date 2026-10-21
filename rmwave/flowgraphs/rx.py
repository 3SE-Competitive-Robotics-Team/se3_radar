"""接收流图：NanoSDR / IQ 文件 → 移频低通 → 鉴频 → ZMQ PUB（float32）。

与 rmwave.dsp.ChannelFrontend 逐步对应（那边已在回环和 TCR 赛场实录上验证）：
  freq_xlating_fir_filter_ccc(Blackman 低通，截止 = 带宽/2，过渡带 20 kHz，中心 = offset)
  → quadrature_demod_cf(1/sensitivity)

offset 由 Python 侧 AFC 通过 XMLRPC 的 set_offset 实时更新。

给调试网页另开两路（给了地址才开）：
  spectrum：原始 IQ 每 1024 点取一段 → 加窗 FFT（移中）→ dBFS → ZMQ PUB（float32 × 1024），约 20 帧/秒；
            满幅单音约为 0 dBFS。
  level：   原始 IQ 的 |x|²，每 50 ms 一组 [峰值, 均值] → ZMQ PUB（float32 × 2）。
            满幅（AD9363 12 位）对应 1.0，峰值接近 1 即削顶。

单独运行（每块板一个进程）：
  python3 -m rmwave.flowgraphs.rx --team red --level 0 --uri ip:192.168.4.1
  python3 -m rmwave.flowgraphs.rx --team red --level 1 --file data/x.c64      # 不接硬件，回放 IQ 文件
"""

import argparse
import math
import signal
import sys
import time

from gnuradio import analog, blocks, fft, gr, zeromq
from gnuradio import filter as grfilter
from gnuradio.fft import window

from .. import phy
from ..dsp import lowpass_taps
from ..telemetry import LEVEL_FPS, SPECTRUM_SIZE
from . import control

DEFAULT_ZMQ = {"info": "tcp://127.0.0.1:53301", "jam": "tcp://127.0.0.1:53302"}
DEFAULT_CTRL = 53310
TRANSITION_HZ = 20e3
SPECTRUM_FPS = 20


class RxFlowgraph(gr.top_block):
    def __init__(self, spec: phy.WaveSpec, zmq_addr: str, uri: str | None = None, in_file: str | None = None,
                 offset_hz: float = 0.0, gain_mode: str = "slow_attack", gain_db: float = 50.0,
                 record_file: str | None = None, buffer_size: int = 65536, throttle: bool = True,
                 spectrum_addr: str | None = None, level_addr: str | None = None):
        gr.top_block.__init__(self, "rmwave rx", catch_exceptions=True)
        self.spec = spec
        self.sdr = None

        if in_file:
            src = blocks.file_source(gr.sizeof_gr_complex, in_file, False)
            if throttle:
                thr = blocks.throttle(gr.sizeof_gr_complex, phy.SAMPLE_RATE, True)
                self.connect(src, thr)
                src = thr
        else:
            from gnuradio import iio
            # 只开 RX1（与 GRC 生成代码一致：[rx1, rx1, rx2, rx2]）
            self.sdr = iio.fmcomms2_source_fc32(uri, [True, True, False, False], buffer_size)
            self.sdr.set_samplerate(int(phy.SAMPLE_RATE))
            self.sdr.set_quadrature(True)
            self.sdr.set_rfdc(True)
            self.sdr.set_bbdc(True)
            self.sdr.set_filter_params("Auto", "", 0, 0)
            self.set_center(spec.center_hz)
            self.set_gain(gain_mode, gain_db)
            src = self.sdr

        taps = lowpass_taps(spec.bandwidth_hz / 2, TRANSITION_HZ).tolist()
        self.xlate = grfilter.freq_xlating_fir_filter_ccc(1, taps, offset_hz, phy.SAMPLE_RATE)
        self.demod = analog.quadrature_demod_cf(1.0 / spec.sensitivity)
        self.sink = zeromq.pub_sink(gr.sizeof_float, 1, zmq_addr, 100, False, -1)
        self.connect(src, self.xlate, self.demod, self.sink)
        if record_file:
            self.connect(src, blocks.file_sink(gr.sizeof_gr_complex, record_file, False))
        if spectrum_addr:
            self._connect_spectrum(src, spectrum_addr)
        if level_addr:
            self._connect_level(src, level_addr)

    def _connect_level(self, src, addr: str) -> None:
        n = int(phy.SAMPLE_RATE / LEVEL_FPS)
        mag2 = blocks.complex_to_mag_squared(1)
        peak = blocks.max_ff(n, 1)
        mean = blocks.integrate_ff(n, 1)
        scale = blocks.multiply_const_ff(1.0 / n)
        both = blocks.interleave(gr.sizeof_float, 1)
        self.connect(src, mag2)
        self.connect(mag2, blocks.stream_to_vector(gr.sizeof_float, n), peak, (both, 0))
        self.connect(mag2, mean, scale, (both, 1))
        self.connect(both, blocks.stream_to_vector(gr.sizeof_float, 2),
                     zeromq.pub_sink(gr.sizeof_float, 2, addr, 100, False, -1))

    def _connect_spectrum(self, src, addr: str) -> None:
        n = SPECTRUM_SIZE
        win = window.blackmanharris(n)
        keep = max(1, round(phy.SAMPLE_RATE / n / SPECTRUM_FPS))
        self.connect(src,
                     blocks.stream_to_vector(gr.sizeof_gr_complex, n),
                     blocks.keep_one_in_n(gr.sizeof_gr_complex * n, keep),
                     fft.fft_vcc(n, True, win, True, 1),
                     blocks.complex_to_mag_squared(n),
                     blocks.nlog10_ff(10, n, -20 * math.log10(sum(win))),
                     zeromq.pub_sink(gr.sizeof_float, n, addr, 100, False, -1))

    # ---- XMLRPC 可调 ----
    def set_offset(self, hz: float) -> None:
        self.xlate.set_center_freq(float(hz))

    def get_offset(self) -> float:
        return self.xlate.center_freq()

    def set_center(self, hz: float) -> None:
        if self.sdr is not None:
            self.sdr.set_frequency(int(hz))

    def set_gain(self, mode: str, db: float = 50.0) -> None:
        """mode: manual / slow_attack / fast_attack / hybrid。manual 时 db 生效。"""
        if self.sdr is not None:
            self.sdr.set_gain_mode(0, mode)
            if mode == "manual":
                self.sdr.set_gain(0, float(db))

    def set_wave(self, team: str, level: int) -> None:
        """切换接收目标（例如干扰等级变了）：改中心频率、低通、鉴频增益，偏移清零。"""
        spec = phy.wave_spec(phy.Team(team), int(level))
        self.spec = spec
        self.xlate.set_taps(lowpass_taps(spec.bandwidth_hz / 2, TRANSITION_HZ).tolist())
        self.demod.set_gain(1.0 / spec.sensitivity)
        self.set_offset(0.0)
        self.set_center(spec.center_hz)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team", choices=["red", "blue"], required=True)
    ap.add_argument("--level", type=int, choices=[0, 1, 2, 3], required=True, help="0=信息波，1~3=干扰波")
    ap.add_argument("--uri", help="NanoSDR 地址，如 ip:192.168.4.1；不给则必须给 --file")
    ap.add_argument("--file", help="不接硬件，回放 IQ 文件（complex64，1 MS/s）")
    ap.add_argument("--zmq", help="鉴频输出发布地址，默认信息波 53301 / 干扰波 53302")
    ap.add_argument("--ctrl-port", type=int, default=DEFAULT_CTRL)
    ap.add_argument("--offset", type=float, default=0.0, help="初始信道偏移 Hz（板子频偏校准值）")
    ap.add_argument("--gain-mode", default="slow_attack", choices=["manual", "slow_attack", "fast_attack", "hybrid"])
    ap.add_argument("--gain", type=float, default=50.0, help="manual 模式下的接收增益 dB")
    ap.add_argument("--record", help="同时把原始 IQ 录到这个文件")
    ap.add_argument("--spectrum", help="频谱发布地址（给调试网页），不给则不算频谱")
    ap.add_argument("--level-pub", help="电平发布地址（给调试网页），不给则不算")
    args = ap.parse_args(argv)
    if not args.uri and not args.file:
        ap.error("需要 --uri 或 --file")

    spec = phy.wave_spec(phy.Team(args.team), args.level)
    tb = RxFlowgraph(spec, args.zmq or DEFAULT_ZMQ[spec.kind.value], args.uri, args.file, args.offset,
                     args.gain_mode, args.gain, args.record, spectrum_addr=args.spectrum,
                     level_addr=args.level_pub)
    control.serve(tb, args.ctrl_port, ["set_offset", "get_offset", "set_center", "set_gain", "set_wave"])
    print(f"rx {spec.kind.value} L{spec.level} {spec.center_hz / 1e6:.2f} MHz ← {args.uri or args.file}", flush=True)

    tb.start()
    if args.file:
        tb.wait()
        time.sleep(0.5)  # 让 ZMQ 把最后一批发出去
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
    tb.stop()
    tb.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
