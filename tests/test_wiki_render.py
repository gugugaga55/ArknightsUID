"""wiki 卡片化后端测试：context 组装 + _render_or_text 回退逻辑。

真实 gsuid_core 导入（不 stub），照 test_p0_render.py 风格。
_patch 目标是源模块 ArknightsUID.utils.render.render_html——
_render_or_text 惰性 import 每次从源模块取属性，patch 源模块才生效。
"""
from __future__ import annotations

import unittest
from unittest import mock

from ArknightsUID.arknightsuid_wiki import (
    _build_enemy_context,
    _build_operator_context,
    _build_page_context,
    _build_stage_context,
    _format_mastery,
    _format_operator,
    _render_or_text,
)
from ArknightsUID.utils.prts.client import (
    BaseSkill,
    EnemyInfo,
    EnemyLevel,
    OperatorInfo,
    OperatorModule,
    OperatorPanel,
    OperatorSkill,
    OperatorTalent,
    ParadoxSimulation,
    SkillLevelDetail,
    SkillMasteryCost,
    StageInfo,
    WikiPageSummary,
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[object, dict]] = []

    async def send(self, message: object, **kwargs: object) -> None:
        self.sent.append((message, kwargs))


class FakeEvent:
    def __init__(self, text: str = "", group_id: object = None) -> None:
        self.text = text
        self.group_id = group_id
        self.user_id = "u1"
        self.bot_id = "b1"


class WikiBuildContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_page_context_has_required_keys(self) -> None:
        page = WikiPageSummary(
            title="龙门币",
            source_url="https://prts.wiki/w/龙门币",
            summary="通用货币。",
            fields={"稀有度": "材料", "用途": "升级"},
        )
        context = await _build_page_context(page)

        self.assertEqual(context["title"], "龙门币")
        self.assertIn("通用货币", context["summary"])  # clip 不改变短文本
        self.assertEqual(
            context["fields"],
            [
                {"key": "稀有度", "value": "材料"},
                {"key": "用途", "value": "升级"},
            ],
        )
        self.assertEqual(context["source_url"], "https://prts.wiki/w/龙门币")
        self.assertTrue(context["version"])

    async def test_build_operator_context_extracts_operator_fields(self) -> None:
        op = OperatorInfo(
            title="银灰",
            name="银灰",
            rarity="5",  # PRTS 存 0-5
            profession="近卫",
            branch="领主",
            position="近战位",
            faction="谢拉格",
            tags="输出 支援",
            obtain="公开招募",
            source_url="https://prts.wiki/w/银灰",
        )
        context = await _build_operator_context(op)

        self.assertEqual(context["name"], "银灰")
        self.assertEqual(context["rarity"], "6")  # +1 转人读
        self.assertEqual(context["profession"], "近卫")
        self.assertEqual(context["branch"], "领主")
        self.assertTrue(context["version"])

    async def test_build_operator_context_includes_skills_and_talents(self) -> None:
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="4",
            skills=[
                OperatorSkill(
                    name="技能一",
                    skill_type="自动回复 / 手动触发",
                    range_="3-7",
                    levels=[
                        SkillLevelDetail(level="1", sp_cost="90", duration="20"),
                        SkillLevelDetail(level="专1", sp_cost="90", duration="27"),
                    ],
                    mastery_costs=[
                        SkillMasteryCost(mastery="专1", materials=["龙门币 ×5", "糖 ×3"]),
                    ],
                )
            ],
            talents=[
                OperatorTalent(slot="第一天赋", name="领袖", unlock="精英1", description="攻击力+5%"),
            ],
        )
        context = await _build_operator_context(op)

        self.assertEqual(len(context["skills"]), 1)
        skill = context["skills"][0]
        self.assertEqual(skill["name"], "技能一")
        self.assertEqual(skill["range"], "3-7")
        self.assertEqual(len(skill["levels"]), 2)
        self.assertEqual(skill["levels"][0]["sp_cost"], "90")
        self.assertEqual(len(skill["mastery_costs"]), 1)
        self.assertEqual(skill["mastery_costs"][0]["materials"], ["龙门币 ×5", "糖 ×3"])
        self.assertEqual(len(context["talents"]), 1)
        self.assertEqual(context["talents"][0]["name"], "领袖")

    async def test_format_operator_renders_skills_and_mastery(self) -> None:
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="5",  # → 6 星
            profession="近卫",
            skills=[
                OperatorSkill(
                    name="技能一",
                    skill_type="自动回复 / 手动触发",
                    levels=[
                        SkillLevelDetail(level="1", sp_cost="10", duration="30"),
                        SkillLevelDetail(level="4", sp_cost="10", duration="33"),
                        SkillLevelDetail(level="7", sp_cost="10", duration="35"),
                        SkillLevelDetail(level="专1", sp_cost="10", duration="36"),
                        SkillLevelDetail(level="专3", sp_cost="10", duration="40"),
                    ],
                    mastery_costs=[
                        SkillMasteryCost(mastery="专1", materials=["龙门币 ×5"]),
                    ],
                )
            ],
            talents=[OperatorTalent(slot="第一天赋", name="天赋一", description="效果")],
            source_url="https://prts.wiki/w/测试",
        )
        text = _format_operator(op)

        self.assertIn("「明日方舟」干员：测试", text)
        self.assertIn("6星", text)
        self.assertIn("近卫", text)
        self.assertIn("技能一", text)
        self.assertIn("自动回复", text)
        self.assertIn("10耗/30持", text)
        self.assertIn("专1材料：龙门币 ×5", text)
        self.assertIn("天赋一", text)
        self.assertIn("来源：https://prts.wiki/w/测试", text)

    async def test_format_operator_low_rarity_panel_without_elite2(self) -> None:
        # 低星干员（如黑角）无精英2数据：应显示存在的最高阶段，不出现空白精二行
        op = OperatorInfo(
            title="黑角",
            name="黑角",
            rarity="1",
            panel=OperatorPanel(
                redeploy="70s",
                deploy_cost="9",
                block="2",
                attack_speed="1.2s",
                panels={
                    "精英0_满级": "30",
                    "精英0_满级_生命上限": "1557",
                    "精英0_满级_攻击": "213",
                    "精英0_满级_防御": "258",
                    "精英0_满级_法术抗性": "0",
                },
            ),
        )
        text = _format_operator(op)

        self.assertIn("满级：HP1557 攻213 防258 抗0", text)
        self.assertNotIn("精二满级：HP 攻 防 抗", text)
        self.assertNotIn("精一满级：HP", text)

    async def test_format_mastery_lists_materials_per_skill(self) -> None:
        op = OperatorInfo(
            title="测试",
            name="测试",
            skills=[
                OperatorSkill(
                    name="技能一",
                    mastery_costs=[
                        SkillMasteryCost(mastery="专1", materials=["龙门币 ×5"]),
                        SkillMasteryCost(mastery="专3", materials=["D32钢 ×6", "糖聚块 ×5"]),
                    ],
                )
            ],
            source_url="https://prts.wiki/w/测试",
        )
        text = _format_mastery(op)

        self.assertIn("「明日方舟」测试 专精材料", text)
        self.assertIn("【技能一】", text)
        self.assertIn("专1：龙门币 ×5", text)
        self.assertIn("专3：D32钢 ×6、糖聚块 ×5", text)
        self.assertIn("来源：https://prts.wiki/w/测试", text)

    async def test_build_operator_context_includes_panel_modules_potentials(self) -> None:
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="5",
            panel=OperatorPanel(
                redeploy="70s",
                deploy_cost="18→20",
                block="2",
                attack_speed="1.3s",
                panels={"精英2_满级_生命上限": "2560", "精英2_满级_攻击": "713"},
            ),
            modules=[OperatorModule(name="证章", type_="基础证章", description="基础")],
            potentials=["部署费用-1", "攻击力+26"],
            paradox=ParadoxSimulation(name="睥睨", elite="2", rewards=["合成玉 ×200"]),
            base_skills=[BaseSkill(name="后勤一", unlock="精英0")],
        )
        context = await _build_operator_context(op)

        self.assertIsNotNone(context["panel"])
        self.assertEqual(context["panel"]["block"], "2")
        self.assertEqual(context["panel"]["panels"]["精英2_满级_生命上限"], "2560")
        self.assertEqual(len(context["modules"]), 1)
        self.assertEqual(context["modules"][0]["type"], "基础证章")
        self.assertEqual(context["potentials"], ["部署费用-1", "攻击力+26"])
        self.assertIsNotNone(context["paradox"])
        self.assertEqual(context["paradox"]["rewards"], ["合成玉 ×200"])
        self.assertEqual(len(context["base_skills"]), 1)

    async def test_build_enemy_context_includes_first_level_stats(self) -> None:
        enemy = EnemyInfo(
            title="源石虫",
            name="源石虫",
            index="1",
            rank="普通",
            description="最常见的感染生物。",
            damage_type="物理",
            levels=[EnemyLevel(max_hp="1000", atk="100", defense="0", res="0", weight="1")],
        )
        context = await _build_enemy_context(enemy)

        self.assertEqual(context["name"], "源石虫")
        self.assertTrue(context["has_stats"])
        self.assertEqual(context["max_hp"], "1000")
        self.assertEqual(context["atk"], "100")
        self.assertEqual(context["defense"], "0")

    async def test_build_stage_context_without_map_url(self) -> None:
        stage = StageInfo(
            title="1-7",
            code="1-7",
            name="暴君",
            ap_cost="6",
            drops=["固源岩", "代糖"],
            enemies=[],
        )
        context = await _build_stage_context(stage)

        self.assertEqual(context["title"], "1-7")
        self.assertEqual(context["name"], "暴君")
        self.assertEqual(context["ap_cost"], "6")
        self.assertEqual(context["drops"], ["固源岩", "代糖"])
        # 无 map_url → map_image 留空，模板需优雅降级
        self.assertEqual(context["map_image"], "")
        self.assertTrue(context["version"])


class WikiRenderOrTextTest(unittest.IsolatedAsyncioTestCase):
    async def test_template_not_found_falls_back_to_text(self) -> None:
        bot = FakeBot()
        ev = FakeEvent()
        # 用一个确定不存在的模板名：真实 get_template 抛 TemplateNotFound
        await _render_or_text(
            bot, ev, "ark_wiki_definitely_missing.html", {"x": 1}, "纯文本回退"
        )

        self.assertEqual(len(bot.sent), 1)
        message, _kwargs = bot.sent[0]
        # 回退路径发送的是字符串（_send_text 在私聊无前缀）
        self.assertEqual(message, "纯文本回退")

    async def test_render_exception_falls_back_to_text(self) -> None:
        bot = FakeBot()
        ev = FakeEvent()
        # patch 源模块 render_html（惰性 import 每次从源模块取属性）
        with mock.patch(
            "ArknightsUID.utils.render.render_html", side_effect=RuntimeError("boom")
        ):
            await _render_or_text(
                bot, ev, "ark_wiki_page.html", {"x": 1}, "纯文本回退"
            )

        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0][0], "纯文本回退")

    async def test_rendered_image_is_sent_as_bytes(self) -> None:
        bot = FakeBot()
        ev = FakeEvent()
        image_bytes = b"\x89PNG\r\n\x1a\n fake png"
        with mock.patch(
            "ArknightsUID.utils.render.render_html", return_value=image_bytes
        ):
            await _render_or_text(
                bot, ev, "ark_wiki_page.html", {"x": 1}, "纯文本回退"
            )

        self.assertEqual(len(bot.sent), 1)
        message, kwargs = bot.sent[0]
        self.assertIs(message, image_bytes)
        # 出图路径不加 at_sender（与文本路径不同）
        self.assertEqual(kwargs.get("at_sender"), False)

    async def test_render_html_returns_none_falls_back_to_text(self) -> None:
        bot = FakeBot()
        ev = FakeEvent()
        # playwright 不可用时 render_html 返回 None（非异常）→ if img 兜住 → 文本回退
        with mock.patch("ArknightsUID.utils.render.render_html", return_value=None):
            await _render_or_text(
                bot, ev, "ark_wiki_page.html", {"x": 1}, "纯文本回退"
            )

        self.assertEqual(bot.sent[0][0], "纯文本回退")


if __name__ == "__main__":
    unittest.main()
