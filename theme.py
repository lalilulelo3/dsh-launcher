# -*- coding: utf-8 -*-
"""theme.py —— 启动器的统一外观（调色板 + 全部控件样式）。

为什么单独一个模块：界面「灰一块白一块」的根因不是颜色选得不好，而是**只配了一半**——
ttk 控件走 clam 主题的自定义色，而滚动条、输入框、树形列表、画布仍是系统默认灰，
再加上 tk 原生控件（Text / Listbox / Canvas）默认是白底，几种来源的灰色和白色拼在一起，
就有了补丁感。

这里的三条原则：

1. **一份调色板管到底**：ttk 里用到的每个样式类都显式配置，不留默认；
2. **tk 原生控件统一上色**（见 :func:`paint`），它们不吃 ttk 样式；
3. **层次靠边框和留白，不靠色块对比**：窗口底色 ``bg``、卡片 ``surface``、输入面 ``field``
   三者只差一点点，卡片用 1px 边框和间距区分。

用法::

    import theme
    palette = theme.apply(root, dark=self.dark_mode)   # 配置全局样式，返回当前调色板
    theme.paint(self.log_text, palette)                # 给原生控件上色
    theme.paint_dialog(dlg, palette)                   # 给 Toplevel 上色
"""
from __future__ import annotations

from tkinter import ttk

# 界面字体：中文用雅黑，日志用等宽
FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

PALETTES = {
    "light": {
        "bg": "#F1F3F6",          # 窗口底
        "surface": "#FFFFFF",     # 卡片面
        "field": "#FBFCFE",       # 输入面（日志、列表、输入框）
        "border": "#DCE1E9",
        "fg": "#1F2430",
        "fg2": "#4B5563",         # 次级正文（插件简介等）
        "muted": "#6B7280",
        "accent": "#2F6FED",
        "accent_hover": "#1F5BD8",
        "accent_fg": "#FFFFFF",
        "danger": "#C0392B",
        "warn": "#B26A00",
        "ok": "#15803D",
        "sel": "#DCE7FD",
        # 按钮单独一组色：**必须和白卡片明显区分**，否则按钮看上去"不存在"
        "btn": "#E7ECF4",
        "btn_hover": "#DAE2EF",
        "btn_press": "#C8D3E4",
        "btn_border": "#B9C4D6",
        "danger_bg": "#FDECEA",
        "ok_bg": "#E8F6EE",
    },
    "dark": {
        "bg": "#14161A",
        "surface": "#1E2127",
        "field": "#171A1F",
        "border": "#3A424E",
        "fg": "#E6E8EB",
        "fg2": "#B9C0CC",
        "muted": "#9AA1AC",
        "accent": "#5B8DEF",
        "accent_hover": "#7AA4F2",
        "accent_fg": "#0F1115",
        "danger": "#F0736A",
        "warn": "#E0A94A",
        "ok": "#5BC98A",
        "sel": "#2A3550",
        "btn": "#2C333D",
        "btn_hover": "#39414D",
        "btn_press": "#48515F",
        "btn_border": "#4C5665",
        "danger_bg": "#3A2523",
        "ok_bg": "#1E3328",
    },
}


def palette(dark: bool) -> dict:
    """取当前该用的调色板。"""
    return PALETTES["dark" if dark else "light"]


# --------------------------------------------------------------------------- #
# 勾选框的「勾」：clam 自带的选中标记其实是个白色 ✕
# --------------------------------------------------------------------------- #
# 放大 16 倍逐像素看过：clam 的 Checkbutton「已选中」画的是一个四臂交叉的 ✕，
# 而 ✕ 在勾选框里的通行含义是「不选 / 不可用」——用户很容易读反。
# 所以这里自己画一个真勾号，用 ttk 的 image 元素替换掉它；万一某个 Tk 版本
# 不支持这套用法，就安静退回 clam 原样，不影响使用。

# 14×14 的勾号点阵（# = 勾；其余为空）
_CHECK_ART = (
    "..............",
    "...........##.",
    "..........###.",
    ".........###..",
    "........###...",
    "#......###....",
    "##....###.....",
    "###..###......",
    ".######.......",
    "..####........",
    "...##.........",
    "..............",
    "..............",
    "..............",
)

_INDICATOR_IMAGES = {}   # 名字 -> (未选中图, 选中图)；必须留引用，否则被回收


def _make_indicator_images(p: dict):
    """生成勾选框的「未选中 / 已选中」两张小图（宽度多留 4px 当右边距）。"""
    import tkinter as tk

    size = 14
    pad = 4
    on = tk.PhotoImage(width=size + pad, height=size)
    off = tk.PhotoImage(width=size + pad, height=size)
    for y in range(size):
        on_row, off_row = [], []
        for x in range(size + pad):
            if x >= size:                       # 右边留白（用卡片底色，看起来就是间距）
                on_row.append(p["surface"])
                off_row.append(p["surface"])
                continue
            edge = x in (0, size - 1) or y in (0, size - 1)
            if edge:                            # 1px 边框
                on_row.append(p["accent"])
                off_row.append(p["border"])
            elif _CHECK_ART[y][x] == "#":       # 勾号本体
                on_row.append("#FFFFFF")
                off_row.append(p["field"])
            else:
                on_row.append(p["accent"])
                off_row.append(p["field"])
        on.put("{" + " ".join(on_row) + "}", to=(0, y))
        off.put("{" + " ".join(off_row) + "}", to=(0, y))
    # 顺序必须是 (未选中, 已选中) —— 调用处就是这么解包的。
    # （写反过一次，结果勾选框"该勾的空着、不该勾的打着勾"。）
    return off, on


def _install_check_indicator(style: ttk.Style, p: dict, dark: bool) -> str:
    """把勾选框指示器换成自画的勾号，返回元素名（失败返回空串）。"""
    suffix = "dark" if dark else "light"
    name = f"Dsh.Checkbutton.indicator.{suffix}"
    try:
        if name not in _INDICATOR_IMAGES:
            _INDICATOR_IMAGES[name] = _make_indicator_images(p)
        off_img, on_img = _INDICATOR_IMAGES[name]
        # 按 CPython 的实现（ttk._format_elemcreate），"image" 元素的第一个参数**必须**是
        # 默认图，后面才是 (状态, 图) 对；所以这里就是 off_img 打头。
        style.element_create(name, "image", off_img, ("selected", on_img))
        # 与 clam 默认布局一致，只把 indicator 换成我们自己的
        style.layout("TCheckbutton", [
            ("Checkbutton.padding", {"sticky": "nswe", "children": [
                (name, {"side": "left", "sticky": ""}),
                ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                    ("Checkbutton.label", {"sticky": "nswe"})]}),
            ]}),
        ])
        return name
    except Exception:
        return ""


def _configure(style: ttk.Style, name: str, **options) -> None:
    """逐项配置样式。

    为什么逐项：ttk 只要有一个选项名不被该主题支持，整次 ``configure`` 都会抛错、
    全部选项都不生效（而且只在运行时才炸）。逐项设置可以让不支持的项安静跳过，
    其余照常生效。
    """
    for key, value in options.items():
        try:
            style.configure(name, **{key: value})
        except Exception:
            pass


def _map(style: ttk.Style, name: str, **options) -> None:
    """逐项配置状态映射（同上，逐项容错）。"""
    for key, value in options.items():
        try:
            style.map(name, **{key: value})
        except Exception:
            pass


def apply(root, dark: bool) -> dict:
    """配置全局 ttk 样式与窗口底色，返回本次使用的调色板。"""
    p = palette(dark)
    style = ttk.Style()
    try:
        style.theme_use("clam")      # 只有 clam 允许自由改色
    except Exception:
        pass

    try:
        root.configure(background=p["bg"])
    except Exception:
        pass

    base = (FONT_UI, 10)
    small = (FONT_UI, 9)

    # —— 全局兜底：任何没单独配置的样式也不会露出系统默认灰 ——
    _configure(style, ".",
               background=p["bg"], foreground=p["fg"],
               fieldbackground=p["field"], bordercolor=p["border"],
               focuscolor=p["accent"], font=base, relief="flat")

    # —— 容器与文字 ——
    # 约定：默认样式 = 卡片面；直接放在窗口上的控件用 *.Window 变体。
    _configure(style, "TFrame", background=p["surface"])
    _configure(style, "Window.TFrame", background=p["bg"])
    _configure(style, "Card.TFrame", background=p["surface"])

    _configure(style, "TLabel", background=p["surface"], foreground=p["fg"])
    _configure(style, "Window.TLabel", background=p["bg"], foreground=p["fg"])
    _configure(style, "Title.TLabel", background=p["surface"], foreground=p["fg"],
               font=(FONT_UI, 12, "bold"))
    _configure(style, "Section.TLabel", background=p["surface"], foreground=p["fg"],
               font=(FONT_UI, 11, "bold"))
    _configure(style, "Muted.TLabel", background=p["surface"], foreground=p["muted"],
               font=small)
    _configure(style, "Desc.TLabel", background=p["surface"], foreground=p["fg2"])
    _configure(style, "DescWindow.TLabel", background=p["bg"], foreground=p["fg2"])
    _configure(style, "MutedWindow.TLabel", background=p["bg"], foreground=p["muted"],
               font=small)
    _configure(style, "Accent.TLabel", background=p["surface"], foreground=p["accent"],
               font=(FONT_UI, 10, "bold"))
    _configure(style, "Danger.TLabel", background=p["surface"], foreground=p["danger"])
    _configure(style, "Warn.TLabel", background=p["surface"], foreground=p["warn"])
    _configure(style, "Ok.TLabel", background=p["surface"], foreground=p["ok"])
    _configure(style, "Link.TLabel", background=p["surface"], foreground=p["accent"],
               font=small)

    _configure(style, "TLabelframe", background=p["surface"],
               bordercolor=p["border"], borderwidth=1, relief="solid",
               padding=8)
    _configure(style, "TLabelframe.Label", background=p["surface"], foreground=p["fg"],
               font=(FONT_UI, 10, "bold"), padding=(2, 0))
    _configure(style, "TSeparator", background=p["border"])

    # —— 按钮 ——
    # 用户反馈过「按钮和背景颜色极其接近、看不清」，所以按钮一律用**自己的底色 + 描边**，
    # 和卡片白底拉开明显差距（btn / btn_border 是专门为此加的一组色）。
    _configure(style, "TButton", background=p["btn"], foreground=p["fg"],
               bordercolor=p["btn_border"], relief="solid", borderwidth=1,
               padding=(12, 6), focusthickness=0, font=base, anchor="center")
    _map(style, "TButton",
         background=[("disabled", p["surface"]),
                     ("pressed", p["btn_press"]), ("active", p["btn_hover"])],
         foreground=[("disabled", p["muted"])],
         bordercolor=[("disabled", p["border"]), ("active", p["accent"])])

    # 主按钮（启动 Harness）：实心强调色，最抢眼
    _configure(style, "Accent.TButton", background=p["accent"], foreground=p["accent_fg"],
               bordercolor=p["accent"], relief="solid", borderwidth=1, padding=(16, 8),
               focusthickness=0, font=(FONT_UI, 11, "bold"))
    _map(style, "Accent.TButton",
         background=[("disabled", p["border"]),
                     ("pressed", p["accent_hover"]), ("active", p["accent_hover"])],
         foreground=[("disabled", p["muted"])],
         bordercolor=[("disabled", p["border"])])

    # 「停止」：浅红底 + 红字红框 —— 一眼看出是"停"，且绝不是白底白字
    _configure(style, "Stop.TButton", background=p["danger_bg"], foreground=p["danger"],
               bordercolor=p["danger"], relief="solid", borderwidth=1,
               padding=(14, 7), focusthickness=0, font=(FONT_UI, 10, "bold"))
    _map(style, "Stop.TButton",
         background=[("disabled", p["surface"]),
                     ("pressed", p["danger_bg"]), ("active", p["danger_bg"])],
         foreground=[("disabled", p["muted"])],
         bordercolor=[("disabled", p["border"])])

    # 「恢复」：浅绿底 + 绿字绿框
    _configure(style, "Resume.TButton", background=p["ok_bg"], foreground=p["ok"],
               bordercolor=p["ok"], relief="solid", borderwidth=1,
               padding=(14, 7), focusthickness=0, font=(FONT_UI, 10, "bold"))
    _map(style, "Resume.TButton",
         background=[("disabled", p["surface"]),
                     ("pressed", p["ok_bg"]), ("active", p["ok_bg"])],
         foreground=[("disabled", p["muted"])],
         bordercolor=[("disabled", p["border"])])

    # 插件行里的小按钮：更紧凑，但同样带底色和描边
    _configure(style, "Mini.TButton", background=p["btn"], foreground=p["fg"],
               bordercolor=p["btn_border"], relief="solid", borderwidth=1,
               padding=(8, 2), focusthickness=0, font=small)
    _map(style, "Mini.TButton",
         background=[("pressed", p["sel"]), ("active", p["sel"])],
         bordercolor=[("active", p["accent"])])

    # —— 勾选框 ——
    # 指示器（那个小方块 + 里面的勾）由 _install_check_indicator 换成**自画的勾号**：
    # clam 自带的选中标记是个 ✕，容易被读成"没勾上"。这里只管文字与背景。
    _configure(style, "TCheckbutton", background=p["surface"], foreground=p["fg"],
               focusthickness=0, font=base, padding=(0, 2))
    _map(style, "TCheckbutton",
         background=[("active", p["surface"])],
         foreground=[("disabled", p["muted"])])
    _install_check_indicator(style, p, dark)
    _configure(style, "Window.TCheckbutton", background=p["bg"], foreground=p["fg"],
               focusthickness=0, font=base)

    # —— 选项卡 ——
    # 选中的那个：卡片白底 + 强调色加粗字 + 下沿一条强调色（用 expand 撑出来）
    _configure(style, "TNotebook", background=p["bg"], bordercolor=p["bg"],
               tabmargins=(0, 6, 0, 0), borderwidth=0, relief="flat")
    _configure(style, "TNotebook.Tab", background=p["bg"], foreground=p["muted"],
               bordercolor=p["bg"], padding=(22, 9), font=(FONT_UI, 10))
    _map(style, "TNotebook.Tab",
         background=[("selected", p["surface"])],
         foreground=[("selected", p["accent"]), ("active", p["fg"])],
         font=[("selected", (FONT_UI, 10, "bold"))],
         expand=[("selected", (0, 0, 0, 2))])

    # —— 输入类 ——
    _configure(style, "TEntry", fieldbackground=p["field"], foreground=p["fg"],
               bordercolor=p["border"], insertcolor=p["fg"], padding=5,
               selectbackground=p["sel"], selectforeground=p["fg"])
    _map(style, "TEntry", bordercolor=[("focus", p["accent"])])

    _configure(style, "TCombobox", fieldbackground=p["field"], background=p["field"],
               foreground=p["fg"], bordercolor=p["border"], arrowcolor=p["muted"],
               padding=4, selectbackground=p["sel"], selectforeground=p["fg"])
    _map(style, "TCombobox",
         fieldbackground=[("readonly", p["field"])],
         bordercolor=[("focus", p["accent"])])

    # —— 树形列表（版本选择器）——
    _configure(style, "Treeview", background=p["field"], fieldbackground=p["field"],
               foreground=p["fg"], bordercolor=p["border"], borderwidth=1,
               relief="solid", rowheight=25)
    _map(style, "Treeview",
         background=[("selected", p["sel"])],
         foreground=[("selected", p["fg"])])
    _configure(style, "Treeview.Heading", background=p["surface"], foreground=p["muted"],
               relief="flat", font=small, padding=(4, 4))
    _map(style, "Treeview.Heading", background=[("active", p["sel"])])

    # —— 滚动条：细一点、别抢眼 ——
    for orient, name in (("vertical", "Vertical.TScrollbar"),
                         ("horizontal", "Horizontal.TScrollbar")):
        _configure(style, name, background=p["border"], troughcolor=p["bg"],
                   bordercolor=p["bg"], arrowcolor=p["muted"], relief="flat",
                   darkcolor=p["border"], lightcolor=p["border"],
                   gripcount=0, arrowsize=12)
        _map(style, name,
             background=[("pressed", p["accent"]), ("active", p["muted"])],
             arrowcolor=[("disabled", p["border"])])

    # 下拉列表是原生 Listbox，只能走 option database
    try:
        root.option_add("*TCombobox*Listbox.background", p["field"])
        root.option_add("*TCombobox*Listbox.foreground", p["fg"])
        root.option_add("*TCombobox*Listbox.selectBackground", p["sel"])
        root.option_add("*TCombobox*Listbox.selectForeground", p["fg"])
        root.option_add("*TCombobox*Listbox.borderWidth", 0)
    except Exception:
        pass

    return p


def paint(widget, p: dict, *, field: bool = True) -> None:
    """给一个 tk 原生控件（Text / Listbox / Canvas）上色。

    ``field=True`` 用输入面（日志、列表），``False`` 用卡片面（画布当容器用时）。
    """
    if widget is None:
        return
    try:
        widget.configure(
            background=p["field"] if field else p["surface"],
            foreground=p["fg"],
            highlightthickness=0,
            insertbackground=p["fg"],
            selectbackground=p["sel"],
            selectforeground=p["fg"],
        )
    except Exception:
        pass


def paint_dialog(dlg, p: dict) -> None:
    """给 Toplevel 对话框上底色（它的子控件靠 ttk 样式，不用管）。"""
    try:
        dlg.configure(background=p["bg"])
    except Exception:
        pass
