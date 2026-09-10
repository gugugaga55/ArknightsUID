"""肉鸽藏品索引/匹配/分组 + 藏品卡与肉鸽进度卡展示测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.gamedata.rogue_relic import (
    RARITY_NAMES,
    build_relic_index,
    group_relic_effects,
    match_relics,
)


def _topic_table() -> dict:
    """最小同构切片：两期主题、一个跨期同名藏品 + 一个单期藏品 + 噪音条目。"""
    def item(name, usage, rarity="RARE", icon="icon_x", desc=""):
        return {
            "name": name, "usage": usage, "rarity": rarity,
            "iconId": icon, "description": desc, "type": "RELIC",
        }

    return {
        "topics": {
            "rogue_4": {"name": "萨卡兹的无终奇语"},
            "rogue_5": {"name": "岁的界园志异"},
        },
        "details": {
            "rogue_4": {
                "relics": {"a1": {}, "a2": {}, "noise": {}},
                "items": {
                    "a1": item("钝爪-典训", "立即进阶一个【先锋】干员", "NORMAL", "icon_a"),
                    "a2": item("《拳经三问》", "攻击力+5%", "RARE", "icon_b", "记载拳法的古书"),
                    # 非 RELIC 类型不入索引（招募券等）
                    "noise": {"name": "券", "usage": "x", "type": "RECRUIT_TICKET"},
                    # items 有但 relics 成员没有的条目不入索引
                    "orphan": item("孤儿", "不应出现"),
                },
            },
            "rogue_5": {
                "relics": {"b1": {}},
                "items": {
                    # 同名跨期但效果不同（主题强化版）
                    "b1": item("钝爪-典训", "立即进阶两个【先锋】干员", "NORMAL", "icon_a2"),
                },
            },
        },
    }


class BuildRelicIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = build_relic_index(_topic_table())

    def test_index_filters_and_aggregates(self) -> None:
        # 非 RELIC / 非 relics 成员不入索引
        self.assertNotIn("券", self.index)
        self.assertNotIn("孤儿", self.index)
        # 跨期同名聚合并按主题新→旧排序
        entries = self.index["钝爪-典训"]
        self.assertEqual(
            [e["theme_name"] for e in entries], ["岁的界园志异", "萨卡兹的无终奇语"]
        )
        self.assertEqual(entries[0]["usage"], "立即进阶两个【先锋】干员")

    def test_match_exact_prefix_and_multi(self) -> None:
        self.assertEqual(match_relics("钝爪-典训", self.index), ["钝爪-典训"])
        self.assertEqual(match_relics("拳经", self.index), ["《拳经三问》"])
        multi = match_relics("钝爪", self.index)
        self.assertEqual(multi, ["钝爪-典训"])  # 唯一前缀直接命中
        self.assertEqual(match_relics("不存在", self.index), [])

    def test_group_by_usage_dedupes(self) -> None:
        entries = self.index["钝爪-典训"]
        groups = group_relic_effects(entries)
        # 两期效果不同 → 两组各一期
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["themes"], ["岁的界园志异"])
        self.assertEqual(RARITY_NAMES.get(groups[0]["rarity"]), "普通")
        # 完全同效果（模拟）合并主题列表
        same = [
            {"usage": "X", "rarity": "RARE", "theme_name": "A", "icon_id": "i", "desc": ""},
            {"usage": "X", "rarity": "RARE", "theme_name": "B", "icon_id": "i", "desc": ""},
        ]
        merged = group_relic_effects(same)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["themes"], ["A", "B"])


class RelicCardTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_relic_context_and_render(self) -> None:
        """藏品卡 context ↔ 模板契约（jinja 全量渲染；图标失败降级空串）。"""
        from unittest import mock

        from ArknightsUID.arknightsuid_wiki import _build_relic_context, _format_relic
        from ArknightsUID.utils.render import ark_templates

        groups = [
            {
                "usage": "立即进阶一个【先锋】干员（不消耗希望）",
                "rarity": "NORMAL",
                "rarity_name": "普通",
                "icon_id": "rogue_4_relic_legacy_1",
                "desc": "一套强有力的装备，一颗勇往直前的心。",
                "themes": ["萨卡兹的无终奇语", "岁的界园志异"],
            }
        ]

        async def fake_icon(icon_id: str) -> str:
            self.assertEqual(icon_id, "rogue_4_relic_legacy_1")
            return "BASE64ICON"

        with mock.patch(
            "ArknightsUID.utils.gamedata.rogue_relic.relic_icon_b64",
            new=fake_icon,
        ):
            context = await _build_relic_context("钝爪-典训", groups)

        self.assertEqual(context["rows"][0]["icon"], "BASE64ICON")
        self.assertIn("不消耗希望", context["rows"][0]["usage"])
        text = _format_relic("钝爪-典训", groups)
        self.assertIn("钝爪-典训", text)
        self.assertIn("普通", text)
        self.assertIn("不消耗希望", text)

        template = ark_templates.get_template("ark_rogue_relic.html")
        html = template.render(font_css_url="about:blank", **context)
        self.assertIn("钝爪-典训", html)
        self.assertIn("不消耗希望", html)
        self.assertIn("萨卡兹的无终奇语 · 岁的界园志异", html)
        self.assertIn("tomimi.dev", html)


class SourceOutageTest(unittest.IsolatedAsyncioTestCase):
    """主题表整体不可用（无精简缓存 + 全量下载失败）→ 空索引，不抛异常。

    处理器（send_relic_query）对空索引返回「藏品数据不可用」提示；
    此处验证索引层在该场景的退化契约：返回 {} 而非异常/None。
    """

    async def test_load_relic_index_degrades_to_empty(self) -> None:
        import tempfile
        from unittest import mock

        import ArknightsUID.utils.gamedata.rogue_relic as rr

        with tempfile.TemporaryDirectory() as td:
            with (
                mock.patch.object(
                    rr, "RELIC_INDEX_PATH", new=Path(td) / "no_index.json"
                ),
                mock.patch.object(
                    rr,
                    "ensure_rogue_topic_table",
                    new=mock.AsyncMock(return_value=None),
                ),
            ):
                result = await rr.load_relic_index()
        self.assertEqual(result, {})


class RogueCardUpgradeTest(unittest.TestCase):
    def _response(self) -> dict:
        return {
            "data": {
                "status": {"nickName": "刀客塔", "uid": "123"},
                "rogue": {
                    "records": [
                        {"rogueId": "rogue_1", "relicCnt": 229, "bank": {"current": 445, "record": 540},
                         "clearTime": 69, "bpLevel": 155, "medal": {"total": 11, "current": 11}},
                        {"rogueId": "rogue_6", "relicCnt": 40, "bank": {"current": 10, "record": 99},
                         "clearTime": 3, "bpLevel": 30, "medal": {"total": 12, "current": 2}},
                    ]
                },
            }
        }

    def test_context_orders_latest_with_totals(self) -> None:
        from ArknightsUID.arknightsuid_rogue import _build_rogue_context

        context = _build_rogue_context(self._response(), shown_uid="1**3")
        assert context is not None
        # 最新期（rogue_6）在前并标记；主题名按推位规律映射（玩家 rogue_6 = 岁的界园志异）
        self.assertEqual(context["records"][0]["rogue_id"], "rogue_6")
        self.assertEqual(context["records"][0]["name"], "岁的界园志异")
        self.assertTrue(context["records"][0]["latest"])
        self.assertFalse(context["records"][1]["latest"])
        self.assertEqual(context["total_clears"], 72)
        self.assertEqual(context["total_relics"], 269)

    def test_context_uid_is_caller_masked_not_raw(self) -> None:
        """审计 #3：卡片 UID 必须用调用方 hide_uid_for 的结果，不吃接口原始值。"""
        from ArknightsUID.arknightsuid_rogue import _build_rogue_context

        context = _build_rogue_context(self._response(), shown_uid="1**3")
        assert context is not None
        self.assertEqual(context["uid"], "1**3")
        self.assertNotIn("123", context["uid"])

    def test_context_prefers_status_name_over_nick_name(self) -> None:
        """真实接口字段是 status.name；nickName 仅为旧字段兜底（审计 #3）。"""
        from ArknightsUID.arknightsuid_rogue import _build_rogue_context

        response = self._response()
        response["data"]["status"]["name"] = "刀客塔不吃香菜"
        context = _build_rogue_context(response, shown_uid="1**3")
        assert context is not None
        self.assertEqual(context["name"], "刀客塔不吃香菜")

        # 无 name 时回退 nickName，再回退"博士"
        response["data"]["status"].pop("name")
        self.assertEqual(
            _build_rogue_context(response, shown_uid="")["name"], "刀客塔"
        )
        response["data"]["status"].pop("nickName")
        self.assertEqual(
            _build_rogue_context(response, shown_uid="")["name"], "博士"
        )

    def test_text_lists_theme_names_with_total(self) -> None:
        from ArknightsUID.arknightsuid_rogue import _format_rogue

        text = _format_rogue(self._response())
        self.assertIn("岁的界园志异：通关 3", text)
        self.assertIn("合计通关 72 次（2 期主题）", text)


class RelicIconSourceTest(unittest.TestCase):
    """藏品图标源必须钉在活跃分支：上游 tanch-p/tomimi-dev 已删除 master
    分支（活分支 main），@master 是 jsdelivr 缓存的死快照——黑流树海等
    最新期的图标上传后永远到不了 @master（实测 2026-09）。"""

    def test_base_uses_active_branch(self) -> None:
        from ArknightsUID.utils.gamedata import rogue_relic

        self.assertIn("@main/", rogue_relic._TOMIMI_STATIC_BASE)
        self.assertNotIn("@master", rogue_relic._TOMIMI_STATIC_BASE)


class RelicIconQuiet404Test(unittest.IsolatedAsyncioTestCase):
    """404 = 数据源尚未上传该图标（最新期常态），应静默降级不打 warning。"""

    async def test_icon_fetch_uses_main_url_and_quiet_404(self) -> None:
        import tempfile
        from unittest import mock

        from ArknightsUID.utils.gamedata import rogue_relic

        seen: dict = {}

        async def fake_status(url: str, **kw):
            seen["url"], seen["kw"] = url, kw
            return 404, None

        async def legacy_download(url: str, **kw):
            seen["legacy"] = url
            return None

        with tempfile.TemporaryDirectory() as td:
            with (
                mock.patch.object(rogue_relic, "RELIC_ICON_CACHE_DIR", Path(td)),
                mock.patch.object(rogue_relic, "download_bytes_status", fake_status),
                # create=True：修复后模块已不导入旧入口；若回退到它会在此暴露
                mock.patch.object(
                    rogue_relic, "download_bytes", legacy_download, create=True
                ),
            ):
                result = await rogue_relic.relic_icon_b64("rogue_6_relic_final_1")
            # 404 落 .miss 负缓存（上游补图后 1h 内自动恢复）；须在临时目录清理前断言
            miss_written = (Path(td) / "rogue_6_relic_final_1.webp.miss").exists()
        self.assertEqual(result, "")
        self.assertNotIn("legacy", seen)
        self.assertIn("@main/", seen.get("url", ""))
        self.assertTrue(seen.get("kw", {}).get("quiet_404"))
        self.assertTrue(miss_written)


class DownloadBytesQuiet404Test(unittest.IsolatedAsyncioTestCase):
    """download_bytes_status(quiet_404=True)：404 降 debug；默认行为不变。"""

    def _patch_client(self):
        from unittest import mock

        from ArknightsUID.utils import http as http_mod

        class FakeResp:
            status_code = 404

        class FakeClient:
            async def get(self, url, **kw):
                return FakeResp()

        async def fake_get_client():
            return FakeClient()

        return mock.patch.object(http_mod, "get_client", fake_get_client)

    async def test_quiet_404_logs_debug_not_warning(self) -> None:
        from ArknightsUID.utils import http as http_mod

        with self._patch_client():
            with self.assertNoLogs(http_mod.logger.name, level="WARNING"):
                status, raw = await http_mod.download_bytes_status(
                    "https://example.com/x.webp", quiet_404=True
                )
        self.assertEqual((status, raw), (404, None))

    async def test_default_404_still_warns(self) -> None:
        from ArknightsUID.utils import http as http_mod

        with self._patch_client():
            with self.assertLogs(http_mod.logger.name, level="WARNING") as logs:
                await http_mod.download_bytes_status("https://example.com/x.webp")
        self.assertTrue(any("404" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
