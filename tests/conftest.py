"""测试环境修正：解除外层仓库目录对内层 ArknightsUID 包的遮蔽。

仓库根目录自带一个 __init__.py（gsuid_core 插件加载标记），而 pytest 在
rootdir=gsuid_core 下收集时会把 plugins/ 目录顶到 sys.path 前端，导致
`import ArknightsUID` 命中外层仓库目录（其下没有 utils/ 等子包）。

这里在收集测试模块之前就以正确路径把内层包导入并缓存，
后续所有 `import ArknightsUID.xxx` 直接命中缓存，不受路径顺序影响。
"""

import atexit
import os
import tempfile
import sys
from pathlib import Path

from cryptography.fernet import Fernet
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 在任何插件/配置/数据库导入前隔离运行时数据，测试不依赖线上缓存。
sys.path.insert(0, str(ROOT / "tests"))
from runtime_isolation import block_external_network, isolate_data_root  # noqa: E402

_collection_network_guard = pytest.MonkeyPatch()
_collection_requests = block_external_network(_collection_network_guard)
atexit.register(_collection_network_guard.undo)

_test_data = tempfile.TemporaryDirectory(prefix="arknightsuid-pytest-")
atexit.register(_test_data.cleanup)
isolate_data_root(Path(_test_data.name))

sys.modules.pop("ArknightsUID", None)

import ArknightsUID  # noqa: F401,E402 —— 立即以仓库根路径缓存内层包

assert ArknightsUID.__file__ is not None and str(ROOT) in ArknightsUID.__file__, (
    f"ArknightsUID 解析到了错误的位置: {ArknightsUID.__file__}"
)

# 固定测试密钥：凭据加密走环境变量，避免测试在真实 gsuid_core 数据目录
# 自动生成密钥文件（utils/crypto 的回退行为）。
os.environ.setdefault("ARK_UID_ENC_KEY", Fernet.generate_key().decode())

# hide_uid 现在跟随全局 HideUid 配置（数据目录里可能是 false）。
# 回复脱敏/安全类测试统一按「开启脱敏」口径断言：内存态固定为 True，
# 不写回数据文件。
from ArknightsUID.utils.config import ArkConfig  # noqa: E402

ArkConfig.get_config("HideUid").data = True


def pytest_collection_finish(session):
    if _collection_requests:
        raise pytest.UsageError(f"测试收集阶段尝试联网：{_collection_requests}")


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    """即使业务捕获异常，离线回归的未模拟联网也必须失败。"""
    requests = block_external_network(monkeypatch)
    yield
    assert not requests, f"请在业务边界模拟外部 HTTP：{requests}"


@pytest.fixture
def offline_card_assets(monkeypatch):
    """模板契约测试使用空素材和名称表；数据及下载由专门测试覆盖。"""
    from unittest.mock import AsyncMock

    from ArknightsUID import arknightsuid_wiki as wiki
    from ArknightsUID.utils import render
    from ArknightsUID.utils.prts import client as prts
    from ArknightsUID.utils.gamedata import _table_cache, charinfo, rogue_enemy, rogue_relic

    for module, names, result in (
        (render, ("get_image_b64_with_cache",), ""),
        (wiki, ("_skill_icon_b64", "_operator_portrait_b64", "_get_stage_map_b64"), ""),
        (charinfo, ("load_skill_names",), {}),
        (_table_cache, ("ensure_uniequip_table",), {}),
        (prts, ("rogue_map_file_urls",), []),
        (rogue_enemy, ("enemy_icon_b64",), ""),
        (rogue_relic, ("relic_icon_b64",), ""),
    ):
        for name in names:
            monkeypatch.setattr(module, name, AsyncMock(return_value=result))
