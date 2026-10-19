"""GNU Radio 流图（Python 编写，不用 .grc）。

每块板一个进程：
- tx.py：ZMQ 拉取空口包 → GFSK 调制 → NanoSDR 发射（或写 IQ 文件）
- rx.py：NanoSDR 接收（或读 IQ 文件）→ 移频低通 → 鉴频 → ZMQ 发给 Python

两者都带一个 XMLRPC 控制口，Python 侧可实时改频点、偏移、增益、衰减。
硬件可以换成文件源 / 文件槽，所以 CI 能在没有 SDR 的情况下跑真实的 GNU Radio 块。
"""
