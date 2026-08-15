"""「罗德岛作战终端」独立设计探索稿渲染脚本（ZCode 自研视觉，非生产模板）。

与 tools/render_previews.py（生产「纸面信号」体系假数据预览）互不影响：
模板在 templates/alt/，输出到 docs/design/alt_previews/，供对比评审。

运行：在 gsuid_core venv 下
    python tools/render_alt_previews.py            # 渲染全部 5 张
    python tools/render_alt_previews.py status     # 只渲染指定卡
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

OUT_DIR = Path(__file__).parents[1] / "docs" / "design" / "alt_previews"
VERSION = "v0.4.0-terminal"

MOCK_CONTEXTS = {
    "alt/status.html": {
        "name": "刀客塔不吃香菜",
        "level": 120,
        "uid": "12****89",
        "ap_current": 45,
        "ap_max": 135,
        "ap_pct": 33,
        "ap_full_at": "08-14 21:07",
        "ap_recover_in": "2h 41m",
        "reg_days": 2150,
        "chars_total": 285,
        "campaign_current": 1200,
        "campaign_total": 1800,
        "version": VERSION,
    },
    "alt/char.html": {
        "name": "银灰",
        "rarity": 6,
        "profession": "近卫 WARRIOR",
        "avatar": "",
        "level": 90,
        "max_level": 90,
        "elite": 2,
        "potential": 6,
        "favor": 200,
        "main_skill_lvl": 7,
        "skills": [
            {"icon": "", "name": "强力击·γ型", "specialize": 3},
            {"icon": "", "name": "雪境强袭", "specialize": 3},
            {"icon": "", "name": "真银斩", "specialize": 2},
        ],
        "equips": [
            {"name": "模组 X · 剑影", "stage": 3},
        ],
        "version": VERSION,
    },
    "alt/gacha.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "total": 1286,
        "six_count": 34,
        "six_rate": "2.64",
        "avg_six": "37.8",
        "pity": 23,
        "pity_max": 80,
        "recent": [
            {"name": "维什戴尔", "pool": "谎言的回响", "pull": 62},
            {"name": "逻各斯", "pool": "标准寻访", "pull": 41},
            {"name": "纯烬艾雅法拉", "pool": "火山旅梦", "pull": 78},
            {"name": "赫德雷", "pool": "联合行动", "pull": 12},
        ],
        "version": VERSION,
    },
    "alt/cultivate.html": {
        "name": "逻各斯",
        "profession": "术师 CASTER",
        "current": {"evolve_phase": 1, "main_skill_lvl": 5, "masteries": [1, 0, 0], "equip_levels": [1]},
        "gap": {
            "materials": [
                {"name": "D32钢", "rarity": "5★", "need": 6, "owned": 2, "lack": 4},
                {"name": "晶体电子单元", "rarity": "5★", "need": 4, "owned": 0, "lack": 4},
                {"name": "聚合凝胶", "rarity": "4★", "need": 12, "owned": 5, "lack": 7},
                {"name": "改量装置", "rarity": "4★", "need": 8, "owned": 6, "lack": 2},
                {"name": "固源岩组", "rarity": "4★", "need": 24, "owned": 19, "lack": 5},
                {"name": "技巧概要·卷三", "rarity": "3★", "need": 36, "owned": 11, "lack": 25},
            ],
            "materials_total": 9,
            "exp_books": [{"name": "高级作战记录", "lack": 47}],
            "gold_lack": 326000,
        },
        "version": VERSION,
    },
    "alt/campaign.html": {
        "name": "刀客塔不吃香菜",
        "uid": "12****89",
        "reward_current": 1440,
        "reward_total": 1800,
        "reward_pct": 80,
        "stages": [
            {"name": "切尔诺伯格", "progress": 400, "total": 400},
            {"name": "龙门外环", "progress": 400, "total": 400},
            {"name": "龙门市区", "progress": 355, "total": 400},
            {"name": "长泉镇郊外", "progress": 245, "total": 400},
        ],
        "version": VERSION,
    },
}


async def main() -> int:
    from ArknightsUID.utils.render import ark_templates, render_html

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    selected = sys.argv[1:] or list(MOCK_CONTEXTS)
    start = time.time()
    ok = 0
    for key in selected:
        name = key if key.endswith(".html") else f"alt/{key}.html"
        context = MOCK_CONTEXTS.get(name)
        if context is None:
            print(f"⚠️  无假数据：{name}，跳过")
            continue
        img = await render_html(ark_templates, name, dict(context))
        if not img:
            print(f"❌ {name}: 渲染返回 None")
            continue
        out = OUT_DIR / name.replace("alt/", "").replace(".html", ".jpg")
        out.write_bytes(img)
        print(f"✅ {name} → {out.name} ({len(img)} bytes)")
        ok += 1
    print(f"\n完成：{ok}/{len(selected)}，耗时 {time.time() - start:.1f}s，输出目录 {OUT_DIR}")
    return 0 if ok == len(selected) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
