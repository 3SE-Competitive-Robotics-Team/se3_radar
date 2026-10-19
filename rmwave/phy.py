"""物理层参数（26 赛季规则 V1.4.0 起，多队赛场验证）。

sensitivity = 2π·Δf / fs，Δf = 带宽/2 − 符号率。
官方摘要写 SPS=52，但赛场可用的代码都用 47，这里以 47 为准。
"""

import math
from dataclasses import dataclass
from enum import Enum

from .protocol.air import WaveKind

SAMPLE_RATE = 1_000_000
SPS = 47
SYMBOL_RATE = SAMPLE_RATE / SPS
GAUSSIAN_BT = 0.35
GAUSSIAN_SPAN = 4  # 高斯滤波跨度，单位：符号


class Team(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass(frozen=True)
class WaveSpec:
    kind: WaveKind
    level: int          # 信息波为 0，干扰波为 1~3
    center_hz: float
    bandwidth_hz: float
    power_dbm: float    # 规则手册标称功率

    @property
    def deviation_hz(self) -> float:
        return self.bandwidth_hz / 2 - SYMBOL_RATE

    @property
    def sensitivity(self) -> float:
        return 2 * math.pi * self.deviation_hz / SAMPLE_RATE


# (红方频点, 蓝方频点, 带宽, 功率)
_TABLE = {
    0: (433.20e6, 433.92e6, 0.54e6, -60.0),
    1: (432.20e6, 434.92e6, 0.94e6, -10.0),
    2: (432.50e6, 434.62e6, 0.86e6, -10.0),
    3: (432.80e6, 434.32e6, 0.25e6, -10.0),
}


def wave_spec(team: Team, level: int = 0) -> WaveSpec:
    """level=0 为信息波，1~3 为对应等级的干扰波。

    team 是发射源所在的雷达基座，也就是接收这路信号的雷达所属阵营：
    红方基座的发射源发的是蓝方信息，由红方雷达接收（规则 5.7.4）。
    """
    red, blue, bw, power = _TABLE[level]
    kind = WaveKind.INFO if level == 0 else WaveKind.JAM
    return WaveSpec(kind, level, red if team is Team.RED else blue, bw, power)
