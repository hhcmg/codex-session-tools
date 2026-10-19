"""Two-tab Tk interface. All filesystem/SSH work runs outside the Tk thread."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

from . import __version__, local_catalog, remote_client
from .presentation import display_time, local_details, remote_details, remote_result, safe_text


def text_content(widget, content):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", content)
    widget.configure(state="disabled")


def wrapped_label(parent, text=None, textvariable=None, **kwargs):
    label = ttk.Label(parent, text=text, textvariable=textvariable, wraplength=700, **kwargs)
    label.bind("<Configure>", lambda event: label.configure(wraplength=max(240, event.width - 8)))
    return label


class ConfirmDialog(simpledialog.Dialog):
    def __init__(self, parent, title, details, phrase, acknowledgment):
        self.details, self.phrase, self.acknowledgment = details, phrase, acknowledgment
        self.result = False
        super().__init__(parent, title)

    def body(self, master):
        self.configure(background="#ffffff")
        master.configure(background="#ffffff")
        ttk.Label(master, text="请核对操作对象", style="Section.TLabel").pack(anchor="w", padx=16, pady=(12, 4))
        body = ScrolledText(master, width=78, height=13, wrap="word", font=("Microsoft YaHei UI", 10),
                            borderwidth=0, highlightthickness=1, highlightbackground="#e1e7ef", padx=12, pady=12,
                            background="#f8fafc", foreground="#21334d")
        body.pack(fill="both", expand=True, padx=16, pady=8)
        text_content(body, self.details)
        self.ack = tk.BooleanVar(value=False)
        ttk.Label(master, text=self.acknowledgment, wraplength=640).pack(fill="x", padx=16, pady=(8, 0))
        ttk.Checkbutton(master, text="我已阅读并确认以上事项", variable=self.ack).pack(fill="x", padx=16, pady=10)
        ttk.Label(master, text=f"输入 {self.phrase} 确认：", wraplength=640).pack(anchor="w", padx=16)
        self.answer_var = tk.StringVar()
        self.answer = ttk.Entry(master, width=72, textvariable=self.answer_var)
        self.answer.pack(fill="x", padx=16, pady=(8, 12))
        self.ack.trace_add("write", self.update_confirmation)
        self.answer_var.trace_add("write", self.update_confirmation)
        return self.answer

    def buttonbox(self):
        box = ttk.Frame(self, padding=(16, 4, 16, 16))
        box.pack(fill="x")
        self.confirm_button = ttk.Button(box, text="确认执行", command=self.ok, style="Danger.TButton", state="disabled")
        self.confirm_button.pack(side="right")
        ttk.Button(box, text="取消", command=self.cancel).pack(side="right", padx=10)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def update_confirmation(self, *_):
        if hasattr(self, "confirm_button"):
            ready = self.ack.get() and self.answer_var.get().strip() == self.phrase
            self.confirm_button.configure(state="normal" if ready else "disabled")

    def validate(self):
        if not self.ack.get() or self.answer.get().strip() != self.phrase:
            messagebox.showwarning("尚未确认", "请勾选确认项，并准确输入上方要求的文字。", parent=self)
            return False
        return True

    def apply(self):
        self.result = True


def confirm(parent, title, details, phrase, acknowledgment):
    return bool(ConfirmDialog(parent, title, details, phrase, acknowledgment).result)


class JobTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=20)
        self.app = app
        self.busy = False
        self.controls = []
        self.status = tk.StringVar(value="")

    def set_busy(self, busy):
        self.busy = busy
        for control in self.controls:
            control.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.pack(fill="x")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
        self.update_actions()

    def update_actions(self):
        pass

    def run_job(self, message, operation, success, failure):
        if self.busy:
            return
        self.status.set(message)
        self.set_busy(True)

        def completed(result, error):
            self.set_busy(False)
            if error is not None:
                failure(error)
            else:
                success(result)

        self.app.submit(operation, completed)

    def footer(self, parent=None):
        parent = self if parent is None else parent
        status_box = ttk.Frame(parent, style="Status.TFrame", padding=(12, 7))
        status_box.pack(fill="x", pady=(12, 0))
        ttk.Label(status_box, text="●", style="Status.TLabel").pack(side="left", padx=(0, 8), anchor="n")
        wrapped_label(status_box, textvariable=self.status, style="Status.TLabel").pack(fill="x", expand=True)
        progress_slot = ttk.Frame(parent, style="Status.TFrame", height=3)
        progress_slot.pack(fill="x")
        progress_slot.pack_propagate(False)
        self.progress = ttk.Progressbar(progress_slot, mode="indeterminate", style="Slim.Horizontal.TProgressbar")


class LocalTab(JobTab):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.rows, self.visible = [], {}
        self.filter_timer = None
        self.db = tk.StringVar(value=str(local_catalog.default_db()))
        self.query = tk.StringVar()
        self.count = tk.StringVar(value="会话目录 · 尚未加载")
        heading = ttk.Frame(self)
        heading.pack(fill="x")
        ttk.Label(heading, text="清理本地残留会话", style="Section.TLabel").pack(side="left")
        ttk.Label(heading, text="  本机 · Windows  ", style="Badge.TLabel").pack(side="right", pady=3)
        wrapped_label(self, text="查找已在网页端删除的 ChatGPT 会话。移除前，请完全退出 Codex / ChatGPT。", style="Muted.TLabel")\
            .pack(fill="x", pady=(7, 18))
        location = ttk.Frame(self)
        location.pack(fill="x")
        ttk.Label(location, text="本机数据库").pack(side="left", padx=(0, 8))
        entry = ttk.Entry(location, textvariable=self.db)
        entry.pack(side="left", fill="x", expand=True)
        browse = ttk.Button(location, text="选择文件", command=self.browse)
        browse.pack(side="left", padx=8)
        self.load_button = ttk.Button(location, text="检查并刷新", command=self.reload, style="Primary.TButton")
        self.load_button.pack(side="left")
        search = ttk.Frame(self)
        search.pack(fill="x", pady=12)
        ttk.Label(search, text="搜索标题").pack(side="left", padx=(0, 8))
        query_entry = ttk.Entry(search, textvariable=self.query)
        query_entry.pack(side="left", fill="x", expand=True)
        self.controls = [entry, browse, self.load_button, query_entry]
        table_heading = ttk.Frame(self)
        table_heading.pack(fill="x", pady=(0, 8))
        ttk.Label(table_heading, textvariable=self.count, style="LabelStrong.TLabel").pack(side="left")
        ttk.Label(table_heading, text="需自行确认哪些记录已在云端删除", style="Muted.TLabel").pack(side="right")
        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("title", "time", "host", "id"), show="headings", selectmode="browse", height=8)
        for name, label, width in (("title", "会话标题", 330), ("time", "更新时间（本机）", 165),
                                   ("host", "主机编号", 100), ("id", "会话编号末尾", 110)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=70, stretch=name == "title")
        scrollbar = ttk.Scrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("odd", background="#f5f8fc")
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", before=table)
        self.details = ScrolledText(bottom, height=3, wrap="word", font=("Microsoft YaHei UI", 9),
                                    background="#f8fafc", foreground="#3d506a", borderwidth=0,
                                    highlightthickness=1, highlightbackground="#e2e8f0", padx=12, pady=6)
        self.details.pack(fill="x", pady=10)
        text_content(self.details, "选中一条记录后，在这里核对完整标题、主机和会话编号。")
        actions = ttk.Frame(bottom)
        actions.pack(fill="x")
        self.remove_button = ttk.Button(actions, text="核对并移除所选记录…", command=self.remove, style="Danger.TButton")
        self.remove_button.pack(side="left")
        wrapped_label(bottom, text="仅修改本地目录；无备份与恢复。后续同步可能使记录重新出现。", style="Muted.TLabel")\
            .pack(fill="x", pady=(10, 0))
        self.footer(bottom)
        self.tree.bind("<<TreeviewSelect>>", self.selection_changed)
        self.db.trace_add("write", self.path_changed)
        self.query.trace_add("write", self.schedule_filter)
        self.status.set("尚未读取数据库。请确认路径后点击“检查并刷新”。")
        self.update_actions()

    def browse(self):
        selected = filedialog.askopenfilename(parent=self, title="选择 Codex 本地目录数据库",
                                              filetypes=[("SQLite 数据库", "*.db *.sqlite"), ("所有文件", "*")])
        if selected:
            self.db.set(selected)

    def clear_rows(self):
        self.rows, self.visible = [], {}
        self.tree.delete(*self.tree.get_children())
        text_content(self.details, "尚未选择有效记录。")
        self.count.set("会话目录 · 尚未加载")
        self.update_actions()

    def path_changed(self, *_):
        self.clear_rows()
        self.status.set("数据库路径已改变，请重新检查。")

    def schedule_filter(self, *_):
        if self.filter_timer:
            self.after_cancel(self.filter_timer)
        self.filter_timer = self.after(180, self.filter_rows)

    def filter_rows(self):
        self.filter_timer = None
        self.tree.delete(*self.tree.get_children())
        self.visible = {}
        text_content(self.details, "选中记录查看完整信息。")
        query = self.query.get().strip().casefold()
        for row in self.rows:
            title = safe_text(row.get("display_title"), "(无标题)")
            if query and query not in title.casefold():
                continue
            iid = self.tree.insert("", "end", values=(title, display_time(row.get("source_updated_at")),
                                                       safe_text(row.get("host_id")), safe_text(row.get("thread_id"))[-8:]),
                                   tags=("odd" if len(self.visible) % 2 else "even",))
            self.visible[iid] = row
        self.count.set(f"会话目录 · {len(self.visible)} / {len(self.rows)} 条")
        self.update_actions()

    def selected(self):
        selection = self.tree.selection()
        return self.visible.get(selection[0]) if selection else None

    def selection_changed(self, *_):
        row = self.selected()
        if row:
            detail = local_details(row).replace("\n主机编号：", "  ·  主机编号：")
            if not row.get("_deletable", True):
                detail += "\n禁止移除：" + safe_text(row.get("_blocked_reason"))
            text_content(self.details, detail)
        self.update_actions()

    def update_actions(self):
        row = self.selected() if hasattr(self, "tree") else None
        if hasattr(self, "remove_button"):
            self.remove_button.configure(state="normal" if not self.busy and row and row.get("_deletable", True) else "disabled")

    def reload(self):
        db = self.db.get()
        self.clear_rows()

        def success(rows):
            if self.db.get() != db:
                return
            self.rows = rows
            self.filter_rows()
            self.status.set(f"读取到 {len(rows)} 条 ChatGPT 目录记录。请自行确认云端已删除的目标。")

        def failure(error):
            self.clear_rows()
            self.status.set("读取失败，过期列表已清空。请检查路径及数据库兼容性。")
            messagebox.showerror("无法读取目录", safe_text(error), parent=self)

        self.run_job("正在检查数据库结构并读取目录…", lambda: local_catalog.list_entries(Path(db)), success, failure)

    def remove(self):
        row = self.selected()
        if self.busy or not row or not row.get("_deletable", True):
            return
        db = self.db.get()
        if not confirm(self, "确认移除本地目录记录", f"数据库：{db}\n\n{local_details(row)}",
                       "REMOVE " + row["thread_id"], "我确认目标已在网页端删除，并了解本工具不提供恢复。"):
            return

        def operation():
            local_catalog.remove_entry(Path(db), row)
            try:
                return local_catalog.list_entries(Path(db)), None
            except Exception as exc:
                return [], exc

        def success(result):
            rows, refresh_error = result
            self.clear_rows()
            if self.db.get() != db:
                self.status.set("已移除记录；当前路径已改变，请重新检查。")
                return
            if refresh_error:
                self.status.set("删除已完成，但列表刷新失败。过期列表已清空，请重新检查。")
                messagebox.showwarning("删除已完成，刷新失败", safe_text(refresh_error), parent=self)
            else:
                self.rows = rows
                self.filter_rows()
                self.status.set("已移除所选本地目录记录。现在可以重新打开 Codex 检查侧栏。")

        def failure(error):
            self.clear_rows()
            self.status.set("未完成移除。请检查错误并刷新列表后重试。")
            messagebox.showerror("未能移除", safe_text(error), parent=self)

        self.run_job("正在检查应用进程、复核记录并执行事务…", operation, success, failure)


class RemoteTab(JobTab):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.snapshot, self.inspected_inputs = None, None
        self.server, self.thread_id, self.home = tk.StringVar(), tk.StringVar(), tk.StringVar()
        heading = ttk.Frame(self)
        heading.pack(fill="x")
        ttk.Label(heading, text="诊断远端会话写锁", style="Section.TLabel").pack(side="left")
        ttk.Label(heading, text="  SSH · Linux  ", style="Badge.TLabel").pack(side="right", pady=3)
        wrapped_label(self, text="先检查持锁后端与受影响会话，再决定是否请求后端退出。活动状态未知时禁止执行。", style="Muted.TLabel")\
            .pack(fill="x", pady=(7, 18))
        form = ttk.Frame(self)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        for row, (label, var) in enumerate((("SSH 服务器别名", self.server), ("会话编号 / 链接", self.thread_id),
                                           ("远端 CODEX_HOME（可选）", self.home))):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            entry = ttk.Entry(form, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            self.controls.append(entry)
        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=12)
        self.inspect_button = ttk.Button(actions, text="仅检查写锁", command=self.inspect, style="Primary.TButton")
        self.inspect_button.pack(side="left")
        self.unlock_button = ttk.Button(actions, text="核对并释放写锁…", command=self.unlock, style="Danger.TButton")
        self.unlock_button.pack(side="left", padx=12)
        self.controls.append(self.inspect_button)
        ttk.Label(self, text="检查结果与影响范围", style="LabelStrong.TLabel").pack(anchor="w", pady=(2, 8))
        self.details = ScrolledText(self, height=16, wrap="word", font=("Microsoft YaHei UI", 10),
                                    background="#f8fafc", foreground="#3d506a", borderwidth=0,
                                    highlightthickness=1, highlightbackground="#e2e8f0", padx=16, pady=12)
        self.details.pack(fill="both", expand=True)
        text_content(self.details, "检查结果将显示远端用户、实际 Codex 目录、持锁 PID 及受影响会话。\n\n首次连接或认证失败时，请先在终端使用 ssh <服务器别名> 完成正常登录。")
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", before=self.details)
        wrapped_label(bottom, text="释放操作可能使同一后端的其他会话断开。不会删除锁文件或历史；仅核验目标历史文件。", style="Muted.TLabel")\
            .pack(fill="x", pady=(12, 0))
        self.footer(bottom)
        for var in (self.server, self.thread_id, self.home):
            var.trace_add("write", self.inputs_changed)
        self.status.set("尚未连接。默认不保存服务器、会话编号或操作日志。")
        self.update_actions()

    def inputs(self):
        return self.server.get().strip(), self.thread_id.get().strip(), self.home.get().strip() or None

    def inputs_changed(self, *_):
        self.snapshot, self.inspected_inputs = None, None
        text_content(self.details, "连接参数已改变，请重新检查写锁。")
        self.status.set("请先执行仅检查。")
        self.update_actions()

    def update_actions(self):
        if hasattr(self, "unlock_button"):
            enabled = not self.busy and self.snapshot and self.snapshot.get("ready") and self.inspected_inputs == self.inputs()
            self.unlock_button.configure(state="normal" if enabled else "disabled")

    def failure(self, error):
        self.snapshot, self.inspected_inputs = None, None
        self.update_actions()
        unknown = getattr(error, "outcome_unknown", False)
        status = "执行结果未知。请重新仅检查，不要直接重试解锁。" if unknown else "远端操作失败。请检查错误后重新仅检查。"
        self.status.set(status)
        text_content(self.details, status + "\n\n" + safe_text(error))
        messagebox.showerror("结果未知" if unknown else "远端操作未完成", safe_text(error) + "\n\n" + status, parent=self)

    def inspect(self):
        inputs = self.inputs()
        self.snapshot, self.inspected_inputs = None, None
        text_content(self.details, "正在获取新的检查结果…")

        def success(snapshot):
            if inputs != self.inputs():
                self.status.set("参数已变化，检查结果已作废。")
                return
            self.snapshot, self.inspected_inputs = snapshot, inputs
            text_content(self.details, remote_details(snapshot))
            if snapshot.get("status") == "unlocked":
                self.status.set("检查时未持有写锁，无需结束任何进程。请返回应用重试。")
            elif snapshot.get("ready"):
                self.status.set("检查完成。请核对全部受影响会话，再决定是否释放。")
            else:
                self.status.set("当前状态禁止释放写锁。请查看检查结果中的原因。")
            self.update_actions()

        self.run_job("正在通过 SSH 检查（最多约 60 秒）…", lambda: remote_client.inspect_remote(*inputs), success, self.failure)

    def unlock(self):
        snapshot = self.snapshot
        if self.busy or not snapshot or not snapshot.get("ready") or self.inputs() != self.inspected_inputs:
            return
        server = self.inspected_inputs[0]
        if not confirm(self, "确认请求远端后端退出", remote_details(snapshot), "UNLOCK " + str(snapshot["owner"]["pid"]),
                       "我确认受影响会话均可断开，已关闭原应用中的目标会话。"):
            return
        self.snapshot, self.inspected_inputs = None, None

        def success(result):
            message, code = remote_result(result)
            self.status.set(message)
            text_content(self.details, message + "\n\n" + safe_text(result.get("message"), "")
                         + "\n\n如需后续操作，请重新执行仅检查。")
            self.update_actions()
            if code:
                messagebox.showwarning("请检查执行结果", message, parent=self)

        self.run_job("正在复核状态、请求后端退出并验证结果…", lambda: remote_client.unlock_remote(server, snapshot), success, self.failure)


class MaintenanceApp:
    def __init__(self, root):
        self.root = root
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codex-maintenance")
        self.events = queue.Queue()
        self.closed = False
        root.title("Codex 会话维护工具")
        root.geometry("1120x850")
        root.minsize(900, 760)
        root.configure(background="#eef2f7")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name, root=root).configure(family="Microsoft YaHei UI", size=10)
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background="#ffffff")
        style.configure("Shell.TFrame", background="#eef2f7")
        style.configure("TLabel", background="#ffffff", foreground="#1e3048")
        style.configure("Muted.TLabel", foreground="#65768c", font=("Microsoft YaHei UI", 9))
        style.configure("LabelStrong.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Badge.TLabel", foreground="#42638b", background="#edf3fb", font=("Microsoft YaHei UI", 9), padding=(7, 4))
        style.configure("TButton", padding=(15, 9), background="#f1f5fa", foreground="#2c4564", borderwidth=0, focusthickness=0)
        style.map("TButton", background=[("disabled", "#f2f4f7"), ("active", "#e4ecf5")], foreground=[("disabled", "#9ba6b5")])
        style.configure("Primary.TButton", background="#2563d9", foreground="#ffffff")
        style.map("Primary.TButton", background=[("disabled", "#dce4ef"), ("active", "#1d4fb4")], foreground=[("disabled", "#8a99ad"), ("!disabled", "#ffffff")])
        style.configure("Danger.TButton", background="#fff0ef", foreground="#b63737")
        style.map("Danger.TButton", background=[("disabled", "#f2f4f7"), ("active", "#fce1df")], foreground=[("disabled", "#9ba6b5"), ("!disabled", "#b63737")])
        style.configure("TEntry", padding=(9, 7), bordercolor="#dce4ee", lightcolor="#dce4ee", darkcolor="#dce4ee", fieldbackground="#ffffff")
        style.map("TEntry", bordercolor=[("focus", "#5082e3")], fieldbackground=[("disabled", "#f6f8fb")])
        style.configure("TCheckbutton", background="#ffffff")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Status.TFrame", background="#eff5ff")
        style.configure("Status.TLabel", foreground="#3d6092", background="#eff5ff", font=("Microsoft YaHei UI", 9))
        style.configure("Slim.Horizontal.TProgressbar", thickness=3, background="#2563d9", troughcolor="#eaf0f8", borderwidth=0)
        style.configure("Treeview", rowheight=34, borderwidth=0, background="#ffffff", fieldbackground="#ffffff", foreground="#253b58", font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", padding=(9, 9), background="#edf2f8", foreground="#53667e", relief="flat", font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#1d4fa4")])
        style.configure("TNotebook", background="#eef2f7", borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(24, 12), background="#e6ecf4", foreground="#5d7089", borderwidth=0,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", "#ffffff"), ("active", "#f6f9fd")], foreground=[("selected", "#215bbd")],
                  padding=[("selected", (24, 12)), ("!selected", (24, 12))])
        header = tk.Frame(root, background="#142c45", padx=28, pady=14)
        header.pack(fill="x")
        title_area = tk.Frame(header, background="#142c45")
        title_area.pack(side="left")
        tk.Label(title_area, text="Codex 会话维护工具", background="#142c45", foreground="#ffffff", font=("Microsoft YaHei UI", 20, "bold"))\
            .pack(anchor="w", pady=(0, 3))
        tk.Label(title_area, text="清理本地残留 · 诊断远端写锁", background="#142c45", foreground="#b5c9df", font=("Microsoft YaHei UI", 10))\
            .pack(anchor="w")
        tk.Label(header, text=f"非官方工具  /  v{__version__}", background="#203f5c", foreground="#c8ddf0", font=("Microsoft YaHei UI", 9), padx=13, pady=8)\
            .pack(side="right", anchor="n", pady=5)
        outer = ttk.Frame(root, style="Shell.TFrame", padding=(22, 18, 22, 18))
        outer.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.local = LocalTab(self.notebook, self)
        self.remote = RemoteTab(self.notebook, self)
        self.notebook.add(self.local, text="本地残留清理")
        self.notebook.add(self.remote, text="远端写锁处理")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(60, self.poll)

    def submit(self, operation, callback):
        future = self.executor.submit(operation)

        def finished(done):
            try:
                value = done.result()
            except BaseException as error:
                self.events.put((callback, None, error))
            else:
                self.events.put((callback, value, None))

        future.add_done_callback(finished)

    def poll(self):
        if self.closed:
            return
        while True:
            try:
                callback, value, error = self.events.get_nowait()
            except queue.Empty:
                break
            callback(value, error)
        self.poll_id = self.root.after(60, self.poll)

    def close(self):
        if self.local.busy or self.remote.busy:
            messagebox.showinfo("操作进行中", "请等待检查或执行结果返回后关闭，避免丢失操作状态。", parent=self.root)
            return
        self.closed = True
        self.root.after_cancel(self.poll_id)
        if self.local.filter_timer:
            self.local.after_cancel(self.local.filter_timer)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    root = tk.Tk()
    MaintenanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
