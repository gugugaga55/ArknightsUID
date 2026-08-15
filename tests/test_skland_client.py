from __future__ import annotations

import sys
import types
import unittest
from dataclasses import dataclass
from typing import Any


class RecordingLogger:
    def __init__(self) -> None:
        self.debugs: list[str] = []
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


logger_module = types.ModuleType("gsuid_core.logger")
logger_module.logger = RecordingLogger()
sv_module = types.ModuleType("gsuid_core.sv")
sv_module.Plugins = lambda *args, **kwargs: None
gsuid_core_module = types.ModuleType("gsuid_core")
gsuid_core_module.__path__ = []
sys.modules.setdefault("gsuid_core", gsuid_core_module)
sys.modules["gsuid_core.logger"] = logger_module
sys.modules["gsuid_core.sv"] = sv_module

from ArknightsUID.utils.skland import client as skland_client  # noqa: E402


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
        self.original_async_client = skland_client.httpx.AsyncClient
        skland_client.logger = self.logger
        skland_client.httpx.AsyncClient = FakeAsyncClient
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []

    def tearDown(self) -> None:
        skland_client.logger = self.original_logger
        skland_client.httpx.AsyncClient = self.original_async_client

    def warning_text(self) -> str:
        return "\n".join(self.logger.warnings)

    async def test_refresh_token_rejects_non_200_without_using_payload_token(self) -> None:
        FakeAsyncClient.queue = [
            FakeResponse(
                500,
                {"code": 0, "data": {"token": "SHOULD_NOT_USE"}},
            )
        ]

        token = await skland_client.refresh_token("SECRET_CRED")

        self.assertEqual(token, "")
        self.assertIn("HTTP 500", self.warning_text())
        self.assertNotIn("SHOULD_NOT_USE", self.warning_text())
        self.assertNotIn("SECRET_CRED", self.warning_text())

    async def test_refresh_token_non_json_log_does_not_leak_credentials(self) -> None:
        FakeAsyncClient.queue = [
            FakeResponse(
                200,
                content=b"not-json",
                json_error=ValueError("cred=SECRET_CRED token=SECRET_TOKEN"),
            )
        ]

        token = await skland_client.refresh_token("SECRET_CRED")

        self.assertEqual(token, "")
        self.assertIn("non-json", self.warning_text())
        self.assertNotIn("SECRET_CRED", self.warning_text())
        self.assertNotIn("SECRET_TOKEN", self.warning_text())

    async def test_signed_request_error_log_does_not_leak_credentials(self) -> None:
        FakeAsyncClient.queue = [
            RuntimeError("headers={'cred':'SECRET_CRED','token':'SECRET_TOKEN'}")
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

    async def test_signed_request_refreshes_and_retries_token_invalid_response(self) -> None:
        FakeAsyncClient.queue = [
            FakeResponse(200, {"code": 220, "message": "token invalid"}),
            FakeResponse(200, {"code": 0, "data": {"token": "FRESH_TOKEN"}}),
            FakeResponse(200, {"code": 0, "data": {"ok": True}}),
        ]

        result = await skland_client._signed_request(
            Credential(),
            url=skland_client.GAME_PLAYER_INFO_URL,
            uid="123456789",
            game_id=skland_client.ARKNIGHTS_GAME_ID,
        )

        self.assertEqual(result, {"code": 0, "data": {"ok": True}})
        self.assertEqual(len(FakeAsyncClient.requests), 3)
        self.assertEqual(FakeAsyncClient.requests[1][1], skland_client.REFRESH_TOKEN_URL)


if __name__ == "__main__":
    unittest.main()
