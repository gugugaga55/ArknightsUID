from __future__ import annotations

import json
import os
import time
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

    async def __aenter__(self) -> FakeAsyncClient:
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
        self.original_get_client = prts_client.get_client
        self.original_cache_path = prts_client.PRTS_CACHE_PATH
        self.original_last_cleanup_at = prts_client._last_cleanup_at
        prts_client.logger = self.logger
        fake_client = FakeAsyncClient()

        async def fake_get_client():
            return fake_client

        prts_client.get_client = fake_get_client  # type: ignore[assignment]
        prts_client.PRTS_CACHE_PATH = self.cache_root
        # 重置节流时间戳，确保本测试用例能立即触发清理扫描。
        prts_client._last_cleanup_at = 0.0
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []
        # PRTS 最小请求间隔在测试中归零（避免每个用例固定 sleep 0.5s）
        self.original_min_interval = prts_client._MIN_REQUEST_INTERVAL
        prts_client._MIN_REQUEST_INTERVAL = 0.0
        prts_client._last_request_at = 0.0

    def tearDown(self) -> None:
        prts_client.logger = self.original_logger
        prts_client.get_client = self.original_get_client  # type: ignore[assignment]
        prts_client._MIN_REQUEST_INTERVAL = self.original_min_interval
        prts_client.PRTS_CACHE_PATH = self.original_cache_path
        prts_client._last_cleanup_at = self.original_last_cleanup_at
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
        # 缓存写入走统一磁盘缓存层（utils.disk_cache）：底层 _write_sync
        # 失败由 cache_write 内部吞掉（debug 类名-only 日志），不影响 _api 返回
        from ArknightsUID.utils import disk_cache as disk_cache_module

        def failing_write_sync(path: Any, payload: Any) -> None:
            raise RuntimeError(
                "cache path failure "
                f"cookie={SENTINEL_CRED} token={SENTINEL_TOKEN}"
            )

        original_write_sync = disk_cache_module._write_sync
        disk_cache_module._write_sync = failing_write_sync  # type: ignore[assignment]
        cache_logger = RecordingLogger()
        original_cache_logger = disk_cache_module.logger
        disk_cache_module.logger = cache_logger
        FakeAsyncClient.queue = [FakeResponse(200, {"query": {"search": []}})]
        try:
            result = await prts_client._api({"action": "query"}, cache_seconds=86400)
        finally:
            disk_cache_module._write_sync = original_write_sync  # type: ignore[assignment]
            disk_cache_module.logger = original_cache_logger

        self.assertEqual(result, {"query": {"search": []}})
        cache_logs = "\n".join(cache_logger.debugs + cache_logger.warnings)
        self.assertIn("写入失败", cache_logs)
        self.assertIn("RuntimeError", cache_logs)
        assert_log_safe(self, cache_logs)

    async def test_stale_cache_cleanup_removes_old_files_and_keeps_recent(self) -> None:
        # 旧缓存：mtime 设为 30 天前；新缓存：当前时间。
        # 触发一次 _api 调用（缓存命中），验证清理扫描删旧留新。
        old_params = {"action": "query", "list": "search", "srsearch": "old"}
        new_params = {"action": "query", "list": "search", "srsearch": "new"}
        old_file = prts_client._cache_file(old_params)
        new_file = prts_client._cache_file(new_params)
        old_payload = {"query": {"search": [{"title": "旧条目"}]}}
        new_payload = {"query": {"search": [{"title": "新条目"}]}}
        old_file.write_text(
            json.dumps({"_cache_time": time.time(), "payload": old_payload}),
            encoding="utf-8",
        )
        new_file.write_text(
            json.dumps({"_cache_time": time.time(), "payload": new_payload}),
            encoding="utf-8",
        )
        # 用 os.utime 把旧文件 mtime 拨到 30 天前，新文件保持当前。
        thirty_days_ago = time.time() - 30 * 86400
        os.utime(old_file, (thirty_days_ago, thirty_days_ago))

        # new 命中缓存，触发清理（setUp 已重置节流）。
        result = await prts_client._api(new_params, cache_seconds=86400)

        self.assertEqual(result, new_payload)
        self.assertFalse(old_file.exists(), "旧缓存应被清理删除")
        self.assertTrue(new_file.exists(), "近期缓存应保留")
        # 清理失败的日志不应泄漏敏感信息。
        assert_log_safe(self, self.all_logs())


if __name__ == "__main__":
    unittest.main()
