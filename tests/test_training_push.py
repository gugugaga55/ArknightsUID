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
    format_training_push_message,
)
from ArknightsUID.arknightsuid_notify import (
    _load_train_state,
    _save_train_state,
    _TRAIN_STATE_PATH,
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


if __name__ == "__main__":
    unittest.main()
