# -*- coding: utf-8 -*-
"""make_screenshots.py —— 给启动器截图（用于 README 与界面自查）。

用法：``python tools/make_screenshots.py``
产物写到 ``screenshots/`` 下。只截启动器自己的窗口区域，
不会把整个桌面（其它窗口）拍进去。

实现方式：tkinter 负责把窗口开出来并摆好，真正的抓屏交给 PowerShell + .NET
（``System.Drawing``）—— 这样不用引入任何第三方 Python 库。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import stat
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import launcher as L  # noqa: E402

OUT_DIR = ROOT / "screenshots"
# 真正画出来的界面截图起码这么大；明显更小就说明抓到的是空白/旧图
MIN_SHOT_BYTES = 15000
SHOTS = (
    # (文件名, 选项卡下标, 是否深色, 是否用"有问题"的示例数据)
    ("01-home.png", 0, False, False),
    ("02-maintain.png", 1, False, False),
    ("03-home-dark.png", 0, True, False),
    ("04-diagnostics.png", 0, False, True),
)


def window_rect(root: tk.Tk):
    """取窗口（含标题栏、不含窗口阴影）在屏幕上的矩形，用于只截这一块。

    用 ``GetWindowRect`` 会把 Win10 那圈**不可见的边框**也算进来，于是截图边缘会
    混进后面窗口的内容；``DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)``
    给的才是肉眼看到的边框。
    """
    try:
        hwnd = int(root.wm_frame(), 16)
    except Exception:
        hwnd = root.winfo_id()
    rect = ctypes.wintypes.RECT()
    try:
        # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(hwnd), ctypes.c_uint(9),
            ctypes.byref(rect), ctypes.sizeof(rect),
        )
        if ok != 0:
            raise OSError(ok)
    except Exception:
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", ctypes.wintypes.DWORD * 3)]


def _write_png(path: Path, width: int, height: int, bgra: bytes) -> None:
    """把 BGRA 原始像素写成 PNG（纯标准库：zlib + struct）。"""
    import struct
    import zlib

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)                                  # 每行前面要有一个 filter byte
        row = bytearray(bgra[y * stride:(y + 1) * stride])
        row[0::4], row[1::4], row[2::4] = row[2::4], row[1::4], row[0::4]   # BGRA → RGBA
        raw += row

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def grab_offscreen(root: tk.Tk, path: Path):
    """把窗口**直接渲染进内存位图**，窗口放在屏幕外也抓得到。

    这样截图时用户完全看不到任何窗口、也不会被抢焦点（老办法是"显示出来再截屏"，
    会打扰正在用电脑的人）。返回 ``(是否成功, 说明)``。
    """
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    try:
        hwnd = int(root.wm_frame(), 16)
    except Exception:
        hwnd = root.winfo_id()

    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return False, "窗口尺寸无效"

    hdc = user32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    old = gdi32.SelectObject(memdc, bitmap)
    buf = None
    try:
        ok = user32.PrintWindow(hwnd, memdc, 2)   # 2 = PW_RENDERFULLCONTENT
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height         # 负数 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0          # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        got = gdi32.GetDIBits(memdc, bitmap, 0, height, buf, ctypes.byref(info), 0)
    finally:
        gdi32.SelectObject(memdc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(hwnd, hdc)

    if not ok or got == 0 or buf is None:
        return False, "PrintWindow/GetDIBits 失败"
    data = buf.raw
    if data.count(b"\x00") > len(data) * 0.98:     # 几乎全黑 = 没渲染出来
        return False, "抓到的画面是空白"
    _write_png(path, width, height, data)
    return True, ""


def grab_visible(root: tk.Tk, path: Path) -> bool:
    """把窗口显示在屏幕上截屏。

    为什么不用离屏的 ``PrintWindow``：试过了——Tk 的控件内容是 GDI 直接画的，
    ``PrintWindow``（flag 0/2/3、无论抓框架还是抓客户区）都只能抓到标题栏，
    客户区一片空白。所以只能让它短暂出现在屏幕上。
    为了尽量少打扰，调用方只在抓图那一刻显示窗口，抓完立刻隐藏。
    """
    x, y, w, h = window_rect(root)
    script = (
        "Add-Type -AssemblyName System.Drawing;"
        f"$bmp = New-Object System.Drawing.Bitmap {w}, {h};"
        "$g = [System.Drawing.Graphics]::FromImage($bmp);"
        f"$g.CopyFromScreen({x}, {y}, 0, 0, $bmp.Size);"
        f"$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png);"
        "$g.Dispose(); $bmp.Dispose()"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0 or not path.is_file():
        return False
    # 空图检测：真正画出来的界面截图起码几十 KB；空白图只有几 KB。
    # （之前就吃过亏——旧图没被覆盖，我误以为"截图成功"）
    return path.stat().st_size >= MIN_SHOT_BYTES


def _prepare_target(path: Path) -> bool:
    """写之前先删掉旧图（老截图可能是只读的，先摘掉只读位）。"""
    try:
        if path.exists():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            path.unlink()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"    无法删除旧图（{exc}）")
        return False

        return False
    return True


def _demo_inventory():
    """读不到真实插件时用的示例数据（只用于截图排版）。"""
    import plugins

    def make(name, version, desc, ids):
        return plugins.Plugin(name=name, version=version, description=desc,
                              homepage="", is_bundle=True, entry_ids=ids, enabled=True)

    return plugins.Inventory(
        plugins=[
            make("@lalilulelo3/dsh-notify", "0.2.1", "任务完成时发通知", ["dsh-notify"]),
            make("@nanmicoder/dsh-agent-teams", "0.1.17-rc.1",
                 "多智能体协作：按角色分工、并行推进", ["agent-teams"]),
            make("dsh-better-sidebar", "0.19.1", "更好用的侧边栏", ["better-sidebar"]),
            make("dsh-sessions-manager", "3.7.1", "会话管理与清理", ["dsh-sessions-manager"]),
        ],
        unmounted=[],
        libraries=[],
        dump_error="",
    )


def _demo_problem_inventory():
    """示例数据：一个「已安装但未挂载」的插件 + 一次条目解析失败。

    用来展示启动器的诊断能力（红色分组、修复入口、顶部告警）。
    """
    import plugins

    base = _demo_inventory()
    unmounted = plugins.Plugin(
        name="dsh-better-sidebar", version="0.19.1",
        description="更好用的侧边栏（声明了插件层，但档案里没有登记）",
        homepage="", is_bundle=False, entry_ids=[], enabled=True,
        declares_bundle=True,
    )
    libs = [plugins.Plugin(name="clsx", version="2.1.1", description="被插件依赖的工具库",
                           homepage="", is_bundle=False, entry_ids=[], enabled=True)]
    return plugins.Inventory(
        plugins=base.plugins[:3],
        unmounted=[unmounted],
        libraries=libs,
        dump_error="dsh web --dump-config 退出码 1：EPERM: operation not permitted, "
                   "open 'C:\\Users\\…\\profiles\\web\\cordis.yml'",
    )


def main() -> int:
    # 截图时不要弹任何对话框
    for fn in ("askyesno", "showinfo", "showerror", "showwarning"):
        setattr(messagebox, fn, lambda *a, **k: True)

    def quiet_versions(self, installed, latest, auto):
        # 只更新版本标签，**不要**顺手刷新插件列表：刷新会执行
        # `dsh web --dump-config`，而那条命令会往**正在运行的档案目录**里写
        # cordis.yml —— 截图脚本绝不该碰用户正在跑的会话。截图的插件数据
        # 全部来自下面的示例数据。
        self.lbl_installed.config(text=f"本机版本：{installed or '未知'}")
        self.lbl_latest.config(text=f"最新版本：{latest or '未知'}")

    L.App._on_versions = quiet_versions

    # 截深色图时 on_toggle_dark() 会把「深色」写进用户的界面设置，
    # 所以先把原文件留一份，结束时原样还回去（不给用户留下副作用）。
    settings_path = L.ui_settings_path()
    original_settings = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else None

    root = tk.Tk()
    app = L.App(root)
    # 一开始就藏起来：只在抓图那一瞬间显示，抓完立刻再藏起来，尽量少打扰
    root.withdraw()
    # 截图里别显示"检测中…"：直接取一次真实版本号填上（只跑 dsh --version / npm view）
    try:
        app.lbl_installed.config(text=f"本机版本：{L.get_installed_version() or '未知'}")
        app.lbl_latest.config(text=f"最新版本：{L.get_latest_version() or '未知'}")
    except Exception:
        pass

    real_inventory = _demo_inventory()
    print("插件列表条目：", len(real_inventory.plugins))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, tab, dark, problems in SHOTS:
        app.dark_var.set(dark)
        app.on_toggle_dark()
        app.nb.select(tab)
        app._render_plugins(_demo_problem_inventory() if problems else real_inventory)
        # 隐藏状态下先把布局算完，避免显示出来时还在重排
        root.geometry("960x820+60+30")
        root.update()
        if problems and getattr(app, "_plugin_canvas", None) is not None:
            # 诊断那张图要能看到下方的「未挂载」红色分组，所以滚到底
            app._plugin_canvas.yview_moveto(1.0)
            root.update()
        path = OUT_DIR / name
        if not _prepare_target(path):
            ok = False
            continue
        # —— 显示 → 抓图 → 立刻隐藏（大约 0.5 秒）——
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.update()
        time.sleep(0.4)
        grabbed = grab_visible(root, path)
        root.attributes("-topmost", False)
        root.withdraw()
        root.update()
        if grabbed:
            print(f"  ✓ {name}  ({path.stat().st_size:,} 字节)")
        else:
            ok = False
            print(f"  ✗ {name} 截图失败或抓到空白")
    print("说明：抓图时窗口会短暂出现在屏幕上（每张约 0.5 秒），这是 Tk 离屏抓不了图的妥协。")

    root.destroy()
    # 把界面设置还原成截图之前的样子
    try:
        if original_settings is None:
            settings_path.unlink(missing_ok=True)
        else:
            settings_path.write_text(original_settings, encoding="utf-8")
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
