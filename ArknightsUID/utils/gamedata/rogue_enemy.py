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
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gsuid_core.logger import logger
from gsuid_core.server import on_core_shutdown

from ..http import (
    download_bytes,
    download_bytes_conditional,
    download_bytes_status,
)
from ..disk_cache import (
    cache_read,
    cache_read_validators,
    cache_write,
    is_fresh,
)
from ._table_cache import GAMEDATA_CACHE_DIR
# 期表数据收敛至 rogue_registry（唯一事实源）；此处按原名再导出，
# rogue_ending 与既有测试的导入路径不破坏
from .rogue_registry import (  # noqa: F401
    THEME_ALIASES,
    THEME_DIRS,
    diff_file_for,
)

IS_ENEMY_CACHE_DIR = GAMEDATA_CACHE_DIR / "is_enemy"
# 复核间隔（非数据寿命）：敌人库/技能词典内容近永久，只在版本更新时
# 变化——间隔到点走条件 GET diff，304 时零下载零解析仅续期，故比旧
# TTL（30 天）激进；新期敌人最长 1 天即可见。下载失败以过期缓存兜底，
# 只有完全无缓存才走负标记。
_DATA_TTL = 86400.0

# 数据直链（jsdelivr CDN：content-type 正确、快；raw 直链核心下载器会 5s 超时）
_TOMIMI_BASE = "https://cdn.jsdelivr.net/gh/tanch-p/tomimi-dev@main/src/lib/data"
_TOMIMI_RAW_BASE = (
    "https://raw.githubusercontent.com/tanch-p/tomimi-dev/main/src/lib/data"
)
_ICON_BASE = (
    "https://cdn.jsdelivr.net/gh/tanch-p/tomimi-dev@main/static/images/enemy_icons"
)
_ICON_MISS_TTL = 3600.0

# 期目录 → 显示名 / 查询别名：见 rogue_registry.THEME_DIRS / THEME_ALIASES
# （上方按原名导入；新一期在注册表加一行即全链路生效）

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


def _floor_buff_mods(grades: list[dict], difficulty: int | None, floor: int) -> list[dict]:
    """每层增益折算为 mods（tomimi floorDifficultyMods 同源）。

    官方难度模型里敌人属性随难度缩放的主要来源：标量 floorBuff（萨米/
    萨卡兹「+N%」）→ ×(1+N)^floor；{atk,hp} 逐层乘数（水月/岁/黑流）
    → ×乘数^floor。层号从 1 起（第 1 层即 1 次方）。无增益/难度缺省/
    floor≤0 返回 []（保持基础面板口径）。
    """
    if difficulty is None or not floor or floor < 0:
        return []
    floor_buff: float | dict | None = None
    for grade in grades:
        if int(grade.get("difficulty", 0)) > difficulty:
            continue
        raw = grade.get("floorBuff")
        if raw and (floor_buff is None or _buff_size(raw) > _buff_size(floor_buff)):
            floor_buff = raw
    if not floor_buff:
        return []
    if isinstance(floor_buff, dict):
        atk = float(floor_buff.get("atk") or 1.0) ** floor
        hp = float(floor_buff.get("hp") or 1.0) ** floor
    else:
        atk = hp = (1.0 + float(floor_buff)) ** floor
    mods: list[dict] = []
    if hp != 1.0:
        mods.append({"key": "hp", "value": hp, "mode": "mul"})
    if atk != 1.0:
        mods.append({"key": "atk", "value": atk, "mode": "mul"})
    return mods


def _floor_buff_desc(grades: list[dict], difficulty: int) -> str:
    """≤ difficulty 档的最大每层增益 → 单层增益描述（不含折算状态）。"""
    floor_buff: float | dict | None = None
    for grade in grades:
        if int(grade.get("difficulty", 0)) <= difficulty:
            raw = grade.get("floorBuff")
            if raw and (floor_buff is None or _buff_size(raw) > _buff_size(floor_buff)):
                floor_buff = raw
    if not floor_buff:
        return ""
    if isinstance(floor_buff, dict):
        atk = float(floor_buff.get("atk") or 1.0)
        hp = float(floor_buff.get("hp") or 1.0)
        if atk == hp:
            return f"每层生命/攻击×{atk:g}"
        return f"每层生命×{hp:g}·攻击×{atk:g}"
    return f"每层增益+{float(floor_buff) * 100:g}%"


def _floor_buff_note(grades: list[dict], difficulty: int, applied_floor: int = 0) -> str:
    """每层增益展示文案；applied_floor>0 表示数值已按该层折算。"""
    desc = _floor_buff_desc(grades, difficulty)
    if not desc:
        return ""
    if applied_floor > 0:
        return f"{desc}，已按第{applied_floor}层折算"
    return f"{desc}（未计入，按层复合）"


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
    extra_effects: list[dict] | None = None,
    extra_label: str = "紧急",
    floor: int = 0,
) -> dict:
    """指定期+难度下的单行精确面板（累积口径与 build_diff_panels 一致）。

    与全档表不同：面板 key（生命/攻击/…）折算进数值的同时也进 buffs 列表，
    让"该难度下这条敌人吃到了什么"一目了然。floor 为折算层号（敌人卡
    无关卡上下文，按 tomimi 惯例传 1；关卡卡传该关出场形态块所在层，
    跨层关取最深层）；缺省 0 = 层数增益不折算（buffs 里保留提示文案）。

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
        mods.extend(_floor_buff_mods(grades, difficulty, floor))
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
        floor_note = _floor_buff_note(grades, difficulty, applied_floor=floor)
        if floor_note:
            buffs.append(floor_note)
        label = f"难度{difficulty}"
        if floor > 0 and _floor_buff_mods(grades, difficulty, floor):
            label += f"·第{floor}层"
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
_STAGE_INDEX_TTL = 86400.0
_STAGE_INDEX_RETRY_SECONDS = 30.0
_stage_index_generation = 0


@dataclass
class _StageIndexState:
    data: dict[str, list[dict]] = field(default_factory=dict)
    status: str = "loading"
    saved_at: float = 0.0
    task: asyncio.Task[None] | None = None


_stage_index_states: dict[str | None, _StageIndexState] = {}


class _IncompleteStageIndex(Exception):
    def __init__(self, data: dict[str, list[dict]], failures: int) -> None:
        super().__init__(f"{failures} stage sources unavailable")
        self.data = data
        self.failures = failures


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
    relative_path = f"{stem}.json"
    legacy_path = resolve_inside(_STAGE_CACHE_DIR, relative_path)
    legacy_data, legacy_ts = None, 0.0
    # tomimi 将各期关卡移入 ro{N}/；新缓存键隔离旧 URL 的 404 负标记。
    # 未知格式/主题仍用旧路径，避免猜测第三方尚未注册的目录布局。
    theme_match = re.match(r"^level_rogue(\d+)_", stem)
    if (
        theme_match
        and (number := int(theme_match.group(1))) in _THEME_ROGUE_NUM.values()
    ):
        relative_path = f"ro{number}/{relative_path}"
        if not resolve_inside(_STAGE_CACHE_DIR, relative_path).exists():
            legacy_data, legacy_ts = await cache_read(legacy_path)
    if legacy_data is not None and is_fresh(legacy_ts, _DATA_TTL):
        # 保留旧有效数据的原 TTL，避免迁移续鲜；旧源 ETag 不用于新 URL。
        data = legacy_data
    else:
        path = resolve_inside(_STAGE_CACHE_DIR, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = await _fetch_url_json_cached(
            f"{_STAGES_BASE}/ro_stage_data/{relative_path}", path
        )
        if data is None:
            data = legacy_data
    if isinstance(data, dict) and not data.get("enemies"):
        inner = data.get("data")
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            return inner[0]
    return data if isinstance(data, dict) else {}


async def load_stage_data(level_id: str) -> dict:
    """关卡完整数据（命令层用公开入口；内部 _load_stage_file 同实现）。"""
    return await _load_stage_file(level_id)


async def _do_build_stage_index(theme_dir: str | None = None) -> dict[str, list[dict]]:
    """所选期关卡 → 出没索引；源缺失抛出携带可用部分的异常。"""
    themes = tuple(_THEME_ROGUE_NUM) if theme_dir is None else (theme_dir,)
    if any(theme not in _THEME_ROGUE_NUM for theme in themes):
        raise ValueError("unknown rogue theme")
    index: dict[str, list[dict]] = {}
    semaphore = asyncio.Semaphore(_STAGE_FETCH_CONCURRENCY)

    async def _one(theme_dir: str, level_id: str, meta: dict) -> None:
        async with semaphore:
            stage = await _load_stage_file(level_id)
        enemies = stage.get("enemies")
        if not isinstance(enemies, list):
            raise ValueError("stage enemy roster unavailable")
        if not enemies:
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

    metas = await asyncio.gather(
        *(_load_stage_meta(t) for t in themes), return_exceptions=True
    )
    failures = sum(not isinstance(meta, dict) or not meta for meta in metas)
    tasks = [
        _one(theme_dir, level_id, meta)
        for theme_dir, meta in zip(themes, metas)
        if isinstance(meta, dict)
        for level_id, meta in meta.items()
    ]
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures += sum(isinstance(result, BaseException) for result in results)
    if failures:
        raise _IncompleteStageIndex(index, failures)
    return index


async def _run_stage_index(
    theme_dir: str | None, state: _StageIndexState, generation: int
) -> None:
    global _stage_index_cache

    data, status = state.data, "failed"
    try:
        # 无参入口兼容既有完整构建调用与测试替身。
        data = (
            await _do_build_stage_index()
            if theme_dir is None
            else await _do_build_stage_index(theme_dir)
        )
        status = "ready"
    except _IncompleteStageIndex as exc:
        data = exc.data
        logger.warning(
            f"[ArknightsUID·肉鸽] 出没索引构建不完整 "
            f"({theme_dir or 'all'}，{exc.failures} 个源不可用)"
        )
    except Exception as exc:
        # 后台异常必须在任务内部消费，避免无人等待时丢失异常。
        logger.warning(
            f"[ArknightsUID·肉鸽] 出没索引构建失败 "
            f"({theme_dir or 'all'}，{exc.__class__.__name__})"
        )
    if (
        generation != _stage_index_generation
        or _stage_index_states.get(theme_dir) is not state
    ):
        return
    state.data, state.status, state.saved_at = data, status, time.monotonic()
    if theme_dir is None:
        _stage_index_cache = data


def _ensure_stage_index(theme_dir: str | None) -> _StageIndexState:
    state = _stage_index_states.get(theme_dir)
    if state is not None:
        if state.task is not None and not state.task.done():
            return state
        # 保留旧测试/调用方将无参缓存置 None 的失效约定。
        invalidated = theme_dir is None and _stage_index_cache is None
        ttl = (
            _STAGE_INDEX_TTL if state.status == "ready" else _STAGE_INDEX_RETRY_SECONDS
        )
        if not invalidated and time.monotonic() - state.saved_at < ttl:
            return state
    state = _StageIndexState(data=state.data if state is not None else {})
    _stage_index_states[theme_dir] = state
    state.task = asyncio.create_task(
        _run_stage_index(theme_dir, state, _stage_index_generation),
        name=f"ark-stage-index-{theme_dir or 'all'}",
    )
    return state


async def _wait_stage_index(
    theme_dir: str | None, timeout: float | None
) -> tuple[dict, str]:
    state = _ensure_stage_index(theme_dir)
    if state.task is not None and not state.task.done():
        try:
            # 调用方超时/取消只停止等待，不会取消其他查询共享的后台构建。
            await asyncio.wait_for(asyncio.shield(state.task), timeout=timeout)
        except TimeoutError:
            return state.data, state.status
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            return {}, "failed"  # 刷新取消了被等待的构建。
    if _stage_index_states.get(theme_dir) is not state:
        return {}, "failed"
    return state.data, state.status


async def load_stage_appearances(
    theme_dir: str | None = None, timeout: float = 1.0
) -> tuple[dict, str]:
    """按期取得出没索引与 ready/loading/failed 状态；超时后后台继续构建。"""
    return await _wait_stage_index(theme_dir, timeout)


async def build_stage_index() -> dict[str, list[dict]]:
    """兼容原无参接口：完整等待全期索引，成功缓存 24 小时。"""
    data, _status = await _wait_stage_index(None, None)
    return data


@on_core_shutdown(priority=-1)
async def clear_stage_index_cache() -> None:
    """刷新/退出时先取消并回收旧构建，再允许共享 HTTP 客户端关闭。"""
    global _stage_index_cache, _stage_index_generation

    _stage_index_generation += 1
    states = list(_stage_index_states.values())
    _stage_index_states.clear()
    _stage_index_cache = None
    tasks = [state.task for state in states if state.task is not None]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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
    """只返回精确且唯一的关卡；同名分支由命令层列出候选供选择。"""
    hits = await search_stages(name, limit=None)
    if len(hits) == 1 and hits[0]["name"] == name:
        return hits[0]
    return None


async def search_stages(query: str, limit: int | None = 8) -> list[dict]:
    """关卡名模糊匹配：唯一前缀/子串 → 候选列表（与敌人匹配同策略）。"""
    q = (query or "").strip()
    if not q:
        return []
    candidates: list[dict] = []

    def _collect(pred) -> list[dict]:
        hits: list[dict] = []
        for theme_dir, meta in zip(_THEME_ROGUE_NUM, all_metas):
            totals = Counter(m.get("name") for m in meta.values())
            positions: Counter[str] = Counter()
            for level_id, m in sorted(meta.items()):
                name = m.get("name") or ""
                if name and pred(name):
                    positions[name] += 1
                    hits.append(
                        {
                            "theme": theme_dir,
                            "level_id": level_id,
                            "code": m.get("code") or "",
                            "name": name,
                            "variants": totals[name],
                            "variant_index": positions[name],
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


def stage_enemy_count(item: dict, elite: bool = False) -> tuple[str | None, str]:
    """使用上游按普通/紧急分组预计算的数量范围；双零不等于不出场。

    EnemyCount.svelte 按模式选择 min/max；一边为零、另一边非零表示
    模式替换。两边均零还可能是召唤/潜在敌人，保留并说明数量未知。
    """
    normal = (int(item.get("min_count") or 0), int(item.get("max_count") or 0))
    has_elite = all(item.get(key) is not None for key in ("elite_min_count", "elite_max_count"))
    emergency = (
        (int(item["elite_min_count"]), int(item["elite_max_count"]))
        if has_elite else normal
    )
    lo, hi = emergency if elite else normal
    other = normal if elite else emergency
    if hi == 0 and other[1] > 0:
        return None, ""
    if hi == 0:
        return "波次", "潜在/召唤，数量未定"
    count = str(hi) if lo == hi else f"{lo}-{hi}"
    if elite and not has_elite:
        return count, "紧急数量未提供，沿用普通范围"
    extra = emergency[1] - normal[1]
    return count, f"紧急+{extra}" if extra > 0 else ""


def build_stage_enemy_panel(
    entry: dict, item: dict, stage: dict, grades: list[dict],
    difficulty: int | None, elite: bool = False, floor: int = 0,
) -> dict:
    """关卡行和单敌面板共用的来源适配：覆写 → 地图 → 难度/层数。

    对照 tomimi stageLoad.prepareStage：只覆盖声明的属性（包括零），
    未声明项继承选定 stats 块；levelType=NORMAL 去掉领袖/精英标签。
    statHelpers.parseStats 将 all_mods 与当前模式 runes 先折算并取整，
    再应用 difficulty/others；不能把两阶段 initial 加成平铺累加。
    """
    blocks = entry.get("stats") or []
    level = max(0, min(int(item.get("level") or 0), len(blocks) - 1))
    block = dict(blocks[level]) if blocks else {}
    overwritten = item.get("overwrittenData") or {}
    for key in PANEL_STATS:
        if overwritten.get(key) is not None:
            block[key] = overwritten[key]
    scoped_entry = dict(entry)
    if overwritten.get("levelType") == "NORMAL":
        scoped_entry["type"] = [
            tag for tag in entry.get("type") or [] if tag not in ("BOSS", "ELITE")
        ]
    key = str(entry.get("key") or item.get("id") or "")
    scoped_entry["key"] = key
    types = list(scoped_entry.get("type") or [])
    groups = [
        ("关卡", stage.get("all_mods")),
        ("紧急" if elite else "普通", stage.get("elite_mods" if elite else "n_mods")),
    ]
    map_mods: list[dict] = []
    map_notes: list[str] = []
    has_elite_mods = False
    for label, effects in groups:
        mods = _effects_mods_for(effects, key, types)
        map_mods.extend(mods)
        map_notes.extend(f"{label}：{_mod_note(mod)}" for mod in mods)
        has_elite_mods |= label == "紧急" and bool(mods)
    if map_mods:
        block.update(_stats_with_mods(block, map_mods, None))
    panel = build_panel_at(
        scoped_entry, grades, difficulty, base_block=block, floor=floor
    )
    panel["buffs"] = list(dict.fromkeys(map_notes + panel["buffs"]))
    if has_elite_mods:
        panel["label"] += "·紧急"
    panel["has_map_mods"] = bool(map_mods)
    return panel


def stage_panel_rows(
    stage: dict,
    db: dict[str, dict],
    grades: list[dict],
    difficulty: int | None,
    elite: bool = False,
    floor: int = 0,
) -> list[dict]:
    """关卡敌人行（含难度/紧急/每层增益折算后的面板）；库外条目跳过。

    difficulty 为 None 时给基础面板（不折算难度）；elite 时叠加该关
    紧急作战效果；floor 为该关所在层号（关卡卡传 floors 最大值），
    难度档的每层增益按 (1+N)^floor 复合进生命/攻击（tomimi 同源；
    缺省 0 = 不折算，兼容无层数上下文的调用）。
    count：min==max 且 >0 → 固定数，0 → 波次出怪，紧急数量差 → 紧急+N 注记。
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
        count, extra_note = stage_enemy_count(item, elite)
        if count is None:
            continue
        level = int(item.get("level") or 0)
        panel = build_stage_enemy_panel(entry, item, stage, grades, difficulty, elite, floor)
        rows.append(
            {
                "key": key,
                "name": str(entry.get("name_zh") or key),
                "count": count,
                "stats": panel["stats"],
                "level": level,
                "elite_note": extra_note,
                "has_diff": bool(panel["buffs"]),
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


async def _write_source_cache(
    path: Path, payload: Any, validators: dict | None = None
) -> None:
    """取消时回收已提交的写盘任务，避免刷新删盘后旧线程重新落盘。"""
    write = asyncio.create_task(cache_write(path, payload, validators=validators))
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        # to_thread 的线程不能随协程取消；仅等待写盘，不延迟网络请求取消。
        await write
        raise


async def _fetch_url_json_cached(url: str, path: Path) -> Any:
    """任意 tomimi 直链 JSON → 本地缓存（每日条件 diff；失败过期兜底 + 负标记）。

    有校验器（ETag/Last-Modified 旁车）时先条件 GET：304 = 上游未变，
    仅续期时间戳零下载；200 = 版本更新，全量换。无校验器（历史缓存）
    或条件请求网络失败，落普通 GET。
    """
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
    validators = await cache_read_validators(path)
    if validators.get("etag") or validators.get("last_modified"):
        status, raw, new_v = await download_bytes_conditional(
            url,
            timeout=20.0,
            etag=str(validators.get("etag")) if validators.get("etag") else None,
            last_modified=(
                str(validators.get("last_modified"))
                if validators.get("last_modified")
                else None
            ),
        )
        if status == 304 and payload is not None:
            await _write_source_cache(
                path, payload, validators={**validators, **new_v, "source_url": url}
            )
            return payload
        if raw is not None:
            try:
                data = json.loads(raw)
            except Exception:
                return payload
            await _write_source_cache(
                path, data, validators={**new_v, "source_url": url}
            )
            return data
        if status != 0:
            # 4xx 与普通 GET 同语义（404=该期尚未上传），不得对同一 URL
            # 二次请求；有旧缓存服务过期数据，无旧缓存写负标记
            if payload is not None:
                return payload
            try:
                miss_path.write_text(
                    "404" if status == 404 else "net", encoding="utf-8"
                )
            except OSError:
                pass
            return None
        # status == 0：条件请求网络失败 → 落普通 GET 兜底（下方，仍回收校验器）
    # quiet_404：数据文件 404 = 该期尚未上传（写 "404" miss 的设计语义）
    status, raw, new_v = await download_bytes_conditional(
        url, timeout=20.0, quiet_404=True
    )
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
    await _write_source_cache(
        path, data, validators={**new_v, "source_url": url} if new_v else None
    )
    return data


async def load_enemy_db() -> dict[str, dict]:
    """敌人库 {enemy_key: entry}；失败返回 {}。"""
    try:
        data = await _fetch_json_cached("enemy_database.json")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _diff_file_url(theme_dir: str, base: str = _TOMIMI_BASE) -> str:
    # 文件名特例（black 期无期名后缀）收录在注册表行内
    return f"{base}/is/{theme_dir}/{diff_file_for(theme_dir)}"


async def load_difficulty_mods(theme_dir: str | None = None) -> dict[str, list]:
    """所选期难度效果 {期目录: [grade entries]}；无参取全期，失败跳过。"""
    import json

    async def fetch_one(theme_dir: str) -> tuple[str, list | None]:
        path = Path(IS_ENEMY_CACHE_DIR) / f"difficulty_{theme_dir}.json"
        try:
            payload, saved_ts = await cache_read(path)
            if payload is None or not is_fresh(saved_ts, _DATA_TTL):
                # CDN 可能缓存新文件的 404；源站取同一 main 分支。
                for base in (_TOMIMI_BASE, _TOMIMI_RAW_BASE):
                    raw = await download_bytes(
                        _diff_file_url(theme_dir, base), timeout=20.0
                    )
                    if raw is None:
                        continue
                    try:
                        fresh = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        logger.warning(
                            f"[ArknightsUID·肉鸽] {theme_dir} 难度表响应非 JSON "
                            f"({exc.__class__.__name__})，尝试其他数据源"
                        )
                        continue
                    if not isinstance(fresh, list) or not fresh:
                        continue
                    payload = fresh
                    await cache_write(path, payload)
                    break
            return theme_dir, payload if isinstance(payload, list) else None
        except Exception:
            return theme_dir, None

    themes = (
        tuple(THEME_DIRS)
        if theme_dir is None
        else ((theme_dir,) if theme_dir in THEME_DIRS else ())
    )
    # 不指定期时并发拉取；指定期只取该期，避免无关的远端慢请求。
    result: dict[str, list] = {}
    for theme_dir, data in await asyncio.gather(*(fetch_one(t) for t in themes)):
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


def _round_stat(value: float) -> int:
    """tomimi Math.round 的半数向上语义（Python round 会把正数 .5 舍偶）。"""
    return math.floor(value + 0.5)


def _stats_with_mods(
    base_block: dict, mods: list[dict], difficulty: int | None
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
        stats[stat_key] = _round_stat(value)
    return stats


def build_diff_panels(
    entry: dict, grades: list[dict], max_rows: int = 10, floor: int = 0
) -> list[dict]:
    """难度面板行：[{grade, stats: {hp/atk/def/res}, notes: [str]}]。

    行 = 新手档(0/1/2) + 有累积变化的高档；数值基于形态 0（基础形态）。
    非面板字段（如 dmg_res/aspd）以文本 note 保留。floor 为折算层号
    （敌人卡无关卡上下文，按 tomimi 惯例传 1）；缺省 0 = 不折算。
    """
    stats_blocks = entry.get("stats") or []
    if not stats_blocks:
        return []
    base_block = stats_blocks[0]
    enemy_key = str(entry.get("key") or "")
    enemy_types = list(entry.get("type") or [])

    # 有效果的难度档（面板行候选）+ 新手档 0/1/2（HP 削减，均有信息量）
    # + 有每层增益的档（floor 折算后数值逐档递增，各档都有信息量）
    effect_grades = sorted(
        {int(g.get("difficulty", 0)) for g in grades if g.get("effects")}
    )
    floor_grades = sorted(
        {int(g.get("difficulty", 0)) for g in grades if g.get("floorBuff")}
        if floor > 0
        else set()
    )
    row_grades = sorted({0, 1, 2} | set(effect_grades) | set(floor_grades))
    if len(row_grades) > max_rows:
        # 超限时保留新手档 + 最近的高档（靠近满难度最有参考价值）
        head = [g for g in row_grades if g <= 2]
        tail = [g for g in row_grades if g > 2][-(max_rows - len(head)) :]
        row_grades = sorted(head + tail)

    rows: list[dict] = []
    for grade in row_grades:
        mods = _grade_mods_for_enemy(grades, enemy_key, enemy_types, grade)
        mods.extend(_floor_buff_mods(grades, grade, floor))
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
            stats[stat_key] = _round_stat(value)
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

    from ..image import _path_lock, _read_valid_image_bytes, _save_valid_image_bytes
    from .path_safety import safe_cache_stem

    enemy_key = (enemy_key or "").strip()
    if not enemy_key:
        return ""
    stem = safe_cache_stem(enemy_key)
    cache_dir = Path(IS_ENEMY_CACHE_DIR) / "icons"
    filename = f"{stem}.webp"
    local_path = cache_dir / filename
    miss_path = cache_dir / f"{filename}.miss"
    async with await _path_lock(local_path):
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            if miss_path.exists():
                if time.time() - miss_path.stat().st_mtime < _ICON_MISS_TTL:
                    return ""
                miss_path.unlink()
            data = await asyncio.to_thread(_read_valid_image_bytes, local_path)
            if data is None:
                local_path.unlink(missing_ok=True)
                # quiet_404：上游尚未上传该图时静默降级；失败保留 1h 退避。
                _status, data = await download_bytes_status(
                    f"{_ICON_BASE}/{filename}", timeout=20.0, quiet_404=True
                )
                if data is None:
                    raise RuntimeError(f"icon download failed: {filename}")
                await asyncio.to_thread(_save_valid_image_bytes, local_path, data)
            return f"data:image/webp;base64,{base64.b64encode(data).decode('utf-8')}"
        except Exception:
            local_path.unlink(missing_ok=True)
            try:
                miss_path.write_text("", encoding="utf-8")
            except OSError:
                pass
            return ""
