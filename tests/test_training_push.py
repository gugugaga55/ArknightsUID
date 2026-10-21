"""专精训练完成推送测试：状态机纯函数 + 状态文件读写 + 文案。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.arknightsuid_notify import (
    TRAIN_TASK_NAME,
    evaluate_training,
    format_lead_secs,
    format_training_push_message,
    parse_training_lead,
)
from ArknightsUID.arknightsuid_notify import (
    _load_train_state,
    _TRAIN_STATE_PATH,
    evaluate_training_warn,
)


class EvaluateTrainingTest(unittest.TestCase):
    def test_training_in_progress_no_push(self) -> None:
        should_push, state = evaluate_training("char_172_svrash", 7200, False, "")
        self.assertFalse(should_push)
        self.assertEqual(state, {"notified": False, "trainee": "char_172_svrash"})

    def test_done_pushes_once(self) -> None:
        should_push, state = evaluate_training("char_172_svrash", 0, False, "char_172_svrash")
        self.assertTrue(should_push)
        self.assertEqual(state, {"notified": True, "trainee": "char_172_svrash"})

    def test_done_but_notified_suppresses(self) -> None:
        should_push, state = evaluate_training("char_172_svrash", 0, True, "char_172_svrash")
        self.assertFalse(should_push)
        self.assertEqual(state, {"notified": True, "trainee": "char_172_svrash"})

    def test_new_trainee_rearms(self) -> None:
        """换干员开新一轮：notified 复位，完成后可再次推送。"""
        should_push, state = evaluate_training("char_123_fang", 7200, True, "char_172_svrash")
        self.assertFalse(should_push)
        self.assertEqual(state, {"notified": False, "trainee": "char_123_fang"})
        # 新一轮走完 → 推送
        should_push, state = evaluate_training("char_123_fang", 0, state["notified"], state["trainee"])
        self.assertTrue(should_push)

    def test_idle_resets_state(self) -> None:
        should_push, state = evaluate_training("", 0, True, "char_172_svrash")
        self.assertFalse(should_push)
        self.assertEqual(state, {"notified": False, "trainee": ""})

    def test_unknown_remaining_keeps_state_no_push(self) -> None:
        """remainSecs<0（接口未给/哨兵穿透）：不推送，状态原样保留。"""
        should_push, state = evaluate_training("char_172_svrash", -1, True, "char_172_svrash")
        self.assertFalse(should_push)
        self.assertEqual(state, {"notified": True, "trainee": "char_172_svrash"})

    def test_subscribe_while_done_pushes_immediately(self) -> None:
        """订阅时训练已完成（remain 0、从未通知）：立即推送一次。"""
        should_push, _ = evaluate_training("char_172_svrash", 0, False, "")
        self.assertTrue(should_push)


class HelperTest(unittest.TestCase):
    def test_task_name_distinct_from_sanity(self) -> None:
        self.assertEqual(TRAIN_TASK_NAME, "订阅训练推送")

    def test_format_message_contains_trainee(self) -> None:
        message = format_training_push_message("刀客塔", "史尔特尔", "123***789")
        self.assertIn("专精训练完成", message)
        self.assertIn("史尔特尔", message)
        self.assertIn("123***789", message)


class ParseTrainingLeadTest(unittest.TestCase):
    """订阅命令提前量解析：时间词 → 秒，抽掉后不影响账号定位。"""

    def test_hours(self) -> None:
        self.assertEqual(parse_training_lead("5小时"), (5 * 3600, ""))

    def test_decimal_hours(self) -> None:
        self.assertEqual(parse_training_lead("5.5小时"), (19800, ""))

    def test_minutes_variants(self) -> None:
        self.assertEqual(parse_training_lead("300分"), (300 * 60, ""))
        self.assertEqual(parse_training_lead("90分钟"), (5400, ""))

    def test_combo_and_days(self) -> None:
        self.assertEqual(parse_training_lead("2小时30分"), (9000, ""))
        self.assertEqual(parse_training_lead("1天2小时"), (93600, ""))

    def test_keeps_account_target(self) -> None:
        lead, rest = parse_training_lead("2号 6小时")
        self.assertEqual(lead, 6 * 3600)
        self.assertEqual(rest, "2号")
        lead, rest = parse_training_lead("123456789 5小时")
        self.assertEqual(lead, 5 * 3600)
        self.assertEqual(rest, "123456789")

    def test_no_time_word_returns_none(self) -> None:
        self.assertEqual(parse_training_lead("第2号"), (None, "第2号"))
        self.assertEqual(parse_training_lead(""), (None, ""))

    def test_out_of_range_rejected(self) -> None:
        # 30 秒 < 1 分钟；10 天 > 7 天：都不算有效提前量
        self.assertIsNone(parse_training_lead("0.5分")[0])
        self.assertIsNone(parse_training_lead("10天")[0])

    def test_out_of_range_still_detectable_for_hint(self) -> None:
        """越界时间词可被命令层的提示条件识别（正则命中但解析为 None）：
        用于在订阅回执里告知"提前量被忽略"，而不是静默回落默认窗口。"""
        from ArknightsUID.arknightsuid_notify import _LEAD_SPEC_RE

        for text in ("10天", "0.5分", "999小时"):
            self.assertIsNone(parse_training_lead(text)[0])
            self.assertTrue(
                _LEAD_SPEC_RE.search(text), f"{text} 应触发越界提示条件"
            )
        # 无时间词（纯账号定位）不触发提示
        for text in ("123456789", "第2号", ""):
            self.assertIsNone(parse_training_lead(text)[0])
            self.assertFalse(
                bool(_LEAD_SPEC_RE.search(text)), f"{text} 不应触发提示"
            )


class FormatLeadSecsTest(unittest.TestCase):
    def test_variants(self) -> None:
        self.assertEqual(format_lead_secs(5 * 3600), "5小时")
        self.assertEqual(format_lead_secs(5400), "1小时30分钟")
        self.assertEqual(format_lead_secs(2700), "45分钟")
        self.assertEqual(format_lead_secs(86400), "1天")
        self.assertEqual(format_lead_secs(0), "0分钟")


class EvaluateTrainingWarnWindowTest(unittest.TestCase):
    """自定义提前量窗口：进入窗口提醒一次，出窗/换人自动复位。"""

    def test_custom_window_fires_earlier(self) -> None:
        # 剩 5 小时：默认 1 小时窗口不提醒，6 小时窗口提醒
        should, _ = evaluate_training_warn(
            "char_1", 5 * 3600, False, "", window_secs=6 * 3600
        )
        self.assertTrue(should)
        should, _ = evaluate_training_warn("char_1", 5 * 3600, False, "")
        self.assertFalse(should)

    def test_once_per_trainee(self) -> None:
        _, new_warned = evaluate_training_warn(
            "char_1", 600, True, "char_1", window_secs=3600
        )
        should, _ = evaluate_training_warn(
            "char_1", 540, new_warned, "char_1", window_secs=3600
        )
        self.assertFalse(should)

    def test_new_trainee_rearms(self) -> None:
        should, _ = evaluate_training_warn(
            "char_2", 600, True, "char_1", window_secs=3600
        )
        self.assertTrue(should)

    def test_out_of_window_resets(self) -> None:
        should, new_warned = evaluate_training_warn(
            "char_1", 10 * 3600, True, "char_1", window_secs=6 * 3600
        )
        self.assertFalse(should)
        self.assertFalse(new_warned)

    def test_invalid_window_falls_back_to_default(self) -> None:
        # 窗口非法（0/负）：回落默认 1 小时
        should, _ = evaluate_training_warn(
            "char_1", 600, False, "", window_secs=0
        )
        self.assertTrue(should)


class TrainStateFileTest(unittest.TestCase):
    def setUp(self) -> None:
        import ArknightsUID.arknightsuid_notify as notify_mod

        self.notify_mod = notify_mod
        self._orig_path = notify_mod._TRAIN_STATE_PATH
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        notify_mod._TRAIN_STATE_PATH = Path(self._tmp.name) / "training_push_state.json"

    def tearDown(self) -> None:
        self.notify_mod._TRAIN_STATE_PATH = self._orig_path
        self._tmp.cleanup()

    def test_roundtrip_and_missing_file(self) -> None:
        self.assertEqual(self.notify_mod._load_train_state(), {})
        state = {"b|u|123": {"notified": True, "trainee": "char_1"}}
        self.notify_mod._save_train_state(state)
        self.assertEqual(self.notify_mod._load_train_state(), state)

    def test_module_level_alias_reads_current_path(self) -> None:
        """_load_train_state 在调用时读 _TRAIN_STATE_PATH（测试可热替换）。"""
        self.notify_mod._save_train_state({"k": {"notified": False, "trainee": ""}})
        self.assertIn("k", _load_train_state())
        self.assertTrue(_TRAIN_STATE_PATH.name == "training_push_state.json")


class CheckTrainingPushUnsubTest(unittest.IsolatedAsyncioTestCase):
    """轮询中途退订不复活（与理智推送同款口径）：训练完成触发状态变更，
    落盘前用户已退订（订阅列表二次拉取为空）→ 状态键不得写回。"""

    def setUp(self) -> None:
        import tempfile
        import types

        import ArknightsUID.arknightsuid_notify as notify_mod

        self.notify_mod = notify_mod
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state = notify_mod._TRAIN_STATE_PATH
        self._orig_cred = notify_mod.CRED_WARN_STATE_PATH
        notify_mod._TRAIN_STATE_PATH = Path(self._tmp.name) / "t.json"
        notify_mod.CRED_WARN_STATE_PATH = Path(self._tmp.name) / "c.json"
        self._orig_random = notify_mod.random
        notify_mod.random = types.SimpleNamespace(uniform=lambda a, b: 0.0)

    def tearDown(self) -> None:
        self.notify_mod._TRAIN_STATE_PATH = self._orig_state
        self.notify_mod.CRED_WARN_STATE_PATH = self._orig_cred
        self.notify_mod.random = self._orig_random
        self._tmp.cleanup()

    class _FakeSub:
        uid = "555"
        bot_id = "bot"
        user_id = "user"

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, message: str) -> None:
            self.sent.append(message)

    class _FakeCred:
        uid = "555"
        nickname = "测试"

    # 训练已完成（remainSecs=0）：本轮必产生状态变更
    _PAYLOAD = {
        "code": 0,
        "data": {
            "status": {"name": "测试", "uid": "555"},
            "charInfoMap": {"char_172_svrash": {"name": "史尔特尔"}},
            "building": {
                # control.level>0 让 parse_building 判 available（否则整栋判缺）
                "control": {"level": 5, "chars": []},
                "training": {
                    "trainee": {"charId": "char_172_svrash", "targetSkill": 2},
                    "remainSecs": 0,
                    "lastUpdateTime": 0,
                    "speed": 0.5,
                },
            },
        },
    }

    def _install(self, sub, round_end_subs: list) -> None:
        notify_mod = self.notify_mod
        calls = {"n": 0}

        async def fake_get_subscribe(task_name: str, *args, **kwargs) -> list:
            calls["n"] += 1
            # 第一次（轮询开始）在订；第二次（落盘前重读）按场景返回
            return [sub] if calls["n"] == 1 else round_end_subs

        async def fake_resolve(user_id: str, bot_id: str, uid: str = ""):
            return self._FakeCred()

        async def fake_fetch(credential, force: bool = False):
            return self._PAYLOAD

        notify_mod.gs_subscribe.get_subscribe = fake_get_subscribe
        notify_mod.resolve_credential = fake_resolve
        notify_mod.fetch_cached_player_info = fake_fetch

    async def test_unsubscribed_mid_round_not_resurrected(self) -> None:
        sub = self._FakeSub()
        self._install(sub, round_end_subs=[])
        await self.notify_mod.check_training_push()
        # 推送发出去了（正常投递），但状态键不得落盘（用户已退订）
        self.assertEqual(len(sub.sent), 1)
        self.assertEqual(self.notify_mod._load_train_state(), {})

    async def test_still_subscribed_writes_state(self) -> None:
        sub = self._FakeSub()
        self._install(sub, round_end_subs=[sub])
        await self.notify_mod.check_training_push()
        state = self.notify_mod._load_train_state()
        self.assertTrue(state["bot|user|555"]["notified"])
        self.assertEqual(state["bot|user|555"]["trainee"], "char_172_svrash")


if __name__ == "__main__":
    unittest.main()
