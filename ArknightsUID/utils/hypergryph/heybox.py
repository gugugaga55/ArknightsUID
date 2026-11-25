"""小黑盒抽卡导出链接导入适配器。

数据契约（小黑盒 App「抽卡分析 → 数据管理 → 导出数据并复制链接」得到
一个直接返回 JSON 的链接；参考 FrostN0v0/nonebot-plugin-skland
29a0e42 的 gacha.py 契约，仅参考数据结构，不复制实现/UI）：

{
  "info": {"uid": "<uid>", "lang": "zh-cn", "export_app": "小黑盒", ...},
  "data": {
    "1700000000": {"p": "卡池名称", "c": [["干员名", 5, 1], ["另一个干员", 3, 0]]}
  }
}

- 外层 data 键：秒级 Unix 时间戳
- c：同一时间戳下按抽取顺序的干员数组，下标 → 本地 pos
- 每项 [name, rarity, isNew]，rarity 0 基（2=三星 … 5=六星）→ 本地 basis="0"
- 小黑盒无官方 category：能识别 gachaRuleType 用 heybox:rule:<type> 内部组，
  否则按 heybox:pool:<poolId或池名> 独立分组（不伪造官方 category ID，
  也不全塞同一组——垫刀/五星分段会跨池串联）

安全边界：URL 是用户输入，只经 utils.http.safe_download_bytes（仅
http/https、拒内网/环回/保留地址、逐跳校验重定向、8MiB 上限）下载，
不走普通 request_json/裸 httpx。
"""

from __future__ import annotations

import json
from typing import Any

from gsuid_core.logger import logger

from ..http import safe_download_bytes

# 已知小黑盒干员名差异（刻意极小：只放确证过的，不建猜测规则）
_HEYBOX_NAME_ALIASES = {
    "麒麟X夜刀": "麒麟R夜刀",
}

# 导出 JSON 的宽松大小上限（safe_download_bytes 默认 8MiB，这里同口径）
_EXPORT_MAX_BYTES = 8 * 1024 * 1024


async def fetch_heybox_export(url: str) -> dict[str, Any] | str:
    """下载导出 JSON（安全下载边界内）；失败返回中文文案。"""
    raw = await safe_download_bytes(url, max_bytes=_EXPORT_MAX_BYTES)
    if raw is None:
        return "导出链接下载失败（链接无效、不可公开访问或超过大小限制），请重新导出。"
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return "导出链接返回的内容不是有效 JSON，请确认是「导出数据并复制链接」得到的链接。"
    if not isinstance(payload, dict):
        return "导出数据结构异常（根对象不是对象），请重新导出。"
    return payload


def _resolve_char_id(
    name: str, name_to_id: dict[str, str]
) -> tuple[str, str]:
    """小黑盒干员名 → (标准名, charId)；映射不到保留原名、charId 留空。"""
    standard = _HEYBOX_NAME_ALIASES.get(name, name)
    return standard, name_to_id.get(standard, "")


async def convert_heybox_records(
    payload: dict[str, Any], uid: str
) -> list[dict[str, Any]] | str:
    """导出 JSON → 本地记录列表；结构/UID 不符返回中文文案。

    记录字段与官方同步一致（gacha.py _normalize_record 的超集）：
    gacha_ts / pos / name / char_id / rarity(0 基) / is_new / pool_name /
    basis="0" / pool_id / category（heybox 内部组）/ source="heybox"。
    """
    info = payload.get("info")
    data = payload.get("data")
    if not isinstance(info, dict) or not isinstance(data, dict) or not data:
        return "导出数据缺少 info/data 字段，请确认链接来自小黑盒「导出数据」。"
    export_uid = str(info.get("uid") or "")
    if export_uid != str(uid):
        shown = export_uid or "未知"
        return f"导出数据的 UID（{shown}）与当前账号不一致，已拒绝导入。"

    from ..gamedata.charinfo import load_char_index

    char_index = await load_char_index() or {}
    name_to_id = {
        str(entry.get("name") or ""): char_id
        for char_id, entry in char_index.items()
        if isinstance(entry, dict) and entry.get("name")
    }

    from ..gamedata.gacha import (
        load_gacha_pools,
        match_pool_in_window,
        rule_category,
    )

    pools = await load_gacha_pools()
    rules_by_name: dict[str, set[str]] = {}
    for p in pools:
        if p.rule_type:
            rules_by_name.setdefault(p.pool_name, set()).add(p.rule_type)

    records: list[dict[str, Any]] = []
    pool_meta: dict[tuple[str, int], tuple[str, str]] = {}
    for raw_ts, group in data.items():
        ts = 0
        try:
            ts = int(raw_ts)
        except (TypeError, ValueError):
            continue
        if not isinstance(group, dict):
            continue
        pool_name = str(group.get("p") or "")
        entries = group.get("c")
        if not isinstance(entries, list):
            continue
        meta_key = (pool_name, ts)
        if meta_key not in pool_meta:
            pool = match_pool_in_window(pool_name, ts, pools)
            inferred_rules = rules_by_name.get(pool_name, set())
            rule_type = (
                pool.rule_type
                if pool
                else (next(iter(inferred_rules)) if len(inferred_rules) == 1 else "")
            )
            pool_meta[meta_key] = (
                (pool.pool_id if pool else ""),
                rule_category(
                    rule_type,
                    pool.pool_id if pool else "",
                    pool_name,
                ),
            )
        pool_id, category = pool_meta[meta_key]
        for pos, entry in enumerate(entries):
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            raw_name = str(entry[0] or "")
            if not raw_name:
                continue
            try:
                rarity = int(entry[1])
            except (TypeError, ValueError):
                continue
            is_new = bool(entry[2]) if len(entry) > 2 else False
            name, char_id = _resolve_char_id(raw_name, name_to_id)
            records.append(
                {
                    "category": category,
                    "char_id": char_id,
                    "name": name,
                    "rarity": rarity,
                    "is_new": is_new,
                    "gacha_ts": ts,
                    "pos": pos,
                    "pool_name": pool_name,
                    "pool_id": pool_id,
                    "basis": "0",
                    "source": "heybox",
                }
            )
    if not records:
        return "导出数据里没有可识别的抽卡记录。"
    logger.info(
        f"[ArknightsUID·抽卡] 小黑盒导入解析成功 uid={uid} 记录数={len(records)}"
    )
    return records
