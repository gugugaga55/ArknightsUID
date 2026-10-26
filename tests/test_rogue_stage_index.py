"""出没索引按期构建、有限等待、缓存寿命与任务回收。"""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from ArknightsUID.utils.gamedata import rogue_enemy as enemies


@pytest_asyncio.fixture(autouse=True)
async def isolated_indexes():
    if hasattr(enemies, "clear_stage_index_cache"):
        await enemies.clear_stage_index_cache()
    yield
    if hasattr(enemies, "clear_stage_index_cache"):
        await enemies.clear_stage_index_cache()


@pytest.mark.asyncio
async def test_selected_theme_only_builds_its_own_stages(monkeypatch):
    meta = AsyncMock(return_value={"level_rogue6_1-1": {"name": "测试关卡"}})
    stage = AsyncMock(return_value={"enemies": [{"id": "enemy_one"}]})
    monkeypatch.setattr(enemies, "_load_stage_meta", meta)
    monkeypatch.setattr(enemies, "_load_stage_file", stage)

    index, status = await enemies.load_stage_appearances("black", timeout=0.2)

    assert status == "ready" and index["enemy_one"][0]["theme"] == "black"
    meta.assert_awaited_once_with("black")
    stage.assert_awaited_once_with("level_rogue6_1-1")


@pytest.mark.asyncio
async def test_slow_index_is_shared_and_continues_after_wait_timeout(monkeypatch):
    release = asyncio.Event()
    expected = {"enemy_one": [{"theme": "black"}]}

    async def slow(theme):
        await release.wait()
        return expected

    build = AsyncMock(side_effect=slow)
    monkeypatch.setattr(enemies, "_do_build_stage_index", build)
    results = await asyncio.gather(
        enemies.load_stage_appearances("black", timeout=0.001),
        enemies.load_stage_appearances("black", timeout=0.001),
    )
    assert results == [({}, "loading"), ({}, "loading")]
    build.assert_awaited_once_with("black")
    release.set()
    assert await enemies.load_stage_appearances("black", timeout=0.2) == (
        expected,
        "ready",
    )


@pytest.mark.asyncio
async def test_legacy_api_waits_for_complete_shared_index_without_scoped_argument(
    monkeypatch,
):
    expected = {"enemy_one": []}
    build = AsyncMock(return_value=expected)
    monkeypatch.setattr(enemies, "_do_build_stage_index", build)
    assert await enemies.build_stage_index() == expected
    assert await enemies.build_stage_index() == expected
    build.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_background_failure_is_reported_and_backed_off(monkeypatch):
    build = AsyncMock(side_effect=RuntimeError("upstream failed"))
    warning = Mock()
    monkeypatch.setattr(enemies, "_do_build_stage_index", build)
    monkeypatch.setattr(enemies.logger, "warning", warning)
    assert await enemies.load_stage_appearances("black", timeout=0.2) == ({}, "failed")
    assert await enemies.load_stage_appearances("black", timeout=0.2) == ({}, "failed")
    build.assert_awaited_once()
    assert "RuntimeError" in str(warning.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["meta", "stage"])
async def test_incomplete_sources_are_failed_instead_of_ready(monkeypatch, missing):
    meta = {} if missing == "meta" else {"level_rogue6_1-1": {"name": "测试关卡"}}
    monkeypatch.setattr(enemies, "_load_stage_meta", AsyncMock(return_value=meta))
    monkeypatch.setattr(enemies, "_load_stage_file", AsyncMock(return_value={}))
    assert await enemies.load_stage_appearances("black", timeout=0.2) == ({}, "failed")


@pytest.mark.asyncio
async def test_partial_failed_index_keeps_successful_stage_information(monkeypatch):
    monkeypatch.setattr(
        enemies, "_load_stage_meta", AsyncMock(return_value={"ok": {}, "missing": {}})
    )

    async def stage(level_id):
        return {"enemies": [{"id": "enemy_one"}]} if level_id == "ok" else {}

    monkeypatch.setattr(enemies, "_load_stage_file", stage)
    index, status = await enemies.load_stage_appearances("black", timeout=0.2)
    assert status == "failed" and "enemy_one" in index


@pytest.mark.asyncio
async def test_valid_empty_enemy_roster_is_complete(monkeypatch):
    monkeypatch.setattr(
        enemies, "_load_stage_meta", AsyncMock(return_value={"empty": {}})
    )
    monkeypatch.setattr(
        enemies, "_load_stage_file", AsyncMock(return_value={"enemies": []})
    )
    assert await enemies.load_stage_appearances("black", timeout=0.2) == ({}, "ready")


@pytest.mark.asyncio
async def test_success_is_rebuilt_after_twenty_four_hours(monkeypatch):
    build = AsyncMock(side_effect=[{"old": []}, {"new": []}])
    monkeypatch.setattr(enemies, "_do_build_stage_index", build)
    assert await enemies.load_stage_appearances("black", timeout=0.2) == (
        {"old": []},
        "ready",
    )
    enemies._stage_index_states["black"].saved_at -= 86401
    assert await enemies.load_stage_appearances("black", timeout=0.2) == (
        {"new": []},
        "ready",
    )
    assert build.await_count == 2


@pytest.mark.asyncio
async def test_refresh_cancels_and_awaits_background_task(monkeypatch):
    finished = asyncio.Event()

    async def slow(theme):
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(enemies, "_do_build_stage_index", slow)
    assert await enemies.load_stage_appearances("black", timeout=0.001) == (
        {},
        "loading",
    )
    await enemies.clear_stage_index_cache()
    assert finished.is_set()
    assert not enemies._stage_index_states


@pytest.mark.asyncio
async def test_refresh_generation_prevents_cancel_suppressing_task_from_writing(
    monkeypatch,
):
    async def ignores_cancellation():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"obsolete": []}

    monkeypatch.setattr(enemies, "_do_build_stage_index", ignores_cancellation)
    assert await enemies.load_stage_appearances(timeout=0.001) == ({}, "loading")
    await enemies.clear_stage_index_cache()
    assert enemies._stage_index_cache is None
    assert not enemies._stage_index_states
    monkeypatch.setattr(
        enemies, "_do_build_stage_index", AsyncMock(return_value={"new": []})
    )
    assert await enemies.build_stage_index() == {"new": []}


def test_shutdown_cancels_indexes_before_shared_http_client_closes():
    from gsuid_core.server import core_shutdown_def

    index_hook = next(
        h for h in core_shutdown_def if h.func is enemies.clear_stage_index_cache
    )
    http_hook = next(
        h for h in core_shutdown_def if h.func.__name__ == "_ark_http_close"
    )
    assert index_hook.priority < http_hook.priority


@pytest.mark.asyncio
async def test_selected_difficulty_only_fetches_one_theme(monkeypatch, tmp_path):
    rows = [{"difficulty": 5, "effects": []}]
    download = AsyncMock(return_value=json.dumps(rows).encode())
    monkeypatch.setattr(enemies, "IS_ENEMY_CACHE_DIR", tmp_path)
    monkeypatch.setattr(enemies, "download_bytes", download)
    assert await enemies.load_difficulty_mods("black") == {"black": rows}
    download.assert_awaited_once_with(enemies._diff_file_url("black"), timeout=20.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["new", "changed", "unchanged"])
async def test_refresh_waits_for_submitted_disk_write_before_removing_cache(
    monkeypatch, tmp_path, response_kind
):
    from ArknightsUID.utils import disk_cache

    level_id = "level_rogue6_6-2"
    path = tmp_path / "ro6" / f"{level_id}.json"
    path.parent.mkdir()
    sidecar = path.with_name(path.name + ".validators.json")
    payload = {"enemies": [{"id": "enemy_old"}]}
    if response_kind != "new":
        path.write_text(json.dumps({"_cache_time": 1, "payload": payload}))
        sidecar.write_text('{"etag":"old"}')
    response = (
        (304, None, {})
        if response_kind == "unchanged"
        else (200, json.dumps(payload).encode(), {"etag": "new"})
    )
    monkeypatch.setattr(
        enemies, "download_bytes_conditional", AsyncMock(return_value=response)
    )
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_write = disk_cache._write_sync

    def slow_write(target, data):
        if target == path:
            started.set()
            assert release.wait(3), "test did not release disk writer"
        original_write(target, data)
        if target == path:
            finished.set()

    monkeypatch.setattr(disk_cache, "_write_sync", slow_write)
    monkeypatch.setattr(enemies, "_STAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        enemies, "_load_stage_meta", AsyncMock(return_value={level_id: {}})
    )
    assert await enemies.load_stage_appearances("black", timeout=0.001) == (
        {},
        "loading",
    )
    assert await asyncio.to_thread(started.wait, 1), "disk write never started"
    clear = asyncio.create_task(enemies.clear_stage_index_cache())
    try:
        done, _pending = await asyncio.wait({clear}, timeout=0.02)
        assert not done, "refresh returned while a cache write was still running"
        release.set()
        await clear
        assert finished.is_set()
        path.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        await asyncio.sleep(0)
        assert not path.exists() and not sidecar.exists()
        assert not enemies._stage_index_states and enemies._stage_index_cache is None
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 1)
        await asyncio.gather(clear, return_exceptions=True)


@pytest.mark.asyncio
async def test_refresh_can_cancel_network_phase_without_waiting_for_download(
    monkeypatch, tmp_path
):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def download(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(enemies, "download_bytes_conditional", download)
    monkeypatch.setattr(enemies, "_STAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        enemies, "_load_stage_meta", AsyncMock(return_value={"level_rogue6_6-2": {}})
    )
    assert await enemies.load_stage_appearances("black", timeout=0.001) == (
        {},
        "loading",
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)
    await asyncio.wait_for(enemies.clear_stage_index_cache(), timeout=0.2)
    assert cancelled.is_set()
