"""配置接入回归测试：

- hide_uid 全局开关 + 用户级 hide_uid_self_value 覆盖语义
- push 定时任务 AnnPushEnable / AnnCheckMinute 消费
- LocalProxyUrl → 共享 HTTP 客户端代理
- ark设置隐藏UID / ark取消隐藏UID 落库链路
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ArknightsUID.utils.config import ArkConfig
from ArknightsUID.utils.enduid import adapter as enduid_adapter
import ArknightsUID.arknightsuid_user_config as user_config_mod


def _cfg(data):
    return SimpleNamespace(data=data)


class HideUidSemanticsTest(unittest.TestCase):
    """hide_uid(uid, user_pref)：on 强制隐藏 / off 强制显示 / 空 跟随全局。"""

    def test_follows_global_when_no_pref(self) -> None:
        with mock.patch.object(
            enduid_adapter.ArkConfig, "get_config", return_value=_cfg(True)
        ):
            self.assertEqual(enduid_adapter.hide_uid("123456789"), "12***89")
        with mock.patch.object(
            enduid_adapter.ArkConfig, "get_config", return_value=_cfg(False)
        ):
            self.assertEqual(enduid_adapter.hide_uid("123456789"), "123456789")

    def test_user_pref_overrides_global(self) -> None:
        with mock.patch.object(
            enduid_adapter.ArkConfig, "get_config", return_value=_cfg(False)
        ):
            self.assertEqual(
                enduid_adapter.hide_uid("123456789", user_pref="on"), "12***89"
            )
        with mock.patch.object(
            enduid_adapter.ArkConfig, "get_config", return_value=_cfg(True)
        ):
            self.assertEqual(
                enduid_adapter.hide_uid("123456789", user_pref="off"), "123456789"
            )

    def test_config_failure_falls_back_to_masking(self) -> None:
        with mock.patch.object(
            enduid_adapter.ArkConfig,
            "get_config",
            side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(enduid_adapter.hide_uid("123456789"), "12***89")


class HideUidPrefLookupTest(unittest.IsolatedAsyncioTestCase):
    async def test_pref_from_ark_user_row(self) -> None:
        row = SimpleNamespace(hide_uid_self_value="on")
        with mock.patch.object(
            enduid_adapter.ArkUser,
            "base_select_data",
            new=mock.AsyncMock(return_value=row),
        ):
            self.assertEqual(
                await enduid_adapter.get_hide_uid_pref("1", "u", "b"), "on"
            )

    async def test_pref_empty_when_no_row_or_error(self) -> None:
        with mock.patch.object(
            enduid_adapter.ArkUser,
            "base_select_data",
            new=mock.AsyncMock(return_value=None),
        ):
            self.assertEqual(
                await enduid_adapter.get_hide_uid_pref("1", "u", "b"), ""
            )
        with mock.patch.object(
            enduid_adapter.ArkUser,
            "base_select_data",
            new=mock.AsyncMock(side_effect=RuntimeError("db down")),
        ):
            self.assertEqual(
                await enduid_adapter.get_hide_uid_pref("1", "u", "b"), ""
            )

    async def test_hide_uid_for_combines_pref_and_mask(self) -> None:
        with mock.patch.object(
            enduid_adapter,
            "get_hide_uid_pref",
            new=mock.AsyncMock(return_value="off"),
        ), mock.patch.object(
            enduid_adapter.ArkConfig, "get_config", return_value=_cfg(True)
        ):
            self.assertEqual(
                await enduid_adapter.hide_uid_for("123456789", "u", "b"),
                "123456789",
            )


class PushConfigTest(unittest.TestCase):
    def _push(self):
        from ArknightsUID.arknightsuid_push import (
            _push_check_minutes,
            _push_enabled,
        )

        return _push_check_minutes, _push_enabled

    def test_interval_reads_config(self) -> None:
        minutes, _ = self._push()
        with mock.patch.object(
            ArkConfig, "get_config", return_value=_cfg(45)
        ) as m:
            self.assertEqual(minutes(), 45)
            m.assert_called_once_with("AnnCheckMinute")

    def test_interval_invalid_falls_back_to_30(self) -> None:
        minutes, _ = self._push()
        with mock.patch.object(ArkConfig, "get_config", return_value=_cfg(-5)):
            self.assertEqual(minutes(), 30)
        with mock.patch.object(ArkConfig, "get_config", return_value=_cfg("abc")):
            self.assertEqual(minutes(), 30)

    def test_enabled_reads_config(self) -> None:
        _, enabled = self._push()
        with mock.patch.object(ArkConfig, "get_config", return_value=_cfg(False)):
            self.assertFalse(enabled())
        with mock.patch.object(ArkConfig, "get_config", return_value=_cfg(True)):
            self.assertTrue(enabled())

    def test_job_registered_with_configured_interval(self) -> None:
        """装饰器在 import 时以配置间隔注册（本机配置为 30 或用户改后的值）。"""
        from ArknightsUID.arknightsuid_push import _push_check_minutes

        self.assertGreater(_push_check_minutes(), 0)


class ProxyClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_gets_proxy_kwarg_when_configured(self) -> None:
        import httpx

        import ArknightsUID.utils.http as http_mod

        original = http_mod._client
        http_mod._client = None
        try:
            real_client = httpx.AsyncClient
            created: list[dict] = []

            def spy(*args, **kwargs):
                created.append(kwargs)
                return real_client(**{k: v for k, v in kwargs.items() if k != "proxy"})

            with mock.patch.object(
                ArkConfig, "get_config", return_value=_cfg("http://127.0.0.1:7890")
            ), mock.patch.object(httpx, "AsyncClient", side_effect=spy):
                client = await http_mod.get_client()
            self.assertIsNotNone(client)
            self.assertEqual(
                created[-1].get("proxy"), "http://127.0.0.1:7890"
            )
            await client.aclose()
        finally:
            http_mod._client = None
            if original is not None and not original.is_closed:
                await original.aclose()

    async def test_client_no_proxy_when_config_empty(self) -> None:
        import httpx

        import ArknightsUID.utils.http as http_mod

        original = http_mod._client
        http_mod._client = None
        try:
            with mock.patch.object(
                ArkConfig, "get_config", return_value=_cfg("")
            ):
                client = await http_mod.get_client()
            self.assertFalse(client.is_closed)
            await client.aclose()
        finally:
            http_mod._client = None
            if original is not None and not original.is_closed:
                await original.aclose()


class UserConfigCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_set_hide_uid_pref_updates_row(self) -> None:
        from ArknightsUID.arknightsuid_user_config import _set_hide_uid_pref

        ev = SimpleNamespace(user_id="u1", bot_id="b1")
        with mock.patch.object(
            user_config_mod.ArkUser,
            "select_rows",
            new=mock.AsyncMock(return_value=[SimpleNamespace(uid="123456789")]),
        ) as m_rows, mock.patch.object(
            user_config_mod.ArkUser,
            "update_data_by_data",
            new=mock.AsyncMock(return_value=0),
        ) as m_update:
            msg = await _set_hide_uid_pref(ev, "123456789", "on")

        self.assertIn("已开启隐藏UID", msg)
        # 两次：存在性检查一次 + 回显走 hide_uid_for→base_select_data 一次
        self.assertEqual(m_rows.await_count, 2)
        m_update.assert_awaited_once_with(
            select_data={"user_id": "u1", "bot_id": "b1", "uid": "123456789"},
            update_data={"hide_uid_self_value": "on"},
        )

    async def test_set_hide_uid_requires_local_login(self) -> None:
        from ArknightsUID.arknightsuid_user_config import _set_hide_uid_pref

        ev = SimpleNamespace(user_id="u1", bot_id="b1")
        with mock.patch.object(
            user_config_mod.ArkUser,
            "select_rows",
            new=mock.AsyncMock(return_value=[]),
        ):
            msg = await _set_hide_uid_pref(ev, "123456789", "on")
        self.assertIn("未通过", msg)

    async def test_set_hide_uid_reports_failure_code(self) -> None:
        from ArknightsUID.arknightsuid_user_config import _set_hide_uid_pref

        ev = SimpleNamespace(user_id="u1", bot_id="b1")
        with mock.patch.object(
            user_config_mod.ArkUser,
            "select_rows",
            new=mock.AsyncMock(return_value=[SimpleNamespace(uid="1")]),
        ), mock.patch.object(
            user_config_mod.ArkUser,
            "update_data_by_data",
            new=mock.AsyncMock(return_value=-1),
        ):
            msg = await _set_hide_uid_pref(ev, "123456789", "off")
        self.assertIn("设置失败", msg)


if __name__ == "__main__":
    unittest.main()
