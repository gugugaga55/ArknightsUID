"""肉鸽敌人 buff 面板测试：乘数语义 / 主题判定 / 面板构建 / 卡片契约。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.gamedata.rogue_enemy import (
    build_diff_panels,
    detect_theme,
    match_rogue_enemies,
)


def _db() -> dict:
    return {
        "enemy_1_boss": {
            "key": "enemy_1_boss", "name_zh": "歌利亚",
            "type": ["BOSS", "melee", "sarkaz"],
            "stats": [{"hp": 80000, "atk": 2350, "def": 1000, "res": 25.0, "weight": 5}],
            "forms": [{"title": None, "normal_attack": {"atk_type": ["melee", "phys"]}}],
        },
        "enemy_2_normal": {
            "key": "enemy_2_normal", "name_zh": "源石虫",
            "type": ["NORMAL", "melee"],
            "stats": [
                {"hp": 1050, "atk": 185, "def": 0, "res": 0.0, "weight": 1},
                {"hp": 1550, "atk": 240, "def": 0, "res": 0.0, "weight": 1},
            ],
            "forms": [
                {"title": None, "normal_attack": {"atk_type": ["melee", "phys"]}},
                {"title": None, "normal_attack": {"atk_type": ["melee", "phys"]}},
            ],
        },
        "enemy_3": {
            "key": "enemy_3", "name_zh": "源石虫·α",
            "type": ["NORMAL"], "stats": [{"hp": 1, "atk": 1, "def": 0, "res": 0}],
            "forms": [],
        },
    }


def _sarkaz_diffs() -> list:
    return [
        {"difficulty": 0, "effects": []},
        {"difficulty": 3, "effects": [
            {"targets": ["enemy_9_trap"], "mods": [{"key": "hp", "value": 0.2, "mode": "mul", "order": "initial"}]}
        ]},
        {"difficulty": 4, "effects": [
            {"targets": ["ELITE", "BOSS"], "mods": [{"key": "hp", "value": 1.2, "mode": "mul"}]}
        ]},
        {"difficulty": 7, "effects": [
            {"targets": ["ELITE", "BOSS"], "mods": [{"key": "atk", "value": 1.1, "mode": "mul"}]}
        ]},
        {"difficulty": 10, "effects": [
            {"targets": ["ALL"], "mods": [{"key": "dmg_res", "value": 0.1, "mode": "mul"}]}
        ]},
        {"difficulty": 14, "effects": [
            {"targets": ["enemy_2_normal"], "mods": [{"key": "hp", "value": 1.5, "mode": "mul"}]}
        ]},
    ]


class MatchTest(unittest.TestCase):
    def test_exact_prefix_contains(self) -> None:
        db = _db()
        self.assertEqual(match_rogue_enemies("歌利亚", db), ["enemy_1_boss"])
        # 精确名优先于前缀（「源石虫」精确命中，不进候选分支）
        self.assertEqual(match_rogue_enemies("源石虫", db), ["enemy_2_normal"])
        # 非精确前缀多命中（源石虫/源石虫·α）返回候选
        multi = match_rogue_enemies("源石", db)
        self.assertEqual(sorted(multi), ["enemy_2_normal", "enemy_3"])
        self.assertEqual(match_rogue_enemies("歌", db), ["enemy_1_boss"])

    def test_no_match(self) -> None:
        self.assertEqual(match_rogue_enemies("不存在", _db()), [])


class DetectThemeTest(unittest.TestCase):
    def test_explicit_target_wins(self) -> None:
        # enemy_2_normal 被萨卡兹难度 14 显式点名（无 type 标签也可归属）
        diff = {"sarkaz": _sarkaz_diffs()}
        self.assertEqual(
            detect_theme("enemy_2_normal", ["NORMAL"], diff), "sarkaz"
        )

    def test_type_hint_fallback(self) -> None:
        self.assertEqual(
            detect_theme("enemy_1_boss", ["BOSS", "sarkaz"], {}), "sarkaz"
        )

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(detect_theme("enemy_x", ["NORMAL"], {}))


class DiffPanelTest(unittest.TestCase):
    def test_cumulative_mods_and_noob_cut(self) -> None:
        entry = _db()["enemy_1_boss"]
        rows = build_diff_panels(entry, _sarkaz_diffs())
        by_grade = {r["grade"]: r for r in rows}
        # 新手档 HP 削减
        self.assertEqual(by_grade[0]["stats"]["hp"], 64000)
        # 难度4：BOSS HP ×1.2（累积），难度 3 只对陷阱生效不影响本敌
        self.assertEqual(by_grade[4]["stats"]["hp"], 96000)
        # 难度7：再叠 攻×1.1
        self.assertEqual(by_grade[7]["stats"]["hp"], 96000)
        self.assertEqual(by_grade[7]["stats"]["atk"], 2585)
        # 难度10：dmg_res 非面板字段 → note，数值不变
        self.assertEqual(by_grade[10]["notes"], ["dmg_res 0.1"])
        # 难度14 只点名 enemy_2_normal，本敌不受影响 → 与 10 合并
        labels = [r["label"] for r in rows]
        self.assertTrue(any("10~14" in label for label in labels), labels)

    def test_targeted_enemy_gets_own_mods(self) -> None:
        entry = _db()["enemy_2_normal"]
        rows = build_diff_panels(entry, _sarkaz_diffs())
        by_grade = {r["grade"]: r for r in rows}
        # 难度14 显式点名：HP ×1.5（形态0 基础 1050）
        self.assertEqual(by_grade[14]["stats"]["hp"], 1575)


class CommandContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_context_and_render(self) -> None:
        """命令上下文 ↔ 模板契约（jinja 渲染；图标失败降级）。"""
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import (
            _build_rogue_enemy_context,
            _format_rogue_enemy,
        )
        from ArknightsUID.utils.render import ark_templates

        async def fake_diffs():
            return {"sarkaz": _sarkaz_diffs()}

        async def fake_icon(key: str) -> str:
            return "BASE64ICON"

        with (
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_enemy_db",
                new=mock.AsyncMock(return_value=_db()),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_difficulty_mods",
                new=fake_diffs,
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.enemy_icon_b64",
                new=fake_icon,
            ),
        ):
            result = await _build_rogue_enemy_context("歌利亚")

        self.assertNotIsInstance(result, str)
        self.assertEqual(result["name"], "歌利亚")
        self.assertEqual(result["theme_name"], "萨卡兹的无终奇语")
        self.assertEqual(result["forms"][0]["hp"], 80000)
        self.assertIn("难度", result["panels"][0]["label"])

        text = _format_rogue_enemy(result)
        self.assertIn("肉鸽敌人：歌利亚", text)
        self.assertIn("HP96000", text)

        html = ark_templates.get_template("ark_rogue_enemy.html").render(
            font_css_url="about:blank", **result
        )
        self.assertIn("歌利亚", html)
        self.assertIn("96000", html)
        self.assertIn("tomimi.dev", html)

    async def test_multi_match_returns_hint(self) -> None:
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import _build_rogue_enemy_context

        with mock.patch(
            "ArknightsUID.utils.gamedata.rogue_enemy.load_enemy_db",
            new=mock.AsyncMock(return_value=_db()),
        ):
            result = await _build_rogue_enemy_context("源石")
        self.assertIsInstance(result, str)
        self.assertIn("候选", result)


if __name__ == "__main__":
    unittest.main()
