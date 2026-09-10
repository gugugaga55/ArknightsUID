"""肉鸽战绩（ark肉鸽战绩）解析器 + 命令辅助 + 帮助分层测试。

数据契约（utils/rogue/records.py 头注释）：响应结构参考社区
nonebot-plugin-skland 的 RogueData schema；topicId 用主题表键 rogue_N。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.rogue.records import (
    TOPIC_IDS,
    format_run_ts,
    parse_rogue_records,
    topic_id_for,
)


async def _fake_banners(*_args, **_kwargs) -> dict:
    """期 KV 拉取打桩（卡片 banner 是可选装饰，测试不打网络）。"""
    return {}


def _records_payload() -> dict:
    """最小同构切片：topics 选中 rogue_6 + history 两项统计 + 两条战绩。"""
    return {
        "topics": [
            {"id": "rogue_5", "isSelected": False, "name": "岁的界园志异", "pic": "p5"},
            {"id": "rogue_6", "isSelected": True, "name": "沉沦者的黑流树海", "pic": "p6"},
        ],
        "gameUserInfo": {"name": "刀客塔", "level": 120, "isOfficial": True},
        "career": {
            "game": 147, "node": 3821, "relic": 932, "invest": 5420,
            "clearInfo": {"difficulty": "标准", "grade": 15,
                          "endings": ["e1", "e2", "e3", "e4"]},
        },
        "history": {
            "medal": {"count": 22, "current": 11},
            "modeGrade": 15,
            "mode": "标准",
            "score": 4318,
            "bpLevel": 155,
            "chars": [
                {"id": "char_002_amiya", "name": "阿米娅", "rarity": 4,
                 "profession": "CASTER", "evolvePhase": 2, "level": 80,
                 "isCandle": False},
                {"id": "", "name": ""},   # 空项应被过滤
                "garbage",                # 非 dict 应被过滤
            ],
            "records": [
                {
                    "id": "run1", "mode": "标准", "modeGrade": 15,
                    "success": 1, "score": 1888,
                    "band": {"id": "rogue_6_band_1", "name": "突击战术分队"},
                    "lastStage": "", "endingText": "大群所向",
                    "startTs": "1770000000", "endTs": "1770003120",
                    "isCollect": False,
                    "cntCrossedZone": 6, "cntBattleNormal": 12,
                    "cntBattleElite": 3, "cntBattleBoss": 1,
                    "cntGainRelicItem": 24, "cntRecruitUpgrade": 9,
                    "cntArrivedNode": 47,
                    "seed": "seed_abc123",
                    "tagList": [{"name": "奇遇记", "icon": "", "id": 1}],
                    "initChars": [
                        {"id": "char_151_myrtle", "name": "桃金娘", "rarity": 3,
                         "profession": "VANGUARD", "evolvePhase": 2, "level": 70},
                    ],
                    "troopChars": [
                        {"id": "char_1035_wisdel", "name": "维什戴尔", "rarity": 5,
                         "profession": "SNIPER", "evolvePhase": 2, "level": 90},
                        {"id": "char_151_myrtle", "name": "桃金娘", "rarity": 3,
                         "profession": "VANGUARD", "evolvePhase": 2, "level": 70},
                    ],
                    "gainRelicList": [
                        {"id": "rogue_5_relic_legacy_55"},
                        "rogue_5_relic_return_12",
                    ],
                    "lastChars": [
                        {"id": "char_1035_wisdel", "name": "维什戴尔", "rarity": 5,
                         "profession": "SNIPER", "evolvePhase": 2, "level": 90},
                    ],
                },
                {
                    "id": "run2", "mode": "标准", "modeGrade": 12,
                    "success": 0, "score": 660,
                    "band": {"id": "rogue_6_band_2", "name": "堡垒战术分队"},
                    "lastStage": "浮士德",
                    "endingText": '[{"content": "你倒在了", "color": ""},{"content": "浮士德", "color": "#d94848"}]',
                    "startTs": "1770000000", "endTs": "1770000600",
                    "isCollect": True,
                    "lastChars": [],
                },
            ],
            "favourRecords": [],
        },
        "itemInfo": {
            "rogue_5_relic_legacy_55": {"name": "钝爪-爆发", "description": "", "usage": ""},
            "rogue_5_relic_return_12": {"name": "教母的信物", "description": "", "usage": ""},
        },
    }


class ParseRogueRecordsTest(unittest.TestCase):
    def test_parse_full_payload(self) -> None:
        stats = parse_rogue_records(_records_payload())
        assert stats is not None
        self.assertEqual(stats.topic_id, "rogue_6")
        self.assertEqual(stats.topic_name, "沉沦者的黑流树海")
        self.assertEqual(stats.player_name, "刀客塔")
        self.assertEqual(stats.player_level, 120)
        self.assertEqual(stats.mode, "标准")
        self.assertEqual(stats.mode_grade, 15)
        self.assertEqual(stats.score, 4318)
        self.assertEqual(stats.bp_level, 155)
        # medal.count=总数 / current=已得（与 player_info 的 total/current 对应）
        self.assertEqual(stats.medal_total, 22)
        self.assertEqual(stats.medal_current, 11)
        # 生涯块
        self.assertEqual(stats.games, 147)
        self.assertEqual(stats.nodes, 3821)
        self.assertEqual(stats.relics, 932)
        self.assertEqual(stats.invest, 5420)
        self.assertEqual(stats.endings_total, 4)
        # 主题列表（切换条）
        self.assertEqual(len(stats.topics), 2)
        self.assertTrue(stats.topics[1]["selected"])
        self.assertFalse(stats.topics[0]["selected"])
        # 空项/非 dict 干员被过滤
        self.assertEqual(len(stats.fav_chars), 1)
        self.assertEqual(stats.fav_chars[0].char_id, "char_002_amiya")
        self.assertEqual(len(stats.records), 2)
        run = stats.records[0]
        self.assertTrue(run.success)
        self.assertEqual(run.band, "突击战术分队")
        self.assertEqual(run.ending_text, "大群所向")
        self.assertEqual(run.chars[0].name, "维什戴尔")
        # 局内明细
        self.assertEqual(run.zones, 6)
        self.assertEqual((run.battle_normal, run.battle_elite, run.battle_boss), (12, 3, 1))
        self.assertEqual(run.relic_count, 24)
        self.assertEqual(run.recruit_count, 9)
        self.assertEqual(run.tags, ["奇遇记"])
        fail = stats.records[1]
        self.assertFalse(fail.success)
        self.assertTrue(fail.is_collect)
        self.assertEqual(fail.last_stage, "浮士德")

    def test_parse_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_rogue_records(None))
        self.assertIsNone(parse_rogue_records({}))
        self.assertIsNone(parse_rogue_records("rogue"))
        # 无 history → 不可展示
        self.assertIsNone(parse_rogue_records({"topics": []}))

    def test_parse_missing_fields_degrade(self) -> None:
        stats = parse_rogue_records({"history": {}})
        assert stats is not None
        self.assertEqual(stats.topic_id, "")
        self.assertEqual(stats.mode_grade, 0)
        self.assertEqual(stats.records, [])

    def test_topic_id_mapping(self) -> None:
        # 主题表从傀影起编（rogue_1=傀影 … rogue_6=黑流树海）
        self.assertEqual(topic_id_for("phantom"), "rogue_1")
        self.assertEqual(topic_id_for("black"), "rogue_6")
        self.assertEqual(len(TOPIC_IDS), 6)
        # 未知目录原样透传（由端点报错），不静默吞掉
        self.assertEqual(topic_id_for("unknown"), "unknown")

    def test_format_run_ts(self) -> None:
        self.assertEqual(format_run_ts(""), "")
        self.assertEqual(format_run_ts(None), "")
        self.assertEqual(format_run_ts("not-a-ts"), "not-a-ts")
        # 秒级 unix 字符串 → MM-DD HH:MM
        self.assertRegex(format_run_ts("1770003120"), r"^\d{2}-\d{2} \d{2}:\d{2}$")


class RecordCommandHelperTest(unittest.TestCase):
    """命令层纯函数：期名解析 / 用时 / 文本格式化（无需 bot）。"""

    def test_resolve_theme(self) -> None:
        from ArknightsUID.arknightsuid_rogue import _resolve_record_theme

        # 空 = 服务端默认期
        self.assertEqual(_resolve_record_theme("", {}), ("", ""))
        self.assertEqual(_resolve_record_theme("  ", {}), ("", ""))
        # 内建别名 → 期目录 → topicId
        self.assertEqual(_resolve_record_theme("界园", {}), ("rogue_5", "岁的界园志异"))
        self.assertEqual(_resolve_record_theme("树海", {}), ("rogue_6", "沉沦者的黑流树海"))
        # 用户别名优先
        self.assertEqual(
            _resolve_record_theme("树海", {"树海": "sarkaz"}),
            ("rogue_4", "萨卡兹的无终奇语"),
        )
        # 不可解析 → 错误文案（含可用期名）
        result = _resolve_record_theme("不存在的期", {})
        self.assertIsInstance(result, str)
        self.assertIn("无法识别", result)

    def test_run_duration(self) -> None:
        from ArknightsUID.arknightsuid_rogue import _run_duration

        self.assertEqual(_run_duration("1770000000", "1770003120"), "用时 52 分钟")
        self.assertEqual(_run_duration("", "1770003120"), "")
        self.assertEqual(_run_duration("1770003120", "1770000000"), "")  # 倒挂
        self.assertEqual(_run_duration("a", "b"), "")

    def test_format_records_text(self) -> None:
        from ArknightsUID.arknightsuid_rogue import _format_rogue_records

        stats = parse_rogue_records(_records_payload())
        assert stats is not None
        text = _format_rogue_records(stats, "沉沦者的黑流树海", "12****89")
        self.assertIn("沉沦者的黑流树海", text)
        self.assertIn("刀客塔", text)
        self.assertIn("12****89", text)
        self.assertIn("最高达成：标准 N15", text)
        self.assertIn("[通关] 标准·N15 | 得分 1888 | 突击战术分队", text)
        self.assertIn("大群所向", text)
        self.assertIn("用时 52 分钟", text)
        self.assertIn("[失败] 标准·N12 | 得分 660", text)
        self.assertIn("你倒在了浮士德", text)  # 结局 JSON 分段优先于止步关卡
        self.assertIn("常用干员：阿米娅", text)
        # 生涯与局内明细
        self.assertIn("生涯：探索 147 次 | 经过节点 3821 | 获得藏品 932 | 结局达成 4", text)
        self.assertIn("探索 6 层", text)
        self.assertIn("战斗 12/3/1", text)
        self.assertIn("藏品 24", text)
        # 主题切换提示（非选中期，短名）
        self.assertIn("切换主题：ark肉鸽战绩 界园", text)

    def test_format_records_empty(self) -> None:
        from ArknightsUID.arknightsuid_rogue import _format_rogue_records

        stats = parse_rogue_records({"history": {"mode": "标准", "modeGrade": 0}})
        assert stats is not None
        text = _format_rogue_records(stats, "", "")
        self.assertIn("暂无战绩记录", text)

    def test_records_context_and_template_contract(self) -> None:
        """context 键与 ark_rogue_record.html 模板变量一一对应。"""
        import asyncio

        from jinja2 import Environment, FileSystemLoader, meta, select_autoescape

        from ArknightsUID.arknightsuid_rogue import _build_rogue_records_context
        from ArknightsUID.utils.path import TEMPLATES_PATH

        stats = parse_rogue_records(_records_payload())
        assert stats is not None
        with mock.patch(
            "ArknightsUID.utils.prts.client.activity_banner_urls",
            _fake_banners,
        ):
            context = asyncio.run(
                _build_rogue_records_context(stats, "沉沦者的黑流树海", "12****89")
            )
        self.assertEqual(context["theme_name"], "沉沦者的黑流树海")
        self.assertEqual(context["medal_current"], 11)
        self.assertEqual(len(context["records"]), 2)
        self.assertEqual(context["records"][0]["duration"], "用时 52 分钟")

        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_PATH)),
            autoescape=select_autoescape(["html", "xml"]),
        )
        source, _, _ = env.loader.get_source(env, "ark_rogue_record.html")
        used = meta.find_undeclared_variables(env.parse(source))
        missing = set(used) - set(context)
        self.assertFalse(missing, f"模板变量缺 context 键: {missing}")

    def test_banner_prefers_skland_topic_pic(self) -> None:
        """横幅优先复用森空岛 topics[].pic；失败/缺失回退 PRTS 标题图。"""
        import asyncio

        from ArknightsUID.arknightsuid_rogue import _build_rogue_records_context

        stats = parse_rogue_records(_records_payload())
        assert stats is not None
        stats.topic_pic = "https://zonai.skland.com/pic/rogue_6.png"

        calls: list[str] = []

        async def fake_b64(url, *args, **kwargs):
            calls.append(url)
            return "data:image/webp;base64,Skland" if "zonai" in url else ""

        async def fake_banners(*_a, **_k):
            return {"沉沦者的黑流树海": "https://media.prts.wiki/prts.png"}

        with (
            mock.patch(
                "ArknightsUID.utils.render.get_image_b64_with_cache",
                fake_b64,
            ),
            mock.patch(
                "ArknightsUID.utils.prts.client.activity_banner_urls",
                fake_banners,
            ),
        ):
            context = asyncio.run(_build_rogue_records_context(stats, "", ""))
        self.assertEqual(context["banner"], "data:image/webp;base64,Skland")
        self.assertEqual(calls, ["https://zonai.skland.com/pic/rogue_6.png"])

        # 森空岛图下载失败 → 回退 PRTS
        calls.clear()

        async def failing_b64(url, *args, **kwargs):
            calls.append(url)
            if "zonai" in url:
                raise RuntimeError("skland pic down")
            return "data:image/webp;base64,PRTS"

        with (
            mock.patch(
                "ArknightsUID.utils.render.get_image_b64_with_cache",
                failing_b64,
            ),
            mock.patch(
                "ArknightsUID.utils.prts.client.activity_banner_urls",
                fake_banners,
            ),
        ):
            context = asyncio.run(_build_rogue_records_context(stats, "", ""))
        self.assertEqual(context["banner"], "data:image/webp;base64,PRTS")
        self.assertEqual(calls, [
            "https://zonai.skland.com/pic/rogue_6.png",
            "https://media.prts.wiki/prts.png",
        ])


class RunDetailTest(unittest.TestCase):
    """ark肉鸽详情：参数解析 / 详情 context / 文本回退。"""

    def test_parse_detail_fields(self) -> None:
        stats = parse_rogue_records(_records_payload())
        assert stats is not None
        run = stats.records[0]
        # 新增明细字段
        self.assertEqual(run.arrived_nodes, 47)
        self.assertEqual(run.seed, "seed_abc123")
        self.assertEqual([c.name for c in run.init_chars], ["桃金娘"])
        self.assertEqual([c.name for c in run.troop_chars], ["维什戴尔", "桃金娘"])
        # gainRelicList 兼容 dict 项与裸字符串两种形态
        self.assertEqual(
            run.relic_ids, ["rogue_5_relic_legacy_55", "rogue_5_relic_return_12"]
        )
        # itemInfo 名字表
        self.assertEqual(stats.item_names.get("rogue_5_relic_legacy_55"), "钝爪-爆发")

    def test_ending_text_json_segments(self) -> None:
        """endingText 是 JSON 分段时：纯文本拼接 + 分段保留配色。"""
        stats = parse_rogue_records(_records_payload())
        assert stats is not None
        run1 = stats.records[0]  # 纯文本结局
        self.assertEqual(run1.ending_text, "大群所向")
        self.assertEqual(run1.ending_segments, [])
        run2 = stats.records[1]  # JSON 分段
        self.assertEqual(run2.ending_text, "你倒在了浮士德")
        self.assertEqual(len(run2.ending_segments), 2)
        self.assertEqual(run2.ending_segments[1]["color"], "#d94848")

    def test_parse_detail_args(self) -> None:
        from ArknightsUID.arknightsuid_rogue import _parse_detail_args

        # 纯局号 → 默认期 + 最近第 N 局
        self.assertEqual(_parse_detail_args("3", {}), ("", "", 3, False))
        # 期名 + 局号
        self.assertEqual(
            _parse_detail_args("界园 2", {}), ("rogue_5", "岁的界园志异", 2, False)
        )
        # 收藏前缀
        self.assertEqual(
            _parse_detail_args("收藏 1", {}), ("", "", 1, True)
        )
        self.assertEqual(
            _parse_detail_args("树海 收藏 2", {}),
            ("rogue_6", "沉沦者的黑流树海", 2, True),
        )
        # 缺省局号 = 1
        self.assertEqual(_parse_detail_args("界园", {}), ("rogue_5", "岁的界园志异", 1, False))
        # 不可识别期名 → 错误文案
        self.assertIsInstance(_parse_detail_args("不存在的期", {}), str)

    def test_build_run_detail_context(self) -> None:
        import asyncio

        from ArknightsUID.arknightsuid_rogue import _build_run_detail_context

        stats = parse_rogue_records(_records_payload())
        assert stats is not None

        async def _no_fetch(*_a, **_k):
            return ""

        with (
            mock.patch(
                "ArknightsUID.utils.prts.client.activity_banner_urls", _fake_banners
            ),
            mock.patch(
                "ArknightsUID.utils.player.context.char_avatar_b64", _no_fetch
            ),
        ):
            context = asyncio.run(
                _build_run_detail_context(stats, "沉沦者的黑流树海", "12****89", 1, False)
            )
        assert isinstance(context, dict)
        self.assertEqual(context["run_index"], 1)
        self.assertEqual(context["run_total"], 2)
        self.assertTrue(context["success"])
        self.assertEqual(context["nodes"], 47)
        self.assertEqual(context["seed"], "seed_abc123")
        self.assertEqual(context["relics"][0]["name"], "钝爪-爆发")
        self.assertEqual(len(context["init_chars"]), 1)
        self.assertEqual(len(context["final_chars"]), 1)

    def test_build_run_detail_out_of_range(self) -> None:
        import asyncio

        from ArknightsUID.arknightsuid_rogue import _build_run_detail_context

        stats = parse_rogue_records(_records_payload())
        assert stats is not None
        # 超出范围 → 错误文案
        result = asyncio.run(
            _build_run_detail_context(stats, "", "", 99, False)
        )
        self.assertIsInstance(result, str)
        self.assertIn("超出范围", result)
        # 收藏列表为空 → 提示文案
        result = asyncio.run(
            _build_run_detail_context(stats, "", "", 1, True)
        )
        self.assertIsInstance(result, str)
        self.assertIn("暂无收藏战绩", result)

    def test_format_run_detail_text(self) -> None:
        from ArknightsUID.arknightsuid_rogue import _format_run_detail

        stats = parse_rogue_records(_records_payload())
        assert stats is not None
        text = _format_run_detail(stats, "沉沦者的黑流树海", "12****89", 1, False)
        self.assertIn("沉沦者的黑流树海", text)
        self.assertIn("第 1/2 局", text)
        self.assertIn("通关 · 标准·N15 · 得分 1888", text)
        self.assertIn("分队：突击战术分队", text)
        self.assertIn("结局：大群所向", text)
        self.assertIn("探索 6 层", text)
        self.assertIn("节点 47", text)
        self.assertIn("种子：seed_abc123", text)
        self.assertIn("开局：桃金娘", text)
        self.assertIn("结算：维什戴尔", text)
        self.assertIn("藏品：钝爪-爆发、教母的信物", text)
        self.assertIn("用时 52 分钟", text)


class HelpViewTest(unittest.TestCase):
    """两级帮助：总览 / 全量 / 单组 / 未命中的参数解析。"""

    def test_help_view(self) -> None:
        from ArknightsUID.arknightsuid_help import HELP_GROUPS, help_view

        self.assertEqual(help_view(""), ("overview", None))
        self.assertEqual(help_view("  "), ("overview", None))
        self.assertEqual(help_view("全部"), ("all", None))
        self.assertEqual(help_view("all"), ("all", None))
        view, group = help_view("肉鸽")
        self.assertEqual(view, "group")
        self.assertEqual(group["name"], "肉鸽")
        # 序号索引
        view, group = help_view("1")
        self.assertEqual(view, "group")
        self.assertIs(group, HELP_GROUPS[0])
        self.assertEqual(help_view("99")[0], "unknown")
        self.assertEqual(help_view("不存在")[0], "unknown")

    def test_overview_context(self) -> None:
        from ArknightsUID.arknightsuid_help import (
            HELP_CORE_CMDS,
            HELP_GROUPS,
            _overview_context,
        )

        context = _overview_context()
        self.assertEqual(context["total"], sum(len(g["cmds"]) for g in HELP_GROUPS))
        self.assertEqual(len(context["groups"]), len(HELP_GROUPS))
        self.assertEqual(context["groups"][0]["idx"], "01")
        self.assertTrue(all(g["blurb"] for g in context["groups"]))
        self.assertTrue(HELP_CORE_CMDS)
        # 组代表 Q 版图标必须是真实烘焙资产（templates/assets/chibi/*.b64）
        from ArknightsUID.utils.path import TEMPLATES_PATH

        for g in context["groups"]:
            self.assertTrue(
                (TEMPLATES_PATH / g["icon"]).exists(),
                f"组 {g['name']} 图标资产缺失: {g['icon']}",
            )

    def test_rogue_records_in_help(self) -> None:
        from ArknightsUID.arknightsuid_help import HELP_GROUPS

        help_cmds = " ".join(c["cmd"] for g in HELP_GROUPS for c in g["cmds"])
        self.assertIn("ark肉鸽战绩", help_cmds)
        readme = Path(__file__).parents[1].joinpath("README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("ark肉鸽战绩", readme)


if __name__ == "__main__":
    unittest.main()
