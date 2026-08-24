"""自动签到推送 + 凭据预警 + 训练剩余时间预警 + 抽卡导出测试。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.arknightsuid_notify import (
    CRED_WARN_COOLDOWN_SECS,
    TRAIN_WARN_WINDOW_SECS,
    evaluate_sign_result,
    evaluate_training_warn,
    format_sign_push_message,
    format_training_warn_message,
    maybe_warn_credential,
)
import ArknightsUID.arknightsuid_notify as notify_mod

from ArknightsUID.arknightsuid_gacha import build_export_payload


class TrainingWarnTest(unittest.TestCase):
    def test_out_of_window_no_warn(self) -> None:
        should_warn, new_warned = evaluate_training_warn("char_1", 7200, False, "")
        self.assertFalse(should_warn)
        self.assertFalse(new_warned)

    def test_in_window_warns_once(self) -> None:
        should_warn, new_warned = evaluate_training_warn(
            "char_1", TRAIN_WARN_WINDOW_SECS - 60, False, ""
        )
        self.assertTrue(should_warn)
        self.assertTrue(new_warned)
        # 已预警（同干员同轮）：不再推，warned 保持
        should_warn, new_warned = evaluate_training_warn("char_1", 1800, True, "char_1")
        self.assertFalse(should_warn)
        self.assertTrue(new_warned)

    def test_new_trainee_rearms(self) -> None:
        should_warn, _ = evaluate_training_warn("char_2", 1800, True, "char_1")
        self.assertTrue(should_warn)

    def test_unknown_or_idle_no_warn(self) -> None:
        self.assertEqual(
            evaluate_training_warn("char_1", -1, False, "char_1"), (False, False)
        )
        self.assertEqual(evaluate_training_warn("", 100, True, "x"), (False, False))

    def test_message_contains_remaining(self) -> None:
        message = format_training_warn_message("刀客塔", "史尔特尔", 2700, "1***9")
        self.assertIn("史尔特尔", message)
        self.assertIn("约45分钟", message)


class SignResultTest(unittest.TestCase):
    def test_success(self) -> None:
        self.assertEqual(evaluate_sign_result({"code": 0, "message": "OK"}), "success")

    def test_already_signed(self) -> None:
        self.assertEqual(
            evaluate_sign_result({"code": 10009, "message": "今日已签到"}), "already"
        )

    def test_fail_and_none(self) -> None:
        self.assertEqual(
            evaluate_sign_result({"code": 1, "message": "boom"}), "fail"
        )
        self.assertEqual(evaluate_sign_result(None), "fail")

    def test_message_shapes(self) -> None:
        success = format_sign_push_message("刀客塔", "1***9", "success", "签到成功")
        self.assertIn("自动签到完成", success)
        fail = format_sign_push_message("刀客塔", "1***9", "fail", "code=1")
        self.assertIn("自动签到失败", fail)
        self.assertIn("ark签到", fail)


class _FakeSubscribe:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.bot_id = "bot"
        self.user_id = "user"

    async def send(self, message: str) -> None:
        self.sent.append(message)


class CredWarnTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_path = notify_mod.CRED_WARN_STATE_PATH
        self._orig_time = time.time
        self._tmp = tempfile.TemporaryDirectory()
        notify_mod.CRED_WARN_STATE_PATH = Path(self._tmp.name) / "c.json"
        self._now = 1_800_000_000
        time.time = lambda: self._now  # type: ignore[assignment]

    def tearDown(self) -> None:
        notify_mod.CRED_WARN_STATE_PATH = self._orig_path
        time.time = self._orig_time  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_warn_once_then_cooldown(self) -> None:
        sub = _FakeSubscribe()
        asyncio_run(maybe_warn_credential(sub, "123456789", "cred 失效"))
        self.assertEqual(len(sub.sent), 1)
        # 冷却期内不再提醒
        sub2 = _FakeSubscribe()
        asyncio_run(maybe_warn_credential(sub2, "123456789", "cred 失效"))
        self.assertEqual(len(sub2.sent), 0)
        # 冷却结束后恢复
        self._now += CRED_WARN_COOLDOWN_SECS + 1
        asyncio_run(maybe_warn_credential(sub2, "123456789", "cred 失效"))
        self.assertEqual(len(sub2.sent), 1)

    def test_send_failure_does_not_stamp(self) -> None:
        class _Broken(_FakeSubscribe):
            async def send(self, message: str) -> None:
                raise RuntimeError("send failed")

        asyncio_run(maybe_warn_credential(_Broken(), "987", "err"))
        self.assertEqual(notify_mod._load_cred_warn_state(), {})


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


class GachaExportTest(unittest.TestCase):
    def test_payload_orders_and_fields(self) -> None:
        records = [
            {"gacha_ts": 200, "pos": 1, "name": "后出的", "char_id": "c2",
             "rarity": 5, "is_new": True, "pool_name": "池B", "category": "2"},
            {"gacha_ts": 100, "pos": 2, "name": "先出的", "char_id": "c1",
             "rarity": 4, "is_new": False, "pool_name": "池A", "category": "1"},
        ]
        payload = build_export_payload("123456789", records)
        self.assertEqual(payload["info"]["uid"], "123456789")
        self.assertEqual(payload["info"]["count"], 2)
        self.assertEqual(payload["info"]["app"], "ArknightsUID")
        self.assertEqual(
            [r["name"] for r in payload["records"]], ["先出的", "后出的"]
        )
        first = payload["records"][0]
        self.assertEqual(first["pos"], 2)
        self.assertEqual(first["pool"], "池A")
        self.assertEqual(first["rarity"], 4)
        self.assertFalse(first["is_new"])
        self.assertTrue(first["time"].startswith("1970-"))

    def test_empty_records(self) -> None:
        payload = build_export_payload("u", [])
        self.assertEqual(payload["records"], [])
        self.assertEqual(payload["info"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
