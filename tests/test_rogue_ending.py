"""肉鸽结局攻略合并/匹配 + 结局卡 context 与模板契约测试。

数据契约（utils/gamedata/rogue_ending.py）：结局名单以主题表 endings 为
权威（按 ending_id join），前置链来自内置攻略数据 ENDING_GUIDES；表有而
攻略没有 → has_guide=False（攻略整理中），表整体不可用 → 攻略兜底展示。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

import ArknightsUID.utils.gamedata.rogue_ending as rogue_ending


def _reset_cache() -> None:
    rogue_ending._cache["themes"] = {}
    rogue_ending._cache["ts"] = 0.0


def _topic_table() -> dict:
    """最小同构切片：萨卡兹一期 + 表内多出的未收录结局 + 部分关键藏品。"""
    return {
        "topics": {"rogue_4": {"name": "萨卡兹的无终奇语"}},
        "details": {
            "rogue_4": {
                "endings": {
                    "ro4_ending_1": {
                        "id": "ro4_ending_1", "name": "憧憬未来",
                        "desc": "把故事讲到最后。",
                    },
                    "ro4_ending_2": {
                        "id": "ro4_ending_2", "name": "双王记", "desc": "两位魔王。",
                    },
                    # 表内有而攻略没有（模拟新 DLC 刚上线）
                    "ro4_ending_99": {
                        "id": "ro4_ending_99", "name": "新结局", "desc": "未收录。",
                    },
                },
                "items": {
                    "rogue_4_relic_final_1": {
                        "name": "巴别塔誓言",
                        "iconId": "rogue_4_relic_final_1",
                    },
                },
            },
        },
    }


class MergeEndingTest(unittest.IsolatedAsyncioTestCase):
    async def _load(self, table: dict | None) -> dict:
        _reset_cache()
        with mock.patch.object(
            rogue_ending,
            "ensure_rogue_topic_table",
            new=mock.AsyncMock(return_value=table),
        ):
            return await rogue_ending.load_all_endings()

    async def test_table_endings_join_and_append_uncurated(self) -> None:
        themes = await self._load(_topic_table())
        sarkaz = themes["sarkaz"]
        self.assertEqual(sarkaz["theme_name"], "萨卡兹的无终奇语")
        rows = sarkaz["rows"]
        # 5 个攻略条目 + 1 个表内未收录 = 6
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r["has_guide"] for r in rows[:5]))
        extra = rows[5]
        self.assertEqual(extra["name"], "新结局")
        self.assertFalse(extra["has_guide"])
        self.assertEqual(extra["desc"], "未收录。")
        # 表内名/desc 按 ending_id join（攻略兜底名与表一致时不可区分，
        # 用 desc 验证 join 生效）
        self.assertEqual(rows[0]["name"], "憧憬未来")
        self.assertEqual(rows[0]["desc"], "把故事讲到最后。")
        # 未提供切片的期退化为攻略兜底（表缺失 ≠ 无数据）
        self.assertEqual(themes["phantom"]["rows"][0]["name"], "舞会终场")
        self.assertTrue(themes["phantom"]["rows"][0]["has_guide"])

    async def test_key_relic_chips_resolve_from_items(self) -> None:
        themes = await self._load(_topic_table())
        row2 = themes["sarkaz"]["rows"][1]  # 双王记
        chips = {c["name"]: c["item_id"] for c in row2["key_relics"]}
        self.assertEqual(chips["巴别塔誓言"], "rogue_4_relic_final_1")
        # 表内查不到的藏品只显示名（item_id 空）
        self.assertEqual(chips["纯白花瓣"], "")

    async def test_offline_degrades_to_curated_guide(self) -> None:
        themes = await self._load(None)
        self.assertTrue(themes)
        for data in themes.values():
            for row in data["rows"]:
                self.assertTrue(row["has_guide"])


class ResolveQueryTest(unittest.IsolatedAsyncioTestCase):
    async def _resolve(self, query: str, aliases: dict | None = None):
        _reset_cache()
        with mock.patch.object(
            rogue_ending,
            "ensure_rogue_topic_table",
            new=mock.AsyncMock(return_value=_topic_table()),
        ):
            return await rogue_ending.resolve_query(query, aliases)

    async def test_theme_ending_and_relic_queries(self) -> None:
        self.assertEqual(
            await self._resolve("萨卡兹"), ("sarkaz", [], "theme")
        )
        # 用户自定义期名优先
        self.assertEqual(
            await self._resolve("卡兹", {"卡兹": "sarkaz"}),
            ("sarkaz", [], "theme"),
        )
        self.assertEqual(
            await self._resolve("双王记"), ("sarkaz", ["双王记"], "ending")
        )
        # 关键藏品名反查结局
        self.assertEqual(
            await self._resolve("巴别塔誓言"),
            ("sarkaz", ["双王记"], "relic"),
        )

    async def test_miss_and_candidates(self) -> None:
        miss = await self._resolve("不存在的查询")
        self.assertIsInstance(miss, str)
        self.assertIn("未找到", miss)
        # 多命中前缀（无中生有 / 无瑕之日）→ 候选提示
        multi = await self._resolve("无")
        self.assertIsInstance(multi, str)
        self.assertIn("找到 2 个候选", multi)
        self.assertIn("无中生有", multi)
        self.assertIn("无瑕之日", multi)


class EndingCardTest(unittest.IsolatedAsyncioTestCase):
    """结局卡 context ↔ 模板契约（jinja 全量渲染；图标/主题图失败降级）。"""

    async def _context(self, query: str) -> dict:
        from ArknightsUID.arknightsuid_rogue import _build_rogue_ending_context

        _reset_cache()

        async def fake_icon(item_id: str) -> str:
            return "BASE64ICON" if item_id else ""

        with (
            mock.patch.object(
                rogue_ending,
                "ensure_rogue_topic_table",
                new=mock.AsyncMock(return_value=_topic_table()),
            ),
            mock.patch(
                "ArknightsUID.utils.gamedata.rogue_relic.relic_icon_b64",
                new=fake_icon,
            ),
            mock.patch(
                "ArknightsUID.utils.prts.client.activity_banner_urls",
                new=mock.AsyncMock(return_value={}),
            ),
        ):
            result = await _build_rogue_ending_context(query)
        self.assertIsInstance(result, dict)
        return result

    async def test_full_theme_context_and_render(self) -> None:
        from ArknightsUID.arknightsuid_rogue import _format_rogue_ending
        from ArknightsUID.utils.render import ark_templates

        context = await self._context("萨卡兹")
        self.assertEqual(context["theme_name"], "萨卡兹的无终奇语")
        self.assertEqual(context["count"], 6)
        self.assertEqual(context["shown_count"], 6)
        self.assertEqual(context["focus_note"], "")
        rows = context["endings"]
        # 序号按期全局位置（新结局排第 6）
        self.assertEqual(rows[-1]["seq"], "06")
        self.assertEqual(rows[-1]["name"], "新结局")
        # 双王记（第 2 个）的藏品图标：表内命中有图，未命中无图
        chips = {c["name"]: c["icon"] for c in rows[1]["key_relics"]}
        self.assertEqual(chips["巴别塔誓言"], "BASE64ICON")
        self.assertEqual(chips["纯白花瓣"], "")

        text = _format_rogue_ending(context)
        self.assertIn("萨卡兹的无终奇语", text)
        self.assertIn("02 双王记", text)
        self.assertIn("巴别塔誓言", text)
        self.assertIn("攻略整理中", text)  # 未收录的新结局降级提示

        template = ark_templates.get_template("ark_rogue_ending.html")
        html = template.render(font_css_url="about:blank", **context)
        self.assertIn("萨卡兹的无终奇语", html)
        self.assertIn("双王记", html)
        self.assertIn("巴别塔誓言", html)
        self.assertIn("攻略核对", html)
        self.assertIn("PRTS Wiki", html)

    async def test_relic_focus_context(self) -> None:
        context = await self._context("巴别塔誓言")
        self.assertEqual(context["count"], 6)
        self.assertEqual(context["shown_count"], 1)
        self.assertEqual(context["focus_note"], "按关键藏品「巴别塔誓言」反查")
        self.assertEqual(context["endings"][0]["name"], "双王记")
        # 聚焦时序号保持期全局位置（双王记是第 2 个）
        self.assertEqual(context["endings"][0]["seq"], "02")


if __name__ == "__main__":
    unittest.main()
