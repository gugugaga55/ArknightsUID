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
            event="初始",
            levels=[
                EnemyLevel(
                    max_hp="1000", atk="100", defense="0", res="0", weight="1",
                    move_speed="1.1", attack_interval="1.7", silence_res="有",
                ),
            ],
        )
        with mock.patch(
            "ArknightsUID.arknightsuid_wiki.enemy_icon_b64",
            new=mock.AsyncMock(return_value=""),
        ):
            context = await _build_enemy_context(enemy)

        self.assertEqual(context["name"], "源石虫")
        self.assertTrue(context["has_stats"])
        self.assertEqual(context["max_hp"], "1000")
        self.assertEqual(context["atk"], "100")
        self.assertEqual(context["defense"], "0")
        self.assertEqual(context["event"], "初始")
        # 次级属性行：非 0 项进入（重量/移动/间隔），防御 "0" 不进
        labels = {row["label"] for row in context["secondary_stats"]}
        self.assertEqual(labels, {"重量", "移动速度", "攻击间隔"})
        # 生效抗性（有）→ chips；"无" 抗性不进
        self.assertEqual(context["resistances"], [{"label": "沉默", "value": "有"}])
        self.assertTrue(context["has_resistances"])
        # 单等级：无变体区块
        self.assertEqual(context["variants"], [])

    async def test_build_enemy_context_includes_level2_variant(self) -> None:
        enemy = EnemyInfo(
            title="爱国者",
            name="爱国者",
            levels=[
                EnemyLevel(
                    max_hp="45000", atk="1600", defense="500", res="45",
                    weight="7", move_speed="0.4", attack_interval="4.0",
                    silence_res="有", hp_regen="100",
                    abilities=["行军姿态：防御力大幅提升，阻挡数+1"],
                ),
                # 等级2 变体（差异字段已在解析层从等级1继承补全）+ 独立机制
                EnemyLevel(
                    max_hp="60000", atk="2000", defense="500", res="45",
                    abilities=["毁灭姿态：攻击造成范围伤害"],
                ),
            ],
        )
        with mock.patch(
            "ArknightsUID.arknightsuid_wiki.enemy_icon_b64",
            new=mock.AsyncMock(return_value=""),
        ):
            context = await _build_enemy_context(enemy)
        self.assertEqual(len(context["variants"]), 1)
        variant = context["variants"][0]
        self.assertEqual(variant["label"], "等级2")
        self.assertEqual(variant["hp"], "60000")
        self.assertEqual(variant["atk"], "2000")
        self.assertEqual(variant["defense"], "500")
        self.assertEqual(variant["res"], "45")
        self.assertEqual(
            context["abilities"], ["行军姿态：防御力大幅提升，阻挡数+1"]
        )
        self.assertEqual(variant["abilities"], ["毁灭姿态：攻击造成范围伤害"])
        # 次级属性含生命恢复（非 0）；抗性 chips 命中沉默
        secondary = {row["label"]: row["value"] for row in context["secondary_stats"]}
        self.assertEqual(secondary["生命恢复"], "100")
        self.assertEqual(secondary["攻击间隔"], "4.0")
        self.assertEqual(context["resistances"], [{"label": "沉默", "value": "有"}])
        text = _format_enemy(enemy)
        self.assertIn("等级2：", text)
        self.assertIn("生命 60000", text)
        self.assertIn("等级2机制：毁灭姿态：攻击造成范围伤害", text)
        self.assertIn("抗性：沉默", text)
        html_render = ark_templates.get_template("ark_wiki_enemy.html").render(
            font_css_url="about:blank", **context
        )
        self.assertIn("毁灭姿态", html_render)
        self.assertIn("等级2机制", html_render)
        self.assertIn("攻击间隔", html_render)
        self.assertIn("抗性 · 沉默", html_render)

    async def test_build_enemy_context_level3_variant_and_no_resistance(self) -> None:
        """弑君者类 3 等级敌人：变体逐级展示；全无抗性 → has_resistances False。"""
        enemy = EnemyInfo(
            title="弑君者",
            name="弑君者",
            levels=[
                EnemyLevel(max_hp="6000", atk="400", defense="120", res="50"),
                EnemyLevel(max_hp="10000", atk="550", defense="150", res="50"),
                EnemyLevel(
                    max_hp="20000", atk="700", defense="250", res="50",
                    abilities=["技能·闪现：仅被阻挡时可触发"],
                ),
            ],
        )
        with mock.patch(
            "ArknightsUID.arknightsuid_wiki.enemy_icon_b64",
            new=mock.AsyncMock(return_value=""),
        ):
            context = await _build_enemy_context(enemy)
        self.assertEqual(
            [v["label"] for v in context["variants"]], ["等级2", "等级3"]
        )
        self.assertEqual(context["variants"][1]["hp"], "20000")
        self.assertEqual(
            context["variants"][1]["abilities"], ["技能·闪现：仅被阻挡时可触发"]
        )
        self.assertFalse(context["has_resistances"])
        text = _format_enemy(enemy)
        self.assertIn("等级3：生命 20000", text)
        self.assertIn("等级3机制：技能·闪现", text)
        html_render = ark_templates.get_template("ark_wiki_enemy.html").render(
            font_css_url="about:blank", **context
        )
        self.assertIn("等级3变体", html_render)
        self.assertIn("异常抗性 · 无", html_render)

    async def test_ability_blocks_parse_and_context(self) -> None:
        """机制分块：技能（含姿态标签/SP 参数）/ 天赋 / 能力 三类配色结构。"""
        from ArknightsUID.arknightsuid_wiki import _ability_blocks

        blocks = _ability_blocks(
            [
                "技能·投枪\n[毁灭姿态]：锁定目标造成伤害\n※不可对空（初始15·消耗20）",
                "技能·生命献祭：60秒内无法移动（初始9999·消耗9999）",
                "天赋：免疫脉冲波\n在场时令全场敌方单位攻击力+20%",
                "能力：能够自然回复生命",
                "无法识别的整行",
            ]
        )
        self.assertEqual([b["kind"] for b in blocks], [
            "skill", "skill", "talent", "other", "other",
        ])
        skill = blocks[0]
        self.assertEqual(skill["name"], "投枪")
        self.assertEqual(skill["tag"], "[毁灭姿态]")
        self.assertEqual(skill["params"], ["初始15", "消耗20"])
        # SP 参数从正文剥离，正文保留 ※ 注记行
        self.assertNotIn("初始", skill["text"])
        self.assertIn("※不可对空", skill["text"])
        talent = blocks[2]
        self.assertEqual(talent["name"], "免疫脉冲波")
        self.assertEqual(talent["text"], "在场时令全场敌方单位攻击力+20%")
        self.assertEqual(blocks[3]["badge"], "能力")
        self.assertEqual(blocks[4]["text"], "无法识别的整行")

        enemy = EnemyInfo(
            title="爱国者",
            name="爱国者",
            levels=[
                EnemyLevel(
                    max_hp="45000", atk="1600", defense="500", res="45",
                    abilities=["技能·投枪：造成伤害（初始15·消耗20）"],
                ),
                EnemyLevel(abilities=["技能·强化投枪：造成更多伤害"]),
            ],
        )
        with mock.patch(
            "ArknightsUID.arknightsuid_wiki.enemy_icon_b64",
            new=mock.AsyncMock(return_value=""),
        ):
            context = await _build_enemy_context(enemy)
        self.assertEqual(len(context["ability_blocks"]), 1)
        self.assertEqual(context["ability_blocks"][0]["params"], ["初始15", "消耗20"])
        self.assertEqual(context["variants"][0]["blocks"][0]["name"], "强化投枪")
        html_render = ark_templates.get_template("ark_wiki_enemy.html").render(
            font_css_url="about:blank", **context
        )
        # 分块配色类与 SP 参数标签进入渲染产物
        self.assertIn("ab--skill", html_render)
        self.assertIn("ab-param", html_render)

    async def test_stage_condition_blocks_assembly(self) -> None:
        """作战条件统一区块：突袭 / 环境条件 / 全息指标（基础+附加）/ CC 预留。"""
        from ArknightsUID.utils.prts.client import HoloIndicator

        stage = StageInfo(
            title="全息H5-1 ＃炼狱行动-1",
            code="H5-1",
            name="炼狱行动-1",
            assault_info="敌方单位的生命值、攻击力、防御力都提升至120%",
            assault_diffs=["部署上限：8→6（-2）"],
            env_conditions=["渴血的子裔的吸血效果大幅提升"],
            holo_indicators=[
                HoloIndicator(
                    section="基础：俯冲轰炸", token="r_fixed_rune",
                    text="我方防御点可承受的敌方数量变为1", kind="锁定", grouped=True,
                ),
                HoloIndicator(
                    section="基础：俯冲轰炸", token="r_enemy_speed_a",
                    text="所有敌人移动速度提升60%", kind="指定", grouped=True,
                ),
                HoloIndicator(
                    section="附加条件", token="r_extra_1",
                    text="出现额外的帝国炮火中枢先兆者", score="2", grouped=False,
                ),
                HoloIndicator(
                    section="附加条件", token="r_conflict_a",
                    text="豪华赞助无人机免疫沉睡", score="2", grouped=True,
                ),
            ],
            has_contracts=True,
        )

        async def no_map(code: str, name: str) -> str:
            return ""

        with mock.patch(
            "ArknightsUID.arknightsuid_wiki.resolve_map_file_url", new=no_map
        ):
            context = await _build_stage_context(stage)

        self.assertTrue(context["has_assault"])
        blocks = context["condition_blocks"]
        self.assertEqual(
            [b["label"] for b in blocks],
            ["突袭条件", "环境条件", "矩阵指标 · 基础：俯冲轰炸", "矩阵指标 · 附加条件", "合约词条"],
        )
        # 突袭块：情报散文 + 数值差异
        self.assertEqual(blocks[0]["tone"], "red")
        self.assertEqual(len(blocks[0]["rows"]), 2)
        # 指标块：基础黄（锁定红标）/ 附加蓝（分数黄标 + 冲突组）
        basic, extra = blocks[2], blocks[3]
        self.assertEqual(basic["tone"], "yellow")
        self.assertEqual(basic["note"], "每组必选其一")
        self.assertEqual(basic["rows"][0]["tags"], ["锁定"])
        self.assertEqual(extra["tone"], "blue")
        self.assertEqual(extra["rows"][0]["tags"], ["分数2"])
        self.assertEqual(extra["rows"][1]["tags"], ["分数2", "冲突"])
        # CC 预留：ghost 提示块
        self.assertEqual(blocks[4]["tone"], "ghost")

        html_render = ark_templates.get_template("ark_wiki_stage.html").render(
            font_css_url="about:blank", **context
        )
        self.assertIn("作战条件", html_render)
        self.assertIn("cond--red", html_render)
        self.assertIn("cond--yellow", html_render)
        self.assertIn("is-lock", html_render)
        self.assertIn("is-score", html_render)
        # 文本回退包含条件行
        from ArknightsUID.arknightsuid_wiki import _format_stage

        text = _format_stage(stage)
        self.assertIn("突袭情报：", text)
        self.assertIn("环境条件：", text)
        self.assertIn("矩阵指标：4 条", text)
        self.assertIn("危机合约关卡", text)

    async def test_build_stage_context_without_map_file(self) -> None:
        """地图文件不存在/解析失败：map_image 留空，其余字段不受影响。"""
        stage = StageInfo(
            title="1-7",
            code="1-7",
            name="暴君",
            ap_cost="6",
            drops=["固源岩", "代糖"],
            enemies=[],
        )

        async def no_map(code: str, name: str) -> str:
            return ""

        async def no_fetch(url, cache_path, *args, **kwargs):
            return ""

        import tempfile
        from pathlib import Path as _P

        import ArknightsUID.utils.render as render_mod
        import ArknightsUID.utils.resource.RESOURCE_PATH as rp_mod

        with (
            mock.patch(
                "ArknightsUID.arknightsuid_wiki.resolve_map_file_url", new=no_map
            ),
            mock.patch.object(render_mod, "get_image_b64_with_cache", no_fetch),
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(rp_mod, "CACHE_PATH", _P(tmp)),
        ):
            context = await _build_stage_context(stage)

        self.assertEqual(context["title"], "1-7")
        self.assertEqual(context["name"], "暴君")
        self.assertEqual(context["ap_cost"], "6")
        # drops 升级为 {name, icon} 结构（图标缺失降级空串）
        self.assertEqual([d["name"] for d in context["drops"]], ["固源岩", "代糖"])
        self.assertIn("icon", context["drops"][0])
        self.assertEqual(context["map_image"], "")
        self.assertTrue(context["version"])

    async def test_build_stage_context_downloads_map_image(self) -> None:
        """地图：md5 直连命中 → 零 API 直接下载上卡。"""
        stage = StageInfo(
            title="1-7",
            code="1-7",
            name="暴君",
            map_url="https://map.prts.wiki/1-7",
        )
        import hashlib as _hl

        digest = _hl.md5("1-7_暴君_地图.png".encode("utf-8")).hexdigest()
        md5_url = f"https://media.prts.wiki/{digest[0]}/{digest[:2]}/1-7_%E6%9A%B4%E5%90%9B_%E5%9C%B0%E5%9B%BE.png"

        async def fake_fetch(url, cache_path, *args, **kwargs):
            self.assertEqual(url, md5_url)
            return "BASE64MAP"

        async def fake_resolve(code: str, name: str) -> str:
            raise AssertionError("md5 直连命中时不应回退 API 查询")

        import ArknightsUID.utils.render as render_mod

        with (
            mock.patch(
                "ArknightsUID.arknightsuid_wiki.resolve_map_file_url",
                new=fake_resolve,
            ),
            mock.patch.object(render_mod, "get_image_b64_with_cache", fake_fetch),
        ):
            context = await _build_stage_context(stage)

        self.assertEqual(context["map_image"], "BASE64MAP")
        self.assertEqual(context["map_url"], "https://map.prts.wiki/1-7")
        # 渲染契约：地图图与 PRTS.Map 链接行都上卡
        template = ark_templates.get_template("ark_wiki_stage.html")
        html = template.render(font_css_url="about:blank", **context)
        self.assertIn("关卡地图", html)
        self.assertIn("PRTS.MAP // https://map.prts.wiki/1-7", html)

    async def test_build_stage_map_falls_back_to_api(self) -> None:
        """地图：md5 直连与辅助命名均 404 → 兜底 imageinfo 查询。"""
        stage = StageInfo(
            title="CW-1",
            code="CW-1",
            name="触前瞻",
            map_url="https://map.prts.wiki/CW-1",
        )

        async def fake_fetch(url, cache_path, *args, **kwargs):
            # md5 直连（标准+辅助）都拿不到；API 兜底 URL 才有图
            if url == "https://media.prts.wiki/api/found.png":
                return "API_MAP"
            return ""

        async def fake_resolve(code: str, name: str) -> str:
            return "https://media.prts.wiki/api/found.png"

        import tempfile
        from pathlib import Path as _P

        import ArknightsUID.utils.render as render_mod
        import ArknightsUID.utils.resource.RESOURCE_PATH as rp_mod

        with (
            mock.patch(
                "ArknightsUID.arknightsuid_wiki.resolve_map_file_url",
                new=fake_resolve,
            ),
            mock.patch.object(render_mod, "get_image_b64_with_cache", fake_fetch),
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(rp_mod, "CACHE_PATH", _P(tmp)),
        ):
            context = await _build_stage_context(stage)

        self.assertEqual(context["map_image"], "API_MAP")

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
                        EnemyLevel(
                            max_hp="1650",
                            atk="200",
                            defense="100",
                            res="0",
                            abilities=["天赋：不会攻击飞行单位"],
                        )
                    ],
                )
            return None

        async def fake_icon(name: str) -> str:
            return "BASE64ICON" if name == "士兵" else ""

        with (
            mock.patch(
                "ArknightsUID.arknightsuid_wiki.query_enemy",
                side_effect=fake_query_enemy,
            ),
            mock.patch(
                "ArknightsUID.arknightsuid_wiki.enemy_icon_b64",
                side_effect=fake_icon,
            ),
        ):
            context = await _build_stage_context(stage)

        rows = context["enemies"]
        self.assertEqual(rows[0]["hp"], "1650")
        self.assertEqual(rows[0]["atk"], "200")
        self.assertEqual(rows[0]["defense"], "100")
        self.assertEqual(rows[0]["res"], "0")
        # 头像命中上卡，查不到的敌人空串降级（模板只显示名字）
        self.assertEqual(rows[0]["icon"], "BASE64ICON")
        self.assertEqual(rows[1]["icon"], "")
        # 查不到的敌人不携带数值键（模板只显示名字）
        self.assertNotIn("hp", rows[1])
        # 文本回退同样带基础数值
        # 机制压缩行（多行条目展平）
        self.assertEqual(rows[0]["mechanics"], "天赋：不会攻击飞行单位")
        self.assertEqual(rows[1]["mechanics"], "")
        text = _format_stage(stage, rows)
        self.assertIn("士兵x18（HP1650·攻200·防100·抗0）", text)
        self.assertIn("机制：天赋：不会攻击飞行单位", text)
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
