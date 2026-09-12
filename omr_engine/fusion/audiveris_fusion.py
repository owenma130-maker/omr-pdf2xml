"""
Audiveris .omr 解析 → MusicXML 导出（本发布物只用到其中的音高/时值函数）

功能:
1. 解析Audiveris .omr文件中的所有音符头(head)和和弦(head-chord)
2. 从head元素提取坐标, 从head-chord提取分组, 从system/voice提取时序
3. 将Audiveris pitch(谱线位置)转换为实际音高(MusicXML pitch)
4. 按乐谱顺序排序(系统→小节→voice→x位置)
5. 导出MusicXML
6. （已移出发布物）与商业 OMR 的真值对齐评估

Audiveris .omr结构:
- ZIP包含 book.xml + N个 sheet#/sheet#.xml + sheet#/BINARY.png
- 每个sheet XML有: system → staff → measure → voice → slot
- head元素: pitch(谱线位置, 0=B4高音谱号中线, 正值向下), shape, staff, bounds
- head-chord元素: 同时演奏的head分组, 有bounds和staff
- voice元素: measure内的时序, slot→chord映射
- stack元素: 时间偏移(x-offset, time-offset)

音高映射(高音谱号G/2):
- Audiveris pitch=0 → 中线B4
- pitch增加→音高降低(向下)
- 公式: diatonic_index = -pitch + 6 (C4=0, D4=1, ..., B4=6, C5=7, ...)
"""
import zipfile
import re
import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, Counter
from pathlib import Path

# 项目路径
_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent.parent
DATA_ROOT = PROJECT_ROOT.parent / "data"

# 默认路径
DEFAULT_OMR = DATA_ROOT / "test_rainbow" / "out" / "彩虹 青春 cl声部(1).omr"
DEFAULT_OUTPUT = PROJECT_ROOT / "results" / "vis" / "audiveris_output.musicxml"
DEFAULT_TRUTH = None   # 不再内置厂商输出真值 (零法律风险); 评测时显式传入 truth_path

# 音名映射
NOTE_NAMES = ['C', 'D', 'E', 'F', 'G', 'A', 'B']
STEP_TO_SEMITONE = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}

# 时值映射(quarter=1)
DUR_MAP = {'whole': 4, 'half': 2, 'quarter': 1, 'eighth': 0.5,
           '16th': 0.25, '32nd': 0.125, '64th': 0.0625}


# ============================================================
# 1. OMR解析: 提取所有音符头和和弦
# ============================================================

def parse_omr(omr_path):
    """
    解析Audiveris .omr文件, 返回所有sheet的音符数据

    Returns:
        list of dict, 每个音符:
        {
            'sheet': int,          # 页码(0-based)
            'system': int,         # 系统编号(0-based)
            'staff': int,          # 五线谱编号(1-based, OMR内部)
            'chord_id': str,       # head-chord ID
            'head_id': str,        # head ID
            'pitch_raw': int,      # Audiveris原始pitch值
            'x': int, 'y': int,   # 坐标(左上角)
            'w': int, 'h': int,   # 尺寸
            'shape': str,          # NOTEHEAD_VOID, WHOLE_NOTE等
            'grade': float,        # 检测置信度
        }
    """
    z = zipfile.ZipFile(omr_path)
    all_notes = []

    # 获取sheet列表
    sheet_names = sorted([n for n in z.namelist()
                          if re.match(r'sheet#\d+/sheet#\d+\.xml$', n)])

    for sheet_idx, sheet_xml_path in enumerate(sheet_names):
        xml = z.read(sheet_xml_path).decode('utf-8', errors='ignore')
        notes = _parse_sheet_xml(xml, sheet_idx)
        all_notes.extend(notes)

    z.close()
    return all_notes


def _extract_accidentals(xml):
    """
    从OMR XML中提取变音记号

    Audiveris使用 <alter pitch="..." shape="SHARP|FLAT|NATURAL" ...> 元素
    pitch属性表示该变音记号影响的谱线位置(与head的pitch一致)

    Returns:
        list of dict: [{'shape': 'SHARP', 'x': ..., 'y': ..., 'pitch': ...}, ...]
    """
    accidentals = []
    for m in re.finditer(
            r'<alter\s[^>]*?pitch="([\d-]+)"[^>]*?shape="(SHARP|FLAT|NATURAL)"'
            r'[^>]*?>\s*<bounds x="(\d+)" y="(\d+)" w="(\d+)" h="(\d+)"/>\s*'
            r'</alter>', xml, re.S):
        accidentals.append({
            'shape': m.group(2),
            'pitch': int(m.group(1)),
            'x': int(m.group(3)), 'y': int(m.group(4)),
            'w': int(m.group(5)), 'h': int(m.group(6))
        })

    # Also try reversed attribute order
    for m in re.finditer(
            r'<alter\s[^>]*?shape="(SHARP|FLAT|NATURAL)"[^>]*?pitch="([\d-]+)"'
            r'[^>]*?>\s*<bounds x="(\d+)" y="(\d+)" w="(\d+)" h="(\d+)"/>\s*'
            r'</alter>', xml, re.S):
        accidentals.append({
            'shape': m.group(1),
            'pitch': int(m.group(2)),
            'x': int(m.group(3)), 'y': int(m.group(4)),
            'w': int(m.group(5)), 'h': int(m.group(6))
        })

    return accidentals


def _parse_sheet_xml(xml, sheet_idx):
    """解析单个sheet的XML, 提取所有音符头"""
    # 0. 提取附点chord IDs
    dotted_chords = extract_dotted_chords(xml)

    # 0b. 提取变音记号(SHARP/FLAT/NATURAL)
    accidentals = _extract_accidentals(xml)

    # 0c. 提取stem元素 (用于时值推断)
    stems = []
    for m in re.finditer(
            r'<stem\s[^>]*?id="(\d+)"[^>]*>\s*(.*?)\s*</stem>', xml, re.S):
        sid = m.group(1)
        content = m.group(2)
        bounds_m = re.search(r'<bounds x="(\d+)" y="(\d+)" w="(\d+)" h="(\d+)"/>', content)
        if bounds_m:
            stems.append({
                'stem_id': sid,
                'x': int(bounds_m.group(1)), 'y': int(bounds_m.group(2)),
                'w': int(bounds_m.group(3)), 'h': int(bounds_m.group(4)),
            })

    # 0d. 提取flag元素 (用于时值推断)
    flags = []
    for m in re.finditer(
            r'<flag\s[^>]*?id="(\d+)"[^>]*>\s*(.*?)\s*</flag>', xml, re.S):
        fid = m.group(1)
        content = m.group(2)
        bounds_m = re.search(r'<bounds x="(\d+)" y="(\d+)" w="(\d+)" h="(\d+)"/>', content)
        if bounds_m:
            flags.append({
                'flag_id': fid,
                'x': int(bounds_m.group(1)), 'y': int(bounds_m.group(2)),
                'w': int(bounds_m.group(3)), 'h': int(bounds_m.group(4)),
            })

    # 0e. 提取augmentation-dot元素 (用于附点检测)
    aug_dots = []
    for m in re.finditer(
            r'<augmentation-dot\s[^>]*?id="(\d+)"[^>]*>\s*(.*?)\s*</augmentation-dot>', xml, re.S):
        did = m.group(1)
        content = m.group(2)
        bounds_m = re.search(r'<bounds x="(\d+)" y="(\d+)" w="(\d+)" h="(\d+)"/>', content)
        if bounds_m:
            aug_dots.append({
                'dot_id': did,
                'x': int(bounds_m.group(1)), 'y': int(bounds_m.group(2)),
                'w': int(bounds_m.group(3)), 'h': int(bounds_m.group(4)),
            })

    # 1. 提取所有head元素
    heads = {}
    for m in re.finditer(
            r'<head\s[^>]*?id="(\d+)"[^>]*?>\s*'
            r'<bounds x="(\d+)" y="(\d+)" w="(\d+)" h="(\d+)"/>\s*'
            r'</head>', xml, re.S):
        hid = m.group(1)
        full = m.group(0)
        pitch_m = re.search(r'pitch="([\d-]+)"', full)
        staff_m = re.search(r'staff="(\d+)"', full)
        shape_m = re.search(r'shape="([^"]+)"', full)
        grade_m = re.search(r'grade="([^"]+)"', full)

        heads[hid] = {
            'head_id': hid,
            'x': int(m.group(2)), 'y': int(m.group(3)),
            'w': int(m.group(4)), 'h': int(m.group(5)),
            'pitch_raw': int(pitch_m.group(1)) if pitch_m else 0,
            'staff': int(staff_m.group(1)) if staff_m else 0,
            'shape': shape_m.group(1) if shape_m else 'UNKNOWN',
            'grade': float(grade_m.group(1)) if grade_m else 0.0,
        }

    # 2. 提取所有head-chord元素
    chords = {}
    for m in re.finditer(
            r'<head-chord\s[^>]*?id="(\d+)"[^>]*?>\s*'
            r'<bounds x="(\d+)" y="(\d+)" w="(\d+)" h="(\d+)"/>\s*'
            r'</head-chord>', xml, re.S):
        cid = m.group(1)
        full = m.group(0)
        staff_m = re.search(r'staff="(\d+)"', full)
        grade_m = re.search(r'grade="([^"]+)"', full)

        chords[cid] = {
            'chord_id': cid,
            'x': int(m.group(2)), 'y': int(m.group(3)),
            'w': int(m.group(4)), 'h': int(m.group(5)),
            'staff': int(staff_m.group(1)) if staff_m else 0,
            'grade': float(grade_m.group(1)) if grade_m else 0.0,
        }

    # 3. 将head映射到chord(空间包含)
    chord_to_heads = {cid: [] for cid in chords}
    for hid, h in heads.items():
        # head中心点
        hx = h['x'] + h['w'] / 2
        hy = h['y'] + h['h'] / 2
        best_cid = None
        best_dist = float('inf')
        for cid, c in chords.items():
            if (c['x'] <= hx <= c['x'] + c['w'] and
                    c['y'] <= hy <= c['y'] + c['h']):
                # 用chord中心距离做tie-break
                cx = c['x'] + c['w'] / 2
                cy = c['y'] + c['h'] / 2
                dist = (hx - cx) ** 2 + (hy - cy) ** 2
                if dist < best_dist:
                    best_dist = dist
                    best_cid = cid
        if best_cid:
            chord_to_heads[best_cid].append(hid)
        # 无匹配的head不丢弃, 用head自身作为"单音chord"

    # 4. 提取system→staff→voice→chord时序
    system_voice_order, unvoiced_chord_ids = _extract_voice_order(xml)

    # 5. 构建chord→system映射(用于unvoiced chords)
    chord_to_system = {}
    for (sys_id, staff_id, voice_id, chord_id) in system_voice_order:
        chord_to_system[chord_id] = (sys_id, staff_id)

    # 同时从measure结构推断unvoiced chord的system
    for sys_m in re.finditer(r'<system id="(\d+)"[^>]*>(.*?)</system>', xml, re.S):
        sys_id = int(sys_m.group(1))
        sys_content = sys_m.group(2)
        staff_m = re.search(r'<staff id="(\d+)"', sys_content)
        staff_id = int(staff_m.group(1)) if staff_m else 1

        for meas_m in re.finditer(r'<measure[^>]*>(.*?)</measure>',
                                  sys_content, re.S):
            hc_m = re.search(r'<head-chords>(.*?)</head-chords>',
                             meas_m.group(1), re.S)
            if hc_m:
                for cid in hc_m.group(1).strip().split():
                    if cid not in chord_to_system:
                        chord_to_system[cid] = (sys_id, staff_id)

    # 6. 组装音符列表, 保持时序
    notes = []
    used_heads = set()

    # 先处理voiced chords(有时序信息)
    for (sys_id, staff_id, voice_id, chord_id) in system_voice_order:
        if chord_id in chord_to_heads:
            head_ids = chord_to_heads[chord_id]
            for hid in head_ids:
                h = heads[hid]
                h['sheet'] = sheet_idx
                h['system'] = sys_id
                h['chord_id'] = chord_id
                h['voice'] = voice_id
                notes.append(h)
                used_heads.add(hid)

    # 7. 处理unvoiced chords — 按system→x排序, 插入到正确位置
    unvoiced_notes = []
    for cid in unvoiced_chord_ids:
        if cid in chord_to_heads:
            sys_info = chord_to_system.get(cid, (-1, 1))
            head_ids = chord_to_heads[cid]
            for hid in head_ids:
                if hid not in used_heads:
                    h = heads[hid]
                    h['sheet'] = sheet_idx
                    h['system'] = sys_info[0]
                    h['chord_id'] = cid
                    h['voice'] = 0
                    unvoiced_notes.append(h)
                    used_heads.add(hid)

    # 8. 处理完全未匹配的head
    unmatched = set(heads.keys()) - used_heads
    if unmatched:
        by_staff = defaultdict(list)
        for hid in unmatched:
            by_staff[heads[hid]['staff']].append(heads[hid])
        for staff_id in sorted(by_staff.keys()):
            for h in sorted(by_staff[staff_id], key=lambda n: n['x']):
                h['sheet'] = sheet_idx
                h['system'] = -1
                h['chord_id'] = ''
                h['voice'] = 0
                unvoiced_notes.append(h)

    # 9. 合并voiced和unvoiced
    # 注意: 不再全局排序, 保留voice顺序
    # unvoiced_notes按x排序后追加
    unvoiced_notes.sort(key=lambda n: (n.get('x', 0), n.get('y', 0)))
    all_notes = notes + unvoiced_notes

    # 10. 链接变音记号到音符头
    # alter元素有pitch属性, 与head的pitch一致, 可精确匹配
    for acc in accidentals:
        acc_pitch = acc.get('pitch')
        ax = acc['x']
        best_note = None
        best_dist = float('inf')
        for n in all_notes:
            # 优先用pitch精确匹配
            if acc_pitch is not None and n.get('pitch_raw') == acc_pitch:
                # 同一pitch, 用x距离做tie-break
                x_dist = abs(n['x'] - ax)
                if x_dist < 200:  # 合理距离
                    if x_dist < best_dist:
                        best_dist = x_dist
                        best_note = n
            elif acc_pitch is None:
                # 无pitch信息, 用位置匹配
                nx, ny = n['x'], n['y']
                if nx > ax and abs(ny - acc['y']) < 30:
                    dist = (nx - ax) + abs(ny - acc['y']) * 2
                    if dist < best_dist:
                        best_dist = dist
                        best_note = n
        if best_note:
            if acc['shape'] == 'SHARP':
                best_note['alter'] = 1
            elif acc['shape'] == 'FLAT':
                best_note['alter'] = -1
            elif acc['shape'] == 'NATURAL':
                best_note['alter'] = 0

    # 11. 标记附点音符
    for n in all_notes:
        cid = n.get('chord_id', '')
        if cid in dotted_chords:
            n['is_dotted'] = True
        else:
            n['is_dotted'] = False

    # 12. 匹配stem到notehead (空间邻近)
    # stem通常在notehead附近 (dx: ±50px, dy: ±100px)
    for n in all_notes:
        n['has_stem'] = False
        n['stem_id'] = None
        ncx = n['x'] + n['w'] / 2
        ncy = n['y'] + n['h'] / 2

        best_stem = None
        best_dist = float('inf')
        for s in stems:
            # stem通常在notehead附近
            scx = s['x'] + s['w'] / 2
            scy = s['y'] + s['h'] / 2
            dx = abs(scx - ncx)
            dy = abs(scy - ncy)
            # 放宽条件: dx<50, dy<100
            if dx < 50 and dy < 100:
                dist = dx + dy
                if dist < best_dist:
                    best_dist = dist
                    best_stem = s

        if best_stem:
            n['has_stem'] = True
            n['stem_id'] = best_stem['stem_id']

    # 13. 匹配flag到notehead (通过stem)
    # flag通常在stem末端
    for n in all_notes:
        n['has_flag'] = False
        n['flag_id'] = None

        if not n.get('has_stem'):
            continue

        # 找到对应的stem
        stem_id = n.get('stem_id')
        stem = next((s for s in stems if s['stem_id'] == stem_id), None)
        if not stem:
            continue

        # flag在stem末端附近
        for f in flags:
            dx = abs(f['x'] - stem['x'])
            dy = abs(f['y'] - (stem['y'] + stem['h']))  # stem末端
            if dx < 30 and dy < 30:
                n['has_flag'] = True
                n['flag_id'] = f['flag_id']
                break

    # 14. 匹配augmentation-dot到notehead (空间邻近)
    for n in all_notes:
        n['has_aug_dot'] = False
        ncx = n['x'] + n['w'] / 2
        ncy = n['y'] + n['h'] / 2

        for d in aug_dots:
            dcx = d['x'] + d['w'] / 2
            dcy = d['y'] + d['h'] / 2
            # dot在notehead右侧50px内, 垂直距离<20px
            dx = dcx - ncx
            dy = abs(dcy - ncy)
            if 0 < dx < 50 and dy < 20:
                n['has_aug_dot'] = True
                # 更新附点标记
                n['is_dotted'] = True
                break

    return all_notes


def _extract_voice_order(xml):
    """
    从XML中提取 (system_id, staff_id, voice_id, chord_id) 的时序列表

    Audiveris XML结构:
    <system id="N">
      <staff id="M"> ... </staff>     ← 五线谱线信息
      <measure id="K">                ← 小节(在system内, 不在staff内)
        <head-chords>id1 id2 ...</head-chords>
        <voice id="V">
          <slots>
            <entry><key>S</key><value chord="CID" status="BEGIN"/></entry>
          </slots>
        </voice>
      </measure>
    </system>

    同时提取<head-chords>中的所有chord ID, 对未被voice引用的chord,
    按x坐标排序作为fallback
    """
    result = []
    all_chord_ids = set()
    voiced_chord_ids = set()

    # 解析system
    for sys_m in re.finditer(r'<system id="(\d+)"[^>]*>(.*?)</system>', xml, re.S):
        sys_id = int(sys_m.group(1))
        sys_content = sys_m.group(2)

        # 提取staff ID(第一个staff)
        staff_m = re.search(r'<staff id="(\d+)"', sys_content)
        staff_id = int(staff_m.group(1)) if staff_m else 1

        # 解析measure(在system内)
        for meas_m in re.finditer(r'<measure[^>]*>(.*?)</measure>',
                                  sys_content, re.S):
            meas_content = meas_m.group(1)

            # 收集该measure的所有head-chord IDs
            hc_m = re.search(r'<head-chords>(.*?)</head-chords>',
                             meas_content, re.S)
            if hc_m:
                for cid in hc_m.group(1).strip().split():
                    all_chord_ids.add(cid)

            # 解析voice entries
            for voice_m in re.finditer(
                    r'<voice id="(\d+)"[^>]*>(.*?)</voice>',
                    meas_content, re.S):
                voice_id = int(voice_m.group(1))
                voice_content = voice_m.group(2)

                for entry_m in re.finditer(
                        r'<entry>\s*<key>(\d+)</key>\s*'
                        r'<value chord="(\d+)"[^/]*/>\s*</entry>',
                        voice_content, re.S):
                    slot_key = int(entry_m.group(1))
                    chord_id = entry_m.group(2)
                    # 跳过rest-chord(ID通常很大, >30000)
                    if int(chord_id) < 30000:
                        result.append((sys_id, staff_id, voice_id,
                                       slot_key, chord_id))
                        voiced_chord_ids.add(chord_id)

    # 排序: system → staff → voice → slot_key
    result.sort(key=lambda x: (x[0], x[1], x[2], x[3]))

    # 返回 (sys_id, staff_id, voice_id, chord_id) 和 未被voice引用的chord IDs
    ordered = [(r[0], r[1], r[2], r[4]) for r in result]
    unvoiced = all_chord_ids - voiced_chord_ids

    return ordered, unvoiced


# ============================================================
# 2. 音高转换: Audiveris pitch → MusicXML pitch
# ============================================================

# 每个 (谱号, 所在线) 上**那条线**是什么音, 写成相对 C4 的全音阶步数
# (C4=0, D4=1, E4=2, F4=3, G4=4, A4=5, B4=6, C3=-7 ...)。
# 这是乐理常识, 独立于本文件的任何代码 ——
# 独立测试在 tools/clefpitch_test.py（含 music21 第三方复核）。
_CLEF_LINE_DIATONIC = {
    ('G', 2): 4,     # 高音谱号: 第 2 线 = G4
    ('G', 1): 4,     # 法式高音谱号: 第 1 线 = G4
    ('F', 4): -4,    # 低音谱号: 第 4 线 = F3
    ('F', 3): -4,    # F 谱号在第 3 线 = F3
    ('C', 1): 0,     # 女高音谱号: 第 1 线 = C4
    ('C', 2): 0,     # 女中音谱号: 第 2 线 = C4
    ('C', 3): 0,     # 中音谱号:   第 3 线 = C4 (中央 C)
    ('C', 4): 0,     # 次中音谱号: 第 4 线 = C4 (中央 C)
    ('C', 5): 0,     # 上低音谱号: 第 5 线 = C4
}

# ★ 调号 fifths -> 每个音名该升/降多少。
#   升号顺序 F C G D A E B（fifths = +1..+7），降号顺序 B E A D G C F（-1..-7）。
#   这是乐理定义，独立测试在 tools/keysig_test.py。
_SHARP_ORDER = ('F', 'C', 'G', 'D', 'A', 'E', 'B')
_FLAT_ORDER = ('B', 'E', 'A', 'D', 'G', 'C', 'F')


def key_alters(fifths):
    """调号 fifths -> ``{音名: alter}``。fifths=0/None -> ``{}``（全部还原）。

    fifths 超过 ±7 时截到 ±7（MusicXML 也只到 ±7）。
    """
    try:
        f = int(fifths)
    except (TypeError, ValueError):
        return {}
    out = {}
    if f > 0:
        for s in _SHARP_ORDER[:min(f, 7)]:
            out[s] = 1
    elif f < 0:
        for s in _FLAT_ORDER[:min(-f, 7)]:
            out[s] = -1
    return out


def pitch_to_musicxml(pitch_raw, clef='G', line=2, clef_by_kind=False,
                      key_fifths=None):
    """
    将Audiveris pitch(谱线位置)转换为MusicXML pitch

    Audiveris pitch约定（**实测**，见 tools/clefpitch_invariant.py）:
    - pitch=0        → **中间那条线**（第 3 线），对任何谱号都一样
    - pitch 每 +1    → 往下半个行距 = 往下一个全音阶步
    - 即 pitch = 2 × (中间线到该音的线/间数)

    `clef_by_kind=False`（默认）: 保持历史行为 —— 只对 G2 正确, F4 差一个八度,
      C 谱号（中音/次中音）完全没处理（当高音谱号算）。
    `clef_by_kind=True`: 按谱号本身算。每种谱号的"中间线上的音":
        G2 → B4   F4 → D3   C3(中音) → C4   C4(次中音) → A3
      G2 的结果与旧路径**逐位相同**（offset 都是 6），所以单声部/彩虹输出不受影响。

    Args:
        pitch_raw: Audiveris pitch值
        clef: 谱号('G', 'F', 'C')
        line: 谱号所在线(高音=2, 低音=4, 中音=3, 次中音=4)
        clef_by_kind: True 时按谱号正确计算（见上）
        key_fifths: 该小节**生效的调号**（MusicXML `<fifths>`）。
            给了就把调号内的升降号叠加到 alter 上（``None``/0 = 不叠加）。
            ★ 只叠加**调号**, 临时升降号由调用方另外覆盖。
            ★ 必须**逐小节**给：实测 f00015 一首里就有 6 种调号（中间转调）。

    Returns:
        (step, octave, alter) tuple
    """
    if clef_by_kind:
        # 该谱号下"中间线（第 3 线）"上的音，写成相对 C4 的全音阶步数。
        # 谱号坐在第 `line` 线上、那条线是该谱号的音（G4 / F3 / C4）,
        # 中间线(第 3 线) 与它相差 2*(3-line) 个全音阶步（向上为正）。
        ref = _CLEF_LINE_DIATONIC.get((clef, line))
        if ref is None:
            ref = _CLEF_LINE_DIATONIC[('G', 2)]
        middle = ref + 2 * (3 - line)
        # pitch=0 在中间线, pitch 增大往下 → 音名递减
        diatonic = middle - pitch_raw
    elif clef == 'G' and line == 2:
        # 高音谱号: 中线(第3线) = B4
        # pitch=0 → B4, pitch=-1 → C5, pitch=1 → A4
        # 实测标定: pitch=9 → G3, pitch=4 → E4 → offset = 6
        diatonic = -pitch_raw + 6
    elif clef == 'F' and line == 4:
        # 低音谱号: 历史行为，比正确值高一个八度（+7 全音阶步）。
        # 保留它以免改变旧调用方的输出；要正确结果请用 clef_by_kind=True。
        diatonic = -pitch_raw + 6 - 7
    else:
        # 默认按高音谱号处理（历史行为；C 谱号在这里是错的）
        diatonic = -pitch_raw + 6

    # diatonic → (step, octave)
    # C4=0, D4=1, E4=2, F4=3, G4=4, A4=5, B4=6, C5=7, ...
    # C3=-7, D3=-6, E3=-5, F3=-4, G3=-3, A3=-2, B3=-1
    # Python整除对负数正确: -3//7=-1, -3%7=4
    octave = 4 + diatonic // 7
    note_idx = diatonic % 7

    step = NOTE_NAMES[note_idx]
    # ★ 调号内的升降号（实测 f00000 是 +1 → F#；f00011 是 -4 → Bb Eb Ab Db）。
    #   临时升降号由调用方（_alter_for_head）覆盖 —— 那是"印出来的记号",
    #   与 <alter> 是两件事, 但都作用在同一个 alter 上。
    alter = key_alters(key_fifths).get(step, 0)

    return step, octave, alter


# ============================================================
# 3. 时值推断: 从shape/stem/voice结构推断duration
# ============================================================

def infer_duration(note, sheet_xml=None):
    """
    推断音符时值

    使用OMR中的shape + stem + flag信息:
    - WHOLE_NOTE → whole (4)
    - NOTEHEAD_VOID + 无stem → half (2)
    - NOTEHEAD_VOID + 有stem + 无flag → quarter (1)
    - NOTEHEAD_VOID + 有stem + 有flag → eighth (0.5)
    - NOTEHEAD_BLACK + 无stem → quarter (1)
    - NOTEHEAD_BLACK + 有stem + 无flag → quarter (1)
    - NOTEHEAD_BLACK + 有stem + 有flag → eighth (0.5)
    - 附点: dur *= 1.5
    """
    shape = note.get('shape', 'UNKNOWN')
    has_stem = note.get('has_stem', False)
    has_flag = note.get('has_flag', False)
    is_dotted = note.get('is_dotted', False)

    if shape == 'WHOLE_NOTE':
        note_type = 'whole'
        dur = 4
    elif shape == 'NOTEHEAD_VOID':
        if has_flag:
            note_type = 'eighth'
            dur = 0.5
        elif has_stem:
            note_type = 'quarter'
            dur = 1
        else:
            note_type = 'half'
            dur = 2
    elif shape == 'NOTEHEAD_BLACK':
        if has_flag:
            note_type = 'eighth'
            dur = 0.5
        else:
            note_type = 'quarter'
            dur = 1
    else:
        # 默认quarter
        note_type = 'quarter'
        dur = 1

    if is_dotted:
        dur *= 1.5

    return note_type, dur


def infer_duration_from_voice(note, voice_duration_map):
    """
    从voice/slot结构推断精确时值

    voice_duration_map: {chord_id: (type, duration)} 从MXL或rhythm分析得到
    """
    cid = note.get('chord_id', '')
    if cid and cid in voice_duration_map:
        return voice_duration_map[cid]
    return None


def extract_dotted_chords(xml):
    """
    从OMR XML中提取附点音符的chord IDs

    <augmentations-dots>cid1 cid2 ...</augmentations-dots>
    这些chord的音符应标记为dotted
    """
    dotted = set()
    for m in re.finditer(r'<augmentations-dots>(.*?)</augmentations-dots>', xml, re.S):
        for cid in m.group(1).strip().split():
            dotted.add(cid)
    return dotted


# ============================================================
# 4. 从Audiveris导出的MXL提取时值映射
# ============================================================

def load_mxl_durations(mxl_path):
    """
    从Audiveris导出的MXL加载已知的时值映射

    Returns:
        dict: {note_index: (type, duration, dotted)}
        以及 chord_id → (type, duration) 映射(如果能建立)
    """
    if not os.path.exists(mxl_path):
        return {}

    tree = ET.parse(mxl_path)
    root = tree.getroot()

    # 不使用命名空间(提取的XML没有命名空间)
    notes = []
    for note in root.iter('note'):
        rest = note.find('rest')
        pitch = note.find('pitch')
        if pitch is not None:
            step = pitch.findtext('step')
            octave = int(pitch.findtext('octave'))
            alter = int(pitch.findtext('alter') or 0)
            typ = note.findtext('type') or 'quarter'
            dotted = note.find('dot') is not None
            dur = DUR_MAP.get(typ, 1)
            if dotted:
                dur *= 1.5
            notes.append({
                'step': step, 'octave': octave, 'alter': alter,
                'type': typ, 'dotted': dotted, 'dur': dur
            })

    return notes


# ============================================================
# 5. MusicXML导出
# ============================================================

def export_musicxml(notes, output_path, divisions=6):
    """
    将解析的音符列表导出为MusicXML

    Args:
        notes: 音符列表, 每个有 step, octave, alter, type, dur, x, system, sheet
        output_path: 输出路径
        divisions: 每拍细分(quarter=divisions个tick)
    """
    # 按页面→系统→x排序
    notes_sorted = sorted(notes, key=lambda n: (
        n.get('sheet', 0),
        n.get('system', 0),
        n.get('x', 0),
        n.get('y', 0)
    ))

    # ★ 和弦分组: 按 chord_id 标记同一和弦内的成员
    # 同一 chord_id 的第一个音符正常输出, 后续音符加 <chord/>
    chord_seen = set()
    for n in notes_sorted:
        cid = n.get('chord_id', '')
        if cid:
            if cid in chord_seen:
                n['is_chord_member'] = True
            else:
                n['is_chord_member'] = False
                chord_seen.add(cid)
        else:
            n['is_chord_member'] = False

    # 去重: 同一chord内的多个head只保留一个音符(和弦)
    # 实际上和弦内的每个head都是独立音符, 保留所有
    # 但要去除完全重复的(同位置同音高)
    seen = set()
    deduped = []
    for n in notes_sorted:
        key = (n.get('sheet', 0), n.get('system', 0),
               n.get('x', 0), n.get('y', 0),
               n.get('step', ''), n.get('octave', 0))
        if key not in seen:
            seen.add(key)
            deduped.append(n)
    notes_sorted = deduped

    # 构建MusicXML
    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 3.1 Partwise//EN" '
        '"http://www.musicxml.org/dtds/partwise.dtd">',
        '<score-partwise version="3.1">',
        '  <work>',
        '    <work-title>彩虹青春 - Audiveris OMR</work-title>',
        '  </work>',
        '  <identification>',
        '    <creator type="composer">Clarinet (A)</creator>',
        '    <encoding>',
        '      <software>Audiveris OMR Parser</software>',
        '    </encoding>',
        '  </identification>',
        '  <part-list>',
        '    <score-part id="P1">',
        '      <part-name>Clarinet (A)</part-name>',
        '      <score-instrument id="P1-I1">',
        '        <instrument-name>Clarinet (A)</instrument-name>',
        '      </score-instrument>',
        '      <midi-instrument id="P1-I1">',
        '        <midi-channel>1</midi-channel>',
        '        <midi-program>71</midi-program>',
        '      </midi-instrument>',
        '    </score-part>',
        '  </part-list>',
        '  <part id="P1">',
    ]

    # 按系统分组, 每个系统作为连续的小节
    measure_num = 1
    current_system = None
    current_sheet = None

    # 为每个音符分配时值
    for i, n in enumerate(notes_sorted):
        # 检测是否需要新小节(新系统或间隔较大)
        new_measure = False
        if current_system is None:
            new_measure = True
        elif (n.get('sheet', 0) != current_sheet or
              n.get('system', 0) != current_system):
            new_measure = True

        if new_measure:
            if measure_num > 1:
                xml_lines.append('    </measure>')
            xml_lines.append(f'    <!--=======================================================-->')
            xml_lines.append(f'    <measure number="{measure_num}">')

            # 第一个小节添加attributes
            if measure_num == 1:
                xml_lines.append('      <attributes>')
                xml_lines.append(f'        <divisions>{divisions}</divisions>')
                xml_lines.append('        <key>')
                xml_lines.append('          <fifths>0</fifths>')
                xml_lines.append('        </key>')
                xml_lines.append('        <time>')
                xml_lines.append('          <beats>4</beats>')
                xml_lines.append('          <beat-type>4</beat-type>')
                xml_lines.append('        </time>')
                xml_lines.append('        <clef>')
                xml_lines.append('          <sign>G</sign>')
                xml_lines.append('          <line>2</line>')
                xml_lines.append('        </clef>')
                xml_lines.append('      </attributes>')

            current_system = n.get('system', 0)
            current_sheet = n.get('sheet', 0)
            measure_num += 1

        # 音符类型和时值
        note_type = n.get('type', 'half')
        dotted = n.get('dotted', False)
        dur_quarter = DUR_MAP.get(note_type, 1)
        if dotted:
            dur_quarter *= 1.5
        dur_ticks = int(dur_quarter * divisions)

        # 写入音符
        step = n.get('step', 'C')
        octave = n.get('octave', 4)
        alter = n.get('alter', 0)

        xml_lines.append('      <note>')
        # ★ 和弦成员标记: 同一chord_id的后续音符加 <chord/>
        if n.get('is_chord_member', False):
            xml_lines.append('        <chord/>')
        xml_lines.append('        <pitch>')
        xml_lines.append(f'          <step>{step}</step>')
        if alter != 0:
            xml_lines.append(f'          <alter>{alter}</alter>')
        xml_lines.append(f'          <octave>{octave}</octave>')
        xml_lines.append('        </pitch>')
        xml_lines.append(f'        <duration>{dur_ticks}</duration>')
        xml_lines.append(f'        <type>{note_type}</type>')
        if dotted:
            xml_lines.append('        <dot/>')
        xml_lines.append('      </note>')

    if measure_num > 1:
        xml_lines.append('    </measure>')

    xml_lines.append('  </part>')
    xml_lines.append('</score-partwise>')

    # 写入文件
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(xml_lines))

    return output_path


# ============================================================


# 说明：原文件后半段是"与某商业 OMR 做真值对齐评估"的工具代码，
# 属于派生用途，**不在本发布物内**。此处只保留导出器需要的纯函数：
#   pitch_to_musicxml / infer_duration
