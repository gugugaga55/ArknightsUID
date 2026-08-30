"""公招标签截图 OCR：本地 RapidOCR 识别 + 标签全集容错匹配。

选型：不依赖 core 的 AI 图片理解（要求部署方配好视觉模型），也不走
远程 OCR 服务（ScoreEcho 模式：token/限流/出网依赖）。rapidocr 的
onnxruntime 后端纯本地 CPU 推理，模型随包自带（PP-OCRv6 det/rec
small，约 15MB）。真机 720p 公招截图实测：0.3s 出全部文本，
标签置信度 0.999+。

识别 ≠ 标签：OCR 返回整屏文字（资源数字、计时器、「职业需求」「招募
时限」等界面文案都在里面），必须对封闭标签全集匹配过滤：

  1. 精确命中 → 直接采用
  2. 碎片包含标签 → 取被包含的标签：屏上职业标签写作「先锋干员」而
     PRTS 职业字段是「先锋」，靠包含匹配归一；「高级资深干员」包含
     「资深干员」时长者优先、短者跳过（避免一行里双计）；两个标签被
     合并成一行时全部拆出
  3. 模糊匹配（OCR 错字「高级资深干贝」→「高级资深干员」）→ difflib
     相似度 ≥ 0.6 取最高分

不匹配任何标签的碎片按界面杂讯丢弃。匹配是纯函数，不装 rapidocr
也能测试；未安装时 ocr_available() 返回 False，命令层回退文字输入。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import difflib
import io
import re
import threading
from typing import Any, NamedTuple

from gsuid_core.logger import logger

# OCR 碎片置信度下限：真机标签 0.99+，0.5 只为容忍高压缩图；穿过来的
# 杂讯由全集匹配兜底过滤
_MIN_TEXT_SCORE = 0.5
# 模糊匹配相似度阈值；碎片至少 2 字才参与（单字对两字标签的相似度
# 天然可达 0.67，会误命中）
_FUZZY_CUTOFF = 0.6
_FUZZY_MIN_LEN = 2
# 参与包含匹配的碎片长度上限：标签药丸单行至多 ~12 字（3 个标签合并），
# 更长的是说明文案，不做包含以免误捞
_MAX_FRAGMENT_LEN = 16
# 单次最多处理的图片数（公招一张截图足够；引用回复可能带多图）
_MAX_IMAGES = 3
# 大图缩边上限：拍照上传可能 4000px+，缩到 2000 内推理明显加速
_MAX_SIDE = 2000
# 单图字节/像素上限（事件图片 URL/base64 均为用户可控输入：无界下载与
# 解压炸弹会直接吃内存；8MB/4000 万像素覆盖任何正常公招截图）
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_PIXELS = 40_000_000

_engine: Any = None
_engine_lock = threading.Lock()

# 裸 base64 判定（适配器可能直接塞 b64 字符串而不是 URL）
_B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")

# 常见图片格式魔数（下载/base64 内容必须命中其一，HTML/JSON 错误页拒绝）
_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",         # JPEG
    b"GIF87a", b"GIF89a",    # GIF
    b"BM",                   # BMP
)


def _looks_like_image(data: bytes) -> bool:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return any(data.startswith(sig) for sig in _IMAGE_SIGNATURES)


class TagOcrResult(NamedTuple):
    """extract_recruit_tags 结果。

    error 为空表示 OCR 流程成功（tags 可能为空 = 截图里没有公招标签）。
    """

    tags: list[str]        # 匹配到的标签（按出现顺序去重）
    raw_texts: list[str]   # OCR 全部文本（排查误识别用）
    error: str             # 非空 = 流程失败，内容为面向用户的中文提示


def ocr_available() -> bool:
    """rapidocr 是否已安装（可选依赖，未装时命令层回退文字输入）。"""
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


def _get_engine() -> Any:
    """RapidOCR 引擎单例（首次构造加载 3 个 onnx 模型约 1s，进程内复用）。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr import RapidOCR

                _engine = RapidOCR(params={"Global.log_level": "ERROR"})
    return _engine


def _ocr_bytes(data: bytes) -> list[tuple[str, float]]:
    """同步 OCR 一张图（CPU 推练，调用方需放线程）。返回 (文本, 置信度)。"""
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.width * img.height > _MAX_IMAGE_PIXELS:
        raise ValueError(f"image too large: {img.width}x{img.height}")
    if img.mode != "RGB":
        img = img.convert("RGB")
    if max(img.size) > _MAX_SIDE:
        img.thumbnail((_MAX_SIDE, _MAX_SIDE))
    result = _get_engine()(np.asarray(img))
    texts = result.txts or ()
    scores = result.scores or ()
    return [(str(t), float(s)) for t, s in zip(texts, scores)]


def match_tag_fragments(
    fragments: list[tuple[str, float]],
    universe: set[str] | frozenset[str],
) -> list[str]:
    """OCR 碎片 → 标签全集匹配（纯函数，未装 rapidocr 也可测）。

    按出现顺序返回去重后的标签；不匹配任何标签的碎片静默丢弃。
    """
    matched: list[str] = []
    seen: set[str] = set()
    # 排序保证 difflib.get_close_matches 平分时结果的确定性
    universe_list = sorted(universe)

    def _add(tag: str) -> None:
        if tag not in seen:
            seen.add(tag)
            matched.append(tag)

    for text, score in fragments:
        text = text.strip()
        if not text or score < _MIN_TEXT_SCORE:
            continue
        if text in universe:
            _add(text)
            continue
        if len(text) <= _MAX_FRAGMENT_LEN:
            contained = [t for t in universe_list if t in text]
            taken: list[str] = []
            for tag in sorted(contained, key=len, reverse=True):
                if any(tag in prev for prev in taken):
                    continue  # 已被更长标签覆盖（资深干员 ⊂ 高级资深干员）
                taken.append(tag)
            # 长度优先只为判定覆盖关系，入列按碎片内出现位置排
            for tag in sorted(taken, key=text.find):
                _add(tag)
            if taken:
                continue
        if len(text) >= _FUZZY_MIN_LEN:
            close = difflib.get_close_matches(
                text, universe_list, n=1, cutoff=_FUZZY_CUTOFF
            )
            if close:
                _add(close[0])
    return matched


async def _materialize(source: str) -> bytes | None:
    """图片来源 → 图片字节。支持 http(s) URL / base64:// / data:image /
    裸 base64 字符串；失败或超限返回 None。

    URL 是用户可控输入：走 safe_download_bytes（仅 http(s)、主机必须公网、
    逐跳重定向校验、流式 + 8MB 上限），不走通用 download_bytes。base64
    输入同样受 _MAX_IMAGE_BYTES 约束——无界解码直接吃内存。
    """
    from .http import SAFE_DOWNLOAD_TIMEOUT, safe_download_bytes

    source = source.strip()
    if source.startswith(("http://", "https://")):
        return await safe_download_bytes(
            source, max_bytes=_MAX_IMAGE_BYTES, timeout=SAFE_DOWNLOAD_TIMEOUT
        )
    if source.startswith("base64://"):
        b64 = source[len("base64://"):]
    elif source.startswith("data:image/"):
        _, _, b64 = source.partition(",")
    elif len(source) > 256 and _B64_RE.match(source):
        b64 = source
    else:
        return None
    b64 = re.sub(r"\s+", "", b64)
    # 解码前先按编码膨胀比粗筛，避免先解码出一个超大 bytes 再检查
    if len(b64) > (_MAX_IMAGE_BYTES // 3 + 1) * 4 + 4:
        return None
    try:
        data = base64.b64decode(b64)
    except (binascii.Error, ValueError):
        return None
    return data if len(data) <= _MAX_IMAGE_BYTES else None


async def extract_recruit_tags(
    image_sources: list[str],
    universe: set[str] | frozenset[str],
) -> TagOcrResult:
    """截图来源列表 → (匹配标签, 原始文本, 错误)。

    逐图下载识别、结果合并（多图时标签并集）；单图失败不影响其余，
    全部失败才返回错误。
    """
    if not ocr_available():
        return TagOcrResult(
            [], [],
            "截图识别需要 OCR 组件（可选依赖）：发送 ark安装依赖 ocr 安装"
            "（装完即时生效，无需重启）；或直接文字输入 ark公招推荐 标签…",
        )
    fragments: list[tuple[str, float]] = []
    for source in image_sources[:_MAX_IMAGES]:
        if not isinstance(source, str) or not source.strip():
            continue
        data = await _materialize(source)
        if not data:
            logger.warning(f"[ArknightsUID·公招OCR] 图片获取失败: {source[:80]}")
            continue
        try:
            fragments.extend(await asyncio.to_thread(_ocr_bytes, data))
        except Exception as e:  # noqa: BLE001 - 单图失败不拖垮整体
            logger.warning(f"[ArknightsUID·公招OCR] 识别失败: {e}")
    if not fragments:
        return TagOcrResult(
            [], [], "图片下载或识别失败，请稍后重试，或直接文字输入 ark公招推荐 标签…"
        )
    tags = match_tag_fragments(fragments, universe)
    return TagOcrResult(tags, [t for t, _ in fragments], "")
