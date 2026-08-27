"""一次性探测脚本：验证森空岛 web 端明日方舟 gameId/cateId 与列表字段结构。

用法：python tools/probe_skland_ann.py [gameId] [cateId]
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> None:
    from ArknightsUID.utils.skland import web as sk_web

    game_id = sys.argv[1] if len(sys.argv) > 1 else sk_web.ARK_GAME_ID
    cate_id = sys.argv[2] if len(sys.argv) > 2 else sk_web.ARK_ANN_CATE_ID

    token = await sk_web.refresh_public_web_token(force=True)
    print(f"token: {'OK ' + token[:12] + '...' if token else 'FAIL'}")

    if len(sys.argv) <= 2:
        # 无参：遍历 cateId 1..15 找明日方舟公告分类
        for cate in range(1, 16):
            res = await sk_web.web_public_get(
                sk_web.WEB_HOME_PATH,
                {"gameId": game_id, "cateId": cate, "pageSize": "3",
                 "sortType": "2"},
            )
            if not isinstance(res, dict) or res.get("code") != 0:
                print(f"cateId={cate}: code={res.get('code') if isinstance(res, dict) else res}")
                continue
            entries = ((res.get("data") or {}).get("list")) or []
            titles = [((e.get("item") or {}).get("title") or "")[:24] for e in entries[:3]]
            print(f"cateId={cate}: {len(entries)} 条 {titles}")
    else:
        res = await sk_web.web_public_get(
            sk_web.WEB_HOME_PATH,
            {"gameId": game_id, "cateId": cate_id, "pageSize": "10",
             "sortType": "2"},
        )
        print(json.dumps(res, ensure_ascii=False, indent=2)[:4000])
        if isinstance(res, dict) and res.get("code") == 0:
            first = (((res.get("data") or {}).get("list")) or [{}])[0]
            item = first.get("item") or {}
            print("\n=== 首条 item keys ===")
            print(sorted(item.keys()))
            print("=== 首条 item（截断） ===")
            print(json.dumps(item, ensure_ascii=False, indent=2)[:2500])
            # 详情探测
            if item.get("id"):
                detail = await sk_web.web_public_get(
                    "/web/v1/item", {"id": str(item["id"])}
                )
                d_item = ((detail or {}).get("data") or {}).get("item") or {}
                print("\n=== 详情 item keys ===")
                print(sorted(d_item.keys()))
                print("format 字段前 500 字符:")
                print(str(d_item.get("format"))[:500])


asyncio.run(main())
