"""缓存与下载层加固回归测试（审计 #14/#16/#17/#18/#19/#21/#22/#30）。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _png_bytes(size: int = 4) -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (size, size)).save(buf, "PNG")
    return buf.getvalue()


class AllImagesCursorFuseTest(unittest.IsolatedAsyncioTestCase):
    """审计 #14：PRTS 返回重复游标/空页时不得无限循环。"""

    async def test_repeated_cursor_breaks_loop(self) -> None:
        import ArknightsUID.utils.prts.client as prts

        calls = {"n": 0}

        async def fake_api(params, **kwargs):
            calls["n"] += 1
            # 恒定返回同一 aicontinue 且无结果（缓存命中的异常响应形态）
            return {"query": {"allimages": []}, "continue": {"aicontinue": "x"}}

        with mock.patch.object(prts, "_api", new=fake_api):
            titles = await prts.all_image_titles("道具_")

        self.assertEqual(titles, [])
        self.assertLessEqual(calls["n"], 3, "重复游标必须尽快熔断")


class DownloadTargetMissTest(unittest.IsolatedAsyncioTestCase):
    """审计 #16/.miss 生效 + #17 200 非图片拒写。"""

    @staticmethod
    def _target(tmp: str) -> "object":
        from ArknightsUID.utils.resource.bulk_download import ResourceTarget

        return ResourceTarget(
            "https://cdn.example.com/a/shot.png",
            Path(tmp), "shot.png.miss",
        )

    async def test_fresh_miss_skips_request(self) -> None:
        import ArknightsUID.utils.resource.bulk_download as bd

        with tempfile.TemporaryDirectory() as tmp:
            target = self._target(tmp)
            (target.cache_dir / target.miss_marker).write_text("", encoding="utf-8")
            with mock.patch.object(
                bd, "download_bytes_status", new=mock.AsyncMock()
            ) as m_dl:
                result = await bd.download_target(target)
            self.assertEqual(result, "skip")
            m_dl.assert_not_awaited()  # TTL 内已知 404 不再打 CDN

    async def test_expired_miss_refetches_and_clears_on_success(self) -> None:
        import os
        import time as _time

        import ArknightsUID.utils.resource.bulk_download as bd

        with tempfile.TemporaryDirectory() as tmp:
            target = self._target(tmp)
            marker = target.cache_dir / target.miss_marker
            marker.write_text("", encoding="utf-8")
            old = _time.time() - 8 * 86400
            os.utime(marker, (old, old))

            with mock.patch.object(
                bd, "download_bytes_status",
                new=mock.AsyncMock(return_value=(200, _png_bytes())),
            ):
                result = await bd.download_target(target)
            self.assertEqual(result, "ok")
            # 资源恢复后旧 .miss 被清除（不再压住惰性路径）
            self.assertFalse(marker.exists())
            self.assertTrue((target.cache_dir / "shot.webp").exists())

    async def test_200_html_rejected_not_cached(self) -> None:
        import ArknightsUID.utils.resource.bulk_download as bd

        with tempfile.TemporaryDirectory() as tmp:
            target = self._target(tmp)
            with mock.patch.object(
                bd, "download_bytes_status",
                new=mock.AsyncMock(return_value=(200, b"<html>err</html>")),
            ):
                result = await bd.download_target(target)
            self.assertEqual(result, "fail")
            # 坏内容不得落盘（存在即 skip 的永久坏缓存）
            self.assertFalse((target.cache_dir / "shot.png").exists())
            self.assertFalse((target.cache_dir / "shot.webp").exists())


class MapNegativeCacheTest(unittest.IsolatedAsyncioTestCase):
    """审计 #18：只有确认 404 才写地图负缓存。"""

    def _make_status(self, status_map: dict):
        async def fake_status(url, **kwargs):
            for prefix, value in status_map.items():
                if prefix in url:
                    return value
            return 404, None
        return fake_status

    async def _run(self, status_map: dict, tmp: str) -> str:
        import ArknightsUID.arknightsuid_wiki as wiki
        import ArknightsUID.utils.http as http_mod
        import ArknightsUID.utils.resource.RESOURCE_PATH as rp

        with mock.patch.object(
            http_mod, "download_bytes_status", self._make_status(status_map)
        ), mock.patch.object(rp, "CACHE_PATH", Path(tmp)):
            return await wiki._get_stage_map_b64("1-7", "暴君")

    async def test_network_failure_writes_no_negative_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = await self._run({"media.prts.wiki": (0, None)}, tmp)
            cache_dir = Path(tmp) / "wiki" / "map"
        self.assertEqual(result, "")
        # 网络失败（status=0）：一个 .miss / nomap 都不该写
        self.assertEqual(list(cache_dir.glob("*.miss")), [])
        self.assertEqual(list(cache_dir.glob("nomap_*")), [])

    async def test_stale_marker_plus_network_failure_writes_no_stage_nomap(self) -> None:
        """审计 #39：标记过期后重查遇网络失败（非 404）不得写关卡级 nomap。

        旧版 all_confirmed_missing 只查 exists()：过期的 .miss + 网络抖动
        （未刷新标记）也会被判"确认缺失"，一次抖动放大成 7 天无图。
        """
        import os
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            # 第一轮全 404：per-URL 标记 + 关卡级 nomap 均写入
            await self._run({}, tmp)
            cache_dir = Path(tmp) / "wiki" / "map"
            nomaps = list(cache_dir.glob("nomap_*"))
            self.assertTrue(nomaps)
            # 全部标记回拨到 8 天前（TTL=7 天外，进入"待重查"状态）
            stale = _time.time() - 8 * 86400
            for marker in list(cache_dir.glob("*.miss")) + nomaps:
                os.utime(marker, (stale, stale))
            # 第二轮网络失败：标记不会被刷新，也不能当"确认缺失"
            await self._run({"media.prts.wiki": (0, None)}, tmp)
            for marker in cache_dir.glob("nomap_*"):
                self.assertLess(
                    marker.stat().st_mtime,
                    _time.time() - 86400,
                    "网络失败不得刷新/新写关卡级 nomap 标记",
                )

    async def test_confirmed_404_writes_url_markers_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = await self._run({}, tmp)  # 全部 404
            cache_dir = Path(tmp) / "wiki" / "map"
            misses = list(cache_dir.glob("*.miss"))
            nomaps = list(cache_dir.glob("nomap_*"))
        self.assertEqual(result, "")
        self.assertTrue(misses, "确认 404 应写 per-URL 负缓存")
        # 且候选全 404 → 关卡级 nomap 也写入
        self.assertTrue(nomaps)


class ImageSingleFlightTest(unittest.IsolatedAsyncioTestCase):
    """审计 #19：同路径并发下载 single-flight；烘焙原子写。"""

    async def test_concurrent_download_hits_network_once(self) -> None:
        import ArknightsUID.utils.image as img_mod

        calls = {"n": 0}

        async def fake_download(url, path, name, tag=None, **kwargs):
            calls["n"] += 1
            (Path(path) / name).write_bytes(_png_bytes(), )
            return 200

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "gsuid_core.utils.download_resource.download_file.download",
            new=fake_download,
        ):
            await asyncio.gather(
                img_mod.pic_download_from_url(Path(tmp), "https://x/one.png"),
                img_mod.pic_download_from_url(Path(tmp), "https://x/one.png"),
            )
        self.assertEqual(calls["n"], 1, "并发同 URL 只应下载一次")

    def test_bake_uses_atomic_replace(self) -> None:
        import inspect

        from ArknightsUID.utils.render import render_utils

        source = inspect.getsource(render_utils.get_image_b64_with_cache)
        self.assertIn("os.replace", source)


class SkinUrlBareIdTest(unittest.TestCase):
    """审计 #21：裸 charId 的皮肤 URL 补 #1（hycdn 无后缀路径恒 404）。"""

    def test_bare_char_id_gets_default_tier(self) -> None:
        from ArknightsUID.utils.player.context import (
            skin_avatar_url,
            skin_portrait_url,
        )

        self.assertTrue(skin_avatar_url("char_002_amiya").endswith("%231.png"))
        self.assertTrue(skin_portrait_url("char_002_amiya").endswith("%231.png"))

    def test_suffixed_ids_unchanged(self) -> None:
        from ArknightsUID.utils.player.context import skin_avatar_url

        self.assertTrue(skin_avatar_url("char_002_amiya@2").endswith("%232.png"))
        self.assertTrue(
            skin_avatar_url("char_002_amiya@2019_nl#2").endswith("%402019_nl%232.png")
        )


class RemoteRenderMagicTest(unittest.IsolatedAsyncioTestCase):
    """审计 #22：远程渲染 200 非图片必须回退本地。"""

    def test_magic_check(self) -> None:
        from ArknightsUID.utils.render.render_utils import _looks_like_rendered_image

        self.assertTrue(_looks_like_rendered_image(b"\xff\xd8\xff\xe0..."))
        self.assertTrue(_looks_like_rendered_image(b"\x89PNG\r\n\x1a\n..."))
        self.assertTrue(
            _looks_like_rendered_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
        )
        self.assertFalse(_looks_like_rendered_image(b'{"error":1}'))
        self.assertFalse(_looks_like_rendered_image(b"<html>502</html>"))
        self.assertFalse(_looks_like_rendered_image(b""))

    async def test_remote_non_image_returns_none(self) -> None:
        import httpx

        from ArknightsUID.utils.render import render_utils as ru

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text='{"detail":"upstream error"}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with mock.patch.object(
                ru, "get_client", new=mock.AsyncMock(return_value=client)
            ):
                result = await ru._render_via_remote("<p>x</p>", "https://r.x/render")
        finally:
            await client.aclose()
        self.assertIsNone(result, "非图片内容必须按失败处理以触发本地回退")


class PrtsErrorEnvelopeTest(unittest.IsolatedAsyncioTestCase):
    """审计 #30：HTTP 200 的 MediaWiki error 信封不进长 TTL 缓存。"""

    async def test_error_envelope_not_cached(self) -> None:
        import httpx

        import ArknightsUID.utils.prts.client as prts

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"error": {"code": "ratelimited"}}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        tmp = tempfile.TemporaryDirectory()
        try:
            with mock.patch.object(
                prts, "get_client", new=mock.AsyncMock(return_value=client)
            ), mock.patch.object(
                prts, "_throttle", new=mock.AsyncMock()
            ), mock.patch.object(
                prts, "_cache_file",
                new=lambda params: Path(tmp.name) / "c.json",
            ), mock.patch.object(
                prts, "_cleanup_stale_cache", new=lambda: None,
            ), mock.patch.object(
                prts, "_load_stale_cache", new=mock.AsyncMock(return_value=None),
            ):
                result = await prts._api({"action": "query"})
            self.assertIsNone(result)
            self.assertEqual(
                list(Path(tmp.name).glob("*.json")), [],
                "错误信封不得写入缓存（TTL 内会持续命中错误数据）",
            )
        finally:
            await client.aclose()
            tmp.cleanup()


class SkinIndexFreshnessTest(unittest.TestCase):
    """审计 #24：皮肤索引新鲜度必须同时参考 skin_table。"""

    def test_skin_table_checked(self) -> None:
        import inspect

        from ArknightsUID.utils.gamedata import skins

        source = inspect.getsource(skins.load_skin_index)
        self.assertIn("SKIN_TABLE_PATH", source)


class SkinIdCompatTest(unittest.TestCase):
    """审计 #25：skins 条目的 skinId 键名兼容。"""

    def test_skinid_key_collected(self) -> None:
        from ArknightsUID.utils.player.parse import parse_player_info

        data = parse_player_info({
            "data": {
                "status": {"name": "D", "uid": "1"},
                "skins": [
                    {"id": "sk_a"}, {"skinId": "sk_b"}, "junk", {"id": ""},
                ],
            }
        })
        self.assertEqual(data.owned_skin_ids, ["sk_a", "sk_b"])


class CacheKeyUrlHashTest(unittest.IsolatedAsyncioTestCase):
    """审计 #20：不同 URL 同 basename 不再共享缓存（串图）。"""

    def test_names_distinct_per_url(self) -> None:
        from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url

        new_a, old_a = cache_names_for_url("https://a.cdn.com/img/foo.png")
        new_b, old_b = cache_names_for_url("https://b.cdn.com/x/foo.png")
        new_c, _ = cache_names_for_url("https://a.cdn.com/img/foo.png?sign=1")
        # 旧键相同（正是串图根源），新键必不同
        self.assertEqual(old_a, old_b == "foo.png" and old_a)
        self.assertNotEqual(new_a, new_b)
        self.assertNotEqual(new_a, new_c)
        # 同 URL 稳定
        self.assertEqual(new_a, cache_names_for_url("https://a.cdn.com/img/foo.png")[0])
        # percent-encoded 与无扩展名形态不炸
        self.assertTrue(cache_names_for_url("https://x/%E9%81%93.png")[0])
        self.assertTrue(cache_names_for_url("https://x/noext")[0])

    async def test_same_basename_urls_get_isolated_caches(self) -> None:
        """串图主场景：两张不同 URL 的 foo.png 各自缓存、互不覆盖。"""
        import ArknightsUID.utils.image as img_mod

        from PIL import Image as PILImage

        def _png(color: tuple) -> bytes:
            buf = BytesIO()
            PILImage.new("RGB", (3, 3), color).save(buf, "PNG")
            return buf.getvalue()

        downloads: dict[str, bytes] = {
            "https://a.cdn.com/img/foo.png": _png((255, 0, 0)),
            "https://b.cdn.com/x/foo.png": _png((0, 0, 255)),
        }

        async def fake_download(url, path, name, sess=None, tag=None, **kw):
            (Path(path) / name).write_bytes(downloads[url])
            return 200

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "gsuid_core.utils.download_resource.download_file.download",
            new=fake_download,
        ):
            img_a = await img_mod.pic_download_from_url(
                Path(tmp), "https://a.cdn.com/img/foo.png"
            )
            img_b = await img_mod.pic_download_from_url(
                Path(tmp), "https://b.cdn.com/x/foo.png"
            )
            files = sorted(p.name for p in Path(tmp).iterdir())

        # 旧逻辑：两者共用 foo.png/foo.webp，后写覆盖先写（img_a 被串成蓝）
        self.assertEqual(len([f for f in files if f.startswith("foo")]), 2,
                         f"应有两个独立缓存文件: {files}")
        self.assertEqual(img_a.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(img_b.getpixel((0, 0)), (0, 0, 255, 255))

    async def test_legacy_cache_file_still_hits_no_redownload(self) -> None:
        """双查兼容：存量旧键文件直接命中，不触发下载。"""
        import ArknightsUID.utils.image as img_mod

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "gsuid_core.utils.download_resource.download_file.download",
            new=mock.AsyncMock(),
        ) as m_dl:
            Path(tmp, "shot.webp").write_bytes(_png_bytes())  # 旧键存量文件
            img = await img_mod.pic_download_from_url(
                Path(tmp), "https://a.cdn.com/img/shot.png"
            )
        m_dl.assert_not_called()
        self.assertIsNotNone(img)

    async def test_render_b64_reads_legacy_file(self) -> None:
        """get_image_b64_with_cache 的双查：旧键文件可读出 data URL。"""
        import base64

        from ArknightsUID.utils.render import render_utils as ru

        import ArknightsUID.utils.path as ark_path

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ark_path, "BAKE_PATH", Path(tmp) / "bake"
        ):
            Path(tmp, "shot.webp").write_bytes(_png_bytes())
            b64 = await ru.get_image_b64_with_cache(
                "https://a.cdn.com/img/shot.png", Path(tmp)
            )
        self.assertTrue(b64.startswith("data:image/"))
        self.assertEqual(
            base64.b64decode(b64.split(",", 1)[1]), _png_bytes()
        )

    async def test_bulk_skips_when_lazy_new_key_exists(self) -> None:
        """bulk 双查：惰性路径写的新键文件存在 → skip 不重下。"""
        from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url
        from ArknightsUID.utils.resource import bulk_download as bd

        url = "https://a.cdn.com/img/shot.png"
        new_name, _ = cache_names_for_url(url)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, new_name).write_bytes(_png_bytes())
            target = bd.ResourceTarget(url, Path(tmp), "shot.png.miss")
            with mock.patch.object(
                bd, "download_bytes_status", new=mock.AsyncMock()
            ) as m_dl:
                result = await bd.download_target(target)
            self.assertEqual(result, "skip")
            m_dl.assert_not_awaited()


class LazyCacheSelfHealTest(unittest.IsolatedAsyncioTestCase):
    """存量坏缓存自愈：PIL 打不开 → 删除 → 重下一次（不再永久无图）。"""

    async def test_corrupt_cache_deleted_and_redownloaded(self) -> None:
        import ArknightsUID.utils.image as img_mod
        from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url

        calls = {"n": 0}

        async def fake_download(url, path, name, sess=None, tag=None, **kw):
            calls["n"] += 1
            (Path(path) / name).write_bytes(_png_bytes())
            return 200

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "gsuid_core.utils.download_resource.download_file.download",
            new=fake_download,
        ):
            # 在新键位置放一份坏缓存（与 cache_names_for_url 同规则）
            new_name, _ = cache_names_for_url("https://x/pic.png")
            bad = Path(tmp) / new_name
            bad.write_bytes(b"definitely not an image")
            img = await img_mod.pic_download_from_url(
                Path(tmp), "https://x/pic.png"
            )
        self.assertEqual(calls["n"], 1, "坏缓存应触发一次重下")
        self.assertFalse(bad.exists(), "坏文件应被删除")
        self.assertIsNotNone(img)

    async def test_fresh_download_failure_leaves_no_poison_file(self) -> None:
        """下载到网关错误页（200 的 HTML）：删除后抛出，不留毒药文件。"""
        import ArknightsUID.utils.image as img_mod

        async def fake_download(url, path, name, sess=None, tag=None, **kw):
            (Path(path) / name).write_bytes(b"<html>gateway error</html>")

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "gsuid_core.utils.download_resource.download_file.download",
            new=fake_download,
        ):
            with self.assertRaises(Exception):
                await img_mod.pic_download_from_url(Path(tmp), "https://x/err.png")
            leftovers = [
                p for p in Path(tmp).iterdir() if not p.name.endswith(".tmp")
            ]
            self.assertEqual(leftovers, [], f"毒药文件应被清理: {leftovers}")

    async def test_http_failure_raises_and_writes_nothing(self) -> None:
        """审计 #39：download 失败（非 200/None）显式抛错——旧版静默返回
        后 Image.open 才 FileNotFoundError；且最终路径上不留任何文件。"""
        import ArknightsUID.utils.image as img_mod

        async def fake_download(url, path, name, sess=None, tag=None, **kw):
            return 403  # 真实 download 失败时返回非 200 且不写文件

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "gsuid_core.utils.download_resource.download_file.download",
            new=fake_download,
        ):
            with self.assertRaises(FileNotFoundError):
                await img_mod.pic_download_from_url(Path(tmp), "https://x/deny.png")
            self.assertEqual(list(Path(tmp).iterdir()), [])


class BakeCacheIntegrityTest(unittest.IsolatedAsyncioTestCase):
    """烘焙缓存命中前过魔数：坏 bake 删除重烘，不再长期喂坏图。"""

    async def test_corrupt_bake_deleted_and_rebaked(self) -> None:
        import base64
        import hashlib

        from ArknightsUID.utils.render import render_utils as ru

        import ArknightsUID.utils.path as ark_path

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ark_path, "BAKE_PATH", Path(tmp) / "bake"
        ):
            # 源图就位（新键名），bake 位置放垃圾内容（mtime 更新成最新，
            # 保证命中"新鲜 bake"分支）
            url = "https://a.cdn.com/img/bake_me.png"
            from ArknightsUID.utils.gamedata.path_safety import (
                cache_names_for_url,
            )

            new_name, _ = cache_names_for_url(url)
            local_path = Path(tmp) / new_name
            local_path.write_bytes(_png_bytes())
            path_hash = hashlib.md5(
                str(local_path.resolve()).encode()
            ).hexdigest()[:8]
            bake_dir = Path(tmp) / "bake"
            bake_dir.mkdir()
            bake = bake_dir / f"{local_path.stem}_{path_hash}_q85.webp"
            bake.write_bytes(b"RIFFxxxxGARBAGE")  # 魔数伪造一半也不够
            import os
            import time as _t

            ts = _t.time() + 10
            os.utime(bake, (ts, ts))

            b64 = await ru.get_image_b64_with_cache(url, Path(tmp), quality=85)
            self.assertTrue(b64.startswith("data:image/webp;base64,"))
            data = base64.b64decode(b64.split(",", 1)[1])
            self.assertTrue(
                ru._looks_like_rendered_image(data),
                "重烘产物必须是合法图片魔数",
            )
            # 坏 bake 已被替换为真实 webp
            self.assertNotEqual(bake.read_bytes(), b"RIFFxxxxGARBAGE")

    async def test_valid_bake_still_served(self) -> None:
        """好 bake 照常命中（不误杀）：内容原样返回，未被重烘。"""
        import base64
        import hashlib
        import os
        import time as _t

        from ArknightsUID.utils.render import render_utils as ru

        import ArknightsUID.utils.path as ark_path

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ark_path, "BAKE_PATH", Path(tmp) / "bake"
        ):
            url = "https://a.cdn.com/img/good_bake.png"
            from ArknightsUID.utils.gamedata.path_safety import (
                cache_names_for_url,
            )

            new_name, _ = cache_names_for_url(url)
            local_path = Path(tmp) / new_name
            local_path.write_bytes(_png_bytes())
            path_hash = hashlib.md5(
                str(local_path.resolve()).encode()
            ).hexdigest()[:8]
            bake_dir = Path(tmp) / "bake"
            bake_dir.mkdir()
            bake = bake_dir / f"{local_path.stem}_{path_hash}_q85.webp"
            # 造一个真 webp 作为好 bake
            from PIL import Image

            Image.open(BytesIO(_png_bytes())).save(bake, "WEBP")
            good = bake.read_bytes()
            ts = _t.time() + 10
            os.utime(bake, (ts, ts))

            b64 = await ru.get_image_b64_with_cache(url, Path(tmp), quality=85)
            self.assertTrue(b64.startswith("data:image/webp;base64,"))
            # 命中即原样返回好 bake 的内容
            self.assertEqual(base64.b64decode(b64.split(",", 1)[1]), good)


class CoverCropAnchorTest(unittest.IsolatedAsyncioTestCase):
    """公告封面裁切锚点：竖长图取顶部切片（居中裁只见中段一条）。"""

    @staticmethod
    def _tall_png(width: int = 100, height: int = 400) -> bytes:
        """上/中/下三段纯色的竖长图。"""
        from PIL import Image

        img = Image.new("RGB", (width, height))
        img.paste((255, 0, 0), (0, 0, width, height // 3))          # 顶红
        img.paste((0, 255, 0), (0, height // 3, width, 2 * height // 3))
        img.paste((0, 0, 255), (0, 2 * height // 3, width, height))
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    async def test_top_anchor_crops_from_top(self) -> None:
        import base64

        from PIL import Image

        from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url
        from ArknightsUID.utils.render import render_utils as ru

        import ArknightsUID.utils.path as ark_path

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ark_path, "BAKE_PATH", Path(tmp) / "bake"
        ):
            (Path(tmp) / "bake").mkdir()
            url = "https://a.cdn.com/img/tall_cover.png"
            new_name, _ = cache_names_for_url(url)
            (Path(tmp) / new_name).write_bytes(self._tall_png())
            b64 = await ru.get_image_b64_with_cache(
                url, Path(tmp), quality=85, cover_size=(100, 50), anchor="top"
            )
            img = Image.open(BytesIO(base64.b64decode(b64.split(",", 1)[1])))
            self.assertEqual(img.size, (100, 50))
            r, g, b = img.getpixel((50, 25))
            self.assertGreater(r, 200, "顶部锚定应截到顶部红色段")
            self.assertLess(g, 80)

    async def test_center_anchor_keeps_default_and_bake_key_differs(self) -> None:
        import base64
        import hashlib

        from PIL import Image

        from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url
        from ArknightsUID.utils.render import render_utils as ru

        import ArknightsUID.utils.path as ark_path

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            ark_path, "BAKE_PATH", Path(tmp) / "bake"
        ):
            (Path(tmp) / "bake").mkdir()
            url = "https://a.cdn.com/img/tall_cover2.png"
            new_name, _ = cache_names_for_url(url)
            local_path = Path(tmp) / new_name
            local_path.write_bytes(self._tall_png())
            b64 = await ru.get_image_b64_with_cache(
                url, Path(tmp), quality=85, cover_size=(100, 50)
            )
            img = Image.open(BytesIO(base64.b64decode(b64.split(",", 1)[1])))
            r, g, b = img.getpixel((50, 25))
            self.assertGreater(g, 200, "默认居中应截到中部绿色段")
            # 同图再取顶部锚定：锚点不同 → 烘焙缓存键不同，不互相串图
            b64_top = await ru.get_image_b64_with_cache(
                url, Path(tmp), quality=85, cover_size=(100, 50), anchor="top"
            )
            self.assertNotEqual(b64, b64_top)
            path_hash = hashlib.md5(
                str(local_path.resolve()).encode()
            ).hexdigest()[:8]
            baked = list((Path(tmp) / "bake").glob(f"{local_path.stem}_*"))
            self.assertTrue(
                any("_top" in p.name for p in baked), [p.name for p in baked]
            )
            self.assertTrue(
                any(
                    p.name == f"{local_path.stem}_{path_hash}_q85_100x50.webp"
                    for p in baked
                ),
                [p.name for p in baked],
            )
