"""板子进程的遥测：状态、解出的帧、频谱，经 ZMQ PUB 发出，调试网页（rmwave.webui）订阅。

只发不收，网页没开时不影响收发。每块板两个地址，默认由 ctrl 端口推出，也可在 boards.yaml 里写死：
  status:   tcp://127.0.0.1:<ctrl+1000>   JSON 消息（状态 / 帧）
  spectrum: tcp://127.0.0.1:<ctrl+2000>   接收流图直接发的 float32 向量（dBFS，FFT 已移中）
"""

import dataclasses
import time

import zmq

from . import phy

STATUS_PORT_OFFSET = 1000
SPECTRUM_PORT_OFFSET = 2000
SPECTRUM_SIZE = 1024


def addresses(board: dict) -> tuple[str, str]:
    """(状态地址, 频谱地址)。"""
    ctrl = int(board["ctrl"])
    status = board.get("status") or f"tcp://127.0.0.1:{ctrl + STATUS_PORT_OFFSET}"
    spectrum = board.get("spectrum") or f"tcp://127.0.0.1:{ctrl + SPECTRUM_PORT_OFFSET}"
    return status, spectrum


def to_jsonable(obj):
    """数据类 / 字节 / 集合 → 能 json.dumps 的结构。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    return obj


def wave_info(spec: phy.WaveSpec, team: str) -> dict:
    return {"kind": spec.kind.value, "team": team, "level": spec.level,
            "center_hz": spec.center_hz, "bandwidth_hz": spec.bandwidth_hz}


def rx_status(rx, counts: dict, board: dict, spec: phy.WaveSpec, team: str) -> dict:
    """rx 为 rmwave.rx.client.StreamReceiver；counts 为各命令累计帧数。"""
    demod = rx.decoder.demod.stats
    reasm = rx.decoder.reassembler.stats
    return {
        "type": "status", "role": "rx", "t": time.time(), "uri": board.get("uri"),
        "wave": wave_info(spec, team),
        "seconds": rx.samples / phy.SAMPLE_RATE,
        "demod": to_jsonable(demod),
        "reassembler": to_jsonable(reasm),
        "frames": {k: int(v) for k, v in counts.items()},
        "afc": {"locked": bool(rx.tracker.locked), "offset_hz": float(rx.tracker.offset_hz)},
        "keys": sorted(rx.keys),
    }


def tx_status(board: dict, spec: phy.WaveSpec, team: str, sent_packets: int, key: str | None) -> dict:
    return {
        "type": "status", "role": "tx", "t": time.time(), "uri": board.get("uri"),
        "wave": wave_info(spec, team),
        "sent_packets": int(sent_packets), "key": key,
        "attenuation_db": float(board.get("attenuation", 70)),
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
