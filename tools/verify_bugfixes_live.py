"""实机验证脚本：修复后的代码对真实数据/真实网络复跑当初全部复现场景。

运行：cd /home/yugugu/core/gsuid_core && .venv/bin/python gsuid_core/plugins/ArknightsUID/tools/verify_bugfixes_live.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


async def main() -> None:
    # ---- 1. 高危#1：真实敌人库 140 个 None atk_type 敌人，tags 不再炸 ----
    from ArknightsUID.arknightsuid_rogue import _build_rogue_enemy_context
    ctx = await _build_rogue_enemy_context("深池逐火战士")
    ok = isinstance(ctx, dict) and ctx.get("name") == "深池逐火战士"
    check("rogue敌人 None atk_type 不再 TypeError", ok,
          f"type={type(ctx).__name__}" + (f" tags={ctx['tags']}" if ok else f" -> {str(ctx)[:60]}"))

    # ---- 2. #10：岁 = BOSS 本体，不再被期别名吃掉 ----
    ctx2 = await _build_rogue_enemy_context("岁")
    ok2 = isinstance(ctx2, dict) and ctx2.get("name") == "岁"
    check("ark肉鸽敌人 岁 可查询", ok2,
          "" if ok2 else f" -> {str(ctx2)[:80]}")
    ctx2b = await _build_rogue_enemy_context("岁 N12")
    ok2b = isinstance(ctx2b, dict) and ctx2b.get("name") == "岁"
    check("ark肉鸽敌人 岁 N12 可查询", ok2b,
          "" if ok2b else f" -> {str(ctx2b)[:80]}")

    # ---- 3. #13：同期重名关卡 variants ----
    from ArknightsUID.utils.gamedata.rogue_enemy import find_stage
    stage = await find_stage("止变")
    check("重名关卡 variants≥5（ro5 止变）",
          bool(stage) and stage["variants"] >= 5,
          f"variants={stage['variants'] if stage else None}")

    # ---- 4. #6：水月 N3 源石虫法抗 +10（真实难度表）----
    from ArknightsUID.utils.gamedata.rogue_enemy import (
        _grade_mods_for_enemy, _stats_with_mods, load_difficulty_mods, load_enemy_db,
    )
    db = await load_enemy_db()
    diffs = await load_difficulty_mods()
    src = next(k for k, v in db.items() if v.get("name_zh") == "源石虫")
    entry = db[src]
    grades = diffs.get("mizuki") or []
    mods3 = _grade_mods_for_enemy(grades, src, list(entry.get("type") or []), 3)
    stats3 = _stats_with_mods((entry.get("stats") or [{}])[0], mods3, 3)
    mods0 = _grade_mods_for_enemy(grades, src, list(entry.get("type") or []), 0)
    stats0 = _stats_with_mods((entry.get("stats") or [{}])[0], mods0, 0)
    check("水月 N3 源石虫 res=10（add 修正生效）", stats3["res"] == stats0["res"] + 10,
          f"N0 res={stats0['res']} N3 res={stats3['res']}")
    mods12 = _grade_mods_for_enemy(grades, src, list(entry.get("type") or []), 12)
    stats12 = _stats_with_mods((entry.get("stats") or [{}])[0], mods12, 12)
    check("水月 N12 源石虫 res=20（两条 add 累积）", stats12["res"] == stats0["res"] + 20,
          f"N12 res={stats12['res']}")

    # ---- 5. #2：真实 PRTS 语音页，嵌套模板不丢条目不错位 ----
    from ArknightsUID.utils.prts.client import query_voice_lines
    lines = await query_voice_lines("阿米娅")
    ok5 = bool(lines) and len(lines) >= 38
    check("阿米娅语音 ≥38 条（原 37 丢 1）", ok5, f"count={len(lines) if lines else 0}")
    if lines:
        t8 = next((l for l in lines if "信赖提升后交谈2" in l["title"]), None)
        t9 = next((l for l in lines if "信赖提升后交谈3" in l["title"]), None)
        ok5b = t8 and t9 and "漫长的道路" in t9["text"] and "漫长的道路" not in t8["text"]
        check("台词8/9 不再错位（9 才是漫长道路）", bool(ok5b),
              f"t8={t8['text'][:18] if t8 else None}…")

    # ---- 6. #14：流明 Y 模组真实表（typeIcon 大写 WAH-Y）----
    from ArknightsUID.utils.gamedata.oppanel import find_module
    uni_raw = json.loads((Path("/home/yugugu/core/gsuid_core/data/ArknightsUID/cache/gamedata/uniequip_table.json")).read_text())
    uni = uni_raw.get("payload") if "payload" in uni_raw else uni_raw
    equip_dict = uni.get("equipDict") or {}
    lumen = "char_4042_lumen"
    lumen_equips = (uni.get("charEquip") or {}).get(lumen) or []
    choices = []
    for eid in lumen_equips:
        e = equip_dict.get(eid) or {}
        icon = str(e.get("typeIcon") or "")
        if icon and icon != "original":
            choices.append({"id": eid, "name": str(e.get("uniEquipName") or eid),
                            "type": "Y" if icon.lower().endswith("y") else "X"})
    picked = find_module(choices, "Y")
    check("流明 Y 模组判型（WAH-Y 大写）", bool(picked) and picked["type"] == "Y",
          f"picked={picked['name'] if picked else None}")

    # ---- 7. #22：HOST/PORT 空串回落不再 KeyError ----
    from ArknightsUID.utils.render import render_utils as ru
    from unittest import mock
    with mock.patch.object(ru.core_config, "get_config", return_value=""):
        url = ru._get_local_base_url()
    check("HOST/PORT 空串回落 core 默认值", url.startswith("http://") and url.count(":") == 2, url)

    # ---- 8. #3：非法 uid 全链路不炸 ----
    from ArknightsUID.utils.player import service
    check("非法 uid 缓存读返回 None",
          service._cache_age_seconds("abc123") is None
          and await service.read_cached_player_info("abc123") is None)

    # ---- 9. 第二轮修复：潜能口径（银灰真实表） ----
    from ArknightsUID.arknightsuid_wiki import _build_op_panel_context
    for ui, want in ((1, "20"), (5, "19"), (6, "18")):
        q = f"银灰 精2 满级 潜{ui} 无模"
        ctx = await _build_op_panel_context(q, {})
        cost = ctx["attrs"]["cost"] if isinstance(ctx, dict) else "?"
        check(f"银灰 潜{ui} 部署费用={want}（口径 N-1）", cost == want, f"cost={cost}")

    print(f"\n===== {len(PASS)} PASS / {len(FAIL)} FAIL =====")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
