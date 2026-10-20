"""上板自检：逐块检查 config/boards.yaml 里的 NanoSDR。

  python3 scripts/check_boards.py                          # 检查全部板：网络、iio、芯片、通道数
  python3 scripts/check_boards.py rx_info tx_info          # 只查这几块
  python3 scripts/check_boards.py --loopback tx_jam rx_jam # 收发自环 6 s：解出多少帧、收发合计频偏多少

自环前把两块板的天线靠近（或用衰减器 + 线缆直连），发射衰减先用配置里的大衰减。
"""

import argparse
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/boards.yaml"


@dataclass
class BoardInfo:
    name: str
    uri: str
    role: str = ""
    ping: bool = False
    iio: bool = False
    attrs: dict = field(default_factory=dict)
    rx_channels: int = 0
    tx_channels: int = 0
    error: str = ""


def check(name: str, board: dict) -> BoardInfo:
    info = BoardInfo(name, board["uri"], board.get("role", ""))
    host = board["uri"].split(":", 1)[1] if board["uri"].startswith("ip:") else None
    if host:
        info.ping = subprocess.run(["ping", "-c", "1", "-W", "1", host], capture_output=True).returncode == 0
        if not info.ping:
            info.error = f"ping {host} 不通"
            return info
    try:
        out = subprocess.run(["iio_info", "-u", board["uri"]], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        info.error = "iio_info 超时"
        return info
    if out.returncode != 0:
        info.error = (out.stderr or out.stdout).strip().splitlines()[-1] if (out.stderr or out.stdout) else "iio_info 失败"
        return info
    info.iio = True
    parse_iio_info(out.stdout, info)
    return info


def parse_iio_info(text: str, info: BoardInfo) -> None:
    for key in ("hw_model", "hw_model_variant", "hw_serial", "fw_version", "ad9361-phy,model", "ad9361-phy,xo_correction"):
        m = re.search(rf"^\s*{re.escape(key)}: (.+)$", text, re.M)
        if m:
            info.attrs[key] = m.group(1).strip()
    # 按设备分段，数 RX（cf-ad9361-lpc 的 input voltageN）和 TX（dds-core 的 output voltageN）I/Q 通道
    for dev, body in re.findall(r"^\s*iio:device\d+: (\S+).*?\n(.*?)(?=^\s*iio:device\d+:|\Z)", text, re.M | re.S):
        if dev == "cf-ad9361-lpc":
            info.rx_channels = len(re.findall(r"^\s*voltage\d+:\s*\(input", body, re.M)) // 2
        elif dev == "cf-ad9361-dds-core-lpc":
            info.tx_channels = len(re.findall(r"^\s*voltage\d+:\s*\(output", body, re.M)) // 2


def report(infos: list[BoardInfo]) -> bool:
    ok = True
    for i in infos:
        if i.error:
            ok = False
            print(f"✗ {i.name:8} {i.uri:18} {i.error}")
            continue
        chip = i.attrs.get("ad9361-phy,model", "?")
        print(f"✓ {i.name:8} {i.uri:18} {i.attrs.get('hw_model', '?')} | 芯片 {chip} | 固件 {i.attrs.get('fw_version', '?')} "
              f"| RX {i.rx_channels} 路 TX {i.tx_channels} 路 | 序列号 {i.attrs.get('hw_serial', '?')}")
        if i.role == "rx" and i.rx_channels == 0:
            ok = False
            print("    ✗ 配置为接收板，但固件里没有接收 DMA（cf-ad9361-lpc），这块板只能发不能收。"
                  "\n      标准 PlutoSDR 固件一定有它；固件版本带 -dirty 说明是改过的，需要刷回标准固件，"
                  "\n      或者把这块板改配成发射板")
        elif i.role == "tx" and i.tx_channels == 0:
            ok = False
            print("    ✗ 配置为发射板，但固件里没有发射 DMA（cf-ad9361-dds-core-lpc）")
        elif i.rx_channels == 0 and i.tx_channels == 0:
            print("    ! 没识别到 AD936x 收发通道，iio_info 输出格式可能不同，请把完整输出发给我")
        if i.attrs.get("fw_version", "").endswith("-dirty"):
            print(f"    ! 固件 {i.attrs['fw_version']} 是改过的版本（非 ADI 官方发布），能力可能和标准固件不同")
    serials = [i.attrs.get("hw_serial") for i in infos if i.attrs.get("hw_serial")]
    if len(serials) != len(set(serials)):
        print("! 有两个 URI 指向同一块板（序列号重复），检查各板 IP 是否改成了不同子网")
        ok = False
    return ok


def loopback(cfg: dict, tx: str, rx: str, seconds: float) -> bool:
    """同时起一发一收（各自走 run_board.py），统计接收端最后一行输出。"""
    level = cfg.get("jam_level", 1)
    procs = []
    try:
        rx_p = subprocess.Popen([sys.executable, "scripts/run_board.py", rx, "--level", str(level)],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(rx_p)
        time.sleep(1.0)
        tx_p = subprocess.Popen([sys.executable, "scripts/run_board.py", tx, "--level", str(level), "--key", "Lb0Tst"],
                                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        procs.append(tx_p)
        time.sleep(seconds)
    finally:
        for p in reversed(procs):
            p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    out = rx_p.stdout.read() if rx_p.stdout else ""
    lines = [l for l in out.splitlines() if l.startswith("[")]
    print("\n".join(out.splitlines()[-15:]))
    if not lines:
        print("✗ 接收端没有输出统计，看上面的报错")
        return False
    last = lines[-1]
    m = re.search(r"AFC (\S+) ([+-][\d.]+) kHz", last)
    got_key = "Lb0Tst" in last
    print()
    print(f"{'✓' if got_key else '✗'} 自环 {tx} → {rx}：{'解出密钥 Lb0Tst' if got_key else '没有解出密钥'}")
    if m:
        print(f"  收发合计频偏 {m.group(2)} kHz（AFC {m.group(1)}）。"
              f"若 AFC 锁定，可把这个值写进 {rx} 的 offset，开机就从这里开始跟踪")
    return got_key


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("boards", nargs="*", help="板名，默认全部")
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--loopback", nargs=2, metavar=("TX", "RX"), help="收发自环测试")
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.loopback:
        return 0 if loopback(cfg, *args.loopback, args.seconds) else 1
    names = args.boards or list(cfg["boards"])
    infos = [check(n, cfg["boards"][n]) for n in names]
    return 0 if report(infos) else 1


if __name__ == "__main__":
    sys.exit(main())
