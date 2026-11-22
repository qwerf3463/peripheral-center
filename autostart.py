"""开机自启动管理：当前用户注册表 Run 键 + pythonw 无黑框启动。

用法：
    D:/python/python.exe autostart.py status    查看状态
    D:/python/python.exe autostart.py enable    启用开机自启
    D:/python/python.exe autostart.py disable   关闭开机自启

说明：
  - 只写 HKCU（当前用户），不需要管理员权限，也不会影响其他用户。
  - 用 pythonw.exe 启动，登录时不会弹出黑色命令行窗口。
  - app.py 内部资源路径基于 __file__，因此从任意工作目录启动都正常。
"""

from __future__ import annotations

import os
import sys

import paths

APP_NAME = "DeviceFloatPanel"
RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

try:
    import winreg
except ImportError:  # 非 Windows 环境，仅便于语法检查
    winreg = None  # type: ignore


def app_path() -> str:
    """返回启动目标（打包后是 exe 本身，源码运行时是 app.py）。"""
    if paths.is_frozen():
        return os.path.abspath(sys.executable)
    return os.path.join(paths.app_dir(), "app.py")


def bundled_exe() -> str | None:
    """若同目录下存在打包好的 exe，优先用它（无黑框、不依赖 Python）。"""
    exe = os.path.join(paths.app_dir(), "dist", "外设控制中心.exe")
    return exe if os.path.isfile(exe) else None


def pythonw_path() -> str | None:
    """找一个可用的 pythonw.exe（优先当前解释器同目录）。"""
    if paths.is_frozen():
        return None            # 打包后直接启动 exe，不需要 pythonw
    candidates = []
    base = os.path.dirname(sys.executable or "")
    if base:
        candidates.append(os.path.join(base, "pythonw.exe"))
    candidates.extend(
        [
            r"D:\python\pythonw.exe",
            r"C:\Python313\pythonw.exe",
            r"C:\Python312\pythonw.exe",
        ]
    )
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def launch_command() -> str | None:
    """生成注册表里存放的启动命令。"""
    if paths.is_frozen():
        return f'"{app_path()}"'
    exe = bundled_exe()
    if exe:
        return f'"{exe}"'
    pyw = pythonw_path()
    if not pyw:
        return None
    return f'"{pyw}" "{app_path()}"'


def status() -> tuple[bool, str]:
    """返回 (是否已启用, 注册表当前值)。"""
    if winreg is None:
        return False, ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return True, str(value)
    except FileNotFoundError:
        return False, ""
    except OSError:
        return False, ""


def enable() -> tuple[bool, str]:
    """启用开机自启，返回 (成功, 说明)。"""
    if winreg is None:
        return False, "当前系统不支持 Windows 注册表。"
    command = launch_command()
    if not command:
        return False, "找不到可用的启动程序，无法配置开机自启。"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_PATH) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        return True, command
    except OSError as exc:
        return False, f"写入注册表失败：{exc}"


def disable() -> tuple[bool, str]:
    """关闭开机自启，返回 (成功, 说明)。"""
    if winreg is None:
        return False, "当前系统不支持 Windows 注册表。"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        return True, "已关闭开机自启。"
    except FileNotFoundError:
        return True, "本来就没有开启开机自启。"
    except OSError as exc:
        return False, f"删除注册表项失败：{exc}"


def _main() -> int:
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if action == "enable":
        ok, msg = enable()
        print(("已启用开机自启\n" if ok else "启用失败\n") + msg)
        return 0 if ok else 1
    if action == "disable":
        ok, msg = disable()
        print((msg if ok else "关闭失败\n" + msg))
        return 0 if ok else 1
    if action == "status":
        on, value = status()
        print(f"开机自启：{'已开启' if on else '未开启'}")
        if on:
            print(f"启动命令：{value}")
        return 0
    print("用法：autostart.py [status|enable|disable]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
