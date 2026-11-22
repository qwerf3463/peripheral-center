"""把电池图标导出成 .ico 文件，供快捷方式和窗口图标使用。

用法：
    D:/python/python.exe export_icon.py          # 生成 app.ico
"""
from __future__ import annotations

import os
import struct

import app as m


def export_ico(path: str, sizes=(16, 32, 48)) -> str:
    """生成多尺寸 .ico 文件。"""
    import ctypes
    import ctypes.wintypes as wt

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

    images = []          # [(size, bgra_bytes)]
    for size in sizes:
        hdc = u.GetDC(0)
        memdc = g.CreateCompatibleDC(hdc)
        bmi = _BIH()
        bmi.biSize = ctypes.sizeof(_BIH)
        bmi.biWidth = size
        bmi.biHeight = -size
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bits = ctypes.c_void_p()
        color = g.CreateDIBSection(memdc, ctypes.byref(bmi), 0,
                                   ctypes.byref(bits), None, 0)
        if not color or not bits:
            if color:
                g.DeleteObject(color)
            g.DeleteDC(memdc)
            u.ReleaseDC(0, hdc)
            continue

        buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
        for i in range(size * size * 4):
            buf[i] = 0

        def put(x, y, r, gg, b, a=255):
            if 0 <= x < size and 0 <= y < size:
                i = (y * size + x) * 4
                buf[i] = b
                buf[i + 1] = gg
                buf[i + 2] = r
                buf[i + 3] = a

        # 按比例缩放 32x32 的原始构图
        k = size / 32.0
        x0, y0, x1, y1 = 3, 9, 26, 23
        sx0, sy0 = int(x0 * k), int(y0 * k)
        sx1, sy1 = int(x1 * k), int(y1 * k)
        for x in range(sx0, sx1 + 1):
            for y in range(sy0, sy1 + 1):
                edge = (x in (sx0, sx1) or y in (sy0, sy1))
                if edge:
                    put(x, y, 210, 214, 219)
                else:
                    put(x, y, 34, 197, 94)
        for x in range(sx1 + 1, sx1 + max(1, int(3 * k))):
            for y in range(sy0 + int(4 * k), sy1 - int(3 * k) + 1):
                put(x, y, 210, 214, 219)

        raw = bytes(buf)
        g.DeleteObject(color)
        g.DeleteDC(memdc)
        u.ReleaseDC(0, hdc)
        images.append((size, raw))

    if not images:
        raise RuntimeError("无法创建 DIB 缓冲区")

    # ICO 容器：ICONDIR + 每个尺寸的 ICONDIRENTRY + 图像数据(BMP: 头+BGRA+AND掩码)
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    payloads = b""
    offset = 6 + 16 * len(images)
    for size, raw in images:
        # ICO 里的 BMP 图像必须自下而上存储（与 DIB 约定相反）
        flipped = bytearray()
        for row in range(size - 1, -1, -1):
            flipped.extend(raw[row * size * 4:(row + 1) * size * 4])
        raw = bytes(flipped)

        # 字段顺序：biSize biWidth biHeight biPlanes biBitCount biCompression
        #           biSizeImage biXPelsPerMeter biYPelsPerMeter biClrUsed biClrImportant
        bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                          len(raw), 0, 0, 0, 0)

        # AND 掩码：1 = 透明。必须按 alpha 生成，否则 Windows 会忽略 alpha 通道，
        # 把本该透明的背景当成不透明的黑色，图标变成一个黑方块。
        mask_stride = ((size + 31) // 32) * 4   # 1bpp 每行按 32 位对齐
        and_mask = bytearray(mask_stride * size)
        for y in range(size):
            for x in range(size):
                i = (y * size + x) * 4
                if raw[i + 3] == 0:              # 透明像素 → 掩码置 1
                    byte_index = (size - 1 - y) * mask_stride + (x // 8)
                    and_mask[byte_index] |= 0x80 >> (x % 8)
        image = bih + raw + bytes(and_mask)
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                               1, 32, len(image), offset)
        payloads += image
        offset += len(image)

    with open(path, "wb") as f:
        f.write(header + entries + payloads)
    return path


if __name__ == "__main__":
    m.DPI_SCALE = m.setup_dpi()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
    export_ico(out)
    print("已生成:", out, os.path.getsize(out), "bytes")
