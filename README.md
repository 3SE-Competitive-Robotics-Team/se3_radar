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
  telemetry.py  板子进程发给调试网页的状态 / 帧 / 频谱（ZMQ PUB）
  webui/        调试台：汇总遥测、漏斗诊断，进程管理与控制，HTTP + SSE 推给浏览器
scripts/
  radar.py              一键启动：调试台 + 全部板子（网页上改阵营 / 等级 / 密钥 / 衰减 / 增益）
  run_board.py          按 config 启动一块板（流图 + Python 侧）
  webui.py              调试台（只读）
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

先在 `config/boards.yaml` 里填好每块板的 IP，然后**一键启动**：

```bash
python3 scripts/radar.py
```

Windows 上直接双击仓库根目录的 `start_radar.bat`（经 WSL 运行同一个脚本）。它会按配置拉起全部板子，并打开调试台
http://127.0.0.1:8800；关窗口 / Ctrl-C 即全部停止。

调试台上能直接改，不用再敲命令：

- **阵营、干扰等级**：切换后自动重启受影响的板（换等级只重启干扰波的两块）
- **每块板**：启动 / 停止 / 重启，看进程输出；进程意外退出会按 1 / 2 / 5 / 10 / 30 s 退避自动拉起
- **发射板**：发射衰减（实时生效）、干扰波密钥（指定或随机，换密钥会重启这块板）
- **接收板**：增益模式（自动 slow_attack / fast_attack / hybrid 或手动）和手动增益（实时生效）

调试台的重点是回答「卡在哪一步」：

- **接收板漏斗**：进程 → 采样流（实际速率、断档）→ 电平（峰值，削顶报警）→ 带内信号（SNR）→ 接入码 → 空口包
  → 裁判帧（CRC）→ 结果（信息波 5 条命令 / 干扰波密钥和出密钥用时）。第一处出问题的一级标红，并直接给出排查建议
  （例如「带内没有信号：天线是否接在 RX 口……」「输入削顶：降低接收增益……」）
- **信号质量**：频谱 + 瀑布图（标出红蓝双方各波和 AFC 偏移）、输入电平、按包对齐的眼图、AFC 频偏 120 s 曲线、
  接入码误码分布 / 相关值 / 残余频偏
- **发射板**：发包速率（对比满载 98.5 包/s）、gr-iio 欠载次数、流图报错
- **事件**：板子上下线（ping）、进程启停与自动重启、AFC 锁定 / 失锁、断档、削顶、欠载、报错、解出密钥、参数改动

其他启动方式：

```bash
python3 scripts/radar.py --no-autostart     # 只开调试台，板子在网页上手动启动
python3 scripts/radar.py --sim data/sim     # 没有板子：接收板循环回放 data/sim/<板名>.c64，不启动发射板
python3 scripts/webui.py                    # 只看不控制（板子在别的终端里手动启动时用）
python3 scripts/run_board.py rx_jam --show  # 单独手动跑一块板，参数见 --help
```

调试台默认只监听本机；`--host 0.0.0.0` 能让同一网络的其他电脑访问，但对方也就能控制板子。
发射衰减默认 70 dB，实验室近距离先从大衰减开始。比赛机上不能发射（制作规范 S23）。

遥测端口默认由每块板的 `ctrl` 端口推出（状态 `ctrl+1000`，频谱 `ctrl+2000`，电平 `ctrl+3000`），也可在
`boards.yaml` 里用 `status` / `spectrum` / `level` 写死。

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
