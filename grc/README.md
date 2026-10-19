# GNU Radio 流图

GNU Radio 3.10 + gr-iio。`.grc` 入库，GRC 生成的 `.py` 不入库，用到时重新生成。

## 接口约定（Python ↔ GNU Radio）

| 流图 | 方向 | ZMQ | 数据 |
|---|---|---|---|
| `tx_gfsk.grc` | Python PUB → GRC SUB | `tcp://127.0.0.1:53201`（信息）/ `53202`（干扰） | 打包好的空口包字节（每包 27 B，MSB 先发） |
| `tx_gfsk.grc` | GRC 内部 | — | unpack → NRZ → 高斯 BT0.35 → FM（sensitivity 按 `rmwave.phy`）→ iio sink |
| `rx_demod.grc` | GRC PUB → Python SUB | `tcp://127.0.0.1:53301`（信息）/ `53302`（干扰） | 鉴频输出 float32，1 MS/s |

- 发射参数（频点、sensitivity、衰减）和接收参数（频点、信道低通）由 Python 通过 XMLRPC 下发，流图里只放默认值。
- 离线生成 IQ 时，把 `tx_gfsk.grc` 的 iio sink 换成 file sink（complex64）。
- 每块板一个流图进程，互不影响。

流图本身在下一步实现。
