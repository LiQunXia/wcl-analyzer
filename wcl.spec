# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把整个应用压成一个可双击的 exe / 可执行文件。

用法（在项目目录）：
    pip install -r requirements.txt "pyinstaller>=6.10"
    pyinstaller --noconfirm wcl.spec

产物在 dist/ 下。HTML/JSON 资源被打进包里，运行时由 server._resource_dir()
从 sys._MEIPASS 读取；运行期缓存写到 %LOCALAPPDATA%\WCLAnalyzer。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)

# curl_cffi 带原生 libcurl，必须整包收集，否则打包后 import 会失败。
datas, binaries, hiddenimports = collect_all("curl_cffi")

datas += [
    (str(ROOT / "index.html"), "."),
    (str(ROOT / "mplus.html"), "."),
    (str(ROOT / "cooldowns.json"), "."),
    (str(ROOT / "mplus_spells.json"), "."),
]

a = Analysis(
    ["launcher.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["mdt_export"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WCL-Analyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
