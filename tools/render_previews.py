"""设计预览渲染脚本（手动工具，非 pytest 测试）。

用假数据把 templates/ 下所有业务卡渲染成 JPEG，输出到 docs/design/previews/，
供设计评审与回归对照使用。

运行：在 gsuid_core venv 下
    python tools/render_previews.py            # 渲染全部
    python tools/render_previews.py ark_help   # 只渲染指定模板
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 让脚本能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

OUT_DIR = Path(__file__).parents[1] / "docs" / "design" / "previews"
ASSET_DIR = Path(__file__).parents[1] / "docs" / "design" / ".assets"

VERSION = "v0.3.0"


class _CharStub:
    """养成卡预览用的干员对象桩（仅暴露模板访问的属性）。"""

    def __init__(self, profession_name: str, rarity_display: int) -> None:
        self.profession_name = profession_name
        self.rarity_display = rarity_display

# 鹰角官方 CDN 资产（hycdn），预览时下载到 .assets 缓存目录后 base64 内联
# URL 规则：char.skinId / skills[].id 来自 player_info；@ → %40，# → %23
HYCDN_ASSETS = {
    "avatar_amiya": "https://web.hycdn.cn/arknights/game/assets/char_skin/avatar/char_002_amiya%231.png",
    "skill_amiya_1": "https://web.hycdn.cn/arknights/game/assets/char_skill/skcom_magic_rage%5B3%5D.png",
    "skill_amiya_2": "https://web.hycdn.cn/arknights/game/assets/char_skill/skchr_amiya_2.png",
    "skill_amiya_3": "https://web.hycdn.cn/arknights/game/assets/char_skill/skchr_amiya_3.png",
}


def asset_b64(key: str) -> str:
    """下载（带缓存）hycdn 资产并转 base64 data URL，失败返回空串。"""
    import base64
    import urllib.request

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    cache = ASSET_DIR / f"{key}.png"
    if not cache.exists():
        try:
            urllib.request.urlretrieve(HYCDN_ASSETS[key], cache)
        except Exception as e:
            print(f"⚠️  资产下载失败 {key}: {e}")
            return ""
    return "data:image/png;base64," + base64.b64encode(cache.read_bytes()).decode()


# 各模板的假数据（数据层接入前的设计验证用）
def _gacha_plan_preview() -> dict:
    """攒抽规划卡预览：走真实计算链（build_summary/build_plan_context）。"""
    from ArknightsUID.utils.gacha_plan import (
        PlanArgs,
        build_plan_context,
        build_summary,
    )
    from ArknightsUID.version import ArknightsUID_version

    args = PlanArgs(
        orundum=60000, originium=30, single_tickets=2, ten_tickets=1, pity=37
    )
    summary = build_summary(
        args,
        {"title": "·跨洲远航·", "end": "2026-09-01 04:00", "end_ts": 1e11},
        pity=37,
    )
    return build_plan_context(
        summary, "刀客塔不吃香菜", "12****89", ArknightsUID_version
    )


MOCK_CONTEXTS = {
    "ark_help.html": {
        "version": VERSION,
        "groups": [
            {
                "name": "账号",
                "en": "ACCOUNT",
                "cmds": [
                    {"cmd": "ark绑定 <凭据>", "desc": "森空岛登录凭据绑定（私聊）", "eg": "ark绑定 <凭据>"},
                    {"cmd": "ark扫码登录", "desc": "森空岛 APP 扫码（私聊）", "eg": "ark扫码登录"},
                    {"cmd": "ark查看绑定", "desc": "本地绑定账号列表", "eg": "ark查看绑定"},
                    {"cmd": "ark签到", "desc": "森空岛每日签到", "eg": "ark签到"},
                ],
            },
            {
                "name": "查询",
                "en": "QUERY",
                "cmds": [
                    {"cmd": "ark理智 / ark便签", "desc": "理智与基础状态卡", "eg": "ark理智"},
                    {"cmd": "ark日常", "desc": "日常 / 周常进度", "eg": "ark日常"},
                    {"cmd": "ark剿灭", "desc": "剿灭合成玉进度", "eg": "ark剿灭"},
                    {"cmd": "ark练度统计", "desc": "全干员练度统计", "eg": "ark练度统计"},
                    {"cmd": "arkBOX", "desc": "BOX 稀有度/精英化分析", "eg": "arkBOX"},
                    {"cmd": "ark练度卡", "desc": "单干员练度卡（支持别名）", "eg": "ark练度卡 银灰"},
                    {"cmd": "ark肉鸽", "desc": "集成战略进度", "eg": "ark肉鸽"},
                ],
            },
            {
                "name": "抽卡",
                "en": "GACHA",
                "cmds": [
                    {"cmd": "ark抽卡记录", "desc": "抽卡记录统计卡", "eg": "ark抽卡记录"},
                    {"cmd": "ark兑换码", "desc": "礼包码兑换", "eg": "ark兑换码 <code>"},
                    {"cmd": "ark养成", "desc": "养成材料缺口计算", "eg": "ark养成 银灰"},
                    {"cmd": "ark攒抽 [参数]", "desc": "攒抽规划（自动垫刀/卡池）", "eg": "ark攒抽 玉:60000 石:30 垫刀:20"},
                ],
            },
            {
                "name": "Wiki",
                "en": "PRTS",
                "cmds": [
                    {"cmd": "arkwiki", "desc": "PRTS 百科查询", "eg": "arkwiki 源石"},
                    {"cmd": "ark干员", "desc": "干员资料卡", "eg": "ark干员 阿米娅"},
                    {"cmd": "ark专精材料", "desc": "技能专精材料清单卡", "eg": "ark专精材料 阿米娅"},
                    {"cmd": "ark材料", "desc": "材料掉落查询", "eg": "ark材料 固源岩"},
                    {"cmd": "ark关卡", "desc": "关卡资料卡", "eg": "ark关卡 1-7"},
                ],
            },
            {
                "name": "活动",
                "en": "PUSH",
                "cmds": [
                    {"cmd": "ark活动", "desc": "进行中活动/卡池", "eg": "ark活动"},
                    {"cmd": "ark公告", "desc": "官方公告列表", "eg": "ark公告"},
                    {"cmd": "ark刷图推荐", "desc": "关卡效率榜（一图流）", "eg": "ark刷图推荐"},
                    {"cmd": "订阅方舟活动", "desc": "群聊订阅新动态推送", "eg": "订阅方舟活动"},
                ],
            },
        ],
    },
    "ark_status.html": {
        "name": "刀客塔不吃香菜",
        "level": 85,
        "uid": "12****89",
        "ap_current": 45,
        "ap_max": 135,
        "ap_pct": 33,
        "ap_full_at": "08-14 02:33",
        "ap_recover_in": "3h 12m",
        "reg_days": 2150,
        "chars_total": 285,
        "campaign_current": 1200,
        "campaign_total": 1800,
        "version": VERSION,
    },
    "ark_daily.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "daily_done": 3,
        "daily_total": 8,
        "weekly_done": 5,
        "weekly_total": 12,
        "campaign_current": 1200,
        "campaign_total": 1800,
        "version": VERSION,
    },
    "ark_campaign.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "reward_current": 1200,
        "reward_total": 1800,
        "reward_pct": 67,
        "stages": [
            {"name": "切尔诺伯格", "progress": 400, "total": 400},
            {"name": "龙门外环", "progress": 400, "total": 400},
            {"name": "龙门市区", "progress": 355, "total": 400},
            {"name": "大骑士领郊外", "progress": 0, "total": 400},
        ],
        "version": VERSION,
    },
    "ark_char_card.html": {
        "name": "阿米娅(术师)",
        "rarity": 5,
        "profession": "术师 CASTER",
        "avatar": asset_b64("avatar_amiya"),
        "level": 80,
        "max_level": 80,
        "elite": 2,
        "potential": 6,
        "favor": 200,
        "main_skill_lvl": 7,
        "skills": [
            {"icon": asset_b64("skill_amiya_1"), "name": "战术咏唱·γ型", "specialize": 3,
             "current": "攻击速度+60", "current_level": "专3",
             "final": "", "final_level": ""},
            {"icon": asset_b64("skill_amiya_2"), "name": "精神爆发", "specialize": 3,
             "current": "每秒消耗最大生命2%，攻击力+200%", "current_level": "专3",
             "final": "", "final_level": ""},
            {"icon": asset_b64("skill_amiya_3"), "name": "奇美拉", "specialize": 1,
             "current": "攻击力+170%，攻击间隔增大", "current_level": "专1",
             "final": "攻击力+220%，攻击间隔增大，攻击范围扩大", "final_level": "专3"},
        ],
        "equips": [
            {"name": "暗语", "stage": 3},
        ],
        "version": VERSION,
    },
    "ark_char_stat.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "total": 285,
        "e2_total": 120,
        "maxlvl_total": 34,
        "professions": [
            {"name": "先锋", "en": "PIONEER", "count": 32, "e2": 15, "maxlvl": 4},
            {"name": "近卫", "en": "WARRIOR", "count": 52, "e2": 28, "maxlvl": 9},
            {"name": "狙击", "en": "SNIPER", "count": 38, "e2": 16, "maxlvl": 5},
            {"name": "重装", "en": "TANK", "count": 30, "e2": 12, "maxlvl": 3},
            {"name": "医疗", "en": "MEDIC", "count": 28, "e2": 11, "maxlvl": 3},
            {"name": "术师", "en": "CASTER", "count": 36, "e2": 18, "maxlvl": 6},
            {"name": "辅助", "en": "SUPPORT", "count": 31, "e2": 10, "maxlvl": 2},
            {"name": "特种", "en": "SPECIAL", "count": 38, "e2": 10, "maxlvl": 2},
        ],
        "version": VERSION,
    },
    "ark_box_analysis.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "total": 285,
        "rarity_rows": [
            {"label": "六星", "count": 52, "pct": 18, "color": "#ff8c00"},
            {"label": "五星", "count": 98, "pct": 34, "color": "#d9a34a"},
            {"label": "四星", "count": 78, "pct": 27, "color": "#8a73c4"},
            {"label": "三星及以下", "count": 57, "pct": 21, "color": "#4a90d9"},
        ],
        "elite_rows": [
            {"label": "精英化二", "count": 120},
            {"label": "精英化一", "count": 80},
            {"label": "无精英化", "count": 85},
        ],
        "version": VERSION,
    },
    "ark_gacha_card.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "total": 300,
        "six_count": 8,
        "six_rate": "2.67",
        "avg_six": "37.5",
        "pity": 23,
        "pity_max": 80,
        "recent": [
            {"name": "维什戴尔", "pool": "限定·重逢", "pull": 62},
            {"name": "逻各斯", "pool": "常驻", "pull": 41},
            {"name": "纯烬艾雅法拉", "pool": "限定·火山", "pull": 78},
            {"name": "左乐", "pool": "常驻", "pull": 12},
        ],
        "version": VERSION,
    },
    "ark_gacha_plan.html": _gacha_plan_preview(),
    "ark_module.html": {
        "name": "银灰",
        "modules": [
            {
                "id": "uniequip_002_svrash",
                "name": "雪境羽兽护理套组",
                "type": "X",
                "type_name": "领袖的远见",
                "desc": "出击时若在部署位 Operator 列表内，自身与「血狼」的嘲讽等级提高",
                "unlock_phase": 2,
                "unlock_level": 60,
                "unlock_favor": 100,
                "costs": [
                    {"name": "模组数据块", "count": 4, "icon": ""},
                    {"name": "晶体电子单元", "count": 2, "icon": ""},
                    {"name": "龙门币", "count": 120000, "icon": ""},
                ],
                "level_attrs": {
                    "L1": "攻击 +45，防御 +25",
                    "L2": "攻击 +55，防御 +30",
                    "L3": "攻击 +65，防御 +35",
                },
            },
            {
                "id": "uniequip_001_svrash",
                "name": "银灰证章",
                "type": "ORIGINAL",
                "type_name": "基础证章",
                "desc": "干员银灰的模组证章，喀兰贸易颁发了这枚象征信任的证章。",
                "unlock_phase": 0,
                "unlock_level": 0,
                "unlock_favor": 0,
                "costs": [],
                "level_attrs": {},
            },
        ],
        "version": VERSION,
    },
    "ark_craft.html": {
        "name": "聚酸酯块",
        "need": 1,
        "icon": "",
        "rows": [
            {"depth": 0, "name": "聚酸酯块", "need": 1, "craftable": True, "output": 1, "gold": 400,
             "costs": [{"name": "聚酸酯组", "count": 4, "icon": ""}, {"name": "异铁", "count": 2, "icon": ""}]},
            {"depth": 1, "name": "聚酸酯组", "need": 4, "craftable": True, "output": 1, "gold": 100,
             "costs": [{"name": "聚酸酯", "count": 4, "icon": ""}]},
            {"depth": 2, "name": "聚酸酯", "need": 16, "craftable": False, "output": 0, "gold": 0, "costs": []},
            {"depth": 1, "name": "异铁", "need": 2, "craftable": True, "output": 1, "gold": 100,
             "costs": [{"name": "异铁碎片", "count": 3, "icon": ""}]},
            {"depth": 2, "name": "异铁碎片", "need": 6, "craftable": False, "output": 0, "gold": 0, "costs": []},
        ],
        "version": VERSION,
    },
    "ark_farm_plan.html": {
        "rows": [
            {"name": "聚酸酯块", "count": 10, "icon": "",
             "farm": {"stage_code": "1-8", "ap_expect": 26.5, "sanity": 2650.0, "times": 99.4},
             "craft": {"sanity": 2310.0, "leaves": "聚酸酯×160 + 异铁×20"}, "note": ""},
            {"name": "D32钢", "count": 4, "icon": "",
             "farm": None,
             "craft": {"sanity": 2394.0, "leaves": "研磨石×12 + RMA70-24×8 + 三水锰矿×8"}, "note": ""},
            {"name": "提纯源岩", "count": 6, "icon": "",
             "farm": None, "craft": None, "note": "没有样本充足的掉落关卡"},
        ],
        "total_farm": 2650.0,
        "unknown": [],
        "version": VERSION,
    },
    "ark_crisis.html": {
        "seasons": [
            {"name": "弧光作战", "v2": True, "start": "2026-08-16", "end": "2026-08-30"},
            {"name": "湮灭计划", "v2": True, "start": "2026-05-01", "end": "2026-05-15"},
            {"name": "尘环行动", "v2": False, "start": "2026-02-05", "end": "2026-02-19"},
            {"name": "松烟行动", "v2": False, "start": "2025-11-20", "end": "2025-12-04"},
        ],
        "version": VERSION,
    },
    "ark_wiki_page.html": {
        "title": "源石",
        "summary": "泰拉大陆上普遍存在的黑色半透明矿物，蕴含着巨大的能量，也是天灾与矿石病的源头。它被广泛用于能源、工业与法术催化等领域，是这个世界运转的基石之一。",
        "fields": [
            {"key": "名称", "value": "源石"},
            {"key": "类型", "value": "矿物 / 能源"},
            {"key": "用途", "value": "能源供给、工业原料、法术催化"},
            {"key": "获取方式", "value": "关卡掉落、任务奖励、采购中心"},
            {"key": "描述", "value": "接触过量源石会导致矿石病感染"},
        ],
        "source_url": "https://prts.wiki/w/源石",
        "version": VERSION,
    },
    "ark_op_panel.html": {
        "name": "银灰",
        "char_id": "char_172_svrash",
        "profession": "近卫",
        "rarity": 6,
        "avatar": "",
        "owned": True,
        "config_labels": ["精2 Lv90", "潜能6", "好感200%", "X模组·雪境羽兽护理套组 Lv3", "技能：真银斩·专3"],
        "attrs": {"hp": "2990", "atk": "859", "def": "397", "res": "10",
                  "cost": "18", "block": "2", "respawn": "66", "interval": "1.3"},
        "breakdown": [
            "生命：基础 2560 → 好感+100、模组+330",
            "攻击：基础 713 → 潜能+26、好感+50、模组+70",
        ],
        "talents": [
            {"name": "领袖", "description": "攻击力+12%（+2%），编入队伍时所有我方单位的再部署时间-10%"},
            {"name": "鹰眼视觉", "description": "攻击范围内敌人的隐匿效果失效"},
        ],
        "skill": {
            "title": "真银斩", "label": "专3",
            "description": "攻击力+200%，攻击范围扩大，最多同时攻击3个敌人（满潜后攻击范围内所有敌人）",
            "sp_init": "15", "sp_cost": "60", "duration": "30",
        },
        "skills": [],
        "potentials": ["部署费用-1", "再部署时间-4秒", "攻击力+26", "第一天赋效果增强", "部署费用-1", "第二天赋效果增强"],
        "module_choices": [{"type": "X", "name": "雪境羽兽护理套组"}],
        "source_url": "https://prts.wiki/w/银灰",
        "source_note": "数值 ArknightsGameData · 技能文本 PRTS Wiki",
        "version": VERSION,
    },
    "ark_wiki_operator.html": {
        "title": "阿米娅",
        "name": "阿米娅",
        "rarity": "5",
        "profession": "术师",
        "branch": "中坚术师",
        "position": "远程位",
        "faction": "罗德岛",
        "tags": "输出",
        "obtain": "初始干员 / 主线剧情",
        "trait": "攻击造成法术伤害",
        "summary": "罗德岛的公开领袖，年纪轻轻却肩负着沉重的责任。",
        "talents": [
            {"slot": "第一天赋", "name": "情绪吸收", "unlock": "精英化1",
             "description": "精英化1：技力回复速度+0.2/秒 / 精英化2：技力回复速度+0.45/秒",
             "stages": [
                 {"condition": "精英化1", "effect": "技力回复速度+0.2/秒"},
                 {"condition": "精英化2", "effect": "技力回复速度+0.45/秒"},
             ],
             "potential": {"condition": "精英化2", "effect": "技力回复速度+0.45/秒（+0.05/秒）"}},
            {"slot": "第二天赋", "name": "奇美拉适性", "unlock": "精英化2",
             "description": "精英化2：技能【奇美拉】结束时不再强制退场，改为眩晕5秒",
             "stages": [
                 {"condition": "精英化2", "effect": "技能【奇美拉】结束时不再强制退场，改为眩晕5秒"},
             ]},
        ],
        "skills": [
            {
                "name": "战术咏唱·γ型",
                "skill_type": "自动回复 · 手动触发",
                "range": "",
                "levels": [
                    {"level": "1", "description": "攻击速度+15", "sp_init": "0", "sp_cost": "40", "duration": "30"},
                    {"level": "4", "description": "攻击速度+30", "sp_init": "0", "sp_cost": "37", "duration": "30"},
                    {"level": "7", "description": "攻击速度+45", "sp_init": "0", "sp_cost": "34", "duration": "30"},
                    {"level": "专精1", "description": "攻击速度+50", "sp_init": "0", "sp_cost": "33", "duration": "30"},
                    {"level": "专精3", "description": "攻击速度+60", "sp_init": "0", "sp_cost": "30", "duration": "30"},
                ],
                "current": {"level": "专精1", "description": "攻击速度+50", "sp_init": "0", "sp_cost": "33", "duration": "30", "meta": "33耗·30s"},
                "final": {"level": "专精3", "description": "攻击速度+60", "sp_init": "0", "sp_cost": "30", "duration": "30", "meta": "30耗·30s"},
                "mastery_costs": [
                    {"mastery": "专精1", "materials": ["技巧概要·卷3", "固源岩组"]},
                    {"mastery": "专精3", "materials": ["技巧概要·卷3", "D32钢", "提纯源岩"]},
                ],
            },
            {
                "name": "精神爆发",
                "skill_type": "自动回复 · 手动触发",
                "range": "",
                "levels": [
                    {"level": "1", "description": "每秒消耗最大生命3%，攻击力+120%", "sp_init": "0", "sp_cost": "80", "duration": "25"},
                    {"level": "7", "description": "每秒消耗最大生命3%，攻击力+160%", "sp_init": "0", "sp_cost": "74", "duration": "25"},
                    {"level": "专精3", "description": "每秒消耗最大生命2%，攻击力+200%", "sp_init": "0", "sp_cost": "68", "duration": "25"},
                ],
                "current": {"level": "7", "description": "每秒消耗最大生命3%，攻击力+160%", "sp_init": "0", "sp_cost": "74", "duration": "25", "meta": "74耗·25s"},
                "final": {"level": "专精3", "description": "每秒消耗最大生命2%，攻击力+200%", "sp_init": "0", "sp_cost": "68", "duration": "25", "meta": "68耗·25s"},
                "mastery_costs": [],
            },
        ],
        "owner": {
            "uid": "12****89",
            "main_skill_lvl": 7,
            "skills": {"战术咏唱·γ型": 1, "精神爆发": 0},
            "training_remain": "剩约2小时15分",
        },
        "source_url": "https://prts.wiki/w/阿米娅",
        "version": VERSION,
    },
    "ark_wiki_enemy.html": {
        "title": "爱国者",
        "name": "爱国者",
        "index": "P7",
        "rank": "领袖",
        "description": "整合运动的干部，温迪戈血脉最后的战士。他的盾与戟只为守护身后的感染者而举起。",
        "damage_type": "物理",
        "attack_type": "近战",
        "move_type": "地面",
        "kind": "感染者",
        "event": "主线第七章",
        "enemy_id": "P7",
        "has_stats": True,
        "max_hp": "45000",
        "atk": "1200",
        "defense": "1500",
        "res": "60",
        "secondary_stats": [
            {"label": "移动速度", "value": "0.4"},
            {"label": "攻击间隔", "value": "4.0"},
            {"label": "攻击范围半径", "value": "1.5"},
            {"label": "重量", "value": "6"},
            {"label": "生命恢复", "value": "200"},
        ],
        "resistances": [
            {"label": "沉默", "value": "有"},
            {"label": "眩晕", "value": "有"},
        ],
        "has_resistances": True,
        "variants": [
            {
                "label": "等级2",
                "hp": "60000",
                "atk": "1600",
                "defense": "1500",
                "res": "60",
                "abilities": [
                    "技能·毁灭击：对目标造成相当于攻击力200%的物理伤害\n※不可对空（初始10·消耗25）",
                ],
            }
        ],
        "abilities": [
            "技能·投枪\n[毁灭姿态]：锁定半径20范围内距离自身最远的位于高地的1名我方单位，蓄力约3s后对其造成相当于攻击力135%的物理普通伤害\n※不可对空（初始15·消耗20）",
            "技能·生命献祭：60秒内无法移动，每间隔0.8s触发一次【献祭】（初始9999·消耗9999）",
            "天赋：免疫脉冲波\n在场时令全场敌方单位攻击力+20%、防御力+200（多个爱国者给予的属性增益不叠加）",
            "能力：大幅强化敌军攻击与防御力",
        ],
        "source_url": "https://prts.wiki/w/爱国者",
        "version": VERSION,
    },
    "ark_wiki_stage.html": {
        "title": "1-7",
        "name": "暴君",
        "stage_id": "main_01-07",
        "stage_type": "主线",
        "difficulty": "普通",
        "recommended_level": "LV.10",
        "ap_cost": "6",
        "enemy_count": "44",
        "description": "切尔诺伯格的废墟中，整合运动的暴徒正在集结。注意敌人的远程单位会从侧翼出现。",
        "drops": [
            {"name": "固源岩", "icon": ""},
            {"name": "源岩", "icon": ""},
            {"name": "破损装置", "icon": ""},
            {"name": "酯原料", "icon": ""},
        ],
        "extra_drops": [
            {"name": "初级作战记录", "icon": ""},
            {"name": "基础加固建材", "icon": ""},
        ],
        "enemies": [
            {"name": "士兵", "count": 18, "level": "", "icon": "", "hp": "1650", "atk": "200", "defense": "100", "res": "0",
             "mechanics": "无特殊机制"},
            {"name": "弩手", "count": 12, "level": "", "icon": "", "hp": "1550", "atk": "240", "defense": "100", "res": "0"},
            {"name": "暴徒", "count": 8, "level": "", "icon": "", "hp": "5250", "atk": "700", "defense": "100", "res": "0"},
            {"name": "机动盾兵", "count": 6, "level": "", "icon": "", "hp": "4000", "atk": "200", "defense": "1000", "res": "0",
             "mechanics": "天赋：受到攻击时获得护盾，可抵挡一次伤害"},
        ],
        "assault_diffs": ["部署上限：8→6（-2）"],
        "has_assault": True,
        "condition_blocks": [
            {
                "label": "突袭条件",
                "en": "ASSAULT",
                "tone": "red",
                "note": "",
                "rows": [
                    {"text": "敌方单位的生命值、攻击力、防御力都提升至120%", "tags": []},
                    {"text": "部署上限：8→6（-2）", "tags": []},
                ],
            },
            {
                "label": "矩阵指标 · 基础：俯冲轰炸",
                "en": "INDICATORS",
                "tone": "yellow",
                "note": "每组必选其一",
                "rows": [
                    {
                        "text": "不再持有<干扰装置>，但额外出现可掉落<干扰装置>的<豪华赞助无人机>，场上初始额外出现一个<寒霜>且<寒霜>的防御力+100%",
                        "tags": ["锁定"],
                    },
                    {"text": "所有敌人移动速度提升60%，且生命值+70%", "tags": []},
                ],
            },
            {
                "label": "矩阵指标 · 附加条件",
                "en": "INDICATORS",
                "tone": "blue",
                "note": "自愿叠加，仅附加指标提供评分",
                "rows": [
                    {
                        "text": "出现额外的<帝国炮火中枢先兆者>，且其防御力+100%，攻击力+100%，生命值+200%",
                        "tags": ["分数2"],
                    },
                    {
                        "text": "<豪华赞助无人机>免疫沉睡，冻结和晕眩，且生命值+350%",
                        "tags": ["分数2", "冲突"],
                    },
                ],
            },
        ],
        "map_image": "",
        "map_url": "",
        "source_url": "https://prts.wiki/w/1-7",
        "version": VERSION,
    },
    "ark_rogue_enemy.html": {
        "name": "歌利亚",
        "enemy_key": "enemy_2074_skzgly",
        "tags": ["领袖", "近战"],
        "resistances": ["沉默"],
        "icon": "",
        "forms": [
            {
                "title": "形态1", "hp": 80000, "atk": 2350, "def": 1000,
                "res": 25, "weight": 5, "attack": "近战/物理",
                "extras": ["移速 0.5", "间隔 6s", "进点-5"],
                "mechanics": [
                    "只在阻挡数不低于5时才会被我方单位阻挡",
                    "进入目标点后停留一段时间，持续造成大量法术伤害",
                ],
            },
        ],
        "theme_name": "萨卡兹的无终奇语",
        "panels": [],
        "stage_groups": [
            {
                "theme": "sarkaz",
                "theme_name": "萨卡兹的无终奇语",
                "stages": [
                    "不容拒绝·ISW-DF·6-7层",
                    "共谋的夜幕·ISW-NO·3层·形态2",
                    "大君之赐·ISW-DF·3层·紧急+",
                    "拆东补西·ISW-NO·1-2层",
                    "守望的河水·ISW-NO·1层",
                    "枯朽的引擎·ISW-DF·4层",
                ],
                "total": 9,
            }
        ],
        "focus": {
            "grade": 12,
            "label": "难度12",
            "stats": {"hp": 122880, "atk": 2866, "def": 1150, "res": 25},
            "buffs": ["生命×1.2", "攻击×1.15", "防御×1.15", "受到伤害×0.1", "每层增益+7%（未计入，按层复合）"],
        },
        "source_note": "数据 tomimi.dev（游戏解包）",
        "version": VERSION,
    },
    "ark_rogue_stage.html": {
        "name": "拆东补西",
        "code": "ISW-NO",
        "level_id": "level_rogue4_2-2",
        "theme_name": "萨卡兹的无终奇语",
        "difficulty": 15,
        "difficulty_label": "难度15",
        "elite": True,
        "floors": "1-2层",
        "elite_descs": ["所有敌方单位的攻击力，防御力，生命值+20%", "额外出现1个年代之刺"],
        "elite_chips": ["攻击×1.2", "防御×1.2", "生命×1.2"],
        "rows": [
            {"key": "enemy_2063_skzlch", "name": "萨卡兹领主", "icon": "", "count": "4",
             "stats": {"hp": 27840, "atk": 1128, "def": 1440, "res": 30}, "level": 0,
             "elite_note": "", "has_diff": True},
            {"key": "enemy_2065_skzjs", "name": "萨卡兹剑士", "icon": "", "count": "波次",
             "stats": {"hp": 5184, "atk": 648, "def": 345, "res": 0}, "level": 0,
             "elite_note": "", "has_diff": True},
            {"key": "enemy_1071_dftman", "name": "重装防御者", "icon": "", "count": "0-1",
             "stats": {"hp": 41472, "atk": 900, "def": 2073, "res": 0}, "level": 0,
             "elite_note": "紧急+1", "has_diff": True},
        ],
        "row_count": 3,
        "skipped": 1,
        "floor_note": "每层增益+10%（未计入，按层复合）",
        "source_note": "数据 tomimi.dev（游戏解包）",
        "version": VERSION,
    },
    "ark_rogue_relic.html": {
        "name": "钝爪-典训",
        "rows": [
            {
                "usage": "立即进阶一个【先锋】干员（不消耗希望）",
                "rarity_name": "普通",
                "themes": ["岁的界园志异", "萨卡兹的无终奇语", "探索者的银凇止境", "水月与深蓝之树", "傀影与猩红孤钻"],
                "icon": "",
            },
        ],
        "desc": "一套强有力的装备，一颗勇往直前的心。",
        "source_note": "数据 ArknightsGameData · 图标 tomimi.dev",
        "version": VERSION,
    },
    "ark_rogue_ending.html": {
        "theme_name": "萨卡兹的无终奇语",
        "count": 5,
        "shown_count": 5,
        "focus_note": "",
        "guide_updated": "2026-08-30",
        "banner": "",
        "endings": [
            {
                "seq": "01", "name": "憧憬未来", "label": "默认",
                "desc": "把故事讲到最后，卡兹戴尔拥有了它的结局。",
                "unlock": "无前置",
                "steps": [
                    "第5层倒数第2节点固定为命运所指，事件「尾页」：选择不报出魔王的名讳",
                    "不持有【时与光】【阿纳萨羯磨】【无终之钥】时通过第5层即可",
                ],
                "key_relics": [],
                "final": "ISW-DF 紧急授课（持有滚动先祖时为思维矫正）",
                "note": "", "has_guide": True,
            },
            {
                "seq": "02", "name": "双王记", "label": "",
                "desc": "两位魔王，一顶冠冕。",
                "unlock": "达成过「憧憬未来」",
                "steps": [
                    "第2层命运所指「戴冠式」：选“两人为对方戴上冠冕”，获得遗愿【纯白花瓣】与【无字地契】",
                    "在【去伪存真】解读这两缕遗愿，获得【巴别塔誓言】",
                    "（可选）「离去者的庭院」查看阿米娅纪念碑获得【十戒】，最终战中尊主残影生命减半",
                    "「尾页」选报出魔王名讳：第5层险路恶敌变为朝谒（或魂灵朝谒），不持三件结局藏品通过第5层",
                ],
                "key_relics": [
                    {"name": "纯白花瓣", "item_id": "rogue_4_fragment_D_01", "icon": ""},
                    {"name": "无字地契", "item_id": "rogue_4_fragment_D_02", "icon": ""},
                    {"name": "巴别塔誓言", "item_id": "rogue_4_relic_final_1", "icon": ""},
                    {"name": "十戒", "item_id": "rogue_4_relic_final_3", "icon": ""},
                ],
                "final": "特雷西斯与特蕾西娅（黑冠形态）",
                "note": "", "has_guide": True,
            },
            {
                "seq": "03", "name": "天使之城", "label": "",
                "desc": "在光芒中一切都被原谅了。",
                "unlock": "达成过「憧憬未来」，且历史重构点亮【置换现实】",
                "steps": [
                    "【失与得】选【一个方盒】获得【先知长角】（与【一片莲瓣】互斥）",
                    "刷新节点：受【先知长角】影响必出命运所指「待诉说的真相」，耗2缕构想进入特殊诡谲断章",
                    "事件「扣响异音」中获得【时与光】（代价：年代更频繁，所有思绪负荷+1）",
                    "不持有【无终之钥】通过第6层【辉光天顶】",
                ],
                "key_relics": [
                    {"name": "先知长角", "item_id": "rogue_4_relic_final_2", "icon": ""},
                    {"name": "时与光", "item_id": "rogue_4_relic_final_4", "icon": ""},
                ],
                "final": "时光凯旋（霜星“冬痕”与爱国者）→ 圣城",
                "note": "", "has_guide": True,
            },
        ],
        "notes": ["表内另收录失败结局「无迹可寻」（特殊结算演出，不占常规结局编号）"],
        "source_note": "结局名单 ArknightsGameData · 攻略口径 PRTS Wiki",
        "version": VERSION,
    },
    "ark_rogue.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "total_clears": 48,
        "total_relics": 371,
        "records": [
            {
                "rogue_id": "rogue_7",
                "name": "沉沦者的黑流树海",
                "latest": True,
                "clear_time": 9,
                "bp_level": 65,
                "relic_cnt": 88,
                "bank_current": 1200,
                "bank_record": 4500,
                "medal_current": 22,
                "medal_total": 110,
            },
            {
                "rogue_id": "rogue_6",
                "name": "岁的界园志异",
                "latest": False,
                "clear_time": 27,
                "bp_level": 155,
                "relic_cnt": 186,
                "bank_current": 3240,
                "bank_record": 9999,
                "medal_current": 78,
                "medal_total": 120,
            },
            {
                "rogue_id": "rogue_5",
                "name": "水月与深蓝之树",
                "clear_time": 12,
                "bp_level": 80,
                "relic_cnt": 97,
                "bank_current": 860,
                "bank_record": 3200,
                "medal_current": 41,
                "medal_total": 108,
            },
        ],
        "version": VERSION,
    },
    "ark_cultivate.html": {
        "version": VERSION,
        "name": "阿米娅(术师)",
        "result": {
            "char": _CharStub(profession_name="术师", rarity_display=5),
            "current": {
                "evolve_phase": 1,
                "main_skill_lvl": 7,
                "masteries": [3, 3, 1],
                "equip_levels": [3],
            },
            "gap": {
                "gold_lack": 180000,
                "materials": [
                    {"id": "m1", "name": "D32钢", "rarity": "5★", "need": 4, "owned": 1, "lack": 3},
                    {"id": "m2", "name": "提纯源岩", "rarity": "4★", "need": 8, "owned": 2, "lack": 6},
                    {"id": "m3", "name": "酮阵列", "rarity": "4★", "need": 6, "owned": 2, "lack": 4},
                    {"id": "m4", "name": "技巧概要·卷3", "rarity": "4★", "need": 24, "owned": 5, "lack": 19},
                    {"id": "m5", "name": "晶体电子单元", "rarity": "5★", "need": 2, "owned": 0, "lack": 2},
                ],
                "materials_total": 5,
                "exp_books": [
                    {"name": "高级作战记录", "lack": 42, "need": 60, "owned": 18},
                ],
            },
            "warnings": ["不含等级经验曲线与龙门币等级消耗", "刷图推荐暂未接入"],
        },
    },
    "ark_wiki_mastery.html": {
        "name": "阿米娅",
        "rarity": "5",
        "profession": "术师",
        "skills": [
            {"name": "战术咏唱·γ型", "mastery_costs": [
                {"mastery": "专精一", "materials": [
                    {"name": "技巧概要·卷3", "count": "8", "icon": ""},
                    {"name": "固源岩组", "count": "5", "icon": ""},
                    {"name": "聚酸酯", "count": "4", "icon": ""},
                ]},
                {"mastery": "专精三", "materials": [
                    {"name": "技巧概要·卷3", "count": "15", "icon": ""},
                    {"name": "D32钢", "count": "6", "icon": ""},
                    {"name": "提纯源岩", "count": "6", "icon": ""},
                ]},
            ]},
            {"name": "精神爆发", "mastery_costs": [
                {"mastery": "专精一", "materials": [
                    {"name": "技巧概要·卷3", "count": "8", "icon": ""},
                    {"name": "RMA70-12", "count": "5", "icon": ""},
                ]},
                {"mastery": "专精三", "materials": [
                    {"name": "技巧概要·卷3", "count": "15", "icon": ""},
                    {"name": "晶体电子单元", "count": "4", "icon": ""},
                    {"name": "五水研磨石", "count": "6", "icon": ""},
                ]},
            ]},
        ],
        "source_url": "https://prts.wiki/w/阿米娅",
        "version": VERSION,
    },
    "ark_activity.html": {
        # 静态兜底（enrich 阶段整体替换为实时活动/卡池 + 官方横幅，生产同源）
        "activities": [
            {"title": "SideStory「挽歌燃烧殆尽」", "start": "08-09 16:00", "end": "08-30 03:59", "image": ""},
            {"title": "危机合约「弧光作战」", "start": "08-16 16:00", "end": "08-30 03:59", "image": ""},
        ],
        "pools": [
            {"title": "【限定寻访·庆典】「宿愿」", "start": "08-09 16:00", "end": "08-23 03:59", "image": ""},
        ],
        "version": VERSION,
    },
    "ark_announce.html": {
        # 静态兜底（enrich 阶段整体替换为森空岛实时公告列表 + 真实封面/头像）
        "anns": [
            {"title": "《明日方舟》2026年夏日嘉年华活动公告", "date": "08-09",
             "author": "", "cover": "", "avatar": "", "url": ""},
            {"title": "SideStory「挽歌燃烧殆尽」限时活动开启公告", "date": "08-07",
             "author": "", "cover": "", "avatar": "", "url": ""},
            {"title": "08月09日 16:00 闪断更新公告", "date": "08-08",
             "author": "", "cover": "", "avatar": "", "url": ""},
        ],
        "version": VERSION,
    },
    "ark_announce_detail.html": {
        # 静态兜底（enrich 阶段整体替换为真实公告详情：标题/头像/段落/配图/视频封面）
        "index": 2,
        "title": "SideStory「挽歌燃烧殆尽」限时活动开启公告",
        "date": "2026-08-07",
        "author": "明日方舟",
        "avatar": "",
        "ip": "上海",
        "url": "https://www.skland.com/article?id=6240522",
        "paragraphs": [
            "博士，SideStory「挽歌燃烧殆尽」现已开启！",
            "活动时间：08月07日 16:00 - 08月21日 03:59",
            "新增干员：维什戴尔（六星·狙击）、逻各斯（五星·术师）。",
            "完成活动关卡与任务可获取合成玉、物资筹备券与限定家具等奖励。",
        ],
        "images": [],
        "videos": [],
        "longImageCount": 0,
        "imageCount": 0,
        "videoCount": 0,
        "version": VERSION,
    },
    "ark_drop_rate.html": {
        "item_name": "固源岩组",
        "rows": [
            {"stage_code": "1-7", "zone": "第一章", "rate": 1.243, "ap_expect": 4.8,
             "times": 1250432, "rate_pct": 100},
            {"stage_code": "6-5", "zone": "第六章", "rate": 1.102, "ap_expect": 16.3,
             "times": 84213, "rate_pct": 89},
            {"stage_code": "7-6", "zone": "第七章", "rate": 1.057, "ap_expect": 17.0,
             "times": 51208, "rate_pct": 85},
            {"stage_code": "S4-1", "zone": "第四章", "rate": 0.986, "ap_expect": 18.3,
             "times": 33981, "rate_pct": 79},
            {"stage_code": "9-10", "zone": "第九章", "rate": 0.912, "ap_expect": 19.7,
             "times": 12450, "rate_pct": 73},
        ],
        "version": VERSION,
    },
    "ark_stage_rank.html": {
        "group": "主线",
        "rows": [
            {"stage_code": "1-7", "zone": "第一章", "spm": 4.86, "ap_cost": 6, "spm_pct": 100},
            {"stage_code": "JT8-3", "zone": "第八章", "spm": 4.52, "ap_cost": 18, "spm_pct": 93},
            {"stage_code": "R8-11", "zone": "第八章", "spm": 4.31, "ap_cost": 18, "spm_pct": 89},
            {"stage_code": "M8-8", "zone": "第八章", "spm": 4.12, "ap_cost": 18, "spm_pct": 85},
            {"stage_code": "9-19", "zone": "第九章", "spm": 3.98, "ap_cost": 21, "spm_pct": 82},
            {"stage_code": "10-6", "zone": "第十章", "spm": 3.77, "ap_cost": 21, "spm_pct": 78},
        ],
        "version": VERSION,
    },
    "ark_calendar.html": {
        "now_str": "2026-08-15 12:00",
        "game_version": "2.7.21",
        "game_updated": "2026-08-09",
        "maint_text": "下次维护：2026-08-22 06:00（还剩 6 天 18 小时）",
        # 静态兜底（enrich 阶段整体替换为实时活动/卡池 + 标题图，生产同源）
        "activities": [
            {"title": "SideStory「挽歌燃烧殆尽」", "remaining": "还剩 14 天", "start": "08-09 16:00", "end": "08-30 03:59", "image": ""},
            {"title": "危机合约「弧光作战」", "remaining": "还剩 15 天", "start": "08-16 16:00", "end": "08-30 03:59", "image": ""},
        ],
        "pools": [
            {"title": "【限定寻访·庆典】「宿愿」", "remaining": "还剩 7 天", "image": ""},
            {"title": "【中坚寻访】", "remaining": "还剩 13 天", "image": ""},
        ],
        "version": VERSION,
    },
    "ark_recruit.html": {
        # 大池样例（资深干员 55 人）：头像前 12 + 其余全名单 rest（生产同构）
        "tags": "资深干员",
        "total": 55,
        "rows": [
            {"rarity": 5, "name": "临光", "profession": "重装", "tags": "防护/治疗", "avatar": ""},
            {"rarity": 5, "name": "乌有", "profession": "特种", "tags": "输出/快速复活", "avatar": ""},
            {"rarity": 5, "name": "凛冬", "profession": "先锋", "tags": "费用回复/支援", "avatar": ""},
            {"rarity": 5, "name": "初雪", "profession": "辅助", "tags": "削弱", "avatar": ""},
            {"rarity": 5, "name": "华法琳", "profession": "医疗", "tags": "支援/治疗", "avatar": ""},
            {"rarity": 5, "name": "卡夫卡", "profession": "特种", "tags": "控场/输出", "avatar": ""},
            {"rarity": 5, "name": "红", "profession": "特种", "tags": "控场/快速复活/输出", "avatar": ""},
            {"rarity": 5, "name": "白面鸮", "profession": "医疗", "tags": "支援/治疗", "avatar": ""},
            {"rarity": 5, "name": "蓝毒", "profession": "狙击", "tags": "输出", "avatar": ""},
            {"rarity": 5, "name": "白金", "profession": "狙击", "tags": "输出", "avatar": ""},
            {"rarity": 5, "name": "雷蛇", "profession": "重装", "tags": "输出/防护", "avatar": ""},
            {"rarity": 5, "name": "赫默", "profession": "医疗", "tags": "治疗", "avatar": ""},
        ],
        "rest": [
            {"rarity": 5, "name": n}
            for n in [
                "德克萨斯", "守林人", "普罗旺斯", "火神", "诗怀雅", "食铁兽",
                "槐琥", "狮蝎", "崖心", "绮良", "梅尔", "格劳克斯", "送葬人", "灰喉",
                "布洛卡", "星极", "因陀罗", "月禾", "极境", "巫恋", "夜魔", "惊蛰",
                "爱丽丝", "贾维", "苇草", "断崖", "奥斯塔", "四月", "安哲拉", "孑",
                "陨星", "慑砂", "星熊", "可颂", "吽", "石棉", "洋灰",
            ]
        ],
        "truncated": True,
        "version": VERSION,
    },
    "ark_accounts.html": {
        "accounts": [
            {"index": 1, "nickname": "刀客塔不吃香菜", "uid": "12****89", "usable": True, "is_default": False},
            {"index": 2, "nickname": "小号只抽限定", "uid": "34****56", "usable": True, "is_default": True},
            {"index": 3, "nickname": "未记录昵称", "uid": "78****23", "usable": False, "is_default": False},
        ],
        "orphan_uids": [],
        "version": VERSION,
    },
    "ark_login.html": {
        "roles": [
            {"index": 1, "nickname": "刀客塔不吃香菜", "uid": "12****89", "is_default": True},
            {"index": 2, "nickname": "小号只抽限定", "uid": "34****56", "is_default": False},
        ],
        "version": VERSION,
    },
    "ark_skin.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "total": 513,
        "owned": 96,
        "pct": 18.7,
        "missing_count": 417,
        "extra_owned": 0,
        "recent": [
            {"name": "报童", "group": "忒斯特收藏/I", "char": "阿米娅", "ts": 1755000000, "avatar": ""},
            {"name": "探索者", "group": "时代系列", "char": "银灰", "ts": 1754000000, "avatar": ""},
            {"name": "慢性焦躁", "group": "纠察队先锋", "char": "陈", "ts": 1753000000, "avatar": ""},
            {"name": "文化滋养", "group": "裂岸狂欢", "char": "史尔特尔", "ts": 1752000000, "avatar": ""},
        ],
        "missing": [
            {"char": "凯尔希", "group": "_foldArts_", "name": "叙述者", "skin_id": "char_003_kalts@FoldArts#1"},
            {"char": "伊芙利特", "group": "忒斯特收藏/I", "name": "炎客", "skin_id": "char_134_ifrit@winter#1"},
            {"char": "临光", "group": "时代系列", "name": "璀璨新姿", "skin_id": "char_199_yak@epoch#1"},
            {"char": "缪尔赛思", "group": "生态园纪念", "name": "乡野游踪", "skin_id": "char_4045_molly@forest#1"},
        ],
        "version": VERSION,
    },
    "ark_gacha_sim.html": {
        "count": 10,
        "results": [
            {"rarity": 3, "name": "芬", "profession": "先锋", "avatar": "", "guarantee": False},
            {"rarity": 4, "name": "流星", "profession": "狙击", "avatar": "", "guarantee": False},
            {"rarity": 3, "name": "空爆", "profession": "术师", "avatar": "", "guarantee": False},
            {"rarity": 5, "name": "德克萨斯", "profession": "近卫", "avatar": "", "guarantee": False},
            {"rarity": 3, "name": "克洛丝", "profession": "狙击", "avatar": "", "guarantee": False},
            {"rarity": 4, "name": "白雪", "profession": "狙击", "avatar": "", "guarantee": False},
            {"rarity": 3, "name": "翎羽", "profession": "先锋", "avatar": "", "guarantee": False},
            {"rarity": 3, "name": "夜烟", "profession": "术师", "avatar": "", "guarantee": False},
            {"rarity": 3, "name": "斑点", "profession": "重装", "avatar": "", "guarantee": False},
            {"rarity": 4, "name": "梅", "profession": "狙击", "avatar": "", "guarantee": True},
        ],
        "summary": {3: 5, 4: 4, 5: 1, 6: 0},
        "version": VERSION,
    },
    "ark_cultivate_plan.html": {
        "uid": "12****89",
        "merged": {
            "chars": [
                {"name": "银灰", "rarity": 6, "current": {"evolve_phase": 2, "main_skill_lvl": 7, "masteries": [3, 0, 0]}, "gap_count": 6},
                {"name": "史尔特尔", "rarity": 6, "current": {"evolve_phase": 2, "main_skill_lvl": 7, "masteries": [1, 0, 0]}, "gap_count": 8},
                {"name": "42", "rarity": 6, "current": {"evolve_phase": 2, "main_skill_lvl": 6, "masteries": [0, 0, 0]}, "gap_count": 9},
            ],
            "materials": [
                {"id": "2004", "name": "D32钢", "rarity": "5★", "need": 8, "owned": 2, "lack": 6, "icon": ""},
                {"id": "30062", "name": "聚合剂", "rarity": "5★", "need": 6, "owned": 1, "lack": 5, "icon": ""},
                {"id": "2003", "name": "改量装置", "rarity": "4★", "need": 12, "owned": 5, "lack": 7, "icon": ""},
                {"id": "3003", "name": "聚酸酯组", "rarity": "3★", "need": 20, "owned": 8, "lack": 12, "icon": ""},
            ],
            "materials_total": 4,
            "exp_books": [
                {"name": "高级作战记录", "lack": 74, "icon": ""},
            ],
            "gold_lack": 386000,
            "gold_icon": "",
            "warnings": ["42 的技能顺序对位不全，专精按已持有技能数计算"],
        },
        "farm_rows": [
            {"name": "D32钢", "lack": 6, "advice": "直刷 13-4（约 18 把，2394 理智）；合成（叶子约 2210 理智）→ 合成更省"},
            {"name": "聚合剂", "lack": 5, "advice": "不可直刷，合成（叶子直刷约 1980 理智）"},
            {"name": "改量装置", "lack": 7, "advice": "直刷 10-4（约 9 把，720 理智）"},
            {"name": "聚酸酯组", "lack": 12, "advice": "直刷 S4-6（约 15 把，600 理智）；合成（叶子约 640 理智）→ 直刷更省"},
        ],
        "version": VERSION,
    },
    "ark_recruit_plan.html": {
        "input_tags": "高级资深干员 / 术师 / 输出 / 远程位 / 生存",
        "unknown_tags": "",
        "best": "高级资深干员 + 术师 + 输出",
        "rows": [
            {"tags": "高级资深干员 + 术师 + 输出", "tier": 1, "tier_name": "6★ 保底",
             "min_rarity": 6, "max_rarity": 6, "pool_count": 2,
             "names": ["刻俄柏", "异客"], "more": 0, "avatars": ["", ""]},
            {"tags": "高级资深干员 + 术师", "tier": 1, "tier_name": "6★ 保底",
             "min_rarity": 6, "max_rarity": 6, "pool_count": 5,
             "names": ["伊芙利特", "刻俄柏", "卡涅利安", "异客"], "more": 1, "avatars": ["", "", "", ""]},
            {"tags": "高级资深干员 + 生存", "tier": 1, "tier_name": "6★ 保底",
             "min_rarity": 6, "max_rarity": 6, "pool_count": 4,
             "names": ["山", "斯卡蒂", "森蚺", "泥岩"], "more": 0, "avatars": ["", "", "", ""]},
            {"tags": "高级资深干员 + 输出", "tier": 1, "tier_name": "6★ 保底",
             "min_rarity": 6, "max_rarity": 6, "pool_count": 12,
             "names": ["史尔特尔", "刻俄柏", "异客", "能天使"], "more": 8, "avatars": ["", "", "", ""]},
            {"tags": "输出 + 远程位", "tier": 4, "tier_name": "常规",
             "min_rarity": 3, "max_rarity": 5, "pool_count": 16,
             "names": ["守林人", "安哲拉", "四月", "流星"], "more": 12, "avatars": ["", "", "", ""]},
        ],
        "version": VERSION,
    },
    "ark_building.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "control_level": 5,
        "labor_value": 130,
        "labor_max": 200,
        "labor_pct": 65,
        "recruit_total": 4,
        "recruit_done": 1,
        "recruit_next": "约2小时15分",
        "training_pct": 62,
        "manufactures": [
            {"label": "制造站", "level": 3, "formula": "赤金×1", "speed_pct": 163,
             "members": [{"name": "温蒂", "char_id": ""}, {"name": "森蚺", "char_id": ""}, {"name": "澄闪", "char_id": ""}]},
            {"label": "制造站2", "level": 3, "formula": "赤金×1", "speed_pct": 111,
             "members": [{"name": "红云", "char_id": ""}, {"name": "泡泡", "char_id": ""}]},
            {"label": "制造站3", "level": 3, "formula": "高级作战记录×1", "speed_pct": 103,
             "members": [{"name": "夜烟", "char_id": ""}]},
        ],
        "tradings": [
            {"label": "贸易站", "level": 3, "strategy": "龙门币", "stock": 8, "stock_limit": 12,
             "members": [{"name": "能天使", "char_id": ""}, {"name": "德克萨斯", "char_id": ""}, {"name": "拉普兰德", "char_id": ""}]},
            {"label": "贸易站2", "level": 3, "strategy": "合成玉", "stock": 2, "stock_limit": 10,
             "members": [{"name": "雪雉", "char_id": ""}, {"name": "崖心", "char_id": ""}]},
        ],
        "power_count": 3,
        "power_levels": [3, 3, 3],
        "dormitories": [
            {"label": "宿舍1", "comfort": 5000, "comfort_pct": 100},
            {"label": "宿舍2", "comfort": 4980, "comfort_pct": 100},
            {"label": "宿舍3", "comfort": 4320, "comfort_pct": 86},
            {"label": "宿舍4", "comfort": 3145, "comfort_pct": 63},
        ],
        "clue_own": 9,
        "clue_board": ["企鹅物流", "喀兰贸易", "莱茵生命"],
        "clue_daily_reward": True,
        "training_trainee": "史尔特尔",
        "training_remain": "剩约2小时15分",
        "tired_chars": [
            {"name": "阿米娅", "room": "控制中枢", "morale": 0.0},
            {"name": "濯尘芙蓉", "room": "控制中枢", "morale": 0.0},
            {"name": "诗怀雅", "room": "控制中枢", "morale": 3.2},
            {"name": "假日威龙陈", "room": "制造站", "morale": 5.1},
            {"name": "浊心斯卡蒂", "room": "制造站", "morale": 5.1},
            {"name": "鸿雪", "room": "贸易站", "morale": 6.6},
            {"name": "讯使", "room": "贸易站", "morale": 6.6},
            {"name": "角峰", "room": "贸易站2", "morale": 9.6},
        ],
        "tired_more": 3,
        "version": VERSION,
    },
    "ark_training.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "trainee": "史尔特尔",
        "portrait": "",
        "avatar": "",
        "skill_name": "黄昏",
        "skill_label": "3技能",
        "mastery_from": 2,
        "mastery_to": 3,
        "trainer": "阿",
        "trainer_avatar": "",
        "state": "受训中",
        "state_en": "TRAINING",
        "remain_text": "约2小时15分",
        "finish_at": "08-30 02:15",
        "pct": 88,
        "base_exact": True,
        "speed": 1.3,
        "version": VERSION,
    },
    "ark_fortune.html": {
        "date": "2026-08-30",
        "level": "大吉",
        "tone": "good",
        "char": "维什戴尔",
        "avatar": "",
        "rarity": 6,
        "profession": "SNIPER",
        "good": ["公开招募", "打肉鸽", "专精训练"],
        "bad": ["凌晨四点抽卡", "越级打 N15"],
        "version": VERSION,
    },
}


async def render_one(render_html, ark_templates, name: str, context: dict) -> bool:
    img = await render_html(ark_templates, name, dict(context))
    if not img:
        print(f"❌ {name}: 渲染返回 None")
        return False
    out = OUT_DIR / name.replace(".html", ".jpg")
    out.write_bytes(img)
    print(f"✅ {name} → {out.name} ({len(img)} bytes)")
    return True


async def _enrich_icons(name: str, context: dict) -> None:
    """给材料相关 mock 挂上真实 PRTS 道具图标（下载一次后走磁盘缓存）。"""
    from ArknightsUID.utils.gamedata.itemicon import item_icon_b64

    if name == "ark_cultivate.html":
        gap = context["result"]["gap"]
        for row in gap["materials"] + gap["exp_books"]:
            row["icon"] = await item_icon_b64(row["name"])
        if gap.get("gold_lack"):
            gap["gold_icon"] = await item_icon_b64("龙门币")
    elif name == "ark_wiki_mastery.html":
        for skill in context["skills"]:
            for cost in skill["mastery_costs"]:
                for mat in cost["materials"]:
                    mat["icon"] = await item_icon_b64(mat["name"])
    elif name == "ark_drop_rate.html":
        context["item_icon"] = await item_icon_b64(context["item_name"])
    elif name == "ark_wiki_enemy.html":
        from ArknightsUID.utils.gamedata.itemicon import enemy_icon_b64

        context["icon"] = await enemy_icon_b64(context["name"])
        # 机制分块与正式链路同源（_build_enemy_context 的拆分逻辑）
        from ArknightsUID.arknightsuid_wiki import _ability_blocks

        context["ability_blocks"] = _ability_blocks(context.get("abilities") or [])
        for variant in context.get("variants", []):
            variant["blocks"] = _ability_blocks(variant.get("abilities") or [])
    elif name == "ark_rogue_enemy.html":
        from ArknightsUID.utils.gamedata.rogue_enemy import enemy_icon_b64

        if context.get("enemy_key"):
            context["icon"] = await enemy_icon_b64(context["enemy_key"])
    elif name == "ark_rogue_stage.html":
        from ArknightsUID.utils.gamedata.rogue_enemy import enemy_icon_b64

        for row in context.get("rows", []):
            row["icon"] = await enemy_icon_b64(row.get("key") or "")
    elif name == "ark_rogue_relic.html":
        from ArknightsUID.utils.gamedata.rogue_relic import relic_icon_b64

        for row in context.get("rows", []):
            row["icon"] = ""
        # 首行用已知存在的图标（钝爪-典训 rogue_5 档）
        if context.get("rows"):
            context["rows"][0]["icon"] = await relic_icon_b64("rogue_5_relic_legacy_1")
    elif name == "ark_rogue_ending.html":
        # 关键藏品图标 + 期主题 KV（与生产 _build_rogue_ending_context 同源）
        from ArknightsUID.utils.gamedata.rogue_relic import relic_icon_b64

        for e in context.get("endings", []):
            for chip in e.get("key_relics") or []:
                chip["icon"] = await relic_icon_b64(chip.get("item_id") or "")
        try:
            from ArknightsUID.utils.prts.client import activity_banner_urls
            from ArknightsUID.utils.render import get_image_b64_with_cache
            from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

            banner_urls = await activity_banner_urls([context["theme_name"]])
            url = banner_urls.get(str(context["theme_name"]), "")
            context["banner"] = (
                await get_image_b64_with_cache(
                    url, CACHE_PATH / "rogue_theme", quality=82
                )
                if url
                else ""
            )
        except Exception as e:
            print(f"⚠️  ark_rogue_ending 主题图拉取失败，按无图降级: {e}")
    elif name == "ark_wiki_stage.html":
        # 真实关卡地图（md5 直链 + 磁盘缓存，与生产同一取图函数）
        from ArknightsUID.arknightsuid_wiki import _get_stage_map_b64

        context["map_image"] = await _get_stage_map_b64(
            context["title"], context["name"]
        )
        # 敌人头像（头像_敌人_{名}.png，失败空串降级）
        from ArknightsUID.utils.gamedata.itemicon import enemy_icon_b64

        for row in context.get("enemies", []):
            row["icon"] = await enemy_icon_b64(row["name"])
    elif name in ("ark_recruit.html", "ark_gacha_card.html"):
        # 行内小头像：精确名反查 charId → 初始头像
        from ArknightsUID.utils.gamedata.charinfo import load_char_index
        from ArknightsUID.utils.player.context import char_avatar_b64

        char_index = await load_char_index()
        rows_key = "rows" if name == "ark_recruit.html" else "recent"
        for row in context.get(rows_key, []):
            hit = next(
                (cid for cid, info in char_index.items()
                 if info.get("name") == row.get("name")),
                "",
            )
            row["avatar"] = await char_avatar_b64(hit) if hit else ""

    if name == "ark_craft.html":
        # 目标材料图标 + 首两层子材料图标（与生产 show_craft_tree 同规则）
        context["icon"] = await item_icon_b64(context["name"])
        for row in context.get("rows", []):
            if int(row.get("depth") or 0) <= 1:
                for c in row.get("costs", []):
                    c["icon"] = await item_icon_b64(c.get("name") or "")

    if name == "ark_module.html":
        # 模组图标（PRTS torappu）+ 解锁材料图标（与生产 send_module_info 同源）
        from ArknightsUID.utils.path import EQUIP_CACHE_PATH
        from ArknightsUID.utils.player.context import equip_icon_url
        from ArknightsUID.utils.render import get_image_b64_with_cache

        for m in context.get("modules", []):
            m["icon"] = await get_image_b64_with_cache(
                equip_icon_url(str(m.get("id") or "")), EQUIP_CACHE_PATH
            )
            for c in m.get("costs", []):
                c["icon"] = await item_icon_b64(c.get("name") or "")

    if name == "ark_rogue.html":
        # 每期主题 KV（PRTS 主题页标题图，与生产 _fill_rogue_theme_images 同源）
        try:
            from ArknightsUID.utils.prts.client import activity_banner_urls
            from ArknightsUID.utils.render import get_image_b64_with_cache
            from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

            names = [str(r.get("name") or "") for r in context.get("records", [])]
            banner_urls = await activity_banner_urls(names)
            for r in context.get("records", []):
                url = banner_urls.get(str(r.get("name") or ""), "")
                r["image"] = (
                    await get_image_b64_with_cache(
                        url, CACHE_PATH / "rogue_theme", quality=82
                    )
                    if url
                    else ""
                )
        except Exception as e:
            print(f"⚠️  ark_rogue 主题图拉取失败，按无图降级: {e}")

    if name == "ark_activity.html":
        # 实时活动/卡池 + 官方标题图（与生产 show_ark_activities 同源），
        # 失败保留静态占位（无图行，与缺图降级一致）
        try:
            from ArknightsUID.utils.prts.client import (
                activity_banner_urls,
                query_activities,
                query_gacha_pools,
            )
            from ArknightsUID.utils.render import get_image_b64_with_cache
            from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

            activities = await query_activities()
            pools = await query_gacha_pools()
            banner_urls = await activity_banner_urls(
                [a.title for a in activities]
            )

            async def _act_b64(url: str) -> str:
                return (
                    await get_image_b64_with_cache(
                        url, CACHE_PATH / "activity", quality=82
                    )
                    if url
                    else ""
                )

            acts = [
                {"title": a.title, "start": a.start, "end": a.end,
                 "image": await _act_b64(banner_urls.get(a.title, ""))}
                for a in activities
            ]
            pls = [
                {"title": p.title, "start": p.start, "end": p.end,
                 "image": await _act_b64(p.image_url)}
                for p in pools
            ]
            if acts:
                context["activities"] = acts
            if pls:
                context["pools"] = pls
        except Exception as e:
            print(f"⚠️  ark_activity 实时数据失败，保留静态占位: {e}")

    if name == "ark_calendar.html":
        # 实时活动/卡池 + 标题图（与生产 send_calendar 同源），失败保留静态占位
        try:
            from datetime import datetime, timedelta, timezone

            from ArknightsUID.arknightsuid_wiki import _format_remaining
            from ArknightsUID.utils.prts.client import (
                activity_banner_urls,
                query_activities,
                query_gacha_pools,
            )
            from ArknightsUID.utils.render import get_image_b64_with_cache
            from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

            now_ts = datetime.now(timezone(timedelta(hours=8))).timestamp()
            activities = await query_activities()
            pools = await query_gacha_pools()
            banner_urls = await activity_banner_urls(
                [a.title for a in activities]
            )

            async def _cal_b64(url: str) -> str:
                return (
                    await get_image_b64_with_cache(
                        url, CACHE_PATH / "activity", quality=82
                    )
                    if url
                    else ""
                )

            acts = [
                {"title": a.title,
                 "remaining": _format_remaining(a.end_ts, now_ts),
                 "start": a.start, "end": a.end,
                 "image": await _cal_b64(banner_urls.get(a.title, ""))}
                for a in activities
            ]
            pls = [
                {"title": p.title,
                 "remaining": _format_remaining(p.end_ts, now_ts),
                 "image": await _cal_b64(p.image_url)}
                for p in pools
            ]
            if acts:
                context["activities"] = acts
            if pls:
                context["pools"] = pls
        except Exception as e:
            print(f"⚠️  ark_calendar 实时数据失败，保留静态占位: {e}")

    if name == "ark_announce.html":
        # 森空岛实时公告列表 + 真实封面/头像（与生产 show_ark_announcements 同源）
        try:
            from ArknightsUID.arknightsuid_push import get_ark_ann_list
            from ArknightsUID.utils.render import get_image_b64_with_cache
            from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

            anns = await get_ark_ann_list()
            if anns:
                rows = []
                for ann in anns:
                    rows.append({
                        "title": ann["title"],
                        "date": ann.get("date", ""),
                        "url": ann.get("url", ""),
                        "author": ann.get("author", ""),
                        "cover": await get_image_b64_with_cache(
                            ann.get("cover", ""), CACHE_PATH / "announce",
                            quality=60, cover_size=(400, 200),
                        ),
                        "avatar": await get_image_b64_with_cache(
                            ann.get("avatar", ""), CACHE_PATH / "announce",
                            quality=70,
                        ),
                    })
                context["anns"] = rows
        except Exception as e:
            print(f"⚠️  ark_announce 实时列表失败，保留静态占位: {e}")

    if name == "ark_announce_detail.html":
        # 真实公告详情（森空岛 web 免登录，优先挑带配图的一条；
        # 与生产 _show_ann_detail 同源：普通图内嵌、视频封面占位、超长图只计数）
        try:
            from ArknightsUID.arknightsuid_push import (
                get_ann_detail,
                get_ark_ann_list,
                split_ann_images,
            )
            from ArknightsUID.utils.render import get_image_b64_with_cache
            from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

            async def _ann_b64(url: str, quality: int = 80) -> str:
                return await get_image_b64_with_cache(
                    url, CACHE_PATH / "announce", quality=quality
                )

            anns = await get_ark_ann_list()
            detail = None
            detail_index = 0
            for i, ann in enumerate(anns or [], start=1):
                d = await get_ann_detail(ann["id"])
                if d and (d.get("images") or []):
                    detail, detail_index = d, i
                    break
            if detail:
                normal_images, long_urls = split_ann_images(detail)
                card_images = []
                for img in normal_images:
                    b64 = await _ann_b64(img["url"])
                    if b64:
                        card_images.append({
                            "b64": b64,
                            "width": img["width"],
                            "height": img["height"],
                        })
                video_covers = []
                for v in detail.get("videos") or []:
                    b64 = await _ann_b64(str(v.get("coverUrl") or ""))
                    if b64:
                        video_covers.append({"b64": b64})
                context.update({
                    "index": detail_index,
                    "title": detail.get("title", ""),
                    "date": detail.get("date", ""),
                    "author": detail.get("userName", ""),
                    "avatar": await _ann_b64(
                        str(detail.get("userAvatar") or ""), 70
                    ),
                    "ip": detail.get("userIpLocation", ""),
                    "url": (
                        "https://www.skland.com/article"
                        f"?id={detail.get('id', '')}"
                    ),
                    "paragraphs": [
                        p[:400]
                        for p in (detail.get("paragraphs") or [])[:30]
                    ],
                    "images": card_images,
                    "videos": video_covers,
                    "longImageCount": len(long_urls),
                    "imageCount": len(detail.get("images") or []),
                    "videoCount": len(detail.get("videos") or []),
                })
        except Exception as e:
            print(f"⚠️  ark_announce_detail 真实公告失败，保留静态占位: {e}")

    # 头像增强（面板模拟卡用 char_id 换头像）
    if name == "ark_op_panel.html":
        from ArknightsUID.utils.player.context import char_avatar_b64

        if context.get("char_id"):
            context["avatar"] = await char_avatar_b64(context["char_id"])

    # 立绘是独立增强位（与材料图标 elif 链不冲突，可同时命中）
    if name in ("ark_char_card.html", "ark_cultivate.html", "ark_wiki_mastery.html", "ark_wiki_operator.html"):
        from ArknightsUID.utils.player.context import char_portrait_b64

        context["portrait"] = await char_portrait_b64("char_002_amiya", 5)

    if name == "ark_char_card.html":
        # 模组图标（PRTS torappu 资产）
        from ArknightsUID.utils.path import EQUIP_CACHE_PATH
        from ArknightsUID.utils.player.context import equip_icon_url
        from ArknightsUID.utils.render import get_image_b64_with_cache

        for eq in context.get("equips", []):
            eq["icon"] = await get_image_b64_with_cache(
                equip_icon_url("uniequip_002_amiya"), EQUIP_CACHE_PATH
            )

    if name in ("ark_wiki_operator.html", "ark_wiki_mastery.html"):
        # 技能图标：技能名反查 skill_id → hycdn
        from ArknightsUID.utils.gamedata.charinfo import load_skill_names
        from ArknightsUID.utils.path import SKILL_CACHE_PATH
        from ArknightsUID.utils.player.context import skill_icon_url
        from ArknightsUID.utils.render import get_image_b64_with_cache

        skill_names = await load_skill_names()
        for s in context.get("skills", []):
            sid = next((k for k, v in skill_names.items() if v == s.get("name")), "")
            if sid:
                s["icon"] = await get_image_b64_with_cache(
                    skill_icon_url(sid), SKILL_CACHE_PATH
                )

    if name == "ark_wiki_stage.html":
        # 掉落物图标（PRTS 带框道具图）
        for row in context.get("drops", []) + context.get("extra_drops", []):
            row["icon"] = await item_icon_b64(row["name"])

    if name == "ark_char_stat.html":
        # 职业图标（PRTS 图标_职业_*）
        from ArknightsUID.utils.gamedata.itemicon import profession_icon_b64

        for p in context.get("professions", []):
            p["icon"] = await profession_icon_b64(p["name"])

    if name == "ark_box_analysis.html":
        # 六星头像墙（当前皮肤头像）
        from ArknightsUID.utils.player.context import char_avatar_b64

        six_ids = [
            ("char_1035_wisdel", "维什戴尔"),
            ("char_4133_logos", "逻各斯"),
            ("char_2013_cerber", "刻俄柏"),
            ("char_134_ifrit", "伊芙利特"),
            ("char_180_amgoat", "艾雅法拉"),
            ("char_4121_zuole", "左乐"),
        ]
        six_rows = []
        for cid, cname in six_ids:
            six_rows.append({"name": cname, "avatar": await char_avatar_b64(cid)})
        context["six_rows"] = six_rows

    if name == "ark_help.html":
        # 直接用生产 HELP_GROUPS（单一事实源：预览永远与帮助卡同步）
        # + 逐命令 Q 版干员图标（与生产同一分配函数）
        from ArknightsUID.arknightsuid_help import HELP_GROUPS, assign_group_icons

        context["groups"] = assign_group_icons(HELP_GROUPS)

    if name == "ark_recruit_plan.html":
        # 每个组合的池内前 4 干员头像（名称反查 charId）
        from ArknightsUID.utils.gamedata.charinfo import load_char_index
        from ArknightsUID.utils.player.context import char_avatar_b64

        char_index = await load_char_index()

        def _cid_of(char_name: str) -> str:
            return next(
                (cid for cid, info in char_index.items()
                 if info.get("name") == char_name),
                "",
            )

        for row in context.get("rows", []):
            avatars = []
            for char_name in row.get("names", [])[:4]:
                hit = _cid_of(str(char_name))
                avatars.append(await char_avatar_b64(hit) if hit else "")
            row["avatars"] = avatars

    if name in ("ark_gacha_sim.html", "ark_skin.html"):
        # 头像：干员名反查 charId → 初始头像（皮肤卡的 recent 用干员头像代）
        from ArknightsUID.utils.gamedata.charinfo import load_char_index
        from ArknightsUID.utils.player.context import char_avatar_b64

        char_index = await load_char_index()

        def _cid_of(char_name: str) -> str:
            return next(
                (cid for cid, info in char_index.items()
                 if info.get("name") == char_name),
                "",
            )

        if name == "ark_gacha_sim.html":
            for row in context.get("results", []):
                hit = _cid_of(str(row.get("name", "")))
                row["avatar"] = await char_avatar_b64(hit) if hit else ""
        else:
            for row in context.get("recent", []):
                hit = _cid_of(str(row.get("char", "")))
                row["avatar"] = await char_avatar_b64(hit) if hit else ""

    if name == "ark_building.html":
        # 房间成员头像：名称反查 charId → 初始头像
        from ArknightsUID.utils.gamedata.charinfo import load_char_index
        from ArknightsUID.utils.player.context import char_avatar_b64

        char_index = await load_char_index()
        for room in context.get("manufactures", []) + context.get("tradings", []):
            for m in room.get("members", []):
                hit = next(
                    (cid for cid, info in char_index.items()
                     if info.get("name") == m.get("name")),
                    "",
                )
                m["avatar"] = await char_avatar_b64(hit) if hit else ""

    if name == "ark_fortune.html":
        # 同行干员头像：干员名反查 charId（与生产 send_daily_fortune 同口径）
        from ArknightsUID.utils.gamedata.charinfo import load_char_index
        from ArknightsUID.utils.player.context import char_avatar_b64

        char_index = await load_char_index()
        hit = next(
            (cid for cid, info in char_index.items()
             if info.get("name") == context.get("char")),
            "",
        )
        if hit:
            context["avatar"] = await char_avatar_b64(hit)

    if name == "ark_training.html":
        # 受训干员立绘/头像 + 协助干员头像：干员名反查 charId（生产同口径）
        from ArknightsUID.utils.gamedata.charinfo import load_char_index
        from ArknightsUID.utils.player.context import (
            char_avatar_b64,
            char_portrait_b64,
        )

        char_index = await load_char_index()

        def _cid_of(char_name: str) -> str:
            return next(
                (cid for cid, info in char_index.items()
                 if info.get("name") == char_name),
                "",
            )

        hit = _cid_of(str(context.get("trainee") or ""))
        if hit:
            rarity = int(char_index[hit].get("rarity") or 0)
            context["portrait"] = await char_portrait_b64(hit, rarity)
            context["avatar"] = await char_avatar_b64(hit)
        trainer_hit = _cid_of(str(context.get("trainer") or ""))
        if trainer_hit:
            context["trainer_avatar"] = await char_avatar_b64(trainer_hit)


async def main() -> int:
    from ArknightsUID.utils.render import ark_templates, render_html

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    selected = sys.argv[1:] or list(MOCK_CONTEXTS)
    start = time.time()
    ok = 0
    for key in selected:
        name = key if key.endswith(".html") else f"{key}.html"
        context = MOCK_CONTEXTS.get(name)
        if context is None:
            print(f"⚠️  无假数据：{name}，跳过")
            continue
        await _enrich_icons(name, context)
        ok += await render_one(render_html, ark_templates, name, context)

    print(f"\n完成：{ok}/{len(selected)}，耗时 {time.time() - start:.1f}s，输出目录 {OUT_DIR}")
    return 0 if ok == len(selected) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
