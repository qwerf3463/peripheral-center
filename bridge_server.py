"""本地桥接服务：托管 WebHID 页面 + 与页面双向交换设备数据。

浏览器页面通过 WebHID 读取鼠标/键盘电量与 DPI（能访问被系统独占的接口），
再 POST 到本服务；悬浮窗从本服务取数据。DPI 设置则反向：悬浮窗写入命令队列，
页面轮询取走并通过 WebHID 写入设备。

纯标准库实现，无第三方依赖。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paths

WEB_DIR = paths.resource("web")


class BridgeState:
    """线程安全的服务端状态。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.report = {          # 网页 / 后台扩展上报的最新设备数据
            "mouse": None,       # {"percent": int, "dpi": int}
            "keyboard": None,    # {"percent": int}
            "ts": 0.0,
            "source": "",        # "extension-*" = 后台扩展；"page" = 手动打开的网页桥接
        }
        self.dpi_command = None  # 悬浮窗写入的 DPI 命令 {"set_dpi": int}
        self.page_online = False

    def set_report(self, data: dict) -> None:
        with self.lock:
            import time
            self.report = {
                "mouse": data.get("mouse"),
                "keyboard": data.get("keyboard"),
                "ts": time.time(),
                "source": str(data.get("source") or "page"),
            }
            self.page_online = True

    def get_report(self) -> dict:
        with self.lock:
            import time
            fresh = (time.time() - self.report.get("ts", 0)) < 8
            return {
                "mouse": self.report.get("mouse") if fresh else None,
                "keyboard": self.report.get("keyboard") if fresh else None,
                "online": self.page_online and fresh,
                "source": (self.report.get("source") or "page") if fresh else "",
                "ts": self.report.get("ts", 0),
            }

    def put_dpi(self, value: int) -> None:
        with self.lock:
            self.dpi_command = {"set_dpi": int(value)}

    def take_dpi(self) -> dict | None:
        with self.lock:
            cmd = self.dpi_command
            self.dpi_command = None
            return cmd


STATE = BridgeState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 浏览器扩展页面 (chrome-extension://...) 属于跨源请求，且访问
        # 127.0.0.1 会触发 Chrome 的 Private Network Access 预检，必须显式放行。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/bridge", "/bridge.html"):
            try:
                with open(os.path.join(WEB_DIR, "bridge.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, b"bridge.html not found", "text/plain; charset=utf-8")
        elif path == "/api/command":
            self._json(200, STATE.take_dpi() or {})
        elif path == "/api/status":
            self._json(200, STATE.get_report())
        elif path == "/api/ping":
            self._json(200, {"ok": True})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if path == "/api/report":
            try:
                STATE.set_report(json.loads(raw.decode("utf-8")))
                self._json(200, {"ok": True})
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
        elif path == "/api/command":
            try:
                data = json.loads(raw.decode("utf-8"))
                if "set_dpi" in data:
                    STATE.put_dpi(data["set_dpi"])
                self._json(200, {"ok": True})
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
        elif path == "/api/command/ack":
            self._json(200, {"ok": True})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain; charset=utf-8")


class BridgeServer:
    def __init__(self, port: int = 18765):
        self.port = port
        self.httpd = None
        self.thread = None

    def start(self) -> bool:
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.thread.start()
            return True
        except Exception:
            self.httpd = None
            return False

    def stop(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @staticmethod
    def state() -> BridgeState:
        return STATE


if __name__ == "__main__":
    srv = BridgeServer()
    if srv.start():
        print(f"桥接服务已启动: {srv.url}")
        print("按 Ctrl+C 停止")
        try:
            srv.thread.join()
        except KeyboardInterrupt:
            srv.stop()
    else:
        print("启动失败：端口被占用")
