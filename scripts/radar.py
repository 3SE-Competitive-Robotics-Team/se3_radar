"""一键启动：调试台网页 + 按 config/boards.yaml 拉起全部板子，阵营 / 干扰等级 / 密钥 / 衰减 / 增益都在网页上改。

  python3 scripts/radar.py                    # 启动全部板子，并打开浏览器 http://127.0.0.1:8800
  python3 scripts/radar.py --no-autostart     # 只开网页，板子在网页上手动启动
  python3 scripts/radar.py --sim data/sim     # 没有板子：接收板循环回放 data/sim/<板名>.c64，不启动发射板
  python3 scripts/radar.py --host 0.0.0.0     # 让同一网络里的其他电脑也能看（注意：也能控制）

Windows 上双击仓库根目录的 start_radar.bat（经 WSL 运行本脚本）。Ctrl-C 退出时会停掉全部板子进程。
只想看、不想控制（板子是在别的终端里手动启动的）：python3 scripts/webui.py
"""

import argparse
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rmwave.webui.server import WebUI  # noqa: E402
from rmwave.webui.supervisor import Supervisor  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config/boards.yaml"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--no-autostart", action="store_true", help="不自动启动板子")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--sim", metavar="DIR", help="仿真：接收板循环回放 DIR/<板名>.c64，不启动发射板")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.sim:
        missing = [n for n, b in cfg["boards"].items()
                   if b.get("role") == "rx" and not (Path(args.sim) / f"{n}.c64").exists()]
        if missing:
            print(f"仿真文件不存在：{', '.join(str(Path(args.sim) / f'{n}.c64') for n in missing)}", file=sys.stderr)
            return 2
    sup = Supervisor(cfg, args.config, sim_dir=args.sim)
    try:
        ui = WebUI(cfg, args.host, args.port, supervisor=sup)
    except OSError as e:
        print(f"端口 {args.port} 打不开（{e}）：是不是已经开着一个调试台？", file=sys.stderr)
        return 1
    ui.start(autostart=not args.no_autostart)
    host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{host}:{ui.port}"
    print(f"调试台：{url}   （Ctrl-C 退出并停掉全部板子）", flush=True)
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1), webbrowser.open(url)), daemon=True).start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    # 收尾时再按 Ctrl-C / 再收到信号也不能打断，否则板子进程停不干净
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print("\n正在停止全部板子…", flush=True)
    ui.close()
    print("已全部停止", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
