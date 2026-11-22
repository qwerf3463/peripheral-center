"""设备核心层：扫描、WebHID 桥接、后台轮询与各类操作（与界面无关）。

大界面和悬浮窗共用同一个 Core 实例，两边看到的状态始终一致。
状态更新写入 core.state（线程安全读快照），并通过回调 on_change 通知界面。
"""

from __future__ import annotations

import ctypes
import threading
import time

from hidpp_win import LogitechScanner, probe
from bridge_server import BridgeServer
import autostart
from aula_web_cache import read_keyboard_battery, read_mouse_battery


def get_pointer_speed() -> int:
    value = ctypes.c_int(0)
    try:
        ctypes.windll.user32.SystemParametersInfoW(0x0070, 0, ctypes.byref(value), 0)
        return value.value
    except Exception:
        return 0


def set_pointer_speed(speed: int) -> bool:
    speed = max(1, min(20, int(speed)))
    try:
        return bool(
            ctypes.windll.user32.SystemParametersInfoW(
                0x0071, 0, ctypes.c_void_p(speed), 0x0001 | 0x0002
            )
        )
    except Exception:
        return False


class Core:
    def __init__(self):
        self.stop_event = threading.Event()
        self.scanner = LogitechScanner()
        self.bridge = BridgeServer()
        # 已改用「设备直连 + 官方网页驱动缓存」方案，不再启动 WebHID 桥接服务，
        # 避免占用 18765 端口。相关代码保留，需要时可改回 self.bridge.start()。
        self.bridge_ok = False

        self.state: dict = {
            "mouse": None,
            "keyboard": None,
            "status": "正在初始化…",
            "pointer_speed": get_pointer_speed(),
        }
        self.diag_text = "尚未完成扫描。"
        self._lock = threading.Lock()
        self.version = 0  # 状态版本号，界面主线程轮询此值决定是否刷新
        self.worker = None

    def _bump(self) -> None:
        """状态已变化时递增版本号（界面在主线程轮询，避免跨线程操作 UI）。"""
        self.version += 1

    def start(self) -> None:
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set_status(self, text: str) -> None:
        with self._lock:
            self.state["status"] = text
        self._bump()

    # ------------------------- 后台轮询 ------------------------- #

    def _worker(self) -> None:
        try:
            self.scanner.scan()
            self.diag_text = self.scanner.describe()
        except Exception as exc:
            self._set_status(f"扫描失败: {exc}")
            return

        while not self.stop_event.is_set():
            state = {
                "mouse": None,
                "keyboard": None,
                "status": f"直连 {len(self.scanner.devices)} 个设备 · 更新于 {time.strftime('%H:%M:%S')}",
                "pointer_speed": get_pointer_speed(),
            }
            for dev in self.scanner.devices:
                try:
                    percent, charge = dev.read_battery()
                    item = {
                        "name": dev.name,
                        "percent": percent,
                        "charge": charge,
                        "protocol": getattr(dev, "protocol", ""),
                    }
                    if dev.kind == "mouse":
                        state["mouse"] = item
                    elif dev.kind == "keyboard":
                        state["keyboard"] = item
                except Exception:
                    pass

            # 键盘电量回退：SONiX 2.4G 不向 PC 回传电量，但狼蛛官方网页驱动
            # 连接设备后会把状态缓存进浏览器 Local Storage，可直接解析出电量。
            if (state["keyboard"] or {}).get("percent", -1) < 0:
                try:
                    info = read_keyboard_battery()
                    if info and info.get("percent", -1) > 0:
                        kb = dict(state["keyboard"] or {
                            "name": "狼蛛 Mini60 Pro",
                            "protocol": "sonix",
                        })
                        kb["percent"] = int(info["percent"])
                        kb["charge"] = "充电中" if info.get("charging") else "放电中"
                        kb["protocol"] = "web_cache"
                        # age 用「当前时间 - 缓存文件写入时间」计算，
                        # 都是绝对时间戳，所以关机/休眠期间的时间也会算进去。
                        kb["age"] = max(0.0, time.time() - float(info.get("mtime") or time.time()))
                        if info.get("name"):
                            kb["name"] = "狼蛛 " + info["name"].replace(" Dongle", "")
                        state["keyboard"] = kb
                except Exception:
                    pass

            # 鼠标电量回退：迈从 G3 等网页驱动连接后会把电量写进浏览器
            # Local Storage，直连读不到时从缓存取，避免只能看网页。
            if (state["mouse"] or {}).get("percent", -1) < 0:
                try:
                    info = read_mouse_battery()
                    if info and info.get("percent", -1) >= 0:
                        m = dict(state["mouse"] or {
                            "name": info.get("name") or "鼠标",
                            "protocol": "web_cache",
                        })
                        m["percent"] = int(info["percent"])
                        m["charge"] = "充电中" if info.get("charging") else "放电中"
                        # 直连没读到、靠缓存兜底时，保留原协议并标注来源
                        base = m.get("protocol") or "web_cache"
                        m["protocol"] = (base + "+cache" if base != "web_cache"
                                         else "web_cache")
                        m["age"] = max(
                            0.0, time.time() - float(info.get("mtime") or time.time()))
                        if info.get("name"):
                            m["name"] = info["name"]
                        state["mouse"] = m
                except Exception:
                    pass

            with self._lock:
                self.state = state
            self._bump()
            self.stop_event.wait(2)

    # ------------------------- 操作 ------------------------- #

    def bridge_online(self) -> bool:
        if not self.bridge_ok:
            return False
        try:
            return bool(self.bridge.state().get_report().get("online"))
        except Exception:
            return False

    def bridge_url(self) -> str:
        return self.bridge.url

    def bridge_source(self) -> str:
        """当前数据来源：'extension-*' 表示后台扩展，'page' 表示手动网页桥接。"""
        if not self.bridge_ok:
            return ""
        try:
            return self.bridge.state().get_report().get("source") or ""
        except Exception:
            return ""

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.scanner.close()
        except Exception:
            pass
        try:
            self.bridge.stop()
        except Exception:
            pass


def autostart_status() -> tuple[bool, str]:
    return autostart.status()


def autostart_toggle() -> tuple[bool, str, str]:
    """返回 (成功, 标题, 说明)。"""
    on, _ = autostart.status()
    if on:
        ok, msg = autostart.disable()
        return ok, ("已关闭开机自启" if ok else "关闭失败"), (
            "下次登录将不再自动启动。" if ok else msg
        )
    ok, msg = autostart.enable()
    return ok, ("已开启开机自启" if ok else "开启失败"), (
        "下次登录 Windows 时会自动启动（无黑框后台启动）。" if ok else msg
    )
