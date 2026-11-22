"""Windows 下通过 HID++ 协议访问罗技鼠标 / 键盘。

纯标准库实现（ctypes），不依赖 hidapi / pywin32。
支持：
  - Unifying / Lightspeed / Bolt 接收器下挂的无线设备（槽位 1-7）
  - 直连 USB 的 G 系列设备（索引 0xFF / 0x00）
  - 电量读取（0x1004 / 0x1000 / 0x1001，以及 HID++ 1.0 的 0x07/0x80）
  - DPI 读取 / 设置（0x2201 ADJUSTABLE_DPI，兼容 0x2202）
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import re
import time
from ctypes import POINTER, byref, create_string_buffer, wstring_at
from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------- #
# Win32 基础定义
# --------------------------------------------------------------------------- #

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
ERROR_NO_MORE_ITEMS = 259
GENERIC_READ_WRITE = 0xC0000000
FILE_SHARE_READ_WRITE = 0x00000003
OPEN_EXISTING = 3


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.ULONG),
        ("VendorID", wintypes.USHORT),
        ("ProductID", wintypes.USHORT),
        ("VersionNumber", wintypes.USHORT),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("DevicePath", wintypes.WCHAR * 1),
    ]


class HIDP_CAPS(ctypes.Structure):
    # 只需要前面几项，后面保留足够空间即可
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


hid.HidD_GetHidGuid.argtypes = [POINTER(GUID)]
hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetAttributes.restype = wintypes.BOOL
hid.HidD_GetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_GetFeature.restype = wintypes.BOOL
hid.HidD_SetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_SetFeature.restype = wintypes.BOOL
hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, POINTER(ctypes.c_void_p)]
hid.HidD_GetPreparsedData.restype = wintypes.BOOL
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, POINTER(HIDP_CAPS)]
hid.HidP_GetCaps.restype = wintypes.USHORT
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidD_FreePreparsedData.restype = wintypes.BOOL

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

setupapi.SetupDiGetClassDevsW.argtypes = [
    POINTER(GUID),
    wintypes.LPCWSTR,
    wintypes.HWND,
    wintypes.DWORD,
]
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    POINTER(GUID),
    wintypes.DWORD,
    POINTER(SP_DEVICE_INTERFACE_DATA),
]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE,
    POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.c_void_p,
    wintypes.DWORD,
    POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL


def _hid_guid() -> GUID:
    g = GUID()
    hid.HidD_GetHidGuid(byref(g))
    return g


@dataclass
class HidHandle:
    handle: int
    path: str
    vid: int
    pid: int
    usage_page: int = 0
    usage: int = 0
    feature_len: int = 0
    input_len: int = 0
    output_len: int = 0
    iface: int = -1

    def close(self) -> None:
        if self.handle and self.handle != INVALID_HANDLE_VALUE:
            try:
                kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = 0


def _caps_of(handle: int) -> tuple[int, int, int, int, int]:
    preparsed = ctypes.c_void_p()
    if not hid.HidD_GetPreparsedData(handle, byref(preparsed)):
        return 0, 0, 0, 0, 0
    try:
        caps = HIDP_CAPS()
        if hid.HidP_GetCaps(preparsed, byref(caps)) == 0:
            return (
                caps.UsagePage,
                caps.Usage,
                caps.FeatureReportByteLength,
                caps.InputReportByteLength,
                caps.OutputReportByteLength,
            )
        return 0, 0, 0, 0, 0
    finally:
        hid.HidD_FreePreparsedData(preparsed)


def enumerate_logitech_hid(vid_filter: int | None = 0x046D) -> list[HidHandle]:
    """列出罗技 HID 接口；vid_filter=None 时列出所有 HID 设备（用于诊断）。"""
    guid = _hid_guid()
    hdev = setupapi.SetupDiGetClassDevsW(
        byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if hdev in (0, INVALID_HANDLE_VALUE):
        return []

    results: list[HidHandle] = []
    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(iface)
            ok = setupapi.SetupDiEnumDeviceInterfaces(
                hdev, None, byref(guid), index, byref(iface)
            )
            if not ok:
                if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                    break
                index += 1
                continue

            buf = create_string_buffer(2048)
            detail = ctypes.cast(buf, POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W)).contents
            detail.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                hdev, byref(iface), byref(detail), 2048, None, None
            ):
                index += 1
                continue

            path = wstring_at(ctypes.addressof(detail) + 4)
            handle = kernel32.CreateFileW(
                path,
                GENERIC_READ_WRITE,
                FILE_SHARE_READ_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            if handle not in (0, INVALID_HANDLE_VALUE):
                attrs = HIDD_ATTRIBUTES()
                attrs.Size = ctypes.sizeof(attrs)
                if hid.HidD_GetAttributes(handle, byref(attrs)) and (
                    vid_filter is None or attrs.VendorID == vid_filter
                ):
                    upage, usage, flen, ilen, olen = _caps_of(handle)
                    match = re.search(r"MI_(\d{2})", path, re.IGNORECASE)
                    results.append(
                        HidHandle(
                            handle=handle,
                            path=path,
                            vid=attrs.VendorID,
                            pid=attrs.ProductID,
                            usage_page=upage,
                            usage=usage,
                            feature_len=flen,
                            input_len=ilen,
                            output_len=olen,
                            iface=int(match.group(1)) if match else -1,
                        )
                    )
                else:
                    kernel32.CloseHandle(handle)
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return results


# --------------------------------------------------------------------------- #
# HID++ 报文
# --------------------------------------------------------------------------- #

SW_ID = 0x0A
RPT_SHORT = 0x10
RPT_LONG = 0x11

FEATURE_ROOT = 0x0000
FEATURE_DEVICE_NAME = 0x0005
FEATURE_BATTERY_STATUS = 0x1000
FEATURE_BATTERY_VOLTAGE = 0x1001
FEATURE_UNIFIED_BATTERY = 0x1004
FEATURE_ADJUSTABLE_DPI = 0x2201
FEATURE_EXT_ADJUSTABLE_DPI = 0x2202
FEATURE_ONBOARD_PROFILES = 0x8100

FEATURES_TO_QUERY = (
    FEATURE_DEVICE_NAME,
    FEATURE_UNIFIED_BATTERY,
    FEATURE_BATTERY_STATUS,
    FEATURE_BATTERY_VOLTAGE,
    FEATURE_ADJUSTABLE_DPI,
    FEATURE_EXT_ADJUSTABLE_DPI,
    FEATURE_ONBOARD_PROFILES,
)


def build_report(
    device_index: int,
    feature_index: int,
    function: int,
    params: Iterable[int] = (),
    long_report: bool = False,
) -> bytes:
    param_list = list(params)
    if long_report:
        param_list = (param_list + [0] * 16)[:16]
        return bytes([RPT_LONG, device_index, feature_index, ((function & 0x0F) << 4) | SW_ID]) + bytes(param_list)
    param_list = (param_list + [0] * 3)[:3]
    return bytes([RPT_SHORT, device_index, feature_index, ((function & 0x0F) << 4) | SW_ID]) + bytes(param_list)


@dataclass
class Reply:
    report_id: int
    device_index: int
    feature_index: int
    function: int
    sw_id: int
    params: list[int]

    @property
    def is_error(self) -> bool:
        return self.feature_index in (0x8F, 0xFF)

    def u16(self, offset: int) -> int:
        if len(self.params) < offset + 2:
            return 0
        return (self.params[offset] << 8) | self.params[offset + 1]


class HidppTransport:
    """负责向所有 Logitech HID 节点发送 feature report 并读取回复。"""

    def __init__(self, handles: list[HidHandle]):
        self.handles = handles
        self.lock = __import__("threading").RLock()

    def close(self) -> None:
        for h in self.handles:
            h.close()
        self.handles = []

    def _drain(self) -> None:
        for h in self.handles:
            buf = create_string_buffer(20)
            try:
                hid.HidD_GetFeature(h.handle, buf, 20)
            except Exception:
                pass

    def _write(self, report: bytes) -> bool:
        ok_any = False
        buf = create_string_buffer(report)
        for h in self.handles:
            try:
                if hid.HidD_SetFeature(h.handle, buf, len(report)):
                    ok_any = True
            except Exception:
                continue
        return ok_any

    def _read(
        self,
        device_index: int,
        feature_index: int,
        timeout: float,
    ) -> Reply | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for h in self.handles:
                buf = create_string_buffer(20)
                try:
                    got = hid.HidD_GetFeature(h.handle, buf, 20)
                except Exception:
                    continue
                if not got:
                    continue
                raw = bytes(buf.raw)
                if raw[0] not in (RPT_SHORT, RPT_LONG):
                    continue
                if raw[1] != device_index:
                    continue
                if raw[2] != feature_index and raw[2] not in (0x8F, 0xFF):
                    continue
                return Reply(
                    report_id=raw[0],
                    device_index=raw[1],
                    feature_index=raw[2],
                    function=(raw[3] >> 4) & 0x0F,
                    sw_id=raw[3] & 0x0F,
                    params=list(raw[4:]),
                )
            time.sleep(0.004)
        return None

    def request(
        self,
        device_index: int,
        feature_index: int,
        function: int,
        params: Iterable[int] = (),
        long_report: bool = False,
        timeout: float = 0.35,
    ) -> Reply | None:
        report = build_report(device_index, feature_index, function, params, long_report)
        with self.lock:
            self._drain()
            if not self._write(report):
                return None
            return self._read(device_index, feature_index, timeout)

    def raw_request(
        self,
        device_index: int,
        class_byte: int,
        address_byte: int,
        params: Iterable[int] = (),
        timeout: float = 0.35,
    ) -> Reply | None:
        """HID++ 1.0 报文：byte2=class, byte3=address，没有 function nibble。"""
        param_list = (list(params) + [0] * 3)[:3]
        report = bytes([RPT_SHORT, device_index, class_byte, address_byte]) + bytes(param_list)
        deadline = time.monotonic() + timeout
        with self.lock:
            self._drain()
            if not self._write(report):
                return None
            while time.monotonic() < deadline:
                for h in self.handles:
                    buf = create_string_buffer(20)
                    try:
                        got = hid.HidD_GetFeature(h.handle, buf, 20)
                    except Exception:
                        continue
                    if not got:
                        continue
                    raw = bytes(buf.raw)
                    if raw[0] != RPT_SHORT or raw[1] != device_index:
                        continue
                    return Reply(
                        report_id=raw[0],
                        device_index=raw[1],
                        feature_index=raw[2],
                        function=(raw[3] >> 4) & 0x0F,
                        sw_id=raw[3] & 0x0F,
                        params=list(raw[4:]),
                    )
                time.sleep(0.004)
        return None


# --------------------------------------------------------------------------- #
# 设备抽象
# --------------------------------------------------------------------------- #

# Solaar 的 13 点放电曲线（近似值）
_VOLTAGE_CURVE = [
    (3060, 0),
    (3164, 10),
    (3266, 20),
    (3358, 30),
    (3451, 40),
    (3568, 50),
    (3685, 60),
    (3794, 70),
    (3905, 80),
    (4024, 90),
    (4186, 100),
]


def voltage_to_percent(mv: int) -> int:
    if mv <= 0:
        return -1
    for i in range(1, len(_VOLTAGE_CURVE)):
        v1, p1 = _VOLTAGE_CURVE[i - 1]
        v2, p2 = _VOLTAGE_CURVE[i]
        if mv <= v2:
            if v2 == v1:
                return p2
            return int(round(p1 + (mv - v1) * (p2 - p1) / (v2 - v1)))
    return 100


@dataclass
class LogitechDevice:
    transport: HidppTransport
    index: int
    name: str = "未知罗技设备"
    features: dict[int, int] = field(default_factory=dict)
    kind: str = "other"
    is_hidpp2: bool = False
    serial: str = ""

    def has(self, feature: int) -> bool:
        return self.features.get(feature, 0) != 0

    def request(self, feature: int, fn: int, params=(), long_report=False, timeout=0.35):
        idx = self.features.get(feature, 0)
        if not idx:
            return None
        return self.transport.request(self.index, idx, fn, params, long_report, timeout)

    def raw_request(self, feature_idx: int, fn: int, params=(), long_report=False, timeout=0.35):
        return self.transport.request(self.index, feature_idx, fn, params, long_report, timeout)

    def read_name(self) -> str:
        if not self.has(FEATURE_DEVICE_NAME):
            return self.name
        r = self.request(FEATURE_DEVICE_NAME, 0, long_report=True)
        if not r:
            return self.name
        text = "".join(chr(b) for b in r.params[:20] if 32 <= b < 127).strip()
        return text or self.name

    def read_battery(self) -> tuple[int, str]:
        """返回 (百分比, 状态文字)。百分比 <0 表示未知。"""
        if self.has(FEATURE_UNIFIED_BATTERY):
            r = self.request(FEATURE_UNIFIED_BATTERY, 1)
            if r and not r.is_error:
                p = r.params
                percent = p[0] if len(p) > 0 else -1
                charge = p[2] if len(p) > 2 else 0
                external = p[3] if len(p) > 3 else 0
                status = "充电中" if charge in (1, 2) or external else ("充满" if charge == 3 else "放电中")
                return (percent if 0 <= percent <= 100 else -1), status

        if self.has(FEATURE_BATTERY_STATUS):
            r = self.request(FEATURE_BATTERY_STATUS, 0)
            if r and not r.is_error:
                p = r.params
                percent = p[0] if len(p) > 0 else -1
                state = p[2] if len(p) > 2 else 0
                status = "充满" if state == 3 else ("充电中" if state in (1, 2, 4) else "放电中")
                return (percent if 0 <= percent <= 100 else -1), status

        if self.has(FEATURE_BATTERY_VOLTAGE):
            r = self.request(FEATURE_BATTERY_VOLTAGE, 0)
            if r and not r.is_error:
                mv = r.u16(0)
                flags = r.params[2] if len(r.params) > 2 else 0
                external = bool(flags & 0x80)
                charge = flags & 0x07 if external else 7
                status = "充满" if charge == 1 else ("充电中" if external and charge == 0 else "放电中")
                return voltage_to_percent(mv), status

        # HID++ 1.0 老设备：class 0x07, cmd 0x80
        r = self.transport.raw_request(self.index, 0x07, 0x80, (0, 0, 0))
        if r and not r.is_error:
            p = r.params
            raw = p[1] if len(p) > 1 else 0
            if raw:
                percent = min(100, max(0, int(raw * 100 / 255)))
                return percent, "放电中"
        return -1, "不支持"

    def read_dpi(self) -> int:
        if self.has(FEATURE_ADJUSTABLE_DPI):
            r = self.request(FEATURE_ADJUSTABLE_DPI, 2, (0, 0, 0, 0))
            if r and not r.is_error:
                dpi = r.u16(1)
                if 50 <= dpi <= 32000:
                    return dpi
        if self.has(FEATURE_EXT_ADJUSTABLE_DPI):
            r = self.request(FEATURE_EXT_ADJUSTABLE_DPI, 5, (0, 0, 0, 0, 0, 0))
            if r and not r.is_error:
                dpi = r.u16(1)
                if 50 <= dpi <= 32000:
                    return dpi
        return 0

    def dpi_presets(self) -> list[int]:
        values: list[int] = []
        if self.has(FEATURE_ADJUSTABLE_DPI):
            r = self.request(FEATURE_ADJUSTABLE_DPI, 1, (0, 0, 0))
            if r and not r.is_error:
                params = r.params[1:]
                for i in range(0, len(params) - 1, 2):
                    value = (params[i] << 8) | params[i + 1]
                    if value == 0:
                        break
                    if value & 0xE000 == 0xE000:
                        continue
                    if 50 <= value <= 32000:
                        values.append(value)
        return values[:8]

    def _ensure_host_mode(self) -> None:
        if not self.has(FEATURE_ONBOARD_PROFILES):
            return
        r = self.request(FEATURE_ONBOARD_PROFILES, 2, (0, 0, 0))
        if r and not r.is_error and r.params and r.params[0] == 1:
            self.request(FEATURE_ONBOARD_PROFILES, 1, (2, 0, 0))

    def set_dpi(self, dpi: int) -> bool:
        dpi = int(dpi)
        if not 50 <= dpi <= 32000:
            return False
        self._ensure_host_mode()
        ok = False
        if self.has(FEATURE_ADJUSTABLE_DPI):
            r = self.request(FEATURE_ADJUSTABLE_DPI, 3, (0, dpi >> 8, dpi & 0xFF, 0))
            ok = bool(r and not r.is_error)
        if not ok and self.has(FEATURE_EXT_ADJUSTABLE_DPI):
            r = self.request(FEATURE_EXT_ADJUSTABLE_DPI, 6, (0, dpi >> 8, dpi & 0xFF, dpi >> 8, dpi & 0xFF, 0))
            ok = bool(r and not r.is_error)
        return ok


def _classify(name: str, has_dpi: bool) -> str:
    low = name.lower()
    if has_dpi or "mouse" in low or low.startswith("g") and any(c.isdigit() for c in low[:3]):
        return "mouse"
    if "key" in low or "keyboard" in low or "k" in low and "keys" in low:
        return "keyboard"
    if "receiver" in low:
        return "receiver"
    return "keyboard" if not has_dpi else "mouse"


class LogitechScanner:
    def __init__(self):
        self.handles: list[HidHandle] = []
        self.transport: HidppTransport | None = None
        self.devices: list = []
        self.aula_devices: list = []
        self.eweadn_devices: list = []
        self.notes: list[str] = []
        self.all_hid: list[HidHandle] = []

    def close(self) -> None:
        if self.transport:
            self.transport.close()
        for dev in self.aula_devices + self.eweadn_devices:
            try:
                dev.close()
            except Exception:
                pass
        self.aula_devices = []
        self.eweadn_devices = []
        self.devices = []

    @property
    def mouse(self) -> LogitechDevice | None:
        for d in self.devices:
            if d.kind == "mouse":
                return d
        return None

    @property
    def keyboard(self):
        for d in self.devices:
            if d.kind == "keyboard":
                return d
        return None

    def scan(self) -> list:
        self.close()
        self.notes = []
        found: list = []

        # 保留全部 HID 设备信息用于诊断（识别不到外设时可据此确认系统是否枚举到）
        self.all_hid = enumerate_logitech_hid(vid_filter=None)
        for h in self.all_hid:
            h.close()

        self.handles = enumerate_logitech_hid()
        if self.handles:
            self.transport = HidppTransport(self.handles)
            seen_names: set[str] = set()
            indices = [0xFF, 0x00] + list(range(1, 8))
            for idx in indices:
                ping = self.transport.request(idx, FEATURE_ROOT, 1, (0, 0, 0x5A))
                if not ping or ping.is_error:
                    continue
                if len(ping.params) < 3 or ping.params[2] != 0x5A:
                    continue

                dev = LogitechDevice(self.transport, idx, is_hidpp2=True)
                for code in FEATURES_TO_QUERY:
                    r = self.transport.request(
                        idx, FEATURE_ROOT, 0, (code >> 8, code & 0xFF, 0)
                    )
                    if r and not r.is_error:
                        dev.features[code] = r.params[0] if r.params else 0
                dev.name = dev.read_name()
                dev.kind = _classify(
                    dev.name,
                    dev.has(FEATURE_ADJUSTABLE_DPI) or dev.has(FEATURE_EXT_ADJUSTABLE_DPI),
                )
                key = (dev.name, dev.kind)
                if key in seen_names:
                    continue
                seen_names.add(key)
                found.append(dev)

            if not found:
                for idx in indices:
                    r = self.transport.raw_request(idx, 0x07, 0x80, (0, 0, 0))
                    if r and not r.is_error:
                        dev = LogitechDevice(
                            self.transport, idx, name="HID++ 1.0 设备", is_hidpp2=False
                        )
                        dev.kind = "mouse"
                        found.append(dev)
                        break
        else:
            self.notes.append("未找到 VID=046D 的罗技 HID 设备（鼠标 DPI 功能不可用）。")

        # 追加狼蛛 / AULA 键盘
        try:
            from aula_battery import scan_aula
            aula_devices, aula_notes = scan_aula()
            self.aula_devices = aula_devices
            self.notes.extend(aula_notes)
            found.extend(aula_devices)
        except Exception as exc:
            self.notes.append(f"AULA 扫描失败: {exc}")

        # 追加前行者 / EWEADN 鼠标
        try:
            from eweadn_mouse import scan_eweadn
            eweadn_devices, eweadn_notes = scan_eweadn()
            self.eweadn_devices = eweadn_devices
            self.notes.extend(eweadn_notes)
            found.extend(eweadn_devices)
        except Exception as exc:
            self.notes.append(f"前行者扫描失败: {exc}")

        # 追加迈从 / MCHOSE 鼠标（A7 V3 Ultra+ 等 0x4d 协议）
        try:
            from mchose_mouse import scan_mchose
            mchose_devices, mchose_notes = scan_mchose()
            self.notes.extend(mchose_notes)
            found.extend(mchose_devices)
        except Exception as exc:
            self.notes.append(f"迈从扫描失败: {exc}")

        self.devices = found
        return found

    def describe(self) -> str:
        lines = [f"系统 HID 接口总数: {len(self.all_hid)}（含触摸板/键盘/接收器等）"]
        seen: set[str] = set()
        for h in self.all_hid:
            lines.append(
                f"  VID/PID {h.vid:04X}:{h.pid:04X}  用法页 {h.usage_page:04X}  "
                f"用法 {h.usage:04X}  IN/OUT {h.input_len}/{h.output_len}  IF {h.iface}"
            )
            seen.add(f"{h.vid:04X}:{h.pid:04X}")
        lines.append(f"罗技 HID 接口数: {len(self.handles)}")
        lines.append(f"已识别设备数: {len(self.devices)}")
        for d in self.devices:
            idx = getattr(d, "index", None)
            features = getattr(d, "features", {})
            prefix = f"[{idx:02X}]" if isinstance(idx, int) else f"[{getattr(d, 'protocol', '?')}]"
            lines.append(
                f"  {prefix} {d.name} ({d.kind}) "
                f"特性: {', '.join(f'{k:04X}' for k, v in features.items() if v)}"
            )
        lines.extend(self.notes)
        return "\n".join(lines)


def probe() -> str:
    """命令行诊断入口。"""
    scanner = LogitechScanner()
    try:
        scanner.scan()
        text = scanner.describe()
        for d in scanner.devices:
            text += f"\n{d.name} 电量: {d.read_battery()}"
            if hasattr(d, "read_dpi"):
                text += f"  DPI: {d.read_dpi()}  档位: {d.dpi_presets()}"
        return text
    finally:
        scanner.close()


if __name__ == "__main__":
    print(probe())
