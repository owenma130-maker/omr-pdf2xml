"""回归测试：坏页隔离 + 去重窗口。

这两个问题的现场：
  · 一本 92 页的谱子里有 2 页空白/歪斜，Audiveris 把整本导出当一次性事务，
    一页坏就 exit 1，90 页好页的 MusicXML 一个都不给 —— 用户看到的就是
    "识别了 22 分钟，最后 ★ Audiveris 失败 (exit 1)"，没有任何产物。
  · 去重原先全对全（O(n^2)）：92 页 = 4186 页对 × 2 次整页膨胀，实测卡
    12 分钟以上、内存冲到 5.8 GB，而这本书里一个重复页都没有。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import omr_cli  # noqa: E402
from omr_engine.fusion import omr_structural_export as X  # noqa: E402


def _omr(tmp_path, sheets):
    """``sheets``: {页码: 该页 XML 文本}。返回 .omr 路径。"""
    p = tmp_path / 'book.omr'
    with zipfile.ZipFile(p, 'w') as zf:
        for n, xml in sheets.items():
            zf.writestr(f'sheet#{n}/sheet#{n}.xml', xml)
            zf.writestr(f'sheet#{n}/BINARY.png', b'')
    return p


GOOD = '<score><page><system/></page></score>'


def test_list_empty_sheets_finds_zero_byte_pages(tmp_path):
    """Audiveris 判无效的页 = 0 字节的 sheet#N.xml（没有 <page>）。"""
    p = _omr(tmp_path, {1: GOOD, 2: '', 3: '<score/>', 4: GOOD})
    assert omr_cli.list_empty_sheets(p) == [2, 3]


def test_list_empty_sheets_on_missing_file():
    """坏 zip / 不存在的文件不能抛异常 —— 它只在报错路径上做诊断。"""
    assert omr_cli.list_empty_sheets(Path('_does_not_exist_.omr')) == []


def test_empty_sheet_is_skipped_not_exported(tmp_path):
    """空页必须整页跳过，不能当"一页没有音符的乐谱"塞进 models。

    跳过后 stats 必须记录页码，好让上层能如实告诉用户哪几页丢了。
    """
    omr = _omr(tmp_path, {1: GOOD, 2: '', 3: GOOD})
    st = X.export_structural_musicxml(str(omr), str(tmp_path / 'o.musicxml'),
                                      verbose=False, no_sidecars=True)
    assert st.get('skipped_empty_sheets') == [2]
    assert any('skipped' in w and 'p2' in w for w in (st.get('warnings') or []))


def test_dilate_matches_pil_maxfilter():
    """性能修复不能改行为：numpy 可分离膨胀必须与 PIL MaxFilter 等价。

    这是本轮最重要的约束 —— _ink_overlap 的判定结果必须逐位不变。
    """
    Image = pytest.importorskip('PIL.Image')
    ImageFilter = pytest.importorskip('PIL.ImageFilter')
    rng = np.random.default_rng(0)
    a = rng.random((120, 80)) < 0.10
    for tol in (1, 3, 5):
        ref = np.asarray(Image.fromarray((a * 255).astype('uint8'))
                         .filter(ImageFilter.MaxFilter(2 * tol + 1))) > 0
        got = X._dilate(a, tol)
        inner = (slice(tol, -tol), slice(tol, -tol))
        assert np.array_equal(ref[inner], got[inner]), f'tol={tol} 不一致'
        assert (got | a).sum() == got.sum(), '膨胀结果必须是原集合的超集'


def test_dedup_window_is_bounded():
    """去重窗口必须存在且有限 —— 否则 92 页又会退化成 O(n^2)。"""
    assert 1 <= X.DEDUP_WINDOW <= 128


def test_dedup_finds_near_identical_page():
    """窗口虽小，真重复页仍必须被识别（彩虹谱第 1、3 页那个用例）。"""
    rng = np.random.default_rng(1)
    page = np.zeros((800, 600), dtype=bool)
    page[::20, :] = True
    page[100:140, 50:300] = True
    other = np.roll(np.roll(page, 2, 0), 1, 1)      # 同一页，偏移 2px/1px
    assert X._ink_overlap(other, page) >= 0.97
    assert X._ink_overlap(page, other) >= 0.97


class _FakeRun:
    """假装 Audiveris：写出一本含 1 页坏页的 .omr，然后 exit 1。

    这就是用户现场的复刻 —— 识别明明成功了（.omr 里有 90 页好页），
    但 Audiveris 因为那 2 页坏页把整本导出判失败。
    """
    returncode = 1
    stderr = ''
    stdout = ('INFO  [x#66] SheetStub 1194 | Sheet x#66 flagged as invalid.\n'
              'INFO  [x] Book 596 | Could not export since transcription did '
              'not complete successfully\n')

    def __init__(self, **kw):
        pass

    def __call__(self, argv, **kw):
        out = Path(argv[argv.index('-output') + 1])
        out.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out / 'x.omr', 'w') as zf:
            zf.writestr('sheet#1/sheet#1.xml', GOOD)
            zf.writestr('sheet#1/BINARY.png', b'')
            zf.writestr('sheet#2/sheet#2.xml', '')      # 坏页
            zf.writestr('sheet#2/BINARY.png', b'')
        return self


def test_pdf2xml_keeps_going_when_audiveris_exits_nonzero(tmp_path, monkeypatch):
    """Audiveris 非 0 退出但 .omr 已落盘时，必须继续导出。

    回归的是用户实际撞到的墙：一本 92 页的谱子，2 页空白/歪斜让 Audiveris
    整本导出 exit 1，于是 90 页好页的 MusicXML 一个都不给 —— 用户等了 22 分钟
    只看到 "★ Audiveris 失败 (exit 1)"。原来的代码在 returncode 判断处就
    return 了，根本没往下看 .omr。
    """
    import argparse
    pdf = tmp_path / 'x.pdf'
    pdf.write_bytes(b'%PDF-1.4\n')
    out = tmp_path / 'out.musicxml'

    monkeypatch.setattr(omr_cli, 'find_audiveris', lambda explicit=None: 'fake')
    monkeypatch.setattr(omr_cli.subprocess, 'run', _FakeRun())

    rc = omr_cli.cmd_pdf2xml(argparse.Namespace(
        pdf=str(pdf), output=str(out), verbose=False, audiveris='fake'))

    assert rc == 0, f'Audiveris exit 1 时不应直接放弃整本书 (rc={rc})'
    assert out.exists(), '好页的 MusicXML 必须被写出来'
    assert '<score-partwise' in out.read_text(encoding='utf-8')


def test_pdf2xml_still_fails_when_no_omr_written(tmp_path, monkeypatch):
    """真失败（.omr 都没产出）仍必须报错退出，不能被上面的容错吞掉。"""
    import argparse

    class _NoOmr:
        returncode = 1
        stdout = 'OutOfMemoryError: Java heap space'
        stderr = ''
        def __init__(self, **kw): pass
        def __call__(self, argv, **kw): return self

    pdf = tmp_path / 'x.pdf'
    pdf.write_bytes(b'%PDF-1.4\n')
    monkeypatch.setattr(omr_cli, 'find_audiveris', lambda explicit=None: 'fake')
    monkeypatch.setattr(omr_cli.subprocess, 'run', _NoOmr())

    rc = omr_cli.cmd_pdf2xml(argparse.Namespace(
        pdf=str(pdf), output=str(tmp_path / 'o.musicxml'), verbose=False,
        audiveris='fake'))
    assert rc == 4
