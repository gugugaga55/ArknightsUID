"""鹰角 OAuth 链（hypergryph/client.py）+ 抽卡数据服务（hypergryph/gacha.py）测试。

httpx 全 mock（替换 client._request，与 test_skland_auth 同模式）；
抽卡本地存储重定向到临时目录，不污染真实资源路径。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ArknightsUID.utils.enduid.adapter import ArkCredential
from ArknightsUID.utils.hypergryph import client as hg
from ArknightsUID.utils.hypergryph import gacha as gacha_svc


class FakeRequest:
    """替换 hg._request 的队列桩，返回 (status_code, json|None, set_cookie 列表)。"""

    queue: list[tuple[int, dict[str, Any] | None, list[str]]] = []
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    @classmethod
    async def fake(
        cls,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any] | None, list[str]]:
        cls.calls.append((method, url, params))
        if not cls.queue:
            raise AssertionError(f"unexpected request {method} {url} {params}")
        return cls.queue.pop(0)


def _cred(**overrides: Any) -> ArkCredential:
    fields: dict[str, Any] = {
        "uid": "123456789",
        "nickname": "博士",
        "server_id": "1",
        "cookie_status": "",
        "cred": "C" * 32,
        "token": "",
        "hg_token": "T" * 24,
        "skland_user_id": "",
        "record_id": "rec_888",
    }
    fields.update(overrides)
    return ArkCredential(**fields)


class OAuthChainTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig = hg._request
        hg._request = FakeRequest.fake  # type: ignore[assignment]
        FakeRequest.queue = []
        FakeRequest.calls = []

    def tearDown(self) -> None:
        hg._request = self._orig  # type: ignore[assignment]

    async def test_grant_token_success(self) -> None:
        FakeRequest.queue = [(200, {"status": 0, "data": {"token": "GRANT"}}, [])]
        self.assertEqual(await hg.get_grant_token("T" * 24), "GRANT")
        method, url, _ = FakeRequest.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("as.hypergryph.com", url)

    async def test_grant_token_fail(self) -> None:
        FakeRequest.queue = [(200, {"status": 1}, [])]
        self.assertEqual(await hg.get_grant_token("T" * 24), "")
        self.assertEqual(await hg.get_grant_token(""), "")

    async def test_u8_token_chains_grant(self) -> None:
        FakeRequest.queue = [
            (200, {"status": 0, "data": {"token": "GRANT"}}, []),
            (200, {"status": 0, "data": {"token": "U8"}}, []),
        ]
        self.assertEqual(await hg.get_u8_token("rec_1", "T" * 24), "U8")
        # 第二跳 body 用 record_uid + grant_token
        method, url, _ = FakeRequest.calls[1]
        self.assertEqual(method, "POST")
        self.assertIn("u8_token_by_uid", url)

    async def test_u8_token_grant_fail_short_circuits(self) -> None:
        FakeRequest.queue = [(200, {"status": 1}, [])]
        self.assertEqual(await hg.get_u8_token("rec_1", "T" * 24), "")
        self.assertEqual(len(FakeRequest.calls), 1)

    async def test_ak_cookie_from_set_cookie(self) -> None:
        FakeRequest.queue = [
            (
                200,
                {"code": 0},
                ["path=/; HttpOnly", "ak-user-center=COOKIE_VALUE; Path=/; Max-Age=86400"],
            )
        ]
        self.assertEqual(await hg.get_ak_cookie("U8"), "COOKIE_VALUE")

    async def test_ak_cookie_missing(self) -> None:
        FakeRequest.queue = [(200, {"code": 0}, [])]
        self.assertEqual(await hg.get_ak_cookie("U8"), "")
        FakeRequest.queue = [(200, {"code": 1}, ["ak-user-center=x;"])]
        self.assertEqual(await hg.get_ak_cookie("U8"), "")

    async def test_login_gacha_chain_full_success(self) -> None:
        FakeRequest.queue = [
            (200, {"status": 0, "data": {"token": "GRANT"}}, []),
            (200, {"status": 0, "data": {"token": "U8"}}, []),
            (200, {"code": 0}, ["ak-user-center=CK; Path=/"]),
        ]
        auth = await hg.login_gacha_chain(_cred())
        self.assertIsInstance(auth, hg.ArkGachaAuth)
        assert isinstance(auth, hg.ArkGachaAuth)
        self.assertTrue(auth.ready)
        self.assertEqual(
            auth.headers(),
            {
                "X-Account-Token": "T" * 24,
                "X-Role-Token": "U8",
                "Cookie": "ak-user-center=CK",
            },
        )

    async def test_login_gacha_chain_requires_hg_token(self) -> None:
        result = await hg.login_gacha_chain(_cred(hg_token=""))
        self.assertIsInstance(result, str)
        self.assertIn("扫码登录", result)

    async def test_login_gacha_chain_u8_fail(self) -> None:
        FakeRequest.queue = [
            (200, {"status": 0, "data": {"token": "GRANT"}}, []),
            (200, {"status": 1}, []),
        ]
        result = await hg.login_gacha_chain(_cred())
        self.assertIsInstance(result, str)
        self.assertIn("u8_token", result)

    async def test_gacha_cate_and_history_headers(self) -> None:
        FakeRequest.queue = [
            (200, {"code": 0, "data": [{"id": 1, "name": "限定寻访"}]}, []),
            (200, {"code": 0, "data": {"list": [], "hasMore": False}}, []),
        ]
        auth = hg.ArkGachaAuth("T" * 24, "U8", "CK")
        cates = await hg.get_gacha_categories(_cred(), auth)
        self.assertEqual(cates, [{"id": 1, "name": "限定寻访"}])
        page = await hg.get_gacha_history_page(_cred(), auth, "1")
        self.assertEqual(page, {"list": [], "hasMore": False})

    async def test_gacha_history_cursor_params(self) -> None:
        FakeRequest.queue = [
            (200, {"code": 0, "data": {"list": [], "hasMore": False}}, []),
        ]
        auth = hg.ArkGachaAuth("T" * 24, "U8", "CK")
        await hg.get_gacha_history_page(_cred(), auth, "2", gacha_ts=1700000000, pos=7)
        _, _, params = FakeRequest.calls[0]
        assert params is not None
        self.assertEqual(params["gachaTs"], 1700000000)
        self.assertEqual(params["pos"], 7)
        self.assertEqual(params["size"], 100)
        self.assertEqual(params["uid"], "123456789")

    async def test_exchange_gift_code(self) -> None:
        FakeRequest.queue = [(200, {"code": 0, "message": "兑换成功"}, [])]
        auth = hg.ArkGachaAuth("T" * 24, "U8", "CK")
        result = await hg.exchange_gift_code(_cred(), auth, "GIFT123")
        self.assertEqual(result, {"code": 0, "message": "兑换成功"})


def _rec(
    ts: int, pos: int, rarity: int, name: str, category: str = "1", pool: str = "限定寻访·集结"
) -> dict[str, Any]:
    return {
        "category": category,
        "char_id": f"char_{name}",
        "name": name,
        "rarity": rarity,
        "is_new": False,
        "gacha_ts": ts,
        "pos": pos,
        "pool_name": pool,
    }


def _raw(ts_ms: int, pos: int, name: str, rarity: int) -> dict[str, Any]:
    """官方接口原始条目（gachaTs 毫秒）。"""
    return {
        "charId": f"char_{name}", "charName": name, "rarity": rarity,
        "isNew": False, "gachaTs": ts_ms, "pos": pos, "poolName": "限定寻访",
    }


class GachaAnalyzeTest(unittest.TestCase):
    def test_stats_zero_based_rarity(self) -> None:
        # 0 基口径（无 rarity==6 出现）→ 六星=5
        records = [_rec(100, 1, 3, "A"), _rec(101, 2, 5, "B"), _rec(102, 3, 4, "C")]
        stats = gacha_svc.analyze_gacha(records)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["six_count"], 1)
        self.assertEqual(stats["six_rate"], "33.33")
        self.assertEqual(stats["avg_six"], "3.0")

    def test_stats_one_based_rarity(self) -> None:
        # 出现 rarity==6 → 1 基口径，六星=6；rarity=5 不算六星
        records = [
            _rec(100, 1, 5, "A"), _rec(101, 2, 6, "B"),
            _rec(102, 3, 6, "C"), _rec(103, 4, 4, "D"),
        ]
        stats = gacha_svc.analyze_gacha(records)
        self.assertEqual(stats["six_count"], 2)
        self.assertEqual(stats["recent"][0]["name"], "C")

    def test_pity_counts_pulls_since_last_six_in_latest_category(self) -> None:
        records = [
            _rec(100, 1, 5, "六1"), _rec(101, 2, 3, "A"), _rec(102, 3, 3, "B"),
            _rec(103, 4, 3, "C"),
        ]
        stats = gacha_svc.analyze_gacha(records)
        # 最新六星在 idx0，其后 3 抽 → pity=3
        self.assertEqual(stats["pity"], 3)

    def test_pity_no_six_in_category(self) -> None:
        records = [_rec(100, 1, 3, "A"), _rec(101, 2, 3, "B")]
        stats = gacha_svc.analyze_gacha(records)
        self.assertEqual(stats["pity"], 2)

    def test_recent_pull_counts_within_category(self) -> None:
        records = [
            _rec(100, 1, 3, "A", category="1"),
            _rec(101, 2, 5, "B", category="1"),
            _rec(102, 3, 3, "C", category="2"),
            _rec(103, 4, 5, "D", category="2"),
        ]
        stats = gacha_svc.analyze_gacha(records)
        # 最新六星 D：同类别内 idx3，无前置六星 → 第 2 抽（C、D）
        newest = stats["recent"][0]
        self.assertEqual(newest["name"], "D")
        self.assertEqual(newest["pool"], "限定寻访·集结")
        self.assertEqual(newest["pull"], 2)
        self.assertEqual(newest["category"], "2")
        self.assertEqual(newest["fives"], [])
        # B：类别 1 内第 2 抽
        self.assertEqual(stats["recent"][1]["pull"], 2)

    def test_five_count_and_rate(self) -> None:
        records = [
            _rec(100, 1, 5, "五1"), _rec(101, 2, 6, "六1"),
            _rec(102, 3, 5, "五2"), _rec(103, 4, 3, "A"),
        ]
        stats = gacha_svc.analyze_gacha(records)
        self.assertEqual(stats["five_count"], 2)
        self.assertEqual(stats["five_rate"], "50.00")

    def test_recent_six_carries_segment_fives(self) -> None:
        # 六1 段内无五星；六2 段内 2 个五星，按抽取顺序（旧→新）排列
        records = [
            _rec(100, 1, 6, "六1"),
            _rec(101, 2, 5, "五A"), _rec(102, 3, 5, "五B"),
            _rec(103, 4, 3, "A"), _rec(104, 5, 6, "六2"),
        ]
        stats = gacha_svc.analyze_gacha(records)
        newest = stats["recent"][0]
        self.assertEqual(newest["name"], "六2")
        self.assertEqual([f["name"] for f in newest["fives"]], ["五A", "五B"])
        self.assertEqual(newest["fives_extra"], 0)
        self.assertEqual(stats["recent"][1]["fives"], [])

    def test_segment_fives_do_not_cross_category(self) -> None:
        # 类别 2 的五星不得挂到类别 1 的六星段下
        records = [
            _rec(100, 1, 5, "五跨类", category="2"),
            _rec(101, 2, 6, "六1", category="1"),
        ]
        stats = gacha_svc.analyze_gacha(records)
        self.assertEqual(stats["recent"][0]["fives"], [])
        self.assertEqual(stats["pity_fives"], [])

    def test_pity_fives_after_latest_six(self) -> None:
        records = [
            _rec(100, 1, 6, "六1"),
            _rec(101, 2, 5, "五X"), _rec(102, 3, 4, "A"), _rec(103, 4, 5, "五Y"),
        ]
        stats = gacha_svc.analyze_gacha(records)
        self.assertEqual(stats["pity"], 3)
        self.assertEqual([f["name"] for f in stats["pity_fives"]], ["五X", "五Y"])
        self.assertEqual(stats["pity_fives_extra"], 0)

    def test_fives_display_cap(self) -> None:
        # 单段五星超过展示上限：截断 + fives_extra 计数
        records = (
            [_rec(100, 1, 6, "六0")]
            + [_rec(200 + i, i + 2, 5, f"五{i}") for i in range(14)]
            + [_rec(300, 99, 6, "六1")]
        )
        stats = gacha_svc.analyze_gacha(records)
        newest = stats["recent"][0]
        self.assertEqual(len(newest["fives"]), 12)
        self.assertEqual(newest["fives_extra"], 2)

    def test_format_gacha_text_mentions_fives(self) -> None:
        records = [_rec(100, 1, 5, "五A"), _rec(101, 2, 6, "六1")]
        text = gacha_svc.format_gacha_text("", "123***789", records, "")
        self.assertIn("五星 1", text)
        self.assertIn("本段五星 ×1：五A", text)

    def test_empty_records(self) -> None:
        self.assertEqual(gacha_svc.analyze_gacha([]), {})

    def test_merge_records_dedupes_and_sorts_desc(self) -> None:
        local = [_rec(100, 1, 5, "旧六"), _rec(99, 2, 3, "旧A")]
        incoming = [_rec(100, 1, 5, "旧六"), _rec(200, 1, 3, "新A")]
        merged = gacha_svc.merge_records(local, incoming)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["gacha_ts"], 200)

    def test_norm_ts_milliseconds(self) -> None:
        self.assertEqual(gacha_svc._norm_ts(1700000000123), 1700000000)
        self.assertEqual(gacha_svc._norm_ts(1700000000), 1700000000)
        self.assertEqual(gacha_svc._norm_ts("bad"), 0)

    def test_build_gacha_context(self) -> None:
        records = [_rec(100, 1, 5, "B"), _rec(101, 2, 3, "A")]
        ctx = gacha_svc.build_gacha_context("博士", "123***789", records, "v")
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx["name"], "博士")
        self.assertEqual(ctx["uid"], "123***789")
        self.assertEqual(ctx["pity_max"], 99)
        self.assertEqual(ctx["total"], 2)

    def test_format_gacha_text_includes_warning(self) -> None:
        records = [_rec(100, 1, 5, "B")]
        text = gacha_svc.format_gacha_text("", "123***789", records, "网络差")
        self.assertIn("抽卡统计", text)
        self.assertIn("网络差", text)
        self.assertIn("123***789", text)


class SyncGachaTest(unittest.IsolatedAsyncioTestCase):
    """sync_gacha_logs 集成测试（client 三函数全 mock + 临时目录存储）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = gacha_svc._logs_path
        gacha_svc._logs_path = lambda uid: Path(self._tmp.name) / f"{uid}.json"  # type: ignore[assignment]

        self._orig_login = hg.login_gacha_chain
        self._orig_cate = hg.get_gacha_categories
        self._orig_page = hg.get_gacha_history_page
        hg.login_gacha_chain = self._fake_login  # type: ignore[assignment]

    def tearDown(self) -> None:
        gacha_svc._logs_path = self._orig_path  # type: ignore[assignment]
        hg.login_gacha_chain = self._orig_login  # type: ignore[assignment]
        hg.get_gacha_categories = self._orig_cate  # type: ignore[assignment]
        hg.get_gacha_history_page = self._orig_page  # type: ignore[assignment]
        self._tmp.cleanup()

    @staticmethod
    async def _fake_login(credential: Any) -> Any:
        return hg.ArkGachaAuth("T" * 24, "U8", "CK")

    def _patch_pages(
        self, cates: list[dict[str, Any]], pages: dict[str, list[dict[str, Any]]]
    ) -> list[Any]:
        async def fake_cate(credential: Any, auth: Any) -> list[dict[str, Any]]:
            return cates

        state = {"page": 0}

        async def fake_page(
            credential: Any, auth: Any, category: str,
            gacha_ts: int | None = None, pos: int | None = None,
        ) -> dict[str, Any]:
            seq = pages.get(category, [])
            if state["page"] >= len(seq):
                return {"list": [], "hasMore": False}
            item = seq[state["page"]]
            state["page"] += 1
            return item

        hg.get_gacha_categories = fake_cate  # type: ignore[assignment]
        hg.get_gacha_history_page = fake_page  # type: ignore[assignment]
        return []

    async def test_sync_first_pull_and_incremental_stop(self) -> None:
        raw_page = {
            "list": [
                {"charId": "c1", "charName": "新干员", "rarity": 5,
                 "gachaTs": 1700000200000, "pos": 2, "poolName": "限定寻访"},
                {"charId": "c2", "charName": "旧干员", "rarity": 3,
                 "gachaTs": 1700000100000, "pos": 1, "poolName": "限定寻访"},
            ],
            "hasMore": True,
        }
        self._patch_pages([{"id": 1, "name": "限定寻访"}], {"1": [raw_page]})

        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual(warning, "")
        self.assertEqual(len(records), 2)
        # ms → s 归一
        self.assertEqual(records[0]["gacha_ts"], 1700000200)

        # 第二次同步：本地最新 1700000200，翻页遇旧记录即停，不重复入库
        state_pages = {
            "list": [
                {"charId": "c3", "charName": "更新", "rarity": 4,
                 "gachaTs": 1700000300000, "pos": 3, "poolName": "限定寻访"},
                {"charId": "c2", "charName": "旧干员", "rarity": 3,
                 "gachaTs": 1700000100000, "pos": 1, "poolName": "限定寻访"},
            ],
            "hasMore": True,
        }
        self._patch_pages([{"id": 1}], {"1": [state_pages]})
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["name"], "更新")

    async def test_sync_login_fail_uses_local_fallback(self) -> None:
        # 先落一份本地记录
        gacha_records = [_rec(100, 1, 5, "本地六")]
        await gacha_svc.save_local_logs("123456789", gacha_records)

        async def fail_login(credential: Any) -> str:
            return "登录链失败提示"

        hg.login_gacha_chain = fail_login  # type: ignore[assignment]
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual(len(records), 1)
        self.assertIn("登录链失败提示", warning)
        self.assertIn("本地记录", warning)

    async def test_sync_nothing_returns_empty(self) -> None:
        self._patch_pages([{"id": 1}], {"1": [{"list": [], "hasMore": False}]})
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual(records, [])
        self.assertNotEqual(warning, "")


if __name__ == "__main__":
    unittest.main()


class SyncIntegrityTest(unittest.IsolatedAsyncioTestCase):
    """审计 #11/#12/#13/#27：同 UID 单飞、分页中断不推检查点、量纲持久化、脏文件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = gacha_svc._logs_path
        gacha_svc._logs_path = lambda uid: Path(self._tmp.name) / f"{uid}.json"  # type: ignore[assignment]
        self._orig_login = hg.login_gacha_chain
        self._orig_cate = hg.get_gacha_categories
        self._orig_page = hg.get_gacha_history_page
        hg.login_gacha_chain = self._fake_login  # type: ignore[assignment]

    def tearDown(self) -> None:
        gacha_svc._logs_path = self._orig_path  # type: ignore[assignment]
        hg.login_gacha_chain = self._orig_login  # type: ignore[assignment]
        hg.get_gacha_categories = self._orig_cate  # type: ignore[assignment]
        hg.get_gacha_history_page = self._orig_page  # type: ignore[assignment]
        self._tmp.cleanup()

    @staticmethod
    async def _fake_login(credential: Any) -> Any:
        return hg.ArkGachaAuth("T" * 24, "U8", "CK")

    def _patch(self, page_side_effect) -> None:
        async def fake_cate(credential: Any, auth: Any) -> list[dict[str, Any]]:
            return [{"id": 1, "name": "限定寻访"}]

        async def fake_page(
            credential: Any, auth: Any, category: str,
            gacha_ts: int | None = None, pos: int | None = None,
        ) -> Any:
            return page_side_effect()

        hg.get_gacha_categories = fake_cate  # type: ignore[assignment]
        hg.get_gacha_history_page = fake_page  # type: ignore[assignment]

    async def test_mid_page_failure_discards_partial_batch(self) -> None:
        """第一页成功、第二页失败：部分批次丢弃，检查点不推进，下轮可补。"""
        await gacha_svc.save_local_logs(
            "123456789", [_rec(100, 1, 5, "本地六")]
        )
        calls = {"n": 0}

        def pages():
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "list": [_raw(1700000200000, 2, "新六", 5)],
                    "hasMore": True,
                }
            return None  # 第二页请求失败

        self._patch(pages)
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        # 第一页的"新六"不得入库（保存会把检查点推进到断层位置）
        self.assertEqual([r["name"] for r in records], ["本地六"])
        self.assertIn("中断", warning)

    async def test_first_page_failure_warns_with_local_fallback(self) -> None:
        """首页即失败：类别进警告（此前只有拉到部分记录才报），
        本地兜底数据照常返回。"""
        await gacha_svc.save_local_logs(
            "123456789", [_rec(100, 1, 5, "本地六")]
        )

        def pages():
            return None  # 首页请求失败

        self._patch(pages)
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual([r["name"] for r in records], ["本地六"])
        self.assertIn("中断", warning)

    async def test_first_page_failure_without_local_is_pull_error(self) -> None:
        """首页失败且无本地记录：报拉取故障，而不是误导性的"无记录"。"""
        def pages():
            return None

        self._patch(pages)
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual(records, [])
        self.assertIn("拉取失败", warning)
        self.assertNotIn("无记录", warning)

    async def test_concurrent_sync_serializes_per_uid(self) -> None:
        """审计 #11：两个同步并发跑，后写者不得覆盖先写者。"""
        counter = {"n": 0}

        def pages():
            counter["n"] += 1
            return {
                "list": [_raw(1700000000000 + counter["n"] * 1000, counter["n"],
                              f"并发{counter['n']}", 4)],
                "hasMore": False,
            }

        self._patch(pages)
        await asyncio.gather(
            gacha_svc.sync_gacha_logs(_cred()),
            gacha_svc.sync_gacha_logs(_cred()),
        )
        final = await gacha_svc.load_local_logs("123456789")
        names = {r["name"] for r in final}
        # 无锁时两协程基于同一份空本地日志各写各的，先写者被覆盖
        self.assertEqual(names, {"并发1", "并发2"}, f"并发同步丢记录: {names}")

    async def test_pagination_echoes_raw_cursor_through_http_params(self) -> None:
        """分页必须原样回传上游毫秒游标；本地统计仍使用秒。"""
        from unittest import mock

        first = _raw(1_700_000_000_123, 0, "第一页", 3)
        second = _raw(1_699_999_999_456, 1, "第二页", 4)
        calls = []

        async def request(method, url, **kwargs):
            params = kwargs["params"]
            calls.append(params)
            if url == hg.GACHA_CATE_API:
                return 200, {"code": 0, "data": [{"id": 1}]}, []
            if "gachaTs" not in params:
                data = {"list": [first], "hasMore": True}
            else:
                self.assertEqual(params["gachaTs"], first["gachaTs"])
                self.assertEqual(params["pos"], 0)
                data = {"list": [second], "hasMore": False}
            return 200, {"code": 0, "data": data}, []

        with mock.patch.object(hg, "_request", request):
            records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual(warning, "")
        self.assertEqual([r["name"] for r in records], ["第一页", "第二页"])
        self.assertEqual(records[0]["gacha_ts"], 1_700_000_000)
        self.assertEqual(len(calls), 3)

    async def test_full_sync_repairs_gaps_without_deleting_local_history(self) -> None:
        await gacha_svc.save_local_logs(
            "123456789", [_rec(1_700_000_100, 1, 5, "已有最新"),
                          _rec(1_600_000_000, 1, 3, "官方已清理的旧记录")],
        )
        pages = iter([
            {"list": [_raw(1_700_000_000_123, 0, "缺口1", 3)], "hasMore": True},
            {"list": [_raw(1_699_999_999_456, 1, "缺口2", 4)], "hasMore": False},
        ])
        self._patch(lambda: next(pages))
        records, warning = await gacha_svc.sync_gacha_logs(_cred(), full=True)
        self.assertEqual(warning, "")
        self.assertEqual({r["name"] for r in records},
                         {"已有最新", "缺口1", "缺口2", "官方已清理的旧记录"})

    async def test_legacy_seconds_floor_restarts_backfill(self) -> None:
        await gacha_svc.save_local_logs("123456789", [_rec(1_700_000_100, 1, 5, "已有")])
        await gacha_svc._save_sync_floors("123456789", {"1": {"ts": 1_700_000_000, "pos": 0}})
        pages = iter([
            {"list": [_raw(1_700_000_000_123, 0, "补拉1", 3)], "hasMore": True},
            {"list": [_raw(1_699_999_999_456, 1, "补拉2", 4)], "hasMore": False},
        ])
        self._patch(lambda: next(pages))
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual(warning, "")
        self.assertEqual(len(records), 3)
        self.assertEqual(await gacha_svc._load_sync_floors("123456789"), {})

    async def test_fuse_exhaustion_continues_next_round(self) -> None:
        """审计 #39：保险丝（50 页）耗尽不再被当"完整"——段内无断层的
        记录像常入库并留续拉游标；下轮从游标继续翻到底后清除游标。
        旧版把耗尽当完整落盘，断层以下历史永久拉不到且无警告。"""
        base_ts = 1_700_000_000
        total = (gacha_svc._MAX_PAGES + 2) * 2  # 52 页 × 2 条
        items = [
            _raw((base_ts - i) * 1000, i % 90 + 1, f"干员{i:03d}", 3 + i % 3)
            for i in range(total)
        ]
        pages_data = [
            {"list": items[i : i + 2], "hasMore": True}
            for i in range(0, total, 2)
        ]
        pages_data[-1]["hasMore"] = False
        # 游标（上页末条 ts）→ 下一页索引
        next_by_cursor = {
            page["list"][-1]["gachaTs"]: i + 1
            for i, page in enumerate(pages_data)
        }

        async def fake_cate(credential: Any, auth: Any) -> list[dict[str, Any]]:
            return [{"id": 1, "name": "限定寻访"}]

        async def fake_page(
            credential: Any, auth: Any, category: str,
            gacha_ts: int | None = None, pos: int | None = None,
        ) -> Any:
            idx = 0 if gacha_ts is None else next_by_cursor.get(
                gacha_ts, len(pages_data)
            )
            if idx >= len(pages_data):
                return {"list": [], "hasMore": False}
            return pages_data[idx]

        hg.get_gacha_categories = fake_cate  # type: ignore[assignment]
        hg.get_gacha_history_page = fake_page  # type: ignore[assignment]

        # 第一轮：50 页保险丝耗尽，records（100 条）入库 + 游标落盘 + 警告
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual(len(records), gacha_svc._MAX_PAGES * 2)
        self.assertIn("补拉", warning)
        floors = await gacha_svc._load_sync_floors("123456789")
        self.assertIn("1", floors, "保险丝耗尽必须留下续拉游标")

        # 第二轮：增量段快速衔接 + 补拉段从游标翻到底 → 全量 104 条、
        # 游标清除、无警告
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        self.assertEqual(len(records), total)
        self.assertEqual(warning, "")
        floors = await gacha_svc._load_sync_floors("123456789")
        self.assertEqual(floors, {}, "翻到底后游标应清除")

    async def test_dirty_timestamps_do_not_crash_commands(self) -> None:
        """审计 #39：本地日志字段级脏数据（null/"abc"）不得拖垮排序/统计。"""
        dirty = [
            {"category": "1", "gacha_ts": None, "pos": None, "rarity": 5,
             "name": "坏时间"},
            {"category": "1", "gacha_ts": "abc", "pos": "x", "rarity": 3,
             "name": "坏字符串"},
        ]
        await gacha_svc.save_local_logs("123456789", dirty)

        def pages():
            return {
                "list": [_raw(1700000200000, 2, "新六", 5)],
                "hasMore": False,
            }

        self._patch(pages)
        records, warning = await gacha_svc.sync_gacha_logs(_cred())
        names = {r["name"] for r in records}
        self.assertEqual(names, {"坏时间", "坏字符串", "新六"})

    def test_mixed_basis_rarity_normalized_per_record(self) -> None:
        """审计 #13：0 基与 1 基记录混存时六星逐条判定。"""
        records = [
            {"category": "1", "gacha_ts": 3, "pos": 1, "rarity": 5,
             "basis": "0", "name": "零基六"},      # 0 基六星
            {"category": "1", "gacha_ts": 2, "pos": 1, "rarity": 6,
             "basis": "1", "name": "壹基六"},      # 1 基六星
            {"category": "1", "gacha_ts": 1, "pos": 1, "rarity": 5,
             "basis": "1", "name": "壹基五"},      # 1 基五星
        ]
        stats = gacha_svc.analyze_gacha(records)
        self.assertEqual(stats["six_count"], 2)
        self.assertEqual(stats["total"], 3)

    def test_legacy_records_keep_old_threshold_semantics(self) -> None:
        """无 basis 存量记录：判定与旧版全局阈值完全一致。"""
        zero_based = [
            {"category": "1", "gacha_ts": 2, "pos": 1, "rarity": 5},  # 0基六星
            {"category": "1", "gacha_ts": 1, "pos": 1, "rarity": 4},
        ]
        self.assertEqual(gacha_svc.analyze_gacha(zero_based)["six_count"], 1)
        one_based = [
            {"category": "1", "gacha_ts": 3, "pos": 1, "rarity": 6},  # 1基六星
            {"category": "1", "gacha_ts": 2, "pos": 1, "rarity": 5},  # 1基五星
            {"category": "1", "gacha_ts": 1, "pos": 1, "rarity": 4},
        ]
        self.assertEqual(gacha_svc.analyze_gacha(one_based)["six_count"], 1)

    async def test_corrupt_local_elements_filtered(self) -> None:
        """审计 #27：[null, "bad"] 不再让命令抛异常。"""
        import json as _json

        path = gacha_svc._logs_path("123456789")
        path.write_text(_json.dumps([None, "bad record"]), encoding="utf-8")
        self.assertEqual(await gacha_svc.load_local_logs("123456789"), [])


class FullSyncCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_keyword_reaches_sync_service(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from ArknightsUID import arknightsuid_gacha as command

        credential = _cred()
        event = SimpleNamespace(text="全量", user_id="u", bot_id="b")
        with (
            mock.patch.object(command, "resolve_or_reply", return_value=credential),
            mock.patch.object(command, "sync_gacha_logs", return_value=([], "暂无")) as sync,
            mock.patch.object(command, "_send_text", new=mock.AsyncMock()),
        ):
            await command.show_gacha_report(None, event)
        sync.assert_awaited_once_with(credential, full=True)
