"""P4 测试：一图流榜单（yituliu）+ 森空岛 web 公告链（skland/web）。"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from ArknightsUID.utils.skland import web as sk_web
from ArknightsUID.utils.yituliu import (
    format_stage_board,
    item_drop_stats,
    latest_matrix_by_item,
    rank_stages,
)


def _record(
    code: str, zone: str, spm: float, stage_type: str = "MAIN",
    ap: int = 21, start: float = 0, end: float = 0,
) -> dict[str, Any]:
    return {
        "stageCode": code, "zoneName": zone, "stageType": stage_type,
        "apCost": ap, "spm": spm, "start": start, "end": end,
        "stageId": code, "zoneId": code,
    }


def _records() -> list[dict[str, Any]]:
    return [
        _record("14-12", " 第十四章(标准) ", 21.0),
        _record("14-11", " 第十四章(标准) ", 18.5),
        _record("MB-6", "孤岛风云·别传", 19.35, stage_type="ACT_PERM", ap=15),
        _record("DH-9", "多索雷斯假日", 13.01, stage_type="ACT_PERM", ap=18),
        _record("TO-7", "露营", 6.92, stage_type="ACT", ap=21,
                start=(time.time() - 3600) * 1000, end=(time.time() + 86400) * 1000),
        _record("FO-1", "旧活动", 9.9, stage_type="ACT", ap=20,
                start=1000, end=2000),  # 已结束
        _record("Z-0", "无效率值", 0.0),  # spm=0 剔除
    ]


class YituliuRankTest(unittest.TestCase):
    def test_rank_by_group_and_spm(self) -> None:
        rows = rank_stages(_records(), "主线", top=5)
        self.assertEqual([r["stageCode"] for r in rows], ["14-12", "14-11"])

    def test_rank_accepts_english_type(self) -> None:
        rows = rank_stages(_records(), "ACT_PERM", top=5)
        self.assertEqual([r["stageCode"] for r in rows], ["MB-6", "DH-9"])

    def test_rank_filters_closed_act_and_zero_spm(self) -> None:
        rows = rank_stages(_records(), "限时活动")
        self.assertEqual([r["stageCode"] for r in rows], ["TO-7"])  # FO-1 已结束

    def test_rank_future_act_excluded(self) -> None:
        records = _records() + [
            _record("FU-1", "未开启", 8.8, stage_type="ACT",
                    start=(time.time() + 86400) * 1000,
                    end=(time.time() + 2 * 86400) * 1000)
        ]
        rows = rank_stages(records, "限时活动")
        self.assertEqual([r["stageCode"] for r in rows], ["TO-7"])

    def test_format_board_groups(self) -> None:
        text = format_stage_board(_records(), top=2)
        self.assertIn("限时活动", text)
        self.assertIn("主线", text)
        self.assertIn("别传", text)
        self.assertIn("14-12", text)
        self.assertNotIn("FO-1", text)  # 已结束不上榜
        self.assertNotIn("Z-0", text)

    def test_format_board_empty(self) -> None:
        self.assertIn("未获取到", format_stage_board([]))


class WebAnnTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        sk_web._token_cache.update({"token": "", "ts": 0.0})
        sk_web._list_cache.update({"data": [], "ts": 0.0})
        sk_web._detail_cache.clear()
        self._orig_http = sk_web._http_get_json
        self.calls: list[str] = []

    def tearDown(self) -> None:
        sk_web._http_get_json = self._orig_http  # type: ignore[assignment]
        sk_web._token_cache.update({"token": "", "ts": 0.0})
        sk_web._list_cache.update({"data": [], "ts": 0.0})
        sk_web._detail_cache.clear()

    def _patch(self, responses: list[dict[str, Any] | None]) -> None:
        queue = list(responses)

        async def fake_get(url: str, headers: dict[str, str]) -> dict[str, Any] | None:
            self.calls.append(url)
            if not queue:
                raise AssertionError(f"unexpected request {url}")
            return queue.pop(0)

        sk_web._http_get_json = fake_get  # type: ignore[assignment]

    async def test_refresh_public_token_success(self) -> None:
        self._patch([{"code": 0, "data": {"token": "T" * 32}}])
        token = await sk_web.refresh_public_web_token()
        self.assertEqual(token, "T" * 32)
        # 命中缓存：二次调用不再发请求
        self.assertEqual(await sk_web.refresh_public_web_token(), "T" * 32)
        self.assertEqual(len(self.calls), 1)

    async def test_refresh_public_token_fail(self) -> None:
        self._patch([{"code": 10000}])
        self.assertEqual(await sk_web.refresh_public_web_token(), "")

    async def test_web_public_get_retries_on_token_expired(self) -> None:
        # 首个 token 请求 → 业务请求 code=10000（token 失效）→ 强刷 → 重试成功
        self._patch([
            {"code": 0, "data": {"token": "OLD"}},
            {"code": 10000},
            {"code": 0, "data": {"token": "NEW"}},
            {"code": 0, "data": {"list": []}},
        ])
        result = await sk_web.web_public_get(
            sk_web.WEB_HOME_PATH, {"gameId": 1, "cateId": 1}
        )
        self.assertEqual(result, {"code": 0, "data": {"list": []}})
        self.assertEqual(len(self.calls), 4)

    def test_parse_ann_list(self) -> None:
        response = {
            "code": 0,
            "data": {
                "list": [
                    {"item": {"id": "9", "title": "维护公告", "url": "https://x/9",
                              "date": "2026-08-14",
                              "imageCover": {"url": "https://img/9.webp"}},
                     "user": {"nickname": "罗德岛公共", "avatar": "https://img/a9.webp"}},
                    {"item": {"title": ""}},  # 空 title 跳过
                    {"item": {"id": "8", "title": "活动开启", "url": "", "date": ""}},
                ]
            },
        }
        anns = sk_web.parse_ann_list(response)
        self.assertEqual(len(anns), 2)
        self.assertEqual(anns[0]["title"], "维护公告")
        self.assertEqual(anns[0]["id"], "9")
        self.assertEqual(anns[0]["cover"], "https://img/9.webp")
        self.assertEqual(anns[0]["author"], "罗德岛公共")
        self.assertEqual(anns[0]["avatar"], "https://img/a9.webp")
        self.assertEqual(anns[1]["title"], "活动开启")
        # 纯文本公告无封面/作者 → 空串（列表卡降级为纯文字行）
        self.assertEqual(anns[1]["cover"], "")
        self.assertEqual(anns[1]["author"], "")
        self.assertEqual(sk_web.parse_ann_list(None), [])
        self.assertEqual(sk_web.parse_ann_list({"code": 1}), [])

    def test_parse_ann_list_cover_fallback_chain(self) -> None:
        # 封面降级链路（EndUID 同规则）：imageCover → 首图 → 视频封面
        response = {
            "code": 0,
            "data": {"list": [
                {"item": {"id": "1", "title": "首图兜底",
                          "imageListSlice": [{"url": "https://img/s1.webp"}]}},
                {"item": {"id": "2", "title": "视频封面兜底",
                          "videoListSlice": [{"cover": {"url": "https://img/v1.webp"}}]}},
                {"item": {"id": "3", "title": "无任何媒体"}},
            ]},
        }
        anns = sk_web.parse_ann_list(response)
        self.assertEqual(anns[0]["cover"], "https://img/s1.webp")
        self.assertEqual(anns[1]["cover"], "https://img/v1.webp")
        self.assertEqual(anns[2]["cover"], "")

    def test_parse_ann_list_url_and_date_fallback(self) -> None:
        # 明日方舟列表项无 url/date 字段：拼文章页链接 + publishedAtTs（毫秒）转日期
        response = {
            "code": 0,
            "data": {"list": [
                {"item": {"id": "6251002", "title": "手办补款",
                          "publishedAtTs": 1787847812000}},
            ]},
        }
        anns = sk_web.parse_ann_list(response)
        self.assertEqual(anns[0]["url"], "https://www.skland.com/article?id=6251002")
        self.assertRegex(anns[0]["date"], r"^\d{4}-\d{2}-\d{2}$")

    async def test_refresh_public_token_retries_on_invalid_did(self) -> None:
        # 首次 10001（设备信息无效）→ 换新设备 ID 重试 → 成功
        self._patch([
            {"code": 10001, "message": "设备信息无效"},
            {"code": 0, "data": {"token": "T" * 32}},
        ])
        token = await sk_web.refresh_public_web_token()
        self.assertEqual(token, "T" * 32)
        self.assertEqual(len(self.calls), 2)

    async def test_web_public_get_retries_on_invalid_did(self) -> None:
        # 业务请求 10001 → 同一 token 换设备 ID 重试成功
        self._patch([
            {"code": 0, "data": {"token": "T" * 32}},
            {"code": 10001, "message": "设备信息无效"},
            {"code": 0, "data": {"list": []}},
        ])
        result = await sk_web.web_public_get(
            sk_web.WEB_HOME_PATH, {"gameId": 1, "cateId": 1}
        )
        self.assertEqual(result, {"code": 0, "data": {"list": []}})
        self.assertEqual(len(self.calls), 3)

    async def test_get_ark_ann_list_paginates_with_page_token(self) -> None:
        # pageSize 服务端上限 10：两页各 8 条（含 1 条跨页重复）累计去重 15 条
        def page(ids: list[str], token: str) -> dict[str, Any]:
            return {
                "code": 0,
                "data": {
                    "list": [
                        {"item": {"id": i, "title": f"公告{i}",
                                  "publishedAtTs": 1787800000 + int(i)}}
                        for i in ids
                    ],
                    "pageToken": token,
                },
            }

        self._patch([
            {"code": 0, "data": {"token": "T" * 32}},  # token 刷新
            page([str(i) for i in range(1, 9)], "PT"),
            page(["8"] + [str(i) for i in range(9, 16)], ""),
        ])
        anns = await sk_web.get_ark_ann_list(limit=15)
        self.assertEqual(len(anns), 15)
        self.assertEqual([a["id"] for a in anns][:3], ["1", "2", "3"])
        # 请求参数：pageSize 恒为服务端上限，第二页带 pageToken（跳过 token 刷新请求）
        for url in self.calls[1:]:
            self.assertIn("pageSize=10", url)
        self.assertIn("pageToken=PT", self.calls[2])
        # 命中缓存后不再请求
        again = await sk_web.get_ark_ann_list(limit=15)
        self.assertEqual(len(again), 15)
        self.assertEqual(len(self.calls), 3)

    async def test_get_ark_ann_list_falls_back_to_stale_cache(self) -> None:
        # 拉取失败但缓存内有旧数据 → 返回旧缓存
        sk_web._list_cache.update({
            "data": [{"id": "1", "title": "旧公告", "url": "u", "date": "d"}],
            "ts": time.time(),
        })
        self._patch([{"code": 10000}])
        anns = await sk_web.get_ark_ann_list(limit=10)
        self.assertEqual(len(anns), 1)
        self.assertEqual(anns[0]["title"], "旧公告")

    async def test_get_ann_detail_caches_by_id(self) -> None:
        detail_resp = {"code": 0, "data": {
            "item": {"id": "9", "title": "维护公告", "format": "",
                     "textSlice": [{"id": "1", "c": "正文"}]},
            "user": {"nickname": "明日方舟"},
        }}
        self._patch([
            {"code": 0, "data": {"token": "T" * 32}},
            detail_resp,
            detail_resp,  # 缓存过期后二次拉取（直接改缓存时间戳模拟）
        ])
        detail = await sk_web.get_ann_detail("9")
        self.assertEqual(detail["title"], "维护公告")  # type: ignore[index]
        # 命中缓存：不再发请求
        self.assertEqual(await sk_web.get_ann_detail("9"), detail)
        self.assertEqual(len(self.calls), 2)
        # 过期后重新拉取
        sk_web._detail_cache["9"] = (0.0, sk_web._detail_cache["9"][1])
        await sk_web.get_ann_detail("9")
        self.assertEqual(len(self.calls), 3)


class AnnDetailParseTest(unittest.TestCase):
    """公告详情解析：format 段落排序（对齐 EndUID b0a82ae 修复）。"""

    def _response(self, format_json: str) -> dict[str, Any]:
        # textSlice 故意乱序存储；真实阅读顺序由 format 指定为 3 → 1 → 2
        return {"code": 0, "data": {
            "item": {
                "id": "9",
                "title": "维护公告",
                "publishedAtTs": 1787800000,
                "textSlice": [
                    {"id": "2", "c": "第二段"},
                    {"id": "3", "c": "第一段"},
                    {"id": "1", "c": "中间段"},
                    {"id": "4", "c": "   "},  # 纯空白段过滤
                ],
                "imageListSlice": [{"url": "https://img/1.webp", "width": 10, "height": 20}],
                "videoListSlice": [],
                "format": format_json,
            },
            "user": {"nickname": "明日方舟", "avatar": "a", "latestIpLocation": "上海"},
        }}

    def test_paragraphs_ordered_by_format(self) -> None:
        fmt = json.dumps({"data": [
            {"type": "paragraph", "contents": [
                {"type": "text", "contentId": "3"},
                {"type": "link", "contentId": "1"},
            ]},
            {"type": "paragraph", "contents": [{"type": "text", "contentId": "2"}]},
            {"type": "image", "contents": []},  # 非段落节点跳过
        ]})
        detail = sk_web.parse_ann_detail(self._response(fmt))
        self.assertIsNotNone(detail)
        self.assertEqual(detail["paragraphs"], ["第一段中间段", "第二段"])
        self.assertEqual(detail["userName"], "明日方舟")
        self.assertEqual(detail["userIpLocation"], "上海")
        self.assertEqual(detail["images"][0]["url"], "https://img/1.webp")
        self.assertEqual(detail["date"], "2026-08-27")

    def test_fallback_to_storage_order_without_format(self) -> None:
        detail = sk_web.parse_ann_detail(self._response(""))
        self.assertEqual(detail["paragraphs"], ["第二段", "第一段", "中间段"])

    def test_bad_format_falls_back(self) -> None:
        detail = sk_web.parse_ann_detail(self._response("not-json{"))
        self.assertEqual(detail["paragraphs"], ["第二段", "第一段", "中间段"])

    def test_nonzero_code_returns_none(self) -> None:
        self.assertIsNone(sk_web.parse_ann_detail({"code": 10000}))
        self.assertIsNone(sk_web.parse_ann_detail(None))


class AnnDetailFormatTest(unittest.TestCase):
    """push 模块公告详情文本格式化。"""

    def test_format_ann_detail(self) -> None:
        from ArknightsUID.arknightsuid_push import _format_ann_detail

        text = _format_ann_detail({
            "id": "9", "title": "维护公告", "userName": "明日方舟",
            "date": "2026-08-25", "userIpLocation": "上海",
            "paragraphs": ["第一段", "二" * 300],
            "images": [{"url": "u", "width": 1, "height": 2}],
            "videos": [],
        })
        self.assertIn("维护公告", text)
        self.assertIn("明日方舟 · 2026-08-25 · 上海", text)
        self.assertIn("第一段", text)
        self.assertIn("…", text)  # 超长段落截断
        self.assertIn("1 图", text)
        self.assertIn("https://www.skland.com/article?id=9", text)


class SplitAnnImagesTest(unittest.TestCase):
    """公告配图分流（对齐 EndUID ann_detail_card：普通图内嵌 / 超长图追发）。"""

    def test_split_by_ratio_and_skip_video_covers(self) -> None:
        from ArknightsUID.arknightsuid_push import split_ann_images

        normal, longs = split_ann_images({
            "images": [
                {"url": "https://img/normal.webp", "width": 1200, "height": 900},
                {"url": "https://img/maint.webp", "width": 800, "height": 8000},  # 10 倍超长
                {"url": "https://img/cover.webp", "width": 600, "height": 400},   # 视频封面重复
                {"url": "https://img/nosize.webp", "width": 0, "height": 0},      # 无尺寸按普通图
            ],
            "videos": [{"url": "https://v/1", "coverUrl": "https://img/cover.webp"}],
        })
        self.assertEqual(
            [img["url"] for img in normal],
            ["https://img/normal.webp", "https://img/nosize.webp"],
        )
        self.assertEqual(longs, ["https://img/maint.webp"])

    def test_limits_truncate(self) -> None:
        from ArknightsUID.arknightsuid_push import (
            _ANN_CARD_IMAGE_LIMIT,
            _ANN_LONG_IMAGE_LIMIT,
            split_ann_images,
        )

        normal, longs = split_ann_images({
            "images": [
                *[{"url": f"https://img/n{i}", "width": 100, "height": 100}
                  for i in range(_ANN_CARD_IMAGE_LIMIT + 5)],
                *[{"url": f"https://img/l{i}", "width": 100, "height": 1000}
                  for i in range(_ANN_LONG_IMAGE_LIMIT + 5)],
            ],
            "videos": [],
        })
        self.assertEqual(len(normal), _ANN_CARD_IMAGE_LIMIT)
        self.assertEqual(len(longs), _ANN_LONG_IMAGE_LIMIT)


class YituliuCacheTest(unittest.IsolatedAsyncioTestCase):
    """fetch_stage_info 缓存行为（临时目录隔离真实资源路径）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        from ArknightsUID.utils import yituliu

        self._module = yituliu
        self._orig_cache = yituliu._STAGE_CACHE
        self._orig_dir = yituliu._CACHE_DIR
        yituliu._STAGE_CACHE = Path(self._tmp.name) / "stage_info.json"  # type: ignore[assignment]
        yituliu._CACHE_DIR = Path(self._tmp.name)  # type: ignore[assignment]

    def tearDown(self) -> None:
        self._module._STAGE_CACHE = self._orig_cache  # type: ignore[assignment]
        self._module._CACHE_DIR = self._orig_dir  # type: ignore[assignment]
        self._tmp.cleanup()

    async def test_fetch_uses_fresh_cache_without_network(self) -> None:
        payload = [_record("C-1", "缓存区", 5.0)]
        self._module._STAGE_CACHE.write_text(
            json.dumps({"_cache_time": time.time(), "payload": payload}),
            encoding="utf-8",
        )
        result = await self._module.fetch_stage_info()
        self.assertEqual(result, payload)

    async def test_fetch_returns_stale_cache_on_network_fail(self) -> None:

        payload = [_record("C-2", "过期缓存", 3.0)]
        self._module._STAGE_CACHE.write_text(
            json.dumps({"_cache_time": time.time() - 99999, "payload": payload}),
            encoding="utf-8",
        )
        from unittest import mock

        with mock.patch.object(self._module, "_download_json", return_value=None) as fetch:
            result = await self._module.fetch_stage_info(force=True)
        fetch.assert_awaited_once_with(self._module.STAGE_INFO_URL)
        self.assertEqual(result, payload)


if __name__ == "__main__":
    unittest.main()


class PushDiffStateTest(unittest.TestCase):
    """推送 diff 与状态文件（arknightsuid_push 纯逻辑部分）。"""

    def setUp(self) -> None:
        import ArknightsUID.arknightsuid_push as push_mod

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = push_mod._STATE_PATH
        push_mod._STATE_PATH = Path(self._tmp.name) / "push_state.json"
        self.push = push_mod

    def tearDown(self) -> None:
        self.push._STATE_PATH = self._orig_path
        self._tmp.cleanup()

    def test_diff_new_entries(self) -> None:
        entries = {
            "活动:A": [{"kind": "活动", "title": "A", "detail": ""}],
            "卡池:B": [{"kind": "卡池", "title": "B", "detail": "d"}],
        }
        self.assertEqual(len(self.push.diff_new_entries(entries, [])), 2)
        self.assertEqual(
            len(self.push.diff_new_entries(entries, ["活动:A"])), 1
        )
        self.assertEqual(self.push.diff_new_entries(entries, ["活动:A", "卡池:B"]), [])

    def test_state_roundtrip_and_limit(self) -> None:
        self.push.save_push_state({"seen": ["a", "b"]})
        self.assertEqual(self.push.load_push_state(), {"seen": ["a", "b"]})
        # 超限裁剪保留最新
        self.push.save_push_state({"seen": [f"k{i}" for i in range(500)]})
        seen = self.push.load_push_state()["seen"]
        self.assertEqual(len(seen), self.push._STATE_LIMIT)
        self.assertIn("k499", seen)
        self.assertNotIn("k0", seen)

    def test_load_state_missing_file(self) -> None:
        self.assertEqual(self.push.load_push_state(), {})


class DropMatrixTest(unittest.TestCase):
    """COS 掉落矩阵（材料掉率）逻辑测试。"""

    def _matrix(self) -> list[dict[str, Any]]:
        return [
            # 同关卡两时间窗：保留 end 最新（times 大的那条）
            {"stageId": "s1", "itemId": "30013", "times": 100, "quantity": 50,
             "start": 1, "end": 100},
            {"stageId": "s1", "itemId": "30013", "times": 1000, "quantity": 600,
             "start": 100, "end": 200},
            {"stageId": "s2", "itemId": "30013", "times": 500, "quantity": 200,
             "start": 0, "end": 200},
            # 样本不足（<300）
            {"stageId": "s3", "itemId": "30013", "times": 100, "quantity": 100,
             "start": 0, "end": 200},
            # 其他材料
            {"stageId": "s2", "itemId": "99999", "times": 1000, "quantity": 100,
             "start": 0, "end": 200},
        ]

    def _meta(self) -> dict[str, dict[str, Any]]:
        return {
            "s1": {"stageCode": "IW-7", "zoneName": "将进酒", "stageType": "ACT_PERM",
                   "apCost": 19},
            "s2": {"stageCode": "11-2", "zoneName": "第十一章", "stageType": "MAIN",
                   "apCost": 21},
            "s3": {"stageCode": "X-1", "zoneName": "未知", "stageType": "MAIN",
                   "apCost": 15},
        }

    def test_latest_matrix_by_item_dedupes_time_windows(self) -> None:
        by_item = latest_matrix_by_item(self._matrix())
        rows_30013 = by_item["30013"]
        s1 = [r for r in rows_30013 if r["stageId"] == "s1"]
        self.assertEqual(len(s1), 1)
        self.assertEqual(s1[0]["times"], 1000)  # end 更新的窗口
        self.assertEqual(len(by_item["99999"]), 1)

    def test_item_drop_stats_sorts_and_filters(self) -> None:
        by_item = latest_matrix_by_item(self._matrix())
        rows = item_drop_stats("30013", by_item, self._meta(), top=5)
        # s3 样本 100 < 300 被剔除；s1 rate=0.6 > s2 rate=0.4 → s1 在前
        self.assertEqual([r["stage_code"] for r in rows], ["IW-7", "11-2"])
        self.assertAlmostEqual(rows[0]["rate"], 0.6)
        self.assertAlmostEqual(rows[0]["ap_expect"], 19 / 0.6)
        self.assertEqual(rows[0]["times"], 1000)

    def test_item_drop_stats_top_limit(self) -> None:
        by_item = latest_matrix_by_item(self._matrix())
        self.assertEqual(len(item_drop_stats("30013", by_item, self._meta(), top=1)), 1)

    def test_item_drop_stats_unknown_stage_skipped(self) -> None:
        meta = {"s1": self._meta()["s1"]}  # 只有 s1
        rows = item_drop_stats("30013", latest_matrix_by_item(self._matrix()),
                               meta, top=5)
        self.assertEqual([r["stage_code"] for r in rows], ["IW-7"])
