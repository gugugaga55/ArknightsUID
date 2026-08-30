"""OCR 图片 URL 安全边界回归测试（审计 P1-1：SSRF / 无界下载 / 解压炸弹）。

背景：事件图片 URL 是用户可控输入，此前直接交给默认跟随重定向、无响应
体上限的 download_bytes——可探测回环/私网/云元数据地址，且恶意大响应
直接整读进内存。

本文件锁住：
- assert_public_http_url：非 http(s)、回环/私网/链路本地/元数据/IPv6 内网拒绝
- safe_download_bytes：逐跳重定向校验、字节上限、正常下载放行
- recruit_ocr._materialize：http 走安全下载、base64 解码前后双层限长
- 图片魔数校验拒绝 HTML/JSON 错误页
"""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils import http as ark_http
from ArknightsUID.utils.http import UnsafeUrlError


class AssertPublicHttpUrlTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_http_schemes(self) -> None:
        for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://x"):
            with self.assertRaises(UnsafeUrlError, msg=url):
                await ark_http.assert_public_http_url(url)

    async def test_rejects_private_and_special_addresses(self) -> None:
        for url in (
            "http://127.0.0.1/x",
            "http://localhost/x",          # 解析到回环
            "http://10.0.0.5/x",
            "http://192.168.1.1/x",
            "http://172.16.0.1/x",
            "http://169.254.169.254/latest/meta-data/",  # 云元数据
            "http://[::1]/x",
            "http://[fe80::1]/x",          # 链路本地
            "http://0.0.0.0/x",
            "http://224.0.0.1/x",          # 多播
        ):
            with self.assertRaises(UnsafeUrlError, msg=url):
                await ark_http.assert_public_http_url(url)

    async def test_accepts_public_ip_literal(self) -> None:
        # IP 字面量不触发 DNS 查询
        self.assertEqual(
            await ark_http.assert_public_http_url("https://93.184.216.34/a"),
            "https://93.184.216.34/a",
        )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class SafeDownloadTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, handler, url: str, **kwargs):
        client = _client(handler)
        try:
            with mock.patch.object(
                ark_http, "get_client", new=mock.AsyncMock(return_value=client)
            ):
                return await ark_http.safe_download_bytes(url, **kwargs)
        finally:
            await client.aclose()

    async def test_redirect_to_private_target_rejected(self) -> None:
        """重定向到私网地址：下一跳校验拒绝，绝不发请求。"""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if len(seen) == 1:
                return httpx.Response(
                    302, headers={"Location": "http://192.168.0.1/steal"}
                )
            return httpx.Response(200, content=b"x")

        # 首跳用公网 IP 字面量（免 DNS）；目标跳私网
        data = await self._run(handler, "http://93.184.216.34/img.png")
        self.assertIsNone(data)
        self.assertEqual(len(seen), 1, "不得向私网重定向目标发请求")

    async def test_oversized_response_truncated_to_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"a" * 4096)

        data = await self._run(handler, "http://93.184.216.34/big", max_bytes=1024)
        self.assertIsNone(data)

    async def test_normal_download_passes(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=png)

        data = await self._run(handler, "http://93.184.216.34/shot.png")
        self.assertEqual(data, png)

    async def test_cross_origin_same_host_redirect_ok(self) -> None:
        """公网 → 公网的重定向允许逐跳跟随（每跳重新校验）。"""
        png = b"\x89PNG\r\n\x1a\nrest"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/a":
                return httpx.Response(
                    307, headers={"Location": "http://93.184.216.34/b"}
                )
            return httpx.Response(200, content=png)

        data = await self._run(handler, "http://93.184.216.34/a")
        self.assertEqual(data, png)


class MaterializeBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_url_uses_safe_download(self) -> None:
        from ArknightsUID.utils import recruit_ocr

        # _materialize 在函数内 from .http import —— patch http 模块属性生效
        with mock.patch.object(
            ark_http, "safe_download_bytes",
            new=mock.AsyncMock(return_value=b"\x89PNG"),
        ) as m_safe:
            data = await recruit_ocr._materialize("http://93.184.216.34/s.png")
        self.assertEqual(data, b"\x89PNG")
        m_safe.assert_awaited_once()

    async def test_base64_input_size_capped(self) -> None:
        from ArknightsUID.utils import recruit_ocr

        # 裸 base64 超 _MAX_IMAGE_BYTES → 拒绝
        huge = base64.b64encode(b"a" * (recruit_ocr._MAX_IMAGE_BYTES + 1)).decode()
        self.assertIsNone(await recruit_ocr._materialize(huge))
        # data: URL 同样受限
        self.assertIsNone(await recruit_ocr._materialize(f"data:image/png;base64,{huge}"))

        ok = base64.b64encode(b"\x89PNGdata").decode()
        self.assertEqual(
            await recruit_ocr._materialize(f"base64://{ok}"), b"\x89PNGdata"
        )

    def test_magic_number_check_rejects_non_image(self) -> None:
        from ArknightsUID.utils.recruit_ocr import _looks_like_image

        for bad in (b"<html>err</html>", b'{"error":1}', b"", b"not-image"):
            self.assertFalse(_looks_like_image(bad), bad)
        for good in (b"\x89PNG\r\n\x1a\n....", b"\xff\xd8\xff\xe0jpeg",
                     b"RIFF\x00\x00\x00\x00WEBPVP8 ", b"GIF89a...", b"BM\x36"):
            self.assertTrue(_looks_like_image(good), good[:4])


if __name__ == "__main__":
    unittest.main()
