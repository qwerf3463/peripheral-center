"""用户配置持久化（settings.json）。

配置写在 exe（或项目）所在目录，用户可读可改。
写失败不应影响程序运行，因此所有操作都吞掉异常并回退到默认值。
"""
from __future__ import annotations

import json
import os

import paths

# 退出方式
EXIT_TRAY = "tray"        # 托管在后台：关窗口后程序继续在托盘运行
EXIT_QUIT = "quit"        # 退出主程序：关窗口即完全退出

EXIT_LABELS = {
    EXIT_TRAY: "托管在后台",
    EXIT_QUIT: "退出主程序",
}

# 显示范围：悬浮窗的层级
SCOPE_DESKTOP = "desktop"        # 只在桌面显示（非置顶，会被其他应用遮挡）
SCOPE_FULLSCREEN = "fullscreen"  # 覆盖任何应用（置顶）

SCOPE_LABELS = {
    SCOPE_DESKTOP: "桌面",
    SCOPE_FULLSCREEN: "全屏",
}

# 后台运行时悬浮窗是否显示（仅退出方式=托管在后台时生效）
FLOAT_BG_YES = "yes"      # 是：托管后台后悬浮窗继续常驻显示
FLOAT_BG_NO = "no"        # 否：托管后台后隐藏所有窗口，只留托盘

FLOAT_BG_LABELS = {
    FLOAT_BG_YES: "是",
    FLOAT_BG_NO: "否",
}

# 界面主题
THEME_DARK = "dark"       # 深色主题（默认）
THEME_LIGHT = "light"     # 浅色主题（全白）

THEME_LABELS = {
    THEME_DARK: "深色模式",
    THEME_LIGHT: "浅色模式",
}

DEFAULTS = {
    "exit_mode": EXIT_TRAY,
    "float_alpha": 0.88,   # 悬浮窗透明度（0.2~1.0，越大越不透明）
    "float_pos": None,     # 悬浮窗上次位置 [x, y]，None 表示首次居中
    "float_size": None,    # 悬浮窗上次尺寸 [w, h]，None 表示用默认尺寸
    "display_scope": SCOPE_DESKTOP,  # 显示范围：桌面 / 全屏
    "float_on_bg": FLOAT_BG_YES,     # 后台运行时悬浮窗是否显示
    "theme": THEME_DARK,             # 界面主题：深色 / 浅色（全白）
    # 背景：大界面和悬浮窗各自独立（旧版只有一个 bg_media，load 时迁移）
    "bg_media": "",                  # 旧配置兼容字段，新配置不再使用
    "bg_blur": 1.0,                  # 旧配置兼容字段
    "bg_sharpen": 0.7,               # 旧配置兼容字段
    "bg_media_main": "",             # 大界面背景（图片/视频/GIF 本地路径）
    "bg_media_float": "",            # 悬浮窗背景
    "bg_blur_main": 1.0,             # 大界面背景模糊（0~8）
    "bg_blur_float": 1.0,            # 悬浮窗背景模糊
    "bg_sharpen_main": 0.7,          # 大界面背景锐化（0~1.5）
    "bg_sharpen_float": 0.7,         # 悬浮窗背景锐化
    "float_show_text": "yes",       # 悬浮窗是否显示文字（电量/设备名），no=纯背景
    "kb_brand": "狼蛛",              # 当前选中的键盘品牌（对应网页驱动）
    "mouse_brand": "狼蛛",           # 当前选中的鼠标品牌（对应网页驱动）
}

FLOAT_TEXT_YES = "yes"
FLOAT_TEXT_NO = "no"
FLOAT_TEXT_LABELS = {FLOAT_TEXT_YES: "显示", FLOAT_TEXT_NO: "隐藏"}

# 背景可以分别设置的目标
BG_TARGETS = ("main", "float")
BG_TARGET_LABELS = {"main": "大界面", "float": "悬浮窗"}


def _bg_target(target: str) -> str:
    return target if target in BG_TARGETS else "main"

# 透明度范围
ALPHA_MIN = 0.2
ALPHA_MAX = 1.0
ALPHA_DEFAULT = 0.88


def settings_path() -> str:
    return paths.writable_resource("settings.json")


# 读取缓存：动图播放时每帧都会读配置，磁盘 I/O 会拖垮帧率。
# 用文件修改时间判断是否需要重新读盘。
_CACHE: dict = {"data": None, "mtime": -1.0}


def load() -> dict:
    """读取配置，失败或缺字段时用默认值补齐。"""
    path = settings_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = -1.0
    if _CACHE["data"] is not None and _CACHE["mtime"] == mtime:
        return dict(_CACHE["data"])
    data = dict(DEFAULTS)
    loaded = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            for key in DEFAULTS:
                if key in loaded:
                    data[key] = loaded[key]
    except Exception:
        pass
    # 兜底：exit_mode 只接受已知取值
    if data.get("exit_mode") not in EXIT_LABELS:
        data["exit_mode"] = DEFAULTS["exit_mode"]
    # 兜底：display_scope 只接受已知取值
    if data.get("display_scope") not in SCOPE_LABELS:
        data["display_scope"] = DEFAULTS["display_scope"]
    # 兜底：float_on_bg 只接受已知取值
    if data.get("float_on_bg") not in FLOAT_BG_LABELS:
        data["float_on_bg"] = DEFAULTS["float_on_bg"]
    # 兜底：theme 只接受已知取值（兼容旧配置里的数字 0/1）
    if data.get("theme") not in THEME_LABELS:
        data["theme"] = DEFAULTS["theme"]
    # 兜底：bg_media 必须是字符串
    if not isinstance(data.get("bg_media"), str):
        data["bg_media"] = ""
    # 兜底：bg_blur 必须是合法数值且落在范围内
    try:
        blur = float(data.get("bg_blur", 1.0))
    except (TypeError, ValueError):
        blur = 1.0
    if not (0.0 <= blur <= 8.0):
        blur = 1.0
    data["bg_blur"] = blur
    # 兜底：bg_sharpen 必须是合法数值且落在范围内
    try:
        sharp = float(data.get("bg_sharpen", 0.7))
    except (TypeError, ValueError):
        sharp = 0.7
    if not (0.0 <= sharp <= 1.5):
        sharp = 0.7
    data["bg_sharpen"] = sharp
    # 旧配置迁移：以前只有一套 bg_*，两个界面共用；首次读取时复制到各自字段，
    # 之后用户就可以分别设置不同背景，老用户不会突然丢背景。
    for _t in BG_TARGETS:
        _media_key = "bg_media_" + _t
        _blur_key = "bg_blur_" + _t
        _sharp_key = "bg_sharpen_" + _t
        if _media_key not in loaded:
            data[_media_key] = data["bg_media"]
        if _blur_key not in loaded:
            data[_blur_key] = data["bg_blur"]
        if _sharp_key not in loaded:
            data[_sharp_key] = data["bg_sharpen"]
    # 兜底：新字段也必须合法
    for _t in BG_TARGETS:
        _media_key = "bg_media_" + _t
        _blur_key = "bg_blur_" + _t
        _sharp_key = "bg_sharpen_" + _t
        if not isinstance(data.get(_media_key), str):
            data[_media_key] = ""
        try:
            _blur = float(data.get(_blur_key, 1.0))
        except (TypeError, ValueError):
            _blur = 1.0
        data[_blur_key] = min(8.0, max(0.0, _blur)) if 0.0 <= _blur <= 8.0 else 1.0
        try:
            _sharp = float(data.get(_sharp_key, 0.7))
        except (TypeError, ValueError):
            _sharp = 0.7
        data[_sharp_key] = min(1.5, max(0.0, _sharp)) if 0.0 <= _sharp <= 1.5 else 0.7
    # 兜底：float_alpha 必须是合法数值且落在范围内
    try:
        alpha = float(data.get("float_alpha", ALPHA_DEFAULT))
    except (TypeError, ValueError):
        alpha = ALPHA_DEFAULT
    if not (ALPHA_MIN <= alpha <= ALPHA_MAX):
        alpha = ALPHA_DEFAULT
    data["float_alpha"] = alpha
    # 兜底：float_show_text 只接受已知取值
    if data.get("float_show_text") not in FLOAT_TEXT_LABELS:
        data["float_show_text"] = DEFAULTS["float_show_text"]
    _CACHE["data"] = dict(data)
    _CACHE["mtime"] = mtime
    return data


def clear_cache() -> None:
    """清空配置读缓存（不删除 settings.json）。下次 load() 会重新读盘。"""
    _CACHE["data"] = None
    _CACHE["mtime"] = -1.0


def save(data: dict) -> bool:
    """保存配置，返回是否成功。"""
    try:
        path = settings_path()
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 更新缓存，避免下一帧又去读盘
        try:
            _CACHE["data"] = dict(data)
            _CACHE["mtime"] = os.path.getmtime(path)
        except OSError:
            _CACHE["mtime"] = -1.0
        return True
    except Exception:
        return False


def get_exit_mode() -> str:
    return load().get("exit_mode", EXIT_TRAY)


def set_exit_mode(mode: str) -> bool:
    if mode not in EXIT_LABELS:
        return False
    data = load()
    data["exit_mode"] = mode
    return save(data)


def get_display_scope() -> str:
    return load().get("display_scope", DEFAULTS["display_scope"])


def set_display_scope(scope: str) -> bool:
    if scope not in SCOPE_LABELS:
        return False
    data = load()
    data["display_scope"] = scope
    return save(data)


def get_float_on_bg() -> bool:
    """后台运行时是否显示悬浮窗（是/否）。"""
    return load().get("float_on_bg", FLOAT_BG_YES) == FLOAT_BG_YES


def set_float_on_bg(value: str) -> bool:
    if value not in FLOAT_BG_LABELS:
        return False
    data = load()
    data["float_on_bg"] = value
    return save(data)


def get_theme() -> str:
    return load().get("theme", DEFAULTS["theme"])


def set_theme(theme: str) -> bool:
    if theme not in THEME_LABELS:
        return False
    data = load()
    data["theme"] = theme
    return save(data)


def get_bg_media(target: str = "main") -> str:
    """指定目标（main/float）的背景路径，空字符串表示无背景。"""
    return load().get("bg_media_" + _bg_target(target), "")


def set_bg_media(path: str, target: str = "main") -> bool:
    data = load()
    data["bg_media_" + _bg_target(target)] = path if isinstance(path, str) else ""
    return save(data)


def get_bg_blur(target: str = "main") -> float:
    """指定目标的背景模糊半径（0~8）。"""
    return float(load().get("bg_blur_" + _bg_target(target), 1.0))


def set_bg_blur(value: float, target: str = "main") -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    v = min(8.0, max(0.0, v))
    data = load()
    data["bg_blur_" + _bg_target(target)] = v
    return save(data)


def get_bg_sharpen(target: str = "main") -> float:
    """指定目标的背景锐化强度（0~1.5）。"""
    return float(load().get("bg_sharpen_" + _bg_target(target), 0.7))


def set_bg_sharpen(value: float, target: str = "main") -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    v = min(1.5, max(0.0, v))
    data = load()
    data["bg_sharpen_" + _bg_target(target)] = v
    return save(data)


def get_float_alpha() -> float:
    return float(load().get("float_alpha", ALPHA_DEFAULT))


def set_float_alpha(alpha: float) -> bool:
    try:
        a = float(alpha)
    except (TypeError, ValueError):
        return False
    a = min(ALPHA_MAX, max(ALPHA_MIN, a))
    data = load()
    data["float_alpha"] = a
    return save(data)


def get_float_show_text() -> bool:
    """悬浮窗是否显示文字（电量/设备名）。"""
    return load().get("float_show_text", FLOAT_TEXT_YES) == FLOAT_TEXT_YES


def set_float_show_text(value: str) -> bool:
    if value not in FLOAT_TEXT_LABELS:
        return False
    data = load()
    data["float_show_text"] = value
    return save(data)


def get_kb_brand() -> str:
    """当前选中的键盘品牌。"""
    return load().get("kb_brand", DEFAULTS["kb_brand"])


def set_kb_brand(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    data = load()
    data["kb_brand"] = value
    return save(data)


def get_mouse_brand() -> str:
    """当前选中的鼠标品牌。"""
    return load().get("mouse_brand", DEFAULTS["mouse_brand"])


def set_mouse_brand(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    data = load()
    data["mouse_brand"] = value
    return save(data)


def get_float_pos():
    """读取悬浮窗上次位置，返回 (x, y) 或 None（首次/非法值则居中）。"""
    try:
        pos = load().get("float_pos")
        if (isinstance(pos, (list, tuple)) and len(pos) == 2
                and isinstance(pos[0], int) and isinstance(pos[1], int)):
            return (pos[0], pos[1])
    except Exception:
        pass
    return None


def set_float_pos(x: int, y: int) -> bool:
    try:
        data = load()
        data["float_pos"] = [int(x), int(y)]
        return save(data)
    except (TypeError, ValueError):
        return False


def get_float_size():
    """读取悬浮窗上次尺寸，返回 (w, h) 或 None（首次用默认尺寸）。"""
    try:
        size = load().get("float_size")
        if (isinstance(size, (list, tuple)) and len(size) == 2
                and isinstance(size[0], int) and isinstance(size[1], int)):
            return (size[0], size[1])
    except Exception:
        pass
    return None


def set_float_size(w: int, h: int) -> bool:
    try:
        data = load()
        data["float_size"] = [int(w), int(h)]
        return save(data)
    except (TypeError, ValueError):
        return False
