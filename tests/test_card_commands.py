"""模组/危机合约命令的卡片化回归。

此前两个 handler 无任何测试，ark模组 曾因引用未导入的 send_card_or_text
直接 NameError 无回复（线上才发现）。此处用 mock 数据走真实 handler，
断言走卡片路径出 JPEG（渲染引擎缺失时自动降级文本，同样算通过路径）。
"""
from __future__ import annotations

import pytest

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, message: object, **kwargs: object) -> None:
        self.sent.append(message)


class FakeEvent:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.user_id = "u1"
        self.bot_id = "b1"
        self.group_id = None


def _is_card(message: object) -> bool:
    return isinstance(message, (bytes, bytearray)) and bytes(message)[:2] == b"\xff\xd8"


class ModuleCardTest(unittest.IsolatedAsyncioTestCase):
    @pytest.mark.usefixtures("offline_card_assets")
    async def test_module_command_sends_card(self) -> None:
        import ArknightsUID.utils.gamedata.modules_info as modules_info
        from ArknightsUID.arknightsuid_wiki import send_module_info

        ctx = {
            "name": "银灰",
            "modules": [
                {
                    "id": "uniequip_002_svrash",
                    "name": "雪境羽兽护理套组",
                    "type": "X",
                    "type_name": "领袖的远见",
                    "desc": "测试描述",
                    "unlock_phase": 2,
                    "unlock_level": 60,
                    "unlock_favor": 100,
                    "costs": [{"name": "模组数据块", "count": 4}],
                    "level_attrs": {"L1": "攻击 +45"},
                }
            ],
        }
        bot = FakeBot()
        with (
            mock.patch.object(modules_info, "build_module_context", return_value=ctx),
            mock.patch(
                "ArknightsUID.utils.gamedata.itemicon.item_icon_b64",
                return_value="",
            ),
        ):
            await send_module_info(bot, FakeEvent(" 银灰"))
        self.assertEqual(len(bot.sent), 1)
        # 渲染引擎可用 → JPEG 卡片；不可用 → 文本降级（两者都算走通）
        if not _is_card(bot.sent[0]):
            self.assertIsInstance(bot.sent[0], str)
            self.assertIn("银灰", bot.sent[0])


class CrisisCardTest(unittest.IsolatedAsyncioTestCase):
    async def test_crisis_command_sends_card(self) -> None:
        import ArknightsUID.utils.gamedata.crisis as crisis
        from ArknightsUID.arknightsuid_wiki import send_crisis_info

        seasons = [
            {"name": "弧光作战", "version": "危机合约V2",
             "start": "2026-08-16", "end": "2026-08-30"},
            {"name": "尘环行动", "version": "",
             "start": "2026-02-05", "end": "2026-02-19"},
        ]
        bot = FakeBot()
        with mock.patch.object(crisis, "load_crisis_seasons", return_value=seasons):
            await send_crisis_info(bot, FakeEvent(""))
        self.assertEqual(len(bot.sent), 1)
        if not _is_card(bot.sent[0]):
            self.assertIsInstance(bot.sent[0], str)
            self.assertIn("弧光作战", bot.sent[0])


if __name__ == "__main__":
    unittest.main()
