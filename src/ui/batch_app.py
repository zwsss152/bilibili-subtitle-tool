"""Automatic queue and source-preserving transcript reader."""
import datetime
import logging
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, scrolledtext
import urllib.parse
import webbrowser
from . import kit as ui
from ..config import load_config, save_config
from ..paths import OUTPUT_ROOT, APP_ICON, resolve_owned
from ..services.scheduler import Scheduler
from ..core.textout import read_document, sanitize_filename
from ..core.bilibili import fmt_ts
from ..core.topics import create_topic


class BatchApp(tk.Tk):
    def __init__(self, autostart=True, demo_scale=None):
        ui.enable_dpi_awareness()
        super().__init__()
        if demo_scale:
            self.tk.call("tk", "scaling", 96 / 72 * demo_scale)
        self.cfg = load_config()
        self.title("文字稿 · B 站、抖音与机核")
        self.geometry(self.cfg.get("geometry") or "1180x820")
        line_height = tkfont.Font(font=ui.ui_font(self, 10)).metrics('linespace')
        self.minsize(960, max(640, line_height * 29))
        self.configure(bg=ui.T.bg)
        if APP_ICON.exists():
            self.iconbitmap(str(APP_ICON))
        self.document, self.documents, self.row_times = None, [], {}
        self.pending = queue.Queue()
        self.model_cancel = threading.Event()
        self.closing = False
        self._style()
        self._build()
        self.scheduler = Scheduler(cookie=self.cookie_var.get(), topic=self.topic_var.get(), autostart=autostart)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Control-Return>", lambda e: self.enqueue())
        self.refresh_library()
        self.after(100, self._refresh)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", padding=4, arrowsize=14, bordercolor=ui.T.border_input,
                        lightcolor=ui.T.border_input, darkcolor=ui.T.border_input, fieldbackground=ui.T.surface)
        style.configure("Queue.Treeview", background=ui.T.surface, fieldbackground=ui.T.surface,
                        foreground=ui.T.text, rowheight=max(34, tkfont.Font(font=ui.ui_font(self, 10)).metrics('linespace')+12), font=ui.ui_font(self, 10), borderwidth=0)
        style.configure("Queue.Treeview.Heading", background=ui.T.surface_alt, foreground=ui.T.text2,
                        font=ui.ui_font(self, 9, "bold"), padding=(8, 9), relief="flat")
        style.map("Queue.Treeview", background=[("selected", ui.T.select)], foreground=[("selected", ui.T.text)])
        style.configure("TPanedwindow", background=ui.T.bg)

    def label(self, parent, text="", size=10, bold=False, color=None, **kw):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color or ui.T.text,
                        font=ui.ui_font(self, size, "bold" if bold else "normal"), **kw)

    def button(self, parent, text, command, variant="secondary"):
        return ui.RoundedButton(parent, text=text, command=command, variant=variant,
                                bg=parent.cget("bg"), padx=12)

    def row(self, parent):
        frame = tk.Frame(parent, bg=parent.cget("bg"))
        frame.pack(fill="x", pady=(0, 10))
        return frame

    def _build(self):
        self.status_var = tk.StringVar(value="粘贴 B 站、抖音或机核链接，即可自动生成文字稿")
        status = tk.Frame(self, bg="#EFEFF3", pady=7)
        status.pack(side="bottom", fill="x")
        self.label(status, size=9, color=ui.T.text2, textvariable=self.status_var, anchor="w").pack(side="left", padx=18, fill="x", expand=True)
        self.count_var = tk.StringVar()
        self.label(status, size=9, color=ui.T.text2, textvariable=self.count_var).pack(side="right", padx=18)
        header = tk.Frame(self, bg=ui.T.bg)
        header.pack(fill="x", padx=22, pady=(16, 12))
        brand = self.label(header, "文字稿", 20, True)
        brand.pack(side="left")
        description = self.label(header, "B 站 · 抖音 · 机核  /  本地转写与阅读", color=ui.T.text2)
        description.pack(side="left", padx=16)
        def header_layout(event):
            if event.width < 1050:
                description.pack_forget()
            elif not description.winfo_manager():
                description.pack(side='left', padx=16, after=brand)
        header.bind('<Configure>', header_layout)
        self.button(header, "设置", self.settings).pack(side="right")
        self.button(header, "运行日志", self.toggle_logs).pack(side="right", padx=8)
        ui.Pill(header, text="自动识别", bg=ui.T.bg).pack(side="right", padx=8)

        new = ui.Card(self, pad=16, border_color=ui.T.border_card)
        new.pack(fill="x", padx=18, pady=(0, 12))
        body = new.body
        body.columnconfigure(1, weight=1)
        self.label(body, "链接", color=ui.T.text2).grid(row=0, column=0, padx=(0, 12), sticky="w")
        self.link_var = tk.StringVar()
        self.link_field = ui.TextField(body, textvariable=self.link_var, width=400)
        self.link_field.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self.link_field.entry.bind("<Return>", lambda e: self.enqueue())
        self.button(body, "加入队列", self.enqueue, "primary").grid(row=0, column=2, sticky="e")
        second = tk.Frame(body, bg=ui.T.surface)
        second.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.label(second, "归档主题", 9, color=ui.T.text2).pack(side="left", padx=(0, 12))
        self.topic_var = tk.StringVar(value=self.cfg.get("topic") or "未分类")
        self.topic_box = ttk.Combobox(second, textvariable=self.topic_var, width=20)
        self.topic_box.pack(side="left")
        self.button(second, "创建主题", self.create_topic).pack(side="left", padx=(8, 0))
        self.topic_box.configure(postcommand=self.refresh_topics)
        self.topic_box.bind("<Return>", lambda e: self.create_topic())
        self.topic_box.bind("<<ComboboxSelected>>", lambda e: self._save_settings())
        self.topic_box.bind("<FocusOut>", lambda e: self._save_settings())
        self.label(second, "入队后主题固定，可一次粘贴多条链接", 9, color=ui.T.text2).pack(side="left", padx=16)
        self.cookie_var = tk.StringVar(value=self.cfg.get("cookie") or "")

        self.log_frame = tk.Frame(self, bg=ui.T.surface, highlightbackground=ui.T.border_strong, highlightthickness=1)
        self.log_text = scrolledtext.ScrolledText(self.log_frame, height=4, relief="flat", bd=0,
                                                 font=("Consolas", 9), bg=ui.T.surface, fg=ui.T.text2)
        self.log_text.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_text.configure(state="disabled")
        self.logs_visible = bool(self.cfg.get("show_logs"))
        if self.logs_visible:
            self.log_frame.pack(side="bottom", fill="x", padx=18, pady=(0, 10))

        self.workspace = ttk.Panedwindow(self, orient="horizontal")
        self.workspace.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        left = tk.Frame(self.workspace, bg=ui.T.bg)
        right = tk.Frame(self.workspace, bg=ui.T.bg)
        self.workspace.add(left, weight=2)
        self.workspace.add(right, weight=3)
        def clamp_panes(event=None):
            width = self.workspace.winfo_width()
            if width > 780:
                position = self.workspace.sashpos(0)
                self.workspace.sashpos(0, max(310, min(position, width-430)))
        self.workspace.bind('<ButtonRelease-1>', clamp_panes)
        queue_card = ui.Card(left, pad=14, expand=True, border_color=ui.T.border_card)
        queue_card.pack(fill="both", expand=True, padx=(0, 5))
        q = queue_card.body
        self.label(q, "处理队列", 11, True).pack(anchor="w", pady=(0, 10))
        tools = self.row(q)
        self.pause_button = self.button(tools, "暂停", self.toggle_pause)
        self.pause_button.pack(side="left")
        retry = self.button(tools, "重试", self.retry)
        delete = self.button(tools, "删除", self.remove, "danger")
        self._responsive_actions(tools, [self.pause_button, retry, delete])
        frame = tk.Frame(q, bg=ui.T.surface)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("status", "title", "progress"), show="headings", style="Queue.Treeview", selectmode="extended")
        for key, label, width in (("status", "状态", 86), ("title", "节目 / 视频", 220), ("progress", "进度", 60)):
            if key != 'title':
                width = max(width, tkfont.Font(font=ui.ui_font(self, 10)).measure('等待识别' if key == 'status' else '100%')+20)
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=45, stretch=key == "title", anchor="w" if key == "title" else "center")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.select_task)
        for name, color in (("done", "#248A3D"), ("fail", "#D70015"), ("active", ui.T.accent)):
            self.tree.tag_configure(name, foreground=color)
        self.task_detail = tk.StringVar(value="完成后选中任务，即可阅读文字稿")
        detail = self.label(q, size=9, anchor="w", justify="left", color=ui.T.text2, textvariable=self.task_detail, wraplength=340, height=2)
        detail.pack(fill="x", pady=10)
        q.bind("<Configure>", lambda e: detail.configure(wraplength=max(100, e.width - 20)))
        detail.pack_configure(side='bottom', before=frame)

        card = ui.Card(right, pad=16, expand=True, border_color=ui.T.border_card)
        card.pack(fill="both", expand=True, padx=(5, 0))
        r = card.body
        top = self.row(r)
        self.label(top, "稿件阅读", 11, True).pack(side="left")
        self.document_var = tk.StringVar()
        self.document_box = ttk.Combobox(r, textvariable=self.document_var, state="readonly")
        self.document_box.configure(postcommand=self.refresh_library)
        self.document_box.pack(fill="x", pady=(0, 10))
        self.document_box.bind("<<ComboboxSelected>>", self.select_document)
        self.document_title = tk.StringVar(value="选择已有稿件，或等待任务完成")
        title = self.label(r, size=11, bold=True, anchor="w", justify="left", textvariable=self.document_title, wraplength=440)
        title.pack(fill="x", pady=(0, 8))
        r.bind("<Configure>", lambda e: title.configure(wraplength=max(100, e.width - 20)))
        view = self.row(r)
        self.mode_var = tk.StringVar(value="带时间戳")
        self.mode_box = ttk.Combobox(view, textvariable=self.mode_var, values=("带时间戳", "通读", "原始记录"), state="readonly", width=12)
        self.mode_box.pack(side="left")
        self.mode_box.bind("<<ComboboxSelected>>", lambda e: self.render())
        self._responsive_actions(view, [self.mode_box,
                                       self.button(view, '打开文件夹', self.open_output),
                                       self.button(view, "打开来源", self.open_source)])
        self.reader = scrolledtext.ScrolledText(r, wrap="word", relief="flat", bd=0,
                                               font=ui.ui_font(self, 11), bg=ui.T.surface, fg=ui.T.text,
                                               padx=5, pady=5, spacing1=4, spacing3=10, selectbackground="#C7E0FF")
        self.reader.pack(fill="both", expand=True)
        self.reader.tag_configure("timestamp", foreground=ui.T.accent)
        self.reader.insert("1.0", "在左侧加入 B 站、抖音或机核链接。\n\n已有文字稿可以从上方列表直接打开。\n\n主题文件夹只输出 TXT，识别记录由程序内部保存。")
        self.reader.configure(state="disabled")
        actions = self.row(r)
        # Reserve the bottom controls before the expanding text viewport.
        actions.pack_configure(side="bottom", before=self.reader, pady=(12, 0))
        self.button(actions, "复制选段", self.copy_selection).pack(side="left")
        self.button(actions, "复制全文", self.copy_all, "primary").pack(side="right")
        self.after(150, lambda: self.workspace.sashpos(0, max(370, int(self.winfo_width() * 0.39))))

    def _responsive_actions(self, frame, buttons):
        for button in buttons:
            button.pack_forget()
        layout = [None]
        def arrange(event=None):
            narrow = frame.winfo_width() < sum(b.winfo_reqwidth() for b in buttons) + 16
            if narrow == layout[0]:
                return
            layout[0] = narrow
            for i in range(len(buttons)):
                frame.columnconfigure(i, weight=0)
            frame.columnconfigure(0 if narrow else len(buttons)-1, weight=1)
            for i, button in enumerate(buttons):
                button.grid(row=(1 if i == len(buttons)-1 else 0) if narrow else 0, column=0 if narrow and i == len(buttons)-1 else i,
                            columnspan=len(buttons)-1 if narrow and i == len(buttons)-1 else 1,
                            sticky='w' if i < len(buttons)-1 else 'e',
                            padx=(0, 6) if not narrow and i < len(buttons)-1 else 0,
                            pady=(0, 5) if narrow and i < len(buttons)-1 else 0)
        frame.bind('<Configure>', arrange)
        frame.after_idle(arrange)

    def _save_settings(self):
        save_config(topic=self.topic_var.get(), cookie=self.cookie_var.get(), geometry=self.geometry(), show_logs=self.logs_visible)
        if hasattr(self, "scheduler"):
            self.scheduler.cookie = self.cookie_var.get()

    def enqueue(self):
        raw = self.link_var.get().strip()
        if not raw:
            self.status_var.set("请先粘贴 B 站、抖音或机核链接")
            self.link_field.focus_set()
            return
        added, duplicates, errors = self.scheduler.enqueue(raw, self.topic_var.get(), self.cookie_var.get())
        if added:
            self.link_var.set("")
            self._save_settings()
            self.status_var.set(f"已加入 {added} 条，主题：{sanitize_filename(self.topic_var.get() or '未分类')}" + (f"；重复 {duplicates} 条" if duplicates else ""))
        else:
            self.status_var.set("；".join(errors) or ("链接已在队列中" if duplicates else "未识别到有效链接，仅支持 B 站、抖音与机核电台"))
        self.refresh_library()

    def toggle_pause(self):
        if self.scheduler.paused.is_set():
            self.scheduler.paused.clear()
            self.pause_button.configure(text="暂停")
            self.status_var.set("队列已继续")
        else:
            self.scheduler.paused.set()
            self.pause_button.configure(text="继续")
            self.status_var.set("已暂停派发；当前下载或识别阶段会完成")

    def remove(self):
        identities = set(self.tree.selection())
        removed = self.scheduler.remove(identities)
        self.refresh_library()
        if self.document and not Path(self.document['path']).exists():
            self.document = None
            self.document_var.set('')
            self.document_title.set('稿件已删除，请选择其他稿件')
            self.reader.configure(state='normal')
            self.reader.delete('1.0', 'end')
            self.reader.configure(state='disabled')
        self.status_var.set(f"已删除 {removed} 条任务及对应稿件" + ('；部分文件删除失败，请查看日志' if removed < len(identities) else ''))

    def retry(self):
        self.scheduler.retry(set(self.tree.selection()))
        self.status_var.set("已重排所选任务的失败部分")

    def select_task(self, event=None):
        selected = self.tree.selection()
        if not selected:
            return
        job = next((j for j in self.scheduler.snapshot() if j["jid"] == selected[0]), None)
        if job:
            self.task_detail.set(f"主题：{job['topic']}\n{job['detail'] or job['error'] or job['status']}")
            path = next((p["txt"] for p in job["pages"] if p["status"] == "完成" and p["txt"]), None)
            if path:
                self.open_document(resolve_owned(path))

    def create_topic(self):
        try:
            folder, existed = create_topic(self.topic_var.get())
            self.topic_var.set(folder.name)
            self.refresh_topics()
            self._save_settings()
            self.status_var.set(f"主题已存在，已选择：{folder.name}" if existed else f"已创建主题文件夹：{folder.name}")
        except (OSError, ValueError) as exc:
            self.status_var.set(f"创建主题失败：{exc}")

    def refresh_topics(self):
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        self.topic_box.configure(values=sorted(p.name for p in OUTPUT_ROOT.iterdir() if p.is_dir()))

    def refresh_library(self):
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        self.refresh_topics()
        self.documents = sorted(OUTPUT_ROOT.rglob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        self.document_box.configure(values=[f"{p.parent.name} / {p.stem}" for p in self.documents])
        if self.document and Path(self.document["path"]) in self.documents:
            self.document_box.current(self.documents.index(Path(self.document["path"])))

    def select_document(self, event=None):
        index = self.document_box.current()
        if 0 <= index < len(self.documents):
            self.open_document(self.documents[index])

    def open_document(self, path):
        try:
            self.document = read_document(path)
            self.document_title.set(self.document["title"])
            if Path(path) in self.documents:
                self.document_box.current(self.documents.index(Path(path)))
            self.render()
        except (OSError, ValueError) as exc:
            self.status_var.set(f"读取稿件失败：{exc}")

    def render(self):
        if not self.document:
            return
        self.reader.configure(state="normal")
        self.reader.delete("1.0", "end")
        self.row_times = {}
        mode = self.mode_var.get()
        if mode == "原始记录" and self.document["raw"]:
            lines = [f"[{fmt_ts(s['start'])}–{fmt_ts(s['end'])}] {s['text']}" for s in self.document["raw"]["segments"]]
        else:
            lines = self.document["text"].splitlines()
            if mode == "原始记录":
                self.status_var.set("这份历史稿件没有原始记录，显示现存文字稿")
        stamp = ""
        for row, line in enumerate(lines, 1):
            match = re.match(r"\[([^\]]+)\]", line)
            if match:
                stamp = match.group(1)
                if mode == "通读":
                    line = line[match.end():].lstrip()
            self.row_times[row] = stamp
            self.reader.insert("end", line + "\n")
            if match and mode != "通读":
                self.reader.tag_add("timestamp", f"{row}.0", f"{row}.{len(match.group(0))}")
        self.reader.configure(state="disabled")

    def _selection(self):
        try:
            return self.reader.get("sel.first", "sel.last"), int(self.reader.index("sel.first").split(".")[0])
        except tk.TclError:
            self.status_var.set("请先在稿件中选中需要复制的文字")
            return "", 0

    def _copy(self, text, message):
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status_var.set(message)

    def copy_selection(self):
        text, row = self._selection()
        self._copy(text, "选段已复制")

    def copy_all(self):
        if self.document:
            self._copy(self.reader.get("1.0", "end-1c"), "当前视图全文已复制")

    def open_source(self):
        if self.document:
            url = self.document["url"]
            if urllib.parse.urlsplit(url).hostname in ("www.bilibili.com", "bilibili.com", "b23.tv", "www.gcores.com", "gcores.com", 'www.douyin.com', 'douyin.com', 'v.douyin.com'):
                webbrowser.open(url)
            else:
                self.status_var.set("稿件中没有受支持的来源链接")

    def toggle_logs(self):
        self.logs_visible = not self.logs_visible
        if self.logs_visible:
            self.log_frame.pack(side="bottom", fill="x", padx=18, pady=(0, 10), before=self.workspace)
        else:
            self.log_frame.pack_forget()

    def open_output(self):
        directory = Path(self.document['path']).parent if self.document else OUTPUT_ROOT / sanitize_filename(self.topic_var.get() or '未分类')
        if not directory.resolve().is_relative_to(OUTPUT_ROOT.resolve()):
            self.status_var.set('无法打开项目外的文件夹')
            return
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(directory)

    def append_log(self, message):
        logging.getLogger("transcripts").info(message)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{datetime.datetime.now():%H:%M:%S}] {message}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 500:
            self.log_text.delete("1.0", "100.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh(self):
        if self.closing:
            return
        jobs = self.scheduler.snapshot()
        seen = set(self.tree.get_children())
        for job in jobs:
            identity = job["jid"]
            seen.discard(identity)
            values = job["status"], job["title"], f"{job['progress']}%"
            tag = "done" if job["status"] == "完成" else "fail" if job["status"] in ("失败", "部分失败") else "active"
            if self.tree.exists(identity):
                self.tree.item(identity, values=values, tags=(tag,))
            else:
                self.tree.insert("", "end", iid=identity, values=values, tags=(tag,))
        for identity in seen:
            self.tree.delete(identity)
        self.count_var.set(f"共 {len(jobs)} · 完成 {sum(j['status'] == '完成' for j in jobs)}")
        selected = self.tree.selection()
        if selected:
            job = next((j for j in jobs if j["jid"] == selected[0]), None)
            if job:
                self.task_detail.set(f"主题：{job['topic']}\n{job['detail'] or job['error'] or job['status']}")
        while True:
            try:
                event, message = self.scheduler.events.get_nowait()
                self.append_log(message)
                if message.startswith("已生成"):
                    self.refresh_library()
            except queue.Empty:
                break
        while True:
            try:
                message = self.pending.get_nowait()
                self.status_var.set(message)
                self.append_log(message)
            except queue.Empty:
                break
        self.after(250, self._refresh)

    def settings(self):
        window = tk.Toplevel(self)
        window.title("本地设置")
        window.geometry("650x350")
        window.configure(bg=ui.T.bg)
        window.transient(self)
        card = ui.Card(window, pad=20)
        card.pack(fill="both", expand=True, padx=16, pady=16)
        body = card.body
        self.label(body, "B 站 Cookie（可选）", 11, True).pack(anchor="w")
        self.label(body, "需要登录的字幕与音频共用；仅保存在本机。", 9, color=ui.T.text2).pack(anchor="w", pady=8)
        field = ui.TextField(body, textvariable=self.cookie_var, width=560)
        field.entry.configure(show="•")
        field.pack(fill="x", pady=(0, 12))
        field.entry.bind("<FocusOut>", lambda e: self._save_settings())
        self.button(body, "保存设置", lambda: (self._save_settings(), window.destroy()), "primary").pack(anchor="e")
        self.label(body, "识别档位自动决定，无需选择模型。", 9, color=ui.T.text2).pack(anchor="w", pady=(20, 8))
        self.button(body, "下载 / 修复标点模型", self.download_punctuation).pack(anchor="w")
        self.label(body, "下载状态显示在主窗口，关闭主窗口会取消下载。", 9, color=ui.T.text2).pack(anchor="w", pady=8)

    def download_punctuation(self):
        if getattr(self, "model_thread", None) and self.model_thread.is_alive():
            self.status_var.set("模型正在下载，请查看运行日志")
            return
        self.model_cancel.clear()
        def run():
            try:
                from ..core.models import ensure_punctuation
                ensure_punctuation(lambda message, pct=None: self.pending.put(message), self.model_cancel.is_set)
                self.pending.put("本地标点模型已就绪")
            except Exception as exc:
                self.pending.put(f"模型下载未完成（{type(exc).__name__}），可重新下载续传")
        self.model_thread = threading.Thread(target=run, daemon=True)
        self.model_thread.start()

    def report_callback_exception(self, exc_type, exc, traceback):
        logging.getLogger("transcripts").error("界面操作失败", exc_info=(exc_type, exc, traceback))
        self.status_var.set(f"操作失败：{exc}；详情见运行日志")

    def close(self):
        if self.closing:
            return
        self._save_settings()
        self.closing = True
        self.model_cancel.set()
        self.status_var.set("正在保存队列并停止后台识别…")
        self.update_idletasks()
        self.scheduler.shutdown()
        self.destroy()


def main():
    from ..main import main as launch
    launch()


if __name__ == "__main__":
    main()
