# 数据

IQ 样本与录波放在这里，不入库。格式统一为 little-endian complex64，1 MS/s，中心频率对准目标信号。

## TCR 赛场实录（独立参照）

TCR 战队公开的 RM2026 赛场单通道录波（Apache-2.0），官方发射源，红方接收：

```bash
bash scripts/fetch_tcr_samples.sh
```

| 文件 | 内容 | TCR 公布结果 | 本仓库解码结果 |
|---|---|---|---|
| `info_wave_2s.c64` | 信息波，对方处于三级干扰 | 97 帧，0x0A01 20 帧 | 96 帧，0x0A01 20 帧 |
| `jam_level1_2s.c64` | 一级干扰波 | 110 个不同序号 | 145 个，密钥 `1SCmbe` |
| `jam_level2_1s.c64` | 二级干扰波 | 57 个不同序号 | 65 个，密钥 `2gL8W5` |

`tests/test_rx_loopback.py` 里的 `test_tcr_*` 用它做回归测试，CI 会自动下载。
