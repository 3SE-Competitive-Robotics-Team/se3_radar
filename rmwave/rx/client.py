"""接收端 Python 侧：订阅 GNU Radio 接收流图的鉴频输出，解析成帧，并把 AFC 结果写回流图。

用法（与 rmwave.flowgraphs.rx 配对）：
  python3 -m rmwave.rx.client --team red --level 0
  python3 -m rmwave.rx.client --team red --level 1 --show
"""

import argparse
import sys
import time
import xmlrpc.client
from collections import Counter, deque

import numpy as np
import zmq

from .. import phy
from .decoder import DecodedFrame, FrequencyTracker, WaveDecoder

DEFAULT_ZMQ = {"info": "tcp://127.0.0.1:53301", "jam": "tcp://127.0.0.1:53302"}
DEFAULT_CTRL = "http://127.0.0.1:53310"
STALL_S = 0.25          # 两批数据之间超过这么久算一次断流
EYE_SYMBOLS = 24
EYE_KEEP = 1 << 16      # 留最近这么多鉴频样本，给眼图取包
AFC_HOLDOFF = 200_000   # AFC 改偏移后 0.2 s 内的估计不用于修正：流图 + USB 里积压着按旧偏移处理的数据


class StreamReceiver:
    """ZMQ SUB → WaveDecoder；有 control 时 AFC 改动超过 afc_min_step_hz 才写回流图。

    同时记录给调试网页用的诊断：数据流是否断档、每个空口包的质量、眼图快照、第一次解出密钥的用时。
    """

    def __init__(self, spec: phy.WaveSpec, address: str, control_url: str | None = None,
                 offset_hz: float = 0.0, afc_min_step_hz: float = 100.0, ctx: zmq.Context | None = None,
                 afc_holdoff: int = AFC_HOLDOFF):
        self.spec = spec
        self.decoder = WaveDecoder(spec.kind)
        self.tracker = FrequencyTracker(spec.deviation_hz, offset_hz=offset_hz, holdoff=afc_holdoff)
        self.control = xmlrpc.client.ServerProxy(control_url, allow_none=True) if control_url else None
        self.afc_min_step_hz = afc_min_step_hz
        self._applied_hz = offset_hz
        self.ctx = ctx or zmq.Context.instance()
        self.sock = self.ctx.socket(zmq.SUB)
        self.sock.setsockopt(zmq.SUBSCRIBE, b"")
        self.sock.setsockopt(zmq.RCVHWM, 1000)
        self.sock.connect(address)
        self.samples = 0
        self.keys: set[str] = set()   # 解出过的干扰波密钥
        self.started = time.monotonic()
        self.first_data: float | None = None
        self.first_key: dict | None = None
        self.stalls = 0
        self.max_gap_s = 0.0
        self._last_msg: float | None = None
        self._packets: deque = deque(maxlen=300)          # (相关值, 接入码误码, 包头误码, 残余频偏 Hz)
        self._tail = np.zeros(0, np.float32)              # 最近的鉴频样本
        self._tail_end = 0                                # _tail 最后一个样本之后的绝对序号
        self._eye_start: int | None = None                # 最近一个空口包的起点

    def poll(self, timeout_ms: int = 100) -> list[DecodedFrame]:
        """收完当前所有消息并解析。"""
        frames = []
        if not self.sock.poll(timeout_ms):
            return frames
        while True:
            try:
                msg = self.sock.recv(zmq.NOBLOCK)
            except zmq.Again:
                break
            x = np.frombuffer(msg, dtype=np.float32)
            self._note_arrival()
            self.samples += len(x)
            self._keep_tail(x)
            frames += self.decoder.feed(x)
            self._note_packets()
            self._afc(len(x))
        return self._note_keys(frames)

    def flush(self) -> list[DecodedFrame]:
        frames = self.decoder.flush()
        self._note_packets()
        return self._note_keys(frames)

    def _note_keys(self, frames: list[DecodedFrame]) -> list[DecodedFrame]:
        new = {f.message.key for f in frames if f.cmd_id == 0x0A06 and f.message is not None}
        if new and self.first_key is None:
            self.first_key = {"key": sorted(new)[0], "wall_s": time.monotonic() - self.started,
                              "since_data_s": time.monotonic() - (self.first_data or self.started),
                              "stream_s": self.samples / phy.SAMPLE_RATE}
        self.keys.update(new)
        return frames

    # ---- 诊断 ----
    def _note_arrival(self) -> None:
        now = time.monotonic()
        if self._last_msg is None:
            self.first_data = now
        else:
            gap = now - self._last_msg
            self.max_gap_s = max(self.max_gap_s, gap)
            if gap > STALL_S:
                self.stalls += 1
        self._last_msg = now

    def _keep_tail(self, x: np.ndarray) -> None:
        self._tail = np.concatenate([self._tail, x])[-EYE_KEEP:]
        self._tail_end += len(x)

    def _note_packets(self) -> None:
        for p in self.decoder.last_packets:
            self._packets.append((abs(p.corr), p.ac_errors, p.header_errors, p.dc * self.spec.deviation_hz))
            self._eye_start = p.start

    def stream_health(self) -> dict:
        idle = None if self._last_msg is None else time.monotonic() - self._last_msg
        return {"stalls": self.stalls, "max_gap_s": self.max_gap_s, "idle_s": idle,
                "wall_s": time.monotonic() - self.started}

    def packet_quality(self) -> dict:
        if not self._packets:
            return {"n": 0}
        a = np.array(self._packets, dtype=np.float64)
        return {"n": len(a), "corr_med": float(np.median(a[:, 0])), "corr_min": float(a[:, 0].min()),
                "ac_err_hist": np.bincount(a[:, 1].astype(int), minlength=5)[:5].tolist(),
                "hdr_err_hist": np.bincount(a[:, 2].astype(int), minlength=3)[:3].tolist(),
                "cfo_med_hz": float(np.median(a[:, 3])), "cfo_std_hz": float(np.std(a[:, 3]))}

    def eye_snapshot(self) -> dict | None:
        """最近一个空口包开头 EYE_SYMBOLS 个符号的鉴频输出（按符号对齐）；没有包时给最近一段原始输出。"""
        sps = self.decoder.demod.sps
        n = EYE_SYMBOLS * sps
        if len(self._tail) < n:
            return None
        begin = self._tail_end - len(self._tail)
        aligned = self._eye_start is not None and begin <= self._eye_start <= self._tail_end - n
        i = self._eye_start - begin if aligned else len(self._tail) - n
        seg = self._tail[i:i + n]
        return {"sps": sps, "aligned": aligned, "samples": np.round(seg, 3).tolist()}

    def _afc(self, n: int) -> None:
        want = self.tracker.update(self.decoder.last_packets, n)
        if self.control is not None and abs(want - self._applied_hz) >= self.afc_min_step_hz:
            self.control.set_offset(want)
            self._applied_hz = want

    def close(self) -> None:
        self.sock.close(linger=0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team", choices=["red", "blue"], required=True)
    ap.add_argument("--level", type=int, choices=[0, 1, 2, 3], required=True)
    ap.add_argument("--zmq", help="默认信息波 53301 / 干扰波 53302")
    ap.add_argument("--ctrl", default=DEFAULT_CTRL, help="接收流图 XMLRPC 地址；给空字符串关闭 AFC 写回")
    ap.add_argument("--offset", type=float, default=0.0, help="与流图 --offset 一致的初始偏移")
    ap.add_argument("--show", action="store_true", help="打印每一帧")
    args = ap.parse_args(argv)

    spec = phy.wave_spec(phy.Team(args.team), args.level)
    rx = StreamReceiver(spec, args.zmq or DEFAULT_ZMQ[spec.kind.value], args.ctrl or None, args.offset)
    print(f"rx client {spec.kind.value} L{spec.level} {spec.center_hz / 1e6:.2f} MHz", flush=True)
    run(rx, args.show)
    return 0


def run(rx: StreamReceiver, show: bool = False, stop=None, on_frame=None, on_status=None,
        status_interval: float = 0.5) -> None:
    """一直收，每秒打印一行统计；Ctrl-C 或 stop() 为真时退出（退出前把剩余数据收完）。

    on_frame(frame) 每解出一帧调一次；on_status(counts) 每 status_interval 秒调一次（给调试网页发遥测）。
    """
    counts: Counter = Counter()

    def handle(frames):
        for f in frames:
            counts[f"0x{f.cmd_id:04X}"] += 1
            if show:
                print(f"seq={f.seq:3d} 0x{f.cmd_id:04X} {f.message}", flush=True)
            if on_frame is not None:
                on_frame(f)

    def report():
        s = rx.decoder.demod.stats
        lock = "锁定" if rx.tracker.locked else "搜索"
        keys = sorted(rx.keys)
        print(f"[{rx.samples / phy.SAMPLE_RATE:7.1f}s] 包 {s.packets} 帧 {dict(sorted(counts.items()))} "
              f"CRC16失败 {rx.decoder.reassembler.stats.crc16_fail} AFC {lock} {rx.tracker.offset_hz / 1e3:+.1f} kHz"
              + (f" 密钥 {keys}" if keys else ""), flush=True)

    last = last_status = time.monotonic()
    try:
        while stop is None or not stop():
            handle(rx.poll())
            now = time.monotonic()
            if on_status is not None and now - last_status >= status_interval:
                on_status(counts)
                last_status = now
            if now - last >= 1.0:
                report()
                last = now
        for _ in range(5):
            handle(rx.poll(100))
        handle(rx.flush())
        report()
        if on_status is not None:
            on_status(counts)
    except KeyboardInterrupt:
        pass
    rx.close()


if __name__ == "__main__":
    sys.exit(main())
