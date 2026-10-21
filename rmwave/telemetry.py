"""板子进程的遥测：状态、解出的帧、频谱、电平，经 ZMQ PUB 发出，调试网页（rmwave.webui）订阅。

只发不收，网页没开时不影响收发。每块板三个地址，默认由 ctrl 端口推出，也可在 boards.yaml 里写死：
  status:   tcp://127.0.0.1:<ctrl+1000>   JSON 消息（状态 / 帧）
  spectrum: tcp://127.0.0.1:<ctrl+2000>   接收流图直接发的 float32 × 1024（dBFS，FFT 已移中）
  level:    tcp://127.0.0.1:<ctrl+3000>   接收流图直接发的 float32 × 2（每 50 ms：|x|² 峰值、均值）
"""

import dataclasses
import os
import re
import threading
import time
from typing import NamedTuple

import numpy as np
import zmq

from . import phy

STATUS_PORT_OFFSET = 1000
SPECTRUM_PORT_OFFSET = 2000
LEVEL_PORT_OFFSET = 3000
SPECTRUM_SIZE = 1024
LEVEL_FPS = 20


class Addresses(NamedTuple):
    status: str
    spectrum: str
    level: str


def addresses(board: dict) -> Addresses:
    ctrl = int(board["ctrl"])
    return Addresses(
        board.get("status") or f"tcp://127.0.0.1:{ctrl + STATUS_PORT_OFFSET}",
        board.get("spectrum") or f"tcp://127.0.0.1:{ctrl + SPECTRUM_PORT_OFFSET}",
        board.get("level") or f"tcp://127.0.0.1:{ctrl + LEVEL_PORT_OFFSET}",
    )


def to_jsonable(obj):
    """数据类 / 字节 / 集合 / numpy 标量 → 能 json.dumps 的结构。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


def wave_info(spec: phy.WaveSpec, team: str) -> dict:
    return {"kind": spec.kind.value, "team": team, "level": spec.level,
            "center_hz": spec.center_hz, "bandwidth_hz": spec.bandwidth_hz}


class FlowgraphMonitor:
    """读流图子进程的输出：数 gr-iio 打印的 U（发射欠载）/ O（接收溢出），记报错行，其余原样转到终端。"""

    ERROR = re.compile(r"error|unable|fail|broken pipe|traceback|exception|timed out", re.I)

    def __init__(self, proc, echo: bool = True):
        self.proc = proc
        self.echo = echo
        self.underflows = 0
        self.overflows = 0
        self.errors = 0
        self.last_error: str | None = None
        self._buf = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.feed(chunk.decode(errors="replace"))
        self.feed("\n")

    def feed(self, text: str) -> None:
        self._buf += text
        *lines, self._buf = self._buf.split("\n")
        if self._buf and set(self._buf) <= {"U", "O"}:     # 连续的 UUUU 不一定换行
            lines.append(self._buf)
            self._buf = ""
        for line in lines:
            body = line.lstrip("UO")
            if body[:1].islower() and len(body) < len(line):   # "Unable" / "Opening" 的首字母不是欠载
                body = line[len(line) - len(body) - 1:]
            lead = line[:len(line) - len(body)]
            self.underflows += lead.count("U")
            self.overflows += lead.count("O")
            body = body.rstrip()
            if not body:
                continue
            if self.ERROR.search(body):
                self.errors += 1
                self.last_error = body[:200]
            if self.echo:
                print(body, flush=True)

    def status(self) -> dict:
        return {"underflows": self.underflows, "overflows": self.overflows, "errors": self.errors,
                "last_error": self.last_error, "running": self.proc.poll() is None,
                "exit_code": self.proc.poll()}


def rx_status(rx, counts: dict, board: dict, spec: phy.WaveSpec, team: str, flowgraph: dict | None = None,
              with_eye: bool = True) -> dict:
    """rx 为 rmwave.rx.client.StreamReceiver；counts 为各命令累计帧数。"""
    msg = {
        "type": "status", "role": "rx", "t": time.time(), "uri": board.get("uri"),
        "wave": wave_info(spec, team),
        "seconds": rx.samples / phy.SAMPLE_RATE,
        "demod": to_jsonable(rx.decoder.demod.stats),
        "reassembler": to_jsonable(rx.decoder.reassembler.stats),
        "frames": {k: int(v) for k, v in counts.items()},
        "afc": {"locked": bool(rx.tracker.locked), "offset_hz": float(rx.tracker.offset_hz)},
        "keys": sorted(rx.keys),
        "stream": rx.stream_health(),
        "quality": rx.packet_quality(),
        "first_key": rx.first_key,
        "flowgraph": flowgraph,
    }
    if with_eye:
        msg["eye"] = rx.eye_snapshot()
    return to_jsonable(msg)


def tx_status(board: dict, spec: phy.WaveSpec, team: str, sent_packets: int, key: str | None,
              flowgraph: dict | None = None, started: float | None = None) -> dict:
    return {
        "type": "status", "role": "tx", "t": time.time(), "uri": board.get("uri"),
        "wave": wave_info(spec, team),
        "sent_packets": int(sent_packets), "key": key,
        "attenuation_db": float(board.get("attenuation", 70)),
        "uptime_s": None if started is None else time.monotonic() - started,
        "flowgraph": flowgraph,
    }


def frame_event(frame) -> dict:
    """frame 为 rmwave.rx.decoder.DecodedFrame。"""
    return {"type": "frame", "t": time.time(), "cmd": f"0x{frame.cmd_id:04X}", "seq": frame.seq,
            "message": to_jsonable(frame.message), "raw": frame.raw.hex()}


class Publisher:
    """绑定一个 PUB 地址发 JSON；绑定失败（端口被占）时降级为什么都不做，不影响收发。"""

    def __init__(self, address: str, board_name: str, ctx: zmq.Context | None = None):
        self.board = board_name
        self.error: str | None = None
        self.sock = (ctx or zmq.Context.instance()).socket(zmq.PUB)
        self.sock.setsockopt(zmq.SNDHWM, 1000)
        self.sock.setsockopt(zmq.LINGER, 0)
        try:
            self.sock.bind(address)
        except zmq.ZMQError as e:
            self.error = f"{address}: {e}"
            self.sock.close()
            self.sock = None

    def send(self, msg: dict) -> None:
        if self.sock is None:
            return
        msg = dict(msg, board=self.board)
        try:
            self.sock.send_json(msg, zmq.NOBLOCK)
        except zmq.Again:
            pass

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close(linger=0)
            self.sock = None
