"""公告专项订阅（ark订阅公告）测试：diff/文案纯函数 + 状态文件读写。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.arknightsuid_push import (
    ANN_TASK_NAME,
    _ANN_PUSH_LIMIT,
    diff_new_announcements,
    format_ann_push_message,
)


def _ann(ann_id: str, title: str = "公告", date: str = "2026-08-30") -> dict:
    return {
        "id": ann_id,
        "title": title,
        "url": f"https://www.skland.com/article?id={ann_id}",
        "date": date,
        "cover": "",
        "author": "",
        "avatar": "",
    }


class TaskNameTest(unittest.TestCase):
    def test_distinct_from_combined_activity_task(self) -> None:
        from ArknightsUID.arknightsuid_push import TASK_NAME

        self.assertEqual(ANN_TASK_NAME, "订阅方舟公告")
        self.assertNotEqual(ANN_TASK_NAME, TASK_NAME)


class DiffNewAnnouncementsTest(unittest.TestCase):
    def test_filters_seen_keeps_order(self) -> None:
        anns = [_ann("3"), _ann("2"), _ann("1")]
        fresh = diff_new_announcements(anns, ["2"])
        self.assertEqual([a["id"] for a in fresh], ["3", "1"])

    def test_empty_seen_returns_all(self) -> None:
        anns = [_ann("3"), _ann("2")]
        self.assertEqual(len(diff_new_announcements(anns, [])), 2)

    def test_entry_without_id_skipped(self) -> None:
        anns = [_ann(""), _ann("9")]
        fresh = diff_new_announcements(anns, [])
        self.assertEqual([a["id"] for a in fresh], ["9"])


class FormatAnnPushMessageTest(unittest.TestCase):
    def test_contains_title_date_url(self) -> None:
        message = format_ann_push_message(
            [_ann("42", title="维护公告", date="2026-08-30")]
        )
        self.assertIn("新公告", message)
        self.assertIn("《维护公告》", message)
        self.assertIn("2026-08-30", message)
        self.assertIn("id=42", message)

    def test_caps_at_limit_with_overflow_note(self) -> None:
        anns = [_ann(str(i)) for i in range(_ANN_PUSH_LIMIT + 3)]
        message = format_ann_push_message(anns)
        self.assertIn(f"共 {_ANN_PUSH_LIMIT + 3} 条", message)
        # 超限的条目不在本轮正文里
        self.assertNotIn(f"id={_ANN_PUSH_LIMIT + 2}", message.split("……")[0])


class BuildAnnPushPayloadTest(unittest.IsolatedAsyncioTestCase):
    """订阅公告推送带图：文案段 + 封面图段；无封面/失败自动降级纯文字。"""

    @staticmethod
    def _data_uri() -> str:
        # 合法 data URI 即可（只验证段构造，不校验图像内容）
        import base64

        return (
            "data:image/webp;base64,"
            + base64.b64encode(b"RIFFxxxxWEBP").decode()
        )

    async def test_payload_text_plus_cover_images(self) -> None:
        import ArknightsUID.arknightsuid_push as push

        anns = [
            {**_ann("1", title="有图公告"), "cover": "https://x/c1.webp"},
            {**_ann("2", title="纯文字公告"), "cover": ""},
        ]
        data_uri = self._data_uri()

        async def fake_b64(url, quality=80, cover_size=None, anchor="center"):
            return data_uri if "c1" in url else ""

        with mock.patch.object(push, "_announce_image_b64", fake_b64):
            payload = await push.build_ann_push_payload(anns)

        self.assertEqual(payload[0].type, "text")
        self.assertIn("有图公告", str(payload[0].data))
        self.assertEqual(payload[1].type, "image")
        # 纯文字公告不产生空图段
        self.assertEqual(len(payload), 2)

    async def test_payload_text_only_when_no_covers(self) -> None:
        import ArknightsUID.arknightsuid_push as push

        async def fail_b64(url, quality=80, cover_size=None, anchor="center"):
            return ""

        with mock.patch.object(push, "_announce_image_b64", fail_b64):
            payload = await push.build_ann_push_payload([_ann("9")])
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0].type, "text")


class AnnStateFileTest(unittest.TestCase):
    def setUp(self) -> None:
        import ArknightsUID.arknightsuid_push as push_mod

        self.push = push_mod
        self._orig_path = push_mod._ANN_STATE_PATH
        self._tmp = tempfile.TemporaryDirectory()
        push_mod._ANN_STATE_PATH = Path(self._tmp.name) / "ann_push_state.json"

    def tearDown(self) -> None:
        self.push._ANN_STATE_PATH = self._orig_path
        self._tmp.cleanup()

    def test_roundtrip_and_missing_file(self) -> None:
        self.assertEqual(self.push._load_ann_state(), {})
        self.push._save_ann_state({"seen": ["a", "b"]})
        self.assertEqual(
            self.push._load_ann_state(), {"seen": ["a", "b"]}
        )

    def test_seen_list_capped(self) -> None:
        from ArknightsUID.arknightsuid_push import _STATE_LIMIT

        many = [str(i) for i in range(_STATE_LIMIT + 50)]
        self.push._save_ann_state({"seen": many})
        self.assertEqual(len(self.push._load_ann_state()["seen"]), _STATE_LIMIT)
        self.assertEqual(self.push._load_ann_state()["seen"][-1], str(len(many) - 1))


if __name__ == "__main__":
    unittest.main()


class PushStateMergeTest(unittest.TestCase):
    """审计 P2-2：状态落盘前重读合并——旧快照不得覆盖并发写入的 seen。"""

    def test_save_merges_concurrent_seen_entries(self) -> None:
        import ArknightsUID.arknightsuid_push as push

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "push_state.json"
            with mock.patch.object(push, "_STATE_PATH", state_path):
                # 写入者 A（定时任务）先落 seen=["a"]
                push.save_push_state({"seen": ["a"]})
                # 写入者 B（命令/另一轮）基于更早读到的快照只见过 ["a"]，
                # 但磁盘此刻已被 C 更新为 seen=["a","b"] + 其他键
                push.save_push_state({"seen": ["a", "b"], "baseline_ts": 9})
                # A 此刻才用旧快照整写（合并语义下不得丢 b / baseline_ts）
                push.save_push_state({"seen": ["a"]})
                final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn("a", final["seen"])
        self.assertIn("b", final["seen"], "旧快照覆盖丢失了并发写入的条目")
        self.assertEqual(final.get("baseline_ts"), 9, "并发写入的其他键被回滚")

    def test_ann_state_merge_keeps_race_entries(self) -> None:
        import ArknightsUID.arknightsuid_push as push

        with tempfile.TemporaryDirectory() as tmp:
            ann_path = Path(tmp) / "ann_state.json"
            with mock.patch.object(push, "_ANN_STATE_PATH", ann_path):
                push._save_ann_state({"seen": [1001, 1002]})
                push._save_ann_state({"seen": ["1003"]})
                final = json.loads(ann_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [str(x) for x in final["seen"]], ["1001", "1002", "1003"]
        )


class AnnImageSizeSafetyTest(unittest.TestCase):
    """审计 P2-4：公告宽高 malformed 输入不抛异常。"""

    def test_safe_img_side_rejects_malformed(self) -> None:
        from ArknightsUID.arknightsuid_push import _safe_img_side

        for bad in ("auto", None, [690], {"w": 1}, float("nan"),
                    float("inf"), -5, 0, 10 ** 9, True, "12.5"):
            self.assertEqual(_safe_img_side(bad), 0, repr(bad))
        self.assertEqual(_safe_img_side(690), 690)
        self.assertEqual(_safe_img_side("690"), 690)

    def test_split_ann_images_survives_malformed_sizes(self) -> None:
        from ArknightsUID.arknightsuid_push import split_ann_images

        detail = {
            "videos": [],
            "images": [
                {"url": "https://x/1.png", "width": "auto", "height": None},
                {"url": "https://x/2.png", "width": [690], "height": {"h": 1}},
                {"url": "https://x/3.png", "width": 690, "height": 4000},
                "not-a-dict",
            ],
        }
        normal, longs = split_ann_images(detail)  # 不抛即通过
        self.assertEqual(len(normal), 2)
        self.assertEqual(longs, ["https://x/3.png"])



class FileLockCrossProcessTest(unittest.TestCase):
    """审计 #10：跨进程文件锁——两个真实进程并发读改写不丢更新。"""

    def test_reentrant_no_deadlock(self) -> None:
        from ArknightsUID.utils.file_lock import file_locked

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "s.json"
            with file_locked(target):
                with file_locked(target):  # 同线程嵌套：不得自锁
                    target.write_text("{}", encoding="utf-8")

    def test_two_processes_no_lost_update(self) -> None:
        import multiprocessing as mp

        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "shared.json")
            # spawn：fork 在多线程进程里有死锁风险（DeprecationWarning）；
            # worker 在独立轻量模块（子进程 import 本模块会拉起全家桶）
            from lock_probe_worker import lock_write_worker

            procs = [
                mp.get_context("spawn").Process(
                    target=lock_write_worker, args=(state_path, f"k{i}")
                )
                for i in range(4)
            ]
            for proc in procs:
                proc.start()
            try:
                for proc in procs:
                    # 负载高时 spawn 子进程拉起解释器很慢，20s 会误报；
                    # 给足 60s（ flock 本身毫秒级，等待几乎全是启动开销）
                    proc.join(timeout=60)
                for proc in procs:
                    self.assertEqual(
                        proc.exitcode, 0,
                        f"锁写子进程异常退出: exitcode={proc.exitcode}",
                    )
            finally:
                # 超时/断言失败时收掉残留子进程，避免挂着污染后续用例
                for proc in procs:
                    if proc.is_alive():
                        proc.terminate()
                        proc.join(timeout=5)
            final = json.loads(Path(state_path).read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(final), ["k0", "k1", "k2", "k3"],
            f"并发进程丢更新: {final}",
        )
