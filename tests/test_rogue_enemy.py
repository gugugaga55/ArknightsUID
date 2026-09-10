"""肉鸽敌人 buff 面板测试：乘数语义 / 主题判定 / 面板构建 / 卡片契约。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.arknightsuid_rogue import (
    _build_rogue_enemy_context,
    _build_rogue_stage_context,
    _parse_rogue_query,
)
from ArknightsUID.utils.gamedata.rogue_enemy import (
    build_diff_panels,
    build_panel_at,
    detect_theme,
    form_context,
    form_mechanisms,
    match_rogue_enemies,
    stage_panel_rows,
    stages_for_enemy,
    status_labels,
)


def _db() -> dict:
    return {
        "enemy_1_boss": {
            "key": "enemy_1_boss", "name_zh": "歌利亚",
            "type": ["BOSS", "melee", "sarkaz"],
            "stats": [{
                "hp": 80000, "atk": 2350, "def": 1000, "res": 25.0,
                "weight": 5, "aspd": 4.0, "ms": 0.4, "range": 0.0,
                "lifepoint": 2, "epResistance": 0.0,
            }],
            "forms": [{
                "title": None,
                "normal_attack": {"atk_type": ["melee", "phys"], "hits": 1},
                "status_immune": ["silence", "stun"],
            }],
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
        # 难度10：dmg_res 非面板字段 → 人读 note，数值不变
        self.assertEqual(by_grade[10]["notes"], ["受到伤害×0.1"])
        # 难度14 只点名 enemy_2_normal，本敌不受影响 → 与 10 合并
        labels = [r["label"] for r in rows]
        self.assertTrue(any("10~14" in label for label in labels), labels)

    def test_targeted_enemy_gets_own_mods(self) -> None:
        entry = _db()["enemy_2_normal"]
        rows = build_diff_panels(entry, _sarkaz_diffs())
        by_grade = {r["grade"]: r for r in rows}
        # 难度14 显式点名：HP ×1.5（形态0 基础 1050）
        self.assertEqual(by_grade[14]["stats"]["hp"], 1575)


class FormAndStatusTest(unittest.TestCase):
    """形态行次级属性段 + 异常免疫并集（status_immune → 中文）。"""

    def test_form_extras_and_hits(self) -> None:
        rows = form_context(_db()["enemy_1_boss"])
        self.assertEqual(len(rows), 1)
        # 移速/间隔/进点（lifepoint=2）进 extras；range=0（近战）不展示
        self.assertEqual(rows[0]["extras"], ["移速 0.4", "间隔 4s", "进点-2"])

    def test_form_extras_empty_for_defaults(self) -> None:
        rows = form_context(_db()["enemy_2_normal"])
        # 默认值（lifepoint=1 无次级数据）→ extras 为空，模板不追加段
        self.assertEqual(rows[0]["extras"], [])

    def test_status_labels_union_and_order(self) -> None:
        # stun/silence 按固定顺序输出中文；多形态取并集
        self.assertEqual(
            status_labels(_db()["enemy_1_boss"]), ["眩晕", "沉默"]
        )
        self.assertEqual(status_labels(_db()["enemy_2_normal"]), [])

    def test_hits_suffix(self) -> None:
        entry = {
            "key": "e", "name_zh": "测试", "type": [],
            "stats": [{"hp": 1, "atk": 1, "def": 0, "res": 0}],
            "forms": [
                {"title": None, "normal_attack": {"atk_type": ["melee", "phys"], "hits": 4}},
            ],
        }
        self.assertEqual(form_context(entry)[0]["attack"], "近战/物理×4段")


class ModNoteTest(unittest.TestCase):
    """难度效果 note 人话化（原样键名如 aspd 15 不可读）。"""

    def test_note_formats(self) -> None:
        from ArknightsUID.utils.gamedata.rogue_enemy import _mod_note

        self.assertEqual(
            _mod_note({"key": "dmg_res", "value": 0.05, "mode": "mul"}),
            "受到伤害×0.05",
        )
        self.assertEqual(
            _mod_note({"key": "aspd", "value": 15, "mode": "add"}), "攻速+15"
        )
        self.assertEqual(
            _mod_note({"key": "ms", "value": 1.15, "mode": "mul"}), "移速×1.15"
        )
        self.assertEqual(
            _mod_note({"key": "res", "value": 10, "mode": "add"}), "法抗+10"
        )
        self.assertEqual(
            _mod_note({"key": "level_hidden_group_enable", "value": 0.0, "valueStr": "reforge"}),
            "开启隐藏强化（reforge）",
        )


class ParseQueryTest(unittest.TestCase):
    """`名字 [期名] [N难度] [紧急]` 条件解析。"""

    def test_name_only(self) -> None:
        self.assertEqual(_parse_rogue_query("歌利亚"), ("歌利亚", None, None, False))

    def test_theme_and_difficulty(self) -> None:
        self.assertEqual(
            _parse_rogue_query("歌利亚 萨卡兹 N12"), ("歌利亚", "sarkaz", 12, False)
        )
        self.assertEqual(
            _parse_rogue_query("源石虫 水月 难度9"), ("源石虫", "mizuki", 9, False)
        )
        # 长期名（含"岁"这类短别名字串）优先整名匹配
        self.assertEqual(
            _parse_rogue_query("歌利亚 岁的界园志异 N5"), ("歌利亚", "sui", 5, False)
        )

    def test_elite_keyword(self) -> None:
        self.assertEqual(
            _parse_rogue_query("拆东补西 N15 紧急"),
            ("拆东补西", None, 15, True),
        )
        self.assertEqual(
            _parse_rogue_query("歌利亚 兀兀构筑 N15 紧急"),
            ("歌利亚 兀兀构筑", None, 15, True),
        )

    def test_difficulty_without_space_and_theme_in_name(self) -> None:
        self.assertEqual(
            _parse_rogue_query("歌利亚N12"), ("歌利亚", None, 12, False)
        )
        # 敌人名含期名子串（萨卡兹魔剑士）：整词匹配不拆名字、不误判期
        self.assertEqual(
            _parse_rogue_query("萨卡兹魔剑士 N5"), ("萨卡兹魔剑士", None, 5, False)
        )
        self.assertEqual(
            _parse_rogue_query("魔剑士 萨卡兹 N5"), ("魔剑士", "sarkaz", 5, False)
        )

    def test_difficulty_not_matched_inside_latin_token(self) -> None:
        # 拉丁词中段的 n7 不当作难度（旧正则会把「sin7」拆成「si」+难度7）
        self.assertEqual(
            _parse_rogue_query("sin7"), ("sin7", None, None, False)
        )
        # N123 整体消化（旧正则只截两位残留「3」）
        self.assertEqual(
            _parse_rogue_query("歌利亚 N123"), ("歌利亚", None, 123, False)
        )


class FocusPanelTest(unittest.TestCase):
    """指定期+难度单面板：累积折算 + 生效 buff 全量列出。"""

    def test_panel_at_grade(self) -> None:
        entry = _db()["enemy_1_boss"]
        row = build_panel_at(entry, _sarkaz_diffs(), 10)
        # ≤10 档累积：难度4 HP×1.2 + 难度7 攻×1.1
        self.assertEqual(row["label"], "难度10")
        self.assertEqual(row["stats"]["hp"], 96000)
        self.assertEqual(row["stats"]["atk"], 2585)
        # 面板 key 折算条目也进 buffs（生命×1.2）+ 非面板效果（受到伤害×0.1）
        self.assertIn("生命×1.2", row["buffs"])
        self.assertIn("攻击×1.1", row["buffs"])
        self.assertIn("受到伤害×0.1", row["buffs"])

    def test_noob_grade(self) -> None:
        entry = _db()["enemy_1_boss"]
        row = build_panel_at(entry, _sarkaz_diffs(), 0)
        self.assertEqual(row["stats"]["hp"], 64000)
        self.assertIn("新手档生命×0.8", row["buffs"])

    def test_floor_buff_both_shapes(self) -> None:
        """每层增益两种形态：数字（萨米/萨卡兹）与 {atk,hp} 乘数（水月/岁）。"""
        entry = _db()["enemy_2_normal"]
        scalar = [
            {"difficulty": 9, "effects": [], "floorBuff": 0.02},
        ]
        mult = [
            {"difficulty": 9, "effects": [], "floorBuff": {"atk": 1.01, "hp": 1.01}},
        ]
        self.assertIn(
            "每层增益+2%（未计入，按层复合）",
            build_panel_at(entry, scalar, 9)["buffs"],
        )
        self.assertIn(
            "每层生命/攻击×1.01（未计入，按层复合）",
            build_panel_at(entry, mult, 9)["buffs"],
        )


class MechanismTest(unittest.TestCase):
    """traits/special → 中文机制（enemy_skills 词典 + 占位符填充）。"""

    SKILLS = {
        "taunt": ["嘲讽等级+<v.value>"],
        "invincible": ["获得<v.value>s无敌效果"],
        "patrt_0": ["攻击力+50%，防御力+200%，普通攻击为4连击"],
        "sarkaz_standby": ["待命<v.standby>秒", "初始处于待命状态，不进行攻击"],
    }

    def test_placeholder_filled_and_unfilled_dropped(self) -> None:
        entry = {
            "key": "e", "name_zh": "测试",
            "stats": [{
                "traits": [],
                "special": [[
                    {"key": "taunt", "value": 2},
                    {"key": "invincible", "value": 15},
                    {"key": "sarkaz_standby"},
                    {"key": "unknown_key"},
                ]],
            }],
        }
        lines = form_mechanisms(entry, self.SKILLS)[0]
        # 数值占位填充；无法填充的行丢弃；词典未收的键跳过
        self.assertEqual(
            lines,
            [
                "嘲讽等级+2",
                "获得15s无敌效果",
                "初始处于待命状态，不进行攻击",
            ],
        )

    def test_form_takes_diagonal_special_group(self) -> None:
        """多形态：形态 i 取 special[i]（爱国者 行军/毁灭 两组实测结构）。"""
        entry = {
            "key": "e", "name_zh": "测试",
            "stats": [
                {"traits": [], "special": [
                    [{"key": "patrt_0"}], [{"key": "invincible", "value": 15}],
                ]},
                {"traits": [], "special": [
                    [{"key": "patrt_0"}], [{"key": "invincible", "value": 15}],
                ]},
            ],
        }
        forms = form_mechanisms(entry, self.SKILLS)
        self.assertEqual(forms[0], ["攻击力+50%，防御力+200%，普通攻击为4连击"])
        self.assertEqual(forms[1], ["获得15s无敌效果"])


class StageIndexTest(unittest.TestCase):
    """出没关卡索引：分组/注记（形态N、紧急+）/每期截断。"""

    INDEX = {
        "enemy_1_boss": [
            {"theme": "sarkaz", "code": "ISW-NO", "name": "拆东补西",
             "floors": "1-2层", "level": 0, "elite": False},
            {"theme": "sarkaz", "code": "ISW-DF", "name": "大君之赐",
             "floors": "3层", "level": 1, "elite": True},
            {"theme": "phantom", "code": "ISW-NO", "name": "与虫为伴",
             "floors": "1层", "level": 0, "elite": False},
        ],
        "enemy_2_normal": [
            {"theme": theme, "code": "ISW-NO", "name": f"关卡{i}",
             "floors": "1层", "level": 0, "elite": False}
            for i, theme in enumerate(["mizuki"] * 8)
        ],
    }

    def test_grouping_and_markers(self) -> None:
        groups = stages_for_enemy(self.INDEX, "enemy_1_boss")
        # 按 total 降序；期名映射
        self.assertEqual([g["theme"] for g in groups], ["sarkaz", "phantom"])
        self.assertEqual(groups[0]["theme_name"], "萨卡兹的无终奇语")
        sarkaz_items = groups[0]["stages"]
        self.assertIn("拆东补西·ISW-NO·1-2层", sarkaz_items)
        self.assertIn("大君之赐·ISW-DF·3层·形态2·紧急+", sarkaz_items)

    def test_per_theme_cap_and_total(self) -> None:
        groups = stages_for_enemy(self.INDEX, "enemy_2_normal", per_theme_limit=6)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["total"], 8)
        self.assertEqual(len(groups[0]["stages"]), 6)

    def test_unknown_enemy(self) -> None:
        self.assertEqual(stages_for_enemy(self.INDEX, "enemy_x"), [])


class StageIndexBuildTest(unittest.IsolatedAsyncioTestCase):
    """索引构建：meta/关卡文件 mock，空壳期静默跳过。"""

    async def test_build_skips_empty_and_maps_ids(self) -> None:
        from unittest import mock

        import ArknightsUID.utils.gamedata.rogue_enemy as re_mod

        async def fake_meta(theme_dir: str) -> dict:
            if theme_dir == "sarkaz":
                return {"level_rogue4_2-2": {"code": "ISW-NO", "name": "拆东补西"}}
            return {}

        async def fake_file(level_id: str) -> dict:
            if level_id == "level_rogue4_2-2":
                return {
                    "enemies": [
                        {"id": "enemy_2063_skzlch", "level": 0,
                         "min_count": 4, "max_count": 4,
                         "elite_min_count": 6, "elite_max_count": 6},
                        {"id": "enemy_2091_skzgds", "level": 1,
                         "min_count": 0, "max_count": 0,
                         "elite_min_count": 1, "elite_max_count": 1},
                    ],
                    "floors": [2],
                }
            return {}

        original_cache = re_mod._stage_index_cache
        re_mod._stage_index_cache = None
        try:
            with (
                mock.patch.object(re_mod, "_load_stage_meta", new=fake_meta),
                mock.patch.object(re_mod, "_load_stage_file", new=fake_file),
            ):
                index = await re_mod.build_stage_index()
        finally:
            re_mod._stage_index_cache = original_cache

        self.assertIn("enemy_2063_skzlch", index)
        row = index["enemy_2063_skzlch"][0]
        self.assertEqual(row["theme"], "sarkaz")
        # max_count 4 == elite_max_count 6？→ elite_extra = 6 > 4 = True
        self.assertTrue(row["elite"])
        self.assertEqual(row["floors"], "2层")
        # level=1 → 形态2 注记由 stages_for_enemy 生成
        groups = re_mod.stages_for_enemy(index, "enemy_2091_skzgds")
        self.assertIn("形态2", groups[0]["stages"][0])


class CachePolicyTest(unittest.IsolatedAsyncioTestCase):
    """缓存策略：TTL 过期后网络失败以过期缓存兜底；无缓存才写负标记。"""

    async def test_stale_fallback_over_network_failure(self) -> None:
        from unittest import mock

        import ArknightsUID.utils.gamedata.rogue_enemy as re_mod

        async def fake_cache_read(path):
            # 过期缓存（时间戳 0，早于 TTL）
            return ([{"stale": True}], 0.0)

        async def fail_download(url, timeout=0.0):
            return None

        with (
            mock.patch.object(re_mod, "cache_read", new=fake_cache_read),
            mock.patch.object(re_mod, "download_bytes", new=fail_download),
        ):
            data = await re_mod._fetch_url_json_cached(
                "https://example.com/x.json",
                re_mod.Path("/tmp/ark_test_nonexistent/x.json"),
            )
        self.assertEqual(data, [{"stale": True}])

    async def test_miss_marker_blocks_retry_without_cache(self) -> None:
        from unittest import mock

        import ArknightsUID.utils.gamedata.rogue_enemy as re_mod

        async def fake_cache_read(path):
            return (None, 0.0)

        calls = []

        async def fake_download(url, timeout=0.0, **kw):
            calls.append(url)
            return 404, None

        async def fake_cond(url, *, timeout=0.0, **kw):
            calls.append(url)
            return 404, None, {}

        target = re_mod.Path("/tmp/ark_test_miss.json")
        miss = target.with_suffix(target.suffix + ".miss")
        miss.unlink(missing_ok=True)

        with (
            mock.patch.object(re_mod, "cache_read", new=fake_cache_read),
            mock.patch.object(re_mod, "download_bytes_status", new=fake_download),
            mock.patch.object(re_mod, "download_bytes_conditional", new=fake_cond),
        ):
            first = await re_mod._fetch_url_json_cached(
                "https://example.com/y.json", target
            )
            self.assertIsNone(first)
            self.assertEqual(len(calls), 1)
            # 404 → 写 7 天负标记；新鲜标记期内不再发请求
            self.assertTrue(miss.exists())
            self.assertEqual(miss.read_text(encoding="utf-8"), "404")
            second = await re_mod._fetch_url_json_cached(
                "https://example.com/y.json", target
            )
            self.assertIsNone(second)
            self.assertEqual(len(calls), 1)
        miss.unlink(missing_ok=True)

    async def test_net_failure_writes_short_lived_marker(self) -> None:
        """网络故障（status=0）也写负标记防抖动，但标记按 1h TTL 自愈。"""
        import os
        import time as time_mod
        from unittest import mock

        import ArknightsUID.utils.gamedata.rogue_enemy as re_mod

        async def fake_cache_read(path):
            return (None, 0.0)

        async def fake_download(url, timeout=0.0, **kw):
            return 0, None

        async def fake_cond(url, *, timeout=0.0, **kw):
            return 0, None, {}

        target = re_mod.Path("/tmp/ark_test_net_miss.json")
        miss = target.with_suffix(target.suffix + ".miss")
        miss.unlink(missing_ok=True)

        with (
            mock.patch.object(re_mod, "cache_read", new=fake_cache_read),
            mock.patch.object(re_mod, "download_bytes_status", new=fake_download),
            mock.patch.object(re_mod, "download_bytes_conditional", new=fake_cond),
        ):
            first = await re_mod._fetch_url_json_cached(
                "https://example.com/y.json", target
            )
            self.assertIsNone(first)
            self.assertEqual(miss.read_text(encoding="utf-8"), "net")
            # 标记新鲜期内（1h）不再请求
            calls = []

            async def fake_download_2(url, timeout=0.0, **kw):
                calls.append(url)
                return 200, b"[]", {}

            with mock.patch.object(
                re_mod, "download_bytes_conditional", new=fake_download_2
            ):
                blocked = await re_mod._fetch_url_json_cached(
                    "https://example.com/y.json", target
                )
            self.assertIsNone(blocked)
            self.assertEqual(calls, [])
            # 标记过期后恢复请求并成功缓存
            old = time_mod.time() - 3700
            os.utime(miss, (old, old))
            with mock.patch.object(
                re_mod, "download_bytes_conditional", new=fake_download_2
            ):
                recovered = await re_mod._fetch_url_json_cached(
                    "https://example.com/y.json", target
                )
            self.assertEqual(recovered, [])
            self.assertEqual(len(calls), 1)
        miss.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


class StagePanelRowTest(unittest.TestCase):
    """关卡出怪行：难度/紧急折算、形态块选择、数量注记、库外跳过。"""

    STAGE = {
        "enemies": [
            {"id": "enemy_1_boss", "level": 0, "min_count": 1, "max_count": 1,
             "elite_min_count": 1, "elite_max_count": 2},
            {"id": "enemy_2_normal", "level": 1, "min_count": 4, "max_count": 4,
             "elite_min_count": 4, "elite_max_count": 4},
            {"id": "trap_999_unknown", "level": 0, "min_count": 2, "max_count": 2,
             "elite_min_count": 2, "elite_max_count": 2},
        ],
        "elite_mods": [
            {"targets": ["ALL"], "mods": [
                {"key": "atk", "value": 1.2, "mode": "mul"},
                {"key": "hp", "value": 1.2, "mode": "mul"},
            ]},
        ],
    }

    def _db(self) -> dict:
        # enemy_2_normal 双块：level=1 → 用第二块（hp 1550）
        db = _db()
        db["enemy_2_normal"]["stats"].append(
            {"hp": 1550, "atk": 240, "def": 0, "res": 0, "weight": 1}
        )
        return db

    def test_block_selection_and_counts(self) -> None:
        rows = stage_panel_rows(self.STAGE, self._db(), [], None, elite=False)
        # trap 不在敌人库 → 跳过
        self.assertEqual([r["name"] for r in rows], ["歌利亚", "源石虫"])
        # level=1 → 第二块 hp=1550；count 固定 4；无难度 → 基础面板
        self.assertEqual(rows[1]["stats"]["hp"], 1550)
        self.assertEqual(rows[1]["count"], "4")
        self.assertFalse(rows[0]["has_diff"])
        # 紧急数量差 → 紧急+1 注记
        self.assertEqual(rows[0]["elite_note"], "紧急+1")
        self.assertEqual(rows[1]["elite_note"], "")

    def test_difficulty_and_elite_folding(self) -> None:
        grades = _sarkaz_diffs()
        rows = stage_panel_rows(
            self.STAGE, self._db(), grades, 4, elite=True
        )
        boss = rows[0]
        # 难度4 BOSS HP×1.2 ×紧急 HP×1.2：80000×1.44
        self.assertEqual(boss["stats"]["hp"], 115200)
        # 紧急 攻×1.2：2350×1.2
        self.assertEqual(boss["stats"]["atk"], 2820)

    def test_wave_spawn_count(self) -> None:
        stage = {"enemies": [
            {"id": "enemy_1_boss", "level": 0, "min_count": 0, "max_count": 0,
             "elite_min_count": 0, "elite_max_count": 0},
        ]}
        rows = stage_panel_rows(stage, _db(), [], None)
        self.assertEqual(rows[0]["count"], "波次")


class PanelAtStageTest(unittest.TestCase):
    """build_panel_at 关卡限定：块选择 + 紧急来源标注 + 无难度基础面板。"""

    def test_base_block_and_elite_annotation(self) -> None:
        db = _db()
        db["enemy_2_normal"]["stats"].append(
            {"hp": 1550, "atk": 240, "def": 0, "res": 0}
        )
        entry = db["enemy_2_normal"]
        base_block = entry["stats"][1]
        focus = build_panel_at(
            entry, _sarkaz_diffs(), 4,
            base_block=base_block,
            extra_effects=[{"targets": ["ALL"], "mods": [
                {"key": "atk", "value": 1.2, "mode": "mul"},
            ]}],
        )
        # 块2 基础 hp 1550，难度14 点名 ×1.5 不含难度4 → 无 hp mod
        self.assertEqual(focus["stats"]["hp"], 1550)
        # 紧急来源前缀 + label 后缀
        self.assertTrue(any(b.startswith("紧急：") for b in focus["buffs"]))
        self.assertIn("·紧急", focus["label"])

    def test_difficulty_none_gives_base(self) -> None:
        focus = build_panel_at(_db()["enemy_1_boss"], _sarkaz_diffs(), None)
        self.assertEqual(focus["label"], "基础")
        self.assertEqual(focus["stats"]["hp"], 80000)
        self.assertEqual(focus["buffs"], [])


class SkillCardTest(unittest.IsolatedAsyncioTestCase):
    """第二张图：PRTS 敌人详情卡（技能完整数值）的取数与降级。"""

    async def test_renders_when_prts_page_matches(self) -> None:
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import _render_enemy_skill_card
        from ArknightsUID.utils.prts.client import EnemyInfo, EnemyLevel

        enemy = EnemyInfo(
            title="高普尼克", name="高普尼克",
            levels=[EnemyLevel(
                max_hp="30000", atk="2000", defense="200", res="50",
                abilities=["技能·横扫：对周围造成200%物理伤害（初始10·消耗20）"],
            )],
        )

        async def fake_query(name: str):
            return enemy if name == "高普尼克" else None

        async def fake_build(ctx_enemy):
            return {"abilities": ctx_enemy.levels[0].abilities, "has_stats": True}

        async def fake_render(templates, name, context):
            return f"IMG({name})"

        with (
            mock.patch(
                "ArknightsUID.utils.prts.client.query_enemy", new=fake_query
            ),
            mock.patch(
                "ArknightsUID.arknightsuid_wiki._build_enemy_context",
                new=fake_build,
            ),
            mock.patch(
                "ArknightsUID.utils.render.render_html", new=fake_render
            ),
        ):
            img = await _render_enemy_skill_card("高普尼克")
        self.assertEqual(img, "IMG(ark_wiki_enemy.html)")

    async def test_skips_on_name_mismatch_or_missing(self) -> None:
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import _render_enemy_skill_card
        from ArknightsUID.utils.prts.client import EnemyInfo, EnemyLevel

        near = EnemyInfo(
            title="高普尼克的故事", name="高普尼克的故事",
            levels=[EnemyLevel(abilities=["x"])],
        )

        async def fake_query(name: str):
            return near

        with mock.patch(
            "ArknightsUID.utils.prts.client.query_enemy", new=fake_query
        ):
            # 搜索命中近似页（名字对不上）→ 不发第二张
            self.assertIsNone(await _render_enemy_skill_card("高普尼克"))

        async def fake_query_none(name: str):
            return None

        with mock.patch(
            "ArknightsUID.utils.prts.client.query_enemy", new=fake_query_none
        ):
            self.assertIsNone(await _render_enemy_skill_card("不存在"))


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

        async def fake_skills():
            return {"taunt": ["嘲讽等级+<v.value>"]}

        async def fake_stage_index():
            return {
                "enemy_1_boss": [
                    {"theme": "sarkaz", "code": "ISW-NO", "name": "拆东补西",
                     "floors": "1-2层", "level": 0, "elite": False},
                ]
            }

        def _patches():
            import contextlib
            from unittest import mock as _mock

            stack = contextlib.ExitStack()
            for target, replacement in (
                ("ArknightsUID.utils.gamedata.rogue_enemy.load_enemy_db",
                 _mock.AsyncMock(return_value=_db())),
                ("ArknightsUID.utils.gamedata.rogue_enemy.load_difficulty_mods",
                 fake_diffs),
                ("ArknightsUID.utils.gamedata.rogue_enemy.enemy_icon_b64",
                 fake_icon),
                ("ArknightsUID.utils.gamedata.rogue_enemy.load_skill_dict",
                 fake_skills),
                ("ArknightsUID.utils.gamedata.rogue_enemy.build_stage_index",
                 fake_stage_index),
            ):
                stack.enter_context(
                    _mock.patch(target, new=replacement)
                )
            return stack

        with _patches():
            result = await _build_rogue_enemy_context("歌利亚")


        self.assertNotIsInstance(result, str)
        self.assertEqual(result["name"], "歌利亚")
        self.assertEqual(result["theme_name"], "萨卡兹的无终奇语")
        self.assertEqual(result["forms"][0]["hp"], 80000)
        self.assertIn("难度", result["panels"][0]["label"])
        # 免疫并集（中文）+ 形态次级属性段
        self.assertEqual(result["resistances"], ["眩晕", "沉默"])
        self.assertIn("间隔 4s", result["forms"][0]["extras"])
        # 出没关卡（索引分组）
        self.assertEqual(len(result["stage_groups"]), 1)
        self.assertEqual(
            result["stage_groups"][0]["stages"], ["拆东补西·ISW-NO·1-2层"]
        )

        text = _format_rogue_enemy(result)
        self.assertIn("肉鸽敌人：歌利亚", text)
        self.assertIn("HP96000", text)
        self.assertIn("抗性：眩晕、沉默", text)
        self.assertIn("出没·萨卡兹的无终奇语：拆东补西", text)

        html = ark_templates.get_template("ark_rogue_enemy.html").render(
            font_css_url="about:blank", **result
        )
        self.assertIn("歌利亚", html)
        self.assertIn("96000", html)
        self.assertIn("免疫 · 眩晕", html)
        self.assertIn("间隔 4s", html)
        self.assertIn("出没关卡", html)
        self.assertIn("拆东补西", html)
        self.assertIn("tomimi.dev", html)

    async def test_conditional_query_theme_and_difficulty(self) -> None:
        """`名字 期 N难度` → focus 精确面板（累积折算 + buff 全量）。"""
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import (
            _build_rogue_enemy_context,
            _format_rogue_enemy,
        )
        from ArknightsUID.utils.render import ark_templates

        async def fake_diffs():
            return {"sarkaz": _sarkaz_diffs()}

        async def fake_skills():
            return {}

        async def fake_stage_index():
            return {}

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
                new=mock.AsyncMock(return_value=""),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_skill_dict",
                new=fake_skills,
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.build_stage_index",
                new=fake_stage_index,
            ),
        ):
            result = await _build_rogue_enemy_context("歌利亚 萨卡兹 N10")

        self.assertNotIsInstance(result, str)
        self.assertEqual(result["theme_name"], "萨卡兹的无终奇语")
        focus = result["focus"]
        self.assertEqual(focus["grade"], 10)
        self.assertEqual(focus["stats"]["hp"], 96000)
        self.assertIn("生命×1.2", focus["buffs"])
        self.assertIn("受到伤害×0.1", focus["buffs"])
        # 指定难度时不再输出全档表
        self.assertEqual(result["panels"], [])
        # 形态带机制行（词典为空 → 空列表，模板兼容）
        self.assertIn("mechanics", result["forms"][0])

        text = _format_rogue_enemy(result)
        self.assertIn("指定面板（萨卡兹的无终奇语·难度10）", text)
        self.assertIn("生效：", text)

        html = ark_templates.get_template("ark_rogue_enemy.html").render(
            font_css_url="about:blank", **result
        )
        self.assertIn("指定面板", html)
        self.assertIn("受到伤害×0.1", html)

    async def test_difficulty_without_theme_hints_user(self) -> None:
        """未识别期的敌人带难度查询 → 提示指定期。"""
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import _build_rogue_enemy_context

        # enemy_3 无 type 标签且不被点名 → 未识别期
        with mock.patch(
            "ArknightsUID.utils.gamedata.rogue_enemy.load_enemy_db",
            new=mock.AsyncMock(return_value=_db()),
        ):
            result = await _build_rogue_enemy_context("源石虫·α N5")
        self.assertIsInstance(result, str)
        self.assertIn("指定期", result)

    async def test_enemy_with_stage_conditions(self) -> None:
        """`敌名 关名 N难度 紧急` → 关卡限定 focus（块选择 + 紧急来源）。"""
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import _build_rogue_enemy_context

        async def fake_diffs():
            return {"sarkaz": _sarkaz_diffs()}

        stage_fixture = {
            "enemies": [
                {"id": "enemy_1_boss", "level": 0, "min_count": 1,
                 "max_count": 1, "elite_min_count": 1, "elite_max_count": 2},
            ],
            "elite_mods": [{"targets": ["ALL"], "mods": [
                {"key": "atk", "value": 1.2, "mode": "mul"},
            ]}],
        }

        async def fake_find_stage(name: str):
            if name == "兀兀构筑":
                return {"theme": "sarkaz", "level_id": "level_rogue4_6-2",
                        "code": "ISW-DF", "name": "兀兀构筑"}
            return None

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
                new=mock.AsyncMock(return_value=""),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_skill_dict",
                new=mock.AsyncMock(return_value={}),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.build_stage_index",
                new=mock.AsyncMock(return_value={}),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.find_stage",
                new=fake_find_stage,
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_stage_data",
                new=mock.AsyncMock(return_value=stage_fixture),
            ),
        ):
            result = await _build_rogue_enemy_context("歌利亚 兀兀构筑 N10 紧急")

        self.assertNotIsInstance(result, str)
        self.assertEqual(result["theme_name"], "萨卡兹的无终奇语")
        self.assertEqual(result["stage_label"], "兀兀构筑·形态1")
        focus = result["focus"]
        self.assertIsNotNone(focus)
        # 难度10 累积：HP×1.2（难度4）、攻×1.1（难度7）×紧急攻×1.2 → 96000 / 3102
        self.assertEqual(focus["stats"]["hp"], 96000)
        self.assertEqual(focus["stats"]["atk"], 3102)
        self.assertTrue(any(b.startswith("紧急：") for b in focus["buffs"]))
        # 关卡限定时不再输出全档表
        self.assertEqual(result["panels"], [])

    async def test_stage_context_and_render(self) -> None:
        """关卡视图：折算行 + 紧急描述 + 模板契约。"""
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import (
            _build_rogue_stage_context,
            _format_rogue_stage,
        )
        from ArknightsUID.utils.render import ark_templates

        async def fake_diffs():
            return {"sarkaz": _sarkaz_diffs()}

        stage_fixture = dict(StagePanelRowTest.STAGE)
        stage_fixture["floors"] = [1, 2]
        stage_fixture["eliteDesc_zh"] = ["所有敌方单位的攻击力+20%", "额外出现1个歌利亚"]

        async def fake_find_stage(name: str):
            if name == "拆东补西":
                return {"theme": "sarkaz", "level_id": "level_rogue4_2-2",
                        "code": "ISW-NO", "name": "拆东补西"}
            return None

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
                new=mock.AsyncMock(return_value=""),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.find_stage",
                new=fake_find_stage,
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.search_stages",
                new=mock.AsyncMock(return_value=[]),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_stage_data",
                new=mock.AsyncMock(return_value=stage_fixture),
            ),
        ):
            result = await _build_rogue_stage_context("拆东补西 N4 紧急")

        self.assertNotIsInstance(result, str)
        self.assertEqual(result["theme_name"], "萨卡兹的无终奇语")
        self.assertEqual(result["difficulty"], 4)
        self.assertTrue(result["elite"])
        self.assertEqual(result["floors"], "1-2层")
        # 折算行：歌利亚 HP 80000×1.2×1.2
        boss_row = next(r for r in result["rows"] if r["name"] == "歌利亚")
        self.assertEqual(boss_row["stats"]["hp"], 115200)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(result["elite_chips"])

        text = _format_rogue_stage(result)
        self.assertIn("肉鸽关卡：拆东补西（ISW-NO）", text)
        self.assertIn("紧急作战：", text)
        self.assertIn("查具体敌人：ark肉鸽敌人 <敌名> 拆东补西 N4 紧急", text)

        html = ark_templates.get_template("ark_rogue_stage.html").render(
            font_css_url="about:blank", **result
        )
        self.assertIn("拆东补西", html)
        self.assertIn("紧急作战", html)
        self.assertIn("115200", html)
        self.assertIn("ark肉鸽敌人", html)

    async def test_enemy_command_falls_back_to_stage(self) -> None:
        """敌人命令查关卡名：无敌人命中 → 渲染关卡卡。"""
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import _build_rogue_enemy_context

        stage_fixture = dict(StagePanelRowTest.STAGE)
        stage_fixture["floors"] = [2]

        async def fake_find_stage(name: str):
            if name == "拆东补西":
                return {"theme": "sarkaz", "level_id": "level_rogue4_2-2",
                        "code": "ISW-NO", "name": "拆东补西"}
            return None

        async def fake_diffs():
            return {"sarkaz": _sarkaz_diffs()}

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
                new=mock.AsyncMock(return_value=""),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.find_stage",
                new=fake_find_stage,
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.search_stages",
                new=mock.AsyncMock(return_value=[]),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_stage_data",
                new=mock.AsyncMock(return_value=stage_fixture),
            ),
        ):
            result = await _build_rogue_enemy_context("拆东补西")

        self.assertNotIsInstance(result, str)
        self.assertEqual(result["name"], "拆东补西")
        self.assertEqual(result["floors"], "2层")

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


class SourceOutageTest(unittest.IsolatedAsyncioTestCase):
    """tomimi 数据源整体不可用（无缓存 + 下载全失败）的端到端降级。

    契约：处理器返回给用户的是可读提示文本（str），不抛异常、不空回复。
    HTTP 层的过期兜底/负标记由 CachePolicyTest 覆盖；此处验证的是
    loader 全部退化（空库/空索引）后命令链的最终行为。
    """

    async def test_enemy_query_degrades_to_readable_text(self) -> None:
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import _build_rogue_enemy_context

        with (
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_enemy_db",
                new=mock.AsyncMock(return_value={}),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_skill_dict",
                new=mock.AsyncMock(return_value={}),
            ),
        ):
            result = await _build_rogue_enemy_context("歌利亚")
        self.assertIsInstance(result, str)
        self.assertIn("敌人库不可用", result)
        self.assertIn("稍后再试", result)

    async def test_stage_query_degrades_to_readable_text(self) -> None:
        from unittest import mock

        from ArknightsUID.arknightsuid_rogue import _build_rogue_stage_context

        with (
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy._load_stage_meta",
                new=mock.AsyncMock(return_value={}),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_enemy.load_enemy_db",
                new=mock.AsyncMock(return_value={}),
            ),
        ):
            result = await _build_rogue_stage_context("拆东补西 N15")
        self.assertIsInstance(result, str)
        self.assertIn("未找到关卡", result)


class EnemyIconQuiet404Test(unittest.IsolatedAsyncioTestCase):
    """敌人图标 404（最新期上游渐进上传）应静默：quiet_404 + .miss 负缓存。"""

    async def test_icon_404_quiet_with_miss_marker(self) -> None:
        import tempfile
        from unittest import mock

        from ArknightsUID.utils.gamedata import rogue_enemy

        seen: dict = {}

        async def fake_status(url: str, **kw):
            seen["url"], seen["kw"] = url, kw
            return 404, None

        with tempfile.TemporaryDirectory() as td:
            with (
                mock.patch.object(rogue_enemy, "IS_ENEMY_CACHE_DIR", td),
                mock.patch.object(rogue_enemy, "download_bytes_status", fake_status),
                mock.patch.object(
                    rogue_enemy,
                    "download_bytes",
                    mock.AsyncMock(side_effect=AssertionError("应走 _status 入口")),
                    create=True,
                ),
            ):
                result = await rogue_enemy.enemy_icon_b64("enemy_missing_key")
            miss_written = (Path(td) / "icons" / "enemy_missing_key.webp.miss").exists()
        self.assertEqual(result, "")
        self.assertIn("enemy_icons", seen.get("url", ""))
        self.assertTrue(seen.get("kw", {}).get("quiet_404"))
        self.assertTrue(miss_written)


if __name__ == "__main__":
    unittest.main()
