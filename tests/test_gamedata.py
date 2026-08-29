"""公招查询逻辑测试（build/match，不触发网络下载）。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from ArknightsUID.utils.gamedata import recruit


class RecruitLogicTest(unittest.TestCase):
    def test_entries_from_smw_maps_and_splits_tags(self) -> None:
        results = {
            "测试A": {"printouts": {  # 5★ 术师 → 资深干员
                "稀有度": ["4"], "职业": ["术师"], "位置": ["远程位"],
                "标签": ["输出 群攻"],
            }},
            "测试B": {"printouts": {  # 6★ 近卫 → 高级资深干员
                "稀有度": ["5"], "职业": ["近卫"], "位置": ["近战位"],
                "标签": [],
            }},
            "测试C": {"printouts": {  # 1★ 小车 → 支援机械
                "稀有度": ["0"], "职业": ["先锋"], "位置": ["近战位"],
                "标签": ["支援机械 控场"],
            }},
            "坏条目": {"printouts": {"稀有度": ["?"], "职业": [], "位置": [], "标签": []}},
        }
        data = {e["name"]: e for e in recruit.entries_from_smw(results)}

        self.assertNotIn("坏条目", data)
        # PRTS 稀有度 0-5 → 展示 1-6 星
        self.assertEqual(data["测试A"]["rarity"], 5)
        self.assertEqual(data["测试B"]["rarity"], 6)
        self.assertEqual(data["测试C"]["rarity"], 1)
        # 5★ → 资深（不含高资）；6★ → 高资（不含资深）
        self.assertIn("资深干员", data["测试A"]["all_tags"])
        self.assertNotIn("高级资深干员", data["测试A"]["all_tags"])
        self.assertIn("高级资深干员", data["测试B"]["all_tags"])
        self.assertNotIn("资深干员", data["测试B"]["all_tags"])
        # 能力标签长串按空格拆开 + 职业/位置挂载
        self.assertEqual(data["测试A"]["tags"], ["输出", "群攻"])
        self.assertIn("术师", data["测试A"]["all_tags"])
        self.assertIn("支援机械", data["测试C"]["all_tags"])
        self.assertIn("控场", data["测试C"]["all_tags"])

    def test_match_recruit_rarity_tag_semantics(self) -> None:
        data = recruit.entries_from_smw({
            "六星术师": {"printouts": {
                "稀有度": ["5"], "职业": ["术师"], "位置": ["远程位"], "标签": ["输出"]}},
            "五星术师": {"printouts": {
                "稀有度": ["4"], "职业": ["术师"], "位置": ["远程位"], "标签": ["输出"]}},
            "四星近卫": {"printouts": {
                "稀有度": ["3"], "职业": ["近卫"], "位置": ["近战位"], "标签": []}},
        })
        # 高级资深池 = 6★ only；资深池 = 5★ only
        self.assertEqual(
            [m["name"] for m in recruit.match_recruit(["高级资深干员"], data)],
            ["六星术师"],
        )
        self.assertEqual(
            [m["name"] for m in recruit.match_recruit(["资深干员"], data)],
            ["五星术师"],
        )
        # 高资+术师 → 六星术师；资深+近卫 → 空（交集不存在）
        self.assertEqual(
            [m["name"] for m in recruit.match_recruit(["高级资深干员", "术师"], data)],
            ["六星术师"],
        )
        self.assertEqual(recruit.match_recruit(["资深干员", "近卫"], data), [])

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

    def test_item_icon_url_strips_drop_mode_suffix(self) -> None:
        """PRTS 掉落文本的掉落方式后缀不得进入图标文件名（否则 404）。"""
        import ArknightsUID.utils.gamedata.itemicon as icon_mod

        cases = [
            ("固源岩", "固源岩:固定掉落"),
            ("全新装置", "全新装置:小概率"),
            ("固源岩", "固源岩:概率掉落"),
            ("固源岩", "固源岩：全角冒号"),
        ]
        for clean, suffixed in cases:
            self.assertEqual(
                icon_mod.prts_item_icon_url(suffixed),
                icon_mod.prts_item_icon_url(clean),
                f"后缀未剥离：{suffixed}",
            )

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
