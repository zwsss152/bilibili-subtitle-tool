"""Automatic queue and source-preserving transcript reader."""
import datetime
import logging
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, scrolledtext, filedialog
import urllib.parse
import webbrowser
from . import kit as ui
from .reader import TranscriptReader, parse_document
from ..config import load_config, save_config
from ..paths import OUTPUT_ROOT, APP_ICON, LOGS_DIR, resolve_owned
from ..version import VERSION
from ..services.scheduler import Scheduler
from ..core.textout import read_document, sanitize_filename
from ..core.topics import create_topic
from ..core.library import ORIGINAL, READING, originals, topics, reading_path
from ..core.documents import record_path
from ..core.reading import fingerprint


class BatchApp(tk.Tk):
    def __init__(self, autostart=True, demo_scale=None):
        ui.enable_dpi_awareness()
        super().__init__()
        if demo_scale:
            self.tk.call("tk", "scaling", 96 / 72 * demo_scale)
        self.cfg = load_config()
        self.title(f"文字稿 · B 站、抖音与机核 · {VERSION}")
        self.geometry(self.cfg.get("geometry") or "1180x820")
        line_height = tkfont.Font(font=ui.ui_font(self, 10)).metrics('linespace')
        self.minsize(960, max(640, line_height * 29))
        self.configure(bg=ui.T.bg)
        if APP_ICON.exists():
            self.iconbitmap(str(APP_ICON))
        self.document, self.documents, self.row_times = None, [], {}
        self.pending = queue.Queue()
        self.closing = False
        self._style()
        self._build()
        self.scheduler = Scheduler(cookie=self.cookie_var.get(), topic=self.topic_var.get(), autostart=autostart)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Control-Return>", lambda e: self.enqueue())
        self.refresh_library()
        self.after(100, self._refresh)
        if autostart:
            from ..core.cloud import configured
            if not configured() and not self.cfg.get('onboarding_seen'):
                self.after(350, self.first_run)

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
        self.status_var = tk.StringVar(value="粘贴平台链接或选择本地视频／音频，即可生成文字稿")
        status = tk.Frame(self, bg="#EFEFF3", pady=7)
        status.pack(side="bottom", fill="x")
        self.label(status, size=9, color=ui.T.text2, textvariable=self.status_var, anchor="w").pack(side="left", padx=18, fill="x", expand=True)
        self.count_var = tk.StringVar()
        self.label(status, size=9, color=ui.T.text2, textvariable=self.count_var).pack(side="right", padx=18)
        header = tk.Frame(self, bg=ui.T.bg)
        header.pack(fill="x", padx=22, pady=(16, 12))
        brand = self.label(header, "文字稿", 20, True)
        brand.pack(side="left")
        description = self.label(header, "B 站 · 抖音 · 机核  /  转写与阅读", color=ui.T.text2)
        description.pack(side="left", padx=16)
        def header_layout(event):
            if event.width < 1050:
                description.pack_forget()
            elif not description.winfo_manager():
                description.pack(side='left', padx=16, after=brand)
        header.bind('<Configure>', header_layout)
        self.button(header, "设置", self.settings).pack(side="right")
        self.button(header, "运行日志", self.toggle_logs).pack(side="right", padx=8)
        self.label(header, '云端识别与整理', color=ui.T.text2).pack(side='right', padx=8)

        new = ui.Card(self, pad=16, border_color=ui.T.border_card)
        new.pack(fill="x", padx=18, pady=(0, 12))
        body = new.body
        body.columnconfigure(1, weight=1)
        self.label(body, "链接／文件", color=ui.T.text2).grid(row=0, column=0, padx=(0, 12), sticky="w")
        self.link_var = tk.StringVar()
        self.link_field = ui.TextField(body, textvariable=self.link_var, width=400)
        self.link_field.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self.link_field.entry.bind("<Return>", lambda e: self.enqueue())
        self.button(body, "加入队列", self.enqueue, "primary").grid(row=0, column=2, sticky="e")
        self.button(body, "选择文件", self.choose_files).grid(row=0, column=3, sticky="e", padx=(8, 0))
        second = tk.Frame(body, bg=ui.T.surface)
        second.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(12, 0))
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
        self.queue_menu = tk.Menu(self, tearoff=False)
        self.queue_menu.add_command(label='重命名稿件', command=self.rename_transcript)
        self.tree.bind('<Button-3>', self.queue_context_menu)
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
        view = self.row(r)
        self.mode_var = tk.StringVar(value=ORIGINAL)
        self.mode_box = ttk.Combobox(view, textvariable=self.mode_var, values=(ORIGINAL, READING), state="readonly", width=12)
        self.mode_box.pack(side="left")
        self.mode_box.bind("<<ComboboxSelected>>", lambda e: self.render())
        self.organize_button = self.button(view, '整理', self.organize_document, 'primary')
        self.organize_button.configure(state='disabled')
        self._responsive_actions(view, [self.mode_box,
                                       self.organize_button,
                                       self.button(view, '打开文件夹', self.open_output),
                                       self.button(view, "打开来源", self.open_source)])
        self.reader = TranscriptReader(r)
        self.reader.pack(fill="both", expand=True)
        self.reader.show_message('选择稿件，开始阅读',
                                 '加入平台链接或选择本地视频／音频，或从上方列表打开已有稿件。')
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
        save_config(topic=self.topic_var.get(), cookie=self.cookie_var.get(), geometry=self.geometry(), show_logs=self.logs_visible,
                    engine='cloud')
        if hasattr(self, "scheduler"):
            self.scheduler.cookie = self.cookie_var.get()

    def choose_files(self):
        from ..core.local_media import MEDIA_EXTENSIONS
        paths = filedialog.askopenfilenames(parent=self, title='选择要转录的视频或音频',
            filetypes=[('视频和音频', ' '.join('*' + ext for ext in sorted(MEDIA_EXTENSIONS)))])
        if paths:
            self.enqueue('\n'.join(paths))

    def enqueue(self, text=None):
        raw = (text if text is not None else self.link_var.get()).strip()
        if not raw:
            self.status_var.set("请粘贴平台链接、本地文件完整路径，或点击选择文件")
            self.link_field.focus_set()
            return
        engine = 'cloud'
        from ..core.cloud import configured
        if engine == 'cloud' and not configured():
            self.status_var.set('请先填写百炼 API Key；输入一次即可自动使用在线方式')
            self.settings(on_saved=lambda: self.enqueue(raw))
            return
        added, duplicates, errors = self.scheduler.enqueue(raw, self.topic_var.get(), self.cookie_var.get(), engine)
        if added:
            if text is None:
                self.link_var.set("")
            self._save_settings()
            self.status_var.set(f"已加入 {added} 条，主题：{sanitize_filename(self.topic_var.get() or '未分类')}" + (f"；重复 {duplicates} 条" if duplicates else ""))
        else:
            self.status_var.set("；".join(errors) or ("内容已在队列中" if duplicates else "未识别到有效链接或本地媒体文件"))
        self.refresh_library()

    def toggle_pause(self):
        if self.scheduler.paused.is_set():
            self.scheduler.paused.clear()
            self.pause_button.configure(text="暂停")
            self.status_var.set("队列已继续")
        else:
            self.scheduler.paused.set()
            self.pause_button.configure(text="继续")
            self.status_var.set("已暂停派发；当前下载、识别或整理分块会完成")

    def remove(self):
        identities = set(self.tree.selection())
        removed = self.scheduler.remove(identities)
        self.refresh_library()
        if self.document and not Path(self.document['path']).exists():
            self.document = None
            self.document_var.set('')
            self.document_title.set('稿件已删除，请选择其他稿件')
            self.reader.show_message('稿件已删除', '请从上方列表选择其他稿件。')
        self.status_var.set(f"已删除 {removed} 条任务及对应稿件" + ('；部分文件删除失败，请查看日志' if removed < len(identities) else ''))

    def retry(self):
        self.scheduler.retry(set(self.tree.selection()))
        self.status_var.set("已重排所选任务的失败部分及失败的稿件整理")

    def queue_context_menu(self, event):
        self.queue_menu.unpost()
        identity = self.tree.identify_row(event.y)
        if not identity:
            return 'break'
        self.tree.selection_set(identity)
        self.tree.focus(identity)
        self.queue_menu.entryconfigure(0, state='normal' if self.scheduler.can_rename_transcript(identity) else 'disabled')
        try:
            self.queue_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.queue_menu.grab_release()
        return 'break'

    def rename_transcript(self):
        selected = self.tree.selection()
        if len(selected) != 1 or not self.scheduler.can_rename_transcript(selected[0]):
            self.status_var.set('只能重命名已完成且稿件文件存在的任务')
            return
        identity = selected[0]
        job = next(job for job in self.scheduler.snapshot() if job['jid'] == identity)
        window = tk.Toplevel(self, bg=ui.T.bg)
        window.title('重命名稿件')
        window.transient(self)
        window.resizable(False, False)
        body = tk.Frame(window, bg=ui.T.bg, padx=20, pady=20)
        body.pack(fill='both', expand=True)
        self.label(body, '稿件名称', bold=True).pack(anchor='w', pady=(0, 8))
        name = tk.StringVar(value=job['title'])
        field = ui.TextField(body, textvariable=name, width=440)
        field.pack(fill='x')
        error = tk.StringVar()
        self.label(body, textvariable=error, color=ui.T.error, wraplength=440, anchor='w', justify='left').pack(fill='x', pady=(8, 12))
        actions = self.row(body)

        def save(event=None):
            try:
                mapping = self.scheduler.rename_transcript(identity, name.get())
            except (OSError, ValueError, RuntimeError) as exc:
                error.set(str(exc))
                return 'break'
            current = Path(self.document['path']).resolve() if self.document else None
            renamed_current = next((new for old, new in mapping.items() if resolve_owned(old) == current), None)
            # Update the current document before refreshing the dropdown.
            if renamed_current:
                self.open_document(resolve_owned(renamed_current))
            self.refresh_library()
            updated = next(job for job in self.scheduler.snapshot() if job['jid'] == identity)
            self.tree.item(identity, values=(updated['status'], updated['title'], f"{updated['progress']}%"))
            if not renamed_current:
                self.select_task()
            self.status_var.set(f"稿件已重命名：{updated['title']}")
            window.destroy()
            return 'break'

        self.button(actions, '取消', window.destroy).pack(side='left')
        self.button(actions, '保存', save, 'primary').pack(side='right')
        window.bind('<Return>', save)
        window.bind('<Escape>', lambda event: window.destroy())
        window.update_idletasks()
        window.geometry(f'+{self.winfo_rootx() + (self.winfo_width() - window.winfo_reqwidth()) // 2}+{self.winfo_rooty() + 160}')
        window.grab_set()
        field.entry.focus_set()
        field.entry.selection_range(0, 'end')

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
        available = topics()
        previous = getattr(self, '_known_topics', {self.cfg.get('topic', '')})
        current = self.topic_var.get()
        if current and current in previous and current not in available:
            self.topic_var.set('')
        self._known_topics = set(available)
        self.topic_box.configure(values=available)

    def organize_document(self):
        if not self.document:
            return
        try:
            queued = self.scheduler.request_reading(self.document['path'])
            self.mode_var.set(READING)
            self.render()
            self.status_var.set('当前稿件已加入整理队列' if queued else '当前稿件已整理或正在整理')
        except (OSError, ValueError) as exc:
            self.status_var.set(f'无法整理：{exc}')

    def refresh_library(self):
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        self.refresh_topics()
        self.documents = sorted(originals(), key=lambda p: p.stat().st_mtime, reverse=True)
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
        try:
            self.document = read_document(self.document['path'])
        except (OSError, ValueError) as exc:
            self.status_var.set(f'读取稿件失败：{exc}')
            return
        mode = self.mode_var.get()
        progress = (self.document['raw'] or {}).get('reading') or {}
        valid = progress.get('source_hash') == fingerprint(self.document)
        complete = reading_path(self.document['path']).is_file() and valid and progress.get('status') in ('完成','原文排版')
        active = progress.get('status') in ('待整理','整理中','写入中') and (self.document['raw'] or {}).get('reading_requested', False)
        self.organize_button.configure(state='disabled' if complete or active else 'normal')
        self._readable_available = True
        if mode == READING:
            path = reading_path(self.document['path'])
            progress = (self.document['raw'] or {}).get('reading') or {}
            valid = progress.get('source_hash') == fingerprint(self.document)
            if path.is_file() and valid and progress.get('status') in ('完成', '原文排版'):
                text = path.read_text(encoding='utf-8-sig')
            else:
                self._readable_available = False
                blocks = len(progress.get('blocks', []))
                text = (f'整理阅读稿生成中（已处理 {blocks} 段）。\n\n时间戳原稿已保存，可切换查看。'
                        if active else '尚未生成整理阅读稿。\n\n点击上方“整理”，仅整理当前稿件。')
                if progress.get('status') == '整理失败':
                    text = f'整理失败：{progress.get("error", "请查看运行日志")}\n\n点击上方“整理”重试。'
        else:
            text = self.document['text']
        key = self.document['path'], mode
        previous_scroll = self.reader.yview()[0] if getattr(self, '_rendered_document_key', None) == key else 0
        source_lines = self.document['text'].splitlines()
        has_title = source_lines and source_lines[0].startswith(('# ', '#\t'))
        fallback_title = self.document['title'] if has_title else Path(self.document['path']).stem
        layout = parse_document(text, fallback_title)
        self.document_title.set(layout.title)
        self.reader.show(layout)
        self.row_times = self.reader.row_times
        self.reader.yview_moveto(previous_scroll)
        self._rendered_document_key = key
        self._document_signature = self._signature()

    def _signature(self):
        if not self.document:
            return None
        original = Path(self.document['path'])
        paths = (original, record_path(original), reading_path(original))
        return tuple(p.stat().st_mtime_ns if p.exists() else None for p in paths)

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
        if not getattr(self, '_readable_available', True):
            self.status_var.set('阅读稿尚未完成，请切换到时间戳原稿')
            return
        text, row = self._selection()
        self._copy(text, "选段已复制")

    def copy_all(self):
        if self.document and getattr(self, '_readable_available', True):
            self._copy(self.reader.copy_text, "当前视图全文已复制")
        elif self.document:
            self.status_var.set('阅读稿尚未完成，请切换到时间戳原稿')

    def open_source(self):
        if self.document:
            url = self.document["url"]
            if urllib.parse.urlsplit(url).scheme == 'file':
                from ..core.local_media import media_path
                try:
                    os.startfile(str(media_path(url)))
                except (OSError, ValueError) as exc:
                    self.status_var.set(str(exc))
            elif urllib.parse.urlsplit(url).hostname in ("www.bilibili.com", "bilibili.com", "b23.tv", "www.gcores.com", "gcores.com", 'www.douyin.com', 'douyin.com', 'v.douyin.com'):
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
        category = READING if self.mode_var.get() == READING else ORIGINAL
        directory = ((reading_path(self.document['path']) if category == READING else Path(self.document['path'])).parent
                     if self.document else OUTPUT_ROOT / category / sanitize_filename(self.topic_var.get() or '未分类'))
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
        self.refresh_topics()
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
                self.task_detail.set(f"主题：{job['topic']} · {'在线' if job.get('engine') == 'cloud' else '旧本地任务（重试转在线）'}\n{job['detail'] or job['error'] or job['status']}")
        while True:
            try:
                event, message = self.scheduler.events.get_nowait()
                self.append_log(message)
                if message.startswith(("已生成", '已整理')):
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
        if self.document:
            try:
                if self._signature() != getattr(self, '_document_signature', None):
                    self.render()
            except (OSError, ValueError):
                pass
        self.after(250, self._refresh)

    def first_run(self):
        from tkinter import messagebox
        save_config(onboarding_seen=True)
        if messagebox.askokcancel('欢迎使用文字稿',
                '程序已包含运行环境，无需安装 Python 或下载模型。\n\n'
                '转录和整理通过阿里云百炼完成，会发送音频与文字并产生对应账户费用。\n'
                '请使用自己的北京地域百炼 API Key，并在设置中选择识别、整理和核对模型。\n'
                '密钥首次填写后会在本机加密保存。\n\n'
                '点击“确定”打开设置；也可以稍后从右上角设置中填写。', parent=self):
            self.settings()

    def settings(self, on_saved=None):
        from ..core import cloud
        existing = getattr(self, '_settings_window', None)
        if existing is not None and existing.winfo_exists():
            if on_saved is not None:
                existing._on_saved = on_saved
            existing.lift()
            existing.focus_set()
            return existing
        window = tk.Toplevel(self)
        window._on_saved = on_saved
        self._settings_window = window
        window.title('设置 · 云端服务与模型')
        window.geometry('720x680')
        window.minsize(680, 640)
        window.configure(bg=ui.T.bg)
        window.transient(self)
        notebook = ttk.Notebook(window)
        tabs = {}
        for title in ('云端服务', '模型选择', '平台与应用'):
            frame = tk.Frame(notebook, bg=ui.T.bg, padx=18, pady=16)
            notebook.add(frame, text=title)
            tabs[title] = frame
        body = tabs['云端服务']
        self.label(body, '阿里云百炼 · 北京地域', 12, True).pack(anchor='w')
        self.label(body, '请使用你自己的 API Key，费用由对应百炼账户承担。', 10, color=ui.T.text2).pack(anchor='w', pady=(8, 4))
        self.label(body, '音频与待整理文字会发送至百炼；平台 Cookie 不会发送。', 9, color=ui.T.text2).pack(anchor='w', pady=(0, 18))
        self.label(body, 'API Key', 11, True).pack(anchor='w')
        key = tk.StringVar()
        key_field = ui.TextField(body, textvariable=key, width=560)
        key_field.entry.configure(show='•')
        key_field.pack(fill='x', pady=(8, 4))
        state = '✓ 密钥已保存；安全起见不回显，留空保留原密钥。' if cloud.configured() else '尚未保存密钥；填写后请点击底部“保存设置”。'
        self.label(body, state, 9, color=ui.T.text2).pack(anchor='w')
        self.label(body, '业务空间 ID（可选）', 10, True).pack(anchor='w', pady=(20, 8))
        workspace = tk.StringVar(value=cloud.read_json(cloud.SETTINGS, {}).get('workspace', ''))
        ui.TextField(body, textvariable=workspace, width=560).pack(fill='x')
        self.label(body, '使用专属业务空间域名时填写；普通账户留空。', 9, color=ui.T.text2).pack(anchor='w', pady=6)
        self.button(body, '打开百炼控制台', lambda: webbrowser.open('https://bailian.console.aliyun.com/')).pack(anchor='w', pady=16)
        body = tabs['模型选择']
        self.label(body, '为三个处理阶段分别选择模型', 12, True).pack(anchor='w', pady=(0, 8))
        self.label(body, '可选择推荐项，也可输入兼容模型的完整 ID；模型权限以你的账户为准。', 9, color=ui.T.text2).pack(anchor='w', pady=(0, 14))
        values = cloud.model_settings()
        model_vars, model_boxes = {}, {}
        fields = [('asr_model', '语音识别', cloud.ASR_CHOICES),
                  ('editor_model', '阅读稿整理', cloud.TEXT_CHOICES),
                  ('review_model', '内容核对', (cloud.REVIEW_MODEL, cloud.EDITOR_MODEL, 'qwen3.7-plus', 'qwen-plus'))]
        for name, title, choices in fields:
            self.label(body, title, 10, True).pack(anchor='w', pady=(8, 4))
            value = tk.StringVar(value=values[name])
            model_vars[name] = value
            box = ttk.Combobox(body, textvariable=value, values=choices, state='normal')
            model_boxes[name] = box
            box.pack(fill='x', pady=(0, 6))
        catalog_state = tk.StringVar(value='可获取当前密钥对应的模型列表，也可保留手动输入。')
        catalog_results = queue.Queue()
        fetching = False
        def poll_catalog():
            nonlocal fetching
            if not window.winfo_exists():
                return
            try:
                result, message = catalog_results.get_nowait()
                fetching = False
                if result is not None:
                    for name, items in result.items():
                        if items:
                            model_boxes[name].configure(values=items)
                catalog_state.set(message)
            except queue.Empty:
                pass
            window.after(100, poll_catalog)
        window.after(100, poll_catalog)
        def refresh_models():
            nonlocal fetching
            if fetching:
                return
            fetching = True
            credential, space = key.get(), workspace.get()
            catalog_state.set('正在获取可用模型…')
            def run():
                try:
                    result = cloud.available_models(credential, space)
                    catalog_results.put((result, '列表已更新；请选择后保存。列表可见不代表账户已开通全部模型。'))
                except (OSError, ValueError, RuntimeError) as exc:
                    catalog_results.put((None, str(exc)))
            threading.Thread(target=run, daemon=True).start()
        self.button(body, '获取可用模型（不发起转录）', refresh_models).pack(anchor='w', pady=(6, 3))
        self.label(body, textvariable=catalog_state, size=9, color=ui.T.text2, wraplength=590).pack(anchor='w')
        self.label(body, '识别仅支持同步 ASR 及其日期快照，不支持实时、filetrans 或其他厂商接口。', 9, color=ui.T.text2).pack(anchor='w', pady=(16, 5))
        self.label(body, 'Qwen3-ASR 的原稿按音频分块标时；具备词时间戳的模型保留更细时间。', 9, color=ui.T.text2).pack(anchor='w', pady=3)
        self.label(body, '保存后用于新任务和新整理请求；已入队任务保持原模型。', 9, color=ui.T.text2).pack(anchor='w', pady=3)
        body = tabs['平台与应用']
        self.label(body, 'B 站 Cookie（可选）', 11, True).pack(anchor='w')
        field = ui.TextField(body, textvariable=self.cookie_var, width=560)
        field.entry.configure(show='•')
        field.pack(fill='x', pady=(8, 5))
        self.label(body, '仅保存在本机，用于需要登录的资源。', 9, color=ui.T.text2).pack(anchor='w')
        self.label(body, f'文字稿 {VERSION}', 11, True).pack(anchor='w', pady=(28, 8))
        self.label(body, '个人数据独立保存，升级和卸载会保留稿件、队列与设置。', 9, color=ui.T.text2).pack(anchor='w', pady=5)
        self.button(body, '打开日志文件夹', self.open_logs).pack(anchor='w', pady=10)
        footer = tk.Frame(window, bg=ui.T.bg)
        # Reserve the footer before the expanding notebook. Otherwise Tk can
        # allocate all available height to the notebook and hide Save at high DPI.
        footer.pack(side='bottom', fill='x', padx=18, pady=(0, 16))
        notebook.pack(fill='both', expand=True, padx=16, pady=(16, 8))
        error = tk.StringVar()
        self.label(footer, textvariable=error, size=9, color=ui.T.error, wraplength=600).pack(anchor='w', pady=6)
        def save():
            try:
                models = cloud.normalize_models({name: value.get() for name, value in model_vars.items()})
                if not key.get().strip() and not cloud.configured():
                    raise ValueError('请先填写 API Key，再点击保存设置。')
                cloud.save_credentials(key.get(), workspace.get(), models=models)
                cloud.key_value()  # Confirm persisted credentials are readable.
                key.set('')
                self._save_settings()
                self.status_var.set('设置已保存；新任务使用所选模型，已入队任务保持原模型')
                window.destroy()
                if window._on_saved is not None:
                    window._on_saved()
            except (OSError, ValueError, RuntimeError) as exc:
                error.set(str(exc))
        self.button(footer, '保存设置', save, 'primary').pack(anchor='e')
        return window

    def open_logs(self):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(LOGS_DIR)

    def report_callback_exception(self, exc_type, exc, traceback):
        logging.getLogger("transcripts").error("界面操作失败", exc_info=(exc_type, exc, traceback))
        self.status_var.set(f"操作失败：{exc}；详情见运行日志")

    def close(self):
        if self.closing:
            return
        self._save_settings()
        self.closing = True
        self.status_var.set("正在保存进度并停止后台识别与整理…")
        self.update_idletasks()
        self.scheduler.shutdown()
        self.destroy()


def main():
    from ..main import main as launch
    launch()


if __name__ == "__main__":
    main()
