"""资源路径解析，兼容 PyInstaller 打包（frozen）与源码运行两种模式。

打包后：
  - sys.frozen 为 True，sys._MEIPASS 指向解包目录（--onefile）或 exe 所在目录
  - 数据文件（app.ico / web/）需要一起打包，用 --add-data 收集到 _MEIPASS
  - 但「用户可写的数据目录」（如自启命令、日志）应指向 exe 所在目录，
    而不是临时解包目录（onefile 每次运行都会重新解压，路径会变）
"""
from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的程序里。"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> str:
    """只读资源目录：打包时是解包目录，源码运行时是项目目录。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_dir() -> str:
    """程序所在目录（可写）。打包后是 exe 所在目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource(*parts: str) -> str:
    """只读资源的完整路径。"""
    return os.path.join(bundle_dir(), *parts)


def writable_resource(*parts: str) -> str:
    """需要在用户机器上长期存在的文件路径（如自启脚本）。"""
    return os.path.join(app_dir(), *parts)
