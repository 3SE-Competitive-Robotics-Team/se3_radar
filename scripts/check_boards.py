"""上板自检：逐块检查 config/boards.yaml 里的 NanoSDR。

  python3 scripts/check_boards.py                          # 检查配置里的全部板：网络、iio、芯片、通道数
  python3 scripts/check_boards.py rx_info tx_info          # 只查这几块
  python3 scripts/check_boards.py --scan                   # 不看配置，自动发现所有插着的板
  python3 scripts/check_boards.py --loopback tx_jam rx_info          # 任意一发一收自环 6 s
  python3 scripts/check_boards.py --loopback ip:192.168.3.1 ip:192.168.4.2 --level 2

自环前把两块板的天线靠近（或用衰减器 + 线缆直连），发射衰减先用配置里的大衰减。
"""

import argparse
import re
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
        # 小包通、大包不通时，iio / SSH 会时好时坏、大文件传输卡死。
        # 实测在 WSL 镜像网络下出现过（Windows 直接发大包是好的），wsl --shutdown 后恢复。
        big = subprocess.run(["ping", "-c", "5", "-i", "0.2", "-W", "1", "-s", "1472", "-M", "do", host],
                             capture_output=True, text=True).stdout
        m = re.search(r"(\d+)% packet loss", big)
        if m and int(m.group(1)) >= 60:
            info.error = (f"小包通但 1472 字节大包丢 {m.group(1)}%：在 WSL 里多半是镜像网络映射异常，"
                          "先在 Windows 执行 wsl --shutdown 再试")
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
        if i.rx_channels == 0 and i.tx_channels == 0:
            if i.role:
                ok = False
            print("    ✗ 收发设备都没有：射频芯片多半没初始化成功（板上 dmesg 常见 `Unsupported PRODUCT_ID 0xFF`），"
                  "\n      同一份固件在别的板上正常时，基本是这块板的硬件问题")
        elif i.rx_channels == 0:
            if i.role == "rx":
                ok = False
            print("    ✗ 没有接收设备（cf-ad9361-lpc）：开机接收校准失败（板上 dmesg 常见 `Tuning RX FAILED`），"
                  "\n      这块板只能当发射板")
        elif i.tx_channels == 0:
            if i.role == "tx":
                ok = False
            print("    ✗ 没有发射设备（cf-ad9361-dds-core-lpc），这块板只能当接收板")
    serials = [i.attrs.get("hw_serial") for i in infos if i.attrs.get("hw_serial")]
    if len(serials) != len(set(serials)):
        print("! 有两个 URI 指向同一块板（序列号重复），检查各板 IP 是否改成了不同子网")
        ok = False
    return ok


LOOPBACK_KEY = "Lb0Tst"


def resolve(cfg: dict, name_or_uri: str) -> tuple[str, dict]:
    """板名 → (uri, 配置)；也可以直接给 ip:x.x.x.x。"""
    if name_or_uri in cfg["boards"]:
        b = cfg["boards"][name_or_uri]
        return b["uri"], b
    return name_or_uri, {}


def loopback(cfg: dict, tx: str, rx: str, seconds: float, level: int | None = None, attenuation: float | None = None) -> bool:
    """任意一发一收：两块板都按同一个干扰波等级起流图，接收端解出发射的密钥即通过。

    不经过 run_board.py 的角色配对，所以发射板、接收板可以是配置里的任何两块（或直接给 URI）。
    """
    import socket

    from rmwave import phy
    from rmwave.flowgraphs.rx import RxFlowgraph
    from rmwave.flowgraphs.tx import TxFlowgraph
    from rmwave.rx.client import StreamReceiver
    from rmwave.tx import stream
    from rmwave.tx.server import PacketServer

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    tx_uri, tx_cfg = resolve(cfg, tx)
    rx_uri, rx_cfg = resolve(cfg, rx)
    level = level or cfg.get("jam_level", 1)
    att = attenuation if attenuation is not None else tx_cfg.get("attenuation", 70)
    spec = phy.wave_spec(phy.Team(cfg.get("team", "red")), level)
    print(f"自环：{tx_uri} 发 → {rx_uri} 收，干扰波 L{level} @ {spec.center_hz / 1e6:.2f} MHz，"
          f"发射衰减 {att} dB，{seconds:.0f} s", flush=True)

    tx_addr, rx_addr, ctrl = (f"tcp://127.0.0.1:{free_port()}", f"tcp://127.0.0.1:{free_port()}", free_port())
    srv = PacketServer(stream.JamStream(LOOPBACK_KEY), tx_addr)
    srv.start()
    rx_client = StreamReceiver(spec, rx_addr, f"http://127.0.0.1:{ctrl}", float(rx_cfg.get("offset", 0)))
    rx_tb = RxFlowgraph(spec, rx_addr, uri=rx_uri, offset_hz=float(rx_cfg.get("offset", 0)),
                        gain_mode=rx_cfg.get("gain_mode", "slow_attack"))
    from rmwave.flowgraphs import control
    ctrl_srv = control.serve(rx_tb, ctrl, ["set_offset", "get_offset"])
    tx_tb = TxFlowgraph(spec, tx_addr, uri=tx_uri, attenuation_db=float(att))
    frames = []
    try:
        rx_tb.start()
        time.sleep(1.0)
        tx_tb.start()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            frames += rx_client.poll(100)
    finally:
        tx_tb.stop(); tx_tb.wait()
        rx_tb.stop(); rx_tb.wait()
        ctrl_srv.shutdown()
        srv.close()
        rx_client.close()

    s = rx_client.decoder.demod.stats
    keys = sorted(rx_client.keys)
    expected = seconds / stream.PACKET_SECONDS
    print(f"  发射 {srv.sent_packets} 包；接收 {rx_client.samples / phy.SAMPLE_RATE:.1f} s 采样，"
          f"解出空口包 {s.packets}、密钥帧 {len(frames)}（满分约 {expected:.0f}），CRC16 失败 {rx_client.decoder.reassembler.stats.crc16_fail}")
    print(f"  解出的密钥 {keys}；AFC {'锁定' if rx_client.tracker.locked else '未锁定'}，"
          f"收发合计频偏 {rx_client.tracker.offset_hz / 1e3:+.1f} kHz")
    ok = keys == [LOOPBACK_KEY]
    print(f"{'✓' if ok else '✗'} 自环 {tx} → {rx}：{'解出密钥 ' + LOOPBACK_KEY if ok else '没有解出密钥'}")
    if not ok:
        print("  排查顺序：两块板天线是否装好、靠近；发射衰减是否太大（可试 --attenuation 50）；接收是否过载（太近时加大衰减）")
    return ok


def scan() -> list[BoardInfo]:
    """不看配置，按本机 192.168.x.y/24 地址逐个网段试 .1/.2，找出所有已连接的板。"""
    out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
    subnets = sorted({m.group(1) for m in re.finditer(r"inet (192\.168\.\d+)\.\d+/24", out)})
    found = []
    for net in subnets:
        for host in (1, 2):
            uri = f"ip:{net}.{host}"
            r = subprocess.run(["iio_attr", "-u", uri, "-C", "hw_serial"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and "hw_serial" in r.stdout:
                found.append(check(f"{net}.{host}", {"uri": uri}))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("boards", nargs="*", help="板名，默认全部")
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--scan", action="store_true", help="不看配置，自动发现所有已连接的板")
    ap.add_argument("--loopback", nargs=2, metavar=("TX", "RX"), help="收发自环：板名或 ip:地址，任意一发一收")
    ap.add_argument("--level", type=int, choices=[1, 2, 3], help="自环用的干扰波等级，默认取配置")
    ap.add_argument("--attenuation", type=float, help="自环发射衰减 dB，默认取发射板配置")
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.scan:
        found = scan()
        if not found:
            print("没发现任何板：检查 USB 线（要插数据口）和 Windows 里有没有 PlutoSDR 网卡")
            return 1
        return 0 if report(found) else 1
    if args.loopback:
        sys.path.insert(0, str(ROOT))
        return 0 if loopback(cfg, *args.loopback, args.seconds, args.level, args.attenuation) else 1
    names = args.boards or list(cfg["boards"])
    infos = [check(n, cfg["boards"][n]) for n in names]
    return 0 if report(infos) else 1


if __name__ == "__main__":
    sys.exit(main())
