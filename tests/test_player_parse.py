"""player_info 富数据解析层测试（parse.py）。

字段结构依据：ProbiusOfficial/Skland_API dump.json（status/chars 一手证据）
+ gxy12345 profile.js（routine/campaign 路径）。
"""
from __future__ import annotations

import unittest

from ArknightsUID.utils.player.parse import (
    PlayerData,
    compute_ap_current,
    effective_ap,
    parse_player_info,
)


def _envelope() -> dict:
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "status": {
                "name": "刀客塔不吃香菜",
                "uid": "123456789",
                "level": 120,
                "registerTs": 1600000000,
                "mainStageProgress": "14-21",
                "charCnt": 285,
                "skinCnt": 40,
                "ap": {"current": 30, "max": 135, "completeRecoveryTime": 0},
            },
            "chars": [
                {
                    "charId": "char_002_amiya",
                    "skinId": "char_002_amiya@1",
                    "level": 90,
                    "evolvePhase": 2,
                    "potentialRank": 6,
                    "mainSkillLvl": 7,
                    "favorPercent": 200,
                    "defaultEquipId": "uniequip_002_amiya",
                    "skills": [
                        {"id": "skcom_magic_rage[3]", "specializeLevel": 3},
                        {"id": "skchr_amiya_2", "specializeLevel": 0},
                    ],
                    "equip": [{"id": "uniequip_002_amiya", "level": 3}],
                },
                {
                    "charId": "char_1001_amiya2",
                    "skinId": "char_1001_amiya2@1",
                    "level": 60,
                    "evolvePhase": 2,
                    "potentialRank": 0,
                    "mainSkillLvl": 7,
                    "favorPercent": 100,
                    "skills": [{"id": "skchr_amiya2_1", "specializeLevel": 0}],
                    "equip": [],
                },
                {
                    "charId": "char_144_red",
                    "skinId": "char_144_red@1",
                    "level": 70,
                    "evolvePhase": 1,
                    "potentialRank": 3,
                    "mainSkillLvl": 7,
                    "favorPercent": 150,
                    "skills": [{"id": "skchr_red_1", "specializeLevel": 2}],
                    "equip": [],
                },
            ],
            "charInfoMap": {
                "char_002_amiya": {
                    "name": "阿米娅", "profession": "CASTER",
                    "subProf": "corecaster", "rarity": 4, "modules": ["X"],
                },
                "char_1001_amiya2": {
                    "name": "阿米娅", "profession": "WARRIOR",
                    "subProf": "lord", "rarity": 5, "modules": [],
                },
                "char_144_red": {
                    "name": "艾丝黛尔", "profession": "WARRIOR",
                    "subProf": "centurion", "rarity": 3, "modules": [],
                },
            },
            "routine": {
                "daily": {"current": 3, "total": 8},
                "weekly": {"current": 5, "total": 12},
            },
            "campaign": {
                "reward": {"current": 1200, "total": 1800},
                "records": {"camp_r1": 400, "camp_r3": 355},
            },
            "rogue": {"records": []},
            "skins": [{"skinId": "char_002_amiya@1"}],
        },
    }


class ParsePlayerInfoTest(unittest.TestCase):
    def test_parse_full_envelope(self) -> None:
        data = parse_player_info(_envelope())
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.name, "刀客塔不吃香菜")
        self.assertEqual(data.uid, "123456789")
        self.assertEqual(data.level, 120)
        self.assertEqual(data.char_cnt, 285)
        self.assertEqual(data.chars_total, 285)  # charCnt 优先于 len(chars)
        self.assertEqual(data.daily_done, 3)
        self.assertEqual(data.daily_total, 8)
        self.assertEqual(data.weekly_done, 5)
        self.assertEqual(data.weekly_total, 12)
        self.assertEqual(data.campaign_current, 1200)
        self.assertEqual(data.campaign_total, 1800)
        self.assertEqual(data.campaign_records, {"camp_r1": 400, "camp_r3": 355})
        self.assertEqual(len(data.chars), 3)

    def test_parse_char_fields_follow_real_dump(self) -> None:
        data = parse_player_info(_envelope())
        assert data is not None
        amiya = data.chars[0]
        # 真实 dump 字段：potentialRank / favorPercent（非早期文档记录的写法）
        self.assertEqual(amiya.char_id, "char_002_amiya")
        self.assertEqual(amiya.potential_rank, 6)
        self.assertEqual(amiya.favor_percent, 200)
        self.assertEqual(amiya.skin_id, "char_002_amiya@1")
        self.assertEqual(amiya.skills[0].specialize_level, 3)
        self.assertEqual(amiya.equips[0].equip_id, "uniequip_002_amiya")
        # charInfoMap 元数据
        self.assertEqual(amiya.name, "阿米娅")
        self.assertEqual(amiya.profession, "CASTER")
        self.assertEqual(amiya.rarity, 4)

    def test_amiya_display_names_get_profession_suffix(self) -> None:
        data = parse_player_info(_envelope())
        assert data is not None
        self.assertEqual(data.chars[0].display_name, "阿米娅(术师)")
        self.assertEqual(data.chars[1].display_name, "阿米娅(近卫)")
        # 非阿米娅不追加后缀
        self.assertEqual(data.chars[2].display_name, "艾丝黛尔")

    def test_amiya_profession_fallback_without_charInfoMap(self) -> None:
        envelope = _envelope()
        del envelope["data"]["charInfoMap"]
        data = parse_player_info(envelope)
        assert data is not None
        self.assertEqual(data.chars[0].profession_name, "术师")
        self.assertEqual(data.chars[1].profession_name, "近卫")

    def test_rarity_display_is_one_based(self) -> None:
        data = parse_player_info(_envelope())
        assert data is not None
        # charInfoMap rarity 4（0-5 编号）→ 展示 5 星
        self.assertEqual(data.chars[0].rarity_display, 5)
        self.assertEqual(data.chars[1].rarity_display, 6)

    def test_max_level_by_rarity_and_evolve_phase(self) -> None:
        data = parse_player_info(_envelope())
        assert data is not None
        # amiya2 6星E2→90；艾丝黛尔 4星E1→60；amiya1 5星E2→80
        self.assertEqual(data.chars[1].max_level(), 90)
        self.assertEqual(data.chars[2].max_level(), 60)
        self.assertEqual(data.chars[0].max_level(), 80)

    def test_ap_computed_from_recovery_formula(self) -> None:
        envelope = _envelope()
        envelope["data"]["status"]["ap"] = {
            "current": 0,
            "max": 135,
            "completeRecoveryTime": 100000,
        }
        data = parse_player_info(envelope)
        assert data is not None
        # 回满时间已过 → 满
        self.assertEqual(effective_ap(data, now=101_000), 135)
        # 距回满 720 秒 → 差 2 点
        envelope["data"]["status"]["ap"]["completeRecoveryTime"] = 100720
        data = parse_player_info(envelope)
        assert data is not None
        self.assertEqual(effective_ap(data, now=100_000), 133)

    def test_compute_ap_current_edges(self) -> None:
        self.assertEqual(compute_ap_current(135, 0, now=1000), 135)  # 无恢复时间戳=满
        self.assertEqual(compute_ap_current(135, 500, now=1000), 135)  # 已过回满时间
        self.assertEqual(compute_ap_current(135, 1360, now=1000), 134)  # 差 1 点（360s）

    def test_effective_ap_falls_back_to_dump_current(self) -> None:
        envelope = _envelope()
        envelope["data"]["status"]["ap"] = {"current": 42, "max": 135}
        data = parse_player_info(envelope)
        assert data is not None
        self.assertEqual(effective_ap(data, now=1000), 42)

    def test_register_days(self) -> None:
        data = parse_player_info(_envelope())
        assert data is not None
        # registerTs=1600000000，now=1700000000 → 100000000s = 1157.4 天 + 1 = 1158
        self.assertEqual(data.register_days(now=1_700_000_000), 1158)
        self.assertEqual(data.register_days(now=1_600_000_000), 1)

    def test_parse_rejects_invalid_input(self) -> None:
        self.assertIsNone(parse_player_info(None))
        self.assertIsNone(parse_player_info("x"))
        self.assertIsNone(parse_player_info({"code": 10002, "message": "失效"}))
        self.assertIsNone(parse_player_info({"code": 0, "data": {}}))

    def test_parse_accepts_bare_data_dict(self) -> None:
        envelope = _envelope()
        data = parse_player_info(envelope["data"])
        self.assertIsNotNone(data)

    def test_parse_tolerates_missing_sections(self) -> None:
        envelope = _envelope()
        del envelope["data"]["routine"]
        del envelope["data"]["campaign"]
        del envelope["data"]["chars"]
        data = parse_player_info(envelope)
        assert data is not None
        self.assertEqual(data.daily_total, 0)
        self.assertEqual(data.campaign_total, 0)
        self.assertEqual(data.chars, [])
        self.assertEqual(data.chars_total, 285)  # charCnt 仍在


if __name__ == "__main__":
    unittest.main()


class ApZeroSemanticsTest(unittest.TestCase):
    """审计 #29：理智 0 与字段缺失必须区分，0 不再被当满理智。"""

    @staticmethod
    def _data(ap: dict) -> PlayerData | None:
        return parse_player_info({"data": {"status": {"name": "D", "uid": "1",
                                                      "ap": ap}}})

    def test_current_zero_is_real_zero(self) -> None:
        data = self._data({"current": 0, "max": 135})
        self.assertIsNotNone(data)
        self.assertEqual(effective_ap(data), 0)  # 旧逻辑误报 135

    def test_current_missing_falls_back_to_max(self) -> None:
        data = self._data({"max": 135})  # 无 current 字段
        self.assertIsNotNone(data)
        self.assertIsNone(data.ap_current)
        self.assertEqual(effective_ap(data), 135)  # 缺失保持回退语义

    def test_positive_current_kept(self) -> None:
        data = self._data({"current": 80, "max": 135})
        self.assertEqual(effective_ap(data), 80)
