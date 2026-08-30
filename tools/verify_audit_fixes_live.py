"""实机验证脚本（2026-08 审计修复批次）：对真实运行时环境复跑全部修复场景。

覆盖：
- 路由：真实插件加载（SL 注册）+ 真实 Trigger.check_command + 真实
  handler._command_sort_key —— ark签到全部 必须路由全部签到（fullmatch 优先）
- 数据库：真实 SQLite 上跑 ArkUser/ArkAlias 的 legacy 表 → 唯一索引迁移
  （跨库安全 DDL/去重/幂等）→ upsert_by_uid 多账号重登隔离
- 加密：fail-closed（不可用时抛错拒绝明文）
- 网络：safe_download_bytes 对真实公网图片放行、回环地址拒绝（SSRF 边界）
- 路径：safe_url_basename 对真实 PRTS md5 直链原样保留（存量缓存兼容）
- 玩家解析：理智 0 语义、皮肤 URL 裸 ID 补 #1

运行：cd /home/yugugu/core/gsuid_core && .venv/bin/python \
  gsuid_core/plugins/ArknightsUID/tools/verify_audit_fixes_live.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS, FAIL, SKIP = [], [], []



def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def skip(name: str, detail: str = "") -> None:
    SKIP.append(name)
    print(f"SKIP  {name}  {detail}")


async def verify_dispatch() -> None:
    """1. 真实注册链 + 真实匹配/排序：签到全部路由。"""
    from gsuid_core.handler import _command_sort_key
    from gsuid_core.sv import SL

    import ArknightsUID.arknightsuid_account  # noqa: F401
    import ArknightsUID.arknightsuid_bind  # noqa: F401
    import ArknightsUID.arknightsuid_cultivate  # noqa: F401
    import ArknightsUID.arknightsuid_gacha  # noqa: F401
    import ArknightsUID.arknightsuid_help  # noqa: F401
    import ArknightsUID.arknightsuid_maintenance  # noqa: F401
    import ArknightsUID.arknightsuid_notify  # noqa: F401
    import ArknightsUID.arknightsuid_push  # noqa: F401
    import ArknightsUID.arknightsuid_rogue  # noqa: F401
    import ArknightsUID.arknightsuid_user_config  # noqa: F401
    import ArknightsUID.arknightsuid_wiki  # noqa: F401

    msg = SimpleNamespace(
        raw_text="ark签到全部", is_tome=False,
        file=None, file_name=None, meta_event_type=None,
    )
    candidates: dict = {}
    sv_count = 0
    trigger_count = 0
    for sv_name, sv in SL.lst.items():
        if "ArknightsUID" not in sv_name:
            continue
        sv_count += 1
        for trig_dict in sv.TL.values():
            for trig in trig_dict.values():
                trigger_count += 1
                if trig.type == "message":
                    continue
                try:
                    if trig.check_command(msg):
                        candidates[trig] = sv.priority
                except Exception:
                    pass
    check("插件真实注册", sv_count >= 10 and trigger_count > 120,
          f"sv={sv_count} triggers={trigger_count}")

    order = [t for t, _ in sorted(candidates.items(), key=_command_sort_key)]
    first = order[0] if order else None
    hit = sorted((t.type, t.keyword) for t in candidates)
    check("ark签到全部 命中两个触发器",
          ("command", "签到") in hit and ("fullmatch", "签到全部") in hit,
          f"candidates={hit}")
    check("分发第一名 = 全部签到(fullmatch)",
          first is not None and first.type == "fullmatch"
          and "sign_all_accounts" in getattr(first.func, "__qualname__", ""),
          f"first={first.type}:{first.keyword}" if first else "no hit")


async def verify_db() -> None:
    """2. 真实 SQLite：legacy 表迁移 + 多账号 upsert 隔离。"""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from ArknightsUID.utils.database import auto_migrate
    from ArknightsUID.utils.database.models import ArkAlias, ArkUser

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}/live.db")
        try:
            # legacy schema：同名同列、无唯一约束（复刻旧库）
            for model in (ArkUser, ArkAlias):
                legacy = sa.Table(
                    model.__tablename__, sa.MetaData(),
                    sa.Column("id", sa.Integer, primary_key=True),
                    *[
                        col._copy()
                        for col in model.__table__.columns
                        if col.name != "id"
                    ],
                )
                async with engine.begin() as conn:
                    await conn.run_sync(lambda c, t=legacy: t.create(c))

            maker = sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)
            async with maker() as s:
                # 旧 bug 遗留：uid=111 重复行 + 兄弟账号 222
                s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                              nickname="旧", cookie="old"))
                s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                              nickname="新", cookie="new"))
                s.add(ArkUser(user_id="u", bot_id="b", uid="222",
                              nickname="B", cookie="b"))
                s.add(ArkAlias(bot_id="b", user_id="u",
                               alias="银羽毛", target="旧"))
                s.add(ArkAlias(bot_id="b", user_id="u",
                               alias="银羽毛", target="新"))
                await s.commit()

            ok1 = await auto_migrate.ensure_ark_user_unique_index(
                engine_override=engine
            )
            ok2 = await auto_migrate.ensure_ark_alias_unique_index(
                engine_override=engine
            )
            # 幂等重跑
            ok1b = await auto_migrate.ensure_ark_user_unique_index(
                engine_override=engine
            )
            check("ArkUser 唯一索引迁移（含幂等）", ok1 and ok1b)
            check("ArkAlias 唯一索引迁移", ok2)

            from sqlalchemy import select
            async with maker() as s:
                users = {r.uid: r for r in
                         (await s.execute(select(ArkUser))).scalars().all()}
                aliases = (await s.execute(select(ArkAlias))).scalars().all()
            check("迁移去重（每组保留最新）",
                  len(users) == 2 and users["111"].nickname == "新"
                  and len(aliases) == 1 and aliases[0].target == "新",
                  f"users={sorted(users)} alias_n={len(aliases)}")

            # 重登 111：222 必须原样保留
            async with maker() as s:
                ok = await ArkUser.upsert_by_uid.__wrapped__(
                    ArkUser, session=s, user_id="u", bot_id="b", uid="111",
                    nickname="重登", cookie="relogin",
                )
                await s.commit()
            async with maker() as s:
                users = {r.uid: r for r in
                         (await s.execute(select(ArkUser))).scalars().all()}
            check("重登只更新目标行（兄弟账号保留）",
                  ok and len(users) == 2
                  and users["111"].nickname == "重登"
                  and users["222"].nickname == "B")

            # alias 原子 set：同键覆盖仍一行
            async with maker() as s:
                await ArkAlias.set_alias.__wrapped__(
                    ArkAlias, session=s,
                    bot_id="b", user_id="u", alias="银羽毛", target="银灰改",
                )
                await s.commit()
            async with maker() as s:
                aliases = (await s.execute(select(ArkAlias))).scalars().all()
            check("别名 set 原子覆盖（一行）",
                  len(aliases) == 1 and aliases[0].target == "银灰改")
        finally:
            await engine.dispose()


def verify_crypto() -> None:
    """3. 加密 fail-closed。"""
    from ArknightsUID.utils import crypto as ark_crypto
    from ArknightsUID.utils.crypto import EncryptionUnavailableError

    with tempfile.TemporaryDirectory() as tmp:
        import os

        old_key = os.environ.pop("ARK_UID_ENC_KEY", None)
        ark_crypto._reset_cache()
        try:
            with __import__("unittest").mock.patch.object(
                ark_crypto, "_key_file", new=lambda: Path(tmp) / "unreadable"
            ), __import__("unittest").mock.patch.object(
                ark_crypto, "_load_or_create_key_file", return_value=None
            ):
                ark_crypto._reset_cache()
                raised = False
                try:
                    ark_crypto.encrypt_secret("c" * 32)
                except EncryptionUnavailableError:
                    raised = True
            check("加密不可用抛错拒绝明文", raised)
        finally:
            if old_key is not None:
                os.environ["ARK_UID_ENC_KEY"] = old_key
            ark_crypto._reset_cache()


async def verify_network() -> None:
    """4. 真实网络边界：公网图片放行、回环拒绝。"""
    from ArknightsUID.utils.http import UnsafeUrlError, safe_download_bytes

    loopback = await safe_download_bytes("http://127.0.0.1:8080/x.png")
    check("SSRF：回环地址拒绝（返回 None）", loopback is None)

    metadata = await safe_download_bytes(
        "http://169.254.169.254/latest/meta-data/"
    )
    check("SSRF：云元数据地址拒绝", metadata is None)

    from ArknightsUID.utils.player.context import skin_avatar_url

    avatar_url = skin_avatar_url("char_002_amiya")  # 真实存在的 hycdn 头像
    try:
        data = await safe_download_bytes(avatar_url, timeout=10.0)
        check("公网图片安全下载放行（真实 hycdn 头像）",
              isinstance(data, bytes) and len(data) > 0,
              f"bytes={len(data) if data else 0}")
    except Exception as exc:  # 无外网环境不算失败
        skip("公网图片安全下载放行", f"{exc.__class__.__name__}")


def verify_misc() -> None:
    """5. 真实函数口径：URL basename / 理智 0 / 皮肤 URL。"""
    from ArknightsUID.utils.gamedata.path_safety import safe_url_basename

    real = ("https://media.prts.wiki/7/74/"
            "1-7_%E6%9A%B4%E5%90%9B_%E5%9C%B0%E5%9B%BE.png")
    check("PRTS 真实直链 basename 原样保留（存量缓存兼容）",
          safe_url_basename(real) == "1-7_%E6%9A%B4%E5%90%9B_%E5%9C%B0%E5%9B%BE.png")

    from ArknightsUID.utils.player.parse import effective_ap, parse_player_info
    data = parse_player_info(
        {"data": {"status": {"name": "D", "uid": "1",
                             "ap": {"current": 0, "max": 135}}}}
    )
    check("理智 0 = 真 0（不再当满理智）",
          data is not None and effective_ap(data) == 0)

    from ArknightsUID.utils.player.context import skin_avatar_url
    check("裸 charId 皮肤 URL 补 #1",
          skin_avatar_url("char_002_amiya").endswith("%231.png"))

    from ArknightsUID.utils.gacha_plan import parse_plan_args
    args = parse_plan_args("玉:60000 刀:inf 149809765")
    check("攒抽参数：inf 拒绝 + UID 不吃成抽数",
          args.errors and args.target_draws is None
          and args.orundum == 60000)


async def verify_final_batch() -> None:
    """6. 收尾批次：#20 串图键 / #10 跨进程锁 / #4 重加密 / #34 reload 钩子。"""
    # #20：缓存键 URL 短哈希
    from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url

    new_a, old_a = cache_names_for_url("https://a.cdn.com/img/foo.png")
    new_b, old_b = cache_names_for_url("https://b.cdn.com/x/foo.png")
    check("#20 同名 URL 缓存键隔离（旧键兼容）",
          old_a == old_b == "foo.png" and new_a != new_b,
          f"{new_a} vs {new_b}")

    # #10：真实多进程并发读改写（worker 在独立轻量模块，避免子进程
    # import 本脚本顶层拉起 gsuid_core 全家桶撞配置 .part 文件）
    import multiprocessing as mp
    import tempfile

    sys.path.insert(0, str(ROOT / "tests"))
    from lock_probe_worker import lock_write_worker

    with tempfile.TemporaryDirectory() as tmp:
        shared = str(Path(tmp) / "shared.json")
        procs = [
            mp.get_context("spawn").Process(
                target=lock_write_worker, args=(shared, f"k{i}")
            )
            for i in range(4)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=20)
        final = json.loads(Path(shared).read_text(encoding="utf-8"))
    check("#10 跨进程文件锁：4 进程并发不丢更新",
          sorted(final) == ["k0", "k1", "k2", "k3"], f"keys={sorted(final)}")

    # #4：存量明文重加密（真实引擎）
    async def _reencrypt_check() -> bool:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from ArknightsUID.utils.crypto import decrypt_secret
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}/r.db")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(
                        lambda c: ArkUser.__table__.create(c, checkfirst=True)
                    )
                maker = sessionmaker(engine, class_=AsyncSession,
                                     expire_on_commit=False)
                async with maker() as s:
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                                  nickname="明文", cookie="p" * 32))
                    await s.commit()
                async with maker() as s:
                    n = await ArkUser.reencrypt_plaintext_credentials.__wrapped__(
                        ArkUser, session=s
                    )
                    await s.commit()
                async with maker() as s:
                    row = (await s.execute(select(ArkUser))).scalars().first()
                return (n == 1 and row.cookie.startswith("enc:v1:")
                        and decrypt_secret(row.cookie) == "p" * 32)
            finally:
                await engine.dispose()

    check("#4 存量明文启动重加密", await _reencrypt_check())

    # #34：reload 的插件归属 shutdown 调度——注册哨兵同步钩子验证机制
    #（async 钩子经 create_task 在 loop 恢复后执行，不在此断言副作用）
    from gsuid_core.server import _DefHook, core_shutdown_def
    from gsuid_core.utils.plugins_update.reload_plugin import (
        _run_plugin_shutdown_hooks,
    )

    fired: list[str] = []

    def sentinel():
        fired.append("ark")

    sentinel.__module__ = "ArknightsUID.utils.http"
    _orig_hooks = set(core_shutdown_def)
    core_shutdown_def.add(_DefHook(priority=0, func=sentinel))
    try:
        _run_plugin_shutdown_hooks("ArknightsUID")
    finally:
        core_shutdown_def.clear()
        core_shutdown_def.update(_orig_hooks)
    check("#34 reload 前执行插件归属 shutdown 钩子", fired == ["ark"])


async def main() -> None:
    print(f"== ArknightsUID 审计修复实机验证 ==\n")
    await verify_dispatch()
    await verify_db()
    verify_crypto()
    await verify_network()
    verify_misc()
    await verify_final_batch()

    print(f"\n== 结果：{len(PASS)} PASS / {len(FAIL)} FAIL / {len(SKIP)} SKIP ==")
    if FAIL:
        for name in FAIL:
            print(f"  FAIL: {name}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
