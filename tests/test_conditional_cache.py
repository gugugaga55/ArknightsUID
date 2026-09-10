"""条件请求 diff 刷新（ETag/If-None-Match）：近永久数据表零流量复核。

背景：敌人库/全量表内容随版本更新才变化，30 天/7 天 TTL 的语义应是
「复核间隔」而非「数据寿命」——复核走条件 GET，上游没变（304）时
不重新下载、不重新解析，只把缓存时间戳续期；变了（200）才全量换。
校验器与来源 URL 一并落盘旁车（多镜像 ETag 不互通，条件请求必须回源）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))


class _FakeResp:
    def __init__(self, status: int, content: bytes | None, headers: dict):
        self.status_code = status
        self.content = content
        self.headers = headers


class DownloadBytesConditionalTest(unittest.TestCase):
    """http 层：条件头发送 / 304 判定 / 200 回收校验器 / quiet_404。"""

    def _run(self, resp, **kw):
        from ArknightsUID.utils import http as http_mod

        captured: dict = {}

        class FakeClient:
            async def get(self, url, **k):
                captured["url"] = url
                captured["headers"] = k.get("headers")
                return resp

        gc = mock.AsyncMock(return_value=FakeClient())
        with mock.patch.object(http_mod, "get_client", gc):
            result = asyncio.run(http_mod.download_bytes_conditional("https://x/y.json", **kw))
        return captured, result

    def test_conditional_headers_sent(self) -> None:
        captured, _ = self._run(
            _FakeResp(304, None, {}),
            etag='"v1"',
            last_modified="Mon, 01 Sep 2026 00:00:00 GMT",
        )
        self.assertEqual(captured["headers"]["If-None-Match"], '"v1"')
        self.assertEqual(
            captured["headers"]["If-Modified-Since"],
            "Mon, 01 Sep 2026 00:00:00 GMT",
        )

    def test_304_means_unchanged(self) -> None:
        _, result = self._run(_FakeResp(304, None, {}))
        self.assertEqual(result, (304, None, {}))

    def test_200_returns_content_and_validators(self) -> None:
        _, result = self._run(
            _FakeResp(
                200,
                b'{"k": 2}',
                {"ETag": '"v2"', "Last-Modified": "Wed, 09 Sep 2026 00:00:00 GMT"},
            )
        )
        status, data, validators = result
        self.assertEqual((status, data), (200, b'{"k": 2}'))
        self.assertEqual(
            validators,
            {"etag": '"v2"', "last_modified": "Wed, 09 Sep 2026 00:00:00 GMT"},
        )

    def test_no_validators_sends_plain_get(self) -> None:
        captured, result = self._run(_FakeResp(200, b"x", {"ETag": '"a"'}))
        self.assertIsNone(captured["headers"])
        self.assertEqual(result[2], {"etag": '"a"'})


class DiskCacheValidatorsTest(unittest.TestCase):
    """旁车校验器读写回路 + 旧格式/损坏容错。"""

    def test_roundtrip_and_missing(self) -> None:
        import tempfile

        from ArknightsUID.utils.disk_cache import (
            cache_read_validators,
            cache_write,
        )

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.json"
            asyncio.run(
                cache_write(
                    p,
                    {"a": 1},
                    validators={"etag": '"v9"', "source_url": "https://m/x"},
                )
            )
            self.assertEqual(
                asyncio.run(cache_read_validators(p)),
                {"etag": '"v9"', "source_url": "https://m/x"},
            )
            # 无旁车（旧格式缓存） → {}
            p2 = Path(td) / "old.json"
            p2.write_text(json.dumps({"_cache_time": 1, "payload": {}}), "utf-8")
            self.assertEqual(asyncio.run(cache_read_validators(p2)), {})
            # 不存在 → {}
            self.assertEqual(
                asyncio.run(cache_read_validators(Path(td) / "nope.json")), {}
            )

    def test_corrupt_sidecar_returns_empty(self) -> None:
        import tempfile

        from ArknightsUID.utils.disk_cache import cache_read_validators

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{}", "utf-8")
            p.with_name("bad.json.validators.json").write_text("{broken", "utf-8")
            self.assertEqual(asyncio.run(cache_read_validators(p)), {})


class TableCacheDiffTest(unittest.TestCase):
    """_table_cache：复核到点 + 304 → 零下载续期；200 → 换新。"""

    def _seed(self, path: Path, payload: dict, validators: dict | None) -> None:
        from ArknightsUID.utils.disk_cache import cache_write

        asyncio.run(cache_write(path, payload, validators=validators))

    def test_304_renews_without_full_download(self) -> None:
        import tempfile

        from ArknightsUID.utils.disk_cache import cache_read, is_fresh
        from ArknightsUID.utils.gamedata import _table_cache as tc

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "table.json"
            self._seed(path, {"v": 1}, {"etag": '"e1"', "source_url": "https://raw/x"})
            calls: list[dict] = []

            async def fake_cond(url, *, etag=None, last_modified=None, timeout=0.0, **kw):
                calls.append({"url": url, "etag": etag})
                return 304, None, {}

            async def fail_full(url):
                raise AssertionError("304 命中时不得走全量下载")

            with (
                mock.patch.object(tc, "download_bytes_conditional", fake_cond),
                mock.patch.object(tc, "_download_json", fail_full),
            ):
                result = asyncio.run(
                    tc._ensure_table("https://raw/x", path, ttl=0.0, force=True)
                )

            self.assertEqual(result, {"v": 1})
            self.assertEqual(calls, [{"url": "https://raw/x", "etag": '"e1"'}])
            _, saved = asyncio.run(cache_read(path))
            self.assertTrue(is_fresh(saved, 3600), "304 复核后应续期时间戳")

    def test_200_replaces_payload(self) -> None:
        import tempfile

        from ArknightsUID.utils.gamedata import _table_cache as tc

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "table.json"
            self._seed(path, {"v": 1}, {"etag": '"e1"', "source_url": "https://raw/x"})

            async def fake_cond(url, **kw):
                return 200, b'{"v": 2}', {"etag": '"e2"'}

            with (
                mock.patch.object(tc, "download_bytes_conditional", fake_cond),
                mock.patch.object(tc, "_download_json", mock.AsyncMock(return_value=(None, {}))),
            ):
                result = asyncio.run(
                    tc._ensure_table("https://raw/x", path, ttl=0.0, force=True)
                )
            self.assertEqual(result, {"v": 2})

    def test_no_validators_falls_to_full_download(self) -> None:
        import tempfile

        from ArknightsUID.utils.gamedata import _table_cache as tc

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "table.json"
            self._seed(path, {"v": 1}, None)  # 历史缓存：无校验器

            async def no_cond(*a, **kw):
                raise AssertionError("无校验器不应发条件请求")

            async def fake_full(url):
                return {"v": 3}, {"etag": '"e3"', "source_url": url}

            with (
                mock.patch.object(tc, "download_bytes_conditional", no_cond),
                mock.patch.object(tc, "_download_json", fake_full),
            ):
                result = asyncio.run(
                    tc._ensure_table("https://raw/x", path, ttl=0.0, force=True)
                )
            self.assertEqual(result, {"v": 3})


class EnemyJsonDiffTest(unittest.TestCase):
    """rogue_enemy：敌人库 JSON 同款 304 diff。"""

    def test_304_serves_old_and_renews(self) -> None:
        import tempfile
        import time

        from ArknightsUID.utils.disk_cache import cache_read, cache_write, is_fresh
        from ArknightsUID.utils.gamedata import rogue_enemy as re_mod

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "enemy_database.json"
            asyncio.run(
                cache_write(
                    path,
                    {"enemy_1": {}},
                    validators={"etag": '"e1"', "source_url": "https://tomimi/db"},
                )
            )
            # 复核到点：信封时间戳做旧（旁车不动）
            path.write_text(
                json.dumps(
                    {"_cache_time": time.time() - 2 * 86400, "payload": {"enemy_1": {}}}
                ),
                encoding="utf-8",
            )
            calls: list[str] = []

            async def fake_cond(url, **kw):
                calls.append(url)
                return 304, None, {}

            async def no_status(*a, **kw):
                raise AssertionError("304 命中时不得走普通 GET 兜底")

            with (
                mock.patch.object(re_mod, "download_bytes_conditional", fake_cond),
                mock.patch.object(re_mod, "download_bytes_status", no_status),
            ):
                result = asyncio.run(
                    re_mod._fetch_url_json_cached("https://tomimi/db", path)
                )
            self.assertEqual(result, {"enemy_1": {}})
            self.assertEqual(calls, ["https://tomimi/db"])
            _, saved = asyncio.run(cache_read(path))
            self.assertTrue(is_fresh(saved, 3600))

    def test_conditional_4xx_no_second_request(self) -> None:
        """条件 GET 得 4xx 与普通 GET 同语义，不得对同一 URL 二次请求。"""
        import tempfile
        import time

        from ArknightsUID.utils.gamedata import rogue_enemy as re_mod

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "enemy_database.json"
            asyncio.run(
                re_mod.cache_write(
                    path,
                    {"enemy_1": {}},
                    validators={"etag": '"e1"', "source_url": "https://tomimi/db"},
                )
            )
            path.write_text(
                json.dumps(
                    {"_cache_time": time.time() - 2 * 86400, "payload": {"enemy_1": {}}}
                ),
                encoding="utf-8",
            )
            calls: list[int] = []

            async def fake_cond(url, **kw):
                calls.append(1)
                return 404, None, {}

            with mock.patch.object(
                re_mod, "download_bytes_conditional", fake_cond
            ):
                result = asyncio.run(
                    re_mod._fetch_url_json_cached("https://tomimi/db", path)
                )
            self.assertEqual(calls, [1], "4xx 后不得再落普通 GET 重复请求")
            self.assertEqual(result, {"enemy_1": {}}, "有旧缓存时 4xx 服务过期数据")


if __name__ == "__main__":
    unittest.main()
