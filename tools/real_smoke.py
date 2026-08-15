"""真实凭据 联调脚本（一键验证登录链 + 6 接口 + rogue/building dump + 新功能链路）。

用法（在 gsuid_core venv 下，插件根目录运行）：
  python tools/real_smoke.py <32位cred>
  # 或先兑换再联调（24位 token）：
  python tools/real_smoke.py --token <24位token>

输出：
  1. cred 有效性（refresh_token）
  2. 登录链（list_ark_roles_by_cred → gameId=1 角色列表）
  3. 6 个接口实测（code 列表）
  4. player_info.data.rogue 结构 dump → tools/rogue_dump.json（供填精确肉鸽字段）
  5. 基建解析实测（parse_building → 控制等级/制造/贸易/心情）
  6. 理智推送状态机实测（evaluate_sanity × 当前理智）
  7. 基建卡真实数据渲染（playwright 可用时出 tools/building_preview.png）

⚠️ 本脚本会发起真实森空岛网络请求，请用你自己的 cred/token（私聊渠道获取）。
   脚本不会上传/外传任何数据，dump 文件只写本地。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# 让脚本能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ArknightsUID.utils.enduid.adapter import ArkCredential  # noqa: E402
from ArknightsUID.utils.skland import auth as ark_auth  # noqa: E402
from ArknightsUID.utils.skland import client as skland_client  # noqa: E402


async def _smoke(cred: str) -> None:
    print("=== 1. refresh_token 验证 cred ===")
    token = await skland_client.refresh_token(cred)
    print(f"  token: {'OK (len=%d)' % len(token) if token else 'FAIL —— cred 可能无效'}")
    if not token:
        return

    print("\n=== 2. list_ark_roles_by_cred（登录链 binding，筛 gameId=1）===")
    roles = await ark_auth.list_ark_roles_by_cred(cred)
    if not roles:
        print("  FAIL 或无方舟角色，结束")
        return
    for r in roles:
        print(f"  - {r['nickname']}（UID {r['role_id']}, record {r['record_uid']}）")

    role = roles[0]
    cred_obj = ArkCredential(
        uid=role["role_id"], nickname=role["nickname"], server_id=role["server_id"],
        cookie_status="", cred=cred, token=token, hg_token="", skland_user_id="",
    )

    print("\n=== 3. 6 接口实测（get_cultivate_character 需 character_id，跳过）===")
    smoke: dict = {"interfaces": {}}
    for name, fn in [
        ("get_user_info", skland_client.get_user_info),
        ("get_binding", skland_client.get_binding),
        ("ark_attendance_query", skland_client.ark_attendance_query),
        ("get_cultivate_info", skland_client.get_cultivate_info),
        ("get_cultivate_player", skland_client.get_cultivate_player),
        ("get_player_info", skland_client.get_player_info),
    ]:
        try:
            resp = await fn(cred_obj)
            code = resp.get("code") if isinstance(resp, dict) else resp
            print(f"  {name}: code={code}")
            smoke["interfaces"][name] = code
        except Exception as exc:  # noqa: BLE001
            print(f"  {name}: ERROR {exc.__class__.__name__}: {exc}")
            smoke["interfaces"][name] = f"ERROR: {exc.__class__.__name__}"

    print("\n=== 4. player_info.data.rogue 结构 dump ===")
    pi = await skland_client.get_player_info(cred_obj)
    if isinstance(pi, dict) and pi.get("code") == 0:
        data = pi.get("data") or {}
        rogue = data.get("rogue")
        smoke["rogue"] = rogue
        tools_dir = Path(__file__).parent
        # 完整联调结果（6 接口 code + rogue），作为真实网络往返的持久化证据
        (tools_dir / "smoke_result.json").write_text(
            json.dumps(smoke, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # rogue 单独 dump，便于读
        rogue_out = tools_dir / "rogue_dump.json"
        rogue_out.write_text(
            json.dumps(rogue, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if isinstance(rogue, dict):
            print(f"  rogue 顶层 keys: {list(rogue.keys())}")
        else:
            print(f"  rogue 类型: {type(rogue).__name__}（值: {rogue!r}）")
        print(f"  已写入 {rogue_out} + smoke_result.json")

        print("\n=== 5. 基建解析实测（parse_building）===")
        from ArknightsUID.utils.player import parse_building, parse_player_info

        player = parse_player_info(pi)
        building = parse_building(player) if player else None
        if building is None:
            print("  FAIL：building 字段缺失或无法解析")
        else:
            print(
                f"  控制中枢 Lv{building.control_level} · 无人机 "
                f"{building.labor_value}/{building.labor_max}"
            )
            for index, room in enumerate(building.manufactures, start=1):
                print(
                    f"  制造站{index} Lv{room.level}：配方#{room.formula_id}"
                    f"（item {room.item_id}×{room.item_count}）· 效率{room.speed}"
                )
            for index, room in enumerate(building.tradings, start=1):
                print(
                    f"  贸易站{index} Lv{room.level}：{room.strategy} "
                    f"{room.stock_count}/{room.stock_limit}"
                )
            print(
                f"  宿舍氛围：{[r.comfort for r in building.dormitories]} · "
                f"线索 {building.clue_own} 板 {building.clue_board}"
            )
            print(
                f"  低心情干员 {len(building.tired_chars)} 人："
                + "、".join(
                    f"{t.name}({t.room} {t.morale})" for t in building.tired_chars[:5]
                )
            )

        print("\n=== 6. 理智推送状态机实测（evaluate_sanity × 当前理智）===")
        from ArknightsUID.utils.player import effective_ap

        from ArknightsUID.arknightsuid_notify import (
            evaluate_sanity,
            format_push_message,
        )

        if player and player.ap_max > 0:
            ap = effective_ap(player, time.time())
            is_full, should_push = evaluate_sanity(ap, player.ap_max, False)
            print(f"  当前理智 {ap}/{player.ap_max} → is_full={is_full} should_push={should_push}")
            if should_push:
                print("  " + format_push_message(player.name, player.ap_max, "123***789").replace("\n", "\n  "))
            # 复推抑制：模拟已通知
            is_full2, should_push2 = evaluate_sanity(ap, player.ap_max, True)
            print(f"  已通知后复检：should_push={should_push2}（应为 False，防重复轰炸）")
        else:
            print("  SKIP：player_info 无理智字段")

        print("\n=== 7. 基建卡真实数据渲染 ===")
        try:
            from ArknightsUID.utils.render import ark_templates, render_html
            from ArknightsUID.utils.player import build_building_context
            from ArknightsUID.version import ArknightsUID_version

            if building is not None and player is not None:
                context = await build_building_context(
                    player, building, ArknightsUID_version, "123***789"
                )
                img = await render_html(ark_templates, "ark_building.html", context)
                out = tools_dir / "building_preview.png"
                out.write_bytes(img)
                print(f"  已渲染 {out}（{len(img)} bytes）")
            else:
                print("  SKIP：building 解析失败")
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP（渲染引擎不可用）: {exc.__class__.__name__}: {exc}")
    else:
        print(f"  player_info 失败: {pi}")
        # 仍持久化 6 接口结果（即使 player_info 失败）
        (Path(__file__).parent / "smoke_result.json").write_text(
            json.dumps(smoke, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    cred = ""
    if args[0] == "--token" and len(args) >= 2:
        print("=== 0. token → cred 兑换 ===")
        info = asyncio.run(ark_auth.get_cred_by_token(args[1]))
        if not isinstance(info, dict) or not info.get("cred"):
            print(f"  兑换失败: {info!r}")
            sys.exit(1)
        cred = info["cred"]
        print(f"  cred: OK (len={len(cred)}), skland_user_id={info.get('skland_user_id')}")
    else:
        cred = args[0]

    asyncio.run(_smoke(cred))


if __name__ == "__main__":
    main()
