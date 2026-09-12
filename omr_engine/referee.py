# -*- coding: utf-8 -*-
"""独立裁判 —— 判断"转出来的 MusicXML 和纸面像不像"。

★ 这是本产品与"黑箱" OMR 最重要的区别：
  黑箱只给你一个结果，不告诉你它对不对；
  这里给你**一份可以自己复核的成绩单**，判据全部来自**纸面/PDF 自身**：

    1. 印刷小节号   —— PDF 文本层里印在 system 左边距的号（斜体 Edwin-Italic 8pt）
                       扫描件没有文本层时退回"没有裁判"，不假装有
    2. 矢量小节线   —— PDF 自己画的竖线，用来数"这一页实际有几小节"
    3. 多小节休止   —— "休止 N 小节"的粗横杠（描边 >=2pt 的水平线，落在谱表中线上）
                       藏在横杠里的小节数 = 相邻两个 system 的印刷号之差 − 已切出的小节数
    4. 丢音符/丢小节 —— 我们自己链路的自报计数（`<head>` 进/出）

★ 诚实原则：**裁判读不出东西时说"没有裁判"，不编数字。**
  数字旁边必须写清口径（哪几页、含不含 `<chord/>`、是数字 PDF 还是扫描件）。
"""
from __future__ import annotations

import re
from pathlib import Path

try:
    import fitz                       # PyMuPDF（AGPL-3.0，与本项目许可一致）
except ImportError:                   # pragma: no cover
    fitz = None

# 页类型判据：封面/清单页的文本层里一定有这几个组名
GROUP_WORDS = ('木管组', '铜管组', '弦乐组', '打击乐组')


# ---------------------------------------------------------------------------
# PDF 侧：全部来自纸面自身
# ---------------------------------------------------------------------------
def page_text(page) -> str:
    return page.get_text() or ''


def is_cover(page) -> bool:
    return any(w in page_text(page) for w in GROUP_WORDS)


def printed_measure_numbers(page):
    """印在 system 左边距的小节号 -> ``[(y, 值)]``（按上下排序）。

    判据（实测）：斜体、字号 8.0、``x <= 110``、``y >= 50``。
    页眉那一行页码在 ``y ≈ 42``，速度标记在 ``x >= 150``，都被排掉。
    """
    out = []
    for blk in page.get_text('dict').get('blocks', []):
        for line in blk.get('lines', []):
            for sp in line.get('spans', []):
                t = (sp.get('text') or '').strip()
                if not t or 'italic' not in sp.get('font', '').lower():
                    continue
                x0, y0, _x1, _y1 = sp['bbox']
                if x0 <= 110 and y0 >= 50 and t.isdigit():
                    out.append((round(y0, 1), int(t)))
    out.sort()
    return out


def has_text_layer(page) -> bool:
    return bool(page_text(page).strip())


def vector_barlines(page, min_votes: int = 5,
                    hmin: float = 12.0, hmax: float = 140.0):
    """PDF 自己画的小节线 -> ``[x, ...]``。

    ★ 判据分两步，两步都是实测出来的：
      1. **高度带**：小节线只跨一行谱表（约 4 个线距）。取 12~140pt 这一带，
         把页边框（整页高）和短的装饰线排掉。
         **★ 不能放宽到"半页高"** —— 那样符干/连谱号也会混进来，
         实测 p3 会从 5 小节虚报成 22 小节。
      2. **投票数**：多谱表页每条小节线在每行谱表各画一小段 →
         一个 x 上会聚 ~谱表数 条。单谱表分谱页只有 1 票，调用方传 ``min_votes=1``。
    """
    xs = []
    for d in page.get_drawings():
        for it in d['items']:
            if it[0] == 'l':
                p1, p2 = it[1], it[2]
                if abs(p1.x - p2.x) < 1.5 and hmin <= abs(p1.y - p2.y) <= hmax:
                    xs.append(round(p1.x, 1))
            elif it[0] == 're':
                r = it[1]
                if r.width < 4.0 and hmin <= r.height <= hmax:
                    xs.append(round(r.x0, 1))
    groups = []
    for x in sorted(xs):
        if groups and x - groups[-1][-1] <= 4.0:
            groups[-1].append(x)
        else:
            groups.append([x])
    return sorted(sum(g) / len(g) for g in groups if len(g) >= min_votes)


def multirest_bars(page, staff_mids):
    """"休止 N 小节"的粗横杠 -> ``[(x中心, y)]``。

    判据：描边 >= 2pt 的水平线，且 y 落在该行谱表**中间那条线**上
    （实测 p53：五条谱线 282.4/287.3/292.3/297.2/302.2，横杠正好在 292.3）。
    """
    hits = []
    for d in page.get_drawings():
        w = d.get('width') or 0
        if w < 2.0:
            continue
        for it in d['items']:
            if it[0] != 'l':
                continue
            p1, p2 = it[1], it[2]
            if abs(p1.y - p2.y) >= 1.5 or abs(p1.x - p2.x) < 25:
                continue
            if any(abs(p1.y - m) <= 2.5 for m in staff_mids):
                hits.append((round((p1.x + p2.x) / 2, 1), round(p1.y, 1)))
    return hits


def printed_span_measures(pn_pages, covers=()):
    """★ 第二把尺子（首选）：由**相邻两页的首号**推出每页有多少小节。

    例：p3 首号 8、p4 首号 13 → p3 = 5 小节。**不需要任何图像处理，完全精确。**

    ★ 只在**同一段（同一首曲子）内部**才成立：
      一本分谱集里每一份都从第 1 小节重新编号，跨过封面页去相减会得到
      负数或荒唐值（实测 p77→p78 会算出 +70）。
      所以**跨段一律留 None**，不编数字。
    """
    order = sorted(pn_pages)
    cov = set(covers)
    seg_of, cur = {}, 0
    for p in range(1, (max(order) if order else 0) + 1):
        if p in cov and p != min(cov or {0}):
            cur += 1
        elif p in cov:
            cur = 0
        seg_of[p] = cur
    out = {}
    for i, p in enumerate(order):
        first = pn_pages[p][0] if pn_pages[p] else None
        nxt, np_ = None, None
        for j in range(i + 1, len(order)):
            q = order[j]
            if seg_of.get(q) != seg_of.get(p):
                break
            if pn_pages[q]:
                nxt, np_ = pn_pages[q][0], q
                break
        out[p] = (nxt - first) if (first is not None and nxt is not None) else None
        if out[p] is not None and out[p] <= 0:
            # 号变小 = 换了一首曲子、或者这一页是重复页 → 相减没有意义
            out[p] = None
    return out


def staff_mid_lines(page):
    """每行谱表的中间线 y（把细横线按 y 聚成 5 条一组，取第 3 条）。"""
    thin = []
    for d in page.get_drawings():
        if (d.get('width') or 0) > 1.0:
            continue
        for it in d['items']:
            if it[0] != 'l':
                continue
            p1, p2 = it[1], it[2]
            if abs(p1.y - p2.y) < 1.5 and abs(p1.x - p2.x) >= 60:
                thin.append(round(p1.y, 1))
    groups = []
    for y in sorted(thin):
        if groups and y - groups[-1][-1] <= 2.5:
            groups[-1].append(y)
        else:
            groups.append([y])
    return [g[2] for g in groups if len(g) == 5]


def covers(pdf_path):
    """哪些页是"分册封面页"（含乐器组清单）。"""
    doc = fitz.open(pdf_path)
    return [i + 1 for i in range(doc.page_count) if is_cover(doc[i])]


# ---------------------------------------------------------------------------
# MusicXML 侧
# ---------------------------------------------------------------------------
def musicxml_stats(path) -> dict:
    t = Path(path).read_text(encoding='utf-8', errors='replace')
    # ★ 用 `<part[ >]` 而不是 `<part\b`：后者会把 `<part-name>` 也算进去
    #   （`\b` 在 't' 和 '-' 之间成立），实测 19 个 part 会被数成 41。
    nparts = len(re.findall(r'<part[ >]', t))
    nmeas = len(re.findall(r'<measure[ >]', t))
    return {
        'parts': nparts,
        'measures': nmeas,
        'measures_per_part': nmeas // nparts if nparts else 0,
        'notes': len(re.findall(r'<note[ >]', t)),
        'pitches': len(re.findall(r'<pitch>', t)),
        'chord_members': len(re.findall(r'<chord\s*/>', t)),
        'rests': len(re.findall(r'<rest\b', t)),
        'time_modifications': len(re.findall(r'<time-modification\b', t)),
    }


def _omr_counts(omr_path, page_no):
    """`.omr` 里某页每个 system 的小节数（Audiveris 自己的切分）。"""
    import zipfile
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(omr_path) as zf:
        name = f'sheet#{page_no}/sheet#{page_no}.xml'
        if name not in zf.namelist():
            return []
        root = ET.fromstring(zf.read(name))
        out = []
        for sysel in root.iter('system'):
            parts = sysel.findall('part')
            out.append(len(parts[0].findall('measure')) if parts else 0)
        return out


# ---------------------------------------------------------------------------
# 扫描件的退路：按 system 顶线**定向 OCR** 左边距的小节号
#
# 为什么不是"OCR 整条左边距"：整条 OCR 会漏、还会跨行切号。
# 而小节号的位置有一条实测常数：
#     system 谱表顶线 − 号的【底边】 = 48~52 px（很稳）
#     号的 bbox 高 44~61 px → 号的顶边约在 顶线 − 113
#   → 正确的小窗 = [顶线 − 122, 顶线 − 38]
# ★ 踩过的坑：第一版用 [顶线−70, 顶线−30]，只盖住号的**底部一小截**，
#   把 "47" 读成 "4"、"63" 读成 "03" —— 自测一直卡在 4/9。
# ---------------------------------------------------------------------------
OCR_X0, OCR_X1 = 120, 420
OFF_TOP_LO, OFF_TOP_HI = 38, 122
_OCR = None


def _ocr_engine():
    global _OCR
    if _OCR is None:
        from paddleocr import PaddleOCR
        _OCR = PaddleOCR(use_doc_orientation_classify=False,
                         use_doc_unwarping=False,
                         use_textline_orientation=False, lang='en',
                         enable_mkldnn=False)
    return _OCR


def system_bands(omr_path):
    """``{sheet 目录名: [(system 序号, 顶线 y, 底线 y)]}`` —— 从 `.omr` 的谱线点读。"""
    import zipfile
    import xml.etree.ElementTree as ET
    out = {}
    with zipfile.ZipFile(omr_path) as zf:
        sheets = sorted({n.split('/')[0] for n in zf.namelist()
                         if n.startswith('sheet#')},
                        key=lambda s: int(s.split('#')[1]))
        for sh in sheets:
            try:
                root = ET.fromstring(zf.read(f'{sh}/{sh}.xml'))
            except Exception:
                continue
            page = root.find('page')
            if page is None:
                continue
            bands = []
            for si, s in enumerate(page.findall('system'), start=1):
                ys = [float(p.get('y')) for ln in s.iter('line')
                      for p in ln.findall('point') if p.get('y')]
                if ys:
                    bands.append((si, min(ys), max(ys)))
            out[sh] = bands
    return out


def _ocr_window(gray, x0, x1, y_lo, y_hi, pad=0):
    import cv2
    import numpy as np
    H = gray.shape[0]
    a, b = max(0, y_lo - pad), min(H, y_hi + pad)
    if b - a < 12:
        return []
    sub = gray[a:b, x0:x1]
    bgr = cv2.cvtColor(255 - sub, cv2.COLOR_GRAY2BGR)
    bgr = cv2.copyMakeBorder(bgr, 24, 24, 24, 24, cv2.BORDER_CONSTANT,
                             value=(255, 255, 255))
    try:
        res = _ocr_engine().predict(bgr)
    except Exception:
        return []
    out = []
    for r in res:
        d = getattr(r, 'json', r)
        if isinstance(d, dict) and 'res' in d:
            d = d['res']
        if not isinstance(d, dict):
            continue
        for t, sc in zip(d.get('rec_texts') or [], d.get('rec_scores') or []):
            t = str(t).strip()
            if t.isdigit():
                out.append({'text': t, 'score': float(sc)})
    return out


def ocr_measure_numbers(omr_path, x0=OCR_X0, x1=OCR_X1):
    """扫描件没有文本层时的退路。返回 ``{sheet 目录名: [(system, 号)]}``。

    读不出就是空的 —— **不编数字**。
    """
    import io
    import zipfile
    import cv2
    import numpy as np
    bands = system_bands(omr_path)
    out = {}
    with zipfile.ZipFile(omr_path) as zf:
        for sh, bs in bands.items():
            png = f'{sh}/BINARY.png'
            if png not in zf.namelist():
                continue
            gray = cv2.imdecode(np.frombuffer(zf.read(png), np.uint8),
                                cv2.IMREAD_GRAYSCALE)
            got = []
            for si, y0, _y1 in bs:
                cands = []
                for pad in (0, 12, 24):
                    cands += _ocr_window(gray, x0, x1,
                                         int(round(y0)) - OFF_TOP_HI,
                                         int(round(y0)) - OFF_TOP_LO, pad=pad)
                cands = [c for c in cands if c['score'] >= 0.50]
                if not cands:
                    continue
                best = max(cands, key=lambda c: c['score'])
                got.append((si, int(best['text'])))
            out[sh] = got
    return out


def our_system_first_measures(musicxml_path):
    """从导出器的注释里取每个 ``(sheet 序号, system)`` 的首小节号。

    导出器在每个小节前写 ``<!-- omr sheet#N system M stacks ... -->``。
    """
    t = Path(musicxml_path).read_text(encoding='utf-8', errors='replace')
    annot = re.compile(r'<!-- omr sheet#(\d+) system (\S+) stacks .*?-->')
    meas = re.compile(r'<measure number="(\d+)"')
    out, pending = {}, None
    for ln in t.splitlines():
        m = annot.search(ln)
        if m:
            pending = (int(m.group(1)), m.group(2))
            continue
        mm = meas.search(ln)
        if mm and pending:
            out.setdefault(pending, int(mm.group(1)))
            pending = None
    return out


def alignment(printed_by_system, ours_by_system):
    """把"纸上印的号"与"我们的号"逐 system 比。

    ``printed_by_system`` / ``ours_by_system`` 的键都是 ``(sheet 序号, system)``。
    返回 ``(命中数, 总数, [(键, 印刷, 我们)])``。
    """
    keys = sorted(set(printed_by_system) & set(ours_by_system))
    rows = [(k, printed_by_system[k], ours_by_system[k]) for k in keys]
    hit = sum(1 for _k, a, b in rows if a == b)
    return hit, len(rows), rows


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def check(pdf_path, musicxml_path=None, omr_path=None, single_part=False):
    """出一份自检报告。**读不出来的项如实标 None，不编。**"""
    pdf_path = Path(pdf_path)
    rep = {'pdf': str(pdf_path), 'referee': None, 'pages': 0,
           'printed_numbers': None, 'vector_measures': None,
           'multirest_hidden': None, 'musicxml': None, 'notes': []}
    if fitz is None:
        rep['notes'].append('未安装 PyMuPDF，无法读 PDF 判据')
        return rep

    doc = fitz.open(pdf_path)
    rep['pages'] = doc.page_count
    cov = covers(pdf_path)

    nums, vmeas, hidden, unreadable = {}, {}, 0, 0
    mr_bars = []
    for i in range(doc.page_count):
        pno, page = i + 1, doc[i]
        if is_cover(page):
            continue
        if not has_text_layer(page):
            unreadable += 1
            continue
        pn = printed_measure_numbers(page)
        if pn:
            nums[pno] = [v for _y, v in pn]
            if omr_path is not None:
                oc = _omr_counts(omr_path, pno)
                mids = staff_mid_lines(page)
                for k in range(min(len(oc), len(pn) - 1)):
                    gap = pn[k + 1][1] - pn[k][1]
                    d = gap - oc[k]
                    if d > 0:
                        hidden += d
                        bars = multirest_bars(page, mids)
                        if bars:
                            mr_bars.append({'sheet': f'sheet#{pno}',
                                            'system': str(k + 1),
                                            'x': int(round(bars[0][0] * 4.166)),
                                            'number': int(d)})

    if not nums:
        rep['notes'].append(
            f'★ 这份 PDF 没有可读的印刷小节号'
            f'（{unreadable} 页无文本层，定向 OCR 也没读出来）'
            '—— 本报告不提供小节数判据，也不编数字')
    else:
        if rep.get('referee') != '定向 OCR 印刷小节号（扫描件，无文本层）':
            rep['referee'] = 'PDF 文本层印刷小节号'
        rep['printed_numbers'] = nums
        # ★ 第二把尺子：由相邻页首号之差推出"本页应有几小节"（精确，无需图像处理）
        rep['vector_measures'] = printed_span_measures(nums, cov)
        if omr_path is not None:
            rep['multirest_hidden'] = hidden
            rep['multirest_bars'] = mr_bars

    # ---- 扫描件的退路：文本层读不出时，按 system 顶线定向 OCR ----
    printed_sys = {}
    if not nums and omr_path is not None:
        try:
            ocr = ocr_measure_numbers(omr_path)
        except Exception as e:                       # OCR 引擎缺失等
            ocr = {}
            rep['notes'].append(f'（OCR 退路不可用：{type(e).__name__}）')
        for sh, rows in (ocr or {}).items():
            if rows:
                nums[int(sh.split('#')[1])] = [v for _s, v in rows]
            for si, v in rows:
                printed_sys[(int(sh.split('#')[1]), str(si))] = v
        if nums:
            rep['referee'] = '定向 OCR 印刷小节号（扫描件，无文本层）'
            rep['printed_numbers'] = nums
            rep['vector_measures'] = printed_span_measures(nums, cov)
            rep['notes'] = [n for n in rep['notes'] if '没有可读的印刷小节号' not in n]

    if musicxml_path:
        rep['musicxml'] = musicxml_stats(musicxml_path)
        m = rep['musicxml']
        if nums:
            exp = sum(len(v) for v in nums.values())
            rep['expected_vs_actual'] = {
                'printed_system_starts': len(nums),
                'printed_groups_total': exp,
                'our_measures_per_part': m['measures_per_part'],
            }
        # ★ 逐 system 对齐：纸上印的号 vs 我们的号（这是"首号命中率"）
        if printed_sys:
            hit, tot, rows = alignment(printed_sys,
                                       our_system_first_measures(musicxml_path))
            rep['alignment'] = {'hit': hit, 'total': tot,
                                'rows': [[k[0], k[1], a, b]
                                         for k, a, b in rows]}
    return rep


def format_report(rep: dict) -> str:
    L = []
    A = L.append
    A('=' * 68)
    A('  自检报告 —— 判据全部来自纸面/PDF 自身')
    A('=' * 68)
    A(f"  输入 PDF      : {rep['pdf']}")
    A(f"  页数          : {rep['pages']}")
    A(f"  裁判          : {rep.get('referee') or '★ 没有裁判（这份 PDF 读不出印刷小节号）'}")
    if rep.get('musicxml'):
        m = rep['musicxml']
        A(f"  输出 part 数  : {m['parts']}")
        A(f"  输出小节/part : {m['measures_per_part']}")
        A(f"  输出音符      : {m['notes']}（<chord/> 成员 {m['chord_members']}）")
        A(f"  输出音高      : {m['pitches']}   休止 {m['rests']}")
        A(f"  连音记号      : {m['time_modifications']}")
    if rep.get('multirest_hidden') is not None:
        A(f"  多小节休止    : {rep['multirest_hidden']} 小节藏在粗横杠里"
          '（已按此展开）')
    pn = rep.get('printed_numbers') or {}
    if pn:
        A('')
        A(f"  {'页':>5}  {'印刷首号':>8}  {'本页应有小节数':>14}   印刷号(前8)")
        for p in sorted(pn):
            vs = (rep.get('vector_measures') or {}).get(p)
            A(f"  {p:>5}  {pn[p][0]:>8}  {str(vs) if vs is not None else '—':>14}"
              f"   {pn[p][:8]}")
    al = rep.get('alignment')
    if al:
        A('')
        A(f"  ★ 逐 system 对齐（纸上印的号 vs 我们的号）: "
          f"{al['hit']}/{al['total']} 命中")
        A(f"  {'sheet':>6}{'system':>7}{'印刷':>7}{'我们':>7}   判定")
        for sh, sy, a, b in al['rows']:
            A(f"  {sh:>6}{sy:>7}{a:>7}{b:>7}   {'OK' if a == b else '★ 不一致'}")
    for n in rep.get('notes', []):
        A('')
        A(f"  ⚠ {n}")
    A('=' * 68)
    return '\n'.join(L)
