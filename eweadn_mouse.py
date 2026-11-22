"""前行者 / EWEADN 鼠标 HID 协议层（Windows，纯 ctypes）。

协议来自官方网页驱动 hub.eweadn.cn 的逆向资料（EWEADN H2 Control 项目）。
覆盖能力：
  - 电量百分比 / 充电状态（cmd 0x30 getDeviceInfo）
  - 6 档 DPI 表读取（cmd 0x13 getDpiData）
  - DPI 表写入 / 当前档位切换（cmd 0x03 setDpiValue）

已知 USB 身份：
  - 089D:062F   2.4G 接收器模式
  - 088D:062E   USB 有线 / 充电模式
  - 089D:062F   部分 S9 也使用此 ID（usage page FF01:0010，协议不同，仅报告）
  - 372E:1026   S9 Pro 8K（Feature 64 字节，协议不同，仅报告）

报文：33 字节（第 0 字节为 report ID 0），校验和 = sum(arr[5:32]) & 0xFF 放在第 32 字节。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import hidpp_win as hw
from aula_battery import (
    FILE_FLAG_OVERLAPPED,
    FILE_SHARE_READ_WRITE,
    GENERIC_READ_WRITE,
    INVALID_HANDLE_VALUE,
    OPEN_EXISTING,
    _product_string,
    _read_report,
    _write_report,
    kernel32,
)

# 已确认/待确认支持 H2 协议的 USB ID
KNOWN_IDS = {
    (0x089D, 0x062F): "H2 / S9 2.4G",
    (0x088D, 0x062E): "H2 有线充电",
}
# X23 Pro 接收器：不响应查询帧，只能被动监听其主动上报的传感器报告
X23_IDS = {
    (0x04F3, 0x026F): "X23 Pro",
}

# 已知存在但协议不同，仅做识别提示
OTHER_IDS = {
    (0x372E, 0x1026): "S9 Pro 8K（协议不同）",
}

ASYNC_MARKERS = (209, 210)
DPI_SLOT_COUNT = 6
DPI_OFFSETS = (5, 9, 13, 17, 21, 25)


def _checksum(arr: list[int]) -> int:
    return sum(arr[5:32]) & 0xFF


def _decode_dpi(raw: int, ic_type: int = 17) -> int:
    if ic_type != 17:
        return raw * 50
    if raw < 200:
        return 50 * raw
    if raw <= 220:
        return (raw - 200) * 100 + 10000
    return (raw - 220) * 1000 + 12000


def _encode_dpi(value: int, ic_type: int = 17) -> int:
    if ic_type != 17:
        return max(1, value // 50)
    if value >= 13000:
        return ((value - 13000) // 1000) + 221
    if value > 10000:
        return ((value - 10000) // 100) + 200
    return value // 50


def _drain(handle: int) -> None:
    for _ in range(20):
        if len(_read_report(handle, 33, 20)) < 5:
            break


def _send(
    handle: int,
    arr: list[int],
    timeout_ms: int = 250,
    attempts: int = 3,
    validator=None,
) -> bytes | None:
    """发送 33 字节命令并读取应答；自动过滤异步状态包与错包。"""
    arr = list(arr)[:33]
    if len(arr) < 33:
        arr += [0] * (33 - len(arr))
    arr[32] = _checksum(arr)
    last = None
    for attempt in range(attempts):
        _drain(handle)
        if not _write_report(handle, bytes(arr), 600):
            continue
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            resp = _read_report(handle, 33, timeout_ms)
            if len(resp) < 5:
                continue
            if resp[0] in ASYNC_MARKERS:
                continue
            last = resp
            if validator is None or validator(resp):
                return resp
            break
        if attempt < attempts - 1:
            time.sleep(0.12 * (attempt + 1))
    return last


def _device_info(handle: int) -> dict:
    def valid(r: bytes) -> bool:
        ic, rm, cf, bv, cs = r[10], r[11], r[12], r[13], r[14]
        if not (1 <= ic <= 250) or not (1 <= rm <= 250):
            return False
        if cf not in (0, 1) or not (0 <= bv <= 100) or not (0 <= cs <= 5):
            return False
        return all(
            b == 0 or 48 <= b <= 57 or 65 <= b <= 90 for b in r[4:8]
        )

    arr = [0x00, 0x30] + [0] * 31
    r = _send(handle, arr, validator=valid)
    if r is None or not valid(r):
        return {}
    device_id = "".join(chr(b) for b in r[4:8] if b)
    return {
        "device_id": device_id,
        "firmware": f"v{r[9]:02X}.{r[8]:02X}",
        "ic_type": r[10],
        "report_max": r[11],
        "charge_flag": r[12],
        "battery": r[13],
        "connect": r[14],
    }


def _dpi_config(handle: int, ic_type: int = 17) -> dict:
    def valid(r: bytes) -> bool:
        count = r[4] & 0x0F
        index = (r[4] >> 4) & 0x0F
        if not (1 <= count <= DPI_SLOT_COUNT) or index >= DPI_SLOT_COUNT:
            return False
        return not all(r[o] == 0 and r[o + 1] == 0 for o in DPI_OFFSETS)

    arr = [0x00, 0x13] + [0] * 31
    r = _send(handle, arr, validator=valid)
    if r is None or not valid(r):
        return {}
    index = (r[4] >> 4) & 0x0F
    count = r[4] & 0x0F
    levels = []
    for o in DPI_OFFSETS:
        raw = r[o] | (r[o + 1] << 8)
        levels.append(_decode_dpi(raw, ic_type) if raw else 0)
    return {"index": index, "count": count, "levels": levels}


def _write_dpi_config(handle: int, levels: list[int], index: int, count: int, ic_type: int = 17) -> bool:
    arr = [0] * 33
    arr[1] = 0x03
    arr[3] = 1
    arr[4] = 26
    arr[6] = (count & 0x0F) | ((index & 0x0F) << 4)
    offset = 7
    for value in levels:
        enc = _encode_dpi(int(value), ic_type)
        arr[offset:offset + 4] = [enc & 0xFF, (enc >> 8) & 0xFF, enc & 0xFF, (enc >> 8) & 0xFF]
        offset += 4
    # 写命令不自动重发，避免设备写 EEPROM 时收到第二遍写导致配置损坏
    r = _send(handle, arr, attempts=1)
    time.sleep(0.35)
    return r is not None


@dataclass
class EweadnMouse:
    handle: int
    path: str
    vid: int
    pid: int
    iface: int
    name: str
    kind: str = "mouse"
    protocol: str = "eweadn_h2"
    ic_type: int = 17

    def close(self) -> None:
        if self.handle and self.handle != INVALID_HANDLE_VALUE:
            try:
                kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = 0

    def read_battery(self) -> tuple[int, str]:
        if not self.handle:
            return -1, "设备未连接"
        if self.protocol == "x23_passive":
            # X23 Pro 接收器主动上报状态帧：04 01 01 01 64 ...，byte[4] 为电量百分比。
            # Windows 的 ReadFile 会保留 report ID 字节（0x04），不参与电量解析。
            deadline = time.monotonic() + 1.6
            last = -1
            while time.monotonic() < deadline:
                raw = _read_report(self.handle, 32, 200)
                if len(raw) < 6 or not any(raw):
                    continue
                level = raw[4]
                if 0 <= level <= 100:
                    last = level
                    # 连续两包一致才采纳，避开刚唤醒时的旧缓存值
                    nxt = _read_report(self.handle, 32, 200)
                    if len(nxt) >= 6 and nxt[4] == level:
                        return level, "放电中"
            return (last, "放电中") if last >= 0 else (-1, "等待上报（移动一下鼠标）")
        info = _device_info(self.handle)
        if not info:
            return -1, "无应答（确认网页驱动已关闭）"
        self.ic_type = info.get("ic_type", 17)
        status = "充电中" if info.get("charge_flag") else "放电中"
        return info.get("battery", -1), status

    def read_dpi(self) -> int:
        if not self.handle or self.protocol != "eweadn_h2":
            return 0
        cfg = _dpi_config(self.handle, self.ic_type)
        if not cfg:
            return 0
        index = min(cfg["index"], len(cfg["levels"]) - 1)
        return cfg["levels"][index] if index >= 0 else 0

    def dpi_presets(self) -> list[int]:
        if not self.handle or self.protocol != "eweadn_h2":
            return []
        cfg = _dpi_config(self.handle, self.ic_type)
        if not cfg:
            return []
        return [v for v in cfg["levels"][: cfg["count"]] if v > 0]

    def set_dpi(self, value: int) -> bool:
        """把当前活动档位改为指定 DPI，其余档位保持不变。"""
        if not self.handle or self.protocol != "eweadn_h2" or not 50 <= value <= 32000:
            return False
        cfg = _dpi_config(self.handle, self.ic_type)
        if not cfg:
            return False
        levels = list(cfg["levels"])
        index = min(cfg["index"], DPI_SLOT_COUNT - 1)
        levels[index] = int(value)
        # 未启用的档位保持原值；count 不变，避免意外改变轮转档位数
        return _write_dpi_config(self.handle, levels, index, cfg["count"], self.ic_type)

    def set_active_slot(self, slot: int) -> bool:
        """切换当前 DPI 档位，slot 从 1 开始。"""
        if not self.handle or not 1 <= slot <= DPI_SLOT_COUNT:
            return False
        cfg = _dpi_config(self.handle, self.ic_type)
        if not cfg:
            return False
        return _write_dpi_config(self.handle, cfg["levels"], slot - 1, cfg["count"], self.ic_type)


def _pick_interface(infos: list[hw.HidHandle]) -> hw.HidHandle | None:
    """优先厂商自定义接口（interface 2 / usage page >= FF00 且 33 字节报文）。"""
    scored = []
    for info in infos:
        score = 0
        if info.iface == 2:
            score += 4
        if info.usage_page >= 0xFF00:
            score += 3
        if info.input_len >= 33 and info.output_len >= 33:
            score += 3
        elif info.feature_len >= 33:
            score += 2
        if info.iface >= 0:
            score += 1
        scored.append((score, info))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1] if scored and scored[0][0] >= 3 else None


def scan_eweadn() -> tuple[list[EweadnMouse], list[str]]:
    """扫描前行者鼠标；返回 (设备列表, 诊断说明)。"""
    notes: list[str] = []
    mice: list[EweadnMouse] = []
    infos = hw.enumerate_logitech_hid(vid_filter=None)
    by_device: dict[tuple[int, int], list[hw.HidHandle]] = {}
    try:
        for info in infos:
            by_device.setdefault((info.vid, info.pid), []).append(info)

        for (vid, pid), group in by_device.items():
            label = KNOWN_IDS.get((vid, pid))

            # X23 Pro：被动监听模式，选 32 字节输入的 000A 集合
            if label is None and (vid, pid) in X23_IDS:
                passive = next(
                    (i for i in group if i.usage_page == 0x000A and i.input_len == 32),
                    None,
                )
                if passive:
                    path = passive.path
                    passive.handle = 0
                    handle = kernel32.CreateFileW(
                        path, GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None,
                        OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None,
                    )
                    if handle not in (0, INVALID_HANDLE_VALUE):
                        name = _product_string(handle) or f"前行者 {X23_IDS[(vid, pid)]} 接收器"
                        mice.append(EweadnMouse(
                            handle=handle, path=path, vid=vid, pid=pid,
                            iface=passive.iface, name=name, protocol="x23_passive",
                        ))
                        notes.append(
                            f"检测到前行者鼠标 [X23 Pro 被动监听] {name} ({vid:04X}:{pid:04X})"
                        )
                    else:
                        notes.append(f"{vid:04X}:{pid:04X} 无法打开 HID 接口。")
                continue

            if label is None and (vid, pid) in OTHER_IDS:
                notes.append(
                    f"发现前行者设备 {vid:04X}:{pid:04X}（{OTHER_IDS[(vid, pid)]}），"
                    "当前协议层未覆盖其电量读取。"
                )
                continue
            if label is None:
                continue
            chosen = _pick_interface(group)
            if chosen is None:
                notes.append(f"{vid:04X}:{pid:04X} 未找到 33 字节厂商 HID 接口。")
                continue
            path = chosen.path
            chosen.handle = 0  # 所有权转交给下面的重叠 I/O 句柄
            handle = kernel32.CreateFileW(
                path, GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None,
                OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None,
            )
            if handle in (0, INVALID_HANDLE_VALUE):
                notes.append(f"{vid:04X}:{pid:04X} 无法打开 HID 接口（可能被官方驱动占用）。")
                continue
            name = _product_string(handle) or f"前行者 {label} {vid:04X}:{pid:04X}"
            mice.append(
                EweadnMouse(
                    handle=handle, path=path, vid=vid, pid=pid,
                    iface=chosen.iface, name=name,
                )
            )
            notes.append(f"检测到前行者鼠标 [{label}] {name} ({vid:04X}:{pid:04X}, IF {chosen.iface})")
    finally:
        for info in infos:
            info.close()

    if (0xA8A5, 0x2255) in by_device:
        notes.append(
            "发现 2.4G 接收器 A8A5:2255（前行者 X23SE / 迈从等共用此 ID），"
            "当前协议层不支持其电量读取。")
    if not mice and not any("前行者" in n for n in notes):
        notes.append(
            "未检测到前行者鼠标的已知 USB ID"
            "（089D:062F / 088D:062E / 04F3:026F）。")
    return mice, notes
