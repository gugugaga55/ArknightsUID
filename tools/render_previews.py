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
        "has_stats": True,
        "max_hp": "45000",
        "atk": "1200",
        "defense": "1500",
        "res": "60",
        "has_level2": True,
        "lv2_hp": "60000",
        "lv2_atk": "1600",
        "lv2_defense": "1500",
        "lv2_res": "60",
        "weight": "6",
        "abilities": [
            "行军姿态：防御力大幅提升，阻挡数+1",
            "毁灭姿态：攻击力大幅提升，攻击造成范围伤害",
            "被击败时释放冲击波，对全场造成真实伤害",
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
            {"name": "士兵", "count": 18, "level": "", "hp": "1650", "atk": "200", "defense": "100", "res": "0"},
            {"name": "弩手", "count": 12, "level": "", "hp": "1550", "atk": "240", "defense": "100", "res": "0"},
            {"name": "暴徒", "count": 8, "level": "", "hp": "5250", "atk": "700", "defense": "100", "res": "0"},
            {"name": "机动盾兵", "count": 6, "level": "", "hp": "4000", "atk": "200", "defense": "1000", "res": "0"},
        ],
        "assault_diffs": ["敌人的生命值与攻击力提升 20%"],
        "map_image": "",
        "map_url": "",
        "source_url": "https://prts.wiki/w/1-7",
        "version": VERSION,
    },
    "ark_rogue.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "records": [
            {
                "rogue_id": "rogue_2",
                "name": "傀影与猩红孤钻",
                "clear_time": 27,
                "bp_level": 155,
                "relic_cnt": 186,
                "bank_current": 3240,
                "bank_record": 9999,
                "medal_current": 78,
                "medal_total": 120,
            },
            {
                "rogue_id": "rogue_3",
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
        "activities": [
            {"title": "SideStory「挽歌燃烧殆尽」", "start": "08-09 16:00", "end": "08-30 03:59"},
            {"title": "危机合约「弧光作战」", "start": "08-16 16:00", "end": "08-30 03:59"},
            {"title": "「促融共竞」#2", "start": "08-22 16:00", "end": "09-05 03:59"},
        ],
        "pools": [
            {"title": "【限定寻访·庆典】「宿愿」", "start": "08-09 16:00", "end": "08-23 03:59"},
            {"title": "【中坚寻访】", "start": "08-15 04:00", "end": "08-29 03:59"},
        ],
        "version": VERSION,
    },
    "ark_announce.html": {
        "anns": [
            {"title": "《明日方舟》2026年夏日嘉年华活动公告", "date": "08-09", "url": ""},
            {"title": "SideStory「挽歌燃烧殆尽」限时活动开启公告", "date": "08-07", "url": ""},
            {"title": "08月09日 16:00 闪断更新公告", "date": "08-08", "url": ""},
            {"title": "「弧光作战」危机合约开启公告", "date": "08-14", "url": ""},
            {"title": "新干员追加：维什戴尔、逻各斯", "date": "08-05", "url": ""},
        ],
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
        "activities": [
            {"title": "SideStory「挽歌燃烧殆尽」", "remaining": "还剩 14 天", "start": "08-09 16:00", "end": "08-30 03:59"},
            {"title": "危机合约「弧光作战」", "remaining": "还剩 15 天", "start": "08-16 16:00", "end": "08-30 03:59"},
        ],
        "pools": [
            {"title": "【限定寻访·庆典】「宿愿」", "remaining": "还剩 7 天"},
            {"title": "【中坚寻访】", "remaining": "还剩 13 天"},
        ],
        "version": VERSION,
    },
    "ark_recruit.html": {
        "tags": "高级资深干员 + 术师",
        "total": 4,
        "rows": [
            {"rarity": 6, "name": "逻各斯", "profession": "术师", "tags": "输出/削弱"},
            {"rarity": 6, "name": "莫斯提马", "profession": "术师", "tags": "群攻/减速"},
            {"rarity": 6, "name": "刻俄柏", "profession": "术师", "tags": "输出"},
            {"rarity": 6, "name": "伊芙利特", "profession": "术师", "tags": "群攻/输出"},
        ],
        "truncated": False,
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
    "ark_building.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "control_level": 5,
        "labor_value": 130,
        "labor_max": 200,
        "labor_pct": 65,
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
        # 装饰浮空立绘（阿米娅，透明 PNG）
        from ArknightsUID.utils.player.context import char_portrait_b64

        context["figure"] = await char_portrait_b64("char_002_amiya", 5)

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
