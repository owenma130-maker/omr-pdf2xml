# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：产出两个 exe

  dist/omr-pdf2xml.exe       图形界面（双击就能用，不弹黑框）
  dist/omr.exe               命令行（check / omr2xml / pdf2xml / eval / status）

★ 说明：识别那一层是 **Audiveris**（Java 程序），本工具不把它打进 exe。
  用户需要自己装 Audiveris —— 就像别的工具需要装 Java 一样。
  界面里会自动检测常见安装位置，也可手工填路径。

用法：
  pip install pyinstaller
  pyinstaller omr-pdf2xml.spec
"""
import sys
from pathlib import Path

ROOT = Path(SPECPATH)                     # noqa: F821  (PyInstaller 注入)

hiddenimports = [
    'omr_engine', 'omr_engine.referee',
    'omr_engine.fusion', 'omr_engine.fusion.omr_structural_export',
    'omr_engine.fusion.audiveris_fusion',
]
datas = [(str(ROOT / 'LICENSE'), '.'),
         (str(ROOT / 'README.md'), '.')]

# ---- 图形界面（无控制台窗口）----
a_gui = Analysis([str(ROOT / 'omr_gui.py')],
                 pathex=[str(ROOT)],
                 hiddenimports=hiddenimports,
                 datas=datas,
                 excludes=['matplotlib', 'pytest', 'numpy.testing'],
                 noarchive=False)
pyz_gui = PYZ(a_gui.pure)
exe_gui = EXE(pyz_gui, a_gui.scripts, a_gui.binaries, a_gui.datas,
              name='omr-pdf2xml', console=False, upx=True,
              icon=None)

# ---- 命令行（保留控制台）----
a_cli = Analysis([str(ROOT / 'omr_cli.py')],
                 pathex=[str(ROOT)],
                 hiddenimports=hiddenimports,
                 datas=datas,
                 excludes=['matplotlib', 'pytest', 'numpy.testing'],
                 noarchive=False)
pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(pyz_cli, a_cli.scripts, a_cli.binaries, a_cli.datas,
              name='omr', console=True, upx=True,
              icon=None)
