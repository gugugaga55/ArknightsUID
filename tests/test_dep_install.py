"""ark安装依赖 测试：白名单解析（供应链边界）+ 状态表 + 命令接线。

核心安全约束：resolve_dependency 只认 pyproject 声明过的依赖/别名，
任意包名（如 requests）必须返回 None——这是聊天命令不接受任意 pip
包名的边界测试。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.dep_install import (
    dependency_status,
    install_dependency,
    resolve_dependency,
)

RAPIDOCR_SPEC = "rapidocr[onnxruntime]>=3.0.0"
PYTAKUMI_SPEC = "pytakumi>=0.1.4"


class ResolveDependencyTest(unittest.TestCase):
    def test_alias_ocr(self) -> None:
        self.assertEqual(resolve_dependency("ocr"), (RAPIDOCR_SPEC, "rapidocr"))

    def test_alias_chinese(self) -> None:
        self.assertEqual(
            resolve_dependency("截图识别"), (RAPIDOCR_SPEC, "rapidocr")
        )

    def test_case_insensitive(self) -> None:
        self.assertEqual(resolve_dependency("OCR"), (RAPIDOCR_SPEC, "rapidocr"))
        self.assertEqual(resolve_dependency("Takumi"), (PYTAKUMI_SPEC, "pytakumi"))

    def test_dep_names(self) -> None:
        self.assertEqual(
            resolve_dependency("rapidocr"), (RAPIDOCR_SPEC, "rapidocr")
        )
        self.assertEqual(
            resolve_dependency("pytakumi"), (PYTAKUMI_SPEC, "pytakumi")
        )

    def test_arbitrary_package_rejected(self) -> None:
        """供应链边界：任意包名一律拒绝。"""
        for name in ("requests", "numpy", "pip", "setuptools", "evil-pkg; rm -rf /"):
            self.assertIsNone(resolve_dependency(name), name)

    def test_empty_rejected(self) -> None:
        self.assertIsNone(resolve_dependency(""))
        self.assertIsNone(resolve_dependency("   "))


class DependencyStatusTest(unittest.TestCase):
    def test_lists_declared_deps(self) -> None:
        entries = {e.spec: e for e in dependency_status()}
        self.assertIn(RAPIDOCR_SPEC, entries)
        self.assertIn(PYTAKUMI_SPEC, entries)
        ocr = entries[RAPIDOCR_SPEC]
        self.assertIn("ocr", ocr.keys)
        # 状态与真实环境一致（本 venv rapidocr 已装）
        self.assertEqual(
            ocr.installed, importlib.util.find_spec("rapidocr") is not None
        )
        self.assertTrue(ocr.installed)

    def test_keys_are_source_of_truth(self) -> None:
        """别名来自 pyproject/extras 组名 + 少量友好别名，可随声明变化。"""
        for entry in dependency_status():
            self.assertTrue(entry.keys)


class InstallDependencyTest(unittest.TestCase):
    def test_unknown_name(self) -> None:
        ok, msg = asyncio.run(install_dependency("not-a-dep"))
        self.assertFalse(ok)
        self.assertIn("未知依赖名", msg)
        self.assertIn("rapidocr", msg)  # 提示里带可用列表

    def test_already_installed(self) -> None:
        ok, msg = asyncio.run(install_dependency("ocr"))
        self.assertTrue(ok)
        self.assertIn("已安装", msg)

    def test_install_success_path(self) -> None:
        """已装包走安装分支：临时伪装未装 → mock pip → 验证成功消息。"""
        with mock.patch(
            "ArknightsUID.utils.dep_install.importlib.util.find_spec",
            side_effect=lambda name: None if name == "rapidocr" else object(),
        ), mock.patch(
            "ArknightsUID.utils.dep_install._pip_install_and_verify",
            return_value=True,
        ) as pip_mock:
            ok, msg = asyncio.run(install_dependency("ocr"))
        self.assertTrue(ok)
        self.assertIn("安装成功", msg)
        self.assertIn("无需重启", msg)
        pip_mock.assert_called_once_with(RAPIDOCR_SPEC, "rapidocr")

    def test_install_failure_path(self) -> None:
        with mock.patch(
            "ArknightsUID.utils.dep_install.importlib.util.find_spec",
            side_effect=lambda name: None if name == "rapidocr" else object(),
        ), mock.patch(
            "ArknightsUID.utils.dep_install._pip_install_and_verify",
            return_value=False,
        ):
            ok, msg = asyncio.run(install_dependency("ocr"))
        self.assertFalse(ok)
        self.assertIn("安装失败", msg)
        self.assertIn("pip install", msg)  # 带手动安装指引


class InstallCommandHandlerTest(unittest.IsolatedAsyncioTestCase):
    """维护模块 ark安装依赖 命令接线。"""

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, message: str, **kwargs: object) -> None:
            self.sent.append(str(message))

    class _Ev:
        def __init__(self, text: str) -> None:
            self.text = text
            self.group_id = None

    async def _run(self, text: str):
        import ArknightsUID.arknightsuid_maintenance as mt

        bot = self._Bot()
        ev = self._Ev(text)
        await mt.install_dependency_command(bot, ev)
        return bot

    async def test_no_arg_lists_status(self) -> None:
        bot = await self._run("")
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("用法", bot.sent[0])
        self.assertIn("rapidocr", bot.sent[0])
        self.assertIn("AutoInstallDep", bot.sent[0])

    async def test_known_arg_install_flow(self) -> None:
        async def fake_install(name: str):
            return True, "✅ 已装好"

        with mock.patch(
            "ArknightsUID.utils.dep_install.is_installed", return_value=False
        ), mock.patch(
            "ArknightsUID.utils.dep_install.install_dependency",
            new=fake_install,
        ):
            bot = await self._run("ocr")
        self.assertEqual(len(bot.sent), 2)
        self.assertIn("正在安装", bot.sent[0])
        self.assertIn("rapidocr", bot.sent[0])
        self.assertIn("已装好", bot.sent[1])

    async def test_installed_short_circuit(self) -> None:
        """已安装时不发「正在安装」进度消息，直接一条结果。"""
        async def fail_install(name: str):
            raise AssertionError("不应走到安装")

        with mock.patch(
            "ArknightsUID.utils.dep_install.is_installed", return_value=True
        ), mock.patch(
            "ArknightsUID.utils.dep_install.install_dependency",
            new=fail_install,
        ):
            bot = await self._run("ocr")
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("已安装", bot.sent[0])

    async def test_unknown_arg_message(self) -> None:
        async def fake_install(name: str):
            return False, "未知依赖名"

        with mock.patch(
            "ArknightsUID.utils.dep_install.install_dependency",
            new=fake_install,
        ):
            bot = await self._run("随便啥")
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("未知依赖名", bot.sent[0])


if __name__ == "__main__":
    unittest.main()
