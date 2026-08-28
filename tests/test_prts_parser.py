from __future__ import annotations

import asyncio
import calendar
import json
import time
import unittest
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ArknightsUID.arknightsuid_wiki import (
    _clip,
    _format_enemy,
    _format_page,
    _format_stage,
)
from ArknightsUID.utils.prts import client as prts_client


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeAsyncClient:
    queue: list[Any] = []
    requests: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((url, kwargs))
        if not self.queue:
            raise AssertionError("unexpected PRTS request")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class PRTSParserTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_root = Path(self.temp_dir.name) / "prts"
        self.cache_root.mkdir()
        self.original_api = prts_client._api
        self.original_cache_path = prts_client.PRTS_CACHE_PATH
        self.original_get_client = prts_client.get_client
        prts_client.PRTS_CACHE_PATH = self.cache_root
        fake_client = FakeAsyncClient()

        async def fake_get_client():
            return fake_client

        prts_client.get_client = fake_get_client  # type: ignore[assignment]
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []
        # PRTS 最小请求间隔在测试中归零（避免每个用例固定 sleep 0.5s）
        self.original_min_interval = prts_client._MIN_REQUEST_INTERVAL
        prts_client._MIN_REQUEST_INTERVAL = 0.0
        prts_client._last_request_at = 0.0

    def tearDown(self) -> None:
        prts_client._api = self.original_api  # type: ignore[assignment]
        prts_client.PRTS_CACHE_PATH = self.original_cache_path
        prts_client.get_client = self.original_get_client  # type: ignore[assignment]
        prts_client._MIN_REQUEST_INTERVAL = self.original_min_interval
        self.temp_dir.cleanup()

    async def test_redirect_target_handles_case_whitespace_alias_and_rejects_normal_page(self) -> None:
        self.assertEqual(
            prts_client._redirect_target(" \n #REDIRECT  [[阿米娅|兔兔]] "),
            "阿米娅",
        )
        self.assertEqual(
            prts_client._redirect_target("#redirect [[龙门币]]"),
            "龙门币",
        )
        self.assertEqual(prts_client._redirect_target("== 标题 ==\n正文"), "")

    async def test_parse_page_follows_redirects_and_can_return_redirect_page(self) -> None:
        calls: list[str] = []
        payloads = {
            "兔兔": {
                "parse": {
                    "title": "兔兔",
                    "wikitext": {"*": "#REDIRECT [[阿米娅|Amiya]]"},
                }
            },
            "阿米娅": {
                "parse": {
                    "title": "阿米娅",
                    "wikitext": {"*": "罗德岛公开领导人。"},
                }
            },
        }

        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            calls.append(str(params["page"]))
            return payloads[str(params["page"])]

        prts_client._api = fake_api  # type: ignore[assignment]

        followed = await prts_client.parse_page("兔兔")
        not_followed = await prts_client.parse_page("兔兔", follow_redirect=False)

        self.assertEqual(followed, prts_client.PageWikitext("阿米娅", "罗德岛公开领导人。"))
        self.assertEqual(not_followed, prts_client.PageWikitext("兔兔", "#REDIRECT [[阿米娅|Amiya]]"))
        self.assertEqual(calls, ["兔兔", "阿米娅", "兔兔"])

    async def test_parse_page_handles_empty_title_missing_wikitext_and_redirect_loop(self) -> None:
        self.assertIsNone(await prts_client.parse_page("  "))

        async def missing_wikitext(
            params: dict[str, Any],
            *,
            cache_seconds: int = 86400,
        ) -> dict[str, Any]:
            return {"parse": {"title": str(params["page"])}}

        prts_client._api = missing_wikitext  # type: ignore[assignment]
        self.assertIsNone(await prts_client.parse_page("缺失页面"))

        async def loop_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            page = str(params["page"])
            target = "B" if page == "A" else "A"
            return {"parse": {"title": page, "wikitext": {"*": f"#REDIRECT [[{target}]]"}}}

        prts_client._api = loop_api  # type: ignore[assignment]
        self.assertIsNone(await prts_client.parse_page("A"))

    async def test_clean_wiki_value_removes_html_templates_links_and_whitespace_noise(self) -> None:
        raw = """
        '''阿米娅'''&nbsp;&amp; <span class="x">罗德岛</span><br/>
        [[干员/阿米娅|领导人]] {{颜色|蓝|文本}} [https://example.invalid 来源]
        <!-- hidden --><references/>
        <div>公开资料</div>
        """

        cleaned = prts_client._clean_wiki_value(raw)

        self.assertIn("阿米娅 & 罗德岛", cleaned)
        self.assertIn("领导人 文本 来源", cleaned)
        self.assertIn("公开资料", cleaned)
        for marker in ("<span", "<div", "{{", "}}", "[[", "]]", "hidden", "references"):
            self.assertNotIn(marker, cleaned)
        self.assertNotIn("  ", cleaned)

    async def test_formatters_clip_long_text_and_keep_source_links(self) -> None:
        long_summary = "摘要" * 500
        long_field = "字段" * 200
        page_reply = _format_page(
            "词条",
            prts_client.WikiPageSummary(
                title="阿米娅",
                source_url=prts_client.page_url("阿米娅"),
                summary=long_summary,
                fields={"名称": long_field, "描述": long_field},
            ),
        )
        self.assertIn("...", page_reply)
        self.assertIn("来源：https://prts.wiki/w/", page_reply)
        self.assertLess(len(page_reply.splitlines()[1]), len(long_summary))

        enemy_reply = _format_enemy(
            prts_client.EnemyInfo(
                title="源石虫",
                name="源石虫",
                description="描述" * 200,
                levels=[
                    prts_client.EnemyLevel(
                        max_hp="1000",
                        abilities=["能力" * 200],
                    )
                ],
                source_url=prts_client.page_url("源石虫"),
            )
        )
        self.assertIn("...", enemy_reply)
        self.assertIn("来源：https://prts.wiki/w/", enemy_reply)

        stage_reply = _format_stage(
            prts_client.StageInfo(
                title="1-7",
                code="1-7",
                name="暴君",
                description="关卡描述" * 200,
                drops=[f"掉落{i}" for i in range(12)],
                extra_drops=[f"额外{i}" for i in range(12)],
                enemies=[
                    prts_client.StageEnemy(name=f"敌人{i}", count=str(i))
                    for i in range(12)
                ],
                source_url=prts_client.page_url("1-7"),
                map_url=prts_client.map_url("main_01-07"),
            )
        )
        self.assertIn("...", stage_reply)
        self.assertIn("地图：https://map.ark-nights.com/?map=main_01-07", stage_reply)
        self.assertIn("来源：https://prts.wiki/w/", stage_reply)
        self.assertNotIn("掉落8", stage_reply)
        self.assertNotIn("敌人10", stage_reply)

    async def test_formatters_return_safe_fallbacks_for_missing_fields(self) -> None:
        page_reply = _format_page(
            "词条",
            prts_client.WikiPageSummary(title="未知页", source_url="https://example.invalid/source"),
        )
        self.assertIn("未能解析到摘要字段", page_reply)
        self.assertIn("来源：https://example.invalid/source", page_reply)

        enemy_reply = _format_enemy(
            prts_client.EnemyInfo(title="未知敌人", source_url="https://example.invalid/enemy")
        )
        self.assertIn("「明日方舟」敌人：未知敌人", enemy_reply)
        self.assertIn("来源：https://example.invalid/enemy", enemy_reply)

        stage_reply = _format_stage(
            prts_client.StageInfo(title="未知关卡", source_url="https://example.invalid/stage")
        )
        self.assertIn("「明日方舟」关卡：未知关卡", stage_reply)
        self.assertIn("来源：https://example.invalid/stage", stage_reply)

    async def test_api_stale_cache_fallback_uses_temp_cache_only(self) -> None:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": "../阿米娅",
            "srlimit": 5,
        }
        payload = {"query": {"search": [{"title": "阿米娅"}]}}
        cache_file = prts_client._cache_file(params)
        cache_file.write_text(
            json.dumps({"_cache_time": time.time() - 999999, "payload": payload}),
            encoding="utf-8",
        )
        FakeAsyncClient.queue = [RuntimeError("network unavailable")]

        result = await prts_client._api(params, cache_seconds=0)

        self.assertEqual(result, payload)
        self.assertEqual(cache_file.parent, self.cache_root)
        self.assertNotIn("..", cache_file.name)
        self.assertEqual(len(FakeAsyncClient.requests), 1)

    # === 干员富解析 + 突袭差异 + query_operator（fixture/monkeypatch 驱动）===

    def _fixture(self, name: str) -> str:
        return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")

    async def test_parse_operator_extracts_skills_talents_mastery(self) -> None:
        op = prts_client.parse_operator_wikitext(
            prts_client.PageWikitext(title="银灰", wikitext=self._fixture("silverash.wikitext"))
        )
        self.assertIsNotNone(op)
        assert op is not None
        # meta（rarity 在 PRTS 存 0-5，5 = 6 星）
        self.assertEqual(op.name, "银灰")
        self.assertEqual(op.rarity, "5")
        self.assertEqual(op.profession, "近卫")
        self.assertEqual(op.branch, "领主")
        self.assertIn("谢拉格", op.faction)
        # 3 技能，每个 10 级（1-7 + 专1-3）+ 3 组专精材料
        self.assertEqual(len(op.skills), 3)
        for skill in op.skills:
            self.assertEqual(len(skill.levels), 10)
            self.assertEqual(len(skill.mastery_costs), 3)
        # 真银斩逐级数值
        skill3 = op.skills[2]
        self.assertEqual(skill3.name, "真银斩")
        self.assertEqual(skill3.levels[0].sp_cost, "90")
        self.assertEqual(skill3.levels[0].duration, "20")
        self.assertEqual(skill3.levels[6].duration, "26")  # L7
        self.assertEqual(skill3.levels[7].duration, "27")  # 专1
        # 专3 材料含双极纳米片
        mastery3 = skill3.mastery_costs[2]
        self.assertEqual(mastery3.mastery, "专3")
        self.assertIn("双极纳米片", " ".join(mastery3.materials))
        # 2 天赋
        self.assertEqual(len(op.talents), 2)
        self.assertEqual(op.talents[1].name, "鹰眼视觉")
        # 天赋逐阶段（精英1/精英2/模组级），效果带具体数值
        first = op.talents[0]
        self.assertEqual(first.name, "领袖")
        self.assertEqual(len(first.stages), 4)
        self.assertEqual(first.stages[0].condition, "精英1")
        self.assertIn("攻击力+5%", first.stages[0].effect)
        self.assertIn("攻击力+10%", first.stages[1].effect)
        self.assertEqual(first.stages[3].condition, "精英2 X模组3级")
        self.assertIn("攻击力+25%", first.stages[3].effect)
        # 满潜强化：取最高档（潜能增强_天赋4，X模组3级 + 潜能加成标注）
        self.assertIsNotNone(first.potential)
        assert first.potential is not None
        self.assertIn("攻击力+27%", first.potential.effect)
        self.assertIn("（+2%）", first.potential.effect)
        self.assertIsNone(op.talents[1].potential)

    async def test_build_assault_diffs_filters_noise_and_computes_deltas(self) -> None:
        # 构造普通/突袭字段：核心机制有数值差，噪音/失效字段存在，情报在描述
        normal = {
            "部署上限": "8", "初始COST": "10", "目标点耐久": "3",
            "部署费用": "18→20",
            "演习消耗": "1", "关卡难度": "NORMAL", "推荐等级": "LV.10",
        }
        assault = {
            "部署上限": "6", "初始COST": "12", "目标点耐久": "3",
            "部署费用": "20→22",
            "演习消耗": "3", "关卡难度": "FOUR_STAR", "推荐等级": "-",
            "情报": "敌方生命/攻击/防御提升至120%",
        }
        diffs = prts_client._build_assault_diffs(normal, assault)
        # 核心数值差异 → 算差值
        self.assertTrue(any("部署上限" in d and "8→6" in d and "-2" in d for d in diffs))
        self.assertTrue(any("初始COST" in d and "10→12" in d and "+2" in d for d in diffs))
        # 值自带→（部署费用 18→20）：避免 18→20→20→22 双箭头
        cost = [d for d in diffs if "部署费用" in d]
        self.assertEqual(len(cost), 1)
        self.assertIn("18→20，突袭改为 20→22", cost[0])
        # 失效/噪音字段 → 过滤（演习消耗已失效、关卡难度/推荐等级是描述噪音）
        self.assertFalse(any("演习消耗" in d for d in diffs))
        self.assertFalse(any("关卡难度" in d for d in diffs))
        self.assertFalse(any("推荐等级" in d for d in diffs))
        # 相同字段（目标点耐久）→ 不列
        self.assertFalse(any("目标点耐久" in d for d in diffs))
        # 情报 → 走 assault_info，不在此重复
        self.assertFalse(any("情报" in d for d in diffs))

    async def test_parse_stage_assault_info_carries_enemy_buff(self) -> None:
        # 1-7 突袭差异是"敌人属性提升至120%"，在情报描述里，非 COST/部署上限等数值变化
        stage = prts_client.parse_stage_wikitext(
            prts_client.PageWikitext(
                title="1-7 暴君", wikitext=self._fixture("stage_1-7.wikitext")
            )
        )
        self.assertIsNotNone(stage)
        assert stage is not None
        self.assertEqual(stage.code, "1-7")
        # 突袭差异由 assault_info（情报）承载
        self.assertTrue(stage.assault_info)
        self.assertIn("120", stage.assault_info)
        # 1-7 无数值机制差异，assault_diffs 可为空；assault_fields 保留突袭模板全字段
        self.assertTrue(stage.assault_fields)

    async def test_material_to_text_extracts_name_and_count(self) -> None:
        # 单个材料消耗模板
        self.assertEqual(
            prts_client._material_to_text("{{材料消耗|龙门币|3w}}"),
            ["龙门币 ×3w"],
        )
        # 多个材料（空格分隔）
        self.assertEqual(
            prts_client._material_to_text(
                "{{材料消耗|技巧概要·卷1|5}} {{材料消耗|酯原料|6}}"
            ),
            ["技巧概要·卷1 ×5", "酯原料 ×6"],
        )
        # 空值
        self.assertEqual(prts_client._material_to_text(""), [])
        # 兜底：无材料消耗模板时走通用拆分
        self.assertEqual(
            prts_client._material_to_text("材料A，材料B"), ["材料A", "材料B"]
        )

    async def test_query_operator_falls_back_to_search_when_direct_miss(self) -> None:
        minimal_op = (
            "{{CharinfoV2\n|干员名=银灰\n|稀有度=5\n|职业=近卫\n}}\n"
            "{{技能2\n|技能名=技能一\n|技能1描述=描述\n|技能1消耗=10\n}}"
        )

        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            if params.get("action") == "query":
                return {"query": {"search": [{"title": "银灰"}]}}
            page = params.get("page")
            if page == "银灰简写":
                return {"parse": {"title": "银灰简写", "wikitext": {"*": "不是干员页"}}}
            if page == "银灰":
                return {"parse": {"title": "银灰", "wikitext": {"*": minimal_op}}}
            return {}

        prts_client._api = fake_api  # type: ignore[assignment]

        op = await prts_client.query_operator("银灰简写")
        self.assertIsNotNone(op)
        assert op is not None
        self.assertEqual(op.name, "银灰")
        self.assertEqual(len(op.skills), 1)

    async def test_query_operator_returns_none_for_non_operator_page(self) -> None:
        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            if params.get("action") == "query":
                return {"query": {"search": []}}
            return {"parse": {"title": "x", "wikitext": {"*": "普通词条，无 CharinfoV2"}}}

        prts_client._api = fake_api  # type: ignore[assignment]
        self.assertIsNone(await prts_client.query_operator("不存在的干员"))

    async def test_parse_operator_base_skill_ignores_grade_key(self) -> None:
        # 黑角型页面：后勤技能模板含 等级 修饰键，不得当成技能名
        wikitext = (
            "{{CharinfoV2\n|干员名=黑角\n|稀有度=1\n|职业=重装\n}}\n"
            "{{后勤技能\n"
            "|后勤技能1-1=仓库整备·β\n|后勤技能1-1阶段=精英0\n"
            "|后勤技能2-1=订单管理·α\n|后勤技能2-1阶段=精英0\n|后勤技能2-1等级=30级\n"
            "}}"
        )
        op = prts_client.parse_operator_wikitext(
            prts_client.PageWikitext(title="黑角", wikitext=wikitext)
        )

        self.assertIsNotNone(op)
        assert op is not None
        self.assertEqual(len(op.base_skills), 2)
        self.assertEqual(op.base_skills[0].name, "仓库整备·β")
        self.assertEqual(op.base_skills[0].unlock, "精英0")
        # 等级并入 unlock，"30级" 不产生第三个技能条目
        self.assertEqual(op.base_skills[1].name, "订单管理·α")
        self.assertEqual(op.base_skills[1].unlock, "精英0 30级")
        self.assertNotIn("30级", [b.name for b in op.base_skills])

    async def test_query_base_skill_cleans_cargo_description(self) -> None:
        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            self.assertEqual(params["action"], "cargoquery")
            self.assertEqual(params["tables"], "building_skill2")
            self.assertIn("测试技能", params["where"])
            return {
                "cargoquery": [
                    {
                        "title": {
                            "name": "测试技能",
                            "room": "贸易站",
                            "icon": "bskill_tra_spd",
                            "description": (
                                "进驻贸易站时，订单获取效率"
                                '<span style="color:#0098DC;">+15%</span>'
                            ),
                        }
                    }
                ]
            }

        prts_client._api = fake_api  # type: ignore[assignment]

        effect = await prts_client.query_base_skill("测试技能")

        self.assertEqual(effect, ("贸易站", "进驻贸易站时，订单获取效率+15%"))

    async def test_query_base_skill_handles_missing_and_empty(self) -> None:
        async def empty_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            return {"cargoquery": []}

        prts_client._api = empty_api  # type: ignore[assignment]
        self.assertIsNone(await prts_client.query_base_skill("无此技能"))

        async def none_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any] | None:
            return None

        prts_client._api = none_api  # type: ignore[assignment]
        self.assertIsNone(await prts_client.query_base_skill("网络失败"))
        self.assertIsNone(await prts_client.query_base_skill("  "))

    async def test_query_operator_fills_base_skill_effects(self) -> None:
        operator_wikitext = (
            "{{CharinfoV2\n|干员名=银灰\n|稀有度=5\n|职业=近卫\n}}\n"
            "{{后勤技能\n|后勤技能1-1=喀兰贸易·α\n|后勤技能1-1阶段=精英0\n"
            "|后勤技能1-2=喀兰之主\n|后勤技能1-2阶段=精英2\n}}\n"
            "{{技能2\n|技能名=技能一\n|技能1描述=描述\n}}"
        )

        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            if params.get("action") == "cargoquery":
                name = params["where"].split("='")[1].split("'")[0]
                desc = {
                    "喀兰贸易·α": "进驻贸易站时，订单获取效率+15%",
                    "喀兰之主": "进驻贸易站时，订单获取效率+20%",
                }[name]
                return {"cargoquery": [{"title": {"name": name, "room": "贸易站", "description": desc}}]}
            return {"parse": {"title": "银灰", "wikitext": {"*": operator_wikitext}}}

        prts_client._api = fake_api  # type: ignore[assignment]

        op = await prts_client.query_operator("银灰")

        self.assertIsNotNone(op)
        assert op is not None
        self.assertEqual(len(op.base_skills), 2)
        self.assertEqual(op.base_skills[0].room, "贸易站")
        self.assertIn("+15%", op.base_skills[0].description)
        self.assertEqual(op.base_skills[1].unlock, "精英2")
        self.assertIn("+20%", op.base_skills[1].description)

    # === 活动日历（SMW ask + Gameinfo LST）===

    def test_parse_smw_raw_date_beijing_epoch(self) -> None:
        text, ts = prts_client._parse_smw_raw_date("1/2026/8/14/19/59/0/0")
        self.assertEqual(text, "2026-08-14 19:59")
        # 北京时间 19:59 = UTC 11:59，epoch 用 timegm 动态核算
        expected = calendar.timegm((2026, 8, 14, 11, 59, 0, 0, 0))
        self.assertAlmostEqual(ts, float(expected), delta=1)
        # 非法格式
        self.assertEqual(prts_client._parse_smw_raw_date("garbage"), ("", 0.0))
        self.assertEqual(prts_client._parse_smw_raw_date(""), ("", 0.0))

    def test_parse_lst_sections_extracts_labeled_values(self) -> None:
        wikitext = (
            "*当前版本：<section class=\"lst\" begin=cnver />2.6.01"
            "<section class=\"lst\" end=cnver />"
            "*开始：<section class=\"lst\" begin=cnmaintstart />2026-08-20 10:00"
            " <section class=\"lst\" end=cnmaintstart />"
            "*空值：<section class=\"lst\" begin=empty />  "
            "<section class=\"lst\" end=empty />"
        )
        sections = prts_client.parse_lst_sections(wikitext)
        self.assertEqual(sections["cnver"], "2.6.01")
        self.assertEqual(sections["cnmaintstart"], "2026-08-20 10:00")
        self.assertNotIn("empty", sections)

    async def test_query_activities_parses_smw_ask_response(self) -> None:
        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            self.assertEqual(params["action"], "ask")
            self.assertIn("分类:有活动信息的页面", params["query"])
            return {
                "query": {
                    "results": {
                        "活动A": {
                            "printouts": {
                                "活动开始时间": [{"raw": "1/2099/8/1/4/0/0/0"}],
                                "活动结束时间": [{"raw": "1/2099/8/21/19/59/0/0"}],
                            }
                        },
                        "已结束活动": {
                            "printouts": {
                                "活动开始时间": [{"raw": "1/2020/1/1/4/0/0/0"}],
                                "活动结束时间": [{"raw": "1/2020/1/7/19/59/0/0"}],
                            }
                        },
                        "无结束时间": {"printouts": {"活动开始时间": []}},
                    }
                }
            }

        prts_client._api = fake_api  # type: ignore[assignment]

        # 注入固定时钟（2099-08-10）：夹具开闭时间相对该点在未来，
        # 测试不随真实日期漂移（防时间炸弹）
        fixed_now = datetime(2099, 8, 10, 12, 0, tzinfo=prts_client._BEIJING_TZ).timestamp()
        activities = await prts_client.query_activities(now_ts=fixed_now)

        # 已结束（2020）与无结束时间条目被剔除，仅剩未来活动
        self.assertEqual(len(activities), 1)
        act = activities[0]
        self.assertEqual(act.title, "活动A")
        self.assertEqual(act.start, "2099-08-01 04:00")
        self.assertEqual(act.end, "2099-08-21 19:59")
        self.assertGreater(act.end_ts, act.start_ts)

    async def test_query_gacha_pools_uses_pool_name(self) -> None:
        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            self.assertIn("国服寻访", params["query"])
            return {
                "query": {
                    "results": {
                        # SMW 结果键即卡池横幅文件页（真实结构，探测验证）
                        "文件:·深水 Nile.png": {
                            "printouts": {
                                "卡池名": [{"raw": "寻访模拟/·深水 Nile"}],
                                "寻访开启时间cn": [{"raw": "1/2099/8/7/4/0/0/0"}],
                                "寻访关闭时间cn": [{"raw": "1/2099/8/21/3/59/0/0"}],
                            }
                        }
                    }
                }
            }

        prts_client._api = fake_api  # type: ignore[assignment]

        # 同 test_query_activities：固定时钟，卡池未到期恒成立
        fixed_now = datetime(2099, 8, 10, 12, 0, tzinfo=prts_client._BEIJING_TZ).timestamp()
        pools = await prts_client.query_gacha_pools(now_ts=fixed_now)

        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0].title, "·深水 Nile")
        self.assertEqual(pools[0].end, "2099-08-21 03:59")
        # 横幅直链：文件名 md5 散列路径（map_file_url 同规则）
        digest = __import__("hashlib").md5("·深水 Nile.png".encode()).hexdigest()
        self.assertIn(f"/{digest[0]}/{digest[:2]}/", pools[0].image_url)
        self.assertTrue(pools[0].image_url.endswith("%C2%B7%E6%B7%B1%E6%B0%B4%20Nile.png"))

    async def test_query_game_info_merges_lst_pages(self) -> None:
        pages = {
            "PRTS:Gameinfo/国服/基础": (
                "<section class=\"lst\" begin=cnver />2.6.01"
                "<section class=\"lst\" end=cnver />"
            ),
            "PRTS:Gameinfo/国服/服务器维护": (
                "<section class=\"lst\" begin=cnmaintstart />2026-08-20 10:00"
                "<section class=\"lst\" end=cnmaintstart />"
            ),
        }

        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            page = str(params.get("page") or "")
            return {"parse": {"title": page, "wikitext": {"*": pages[page]}}}

        prts_client._api = fake_api  # type: ignore[assignment]

        info = await prts_client.query_game_info()

        self.assertEqual(info["cnver"], "2.6.01")
        self.assertEqual(info["cnmaintstart"], "2026-08-20 10:00")


class StageMapFileTest(unittest.IsolatedAsyncioTestCase):
    """关卡地图：PRTS 文件命名 {代号}_{关卡名}_地图.png + imageinfo 直链解析。"""

    def test_map_file_title(self) -> None:
        self.assertEqual(prts_client.map_file_title("1-7", "暴君"), "1-7_暴君_地图.png")
        self.assertEqual(prts_client.map_file_title("TR-10", "小队防御"), "TR-10_小队防御_地图.png")
        # 缺代号/关卡名（活动关等）不构造文件名
        self.assertEqual(prts_client.map_file_title("", "暴君"), "")
        self.assertEqual(prts_client.map_file_title("CW-1", ""), "")

    def test_resolve_map_file_url_found(self) -> None:
        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            return {
                "query": {
                    "pages": {
                        "3515": {
                            "title": "文件:1-7 暴君 地图.png",
                            "imagerepository": "local",
                            "imageinfo": [{"url": "https://media.prts.wiki/7/74/1-7.png"}],
                        }
                    }
                }
            }

        orig = prts_client._api
        prts_client._api = fake_api  # type: ignore[assignment]
        try:
            url = asyncio.run(prts_client.resolve_map_file_url("1-7", "暴君"))
        finally:
            prts_client._api = orig
        self.assertEqual(url, "https://media.prts.wiki/7/74/1-7.png")

    def test_resolve_map_file_url_missing_degrades(self) -> None:
        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            # 文件不存在：page 带 missing 标记、无 imageinfo
            return {"query": {"pages": {"-1": {"title": "文件:CW-1 地图.png", "missing": ""}}}}

        orig = prts_client._api
        prts_client._api = fake_api  # type: ignore[assignment]
        try:
            url = asyncio.run(prts_client.resolve_map_file_url("CW-1", ""))
            url_found = asyncio.run(prts_client.resolve_map_file_url("CW-1", "编号"))
        finally:
            prts_client._api = orig
        self.assertEqual(url, "")  # 无文件名直接空
        self.assertEqual(url_found, "")  # 两种命名均查无文件，降级空串

    def test_resolve_map_file_url_assist_fallback(self) -> None:
        """JT 插曲关只有 辅助地图 命名：主命名 miss 后回退辅助命名。"""

        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            title = params["titles"]
            if "辅助地图" in title:
                return {
                    "query": {
                        "pages": {
                            "9": {
                                "title": f"File:{title[5:]}",
                                "imageinfo": [{"url": "https://media.prts.wiki/x/assist.png"}],
                            }
                        }
                    }
                }
            return {"query": {"pages": {"-1": {"title": title, "missing": ""}}}}

        orig = prts_client._api
        prts_client._api = fake_api  # type: ignore[assignment]
        try:
            url = asyncio.run(prts_client.resolve_map_file_url("JT8-3", "昂首，足践烈焰"))
        finally:
            prts_client._api = orig
        self.assertEqual(url, "https://media.prts.wiki/x/assist.png")

    def test_rogue_map_file_urls_variants(self) -> None:
        """肉鸽地图：标准命名 + _1..3 变体都尝试，命中的按序返回。"""

        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            title = params["titles"]
            if "地图_" in title:  # 变体命名存在
                idx = title[5:].split("_")[-1].split(".")[0]
                return {
                    "query": {
                        "pages": {
                            f"1{idx}": {
                                "title": f"文件:{title[5:]}",
                                "imageinfo": [{"url": f"https://media.prts.wiki/m/{idx}.png"}],
                            }
                        }
                    }
                }
            return {"query": {"pages": {"-1": {"title": title, "missing": ""}}}}

        orig = prts_client._api
        prts_client._api = fake_api  # type: ignore[assignment]
        try:
            urls = asyncio.run(prts_client.rogue_map_file_urls("ISW-DF", "不容拒绝"))
        finally:
            prts_client._api = orig
        self.assertEqual(
            urls,
            [
                "https://media.prts.wiki/m/1.png",
                "https://media.prts.wiki/m/2.png",
                "https://media.prts.wiki/m/3.png",
            ],
        )

    def test_rogue_map_preview_urls_derivation(self) -> None:
        """tomimi level_id → torappu 预览直链推导（层关加 n 前缀，分歧/变体直映射）。"""
        cases = {
            "level_rogue4_2-2": "https://torappu.prts.wiki/assets/map_preview/ro4_n_2_2.png",
            "level_rogue4_b-8": "https://torappu.prts.wiki/assets/map_preview/ro4_b_8.png",
            "level_rogue5_b-9-c": "https://torappu.prts.wiki/assets/map_preview/ro5_b_9_c.png",
        }
        for level_id, url in cases.items():
            self.assertEqual(prts_client.rogue_map_preview_urls(level_id), [url])
        self.assertEqual(prts_client.rogue_map_preview_urls("main_01-07"), [])
        self.assertEqual(prts_client.rogue_map_preview_urls(""), [])

    def test_map_file_url_md5_pattern(self) -> None:
        """地图直链 md5 散列路径（对照真实下载地址锁定，免 API 查询）。"""
        cases = {
            "1-7_暴君_地图.png": "https://media.prts.wiki/7/74/1-7_%E6%9A%B4%E5%90%9B_%E5%9C%B0%E5%9B%BE.png",
            "JT8-3_昂首，足践烈焰_辅助地图.png": (
                "https://media.prts.wiki/5/50/JT8-3_%E6%98%82%E9%A6%96%EF%BC%8C"
                "%E8%B6%B3%E8%B7%B5%E7%83%88%E7%84%B0_%E8%BE%85%E5%8A%A9%E5%9C%B0%E5%9B%BE.png"
            ),
        }
        for title, url in cases.items():
            self.assertEqual(prts_client.map_file_url(title), url)
        self.assertEqual(prts_client.map_file_url(""), "")
        self.assertEqual(
            prts_client.map_candidate_titles("JT8-3", "昂首，足践烈焰"),
            ["JT8-3_昂首，足践烈焰_地图.png", "JT8-3_昂首，足践烈焰_辅助地图.png"],
        )
        self.assertEqual(prts_client.map_candidate_titles("", "暴君"), [])

    def test_activity_banner_urls_batch(self) -> None:
        """活动标题图：revisions 提信息框文件名 + imageinfo 换直链；无信息框降级。"""

        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            if params.get("prop") == "revisions":
                return {
                    "query": {
                        "pages": {
                            "1": {
                                "title": "沉沦者的黑流树海",
                                "revisions": [
                                    {
                                        "slots": {
                                            "main": {
                                                "*": "{{活动信息\n|类型=集成战略\n|标题图文件名=主题图 黑流.png\n}}"
                                            }
                                        }
                                    }
                                ],
                            },
                            "2": {
                                "title": "无图活动",
                                "revisions": [
                                    {"slots": {"main": {"*": "{{活动信息\n|类型=_side_story_\n}}"}}}
                                ],
                            },
                        }
                    }
                }
            if params.get("prop") == "imageinfo":
                # PRTS 会把 File: 规范化为中文命名空间 文件:
                return {
                    "query": {
                        "pages": {
                            "3": {
                                "title": "文件:主题图 黑流.png",
                                "imageinfo": [{"url": "https://media.prts.wiki/b/banner.png"}],
                            }
                        }
                    }
                }
            raise AssertionError(f"未预期的请求: {params}")

        orig = prts_client._api
        prts_client._api = fake_api  # type: ignore[assignment]
        try:
            urls = asyncio.run(
                prts_client.activity_banner_urls(["沉沦者的黑流树海", "无图活动"])
            )
        finally:
            prts_client._api = orig
        self.assertEqual(
            urls, {"沉沦者的黑流树海": "https://media.prts.wiki/b/banner.png"}
        )


class InlineValueTemplateTest(unittest.TestCase):
    """机制文本的内嵌数值模板必须展开为纯文本（数值不丢）。

    实测 爱国者 等级2 差异块：{{变动数值|5%|-}} 曾被通用"取末段"规则
    吃成 "-"，等级 2 的献祭伤害数值整体丢失。
    """

    def test_value_templates_keep_numbers(self) -> None:
        cases = {
            "{{变动数值|5%|-}}": "5%↓",
            "{{变动数值|750|+}}": "750↑",
            "{{变动数值lite|down|蓝|16}}": "↓16",
            "{{变动数值lite|up|蓝|+20%}}": "↑+20%",
            "{{变动数值lite||橙|击倒敌人时获得1点技力}}": "击倒敌人时获得1点技力",
            "{{异常效果|眩晕|免疫}}": "眩晕免疫",
            "{{异常效果|不可阻挡}}": "不可阻挡",
            "{{color|#FF4F0B|行军姿态}}": "行军姿态",
        }
        for raw, want in cases.items():
            self.assertEqual(prts_client._clean_wiki_value(raw), want, raw)

    def test_level2_ability_keeps_changed_value(self) -> None:
        """等级2 差异块技能效果中的变动数值展开后进机制条目。"""
        wikitext = """
{{敌人信息/common2|名称=测试敌人}}
{{敌人信息/levelcontent
|index=0
|技能0=献祭
|技能0效果=造成相当于攻击力5.21%的真实伤害
|技能0初始=9999
|技能0消耗=9999
}}
{{敌人信息/levelcontent
|index=1
|最大生命值=60000
|技能0效果=造成相当于攻击力{{变动数值|5%|-}}的真实伤害
}}
"""
        enemy = prts_client.parse_enemy_wikitext(
            prts_client.PageWikitext(title="测试敌人", wikitext=wikitext)
        )
        self.assertIsNotNone(enemy)
        assert enemy is not None
        lv2_text = "\n".join(enemy.levels[1].abilities)
        self.assertIn("攻击力5%↓", lv2_text)
        self.assertNotIn("{{", lv2_text)

    def test_level2_orphan_sp_and_effect_aggregate(self) -> None:
        """等级2 差异块孤儿字段聚合（实测浮士德/霜星页行为）。

        - 孤儿效果 + 本块 SP → 「技能·名：新效果（初始X·消耗Y）」，SP 不丢
        - 仅 SP 变化（无名无效果）→ 「技能·名（初始X·消耗Y）」
        - 不再泄漏「技能N初始：X」裸字段行
        """
        wikitext = """
{{敌人信息/common2|名称=测试敌人}}
{{敌人信息/levelcontent
|index=0
|技能0=致命一击
|技能0效果=造成攻击力200%的物理伤害
|技能0初始=17
|技能0消耗=17
|技能1=召唤弩炮
|技能1效果=在预设位置召唤弩炮
|技能1初始=15
|技能1消耗=30
}}
{{敌人信息/levelcontent
|index=1
|技能0初始=30
|技能0消耗=30
|技能0效果=造成攻击力250%的物理伤害
|技能1初始=1
|技能1消耗=45
|天赋=不可阻挡、沉睡免疫
}}
"""
        enemy = prts_client.parse_enemy_wikitext(
            prts_client.PageWikitext(title="测试敌人", wikitext=wikitext)
        )
        self.assertIsNotNone(enemy)
        assert enemy is not None
        self.assertEqual(
            enemy.levels[1].abilities,
            [
                "技能·致命一击：造成攻击力250%的物理伤害（初始30·消耗30）",
                "技能·召唤弩炮（初始1·消耗45）",
                "天赋：不可阻挡、沉睡免疫",
            ],
        )
        # 完全只改 SP 的差异块（霜星）：单条参数条目，无裸字段行
        wikitext_frost = """
{{敌人信息/common2|名称=测试敌人}}
{{敌人信息/levelcontent
|index=0
|技能1=冻结
|技能1效果=随机冻结2格地面
|技能1初始=30
|技能1消耗=30
}}
{{敌人信息/levelcontent
|index=1
|技能1初始=21
|技能1消耗=21
}}
"""
        enemy = prts_client.parse_enemy_wikitext(
            prts_client.PageWikitext(title="测试敌人", wikitext=wikitext_frost)
        )
        self.assertIsNotNone(enemy)
        assert enemy is not None
        self.assertEqual(enemy.levels[1].abilities, ["技能·冻结（初始21·消耗21）"])

    def test_popup_named_param_template_cleaned(self) -> None:
        """全命名参数模板（{{Popup|内容=…|颜色=…}}）取内容段，不泄漏「颜色=#007DFA」。"""
        cleaned = prts_client._clean_wiki_value(
            "防御力下降50%{{Popup|内容=该效果为[[游戏数据基础#公式|直接乘算]]|颜色=#007DFA}}"
        )
        self.assertIn("直接乘算", cleaned)
        self.assertNotIn("颜色=", cleaned)
        self.assertNotIn("{{", cleaned)

    def test_parse_holo_indicators(self) -> None:
        """全息矩阵指标：分组/分区/锁定/评分标记 + 嵌套 color 数值保留。"""
        wikitext = """
{{普通关卡信息|关卡代号=H5-1|关卡名=炼狱行动-1}}
==矩阵指标一览==
{{测试指标/全息/style}}
!class="group_title"|{{mdi|chess-rook}} 基础：俯冲轰炸
|-
|class="group_content"|
{{测试指标/全息/分组|锁定=yes
|{{测试指标/全息|选中=yes|类型=锁定|r_fixed_rune|我方防御点可承受的敌方数量变为{{color|#c9001c|1}}}}
}}
{{测试指标/全息/分组
|{{测试指标/全息|类型=指定|r_token_build_a|<干扰装置>可以额外被部署在地面上，且每{{color|#c9001c|3}}秒扣除{{color|#c9001c|3}}点部署费用}}
{{测试指标/全息|类型=指定|r_token_build_b|<干扰装置>仅可被部署在高台上}}
}}
!class="group_title"|{{mdi|bullseye-arrow}} 附加条件
|-
|class="group_content"|
{{测试指标/全息|分数=2|r_emppnt_level_up_2|出现额外的<帝国炮火中枢先兆者>，且生命值{{color|#c9001c|+200%}}}}
{{测试指标/全息/分组
|{{测试指标/全息|分数=2|r_a|<豪华赞助无人机>免疫沉睡}}
{{测试指标/全息|分数=2|r_b|<豪华赞助无人机>的攻击力{{color|#c9001c|+500}}}}
}}
"""
        indicators = prts_client.parse_holo_indicators(wikitext)
        self.assertEqual(len(indicators), 6)
        locked = indicators[0]
        self.assertEqual(locked.section, "基础：俯冲轰炸")
        self.assertEqual(locked.kind, "锁定")
        self.assertTrue(locked.grouped)
        self.assertEqual(locked.token, "r_fixed_rune")
        self.assertIn("变为1", locked.text)
        # 嵌套 color 顶层切分不丢段：描述含多个数值
        build_a = indicators[1]
        self.assertEqual(build_a.kind, "指定")
        self.assertTrue(build_a.grouped)
        self.assertIn("每3秒扣除3点部署费用", build_a.text)
        self.assertNotIn("{{", build_a.text)
        # 附加区：裸行（独立）+ 分组行（冲突组）
        extra = indicators[3]
        self.assertEqual(extra.section, "附加条件")
        self.assertEqual(extra.score, "2")
        self.assertFalse(extra.grouped)
        self.assertIn("+200%", extra.text)
        self.assertTrue(indicators[4].grouped)

    def test_stage_env_conditions_extracted(self) -> None:
        """磨难式关卡描述：「环境条件」段拆出，剩余描述不再包含条件行。"""
        wikitext = """
{{普通关卡信息
|关卡代号=10-10
|关卡名=旧日之影
|关卡描述=回忆向你投下阴影。恐惧，仇恨，生死别离，还有你的旧相识。⏎环境条件：⏎渴血的子裔的吸血效果大幅提升，生命值增加
}}
{{敌方情报|敌人1=萨卡兹子裔战士}}
{{合约详情}}
""".replace("⏎", "\n")
        info = prts_client.parse_stage_wikitext(
            prts_client.PageWikitext(title="磨难10-10 旧日之影", wikitext=wikitext)
        )
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(
            info.env_conditions, ["渴血的子裔的吸血效果大幅提升，生命值增加"]
        )
        self.assertNotIn("环境条件", info.description)
        self.assertIn("回忆向你投下阴影", info.description)
        # 同行标签格式
        info2 = prts_client.parse_stage_wikitext(
            prts_client.PageWikitext(
                title="测试", wikitext=wikitext.replace("环境条件：\n", "环境条件：")
            )
        )
        assert info2 is not None
        self.assertEqual(len(info2.env_conditions), 1)
        # 危机合约标记
        self.assertTrue(info.has_contracts)

    def test_talent_markup_fully_cleaned(self) -> None:
        """天赋里的 color/链接/粗体/异常效果/注释全部清理，数值保留。"""
        wikitext = """
{{敌人信息/common2|名称=测试敌人}}
{{敌人信息/levelcontent
|index=0
|天赋=[[源石祭坛|免疫脉冲波]]<br>'''{{color|#FF4F0B|行军姿态}}'''：攻击力+50%<br>{{异常效果|眩晕|免疫}}<!--注释-->,攻击间隔-0.4
}}
"""
        enemy = prts_client.parse_enemy_wikitext(
            prts_client.PageWikitext(title="测试敌人", wikitext=wikitext)
        )
        self.assertIsNotNone(enemy)
        assert enemy is not None
        talent = enemy.levels[0].abilities[0]
        self.assertIn("免疫脉冲波", talent)
        self.assertIn("行军姿态：攻击力+50%", talent)
        self.assertIn("眩晕免疫", talent)
        self.assertIn("攻击间隔-0.4", talent)
        for residue in ("{{", "}}", "[[", "]]", "'''", "<br", "<!--", "注释"):
            self.assertNotIn(residue, talent)


class EnemyLevelInheritTest(unittest.TestCase):
    """等级 2+ 变体块只写差异字段：空数值从上一级继承（实测爱国者页行为）。"""

    WIKITEXT = """
{{敌人信息/common2
|名称=测试敌人
|id=E1
|index=E1
|地位级别=普通
|登场活动=生于黑夜
}}
{{敌人信息/levelcontent
|index=0
|最大生命值=1000
|攻击力=200
|防御力=100
|法术抗性=10
|重量等级=3
|移动速度=1.1
|攻击间隔=2.0
|攻击范围半径=2.5
|生命恢复速度=50
|sp恢复速度=0.2
|元素抗性=0
|基础嘲讽等级=1
|眩晕抗性=无
|沉默抗性=有
|战栗抗性=无
}}
{{敌人信息/levelcontent
|index=1
|最大生命值=2000
|攻击力=300
}}
"""

    def test_level2_inherits_missing_stats(self) -> None:
        enemy = prts_client.parse_enemy_wikitext(
            prts_client.PageWikitext(title="测试敌人", wikitext=self.WIKITEXT)
        )
        self.assertIsNotNone(enemy)
        assert enemy is not None
        self.assertEqual(len(enemy.levels), 2)
        self.assertEqual(enemy.event, "生于黑夜")
        lv1, lv2 = enemy.levels
        # 有差异字段用变体值
        self.assertEqual(lv2.max_hp, "2000")
        self.assertEqual(lv2.atk, "300")
        # 空字段从等级 1 继承（含次级属性与抗性）
        self.assertEqual(lv2.defense, "100")
        self.assertEqual(lv2.res, "10")
        self.assertEqual(lv2.weight, "3")
        self.assertEqual(lv2.move_speed, "1.1")
        self.assertEqual(lv2.attack_interval, "2.0")
        self.assertEqual(lv2.silence_res, "有")

    def test_extended_level_fields_parsed(self) -> None:
        """攻击间隔/范围/恢复/嘲讽等扩展字段 + 生效抗性收集。"""
        enemy = prts_client.parse_enemy_wikitext(
            prts_client.PageWikitext(title="测试敌人", wikitext=self.WIKITEXT)
        )
        self.assertIsNotNone(enemy)
        assert enemy is not None
        lv1 = enemy.levels[0]
        self.assertEqual(lv1.attack_radius, "2.5")
        self.assertEqual(lv1.hp_regen, "50")
        self.assertEqual(lv1.sp_regen, "0.2")
        self.assertEqual(lv1.taunt, "1")
        # 元素抗性 0 原样保留（展示层过滤），解析层不做判断
        self.assertEqual(lv1.ep_res, "0")
        # 生效抗性只收 "有"/非 "无" 项
        self.assertEqual(
            prts_client.level_resistances(lv1), [("沉默", "有")]
        )

    def test_skill_fields_merged_into_readable_abilities(self) -> None:
        """技能N/效果/初始/消耗 重组为单条；等级2 孤儿效果回查技能名。"""
        wikitext = """
{{敌人信息/common2
|名称=测试敌人
}}
{{敌人信息/levelcontent
|index=0
|最大生命值=1000
|技能0=投枪
|技能0效果=锁定目标造成伤害
|技能0初始=15
|技能0消耗=20
|天赋=免疫脉冲波
}}
{{敌人信息/levelcontent
|index=1
|最大生命值=2000
|技能0效果=强化投枪
}}
"""
        enemy = prts_client.parse_enemy_wikitext(
            prts_client.PageWikitext(title="测试敌人", wikitext=wikitext)
        )
        self.assertIsNotNone(enemy)
        assert enemy is not None
        abilities = enemy.levels[0].abilities
        # 技能合并为单条（SP 参数入括号），天赋原样保留
        self.assertEqual(
            abilities, ["技能·投枪：锁定目标造成伤害（初始15·消耗20）", "天赋：免疫脉冲波"]
        )
        # 等级2 差异块孤儿效果回查上一级技能名
        self.assertEqual(enemy.levels[1].abilities, ["技能·投枪：强化投枪"])

    def test_level_select_helpers(self) -> None:
        """final_level / select_owner_level 公共助手（wiki 与练度卡共用）。"""
        levels = [
            prts_client.SkillLevelDetail(level=str(n), description=f"效果+{n}%")
            for n in range(1, 8)
        ] + [
            prts_client.SkillLevelDetail(level=f"专{n}", description=f"效果+{70 + n * 10}%")
            for n in (1, 2, 3)
        ]
        self.assertEqual(prts_client.final_level(levels).level, "专3")
        self.assertEqual(prts_client.select_owner_level(levels, 2, 7).level, "专2")
        self.assertEqual(prts_client.select_owner_level(levels, 0, 4).level, "4")
        self.assertIsNone(prts_client.select_owner_level(levels, 0, 0))


if __name__ == "__main__":
    unittest.main()
