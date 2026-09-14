# -*- coding: utf-8 -*-
"""core.py —— 与 DeepSeek Harness / Node 工具链交互的底层封装。

本文件负责：
1. 找到 DSH 的“家目录”和 web 档案目录；
2. 调用全局 ``dsh`` 命令与 ``npm``（版本检测、导出插件树、启动 / 停止 / 更新）；
3. 提供统一的命令执行、输出捕获和端口检测。

背景知识（写给阅读者）：
- DeepSeek Harness 的所有用户数据都在一个“家目录”下：默认是 ``~/.dsh``，
  也可用环境变量 ``DSH_HOME`` 指定到别处。
- web 界面对应其中的 ``profiles/web`` 这个“档案（profile）”。
- 用户安装的插件记录在 ``profiles/web/package.json`` 的 ``dependencies`` 字段里，
  实际文件装在 ``profiles/web/node_modules/`` 下。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

# DSH 家目录环境变量名
DSH_HOME_ENV = "DSH_HOME"
# web 档案名
WEB_PROFILE = "web"
# DSH 网页服务端口（与你批处理里打开的地址一致）
WEB_PORT = 3080
WEB_URL = f"http://127.0.0.1:{WEB_PORT}"


def dsh_home() -> Path:
    """解析 DSH 家目录：优先 ``$DSH_HOME``，否则 ``~/.dsh``。"""
    env = os.environ.get(DSH_HOME_ENV, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".dsh").resolve()


def profile_dir() -> Path:
    """web 档案目录：``<home>/profiles/web``。"""
    return dsh_home() / "profiles" / WEB_PROFILE


def profile_package_json() -> Path:
    """web 档案的 package.json（记录已装插件与插件层列表）。"""
    return profile_dir() / "package.json"


def launcher_dir() -> Path:
    """启动器自己的数据目录（放在 DSH 家目录下，便于随 DSH 一起备份）。"""
    return dsh_home() / "launcher"


def disabled_patch_path() -> Path:
    """启动器生成的“禁用插件”补丁文件，启动时通过 ``--patch`` 传给 dsh。"""
    return launcher_dir() / "disabled.patch.yml"


def safe_mode_patch_path() -> Path:
    """“安全模式”临时补丁：一次性禁用全部第三方插件，不改动用户的开关设置。"""
    return launcher_dir() / "safe-mode.patch.yml"


def ui_settings_path() -> Path:
    """启动器自己的界面设置（窗口大小、深浅色等）。"""
    return launcher_dir() / "ui-settings.json"


def last_good_path() -> Path:
    """“上次正常启动”的插件配置快照。"""
    return launcher_dir() / "last-good.json"


def _quote(s: str) -> str:
    """给命令参数加双引号（Windows cmd 下最稳妥的引号处理）。"""
    return '"' + str(s).replace('"', '\\"') + '"'


def dsh_cmd() -> str:
    """返回调用全局 ``dsh`` 的命令字符串（已带引号）。

    优先直接使用全局安装目录里的 ``dsh.cmd`` **完整路径**：Windows 上从资源管理器
    双击启动的进程，继承到的 PATH 未必完整（这是很常见的坑），按完整路径找才稳。
    找不到时再退回 PATH 里的 ``dsh``。
    """
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        candidate = Path(appdata) / "npm" / "dsh.cmd"
        if candidate.is_file():
            return _quote(str(candidate))
    found = shutil.which("dsh")
    if found:
        return _quote(found)
    return "dsh"


def child_env() -> dict:
    """构造给子进程用的环境变量。

    打包成 exe（PyInstaller）运行时，它会把 ``TCL_LIBRARY`` / ``TK_LIBRARY`` 指到
    自己的临时解压目录（形如 ``...\\_MEIxxxxxx\\_tcl_data``）。这些是**打包器内部路径**，
    绝不该传给子进程——否则从启动器里启动的 dsh、命令行窗口，以及它们的后代进程
    （比如 AI 的 shell）都会指着一个可能已被删除的目录，导致任何 Python + tkinter
    的程序报 ``Can't find a usable init.tcl``。

    这里只清理"指向 _MEI 目录"的那种值，用户自己正经设置的不动。
    """
    env = dict(os.environ)
    for key in ("TCL_LIBRARY", "TK_LIBRARY"):
        if "_MEI" in env.get(key, ""):
            env.pop(key, None)
    return env


def run_command(cmd: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """同步执行一条命令（经 cmd 解释），返回带 stdout/stderr 的 CompletedProcess。

    在 Windows 上 ``npm`` / ``npx`` / ``pnpm`` 都是 .cmd 脚本，必须走 shell 才能调用。
    超时会抛 ``subprocess.TimeoutExpired``，由调用方决定如何处理。
    """
    return subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=child_env(),
    )


def _first_nonempty_line(text: str) -> str | None:
    """取一段输出里第一行非空内容（版本号通常就在这里）。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return None


def dsh_available() -> bool:
    """检测全局 ``dsh`` 命令是否可用（是否已 ``npm install -g @deepseek-ai/dsh``）。"""
    try:
        proc = run_command(f"{dsh_cmd()} --version", timeout=30)
        return proc.returncode == 0
    except Exception:
        return False


def get_installed_version(timeout: float = 90.0) -> str | None:
    """本机当前 dsh 版本（读取全局安装的 ``dsh`` 命令）。失败返回 None。

    启动器统一用全局 ``dsh`` 来启动和查版本，这样“本机版本”指的就是
    真正会被启动的那一份，更新后也会随之变化。
    """
    try:
        proc = run_command(f"{dsh_cmd()} --version", timeout=timeout)
        if proc.returncode != 0:
            return None
        return _first_nonempty_line(proc.stdout)
    except Exception:
        return None


def get_latest_version(timeout: float = 60.0) -> str | None:
    """npm 仓库里的最新版本。失败返回 None。"""
    try:
        proc = run_command("npm view @deepseek-ai/dsh version", timeout=timeout)
        return _first_nonempty_line(proc.stdout)
    except Exception:
        return None


def get_package_versions(pkg: str, timeout: float = 90.0) -> list:
    """查询 npm 上某个插件的全部版本及发布时间。

    返回 ``[(版本号, 发布时间ISO文本), ...]``，按发布时间**从新到旧**排序；
    查询失败、或该包不在 npm 上时返回空列表。

    为什么要把"时间"也取回来：pnpm 11 默认会跳过发布时间不满 24 小时的版本
    （`minimumReleaseAge` 供应链保护），所以界面上要能看出哪一版"太新"。
    """
    quoted = _quote(pkg)
    try:
        vproc = run_command(f"npm view {quoted} versions --json", timeout=timeout)
    except Exception:
        return []
    if vproc.returncode != 0:
        return []
    try:
        versions = json.loads(vproc.stdout or "[]")
    except Exception:
        return []
    if isinstance(versions, str):      # 只有一个版本时 npm 返回裸字符串
        versions = [versions]
    if not isinstance(versions, list):
        return []

    times: dict = {}
    try:
        tproc = run_command(f"npm view {quoted} time --json", timeout=timeout)
        parsed = json.loads(tproc.stdout or "{}")
        if isinstance(parsed, dict):
            times = parsed
    except Exception:
        times = {}

    items = [(str(v), str(times.get(str(v)) or "")) for v in versions]
    items.reverse()                                   # npm 通常按从旧到新返回，先反过来
    items.sort(key=lambda item: item[1], reverse=True)  # 有发布时间就按时间排（稳定排序）
    return items


def update_dsh(timeout: float = 300.0) -> bool:
    """更新 DSH：把全局安装的 ``dsh`` 升级到最新版。返回是否成功。

    启动器统一用全局 ``dsh`` 启动，所以“更新”就是真正地
    ``npm install -g @deepseek-ai/dsh@latest``，更新后启动的就是新版本。
    """
    try:
        proc = run_command("npm install -g @deepseek-ai/dsh@latest", timeout=timeout)
        return proc.returncode == 0
    except Exception:
        return False


def dump_config(timeout: float = 90.0) -> str | None:
    """导出当前 web 档案“组合后”的插件树（不启动服务，只打印配置）。

    这是启动器最重要的诊断能力：即使某个插件坏到让 DSH 启动不起来，
    只要它的配置本身还能被解析，这个命令通常仍能给出完整的插件条目清单
    （每个条目的 id、模块名、是否禁用）。失败返回 None。
    """
    try:
        proc = run_command(f"{dsh_cmd()} web --dump-config", timeout=timeout)
        if proc.returncode != 0:
            return None
        return proc.stdout or ""
    except Exception:
        return None


def is_port_open(port: int = WEB_PORT, timeout: float = 1.0) -> bool:
    """检测 127.0.0.1:<port> 是否有服务在监听。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def stream_command(cmd: str, line_callback, timeout: float | None = None) -> int:
    """执行一条命令，把输出**逐行实时**回调出去（用于安装等耗时命令）。返回退出码。

    与 ``run_command`` 的区别：这个适合需要边跑边看输出的长任务，
    例如 ``npm install -g @deepseek-ai/dsh@latest`` 或安装插件。
    """
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # 合并 stderr，保证错误也能看到
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=child_env(),
    )
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                try:
                    line_callback(line.rstrip("\n"))
                except Exception:
                    pass
    except Exception:
        pass
    try:
        return proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return -1


def launch_in_console(cmd: str, title: str = "DeepSeek Harness") -> None:
    """新开一个命令行窗口执行命令（“手动 / 兜底启动”用）。

    不捕获输出、不托管进程——就是把控制台原原本本交还给用户，
    相当于你以前双击的那个批处理。
    """
    subprocess.Popen(f'start "{title}" cmd /k "{cmd}"', shell=True, env=child_env())


class HarnessProcess:
    """管理 DSH web 进程的生命周期：启动、流式读日志、停止。"""

    def __init__(self, log_callback=None):
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._log_callback = log_callback

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def launch(self, patch_file: Path | None) -> None:
        """启动 ``dsh web``（可选附加禁用补丁）。日志通过 log_callback 逐行回调。"""
        if self.is_running():
            return
        parts = [dsh_cmd(), "web"]
        if patch_file is not None and patch_file.exists():
            parts += ["--patch", _quote(str(patch_file))]
        # DSH web 默认会自动打开一次浏览器；这里用 --no-open 关掉它，
        # 改由启动器在确认服务就绪后只打开一次，避免出现两个相同标签页。
        parts += ["--no-open"]
        cmd = " ".join(parts)

        self.proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # 合并错误输出到同一个流
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,                  # 行缓冲，便于实时看到日志
            env=child_env(),            # 不把打包器的 Tcl 临时路径泄漏给 dsh
        )
        self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self._reader_thread.start()

    def _read_output(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                if self._log_callback is not None:
                    self._log_callback(line.rstrip("\n"))
        except Exception:
            # 读管道出错（例如进程被我们主动杀掉）不必抛出
            pass

    def stop(self) -> None:
        """停止 DSH（连同它的子进程一起）。"""
        if self.proc is None:
            return
        pid = self.proc.pid
        # /T 表示杀掉整棵进程树（cmd -> npx -> node），/F 强制
        try:
            subprocess.run(
                f"taskkill /PID {pid} /T /F",
                shell=True,
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        self.proc = None
