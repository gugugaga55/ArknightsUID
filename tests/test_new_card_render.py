"""ark_cultivate / ark_wiki_mastery 两张新卡的模板契约测试。

context 结构与生产组装端一一对应：
- arknightsuid_cultivate.show_cultivation → {"version", "name", "result"}
- arknightsuid_wiki.send_mastery_materials → {"name", "rarity", "profession",
  "skills", "source_url", "version"}

builder 漏键 / 类型错误会让 jinja 渲染抛错，此测试即「数据层 ↔ 模板」的
schema 校验。运行：在 gsuid_core venv 下 `python -m pytest tests/test_new_card_render.py -v`
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

from test_player_parse import _envelope  # noqa: E402

from ArknightsUID.utils.player import parse_player_info  # noqa: E402
from ArknightsUID.utils.render import ark_templates  # noqa: E402

VERSION = "test"


def _render(template_name: str, context: dict) -> str:
    template = ark_templates.get_template(template_name)
    return template.render(font_css_url="about:blank", **context)


class CultivateCardContractTest(unittest.TestCase):
    """ark_cultivate.html：result 结构同 calculate_cultivation 返回。"""

    @classmethod
    def setUpClass(cls) -> None:
        data = parse_player_info(_envelope())
        assert data is not None and data.chars
        cls.char = data.chars[0]

    def _context(self, gap: dict, warnings: list[str] | None = None) -> dict:
        return {
            "version": VERSION,
            "name": self.char.display_name,
            "result": {
                "char": self.char,
                "need": {},
                "current": {
                    "evolve_phase": self.char.evolve_phase,
                    "main_skill_lvl": self.char.main_skill_lvl,
                    "masteries": [s.specialize_level for s in self.char.skills],
                    "equip_levels": [e.level for e in self.char.equips],
                },
                "gap": gap,
                "warnings": warnings or [],
            },
        }

    def test_cultivate_full_gap(self) -> None:
        gap = {
            "gold_lack": 180000,
            "materials": [
                {"id": "m1", "name": "D32钢", "rarity": "5★",
                 "need": 4, "owned": 1, "lack": 3},
                {"id": "m2", "name": "固源岩组", "rarity": "",
                 "need": 10, "owned": 2, "lack": 8},
            ],
            "materials_total": 2,
            "exp_books": [{"name": "高级作战记录", "lack": 42, "need": 60, "owned": 18}],
        }
        html = _render("ark_cultivate.html", self._context(gap, ["口径提示"]))
        self.assertIn("ArknightsUID", html)
        self.assertIn(self.char.display_name, html)
        self.assertIn("D32钢", html)
        self.assertIn("180,000", html)
        self.assertIn("口径提示", html)

    def test_cultivate_empty_gap_renders_graduated(self) -> None:
        gap = {"gold_lack": 0, "materials": [], "materials_total": 0, "exp_books": []}
        html = _render("ark_cultivate.html", self._context(gap))
        self.assertIn("ArknightsUID", html)
        self.assertIn("已毕业", html)

    def test_cultivate_no_skills_no_equips(self) -> None:
        """无技能/无模组干员：masteries/equip_levels 为空列表时不炸。"""
        context = self._context(
            {"gold_lack": 100, "materials": [], "materials_total": 0, "exp_books": []}
        )
        context["result"]["current"]["masteries"] = []
        context["result"]["current"]["equip_levels"] = []
        html = _render("ark_cultivate.html", context)
        self.assertIn("ArknightsUID", html)


class MasteryCardContractTest(unittest.TestCase):
    """ark_wiki_mastery.html：与 send_mastery_materials 组装的 context 同形。"""

    def _context(self) -> dict:
        def m(name: str, count: str) -> dict:
            return {"name": name, "count": count, "icon": ""}

        return {
            "name": "阿米娅",
            "rarity": "5",
            "profession": "术师",
            "skills": [
                {
                    "name": "战术咏唱·γ型",
                    "mastery_costs": [
                        {"mastery": "专精一", "materials": [m("技巧概要·卷3", "8"), m("固源岩组", "5")]},
                        {"mastery": "专精三", "materials": [m("技巧概要·卷3", "15"), m("D32钢", "6")]},
                    ],
                },
                {
                    "name": "精神爆发",
                    "mastery_costs": [
                        {"mastery": "专精一", "materials": [m("技巧概要·卷3", "8")]},
                    ],
                },
            ],
            "source_url": "https://prts.wiki/w/阿米娅",
            "version": VERSION,
        }

    def test_mastery_full(self) -> None:
        html = _render("ark_wiki_mastery.html", self._context())
        self.assertIn("ArknightsUID", html)
        self.assertIn("阿米娅", html)
        self.assertIn("战术咏唱·γ型", html)
        self.assertIn("专精三", html)
        self.assertIn("D32钢", html)
        self.assertIn("×6", html)

    def test_mastery_rarity_not_digit(self) -> None:
        """rarity 非纯数字（富解析失败的兜底形态）时不渲染星级且不炸。"""
        context = self._context()
        context["rarity"] = ""
        html = _render("ark_wiki_mastery.html", context)
        self.assertIn("ArknightsUID", html)


class PushCardContractTest(unittest.TestCase):
    """P4 推送/查询五卡：与 arknightsuid_push / send_calendar 组装的 context 同形。"""

    def test_activity_card(self) -> None:
        html = _render("ark_activity.html", {
            "version": VERSION,
            "activities": [
                {"title": "SideStory「挽歌燃烧殆尽」", "start": "08-09 16:00", "end": "08-30 03:59"},
            ],
            "pools": [
                {"title": "【限定寻访·庆典】", "start": "08-09 16:00", "end": "08-23 03:59"},
            ],
        })
        self.assertIn("ArknightsUID", html)
        self.assertIn("挽歌燃烧殆尽", html)
        self.assertIn("限定寻访", html)

    def test_activity_card_empty_state(self) -> None:
        html = _render("ark_activity.html", {
            "version": VERSION, "activities": [], "pools": [],
        })
        self.assertIn("暂无进行中的活动或卡池", html)

    def test_announce_card(self) -> None:
        html = _render("ark_announce.html", {
            "version": VERSION,
            "anns": [
                {"title": "闪断更新公告", "date": "08-08", "url": "https://example.com/1"},
                {"title": "新干员追加", "date": "", "url": ""},
            ],
        })
        self.assertIn("ArknightsUID", html)
        self.assertIn("闪断更新公告", html)
        self.assertIn("08-08", html)

    def test_drop_rate_card(self) -> None:
        html = _render("ark_drop_rate.html", {
            "version": VERSION,
            "item_name": "固源岩组",
            "rows": [
                {"stage_code": "1-7", "zone": "第一章", "rate": 1.243,
                 "ap_expect": 4.8, "times": 1250432, "rate_pct": 100},
                {"stage_code": "6-5", "zone": "第六章", "rate": 1.102,
                 "ap_expect": 16.3, "times": 84213, "rate_pct": 89},
            ],
        })
        self.assertIn("ArknightsUID", html)
        self.assertIn("固源岩组", html)
        self.assertIn("1-7", html)
        self.assertIn("1.243", html)

    def test_stage_rank_card(self) -> None:
        html = _render("ark_stage_rank.html", {
            "version": VERSION,
            "group": "主线",
            "rows": [
                {"stage_code": "1-7", "zone": "第一章", "spm": 4.86,
                 "ap_cost": 6, "spm_pct": 100},
                {"stage_code": "JT8-3", "zone": "第八章", "spm": 4.52,
                 "ap_cost": 18, "spm_pct": 93},
            ],
        })
        self.assertIn("ArknightsUID", html)
        self.assertIn("主线", html)
        self.assertIn("4.86", html)

    def test_calendar_card(self) -> None:
        html = _render("ark_calendar.html", {
            "version": VERSION,
            "now_str": "2026-08-15 12:00",
            "game_version": "2.7.21",
            "game_updated": "2026-08-09",
            "maint_text": "维护中，预计 2026-08-15 14:00 结束",
            "activities": [
                {"title": "危机合约「弧光作战」", "remaining": "还剩 15 天",
                 "start": "08-16 16:00", "end": "08-30 03:59"},
            ],
            "pools": [
                {"title": "【中坚寻访】", "remaining": "还剩 13 天"},
            ],
        })
        self.assertIn("ArknightsUID", html)
        self.assertIn("2.7.21", html)
        self.assertIn("维护中", html)
        self.assertIn("还剩 15 天", html)

    def test_calendar_card_minimal(self) -> None:
        """无版本信息/无维护/无活动时（仅卡池）不炸。"""
        html = _render("ark_calendar.html", {
            "version": VERSION,
            "now_str": "2026-08-15 12:00",
            "game_version": "",
            "game_updated": "",
            "maint_text": "",
            "activities": [],
            "pools": [{"title": "【中坚寻访】", "remaining": "还剩 13 天"}],
        })
        self.assertIn("ArknightsUID", html)
        self.assertIn("中坚寻访", html)


class RecruitAccountsCardContractTest(unittest.TestCase):
    """公招结果卡 / 账号列表卡：与 send_recruit_query / show_ark_accounts 同形。"""

    def test_recruit_card(self) -> None:
        html = _render("ark_recruit.html", {
            "version": VERSION,
            "tags": "高级资深干员 + 术师",
            "total": 4,
            "rows": [
                {"rarity": 6, "name": "逻各斯", "profession": "术师", "tags": "输出/削弱"},
                {"rarity": 5, "name": "阿米娅", "profession": "术师", "tags": ""},
            ],
            "truncated": False,
        })
        self.assertIn("ArknightsUID", html)
        self.assertIn("逻各斯", html)
        self.assertIn("高级资深干员", html)

    def test_recruit_card_truncated(self) -> None:
        html = _render("ark_recruit.html", {
            "version": VERSION,
            "tags": "输出",
            "total": 35,
            "rows": [
                {"rarity": 4, "name": "清道夫", "profession": "先锋", "tags": "输出"},
            ],
            "truncated": True,
        })
        self.assertIn("共 35 名", html)

    def test_accounts_card(self) -> None:
        html = _render("ark_accounts.html", {
            "version": VERSION,
            "accounts": [
                {"index": 1, "nickname": "刀客塔", "uid": "12****89", "usable": True},
                {"index": 2, "nickname": "未记录昵称", "uid": "34****56", "usable": False},
            ],
            "orphan_uids": [],
        })
        self.assertIn("ArknightsUID", html)
        self.assertIn("刀客塔", html)
        self.assertIn("凭据失效", html)

    def test_accounts_card_orphan_and_empty(self) -> None:
        html = _render("ark_accounts.html", {
            "version": VERSION, "accounts": [], "orphan_uids": ["99****00"],
        })
        self.assertIn("凭据缺失", html)
        html_empty = _render("ark_accounts.html", {
            "version": VERSION, "accounts": [], "orphan_uids": [],
        })
        self.assertIn("未找到明日方舟账号", html_empty)


class ItemIconTest(unittest.TestCase):
    """itemicon 工具：PRTS MediaWiki md5 路径规则 + 材料文本拆分。"""

    def test_prts_url_md5_scheme(self) -> None:
        from ArknightsUID.utils.gamedata.itemicon import prts_item_icon_url

        url = prts_item_icon_url("固源岩组")
        # PRTS 实际存储路径前缀（页面实测）：/2/2e/道具_带框_固源岩组.png
        self.assertTrue(url.startswith("https://media.prts.wiki/2/2e/"), url)
        import urllib.parse

        self.assertIn("道具_带框_固源岩组.png", urllib.parse.unquote(url))
        self.assertEqual(prts_item_icon_url("  "), "")
        self.assertEqual(prts_item_icon_url(""), "")

    def test_split_material_text(self) -> None:
        from ArknightsUID.utils.gamedata.itemicon import split_material_text

        self.assertEqual(split_material_text("技巧概要·卷3 ×8"), ("技巧概要·卷3", "8"))
        self.assertEqual(split_material_text("D32钢x6"), ("D32钢", "6"))
        self.assertEqual(split_material_text("扭转醇*4"), ("扭转醇", "4"))
        self.assertEqual(split_material_text("无数量材料"), ("无数量材料", ""))


if __name__ == "__main__":
    unittest.main()
