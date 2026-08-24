"""公招查询逻辑测试（build/match，不触发网络下载）。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from ArknightsUID.utils.gamedata import recruit


class RecruitLogicTest(unittest.TestCase):
    def test_build_recruit_data_filters_and_maps(self) -> None:
        char_table = {
            "char_a": {  # 可公招 5 星术师
                "name": "测试A", "rarity": "TIER_5", "profession": "CASTER",
                "position": "RANGED", "tagList": ["输出"], "itemObtainApproach": "招募寻访",
            },
            "char_b": {  # 6 星（TIER_6）→ 排除（不在公招池）
                "name": "测试B", "rarity": "TIER_6", "profession": "WARRIOR",
                "position": "MELEE", "tagList": [], "itemObtainApproach": "招募寻访",
            },
            "char_c": {  # NPC（approach 空）→ 排除
                "name": "NPC", "rarity": "TIER_3", "profession": "PIONEER",
                "position": "MELEE", "tagList": [], "itemObtainApproach": "",
            },
            "char_d": {  # 活动获得 → 排除
                "name": "活动干员", "rarity": "TIER_5", "profession": "MEDIC",
                "position": "RANGED", "tagList": [], "itemObtainApproach": "活动获得",
            },
        }
        data = recruit.build_recruit_data(char_table)

        self.assertEqual(len(data), 1)
        entry = data[0]
        self.assertEqual(entry["name"], "测试A")
        # TIER_5 = 5 星（character_table 的 TIER_x = x 星）
        self.assertEqual(entry["rarity"], 5)
        self.assertEqual(entry["profession"], "术师")
        self.assertEqual(entry["position"], "远程位")
        # 5 星自动获得"高级资深干员"标签（阈值语义：4★+ 资深 / 5★+ 高级资深）
        self.assertIn("高级资深干员", entry["all_tags"])
        self.assertIn("资深干员", entry["all_tags"])
        self.assertIn("输出", entry["all_tags"])
        self.assertIn("术师", entry["all_tags"])

    def test_build_recruit_data_rarity_tag_thresholds(self) -> None:
        # 4 星 → 资深干员；3 星 → 无稀有度标签
        char_table = {
            "c4": {"name": "四星", "rarity": "TIER_4", "profession": "SNIPER",
                   "position": "RANGED", "tagList": [], "itemObtainApproach": "招募寻访"},
            "c3": {"name": "三星", "rarity": "TIER_3", "profession": "PIONEER",
                   "position": "MELEE", "tagList": [], "itemObtainApproach": "招募寻访"},
        }
        data = {e["name"]: e for e in recruit.build_recruit_data(char_table)}
        self.assertIn("资深干员", data["四星"]["all_tags"])
        self.assertNotIn("高级资深干员", data["四星"]["all_tags"])
        self.assertNotIn("资深干员", data["三星"]["all_tags"])
        self.assertNotIn("高级资深干员", data["三星"]["all_tags"])

    def test_match_recruit_returns_tag_superset_sorted(self) -> None:
        data = [
            {"name": "高星术师", "rarity": 5, "tags": ["输出"],
             "all_tags": ["术师", "远程位", "高级资深干员", "输出"]},
            {"name": "四星狙击", "rarity": 4, "tags": [],
             "all_tags": ["狙击", "远程位", "资深干员"]},
        ]
        # 高级资深+术师 → 只高星术师
        matched = recruit.match_recruit(["高级资深干员", "术师"], data)
        self.assertEqual([m["name"] for m in matched], ["高星术师"])
        # 单标签 狙击 → 四星狙击
        matched = recruit.match_recruit(["狙击"], data)
        self.assertEqual([m["name"] for m in matched], ["四星狙击"])
        # 空标签 → []
        self.assertEqual(recruit.match_recruit([], data), [])
        # 无匹配 → []
        self.assertEqual(recruit.match_recruit(["不存在的标签"], data), [])

    def test_match_recruit_sorts_by_rarity_desc(self) -> None:
        data = [
            {"name": "三星", "rarity": 3, "tags": [], "all_tags": ["先锋"]},
            {"name": "五星", "rarity": 5, "tags": [], "all_tags": ["先锋"]},
            {"name": "四星", "rarity": 4, "tags": [], "all_tags": ["先锋"]},
        ]
        matched = recruit.match_recruit(["先锋"], data)
        # 按稀有度降序
        self.assertEqual([m["rarity"] for m in matched], [5, 4, 3])

    def test_match_recruit_strips_whitespace(self) -> None:
        data = [{"name": "X", "rarity": 3, "tags": [], "all_tags": ["先锋"]}]
        matched = recruit.match_recruit([" 先锋 "], data)
        self.assertEqual(len(matched), 1)



class IconNegativeCacheTest(unittest.IsolatedAsyncioTestCase):
    """图标失败负缓存（.miss，1h）：名字对不上文件时不再重复打 CDN。

    场景：关卡敌人数值一次取最多 10 个头像，带修饰的敌人名（源石虫·α）
    按散列直链查不到文件，不缓存会每次查询都重试。
    """

    async def test_missing_icon_negatively_cached(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        import ArknightsUID.utils.gamedata.itemicon as icon_mod

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            calls: list[str] = []

            async def fail_get(url, cache_dir, *args, **kwargs):
                calls.append(url)
                return ""

            with (
                mock.patch.object(icon_mod, "ITEM_ICON_CACHE_DIR", cache),
                mock.patch(
                    "ArknightsUID.utils.render.get_image_b64_with_cache", fail_get
                ),
            ):
                self.assertEqual(await icon_mod.enemy_icon_b64("源石虫·α"), "")
                self.assertEqual(await icon_mod.enemy_icon_b64("源石虫·α"), "")
            # 第二次命中负缓存，不再请求
            self.assertEqual(len(calls), 1)
            self.assertTrue(
                (cache / f"{icon_mod.prts_enemy_icon_url('源石虫·α').split('/')[-1]}.miss").exists()
            )

    async def test_existing_icon_skips_miss_marker(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        import ArknightsUID.utils.gamedata.itemicon as icon_mod

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)

            async def ok_get(url, cache_dir, *args, **kwargs):
                return "B64"

            with (
                mock.patch.object(icon_mod, "ITEM_ICON_CACHE_DIR", cache),
                mock.patch(
                    "ArknightsUID.utils.render.get_image_b64_with_cache", ok_get
                ),
            ):
                self.assertEqual(await icon_mod.item_icon_b64("固源岩组"), "B64")
            marker = cache / f"{icon_mod.prts_item_icon_url('固源岩组').split('/')[-1]}.miss"
            self.assertFalse(marker.exists())

    async def test_empty_name_returns_empty_without_request(self) -> None:
        from unittest import mock

        import ArknightsUID.utils.gamedata.itemicon as icon_mod

        async def boom(url, cache_dir, *args, **kwargs):
            raise AssertionError("空名不应触发下载")

        with mock.patch(
            "ArknightsUID.utils.render.get_image_b64_with_cache", boom
        ):
            self.assertEqual(await icon_mod.enemy_icon_b64("  "), "")
            self.assertEqual(await icon_mod.item_icon_b64(""), "")


if __name__ == "__main__":
    unittest.main()


class DiskCacheConcurrentWriteTest(unittest.IsolatedAsyncioTestCase):
    """同路径并发 cache_write：tmp 名必须唯一，不得互相覆盖半成品。"""

    async def test_concurrent_writes_all_succeed(self) -> None:
        import json as json_mod
        import tempfile
        from pathlib import Path

        from ArknightsUID.utils.disk_cache import cache_read, cache_write

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "c.json"
            payload = {"blob": "x" * 2_000_000}
            results = await asyncio.gather(
                *(cache_write(path, payload) for _ in range(8)),
                return_exceptions=True,
            )
            self.assertEqual([r for r in results if r is not None], [])
            # 落盘内容完整可解析（无交错写入的撕裂）
            data, _ts = await cache_read(path)
            self.assertEqual(json_mod.loads(json_mod.dumps(payload)), data)
            # 无残留 tmp 文件
            leftovers = list(Path(td).glob("*.tmp*"))
            self.assertEqual(leftovers, [])

    async def test_table_download_single_flight(self) -> None:
        """TTL 过期瞬间的并发 ensure：只触发一次下载（锁内复查新鲜度）。"""
        from unittest import mock

        import ArknightsUID.utils.gamedata._table_cache as tc

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.json"
            calls = []

            async def fake_download(url):
                calls.append(url)
                await asyncio.sleep(0.05)  # 模拟慢下载，让并发都堆在锁上
                return {"v": 1}

            with mock.patch.object(tc, "_download_json", new=fake_download):
                results = await asyncio.gather(
                    *(tc._ensure_table("http://x/t.json", path) for _ in range(6))
                )
            self.assertEqual(results, [{"v": 1}] * 6)
            self.assertEqual(len(calls), 1)
