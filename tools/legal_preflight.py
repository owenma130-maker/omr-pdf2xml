# -*- coding: utf-8 -*-
"""★ 发布前法律预检 —— 只扫【已进 git 索引】的文件。

目标不是"大概安全"，而是**逐条列出命中，直到零命中**。
宁严勿宽：命中即视为"需要人工确认或直接移出"。

分类：
  PS_DERIVED   厂商软件的逆向产物 / 派生数据路径（最高风险）
  THIRD_PARTY  第三方代码克隆（LibreScore / node_modules 等）
  SECRET       凭据（token / key / 密码）
  PERSONAL     个人路径 / 微信目录 / 他人数据
  BINARY_ASSET 仓库里的图片、PDF、音频等（多半含第三方版权内容）

用法:
  python tools/legal_preflight.py            # 扫描并列出
  python tools/legal_preflight.py --summary  # 只出统计
退出码 0 = 零命中；1 = 有命中
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS = {
    'PS_DERIVED': [
        r'photoscore', r'neuratron', r'photo\s*score',
        r'ps_reverse', r'ps_derived', r'ps_derived_archive',
        r'fix\s*版', r'fix版',
        r'replace_exe', r'frida', r'\bida\b', r'\bhxd\b', r'x64dbg',
        r'HNP\.dll', r'prepro\.dll', r'ir_fe\.dll', r'\bnotate\b',
        r'0x10[0-9a-f]{4}', r'常量表',
        # ★ 补：厂商在本项目里一律被简写成 "PS"，光扫全名会漏
        #   （实测漏了 `eval` 子命令的 "对照 PS 真值"）
        r'\bPS\b', r'\bps_[a-z]',
    ],
    'THIRD_PARTY': [
        r'librescore', r'webmscore', r'node_modules',
        r'\.min\.js', r'research_omr_clones',
    ],
    'SECRET': [
        r'gh[pousr]_[A-Za-z0-9]{20,}', r'sk-[A-Za-z0-9]{20,}',
        r'(?i)(api[_-]?key|secret|passwd|password|access[_-]?token)'
        r'\s*[:=]\s*["\'][^"\']{8,}',
    ],
    'PERSONAL': [
        r'wxid_', r'xwechat_files', r'C:\\+Users\\+[A-Za-z]',
        r'ps_derived_archive',
    ],
    'BINARY_ASSET': [],
}
BINARY_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.pdf', '.mp3',
              '.mp4', '.wav', '.zip', '.tar', '.gz', '.7z', '.pt', '.onnx',
              '.pth', '.pb', '.tflite', '.dll', '.exe', '.so', '.dylib'}
TEXT_EXT = {'.py', '.md', '.txt', '.json', '.toml', '.cfg', '.ini', '.yml',
            '.yaml', '.csv', '.html', '.js', '.ts', '.vue', '.ps1', '.sh',
            '.bat', '.xml', ''}

# ★ 这个检查器自己【必须】含有那些词（它们是"要抓的模式"），
#   否则它没法工作。所以显式豁免自身，并在文件里写明原因 ——
#   而不是把模式藏起来（藏起来就无法审计了）。
EXEMPT = {'tools/legal_preflight.py'}


SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', '.venv', 'venv',
             'node_modules', 'build', 'dist'}


def tracked(only_dir=None):
    if only_dir:
        base = Path(only_dir)
        # ★ 只要这个目录本身是 git 仓库，就以【已跟踪文件】为准 ——
        #   否则会把 .git/、__pycache__/、缓存文件全扫进来（实测多出 20 条噪声）。
        r = subprocess.run(['git', '-C', str(base), 'ls-files'],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace')
        if r.returncode == 0 and r.stdout.strip():
            return [l for l in r.stdout.splitlines() if l.strip()]
        out = []
        for p in base.rglob('*'):
            if not p.is_file():
                continue
            rel = p.relative_to(base)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            out.append(str(rel).replace('\\', '/'))
        return out
    out = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True,
                         text=True, encoding='utf-8', errors='replace')
    return [l for l in out.stdout.splitlines() if l.strip()]


def scan(only_dir=None):
    files = tracked(only_dir)
    base = Path(only_dir) if only_dir else ROOT
    print(f'待检文件数: {len(files)}  (根: {base})')
    hits = {k: [] for k in PATTERNS}
    for rel in files:
        if rel in EXEMPT:
            continue
        p = base / rel
        ext = p.suffix.lower()
        if ext in BINARY_EXT:
            hits['BINARY_ASSET'].append((rel, f'二进制/媒体文件 ({ext})'))
            continue
        if ext not in TEXT_EXT:
            hits['BINARY_ASSET'].append((rel, f'未知类型 ({ext})'))
            continue
        try:
            t = p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        for cat, pats in PATTERNS.items():
            if not pats:
                continue
            for pat in pats:
                m = re.search(pat, t, re.IGNORECASE)
                if m:
                    line = t[:m.start()].count('\n') + 1
                    frag = t.splitlines()[line - 1].strip()[:90] if \
                        t.splitlines() else ''
                    hits[cat].append((rel, f'L{line}  …{frag}'))
                    break
    return files, hits


def main():
    only_dir = None
    if '--path' in sys.argv:
        only_dir = sys.argv[sys.argv.index('--path') + 1]
    files, hits = scan(only_dir)
    total = 0
    for cat in PATTERNS:
        rows = hits[cat]
        total += len(rows)
        print(f'\n=== {cat}  {len(rows)} 个文件 ===')
        for rel, why in ([] if '--summary' in sys.argv else rows[:60]):
            print(f'  {rel:<58} {why}')
        if len(rows) > 60 and '--summary' not in sys.argv:
            print(f'  … 其余 {len(rows) - 60} 个')
    print(f'\n★ 命中文件总数（去重前）: {total}')
    uniq = sorted({r for cat in hits for r, _w in hits[cat]})
    print(f'★ 命中文件总数（去重后）: {len(uniq)}')
    try:
        outdir = ROOT / 'results'
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / 'legal_preflight.txt').write_text(
            '\n'.join(f'{cat}\t{r}\t{w}' for cat in hits for r, w in hits[cat]),
            encoding='utf-8')
        print(f'[写出] {outdir / "legal_preflight.txt"}')
    except OSError as e:                      # 只读环境 / 目录不存在
        print(f'（明细写不出，跳过：{e}）')
    return 0 if total == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
