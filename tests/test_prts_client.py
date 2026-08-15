from __future__ import annotations

import json
import sys
import time
import types
import unittest
import tempfile
from pathlib import Path
from typing import Any


SENTINEL_CRED = "SENTINEL_CRED_DO_NOT_LEAK"
SENTINEL_TOKEN = "SENTINEL_TOKEN_DO_NOT_LEAK"
SENTINEL_HG_TOKEN = "SENTINEL_HG_TOKEN_DO_NOT_LEAK"
SENTINEL_SKLAND_USER = "SENTINEL_SKLAND_USER_DO_NOT_LEAK"
SENSITIVE_VALUES = (
    SENTINEL_CRED,
    SENTINEL_TOKEN,
    SENTINEL_HG_TOKEN,
    SENTINEL_SKLAND_USER,
)
SENSITIVE_FIELD_NAMES = ("cookie", "cred", "token", "hg_token", "skland_user_id")


class RecordingLogger:
    def __init__(self) -> None:
        self.debugs: list[str] = []
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


_module_temp = tempfile.TemporaryDirectory()

logger_module = types.ModuleType("gsuid_core.logger")
logger_module.logger = RecordingLogger()
data_store_module = types.ModuleType("gsuid_core.data_store")
data_store_module.get_res_path = lambda name: Path(_module_temp.name) / name
sv_module = types.ModuleType("gsuid_core.sv")
sv_module.Plugins = lambda *args, **kwargs: None
gsuid_core_module = types.ModuleType("gsuid_core")
gsuid_core_module.__path__ = []
sys.modules.setdefault("gsuid_core", gsuid_core_module)
sys.modules["gsuid_core.logger"] = logger_module
sys.modules["gsuid_core.data_store"] = data_store_module
sys.modules["gsuid_core.sv"] = sv_module

from ArknightsUID.utils.prts import client as prts_client  # noqa: E402


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
            raise AssertionError("unexpected PRTS HTTP request")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def assert_log_safe(testcase: unittest.TestCase, text: str) -> None:
    for value in SENSITIVE_VALUES:
        testcase.assertNotIn(value, text)
    lowered = text.lower()
    for field_name in SENSITIVE_FIELD_NAMES:
        testcase.assertNotIn(field_name, lowered)


class PRTSClientSafetyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_root = Path(self.temp_dir.name) / "prts"
        self.cache_root.mkdir()
        self.logger = RecordingLogger()
        self.original_logger = prts_client.logger
        self.original_async_client = prts_client.httpx.AsyncClient
        self.original_cache_path = prts_client.PRTS_CACHE_PATH
        prts_client.logger = self.logger
        prts_client.httpx.AsyncClient = FakeAsyncClient
        prts_client.PRTS_CACHE_PATH = self.cache_root
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []

    def tearDown(self) -> None:
        prts_client.logger = self.original_logger
        prts_client.httpx.AsyncClient = self.original_async_client
        prts_client.PRTS_CACHE_PATH = self.original_cache_path
        self.temp_dir.cleanup()

    def all_logs(self) -> str:
        return "\n".join(self.logger.debugs + self.logger.warnings)

    async def test_api_failure_log_is_safe_and_returns_stale_cache(self) -> None:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": f"../Amiya?token={SENTINEL_TOKEN}",
            "srlimit": 5,
        }
        payload = {"query": {"search": [{"title": "阿米娅"}]}}
        cache_file = prts_client._cache_file(params)
        cache_file.write_text(
            json.dumps({"_cache_time": time.time() - 999999, "payload": payload}),
            encoding="utf-8",
        )
        FakeAsyncClient.queue = [
            RuntimeError(
                "request failed "
                f"cred={SENTINEL_CRED} token={SENTINEL_TOKEN} "
                f"hg_token={SENTINEL_HG_TOKEN} "
                f"skland_user_id={SENTINEL_SKLAND_USER}"
            )
        ]

        result = await prts_client._api(params, cache_seconds=0)

        self.assertEqual(result, payload)
        self.assertEqual(cache_file.parent, self.cache_root)
        self.assertEqual(cache_file.suffix, ".json")
        self.assertNotIn("..", cache_file.name)
        self.assertNotIn(SENTINEL_TOKEN, str(cache_file))
        self.assertIn("RuntimeError", self.all_logs())
        assert_log_safe(self, self.all_logs())

    async def test_cache_save_failure_log_is_safe(self) -> None:
        class FailingCachePath:
            def exists(self) -> bool:
                return False

            def write_text(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError(
                    "cache path failure "
                    f"cookie={SENTINEL_CRED} token={SENTINEL_TOKEN}"
                )

        original_cache_file = prts_client._cache_file
        prts_client._cache_file = lambda params: FailingCachePath()  # type: ignore[assignment]
        FakeAsyncClient.queue = [FakeResponse(200, {"query": {"search": []}})]
        try:
            result = await prts_client._api({"action": "query"}, cache_seconds=86400)
        finally:
            prts_client._cache_file = original_cache_file  # type: ignore[assignment]

        self.assertEqual(result, {"query": {"search": []}})
        self.assertIn("RuntimeError", self.all_logs())
        assert_log_safe(self, self.all_logs())


if __name__ == "__main__":
    unittest.main()
