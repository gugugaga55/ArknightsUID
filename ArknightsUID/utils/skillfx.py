"""技能等级效果共享层：ark干员（wiki）与 ark练度卡（account）复用。

此前两侧各自实现「玩家专精档 → 当前级详情 + 满级详情」：wiki 走
_skill_level_context（带 meta），account 走 _char_skill_effects（仅描述），
口径分叉后练度卡长期缺 初动/消耗/持续 数值。现统一到本模块：
描述与 SP 数值同源同格式（初动X·Y耗·Zs，空字段跳过）。
"""

from __future__ import annotations

from .prts.client import (
    OperatorSkill,
    SkillLevelDetail,
    final_level,
    select_owner_level,
)


def skill_meta(lv: SkillLevelDetail | None) -> str:
    """单级 SP 尾注：`初动10·消耗24·持续30秒`，每项全标签写清楚，空项跳过。

    消耗/持续不带单位后缀缩写（此前 "24耗/30s" 与初动混排读不清）：
    数值直接跟在标签后，持续补「秒」，被动技能（无 SP）返回空串。
    """
    if lv is None:
        return ""
    parts = []
    if lv.sp_init:
        parts.append(f"初动{lv.sp_init}")
    if lv.sp_cost:
        parts.append(f"消耗{lv.sp_cost}")
    if lv.duration:
        parts.append(f"持续{lv.duration}秒")
    return "·".join(parts)


def skill_level_context(lv: SkillLevelDetail | None) -> dict | None:
    """单级详情 → {level, description, sp_init, sp_cost, duration, meta}。

    无等级或无描述（被动/占位级）返回 None，调用方以此判断是否展示。
    """
    if lv is None or not lv.description:
        return None
    return {
        "level": lv.level,
        "description": lv.description,
        "sp_init": lv.sp_init,
        "sp_cost": lv.sp_cost,
        "duration": lv.duration,
        "meta": skill_meta(lv),
    }


def skill_current_final(
    skill: OperatorSkill,
    specialize: int | None,
    main_skill_lvl: int | None,
) -> tuple[dict | None, dict | None]:
    """单个技能的 (当前级, 满级级) 详情上下文。

    specialize 为 None（玩家数据里没有该技能的专精档）时当前级为 None；
    满级取 final_level（专3，无专精回退 7 级），已等于当前级时不重复给
    （模板只渲染一条）。main_skill_lvl 缺省按 7 级处理。
    """
    current_lv = (
        select_owner_level(skill.levels, int(specialize), int(main_skill_lvl or 7))
        if specialize is not None
        else None
    )
    final_lv = final_level(skill.levels)
    current = skill_level_context(current_lv)
    final = skill_level_context(final_lv) if final_lv is not current_lv else None
    return current, final


def owner_skill_effects(
    op,
    specialize_by_name: dict[str, int],
    main_skill_lvl: int | None,
) -> dict[str, dict]:
    """玩家持有技能名 → {current, final} 上下文（练度卡增强用）。

    与 skill_current_final 同口径；op 上玩家未点亮专精的技能不出现在
    结果里，调用方对缺省技能只展示专精点。
    """
    effects: dict[str, dict] = {}
    for s in op.skills:
        if s.name not in specialize_by_name:
            continue
        current, final = skill_current_final(
            s, specialize_by_name[s.name], main_skill_lvl
        )
        effects[s.name] = {"current": current, "final": final}
    return effects
