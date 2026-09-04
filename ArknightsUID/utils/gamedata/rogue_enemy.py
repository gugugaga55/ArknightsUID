"""肉鸽专用敌人 buff 面板（tomimi.dev 开源数据）。

数据源（tanch-p/tomimi-dev 仓库，jsdelivr/raw 直链 + 30 天磁盘缓存）：
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

import asyncio
import time
from pathlib import Path
from typing import Any

from gsuid_core.logger import logger

from ..http import download_bytes, download_bytes_status
from ..disk_cache import cache_read, cache_write, is_fresh
from ._table_cache import GAMEDATA_CACHE_DIR

IS_ENEMY_CACHE_DIR = GAMEDATA_CACHE_DIR / "is_enemy"
# 全量缓存 TTL。肉鸽数据只随游戏版本更新（半年一更，历史期几乎不变），
# 全部落盘约 12MB（敌人库 1.4M + 技能词典 0.5M + 关卡 ~10M），30 天足够；
# 下载失败时以过期缓存兜底，只有完全无缓存才走负标记。
_DATA_TTL = 30 * 86400.0

# 数据直链（jsdelivr CDN：content-type 正确、快；raw 直链核心下载器会 5s 超时）
_TOMIMI_BASE = "https://cdn.jsdelivr.net/gh/tanch-p/tomimi-dev@main/src/lib/data"
_ICON_BASE = (
    "https://cdn.jsdelivr.net/gh/tanch-p/tomimi-dev@main/static/images/enemy_icons"
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

# 查询用的期别名（用户输入 → 期目录；含显示名、常用简称与「X肉鸽」叫法，
# 多数用户记不住主题全称，短名/肉鸽后缀都叫得出来）
THEME_ALIASES: dict[str, str] = {
    "傀影": "phantom",
    "猩红": "phantom",
    "傀影与猩红孤钻": "phantom",
    "傀影肉鸽": "phantom",
    "猩红肉鸽": "phantom",
    "水月": "mizuki",
    "深蓝": "mizuki",
    "水月与深蓝之树": "mizuki",
    "水月肉鸽": "mizuki",
    "萨米": "sami",
    "银凇": "sami",
    "萨米的腐朽安眠": "sami",
    "萨米肉鸽": "sami",
    "萨卡兹": "sarkaz",
    "无终奇语": "sarkaz",
    "萨卡兹的无终奇语": "sarkaz",
    "萨卡兹肉鸽": "sarkaz",
    "岁": "sui",
    "界园": "sui",
    "岁的界园志异": "sui",
    "界园肉鸽": "sui",
    "黑流": "black",
    "树海": "black",
    "沉沦者": "black",
    "沉沦者的黑流树海": "black",
    "黑流树海": "black",
    "树海肉鸽": "black",
    "黑流肉鸽": "black",
}

# enemy.type 标签 → 期目录（归属启发式；权威判定是难度效果里的显式 targets）
_TYPE_THEME_HINTS = {
    "sarkaz": "sarkaz",
    "seamonster": "mizuki",
    "collapsal": "sami",
    "mutant": "sami",
}


def resolve_theme_dir(name: str) -> str | None:
    """期名（内建别名 / 主题显示名 / 期目录本身）→ 期目录；不可解析返回 None。

    供用户自定义期别名命令校验目标用（ark肉鸽别名 X=<期名>）。
    """
    text = (name or "").strip()
    if not text:
        return None
    if text in THEME_DIRS:
        return text
    if text in THEME_ALIASES:
        return THEME_ALIASES[text]
    for theme_dir, display in THEME_DIRS.items():
        if text == display:
            return theme_dir
    return None


# 面板数值字段（参与难度折算表）；aspd/ms/range 等次级属性走形态行 extras
PANEL_STATS = ("hp", "atk", "def", "res")
STAT_LABELS = {"hp": "HP", "atk": "攻", "def": "防", "res": "抗"}

# status_immune 键 → 中文（tomimi 用游戏异常状态键；顺序即展示顺序）
_STATUS_LABELS = {
    "stun": "眩晕",
    "silence": "沉默",
    "sleep": "沉睡",
    "freeze": "冻结",
    "levitate": "浮空",
    "tremble": "战栗",
    "fear": "恐惧",
    "palsy": "麻痹",
    "attract": "诱导",
    "sluggish": "迟缓",
}

# 非面板难度效果键 → 中文标签（hp/atk/def/res 折算进数值列，不进 note）
_MOD_LABELS = {
    "hp": "生命",
    "atk": "攻击",
    "def": "防御",
    "res": "法抗",
    "ms": "移速",
    "aspd": "攻速",
    "dmg_res": "受到伤害",
}


def _fmt_num(value: Any) -> str:
    """数值展示：整数去小数点，浮数保留至多两位。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num.is_integer():
        return str(int(num))
    return f"{num:g}"


def _mod_note(mod: dict) -> str:
    """非面板难度效果 → 人读 note（原样键名如 dmg_res 0.1 不可读）。"""
    key = str(mod.get("key") or "")
    if key == "level_hidden_group_enable":
        extra = mod.get("valueStr") or mod.get("value")
        return f"开启隐藏强化（{extra}）" if extra else "开启隐藏强化"
    label = _MOD_LABELS.get(key, key)
    value = mod.get("value")
    mode = mod.get("mode")
    if mode == "mul":
        return f"{label}×{_fmt_num(value)}"
    if mode == "add":
        value_text = _fmt_num(value)
        return (
            f"{label}+{value_text}"
            if float(value or 0) >= 0
            else f"{label}{value_text}"
        )
    return f"{label} {_fmt_num(value)}"


def status_labels(entry: dict) -> list[str]:
    """各形态 status_immune 并集 → 中文联免疫标签（按固定顺序）。"""
    keys: set[str] = set()
    for form in entry.get("forms") or []:
        keys.update(str(s) for s in (form.get("status_immune") or []))
    order = {k: i for i, k in enumerate(_STATUS_LABELS)}
    ordered = sorted(keys, key=lambda k: (order.get(k, len(order)), k))
    return [_STATUS_LABELS.get(k, k) for k in ordered]


async def load_skill_dict() -> dict[str, list[str]]:
    """enemy_skills.json（974 键）→ {技能键: 中文描述行列表}。

    词典覆盖率 ~98.6%（db 用到的 954 键命中 941）；未命中键在翻译层跳过。
    """
    try:
        data = await _fetch_json_cached("enemy_skills.json")
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, value in data.items():
        lines = ((value or {}).get("tooltip") or {}).get("zh")
        if isinstance(lines, list) and lines:
            result[str(key)] = [str(x) for x in lines if x]
    return result


def _translate_trait(trait: dict, skill_dict: dict[str, list[str]]) -> list[str]:
    """单个 trait/special 条目 → 中文描述行（占位符 <v.value> 用条目数值填充）。

    无法填充的占位行（引用不存在字段，如 <v.standby>）丢弃；词典未收的键
    跳过（原始键名对用户无信息量）。
    """
    lines = skill_dict.get(str(trait.get("key") or ""))
    if not lines:
        return []
    value = trait.get("value")
    out: list[str] = []
    for line in lines:
        text = str(line)
        if value is not None:
            text = text.replace("<v.value>", _fmt_num(value))
        if "<v." in text:
            continue
        if text and text not in out:
            out.append(text)
    return out


def max_difficulty(grades: list[dict]) -> int:
    """该期难度表的最大档位（查询难度截断用；空表返回 0）。"""
    return max((int(g.get("difficulty", 0)) for g in grades), default=0)


def _floor_buff_note(grades: list[dict], difficulty: int) -> str:
    """≤ difficulty 档的最大每层增益 → 展示文案（层数未知，不折算）。

    两种数据形态：数字（萨米/萨卡兹 +N%）与 {atk, hp} 逐层乘数
    （水月/岁/黑流，×1.01 即每层+1%）。
    """
    floor_buff: float | dict | None = None
    for grade in grades:
        if int(grade.get("difficulty", 0)) <= difficulty:
            raw = grade.get("floorBuff")
            # 取数值更大的档（档位递增时增益不降，dict 时按 atk 比较）
            if not raw:
                continue
            if floor_buff is None or _buff_size(raw) > _buff_size(floor_buff):
                floor_buff = raw
    if not floor_buff:
        return ""
    if isinstance(floor_buff, dict):
        atk = float(floor_buff.get("atk") or 1.0)
        hp = float(floor_buff.get("hp") or 1.0)
        if atk == hp:
            return f"每层生命/攻击×{atk:g}（未计入，按层复合）"
        return f"每层生命×{hp:g}·攻击×{atk:g}（未计入，按层复合）"
    value = float(floor_buff)
    return f"每层增益+{value * 100:g}%（未计入，按层复合）"


def _buff_size(raw: Any) -> float:
    """floorBuff 两种形态的统一比较尺度（相对增量）。"""
    try:
        if isinstance(raw, dict):
            return max(float(raw.get(k) or 1.0) for k in ("atk", "hp")) - 1.0
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def build_panel_at(
    entry: dict,
    grades: list[dict],
    difficulty: int | None,
    *,
    base_block: dict | None = None,
    extra_effects: list | None = None,
    extra_label: str = "紧急",
) -> dict:
    """指定期+难度下的单行精确面板（累积口径与 build_diff_panels 一致）。

    与全档表不同：面板 key（生命/攻击/…）折算进数值的同时也进 buffs 列表，
    让"该难度下这条敌人吃到了什么"一目了然；floorBuff（每层增益）无法在
    查询时得知层数，以固定文案进 buffs 提示。

    关卡限定查询：base_block 指定出场形态块（关卡 enemies[].level），
    extra_effects 附加该关紧急作战效果（buffs 以「{extra_label}：」前缀标注来源）。
    difficulty 为 None（关卡限定但未给难度）：不折算难度，label 为"基础"。
    """
    stats_blocks = entry.get("stats") or []
    base_block = base_block or (stats_blocks[0] if stats_blocks else {})
    enemy_key = str(entry.get("key") or "")
    enemy_types = list(entry.get("type") or [])
    if difficulty is None:
        mods: list[dict] = []
    else:
        mods = _grade_mods_for_enemy(grades, enemy_key, enemy_types, difficulty)
    extra_mods = _effects_mods_for(extra_effects, enemy_key, enemy_types)

    buffs: list[str] = []
    for mod in mods:
        note = _mod_note(mod)
        if note not in buffs:
            buffs.append(note)
    for mod in extra_mods:
        note = f"{extra_label}：{_mod_note(mod)}"
        if note not in buffs:
            buffs.append(note)

    stats = _stats_with_mods(base_block, mods + extra_mods, difficulty)
    noob = _NOOB_HP_CUT.get(difficulty) if difficulty is not None else None
    if noob:
        buffs.append(f"新手档生命×{noob}")
    # 去重面板 key 折算条目（多条同类 mod 保留每条明细，此处只去重文本）
    buffs = list(dict.fromkeys(buffs))
    if difficulty is not None:
        floor_note = _floor_buff_note(grades, difficulty)
        if floor_note:
            buffs.append(floor_note)
        label = f"难度{difficulty}"
    else:
        label = "基础"
    if noob:
        label += "·新手档"
    if extra_mods:
        label += f"·{extra_label}"
    return {"grade": difficulty, "label": label, "stats": stats, "buffs": buffs}


def form_mechanisms(entry: dict, skill_dict: dict[str, list[str]]) -> list[list[str]]:
    """各形态机制行（与 stats 块对齐）：traits + special 对角组。

    实测爱国者：stats[i].special 有 2 组（组=形态，[0]=行军姿态、[1]=毁灭姿态），
    形态 i 取 special[i]（越界回退最后一组）；traits 逐条平铺。
    """
    stats_blocks = entry.get("stats") or []
    result: list[list[str]] = []
    for i, block in enumerate(stats_blocks):
        lines: list[str] = []
        for trait in block.get("traits") or []:
            for text in _translate_trait(trait, skill_dict):
                if text not in lines:
                    lines.append(text)
        groups = block.get("special") or []
        group = groups[min(i, len(groups) - 1)] if groups else []
        for trait in group:
            for text in _translate_trait(trait, skill_dict):
                if text not in lines:
                    lines.append(text)
        result.append(lines)
    return result


# ---- 出没关卡索引（ro_stage_data：每关固定出怪表 + 紧急作战差异） ----

# 期目录 → tomimi stages/ro{N}.json 编号（游戏主题表编号，傀影起编）。
# 实测 2026-08 更新：ro1-6 均有出怪数据；ro5/6（岁/黑流）文件多一层
# {"name","data":[...]} 包装（_load_stage_file 已归一化）。
_THEME_ROGUE_NUM = {
    "phantom": 1,
    "mizuki": 2,
    "sami": 3,
    "sarkaz": 4,
    "sui": 5,
    "black": 6,
}
_STAGES_BASE = "https://cdn.jsdelivr.net/gh/tanch-p/tomimi-dev@main/src/lib/data/stages"
_STAGE_CACHE_DIR = Path(IS_ENEMY_CACHE_DIR) / "stages"
# 全量关卡文件并发上限（约 250 个文件 × ~40KB，首次构建拉满也要留余地）
_STAGE_FETCH_CONCURRENCY = 8

_stage_index_cache: dict[str, list[dict]] | None = None
_stage_index_lock: asyncio.Lock | None = None


def _floors_text(floors: list) -> str:
    """层数 [1,2] → "1-2层"；[2] → "2层"；乱序取 min-max。"""
    nums = sorted({int(f) for f in (floors or []) if str(f).isdigit()})
    if not nums:
        return ""
    return f"{nums[0]}-{nums[-1]}层" if len(nums) > 1 else f"{nums[0]}层"


async def _load_stage_meta(theme_dir: str) -> dict[str, dict]:
    """ro{N}.json → {levelId: {code, name_zh}}；失败返回 {}。"""
    num = _THEME_ROGUE_NUM.get(theme_dir)
    if not num:
        return {}
    data = await _fetch_url_json_cached(
        f"{_STAGES_BASE}/ro{num}.json", _STAGE_CACHE_DIR / f"meta_{theme_dir}.json"
    )
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        level_id = value.get("levelId") or key
        if isinstance(level_id, str):
            result[level_id] = {
                "code": str(value.get("code") or ""),
                "name": str(value.get("name_zh") or ""),
            }
    return result


async def _load_stage_file(level_id: str) -> dict:
    """单关出怪表（enemies/floors/数量）；失败或缺数据返回 {}。

    ro5/ro6（岁/黑流）文件多一层包装：{"name", "data": [<关卡 dict>]}，
    归一化为关卡 dict 本身（data 恒为单元素，变体关是独立 level_id 文件）。

    levelId 来自外部表（可能被污染）：先过 safe_cache_stem，URL 与缓存
    路径用同一安全名，杜绝 "../" 逃出缓存目录。
    """
    from .path_safety import resolve_inside, safe_cache_stem

    stem = safe_cache_stem(level_id)
    data = await _fetch_url_json_cached(
        f"{_STAGES_BASE}/ro_stage_data/{stem}.json",
        resolve_inside(_STAGE_CACHE_DIR, f"{stem}.json"),
    )
    if isinstance(data, dict) and not data.get("enemies"):
        inner = data.get("data")
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            return inner[0]
    return data if isinstance(data, dict) else {}


async def load_stage_data(level_id: str) -> dict:
    """关卡完整数据（命令层用公开入口；内部 _load_stage_file 同实现）。"""
    return await _load_stage_file(level_id)


async def _do_build_stage_index() -> dict[str, list[dict]]:
    """全期关卡 → {敌人键: [关卡条目]}。空壳期/单关失败静默跳过。"""
    index: dict[str, list[dict]] = {}
    semaphore = asyncio.Semaphore(_STAGE_FETCH_CONCURRENCY)

    async def _one(theme_dir: str, level_id: str, meta: dict) -> None:
        async with semaphore:
            stage = await _load_stage_file(level_id)
        enemies = stage.get("enemies")
        if not isinstance(enemies, list) or not enemies:
            return
        floors = _floors_text(stage.get("floors"))
        for enemy in enemies:
            if not isinstance(enemy, dict):
                continue
            enemy_id = str(enemy.get("id") or "")
            if not enemy_id:
                continue
            level = int(enemy.get("level") or 0)
            elite_extra = int(enemy.get("elite_max_count") or 0) > int(
                enemy.get("max_count") or 0
            )
            index.setdefault(enemy_id, []).append(
                {
                    "theme": theme_dir,
                    "code": meta.get("code") or "",
                    "name": meta.get("name") or level_id,
                    "floors": floors,
                    "level": level,
                    "elite": elite_extra,
                }
            )

    metas = await asyncio.gather(*(_load_stage_meta(t) for t in _THEME_ROGUE_NUM))
    tasks = [
        _one(theme_dir, level_id, meta)
        for theme_dir, meta in zip(_THEME_ROGUE_NUM, metas)
        for level_id, meta in meta.items()
    ]
    if tasks:
        await asyncio.gather(*tasks)
    return index


async def build_stage_index() -> dict[str, list[dict]]:
    """出没索引（进程内 memo + 单飞锁；构建失败返回 {} 不报错）。

    首次构建拉全部关卡文件（约 250 个有效文件，并发 8）；单文件与
    meta 均带 30 天磁盘缓存；下载失败过期兜底，404 走 7 天负标记。
    """
    global _stage_index_cache, _stage_index_lock
    if _stage_index_cache is not None:
        return _stage_index_cache
    if _stage_index_lock is None:
        _stage_index_lock = asyncio.Lock()
    async with _stage_index_lock:
        if _stage_index_cache is not None:
            return _stage_index_cache
        try:
            index = await _do_build_stage_index()
        except Exception:
            index = {}
        if index:
            _stage_index_cache = index
        return index


def stages_for_enemy(
    index: dict[str, list[dict]], enemy_key: str, per_theme_limit: int = 6
) -> list[dict]:
    """敌人键 → 按期分组的出没关卡（每期截断展示，带 total）。

    返回 [{theme, theme_name, items: ["拆东补西·ISW-NO·1-2层", ...], total}]，
    items 内注记：形态N（level>0）/ 紧急+（紧急作战额外出怪）。
    """
    entries = index.get(enemy_key) or []
    if not entries:
        return []
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["theme"], []).append(entry)
    result = []
    for theme_dir, rows in grouped.items():
        # 同关只出现一次的常规排序：层数原文短的优先（更泛用），名称稳定排序
        def _sort_key(row: dict) -> tuple:
            return (row["name"], row["code"])

        rows.sort(key=_sort_key)
        items = []
        for row in rows:
            text = "·".join(
                part
                for part in (
                    row["name"],
                    row["code"],
                    row["floors"],
                    f"形态{row['level'] + 1}" if row["level"] else "",
                    "紧急+" if row["elite"] else "",
                )
                if part
            )
            items.append(text)
        result.append(
            {
                "theme": theme_dir,
                "theme_name": THEME_DIRS.get(theme_dir, theme_dir),
                "stages": items[:per_theme_limit],
                "total": len(items),
            }
        )
    result.sort(key=lambda g: -g["total"])
    return result


async def find_stage(name: str) -> dict | None:
    """关卡名 → {theme, level_id, code, name, variants}；精确匹配。

    ro5/6 存在同期同名分支变体（level_id 带 -b/-c/_dlc1 后缀，实测
    ro5 有 17 个重名关），此时返回字典序第一个变体，variants 给出
    同名变体总数（调用方可提示用户）。找不到返回 None（再走
    search_stages 模糊候选）。
    """
    metas = await asyncio.gather(*(_load_stage_meta(t) for t in _THEME_ROGUE_NUM))
    for theme_dir, meta in zip(_THEME_ROGUE_NUM, metas):
        hits = [(level_id, m) for level_id, m in meta.items() if m.get("name") == name]
        if hits:
            level_id, m = sorted(hits)[0]
            return {
                "theme": theme_dir,
                "level_id": level_id,
                "code": m.get("code") or "",
                "name": name,
                "variants": len(hits),
            }
    return None


async def search_stages(query: str, limit: int = 8) -> list[dict]:
    """关卡名模糊匹配：唯一前缀/子串 → 候选列表（与敌人匹配同策略）。"""
    q = (query or "").strip()
    if not q:
        return []
    candidates: list[dict] = []

    def _collect(pred) -> list[dict]:
        hits: list[dict] = []
        for theme_dir, meta in zip(_THEME_ROGUE_NUM, all_metas):
            for level_id, m in meta.items():
                name = m.get("name") or ""
                if name and pred(name):
                    hits.append(
                        {
                            "theme": theme_dir,
                            "level_id": level_id,
                            "code": m.get("code") or "",
                            "name": name,
                        }
                    )
        return hits

    all_metas = await asyncio.gather(*(_load_stage_meta(t) for t in _THEME_ROGUE_NUM))
    for pred in (lambda n: n == q, lambda n: n.startswith(q), lambda n: q in n):
        candidates = _collect(pred)
        if len(candidates) == 1:
            return candidates
        if candidates:
            break
    return candidates[:limit]


def stage_panel_rows(
    stage: dict,
    db: dict[str, dict],
    grades: list[dict],
    difficulty: int | None,
    elite: bool = False,
) -> list[dict]:
    """关卡敌人行（含难度/紧急折算后的面板）；库外条目（trap 等）跳过。

    difficulty 为 None 时给基础面板（不折算难度）；elite 时叠加该关
    紧急作战效果。count：min==max 且 >0 → 固定数，0 → 波次出怪，
    紧急数量差 → 紧急+N 注记。
    """
    rows: list[dict] = []
    for item in stage.get("enemies") or []:
        key = str(item.get("id") or "")
        entry = db.get(key)
        if not entry:
            continue
        blocks = entry.get("stats") or []
        if not blocks:
            continue
        level = int(item.get("level") or 0)
        block = blocks[min(level, len(blocks) - 1)]
        types = list(entry.get("type") or [])
        mods: list[dict] = []
        if difficulty is not None:
            mods.extend(_grade_mods_for_enemy(grades, key, types, difficulty))
        elite_mods: list[dict] = []
        if elite:
            elite_mods = _effects_mods_for(stage.get("elite_mods"), key, types)
        stats: dict[str, int]
        if difficulty is not None or elite_mods:
            stats = _stats_with_mods(
                block,
                mods + elite_mods,
                difficulty if difficulty is not None else 99,
            )
        else:
            stats = {stat: round(float(block.get(stat) or 0)) for stat in PANEL_STATS}
        lo, hi = int(item.get("min_count") or 0), int(item.get("max_count") or 0)
        if lo == hi and hi > 0:
            count = str(hi)
        elif hi == 0:
            count = "波次"
        else:
            count = f"{lo}-{hi}"
        elite_hi = int(item.get("elite_max_count") or 0)
        extra_note = ""
        if elite_hi > hi:
            extra_note = f"紧急+{elite_hi - hi}"
        rows.append(
            {
                "key": key,
                "name": str(entry.get("name_zh") or key),
                "count": count,
                "stats": stats,
                "level": level,
                "elite_note": extra_note,
                "has_diff": bool(mods or elite_mods),
            }
        )
    return rows


_ATK_TYPE_NAMES = {
    "melee": "近战",
    "ranged": "远程",
    "phys": "物理",
    "arts": "法术",
    "real": "真实",
    # 线上数据实际值域（真实伤害有 true/raw 两种键、无 real）：
    # true×5 / raw×29 / no_attack×248 / heal×2，缺映射会裸显英文
    "true": "真实",
    "raw": "真实",
    "no_attack": "不攻击",
    "heal": "治疗",
}
_CLASS_NAMES = {"BOSS": "领袖", "ELITE": "精英", "NORMAL": "普通"}

# 新手档（难度 0/1/2）敌人 HP 削减（tomimi noobHelp 同源常量）
_NOOB_HP_CUT = {0: 0.8, 1: 0.85, 2: 0.9}


async def _fetch_json_cached(filename: str) -> Any:
    """enemy/ 根下的数据文件 → 本地缓存（30 天 TTL，统一缓存层 + 共享下载）。

    缓存改用 {"_cache_time", "payload"} 包装格式（旧 mtime 裸文件最多
    30 天内多下载一次，自然淘汰）。文件 I/O 走 to_thread 不阻塞事件循环。
    """
    path = Path(IS_ENEMY_CACHE_DIR) / filename
    return await _fetch_url_json_cached(f"{_TOMIMI_BASE}/enemy/{filename}", path)


# 负标记 TTL：404（上游未收录，短期不会恢复）7 天；
# 网络故障 1 小时（防止首次使用的瞬时抖动把敌人库/技能词典瘫痪一周）。
# 标记文件内容写 "404"/"net" 区分两类；历史空标记按 net 处理（1 小时自愈）。
_STAGE_MISS_TTL = 7 * 86400.0
_NET_MISS_TTL = 3600.0


def _miss_ttl(miss_path: Path) -> float:
    try:
        kind = miss_path.read_text(encoding="utf-8").strip()
    except OSError:
        kind = ""
    return _STAGE_MISS_TTL if kind == "404" else _NET_MISS_TTL


async def _fetch_url_json_cached(url: str, path: Path) -> Any:
    """任意 tomimi 直链 JSON → 本地缓存（30 天 TTL；失败过期兜底 + 负标记）。"""
    import json

    payload, saved_ts = await cache_read(path)
    if payload is not None and is_fresh(saved_ts, _DATA_TTL):
        return payload
    miss_path = path.with_suffix(path.suffix + ".miss")
    if payload is None and miss_path.exists():
        try:
            if time.time() - miss_path.stat().st_mtime < _miss_ttl(miss_path):
                return None
            miss_path.unlink()
        except OSError:
            return None
    status, raw = await download_bytes_status(url, timeout=20.0)
    if raw is None:
        if payload is not None:
            # 网络抖动/CDN 故障：宁可给过期数据，不写负标记（恢复即自愈）
            return payload
        try:
            miss_path.write_text("404" if status == 404 else "net", encoding="utf-8")
        except OSError:
            pass
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return payload
    await cache_write(path, data)
    return data


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

    async def fetch_one(theme_dir: str) -> tuple[str, list | None]:
        path = Path(IS_ENEMY_CACHE_DIR) / f"difficulty_{theme_dir}.json"
        try:
            payload, saved_ts = await cache_read(path)
            if payload is None or not is_fresh(saved_ts, _DATA_TTL):
                raw = await download_bytes(_diff_file_url(theme_dir), timeout=20.0)
                if raw is not None:
                    # CDN 200 返回 HTML/截断体时 json.loads 抛异常：
                    # 保留旧（过期）缓存兜底，不让单期效果整段丢失
                    try:
                        payload = json.loads(raw)
                    except Exception as exc:
                        logger.warning(
                            f"[ArknightsUID·肉鸽] {theme_dir} 难度表响应非 JSON "
                            f"({exc.__class__.__name__})，沿用旧缓存"
                        )
                    else:
                        await cache_write(path, payload)
            return theme_dir, payload if isinstance(payload, list) else None
        except Exception:
            return theme_dir, None

    # 六期难度表互不依赖，并发拉取（此前串行 6 × 20s）
    result: dict[str, list] = {}
    for theme_dir, data in await asyncio.gather(*(fetch_one(t) for t in THEME_DIRS)):
        if data is not None:
            result[theme_dir] = data
    return result


def match_rogue_enemies(query: str, db: dict[str, dict], limit: int = 8) -> list[str]:
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
    """tomimi getModdedStat 同源语义：mods 累积折算单个属性。

    - mul + initial：乘数**增量**累加（init_mul = 1 + Σvalue，tomimi 为
      `initialMul += value`，不是连乘）
    - mul + final：乘数连乘
    - add + initial：基底平加；add + final：initial 段之后平加
    - 结果 = (base + init_add) * init_mul + final_add) * final_mul

    此前只处理 mul（add 全部丢弃），水月 N3+ 法抗 +10 等修正被漏算。
    """
    init_add = 0.0
    init_mul = 1.0
    final_add = 0.0
    final_mul = 1.0
    for mod in mods:
        mode = mod.get("mode")
        if mode not in ("mul", "add"):
            continue
        try:
            value = float(mod.get("value") or 0)
        except (TypeError, ValueError):
            continue
        initial = mod.get("order") == "initial"
        if mode == "mul":
            if initial:
                init_mul += value
            else:
                final_mul *= value
        else:
            if initial:
                init_add += value
            else:
                final_add += value
    return ((base + init_add) * init_mul + final_add) * final_mul


def _effects_mods_for(
    effects: list, enemy_key: str, enemy_types: list[str]
) -> list[dict]:
    """单组效果（targets + mods）中命中该敌人的 mods；与难度档同构。"""
    type_set = set(enemy_types or [])
    mods: list[dict] = []
    for effect in effects or []:
        if not isinstance(effect, dict):
            continue
        targets = set(effect.get("targets") or [])
        if enemy_key in targets or (targets & type_set) or "ALL" in targets:
            mods.extend(effect.get("mods") or [])
    return mods


def _grade_mods_for_enemy(
    grades: list[dict], enemy_key: str, enemy_types: list[str], upto: int
) -> list[dict]:
    """≤ upto 档中命中该敌人的全部 mods（累积口径）。"""
    mods: list[dict] = []
    for grade in grades:
        if int(grade.get("difficulty", 0)) > upto:
            continue
        mods.extend(_effects_mods_for(grade.get("effects"), enemy_key, enemy_types))
    return mods


def _stats_with_mods(
    base_block: dict, mods: list[dict], difficulty: int
) -> dict[str, int]:
    """面板四维折算：mods 累积 + 新手档（难度 0/1/2）HP 削减。"""
    stat_mods: dict[str, list[dict]] = {}
    for mod in mods:
        key = str(mod.get("key") or "")
        if key in PANEL_STATS:
            stat_mods.setdefault(key, []).append(mod)
    noob = _NOOB_HP_CUT.get(difficulty)
    stats: dict[str, int] = {}
    for stat_key in PANEL_STATS:
        base = float(base_block.get(stat_key) or 0)
        value = _mods_apply(base, stat_mods.get(stat_key, []))
        if stat_key in ("res", "weight"):
            # tomimi calculateModdedStat 对 res/weight 钳 [0,100]
            value = min(100.0, max(0.0, value))
        if noob and stat_key == "hp":
            value *= noob
        stats[stat_key] = round(value)
    return stats


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
        tail = [g for g in row_grades if g > 2][-(max_rows - len(head)) :]
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
                notes.append(_mod_note(mod))
        for stat_key in PANEL_STATS:
            base = float(base_block.get(stat_key) or 0)
            value = _mods_apply(base, stat_mods.get(stat_key, []))
            if stat_key in ("res", "weight"):
                # tomimi calculateModdedStat 对 res/weight 钳 [0,100]
                # （与 _stats_with_mods 同口径）
                value = min(100.0, max(0.0, value))
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


def _form_extras(block: dict) -> list[str]:
    """形态次级属性段：移速/攻击间隔/范围/进点代价/元素抗性（默认值不展示）。

    tomimi 口径：aspd = 攻击间隔秒（与 PRTS 攻击间隔同值）；range 仅远程 >0；
    lifepoint 默认 1（进入保护目标点扣除的生命值）。
    """
    extras: list[str] = []
    if block.get("ms"):
        extras.append(f"移速 {_fmt_num(block['ms'])}")
    if block.get("aspd"):
        extras.append(f"间隔 {_fmt_num(block['aspd'])}s")
    if block.get("range"):
        extras.append(f"范围 {_fmt_num(block['range'])}")
    lifepoint = block.get("lifepoint")
    if lifepoint == 0:
        extras.append("进点不扣生命")
    elif lifepoint is not None and lifepoint != 1:
        extras.append(f"进点-{_fmt_num(lifepoint)}")
    if block.get("epResistance"):
        extras.append(f"元素抗性 {_fmt_num(block['epResistance'])}")
    if block.get("epDamageResistance"):
        extras.append(f"损伤抵抗 {_fmt_num(block['epDamageResistance'])}")
    return extras


def form_context(entry: dict) -> list[dict]:
    """形态行：各形态基础面板 + 攻击方式 + 次级属性段（atk_type 映射中文）。"""
    stats_blocks = entry.get("stats") or []
    forms = entry.get("forms") or []
    rows: list[dict] = []
    for i, block in enumerate(stats_blocks):
        form = forms[i] if i < len(forms) else {}
        attack = form.get("normal_attack") or {}
        atk_tags = [_ATK_TYPE_NAMES.get(t, t) for t in (attack.get("atk_type") or [])]
        attack_text = "/".join(t for t in atk_tags if t) or "-"
        hits = int(attack.get("hits") or 1)
        if hits > 1:
            attack_text += f"×{hits}段"
        rows.append(
            {
                "title": form.get("title") or f"形态{i + 1}",
                "hp": round(float(block.get("hp") or 0)),
                "atk": round(float(block.get("atk") or 0)),
                "def": round(float(block.get("def") or 0)),
                "res": round(float(block.get("res") or 0)),
                "weight": round(float(block.get("weight") or 0)),
                "attack": attack_text,
                "extras": _form_extras(block),
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
    """敌人头像（tomimi enemy_icons/{key}.webp，自下载 + 负缓存）。

    key 可能来自远端 Wiki 页名：过 safe_cache_stem 清洗后再拼 URL/路径，
    防数据源污染时的路径逃逸（正常 ascii key 文件名不变，缓存兼容）。
    """
    import base64

    from .path_safety import safe_cache_stem

    enemy_key = (enemy_key or "").strip()
    if not enemy_key:
        return ""
    stem = safe_cache_stem(enemy_key)
    cache_dir = Path(IS_ENEMY_CACHE_DIR) / "icons"
    filename = f"{stem}.webp"
    local_path = cache_dir / filename
    miss_path = cache_dir / f"{filename}.miss"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if miss_path.exists():
            if time.time() - miss_path.stat().st_mtime < _ICON_MISS_TTL:
                return ""
            miss_path.unlink()
        if not local_path.exists():
            raw = await download_bytes(f"{_ICON_BASE}/{filename}", timeout=20.0)
            if raw is None:
                raise RuntimeError(f"icon download failed: {filename}")
            local_path.write_bytes(raw)
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
