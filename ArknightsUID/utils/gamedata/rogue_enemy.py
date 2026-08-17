"""肉鸽专用敌人 buff 面板（tomimi.dev 开源数据）。

数据源（tanch-p/tomimi-dev 仓库，jsdelivr/raw 直链 + 7 天磁盘缓存）：
- enemy_database.json：1718 个敌人的分形态基础面板（hp/atk/def/res/aspd/
  range/weight/ms）+ 攻击方式 + traits 技能键
- difficulty_mods_{theme}.json：各期难度档效果（targets 为敌人键或
  NORMAL/ELITE/BOSS 类标签；mods 为 mul 乘数）

计算口径（tomimi statHelpers 同源语义）：
- 难度档效果**累积**（所选档 ≤ 的全部效果生效，store 源码实证）
- order=initial + value 0.2 → 基础值 +20%；无 order + value 1.2 → ×1.2（final）
- 难度 0/1/2（新手档）额外 HP ×0.8/0.85/0.9
- 层数增益 floorBuff 按层指数复合，bot 查询无法得知层数，不折算（提示见卡）

仅服务肉鸽场景：enemy_database 是 IS 出怪池的并集，与主线 ark敌人（PRTS）
互补；交互式全 buff 模拟（藏品/分队/密文叠加）请用 tomimi.dev。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from ._table_cache import GAMEDATA_CACHE_DIR

IS_ENEMY_CACHE_DIR = GAMEDATA_CACHE_DIR / "is_enemy"
_DATA_TTL = 7 * 86400.0

# 数据直链（jsdelivr CDN：content-type 正确、快；raw 直链核心下载器会 5s 超时）
_TOMIMI_BASE = (
    "https://cdn.jsdelivr.net/gh/tanch-p/tomimi-dev@main/src/lib/data"
)
_ICON_BASE = (
    "https://cdn.jsdelivr.net/gh/tanch-p/tomimi-dev@main"
    "/static/images/enemy_icons"
)
_ICON_MISS_TTL = 3600.0

# 期目录 → 显示名（tomimi 目录命名；black 为最新期沉沦者）
THEME_DIRS = {
    "phantom": "傀影与猩红孤钻",
    "mizuki": "水月与深蓝之树",
    "sami": "萨米的腐朽安眠",
    "sarkaz": "萨卡兹的无终奇语",
    "sui": "岁的界园志异",
    "black": "沉沦者的黑流树海",
}

# enemy.type 标签 → 期目录（归属启发式；权威判定是难度效果里的显式 targets）
_TYPE_THEME_HINTS = {
    "sarkaz": "sarkaz",
    "seamonster": "mizuki",
    "collapsal": "sami",
    "mutant": "sami",
}

# 面板数值字段（aspd/ms/range 等不参与难度折算表，v1 不展示）
PANEL_STATS = ("hp", "atk", "def", "res")
STAT_LABELS = {"hp": "HP", "atk": "攻", "def": "防", "res": "抗"}

_ATK_TYPE_NAMES = {
    "melee": "近战", "ranged": "远程",
    "phys": "物理", "arts": "法术", "real": "真实",
}
_CLASS_NAMES = {"BOSS": "领袖", "ELITE": "精英", "NORMAL": "普通"}

# 新手档（难度 0/1/2）敌人 HP 削减（tomimi noobHelp 同源常量）
_NOOB_HP_CUT = {0: 0.8, 1: 0.85, 2: 0.9}


async def _fetch_json_cached(filename: str) -> Any:
    """enemy/ 根下的数据文件 → 本地缓存（7 天 TTL），自带 20s httpx 客户端。"""
    import json

    path = Path(IS_ENEMY_CACHE_DIR) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and time.time() - path.stat().st_mtime < _DATA_TTL:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    url = f"{_TOMIMI_BASE}/enemy/{filename}"
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        path.write_bytes(resp.content)
    return json.loads(path.read_text(encoding="utf-8"))


async def load_enemy_db() -> dict[str, dict]:
    """敌人库 {enemy_key: entry}；失败返回 {}。"""
    try:
        data = await _fetch_json_cached("enemy_database.json")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _diff_file_url(theme_dir: str) -> str:
    name = {
        "phantom": "difficulty_mods_phantom.json",
        "mizuki": "difficulty_mods_mizuki.json",
        "sami": "difficulty_mods_sami.json",
        "sarkaz": "difficulty_mods_sarkaz.json",
        "sui": "difficulty_mods_sui.json",
        "black": "difficulty_mods.json",
    }[theme_dir]
    return f"{_TOMIMI_BASE}/is/{theme_dir}/{name}"


async def load_difficulty_mods() -> dict[str, list]:
    """各期难度效果 {期目录: [grade entries]}；单期失败跳过。"""
    import json

    result: dict[str, list] = {}
    for theme_dir in THEME_DIRS:
        path = Path(IS_ENEMY_CACHE_DIR) / f"difficulty_{theme_dir}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not (path.exists() and time.time() - path.stat().st_mtime < _DATA_TTL):
                async with httpx.AsyncClient(
                    timeout=20.0, follow_redirects=True
                ) as client:
                    resp = await client.get(_diff_file_url(theme_dir))
                    resp.raise_for_status()
                    path.write_bytes(resp.content)
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                result[theme_dir] = data
        except Exception:
            continue
    return result


def match_rogue_enemies(
    query: str, db: dict[str, dict], limit: int = 8
) -> list[str]:
    """按中文名匹配敌人键：精确 → 唯一前缀 → 唯一子串；多命中返回候选。"""
    q = (query or "").strip()
    if not q:
        return []
    names = {key: str(entry.get("name_zh") or "") for key, entry in db.items()}
    for key, name in names.items():
        if name == q:
            return [key]
    prefix = [key for key, name in names.items() if name.startswith(q)]
    if len(prefix) == 1:
        return prefix
    if len(prefix) > 1:
        prefix.sort(key=lambda k: names[k])
        return prefix[:limit]
    contains = [key for key, name in names.items() if q in name]
    if len(contains) == 1:
        return contains
    contains.sort(key=lambda k: names[k])
    return contains[:limit]


def detect_theme(
    enemy_key: str, enemy_types: list[str], diff_data: dict[str, list]
) -> str | None:
    """敌人所属期：显式 targets（权威）→ type 标签启发式 → None（仅基础面板）。"""
    for theme_dir, grades in diff_data.items():
        for grade in grades:
            for effect in grade.get("effects", []):
                targets = effect.get("targets") or []
                if enemy_key in targets:
                    return theme_dir
    for tag in enemy_types or []:
        if tag in _TYPE_THEME_HINTS:
            return _TYPE_THEME_HINTS[tag]
    return None


def _mods_apply(base: float, mods: list[dict]) -> float:
    """tomimi 乘数语义：initial delta 相加、final 乘数连乘。"""
    init_delta = 0.0
    final_mul = 1.0
    for mod in mods:
        if mod.get("mode") != "mul":
            continue
        if mod.get("order") == "initial":
            init_delta += float(mod.get("value") or 0)
        else:
            final_mul *= float(mod.get("value") or 1)
    return base * (1 + init_delta) * final_mul


def _grade_mods_for_enemy(
    grades: list[dict], enemy_key: str, enemy_types: list[str], upto: int
) -> list[dict]:
    """≤ upto 档中命中该敌人的全部 mods（累积口径）。"""
    type_set = set(enemy_types or [])
    mods: list[dict] = []
    for grade in grades:
        if int(grade.get("difficulty", 0)) > upto:
            continue
        for effect in grade.get("effects", []):
            targets = set(effect.get("targets") or [])
            if (
                enemy_key in targets
                or (targets & type_set)
                or "ALL" in targets
            ):
                mods.extend(effect.get("mods") or [])
    return mods


def build_diff_panels(
    entry: dict, grades: list[dict], max_rows: int = 10
) -> list[dict]:
    """难度面板行：[{grade, stats: {hp/atk/def/res}, notes: [str]}]。

    行 = 新手档(0/1/2) + 有累积变化的高档；数值基于形态 0（基础形态）。
    非面板字段（如 dmg_res/aspd）以文本 note 保留。
    """
    stats_blocks = entry.get("stats") or []
    if not stats_blocks:
        return []
    base_block = stats_blocks[0]
    enemy_key = str(entry.get("key") or "")
    enemy_types = list(entry.get("type") or [])

    # 有效果的难度档（面板行候选）+ 新手档 0/1/2（HP 削减，均有信息量）
    effect_grades = sorted(
        {int(g.get("difficulty", 0)) for g in grades if g.get("effects")}
    )
    row_grades = sorted({0, 1, 2} | set(effect_grades))
    if len(row_grades) > max_rows:
        # 超限时保留新手档 + 最近的高档（靠近满难度最有参考价值）
        head = [g for g in row_grades if g <= 2]
        tail = [g for g in row_grades if g > 2][-(max_rows - len(head)):]
        row_grades = sorted(head + tail)

    rows: list[dict] = []
    for grade in row_grades:
        mods = _grade_mods_for_enemy(grades, enemy_key, enemy_types, grade)
        # 新手档 HP 削减（作为 final 乘数）
        noob = _NOOB_HP_CUT.get(grade)
        stats: dict[str, int] = {}
        notes: list[str] = []
        stat_mods: dict[str, list[dict]] = {}
        for mod in mods:
            key = str(mod.get("key") or "")
            if key in PANEL_STATS:
                stat_mods.setdefault(key, []).append(mod)
            else:
                notes.append(f"{key} {mod.get('value')}")
        for stat_key in PANEL_STATS:
            base = float(base_block.get(stat_key) or 0)
            value = _mods_apply(base, stat_mods.get(stat_key, []))
            if noob and stat_key == "hp":
                value *= noob
            stats[stat_key] = round(value)
        label = f"难度{grade}"
        if effect_grades and grade >= max(effect_grades):
            label += " 起（更高档数值不变）"
        if noob and grade <= 2:
            label += f"·新手（HP×{noob}）"
        rows.append({"grade": grade, "label": label, "stats": stats, "notes": notes})

    # 连续同面板的档位合并（如 10~15 数值不变），减少重复行
    merged: list[dict] = []
    for row in rows:
        if (
            merged
            and merged[-1]["stats"] == row["stats"]
            and merged[-1]["notes"] == row["notes"]
        ):
            first_grade = merged[-1]["grade"]
            if "起" in row["label"]:
                merged[-1]["label"] = (
                    f"难度{first_grade}~{row['grade']} 起（更高档数值不变）"
                )
            else:
                merged[-1]["label"] = f"难度{first_grade}~{row['grade']}"
            continue
        merged.append(row)
    return merged


def form_context(entry: dict) -> list[dict]:
    """形态行：各形态基础面板 + 攻击方式（atk_type 映射中文）。"""
    stats_blocks = entry.get("stats") or []
    forms = entry.get("forms") or []
    rows: list[dict] = []
    for i, block in enumerate(stats_blocks):
        form = forms[i] if i < len(forms) else {}
        attack = form.get("normal_attack") or {}
        atk_tags = [
            _ATK_TYPE_NAMES.get(t, t) for t in (attack.get("atk_type") or [])
        ]
        rows.append(
            {
                "title": form.get("title") or f"形态{i + 1}",
                "hp": round(float(block.get("hp") or 0)),
                "atk": round(float(block.get("atk") or 0)),
                "def": round(float(block.get("def") or 0)),
                "res": round(float(block.get("res") or 0)),
                "weight": round(float(block.get("weight") or 0)),
                "attack": "/".join(t for t in atk_tags if t) or "-",
            }
        )
    return rows


def class_labels(entry: dict) -> list[str]:
    labels = []
    for tag in entry.get("type") or []:
        name = _CLASS_NAMES.get(tag)
        if name:
            labels.append(name)
    return labels


async def enemy_icon_b64(enemy_key: str) -> str:
    """敌人头像（tomimi enemy_icons/{key}.webp，自下载 + 负缓存）。"""
    import base64

    enemy_key = (enemy_key or "").strip()
    if not enemy_key:
        return ""
    cache_dir = Path(IS_ENEMY_CACHE_DIR) / "icons"
    filename = f"{enemy_key}.webp"
    local_path = cache_dir / filename
    miss_path = cache_dir / f"{filename}.miss"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if miss_path.exists():
            if time.time() - miss_path.stat().st_mtime < _ICON_MISS_TTL:
                return ""
            miss_path.unlink()
        if not local_path.exists():
            async with httpx.AsyncClient(
                timeout=20.0, follow_redirects=True
            ) as client:
                resp = await client.get(f"{_ICON_BASE}/{filename}")
                resp.raise_for_status()
                local_path.write_bytes(resp.content)
        data = local_path.read_bytes()
        if not data:
            raise ValueError("empty icon")
        return f"data:image/webp;base64,{base64.b64encode(data).decode('utf-8')}"
    except Exception:
        local_path.unlink(missing_ok=True)
        try:
            miss_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        return ""
