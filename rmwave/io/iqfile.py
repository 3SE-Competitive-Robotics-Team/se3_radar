"""IQ 文件：little-endian complex64（I/Q 各 float32），与 GNU Radio file sink/source 一致。"""

from pathlib import Path

import numpy as np


def read_c64(path: str | Path, offset: int = 0, count: int = -1) -> np.ndarray:
    """offset / count 以复数样本为单位。"""
    return np.fromfile(path, dtype="<c8", count=count, offset=offset * 8)


def write_c64(path: str | Path, iq: np.ndarray) -> None:
    np.asarray(iq, dtype="<c8").tofile(path)
