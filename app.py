"""外设控制中心：大界面 + 悬浮窗 + 迷你窗（共享同一份设备状态）。

运行：       D:/python/python.exe app.py           （默认悬浮窗，置中）
大界面启动： D:/python/python.exe app.py --main
悬浮窗启动： D:/python/python.exe app.py --float
迷你窗启动： D:/python/python.exe app.py --mini
指定页面：   D:/python/python.exe app.py --main --page=settings
诊断：       D:/python/python.exe app.py --check
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import math
import os
import re
import sys
import time
import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from tkinter import filedialog, messagebox

from core import Core, autostart_toggle
from hidpp_win import probe
import autostart
import aula_web_cache
import backdrop
import config
import paths


def _build_stamp() -> str:
    """构建号：优先用打包时写入的固定值，避免文件时间被复制改掉。"""
    try:
        from _build_id import BUILD_ID
        if BUILD_ID:
            return str(BUILD_ID)
    except Exception:
        pass
    target = sys.executable if paths.is_frozen() else os.path.abspath(__file__)
    try:
        return time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(os.path.getmtime(target)))
    except Exception:
        return "未知"


# 判断「像不像外设驱动」的关键字：只认外设品牌/品类，避免把显卡、开发工具混进来
_DRIVER_KEYWORDS = (
    "logitech", "lghub", "ghub", "razer", "synapse", "ikf", "eweadn",
    "aula", "mchose", "atk", "hyperx", "steelseries", "corsair", "icue",
    "armoury", "keychron", "glorious", "pulsar", "vgn", "cherry", "dareu",
    "狼蛛", "迈从", "前行者", "罗技", "雷蛇", "雷柏", "达尔优", "双飞燕",
    "鼠标", "键盘", "耳机", "mouse", "keyboard", "headset",
)


# 下拉列表里的品牌 → 识别其驱动软件的关键字
_BRAND_KEYWORDS = {
    "狼蛛": ("aula", "狼蛛"),
    "罗技": ("logitech", "lghub", "ghub", "罗技"),
    "ATK": ("atk",),
    "迈从": ("mchose", "迈从"),
    "前行者": ("eweadn", "前行者"),
}


def _installed_apps() -> list[tuple[str, str, str]]:
    """所有已安装程序 (名称, 版本, 安装位置)。"""
    import winreg

    found: list[tuple[str, str, str]] = []
    roots = (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for root, path in roots:
        try:
            parent = winreg.OpenKey(root, path)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(parent)[0]
        except OSError:
            count = 0
        for idx in range(count):
            try:
                key = winreg.OpenKey(parent, winreg.EnumKey(parent, idx))
            except OSError:
                continue

            def value(name: str) -> str:
                try:
                    return str(winreg.QueryValueEx(key, name)[0]).strip()
                except OSError:
                    return ""

            name = value("DisplayName")
            if not name:
                continue
            found.append((name, value("DisplayVersion"),
                          value("InstallLocation")))

    # 同名同版本只保留一条（三个注册表位置常有重复）
    seen: set[tuple[str, str]] = set()
    apps: list[tuple[str, str, str]] = []
    for item in found:
        key = (item[0].lower(), item[1])
        if key in seen:
            continue
        seen.add(key)
        apps.append(item)
    return apps


def _running_processes() -> list[tuple[str, str]]:
    """正在运行的进程 (名称, PID)。"""
    import subprocess

    try:
        proc = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, timeout=5,
            creationflags=0x08000000,  # CREATE_NO_WINDOW，避免弹黑框
        )
    except Exception:
        return []
    # tasklist 输出是系统 ANSI 编码（GBK），按字节取再忽略解码错误
    raw = proc.stdout or b""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "ignore")
    else:
        text = str(raw)
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if not parts or not parts[0]:
            continue
        rows.append((parts[0], parts[1] if len(parts) > 1 else "?"))
    return rows


def _match_brand(brand: str, text: str) -> bool:
    keywords = _BRAND_KEYWORDS.get(brand, (brand.lower(),))
    low = text.lower()
    return any(k in low for k in keywords)


def _brand_driver_lines(apps, procs) -> list[str]:
    """按下拉列表里的品牌逐个检查：装没装驱动、驱动在不在运行。"""
    lines: list[str] = []
    for brand in KB_BRAND_URLS:
        hits = [a for a in apps if _match_brand(brand, a[0])]
        running = [p for p in procs if _match_brand(brand, p[0])]
        if hits:
            for name, ver, loc in hits:
                line = f"  [{brand}] {name}"
                if ver:
                    line += "  " + ver
                if loc:
                    line += f"  [{loc}]"
                lines.append(line)
        else:
            lines.append(f"  [{brand}] 未安装 / 未检测到")
        if running:
            lines.append("      运行中: " + ", ".join(
                f"{n}(PID {pid})" for n, pid in running))
    return lines


def _other_driver_lines(apps) -> list[str]:
    """品牌列表之外、但看起来像外设驱动的软件（如 iKF Hub）。"""
    rows: list[str] = []
    for name, ver, loc in apps:
        low = name.lower()
        if not any(k in low for k in _DRIVER_KEYWORDS):
            continue
        if any(_match_brand(b, name) for b in KB_BRAND_URLS):
            continue
        line = "  " + name
        if ver:
            line += "  " + ver
        if loc:
            line += f"  [{loc}]"
        rows.append(line)
    return sorted(set(rows), key=str.lower)


def _downloads_dir() -> str:
    """当前用户的「下载」文件夹，读注册表兼容重定向；失败则回退 ~/Downloads。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        path, _ = winreg.QueryValueEx(
            key, "{374DE290-123F-4565-9164-39C4925E467B}")
        path = os.path.expandvars(path)
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    fallback = os.path.join(os.path.expanduser("~"), "Downloads")
    return fallback if os.path.isdir(fallback) else paths.app_dir()


# ---------------------------------------------------------------------------
# 配色：深色 / 浅色（全白）两套主题
# 这些全局变量由 apply_theme() 按当前主题重设；控件创建后颜色已固定，
# 切换主题时由 apply_theme() 遍历控件树统一改色。
# ---------------------------------------------------------------------------
TRANSPARENT = "#010203"     # 圆角抠图用的透明色，越不常见越好（不随主题变）

# 键盘品牌 → 官方网页驱动地址。下拉选项、按钮文字、打开地址都从这里取，
# 以后加新品牌只需在这里加一行。
KB_BRAND_URLS = {
    "狼蛛": "https://hub.aulacn.com/checkDevice",
    "罗技": "https://ghub.logitechg.com.cn/",
    "ATK": "https://hub.atk.pro/",
    "迈从": "https://www.mchose.com.cn/#/connectDevice",
    "前行者": "https://new-hub.eweadn.cn/",
}
KB_BRANDS = tuple(KB_BRAND_URLS.keys())

# 背景动画最高帧率：120Hz 时基（约 8.33ms/帧），同时作为源帧率上限。
# 素材本身低于 120fps 时按素材原速播放，不会快进；高于 120fps 时截到 120。
BG_MAX_FPS = 120.0
BG_TICK_SECONDS = 1.0 / BG_MAX_FPS

# 播放动图时用来提升计时器精度（最高 120fps）
try:
    _WINMM = ctypes.WinDLL("winmm") if os.name == "nt" else None
except Exception:
    _WINMM = None
_TIMER_PERIOD_ON = False

_PALETTE_DARK = {
    "CARD_BG": "#1f2937", "PAGE_BG": "#111827",
    "TEXT_MAIN": "#f3f4f6", "TEXT_SUB": "#9ca3af",
    "ACCENT": "#60a5fa", "SOFT_BG": "#374151", "SOFT_BG2": "#2b3441",
    "BORDER": "#374151", "GREEN": "#16a34a", "ORANGE": "#f59e0b",
    "RED": "#dc2626",
    "SEL_BG": "#1e3a5f", "SEL_FG": "#93c5fd",   # 选中态
    "NAV_BG": "#2a3550", "PURPLE_BG": "#2e2a3f", "PURPLE_FG": "#a78bfa",
    "GREEN_BG": "#1e3a2a", "GREEN_FG": "#86efac",
}
_PALETTE_LIGHT = {
    "CARD_BG": "#ffffff", "PAGE_BG": "#f5f6f8",
    "TEXT_MAIN": "#1f2937", "TEXT_SUB": "#6b7280",
    "ACCENT": "#2563eb", "SOFT_BG": "#e5e7eb", "SOFT_BG2": "#eef0f3",
    "BORDER": "#d1d5db", "GREEN": "#16a34a", "ORANGE": "#d97706",
    "RED": "#dc2626",
    "SEL_BG": "#dbeafe", "SEL_FG": "#1d4edb",
    "NAV_BG": "#dbeafe", "PURPLE_BG": "#f3e8ff", "PURPLE_FG": "#7c3aed",
    "GREEN_BG": "#dcfce7", "GREEN_FG": "#15803d",
}

# 当前生效配色（默认深色，App 启动时按配置重设）
CARD_BG = _PALETTE_DARK["CARD_BG"]
PAGE_BG = _PALETTE_DARK["PAGE_BG"]
TEXT_MAIN = _PALETTE_DARK["TEXT_MAIN"]
TEXT_SUB = _PALETTE_DARK["TEXT_SUB"]
ACCENT = _PALETTE_DARK["ACCENT"]
SOFT_BG = _PALETTE_DARK["SOFT_BG"]
SOFT_BG2 = _PALETTE_DARK["SOFT_BG2"]
BORDER = _PALETTE_DARK["BORDER"]
GREEN = _PALETTE_DARK["GREEN"]
ORANGE = _PALETTE_DARK["ORANGE"]
RED = _PALETTE_DARK["RED"]
SEL_BG = _PALETTE_DARK["SEL_BG"]
SEL_FG = _PALETTE_DARK["SEL_FG"]

# 旧色 -> 新色的映射（切换主题时遍历控件替换）
_COLOR_MAP: dict[str, str] = {}

# 颜色 -> 配色角色（玻璃模式下还原主题色用）
_COLOR_ROLE_ALIASES = {
    "#2a3550": "NAV_BG",
    "#1e3a5f": "SEL_BG",
    "#2e2a3f": "PURPLE_BG",
    "#1e3a2a": "GREEN_BG",
}


def current_palette() -> dict:
    """当前生效的配色表。"""
    return _PALETTE_LIGHT if _CURRENT_THEME == config.THEME_LIGHT else _PALETTE_DARK


def role_of_color(color: str) -> str | None:
    """把具体颜色反查成配色角色（card / page / soft…）。"""
    if not color:
        return None
    c = color.lower()
    for role, val in current_palette().items():
        if val.lower() == c:
            return role
    return _COLOR_ROLE_ALIASES.get(c)


def color_of_role(role: str | None, fallback: str = "") -> str:
    """按角色取当前主题下的颜色。"""
    if not role:
        return fallback
    pal = current_palette()
    return pal.get(role, fallback)

# 设计稿基准尺寸（96 DPI）
BASE_MAIN = (1120, 680)
BASE_FLOAT = (380, 182)
MIN_FLOAT = (300, 150)   # 悬浮窗可拖动缩放的最小尺寸（设计稿像素）

# 图标文件（由 export_icon.py 生成）
APP_DIR = paths.app_dir()
ICON_PATH = paths.resource("app.ico")


# ---------------------------------------------------------------------------
# 主题切换
# ---------------------------------------------------------------------------
def build_color_map(src: dict, dst: dict) -> dict[str, str]:
    """构造 src 配色到 dst 配色的旧色->新色映射（含静态字面量别名）。"""
    m = {}
    for key in src:
        if key in dst:
            m[src[key].lower()] = dst[key]
    # 代码里直接写死的字面量，按语义补进映射
    m.update({
        "#2a3550": dst["NAV_BG"],       # 导航/按钮选中底
        "#1e3a5f": dst["SEL_BG"],       # 选中按钮底
        "#93c5fd": dst["SEL_FG"],       # 选中文字
        "#2e2a3f": dst["PURPLE_BG"],    # 网页驱动按钮底
        "#a78bfa": dst["PURPLE_FG"],
        "#1e3a2a": dst["GREEN_BG"],     # 自开启用底
        "#86efac": dst["GREEN_FG"],
    })
    return m


def apply_theme(theme: str) -> None:
    """切换全局主题：更新配色变量，并遍历所有现存控件重设颜色。

    Tk 控件创建时颜色已固定，切换后需把已创建的控件按旧->新映射重配一遍。
    """
    global CARD_BG, PAGE_BG, TEXT_MAIN, TEXT_SUB, ACCENT, SOFT_BG, SOFT_BG2
    global BORDER, GREEN, ORANGE, RED, SEL_BG, SEL_FG, _COLOR_MAP, _CURRENT_THEME

    pal = _PALETTE_LIGHT if theme == config.THEME_LIGHT else _PALETTE_DARK
    if theme == _CURRENT_THEME:
        return  # 没变化，跳过（避免重复遍历）
    # 当前配色 -> 目标配色的映射
    old_full = _PALETTE_LIGHT if _CURRENT_THEME == config.THEME_LIGHT else _PALETTE_DARK
    _COLOR_MAP = build_color_map(old_full, pal)
    _CURRENT_THEME = theme

    CARD_BG = pal["CARD_BG"]; PAGE_BG = pal["PAGE_BG"]
    TEXT_MAIN = pal["TEXT_MAIN"]; TEXT_SUB = pal["TEXT_SUB"]
    ACCENT = pal["ACCENT"]; SOFT_BG = pal["SOFT_BG"]; SOFT_BG2 = pal["SOFT_BG2"]
    BORDER = pal["BORDER"]; GREEN = pal["GREEN"]; ORANGE = pal["ORANGE"]
    RED = pal["RED"]; SEL_BG = pal["SEL_BG"]; SEL_FG = pal["SEL_FG"]

    roots = []
    try:
        roots.append(_APP_ROOT)
    except Exception:
        pass
    remap_widgets(roots, _COLOR_MAP)


def remap_widgets(roots, cmap: dict[str, str]) -> None:
    """递归遍历控件树，把颜色选项里的旧色替换成新色。"""
    color_opts = ("bg", "foreground", "activebackground", "activeforeground",
                  "highlightbackground", "troughcolor", "selectbackground",
                  "disabledforeground", "readonlybackground")
    seen = set()
    def walk(w):
        if not w or id(w) in seen:
            return
        seen.add(id(w))
        try:
            opts = w.configure()
        except Exception:
            opts = {}
        changes = {}
        for opt in color_opts:
            if opt not in opts:
                continue
            try:
                val = w.cget(opt)          # cget 直接返回当前值，最可靠
            except Exception:
                continue
            if isinstance(val, str) and val.lower() in cmap:
                changes[opt] = cmap[val.lower()]
        if changes:
            try:
                w.configure(**changes)
            except Exception:
                pass
        # Canvas 上画的圆角背景等 item 也要改色
        if isinstance(w, tk.Canvas):
            try:
                for item in w.find_all():
                    for opt in ("fill", "outline", "activefill"):
                        try:
                            val = w.itemcget(item, opt)
                        except Exception:
                            continue
                        if isinstance(val, str) and val.lower() in cmap:
                            w.itemconfigure(item, **{opt: cmap[val.lower()]})
            except Exception:
                pass
        try:
            for child in w.winfo_children():
                walk(child)
        except Exception:
            pass
    for r in roots:
        walk(r)


# 记录"当前处于哪套 palette"，用于构造反向映射
_COLOR_MAP: dict[str, str] = {}
_CURRENT_THEME = config.THEME_DARK
_APP_ROOT = None   # App 启动后指向 tk.Tk 根，供主题遍历用


# ---------------------------------------------------------------------------
# 高 DPI
# 不声明 DPI 感知时，Windows 会把整个窗口当位图拉伸（125% 缩放下放大 1.25 倍），
# 界面就会发虚。声明后按真实像素渲染，需要自己把尺寸和字号放大同样的倍数。
# ---------------------------------------------------------------------------
DPI_SCALE = 1.0


def setup_dpi() -> float:
    """启用 DPI 感知，返回缩放系数（96 DPI → 1.0，120 DPI → 1.25）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi and dpi > 0:
            return max(1.0, dpi / 96.0)
    except Exception:
        pass
    return 1.0


def S(value: float) -> int:
    """把设计稿像素换算成当前 DPI 下的实际像素。"""
    return int(round(value * DPI_SCALE))


def apply_window_icon(win) -> bool:
    """给 Tk 窗口设置应用图标（标题栏 / 任务栏 / Alt-Tab）。

    用 Tk 原生的 wm iconbitmap：Tk 自己管理窗口图标，比直接发 WM_SETICON
    可靠（后者会被 Tk 覆盖掉，读回 WM_GETICON 始终是 0）。
    """
    if not os.path.exists(ICON_PATH):
        return False
    try:
        win.iconbitmap(ICON_PATH)
        return True
    except Exception:
        return False


def hide_from_taskbar(win) -> bool:
    """把窗口从任务栏 / Alt-Tab 列表里摘掉（纯托盘程序用）。

    原理：给窗口加 WS_EX_TOOLWINDOW、去掉 WS_EX_APPWINDOW，
    再发一次 SWP_FRAMECHANGED 让任务栏重新读取样式。
    """
    try:
        win.update_idletasks()
        child = int(win.winfo_id())
        hwnd = int(ctypes.windll.user32.GetParent(child)) or child
        if not hwnd:
            return False
        u = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            get_long = u.GetWindowLongPtrW
            set_long = u.SetWindowLongPtrW
            get_long.restype = ctypes.c_longlong
            set_long.restype = ctypes.c_longlong
            set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]
        else:
            get_long = u.GetWindowLongW
            set_long = u.SetWindowLongW
            get_long.restype = ctypes.c_long
            set_long.restype = ctypes.c_long
            set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        ex = get_long(hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        set_long(hwnd, GWL_EXSTYLE, ex)
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
        SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0010, 0x0020
        u.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                       SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                       | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except Exception:
        return False


def _set_timer_resolution(active: bool) -> None:
    """播放动图时把系统计时器精度调到 1ms，停止后恢复。

    Windows 默认计时器精度约 15.6ms，120Hz 的定时回调会抖成 30~60Hz；
    timeBeginPeriod(1) 能让 120fps 尽量跑满。属于系统级设置，用完要还原。
    """
    global _TIMER_PERIOD_ON
    if _WINMM is None:
        return
    try:
        if active and not _TIMER_PERIOD_ON:
            _WINMM.timeBeginPeriod(1)
            _TIMER_PERIOD_ON = True
        elif not active and _TIMER_PERIOD_ON:
            _WINMM.timeEndPeriod(1)
            _TIMER_PERIOD_ON = False
    except Exception:
        pass


def window_hwnd(win) -> int:
    """取 Tk 窗口对应的顶层 HWND。"""
    try:
        child = int(win.winfo_id())
        return int(ctypes.windll.user32.GetParent(child)) or child
    except Exception:
        return 0


def set_noactivate(win) -> bool:
    """给窗口加 WS_EX_NOACTIVATE：点到它不会抢走焦点。

    背景图层窗口必须这样，否则用户点大界面的空白处时，
    焦点会被图层窗口抢走，主窗口标题栏变灰。
    """
    try:
        win.update_idletasks()
        child = int(win.winfo_id())
        hwnd = int(ctypes.windll.user32.GetParent(child)) or child
        u = ctypes.windll.user32
        ex = u.GetWindowLongW(hwnd, -20) | 0x08000000
        u.SetWindowLongW(hwnd, -20, ex)
        return True
    except Exception:
        return False


def short_device_name(name: str) -> str:
    """压缩设备名：去品牌前缀与"接收器"，超长只留前两段。"""
    n = (name or "").replace("接收器", "").strip()
    for prefix in ("前行者", "狼蛛", "AULA", "EWEADN", "罗技"):
        if n.startswith(prefix):
            n = n[len(prefix):].strip()
    parts = n.split()
    if len(parts) > 2:
        n = " ".join(parts[:2])
    if len(n) > 14:
        n = n[:13] + "…"
    return n


def device_tip_text(snapshot: dict) -> str:
    """生成「设备 · 电量 / 设备 · 电量」形式的一行文字。"""
    parts = []
    for kind in ("mouse", "keyboard"):
        dev = snapshot.get(kind)
        if not dev:
            continue
        pct = dev.get("percent", -1)
        name = short_device_name(dev.get("name", ""))
        parts.append(f"{name} · {pct}%" if pct and pct >= 0 else f"{name} · --")
    return "  /  ".join(parts) if parts else "未检测到设备"


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, fill, outline=None, width=1):
    """在 Canvas 上画圆角矩形（用平滑多边形近似）。"""
    points = []
    corners = [
        (x2 - r, y1 + r, -90),
        (x2 - r, y2 - r, 0),
        (x1 + r, y2 - r, 90),
        (x1 + r, y1 + r, 180),
    ]
    for cx, cy, start in corners:
        for angle in range(start, start + 91, 10):
            rad = math.radians(angle)
            points.append((cx + r * math.cos(rad), cy + r * math.sin(rad)))
    return canvas.create_polygon(
        points, smooth=True, fill=fill, outline=outline or fill, width=width
    )


# ---------------------------------------------------------------------------
# 电量条
# ---------------------------------------------------------------------------
class BatteryBar(tk.Canvas):
    def __init__(self, master, width=140, height=10, bg=CARD_BG):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0)
        self.bar_width = width
        self.bar_height = height
        self.percent = -1
        self._draw()

    def set(self, percent: int) -> None:
        self.percent = percent
        self.delete("all")
        self._draw()

    def _draw(self) -> None:
        w, h = self.bar_width, self.bar_height
        round_rect(self, 0, 0, w, h, 5, fill=SOFT_BG)
        if self.percent and self.percent > 0:
            fw = int(w * min(100, self.percent) / 100)
            if fw > 4:
                color = GREEN if self.percent > 45 else (
                    ORANGE if self.percent > 15 else RED)
                round_rect(self, 0, 0, fw, h, 5, fill=color)


# ---------------------------------------------------------------------------
# 设备卡片
# ---------------------------------------------------------------------------
class DeviceCard(tk.Frame):
    """名称 + 数据来源 + 电量条 + 百分比与状态。"""

    PROTOCOL_TEXT = {
        "x23_passive": "2.4G 被动监听",
        "web_cache": "网页驱动缓存",
        "sonix": "SONiX 2.4G",
        "logitech": "Logitech HID++",
        "g_hub": "G HUB 缓存",
        "mchose_4d": "迈从 0x4D 直连",
        "mchose_4d+cache": "迈从 0x4D 直连 + 客户端缓存",
    }

    def __init__(self, master, kind: str, title_font, label_font, small_font,
                 bar_width=200):
        super().__init__(master, bg=CARD_BG, highlightbackground=BORDER,
                         highlightthickness=1, bd=0)
        self.kind = kind
        prefix = {"mouse": "鼠标", "keyboard": "键盘"}.get(kind, "设备")
        self.name_lbl = tk.Label(self, text=f"{prefix}：扫描中…", font=title_font,
                                 bg=CARD_BG, fg=TEXT_MAIN, anchor="w")
        self.name_lbl.pack(fill="x", padx=14, pady=(12, 2))
        self.protocol_lbl = tk.Label(self, text="", font=small_font, bg=CARD_BG,
                                     fg=ACCENT, anchor="w")
        self.protocol_lbl.pack(fill="x", padx=14)
        row = tk.Frame(self, bg=CARD_BG)
        row.pack(fill="x", padx=14, pady=(8, 12))
        self.bar = BatteryBar(row, width=bar_width, height=12)
        self.bar.pack(side="left")
        self.text = tk.Label(row, text="--%", font=label_font, bg=CARD_BG,
                             fg=TEXT_SUB, width=20, anchor="w")
        self.text.pack(side="left", padx=(12, 0))

    def apply(self, item) -> None:
        prefix = {"mouse": "鼠标", "keyboard": "键盘"}.get(self.kind, "设备")
        if not item:
            self.name_lbl.configure(text=f"{prefix}：未检测到")
            self.protocol_lbl.configure(text="")
            self.bar.set(-1)
            self.text.configure(text="--%")
            return
        self.name_lbl.configure(text=f"{prefix}：{item['name']}")
        proto = self.PROTOCOL_TEXT.get(item.get("protocol", ""), "")
        age = item.get("age")
        if age is not None and proto:
            proto = f"{proto} · {self._age_text(age)}"
        self.protocol_lbl.configure(text=proto)
        pct = item.get("percent", -1)
        self.bar.set(pct)
        if pct < 0:
            self.text.configure(text=item.get("charge") or "无电量")
        else:
            self.text.configure(text=f"{pct}%  {item.get('charge', '')}".strip())

    @staticmethod
    def _age_text(age: float) -> str:
        sec = int(age)
        if sec < 90:
            return "刚刚"
        if sec < 5400:
            return f"{sec // 60} 分钟前"
        if sec < 172800:
            return f"{sec // 3600} 小时前"
        return f"{sec // 86400} 天前"


# ---------------------------------------------------------------------------
# 背景图层窗口
# ---------------------------------------------------------------------------
class PictureWindow:
    """贴在目标窗口正下方、专门画背景图的图层窗口。

    Tk 不允许控件"透出"同一窗口里画布上的图（控件只能填颜色），所以整窗
    背景只能这样做：目标窗口把面板刷成透明色（-transparentcolor），再用
    一个无边框窗口在它正下方画背景图，被抠掉的区域就自然露出这张图。
    """

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.configure(bg=TRANSPARENT)
        self.win.attributes("-transparentcolor", TRANSPARENT)
        self.canvas = tk.Canvas(self.win, bg=TRANSPARENT,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._item = self.canvas.create_image(0, 0, anchor="nw")
        self.photo = None
        self.visible = False
        self._attach_args = (0, 0, 0, 0)
        self._target_widget = None
        self._prev_wndp: list = []
        self._wndproc_refs: list = []
        hide_from_taskbar(self.win)
        set_noactivate(self.win)
        # 图层窗口会挡住下面的界面，鼠标事件要转发回去。
        # 先摘掉 Canvas 类标签，否则它的类绑定会吃掉按下事件（return break）。
        try:
            tags = [t for t in self.canvas.bindtags() if t != "Canvas"]
            self.canvas.bindtags(tags)
            self.canvas.bind("<Button-1>", self._on_pic_press)
            self.canvas.bind("<B1-Motion>", self._on_pic_motion)
            self.canvas.bind("<ButtonRelease-1>", self._on_pic_release)
            self.canvas.bind("<Motion>", self._on_pic_move)
        except Exception:
            pass
        self._subclass_wndproc()

    def hwnd(self) -> int:
        try:
            child = int(self.win.winfo_id())
            return int(ctypes.windll.user32.GetParent(child)) or child
        except Exception:
            return 0

    def _subclass_wndproc(self) -> None:
        """（已废弃）曾用 ctypes 子类化窗口过程转发鼠标消息。

        ★ 在 Python 3.13 + ctypes 下，窗口过程回调会与设备读取线程的
          ctypes 调用冲突，导致 "PyEval_RestoreThread ... thread state is NULL"
          致命错误、整个程序崩掉。现在改用纯 Tk 的事件转发，不用任何回调。
        """
        return

    def set_target(self, widget) -> None:
        """设置转发目标控件（悬浮窗的画布 / 大界面的根窗口）。"""
        self._target_widget = widget

    def _forward(self, pattern: str, event) -> None:
        """把鼠标事件转发给目标控件（纯 Tk 实现，不用 Win32 回调）。

        背景图层垫在下面，界面被抠透明的地方点下去会落到图层窗口上，
        转发回目标控件，悬浮窗才能正常拖动、按钮才能点。
        """
        target = self._target_widget
        if target is None:
            return
        try:
            target.event_generate(pattern, x=int(event.x), y=int(event.y))
        except Exception:
            pass

    def _on_pic_press(self, event):
        self._forward("<Button-1>", event)
        return "break"

    def _on_pic_motion(self, event):
        self._forward("<B1-Motion>", event)
        return "break"

    def _on_pic_release(self, event):
        self._forward("<ButtonRelease-1>", event)
        return "break"

    def _on_pic_move(self, event):
        # 转发移动事件，让目标窗口的缩放光标跟随
        self._forward("<Motion>", event)
        return "break"

    def reattach(self) -> None:
        """把图层窗口按回目标窗口下方（防止被点击后浮到上面）。"""
        try:
            target, x, y, w, h = self._attach_args
            if not target or w <= 0 or h <= 0:
                return
            u = ctypes.windll.user32
            u.SetWindowPos(self.hwnd(), target, x, y, w, h,
                           0x0010 | 0x0040)   # NOACTIVATE | SHOWWINDOW
        except Exception:
            pass

    def move_to(self, x: int, y: int) -> None:
        """把图层窗口挪到指定屏幕位置（拖动时用，比整块重贴轻得多）。"""
        if not self.visible:
            return
        try:
            u = ctypes.windll.user32
            SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
            xi, yi = int(x), int(y)
            u.SetWindowPos(self.hwnd(), None, xi, yi, 0, 0,
                           SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            # 同步缓存的位置：否则 attach 会以为位置变了，多跑一次 geometry
            t, _, _, w, h = self._attach_args
            self._attach_args = (t, xi, yi, w, h)
        except Exception:
            pass

    def paint(self, frame) -> None:
        """更新图层窗口里的画面（复用 PhotoImage，省掉每帧重建开销）。

        传 numpy BGR 帧：尺寸不变时用 photo.put() 原地刷新（4K 大界面能省
        约 2~4ms/帧）；尺寸变化才新建图片对象。
        """
        try:
            h, w = frame.shape[:2]
        except Exception:
            self.photo = None
            try:
                self.canvas.itemconfigure(self._item, image="")
            except Exception:
                pass
            return
        try:
            if (self.photo is None or self.photo.width() != w
                    or self.photo.height() != h):
                self.photo = tk.PhotoImage(width=w, height=h)
        except Exception:
            self.photo = None
        if self.photo is None:
            return
        photo = backdrop.put_photo(self.photo, frame)
        if photo is None:
            return
        try:
            self.canvas.itemconfigure(self._item, image=photo)
        except Exception:
            pass

    def attach(self, target_hwnd: int, x: int, y: int, w: int, h: int) -> None:
        """贴到目标窗口正下方并显示（幂等：没变化就不碰窗口）。

        ★ 这是消除闪烁的关键：动画每帧都会走到这里，如果每次都执行
          geometry + deiconify + SetWindowPos（还带 Z 序重排），120fps 下
          窗口被反复重定位/压层级，肉眼就是持续闪烁。位置尺寸层级都没变
          时直接返回，只有真正的拖动/缩放/切窗口才动 Win32。
        """
        if not target_hwnd or w <= 0 or h <= 0:
            return
        args = (target_hwnd, int(x), int(y), int(w), int(h))
        if self.visible and args == self._attach_args:
            return
        try:
            was_mapped = bool(self.win.winfo_ismapped())
            need_geom = (not was_mapped or args[2:] != self._attach_args[2:])
            if need_geom:
                self.win.geometry(f"{w}x{h}+{x}+{y}")
            if not was_mapped:
                self.win.deiconify()
                self.win.update_idletasks()
            u = ctypes.windll.user32
            SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
            # hWndInsertAfter=目标窗口 => 排到它后面（即下方），不要加 NOZORDER
            u.SetWindowPos(self.hwnd(), target_hwnd, x, y, w, h,
                           SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self._attach_args = args
            self.visible = True
        except Exception:
            pass

    def detach(self) -> None:
        try:
            self.win.withdraw()
        except Exception:
            pass
        self.visible = False


# ---------------------------------------------------------------------------
# 大界面
# ---------------------------------------------------------------------------
class MainWindow:
    def __init__(self, root: tk.Tk, app):
        self.root = root
        self.app = app
        self.root.title("外设控制中心 " + _build_stamp())
        apply_window_icon(self.root)
        self.root.configure(bg=PAGE_BG)
        self.root.geometry(f"{S(BASE_MAIN[0])}x{S(BASE_MAIN[1])}+{S(100)}+{S(80)}")
        self.root.minsize(S(960), S(600))

        self.title_font = tkfont.Font(family="Microsoft YaHei UI", size=15, weight="bold")
        self.section_font = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        self.label_font = tkfont.Font(family="Microsoft YaHei UI", size=10)
        self.small_font = tkfont.Font(family="Microsoft YaHei UI", size=9)

        self._glass = False
        self._glass_widgets: list[tuple] = []

        self._build()

    def _build(self) -> None:
        # ---------- 顶部栏 ----------
        top = tk.Frame(self.root, bg=CARD_BG, height=S(58))
        top.pack(fill="x", side="top")
        top.pack_propagate(False)
        tk.Label(top, text="外设控制中心", font=self.title_font, bg=CARD_BG,
                 fg=TEXT_MAIN).pack(side="left", padx=S(22))
        tk.Button(top, text="×", font=tkfont.Font(size=16), relief="flat",
                  bg=CARD_BG, fg=TEXT_SUB, cursor="hand2",
                  command=self.app.on_close).pack(side="right", padx=(0, S(14)))
        tk.Button(top, text="悬浮窗模式", font=self.small_font, relief="flat",
                  bg="#2a3550", fg=ACCENT, cursor="hand2",
                  command=self.app.show_float).pack(side="right", padx=(0, S(10)))
        self.status_lbl = tk.Label(top, text="正在初始化…", font=self.small_font,
                                   bg=CARD_BG, fg=TEXT_SUB, anchor="e", width=30)
        self.status_lbl.pack(side="right", padx=(0, S(14)), fill="y")

        body = tk.Frame(self.root, bg=PAGE_BG)
        body.pack(fill="both", expand=True, padx=S(16), pady=S(14))
        self.body = body

        # ---------- 左侧导航 ----------
        left = tk.Frame(body, bg=CARD_BG, width=S(176),
                        highlightbackground=BORDER, highlightthickness=1)
        left.pack(side="left", fill="y", padx=(0, S(14)))
        left.pack_propagate(False)

        tk.Label(left, text="导航", font=self.small_font, bg=CARD_BG,
                 fg=TEXT_SUB, anchor="w").pack(anchor="w", padx=S(18),
                                               pady=(S(18), S(6)))

        self.nav_btns: dict[str, tk.Button] = {}
        for key, text in (("devices", "设备"), ("settings", "设置"),
                          ("background", "背景"), ("support", "设备不适配")):
            b = tk.Button(left, text=text, font=self.label_font, relief="flat",
                          bg=CARD_BG, fg=TEXT_MAIN, anchor="w", cursor="hand2",
                          bd=0, highlightthickness=0, padx=S(14),
                          activebackground="#2a3550", activeforeground=ACCENT,
                          command=lambda k=key: self.show_page(k))
            b.pack(fill="x", padx=S(12), pady=S(2), ipady=S(9))
            self.nav_btns[key] = b

        tk.Label(left, text="", bg=CARD_BG).pack(fill="y", expand=True)
        tk.Label(left, text="电量数据全部只读\n不会向设备写入任何内容",
                 font=self.small_font, bg=CARD_BG, fg=TEXT_SUB, anchor="w",
                 justify="left", wraplength=S(140)).pack(anchor="w", padx=S(18),
                                                         pady=(0, S(16)))

        # ---------- 右侧页面容器 ----------
        right = tk.Frame(body, bg=PAGE_BG)
        right.pack(side="left", fill="both", expand=True)

        self.pages: dict[str, tk.Frame] = {}
        dev_page = tk.Frame(right, bg=PAGE_BG)
        set_page = tk.Frame(right, bg=PAGE_BG)
        bg_page = tk.Frame(right, bg=PAGE_BG)
        sup_page = tk.Frame(right, bg=PAGE_BG)
        self.pages["devices"] = dev_page
        self.pages["settings"] = set_page
        self.pages["background"] = bg_page
        self.pages["support"] = sup_page
        # 页面一次性 place 堆叠（零次重排）；切换时只 lift 提升层级。
        # pack_forget/pack 每次都要让 Tk 重算整块区域布局，实测 16ms/次（吃满
        # 一帧 → 导航切换肉眼卡顿）；place+lift 同规模控件仅 0.35ms/次。
        for _page in (dev_page, set_page, bg_page, sup_page):
            _page.place(relx=0, rely=0, relwidth=1, relheight=1)

        # ----- 设备页（可滚动） -----
        dev_canvas = tk.Canvas(dev_page, bg=PAGE_BG, highlightthickness=0, bd=0)
        dev_scrollbar = tk.Scrollbar(
            dev_page, orient="vertical", command=dev_canvas.yview,
            bg=SOFT_BG2, troughcolor=PAGE_BG, activebackground=ACCENT,
            relief="flat", width=S(10), takefocus=0)
        dev_canvas.configure(yscrollcommand=dev_scrollbar.set)
        dev_scrollbar.pack(side="right", fill="y")
        dev_canvas.pack(side="left", fill="both", expand=True)
        dev_content = tk.Frame(dev_canvas, bg=PAGE_BG)
        dev_window = dev_canvas.create_window((0, 0), window=dev_content,
                                              anchor="nw")
        self.dev_canvas = dev_canvas
        self.dev_scrollbar = dev_scrollbar
        self.dev_content = dev_content
        self._dev_window = dev_window

        def _update_scrollregion(_event=None):
            width = dev_canvas.winfo_width()
            if width <= 1:
                return
            dev_canvas.itemconfigure(dev_window, width=width)
            height = max(dev_content.winfo_height(), dev_canvas.winfo_height())
            dev_canvas.configure(scrollregion=(0, 0, width, height))

        dev_content.bind("<Configure>", _update_scrollregion)
        dev_canvas.bind("<Configure>", _update_scrollregion)

        tk.Label(dev_content, text="已连接设备", font=self.section_font,
                 bg=PAGE_BG, fg=TEXT_MAIN, anchor="w").pack(
                     fill="x", pady=(0, S(10)))

        self.mouse_card = DeviceCard(dev_content, "mouse", self.label_font,
                                     self.label_font, self.small_font,
                                     bar_width=S(240))
        self.mouse_card.pack(fill="x", pady=(0, S(12)))

        self.kb_card = DeviceCard(dev_content, "keyboard", self.label_font,
                                  self.label_font, self.small_font,
                                  bar_width=S(240))
        self.kb_card.pack(fill="x", pady=(0, S(12)))

        # 耳机占位：两处都先只留标题，不填内容。
        self.headphone_card = self._build_placeholder_card(dev_content, "耳机")

        tk.Label(dev_content, text="无法显示？", font=self.section_font,
                 bg=PAGE_BG, fg=TEXT_MAIN, anchor="w").pack(
                     fill="x", pady=(0, S(10)))
        # 鼠标卡片在上、键盘卡片在下，两者结构完全一致
        self.mouse_src_card = self._build_brand_card(dev_content, "mouse", "鼠标")
        self.kb_src_card = self._build_brand_card(dev_content, "kb", "键盘")
        self.headphone_src_card = self._build_placeholder_card(
            dev_content, "耳机品牌", height=120)

        tk.Frame(dev_content, bg=PAGE_BG, height=S(16)).pack(fill="x")

        def _on_mousewheel(event):
            delta = getattr(event, "delta", 0)
            if delta:
                step = -1 if delta > 0 else 1
            else:
                step = -1 if getattr(event, "num", 4) == 4 else 1
            dev_canvas.yview_scroll(step * 3, "units")
            return "break"

        def _bind_mousewheel(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            widget.bind("<Button-4>", _on_mousewheel)
            widget.bind("<Button-5>", _on_mousewheel)
            for child in widget.winfo_children():
                _bind_mousewheel(child)

        _bind_mousewheel(dev_content)
        _bind_mousewheel(dev_canvas)
        _update_scrollregion()
        dev_canvas.yview_moveto(0)
        dev_canvas.after(150, lambda: dev_canvas.yview_moveto(0))

        # ----- 设置页 -----
        tk.Label(set_page, text="设置", font=self.section_font, bg=PAGE_BG,
                 fg=TEXT_MAIN, anchor="w").pack(fill="x", pady=(0, S(10)))

        set_card = tk.Frame(set_page, bg=CARD_BG, highlightbackground=BORDER,
                            highlightthickness=1)
        set_card.pack(fill="x")

        # 左列：现有全部设置项
        left_col = tk.Frame(set_card, bg=CARD_BG)
        left_col.pack(side="left", fill="both")

        self.autostart_btn = tk.Button(left_col, text="自启: ?", font=self.label_font,
                                       relief="flat", bg=SOFT_BG, fg=TEXT_SUB,
                                       cursor="hand2", anchor="w",
                                       command=self.app.toggle_autostart)
        self.autostart_btn.pack(fill="x", padx=S(16), pady=(S(16), S(12)), ipady=S(6))

        # ---------- 退出方式 ----------
        tk.Label(left_col, text="退出方式", font=self.label_font, bg=CARD_BG,
                 fg=TEXT_MAIN, anchor="w").pack(fill="x", padx=S(16),
                                                 pady=(0, S(4)))
        tk.Label(left_col, text="点击右上角关闭按钮时", font=self.small_font,
                 bg=CARD_BG, fg=TEXT_SUB, anchor="w").pack(fill="x", padx=S(16),
                                                            pady=(0, S(8)))

        exit_row = tk.Frame(left_col, bg=CARD_BG)
        exit_row.pack(fill="x", padx=S(16), pady=(0, S(4)))
        self.exit_btns = {}
        for mode in (config.EXIT_TRAY, config.EXIT_QUIT):
            btn = tk.Button(exit_row, text=config.EXIT_LABELS[mode],
                            font=self.small_font, relief="flat", bg=SOFT_BG,
                            fg=TEXT_SUB, cursor="hand2", anchor="center",
                            width=14, command=lambda m=mode: self.app.set_exit_mode(m))
            btn.pack(side="left", padx=(0, S(10)))
            self.exit_btns[mode] = btn

        # ---------- 显示范围 ----------
        tk.Label(left_col, text="显示范围", font=self.label_font, bg=CARD_BG,
                 fg=TEXT_MAIN, anchor="w").pack(fill="x", padx=S(16),
                                                 pady=(S(8), S(4)))
        tk.Label(left_col, text="桌面：仅在桌面显示，会被其他窗口遮挡\n全屏：置顶显示在任意应用之上",
                 font=self.small_font, bg=CARD_BG, fg=TEXT_SUB, anchor="w",
                 justify="left").pack(fill="x", padx=S(16), pady=(0, S(8)))
        scope_row = tk.Frame(left_col, bg=CARD_BG)
        scope_row.pack(fill="x", padx=S(16), pady=(0, S(4)))
        self.scope_btns = {}
        for scope in (config.SCOPE_DESKTOP, config.SCOPE_FULLSCREEN):
            btn = tk.Button(scope_row, text=config.SCOPE_LABELS[scope],
                            font=self.small_font, relief="flat", bg=SOFT_BG,
                            fg=TEXT_SUB, cursor="hand2", anchor="center",
                            width=14,
                            command=lambda s=scope: self.app.set_display_scope(s))
            btn.pack(side="left", padx=(0, S(10)))
            self.scope_btns[scope] = btn

        # ---------- 悬浮窗（后台运行时是否显示） ----------
        tk.Label(left_col, text="悬浮窗", font=self.label_font, bg=CARD_BG,
                 fg=TEXT_MAIN, anchor="w").pack(fill="x", padx=S(16),
                                                 pady=(S(8), S(4)))
        tk.Label(left_col, text="托管在后台运行时，悬浮窗是否继续显示",
                 font=self.small_font, bg=CARD_BG, fg=TEXT_SUB, anchor="w").pack(
                     fill="x", padx=S(16), pady=(0, S(8)))
        float_row = tk.Frame(left_col, bg=CARD_BG)
        float_row.pack(fill="x", padx=S(16), pady=(0, S(4)))
        self.float_bg_btns = {}
        for val in (config.FLOAT_BG_YES, config.FLOAT_BG_NO):
            btn = tk.Button(float_row, text=config.FLOAT_BG_LABELS[val],
                            font=self.small_font, relief="flat", bg=SOFT_BG,
                            fg=TEXT_SUB, cursor="hand2", anchor="center",
                            width=14,
                            command=lambda v=val: self.app.set_float_on_bg(v))
            btn.pack(side="left", padx=(0, S(10)))
            self.float_bg_btns[val] = btn

        tk.Label(left_col, text="", bg=CARD_BG).pack(fill="x", pady=(0, S(4)))

        # 右列：主题设置
        right_col = tk.Frame(set_card, bg=CARD_BG)
        right_col.pack(side="left", fill="both", expand=True)
        tk.Label(right_col, text="主题", font=self.label_font, bg=CARD_BG,
                 fg=TEXT_MAIN, anchor="w").pack(fill="x", padx=S(16),
                                                 pady=(S(16), S(4)))

        # 主题：深色 / 浅色（全白）
        theme_row = tk.Frame(right_col, bg=CARD_BG)
        theme_row.pack(fill="x", padx=S(16), pady=(0, S(6)))
        self.theme_btns = {}
        for th in (config.THEME_DARK, config.THEME_LIGHT):
            btn = tk.Button(theme_row, text=config.THEME_LABELS[th],
                            font=self.small_font, relief="flat", bg=SOFT_BG,
                            fg=TEXT_SUB, cursor="hand2", anchor="center",
                            width=9,
                            command=lambda t=th: self.app.set_theme(t))
            btn.pack(side="left", padx=(0, S(8)))
            self.theme_btns[th] = btn

        # 选择本地视频 / 图片作为背景
        tk.Label(right_col, text="界面配色，可随时切换", font=self.small_font,
                 bg=CARD_BG, fg=TEXT_SUB, anchor="w").pack(
                     fill="x", padx=S(16), pady=(0, S(10)))

        # ---------- 清理缓存 ----------
        tk.Label(right_col, text="缓存", font=self.label_font, bg=CARD_BG,
                 fg=TEXT_MAIN, anchor="w").pack(fill="x", padx=S(16),
                                                 pady=(S(10), S(4)))
        tk.Label(right_col, text="清理程序运行时缓存和遗留临时文件\n不会删除设置、背景素材或浏览器数据",
                 font=self.small_font, bg=CARD_BG, fg=TEXT_SUB, anchor="w",
                 justify="left").pack(fill="x", padx=S(16), pady=(0, S(8)))
        self.clear_cache_btn = tk.Button(
            right_col, text="清理缓存", font=self.small_font, relief="flat",
            bg=SOFT_BG, fg=TEXT_SUB, cursor="hand2", anchor="center",
            command=self.app.clear_cache)
        self.clear_cache_btn.pack(fill="x", padx=S(16), pady=(0, S(6)), ipady=S(5))
        self.cache_state_lbl = tk.Label(right_col, text="", font=self.small_font,
                                        bg=CARD_BG, fg=GREEN, anchor="w")
        self.cache_state_lbl.pack(fill="x", padx=S(16), pady=(0, S(4)))

        # 版本号：方便确认对方跑的是不是最新版
        tk.Label(set_page, text="版本 " + _build_stamp(),
                 font=self.small_font, bg=PAGE_BG, fg=TEXT_SUB,
                 anchor="w").pack(fill="x", padx=S(4), pady=(S(10), 0))

        tk.Label(set_page, text="", bg=PAGE_BG).pack(fill="y", expand=True)

        # ----- 背景页 -----
        tk.Label(bg_page, text="背景", font=self.section_font, bg=PAGE_BG,
                 fg=TEXT_MAIN, anchor="w").pack(fill="x", pady=(0, S(10)))

        bg_card = tk.Frame(bg_page, bg=CARD_BG, highlightbackground=BORDER,
                            highlightthickness=1)
        bg_card.pack(fill="x")

        # 目标切换：大界面 / 悬浮窗各自独立背景
        bg_head = tk.Frame(bg_card, bg=CARD_BG)
        bg_head.pack(fill="x", padx=S(16), pady=(S(16), S(4)))
        tk.Label(bg_head, text="界面背景", font=self.label_font, bg=CARD_BG,
                 fg=TEXT_MAIN, anchor="w").pack(side="left")
        self.bg_target_btns: dict[str, tk.Button] = {}
        # side="right" 先放的靠右，所以反过来写才能让「大界面」在左
        for key, text in (("float", "悬浮窗"), ("main", "大界面")):
            btn = tk.Button(bg_head, text=text, font=self.small_font,
                            relief="flat", bg=SOFT_BG, fg=TEXT_SUB,
                            cursor="hand2", anchor="center",
                            command=lambda k=key: self.app.set_bg_target(k))
            btn.pack(side="right", padx=(S(6), 0), ipady=S(3))
            self.bg_target_btns[key] = btn

        self.bg_desc_lbl = tk.Label(
            bg_card, text="", font=self.small_font, bg=CARD_BG, fg=TEXT_SUB,
            anchor="w", justify="left")
        self.bg_desc_lbl.pack(fill="x", padx=S(16), pady=(0, S(12)))

        tk.Button(bg_card, text="选择本地视频或图片…", font=self.small_font,
                  relief="flat", bg=SOFT_BG, fg=TEXT_SUB, cursor="hand2",
                  anchor="w", command=self.app.choose_bg_media).pack(
                      fill="x", padx=S(16), pady=(0, S(6)), ipady=S(6))

        self.bg_path_lbl = tk.Label(bg_card, text="未设置背景", font=self.small_font,
                                    bg=CARD_BG, fg=TEXT_SUB, anchor="w",
                                    justify="left", wraplength=S(560))
        self.bg_path_lbl.pack(fill="x", padx=S(16), pady=(0, S(10)))

        # 模糊度：0 = 清晰原图，越大越磨砂
        tk.Label(bg_card, text="背景模糊", font=self.label_font, bg=CARD_BG,
                 fg=TEXT_MAIN, anchor="w").pack(fill="x", padx=S(16),
                                                pady=(0, S(4)))
        tk.Label(bg_card, text="0 为清晰原图，数值越大背景越柔和",
                 font=self.small_font, bg=CARD_BG, fg=TEXT_SUB, anchor="w").pack(
                     fill="x", padx=S(16), pady=(0, S(6)))
        blur_row = tk.Frame(bg_card, bg=CARD_BG)
        blur_row.pack(fill="x", padx=S(16), pady=(0, S(6)))
        self.bg_blur_scale = tk.Scale(
            blur_row, from_=0, to=8, orient="horizontal", resolution=1,
            showvalue=False, length=S(220), bg=CARD_BG, fg=ACCENT,
            troughcolor=SOFT_BG, activebackground=ACCENT,
            highlightthickness=0, relief="flat", sliderrelief="raised",
            command=self.app.set_bg_blur)
        self.bg_blur_scale.set(int(round(config.get_bg_blur(self.app.bg_target))))
        self.bg_blur_scale.pack(side="left")
        self.bg_blur_lbl = tk.Label(blur_row, text="", font=self.small_font,
                                    bg=CARD_BG, fg=ACCENT, width=6, anchor="w")
        self.bg_blur_lbl.pack(side="left", padx=(S(8), 0))

        # 锐化：图片缩到小窗口后发糊，用锐化把细节找回来
        tk.Label(bg_card, text="背景锐化", font=self.label_font, bg=CARD_BG,
                 fg=TEXT_MAIN, anchor="w").pack(fill="x", padx=S(16),
                                                pady=(0, S(4)))
        tk.Label(bg_card, text="图片缩小后通常会发糊，数值越大越锐利",
                 font=self.small_font, bg=CARD_BG, fg=TEXT_SUB, anchor="w").pack(
                     fill="x", padx=S(16), pady=(0, S(6)))
        sharp_row = tk.Frame(bg_card, bg=CARD_BG)
        sharp_row.pack(fill="x", padx=S(16), pady=(0, S(6)))
        self.bg_sharp_scale = tk.Scale(
            sharp_row, from_=0, to=15, orient="horizontal", resolution=1,
            showvalue=False, length=S(220), bg=CARD_BG, fg=ACCENT,
            troughcolor=SOFT_BG, activebackground=ACCENT,
            highlightthickness=0, relief="flat", sliderrelief="raised",
            command=self.app.set_bg_sharpen)
        self.bg_sharp_scale.set(int(round(config.get_bg_sharpen(self.app.bg_target) * 10)))
        self.bg_sharp_scale.pack(side="left")
        self.bg_sharp_lbl = tk.Label(sharp_row, text="", font=self.small_font,
                                     bg=CARD_BG, fg=ACCENT, width=6, anchor="w")
        self.bg_sharp_lbl.pack(side="left", padx=(S(8), 0))

        tk.Button(bg_card, text="清除背景", font=self.small_font, relief="flat",
                  bg=SOFT_BG, fg=TEXT_SUB, cursor="hand2", anchor="w",
                  command=self.app.clear_bg_media).pack(fill="x", padx=S(16),
                                                        pady=(0, S(16)), ipady=S(6))

        tk.Label(bg_page, text="", bg=PAGE_BG).pack(fill="y", expand=True)

        # ----- 设备不适配页 -----
        tk.Label(sup_page, text="设备不适配？", font=self.section_font,
                 bg=PAGE_BG, fg=TEXT_MAIN, anchor="w").pack(
                     fill="x", pady=(0, S(10)))

        sup_card = tk.Frame(sup_page, bg=CARD_BG, highlightbackground=BORDER,
                            highlightthickness=1)
        sup_card.pack(fill="x")

        tk.Label(sup_card,
                 text="电量显示不出来时，可以导出这台电脑的设备诊断信息，"
                      "发给开发者补充适配规则。",
                 font=self.label_font, bg=CARD_BG, fg=TEXT_MAIN, anchor="w",
                 justify="left", wraplength=S(520)).pack(
                     fill="x", padx=S(16), pady=(S(16), S(12)))

        for step in (
            "1. 先打开对应品牌的网页驱动，连接设备并让页面显示电量",
            "2. 回到这里点下面的按钮，保存诊断文件",
            "3. 把生成的 txt 发给开发者",
            "4. 也可以直接发到开发者邮箱（点击邮箱可复制）：",
        ):
            tk.Label(sup_card, text=step, font=self.small_font, bg=CARD_BG,
                     fg=TEXT_SUB, anchor="w", justify="left",
                     wraplength=S(520)).pack(fill="x", padx=S(16),
                                             pady=(0, S(5)))

        self.dev_mail_lbl = tk.Label(
            sup_card, text="3464162519@qq.com", font=self.label_font,
            bg=CARD_BG, fg="#a78bfa", anchor="w", cursor="hand2")
        self.dev_mail_lbl.pack(fill="x", padx=S(24), pady=(0, S(6)))
        self.dev_mail_lbl.bind("<Button-1>", lambda _e: self.app.copy_dev_mail())

        self.export_diag_btn = tk.Button(
            sup_card, text="导出诊断信息（发给开发者）",
            font=self.small_font, relief="flat", bg="#2e2a3f", fg="#a78bfa",
            cursor="hand2", command=self.app.export_diagnostics)
        self.export_diag_btn.pack(fill="x", padx=S(16),
                                  pady=(S(16), S(8)), ipady=S(7))
        tk.Label(sup_card,
                 text="只含设备型号、电量和相关缓存字段，不含账号与 Cookie",
                 font=self.small_font, bg=CARD_BG, fg=TEXT_SUB, anchor="w",
                 justify="left", wraplength=S(520)).pack(
                     fill="x", padx=S(16), pady=(0, S(16)))

        tk.Label(sup_page, text="", bg=PAGE_BG).pack(fill="y", expand=True)

        # 启动时指定页面：--page=settings / --page=background …
        start_page = "devices"
        for arg in sys.argv:
            if arg.startswith("--page="):
                start_page = arg.split("=", 1)[1].strip()
        if start_page not in self.pages:
            start_page = "devices"
        self.show_page(start_page)

    # ------------------------- 页面切换 -------------------------
    def show_page(self, key: str) -> None:
        """切换右侧内容区（设备 / 设置 / 背景 / 设备不适配）。

        页面用 place 一次性堆叠好，切换只需 lift() 改层级——不触发 Tk
        重新布局（实测比 pack_forget/pack 快约 45 倍，导航不再卡顿）。
        """
        if key not in self.pages:
            return
        self.page = key
        try:
            self.pages[key].lift()
        except Exception:
            pass
        if key == "devices":
            try:
                self.dev_canvas.yview_moveto(0)
            except Exception:
                pass
        for name, btn in self.nav_btns.items():
            selected = (name == key)
            # 导航按钮保持实色：抠透明后鼠标点不到（会穿透到背景图层）
            btn.configure(bg="#2a3550" if selected else CARD_BG,
                          fg=ACCENT if selected else TEXT_MAIN)

    # ------------------------- 玻璃模式（背景） -------------------------
    def collect_glass_widgets(self) -> None:
        """记录可抠成透明色的面板控件及其配色角色。

        角色（而不是具体色值）用于还原：切换主题后颜色会跟着变。
        """
        self._glass_widgets = []
        seen = set()

        def walk(widget):
            try:
                children = widget.winfo_children()
            except Exception:
                return
            for child in children:
                # 悬浮窗 / 背景图层等 Toplevel 虽然挂在 root 下，但属于独立
                # 窗口，有自己的玻璃管理。收进来会让主窗口关玻璃时把悬浮窗
                # 的标题、透明度滑块恢复成实色，抠图失效（标题后冒出色块）。
                if isinstance(child, tk.Toplevel):
                    continue
                if id(child) in seen:
                    continue
                seen.add(id(child))
                if isinstance(child, (tk.Frame, tk.Label, tk.Canvas, tk.Scale)):
                    try:
                        color = child.cget("bg")
                    except Exception:
                        color = ""
                    self._glass_widgets.append(
                        (child, role_of_color(color), color or ""))
                walk(child)

        walk(self.root)

    def set_glass(self, active: bool) -> None:
        """开 / 关玻璃模式：面板刷成透明色，露出背后的背景图。"""
        if active and not self._glass_widgets:
            self.collect_glass_widgets()
        for child, role, literal in self._glass_widgets:
            try:
                child.configure(
                    bg=TRANSPARENT if active else color_of_role(role, literal))
            except Exception:
                pass
        try:
            self.root.configure(bg=TRANSPARENT if active else PAGE_BG)
            self.root.attributes("-transparentcolor",
                                 TRANSPARENT if active else "")
        except Exception:
            pass
        self._glass = active
        # 导航按钮的选中态刷新（按钮保持实色，才能点到）
        try:
            self.show_page(getattr(self, "page", "devices"))
        except Exception:
            pass

    def _build_placeholder_card(self, parent, title: str, height: int = 58):
        """空占位卡片：只显示标题，给后续功能预留同样式位置。"""
        card = tk.Frame(parent, bg=CARD_BG, highlightbackground=BORDER,
                        highlightthickness=1, bd=0, height=S(height))
        card.pack(fill="x", pady=(0, S(12)))
        card.pack_propagate(False)
        tk.Label(card, text=title, font=self.label_font, bg=CARD_BG,
                 fg=TEXT_MAIN, anchor="w").pack(
                     fill="x", padx=S(14), pady=(S(12), 0))
        return card

    def _build_brand_card(self, parent, kind: str, label: str):
        """构建「品牌下拉 + 状态行 + 打开驱动按钮 + 说明」卡片。

        鼠标和键盘结构完全一致，只有 kind / label 与配置读写不同。
        """
        is_mouse = (kind == "mouse")
        getter = config.get_mouse_brand if is_mouse else config.get_kb_brand
        setter = config.set_mouse_brand if is_mouse else config.set_kb_brand
        changed_cb = (self._on_mouse_brand_changed if is_mouse
                      else self._on_kb_brand_changed)
        open_cb = self.app.open_mouse_web if is_mouse else self.app.open_kb_web

        card = tk.Frame(parent, bg=CARD_BG, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", pady=(0, S(12)))

        # 品牌下拉：选项 / 按钮文字 / 打开地址都由 KB_BRAND_URLS 驱动
        brand_row = tk.Frame(card, bg=CARD_BG)
        brand_row.pack(fill="x", padx=S(16), pady=(S(14), S(4)))
        tk.Label(brand_row, text=f"{label}品牌", font=self.small_font, bg=CARD_BG,
                 fg=TEXT_SUB, anchor="w").pack(side="left")
        saved = getter()
        if saved not in KB_BRANDS:
            saved = KB_BRANDS[0]
        brand_var = tk.StringVar(value=saved)
        setattr(self, f"{kind}_brand_var", brand_var)
        menu = tk.OptionMenu(brand_row, brand_var, *KB_BRANDS, command=changed_cb)
        menu.configure(font=self.small_font, bg="#2e2a3f", fg="#a78bfa",
                       activebackground="#3a3452", activeforeground="#c4b5fd",
                       relief="flat", highlightthickness=0, cursor="hand2")
        try:
            menu["menu"].configure(
                bg="#2e2a3f", fg="#a78bfa", activebackground="#3a3452",
                activeforeground="#c4b5fd", font=self.small_font, tearoff=0)
        except Exception:
            pass
        menu.pack(side="right")

        state = tk.Label(card, text="检测中…", font=self.label_font,
                         bg=CARD_BG, fg=ORANGE, anchor="w")
        state.pack(fill="x", padx=S(16), pady=(S(6), S(0)))
        setattr(self, f"{kind}_bridge_state", state)

        btn = tk.Button(card, text=f"打开{saved}网页驱动（刷新电量）",
                        font=self.small_font, relief="flat", bg="#2e2a3f",
                        fg="#a78bfa", cursor="hand2", command=open_cb)
        btn.pack(fill="x", padx=S(16), pady=(S(10), S(4)), ipady=S(5))
        setattr(self, f"{kind}_driver_btn", btn)
        # 保存配置写入函数，供切换回调使用
        setattr(self, f"_{kind}_brand_setter", setter)

        tk.Label(card, text=f"连上{label}后左上角显示电量，程序随即同步",
                 font=self.small_font, bg=CARD_BG, fg=TEXT_SUB, anchor="w").pack(
                     anchor="w", padx=S(16), pady=(0, S(14)))
        return card

    def _on_kb_brand_changed(self, brand: str) -> None:
        """切换键盘品牌：保存选择、按钮文字跟着换成对应品牌。"""
        try:
            config.set_kb_brand(brand)
        except Exception:
            pass
        try:
            self.kb_driver_btn.configure(
                text=f"打开{brand}网页驱动（刷新电量）")
        except Exception:
            pass

    def _on_mouse_brand_changed(self, brand: str) -> None:
        """切换鼠标品牌：保存选择、按钮文字跟着换成对应品牌。"""
        try:
            config.set_mouse_brand(brand)
        except Exception:
            pass
        try:
            self.mouse_driver_btn.configure(
                text=f"打开{brand}网页驱动（刷新电量）")
        except Exception:
            pass

    def refresh_bg_target(self) -> None:
        """刷新「大界面 / 悬浮窗」目标按钮的选中态。"""
        target = self.app.bg_target
        for key, btn in getattr(self, "bg_target_btns", {}).items():
            selected = (key == target)
            btn.configure(bg=SEL_BG if selected else SOFT_BG,
                          fg=SEL_FG if selected else TEXT_SUB,
                          text=("● " if selected else "○ ") + config.BG_TARGET_LABELS[key])

    def refresh_bg_media(self) -> None:
        """刷新当前目标（大界面 / 悬浮窗）的背景路径与滑块数值。"""
        target = self.app.bg_target
        name = config.BG_TARGET_LABELS[target]
        path = config.get_bg_media(target)
        if path:
            self.bg_path_lbl.configure(text=f"当前背景：{os.path.basename(path)}")
        else:
            self.bg_path_lbl.configure(text=f"{name}未设置背景")
        try:
            self.bg_desc_lbl.configure(
                text=f"正在设置：{name} · 两个界面可以分别使用不同的图片、视频或 GIF")
        except Exception:
            pass
        try:
            blur = int(round(config.get_bg_blur(target)))
            self.bg_blur_scale.set(blur)
            self.bg_blur_lbl.configure(text=f"{blur}")
        except Exception:
            pass
        try:
            sharp = int(round(config.get_bg_sharpen(target) * 10))
            self.bg_sharp_scale.set(sharp)
            self.bg_sharp_lbl.configure(text=f"{sharp / 10:.1f}")
        except Exception:
            pass
        self.refresh_bg_target()

    # ------------------------- 刷新 -------------------------
    def refresh(self) -> None:
        s = self.app.core.snapshot()
        status = (s.get("status") or "").split(" · 更新于")[0]
        if len(status) > 18:
            status = status[:18] + "…"
        self.status_lbl.configure(text=status)
        self.mouse_card.apply(s.get("mouse"))
        self.kb_card.apply(s.get("keyboard"))

        kb = s.get("keyboard") or {}
        proto = kb.get("protocol") or ""
        pct = kb.get("percent", -1)
        if proto == "web_cache":
            text, color = "键盘电量：网页驱动缓存 ✓", GREEN
        elif pct is not None and pct >= 0:
            text, color = "键盘电量：直连读取 ✓", GREEN
        else:
            text, color = "键盘电量未获取（点下方打开网页驱动）", ORANGE
        self.kb_bridge_state.configure(text=text, fg=color)

        # 鼠标状态行：与键盘对称，取鼠标快照
        mo = s.get("mouse") or {}
        mproto = mo.get("protocol") or ""
        mpct = mo.get("percent", -1)
        if mproto == "web_cache":
            mtext, mcolor = "鼠标电量：网页驱动缓存 ✓", GREEN
        elif mpct is not None and mpct >= 0:
            mtext, mcolor = "鼠标电量：直连读取 ✓", GREEN
        else:
            mtext, mcolor = "鼠标电量未获取（点下方打开网页驱动）", ORANGE
        self.mouse_bridge_state.configure(text=mtext, fg=mcolor)

        on, _ = autostart.status()
        if on:
            bg, fg = "#1e3a2a", "#86efac"
        else:
            bg, fg = SOFT_BG, TEXT_SUB
        self.autostart_btn.configure(
            text="开机自启：开" if on else "开机自启：关", bg=bg, fg=fg)

        self.refresh_exit_mode()
        self.refresh_display_scope()
        self.refresh_float_on_bg()
        self.refresh_theme()

    def refresh_theme(self) -> None:
        """刷新主题按钮选中态。"""
        current = config.get_theme()
        for th, btn in self.theme_btns.items():
            if th == current:
                btn.configure(bg=SEL_BG, fg=SEL_FG,
                              text="● " + config.THEME_LABELS[th])
            else:
                btn.configure(bg=SOFT_BG, fg=TEXT_SUB,
                              text="○ " + config.THEME_LABELS[th])

    def refresh_float_on_bg(self) -> None:
        """刷新「悬浮窗 / 后台运行时是否显示」两个按钮的选中态。"""
        current = config.FLOAT_BG_YES if config.get_float_on_bg() else config.FLOAT_BG_NO
        for val, btn in self.float_bg_btns.items():
            if val == current:
                btn.configure(bg=SEL_BG, fg=SEL_FG,
                              text="● " + config.FLOAT_BG_LABELS[val])
            else:
                btn.configure(bg=SOFT_BG, fg=TEXT_SUB,
                              text="○ " + config.FLOAT_BG_LABELS[val])

    def refresh_display_scope(self) -> None:
        """刷新「显示范围」两个按钮的选中态。"""
        current = config.get_display_scope()
        for scope, btn in self.scope_btns.items():
            if scope == current:
                btn.configure(bg=SEL_BG, fg=SEL_FG,
                              text="● " + config.SCOPE_LABELS[scope])
            else:
                btn.configure(bg=SOFT_BG, fg=TEXT_SUB,
                              text="○ " + config.SCOPE_LABELS[scope])

    def refresh_exit_mode(self) -> None:
        """刷新「退出方式」两个按钮的选中态。"""
        current = config.get_exit_mode()
        for mode, btn in self.exit_btns.items():
            if mode == current:
                btn.configure(bg=SEL_BG, fg=SEL_FG,
                              text="● " + config.EXIT_LABELS[mode])
            else:
                btn.configure(bg=SOFT_BG, fg=TEXT_SUB,
                              text="○ " + config.EXIT_LABELS[mode])


# ---------------------------------------------------------------------------
# 悬浮窗
# ---------------------------------------------------------------------------
class FloatWindow:
    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("外设控制中心 - 悬浮窗")
        apply_window_icon(self.win)
        self.win.withdraw()
        self.win.overrideredirect(True)
        # 置顶状态由「显示范围」决定：全屏=置顶，桌面=非置顶
        self.topmost = (config.get_display_scope() == config.SCOPE_FULLSCREEN)
        self.win.attributes("-topmost", self.topmost)
        self.win.attributes("-transparentcolor", TRANSPARENT)
        self.win.configure(bg=TRANSPARENT)
        self.alpha = config.get_float_alpha()
        self.win.attributes("-alpha", self.alpha)
        _sw, _sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self._screen_size = (_sw, _sh)
        # 尺寸：上次拖动调整过的优先；否则用默认。钳制在最小尺寸与屏幕之间。
        saved_size = config.get_float_size()
        if saved_size is not None:
            _fw = min(max(saved_size[0], S(MIN_FLOAT[0])), _sw)
            _fh = min(max(saved_size[1], S(MIN_FLOAT[1])), _sh)
        else:
            _fw, _fh = S(BASE_FLOAT[0]), S(BASE_FLOAT[1])
        self._win_size = (_fw, _fh)
        # 上次拖动过的位置优先；否则首次居中。都做屏幕边界钳制，避免跑出屏幕。
        saved = config.get_float_pos()
        if saved is not None:
            _x = min(max(saved[0], 0), max(0, _sw - _fw))
            _y = min(max(saved[1], 0), max(0, _sh - _fh))
        else:
            _x = max(0, (_sw - _fw) // 2)
            _y = max(0, (_sh - _fh) // 2)
        self.win.geometry(f"{_fw}x{_fh}+{_x}+{_y}")
        self._pos_save_job = None
        self._size_save_job = None
        self._glass = False
        self._glass_widgets: list[tuple] = []
        self.drag_origin = None       # 移动窗口用
        self.resize_origin = None     # 缩放窗口用 (方向, 屏幕鼠标, 窗口几何)
        self._build()
        self._apply_lock_state()
        hide_from_taskbar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", app.on_close)

    def _build(self) -> None:
        title_font = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        label_font = tkfont.Font(family="Microsoft YaHei UI", size=9)
        small_font = tkfont.Font(family="Microsoft YaHei UI", size=8)

        W, H = self._win_size
        self.canvas = tk.Canvas(self.win, width=W, height=H, bg=TRANSPARENT,
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        # ★ Canvas 的类绑定会在 <Button-1> 里做"框选"并 return break，
        #   会把顶层窗口的拖动绑定吃掉（表现为悬浮窗拖不动）。
        #   这里把 Canvas 类标签摘掉，保留控件路径、顶层窗口与 All 标签。
        try:
            tags = [t for t in self.canvas.bindtags() if t != "Canvas"]
            self.canvas.bindtags(tags)
        except Exception:
            pass
        self._bg_item = round_rect(self.canvas, S(6), S(6), W - S(6), H - S(6), S(18),
                                   fill=CARD_BG, outline=BORDER, width=1)
        # 鼠标移动时根据位置更新光标（边缘=缩放，中间=移动）
        self.canvas.bind("<Motion>", self._on_hover)
        # 从边框移到内容区时复位光标，避免残留缩放箭头
        self.canvas.bind("<Leave>", self._on_leave)
        self.win.bind("<Leave>", self._on_leave)
        # 拖动绑定挂在顶层窗口：内容 frame 覆盖在 canvas 之上，
        # 若只绑 canvas，按在中间内容区时事件被 frame 拦截，窗口无法移动。
        # Tk 子控件的事件会沿 bindtags 上传递到 toplevel，故绑 win 可覆盖全窗口。
        self.win.bind("<ButtonPress-1>", self._drag_start)
        self.win.bind("<B1-Motion>", self._drag_move)
        self.win.bind("<ButtonRelease-1>", self._drag_end)
        # 悬浮窗被点击/激活后，Windows 会把背景图层顶到上层盖住文字；
        # 这两个事件把图层重新压回悬浮窗下方，保证文字始终可见。
        self.win.bind("<ButtonPress-1>", self._relower_picture, add="+")
        self.win.bind("<Activate>", self._relower_picture, add="+")
        # 窗口尺寸变化时重绘圆角背景 + 让内容区跟着缩放
        self.canvas.bind("<Configure>", self._on_configure)
        # 移动/缩放时同步背景图层窗口（位置和尺寸都要跟住）
        self.win.bind("<Configure>", self._on_win_configure)

        self.frame = tk.Frame(self.win, bg=CARD_BG)
        self.frame.place(x=S(18), y=S(18), relwidth=1.0, relheight=1.0,
                         width=-S(36), height=-S(36))
        self.frame.pack_propagate(False)

        header = tk.Frame(self.frame, bg=CARD_BG)
        header.pack(fill="x", pady=(0, S(8)))
        self.title_lbl = tk.Label(header, text="外设电量", font=title_font, bg=CARD_BG,
                                  fg=TEXT_MAIN)
        self.title_lbl.pack(side="left", padx=(0, S(10)))
        top_text, top_bg, top_fg = self._topmost_style()
        self.top_btn = tk.Button(header, text=top_text, font=small_font, relief="flat",
                                 bg=top_bg, fg=top_fg, cursor="hand2",
                                 command=self._toggle_topmost)
        self.top_btn.pack(side="right", padx=(0, S(4)))
        self.main_btn = tk.Button(header, text="大界面", font=small_font, relief="flat",
                                  bg="#2a3550", fg=ACCENT, cursor="hand2",
                                  command=self.app.show_main)
        self.main_btn.pack(side="right", padx=(0, S(4)))
        # 文字显示开关：背景好看时可隐藏全部文字/控件，只留这一个按钮
        self.text_show = config.get_float_show_text()
        self.text_btn = tk.Button(
            header, text="文字" if self.text_show else "文字×",
            font=small_font, relief="flat",
            bg="#2a3550" if self.text_show else SOFT_BG,
            fg=ACCENT if self.text_show else TEXT_SUB,
            cursor="hand2", command=self._toggle_text)
        self.text_btn.pack(side="right", padx=(0, S(4)))

        # 透明度滑块（放在顶栏，鼠标拖动调节 20%~100%）
        self.alpha_scale = tk.Scale(
            header, from_=20, to=100, orient="horizontal",
            resolution=1, showvalue=False, length=S(70),
            bg=CARD_BG, fg=ACCENT, troughcolor=SOFT_BG,
            activebackground=ACCENT, highlightthickness=0,
            relief="flat", sliderrelief="raised",
            command=self._on_alpha_changed)
        self.alpha_scale.set(int(round(self.alpha * 100)))
        self.alpha_scale.pack(side="right", padx=(0, S(4)))
        self.alpha_value_lbl = tk.Label(
            header, text=f"{int(round(self.alpha * 100))}%",
            font=small_font, bg=CARD_BG, fg=ACCENT, width=4, anchor="e")
        self.alpha_value_lbl.pack(side="right", padx=(0, S(2)))
        self._alpha_save_job = None

        # 顶栏全部控件按原始 pack 顺序记录（含文字按钮）。
        # 隐藏状态只重新 pack 文字按钮；显示状态按此顺序全部还原，避免顺序错乱。
        self._header_widgets = [
            (self.title_lbl, {"side": "left", "padx": (0, S(10))}),
            (self.top_btn, {"side": "right", "padx": (0, S(4))}),
            (self.main_btn, {"side": "right", "padx": (0, S(4))}),
            (self.text_btn, {"side": "right", "padx": (0, S(4))}),
            (self.alpha_scale, {"side": "right", "padx": (0, S(4))}),
            (self.alpha_value_lbl, {"side": "right", "padx": (0, S(2))}),
        ]

        self.mouse_name = tk.Label(self.frame, text="鼠标：扫描中…", font=label_font,
                                   bg=CARD_BG, fg=TEXT_MAIN, anchor="w")
        self.mouse_name.pack(fill="x")
        mrow = tk.Frame(self.frame, bg=CARD_BG)
        mrow.pack(fill="x", pady=(S(2), S(6)))
        self.mouse_bar = BatteryBar(mrow, width=S(132))
        self.mouse_bar.pack(side="left")
        self.mouse_text = tk.Label(mrow, text="--%", font=label_font, bg=CARD_BG,
                                   fg=TEXT_SUB, width=18, anchor="w")
        self.mouse_text.pack(side="left", padx=(S(8), 0))

        self.kb_name = tk.Label(self.frame, text="键盘：扫描中…", font=label_font,
                                bg=CARD_BG, fg=TEXT_MAIN, anchor="w")
        self.kb_name.pack(fill="x")
        krow = tk.Frame(self.frame, bg=CARD_BG)
        krow.pack(fill="x", pady=(S(2), S(8)))
        self.kb_bar = BatteryBar(krow, width=S(132))
        self.kb_bar.pack(side="left")
        self.kb_text = tk.Label(krow, text="--%", font=label_font, bg=CARD_BG,
                                fg=TEXT_SUB, width=12, anchor="w")
        self.kb_text.pack(side="left", padx=(S(8), 0))

        # 内容区文字控件（可被「文字」开关整体隐藏，只留背景图）。
        # 记录原始 pack 参数，显示时用原参数还原，避免布局走样。
        self._text_widgets = [
            (self.mouse_name, {"fill": "x"}),
            (mrow, {"fill": "x", "pady": (S(2), S(6))}),
            (self.kb_name, {"fill": "x"}),
            (krow, {"fill": "x", "pady": (S(2), S(8))}),
        ]
        self._apply_text_visibility()

    # ------------------------- 文字显示开关 -------------------------
    def _toggle_text(self):
        """切换悬浮窗文字显示，并持久化。"""
        self.text_show = not self.text_show
        config.set_float_show_text(
            config.FLOAT_TEXT_YES if self.text_show else config.FLOAT_TEXT_NO)
        self._apply_text_visibility()

    def _apply_text_visibility(self):
        """文字开关：隐藏时连顶栏标题/置顶/大界面/透明度一起收起，只留「文字」按钮；
        显示时把内容区文字和顶栏控件全部原样还原。"""
        if not hasattr(self, "_text_widgets"):
            return
        # 内容区文字（鼠标/键盘电量）
        for w, kw in self._text_widgets:
            try:
                if self.text_show:
                    w.pack(**kw)
                else:
                    w.pack_forget()
            except Exception:
                pass
        # 顶栏控件：先全部收起，再按原始顺序还原（隐藏时只还原文字按钮）
        header_widgets = getattr(self, "_header_widgets", [])
        for w, _ in header_widgets:
            try:
                w.pack_forget()
            except Exception:
                pass
        for w, kw in header_widgets:
            if (not self.text_show) and w is not self.text_btn:
                continue
            try:
                w.pack(**kw)
            except Exception:
                pass
        try:
            self.text_btn.configure(
                text="文字" if self.text_show else "文字×",
                bg="#2a3550" if self.text_show else SOFT_BG,
                fg=ACCENT if self.text_show else TEXT_SUB)
        except Exception:
            pass

    # ------------------------- 玻璃模式（背景） -------------------------
    def set_glass(self, active: bool) -> None:
        """开 / 关玻璃模式：悬浮窗内容区抠透明，露出背景图。"""
        if active and not self._glass_widgets:
            self._glass_widgets = []
            seen = set()

            def walk(widget):
                try:
                    children = widget.winfo_children()
                except Exception:
                    return
                for child in children:
                    if id(child) in seen:
                        continue
                    seen.add(id(child))
                    if isinstance(child, (tk.Frame, tk.Label, tk.Canvas,
                                          tk.Scale)):
                        try:
                            color = child.cget("bg")
                        except Exception:
                            color = ""
                        self._glass_widgets.append(
                            (child, role_of_color(color), color or ""))
                    walk(child)

            walk(self.win)

        for child, role, literal in self._glass_widgets:
            try:
                child.configure(
                    bg=TRANSPARENT if active else color_of_role(role, literal))
            except Exception:
                pass
        # 卡片底色：玻璃模式抠空（露出背景图），否则恢复实色
        try:
            self.canvas.itemconfigure(
                self._bg_item, fill=TRANSPARENT if active else CARD_BG)
        except Exception:
            pass
        self._glass = active

    def flush_alpha(self):
        """退出前立即写盘未保存的透明度 + 位置，避免防抖任务被 os._exit 跳过。"""
        try:
            if self._alpha_save_job is not None:
                self.app.root.after_cancel(self._alpha_save_job)
                self._alpha_save_job = None
            config.set_float_alpha(self.alpha)
        except Exception:
            pass
        self.flush_pos()
        self.flush_size()

    def flush_pos(self):
        """立即写盘悬浮窗当前位置。"""
        try:
            if self._pos_save_job is not None:
                self.app.root.after_cancel(self._pos_save_job)
                self._pos_save_job = None
            config.set_float_pos(self.win.winfo_x(), self.win.winfo_y())
        except Exception:
            pass

    def flush_size(self):
        """立即写盘悬浮窗当前尺寸。"""
        try:
            if self._size_save_job is not None:
                self.app.root.after_cancel(self._size_save_job)
                self._size_save_job = None
            config.set_float_size(self.win.winfo_width(), self.win.winfo_height())
        except Exception:
            pass

    def _on_alpha_changed(self, value):
        """拖动滑块：实时改透明度，防抖保存（0.4s 后才写盘）。"""
        try:
            pct = int(round(float(value)))
        except (TypeError, ValueError):
            return
        pct = min(100, max(20, pct))
        self.alpha = pct / 100.0
        self.win.attributes("-alpha", self.alpha)
        # 背景图层窗口跟着变，滑块对整块卡片才有效
        try:
            if self.app.mode == "float":
                self.app.picture.win.attributes("-alpha", self.alpha)
        except Exception:
            pass
        self.alpha_value_lbl.configure(text=f"{pct}%")
        # 防抖：频繁拖动只在停手 0.4s 后写一次配置
        if self._alpha_save_job is not None:
            try:
                self.app.root.after_cancel(self._alpha_save_job)
            except Exception:
                pass
        self._alpha_save_job = self.app.root.after(
            400, lambda: config.set_float_alpha(self.alpha))

    def refresh(self) -> None:
        s = self.app.core.snapshot()
        self._apply_device("mouse", s.get("mouse"), self.mouse_name, self.mouse_bar,
                           self.mouse_text)
        self._apply_device("keyboard", s.get("keyboard"), self.kb_name, self.kb_bar,
                           self.kb_text)

    @staticmethod
    def _apply_device(kind, item, name_lbl, bar, text_lbl) -> None:
        prefix = {"mouse": "鼠标", "keyboard": "键盘"}.get(kind, "设备")
        if not item:
            name_lbl.configure(text=f"{prefix}：未检测到")
            bar.set(-1)
            text_lbl.configure(text="--%")
            return
        name_lbl.configure(text=f"{prefix}：{item['name']}")
        pct = item.get("percent", -1)
        bar.set(pct)
        if pct < 0:
            text_lbl.configure(text=item.get("charge") or "无电量")
        else:
            text_lbl.configure(text=f"{pct}% {item.get('charge', '')}".strip())

    def _topmost_style(self):
        """按钮文字/配色：置顶态显示「置顶」，非置顶态显示「置中」。"""
        if self.topmost:
            return ("置顶", "#2a3550", ACCENT)
        return ("置中", SOFT_BG, TEXT_SUB)

    def apply_display_scope(self, scope: str):
        """由设置项的「显示范围」同步悬浮窗层级与按钮（全屏=置顶）。"""
        self.topmost = (scope == config.SCOPE_FULLSCREEN)
        self.win.attributes("-topmost", self.topmost)
        text, bg, fg = self._topmost_style()
        self.top_btn.configure(text=text, bg=bg, fg=fg)
        self._apply_lock_state()
        # 置顶状态一变，Windows 立刻重排顶层窗口 Z 序；独立背景图层可能被
        # 顶到悬浮窗上方，玻璃模式下的内容就像"消失"了。马上压回去。
        try:
            self.app._relower_picture_now()
        except Exception:
            pass

    def _toggle_topmost(self):
        """点悬浮窗「置顶/置中」按钮 → 切换显示范围（与设置项双向同步）。"""
        new_scope = (config.SCOPE_DESKTOP if self.topmost else config.SCOPE_FULLSCREEN)
        self.app.set_display_scope(new_scope)

    def _apply_lock_state(self):
        """置顶=锁定位置不可拖动；非置顶=可拖动（光标提示 move）。"""
        try:
            self.canvas.configure(cursor="arrow" if self.topmost else "fleur")
        except Exception:
            pass

    def show(self):
        self.win.deiconify()
        self.win.attributes("-topmost", self.topmost)
        # 悬浮窗只作为托盘程序的浮层，不该在任务栏留按钮
        hide_from_taskbar(self.win)

    def hide(self):
        self.win.withdraw()

    # ---- 缩放：命中检测 / 光标 ----
    # 注意：必须用 Tk 支持的光标名，CSS 风格（nwse-resize 等）会抛 TclError。
    _CURSORS = {
        "nw": "top_left_corner", "se": "bottom_right_corner",
        "ne": "top_right_corner", "sw": "bottom_left_corner",
        "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
        "w": "sb_h_double_arrow", "e": "sb_h_double_arrow",
    }

    def _hit_zone(self, x, y):
        """返回鼠标所在区域：'nw'/'n'/…/'se' 表示缩放手柄，None 表示可移动区。"""
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        # 热区覆盖整个可见边框带（内容 frame 从 S(18) 开始），并随窗口变小收紧
        e = min(S(18), max(6, w // 4), max(6, h // 4))
        left, right = x <= e, x >= w - e
        top, bottom = y <= e, y >= h - e
        if top and left:
            return "nw"
        if top and right:
            return "ne"
        if bottom and left:
            return "sw"
        if bottom and right:
            return "se"
        if top:
            return "n"
        if bottom:
            return "s"
        if left:
            return "w"
        if right:
            return "e"
        return None

    def _on_hover(self, event):
        zone = self._hit_zone(event.x, event.y)
        if zone in self._CURSORS:
            cursor = self._CURSORS[zone]      # 边框/角落 → 缩放光标
        elif self.topmost:
            cursor = "arrow"                  # 置顶锁定，中间区不可移动
        else:
            cursor = "fleur"                  # 可拖动（Tk 无 move 光标，用 fleur 十字箭头）
        try:
            if self.canvas.cget("cursor") != cursor:
                self.canvas.configure(cursor=cursor)
        except tk.TclError:
            pass                              # 个别主题下光标名不可用时静默忽略

    def _on_leave(self, event):
        """鼠标离开窗口：光标复位为默认/移动。"""
        cursor = "arrow" if self.topmost else "fleur"
        try:
            if self.canvas.cget("cursor") != cursor:
                self.canvas.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _on_configure(self, event):
        """窗口尺寸变化：重绘圆角背景，使卡片填满新尺寸。"""
        w, h = event.width, event.height
        if w <= 0 or h <= 0:
            return
        try:
            self.canvas.delete(self._bg_item)
            self._bg_item = round_rect(
                self.canvas, S(6), S(6), w - S(6), h - S(6), S(18),
                fill=TRANSPARENT if self._glass else CARD_BG,
                outline=BORDER, width=1)
        except Exception:
            pass

    def _on_win_configure(self, event):
        """窗口移动/缩放：背景图层窗口立刻跟位，停手后再重渲染图片。"""
        try:
            self.app.on_window_geometry_changed()
        except Exception:
            pass

    def _event_pos(self, event):
        """事件在悬浮窗客户区里的坐标。

        ★ 不能用 event.x_root：背景图层转发过来的鼠标事件，其根坐标在
        override-redirect 窗口上算不对（会当成窗口在原点），拖动就会跳位。
        这里用控件相对窗口的偏移自己累加，天然与转发/原生事件无关。
        """
        x, y = event.x, event.y
        try:
            w = event.widget
            while w is not None and w is not self.win:
                x += w.winfo_x()
                y += w.winfo_y()
                w = getattr(w, "master", None)
        except Exception:
            pass
        return x, y

    def _event_screen_pos(self, event):
        """事件对应的屏幕坐标。

        拖动位移必须用屏幕坐标：客户区坐标会随窗口自身移动而变化，
        用它算位移会导致窗口只走一半甚至来回跳。
        这里用 ClientToScreen 换算，原生事件与转发事件都准。
        """
        try:
            u = ctypes.windll.user32
            hwnd = int(self.win.winfo_id())
            parent = int(u.GetParent(hwnd))
            if parent:
                hwnd = parent
            pt = ctypes.wintypes.POINT(0, 0)
            u.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(pt))
            lx, ly = self._event_pos(event)
            return pt.x + lx, pt.y + ly
        except Exception:
            return event.x_root, event.y_root

    def _relower_picture(self, event=None):
        """把背景图层压回悬浮窗下方（点/激活后 Z 序会被打乱）。"""
        try:
            if self.app._bg_active and self.app.picture.visible:
                self.app.picture.reattach()
        except Exception:
            pass

    def _drag_start(self, event):
        # 交互控件（按钮、透明度滑块）自己处理鼠标，不触发窗口拖动
        if isinstance(event.widget, (tk.Button, tk.Scale)):
            return
        lx, ly = self._event_pos(event)
        zone = self._hit_zone(lx, ly)
        sx, sy = self._event_screen_pos(event)
        if zone in self._CURSORS:
            # 缩放：置顶与否都允许调整大小
            self.resize_origin = (
                zone, sx, sy,
                self.win.winfo_x(), self.win.winfo_y(),
                self.win.winfo_width(), self.win.winfo_height())
            self.drag_origin = None
            return
        # 中间区：置顶状态下位置锁定，不可拖动；先点「置中」取消置顶
        if self.topmost:
            return
        self.drag_origin = (sx, sy,
                            self.win.winfo_x(), self.win.winfo_y())

    def _drag_move(self, event):
        # ---- 缩放 ----
        if self.resize_origin:
            zone, sx, sy, wx, wy, ww, wh = self.resize_origin
            cx, cy = self._event_screen_pos(event)
            dx, dy = cx - sx, cy - sy
            minw, minh = S(MIN_FLOAT[0]), S(MIN_FLOAT[1])
            nx, ny, nw, nh = wx, wy, ww, wh
            if "e" in zone:
                nw = min(max(ww + dx, minw), self._screen_size[0] - wx)
            if "s" in zone:
                nh = min(max(wh + dy, minh), self._screen_size[1] - wy)
            if "w" in zone:
                new_w = min(max(ww - dx, minw), wx + ww)
                nx = wx + ww - new_w
                nw = new_w
            if "n" in zone:
                new_h = min(max(wh - dy, minh), wy + wh)
                ny = wy + wh - new_h
                nh = new_h
            self.win.geometry(f"{nw}x{nh}+{nx}+{ny}")
            self._win_size = (nw, nh)
            self._schedule_size_save()
            return
        # ---- 移动 ----
        if self.topmost or not self.drag_origin:
            return
        ox, oy, wx, wy = self.drag_origin
        cx, cy = self._event_screen_pos(event)
        nx, ny = wx + cx - ox, wy + cy - oy
        self.win.geometry(f"+{nx}+{ny}")
        # 背景图层跟着同步挪（只改位置，最轻），保证拖动跟手不拖影
        try:
            self.app.picture.move_to(nx, ny)
        except Exception:
            pass
        self._schedule_pos_save()

    def _drag_end(self, event):
        """鼠标松开：若刚缩放过则立即落盘尺寸。"""
        if self.resize_origin is not None:
            self.resize_origin = None
            self.flush_size()
        self.drag_origin = None

    def _schedule_pos_save(self):
        if self._pos_save_job is not None:
            try:
                self.app.root.after_cancel(self._pos_save_job)
            except Exception:
                pass
        self._pos_save_job = self.app.root.after(500, self.flush_pos)

    def _schedule_size_save(self):
        if self._size_save_job is not None:
            try:
                self.app.root.after_cancel(self._size_save_job)
            except Exception:
                pass
        self._size_save_job = self.app.root.after(500, self.flush_size)


# ---------------------------------------------------------------------------
# 任务栏托盘图标：鼠标悬停时显示设备电量
# ---------------------------------------------------------------------------
# 64 位安全的 WNDPROC 签名：Windows 的 LPARAM/WPARAM/LRESULT 在 64 位是 64 位，
# 用 ctypes.c_long（Windows 上恒为 32 位）会截断导致 lParam 错乱。
if ctypes.sizeof(ctypes.c_void_p) == 8:
    _LRESULT = ctypes.c_longlong
    _WPARAM = ctypes.c_ulonglong
    _LPARAM = ctypes.c_longlong
else:
    _LRESULT = ctypes.c_long
    _WPARAM = ctypes.c_uint
    _LPARAM = ctypes.c_long
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_void_p, ctypes.c_uint,
                              _WPARAM, _LPARAM)


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


def make_battery_icon(size: int = 32) -> int:
    """用 GDI 画一个电池形状的 HICON（透明背景），返回句柄，失败返回 0。"""
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32

    class _BIH(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint), ("biWidth", ctypes.c_int),
            ("biHeight", ctypes.c_int), ("biPlanes", ctypes.c_ushort),
            ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint),
            ("biSizeImage", ctypes.c_uint), ("biXPelsPerMeter", ctypes.c_int),
            ("biYPelsPerMeter", ctypes.c_int), ("biClrUsed", ctypes.c_uint),
            ("biClrImportant", ctypes.c_uint),
        ]

    class _ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", ctypes.c_int), ("xHotspot", ctypes.c_uint),
                    ("yHotspot", ctypes.c_uint), ("hbmMask", ctypes.c_void_p),
                    ("hbmColor", ctypes.c_void_p)]

    hdc = u.GetDC(0)
    memdc = g.CreateCompatibleDC(hdc)
    bmi = _BIH()
    bmi.biSize = ctypes.sizeof(_BIH)
    bmi.biWidth = size
    bmi.biHeight = -size
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bits = ctypes.c_void_p()
    color = g.CreateDIBSection(memdc, ctypes.byref(bmi), 0, ctypes.byref(bits),
                               None, 0)
    if not color or not bits:
        g.DeleteDC(memdc)
        u.ReleaseDC(0, hdc)
        return 0

    buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
    for i in range(size * size * 4):
        buf[i] = 0                      # 全透明

    def put(x, y, r, gg, b, a=255):
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 4
            buf[i] = b
            buf[i + 1] = gg
            buf[i + 2] = r
            buf[i + 3] = a

    # 电池外壳（浅灰描边）+ 内部绿色电量
    x0, y0, x1, y1 = 3, 9, 26, 23
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            edge = (x in (x0, x1) or y in (y0, y1))
            if edge:
                put(x, y, 210, 214, 219)
            else:
                put(x, y, 34, 197, 94)   # 绿色电量
    # 正极凸起
    for x in range(x1 + 1, x1 + 4):
        for y in range(y0 + 4, y1 - 3):
            put(x, y, 210, 214, 219)

    # AND 掩码全 0 = 完全不透明（不传位图时 CreateBitmap 内容未初始化，
    # 可能全是 1，导致整个图标被当成透明而看不见）
    stride = ((size + 15) // 16) * 2      # 1bpp 每行按 16 位对齐
    mask_bits = (ctypes.c_ubyte * (stride * size))()
    mask = g.CreateBitmap(size, size, 1, 1, mask_bits)
    ii = _ICONINFO(1, 0, 0, mask, color)
    hicon = u.CreateIconIndirect(ctypes.byref(ii))

    g.DeleteObject(color)
    if mask:
        g.DeleteObject(mask)
    g.DeleteDC(memdc)
    u.ReleaseDC(0, hdc)
    return int(hicon or 0)


def promote_tray_icon() -> bool:
    """把本程序的托盘图标设为「常显」。

    Windows 11 默认把新图标折叠进 ^ 隐藏区，对应注册表
    HKCU\\Control Panel\\NotifyIconSettings\\<hash>\\IsPromoted，
    置 1 即固定显示在任务栏上。
    """
    try:
        import winreg
    except Exception:
        return False
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r"Control Panel\NotifyIconSettings")
    except Exception:
        return False
    exe = os.path.abspath(sys.executable).lower()
    changed = False
    index = 0
    while True:
        try:
            sub = winreg.EnumKey(root, index)
        except OSError:
            break
        index += 1
        try:
            key = winreg.OpenKey(root, sub, 0, winreg.KEY_READ)
            path = winreg.QueryValueEx(key, "ExecutablePath")[0]
            winreg.CloseKey(key)
        except Exception:
            continue
        if not path or path.lower() != exe:
            continue
        try:
            key = winreg.OpenKey(root, sub, 0, winreg.KEY_SET_VALUE)
            cur = None
            try:
                cur = winreg.QueryValueEx(key, "IsPromoted")[0]
            except Exception:
                pass
            if cur != 1:
                winreg.SetValueEx(key, "IsPromoted", 0, winreg.REG_DWORD, 1)
                changed = True
            winreg.CloseKey(key)
        except Exception:
            pass
    try:
        winreg.CloseKey(root)
    except Exception:
        pass
    return changed


class TrayIcon:
    """通知区电池图标。

    悬停 → 系统 szTip 显示鼠标/键盘电量；
    左键单击 → 打开大界面；右键单击 → 菜单（打开控制中心 / 悬浮窗 / 退出）。
    """

    NIM_ADD = 0
    NIM_MODIFY = 1
    NIM_DELETE = 2
    NIF_MESSAGE = 0x01
    NIF_ICON = 0x02
    NIF_TIP = 0x04
    WM_USER = 0x0400
    IDI_APPLICATION = 32512

    def __init__(self, app, hwnd: int):
        self.app = app
        self.hwnd = hwnd
        self.uid = 1
        self.last_tip = ""
        self.added = False
        self.tray_hwnd = None
        self.icon_rect = None
        self.mouse_in = False
        self.left_down = False
        self.right_down = False
        self._menu = None        # 右键菜单保活
        self._prev_wndproc = None   # 原窗口过程
        self._wndproc_ref = None    # 回调引用（防 GC）
        self._last_action = 0.0     # 动作去重时间戳
        self._init_tray_target()
        self._add()
        # 优先用 Windows 原生托盘回调消息处理左右键；轮询作为兜底
        self._subclass_wndproc()

    # ---------- 托盘交互（轮询检测） ----------
    def _init_tray_target(self) -> None:
        """托盘图标挂在 Tk 主窗口上（悬停提示由系统弹出）。

        ★ 为什么不用 WM_USER+20 回调消息：
        子类化窗口过程在这台机器上始终拿不到正确的 lParam
        （Windows 发的托盘回调里，鼠标事件在 lParam 低 16 位，
        但经过 Tk 的窗口过程分发后取到的值不对），
        而用 ctypes 自建 WNDCLASSEX 也始终失败（ERROR_CLASS_ALREADY_EXISTS）。
        改为轮询图标矩形 + 鼠标位置，稳定且无副作用。
        """
        self.tray_hwnd = self.hwnd

    def _subclass_wndproc(self) -> None:
        """子类化 owner 窗口过程，接收托盘原生回调消息（WM_USER+20）。

        Shell_NotifyIcon 会把鼠标事件以消息形式发到 nid.hWnd：
          wParam = uID, lParam 低 16 位 = 鼠标事件码
        （WM_LBUTTONUP / WM_RBUTTONUP / WM_LBUTTONDBLCLK 等）。
        这比轮询 GetAsyncKeyState 可靠得多，不会漏掉快速点击。
        """
        try:
            u = ctypes.windll.user32
            hwnd = self._owner_hwnd()
            if not hwnd:
                return
            GWLP_WNDPROC = -4
            is64 = ctypes.sizeof(ctypes.c_void_p) == 8
            getlp = u.GetWindowLongPtrW if is64 else u.GetWindowLongW
            setlp = u.SetWindowLongPtrW if is64 else u.SetWindowLongW
            getlp.restype = ctypes.c_void_p
            getlp.argtypes = [ctypes.c_void_p, ctypes.c_int]
            setlp.restype = ctypes.c_void_p
            setlp.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            # 64 位签名：避免 lParam 被当成 32 位截断
            u.CallWindowProcW.restype = _LRESULT
            u.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_uint, _WPARAM, _LPARAM]

            old = getlp(ctypes.c_void_p(hwnd), GWLP_WNDPROC)
            if not old:
                return
            self._prev_wndproc = old
            tray = self

            @_WNDPROC
            def wndproc(h, msg, wp, lp):
                # 托盘图标回调消息
                if msg == tray.WM_USER + 20:
                    event = lp & 0xFFFF           # LOWORD(lParam) = 鼠标事件码
                    WM_LBUTTONUP = 0x0202
                    WM_RBUTTONUP = 0x0205
                    WM_LBUTTONDBLCLK = 0x0203
                    if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                        tray._fire(tray.app.show_main)
                        return 0
                    if event == WM_RBUTTONUP:
                        tray._fire(tray._show_menu)
                        return 0
                # 其余消息交回原窗口过程（Tk 自身依赖它工作）
                return u.CallWindowProcW(
                    ctypes.c_void_p(tray._prev_wndproc), h, msg, wp, lp)

            self._wndproc_ref = wndproc      # 保活，防止被 GC
            setlp(ctypes.c_void_p(hwnd), GWLP_WNDPROC,
                  ctypes.cast(wndproc, ctypes.c_void_p))
        except Exception:
            pass

    def _fire(self, action) -> None:
        """触发一次托盘动作（0.4 秒内去重，避免消息回调+轮询重复执行）。

        回调已在 Tk 主线程（DispatchMessage），直接调用即可；
        用 after(0) 排队在消息重入时可能不被处理。
        """
        now = time.monotonic()
        if now - self._last_action < 0.4:
            return
        self._last_action = now
        try:
            action()
        except Exception:
            pass

    def poll_mouse(self) -> None:
        """轮询托盘图标上的鼠标状态。

        左键单击 → 打开大界面；右键单击 → 弹出菜单。
        电量由系统 szTip 在鼠标停留时显示（update() 持续刷新）。
        """
        if not self.added:
            return
        try:
            rect = self._get_rect()
            if rect is None:
                # 偶发取不到时用上一次成功的位置兜底
                rect = self.icon_rect
            if rect is None:
                return
            self.icon_rect = rect
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            inside = (rect.left - 2 <= pt.x <= rect.right + 2 and
                      rect.top - 2 <= pt.y <= rect.bottom + 2)
            user32 = ctypes.windll.user32
            # VK_LBUTTON=0x01 / VK_RBUTTON=0x02
            left = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
            right = bool(user32.GetAsyncKeyState(0x02) & 0x8000)

            # 左键：按下→抬起都在图标上视为一次单击 → 打开大界面
            if inside and left and not self.left_down:
                self.left_down = True
            elif inside and not left and self.left_down:
                self.left_down = False
                self._fire(self.app.show_main)
            elif not inside:
                self.left_down = False

            # 右键：同样规则 → 弹出原菜单
            if inside and right and not self.right_down:
                self.right_down = True
            elif inside and not right and self.right_down:
                self.right_down = False
                self._fire(self._show_menu)
            elif not inside:
                self.right_down = False

            self.mouse_in = inside
        except Exception:
            pass

    def _get_rect(self):
        """取托盘图标在屏幕上的矩形。"""
        try:
            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = ctypes.c_void_p(self._owner_hwnd())
            nid.uID = self.uid
            rc = ctypes.wintypes.RECT()
            hr = ctypes.windll.shell32.Shell_NotifyIconGetRect(
                ctypes.byref(nid), ctypes.byref(rc))
            if hr == 0:
                return rc
        except Exception:
            pass
        return None

    def _show_menu(self) -> None:
        """右键：Windows 原生弹出菜单，返回选中项 ID 后直接分发。

        ★ 不用 tk.Menu.tk_popup()：菜单是在托盘回调 wndproc 里弹出的，
        此时 Tcl 解释器被重入，tk_popup 菜单的 command 不会被执行，
        三个菜单项全都点不动。原生 TrackPopupMenuEx 用 TPM_RETURNCMD
        同步返回选中项，绕开 Tk，稳定可靠。
        """
        try:
            u = ctypes.windll.user32
            hwnd = self._owner_hwnd()

            MF_STRING = 0x0000
            MF_SEPARATOR = 0x0800
            TPM_RETURNCMD = 0x0100
            TPM_RIGHTBUTTON = 0x0002
            ID_MAIN, ID_FLOAT, ID_QUIT = 1, 2, 3

            u.CreatePopupMenu.restype = ctypes.c_void_p
            hmenu = u.CreatePopupMenu()
            if not hmenu:
                return
            u.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.c_void_p, ctypes.c_wchar_p]
            u.AppendMenuW(ctypes.c_void_p(hmenu), MF_STRING,
                          ctypes.c_void_p(ID_MAIN), "打开控制中心")
            u.AppendMenuW(ctypes.c_void_p(hmenu), MF_STRING,
                          ctypes.c_void_p(ID_FLOAT), "打开悬浮窗")
            u.AppendMenuW(ctypes.c_void_p(hmenu), MF_SEPARATOR,
                          ctypes.c_void_p(0), None)
            u.AppendMenuW(ctypes.c_void_p(hmenu), MF_STRING,
                          ctypes.c_void_p(ID_QUIT), "退出程序")

            pt = ctypes.wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            # 前置窗口，否则菜单弹出后点一下就消失（Windows 焦点规则）
            u.SetForegroundWindow(ctypes.c_void_p(hwnd))
            u.TrackPopupMenuEx.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                ctypes.c_void_p, ctypes.c_void_p]
            u.TrackPopupMenuEx.restype = ctypes.c_uint
            cmd = u.TrackPopupMenuEx(
                ctypes.c_void_p(hmenu),
                TPM_RETURNCMD | TPM_RIGHTBUTTON,
                pt.x, pt.y, ctypes.c_void_p(hwnd), None)
            u.DestroyMenu(ctypes.c_void_p(hmenu))

            if cmd == ID_MAIN:
                self.app.show_main()
            elif cmd == ID_FLOAT:
                self.app.show_float()
            elif cmd == ID_QUIT:
                self.app.quit_app()
        except Exception:
            pass

    def _icon_handle(self):
        try:
            hicon = make_battery_icon(32)
            if hicon:
                return hicon
            return ctypes.windll.user32.LoadIconW(None, self.IDI_APPLICATION)
        except Exception:
            return None

    def _owner_hwnd(self) -> int:
        """图标归属窗口：消息窗口的顶层句柄（托盘回调发到这里）。"""
        return int(self.tray_hwnd or self.hwnd or 0)

    def _add(self) -> None:
        if not self._owner_hwnd():
            return
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = ctypes.c_void_p(self._owner_hwnd())
        nid.uID = self.uid
        nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        nid.uCallbackMessage = self.WM_USER + 20
        nid.hIcon = self._icon_handle()
        nid.szTip = "外设电量 · 正在读取…"
        try:
            ok = ctypes.windll.shell32.Shell_NotifyIconW(
                self.NIM_ADD, ctypes.byref(nid))
            self.added = bool(ok)
        except Exception:
            self.added = False

    def update(self, tip: str) -> None:
        """更新悬停提示文字（<=127 字符）。"""
        if not self.added or not tip:
            return
        tip = tip[:127]
        if tip == self.last_tip:
            return
        self.last_tip = tip
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = ctypes.c_void_p(self._owner_hwnd())
        nid.uID = self.uid
        nid.uFlags = self.NIF_TIP
        nid.szTip = tip
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_MODIFY,
                                                   ctypes.byref(nid))
        except Exception:
            pass

    def remove(self) -> None:
        if not self.added:
            return
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = ctypes.c_void_p(self._owner_hwnd())
        nid.uID = self.uid
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_DELETE,
                                                   ctypes.byref(nid))
        except Exception:
            pass
        self.added = False


# ---------------------------------------------------------------------------
# 应用主体
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk, start_mode: str = "float"):
        self.root = root
        self.core = Core()
        self.core.start()
        self._last_version = -1
        self.mode = "main"
        self.bg_target = "main"      # 背景页当前正在设置的目标（main / float）

        self.main = MainWindow(root, self)
        self.float = FloatWindow(self)

        # 背景（本地图片/视频）：大界面和悬浮窗各自一个素材源。
        # 同一时刻只有一个窗口可见，所以仍共用一个图层窗口，贴到当前窗口下方。
        self.backdrops = {k: backdrop.Backdrop() for k in ("main", "float")}
        self.picture = PictureWindow(root)
        self._bg_enabled = {"main": False, "float": False}  # 各目标是否启用背景
        self._bg_active = False      # 任一目标启用（兼容旧判断，如悬浮窗压层级）
        self._bg_sync_job = None
        self._bg_render_keys = {"main": None, "float": None}
        self._bg_last_advance = 0.0
        self._bg_last_render = 0.0
        self._bg_next_tick = 0.0
        self._bg_acc = 0.0
        self._bg_tick_job = None      # 动画定时器句柄：按需启停，不常驻空转
        self._bg_alpha_set = None     # 背景图层透明度缓存，避免每帧设 Win32 属性

        global _APP_ROOT
        _APP_ROOT = root

        # 任务栏托盘图标：悬停显示两台设备电量
        try:
            child = int(root.winfo_id())
            hwnd = int(ctypes.windll.user32.GetParent(child)) or child
            if not hwnd:
                raise RuntimeError("no hwnd")
            self.tray = TrayIcon(self, hwnd)
        except Exception:
            self.tray = None
        # 图标默认会被折叠进 ^ 隐藏区，稍后补一次「常显」标记
        root.after(2500, promote_tray_icon)
        # 窗口映射后重设一次图标（winfo_id 的父窗口此时才可用）
        root.after(600, lambda: apply_window_icon(root))

        {"main": self.show_main,
         "float": self.show_float}.get(start_mode, self.show_main)()

        # 应用启动时保存的主题（控件已创建，遍历改色）
        try:
            apply_theme(config.get_theme())
            self.main.refresh_theme()
        except Exception:
            pass

        # 背景：大界面 / 悬浮窗分别按配置加载（图片 / 视频 / GIF 都支持）
        try:
            for target in ("main", "float"):
                saved_bg = config.get_bg_media(target)
                if not saved_bg:
                    continue
                if self.backdrops[target].load(saved_bg):
                    self._bg_enabled[target] = True
                else:
                    config.set_bg_media("", target)
            self._bg_active = any(self._bg_enabled.values())
            # 各自窗口的玻璃模式只跟自己的背景走，没背景的窗口保持正常实色
            self.main.set_glass(self._bg_enabled["main"])
            self.float.set_glass(self._bg_enabled["float"])
            self.main.refresh_bg_media()
            self._apply_bg_for_mode()
            # _apply_bg_for_mode 内部按素材类型决定是否启动定时器：
            # 静态图不启动，动图才启动，避免无谓的 120Hz 空转
        except Exception:
            pass

        # 主窗口移动/缩放/激活时同步背景图层窗口
        root.bind("<Configure>", self._on_root_configure)
        root.bind("<Activate>", lambda e: self.sync_background())
        root.bind("<Unmap>", lambda e: self.picture.detach())
        root.bind("<Map>", lambda e: self.refresh_background())

        # 重复启动通知事件（其他地方再次双击 exe 时触发，用来弹出本窗口）
        self._show_event = _create_show_event()
        self._check_show_event()

        self._poll()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _check_show_event(self):
        """若收到'显示窗口'信号，弹出悬浮窗；持续轮询。"""
        try:
            if self._show_event:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                # WAIT_OBJECT_0=0 表示事件被置位（自动重置）
                if k32.WaitForSingleObject(self._show_event, 0) == 0:
                    self.show_float()
        except Exception:
            pass
        try:
            self.root.after(200, self._check_show_event)
        except Exception:
            pass

    # ---------- 模式切换 ----------
    def show_main(self):
        self.mode = "main"
        self.float.hide()
        self.root.deiconify()
        self.root.attributes("-topmost", False)
        self._force_refresh()
        self._bg_alpha_set = None      # 大界面 alpha=1，强制刷一次图层
        self._apply_bg_for_mode()

    def show_float(self):
        self.mode = "float"
        self.root.withdraw()
        self.float.show()
        self._force_refresh()
        self._bg_alpha_set = None      # 悬浮窗用悬浮透明度，强制刷一次
        self._apply_bg_for_mode()

    def cycle_mode(self):
        """迷你窗上的「切」：迷你 → 悬浮 → 大界面 → 迷你。"""
        {"float": self.show_main}.get(self.mode, self.show_float)()

    # ---------- 状态轮询（主线程） ----------
    def _poll(self):
        if self.core.version != self._last_version:
            self._last_version = self.core.version
            self.main.refresh()
            self.float.refresh()
            if self.tray:
                self.tray.update(device_tip_text(self.core.snapshot()))
        # 托盘图标上的鼠标状态（点击弹菜单）
        if self.tray:
            self.tray.poll_mouse()
        # 背景图层要一直垫在目标窗口下方。低频兜底重压层级：Windows 在
        # 窗口激活/切换后会把无主图层顶上来盖住文字，仅靠幂等 attach 无法
        # 恢复（参数没变就跳过）。每 2 秒 reattach 一次（只动 Z 序、不重排
        # 几何，代价极小），既防盖住文字，又不会像旧版每 250ms 那样闪烁。
        self._poll_tick = getattr(self, "_poll_tick", 0) + 1
        if (self._bg_enabled.get(self.mode) and self._poll_tick % 8 == 0
                and self.picture.visible):
            try:
                self.picture.reattach()
            except Exception:
                pass
        self.root.after(250, self._poll)

    def _force_refresh(self):
        self._last_version = -1

    # ---------- 共享操作 ----------
    def toggle_autostart(self):
        ok, title, detail = autostart_toggle()
        if ok:
            messagebox.showinfo(title, detail, parent=self.root)
        else:
            messagebox.showerror(title, detail, parent=self.root)
        self._force_refresh()

    def set_exit_mode(self, mode: str) -> None:
        """切换退出方式：托管在后台 / 退出主程序（切换后不再弹提示框）。"""
        if not config.set_exit_mode(mode):
            messagebox.showerror("保存失败", "无法写入配置文件。", parent=self.root)
            return
        self.main.refresh_exit_mode()

    def set_float_on_bg(self, value: str) -> None:
        """切换：后台运行时悬浮窗是否显示（是 / 否）。"""
        if not config.set_float_on_bg(value):
            messagebox.showerror("保存失败", "无法写入配置文件。", parent=self.root)
            return
        self.main.refresh_float_on_bg()

    def set_theme(self, theme: str) -> None:
        """切换主题：深色 / 浅色（全白），立即应用到所有窗口。"""
        if not config.set_theme(theme):
            messagebox.showerror("保存失败", "无法写入配置文件。", parent=self.root)
            return
        apply_theme(theme)
        self.main.refresh_theme()
        # 背景图的明暗跟主题走，重新渲染一次
        self.refresh_background()

    def set_display_scope(self, scope: str) -> None:
        """切换显示范围：桌面（非置顶）/ 全屏（置顶覆盖所有应用）。"""
        if not config.set_display_scope(scope):
            messagebox.showerror("保存失败", "无法写入配置文件。", parent=self.root)
            return
        self.main.refresh_display_scope()
        # 同步悬浮窗的置顶状态与按钮文字（与设置项双向一致）
        self.float.apply_display_scope(scope)

    # ---------- 背景（本地图片 / 视频） ----------
    def _bg_shade(self, target: str = "main"):
        """按主题决定背景图的明暗与模糊（模糊值取当前目标的设置）。

        深色主题：整体压暗，浅色文字才看得清；
        浅色主题：整体提亮 + 轻微模糊，深色文字才看得清。
        返回 (压暗系数, 提亮比例, 模糊半径)。
        """
        blur = float(config.get_bg_blur(target))
        if config.get_theme() == config.THEME_LIGHT:
            return 1.0, 0.45, max(blur, 1.5)
        return 0.30, 0.0, blur

    def _bg_veil(self) -> float:
        """背景上盖的色纱浓度：压住亮度，让界面文字始终清晰。"""
        if config.get_theme() == config.THEME_LIGHT:
            return 0.34
        return 0.32

    def _bg_veil_color(self):
        """色纱颜色：深色主题用黑纱，浅色主题用白纱。"""
        if config.get_theme() == config.THEME_LIGHT:
            return (255, 255, 255)
        return (0, 0, 0)

    def set_background(self, active: bool, target: str = "main") -> None:
        """开 / 关指定目标（大界面 / 悬浮窗）的背景。

        两个窗口的玻璃模式互不影响；只重贴"当前可见"的那个窗口的图层。
        """
        if target not in ("main", "float"):
            return
        self._bg_enabled[target] = bool(active)
        self._bg_active = any(self._bg_enabled.values())
        if target == "main":
            self.main.set_glass(bool(active))
        else:
            self.float.set_glass(bool(active))
        if target == self.mode:
            self._apply_bg_for_mode()

    def _apply_bg_for_mode(self) -> None:
        """模式切换 / 背景变化后：贴当前模式的图层，按需启停动画定时器。"""
        self._bg_active = any(self._bg_enabled.values())
        active = self._bg_enabled.get(self.mode, False)
        bd = self.backdrops[self.mode]
        animating = active and bd.is_video()
        # 两个都是高帧率视频时，只让当前可见的那个后台预解码，避免双倍 CPU
        for key, item in self.backdrops.items():
            try:
                if key == self.mode and active and item.is_video():
                    item.resume()
                else:
                    item.pause()
            except Exception:
                pass
        # 只有"真正播放动图"才需要高精度计时和定时器；静态图/关闭时全停，
        # 避免 120Hz 定时器常驻空转把主线程事件循环占满（所有界面卡顿）。
        try:
            _set_timer_resolution(animating)
        except Exception:
            pass
        self._stop_bg_tick()
        if active:
            self.refresh_background()
            # 切窗口 / 切置顶后强制压一次层级：attach() 是幂等的，目标窗口
            # 没变时不会重排 Z 序，但 Windows 自己会重排，必须显式纠正。
            self._relower_picture_now()
            if animating:
                self._start_bg_tick()
        else:
            self.picture.detach()

    def _relower_picture_now(self) -> None:
        """立刻把背景图层压回当前窗口下方，并补两次延时重压。

        置顶 / 置中切换会让 Windows 重排顶层窗口；背景图层是独立的顶层
        工具窗，可能被顶到目标窗口上方，玻璃模式下看起来就是内容突然
        消失。延时两补是为了等系统异步完成 topmost 相关的层级调整。
        """
        def _do():
            if (self._bg_enabled.get(self.mode, False)
                    and self.picture.visible):
                try:
                    self.picture.reattach()
                except Exception:
                    pass
        _do()
        for delay in (60, 250):
            try:
                self.root.after(delay, _do)
            except Exception:
                pass

    def _stop_bg_tick(self) -> None:
        """停掉动画定时器，并把时基归零。"""
        job = self._bg_tick_job
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._bg_tick_job = None
        self._bg_next_tick = 0.0
        self._bg_acc = 0.0

    def _start_bg_tick(self) -> None:
        """启动动画定时器（幂等）。"""
        if self._bg_tick_job is not None:
            return
        if not (self._bg_enabled.get(self.mode, False)
                and self.backdrops[self.mode].is_video()):
            return
        self._bg_next_tick = 0.0
        self._bg_tick()

    def _target_window(self):
        """当前可见的目标窗口（大界面 or 悬浮窗）。"""
        return self.main.root if self.mode == "main" else self.float.win

    def refresh_background(self, reposition: bool = True) -> None:
        """按目标窗口尺寸渲染背景画面。

        reposition=False 供动画帧用：只更新画布内容，不重贴/压层级窗口。
        每帧都 attach 会把 120fps 变成 120 次 Win32 重定位 → 闪烁 + 卡顿；
        图层窗口的位置由拖动/缩放/窗口事件单独同步，动画只管换画面。
        """
        bd = self.backdrops[self.mode]
        if not self._bg_enabled.get(self.mode, False) or not bd.has_media():
            if reposition:
                self.picture.detach()
            return
        win = self._target_window()
        w = win.winfo_width()
        h = win.winfo_height()
        if w <= 1 or h <= 1:
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        if w <= 1 or h <= 1:
            return
        # 尺寸没变就不用重新渲染（拖动时只挪位置，避免卡顿）
        key = (self.mode, w, h, config.get_theme(),
               config.get_bg_blur(self.mode), bd.generation)
        if (key == self._bg_render_keys[self.mode] and self.picture.photo is not None
                and not bd.is_video()):
            if reposition:
                self.sync_background()
            return
        dark, white, blur = self._bg_shade(self.mode)
        try:
            bd.sharpen = config.get_bg_sharpen(self.mode)
        except Exception:
            pass
        if self.mode == "main":
            frame = bd.render(w, h, dark=dark, white=white, blur=blur,
                              veil=self._bg_veil())
        else:
            # 悬浮窗：把图合成到圆角卡片里，四角留透明；盖一层色纱保证文字可读
            frame = bd.render(w, h, dark=dark, white=white,
                              radius=S(18), inset=S(6), blur=blur,
                              veil=self._bg_veil(),
                              veil_bgr=self._bg_veil_color(),
                              key_bgr=backdrop.hex_to_bgr(TRANSPARENT))
        if frame is None:
            return
        # 直接把 numpy 帧交给图层：尺寸不变时原地 put，省掉每帧新建图片
        self.picture.paint(frame)
        self._bg_render_keys[self.mode] = key
        # 悬浮窗的不透明度要跟背景图层一致，否则滑块看着不起作用。
        # 缓存上一次的值，动画帧不再重复调用 Win32 窗口属性。
        alpha = self.float.alpha if self.mode == "float" else 1.0
        if alpha != self._bg_alpha_set:
            try:
                self.picture.win.attributes("-alpha", alpha)
                self._bg_alpha_set = alpha
            except Exception:
                pass
        if reposition:
            self.sync_background()

    def sync_background(self) -> None:
        """把图层窗口贴到目标窗口正下方（同时压住层级）。"""
        if (not self._bg_enabled.get(self.mode, False)
                or not self.backdrops[self.mode].has_media()):
            self.picture.detach()
            return
        win = self._target_window()
        try:
            if not win.winfo_ismapped():
                self.picture.detach()
                return
        except Exception:
            self.picture.detach()
            return
        w = win.winfo_width()
        h = win.winfo_height()
        if w <= 1 or h <= 1:
            return
        # 鼠标事件要转发到承载内容的控件（悬浮窗画布 / 大界面根窗口）
        try:
            if self.mode == "main":
                self.picture.set_target(self.main.root)
            else:
                self.picture.set_target(self.float.canvas)
        except Exception:
            self.picture.set_target(None)
        self.picture.attach(window_hwnd(win), win.winfo_rootx(),
                            win.winfo_rooty(), w, h)

    def schedule_background_sync(self, delay: int = 150) -> None:
        """窗口移动/缩放后延迟重贴（防抖，避免拖动时反复重渲染）。"""
        if self._bg_sync_job is not None:
            try:
                self.root.after_cancel(self._bg_sync_job)
            except Exception:
                pass
            self._bg_sync_job = None
        self._bg_sync_job = self.root.after(delay, self._on_bg_sync)

    def _on_bg_sync(self) -> None:
        self._bg_sync_job = None
        self.refresh_background()

    def _on_root_configure(self, event) -> None:
        """主窗口移动/缩放：先重贴图层窗口，停手后再重新渲染图片。"""
        if not self._bg_enabled.get("main", False):
            return
        self.sync_background()
        self.schedule_background_sync(150)

    def on_window_geometry_changed(self) -> None:
        """窗口位置/大小变了：背景图层立刻跟位（拖动时不卡），停手后重渲染。"""
        if not self._bg_enabled.get(self.mode, False):
            return
        self.sync_background()
        self.schedule_background_sync(200)

    def _bg_tick(self) -> None:
        """动图帧推进：按素材源帧率调度，窗口不可见就暂停。

        ★ 与旧版的关键区别：
          - 旧版定时器 120Hz 常驻空转，且每帧都走 refresh_background()
            → sync_background() → attach()（geometry + SetWindowPos 重排层级），
            120 次/秒的 Win32 重定位正是"所有界面闪烁 + 卡顿"的根源。
          - 新版：定时器只在"背景是动图且窗口可见"时活着；调度间隔按素材
            源帧率算（GIF 12.5fps 就 80ms 一次，不会空转 120 次）；动画帧
            只 repaint 画面，绝不重贴图层窗口。
          - 静止画面（静态图 / 无背景）根本没有定时器，主线程完全空闲。
        """
        self._bg_tick_job = None
        bd = self.backdrops[self.mode]
        if not (self._bg_enabled.get(self.mode, False) and bd.is_video()):
            return
        win = self._target_window()
        try:
            if not win.winfo_ismapped():
                # 目标窗口没显示（托管后台 / 最小化）：暂停动画，低频复查
                self._bg_tick_job = self.root.after(400, self._bg_tick)
                return
        except Exception:
            self._bg_tick_job = self.root.after(400, self._bg_tick)
            return
        try:
            now = time.monotonic()
            if self._bg_next_tick <= 0:
                self._bg_next_tick = now
                self._bg_last_advance = now
            dt = now - self._bg_last_advance
            self._bg_last_advance = now
            fps = min(BG_MAX_FPS, max(1.0, bd.fps or 12.5))
            step = 1.0 / fps
            # 时间累加器补偿定时抖动，8% 容差避免每次差一点而隔帧推进
            self._bg_acc += dt
            if self._bg_acc >= step * 0.92:
                self._bg_acc -= step
                if self._bg_acc < 0:
                    self._bg_acc = 0.0
                if bd.advance():
                    # 只换画面，不重贴窗口 → 无闪烁、主线程占用大幅下降
                    self.refresh_background(reposition=False)
        except Exception:
            pass
        try:
            self._bg_next_tick += step
            delay = self._bg_next_tick - time.monotonic()
            if delay < 0:
                delay = 0.0
                self._bg_next_tick = time.monotonic()
            self._bg_tick_job = self.root.after(
                max(1, int(delay * 1000)), self._bg_tick)
        except Exception:
            pass

    def set_bg_target(self, target: str) -> None:
        """切换背景页正在设置的目标（大界面 / 悬浮窗）。"""
        if target not in ("main", "float"):
            return
        self.bg_target = target
        self.main.refresh_bg_target()
        self.main.refresh_bg_media()

    def choose_bg_media(self) -> None:
        """为当前目标选择本地图片/视频作为背景，立即生效。"""
        target = self.bg_target
        path = filedialog.askopenfilename(
            parent=self.root, title="选择背景视频或图片",
            filetypes=[("视频和图片",
                        "*.mp4 *.webm *.avi *.mov *.mkv *.gif *.png *.jpg *.jpeg *.bmp *.webp"),
                       ("视频和 GIF", "*.mp4 *.webm *.avi *.mov *.mkv *.gif"),
                       ("GIF 动图", "*.gif"),
                       ("图片", "*.png *.jpg *.jpeg *.bmp *.webp"),
                       ("所有文件", "*.*")])
        if not path:
            return
        if not self.backdrops[target].load(path):
            messagebox.showerror(
                "无法使用该文件",
                "未能解码该文件，请换一个常见格式的视频或图片。",
                parent=self.root)
            return
        if not config.set_bg_media(path, target):
            messagebox.showerror("保存失败", "无法写入配置文件。", parent=self.root)
            return
        self.main.refresh_bg_media()
        self.set_background(True, target)

    def set_bg_blur(self, value) -> None:
        """拖动模糊度滑块：立即重渲染背景并记住数值。"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        v = min(8.0, max(0.0, v))
        try:
            self.main.bg_blur_lbl.configure(text=f"{int(round(v))}")
        except Exception:
            pass
        target = self.bg_target
        config.set_bg_blur(v, target)
        self._bg_render_keys[target] = None      # 强制重新渲染
        if target == self.mode:
            self.refresh_background()

    def set_bg_sharpen(self, value) -> None:
        """拖动锐化滑块：立即重渲染背景并记住数值。"""
        try:
            v = float(value) / 10.0
        except (TypeError, ValueError):
            return
        v = min(1.5, max(0.0, v))
        try:
            self.main.bg_sharp_lbl.configure(text=f"{v:.1f}")
        except Exception:
            pass
        target = self.bg_target
        config.set_bg_sharpen(v, target)
        self._bg_render_keys[target] = None      # 强制重新渲染
        if target == self.mode:
            self.refresh_background()

    def clear_cache(self) -> None:
        """清理程序缓存（设置页按钮）。

        只清理可重建的运行时缓存，绝不删除用户设置、背景素材、浏览器
        Local Storage 或程序文件。清理后按钮短暂显示结果，不弹窗。
        """
        count = 0
        try:
            config.clear_cache()
            count += 1
        except Exception:
            pass
        try:
            aula_web_cache.clear_cache()
            count += 1
        except Exception:
            pass
        for bd in self.backdrops.values():
            try:
                bd.clear_cache()
                count += 1
            except Exception:
                pass
            try:
                count += bd.clean_temp_copies()
            except Exception:
                pass
        try:
            self._bg_render_keys = {"main": None, "float": None}
            self._force_refresh()          # 下一轮轮询立即刷新电量显示
            self.refresh_background()      # 背景按新缓存重渲染一帧
        except Exception:
            pass
        btn = getattr(self.main, "clear_cache_btn", None)
        lbl = getattr(self.main, "cache_state_lbl", None)
        if lbl is not None:
            try:
                lbl.configure(text=f"已清理 {count} 项 · 设置与素材未改动")
            except Exception:
                pass
        if btn is not None:
            try:
                btn.configure(text="已清理 ✓")
                if getattr(self, "_cache_btn_job", None):
                    self.root.after_cancel(self._cache_btn_job)
                self._cache_btn_job = self.root.after(
                    2200, lambda: btn.configure(text="清理缓存"))
            except Exception:
                pass

    def clear_bg_media(self) -> None:
        """清除当前目标的背景，恢复纯色界面。"""
        target = self.bg_target
        config.set_bg_media("", target)
        self.main.refresh_bg_media()
        self.backdrops[target].clear()
        self._bg_render_keys[target] = None
        self.set_background(False, target)

    def open_kb_web(self):
        """打开当前所选键盘品牌的官方网页驱动。"""
        try:
            brand = self.main.kb_brand_var.get()
        except Exception:
            brand = KB_BRANDS[0]
        url = KB_BRAND_URLS.get(brand, "")
        if not url:
            return
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def open_mouse_web(self):
        """打开当前所选鼠标品牌的官方网页驱动。"""
        try:
            brand = self.main.mouse_brand_var.get()
        except Exception:
            brand = KB_BRANDS[0]
        url = KB_BRAND_URLS.get(brand, "")
        if not url:
            return
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # 旧接口保留，避免其它调用点失效
    def open_aula_web(self):
        self.open_kb_web()

    def export_diagnostics(self):
        """导出脱敏诊断信息（设备识别 + 缓存字段），生成 txt 供他人分析。

        只写入白名单字段，不包含账号、Cookie、token 等隐私内容。
        """
        try:
            now = time.localtime()
            default_name = "外设诊断_%s.txt" % time.strftime("%Y%m%d_%H%M%S", now)
            init_dir = _downloads_dir()
            path = filedialog.asksaveasfilename(
                title="保存诊断信息",
                initialdir=init_dir,
                initialfile=default_name,
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
            if not path:
                return

            parts = [
                "外设控制中心 诊断信息",
                "程序版本: " + _build_stamp(),
                "生成时间: " + time.strftime("%Y-%m-%d %H:%M:%S", now),
                "系统: Windows",
                "",
                "=" * 46,
                "一、设备识别结果",
                "=" * 46,
            ]
            try:
                full_diag = (self.core.diag_text or "").strip()
            except Exception:
                full_diag = ""
            # 去重的 VID:PID 清单：设备没被识别时，靠这个判断它是什么芯片/型号
            vp: list[str] = []
            for m in re.finditer(
                    r"VID/PID\s+([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4})", full_diag):
                item = m.group(1).upper()
                if item not in vp:
                    vp.append(item)
            # 只保留「已识别设备」之后的内容，避免把几十行无关 HID 接口写进去
            diag = full_diag
            marker = "已识别设备数"
            if marker in diag:
                diag = diag[diag.index(marker):]
            parts.append(diag[:2000] if diag else "（暂无设备识别信息）")
            parts.extend([
                "",
                "HID 设备 VID:PID（去重，共 %d 个）:" % len(vp),
                "  " + ("  ".join(vp) if vp else "无"),
            ])
            # 接口明细：适配新设备时要看 usage page / usage / 接口号 / 报文长度
            detail: list[str] = []
            for line in full_diag.splitlines():
                if "VID/PID" in line and "用法页" in line:
                    detail.append("  " + line.strip())
                if len(detail) >= 40:
                    break
            parts.extend([
                "",
                "HID 接口明细（适配调试用，最多 40 行）:",
            ])
            parts.extend(detail if detail else ["  无"])
            parts.extend([
                "",
                "=" * 46,
                "二、驱动软件与进程",
                "=" * 46,
            ])
            apps = _installed_apps()
            procs = _running_processes()
            parts.append("按品牌检查（下拉列表共 %d 个品牌）:" % len(KB_BRAND_URLS))
            parts.append("  说明：这里只列已安装的桌面客户端；网页驱动见第三部分缓存。")
            parts.extend(_brand_driver_lines(apps, procs))
            others = _other_driver_lines(apps)
            parts.append("")
            parts.append("其他检测到的外设驱动（%d 个）:" % len(others))
            parts.extend(others if others else ["  未发现"])
            brand_procs = [p for p in procs
                           if any(_match_brand(b, p[0]) for b in KB_BRAND_URLS)]
            parts.append("")
            parts.append("品牌驱动进程（%d 个）:" % len(brand_procs))
            parts.extend([f"  {n} (PID {pid})" for n, pid in brand_procs]
                         or ["  未运行"])
            parts.extend([
                "",
                "=" * 46,
                "三、浏览器缓存字段（已脱敏）",
                "=" * 46,
            ])
            try:
                parts.append(aula_web_cache.collect_diagnostics())
            except Exception as exc:
                parts.append("收集缓存字段失败: %r" % (exc,))

            parts.extend([
                "",
                "=" * 46,
                "四、运行时缓存读取结果",
                "=" * 46,
            ])
            try:
                kb = aula_web_cache.read_keyboard_battery(force=True)
                ms = aula_web_cache.read_mouse_battery(force=True)
            except Exception as exc:
                kb = ms = None
                parts.append("读取缓存时出错: %r" % (exc,))
            for label, info in (("键盘", kb), ("鼠标", ms)):
                if info:
                    parts.append(
                        f"  {label}: {info.get('percent')}%  "
                        f"充电={info.get('charging')}  名称={info.get('name')}  "
                        f"来源={info.get('file')}")
                else:
                    parts.append(f"  {label}: 未读到")

            with open(path, "w", encoding="utf-8-sig") as f:
                f.write("\n".join(parts))
            messagebox.showinfo(
                "导出完成",
                "诊断信息已保存到：\n%s\n\n可以直接把这个文件发给对方。" % path)
        except Exception as exc:
            messagebox.showerror("导出失败", "导出诊断信息时出错：\n%r" % (exc,))

    DEV_EMAIL = "3464162519@qq.com"

    def copy_dev_mail(self):
        """把开发者邮箱复制到剪贴板。"""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.DEV_EMAIL)
            self.root.update_idletasks()
            messagebox.showinfo("已复制", "开发者邮箱已复制：\n%s" % self.DEV_EMAIL)
        except Exception as exc:
            messagebox.showerror("复制失败", "复制邮箱时出错：\n%r" % (exc,))

    def on_close(self):
        """关闭按钮 / 关闭窗口时的行为，取决于设置里的「退出方式」。"""
        if config.get_exit_mode() == config.EXIT_TRAY:
            # 托管在后台：按「悬浮窗 / 后台运行时是否显示」决定关窗后
            # 悬浮窗是常驻显示，还是连同大界面一起隐藏只留托盘。
            # 要彻底退出请用托盘右键的「退出程序」，或改成「退出主程序」。
            try:
                if config.get_float_on_bg():
                    self.show_float()
                else:
                    self.hide_all()
            except Exception:
                pass
            return
        self.quit_app()

    def hide_all(self) -> None:
        """隐藏全部窗口，程序继续在后台（托盘）运行。"""
        try:
            self.float.hide()
        except Exception:
            pass
        try:
            self.main.root.withdraw()
        except Exception:
            pass

    def quit_app(self) -> None:
        """彻底退出：移除托盘图标、停止后台线程、销毁窗口、结束进程。

        仅调用 root.destroy() 只能结束 Tk 主循环；打包后仍可能有
        设备读取/托盘相关的 Win32 句柄让进程挂在任务管理器里。
        因此清理完资源后强制结束当前进程，确保「退出主程序」真正干净。
        """
        # 先落盘未保存的透明度（防抖任务还没执行就强退会丢失）
        try:
            self.float.flush_alpha()
        except Exception:
            pass
        # 还原系统计时器精度
        try:
            _set_timer_resolution(False)
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.remove()
        except Exception:
            pass
        try:
            self.core.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        # 强制退出：跳过 Python 解释器清理阶段（配置已在切换时写盘，无待刷数据）
        try:
            os._exit(0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 单实例锁
# ---------------------------------------------------------------------------
def _acquire_single_instance() -> bool:
    """已有实例在运行时返回 False，避免重复启动出现多套窗口。

    用命名互斥量；句柄故意不关闭，进程退出时由系统释放。
    """
    try:
        # 必须用 use_last_error=True 创建 WinDLL，否则 ctypes.get_last_error()
        # 读不到 CreateMutexW 设置的 ERROR_ALREADY_EXISTS，锁会形同虚设。
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        ctypes.set_last_error(0)
        handle = k32.CreateMutexW(None, True, "DeviceFloatPanel_SingleInstance")
        if not handle:
            return True
        return ctypes.get_last_error() != 183   # 183 = ERROR_ALREADY_EXISTS
    except Exception:
        return True


_SHOW_EVENT_NAME = "DeviceFloatPanel_ShowRequest"


def _create_show_event():
    """已运行实例创建'显示窗口'事件（自动重置），供重复启动时通知。"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateEventW.restype = ctypes.c_void_p
        k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                     ctypes.c_bool, ctypes.c_wchar_p]
        return k32.CreateEventW(None, False, False, _SHOW_EVENT_NAME) or None
    except Exception:
        return None


def _signal_show_event():
    """重复启动时：通知已运行实例显示窗口（不弹框）。"""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenEventW.restype = ctypes.c_void_p
        k32.OpenEventW.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_wchar_p]
        h = k32.OpenEventW(0x0002, False, _SHOW_EVENT_NAME)  # EVENT_MODIFY_STATE
        if not h:
            return
        k32.SetEvent.argtypes = [ctypes.c_void_p]
        k32.SetEvent(h)
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle(h)
    except Exception:
        pass


def main() -> None:
    global DPI_SCALE
    if "--check" in sys.argv:
        print(probe())
        return
    if not _acquire_single_instance():
        # 已有实例（可能在后台托管）：通知它显示窗口，本进程静默退出，不弹提示框。
        _signal_show_event()
        return
    DPI_SCALE = setup_dpi()
    # 默认以悬浮窗（置中）启动；--main 可显式打开大界面
    start_mode = "float"
    if "--main" in sys.argv:
        start_mode = "main"
    elif "--bar" in sys.argv or "--mini" in sys.argv:
        start_mode = "float"
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", DPI_SCALE * 96.0 / 72.0)
    except Exception:
        pass
    App(root, start_mode=start_mode)
    root.mainloop()


if __name__ == "__main__":
    main()
