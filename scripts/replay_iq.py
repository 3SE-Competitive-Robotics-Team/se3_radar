"""离线回放 IQ 文件（complex64，1 MS/s），统计解出的帧。

例：
  python3 scripts/replay_iq.py data/x.c64 --team red --level 0           # 信息波
  python3 scripts/replay_iq.py data/x.c64 --team red --level 1 --show 5  # 一级干扰波
  python3 scripts/replay_iq.py data/x.c64 --team red --level 0 --offset-hz 600e3  # 宽带录波里取信息波
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rmwave import phy  # noqa: E402
from rmwave.io.iqfile import read_c64  # noqa: E402
from rmwave.rx.decoder import decode_iq  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--team", choices=["red", "blue"], required=True, help="发射源所在基座 / 接收雷达阵营")
    ap.add_argument("--level", type=int, choices=[0, 1, 2, 3], required=True, help="0=信息波，1~3=干扰波等级")
    ap.add_argument("--offset-hz", type=float, default=0.0, help="目标信号相对录波中心频率的偏移")
    ap.add_argument("--show", type=int, default=0, help="打印前 N 帧内容")
    ap.add_argument("--no-afc", action="store_true", help="关闭载波频偏跟踪")
    args = ap.parse_args()

    spec = phy.wave_spec(phy.Team(args.team), args.level)
    iq = read_c64(args.file)
    frames, rx = decode_iq(iq, spec, args.offset_hz, afc=not args.no_afc)

    seconds = len(iq) / phy.SAMPLE_RATE
    counts = Counter(f"0x{f.cmd_id:04X}" for f in frames)
    s = rx.decoder.demod.stats
    print(f"{Path(args.file).name}: {seconds:.2f} s, {spec.kind.value} L{spec.level} @ {spec.center_hz / 1e6:.2f} MHz")
    print(f"  空口包 {s.packets}（候选 {s.candidates}，接入码拒 {s.rejected_ac}，头部拒 {s.rejected_header}），"
          f"前端偏移 {rx.frontend.offset_hz / 1e3:+.1f} kHz")
    r = rx.decoder.reassembler.stats
    print(f"  裁判帧 {len(frames)}（CRC8 失败 {r.crc8_fail}，CRC16 失败 {r.crc16_fail}）  {dict(sorted(counts.items()))}")
    keys = Counter(f.message.key for f in frames if f.cmd_id == 0x0A06 and f.message)
    if keys:
        print(f"  密钥: {dict(keys)}，不同序号 {len({f.seq for f in frames if f.cmd_id == 0x0A06})}")
    for f in frames[:args.show]:
        print(f"  @{f.sample_index:>8} seq={f.seq:3d} 0x{f.cmd_id:04X} {f.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
