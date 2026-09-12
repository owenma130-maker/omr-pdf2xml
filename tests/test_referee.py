# -*- coding: utf-8 -*-
"""最小测试：裁判的核心判据。

只测**纯函数与统计**（不依赖具体样本文件），
样本相关的用例在文件不存在时自动 skip —— 保证 `pytest` 在任何机器上都能跑。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omr_engine import referee  # noqa: E402


# --------------------------------------------------------------------------
# 相邻页首号相减 —— 这是"本页应有几小节"的核心算法
# --------------------------------------------------------------------------
def test_printed_span_basic():
    # p3 首号 8、p4 首号 13 → p3 有 5 小节；p4 有 4 小节（到 p5 的 17）
    pn = {3: [8], 4: [13], 5: [17]}
    span = referee.printed_span_measures(pn)
    assert span[3] == 5
    assert span[4] == 4
    assert span[5] is None          # 后面没有号了 → 如实留 None，不编


def test_printed_span_does_not_cross_segments():
    """★ 一本分谱集里每份都从第 1 小节重新编号，跨封面相减会得到负数。

    封面页在第 4 页：p3（首号 30）属于第 1 段，p5（首号 8）属于第 2 段。
    """
    pn = {3: [30], 5: [8]}
    span = referee.printed_span_measures(pn, covers=(1, 4))
    assert span[3] is None, '跨段不得相减'
    assert span[5] is None


def test_printed_span_within_segment_is_kept():
    pn = {5: [8], 6: [13]}
    span = referee.printed_span_measures(pn, covers=(1, 4))
    assert span[5] == 5


# --------------------------------------------------------------------------
# MusicXML 统计 —— 口径必须写在代码里
# --------------------------------------------------------------------------
def test_musicxml_stats_does_not_count_part_name(tmp_path):
    """★ `<part\\b` 会把 `<part-name>` 也算进去（`\\b` 在 t 与 - 之间成立）。

    实测：19 个 part 会被数成 41。这里把它钉住。
    """
    f = tmp_path / 'a.musicxml'
    f.write_text(
        '<score-partwise><part-list>'
        '<score-part id="P1"><part-name>Flute</part-name></score-part>'
        '<score-part id="P2"><part-name>Oboe</part-name></score-part>'
        '</part-list>'
        '<part id="P1"><measure number="1"/><measure number="2"/></part>'
        '<part id="P2"><measure number="1"/><measure number="2"/></part>'
        '</score-partwise>', encoding='utf-8')
    s = referee.musicxml_stats(f)
    assert s['parts'] == 2, f"part 数应为 2，实际 {s['parts']}"
    assert s['measures'] == 4
    assert s['measures_per_part'] == 2


def test_musicxml_stats_counts_chord_members_separately(tmp_path):
    f = tmp_path / 'b.musicxml'
    f.write_text(
        '<part><measure>'
        '<note><pitch><step>C</step><octave>4</octave></pitch></note>'
        '<note><chord/><pitch><step>E</step><octave>4</octave></pitch></note>'
        '<note><rest/></note>'
        '</measure></part>', encoding='utf-8')
    s = referee.musicxml_stats(f)
    assert s['notes'] == 3
    assert s['pitches'] == 2
    assert s['chord_members'] == 1
    assert s['rests'] == 1


# --------------------------------------------------------------------------
# 端到端（样本不存在就 skip）
# --------------------------------------------------------------------------
FULL_PDF = ROOT / 'data' / 'new_score_full' / 'full.pdf'
SEG00 = ROOT / 'results' / 'final_parts' / 'seg00.musicxml'


@pytest.mark.skipif(not (FULL_PDF.exists() and SEG00.exists()),
                    reason='样本不在本机')
def test_check_on_digital_score():
    rep = referee.check(FULL_PDF, SEG00)
    assert rep['referee'], '数字 PDF 应该有裁判'
    assert rep['musicxml']['measures_per_part'] == 132
    assert rep['musicxml']['parts'] == 19


def test_format_report_says_no_referee_when_nothing_readable():
    rep = {'pdf': 'x.pdf', 'pages': 3, 'referee': None, 'notes': ['无文本层']}
    out = referee.format_report(rep)
    assert '没有裁判' in out
