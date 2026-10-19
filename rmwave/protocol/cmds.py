"""雷达无线链路命令 0x0A01~0x0A06（通信协议 V2.0.0 表 1-44~1-49）。

所有多字节字段为小端。
"""

import struct
from dataclasses import dataclass, fields


class Cmd:
    POSITION = 0x0A01
    HP = 0x0A02
    AMMO = 0x0A03
    MACRO = 0x0A04
    BUFF = 0x0A05
    JAM_KEY = 0x0A06


# 信息波每个周期依次发送的命令
INFO_CMDS = (Cmd.POSITION, Cmd.HP, Cmd.AMMO, Cmd.MACRO, Cmd.BUFF)


class _Packed:
    """按 FORMAT 顺序把 dataclass 字段打包/解包。"""

    FORMAT = ""
    CMD_ID = 0

    @classmethod
    def size(cls) -> int:
        return struct.calcsize(cls.FORMAT)

    def pack(self) -> bytes:
        return struct.pack(self.FORMAT, *(getattr(self, f.name) for f in fields(self)))

    @classmethod
    def unpack(cls, data: bytes):
        return cls(*struct.unpack(cls.FORMAT, data))


@dataclass
class Position(_Packed):
    """0x0A01：对方机器人坐标，单位 cm。"""

    FORMAT = "<12H"
    CMD_ID = Cmd.POSITION
    hero_x: int = 0
    hero_y: int = 0
    engineer_x: int = 0
    engineer_y: int = 0
    infantry3_x: int = 0
    infantry3_y: int = 0
    infantry4_x: int = 0
    infantry4_y: int = 0
    aerial_x: int = 0
    aerial_y: int = 0
    sentry_x: int = 0
    sentry_y: int = 0


@dataclass
class Hp(_Packed):
    """0x0A02：对方机器人血量，未上场或被罚下为 0。"""

    FORMAT = "<6H"
    CMD_ID = Cmd.HP
    hero: int = 0
    engineer: int = 0
    infantry3: int = 0
    infantry4: int = 0
    reserved: int = 0
    sentry: int = 0


@dataclass
class Ammo(_Packed):
    """0x0A03：对方机器人允许发弹量（步兵含堡垒储备）。"""

    FORMAT = "<5H"
    CMD_ID = Cmd.AMMO
    hero: int = 0
    infantry3: int = 0
    infantry4: int = 0
    aerial: int = 0
    sentry: int = 0


@dataclass
class Macro(_Packed):
    """0x0A04：对方剩余金币、累计总金币、增益点占领状态位图。"""

    FORMAT = "<HHI"
    CMD_ID = Cmd.MACRO
    remaining_coin: int = 0
    total_coin: int = 0
    occupy_bits: int = 0


_BUFF_ROBOTS = ("hero", "engineer", "infantry3", "infantry4", "sentry")
_BUFF_ITEMS = ("heal", "cooldown", "defence", "neg_defence", "attack")


@dataclass
class RobotBuff:
    heal: int = 0         # u8，回血百分比
    cooldown: int = 0     # u16，热量冷却增加值
    defence: int = 0      # u8，防御增益百分比
    neg_defence: int = 0  # u8，负防御增益百分比
    attack: int = 0       # u16，攻击增益百分比


@dataclass
class Buff:
    """0x0A05：对方各机器人增益、哨兵姿态、主要状态，共 41 B。

    主要状态：0 存活 / 1 战亡 / 2 无敌但不虚弱 / 3 无敌且虚弱。
    """

    ROBOT_FORMAT = "<BHBBH"
    CMD_ID = Cmd.BUFF
    robots: dict[str, RobotBuff] = None
    sentry_posture: int = 0
    status: dict[str, int] = None

    def __post_init__(self):
        if self.robots is None:
            self.robots = {name: RobotBuff() for name in _BUFF_ROBOTS}
        if self.status is None:
            self.status = {name: 0 for name in _BUFF_ROBOTS}

    @classmethod
    def size(cls) -> int:
        return len(_BUFF_ROBOTS) * struct.calcsize(cls.ROBOT_FORMAT) + 1 + len(_BUFF_ROBOTS)

    def pack(self) -> bytes:
        out = b"".join(
            struct.pack(self.ROBOT_FORMAT, *(getattr(self.robots[n], i) for i in _BUFF_ITEMS))
            for n in _BUFF_ROBOTS
        )
        out += bytes([self.sentry_posture])
        return out + bytes(self.status[n] for n in _BUFF_ROBOTS)

    @classmethod
    def unpack(cls, data: bytes):
        step = struct.calcsize(cls.ROBOT_FORMAT)
        robots = {
            name: RobotBuff(*struct.unpack_from(cls.ROBOT_FORMAT, data, k * step))
            for k, name in enumerate(_BUFF_ROBOTS)
        }
        off = len(_BUFF_ROBOTS) * step
        status = dict(zip(_BUFF_ROBOTS, data[off + 1:off + 1 + len(_BUFF_ROBOTS)]))
        return cls(robots, data[off], status)


@dataclass
class JamKey:
    """0x0A06：对方干扰波密钥，6 个 ASCII 字母或数字。"""

    CMD_ID = Cmd.JAM_KEY
    key: str = "000000"

    @classmethod
    def size(cls) -> int:
        return 6

    def pack(self) -> bytes:
        raw = self.key.encode("ascii")
        if len(raw) != 6 or not raw.isalnum():
            raise ValueError(f"密钥必须是 6 位字母或数字: {self.key!r}")
        return raw

    @classmethod
    def unpack(cls, data: bytes):
        return cls(data.decode("ascii"))


CMD_TYPES = {t.CMD_ID: t for t in (Position, Hp, Ammo, Macro, Buff, JamKey)}
CMD_SIZES = {cmd: t.size() for cmd, t in CMD_TYPES.items()}


def decode(cmd_id: int, data: bytes):
    """按 cmd_id 解析数据段；未知命令或长度不符返回 None。"""
    t = CMD_TYPES.get(cmd_id)
    if t is None or len(data) != t.size():
        return None
    try:
        return t.unpack(data)
    except (struct.error, UnicodeDecodeError):
        return None
