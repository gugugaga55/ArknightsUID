"""皮肤收集 + 公招槽位解析测试（ark皮肤 / 基建公招行 / 公招推送数据源）。"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.gamedata.skins import (
    build_skin_index,
    summarize_collection,
)
from ArknightsUID.utils.player.parse import (
    RecruitSlot,
    parse_player_info,
    recruit_done_count,
    recruit_next_remaining,
)


def _player_info_with(recruit=None, skins=None) -> dict:
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "status": {
                "name": "测试博士",
                "uid": "123456789",
                "level": 120,
                "ap": {"max": 135, "completeRecoveryTime": 0},
            },
            "chars": [
                {
                    "charId": "char_002_amiya",
                    "skinId": "char_002_amiya@amiya#1",
                    "level": 50,
                }
            ],
            "recruit": recruit if recruit is not None else [],
            "skins": skins if skins is not None else [],
        },
    }


class RecruitParseTest(unittest.TestCase):
    def test_slots_parsed(self) -> None:
        data = parse_player_info(
            _player_info_with(
                recruit=[
                    {"startTs": -1, "finishTs": -1, "state": 1},
                    {"startTs": 100, "finishTs": 200, "state": 2},
                ]
            )
        )
        assert data is not None
        self.assertEqual(len(data.recruit_slots), 2)
        self.assertFalse(data.recruit_slots[0].recruiting)
        self.assertTrue(data.recruit_slots[1].recruiting)

    def test_done_and_remaining(self) -> None:
        now = float(int(time.time()))
        done = RecruitSlot(start_ts=1, finish_ts=int(now) - 50)
        pending = RecruitSlot(start_ts=1, finish_ts=int(now) + 3600)
        idle = RecruitSlot(start_ts=-1, finish_ts=-1)
        self.assertEqual(recruit_done_count([done, pending, idle], now=now), 1)
        self.assertEqual(recruit_next_remaining([done, pending, idle], now=now), 3600)
        self.assertIsNone(recruit_next_remaining([done, idle], now=now))

    def test_remaining_clamps_at_zero(self) -> None:
        slot = RecruitSlot(start_ts=1, finish_ts=100)
        self.assertEqual(slot.remaining_secs(now=1000), 0)
        self.assertEqual(slot.remaining_secs(now=50), 50)
        self.assertEqual(slot.remaining_secs(), -1 if not slot.recruiting else 0)

    def test_idle_slot_remaining_unknown(self) -> None:
        self.assertEqual(RecruitSlot().remaining_secs(), -1)


class SkinIndexTest(unittest.TestCase):
    def _skin_table(self) -> dict:
        return {
            "charSkins": {
                # 时装（@）且干员可获得 → 进目录
                "char_002_amiya@winter#1": {
                    "skinId": "char_002_amiya@winter#1",
                    "charId": "char_002_amiya",
                    "displaySkin": {
                        "skinName": "报童",
                        "skinGroupName": "忒斯特收藏/I",
                    },
                },
                "char_123_fang@epoque#1": {
                    "skinId": "char_123_fang@epoque#1",
                    "charId": "char_123_fang",
                    "displaySkin": {"skinName": "夏栎碎片", "skinGroupName": "时代"},
                },
                # 基础形态（无 @）不进目录
                "char_002_amiya#2": {
                    "skinId": "char_002_amiya#2",
                    "charId": "char_002_amiya",
                    "displaySkin": {"skinName": "精英二", "skinGroupName": ""},
                },
                # 干员不在索引（不可获得）不进目录
                "char_999_npc@sale#1": {
                    "skinId": "char_999_npc@sale#1",
                    "charId": "char_999_npc",
                    "displaySkin": {"skinName": "内部", "skinGroupName": ""},
                },
            }
        }

    def _char_index(self) -> dict:
        return {
            "char_002_amiya": {"name": "阿米娅", "rarity": 6},
            "char_123_fang": {"name": "芬", "rarity": 3},
        }

    def test_index_filters_to_cosmetic_and_obtainable(self) -> None:
        index = build_skin_index(self._skin_table(), self._char_index())
        self.assertEqual(
            set(index),
            {"char_002_amiya@winter#1", "char_123_fang@epoque#1"},
        )
        self.assertEqual(index["char_002_amiya@winter#1"]["name"], "报童")
        self.assertEqual(index["char_002_amiya@winter#1"]["group"], "忒斯特收藏/I")

    def test_summarize_collection(self) -> None:
        catalog = build_skin_index(self._skin_table(), self._char_index())
        summary = summarize_collection(
            catalog, ["char_002_amiya@winter#1"], self._names()
        )
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["owned"], 1)
        self.assertEqual(summary["missing_count"], 1)
        self.assertEqual(summary["pct"], 50.0)
        self.assertEqual(summary["missing"][0]["char"], "芬")

    def test_summarize_extra_owned_counted(self) -> None:
        catalog = build_skin_index(self._skin_table(), self._char_index())
        summary = summarize_collection(
            catalog,
            ["char_002_amiya@winter#1", "char_999_npc@sale#1"],
            self._names(),
        )
        self.assertEqual(summary["owned"], 1)
        self.assertEqual(summary["extra_owned"], 1)

    def test_player_owned_skin_ids(self) -> None:
        data = parse_player_info(
            _player_info_with(
                skins=[{"id": "char_002_amiya@winter#1", "ts": 100}]
            )
        )
        assert data is not None
        self.assertEqual(data.owned_skin_ids, ["char_002_amiya@winter#1"])

    def _names(self) -> dict:
        return {
            "char_002_amiya": "阿米娅",
            "char_123_fang": "芬",
        }


if __name__ == "__main__":
    unittest.main()
