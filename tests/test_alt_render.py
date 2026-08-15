"""「罗德岛作战终端」alt 模板集契约测试。

用真实数据层产物（非手写 mock）渲染 alt/ 下的探索稿模板：
- alt/status.html ← build_status_context 真实 builder
- alt/cultivate.html ← calculate_cultivation 的结果结构（本地组装）
确保探索稿与生产数据层 schema 一致，可随时晋升为生产模板。
"""
from __future__ import annotations

import unittest

from test_player_parse import _envelope

from ArknightsUID.utils.cultivate import parse_cultivate_cost, summarize_gap, build_remaining_need
from ArknightsUID.utils.player import build_status_context, parse_player_info
from ArknightsUID.utils.render import ark_templates


def _render(name: str, context: dict) -> str:
    return ark_templates.get_template(name).render(font_css_url="about:blank", **context)


class AltRenderContractTest(unittest.TestCase):
    def test_alt_status_with_real_builder_context(self) -> None:
        data = parse_player_info(_envelope())
        self.assertIsNotNone(data)
        assert data is not None
        context = build_status_context(data, "v", "123***789", now=1e9 + 3600)
        self.assertIsNotNone(context)
        html = _render("alt/status.html", context)
        self.assertIn("terminal", html)
        self.assertIn("SANITY", html)

    def test_alt_cultivate_with_result_shape(self) -> None:
        # 与 calculate_cultivation 返回结构同形（char → 展示字段 + gap + current）
        envelope = _envelope()
        cost = parse_cultivate_cost(
            {
                "code": 0,
                "data": {
                    "evolvePhaseCost": [
                        {"items": [{"id": "3003", "count": 4}]},
                        {"items": [{"id": "3004", "count": 2}]},
                    ],
                    "mainSkillLevelCost": [
                        {"items": [{"id": "2001", "count": 2}]} for _ in range(7)
                    ],
                    "skillCost": [
                        {
                            "name": "战术咏唱",
                            "levelCost": [
                                {"items": [{"id": "2001", "count": 1}]} for _ in range(10)
                            ],
                        }
                    ],
                    "equipCost": [],
                },
            }
        )
        assert cost is not None
        data = parse_player_info(envelope)
        assert data is not None
        char = data.chars[0]
        need, _ = build_remaining_need(cost, char)
        gap = summarize_gap(need, {"3003": 10}, {})
        context = {
            "name": char.display_name,
            "profession": f"{char.profession_name} {char.profession}",
            "current": {
                "evolve_phase": char.evolve_phase,
                "main_skill_lvl": char.main_skill_lvl,
                "masteries": [s.specialize_level for s in char.skills],
                "equip_levels": [e.level for e in char.equips],
            },
            "gap": gap,
            "version": "v",
        }
        html = _render("alt/cultivate.html", context)
        self.assertIn("固源岩组" if gap["materials"] else "已达成", html)
        self.assertIn("Terminal/04", html)

    def test_alt_cultivate_graduated_state(self) -> None:
        context = {
            "name": "银灰",
            "profession": "近卫 WARRIOR",
            "current": {"evolve_phase": 2, "main_skill_lvl": 7, "masteries": [3, 3, 3], "equip_levels": [3]},
            "gap": {"materials": [], "materials_total": 0, "exp_books": [], "gold_lack": 0},
            "version": "v",
        }
        html = _render("alt/cultivate.html", context)
        self.assertIn("CLEAR", html)


if __name__ == "__main__":
    unittest.main()
