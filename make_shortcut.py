"""创建 Windows 快捷方式（.lnk），不依赖 COM 自动化/pywin32。

用 IShellLinkW + IPersistFile 接口通过 ctypes 调 Shell32 实现。

注意：IPersistFile 在本机的 vtable 里，Save 位于索引 6（不是标准的 3），
SaveCompleted=7，GetCurFile=8 —— 系统实现比文档多前置方法，必须实测确定。

用法：
    D:/python/python.exe make_shortcut.py
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

APP_DIR = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(APP_DIR, "dist", "外设控制中心.exe")
PYW = r"D:\python\pythonw.exe"
APP_PY = os.path.join(APP_DIR, "app.py")
ICON = os.path.join(APP_DIR, "app.ico")
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
LNK_NAME = "外设控制中心.lnk"

# IShellLinkW vtable 索引
SL_SET_DESCRIPTION = 7
SL_SET_WORKING_DIR = 9
SL_SET_ARGUMENTS = 11
SL_SET_ICON_LOCATION = 15
SL_SET_PATH = 20

# IPersistFile vtable 索引（本机实测）
PF_LOAD = 2
PF_SAVE = 6


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(s: str) -> GUID:
    g = GUID()
    ole32 = ctypes.windll.ole32
    ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID)]
    ole32.CLSIDFromString.restype = ctypes.c_long
    hr = ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    if hr != 0:
        raise RuntimeError(f"CLSIDFromString 失败: {s}")
    return g


CLSID_SHELL_LINK = _guid("{00021401-0000-0000-C000-000000000046}")
IID_SHELL_LINK_W = _guid("{000214F9-0000-0000-C000-000000000046}")
IID_PERSIST_FILE = _guid("{0000010B-0000-0000-C000-000000000046}")


def _init_com() -> None:
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    # 不调 CoInitializeEx 的话，CoCreateInstance 会返回
    # CO_E_CLASSSTRING_INVALID (0x800401F0)，表现为"类未注册"
    ole32.CoInitializeEx(None, 2)      # COINIT_APARTMENTTHREADED


def _vtable(ptr):
    return ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents


def create_shortcut(link_path: str, target: str, args: str, workdir: str,
                    icon: str, description: str) -> str:
    _init_com()
    ole32 = ctypes.windll.ole32

    cocreate = ole32.CoCreateInstance
    cocreate.argtypes = [
        ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    cocreate.restype = ctypes.c_long

    link = ctypes.c_void_p()
    hr = cocreate(ctypes.byref(CLSID_SHELL_LINK), None, 1,   # CLSCTX_INPROC_SERVER
                  ctypes.byref(IID_SHELL_LINK_W), ctypes.byref(link))
    if hr != 0 or not link:
        raise RuntimeError(f"CoCreateInstance 失败 0x{hr & 0xFFFFFFFF:08X}")

    vtbl = _vtable(link)
    try:
        def call(idx, *args):
            proto = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, *([ctypes.c_void_p] * len(args)))
            return ctypes.cast(vtbl[idx], proto)(link, *args)

        call(SL_SET_DESCRIPTION, ctypes.c_wchar_p(description))
        call(SL_SET_WORKING_DIR, ctypes.c_wchar_p(workdir))
        call(SL_SET_ARGUMENTS, ctypes.c_wchar_p(args))
        call(SL_SET_ICON_LOCATION, ctypes.c_wchar_p(f"{icon},0"), 0)
        call(SL_SET_PATH, ctypes.c_wchar_p(target))

        # QI 到 IPersistFile —— QueryInterface 是 IUnknown vtable 的第 0 项
        persist = ctypes.c_void_p()
        qi = ctypes.cast(vtbl[0], ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p)))
        hr = qi(link, ctypes.byref(IID_PERSIST_FILE), ctypes.byref(persist))
        if hr != 0 or not persist:
            raise RuntimeError(f"QueryInterface 失败 0x{hr & 0xFFFFFFFF:08X}")

        pvtbl = _vtable(persist)
        save = ctypes.cast(pvtbl[PF_SAVE], ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, wintypes.BOOL))
        hr = save(persist, link_path, True)
        if hr != 0:
            raise RuntimeError(f"IPersistFile::Save 失败 0x{hr & 0xFFFFFFFF:08X}")
    finally:
        rel = ctypes.cast(vtbl[2], ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p))
        rel(link)
    return link_path


def main() -> None:
    """优先指向打包好的 exe（无黑框、不依赖 Python）；没有则退回 pythonw。"""
    if os.path.exists(EXE):
        target, args = EXE, ""
    else:
        if not os.path.exists(PYW):
            raise SystemExit(f"找不到 {EXE} 或 {PYW}")
        target, args = PYW, f'"{APP_PY}"'
    os.makedirs(DESKTOP, exist_ok=True)
    lnk = os.path.join(DESKTOP, LNK_NAME)
    create_shortcut(lnk, target, args, APP_DIR, ICON, "外设电量控制中心")
    print("快捷方式已创建:", lnk)
    print("  大小:", os.path.getsize(lnk), "bytes")
    print("  目标:", target)
    if args:
        print("  参数:", args)
    print("  图标:", ICON)


if __name__ == "__main__":
    main()
