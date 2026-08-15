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
        self._orig_http = sk_web._http_get_json
        self.calls: list[str] = []

    def tearDown(self) -> None:
        sk_web._http_get_json = self._orig_http  # type: ignore[assignment]
        sk_web._token_cache.update({"token": "", "ts": 0.0})

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
                              "date": "2026-08-14"}},
                    {"item": {"title": ""}},  # 空 title 跳过
                    {"item": {"id": "8", "title": "活动开启", "url": "", "date": ""}},
                ]
            },
        }
        anns = sk_web.parse_ann_list(response)
        self.assertEqual(len(anns), 2)
        self.assertEqual(anns[0]["title"], "维护公告")
        self.assertEqual(anns[0]["id"], "9")
        self.assertEqual(anns[1]["title"], "活动开启")
        self.assertEqual(sk_web.parse_ann_list(None), [])
        self.assertEqual(sk_web.parse_ann_list({"code": 1}), [])


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
        import httpx

        payload = [_record("C-2", "过期缓存", 3.0)]
        self._module._STAGE_CACHE.write_text(
            json.dumps({"_cache_time": time.time() - 99999, "payload": payload}),
            encoding="utf-8",
        )
        # 网络必然失败（不可路由地址），应回退过期缓存
        self._module.STAGE_INFO_URL = "http://127.0.0.1:1/stage/info"
        try:
            result = await self._module.fetch_stage_info(force=True)
        finally:
            self._module.STAGE_INFO_URL = (
                "https://backend.yituliu.cn/stage/info"
            )
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
