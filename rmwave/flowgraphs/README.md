# GNU Radio 流图

GNU Radio 3.10（Ubuntu 24.04 apt 装的是 3.10.9.2，自带 gr-iio、gr-zeromq）。

流图用 **Python 直接写**，不用 `.grc`：
- `.grc` 是大段 YAML，PR 里几乎没法审；Python 代码能正常 review。
- 硬件可以换成文件源 / 文件槽，CI 能在没有 SDR 的情况下跑真实的 GNU Radio 块（`tests/test_gnuradio.py`）。

每块板一个流图进程，配一个 Python 侧，平时用 `scripts/run_board.py` 一起启动。

## 发射 `tx.py`

```
Python (rmwave.tx.server, ZMQ REP)
   │ ±1 float32 符号，每次最多 2 包
   ▼
zeromq.req_source → interp_fir_filter_fff(sps=47, 矩形*高斯 BT0.35) → frequency_modulator_fc(sensitivity) → ×幅度 → iio sink / file sink
```

- **拉模式**：流图需要数据时才向 Python 要，发射速率由 SDR 采样时钟决定，不会断流。
- **低延迟**：传符号而不是字节，各级缓冲区压到最小一个内存页，每次应答最多 2 包。
  改密钥 / 信息内容后约 0.06 s 上天（默认设置下会在流图里积压约 28 s）。
- 调制与 `rmwave.dsp.gfsk_modulate` 逐样本一致（差异在 float 精度内），测试里有对照。
- XMLRPC：`set_center`、`set_attenuation`、`set_sensitivity`。

## 接收 `rx.py`

```
iio source / file source → freq_xlating_fir_filter_ccc → quadrature_demod_cf(1/sensitivity) → zeromq.pub_sink(float32)
                                                                                                │
                                                                       Python (rmwave.rx.client) ┘ 解析 + AFC
```

和 `rmwave.dsp.ChannelFrontend` 逐步对应（那边在回环和 TCR 赛场实录上验证过）：

| 步骤 | 参数 |
|---|---|
| 移频 + 低通 | 中心偏移 = `offset`（AFC 通过 XMLRPC `set_offset` 实时改）；Blackman 低通，截止 = 带宽/2（信息 270k、L1 470k、L2 430k、L3 125k），过渡带 20 kHz |
| 鉴频 | 增益 = 1/sensitivity，理想输出 ±1 |

- 截止频率不能放宽：三级干扰离信息波只有 400 kHz、功率高 50 dB，靠这个窄滤波器挡住。
- 代价是频偏容限约一个符号率（≈21 kHz）；更大的频偏由 Python 侧 AFC 网格搜索捕获（实测约 0.6 s 锁定）。
- XMLRPC：`set_offset`、`get_offset`、`set_center`、`set_gain`、`set_wave`（换波形 / 等级）。
- `--record` 可同时把原始 IQ 录到文件，方便事后用 `scripts/replay_iq.py` 复盘。

## 端口

在 `config/boards.yaml` 里按板分配，每块板的 ZMQ 和 XMLRPC 端口必须不同。
