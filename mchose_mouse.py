"""迈从 / MCHOSE 鼠标 HID 协议层（Windows，纯 ctypes）。

覆盖型号（VID 0x3837）：
  - 1018  A7 V3 Ultra+（8K 接收器）
  - 1014  MCHOSE V3 8K 接收器
  - 1016  MCHOSE V3 1K 接收器

协议「0x4d」：
  - 所有帧 report ID = 0x4D，报文长度 64 字节
  - 查询帧：4D 01 01 00 00 <cmd_lo> <cmd_hi> 00 <xor>
    其中 xor = 第 2..7 字节异或
  - 电量查询命令：0x0900（readBasics），部分型号也接受 0x0010
  - 应答从第 8 字节开始：
      vid:u16le, pid:u16le, fw:u32le, 保留×2, mode:u8, charge:u8, level:u8
    level 只接受 0..100；charge != 0 表示充电中
  - 厂商接口：usagePage 0xFF0B / usage 0x00C4（优先），
    缺失时退回 usagePage 0xFF01 / usage 0x0001（接口 2）
  - 磁吸充电底座接入时同样暴露 0xFF0B/0x00C4，查询会经无线电转发给
    无线连接的鼠标，所以无线模式下也能读到电量（应答里的 pid 是鼠标本体）。
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

# 已确认支持 0x4d 协议的 USB ID
KNOWN_IDS = {
    (0x3837, 0x1018): "A7 V3 Ultra+",
    (0x3837, 0x1014): "MCHOSE V3 8K 接收器",
    (0x3837, 0x1016): "MCHOSE V3 1K 接收器",
}

REPORT_ID = 0x4D
REPORT_LEN = 64          # 载荷长度（不含 report ID）
CMD_READ_BASICS = 0x0900
CMD_READ_BASICS_ALT = 0x0010


def _build_frame(cmd: int, payload_len: int = REPORT_LEN) -> bytes:
    """构造查询帧：4D 01 01 00 00 <cmd> 00 00 <xor>，其余补零。"""
    frame = bytearray(payload_len)
    frame[0] = REPORT_ID
    frame[1] = 0x01
    frame[2] = 0x01
    frame[3] = 0x00
    frame[4] = 0x00
    frame[5] = (cmd >> 8) & 0xFF   # 命令字大端（官方抓包：4d 01 01 00 00 09 00 00 08）
    frame[6] = cmd & 0xFF
    frame[7] = 0x00
    frame[8] = 0x00 ^ frame[2] ^ frame[3] ^ frame[4] ^ frame[5] ^ \
        frame[6] ^ frame[7]
    # 校验位落在第 8 字节（与官方抓包一致：4d 01 01 00 00 09 00 00 08）
    return bytes(frame)


def _parse_response(raw: bytes) -> dict | None:
    """从应答里取 mode / charge / level。"""
    if len(raw) < 21 or raw[0] != REPORT_ID:
        return None
    level = raw[20]
    if not 0 <= level <= 100:
        return None
    charge = raw[19]
    mode = raw[18]
    vid = raw[8] | (raw[9] << 8)
    pid = raw[10] | (raw[11] << 8)
    return {
        "level": level,
        "charging": bool(charge),
        "mode": mode,
        "vid": vid,
        "pid": pid,
    }


@dataclass
class MchoseMouse:
    handle: int
    path: str
    vid: int
    pid: int
    iface: int
    name: str
    kind: str = "mouse"
    protocol: str = "mchose_4d"
    out_len: int = REPORT_LEN + 1
    in_len: int = REPORT_LEN + 1
    # 备用接口（同一设备的其它厂商集合），任一有应答即采用
    extra: list[tuple[int, int, int]] = None  # [(handle, in_len, out_len)]

    def close(self) -> None:
        for h in [self.handle] + [e[0] for e in (self.extra or [])]:
            if h and h != INVALID_HANDLE_VALUE:
                try:
                    kernel32.CloseHandle(h)
                except Exception:
                    pass
        self.handle = 0
        self.extra = []

    def _try_handle(self, handle: int, in_len: int, out_len: int,
                    trace: list[str] | None = None) -> dict | None:
        for cmd in (CMD_READ_BASICS, CMD_READ_BASICS_ALT):
            try:
                for _ in range(4):
                    if len(_read_report(handle, in_len, 30)) < 8:
                        break
                frame = _build_frame(cmd, max(1, out_len - 1))
                if not _write_report(handle, frame, 400):
                    if trace is not None:
                        trace.append(f"cmd 0x{cmd:04X} 写入失败")
                    continue
                deadline = time.monotonic() + 0.8
                while time.monotonic() < deadline:
                    raw = _read_report(handle, in_len, 200)
                    if len(raw) < 8:
                        continue
                    if trace is not None and len(trace) < 4:
                        trace.append(
                            f"cmd 0x{cmd:04X} 收到 {len(raw)} 字节: "
                            + raw[:24].hex(" "))
                    if len(raw) < 21:
                        continue
                    info = _parse_response(raw)
                    if info:
                        return info
            except Exception as exc:
                if trace is not None:
                    trace.append(f"cmd 0x{cmd:04X} 异常: {exc!r}")
                continue
        return None

    def read_battery(self) -> tuple[int, str]:
        if not self.handle:
            return -1, "设备未连接"
        info = self._try_handle(self.handle, self.in_len, self.out_len)
        if info is None:
            for handle, in_len, out_len in (self.extra or []):
                info = self._try_handle(handle, in_len, out_len)
                if info:
                    break
        if info is None:
            return -1, "无应答（确认设备已连接）"
        status = "充电中" if info["charging"] else "放电中"
        return info["level"], status


def _candidate_interfaces(group: list[hw.HidHandle]) -> list[hw.HidHandle]:
    """列出可能承载 0x4d 协议的厂商接口（优先级从高到低）。

    - 0xFF0B/0x00C4：磁吸底座 / 充电底座暴露的厂商集合
    - 0xFF01/0x0001：接收器主厂商集合（接口 2 优先）
    """
    scored: list[tuple[int, hw.HidHandle]] = []
    for info in group:
        if info.usage_page == 0xFF0B and info.usage == 0x00C4:
            score = 6
        elif info.usage_page == 0xFF01 and info.usage == 0x0001:
            score = 5 if info.iface == 2 else 3
        else:
            continue
        if info.input_len >= 64 and info.output_len >= 64:
            score += 2
        scored.append((score, info))
    scored.sort(key=lambda item: -item[0])
    return [info for _, info in scored[:3]]


def scan_mchose() -> tuple[list[MchoseMouse], list[str]]:
    """扫描迈从鼠标；返回 (设备列表, 诊断说明)。"""
    notes: list[str] = []
    mice: list[MchoseMouse] = []
    infos = hw.enumerate_logitech_hid(vid_filter=0x3837)
    try:
        by_device: dict[tuple[int, int], list[hw.HidHandle]] = {}
        for info in infos:
            by_device.setdefault((info.vid, info.pid), []).append(info)

        for (vid, pid), group in by_device.items():
            label = KNOWN_IDS.get((vid, pid))
            if label is None:
                continue
            candidates = _candidate_interfaces(group)
            if not candidates:
                notes.append(
                    f"检测到迈从 {label}（{vid:04X}:{pid:04X}），"
                    "但未找到 0x4D 协议的厂商接口（0xFF0B/0x00C4、0xFF01/0x0001）。")
                continue

            opened: list[tuple[int, hw.HidHandle]] = []
            for info in candidates:
                handle = kernel32.CreateFileW(
                    info.path, GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None,
                    OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None,
                )
                if handle in (0, INVALID_HANDLE_VALUE):
                    continue
                opened.append((handle, info))
            if not opened:
                notes.append(
                    f"迈从 {label} 的厂商接口无法打开（可能被官方驱动占用）。")
                continue

            main_handle, main_info = opened[0]
            name = _product_string(main_handle) or f"迈从 {label}"
            extras = [
                (h, max(64, i.input_len or 65), max(64, i.output_len or 65))
                for h, i in opened[1:]
            ]
            mice.append(MchoseMouse(
                handle=main_handle, path=main_info.path, vid=vid, pid=pid,
                iface=main_info.iface, name=name,
                out_len=max(64, main_info.output_len or 65),
                in_len=max(64, main_info.input_len or 65),
                extra=extras,
            ))
            notes.append(
                f"检测到迈从鼠标 [0x4d 协议] {name} ({vid:04X}:{pid:04X}, IF {main_info.iface}"
                + (f"，另有 {len(extras)} 个备用接口" if extras else "") + ")")

            # 立刻探一次，把设备实际回了什么记进诊断（方便适配调试）
            dev = mice[-1]
            trace: list[str] = []
            dev._try_handle(dev.handle, dev.in_len, dev.out_len, trace)
            for handle, in_len, out_len in extras:
                if trace:
                    break
                dev._try_handle(handle, in_len, out_len, trace)
            if trace:
                for item in trace[:3]:
                    notes.append("迈从 0x4d 查询回包: " + item)
            else:
                notes.append("迈从 0x4d 查询: 无任何回包（可能被官方驱动占用或鼠标休眠）")
    finally:
        for info in infos:
            info.close()

    if not mice and not any("迈从" in n for n in notes):
        notes.append("未检测到迈从鼠标的已知 USB ID（3837:1018 / 1014 / 1016）。")
    return mice, notes
