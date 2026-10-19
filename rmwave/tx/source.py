"""官方发射源每个 100 ms 周期的字节流。

信息波：0x0A01~0x0A05 五帧首尾相连，共 140 B。
干扰波：10×0xCC + 0x0A06 帧(15 B) + 0xDD 填充到 135 B（取自复旦 EGA 对官方波形的复现）。
"""

import random
import string
from dataclasses import dataclass, field

from ..protocol import cmds
from ..protocol.referee import encode_frame

JAM_PREFIX = bytes([0xCC]) * 10
JAM_FILL = 0xDD
JAM_CYCLE_LEN = 135
KEY_ALPHABET = string.ascii_letters + string.digits


def random_key(rng: random.Random | None = None) -> str:
    rng = rng or random
    return "".join(rng.choice(KEY_ALPHABET) for _ in range(6))


@dataclass
class InfoState:
    """信息波携带的对方状态。第一阶段由模拟器填充，内容本身不重要。"""

    position: cmds.Position = field(default_factory=cmds.Position)
    hp: cmds.Hp = field(default_factory=cmds.Hp)
    ammo: cmds.Ammo = field(default_factory=cmds.Ammo)
    macro: cmds.Macro = field(default_factory=cmds.Macro)
    buff: cmds.Buff = field(default_factory=cmds.Buff)

    def messages(self):
        return (self.position, self.hp, self.ammo, self.macro, self.buff)


def info_cycle(state: InfoState, seq: int = 0) -> bytes:
    return b"".join(
        encode_frame(m.CMD_ID, m.pack(), (seq + i) & 0xFF) for i, m in enumerate(state.messages())
    )


def jam_cycle(key: str, seq: int = 0) -> bytes:
    frame = encode_frame(cmds.Cmd.JAM_KEY, cmds.JamKey(key).pack(), seq)
    body = JAM_PREFIX + frame
    return body + bytes([JAM_FILL]) * (JAM_CYCLE_LEN - len(body))


def random_info_state(rng: random.Random | None = None) -> InfoState:
    """场地 28 m × 15 m 内随机坐标和随机数值，用于联调。"""
    rng = rng or random
    pos = cmds.Position(*(rng.randint(0, 2800) if i % 2 == 0 else rng.randint(0, 1500) for i in range(12)))
    hp = cmds.Hp(*(rng.randint(0, 600) for _ in range(4)), 0, rng.randint(0, 600))
    ammo = cmds.Ammo(*(rng.randint(0, 500) for _ in range(5)))
    macro = cmds.Macro(rng.randint(0, 3000), rng.randint(0, 6000), rng.getrandbits(16))
    buff = cmds.Buff()
    for rb in buff.robots.values():
        rb.heal, rb.cooldown, rb.defence, rb.attack = rng.randint(0, 10), rng.randint(0, 50), rng.randint(0, 50), rng.randint(0, 100)
    buff.sentry_posture = rng.randint(1, 6)
    buff.status = {k: rng.randint(0, 3) for k in buff.status}
    return InfoState(pos, hp, ammo, macro, buff)
