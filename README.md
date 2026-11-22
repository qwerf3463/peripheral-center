# 外设中心(动态背景板)

Windows 桌面外设电量监控工具,带动态背景悬浮窗。

## 功能
- 实时显示鼠标 / 键盘电量(罗技 HID++、狼蛛、前行者、迈从等)
- 电量读不到时自动回退到官方客户端缓存
- 大界面 + 可拖动悬浮窗,深色/浅色主题
- 图片/视频/GIF 动态背景,支持模糊、锐化、圆角
- 开机自启、诊断信息导出（目前很多设备没有适配，需要联系作者适配）

## 下载
到 Releases 下载 `外设控制中心.exe`,双击即可运行。

## 从源码运行
需要 Python 3.x,安装依赖:
pip install opencv-python numpy

然后运行:
python app.py          # 大界面
python app.py --float  # 悬浮窗
python app.py --check  # 设备诊断

## 打包 exe
python build_exe.py

## 说明
本程序只读设备电量,不会向设备写入任何配置。

