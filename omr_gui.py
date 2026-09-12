# -*- coding: utf-8 -*-
"""omr-pdf2xml 图形界面（Tkinter，只用标准库，方便打包成 exe）。

设计原则（都是被真实使用逼出来的）：
  1. **每一步都要说话**：识别那一步是分钟级的，界面不能长时间不动，
     否则用户以为卡死了。
  2. **日志要能整段复制**：出问题时用户要能把"卡在哪"直接贴给别人。
     所以有「复制日志」按钮，且每行带 [分:秒] 时间戳。
  3. **看到什么就是什么**：Audiveris 自己的输出也实时转进来，不藏。
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _force_utf8_stdio():
    """把 stdout/stderr 强制成 UTF-8（出错时用替换字符，不抛异常）。

    本项目所有输出都是中文，而 Windows 控制台默认是 cp936/cp1252。
    打包成 exe 后靠 PYTHONIOENCODING 并不可靠（实测 CI 设了仍崩），
    所以启动时自己改。
    """
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def find_audiveris(explicit: str | None = None):
    """统一走 omr_engine.runtime（环境变量 → 自带 → 系统 → PATH）。"""
    from omr_engine import runtime
    info = runtime.find_audiveris(explicit)
    return info.path if info else None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('omr-pdf2xml v0.1 —— 乐谱 PDF → MusicXML（带自检报告）')
        root.geometry('1000x700')
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.t0 = time.time()
        self.last_out = time.time()
        # 当前阶段。心跳要按阶段说实话 —— 原来不管在哪一步都说
        # "Audiveris 在算"，而导出一本 92 页的谱子要跑 5 分钟，
        # Audiveris 那时早就退出了，用户看久了会以为卡死。
        self.phase = ''

        top = ttk.Frame(root, padding=10)
        top.pack(fill='x')

        ttk.Label(top, text='乐谱 PDF：').grid(row=0, column=0, sticky='w')
        self.pdf_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.pdf_var, width=76).grid(
            row=0, column=1, sticky='we', padx=4)
        ttk.Button(top, text='选择…', command=self.pick_pdf).grid(row=0, column=2)

        ttk.Label(top, text='Audiveris：').grid(row=1, column=0, sticky='w',
                                                pady=(6, 0))
        self.av_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.av_var, width=76).grid(
            row=1, column=1, sticky='we', padx=4, pady=(6, 0))
        ttk.Button(top, text='自动检测', command=self.detect_av).grid(
            row=1, column=2, pady=(6, 0))

        # ★ 只处理前 N 页：Audiveris 是分钟级的，测试时没必要每次跑整本。
        ttk.Label(top, text='只处理前 N 页：').grid(row=2, column=0, sticky='w',
                                                   pady=(6, 0))
        self.pages_var = tk.StringVar(value='0')
        ttk.Spinbox(top, from_=0, to=999, width=6,
                    textvariable=self.pages_var).grid(row=2, column=1,
                                                      sticky='w', padx=4,
                                                      pady=(6, 0))
        ttk.Label(top, text='0 = 全部页（测试时填 1 会快很多）').grid(
            row=2, column=1, sticky='w', padx=(80, 0), pady=(6, 0))

        # ★ 复用已有 .omr：Audiveris 识别一本 90 页的谱子要 20 分钟以上，
        #   而导出只要 5 分钟。改了导出逻辑后想再试一次，没必要重跑识别。
        self.reuse_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='复用已有 .omr（跳过识别，只重跑导出）',
                        variable=self.reuse_var).grid(row=3, column=1,
                                                      sticky='w', pady=(4, 0))
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
        ttk.Button(bar, text='复制日志',
                   command=self.copy_log).pack(side='left', padx=6)
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
        self.root.after(100, self.drain)

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
            self.say('★ 没找到 Audiveris。识别这一步是它做的，'
                     '请先安装，或在上面输入框手工填路径。')

    def clear(self):
        self.txt.delete('1.0', 'end')

    def copy_log(self):
        """把整段日志放进剪贴板 —— 出问题时用户直接粘贴给别人。"""
        self.root.clipboard_clear()
        self.root.clipboard_append(self.txt.get('1.0', 'end'))
        self.say('（日志已复制到剪贴板）')

    def say(self, s):
        for ln in str(s).splitlines() or ['']:
            self.q.put(ln)

    def save_xml(self):
        if not self.last_xml or not self.last_xml.exists():
            messagebox.showinfo('提示', '还没有生成 MusicXML。')
            return
        dst = filedialog.asksaveasfilename(
            defaultextension='.musicxml', initialfile=self.last_xml.name,
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
        self.t0 = time.time()
        self.last_out = time.time()
        self.status.set('工作…')
        self.btn_full.state(['disabled'])
        self.btn_check.state(['disabled'])
        self.say('=' * 60)
        self.say(f'开始：{pdf.name}   模式：{"转换" if full else "只做自检"}')
        threading.Thread(target=self._work, args=(pdf, full),
                         daemon=True).start()

    def _reuse_omr(self, work: Path):
        """勾了"复用已有 .omr"且目录里确实有，就返回最新的那个。"""
        if not self.reuse_var.get():
            return None
        cand = sorted(work.rglob('*.omr'),
                      key=lambda q: q.stat().st_mtime, reverse=True)
        if not cand:
            self.say('提示：没找到已有 .omr，还是要跑识别。')
            return None
        omr = cand[0]
        self.say('[1/2] 复用已有 .omr（跳过 Audiveris 识别）')
        self.say(f'      {omr.name}  {omr.stat().st_size / 1048576:.1f} MB  '
                 + time.strftime('%Y-%m-%d %H:%M',
                                 time.localtime(omr.stat().st_mtime)))
        self.say('      注意：只重跑导出。要改识别参数请取消勾选。')
        return omr

    def _run_audiveris(self, pdf: Path, work: Path):
        """跑 Audiveris，返回 .omr 路径；失败返回 None。"""
        avp = find_audiveris(self.av_var.get().strip() or None)
        if avp is None:
            self.say('★ 没有 Audiveris，无法从 PDF 生成 .omr。')
            return None
        self.say('[1/2] Audiveris 识别中（这一步最慢，通常 1~3 分钟）')
        self.phase = 'audiveris'
        self.say(f'      程序: {avp}')
        self.say(f'      输出目录: {work}')
        cmd = [str(avp), '-batch', '-output', str(work),
               '-export', str(pdf)]
        try:
            n = int(self.pages_var.get() or 0)
        except ValueError:
            n = 0
        if n > 0:
            # Audiveris 的 -sheets 接受 "1 4-5" 这样的写法
            cmd.insert(1, f'1-{n}')
            cmd.insert(1, '-sheets')
            self.say(f'      只处理前 {n} 页（-sheets 1-{n}）')
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             errors='replace', bufsize=1,
                             encoding='utf-8')
        # ★ 逐行转发 Audiveris 的输出：它自己会报走到哪一步
        #   （LOAD / BINARY / GRID / HEADS / RHYTHMS / PAGE …）
        for line in p.stdout:                          # type: ignore
            line = line.rstrip()
            if line:
                self.last_out = time.time()
                self.say('    ' + line[:160])
        rc = p.wait()
        # ★ 先看 .omr，再看 exit code。
        #   Audiveris 是【逐页增量存盘】的（每识别完一页就写一次 .omr），
        #   而它的 MusicXML 导出是【整本一次性事务】—— 任何一页失败就
        #   "Could not export since transcription did not complete
        #   successfully" 然后 exit 1，连一页的 MusicXML 都不给。
        #   用户实测：92 页的书里只有 2 页空白/歪斜，等了 22 分钟只看到
        #   "★ Audiveris 失败 (exit 1)"，90 页好页的成果全被丢掉。
        omrs = list(work.rglob('*.omr'))
        if rc != 0:
            if not omrs:
                self.say(f'★ Audiveris 失败 (exit {rc})，且没有产出 .omr。')
                return None
            self.say(f'⚠ Audiveris 报了错 (exit {rc})，'
                     f'但 .omr 已逐页存盘。')
            self.say('  原因通常是某几页空白 / 歪斜 / 分辨率异常。'
                     '这不影响其余页面，继续导出。')
            try:
                from omr_cli import list_empty_sheets
                bad = list_empty_sheets(omrs[0])
            except Exception:
                bad = []
            if bad:
                self.say('  读不了的页（会被跳过）: '
                         + ', '.join(f'第{n}页' for n in bad))
        if not omrs:
            self.say('★ Audiveris 没有产出 .omr。')
            return None
        omr = omrs[0]
        self.say(f'      ✔ .omr 已生成: {omr.name}')
        return omr

    def _work(self, pdf: Path, full: bool):
        try:
            from omr_engine import referee
            omr = None
            if full:
                work = pdf.parent / (pdf.stem + '_omr')
                work.mkdir(parents=True, exist_ok=True)
                omr = self._reuse_omr(work)
                if omr is None:
                    omr = self._run_audiveris(pdf, work)
                if omr is None:
                    return

            xml = pdf.parent / (pdf.stem + '.musicxml')
            if full and omr is not None:
                self.say('[2/2] 结构化导出 → MusicXML')
                self.phase = 'export'
                self.last_out = time.time()
                from omr_engine.fusion.omr_structural_export import (
                    export_structural_musicxml)
                export_structural_musicxml(omr, xml, verbose=False,
                                           no_sidecars=True)
                self.last_xml = xml
                self.say(f'      ✔ 已写出: {xml}')
            else:
                self.say('[2/2] 跳过导出（只做自检）')

            self.say('')
            self.say('正在生成自检报告…')
            rep = referee.check(pdf, xml if xml.exists() else None,
                                omr_path=omr)
            self.say(referee.format_report(rep))
            self.say(f'完成，共 {time.time() - self.t0:.0f} 秒。')
        except Exception as e:                     # 界面上不要抛栈
            import traceback
            self.say('★ 出错：' + str(e))
            self.say(traceback.format_exc()[-1500:])
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
                ln = self.q.get_nowait()
                ts = time.strftime('%M:%S', time.gmtime(time.time() - self.t0))
                self.txt.insert('end', f'[{ts}] {ln}\n')
                self.txt.see('end')
        except queue.Empty:
            pass
        # 心跳：长时间没有新输出就报一声，让用户知道"还在跑、不是卡死"
        if self.busy and time.time() - self.last_out > 12:
            self.last_out = time.time()
            if self.phase == 'export':
                # 导出阶段没有逐行输出可转发，必须明说还要多久，
                # 否则用户会以为程序卡死（92 页实测约 5 分钟）。
                msg = ' …仍在导出 MusicXML（这一步按页数算，大谱子要几分钟）\n'
            elif self.phase == 'audiveris':
                msg = ' …仍在识别（Audiveris 在算，没有新输出是正常的）\n'
            else:
                msg = ' …仍在运行\n'
            self.txt.insert(
                'end',
                f'[{time.strftime("%M:%S", time.gmtime(time.time() - self.t0))}]'
                + msg)
            self.txt.see('end')
        self.root.after(100, self.drain)


def main():
    _force_utf8_stdio()
    root = tk.Tk()
    try:
        ttk.Style().theme_use('vista')
    except tk.TclError:
        pass
    App(root)
    # 启动时把自己提到最前：不这么做窗口会开在浏览器后面，
    # 用户会以为"点了没反应"。置顶只保持 0.6 秒。
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
