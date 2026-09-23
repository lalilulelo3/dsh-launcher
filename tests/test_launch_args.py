# -*- coding: utf-8 -*-
"""回归测试：启动 dsh 时交给 Popen 的参数必须是"干净"的。

**为什么专门钉死这一条（真实事故）**

v1.1.0 把启动方式改成「直接起 node + lib/bin.js、不经 shell」，参数以**列表**形式交给
CreateProcess —— 但拼参数时仍沿用了为 shell 准备的引号化，于是 `--patch` 的路径被加上
双引号。不经 shell 时引号**不会被剥掉**，路径就成了 `"C:\\...\\disabled.patch.yml"`：

    Node 判定它不是绝对路径 → 按当前工作目录解析 → 前缀上当前盘符
    → `failed to read overlay D:\\"C:\\Users\\…\\disabled.patch.yml"` → 启动必然失败

而且只在「存在被禁用的插件」或「走安全模式」时才会传 `--patch`，所以开发机上一路没
暴露 —— 直到用户在家里那台（exe 在 D: 盘）踩到。

**结论（本测试钉死的规则）**：列表形式（不经 shell）下，任何参数都不许含引号；
只有需要拼成一条 shell 字符串时，才由调用方逐个引号化。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core

FAKE_NODE = r"C:\Program Files\nodejs\node.exe"
FAKE_BIN = r"C:\Users\someone\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\lib\bin.js"


class LaunchArgsTest(unittest.TestCase):
    """全部用 mock 抓取参数，不真的启动 dsh。"""

    def _capture(self, patch_file: Path | None, spec):
        """返回 (真正传给 Popen 的 cmd, shell 标志)。"""
        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["shell"] = kwargs.get("shell")
            fake = mock.MagicMock()
            fake.poll.return_value = None
            fake.stdout = None
            return fake

        harness = core.HarnessProcess(log_callback=None)
        with mock.patch.object(core, "dsh_launch_spec", return_value=spec), \
                mock.patch.object(core.subprocess, "Popen", side_effect=fake_popen):
            harness.launch(patch_file)
        self.assertIn("cmd", captured, "launch() 没有调用 Popen？")
        return captured["cmd"], captured["shell"]

    @staticmethod
    def _patch_file_with_space() -> Path:
        """造一个**路径里带空格**的补丁文件 —— 空格正是"加引号"最容易出错的地方。"""
        d = Path(tempfile.mkdtemp(prefix="dsh patch test "))
        f = d / "disabled.patch.yml"
        f.write_text("- id: demo\n  disabled: true\n", encoding="utf-8")
        return f

    # ---------------------------------------------------------------- #
    def test_list_form_args_have_no_quotes(self):
        """不经 shell 时：参数是列表，且没有任何引号混进参数里。"""
        patch = self._patch_file_with_space()
        try:
            cmd, shell = self._capture(patch, ([FAKE_NODE, FAKE_BIN], False))
            self.assertFalse(shell, "这一分支不该经 shell")
            self.assertIsInstance(cmd, list, "不经 shell 时必须是参数列表，不是字符串")
            for arg in cmd:
                self.assertNotIn('"', arg, f"参数里混进了引号：{arg!r}")
                self.assertFalse(arg.startswith('"') or arg.endswith('"'),
                                 f"参数被引号包裹：{arg!r}")
        finally:
            shutil.rmtree(patch.parent, ignore_errors=True)

    def test_patch_path_is_byte_exact(self):
        """`--patch` 后面跟的必须是**原样的绝对路径**（含空格的路径也不许变形）。"""
        patch = self._patch_file_with_space()
        try:
            cmd, _ = self._capture(patch, ([FAKE_NODE, FAKE_BIN], False))
            self.assertIn("--patch", cmd, "补丁文件存在时必须传 --patch")
            value = cmd[cmd.index("--patch") + 1]
            self.assertEqual(value, str(patch))
            self.assertTrue(Path(value).is_absolute(), "必须是绝对路径")
            self.assertNotIn('"', value)
        finally:
            shutil.rmtree(patch.parent, ignore_errors=True)

    def test_no_patch_flag_when_file_missing(self):
        """补丁文件不存在时不该传 --patch（否则 dsh 会报读不到覆盖层）。"""
        cmd, _ = self._capture(Path(tempfile.gettempdir()) / "definitely-not-here-9f3a.yml",
                               ([FAKE_NODE, FAKE_BIN], False))
        self.assertNotIn("--patch", cmd)

    def test_always_no_open(self):
        """始终要有 --no-open：否则 dsh 会自己开一个浏览器、和启动器重复。"""
        cmd, _ = self._capture(None, ([FAKE_NODE, FAKE_BIN], False))
        self.assertIn("--no-open", cmd)
        self.assertIn("web", cmd, "第一个子命令必须是 web")

    def test_shell_branch_is_quoted(self):
        """退回 dsh.cmd（必须经 shell）时，含空格的路径**要**被正确引号化。"""
        patch = self._patch_file_with_space()
        try:
            cmd, shell = self._capture(patch, ([r"C:\Users\someone\AppData\Roaming\npm\dsh.cmd"], True))
            self.assertTrue(shell)
            self.assertIsInstance(cmd, str, "经 shell 时必须是一条命令字符串")
            self.assertIn(f'"{patch}"', cmd, "经 shell 时路径必须被引号包裹")
        finally:
            shutil.rmtree(patch.parent, ignore_errors=True)

    def test_spec_never_quotes(self):
        """dsh_launch_spec() 本身返回的参数一律不带引号（引号化只发生在拼 shell 字符串时）。"""
        argv, use_shell = core.dsh_launch_spec()
        self.assertTrue(argv)
        for arg in argv:
            self.assertNotIn('"', arg, f"dsh_launch_spec() 返回了带引号的参数：{arg!r}")


class NodePathResolutionTest(unittest.TestCase):
    """用 Node 复现"引号变成路径一部分"的机制 —— 这段是给未来的人看的证据。"""

    def test_quoted_path_is_resolved_as_relative(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("本机没有 node，跳过机制复现")
        # 直接以列表形式起 node（不经 shell），参数本身就是一个带引号的路径
        code = "process.stdout.write(require('path').resolve(process.argv[1]))"
        quoted = r'"C:\Users\someone\.dsh\launcher\disabled.patch.yml"'
        # ★ 必须显式 encoding="utf-8"：本机工作目录名含中文（…\启动器），
        #   而 node 输出的是 UTF-8；靠 locale（GBK）解码会直接抛 UnicodeDecodeError。
        out = subprocess.run([node, "-e", code, quoted],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
        resolved = (out.stdout or "").strip()
        self.assertTrue(resolved, f"node 没有输出：{out.stderr}")
        # 关键现象：解析结果不再是原来的绝对路径，而是「当前盘符 + 带引号的原文」
        self.assertNotEqual(resolved, quoted)
        self.assertIn(quoted, resolved, "应当能复现『前缀上盘符』的现象")


if __name__ == "__main__":
    unittest.main(verbosity=2)
