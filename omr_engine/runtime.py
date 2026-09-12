# -*- coding: utf-8 -*-
"""定位 Audiveris 可执行文件。

★ 为什么需要"自带"：Audiveris **没有插件接口**，我们只能把它当外部程序调用
  （`-batch -output ... -export ...`）。既然只能调用，那就**随产品一起发** ——
  用户不该为了用它先去装一个 Java 程序。

查找顺序（先自带的，后系统装的）：
  1. 环境变量 `OMR_AUDIVERIS`（用户显式指定，最高优先）
  2. **随产品自带的**：`<包目录>/vendor/audiveris/Audiveris[.exe]`
     · 打包成 exe 后，包目录 = PyInstaller 的解包目录（见 `_base_dir`）
     · 也认 exe 旁边的 `vendor/audiveris/`
  3. 系统安装位置（Windows / macOS / Linux 常见路径）
  4. PATH 里的 `audiveris`

返回 `AudiverisInfo(path, source)`；找不到时 `None`。
**找不到时不猜、不编**，如实让调用方去报错。
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

EXE_NAMES = ('Audiveris.exe', 'audiveris.exe', 'Audiveris', 'audiveris')


@dataclass
class AudiverisInfo:
    path: Path
    source: str            # 'env' / 'bundled' / 'system' / 'path'

    def __str__(self) -> str:
        return f'{self.path}  ({self.source})'


def _base_dirs() -> list[Path]:
    """可能放着 `vendor/` 的目录，按优先级排。"""
    out = []
    # PyInstaller：单文件模式解包到 _MEIPASS；目录模式就是 exe 所在目录
    mei = getattr(sys, '_MEIPASS', None)
    if mei:
        out.append(Path(mei))
    if getattr(sys, 'frozen', False):
        out.append(Path(sys.executable).resolve().parent)
    # 源码运行：包目录 = omr_engine/ 的上一级
    out.append(Path(__file__).resolve().parents[1])
    out.append(Path.cwd())
    # 去重且保序
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def _looks_like_audiveris(d: Path) -> Path | None:
    for n in EXE_NAMES:
        p = d / n
        if p.exists():
            return p
    return None


def find_audiveris(explicit: str | os.PathLike | None = None) -> AudiverisInfo | None:
    # 1) 显式指定
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return AudiverisInfo(p, 'env')
        hit = _looks_like_audiveris(p) if p.is_dir() else None
        if hit:
            return AudiverisInfo(hit, 'env')
    # 1b) 环境变量
    env = os.environ.get('OMR_AUDIVERIS')
    if env:
        p = Path(env)
        if p.is_file():
            return AudiverisInfo(p, 'env')
        hit = _looks_like_audiveris(p) if p.is_dir() else None
        if hit:
            return AudiverisInfo(hit, 'env')

    # 2) 自带的
    for base in _base_dirs():
        for sub in (('vendor', 'audiveris'), ('audiveris',),
                    ('vendor', 'Audiveris')):
            d = base.joinpath(*sub)
            if d.is_dir():
                hit = _looks_like_audiveris(d)
                if hit:
                    return AudiverisInfo(hit, 'bundled')

    # 3) 系统安装位置
    if sys.platform.startswith('win'):
        roots = [Path(r'C:\Program Files\Audiveris'),
                 Path(r'C:\Program Files (x86)\Audiveris')]
    elif sys.platform == 'darwin':
        roots = [Path('/Applications/Audiveris.app/Contents/MacOS'),
                 Path('/Applications/Audiveris.app/Contents/app')]
    else:
        roots = [Path('/opt/audiveris'), Path('/usr/local/audiveris')]
    for d in roots:
        if d.is_dir():
            hit = _looks_like_audiveris(d)
            if hit:
                return AudiverisInfo(hit, 'system')

    # 4) PATH
    w = shutil.which('audiveris') or shutil.which('Audiveris')
    if w:
        return AudiverisInfo(Path(w), 'path')
    return None


def bundled_dir() -> Path | None:
    """自带 Audiveris 应该放的目录（给打包脚本用）。"""
    for base in _base_dirs():
        d = base / 'vendor' / 'audiveris'
        if d.is_dir():
            return d
    return None


def audit_report() -> str:
    """人话汇报"Audiveris 从哪来的"，启动时打印一行。"""
    info = find_audiveris()
    if info is None:
        return ('★ 没找到 Audiveris。识别那一层靠它；'
                '官方发行版自带，源码运行时请放到 vendor/audiveris/。')
    where = {'env': '环境变量指定', 'bundled': '随产品自带',
             'system': '系统安装', 'path': 'PATH 里找到'}[info.source]
    return f'Audiveris: {info.path}（{where}）'


if __name__ == '__main__':
    print(audit_report())
