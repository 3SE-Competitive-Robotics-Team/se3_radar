"""发射流图：ZMQ（拉模式）空口包符号 → GFSK → NanoSDR / IQ 文件。

调制与 rmwave.dsp.gfsk_modulate 相同：
  ±1 符号 → interp_fir(sps, 矩形*高斯) → frequency_modulator(sensitivity)

数据来源是 zeromq.req_source：流图需要数据时向 Python 发请求（rmwave.tx.server 应答），
发射速率由硬件采样时钟决定，不会断流；各级缓冲区压到最小，Python 改内容后约 0.1 s 上天。

单独运行（每块板一个进程）：
  python3 -m rmwave.flowgraphs.tx --team red --level 0 --uri ip:192.168.2.1
  python3 -m rmwave.flowgraphs.tx --team red --level 1 --out jam.c64 --seconds 2   # 不接硬件，写 IQ 文件
"""

import argparse
import signal
import sys
import time

import numpy as np
from gnuradio import analog, blocks, gr, zeromq
from gnuradio import filter as grfilter

from .. import phy
from ..dsp import gaussian_taps
from . import control

DEFAULT_ZMQ = {0: "tcp://127.0.0.1:53201", 1: "tcp://127.0.0.1:53202", 2: "tcp://127.0.0.1:53202", 3: "tcp://127.0.0.1:53202"}
DEFAULT_CTRL = 53210
BUFFER_ITEMS = 1024


class TxFlowgraph(gr.top_block):
    def __init__(self, spec: phy.WaveSpec, zmq_addr: str, uri: str | None = None, out_file: str | None = None,
                 attenuation_db: float = 70.0, amplitude: float = 0.7, buffer_size: int = 32768,
                 max_samples: int | None = None):
        gr.top_block.__init__(self, "rmwave tx", catch_exceptions=True)
        self.spec = spec

        # Python 直接给 ±1 的 float32 符号（MSB 先）。不传字节是为了压延迟：
        # 拉模式下流图会把各级缓冲区填满，GNU Radio 缓冲区最小一个内存页，
        # 字节流一页 = 4096 B ≈ 151 包 ≈ 1.5 s 空中时间；float 一页只有 1024 符号 ≈ 50 ms。
        src = zeromq.req_source(gr.sizeof_float, 1, zmq_addr, 100, False, -1)
        pulse = np.convolve(np.ones(phy.SPS), gaussian_taps(phy.SPS, phy.GAUSSIAN_BT, phy.GAUSSIAN_SPAN * phy.SPS))
        shaping = grfilter.interp_fir_filter_fff(phy.SPS, pulse.tolist())
        self.fm = analog.frequency_modulator_fc(spec.sensitivity)
        amp = blocks.multiply_const_cc(amplitude)
        for blk in (src, shaping, self.fm, amp):
            blk.set_max_output_buffer(BUFFER_ITEMS)
        self.connect(src, shaping, self.fm, amp)

        tail = amp
        if max_samples is not None:
            head = blocks.head(gr.sizeof_gr_complex, max_samples)
            self.connect(tail, head)
            tail = head

        self.sdr = None
        if out_file:
            self.connect(tail, blocks.file_sink(gr.sizeof_gr_complex, out_file, False))
        else:
            from gnuradio import iio
            # 只开 TX1（与 GRC 生成代码一致：[tx1, tx1, tx2, tx2]）
            self.sdr = iio.fmcomms2_sink_fc32(uri, [True, True, False, False], buffer_size, False)
            self.sdr.set_samplerate(int(phy.SAMPLE_RATE))
            self.sdr.set_bandwidth(int(phy.SAMPLE_RATE))
            self.sdr.set_filter_params("Auto", "", 0, 0)
            self.set_center(spec.center_hz)
            self.set_attenuation(attenuation_db)
            self.connect(tail, self.sdr)

    # ---- XMLRPC 可调 ----
    def set_center(self, hz: float) -> None:
        if self.sdr is not None:
            self.sdr.set_frequency(int(hz))

    def set_attenuation(self, db: float) -> None:
        """NanoSDR 发射衰减 0~89.75 dB。实验室近距离调试建议从大衰减开始。"""
        if self.sdr is not None:
            self.sdr.set_attenuation(0, float(db))

    def set_sensitivity(self, sensitivity: float) -> None:
        self.fm.set_sensitivity(float(sensitivity))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team", choices=["red", "blue"], required=True)
    ap.add_argument("--level", type=int, choices=[0, 1, 2, 3], required=True, help="0=信息波，1~3=干扰波")
    ap.add_argument("--uri", help="NanoSDR 地址，如 ip:192.168.2.1；不给则必须给 --out")
    ap.add_argument("--out", help="不接硬件，把 IQ 写到文件（complex64）")
    ap.add_argument("--seconds", type=float, help="写文件时的时长")
    ap.add_argument("--zmq", help="Python 发射端地址，默认按波形选 53201/53202")
    ap.add_argument("--ctrl-port", type=int, default=DEFAULT_CTRL)
    ap.add_argument("--attenuation", type=float, default=70.0, help="发射衰减 dB")
    args = ap.parse_args(argv)
    if not args.uri and not args.out:
        ap.error("需要 --uri 或 --out")

    spec = phy.wave_spec(phy.Team(args.team), args.level)
    n = int(args.seconds * phy.SAMPLE_RATE) if args.seconds else None
    tb = TxFlowgraph(spec, args.zmq or DEFAULT_ZMQ[args.level], args.uri, args.out, args.attenuation, max_samples=n)
    control.serve(tb, args.ctrl_port, ["set_center", "set_attenuation", "set_sensitivity"])
    print(f"tx {spec.kind.value} L{spec.level} {spec.center_hz / 1e6:.2f} MHz → {args.uri or args.out}", flush=True)

    tb.start()
    if n is not None:
        tb.wait()
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
