"""打包成单文件 exe（无控制台窗口）。

用法：
    D:/python/python.exe build_exe.py

产物：
    dist/外设控制中心.exe   —— 双击即运行，无黑框，无需安装 Python
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(APP_DIR, "dist")
BUILD = os.path.join(APP_DIR, "build")
EXE_NAME = "外设控制中心"
ICON = os.path.join(APP_DIR, "app.ico")

# 打包后是否自动覆盖「下载」文件夹里的 exe。
# 用户要求先不覆盖：下载文件夹那份可能正在用于分发，保持原样。
COPY_TO_DOWNLOADS = False
# 打包后是否自动复制到 D 盘交付目录（存在才复制）
COPY_TO_DELIVER = True
DELIVER_DIR = r"D:\鼠标键盘驱动显示"


def downloads_dir() -> str:
    """当前用户的「下载」文件夹（读注册表，兼容自定义/重定向位置）。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        # Downloads 的 GUID
        path, _ = winreg.QueryValueEx(
            key, "{374DE290-123F-4565-9164-39C4925E467B}")
        path = os.path.expandvars(path)
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Downloads")


def sync_source() -> None:
    """把项目源代码同步到 D 盘交付目录的「源代码」子目录。

    只复制源码与资源，不带 settings.json、build/dist、缓存和调试截图。
    """
    if not COPY_TO_DELIVER or not os.path.isdir(DELIVER_DIR):
        return
    dst_src = os.path.join(DELIVER_DIR, "源代码")
    try:
        os.makedirs(dst_src, exist_ok=True)
        count = 0
        for name in os.listdir(APP_DIR):
            src = os.path.join(APP_DIR, name)
            if os.path.isfile(src) and name.endswith(".py"):
                shutil.copy2(src, os.path.join(dst_src, name))
                count += 1
        # 配套资源：网页、图标、打包配置、启动脚本
        web = os.path.join(APP_DIR, "web")
        if os.path.isdir(web):
            shutil.copytree(web, os.path.join(dst_src, "web"), dirs_exist_ok=True)
        for name in (ICON, "外设控制中心.spec", "run.bat"):
            if os.path.isfile(name):
                shutil.copy2(name, os.path.join(dst_src, os.path.basename(name)))
        print(f"已同步源代码到: {dst_src}（{count} 个 .py）")
    except Exception as exc:
        print("同步源代码失败:", exc)


def main() -> int:
    if not os.path.exists(ICON):
        print("缺少 app.ico，先运行 export_icon.py 生成")
        return 1

    # 写入固定构建号，诊断文件用它判断版本（文件修改时间会被复制/传输改掉）
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        with open(os.path.join(APP_DIR, "_build_id.py"), "w", encoding="utf-8") as f:
            f.write('"""由 build_exe.py 自动生成，记录本次打包时间。"""\n')
            f.write(f'BUILD_ID = "{stamp}"\n')
        print("构建号:", stamp)
    except Exception as exc:
        print("写入构建号失败:", exc)

    # 清理上次产物
    for d in (DIST, BUILD):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    spec = os.path.join(APP_DIR, "外设控制中心.spec")
    if os.path.exists(spec):
        os.remove(spec)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",           # 单文件 exe
        "--noconsole",         # ★ 不带控制台窗口 —— 根治黑框
        "--windowed",          # GUI 子系统
        "--name", EXE_NAME,
        "--icon", ICON,
        # 数据文件：图标 + 网页资源（放进解包目录）
        "--add-data", f"{ICON};.",
        "--add-data", f"{os.path.join(APP_DIR, 'web')};web",
        # 隐藏导入
        "--hidden-import", "hidpp_win",
        "--hidden-import", "aula_battery",
        "--hidden-import", "aula_web_cache",
        "--hidden-import", "eweadn_mouse",
        "--hidden-import", "mchose_mouse",
        "--hidden-import", "bridge_server",
        "--hidden-import", "autostart",
        "--hidden-import", "paths",
        "--hidden-import", "config",
        "--hidden-import", "_build_id",
        "--hidden-import", "backdrop",     # 背景图片/视频解码
        "--hidden-import", "cv2",          # OpenCV
        "--hidden-import", "numpy",        # OpenCV 依赖
        # 排除不需要的大依赖，减小体积（numpy/cv2 必须保留，供背景用）
        "--exclude-module", "pandas",
        "--exclude-module", "matplotlib",
        "--exclude-module", "PIL",
        "--exclude-module", "PyQt5",
        "--exclude-module", "pytest",
        "--noupx",
        os.path.join(APP_DIR, "app.py"),
    ]

    print("打包中（首次可能要 1-2 分钟）…")
    result = subprocess.run(cmd, cwd=APP_DIR)
    if result.returncode != 0:
        print("打包失败")
        return result.returncode

    exe = os.path.join(DIST, EXE_NAME + ".exe")
    if not os.path.exists(exe):
        print("打包结束但未找到产物:", exe)
        return 1

    print()
    print("打包完成:", exe)
    print("  大小: %.1f MB" % (os.path.getsize(exe) / 1024 / 1024))

    # 每次打包都复制到固定位置，方便直接发给别人
    targets = []
    if COPY_TO_DOWNLOADS and os.path.isdir(downloads_dir()):
        targets.append((downloads_dir(), "下载文件夹"))
    else:
        print("已跳过下载文件夹（保持原文件不动）")
    # D 盘交付目录：存在才复制，避免在别人电脑上报错
    if COPY_TO_DELIVER and os.path.isdir(DELIVER_DIR):
        targets.append((DELIVER_DIR, "D盘交付目录"))

    for dst_dir, label in targets:
        if not os.path.isdir(dst_dir):
            print(f"{label}不存在，跳过复制:", dst_dir)
            continue
        try:
            dst = os.path.join(dst_dir, EXE_NAME + ".exe")
            shutil.copy2(exe, dst)
            print(f"已复制到{label}:", dst)
        except Exception as exc:
            print(f"复制到{label}失败:", exc)

    # 同步源代码到交付目录，保证 D 盘源码和 exe 是同一版
    sync_source()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
