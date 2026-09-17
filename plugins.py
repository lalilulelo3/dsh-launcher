# -*- coding: utf-8 -*-
"""plugins.py —— 插件发现、元数据读取、开关状态管理。

设计要点（写给阅读者）：
- “用户插件” = ``profiles/web/package.json`` 里 ``dependencies`` 字段的键。
  其中同时出现在 ``dsh.profile.bundles`` 里的才是“插件层”（会影响启动、可开关），
  其余是普通“库依赖”（一般不参与启动，不可开关）。
- **但有一类特殊的“不一致”状态**：包声明了插件层（``package.json`` 里有
  ``dsh.bundle.patch``）、文件也装进了 ``node_modules``，却**没有**登记到
  ``dsh.profile.bundles``。此时它不会参与启动，效果是「装了但不生效」。
  成因见 ``core.approve_builds_cmd()`` 的说明（pnpm 因构建脚本拦截而中途失败，
  dsh 只在 pnpm 成功时才登记插件层）。这类包必须与纯库依赖分开显示，
  否则用户唯一的线索就没了。
- “开关”不是删文件，也不是改 DSH 的配置文件，而是：
  启动器在 ``~/.dsh/launcher/disabled.patch.yml`` 里生成一个补丁，启动时用
  ``dsh web --patch <该文件>`` 把它叠加上去。这样禁用是**可逆**的，且**不会碰坏**
  DSH 自己的 ``cordis.patch.yml``。
- 补丁条目格式：``- id: <插件条目id> / disabled: true``（与 DSH 内部禁用遥测插件
  的做法完全一致）。要拿到“条目 id”，用 ``dsh web --dump-config`` 导出组合后的
  插件树，再按模块名匹配回插件包。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from core import (
    profile_package_json,
    profile_dir,
    launcher_dir,
    disabled_patch_path,
    safe_mode_patch_path,
    dump_config,
)


@dataclass
class Plugin:
    """一个已安装插件的完整展示信息。"""
    name: str          # npm 包名
    version: str       # 已安装版本
    description: str   # 简介
    homepage: str      # 项目主页（可能为空）
    is_bundle: bool    # 是否已登记为“插件层”（可开关）
    entry_ids: list    # 该包在 Loader 里的条目 id（用于开关）
    enabled: bool      # 当前是否启用
    declares_bundle: bool = False   # 包自身是否声明了插件层（dsh.bundle.patch）


class Inventory(NamedTuple):
    """一次插件清点的完整结果（``list_plugins()`` 的返回值）。"""
    plugins: list      # 插件层：已登记，会参与启动，可开关
    unmounted: list    # 已安装但未登记：声明了插件层却没挂载，需要修复
    libraries: list    # 纯库依赖：被插件依赖，不参与启动
    dump_error: str    # ``--dump-config`` 的失败原因；成功时为空字符串



def _read_json(path: Path) -> dict:
    """安全读取一个 JSON 文件，失败返回空字典。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_profile_package() -> dict:
    return _read_json(profile_package_json())


def installed_packages() -> list:
    """用户显式安装的插件包名列表（已排序）。"""
    deps = _read_profile_package().get("dependencies") or {}
    return sorted(deps.keys())


def bundles_list() -> list:
    """profile 的插件层列表（dsh.profile.bundles）。"""
    dsh = _read_profile_package().get("dsh") or {}
    profile = dsh.get("profile") or {}
    return list(profile.get("bundles") or [])


def _repo_url(data: dict) -> str:
    """把 package.json 里各种仓库地址写法转成可点击的 https 链接。"""
    repo = data.get("repository")
    if isinstance(repo, dict):
        url = repo.get("url", "") or ""
    else:
        url = repo or ""
    url = re.sub(r"^git\+", "", url)
    url = re.sub(r"^git://", "https://", url)
    url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
    url = re.sub(r"^ssh://git@", "https://", url)
    if url.endswith(".git"):
        url = url[:-4]
    return url


def _declares_bundle(data: dict) -> bool:
    """判断一个 package.json 是否声明了插件层（``dsh.bundle``）。

    插件包的写法通常是 ``"dsh": {"bundle": {"patch": "./cordis.patch.yml"}}``。
    这里对几种可能写法都做兼容，判定失败一律当作“没声明”（宁可归到库依赖，
    也不要凭空把普通库标成“待修复”去打扰用户）。
    """
    dsh = data.get("dsh")
    if not isinstance(dsh, dict):
        return False
    bundle = dsh.get("bundle")
    if bundle is True:
        return True
    if isinstance(bundle, str):
        return bool(bundle.strip())
    if isinstance(bundle, dict):
        patch = bundle.get("patch")
        return bool(str(patch or "").strip())
    return False


def _plugin_metadata(pkg: str) -> dict:
    """读取某个插件的 package.json，取版本/简介/主页/是否声明插件层。"""
    data = _read_json(profile_dir() / "node_modules" / pkg / "package.json")
    return {
        "version": data.get("version", "?"),
        "description": data.get("description", ""),
        "homepage": data.get("homepage") or _repo_url(data),
        "declares_bundle": _declares_bundle(data),
    }


_IGNORED_BUILDS_RE = re.compile(
    r"Ignored build scripts?:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


def ignored_build_packages(output) -> list:
    """从 pnpm 的输出里解析「因构建脚本被拦截而被拒绝安装」的包名。

    pnpm 的原文形如::

        [ERR_PNPM_IGNORED_BUILDS] Ignored build scripts: node-pty@1.1.0
        Run "pnpm approve-builds" to pick which dependencies should be allowed to run scripts.

    返回 ``['node-pty@1.1.0']`` 这样带版本号的条目（pnpm 就是这么打印的，
    有的版本还会用逗号分隔多个包）。识别不出时返回空列表。
    """
    text = "\n".join(output) if isinstance(output, (list, tuple)) else str(output or "")
    if "ERR_PNPM_IGNORED_BUILDS" not in text and "Ignored build scripts" not in text:
        return []
    names = []
    for m in _IGNORED_BUILDS_RE.finditer(text):
        for part in m.group(1).split(","):
            # pnpm 的「警告」形态（非致命）会在末尾带一个句号，去掉它
            part = part.strip().strip("'\"").rstrip("。.")
            if part and part not in names:
                names.append(part)
    return names


# --------------------------------------------------------------------------- #
# 启动失败诊断：从宿主输出里认出已知的致命故障
# --------------------------------------------------------------------------- #

_LOADER_ENTRY_RE = re.compile(r"failed to import loader entry (\S+) \(([^)]+)\)")
_EXPORT_MISMATCH_RE = re.compile(
    r"The requested module '([^']+)' does not provide an export named '([^']+)'"
)
_MODULE_NOT_FOUND_RE = re.compile(r"Cannot find (?:module|package) '([^']+)'")
# 出问题的模块是从「档案目录」里加载的（而不是全局安装目录）——这本身就是一个诊断信号：
# 档案里混进了 DSH 内部包的副本，版本和全局那份对不上。
_PROFILE_COPY_RE = re.compile(
    r"profiles[/\\][^/\\]+[/\\]node_modules[/\\]((?:@[^/\\]+[/\\])?[^/\\]+)"
)


def diagnose_boot_failure(output) -> dict | None:
    """从 DSH 启动输出里认出已知的致命故障，返回一份结构化诊断。

    识别不出（或输出为空）时返回 ``None``，交给调用方按原样展示日志。

    目前认识三类，每一类都对应实测见过的故障：

    - ``module-export-mismatch``：某个包要的导出，在另一个包里不存在
      （``The requested module 'X' does not provide an export named 'Y'``）。
      实测成因是**全局 DSH 装到一半**：外层 dsh 与它内部包的版本拼在一起了，
      于是插件树加载失败、dsh 直接退出（表现为「启动器一启动就失败」）。
    - ``module-not-found``：缺模块，多半是插件安装被中断。
    - ``plugin-tree-load-failed``：只知道插件树挂了，用安全模式缩小范围。
    """
    text = "\n".join(output) if isinstance(output, (list, tuple)) else str(output or "")
    if not text.strip():
        return None

    entries = _LOADER_ENTRY_RE.findall(text)
    exports = _EXPORT_MISMATCH_RE.findall(text)
    missing = _MODULE_NOT_FOUND_RE.findall(text)

    if exports or (entries and "plugin tree failed to load" in text):
        module, export = exports[0] if exports else ("", "")
        entry, entry_pkg = entries[0] if entries else ("", "")
        pkgs = []
        for cand in (entry_pkg, module):
            if cand and cand not in pkgs:
                pkgs.append(cand)
        shadowed = []
        for cand in _PROFILE_COPY_RE.findall(text):
            if cand not in shadowed:
                shadowed.append(cand)
        detail = []
        if entry_pkg:
            detail.append(f"出问题的插件条目：{entry}（来自 {entry_pkg}）")
        if module and export:
            detail.append(f"它要从 {module} 取一个叫 {export} 的导出，但装着的这一版里没有这个名字。")
        if shadowed:
            detail.append("而且它加载的是**档案目录**里的副本，不是全局那一份：")
            detail.extend(f"    profiles/…/node_modules/{s}" for s in shadowed)
        else:
            detail.append("也就是说：这两个包的版本对不上，被拼在了一起。")
        if shadowed:
            hint = ("这类故障的典型成因：某个插件把 DSH 自己的内部包（@deepseek-ai/*）当依赖装进了档案，\n"
                    "版本和全局 DSH 对不上，于是插件树加载失败、dsh 直接退出。\n"
                    "处理顺序：\n"
                    "  1. 到「工具」页点【安装 / 修复 DSH】，把全局那份补成一致版本；\n"
                    "  2. 回插件列表，把最近装过的插件逐个「卸载…」（副本会随它一起被清掉）；\n"
                    "  3. 想确认是谁引进来的，可在档案目录里跑：pnpm why <副本包名>。")
        else:
            hint = ("实测这类故障几乎都是「全局 DSH 装到一半 / 版本拼在一起」造成的：\n"
                    "  1. 到「工具」页点【安装 / 修复 DSH】（即 npm install -g @deepseek-ai/dsh@latest）；\n"
                    "  2. 若刚才是被某个插件拖坏的，用【安全模式启动】确认一次；\n"
                    "  3. 仍不行就【导出诊断报告】，报告里已包含启动日志。")
        return {
            "kind": "module-export-mismatch",
            "title": "插件树加载失败：模块版本对不上",
            "detail": "\n".join(detail) or "有包在加载时找不到它期望的导出。",
            "hint": hint,
            "packages": pkgs + [s for s in shadowed if s not in pkgs],
        }

    if missing:
        pkg = missing[0]
        return {
            "kind": "module-not-found",
            "title": "插件树加载失败：缺少模块",
            "detail": f"启动时找不到模块：{pkg}",
            "hint": "多半是插件没装完（依赖缺失或被中断）。到「工具」页重跑一次插件安装；"
                    "若列表里有红色的「已安装但未挂载」，对它点【修复】。",
            "packages": [pkg],
        }

    if "plugin tree failed to load" in text:
        return {
            "kind": "plugin-tree-load-failed",
            "title": "插件树加载失败",
            "detail": "有插件在加载阶段就报错，导致整个插件树起不来（具体报错见下方原始输出）。",
            "hint": "先用【安全模式启动】确认是不是第三方插件引起的，"
                    "再回插件列表逐个关掉可疑插件重试。",
            "packages": [],
        }

    if "pnpm failed in profile directory" in text or "ERR_PNPM" in text:
        return {
            "kind": "pnpm-failed",
            "title": "档案依赖安装失败（pnpm）",
            "detail": "dsh 在档案目录里跑 pnpm 时失败了，插件可能只装了一半。",
            "hint": "若日志里是 Ignored build scripts，就到插件列表对红色的「已安装但未挂载」项点【修复】；"
                    "否则看下方原始输出里 pnpm 的具体报错。",
            "packages": [],
        }
    return None


def _parse_dump(raw: str | None) -> list:
    """把 ``dsh web --dump-config`` 的输出解析成条目列表。

    输出形如：:

        - id: timer
          name: '@deepseek-ai/cordis-plugin-timer'
        - id: hmr
          name: '@deepseek-ai/cordis-plugin-hmr'
          disabled: true

    只关心 ``- id:``、缩进 2 格的 ``name:`` 和 ``disabled:`` 三行，其余（config 等）
    全部忽略，从而避免引入 YAML 库、也避免被 ``!!js`` 表达式干扰。
    """
    entries = []
    current = None
    for line in (raw or "").splitlines():
        m = re.match(r"^- id:\s*(.+?)\s*$", line)
        if m:
            if current is not None:
                entries.append(current)
            current = {"id": m.group(1), "name": "", "disabled": False}
            continue
        if current is None:
            continue
        m = re.match(r"^  name:\s*['\"]?([^'\"]+)['\"]?\s*$", line)
        if m:
            current["name"] = m.group(1)
            continue
        if re.match(r"^  disabled:\s*true\s*$", line):
            current["disabled"] = True
    if current is not None:
        entries.append(current)
    return entries


def _entry_ids_by_package(entries: list, pkgs: list) -> dict:
    """按模块名把条目 id 归并到插件包：{包名: [id, ...]}。

    匹配规则：条目模块名 == 包名，或以 ``包名/`` 开头（即包内的子模块）。
    """
    mapping = {pkg: [] for pkg in pkgs}
    for e in entries:
        name = e.get("name", "")
        if not name:
            continue
        for pkg in pkgs:
            if name == pkg or name.startswith(pkg + "/"):
                mapping[pkg].append(e["id"])
                break
    return mapping


# --------------------------------------------------------------------------- #
# 开关状态持久化（存在 ~/.dsh/launcher/state.json）
# --------------------------------------------------------------------------- #

def _state_file() -> Path:
    return launcher_dir() / "state.json"


def _load_state() -> dict:
    return _read_json(_state_file())


def _save_state(state: dict) -> None:
    launcher_dir().mkdir(parents=True, exist_ok=True)
    with open(_state_file(), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def disabled_packages() -> dict:
    """返回当前被禁用的插件：{包名: [条目id, ...]}。"""
    return dict(_load_state().get("disabled_packages") or {})


def write_disabled_patch() -> None:
    """根据 state 里的禁用信息，重新生成 ``--patch`` 补丁文件。

    该函数只依赖本地 state，不访问网络，因此很快，切换开关后立即调用即可。
    """
    disabled = disabled_packages()
    lines = [
        "# 由 DeepSeek Harness 启动器自动生成，请勿手工修改。",
        "# 记录被禁用的插件条目；清空本文件内容即可恢复全部插件。",
    ]
    for pkg in sorted(disabled.keys()):
        ids = disabled[pkg] or [pkg]  # 兜底：用包名当 id 尝试（目标不存在时 dsh 只会告警）
        for eid in ids:
            lines.append(f"- id: {eid}")
            lines.append("  disabled: true")
    if not disabled:
        # 没有任何禁用项时，也必须是一个合法的 YAML 数组（空数组），
        # 否则 --patch 加载一个“非数组”文件会导致启动失败。
        lines.append("[]")
    disabled_patch_path().parent.mkdir(parents=True, exist_ok=True)
    with open(disabled_patch_path(), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_safe_mode_patch(entry_ids: list) -> Path:
    """把所有第三方插件都写进“安全模式”临时补丁，返回补丁路径。

    这只影响使用该补丁的那一次启动，**不会改动**用户保存的开关状态。
    """
    lines = [
        "# 安全模式：本次启动临时禁用全部第三方插件（不改变你的开关设置）。",
    ]
    ids = [eid for eid in entry_ids if eid]
    for eid in ids:
        lines.append(f"- id: {eid}")
        lines.append("  disabled: true")
    if not ids:
        lines.append("[]")
    path = safe_mode_patch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def set_disabled_map(mapping: dict) -> None:
    """整体替换禁用状态（用于「恢复上次正常配置」）。"""
    state = _load_state()
    state["disabled_packages"] = {k: list(v or [k]) for k, v in dict(mapping or {}).items()}
    _save_state(state)
    write_disabled_patch()


def set_package_enabled(pkg: str, enabled: bool, entry_ids: list | None = None) -> bool:
    """启用或禁用某个插件。

    禁用时把该包的条目 id 记入 state（并立即重写补丁文件）；启用时移除。
    若禁用时拿不到任何条目 id，则返回 False 表示无法安全禁用。
    """
    state = _load_state()
    disabled = dict(state.get("disabled_packages") or {})
    if enabled:
        disabled.pop(pkg, None)
    else:
        ids = list(entry_ids or [])
        if not ids:
            return False
        disabled[pkg] = ids
    state["disabled_packages"] = disabled
    _save_state(state)
    write_disabled_patch()
    return True


def prune_stale_disables(installed: list) -> dict:
    """清理 state 里已不再安装的禁用记录（例如用户手动删掉了插件）。

    返回清理后的禁用映射 {包名: [条目id, ...]}。
    """
    disabled = dict(disabled_packages())
    stale = [pkg for pkg in disabled if pkg not in installed]
    if stale:
        remaining = {k: v for k, v in disabled.items() if k not in stale}
        state = _load_state()
        state["disabled_packages"] = remaining
        _save_state(state)
        write_disabled_patch()
        return remaining
    return disabled


def list_plugins() -> Inventory:
    """清点已安装的包，返回 :class:`Inventory`。

    分三类：

    - ``plugins``：已登记为插件层 ⇒ 参与启动，可开关；
    - ``unmounted``：**声明了插件层却没登记** ⇒ 装了但不生效，需要修复；
    - ``libraries``：纯库依赖 ⇒ 不参与启动，不可开关。

    ``dump_error`` 非空表示 ``--dump-config`` 没跑成功，此时 ``entry_ids``
    必然全为空、开关会失灵——调用方**必须**把这件事显性告诉用户。
    """
    pkgs = installed_packages()
    bundles = set(bundles_list())
    disabled = prune_stale_disables(pkgs)
    raw, dump_error = dump_config()
    entries = _parse_dump(raw)
    id_map = _entry_ids_by_package(entries, pkgs)

    plugins = []
    unmounted = []
    libraries = []
    for pkg in pkgs:
        meta = _plugin_metadata(pkg)
        is_bundle = pkg in bundles
        p = Plugin(
            name=pkg,
            version=meta["version"],
            description=meta["description"],
            homepage=meta["homepage"],
            is_bundle=is_bundle,
            entry_ids=id_map.get(pkg, []),
            enabled=pkg not in disabled,
            declares_bundle=meta["declares_bundle"],
        )
        if is_bundle:
            plugins.append(p)
        elif p.declares_bundle:
            unmounted.append(p)
        else:
            libraries.append(p)
    return Inventory(plugins=plugins, unmounted=unmounted,
                     libraries=libraries, dump_error=dump_error or "")
