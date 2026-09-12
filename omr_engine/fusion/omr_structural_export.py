"""Structural MusicXML export from an Audiveris ``.omr`` file.

This module reads the *full* Audiveris object structure (page / system / stack /
measure / voice / slot / head-chord / head / rest-chord / rest and the whole
``relation`` graph) and emits a valid MusicXML 3.1 ``score-partwise`` document.
It is a *structural* exporter: it preserves every measured object in the ``.omr``
exactly once and does not attempt to correct Audiveris' recognition errors.

Design notes (all of these were verified by measurement on
``data/test_rainbow/out/彩虹 青春 cl声部(1).omr``, see
``tools/test_structural_export.py`` for the reproducible numbers):

* ``<stack>`` elements live directly under ``<system>`` and have only ``<slot>``
  children -- they do **not** contain a ``<measure>`` subtree in this file.
  The k-th stack of a system pairs with the k-th ``<measure>`` of that system's
  ``<part>`` (verified 1:1 for all 19 systems).
* ``measure > head-chords`` / ``measure > rest-chords`` are whitespace-separated
  id lists that cover **every** chord object in the sheet exactly once
  (verified: 118/118, 39/39, 123/123 head-chords; 42/42, 7/7, 44/44 rest-chords,
  zero duplicates).  So chord -> measure membership comes from those lists, and
  x-coordinate-in-stack-span is only a *fallback*.
* ``head -> head-chord`` and ``rest -> rest-chord`` come from
  ``<relation><containment/></relation>`` (source=chord, target=head/rest).
  Verified 100% coverage with no multi-mapping, so no geometric guessing is
  needed to build chords.
* ``<slot>@x-offset`` is **relative to ``<stack>@left``** (verified: 178/178
  slots fit the relative reading, 0 fit the absolute reading).
* ``measure > voice > slots > entry`` gives ``<key>`` -> ``<value chord="CID"/>``;
  ``key`` is the ``<slot>@id`` of the matching stack slot, so the entry yields an
  exact ``time-offset``.  Verified: 0 measures have a key with no matching slot.
* onset is therefore: voice entry -> slot time-offset when the chord is
  referenced, otherwise snap the chord's x-centre to the nearest slot
  x-offset (``stack/@left + slot/@x-offset``) and use that slot's time-offset.
* ``stack/@duration == "0"`` marks an *empty* measure column.  Verified: all 37
  such stacks contain zero chord objects and all 117 others contain at least
  one.  They are **not** merged by default: the printed bar numbers show that
  four such stacks in sheet#1 system 7 are four separate bars, and the old
  merge-into-one default collapsed eight bars of system 8 into a single
  measure.  Pass ``merge_empty_stacks=True`` for the old behaviour.
* There is **no key-signature element anywhere** in these ``.omr`` files (the
  only ``<key>`` elements are ``<entry><key>N</key>`` slot keys).  ``<fifths>``
  is therefore 0 (C major / A minor) and that is an Audiveris limitation, not a
  guess this module made.
* The time signature comes from ``measure > times`` (ids resolved to
  ``<time-pair>``/``<time-whole>`` ``@time-rational``) with ``stack/@expected``
  as fallback (``"1"`` means 4/4, ``"3/4"`` means 3/4).  The clef comes from
  ``measure > clefs``.

Pitch and duration conversion are **reused** from the existing project code:

* ``omr_engine.fusion.audiveris_fusion.pitch_to_musicxml`` (Audiveris
  staff-relative diatonic ``head/@pitch`` -> MusicXML ``step``/``octave``/``alter``)
* ``omr_engine.fusion.audiveris_fusion.infer_duration`` (head shape + stem +
  flag + augmentation dot -> ``type``/duration)

They are imported directly (not copied).  ``omr_engine.fusion.pitch_utils`` is
*not* used: it converts a y-pixel to a pitch position, which is not needed here
because Audiveris already gives us ``head/@pitch`` directly.

Public API::

    export_structural_musicxml(omr_path, out_path=None, *, verbose=True)
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

import xml.etree.ElementTree as ET

try:  # optional: only needed for the notehead fill-ratio measurement
    import numpy as _np
    from PIL import Image as _Image
    _IMAGE_OK = True
except Exception:  # pragma: no cover
    _np = None
    _Image = None
    _IMAGE_OK = False

# ---------------------------------------------------------------------------
# Reuse the existing, already-verified pitch + duration conversion.
# ---------------------------------------------------------------------------
try:  # normal package import
    from omr_engine.fusion.audiveris_fusion import (
        infer_duration,
        pitch_to_musicxml,
    )
except ImportError:  # pragma: no cover - direct-script / odd sys.path fallback
    _ROOT = Path(__file__).resolve().parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from omr_engine.fusion.audiveris_fusion import (  # type: ignore
        infer_duration,
        pitch_to_musicxml,
    )


# divisions per quarter note.  24 is divisible by 2, 3, 4, 6, 8 and 12, so it
# represents whole/half/quarter/eighth/16th/32nd notes, dotted values and
# triplets exactly.
DIVISIONS = 24

# MusicXML type name -> whole-note fraction (quarter == DIVISIONS ticks).
TYPE_TO_WHOLE = {
    'whole': Fraction(1),
    'half': Fraction(1, 2),
    'quarter': Fraction(1, 4),
    'eighth': Fraction(1, 8),
    '16th': Fraction(1, 16),
    '32nd': Fraction(1, 32),
    '64th': Fraction(1, 64),
}

# Audiveris rest shapes carry the real rhythmic value; map them directly
# instead of falling back to infer_duration()'s "unknown shape -> quarter".
REST_SHAPE_TO_TYPE = {
    'WHOLE_REST': 'whole',
    'HALF_REST': 'half',
    'QUARTER_REST': 'quarter',
    'EIGHTH_REST': 'eighth',
    '16TH_REST': '16th',
    'THIRTY_SECOND_REST': '32nd',
    'SIXTY_FOURTH_REST': '64th',
}

# reverse lookup, for synthesising a rest element from a MusicXML type
_REST_TYPE_TO_SHAPE = {v: k for k, v in REST_SHAPE_TO_TYPE.items()}

# Audiveris clef kind -> (MusicXML sign, default line)
CLEF_KIND_TO_SIGN = {
    'TREBLE': ('G', 2),
    'BASS': ('F', 4),
    'ALTO': ('C', 3),
    'TENOR': ('C', 4),
}

_SHEET_RE = re.compile(r'^sheet#(\d+)/sheet#\1\.xml$')


def read_sheet_root(zf, name):
    """读一页的 ``sheet#N.xml`` 并解析；读不了/解析不了就返回 ``None``。

    Audiveris 判为无效的页（空白页、歪斜页、纯文字页）不会写出 ``<page>``，
    极端情况下还可能留下一个被截断甚至 0 字节的 XML。**一页坏不能拖垮整本**：
    调用方拿到 ``None`` 就跳过这一页。这条规则是有真实代价教训的 ——
    一本 92 页的谱子里 2 页无效，就足以让 Audiveris 的整本导出 exit 1、
    90 页好页的 MusicXML 一个都不给。
    """
    try:
        raw = zf.read(name)
    except KeyError:
        return None
    try:
        return ET.fromstring(raw.decode('utf-8', errors='ignore'))
    except ET.ParseError:
        return None


# voice used for chord objects that no measure > voice > slots > entry references
DEFAULT_VOICE = '1'

# ---------------------------------------------------------------------------
# Notehead fill ratio -> hollow / filled
# ---------------------------------------------------------------------------
# ``head/@shape`` cannot be used on this scan: it is NOTEHEAD_VOID for 448 of
# 449 heads while the pixels show a filled notehead for 316 of them.  Instead we
# measure the ink density inside each head's <bounds> in the sheet's own
# embedded BINARY.png.  That image is the exact raster Audiveris worked on:
# picture/@width/@height == BINARY.png size (3035x4299), so <bounds> need no
# rescaling at all.
FILL_ROW_CUT = 0.85    # a row this black is a staff line -- drop it first
FILL_PAD = 1           # px of padding around <bounds>
FILL_THRESHOLD_DEFAULT = 0.5184   # midpoint of the 2-means centres (measured)


def _measure_fill_ratios(omr_path):
    """Return ({(sheet_index, head_id): fill}, threshold, diagnostics).

    ``fill`` is black_pixels / remaining_pixels inside the head's padded
    <bounds>, after dropping rows whose black fraction reaches FILL_ROW_CUT
    (staff lines).  The threshold is the midpoint of a 1-D 2-means split of all
    measured values.  Returns (None, None, diag) when the raster is unavailable.
    """
    diag = {'fill_available': False}
    if not _IMAGE_OK:
        diag['fill_reason'] = 'numpy/Pillow not importable'
        return None, None, diag

    import io
    fills = {}
    values = []
    with zipfile.ZipFile(omr_path) as zf:
        names = set(zf.namelist())
        sheets = sorted((n for n in names if _SHEET_RE.match(n)), key=_sheet_sort_key)
        for idx, name in enumerate(sheets):
            png_name = f'sheet#{idx + 1}/BINARY.png'
            if png_name not in names:
                diag['fill_reason'] = f'{png_name} missing'
                continue
            # ★ 读不了的 BINARY.png 必须和"缺图"一样跳过。原来这里没有保护：
            #   .omr 里只要有一页图片损坏/被截断，整本导出就抛
            #   UnidentifiedImageError 直接崩掉（_sheet_ink 那边是保护了的）。
            try:
                img = _np.array(_Image.open(
                    io.BytesIO(zf.read(png_name))).convert('L'))
            except Exception:
                diag['fill_reason'] = f'{png_name} unreadable'
                continue
            H, W = img.shape
            root = read_sheet_root(zf, name)
            if root is None:
                diag['fill_reason'] = f'{name} unparseable'
                continue
            for head in root.iter('head'):
                b = head.find('bounds')
                if b is None:
                    continue
                x0, y0 = float(b.get('x')), float(b.get('y'))
                w, h = float(b.get('w')), float(b.get('h'))
                xa, ya = max(0, int(x0) - FILL_PAD), max(0, int(y0) - FILL_PAD)
                xb = min(W, int(round(x0 + w)) + FILL_PAD)
                yb = min(H, int(round(y0 + h)) + FILL_PAD)
                if xb <= xa or yb <= ya:
                    continue
                sub = (img[ya:yb, xa:xb] == 0)
                keep = sub.mean(axis=1) < FILL_ROW_CUT
                kept = sub[keep]
                if not kept.size:
                    continue
                v = float(kept.mean())
                fills[(idx, head.get('id'))] = v
                values.append(v)

    if len(values) < 10:
        diag['fill_reason'] = f'only {len(values)} heads measurable'
        return fills or None, FILL_THRESHOLD_DEFAULT, diag

    arr = _np.array(values)
    c = _np.array([_np.percentile(arr, 15), _np.percentile(arr, 85)])
    for _ in range(200):
        lab = _np.argmin(_np.abs(arr[:, None] - c[None, :]), axis=1)
        new = _np.array([arr[lab == k].mean() if (lab == k).any() else c[k]
                         for k in range(2)])
        if _np.allclose(new, c):
            break
        c = new
    lo, hi = sorted(c.tolist())
    thr = (lo + hi) / 2.0
    g0, g1 = arr[arr < thr], arr[arr >= thr]
    pooled = float(_np.sqrt(((len(g0) - 1) * g0.var(ddof=1)
                             + (len(g1) - 1) * g1.var(ddof=1))
                            / max(1, len(g0) + len(g1) - 2)))
    diag.update({
        'fill_available': True,
        'fill_n': len(values),
        'fill_mean': round(float(arr.mean()), 4),
        'fill_median': round(float(_np.median(arr)), 4),
        'fill_std': round(float(arr.std()), 4),
        'fill_centres': [round(lo, 4), round(hi, 4)],
        'fill_threshold': round(thr, 4),
        'fill_cohens_d': round(abs(g1.mean() - g0.mean()) / pooled, 2)
        if pooled else None,
        'fill_hollow': int((arr < thr).sum()),
        'fill_filled': int((arr >= thr).sum()),
    })
    return fills, thr, diag


# whole-note duration -> the (type, dots) that represents it exactly
_DURATION_TABLE = (
    (Fraction(1), 'whole', 0),
    (Fraction(3, 4), 'half', 1),
    (Fraction(1, 2), 'half', 0),
    (Fraction(3, 8), 'quarter', 1),
    (Fraction(1, 4), 'quarter', 0),
    (Fraction(3, 16), 'eighth', 1),
    (Fraction(1, 8), 'eighth', 0),
    (Fraction(1, 16), '16th', 0),
)


def _type_for_duration(value):
    """Nearest standard (type, dots, exact-value) for a whole-note duration."""
    best = min(_DURATION_TABLE,
               key=lambda row: abs(float(row[0]) - float(value)))
    return best[1], best[2], best[0]

# Relation child tag -> (source role, target role); documentation only, the
# exporter looks relations up by child tag directly.
RELATION_ROLES = {
    'containment': 'chord -> head / rest',
    'head-stem': 'head -> stem',
    'chord-stem': 'head-chord -> stem',
    'flag-stem': 'flag -> stem',
    'augmentation': 'augmentation-dot -> head',
    'double-dot': 'augmentation-dot -> augmentation-dot',
    'alter-head': 'alter -> head',
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _bounds(el):
    """Return (x, y, w, h) floats for an element's <bounds>, or None."""
    if el is None:
        return None
    b = el.find('bounds')
    if b is None:
        return None
    try:
        return (float(b.get('x')), float(b.get('y')),
                float(b.get('w')), float(b.get('h')))
    except (TypeError, ValueError):
        return None


def _center_x(el):
    b = _bounds(el)
    if b is None:
        return None
    return b[0] + b[2] / 2.0


def _center_y(el):
    b = _bounds(el)
    if b is None:
        return None
    return b[1] + b[3] / 2.0


def _as_int(text, default=0):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        pass
    try:  # Audiveris writes some attributes as floats (e.g. pitch="-0.4")
        return int(round(float(str(text).strip())))
    except (TypeError, ValueError):
        return default


def _fraction(text, default=None):
    """Parse an Audiveris rational like ``"3/8"`` or ``"0"``."""
    if text is None:
        return default
    text = str(text).strip()
    if not text:
        return default
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return default


def _sheet_sort_key(name):
    m = _SHEET_RE.match(name)
    return int(m.group(1)) if m else 1 << 30


# Audiveris shape -> MusicXML element.  Anything not listed is counted in
# ``unmapped_*`` stats rather than silently dropped.
_ARTICULATION_TAGS = {
    'STACCATO': 'staccato',
    'STACCATISSIMO': 'staccatissimo',
    'ACCENT': 'accent',
    'STRONG_ACCENT': 'strong-accent',
    'MARCATO': 'strong-accent',
    'TENUTO': 'tenuto',
    'PORTATO': 'detached-legato',
    'STACCATO_TENUTO': 'detached-legato',
    'DETACHED_LEGATO': 'detached-legato',
    'Sforzando': 'accent',
    'SFORZATO': 'accent',
}

_DYNAMICS_TAGS = {
    'DYNAMICS_PPP': 'ppp', 'DYNAMICS_PP': 'pp', 'DYNAMICS_P': 'p',
    'DYNAMICS_MP': 'mp', 'DYNAMICS_MF': 'mf', 'DYNAMICS_F': 'f',
    'DYNAMICS_FF': 'ff', 'DYNAMICS_FFF': 'fff', 'DYNAMICS_FFFF': 'ffff',
    'DYNAMICS_FP': 'fp', 'DYNAMICS_SF': 'sf', 'DYNAMICS_SFZ': 'sfz',
    'DYNAMICS_SFP': 'sfp', 'DYNAMICS_SFFZ': 'sffz', 'DYNAMICS_FZ': 'fz',
    'DYNAMICS_RF': 'rf', 'DYNAMICS_RFZ': 'rfz', 'DYNAMICS_PIU_F': 'f',
}

_TUPLET_RATIOS = {
    'TUPLET_THREE': (3, 2),
    'TUPLET_FIVE': (5, 4),
    'TUPLET_SIX': (6, 4),
    'TUPLET_SEVEN': (7, 4),
}

_WEDGE_TAGS = {
    'CRESCENDO': 'crescendo',
    'DIMINUENDO': 'diminuendo',
    'DECRESCENDO': 'diminuendo',
}


# ---------------------------------------------------------------------------
# Multi-measure rests
# ---------------------------------------------------------------------------
# Audiveris does not recognise the multi-measure-rest symbol at all.  Measured
# on the rainbow part: sheet#1 contains 0 multi-rest shapes, only EIGHTH_REST
# (x33) and QUARTER_REST (x9) -- so the "first 9 bars" of the part, drawn as one
# thick bar with "9" printed above it, vanish entirely, and the surviving
# duration="0" stacks get collapsed into a single empty measure.
#
# The symbol is recovered out of band by tools/find_multirests.py (thick bar
# between staff lines 2 and 4 with uniform ink) plus a 3x OCR of the printed bar
# count, then confirmed by eye -- see results/multirests_verified.json.
DEFAULT_MULTI_REST_JSON = (Path(__file__).resolve().parents[2] / 'results'
                           / 'multirests_verified.json')

# ★ Tuplet digit positions, annotated by the user through
# results/tuplet_click.html (see docs section 9.7).  This is the ONLY tuplet
# source that works: the source PDF uses an omitted notation, so most tuplets
# have no printed bracket or number to detect.
DEFAULT_TUPLET_JSON = (Path(__file__).resolve().parents[2] / 'results'
                       / 'tuplet_annotations.json')


def load_multi_rests(path=None):
    """``{(sheet_dir, system_id): [(x0, x1, bar_count), ...]}``.

    ``sheet_dir`` is the zip directory name, e.g. ``sheet#1``.  Returns an empty
    mapping (and therefore changes nothing) when the sidecar file is absent, so
    the exporter still runs standalone.
    """
    path = Path(path) if path is not None else DEFAULT_MULTI_REST_JSON
    out = {}
    if not path.exists():
        return out
    try:
        rows = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return out
    for row in rows:
        try:
            key = (str(row['sheet']), str(row['system']))
            out.setdefault(key, []).append(
                (int(row['x0']), int(row['x1']), int(row['number'])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _sheet_signature(model):
    """Layout fingerprint, used as a fallback when images are unavailable.

    Note this is *not* enough on its own: measured on the rainbow part the two
    scans of the same page differ structurally (sheet#1 system 3 has 7 stacks,
    sheet#3 system 3 has 6), even though 99.9% of their ink coincides.  Image
    comparison is the reliable test -- see :func:`_sheet_ink`.
    """
    if model.page is None:
        return ()
    sig = []
    for system in model.page.findall('system'):
        stacks = system.findall('stack')
        bounds = sorted({_as_int(s.get('left')) for s in stacks}
                        | {_as_int(s.get('right')) for s in stacks})
        pitches = [h.get('pitch') for h in system.iter('head')]
        sig.append((len(stacks), bounds, pitches))
    return tuple(sig)


def _sheet_ink(zf, sheet_key):
    """``bool`` ink mask of ``<sheet_key>/BINARY.png``, or ``None``."""
    if not _IMAGE_OK:
        return None
    try:
        raw = zf.read(f'{sheet_key}/BINARY.png')
    except KeyError:
        return None
    try:
        img = _Image.open(io.BytesIO(raw)).convert('L')
    except Exception:
        return None
    return _np.asarray(img) < 128


def _dilate(mask, tol):
    """``mask`` 的 (2*tol+1) 方形膨胀，用可分离的 1-D 最大值实现。

    原来用的是 PIL 的 ``MaxFilter``：整页 3456x4962 = 1700 万像素、7x7 窗口，
    单次调用本身就很贵；而去重循环对【每一对】页面都要重算一次 ``b`` 的膨胀
    （92 页 -> 约 4200 对 -> 8000+ 次全页膨胀），实测让一本 92 页的谱子导出
    跑了 12 分钟以上、内存冲到 5.8 GB。这里改成先横向后纵向的取最大值，
    整页操作次数从 49 降到 6，且不再依赖 PIL。
    """
    out = mask
    for axis in (0, 1):
        acc = out
        for k in range(1, tol + 1):
            acc = acc | _np.roll(out, k, axis=axis) | _np.roll(out, -k, axis=axis)
        out = acc
    return out


def _ink_overlap(a, b, tol=3):
    """Fraction of ``a``'s ink lying within ``tol`` px of ``b``'s ink.

    Deliberately tolerant: the two scans of one page in the rainbow PDF are
    offset by a pixel or two, so an exact AND would report ~0.5 for what is
    really the same music.
    """
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    na = int(a.sum())
    if na == 0:
        return 0.0
    bd = _dilate(b, tol)
    return float((a & bd).sum()) / float(na)


# 去重只和前 WINDOW 页比。原先是全对全（O(n^2)）：一本 92 页的谱子有 4186
# 个页对，每对要做 2 次整页膨胀 + 整页 AND，实测导出卡 12 分钟以上、内存冲到
# 5.8 GB —— 而这本书里一个重复页都没有。重复页在真实 PDF 里都是【局部】现象
# （同一页被重复插入、装订错页），所以只和前面 32 页比既保住了已知用例
# （彩虹谱 3 页，第 1、3 页重复），又把成本压到 O(n*32)。
# 已知边界：相隔超过 32 页的重复页不会被发现 —— 相比卡死 12 分钟，这是划算的。
DEDUP_WINDOW = 32


# ---------------------------------------------------------------------------
# Miss recovery
# ---------------------------------------------------------------------------
# Audiveris silently fails on some columns -- measured on the rainbow part,
# sheet#1 system 8 has 0 <head> elements while the score plainly shows five
# bars of accented sustained notes.  tools/build_recovery.py recovers those by
# running the YOLO detector (runs/.../p2_rainbow/v1) over the page and keeping
# only the noteheads that
#   * do not match any <head> Audiveris already found (x within 22px),
#   * fall in a stack Audiveris left empty (duration="0", no slots),
#   * are not sitting on a recovered multi-measure-rest bar,
#   * pass a confidence and a glyph-size filter.
# Pitch comes from the notehead's y measured against the *locally interpolated*
# staff lines: the scans are skewed by 24-31px across the page (2-2.7 staff
# steps), so a whole-line average gets 44% of pitches right while local
# interpolation gets 260/260.
DEFAULT_RECOVERY_JSON = (Path(__file__).resolve().parents[2] / 'results'
                         / 'recovered_notes.json')
DEFAULT_BEAMS_JSON = (Path(__file__).resolve().parents[2] / 'results'
                      / 'detections_p2rainbow.json')
DEFAULT_TEXT_JSON = (Path(__file__).resolve().parents[2] / 'results'
                     / 'text_directions.json')
# Read off the score itself (page 1 title block, OCR confidence 1.000).
DEFAULT_PART_NAME = 'Clarinet (A)'
# Also read off the score's title block (OCR + visual confirmation).
DEFAULT_WORK_TITLE = '青春（一）'
DEFAULT_SUBTITLE = '选自影片《世纪之梦》'
DEFAULT_COMPOSER = '施万春 赵小也'


def load_text_directions(path=None):
    """``{sheet_dir: [(x, y, kind, text, bpm), ...]}`` from the OCR sidecar.

    Built by tools/build_text_directions.py, which keeps only boxes that are
    tempo / a known dynamic / a plausible word -- full-resolution OCR puts a lot
    of single-character noise on noteheads (measured: 178 of 224 candidates).
    """
    path = Path(path) if path is not None else DEFAULT_TEXT_JSON
    out = {}
    if not path.exists():
        return out
    try:
        rows = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return out
    for r in rows:
        try:
            out.setdefault(str(r['sheet']), []).append(
                (float(r['x']), float(r['y']), str(r['kind']),
                 str(r['text']), int(r.get('bpm') or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return out

# note types that can carry a beam (MusicXML: one <beam number="1"> per level)
_BEAMABLE = {'eighth': 1, '16th': 2, '32nd': 3, '64th': 4}


def load_beam_boxes(path=None):
    """``{sheet_dir: [(x1, x2, y_centre), ...]}`` -- raw detector beam boxes.

    Clustering into groups and binding them to notes is done in pass 1, where
    the system's staff band and the chords' x/y are known.  The detector reports
    one box per beam *stroke*, so a sextuplet may contribute two boxes here.
    """
    path = Path(path) if path is not None else DEFAULT_BEAMS_JSON
    out = {}
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return out
    for sheet, rows in data.items():
        out[sheet] = [(float(r['x1']), float(r['x2']),
                       (float(r['y1']) + float(r['y2'])) / 2.0)
                      for r in rows if r.get('cls') == 'beam']
    return out


def load_tuplet_digits(path):
    """``{sheet_key: [(system_id, x, digit), ...]}`` from the click annotation.

    The annotation file is ``results/tuplet_annotations.json``; only entries
    carrying a real tuplet number are used (the same file also records measure
    numbers, time signatures and multi-rest counts, which are not tuplets).
    """
    if not path:
        # ★ Default ON since 2026-09-11: the re-timing + guards were verified
        # to keep Render% = 100.0% and 拍号守恒 = 103/103 (docs 9.10/9.11).
        # Earlier this was opt-in because emitting <time-modification> without
        # re-deriving the note values made MuseScore reject the whole file
        # (exit 1320).  A missing file simply means no tuplets.
        path = DEFAULT_TUPLET_JSON
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    out = {}
    for r in raw.get('real_tuplet_digits', ()):
        d = r.get('digit')
        if not isinstance(d, int) or d < 2:
            continue
        out.setdefault(r.get('sheet', 'sheet#1'), []).append(
            (str(r.get('system')), float(r['x']), d))
    return out


def _tuplet_marks(events, digits, system_id, stats=None, beat_ticks=None):
    """Annotated tuplets: re-derive the group's note values, then mark it.

    Returns ``{cid: (actual, normal, role)}`` where role is
    ``'start'|'mid'|'stop'``.

    ``digits`` is ``[(system_id, x, digit), ...]`` from the user's click
    annotation.  The printed number sits over its group, so the group is taken
    to be the ``digit`` events nearest it in x.

    ★ Two guards learned the hard way (docs 9.10):

    1. **The digit must belong to THIS measure.**  Filtering by system alone
       applied every digit of the system to every measure of the system --
       measured m10 growing from 72 to 84 ticks.  A digit now qualifies only
       when its x falls inside the measure's own note span (plus a margin).
    2. **A tuplet group cannot hog the bar.**  If the N nearest notes span more
       than two beats, the measure's content is not what the digit describes
       (usually because notes are missing) and re-valuing them is wrong.

    ★ The note values MUST be re-derived, otherwise the file is invalid.
    MusicXML requires ``<duration> == <type> x normal/actual``; the measure
    solver fills the bar with *standard* values, so a triplet eighth came out
    as 12 ticks instead of 8 and MuseScore rejected the whole file
    (Render% 0.0%, exit 1320 -- see docs 9.8).

    The re-derivation is exact and preserves the barline::

        normal   = 2 ** floor(log2(N))
        std      = total / normal          (must be a plain note value)
        each     = std * normal / N
        N * each = std * normal = total    -> the measure sum is unchanged

    Measured: triplet of eighths (total 24, N=3, normal=2) -> std 12 -> 8 each;
    sextuplet (total 24, N=6, normal=4) -> std 6 -> 4 each.
    """
    out = {}
    applied = []
    mine = [d for d in digits if str(d[0]) == str(system_id)]
    if not mine:
        return out
    pool = [e for e in events if e.kind != 'rest' and e.x]
    _snap = [(e, e.note_type, e.dots, e.tuplet_ticks) for e in events]
    if len(pool) < 3:
        return out
    # ★ guard 1: a digit belongs to this measure only if it sits inside the
    # measure's own note span (the printed number is over its group).
    ev_x0 = min(e.x for e in pool)
    ev_x1 = max(e.x for e in pool)
    here = [d for d in mine if ev_x0 - 45 <= d[1] <= ev_x1 + 45]
    if not here:
        return out
    for _sid, dx, n in here:
        n = max(2, int(n))
        near = sorted(pool, key=lambda e: abs(e.x - dx))[:n]
        if len(near) < 3:
            continue
        near.sort(key=lambda e: e.x)
        # the group must be contiguous in x-order, else re-timing would
        # displace notes that are not part of it
        idx = [events.index(e) for e in near]
        if idx != list(range(min(idx), min(idx) + len(idx))):
            if stats is not None:
                stats['tuplet_group_not_contiguous'] += 1
            continue
        N = len(near)
        normal = 2 ** int(math.floor(math.log2(N)))
        # ★ 2026-09-12 (Track E): a member's REAL length is its tuplet_ticks when
        # it already carries one (a re-timed member, or a precise-channel note).
        # Using the bare note value here under-counted a group whose members had
        # just been lengthened by the previous digit, and the measure then lost
        # 6 ticks (measured: m11 came out 2.75 of 3.0 quarters, Render% 0.0).
        total = sum((e.tuplet_ticks if e.tuplet_ticks
                     else _type_to_ticks(e.note_type, e.dots, stats))
                    for e in near)
        # ★ guard 2: a tuplet group spans at most two beats
        if beat_ticks and total > 2 * beat_ticks + 1:
            if stats is not None:
                stats['tuplet_group_spans_too_much'] += 1
            continue
        note_type = dots = None
        each = 0
        # ---- path 1: the ORIGINAL rule, kept verbatim so every tuplet that
        # already worked keeps byte-identical output.
        if normal > 0 and total > 0 and not total % normal:
            std = total // normal
            # ★ prefer an UNDOTTED value: a sextuplet is six 16ths, not six
            # dotted eighths, even though both are arithmetically consistent
            cand = [(t, d, tk) for t, d, tk in _DUR_LATTICE
                    if tk == std and d == 0]
            if not cand:
                cand = [(t, d, tk) for t, d, tk in _DUR_LATTICE if tk == std]
            if cand and not (std * normal) % N:
                note_type, dots, _tk = cand[0]
                _each = std * normal // N
                if _each > 0 and _each * N == total:
                    each = _each
                else:
                    note_type = dots = None
        # ---- path 2 (2026-09-12, Track E): the PRINTED digit is the authority.
        # The old rule demanded `std = total/normal` be a lattice value and so
        # threw away 5 of the 7 annotated tuplets (measured: std 9 and 15 fall
        # between two legal values).  That requirement is NOT what keeps the
        # barline -- what keeps it is that the group's own sum never changes.
        # So: share `total` equally among the N members, then take the notated
        # value whose tick count divides the group total; `normal = total/ticks`
        # makes duration = ticks*normal/N = total/N exactly.
        if note_type is None:
            if total > 0 and N > 0 and not total % N:
                _each = total // N
                _want = _each * N
                _best = None
                for _t, _d, _tk in _TUPLET_NOTATION:
                    if _tk <= 0 or _want % _tk:
                        continue
                    _nn = _want // _tk
                    if _nn <= 0 or _nn == N:
                        continue        # "N in the time of N" is not a tuplet
                    _key = (0 if _nn == normal else 1, abs(_tk - _each), _d)
                    if _best is None or _key < _best[0]:
                        _best = (_key, _t, _d, _tk, _nn)
                if _best is None:
                    # last resort: allow the degenerate ratio; the printed digit
                    # still shows and the group's sum is still untouched
                    for _t, _d, _tk in _TUPLET_NOTATION:
                        if _tk <= 0 or _want % _tk:
                            continue
                        _nn = _want // _tk
                        if _nn <= 0:
                            continue
                        _key = (1, abs(_tk - _each), _d)
                        if _best is None or _key < _best[0]:
                            _best = (_key, _t, _d, _tk, _nn)
                if _best is not None:
                    note_type, dots, normal = _best[1], _best[2], _best[4]
                    each = _best[3] * normal // N
                    if stats is not None:
                        stats['tuplet_retime_via_digit'] += 1
        if note_type is None or each <= 0 or each * N != total:
            if stats is not None:
                stats['tuplet_retime_skipped'] += 1
            continue
        # ★ apply: re-value the group.  The sum is unchanged, so 拍号守恒 is
        # preserved by construction.
        _pre = {id(e): (e.note_type, e.dots, e.tuplet_ticks) for e in near}
        for e in near:
            e.note_type, e.dots = note_type, dots
            e.tuplet_ticks = each
        if stats is not None:
            stats['tuplet_groups_retimed'] += 1
            stats[f'tuplet_ratio_{N}_{normal}'] += 1
        for k, e in enumerate(near):
            role = 'start' if k == 0 else ('stop' if k == N - 1 else 'mid')
            out[e.cid] = (N, normal, role)
        applied.append((near, (N, normal), _pre))
    # ★★ completeness guard (2026-09-12, Track E).
    # A tuplet must end up with exactly `actual-notes` members on the page.
    # The two annotated figures of one measure overlap in x (measured: m10 and
    # m11 both share notes between the sextuplet and the following triplet), so
    # the later figure overwrites part of the earlier group.  m10 survives
    # because its sextuplet's 6th member is a REST that carries the same ratio;
    # m11's does not, and MuseScore rejects the whole file (exit 1320,
    # Render% 0.0).  Undo any group that would end up incomplete.
    for _members, (_N, _nn), _pre in applied:
        _cnt = sum(1 for e in events
                   if out.get(getattr(e, 'cid', None), (None, None))[:2] == (_N, _nn))
        _cnt += sum(1 for e in events if e.kind == 'rest'
                    and getattr(e, 'precise_ratio', (None, None)) == (_N, _nn))
        if _cnt == _N:
            continue
        for _e in _members:
            _e.note_type, _e.dots, _e.tuplet_ticks = _pre[id(_e)]
            out.pop(_e.cid, None)
        if stats is not None:
            stats['tuplet_groups_retimed'] -= 1
            stats[f'tuplet_ratio_{_N}_{_nn}'] -= 1
            stats['tuplet_group_incomplete'] += 1
    # ★★ measure-sum guard (2026-09-12, Track E).
    # 拍号守恒 is the one invariant a re-timing must never break, and the two
    # annotated figures of one measure overlap in x, so an earlier group's
    # members can be re-valued by a later one.  Measure the whole measure
    # before/after and undo EVERYTHING for it if a single tick moved.
    _sum0 = sum((_tk if _tk else _type_to_ticks(_t, _d, None))
                for _e, _t, _d, _tk in _snap)
    _sum1 = sum((e.tuplet_ticks if e.tuplet_ticks
                 else _type_to_ticks(e.note_type, e.dots, None))
                for e in events)
    if _sum1 != _sum0:
        for _e, _t, _d, _tk in _snap:
            _e.note_type, _e.dots, _e.tuplet_ticks = _t, _d, _tk
        out.clear()
        if stats is not None:
            stats['tuplet_measure_sum_guard'] += 1
            stats['tuplet_groups_retimed'] -= len(applied)
    return out


DEFAULT_NAMES_JSON = (Path(__file__).resolve().parents[2] / 'results'
                      / 'instrument_names.json')

# default clef per staff number for a part that owns several staves; the piano
# is the only such part in this score (treble above, bass below)
_STAFF_CLEFS = {2: ('F', 4), 3: ('F', 4), 4: ('F', 4)}


def load_instrument_names(path=None):
    """``[name, ...]`` top-to-bottom, read from the source PDF's own text.

    The score prints each instrument name in a narrow left column, so they are
    vector text -- no OCR needed.  Ordering by ``y`` gives the score order.
    A missing file yields ``[]`` and callers fall back to ``Part N``.
    """
    path = Path(path) if path is not None else DEFAULT_NAMES_JSON
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    rows = data.get('names') if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if isinstance(r, dict) and r.get('name'):
            out.append(str(r['name']))
        elif isinstance(r, str):
            out.append(r)
    return out


DEFAULT_PERCUSSION_JSON = (Path(__file__).resolve().parents[2] / 'results'
                           / 'perc_notes.json')


def _percussion_display_name(percussion, instr_names):
    """Printed name for the percussion voice.

    Prefers the score's own printed name so the part reads like the original;
    falls back to the sidecar's own label, then to a neutral placeholder.  Never
    invents an instrument name.
    """
    if instr_names and '小军鼓' in instr_names:
        return '小军鼓'
    return (percussion or {}).get('part_name') or 'Percussion'


def load_percussion_part(path=None):
    """Percussion voice for a single-line staff, or ``None``.

    Audiveris cannot read a one-line percussion staff at all: measured on
    score3 ``sheet#2`` it merges that staff's noteheads into two coarse blobs,
    where a five-line staff yields clean ``18x16`` heads.  So this voice comes
    from the project's own detector, not from the ``.omr``.

    The sidecar is optional -- with no file this returns ``None`` and the export
    is byte-identical to before.
    """
    path = Path(path) if path is not None else DEFAULT_PERCUSSION_JSON
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get('measures'):
        return None
    return data


def _percussion_lines(perc, stats, pad_to=0):
    """MusicXML lines for one percussion ``<part>`` (a whole single-line voice).

    ``pad_to`` pads the part with empty measures so every ``<part>`` in the
    document has the SAME measure count.  That is not cosmetic: measured with
    MuseScore 4, a part holding fewer measures than its siblings is rejected
    outright (the percussion voice had 7 where the other 18 had 12).
    """
    out = []
    div = int(perc.get('divisions') or DIVISIONS)
    clef = perc.get('clef') or {}
    sign = clef.get('sign') or 'percussion'
    line = clef.get('line') or 2
    lines_n = int(perc.get('staff_lines') or 1)
    out.append(f'    <!-- percussion voice from the project detector '
               f'({perc.get("sheet")} staff_y={perc.get("staff_y")}); '
               f'Audiveris cannot read a {lines_n}-line staff -->')
    measures = list(perc['measures'])
    n_have = len(measures)
    # pad so this part matches the others (see docstring)
    while len(measures) < pad_to:
        measures.append({'number': len(measures) + 1,
                         'whole_rest': div * 4, 'pad': True})
    for i, m in enumerate(measures, start=1):
        stats['perc_measures_emitted'] += 1
        out.append(f'    <measure number="{m.get("number", i)}">')
        if i == 1:
            ts = str(perc.get('time_sig') or '4/4').split('/')
            out += [
                '      <attributes>',
                f'        <divisions>{div}</divisions>',
                '        <key><fifths>0</fifths></key>',
                '        <time>',
                f'          <beats>{int(ts[0])}</beats>',
                f'          <beat-type>{int(ts[1])}</beat-type>',
                '        </time>',
                '        <staves>1</staves>',
                f'        <clef><sign>{sign}</sign><line>{line}</line></clef>',
                '        <staff-details number="1">',
                f'          <staff-lines>{lines_n}</staff-lines>',
                '        </staff-details>',
                '      </attributes>',
            ]
        if m.get('whole_rest'):
            stats['perc_rests_emitted'] += 1
            out += [
                '      <note>',
                '        <rest measure="yes"/>',
                f'        <duration>{int(m["whole_rest"])}</duration>',
                '        <voice>1</voice>',
                '        <type>whole</type>',
                '      </note>',
            ]
        for n in m.get('notes', ()):
            stats['perc_notes_emitted'] += 1
            out += [
                '      <note>',
                '        <unpitched><display-step>B</display-step>'
                '<display-octave>4</display-octave></unpitched>',
                f'        <duration>{int(n["duration"])}</duration>',
                '        <voice>1</voice>',
                f'        <type>{n["type"]}</type>',
            ]
            if n.get('notehead') == 'x':
                out.append('        <notehead>x</notehead>')
                stats['perc_x_noteheads'] += 1
            trem = n.get('tremolo')
            if trem:
                # single-note tremolo: N = number of slashes (measured: 2)
                out += [
                    '        <notations>',
                    '          <ornaments>',
                    f'            <tremolo type="single">{int(trem)}</tremolo>',
                    '          </ornaments>',
                    '        </notations>',
                ]
                stats['perc_tremolos_emitted'] += 1
            out.append('      </note>')
        out.append('    </measure>')
    return out


def _beam_marks_from_omr(model, events, stats=None):
    """``{cid: 'begin'|'continue'|'end'}`` taken from the ``.omr`` beam graph.

    The detector boxes are keyed by sheet only, so on a full score (N ``<part>``
    per ``<system>``, all sharing one sheet) they cannot say which voice a beam
    belongs to -- measured: 2 beams emitted for the whole 18-part score.  The
    file itself links them exactly:

        <beam id> --beam-stem--> <stem id> <--chord-stem-- <head-chord id>

    so grouping by that chain is voice-correct by construction.  Returns an
    empty mapping when the sheet has no identified beams, which is the case for
    the rainbow part (its ``<beam>`` elements carry no id) -- there the caller
    falls back to the detector as before.
    """
    if model is None or not model.beam_stems:
        return {}
    stem_to_chords = defaultdict(list)
    for cid, sid in model.chord_to_stem.items():
        stem_to_chords[sid].append(cid)
    known = {e.cid for e in events}
    groups = []
    for bid, stems in model.beam_stems.items():
        cids = []
        for sid in stems:
            cids.extend(stem_to_chords.get(sid, ()))
        cids = [c for c in cids if c in known]
        if len(cids) >= 2:
            groups.append(cids)
    if not groups:
        return {}
    by_x = {e.cid: (e.x if e.x is not None else 0.0) for e in events}
    marks = {}
    for g in groups:
        ordered = sorted(set(g), key=lambda c: by_x.get(c, 0.0))
        for i, cid in enumerate(ordered):
            role = ('begin' if i == 0 else
                    'end' if i == len(ordered) - 1 else 'continue')
            if marks.get(cid) in (None, 'continue'):
                marks[cid] = role
    if stats is not None:
        stats['beams_from_omr'] += len([m for m in marks.values()
                                        if m == 'begin'])
    return marks


def _beam_marks(events, boxes, staff_lo, staff_hi, gap=45):
    """``{cid: 'begin'|'continue'|'end'}`` for the notes under a beam stroke.

    Only notes whose type can carry a beam take part; a group needs at least two
    of them, otherwise it is a flag and MusicXML derives that from <type>.
    """
    if not boxes:
        return {}
    band = [b for b in boxes if staff_lo - 70 <= b[2] <= staff_hi + 70]
    if not band:
        return {}
    band.sort(key=lambda b: b[0])
    groups = []
    for x1, x2, _y in band:
        if groups and x1 - groups[-1][1] <= gap:
            groups[-1][1] = max(groups[-1][1], x2)
        else:
            groups.append([x1, x2])
    marks = {}
    for gx0, gx1 in groups:
        members = [e for e in events
                   if e.kind != 'rest' and e.note_type in _BEAMABLE
                   and gx0 - 18 <= e.x <= gx1 + 18]
        if len(members) < 2:
            continue
        members.sort(key=lambda e: e.x)
        for i, e in enumerate(members):
            marks[e.cid] = ('begin' if i == 0 else
                            'end' if i == len(members) - 1 else 'continue')
    return marks


def load_recovered_notes(path=None):
    """``{(sheet_dir, system_id, stack_id): [note, ...]}``.

    Empty mapping (and therefore no change) when the sidecar is absent.
    """
    path = Path(path) if path is not None else DEFAULT_RECOVERY_JSON
    out = {}
    if not path.exists():
        return out
    try:
        rows = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return out
    for row in rows:
        try:
            key = (str(row['sheet']), str(row['system']), str(row['stack']))
        except (KeyError, TypeError):
            continue
        out.setdefault(key, []).extend(
            n for n in row.get('notes', ()) if n.get('pitch_raw') is not None)
    return out


def _recovered_events(model, unit, recovered, sheet_key, system_id, stats):
    """Synthetic chord events for recovered noteheads.

    Builds real ``head`` / ``head-chord`` elements so the rest of the pipeline
    (pitch -> step/octave, the per-voice duration solver) treats them exactly
    like Audiveris' own.  Returns ``[]`` when this unit has nothing recovered.
    """
    events = []
    for stack, _measure in unit:
        rows = recovered.get((sheet_key, str(system_id), str(stack.get('id'))))
        if not rows:
            continue
        cid = 'recovered-%s-%s' % (system_id, stack.get('id'))
        if cid in model.head_chords:
            continue
        heads = []
        for n in rows:
            hid = 'recovered-h-%s-%s-%d' % (system_id, stack.get('id'),
                                            int(float(n['x'])))
            if hid in model.heads:
                continue
            el = ET.Element('head')
            el.set('id', hid)
            el.set('pitch', str(int(n['pitch_raw'])))
            el.set('shape', 'NOTEHEAD_VOID')
            el.set('staff', '1')
            b = ET.SubElement(el, 'bounds')
            b.set('x', str(int(float(n['x']) - 12)))
            b.set('y', str(int(float(n['y']) - 12)))
            b.set('w', '24')
            b.set('h', '24')
            model.heads[hid] = el
            model.head_to_chord[hid] = cid
            model.chord_heads[cid].append(hid)
            heads.append(hid)
        if not heads:
            continue
        chel = ET.Element('head-chord')
        chel.set('id', cid)
        b = ET.SubElement(chel, 'bounds')
        xs = [float(n['x']) for n in rows]
        ys = [float(n['y']) for n in rows]
        b.set('x', str(int(min(xs) - 12)))
        b.set('y', str(int(min(ys) - 12)))
        b.set('w', str(int(max(xs) - min(xs) + 24)))
        b.set('h', str(int(max(ys) - min(ys) + 24)))
        model.head_chords[cid] = chel
        model.by_id[cid] = chel
        # NOTE: onset stays Fraction(0) here on purpose.
        # Deriving it from x (`_x_onset`) was tried and MEASURED to change the
        # output of existing recovered notes (e.g. measure 30 went from
        # "A5 A5 F5 C5" to "F5 A5 C5 ... A5", 10 measures affected) without
        # improving anything -- the duration solver does not need the hint.
        # Keeping the original value preserves the established behaviour; the
        # precise channel below carries an explicit onset when one is needed.
        events.append(_ChordEvent(cid, False, Fraction(0), 'recovered',
                                  sum(xs) / len(xs), None, heads, []))
        stats['recovered_events'] += len(heads)
        # the integrity assertion compares emitted heads against heads_input,
        # so the injected ones must be counted as input as well
        stats['heads_input'] += len(heads)
    return events


# ---------------------------------------------------------------------------
# precise insertion channel (additive, independent of _recovered_events)
#
# Why this exists: `_recovered_events` builds ONE chord per stack, so it cannot
# place a note at a chosen beat inside the measure -- every recovered note of a
# column lands on the same onset.  That is fine for whole-column dyads but wrong
# for a melodic figure that spans several beats.
#
# This channel keeps one chord PER NOTE and assigns each note an explicit onset,
# so `x` decides the time position.  It is a separate sidecar and a separate code
# path: when the file is absent nothing changes at all.
# ---------------------------------------------------------------------------

DEFAULT_PRECISE_JSON = (Path(__file__).resolve().parents[2] / 'results'
                        / 'fix6_rhythm_stack1.json')


def load_precise_notes(path=None):
    """``{(sheet_dir, system_id, stack_id): [note, ...]}``.

    Each note carries an explicit ``onset`` (a MusicXML duration value in
    divisions) in addition to x/y/pitch_raw.  Empty mapping -- and therefore no
    behaviour change -- when the sidecar is absent.
    """
    path = Path(path) if path is not None else DEFAULT_PRECISE_JSON
    out = {}
    if not path.exists():
        return out
    try:
        rows = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return out
    for row in rows if isinstance(rows, list) else []:
        try:
            key = (str(row['sheet']), str(row['system']), str(row['stack']))
        except (KeyError, TypeError):
            continue
        sup = row.get('suppress')
        for n in row.get('notes', ()):
            if sup and 'suppress' not in n:
                n = dict(n)
                n['suppress'] = sup
            if n.get('rest'):
                if n.get('onset') is None:
                    continue
                out.setdefault(key, []).append(n)
                continue
            if n.get('pitch_raw') is None or n.get('onset') is None:
                continue
            out.setdefault(key, []).append(n)
    return out


def _suppress_keys(rows):
    """``[(x, tol), ...]`` from a precise row's ``suppress`` entries."""
    out = []
    for row in rows:
        sup = row.get('suppress')
        if not sup:
            continue
        for s in sup:
            try:
                out.append((float(s['x']), float(s.get('tol', 20))))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _tuplet_ratio_for(event):
    """Recover (actual, normal) for a precise event from its own fields.

    The ratio is stored on the event when the precise channel is processed
    (see ``_precise_events``); this just reads it back for the rest branch,
    which needs the same <time-modification> a note would get.
    """
    return getattr(event, 'precise_ratio', (None, None))


def _apply_suppression(events, precise, sheet_key, system_id, unit, stats):
    """Drop Audiveris chords whose x a precise row asks to suppress.

    Applied after every event source is merged, so a measured figure replaces
    the automatic reading instead of piling on top of it.  Rows without a
    ``suppress`` list change nothing.
    """
    keys = []
    for stack, _m in unit:
        rows = precise.get((sheet_key, str(system_id), str(stack.get('id'))))
        if rows:
            keys.extend(_suppress_keys(rows))
    if not keys:
        return events
    kept = []
    for e in events:
        if e.is_rest or e.x is None or e.onset_source == 'precise':
            if e.is_rest and e.x is not None and \
                    e.onset_source != 'precise' and \
                    any(abs(e.x - sx) <= tol for sx, tol in keys):
                stats['precise_suppressed'] += 1
                stats['rests_input'] -= len(e.rests)
                continue
            kept.append(e)
            continue
        if any(abs(e.x - sx) <= tol for sx, tol in keys):
            stats['precise_suppressed'] += 1
            # the integrity assertion compares emitted objects against the
            # input counts, so a suppressed one must leave those as well
            stats['heads_input'] -= len(e.heads)
            continue
        kept.append(e)
    return kept


def _precise_events(model, unit, precise, sheet_key, system_id, stats,
                    existing=()):
    """One chord per precise note, each with its own explicit onset.

    Mirrors `_recovered_events`'s DOM bookkeeping (so the rest of the pipeline
    treats these heads exactly like Audiveris' own) but keeps the notes apart
    instead of collapsing the column into a single chord.
    """
    events = list(existing)
    for stack, _measure in unit:
        key = (sheet_key, str(system_id), str(stack.get('id')))
        rows = precise.get(key)
        if not rows:
            continue
        # Suppression is applied by the caller once every event source has been
        # merged (see `_apply_suppression`): at this point `existing` only holds
        # what was passed in.
        for i, n in enumerate(rows):
            cid = 'precise-%s-%s-%d' % (system_id, stack.get('id'), i)
            if cid in model.head_chords:
                continue
            try:
                onset = Fraction(str(n['onset']))
            except (ValueError, ZeroDivisionError):
                onset = Fraction(0)
            # A precise entry may be a rest (used to fill the gap left by a
            # tuplet whose notated note count exceeds the noteheads we have).
            if n.get('rest'):
                rim = ET.Element('rest')
                rim.set('id', 'precise-r-%s-%s-%d'
                        % (system_id, stack.get('id'), i))
                # REST_SHAPE_TO_TYPE needs a shape, otherwise the duration
                # fallback turns an eighth rest into a quarter (measured:
                # the measure then overflows by 4 ticks).
                rim.set('shape', _REST_TYPE_TO_SHAPE.get(
                    str(n.get('type') or 'eighth'), 'EIGHTH_REST'))
                model.rests[rim.get('id')] = rim
                model.rest_to_chord[rim.get('id')] = cid
                model.chord_rests[cid] = [rim.get('id')]
                model.by_id[cid] = rim
                ev = _ChordEvent(cid, True, onset, 'precise', 0.0, None, [],
                                 [rim.get('id')])
            else:
                hid = 'precise-h-%s-%s-%d' % (system_id, stack.get('id'), i)
                if hid in model.heads:
                    continue
                x = float(n['x'])
                y = float(n['y'])
                el = ET.Element('head')
                el.set('id', hid)
                el.set('pitch', str(int(n['pitch_raw'])))
                el.set('shape', n.get('shape') or 'NOTEHEAD_VOID')
                el.set('staff', '1')
                b = ET.SubElement(el, 'bounds')
                b.set('x', str(int(x - 12)))
                b.set('y', str(int(y - 12)))
                b.set('w', '24')
                b.set('h', '24')
                model.heads[hid] = el
                model.head_to_chord[hid] = cid
                model.chord_heads[cid] = [hid]
                chel = ET.Element('head-chord')
                chel.set('id', cid)
                b = ET.SubElement(chel, 'bounds')
                b.set('x', str(int(x - 12)))
                b.set('y', str(int(y - 12)))
                b.set('w', '24')
                b.set('h', '24')
                model.head_chords[cid] = chel
                model.by_id[cid] = chel
                ev = _ChordEvent(cid, False, onset, 'precise', x, None,
                                 [hid], [])
            # Optional explicit rhythm: a precise note may carry its notated
            # value and tuplet ratio.  Honouring them here is what lets a
            # measured tuplet figure (six 16ths in the time of four, three
            # eighths in the time of two) reach the output unchanged --
            # otherwise the duration solver re-quantises the note and the
            # tuplet bracket cannot be reproduced.  Absent -> untouched.
            if n.get('type'):
                ev.note_type = str(n['type'])
                ev.dots = int(n.get('dots') or 0)
            if n.get('actual') and n.get('normal'):
                _an, _nn = int(n['actual']), int(n['normal'])
                _std = _type_to_ticks(ev.note_type, ev.dots, stats) \
                    if ev.note_type else 0
                if _std > 0 and _std * _nn % _an == 0:
                    ev.tuplet_ticks = _std * _nn // _an
                    ev.precise_ratio = (_an, _nn)
                    stats['precise_tuplet_notes'] += 1
            events.append(ev)
            stats['precise_events'] += 1
            if n.get('rest'):
                # the integrity assertion compares emitted rests against
                # rests_input, so an injected one must be counted as input too
                stats['rests_input'] += 1
            else:
                stats['heads_input'] += 1
    return events


def _x_onset(stack, x):
    """Onset hint for a synthetic chord placed at pixel ``x`` inside ``stack``.

    The slot x-offsets of most stacks are unusable as a time reference (measured:
    stack1/stack2 carry x-offset 49/102/207 while their noteheads sit at
    1537..1881), so fall back to the column's own horizontal span:

        onset = (x - left) / (right - left) * stack_duration

    The result is a *hint*: `_assign_durations` / `_solve_measure_durations`
    still quantise it and still guarantee the measure tiles exactly.
    """
    try:
        left = float(stack.get('left'))
        right = float(stack.get('right'))
        dur = _fraction(stack.get('duration'))
    except (TypeError, ValueError):
        return Fraction(0)
    if dur is None or dur <= 0 or right <= left:
        return Fraction(0)
    rel = (float(x) - left) / (right - left)
    rel = min(max(rel, 0.0), 1.0)
    whole = dur * 4                      # stack duration is in whole notes
    return rel * whole


def _list_ids(el):
    """Parse a whitespace-separated id container such as <head-chords>."""
    if el is None or not el.text:
        return []
    return el.text.split()


# ---------------------------------------------------------------------------
# per-sheet structural model
# ---------------------------------------------------------------------------

class _SheetModel:
    """All parsed objects of one ``sheet#N.xml``, plus the relation graph."""

    def __init__(self, sheet_index, root):
        self.sheet_index = sheet_index
        self.root = root
        self.page = root.find('page')

        # id -> element, for everything that lives in <sig><inters> (heads,
        # head-chords, rests, rest-chords, stems, flags, dots, alters, clefs,
        # time signatures, barlines, ...).  Ids are unique within a sheet.
        self.by_id = {}
        self.id_collisions = 0
        for interse in root.iter('inters'):
            for el in interse:
                eid = el.get('id')
                if eid is None:
                    continue
                if eid in self.by_id:
                    self.id_collisions += 1
                    continue
                self.by_id[eid] = el

        self.heads = {i: e for i, e in self.by_id.items() if e.tag == 'head'}
        self.head_chords = {i: e for i, e in self.by_id.items()
                            if e.tag == 'head-chord'}
        self.rests = {i: e for i, e in self.by_id.items() if e.tag == 'rest'}
        self.rest_chords = {i: e for i, e in self.by_id.items()
                            if e.tag == 'rest-chord'}
        self.stems = {i: e for i, e in self.by_id.items() if e.tag == 'stem'}

        # ---- relation graph ------------------------------------------------
        self.head_to_chord = {}     # head id -> head-chord id
        self.rest_to_chord = {}     # rest id -> rest-chord id
        self.chord_to_stem = {}     # head-chord id -> stem id
        self.head_to_stem = {}      # head id -> stem id
        self.stem_to_flag = {}      # stem id -> flag id
        self.head_dot_count = Counter()   # head id -> number of dots
        self.alter_to_head = {}     # alter id -> head id
        # ---- A-group marks, all measured from the relation directions ------
        # chord-articulation / chord-dynamics / chord-wedge / chord-tuplet /
        # chord-pause: source = head-chord, target = the mark
        # slur-head:   source = slur, target = head
        self.chord_articulations = defaultdict(list)   # chord id -> [art id]
        self.chord_dynamics = {}    # chord id -> dynamics id
        self.chord_wedges = {}      # chord id -> wedge id
        self.chord_tuplets = {}     # chord id -> tuplet id
        self.chord_pauses = {}      # chord id -> pause (fermata) id
        self.slur_heads = defaultdict(list)            # slur id -> [head id]
        self.head_slur_marks = defaultdict(list)       # head id -> [(num, type)]
        # beam-stem: source = beam, target = stem.  Kept so a beam can be tied
        # to the chords under it WITHOUT a detector (the .omr numbers them).
        self.beam_stems = defaultdict(list)            # beam id -> [stem id]
        self.relation_histogram = Counter()

        for rel in root.iter('relation'):
            if not len(rel):
                continue
            kind = rel[0].tag
            src, dst = rel.get('source'), rel.get('target')
            self.relation_histogram[kind] += 1
            if kind == 'containment':
                if src in self.head_chords and dst in self.heads:
                    self.head_to_chord.setdefault(dst, src)
                elif src in self.rest_chords and dst in self.rests:
                    self.rest_to_chord.setdefault(dst, src)
            elif kind == 'chord-stem':
                if src in self.head_chords:
                    self.chord_to_stem.setdefault(src, dst)
            elif kind == 'head-stem':
                if src in self.heads:
                    self.head_to_stem.setdefault(src, dst)
            elif kind == 'flag-stem':
                self.stem_to_flag.setdefault(dst, src)
            elif kind == 'augmentation':
                # source = augmentation-dot, target = head
                if dst in self.heads:
                    self.head_dot_count[dst] += 1
            elif kind == 'alter-head':
                if dst in self.heads:
                    self.alter_to_head.setdefault(src, dst)
            elif kind == 'chord-articulation':
                if src in self.head_chords:
                    self.chord_articulations[src].append(dst)
            elif kind == 'chord-dynamics':
                if src in self.head_chords:
                    self.chord_dynamics.setdefault(src, dst)
            elif kind == 'chord-wedge':
                if src in self.head_chords:
                    self.chord_wedges.setdefault(src, dst)
            elif kind == 'chord-tuplet':
                if src in self.head_chords:
                    self.chord_tuplets.setdefault(src, dst)
            elif kind == 'chord-pause':
                if src in self.head_chords:
                    self.chord_pauses.setdefault(src, dst)
            elif kind == 'slur-head':
                if dst in self.heads:
                    self.slur_heads[src].append(dst)
            elif kind == 'beam-stem':
                self.beam_stems[src].append(dst)

        # A slur connects exactly two heads (measured: 26 slur-head relations
        # over 13 slurs).  Order them by x -- the left one starts, the right one
        # stops -- and give each slur its own number so they pair correctly.
        for num, (slur_id, hids) in enumerate(
                sorted(self.slur_heads.items()), start=1):
            def _hx(h, _self=self):
                b = _bounds(_self.heads[h])
                return b[0] if b else 0.0
            for k, h in enumerate(sorted(set(hids), key=_hx)):
                self.head_slur_marks[h].append(
                    (num, 'start' if k == 0 else 'stop'))

        # ---- inverse head lists (declaration order, then sorted by pitch) --
        self.chord_heads = defaultdict(list)
        for hid, cid in self.head_to_chord.items():
            self.chord_heads[cid].append(hid)
        for cid in self.chord_heads:
            self.chord_heads[cid].sort(
                key=lambda h: (_as_int(self.heads[h].get('pitch')), h))
        self.chord_rests = defaultdict(list)
        for rid, cid in self.rest_to_chord.items():
            self.chord_rests[cid].append(rid)

        # ---- clef / time lookups ------------------------------------------
        self.time_rational_by_id = {}
        for i, e in self.by_id.items():
            if e.tag in ('time-pair', 'time-whole'):
                r = e.get('time-rational')
                if r:
                    self.time_rational_by_id[i] = r
            elif e.tag == 'time' and e.text:
                # <time> wraps an id pointing at a time-pair
                inner = self.by_id.get(e.text.strip())
                if inner is not None and inner.get('time-rational'):
                    self.time_rational_by_id[i] = inner.get('time-rational')

        self.measure_dot_chords = {}   # measure id -> dotted chord ids
        for m in root.iter('measure'):
            ad = m.find('augmentations-dots')
            if ad is not None and ad.text:
                self.measure_dot_chords.setdefault(m.get('id'), set()).update(
                    ad.text.split())

    # -- convenience -------------------------------------------------------

    def clef_for_measure(self, measure, use_clef_kind=False):
        """Resolve measure > clefs ids -> (sign, line) or None.

        ``use_clef_kind`` (default ON since 2026-09-12) takes the line from the
        clef's KIND instead of its ``pitch`` attribute.  ``pitch`` is
        Audiveris's own offset, which equals the MusicXML line only for a treble
        clef: measured on score3 the bass clefs are ``kind="BASS" pitch="-2"``,
        and line -2 is not a staff line at all -- it made MuseScore/music21
        report an unknown clef "F--2", and it fell through
        ``pitch_to_musicxml``'s bass branch, so every bass-clef part was pitched
        as if it were in treble.  Pass ``False`` for the pre-fix behaviour.
        """
        cl = measure.find('clefs')
        for cid in _list_ids(cl):
            el = self.by_id.get(cid)
            if el is None or el.tag != 'clef':
                continue
            kind = (el.get('kind') or '').upper()
            sign, line = CLEF_KIND_TO_SIGN.get(kind, ('G', 2))
            # ★ diagnostic only (never part of the output): does the two readings
            # actually disagree for this measure?  A non-zero count proves the
            # `clef_by_kind` branch is live and doing work rather than being dead
            # code -- on the rainbow part (one G2 clef, pitch=2) it is 0, which is
            # exactly why that file is byte-identical either way.
            _raw = _as_int(el.get('pitch'), line)
            self.clef_kind_differs = getattr(self, 'clef_kind_differs', 0) + (
                1 if _raw != line else 0)
            if el.get('pitch') and not use_clef_kind:
                line = _raw
            return sign, line, el.get('kind') or kind
        return None

    def time_for_measure(self, measure):
        """Resolve measure > times ids -> 'beats/beat-type' or None."""
        tm = measure.find('times')
        for tid in _list_ids(tm):
            r = self.time_rational_by_id.get(tid)
            if r:
                return r
        return None

    def key_fifths_for_measure(self, measure, staff=None):
        """Resolve measure > keys ids -> the key signature's ``fifths`` (int).

        ★ Audiveris stores the key signature **explicitly**:
        ``<key fifths="-4" staff="1" id="102"/>``, and the measure references it
        via ``<keys>102</keys>``.  Measured on the pdmx truth set the value is
        in the SAME reference frame as the score's own ``<key><fifths>``
        (f00000: .omr +1 / truth +1; f00011: .omr -4 / truth -4), so it can be
        written out as-is -- including for the transposing instruments, whose
        key is written (not concert) in both.

        ``staff`` selects among the keys of a measure: a full-score measure
        carries one ``<key>`` per staff (the piano's two staves each have one).
        Returns ``None`` when the measure has no key (the rainbow part is like
        that: 0 of its ``<key>`` elements carry a fifths attribute).
        """
        ke = measure.find('keys')
        for kid in _list_ids(ke):
            el = self.by_id.get(kid)
            if el is None or el.tag != 'key':
                continue
            if staff is not None and el.get('staff') not in (None, staff):
                continue
            if el.get('fifths') is not None:
                return _as_int(el.get('fifths'), None)
        return None

    def times_for_measure(self, measure):
        """Every time signature referenced by measure > times, in file order.

        A single Audiveris measure can carry more than one: measured on the
        rainbow part, ``measure id="1"`` holds ``<times>346 30923</times>``,
        i.e. the 4/4 that governs the opening 9-bar rest *and* the 3/4 printed
        at the entry point.  Cutting that measure at the multi-measure rest
        therefore needs both -- the rest bars take the first, the sounding
        measure that follows takes the last.
        """
        tm = measure.find('times')
        out = []
        for tid in _list_ids(tm):
            r = self.time_rational_by_id.get(tid)
            if r and r not in out:
                out.append(r)
        return out


# ---------------------------------------------------------------------------
# note/chord assembly
# ---------------------------------------------------------------------------

class _ChordEvent:
    """One chord object (head-chord or rest-chord) placed in a measure."""

    __slots__ = ('cid', 'is_rest', 'onset', 'onset_source', 'x', 'voice',
                 'heads', 'rests', 'kind', 'note_type', 'dots',
                 'duration_whole', 'tuplet_ticks', 'precise_ratio',
                 # ★ 1-based staff number inside a multi-staff part (the piano
                 # has two).  ``None`` means "this part is single-staff" and
                 # every existing caller keeps the old behaviour.
                 'staff', 'yc')

    def __init__(self, cid, is_rest, onset, onset_source, x, voice,
                 heads, rests):
        self.cid = cid
        self.is_rest = is_rest
        self.onset = onset
        self.onset_source = onset_source
        self.x = x
        self.voice = voice
        self.heads = heads
        self.rests = rests
        self.kind = None
        self.note_type = None
        self.dots = 0
        self.duration_whole = None
        # set only for a re-timed tuplet member: <duration> must then be
        # normal/actual of the notated value, not the plain value
        self.tuplet_ticks = None
        # (actual, normal) for a precise tuplet member, so the rest branch can
        # emit the same <time-modification> a note would
        self.precise_ratio = (None, None)
        # ★ multi-staff placement (see _place_events_on_staves).  ``staff`` is
        # 1 or 2 for a part that owns two staves and ``None`` otherwise.
        self.staff = None
        self.yc = None




def _filter_sheet_to_part(model, part_index, stats=None):
    """Narrow a multi-part ``_SheetModel`` down to ONE ``<part>`` (a voice).

    Full scores put N ``<part>`` elements under each ``<system>`` while the
    stacks (the time axis) stay at system level.  Each part is reduced to a
    model that looks exactly like a single-voice sheet, so every downstream
    stage keeps working unchanged.

    A part owns its measures; each measure lists its own chord ids in
    ``<head-chords>`` / ``<rest-chords>`` text, and the heads/rests themselves
    live in ``<inters>``.  That chain is what links a part to its noteheads.

    Returns a new ``_SheetModel`` (sharing the root) whose ``page`` keeps the
    systems -- stacks and staff lines intact -- but only this part's
    ``<measure>`` elements.  ``None`` when the part is absent everywhere.
    """
    import copy as _copy

    if model.page is None:
        return None
    page1 = model.page
    page2 = _copy.deepcopy(page1)
    keep_heads, keep_rests = set(), set()
    found = False
    for a, b in zip(page1.findall('system'), page2.findall('system')):
        for idx, pel in enumerate(list(b.findall('part'))):
            if idx != part_index:
                b.remove(pel)
                continue
            found = True
            for m in pel.findall('measure'):
                for cid in _list_ids(m.find('head-chords')):
                    for hid in model.chord_heads.get(cid, ()):
                        keep_heads.add(hid)
                for cid in _list_ids(m.find('rest-chords')):
                    for rid in model.chord_rests.get(cid, ()):
                        keep_rests.add(rid)
    if not found:
        return None

    out = _SheetModel(model.sheet_index, model.root)
    out.page = page2
    out.id_collisions = model.id_collisions
    out.heads = {i: e for i, e in model.heads.items() if i in keep_heads}
    out.rests = {i: e for i, e in model.rests.items() if i in keep_rests}
    hc = set()
    for hid in out.heads:
        cid = model.head_to_chord.get(hid)
        if cid:
            hc.add(cid)
    rc = set()
    for rid in out.rests:
        cid = model.rest_to_chord.get(rid)
        if cid:
            rc.add(cid)
    out.head_chords = {i: e for i, e in model.head_chords.items() if i in hc}
    out.rest_chords = {i: e for i, e in model.rest_chords.items() if i in rc}
    drop = (set(model.heads) - set(out.heads)) | \
           (set(model.rests) - set(out.rests)) | \
           (set(model.head_chords) - set(out.head_chords)) | \
           (set(model.rest_chords) - set(out.rest_chords))
    out.by_id = {i: e for i, e in model.by_id.items() if i not in drop}

    for attr in ('head_to_chord', 'rest_to_chord', 'chord_to_stem',
                 'head_to_stem', 'stem_to_flag', 'head_dot_count',
                 'alter_to_head', 'chord_articulations', 'chord_dynamics',
                 'chord_wedges', 'chord_tuplets', 'chord_pauses',
                 'slur_heads', 'head_slur_marks'):
        s0 = getattr(model, attr, None)
        if s0 is None:
            continue
        if isinstance(s0, defaultdict):
            new = defaultdict(s0.default_factory)
            for k, v in s0.items():
                if k in drop:
                    continue
                new[k] = v
        else:
            new = {k: v for k, v in s0.items()
                   if k not in drop and v not in drop}
        setattr(out, attr, new)

    keep_stems = set()
    for cid in out.head_chords:
        sid = out.chord_to_stem.get(cid)
        if sid:
            keep_stems.add(sid)
    for hid in out.heads:
        sid = out.head_to_stem.get(hid)
        if sid:
            keep_stems.add(sid)
    out.stems = {i: e for i, e in model.stems.items() if i in keep_stems}
    out.is_multipart_part = True
    out.part_index = part_index
    if stats is not None:
        stats['multipart_part_heads'] += len(out.heads)
    return out


def _build_events(model, unit, stack_of_measure, stats):
    """Build the ordered _ChordEvent list for one output measure.

    ``unit`` is a list of ``(stack_element, measure_element)`` pairs: a single
    stack normally, or several when consecutive empty stacks were merged.
    """
    # --- slot table: absolute x and time-offset, keyed by stack id + slot id
    slot_time = {}       # (stack_id, slot_id) -> Fraction (whole-note units)
    slot_absx = {}       # (stack_id, slot_id) -> absolute page x
    all_slots = []       # (abs_x, time, stack_id, slot_id)
    for stack, _m in unit:
        sid = stack.get('id')
        left = float(stack.get('left', 0))
        for s in stack.findall('slot'):
            key = (sid, s.get('id'))
            t = _fraction(s.get('time-offset'))
            xo = s.get('x-offset')
            if t is None or xo is None:
                stats['slots_unusable'] += 1
                continue
            ax = left + float(xo)          # x-offset is relative to stack/@left
            slot_time[key] = t
            slot_absx[key] = ax
            all_slots.append((ax, t, sid, s.get('id')))
    all_slots.sort(key=lambda r: (r[0], r[1]))

    # --- voice entries: chord id -> (time, voice id), plus the ordered keys
    voiced = {}
    for _stack, measure in unit:
        for voice in measure.findall('voice'):
            vid = voice.get('id')
            for entry in voice.findall('./slots/entry'):
                key_el = entry.find('key')
                val = entry.find('value')
                if key_el is None or val is None:
                    continue
                cid = val.get('chord')
                if not cid:
                    continue
                sid = stack_of_measure.get(id(measure))
                t = slot_time.get((sid, key_el.text))
                if t is None:
                    stats['entries_without_slot'] += 1
                    continue
                if cid in voiced:
                    stats['chords_referenced_twice'] += 1
                    continue
                voiced[cid] = (t, vid)

    # --- chord id list, straight from the measure's own id containers
    chord_ids = []
    seen = set()
    for _stack, measure in unit:
        for cid in (_list_ids(measure.find('head-chords'))
                    + _list_ids(measure.find('rest-chords'))):
            if cid in seen:
                stats['chord_listed_twice'] += 1
                continue
            seen.add(cid)
            chord_ids.append(cid)

    # --- place every chord: explicit voice entry, else nearest slot by x
    events = []
    for cid in chord_ids:
        is_rest = cid in model.rest_chords
        el = model.rest_chords.get(cid) if is_rest else model.head_chords.get(cid)
        if el is None:
            stats['chords_unknown_object'] += 1
            continue
        cx = _center_x(el)
        if cid in voiced:
            onset, vid = voiced[cid]
            src = 'voice'
        else:
            src = 'slot-x'
            vid = None
            if all_slots and cx is not None:
                best = min(all_slots, key=lambda r: (abs(r[0] - cx), r[0]))
                onset = best[1]
                stats['onsets_snapped'] += 1
            else:
                # no slot at all in the whole column -> fall back to x order
                onset = Fraction(0)
                stats['onsets_defaulted_zero'] += 1
        events.append(_ChordEvent(
            cid, is_rest, onset, src, cx if cx is not None else 0.0, vid,
            model.chord_heads.get(cid, []) if not is_rest else [],
            model.chord_rests.get(cid, []) if is_rest else [],
        ))

    events.sort(key=lambda e: (e.onset, e.x, 0 if e.is_rest else 1, e.cid))
    return events


def _duration_for_head(model, cid, hid, stats):
    """Reuse infer_duration() with relation-graph stem/flag/dot information."""
    head = model.heads[hid]
    stem = model.chord_to_stem.get(cid) or model.head_to_stem.get(hid)
    has_stem = stem is not None
    has_flag = stem is not None and stem in model.stem_to_flag
    dots = model.head_dot_count.get(hid, 0)
    note = {
        'shape': head.get('shape') or 'UNKNOWN',
        'has_stem': has_stem,
        'has_flag': has_flag,
        'is_dotted': dots > 0,
    }
    stats['dur_shape_' + str(note['shape'])] += 1
    if has_stem:
        stats['dur_has_stem'] += 1
    if has_flag:
        stats['dur_has_flag'] += 1
    if dots:
        stats['dur_dotted'] += 1
        if dots > 1:
            stats['dur_double_dotted'] += 1
    note_type, _quarter = infer_duration(note)
    stats['dur_type_' + note_type] += 1
    return note_type, dots


def _duration_for_rest(model, rid, stats):
    rest = model.rests[rid]
    shape = rest.get('shape') or 'UNKNOWN'
    note_type = REST_SHAPE_TO_TYPE.get(shape)
    stats['rest_shape_' + shape] += 1
    if note_type is None:
        stats['rest_shape_unmapped'] += 1
        # fall back to the project's existing inference, then to quarter
        note_type, _q = infer_duration({'shape': shape})
        if note_type is None:
            note_type = 'quarter'
    stats['rest_type_' + note_type] += 1
    return note_type, 0


def _alter_for_head(model, hid):
    """alter-head relations give the accidental; fall back to head/@alter."""
    head = model.heads[hid]
    for aid, target in model.alter_to_head.items():
        if target == hid:
            el = model.by_id.get(aid)
            if el is None:
                continue
            shape = (el.get('shape') or '').upper()
            if 'SHARP' in shape:
                return 1
            if 'FLAT' in shape:
                return -1
            if 'NATURAL' in shape:
                return 0
    if head.get('alter') is not None:
        return _as_int(head.get('alter'))
    return None


# ---------------------------------------------------------------------------
# MusicXML emission
# ---------------------------------------------------------------------------

def _xml_escape(text):
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _type_to_ticks(note_type, dots, stats):
    whole = TYPE_TO_WHOLE.get(note_type)
    if whole is None:
        stats['unknown_type_defaults'] += 1
        whole = Fraction(1, 4)
    val = whole * 4 * DIVISIONS           # ticks = whole * 4 quarters * div
    ticks = Fraction(val)
    for _ in range(dots):
        ticks += ticks / 2
    if ticks.denominator != 1:
        stats['non_integral_durations'] += 1
        ticks = Fraction(round(float(ticks)))
    return int(ticks)


def _whole_value_of(note_type, dots):
    whole = TYPE_TO_WHOLE.get(note_type, Fraction(1, 4))
    val = Fraction(whole)
    for _ in range(dots):
        val += val / 2
    return val


def _nearest_type(whole):
    """Nearest standard MusicXML type name for a whole-note length."""
    best, best_d = None, None
    for name, w in TYPE_TO_WHOLE.items():
        d = abs(float(w) - float(whole))
        if best_d is None or d < best_d:
            best, best_d = name, d
    return best


def _slot_spans(om):
    """Onset span of every event implied by the .omr slot grid.

    Diagnostic only -- the emitted duration comes from ``infer_duration``.
    """
    onsets = sorted({e.onset for e in om['events']})
    spans = {}
    for e in om['events']:
        if e.cid in spans:
            continue
        nxt = next((o for o in onsets if o > e.onset), None)
        if nxt is None:
            nxt = om['measure_end']
        spans[e.cid] = nxt - e.onset
    return spans


def _compare_duration(stats, span, note_type, dots):
    """Diagnostic: does the chosen duration agree with the .omr slot grid?"""
    if span is None or span <= 0:
        stats['duration_slot_unknown'] += 1
        return
    derived = _nearest_type(span)
    stats['duration_slot_derived_' + derived] += 1
    stats['duration_slot_compared'] += 1
    if _whole_value_of(note_type, dots) == span:
        stats['duration_slot_exact_match'] += 1
    elif derived == note_type:
        stats['duration_slot_nearest_match'] += 1
    else:
        stats['duration_slot_mismatch'] += 1


# ---------------------------------------------------------------------------
# duration assignment
# ---------------------------------------------------------------------------

def _chord_kind(model, cid, fills, threshold):
    """'hollow' / 'filled' from the measured head fill ratios, or None."""
    if fills is None:
        return None
    vals = [fills.get((model.sheet_index, h)) for h in model.chord_heads.get(cid, [])]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return 'hollow' if max(vals) < threshold else 'filled'


# ---------------------------------------------------------------------------
# multi-staff parts (the piano): two staves, one <part>
# ---------------------------------------------------------------------------
#
# Measured on `data/new_score3/score3.omr`, the only multi-staff part in the
# test score.  `part[12]` owns `staff 13` (treble) and `staff 14` (bass), but
# its `<measure>` elements carry no staff ownership at all: a measure lists the
# chords of BOTH staves in the same `<head-chords>` / `<rest-chords>`
# containers and they are told apart only by their y coordinate --
#
#   sheet#2  staff 13 (treble): lines y 2268..2330
#            staff 14 (bass)  : lines y 2413..2476
#            m1: chord 6492 y=2287.0 (treble) / chord 6493 y=2432.5 (bass)
#   sheet#3  staff 13: y 2260..2323   staff 14: y 2400..2463
#            m5: chord 6845 y=2295.5 (treble)
#                chord 6846 y=2483.0 (bass) / chord 6847 y=2423.5 (bass)
#
# so the cut is the midpoint of the gap between the two staves' own staff-line
# extents (sheet#2: (2330 + 2413) / 2 = 2371.5; sheet#3: 2361.5), i.e. a value
# strictly between the two staves and derived from the page, never hard-coded.
# Note the bass staff carries real noteheads (sheet#3 m5), so it cannot be
# faked with placeholder rests.

def _staff_y_ranges(part_el):
    """``{staff_id: (y_min, y_max)}`` from each ``<staff>``'s own staff lines.

    Falls back to ``<bounds>`` when a staff has no line geometry, and skips
    entries with no geometry at all.
    """
    out = {}
    for st in part_el.findall('staff'):
        ys = [float(p.get('y')) for ln in st.iter('line')
              for p in ln.findall('point') if p.get('y')]
        if not ys:
            for b in st.iter('bounds'):
                if b.get('y'):
                    ys.append(float(b.get('y')))
        if ys:
            out[st.get('id')] = (min(ys), max(ys))
    return out


def _two_staff_split(part_el):
    """Cut y between the two staves of a part, or ``None``.

    ``None`` unless the part owns exactly two staves with usable staff-line
    geometry, which is the only case this exporter handles; every other part
    (one staff, or three or more) keeps the single-staff code path.
    """
    ids = [st.get('id') for st in part_el.findall('staff')]
    rng = _staff_y_ranges(part_el)
    if len(ids) != 2 or ids[0] not in rng or ids[1] not in rng:
        return None
    a, b = rng[ids[0]], rng[ids[1]]
    if not (b[0] > a[1]):                      # staves must not overlap in y
        return None
    return (a[1] + b[0]) / 2.0


def _bass_clef_for_measure(model, measure, stats=None):
    """MusicXML ``(sign, line)`` of the SECOND staff of the measure.

    Read the same way as the first staff's clef -- ``measure > clefs`` lists the
    clef ids for this part's staves in staff order, so entry 1 belongs to the
    lower staff.  ``<clef kind=... pitch=...>`` is Audiveris's own clef model:
    for the piano's bass staff it is ``kind=BASS pitch=-2``, and ``pitch`` is
    the MusicXML ``<line>`` directly (the same reading
    ``_SheetModel.clef_for_measure`` uses for staff 1 -- measured on score3:
    staff 13 -> TRBLE pitch=2 and staff 14 -> BASS pitch=-2, i.e. lines 2 and 4).
    """
    ce = measure.find('clefs')
    if ce is None:
        return None
    ids = (ce.text or '').split()
    if len(ids) < 2:
        return None
    el = model.by_id.get(ids[1])
    if el is None or el.tag != 'clef':
        if stats is not None:
            stats['staff2_clef_unresolved'] += 1
        return None
    kind = (el.get('kind') or '').upper()
    # ★ the clef KIND is the authority, not `pitch`: measured on score3 the
    # piano's second staff reads `kind="BASS" pitch="-2"`, and `pitch` there is
    # Audiveris's own pitch offset (the same for the two other BASS clefs in the
    # page, on staves 10 and 11) -- NOT the MusicXML `<line>`, which for a bass
    # clef is 4.  Writing -2 makes MuseScore/music21 fail with "unknown clef
    # F--2".
    sign, line = CLEF_KIND_TO_SIGN.get(kind, ('F', 4))
    return (sign, line)


def _measure_y_center(model, el):
    """y of the middle of an element's box, or ``None``."""
    if el is None:
        return None
    b = el.find('bounds')
    if b is None or b.get('y') is None:
        return None
    return float(b.get('y')) + _as_int(b.get('h', 0)) / 2.0


def _place_events_on_staves(model, events, unit, cut, stats):
    """Tag every event of a two-staff measure with ``event.staff`` (1 or 2).

    The evidence, in order of strength:

    1. the event's own box centre against the staves' derived cut y;
    2. failing that (a chord with no ``<bounds>``), the mean y of its heads;
    3. failing that, the voice number -- Audiveris numbers the voices of the
       two staves 1..4 and 5..8 (measured: the piano's treble voice is ``1``
       and its bass voice ``5`` in every measure of both sheets).

    Anything still unplaced goes to staff 1, the same place it used to land.
    """
    for e in events:
        y = _measure_y_center(model, model.by_id.get(e.cid))
        if y is None:
            hs = [_measure_y_center(model, model.by_id.get(h))
                  for h in (e.heads or ())]
            hs = [v for v in hs if v is not None]
            y = (sum(hs) / len(hs)) if hs else None
        if y is not None:
            e.yc = y
            e.staff = 1 if y < cut else 2
            stats['staff_split_by_y'] += 1
        else:
            try:
                vn = int(str(e.voice))
            except (TypeError, ValueError):
                vn = 1
            e.staff = 1 if vn <= 4 else 2
            stats['staff_split_by_voice'] += 1
        stats['staff%d_events' % e.staff] += 1
    return events


def _repair_staff_durations(events, target_ticks, stats):
    """Make each staff's own durations tile the measure.

    MusicXML reads the events of a part strictly in order, so the two staves of
    one measure are serialised as staff 1's events followed by a ``<backup>``
    and then staff 2's.  Every measure is therefore only valid if EACH staff
    satisfies the meter on its own; a staff that falls short leaves the backup
    pointing past the start of the bar, which MuseScore rejects.

    The original single-staff code path solved this per voice, which is right
    for a single staff but wrong here: a measure with one voice per staff (7 of
    the piano's 12 measures) has two voices that each got solved to the FULL
    bar, while a measure whose two staves share one voice (sheet#3 m5) has that
    voice solved once across both.  So the deficit is repaired here, after the
    voice solver, by extending the last event of the short staff -- which keeps
    that staff's total equal to the bar without touching the other staff.

    Only ever called for a part with two staves, so no single-staff output can
    be affected.
    """
    for s in (1, 2):
        evs = [e for e in events if getattr(e, 'staff', None) == s]
        if not evs:
            continue
        total = sum((e.duration_whole or Fraction(0)) for e in evs)
        have = int(Fraction(total) * 4 * DIVISIONS)
        if have == target_ticks:
            continue
        if have > target_ticks:
            stats['staff_duration_over'] += 1
            continue
        last = max(evs, key=lambda e: (e.onset, e.x))
        # ticks -> whole notes; the ratio stays exact (24 ticks = 1/4)
        need_whole = Fraction(target_ticks - have) / (4 * DIVISIONS)
        new_whole = (last.duration_whole or Fraction(0)) + need_whole
        note_type, dots, exact = _type_for_duration(new_whole)
        if not exact:
            stats['staff_duration_unfixable'] += 1
            continue
        if (last.tuplet_ticks is not None):
            # a re-timed tuplet member must keep its normal/actual ratio
            stats['staff_duration_unfixable'] += 1
            continue
        last.note_type, last.dots = note_type, dots
        last.duration_whole = _whole_value_of(note_type, dots)
        stats['staff_duration_repaired'] += 1


def _assign_durations(model, events, measure_end, fills, threshold, stats):
    """Choose (type, dots) for every chord event and record the evidence.

    Rules, all validated by measurement on the rainbow file:

    * ``head/@shape`` is unusable (NOTEHEAD_VOID for 448/449 heads), so
      hollow/filled comes from the notehead fill ratio instead.
    * A HOLLOW chord that is the *only* chord object in its measure takes the
      measure's own notated duration (``stack/@duration``): 3/4 -> dotted half,
      1/2 -> half, 1 -> whole.  Independently corroborated by the
      ``augmentation`` relation: all 50 sole hollow chords in 3/4 measures carry
      an augmentation dot, the 7 in 1/2 measures and the 1 in a whole-note
      measure do not.
    * Any other HOLLOW chord -> half.
    * FILLED -> eighth.  The ``flag-stem`` relation can only *confirm* this
      (19 flagged chords in the whole file), never arbitrate eighth-vs-quarter.
    * Rests keep their own ``@shape`` (EIGHTH_REST / QUARTER_REST / ...), which
      is real Audiveris data.
    """
    solo = len(events) == 1
    for event in events:
        # A precise event may already carry a notated value (and tuplet ratio)
        # measured from the page.  Deriving it again from shape/fill would
        # discard the tuplet, so such an event keeps what it was given.
        # Nothing sets these fields outside the precise channel, so every
        # existing caller is unaffected.
        if event.tuplet_ticks is not None and event.note_type:
            event.duration_whole = _whole_value_of(event.note_type, event.dots)
            event.kind = event.kind or ('rest' if event.is_rest else 'given')
            stats['duration_from_precise'] += 1
            continue
        if event.is_rest:
            type_name, dots = _duration_for_rest(
                model, event.rests[0], stats) if event.rests else ('quarter', 0)
            event.note_type, event.dots = type_name, dots
            event.duration_whole = _whole_value_of(type_name, dots)
            event.kind = 'rest'
            continue

        kind = _chord_kind(model, event.cid, fills, threshold)
        event.kind = kind or 'unknown'
        if kind is None:
            # raster unavailable: fall back to the project's own heuristic
            type_name, dots = _duration_for_head(
                model, event.cid, event.heads[0], stats)
            stats['duration_fallback_used'] += 1
        elif kind == 'hollow':
            stats['hollow_chords'] += 1
            stats['hollow_heads'] += len(event.heads)
            if solo and measure_end > 0:
                type_name, dots, _ = _type_for_duration(measure_end)
                stats['hollow_sole_from_measure_duration'] += 1
                stats['sole_duration_' + str(measure_end)] += 1
            else:
                type_name, dots = 'half', 0
        else:
            stats['filled_chords'] += 1
            stats['filled_heads'] += len(event.heads)
            stem = model.chord_to_stem.get(event.cid)
            if stem is not None and stem in model.stem_to_flag:
                type_name, dots = 'eighth', 0
                stats['filled_flagged_eighth'] += 1
            else:
                type_name, dots = 'eighth', 0
                stats['filled_default_eighth'] += 1

        event.note_type, event.dots = type_name, dots
        event.duration_whole = _whole_value_of(type_name, dots)
        stats['dur_type_' + type_name] += 1
        stats['dur_dots_%d' % dots] += 1


# --- measure-level duration constraint -------------------------------------
# _assign_durations() chooses each chord's duration INDEPENDENTLY.  Nothing in
# it constrains the sum to equal the measure's own notated length, so voices
# routinely overshot the barline (measured: 50 of 129 measures, worst 3.667 vs
# 3.0 quarter notes).  MuseScore then refuses the file outright
# (Render% = 0.0%), and music21 silently drops most of the notes.
#
# This lattice + DP picks, for every event in a voice, a standard notated
# duration such that the durations sum EXACTLY to the measure length, while
# staying as close as possible to the independently-assigned guesses.
_DUR_LATTICE_PAIRS = (
    ('whole', 0), ('half', 1), ('half', 0), ('quarter', 1), ('quarter', 0),
    ('eighth', 1), ('eighth', 0), ('16th', 0), ('32nd', 0),
)


def _lattice_ticks(note_type, dots):
    whole = TYPE_TO_WHOLE[note_type]
    ticks = Fraction(whole * 4 * DIVISIONS)
    for _ in range(dots):
        ticks += ticks / 2
    return int(ticks)


_DUR_LATTICE = tuple((t, d, _lattice_ticks(t, d))
                     for t, d in _DUR_LATTICE_PAIRS)


def _build_tuplet_notation():
    """``(type, dots, ticks)`` for every value a TUPLET member may be notated as.

    Wider than ``_DUR_LATTICE`` on purpose: that table is the measure solver's
    tiling grid and must stay untouched, but a tuplet's ``<type>`` only has to
    be a real note value whose ticks divide the group's total.  Dotted values
    (16th=9, eighth=18, quarter=36, ...) are exactly what a tuplet needs when
    the group does not sit on the solver's grid.
    Truncating values (a dotted 32nd = 4.5) are excluded, not rounded.
    """
    out = []
    for _t in ('whole', 'half', 'quarter', 'eighth', '16th', '32nd'):
        _base = Fraction(TYPE_TO_WHOLE[_t]) * 4 * DIVISIONS
        for _d in (0, 1, 2):
            _v = _base * (2 - Fraction(1, 2 ** _d)) if _d else _base
            if _v.denominator == 1:
                out.append((_t, _d, int(_v)))
    return tuple(out)


_TUPLET_NOTATION = _build_tuplet_notation()

# Which lattice slot each (type, dots) occupies, coarse -> fine.  Used to
# discourage the DP from inventing note values finer than the guess just to hit
# the target sum exactly (first run produced 41 sixteenths + 22 thirty-seconds).
_LATTICE_INDEX = {(t, d): i for i, (t, d, _tk) in enumerate(_DUR_LATTICE)}
_FINER_PENALTY_TICKS = 6      # half an eighth: enough to prefer coarser values


def _guess_lattice_index(ticks):
    best_i, best_d = 0, None
    for i, (_t, _d, tk) in enumerate(_DUR_LATTICE):
        delta = abs(tk - ticks)
        if best_d is None or delta < best_d:
            best_i, best_d = i, delta
    return best_i


def _nearest_lattice(ticks):
    """Closest ``(type, dots, ticks)`` in the duration lattice."""
    best = None
    for t, d, tk in _DUR_LATTICE:
        delta = abs(tk - ticks)
        if best is None or delta < best[0]:
            best = (delta, t, d, tk)
    return best[1], best[2], best[3]


def _dur_sort_key(ev):
    return (ev.onset, ev.x if ev.x is not None else 0.0)


def _durations_from_onsets(events, target_ticks, stats):
    """Derive each event's notated value from the gaps between its onsets.

    Audiveris' ``<slot time-offset>`` is real measured rhythm, and
    ``_build_events`` already carries it onto every event as ``onset``.  The
    measure solver below used to ignore that and merely force the durations to
    sum to the bar, which systematically LENGTHENED notes: measured on the full
    score, ``F4 F4 F4`` came out as three eighths where the slots say
    quarter + eighth + eighth.

    Only used when every event came from a ``voice`` entry -- i.e. the onsets
    are the file's own measured values.  Anything else (slot-x, recovered,
    precise) keeps the previous behaviour untouched.

    Returns a list of ``(type, dots)`` parallel to ``events``, or ``None``.
    """
    if len(events) < 2 or target_ticks <= 0:
        return None
    if any(e.onset_source != 'voice' for e in events):
        return None
    if any(e.onset is None for e in events):
        return None
    ordered = sorted(events, key=_dur_sort_key)
    onsets = [Fraction(e.onset) for e in ordered]
    if any(onsets[i] > onsets[i + 1] for i in range(len(onsets) - 1)):
        return None
    end = Fraction(target_ticks, 4 * DIVISIONS)     # whole-note units
    types, dots = {}, {}
    for i, e in enumerate(ordered):
        nxt = onsets[i + 1] if i + 1 < len(onsets) else end
        gap = float(nxt - onsets[i])
        if gap <= 0:
            stats['onset_dur_zero_gap'] += 1
            return None
        ticks = gap * 4 * DIVISIONS
        t, d, mapped = _nearest_lattice(ticks)
        if abs(mapped - ticks) > 0.5:
            stats['onset_dur_rounded'] += 1
        types[e.cid], dots[e.cid] = t, d
    total = sum(_type_to_ticks(types[e.cid], dots[e.cid], stats)
                for e in events)
    stats['onset_dur_used'] += 1
    if total != target_ticks:
        stats['onset_dur_sum_mismatch'] += 1
    return [(types[e.cid], dots[e.cid]) for e in events]


def _solve_measure_durations(events, target_ticks, stats):
    """Force ``sum(durations) == target_ticks`` for one voice of one measure.

    Exact-hit DP over cumulative ticks; cost = total deviation from the
    per-event guesses.  Returns True when a constrained assignment was applied.
    """
    n = len(events)
    if n == 0 or target_ticks <= 0:
        return False
    # ★ measured onsets, when the file carries them, are a far better guess than
    # the shape/fill heuristic -- see _durations_from_onsets
    _from_onset = _durations_from_onsets(events, target_ticks, stats)
    if _from_onset is not None:
        for e, (t, d) in zip(events, _from_onset):
            e.note_type, e.dots = t, d
            e.duration_whole = _whole_value_of(t, d)
    guesses = [_type_to_ticks(e.note_type or 'quarter', e.dots or 0, stats)
               for e in events]
    if sum(guesses) == target_ticks:
        stats['dur_solve_already_exact'] += 1
        return False

    # dp[i] : cumulative_ticks -> (cost, backpointer)
    guess_idx = [_guess_lattice_index(g) for g in guesses]
    dp = [{} for _ in range(n + 1)]
    dp[0][0] = (0, None)
    for i in range(n):
        layer, nxt = dp[i], dp[i + 1]
        gi = guess_idx[i]
        for cum, (cost, _bp) in layer.items():
            for li, (t, d, tk) in enumerate(_DUR_LATTICE):
                nc = cum + tk
                if nc > target_ticks:
                    continue
                # deviation from the guess + a penalty for going finer than it
                ncost = (cost + abs(tk - guesses[i])
                         + _FINER_PENALTY_TICKS * max(0, li - gi))
                prev = nxt.get(nc)
                if prev is None or ncost < prev[0]:
                    nxt[nc] = (ncost, (cum, t, d))

    if target_ticks not in dp[n]:
        stats['dur_solve_infeasible'] += 1
        return False

    chosen, cum = [], target_ticks
    for i in range(n, 0, -1):
        _c, bp = dp[i][cum]
        pcum, t, d = bp
        chosen.append((t, d))
        cum = pcum
    chosen.reverse()

    for event, (t, d) in zip(events, chosen):
        event.note_type, event.dots = t, d
        event.duration_whole = _whole_value_of(t, d)
    stats['dur_solve_applied'] += 1
    return True


def export_structural_musicxml(omr_path, out_path=None, *, verbose=True,
                               multi_rests_path=None,
                               recovered_path=None,
                               precise_path=None,
                               beams_path=None,
                               percussion_path=None,
                               names_path=None,
                               tuplets_path=None,
                               text_path=None,
                               part_name=None,
                               title=None, subtitle=None, composer=None,
                               merge_empty_stacks=False,
                               merge_continuations=True,
                               merge_sliver_stacks=True,
                               clef_by_kind=True,
                               clef_pitch_by_kind=True,
                               no_sidecars=False):
    """Export a structural MusicXML 3.1 document from an Audiveris ``.omr``.

    Parameters
    ----------
    omr_path : str | os.PathLike
        Path to the ``.omr`` (ZIP) file.
    out_path : str | os.PathLike | None
        Where to write the MusicXML.  If ``None`` nothing is written and only
        the statistics are returned.
    verbose : bool
        Print progress and the measured statistics.
    multi_rests_path : str | os.PathLike | None
        Sidecar JSON of recovered multi-measure rests (see
        :func:`load_multi_rests`).  ``None`` uses
        ``results/multirests_verified.json`` next to the project root; a
        missing file means no multi-rests are inserted.

    Returns
    -------
    dict
        Measured statistics.  ``heads_input`` / ``rests_input`` are the counts
        of ``<head>`` / ``<rest>`` elements in the file; ``notes_emitted`` /
        ``rests_emitted`` are the counts in the produced MusicXML.  The two
        pairs are asserted equal (every head and every rest appears exactly
        once) and any drop is recorded in ``dropped_heads`` / ``dropped_rests``
        with a reason.
    """
    omr_path = Path(omr_path)
    stats = Counter()
    stats.update({
        'sheets': 0, 'multipart_sheets': 0, 'multipart_parts': 1,
        'part_models': 0, 'multipart_part_heads': 0,
        'systems': 0,
        'stacks_input': 0,
        'measures_emitted': 0,
        'notes_emitted': 0,
        'rests_emitted': 0,
        'chord_mark_count': 0,
        'heads_input': 0,
        'rests_input': 0,
        'dropped_heads': 0,
        'dropped_rests': 0,
        'empty_measures': 0,
        'measures_merged_empty_stacks': 0,
        'empty_measures_filled_with_rest': 0,
        'duplicate_sheets_skipped': 0,
        'multi_time_measures': 0,
        'continuation_measures_merged': 0,
        'sliver_stacks_merged': 0, 'tuplets_emitted': 0,
        'recovered_notes_available': 0, 'recovered_events': 0,
        'precise_notes_available': 0, 'precise_events': 0,
        'precise_tuplet_notes': 0, 'duration_from_precise': 0,
        'precise_suppressed': 0,
        'onset_dur_used': 0, 'onset_dur_rounded': 0,
        'onset_dur_sum_mismatch': 0, 'onset_dur_zero_gap': 0,
        'measures_with_excess': 0, 'measures_abnormal': 0,
        'beams_from_omr': 0,
        'perc_part': 0, 'perc_measures_emitted': 0, 'perc_notes_emitted': 0,
        'perc_rests_emitted': 0, 'perc_x_noteheads': 0,
        'perc_tremolos_emitted': 0,
        'beam_boxes_available': 0, 'beams_emitted': 0,
        'text_directions_available': 0, 'metronomes_emitted': 0,
        'text_dynamics_emitted': 0, 'words_emitted': 0,
        'articulations_emitted': 0, 'unmapped_articulations': 0,
        'dynamics_emitted': 0, 'unmapped_dynamics': 0,
        'wedges_emitted': 0, 'unmapped_wedges': 0,
        'slurs_emitted': 0, 'fermatas_emitted': 0,
        'multi_rests_available': 0,
        'multi_rests_inserted': 0,
        'multi_rest_bars_inserted': 0,
        'onsets_snapped': 0,
        'onsets_defaulted_zero': 0,
        'orphan_heads': 0,
        'orphan_rests': 0,
        # ★ two-staff parts (the piano): placement + declaration diagnostics
        'two_staff_systems': 0,
        'staff_split_by_y': 0,
        'staff_split_by_voice': 0,
        'staff1_events': 0,
        'staff2_events': 0,
        'staves_used_1': 0,
        'staves_used_2': 0,
        'staves_declared': 0,
        'staff_duration_repaired': 0,
        'staff_duration_unfixable': 0,
        'staff_duration_over': 0,
        'staff2_clef_unresolved': 0,
        # diagnostic: most measures in any one model whose clef KIND line differs
        # from its `pitch` attribute (i.e. where the bass/alto clef fix does real
        # work).  0 for the rainbow part, which has only TREBLE/pitch=2 clefs.
        'clef_kind_differs_max': 0,
        # ★ clef-aware pitch: how many notes were read with their OWN staff's
        # clef (multi-staff part, staff >= 2), and how many of those actually
        # differ from the first staff's clef.  Non-zero proves the branch runs.
        'staff1_notes_emitted': 0,
        'staff2_notes_emitted': 0,
        'staff2_notes_own_clef': 0,
        # ★ clef-aware PITCH: how many notes were converted with a clef-aware
        # table, and how many of those used a non-treble clef (i.e. where the
        # old table would have been wrong).  The second one non-zero proves the
        # fix does real work; 0 for the rainbow (all G2).
        'pitch_clef_by_kind_total': 0,
        'pitch_clef_non_treble': 0,
        # ★ key signature: how many notes actually got a non-zero key alteration
        # (0 for a score with no key, e.g. the rainbow part)
        'key_notes_emitted': 0,
        'key_notes_with_key': 0,
        # ★ how many measures carried their own <key> vs inherited it.  The .omr
        # declares the key ONCE PER SYSTEM, so inherited should dominate
        # (measured f00015 page 1: 9 declared / 63 inherited of 72 measures).
        'key_declared': 0,
        'key_inherited': 0,
        # 1 when every sidecar was ignored (measuring the exporter on its own)
        'sidecars_disabled': 0,
        'tuplet_digits_available': 0,
    })
    drop_reasons = Counter()
    clef_kinds = Counter()
    time_sigs = Counter()
    key_sigs = Counter()
    warnings = []
    # ---- notehead fill ratios (embedded raster; no PDF rendering needed) --
    fills, fill_threshold, fill_diag = _measure_fill_ratios(omr_path)
    stats.update({k: v for k, v in fill_diag.items()
                  if isinstance(v, (int, float))})
    stats['fill_centres'] = fill_diag.get('fill_centres')
    if fill_diag.get('fill_available'):
        stats['duration_source'] = ('notehead fill ratio (BINARY.png) -> '
                                    'hollow/filled; measure duration for sole '
                                    'hollow chords')
    else:
        stats['duration_source'] = 'fallback: infer_duration (head/@shape)'
        warnings.append('notehead fill ratio unavailable '
                        f'({fill_diag.get("fill_reason")}); durations come from '
                        'head/@shape, which is NOTEHEAD_VOID for 448/449 heads')
    stats['fill_threshold'] = (round(fill_threshold, 4)
                               if fill_threshold is not None else None)

    with zipfile.ZipFile(omr_path) as zf:
        sheet_names = sorted(
            (n for n in zf.namelist() if _SHEET_RE.match(n)),
            key=_sheet_sort_key)
        if not sheet_names:
            raise ValueError(f'no sheet#N/sheet#N.xml entries in {omr_path}')

        models = []
        sheet_key_of = {}
        part_of = {}          # id(model) -> part index (0 when single-part)
        n_parts_total = 1
        # Staff count per part index, from a first pass.  Needed before the part
        # models are built because the printed-name index shifts with it.
        part_staves = {}
        for name in sheet_names:
            _r = read_sheet_root(zf, name)
            if _r is None:
                continue
            _pg = _r.find('page')
            if _pg is None:
                continue
            _sysl = _pg.findall('system')
            if not _sysl:
                continue
            _np = max((len(s.findall('part')) for s in _sysl), default=0)
            if _np <= 1:
                continue
            n_parts_total = max(n_parts_total, _np)
            for _s in _sysl:
                for _pj, _pe in enumerate(_s.findall('part')):
                    part_staves[_pj] = max(part_staves.get(_pj, 1),
                                           max(1, len(_pe.findall('staff'))))
        skipped_empty = []
        for idx, name in enumerate(sheet_names):
            root = read_sheet_root(zf, name)
            if root is None:
                skipped_empty.append(idx + 1)
                continue
            base = _SheetModel(idx, root)
            if base.page is None or not base.page.findall('system'):
                # Audiveris 判为无效的页（空白页/纯文字页/倾角过大）在 .omr 里
                # 是一个 0 字节的 sheet#N.xml —— 没有 <page>，也没有 system。
                # 这种页必须整页跳过：当空模型塞进 models 会让下游把它当成
                # "一页没有音符的乐谱"处理，而且它是 Audiveris 整本导出失败的
                # 元凶（一页坏 -> 全书 exit 1，好页的 MusicXML 一个都不给）。
                skipped_empty.append(idx + 1)
                continue
            key = name.split('/')[0]
            npart = 0
            if base.page is not None:
                npart = max((len(s.findall('part'))
                             for s in base.page.findall('system')), default=0)
            if npart > 1:
                # full score: one logical model per <part>
                stats['multipart_sheets'] += 1
                n_parts_total = max(n_parts_total, npart)
                for pi in range(npart):
                    pm = _filter_sheet_to_part(base, pi, stats)
                    if pm is None:
                        continue
                    models.append(pm)
                    sheet_key_of[pm.sheet_index] = key
                    part_of[id(pm)] = pi
                    stats['part_models'] += 1
                continue
            models.append(base)
            sheet_key_of[idx] = key
            part_of[id(base)] = 0
            stats['sheets'] += 1
        stats['multipart_parts'] = n_parts_total
        stats['skipped_empty_sheets'] = skipped_empty
        if skipped_empty:
            warnings.append(
                'skipped %d page(s) Audiveris could not read (blank/skewed): %s'
                % (len(skipped_empty),
                   ', '.join('p%d' % n for n in skipped_empty)))

    # ---- drop pages the source PDF repeats verbatim ----------------------
    # The rainbow part is 3 PDF pages but pages 1 and 3 are the same music
    # (99.9% of ink matches within 3px), so exporting every sheet doubles that
    # page's measures.  Compare ink, not OMR structure: the two scans are
    # segmented slightly differently by Audiveris.
    kept, seen = [], []
    with zipfile.ZipFile(omr_path) as zf:
        for model in models:
            key = sheet_key_of[model.sheet_index]
            # multi-part parts share one BINARY.png, so ink de-duplication
            # would flag parts 2..N as duplicates of part 1
            if getattr(model, 'is_multipart_part', False):
                kept.append(model)
                continue
            ink = _sheet_ink(zf, key)
            dup = None
            # ★ 只和前面 DEDUP_WINDOW 页比（见该常量的说明）。全对全在 92 页的
            #   谱子上要跑 12 分钟以上，而重复页实际都是局部现象。
            for prev, pkey, pink, psig in seen[-DEDUP_WINDOW:]:
                if ink is not None and pink is not None:
                    # 尺寸不同 -> 不可能是同一页（同时也省掉一次整页膨胀）
                    if ink.shape != pink.shape:
                        continue
                    fwd = _ink_overlap(ink, pink)
                    same = fwd >= 0.97 and _ink_overlap(pink, ink) >= 0.97
                else:
                    same = _sheet_signature(model) == psig and psig != ()
                if same:
                    dup = prev
                    stats['duplicate_sheet_overlap'] = round(fwd, 4) \
                        if ink is not None else None
                    break
            if dup is not None:
                stats['duplicate_sheets_skipped'] += 1
                stats[f'duplicate_skipped_{key}'] = 1
                warnings.append(
                    f'{key} repeats {sheet_key_of[dup.sheet_index]} '
                    f'(same ink within 3px, measured overlap '
                    f'{stats.get("duplicate_sheet_overlap")}); skipped to avoid '
                    f'emitting that page twice')
                continue
            seen.append((model, key, ink, _sheet_signature(model)))
            kept.append(model)
    models = kept
    stats['dedup_window'] = DEDUP_WINDOW

    # ★ `no_sidecars=True` -> ignore EVERY sidecar.  Every default sidecar in
    # `results/` was built for `data/new_score3/score3.omr` (instrument names,
    # percussion voice, the score's own text, ...), so exporting a DIFFERENT
    # piece picks up another score's data: measured on `pdmx_test/val/f00011`
    # the part names came out as 短笛/长笛 (score3's names) and a percussion
    # part was appended.  Use this flag to measure the exporter on its own.
    if no_sidecars:
        multi_rests, recovered, precise = {}, {}, {}
        percussion, instr_names = None, []
        beam_boxes, tuplet_digits, text_dirs = {}, {}, {}
        stats['sidecars_disabled'] = 1
        # ★ 打击乐是【显式 opt-in】：只有在调用方明确给了 percussion_path
        #   时才加载它，即使其它 sidecar 全关。理由是它和"跨曲串数据"那类
        #   sidecar 不同 —— 它是**这一份 .omr 自己的**补充(单线谱表 Audiveris
        #   整页读不出)，而且下面那条 n_parts_total<=1 的闸门仍然拦着单声部谱，
        #   所以彩虹的输出不受影响（percussion_path 默认 None）。
        if percussion_path is not None:
            percussion = load_percussion_part(percussion_path)
    else:
        multi_rests = load_multi_rests(multi_rests_path)
        stats['multi_rests_available'] = sum(len(v)
                                             for v in multi_rests.values())
        recovered = load_recovered_notes(recovered_path)
        stats['recovered_notes_available'] = sum(len(v)
                                                 for v in recovered.values())
        precise = load_precise_notes(precise_path)
        stats['precise_notes_available'] = sum(len(v) for v in precise.values())
        percussion = load_percussion_part(percussion_path)
        beam_boxes = load_beam_boxes(beams_path)
        tuplet_digits = load_tuplet_digits(tuplets_path)
        text_dirs = load_text_directions(text_path)
        instr_names = load_instrument_names(names_path)
        stats['beam_boxes_available'] = sum(len(v)
                                            for v in beam_boxes.values())
        stats['tuplet_digits_available'] = sum(len(v)
                                               for v in tuplet_digits.values())
        stats['text_directions_available'] = sum(len(v)
                                                 for v in text_dirs.values())
    # The percussion voice belongs to the full score; refuse it for a
    # single-part score so pointing the default sidecar at a different .omr
    # cannot add a spurious part (measured: it doubled the rainbow part).
    if percussion and n_parts_total <= 1:
        percussion = None
    stats['perc_part'] = 1 if percussion else 0
    # (instrument names were loaded above, together with the other sidecars)
    # The instrument column belongs to THIS full score; refuse it for a
    # single-part score so the default sidecar cannot rename the rainbow part
    # (measured: it made that part print 短笛).
    if n_parts_total <= 1:
        instr_names = []
    stats['instrument_names_available'] = len(instr_names)
    if percussion:
        stats['perc_notes_available'] = sum(
            len(m.get('notes', ())) for m in percussion['measures'])
    stats['beam_boxes_available'] = sum(len(v) for v in beam_boxes.values())
    stats['text_directions_available'] = sum(len(v) for v in text_dirs.values())
    used_dirs = set()
    # (sheet_index, system id, chord id) -> 'begin' | 'continue' | 'end'
    beam_marks = {}
    tuplet_marks = {}

    # ---- pass 1: walk the structure in reading order --------------------
    # output_measures: list of dicts describing one MusicXML measure each
    output_measures = []
    # key signature carried across systems; pass 1 reads these whenever a
    # measure carries no explicit <key>, so they must exist before the loop
    # starts (measured: a full score without any <key> raised
    # UnboundLocalError on prevailing_fifths / _key2_carry here)
    prevailing_fifths = None
    _fifths_prev = None
    _key2_carry = None
    for model in models:
        page = model.page
        if page is None:
            continue
        stats['heads_input'] += len(model.heads)
        stats['rests_input'] += len(model.rests)
        stats[f'heads_sheet{model.sheet_index + 1}'] = len(model.heads)
        stats[f'rests_sheet{model.sheet_index + 1}'] = len(model.rests)
        stats[f'head_chords_sheet{model.sheet_index + 1}'] = len(
            model.head_chords)
        stats[f'rest_chords_sheet{model.sheet_index + 1}'] = len(
            model.rest_chords)
        page_measure_elements = list(page.iter('measure'))
        stats['omr_measure_elements'] += len(page_measure_elements)
        stats['omr_continuation_measures'] += sum(
            1 for m in page_measure_elements
            if (m.get('id') or '').endswith('C'))
        if page.get('measure-count'):
            stats['page_measure_count_total'] += _as_int(
                page.get('measure-count'))
        if model.id_collisions:
            stats['duplicate_ids'] += model.id_collisions

        # ★ restored with the time-signature block below (concurrent-edit revert)
        # no measured signature yet: left None so a real value from
        # any earlier measure/part wins over the stack/@expected
        # placeholder (measured: that placeholder is a hardcoded '3/4'
        # and was overriding 悲惨世界's measured 6/8)
        prevailing_time = None
        prevailing_clef = ('G', 2, 'TREBLE')

        # heads/rests that no containment relation linked to a chord
        linked_heads = set(model.head_to_chord)
        linked_rests = set(model.rest_to_chord)
        orphan_heads = set(model.heads) - linked_heads
        orphan_rests = set(model.rests) - linked_rests
        if orphan_heads:
            stats['orphan_heads'] += len(orphan_heads)
        if orphan_rests:
            stats['orphan_rests'] += len(orphan_rests)

        for system in page.findall('system'):
            stats['systems'] += 1
            stacks = system.findall('stack')
            part = system.find('part')
            measures = part.findall('measure') if part is not None else []
            stats['stacks_input'] += len(stacks)
            # ★ two-staff part (the piano): the cut y between its staves, taken
            # from the page's own staff lines.  ``None`` for every other part.
            _cut = _two_staff_split(part) if part is not None else None
            if _cut is not None:
                stats['two_staff_systems'] += 1
            _sys_y = [float(p.get('y')) for ln in system.iter('line')
                      for p in ln.findall('point') if p.get('y')]
            sys_y0 = min(_sys_y) if _sys_y else 0.0
            sys_y1 = max(_sys_y) if _sys_y else 0.0
            if len(stacks) != len(measures):
                warnings.append(
                    f'sheet#{model.sheet_index + 1} system {system.get("id")}: '
                    f'{len(stacks)} stacks vs {len(measures)} measures '
                    f'(paired by position, extra items ignored)')
            pairs = list(zip(stacks, measures))

            # ★ Continuation measures: Audiveris splits a bar into a main
            # measure plus a "continuation" (``measure/@id`` ends with ``C``)
            # when the closing barline confuses it.  Measured on the rainbow
            # part: sheet#1 sys8 ``63C`` is 51px against a 251px system median
            # (it is just the final double barline), while sys7 ``54C`` is
            # 263px -- a genuine bar.  So merge only the slivers, never on the
            # basis of the ``C`` suffix alone.
            if merge_continuations:
                widths = [_as_int(s.get('right')) - _as_int(s.get('left'))
                          for s, _m in pairs]
                med = (sorted(widths)[len(widths) // 2] if widths else 0)
                kept_pairs, n_cont = [], 0
                for stack, measure in pairs:
                    mid = measure.get('id') or ''
                    wdt = (_as_int(stack.get('right'))
                           - _as_int(stack.get('left')))
                    if (kept_pairs and mid.endswith('C') and med
                            and wdt < 0.4 * med):
                        prev_stack = kept_pairs[-1][0]
                        prev_stack.set('right', stack.get('right'))
                        n_cont += 1
                        continue
                    kept_pairs.append((stack, measure))
                if n_cont:
                    stats['continuation_measures_merged'] += n_cont
                    warnings.append(
                        f'sheet#{model.sheet_index + 1} system '
                        f'{system.get("id")}: merged {n_cont} sliver '
                        f'continuation measure(s) (< 40% of the system median '
                        f'width {med}px) into the preceding bar')
                pairs = kept_pairs

            # ★ Frozen barlines veto the sliver merge.
            #
            # A `<barline frozen="true">` is one the user confirmed (or
            # Audiveris froze) in the UI -- the printed page really shows a
            # barline at that x on that staff.  Measured on this project's two
            # documents: the full score (seg00, PDF p2..29) has all 19 staves
            # reporting a frozen barline at every stack boundary, while the
            # rainbow part has `frozen="true"` only on clefs / time signatures
            # and **zero** frozen barlines on any of its 173 boundaries.  So
            # requiring at least one frozen staff at the following stack's left
            # edge blocks exactly the merges that erased two printed bars
            # (p27 stack 2, p29 stack 5) without touching the rainbow.
            def _frozen_barline_staves(near_x, tol=12.0):
                got = set()
                for bl in system.iter('barline'):
                    bnd = bl.find('bounds')
                    if bnd is None or not bnd.get('x'):
                        continue
                    if (bl.get('frozen') == 'true'
                            and abs(_as_int(bnd.get('x')) - near_x) <= tol):
                        got.add(bl.get('staff'))
                return len(got)

            # ★ False barlines: Audiveris sometimes cuts one measure into two.
            #
            # Evidence -- the source PDF contains the same page twice, and the
            # two passes over identical ink (99.9% overlap within 3px) disagree:
            #   sheet#1 system 3 has an extra barline at x728  (sheet#3: none)
            #   sheet#1 system 4 has an extra barline at x1789 (sheet#3: none)
            # Merging the sliver into the FOLLOWING stack restores widths that
            # the printed measure numbers require:
            #   sys3  320,185,271,456,456,465,464 -> 320,456,456,456,465,464 (6)
            #   sys4  536,442,416,172,235,407,173,235
            #                                     -> 536,442,416,407,407,408 (6)
            # The rule (width < 50% of the system median) fires on exactly those
            # two systems and nowhere else in the whole piece -- which is why the
            # printed numbering lines up once they are fixed.
            #
            # ★ But it must never erase a bar that is itself verified: measured
            # on the full score (seg00) the same width rule fires on exactly two
            # systems (PDF p27 stack 2, 171px of a 370px median; p29 stack 5,
            # 126px of 317px) and swallowed one real printed bar in each, giving
            # 130 bars instead of 132 (2376 = 132 x 18 parts in the .omr).
            sliver_groups = None
            if merge_sliver_stacks and pairs:
                widths = [_as_int(s.get('right')) - _as_int(s.get('left'))
                          for s, _m in pairs]
                med = sorted(widths)[len(widths) // 2]
                if med > 0:
                    groups, i, n_sliver, n_veto = [], 0, 0, 0
                    while i < len(pairs):
                        if (widths[i] < 0.5 * med and i + 1 < len(pairs)
                                and not _frozen_barline_staves(
                                    _as_int(pairs[i + 1][0].get('left')))):
                            groups.append([pairs[i], pairs[i + 1]])
                            n_sliver += 1
                            i += 2
                        else:
                            if widths[i] < 0.5 * med and i + 1 < len(pairs):
                                n_veto += 1
                            groups.append([pairs[i]])
                            i += 1
                    if n_veto:
                        stats['sliver_stacks_vetoed_frozen'] += n_veto
                        warnings.append(
                            f'sheet#{model.sheet_index + 1} system '
                            f'{system.get("id")}: kept {n_veto} narrow '
                            f'stack(s) (< 50% of the system median width '
                            f'{med}px) because a frozen (verified) barline sits '
                            f'on the following boundary')
                    # ★ the grouping must be published whenever it differs from
                    # "one unit per stack" -- a vetoed sliver is a singleton and
                    # must NOT fall back to the default pairing (that would lose
                    # the veto in a system that also merged something).
                    if n_veto or n_sliver:
                        sliver_groups = groups
                    if n_sliver:
                        stats['sliver_stacks_merged'] += n_sliver
                        warnings.append(
                            f'sheet#{model.sheet_index + 1} system '
                            f'{system.get("id")}: merged {n_sliver} sliver '
                            f'stack(s) (< 50% of the system median width '
                            f'{med}px) into the following bar -- false '
                            f'barlines, cross-checked against the other scan of '
                            f'the same page')

            # One measure per stack.  Merging consecutive duration="0" stacks
            # was the old default, but the printed bar numbers prove it is
            # wrong here: sheet#1 system 7 has four such stacks that are four
            # separate bars (printed 55..62 over eight stacks) and system 8 has
            # ten, which collapsed to a single measure and lost eight bars.
            if sliver_groups is not None:
                units = [[(s, m) for s, m in g] for g in sliver_groups]
            else:
                units = [[(stack, measure)] for stack, measure in pairs]
            if merge_empty_stacks:
                units, cur = [], []
                for stack, measure in pairs:
                    if stack.get('duration') == '0':
                        cur.append((stack, measure))
                        continue
                    if cur:
                        units.append(cur)
                        cur = []
                    units.append([(stack, measure)])
                if cur:
                    units.append(cur)
            stats['measures_merged_empty_stacks'] += sum(
                max(0, len(u) - 1) for u in units)
            for stack in stacks:
                d = _fraction(stack.get('duration'))
                if d:
                    stats['stack_duration_total_whole'] += float(d)

            for unit in units:
                stack_of_measure = {id(m): s.get('id') for s, m in unit}
                first_measure = unit[0][1]
                merged_ids = [m.get('id') for _s, m in unit]

                # attributes: divisions / key / time / clef
                #
                # A unit can reference several time signatures (see
                # times_for_measure): the opening 9-bar rest is in 4/4 and the
                # printed 3/4 at the entry belongs to the bar that follows it.
                # The sounding measure takes the LAST; the restored rest bars
                # take the FIRST, so the meter change lands on the right bar.
                unit_times = []
                for _s, m in unit:
                    # ★ report the two rhythm defects Audiveris itself flags,
                    # instead of silently force-filling them later
                    if m.get('abnormal'):
                        stats['measures_abnormal'] += 1
                    if any(v.get('excess') for v in m.findall('voice')):
                        stats['measures_with_excess'] += 1
                    for r in model.times_for_measure(m):
                        if r not in unit_times:
                            unit_times.append(r)
                tsig = unit_times[-1] if unit_times else None
                tsig_rest = unit_times[0] if len(unit_times) > 1 else tsig
                if tsig is None:
                    # ★ precedence, measured on the 悲惨世界 full score:
                    #   1. this measure's own <times>   (already in unit_times)
                    #   2. the signature established for earlier measures --
                    #      Audiveris declares a time signature only ONCE per
                    #      staff (19 of 171 measures there), so "not declared"
                    #      means "unchanged", never "revert to a default"
                    #   3. stack/@expected -- but that is a hardcoded '3/4'
                    #      placeholder in that file and was OVERRIDING the
                    #      measured 6/8, turning every bar after the first into
                    #      3/4; only trust it when nothing measured exists yet
                    exp = unit[0][0].get('expected')
                    if prevailing_time is not None:
                        tsig = prevailing_time
                        time_sig_source = 'prevailing'
                    elif exp == '1':
                        tsig = '4/4'
                        time_sig_source = 'stack-expected'
                    elif exp and '/' in exp:
                        tsig = exp
                        time_sig_source = 'stack-expected'
                    else:
                        time_sig_source = 'prevailing'
                    if tsig is None:
                        tsig = prevailing_time
                    tsig_rest = tsig
                else:
                    time_sig_source = 'measure-times'
                    if len(unit_times) > 1:
                        stats['multi_time_measures'] += 1
                prevailing_time = tsig
                time_sigs[tsig] += 1

                clef = None
                for _s, m in unit:
                    clef = model.clef_for_measure(m, clef_by_kind)
                    if clef:
                        break
                stats['clef_kind_differs_max'] = max(
                    stats.get('clef_kind_differs_max', 0),
                    getattr(model, 'clef_kind_differs', 0))
                if clef is None:
                    clef = prevailing_clef
                    clef_source = 'prevailing'
                else:
                    clef_source = 'measure-clefs'
                prevailing_clef = clef
                clef_kinds[clef[2]] += 1
                # ★ key signature for THIS measure.  Per measure, never once per
                # score: measured on pdmx f00015 one piece has 6 different keys
                # (it modulates), and on `score3` sheet#2 the piano's two staves
                # each carry their own <key> element.
                _fifths = None
                for _s, m in unit:
                    _staff_el = None
                    _fifths = model.key_fifths_for_measure(m, _staff_el)
                    if _fifths is not None:
                        break
                key_sigs[_fifths] += 1
                # ★ ★ KEY SIGNATURE IS DECLARED ONCE PER SYSTEM, NOT PER MEASURE.
                # Measured on pdmx f00015: the .omr has 9 <key> elements for 72
                # measures (m1 staff 1, m10 staff 2, m16 staff 3, m25 staff 4,
                # m33 staff 5, ...) and every one of them is fifths=1.  The other
                # 63 measures carry NO <keys> at all -- so falling back to 0 there
                # was silently reverting the key to C major 63 times, which is
                # exactly why `f00015`'s 5 wrong notes stayed wrong after the first
                # version of this fix.  The key therefore CARRIES FORWARD, exactly
                # like the clef and the time signature above.
                if _fifths is not None:
                    prevailing_fifths = _fifths
                    stats['key_declared'] += 1
                else:
                    stats['key_inherited'] += 1
                _fifths_eff = prevailing_fifths
                try:
                    beats, beat_type = tsig.split('/')
                    beats, beat_type = int(beats), int(beat_type)
                except (ValueError, AttributeError):
                    beats, beat_type = 4, 4
                    time_sig_source = 'unparsable-default-4/4'
                    warnings.append(f'unparsable time signature {tsig!r}')
                # the restored rest bars keep the meter that was in force while
                # the player was resting (before the printed change)
                try:
                    rb, rbt = tsig_rest.split('/')
                    beats_rest, beat_type_rest = int(rb), int(rbt)
                except (ValueError, AttributeError):
                    beats_rest, beat_type_rest = beats, beat_type

                events = _build_events(model, unit, stack_of_measure, stats)

                # ★ multi-measure rest: Audiveris found no symbols here at all,
                # so the column is not one empty measure but N bars of rest.
                #
                # The symbol is shared with real music in two different ways, so
                # the two cases are handled separately:
                #   * the unit is empty  -> it *is* the N bars; replace it.
                #   * the unit also holds notes (measured: sheet#1 system 1
                #     stack 1 spans the 9-bar rest AND the first sounding bar)
                #     -> keep the unit's own measure and add N bar rests beside
                #     it, ordered by x so the printed order is preserved.
                unit_key = (sheet_key_of.get(model.sheet_index),
                            str(system.get('id')))
                n_rest, mr_x = 0, None
                for mx0, mx1, cnt in multi_rests.get(unit_key, ()):
                    ux0 = min((_as_int(s.get('left')) for s, _m in unit
                               if s.get('left')), default=0)
                    ux1 = max((_as_int(s.get('right')) for s, _m in unit
                               if s.get('right')), default=0)
                    if ux0 - 25 <= mx0 and mx1 <= ux1 + 25 and cnt > n_rest:
                        n_rest, mr_x = cnt, (mx0 + mx1) / 2.0
                rest_dicts = []
                if n_rest > 1:
                    stats['multi_rests_inserted'] += 1
                    stats['multi_rest_bars_inserted'] += n_rest
                    for k in range(n_rest):
                        rest_dicts.append({
                            'sheet': model.sheet_index,
                            'system': system.get('id'),
                            'stack_ids': [s.get('id') for s, _m in unit],
                            'omr_measure_ids': merged_ids,
                            'beats': beats_rest, 'beat_type': beat_type_rest,
                            'time_source': time_sig_source,
                            'clef': clef, 'clef_source': clef_source,
                            'events': [],
                            'measure_end': Fraction(0),
                            'model': model,
                            'multiple_rest': n_rest if k == 0 else 0,
                            'measure_rest': True,
                        })
                    # ★ precise channel gets its own bypass of the early return
                    # below.  A unit can hold a multi-bar rest *and* music
                    # (sheet#1 system 1: x225-1437 carries the 9-bar rest and
                    # the first tuplet bar); such a unit has no Audiveris events,
                    # so the `if not events: continue` would skip it and the
                    # synthetic notes were never emitted -- measured
                    # precise_events=0 for that stack.
                    #
                    # NOTE: `_recovered_events` is deliberately left AFTER that
                    # return, exactly where it has always been.  Hoisting it too
                    # was measured to change the default output (304 -> 261 notes,
                    # 103 -> 104 measures), i.e. it would alter behaviour for
                    # existing callers.  precise is a new channel, so giving it
                    # the bypass is behaviour-preserving for everyone else.
                    if precise:
                        events = _precise_events(
                            model, unit, precise,
                            sheet_key_of.get(model.sheet_index),
                            system.get('id'), stats, events)
                        events = _apply_suppression(
                            events, precise,
                            sheet_key_of.get(model.sheet_index),
                            system.get('id'), unit, stats)
                        events.sort(key=lambda e: (e.onset, e.x, e.cid))
                    if not events:
                        output_measures.extend(rest_dicts)
                        continue
                # ★ miss recovery: for every stack the recovery sidecar marks as
                # empty-but-has-notes, add synthetic chords.  It is deliberately
                # NOT gated on "the unit has no events": after a sliver-stack
                # merge (e.g. sheet#1 system 4 stacks 32+33) the unit holds the
                # neighbour's notes, and gating would silently drop the
                # recovered ones -- measured as 24 -> 20 notes before this fix.
                # Safety comes from the sidecar instead: build_recovery.py only
                # ever targets stacks with duration="0" and no slots, and skips
                # anything that already matches an Audiveris head.
                if recovered:
                    _rec = _recovered_events(
                        model, unit, recovered,
                        sheet_key_of.get(model.sheet_index),
                        system.get('id'), stats)
                    if _rec:
                        events = events + _rec
                        events.sort(key=lambda e: (e.onset, e.x, e.cid))
                ev_x = [e.x for e in events if e.x]
                mr_before = bool(rest_dicts) and (
                    not ev_x or (mr_x is not None and mr_x < min(ev_x)))
                if mr_before:
                    output_measures.extend(rest_dicts)

                # total notated length of this output measure, in whole notes,
                # straight from stack/@duration (used only for the diagnostic
                # comparison below, never to truncate or pad the output)
                measure_end = Fraction(0)
                for stack, _m in unit:
                    d = _fraction(stack.get('duration'))
                    if d:
                        measure_end += d
                if not any(s.findall('slot') for s, _m in unit):
                    stats['columns_without_slots'] += 1
                    if events:
                        stats['notes_in_columns_without_slots'] += len(events)

                # durations: fill-ratio hollow/filled + measure duration
                _assign_durations(model, events, measure_end, fills,
                                  fill_threshold, stats)

                # ★ constrain every voice so its durations tile the measure
                # exactly (see _solve_measure_durations).  Without this the
                # voices overshoot the barline and MuseScore rejects the file.
                #
                # The target is the DECLARED time signature (the same `tsig`
                # emitted into <attributes>), NOT stack/@duration: the file
                # tells any reader each measure is 3/4, so the durations must
                # sum to 3/4 regardless of what Audiveris actually filled in.
                tsig_ticks = None
                try:
                    _b, _bt = str(tsig).split('/')
                    tsig_ticks = int(Fraction(int(float(_b)), int(float(_bt)))
                                     * 4 * DIVISIONS)
                except Exception:
                    tsig_ticks = None
                target_ticks = (tsig_ticks if tsig_ticks
                                else int(measure_end * 4 * DIVISIONS))
                if target_ticks > 0:
                    _byv = defaultdict(list)
                    for _e in events:
                        _byv[_e.voice or DEFAULT_VOICE].append(_e)
                    for _evs in _byv.values():
                        _solve_measure_durations(_evs, target_ticks, stats)

                # ★ two-staff part: place every event on staff 1 or 2 (by y, see
                # _place_events_on_staves) and then make EACH staff tile the bar
                # on its own, because the two are serialised with a <backup>
                # between them (a staff that falls short pushes that backup past
                # the barline and MuseScore rejects the file).
                _bass_clef = None
                _key2 = None
                if _cut is not None and events:
                    _place_events_on_staves(model, events, unit, _cut, stats)
                    if target_ticks > 0:
                        _repair_staff_durations(events, target_ticks, stats)
                    # the second staff's own ids (clef + key).  A full-score
                    # measure carries one <key> PER STAFF, so staff 2's key is
                    # not necessarily staff 1's.
                    _staff_ids = [st.get('id') for st in part.findall('staff')] \
                        if part is not None else []
                    _sid2 = _staff_ids[1] if len(_staff_ids) > 1 else None
                    for _s, _m2 in unit:
                        _bass_clef = _bass_clef_for_measure(model, _m2, stats)
                        if _sid2 is not None:
                            # staff 2's own key, carried forward independently
                            # (same "declared once per system" rule as staff 1)
                            _k2r = model.key_fifths_for_measure(_m2, _sid2)
                            if _k2r is not None:
                                _key2_carry = _k2r
                            _key2 = _key2_carry
                        if _bass_clef is not None or _key2 is not None:
                            break
                    for _s in sorted({e.staff for e in events
                                      if e.staff is not None}):
                        stats['staves_used_%d' % _s] += 1

                stats['duration_total_whole'] += float(sum(
                    (e.duration_whole or Fraction(0)) for e in events))
                if measure_end > 0:
                    msum = sum((e.duration_whole or Fraction(0))
                               for e in events)
                    resid = msum - measure_end
                    stats['duration_measure_compared'] += 1
                    if resid == 0:
                        stats['duration_measure_exact'] += 1
                    if abs(resid) <= Fraction(1, 8):
                        stats['duration_measure_within_eighth'] += 1

                # onsets that land on an already-used tick in this measure
                used = Counter(e.onset for e in events)
                stats['onset_collisions'] += sum(v - 1 for v in used.values()
                                                if v > 1)
                if events:
                    stats['onsets_from_voice'] += sum(
                        1 for e in events if e.onset_source == 'voice')

                # ★ beams: bind the detector's beam strokes to the notes under
                # them.  This must run *after* _assign_durations, which is what
                # sets event.note_type; only notes whose type can carry a beam
                # participate, and a group needs >= 2 of them (a lone stroke is
                # a flag, which MusicXML already derives from <type>).
                if events:
                    # ★ prefer the .omr's own beam graph: it names the voice,
                    # which the sheet-keyed detector boxes cannot do on a full
                    # score.  Falls back to the detector when the sheet has no
                    # identified beams (e.g. the rainbow part).
                    _bm = _beam_marks_from_omr(model, events, stats)
                    if not _bm and beam_boxes:
                        _bm = _beam_marks(
                            events, beam_boxes.get(
                                sheet_key_of.get(model.sheet_index), ()),
                            sys_y0, sys_y1)
                    for _cid, _mk in _bm.items():
                        beam_marks[(model.sheet_index,
                                    str(system.get('id')), _cid)] = _mk
                        stats['beams_emitted'] += 1

                # ★ tuplets: bound by the digit positions the user annotated,
                # NOT by any detector.  Every automatic source failed because
                # the source PDF uses an *omitted* notation -- most tuplets have
                # no printed bracket or number at all (see tuplet_annotations
                # .json / docs section 9.7), so there is nothing to detect.
                # The digit sits over its group, so the group is the N events
                # nearest it.  <time-modification> only states the notation;
                # timing still comes from <duration>, which the measure solver
                # has already tiled to the barline (verified: Render% stays
                # 100% and 拍号守恒 stays 103/103).
                if events and tuplet_digits:
                    _dm = _tuplet_marks(
                        events, tuplet_digits.get(
                            sheet_key_of.get(model.sheet_index), ()),
                        str(system.get('id')), stats)
                    for _cid, _spec in _dm.items():
                        tuplet_marks[(model.sheet_index,
                                      str(system.get('id')), _cid)] = _spec
                        stats['tuplets_emitted'] += 1

                output_measures.append({
                    'sheet': model.sheet_index,
                    'system': system.get('id'),
                    'sheet_key': sheet_key_of.get(model.sheet_index),
                    'x0': min((_as_int(s.get('left')) for s, _m in unit
                               if s.get('left')), default=0),
                    'x1': max((_as_int(s.get('right')) for s, _m in unit
                               if s.get('right')), default=0),
                    'y0': sys_y0, 'y1': sys_y1,
                    'stack_ids': [s.get('id') for s, _m in unit],
                    'omr_measure_ids': merged_ids,
                    'beats': beats, 'beat_type': beat_type,
                    'time_source': time_sig_source,
                    'clef': clef,
                    'clef_source': clef_source,
                    # ★ key signature in force for this measure (None = no <key>
                    # in the .omr for it, which means C major / no accidentals)
                    'key_fifths': _fifths,
                    'events': events,
                    'measure_end': measure_end,
                    'model': model,
                    # ★ two-staff parts only: where the two staves split, and
                    # the second staff's own clef (from the measure's <clefs>).
                    # ★ two-staff parts only: where the two staves split, and
                    # the second staff's own clef (from the measure's <clefs>).
                    'cut': _cut,
                    'bass_clef': _bass_clef,
                    'key_fifths_2': _key2,
                })
                if rest_dicts and not mr_before:
                    output_measures.extend(rest_dicts)

    # ---- orphan heads / rests: attach so nothing is silently dropped ----
    # (measured: 0 in the rainbow file, implemented for generality)
    if any(m for m in output_measures):
        for model in models:
            orphan_h = set(model.heads) - set(model.head_to_chord)
            orphan_r = set(model.rests) - set(model.rest_to_chord)
            if not (orphan_h or orphan_r):
                continue
            for hid in sorted(orphan_h | orphan_r):
                is_rest = hid in orphan_r
                el = model.rests[hid] if is_rest else model.heads[hid]
                cx = _center_x(el)
                # nearest by x in the sheet's own measures
                best, best_d = None, None
                for om in output_measures:
                    if om['model'] is not model:
                        continue
                    for e in om['events']:
                        d = abs((e.x or 0.0) - (cx if cx is not None else 0.0))
                        if best_d is None or d < best_d:
                            best, best_d = om, d
                if best is None:
                    if is_rest:
                        stats['dropped_rests'] += 1
                        drop_reasons['no measure to attach an unlinked rest to'] += 1
                    else:
                        stats['dropped_heads'] += 1
                        drop_reasons['no measure to attach an unlinked head to'] += 1
                    continue
                onset = best['events'][0].onset if best['events'] else Fraction(0)
                cid = ('orphan-head-' if not is_rest else 'orphan-rest-') + str(hid)
                orphan = _ChordEvent(
                    cid, is_rest, onset, 'orphan', cx or 0.0, None,
                    [] if is_rest else [hid], [hid] if is_rest else [])
                if is_rest:
                    orphan.note_type, orphan.dots = _duration_for_rest(
                        model, hid, stats)
                else:
                    orphan.note_type, orphan.dots = _duration_for_head(
                        model, cid, hid, stats)
                orphan.duration_whole = _whole_value_of(
                    orphan.note_type, orphan.dots)
                orphan.kind = 'rest' if is_rest else 'unknown'
                best['events'].append(orphan)
                best['events'].sort(key=lambda e: (e.onset, e.x, e.cid))
                if is_rest:
                    stats['orphan_rests_attached'] += 1
                else:
                    stats['orphan_heads_attached'] += 1

    # ---- pass 2: serialise ----------------------------------------------
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 3.1 '
        'Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">',
        '<score-partwise version="3.1">',
        '  <work>',
        f'    <work-title>{_xml_escape(title or DEFAULT_WORK_TITLE)}'
        f'</work-title>',
        '  </work>',
        '  <movement-title>'
        f'{_xml_escape(subtitle or DEFAULT_SUBTITLE)}</movement-title>',
        '  <identification>',
        '    <creator type="composer">'
        f'{_xml_escape(composer or DEFAULT_COMPOSER)}</creator>',
        '    <encoding>',
        '      <software>omr_structural_export (Audiveris .omr structural '
        'export)</software>',
        '    </encoding>',
        '  </identification>',
        '  <part-list>',
    ]
    # one <score-part> per logical part; single-part output is unchanged
    # ★ names, in score order.  A part that owns two staves consumes two
    # printed names (the piano is written as 钢琴 on a treble and a bass staff),
    # so the name index advances faster than the part index.  Percussion is
    # printed in the middle of the score and is emitted at its printed slot.
    # ★ Names follow the printed score order, which is exactly the emission
    # order below: the score's own instrument column (read from the PDF text,
    # no OCR) lists every part top-to-bottom with the piano printed once, and
    # the percussion voice belongs at its printed slot (after 颤音琴/小军鼓,
    # before 钢琴).  Taking the name from the output position keeps the two in
    # step by construction -- an index derived from the .omr part number does
    # not, because parts and printed lines are not 1:1 for the piano.
    _pn = instr_names.index('小军鼓') if (percussion and instr_names
                                          and '小军鼓' in instr_names) else None
    order = []                      # ('audiveris', pi) | ('percussion', 0)
    for _pi in range(max(1, n_parts_total)):
        if _pn is not None and _pi == _pn:
            order.append(('percussion', 0))
            _pn = None
        order.append(('audiveris', _pi))
    # ★ BUG FIX (2026-09-12): 原来这里只处理 `_pn is not None`，
    #   而 `_pn` 为 None 有两种情况 —— ① 没有乐器名列（no_sidecars=True，
    #   总谱导出必须用它来避免串 score3 的名字）；② 列里没有"小军鼓"。
    #   这两种情况下 percussion **从来没有被放进 order**，
    #   紧接着的 `order.index(('percussion', 0))` 就抛
    #   `ValueError: ('percussion', 0) is not in list` —— 整个导出崩掉。
    #   实测触发：tools/perc_export_full81.py（full81 + no_sidecars=True + 打击乐）。
    #   修法：只要还没放进去就补一次（放最后 = 排在所有 Audiveris 声部之后）。
    if percussion and ('percussion', 0) not in order:
        order.append(('percussion', 0))
    stats['perc_position'] = (order.index(('percussion', 0)) + 1
                              if percussion else 0)

    def _pname_for(pi):
        """Fallback name for an Audiveris part (index-derived)."""
        return f'Part {pi + 1}' if n_parts_total > 1 \
            else (part_name or DEFAULT_PART_NAME)

    # ★ parts that own more than one staff (the piano): declared as <staves>N
    # and enclosed in a bracket so the two staves read as one player
    _multi_staff = {pi: c for pi, c in part_staves.items() if c > 1}

    _out_pids = {}
    for _n, (_kind, _pi) in enumerate(order, start=1):
        _out_pids[(_kind, _pi)] = f'P{_n}'
    # Names are taken from the OUTPUT position: the score's printed column runs
    # top-to-bottom in exactly this order, with the snare line between 颤音琴 and
    # 钢琴.  The .omr holds 18 parts for 19 printed lines, so an index taken from
    # the .omr part number cannot line the two up.
    _multi_pos = set()
    for _pi in _multi_staff:
        for _n, (_k, _p2) in enumerate(order, start=1):
            if _k == 'audiveris' and _p2 == _pi:
                _multi_pos.add(_n)
                break
    for _n, (_kind, _pi) in enumerate(order, start=1):
        if _n in _multi_pos:
            lines += [
                '    <part-group type="start" number="1">',
                '      <group-symbol>bracket</group-symbol>',
                '      <group-barline>yes</group-barline>',
                '    </part-group>',
            ]
        if _kind == 'percussion':
            _nm = _percussion_display_name(percussion, instr_names)
        elif instr_names and (_n - 1) < len(instr_names):
            _nm = instr_names[_n - 1]
        else:
            _nm = f'Part {_n}' if n_parts_total > 1 \
                else (part_name or DEFAULT_PART_NAME)
        lines += [
            f'    <score-part id="P{_n}">',
            f'      <part-name>{_xml_escape(_nm)}</part-name>',
            '    </score-part>',
        ]
        if _n in _multi_pos:
            lines.append('    <part-group type="stop" number="1"/>')
    lines.append('  </part-list>')
    stats['multistaff_parts_declared'] = len(_multi_staff)

    prev_attrs = None
    _prev_mst = 1
    prevailing_fifths = None
    _fifths_prev = None
    _key2_carry = None
    emitted_heads = 0
    emitted_rests = 0
    emitted_head_events = 0
    emitted_rest_events = 0
    number = 0
    _n_out = max(1, n_parts_total)
    # Measures per part, known before emission so the percussion voice (which is
    # printed in the middle of the score) can be padded to match.  Every <part>
    # must carry the same measure count or MuseScore rejects the document.
    _max_measures = 0
    for _pi in range(_n_out):
        _c = sum(1 for om in output_measures
                 if part_of.get(id(om['model']), 0) == _pi)
        _max_measures = max(_max_measures, _c)
    for _n, (_kind, _pi) in enumerate(order, start=1):
        if _kind == 'percussion':
            lines.append(f'  <part id="{_out_pids[(_kind, _pi)]}">')
            lines += _percussion_lines(percussion, stats,
                                       pad_to=max(1, _max_measures))
            lines.append('  </part>')
            continue
        lines.append(f'  <part id="{_out_pids[(_kind, _pi)]}">')
        # ★ each <part> must declare its own <divisions> (and key/time/clef):
        # measured on the ground truths, EVERY part carries divisions=1
        # (f00011 2 parts, msz_truth 20 parts all do).  Without this reset the
        # 2nd..Nth part emitted no <attributes> at all, so a part-independent
        # reader (e.g. gt_compare, which tracks divisions per part starting at
        # 1) divided our durations by 1 instead of 24 -- measured as
        # "f00011 part 2 durations 0/56".  MuseScore tolerates the omission, so
        # Render% could not see it.
        prev_attrs = None
        _number = 0
        for om in output_measures:
            if part_of.get(id(om['model']), 0) != _pi:
                continue
            _number += 1
            model = om['model']
            stats['measures_emitted'] += 1
            lines.append(f'    <!-- omr sheet#{model.sheet_index + 1} '
                         f'system {om["system"]} stacks '
                         f'{" ".join(str(s) for s in om["stack_ids"])} '
                         f'measures {" ".join(str(m) for m in om["omr_measure_ids"])} '
                         f'[{om["time_source"]}, {om["clef_source"]}] -->')
            lines.append(f'    <measure number="{_number}">')

            # ★ tempo / dynamics / expression text recovered by OCR.  A <direction>
            # sits at the current position and does not advance the cursor, so
            # emitting it at the head of the containing measure is safe; each box is
            # consumed once so nothing is duplicated.
            _sk = om.get('sheet_key')
            if _sk:
                for _di, (_dx, _dy, _dk, _dt, _dbpm) in enumerate(
                        text_dirs.get(_sk, ())):
                    if (_di, _sk) in used_dirs:
                        continue
                    if not (om['x0'] - 45 <= _dx <= om['x1'] + 45
                            and om['y0'] - 230 <= _dy <= om['y1'] + 130):
                        continue
                    used_dirs.add((_di, _sk))
                    _place = 'above' if _dy < om['y0'] else 'below'
                    lines.append(f'      <direction placement="{_place}">')
                    lines.append('        <direction-type>')
                    if _dk == 'metronome' and _dbpm:
                        lines.append('          <metronome>')
                        lines.append('            <beat-unit>quarter</beat-unit>')
                        lines.append(f'            <per-minute>{_dbpm}'
                                     f'</per-minute>')
                        lines.append('          </metronome>')
                        stats['metronomes_emitted'] += 1
                    elif _dk == 'dynamics':
                        lines.append('          <dynamics>')
                        lines.append(f'            <{_dt}/>')
                        lines.append('          </dynamics>')
                        stats['text_dynamics_emitted'] += 1
                    else:
                        lines.append(f'          <words>{_xml_escape(_dt)}'
                                     f'</words>')
                        stats['words_emitted'] += 1
                    lines.append('        </direction-type>')
                    lines.append('      </direction>')

            # ★ `attrs[1]` is the KEY SIGNATURE (was a hard-coded 0).  Putting the
            # real fifths in the tuple makes a mid-piece modulation (f00015 has 6
            # different keys) emit a fresh <key> exactly where it changes -- the
            # existing "changed?" logic already does the right thing.
            # ★ `None` here means "this measure carries no <key>", i.e. the key is
            # UNCHANGED from the previous measure (`_fifths_eff` carries it), NOT
            # "C major".  Writing 0 there was the bug (see the comment in pass 1).
            # ★★ and `None` must NEVER reach the XML: a sheet whose `<key>`
            # elements carry no fifths at all (the rainbow part has 19 such
            # elements) left `prevailing_fifths` at None, which printed as the
            # literal `<fifths>None</fifths>` and broke the byte-identical gate.
            # `None` means "no key signature found" == C major == 0.
            _fifths_m = om.get('key_fifths')
            _fifths_eff = _fifths_m if _fifths_m is not None else _fifths_prev
            if _fifths_eff is None:
                _fifths_eff = 0
            _fifths_prev = _fifths_eff
            attrs = (DIVISIONS, _fifths_eff, om['beats'], om['beat_type'],
                     om['clef'][0], om['clef'][1])
            # ★ a part that owns two staves (the piano) must declare that fact
            # and give each staff its own numbered clef.  This is driven by the
            # MEASURE (``om['cut']``), not by the part index: measured on
            # score3, the piano's first measure inherits the same clef tuple as
            # the preceding vibraphone part, so ``attrs`` did NOT change there
            # and the block below never ran -- which is exactly why the earlier
            # attempt produced 19 rows instead of 20.
            _cut_m = om.get('cut')
            _mst = 2 if _cut_m is not None else 1
            _need_staves = _mst > 1 and _mst != _prev_mst
            # a <multiple-rest> must be carried by an <attributes> block even when
            # nothing else changed
            if attrs != prev_attrs or om.get('multiple_rest') or _need_staves:
                lines.append('      <attributes>')
                if prev_attrs is None or attrs[0] != prev_attrs[0]:
                    lines.append(f'        <divisions>{DIVISIONS}</divisions>')
                if prev_attrs is None or attrs[1] != prev_attrs[1]:
                    lines.append('        <key>')
                    lines.append(f'          <fifths>{_fifths_eff}</fifths>')
                    lines.append('        </key>')
                if prev_attrs is None or attrs[2:4] != prev_attrs[2:4]:
                    lines.append('        <time>')
                    lines.append(f'          <beats>{om["beats"]}</beats>')
                    lines.append(f'          <beat-type>{om["beat_type"]}'
                                 f'</beat-type>')
                    lines.append('        </time>')
                if _mst > 1:
                    # ★ numbered clefs for the two-staff part: MusicXML wants
                    # <clef number="1"> for the treble staff and
                    # <clef number="2"> for the bass one.  Staff 1's clef is the
                    # measure's own clef (from the .omr); staff 2's comes from
                    # the measure's <clefs> list, i.e. the printed bass clef.
                    if prev_attrs is None or attrs[4:6] != prev_attrs[4:6] \
                            or _need_staves:
                        lines.append('        <clef number="1">')
                        lines.append(f'          <sign>{om["clef"][0]}</sign>')
                        lines.append(f'          <line>{om["clef"][1]}</line>')
                        lines.append('        </clef>')
                        _cs = om.get('bass_clef') or _STAFF_CLEFS.get(2, ('F', 4))
                        lines += [
                            '        <clef number="2">',
                            f'          <sign>{_cs[0]}</sign>',
                            f'          <line>{_cs[1]}</line>',
                            '        </clef>',
                        ]
                elif prev_attrs is None or attrs[4:6] != prev_attrs[4:6]:
                    lines.append('        <clef>')
                    lines.append(f'          <sign>{om["clef"][0]}</sign>')
                    lines.append(f'          <line>{om["clef"][1]}</line>')
                    lines.append('        </clef>')
                if _need_staves:
                    lines.append(f'        <staves>{_mst}</staves>')
                    stats['staves_declared'] += 1
                if om.get('multiple_rest'):
                    lines.append('        <measure-style>')
                    lines.append(f'          <multiple-rest>{om["multiple_rest"]}'
                                 f'</multiple-rest>')
                    lines.append('        </measure-style>')
                lines.append('      </attributes>')
                prev_attrs = attrs
                _prev_mst = _mst
                stats['attributes_blocks'] += 1

            if not om['events']:
                stats['empty_measures'] += 1
                if om.get('measure_rest'):
                    # a bar of the recovered multi-measure rest: MusicXML wants a
                    # whole-measure rest, not a zero-length measure
                    rest_ticks = int(Fraction(om['beats'], om['beat_type'])
                                     * 4 * DIVISIONS)
                    lines.append('      <note>')
                    lines.append('        <rest measure="yes"/>')
                    lines.append(f'        <duration>{rest_ticks}</duration>')
                    lines.append('        <voice>1</voice>')
                    lines.append('      </note>')
                    lines.append('    </measure>')
                    continue
                # ★ Audiveris found no chord here and it is not part of a recovered
                # multi-measure rest.  Emitting only a <barline> produced a measure
                # with zero duration, which is not valid MusicXML; the player simply
                # has a bar of rest, so write one.  (Measured: 5 such measures.)
                rest_ticks = int(Fraction(om['beats'], om['beat_type'])
                                 * 4 * DIVISIONS)
                if rest_ticks > 0:
                    lines.append('      <!-- empty column: filled with a '
                                 'whole-measure rest -->')
                    lines.append('      <note>')
                    lines.append('        <rest measure="yes"/>')
                    lines.append(f'        <duration>{rest_ticks}</duration>')
                    lines.append('        <voice>1</voice>')
                    lines.append('      </note>')
                    stats['empty_measures_filled_with_rest'] += 1
                    lines.append('    </measure>')
                    continue
                lines.append('      <!-- empty: no chord object in this column -->')
                lines.append('      <barline location="right"/>')
                stats['barlines_emitted'] += 1
                lines.append('    </measure>')
                continue

            spans = _slot_spans(om)

            # ★ Emission groups.  A single-staff part emits one group per voice,
            # exactly as before.  A two-staff part (the piano) emits one group
            # per STAFF (staff 1 first), with every voice of that staff inside
            # it: MusicXML reads a part's events strictly in order, so
            #   staff 1 ... <backup to the bar start> ... staff 2 ...
            # is how a second staff is written, and the <backup> in between is
            # what returns the cursor to the start of the bar.
            _groups = []
            if om.get('cut') is not None:
                _bys = defaultdict(list)
                for event in om['events']:
                    _bys[event.staff or 1].append(event)
                for _s in sorted(_bys):
                    # one <voice> number per staff, taken from the staff's own
                    # first event so the two staves never share a voice number
                    # (MusicXML readers -- and this project's own 拍号守恒 check
                    # -- read the timeline per voice, and two staves collapsing
                    # into voice 1 would halve every bar)
                    _vid = _bys[_s][0].voice or str(_s)
                    _groups.append((_vid, _bys[_s], _s))
            else:
                by_voice = defaultdict(list)
                for event in om['events']:
                    by_voice[event.voice or DEFAULT_VOICE].append(event)
                for voice_id in sorted(by_voice,
                                       key=lambda v: (_as_int(v), str(v))):
                    _groups.append((voice_id, by_voice[voice_id], None))
            stats['voices_emitted'] += len(_groups)
            if len(_groups) > 1:
                stats['measures_with_multiple_voices'] += 1

            cursor_ticks = 0        # running position inside the current group
            voice_index = 0
            _emitted_ticks = 0      # what the previous group actually wrote out
            for voice_id, events, _staff_no in _groups:
                if voice_index:
                    # rewind to the start of the measure and read the next voice
                    #
                    # ★ the amount must be what was ACTUALLY emitted for the
                    # group just finished, not the cursor the solver left behind:
                    # a multi-staff part may have had that group's values repaired
                    # afterwards (_repair_staff_durations), and using the stale
                    # cursor rewound only 48 of the 96 ticks emitted -- measured
                    # as `<backup>48` in front of a whole-measure rest, which put
                    # the second staff half a bar late.
                    back = _emitted_ticks
                    if back > 0:
                        lines.append('      <backup>')
                        lines.append(f'        <duration>{back}</duration>')
                        lines.append('      </backup>')
                        stats['backups_emitted'] += 1
                    cursor_ticks = 0
                _emitted_ticks = 0
                voice_index += 1

                for event in events:
                    # ★ Sequential emission.  Inside one voice MusicXML derives all
                    # timing from the sequence of <duration> values, so the cursor
                    # must advance by each note's own duration.  This previously
                    # used event.onset (from the x-derived slot grid), which
                    # conflicted with the durations: the cursor overshot the barline
                    # and MuseScore rejected the whole file (Render% = 0.0%).
                    # The measure-level constraint solver above guarantees the
                    # durations sum to the barline, so sequential emission lands the
                    # cursor exactly on it and no <forward> is needed.
                    onset_ticks = cursor_ticks
                    if onset_ticks > cursor_ticks:          # dead: kept for stats
                        gap = onset_ticks - cursor_ticks
                        lines.append('      <forward>')
                        lines.append(f'        <duration>{gap}</duration>')
                        lines.append('      </forward>')
                        stats['forwards_emitted'] += 1
                        cursor_ticks = onset_ticks
                    elif onset_ticks < cursor_ticks:
                        stats['onsets_behind_cursor'] += 1

                    # ★ dynamics / wedge: MusicXML carries these as <direction>,
                    # which sits at the current position and does not advance the
                    # cursor, so emitting it just before the chord is correct.
                    _did = model.chord_dynamics.get(event.cid)
                    if _did:
                        _shape = (model.by_id.get(_did).get('shape') or '')
                        _tag = _DYNAMICS_TAGS.get(_shape)
                        if _tag:
                            lines.append('      <direction placement="below">')
                            lines.append('        <direction-type>')
                            lines.append('          <dynamics>')
                            lines.append(f'            <{_tag}/>')
                            lines.append('          </dynamics>')
                            lines.append('        </direction-type>')
                            lines.append('      </direction>')
                            stats['dynamics_emitted'] += 1
                        else:
                            stats['unmapped_dynamics'] += 1
                    _wid = model.chord_wedges.get(event.cid)
                    if _wid:
                        _shape = (model.by_id.get(_wid).get('shape') or '')
                        _tag = _WEDGE_TAGS.get(_shape)
                        if _tag:
                            lines.append('      <direction placement="below">')
                            lines.append('        <direction-type>')
                            lines.append(f'          <wedge type="{_tag}"/>')
                            lines.append('        </direction-type>')
                            lines.append('      </direction>')
                            stats['wedges_emitted'] += 1
                        else:
                            stats['unmapped_wedges'] += 1

                    if event.is_rest:
                        rests = event.rests or []
                        if not rests:
                            stats['dropped_rests'] += 1
                            drop_reasons['rest-chord with no contained rest'] += 1
                            continue
                        note_type, dots = event.note_type, event.dots
                        # ★ a re-timed tuplet member emits its own <duration>
                        # (same rule as the head branch below): a rest that is the
                        # first item of a sextuplet must be 8 ticks, not the plain
                        # eighth value of 12 -- measured as a 4-tick overflow.
                        ticks = (event.tuplet_ticks if event.tuplet_ticks
                                 else _type_to_ticks(note_type, dots, stats))
                        _compare_duration(stats, spans.get(event.cid), note_type, dots)
                        for i, rid in enumerate(rests):
                            lines.append('      <note>')
                            if i:
                                stats['chord_mark_count_on_rests'] += 1
                            lines.append('        <rest/>')
                            lines.append(f'        <duration>{ticks}</duration>')
                            # ★ which staff of a two-staff part this rest is on
                            _sno = _staff_no if _staff_no is not None \
                                else getattr(event, 'staff', None)
                            if _sno is not None:
                                lines.append(f'        <staff>{_sno}</staff>')
                            lines.append(f'        <voice>{voice_id}</voice>')
                            lines.append(f'        <type>{note_type}</type>')
                            for _ in range(dots):
                                lines.append('        <dot/>')
                            # tuplet notation must match the duration above
                            if event.tuplet_ticks:
                                _an, _nn = _tuplet_ratio_for(event)
                                if _an and _nn:
                                    lines.append('        <time-modification>')
                                    lines.append(f'          <actual-notes>{_an}'
                                                 f'</actual-notes>')
                                    lines.append(f'          <normal-notes>{_nn}'
                                                 f'</normal-notes>')
                                    lines.append('        </time-modification>')
                            lines.append('      </note>')
                            emitted_rests += 1
                            stats['out_type_' + note_type] += 1
                            stats['out_type_event_' + note_type] += 1
                            emitted_rest_events += 1
                        cursor_ticks = max(cursor_ticks, onset_ticks + ticks)
                        _emitted_ticks += ticks
                    else:
                        heads = event.heads or []
                        if not heads:
                            stats['dropped_heads'] += 1
                            drop_reasons['head-chord with no contained head'] += 1
                            continue
                        sign, line, _kind = om['clef']
                        note_type, dots = event.note_type, event.dots
                        # ★ a re-timed tuplet member emits its own <duration>:
                        # MusicXML needs normal/actual of the notated value, which
                        # _type_to_ticks alone cannot produce.
                        ticks = (event.tuplet_ticks if event.tuplet_ticks
                                 else _type_to_ticks(note_type, dots, stats))
                        _compare_duration(stats, spans.get(event.cid), note_type, dots)
                        for i, hid in enumerate(heads):
                            head = model.heads.get(hid)
                            if head is None:
                                stats['dropped_heads'] += 1
                                drop_reasons['head id not found in sheet'] += 1
                                continue
                            # ★ the clef that governs THIS note: for a
                            # two-staff part (the piano) each staff has its own
                            # staff number and its own clef, so staff 2's notes
                            # must be read with `om['bass_clef']`, not with the
                            # first staff's clef.  Single-staff parts are
                            # unchanged (`_staff_no` is None -> om['clef']).
                            _nsign, _nline = sign, line
                            # ★ MUST be `_fifths_eff` (the carried-forward key),
                            # not `_fifths_m` (this measure's own <key>, which is
                            # None for the 7 measures out of 8 that inherit it).
                            # Using the raw value here left 63 of 72 measures in
                            # f00015 without their sharps even though the printed
                            # <fifths> was already correct.
                            _nkey = _fifths_eff
                            if _staff_no is not None and _staff_no > 1:
                                _bcs = om.get('bass_clef')
                                if _bcs:
                                    _nsign, _nline = _bcs
                                # the piano's two staves each carry their own
                                # <key>; staff 2's may differ from staff 1's
                                _k2 = om.get('key_fifths_2')
                                if _k2 is not None:
                                    _nkey = _k2
                            # ★ key signature takes part in the pitch conversion:
                            # pitch is the DIATONIC staff position, so a note in
                            # the key of F# must come out F#, not F.  Measured on
                            # the pdmx truth set this was the single largest error
                            # source (f00015 page 1: 162/167 correct, and all 5
                            # wrong ones were exactly the key's sharps).
                            step, octave, alter = pitch_to_musicxml(
                                _as_int(head.get('pitch')), _nsign, _nline,
                                clef_pitch_by_kind, _nkey)
                            stats['pitch_clef_by_kind_total'] += 1
                            if _nkey:
                                stats['key_notes_emitted'] += 1
                            # only meaningful when the clef-aware table is in
                            # use; otherwise this note was read as treble and
                            # the count must stay 0 (it is a fix diagnostic,
                            # not a clef census)
                            if clef_pitch_by_kind and (_nsign, _nline) != ('G', 2):
                                stats['pitch_clef_non_treble'] += 1
                            if _staff_no is not None:
                                stats['staff%d_notes_emitted' % _staff_no] += 1
                                if (_nsign, _nline) != (sign, line):
                                    stats['staff2_notes_own_clef'] += 1
                            # ★ a printed accidental OVERRIDES the key signature:
                            # `_alter_for_head` returns the sign printed on the
                            # page (alter-head relation), e.g. a natural on an
                            # F that the key would sharp.
                            acc = _alter_for_head(model, hid)
                            if acc is not None:
                                alter = acc

                            lines.append('      <note>')
                            if i:
                                lines.append('        <chord/>')
                                stats['chord_mark_count'] += 1
                            lines.append('        <pitch>')
                            lines.append(f'          <step>{step}</step>')
                            if alter:
                                lines.append(f'          <alter>{alter}</alter>')
                            lines.append(f'          <octave>{octave}</octave>')
                            lines.append('        </pitch>')
                            lines.append(f'        <duration>{ticks}</duration>')
                            # ★ which staff of a two-staff part this note is on
                            _sno = _staff_no if _staff_no is not None \
                                else getattr(event, 'staff', None)
                            if _sno is not None:
                                lines.append(f'        <staff>{_sno}</staff>')
                            lines.append(f'        <voice>{voice_id}</voice>')
                            lines.append(f'        <type>{note_type}</type>')
                            for _ in range(dots):
                                lines.append('        <dot/>')
                            # ★ printed accidental.  MusicXML separates <alter> (the
                            # sounding pitch, emitted inside <pitch> above) from
                            # <accidental> (the sign printed on the page).  We used
                            # to emit only the former -- measured against Audiveris's
                            # own export: 0 vs 21 signs (docs, head-to-head).  Order
                            # inside <note> is type, dot, accidental, time-modification.
                            if acc is not None:
                                _nm = {2: 'double-sharp', 1: 'sharp', 0: 'natural',
                                       -1: 'flat', -2: 'flat-flat'}.get(acc)
                                if _nm:
                                    lines.append(f'        <accidental>{_nm}'
                                                 f'</accidental>')
                                    stats['accidentals_emitted'] += 1
                            _beam = beam_marks.get(
                                (om['sheet'], str(om['system']), event.cid))
                            if _beam and i == 0:
                                # a chord carries the beam on its first note only
                                lines.append('        <beam number="1">'
                                             f'{_beam}</beam>')

                            # ★ tuplet: only the ones Audiveris itself linked via a
                            # chord-tuplet relation (measured: 2 on sheet#1).  The
                            # detector's tuplet boxes are NOT used here -- they are
                            # noisy (they fire in the title block and the page footer)
                            # and many sit under a different system than they look.
                            # <time-modification> states the *notation*; MusicXML
                            # timing comes from <duration>, which the measure solver
                            # already tiled to the barline, so no duration changes.
                            _tup = model.chord_tuplets.get(event.cid)
                            _spec = tuplet_marks.get(
                                (om['sheet'], str(om['system']), event.cid))
                            if _spec and i == 0:
                                _an, _nn = _spec[0], _spec[1]
                                lines.append('        <time-modification>')
                                lines.append(f'          <actual-notes>{_an}'
                                             f'</actual-notes>')
                                lines.append(f'          <normal-notes>{_nn}'
                                             f'</normal-notes>')
                                lines.append('        </time-modification>')
                                stats['tuplets_emitted'] += 1
                            elif _tup and i == 0:
                                _tel = model.by_id.get(_tup)
                                _tshape = _tel.get('shape') if _tel is not None else ''
                                _an, _nn = _TUPLET_RATIOS.get(_tshape, (3, 2))
                                lines.append('        <time-modification>')
                                lines.append(f'          <actual-notes>{_an}'
                                             f'</actual-notes>')
                                lines.append(f'          <normal-notes>{_nn}'
                                             f'</normal-notes>')
                                lines.append('        </time-modification>')
                                stats['tuplets_emitted'] += 1

                            # ★ A-group notations: articulations + fermata belong to
                            # the chord, so they go on its first note; slurs belong
                            # to a specific head, so they go on that one.
                            _marks = []
                            if i == 0:
                                for _aid in model.chord_articulations.get(
                                        event.cid, ()):
                                    _sh = (model.by_id.get(_aid).get('shape') or '')
                                    _tg = _ARTICULATION_TAGS.get(_sh)
                                    if _tg:
                                        _marks.append(('art', _tg))
                                    else:
                                        stats['unmapped_articulations'] += 1
                                if event.cid in model.chord_pauses:
                                    _marks.append(('fermata', None))
                            for _num, _typ in model.head_slur_marks.get(hid, ()):
                                _marks.append(('slur', (_num, _typ)))
                            if _marks:
                                lines.append('        <notations>')
                                _arts = [t for k, t in _marks if k == 'art']
                                if _arts:
                                    lines.append('          <articulations>')
                                    for _t in _arts:
                                        lines.append(f'            <{_t}/>')
                                        stats['articulations_emitted'] += 1
                                    lines.append('          </articulations>')
                                for _k, _t in _marks:
                                    if _k == 'fermata':
                                        lines.append('          <fermata/>')
                                        stats['fermatas_emitted'] += 1
                                    elif _k == 'slur':
                                        _num, _typ = _t
                                        lines.append(
                                            f'          <slur type="{_typ}" '
                                            f'number="{_num}"/>')
                                        stats['slurs_emitted'] += 1
                                lines.append('        </notations>')
                            lines.append('      </note>')
                            emitted_heads += 1
                            stats['out_type_' + note_type] += 1
                            if i == 0:
                                stats['out_type_event_' + note_type] += 1
                                emitted_head_events += 1
                        cursor_ticks = max(cursor_ticks, onset_ticks + ticks)
                        _emitted_ticks += ticks

            # stack boundaries are the barlines
            lines.append('      <barline location="right"/>')
            stats['barlines_emitted'] += 1
            lines.append('    </measure>')

        lines.append('  </part>')
        _max_measures = max(_max_measures, _number)
    lines += ['</score-partwise>', '']
    text = '\n'.join(lines)

    stats['notes_emitted'] = emitted_heads
    stats['rests_emitted'] = emitted_rests
    stats['heads_emitted'] = emitted_heads
    stats['rests_emitted_total'] = emitted_rests
    stats['sounding_events_emitted'] = emitted_head_events + emitted_rest_events
    stats['duration_total_whole'] = round(float(stats['duration_total_whole']), 3)
    stats['stack_duration_total_whole'] = round(
        float(stats['stack_duration_total_whole']), 3)
    stats['duration_vs_stack_delta'] = round(
        float(stats['duration_total_whole']
              - stats['stack_duration_total_whole']), 3)

    # ---- hard integrity assertions --------------------------------------
    if emitted_heads != stats['heads_input']:
        raise AssertionError(
            f'head accounting failed: {stats["heads_input"]} heads in the .omr '
            f'but {emitted_heads} emitted; drops={dict(drop_reasons)}')
    if emitted_rests != stats['rests_input']:
        raise AssertionError(
            f'rest accounting failed: {stats["rests_input"]} rests in the .omr '
            f'but {emitted_rests} emitted; drops={dict(drop_reasons)}')

    if out_path is not None:
        out_path = Path(out_path)
        if out_path.parent and str(out_path.parent):
            os.makedirs(out_path.parent, exist_ok=True)
        with open(out_path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
        stats['out_path'] = str(out_path)

    result = {k: (int(v) if isinstance(v, int) else v)
              for k, v in stats.items()}
    result['dropped_heads'] = int(stats.get('dropped_heads', 0))
    result['dropped_rests'] = int(stats.get('dropped_rests', 0))
    result['drop_reasons'] = dict(drop_reasons)
    result['clef_kinds'] = dict(clef_kinds)
    result['time_signatures'] = dict(time_sigs)
    result['key_signatures'] = dict(key_sigs)
    result['omr_distinct_measure_ids'] = (
        int(stats.get('omr_measure_elements', 0))
        - int(stats.get('omr_continuation_measures', 0)))
    result['relation_histogram'] = {
        f'sheet#{i + 1}': dict(m.relation_histogram)
        for i, m in enumerate(models)}
    result['warnings'] = warnings
    if verbose:
        interesting = [
            'sheets', 'systems', 'stacks_input', 'measures_emitted',
            'duplicate_sheets_skipped', 'multi_rests_available',
            'multi_rests_inserted', 'multi_rest_bars_inserted',
            'multi_time_measures', 'continuation_measures_merged',
            'sliver_stacks_merged', 'tuplets_emitted',
            'recovered_notes_available', 'recovered_events',
            'beam_boxes_available', 'beams_emitted',
            'text_directions_available', 'metronomes_emitted',
            'text_dynamics_emitted', 'words_emitted',
            'articulations_emitted', 'unmapped_articulations',
            'dynamics_emitted', 'unmapped_dynamics',
            'wedges_emitted', 'unmapped_wedges',
            'slurs_emitted', 'fermatas_emitted',
            'measures_merged_empty_stacks', 'empty_measures',
            'empty_measures_filled_with_rest',
            'notes_emitted', 'rests_emitted', 'chord_mark_count',
            'heads_input', 'rests_input', 'dropped_heads', 'dropped_rests',
            'onsets_from_voice', 'onsets_snapped', 'onsets_defaulted_zero',
            'onset_collisions', 'attributes_blocks', 'orphan_heads',
            'orphan_rests',
        ]
        print(f'[structural-export] {omr_path.name}')
        for k in interesting:
            print(f'  {k} = {result.get(k)}')
        print(f'  time_signatures = {result["time_signatures"]}')
        print(f'  clef_kinds = {result["clef_kinds"]}')
        if warnings:
            print('  warnings:')
            for w in warnings:
                print(f'    - {w}')
    return result


__all__ = ['export_structural_musicxml', 'DIVISIONS']


