"""ArknightsUID 路径管理。

插件内模板和内置字体只读；玩家数据、缓存及下载资源统一落在 GsCore
数据目录。路径在此集中声明，兼容入口 utils/resource/RESOURCE_PATH.py
仅转出这些定义。
"""

from __future__ import annotations

import os
from pathlib import Path

from gsuid_core.data_store import get_res_path

# 随插件发布的代码与静态资源
PACKAGE_PATH: Path = Path(__file__).resolve().parents[1]
PLUGIN_ROOT: Path = PACKAGE_PATH.parent
TEMPLATES_PATH: Path = PACKAGE_PATH / "templates"
BUNDLED_FONTS_PATH: Path = TEMPLATES_PATH / "fonts"

# 插件根数据目录：<GsCore 资源目录>/ArknightsUID
MAIN_PATH: Path = get_res_path() / "ArknightsUID"

# 玩家数据（player_info / 抽卡记录 等），按 UID 分子目录
PLAYER_PATH: Path = MAIN_PATH / "players"

# 缓存根目录及子目录
CACHE_BASE: Path = MAIN_PATH / "cache"
CACHE_PATH: Path = CACHE_BASE  # 兼容早期资源模块的命名
AVATAR_CACHE_PATH: Path = CACHE_BASE / "avatar"  # 干员头像
CHAR_CACHE_PATH: Path = CACHE_BASE / "char"  # 干员立绘
SKILL_CACHE_PATH: Path = CACHE_BASE / "skill"  # 技能图标
EQUIP_CACHE_PATH: Path = CACHE_BASE / "equip"  # 模组图标
BAKE_PATH: Path = CACHE_BASE / "bake"  # 图片烘焙缓存（webp）
WIKI_CACHE_PATH: Path = CACHE_BASE / "wiki"  # PRTS Wiki 查询缓存

# 配置文件
CONFIG_PATH: Path = MAIN_PATH / "config.json"

# 用户背景与自动下载的资源；不向插件源码目录写入新文件
RESOURCE_PATH: Path = MAIN_PATH / "resource"
FONT_CACHE_PATH: Path = RESOURCE_PATH / "fonts"


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
        RESOURCE_PATH,
        FONT_CACHE_PATH,
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
