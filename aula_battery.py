"""狼蛛 / AULA 2.4G 无线键盘识别与电量读取（只读，不发送配置命令）。

只处理 2.4G 接收器，不做蓝牙识别：
  - SONiX 2.4G 接收器（05AC:024F / 0C45:FEFE / 0C45:FDFD 等）：20 01 查询
  - BY / Compx 2.4G 接收器（3554:FA09 等）：0x13/0x4A 查询
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time
from ctypes import POINTER, byref, create_string_buffer
from dataclasses import dataclass, field

import hidpp_win as hw


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

GENERIC_READ_WRITE = 0xC0000000
FILE_SHARE_READ_WRITE = 0x00000003
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INVALID_HANDLE_VALUE = hw.INVALID_HANDLE_VALUE


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, POINTER(OVERLAPPED), POINTER(wintypes.DWORD), wintypes.BOOL
]
kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.CancelIo.argtypes = [wintypes.HANDLE]
kernel32.CancelIo.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, POINTER(OVERLAPPED)
]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, POINTER(OVERLAPPED)
]
kernel32.ReadFile.restype = wintypes.BOOL

# --------------------------------------------------------------------------- #
# 同步重叠 I/O（避免 ReadFile 永久阻塞）
# --------------------------------------------------------------------------- #

def _wait(handle: int, ov: OVERLAPPED, timeout_ms: int, writing: bool, size: int) -> bool:
    if not writing and ctypes.get_last_error() not in (0, ERROR_IO_PENDING):
        pass
    wait = kernel32.WaitForSingleObject(ov.hEvent, timeout_ms)
    if wait != WAIT_OBJECT_0:
        kernel32.CancelIo(handle)
        return False
    done = wintypes.DWORD(0)
    if not kernel32.GetOverlappedResult(handle, byref(ov), byref(done), False):
        return False
    return done.value >= size if writing else done.value > 0


def _write_report(handle: int, data: bytes, timeout_ms: int = 500) -> bool:
    ov = OVERLAPPED()
    ov.hEvent = kernel32.CreateEventW(None, True, False, None)
    try:
        kernel32.ResetEvent(ov.hEvent)
        buf = create_string_buffer(data)
        ok = kernel32.WriteFile(handle, buf, len(data), None, byref(ov))
        err = ctypes.get_last_error()
        if not ok and err != ERROR_IO_PENDING:
            return False
        return _wait(handle, ov, timeout_ms, True, len(data))
    finally:
        if ov.hEvent:
            kernel32.CloseHandle(ov.hEvent)


def _read_report(handle: int, size: int, timeout_ms: int = 500) -> bytes:
    ov = OVERLAPPED()
    ov.hEvent = kernel32.CreateEventW(None, True, False, None)
    try:
        kernel32.ResetEvent(ov.hEvent)
        buf = create_string_buffer(size)
        ok = kernel32.ReadFile(handle, buf, size, None, byref(ov))
        err = ctypes.get_last_error()
        if not ok and err != ERROR_IO_PENDING:
            return b""
        if not _wait(handle, ov, timeout_ms, False, size):
            return b""
        return buf.raw
    finally:
        if ov.hEvent:
            kernel32.CloseHandle(ov.hEvent)


def _product_string(handle: int) -> str:
    buf = create_string_buffer(256)
    try:
        if hw.hid.HidD_GetProductString(handle, buf, 256):
            return buf.value.decode("utf-16-le", "ignore").strip()
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------- #
# 设备定义
# --------------------------------------------------------------------------- #

# SONiX 2.4G 接收器：(VID, PID, 接口号；-1 表示任意接口)
SONIX_DONGLES = {
    (0x05AC, 0x024F): 3,   # AULA F75 Max / F108 Pro / F2087 Pro 等
    (0x0C45, 0xFEFE): 3,
    (0x0C45, 0xFDFD): 3,
    (0x342D, 0xE479): -1,
    (0x342D, 0xE485): -1,
    (0x342D, 0xE4BC): -1,
    (0x342D, 0xE4C2): -1,
    (0x342D, 0xE407): -1,
    (0x388D, 0x0003): -1,
    (0x0DB0, 0x0CA0): -1,
    (0x36B0, 0x30BD): -1,
    (0x342D, 0xE51A): -1,
    (0x342D, 0xE487): -1,
    (0x342D, 0xE537): -1,
}

BY_RECEIVERS = {(0x3554, 0xFA09), (0x3554, 0xFB40), (0x41E4, 0x2001), (0x05AC, 0x024F)}


@dataclass
class AulaHid:
    handle: int
    path: str
    vid: int
    pid: int
    usage_page: int
    usage: int
    iface: int
    protocol: str
    name: str = ""
    input_len: int = 0
    output_len: int = 0

    def close(self) -> None:
        if self.handle and self.handle != INVALID_HANDLE_VALUE:
            try:
                kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = 0


@dataclass
class AulaKeyboard:
    transport: AulaHid | None
    name: str
    protocol: str
    kind: str = "keyboard"
    vid: int = 0
    pid: int = 0

    def close(self) -> None:
        if self.transport:
            self.transport.close()

    def read_battery(self) -> tuple[int, str]:
        if not self.transport or not self.transport.handle:
            return -1, "设备未连接"
        if self.protocol == "sonix":
            return self._read_sonix()
        if self.protocol == "by":
            return self._read_by()
        return -1, "不支持"

    def _read_sonix(self) -> tuple[int, str]:
        h = self.transport.handle
        # 关键：SONiX 0C45:FEFE 的 output 报告写入要「纯 outlen 字节、不带 00 report ID
        # 前缀」，带 00 头会 WriteFile 失败（实测）。但即便写成功，20 xx 全系列查询
        # 也只回命令回显、无电量应答（遍历 20 01..40 / 21 01..20 均验证），键盘静置
        # 完全静默不主动上报，系统也未注册为电池设备 —— 即 2.4G 下此方案不向 PC 报电量。
        # 这里保留「正确写法」发一次查询，万一固件更新后开始应答即可直接生效。
        payload = bytearray(self.transport.output_len or 33)
        payload[0] = 0x20
        payload[1] = 0x01
        payload[31] = sum(payload[:31]) & 0xFF
        if not _write_report(h, bytes(payload), 500):
            return -1, "查询写入失败"
        for _ in range(20):
            raw = _read_report(h, (self.transport.output_len or 33), 50)
            if len(raw) < 4:
                continue
            p = raw[1:] if raw[0] == 0x00 else raw
            if p[0] == 0x20 and p[1] == 0x01 and p[2] == 0x00:
                level = p[3]
                if 1 <= level <= 100:
                    return min(level, 100), "放电中"
        return -1, "2.4G 不回传电量 · 按 FN+B 看灯"

    def _read_by(self) -> tuple[int, str]:
        h = self.transport.handle
        # 狼蛛 F87 等 Compx 接收器需要先发一个只读握手帧才回电量。
        handshake = bytearray(20)
        handshake[0] = 0x13
        handshake[1] = 0x05
        handshake[2] = 0x01
        handshake[19] = sum(handshake[:19]) & 0xFF
        _write_report(h, bytes(handshake))
        time.sleep(0.05)

        start = time.monotonic()
        sent = 0
        while time.monotonic() - start < 4.0:
            if sent == 0 or time.monotonic() - start >= sent * 1.3:
                req = bytearray(20)
                req[0] = 0x13
                req[1] = 0x4A
                req[2] = 1 if sent == 0 else sent & 1
                req[19] = sum(req[:19]) & 0xFF
                _write_report(h, bytes(req))
                sent += 1
            raw = _read_report(h, 20, 120)
            if len(raw) < 20 or raw[0] != 0x13:
                continue
            cmd = raw[1] & 0x7F
            if cmd not in (0x4A, 0x0A):
                continue
            if all(b == 0 for b in raw[4:19]):
                continue
            checksum_ok = raw[19] == (sum(raw[:19]) & 0xFF)
            mchose = raw[19] == 0 and raw[2] == 1 and raw[3] == 0
            if not checksum_ok and not mchose:
                continue
            level = raw[5]
            state = raw[6]
            status = "充电中" if state & 0x10 else "放电中"
            if 1 <= level <= 100:
                return level, status
        return -1, "无应答（按一下键盘唤醒后重试）"


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #

def _is_candidate(info: hw.HidHandle) -> str | None:
    key = (info.vid, info.pid)
    if info.usage_page == 0xFF60 and info.usage == 0x0061 and info.input_len == 33 and info.output_len == 33:
        if key in SONIX_DONGLES and SONIX_DONGLES[key] in (-1, info.iface):
            return "sonix"
    if info.usage_page in (0xFF02, 0xFF04) and info.usage == 0x0002 and info.input_len >= 20 and info.output_len >= 20:
        if key in BY_RECEIVERS:
            if key == (0x05AC, 0x024F) and info.iface != 1:
                return None
            return "by"
    return None


def scan_aula() -> tuple[list[AulaKeyboard], list[str]]:
    """扫描 AULA 键盘；返回 (设备列表, 说明)。"""
    notes: list[str] = []
    keyboards: list[AulaKeyboard] = []
    infos = hw.enumerate_logitech_hid(vid_filter=None)
    try:
        for info in infos:
            protocol = _is_candidate(info)
            if not protocol:
                continue
            path, vid, pid = info.path, info.vid, info.pid
            page, usage, iface = info.usage_page, info.usage, info.iface
            info.close()
            info.handle = 0
            handle = kernel32.CreateFileW(
                path, GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None,
                OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None,
            )
            if handle in (0, INVALID_HANDLE_VALUE):
                notes.append(f"{vid:04X}:{pid:04X} 无法打开 HID 接口")
                continue
            # 型号按用户确认为狼蛛 Mini60 Pro；0C45:FEFE 属 SONiX 通用方案，多品牌共用此 ID
            name = "狼蛛 Mini60 Pro" if (vid, pid) == (0x0C45, 0xFEFE) else (
                _product_string(handle) or f"AULA {vid:04X}:{pid:04X}"
            )
            keyboards.append(
                AulaKeyboard(
                    transport=AulaHid(handle, path, vid, pid, page, usage, iface, protocol, name,
                                      input_len=info.input_len, output_len=info.output_len),
                    name=name,
                    protocol=protocol,
                    vid=vid,
                    pid=pid,
                )
            )
    finally:
        for info in infos:
            info.close()

    if keyboards:
        for kb in keyboards:
            iface = kb.transport.iface if kb.transport else "?"
            notes.append(
                f"2.4G 识别：{kb.name} [{kb.protocol}] 接收器 {kb.vid:04X}:{kb.pid:04X} "
                f"IF{iface} · 当前 2.4G 模式"
            )
    else:
        notes.append("未检测到 2.4G 键盘接收器，请确认接收器已插入 USB 口。")

    # 2.4G 模式电量说明（不做蓝牙识别）
    notes.append(
        "键盘电量：实测 0C45:FEFE 的 FF60/FF59 两个通道均不回传电量（多种只读查询无应答、无主动上报），"
        "系统也未把键盘注册为电池设备；Mini60 Pro 在 2.4G 模式下无法在电脑上显示电量，"
        "请用 FN+B 看键盘指示灯。"
    )
    return keyboards, notes


if __name__ == "__main__":
    devices, msgs = scan_aula()
    print("\n".join(msgs))
    for d in devices:
        try:
            print(f"{d.name}: {d.read_battery()}")
        finally:
            d.close()
