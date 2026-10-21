"""运行时优化通过调用者使用的入口验证并发、失效和文件完整性。"""

import asyncio
import threading
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ArknightsUID.utils import background, disk_cache, gacha_plan, http
from ArknightsUID.utils.enduid import adapter
from ArknightsUID.utils.player import service


def response(uid, value=1):
    return {"code": 0, "data": {"status": {"uid": uid}, "value": value}}


def test_concurrent_player_queries_share_successful_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "PLAYER_PATH", tmp_path)

    async def fetch(credential):
        await asyncio.sleep(0.01)
        return response(credential.uid)

    upstream = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(adapter, "fetch_player_info", upstream)

    async def scenario():
        results = await asyncio.gather(
            *(
                service.fetch_cached_player_info(SimpleNamespace(uid="12345"))
                for _ in range(8)
            )
        )
        assert results == [response("12345")] * 8
        assert upstream.await_count == 1

    asyncio.run(scenario())


def test_invalidation_waits_for_old_fetch_before_removing_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "PLAYER_PATH", tmp_path)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(credential):
            entered.set()
            await release.wait()
            return response(credential.uid)

        monkeypatch.setattr(adapter, "fetch_player_info", fetch)
        fetching = asyncio.create_task(
            service.fetch_cached_player_info(SimpleNamespace(uid="12345"))
        )
        await entered.wait()
        invalidating = asyncio.create_task(service.drop_cached_player_info("12345"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(fetching, invalidating)
        assert await service.read_cached_player_info("12345") is None

    asyncio.run(scenario())


def test_different_players_do_not_block_each_other(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "PLAYER_PATH", tmp_path)

    async def scenario():
        started = set()
        both = asyncio.Event()

        async def fetch(credential):
            started.add(credential.uid)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            return response(credential.uid)

        monkeypatch.setattr(adapter, "fetch_player_info", fetch)
        values = await asyncio.gather(
            *(
                service.fetch_cached_player_info(SimpleNamespace(uid=uid))
                for uid in ("12345", "67890")
            )
        )
        assert [v["data"]["status"]["uid"] for v in values] == ["12345", "67890"]

    asyncio.run(scenario())


def test_force_refresh_and_errors_preserve_cache_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "PLAYER_PATH", tmp_path)
    fetch = AsyncMock(
        side_effect=["网络失败", response("12345", 1), response("12345", 2)]
    )
    monkeypatch.setattr(adapter, "fetch_player_info", fetch)

    async def scenario():
        credential = SimpleNamespace(uid="12345")
        assert await service.fetch_cached_player_info(credential) == "网络失败"
        assert await service.fetch_cached_player_info(credential) == response(
            "12345", 1
        )
        assert await service.fetch_cached_player_info(credential) == response(
            "12345", 1
        )
        assert await service.fetch_cached_player_info(
            credential, force=True
        ) == response("12345", 2)
        assert fetch.await_count == 3

    asyncio.run(scenario())


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (4, 4), "blue").save(output, "PNG")
    return output.getvalue()


def test_background_encoding_does_not_run_on_event_loop_thread(tmp_path, monkeypatch):
    raw = image_bytes()
    monkeypatch.setattr(http, "safe_download_bytes", AsyncMock(return_value=raw))
    original_save = Image.Image.save
    main_thread = threading.get_ident()
    threads = []

    def save(image, *args, **kwargs):
        threads.append(threading.get_ident())
        return original_save(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", save)
    assert asyncio.run(
        background.save_bg_from_url(
            "https://example.test/image", tmp_path / "image.webp"
        )
    )
    assert threads and all(thread != main_thread for thread in threads)


def test_failed_background_write_preserves_previous_image(tmp_path, monkeypatch):
    raw = image_bytes()
    target = tmp_path / "image.webp"
    target.write_bytes(raw)
    monkeypatch.setattr(http, "safe_download_bytes", AsyncMock(return_value=raw))

    def fail_save(image, path, *args, **kwargs):
        from pathlib import Path

        Path(path).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(Image.Image, "save", fail_save)
    assert not asyncio.run(
        background.save_bg_from_url("https://example.test/image", target)
    )
    assert target.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [target]


def test_raw_json_cache_roundtrip_and_failed_replace(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "index.json"
        await disk_cache.write_json(path, {"name": "旧索引"})
        assert await disk_cache.read_json(path) == {"name": "旧索引"}
        with monkeypatch.context() as patch:
            patch.setattr(
                disk_cache.os,
                "replace",
                lambda *args: (_ for _ in ()).throw(OSError("disk full")),
            )
            await disk_cache.write_json(path, {"name": "新索引"})
        assert await disk_cache.read_json(path) == {"name": "旧索引"}
        assert list(tmp_path.iterdir()) == [path]
        path.write_text("{broken", encoding="utf-8")
        assert await disk_cache.read_json(path) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("pity", [0, 20, 50, 98, 1000])
def test_plan_cache_matches_probability_interface(pity):
    args = gacha_plan.parse_plan_args(f"单:70 垫刀:{pity}")
    expected = gacha_plan.build_up_table(0.35, start_pity=pity)
    for _ in range(2):
        result = gacha_plan.build_summary(args, None, pity, now_ts=1000)
        assert result.chance_now == pytest.approx(expected[70], abs=1e-12)
        assert result.chance_150 == pytest.approx(expected[220], abs=1e-12)
    # 可变结果不应污染下一次命令。
    result.notes.clear()
    expected[70] = -1
    assert gacha_plan.build_summary(args, None, pity, now_ts=1000).chance_now >= 0


def test_relic_index_rebuilds_after_source_refresh(tmp_path, monkeypatch):
    import os
    import time
    from ArknightsUID.utils.gamedata import rogue_relic

    source = tmp_path / "topics.json"
    index = tmp_path / "index.json"
    monkeypatch.setattr(rogue_relic, "ROGUE_TOPIC_TABLE_PATH", source)
    monkeypatch.setattr(rogue_relic, "RELIC_INDEX_PATH", index)
    table = {
        "topics": {"rogue_1": {"name": "主题"}},
        "details": {
            "rogue_1": {
                "relics": {"r1": {}},
                "items": {
                    "r1": {"type": "RELIC", "name": "新藏品", "usage": "新版效果"}
                },
            }
        },
    }
    monkeypatch.setattr(
        rogue_relic, "ensure_rogue_topic_table", AsyncMock(return_value=table)
    )

    async def scenario():
        await disk_cache.write_json(index, {"旧藏品": []})
        await disk_cache.cache_write(source, table)
        old = time.time() - 100
        os.utime(index, (old, old))
        result = await rogue_relic.load_relic_index()
        assert "新藏品" in result and "旧藏品" not in result
        assert await disk_cache.read_json(index) == result

    asyncio.run(scenario())


def test_image_batch_deduplicates_keys_without_reordering():
    from ArknightsUID.utils.image import gather_b64

    fetch = AsyncMock(side_effect=lambda key: "image:" + key)
    values = asyncio.run(gather_b64(fetch, ["a", "b", "a", "", "b"]))
    assert values == ["image:a", "image:b", "image:a", "", "image:b"]
    assert fetch.await_count == 2


@pytest.mark.parametrize(
    "targets, want_off, pity", [(1, 0, 98), (2, 0, 50), (1, 1, 97), (2, 1, 1000)]
)
def test_probability_matches_independent_short_tree(targets, want_off, pity):
    # 逐路径枚举短序列的参考模型，不使用生产 DP 的数组或概率聚合。
    def probability(draws, u=0, o=0, k=pity):
        if u >= targets and o >= want_off:
            return 1.0
        if not draws:
            return 0.0
        rate = min(1.0, 0.02 + 0.02 * max(0, k + 1 - 50))
        return (
            rate * 0.35 * probability(draws - 1, u + 1, o, 0)
            + rate * 0.30 * probability(draws - 1, u, o + 1, 0)
            + rate * 0.35 * probability(draws - 1, u, o, 0)
            + (1 - rate) * probability(draws - 1, u, o, k + 1)
        )

    table = gacha_plan.build_up_table(
        0.35,
        max_pulls=5,
        targets=targets,
        want_off=want_off,
        off_share=0.30,
        start_pity=pity,
    )
    assert table == pytest.approx([probability(n) for n in range(6)], abs=1e-12)
