"""发射端 Python 侧：应答 GNU Radio 发射流图（zeromq.req_source）的数据请求。

协议（gr-zeromq req_source）：流图发一条 4 字节小端 int 表示还能接收多少项（float32 符号），
这里回一条消息，内容是若干整包空口包的 ±1 符号。每次最多回 MAX_PACKETS_PER_REPLY 包，
否则多出来的会在流图里排队，改密钥 / 信息后要过很久才发出去。

用法（与 rmwave.flowgraphs.tx 配对，各开一个终端）：
  python3 -m rmwave.tx.server --level 0                 # 信息波，随机内容
  python3 -m rmwave.tx.server --level 1 --key Ab3dE9    # 干扰波，固定密钥
  python3 -m rmwave.tx.server --level 2 --rotate 30     # 每 30 s 换一个随机密钥
"""

import argparse
import sys
import threading
import time

import zmq

from . import source, stream

DEFAULT_ZMQ = {"info": "tcp://127.0.0.1:53201", "jam": "tcp://127.0.0.1:53202"}
MAX_PACKETS_PER_REPLY = 2


class PacketServer:
    def __init__(self, packet_stream, address: str, ctx: zmq.Context | None = None):
        self.stream = packet_stream
        self.ctx = ctx or zmq.Context.instance()
        self.sock = self.ctx.socket(zmq.REP)
        self.sock.bind(address)
        self.sent_packets = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def serve(self) -> None:
        poller = zmq.Poller()
        poller.register(self.sock, zmq.POLLIN)
        while not self._stop.is_set():
            if not poller.poll(100):
                continue
            req = self.sock.recv()
            want = int.from_bytes(req[:4], "little", signed=True) if len(req) >= 4 else 0
            symbols, n = stream.take_symbols(self.stream, want, MAX_PACKETS_PER_REPLY)
            self.sock.send(symbols.tobytes())
            self.sent_packets += n

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.serve, daemon=True)
        self._thread.start()
        return self._thread

    def close(self) -> None:
        """先等服务线程退出再关 socket，避免线程还在 poll 时 socket 被关掉。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self.sock.close(linger=0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level", type=int, choices=[0, 1, 2, 3], required=True, help="0=信息波，1~3=干扰波")
    ap.add_argument("--key", help="干扰波密钥（6 位字母数字），默认随机")
    ap.add_argument("--rotate", type=float, help="每隔多少秒换一个随机密钥")
    ap.add_argument("--seed", type=int, help="信息波随机内容的种子")
    ap.add_argument("--zmq", help="默认信息波 53201 / 干扰波 53202")
    args = ap.parse_args(argv)

    if args.level == 0:
        s = stream.InfoStream(seed=args.seed)
        addr = args.zmq or DEFAULT_ZMQ["info"]
    else:
        s = stream.JamStream(args.key or source.random_key())
        addr = args.zmq or DEFAULT_ZMQ["jam"]
    srv = PacketServer(s, addr)
    srv.start()
    print(f"tx server {'info' if args.level == 0 else f'jam L{args.level} key={s.key}'} @ {addr}", flush=True)
    try:
        last = time.monotonic()
        while True:
            time.sleep(1)
            if args.rotate and args.level and time.monotonic() - last >= args.rotate:
                s.key = source.random_key()
                last = time.monotonic()
                print(f"新密钥 {s.key}", flush=True)
    except KeyboardInterrupt:
        pass
    srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
