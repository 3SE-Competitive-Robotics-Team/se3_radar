"""订阅 boards.yaml 里每块板的遥测（rmwave.telemetry），维护最新状态、近期帧、频谱和窗口统计。

线程安全：后台线程收 ZMQ，HTTP 线程读 snapshot() / spectrum()。
"""

import json
import platform
import subprocess
import threading
import time
from collections import deque

import numpy as np
import zmq

from .. import phy, telemetry

CMD_NAMES = {"0x0A01": "位置", "0x0A02": "血量", "0x0A03": "弹量", "0x0A04": "宏观", "0x0A05": "增益",
             "0x0A06": "干扰密钥"}


def board_host(uri: str | None) -> str | None:
    """'ip:192.168.4.2' → '192.168.4.2'；其他形式（usb:、空）返回 None。"""
    if uri and uri.startswith("ip:"):
        return uri[3:].split(":")[0] or None
    return None


def ping(host: str, timeout_s: float = 1.0) -> bool:
    """ICMP ping 一次。不用 TCP 连 iiod 端口判断：开了 TUN 模式代理的电脑上，任何地址的 TCP 连接都会被先接下来。"""
    if platform.system() == "Windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, round(timeout_s))), host]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout_s + 2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wave_markers(center_hz: float, span_hz: float = phy.SAMPLE_RATE) -> list[dict]:
    """频谱图上要标的波：红蓝双方信息波和三级干扰波中，落在 center ± span/2 内的。"""
    out = []
    for team in phy.Team:
        for level in range(4):
            s = phy.wave_spec(team, level)
            off = s.center_hz - center_hz
            if abs(off) <= span_hz / 2:
                name = "信息波" if level == 0 else f"干扰 L{level}"
                out.append({"label": f"{'红' if team.value == 'red' else '蓝'}{name}", "offset_hz": off,
                            "bandwidth_hz": s.bandwidth_hz, "team": team.value, "level": level})
    return out


class BoardView:
    def __init__(self, name: str, cfg: dict, window: float):
        self.name = name
        self.cfg = cfg
        self.window = window
        self.status: dict | None = None
        self.status_at: float | None = None          # 本地 monotonic 收到时间
        self.history: deque = deque()                # (monotonic, status)
        self.frames: deque = deque(maxlen=30)
        self.last_by_cmd: dict[str, dict] = {}
        self.cmd_times: dict[str, deque] = {}
        self.spectrum: np.ndarray | None = None
        self.spectrum_at: float | None = None
        self.spectrum_version = 0
        self.reachable: bool | None = None

    def add_status(self, msg: dict, now: float) -> None:
        self.status, self.status_at = msg, now
        self.history.append((now, msg))
        while self.history and now - self.history[0][0] > self.window + 1:
            self.history.popleft()

    def add_frame(self, msg: dict, now: float) -> None:
        cmd = msg.get("cmd", "?")
        item = dict(msg, received=now)
        self.frames.append(item)
        self.last_by_cmd[cmd] = item
        q = self.cmd_times.setdefault(cmd, deque())
        q.append(now)
        while q and now - q[0] > self.window:
            q.popleft()

    def add_spectrum(self, db: np.ndarray, now: float) -> None:
        self.spectrum, self.spectrum_at = db, now
        self.spectrum_version += 1

    def _delta(self, now: float, path: tuple) -> tuple[float, float] | None:
        """窗口内某计数的增量和时长（按状态里的值求差）。"""
        pts = [(t, s) for t, s in self.history if now - t <= self.window]
        if len(pts) < 2:
            return None

        def get(s):
            v = s
            for k in path:
                if not isinstance(v, dict) or k not in v:
                    return None
                v = v[k]
            return v

        a, b = get(pts[0][1]), get(pts[-1][1])
        if a is None or b is None:
            return None
        return float(b) - float(a), pts[-1][0] - pts[0][0]

    def view(self, now: float, stale: float) -> dict:
        s = self.status or {}
        running = self.status_at is not None and now - self.status_at <= stale
        out = {
            "name": self.name, "role": self.cfg.get("role"), "wave_cfg": self.cfg.get("wave"),
            "uri": self.cfg.get("uri"), "reachable": self.reachable, "running": running,
            "status_age": None if self.status_at is None else now - self.status_at,
            "status": s, "window": self.window,
        }
        rates, diffs = {}, {}
        if s.get("role") == "tx":
            d = self._delta(now, ("sent_packets",))
            rates["packets"] = d[0] / d[1] if d and d[1] > 0 else None
        elif s.get("role") == "rx":
            for key, path in {"packets": ("demod", "packets"), "candidates": ("demod", "candidates"),
                              "rejected_ac": ("demod", "rejected_ac"),
                              "rejected_header": ("demod", "rejected_header"),
                              "frames": ("reassembler", "frames"), "crc8_fail": ("reassembler", "crc8_fail"),
                              "crc16_fail": ("reassembler", "crc16_fail"),
                              "dropped_bytes": ("reassembler", "dropped_bytes")}.items():
                d = self._delta(now, path)
                diffs[key] = d[0] if d else None
                if key in ("packets", "frames"):
                    rates[key] = d[0] / d[1] if d and d[1] > 0 else None
        out["rates"], out["window_counts"] = rates, diffs
        out["cmds"] = [
            {"cmd": cmd, "name": CMD_NAMES.get(cmd, ""), "age": now - item["received"],
             "rate": len(self.cmd_times.get(cmd, ())) / self.window, "seq": item.get("seq"),
             "message": item.get("message")}
            for cmd, item in sorted(self.last_by_cmd.items())
        ]
        out["recent"] = [{"cmd": f["cmd"], "name": CMD_NAMES.get(f["cmd"], ""), "seq": f.get("seq"),
                          "age": now - f["received"], "message": f.get("message")}
                         for f in reversed(self.frames)]
        wave = s.get("wave")
        out["markers"] = wave_markers(wave["center_hz"]) if wave else []
        out["spectrum_age"] = None if self.spectrum_at is None else now - self.spectrum_at
        return out


class Aggregator:
    def __init__(self, cfg: dict, ctx: zmq.Context | None = None, window: float = 10.0, stale: float = 3.0,
                 probe_interval: float | None = 3.0):
        self.cfg = cfg
        self.window, self.stale = window, stale
        self.ctx = ctx or zmq.Context.instance()
        self.lock = threading.Lock()
        self.boards: dict[str, BoardView] = {}
        self._socks: dict[zmq.Socket, tuple[str, str]] = {}
        self.poller = zmq.Poller()
        for name, b in cfg["boards"].items():
            self.boards[name] = BoardView(name, b, window)
            status, spectrum = telemetry.addresses(b)
            self._sub(status, name, "status")
            if b.get("role") == "rx":
                self._sub(spectrum, name, "spectrum")
        self.probe_interval = probe_interval
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _sub(self, addr: str, name: str, kind: str) -> None:
        s = self.ctx.socket(zmq.SUB)
        s.setsockopt(zmq.SUBSCRIBE, b"")
        s.setsockopt(zmq.RCVHWM, 200)
        s.setsockopt(zmq.LINGER, 0)
        if kind == "spectrum":
            s.setsockopt(zmq.CONFLATE, 1)   # 只要最新一帧
        s.connect(addr)
        self._socks[s] = (name, kind)
        self.poller.register(s, zmq.POLLIN)

    # ---- 收 ----
    def poll_once(self, timeout_ms: int = 100) -> int:
        n = 0
        for sock, _ in self.poller.poll(timeout_ms):
            name, kind = self._socks[sock]
            while True:
                try:
                    raw = sock.recv(zmq.NOBLOCK)
                except zmq.Again:
                    break
                n += 1
                self._handle(name, kind, raw, time.monotonic())
        return n

    def _handle(self, name: str, kind: str, raw: bytes, now: float) -> None:
        b = self.boards[name]
        if kind == "spectrum":
            x = np.frombuffer(raw, dtype=np.float32)
            if len(x) >= telemetry.SPECTRUM_SIZE:
                with self.lock:
                    b.add_spectrum(x[-telemetry.SPECTRUM_SIZE:].copy(), now)
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        with self.lock:
            if msg.get("type") == "status":
                b.add_status(msg, now)
            elif msg.get("type") == "frame":
                b.add_frame(msg, now)

    # ---- 板子可达性（ping）----
    def probe_once(self) -> None:
        for b in list(self.boards.values()):
            host = board_host(b.cfg.get("uri"))
            ok = ping(host) if host else None
            with self.lock:
                b.reachable = ok

    # ---- 读 ----
    def snapshot(self) -> dict:
        now = time.monotonic()
        with self.lock:
            boards = [b.view(now, self.stale) for b in self.boards.values()]
        return {"t": time.time(), "team": self.cfg.get("team"), "jam_level": self.cfg.get("jam_level"),
                "sample_rate": phy.SAMPLE_RATE, "boards": boards}

    def spectrum(self, name: str) -> tuple[np.ndarray | None, int]:
        with self.lock:
            b = self.boards.get(name)
            return (None, 0) if b is None else (b.spectrum, b.spectrum_version)

    def spectrum_versions(self) -> dict[str, int]:
        with self.lock:
            return {n: b.spectrum_version for n, b in self.boards.items() if b.spectrum is not None}

    # ---- 线程 ----
    def start(self) -> None:
        def recv_loop():
            while not self._stop.is_set():
                self.poll_once(100)

        def probe_loop():
            while not self._stop.is_set():
                self.probe_once()
                self._stop.wait(self.probe_interval)

        self._threads = [threading.Thread(target=recv_loop, daemon=True)]
        if self.probe_interval:
            self._threads.append(threading.Thread(target=probe_loop, daemon=True))
        for t in self._threads:
            t.start()

    def close(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        for s in self._socks:
            s.close(linger=0)
