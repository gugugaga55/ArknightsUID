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
    _build_owner_context,
    _build_page_context,
    _build_stage_context,
    _final_level,
    _format_enemy,
    _format_mastery,
    _format_operator,
    _format_stage,
    _owner_level,
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
    StageEnemy,
    StageInfo,
    TalentStage,
    WikiPageSummary,
)
from ArknightsUID.utils.render import ark_templates  # noqa: F401  模板存在性


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


def _full_levels(with_mastery: bool = True) -> list[SkillLevelDetail]:
    """1-7 + 专1-3 十级（PRTS {{技能2}} 解析布局）；无专精技能后三级描述为空。"""
    levels = [
        SkillLevelDetail(level=str(n), description=f"效果+{n * 10}%", sp_cost="10")
        for n in range(1, 8)
    ]
    if with_mastery:
        levels += [
            SkillLevelDetail(level=f"专{n}", description=f"效果+{70 + n * 10}%", sp_cost="10")
            for n in (1, 2, 3)
        ]
    else:
        levels += [SkillLevelDetail(level=f"专{n}") for n in (1, 2, 3)]
    return levels


class SkillLevelSelectionTest(unittest.TestCase):
    def test_final_level_prefers_mastery_then_walks_back(self) -> None:
        self.assertEqual(_final_level(_full_levels()).level, "专3")
        # 无专精技能：最后一个带描述的等级是 7 级
        self.assertEqual(_final_level(_full_levels(with_mastery=False)).level, "7")
        self.assertIsNone(_final_level([SkillLevelDetail(level="1")]))

    def test_owner_level_by_specialize(self) -> None:
        levels = _full_levels()
        self.assertEqual(_owner_level(levels, 2, 7).level, "专2")
        self.assertEqual(_owner_level(levels, 3, 7).level, "专3")

    def test_owner_level_by_main_lvl_without_mastery(self) -> None:
        levels = _full_levels()
        self.assertEqual(_owner_level(levels, 0, 4).level, "4")
        # 边界：0/越界值拿不到有效级
        self.assertIsNone(_owner_level(levels, 0, 0))

    def test_owner_level_walks_back_when_mastery_slot_empty(self) -> None:
        # 无专精技能但玩家数据带专精位（异常兜底）：回退到 7 级
        self.assertEqual(
            _owner_level(_full_levels(with_mastery=False), 1, 7).level, "7"
        )


class TalentDisplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_context_includes_stages_and_potential(self) -> None:
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="5",
            talents=[
                OperatorTalent(
                    slot="第一天赋",
                    name="领袖",
                    unlock="精英1",
                    description="精英1：攻击力+5%",
                    stages=[
                        TalentStage(condition="精英1", effect="攻击力+5%"),
                        TalentStage(condition="精英2", effect="攻击力+10%"),
                    ],
                    potential=TalentStage(condition="精英2", effect="攻击力+12%（+2%）"),
                )
            ],
        )
        context = await _build_operator_context(op)
        talent = context["talents"][0]
        self.assertEqual(
            talent["stages"],
            [
                {"condition": "精英1", "effect": "攻击力+5%"},
                {"condition": "精英2", "effect": "攻击力+10%"},
            ],
        )
        self.assertEqual(
            talent["potential"], {"condition": "精英2", "effect": "攻击力+12%（+2%）"}
        )

    async def test_format_operator_lists_talent_stages_and_potential(self) -> None:
        """文本回退：天赋逐阶段一行 + 满潜一行（与卡片展示一致）。"""
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="5",
            talents=[
                OperatorTalent(
                    slot="第一天赋",
                    name="领袖",
                    unlock="精英1",
                    stages=[
                        TalentStage(condition="精英1", effect="攻击力+5%"),
                        TalentStage(condition="精英2", effect="攻击力+10%"),
                    ],
                    potential=TalentStage(condition="精英2", effect="攻击力+12%（+2%）"),
                )
            ],
        )
        text = _format_operator(op)
        self.assertIn("精英1：攻击力+5%", text)
        self.assertIn("精英2：攻击力+10%", text)
        self.assertIn("满潜：攻击力+12%（+2%）", text)

    async def test_build_owner_context_degrades_without_account(self) -> None:
        """未绑定账号：_build_owner_context 返回 None（资料查询不报账号错误）。"""
        from ArknightsUID.utils.enduid import adapter

        op = OperatorInfo(title="银灰", name="银灰", rarity="5")
        ev = FakeEvent()
        with mock.patch.object(
            adapter,
            "resolve_credential",
            new=mock.AsyncMock(return_value="未绑定账号提示"),
        ):
            self.assertIsNone(await _build_owner_context(op, ev))

    async def test_build_owner_context_swallows_errors(self) -> None:
        """凭据层抛异常也静默降级为通用展示。"""
        from ArknightsUID.utils.enduid import adapter

        op = OperatorInfo(title="银灰", name="银灰", rarity="5")
        ev = FakeEvent()
        with mock.patch.object(
            adapter,
            "resolve_credential",
            new=mock.AsyncMock(side_effect=RuntimeError("boom")),
        ):
            self.assertIsNone(await _build_owner_context(op, ev))


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
                OperatorTalent(
                    slot="第一天赋",
                    name="领袖",
                    unlock="精英1",
                    description="攻击力+5%",
                    stages=[TalentStage(condition="精英2", effect="攻击力+10%")],
                ),
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
        # 天赋逐阶段结构（模板按 stage 渲染具体数值）
        self.assertEqual(
            context["talents"][0]["stages"],
            [{"condition": "精英2", "effect": "攻击力+10%"}],
        )

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

    async def test_format_operator_shows_descriptions_and_owner_levels(self) -> None:
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="5",
            profession="近卫",
            skills=[
                OperatorSkill(
                    name="技能一",
                    skill_type="自动回复 / 手动触发",
                    levels=[
                        SkillLevelDetail(
                            level="1", description="攻击力+10%", sp_cost="10", duration="30"
                        ),
                        SkillLevelDetail(
                            level="7", description="攻击力+25%", sp_cost="10", duration="35"
                        ),
                        SkillLevelDetail(
                            level="专1", description="攻击力+30%", sp_cost="10", duration="36"
                        ),
                        SkillLevelDetail(
                            level="专3", description="攻击力+40%", sp_cost="10", duration="40"
                        ),
                    ],
                )
            ],
            source_url="https://prts.wiki/w/测试",
        )
        owner = {
            "uid": "12****89",
            "main_skill_lvl": 7,
            "skills": {"技能一": 1},
            "training_remain": "剩约2小时15分",
        }

        # 有 owner：当前级（专1）与满级（专3）详情并列
        text = _format_operator(op, owner)
        self.assertIn("我的练度（UID 12****89）：技能等级 7/7，专精训练剩约2小时15分", text)
        self.assertIn("当前 专1：攻击力+30%（10耗·36持）", text)
        self.assertIn("专3：攻击力+40%（10耗·40持）", text)

        # 无 owner（未绑定/未持有）：仍展示满级描述（修复描述丢失）
        plain = _format_operator(op)
        self.assertIn("专3：攻击力+40%（10耗·40持）", plain)
        self.assertNotIn("我的练度", plain)
        self.assertNotIn("当前 专1", plain)

    async def test_format_operator_owner_maxed_shows_single_level(self) -> None:
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="5",
            skills=[
                OperatorSkill(name="技能一", levels=_full_levels()),
            ],
        )
        owner = {"uid": "12****89", "main_skill_lvl": 7, "skills": {"技能一": 3}}
        text = _format_operator(op, owner)
        # 已专3：只出一条「当前 专3」，不重复罗列满级行
        self.assertIn("当前 专3：", text)
        self.assertEqual(text.count("专3：效果+100%"), 1)

    async def test_build_operator_context_with_owner(self) -> None:
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="5",
            skills=[OperatorSkill(name="技能一", levels=_full_levels())],
        )
        owner = {"uid": "12****89", "main_skill_lvl": 6, "skills": {"技能一": 2}}
        context = await _build_operator_context(op, owner)

        self.assertEqual(context["owner"], owner)
        skill = context["skills"][0]
        self.assertEqual(skill["current"]["level"], "专2")
        self.assertEqual(skill["current"]["meta"], "10耗")
        self.assertEqual(skill["final"]["level"], "专3")

    async def test_build_operator_context_without_owner_has_final_only(self) -> None:
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="5",
            skills=[OperatorSkill(name="技能一", levels=_full_levels())],
        )
        context = await _build_operator_context(op)
        self.assertIsNone(context["owner"])
        skill = context["skills"][0]
        self.assertIsNone(skill["current"])
        self.assertEqual(skill["final"]["level"], "专3")

    async def test_render_contract(self) -> None:
        """ark_wiki_operator.html 与 builder context 的 schema 契约（jinja 全量渲染）。"""
        op = OperatorInfo(
            title="测试",
            name="测试",
            rarity="5",
            profession="近卫",
            skills=[OperatorSkill(name="技能一", levels=_full_levels())],
        )
        owner = {
            "uid": "12****89",
            "main_skill_lvl": 6,
            "skills": {"技能一": 2},
            "training_remain": "剩约2小时15分",
        }
        context = await _build_operator_context(op, owner)
        template = ark_templates.get_template("ark_wiki_operator.html")
        html = template.render(font_css_url="about:blank", **context)
        self.assertIn("当前 专2", html)
        self.assertIn("效果+90%", html)  # 专2 描述（伤害数值必须上卡）
        self.assertIn("效果+100%", html)  # 专3 描述
        self.assertIn("练度档案 UID 12****89", html)
        self.assertIn("专精训练 剩约2小时15分", html)

        # 无 owner（未绑定）渲染不报错、无练度区块
        plain_context = await _build_operator_context(op)
        plain_html = template.render(font_css_url="about:blank", **plain_context)
        self.assertNotIn("练度档案", plain_html)
        self.assertIn("效果+100%", plain_html)

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
        # 单等级：无等级 2 变体区块
        self.assertFalse(context["has_level2"])

    async def test_build_enemy_context_includes_level2_variant(self) -> None:
        enemy = EnemyInfo(
            title="爱国者",
            name="爱国者",
            levels=[
                EnemyLevel(max_hp="45000", atk="1600", defense="500", res="45"),
                # 等级2 变体（差异字段已在解析层从等级1继承补全）
                EnemyLevel(max_hp="60000", atk="2000", defense="500", res="45"),
            ],
        )
        context = await _build_enemy_context(enemy)
        self.assertTrue(context["has_level2"])
        self.assertEqual(context["lv2_hp"], "60000")
        self.assertEqual(context["lv2_atk"], "2000")
        self.assertEqual(context["lv2_defense"], "500")
        self.assertEqual(context["lv2_res"], "45")
        text = _format_enemy(enemy)
        self.assertIn("等级2：", text)
        self.assertIn("生命 60000", text)

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
        # drops 升级为 {name, icon} 结构（图标缺失降级空串）
        self.assertEqual([d["name"] for d in context["drops"]], ["固源岩", "代糖"])
        self.assertIn("icon", context["drops"][0])
        # 无 map_url → map_image 留空，模板需优雅降级
        self.assertEqual(context["map_image"], "")
        self.assertTrue(context["version"])

    async def test_stage_context_enriches_enemy_basic_stats(self) -> None:
        """敌方情报基础数值：敌人页一级 HP/攻/防/抗上卡；查不到降级仅名字。"""
        stage = StageInfo(
            title="1-7",
            code="1-7",
            enemies=[
                StageEnemy(name="士兵", count="18"),
                StageEnemy(name="查无此敌", count="1"),
            ],
        )

        async def fake_query_enemy(name: str):
            if name == "士兵":
                return EnemyInfo(
                    title="士兵",
                    name="士兵",
                    levels=[
                        EnemyLevel(max_hp="1650", atk="200", defense="100", res="0")
                    ],
                )
            return None

        with mock.patch(
            "ArknightsUID.arknightsuid_wiki.query_enemy",
            side_effect=fake_query_enemy,
        ):
            context = await _build_stage_context(stage)

        rows = context["enemies"]
        self.assertEqual(rows[0]["hp"], "1650")
        self.assertEqual(rows[0]["atk"], "200")
        self.assertEqual(rows[0]["defense"], "100")
        self.assertEqual(rows[0]["res"], "0")
        # 查不到的敌人不携带数值键（模板只显示名字）
        self.assertNotIn("hp", rows[1])
        # 文本回退同样带基础数值
        text = _format_stage(stage, rows)
        self.assertIn("士兵x18（HP1650·攻200·防100·抗0）", text)
        self.assertIn("查无此敌x1", text)
        # 无增强数据时回退纯名单（不带空括号）
        plain = _format_stage(stage)
        self.assertIn("士兵x18、查无此敌x1", plain)


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
