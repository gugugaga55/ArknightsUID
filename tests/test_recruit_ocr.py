"""公招标签 OCR 测试：全集容错匹配（纯函数）+ 图片来源解析 + 端到端识别。

REAL_SCREEN_OCR 是真机 720p 公招截图的 RapidOCR 原始输出（含资源数字、
计时器、「职业需求」「招募时限」等全部界面杂讯），作为匹配过滤的回归
基准：只允许捞出 5 个真标签。
"""
from __future__ import annotations

import asyncio
import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.recruit_ocr import (
    TagOcrResult,
    _materialize,
    extract_recruit_tags,
    match_tag_fragments,
    ocr_available,
)

# 真机公招标签全集子集（PRTS 口径：职业为裸名「先锋」，屏上显示
# 「先锋干员」靠包含匹配归一）
UNIVERSE = {
    "先锋", "医疗", "术师", "近卫", "重装", "辅助", "狙击", "特种",
    "近战位", "远程位", "治疗", "支援", "输出", "生存", "防护", "群攻",
    "位移", "控场", "爆发", "召唤", "减速", "削弱", "费用回复",
    "快速复活", "支援机械", "新手", "元素",
    "资深干员", "高级资深干员",
}

# 真机 720p 截图的 RapidOCR 原始输出（顺序与置信度照录）
REAL_SCREEN_OCR: list[tuple[str, float]] = [
    ("4812082", 1.0), ("532", 1.0), ("15300", 1.0), ("30075", 1.0),
    ("联络次数0", 0.999), ("04:45:28 联络中", 0.995), ("招募说明", 1.0),
    ("·可获得的干员", 0.993), ("09:00:00", 0.973), ("3★", 0.998),
    ("4★", 0.998), ("5★", 0.999), ("招募时限", 1.0), ("先锋干员", 0.999),
    ("远程位", 1.0), ("高级资深干员", 0.999), ("职业需求", 1.0),
    ("(最多3项)", 1.0), ("治疗", 1.0), ("支援", 1.0),
    ("人脉资源联络中", 1.0), ("招募预算", 0.999), ("532/1", 1.0),
]
REAL_SCREEN_TAGS = ["先锋", "远程位", "高级资深干员", "治疗", "支援"]


class MatchTagFragmentsTest(unittest.TestCase):
    def test_real_screen_noise_filtered(self) -> None:
        """真机截图：杂讯全部丢弃，恰好捞出 5 个真标签（按出现顺序）。"""
        self.assertEqual(
            match_tag_fragments(REAL_SCREEN_OCR, UNIVERSE), REAL_SCREEN_TAGS
        )

    def test_exact_match(self) -> None:
        self.assertEqual(
            match_tag_fragments([("治疗", 1.0)], UNIVERSE), ["治疗"]
        )

    def test_profession_suffix_normalized(self) -> None:
        """屏上「先锋干员」→ PRTS 口径「先锋」。"""
        self.assertEqual(
            match_tag_fragments([("狙击干员", 1.0)], UNIVERSE), ["狙击"]
        )

    def test_containment_prefers_longest(self) -> None:
        """「高级资深干员招募中」只取高资，不双计资深。"""
        self.assertEqual(
            match_tag_fragments([("高级资深干员招募中", 1.0)], UNIVERSE),
            ["高级资深干员"],
        )

    def test_merged_pills_split(self) -> None:
        """两个标签被 OCR 合并成一行时全部拆出。"""
        self.assertEqual(
            match_tag_fragments([("术师输出", 1.0)], UNIVERSE), ["术师", "输出"]
        )

    def test_fuzzy_ocr_typo(self) -> None:
        self.assertEqual(
            match_tag_fragments([("高级资深干贝", 1.0)], UNIVERSE),
            ["高级资深干员"],
        )

    def test_single_char_no_fuzzy(self) -> None:
        """单字碎片不做模糊匹配（对两字标签相似度天然 0.67 会误命中）。"""
        self.assertEqual(match_tag_fragments([("员", 1.0)], UNIVERSE), [])

    def test_low_score_dropped(self) -> None:
        self.assertEqual(match_tag_fragments([("治疗", 0.3)], UNIVERSE), [])

    def test_dedup_preserves_order(self) -> None:
        self.assertEqual(
            match_tag_fragments(
                [("治疗", 1.0), ("支援", 1.0), ("治疗", 1.0)], UNIVERSE
            ),
            ["治疗", "支援"],
        )

    def test_merged_row_extracted(self) -> None:
        """低分辨率下一整行标签被合并成一个碎片时全部拆出——
        这也是包含匹配长度上限不能收太紧的原因。"""
        self.assertEqual(
            match_tag_fragments(
                [("先锋干员远程位高级资深干员", 1.0)], UNIVERSE
            ),
            ["先锋", "远程位", "高级资深干员"],
        )

    def test_overlong_fragment_ignored(self) -> None:
        """超过长度上限的说明文案不做包含匹配（避免误捞两字标签）。"""
        self.assertEqual(
            match_tag_fragments(
                [("招募说明文本可能很长很长长于标签行的上限", 1.0)], UNIVERSE
            ),
            [],
        )


class MaterializeTest(unittest.TestCase):
    def test_data_uri(self) -> None:
        b64 = base64.b64encode(b"hello").decode()
        self.assertEqual(
            asyncio.run(_materialize(f"data:image/png;base64,{b64}")), b"hello"
        )

    def test_base64_prefix(self) -> None:
        b64 = base64.b64encode(b"hello").decode()
        self.assertEqual(
            asyncio.run(_materialize(f"base64://{b64}")), b"hello"
        )

    def test_bare_base64(self) -> None:
        payload = bytes(range(256)) * 2  # >256 字符的裸 base64
        b64 = base64.b64encode(payload).decode()
        self.assertEqual(asyncio.run(_materialize(b64)), payload)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(asyncio.run(_materialize("not-a-source")))

    def test_http_url_dispatch(self) -> None:
        # 事件图片 URL 是用户可控输入：http 分支必须走受限的安全下载
        with mock.patch(
            "ArknightsUID.utils.http.safe_download_bytes",
            new=mock.AsyncMock(return_value=b"imgbytes"),
        ) as m_safe, mock.patch(
            "ArknightsUID.utils.http.download_bytes"
        ) as m_unsafe:
            self.assertEqual(
                asyncio.run(_materialize("https://example.com/a.jpg")),
                b"imgbytes",
            )
        m_safe.assert_awaited()
        m_unsafe.assert_not_called()


class ExtractRecruitTagsTest(unittest.TestCase):
    def test_ocr_unavailable_error(self) -> None:
        with mock.patch(
            "ArknightsUID.utils.recruit_ocr.ocr_available", return_value=False
        ):
            result = asyncio.run(extract_recruit_tags(["https://x/1.jpg"], UNIVERSE))
        self.assertTrue(result.error)
        self.assertEqual(result.tags, [])

    def test_no_tags_found_is_success_with_empty(self) -> None:
        """OCR 成功但没标签 → error 为空、tags 为空（调用方给用户提示）。"""
        b64 = base64.b64encode(b"x").decode()
        def fake_ocr(data: bytes):
            return [("招募说明", 1.0), ("09:00:00", 0.97)]

        with mock.patch(
            "ArknightsUID.utils.recruit_ocr.ocr_available", return_value=True
        ), mock.patch(
            "ArknightsUID.utils.recruit_ocr._ocr_bytes", new=fake_ocr
        ):
            result = asyncio.run(
                extract_recruit_tags([f"data:image/png;base64,{b64}"], UNIVERSE)
            )
        self.assertEqual(result.error, "")
        self.assertEqual(result.tags, [])
        self.assertEqual(result.raw_texts, ["招募说明", "09:00:00"])

    def test_all_images_failed(self) -> None:
        async def fail_download(url: str):
            return None

        with mock.patch(
            "ArknightsUID.utils.recruit_ocr.ocr_available", return_value=True
        ), mock.patch(
            "ArknightsUID.utils.http.download_bytes", new=fail_download
        ):
            result = asyncio.run(
                extract_recruit_tags(["https://x/1.jpg"], UNIVERSE)
            )
        self.assertTrue(result.error)


class EndToEndOcrTest(unittest.TestCase):
    """合成公招标签图 → 端到端识别（需 rapidocr + PIL + CJK 字体）。"""

    def setUp(self) -> None:
        if not ocr_available():
            self.skipTest("rapidocr 未安装")
        try:
            from PIL import ImageFont  # noqa: F401
        except ImportError:
            self.skipTest("PIL 未安装")
        self.font_path = next(
            (
                p for p in (
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                )
                if Path(p).exists()
            ),
            None,
        )
        if self.font_path is None:
            self.skipTest("无 CJK 字体")

    def test_synthetic_recruit_screen(self) -> None:
        import base64 as b64mod
        from io import BytesIO

        from PIL import Image, ImageDraw, ImageFont

        tags = ["高级资深干员", "术师", "输出", "远程位", "生存"]
        img = Image.new("RGB", (900, 320), (24, 28, 36))
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(self.font_path, 30)
        pos = [(60, 60), (360, 60), (660, 60), (60, 200), (360, 200)]
        for (x, y), text in zip(pos, tags):
            draw.rounded_rectangle(
                [x, y, x + 230, y + 80], 10, fill=(212, 203, 172)
            )
            draw.text((x + 20, y + 18), text, font=font, fill=(40, 44, 52))
        buf = BytesIO()
        img.save(buf, format="PNG")
        source = f"data:image/png;base64,{b64mod.b64encode(buf.getvalue()).decode()}"

        result = asyncio.run(extract_recruit_tags([source], UNIVERSE))
        self.assertEqual(result.error, "")
        self.assertEqual(result.tags, tags)


class RecruitRecommendHandlerTest(unittest.IsolatedAsyncioTestCase):
    """公招推荐命令的截图分支接线（OCR 识别本身由前述用例覆盖）。"""

    DATA = None  # 类级惰性：entries_from_smw 合成数据

    @classmethod
    def _load_data(cls):
        if cls.DATA is None:
            from ArknightsUID.utils.gamedata.recruit import entries_from_smw

            cls.DATA = entries_from_smw(
                {
                    "六星术师": {"printouts": {
                        "稀有度": ["5"], "职业": ["术师"], "位置": ["远程位"],
                        "标签": ["输出"]}},
                    "五星术师": {"printouts": {
                        "稀有度": ["4"], "职业": ["术师"], "位置": ["远程位"],
                        "标签": ["输出"]}},
                }
            )
        return cls.DATA

    @staticmethod
    def _fake_event(text: str = "", image: str | None = None):
        class _Ev:
            pass

        ev = _Ev()
        ev.text = text
        ev.image = image
        ev.image_list = [image] if image else []
        ev.group_id = None
        ev.user_id = "u1"
        return ev

    async def _run(self, ev, ocr_result=None, render_raises=False):
        import ArknightsUID.arknightsuid_wiki as wiki_mod

        class _Bot:
            def __init__(self):
                self.sent = []

            async def send(self, message, **kwargs):
                self.sent.append(message)

        captured: dict = {}

        async def fake_render(bot, ev, template, context, text):
            captured.update(template=template, context=context, text=text)

        async def fake_extract(sources, universe):
            assert isinstance(sources, list) and sources
            assert ocr_result is not None
            return ocr_result

        bot = _Bot()
        patches = [
            mock.patch.object(
                wiki_mod, "load_recruit_data", return_value=self._load_data()
            ),
            mock.patch.object(wiki_mod, "extract_recruit_tags", fake_extract),
            mock.patch.object(wiki_mod, "_render_or_text", fake_render),
        ]
        with patches[0], patches[1], patches[2]:
            await wiki_mod.send_recruit_recommend(bot, ev)
        return bot, captured

    async def test_screenshot_branch_labeled(self) -> None:
        """截图无文字 → OCR 分支，渲染上下文与文本头都带「截图识别」。"""
        bot, captured = await self._run(
            self._fake_event(image="data:image/png;base64,aGVsbG8="),
            ocr_result=TagOcrResult(["高级资深干员", "术师"], ["高级资深干员"], ""),
        )
        self.assertEqual(captured["template"], "ark_recruit_plan.html")
        self.assertEqual(
            captured["context"]["input_tags"], "高级资深干员 / 术师（截图识别）"
        )
        self.assertIn("（截图识别：高级资深干员 / 术师）", captured["text"])
        self.assertIn("📸", captured["text"])

    async def test_screenshot_no_tags_message(self) -> None:
        """OCR 成功但无标签 → 提示读到的文字 + 文字输入用法。"""
        bot, captured = await self._run(
            self._fake_event(image="data:image/png;base64,aGVsbG8="),
            ocr_result=TagOcrResult([], ["招募说明", "09:00:00"], ""),
        )
        self.assertFalse(captured)
        self.assertTrue(bot.sent)
        msg = bot.sent[0]
        self.assertIn("没识别到公招标签", msg)
        self.assertIn("招募说明", msg)

    async def test_screenshot_ocr_error_propagated(self) -> None:
        bot, captured = await self._run(
            self._fake_event(image="data:image/png;base64,aGVsbG8="),
            ocr_result=TagOcrResult([], [], "OCR 组件未安装"),
        )
        self.assertFalse(captured)
        self.assertIn("OCR 组件未安装", bot.sent[0])

    async def test_text_takes_precedence_over_image(self) -> None:
        """带文字参数时忽略截图、不走 OCR。"""
        bot, captured = await self._run(
            self._fake_event(
                text="高级资深干员 术师", image="data:image/png;base64,aGVsbG8="
            ),
            ocr_result=None,  # fake_extract 断言 sources 非空前会先被调用断言失败
        )
        self.assertEqual(
            captured["context"]["input_tags"], "高级资深干员 / 术师"
        )
        self.assertNotIn("📸", captured["text"])


if __name__ == "__main__":
    unittest.main()
