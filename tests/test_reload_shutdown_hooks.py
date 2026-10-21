"""审计 #34：热重载执行插件归属的 @on_core_shutdown 钩子。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ReloadShutdownHooksTest(unittest.TestCase):
    def test_only_plugin_owned_hooks_run(self) -> None:

        from gsuid_core.server import _DefHook, core_shutdown_def
        from gsuid_core.utils.plugins_update.reload_plugin import (
            _run_plugin_shutdown_hooks,
        )

        called: list[str] = []

        async def ark_http_close():  # __module__ 被手动改为插件模块名
            called.append("ark")

        async def other_plugin_close():
            called.append("other")

        def sync_hook():
            called.append("sync")

        ark_http_close.__module__ = "ArknightsUID.utils.http"
        sync_hook.__module__ = "plugins.Demo.utils.x"
        other_plugin_close.__module__ = "SomeOtherPlugin"

        added = {
            _DefHook(priority=0, func=ark_http_close),
            _DefHook(priority=0, func=sync_hook),
            _DefHook(priority=0, func=other_plugin_close),
        }
        original = set(core_shutdown_def)
        core_shutdown_def.update(added)
        try:
            _run_plugin_shutdown_hooks("ArknightsUID")
            self.assertEqual(called, ["ark"])  # async 归属钩子被执行
            _run_plugin_shutdown_hooks("Demo")
            self.assertEqual(called, ["ark", "sync"])  # sync 钩子也执行
            _run_plugin_shutdown_hooks("NoSuchPlugin")  # 无归属：无操作
            self.assertEqual(called, ["ark", "sync"])
        finally:
            core_shutdown_def.clear()
            core_shutdown_def.update(original)

    def test_failing_hook_does_not_break_reload(self) -> None:
        from gsuid_core.server import _DefHook, core_shutdown_def
        from gsuid_core.utils.plugins_update.reload_plugin import (
            _run_plugin_shutdown_hooks,
        )

        async def bad():
            raise RuntimeError("boom")

        bad.__module__ = "ArknightsUID.utils.http"

        original = set(core_shutdown_def)
        core_shutdown_def.add(_DefHook(priority=0, func=bad))
        try:
            _run_plugin_shutdown_hooks("ArknightsUID")  # 不抛即通过
        finally:
            core_shutdown_def.clear()
            core_shutdown_def.update(original)


if __name__ == "__main__":
    unittest.main()
