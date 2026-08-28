import base64
import asyncio
import time
import logging
from typing import Union, Optional
from pathlib import Path

import httpx

from gsuid_core.logger import logger
from gsuid_core.config import core_config, CONFIG_DEFAULT
from gsuid_core.app_life import app as fastapi_app
from fastapi.staticfiles import StaticFiles

from ..path import TEMPLATES_PATH, RESOURCE_PATH
from ..config import ArkConfig, CONFIG_DEFAULT
from ..http import get_client

logging.getLogger("uvicorn.access").addFilter(
    lambda record: "/ark/fonts" not in record.getMessage()
)

TEMPLATES_ABS_PATH = TEMPLATES_PATH

class CORSStaticFiles(StaticFiles):
    """Custom StaticFiles class to add CORS headers only for served files."""
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD"
        return response

def _import_playwright():
    try:
        from playwright.async_api import async_playwright
        return async_playwright
    except ImportError:
        if not ArkConfig.get_config("RemoteRenderEnable").data:
            logger.warning("[ARK·渲染] 未安装 playwright，无法渲染卡片。")
            logger.warning("[ARK·渲染] 可选择配置外置渲染（RemoteRenderEnable + RemoteRenderUrl）。")
            logger.info("[ARK·渲染] 安装方法 Linux/Mac: source .venv/bin/activate && uv pip install playwright && uv run playwright install chromium")
            logger.info("[ARK·渲染] 安装方法 Windows: .venv\\Scripts\\activate; uv pip install playwright; uv run playwright install chromium")
        return None


async_playwright = _import_playwright()
PLAYWRIGHT_AVAILABLE = async_playwright is not None

# ── pytakumi（Takumi 无浏览器渲染引擎）─────────────────────────────
# 同步 C 库调用，经 asyncio.to_thread 下放到线程池；fonts 参数注册本地
# CJK 字体作为兜底——模板的 MiSans 远程 CSS 失效（离线/CDN 故障）时
# 仍能出正常中文而不是豆腐块，注册 19MB ttc 实测仅 ~20ms。
TAKUMI_RENDER_WIDTH = 560  # 与 ark_base.css 的 .container 同宽


def _import_takumi():
    try:
        import pytakumi
        return pytakumi
    except ImportError:
        return None


pytakumi = _import_takumi()
TAKUMI_AVAILABLE = pytakumi is not None

_local_font_bytes: Optional[list] = None


def _local_cjk_fonts() -> list:
    """缓存读取系统 Noto Sans CJK（存在才返回），作为远程字体失败的兜底。"""
    global _local_font_bytes
    if _local_font_bytes is None:
        _local_font_bytes = []
        for candidate in (
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
        ):
            try:
                if candidate.exists():
                    _local_font_bytes = [candidate.read_bytes()]
                    break
            except OSError:
                continue
    return _local_font_bytes


def _render_via_takumi(html_content: str, width: int = TAKUMI_RENDER_WIDTH) -> bytes:
    """Takumi 无浏览器渲染（同步，调用方负责下放线程）。失败抛异常由上层回退。

    device_pixel_ratio 由 RenderScale 配置驱动。注意 pytakumi 的 width 参数是
    物理像素、CSS 视口 = width / DPR，因此要传 width×scale 才能保持 560px 的
    卡片设计宽：1x 输出 560px（手机端拉伸发糊），默认 2x 输出 1120px。
    """
    if pytakumi is None:
        raise RuntimeError("pytakumi 不可用")
    scale = _render_scale()
    return pytakumi.html_to_pic(
        html_content,
        width=int(width * scale),
        format="jpeg",
        quality=90,
        fonts=_local_cjk_fonts() or None,
        device_pixel_ratio=scale,
    )


def _render_scale() -> float:
    """RenderScale 配置 → 渲染倍率，非法值回落 2（清晰度优先）。"""
    try:
        value = int(ArkConfig.get_config("RenderScale").data or 2)
    except Exception:
        value = 2
    return float(value) if value >= 1 else 2.0


async def _render_local_takumi(html_content: str) -> Optional[bytes]:
    """引擎选择与执行；不可用/失败返回 None（打日志），由调用方回退 playwright。"""
    if not TAKUMI_AVAILABLE:
        logger.warning(
            "[ARK·渲染] RenderEngine=takumi 但未安装 pytakumi，"
            "回退 playwright（安装：uv pip install pytakumi）"
        )
        return None
    start_time = time.time()
    try:
        screenshot = await asyncio.to_thread(_render_via_takumi, html_content)
        render_time = time.time() - start_time
        html_kb = len(html_content) / 1024
        logger.info(
            f"[ARK·渲染] takumi 渲染成功，耗时: {render_time:.2f}s，"
            f"HTML: {html_kb:.0f}KB，图片: {len(screenshot)} bytes"
        )
        return screenshot
    except Exception as e:
        logger.warning(f"[ARK·渲染] takumi 渲染失败，回退 playwright: {e}")
        return None

_playwright = None
_browser = None
_browser_lock = asyncio.Lock()
_browser_loop = None  # 当前浏览器/页池/锁所属的事件循环
_browser_uses = 0
_last_used = 0.0
_active_renders = 0

_MAX_BROWSER_USES = 1000
_BROWSER_IDLE_TTL = 3600

# Page pool for reuse (avoids context/page creation overhead per render)
_page_pool: asyncio.Queue = asyncio.Queue()
_pool_lock = asyncio.Lock()
_pool_ctx = None
_pool_generation = 0  # Incremented on browser restart to invalidate stale pages

_FONT_CSS_NAME = "fonts.css"
_FONTS_DIR = TEMPLATES_PATH / "fonts"


def _get_font_css_url() -> str:
    url = ArkConfig.get_config("FontCssUrl").data
    if not url or not str(url).strip():
        return CONFIG_DEFAULT["FontCssUrl"].data
    return url


def _mount_fonts() -> None:
    try:
        for route in fastapi_app.routes:
            if getattr(route, "path", None) == "/ark/fonts":
                return
        if _FONTS_DIR.exists():
            fastapi_app.mount(
                "/ark/fonts",
                CORSStaticFiles(directory=_FONTS_DIR),
                name="ark_fonts",
            )
        logger.debug("[ARK·渲染] 已挂载字体静态路由 (CORS Enabled)")
    except Exception as e:
        logger.warning(f"[ARK·渲染] 挂载字体静态路由失败: {e}")


def _get_local_base_url() -> str:
    host = core_config.get_config("HOST") or CONFIG_DEFAULT["HOST"]
    port = core_config.get_config("PORT") or CONFIG_DEFAULT["PORT"]
    if host in ("0.0.0.0", "0.0.0.0:"):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


_mount_fonts()


async def _ensure_browser():
    """Get a reusable browser instance; restart periodically to bound memory."""
    global _playwright, _browser, _browser_uses, _last_used, _active_renders
    global _pool_ctx, _pool_generation, _browser_loop, _browser_lock, _pool_lock

    if not PLAYWRIGHT_AVAILABLE or async_playwright is None:
        return None

    # 事件循环更换（如测试套件逐用例换 loop / 插件重载）时，旧浏览器、页池与
    # 锁都绑定在旧 loop 上——跨 loop await 会永久挂起，这里直接整组作废重建，
    # 旧对象不做跨 loop 清理，由 GC / 进程退出回收。生产环境单 loop 不触发。
    loop = asyncio.get_running_loop()
    if _browser_loop is not None and _browser_loop is not loop:
        _playwright = None
        _browser = None
        _pool_ctx = None
        _pool_generation += 1
        while not _page_pool.empty():
            try:
                _page_pool.get_nowait()
            except asyncio.QueueEmpty:
                break
        _browser_lock = asyncio.Lock()
        _pool_lock = asyncio.Lock()
        _browser_uses = 0
        _last_used = 0.0
        _active_renders = 0
    _browser_loop = loop

    async with _browser_lock:
        now = time.monotonic()

        if _browser is not None and not _browser.is_connected():
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None

        need_restart = (
            _browser is None
            or _browser_uses >= _MAX_BROWSER_USES
            or (_last_used > 0 and now - _last_used > _BROWSER_IDLE_TTL)
        )

        if need_restart and _browser is not None and _active_renders > 0:
            need_restart = False

        if need_restart:
            if _browser is not None:
                try:
                    await _browser.close()
                except Exception:
                    pass
                _browser = None

            # Invalidate page pool (old pages belong to dead browser)
            _pool_ctx = None
            _pool_generation += 1
            while not _page_pool.empty():
                try:
                    _page_pool.get_nowait()
                except asyncio.QueueEmpty:
                    break

            if _playwright is None:
                _playwright = await async_playwright().start()

            _browser = await _playwright.chromium.launch(
                # --disable-gpu/--no-zygote：对齐 EndUID 实测，禁 GPU 避免
                # Windows 上 NVIDIA 驱动锁泄漏；单 renderer 进程降常驻内存
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-gpu",
                    "--no-zygote",
                    "--renderer-process-limit=1",
                ]
            )
            _browser_uses = 0

        _last_used = now
        return _browser


_pool_lock = asyncio.Lock()


async def _acquire_page():
    """Get a reusable page from the pool, or create a new one."""
    global _pool_ctx, _active_renders

    browser = await _ensure_browser()
    if browser is None:
        return None, -1

    async with _pool_lock:
        gen = _pool_generation

        # Try to reuse a pooled page
        while not _page_pool.empty():
            try:
                page, page_gen = _page_pool.get_nowait()
                if page_gen == gen and not page.is_closed():
                    _active_renders += 1
                    return page, gen
            except asyncio.QueueEmpty:
                break

        # Create shared context if needed
        ctx_closed = _pool_ctx is None
        if not ctx_closed:
            try:
                ctx_closed = _pool_ctx._impl_obj._is_closed
            except AttributeError:
                ctx_closed = True
        if ctx_closed:
            _pool_ctx = await browser.new_context(
                viewport={"width": 1200, "height": 1000},
                # 截图物理分辨率 = CSS 尺寸 × 倍率（RenderScale，默认 2x 防手机端发糊）
                device_scale_factor=_render_scale(),
            )

    page = await _pool_ctx.new_page()
    _active_renders += 1
    return page, _pool_generation


async def _release_page(page, gen: int):
    """Return a page to the pool for reuse, or discard if stale."""
    global _active_renders, _browser_uses, _last_used

    _active_renders = max(0, _active_renders - 1)
    _browser_uses += 1
    _last_used = time.monotonic()

    if gen == _pool_generation and not page.is_closed():
        await _page_pool.put((page, gen))
    else:
        try:
            await page.close()
        except Exception:
            pass


async def _render_via_remote(html_content: str, remote_url: str) -> Optional[bytes]:
    """使用外置渲染服务渲染 HTML"""
    start_time = time.time()
    try:
        logger.debug(f"[ARK·渲染] 尝试使用外置渲染服务: {remote_url}")

        client = await get_client()
        response = await client.post(
            remote_url,
            json={"html": html_content},
            headers={"Content-Type": "application/json"},
            timeout=60.0,
        )

        if response.status_code == 200:
            image_data = response.content
            elapsed_time = time.time() - start_time
            html_kb = len(html_content) / 1024
            logger.info(f"[ARK·渲染] 外置渲染成功，耗时: {elapsed_time:.2f}s，HTML大小: {html_kb:.1f}KB，图片大小: {len(image_data)} bytes")
            return image_data
        else:
            logger.warning(f"[ARK·渲染] 外置渲染失败，状态码: {response.status_code}, 错误: {response.text}")
            return None
    except httpx.TimeoutException:
        elapsed_time = time.time() - start_time
        logger.warning(f"[ARK·渲染] 外置渲染超时 ({elapsed_time:.2f}s)，将回退到本地渲染")
        return None
    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.warning(f"[ARK·渲染] 外置渲染异常 ({elapsed_time:.2f}s): {e}，将回退到本地渲染")
        return None


async def render_html(ark_templates, template_name: str, context: dict) -> Optional[bytes]:

    try:
        logger.debug(f"[ARK·渲染] HTML渲染开始: {template_name}")

        template = ark_templates.get_template(template_name)

        remote_render_enable = ArkConfig.get_config("RemoteRenderEnable").data
        remote_url = ArkConfig.get_config("RemoteRenderUrl").data if remote_render_enable else None

        if remote_render_enable and remote_url:
            try:
                font_css_url = _get_font_css_url()
                context["font_css_url"] = font_css_url
                html_content = template.render(**context)
                logger.debug(f"[ARK·渲染] 使用在线字体渲染 HTML: {template_name}")
                logger.debug(f"[ARK·渲染] 外置渲染已启用，尝试使用: {remote_url}")
                remote_result = await _render_via_remote(html_content, remote_url)
                if remote_result is not None:
                    return remote_result

                logger.info("[ARK·渲染] 外置渲染失败，回退到本地渲染")
            except Exception as e:
                logger.warning(f"[ARK·渲染] 外置渲染异常: {e}，回退到本地渲染")

        try:
            font_css_path = _FONTS_DIR / _FONT_CSS_NAME
            base_url = _get_local_base_url()

            if font_css_path.exists():
                context["font_css_url"] = f"{base_url}/ark/fonts/{_FONT_CSS_NAME}"
            else:
                context["font_css_url"] = _get_font_css_url()

            html_content = template.render(**context)
            logger.debug(f"[ARK·渲染] 使用本地字体渲染 HTML: {template_name}")
        except Exception as e:
            logger.error(f"[ARK·渲染] Template render failed: {e}")
            raise e

        # 本地渲染
        engine = str(ArkConfig.get_config("RenderEngine").data or "takumi").lower()
        if engine == "takumi":
            takumi_result = await _render_local_takumi(html_content)
            if takumi_result is not None:
                return takumi_result
            # 回退到下方 playwright 路径

        if not PLAYWRIGHT_AVAILABLE or async_playwright is None:
            logger.warning("[ARK·渲染] Playwright 未安装，无法渲染，将回退到 PIL 渲染（如有）")
            return None

        logger.debug(f"[ARK·渲染] 使用本地 Playwright 渲染")

        local_start_time = time.time()
        page, gen = None, -1
        try:
            page, gen = await _acquire_page()
            if page is None:
                return None

            await page.set_content(html_content, wait_until='load')

            container = page.locator(".container")
            await page.wait_for_selector(".container", timeout=2000)
            size = await container.evaluate(
                """(el) => {
                    const rect = el.getBoundingClientRect();
                    const width = Math.ceil(Math.max(rect.width, el.scrollWidth));
                    const height = Math.ceil(Math.max(rect.height, el.scrollHeight));
                    return { width, height };
                }"""
            )

            if size and size.get("width") and size.get("height"):
                await page.set_viewport_size(
                    {
                        "width": max(1, int(size["width"])),
                        "height": max(1, int(size["height"])),
                    }
                )

            screenshot = await container.screenshot(type='jpeg', quality=90)
            render_time = time.time() - local_start_time
            html_kb = len(html_content) / 1024
            logger.info(f"[ARK·渲染] 本地渲染成功，耗时: {render_time:.2f}s，HTML: {html_kb:.0f}KB，图片: {len(screenshot)} bytes")
            return screenshot
        except Exception as e:
            logger.error(f"[ARK·渲染] Playwright execution failed: {e}")
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
                page = None
            raise e
        finally:
            if page is not None:
                await _release_page(page, gen)

    except Exception as e:
        logger.error(f"[ARK·渲染] HTML渲染失败: {e}")
        return None


def image_to_base64(image_path: Union[str, Path], quality: int = 0) -> str:
    """图片文件转 base64 data URL。

    quality=0: 原格式直读（无损）
    quality>0: WebP 有损压缩
    """
    if not isinstance(image_path, Path):
        image_path = Path(image_path)
    if not image_path.exists():
        return ""
    try:
        if quality > 0:
            from PIL import Image
            from io import BytesIO
            img = Image.open(image_path).convert("RGBA")
            buf = BytesIO()
            img.save(buf, format="WEBP", quality=quality)
            return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode('utf-8')
        with open(image_path, "rb") as f:
            data = f.read()
        ext = image_path.suffix.lstrip(".").lower()
        if ext == "jpg":
            ext = "jpeg"
        return f"data:image/{ext};base64,{base64.b64encode(data).decode('utf-8')}"
    except Exception as e:
        logger.warning(f"[ARK·渲染] 图片转 base64 失败: {image_path}, {e}")
        return ""


async def get_image_b64_with_cache(
    url: str, cache_path: Path, quality=None, cover_size: tuple = None,
) -> str:
    if not url:
        return ""

    try:
        from ..image import pic_download_from_url
        from ..path import BAKE_PATH
        from PIL import Image
        from io import BytesIO

        await pic_download_from_url(cache_path, url)

        filename = url.split("/")[-1]
        local_path = cache_path / filename
        # pic_download_from_url 会将图片转为 webp 并删除原文件
        webp_path = local_path.with_suffix(".webp")
        if not local_path.exists() and webp_path.exists():
            local_path = webp_path

        # 不压缩也不裁切，直接返回原图
        if quality is None and cover_size is None:
            ext = local_path.suffix.lstrip(".").lower()
            if ext == "jpg":
                ext = "jpeg"
            with open(local_path, "rb") as f:
                data = f.read()
            return f"data:image/{ext};base64,{base64.b64encode(data).decode('utf-8')}"

        # 烘焙缓存: bake/{文件名}_{路径hash}_{quality}_{宽x高}.webp
        import hashlib
        path_hash = hashlib.md5(str(local_path.resolve()).encode()).hexdigest()[:8]
        stem = Path(filename).stem
        size_tag = f"_{cover_size[0]}x{cover_size[1]}" if cover_size else ""
        bake_name = f"{stem}_{path_hash}_q{quality or 80}{size_tag}.webp"
        bake_path = BAKE_PATH / bake_name

        if bake_path.exists() and bake_path.stat().st_mtime >= local_path.stat().st_mtime:
            with open(bake_path, "rb") as f:
                data = f.read()
            return f"data:image/webp;base64,{base64.b64encode(data).decode('utf-8')}"

        # 未命中 — PIL 处理 + 写入烘焙缓存
        img = Image.open(local_path)

        if cover_size is not None:
            tw, th = cover_size
            scale = max(tw / img.width, th / img.height)
            new_w, new_h = int(img.width * scale), int(img.height * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - tw) // 2
            top = (new_h - th) // 2
            img = img.crop((left, top, left + tw, top + th))

        img.save(bake_path, 'WEBP', quality=quality or 80)

        with open(bake_path, "rb") as f:
            data = f.read()

        orig_size = local_path.stat().st_size
        logger.debug(
            f"[ARK·渲染] 烘焙: {filename} → {bake_name}, "
            f"原始: {orig_size} bytes, 烘焙后: {len(data)} bytes"
        )

        return f"data:image/webp;base64,{base64.b64encode(data).decode('utf-8')}"

    except Exception as e:
        logger.warning(f"[ARK·渲染] 获取图片 base64 失败: {url}, {e}")
        return ""
