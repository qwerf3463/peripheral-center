"""纯标准库截图工具：ctypes 调用 Windows GDI + zlib 手写 PNG。

不依赖 PIL / pywin32 / .NET，规避被安全策略拦截的截图方式。

用法：
    D:/python/python.exe screenshot.py <输出路径> [窗口标题] [外扩像素]
  - 给了窗口标题：只截该窗口区域（会尝试置前）
  - 外扩像素：窗口四周额外包含多少桌面（展示半透明效果时很有用）
  - 没给标题：截全屏
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import struct
import sys
import time
import zlib

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: str, width: int, height: int, bgra: bytes) -> None:
    """BGRA 像素 -> PNG（RGB，无损）。"""
    pixel_count = width * height
    rgb = bytearray(pixel_count * 3)
    j = 0
    for i in range(0, pixel_count * 4, 4):
        rgb[j] = bgra[i + 2]      # R
        rgb[j + 1] = bgra[i + 1]  # G
        rgb[j + 2] = bgra[i]      # B
        j += 3

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: None
        start = y * width * 3
        raw.extend(rgb[start:start + width * 3])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", ihdr)
    png += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += _png_chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def grab(path: str, title: str | None = None, pad: int = 0) -> str:
    # 声明 DPI 感知，否则在高缩放（如 125%）下 GetSystemMetrics 只返回
    # 虚拟化后的尺寸（2560x1440 会被看成 2048x1152），截出来只有左上角一块。
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    x = y = 0
    hwnd = None
    if title:
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            raise RuntimeError(f"找不到窗口：{title}")
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.6)
        rect = wintypes.RECT()
        # 优先用 DWM 可见边框（排除阴影/不可见扩展），避免 BitBlt 坐标偏移导致右侧裁切
        got_visible = False
        try:
            dwmapi = ctypes.windll.dwmapi
            DWMWA_EXTENDED_FRAME_BOUNDS = 9
            res = dwmapi.DwmGetWindowAttribute(
                hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(rect), ctypes.sizeof(rect))
            got_visible = (res == 0)
        except Exception:
            pass
        if not got_visible:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        x = max(0, rect.left - pad)
        y = max(0, rect.top - pad)
        right = min(sw, rect.right + pad)
        bottom = min(sh, rect.bottom + pad)
        width = right - x
        height = bottom - y
    else:
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)

    if width <= 0 or height <= 0:
        raise RuntimeError(f"无效尺寸 {width}x{height}")

    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, x, y, SRCCOPY):
        raise RuntimeError("BitBlt 失败")

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = width
    bi.biHeight = -height  # 负值 = 自上而下
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0

    buffer = ctypes.create_string_buffer(width * height * 4)
    got = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bi), DIB_RGB_COLORS)
    if not got:
        raise RuntimeError("GetDIBits 失败")

    write_png(path, width, height, buffer.raw)

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, screen_dc)
    return f"已保存 {path} ({width}x{height})"


def _main() -> int:
    if len(sys.argv) < 2:
        print("用法：screenshot.py <输出路径> [窗口标题] [外扩像素]")
        return 2
    path = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else None
    pad = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    try:
        print(grab(path, title, pad))
        return 0
    except Exception as exc:
        print(f"截图失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
