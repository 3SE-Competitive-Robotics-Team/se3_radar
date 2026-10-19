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
  dsp.py        与 GNU Radio 对齐的 numpy 调制 / 接收前端（回环测试、离线回放用）
  tx/
    source.py     模拟发射源的帧内容
    stream.py     按空中时间生成信息波 / 干扰波包流
    server.py     应答发射流图的 ZMQ 请求
  rx/
    demod.py      鉴频输出 → 接入码同步、判决 → 空口包
    decoder.py    空口包 → 裁判帧 → 0x0A 命令；AFC；离线 decode_iq
    client.py     订阅接收流图的鉴频输出，解析并回写 AFC
  flowgraphs/   GNU Radio 收发流图（Python 编写），说明见其 README
  io/           IQ 文件读写
scripts/
  run_board.py          按 config 启动一块板（流图 + Python 侧）
  replay_iq.py          离线回放 IQ 文件
  fetch_tcr_samples.sh  下载 TCR 赛场实录 IQ（测试用）
config/         板子 IP、角色、端口
docs/           规则摘录、开源调研
tests/          pytest
data/           IQ 样本（不入库）
```

## 环境

开发用 WSL2（Ubuntu 24.04），比赛用 Ubuntu。GNU Radio 用系统 apt 装（3.10，自带 gr-iio、gr-zeromq），
Python 依赖也走系统包，这样 GNU Radio 的 Python 绑定和本项目在同一个解释器里：

```bash
sudo apt install gnuradio python3-zmq python3-yaml python3-numpy python3-pytest libiio-utils
bash scripts/fetch_tcr_samples.sh   # 可选，没有时赛场实录测试会跳过
python3 -m pytest
```

不装 GNU Radio 也能跑除流图以外的全部测试（`pip install -e .[dev]`），流图测试会自动跳过。

## 实验室运行（第一阶段）

先在 `config/boards.yaml` 里填好每块板的 IP，再各开一个终端：

```bash
python3 scripts/run_board.py tx_info               # 模拟信息波发射源
python3 scripts/run_board.py tx_jam --key Ab3dE9   # 模拟干扰波发射源（等级取配置里的 jam_level）
python3 scripts/run_board.py rx_info               # 收信息波，每秒打印统计
python3 scripts/run_board.py rx_jam --show         # 收干扰波，打印每一帧和解出的密钥
```

`--team`、`--level` 可临时覆盖配置。没有硬件时加 `--sim 文件`：发射写 IQ 文件、接收读 IQ 文件，用来检查整套流程。
发射衰减默认 70 dB，实验室近距离先从大衰减开始。比赛机上不能发射（制作规范 S23）。

## 开发流程

**所有代码改动都要走 Pull Request。** `main` 由规则集 `protect-main` 保护（Settings → Rules → Rulesets），管理员同样受限：
- 禁止直接推送、强制推送和删除分支；
- CI（`.github/workflows/pytest.yml`）全部通过才能合并：`pytest (3.10)`、`pytest (3.12)`（不含 GNU Radio）和 `gnuradio`（装 GNU Radio 跑全部测试，含流图）。
- 目前不要求审批人数（单人开发时会卡住自己的 PR）；队员多了把 Required approvals 改成 1。

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
