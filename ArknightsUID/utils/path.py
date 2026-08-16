"""ArknightsUID 路径管理。

所有运行时数据（缓存、玩家数据、模板等）的路径都在此集中声明，
避免散落在各模块导致路径不一致。基础目录复用 GsCore 的 get_res_path，
保证插件数据落在 GsCore 的统一资源目录下。
"""
from __future__ import annotations

import os
from pathlib import Path

from gsuid_core.data_store import get_res_path

# 插件根数据目录：<GsCore 资源目录>/ArknightsUID
MAIN_PATH: Path = get_res_path() / "ArknightsUID"

# 玩家数据（player_info / 抽卡记录 等），按 UID 分子目录
PLAYER_PATH: Path = MAIN_PATH / "players"

# 缓存根目录及子目录
CACHE_BASE: Path = MAIN_PATH / "cache"
AVATAR_CACHE_PATH: Path = CACHE_BASE / "avatar"        # 干员头像
CHAR_CACHE_PATH: Path = CACHE_BASE / "char"            # 干员立绘
SKILL_CACHE_PATH: Path = CACHE_BASE / "skill"          # 技能图标
EQUIP_CACHE_PATH: Path = CACHE_BASE / "equip"          # 模组图标
BAKE_PATH: Path = CACHE_BASE / "bake"                  # 图片烘焙缓存（webp）
WIKI_CACHE_PATH: Path = CACHE_BASE / "wiki"            # PRTS Wiki 查询缓存

# 配置文件
CONFIG_PATH: Path = MAIN_PATH / "config.json"

# 模板目录（jinja2 模板根）
TEMPLATES_PATH: Path = Path(__file__).parents[1] / "templates"

# 静态资源根（插件内置资源，非运行时生成）
RESOURCE_PATH: Path = Path(__file__).parents[1] / "resources"


def init_dir() -> None:
    """确保所有运行时目录存在，并收紧到 700。

    目录内存玩家数据（player_info/抽卡记录）与凭据相关缓存，
    同机其他本地用户不应可读（gsuid_core 建目录默认受 umask 影响为 755）。
    """
    dirs = [
        MAIN_PATH,
        PLAYER_PATH,
        CACHE_BASE,
        AVATAR_CACHE_PATH,
        CHAR_CACHE_PATH,
        SKILL_CACHE_PATH,
        EQUIP_CACHE_PATH,
        BAKE_PATH,
        WIKI_CACHE_PATH,
    ]
    for p in dirs:
        p.mkdir(parents=True, exist_ok=True)
    for p in dirs:
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass  # 无所有权等环境下跳过，不阻断插件加载


# 导入时立即初始化，保证后续读写不会因目录缺失失败
init_dir()
