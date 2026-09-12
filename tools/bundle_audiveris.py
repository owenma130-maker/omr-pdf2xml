# -*- coding: utf-8 -*-
"""把 Audiveris 放进产品里，让用户不用另行安装。

为什么必须自带：**Audiveris 没有插件接口**，我们只能把它当外部程序调用
（`-batch -output … -export …`）。既然只能调用，那就随产品一起发。

用法：
  python tools/bundle_audiveris.py --from "C:\\Program Files\\Audiveris"
  python tools/bundle_audiveris.py --from <解压出来的 Audiveris 发行目录>

结果：`vendor/audiveris/`（含 Audiveris.exe 与它自带的 JRE）。
**这一份不进 git**（约 150+ MB），由打包流程现取现放。

★ 许可：Audiveris 是 AGPL-3.0，本项目同为 AGPL-3.0 —— 原样再分发是允许的，
  但必须**保留它的许可证与出处**。本脚本会一并写入 `vendor/audiveris/NOTICE.txt`。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DST = ROOT / 'vendor' / 'audiveris'

NOTICE = """本目录是 Audiveris 的**原样再分发**（未做任何修改）。

  Audiveris  ——  开源光学乐谱识别（OMR）
  版本      : {version}
  许可      : GNU Affero General Public License v3.0 (AGPL-3.0)
  源码      : https://github.com/Audiveris/audiveris
  许可证全文: https://www.gnu.org/licenses/agpl-3.0.txt

本项目 omr-pdf2xml 同样以 AGPL-3.0 发布，因此可以随产品一并分发它。
Audiveris **没有插件接口**，本项目是以外部进程方式调用它：
    Audiveris -batch -output <目录> -export <输入.pdf>

分发本产品即表示你也受 AGPL-3.0 约束：需要向使用者提供完整对应源码。
"""


def version_of(exe: Path) -> str:
    try:
        r = subprocess.run([str(exe), '-version'], capture_output=True,
                           text=True, errors='replace', timeout=120)
        for ln in (r.stdout + r.stderr).splitlines():
            if 'Version' in ln:
                return ln.split(':', 1)[1].strip()
    except Exception:
        pass
    return '未知'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='src', required=True,
                    help='Audiveris 安装/解压目录（含 Audiveris.exe）')
    ap.add_argument('--force', action='store_true', help='覆盖已有的 vendor 副本')
    a = ap.parse_args()

    src = Path(a.src)
    if not src.is_dir():
        print(f'[错误] 不是目录: {src}')
        return 2
    exe = None
    for n in ('Audiveris.exe', 'audiveris.exe', 'Audiveris', 'audiveris'):
        if (src / n).exists():
            exe = src / n
            break
    if exe is None:
        print(f'[错误] {src} 里没找到 Audiveris 可执行文件')
        return 2

    if DST.exists():
        if not a.force:
            print(f'[跳过] {DST} 已存在（要覆盖加 --force）')
            return 0
        shutil.rmtree(DST)

    ver = version_of(exe)
    print(f'来源   : {exe}')
    print(f'版本   : {ver}')
    print(f'目标   : {DST}')
    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, DST)
    (DST / 'NOTICE.txt').write_text(
        NOTICE.format(version=ver), encoding='utf-8')

    n = sum(1 for _ in DST.rglob('*') if _.is_file())
    mb = sum(f.stat().st_size for f in DST.rglob('*') if f.is_file()) / 1e6
    print(f'完成   : {n} 个文件 / {mb:.1f} MB')

    sys.path.insert(0, str(ROOT))
    from omr_engine import runtime
    print('自检   :', runtime.audit_report())
    return 0


if __name__ == '__main__':
    sys.exit(main())
