"""只读实网检查公开数据源；临时缓存，不读取账号或触发签到/推送。

在插件根目录用 GsCore 解释器运行：python tools/check_public_sources.py
输出各源的记录数；失败退出 1。字段存在不证明全量或字段值准确，
PRTS 查询仅验证指定样本。账号接口另需凭据联调，不能由此替代。
"""

from __future__ import annotations

import asyncio
import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def validate_rows(payload: Any, key: str | None, fields: tuple[str, ...]) -> int:
    """检查每条记录的消费字段，避免只凭顶层非空就判成功。"""
    rows = payload[key] if key else payload
    if not isinstance(rows, (dict, list)) or not rows:
        raise ValueError("数据为空或结构不符")
    entries = rows.items() if isinstance(rows, dict) else enumerate(rows)
    for row_id, row in entries:
        if not isinstance(row, dict):
            raise ValueError(f"记录 {row_id} 不是对象")
        missing = set(fields) - row.keys()
        if missing:
            raise ValueError(f"记录 {row_id} 缺少字段 {sorted(missing)}")
    return len(rows)


def validate_recruit(entries: list[dict], response: dict) -> None:
    """比对 SMW 原始成员和解析结果，检查漏条目及 0-5 到 1-6 星的转换。"""
    raw = response["query"]["results"]
    if response.get("query-continue-offset") is not None:
        raise ValueError("公招查询达到分页上限，尚有未拉取成员")
    if len(entries) != len(raw) or {row["name"] for row in entries} != set(raw):
        raise ValueError("公招解析遗漏或重复成员")
    for row in entries:
        fields = raw[row["name"]]["printouts"]
        if row["rarity"] != int(fields["稀有度"][0]) + 1:
            raise ValueError(f"公招干员 {row['name']} 星级转换不符")
        expected_tags = {fields["职业"][0], fields["位置"][0]}
        if not expected_tags <= set(row["all_tags"]):
            raise ValueError(f"公招干员 {row['name']} 职业/位置标签缺失")


async def check(source_names: set[str] | None = None) -> bool:
    from ArknightsUID.utils import http, yituliu
    from ArknightsUID.utils.gamedata import _table_cache as tables
    from ArknightsUID.utils.gamedata import recruit
    from ArknightsUID.utils.gamedata import rogue_enemy
    from ArknightsUID.utils.gamedata.rogue_registry import THEME_DIRS
    from ArknightsUID.utils.prts import client as prts
    from ArknightsUID.utils.skland import web

    async def public_avatar_data(path, params):
        response = await web.web_public_get(path, params)
        if response is None or response.get("code") != 0:
            raise ValueError("公开头像目录获取失败")
        data = response["data"]
        if data.get("hasMore"):
            raise ValueError("公开头像目录尚未返回完整数据")
        return data

    async def recruit_pool():
        entries = await recruit._fetch_pool()
        response = await prts._smw_ask(recruit._POOL_QUERY)
        validate_recruit(entries, response)
        return entries

    async def crisis_table(version):
        return (await tables.ensure_crisis_tables())[version]

    async def difficulties():
        data = await rogue_enemy.load_difficulty_mods()
        if set(data) != set(THEME_DIRS):
            raise ValueError(f"难度表缺少期次 {sorted(set(THEME_DIRS) - set(data))}")
        for rows in data.values():
            validate_rows(rows, None, ("difficulty",))
        return {theme: {"grades": len(rows)} for theme, rows in data.items()}

    async def wiki_operator():
        result = await prts.query_operator("银灰")
        if result is None or result.name != "银灰" or not result.skills:
            raise ValueError("银灰资料缺少名称或技能")
        if not result.panel or not result.panel.panels:
            raise ValueError("银灰资料缺少面板")
        return {"银灰": asdict(result)}

    async def wiki_stage():
        result = await prts.query_stage("1-7")
        if result is None or result.code != "1-7" or float(result.ap_cost) != 6:
            raise ValueError("1-7 关卡编码或理智消耗不符")
        if not result.enemies or not result.drops:
            raise ValueError("1-7 关卡敌人或掉落解析为空")
        return {"1-7": asdict(result)}

    async def wiki_enemy():
        result = await prts.query_enemy("源石虫")
        if result is None or result.name != "源石虫" or not result.levels:
            raise ValueError("源石虫名称或等级解析为空")
        if float(result.levels[0].max_hp) <= 0 or float(result.levels[0].atk) <= 0:
            raise ValueError("源石虫生命或攻击数值无效")
        return {"源石虫": asdict(result)}

    sources = [
        ("characters", tables.ensure_char_table, None, ("name", "rarity", "skills")),
        ("skills", tables.ensure_skill_table, None, ("levels",)),
        ("items", tables.ensure_item_table, "items", ("itemId", "name")),
        ("skins", tables.ensure_skin_table, "charSkins", ("charId", "displaySkin")),
        ("stages", tables.ensure_stage_table, "stages", ("stageId", "code", "apCost")),
        (
            "modules",
            tables.ensure_uniequip_table,
            "equipDict",
            ("uniEquipId", "uniEquipName"),
        ),
        ("module_stats", tables.ensure_battle_equip_table, None, ("phases",)),
        (
            "building",
            tables.ensure_building_table,
            "workshopFormulas",
            ("itemId", "count", "costs"),
        ),
        ("rogue", tables.ensure_rogue_topic_table, "topics", ("id", "name")),
        ("recruit", recruit_pool, None, ("name", "rarity", "all_tags")),
        (
            "stage_efficiency",
            yituliu.fetch_stage_info,
            None,
            ("stageId", "stageCode", "apCost", "spm"),
        ),
        (
            "drop_matrix",
            yituliu.fetch_drop_matrix,
            None,
            ("stageId", "itemId", "times", "quantity"),
        ),
        (
            "crisis_old",
            lambda: crisis_table(0),
            "seasonInfo",
            ("name", "startTs", "endTs"),
        ),
        (
            "crisis_new",
            lambda: crisis_table(1),
            "seasonInfoDataMap",
            ("name", "startTs", "endTs"),
        ),
        ("rogue_enemies", rogue_enemy.load_enemy_db, None, ("name_zh",)),
        ("rogue_difficulties", difficulties, None, ("grades",)),
        ("prts_operator", wiki_operator, None, ("skills", "panel")),
        ("prts_stage", wiki_stage, None, ("enemies", "drops")),
        ("prts_enemy", wiki_enemy, None, ("levels",)),
        (
            "avatar_categories",
            lambda: public_avatar_data("/web/v1/user/avatar/category/list", {}),
            "list",
            ("id", "title"),
        ),
        (
            "arknights_avatars",
            lambda: public_avatar_data("/web/v1/user/avatars", {"categoryId": 2}),
            "list",
            ("id", "url"),
        ),
    ]
    if source_names is not None:
        unknown = source_names - {source[0] for source in sources}
        if unknown:
            raise ValueError(f"未知数据源：{', '.join(sorted(unknown))}")
        sources = [source for source in sources if source[0] in source_names]
    semaphore = asyncio.Semaphore(3)

    async def one(name, fetch, key, fields):
        async with semaphore:
            try:
                payload = await asyncio.wait_for(fetch(), timeout=150)
                count = validate_rows(payload, key, fields)
                result = {"source": name, "ok": True, "count": count}
            except Exception as exc:
                result = {
                    "source": name,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result["ok"]

    try:
        results = await asyncio.gather(*(one(*source) for source in sources))
        return all(results)
    finally:
        await http.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", action="append", help="只检查指定数据源，可重复传入"
    )
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "tests"))
    from runtime_isolation import isolate_data_root

    with tempfile.TemporaryDirectory(prefix="arkuid-public-check-") as directory:
        isolate_data_root(Path(directory))
        selected = set(args.source) if args.source else None
        raise SystemExit(0 if asyncio.run(check(selected)) else 1)
