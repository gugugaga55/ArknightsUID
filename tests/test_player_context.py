"""卡片 context 组装层测试（context.py）+ 别名索引测试（charinfo.py）。"""
from __future__ import annotations

import unittest

from ArknightsUID.utils.gamedata.charinfo import (
    CURATED_ALIASES,
    build_char_index,
    match_entries,
    resolve_char,
)
from ArknightsUID.utils.player.context import (
    build_box_context,
    build_campaign_context,
    build_char_stat_context,
    build_status_context,
    equip_display_name,
    format_remaining,
    skill_icon_url,
    skin_avatar_url,
)
from ArknightsUID.utils.player.parse import parse_player_info

from test_player_parse import _envelope

VERSION = "test"


def _data():
    return parse_player_info(_envelope())


class ContextBuilderTest(unittest.TestCase):
    def test_status_context(self) -> None:
        data = _data()
        assert data is not None
        # 无恢复时间戳 → 已回满文案
        ctx = build_status_context(data, VERSION, "123***789", now=1000)
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx["name"], "刀客塔不吃香菜")
        self.assertEqual(ctx["uid"], "123***789")
        self.assertEqual(ctx["ap_max"], 135)
        self.assertEqual(ctx["chars_total"], 285)
        self.assertEqual(ctx["campaign_current"], 1200)
        self.assertEqual(ctx["reg_days"], data.register_days(now=1000))

    def test_status_context_requires_name_and_ap(self) -> None:
        envelope = _envelope()
        del envelope["data"]["status"]["ap"]
        data = parse_player_info(envelope)
        assert data is not None
        self.assertIsNone(build_status_context(data, VERSION))

    def test_status_context_merges_routine(self) -> None:
        """ark日常并入后的合并 context：理智+日常/周常同卡。"""
        data = _data()
        assert data is not None
        ctx = build_status_context(data, VERSION)
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertTrue(ctx["has_routine"])
        self.assertEqual(ctx["daily_done"], 3)
        self.assertEqual(ctx["daily_total"], 8)
        self.assertEqual(ctx["weekly_done"], 5)
        self.assertEqual(ctx["campaign_total"], 1800)

    def test_status_context_without_routine_hides_block(self) -> None:
        envelope = _envelope()
        del envelope["data"]["routine"]
        data = parse_player_info(envelope)
        assert data is not None
        ctx = build_status_context(data, VERSION)
        assert ctx is not None
        self.assertFalse(ctx["has_routine"])

    def test_campaign_context(self) -> None:
        data = _data()
        assert data is not None
        ctx = build_campaign_context(data, VERSION)
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx["reward_current"], 1200)
        self.assertEqual(ctx["reward_pct"], 67)
        self.assertEqual(ctx["stages"][0], {"name": "camp_r1", "progress": 400, "total": 400})

    def test_char_stat_context_groups_by_profession(self) -> None:
        data = _data()
        assert data is not None
        ctx = build_char_stat_context(data, VERSION)
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx["total"], 3)
        self.assertEqual(ctx["e2_total"], 2)  # 两个阿米娅 E2，艾丝黛尔 E1
        professions = {row["en"]: row for row in ctx["professions"]}
        self.assertEqual(professions["CASTER"]["count"], 1)
        self.assertEqual(professions["WARRIOR"]["count"], 2)
        self.assertEqual(professions["WARRIOR"]["e2"], 1)

    def test_char_stat_context_requires_chars(self) -> None:
        envelope = _envelope()
        envelope["data"]["chars"] = []
        data = parse_player_info(envelope)
        assert data is not None
        self.assertIsNone(build_char_stat_context(data, VERSION))

    def test_box_context_discrete_rarity_distribution(self) -> None:
        data = _data()
        assert data is not None
        ctx = build_box_context(data, VERSION)
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx["total"], 3)
        by_label = {row["label"]: row for row in ctx["rarity_rows"]}
        # rarity_display：阿米娅1=5★、阿米娅2=6★、艾丝黛尔=4★
        self.assertEqual(by_label["六星"]["count"], 1)
        self.assertEqual(by_label["五星"]["count"], 1)
        self.assertEqual(by_label["四星"]["count"], 1)
        self.assertEqual(by_label["三星及以下"]["count"], 0)
        self.assertEqual(sum(r["count"] for r in ctx["rarity_rows"]), 3)
        elite = {row["label"]: row["count"] for row in ctx["elite_rows"]}
        self.assertEqual(elite["精英化二"], 2)
        self.assertEqual(elite["精英化一"], 1)
        self.assertEqual(elite["无精英化"], 0)

    def test_hycdn_url_rules(self) -> None:
        # 基础皮肤 @N → #N（hycdn 按皮肤表键名提供头像；@N 形态实测 404）
        self.assertEqual(
            skin_avatar_url("char_002_amiya@1"),
            "https://web.hycdn.cn/arknights/game/assets/char_skin/avatar/char_002_amiya%231.png",
        )
        # 装扮皮肤 @系列#N 原样编码（@ → %40，# → %23）
        self.assertEqual(
            skin_avatar_url("char_002_amiya@epoque#4"),
            "https://web.hycdn.cn/arknights/game/assets/char_skin/avatar/char_002_amiya%40epoque%234.png",
        )
        self.assertEqual(
            skin_avatar_url("char_1001_amiya#5"),
            "https://web.hycdn.cn/arknights/game/assets/char_skin/avatar/char_1001_amiya%235.png",
        )
        self.assertEqual(skin_avatar_url(""), "")
        # 技能图标整体编码（[] → %5B %5D）
        self.assertEqual(
            skill_icon_url("skcom_magic_rage[3]"),
            "https://web.hycdn.cn/arknights/game/assets/char_skill/skcom_magic_rage%5B3%5D.png",
        )

    def test_format_remaining(self) -> None:
        self.assertEqual(format_remaining(0), "")
        self.assertEqual(format_remaining(1000, now=2000), "")  # 已回满
        self.assertEqual(format_remaining(2000, now=1000), "16m")
        self.assertEqual(format_remaining(8300, now=1000), "2h 01m")  # 7300s

    def test_equip_display_name(self) -> None:
        self.assertEqual(equip_display_name("uniequip_002_amiya"), "uniequip_002_amiya")
        self.assertEqual(equip_display_name("uniequip_003_amiya"), "uniequip_003_amiya")
        self.assertEqual(equip_display_name("weird_id"), "weird_id")
        self.assertEqual(equip_display_name(""), "模组")


class AliasIndexTest(unittest.TestCase):
    def _index(self) -> dict:
        table = {
            "char_123_silverash": {
                "name": "银灰", "appellation": "SilverAsh",
                "rarity": "TIER_6", "profession": "WARRIOR",
                "itemObtainApproach": "招募寻访",
            },
            "char_291_thorn": {
                "name": "棘刺", "appellation": "Thorns",
                "rarity": "TIER_6", "profession": "WARRIOR",
                "itemObtainApproach": "招募寻访",
            },
            "char_180_amgo": {
                "name": "史尔特尔", "appellation": "Surtr",
                "rarity": "TIER_6", "profession": "WARRIOR",
                "itemObtainApproach": "招募寻访",
            },
            "char_010_chen": {
                "name": "陈", "appellation": "Ch'en",
                "rarity": "TIER_6", "profession": "WARRIOR",
                "itemObtainApproach": "招募寻访",
            },
            "npc_001": {  # NPC → 排除
                "name": "塔露拉", "appellation": "Talulah",
                "rarity": "TIER_6", "profession": "WARRIOR",
                "isNotObtainable": True,
            },
        }
        return build_char_index(table)

    def test_build_char_index_filters_npc_and_maps_rarity(self) -> None:
        index = self._index()
        self.assertNotIn("npc_001", index)
        self.assertEqual(index["char_123_silverash"]["rarity"], 6)  # TIER_6 → 6
        self.assertEqual(index["char_123_silverash"]["appellation"], "SilverAsh")

    def test_build_char_index_keeps_alters_excludes_traps(self) -> None:
        """异格干员（isSpChar，寻访可得）进索引；装置/召唤物（TRAP/TOKEN）排除。"""
        table = {
            "char_1035_wisdel": {
                "name": "维什戴尔", "appellation": "Wis'adel",
                "rarity": "TIER_6", "profession": "SNIPER",
                "isSpChar": True, "itemObtainApproach": "招募寻访",
            },
            "trap_305_izwsdl": {
                "name": "变格维什戴尔", "rarity": "TIER_1", "profession": "TRAP",
            },
        }
        index = build_char_index(table)
        self.assertIn("char_1035_wisdel", index)
        self.assertNotIn("trap_305_izwsdl", index)

    def test_exact_chinese_name(self) -> None:
        index = self._index()
        matched = match_entries("银灰", index)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["char_id"], "char_123_silverash")

    def test_appellation_case_insensitive(self) -> None:
        index = self._index()
        for query in ("SilverAsh", "silverash", "SILVERASH"):
            matched = match_entries(query, index)
            self.assertEqual(len(matched), 1, query)
            self.assertEqual(matched[0]["name"], "银灰")

    def test_curated_alias(self) -> None:
        index = self._index()
        self.assertEqual(match_entries("42", index)[0]["name"], "史尔特尔")
        self.assertEqual(match_entries("陈sir", index)[0]["name"], "陈")
        # 别名表全部指向索引内存在的干员（防手滑配错）
        for alias, target in CURATED_ALIASES.items():
            if target in ("阿米娅", "陈", "史尔特尔"):
                continue
            self.assertTrue(
                any(v["name"] == target for v in index.values()),
                f"别名 {alias} 指向不存在的干员 {target}",
            )

    def test_unique_prefix_returns_single(self) -> None:
        index = self._index()
        matched = match_entries("棘", index)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["name"], "棘刺")

    def test_ambiguous_prefix_returns_candidates(self) -> None:
        index = self._index()
        matched = match_entries("s", index)  # SilverAsh / Surtr 两个前缀命中
        self.assertEqual(len(matched), 2)
        self.assertIsNone(resolve_char("s", index))

    def test_no_match_returns_empty(self) -> None:
        index = self._index()
        self.assertEqual(match_entries("不存在的", index), [])
        self.assertEqual(match_entries("", index), [])
        self.assertIsNone(resolve_char("不存在的", index))

    def test_resolve_char_unique(self) -> None:
        index = self._index()
        self.assertEqual(resolve_char("银灰", index), ("char_123_silverash", "银灰"))
        self.assertEqual(resolve_char("42", index), ("char_180_amgo", "史尔特尔"))


if __name__ == "__main__":
    unittest.main()
