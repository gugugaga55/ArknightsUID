"""用户级 UID 隐藏覆盖回归测试（2026-08 审计问题 #3）。

背景：hide_uid 只看全局开关；「ark设置隐藏UID」写入的用户级偏好
（hide_uid_self_value）必须经 hide_uid_for 生效。此前肉鸽/抽卡/养成/
干员查询等路径直接用 hide_uid 或接口原始 uid，用户开了隐藏仍明文回显
（肉鸽卡片甚至绕过全局开关）。

本文件锁住各查询路径"必须把 hide_uid_for 的结果送进展示层"：
- gacha 抽卡记录 / 攒抽：masked 值传入 context 构建函数
- cultivate 单干员 / 多干员规划：格式化文本用调用方传入的脱敏 uid
- wiki 干员练度增强：源码级断言（_build_owner_context 依赖链较重）
- rogue：见 test_rogue_relic.py 的 RogueCardUpgradeTest
"""
from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

MASKED = "123***789"


def _credential(uid: str = "123456789"):
    return SimpleNamespace(uid=uid, nickname="刀客塔")


def _fake_ev(group_id=None):
    return SimpleNamespace(
        user_id="u1", bot_id="b1", group_id=group_id, text=""
    )


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, message, **kwargs) -> None:
        self.sent.append((message, kwargs))


class GachaUidMaskTest(unittest.IsolatedAsyncioTestCase):
    async def test_gacha_report_uses_user_level_mask(self) -> None:
        import ArknightsUID.arknightsuid_gacha as gacha_mod

        captured: dict = {}

        def fake_build_context(nickname, uid, records, version):
            captured["uid"] = uid
            return None  # 走文本回退

        async def fake_send_card(bot, ev, template, context, text):
            captured["text_uid"] = uid_in_text(text)

        def uid_in_text(text: str) -> bool:
            return MASKED in text

        with mock.patch.object(
            gacha_mod, "resolve_credential",
            new=mock.AsyncMock(return_value=_credential()),
        ), mock.patch.object(
            gacha_mod, "sync_gacha_logs",
            new=mock.AsyncMock(return_value=([{"gacha_ts": 1}], "")),
        ), mock.patch.object(
            gacha_mod, "hide_uid_for", new=mock.AsyncMock(return_value=MASKED)
        ) as m_mask, mock.patch.object(
            gacha_mod, "build_gacha_context", new=fake_build_context,
        ), mock.patch.object(
            gacha_mod, "send_card_or_text", new=fake_send_card,
        ):
            await gacha_mod.show_gacha_report(_FakeBot(), _fake_ev())

        m_mask.assert_awaited_once()
        self.assertEqual(captured["uid"], MASKED)

    async def test_gacha_plan_uses_user_level_mask(self) -> None:
        import ArknightsUID.arknightsuid_gacha as gacha_mod

        captured: dict = {}

        def fake_build_plan_context(summary, name, uid_masked, version):
            captured["uid"] = uid_masked
            return {}

        with mock.patch.object(
            gacha_mod, "resolve_credential",
            new=mock.AsyncMock(return_value=_credential()),
        ), mock.patch.object(
            gacha_mod, "hide_uid_for", new=mock.AsyncMock(return_value=MASKED)
        ) as m_mask, mock.patch.object(
            gacha_mod, "_auto_pity", new=mock.AsyncMock(return_value=0),
        ), mock.patch.object(
            gacha_mod, "_pick_pool",
            new=mock.AsyncMock(return_value=({"name": "池"}, "")),
        ), mock.patch.object(
            gacha_mod, "build_summary",
            new=mock.Mock(return_value={"notes": []}),
        ), mock.patch.object(
            gacha_mod, "build_plan_context", new=fake_build_plan_context,
        ), mock.patch.object(
            gacha_mod, "format_plan_text", new=mock.Mock(return_value=""),
        ), mock.patch.object(
            gacha_mod, "send_card_or_text", new=mock.AsyncMock(),
        ):
            await gacha_mod.plan_gacha_draws(_FakeBot(), _fake_ev())

        m_mask.assert_awaited_once()
        self.assertEqual(captured["uid"], MASKED)


class CultivateUidMaskTest(unittest.TestCase):
    """_format_result/_format_plan_result 必须优先用调用方传入的脱敏 uid。"""

    def _result(self) -> dict:
        char = SimpleNamespace(display_name="银灰")
        gap = {"gold_lack": 0, "materials": [], "materials_total": 0, "exp_books": []}
        current = {"evolve_phase": 2, "main_skill_lvl": 7, "masteries": [3, 3, 3]}
        return {"char": char, "gap": gap, "current": current}

    def test_format_result_uses_shown_uid(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _format_result

        text = _format_result(_credential(), self._result(), shown_uid=MASKED)
        self.assertIn(MASKED, text)
        self.assertNotIn("123456789", text)

    def test_format_plan_result_uses_shown_uid(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _format_plan_result

        merged = {
            "chars": [{"name": "银灰"}],
            "gold_lack": 0,
            "materials": [],
            "materials_total": 0,
            "exp_books": [],
        }
        text = _format_plan_result(
            _credential(), merged, [], shown_uid=MASKED
        )
        self.assertIn(MASKED, text)
        self.assertNotIn("123456789", text)


class WikiUidMaskSourceTest(unittest.TestCase):
    def test_owner_context_uses_hide_uid_for(self) -> None:
        """wiki 练度增强上下文走用户级覆盖（依赖链重，源码级防回归）。"""
        from ArknightsUID.arknightsuid_wiki import _build_owner_context

        source = inspect.getsource(_build_owner_context)
        self.assertIn("hide_uid_for", source)
        self.assertNotIn("hide_uid(credential.uid)", source)


if __name__ == "__main__":
    unittest.main()


class ContextUidFailClosedTest(unittest.TestCase):
    """审计 P3-1：展示上下文空 mask 时不得回退原始 UID（fail-closed）。"""

    def test_mask_uid_defaults_to_mask_not_raw(self) -> None:
        from ArknightsUID.utils.player.context import _mask_uid

        self.assertEqual(_mask_uid("123***789", "123456789"), "123***789")
        # 空 mask：默认掩码，绝不返回明文
        masked = _mask_uid("", "123456789")
        self.assertNotEqual(masked, "123456789")
        self.assertNotIn("123456789", masked)
        self.assertIn("***", masked)
        # 极短/空 uid 也不泄漏
        self.assertEqual(_mask_uid("", ""), "***")
        self.assertNotEqual(_mask_uid("", "123"), "123")

    def test_status_context_never_leaks_raw_uid(self) -> None:
        from ArknightsUID.utils.player.context import build_status_context
        from ArknightsUID.utils.player.parse import PlayerData

        data = PlayerData(
            uid="123456789", name="刀客塔", level=60, ap_max=135,
            ap_current=130, ap_complete_recovery_time=0,
        )
        context = build_status_context(data, version="v")  # 不传 uid_masked
        self.assertIsNotNone(context)
        self.assertNotIn("123456789", str(context["uid"]))
