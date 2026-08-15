from __future__ import annotations

import sys
import types
import unittest
from typing import Any


def _install_gsuid_core_stubs() -> None:
    class FakeSV:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def on_command(self, *args: Any, **kwargs: Any) -> Any:
            return lambda func: func

        def on_fullmatch(self, *args: Any, **kwargs: Any) -> Any:
            return lambda func: func

    bot_module = types.ModuleType("gsuid_core.bot")
    bot_module.Bot = object
    models_module = types.ModuleType("gsuid_core.models")
    models_module.Event = object
    logger_module = types.ModuleType("gsuid_core.logger")
    logger_module.logger = types.SimpleNamespace(debug=lambda message: None, warning=lambda message: None)
    sv_module = types.ModuleType("gsuid_core.sv")
    sv_module.Plugins = lambda *args, **kwargs: None
    sv_module.SV = FakeSV

    gsuid_core_module = types.ModuleType("gsuid_core")
    gsuid_core_module.__path__ = []
    sys.modules.setdefault("gsuid_core", gsuid_core_module)
    sys.modules["gsuid_core.bot"] = bot_module
    sys.modules["gsuid_core.models"] = models_module
    sys.modules["gsuid_core.logger"] = logger_module
    sys.modules["gsuid_core.sv"] = sv_module


_install_gsuid_core_stubs()

from ArknightsUID.arknightsuid_account import _extract_index, _extract_uid  # noqa: E402


class AccountParsingTest(unittest.TestCase):
    def test_extract_uid_returns_first_uid_like_number(self) -> None:
        self.assertEqual(_extract_uid("理智 123456789"), "123456789")
        self.assertEqual(_extract_uid("签到 #12 987654321"), "987654321")
        self.assertEqual(_extract_uid("理智 1234"), "")

    def test_extract_index_uses_trailing_small_number_unless_uid_exists(self) -> None:
        self.assertEqual(_extract_index("理智 2"), 2)
        self.assertEqual(_extract_index("理智 第2号"), 2)
        self.assertEqual(_extract_index("理智 #12"), 12)
        self.assertEqual(_extract_index("理智 123456789"), 0)
        self.assertEqual(_extract_index("理智 abc"), 0)


if __name__ == "__main__":
    unittest.main()
