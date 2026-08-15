from __future__ import annotations

import unittest

from ArknightsUID.arknightsuid_account import _extract_index, _extract_uid


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
