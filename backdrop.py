"""本地图片 / 视频背景：解码、铺满裁剪、压暗、圆角卡片合成。

Windows 上 OpenCV 的 imread 打不开中文路径，统一走 np.fromfile + imdecode；
视频若路径含非 ASCII 字符，先复制一份到临时文件再打开。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from collections import deque

import tkinter as tk

try:
    import cv2
    import numpy as np
    _HAS_CV2 = True
except Exception:          # 没装解码库时静默降级为“无背景”
    cv2 = None
    np = None
    _HAS_CV2 = False


VIDEO_EXTS = {".mp4", ".webm", ".avi", ".mov", ".mkv"}
GIF_EXTS = {".gif"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def hex_to_bgr(color: str):
    """#rrggbb -> BGR 三元组。"""
    c = color.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return (b, g, r)


def _imread(path: str):
    """读图片（兼容中文 / 特殊字符路径）。"""
    if not _HAS_CV2:
        return None
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def _open_video(path: str):
    """打开视频 / 动态 GIF；中文路径先复制到临时文件。返回 (cap, 临时文件路径)。"""
    if not _HAS_CV2:
        return None, ""
    try:
        cap = cv2.VideoCapture(path)
        if cap.isOpened():
            return cap, ""
        try:
            path.encode("ascii")
        except UnicodeEncodeError:
            ext = os.path.splitext(path)[1] or ".mp4"
            tmp = os.path.join(tempfile.gettempdir(),
                               "logi_bg_%d%s" % (os.getpid(), ext))
            try:
                shutil.copyfile(path, tmp)
            except Exception:
                return None, ""
            cap = cv2.VideoCapture(tmp)
            if cap.isOpened():
                return cap, tmp
            try:
                os.remove(tmp)
            except Exception:
                pass
    except Exception:
        pass
    return None, ""


def rounded_mask(w: int, h: int, r: int, inset: int = 0):
    """圆角矩形遮罩（白=保留）。inset 为内缩边距。"""
    m = np.zeros((h, w), dtype=np.uint8)
    x0, y0, x1, y1 = inset, inset, w - inset, h - inset
    if x1 <= x0 or y1 <= y0:
        return m
    r = max(0, min(r, (x1 - x0) // 2, (y1 - y0) // 2))
    if r == 0:
        m[y0:y1, x0:x1] = 255
        return m
    cv2.rectangle(m, (x0 + r, y0), (x1 - r, y1), 255, -1)
    cv2.rectangle(m, (x0, y0 + r), (x1, y1 - r), 255, -1)
    for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r),
                   (x0 + r, y1 - r), (x1 - r, y1 - r)):
        cv2.circle(m, (cx, cy), r, 255, -1)
    return m


def _rgb_ppm(frame):
    """numpy BGR -> 连续 RGB 字节 + PPM 头。"""
    if frame is None or not _HAS_CV2:
        return None, None, None
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    return rgb, b"P6\n%d %d\n255\n" % (w, h) + rgb.tobytes(), (w, h)


def to_photo(frame):
    """numpy BGR -> 新建 Tk PhotoImage（PPM）。"""
    _, ppm, _ = _rgb_ppm(frame)
    if ppm is None:
        return None
    return tk.PhotoImage(data=ppm, format="ppm")


def put_photo(photo, frame):
    """numpy BGR -> 写入已有 PhotoImage（尺寸必须一致），返回该对象。"""
    _, ppm, size = _rgb_ppm(frame)
    if ppm is None or photo is None or size is None:
        return None
    try:
        if photo.width() != size[0] or photo.height() != size[1]:
            return None
        photo.put(ppm)
        return photo
    except Exception:
        return None


class Backdrop:
    """一个本地图片或视频背景源。"""

    def __init__(self) -> None:
        self.path = ""
        self.kind = ""          # "image" / "video"
        self._frame = None       # 当前帧（BGR）
        self._cap = None
        self._temp = ""
        self._scaled = None      # 缩放缓存
        self._scaled_key = None
        self.fps = 0.0           # 源文件的帧率（GIF 也适用）
        # 每次换素材/清空自增，用于让上层的渲染缓存失效
        self.generation = 0
        # 高帧率视频：后台预解码线程。4K 120fps 单帧解码 ~6ms，放在主线程
        # 会直接吃掉 8.3ms 的帧预算；后台边播边解，主线程只管缩放上屏。
        self._prefetch: deque | None = None
        self._prefetch_stop: threading.Event | None = None
        self._prefetch_thread: threading.Thread | None = None
        # 大图缩到小窗口（悬浮窗只有 375px 宽）必然发糊，
        # 缩放后补一道锐化（Unsharp Mask）把细节找回来。
        self.sharpen = 0.7

    # ---------- 加载 / 释放 ----------
    def load(self, path: str) -> bool:
        """加载本地图片或视频；成功返回 True（失败时保持原状）。"""
        if not path or not os.path.isfile(path) or not _HAS_CV2:
            return False
        ext = os.path.splitext(path)[1].lower()
        if ext in VIDEO_EXTS or ext in GIF_EXTS:
            cap, tmp = _open_video(path)
            if cap is None:
                # 有些 GIF 解码不了，退回静态图方式
                frame = _imread(path)
                if frame is None:
                    return False
                self.clear()
                self._frame = frame
                self.kind, self.path = "image", path
                self.generation += 1
                return True
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                if tmp:
                    _silent_remove(tmp)
                return False
            self.clear()
            self._cap, self._temp, self._frame = cap, tmp, frame
            self.kind, self.path = "video", path
            self.generation += 1
            try:
                fps = float(cap.get(cv2.CAP_PROP_FPS))
                if 0 < fps <= 240:
                    self.fps = fps
            except Exception:
                pass
            # 高帧率视频开预解码；低帧率 GIF/视频没必要，避免多线程开销
            if self.fps >= 50.0:
                self._start_prefetch()
            return True
        if ext in IMAGE_EXTS:
            frame = _imread(path)
            if frame is None:
                return False
            self.clear()
            self._frame = frame
            self.kind, self.path = "image", path
            self.generation += 1
            return True
        return False

    def _start_prefetch(self) -> None:
        """后台预解帧：解码线程只碰 cap，主线程只取 numpy 帧。"""
        if self._cap is None or self._prefetch_thread is not None:
            return
        self._prefetch = deque(maxlen=12)
        self._prefetch_stop = threading.Event()
        cap = self._cap
        queue = self._prefetch
        stop = self._prefetch_stop

        def worker():
            while not stop.is_set():
                try:
                    if len(queue) >= queue.maxlen:
                        time.sleep(0.001)
                        continue
                    ok, frame = cap.read()
                    if not ok:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    queue.append(frame)
                except Exception:
                    break

        self._prefetch_thread = threading.Thread(
            target=worker, name="logi-bg-decode", daemon=True)
        self._prefetch_thread.start()

    def _stop_prefetch(self) -> None:
        """停止预解码；只在 clear/换素材时调用，避免和 cap.read 抢同一对象。"""
        stop = self._prefetch_stop
        thread = self._prefetch_thread
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        self._prefetch = None
        self._prefetch_stop = None
        self._prefetch_thread = None

    def clear(self) -> None:
        self._stop_prefetch()
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None
        if self._temp:
            _silent_remove(self._temp)
        self._temp = ""
        self._frame = None
        self._scaled = None
        self._scaled_key = None
        self.fps = 0.0
        self.generation += 1
        self.kind = ""
        self.path = ""

    def clear_cache(self) -> None:
        """清掉可重建的缓存，不卸载当前素材、不删用户背景文件。

        - _scaled：缩放 / 锐化 / 压暗 / 色纱的合成结果，下一帧会重算；
        - 预解帧队列：清掉后后台会继续解码，只是短时没有预读缓冲；
        - 临时视频副本由 clean_temp_copies() 单独处理（当前正在用的跳过）。
        """
        self._scaled = None
        self._scaled_key = None
        if self._prefetch is not None:
            try:
                self._prefetch.clear()
            except Exception:
                pass

    def clean_temp_copies(self) -> int:
        """删除历史遗留的中文路径临时视频副本，返回删除个数。

        只碰本程序生成、命名固定的 `logi_bg_<pid><ext>`，且跳过当前正在
        使用的那一个；不会碰用户背景文件，也不会碰浏览器数据。
        """
        removed = 0
        current = os.path.normcase(os.path.abspath(self._temp)) if self._temp else ""
        try:
            folder = tempfile.gettempdir()
            names = os.listdir(folder)
        except Exception:
            return 0
        for name in names:
            if not name.startswith("logi_bg_"):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in VIDEO_EXTS and ext not in GIF_EXTS:
                continue
            path = os.path.join(folder, name)
            if current and os.path.normcase(os.path.abspath(path)) == current:
                continue
            try:
                os.remove(path)
                removed += 1
            except Exception:
                pass
        return removed

    def is_video(self) -> bool:
        return self.kind == "video"

    def pause(self) -> None:
        """窗口不可见时停掉后台预解码，避免两个动图同时吃 CPU。"""
        if self._prefetch_thread is not None:
            self._stop_prefetch()

    def resume(self) -> None:
        """窗口重新可见时恢复高帧率视频的后台预解码。"""
        if (self.is_video() and self.fps >= 50.0
                and self._prefetch_thread is None):
            self._start_prefetch()

    def has_media(self) -> bool:
        return self._frame is not None

    # ---------- 帧推进 ----------
    def advance(self) -> bool:
        """视频推进一帧（到末尾自动循环）。"""
        if self._cap is None:
            return False
        if self._prefetch is not None:
            try:
                self._frame = self._prefetch.popleft()
                self._scaled = None
                self._scaled_key = None
                return True
            except IndexError:
                # 后台还没解出来，宁可跳过这一帧，也不要卡住主线程
                return False
        try:
            ok, frame = self._cap.read()
            if not ok or frame is None:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._cap.read()
            if not ok or frame is None:
                return False
        except Exception:
            return False
        self._frame = frame
        self._scaled = None
        self._scaled_key = None
        return True

    # ---------- 渲染 ----------
    def fitted(self, w: int, h: int, shade=None):
        """按目标尺寸等比铺满并居中裁剪（带缩放 + 调色缓存）。

        缩放、锐化、压暗/提亮、色纱都只跟源图和参数有关，与裁剪位置无关，
        放在缓存里做一次即可；每帧只剩"裁剪 + 转 Tk 图片"。
        shade = (dark, white, veil, veil_bgr)
        """
        if self._frame is None or w <= 0 or h <= 0:
            return None
        dark, white, veil, veil_bgr = shade or (1.0, 0.0, 0.0, None)
        key = (w, h, round(self.sharpen, 2), round(dark, 3), round(white, 3),
               round(veil, 3), None if not veil_bgr else tuple(veil_bgr))
        if self._scaled_key != key or self._scaled is None:
            ih, iw = self._frame.shape[:2]
            scale = max(w / float(iw), h / float(ih))
            nw = max(w, int(round(iw * scale)))
            nh = max(h, int(round(ih * scale)))
            # 高质量 INTER_AREA 缩 4K→小窗要 8~14ms，120fps 时单这一步就超预算。
            # 高帧率视频改用 LINEAR（0.6ms）+ 后面的 Unsharp 锐化补偿清晰度；
            # 静态图 / 低帧率 GIF 仍用 INTER_AREA，保留原来的细节质量。
            if scale < 1 and not (self.kind == "video" and self.fps >= 50.0):
                interp = cv2.INTER_AREA
            else:
                interp = cv2.INTER_LINEAR
            scaled = cv2.resize(self._frame, (nw, nh), interpolation=interp)
            # 缩放后锐化：小窗口下不发糊
            if self.sharpen > 0:
                try:
                    soft = cv2.GaussianBlur(scaled, (0, 0), 1.0)
                    scaled = cv2.addWeighted(scaled, 1.0 + self.sharpen,
                                             soft, -self.sharpen, 0)
                except Exception:
                    pass
            if dark and dark != 1.0:
                scaled = cv2.convertScaleAbs(scaled, alpha=dark, beta=0)
            if white:
                scaled = cv2.addWeighted(scaled, 1.0 - white,
                                         np.full_like(scaled, 255), white, 0)
            if veil and veil > 0:
                color = veil_bgr if veil_bgr else (0, 0, 0)
                scaled = cv2.addWeighted(scaled, 1.0 - veil,
                                         np.full_like(scaled, color), veil, 0)
            self._scaled = scaled
            self._scaled_key = key
        s = self._scaled
        x = max(0, (s.shape[1] - w) // 2)
        y = max(0, (s.shape[0] - h) // 2)
        return s[y:y + h, x:x + w]

    def render(self, w: int, h: int, dark: float = 0.45, white: float = 0.0,
               radius: int = 0, inset: int = 0, key_bgr=None, blur: float = 0.0,
               veil: float = 0.0, veil_bgr=None):
        """渲染成目标尺寸的画面。

        dark<1 压暗（深色主题），white>0 提亮（浅色主题）；
        veil>0 叠一层半透明色纱，保证文字可读；
        radius>0 时输出圆角卡片，四角填 key_bgr（透明色）。
        """
        f = self.fitted(w, h, (dark, white, veil, veil_bgr))
        if f is None:
            return None
        if blur and blur > 0:
            k = int(max(3, round(blur * 4)) | 1)
            f = cv2.GaussianBlur(f, (k, k), blur)
        if radius > 0:
            mask = rounded_mask(w, h, radius, inset)
            out = np.empty_like(f)
            out[:] = key_bgr if key_bgr else (0, 0, 0)
            sel = mask > 0
            out[sel] = f[sel]
            f = out
        return f


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except Exception:
        pass
