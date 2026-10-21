"""CDN 缺文件时仍能从同一上游分支获取难度；错误响应不能覆盖有效表。"""

import json
from unittest.mock import AsyncMock

import pytest

from ArknightsUID.utils.disk_cache import cache_read
from ArknightsUID.utils.gamedata import rogue_enemy as enemies


@pytest.mark.asyncio
async def test_difficulty_falls_back_to_origin_when_cdn_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(enemies, "THEME_DIRS", {"phantom": "傀影与猩红孤钻"})
    monkeypatch.setattr(enemies, "IS_ENEMY_CACHE_DIR", tmp_path)
    rows = [
        {
            "difficulty": 5,
            "effects": [
                {
                    "targets": ["BOSS"],
                    "mods": [{"key": "hp", "value": 1.2, "mode": "mul"}],
                }
            ],
        }
    ]
    download = AsyncMock(side_effect=[None, json.dumps(rows).encode()])
    monkeypatch.setattr(enemies, "download_bytes", download)
    assert await enemies.load_difficulty_mods() == {"phantom": rows}
    assert download.await_count == 2
    assert (
        download.await_args_list[1].args[0]
        == "https://raw.githubusercontent.com/tanch-p/tomimi-dev/main/src/lib/data/is/phantom/difficulty_mods_phantom.json"
    )
    cached, _ = await cache_read(tmp_path / "difficulty_phantom.json")
    assert cached == rows


@pytest.mark.asyncio
async def test_invalid_difficulty_responses_keep_previous_table(monkeypatch, tmp_path):
    monkeypatch.setattr(enemies, "THEME_DIRS", {"phantom": "傀影与猩红孤钻"})
    monkeypatch.setattr(enemies, "IS_ENEMY_CACHE_DIR", tmp_path)
    rows = [{"difficulty": 5, "effects": []}]
    path = tmp_path / "difficulty_phantom.json"
    path.write_text(json.dumps({"_cache_time": 1, "payload": rows}), encoding="utf-8")
    download = AsyncMock(
        side_effect=[
            b'{"error": "upstream unavailable"}',
            b"<html>gateway error</html>",
        ]
    )
    monkeypatch.setattr(enemies, "download_bytes", download)
    assert await enemies.load_difficulty_mods() == {"phantom": rows}
    cached, _ = await cache_read(path)
    assert cached == rows
