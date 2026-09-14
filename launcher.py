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
import threading
import webbrowser
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from tkinter import scrolledtext
from pathlib import Path

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
)
from plugins import (
    list_plugins,
    set_package_enabled,
    disabled_packages,
    write_disabled_patch,
    write_safe_mode_patch,
    set_disabled_map,
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
__version__ = "1.0.0"

# 首次启动可能要走 npx 下载依赖，因此给足等待时间（秒）
READY_TIMEOUT = 180
# 日志面板保留的行数（也用于失败时展示错误摘要）
LOG_TAIL = 80

LINK_STYLE = {"foreground": "#2a7ae2", "cursor": "hand2"}


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


DARK_PALETTE = {"bg": "#1f1f1f", "fg": "#e6e6e6", "field": "#2b2b2b"}
LIGHT_PALETTE = {"bg": "#f0f0f0", "fg": "#202020", "field": "#ffffff"}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.harness = HarnessProcess(log_callback=self._on_log_line)
        self.ui_queue: queue.Queue = queue.Queue()
        self.log_tail: collections.deque = collections.deque(maxlen=LOG_TAIL)
        self.starting = False          # 是否正处于“启动→等待就绪”的过程
        self.plugin_vars = {}          # 包名 -> BooleanVar（开关）
        self.plugin_entry_ids = {}     # 包名 -> 条目 id 列表（渲染时缓存，供开关使用）
        self._ready_url = None         # DSH 启动成功后打印的带 token 访问地址
        self._current_plugins = []     # 最近一次渲染出来的插件列表（供“检查全部更新”使用）
        self.ui_settings = _load_ui_settings()
        self.dark_mode = bool(self.ui_settings.get("dark", False))
        self.dark_var = tk.BooleanVar(value=self.dark_mode)

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
    def _build_ui(self) -> None:
        self.root.title(f"DeepSeek Harness 启动器  v{__version__}")
        self.root.geometry("840x720")
        self.root.minsize(720, 600)

        # —— 主体：三个选项卡，按钮按职责分组，不再堆在一起 ——
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        self._build_home_tab(nb)
        self._build_backup_tab(nb)
        self._build_tools_tab(nb)

        # —— 底部：状态栏 + 运行日志（无论切到哪个选项卡都看得到）——
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, padding=(12, 3)).pack(fill="x")
        log_frame = ttk.LabelFrame(self.root, text="运行日志", padding=6)
        log_frame.pack(fill="both", padx=10, pady=(0, 10))
        log_head = ttk.Frame(log_frame)
        log_head.pack(fill="x")
        ttk.Checkbutton(log_head, text="深色模式", variable=self.dark_var,
                        command=self.on_toggle_dark).pack(side="left")
        ttk.Button(log_head, text="清空", command=self.on_clear_log).pack(side="right")
        ttk.Button(log_head, text="日志另存为…", command=self.on_save_log).pack(side="right", padx=(0, 6))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    # —— 选项卡一：主页（版本 / 启动 / 插件）——
    def _build_home_tab(self, nb) -> None:
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="  主页  ")

        ver = ttk.LabelFrame(tab, text="DSH 版本", padding=(10, 6))
        ver.pack(fill="x")
        self.lbl_installed = ttk.Label(ver, text="本机版本：检测中…",
                                       font=("Microsoft YaHei UI", 10, "bold"))
        self.lbl_installed.pack(side="left")
        self.lbl_latest = ttk.Label(ver, text="最新版本：检测中…")
        self.lbl_latest.pack(side="left", padx=(18, 0))
        ttk.Button(ver, text="检查更新", command=self.manual_check_update).pack(side="right")

        act = ttk.Frame(tab)
        act.pack(fill="x", pady=(12, 6))
        self.btn_launch = ttk.Button(act, text="启动 Harness", command=self.on_launch)
        self.btn_launch.pack(side="left", ipadx=10, ipady=2)
        self.btn_stop = ttk.Button(act, text="停止", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        ttk.Button(act, text="打开界面", command=self.open_browser).pack(side="left", padx=(8, 0))
        ttk.Button(act, text="安全模式启动", command=self.on_safe_mode_launch).pack(side="left", padx=(8, 0))
        ttk.Label(tab, text="「安全模式启动」＝ 本次启动临时禁用全部第三方插件，"
                            "用来判断问题是不是插件引起的（不会改动你的开关设置）。",
                  foreground="#666", wraplength=780, justify="left").pack(anchor="w", pady=(0, 8))

        box = ttk.LabelFrame(tab, text="已安装插件（勾选 = 启用）", padding=8)
        box.pack(fill="both", expand=True)

        head = ttk.Frame(box)
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text="点右侧「版本…」可查看全部版本、升级或回退",
                  foreground="#666").pack(side="left")
        ttk.Button(head, text="刷新", command=self.refresh_plugins).pack(side="right")
        ttk.Button(head, text="检查全部更新", command=self.on_check_all_updates).pack(side="right", padx=(0, 6))

        canvas = tk.Canvas(box, highlightthickness=0)
        self._plugin_canvas = canvas
        # Canvas 不会自动处理鼠标滚轮，必须显式绑定（见 _on_plugin_wheel）
        canvas.bind("<MouseWheel>", self._on_plugin_wheel)
        scrollbar = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        self.plugin_frame = ttk.Frame(canvas)
        self.plugin_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.plugin_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # —— 选项卡二：备份与恢复 ——
    def _build_backup_tab(self, nb) -> None:
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="  备份与恢复  ")

        b1 = ttk.LabelFrame(tab, text="档案", padding=10)
        b1.pack(fill="x")
        ttk.Label(b1, text="重置前会自动备份；恢复前也会自动备份当前状态，随时可回退。",
                  foreground="#666").pack(anchor="w", pady=(0, 8))
        row1 = ttk.Frame(b1)
        row1.pack(fill="x")
        ttk.Button(row1, text="立即备份…", command=self.on_manual_backup).pack(side="left")
        ttk.Button(row1, text="恢复备份…", command=self.on_restore_backup).pack(side="left", padx=(8, 0))
        ttk.Button(row1, text="重置档案…", command=self.on_reset_profile).pack(side="left", padx=(8, 0))

        b2 = ttk.LabelFrame(tab, text="插件清单", padding=10)
        b2.pack(fill="x", pady=(10, 0))
        ttk.Label(b2, text="导出当前插件清单；以后可一键导入并按清单重装。",
                  foreground="#666").pack(anchor="w", pady=(0, 8))
        row2 = ttk.Frame(b2)
        row2.pack(fill="x")
        ttk.Button(row2, text="导出清单…", command=self.on_export_plugin_list).pack(side="left")
        ttk.Button(row2, text="导入并重装…", command=self.on_import_plugin_list).pack(side="left", padx=(8, 0))

        b3 = ttk.LabelFrame(tab, text="已有备份", padding=10)
        b3.pack(fill="both", expand=True, pady=(10, 0))
        self.backup_list = tk.Listbox(b3, height=8)
        sb = ttk.Scrollbar(b3, orient="vertical", command=self.backup_list.yview)
        self.backup_list.configure(yscrollcommand=sb.set)
        self.backup_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        row3 = ttk.Frame(tab)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Button(row3, text="刷新备份列表", command=self.refresh_backup_list).pack(side="right")
        self.refresh_backup_list()

    # —— 选项卡三：工具 ——
    def _build_tools_tab(self, nb) -> None:
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="  工具  ")

        b1 = ttk.LabelFrame(tab, text="DSH 本体", padding=10)
        b1.pack(fill="x")
        row1 = ttk.Frame(b1)
        row1.pack(fill="x")
        ttk.Button(row1, text="安装 / 修复 DSH", command=self.on_install_dsh).pack(side="left")
        ttk.Button(row1, text="手动启动（命令行）", command=self.on_manual_launch).pack(side="left", padx=(8, 0))
        ttk.Label(b1, text="「安装 / 修复」重装 DSH 程序本身；「手动启动」在启动器出问题时"
                           "新开命令行窗口启动。",
                  foreground="#666", wraplength=680, justify="left").pack(anchor="w", pady=(8, 0))

        b2 = ttk.LabelFrame(tab, text="插件", padding=10)
        b2.pack(fill="x", pady=(10, 0))
        row2 = ttk.Frame(b2)
        row2.pack(fill="x")
        ttk.Button(row2, text="安装插件…", command=self.on_install_plugin).pack(side="left")

        b3 = ttk.LabelFrame(tab, text="高级", padding=10)
        b3.pack(fill="x", pady=(10, 0))
        row3 = ttk.Frame(b3)
        row3.pack(fill="x")
        ttk.Button(row3, text="运行命令…", command=self.on_run_command).pack(side="left")
        ttk.Button(row3, text="导出诊断报告…", command=self.on_export_diagnostics).pack(side="left", padx=(8, 0))
        ttk.Label(b3, text="「导出诊断报告」会把版本、路径、插件清单和最近日志写成一个 txt，"
                           "方便你在求助时直接发给别人。",
                  foreground="#666", wraplength=680, justify="left").pack(anchor="w", pady=(8, 0))

        b4 = ttk.LabelFrame(tab, text="环境信息", padding=10)
        b4.pack(fill="both", expand=True, pady=(10, 0))
        self.env_text = tk.Text(b4, height=6, wrap="word", relief="flat")
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
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

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
            self._render_plugins(item[1], item[2])
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

    def _on_log_line(self, line: str) -> None:
        # 运行在后台读线程里：只入队，不碰界面
        self.log_tail.append(line)
        self._post(("log", line))
        # DSH 启动成功后会用这行打印“带 token 的认证地址”，
        # 例如：dsh web: http://127.0.0.1:3080/?token=xxx
        # 这才是浏览器真正要打开的地址（裸地址会被 401 拒绝）。
        m = re.match(r"^dsh web:\s+(\S+)", line)
        if m and self._ready_url is None:
            self._ready_url = m.group(1)

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
            plugins, libraries = list_plugins()
            self._post(("plugins", plugins, libraries))
        except Exception as exc:  # noqa: BLE001
            self._post(("plugins_error", exc))

    def _clear_plugin_frame(self) -> None:
        for child in self.plugin_frame.winfo_children():
            child.destroy()
        self.plugin_vars.clear()

    def _render_plugins(self, plugins, libraries) -> None:
        self._clear_plugin_frame()
        self._current_plugins = list(plugins)
        if not plugins and not libraries:
            ttk.Label(self.plugin_frame, text="暂未安装任何插件。\n可以在「工具」选项卡里安装插件，装好后点「刷新」。",
                      foreground="#888").pack(anchor="w", pady=10)
        self.plugin_entry_ids = {p.name: p.entry_ids for p in plugins}
        for p in plugins:
            self._render_plugin_row(p)
        if libraries:
            ttk.Separator(self.plugin_frame, orient="horizontal").pack(fill="x", pady=8)
            ttk.Label(self.plugin_frame, text="库依赖（非插件层，不影响启动，不可开关）：",
                      foreground="#888").pack(anchor="w")
            for p in libraries:
                self._render_library_row(p)
        # 行内子控件会"吃掉"滚轮事件，所以要给它们逐个补绑
        self._bind_wheel(self.plugin_frame)
        self._set_status(f"共 {len(plugins)} 个插件，{len(libraries)} 个库依赖")

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
            ttk.Label(info, text=p.description, wraplength=540, foreground="#444").pack(anchor="w")
        if p.homepage:
            link = ttk.Label(info, text="项目主页 ↗", **LINK_STYLE)
            link.pack(anchor="w")
            link.bind("<Button-1>", lambda e, url=p.homepage: webbrowser.open(url))
        if not p.entry_ids:
            ttk.Label(info, text="⚠ 未能定位该插件的启动条目，可能无法正常开关（请先“刷新列表”）",
                      foreground="#b26a00").pack(anchor="w")

    def _render_library_row(self, p) -> None:
        ttk.Label(self.plugin_frame, text=f"• {p.name}  v{p.version}",
                  foreground="#999", padding=(20, 1)).pack(anchor="w")

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
        dlg.title("插件版本")
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=pkg, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(frm, text=f"当前版本：{current}        最新版本：{newest}").pack(anchor="w", pady=(4, 4))
        if newest == current:
            ttk.Label(frm, text="当前已是最新版本。你仍然可以选择下面的任意版本进行「回退」。",
                      foreground="#1a7f37").pack(anchor="w", pady=(0, 8))
        else:
            ttk.Label(frm, text=f"有新版本可用：{newest}",
                      foreground="#1a7f37").pack(anchor="w", pady=(0, 8))

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
            foreground="#b26a00", justify="left",
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
        self._run_async(cmd, f"更新插件 {pkg} → {version}", on_done=self._after_plugin_update)

    def _after_plugin_update(self, code: int) -> None:
        if code == 0:
            messagebox.showinfo("完成", "插件更新完成，正在刷新列表。")
        else:
            messagebox.showerror("失败", f"插件更新失败（退出码 {code}），请查看下方日志。")
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
        """后台运行一条命令，输出实时打到日志面板；结束（主线程）回调 on_done(退出码)。"""
        self._append_log(f"[启动器] 运行命令：{cmd}")
        self._set_status(f"正在执行：{label} …")

        def worker():
            code = stream_command(cmd, lambda line: self._post(("log", line)))
            self._post(("cmd_done", label, code, on_done))

        threading.Thread(target=worker, daemon=True).start()

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
                        f"安装插件 {pkg}", on_done=self._after_install_plugin)

    def _after_install_plugin(self, code: int) -> None:
        if code == 0:
            messagebox.showinfo("完成", "插件安装完成，正在刷新列表。")
        else:
            messagebox.showerror("失败", f"插件安装失败（退出码 {code}），请查看下方日志。")
        self.refresh_plugins()

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
        """切换浅色 / 深色外观（含输入框、列表等非 ttk 控件）。"""
        self.dark_mode = bool(dark)
        palette = DARK_PALETTE if self.dark_mode else LIGHT_PALETTE
        bg, fg, field = palette["bg"], palette["fg"], palette["field"]
        style = ttk.Style()
        try:
            style.theme_use("clam")     # 只有 clam 主题允许自由改色
        except Exception:
            pass
        try:
            style.configure(".", background=bg, foreground=fg, fieldbackground=field)
            for name in ("TFrame", "TLabel", "TLabelframe", "TLabelframe.Label",
                         "TCheckbutton", "TRadiobutton", "TNotebook"):
                style.configure(name, background=bg, foreground=fg)
            style.configure("TButton", background=field, foreground=fg)
            style.configure("TNotebook.Tab", background=field, foreground=fg)
            style.map("TNotebook.Tab", background=[("selected", bg)])
        except Exception:
            pass
        self.root.configure(background=bg)
        for widget in (getattr(self, "log_text", None),
                       getattr(self, "backup_list", None),
                       getattr(self, "env_text", None),
                       getattr(self, "_plugin_canvas", None)):
            if widget is None:
                continue
            try:
                widget.configure(background=field, foreground=fg, insertbackground=fg)
            except Exception:
                pass

    def on_toggle_dark(self) -> None:
        self._apply_theme(self.dark_var.get())
        self.ui_settings["dark"] = self.dark_mode
        _save_ui_settings(self.ui_settings)

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
            plugins, libraries = list_plugins()
        except Exception:
            plugins, libraries = [], []
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
        lines += ["", "== 已安装插件 =="]
        if plugins:
            for p in plugins:
                lines.append(f"{p.name}  v{p.version}  [{'启用' if p.enabled else '已禁用'}]")
        else:
            lines.append("（无）")
        if libraries:
            lines += ["", "== 库依赖 =="]
            for p in libraries:
                lines.append(f"{p.name}  v{p.version}")
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
                  foreground="#888").pack(anchor="w", pady=(8, 12))
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
        dlg.title("恢复备份")
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="选择一个备份：", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        listbox = tk.Listbox(frm, height=8, width=72)
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
                  foreground="#888").pack(anchor="w", pady=(8, 12))

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
            messagebox.showinfo("完成", "插件安装完成，正在刷新列表。")
        else:
            messagebox.showerror("失败", f"安装失败（退出码 {code}），请查看下方日志。")
        self.refresh_plugins()

    # ------------------------------------------------------------------ #
    # 启动 / 停止
    # ------------------------------------------------------------------ #
    def open_browser(self) -> None:
        """打开浏览器；优先打开 DSH 打印的带 token 认证地址（裸地址会被 401 拒绝）。"""
        webbrowser.open(self._ready_url or WEB_URL)

    def on_launch(self) -> None:
        if not dsh_available():
            messagebox.showerror(
                "未检测到 dsh",
                "未检测到可用的全局 dsh 命令，无法启动 Harness。\n\n"
                "请到「工具」选项卡点【安装 / 修复 DSH】自动完成安装；\n"
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
        self._set_status(f"正在启动 Harness{suffix}…（首次启动可能需下载依赖，请稍候）")
        self.btn_launch.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._append_log(f"[启动器] 正在启动 DeepSeek Harness{suffix} …")
        try:
            self.harness.launch(patch)
        except Exception as exc:  # noqa: BLE001
            self._append_log("启动进程失败：" + str(exc))
            self._finish_launch_failed(f"启动进程失败：{exc}")
            return
        threading.Thread(target=self._wait_ready, daemon=True).start()

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
        self._append_log(f"[启动器] 服务已就绪，正在打开浏览器：{url}")
        webbrowser.open(url)
        self.starting = False
        self.btn_launch.config(state="normal")
        self.btn_stop.config(state="normal")
        try:
            save_last_good()      # 记下“这次是好的”，以后出问题能一键回退
            self._append_log("[启动器] 已记录本次为「上次正常配置」。")
        except Exception:
            pass

    def _on_launch_failed(self, code) -> None:
        self.starting = False
        self.btn_launch.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._set_status("启动失败 ✗ 请查看错误信息，关闭可疑插件后重试")
        tail = "\n".join(self.log_tail) or "（无日志输出）"
        self._show_failed_dialog(code, tail)

    def _on_launch_timeout(self) -> None:
        self.starting = False
        self.btn_launch.config(state="normal")
        self.btn_stop.config(state="normal")
        self._set_status("启动超时，可能仍在初始化，请看日志")
        self._append_log("[启动器] 等待服务就绪超时（仍可点击“打开浏览器”手动访问）")

    def _finish_launch_failed(self, msg: str) -> None:
        self.starting = False
        self.btn_launch.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._set_status("启动失败 ✗")
        self._show_failed_dialog(None, msg)

    def _show_failed_dialog(self, code, detail: str) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("启动失败")
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        head = "Harness 启动失败"
        if code is not None:
            head += f"（退出码 {code}）"
        ttk.Label(frm, text=head, font=("Microsoft YaHei UI", 11, "bold"), foreground="#c00").pack(anchor="w")
        ttk.Label(frm, text="下方是错误信息。请在主窗口关闭可疑插件后，重新点击“启动 Harness”。",
                  wraplength=460).pack(anchor="w", pady=(6, 8))
        txt = scrolledtext.ScrolledText(frm, height=14, width=64, wrap="word")
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

    def on_stop(self) -> None:
        self._append_log("[启动器] 正在停止 …")
        self.harness.stop()
        self._on_stopped()

    def _on_stopped(self) -> None:
        self.starting = False
        self.btn_launch.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._set_status("已停止")
        self._append_log("[启动器] 已停止。")

    def _on_close(self) -> None:
        """关闭窗口前先提醒：关闭启动器会同时停止正在运行的 Harness。"""
        if self.harness.is_running():
            if not messagebox.askyesno(
                "确认关闭",
                "Harness 正在运行中。\n\n"
                "关闭启动器会同时停止 Harness，导致正在执行的任务中断。\n"
                "确定要关闭吗？",
            ):
                return  # 用户点了“否”，取消关闭，继续运行
            try:
                self.harness.stop()
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
