# -*- coding: utf-8 -*-
"""omr-pdf2xml 图形界面（Tkinter，只用标准库，方便打包成 exe）。

为什么要有它：命令行对做音乐的人不友好。这个窗口只做三件事 ——
  选 PDF → 跑 → 把【自检报告】和 MusicXML 一起给你。

界面刻意保持极简：识别那一层是 Audiveris（外部程序），
本工具的差异点是**自检报告**，所以报告占最大的位置。
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def find_audiveris(explicit: str | None = None):
    """统一走 omr_engine.runtime（环境变量 → 自带 → 系统 → PATH）。"""
    from omr_engine import runtime
    info = runtime.find_audiveris(explicit)
    return info.path if info else None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('omr-pdf2xml v0.1 —— 乐谱 PDF → MusicXML（带自检报告）')
        root.geometry('980x680')
        self.q: queue.Queue = queue.Queue()
        self.busy = False

        top = ttk.Frame(root, padding=10)
        top.pack(fill='x')

        ttk.Label(top, text='乐谱 PDF：').grid(row=0, column=0, sticky='w')
        self.pdf_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.pdf_var, width=74).grid(
            row=0, column=1, sticky='we', padx=4)
        ttk.Button(top, text='选择…', command=self.pick_pdf).grid(row=0, column=2)

        ttk.Label(top, text='Audiveris：').grid(row=1, column=0, sticky='w',
                                                pady=(6, 0))
        self.av_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.av_var, width=74).grid(
            row=1, column=1, sticky='we', padx=4, pady=(6, 0))
        ttk.Button(top, text='自动检测', command=self.detect_av).grid(
            row=1, column=2, pady=(6, 0))
        top.columnconfigure(1, weight=1)

        bar = ttk.Frame(root, padding=(10, 0))
        bar.pack(fill='x')
        self.btn_full = ttk.Button(bar, text='转换（PDF → MusicXML）',
                                   command=lambda: self.run(True))
        self.btn_full.pack(side='left')
        self.btn_check = ttk.Button(bar, text='只做自检（不转）',
                                    command=lambda: self.run(False))
        self.btn_check.pack(side='left', padx=6)
        ttk.Button(bar, text='另存 MusicXML…',
                   command=self.save_xml).pack(side='left', padx=6)
        ttk.Button(bar, text='清空', command=self.clear).pack(side='left')

        self.status = tk.StringVar(value='就绪')
        ttk.Label(root, textvariable=self.status, padding=(10, 6)).pack(
            fill='x', anchor='w')

        mid = ttk.Frame(root, padding=(10, 0, 10, 10))
        mid.pack(fill='both', expand=True)
        self.txt = tk.Text(mid, wrap='none', font=('Consolas', 9),
                           background='#11151a', foreground='#e6e6e6',
                           insertbackground='#e6e6e6')
        ysb = ttk.Scrollbar(mid, orient='vertical', command=self.txt.yview)
        self.txt.configure(yscrollcommand=ysb.set)
        self.txt.pack(side='left', fill='both', expand=True)
        ysb.pack(side='right', fill='y')

        self.last_xml: Path | None = None
        self.detect_av()
        self.root.after(120, self.drain)

    # ---------------- 基本交互 ----------------
    def pick_pdf(self):
        f = filedialog.askopenfilename(
            title='选择乐谱 PDF', filetypes=[('PDF', '*.pdf'), ('所有文件', '*.*')])
        if f:
            self.pdf_var.set(f)

    def detect_av(self):
        av = find_audiveris(self.av_var.get() or None)
        if av:
            self.av_var.set(str(av))
            self.say(f'找到 Audiveris: {av}')
        else:
            self.say('★ 没找到 Audiveris。PDF→.omr 这一步是它做的，'
                     '请先安装，或点"自动检测"旁边的输入框手工填写路径。')

    def clear(self):
        self.txt.delete('1.0', 'end')

    def say(self, s):
        self.q.put(s if s.endswith('\n') else s + '\n')

    def save_xml(self):
        if not self.last_xml or not self.last_xml.exists():
            messagebox.showinfo('提示', '还没有生成 MusicXML。')
            return
        dst = filedialog.asksaveasfilename(
            defaultextension='.musicxml',
            initialfile=self.last_xml.name,
            filetypes=[('MusicXML', '*.musicxml'), ('所有文件', '*.*')])
        if dst:
            Path(dst).write_bytes(self.last_xml.read_bytes())
            self.say(f'已另存: {dst}')

    # ---------------- 跑 ----------------
    def run(self, full: bool):
        if self.busy:
            return
        pdf = Path(self.pdf_var.get().strip())
        if not pdf.exists():
            messagebox.showerror('错误', '请先选择一个存在的 PDF。')
            return
        self.busy = True
        self.status.set('工作…')
        self.btn_full.state(['disabled'])
        self.btn_check.state(['disabled'])
        threading.Thread(target=self._work, args=(pdf, full), daemon=True).start()

    def _work(self, pdf: Path, full: bool):
        try:
            from omr_engine import referee
            omr = None
            av = self.av_var.get().strip()
            if full:
                avp = find_audiveris(av or None)
                if avp is None:
                    self.say('★ 没有 Audiveris，无法从 PDF 生成 .omr。'
                             '可以先用别的方式得到 .omr 再跑"只做自检"。')
                    return
                work = pdf.parent / (pdf.stem + '_omr')
                work.mkdir(parents=True, exist_ok=True)
                self.say(f'[1/2] Audiveris 正在识别…（这一步最慢）\n  → {work}')
                r = subprocess.run([str(avp), '-batch', '-output', str(work),
                                    '-export', str(pdf)],
                                   capture_output=True, text=True,
                                   errors='replace')
                if r.returncode != 0:
                    low = ((r.stderr or '') + (r.stdout or '')).lower()
                    if 'insufficient memory' in low or 'outofmemory' in low:
                        self.say('★ Audiveris 内存不足（Java 堆申请失败）。'
                                 '关掉别的程序，或设 JAVA_TOOL_OPTIONS=-Xmx2g 再试。')
                    else:
                        self.say(f'★ Audiveris 失败 (exit {r.returncode})')
                    return
                omrs = list(work.rglob('*.omr'))
                if not omrs:
                    self.say('★ Audiveris 没有产出 .omr。')
                    return
                omr = omrs[0]
                self.say(f'[2/2] 结构化导出 ← {omr.name}')

            xml = pdf.parent / (pdf.stem + '.musicxml')
            if full and omr is not None:
                from omr_engine.fusion.omr_structural_export import (
                    export_structural_musicxml)
                export_structural_musicxml(omr, xml, verbose=False,
                                           no_sidecars=True)
                self.last_xml = xml
                self.say(f'已写出 MusicXML: {xml}')

            self.say('')
            rep = referee.check(pdf, xml if xml.exists() else None,
                                omr_path=omr)
            self.say(referee.format_report(rep))
            self.last_report = rep
        except Exception as e:                     # 界面上不要抛栈
            import traceback
            self.say('★ 出错：' + str(e))
            self.say(traceback.format_exc()[-1200:])
        finally:
            self.root.after(0, self._done)

    def _done(self):
        self.busy = False
        self.status.set('就绪')
        self.btn_full.state(['!disabled'])
        self.btn_check.state(['!disabled'])

    def drain(self):
        try:
            while True:
                self.txt.insert('end', self.q.get_nowait())
                self.txt.see('end')
        except queue.Empty:
            pass
        self.root.after(120, self.drain)


def _force_utf8_stdio():
    """把 stdout/stderr 强制成 UTF-8（出错时用替换字符，不抛异常）。

    为什么必须由程序自己做：本项目所有输出都是中文，而 Windows 控制台
    默认是 cp936/cp1252。打包成 exe 后，靠 PYTHONIOENCODING 环境变量
    并不可靠（实测 CI 里设了仍然崩），所以启动时自己改。
    """
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def main():
    _force_utf8_stdio()
    root = tk.Tk()
    try:
        ttk.Style().theme_use('vista')
    except tk.TclError:
        pass
    App(root)
    # ★ 启动时把自己提到最前。不这么做窗口会开在浏览器后面，
    #   用户会以为"点了没反应"。置顶只保持 0.6 秒，之后恢复普通层级，
    #   免得一直压着别的窗口。
    try:
        root.lift()
        root.attributes('-topmost', True)
        root.after(600, lambda: root.attributes('-topmost', False))
        root.focus_force()
    except tk.TclError:
        pass
    root.mainloop()


if __name__ == '__main__':
    main()
