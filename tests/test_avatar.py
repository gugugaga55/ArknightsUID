"""QQ 头像助手（utils/avatar.py）测试。

覆盖：平台 URL 选择（onebot/qqgroup/其他）、非法 user_id、企鹅占位
md5 识别、正常图片 data URI、网络失败降级空串。
"""
from __future__ import annotations

import base64
import hashlib
import unittest
from unittest import mock

from ArknightsUID.utils import avatar as avatar_mod
from ArknightsUID.utils.avatar import user_avatar_b64

# 最小合法 JPEG 头 + 填充（避开 <400 字节的小响应判定）
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 1024
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024


class _Resp:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass


def _client_with(content: bytes):
    client = mock.AsyncMock()
    client.get.return_value = _Resp(content)
    return client


class AvatarTest(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_url_and_jpeg_mime(self) -> None:
        client = _client_with(JPEG_BYTES)
        with mock.patch(
            "ArknightsUID.utils.http.get_client", return_value=client
        ):
            out = await user_avatar_b64("onebot", "123456")
        self.assertTrue(out.startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(out.split(",", 1)[1]), JPEG_BYTES)
        url = client.get.await_args.args[0]
        self.assertIn("q1.qlogo.cn", url)
        self.assertIn("nk=123456", url)

    async def test_qqgroup_url_and_png_mime(self) -> None:
        client = _client_with(PNG_BYTES)
        with mock.patch(
            "ArknightsUID.utils.http.get_client", return_value=client
        ):
            out = await user_avatar_b64("qqgroup", "123456", "9999")
        self.assertTrue(out.startswith("data:image/png;base64,"))
        url = client.get.await_args.args[0]
        self.assertIn("q.qlogo.cn/qqapp/9999/123456", url)

    async def test_other_platform_returns_empty(self) -> None:
        self.assertEqual(await user_avatar_b64("discord", "123456"), "")
        self.assertEqual(await user_avatar_b64("onebot", ""), "")
        self.assertEqual(await user_avatar_b64("onebot", "not-a-qq号"), "")

    async def test_qq_default_avatar_treated_as_missing(self) -> None:
        """企鹅占位图（无头像号）按 md5 识别 → 空串走模板兜底。"""
        digest = hashlib.md5(JPEG_BYTES).hexdigest()
        with mock.patch.object(
            avatar_mod, "_QQ_DEFAULT_AVATAR_MD5", frozenset({digest})
        ):
            with mock.patch(
                "ArknightsUID.utils.http.get_client",
                return_value=_client_with(JPEG_BYTES),
            ):
                self.assertEqual(await user_avatar_b64("onebot", "123456"), "")

    async def test_tiny_response_treated_as_missing(self) -> None:
        with mock.patch(
            "ArknightsUID.utils.http.get_client",
            return_value=_client_with(b"\xff\xd8"),
        ):
            self.assertEqual(await user_avatar_b64("onebot", "123456"), "")

    async def test_network_failure_returns_empty(self) -> None:
        client = mock.AsyncMock()
        client.get.side_effect = OSError("boom")
        with mock.patch(
            "ArknightsUID.utils.http.get_client", return_value=client
        ):
            self.assertEqual(await user_avatar_b64("onebot", "123456"), "")


if __name__ == "__main__":
    unittest.main()
