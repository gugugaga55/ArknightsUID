"""颜文字符号字体包：公告正文 kaomoji 字符的渲染兜底（下载与供给）。

为什么需要：森空岛官方公告正文常含颜文字（实帖样本 ``꒰˶>ᗜ<˶꒱``、
``ദ്ദി˶>𖥦<)``），其字符分布在 Yi 音节（꒰꒱）、加拿大原住民音节
（ᗜᕕᐛᕗᐢ）、改造上标字母（˶ᴗᵕˬ）、马来亚拉姆文（ദ്ദി）等 Unicode
区块——MiSans 远程 CSS 切片不含这些码位，Noto CJK / 彩色 Emoji 同样
不覆盖，缺兜底时公告卡里颜文字渲染成豆腐块（像素哈希探针证实：无包
时 9 个颜文字字符渲染完全同相，加包后逐字不同）。

对齐 XutheringWavesUID README 的系统字体建议（emoji / Symbola /
Noto Sans Symbols2 / CJK），本模块在插件侧自动补齐等价能力：

- 多级自愈：系统已装符号字体（Symbola / Segoe UI Symbol 等）直接用
  → 插件字体包（全部 OFL，googlefonts/noto-fonts 单源 + jsdelivr
  镜像；首个本地渲染触发后台单飞下载）→ 仍无字体的字符（如 Lisu
  增补 𖥦，noto 静态版无此区块）降级为豆腐块，不影响其余内容。
- 供给两条渲染链：playwright 经 /ark/fonts 静态路由 + 内联 @font-face
  （font_face_css，由 render_html 注入模板上下文）；takumi 不走系统
  fontconfig，由 render_utils._local_cjk_fonts 直接读字节注册。
- 失败语义：单镜像失败 warning 后换镜像，整轮未补齐进入 1h 冷却，
  不阻塞渲染、不成重试风暴；下载 tmp + 原子替换，跨进程 file_lock
  防多 worker 重复下载（锁超时降级并发重下，产物等价无害）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path

from gsuid_core.logger import logger

from ..path import TEMPLATES_PATH

# 字体包落盘目录：复用 templates/fonts 的 /ark/fonts 静态路由设计
# （render_utils._mount_fonts），运行时生成、仓库 .gitignore 忽略
FONT_PACK_DIR = TEMPLATES_PATH / "fonts" / "kaomoji"

# (落盘文件名, CSS font-family 名, 仓库内路径)。
# 选型依据 fc-scan 实测覆盖：NotoSans → ˶ˬᵕᴗ‿；Yi → ꒰꒱；
# CanadianAboriginal → ᗜᕕᐛᕗᐢ；Malayalam → ദ്ദി；Symbols2 → ✧ 等杂锦。
_FONT_PACK: tuple[tuple[str, str, str], ...] = (
    (
        "NotoSans-Regular.ttf",
        "Noto Sans",
        "hinted/ttf/NotoSans/NotoSans-Regular.ttf",
    ),
    (
        "NotoSansYi-Regular.ttf",
        "Noto Sans Yi",
        "hinted/ttf/NotoSansYi/NotoSansYi-Regular.ttf",
    ),
    (
        "NotoSansCanadianAboriginal-Regular.ttf",
        "Noto Sans Canadian Aboriginal",
        "hinted/ttf/NotoSansCanadianAboriginal/NotoSansCanadianAboriginal-Regular.ttf",
    ),
    (
        "NotoSansMalayalam-Regular.ttf",
        "Noto Sans Malayalam",
        "hinted/ttf/NotoSansMalayalam/NotoSansMalayalam-Regular.ttf",
    ),
    (
        "NotoSansSymbols2-Regular.ttf",
        "Noto Sans Symbols 2",
        "hinted/ttf/NotoSansSymbols2/NotoSansSymbols2-Regular.ttf",
    ),
)

_FONT_MIRRORS: tuple[str, ...] = (
    "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/",
    "https://cdn.jsdelivr.net/gh/googlefonts/noto-fonts@main/",
)

# 系统已装符号字体（存在即用；命中即部分免下包，wwuid README 建议清单同源）
_SYSTEM_SYMBOL_FONTS: tuple[Path, ...] = (
    Path("/usr/share/fonts/truetype/symbola/Symbola.ttf"),
    Path("/usr/share/fonts/Symbola.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansSymbols2-Regular.otf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Apple Symbols.ttf"),
    Path("/System/Library/Fonts/Supplemental/Symbola.ttf"),
    Path("C:/Windows/Fonts/seguisym.ttf"),
)

_FONT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"wOFF", b"wOF2")
# 反代/镜像错误页（实测 ~35KB HTML 文档）靠魔数 + 尺寸双拦
_FONT_MIN_BYTES = 20_000
_FONT_MAX_BYTES = 8_000_000
_RETRY_COOLDOWN = 3600.0

_pack_task: asyncio.Task | None = None
_fail_not_before: float = 0.0
_on_pack_ready: list[Callable[[], None]] = []


def register_on_pack_ready(callback: Callable[[], None]) -> None:
    """字体包有新文件落盘后的回调（render_utils 刷新缓存/重挂路由用）。"""
    _on_pack_ready.append(callback)


def is_valid_font(data: bytes) -> bool:
    """字体字节校验：魔数（ttf/otf/ttc/woff[2]）+ 尺寸护栏。"""
    return _FONT_MIN_BYTES <= len(data) <= _FONT_MAX_BYTES and data[:4] in _FONT_MAGIC


def pack_complete() -> bool:
    """字体包是否齐（stat 级；字节校验在下载与读取时做）。"""
    if not FONT_PACK_DIR.exists():
        return False
    return all((FONT_PACK_DIR / item[0]).exists() for item in _FONT_PACK)


def pack_font_paths() -> list[Path]:
    """字体包中校验通过的字体文件（takumi 字节注册用）。"""
    result: list[Path] = []
    if not FONT_PACK_DIR.exists():
        return result
    for filename, _, _ in _FONT_PACK:
        path = FONT_PACK_DIR / filename
        try:
            if is_valid_font(path.read_bytes()):
                result.append(path)
        except OSError:
            continue
    return result


def system_symbol_font_paths() -> list[Path]:
    """系统已装符号字体（存在即用）。"""
    return [path for path in _SYSTEM_SYMBOL_FONTS if path.exists()]


def font_face_css(base_url: str) -> str:
    """字体包已就绪字体 → 内联 @font-face CSS（playwright 页面加载用）。

    base_url 为 core 本地服务地址（/ark/fonts 静态路由）；包未就绪返回
    空串，模板按 falsy 跳过注入。系统字体无需声明——fontconfig 按
    font-family 栈中的 family 名直取。
    """
    rules = []
    for filename, family, _ in _FONT_PACK:
        if (FONT_PACK_DIR / filename).exists():
            rules.append(
                f'@font-face{{font-family:"{family}";'
                f'src:url("{base_url}/ark/fonts/kaomoji/{filename}")'
                ' format("truetype");font-display:swap;}'
            )
    return "\n".join(rules)


async def _download_font(filename: str, repo_path: str) -> bool:
    """单字体下载：双镜像顺序尝试，魔数+尺寸校验，tmp+原子替换。"""
    from gsuid_core.utils.download_resource.download_file import download

    from ..file_lock import file_locked, unique_tmp
    from ..http import get_client

    target = FONT_PACK_DIR / filename
    with file_locked(target):
        if target.exists():  # 等锁期间其他进程已下好
            return False
        FONT_PACK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = unique_tmp(target)
        client = await get_client()
        try:
            for mirror in _FONT_MIRRORS:
                url = f"{mirror}{repo_path}"
                retcode = await download(
                    url, FONT_PACK_DIR, tmp.name, sess=client, tag="[ARK·字体]"
                )
                data = tmp.read_bytes() if tmp.exists() else b""
                if retcode == 200 and is_valid_font(data):
                    os.replace(tmp, target)
                    logger.info(
                        f"[ARK·字体] 颜文字字体包 +{filename}"
                        f"（{len(data) // 1024}KB, {url.split('/')[2]}）"
                    )
                    return True
                logger.warning(
                    f"[ARK·字体] {filename} 下载无效"
                    f"（HTTP {retcode}, {len(data)}B），尝试下一镜像"
                )
        finally:
            tmp.unlink(missing_ok=True)
    return False


async def ensure_font_pack() -> tuple[bool, bool]:
    """补齐缺失字体（逐文件双镜像，跨进程锁 + 原子替换）。

    返回 (是否有新文件落盘, 包是否已齐)。任何单点失败不抛异常——已
    就绪文件照常生效，缺的维持降级（豆腐块同现状）。
    """
    if pack_complete():
        return False, True
    changed = False
    for filename, _, repo_path in _FONT_PACK:
        if (FONT_PACK_DIR / filename).exists():
            continue
        if await _download_font(filename, repo_path):
            changed = True
    return changed, pack_complete()


def maybe_start_download() -> None:
    """包未就绪且不在冷却期 → 后台单飞补包（渲染热路径调用，不阻塞）。

    单进程并发由 _pack_task 单飞保证（同进程不会出现两个下载任务），
    跨进程由 _download_font 内的 file_lock 协调。
    """
    global _pack_task
    if _pack_task is not None and not _pack_task.done():
        return
    if pack_complete() or time.time() < _fail_not_before:
        return
    _pack_task = asyncio.create_task(_download_task())


async def _download_task() -> None:
    global _fail_not_before
    try:
        changed, complete = await ensure_font_pack()
    except Exception:
        logger.exception("[ARK·字体] 颜文字字体包下载任务异常")
        changed, complete = False, False
    if not complete:
        _fail_not_before = time.time() + _RETRY_COOLDOWN
        logger.warning(
            "[ARK·字体] 颜文字字体包未补齐，"
            f"{int(_RETRY_COOLDOWN // 60)} 分钟后重试；"
            "期间公告颜文字可能显示为方框"
        )
    if changed:
        for callback in _on_pack_ready:
            try:
                callback()
            except Exception:
                logger.exception("[ARK·字体] 字体包就绪回调失败")
