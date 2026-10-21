"""按 config/boards.yaml 启动一块板：GNU Radio 流图（子进程）+ Python 侧（本进程）。

  python3 scripts/run_board.py tx_info               # 发信息波
  python3 scripts/run_board.py tx_jam --level 2      # 发二级干扰波（覆盖配置里的 jam_level）
  python3 scripts/run_board.py rx_info               # 收信息波，每秒打印统计
  python3 scripts/run_board.py rx_jam --show         # 收干扰波，打印每一帧

没有硬件时用 --sim：发射端写 IQ 文件，接收端读 IQ 文件，用来检查整套启动流程。
  python3 scripts/run_board.py tx_jam --sim /tmp/jam.c64 --seconds 3
  python3 scripts/run_board.py rx_jam --sim /tmp/jam.c64

运行时会把状态、解出的帧、频谱发给调试网页（python3 scripts/webui.py），网页没开也不影响。
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rmwave import phy, telemetry  # noqa: E402
from rmwave.rx.client import StreamReceiver, run  # noqa: E402
from rmwave.tx import source, stream  # noqa: E402
from rmwave.tx.server import PacketServer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help="config/boards.yaml 里的板名")
    ap.add_argument("--config", default=str(ROOT / "config/boards.yaml"))
    ap.add_argument("--team", choices=["red", "blue"], help="覆盖配置里的 team")
    ap.add_argument("--level", type=int, choices=[1, 2, 3], help="覆盖配置里的 jam_level（干扰波板）")
    ap.add_argument("--key", help="干扰波密钥，覆盖配置")
    ap.add_argument("--show", action="store_true", help="接收端打印每一帧")
    ap.add_argument("--sim", help="不接硬件：发射写到这个 IQ 文件 / 接收读这个 IQ 文件")
    ap.add_argument("--seconds", type=float, default=3.0, help="--sim 发射时长")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    board = cfg["boards"][args.board]
    team = args.team or cfg["team"]
    level = 0 if board["wave"] == "info" else (args.level or cfg["jam_level"])
    spec = phy.wave_spec(phy.Team(team), level)

    status_addr, spectrum_addr = telemetry.addresses(board)
    pub = telemetry.Publisher(status_addr, args.board)
    if pub.error:
        print(f"[{args.board}] 调试网页遥测关闭（{pub.error}）", flush=True)
    try:
        if board["role"] == "tx":
            return run_tx(board, spec, team, level, args, pub)
        return run_rx(board, spec, team, level, args, pub, spectrum_addr)
    finally:
        pub.close()


def run_tx(board, spec, team, level, args, pub) -> int:
    if level == 0:
        s = stream.InfoStream()
        desc = "信息波"
    else:
        s = stream.JamStream(args.key or board.get("key") or source.random_key())
        desc = f"L{level} 干扰波，密钥 {s.key}"
    key = getattr(s, "key", None)
    srv = PacketServer(s, board["zmq"])
    srv.start()
    cmd = [sys.executable, "-m", "rmwave.flowgraphs.tx", "--team", team, "--level", str(level),
           "--zmq", board["zmq"], "--ctrl-port", str(board["ctrl"]), "--attenuation", str(board.get("attenuation", 70))]
    cmd += ["--out", args.sim, "--seconds", str(args.seconds)] if args.sim else ["--uri", board["uri"]]
    print(f"[{args.board}] 发射 {desc} @ {spec.center_hz / 1e6:.2f} MHz", flush=True)
    proc = subprocess.Popen(cmd, cwd=ROOT)
    last = time.monotonic()
    try:
        while proc.poll() is None:
            time.sleep(0.5)
            pub.send(telemetry.tx_status(board, spec, team, srv.sent_packets, key))
            if time.monotonic() - last >= 1.0:
                print(f"[{args.board}] 已发 {srv.sent_packets} 包", flush=True)
                last = time.monotonic()
    except KeyboardInterrupt:
        proc.terminate()
    proc.wait()
    srv.close()
    pub.send(telemetry.tx_status(board, spec, team, srv.sent_packets, key))
    return proc.returncode or 0


def run_rx(board, spec, team, level, args, pub, spectrum_addr) -> int:
    offset = float(board.get("offset", 0))
    rx = StreamReceiver(spec, board["zmq"], f"http://127.0.0.1:{board['ctrl']}", offset)
    cmd = [sys.executable, "-m", "rmwave.flowgraphs.rx", "--team", team, "--level", str(level),
           "--zmq", board["zmq"], "--ctrl-port", str(board["ctrl"]), "--offset", str(offset),
           "--gain-mode", board.get("gain_mode", "slow_attack"), "--spectrum", spectrum_addr]
    cmd += ["--file", args.sim] if args.sim else ["--uri", board["uri"]]
    print(f"[{args.board}] 接收 {spec.kind.value} L{level} @ {spec.center_hz / 1e6:.2f} MHz", flush=True)
    time.sleep(0.3)  # 先让 SUB 连上
    proc = subprocess.Popen(cmd, cwd=ROOT)
    try:
        run(rx, args.show, stop=lambda: proc.poll() is not None,
            on_frame=lambda f: pub.send(telemetry.frame_event(f)),
            on_status=lambda counts: pub.send(telemetry.rx_status(rx, counts, board, spec, team)))
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
