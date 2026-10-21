"""调试台的进程管理与控制接口：命令行拼装、启停、崩溃自动重启、全局切换只重启受影响的板、
参数校验、衰减 / 增益经 XMLRPC 实时下发、HTTP 控制接口。板子进程用假进程代替，不需要 GNU Radio。"""

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from xmlrpc.server import SimpleXMLRPCServer

import pytest

from rmwave.webui.server import WebUI
from rmwave.webui.supervisor import Supervisor

FAKE = [sys.executable, "-u", "-c",
        "import sys, time; print('fake', *sys.argv[1:], flush=True); time.sleep(float(sys.argv[1]))"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_cfg() -> dict:
    def b(role, wave):
        return {"role": role, "wave": wave, "uri": "ip:192.0.2.1", "zmq": f"tcp://127.0.0.1:{free_port()}",
                "ctrl": free_port(), "status": f"tcp://127.0.0.1:{free_port()}",
                "spectrum": f"tcp://127.0.0.1:{free_port()}", "level": f"tcp://127.0.0.1:{free_port()}",
                "attenuation": 70, "gain_mode": "slow_attack"}
    return {"boards": {"tx_info": b("tx", "info"), "tx_jam": b("tx", "jam"), "rx_info": b("rx", "info"),
                       "rx_jam": b("rx", "jam")}, "team": "red", "jam_level": 1}


def fake(seconds=30.0):
    return lambda bp: FAKE + [str(seconds), bp.name]


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def sup():
    events = []
    s = Supervisor(make_cfg(), "config/boards.yaml", command_fn=fake(), on_event=lambda *e: events.append(e))
    s.events = events
    yield s
    s.close()


def test_run_board_command_line():
    s = Supervisor(make_cfg(), "cfg.yaml")
    s.settings.update(team="blue", jam_level=3)
    s.boards["tx_jam"].settings["key"] = "Ab3dE9"
    tx = s.boards["tx_jam"].run_board_command()
    assert tx[3:] == ["tx_jam", "--config", "cfg.yaml", "--team", "blue", "--level", "3", "--attenuation", "70.0",
                      "--key", "Ab3dE9"]
    rx = s.boards["rx_info"].run_board_command()
    assert "--level" not in rx and rx[-4:] == ["--gain-mode", "slow_attack", "--gain", "50.0"]
    s2 = Supervisor(make_cfg(), "cfg.yaml", sim_dir="data/sim")
    assert s2.boards["rx_jam"].run_board_command()[-2:] == ["--sim", "data/sim/rx_jam.c64"]


def test_start_stop_and_log(sup):
    b = sup.boards["rx_info"]
    sup.control({"action": "start", "board": "rx_info"})
    assert b.running and wait_for(lambda: any(line.startswith("fake 30.0 rx_info") for line in b.log))
    sup.control({"action": "stop", "board": "rx_info"})
    assert not b.running and not b.want_running
    st = sup.state()["boards"]["rx_info"]
    assert st["running"] is False and st["log"][-1].startswith("[调试台] 已停止")


def test_crash_is_restarted_with_backoff():
    events = []
    s = Supervisor(make_cfg(), "x", command_fn=lambda bp: [sys.executable, "-c", "import sys; sys.exit(3)"],
                   on_event=lambda *e: events.append(e))
    try:
        b = s.boards["rx_jam"]
        b.start()
        assert wait_for(lambda: b.proc.poll() is not None)
        assert b.check(time.monotonic()) is not None and b.exit_code == 3
        assert "1 s 后重启" in b.log[-1]
        b._next_try = 0
        b.check(time.monotonic())
        assert b.restarts == 1 and b.exit_code is None          # 已重新拉起
    finally:
        s.close()


def test_set_global_restarts_only_affected(sup):
    sup.control({"action": "start_all"})
    pids = {n: b.proc.pid for n, b in sup.boards.items()}
    msg = sup.control({"action": "set_global", "jam_level": 2})
    assert "2" in msg and sup.settings["jam_level"] == 2
    changed = {n for n, b in sup.boards.items() if b.proc.pid != pids[n]}
    assert changed == {"tx_jam", "rx_jam"}                  # 换等级只影响干扰波的板
    pids = {n: b.proc.pid for n, b in sup.boards.items()}
    sup.control({"action": "set_global", "team": "blue"})
    assert all(b.proc.pid != pids[n] for n, b in sup.boards.items())   # 换阵营全部重启
    assert any("切换到 蓝方" in e[2] for e in sup.events)


def test_set_board_validation_and_key(sup):
    for bad in ({"key": "abc"}, {"key": "ab cd!"}, {"attenuation": 120}, {"gain_mode": "max"}, {"gain": 99}, {}):
        with pytest.raises(ValueError):
            sup.control(dict(action="set_board", board="tx_jam", **bad))
    with pytest.raises(ValueError):
        sup.control({"action": "set_board", "board": "nope", "attenuation": 10})
    sup.control({"action": "set_board", "board": "tx_jam", "key": "Zz9Yy8"})
    assert sup.boards["tx_jam"].settings["key"] == "Zz9Yy8"
    sup.control({"action": "set_board", "board": "tx_jam", "key": None})
    k = sup.boards["tx_jam"].settings["key"]
    assert len(k) == 6 and k.isalnum() and k != "Zz9Yy8"
    b = sup.boards["tx_jam"]
    b.start()
    pid = b.proc.pid
    sup.control({"action": "set_board", "board": "tx_jam", "key": "Qq1Ww2"})
    assert b.proc.pid != pid                                 # 换密钥要重启发射板


def test_live_gain_and_attenuation_via_xmlrpc(sup):
    calls = []
    for name in ("rx_info", "tx_info"):
        srv = SimpleXMLRPCServer(("127.0.0.1", sup.boards[name].cfg["ctrl"]), logRequests=False, allow_none=True)
        srv.register_function(lambda mode, db: calls.append(("gain", mode, db)), "set_gain")
        srv.register_function(lambda db: calls.append(("att", db)), "set_attenuation")
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    sup.control({"action": "start", "board": "rx_info"})
    sup.control({"action": "start", "board": "tx_info"})
    pid = sup.boards["rx_info"].proc.pid
    msg = sup.control({"action": "set_board", "board": "rx_info", "gain_mode": "manual", "gain": 40})
    assert "实时生效" in msg and ("gain", "manual", 40.0) in calls
    assert sup.boards["rx_info"].proc.pid == pid             # 不重启
    sup.control({"action": "set_board", "board": "tx_info", "attenuation": 25})
    assert ("att", 25.0) in calls
    sup.control({"action": "stop", "board": "rx_info"})
    assert "下次启动生效" in sup.control({"action": "set_board", "board": "rx_info", "gain": 30})


def post(port, body, ctype="application/json"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/control", data=json.dumps(body).encode(),
                                 headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_http_control():
    cfg = make_cfg()
    s = Supervisor(cfg, "x", command_fn=fake())
    ui = WebUI(cfg, port=0, supervisor=s, probe_interval=None)
    ui.start()
    try:
        assert post(ui.port, {"action": "start", "board": "rx_jam"}, "text/plain")[0] == 415
        code, j = post(ui.port, {"action": "start", "board": "rx_jam"})
        assert code == 200 and j["ok"] and s.boards["rx_jam"].running
        code, j = post(ui.port, {"action": "set_global", "team": "green"})
        assert code == 400 and not j["ok"]
        post(ui.port, {"action": "set_global", "team": "blue", "jam_level": 3})
        with urllib.request.urlopen(f"http://127.0.0.1:{ui.port}/api/state", timeout=5) as r:
            snap = json.loads(r.read())
        assert snap["team"] == "blue" and snap["jam_level"] == 3
        assert snap["control"]["boards"]["rx_jam"]["running"]
        assert any("切换到 蓝方" in e["text"] for e in snap["events"])
    finally:
        ui.close()
    assert not s.boards["rx_jam"].running                     # 关调试台时板子进程一起停


def test_http_control_read_only():
    ui = WebUI(make_cfg(), port=0, probe_interval=None)
    ui.start()
    try:
        assert post(ui.port, {"action": "start_all"})[0] == 403
    finally:
        ui.close()
