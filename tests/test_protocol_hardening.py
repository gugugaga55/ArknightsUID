"""协议边界与生命周期加固回归测试（审计 #26/#31/#33/#37）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeBot:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, message, **kwargs) -> None:
        self.sent.append(str(message))


class RefreshNonDictGuardTest(unittest.IsolatedAsyncioTestCase):
    """审计 #26a：refresh 响应 JSON 顶层数组/字符串不再 AttributeError。"""

    async def test_array_body_returns_empty(self) -> None:
        import httpx

        from ArknightsUID.utils.skland import client as sk_client

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["unexpected"])

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with mock.patch.object(
                sk_client, "get_client",
                new=mock.AsyncMock(return_value=client),
            ), mock.patch.object(sk_client, "_REFRESH_RETRY_DELAY", 0):
                token = await sk_client.refresh_token("c" * 32)
        finally:
            await client.aclose()
        self.assertEqual(token, "")


class ScanPayloadGuardTest(unittest.IsolatedAsyncioTestCase):
    """审计 #26b：status=0 但 data 缺失/畸形不再 KeyError。"""

    async def test_missing_data_returns_none(self) -> None:
        from ArknightsUID.utils.skland import auth as sk_auth

        for payload in (None, "x", {"scanId": ""}):
            async def fake_request(method, url, *, payload=payload, **kwargs):
                return 200, {"status": 0, "msg": "OK", "data": payload}

            with mock.patch.object(sk_auth, "_request", fake_request):
                self.assertIsNone(await sk_auth.get_scan_id())
                self.assertIsNone(await sk_auth.get_scan_status("s"))


class AnnDetailGuardTest(unittest.TestCase):
    """审计 #26c：textSlice 不可迭代 / cover 非 dict 不再抛。"""

    def test_malformed_slices_survive(self) -> None:
        from ArknightsUID.utils.skland.web import parse_ann_detail

        response = {
            "code": 0,
            "data": {
                "item": {
                    "textSlice": 6,               # truthy 不可迭代
                    "imageListSlice": [{"url": "u", "width": 1, "height": 1}],
                    "videoListSlice": [
                        {"url": "v", "cover": "https://c"},  # cover 非 dict
                        {"url": "v2", "cover": {"url": "c2"}},
                    ],
                },
                "user": {},
            },
        }
        detail = parse_ann_detail(response)  # 不抛即通过
        self.assertEqual(detail["images"][0]["url"], "u")


class YituliuNumericGuardTest(unittest.TestCase):
    """审计 #26d：异常数值字段不再让 float() 抛异常。"""

    def test_num_guard(self) -> None:
        from ArknightsUID.utils.yituliu import _num, rank_stages

        self.assertEqual(_num("N/A"), 0.0)
        self.assertEqual(_num([1]), 0.0)
        self.assertEqual(_num({"x": 1}), 0.0)
        self.assertEqual(_num(None), 0.0)
        self.assertEqual(_num(float("inf")), 0.0)
        self.assertEqual(_num("1.5"), 1.5)
        self.assertEqual(_num(2), 2.0)

        dirty = [
            {"stageType": "MAIN", "stageCode": "1-7", "spm": "N/A"},
            {"stageType": "MAIN", "stageCode": "4-6", "spm": 1.2},
        ]
        rows = rank_stages(dirty, "主线")
        self.assertEqual([r["stageCode"] for r in rows], ["4-6"])


class PlayerParseGuardTest(unittest.TestCase):
    """审计 #26e：status/ap/campaign 为 truthy 非 dict 时降级不崩。"""

    def test_malformed_envelope_degrades(self) -> None:
        from ArknightsUID.utils.player.parse import effective_ap, parse_player_info

        data = parse_player_info({
            "data": {"status": ["bad"], "campaign": "x", "routine": 3}
        })
        self.assertIsNotNone(data)
        self.assertEqual(data.ap_max, 0)
        self.assertEqual(effective_ap(data), 0)


class SmwResultsGuardTest(unittest.IsolatedAsyncioTestCase):
    """审计 #26g：SMW results 为 list 时返回空列表不崩。"""

    async def test_list_results_returns_empty(self) -> None:
        import ArknightsUID.utils.prts.client as prts

        async def fake_smw(query):
            return {"query": {"results": ["v3", "list"]}}

        with mock.patch.object(prts, "_smw_ask", fake_smw):
            activities = await prts.query_activities()
        self.assertEqual(activities, [])


class SignAllCredentialFailTest(unittest.IsolatedAsyncioTestCase):
    """审计 #31：10001 无"已签"文案 = 凭据失效 → 计失败而非跳过。"""

    async def test_10001_without_already_text_counts_failed(self) -> None:
        import ArknightsUID.arknightsuid_notify as notify

        rows = [
            SimpleNamespace(uid=uid, user_id="u", bot_id="b")
            for uid in ("111", "222", "333")
        ]

        async def fake_resolve(user_id, bot_id, uid="", index=0):
            return SimpleNamespace(
                uid=uid, nickname="A", cred="c", token="t", hg_token="",
                server_id="1", cookie_status="", skland_user_id="",
                record_id="",
            )

        responses = iter([
            {"code": 10001, "message": "登录态失效"},   # 失效 → failed
            {"code": 10001, "message": "今日已签到"},   # 已签 → skipped
            {"code": 0, "message": "签到成功"},          # 成功 → ok
        ])

        async def fake_select_rows(**kwargs):
            return rows

        async def fake_attendance(credential):
            return next(responses)

        bot = FakeBot()
        ev = SimpleNamespace(user_id="u", bot_id="b", group_id=None, text="")
        send_mock = mock.AsyncMock()
        with mock.patch.object(
            notify, "resolve_credential", fake_resolve
        ), mock.patch.object(
            notify, "ark_attendance", fake_attendance
        ), mock.patch(
            "ArknightsUID.utils.database.models.ArkUser.select_rows",
            new=fake_select_rows,
        ), mock.patch.object(
            notify, "_send_text", new=send_mock
        ):
            await notify.sign_all_accounts(bot, ev)

        # 最后一条汇总：成功 1 · 已签跳过 1 · 失败 1（失效不再被算成已签）
        last = send_mock.await_args_list[-1].args
        self.assertIn("成功 1", last[-1])
        self.assertIn("已签跳过 1", last[-1])
        self.assertIn("失败 1", last[-1])


class ScanDeadlineTest(unittest.TestCase):
    """审计 #33：扫码轮询必须有整体 deadline。"""

    def test_scan_loop_has_deadline(self) -> None:
        import inspect

        from ArknightsUID.arknightsuid_bind import scan_login

        source = inspect.getsource(scan_login)
        self.assertIn("deadline", source)
        # 旧最坏 50×(2+25)=1350s；3 分钟 deadline 收敛
        self.assertIn("180", source)


if __name__ == "__main__":
    unittest.main()
