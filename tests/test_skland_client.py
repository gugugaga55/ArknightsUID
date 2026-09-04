from __future__ import annotations

import unittest
from typing import Any
from dataclasses import dataclass

from ArknightsUID.utils.skland import client as skland_client


class RecordingLogger:
    def __init__(self) -> None:
        self.debugs: list[str] = []
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


@dataclass(slots=True)
class Credential:
    uid: str = "123456789"
    cred: str = "SECRET_CRED"
    token: str = "SECRET_TOKEN"


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        *,
        content: bytes = b"{}",
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self._json_error = json_error

    def json(self) -> dict[str, Any]:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def refresh_ok(token: str) -> FakeResponse:
    return FakeResponse(200, {"code": 0, "message": "OK", "data": {"token": token}})


class FakeAsyncClient:
    queue: list[Any] = []
    requests: list[tuple[str, str, dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

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


class SklandClientTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.logger = RecordingLogger()
        self.original_logger = skland_client.logger
        # _query_string 已上移 utils/api/request_util（签名原语共享层），
        # 其告警经 request_util.logger 发出——一并替换才能捕获
        import ArknightsUID.utils.api.request_util as request_util

        self.original_ru_logger = request_util.logger
        request_util.logger = self.logger
        self.original_get_client = skland_client.get_client
        self.original_retry_delay = skland_client._REFRESH_RETRY_DELAY
        self.original_mark_invalid = skland_client._mark_invalid
        self.original_persist_token = skland_client._persist_token
        skland_client.logger = self.logger
        # 共享 client 层的测试缝：get_client() 返回队列桩
        fake_client = FakeAsyncClient()

        async def fake_get_client():
            return fake_client

        skland_client.get_client = fake_get_client  # type: ignore[assignment]
        # 重试间隔打 0，避免测试真实 sleep
        skland_client._REFRESH_RETRY_DELAY = 0.0
        # 默认挡掉 _mark_invalid/_persist_token 的 DB 访问（相关用例内再替换）
        async def noop_mark_invalid(cred: str) -> None:
            return None

        skland_client._mark_invalid = noop_mark_invalid  # type: ignore[assignment]

        async def noop_persist_token(cred: str, token: str) -> None:
            return None

        skland_client._persist_token = noop_persist_token  # type: ignore[assignment]
        # 模块级 token 缓存/锁跨测试会泄漏（同 cred 命中旧缓存导致不发请求）
        skland_client._token_cache.clear()
        skland_client._refresh_locks.clear()
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []

    def tearDown(self) -> None:
        skland_client.logger = self.original_logger
        import ArknightsUID.utils.api.request_util as request_util

        request_util.logger = self.original_ru_logger
        skland_client.get_client = self.original_get_client  # type: ignore[assignment]
        skland_client._REFRESH_RETRY_DELAY = self.original_retry_delay
        skland_client._mark_invalid = self.original_mark_invalid  # type: ignore[assignment]
        skland_client._persist_token = self.original_persist_token  # type: ignore[assignment]
        skland_client._token_cache.clear()
        skland_client._refresh_locks.clear()

    def warning_text(self) -> str:
        return "\n".join(self.logger.warnings)

    # === _query_string：'=' 误报修复 ===

    def test_query_string_plain_params_do_not_warn(self) -> None:
        query = skland_client._query_string({"uid": "540840237", "gameId": 1})

        self.assertEqual(query, "gameId=1&uid=540840237")
        self.assertEqual(self.logger.warnings, [])

    def test_query_string_warns_on_genuinely_unsafe_chars(self) -> None:
        skland_client._query_string({"q": "a b/c"})

        self.assertIn("特殊字符", self.warning_text())

    # === refresh_token：重试与凭证失效（对齐 EndUID）===

    async def test_refresh_token_rejects_non_200_without_using_payload_token(self) -> None:
        FakeAsyncClient.queue = [
            FakeResponse(
                500,
                {"code": 0, "data": {"token": "SHOULD_NOT_USE"}},
            )
        ] * skland_client._REFRESH_MAX_ATTEMPTS

        token = await skland_client.refresh_token("SECRET_CRED")

        self.assertEqual(token, "")
        self.assertIn("HTTP 500", self.warning_text())
        self.assertNotIn("SHOULD_NOT_USE", self.warning_text())
        self.assertNotIn("SECRET_CRED", self.warning_text())
        self.assertEqual(len(FakeAsyncClient.requests), 3)

    async def test_refresh_token_non_json_log_does_not_leak_credentials(self) -> None:
        FakeAsyncClient.queue = [
            FakeResponse(
                200,
                content=b"not-json",
                json_error=ValueError("cred=SECRET_CRED token=SECRET_TOKEN"),
            )
        ] * skland_client._REFRESH_MAX_ATTEMPTS

        token = await skland_client.refresh_token("SECRET_CRED")

        self.assertEqual(token, "")
        self.assertIn("non-json", self.warning_text())
        self.assertNotIn("SECRET_CRED", self.warning_text())
        self.assertNotIn("SECRET_TOKEN", self.warning_text())

    async def test_refresh_token_retries_then_succeeds(self) -> None:
        FakeAsyncClient.queue = [
            FakeResponse(500, {}),
            FakeResponse(200, content=b"not-json", json_error=ValueError("bad")),
            refresh_ok("FRESH_TOKEN"),
        ]

        token = await skland_client.refresh_token("SECRET_CRED")

        self.assertEqual(token, "FRESH_TOKEN")
        self.assertEqual(len(FakeAsyncClient.requests), 3)

    async def test_refresh_token_cred_invalid_raises_without_retry(self) -> None:
        FakeAsyncClient.queue = [
            FakeResponse(200, {"code": 10001, "message": "cred invalid"})
        ]

        with self.assertRaises(skland_client.CredentialInvalidError):
            await skland_client.refresh_token("SECRET_CRED")

        self.assertEqual(len(FakeAsyncClient.requests), 1)

    # === token 新鲜度窗口（180 秒，对齐 EndUID）===

    async def test_token_cache_window_skips_refresh_within_180s(self) -> None:
        FakeAsyncClient.queue = [refresh_ok("T1")]

        first = await skland_client._token_for_cred("SECRET_CRED")
        second = await skland_client._token_for_cred("SECRET_CRED")

        self.assertEqual((first, second), ("T1", "T1"))
        self.assertEqual(len(FakeAsyncClient.requests), 1)

        # 缓存老化越过窗口 → 重新 refresh
        token, ts = skland_client._token_cache["SECRET_CRED"]
        skland_client._token_cache["SECRET_CRED"] = (token, ts - 181)
        FakeAsyncClient.queue = [refresh_ok("T2")]

        third = await skland_client._token_for_cred("SECRET_CRED")

        self.assertEqual(third, "T2")
        self.assertEqual(len(FakeAsyncClient.requests), 2)
        self.assertEqual(FakeAsyncClient.requests[1][1], skland_client.REFRESH_TOKEN_URL)

    # === _signed_request 主流程 ===

    async def test_signed_request_error_log_does_not_leak_credentials(self) -> None:
        FakeAsyncClient.queue = [
            refresh_ok("NET_TOKEN"),
            RuntimeError("headers={'cred':'SECRET_CRED','token':'NET_TOKEN'}"),
        ]

        result = await skland_client._signed_request(
            Credential(),
            url=skland_client.GAME_PLAYER_INFO_URL,
            uid="123456789",
            game_id=skland_client.ARKNIGHTS_GAME_ID,
        )

        self.assertIsNone(result)
        self.assertNotIn("SECRET_CRED", self.warning_text())
        self.assertNotIn("SECRET_TOKEN", self.warning_text())
        self.assertNotIn("NET_TOKEN", self.warning_text())

    async def test_signed_request_refreshes_and_retries_token_invalid_response(self) -> None:
        FakeAsyncClient.queue = [
            refresh_ok("T1"),  # 首发 refresh（新策略：不新鲜就刷）
            FakeResponse(200, {"code": 220, "message": "token invalid"}),
            refresh_ok("T2"),  # 220 → 强制 refresh
            FakeResponse(200, {"code": 0, "data": {"ok": True}}),
        ]

        result = await skland_client._signed_request(
            Credential(),
            url=skland_client.GAME_PLAYER_INFO_URL,
            uid="123456789",
            game_id=skland_client.ARKNIGHTS_GAME_ID,
        )

        self.assertEqual(result, {"code": 0, "data": {"ok": True}})
        self.assertEqual(len(FakeAsyncClient.requests), 4)
        self.assertEqual(FakeAsyncClient.requests[0][1], skland_client.REFRESH_TOKEN_URL)
        self.assertEqual(FakeAsyncClient.requests[2][1], skland_client.REFRESH_TOKEN_URL)

    async def test_signed_request_marks_invalid_when_refresh_rejected(self) -> None:
        marked: list[str] = []

        async def fake_mark_invalid(cred: str) -> None:
            marked.append(cred)

        skland_client._mark_invalid = fake_mark_invalid  # type: ignore[assignment]
        FakeAsyncClient.queue = [
            FakeResponse(200, {"code": 10001, "message": "cred invalid"})
        ]

        result = await skland_client._signed_request(
            Credential(),
            url=skland_client.GAME_PLAYER_INFO_URL,
            uid="123456789",
            game_id=skland_client.ARKNIGHTS_GAME_ID,
        )

        self.assertIsNone(result)
        self.assertEqual(marked, ["SECRET_CRED"])
        self.assertIn("凭证失效", self.warning_text())

    # === 补全接口（6 GET）请求形态测试 ===

    async def test_get_user_info_hits_user_endpoint_without_query(self) -> None:
        FakeAsyncClient.queue = [
            refresh_ok("T"),
            FakeResponse(200, {"code": 0, "data": {"nickname": "x"}}),
        ]

        result = await skland_client.get_user_info(Credential())

        self.assertEqual(result, {"code": 0, "data": {"nickname": "x"}})
        method, url, _kwargs = FakeAsyncClient.requests[-1]
        self.assertEqual(method, "GET")
        self.assertIn("/api/v1/user", url)
        # 无 params：url 不应拼 query string
        self.assertNotIn("?", url)
        self.assertNotIn("SECRET_CRED", url)

    async def test_get_binding_hits_binding_endpoint_without_query(self) -> None:
        FakeAsyncClient.queue = [
            refresh_ok("T"),
            FakeResponse(200, {"code": 0, "data": []}),
        ]

        result = await skland_client.get_binding(Credential())

        self.assertIsNotNone(result)
        method, url, _kwargs = FakeAsyncClient.requests[-1]
        self.assertEqual(method, "GET")
        self.assertIn("/api/v1/game/player/binding", url)
        self.assertNotIn("?", url)

    async def test_ark_attendance_query_uses_get_with_uid_and_gameid(self) -> None:
        FakeAsyncClient.queue = [
            refresh_ok("T"),
            FakeResponse(200, {"code": 0, "data": {}}),
        ]

        await skland_client.ark_attendance_query(Credential())

        method, url, _kwargs = FakeAsyncClient.requests[-1]
        self.assertEqual(method, "GET")
        self.assertIn("/api/v1/game/attendance", url)
        # GET params 被 _query_string 按 key 排序拼进 url（client.py 签名链路）
        self.assertIn("gameId=1", url)
        self.assertIn("uid=123456789", url)

    async def test_get_cultivate_character_passes_character_id(self) -> None:
        FakeAsyncClient.queue = [
            refresh_ok("T"),
            FakeResponse(200, {"code": 0, "data": {}}),
        ]

        await skland_client.get_cultivate_character(Credential(), "char_002_amiya")

        method, url, _kwargs = FakeAsyncClient.requests[-1]
        self.assertEqual(method, "GET")
        self.assertIn("/api/v1/game/cultivate/character", url)
        self.assertIn("characterId=char_002_amiya", url)

    async def test_get_cultivate_info_hits_info_endpoint_without_query(self) -> None:
        FakeAsyncClient.queue = [
            refresh_ok("T"),
            FakeResponse(200, {"code": 0, "data": {}}),
        ]

        result = await skland_client.get_cultivate_info(Credential())

        self.assertIsNotNone(result)
        method, url, _kwargs = FakeAsyncClient.requests[-1]
        self.assertEqual(method, "GET")
        self.assertIn("/api/v1/game/cultivate/info", url)
        self.assertNotIn("?", url)

    async def test_get_cultivate_player_passes_uid(self) -> None:
        FakeAsyncClient.queue = [
            refresh_ok("T"),
            FakeResponse(200, {"code": 0, "data": {}}),
        ]

        await skland_client.get_cultivate_player(Credential())

        method, url, _kwargs = FakeAsyncClient.requests[-1]
        self.assertEqual(method, "GET")
        self.assertIn("/api/v1/game/cultivate/player", url)
        self.assertIn("uid=123456789", url)

    async def test_get_rogue_records_passes_topic_and_target(self) -> None:
        """肉鸽战绩端点（森空岛 App 战绩页同源）：uid/targetUserId/topicId 传递。

        topicId 用主题表键 rogue_N（与社区 nonebot-plugin-skland 一致）；
        签名组合未经真实凭据联调（见函数 docstring），这里只锁参数链路。
        """
        FakeAsyncClient.queue = [
            refresh_ok("T"),
            FakeResponse(200, {"code": 0, "data": {}}),
        ]

        await skland_client.get_rogue_records(Credential(), topic_id="rogue_6")

        method, url, _kwargs = FakeAsyncClient.requests[-1]
        self.assertEqual(method, "GET")
        self.assertIn("/api/v1/game/arknights/rogue", url)
        self.assertIn("uid=123456789", url)
        # target 缺省查自己
        self.assertIn("targetUserId=123456789", url)
        self.assertIn("topicId=rogue_6", url)
