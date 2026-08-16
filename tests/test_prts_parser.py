from __future__ import annotations

import asyncio
import calendar
import json
import time
import unittest
import tempfile
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
        self.original_async_client = prts_client.httpx.AsyncClient
        prts_client.PRTS_CACHE_PATH = self.cache_root
        prts_client.httpx.AsyncClient = FakeAsyncClient
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []

    def tearDown(self) -> None:
        prts_client._api = self.original_api  # type: ignore[assignment]
        prts_client.PRTS_CACHE_PATH = self.original_cache_path
        prts_client.httpx.AsyncClient = self.original_async_client
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

        activities = await prts_client.query_activities()

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
                        "卡池页": {
                            "printouts": {
                                "卡池名": [{"raw": "·深水 Nile"}],
                                "寻访开启时间cn": [{"raw": "1/2026/8/7/4/0/0/0"}],
                                "寻访关闭时间cn": [{"raw": "1/2026/8/21/3/59/0/0"}],
                            }
                        }
                    }
                }
            }

        prts_client._api = fake_api  # type: ignore[assignment]

        pools = await prts_client.query_gacha_pools()

        self.assertEqual(len(pools), 1)
        self.assertEqual(pools[0].title, "·深水 Nile")
        self.assertEqual(pools[0].end, "2026-08-21 03:59")

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
        self.assertEqual(url_found, "")  # 查无文件降级空串


class EnemyLevelInheritTest(unittest.TestCase):
    """等级 2+ 变体块只写差异字段：空数值从上一级继承（实测爱国者页行为）。"""

    WIKITEXT = """
{{敌人信息/common2
|名称=测试敌人
|id=E1
|index=E1
|地位级别=普通
}}
{{敌人信息/levelcontent
|index=0
|最大生命值=1000
|攻击力=200
|防御力=100
|法术抗性=10
|重量等级=3
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
        lv2 = enemy.levels[1]
        # 有差异字段用变体值
        self.assertEqual(lv2.max_hp, "2000")
        self.assertEqual(lv2.atk, "300")
        # 空字段从等级 1 继承
        self.assertEqual(lv2.defense, "100")
        self.assertEqual(lv2.res, "10")
        self.assertEqual(lv2.weight, "3")

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
