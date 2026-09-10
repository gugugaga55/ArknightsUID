"""ark下载资源（bulk_download）回归测试：分类解析 / 清单枚举 / 落盘规则 / 汇总。

全部离线：下载与表拉取均 mock，不触发网络。
"""
from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ArknightsUID.utils.resource import bulk_download as bd


def _tiny_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(buf, "PNG")
    return buf.getvalue()


class ParseCategoryArgsTest(unittest.TestCase):
    def test_empty_or_all_means_everything(self) -> None:
        for text in ("", "   ", "全部", "全量", "all"):
            self.assertEqual(bd.parse_category_args(text), bd.ALL_CATEGORY_KEYS)

    def test_named_categories_keep_fixed_order(self) -> None:
        # 立绘写在前面也应按固定顺序（立绘最后）执行
        self.assertEqual(
            bd.parse_category_args("立绘 头像"), ["avatar", "portrait"]
        )
        self.assertEqual(bd.parse_category_args("敌人"), ["enemy"])

    def test_aliases_and_dedup(self) -> None:
        self.assertEqual(
            bd.parse_category_args("材料图标,物品、item"), ["item"]
        )
        self.assertEqual(bd.parse_category_args("肉鸽地图 / rogue_map"), ["rogue_map"])

    def test_unknown_token_returns_error_text(self) -> None:
        result = bd.parse_category_args("头像 乱写")
        self.assertIsInstance(result, str)
        self.assertIn("未知资源分类「乱写」", result)
        self.assertIn("立绘", result)  # 可用分类清单给出提示


class PortraitMissMarkerTest(unittest.TestCase):
    def test_base_form_marker_matches_lazy_naming(self) -> None:
        # 惰性路径 char_portrait_b64 读 {charId}%23{tier}.miss（无 .png）
        self.assertEqual(
            bd._portrait_miss_marker("char_002_amiya%233.png"),
            "char_002_amiya%233.miss",
        )

    def test_costume_form_no_marker(self) -> None:
        # 时装编码含 %40，惰性路径不读其标记
        self.assertEqual(
            bd._portrait_miss_marker("char_002_amiya%40winter%231.png"), ""
        )


class DownloadTargetTest(unittest.IsolatedAsyncioTestCase):
    async def test_skip_existing_webp_and_touch_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            webp = cache / "char_002_amiya%231.webp"
            webp.write_bytes(b"x")
            old = time.time() - 40 * 86400
            os.utime(webp, (old, old))
            target = bd.ResourceTarget(
                "https://web.hycdn.cn/x/char_002_amiya%231.png", cache
            )
            with mock.patch.object(
                bd,
                "download_bytes_status",
                mock.AsyncMock(side_effect=AssertionError("不应下载")),
            ):
                status = await bd.download_target(target)
            self.assertEqual(status, "skip")
            self.assertGreater(webp.stat().st_mtime, old)

    async def test_download_ok_converts_to_webp(self) -> None:
        png = _tiny_png()
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            target = bd.ResourceTarget("https://web.hycdn.cn/x/skchr_ayer_2.png", cache)
            with mock.patch.object(
                bd,
                "download_bytes_status",
                mock.AsyncMock(return_value=(200, png)),
            ):
                status = await bd.download_target(target)
            self.assertEqual(status, "ok")
            webp = cache / "skchr_ayer_2.webp"
            self.assertTrue(webp.exists())
            self.assertFalse((cache / "skchr_ayer_2.png").exists())

    async def test_404_writes_miss_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            target = bd.ResourceTarget(
                "https://media.prts.wiki/2/2e/%E9%81%93%E5%85%B7.png",
                cache,
                "%E9%81%93%E5%85%B7.png.miss",
            )
            with mock.patch.object(
                bd,
                "download_bytes_status",
                mock.AsyncMock(return_value=(404, None)),
            ):
                status = await bd.download_target(target)
            self.assertEqual(status, "miss")
            self.assertTrue((cache / "%E9%81%93%E5%85%B7.png.miss").exists())

    async def test_404_without_marker_only_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            target = bd.ResourceTarget("https://web.hycdn.cn/x/a.png", cache)
            with mock.patch.object(
                bd,
                "download_bytes_status",
                mock.AsyncMock(return_value=(404, None)),
            ):
                status = await bd.download_target(target)
            self.assertEqual(status, "miss")
            self.assertEqual(list(cache.iterdir()), [])

    async def test_network_failure_no_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            target = bd.ResourceTarget(
                "https://web.hycdn.cn/x/a.png", cache, "a.png.miss"
            )
            with mock.patch.object(
                bd,
                "download_bytes_status",
                mock.AsyncMock(return_value=(0, None)),
            ):
                status = await bd.download_target(target)
            self.assertEqual(status, "fail")
            self.assertEqual(list(cache.iterdir()), [])


class TargetBuildersTest(unittest.IsolatedAsyncioTestCase):
    def _patch_skin_source(self, char_skins: dict, char_index: dict):
        import ArknightsUID.utils.gamedata._table_cache as table_cache
        import ArknightsUID.utils.gamedata.charinfo as charinfo

        return (
            mock.patch.object(
                table_cache, "ensure_skin_table", mock.AsyncMock(return_value={"charSkins": char_skins})
            ),
            mock.patch.object(
                charinfo, "load_char_index", mock.AsyncMock(return_value=char_index)
            ),
        )

    async def test_avatar_targets_url_parity_and_double_dirs(self) -> None:
        from ArknightsUID.utils.player.context import skin_avatar_url

        skins = {
            "char_002_amiya#1": {"charId": "char_002_amiya"},
            "char_002_amiya@winter#1": {"charId": "char_002_amiya"},
            "trap_124_eradio#1": {"charId": "trap_124_eradio"},  # 装置：不可获得
        }
        index = {"char_002_amiya": {"name": "阿米娅"}}
        p1, p2 = self._patch_skin_source(skins, index)
        with p1, p2:
            targets = await bd.build_avatar_targets()
        urls = {t.url for t in targets}
        # 与惰性路径同一 URL 构造函数 → 文件名一致，运行时直接命中缓存
        self.assertIn(skin_avatar_url("char_002_amiya#1"), urls)
        self.assertIn(skin_avatar_url("char_002_amiya@winter#1"), urls)
        self.assertNotIn(skin_avatar_url("trap_124_eradio#1"), urls)
        dirs = {str(t.cache_dir) for t in targets}
        self.assertEqual(len(dirs), 2)  # cache/avatar + bake/skin_avatars

    async def test_portrait_targets_marker_rules(self) -> None:
        skins = {
            "char_002_amiya#3": {"charId": "char_002_amiya"},
            "char_002_amiya@winter#1": {"charId": "char_002_amiya"},
        }
        index = {"char_002_amiya": {"name": "阿米娅"}}
        p1, p2 = self._patch_skin_source(skins, index)
        with p1, p2:
            targets = await bd.build_portrait_targets()
        by_url = {t.url: t for t in targets}
        base = next(u for u in by_url if "%40" not in u)
        costume = next(u for u in by_url if "%40" in u)
        self.assertTrue(by_url[base].miss_marker.endswith("%233.miss"))
        self.assertEqual(by_url[costume].miss_marker, "")

    async def test_skill_targets_from_skill_table(self) -> None:
        import ArknightsUID.utils.gamedata._table_cache as table_cache

        with mock.patch.object(
            table_cache,
            "ensure_skill_table",
            mock.AsyncMock(return_value={"skchr_ayer_2": {"levels": []}}),
        ):
            targets = await bd.build_skill_targets()
        self.assertEqual(len(targets), 1)
        self.assertIn("skchr_ayer_2.png", targets[0].url.rsplit("/", 1)[-1])

    async def test_equip_targets_from_uniequip(self) -> None:
        import ArknightsUID.utils.gamedata._table_cache as table_cache

        with mock.patch.object(
            table_cache,
            "ensure_uniequip_table",
            mock.AsyncMock(
                return_value={"equipDict": {"uniequip_002_amiya2": {}}}
            ),
        ):
            targets = await bd.build_equip_targets()
        self.assertEqual(len(targets), 1)
        self.assertIn("uniequip_img", targets[0].url)

    async def test_item_targets_from_allimages(self) -> None:
        import ArknightsUID.utils.prts.client as prts_client

        titles = ["道具_带框_固源岩.png", "道具_带框_聚酸酯.png"]
        with mock.patch.object(
            prts_client, "all_image_titles", mock.AsyncMock(return_value=titles)
        ):
            targets = await bd.build_item_targets()
        self.assertEqual(len(targets), 2)
        # md5 直链 + 与 itemicon 相同的 .miss 标记命名（{url_last}.miss）
        for t in targets:
            self.assertTrue(t.url.startswith("https://media.prts.wiki/"))
            self.assertEqual(t.miss_marker, t.url.rsplit("/", 1)[-1] + ".miss")

    async def test_enemy_targets_from_allimages(self) -> None:
        """回归：build_enemy_targets 曾漏 import safe_url_basename，
        敌人头像分类整类枚举失败（name 'safe_url_basename' is not defined）。"""
        import ArknightsUID.utils.prts.client as prts_client

        titles = ["头像_敌人_源石虫.png"]
        with mock.patch.object(
            prts_client, "all_image_titles", mock.AsyncMock(return_value=titles)
        ):
            targets = await bd.build_enemy_targets()
        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0].url.startswith("https://media.prts.wiki/"))
        self.assertEqual(
            targets[0].miss_marker, targets[0].url.rsplit("/", 1)[-1] + ".miss"
        )

    async def test_profession_targets_cover_eight(self) -> None:
        targets = await bd.build_profession_targets()
        self.assertEqual(len(targets), 8)
        # 图标_职业_{名}.png 的 md5 直链（与 prts_profession_icon_url 同源）
        for t in targets:
            self.assertIn("%E5%9B%BE%E6%A0%87_%E8%81%8C%E4%B8%9A_", t.url)
            self.assertEqual(t.miss_marker, t.url.rsplit("/", 1)[-1] + ".miss")

    async def test_map_targets_only_mainline_codes(self) -> None:
        import ArknightsUID.utils.gamedata._table_cache as table_cache

        stages = {
            "main_00-01": {"code": "0-1", "name": "邪恶摇笼"},
            "tr_10": {"code": "TR-10", "name": "小队防御"},
            "act_1": {"code": "CW-1", "name": "活动关"},  # 活动代号无地图命名
        }
        with mock.patch.object(
            table_cache,
            "ensure_stage_table",
            mock.AsyncMock(return_value={"stages": stages}),
        ):
            targets = await bd.build_map_targets()
        url_lasts = {t.url.rsplit("/", 1)[-1] for t in targets}
        self.assertIn("0-1_%E9%82%AA%E6%81%B6%E6%91%87%E7%AC%BC_%E5%9C%B0%E5%9B%BE.png", url_lasts)
        self.assertIn("0-1_%E9%82%AA%E6%81%B6%E6%91%87%E7%AC%BC_%E8%BE%85%E5%8A%A9%E5%9C%B0%E5%9B%BE.png", url_lasts)
        self.assertTrue(any(t.url.startswith("https://media.prts.wiki/") for t in targets))
        self.assertFalse(any("CW" in u for u in url_lasts))
        # 标记命名与 wiki 模块 _url_miss 一致（{url_last}.miss）
        for t in targets:
            self.assertEqual(t.miss_marker, t.url.rsplit("/", 1)[-1] + ".miss")


class RunBulkDownloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_stats_and_progress_callback(self) -> None:
        targets = [
            bd.ResourceTarget(f"https://x/{i}.png", Path("/tmp/ark-test"))
            for i in range(4)
        ]

        async def fake_builder():
            return targets

        statuses = iter(["ok", "skip", "miss", "fail"])

        async def fake_download(target, verify=False):
            return next(statuses)

        reported: list[bd.CategoryResult] = []

        async def on_done(result):
            reported.append(result)

        with mock.patch.dict(
            bd.CATEGORY_BUILDERS, {"avatar": fake_builder}
        ), mock.patch.object(bd, "download_target", fake_download):
            summary = await bd.run_bulk_download(["avatar"], on_category_done=on_done)

        self.assertEqual(len(summary.results), 1)
        result = summary.results[0]
        self.assertEqual(result.total, 4)
        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.missing, 1)
        self.assertEqual(result.failed, 1)
        self.assertFalse(result.error)
        self.assertEqual(reported, [result])
        text = bd.format_summary(summary)
        self.assertIn("新增 1 张", text)

    async def test_enumeration_error_does_not_block_others(self) -> None:
        async def bad_builder():
            raise RuntimeError("skill_table 拉取失败")

        async def good_builder():
            return [bd.ResourceTarget("https://x/a.png", Path("/tmp/ark-test"))]

        async def fake_download(target, verify=False):
            return "ok"

        with mock.patch.dict(
            bd.CATEGORY_BUILDERS,
            {"skill": bad_builder, "avatar": good_builder},
        ), mock.patch.object(bd, "download_target", fake_download):
            summary = await bd.run_bulk_download(["skill", "avatar"])

        self.assertIn("清单枚举失败", summary.results[0].error)
        self.assertEqual(summary.results[1].downloaded, 1)
        text = bd.format_summary(summary)
        self.assertIn("技能图标", text)


class AllImageTitlesTest(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_continue(self) -> None:
        import ArknightsUID.utils.prts.client as prts_client

        pages = [
            {
                "query": {"allimages": [
                    {"name": "头像_敌人_A.png", "url": "https://m/1"},
                    {"name": "头像_敌人_B.png", "url": "https://m/2"},
                ]},
                "continue": {"aicontinue": "头像_敌人_C.png", "continue": "-||"},
            },
            {
                "query": {"allimages": [
                    {"name": "头像_敌人_C.png", "url": "https://m/3"},
                ]},
            },
        ]

        async def fake_api(params, **kwargs):
            self.assertEqual(params["list"], "allimages")
            self.assertEqual(params["aiprefix"], "头像_敌人_")
            return pages.pop(0)

        with mock.patch.object(prts_client, "_api", fake_api):
            titles = await prts_client.all_image_titles("头像_敌人_")

        self.assertEqual(
            titles, ["头像_敌人_A.png", "头像_敌人_B.png", "头像_敌人_C.png"]
        )


class RunFlagTest(unittest.TestCase):
    def test_acquire_release(self) -> None:
        self.assertTrue(bd.try_acquire_run())
        self.assertFalse(bd.try_acquire_run())
        bd.release_run()
        self.assertTrue(bd.try_acquire_run())
        bd.release_run()


class CommandSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_command_sends_forward_summary_not_per_category(self) -> None:
        """进度不逐类刷屏：分类行 + 总结合并为一条转发消息发回。"""
        import ArknightsUID.arknightsuid_maintenance as maintenance

        sent: list = []

        class FakeBot:
            async def send(self, msg, **kw):
                sent.append(msg)

        ev = type("Ev", (), {"text": "头像", "group_id": ""})()

        summary = bd.BulkSummary(
            results=[bd.CategoryResult("avatar", "干员头像", total=1, downloaded=1)],
            started_at=time.time(),
            finished_at=time.time(),
        )

        with mock.patch.object(
            bd, "run_bulk_download", mock.AsyncMock(return_value=summary)
        ) as m_run, mock.patch.object(bd, "try_acquire_run", return_value=True), \
            mock.patch.object(bd, "release_run") as m_release:
            await maintenance.bulk_download_resources(FakeBot(), ev)

        m_run.assert_awaited_once()
        self.assertEqual([k for k in m_run.await_args.args[0]], ["avatar"])
        self.assertTrue(any("开始预下载" in str(s) for s in sent))
        # 结束后只有一条转发消息：node 段，内含分类行与总结
        nodes = [s for s in sent if getattr(s, "type", None) == "node"]
        self.assertEqual(len(nodes), 1)
        node_texts = [
            str(m.data) for m in nodes[0].data if m.type == "text"
        ]
        self.assertTrue(any("干员头像" in t for t in node_texts))
        self.assertTrue(any("资源下载完成" in t for t in node_texts))
        m_release.assert_called_once()

    async def test_command_busy_message(self) -> None:
        import ArknightsUID.arknightsuid_maintenance as maintenance

        sent: list[str] = []

        class FakeBot:
            async def send(self, msg, **kw):
                sent.append(str(msg))

        ev = type("Ev", (), {"text": "", "group_id": ""})()
        with mock.patch.object(bd, "try_acquire_run", return_value=False), \
             mock.patch.object(bd, "run_bulk_download", mock.AsyncMock()) as m_run:
            await maintenance.bulk_download_resources(FakeBot(), ev)

        m_run.assert_not_awaited()
        self.assertTrue(any("正在进行中" in s for s in sent))


class DownloadTargetQuiet404Test(unittest.IsolatedAsyncioTestCase):
    """批量预下载的 404 是预期缺失（PRTS 未上传的关卡地图 / torappu 未覆盖
    的肉鸽预览等），除计入 missing 外不应再刷 http 层 warning。"""

    async def test_download_target_quiet_on_404(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = bd.ResourceTarget("https://media.prts.wiki/1/1a/x.png", Path(td))
            dl = mock.AsyncMock(return_value=(404, None))
            with mock.patch.object(bd, "download_bytes_status", dl):
                status = await bd.download_target(target)
            self.assertEqual(status, "miss")
            self.assertTrue(dl.call_args.kwargs.get("quiet_404"))


class SkinKeyPercentEncodingTest(unittest.TestCase):
    """含 + 的形态键（char_002_amiya#1+，skin_table 真实键）必须完整
    百分号编码：hycdn 实测 %2B 200 / 裸 + 404（2026-09）。常规键编码
    输出与旧手工替换一致，存量缓存名不受影响。"""

    def test_plus_form_fully_encoded(self) -> None:
        from ArknightsUID.utils.player.context import (
            skin_avatar_url,
            skin_portrait_url,
        )

        self.assertTrue(
            skin_portrait_url("char_002_amiya#1+").endswith(
                "char_002_amiya%231%2B.png"
            )
        )
        self.assertTrue(
            skin_avatar_url("char_002_amiya#1+").endswith(
                "char_002_amiya%231%2B.png"
            )
        )

    def test_regular_keys_encoding_unchanged(self) -> None:
        from ArknightsUID.utils.player.context import skin_portrait_url

        self.assertTrue(
            skin_portrait_url("char_002_amiya#1").endswith("char_002_amiya%231.png")
        )
        self.assertTrue(
            skin_portrait_url("char_002_amiya@winter#1").endswith(
                "char_002_amiya%40winter%231.png"
            )
        )


if __name__ == "__main__":
    unittest.main()


class ParseVerifyFlagTest(unittest.TestCase):
    """「校验」修饰词：不占分类位、可单独用、与分类混用；list 契约不变。"""

    def test_verify_only_means_all_categories(self) -> None:
        plan = bd.parse_category_args("校验")
        self.assertEqual(plan, bd.ALL_CATEGORY_KEYS)  # list 相等契约
        self.assertTrue(plan.verify)

    def test_verify_with_categories(self) -> None:
        plan = bd.parse_category_args("立绘 校验")
        self.assertEqual(plan, ["portrait"])
        self.assertTrue(plan.verify)
        plan2 = bd.parse_category_args("check 敌人")
        self.assertEqual(plan2, ["enemy"])
        self.assertTrue(plan2.verify)

    def test_default_no_verify(self) -> None:
        self.assertFalse(bd.parse_category_args("立绘").verify)
        self.assertFalse(bd.parse_category_args("").verify)


class DownloadTargetIntegrityTest(unittest.IsolatedAsyncioTestCase):
    """存量完整性：0 字节残档一律重下；校验模式 PIL 验坏档重下。"""

    async def test_zero_byte_existing_redownloads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / "a.png").write_bytes(b"")  # 0 字节残档
            target = bd.ResourceTarget("https://web.hycdn.cn/x/a.png", cache)
            with mock.patch.object(
                bd, "download_bytes_status",
                mock.AsyncMock(return_value=(200, _tiny_png())),
            ) as dl:
                status = await bd.download_target(target)
            self.assertEqual(status, "ok")
            self.assertEqual(dl.await_count, 1, "0 字节残档应触发重下")
            self.assertTrue((cache / "a.webp").exists())
            self.assertFalse((cache / "a.png").exists())

    async def test_corrupt_file_skips_without_verify(self) -> None:
        """默认模式只看存在性（快）：非空坏档仍 skip——校验是显式开关。"""
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / "a.png").write_bytes(b"not an image at all")
            target = bd.ResourceTarget("https://web.hycdn.cn/x/a.png", cache)
            with mock.patch.object(
                bd, "download_bytes_status",
                mock.AsyncMock(side_effect=AssertionError("不应下载")),
            ):
                status = await bd.download_target(target)
            self.assertEqual(status, "skip")

    async def test_corrupt_file_redownloads_in_verify_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            bad = cache / "a.png"
            bad.write_bytes(b"not an image at all")
            target = bd.ResourceTarget("https://web.hycdn.cn/x/a.png", cache)
            with mock.patch.object(
                bd, "download_bytes_status",
                mock.AsyncMock(return_value=(200, _tiny_png())),
            ):
                status = await bd.download_target(target, verify=True)
            self.assertEqual(status, "ok")
            self.assertFalse(bad.exists(), "坏档应被删除")
            self.assertTrue((cache / "a.webp").exists())

    async def test_valid_file_passes_verify_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / "a.png").write_bytes(_tiny_png())
            target = bd.ResourceTarget("https://web.hycdn.cn/x/a.png", cache)
            with mock.patch.object(
                bd, "download_bytes_status",
                mock.AsyncMock(side_effect=AssertionError("不应下载")),
            ):
                status = await bd.download_target(target, verify=True)
            self.assertEqual(status, "skip")
