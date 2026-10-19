# 3se_radar：雷达解析信息波

RoboMaster 雷达「解析信息波 / 干扰波」。硬件为 NanoSDR（Zynq-7010 + AD9363，Pluto 兼容），射频与鉴频用 GNU Radio + gr-iio，之后的处理全部在 Python 里做。

目前按 **26 赛季通信协议 V2.0.0** 开发，27 版协议发布后再更新 `rmwave/protocol/`。

## 第一阶段目标

在实验室模拟赛场：两块 NanoSDR 模拟官方信息波和干扰波发射源，另两块接收并解析。阵营和干扰等级手动选择。验收标准见 [docs/research_open_source.md](docs/research_open_source.md) 第 6 节。

## 结构

```
rmwave/
  protocol/     26 协议，收发共用
    crc.py        CRC8 / CRC16
    referee.py    0xA5 帧编码 + 流式重组
    cmds.py       0x0A01~0x0A06 数据结构
    air.py        27 B 空口包、接入码
  phy.py        频点、带宽、sensitivity、SPS
  tx/           模拟发射源字节流（→ ZMQ → GNU Radio 调制发射）
  rx/           鉴频输出 → 定时/接入码/判决/拼包（待实现）
  io/           IQ 文件与 ZMQ（待实现）
grc/            GNU Radio 流图与接口约定
config/         板子 IP 与角色
docs/           规则摘录、开源调研
tests/          pytest
data/           IQ 样本（不入库）
```

## 环境

开发用 WSL2（Ubuntu），比赛用 Ubuntu。

```bash
pip install -e .[dev]
python3 -m pytest
```

GNU Radio 建议用 radioconda 安装，需要 `gnuradio`、`gr-iio`、`zeromq` 模块。

## 开发流程

**所有代码改动都要走 Pull Request。** `main` 的规则：
- 禁止直接推送、强制推送和删除分支；
- CI（`.github/workflows/pytest.yml`）的 `pytest (3.10)` 和 `pytest (3.12)` 全部通过才能合并。

> 仓库是私有的，组织为免费计划，GitHub 不提供分支保护，以上规则目前靠大家自觉遵守。

1. 从最新的 `main` 拉分支，命名为 `类型/简述`，例如 `feat/rx-sync`、`fix/crc16`、`docs/rules`。
2. 在分支上提交，推送后对 `main` 开 PR。PR 描述写清楚改了什么、为什么改、怎么测的。
3. 合并前要满足：
   - CI 通过（本地可先跑 `python3 -m pytest`）；
   - 至少一名队员 review 通过；
   - 改了协议或物理层参数时，在 PR 里写明出处（规则手册 / 协议版本 / 实测数据）。
4. 合并后删除分支。

## 文档

- [docs/rules_info_wave.md](docs/rules_info_wave.md)：规则、制作规范、通信协议中与信息波相关的部分
- [docs/research_open_source.md](docs/research_open_source.md)：RM2026 各队开源方案调研、物理层参数、架构决策
