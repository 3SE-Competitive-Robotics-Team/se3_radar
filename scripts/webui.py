"""调试网页（只读）：看各板子是否在线、收发统计、频谱 / 瀑布图、解出的帧和密钥。

  python3 scripts/webui.py                  # 浏览器打开 http://127.0.0.1:8800
  python3 scripts/webui.py --host 0.0.0.0   # 让同一网络里的其他电脑也能看

先开网页、再开各块板（python3 scripts/run_board.py ...）都行，顺序无所谓；
不接硬件时配合 run_board.py --sim 使用。
"""

import argparse
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rmwave.webui.server import WebUI  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config/boards.yaml"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--no-probe", action="store_true", help="不探测板子 IP 是否可达")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    ui = WebUI(cfg, args.host, args.port, probe_interval=None if args.no_probe else 3.0)
    ui.start()
    host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    print(f"调试网页：http://{host}:{ui.port}   （Ctrl-C 退出）", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    ui.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
