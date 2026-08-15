from __future__ import annotations

import unittest
from typing import Any

from ArknightsUID.utils.skland import auth as ark_auth
from ArknightsUID.utils.skland import client as skland_client


class FakeRequest:
    """替换 auth._request 的队列桩，返回 (status_code, json|None)。"""

    queue: list[tuple[int, dict[str, Any] | None]] = []
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    @classmethod
    async def fake(
        cls,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any] | None]:
        cls.calls.append((method, url, body))
        if not cls.queue:
            raise AssertionError(f"unexpected request {method} {url}")
        return cls.queue.pop(0)


class ParseCredentialTest(unittest.TestCase):
    def test_cred_and_token_lengths(self) -> None:
        self.assertEqual(ark_auth.parse_credential("x" * 32), ("cred", "x" * 32))
        self.assertEqual(ark_auth.parse_credential("y" * 24), ("token", "y" * 24))

    def test_invalid_length_returns_none(self) -> None:
        kind, _value = ark_auth.parse_credential("short")
        self.assertIsNone(kind)

    def test_strips_whitespace(self) -> None:
        kind, value = ark_auth.parse_credential("  " + "z" * 32 + "  ")
        self.assertEqual((kind, value), ("cred", "z" * 32))


class AuthHttpTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig = ark_auth._request
        ark_auth._request = FakeRequest.fake  # type: ignore[assignment]
        FakeRequest.queue = []
        FakeRequest.calls = []

    def tearDown(self) -> None:
        ark_auth._request = self._orig  # type: ignore[assignment]

    async def test_get_scan_id_success(self) -> None:
        FakeRequest.queue = [(200, {"status": 0, "msg": "OK", "data": {"scanId": "SID"}})]
        self.assertEqual(await ark_auth.get_scan_id(), "SID")

    async def test_get_scan_id_business_fail(self) -> None:
        FakeRequest.queue = [(200, {"status": 1})]
        self.assertIsNone(await ark_auth.get_scan_id())

    async def test_get_scan_status_not_scanned(self) -> None:
        FakeRequest.queue = [(200, {"status": 1})]
        self.assertIsNone(await ark_auth.get_scan_status("SID"))

    async def test_get_scan_status_confirmed(self) -> None:
        FakeRequest.queue = [(200, {"status": 0, "data": {"scanCode": "CODE"}})]
        self.assertEqual(await ark_auth.get_scan_status("SID"), "CODE")

    async def test_get_token_by_scan_code_success(self) -> None:
        FakeRequest.queue = [
            (200, {"status": 0, "data": {"token": "T", "deviceToken": "D"}})
        ]
        self.assertEqual(
            await ark_auth.get_token_by_scan_code("CODE"),
            {"token": "T", "device_token": "D"},
        )

    async def test_get_cred_by_token_405(self) -> None:
        FakeRequest.queue = [(405, None)]
        self.assertEqual(await ark_auth.get_cred_by_token("T"), "405")

    async def test_get_cred_by_token_success(self) -> None:
        FakeRequest.queue = [
            (200, {"status": 0, "data": {"code": "OAUTH_CODE"}}),
            (200, {"code": 0, "data": {"cred": "CRED", "userId": 999}}),
        ]
        result = await ark_auth.get_cred_by_token("T")
        self.assertEqual(result, {"cred": "CRED", "skland_user_id": "999"})

    async def test_get_cred_by_token_oauth_step_fail(self) -> None:
        FakeRequest.queue = [(200, {"status": 1})]
        self.assertIsNone(await ark_auth.get_cred_by_token("T"))

    async def test_get_cred_by_token_cred_step_fail(self) -> None:
        FakeRequest.queue = [
            (200, {"status": 0, "data": {"code": "C"}}),
            (200, {"code": 1}),
        ]
        self.assertIsNone(await ark_auth.get_cred_by_token("T"))


class ListArkRolesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig = skland_client.get_binding

    def tearDown(self) -> None:
        skland_client.get_binding = self._orig  # type: ignore[assignment]

    async def test_filters_game_id_1_and_handles_missing_default_role(self) -> None:
        async def fake_get_binding(cred_obj: object) -> dict[str, Any]:
            return {
                "code": 0,
                "data": {"list": [{"bindingList": [
                    {"gameId": 1, "uid": "rec1", "nickName": "博士A",
                     "defaultRole": {"roleId": "111", "nickname": "阿米娅", "serverId": 1}},
                    {"gameId": 3, "uid": "rec2", "defaultRole": {"roleId": "222"}},  # 终末地跳过
                    {"gameId": 1, "uid": "rec3", "nickName": "博士B"},  # 无 defaultRole
                ]}]},
            }

        skland_client.get_binding = fake_get_binding  # type: ignore[assignment]
        roles = await ark_auth.list_ark_roles_by_cred("CRED")

        self.assertEqual(len(roles), 2)
        self.assertEqual(
            roles[0],
            {"role_id": "111", "nickname": "阿米娅", "record_uid": "rec1", "server_id": "1"},
        )
        self.assertEqual(
            roles[1],
            {"role_id": "rec3", "nickname": "博士B", "record_uid": "rec3", "server_id": "1"},
        )

    async def test_returns_none_when_binding_fails(self) -> None:
        async def fake_get_binding(cred_obj: object) -> None:
            return None

        skland_client.get_binding = fake_get_binding  # type: ignore[assignment]
        self.assertIsNone(await ark_auth.list_ark_roles_by_cred("CRED"))


if __name__ == "__main__":
    unittest.main()
