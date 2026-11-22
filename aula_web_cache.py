"""从浏览器本地存储读取狼蛛网页驱动的设备状态（含键盘电量）。

背景：狼蛛官方网页驱动 https://hub.aulacn.com 连接设备后，会把设备信息
缓存进浏览器 Local Storage（leveldb），形如：

    {"deviceInfo":{"pId":65278,"vId":3141,"pName":"MINI 60 HE PRO Dongle",...},
     "version":"V1.53","deviceId":"32930_3141","isCharging":false,"battery":92}

该缓存可直接解析出电量百分比，无需与设备通信、无需扩展、无需管理员权限。
这是目前唯一能拿到狼蛛 Mini60 Pro（SONiX 2.4G）电量且不依赖打开网页的途径。

局限：数据来自网页驱动最后一次连接时的快照，若长时间未打开网页驱动，
电量可能不是最新（电量变化慢，通常仍可用）。调用方可结合时间戳判断。
"""

from __future__ import annotations

import os
import re
import time

# 命中条件：同一段文本里同时出现型号关键字与 battery 字段
_MODEL_HINTS = ("MINI 60", "65278", "MINI60")
_BATTERY_RE = re.compile(r'"battery"\s*:\s*(\d{1,3})')
_CHARGING_RE = re.compile(r'"isCharging"\s*:\s*(true|false)')
_VERSION_RE = re.compile(r'"version"\s*:\s*"([^"]{1,16})"')
_NAME_RE = re.compile(r'"pName"\s*:\s*"([^"]{1,64})"')

# 扫描时按 mtime 取最新的若干个文件。leveldb 会把旧 .log 合并成 .ldb，
# 合并后的 .ldb 虽然更旧但可能仍持有最新数据（实测 battery 会落在第 4~6 个文件里），
# 因此范围要留够，否则会漏读导致电量突然读不到。
_MAX_FILES = 8
# 单个文件最多读 4MB（leveldb 段文件可能很大）
_MAX_BYTES = 4 * 1024 * 1024

_cache: dict = {"key": None, "value": None, "ts": 0.0}
_mouse_cache: dict = {"key": None, "value": None, "ts": 0.0}
_CACHE_TTL = 8.0  # 秒

# 迈从 G3 / ATK 等网页驱动可能把电量写成 batteryLevel（数字或带 % 的字符串），
# "breakConnect" 表示网页驱动本身没连上设备，此时不能当成电量。
_MOUSE_BATTERY_RE = re.compile(r'"batteryLevel"\s*:\s*"?(\d{1,3})(?:\.\d+)?%?"?')
_GENERIC_BATTERY_RE = re.compile(r'"battery"\s*:\s*(\d{1,3})')
_MOUSE_CHARGE_RE = re.compile(r'"chargeStatus"\s*:\s*(\d+)')
_MOUSE_PRODUCT_RE = re.compile(r'"productName"\s*:\s*"([^"]{1,64})"')
_MOUSE_DEVICE_RE = re.compile(r'"deviceName"\s*:\s*"([^"]{1,64})"')

# 前行者 EWEADN 网页驱动预留规则。
# 注意：X23 Pro 等型号走 HID 直连（eweadn_mouse.py 的 x23_passive），不需要网页缓存；
# 实测 hub.eweadn.cn / new-hub.eweadn.cn 的 Local Storage 里只有 mouse-configs
# （按键映射/DPI）和 powerSleepTime（休眠时间）等设置项，并没有电量字段。
# 因此这里只在「前行者域名的数据段内」出现严格电量字段名时才解析，
# 避免把 powerSleepTime / powerTimeRange 之类误读成电量。
_EWEADN_DOMAIN_RE = re.compile(r"eweadn\.cn", re.I)
_EWEADN_RECORD_RE = re.compile(r"_[0-9]?https?://")
_EWEADN_BATTERY_RE = re.compile(
    r'"(?:batteryLevel|batteryPercent|battery)"\s*:\s*"?(\d{1,3})(?:\.\d+)?%?"?'
)
_EWEADN_CHARGE_RE = re.compile(
    r'"(?:chargeStatus|isCharging)"\s*:\s*"?([^",}]{1,12})'
)


def _profile_dirs() -> list[str]:
    """返回 Edge / Chrome 各 profile 的 Local Storage/leveldb 目录。"""
    bases = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Edge\User Data"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\User Data"),
    ]
    dirs: list[str] = []
    for base in bases:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            if name != "Default" and not name.startswith("Profile "):
                continue
            d = os.path.join(base, name, "Local Storage", "leveldb")
            if os.path.isdir(d):
                dirs.append(d)
    return dirs


# Electron 类驱动客户端（Aula Hub / MCHOSE HUB 等）把设备状态存在自己的
# Local Storage 里，结构和网页驱动一致，同样能读到 battery。
_APP_DIR_HINTS = (
    ("mchose", "迈从 MCHOSE"),
    ("aula", "狼蛛 AULA"),
    ("eweadn", "前行者 EWEADN"),
    ("atk", "ATK"),
    ("ikf", "iKF"),
    ("vgn", "VGN"),
    ("logitech", "罗技 Logitech"),
    ("lghub", "罗技 Logitech"),
)


def _app_storage_dirs() -> list[tuple[str, str]]:
    """驱动客户端自己的 Local Storage 目录：[(目录, 品牌)]。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    bases = [os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")]
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            low = name.lower()
            brand = next((b for hint, b in _APP_DIR_HINTS if hint in low), None)
            if not brand:
                continue
            d = os.path.join(base, name, "Local Storage", "leveldb")
            if os.path.isdir(d) and d not in seen:
                seen.add(d)
                out.append((d, brand))
    return out


def _all_storage_dirs() -> list[str]:
    """浏览器缓存目录 + 驱动客户端缓存目录。"""
    return _profile_dirs() + [d for d, _ in _app_storage_dirs()]


# 驱动客户端目录 → 设备类别（决定它算键盘还是鼠标）
_KEYBOARD_APP_BRANDS = {"狼蛛 AULA"}
_MOUSE_APP_BRANDS = {"迈从 MCHOSE", "前行者 EWEADN", "ATK", "iKF", "VGN",
                     "罗技 Logitech"}

# 客户端本地存储里通用的字段（品牌已知，不需要靠域名判断）
_APP_BATTERY_RE = re.compile(
    r'"(?:batteryLevel|batteryPercent|battery)"\s*:\s*"?(\d{1,3})(?:\.\d+)?%?"?')
_APP_CHARGE_RE = re.compile(r'"(?:isCharging|chargeStatus)"\s*:\s*"?([^",}]{1,12})')
_APP_NAME_RE = re.compile(
    r'"(?:productName|deviceName|pName|customName|productModel|modelName)"'
    r'\s*:\s*"([^"]{1,64})"')


def _parse_app_text(text: str, mtime: float, filename: str,
                    brand: str) -> dict | None:
    """从驱动客户端的本地存储里解析电量（品牌已知，不依赖域名关键字）。"""
    if not text:
        return None
    picked = None
    for m in _APP_BATTERY_RE.finditer(text):
        if 0 <= int(m.group(1)) <= 100:
            picked = m          # leveldb 里越靠后 = 越新
    if picked is None:
        return None

    percent = int(picked.group(1))
    ctx = text[max(0, picked.start() - 3000): picked.end() + 1500]
    charging = False
    cm = _APP_CHARGE_RE.search(ctx)
    if cm:
        charging = cm.group(1).strip().lower() in (
            "1", "true", "charging", "charge", "chargingstate")
    nm = _APP_NAME_RE.search(ctx)
    raw_name = nm.group(1).strip() if nm else ""
    if raw_name:
        latin = brand.split()[-1].upper()   # MCHOSE / AULA / EWEADN
        if raw_name.upper().startswith(latin[:4]):
            name = raw_name
        else:
            name = f"{brand.split()[0]} {raw_name}"
    else:
        name = brand.split()[0]
    return {
        "percent": percent,
        "charging": charging,
        "name": name,
        "file": filename,
        "mtime": mtime,
    }



def _scan_file(path: str) -> dict | None:
    """在单个 leveldb 文件里找最新的狼蛛设备记录。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > _MAX_BYTES:
                f.seek(max(0, size - _MAX_BYTES))
            raw = f.read(_MAX_BYTES)
    except Exception:
        return None

    text = raw.decode("utf-8", "ignore")
    if not text:
        return None

    matches = list(_BATTERY_RE.finditer(text))
    if not matches:
        return None

    # leveldb 做 compaction 时会把 key/value 拆块，型号字段可能与 battery 相隔很远，
    # 所以采用两级匹配：优先看上下文，找不到再退化为「整个文件含型号即认为可信」。
    file_has_model = any(h in text for h in _MODEL_HINTS)
    picked = None
    for m in matches:
        ctx = text[max(0, m.start() - 3000): m.end() + 1500]
        if any(h in ctx for h in _MODEL_HINTS):
            picked = m
        # 继续循环 → 最终留下的是最靠后（最新）的一条
    if picked is None and file_has_model:
        picked = matches[-1]
    if picked is None:
        return None

    m = picked
    level = int(m.group(1))
    if not 1 <= level <= 100:
        return None
    ctx = text[max(0, m.start() - 3000): m.end() + 1500]
    charging = False
    cm = _CHARGING_RE.search(ctx)
    if cm:
        charging = cm.group(1) == "true"
    vm = _VERSION_RE.search(ctx)
    nm = _NAME_RE.search(ctx)
    return {
        "percent": level,
        "charging": charging,
        "version": vm.group(1) if vm else "",
        "name": nm.group(1) if nm else "",
        "file": os.path.basename(path),
        "mtime": os.path.getmtime(path),
    }


def read_keyboard_battery(force: bool = False) -> dict | None:
    """读取键盘电量。

    返回 {"percent": int, "charging": bool, "version": str, "name": str,
          "age": float  # 缓存写入距今秒数
         }，未找到返回 None。
    """
    # 浏览器缓存 + 键盘类驱动客户端缓存（Aula Hub 等）
    sources: list[tuple[str, str | None]] = [(d, None) for d in _profile_dirs()]
    sources.extend((d, brand) for d, brand in _app_storage_dirs()
                   if brand in _KEYBOARD_APP_BRANDS)
    if not sources:
        return None

    files: list[tuple[str, str | None]] = []
    for d, brand in sources:
        try:
            for name in os.listdir(d):
                if name.endswith(".log") or name.endswith(".ldb"):
                    files.append((os.path.join(d, name), brand))
        except Exception:
            continue
    if not files:
        return None

    files.sort(key=lambda item: os.path.getmtime(item[0]), reverse=True)
    cache_key = tuple((os.path.basename(p), os.path.getmtime(p))
                      for p, _ in files[:_MAX_FILES])

    now = time.time()
    if not force and _cache["value"] is not None and _cache["key"] == cache_key \
            and now - _cache["ts"] < _CACHE_TTL:
        return _cache["value"]

    # 优先采用「有电量数值」的结果
    result = None
    fallback = None
    for path, brand in files[:_MAX_FILES]:
        hit = _scan_file(path)
        if not hit and brand:
            hit = _parse_app_text(_read_text(path), os.path.getmtime(path),
                                  os.path.basename(path), brand)
        if not hit:
            continue
        if hit.get("percent", -1) >= 0:
            result = dict(hit)
            result["age"] = max(0.0, now - hit["mtime"])
            break
        if fallback is None:
            fallback = dict(hit)
            fallback["age"] = max(0.0, now - hit["mtime"])
    if result is None:
        result = fallback
    if result is None or result.get("percent", -1) < 0:
        for brand in _KEYBOARD_APP_BRANDS:
            hit = _client_battery_from_disk(brand)
            if hit:
                result = hit
                break
    if result is None or result.get("percent", -1) < 0:
        hit = _webcache_battery_from_disk(("aula", "sonix"))
        if hit:
            result = hit
    if result is not None and result.get("percent", -1) >= 0:
        _LAST_GOOD["keyboard"] = dict(result)
    elif _LAST_GOOD["keyboard"] is not None:
        # 这次没读到，沿用上一次成功的结果（界面会标注是多久之前的值）
        result = dict(_LAST_GOOD["keyboard"])

    _cache["key"] = cache_key
    _cache["value"] = result
    _cache["ts"] = now
    return result


def _read_text(path: str) -> str:
    """读取 leveldb 段文件末尾的内容（避免整个大文件读入内存）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > _MAX_BYTES:
                f.seek(max(0, size - _MAX_BYTES))
            raw = f.read(_MAX_BYTES)
        return raw.decode("utf-8", "ignore")
    except Exception:
        return ""


def _parse_mouse_text(text: str, mtime: float, filename: str) -> dict | None:
    """从浏览器缓存文本里找迈从 / 通用网页驱动的鼠标电量。

    迈从驱动写入的字段是 ``"batteryLevel":数字``（或带 % 的字符串）；
    ``"batteryLevel":"breakConnect"`` 表示网页驱动没连上设备，不能当电量。
    """
    if not text or not re.search(r"mchose", text, re.I):
        return None

    picked = None
    for pattern in (_MOUSE_BATTERY_RE, _GENERIC_BATTERY_RE):
        for m in pattern.finditer(text):
            ctx = text[max(0, m.start() - 4000): m.end() + 2000]
            if re.search(r"mchose", ctx, re.I):
                picked = m
        if picked is not None:
            break
    if picked is None:
        return None

    percent = int(picked.group(1))
    if not 0 <= percent <= 100:
        return None

    ctx = text[max(0, picked.start() - 4000): picked.end() + 2000]
    cm = _MOUSE_CHARGE_RE.search(ctx)
    charging = bool(cm and cm.group(1) != "0")
    nm = _MOUSE_PRODUCT_RE.search(ctx) or _MOUSE_DEVICE_RE.search(ctx)
    raw_name = nm.group(1) if nm else ""
    if not raw_name:
        name = "迈从 G3"
    elif raw_name.upper().startswith("MCHOSE"):
        name = raw_name
    else:
        name = f"迈从 {raw_name}"

    return {
        "percent": percent,
        "charging": charging,
        "name": name,
        "file": filename,
        "mtime": mtime,
    }


def _parse_eweadn_text(text: str, mtime: float, filename: str) -> dict | None:
    """从前行者网页驱动缓存里找鼠标电量（预留规则）。

    只在包含 eweadn.cn 域名的数据段内、且出现严格电量字段名时才解析。
    这样即使同一文件里有迈从/其他网站缓存，也不会串味；同时也不会把
    ``powerSleepTime`` / ``powerTimeRange`` 等休眠设置误当成电量。
    """
    if not text or not _EWEADN_DOMAIN_RE.search(text):
        return None

    # leveldb 里每条记录以 "_https://域名" 开头，按记录边界切段。
    segments = _EWEADN_RECORD_RE.split(text)
    for seg in segments:
        if not _EWEADN_DOMAIN_RE.search(seg[:120]):
            continue
        # 电量字段必须在这一段（同一个域名记录）里
        picked = None
        for m in _EWEADN_BATTERY_RE.finditer(seg):
            if 0 <= int(m.group(1)) <= 100:
                picked = m
        if picked is None:
            continue

        percent = int(picked.group(1))
        ctx = seg[max(0, picked.start() - 2000): picked.end() + 2000]
        charging = False
        cm = _EWEADN_CHARGE_RE.search(ctx)
        if cm:
            val = cm.group(1).strip().lower()
            charging = val in ("1", "true", "charging", "charge", "chargingstate")
        nm = _MOUSE_PRODUCT_RE.search(ctx) or _MOUSE_DEVICE_RE.search(ctx)
        raw_name = nm.group(1) if nm else ""
        name = f"前行者 {raw_name}" if raw_name and "前行者" not in raw_name else (raw_name or "前行者鼠标")

        return {
            "percent": percent,
            "charging": charging,
            "name": name,
            "file": filename,
            "mtime": mtime,
        }
    return None


def read_mouse_battery(force: bool = False) -> dict | None:
    """读取鼠标网页驱动缓存（迈从 G3 / 前行者等）。未连上 / 没数据返回 None。"""
    # 浏览器缓存 + 鼠标类驱动客户端缓存（MCHOSE HUB / 前行者 HUB 等）
    sources: list[tuple[str, str | None]] = [(d, None) for d in _profile_dirs()]
    sources.extend((d, brand) for d, brand in _app_storage_dirs()
                   if brand in _MOUSE_APP_BRANDS)
    if not sources:
        return None

    files: list[tuple[str, str | None]] = []
    for d, brand in sources:
        try:
            for name in os.listdir(d):
                if name.endswith(".log") or name.endswith(".ldb"):
                    files.append((os.path.join(d, name), brand))
        except Exception:
            continue
    if not files:
        return None

    files.sort(key=lambda item: os.path.getmtime(item[0]), reverse=True)
    cache_key = tuple((os.path.basename(p), os.path.getmtime(p))
                      for p, _ in files[:_MAX_FILES])
    now = time.time()
    if not force and _mouse_cache["value"] is not None \
            and _mouse_cache["key"] == cache_key \
            and now - _mouse_cache["ts"] < _CACHE_TTL:
        return _mouse_cache["value"]

    # 优先采用「有电量数值」的结果；只有全是未连接时才退而求其次
    result = None
    fallback = None
    for path, brand in files[:_MAX_FILES]:
        try:
            mt = os.path.getmtime(path)
        except Exception:
            continue
        text = _read_text(path)
        hit = _parse_mouse_text(text, mt, os.path.basename(path)) \
            or _parse_eweadn_text(text, mt, os.path.basename(path))
        if not hit and brand:
            hit = _parse_app_text(text, mt, os.path.basename(path), brand)
        if not hit:
            continue
        if hit.get("percent", -1) >= 0:
            result = dict(hit)
            result["age"] = max(0.0, now - mt)
            break
        if fallback is None:
            fallback = dict(hit)
            fallback["age"] = max(0.0, now - mt)
    if result is None:
        result = fallback
    if result is None or result.get("percent", -1) < 0:
        # 直连读不到时，改用「客户端目录整目录扫描」——和诊断同一套逻辑
        for brand in _MOUSE_APP_BRANDS:
            hit = _client_battery_from_disk(brand)
            if hit:
                result = hit
                break
    if result is None or result.get("percent", -1) < 0:
        # 再退一步：翻浏览器 profile（IndexedDB / Session Storage 也算）
        hit = _webcache_battery_from_disk(("mchose",))
        if hit:
            result = hit
    if result is not None and result.get("percent", -1) >= 0:
        _LAST_GOOD["mouse"] = dict(result)
    elif _LAST_GOOD["mouse"] is not None:
        # 这次没读到，沿用上一次成功的结果（界面会标注是多久之前的值）
        result = dict(_LAST_GOOD["mouse"])

    _mouse_cache["key"] = cache_key
    _mouse_cache["value"] = result
    _mouse_cache["ts"] = now
    return result


def _running_app_dirs() -> list[tuple[str, str]]:
    """正在运行的驱动客户端程序所在目录：[(目录, 品牌)]。

    用 ctypes 枚举进程取 exe 路径，比 tasklist 更能定位安装目录
    （例如 D:\\EWEADN-X87 Driver\\、C:\\Program Files\\Tonemay Gaming Headset\\）。
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    try:
        psapi = ctypes.windll.psapi
        enum_proc = psapi.EnumProcesses
    except AttributeError:
        enum_proc = kernel32.K32EnumProcesses
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    arr = (wintypes.DWORD * 4096)()
    got = wintypes.DWORD(0)
    if not enum_proc(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(got)):
        return []
    total = got.value
    out: list[tuple[str, str]] = []
    for pid in arr[:total]:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if not kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                      ctypes.byref(size)):
                continue
            exe = buf.value
        finally:
            kernel32.CloseHandle(h)
        low = exe.lower()
        brand = next((b for hint, b in _APP_DIR_HINTS if hint in low), None)
        if not brand:
            continue
        folder = os.path.dirname(exe)
        if folder and os.path.isdir(folder):
            out.append((folder, brand))
    # 去重
    seen: set[tuple[str, str]] = set()
    uniq: list[tuple[str, str]] = []
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        uniq.append(item)
    return uniq


def _installed_app_dirs() -> list[tuple[str, str]]:
    """注册表里记录的驱动客户端安装目录：[(目录, 品牌)]。"""
    import winreg

    out: list[tuple[str, str]] = []
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
            count = winreg.QueryInfoKey(parent)[0]
        except OSError:
            continue
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
            loc = value("InstallLocation")
            if not name or not loc:
                continue
            low = name.lower()
            brand = next((b for hint, b in _APP_DIR_HINTS if hint in low), None)
            if brand and os.path.isdir(loc):
                out.append((loc, brand))
    return out


def _client_battery_from_disk(brand: str) -> dict | None:
    """从驱动客户端目录（含所有子目录）里找最近的一条电量记录。

    与诊断扫描用的是同一套逻辑，保证「诊断里看得到 → 运行时也读得到」。
    """
    best: dict | None = None
    now = time.time()
    cached = _CLIENT_SCAN_CACHE.get(brand)
    if cached and now - cached[0] < 60:
        return dict(cached[1]) if cached[1] else None

    def _consider(dirs: list[tuple[str, str]]) -> None:
        nonlocal best
        for folder, b in dirs:
            if b != brand or not folder or not os.path.isdir(folder):
                continue
            try:
                hits = _scan_app_dir_files(folder)
            except Exception:
                continue
            for fields, rel, _stamp, _tags in hits:
                vals = dict(fields)
                if "电量" not in vals:
                    continue
                try:
                    pct = int(vals["电量"])
                except Exception:
                    continue
                if not 0 <= pct <= 100:
                    continue
                path = os.path.join(folder, rel)
                try:
                    mt = os.path.getmtime(path)
                except Exception:
                    mt = 0.0
                if best is not None and mt <= best["mtime"]:
                    continue
                raw_name = vals.get("设备名", "")
                latin = brand.split()[-1].upper()
                if raw_name and not raw_name.upper().startswith(latin[:4]):
                    raw_name = f"{brand.split()[0]} {raw_name}"
                charge_raw = str(vals.get("充电状态", "")).strip().lower()
                best = {
                    "percent": pct,
                    "charging": charge_raw in ("true", "1", "charging", "charge"),
                    "name": raw_name or brand.split()[0],
                    "file": rel,
                    "mtime": mt,
                }

    # 1) 客户端用户数据目录（%APPDATA%\<app>）
    data_dirs: list[tuple[str, str]] = []
    for d, b in _app_storage_dirs():
        data_dirs.append((os.path.dirname(os.path.dirname(d)), b))  # <app> 根目录
    _consider(data_dirs)
    # 2) 注册表记录的安装目录
    try:
        _consider(_installed_app_dirs())
    except Exception:
        pass
    # 3) 正在运行的客户端程序所在目录
    try:
        _consider(_running_app_dirs())
    except Exception:
        pass
    _CLIENT_SCAN_CACHE[brand] = (now, dict(best) if best else None)
    return best


# 品牌关键字：用于在浏览器 IndexedDB / Session Storage 里判断归属
_BRAND_TOKENS = ("aula", "mchose", "eweadn", "logitech", "sonix")

# 关键字 → 诊断里的品牌名
_TOKEN_TO_BRAND = {
    "aula": "狼蛛 AULA",
    "sonix": "狼蛛 AULA",
    "mchose": "迈从 MCHOSE",
    "eweadn": "前行者 EWEADN",
    "logitech": "罗技 Logitech",
}


def _profile_roots() -> list[str]:
    """Edge / Chrome 各 profile 的根目录（含 IndexedDB、Session Storage 等）。"""
    bases = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Edge\User Data"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\User Data"),
    ]
    roots: list[str] = []
    for base in bases:
        if not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            if name != "Default" and not name.startswith("Profile "):
                continue
            d = os.path.join(base, name)
            if os.path.isdir(d):
                roots.append(d)
    return roots


def _webcache_battery_from_disk(tokens: tuple[str, ...]) -> dict | None:
    """在浏览器 profile（含 IndexedDB / Session Storage）里找电量记录。

    网页驱动不一定写在 Local Storage，也可能落在 IndexedDB 或
    Session Storage，这里把整个 profile 目录翻一遍。
    """
    best: dict | None = None
    now = time.time()
    cached = _WEBCACHE_SCAN_CACHE.get(tokens)
    if cached and now - cached[0] < 60:
        return dict(cached[1]) if cached[1] else None
    for root in _profile_roots():
        try:
            hits = _scan_app_dir_files(root)
        except Exception:
            continue
        for fields, rel, _stamp, tags in hits:
            if not any(t in tags for t in tokens):
                continue
            vals = dict(fields)
            if "电量" not in vals:
                continue
            try:
                pct = int(vals["电量"])
            except Exception:
                continue
            if not 0 <= pct <= 100:
                continue
            path = os.path.join(root, rel)
            try:
                mt = os.path.getmtime(path)
            except Exception:
                mt = 0.0
            if best is not None and mt <= best["mtime"]:
                continue
            raw_name = vals.get("设备名", "")
            charge_raw = str(vals.get("充电状态", "")).strip().lower()
            best = {
                "percent": pct,
                "charging": charge_raw in ("true", "1", "charging", "charge"),
                "name": raw_name,
                "file": rel,
                "mtime": mt,
            }
    _WEBCACHE_SCAN_CACHE[tokens] = (now, dict(best) if best else None)
    return best


# 最后一次成功读到的电量：即使这次扫描失败也继续显示（标注是旧值）
_LAST_GOOD: dict = {"keyboard": None, "mouse": None}

# 客户端目录整盘扫描的结果缓存（60 秒），避免每次轮询都扫一遍大目录
_CLIENT_SCAN_CACHE: dict = {}

# 浏览器 profile 整盘扫描的结果缓存（60 秒）
_WEBCACHE_SCAN_CACHE: dict = {}


def clear_cache() -> None:
    """清空网页电量缓存（不删除浏览器 Local Storage）。下次读取会重新扫描。"""
    _cache["key"] = None
    _cache["value"] = None
    _cache["ts"] = 0.0
    _mouse_cache["key"] = None
    _mouse_cache["value"] = None
    _mouse_cache["ts"] = 0.0


# ---------------------------------------------------------------------------
# 脱敏诊断：只提取设备相关字段，生成可安全外发的文本
# ---------------------------------------------------------------------------
# 只认这些品牌域名，避免把无关网站的数据带出来
_DIAG_DOMAINS = (
    ("狼蛛 AULA", re.compile(r"aulacn\.com", re.I)),
    ("迈从 MCHOSE", re.compile(r"mchose\.com\.cn", re.I)),
    ("前行者 EWEADN", re.compile(r"eweadn\.cn", re.I)),
    ("ATK", re.compile(r"atk\.pro", re.I)),
    ("罗技 Logitech", re.compile(r"logitechg\.com", re.I)),
)
# 白名单字段：只取设备名/电量/充电/型号标识，token、账号、Cookie 一律不读
_DIAG_FIELDS = (
    ("设备名", re.compile(r'"(?:productName|deviceName|pName|customName|productModel|modelName)"\s*:\s*"([^"]{1,64})"')),
    ("电量", re.compile(r'"(?:batteryLevel|batteryPercent|battery)"\s*:\s*"?(\d{1,3})(?:\.\d+)?%?"?')),
    ("充电状态", re.compile(r'"(?:isCharging|chargeStatus)"\s*:\s*"?([^",}\s]{1,12})')),
    ("型号标识", re.compile(r'"(?:deviceId|productId|vendorId|pId|vId)"\s*:\s*"?(\d{1,8})"?')),
)


def _diag_clean(label: str, value: str) -> str:
    """过滤 leveldb 碎片带来的无效值，返回空串表示丢弃。"""
    value = value.strip()
    if not value:
        return ""
    # 去掉控制字符
    value = "".join(ch for ch in value if ch.isprintable()).strip()
    if label == "电量":
        try:
            n = int(float(value.replace("%", "")))
        except Exception:
            return ""
        return str(n) if 0 <= n <= 100 else ""
    if label == "型号标识":
        return value if value.isdigit() and 3 <= len(value) <= 8 else ""
    if label == "充电状态":
        low = value.lower()
        if low in ("true", "false", "0", "1", "charging", "discharging",
                   "charge", "full", "none"):
            return value
        return ""
    # 设备名：至少 2 个字符，且不能是纯符号
    if len(value) < 2:
        return ""
    if not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in value):
        return ""
    return value


def _scan_app_dir_files(app_dir: str) -> list[tuple[tuple[tuple[str, str], ...], str, str, tuple[str, ...]]]:
    """递归扫描驱动客户端目录里的所有文件，找设备/电量字段。

    实时数据可能不在 Local Storage（在 IndexedDB、Session Storage、日志、
    甚至 SQLite 里），所以这里把整个应用目录翻一遍。

    返回 [(字段元组, 相对路径, 时间戳, 命中的品牌关键字)]。
    """
    skip_dirs = {"cache", "code cache", "gpucache", "dawnwebgpucache",
                 "dawngraphitecache", "shadercache", "crashpad",
                 "blob_storage", "service worker", "network"}
    max_bytes = 4 * 1024 * 1024
    max_files = 400
    out: list[tuple[tuple[tuple[str, str], ...], str, str, tuple[str, ...]]] = []
    seen_files = 0
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]
        for name in files:
            seen_files += 1
            if seen_files > max_files:
                return out
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > max_bytes:
                    continue
                with open(path, "rb") as f:
                    raw = f.read(max_bytes)
            except Exception:
                continue
            if not raw:
                continue
            text = raw.decode("utf-8", "ignore")
            if "battery" not in text.lower() and "charge" not in text.lower():
                # 只在没有 utf-8 结果时再试 utf-16（有些 Electron 应用会写）
                text = raw.decode("utf-16-le", "ignore")
            fields: list[tuple[str, str]] = []
            for label, rx in _DIAG_FIELDS:
                for m in rx.finditer(text):
                    val = _diag_clean(label, m.group(1))
                    if val and (label, val) not in fields:
                        fields.append((label, val))
            if fields:
                try:
                    stamp = time.strftime("%m-%d %H:%M",
                                         time.localtime(os.path.getmtime(path)))
                except Exception:
                    stamp = "?"
                rel = os.path.relpath(path, app_dir)
                low = text.lower()
                tags = tuple(t for t in _BRAND_TOKENS if t in low)
                out.append((tuple(sorted(fields)), rel, stamp, tags))
    return out


def collect_diagnostics(max_files: int = 12) -> str:
    """扫描浏览器缓存，生成**脱敏**的诊断文本。

    只输出白名单字段（设备名、电量、充电状态、型号标识），不包含账号、
    Cookie、token、历史记录等任何隐私数据，可直接发给他人分析。
    """
    browser_dirs = _profile_dirs()
    app_dirs = _app_storage_dirs()
    out: list[str] = []
    out.append("浏览器缓存目录数: %d" % len(browser_dirs))
    for d in browser_dirs:
        out.append("  " + d)
    out.append("驱动客户端缓存目录数: %d" % len(app_dirs))
    for d, brand in app_dirs:
        out.append(f"  [{brand}] {d}")
    if not browser_dirs and not app_dirs:
        out.append("  （未找到 Edge / Chrome 或驱动客户端的 Local Storage 目录）")
        return "\n".join(out)

    # 品牌 -> 记录列表；每条记录 = 同一段缓存里抽出的字段
    records: dict[str, list[tuple[tuple[tuple[str, str], ...], str, str]]] = {
        name: [] for name, _ in _DIAG_DOMAINS
    }
    # (目录, 强制品牌)：浏览器目录按域名判断，客户端目录直接归属其品牌
    sources: list[tuple[str, str | None]] = [(d, None) for d in browser_dirs]
    sources.extend(app_dirs)
    scanned = 0
    for d, forced in sources:
        try:
            names = [n for n in os.listdir(d) if n.endswith((".log", ".ldb"))]
        except Exception:
            continue
        paths = sorted((os.path.join(d, n) for n in names),
                       key=lambda p: os.path.getmtime(p), reverse=True)[:max_files]
        for path in paths:
            text = _read_text(path)
            if not text:
                continue
            scanned += 1
            fname = os.path.basename(path)
            try:
                stamp = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
            except Exception:
                stamp = "?"
            # 同时兼容 https:// 与客户端自定义 scheme（如 aula://dist）
            for seg in re.split(r"_[0-9]?[a-zA-Z][a-zA-Z0-9+.\-]*://", text):
                head = seg[:120]
                if forced:
                    matched_brands = [forced]
                else:
                    matched_brands = [b for b, pat in _DIAG_DOMAINS
                                      if pat.search(head)]
                for brand in matched_brands:
                    if brand not in records:
                        continue
                    fields: list[tuple[str, str]] = []
                    for label, rx in _DIAG_FIELDS:
                        for m in rx.finditer(seg):
                            val = _diag_clean(label, m.group(1))
                            if val and (label, val) not in fields:
                                fields.append((label, val))
                    if fields:
                        records[brand].append((tuple(sorted(fields)), fname, stamp))

    # 客户端目录再全量扫一遍（实时数据可能不在 leveldb 里）
    for d, brand in app_dirs:
        if brand not in records:
            continue
        app_root = os.path.dirname(os.path.dirname(d))  # <app> 根目录
        for fields, rel, stamp, _tags in _scan_app_dir_files(app_root):
            records[brand].append((fields, rel, stamp))

    # 浏览器 profile 也整体扫一遍（IndexedDB / Session Storage 里的数据）
    for root in _profile_roots():
        for fields, rel, stamp, tags in _scan_app_dir_files(root):
            for token in tags:
                brand = _TOKEN_TO_BRAND.get(token)
                if not brand or brand not in records:
                    continue
                records[brand].append((fields, rel, stamp))
                break

    out.append("实际扫描文件数: %d" % scanned)
    out.append("")
    found_any = False
    for brand, _ in _DIAG_DOMAINS:
        rows = records[brand]
        if not rows:
            continue
        found_any = True
        # 同一记录可能在多个文件里重复出现，按字段内容去重
        seen = set()
        unique = []
        for fields, fname, stamp in rows:
            key = tuple(fields)
            if key in seen:
                continue
            seen.add(key)
            unique.append((fields, fname, stamp))
        out.append(f"[{brand}] 共 {len(unique)} 条记录")
        for idx, (fields, fname, stamp) in enumerate(unique[:6], 1):
            out.append(f"  记录 {idx}（{fname}  {stamp}）")
            for label, val in fields:
                out.append(f"    {label}: {val}")
        out.append("")
    if not found_any:
        out.append("未发现上述品牌的可识别字段。")
        out.append("请先打开对应品牌的网页驱动或 HUB 客户端，连接设备后再导出。")
        out.append("")

    out.append("-" * 46)
    out.append("说明：本文件仅包含设备名、电量、充电状态、型号标识，")
    out.append("不含账号、密码、Cookie、浏览记录等隐私信息。")
    return "\n".join(out)


if __name__ == "__main__":
    info = read_keyboard_battery(force=True)
    if not info:
        print("未找到狼蛛设备缓存（请先在浏览器打开 https://hub.aulacn.com 并连接设备）")
    else:
        print("键盘电量:", info["percent"], "%")
        print("  型号:", info["name"] or "?")
        print("  固件:", info["version"] or "?")
        print("  充电中:", "是" if info["charging"] else "否")
        print("  缓存距今: %.0f 秒" % info["age"])

    mouse = read_mouse_battery(force=True)
    if not mouse:
        print("未找到鼠标网页电量（请在迈从驱动里点击连接设备，保持页面打开）")
    else:
        print("鼠标电量:", mouse["percent"], "%")
        print("  型号:", mouse["name"] or "?")
        print("  充电中:", "是" if mouse["charging"] else "否")
        print("  缓存距今: %.0f 秒" % mouse["age"])
