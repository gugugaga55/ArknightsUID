from __future__ import annotations

import json
import sys
import time
import types
import unittest
import tempfile
from pathlib import Path
from typing import Any


class RecordingLogger:
    def __init__(self) -> None:
        self.debugs: list[str] = []
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def _install_gsuid_core_stubs() -> tempfile.TemporaryDirectory[str]:
    class FakeSV:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def on_command(self, *args: Any, **kwargs: Any) -> Any:
            return lambda func: func

        def on_fullmatch(self, *args: Any, **kwargs: Any) -> Any:
            return lambda func: func

    temp_root = tempfile.TemporaryDirectory()
    bot_module = types.ModuleType("gsuid_core.bot")
    bot_module.Bot = object
    models_module = types.ModuleType("gsuid_core.models")
    models_module.Event = object
    logger_module = types.ModuleType("gsuid_core.logger")
    logger_module.logger = RecordingLogger()
    data_store_module = types.ModuleType("gsuid_core.data_store")
    data_store_module.get_res_path = lambda name: Path(temp_root.name) / name
    help_module = types.ModuleType("gsuid_core.help")
    help_module.__path__ = []
    help_utils_module = types.ModuleType("gsuid_core.help.utils")
    help_utils_module.register_help = lambda *args, **kwargs: None
    sv_module = types.ModuleType("gsuid_core.sv")
    sv_module.Plugins = lambda *args, **kwargs: None
    sv_module.SV = FakeSV

    gsuid_core_module = types.ModuleType("gsuid_core")
    gsuid_core_module.__path__ = []
    sys.modules.setdefault("gsuid_core", gsuid_core_module)
    sys.modules["gsuid_core.bot"] = bot_module
    sys.modules["gsuid_core.models"] = models_module
    sys.modules["gsuid_core.logger"] = logger_module
    sys.modules["gsuid_core.data_store"] = data_store_module
    sys.modules["gsuid_core.help"] = help_module
    sys.modules["gsuid_core.help.utils"] = help_utils_module
    sys.modules["gsuid_core.sv"] = sv_module
    return temp_root


_STUB_TEMP = _install_gsuid_core_stubs()

from ArknightsUID.arknightsuid_wiki import (  # noqa: E402
    _clip,
    _format_enemy,
    _format_page,
    _format_stage,
)
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
            raise AssertionError("unexpected PRTS request")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class PRTSParserTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_root = Path(self.temp_dir.name) / "prts"
        self.cache_root.mkdir()
        self.original_api = prts_client._api
        self.original_cache_path = prts_client.PRTS_CACHE_PATH
        self.original_async_client = prts_client.httpx.AsyncClient
        prts_client.PRTS_CACHE_PATH = self.cache_root
        prts_client.httpx.AsyncClient = FakeAsyncClient
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []

    def tearDown(self) -> None:
        prts_client._api = self.original_api  # type: ignore[assignment]
        prts_client.PRTS_CACHE_PATH = self.original_cache_path
        prts_client.httpx.AsyncClient = self.original_async_client
        self.temp_dir.cleanup()

    async def test_redirect_target_handles_case_whitespace_alias_and_rejects_normal_page(self) -> None:
        self.assertEqual(
            prts_client._redirect_target(" \n #REDIRECT  [[阿米娅|兔兔]] "),
            "阿米娅",
        )
        self.assertEqual(
            prts_client._redirect_target("#redirect [[龙门币]]"),
            "龙门币",
        )
        self.assertEqual(prts_client._redirect_target("== 标题 ==\n正文"), "")

    async def test_parse_page_follows_redirects_and_can_return_redirect_page(self) -> None:
        calls: list[str] = []
        payloads = {
            "兔兔": {
                "parse": {
                    "title": "兔兔",
                    "wikitext": {"*": "#REDIRECT [[阿米娅|Amiya]]"},
                }
            },
            "阿米娅": {
                "parse": {
                    "title": "阿米娅",
                    "wikitext": {"*": "罗德岛公开领导人。"},
                }
            },
        }

        async def fake_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            calls.append(str(params["page"]))
            return payloads[str(params["page"])]

        prts_client._api = fake_api  # type: ignore[assignment]

        followed = await prts_client.parse_page("兔兔")
        not_followed = await prts_client.parse_page("兔兔", follow_redirect=False)

        self.assertEqual(followed, prts_client.PageWikitext("阿米娅", "罗德岛公开领导人。"))
        self.assertEqual(not_followed, prts_client.PageWikitext("兔兔", "#REDIRECT [[阿米娅|Amiya]]"))
        self.assertEqual(calls, ["兔兔", "阿米娅", "兔兔"])

    async def test_parse_page_handles_empty_title_missing_wikitext_and_redirect_loop(self) -> None:
        self.assertIsNone(await prts_client.parse_page("  "))

        async def missing_wikitext(
            params: dict[str, Any],
            *,
            cache_seconds: int = 86400,
        ) -> dict[str, Any]:
            return {"parse": {"title": str(params["page"])}}

        prts_client._api = missing_wikitext  # type: ignore[assignment]
        self.assertIsNone(await prts_client.parse_page("缺失页面"))

        async def loop_api(params: dict[str, Any], *, cache_seconds: int = 86400) -> dict[str, Any]:
            page = str(params["page"])
            target = "B" if page == "A" else "A"
            return {"parse": {"title": page, "wikitext": {"*": f"#REDIRECT [[{target}]]"}}}

        prts_client._api = loop_api  # type: ignore[assignment]
        self.assertIsNone(await prts_client.parse_page("A"))

    async def test_clean_wiki_value_removes_html_templates_links_and_whitespace_noise(self) -> None:
        raw = """
        '''阿米娅'''&nbsp;&amp; <span class="x">罗德岛</span><br/>
        [[干员/阿米娅|领导人]] {{颜色|蓝|文本}} [https://example.invalid 来源]
        <!-- hidden --><references/>
        <div>公开资料</div>
        """

        cleaned = prts_client._clean_wiki_value(raw)

        self.assertIn("阿米娅 & 罗德岛", cleaned)
        self.assertIn("领导人 文本 来源", cleaned)
        self.assertIn("公开资料", cleaned)
        for marker in ("<span", "<div", "{{", "}}", "[[", "]]", "hidden", "references"):
            self.assertNotIn(marker, cleaned)
        self.assertNotIn("  ", cleaned)

    async def test_formatters_clip_long_text_and_keep_source_links(self) -> None:
        long_summary = "摘要" * 500
        long_field = "字段" * 200
        page_reply = _format_page(
            "词条",
            prts_client.WikiPageSummary(
                title="阿米娅",
                source_url=prts_client.page_url("阿米娅"),
                summary=long_summary,
                fields={"名称": long_field, "描述": long_field},
            ),
        )
        self.assertIn("...", page_reply)
        self.assertIn("来源：https://prts.wiki/w/", page_reply)
        self.assertLess(len(page_reply.splitlines()[1]), len(long_summary))

        enemy_reply = _format_enemy(
            prts_client.EnemyInfo(
                title="源石虫",
                name="源石虫",
                description="描述" * 200,
                levels=[
                    prts_client.EnemyLevel(
                        max_hp="1000",
                        abilities=["能力" * 200],
                    )
                ],
                source_url=prts_client.page_url("源石虫"),
            )
        )
        self.assertIn("...", enemy_reply)
        self.assertIn("来源：https://prts.wiki/w/", enemy_reply)

        stage_reply = _format_stage(
            prts_client.StageInfo(
                title="1-7",
                code="1-7",
                name="暴君",
                description="关卡描述" * 200,
                drops=[f"掉落{i}" for i in range(12)],
                extra_drops=[f"额外{i}" for i in range(12)],
                enemies=[
                    prts_client.StageEnemy(name=f"敌人{i}", count=str(i))
                    for i in range(12)
                ],
                source_url=prts_client.page_url("1-7"),
                map_url=prts_client.map_url("main_01-07"),
            )
        )
        self.assertIn("...", stage_reply)
        self.assertIn("地图：https://mapen.ark-nights.com/map/main_01-07", stage_reply)
        self.assertIn("来源：https://prts.wiki/w/", stage_reply)
        self.assertNotIn("掉落8", stage_reply)
        self.assertNotIn("敌人10", stage_reply)

    async def test_formatters_return_safe_fallbacks_for_missing_fields(self) -> None:
        page_reply = _format_page(
            "词条",
            prts_client.WikiPageSummary(title="未知页", source_url="https://example.invalid/source"),
        )
        self.assertIn("未能解析到摘要字段", page_reply)
        self.assertIn("来源：https://example.invalid/source", page_reply)

        enemy_reply = _format_enemy(
            prts_client.EnemyInfo(title="未知敌人", source_url="https://example.invalid/enemy")
        )
        self.assertIn("「明日方舟」敌人：未知敌人", enemy_reply)
        self.assertIn("来源：https://example.invalid/enemy", enemy_reply)

        stage_reply = _format_stage(
            prts_client.StageInfo(title="未知关卡", source_url="https://example.invalid/stage")
        )
        self.assertIn("「明日方舟」关卡：未知关卡", stage_reply)
        self.assertIn("来源：https://example.invalid/stage", stage_reply)

    async def test_api_stale_cache_fallback_uses_temp_cache_only(self) -> None:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": "../阿米娅",
            "srlimit": 5,
        }
        payload = {"query": {"search": [{"title": "阿米娅"}]}}
        cache_file = prts_client._cache_file(params)
        cache_file.write_text(
            json.dumps({"_cache_time": time.time() - 999999, "payload": payload}),
            encoding="utf-8",
        )
        FakeAsyncClient.queue = [RuntimeError("network unavailable")]

        result = await prts_client._api(params, cache_seconds=0)

        self.assertEqual(result, payload)
        self.assertEqual(cache_file.parent, self.cache_root)
        self.assertNotIn("..", cache_file.name)
        self.assertEqual(len(FakeAsyncClient.requests), 1)


if __name__ == "__main__":
    unittest.main()
