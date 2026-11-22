# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/Users/qwer/WorkBuddy/2026-11-20-13-45-25/logi_float/app.py'],
    pathex=[],
    binaries=[],
    datas=[('C:/Users/qwer/WorkBuddy/2026-11-20-13-45-25/logi_float/app.ico', '.'), ('C:/Users/qwer/WorkBuddy/2026-11-20-13-45-25/logi_float/web', 'web')],
    hiddenimports=['hidpp_win', 'aula_battery', 'aula_web_cache', 'eweadn_mouse', 'mchose_mouse', 'bridge_server', 'autostart', 'paths', 'config', '_build_id', 'backdrop', 'cv2', 'numpy'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'matplotlib', 'PIL', 'PyQt5', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='外设控制中心',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:/Users/qwer/WorkBuddy/2026-11-20-13-45-25/logi_float/app.ico'],
)
