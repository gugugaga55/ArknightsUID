"""状态卡背景链（utils/background.py）测试。

覆盖：用户槽位（上传图 ⇄ 干员立绘偏好）后写覆盖与同清、
resolve_status_bg 的优先级链（上传 > 干员偏好 > 全局 > 随机 > 兜底）、
干员偏好「跟随游戏内当前皮肤 / 钉死皮肤 / 未持有回退默认立绘」、
以及坏数据降级（坏 JSON、取图失败落下一级）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ArknightsUID.utils import background as bg_mod
from ArknightsUID.utils.background import (
    clear_user_bg,
    read_op_bg,
    resolve_status_bg,
    save_op_bg,
    user_bg_path,
    user_op_bg_path,
)
from ArknightsUID.utils.gamedata.skins import match_skin

BOT, USER = "bot1", "user1"
OP = {"char_id": "char_002_amiya", "skin_id": "", "name": "阿米娅", "rarity": 4}


class OpPrefStoreTest(unittest.TestCase):
    """干员立绘偏好的读写与槽位互斥。"""

    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        from pathlib import Path

        self._dir = Path(self._tmp.name)
        self._patch = mock.patch.object(bg_mod, "BG_DIR", self._dir)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_save_read_roundtrip(self) -> None:
        save_op_bg(BOT, USER, **OP)
        self.assertEqual(read_op_bg(BOT, USER), OP)

    def test_read_corrupt_json_degrades_to_empty(self) -> None:
        path = user_op_bg_path(BOT, USER)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad json", encoding="utf-8")
        self.assertEqual(read_op_bg(BOT, USER), {})

    def test_read_missing_fields_degrades_to_empty(self) -> None:
        path = user_op_bg_path(BOT, USER)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"name": "阿米娅"}', encoding="utf-8")
        self.assertEqual(read_op_bg(BOT, USER), {})

    def test_save_op_clears_upload(self) -> None:
        upload = user_bg_path(BOT, USER)
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(b"img")
        save_op_bg(BOT, USER, **OP)
        self.assertFalse(upload.exists())
        self.assertTrue(user_op_bg_path(BOT, USER).exists())

    def test_clear_user_bg_clears_both(self) -> None:
        upload = user_bg_path(BOT, USER)
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(b"img")
        save_op_bg(BOT, USER, **OP)
        # save_op_bg 清了上传图；补建上传图再验同清
        upload.write_bytes(b"img")
        self.assertTrue(clear_user_bg(BOT, USER))
        self.assertFalse(upload.exists())
        self.assertFalse(user_op_bg_path(BOT, USER).exists())
        self.assertFalse(clear_user_bg(BOT, USER))  # 已清空 → 无事可做


class ResolveChainTest(unittest.IsolatedAsyncioTestCase):
    """背景优先级链：上传 > 干员偏好 > 全局 > 随机 > 空兜底。"""

    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        from pathlib import Path

        self._dir = Path(self._tmp.name)
        self._patch = mock.patch.object(bg_mod, "BG_DIR", self._dir)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _write_upload(self) -> None:
        path = user_bg_path(BOT, USER)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"img")

    async def test_upload_beats_op_pref(self) -> None:
        """防御性优先级：两槽位通过公共 API 互斥（后写覆盖），
        此处直写文件构造共存态，验证上传图仍优先。"""
        self._write_upload()
        user_op_bg_path(BOT, USER).write_text(
            '{"char_id": "char_002_amiya", "name": "阿米娅", "rarity": 4}',
            encoding="utf-8",
        )
        bg, label = await resolve_status_bg(BOT, USER, chars=[])
        self.assertEqual(label, "自定义")
        self.assertTrue(bg.startswith("data:image/"))

    async def test_op_pref_follows_current_skin(self) -> None:
        """未钉皮肤时跟随游戏内当前穿戴皮肤（skin_id 来自 chars）。"""
        save_op_bg(BOT, USER, **OP)
        chars = [
            SimpleNamespace(
                char_id="char_002_amiya",
                skin_id="char_002_amiya@sumeru#1",
                name="阿米娅",
            )
        ]
        with mock.patch(
            "ArknightsUID.utils.render.get_image_b64_with_cache",
            return_value="B64",
        ) as fetch:
            bg, label = await resolve_status_bg(BOT, USER, chars=chars)
        self.assertEqual((bg, label), ("B64", "阿米娅"))
        url = fetch.await_args.args[0]
        self.assertIn("char_002_amiya%40sumeru%231", url)  # @/# 已编码

    async def test_op_pref_pinned_skin_ignores_chars(self) -> None:
        pinned = {**OP, "skin_id": "char_002_amiya@winter#1", "name": "阿米娅·报童"}
        save_op_bg(BOT, USER, **pinned)
        chars = [SimpleNamespace(char_id="char_002_amiya", skin_id="", name="阿米娅")]
        with mock.patch(
            "ArknightsUID.utils.render.get_image_b64_with_cache",
            return_value="B64",
        ) as fetch:
            bg, label = await resolve_status_bg(BOT, USER, chars=chars)
        self.assertEqual((bg, label), ("B64", "阿米娅·报童"))
        self.assertIn("char_002_amiya%40winter%231", fetch.await_args.args[0])

    async def test_op_pref_not_owned_falls_back_to_default_portrait(self) -> None:
        """未持有该干员（chars 无此 char_id）→ char_portrait_b64 默认立绘档。"""
        save_op_bg(BOT, USER, **OP)
        chars = [SimpleNamespace(char_id="char_x", skin_id="char_x#1", name="其他")]
        with mock.patch(
            "ArknightsUID.utils.player.context.char_portrait_b64",
            return_value="B64",
        ) as portrait:
            bg, label = await resolve_status_bg(BOT, USER, chars=chars)
        self.assertEqual((bg, label), ("B64", "阿米娅"))
        portrait.assert_awaited_once_with("char_002_amiya", 4)

    async def test_op_fetch_failure_falls_through(self) -> None:
        """干员立绘取图失败不落错：继续走随机/兜底，而不是卡死或出碎图。"""
        save_op_bg(BOT, USER, **OP)
        with mock.patch(
            "ArknightsUID.utils.render.get_image_b64_with_cache", return_value=""
        ):
            bg, label = await resolve_status_bg(BOT, USER, chars=[])
        self.assertEqual((bg, label), ("", ""))


class MatchSkinTest(unittest.TestCase):
    CATALOG = {
        "char_002_amiya@sumeru#1": {
            "name": "报童", "group": "忒斯特收藏/I", "char_id": "char_002_amiya",
        },
        "char_002_amiya@winter#1": {
            "name": "见习联结者", "group": "时代系列", "char_id": "char_002_amiya",
        },
        "char_103_angel@beach#1": {
            "name": "夏卉", "group": "珊瑚海岸", "char_id": "char_103_angel",
        },
    }

    def test_exact_match(self) -> None:
        sid, name, all_names = match_skin(self.CATALOG, "char_002_amiya", "报童")
        self.assertEqual(sid, "char_002_amiya@sumeru#1")
        self.assertEqual(name, "报童")
        self.assertEqual(all_names, ["报童", "见习联结者"])

    def test_unique_substring_match(self) -> None:
        sid, name, _ = match_skin(self.CATALOG, "char_103_angel", "夏")
        self.assertEqual(sid, "char_103_angel@beach#1")
        self.assertEqual(name, "夏卉")

    def test_miss_lists_options(self) -> None:
        sid, _, all_names = match_skin(self.CATALOG, "char_002_amiya", "不存在")
        self.assertEqual(sid, "")
        self.assertEqual(len(all_names), 2)

    def test_ambiguous_substring_misses(self) -> None:
        catalog = {
            "a@x#1": {"name": "夜幕", "group": "", "char_id": "a"},
            "a@y#1": {"name": "夜莺挽歌", "group": "", "char_id": "a"},
        }
        sid, _, all_names = match_skin(catalog, "a", "夜")
        self.assertEqual(sid, "")
        self.assertEqual(len(all_names), 2)


if __name__ == "__main__":
    unittest.main()
