"""基建订阅推送测试：无人机/贸易/低心情/公招四类状态机 + 文案 + 状态文件。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.arknightsuid_notify import (
    BUILDING_TASK_NAME,
    evaluate_full,
    evaluate_recruit,
    evaluate_tired,
    evaluate_trading_full,
    format_labor_push_message,
    format_recruit_push_message,
    format_tired_push_message,
    format_trading_push_message,
)
import ArknightsUID.arknightsuid_notify as notify_mod


class EvaluateFullTest(unittest.TestCase):
    def test_labor_full_pushes_once(self) -> None:
        is_full, should_push = evaluate_full(200, 200, False)
        self.assertTrue(is_full)
        self.assertTrue(should_push)
        # 第二轮（仍满）：不再推送
        _, should_push = evaluate_full(200, 200, True)
        self.assertFalse(should_push)

    def test_drop_below_rearms(self) -> None:
        _, should_push = evaluate_full(120, 200, True)
        self.assertFalse(should_push)
        _, should_push = evaluate_full(200, 200, False)
        self.assertTrue(should_push)

    def test_invalid_limit_never_pushes(self) -> None:
        is_full, should_push = evaluate_full(100, 0, False)
        self.assertFalse(is_full)
        self.assertFalse(should_push)


class EvaluateTradingFullTest(unittest.TestCase):
    def test_any_room_full_counts(self) -> None:
        rooms = [(8, 12), (10, 10)]
        is_full, should_push = evaluate_trading_full(rooms, False)
        self.assertTrue(is_full)
        self.assertTrue(should_push)

    def test_all_below_not_full(self) -> None:
        is_full, should_push = evaluate_trading_full([(8, 12), (2, 10)], False)
        self.assertFalse(is_full)
        self.assertFalse(should_push)

    def test_notified_suppresses(self) -> None:
        _, should_push = evaluate_trading_full([(12, 12)], True)
        self.assertFalse(should_push)


class EvaluateTiredTest(unittest.TestCase):
    def test_tired_episode(self) -> None:
        is_tired, should_push = evaluate_tired(3, False)
        self.assertTrue(is_tired)
        self.assertTrue(should_push)
        _, should_push = evaluate_tired(3, True)
        self.assertFalse(should_push)
        # 换完班清零 → 复位
        is_tired, _ = evaluate_tired(0, True)
        self.assertFalse(is_tired)
        _, should_push = evaluate_tired(1, False)
        self.assertTrue(should_push)


class EvaluateRecruitTest(unittest.TestCase):
    def test_fresh_done_pushes(self) -> None:
        fresh, should_push = evaluate_recruit([100, 200], [])
        self.assertEqual(fresh, [100, 200])
        self.assertTrue(should_push)

    def test_seen_dedupes(self) -> None:
        fresh, should_push = evaluate_recruit([100], [100])
        self.assertEqual(fresh, [])
        self.assertFalse(should_push)

    def test_partial_new(self) -> None:
        fresh, should_push = evaluate_recruit([100, 300], [100, 200])
        self.assertEqual(fresh, [300])
        self.assertTrue(should_push)


class BuildingMessageTest(unittest.TestCase):
    def test_labor_message_contains_values(self) -> None:
        message = format_labor_push_message("刀客塔", 200, 200, "12****89")
        self.assertIn("无人机", message)
        self.assertIn("200/200", message)
        self.assertIn("12****89", message)

    def test_trading_message_lists_rooms(self) -> None:
        message = format_trading_push_message(
            "刀客塔",
            [("贸易站2", "龙门币", 12, 12), ("贸易站3", "合成玉", 9, 9)],
            "12****89",
        )
        self.assertIn("贸易站2", message)
        self.assertIn("龙门币订单 12/12", message)
        self.assertIn("贸易站3", message)

    def test_tired_message_truncates_to_six(self) -> None:
        tired = [(f"干员{i}", "制造站", 5.0) for i in range(8)]
        message = format_tired_push_message("刀客塔", tired, "12****89")
        self.assertIn("等8人", message)
        self.assertNotIn("干员7(", message)

    def test_recruit_message_count(self) -> None:
        message = format_recruit_push_message("刀客塔", 2, "12****89")
        self.assertIn("2 个公招标位", message)


class BuildingStateFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = notify_mod._BUILDING_STATE_PATH
        self._tmp = tempfile.TemporaryDirectory()
        notify_mod._BUILDING_STATE_PATH = Path(self._tmp.name) / "b.json"

    def tearDown(self) -> None:
        notify_mod._BUILDING_STATE_PATH = self._orig
        self._tmp.cleanup()

    def test_roundtrip(self) -> None:
        self.assertEqual(notify_mod._load_building_state(), {})
        state = {"b|u|1": {"labor": True, "recruit_seen": [1, 2]}}
        notify_mod._save_building_state(state)
        self.assertEqual(notify_mod._load_building_state(), state)

    def test_task_name(self) -> None:
        self.assertEqual(BUILDING_TASK_NAME, "订阅基建推送")


if __name__ == "__main__":
    unittest.main()


class _FakeCred:
    def __init__(self, uid: str) -> None:
        self.uid = uid
        self.nickname = f"玩家{uid}"


class _FakeSub:
    def __init__(self, uid: str) -> None:
        self.uid = uid
        self.bot_id = "bot"
        self.user_id = "user"
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


class CheckBuildingPushWiringTest(unittest.IsolatedAsyncioTestCase):
    """调度器全链路 wiring：订阅 → 凭据 → player_info → 四类事件推送。"""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = notify_mod._BUILDING_STATE_PATH
        self._orig_cred = notify_mod.CRED_WARN_STATE_PATH
        notify_mod._BUILDING_STATE_PATH = Path(self._tmp.name) / "b.json"
        notify_mod.CRED_WARN_STATE_PATH = Path(self._tmp.name) / "c.json"
        import types

        self._orig_random = notify_mod.random
        notify_mod.random = types.SimpleNamespace(uniform=lambda a, b: 0.0)

    def tearDown(self) -> None:
        notify_mod._BUILDING_STATE_PATH = self._orig_state
        notify_mod.CRED_WARN_STATE_PATH = self._orig_cred
        notify_mod.random = self._orig_random
        self._tmp.cleanup()

    def _install(self, subscribe: _FakeSub, payload: dict) -> None:
        async def fake_get_subscribe(task_name: str) -> list:
            return [subscribe]

        async def fake_resolve(user_id: str, bot_id: str, uid: str = ""):
            return _FakeCred(uid or subscribe.uid)

        async def fake_fetch(credential, force: bool = False):
            return payload

        notify_mod.gs_subscribe.get_subscribe = fake_get_subscribe
        notify_mod.resolve_credential = fake_resolve
        notify_mod.fetch_cached_player_info = fake_fetch

    @staticmethod
    def _payload(labor=(200, 200), recruit=None) -> dict:
        return {
            "code": 0,
            "data": {
                "status": {"name": "测试", "uid": "555", "level": 120,
                           "ap": {"max": 135}},
                "chars": [],
                "recruit": recruit if recruit is not None else [
                    {"startTs": -1, "finishTs": -1, "state": 1},
                ],
                "building": {
                    "control": {"level": 5, "chars": []},
                    "labor": {"value": labor[0], "maxValue": labor[1]},
                    "manufactures": [],
                    "tradings": [
                        {"slotId": "t1", "level": 3, "chars": [], "strategy": "O_GOLD",
                         "stock": [], "stockLimit": 0},
                    ],
                    "powers": [],
                    "dormitories": [],
                },
            },
        }

    async def test_labor_full_pushes_then_suppresses(self) -> None:
        subscribe = _FakeSub("555")
        self._install(subscribe, self._payload())
        await notify_mod.check_building_push()
        self.assertEqual(len(subscribe.sent), 1)
        self.assertIn("无人机已回满", subscribe.sent[0])
        state = notify_mod._load_building_state()["bot|user|555"]
        self.assertTrue(state["labor"])
        # 下一轮仍满 → 不再推
        await notify_mod.check_building_push()
        self.assertEqual(len(subscribe.sent), 1)
        # 用掉无人机 → 状态复位
        notify_mod.fetch_cached_player_info = self._install_fetch(
            self._payload(labor=(100, 200))
        )
        await notify_mod.check_building_push()
        self.assertFalse(
            notify_mod._load_building_state()["bot|user|555"]["labor"]
        )

    def _install_fetch(self, payload: dict):
        async def fake_fetch(credential, force: bool = False):
            return payload
        return fake_fetch

    async def test_recruit_done_pushes_and_dedupes(self) -> None:
        subscribe = _FakeSub("555")
        payload = self._payload(
            labor=(100, 200),
            recruit=[{"startTs": 1, "finishTs": 100, "state": 2}],
        )
        self._install(subscribe, payload)
        await notify_mod.check_building_push()
        self.assertEqual(len(subscribe.sent), 1)
        self.assertIn("公招标位可领取", subscribe.sent[0])
        seen = notify_mod._load_building_state()["bot|user|555"]["recruit_seen"]
        self.assertEqual(seen, [100])
        # 同一 finishTs 下一轮不重复推
        await notify_mod.check_building_push()
        self.assertEqual(len(subscribe.sent), 1)

    async def test_no_events_no_push(self) -> None:
        subscribe = _FakeSub("555")
        payload = self._payload(labor=(100, 200))
        self._install(subscribe, payload)
        await notify_mod.check_building_push()
        self.assertEqual(subscribe.sent, [])
        # 无事件：不推送，状态仅记录全 False 标志（不触发任何提醒）
        state = notify_mod._load_building_state()["bot|user|555"]
        self.assertFalse(any((state.get(k) for k in ("labor", "trading", "tired"))))
        self.assertNotIn("recruit_seen", state)
