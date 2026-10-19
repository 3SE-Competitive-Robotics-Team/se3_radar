"""用真实的 GNU Radio 块跑收发流图（文件代替 SDR）。没装 GNU Radio 时跳过。

- 发射：PacketServer（Python）→ zeromq.req_source → GFSK → 文件；再用已验证的 numpy 接收端解，
  证明 GNU Radio 调制与 rmwave.dsp 一致。
- 接收：文件 → 移频低通 → 鉴频 → zeromq.pub_sink → StreamReceiver（Python），
  与 numpy 前端在同一份 IQ 上的结果对比。
- 发射 → 接收全链路，以及 XMLRPC 改偏移。
"""

import socket
import threading
import time
import xmlrpc.client
from pathlib import Path

import pytest

pytest.importorskip("gnuradio.zeromq")
pytest.importorskip("gnuradio.iio")

from rmwave import phy  # noqa: E402
from rmwave.flowgraphs import control  # noqa: E402
from rmwave.flowgraphs.rx import RxFlowgraph  # noqa: E402
from rmwave.flowgraphs.tx import TxFlowgraph  # noqa: E402
from rmwave.io.iqfile import read_c64  # noqa: E402
from rmwave.protocol import cmds  # noqa: E402
from rmwave.rx.client import StreamReceiver  # noqa: E402
from rmwave.rx.decoder import decode_iq  # noqa: E402
from rmwave.tx import stream  # noqa: E402
from rmwave.tx.server import PacketServer  # noqa: E402

TCR = Path(__file__).resolve().parents[1] / "data/tcr/tcr_rm2026_single_channel_iq_test_pack_v1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def transmit_to_file(spec, packet_stream, path: Path, seconds: float) -> PacketServer:
    addr = f"tcp://127.0.0.1:{free_port()}"
    srv = PacketServer(packet_stream, addr)
    srv.start()
    try:
        tb = TxFlowgraph(spec, addr, out_file=str(path), max_samples=int(seconds * phy.SAMPLE_RATE))
        tb.run()
    finally:
        srv.close()
    return srv


def receive_file(spec, path: Path, ctrl_port: int | None = None, offset_hz: float = 0.0):
    """文件 → RxFlowgraph → ZMQ → StreamReceiver，返回 (帧, 接收器)。"""
    addr = f"tcp://127.0.0.1:{free_port()}"
    rx = StreamReceiver(spec, addr, f"http://127.0.0.1:{ctrl_port}" if ctrl_port else None, offset_hz)
    tb = RxFlowgraph(spec, addr, in_file=str(path), offset_hz=offset_hz)
    if ctrl_port:
        server = control.serve(tb, ctrl_port, ["set_offset", "get_offset"])
    time.sleep(0.3)  # 等 SUB 连上，避免开头的数据丢在连接建立之前
    tb.start()
    frames = []
    done = threading.Event()
    threading.Thread(target=lambda: (tb.wait(), done.set()), daemon=True).start()
    while not done.is_set():
        frames += rx.poll(50)
    for _ in range(5):  # 收尾：把还在路上的消息收完
        frames += rx.poll(100)
    frames += rx.flush()
    tb.stop()
    tb.wait()
    if ctrl_port:
        server.shutdown()
    rx.close()
    return frames, rx, tb


def test_tx_flowgraph_jam_matches_numpy_receiver(tmp_path):
    spec = phy.wave_spec(phy.Team.BLUE, 2)
    out = tmp_path / "jam.c64"
    srv = transmit_to_file(spec, stream.JamStream("Ab3dE9"), out, 0.6)
    frames, _ = decode_iq(read_c64(out), spec)
    # 流图里积压的包 = 改密钥后要等多久才上天；默认缓冲区会积压几十秒
    assert srv.sent_packets - 0.6 / stream.PACKET_SECONDS <= 20
    assert len(frames) >= 0.6 / stream.PACKET_SECONDS - 3
    assert {f.message.key for f in frames} == {"Ab3dE9"}
    seqs = [f.seq for f in frames]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))  # 连续，没有丢包


def test_tx_flowgraph_info_matches_numpy_receiver(tmp_path):
    spec = phy.wave_spec(phy.Team.RED, 0)
    out = tmp_path / "info.c64"
    transmit_to_file(spec, stream.InfoStream(seed=3), out, 1.0)
    frames, _ = decode_iq(read_c64(out), spec)
    assert len(frames) >= 45  # 1 s ≈ 10 组 × 5 帧，首尾各可能截掉一帧
    assert all(f.message is not None for f in frames)
    assert [f.cmd_id for f in frames[:5]] == list(cmds.INFO_CMDS)


def test_tx_then_rx_flowgraph_end_to_end(tmp_path):
    spec = phy.wave_spec(phy.Team.RED, 3)
    out = tmp_path / "l3.c64"
    transmit_to_file(spec, stream.JamStream("Zz9Yy8"), out, 1.0)
    frames, rx, _ = receive_file(spec, out)
    assert len(frames) >= 0.8 / stream.PACKET_SECONDS
    assert {f.message.key for f in frames} == {"Zz9Yy8"}


@pytest.mark.skipif(not TCR.exists(), reason="未下载 TCR 赛场 IQ，见 data/README.md")
def test_rx_flowgraph_on_tcr_field_info_wave():
    spec = phy.wave_spec(phy.Team.RED, 0)
    path = TCR / "info_wave_2s.c64"
    gr_frames, rx, _ = receive_file(spec, path, ctrl_port=free_port())
    np_frames, _ = decode_iq(read_c64(path), spec)
    assert len(gr_frames) >= len(np_frames) - 3  # 允许开头连接建立时少几帧
    assert sum(f.cmd_id == cmds.Cmd.POSITION for f in gr_frames) >= 17
    assert abs(rx.tracker.offset_hz) < 5e3


def test_xmlrpc_set_offset(tmp_path):
    spec = phy.wave_spec(phy.Team.RED, 0)
    path = tmp_path / "zeros.c64"
    path.write_bytes(bytes(8 * 1000))
    tb = RxFlowgraph(spec, f"tcp://127.0.0.1:{free_port()}", in_file=str(path))
    port = free_port()
    server = control.serve(tb, port, ["set_offset", "get_offset"])
    try:
        proxy = xmlrpc.client.ServerProxy(f"http://127.0.0.1:{port}")
        proxy.set_offset(12345.0)
        assert proxy.get_offset() == pytest.approx(12345.0)
    finally:
        server.shutdown()
