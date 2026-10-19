"""流图进程里的 XMLRPC 控制口。只开放显式列出的 setter，监听本机。"""

import threading
from xmlrpc.server import SimpleXMLRPCServer


def serve(obj, port: int, methods: list[str]) -> SimpleXMLRPCServer:
    server = SimpleXMLRPCServer(("127.0.0.1", port), logRequests=False, allow_none=True)
    for name in methods:
        server.register_function(getattr(obj, name), name)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
