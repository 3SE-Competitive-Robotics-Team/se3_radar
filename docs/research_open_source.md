# RM2026 雷达解析信息波 开源调研（2026-10-19）

目的：为本仓库（NanoSDR 平台）选定工具链与代码框架。调研范围：RoboMaster 社区「算法/雷达」标签下的 RM2026 开源帖 + 对应 GitHub 仓库。

## 1. 物理层参数（26 赛季规则 V1.4.0 起，多队代码一致、赛场验证）

| 波源 | 红方 fc | 蓝方 fc | 标称带宽 | sensitivity (SPS=47) | 频偏 Δf | 标称功率 |
|---|---|---|---|---|---|---|
| 信息波 | 433.20 MHz | 433.92 MHz | 0.54 MHz | 1.5628 | ≈248.7 kHz | −60 dBm |
| 干扰 L1 | 432.20 MHz | 434.92 MHz | 0.94 MHz | 2.8194 | ≈448.7 kHz | −10 dBm |
| 干扰 L2 | 432.50 MHz | 434.62 MHz | 0.86 MHz | 2.5681 | ≈408.7 kHz | −10 dBm |
| 干扰 L3 | 432.80 MHz | 434.32 MHz | 0.25 MHz | 0.6517 | ≈103.7 kHz | −10 dBm |

- 调制 2-GFSK，BT=0.35，高斯跨度 4 符号，0→−1 / 1→+1，MSB 先发。
- 采样率 1 MS/s，**SPS=47**（≈21.277 kBd）。官方摘要写 SPS=52 / 19.23 kBd，但所有赛场可用代码都用 47。
- Δf = 带宽/2 − Rs，sensitivity = 2π·Δf/fs。
- 官方发射源是 PlutoSDR（深大帖），信息波与干扰波天线间距约 5 cm。
- 赛场实际信息波功率高于标称值，信号与干扰的功率差比规则写的小（同济、江南、TCR 都提到）。

### 空口帧（27 字节，无前导、无白化、无 FEC）
```
[Access Code 8B][00 0F 00 0F][Payload 15B]
信息波 AC = 0x2F6F4C74B914492E
干扰波 AC = 0x16E8D377151C712D
```
- 15 B payload 首尾相连，组成**裁判系统串口字节流**（`A5 | len LE | seq | CRC8 | cmd LE | data | CRC16 LE`）。帧可以跨包，接收端拼接后找 0xA5 并校验 CRC。
  - CRC8：init 0xFF，poly 0x8C（反射）。
  - CRC16：init 0xFFFF，poly 0x8408（反射）。
- 信息波每 100 ms 发 0x0A01(24)/0A02(12)/0A03(10)/0A04(8)/0A05(41) 五帧，约 1400 B/s。
- 干扰波：**每个空口包正好装一帧 0x0A06（15 B）**，seq 每包加 1。这是用本仓库解码器对 TCR 赛场实录验证的结果；复旦复现的 `10×0xCC + 帧 + 0xDD 填充` 与实录不符。0x0A06 data 是 6 个 ASCII 字母或数字，大小写都有（实录密钥如 `1SCmbe`、`2gL8W5`）。
- 赛场实测载波频偏约 1 kHz（TCR 的 AD9361 收官方 Pluto）。

## 2. 开源方案对比

| 战队 | 硬件 | 工具链 | 并行能力 | 成绩/成熟度 | 许可 | 链接 |
|---|---|---|---|---|---|---|
| 同济 SuperPower | **NanoSDR（Zynq7010+AD9363）**+羊角天线 | GNU Radio(gr-iio) + Python + ROS2 Humble，ZMQ/XMLRPC，Web 调试台，WSL2 可用 | 单板 1 MS/s；L1/L2 只解干扰；L3 时干扰+信息同时解（SIC） | 国赛平均易伤 1867.9 s（第 2） | 未确认 | [帖](https://bbs.robomaster.com/article/1942083) / [repo](https://github.com/WeltStalin/sp_rf_2026-OpenSource) |
| 东北 T-DT | 微相 ANTSDR（网口） | GNU Radio 3.10 + **C++ OOT 块** + ROS2 Jazzy C++ 节点，ZMQ | 单板 2 MS/s 宽带，一次采样同时 DDC 出信息+L1+L2 | 2026 全明星雷达，易伤/解析次数第一 | MIT（OOT GPL-3） | [帖](https://bbs.robomaster.com/article/1970119) / [repo](https://github.com/T-DT-Algorithm-2026/RM_sdr) |
| 复旦 EGA | 2× 微相 E310（Pluto 固件） | **MATLAB** + Python(pyserial) | 双板：信息板 + 干扰板，互为故障接管 | 国赛局均易伤 2303.7 s，全国第一 | MIT | [帖](https://bbs.robomaster.com/article/1941833) / [repo](https://github.com/Breeze-by/FDUEGA_RM2026_Waves_Analyze) |
| 江南 SHARK | 2× ANTSDR E310 + 八木（信息）/全向（干扰） | gr-iio 前端 + 纯 Python 协议层，ROS2，Dashboard，IQ 录制回放，LabTX 发射台 | 双板 | 局均易伤 1927 s，宣称稳定三级 | MIT | [帖](https://bbs.robomaster.com/article/1938353) / [repo](https://github.com/JNU-SHARK/shark-radar-radio) |
| 西交 笃行 | 2× PlutoSDR + 433 全向 | GNU Radio 3.10.12 + embedded py，PyQt5，双机 TCP | 双板，L3 未实现 | 每局易伤 >2070 s | MIT | [帖](https://bbs.robomaster.com/article/1940695) / [repo](https://github.com/XJTU-RMV/Radar26) |
| 嘉庚 TCR（上游南理工 Combat） | 1× AD9361/Pluto | **纯 Python numpy** + pyadi-iio，Tk GUI，pytest | 单板分时：L1→L2→信息波 | 实战验证，**公开赛场 IQ 样本** | Apache/混合 | [帖](https://bbs.robomaster.com/article/1938244) / [repo](https://github.com/coach114514/TCR-RM2026-Radar-OpenSource) |
| 西工大 WMJ | PlutoSDR + 八木 | GNU Radio（参考 Combat、PIONEER） | 单板 | 赛后可在 L3 下解析 | — | [帖](https://bbs.robomaster.com/article/1938208) / [repo](https://github.com/zplszz/WMJRadar) |
| 深大 RobotPilots | ZC706 + AD9361 | 纯 FPGA Verilog | 4 路并行 | 本人结论："Pluto+GNU Radio 才是版本答案" | CC BY-NC-SA | [帖](https://bbs.robomaster.com/article/1970483) |
| FZSD | 1× Pluto | GNU Radio(gr-soapy) + Python | 单板分时 | 无实战数据，0x0A05 按旧长度 36 | 无 | [repo](https://github.com/MTQDY/FZSD_Rader_SDR) |

早期参考：华南师范 PIONEER（[1882741](https://bbs.robomaster.com/article/1882741)），北师港浸大 Navigator 射频开发小记（[1885744](https://bbs.robomaster.com/article/1885744)）。

## 3. 关键经验（多队共识）

1. **GNU Radio + gr-iio 是主流**，上手快。高级抗干扰算法（SIC、FPGA）收益有限，同济、复旦都说"很多高级算法反而有负面作用"。
2. **解调链路基本一致**：DDC/信道低通 → quadrature demod → （高斯匹配或低通）→ 定时恢复（M&M / 零交叉 TED，或 T-DT 的"相关峰 + 选相位 + 取中段均值"）→ 判决 → 64 bit AC 汉明距离 ≤2~4 → 15 B 拼流 → A5/CRC。
3. **协议层与射频层解耦**：协议层用纯 Python/C++ 实现，可以脱离硬件、用录制的 IQ 离线测试（SHARK、同济、TCR 都这么做）。
4. **IQ 录制 + 回放是必备工具**。TCR 公开了赛场 IQ 样本（complex64，约 40 MB），可以直接用来做回归测试。
5. **自制发射台**：用一块 Pluto/NanoSDR 按规则参数发射，实验室闭环调试（同济、SHARK、TCR、复旦都有）。
6. **硬件坑**：
   - 纯 USB 供电的 Pluto 类板长时间取流会掉设备，有队伍反馈烧板（SHARK）。比赛机优先用网口板，或给 USB 加独立供电。
   - **天线极化比增益重要**，失配会损失十几 dB。信息波用定向八木正对发射源。
   - ADC 饱和会导致解不出，需要 AGC 或自适应增益。同济的实测稳定范围是 10 cm ~ 2 m。
7. **密钥上报**：0x0301 / 0x0121 发给 0x8080，password_cmd=2，提交间隔 ≥10 s。读 0x020E bit3-4 跟随等级切换解调目标。

## 4. NanoSDR 板

- 淘宝 / HamGeek 在售的 "PlutoSDR 兼容 NanoSDR"：Zynq-7010 + AD9363（可能随机为 AD80305），预刷 Pluto 固件。
- 只有 Type-C USB2，通过 RNDIS 虚拟网卡访问，默认 `ip:192.168.2.1`。多块板并用时要改板内 `config.txt` 的 IP。
- 驱动支持 libiio / pyadi-iio / gr-iio / MATLAB Pluto 支持包。
- 同济实测连续取流只用到 1 MS/s。红方信息波 433.2 与 L1 432.2 相距 1 MHz，单板 1 MS/s 无法同时覆盖信息波和 L1/L2，只能和 L3（相距 400 kHz）同时覆盖。2 MS/s 以上能否稳定取流需要实测。
- 市售通用产品：公开销售、价格远低于 5000 元，按条文看符合豁免，但组委会没有明文答疑。

## 5. 已定决策（2026-10-19）

- 多块 NanoSDR，按下表分工。
- 协议层用 **Python**；射频与解调用 GNU Radio 3.10 + gr-iio，两者之间用 ZMQ。
- 开发环境用 Windows + **WSL2**（mirrored 网络），比赛机用 **Ubuntu**。
- **发射端（同济做法）**：Python 生成协议字节流和 27 B 空口包，经 ZMQ PUB 交给 GNU Radio。由 GNU Radio 做 GFSK 调制，再从 NanoSDR（gr-iio sink）发出。离线 IQ 文件由同一个流图换成 file sink 生成，保证文件里的波形和实际发射的一致。
- **接收端分界**：GNU Radio 只做到鉴频（gr-iio source → 移频 / 信道低通 → quadrature demod），浮点鉴频输出经 ZMQ 交给 Python。定时恢复、接入码搜索、判决、拼包、CRC 都在 Python（numpy）里做，可以直接读 IQ 文件离线回放。
- **裁判串口由解析波程序独占**（参考 SHARK 做法）。雷达主程序不开串口，把视觉/定位坐标发给解析波程序，由它统一限速后发送。

### 整体架构（草案）
```
NanoSDR RX-INFO ─┐                          ┌─> 0x0305 小地图坐标（≤5Hz）
NanoSDR RX-JAM ──┼─ GNU Radio 流图 ──ZMQ──> Python 主进程 ─┼─> 0x0301/0x0121 密钥上报、双倍易伤
(RX-SPARE) ──────┘   (gr-iio, 解调)          │  ├ 拼包/CRC/0x0A 解析  └─> 0x0301 队友转发（可选）
                                             │  ├ 状态机（读 0x0001/0x0201/0x020E）
雷达主程序（视觉/定位）──本机 UDP/ZMQ──────────>│  ├ 坐标融合：无线电优先，视觉回退
                                             │  └ 串口调度（0x0301 总上行 ≤30Hz）
                                             └─> 回传给主程序：对方坐标/血量/等级（可选）
```
- 坐标融合规则（SHARK/T-DT 共识）：对方坐标无线电优先，超时回退视觉；己方坐标只能来自视觉；按数据时效切换，不做加权平均。
- 与主程序的接口用本机 UDP 或 ZMQ + 固定格式消息，不强依赖 ROS2。主程序如果是 ROS2，再加一个薄桥接节点。

## 6. 第一阶段：模拟赛场发送与解析（讨论稿）

目标：在实验室里复现官方发射源，并用我们的接收端解出来，形成闭环。不接比赛机、不接主程序。

### 组成
| 部分 | 内容 |
|---|---|
| 发射端（模拟官方发射源） | 按 26 协议生成字节流：信息波每 100 ms 发 0x0A01–0x0A05，干扰波每包一帧 0x0A06、seq 递增。切成 27 B 空口包，做 GFSK 调制（BT 0.35、SPS 47、各档 sensitivity），在对应频点发射，功率可调 |
| 接收端 | 信息板加干扰板，解出 0x0A01–0x0A06，统计丢包、AC 失败、CRC 失败 |
| 板子分配 | 4 块 NanoSDR：TX-INFO、TX-JAM、RX-INFO、RX-JAM。发射端两块板对应官方的两个发射源 |
| 等级控制 | **第一阶段不做模拟裁判**。干扰等级（L1/L2/L3）和阵营由人手动选择，收发两端各自配置 |
| 离线通道 | 发射端也能直接输出 IQ 文件，接收端读文件解码，不需要硬件 |

### 防"对称错误"
发射和接收共用同一份协议代码时，两边如果犯同一个错误，回环照样能通过。因此必须额外用 **TCR 公开的赛场 IQ 样本**验证接收端。这份样本是官方发射源的真实录波，是唯一独立的参照。

### 验收标准（草案）
1. 软件回环（纯 IQ，不经硬件）：0 误包。
2. 回放 TCR 赛场 IQ：解出的有效帧数不少于他们公布的结果（L1 211、L2 112、信息波 97）。
3. 硬件回环，按标称功率（信息 −60 / 干扰 −10 dBm 等效）、距离约 1~2 m：信息波丢包率 <10%；L1、L2、L3 各档在 **14 秒**内解出密钥（参考复旦三级场均约 14 s，实测后再调）。
4. 手动切到 L3（干扰离信息波 400 kHz，最难的一档）时，信息波仍然可解。

### 注意
- 发射只能在实验室进行；比赛机上 SDR 的 TX 必须关掉（S23）。
- 433 MHz ISM 频段，发射功率尽量调低。

## 7. 本队硬件分工（多块 NanoSDR，参考复旦/SHARK/西交双板）

| 角色 | 频点 | 天线 | 说明 |
|---|---|---|---|
| RX-INFO | 对方信息波，固定（红 433.20 / 蓝 433.92） | 定向八木，极化对准 | 全场持续解信息波，不受干扰等级限制 |
| RX-JAM | 跟随 0x020E bit3-4 切到 L1/L2/L3 | 全向 | 解密钥，上报 0x0121 |
| RX-SPARE（可选） | 热备 | — | 任一 RX 掉线时接管（参考复旦 failover），或 L3 时与信息波做 SIC |
| TX-LAB | 按规则参数发射信息波/干扰波 | 短天线 | **只在实验室用**，比赛机禁止发射（S23） |

- 每块板在 `config.txt` 里改成不同子网（如 192.168.2.1 / 192.168.3.1 / 192.168.4.1），按 IP 区分角色。
- USB2 供电和带宽都偏紧：用带独立供电的 USB Hub 或分开的 USB 控制器，并做长时间取流测试（SHARK 的教训）。
- 待实测：单板连续取流的稳定采样率上限（1 / 2 / 4 MS/s）、每块板是 1R 还是 2R。
