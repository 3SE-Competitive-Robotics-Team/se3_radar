"""订阅 boards.yaml 里每块板的遥测（rmwave.telemetry），维护最新状态、近期帧、频谱、电平，
并给出「卡在哪一步」的漏斗诊断和事件流。

线程安全：后台线程收 ZMQ，HTTP 线程读 snapshot() / spectrum()。
"""

import json
import math
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
INFO_CMDS = ("0x0A01", "0x0A02", "0x0A03", "0x0A04", "0x0A05")
PACKETS_PER_S = phy.SAMPLE_RATE / (27 * 8 * phy.SPS)      # 满载约 98.5 包/秒
CLIP_DBFS = -0.5
SNR_OK_DB, SNR_MIN_DB = 10.0, 3.0


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


def band_snr_db(db: np.ndarray, offset_hz: float, bandwidth_hz: float, fs: float = phy.SAMPLE_RATE) -> float:
    """带内平均功率比底噪高多少 dB。底噪取本波带外各点的中位数（带外点太少时取全谱 10% 分位）。"""
    n = len(db)
    f = (np.arange(n) + 0.5) / n * fs - fs / 2
    inside = np.abs(f - offset_hz) <= 0.4 * bandwidth_hz
    outside = (np.abs(f - offset_hz) > 0.6 * bandwidth_hz) & (np.abs(f) < 0.45 * fs)
    if not inside.any():
        return float("nan")
    sig = 10 * np.log10(np.mean(10 ** (db[inside] / 10)))
    noise = np.median(db[outside]) if outside.sum() >= 40 else np.percentile(db, 10)
    return float(sig - noise)


def _stage(key, label, state, value, hint=None):
    return {"key": key, "label": label, "state": state, "value": value, "hint": hint}


def rx_funnel(name: str, running: bool, status: dict, win: dict, level: dict | None, snr: float | None,
              cmds_in_window: set[str]) -> list[dict]:
    """接收板从进程到结果的各级状态。state: ok / warn / bad / idle（没数据）。"""
    st = []
    if not running:
        st.append(_stage("proc", "进程", "bad", "未运行", f"接收进程没在跑：python3 scripts/run_board.py {name}"))
        return st
    st.append(_stage("proc", "进程", "ok", "运行中"))

    stream = status.get("stream") or {}
    ratio, stalls = win.get("stream_ratio"), win.get("stalls") or 0
    idle = stream.get("idle_s")
    if idle is None and (stream.get("wall_s") or 0) < 3:
        st.append(_stage("stream", "采样流", "idle", "等待数据"))
    elif idle is None or idle > 2 or (ratio is not None and ratio < 0.5):
        st.append(_stage("stream", "采样流", "bad", "断流" if idle is not None else "无数据",
                         "数据流断了：板子掉线或 USB 网卡被重置——看事件栏；板子直插电脑、换线"))
    elif stalls or (ratio is not None and ratio < 0.95) or (win.get("overflows") or 0):
        st.append(_stage("stream", "采样流", "warn", f"{(ratio or 0):.2f} MS/s",
                         f"数据流有断档（近 {win['window']:.0f} s {stalls} 次）：USB 链路不稳，会丢包"))
    else:
        st.append(_stage("stream", "采样流", "ok", "统计中" if ratio is None else f"{ratio:.2f} MS/s"))

    if not level:
        st.append(_stage("level", "电平", "idle", "—"))
    elif level["peak_dbfs"] > CLIP_DBFS:
        st.append(_stage("level", "电平", "bad", f"峰值 {level['peak_dbfs']:.1f} dBFS",
                         "输入削顶（过载）：降低接收增益 / 加大发射衰减 / 拉开距离"))
    else:
        st.append(_stage("level", "电平", "ok", f"峰值 {level['peak_dbfs']:.0f} dBFS"))

    if snr is None or not math.isfinite(snr):
        st.append(_stage("snr", "带内信号", "idle", "—"))
    elif snr < SNR_MIN_DB:
        st.append(_stage("snr", "带内信号", "bad", f"{snr:.0f} dB",
                         "带内没有信号：天线是否接在 RX 口、发射端是否在发、阵营 / 等级 / 频点是否对得上"))
    elif snr < SNR_OK_DB:
        st.append(_stage("snr", "带内信号", "warn", f"{snr:.0f} dB", "带内信号偏弱：天线靠近一些或减小发射衰减"))
    else:
        st.append(_stage("snr", "带内信号", "ok", f"{snr:.0f} dB"))

    dt = win.get("dt") or 0
    rate = (lambda k: (win.get(k) or 0) / dt if dt > 0 else None)
    cand, pk, fr = rate("candidates"), rate("packets"), rate("frames")
    if cand is None:
        st.append(_stage("sync", "接入码", "idle", "—"))
    elif cand == 0:
        st.append(_stage("sync", "接入码", "bad", "0 /s",
                         "有数据但找不到接入码：等级或波形不对、频偏太大（看 AFC），或收到的是别的波"))
    else:
        st.append(_stage("sync", "接入码", "ok", f"{cand:.0f} /s"))

    if pk is None:
        st.append(_stage("packets", "空口包", "idle", "—"))
    elif pk == 0:
        st.append(_stage("packets", "空口包", "bad", "0 包/s", "接入码候选全被拒（误码过多）：信号太弱或有干扰，看眼图"))
    elif cand and pk / cand < 0.5:
        st.append(_stage("packets", "空口包", "warn", f"{pk:.0f} 包/s", "大量候选被拒：信号质量差，看眼图和误码分布"))
    else:
        st.append(_stage("packets", "空口包", "ok", f"{pk:.0f} 包/s"))

    crc = (win.get("crc8_fail") or 0) + (win.get("crc16_fail") or 0)
    if fr is None:
        st.append(_stage("frames", "裁判帧", "idle", "—"))
    elif fr == 0:
        st.append(_stage("frames", "裁判帧", "bad" if pk else "idle", "0 帧/s",
                         "空口包能解但拼不出裁判帧：载荷格式不对或误码" if pk else None))
    elif crc:
        st.append(_stage("frames", "裁判帧", "warn", f"{fr:.0f} 帧/s", f"近 {win['window']:.0f} s 有 {crc} 次 CRC 失败：存在误码"))
    else:
        st.append(_stage("frames", "裁判帧", "ok", f"{fr:.0f} 帧/s"))

    wave = status.get("wave") or {}
    if wave.get("kind") == "jam":
        keys = status.get("keys") or []
        fk = status.get("first_key")
        if keys:
            st.append(_stage("result", "密钥", "ok", " ".join(keys)
                             + (f"（{fk['since_data_s']:.1f} s）" if fk else "")))
        else:
            st.append(_stage("result", "密钥", "bad" if fr else "idle", "未解出",
                             "帧在收但没有 0x0A06：确认发射端发的是干扰波" if fr else None))
    else:
        got = len(cmds_in_window & set(INFO_CMDS))
        state = "ok" if got == 5 else ("warn" if got else ("bad" if fr else "idle"))
        st.append(_stage("result", "信息命令", state, f"{got}/5",
                         "部分命令缺失：误码或发射内容不全" if 0 < got < 5 else None))
    return st


def verdict(stages: list[dict]) -> dict:
    for want in ("bad", "warn"):
        for s in stages:
            if s["state"] == want:
                return {"state": want, "stage": s["label"], "text": s["hint"] or f"{s['label']}：{s['value']}"}
    if all(s["state"] == "ok" for s in stages):
        return {"state": "ok", "stage": None, "text": "全链路正常"}
    return {"state": "idle", "stage": None, "text": "等待数据…"}


def tx_health(running: bool, status: dict, win: dict) -> dict:
    fg = status.get("flowgraph") or {}
    if not running:
        code = fg.get("exit_code")
        return {"state": "bad", "text": "发射进程未运行" + (f"（退出码 {code}）" if code not in (None, 0) else "")}
    if win.get("errors"):
        return {"state": "bad", "text": f"流图报错：{fg.get('last_error') or ''}"}
    if win.get("underflows"):
        return {"state": "warn", "text": f"发射欠载 {win['underflows']} 次：USB 带宽不够或主机卡顿，发出去的信号会断续"}
    rate = win.get("packets_rate")
    if rate is not None and rate < 0.9 * PACKETS_PER_S:
        return {"state": "warn", "text": f"发包速率 {rate:.0f} 包/s，低于满载 {PACKETS_PER_S:.0f}"}
    if rate is None:
        return {"state": "idle", "text": "等待数据…"}
    return {"state": "ok", "text": "发射正常"}


class BoardView:
    def __init__(self, name: str, cfg: dict, window: float):
        self.name = name
        self.cfg = cfg
        self.window = window
        self.status: dict | None = None
        self.status_at: float | None = None          # 本地 monotonic 收到时间
        self.history: deque = deque()                # (monotonic, status)
        self.afc: deque = deque(maxlen=360)          # (monotonic, offset_hz, locked)
        self.frames: deque = deque(maxlen=30)
        self.last_by_cmd: dict[str, dict] = {}
        self.cmd_times: dict[str, deque] = {}
        self.spectrum: np.ndarray | None = None
        self.spectrum_at: float | None = None
        self.spectrum_version = 0
        self.level: deque = deque(maxlen=400)        # (monotonic, peak_dbfs, mean_dbfs)
        self.reachable: bool | None = None
        self.prev: dict = {}                         # 事件检测用的上一次状态

    def add_status(self, msg: dict, now: float) -> None:
        self.status, self.status_at = msg, now
        self.history.append((now, msg))
        while self.history and now - self.history[0][0] > self.window + 1:
            self.history.popleft()
        afc = msg.get("afc")
        if afc:
            self.afc.append((now, float(afc.get("offset_hz", 0.0)), bool(afc.get("locked"))))

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

    def add_level(self, peak: float, mean: float, now: float) -> None:
        to_db = (lambda p: 10 * math.log10(p) if p > 0 else -150.0)
        self.level.append((now, to_db(peak), to_db(mean)))

    def _get(self, s: dict, path: tuple):
        v = s
        for k in path:
            if not isinstance(v, dict) or k not in v:
                return None
            v = v[k]
        return v

    RESTART_KEYS = (("seconds",), ("sent_packets",), ("stream", "wall_s"), ("uptime_s",))

    def _since_restart(self, pts: list) -> list:
        """板子进程重启后计数归零：只保留最后一次重启之后的点，否则差值会是负数。"""
        start = 0
        for i in range(1, len(pts)):
            for path in self.RESTART_KEYS:
                a, b = self._get(pts[i - 1][1], path), self._get(pts[i][1], path)
                if a is not None and b is not None and b < a:
                    start = i
                    break
        return pts[start:]

    def window_stats(self, now: float) -> dict:
        pts = self._since_restart([(t, s) for t, s in self.history if now - t <= self.window])
        out = {"window": self.window, "dt": None}
        if len(pts) < 2:
            return out
        (t0, a), (t1, b) = pts[0], pts[-1]
        out["dt"] = t1 - t0

        def d(path):
            x, y = self._get(a, path), self._get(b, path)
            return None if x is None or y is None else float(y) - float(x)

        for key, path in {"candidates": ("demod", "candidates"), "packets": ("demod", "packets"),
                          "rejected_ac": ("demod", "rejected_ac"), "rejected_header": ("demod", "rejected_header"),
                          "frames": ("reassembler", "frames"), "crc8_fail": ("reassembler", "crc8_fail"),
                          "crc16_fail": ("reassembler", "crc16_fail"),
                          "dropped_bytes": ("reassembler", "dropped_bytes"),
                          "stalls": ("stream", "stalls"), "sent_packets": ("sent_packets",),
                          "underflows": ("flowgraph", "underflows"), "overflows": ("flowgraph", "overflows"),
                          "errors": ("flowgraph", "errors"), "seconds": ("seconds",)}.items():
            v = d(path)
            out[key] = None if v is None else int(v) if key != "seconds" else v
        if out["dt"] and out.get("seconds") is not None:
            out["stream_ratio"] = out["seconds"] / out["dt"]
        if out["dt"] and out.get("sent_packets") is not None:
            out["packets_rate"] = out["sent_packets"] / out["dt"]
        return out

    def level_now(self, now: float) -> dict | None:
        recent = [(p, m) for t, p, m in self.level if now - t <= 1.0]
        if not recent:
            return None
        return {"peak_dbfs": max(p for p, _ in recent), "mean_dbfs": 10 * math.log10(
            np.mean([10 ** (m / 10) for _, m in recent]))}

    def view(self, now: float, stale: float) -> dict:
        s = self.status or {}
        running = self.status_at is not None and now - self.status_at <= stale
        win = self.window_stats(now)
        out = {
            "name": self.name, "role": self.cfg.get("role"), "wave_cfg": self.cfg.get("wave"),
            "uri": self.cfg.get("uri"), "reachable": self.reachable, "running": running,
            "status_age": None if self.status_at is None else now - self.status_at,
            "status": s, "win": win,
        }
        wave = s.get("wave")
        if self.cfg.get("role") == "tx":
            out["health"] = tx_health(running, s, win)
            return out

        cmds_in_window = {c for c, q in self.cmd_times.items() if q and now - q[-1] <= self.window}
        level = self.level_now(now)
        snr = None
        if self.spectrum is not None and wave and self.spectrum_at is not None and now - self.spectrum_at <= stale:
            off = (s.get("afc") or {}).get("offset_hz", 0.0)
            snr = band_snr_db(self.spectrum, off, wave["bandwidth_hz"])
        stages = rx_funnel(self.name, running, s, win, level, snr, cmds_in_window)
        out.update({
            "funnel": stages, "verdict": verdict(stages), "level": level, "snr_db": snr,
            "level_series": [[round(t - now, 2), round(p, 1), round(m, 1)] for t, p, m in self.level
                             if now - t <= self.window],
            "afc_series": [[round(t - now, 1), o, lk] for t, o, lk in self.afc if now - t <= 120],
            "markers": wave_markers(wave["center_hz"]) if wave else [],
            "spectrum_age": None if self.spectrum_at is None else now - self.spectrum_at,
            "cmds": [{"cmd": c, "name": CMD_NAMES.get(c, ""), "age": now - it["received"],
                      "rate": len(self.cmd_times.get(c, ())) / self.window, "seq": it.get("seq"),
                      "message": it.get("message")} for c, it in sorted(self.last_by_cmd.items())],
            "recent": [{"cmd": f["cmd"], "name": CMD_NAMES.get(f["cmd"], ""), "seq": f.get("seq"),
                        "age": now - f["received"], "message": f.get("message")} for f in reversed(self.frames)],
        })
        return out


class Aggregator:
    def __init__(self, cfg: dict, ctx: zmq.Context | None = None, window: float = 10.0, stale: float = 3.0,
                 probe_interval: float | None = 3.0):
        self.cfg = cfg
        self.window, self.stale = window, stale
        self.ctx = ctx or zmq.Context.instance()
        self.lock = threading.Lock()
        self.boards: dict[str, BoardView] = {}
        self.events: deque = deque(maxlen=200)
        self._socks: dict[zmq.Socket, tuple[str, str]] = {}
        self.poller = zmq.Poller()
        for name, b in cfg["boards"].items():
            self.boards[name] = BoardView(name, b, window)
            addrs = telemetry.addresses(b)
            self._sub(addrs.status, name, "status")
            if b.get("role") == "rx":
                self._sub(addrs.spectrum, name, "spectrum")
                self._sub(addrs.level, name, "level")
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

    # ---- 事件 ----
    def add_event(self, board: str, level: str, text: str) -> None:
        """外部（进程管理）记事件，线程安全。"""
        with self.lock:
            self._event(board, level, text)

    def _event(self, board: str, level: str, text: str) -> None:
        self.events.append({"t": time.time(), "board": board, "level": level, "text": text})

    def _check_events(self, b: BoardView, now: float) -> None:
        """在锁内调用：和上一次比，状态有变化就记一条事件。"""
        s = b.status or {}
        p = b.prev
        running = b.status_at is not None and now - b.status_at <= self.stale
        if p.get("running") is not None and running != p["running"]:
            fg = s.get("flowgraph") or {}
            code = fg.get("exit_code")
            self._event(b.name, "ok" if running else "bad",
                        "进程启动" if running else "进程停止" + (f"（退出码 {code}）" if code not in (None, 0) else ""))
        p["running"] = running
        if p.get("reachable") is not None and b.reachable is not None and b.reachable != p["reachable"]:
            self._event(b.name, "ok" if b.reachable else "bad", "板子上线（ping 通）" if b.reachable else "板子掉线（ping 不通）")
        if b.reachable is not None:
            p["reachable"] = b.reachable
        afc = s.get("afc")
        if afc is not None:
            if p.get("afc") is None:
                p["afc"] = afc["locked"]
            elif afc["locked"] != p["afc"] and now - p.get("afc_t", -1e9) >= 10:   # 来回跳时 10 s 内只记一条
                self._event(b.name, "ok" if afc["locked"] else "warn",
                            f"AFC {'锁定' if afc['locked'] else '失锁，开始搜索'}（{afc['offset_hz'] / 1e3:+.1f} kHz）")
                p["afc"], p["afc_t"] = afc["locked"], now
        keys = set(s.get("keys") or [])
        for k in sorted(keys - p.get("keys", set())):
            fk = s.get("first_key") or {}
            extra = f"，收到数据后 {fk['since_data_s']:.1f} s" if fk.get("key") == k and "since_data_s" in fk else ""
            self._event(b.name, "ok", f"解出密钥 {k}{extra}")
        p["keys"] = keys | p.get("keys", set())
        fg = s.get("flowgraph") or {}
        for key, level, text in (("errors", "bad", "流图报错：{last}"), ("underflows", "warn", "发射欠载 +{d}"),
                                 ("overflows", "warn", "接收溢出 +{d}")):
            v = fg.get(key)
            if v is None:
                continue
            last_v = p.get(key)
            if last_v is not None and v > last_v and now - p.get(key + "_t", 0) >= 5:
                self._event(b.name, level, text.format(d=v - last_v, last=fg.get("last_error") or ""))
                p[key + "_t"] = now
                p[key] = v
            elif last_v is None or v < last_v:     # 第一次 / 进程重启后计数归零
                p[key] = v
        stalls = (s.get("stream") or {}).get("stalls")
        if stalls is not None:
            if p.get("stalls") is not None and stalls > p["stalls"]:
                self._event(b.name, "warn", f"数据流断档 +{stalls - p['stalls']}（最长 {s['stream']['max_gap_s']:.1f} s）")
            p["stalls"] = stalls
        lv = b.level_now(now)
        clip = lv is not None and lv["peak_dbfs"] > CLIP_DBFS
        if clip and not p.get("clip"):
            self._event(b.name, "bad", f"输入削顶（峰值 {lv['peak_dbfs']:.1f} dBFS）")
        p["clip"] = clip

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
        if kind == "level":
            x = np.frombuffer(raw, dtype=np.float32)
            with self.lock:
                for peak, mean in x[: len(x) // 2 * 2].reshape(-1, 2):
                    b.add_level(float(peak), float(mean), now)
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        with self.lock:
            if msg.get("type") == "status":
                b.add_status(msg, now)
                self._check_events(b, now)
            elif msg.get("type") == "frame":
                b.add_frame(msg, now)

    def tick(self) -> None:
        """定时检查（进程超时停止、ping 变化等不靠新消息触发的事件）。"""
        now = time.monotonic()
        with self.lock:
            for b in self.boards.values():
                self._check_events(b, now)

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
            events = list(reversed(self.events))[:60]
        return {"t": time.time(), "team": self.cfg.get("team"), "jam_level": self.cfg.get("jam_level"),
                "sample_rate": phy.SAMPLE_RATE, "packets_per_s": PACKETS_PER_S, "boards": boards,
                "events": events}

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
            last_tick = 0.0
            while not self._stop.is_set():
                self.poll_once(100)
                if time.monotonic() - last_tick >= 0.5:
                    self.tick()
                    last_tick = time.monotonic()

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
