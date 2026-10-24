"""实验室：一块板同时发信息波和干扰波，干扰波比信息波强（默认 50 dB，接近比赛），检验接收端在强邻道干扰下还能不能解信息波。

  python3 scripts/lab_dual_tx.py --uri ip:192.168.2.1                       # 红方信息波 + L3，干扰强 50 dB，衰减 20 dB
  python3 scripts/lab_dual_tx.py --uri ip:192.168.2.1 --level 1             # 信息波 + L1
  python3 scripts/lab_dual_tx.py --uri ip:192.168.2.1 --ratio 30 --key Ab3dE9
  python3 scripts/lab_dual_tx.py --out /tmp/dual.c64 --seconds 3             # 不接硬件，写 IQ 文件

三个干扰等级都能配（发射用 4 MS/s）。接收端照常用 run_board.py / radar.py 收信息波或干扰波。
"""

import argparse
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rmwave import phy  # noqa: E402
from rmwave.flowgraphs.dual_tx import TX_RATE, DualTxFlowgraph  # noqa: E402
from rmwave.tx import source, stream  # noqa: E402
from rmwave.tx.server import PacketServer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", help="发射板，如 ip:192.168.2.1；不给则必须给 --out")
    ap.add_argument("--out", help="不接硬件，把 IQ 写到文件")
    ap.add_argument("--seconds", type=float, default=3.0, help="--out 时的时长（文件为 4 MS/s）")
    ap.add_argument("--team", choices=["red", "blue"], default="red")
    ap.add_argument("--level", type=int, default=3, choices=[1, 2, 3], help="干扰等级")
    ap.add_argument("--ratio", type=float, default=50.0, help="干扰波比信息波强多少 dB")
    ap.add_argument("--attenuation", type=float, default=20.0, help="发射衰减 dB")
    ap.add_argument("--key", help="干扰波密钥，默认随机")
    ap.add_argument("--info-zmq", default="tcp://127.0.0.1:53221")
    ap.add_argument("--jam-zmq", default="tcp://127.0.0.1:53222")
    args = ap.parse_args()
    if not args.uri and not args.out:
        ap.error("需要 --uri 或 --out")

    jam_stream = stream.JamStream(args.key or source.random_key())
    servers = [PacketServer(stream.InfoStream(), args.info_zmq), PacketServer(jam_stream, args.jam_zmq)]
    for s in servers:
        s.start()
    try:
        tb = DualTxFlowgraph(phy.Team(args.team), args.level, args.info_zmq, args.jam_zmq, args.uri, args.out,
                             args.ratio, args.attenuation,
                             max_samples=int(args.seconds * TX_RATE) if args.out else None)
    except ValueError as e:
        for s in servers:
            s.close()
        print(e, file=sys.stderr)
        return 2
    print(f"同时发：{tb.info.center_hz / 1e6:.2f} MHz 信息波 + {tb.jam.center_hz / 1e6:.2f} MHz L{args.level} 干扰波"
          f"（强 {args.ratio:g} dB，密钥 {jam_stream.key}），发射本振在信息波中心 → {args.uri or args.out}",
          flush=True)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))   # kill 也正常收尾
    tb.start()
    try:
        if args.out:
            tb.wait()
        else:
            while True:
                time.sleep(1)
                print(f"已发 信息波 {servers[0].sent_packets} 包 / 干扰波 {servers[1].sent_packets} 包", flush=True)
    except KeyboardInterrupt:
        pass
    tb.stop()
    tb.wait()
    for s in servers:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
