"""用户自定义肉鸽期名（ark肉鸽别名）：解析接入 + 目标校验 + 存储前缀过滤。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.arknightsuid_rogue import (
    _load_user_theme_aliases,
    _parse_alias_pair,
    _parse_rogue_query,
)
from ArknightsUID.utils.gamedata.rogue_enemy import resolve_theme_dir


class ParseWithUserAliasTest(unittest.TestCase):
    def test_user_alias_routes_theme(self) -> None:
        name, theme, diff, elite = _parse_rogue_query(
            "汉科 树海 N15", {"树海": "black"}
        )
        self.assertEqual((name, theme, diff, elite), ("汉科", "black", 15, False))

    def test_user_alias_takes_precedence_over_builtin(self) -> None:
        # 用户把「水月」重定向到别的期：用户意图优先
        name, theme, _, _ = _parse_rogue_query(
            "延命塑路者 水月", {"水月": "sui"}
        )
        self.assertEqual(theme, "sui")
        self.assertEqual(name, "延命塑路者")

    def test_builtin_alias_still_works_without_user_hit(self) -> None:
        _, theme, _, _ = _parse_rogue_query("歌利亚 界园", {"树海": "black"})
        self.assertEqual(theme, "sui")

    def test_user_alias_is_token_exact_not_substring(self) -> None:
        # 敌人名内含别名子串不得误伤（整词匹配）
        name, theme, _, _ = _parse_rogue_query("树海魔剑士 N7", {"树海": "black"})
        self.assertIsNone(theme)
        self.assertEqual(name, "树海魔剑士")

    def test_alias_key_case_insensitive(self) -> None:
        _, theme, _, _ = _parse_rogue_query("汉科 BLACK", {"black": "sui"})
        # extra_aliases 用小写键查 token.lower()；内建 black 目录名不在 THEME_ALIASES，
        # 这里命中用户别名
        self.assertEqual(theme, "sui")


class ResolveThemeDirTest(unittest.TestCase):
    def test_display_name_and_dir_and_alias(self) -> None:
        self.assertEqual(resolve_theme_dir("沉沦者的黑流树海"), "black")
        self.assertEqual(resolve_theme_dir("black"), "black")
        self.assertEqual(resolve_theme_dir("树海"), "black")
        self.assertEqual(resolve_theme_dir("岁的界园志异"), "sui")
        self.assertEqual(resolve_theme_dir("界园肉鸽"), "sui")

    def test_unresolvable_returns_none(self) -> None:
        self.assertIsNone(resolve_theme_dir("不存在的期"))
        self.assertIsNone(resolve_theme_dir(""))
        self.assertIsNone(resolve_theme_dir("  "))


class ParseAliasPairTest(unittest.TestCase):
    def test_separator_forms(self) -> None:
        self.assertEqual(_parse_alias_pair("树海=black"), ("树海", "black"))
        self.assertEqual(_parse_alias_pair("树海＝black"), ("树海", "black"))
        self.assertEqual(_parse_alias_pair("树海 → black"), ("树海", "black"))
        self.assertEqual(_parse_alias_pair("树海 black"), ("树海", "black"))
        self.assertEqual(
            _parse_alias_pair("树海=沉沦者的黑流树海"), ("树海", "沉沦者的黑流树海")
        )

    def test_invalid_forms(self) -> None:
        self.assertIsNone(_parse_alias_pair(""))
        self.assertIsNone(_parse_alias_pair("只有一个词"))
        self.assertIsNone(_parse_alias_pair("树海="))


class LoadUserThemeAliasesTest(unittest.IsolatedAsyncioTestCase):
    async def test_filters_rogue_prefix_only(self) -> None:
        rows = [
            mock.Mock(alias="树海", target="rogue:black"),
            mock.Mock(alias="银羽毛", target="银灰"),  # 干员别名不混入
        ]
        with mock.patch(
            "ArknightsUID.utils.database.models.ArkAlias.list_aliases",
            new=mock.AsyncMock(return_value=rows),
        ):
            aliases = await _load_user_theme_aliases("bot", "user")
        self.assertEqual(aliases, {"树海": "black"})

    async def test_db_error_returns_empty(self) -> None:
        with mock.patch(
            "ArknightsUID.utils.database.models.ArkAlias.list_aliases",
            new=mock.AsyncMock(side_effect=RuntimeError("db down")),
        ):
            self.assertEqual(await _load_user_theme_aliases("bot", "user"), {})


if __name__ == "__main__":
    unittest.main()
