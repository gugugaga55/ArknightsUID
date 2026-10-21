"""离线运行时基准：真实计算/缓存/图像处理，模拟 HTTP，不读线上账号。

使用 GsCore 解释器在插件根运行：python tools/benchmark_runtime.py
输出 JSON；数值只代表这些受控场景，不代表整条机器人命令的耗时。
"""

from __future__ import annotations

import asyncio
import io
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from runtime_isolation import isolate_data_root


def median_ms(fn, repeats=5):
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        values.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(values), 3)


async def loop_timing(work):
    running = True
    delays = []

    async def heartbeat():
        while running:
            start = time.perf_counter()
            await asyncio.sleep(0.002)
            delays.append((time.perf_counter() - start) * 1000)

    heartbeat_task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    start = time.perf_counter()
    try:
        await work()
    finally:
        elapsed = (time.perf_counter() - start) * 1000
        running = False
        await heartbeat_task
    return round(elapsed, 3), round(max(delays), 3)


async def benchmark(directory):
    from ArknightsUID.utils import background, disk_cache, gacha_plan, http
    from ArknightsUID.utils.enduid import adapter
    from ArknightsUID.utils.gamedata import _table_cache
    from ArknightsUID.utils.player import service

    result = {}
    result["probability_300_ms"] = median_ms(lambda: gacha_plan.build_up_table(0.35))
    result["probability_1200_ms"] = median_ms(
        lambda: gacha_plan.build_up_table(0.35, max_pulls=1200), 3
    )
    args = gacha_plan.parse_plan_args("玉:60000 垫刀:20")

    def make_plan():
        return gacha_plan.build_summary(args, None, 20, now_ts=1000)

    make_plan()
    result["warm_plan_ms"] = median_ms(make_plan)

    requests = 0

    async def player_fetch(credential):
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.03)
        return {
            "code": 0,
            "data": {"status": {"uid": credential.uid}, "blob": "x" * 100_000},
        }

    with patch.object(adapter, "fetch_player_info", player_fetch):
        await asyncio.gather(
            *(
                service.fetch_cached_player_info(SimpleNamespace(uid="123456789"))
                for _ in range(8)
            )
        )
    result["player_burst_requests"] = requests

    raw_image = Image.frombytes(
        "L", (2048, 2048), random.Random(42).randbytes(2048 * 2048)
    )
    buffer = io.BytesIO()
    raw_image.save(buffer, "PNG")
    raw = buffer.getvalue()

    async def image_download(*args, **kwargs):
        return raw

    async def save_background():
        assert await background.save_bg_from_url(
            "https://example.test/image", directory / "bg.webp"
        )

    with patch.object(http, "safe_download_bytes", image_download):
        image_times = [await loop_timing(save_background) for _ in range(3)]
    result["background_total_ms"] = round(
        statistics.median(t[0] for t in image_times), 3
    )
    result["background_loop_gap_ms"] = round(
        statistics.median(t[1] for t in image_times), 3
    )

    index_file = directory / "index.json"
    index_file.write_text(
        json.dumps(
            {
                str(i): {"name": "干员" + str(i), "details": "x" * 80}
                for i in range(10000)
            }
        )
    )

    async def read_indexes():
        for _ in range(30):
            # 兼容改动前的接口，便于用同一脚本比较基线。
            if hasattr(disk_cache, "read_json"):
                value = await disk_cache.read_json(index_file)
            else:
                value = _table_cache._read_sidecar(index_file)
            assert len(value) == 10000

    result["index_total_ms"], result["index_loop_gap_ms"] = await loop_timing(
        read_indexes
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="arkuid-benchmark-") as temp:
        directory = Path(temp)
        isolate_data_root(directory)
        asyncio.run(benchmark(directory))
