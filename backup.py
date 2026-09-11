# -*- coding: utf-8 -*-
"""backup.py —— 备份 / 重置 / 还原 web 档案与会话，以及插件清单的导出导入。

数据分布（理解本模块的关键）：
- 插件相关：``~/.dsh/profiles/web``（package.json 记录装了什么，node_modules 是插件文件）
- 历史会话：``~/.dsh/sessions``（与档案目录相互独立，所以能分开备份/还原）
- 全局设置：``~/.dsh/settings.yaml``

一份备份长这样（放在 ``~/.dsh/launcher/backups/<时间戳>/``）：:

    manifest.json    元信息：时间、标签、内容清单
    profile/         档案的配置类文件（package.json、cordis.patch.yml 等）
    plugins.json     已安装插件清单（名称 -> 版本），用于一键重装
    settings.yaml    全局设置（若存在）
    sessions/        历史会话（可选，体积可能较大）
    node_files/      插件文件本身（可选，体积很大，一般不需要）
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from core import dsh_home, profile_dir, launcher_dir, last_good_path

# 档案里属于“配置”的文件（不含 node_modules；插件文件靠 plugins.json 重装）
PROFILE_CONFIG_FILES = (
    "package.json",
    "cordis.patch.yml",
    "cordis.yml",
    "pnpm-workspace.yaml",
    "pnpm-lock.yaml",
)


def backups_root() -> Path:
    """备份总目录。"""
    return launcher_dir() / "backups"


def sessions_dir() -> Path:
    """历史会话目录。"""
    return dsh_home() / "sessions"


def settings_file() -> Path:
    """全局设置文件。"""
    return dsh_home() / "settings.yaml"


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 插件清单
# --------------------------------------------------------------------------- #
def current_plugins() -> dict:
    """读取当前档案里已装的插件：``{包名: 实际安装版本}``。"""
    pkg_json = profile_dir() / "package.json"
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except Exception:
        return {}
    deps = data.get("dependencies") or {}
    result = {}
    for name, spec in deps.items():
        meta = profile_dir() / "node_modules" / name / "package.json"
        version = ""
        try:
            version = json.loads(meta.read_text(encoding="utf-8")).get("version", "")
        except Exception:
            version = ""
        result[str(name)] = str(version or spec or "")
    return result


def export_plugin_list(dest: Path) -> int:
    """把插件清单导出到指定文件（名称 + 版本）。返回导出的插件数量。"""
    plugins = current_plugins()
    payload = {"exported": _now_text(), "plugins": plugins}
    Path(dest).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(plugins)


def read_plugin_list(path: Path) -> dict:
    """读取插件清单文件，返回 ``{包名: 版本}``。

    兼容三种写法：本启动器导出的 ``{"plugins": {...}}``、纯字典、纯名称列表。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and isinstance(data.get("plugins"), (dict, list)):
        data = data["plugins"]
    if isinstance(data, list):
        return {str(x): "" for x in data if str(x).strip()}
    if isinstance(data, dict):
        return {str(k): str(v or "") for k, v in data.items() if str(k).strip()}
    return {}


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #
def create_backup(label: str = "", include_sessions: bool = False,
                  include_plugin_files: bool = False) -> Path:
    """创建一份带时间戳的备份，返回备份目录。

    - 档案的配置类文件、插件清单、全局设置：总是备份（很小很快）；
    - ``include_sessions``：是否把历史会话一起备份（体积可能较大）；
    - ``include_plugin_files``：是否把插件文件本身（node_modules）也复制一份
      （体积很大、较慢，一般不需要——靠插件清单就能重装回来）。
    """
    root = backups_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = root / stamp
    n = 1
    while dest.exists():          # 同一秒内多次备份也不冲突
        n += 1
        dest = root / f"{stamp}-{n}"
    dest.mkdir(parents=True)

    # 1) 档案配置类文件
    cfg_dir = dest / "profile"
    cfg_dir.mkdir()
    copied = []
    for fname in PROFILE_CONFIG_FILES:
        src = profile_dir() / fname
        if src.is_file():
            shutil.copy2(src, cfg_dir / fname)
            copied.append(fname)

    # 2) 插件清单
    plugins = current_plugins()
    (dest / "plugins.json").write_text(
        json.dumps(plugins, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3) 全局设置
    has_settings = settings_file().is_file()
    if has_settings:
        shutil.copy2(settings_file(), dest / "settings.yaml")

    # 4) 历史会话（可选）
    has_sessions = False
    src_sessions = sessions_dir()
    if include_sessions and src_sessions.is_dir():
        shutil.copytree(src_sessions, dest / "sessions", dirs_exist_ok=True)
        has_sessions = True

    # 5) 插件文件本身（可选，很大）
    has_files = False
    src_modules = profile_dir() / "node_modules"
    if include_plugin_files and src_modules.is_dir():
        shutil.copytree(src_modules, dest / "node_files", dirs_exist_ok=True)
        has_files = True

    manifest = {
        "created": _now_text(),
        "label": label,
        "profileFiles": copied,
        "plugins": plugins,
        "hasSettings": has_settings,
        "hasSessions": has_sessions,
        "hasPluginFiles": has_files,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def list_backups() -> list:
    """列出全部备份（新→旧）。每项是 manifest 字典，另含 ``path``。"""
    root = backups_root()
    if not root.is_dir():
        return []
    items = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        data = {}
        try:
            data = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data["path"] = str(d)
        data.setdefault("created", d.name)
        data.setdefault("label", "")
        items.append(data)
    items.sort(key=lambda x: str(x.get("created", "")), reverse=True)
    return items


def backup_summary(manifest: dict) -> str:
    """把一份备份的元信息拼成一行给人看的说明。"""
    bits = []
    plugins = manifest.get("plugins") or {}
    bits.append(f"{len(plugins)} 个插件")
    if manifest.get("hasSessions"):
        bits.append("含历史会话")
    if manifest.get("hasPluginFiles"):
        bits.append("含插件文件")
    if manifest.get("hasSettings"):
        bits.append("含设置")
    label = manifest.get("label") or ""
    head = f"{manifest.get('created', '?')}"
    if label:
        head += f"  [{label}]"
    return f"{head}   （{'、'.join(bits)}）"


# --------------------------------------------------------------------------- #
# 还原
# --------------------------------------------------------------------------- #
def restore_backup(backup_path, mode: str = "all") -> list:
    """从备份还原。``mode`` 取 ``plugins`` / ``sessions`` / ``all``。

    还原前会先对**当前状态**再做一次自动备份（安全网）。
    返回实际执行的操作说明列表。
    """
    src = Path(backup_path)
    if not src.is_dir():
        raise FileNotFoundError(f"备份目录不存在：{src}")

    create_backup(label="还原前自动备份", include_sessions=False)
    actions = []

    if mode in ("plugins", "all"):
        cfg = src / "profile"
        if cfg.is_dir():
            profile_dir().mkdir(parents=True, exist_ok=True)
            for f in cfg.iterdir():
                if f.is_file():
                    shutil.copy2(f, profile_dir() / f.name)
                    actions.append(f"还原档案配置 {f.name}")
        pl = src / "plugins.json"
        if pl.is_file():
            profile_dir().mkdir(parents=True, exist_ok=True)
            shutil.copy2(pl, profile_dir() / "plugins.json")
            actions.append("还原插件清单")

    if mode in ("sessions", "all"):
        sess = src / "sessions"
        if sess.is_dir():
            dest = sessions_dir()
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(sess, dest, dirs_exist_ok=True)   # 合并，不删除现有会话
            actions.append("还原历史会话（与现有会话合并）")

    if mode == "all":
        st = src / "settings.yaml"
        if st.is_file():
            shutil.copy2(st, settings_file())
            actions.append("还原全局设置")

    if not actions:
        actions.append("该备份里没有所选类型的内容，未做改动")
    return actions


# --------------------------------------------------------------------------- #
# 重置
# --------------------------------------------------------------------------- #
def reset_profile(include_sessions_in_backup: bool = True) -> tuple:
    """重置 web 档案：**先自动备份**，再清空档案目录。

    DSH 下次启动会自动重新初始化一个干净的档案。
    返回 ``(备份目录, 被清理的条目名列表)``。
    """
    backup = create_backup(label="重置前自动备份",
                           include_sessions=include_sessions_in_backup)
    prof = profile_dir()
    removed = []
    if prof.is_dir():
        for child in list(prof.iterdir()):
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
                removed.append(child.name)
            except Exception:
                pass
    return backup, removed


# --------------------------------------------------------------------------- #
# “上次正常启动”快照：启动成功后记下插件配置，出问题时能一键回退
# --------------------------------------------------------------------------- #
def save_last_good() -> dict:
    """记录“上次成功启动”的配置：插件开关状态 + 各插件版本。"""
    from plugins import disabled_packages      # 函数内导入，避免模块循环依赖
    data = {
        "saved": _now_text(),
        "disabled": disabled_packages(),
        "plugins": current_plugins(),
    }
    launcher_dir().mkdir(parents=True, exist_ok=True)
    last_good_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def load_last_good() -> dict | None:
    """读取“上次正常启动”的快照；不存在或已损坏时返回 None。"""
    try:
        data = json.loads(last_good_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
