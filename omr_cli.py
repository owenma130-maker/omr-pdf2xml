#!/usr/bin/env python
"""OMR 命令行入口 —— 此项目此前没有可用入口, 只能手敲 python -c。

目标导向的三个子命令:
  omr2xml  .omr  -> MusicXML   (结构化导出, 保留小节/声部/休止/小节线)
  pdf2xml  PDF   -> .omr -> MusicXML  (需本机安装 Audiveris)
  eval     MusicXML 对照开放许可真值做【结构分项】评测
  status   打印当前进度 vs 目标

用法:
  python omr_cli.py omr2xml <file.omr> [-o out.musicxml]
  python omr_cli.py pdf2xml <file.pdf> [-o out.musicxml] [--audiveris <path>]
  python omr_cli.py eval <pred.musicxml> [--truth <truth.musicxml>]
  python omr_cli.py status
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DEFAULT_TRUTH = None   # 不再默认指向任何厂商输出; 必须用 --truth 显式指定开放许可真值
OPEN_TRUTH_HINT = """开放许可评测集 (CC BY-SA, 直接提供 MusicXML):
  OLiMPiC-scanned : https://github.com/ufal/olimpic-icdar24  (make install-olimpic-scanned)
  GrandStaff-LMX  : https://huggingface.co/datasets/jhlusko/grandstaff_lmx
下载后用 --truth <file.musicxml> 指定即可。"""
DEFAULT_OMR = ROOT / "data" / "test_rainbow" / "out" / "彩虹 青春 cl声部(1).omr"


# ----------------------------------------------------------------------
# 统计 MusicXML 的结构量
# ----------------------------------------------------------------------
def musicxml_stats(path: Path) -> dict:
    t = path.read_text(encoding="utf-8", errors="replace")
    return {
        "measure": len(re.findall(r"<measure[ >]", t)),
        "note": len(re.findall(r"<note[ >]", t)),
        "pitch": len(re.findall(r"<pitch>", t)),
        "rest": len(re.findall(r"<rest\s*/>", t)),
        "chord_mark": len(re.findall(r"<chord\s*/>", t)),
        "barline": len(re.findall(r"<barline", t)),
        "voice": len(re.findall(r"<voice>", t)),
        # 注意: <backup>/<forward> 不是自闭合标签, 不能用 />
        "backup": len(re.findall(r"<backup[ >]", t)),
        "forward": len(re.findall(r"<forward[ >]", t)),
        "attributes": len(re.findall(r"<attributes>", t)),
    }


def type_dist(path: Path) -> dict:
    from collections import Counter
    t = path.read_text(encoding="utf-8", errors="replace")
    return dict(Counter(re.findall(r"<type>(\w+)</type>", t)))


# ----------------------------------------------------------------------
# omr2xml
# ----------------------------------------------------------------------
def cmd_omr2xml(args):
    omr = Path(args.omr)
    if not omr.exists():
        print(f"[错误] 找不到 .omr: {omr}")
        return 2
    out = Path(args.output) if args.output else ROOT / "results" / "structural_export" / (omr.stem + ".musicxml")
    out.parent.mkdir(parents=True, exist_ok=True)

    from omr_engine.fusion.omr_structural_export import export_structural_musicxml
    stats = export_structural_musicxml(str(omr), str(out), verbose=args.verbose)

    print(f"\n[OK] 已写出: {out}")
    print(f"     小节={stats.get('measures_emitted')}  音符={stats.get('heads_emitted')}"
          f"  休止={stats.get('rests_emitted_total')}"
          f"  和弦={stats.get('chord_mark_count')}"
          f"  丢弃={len(stats.get('drop_reasons') or {})}")
    if stats.get("drop_reasons"):
        print(f"     [警告] 有丢弃: {stats['drop_reasons']}")
    return 0


# ----------------------------------------------------------------------
# pdf2xml
# ----------------------------------------------------------------------
def find_audiveris(explicit=None):
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    for name in ("audiveris", "Audiveris", "audiveris.bat"):
        w = shutil.which(name)
        if w:
            return Path(w)
    for cand in (Path(r"C:\Program Files\Audiveris\audiveris.bat"),
                 Path(r"C:\Program Files (x86)\Audiveris\audiveris.bat"),
                 Path(os.path.expanduser(r"~\Audiveris\audiveris.bat"))):
        if cand.exists():
            return cand
    return None


def cmd_pdf2xml(args):
    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"[错误] 找不到 PDF: {pdf}")
        return 2

    av = find_audiveris(args.audiveris)
    if av is None:
        print("[错误] 未找到 Audiveris。PDF->.omr 这一步依赖 Audiveris (Java)。")
        print("       解决方式(任一):")
        print("         1) 安装 Audiveris 后用 --audiveris <path/to/audiveris.bat> 指定")
        print("         2) 先手工生成 .omr, 再跑: python omr_cli.py omr2xml <file.omr>")
        return 3

    # ★ 产物写在【用户 PDF 旁边】，不写在包目录里。
    #   早期版本写 `ROOT/results/...`，而 ROOT 是包安装目录 ——
    #   用户一跑就会往 site-packages 里写东西（干净安装实测发现的）。
    work = pdf.parent / (pdf.stem + "_omr")
    work.mkdir(parents=True, exist_ok=True)
    print(f"[1/2] Audiveris: {av}")
    print(f"      {pdf.name} -> {work}")
    try:
        r = subprocess.run([str(av), "-batch", "-output", str(work),
                            "-export", str(pdf)],
                           capture_output=True, text=True, errors='replace')
    except OSError as e:
        print(f"[错误] 无法运行 Audiveris: {e}")
        return 4
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or '')[-800:]
        # ★ 实测最常见的失败是 Java 堆/页面文件不够，报错信息藏在 Audiveris 的
        #   原始输出里，用户看不懂。这里把它翻译成一句人话。
        low = tail.lower()
        if ('insufficient memory' in low or 'outofmemory' in low
                or 'commit_memory' in low):
            print("[错误] Audiveris 内存不足（Java 堆申请失败）。")
            print("       常见原因：系统页面文件/可用内存太小。可尝试：")
            print("         · 关掉其它占内存的程序后重跑")
            print("         · 设置环境变量 JAVA_TOOL_OPTIONS=-Xmx2g 再跑")
        else:
            print(f"[错误] Audiveris 失败 (exit {r.returncode})")
        if tail.strip():
            print("       Audiveris 最后几行输出：")
            for ln in tail.strip().splitlines()[-6:]:
                print(f"         {ln}")
        return 4

    omrs = list(work.rglob("*.omr"))
    if not omrs:
        print(f"[错误] Audiveris 未产出 .omr, 请检查 {work}")
        return 5
    omr = omrs[0]
    print(f"[2/2] 结构化导出 <- {omr.name}")
    return cmd_omr2xml(argparse.Namespace(omr=str(omr), output=args.output,
                                          verbose=args.verbose))


# ----------------------------------------------------------------------
# eval
# ----------------------------------------------------------------------
def cmd_eval(args):
    pred = Path(args.pred)
    if not pred.exists():
        print(f"[错误] 找不到预测文件: {pred}")
        return 2
    truth = Path(args.truth) if args.truth else DEFAULT_TRUTH
    if truth is None:
        print("[错误] 未指定真值文件。本项目【不再内置任何厂商输出的真值】")
        print("       以保证交付物零法律风险。请用 --truth 指定开放许可真值。")
        print()
        print(OPEN_TRUTH_HINT)
        return 3
    if not truth.exists():
        print(f"[错误] 找不到真值: {truth}")
        return 3

    pred_stats = musicxml_stats(pred)
    truth_stats = musicxml_stats(truth)
    pd = type_dist(pred)
    td = type_dist(truth)

    print("=" * 88)
    print("结构分项评测 (对照开放许可真值)")
    print("=" * 88)
    print(f"  预测: {pred}")
    print(f"  真值: {truth}")
    print(f"\n  {'项目':<14}{'预测':>9}{'真值':>9}{'差':>9}{'达成':>10}")
    print("  " + "-" * 54)
    for k in ("measure", "note", "pitch", "rest", "chord_mark", "barline",
              "voice", "backup", "forward"):
        a, b = pred_stats[k], truth_stats[k]
        if k == "barline":
            # MusicXML 里普通小节线是隐式的; <barline> 只用于特殊/显式标注。
            # 输出更多显式小节线是合法的, 不构成错误, 故不给"达成率"。
            print(f"  {k:<14}{a:>9}{b:>9}{a-b:>+9}{'显式(合法)':>10}")
            continue
        if b == 0:
            ach = "n/a" if a == 0 else "真值0"
        else:
            ach = f"{100*min(a,b)/max(a,b):.1f}%"
        print(f"  {k:<14}{a:>9}{b:>9}{a-b:>+9}{ach:>10}")

    print(f"\n  {'时值类型':<14}{'预测':>9}{'真值':>9}{'差':>9}")
    print("  " + "-" * 45)
    keys = [k for k in ("whole", "half", "quarter", "eighth", "16th")
            if pd.get(k, 0) or td.get(k, 0)]
    for k in keys:
        a, b = pd.get(k, 0), td.get(k, 0)
        print(f"  {k:<14}{a:>9}{b:>9}{a-b:>+9}")
    total = sum(abs(pd.get(k, 0) - td.get(k, 0)) for k in set(pd) | set(td))
    print(f"  {'偏差合计':<14}{total:>9}")
    if keys:
        print(f"  {'平均每类偏差':<14}{total/len(keys):>9.1f}")

    print("\n  ⚠️ 不要用 49.9% 作为音高准确率 —— 那是允许自由重排的 DTW 指标,")
    print("     诚实基线是 5.8% (直接 1:1 匹配)。详见 docs/STATUS_VS_GOAL.md §5")
    return 0


# ----------------------------------------------------------------------
# check —— 自检报告（本产品与黑箱 OMR 的区别所在）
# ----------------------------------------------------------------------
def cmd_check(args):
    """拿纸面当标准答案，给转出来的 MusicXML 出一份成绩单。

    ★ 判据全部来自 PDF 自身（印刷小节号 / 相邻页首号相减 / 多小节休止粗横杠），
      **不是**再识别一遍。读不出就如实说"没有裁判"，不编数字。
    """
    from omr_engine import referee

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"[错误] 找不到 PDF: {pdf}")
        return 3
    xml = Path(args.musicxml) if args.musicxml else None
    if xml is not None and not xml.exists():
        print(f"[错误] 找不到 MusicXML: {xml}")
        return 3
    omr = Path(args.omr) if args.omr else None
    rep = referee.check(pdf, xml, omr_path=omr,
                        single_part=bool(args.single_part))
    print(referee.format_report(rep))
    if args.json:
        import json
        Path(args.json).write_text(
            json.dumps(rep, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f"\n[写出] {args.json}")
    return 0


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------
def cmd_status(args):
    print("=" * 88)
    print("omr-pdf2xml v0.1 —— 关键产物检查")
    print("=" * 88)
    checks = [
        ("结构化导出器", ROOT / "omr_engine" / "fusion" / "omr_structural_export.py"),
        ("独立裁判", ROOT / "omr_engine" / "referee.py"),
        ("最小测试", ROOT / "tests" / "test_referee.py"),
        ("许可", ROOT / "LICENSE"),
    ]
    for name, p in checks:
        print(f"  {'OK  ' if p.exists() else 'MISS'} {name:<16} {p}")
    print("\n  评测请用 --truth 指定开放许可真值（本项目不内置任何厂商输出）:")
    for ln in OPEN_TRUTH_HINT.splitlines():
        print("        " + ln)
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="omr_cli", description="OMR 命令行入口 (PDF/.omr -> MusicXML)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("omr2xml", help=".omr -> MusicXML (结构化)")
    p1.add_argument("omr")
    p1.add_argument("-o", "--output")
    p1.add_argument("-q", "--quiet", dest="verbose", action="store_false", default=True)
    p1.set_defaults(func=cmd_omr2xml)

    p2 = sub.add_parser("pdf2xml", help="PDF -> .omr -> MusicXML (需 Audiveris)")
    p2.add_argument("pdf")
    p2.add_argument("-o", "--output")
    p2.add_argument("--audiveris", help="audiveris 可执行文件路径")
    p2.add_argument("-q", "--quiet", dest="verbose", action="store_false", default=True)
    p2.set_defaults(func=cmd_pdf2xml)

    p3 = sub.add_parser("eval", help="结构分项评测 (对照开放许可真值)")
    p3.add_argument("pred")
    p3.add_argument("--truth")
    p3.set_defaults(func=cmd_eval)

    p5 = sub.add_parser(
        "check",
        help="自检报告: 拿纸面当标准答案，给输出打分（本产品的差异点）")
    p5.add_argument("pdf", help="原始乐谱 PDF")
    p5.add_argument("--musicxml", help="要检查的 MusicXML（可省，只看输入侧）")
    p5.add_argument("--omr", help=".omr（给了才能算多小节休止展开数）")
    p5.add_argument("--single-part", action="store_true",
                    help="单谱表分谱：小节线在一行里只出现一次，判据要放宽")
    p5.add_argument("--json", help="把报告另存为 JSON")
    p5.set_defaults(func=cmd_check)

    p4 = sub.add_parser("status", help="打印当前进度 vs 目标")
    p4.set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
