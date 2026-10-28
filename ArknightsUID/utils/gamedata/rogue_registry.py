"""肉鸽期权威注册表（唯一事实源，纯数据模块零 I/O）。

期目录使用 tomimi 命名；主题表、森空岛玩家进度与战绩均从傀影
rogue_1 起编，同一期使用相同 rogue_N。官方 player/info 页面直接
以 records[].rogueId 查询 rogueInfoMap，不对编号做偏移。

收敛后**新一期 = 本表 THEMES 加一行** + rogue_ending.ENDING_GUIDES
补该期人工攻略；其余消费者（期名/别名/topicId/短名/排序/难度数据
文件名）全部从本模块派生导入，不再各养副本。

灰蕈迷境不占本表编号，不能为其插入 rogue_1 并平移其他主题。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RogueTheme:
    """一期肉鸽的目录、官方编号及展示信息。"""

    dir: str  # tomimi 期目录（数据文件与图标的目录名）
    table_id: str  # 主题表键 rogue_N（傀影起编；skland topicId 同口径）
    name: str  # 官方主题名（ArknightsGameData 主题表口径）
    short: str  # 战绩切换条短名（全称 7-8 字 × 多期超宽换行）
    diff_file: str  # tomimi difficulty_mods 文件名（black 期为上游无期名特例）
    aliases: tuple[str, ...] = ()  # 期内建别名（查询输入 → 期目录）


THEMES: tuple[RogueTheme, ...] = (
    RogueTheme(
        "phantom", "rogue_1", "傀影与猩红孤钻", "傀影",
        "difficulty_mods_phantom.json",
        ("傀影", "猩红", "傀影与猩红孤钻", "傀影肉鸽", "猩红肉鸽"),
    ),
    RogueTheme(
        "mizuki", "rogue_2", "水月与深蓝之树", "水月",
        "difficulty_mods_mizuki.json",
        ("水月", "深蓝", "水月与深蓝之树", "水月肉鸽"),
    ),
    RogueTheme(
        "sami", "rogue_3", "探索者的银凇止境", "萨米",
        "difficulty_mods_sami.json",
        # "萨米的腐朽安眠"是历史误写显示名，保留为别名兼容存量输入
        ("萨米", "银凇", "萨米的腐朽安眠", "萨米肉鸽"),
    ),
    RogueTheme(
        "sarkaz", "rogue_4", "萨卡兹的无终奇语", "萨卡兹",
        "difficulty_mods_sarkaz.json",
        ("萨卡兹", "无终奇语", "萨卡兹的无终奇语", "萨卡兹肉鸽"),
    ),
    RogueTheme(
        "sui", "rogue_5", "岁的界园志异", "界园",
        "difficulty_mods_sui.json",
        ("岁", "界园", "岁的界园志异", "界园肉鸽"),
    ),
    RogueTheme(
        "black", "rogue_6", "沉沦者的黑流树海", "树海",
        # 上游最新期文件名无期名后缀，是特例而非规律
        "difficulty_mods.json",
        ("黑流", "树海", "沉沦者", "沉沦者的黑流树海", "黑流树海",
         "树海肉鸽", "黑流肉鸽"),
    ),
)

# ---------------------------------------------------------------------------
# 派生视图（消费者按历史名导入，零行为漂移）
# ---------------------------------------------------------------------------

# 期目录 → 官方显示名（rogue_enemy 卡片期名 / rogue_ending 可用期列举）
THEME_DIRS: dict[str, str] = {t.dir: t.name for t in THEMES}

# 查询别名 → 期目录：逐字迁移自各处旧表（含全称简称与「X肉鸽」叫法，
# 零漂移）；官方显示名不进此表——resolve_theme_dir 走 THEME_DIRS 反查
THEME_ALIASES: dict[str, str] = {
    alias: t.dir for t in THEMES for alias in t.aliases
}

# 期目录 → 主题表键（skland 战绩 topicId 同口径；rogue_ending / records 共用）
THEME_TABLE_IDS: dict[str, str] = {t.dir: t.table_id for t in THEMES}

# 主题表键升序列表（rogue_relic 藏品期排序）
TABLE_THEME_ORDER: list[str] = [t.table_id for t in THEMES]

# 主题表键 → 切换条短名（records 战绩卡）
TOPIC_SHORT_NAMES: dict[str, str] = {t.table_id: t.short for t in THEMES}

# 玩家进度键与主题表键相同（官方 rogueInfoMap 同键查名）。
ROGUE_THEME_NAMES: dict[str, str] = {t.table_id: t.name for t in THEMES}


def diff_file_for(theme_dir: str) -> str:
    """期目录 → tomimi difficulty_mods 文件名；未知目录回退期名后缀规律。"""
    for t in THEMES:
        if t.dir == theme_dir:
            return t.diff_file
    return f"difficulty_mods_{theme_dir}.json"
