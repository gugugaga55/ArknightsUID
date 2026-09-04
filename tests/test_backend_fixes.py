"""后端接口修复的回归测试（对应 2026-08 接口层审阅）。

覆盖：
1. token 缓存/锁/回写：过期 token 不再每条命令三连往返
2. resolve_record_uid 复用统一 binding 解析（gameId==1 过滤）
3. 抽卡翻页游标失效（末条无 ts）时停止而非从头重拉
4. 统一磁盘缓存层：旧格式兼容 + 原子写 + TTL 判定
5. 共享 HTTP 层 request_json 的结果形状
6. PRTS 查询链 deadline 耗尽后不再逐页解析
7. RespCode 业务码收敛（未接线成员/HttpCode 已随死代码清理移除）
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from ArknightsUID.utils import disk_cache
from ArknightsUID.utils.api import request_util
from ArknightsUID.utils.api.request_util import RespCode
from ArknightsUID.utils.hypergryph import client as hyper_client
from ArknightsUID.utils.hypergryph import gacha as gacha_svc
from ArknightsUID.utils.skland import client as skland_client


@dataclass(slots=True)
class Credential:
    uid: str = "123456789"
    cred: str = "SECRET_CRED"
    token: str = ""
    record_id: str = ""


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}"

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeAsyncClient:
    queue: list[Any] = []
    requests: list[tuple[str, str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url, kwargs)

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url, kwargs)

    @classmethod
    def _next(cls, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        cls.requests.append((method, url, kwargs))
        if not cls.queue:
            raise AssertionError("unexpected HTTP request")
        item = cls.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def install_fake_client(module: Any) -> tuple[Any, Any]:
    """把 module.get_client 替换为返回队列桩；返回 (原函数, 桩实例)。"""
    original = module.get_client
    fake_client = FakeAsyncClient()

    async def fake_get_client() -> Any:
        return fake_client

    module.get_client = fake_get_client
    return original, fake_client


class TokenCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        skland_client._token_cache.clear()
        skland_client._refresh_locks.clear()
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []
        self._patcher = mock.patch.object(skland_client, "_persist_token")

    def tearDown(self) -> None:
        skland_client._token_cache.clear()
        skland_client._refresh_locks.clear()
        original, _ = install_fake_client(skland_client)
        skland_client.get_client = original
        self._patcher.stop()

    async def test_second_request_reuses_cached_token(self) -> None:
        """token 过期后：首次 refresh + 回写缓存，第二次直接命中缓存单请求。"""
        install_fake_client(skland_client)
        persist = self._patcher.start()
        ok_body = {"code": 0, "data": {"ok": True}}
        FakeAsyncClient.queue = [
            # 第一次调用：refresh → 业务
            FakeResponse(200, {"code": 0, "data": {"token": "FRESH_TOKEN"}}),
            FakeResponse(200, ok_body),
            # 第二次调用：应直接业务请求（无 refresh）
            FakeResponse(200, ok_body),
        ]

        first = await skland_client._signed_request(
            Credential(), url=skland_client.GAME_PLAYER_INFO_URL, uid="1", game_id=1
        )
        second = await skland_client._signed_request(
            Credential(), url=skland_client.GAME_PLAYER_INFO_URL, uid="1", game_id=1
        )

        self.assertEqual(first, ok_body)
        self.assertEqual(second, ok_body)
        # 3 个请求里只有 1 次是 refresh；回写只发生一次
        refresh_count = sum(
            1 for _m, url, _kw in FakeAsyncClient.requests
            if url == skland_client.REFRESH_TOKEN_URL
        )
        self.assertEqual(refresh_count, 1)
        self.assertEqual(len(FakeAsyncClient.requests), 3)
        persist.assert_awaited_once_with("SECRET_CRED", "FRESH_TOKEN")
        self.assertEqual(
            skland_client._token_cache["SECRET_CRED"][0], "FRESH_TOKEN"
        )

    async def test_token_invalid_forces_refresh_even_with_warm_cache(self) -> None:
        """服务端提前吊销缓存 token（220）→ 强制 refresh 而非复用缓存。"""
        install_fake_client(skland_client)
        persist = self._patcher.start()
        # 预热缓存（模拟另一命令刚刷新过）
        skland_client._token_cache["SECRET_CRED"] = ("STALE_TOKEN", time.time())
        FakeAsyncClient.queue = [
            FakeResponse(200, {"code": 220}),                       # 缓存 token 被判失效
            FakeResponse(200, {"code": 0, "data": {"token": "NEW"}}),  # 强制 refresh
            FakeResponse(200, {"code": 0, "data": {"ok": True}}),   # 重试成功
        ]

        result = await skland_client._signed_request(
            Credential(token=""), url=skland_client.GAME_PLAYER_INFO_URL,
            uid="1", game_id=1,
        )

        self.assertEqual(result, {"code": 0, "data": {"ok": True}})
        refresh_count = sum(
            1 for _m, url, _kw in FakeAsyncClient.requests
            if url == skland_client.REFRESH_TOKEN_URL
        )
        self.assertEqual(refresh_count, 1)
        persist.assert_awaited_once()
        self.assertEqual(skland_client._token_cache["SECRET_CRED"][0], "NEW")

    async def test_concurrent_force_refresh_single_flight(self) -> None:
        """token 失效瞬间的并发强制刷新：只 refresh 一次，其余复用新 token。

        回归：旧实现 force 路径跳过锁内双检，N 个并发 220 → N 次 refresh；
        若服务端单活 token，第二次 refresh 会吊销第一个协程刚拿到的 token。
        """
        install_fake_client(skland_client)
        self._patcher.start()
        # 两个协程各自带着同一个失效 token 触发 force 刷新
        FakeAsyncClient.queue = [
            FakeResponse(200, {"code": 0, "data": {"token": "NEW_TOKEN_1"}}),
        ]
        results = await asyncio.gather(
            skland_client._token_for_cred("SECRET_CRED", force=True, stale="OLD"),
            skland_client._token_for_cred("SECRET_CRED", force=True, stale="OLD"),
        )
        self.assertEqual(results, ["NEW_TOKEN_1", "NEW_TOKEN_1"])
        refresh_count = sum(
            1 for _m, url, _kw in FakeAsyncClient.requests
            if url == skland_client.REFRESH_TOKEN_URL
        )
        self.assertEqual(refresh_count, 1)

    async def test_force_refresh_refreshes_again_when_cache_still_stale(self) -> None:
        """缓存里仍是刚失败的那个 token → 必须真正 refresh（不得误复用）。"""
        install_fake_client(skland_client)
        self._patcher.start()
        skland_client._token_cache["SECRET_CRED"] = ("OLD", time.time())
        FakeAsyncClient.queue = [
            FakeResponse(200, {"code": 0, "data": {"token": "NEW_TOKEN_2"}}),
        ]
        token = await skland_client._token_for_cred(
            "SECRET_CRED", force=True, stale="OLD"
        )
        self.assertEqual(token, "NEW_TOKEN_2")


class ResolveRecordUidTest(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_role_matching_credential_uid(self) -> None:
        roles = [
            {"role_id": "111", "record_uid": "R1", "nickname": "a", "server_id": "1"},
            {"role_id": "222", "record_uid": "R2", "nickname": "b", "server_id": "1"},
        ]
        with mock.patch.object(hyper_client, "list_ark_roles_by_cred", return_value=roles):
            record_uid = await hyper_client.resolve_record_uid(
                Credential(uid="222", cred="C")
            )
        self.assertEqual(record_uid, "R2")

    async def test_falls_back_to_first_game1_role_record_uid(self) -> None:
        """binding 里没有当前 uid 的角色时，取首条（gameId==1 由统一解析保证）。"""
        roles = [{"role_id": "999", "record_uid": "R9", "nickname": "x", "server_id": "1"}]
        with mock.patch.object(hyper_client, "list_ark_roles_by_cred", return_value=roles):
            record_uid = await hyper_client.resolve_record_uid(
                Credential(uid="111", cred="C")
            )
        self.assertEqual(record_uid, "R9")

    async def test_record_id_short_circuits(self) -> None:
        credential = Credential(uid="1", cred="C", record_id="STORED")
        with mock.patch.object(
            hyper_client, "list_ark_roles_by_cred", side_effect=AssertionError
        ):
            self.assertEqual(await hyper_client.resolve_record_uid(credential), "STORED")


class GachaCursorTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_cursor_stops_pagination(self) -> None:
        """末条 gachaTs 缺失（归一后为 0）→ 停止翻页，而非从头重拉。"""
        pages = {
            ("cat", None, None): {
                "list": [
                    {"charId": "A", "charName": "A", "rarity": 5,
                     "gachaTs": None, "pos": 1},
                ],
                "hasMore": True,
            }
        }
        calls: list[tuple] = []

        async def fake_page(_cred, _auth, category, gacha_ts=None, pos=None):
            calls.append((category, gacha_ts, pos))
            return pages.get((category, gacha_ts, pos))

        auth_stub = hyper_client.ArkGachaAuth("hg", "u8", "cookie")
        self.assertTrue(auth_stub.ready)
        with mock.patch.object(
            gacha_svc, "load_local_logs", return_value=[]
        ), mock.patch.object(
            gacha_svc, "save_local_logs"
        ), mock.patch.object(
            hyper_client, "login_gacha_chain", return_value=auth_stub
        ), mock.patch.object(
            hyper_client, "get_gacha_categories",
            return_value=[{"id": "cat", "name": "x"}],
        ), mock.patch.object(
            hyper_client, "get_gacha_history_page", fake_page
        ):
            records, warning = await gacha_svc.sync_gacha_logs(Credential())

        self.assertEqual(warning, "")
        self.assertEqual(len(records), 1)
        # 游标失效后立刻停止：只请求了第一页
        self.assertEqual(calls, [("cat", None, None)])


class DiskCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "wrapped.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_roundtrip_and_freshness(self) -> None:
        await disk_cache.cache_write(self.path, {"k": "v"})
        payload, saved_ts = await disk_cache.cache_read(self.path)
        self.assertEqual(payload, {"k": "v"})
        self.assertGreater(saved_ts, 0)
        self.assertTrue(disk_cache.is_fresh(saved_ts, 60))
        self.assertFalse(disk_cache.is_fresh(saved_ts - 120, 60))
        self.assertFalse(disk_cache.is_fresh(0, 60))

    async def test_reads_legacy_wrapped_format(self) -> None:
        """旧 prts/yituliu/gamedata 缓存文件（同包装格式）可直接读取。"""
        self.path.write_text(
            json.dumps(
                {"_cache_time": time.time(), "payload": [{"legacy": 1}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        payload, saved_ts = await disk_cache.cache_read(self.path)
        self.assertEqual(payload, [{"legacy": 1}])
        self.assertGreater(saved_ts, 0)

    async def test_atomic_write_leaves_no_tmp(self) -> None:
        await disk_cache.cache_write(self.path, {"a": 1})
        leftovers = [p for p in self.path.parent.iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    async def test_corrupt_file_returns_none(self) -> None:
        self.path.write_text("not-json{", encoding="utf-8")
        payload, saved_ts = await disk_cache.cache_read(self.path)
        self.assertIsNone(payload)
        self.assertEqual(saved_ts, 0)


class RequestJsonShapeTest(unittest.IsolatedAsyncioTestCase):
    async def test_result_tuple_and_cookie_extraction(self) -> None:
        from ArknightsUID.utils import http as http_layer

        class Headers:
            @staticmethod
            def get_list(name: str) -> list[str]:
                return ["ak-user-center=abc; Path=/"] if name == "set-cookie" else []

        class Resp:
            status_code = 200
            headers = Headers()

            @staticmethod
            def json() -> dict[str, Any]:
                return {"code": 0}

        class Client:
            @staticmethod
            async def get(url: str, **kwargs: Any) -> Resp:
                return Resp()

        async def fake_get_client() -> Any:
            return Client()

        with mock.patch.object(http_layer, "get_client", fake_get_client):
            result = await http_layer.request_json("GET", "https://x.example/api")

        self.assertEqual(result.status, 200)
        self.assertEqual(result.data, {"code": 0})
        self.assertEqual(result.cookies, ["ak-user-center=abc; Path=/"])

    async def test_network_error_returns_zero_status(self) -> None:
        from ArknightsUID.utils import http as http_layer

        class BrokenClient:
            @staticmethod
            async def get(url: str, **kwargs: Any) -> Any:
                raise RuntimeError("boom")

        async def fake_get_client() -> Any:
            return BrokenClient()

        with mock.patch.object(http_layer, "get_client", fake_get_client):
            result = await http_layer.request_json("GET", "https://x.example/api")

        self.assertEqual(result.status, 0)
        self.assertIsNone(result.data)
        self.assertEqual(result.cookies, [])


class PRTSDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_stage_stops_when_deadline_expired(self) -> None:
        """deadline 耗尽后不再逐个解析搜索候选页。"""
        from ArknightsUID.utils.prts import client as prts_client

        parse_calls: list[str] = []

        async def fake_parse_page(title, *, follow_redirect=True, deadline=None):
            parse_calls.append(title)
            return None

        async def fake_search(query: str, limit: int = 5):
            return [prts_client.SearchResult(title=f"候选{i}") for i in range(8)]

        with mock.patch.object(prts_client, "parse_page", fake_parse_page), \
                mock.patch.object(prts_client, "search_pages", fake_search), \
                mock.patch.object(
                    prts_client, "_new_deadline", return_value=0.0
                ):
            result = await prts_client.query_stage("1-7")

        self.assertIsNone(result)
        # deadline=0：直查一次后，回退循环因超时立即退出
        self.assertEqual(len(parse_calls), 1)


class RespCodeNamespaceTest(unittest.TestCase):
    def test_business_codes_kept_and_unused_removed(self) -> None:
        self.assertEqual(int(RespCode.OK), 0)
        self.assertEqual(int(RespCode.TOKEN_INVALID), 220)
        self.assertEqual(int(RespCode.REQUEST_ERROR), 10000)
        self.assertEqual(int(RespCode.LOGIN_EXPIRED), 10002)
        # 未接线的成员（TIME_ERROR/CAPTCHA_*/HttpCode）已随死代码清理移除
        for member in ("TIME_ERROR", "CAPTCHA_ERROR", "CAPTCHA_EXPIRED"):
            self.assertFalse(hasattr(RespCode, member))
        self.assertFalse(hasattr(request_util, "HttpCode"))


if __name__ == "__main__":
    unittest.main()
