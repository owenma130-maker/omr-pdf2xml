# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（目录模式 / onedir）。

产出：
  dist/omr-pdf2xml/
      omr-pdf2xml.exe        图形界面（双击运行）
      omr.exe                命令行
      _internal/...          Python 运行时等
      _internal/vendor/audiveris/...   ★ 自带的 Audiveris（含它自己的 JRE）

★ 为什么用【目录模式】而不是单文件 exe：
  自带 Audiveris 约 163 MB / 2700 个文件。单文件模式每次启动都要把它解包到临时目录，
  既慢又占地。目录模式就是"一个文件夹，双击里面的 exe"，与 Audiveris 官方发行版一致。

用法：
  python tools/bundle_audiveris.py --from "<Audiveris 目录>"   # 先把 Audiveris 放好
  pip install pyinstaller
  pyinstaller omr-pdf2xml.spec --noconfirm
"""
from pathlib import Path

ROOT = Path(SPECPATH)                      # noqa: F821  (PyInstaller 注入)
VENDOR = ROOT / 'vendor' / 'audiveris'

hiddenimports = [
    'omr_engine', 'omr_engine.referee', 'omr_engine.runtime',
    'omr_engine.fusion', 'omr_engine.fusion.omr_structural_export',
    'omr_engine.fusion.audiveris_fusion',
]
datas = [(str(ROOT / 'LICENSE'), '.'),
         (str(ROOT / 'README.md'), '.'),
         (str(ROOT / 'CHANGELOG.md'), '.')]
if VENDOR.is_dir():
    datas.append((str(VENDOR), 'vendor/audiveris'))
    print(f'[spec] 自带 Audiveris: {VENDOR}')
else:
    print('[spec] ★ 没有 vendor/audiveris —— 打出来的包需要用户自装 Audiveris。'
          '先跑 tools/bundle_audiveris.py')

common = dict(pathex=[str(ROOT)], hiddenimports=hiddenimports, datas=datas,
              excludes=['matplotlib', 'pytest', 'pandas', 'scipy'])

a_gui = Analysis([str(ROOT / 'omr_gui.py')], **common)
pyz_gui = PYZ(a_gui.pure)
exe_gui = EXE(pyz_gui, a_gui.scripts, [],
              name='omr-pdf2xml', console=False, upx=False)

a_cli = Analysis([str(ROOT / 'omr_cli.py')], **common)
pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(pyz_cli, a_cli.scripts, [],
              name='omr', console=True, upx=False)

COLLECT(exe_gui, exe_cli,
        a_gui.binaries + a_cli.binaries,
        a_gui.datas + a_cli.datas,
        strip=False, upx=False, name='omr-pdf2xml')
