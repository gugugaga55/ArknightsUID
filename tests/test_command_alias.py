"""ark指令别名（bot 主人管理的多触发词机制）。

覆盖：权限（仅 user_pm=0）、注册校验（目标存在/冲突/格式）、转发参数
透传（别名触发词 ≡ 原命令）、内置别名与 tombstone（删除内置不复活）、
触发器表挂载形态（前缀展开键 + block）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[object, dict]] = []

    async def send(self, message: object, **kwargs: object) -> None:
        self.sent.append((message, kwargs))


class FakeEvent:
    def __init__(self, text: str = "", user_pm: int = 6) -> None:
        self.text = text
        self.command = ""
        self.group_id = None
        self.user_id = "u1"
        self.bot_id = "b1"
        self.user_pm = user_pm


class CommandAliasTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import ArknightsUID.arknightsuid_alias as alias_mod

        self.mod = alias_mod
        # 每个用例重置运行时状态
        alias_mod._alias_map.clear()
        alias_mod._uninstall_alias_trigger("招募")
        alias_mod._uninstall_alias_trigger("测试别名")

    def _fake_target_trigger(self, keyword: str = "公招推荐"):
        """构造一个属于本插件的假目标 Trigger（含包装函数归属）。"""
        from gsuid_core.trigger import Trigger

        async def handler(bot, ev):  # pragma: no cover - 不实际执行
            return None

        handler.__module__ = "ArknightsUID.arknightsuid_wiki"
        return Trigger("command", keyword, handler, "ark", True, False)

    async def test_permission_gate_rejects_non_master(self) -> None:
        """非主人（含群管理员语义的普通用户 user_pm=6）被拒；主人放行到解析。"""
        bot, ev = FakeBot(), FakeEvent("招募=公招推荐", user_pm=6)
        await self.mod.manage_command_alias(bot, ev)
        text = str(bot.sent[0][0])
        self.assertIn("仅 bot 主人", text)
        # superuser（user_pm=1）同样不可管理（主人=0 才行）
        bot2, ev2 = FakeBot(), FakeEvent("招募=公招推荐", user_pm=1)
        await self.mod.manage_command_alias(bot2, ev2)
        self.assertIn("仅 bot 主人", str(bot2.sent[0][0]))

    async def test_register_validates_target_and_conflicts(self) -> None:
        fake = self._fake_target_trigger("公招推荐")
        # 目标不存在
        with mock.patch.object(self.mod, "_find_target_trigger", return_value=None):
            err = await self.mod.register_command_alias("招募", "不存在命令")
            self.assertIn("未找到本插件指令", err)
        # 与现有命令词冲突
        with (
            mock.patch.object(self.mod, "_find_target_trigger", return_value=fake),
            mock.patch.object(self.mod, "_alias_conflicts", return_value=True),
        ):
            err = await self.mod.register_command_alias("干员", "公招推荐")
            self.assertIn("冲突", err)
        # 格式：别名内部含空白（两侧空白会被 strip 容错，内部空白拒绝）
        with mock.patch.object(self.mod, "_find_target_trigger", return_value=fake):
            err = await self.mod.register_command_alias("招 募", "公招推荐")
            self.assertIsNotNone(err)
            self.assertIn("空白", err)
        # 别名与目标相同
        with mock.patch.object(self.mod, "_find_target_trigger", return_value=fake):
            err = await self.mod.register_command_alias("公招推荐", "公招推荐")
            self.assertIn("相同", err)

    async def test_register_persists_and_installs_trigger(self) -> None:
        """正常注册：落库 + 内存表 + TL 挂载（前缀展开键、block=True）。"""
        from ArknightsUID.utils.database import models as models_mod

        fake = self._fake_target_trigger("公招推荐")
        set_alias_mock = mock.AsyncMock()
        with (
            mock.patch.object(self.mod, "_find_target_trigger", return_value=fake),
            mock.patch.object(
                models_mod.ArkCommandAlias, "set_alias", set_alias_mock
            ),
        ):
            err = await self.mod.register_command_alias("招募2", "ark公招推荐")
        self.assertIsNone(err, f"注册不应报错: {err}")
        set_alias_mock.assert_awaited_once_with("招募2", "公招推荐")
        self.assertEqual(self.mod._alias_map["招募2"], "公招推荐")
        # 触发器挂载：每个插件前缀一个键，且 block 生效
        prefixes = self.mod._plugin_prefixes()
        table = self.mod.alias_sv.TL["command"]
        for p in prefixes:
            trig = table.get(p + "招募2")
            self.assertIsNotNone(trig, f"缺少键 {p}招募2")
            self.assertTrue(trig.block)
            self.assertEqual(trig.keyword, "招募2")

    async def test_forward_passes_text_through(self) -> None:
        """转发：别名命中后直调目标 handler，ev.text 与原命令参数一致。"""
        fake = self._fake_target_trigger("公招推荐")
        calls: list[tuple[str, str]] = []

        async def target_handler(bot, ev):
            calls.append((ev.command, ev.text))

        target_handler.__module__ = "ArknightsUID.arknightsuid_wiki"
        fake.func = target_handler
        self.mod._alias_map["招募"] = "公招推荐"
        ev = FakeEvent(" 术师 输出")
        ev.command = "招募"
        with mock.patch.object(self.mod, "_find_target_trigger", return_value=fake):
            await self.mod.forward_command(FakeBot(), ev)
        # 目标 handler 收到的 text 与 "ark公招推荐 术师 输出" 触发时一致
        self.assertEqual(calls, [("招募", " 术师 输出")])

    async def test_forward_target_missing_notifies(self) -> None:
        self.mod._alias_map["招募"] = "公招推荐"
        bot, ev = FakeBot(), FakeEvent()
        ev.command = "招募"
        with mock.patch.object(self.mod, "_find_target_trigger", return_value=None):
            await self.mod.forward_command(bot, ev)
        self.assertIn("目标指令", str(bot.sent[0][0]))

    async def test_builtin_alias_delete_writes_tombstone(self) -> None:
        """内置别名删除 → tombstone（target=""）而非删行，重启不复活。"""
        self.mod._alias_map["招募"] = "公招推荐"
        self.mod._install_alias_trigger("招募")
        from ArknightsUID.utils.database import models as models_mod

        set_alias_mock = mock.AsyncMock()
        with mock.patch.object(
            models_mod.ArkCommandAlias, "set_alias", set_alias_mock
        ):
            err = await self.mod.remove_command_alias("招募")
        self.assertIsNone(err)
        set_alias_mock.assert_awaited_once_with("招募", "")
        self.assertNotIn("招募", self.mod._alias_map)
        # 触发器键已摘除
        for p in self.mod._plugin_prefixes():
            self.assertNotIn(p + "招募", self.mod.alias_sv.TL["command"])

    async def test_ensure_merges_builtin_and_tombstone(self) -> None:
        """重放：内置 ∪ DB 行；tombstone 停用内置。"""
        rows = [
            SimpleNamespace(alias="招募", target=""),   # tombstone 停用内置
            SimpleNamespace(alias="抽卡", target="寻访"),
        ]
        global _installed
        self.mod._installed = False
        from ArknightsUID.utils.database import models as models_mod

        with mock.patch.object(
            models_mod.ArkCommandAlias, "list_aliases",
            mock.AsyncMock(return_value=rows),
        ):
            await self.mod.ensure_aliases_installed()
        try:
            self.assertNotIn("招募", self.mod._alias_map)
            self.assertEqual(self.mod._alias_map["抽卡"], "寻访")
            self.assertIn("ark抽卡", self.mod.alias_sv.TL["command"])
            self.assertNotIn("ark招募", self.mod.alias_sv.TL["command"])
        finally:
            self.mod._installed = False
            self.mod._alias_map.clear()
            self.mod._uninstall_alias_trigger("抽卡")

    async def test_alias_cannot_hijack_management_command(self) -> None:
        """别名不得与本模块管理命令冲突（排除表只豁免转发器键）。"""
        # 「指令别名列表」键在 alias_sv.TL["command"]，但不是转发器 → 应冲突
        self.assertTrue(self.mod._alias_conflicts("指令别名列表"))


if __name__ == "__main__":
    unittest.main()
