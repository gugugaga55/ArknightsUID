"""森空岛官方图表资源表测试（utils/rogue/skland_assets.py）。

表内容来自 game.skland.com H5 chunk 提取（模块头注释有溯源），
这里锁定结构契约：6 期齐全、键语义、URL 域名与缓存降级。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.rogue.skland_assets import (
    SKLAND_TOPIC_ASSETS,
    skland_asset_url,
)


class SklandAssetsTest(unittest.TestCase):
    def test_table_covers_all_topics(self) -> None:
        # 主题表口径 rogue_1..rogue_6（傀影起编），六期齐全
        self.assertEqual(
            set(SKLAND_TOPIC_ASSETS), {f"rogue_{i}" for i in range(1, 7)}
        )

    def test_common_keys_per_topic(self) -> None:
        # 战绩卡消费的三个键每期必须有（其余键允许逐期差异）
        for topic_id, assets in SKLAND_TOPIC_ASSETS.items():
            for key in ("profileBg", "historyCardTitleSuccess", "historyCardTitleFail"):
                self.assertIn(key, assets, f"{topic_id} 缺 {key}")

    def test_urls_are_official_cdn(self) -> None:
        for topic_id, assets in SKLAND_TOPIC_ASSETS.items():
            for key, url in assets.items():
                self.assertTrue(
                    url.startswith("https://bbs.hycdn.cn/public/skland-game/"),
                    f"{topic_id}.{key} 非官方 CDN: {url}",
                )

    def test_lookup_misses(self) -> None:
        self.assertEqual(skland_asset_url("rogue_6", "careerBg")[:8], "https://")
        self.assertEqual(skland_asset_url("rogue_99", "careerBg"), "")
        self.assertEqual(skland_asset_url("rogue_6", "no_such_key"), "")


if __name__ == "__main__":
    unittest.main()
