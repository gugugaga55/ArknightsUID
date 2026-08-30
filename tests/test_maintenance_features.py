"""新移植小功能回归测试：缓存清理 / SignTime / 删除抽卡记录 / playwright 参数。"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ArknightsUID.utils import cache_clean as cc


class CacheCleanTest(unittest.IsolatedAsyncioTestCase):
    def test_clean_dir_removes_only_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stale = root / "avatar" / "old.png"
            fresh = root / "avatar" / "new.png"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"x" * 10)
            fresh.write_bytes(b"y" * 20)
            old_ts = time.time() - 40 * 86400
            os.utime(stale, (old_ts, old_ts))

            removed, freed = cc._clean_dir_sync(
                root / "avatar", time.time() - 30 * 86400
            )
            self.assertEqual(removed, 1)
            self.assertEqual(freed, 10)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())

    def test_clean_dir_missing_dir_is_noop(self) -> None:
        self.assertEqual(
            cc._clean_dir_sync(Path("/nonexistent-ark-cache"), 0.0), (0, 0)
        )

    async def test_clean_cache_directories_walks_children(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "gamedata"
            target.mkdir()
            f = target / "character_table.json"
            f.write_bytes(b"z" * 100)
            old_ts = time.time() - 40 * 86400
            os.utime(f, (old_ts, old_ts))
            with mock.patch.object(cc, "CACHE_BASE", Path(td)):
                msg = await cc.clean_cache_directories(30)
            self.assertFalse(f.exists())
            self.assertIn("删除 1 个文件", msg)
            self.assertIn("0.0MB", msg)


class SignScheduleTest(unittest.TestCase):
    def _sched(self):
        from ArknightsUID.arknightsuid_notify import _sign_schedule

        return _sign_schedule

    def test_valid_config(self) -> None:
        sched = self._sched()
        with mock.patch.object(
            __import__(
                "ArknightsUID.arknightsuid_notify", fromlist=["ArkConfig"]
            ).ArkConfig,
            "get_config",
            return_value=SimpleNamespace(data=["7", "30"]),
        ):
            self.assertEqual(sched(), (7, 30))

    def test_invalid_falls_back(self) -> None:
        sched = self._sched()
        for bad in (["25", "0"], ["8"], ["abc", "x"], []):
            with mock.patch.object(
                __import__(
                    "ArknightsUID.arknightsuid_notify", fromlist=["ArkConfig"]
                ).ArkConfig,
                "get_config",
                return_value=SimpleNamespace(data=bad),
            ):
                self.assertEqual(sched(), (8, 5))
        with mock.patch.object(
            __import__(
                "ArknightsUID.arknightsuid_notify", fromlist=["ArkConfig"]
            ).ArkConfig,
            "get_config",
            side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(sched(), (8, 5))


class PlaywrightArgsTest(unittest.TestCase):
    def test_launch_args_include_gpu_hardening(self) -> None:
        import inspect

        from ArknightsUID.utils.render import render_utils

        src = inspect.getsource(render_utils)
        self.assertIn("--disable-gpu", src)
        self.assertIn("--no-zygote", src)
        self.assertIn("--renderer-process-limit=1", src)


class DeleteGachaRecordsTest(unittest.IsolatedAsyncioTestCase):
    async def test_delete_clears_local_logs(self) -> None:
        import ArknightsUID.arknightsuid_gacha as gacha_mod
        from ArknightsUID.utils.enduid.adapter import ArkCredential

        calls: dict[str, object] = {}

        cred = ArkCredential(
            uid="123456789", nickname="Dr", server_id="1",
            cookie_status="", cred="C", token="T", hg_token="H",
            skland_user_id="S",
        )

        async def fake_resolve(user_id, bot_id, uid="", index=0):
            calls["resolved"] = True
            return cred

        async def fake_load(uid):
            return [{"gacha_ts": 1}, {"gacha_ts": 2}]

        async def fake_save(uid, records):
            calls["saved"] = records

        sent: list[str] = []

        class FakeBot:
            async def send(self, msg, **kw):
                sent.append(str(msg))

        ev = SimpleNamespace(
            user_id="u", bot_id="b", group_id="", text="",
        )
        with mock.patch.object(gacha_mod, "resolve_credential", fake_resolve), \
             mock.patch.object(gacha_mod, "load_local_logs", fake_load), \
             mock.patch.object(gacha_mod, "save_local_logs", fake_save), \
             mock.patch.object(gacha_mod, "hide_uid_for", mock.AsyncMock(return_value="123***789")):
            await gacha_mod.delete_gacha_records(FakeBot(), ev)

        self.assertEqual(calls.get("saved"), [])
        self.assertEqual(len(sent), 1)
        self.assertIn("已删除", sent[0])
        self.assertIn("123***789", sent[0])
        self.assertIn("2 条", sent[0])

    async def test_delete_with_no_records(self) -> None:
        import ArknightsUID.arknightsuid_gacha as gacha_mod
        from ArknightsUID.utils.enduid.adapter import ArkCredential

        cred = ArkCredential(
            uid="123456789", nickname="Dr", server_id="1",
            cookie_status="", cred="C", token="T", hg_token="H",
            skland_user_id="S",
        )

        async def fake_resolve(user_id, bot_id, uid="", index=0):
            return cred

        async def fake_load(uid):
            return []

        sent: list[str] = []

        class FakeBot:
            async def send(self, msg, **kw):
                sent.append(str(msg))

        ev = SimpleNamespace(user_id="u", bot_id="b", group_id="", text="")
        with mock.patch.object(gacha_mod, "resolve_credential", fake_resolve), \
             mock.patch.object(gacha_mod, "load_local_logs", fake_load), \
             mock.patch.object(gacha_mod, "save_local_logs", mock.AsyncMock()) as m_save, \
             mock.patch.object(gacha_mod, "hide_uid_for", mock.AsyncMock(return_value="123***789")):
            await gacha_mod.delete_gacha_records(FakeBot(), ev)

        m_save.assert_not_awaited()
        self.assertIn("没有抽卡记录", sent[0])


if __name__ == "__main__":
    unittest.main()


class RenderScaleTest(unittest.TestCase):
    def test_scale_config_fallback(self) -> None:
        from ArknightsUID.utils.render import render_utils as ru

        import ArknightsUID.utils.config as cfg_mod
        with mock.patch.object(
            cfg_mod.ArkConfig,
            "get_config",
            return_value=SimpleNamespace(data=3),
        ):
            self.assertEqual(ru._render_scale(), 3.0)
        with mock.patch.object(
            cfg_mod.ArkConfig,
            "get_config",
            return_value=SimpleNamespace(data="bad"),
        ):
            self.assertEqual(ru._render_scale(), 2.0)
        with mock.patch.object(
            cfg_mod.ArkConfig,
            "get_config",
            side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(ru._render_scale(), 2.0)

    def test_takumi_passes_width_times_scale_and_dpr(self) -> None:
        """pytakumi 的 width 是物理像素、CSS 视口=width/DPR：
        必须传 width×scale 才能保持 560px 设计宽，否则布局会被压窄。"""
        import ArknightsUID.utils.config as cfg_mod
        from ArknightsUID.utils.render import render_utils as ru

        captured: dict = {}

        def fake_html_to_pic(html, **kwargs):
            captured.update(kwargs)
            return b"fake-jpeg"

        with mock.patch.object(ru, "pytakumi") as fake_mod, mock.patch.object(
            cfg_mod.ArkConfig,
            "get_config",
            return_value=SimpleNamespace(data=2),
        ):
            fake_mod.html_to_pic.side_effect = fake_html_to_pic
            ru._render_via_takumi("<html></html>")

        self.assertEqual(captured.get("width"), 1120)  # 560 × 2
        self.assertEqual(captured.get("device_pixel_ratio"), 2)

    def test_playwright_context_has_device_scale_factor(self) -> None:
        import inspect

        from ArknightsUID.utils.render import render_utils

        src = inspect.getsource(render_utils)
        self.assertIn("device_scale_factor=_render_scale()", src)
