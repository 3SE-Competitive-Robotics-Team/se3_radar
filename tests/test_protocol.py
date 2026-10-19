import random

import pytest

from rmwave import phy
from rmwave.protocol import air, cmds
from rmwave.protocol.crc import crc8, crc16
from rmwave.protocol.referee import FrameReassembler, encode_frame
from rmwave.tx import source

# 复旦 EGA 复现的官方干扰波 0x0A06 帧（密钥 abcdef，seq 7）
KEY_FRAME_ABCDEF = bytes.fromhex("A5 06 00 07 91 06 0A 61 62 63 64 65 66 16 E2")


def test_crc_against_known_frame():
    assert crc8(KEY_FRAME_ABCDEF[:4]) == 0x91
    assert crc16(KEY_FRAME_ABCDEF[:-2]) == 0xE216


def test_encode_frame_matches_known_frame():
    assert encode_frame(cmds.Cmd.JAM_KEY, b"abcdef", seq=7) == KEY_FRAME_ABCDEF


@pytest.mark.parametrize("cmd,size", [(0x0A01, 24), (0x0A02, 12), (0x0A03, 10), (0x0A04, 8), (0x0A05, 41), (0x0A06, 6)])
def test_cmd_sizes(cmd, size):
    assert cmds.CMD_SIZES[cmd] == size


def test_info_cycle_length():
    assert len(source.info_cycle(source.InfoState())) == 140


def test_jam_frame_fills_one_payload():
    frame = source.jam_frame("abcdef", seq=7)
    assert frame == KEY_FRAME_ABCDEF
    assert len(frame) == air.PAYLOAD_LEN


def test_cmd_roundtrip():
    state = source.random_info_state(random.Random(1))
    for msg in state.messages():
        assert cmds.decode(msg.CMD_ID, msg.pack()) == msg
    assert cmds.decode(0x0A06, b"Ab3dE9").key == "Ab3dE9"


def test_jam_key_rejects_bad_key():
    with pytest.raises(ValueError):
        cmds.JamKey("abc!ef").pack()


def test_air_packet_roundtrip():
    stream = source.info_cycle(source.InfoState()) * 3
    packets = air.packetize(air.WaveKind.INFO, stream)
    assert all(len(p) == air.PACKET_LEN == 27 for p in packets)
    assert packets[0][:8].hex() == "2f6f4c74b914492e"
    assert packets[0][8:12] == b"\x00\x0f\x00\x0f"
    payloads = [air.parse_packet(p, air.WaveKind.INFO) for p in packets]
    assert b"".join(payloads)[:len(stream)] == stream
    assert air.parse_packet(packets[0], air.WaveKind.JAM) is None


def test_reassembler_handles_arbitrary_split_and_noise():
    rng = random.Random(2)
    states = [source.random_info_state(rng) for _ in range(5)]
    stream = b"\xa5\x00" + b"".join(source.info_cycle(s, seq=i * 5) for i, s in enumerate(states))
    r = FrameReassembler()
    frames = []
    i = 0
    while i < len(stream):
        n = rng.randint(1, 20)
        frames += r.feed(stream[i:i + n])
        i += n
    decoded = [cmds.decode(f.cmd_id, f.data) for f in frames]
    expected = [m for s in states for m in s.messages()]
    assert decoded == expected


def test_reassembler_skips_corrupted_frame():
    good = source.info_cycle(source.InfoState())
    bad = bytearray(good)
    bad[20] ^= 0xFF  # 破坏第一帧（0x0A01）的数据段
    r = FrameReassembler()
    frames = r.feed(bytes(bad))
    assert [f.cmd_id for f in frames] == list(cmds.INFO_CMDS[1:])
    assert r.stats.crc16_fail == 1


def test_wave_specs():
    info = phy.wave_spec(phy.Team.RED, 0)
    assert info.kind is air.WaveKind.INFO and info.center_hz == 433.20e6
    assert phy.wave_spec(phy.Team.BLUE, 3).center_hz == 434.32e6
    assert info.sensitivity == pytest.approx(1.5628, abs=1e-4)
    assert phy.wave_spec(phy.Team.RED, 1).sensitivity == pytest.approx(2.8194, abs=1e-4)
    assert phy.wave_spec(phy.Team.RED, 2).sensitivity == pytest.approx(2.5681, abs=1e-4)
    assert phy.wave_spec(phy.Team.RED, 3).sensitivity == pytest.approx(0.6517, abs=1e-4)
