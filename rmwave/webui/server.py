"""HTTP 服务（只用标准库）：静态页面 + JSON 接口 + SSE 实时推送；带进程管理时还能控制各块板。

  GET  /                  调试页面
  GET  /api/state         全量状态快照（带进程管理时含 control）
  GET  /api/spectrum?board=rx_info   某块板最新一帧频谱
  GET  /api/events        SSE：state 每 0.5 s 一次，spectrum 每块板最多 10 帧/秒
  POST /api/control       控制（只收 Content-Type: application/json，挡住别的网页跨站提交），见 Supervisor.control
"""

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from .aggregator import Aggregator
from .supervisor import Supervisor

STATIC = Path(__file__).resolve().parent / "static"
STATE_INTERVAL = 0.5
SPECTRUM_INTERVAL = 0.1


def _clean(obj):
    """NaN / inf 不是合法 JSON，换成 null。"""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def dumps(obj) -> str:
    return json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"))


def spectrum_payload(name: str, db: np.ndarray) -> dict:
    db = np.nan_to_num(db, nan=-150.0, neginf=-150.0, posinf=0.0)
    return {"board": name, "db": np.round(db, 1).tolist()}


def make_handler(agg: Aggregator, stop: threading.Event, sup: Supervisor | None = None):
    def snapshot() -> dict:
        snap = agg.snapshot()
        if sup is not None:
            ctl = sup.state()
            snap["control"] = ctl
            snap["team"], snap["jam_level"] = ctl["settings"]["team"], ctl["settings"]["jam_level"]
        return snap

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):   # 不刷终端
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self._send(200, dumps(snapshot()).encode(), "application/json; charset=utf-8")
            elif url.path == "/api/spectrum":
                name = parse_qs(url.query).get("board", [""])[0]
                db, _ = agg.spectrum(name)
                if db is None:
                    self._send(404, b'{"error":"no spectrum"}', "application/json")
                else:
                    self._send(200, dumps(spectrum_payload(name, db)).encode(), "application/json; charset=utf-8")
            elif url.path == "/api/events":
                self._events()
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if urlparse(self.path).path != "/api/control":
                self._send(404, b"not found", "text/plain")
                return
            if sup is None:
                self._send(403, dumps({"ok": False, "error": "只读模式：用 scripts/radar.py 启动才能控制"}).encode(),
                           "application/json; charset=utf-8")
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._send(415, dumps({"ok": False, "error": "只收 application/json"}).encode(), "application/json")
                return
            try:
                length = min(int(self.headers.get("Content-Length", 0)), 4096)
                req = json.loads(self.rfile.read(length) or b"{}")
                msg = sup.control(req)
                self._send(200, dumps({"ok": True, "message": msg}).encode(), "application/json; charset=utf-8")
            except (ValueError, TypeError) as e:
                self._send(400, dumps({"ok": False, "error": str(e)}).encode(), "application/json; charset=utf-8")

        def _event(self, name: str, data) -> None:
            self.wfile.write(f"event: {name}\ndata: {dumps(data)}\n\n".encode())
            self.wfile.flush()

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            sent: dict[str, int] = {}
            last_state = 0.0
            try:
                while not stop.is_set():
                    now = time.monotonic()
                    if now - last_state >= STATE_INTERVAL:
                        self._event("state", snapshot())
                        last_state = now
                    for name, ver in agg.spectrum_versions().items():
                        if sent.get(name) != ver:
                            db, ver = agg.spectrum(name)
                            if db is not None:
                                self._event("spectrum", spectrum_payload(name, db))
                                sent[name] = ver
                    stop.wait(SPECTRUM_INTERVAL)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    return Handler


class WebUI:
    def __init__(self, cfg: dict, host: str = "127.0.0.1", port: int = 8800, supervisor: Supervisor | None = None,
                 **agg_kwargs):
        self.agg = Aggregator(cfg, **agg_kwargs)
        self.sup = supervisor
        if supervisor is not None:
            supervisor.on_event = self.agg.add_event
        self.stop = threading.Event()
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.agg, self.stop, supervisor))
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self, autostart: bool = False) -> None:
        self.agg.start()
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        if self.sup is not None:
            self.sup.start(autostart)

    def close(self) -> None:
        self.stop.set()
        if self.sup is not None:
            self.sup.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.agg.close()
