"""进程管理：由调试台启动 / 停止 / 重启各块板（scripts/run_board.py），进程意外退出时自动拉起。

全局参数（阵营、干扰等级）变了会重启受影响的板；发射衰减、接收增益通过各板的 XMLRPC 控制口实时下发，不重启。
"""

import os
import random
import string
import subprocess
import sys
import threading
import time
import xmlrpc.client
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GAIN_MODES = ("slow_attack", "fast_attack", "hybrid", "manual")
RESTART_BACKOFF_S = (1, 2, 5, 10, 30)
KEY_CHARS = string.ascii_letters + string.digits


def valid_key(key: str) -> bool:
    return len(key) == 6 and all(c in KEY_CHARS for c in key)


class BoardProcess:
    def __init__(self, name: str, cfg: dict, sup: "Supervisor"):
        self.name, self.cfg, self.sup = name, cfg, sup
        self.role = cfg.get("role")
        self.settings = {
            "key": cfg.get("key") or None,
            "attenuation": float(cfg.get("attenuation", 70)),
            "gain_mode": cfg.get("gain_mode", "slow_attack"),
            "gain": float(cfg.get("gain", 50)),
        }
        self.proc: subprocess.Popen | None = None
        self.want_running = False
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.restarts = 0
        self.log: deque = deque(maxlen=300)
        self._next_try = 0.0
        self._fails = 0
        self._reader: threading.Thread | None = None

    # ---- 命令行 ----
    def command(self) -> list[str]:
        if self.sup.command_fn is not None:            # 测试用：换成假进程
            return self.sup.command_fn(self)
        return self.run_board_command()

    def run_board_command(self) -> list[str]:
        s, g = self.settings, self.sup.settings
        cmd = [sys.executable, "-u", str(ROOT / "scripts/run_board.py"), self.name, "--config", str(self.sup.config_path),
               "--team", g["team"]]
        if self.cfg.get("wave") == "jam":
            cmd += ["--level", str(g["jam_level"])]
        if self.role == "tx":
            cmd += ["--attenuation", str(s["attenuation"])]
            if self.cfg.get("wave") == "jam" and s["key"]:
                cmd += ["--key", s["key"]]
        else:
            cmd += ["--gain-mode", s["gain_mode"], "--gain", str(s["gain"])]
        sim = self.sup.sim_file(self.name)
        if sim:
            cmd += ["--sim", sim]
        return cmd

    # ---- 启停 ----
    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self.want_running = True
        self._fails = 0
        self._spawn()

    def _spawn(self) -> None:
        if self.running:
            return
        if self.role == "tx" and self.sup.sim_dir:
            self.log.append("[调试台] 仿真模式下不启动发射板")
            self.want_running = False
            return
        cmd = self.command()
        self.log.append("[调试台] $ " + " ".join(cmd[2:]))
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.started_at, self.exit_code = time.monotonic(), None
        self._reader = threading.Thread(target=self._read, args=(self.proc,), daemon=True)
        self._reader.start()

    def _read(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if line and set(line) - {"U", "O"}:
                self.log.append(line[:300])
        proc.stdout.close()

    def stop(self, timeout: float = 5.0) -> None:
        self.want_running = False
        self._terminate(timeout)

    def _terminate(self, timeout: float) -> None:
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, 2)          # SIGINT：让 run_board.py 自己收尾（关流图、关 socket）
        except (OSError, AttributeError):
            p.terminate()
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(2)
        self.exit_code = p.returncode
        self.log.append(f"[调试台] 已停止（退出码 {p.returncode}）")

    def restart(self) -> None:
        self._terminate(5.0)
        self.start()

    def check(self, now: float) -> str | None:
        """守护：想让它跑但它退出了，就按退避时间重启。返回一条事件文字或 None。"""
        if not self.want_running or self.proc is None or self.proc.poll() is None:
            return None
        if self.exit_code is None:
            self.exit_code = self.proc.returncode
            if self.started_at and now - self.started_at > 30:   # 跑了一阵才退出，退避从头算
                self._fails = 0
            self._fails += 1
            delay = RESTART_BACKOFF_S[min(self._fails - 1, len(RESTART_BACKOFF_S) - 1)]
            self._next_try = now + delay
            self.log.append(f"[调试台] 进程退出（退出码 {self.exit_code}），{delay} s 后重启")
            if not self.sup.sim_dir:
                return f"进程意外退出（退出码 {self.exit_code}），{delay} s 后自动重启"
            return None
        if now >= self._next_try:
            self.restarts += 1
            self._spawn()
        return None

    # ---- 实时参数 ----
    def _rpc(self):
        return xmlrpc.client.ServerProxy(f"http://127.0.0.1:{self.cfg['ctrl']}", allow_none=True)

    def apply_live(self) -> str | None:
        """把衰减 / 增益发给正在跑的流图；没在跑就只记下，下次启动生效。"""
        if not self.running:
            return None
        try:
            if self.role == "tx":
                self._rpc().set_attenuation(float(self.settings["attenuation"]))
            else:
                self._rpc().set_gain(self.settings["gain_mode"], float(self.settings["gain"]))
        except (OSError, xmlrpc.client.Error) as e:
            return f"实时下发失败（{e}），重启后生效"
        return None

    def state(self, now: float) -> dict:
        return {"name": self.name, "role": self.role, "wave": self.cfg.get("wave"), "running": self.running,
                "want_running": self.want_running, "pid": self.proc.pid if self.running else None,
                "uptime_s": now - self.started_at if self.running and self.started_at else None,
                "exit_code": self.exit_code, "restarts": self.restarts, "settings": dict(self.settings),
                "log": list(self.log)[-80:]}


class Supervisor:
    def __init__(self, cfg: dict, config_path: str | Path, sim_dir: str | None = None, on_event=None,
                 command_fn=None):
        self.cfg = cfg
        self.command_fn = command_fn
        self.config_path = Path(config_path)
        self.sim_dir = sim_dir
        self.on_event = on_event or (lambda board, level, text: None)
        self.settings = {"team": cfg.get("team", "red"), "jam_level": int(cfg.get("jam_level", 1))}
        self.lock = threading.RLock()
        self.boards = {n: BoardProcess(n, b, self) for n, b in cfg["boards"].items()}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sim_file(self, name: str) -> str | None:
        """仿真模式：接收板循环回放 <sim_dir>/<板名>.c64。"""
        if not self.sim_dir:
            return None
        return str(Path(self.sim_dir) / f"{name}.c64")

    # ---- 操作（HTTP 线程调用）----
    def _board(self, name: str) -> BoardProcess:
        if name not in self.boards:
            raise ValueError(f"没有这块板：{name}")
        return self.boards[name]

    def control(self, req: dict) -> str:
        """处理一条控制请求，返回给用户看的结果文字；参数不对抛 ValueError。"""
        action = req.get("action")
        with self.lock:
            if action == "start_all":
                for b in self.boards.values():
                    b.start()
                return "全部启动"
            if action == "stop_all":
                for b in self.boards.values():
                    b.stop()
                return "全部停止"
            if action in ("start", "stop", "restart"):
                b = self._board(req.get("board", ""))
                getattr(b, action)()
                return {"start": "已启动", "stop": "已停止", "restart": "已重启"}[action] + f" {b.name}"
            if action == "set_global":
                return self._set_global(req)
            if action == "set_board":
                return self._set_board(req)
        raise ValueError(f"未知操作：{action}")

    def _set_global(self, req: dict) -> str:
        team, level = req.get("team", self.settings["team"]), int(req.get("jam_level", self.settings["jam_level"]))
        if team not in ("red", "blue") or level not in (1, 2, 3):
            raise ValueError("阵营只能是 red / blue，干扰等级只能是 1~3")
        changed_team, changed_level = team != self.settings["team"], level != self.settings["jam_level"]
        self.settings.update(team=team, jam_level=level)
        hit = [b for b in self.boards.values()
               if b.want_running and (changed_team or (changed_level and b.cfg.get("wave") == "jam"))]
        for b in hit:
            b.restart()
        tag = f"{'红方' if team == 'red' else '蓝方'} · 干扰等级 {level}"
        self.on_event("调试台", "info", f"切换到 {tag}" + (f"，重启 {', '.join(b.name for b in hit)}" if hit else ""))
        return f"已切换到 {tag}" + (f"，重启了 {len(hit)} 块板" if hit else "")

    def _set_board(self, req: dict) -> str:
        b = self._board(req.get("board", ""))
        s, msgs, restart = b.settings, [], False
        if "key" in req:
            key = (req["key"] or "").strip() or None      # 空或 None = 随机
            if key is not None and not valid_key(key):
                raise ValueError("密钥要 6 位字母或数字")
            if key is None:
                key = "".join(random.choice(KEY_CHARS) for _ in range(6))
            s["key"], restart = key, True
            msgs.append(f"密钥 {key}")
        if "attenuation" in req:
            att = float(req["attenuation"])
            if not 0 <= att <= 89.75:
                raise ValueError("发射衰减范围 0~89.75 dB")
            s["attenuation"] = att
            msgs.append(f"衰减 {att:g} dB")
        if "gain_mode" in req:
            if req["gain_mode"] not in GAIN_MODES:
                raise ValueError(f"增益模式只能是 {' / '.join(GAIN_MODES)}")
            s["gain_mode"] = req["gain_mode"]
            msgs.append(f"增益模式 {s['gain_mode']}")
        if "gain" in req:
            gain = float(req["gain"])
            if not -3 <= gain <= 73:
                raise ValueError("接收增益范围 -3~73 dB")
            s["gain"] = gain
            msgs.append(f"增益 {gain:g} dB")
        if not msgs:
            raise ValueError("没有要改的参数")
        if restart and b.want_running:
            b.restart()
            msgs.append("已重启")
        elif not restart:
            err = b.apply_live()
            msgs.append(err or ("已实时生效" if b.running else "下次启动生效"))
        text = f"{b.name}：" + "，".join(msgs)
        self.on_event(b.name, "info", text)
        return text

    # ---- 守护线程 ----
    def start(self, autostart: bool = False) -> None:
        if autostart:
            self.control({"action": "start_all"})

        def loop():
            while not self._stop.is_set():
                now = time.monotonic()
                with self.lock:
                    for b in self.boards.values():
                        ev = b.check(now)
                        if ev:
                            self.on_event(b.name, "bad", ev)
                self._stop.wait(0.5)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def state(self) -> dict:
        now = time.monotonic()
        with self.lock:
            return {"settings": dict(self.settings), "sim_dir": self.sim_dir,
                    "boards": {n: b.state(now) for n, b in self.boards.items()}}

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self.lock:
            for b in self.boards.values():
                b.stop()
