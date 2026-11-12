# -*- coding: utf-8 -*-
"""
视频批量解析 · 图形界面

双击桌面「视频批量解析」即可打开。全程点鼠标，不出现命令行窗口。

功能：
    选择视频文件 / 整个文件夹 / 粘贴 B站链接
    → 一键解析（本地转写 + 关键帧总览图）
    → 完成后打开结果文件夹
"""
import os
import re
import sys
import time
import queue
import threading
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ----------------------------------------------------------------- 配置
# 路径自动定位：本文件可能被复制到 D 盘归档目录，不能写死 C 盘。
_HERE = Path(__file__).resolve().parent          # .../gui
_ROOT = _HERE.parent                             # 版本根目录（含 gui/ 与 scripts/）


def _first_exist(*cands):
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return str(cands[0]) if cands else ""


# 解析引擎（scripts/batch.py）：优先用与本文件同级的 scripts/
BATCH = _first_exist(
    _ROOT / "scripts" / "batch.py",
    r"C:\Users\songyiheng\.workbuddy\skills\video-batch-insight\scripts\batch.py",
)

# 转写/抽帧用的 Python 环境（装了 faster-whisper 的那个）
PY = _first_exist(
    r"C:\Users\songyiheng\.workbuddy\binaries\python\envs\asr\Scripts\python.exe",
    _ROOT / "python" / "Scripts" / "python.exe",
)

# 默认输出目录：优先 D 盘（C 盘很容易被占满，写满会让解析"看起来完成、实际全空"）
def _pick_default_out():
    for d in (r"D:\视频解析结果", r"C:\Users\songyiheng\Downloads\视频解析"):
        try:
            drive = os.path.splitdrive(d)[0] or "C:"
            if os.path.exists(drive + "\\"):
                # 有 D 盘就用 D 盘
                if drive.upper().startswith("D"):
                    return d
        except Exception:
            pass
    return r"C:\Users\songyiheng\Downloads\视频解析"


DEFAULT_OUT = _pick_default_out()


def free_space_gb(path):
    """返回 path 所在磁盘剩余空间（GB）；取不到返回 None。"""
    try:
        p = Path(path).resolve()
        drive = os.path.splitdrive(str(p))[0] or "C:"
        if not drive:
            return None
        import ctypes
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(drive + "\\"), None,
            ctypes.byref(total), ctypes.byref(free))
        return free.value / (1024 ** 3)
    except Exception:
        return None

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

BG = "#f3f4f6"
CARD = "#ffffff"
BORDER = "#e2e5e9"
PRIMARY = "#185fa5"
PRIMARY_HOVER = "#12497f"
TEXT = "#1f2328"
MUTED = "#6b7280"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# 结构化进度行（batch.py 发出来的），由界面吃掉、不进日志区
PROG_RE = re.compile(r"@@P\|[^\n\r]*")

VIDEO_TYPES = ("*.mp4", "*.mkv", "*.mov", "*.avi", "*.flv",
               "*.webm", "*.m4v", "*.wmv", "*.MP4", "*.MOV")


class App:
    def __init__(self, root):
        self.root = root
        self.items = []
        self.msgq = queue.Queue()
        self.running = False
        self.outdir = tk.StringVar(value=DEFAULT_OUT)
        self.link_var = tk.StringVar()
        self.status_var = tk.StringVar(value="准备就绪")
        # 解析内容：两路通道都做成勾选项，「音频转文字」不是一个独立功能
        self.do_asr = tk.BooleanVar(value=True)
        self.do_frames = tk.BooleanVar(value=True)
        # 进度状态
        self.t_start = 0.0
        self.eta_sec = 0.0
        self.cur_i = 0
        self.total_n = 0
        self.prog_var = tk.StringVar(value="")
        self.log_file = None

        self.root.title("视频批量解析")
        self.root.geometry("940x880")
        self.root.minsize(860, 720)
        self.root.configure(bg=BG)

        # 关窗口时的行为要说清楚 —— 否则用户会以为"任务被我关掉了"。
        # 实际上任务早就脱离出去了，关掉界面它照跑，只是界面不再跟踪。
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self.root.after(120, self._drain)

    def _on_close(self):
        """关窗口：正在跑的时候明确问一句，避免误解成"把任务也关了"。"""
        if self.running:
            keep = messagebox.askyesnocancel(
                "关掉界面？",
                "解析还在跑。\n\n"
                "关掉这个窗口，任务【不会停】—— 它已经在后台独立运行了。\n"
                "只是界面不再显示进度。等一下直接打开结果目录看即可。\n\n"
                "  是 —— 关掉界面，任务继续跑\n"
                "  否 —— 不关，继续看着进度\n",
                icon="question")
            if keep is None:          # 点 X / Esc → 什么都不做
                return
            if keep:                  # 是 → 关界面，任务留后台
                self.running = False
                self.root.destroy()
            return
        self.root.destroy()

    # ------------------------------------------------------------- 组件
    def _card(self, parent, expand=True):
        shell = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1)
        shell.pack(fill="both", expand=expand, pady=(0, 12))
        inner = tk.Frame(shell, bg=CARD)
        inner.pack(fill="both", expand=True, padx=16, pady=14)
        return inner

    def _btn(self, parent, text, cmd, kind="normal", big=False):
        font = (FONT, 12, "bold") if big else (FONT, 10)
        if kind == "primary":
            return tk.Button(parent, text=text, command=cmd, font=font,
                             bg=PRIMARY, fg="white",
                             activebackground=PRIMARY_HOVER, activeforeground="white",
                             relief="flat", bd=0, padx=26, pady=10, cursor="hand2")
        if kind == "ghost":
            return tk.Button(parent, text=text, command=cmd, font=font,
                             bg=CARD, fg=MUTED, activebackground="#eef0f3",
                             relief="solid", bd=1, padx=14, pady=7, cursor="hand2")
        return tk.Button(parent, text=text, command=cmd, font=font,
                         bg="#eef2f7", fg=TEXT, activebackground="#dde5ef",
                         relief="flat", bd=0, padx=18, pady=9, cursor="hand2")

    def _build(self):
        pad = 16
        # 主区域放进可滚动画布：窗口一矮，pack 会把最后一块「运行记录」压成 1px，
        # 出错时反而看不到日志。滚动容器保证每一块都按需求高度完整渲染。
        holder = tk.Frame(self.root, bg=BG)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        outer = tk.Frame(self.canvas, bg=BG)
        self._canvas_win = self.canvas.create_window(
            (pad, pad), window=outer, anchor="nw")

        def _sync(_e=None):
            w = max(1, self.canvas.winfo_width() - 2 * pad)
            self.canvas.itemconfig(self._canvas_win, width=w)
            b = self.canvas.bbox("all")
            if b:
                self.canvas.configure(scrollregion=(0, 0, b[2] + pad, b[3] + pad))

        def _wheel(e):
            self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        self.canvas.bind("<Configure>", _sync)
        outer.bind("<Configure>", _sync)
        self.canvas.bind("<Enter>",
                         lambda e: self.canvas.bind_all("<MouseWheel>", _wheel))
        self.canvas.bind("<Leave>",
                         lambda e: self.canvas.unbind_all("<MouseWheel>"))

        # ---- 标题
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="视频批量解析", bg=BG, fg=TEXT,
                 font=(FONT, 18, "bold")).pack(anchor="w")
        tk.Label(head, text="视频  →  逐字稿（音频转文字）+ 关键帧总览图（画面）　文字稿全文附在汇总分析里",
                 bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(3, 14))

        # ---- ① 选内容
        c1 = self._card(outer)
        tk.Label(c1, text="①  要解析什么", bg=CARD, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(anchor="w")

        row = tk.Frame(c1, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        self._btn(row, "选择视频文件", self._add_files).pack(side="left")
        self._btn(row, "选择整个文件夹", self._add_folder).pack(side="left", padx=8)
        self._btn(row, "移除选中", self._remove_sel, "ghost").pack(side="left")
        self._btn(row, "清空", self._clear, "ghost").pack(side="left", padx=8)

        row2 = tk.Frame(c1, bg=CARD)
        row2.pack(fill="x", pady=(12, 0))
        tk.Label(row2, text="或粘贴 B站 链接：", bg=CARD, fg=MUTED,
                 font=(FONT, 10)).pack(side="left")
        ent = tk.Entry(row2, textvariable=self.link_var, font=(FONT, 10),
                       relief="solid", bd=1, highlightthickness=0,
                       bg="#fbfcfd", fg=TEXT, insertbackground=TEXT)
        ent.pack(side="left", fill="x", expand=True, padx=(6, 8), ipady=5)
        ent.bind("<Return>", lambda e: self._add_link())
        self._btn(row2, "添加", self._add_link).pack(side="left")

        # ---- 解析内容（两路通道都在这里选，不另开功能）
        opt_bg = "#f7f9fc"
        opt_box = tk.Frame(c1, bg=opt_bg, highlightbackground=BORDER,
                           highlightthickness=1)
        opt_box.pack(fill="x", pady=(12, 0))
        opt_row = tk.Frame(opt_box, bg=opt_bg)
        opt_row.pack(fill="x", padx=10, pady=(9, 0))
        tk.Label(opt_row, text="解析内容", bg=opt_bg, fg=TEXT,
                 font=(FONT, 10, "bold")).pack(side="left")
        ck = dict(bg=opt_bg, activebackground=opt_bg, fg=TEXT,
                  selectcolor="#ffffff", bd=0, highlightthickness=0,
                  font=(FONT, 10), cursor="hand2")
        tk.Checkbutton(opt_row, text="音频转文字（逐字稿 / 字幕）",
                       variable=self.do_asr, **ck).pack(side="left", padx=(18, 0))
        tk.Checkbutton(opt_row, text="关键帧总览图（画面）",
                       variable=self.do_frames, **ck).pack(side="left", padx=(18, 0))
        tk.Label(opt_box,
                 text="勾选「音频转文字」后，文字稿全文会直接附在 _index.md 汇总分析里，分析和原文在同一份文件",
                 bg=opt_bg, fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=10, pady=(5, 9))

        lb_wrap = tk.Frame(c1, bg=CARD)
        lb_wrap.pack(fill="both", expand=True, pady=(12, 0))
        self.lb = tk.Listbox(lb_wrap, font=(FONT, 10), height=7,
                             selectmode="extended", activestyle="none",
                             relief="solid", bd=1, highlightthickness=0,
                             bg="#fbfcfd", fg=TEXT, selectbackground="#cfe0f3",
                             selectforeground=TEXT)
        sb = tk.Scrollbar(lb_wrap, command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ---- ② 结果放到哪里
        c2 = self._card(outer, expand=False)
        head2 = tk.Frame(c2, bg=CARD)
        head2.pack(fill="x")
        tk.Label(head2, text="②  结果放到哪里", bg=CARD, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(side="left")
        self.space_var = tk.StringVar(value="")
        self.lbl_space = tk.Label(head2, textvariable=self.space_var, bg=CARD,
                                  fg=MUTED, font=(FONT, 9))
        self.lbl_space.pack(side="right")
        row3 = tk.Frame(c2, bg=CARD)
        row3.pack(fill="x", pady=(10, 0))
        tk.Entry(row3, textvariable=self.outdir, font=(FONT, 10),
                 relief="solid", bd=1, highlightthickness=0,
                 bg="#fbfcfd", fg=TEXT, insertbackground=TEXT
                 ).pack(side="left", fill="x", expand=True, ipady=5)
        self._btn(row3, "更改", self._pick_out).pack(side="left", padx=(8, 0))
        self._btn(row3, "打开", self._open_dir, "ghost").pack(side="left", padx=(6, 0))
        self._btn(row3, "复制路径", self._copy_out, "ghost").pack(side="left", padx=(6, 0))
        self._refresh_space()

        # ---- ③ 开始 + 进度 + 日志
        c3 = self._card(outer)
        row4 = tk.Frame(c3, bg=CARD)
        row4.pack(fill="x")
        self.btn_start = self._btn(row4, "开始解析", self._start, "primary", big=True)
        self.btn_start.pack(side="left")
        self.btn_open = self._btn(row4, "打开结果文件夹", self._open_dir, "ghost")
        self.btn_open.pack(side="left", padx=10)
        tk.Label(row4, textvariable=self.status_var, bg=CARD, fg=MUTED,
                 font=(FONT, 10)).pack(side="left", padx=8)

        # 进度条 + 进度文字（跑起来后这里告诉你在第几条、还要多久）
        pbar_row = tk.Frame(c3, bg=CARD)
        pbar_row.pack(fill="x", pady=(12, 0))
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Ins.Horizontal.TProgressbar",
                        troughcolor="#e9edf2", background=PRIMARY,
                        bordercolor="#e9edf2", lightcolor=PRIMARY,
                        darkcolor=PRIMARY, thickness=16)
        self.pbar = ttk.Progressbar(pbar_row, style="Ins.Horizontal.TProgressbar",
                                    mode="determinate", maximum=100, value=0)
        self.pbar.pack(fill="x")
        tk.Label(c3, textvariable=self.prog_var, bg=CARD, fg=PRIMARY,
                 font=(FONT, 10, "bold"), anchor="w").pack(fill="x", pady=(5, 0))

        # 跑完后的"结果在哪"提示条（平时藏起来，完成后才出现，看得见、可点、可复制）
        self.result_bar = tk.Frame(c3, bg="#eaf7ef", highlightbackground="#1a7f47",
                                   highlightthickness=1)
        self.result_lbl = tk.Label(self.result_bar, text="", bg="#eaf7ef",
                                   fg="#12603a", font=(FONT, 10),
                                   justify="left", anchor="w")
        self.result_lbl.pack(side="left", fill="x", expand=True, padx=12, pady=10)
        rb_btns = tk.Frame(self.result_bar, bg="#eaf7ef")
        rb_btns.pack(side="right", padx=(0, 10))
        tk.Button(rb_btns, text="打开文件夹", command=self._open_dir, font=(FONT, 10, "bold"),
                  bg="#1a7f47", fg="white", activebackground="#12603a",
                  activeforeground="white", relief="flat", bd=0,
                  padx=14, pady=6, cursor="hand2").pack(side="left")
        tk.Button(rb_btns, text="复制路径", command=self._copy_out, font=(FONT, 10),
                  bg="#eaf7ef", fg="#12603a", activebackground="#d6efe0",
                  relief="solid", bd=1, padx=12, pady=6,
                  cursor="hand2").pack(side="left", padx=(6, 0))

        tk.Label(c3, text="运行记录（第一次跑会慢一点，要加载模型；之后很快）",
                 bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(10, 0))
        self.log = tk.Text(c3, height=11, font=(MONO, 9), wrap="word",
                           bg="#fbfcfd", fg="#3c4149", relief="solid", bd=1,
                           highlightthickness=0, padx=10, pady=8, state="disabled")
        self.log.pack(fill="both", expand=True, pady=(6, 0))

    # ------------------------------------------------------------- 结果路径相关
    def _refresh_space(self):
        """在输出目录旁边常显"还剩多少空间"，让磁盘问题一眼可见。"""
        gb = free_space_gb(self.outdir.get().strip() or "C:\\")
        if gb is None:
            self.space_var.set("")
            self.lbl_space.configure(fg=MUTED)
        elif gb < 0.5:
            self.space_var.set("⚠ 仅剩 %.1f GB，不够用" % gb)
            self.lbl_space.configure(fg="#b02a37")
        elif gb < 5:
            self.space_var.set("⚠ 仅剩 %.1f GB，偏紧" % gb)
            self.lbl_space.configure(fg="#9a6700")
        else:
            self.space_var.set("剩余空间 %.0f GB" % gb)
            self.lbl_space.configure(fg="#1a7f47")

    def _copy_out(self):
        d = self.outdir.get().strip()
        if not d:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(d)
            self.status_var.set("路径已复制，可直接粘贴到资源管理器")
        except Exception as e:
            messagebox.showerror("复制失败", str(e))

    def _show_result_bar(self, text):
        """完成后把结果条显示出来 —— 关掉窗口前一定看得见东西在哪。"""
        self.result_lbl.configure(text=text)
        self.result_bar.pack(fill="x", pady=(10, 0), before=self.log)
        self.root.update_idletasks()

    def _hide_result_bar(self):
        try:
            self.result_bar.pack_forget()
        except Exception:
            pass

    # ------------------------------------------------------------- 选内容
    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择视频文件（可多选）",
            filetypes=[("视频文件", " ".join(VIDEO_TYPES)), ("所有文件", "*.*")])
        if paths:
            self._add_items(list(paths))

    def _add_folder(self):
        d = filedialog.askdirectory(title="选择文件夹（里面所有视频都会被处理）")
        if d:
            self._add_items([d])

    def _add_link(self):
        v = self.link_var.get().strip()
        if not v:
            return
        self._add_items([v])
        self.link_var.set("")

    def _add_items(self, paths):
        for p in paths:
            if p and p not in self.items:
                self.items.append(p)
        self._refresh()

    def _remove_sel(self):
        for i in reversed(list(self.lb.curselection())):
            del self.items[i]
        self._refresh()

    def _clear(self):
        self.items = []
        self._refresh()

    def _refresh(self):
        self.lb.delete(0, "end")
        for p in self.items:
            name = os.path.basename(p.rstrip("\\/")) or p
            if p.startswith("http"):
                self.lb.insert("end", "  [链接]  " + p)
            elif os.path.isdir(p):
                self.lb.insert("end", "  [文件夹]  " + name)
            else:
                self.lb.insert("end", "  " + name)
        self.status_var.set("已选 %d 项" % len(self.items) if self.items else "准备就绪")

    def _pick_out(self):
        d = filedialog.askdirectory(title="选择结果保存位置")
        if d:
            self.outdir.set(d)
            self._refresh_space()

    # ------------------------------------------------------------- 运行
    def _run_args(self):
        """按「解析内容」勾选项拼参数：两路通道都在这一个流程里。"""
        a = []
        if self.do_asr.get():
            # 勾了音频转文字 → 文字稿全文附进 _index.md 汇总分析
            a.append("--attach-transcript")
        else:
            a.append("--skip-asr")
        if not self.do_frames.get():
            a.append("--skip-frames")
        return a

    def _count_files(self):
        """先清点要处理多少个文件，避免误选文件夹白跑几小时。返回 -1 表示失败。"""
        cmd = ([PY, "-u", BATCH] + self.items
               + ["--out", self.outdir.get().strip(), "--list-only"]
               + self._run_args())
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            p = subprocess.run(cmd, capture_output=True, env=env,
                               creationflags=CREATE_NO_WINDOW, timeout=600)
        except Exception:
            return -1
        out = (p.stdout or b"").decode("utf-8", "replace")
        m = re.search(r"共\s*(\d+)\s*个文件", out)
        return int(m.group(1)) if m else -1

    def _start(self):
        if self.running:
            return
        if not self.do_asr.get() and not self.do_frames.get():
            messagebox.showwarning(
                "没选解析内容",
                "「音频转文字」和「关键帧总览图」至少勾一项。\n\n"
                "只想要文字稿：只勾第一项；只想要画面：只勾第二项。")
            return
        if not self.items:
            messagebox.showwarning("还没选内容",
                                   "请先「选择视频文件」或「选择整个文件夹」，\n"
                                   "也可以粘贴一个 B站链接。")
            return
        outdir = self.outdir.get().strip()
        if not outdir:
            messagebox.showwarning("缺少保存位置", "请先选择结果保存到哪里。")
            return
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("目录打不开", "无法创建结果目录：\n%s" % e)
            return

        # ★ 磁盘空间预检：C 盘写满会让解析"看起来完成、实际全是空文件夹"
        gb = free_space_gb(outdir)
        if gb is not None:
            if gb < 0.5:
                messagebox.showerror(
                    "磁盘空间不够",
                    "所选位置所在的盘只剩 %.1f GB 可用空间，放不下解析结果。\n\n"
                    "请先清理磁盘，或把结果保存到别的盘（比如 D 盘）。\n\n"
                    "位置：%s" % (gb, outdir))
                return
            if gb < 5:
                if not messagebox.askyesno(
                        "磁盘空间偏紧",
                        "所选位置所在的盘只剩 %.1f GB。\n\n"
                        "解析一个长视频会产生几百 MB 到几 GB 的文件，\n"
                        "空间写满会导致解析失败（结果目录会是空的）。\n\n"
                        "仍然继续吗？建议先换到空间充足的盘。" % gb):
                    return

        # 先清点，让用户确认范围（选错文件夹能在这里拦下来）
        self.status_var.set("正在清点文件…")
        self.prog_var.set("")
        self.root.update_idletasks()
        n = self._count_files()
        self.status_var.set("准备就绪")
        if n == 0:
            messagebox.showwarning(
                "没有可处理的视频",
                "这些位置里没找到能解析的视频。\n\n"
                "（已自动跳过 node_modules、.git 等代码目录）")
            return
        if n > 0:
            what = []
            if self.do_asr.get():
                what.append("音频转文字")
            if self.do_frames.get():
                what.append("关键帧总览图")
            mins = n * (1.5 if self.do_asr.get() else 0.8)
            if not messagebox.askyesno(
                    "确认开始",
                    "一共有 %d 个文件要处理。\n"
                    "解析内容：%s\n\n"
                    "按经验每个 1~3 分钟，估计总共要跑 %.0f 分钟以上。\n"
                    "开始后会显示进度和剩余时间。\n\n现在开始吗？"
                    % (n, " ＋ ".join(what), mins)):
                return
        else:
            if not messagebox.askyesno("确认", "没能清点出文件数量。仍然开始吗？"):
                return

        # 记下本次真正跑的是哪几路，收尾时按它判定产出，避免用户中途改勾选
        self._ran_asr = self.do_asr.get()
        self._ran_frames = self.do_frames.get()
        self.running = True
        self.btn_start.configure(state="disabled", text="正在解析...")
        self.status_var.set("运行中，请不要关闭窗口")
        self._hide_result_bar()
        self._log_clear()
        self._append("开始处理 %d 项\n\n" % len(self.items))
        self._append("结果会放到：%s\n\n" % outdir)

        # 重置进度
        self.t_start = time.time()
        self.eta_sec = 0.0
        self.cur_i = 0
        self.total_n = n if n > 0 else 0
        self.pbar.configure(maximum=max(1, self.total_n or 1), value=0)
        self.prog_var.set("正在加载模型…")
        self._tick()

        cmd = [PY, "-u", BATCH] + self.items + ["--out", outdir] + self._run_args()
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()

    # ------------------------------------------------------------- 进度
    def _fmt(self, sec):
        sec = int(max(0, sec))
        if sec < 60:
            return "%d 秒" % sec
        if sec < 3600:
            return "%d 分 %02d 秒" % (sec // 60, sec % 60)
        return "%d 小时 %02d 分" % (sec // 3600, (sec % 3600) // 60)

    def _handle_progress(self, line):
        """解析 batch.py 的 @@P|phase|i|n|name|extra 进度行。"""
        p = line.split("|")
        if len(p) < 5:
            return
        phase = p[1]
        try:
            i, n = int(p[2]), int(p[3])
        except ValueError:
            return
        name, extra = p[4], (p[5] if len(p) > 5 else "")
        if phase == "begin":
            self.total_n = n
            self.pbar.configure(maximum=max(1, n), value=0)
            self.prog_var.set("共 %d 个文件，开始…" % n)
        elif phase == "item":
            self.cur_i, self.total_n = i, n
            self.pbar.configure(value=i - 1)
            self.status_var.set("正在处理第 %d/%d 个" % (i, n))
            if i > 1:
                el = time.time() - self.t_start
                self.eta_sec = (el / (i - 1)) * (n - i + 1)
        elif phase == "stage":
            self.pbar.configure(value=min(i, n))
            self.eta_sec = max(0.0, self.eta_sec - 10)
        elif phase == "end":
            self.pbar.configure(value=max(1, n), maximum=max(1, n))
            self.eta_sec = 0.0

    def _tick(self):
        if not self.running:
            return
        parts = []
        if self.cur_i and self.total_n:
            parts.append("第 %d/%d 个" % (self.cur_i, self.total_n))
        if self.t_start:
            parts.append("已用 %s" % self._fmt(time.time() - self.t_start))
        if self.eta_sec > 5:
            parts.append("预计还需 %s" % self._fmt(self.eta_sec))
        self.prog_var.set(" · ".join(parts) if parts else "正在准备…")
        self.root.after(500, self._tick)

    def _launch_detached(self, cmd, log_path):
        """经由 run_detached.py 中转启动，真正脱离本进程的作业对象。

        直接用 Popen(creationflags=DETACHED_PROCESS) 是不够的：Windows 会把
        子进程并入父进程的 Job Object，父进程一退，子进程跟着被回收（实测
        日志 0 字节、进程消失）。走一层中转后，Python 加载器会显式
        CREATE_BREAKAWAY_FROM_JOB，解析进程就真的独立了。
        """
        runner = str(Path(BATCH).parent / "run_detached.py")
        if not Path(runner).exists():
            return None
        try:
            return subprocess.Popen(
                [PY, runner, log_path] + list(cmd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        except Exception:
            return None

    def _run(self, cmd):
        """把解析任务作为「独立进程」启动，输出写进日志文件。

        只要日志文件存在，就不去管那个中转进程 —— 因为解析进程已经脱离。
        这样即使界面窗口被关掉、界面进程被杀，解析依然会跑完。
        """
        log_path = Path(self.outdir.get().strip()) / "_运行日志.txt"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "w", encoding="utf-8", buffering=1) as f:
                f.write("启动命令：%s\n\n" % " ".join(cmd))
        except Exception as e:
            self.msgq.put(("log", "\n无法打开日志文件：%s\n" % e))
            self.msgq.put(("done", -1))
            return
        self.log_file = log_path

        self._launch_detached(cmd, str(log_path))

        # tail 日志文件——界面只负责"看"，跟子进程没有句柄绑定
        pos = 0
        idle = 0
        IDLE_LIMIT = 750          # 750 × 0.4s ≈ 5 分钟无输出才算异常
                                  # （原值是 75≈30 秒，转一个 3 小时的大课期间
                                  #   正常也会有几十秒不出进度行，30 秒就误判"已完成"）
        while True:
            text = ""
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    text = f.read()
                    pos = f.tell()
            except FileNotFoundError:
                pass
            if text:
                self.msgq.put(("log", text))
                idle = 0
            else:
                idle += 1
                # 心跳：告诉界面"我还在等"，避免用户以为卡死
                if idle and idle % 25 == 0:
                    self.msgq.put(("log", "\r· 等待中…（%d 秒无新输出，进程仍在运行）"
                                        % int(idle * 0.4)))

            # 看日志里有没有 #END#（解析进程真的收尾了）
            try:
                tail = Path(log_path).read_text(encoding="utf-8", errors="replace")
                done = "#END#" in tail
            except Exception:
                done = False
            if done:
                break
            if idle > IDLE_LIMIT:
                self.msgq.put(("log", "\n\n⚠ 已超过 5 分钟没有任何输出，判定为异常停止。\n"
                                      "日志文件：\n%s\n" % log_path))
                break
            time.sleep(0.4)

        # 从日志里读退出码。注意：拿到 #END# 才算真完成；
        # 没拿到就按 kUnknown 处理，绝不默认"成功"（原来默认 0 会谎报完成）。
        code = None
        try:
            txt = Path(log_path).read_text(encoding="utf-8", errors="replace")
            m = re.search(r"#EXIT#(-?\d+)", txt)
            if m:
                code = int(m.group(1))
            elif "#END#" not in txt:
                code = -998          # 异常中止（被 5 分钟超时打断 / 进程没了）
        except Exception:
            code = -997
        if code is None:
            self.msgq.put(("done", 0))          # 有 #END# 无 #EXIT# → 正常结束
        else:
            self.msgq.put(("done", code))

    def _drain(self):
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "log":
                    self._append(payload)
                else:
                    self._finish(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    # ------------------------------------------------------------- 日志
    def _log_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _append(self, text):
        """写入日志；把 \\r 处理成"覆盖当前行"，让进度条不会刷屏。"""
        if "@@P|" in text:
            for m in PROG_RE.finditer(text):
                self._handle_progress(m.group(0))
            text = PROG_RE.sub("", text)
        if not text:
            return
        self.log.configure(state="normal")
        buf = ""
        for ch in text:
            if ch == "\r":
                self.log.insert("end", buf)
                buf = ""
                self.log.delete("end-1l linestart", "end-1c")
            elif ch == "\n":
                self.log.insert("end", buf + "\n")
                buf = ""
            else:
                buf += ch
        if buf:
            self.log.insert("end", buf)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------- 收尾
    def _finish(self, code):
        self.running = False
        self.eta_sec = 0.0
        self.btn_start.configure(state="normal", text="开始解析")
        outdir = self.outdir.get()
        elapsed = self._fmt(time.time() - self.t_start)

        # 不轻信退出码：实际数一下产出（逐字稿 / 总览图），有实据才敢说"完成"
        n_md = n_jpg = 0
        try:
            for root, dirs, files in os.walk(outdir):
                for f in files:
                    if f == "transcript.md":
                        n_md += 1
                    elif f.startswith("overview_") and f.endswith(".jpg"):
                        n_jpg += 1
        except Exception:
            pass

        ran_asr = getattr(self, "_ran_asr", True)
        ran_frames = getattr(self, "_ran_frames", True)
        if ran_asr and ran_frames:
            summary = "逐字稿 %d 份、总览图 %d 张（文字稿全文已附在 _index.md）" % (n_md, n_jpg)
        elif ran_asr:
            summary = "逐字稿 %d 份（文字稿全文已附在 _index.md）" % n_md
        else:
            summary = "总览图 %d 张" % n_jpg
        # 只按本次勾选的那几路判定有没有产出
        produced = (ran_asr and n_md > 0) or (ran_frames and n_jpg > 0)

        # 查日志里有没有"磁盘写满"的痕迹 —— 这是最常见的"跑了但没产出"元凶
        disk_full = False
        try:
            txt = Path(self.log_file).read_text(encoding="utf-8", errors="replace")
            if "No space left on device" in txt or "Errno 28" in txt:
                disk_full = True
        except Exception:
            pass

        if disk_full:
            self.status_var.set("磁盘空间满了")
            self.prog_var.set("磁盘写满，结果没写出来")
            self._append("\n\n⚠ 磁盘空间满了！\n"
                         "解析过程中写着写着没空间了，所以结果文件没生成。\n"
                         "清一下磁盘，或把结果保存到别的盘，然后重跑。\n")
            self._show_result_bar("⚠ 磁盘写满，没有产出。结果目录：%s" % outdir)
            messagebox.showerror(
                "磁盘空间满了",
                "解析跑到一半，磁盘被写满了，所以结果没写出来。\n\n"
                "这个不是程序的问题，是空间不够。请：\n"
                "① 清理一下磁盘（或把结果位置换到 D 盘）\n"
                "② 再重新解析一次\n\n"
                "结果目录：%s" % outdir)
            return

        if code == 0:
            self.status_var.set("解析完成")
            self.pbar.configure(value=self.pbar["maximum"])
            self.prog_var.set("全部完成 · 总共用时 %s" % elapsed)
            self._append("\n\n完成。用时 %s。\n"
                         "实际产出：%s\n"
                         "结果目录：%s\n" % (elapsed, summary, outdir))
            if not produced:
                # 退出码说成功，但目录里什么都没有 —— 绝不能报"完成"
                self.status_var.set("⚠ 完成但没产出")
                self._append("\n⚠ 上面说已完成，但结果目录里没找到对应产出。\n"
                             "可能是选错了目录，或源文件里没有音轨 / 画面。\n")
                self._show_result_bar("⚠ 没找到产出。结果目录：%s" % outdir)
                messagebox.showwarning(
                    "好像没产出",
                    "程序报告已结束，但结果目录里没有任何产物：\n%s\n\n"
                    "请确认：① 要解析的文件夹选对了吗？② 里面有视频文件吗？\n"
                     "详细过程看窗口下方的运行记录。" % outdir)
            else:
                self._show_result_bar("✓ 解析完成 · 用时 %s\n%s\n在：%s"
                                      % (elapsed, summary, outdir))
                if messagebox.askyesno("解析完成",
                                       "已经处理好了，东西都在下面这个文件夹：\n\n"
                                       "%s\n\n%s。\n\n要现在打开吗？"
                                       % (outdir, summary)):
                    self._open_dir()
        elif code == -998:
            # 5 分钟无输出被我们主动打断 —— 这种情况解析进程可能还在跑
            self.status_var.set("界面已脱离（任务可能还在跑）")
            self.prog_var.set("界面停止跟踪，任务可能仍在后台运行")
            self._append("\n\n⚠ 界面已停止跟踪（超过 5 分钟没收到新输出）。\n"
                         "但解析进程可能还在后台跑 —— 关掉这个窗口也不影响它。\n"
                         "过一会儿直接打开结果目录看有没有新文件即可：\n%s\n" % outdir)
            self._show_result_bar(
                "任务可能还在后台跑。稍后打开这个目录看：\n%s" % outdir)
        else:
            self.status_var.set("出错了")
            self.prog_var.set("中途出错，看下面的运行记录")
            self._append("\n\n出错了（代码 %s）。把上面的内容发给我，我来看。\n" % code)
            self._show_result_bar("出错中断。已产生的文件仍在这个目录：\n%s" % outdir)
            messagebox.showerror("出错了",
                                 "解析过程中出错（代码 %s）。\n\n"
                                 "窗口下方的运行记录里有详细信息。" % code)

    def _open_dir(self):
        d = self.outdir.get().strip()
        if not d:
            return
        if not os.path.isdir(d):
            # 目录还不存在（没跑过）就先建出来，别让用户以为出错了
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as e:
                messagebox.showerror("打不开", "目录不存在，也建不出来：\n%s\n\n%s" % (d, e))
                return
        try:
            os.startfile(d)
        except Exception as e:
            messagebox.showerror("打不开", str(e))


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    try:
        dpi = root.winfo_fpixels("1i")
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass

    # 双击启动后强行把窗口拉到最前面。
    # 为什么不能只靠 focus_force：Windows 有 SetForegroundWindow 限制，
    # 非前台进程直接抢会失败。用 AttachThreadInput 把本线程接到当前前台线程上再抢，
    # 这是唯一稳的做法 —— 实测有效（之前窗口被浏览器/编辑器压在下面，用户以为没打开）。
    def _front():
        try:
            root.deiconify()
            root.lift()
            # 置顶 2 秒（原来是 0.7 秒）—— 时间太短时用户切回来就已经消失了
            root.attributes("-topmost", True)
            root.after(2000, lambda: root.attributes("-topmost", False))
            root.focus_force()
            root.after(120, _force_foreground)
            root.after(400, _flash)
        except Exception:
            pass

    def _force_foreground():
        try:
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            root.update_idletasks()
            hwnd = u32.GetParent(root.winfo_id()) or root.winfo_id()
            if u32.IsIconic(hwnd):
                u32.ShowWindow(hwnd, 9)          # SW_RESTORE
            fg = u32.GetForegroundWindow()
            t_fg = u32.GetWindowThreadProcessId(fg, None)
            t_me = k32.GetCurrentThreadId()
            u32.AttachThreadInput(t_fg, t_me, True)
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
            u32.SetFocus(hwnd)
            u32.AttachThreadInput(t_fg, t_me, False)
        except Exception:
            pass

    def _flash():
        # 任务栏图标闪烁 + 窗口标题闪烁，这是 Windows 官方的"叫你来看"机制，
        # 不受 SetForegroundWindow 的前台限制。抢不到前台时靠它把用户叫回来。
        try:
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT),
                            ("hwnd", wintypes.HWND),
                            ("dwFlags", wintypes.DWORD),
                            ("uCount", wintypes.UINT),
                            ("dwTimeout", wintypes.DWORD)]

            root.update_idletasks()
            hwnd = u32.GetParent(root.winfo_id()) or root.winfo_id()
            fwi = FLASHWINFO()
            fwi.cbSize = ctypes.sizeof(FLASHWINFO)
            fwi.hwnd = hwnd
            # 3 = FLASHW_ALL(标题栏+任务栏)，12 = 闪 12 次
            fwi.dwFlags = 0x00000003
            fwi.uCount = 12
            fwi.dwTimeout = 0
            u32.FlashWindowEx(ctypes.byref(fwi))
        except Exception:
            pass

    root.after(80, _front)

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
