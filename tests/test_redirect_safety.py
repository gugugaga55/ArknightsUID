"""敏感请求禁用自动重定向回归测试（2026-08 审计问题 #5）。

背景：共享 httpx.AsyncClient 默认 follow_redirects=True。森空岛链路请求
携带 cred / sign / dId 等自定义头（auth 链还带 token body）；httpx 跨域
重定向只摘除 Authorization/Cookie，自定义头与 307/308 的请求体会被原样
重放给新 Origin——一旦上游出现开放重定向即等于把凭据交给第三方。

hypergryph 链路本就显式关闭；本文件锁住 skland 链路（client/auth/web）
与 request_json 的 no-follow 行为，防止回归。
"""
from __future__ import annotations

import unittest
from unittest import mock

import httpx

from ArknightsUID.utils import http as ark_http
from ArknightsUID.utils.skland import auth as sk_auth
from ArknightsUID.utils.skland import client as sk_client
from ArknightsUID.utils.skland import web as sk_web
from ArknightsUID.utils.http import RequestResult


def _redirect_client() -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """307 跨域重定向的假 client；记录收到的全部请求。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                307,
                headers={"Location": "https://evil.example.com/steal"},
            )
        return httpx.Response(200, json={"code": 0, "data": {}})

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, follow_redirects=False), seen


class RequestJsonNoFollowTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_follow_returns_307_without_replay(self) -> None:
        client, seen = _redirect_client()
        with mock.patch.object(ark_http, "get_client", new=mock.AsyncMock(return_value=client)):
            result = await ark_http.request_json(
                "GET",
                "https://zonai.skland.com/api/v1/test",
                headers={"cred": "c" * 32, "sign": "s"},
                follow_redirects=False,
            )
        await client.aclose()

        self.assertEqual(result.status, 307)
        self.assertEqual(len(seen), 1, "不得向重定向目标重发请求")
        self.assertEqual(
            seen[0].url.host, "zonai.skland.com"
        )


class SklandChainNoFollowTest(unittest.IsolatedAsyncioTestCase):
    """skland 三条链路的请求都必须显式关闭自动重定向。"""

    async def test_auth_request_disables_redirects(self) -> None:
        captured: dict = {}

        async def fake_request_json(method, url, **kwargs):
            captured.update(kwargs, method=method, url=url)
            return RequestResult(200, {"status": 0, "data": {}}, [])

        with mock.patch.object(
            sk_auth, "request_json", new=fake_request_json
        ):
            status, data = await sk_auth._request(
                "POST", "https://as.hypergryph.com/user/oauth2/v2/grant",
                headers={"Authorization": "Bearer t"}, body={"token": "t"},
            )
        self.assertEqual(status, 200)
        self.assertFalse(captured["follow_redirects"])

    async def test_web_http_get_json_disables_redirects(self) -> None:
        captured: dict = {}

        async def fake_request_json(method, url, **kwargs):
            captured.update(kwargs, method=method, url=url)
            return RequestResult(200, {"code": 0}, [])

        with mock.patch.object(
            sk_web, "request_json", new=fake_request_json
        ):
            data = await sk_web._http_get_json(
                "https://zonai.skland.com/api/v1/web/x", headers={}
            )
        self.assertEqual(data, {"code": 0})
        self.assertFalse(captured["follow_redirects"])

    async def test_refresh_token_does_not_follow_cross_origin(self) -> None:
        client, seen = _redirect_client()
        with mock.patch.object(
            sk_client, "get_client", new=mock.AsyncMock(return_value=client)
        ), mock.patch.object(sk_client, "_REFRESH_RETRY_DELAY", 0):
            token = await sk_client.refresh_token("c" * 32)
        await client.aclose()

        # 307 不跟随 → 非 200 → 重试仍 307 → 最终返回空串；凭据头只发给 skland
        self.assertEqual(token, "")
        self.assertTrue(seen)
        self.assertTrue(
            all(r.url.host == "zonai.skland.com" for r in seen),
            f"请求被重放给了重定向目标: {[str(r.url) for r in seen]}",
        )


class HypergryphBaselineTest(unittest.TestCase):
    def test_hypergryph_client_keeps_no_follow(self) -> None:
        """鹰角链路既有防线不得回退（源码级断言）。"""
        import inspect

        from ArknightsUID.utils.hypergryph import client as hg_client

        source = inspect.getsource(hg_client._request)
        self.assertIn("follow_redirects=False", source)


if __name__ == "__main__":
    unittest.main()
