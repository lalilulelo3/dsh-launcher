# -*- coding: utf-8 -*-
"""browser.py —— 用「应用窗口」方式打开网页，并让启动器能把它关掉。

需求背景：用户希望**关闭启动器时把 DSH 的网页也一起关掉**。
浏览器不允许外部程序关掉任意标签页，所以这里换一条路：用 Chromium 系浏览器的
「应用窗口」模式（``--app=<url>``）配合**独立的用户数据目录**打开页面——

- ``--app`` 让页面以独立窗口出现（没有地址栏、没有标签栏，像个 App）；
- ``--user-data-dir`` 才是关键：它强制浏览器为这个目录**另起一个浏览器进程**，
  而不是把请求转交给已经在跑的那个浏览器。于是这个进程的**命令行里带着我们的目录名**，
  可以据此精确地找到并关掉它 —— 绝不会误伤用户自己开的浏览器窗口。

代价：这个窗口使用独立配置（没有用户平时的扩展、书签、登录态）。DSH 网页靠 URL 里的
token 认证，不依赖 cookie，所以不影响使用。

用户若用的不是 Chromium 系浏览器，就退回 ``webbrowser.open``，此时**无法**自动关闭，
:meth:`AppWindow.open` 会明确说明这一点，由界面提示用户。

⚠ **关窗口时的铁律（踩过一次大坑）**：只按「命令行里含我们的配置目录」去找进程是不够的，
因为**这条查询命令自己的命令行里也含这个字符串**，而 DSH 的每个工具调用都跑在
``node runner.js -- powershell -Command "..."`` 这样的 subprocess runner 里 ——
结果那个 runner 被当成浏览器杀掉，宿主随即因为收不到子进程输出而崩溃退出，
**所有会话一起断掉**。所以现在必须叠加「映像名白名单」这一层（见
:data:`_BROWSER_IMAGES` 与 :func:`looks_like_our_window`），node / powershell / cmd
一律不碰；并且没开过窗口时连扫都不扫。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

from core import child_env, launcher_dir, no_window_flags

# Chromium 系浏览器的常见安装位置（按优先级）
_CHROMIUM_CANDIDATES = (
    ("Chrome", Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")),
    ("Chrome", Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")),
    ("Edge", Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")),
    ("Edge", Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")),
)

# 注册表里 ProgId → 浏览器族
_PROGID_FAMILY = {
    "chromehtml": "chrome",
    "msedgehtm": "edge",
}

# 允许被「按命令行关掉」的进程映像名白名单。
# 只认浏览器：绝不能把 node / powershell / cmd 之类纳入匹配范围——
# 详见 AppWindow._browser_processes 里的说明（曾经因此把 DSH 宿主搞崩过）。
_BROWSER_IMAGES = ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe", "vivaldi.exe")


def looks_like_our_window(image_name: str, marker: str) -> bool:
    """纯判断：这个映像名是否属于「浏览器白名单」。

    单独抽出来是为了**能被单元测试**——不需要真的去扫进程、更不需要杀任何东西。
    （只按命令行匹配、不限定映像名，曾把 DSH 自己的 subprocess runner 当成浏览器杀掉，
    导致宿主崩溃、所有会话断开。）
    """
    name = (image_name or "").strip().lower()
    if name not in _BROWSER_IMAGES:
        return False
    return bool((marker or "").strip())


def default_browser_family() -> str:
    """从注册表读默认浏览器的「族」：``chrome`` / ``edge`` / 其它返回空串。

    只用来决定优先用哪一个；读不到也不影响（下面还有常见路径兜底）。
    """
    try:
        import winreg  # 仅 Windows 有
        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            prog_id = str(winreg.QueryValueEx(handle, "ProgId")[0] or "").lower()
        for token, family in _PROGID_FAMILY.items():
            if token.replace(" ", "") in prog_id.replace(" ", ""):
                return family
    except Exception:
        pass
    return ""


def find_chromium() -> tuple[str, str]:
    """找一个可用的 Chromium 系浏览器，返回 ``(可执行文件路径, 名称)``。

    优先用户默认的那一族；找不到任何 Chromium 系浏览器时返回 ``("", "")``。
    """
    family = default_browser_family()
    ordered = sorted(
        _CHROMIUM_CANDIDATES,
        key=lambda item: 0 if family and item[0].lower() == family else 1,
    )
    for name, path in ordered:
        if path.is_file():
            return str(path), name
    # 兜底：PATH 里找
    for exe, name in (("chrome", "Chrome"), ("msedge", "Edge")):
        found = shutil.which(exe)
        if found:
            return found, name
    return "", ""


_WNDENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
)

SW_MAXIMIZE = 3


def _find_main_window(pid: int) -> int:
    """找出属于该进程的第一个「有标题的可见顶层窗口」，返回 hwnd（找不到返回 0）。"""
    user32 = ctypes.windll.user32
    found = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        owner = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found[0] if found else 0


class AppWindow:
    """由启动器持有的那个浏览器「应用窗口」。"""

    def __init__(self, profile_dir: Path | None = None, log=None):
        self.profile_dir = Path(profile_dir) if profile_dir else (launcher_dir() / "browser-profile")
        self._proc: subprocess.Popen | None = None
        self.owned = False         # 是否由我们启动（True 才能自动关）
        self._ever_opened = False  # 是否真的开过窗口：没开过就绝不扫进程、更不杀
        self.browser_name = ""
        self.maximized: bool | None = None   # 自动最大化是否成功（None = 还没结果）
        self._log = log or (lambda _msg: None)

    # ---------------------------------------------------------------- #
    def is_open(self) -> bool:
        """我们启动的那个窗口是否还活着（只看自己启动的进程，不做全表扫描）。"""
        return self._proc is not None and self._proc.poll() is None

    def open(self, url: str, as_app_window: bool = True) -> tuple[bool, str]:
        """打开网页。

        ``as_app_window=True``（默认）：用 Chrome / Edge 的「应用窗口」打开，启动器**持有**
        这个窗口，因此之后能把它一起关掉。
        ``as_app_window=False``：交给系统默认浏览器开一个普通标签页 —— 和用户平时的浏览
        体验一致，但**启动器管不到它**（浏览器不允许外部程序关标签页），所以关闭启动器时
        不会（也无法）关掉它。

        返回 ``(是否由启动器接管, 给用户看的一句话)``。
        """
        if not as_app_window:
            webbrowser.open(url)
            self.owned = False
            return False, "已用系统默认浏览器打开普通标签页（这种方式启动器关不掉它）"

        exe, name = find_chromium()
        if not exe:
            webbrowser.open(url)
            self.owned = False
            return False, "已用系统默认浏览器打开（未能识别到 Chrome / Edge，无法自动关闭该页面）"

        # 之前的窗口如果还在，先关掉——token 每次启动都会变，旧页面已经失效
        if self.is_open():
            self.close()

        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        cmd = [
            exe,
            f"--app={url}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter",
            # 这个开关对**普通窗口**有效，但对 --app 应用窗口实测会被忽略，
            # 所以真正靠得住的是下面 _maximize_when_ready() 里的 Win32 调用。
            "--start-maximized",
            "--window-size=1280,900",   # 兜底：至少不会太小
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_env(),
                creationflags=no_window_flags(),   # 浏览器是 GUI 程序，加了也无害
            )
        except Exception as exc:  # noqa: BLE001
            webbrowser.open(url)
            self.owned = False
            return False, f"启动 {name} 应用窗口失败（{exc}），已退回系统默认浏览器"

        self.owned = True
        self._ever_opened = True
        self.browser_name = name
        self.maximized = None
        # 最大化放到后台线程里做：窗口要几秒才出来，不能让这里阻塞界面
        threading.Thread(
            target=self._maximize_when_ready, args=(self._proc.pid,), daemon=True
        ).start()
        return True, f"已用 {name} 应用窗口打开（关闭启动器时会一并关闭）"

    # ---------------------------------------------------------------- #
    def _maximize_when_ready(self, pid: int, timeout: float = 15.0) -> bool:
        """等浏览器把窗口开出来，然后用 Win32 把它最大化。

        实测（1920×1080 / Edge）：``--start-maximized`` 对**普通窗口**有效，
        但对 **``--app`` 应用窗口**会被忽略 —— 所以必须自己动手。
        窗口属于我们 Popen 起来的那个进程（同一 PID），据此精确找到句柄，
        不会动到用户自己的浏览器窗口。
        """
        user32 = ctypes.windll.user32
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                self.maximized = False
                return False
            hwnd = _find_main_window(pid)
            if hwnd:
                try:
                    user32.ShowWindow(hwnd, SW_MAXIMIZE)
                    self.maximized = True
                    self._log("已把应用窗口最大化（可用窗口右上角按钮还原）")
                except Exception as exc:  # noqa: BLE001
                    self.maximized = False
                    self._log(f"自动最大化失败（{exc}），可以自己拖大窗口")
                return bool(self.maximized)
            time.sleep(0.25)
        self.maximized = False
        self._log("没等到应用窗口出现，未能自动最大化（可以自己拖大）")
        return False

    # ---------------------------------------------------------------- #
    def _browser_processes(self) -> list:
        """列出「有可能是我们那个浏览器窗口」的进程，返回 ``[(pid, 映像名)]``。

        **为什么必须限定映像名（这是血泪教训）**：这里只能拿命令行做匹配，
        而用来匹配的那个字符串会出现在**我们自己的查询命令里**。曾经有一版
        只按「命令行里含这个目录」来找进程，结果把 **DSH 自己的子进程 runner**
        也匹配上了——它的命令行长这样::

            node .../dsh-subprocess-local/lib/runner.js -- powershell -Command "...<标记>..."

        于是我们照着把 runner 杀了；runner 一死，它的临时目录被清掉，而 DSH 宿主
        还在给这个子进程收输出、要往那个目录里写 stdout 落盘文件 →
        ``ENOENT ... stdout.log`` 未捕获异常 → **整个宿主退出，所有会话一起断**。

        所以现在的规则是：**只认浏览器进程的映像名**，node / powershell / cmd
        一律不碰；命中之后还会再用 :func:`looks_like_our_window` 复核一遍。
        """
        marker = str(self.profile_dir)
        names = ",".join(f"'{n}'" for n in _BROWSER_IMAGES)
        script = (
            "$names = @(" + names + ");"
            "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue"
            " | Where-Object { $names -contains $_.Name -and $_.CommandLine"
            " -and $_.CommandLine.Contains('" + marker + "') }"
            " | ForEach-Object { \"$($_.ProcessId)`t$($_.Name)\" }"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=25, env=child_env(),
                creationflags=no_window_flags(),
            )
        except Exception:
            return []
        found = []
        for line in (proc.stdout or "").splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[0].strip().isdigit():
                found.append((int(parts[0]), parts[1].strip()))
        return found

    def owned_window_pids(self) -> list:
        """我们那个浏览器窗口的进程号（已按映像名与当前进程过滤）。"""
        mine = {os.getpid()}
        try:
            mine.add(os.getppid())
        except Exception:
            pass
        return [pid for pid, name in self._browser_processes()
                if pid not in mine and looks_like_our_window(name, str(self.profile_dir))]

    def close(self, deep: bool | None = None) -> bool:
        """关掉我们启动的浏览器窗口（连同它拉起的子进程）。返回是否确实关了东西。

        只关两类进程：① 我们自己 ``Popen`` 起来的那个浏览器；② 命令行里带着
        我们专属配置目录、且**映像名确实是浏览器**的残留进程。
        绝不按命令行内容去杀其它程序（原因见 :meth:`_browser_processes`）。

        **为什么要区分"深扫"**：第 ② 步要跑一次 PowerShell 进程表扫描，实测 1~3 秒。
        用户反馈过「点 X 后独立窗口立刻关了，但启动器自己还要等一会才退」—— 等的那一会
        就是它。所以现在：

        1. 先杀我们自己启动的那个进程（几十毫秒），并最多等 0.6 秒让它真的退出；
        2. **只有它没死透**（句柄失效 / 有残留）才去做进程表扫描；
        3. 确实需要强制扫描时可以传 ``deep=True``。

        另外：没开过窗口就直接返回，连进程表都不扫。
        """
        if not self._ever_opened and self._proc is None:
            return False
        closed = False
        died = True
        if self._proc is not None:
            proc = self._proc
            if proc.poll() is None:
                closed = _taskkill(proc.pid) or closed
                for _ in range(6):            # 最多等 0.6 秒
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
            died = proc.poll() is not None
            self._proc = None
        if deep is None:
            deep = not died
        if deep and self._ever_opened:
            for pid in self.owned_window_pids():
                closed = _taskkill(pid) or closed
        self.owned = False
        self._ever_opened = False
        return closed


def _taskkill(pid: int) -> bool:
    """强杀一棵进程树。

    ⚠ 调用前必须确认目标**确实是浏览器窗口**（见 :func:`looks_like_our_window`）。
    往这里塞任意 PID 是危险的：DSH 的每个工具调用都跑在自己的 subprocess runner 里，
    杀掉 runner 会让宿主收不到它的输出管线、甚至整个宿主崩掉。
    """
    try:
        proc = subprocess.run(
            f"taskkill /PID {int(pid)} /T /F",
            shell=True, capture_output=True, timeout=20, env=child_env(),
            creationflags=no_window_flags(),
        )
        return proc.returncode == 0
    except Exception:
        return False


def endpoint_label(url: str) -> str:
    """给日志用：去掉 token 的地址（避免把认证令牌写进日志、复制给别人时泄露）。"""
    return url.split("?", 1)[0] if url else ""
