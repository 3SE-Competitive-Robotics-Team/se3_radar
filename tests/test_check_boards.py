"""check_boards.py 解析 iio_info 输出（libiio 0.25，PlutoSDR 固件的典型格式）。"""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_boards", Path(__file__).resolve().parents[1] / "scripts/check_boards.py")
check_boards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_boards)

PLUTO_1R1T = """\
IIO context created with network backend.
IIO context has 15 attributes:
\thw_model: Analog Devices PlutoSDR Rev.C (Z7010-AD9363A)
\thw_model_variant: 0
\thw_serial: 1044730a1997000b1f00230089c6a0db96
\tfw_version: v0.38
\tad9361-phy,xo_correction: 40000083
\tad9361-phy,model: ad9363a
IIO context has 5 devices:
\tiio:device0: ad9361-phy
\t\t9 channels found:
\t\t\taltvoltage1: TX_LO (output)
\t\t\tvoltage0:  (input)
\tiio:device3: cf-ad9361-dds-core-lpc (buffer capable)
\t\t6 channels found:
\t\t\tvoltage0:  (output, index: 0, format: le:S16/16>>0)
\t\t\tvoltage1:  (output, index: 1, format: le:S16/16>>0)
\t\t\taltvoltage3: TX1_Q_F2 (output)
\tiio:device4: cf-ad9361-lpc (buffer capable)
\t\t2 channels found:
\t\t\tvoltage0:  (input, index: 0, format: le:S12/16>>0)
\t\t\tvoltage1:  (input, index: 1, format: le:S12/16>>0)
"""


def test_parse_pluto_like_board():
    info = check_boards.BoardInfo("rx_info", "ip:192.168.4.1")
    check_boards.parse_iio_info(PLUTO_1R1T, info)
    assert info.attrs["ad9361-phy,model"] == "ad9363a"
    assert info.attrs["fw_version"] == "v0.38"
    assert info.rx_channels == 1
    assert info.tx_channels == 1


def test_rx_board_without_rx_dma_fails(capsys):
    """实测：一块 v0.39-dirty 固件的板只有 ad9361-phy / xadc / dds-core 三个设备，没有接收 DMA。"""
    tx_only = PLUTO_1R1T.split("\tiio:device4: cf-ad9361-lpc")[0].replace("v0.38", "v0.39-dirty")
    info = check_boards.BoardInfo("rx_info", "ip:192.168.4.1", "rx")
    check_boards.parse_iio_info(tx_only, info)
    info.iio = True
    assert info.rx_channels == 0 and info.tx_channels == 1
    assert not check_boards.report([info])
    out = capsys.readouterr().out
    assert "没有接收 DMA" in out and "-dirty" in out


def test_parse_2r2t_board():
    text = PLUTO_1R1T.replace(
        "\t\t\tvoltage1:  (input, index: 1, format: le:S12/16>>0)\n",
        "\t\t\tvoltage1:  (input, index: 1, format: le:S12/16>>0)\n"
        "\t\t\tvoltage2:  (input, index: 2, format: le:S12/16>>0)\n"
        "\t\t\tvoltage3:  (input, index: 3, format: le:S12/16>>0)\n",
    )
    info = check_boards.BoardInfo("rx_info", "ip:192.168.4.1")
    check_boards.parse_iio_info(text, info)
    assert info.rx_channels == 2
