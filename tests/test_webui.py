"""调试网页：遥测地址与消息格式、汇总统计、HTTP / SSE 接口，以及
调制 → 鉴频 → ZMQ → StreamReceiver → 遥测 → 网页 的软件全链路（不需要 GNU Radio 和硬件）。
"""

import json
import socket
import threading
import time
import urllib.request

import numpy as np
import zmq

from rmwave import dsp, phy, telemetry
from rmwave.rx.client import StreamReceiver, run
from rmwave.webui.aggregator import Aggregator, board_host, ping, wave_markers
from rmwave.webui.server import WebUI
from test_rx_loopback import channel, jam_signal


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rx_board(**extra) -> dict:
    ctrl = free_port()
    b = {"role": "rx", "wave": "jam", "uri": "ip:192.0.2.1", "zmq": f"tcp://127.0.0.1:{free_port()}", "ctrl": ctrl,
         "status": f"tcp://127.0.0.1:{free_port()}", "spectrum": f"tcp://127.0.0.1:{free_port()}"}
    b.update(extra)
    return b


def get_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------- 遥测 ----------

def test_addresses_default_from_ctrl_and_override():
    assert telemetry.addresses({"ctrl": 53311}) == ("tcp://127.0.0.1:54311", "tcp://127.0.0.1:55311")
    assert telemetry.addresses({"ctrl": 53311, "status": "tcp://127.0.0.1:1", "spectrum": "tcp://127.0.0.1:2"}) == \
        ("tcp://127.0.0.1:1", "tcp://127.0.0.1:2")


def test_publisher_degrades_when_port_taken():
    addr = f"tcp://127.0.0.1:{free_port()}"
    a = telemetry.Publisher(addr, "a")
    b = telemetry.Publisher(addr, "b")
    try:
        assert a.error is None and b.error is not None
        b.send({"type": "status"})   # 不抛异常
    finally:
        a.close()
        b.close()


def test_board_host_and_markers():
    assert board_host("ip:192.168.4.2") == "192.168.4.2"
    assert board_host("usb:1.2.5") is None and board_host(None) is None
    red_info = phy.wave_spec(phy.Team.RED, 0)
    labels = {m["label"] for m in wave_markers(red_info.center_hz)}
    assert labels == {"红信息波", "红干扰 L3"}       # 432.80 在 ±500 kHz 内，433.92 不在


def test_ping_probe():
    assert ping("127.0.0.1")
    assert not ping("192.0.2.1")      # TEST-NET-1，保证不可达
    cfg = {"boards": {"a": rx_board(uri="ip:127.0.0.1"), "b": rx_board(uri="usb:1.2.5")}}
    agg = Aggregator(cfg, probe_interval=None)
    try:
        agg.probe_once()
        reach = {b["name"]: b["reachable"] for b in agg.snapshot()["boards"]}
        assert reach == {"a": True, "b": None}
    finally:
        agg.close()


# ---------- 汇总 ----------

def test_aggregator_window_rates_and_cmds():
    cfg = {"boards": {"rx": rx_board(), "tx": dict(rx_board(), role="tx")}, "team": "red", "jam_level": 2}
    agg = Aggregator(cfg, probe_interval=None)
    try:
        now = time.monotonic()
        for i in range(10):     # 9 s 内包数 0→90、CRC16 失败 0→3
            st = {"type": "status", "role": "rx", "demod": {"packets": 10 * i, "candidates": 12 * i},
                  "reassembler": {"frames": 10 * i, "crc8_fail": 0, "crc16_fail": i // 3},
                  "afc": {"locked": True, "offset_hz": -1200.0}, "keys": []}
            agg._handle("rx", "status", json.dumps(st).encode(), now - 9 + i)
        for i in range(5):
            agg._handle("rx", "frame", json.dumps({"type": "frame", "cmd": "0x0A06", "seq": i,
                                                   "message": {"key": "Ab3dE9"}}).encode(), now - 0.1)
        agg._handle("tx", "status", json.dumps({"type": "status", "role": "tx", "sent_packets": 0}).encode(), now - 5)
        agg._handle("tx", "status", json.dumps({"type": "status", "role": "tx", "sent_packets": 50}).encode(), now)
        snap = agg.snapshot()
        rx = next(b for b in snap["boards"] if b["name"] == "rx")
        tx = next(b for b in snap["boards"] if b["name"] == "tx")
        assert rx["running"] and abs(rx["rates"]["packets"] - 10) < 0.01
        assert rx["window_counts"]["crc16_fail"] == 3
        assert rx["cmds"][0]["cmd"] == "0x0A06" and rx["cmds"][0]["name"] == "干扰密钥"
        assert abs(rx["cmds"][0]["rate"] - 0.5) < 1e-9          # 10 s 窗口里 5 帧
        assert rx["recent"][0]["seq"] == 4                       # 最新的在前
        assert abs(tx["rates"]["packets"] - 10) < 0.01
        json.dumps(snap)
    finally:
        agg.close()


def test_aggregator_stale_status_means_stopped():
    cfg = {"boards": {"rx": rx_board()}}
    agg = Aggregator(cfg, probe_interval=None, stale=3.0)
    try:
        agg._handle("rx", "status", b'{"type":"status","role":"rx"}', time.monotonic() - 10)
        b = agg.snapshot()["boards"][0]
        assert not b["running"] and b["status_age"] > 9
    finally:
        agg.close()


# ---------- HTTP / SSE ----------

def test_http_endpoints_and_sse():
    cfg = {"boards": {"rx": rx_board()}, "team": "blue", "jam_level": 1}
    ui = WebUI(cfg, port=0, probe_interval=None)
    ui.start()
    try:
        now = time.monotonic()
        ui.agg._handle("rx", "status", b'{"type":"status","role":"rx","keys":["ZZ0001"]}', now)
        db = np.linspace(-120, -20, telemetry.SPECTRUM_SIZE).astype(np.float32)
        db[3] = np.nan
        ui.agg._handle("rx", "spectrum", db.tobytes(), now)

        with urllib.request.urlopen(f"http://127.0.0.1:{ui.port}/", timeout=5) as r:
            assert "信息波调试台" in r.read().decode()
        state = get_json(ui.port, "/api/state")
        assert state["team"] == "blue" and state["boards"][0]["status"]["keys"] == ["ZZ0001"]
        spec = get_json(ui.port, "/api/spectrum?board=rx")
        assert len(spec["db"]) == telemetry.SPECTRUM_SIZE and spec["db"][3] == -150.0

        events = set()
        with urllib.request.urlopen(f"http://127.0.0.1:{ui.port}/api/events", timeout=5) as r:
            while len(events) < 2:
                line = r.readline().decode().strip()
                if line.startswith("event: "):
                    events.add(line[7:])
                elif line.startswith("data: "):
                    json.loads(line[6:])
        assert events == {"state", "spectrum"}
    finally:
        ui.close()


# ---------- 软件全链路 ----------

def test_receiver_telemetry_reaches_webui():
    """numpy 调制 + 鉴频代替 GNU Radio，其余与 run_board.py 的接收端一致。"""
    key = "Ab3dE9"
    iq, spec = jam_signal(2, key, 40)
    demod = dsp.ChannelFrontend(spec).process(channel(iq, snr_db=12))

    board = rx_board()
    status_addr, spectrum_addr = telemetry.addresses(board)
    cfg = {"boards": {"rx_jam": board}, "team": "red", "jam_level": 2}
    ui = WebUI(cfg, port=0, probe_interval=None)
    ui.start()
    ctx = zmq.Context.instance()
    data_pub = ctx.socket(zmq.PUB)
    data_pub.bind(board["zmq"])
    spec_pub = ctx.socket(zmq.PUB)
    spec_pub.bind(spectrum_addr)
    pub = telemetry.Publisher(status_addr, "rx_jam")
    rx = StreamReceiver(spec, board["zmq"])
    done = threading.Event()
    t = threading.Thread(target=run, args=(rx,), daemon=True, kwargs=dict(
        stop=done.is_set, status_interval=0.1,
        on_frame=lambda f: pub.send(telemetry.frame_event(f)),
        on_status=lambda counts: pub.send(telemetry.rx_status(rx, counts, board, spec, "red"))))
    try:
        time.sleep(0.5)   # 等 SUB 都连上
        t.start()
        for i in range(0, len(demod), 20_000):
            data_pub.send(demod[i:i + 20_000].astype(np.float32).tobytes())
            time.sleep(0.005)
        spec_pub.send(np.full(telemetry.SPECTRUM_SIZE, -80, np.float32).tobytes())

        def ready():
            b = get_json(ui.port, "/api/state")["boards"][0]
            return b["status"].get("keys") == [key] and any(c["cmd"] == "0x0A06" for c in b["cmds"])

        assert wait_for(ready), get_json(ui.port, "/api/state")
        b = get_json(ui.port, "/api/state")["boards"][0]
        assert b["running"] and b["status"]["wave"]["level"] == 2
        assert b["status"]["reassembler"]["crc16_fail"] == 0
        assert b["recent"][0]["message"] == {"key": key}
        assert wait_for(lambda: ui.agg.spectrum("rx_jam")[0] is not None)
    finally:
        done.set()
        t.join(timeout=5)
        pub.close()
        data_pub.close(linger=0)
        spec_pub.close(linger=0)
        ui.close()
