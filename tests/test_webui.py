"""调试网页：遥测地址与消息格式、流图输出解析、漏斗诊断、汇总统计与事件、HTTP / SSE 接口，以及
调制 → 鉴频 → ZMQ → StreamReceiver → 遥测 → 网页 的软件全链路（不需要 GNU Radio 和硬件）。
"""

import json
import socket
import threading
import time
import urllib.request
from types import SimpleNamespace

import numpy as np
import zmq

from rmwave import dsp, phy, telemetry
from rmwave.rx.client import StreamReceiver, run
from rmwave.webui.aggregator import (Aggregator, band_snr_db, board_host, ping, rx_funnel, tx_health, verdict,
                                     wave_markers)
from rmwave.webui.server import WebUI
from test_rx_loopback import channel, jam_signal


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def board_cfg(role="rx", **extra) -> dict:
    b = {"role": role, "wave": "jam", "uri": "ip:192.0.2.1", "zmq": f"tcp://127.0.0.1:{free_port()}",
         "ctrl": free_port(), "status": f"tcp://127.0.0.1:{free_port()}",
         "spectrum": f"tcp://127.0.0.1:{free_port()}", "level": f"tcp://127.0.0.1:{free_port()}"}
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


def status_msg(**kw) -> bytes:
    return json.dumps({"type": "status", **kw}).encode()


# ---------- 遥测 ----------

def test_addresses_default_from_ctrl_and_override():
    assert telemetry.addresses({"ctrl": 53311}) == (
        "tcp://127.0.0.1:54311", "tcp://127.0.0.1:55311", "tcp://127.0.0.1:56311")
    a = telemetry.addresses({"ctrl": 1, "status": "s", "spectrum": "p", "level": "l"})
    assert (a.status, a.spectrum, a.level) == ("s", "p", "l")


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


def test_flowgraph_monitor_counts_underflows_and_errors():
    proc = SimpleNamespace(stdout=None, poll=lambda: None)
    mon = telemetry.FlowgraphMonitor.__new__(telemetry.FlowgraphMonitor)
    mon.proc, mon.echo = proc, False
    mon.underflows = mon.overflows = mon.errors = 0
    mon.last_error, mon._buf = None, ""
    mon.feed("UUmultiply_const_cc :warning: buffer\nUU")
    mon.feed("UO")                                     # 不换行的连续 U / O
    mon.feed("Unable to push buffer: Broken pipe (32)\n")
    mon.feed("Opening device ok\nrx red L0 433.20 MHz\n")
    assert mon.underflows == 1 + 3 and mon.overflows == 1   # "Umultiply" 的 U 算作单词首字母
    assert mon.errors == 1 and mon.last_error.startswith("Unable to push buffer")
    assert mon.status()["running"] is True


def test_board_host_markers_and_ping():
    assert board_host("ip:192.168.4.2") == "192.168.4.2"
    assert board_host("usb:1.2.5") is None and board_host(None) is None
    labels = {m["label"] for m in wave_markers(phy.wave_spec(phy.Team.RED, 0).center_hz)}
    assert labels == {"红信息波", "红干扰 L3"}       # 432.80 在 ±500 kHz 内，433.92 不在
    assert ping("127.0.0.1")
    assert not ping("192.0.2.1")                     # TEST-NET-1，保证不可达


# ---------- 诊断 ----------

def test_band_snr():
    n = telemetry.SPECTRUM_SIZE
    db = np.full(n, -100.0)
    f = (np.arange(n) + 0.5) / n * phy.SAMPLE_RATE - phy.SAMPLE_RATE / 2
    db[np.abs(f - 20e3) < 100e3] = -60.0
    assert abs(band_snr_db(db, 20e3, 250e3) - 40) < 0.5
    assert abs(band_snr_db(np.full(n, -100.0), 0, 540e3)) < 0.5


def healthy_inputs():
    status = {"wave": {"kind": "info", "level": 0}, "stream": {"idle_s": 0.05, "wall_s": 20}}
    win = {"window": 10.0, "dt": 9.5, "stream_ratio": 1.0, "stalls": 0, "candidates": 950, "packets": 940,
           "frames": 470, "crc8_fail": 0, "crc16_fail": 0}
    return status, win, {"peak_dbfs": -12.0, "mean_dbfs": -30.0}, 35.0, {"0x0A01", "0x0A02", "0x0A03", "0x0A04", "0x0A05"}


def test_funnel_healthy_and_failures():
    status, win, level, snr, cmds = healthy_inputs()
    st = rx_funnel("rx_info", True, status, win, level, snr, cmds)
    assert [s["key"] for s in st] == ["proc", "stream", "level", "snr", "sync", "packets", "frames", "result"]
    assert verdict(st)["state"] == "ok"

    v = verdict(rx_funnel("rx_info", False, status, win, level, snr, cmds))
    assert v["state"] == "bad" and v["stage"] == "进程" and "run_board.py rx_info" in v["text"]

    v = verdict(rx_funnel("rx_info", True, status, dict(win, candidates=0, packets=0, frames=0), level, 1.0, set()))
    assert v["stage"] == "带内信号" and "天线" in v["text"]                 # 没信号先报带内，不报接入码

    v = verdict(rx_funnel("rx_info", True, status, win, {"peak_dbfs": -0.1, "mean_dbfs": -3}, snr, cmds))
    assert v["state"] == "bad" and v["stage"] == "电平"

    v = verdict(rx_funnel("rx_info", True, status, dict(win, candidates=0, packets=0, frames=0), level, snr, set()))
    assert v["stage"] == "接入码"

    v = verdict(rx_funnel("rx_info", True, status, dict(win, crc16_fail=4), level, snr, cmds))
    assert v["state"] == "warn" and v["stage"] == "裁判帧"

    v = verdict(rx_funnel("rx_info", True, dict(status, stream={"idle_s": 5.0, "wall_s": 30}), win, level, snr, cmds))
    assert v["stage"] == "采样流"

    jam = dict(status, wave={"kind": "jam", "level": 2}, keys=["Ab3dE9"],
               first_key={"key": "Ab3dE9", "since_data_s": 1.2})
    st = rx_funnel("rx_jam", True, jam, win, level, snr, {"0x0A06"})
    assert st[-1]["state"] == "ok" and "Ab3dE9" in st[-1]["value"]


def test_tx_health():
    full = 98.5
    assert tx_health(False, {}, {})["state"] == "bad"
    assert tx_health(True, {}, {"packets_rate": full})["state"] == "ok"
    assert tx_health(True, {}, {"packets_rate": full, "underflows": 3})["state"] == "warn"
    assert tx_health(True, {"flowgraph": {"last_error": "Broken pipe"}}, {"packets_rate": full, "errors": 1})["state"] == "bad"
    assert tx_health(True, {}, {"packets_rate": 40})["state"] == "warn"


# ---------- 汇总 ----------

def test_aggregator_window_stats_level_and_events():
    cfg = {"boards": {"rx": board_cfg(), "tx": board_cfg("tx")}, "team": "red", "jam_level": 2}
    agg = Aggregator(cfg, probe_interval=None)
    try:
        now = time.monotonic()
        for i in range(10):     # 9 s 内包数 0→90、CRC16 失败 0→3、样本 9 s
            agg._handle("rx", "status", status_msg(
                role="rx", seconds=float(i), demod={"packets": 10 * i, "candidates": 12 * i},
                reassembler={"frames": 10 * i, "crc8_fail": 0, "crc16_fail": i // 3},
                afc={"locked": i >= 5, "offset_hz": -1200.0}, keys=["Ab3dE9"] if i >= 8 else [],
                first_key={"key": "Ab3dE9", "since_data_s": 8.0} if i >= 8 else None,
                stream={"stalls": 0, "idle_s": 0.05, "wall_s": i}), now - 9 + i)
        for i in range(5):
            agg._handle("rx", "frame", json.dumps({"type": "frame", "cmd": "0x0A06", "seq": i,
                                                   "message": {"key": "Ab3dE9"}}).encode(), now - 0.1)
        agg._handle("rx", "level", np.array([0.5, 0.01, 0.25, 0.01], np.float32).tobytes(), now - 0.2)
        agg._handle("tx", "status", status_msg(role="tx", sent_packets=0, flowgraph={"underflows": 0}), now - 5)
        agg._handle("tx", "status", status_msg(role="tx", sent_packets=490, flowgraph={"underflows": 0}), now)

        snap = agg.snapshot()
        rx = next(b for b in snap["boards"] if b["name"] == "rx")
        tx = next(b for b in snap["boards"] if b["name"] == "tx")
        assert rx["running"] and rx["win"]["packets"] == 90 and rx["win"]["crc16_fail"] == 3
        assert abs(rx["win"]["stream_ratio"] - 1.0) < 0.01
        assert rx["cmds"][0]["cmd"] == "0x0A06" and abs(rx["cmds"][0]["rate"] - 0.5) < 1e-9
        assert rx["recent"][0]["seq"] == 4
        assert abs(rx["level"]["peak_dbfs"] - 10 * np.log10(0.5)) < 0.01
        assert len(rx["afc_series"]) == 10 and len(rx["level_series"]) == 2
        assert {s["key"] for s in rx["funnel"]} >= {"proc", "result"}
        assert abs(tx["win"]["packets_rate"] - 98) < 0.1 and tx["health"]["state"] == "ok"
        texts = [e["text"] for e in snap["events"]]
        assert any("AFC 锁定" in t for t in texts)
        assert any(t.startswith("解出密钥 Ab3dE9") and "8.0 s" in t for t in texts)
        json.dumps(snap)
    finally:
        agg.close()


def test_window_stats_ignore_process_restart():
    """进程重启后计数归零，窗口统计只能用重启之后的点，不能算出负速率而误报断流。"""
    cfg = {"boards": {"rx": board_cfg()}}
    agg = Aggregator(cfg, probe_interval=None)
    try:
        now = time.monotonic()
        seq = [(now - 8, 28.0), (now - 6, 30.0), (now - 4, 0.5), (now - 2, 2.5), (now, 4.5)]
        for t, sec in seq:
            agg._handle("rx", "status", status_msg(role="rx", seconds=sec, demod={"packets": int(sec * 98)},
                                                   stream={"stalls": 0, "idle_s": 0.01, "wall_s": sec}), t)
        b = agg.snapshot()["boards"][0]
        assert abs(b["win"]["stream_ratio"] - 1.0) < 0.01 and b["win"]["packets"] >= 0
        assert next(s for s in b["funnel"] if s["key"] == "stream")["state"] == "ok"
    finally:
        agg.close()


def test_aggregator_process_stop_event():
    cfg = {"boards": {"rx": board_cfg()}}
    agg = Aggregator(cfg, probe_interval=None, stale=3.0)
    try:
        agg._handle("rx", "status", status_msg(role="rx", flowgraph={"exit_code": 1}), time.monotonic())
        assert agg.snapshot()["boards"][0]["running"]
        agg.boards["rx"].status_at -= 10          # 模拟 10 s 没消息
        agg.tick()
        snap = agg.snapshot()
        assert not snap["boards"][0]["running"]
        assert snap["events"][0]["level"] == "bad" and "进程停止（退出码 1）" in snap["events"][0]["text"]
        assert snap["boards"][0]["verdict"]["stage"] == "进程"
    finally:
        agg.close()


# ---------- HTTP / SSE ----------

def test_http_endpoints_and_sse():
    cfg = {"boards": {"rx": board_cfg()}, "team": "blue", "jam_level": 1}
    ui = WebUI(cfg, port=0, probe_interval=None)
    ui.start()
    try:
        now = time.monotonic()
        ui.agg._handle("rx", "status", status_msg(role="rx", keys=["ZZ0001"]), now)
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

    board = board_cfg()
    addrs = telemetry.addresses(board)
    cfg = {"boards": {"rx_jam": board}, "team": "red", "jam_level": 2}
    ui = WebUI(cfg, port=0, probe_interval=None)
    ui.start()
    ctx = zmq.Context.instance()
    data_pub = ctx.socket(zmq.PUB)
    data_pub.bind(board["zmq"])
    spec_pub = ctx.socket(zmq.PUB)
    spec_pub.bind(addrs.spectrum)
    level_pub = ctx.socket(zmq.PUB)
    level_pub.bind(addrs.level)
    pub = telemetry.Publisher(addrs.status, "rx_jam")
    rx = StreamReceiver(spec, board["zmq"])
    done = threading.Event()
    t = threading.Thread(target=run, args=(rx,), daemon=True, kwargs=dict(
        stop=done.is_set, status_interval=0.1,
        on_frame=lambda f: pub.send(telemetry.frame_event(f)),
        on_status=lambda counts: pub.send(telemetry.rx_status(rx, counts, board, spec, "red",
                                                              {"underflows": 0, "errors": 0}))))
    try:
        time.sleep(0.5)   # 等 SUB 都连上
        t.start()
        for i in range(0, len(demod), 20_000):
            data_pub.send(demod[i:i + 20_000].astype(np.float32).tobytes())
            time.sleep(0.005)
        spec_pub.send(np.full(telemetry.SPECTRUM_SIZE, -80, np.float32).tobytes())
        level_pub.send(np.array([0.3, 0.02], np.float32).tobytes())

        def ready():
            b = get_json(ui.port, "/api/state")["boards"][0]
            return b["status"].get("keys") == [key] and any(c["cmd"] == "0x0A06" for c in b["cmds"]) \
                and b.get("level") is not None

        assert wait_for(ready), get_json(ui.port, "/api/state")
        snap = get_json(ui.port, "/api/state")
        b = snap["boards"][0]
        s = b["status"]
        assert b["running"] and s["wave"]["level"] == 2
        assert s["reassembler"]["crc16_fail"] == 0
        assert b["recent"][0]["message"] == {"key": key}
        q = s["quality"]
        assert q["n"] >= 30 and sum(q["ac_err_hist"]) == q["n"] and q["corr_med"] > 0.8
        assert abs(q["cfo_med_hz"]) < 2e3
        assert s["eye"]["aligned"] and len(s["eye"]["samples"]) == 24 * phy.SPS
        assert s["first_key"]["key"] == key and s["first_key"]["since_data_s"] < 5
        assert s["stream"]["stalls"] == 0
        assert b["funnel"][-1]["state"] == "ok"
        assert any(e["text"].startswith(f"解出密钥 {key}") for e in snap["events"])
        assert wait_for(lambda: ui.agg.spectrum("rx_jam")[0] is not None)
    finally:
        done.set()
        t.join(timeout=5)
        pub.close()
        for sock in (data_pub, spec_pub, level_pub):
            sock.close(linger=0)
        ui.close()
