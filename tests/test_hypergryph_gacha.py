"""鹰角 OAuth 链（hypergryph/client.py）+ 抽卡数据服务（hypergryph/gacha.py）测试。

httpx 全 mock（替换 client._request，与 test_skland_auth 同模式）；
抽卡本地存储重定向到临时目录，不污染真实资源路径。
"""
from __future__ import annotations

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
        self.assertEqual(stats["recent"][0], {
            "name": "D", "pool": "限定寻访·集结", "pull": 2,
            "ts": 103, "category": "2",
        })
        # B：类别 1 内第 2 抽
        self.assertEqual(stats["recent"][1]["pull"], 2)

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
        self.assertEqual(ctx["pity_max"], 80)
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
