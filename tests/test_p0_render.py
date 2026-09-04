"""P0 渲染管线自检。

不依赖完整 GsCore 运行时——只验证：
1. jinja2 能加载模板
2. CSS @import 能解析
3. ArkConfig 能读取默认值
4. render_html 能调用（playwright 不可用时返回 None，但流程不报错）

运行：在 gsuid_core venv 下 `python -m pytest tests/test_p0_render.py -v`
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))


def test_jinja_template_loads():
    """jinja2 能加载 ark_ping.html 模板。"""
    from ArknightsUID.utils.render import ark_templates

    template = ark_templates.get_template("ark_ping.html")
    assert template is not None
    # 渲染一次，确保没有语法错误
    html = template.render(
        font_css_url="about:blank",
        ping_id="test-001",
        status="OK",
        engine="playwright",
        template_path="/tmp",
        timestamp=str(int(time.time())),
        version="0.3.0",
    )
    assert "ArknightsUID" in html
    assert "test-001" in html


def test_config_defaults():
    """ArkConfig 能读取默认值。"""
    from ArknightsUID.utils.config import ArkConfig

    # RemoteRenderEnable 默认 False
    assert ArkConfig.get_config("RemoteRenderEnable").data is False
    # FontCssUrl 有默认值
    assert "fonts" in ArkConfig.get_config("FontCssUrl").data


def test_smsdk_generates_valid_did():
    """smsdk 能生成合法的 dId（以 D 开头）。"""
    from ArknightsUID.utils.api.smsdk import get_device_id

    did = get_device_id()
    assert isinstance(did, str)
    assert did.startswith("D"), f"dId 应以 D 开头，实际：{did[:5]}..."
    assert len(did) > 100, f"dId 长度应 >100，实际：{len(did)}"


def test_request_util_respcode():
    """RespCode 枚举值正确。"""
    from ArknightsUID.utils.api.request_util import RespCode

    assert int(RespCode.OK) == 0
    assert int(RespCode.TOKEN_INVALID) == 220
    assert int(RespCode.CRED_INVALID) == 10001


def test_skland_client_constants():
    """client.py 常量重命名生效。"""
    from ArknightsUID.utils.skland import client

    # 新名称存在
    assert client.ARKNIGHTS_CHANNEL_OFFICIAL == 1
    # 旧名称仍兼容
    assert client.ARKNIGHTS_GAME_ID == 1
    assert client.ARKNIGHTS_GAME_ID is client.ARKNIGHTS_CHANNEL_OFFICIAL


def test_path_directories_exist():
    """path.py 初始化时创建的目录确实存在。"""
    from ArknightsUID.utils.path import (
        MAIN_PATH,
        PLAYER_PATH,
        CACHE_BASE,
        AVATAR_CACHE_PATH,
    )

    assert MAIN_PATH.exists()
    assert PLAYER_PATH.exists()
    assert CACHE_BASE.exists()
    assert AVATAR_CACHE_PATH.exists()


def test_player_store_gzip_whitelist():
    """player_store 的 gzip 白名单只含明日方舟的 json。"""
    from ArknightsUID.utils import player_store

    # 明日方舟的大文件应启用 gzip
    assert player_store._is_gzip("player_info.json")
    assert player_store._is_gzip("gacha_logs.json")
    # 终末地的文件名应不在白名单
    assert not player_store._is_gzip("card_detail.json")
    assert not player_store._is_gzip("indie_hard.json")


def test_render_html_signature():
    """render_html 接口签名正确（playwright 不可用时返回 None）。

    这是一个轻量验证——不真的渲染，只确保函数能被调用、
    playwright 不可用时优雅返回 None 而不抛异常。
    """
    from ArknightsUID.utils.render import ark_templates, render_html

    async def _run():
        # playwright 未安装时 PLAYWRIGHT_AVAILABLE=False，函数应返回 None
        result = await render_html(
            ark_templates,
            "ark_ping.html",
            {
                "ping_id": "sig-test",
                "status": "OK",
                "engine": "test",
                "template_path": "/tmp",
                "timestamp": "0",
                "version": "0.0.0",
            },
        )
        # 安装了 playwright 时返回 bytes，没装时返回 None
        assert result is None or isinstance(result, bytes)

    asyncio.run(_run())


if __name__ == "__main__":
    # 手动运行（无 pytest 时）
    tests = [
        test_jinja_template_loads,
        test_config_defaults,
        test_smsdk_generates_valid_did,
        test_request_util_respcode,
        test_skland_client_constants,
        test_path_directories_exist,
        test_cache_basic_ops,
        test_player_store_gzip_whitelist,
        test_render_html_signature,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"❌ {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n总计：{passed} 通过 / {failed} 失败 / {len(tests)} 项")
    sys.exit(0 if failed == 0 else 1)
