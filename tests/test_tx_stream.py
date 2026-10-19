import numpy as np

from rmwave.protocol import air, cmds
from rmwave.protocol.referee import FrameReassembler
from rmwave.tx import source, stream


def payloads(s, n):
    pkts = [s.next_packet() for _ in range(n)]
    assert all(len(p) == air.PACKET_LEN for p in pkts)
    return [air.parse_packet(p, s_kind(s)) for p in pkts]


def s_kind(s):
    return air.WaveKind.INFO if isinstance(s, stream.InfoStream) else air.WaveKind.JAM


def test_info_stream_runs_at_10hz_air_time():
    s = stream.InfoStream(seed=1)
    n = 197  # ≈ 2 s 空中时间
    frames = FrameReassembler().feed(b"".join(payloads(s, n)))
    cycles = n * stream.PACKET_SECONDS / stream.INFO_PERIOD_S
    assert abs(len(frames) / 5 - cycles) <= 1
    assert [f.cmd_id for f in frames[:5]] == list(cmds.INFO_CMDS)
    assert [f.seq for f in frames] == [i & 0xFF for i in range(len(frames))]
    assert all(cmds.decode(f.cmd_id, f.data) is not None for f in frames)


def test_jam_stream_one_frame_per_packet_and_key_change():
    s = stream.JamStream("Ab3dE9")
    first = payloads(s, 3)
    s.key = "Zz9Yy8"
    second = payloads(s, 2)
    frames = [FrameReassembler().feed(p)[0] for p in first + second]
    assert [f.seq for f in frames] == [0, 1, 2, 3, 4]
    assert [cmds.decode(f.cmd_id, f.data).key for f in frames] == ["Ab3dE9"] * 3 + ["Zz9Yy8"] * 2


def test_take_symbols_whole_packets_capped():
    s = stream.JamStream("Ab3dE9")
    sym, n = stream.take_symbols(s, 10_000, max_packets=2)
    assert n == 2 and len(sym) == 2 * air.PACKET_BITS
    assert set(np.unique(sym)) == {-1.0, 1.0}
    bits = (sym[:air.PACKET_BITS] > 0).astype(np.uint8)
    assert np.packbits(bits).tobytes() == air.build_packet(air.WaveKind.JAM, source.jam_frame("Ab3dE9", 0))
    assert stream.take_symbols(s, 5, max_packets=2)[1] == 1  # 请求再少也至少给一包
