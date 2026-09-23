# -*- coding: utf-8 -*-
"""launcher.py —— DeepSeek Harness 启动器（图形界面）。

用法：双击或在命令行运行 ``python launcher.py``。
只需 Windows + Python 3.8+（自带 tkinter），无任何第三方依赖。

界面功能：
- 显示本机 DSH 版本与最新版本，发现新版本时提示更新；
- 列出已安装插件（名称 / 版本 / 简介 / 项目主页），带开关；
- 一键启动 Harness，实时显示日志；启动失败时展示错误并允许关闭插件后重试。
"""
from __future__ import annotations

import collections
import datetime
import json
import queue
import re
import sys
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from tkinter import scrolledtext
from pathlib import Path

import theme
from browser import AppWindow, endpoint_label
from core import (
    WEB_URL,
    WEB_PORT,
    HarnessProcess,
    get_installed_version,
    get_latest_version,
    update_dsh,
    is_port_open,
    disabled_patch_path,
    dsh_available,
    dsh_cmd,
    stream_command,
    get_package_versions,
    launch_in_console,
    dsh_home,
    profile_dir,
    ui_settings_path,
    run_command,
    approve_builds_cmd,
    probe_url,
)
from plugins import (
    list_plugins,
    set_package_enabled,
    disabled_packages,
    write_disabled_patch,
    write_safe_mode_patch,
    set_disabled_map,
    ignored_build_packages,
    bundles_list,
    diagnose_boot_failure,
    Inventory,
)
from backup import (
    create_backup,
    list_backups,
    backup_summary,
    restore_backup,
    reset_profile,
    export_plugin_list,
    read_plugin_list,
    backups_root,
    save_last_good,
    load_last_good,
)

# 启动器自身的版本号（发布 Release 时与 git tag 对应）
__version__ = "1.2.1"

# 首次启动可能要走 npx 下载依赖，因此给足等待时间（秒）
READY_TIMEOUT = 180
# 日志面板保留的行数（也用于失败时展示错误摘要）
LOG_TAIL = 80
# 运行日志面板最多保留的行数：面板是一直累积的，长时间开着会无限吃内存，
# 超出后从顶部丢弃。修剪时一次多丢一些，避免每来一行就修剪一次。
# 注意：诊断报告与失败摘要用的是 LOG_TAIL 那个独立的小缓冲，不受这里影响。
LOG_PANEL_MAX_LINES = 5000
LOG_PANEL_TRIM_STEP = 500



def _fmt_time(iso: str) -> str:
    """把 npm 返回的 ISO 时间格式化成好读的本地时间。"""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:19].replace("T", " ")


# —— 界面设置（窗口大小位置、深浅色）的读写 ——
def _load_ui_settings() -> dict:
    try:
        data = json.loads(ui_settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_ui_settings(data: dict) -> None:
    try:
        ui_settings_path().parent.mkdir(parents=True, exist_ok=True)
        ui_settings_path().write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    except Exception:
        pass


def _resource_path(*parts) -> Path:
    """定位随程序一起分发的资源（图标等）。

    打包成单文件 exe 后，程序自己会被解压到临时目录 ``sys._MEIPASS``，
    资源在那里而不是源码旁边，所以两种情形都要照顾到。
    """
    base = getattr(sys, "_MEIPASS", None)
    root_dir = Path(base) if base else Path(__file__).resolve().parent
    return root_dir.joinpath(*parts)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.harness = HarnessProcess(log_callback=self._on_log_line)
        # 「自动最大化」是后台线程做的，日志必须走队列回主线程再写控件
        self.app_window = AppWindow(
            log=lambda msg: self._post(("log", f"[启动器] {msg}"))
        )
        self.ui_queue: queue.Queue = queue.Queue()
        self.log_tail: collections.deque = collections.deque(maxlen=LOG_TAIL)
        self.starting = False          # 是否正处于“启动→等待就绪”的过程
        self.power_state = "idle"      # idle / starting / running / stopped / failed
        self._dot_key = "muted"        # 状态灯当前用的调色板键
        self._spin_angle = 0           # 启动时那个"转圈"的当前角度
        self.plugin_vars = {}          # 包名 -> BooleanVar（开关）
        self.plugin_entry_ids = {}     # 包名 -> 条目 id 列表（渲染时缓存，供开关使用）
        self._ready_url = None         # DSH 启动成功后打印的带 token 访问地址
        self._current_plugins = []     # 最近一次渲染出来的插件列表（供“检查全部更新”使用）
        self._current_unmounted = []   # 最近一次渲染出来的“已安装但未挂载”列表
        self._last_cmd_output = []     # 最近一条后台命令的输出行（用于分析失败原因）
        self._bundles_before = []      # 命令执行前的插件层清单（用于判断“需要重启”）
        self._last_inventory = None    # 最近一次插件清点结果（换主题时重画用，不重新读档案）
        self._restart_deadline = 0.0   # 自动重启时等待端口释放的截止时间
        self._health_stop = True       # 健康检查线程是否该停
        self._health_fails = 0         # 连续失败次数
        self._health_state = "off"     # off / ok / bad / dead
        self._health_thread = None
        self._window_gone_reported = False   # 「服务正常但窗口没了」只提示一次
        self.ui_settings = _load_ui_settings()
        self.dark_mode = bool(self.ui_settings.get("dark", False))
        # 打开方式：True = 独立应用窗口（可随启动器关闭）；False = 默认浏览器标签页
        self.open_as_app = bool(self.ui_settings.get("open_as_app", True))
        self.dark_var = tk.BooleanVar(value=self.dark_mode)
        self.status_var = tk.StringVar(value="就绪")

        self.palette = theme.palette(self.dark_mode)
        self._build_ui()
        saved_geometry = self.ui_settings.get("geometry")
        if isinstance(saved_geometry, str) and saved_geometry:
            try:
                self.root.geometry(saved_geometry)   # 恢复上次的窗口大小 / 位置
            except Exception:
                pass
        self._apply_theme(self.dark_mode)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_queue)

        # 启动流程：先检查版本（后台），随后刷新插件列表
        self._set_status("正在检查版本…")
        threading.Thread(target=self._check_versions, args=(True,), daemon=True).start()

    # ------------------------------------------------------------------ #
    # 界面构建
    # ------------------------------------------------------------------ #
    def _set_window_icon(self) -> None:
        """设置窗口 / 任务栏图标（没有图标文件时安静跳过，不影响使用）。"""
        ico = _resource_path("assets", "launcher.ico")
        if ico.is_file():
            try:
                # default= 让之后新建的 Toplevel（各种对话框）也带上同一个图标
                self.root.iconbitmap(default=str(ico))
                return
            except Exception:
                pass
        png = _resource_path("assets", "launcher-256.png")
        if png.is_file():
            try:
                self._icon_image = tk.PhotoImage(file=str(png))
                self.root.iconphoto(True, self._icon_image)
            except Exception:
                pass

    def _build_ui(self) -> None:
        self.root.title(f"DeepSeek Harness 启动器  v{__version__}")
        self.root.geometry("920x800")
        self.root.minsize(800, 680)
        self._set_window_icon()

        # —— 主体：两个选项卡（原来的三个太碎，插件安装被隔到了别的页）——
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        self.nb = nb
        self._build_home_tab(nb)
        self._build_maintain_tab(nb)

        # —— 底部：状态栏 + 运行日志（切到哪个选项卡都看得到）——
        bar = ttk.Frame(self.root, style="Window.TFrame")
        bar.pack(fill="x", padx=14, pady=(0, 6))
        # 状态灯：平时是实心圆点，启动过程中变成转圈的弧（见 _draw_status_dot）
        self._status_dot = tk.Canvas(bar, width=16, height=16, highlightthickness=0, bd=0)
        self._status_dot.pack(side="left", pady=(1, 0))
        self._dot_id = self._status_dot.create_oval(2, 2, 14, 14, outline="", fill="#9AA1AC")
        ttk.Label(bar, textvariable=self.status_var, style="Window.TLabel").pack(
            side="left", padx=(8, 0))

        log_frame = ttk.LabelFrame(self.root, text="运行日志", padding=(10, 6))
        log_frame.pack(fill="both", padx=12, pady=(0, 12))
        log_head = ttk.Frame(log_frame)
        log_head.pack(fill="x")
        ttk.Checkbutton(log_head, text="深色模式", variable=self.dark_var,
                        command=self.on_toggle_dark).pack(side="left")
        ttk.Label(log_head, text=f"只保留最近 {LOG_PANEL_MAX_LINES} 行，"
                                 "要留档请先「日志另存为…」",
                  style="Muted.TLabel").pack(side="left", padx=(12, 0))
        ttk.Button(log_head, text="清空", style="Mini.TButton",
                   command=self.on_clear_log).pack(side="right")
        ttk.Button(log_head, text="日志另存为…", style="Mini.TButton",
                   command=self.on_save_log).pack(side="right", padx=(0, 6))
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=9, state="disabled", wrap="word",
            font=(theme.FONT_MONO, 9), relief="flat", bd=0,
        )
        self.log_text.pack(fill="both", expand=True, pady=(6, 0))

    # —— 选项卡一：主页（状态 + 启动控制 + 插件管理）——
    def _build_home_tab(self, nb) -> None:
        page = ttk.Frame(nb, style="Window.TFrame", padding=(4, 10, 4, 4))
        nb.add(page, text="  主页  ")

        card = ttk.LabelFrame(page, text="Harness", padding=(12, 10))
        card.pack(fill="x")
        top = ttk.Frame(card)
        top.pack(fill="x")
        self.lbl_installed = ttk.Label(top, text="本机版本：检测中…", style="Section.TLabel")
        self.lbl_installed.pack(side="left")
        self.lbl_latest = ttk.Label(top, text="最新版本：检测中…", style="Muted.TLabel")
        self.lbl_latest.pack(side="left", padx=(14, 0), pady=(4, 0))
        ttk.Button(top, text="检查更新", style="Mini.TButton",
                   command=self.manual_check_update).pack(side="right")

        act = ttk.Frame(card)
        act.pack(fill="x", pady=(12, 0))
        # 主按钮是三态的：启动 → 停止 → 恢复
        # 「停止」只停服务、浏览器窗口留着；停下之后按钮变成「恢复」。
        self.btn_power = ttk.Button(act, text="启动 Harness", style="Accent.TButton",
                                    command=self.on_power)
        self.btn_power.pack(side="left")
        self.btn_restart = ttk.Button(act, text="重启", style="Mini.TButton",
                                      command=self.on_restart, state="disabled")
        self.btn_restart.pack(side="left", padx=(8, 0))
        ttk.Button(act, text="打开界面", style="Mini.TButton",
                   command=self.open_browser).pack(side="left", padx=(8, 0))
        ttk.Button(act, text="安全模式启动", style="Mini.TButton",
                   command=self.on_safe_mode_launch).pack(side="left", padx=(8, 0))

        # 打开方式让用户自己选：独立窗口（启动器管得住、可随启动器关闭）
        # 还是默认浏览器的普通标签页（和平时上网一致，但启动器关不掉它）
        mode_row = ttk.Frame(card)
        mode_row.pack(fill="x", pady=(10, 0))
        self.open_mode_var = tk.BooleanVar(value=self.open_as_app)
        ttk.Checkbutton(mode_row, text="用独立窗口打开",
                        variable=self.open_mode_var,
                        command=self.on_open_mode_changed).pack(side="left")
        ttk.Label(mode_row, text="（勾选＝独立窗口，可随启动器一起关闭；"
                                 "取消＝默认浏览器的普通标签页，启动器关不掉它）",
                  style="Muted.TLabel").pack(side="left", padx=(8, 0))

        ttk.Label(card,
                  text="「停止」＝ 只停服务，浏览器窗口留着（按钮会变成「恢复」）；"
                       "「重启」＝ 停掉再启动，改完插件用它。\n"
                       "「安全模式启动」＝ 本次临时禁用全部第三方插件，"
                       "用来判断问题是不是插件引起的（不会改动你的开关设置）。",
                  style="Muted.TLabel", wraplength=830, justify="left").pack(anchor="w", pady=(10, 0))

        box = ttk.LabelFrame(page, text="插件", padding=(12, 10))
        box.pack(fill="both", expand=True, pady=(12, 0))

        head = ttk.Frame(box)
        head.pack(fill="x")
        ttk.Button(head, text="安装插件…", command=self.on_install_plugin).pack(side="left")
        ttk.Button(head, text="检查全部更新", command=self.on_check_all_updates).pack(side="left", padx=(8, 0))
        ttk.Button(head, text="刷新", command=self.refresh_plugins).pack(side="left", padx=(8, 0))
        ttk.Button(head, text="导入并重装…", style="Mini.TButton",
                   command=self.on_import_plugin_list).pack(side="right")
        ttk.Button(head, text="导出清单…", style="Mini.TButton",
                   command=self.on_export_plugin_list).pack(side="right", padx=(0, 8))
        ttk.Label(box, text="勾选 = 启用；点「版本…」可看全部版本，升级或回退都行。"
                            "安装、更新、清单导入导出都在这一页，装完直接出现在下方。",
                  style="Muted.TLabel", wraplength=830, justify="left").pack(anchor="w", pady=(8, 8))

        listwrap = ttk.Frame(box)
        listwrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(listwrap, highlightthickness=0, bd=0)
        self._plugin_canvas = canvas
        # Canvas 不会自动处理鼠标滚轮，必须显式绑定（见 _on_plugin_wheel）
        canvas.bind("<MouseWheel>", self._on_plugin_wheel)
        scrollbar = ttk.Scrollbar(listwrap, orient="vertical", command=canvas.yview)
        self.plugin_frame = ttk.Frame(canvas)
        self.plugin_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        self._plugin_window = canvas.create_window((0, 0), window=self.plugin_frame, anchor="nw")
        # 让内层跟着画布一起变宽，插件行才会铺满而不是挤在左边
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(self._plugin_window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # —— 选项卡二：维护（备份 / 恢复 / 诊断 / 工具）——
    def _build_maintain_tab(self, nb) -> None:
        page = ttk.Frame(nb, style="Window.TFrame", padding=(4, 10, 4, 4))
        nb.add(page, text="  维护  ")

        b1 = ttk.LabelFrame(page, text="备份与恢复", padding=(12, 10))
        b1.pack(fill="x")
        row1 = ttk.Frame(b1)
        row1.pack(fill="x")
        ttk.Button(row1, text="立即备份…", command=self.on_manual_backup).pack(side="left")
        ttk.Button(row1, text="恢复备份…", command=self.on_restore_backup).pack(side="left", padx=(8, 0))
        ttk.Button(row1, text="重置档案…", command=self.on_reset_profile).pack(side="left", padx=(8, 0))
        ttk.Label(b1, text="重置前会自动备份；恢复前也会自动备份当前状态，随时可回退。",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

        b2 = ttk.LabelFrame(page, text="已有备份", padding=(12, 10))
        b2.pack(fill="both", expand=True, pady=(12, 0))
        listwrap = ttk.Frame(b2)
        listwrap.pack(fill="both", expand=True)
        self.backup_list = tk.Listbox(listwrap, height=7, relief="flat", bd=0,
                                      activestyle="none", highlightthickness=0)
        sb = ttk.Scrollbar(listwrap, orient="vertical", command=self.backup_list.yview)
        self.backup_list.configure(yscrollcommand=sb.set)
        self.backup_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        ttk.Button(b2, text="刷新备份列表", style="Mini.TButton",
                   command=self.refresh_backup_list).pack(anchor="e", pady=(8, 0))
        self.refresh_backup_list()

        b3 = ttk.LabelFrame(page, text="诊断与工具", padding=(12, 10))
        b3.pack(fill="x", pady=(12, 0))
        row3 = ttk.Frame(b3)
        row3.pack(fill="x")
        ttk.Button(row3, text="导出诊断报告…", command=self.on_export_diagnostics).pack(side="left")
        ttk.Button(row3, text="运行命令…", command=self.on_run_command).pack(side="left", padx=(8, 0))
        ttk.Button(row3, text="安装 / 修复 DSH", command=self.on_install_dsh).pack(side="left", padx=(8, 0))
        ttk.Button(row3, text="手动启动（命令行）", style="Mini.TButton",
                   command=self.on_manual_launch).pack(side="left", padx=(8, 0))
        ttk.Label(b3, text="「导出诊断报告」把版本、路径、插件清单和最近日志写成一个 txt，"
                           "求助时可直接发给别人；「安装 / 修复 DSH」重装 DSH 程序本身。",
                  style="Muted.TLabel", wraplength=830, justify="left").pack(anchor="w", pady=(8, 8))
        self.env_text = tk.Text(b3, height=5, wrap="word", relief="flat", bd=0,
                                font=(theme.FONT_MONO, 9))
        self.env_text.pack(fill="both", expand=True)
        self.env_text.insert("1.0", "\n".join([
            f"DSH 家目录：{dsh_home()}",
            f"web 档案目录：{profile_dir()}",
            f"备份目录：{backups_root()}",
            f"调用的 dsh 命令：{dsh_cmd()}",
        ]))
        self.env_text.configure(state="disabled")

    def refresh_backup_list(self) -> None:
        """刷新“已有备份”列表。"""
        if not hasattr(self, "backup_list"):
            return
        self.backup_list.delete(0, "end")
        try:
            for b in list_backups():
                self.backup_list.insert("end", backup_summary(b))
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _append_log(self, text: str) -> None:
        # 每行前面加时间戳：事后排查时，没有时间戳就只能靠内容猜顺序
        stamp = datetime.datetime.now().strftime("[%H:%M:%S] ")
        self.log_text.configure(state="normal")
        for line in str(text).split("\n"):
            self.log_text.insert("end", stamp + line + "\n")
        self._trim_log_panel()
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _trim_log_panel(self) -> None:
        """把日志面板裁到上限以内（从顶部丢弃最旧的若干行）。

        面板里的内容是一直累积的，长跑一天能上万行；不裁剪既吃内存，
        也会让 Tk 的文本控件越来越慢。这里只在**超过上限**时修剪，
        并且一次多丢一点（留出 LOG_PANEL_TRIM_STEP 的余量），
        免得每追加一行都要删一次。
        """
        try:
            lines = int(self.log_text.index("end-1c").split(".")[0])
        except Exception:
            return
        if lines <= LOG_PANEL_MAX_LINES:
            return
        keep = max(1, LOG_PANEL_MAX_LINES - LOG_PANEL_TRIM_STEP)
        try:
            self.log_text.delete("1.0", f"{lines - keep + 1}.0")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 线程安全：后台线程 -> 队列 -> 主线程
    # ------------------------------------------------------------------ #
    def _post(self, item) -> None:
        self.ui_queue.put(item)

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.ui_queue.get_nowait()
                self._handle(item)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle(self, item) -> None:
        tag = item[0]
        if tag == "versions":
            self._on_versions(item[1], item[2], item[3])
        elif tag == "update_done":
            self._on_update_done(item[1])
        elif tag == "plugins":
            self._render_plugins(item[1])
        elif tag == "plugins_error":
            self._set_status("读取插件列表失败，请检查网络后重试")
            self._append_log("读取插件列表失败：" + str(item[1]))
        elif tag == "update_info":
            self._show_update_info(item[1], item[2], item[3])
        elif tag == "all_update_info":
            self._show_all_update_info(item[1])
        elif tag == "backup_done":
            self._on_backup_done(item[1], item[2])
        elif tag == "reset_done":
            self._on_reset_done(item[1], item[2], item[3])
        elif tag == "restore_done":
            self._on_restore_done(item[1], item[2], item[3])
        elif tag == "import_info":
            self._show_import_dialog(item[1])
        elif tag == "diag_done":
            self._on_diag_done(item[1], item[2])
        elif tag == "toggle_error":
            messagebox.showwarning("操作失败", str(item[1]))
        elif tag == "toggled":
            self._set_status(f"已{item[2]}：{item[1]}")
        elif tag == "toggle_failed":
            # item = (tag, name, enabled, var, msg)；把开关状态回滚
            name, enabled, var, msg = item[1], item[2], item[3], item[4]
            var.set(not enabled)
            messagebox.showwarning("操作失败", f"{name}：{msg}")
        elif tag == "log":
            self._append_log(item[1])
        elif tag == "cmd_done":
            # item = (tag, label, code, on_done)
            label, code, on_done = item[1], item[2], item[3]
            self._append_log(f"[启动器] 命令结束（退出码 {code}）：{label}")
            self._set_status(f"{label} — 结束（退出码 {code}）")
            if on_done is not None:
                on_done(code)
        elif tag == "launch_ready":
            self._on_launch_ready(item[1])
        elif tag == "launch_failed":
            self._on_launch_failed(item[1])
        elif tag == "launch_timeout":
            self._on_launch_timeout()
        elif tag == "stopped":
            self._on_stopped()
        elif tag == "health":
            # item = (tag, "ok"|"bad"|"dead", 说明)
            self._on_health(item[1], item[2])

    def _on_log_line(self, line: str) -> None:
        # 运行在后台读线程里：只入队，不碰界面
        # 先做「就绪地址」匹配，再加时间戳——匹配用的是行首锚定，
        # 加了前缀就匹配不上了。
        m = re.match(r"^dsh web:\s+(\S+)", line)
        if m and self._ready_url is None:
            self._ready_url = m.group(1)
        # DSH 启动成功后会用这行打印“带 token 的认证地址”，
        # 例如：dsh web: http://127.0.0.1:3080/?token=xxx
        # 这才是浏览器真正要打开的地址（裸地址会被 401 拒绝）。
        stamp = datetime.datetime.now().strftime("[%H:%M:%S] ")
        self.log_tail.append(stamp + line)
        self._post(("log", line))

    # ------------------------------------------------------------------ #
    # 版本检查与更新
    # ------------------------------------------------------------------ #
    def _check_versions(self, auto: bool) -> None:
        installed = get_installed_version()
        latest = get_latest_version()
        self._post(("versions", installed, latest, auto))

    def manual_check_update(self) -> None:
        self._set_status("正在检查更新…")
        threading.Thread(target=self._check_versions, args=(False,), daemon=True).start()

    def _on_versions(self, installed, latest, auto) -> None:
        self.lbl_installed.config(text=f"本机版本：{installed or '未知'}")
        self.lbl_latest.config(text=f"最新版本：{latest or '未知'}")
        has_update = bool(installed and latest and installed != latest)
        if auto and has_update:
            # 需求：发现新版本时提示更新；用户取消则关闭启动器
            self._show_update_dialog(installed, latest)
        elif auto:
            self._set_status("版本检查完成")
        else:
            if has_update:
                messagebox.showinfo("发现新版本", f"发现新版本 {latest}（当前 {installed}）。\n可通过“检查更新”按钮手动更新。")
                self._set_status(f"有新版本 {latest} 可用")
            else:
                messagebox.showinfo("检查更新", "当前已是最新版本。")
                self._set_status("已是最新版本")
        # 无论是否有更新，都把插件列表刷出来
        self.refresh_plugins()

    def _show_update_dialog(self, installed, latest) -> None:
        dlg = tk.Toplevel(self.root)
        theme.paint_dialog(dlg, self.palette)
        dlg.title("发现新版本")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"发现新版本 {latest}（当前 {installed}）。", font=("Microsoft YaHei UI", 11)).pack(anchor="w")
        ttk.Label(frm, text="是否立即更新？选择“取消”将关闭启动器。", wraplength=340).pack(anchor="w", pady=(6, 14))
        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Button(row, text="立即更新", command=lambda: self._on_choose_update(dlg)).pack(side="left")
        ttk.Button(row, text="取消", command=lambda: self._on_choose_cancel(dlg)).pack(side="left", padx=(10, 0))

    def _on_choose_update(self, dlg) -> None:
        dlg.destroy()
        self._set_status("正在更新 DeepSeek Harness…")
        threading.Thread(target=self._update_thread, daemon=True).start()

    def _on_choose_cancel(self, dlg) -> None:
        dlg.destroy()
        self.root.destroy()

    def _update_thread(self) -> None:
        ok = update_dsh()
        self._post(("update_done", ok))

    def _on_update_done(self, ok) -> None:
        if ok:
            self._set_status("更新完成，正在重新检查版本…")
            threading.Thread(target=self._check_versions, args=(False,), daemon=True).start()
        else:
            messagebox.showerror("更新失败", "更新失败，请检查网络后重试。")
            self._set_status("更新失败")
            self.refresh_plugins()

    # ------------------------------------------------------------------ #
    # 插件列表
    # ------------------------------------------------------------------ #
    def refresh_plugins(self) -> None:
        self._set_status("正在读取插件列表…")
        threading.Thread(target=self._refresh_thread, daemon=True).start()

    def _refresh_thread(self) -> None:
        try:
            inv = list_plugins()
            self._post(("plugins", inv))
        except Exception as exc:  # noqa: BLE001
            self._post(("plugins_error", exc))

    def _clear_plugin_frame(self) -> None:
        for child in self.plugin_frame.winfo_children():
            child.destroy()
        self.plugin_vars.clear()

    def _render_plugins(self, inv) -> None:
        self._clear_plugin_frame()
        self._last_inventory = inv
        plugins = list(inv.plugins)
        unmounted = list(inv.unmounted)
        libraries = list(inv.libraries)
        self._current_plugins = plugins
        self._current_unmounted = unmounted
        if not plugins and not unmounted and not libraries:
            ttk.Label(self.plugin_frame,
                      text="暂未安装任何插件。\n可以在上面的「安装插件…」里装，装好后点「刷新」。",
                      style="Muted.TLabel").pack(anchor="w", pady=10)
        # --dump-config 失败 ⇒ 所有条目 id 为空 ⇒ 开关会静默失灵。
        # 这种情况必须显性告警，绝不能和「确实没有条目」长得一样。
        if inv.dump_error:
            warn = ttk.Frame(self.plugin_frame, padding=(6, 6))
            warn.pack(fill="x", pady=(0, 6))
            ttk.Label(
                warn,
                text="⚠ 无法解析插件条目，下面的「开关」暂时不可用。",
                font=("Microsoft YaHei UI", 10, "bold"), style="Danger.TLabel",
            ).pack(anchor="w")
            ttk.Label(
                warn,
                text=f"原因：{inv.dump_error}\n"
                     "常见于档案目录不可写（权限、只读、被安全软件或其它进程占用）。\n"
                     "排查后可点「刷新」重试；详细输出见下方运行日志。",
                style="Warn.TLabel", justify="left", wraplength=760,
            ).pack(anchor="w")
            self._append_log(f"[启动器] 警告：导出插件条目失败 —— {inv.dump_error}")
        self.plugin_entry_ids = {p.name: p.entry_ids for p in plugins}
        for index, p in enumerate(plugins):
            if index:
                # 行与行之间加一条极淡的分隔线，列表才不像一坨
                ttk.Separator(self.plugin_frame, orient="horizontal").pack(fill="x")
            self._render_plugin_row(p)
        if unmounted:
            ttk.Separator(self.plugin_frame, orient="horizontal").pack(fill="x", pady=8)
            ttk.Label(
                self.plugin_frame,
                text="⚠ 已安装但未挂载（声明了插件层却没登记，因此不会生效）：",
                font=("Microsoft YaHei UI", 10, "bold"), style="Danger.TLabel",
            ).pack(anchor="w")
            ttk.Label(
                self.plugin_frame,
                text="点右边的「修复」即可：放行被拦下的构建脚本，再重新登记一次。",
                style="Warn.TLabel",
            ).pack(anchor="w")
            for p in unmounted:
                self._render_unmounted_row(p)
        if libraries:
            ttk.Separator(self.plugin_frame, orient="horizontal").pack(fill="x", pady=8)
            ttk.Label(self.plugin_frame, text="库依赖（非插件层，不影响启动，不可开关）：",
                      style="Muted.TLabel").pack(anchor="w")
            for p in libraries:
                self._render_library_row(p)
        # 行内子控件会"吃掉"滚轮事件，所以要给它们逐个补绑
        self._bind_wheel(self.plugin_frame)
        parts = [f"共 {len(plugins)} 个插件"]
        if unmounted:
            parts.append(f"{len(unmounted)} 个未挂载")
        parts.append(f"{len(libraries)} 个库依赖")
        self._set_status("，".join(parts))


    # —— 鼠标滚轮：Canvas 不会自动响应，必须手动处理 ——
    def _on_plugin_wheel(self, event) -> None:
        """滚动插件列表。Windows 下每格滚轮 delta 为 ±120。"""
        canvas = getattr(self, "_plugin_canvas", None)
        if canvas is None:
            return
        delta = getattr(event, "delta", 0)
        steps = int(-delta / 120)
        if steps == 0:                       # 有些鼠标 delta 很小，兜底为 1 格
            steps = -1 if delta > 0 else 1
        try:
            canvas.yview_scroll(steps, "units")
        except Exception:
            pass

    def _bind_wheel(self, widget) -> None:
        """递归给控件及其所有子控件绑定滚轮事件。"""
        try:
            widget.bind("<MouseWheel>", self._on_plugin_wheel)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _render_plugin_row(self, p) -> None:
        row = ttk.Frame(self.plugin_frame, padding=(2, 6))
        row.pack(fill="x", pady=3)
        var = tk.BooleanVar(value=p.enabled)
        self.plugin_vars[p.name] = var
        cb = ttk.Checkbutton(
            row, variable=var,
            command=lambda name=p.name, v=var: self._on_toggle(name, v),
        )
        cb.pack(side="left", anchor="n")
        btn_bar = ttk.Frame(row)
        btn_bar.pack(side="right", anchor="n")
        ttk.Button(
            btn_bar, text="卸载…", width=7,
            command=lambda name=p.name: self.on_uninstall_plugin(name),
        ).pack(side="right")
        ttk.Button(
            btn_bar, text="版本…", width=7,
            command=lambda name=p.name, ver=p.version: self.on_check_update(name, ver),
        ).pack(side="right", padx=(0, 6))

        info = ttk.Frame(row)
        info.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Label(info, text=f"{p.name}   v{p.version}", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        if p.description:
            ttk.Label(info, text=p.description, wraplength=540, style="Desc.TLabel").pack(anchor="w")
        if p.homepage:
            link = ttk.Label(info, text="项目主页 ↗", style="Link.TLabel", cursor="hand2")
            link.pack(anchor="w")
            link.bind("<Button-1>", lambda e, url=p.homepage: webbrowser.open(url))
        if not p.entry_ids:
            ttk.Label(info, text="⚠ 未能定位该插件的启动条目，可能无法正常开关（请先“刷新列表”）",
                      style="Warn.TLabel").pack(anchor="w")

    def _render_library_row(self, p) -> None:
        ttk.Label(self.plugin_frame, text=f"• {p.name}  v{p.version}",
                  style="Muted.TLabel", padding=(20, 1)).pack(anchor="w")

    def _render_unmounted_row(self, p) -> None:
        """渲染一个「已安装但未挂载」的插件：只给「修复」和「卸载」。"""
        row = ttk.Frame(self.plugin_frame, padding=(2, 6))
        row.pack(fill="x", pady=3)
        btn_bar = ttk.Frame(row)
        btn_bar.pack(side="right", anchor="n")
        ttk.Button(
            btn_bar, text="卸载…", width=7,
            command=lambda name=p.name: self.on_uninstall_plugin(name),
        ).pack(side="right")
        ttk.Button(
            btn_bar, text="修复", width=7,
            command=lambda name=p.name: self.on_repair_plugin(name),
        ).pack(side="right", padx=(0, 6))

        info = ttk.Frame(row)
        info.pack(side="left", fill="x", expand=True)
        ttk.Label(info, text=f"{p.name}   v{p.version}",
                  font=("Microsoft YaHei UI", 10, "bold"), style="Danger.TLabel").pack(anchor="w")
        ttk.Label(
            info,
            text="它自己声明了插件层，但没有出现在档案的插件层清单里，"
                 "所以 Harness 根本不会加载它（装了也不生效）。",
            wraplength=540, style="Warn.TLabel", justify="left",
        ).pack(anchor="w")
        if p.description:
            ttk.Label(info, text=p.description, wraplength=540, style="Desc.TLabel").pack(anchor="w")
        if p.homepage:
            link = ttk.Label(info, text="项目主页 ↗", style="Link.TLabel", cursor="hand2")
            link.pack(anchor="w")
            link.bind("<Button-1>", lambda e, url=p.homepage: webbrowser.open(url))

    def _on_toggle(self, name: str, var: tk.BooleanVar) -> None:
        enabled = var.get()
        entry_ids = self.plugin_entry_ids.get(name, [])
        self._set_status(f"正在{'启用' if enabled else '禁用'} {name} …")
        threading.Thread(target=self._toggle_thread, args=(name, enabled, entry_ids, var), daemon=True).start()

    def _toggle_thread(self, name: str, enabled: bool, entry_ids: list, var: tk.BooleanVar) -> None:
        try:
            ok = set_package_enabled(name, enabled, entry_ids)
            if ok:
                self._post(("log", f"[启动器] 已{'启用' if enabled else '禁用'}插件：{name}"))
                self._post(("toggled", name, "启用" if enabled else "禁用"))
            else:
                self._post(("toggle_failed", name, enabled, var,
                            "未能定位它的启动条目，请先点击“刷新列表”。"))
        except Exception as exc:  # noqa: BLE001
            self._post(("toggle_failed", name, enabled, var, str(exc)))

    # ------------------------------------------------------------------ #
    # 插件更新：检查新版本 → 选版本 → 用显式版本号安装
    # ------------------------------------------------------------------ #
    def on_check_update(self, pkg: str, current: str) -> None:
        """检查某个插件在 npm 上有没有新版本。"""
        self._set_status(f"正在检查 {pkg} 的更新…")
        threading.Thread(target=self._check_update_thread, args=(pkg, current), daemon=True).start()

    def _check_update_thread(self, pkg: str, current: str) -> None:
        versions = get_package_versions(pkg)
        self._post(("update_info", pkg, current, versions))

    def on_check_all_updates(self) -> None:
        """一次检查所有已安装插件的更新情况。"""
        names = [(p.name, p.version) for p in self._current_plugins]
        if not names:
            messagebox.showinfo("检查全部更新", "当前没有已安装的插件。")
            return
        self._set_status(f"正在检查 {len(names)} 个插件的更新…")
        threading.Thread(target=self._check_all_thread, args=(names,), daemon=True).start()

    def _check_all_thread(self, names: list) -> None:
        results = []
        for name, ver in names:
            versions = get_package_versions(name)
            results.append({
                "name": name,
                "current": ver,
                "latest": versions[0][0] if versions else "",
                "found": bool(versions),
            })
        self._post(("all_update_info", results))

    def _show_all_update_info(self, results: list) -> None:
        outdated = [r for r in results
                    if r["found"] and r["latest"] and r["latest"] != r["current"]]
        lines = []
        for r in results:
            if not r["found"]:
                lines.append(f"• {r['name']}：查询失败（可能不是 npm 上的包）")
            elif r["latest"] == r["current"]:
                lines.append(f"• {r['name']}：已是最新（{r['current']}）")
            else:
                lines.append(f"• {r['name']}：{r['current']} → {r['latest']}")
        self._set_status(f"检查完成：{len(outdated)} / {len(results)} 个插件有新版本")
        if not outdated:
            messagebox.showinfo("检查全部更新", "全部插件都已是最新版本。\n\n" + "\n".join(lines))
            return
        if messagebox.askyesno(
            "检查全部更新",
            f"{len(outdated)} 个插件有新版本：\n\n" + "\n".join(lines) +
            "\n\n是否全部更新到最新版本？",
        ):
            self._start_import_install(
                [{"name": r["name"], "target": r["latest"]} for r in outdated])

    def _show_update_info(self, pkg: str, current: str, versions: list) -> None:
        if not versions:
            self._set_status("检查更新失败")
            messagebox.showinfo(
                "检查更新",
                f"无法查询 {pkg} 的版本信息。\n可能它不是 npm 上的包，或者网络不通。",
            )
            return
        newest = versions[0][0]
        if newest == current:
            self._set_status(f"{pkg} 已是最新版本 {current}（仍可切换其他版本）")
        else:
            self._set_status(f"{pkg} 有新版本 {newest}（当前 {current}）")
        # 无论是否已是最新，都打开版本选择框——这样用户也能“回退到旧版本”
        self._show_version_picker(pkg, current, versions)

    def _show_version_picker(self, pkg: str, current: str, versions: list) -> None:
        """弹出版本选择框：列出可用版本，让用户挑一个安装。"""
        newest = versions[0][0]
        dlg = tk.Toplevel(self.root)
        theme.paint_dialog(dlg, self.palette)
        dlg.title("插件版本")
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=pkg, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(frm, text=f"当前版本：{current}        最新版本：{newest}").pack(anchor="w", pady=(4, 4))
        if newest == current:
            ttk.Label(frm, text="当前已是最新版本。你仍然可以选择下面的任意版本进行「回退」。",
                      style="Ok.TLabel").pack(anchor="w", pady=(0, 8))
        else:
            ttk.Label(frm, text=f"有新版本可用：{newest}",
                      style="Ok.TLabel").pack(anchor="w", pady=(0, 8))

        tree = ttk.Treeview(frm, columns=("version", "time", "note"),
                            show="headings", height=10, selectmode="browse")
        tree.heading("version", text="版本")
        tree.heading("time", text="发布时间")
        tree.heading("note", text="说明")
        tree.column("version", width=130, anchor="w")
        tree.column("time", width=150, anchor="w")
        tree.column("note", width=110, anchor="w")
        tree.pack(fill="both", expand=True)

        for ver, stamp in versions:
            if ver == current:
                note = "← 当前"
            elif ver == newest:
                note = "← 最新"
            else:
                note = ""
            tree.insert("", "end", values=(ver, _fmt_time(stamp), note))

        children = tree.get_children()
        if children:
            tree.selection_set(children[0])   # 默认选中最新版
            tree.focus(children[0])

        ttk.Label(
            frm,
            text="说明：pnpm 默认会跳过发布时间不满 24 小时的版本（供应链保护）。\n"
                 "这里用「显式版本号」安装，选中哪一版就装哪一版，升级、回退都不受该限制。\n"
                 "更新插件会改动 Harness 正在使用的文件，建议先停止 Harness。",
            style="Warn.TLabel", justify="left",
        ).pack(anchor="w", pady=(8, 6))

        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(6, 0))

        def do_update() -> None:
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个版本。")
                return
            ver = tree.item(sel[0], "values")[0]
            dlg.destroy()
            self._start_plugin_update(pkg, ver)

        ttk.Button(row, text="安装选中版本", command=do_update).pack(side="left")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="right")

    def _start_plugin_update(self, pkg: str, version: str) -> None:
        """把插件更新到指定版本（显式版本号，绕开 pnpm 的发布年龄限制）。"""
        if self.harness.is_running():
            choice = messagebox.askyesnocancel(
                "更新插件",
                "Harness 正在运行中。更新插件会改动它的文件，建议先停止以免文件被占用。\n\n"
                "「是」＝ 先停止 Harness 再更新（推荐）\n"
                "「否」＝ 不停止，直接更新\n"
                "「取消」＝ 放弃本次更新",
            )
            if choice is None:
                return
            if choice:
                self.on_stop()
        cmd = f'{dsh_cmd()} plugin --profile web add "{pkg}@{version}"'
        self._run_async(cmd, f"更新插件 {pkg} → {version}",
                        on_done=lambda code: self._after_plugin_update(code, pkg))

    def _after_plugin_update(self, code: int, pkg: str = "") -> None:
        if code == 0:
            self._finish_plugin_change("插件更新完成")
            return
        if self._maybe_offer_repair(pkg, "更新"):
            return
        messagebox.showerror(
            "失败",
            f"插件更新失败（退出码 {code}）。\n\n"
            f"命令最后几行输出：\n{self._command_output_tail() or '（无）'}\n\n"
            "完整输出见下方「运行日志」。",
        )
        self.refresh_plugins()


    # ------------------------------------------------------------------ #
    # 彻底删除插件
    # ------------------------------------------------------------------ #
    def on_uninstall_plugin(self, pkg: str) -> None:
        """彻底删除某个插件：移除它的依赖与文件（不可撤销）。"""
        if self.harness.is_running():
            if not messagebox.askyesno(
                "卸载插件",
                "Harness 正在运行中，卸载会删除它正在使用的文件。\n\n"
                "是否先停止 Harness？",
            ):
                return
            self.on_stop()
        if not messagebox.askyesno(
            "彻底删除插件",
            f"将从 web 档案中彻底删除：\n\n    {pkg}\n\n"
            "这会移除它的依赖与文件，之后启动器列表里也不会再有它。\n"
            "（删除前会自动备份一次档案，日后可用「恢复备份…」找回配置）\n\n"
            "确定要删除吗？",
        ):
            return
        try:                                  # 先留一份“后悔药”
            create_backup(label=f"删除 {pkg} 前自动备份", include_sessions=False)
            self.refresh_backup_list()
        except Exception:
            pass
        cmd = f'{dsh_cmd()} plugin --profile web remove "{pkg}"'
        self._run_async(cmd, f"删除插件 {pkg}",
                        on_done=lambda code: self._after_uninstall(code, pkg))

    def _after_uninstall(self, code: int, pkg: str) -> None:
        if code == 0:
            try:
                set_package_enabled(pkg, True)   # 从启动器的“已禁用”记录里清掉它
            except Exception:
                pass
            messagebox.showinfo("完成", f"{pkg} 已彻底删除，正在刷新列表。")
        else:
            messagebox.showerror("失败", f"删除失败（退出码 {code}），请查看下方日志。")
        self.refresh_plugins()

    # ------------------------------------------------------------------ #
    # 工具：在启动器里直接执行常用命令（不用另外开命令行）
    # ------------------------------------------------------------------ #
    def _run_async(self, cmd: str, label: str, on_done=None) -> None:
        """后台运行一条命令，输出实时打到日志面板；结束（主线程）回调 on_done(退出码)。

        同时做两件对“事后判断”很关键的事：

        - 把输出**留一份**在 ``self._last_cmd_output``，这样失败回调能拿到真实原因
          （例如 pnpm 的 ``ERR_PNPM_IGNORED_BUILDS``），而不是只有一个退出码；
        - 记下执行前的插件层清单，结束后一对比就知道“是否需要重启 Harness”。
        """
        self._append_log(f"[启动器] 运行命令：{cmd}")
        self._set_status(f"正在执行：{label} …")
        self._last_cmd_output = []
        try:
            self._bundles_before = bundles_list()
        except Exception:
            self._bundles_before = []

        def worker():
            lines = []

            def on_line(line: str) -> None:
                lines.append(line)
                self._post(("log", line))

            code = stream_command(cmd, on_line)
            self._last_cmd_output = lines
            self._post(("cmd_done", label, code, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _command_output_tail(self, limit: int = 6) -> str:
        """把最近一条命令的尾部输出拼成一行，用于错误提示里给点线索。"""
        lines = [ln.strip() for ln in (self._last_cmd_output or []) if ln.strip()]
        return "\n".join(lines[-limit:])

    def on_install_dsh(self) -> None:
        """安装 / 修复全局 DSH（npm install -g @deepseek-ai/dsh@latest）。"""
        if not messagebox.askyesno(
            "安装 / 修复 全局 DSH",
            "将执行：\nnpm install -g @deepseek-ai/dsh@latest\n\n"
            "用于首次安装，或修复启动文件损坏的情况。继续吗？",
        ):
            return
        self._run_async("npm install -g @deepseek-ai/dsh@latest", "安装/修复 全局 DSH",
                        on_done=self._after_install_dsh)

    def _after_install_dsh(self, code: int) -> None:
        if code == 0:
            messagebox.showinfo("完成", "全局 DSH 已安装 / 修复完成，正在重新检查版本。")
            self._set_status("正在重新检查版本…")
            threading.Thread(target=self._check_versions, args=(False,), daemon=True).start()
        else:
            messagebox.showerror("失败", f"安装失败（退出码 {code}），请查看下方日志。")

    def on_install_plugin(self) -> None:
        """安装插件：dsh plugin --profile web add <包名>。"""
        pkg = simpledialog.askstring(
            "安装插件",
            "请输入插件包名（例如 dsh-better-sidebar 或 @scope/name）：",
            parent=self.root,
        )
        if not pkg or not pkg.strip():
            return
        pkg = pkg.strip()
        self._run_async(f'{dsh_cmd()} plugin --profile web add "{pkg}"',
                        f"安装插件 {pkg}",
                        on_done=lambda code: self._after_install_plugin(code, pkg))

    def _after_install_plugin(self, code: int, pkg: str = "") -> None:
        if code == 0:
            self._finish_plugin_change("插件安装完成")
            return
        if self._maybe_offer_repair(pkg, "安装"):
            return
        messagebox.showerror(
            "失败",
            f"插件安装失败（退出码 {code}）。\n\n"
            f"命令最后几行输出：\n{self._command_output_tail() or '（无）'}\n\n"
            "完整输出见下方「运行日志」。",
        )
        self.refresh_plugins()

    # ------------------------------------------------------------------ #
    # 「已安装但未挂载」的修复 + 插件层变动后的重启提示
    # ------------------------------------------------------------------ #
    def _maybe_offer_repair(self, pkg: str, action: str) -> bool:
        """判断这次失败是不是「pnpm 拦下构建脚本」这一特定原因。

        是的话给出可操作提示（并可一键修复），返回 True；
        不是（或拿不到包名）返回 False，由调用方弹通用的失败提示。
        """
        blocked = ignored_build_packages(self._last_cmd_output)
        if not blocked or not pkg:
            return False
        if messagebox.askyesno(
            f"{action}没有完成 —— 发现可修复的原因",
            f"{action}没有完成：pnpm 拦下了依赖的安装脚本。\n\n"
            "    被拦下的包：" + "、".join(blocked) + "\n\n"
            "pnpm 11 默认开启 strictDepBuilds（供应链保护）：碰到带原生依赖的包时，\n"
            "它会拒绝执行这些包的安装脚本，并让命令以非 0 退出。\n\n"
            "这里有个关键点：pnpm 在报错**之前**就已经把插件写进了档案的依赖清单、\n"
            "文件也放进 node_modules 了，而 Harness 只在命令成功时才登记「插件层」。\n"
            "于是它会停在「已安装但未挂载」的半成品状态——列表里看得到，却完全不生效。\n\n"
            "是否现在自动修复？",
        ):
            self.on_repair_plugin(pkg, blocked)
        else:
            self.refresh_plugins()
        return True

    def on_repair_plugin(self, pkg: str, blocked: list | None = None) -> None:
        """一键修复「已安装但未挂载」：放行构建脚本 → 重新登记插件层。"""
        blocked = list(blocked or [])
        detail = ("被拦下的构建脚本：" + "、".join(blocked) + "\n\n") if blocked else ""
        if not messagebox.askyesno(
            "修复插件登记",
            f"将修复：{pkg}\n\n"
            f"{detail}"
            "启动器会依次执行两步（都在 web 档案目录里，不改动 Harness 自己的配置文件）：\n\n"
            "  1. pnpm approve-builds --all\n"
            "     放行被拦下的依赖安装脚本（把档案目录 pnpm-workspace.yaml 里\n"
            "     allowBuilds 的占位值 \"set this to true or false\" 改成 true）。\n"
            "     安全提示：这等于信任这些包在安装时执行的脚本；\n"
            "     插件不带原生依赖时不会出现这一步，也就不需要它。\n\n"
            f"  2. dsh plugin --profile web add {pkg}\n"
            "     重新走一遍安装，让 Harness 把插件层登记上。\n\n"
            "修复后需要重启 Harness 才会真正生效。继续吗？",
        ):
            return
        if self.harness.is_running():
            if not messagebox.askyesno(
                "先停止 Harness",
                "修复会改动 Harness 正在使用的文件。\n\n是否先停止 Harness？",
            ):
                return
            self.on_stop()
        cmd = f'{approve_builds_cmd()} && {dsh_cmd()} plugin --profile web add "{pkg}"'
        self._run_async(cmd, f"修复插件登记 {pkg}",
                        on_done=lambda code: self._after_repair(code, pkg))

    def _after_repair(self, code: int, pkg: str) -> None:
        if code == 0:
            self._finish_plugin_change(f"{pkg} 的插件登记已修复")
            return
        messagebox.showerror(
            "修复未成功",
            f"修复命令退出码 {code}。\n\n"
            f"最后几行输出：\n{self._command_output_tail() or '（无）'}\n\n"
            "也可以手动试一次——在 web 档案目录里依次执行：\n"
            "    pnpm approve-builds --all\n"
            f"    dsh plugin --profile web add {pkg}\n\n"
            "完整输出见下方「运行日志」。",
        )
        self.refresh_plugins()

    def _finish_plugin_change(self, action: str) -> None:
        """插件层发生变动后的统一收尾：提示是否需要重启，然后刷新列表。

        插件层（``dsh.profile.bundles``）是在 Harness **启动时**加载的，
        装完插件只刷新浏览器没有用——这里主动把这件事说清楚。
        """
        try:
            after = bundles_list()
        except Exception:
            after = []
        before = list(self._bundles_before or [])
        added = [x for x in after if x not in before]
        removed = [x for x in before if x not in after]
        if not added and not removed:
            messagebox.showinfo("完成", f"{action}，正在刷新列表。")
            self.refresh_plugins()
            return
        detail = ""
        if added:
            detail += "新增插件层：" + "、".join(added) + "\n"
        if removed:
            detail += "移除插件层：" + "、".join(removed) + "\n"
        if self.harness.is_running():
            restart = messagebox.askyesno(
                "完成（需要重启才生效）",
                f"{action}。\n\n{detail}\n"
                "插件层是 Harness 启动时加载的，光刷新浏览器不管用。\n\n"
                "是否现在自动重启 Harness？\n"
                "（会先停止当前进程，等端口释放后自动重新启动）",
            )
            self.refresh_plugins()
            if restart:
                self._restart_dsh()
        else:
            messagebox.showinfo(
                "完成（下次启动生效）",
                f"{action}。\n\n{detail}\n"
                "插件层在启动时加载，所以下次点「启动 Harness」就会带上它。",
            )
            self.refresh_plugins()

    def _restart_dsh(self) -> None:
        """重启：**先关掉旧窗口 → 立刻转圈 → 再停服务 → 等端口释放 → 重新启动**。

        顺序是用户反馈后改的。原来的顺序是「停服务 → 等端口 → 启动 → 就绪 → 才换窗口」，
        结果那个卡住的旧窗口会在整个过程里一直杵在屏幕上（好几秒），用户既看不出
        "是不是在重启"，也不知道该不该等。现在一按下去就有明确反馈：

        - 旧窗口**立刻**关掉（它本来就是失效的）；
        - 状态灯**立刻**变成转圈的动画；
        - 服务停下、端口释放、重新启动（这几步本来就免不了），就绪后自动开新窗口。
        """
        self._append_log("[启动器] 正在重启 Harness（先关旧窗口，再停服务，然后重新启动）…")
        self._stop_health_monitor()
        # ① 先关旧窗口（勾了独立窗口才有；标签页模式关不掉，只能留在那儿）
        try:
            if self.app_window.close():
                self._append_log("[启动器] 已关闭旧的浏览器窗口。")
        except Exception:
            pass
        # ② 立刻进入"启动中"：状态灯转圈、主按钮变「停止」
        self.starting = True
        self._set_power_state("starting")
        self._set_status("正在重启 Harness…（旧窗口已关闭，稍后会打开新窗口）")
        self._start_wait_ticker()
        # ③ 停服务（等它真的退干净），④ 等端口释放后重新启动
        try:
            self.harness.stop()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[启动器] 停止时出错：{exc}")
        self._restart_deadline = time.time() + 30
        self.root.after(600, self._restart_when_port_free)

    def _restart_when_port_free(self) -> None:
        """等端口真正释放后再启动，避免撞上「端口已被占用」的提示。"""
        if is_port_open(WEB_PORT):
            if time.time() < self._restart_deadline:
                self.root.after(500, self._restart_when_port_free)
                return
            self._append_log("[启动器] 端口仍被占用，已取消自动重启；稍后可手动点「启动 Harness」。")
            self._set_status("已停止（端口仍被占用，请稍后手动启动）")
            return
        self.on_launch()


    def on_run_command(self) -> None:
        """运行任意命令（高级用法），输出显示在日志面板。"""
        cmd = simpledialog.askstring(
            "运行命令",
            "请输入要执行的命令（输出会显示在下方日志里）：",
            parent=self.root,
        )
        if not cmd or not cmd.strip():
            return
        self._run_async(cmd.strip(), "自定义命令")

    def on_manual_launch(self) -> None:
        """兜底：新开一个命令行窗口，用最原始的方式启动 DSH。"""
        cmd = "npx -y @deepseek-ai/dsh web"
        try:
            launch_in_console(cmd, "DeepSeek Harness（手动启动）")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("手动启动失败", str(exc))
            return
        self._append_log(f"[启动器] 已打开命令行窗口手动启动：{cmd}")
        self._append_log("[启动器] 该窗口请勿关闭；服务就绪后它会自己打开浏览器。")
        self._set_status("已用手动方式启动（见新开的命令行窗口）")

    # ------------------------------------------------------------------ #
    # 外观 / 日志 / 诊断报告
    # ------------------------------------------------------------------ #
    def _apply_theme(self, dark: bool) -> None:
        """切换浅色 / 深色外观。

        样式全部交给 ``theme`` 模块（ttk 的每个样式类都显式配置，不留系统默认），
        这里只负责把那些**不吃 ttk 样式**的原生控件逐个上色。
        """
        self.dark_mode = bool(dark)
        self.palette = theme.apply(self.root, self.dark_mode)
        p = self.palette
        for widget, is_field in (
            (getattr(self, "log_text", None), True),
            (getattr(self, "backup_list", None), True),
            (getattr(self, "env_text", None), True),
            (getattr(self, "_plugin_canvas", None), False),
            (getattr(self, "_status_dot", None), False),
        ):
            theme.paint(widget, p, field=is_field)
        self._draw_status_dot()

    def on_toggle_dark(self) -> None:
        self._apply_theme(self.dark_var.get())
        self.ui_settings["dark"] = self.dark_mode
        _save_ui_settings(self.ui_settings)
        # 插件行是渲染时定色的，换主题要重画一次（用缓存，不重新读档案）
        if self._last_inventory is not None:
            try:
                self._render_plugins(self._last_inventory)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 状态灯与主按钮（启动 / 停止 / 恢复 三态）
    # ------------------------------------------------------------------ #
    _DOT_KEYS = {
        "idle": "muted",
        "starting": "warn",
        "running": "ok",
        "stopped": "muted",
        "failed": "danger",
    }

    @property
    def _spinning(self) -> bool:
        """是否处在"正在启动"（该转圈）的状态。

        这里用两个信号**任一**成立来判断：``power_state`` 与 ``starting``。
        正常情况下它们一致，但如果哪天只更新了其中一个，也不会出现
        "状态说在启动、灯却不转"这种看着像卡死的假象。
        """
        return self.power_state == "starting" or bool(self.starting)

    def _draw_status_dot(self) -> None:
        """状态灯：平时是实心圆点；**启动过程中变成一个转圈的弧**。

        用转圈代替"已等待 N 秒"：等待是连续的过程，动画比数字更直观，
        也不会让人盯着数字越看越焦虑。
        """
        canvas = self._status_dot
        try:
            canvas.delete("all")
        except Exception:
            return
        if self._spinning:
            # 这一笔由 _animate_spinner 不断更新角度
            try:
                canvas.create_arc(2, 2, 14, 14, start=self._spin_angle, extent=270,
                                  style="arc", outline=self.palette["accent"],
                                  width=3, tags="spin")
            except Exception:
                pass
            return
        color = self.palette.get(self._dot_key, self.palette["muted"])
        try:
            self._dot_id = canvas.create_oval(2, 2, 14, 14, outline="", fill=color)
        except Exception:
            pass

    def _set_status_dot(self, key: str) -> None:
        self._dot_key = key
        self._draw_status_dot()

    def _set_power_state(self, mode: str) -> None:
        """统一管理主按钮与「重启」按钮。

        - ``idle``     未运行         → 「启动 Harness」（实心强调蓝，最抢眼）
        - ``starting`` 正在启动       → 「停止」（浅红底红字，可中断）
        - ``running``  运行中         → 「停止」（浅红底红字），「重启」可用
        - ``stopped``  被手动停止     → 「恢复」（浅绿底绿字）：临时停一下就点它回来
        - ``failed``   启动失败       → 「启动 Harness」
        """
        self.power_state = mode
        if mode in ("running", "starting"):
            text, style, state = "停止", "Stop.TButton", "normal"
        elif mode == "stopped":
            text, style, state = "恢复", "Resume.TButton", "normal"
        else:
            text, style, state = "启动 Harness", "Accent.TButton", "normal"
        restart_state = "normal" if mode == "running" else "disabled"
        try:
            self.btn_power.configure(text=text, style=style, state=state)
        except Exception:
            pass
        try:
            self.btn_restart.configure(state=restart_state)
        except Exception:
            pass
        self._set_status_dot(self._DOT_KEYS.get(mode, "muted"))

    def on_power(self) -> None:
        """主按钮：按当前状态决定是启动、停止还是恢复。"""
        if self.power_state == "starting":
            self.on_stop(manual=True)
        elif self.power_state == "running":
            self.on_stop(manual=True)
        else:
            self.on_launch()

    def on_restart(self) -> None:
        """重启：停掉再启动（改完插件 / 想彻底重来一次时用）。"""
        if not dsh_available():
            messagebox.showerror("未检测到 dsh", "未检测到可用的全局 dsh 命令，无法重启。")
            return
        if not messagebox.askyesno(
            "重启 Harness",
            "将先停止当前的 Harness，等端口释放后再重新启动。\n\n"
            "正在进行的对话 / 任务会被中断（浏览器窗口会在启动后重新打开）。\n\n确定重启吗？",
        ):
            return
        self._restart_dsh()

    def on_save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="日志另存为",
            defaultextension=".txt",
            initialfile="dsh-launcher-log.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_text.get("1.0", "end"), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))
            return
        messagebox.showinfo("已保存", f"日志已保存到：\n{path}")

    def on_clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.log_tail.clear()

    def on_export_diagnostics(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出诊断报告",
            defaultextension=".txt",
            initialfile="dsh-diagnostic.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self._set_status("正在生成诊断报告…")
        threading.Thread(target=self._diag_thread, args=(path,), daemon=True).start()

    def _diag_thread(self, path: str) -> None:
        try:
            inv = list_plugins()
        except Exception as exc:  # noqa: BLE001
            inv = Inventory([], [], [], f"清点插件时出错：{exc}")
        lines = [
            f"DeepSeek Harness 启动器 —— 诊断报告（启动器 v{__version__}）",
            "生成时间：" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "",
            "== 环境 ==",
            f"DSH 家目录：{dsh_home()}",
            f"web 档案目录：{profile_dir()}",
            f"备份目录：{backups_root()}",
            f"调用的 dsh 命令：{dsh_cmd()}",
            f"dsh 是否可用：{'是' if dsh_available() else '否'}",
            "",
            "== 版本 ==",
            f"DSH 本机版本：{get_installed_version()}",
            f"DSH 最新版本：{get_latest_version()}",
        ]
        for tool in ("node", "npm", "pnpm"):
            try:
                proc = run_command(f"{tool} --version", timeout=30)
                lines.append(f"{tool}：{(proc.stdout or '').strip() or '未知'}")
            except Exception:
                lines.append(f"{tool}：查询失败")
        lines += ["", "== 插件层（会参与启动，可开关）=="]
        if inv.plugins:
            for p in inv.plugins:
                lines.append(f"{p.name}  v{p.version}  [{'启用' if p.enabled else '已禁用'}]")
        else:
            lines.append("（无）")
        if inv.unmounted:
            lines += ["", "== 已安装但未挂载（声明了插件层却没登记，不会生效）=="]
            for p in inv.unmounted:
                lines.append(f"{p.name}  v{p.version}")
        if inv.libraries:
            lines += ["", "== 库依赖 =="]
            for p in inv.libraries:
                lines.append(f"{p.name}  v{p.version}")
        lines += ["", "== 插件条目解析（--dump-config）=="]
        if inv.dump_error:
            lines.append("失败：" + inv.dump_error)
            lines.append("（注意：此时所有插件的「开关」都不可用。）")
        else:
            total = sum(len(p.entry_ids) for p in inv.plugins)
            lines.append(f"成功，共定位到 {total} 个条目 id。")
        lines += ["", "== 最近日志 =="]
        lines.extend(self.log_tail)
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
            self._post(("diag_done", path, None))
        except Exception as exc:  # noqa: BLE001
            self._post(("diag_done", "", exc))

    def _on_diag_done(self, path: str, err) -> None:
        if err is not None:
            self._set_status("诊断报告导出失败")
            messagebox.showerror("导出失败", str(err))
            return
        self._set_status("诊断报告已导出")
        messagebox.showinfo("已导出", f"诊断报告已保存到：\n{path}")

    # ------------------------------------------------------------------ #
    # 安全模式 / 上次正常配置
    # ------------------------------------------------------------------ #
    def on_safe_mode_launch(self) -> None:
        """安全模式：临时禁用全部第三方插件启动一次，不改动开关设置。"""
        if not dsh_available():
            messagebox.showerror("未检测到 dsh", "未检测到可用的全局 dsh 命令，无法启动。")
            return
        if self.harness.is_running():
            messagebox.showinfo("提示", "Harness 正在运行中，请先停止。")
            return
        ids = [eid for p in self._current_plugins for eid in p.entry_ids]
        if not ids:
            messagebox.showinfo(
                "安全模式",
                "没有检测到可禁用的第三方插件条目，无需安全模式。\n"
                "如果插件列表是空的，请先点「刷新」。",
            )
            return
        if not messagebox.askyesno(
            "安全模式启动",
            f"本次启动将临时禁用 {len(ids)} 个第三方插件条目（不会改动你的开关设置）。\n\n"
            "用途：判断启动失败究竟是不是插件引起的。\n\n确定继续吗？",
        ):
            return
        try:
            patch = write_safe_mode_patch(ids)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("安全模式", f"生成补丁失败：{exc}")
            return
        self._append_log(f"[启动器] 安全模式：临时禁用 {len(ids)} 个插件条目")
        self._launch(patch, "（安全模式）")

    def _restore_last_good(self) -> None:
        """把插件开关状态恢复到“上次正常启动”时的样子。"""
        data = load_last_good()
        if not data:
            messagebox.showinfo("恢复上次正常配置", "还没有记录到“上次正常启动”的配置。")
            return
        try:
            set_disabled_map(data.get("disabled") or {})
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("恢复失败", str(exc))
            return
        self._append_log(f"[启动器] 已恢复上次正常配置（{data.get('saved', '?')}）")
        saved_plugins = data.get("plugins") or {}
        self.refresh_plugins()
        msg = f"已把插件开关恢复到 {data.get('saved', '上次正常启动')} 的状态。"
        try:
            current = {p.name: p.version for p in self._current_plugins}
            diff = [n for n, v in saved_plugins.items()
                    if current.get(n) and current[n] != v]
            if diff:
                msg += "\n\n下列插件版本与当时不同，可用「版本…」回退：\n" + "\n".join(
                    f"• {n}：现在 {current[n]}，当时 {saved_plugins[n]}" for n in diff)
        except Exception:
            pass
        messagebox.showinfo("恢复上次正常配置", msg)

    # ------------------------------------------------------------------ #
    # 备份 / 重置 / 还原 / 插件清单导入导出
    # ------------------------------------------------------------------ #
    def _make_scroll_frame(self, parent, height: int = 220):
        """在对话框里做一个可滚动区域，返回内部 Frame。"""
        canvas = tk.Canvas(parent, height=height, highlightthickness=0)
        theme.paint(canvas, self.palette, field=False)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return inner

    # —— 手动备份 ——
    def on_manual_backup(self) -> None:
        dlg = tk.Toplevel(self.root)
        theme.paint_dialog(dlg, self.palette)
        dlg.title("手动备份")
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="要备份的内容：", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        var_sessions = tk.BooleanVar(value=True)
        var_files = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="包含历史会话（推荐，体积可能较大）",
                        variable=var_sessions).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(frm, text="包含插件文件本身 node_modules（很大很慢，一般不需要）",
                        variable=var_files).pack(anchor="w")
        ttk.Label(frm, text="插件清单（名称+版本）与档案配置总是会备份。",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 12))
        row = ttk.Frame(frm)
        row.pack(fill="x")

        def do_backup() -> None:
            dlg.destroy()
            self._start_backup(var_sessions.get(), var_files.get())

        ttk.Button(row, text="开始备份", command=do_backup).pack(side="left")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="right")

    def _start_backup(self, include_sessions: bool, include_files: bool) -> None:
        self._set_status("正在备份…")

        def worker() -> None:
            try:
                dest = create_backup(label="手动备份",
                                     include_sessions=include_sessions,
                                     include_plugin_files=include_files)
                self._post(("backup_done", str(dest), None))
            except Exception as exc:  # noqa: BLE001
                self._post(("backup_done", "", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_backup_done(self, dest: str, err) -> None:
        if err is not None:
            self._set_status("备份失败")
            messagebox.showerror("备份失败", str(err))
            return
        self._set_status("备份完成")
        self._append_log(f"[启动器] 已创建备份：{dest}")
        self.refresh_backup_list()
        messagebox.showinfo("备份完成", f"已创建备份：\n{dest}")

    # —— 重置档案 ——
    def on_reset_profile(self) -> None:
        if self.harness.is_running():
            if not messagebox.askyesno(
                "重置 web 档案",
                "Harness 正在运行中，重置会删除它正在使用的文件。\n\n是否先停止 Harness？",
            ):
                return
            self.on_stop()
        if not messagebox.askyesno(
            "重置 web 档案",
            "重置会做两件事：\n"
            "  1. 先自动备份当前档案（含插件清单与历史会话）\n"
            "  2. 清空 profiles/web（已装插件被移除，配置恢复默认）\n\n"
            "你的聊天记录不受影响。确定要重置吗？",
        ):
            return
        self._set_status("正在重置档案（已自动备份）…")

        def worker() -> None:
            try:
                dest, removed = reset_profile(include_sessions_in_backup=True)
                self._post(("reset_done", str(dest), removed, None))
            except Exception as exc:  # noqa: BLE001
                self._post(("reset_done", "", [], exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_reset_done(self, dest: str, removed: list, err) -> None:
        if err is not None:
            self._set_status("重置失败")
            messagebox.showerror("重置失败", str(err))
            return
        self._set_status("档案已重置")
        self._append_log(f"[启动器] 已重置档案，自动备份在：{dest}")
        self.refresh_backup_list()
        messagebox.showinfo(
            "重置完成",
            "web 档案已重置为干净状态。\n\n"
            f"重置前的自动备份在：\n{dest}\n\n"
            "下次启动 Harness 会自动重新初始化档案。",
        )
        self.refresh_plugins()

    # —— 恢复备份 ——
    def on_restore_backup(self) -> None:
        try:
            backups = list_backups()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("恢复备份", f"读取备份列表失败：{exc}")
            return
        if not backups:
            messagebox.showinfo("恢复备份", "还没有任何备份。\n可以先用「手动备份」创建一份。")
            return

        dlg = tk.Toplevel(self.root)

        theme.paint_dialog(dlg, self.palette)
        dlg.title("恢复备份")
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="选择一个备份：", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        listbox = tk.Listbox(frm, height=8, width=72, relief="flat", bd=0,
                             activestyle="none", highlightthickness=0)
        theme.paint(listbox, self.palette)
        for b in backups:
            listbox.insert("end", backup_summary(b))
        listbox.pack(fill="both", expand=True, pady=(4, 10))
        listbox.selection_set(0)

        ttk.Label(frm, text="还原内容：", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        mode = tk.StringVar(value="plugins")
        ttk.Radiobutton(frm, text="仅插件（还原插件清单与档案配置）",
                        variable=mode, value="plugins").pack(anchor="w")
        ttk.Radiobutton(frm, text="仅历史会话（与现有会话合并，不删除现有记录）",
                        variable=mode, value="sessions").pack(anchor="w")
        ttk.Radiobutton(frm, text="全部还原（插件 + 历史会话 + 设置）",
                        variable=mode, value="all").pack(anchor="w")
        ttk.Label(frm, text="还原前会自动再备份一次当前状态，随时可以回退。",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 12))

        row = ttk.Frame(frm)
        row.pack(fill="x")

        def do_restore() -> None:
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个备份。")
                return
            chosen = backups[sel[0]]
            chosen_mode = mode.get()
            dlg.destroy()
            self._start_restore(chosen["path"], chosen_mode)

        ttk.Button(row, text="开始还原", command=do_restore).pack(side="left")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="right")

    def _start_restore(self, path: str, mode: str) -> None:
        self._set_status("正在还原…")

        def worker() -> None:
            try:
                actions = restore_backup(path, mode)
                self._post(("restore_done", mode, actions, None))
            except Exception as exc:  # noqa: BLE001
                self._post(("restore_done", mode, [], exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_restore_done(self, mode: str, actions: list, err) -> None:
        if err is not None:
            self._set_status("还原失败")
            messagebox.showerror("还原失败", str(err))
            return
        self._set_status("还原完成")
        self._append_log("[启动器] 还原：" + "；".join(actions))
        self.refresh_backup_list()
        messagebox.showinfo("还原完成", "已完成：\n" + "\n".join("• " + a for a in actions))
        if mode in ("plugins", "all"):
            if messagebox.askyesno(
                "重新安装插件",
                "是否现在按还原后的插件清单重新安装插件文件？\n"
                "（会执行 pnpm install，可能需要一点时间）",
            ):
                self._run_async(f"{dsh_cmd()} plugin --profile web install", "重新安装插件",
                                on_done=lambda code: self.refresh_plugins())
                return
        self.refresh_plugins()

    # —— 导出 / 导入插件清单 ——
    def on_export_plugin_list(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出插件列表",
            defaultextension=".json",
            initialfile="dsh-plugins.json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            count = export_plugin_list(Path(path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", str(exc))
            return
        messagebox.showinfo("导出完成", f"已导出 {count} 个插件到：\n{path}")

    def on_import_plugin_list(self) -> None:
        path = filedialog.askopenfilename(
            title="导入插件列表",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            plugins = read_plugin_list(Path(path))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", str(exc))
            return
        if not plugins:
            messagebox.showwarning("导入失败", "文件里没有识别到插件名称。")
            return
        self._set_status(f"正在检查 {len(plugins)} 个插件的更新情况…")
        threading.Thread(target=self._check_import_thread, args=(plugins,), daemon=True).start()

    def _check_import_thread(self, plugins: dict) -> None:
        results = []
        for name, ver in plugins.items():
            versions = get_package_versions(name)
            results.append({
                "name": name,
                "backup": str(ver or ""),
                "latest": versions[0][0] if versions else "",
                "found": bool(versions),
            })
        self._post(("import_info", results))

    def _show_import_dialog(self, results: list) -> None:
        if not results:
            self._set_status("导入失败")
            return
        outdated = [r for r in results if r["found"] and r["backup"] and r["latest"] != r["backup"]]
        self._set_status(f"共 {len(results)} 个插件，其中 {len(outdated)} 个有新版本")

        dlg = tk.Toplevel(self.root)

        theme.paint_dialog(dlg, self.palette)
        dlg.title("从备份列表重新安装插件")
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=f"共 {len(results)} 个插件，{len(outdated)} 个有新版本：",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        inner = self._make_scroll_frame(frm, height=240)
        vars_map = {}
        for r in results:
            var = tk.BooleanVar(value=True)
            vars_map[r["name"]] = var
            row = ttk.Frame(inner, padding=(2, 3))
            row.pack(fill="x", anchor="w")
            ttk.Checkbutton(row, variable=var).pack(side="left", anchor="n")
            text = r["name"]
            if not r["found"]:
                text += "   （npm 上查不到，将按备份版本尝试）"
            elif r["backup"] and r["latest"] != r["backup"]:
                text += f"   {r['backup']} → 最新 {r['latest']}"
            elif r["latest"]:
                text += f"   {r['latest']}（已是最新）"
            ttk.Label(row, text=text, wraplength=560, justify="left").pack(side="left", anchor="w")

        var_latest = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="有更新时优先安装最新版本（不勾选则按备份时记录的版本安装）",
                        variable=var_latest).pack(anchor="w", pady=(8, 12))

        row = ttk.Frame(frm)
        row.pack(fill="x")

        def do_install() -> None:
            chosen = []
            for r in results:
                if not vars_map[r["name"]].get():
                    continue
                target = ""
                if var_latest.get() and r["found"] and r["latest"]:
                    target = r["latest"]
                elif r["backup"]:
                    target = r["backup"]
                elif r["found"] and r["latest"]:
                    target = r["latest"]
                chosen.append({"name": r["name"], "target": target})
            if not chosen:
                messagebox.showwarning("提示", "请至少勾选一个插件。")
                return
            dlg.destroy()
            self._start_import_install(chosen)

        ttk.Button(row, text="开始安装", command=do_install).pack(side="left")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="right")

    def _start_import_install(self, chosen: list) -> None:
        specs = []
        for item in chosen:
            name, target = item["name"], item["target"]
            specs.append(f'"{name}@{target}"' if target else f'"{name}"')
        cmd = f"{dsh_cmd()} plugin --profile web add " + " ".join(specs)
        self._run_async(cmd, f"重新安装 {len(chosen)} 个插件", on_done=self._after_import_install)

    def _after_import_install(self, code: int) -> None:
        if code == 0:
            self._finish_plugin_change("插件安装完成")
            return
        # 批量安装，拿不到单一的包名，所以只给线索、不做一键修复
        blocked = ignored_build_packages(self._last_cmd_output)
        hint = ""
        if blocked:
            hint = ("\n\n⚠ 这些依赖的安装脚本被 pnpm 拦下了：" + "、".join(blocked) +
                    "\n对应的插件会停在「已安装但未挂载」状态，"
                    "请回到插件列表在红色分组里逐个点「修复」。")
        messagebox.showerror(
            "失败",
            f"安装失败（退出码 {code}）。\n\n"
            f"命令最后几行输出：\n{self._command_output_tail() or '（无）'}{hint}\n\n"
            "完整输出见下方「运行日志」。",
        )
        self.refresh_plugins()

    # ------------------------------------------------------------------ #
    # 启动 / 停止
    # ------------------------------------------------------------------ #
    def open_browser(self) -> None:
        """打开界面。

        用它自己那套「应用窗口」打开，这样启动器才管得住这个窗口
        （关闭启动器时可以把它一起关掉）；服务还没起来时给出提示。
        """
        if self._ready_url is None:
            if is_port_open(WEB_PORT):
                messagebox.showinfo(
                    "提示",
                    "Harness 的服务已经在监听端口，但启动器没拿到本次的认证地址"
                    "（通常是这次是别的程序启动的）。\n\n"
                    "请在 Harness 自己的窗口里点开界面，或先「重启」一次由启动器接管。",
                )
                return
            messagebox.showinfo("提示", "Harness 还没启动，先点「启动 Harness」吧。")
            return
        self._open_ready_page(self._ready_url)

    def on_launch(self) -> None:
        if not dsh_available():
            messagebox.showerror(
                "未检测到 dsh",
                "未检测到可用的全局 dsh 命令，无法启动 Harness。\n\n"
                "请到「维护」选项卡点【安装 / 修复 DSH】自动完成安装；\n"
                "也可以手动执行：npm install -g @deepseek-ai/dsh",
            )
            return
        if self.harness.is_running():
            messagebox.showinfo("提示", "Harness 正在运行中。")
            return
        if is_port_open(WEB_PORT):
            if messagebox.askyesno("检测到服务", f"端口 {WEB_PORT} 已被占用，可能 Harness 已在运行。\n是否直接打开浏览器？"):
                self.open_browser()
            return

        # 生成（或刷新）禁用补丁，并带上当前状态一起启动
        write_disabled_patch()
        patch = disabled_patch_path() if disabled_packages() else None
        self._launch(patch, "")

    def _launch(self, patch, label: str = "") -> None:
        """统一的启动流程。``patch`` 是要附加的补丁文件（可为 None）。"""
        self._ready_url = None      # 清空上次的地址，等待本次启动重新捕获
        self.starting = True
        suffix = f" {label}" if label else ""
        self._set_status(f"正在启动 Harness{suffix}…（通常需要几秒，实时日志在下方）")
        self._set_power_state("starting")
        self._append_log(f"[启动器] 正在启动 DeepSeek Harness{suffix} …")
        self._start_wait_ticker()
        try:
            self.harness.launch(patch)
        except Exception as exc:  # noqa: BLE001
            self._append_log("启动进程失败：" + str(exc))
            self._finish_launch_failed(f"启动进程失败：{exc}")
            return
        threading.Thread(target=self._wait_ready, daemon=True).start()

    def _start_wait_ticker(self) -> None:
        """启动期间在状态栏转圈。

        DSH 自身启动要几秒（node + 插件树，实测本机约 5~7 秒），这段时间启动器做不了别的
        —— 但至少要让人看到"它在动"，而不是以为点了没反应。
        """
        self._launch_started_at = time.time()
        self._spin_angle = 0
        self._draw_status_dot()          # 立刻切成"转圈"形态
        self._animate_spinner()

    def _animate_spinner(self) -> None:
        """每 80ms 把圆弧转一点。启动结束（``_spinning`` 变假）就自动停下。"""
        if not self._spinning:
            return
        self._spin_angle = (self._spin_angle + 30) % 360
        try:
            self._status_dot.itemconfigure("spin", start=self._spin_angle)
        except Exception:
            pass
        self.root.after(80, self._animate_spinner)

    # ------------------------------------------------------------------ #
    # 健康检查：让用户知道"到底发生了什么"
    # ------------------------------------------------------------------ #
    # 用户能看到的现象（页面「自动重连中」、对话卡住）其实有三种完全不同的原因：
    #   ① 宿主进程没了      ② 宿主还在但对请求没反应（卡住）    ③ 宿主一切正常、是页面/窗口自己掉线
    # 启动器没法直接看页面的 WebSocket，但能把这三种区分开 —— 这就够告诉用户该干什么了。
    HEALTH_INTERVAL = 5.0          # 每次检查间隔（秒）
    HEALTH_FAILS_TO_WARN = 2       # 连续失败几次才告警（避免偶发抖动就报警）

    def _start_health_monitor(self) -> None:
        self._health_stop = False
        self._health_fails = 0
        # 刚启动检查时**视为正常**：否则第一次成功会被当成"从故障恢复"，平白写一行日志
        self._health_state = "ok"
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()

    def _stop_health_monitor(self) -> None:
        """只让线程停下。

        **不要**在这里改 ``_health_state`` —— 它记录的是"最后已知的服务状态"（比如 dead），
        清掉它会把"宿主已退出"这个结论丢掉（测试抓到过）。
        """
        self._health_stop = True

    def _health_loop(self) -> None:
        """后台线程：定时看宿主进程是否还在、是否还在应答 HTTP 请求。"""
        url = self._ready_url
        while not self._health_stop:
            time.sleep(self.HEALTH_INTERVAL)
            if self._health_stop:
                return
            if not self.harness.is_running():
                self._post(("health", "dead", "宿主进程已退出"))
                return
            if not url:
                continue
            ok, detail = probe_url(url, timeout=4.0)
            self._post(("health", "ok" if ok else "bad", detail))

    def _on_health(self, state: str, detail: str) -> None:
        """健康检查结果回到主线程后的处理：只在**状态发生变化**时改界面/写日志。

        为什么只在变化时动：状态栏和日志还可能显示别的重要信息（比如"已禁用插件 X"），
        每 5 秒覆盖一次会把这些冲掉，反而更乱。
        """
        if state == "dead":
            if self._health_state != "dead":
                self._health_state = "dead"
                self._stop_health_monitor()
                self._set_power_state("failed")
                self._set_status("宿主进程已退出 —— 服务已停（页面会一直显示「自动重连中」）")
                self._append_log("[启动器] 健康检查：宿主进程已退出（不是我停的）。"
                                 "可点「启动 Harness」重新拉起；日志上方可能有它的最后遗言。")
            return

        if state == "ok":
            self._health_fails = 0
            if self._health_state != "ok":
                self._health_state = "ok"
                self._set_status_dot("ok")
                self._set_status("服务已恢复正常 ✓")
                self._append_log(f"[启动器] 健康检查：服务恢复正常（{detail}）。")
            self._check_app_window_alive()
            return

        # state == "bad"：宿主还在，但对 HTTP 请求没反应
        self._health_fails += 1
        if self._health_fails < self.HEALTH_FAILS_TO_WARN:
            return
        if self._health_state != "bad":
            self._health_state = "bad"
            self._set_status_dot("warn")
            self._set_status(f"⚠ 服务无响应（连续 {self._health_fails} 次）· 页面多半正显示「自动重连中」")
            self._append_log(f"[启动器] 健康检查：连续 {self._health_fails} 次访问本地服务无响应（{detail}）。")
            self._append_log("[启动器] 含义：宿主进程**还在**，但它没有应答请求 —— "
                             "页面此时通常显示「自动重连中」、正在进行的对话也会卡住。")
            self._append_log("[启动器] 常见原因：① 机器正忙、宿主被卡住；"
                             "② 本地回环流量被代理/VPN 劫持（Clash 开 TUN 时尤其要注意）；"
                             "③ 宿主内部出问题。")
            self._append_log("[启动器] 建议：先点「打开界面」重开页面；仍不行就点「重启」"
                             "（重启会先关窗口、再停服务、然后重新启动）。")

    def _check_app_window_alive(self) -> None:
        """服务一切正常，但我们开的那个独立窗口已经不在了 —— 提示一次。

        这是第三类情况：「宿主没事、只是窗口掉了」。用户看到的现象和"服务卡住"很像
        （页面连不上），但处理办法完全不同（重开窗口即可，不用重启服务）。
        """
        if not self.open_as_app or self._window_gone_reported:
            return
        try:
            gone = self.app_window.window_gone()
        except Exception:
            return
        if gone:
            self._window_gone_reported = True
            self._set_status("服务正常，但独立窗口已关闭 —— 点「打开界面」可重开")
            self._append_log("[启动器] 健康检查：服务正常，但之前打开的独立窗口已经不在了"
                             "（被关掉，或浏览器进程退出了）。点「打开界面」即可重开一个。")

    def _wait_ready(self) -> None:
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if not self.starting:
                # 用户在等待期间手动点了“停止”
                return
            if self.harness.proc is not None and self.harness.proc.poll() is not None:
                code = self.harness.proc.poll()
                self._post(("launch_failed", code))
                return
            # 以 DSH 打印的“dsh web: <认证地址>”这一行为准判断就绪，
            # 而不是只看端口是否被占用——端口可能在插件树加载完成前就通了。
            if self._ready_url is not None:
                self._post(("launch_ready", self._ready_url))
                return
            time.sleep(0.2)
        self._post(("launch_timeout",))

    def _on_launch_ready(self, url: str) -> None:
        self._set_status("启动成功 ✓")
        self.starting = False
        self._set_power_state("running")
        self._open_ready_page(url)
        self._start_health_monitor()      # 之后就靠它盯着"服务还在不在、还答不答理"
        try:
            save_last_good()      # 记下“这次是好的”，以后出问题能一键回退
            self._append_log("[启动器] 已记录本次为「上次正常配置」。")
        except Exception:
            pass

    def on_open_mode_changed(self) -> None:
        """切换「独立窗口 / 普通标签页」并记住选择。"""
        self.open_as_app = bool(self.open_mode_var.get())
        self.ui_settings["open_as_app"] = self.open_as_app
        _save_ui_settings(self.ui_settings)
        if self.open_as_app:
            self._append_log("[启动器] 打开方式：独立窗口（关启动器时会一并关闭它）")
        else:
            self._append_log("[启动器] 打开方式：默认浏览器标签页（启动器关不掉它）")

    def _open_ready_page(self, url: str) -> None:
        """按用户选的打开方式打开页面。"""
        self._append_log(f"[启动器] 服务已就绪，正在打开界面：{endpoint_label(url)}")
        try:
            owned, note = self.app_window.open(url, as_app_window=self.open_as_app)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[启动器] 打开浏览器失败：{exc}")
            return
        self._append_log(f"[启动器] {note}")
        if owned:
            self._window_gone_reported = False   # 新窗口开出来了，之前的"窗口没了"提示可以重来
        if not owned:
            self._append_log("[启动器] 提示：这个页面不由启动器接管，关闭启动器时无法自动关掉它。")

    def _on_launch_failed(self, code) -> None:
        self.starting = False
        self._stop_health_monitor()
        self._set_power_state("failed")
        self._set_status("启动失败 ✗ 请查看错误信息，关闭可疑插件后重试")
        tail = "\n".join(self.log_tail) or "（无日志输出）"
        self._show_failed_dialog(code, tail)

    def _on_launch_timeout(self) -> None:
        self.starting = False
        self._set_power_state("running")     # 进程还在，按“运行中”处理，用户可停止
        self._set_status("启动超时，可能仍在初始化，请看日志")
        self._append_log("[启动器] 等待服务就绪超时（仍可点击“打开界面”手动访问）")
        self._start_health_monitor()         # 没等到就绪行，就更需要盯着它到底活没活

    def _finish_launch_failed(self, msg: str) -> None:
        self.starting = False
        self._stop_health_monitor()
        self._set_power_state("failed")
        self._set_status("启动失败 ✗")
        self._show_failed_dialog(None, msg)

    def _show_failed_dialog(self, code, detail: str) -> None:
        diag = diagnose_boot_failure(detail)
        dlg = tk.Toplevel(self.root)
        theme.paint_dialog(dlg, self.palette)
        dlg.title("启动失败")
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        head = "Harness 启动失败"
        if code is not None:
            head += f"（退出码 {code}）"
        ttk.Label(frm, text=head, font=("Microsoft YaHei UI", 11, "bold"), style="Danger.TLabel").pack(anchor="w")

        if diag:
            # 认出已知故障时，先说人话：是什么、为什么、怎么办
            self._append_log(f"[启动器] 启动失败诊断：{diag['title']}")
            ttk.Label(frm, text="诊断：" + diag["title"],
                      font=("Microsoft YaHei UI", 10, "bold"), style="Danger.TLabel",
                      wraplength=460, justify="left").pack(anchor="w", pady=(8, 2))
            if diag.get("detail"):
                ttk.Label(frm, text=diag["detail"], wraplength=460, justify="left").pack(anchor="w")
            if diag.get("hint"):
                ttk.Label(frm, text=diag["hint"], wraplength=460, justify="left",
                          style="Warn.TLabel").pack(anchor="w", pady=(6, 0))
            ttk.Label(frm, text="下方是原始输出，可直接复制给别人看。",
                      style="Muted.TLabel").pack(anchor="w", pady=(10, 4))
        else:
            ttk.Label(frm, text="下方是错误信息。请在主窗口关闭可疑插件后，重新点击“启动 Harness”。",
                      wraplength=460).pack(anchor="w", pady=(6, 8))

        txt = scrolledtext.ScrolledText(frm, height=14, width=64, wrap="word",
                                        relief="flat", bd=0, font=(theme.FONT_MONO, 9))
        theme.paint(txt, self.palette)
        txt.insert("1.0", detail)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)

        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(10, 0))
        copy_btn = ttk.Button(row, text="一键复制错误信息")
        copy_btn.pack(side="left")

        def do_copy() -> None:
            if self._copy_to_clipboard(detail):
                copy_btn.config(text="已复制 ✓")
                dlg.after(1500, lambda: copy_btn.config(text="一键复制错误信息"))

        copy_btn.config(command=do_copy)

        def do_restore() -> None:
            dlg.destroy()
            self._restore_last_good()

        if load_last_good():
            ttk.Button(row, text="恢复上次正常配置", command=do_restore).pack(side="left", padx=(8, 0))

        if diag and diag.get("kind") == "module-export-mismatch":
            def do_repair_dsh() -> None:
                dlg.destroy()
                self.on_install_dsh()      # 自带二次确认，不直接动手

            ttk.Button(row, text="安装 / 修复 DSH…", command=do_repair_dsh).pack(side="left", padx=(8, 0))

        ttk.Button(row, text="知道了", command=dlg.destroy).pack(side="right")

    def _copy_to_clipboard(self, text: str) -> bool:
        """把文本复制到系统剪贴板；成功返回 True。"""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()   # 确保内容真正写入剪贴板
            return True
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("复制失败", f"无法复制到剪贴板：{exc}")
            return False

    def on_stop(self, manual: bool = False) -> None:
        """停止 Harness。

        ``manual=True``：用户在主按钮上主动点的「停止」——**保留浏览器窗口**，
        按钮变成「恢复」，方便临时停一下再拉起来。
        其它流程里调用（重启、卸载插件前、恢复配置前）不需要保留，马上会重开新页面。

        这里等 taskkill 真正杀完整棵进程树（``wait=True``）：返回后界面才说"已停止"，
        用户紧接着点「重启」时端口才确实空着。
        """
        self._append_log("[启动器] 正在停止 …")
        try:
            self.harness.stop()          # wait=True
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[启动器] 停止时出错：{exc}")
        self._on_stopped(manual=manual)

    def _on_stopped(self, manual: bool = True) -> None:
        self.starting = False
        self._stop_health_monitor()
        self._set_power_state("stopped" if manual else "idle")
        if manual:
            self._set_status("已停止（浏览器窗口留着，点「恢复」重新启动）")
        else:
            self._set_status("已停止")
        self._append_log("[启动器] 已停止。")

    def _on_close(self) -> None:
        """关闭窗口前先提醒：会连 Harness 和它的浏览器窗口一起关掉。"""
        self._stop_health_monitor()
        owns_window = self.app_window.is_open()
        if self.harness.is_running() or owns_window:
            lines = ["关闭启动器会同时："]
            if self.harness.is_running():
                lines.append("  • 停止 Harness（正在执行的对话 / 任务会中断）")
            if owns_window:
                lines.append("  • 关闭为它打开的浏览器窗口")
            lines.append("\n确定要关闭吗？")
            if not messagebox.askyesno("确认关闭", "\n".join(lines)):
                return  # 用户点了“否”，取消关闭，继续运行
        # 关启动器时**不等** taskkill：杀整棵进程树要等它退干净，界面会白等一会儿。
        # taskkill 是独立进程，启动器退出后照样把活干完（点「停止」按钮仍会等，见 on_stop）。
        try:
            self.harness.stop(wait=False)
        except Exception:
            pass
        try:
            if self.app_window.close():
                self._append_log("[启动器] 已关闭浏览器窗口。")
        except Exception:
            pass
        try:                                # 记住窗口大小 / 位置与深浅色
            self.ui_settings["geometry"] = self.root.winfo_geometry()
            self.ui_settings["dark"] = self.dark_mode
            _save_ui_settings(self.ui_settings)
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
