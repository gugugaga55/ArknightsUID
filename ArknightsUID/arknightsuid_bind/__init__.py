from __future__ import annotations

import asyncio

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

# gsuid_core 内部工具（挪动/改名时降级为可读错误，不拖垮模块加载）
try:
    from gsuid_core.utils.cookie_manager.qrlogin import get_qrcode_base64
except ImportError:  # pragma: no cover
    get_qrcode_base64 = None

from ArknightsUID.utils.account_switch import (
    clear_default_uid,
    get_default_uid,
)
from ArknightsUID.utils.arg_utils import extract_uid
from ArknightsUID.utils.config import ArkConfig
from ArknightsUID.utils.enduid.adapter import hide_uid, hide_uid_for
from ArknightsUID.utils.registration import sync_sensitive_prefixes
from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH
from ArknightsUID.utils.skland import auth as ark_auth
from ArknightsUID.utils.skland import store as ark_store

# 开关在 handler 内实时判定——area="DIRECT" 是框架级静默拦截，开关
# 开了也进不来，所以扫码不能挂在 bind_sv 下
scan_sv = SV("ArknightsUID扫码登录", priority=5)
# 网页登录链接同属"免密登录入口"向量，与扫码共用 AllowGroupScanLogin 开关，
# 也不挂 bind_sv
web_login_sv = SV("ArknightsUID网页登录", priority=5)

_PRIVATE_ONLY_HINT = (
    "⚠️ 绑定/登录涉及账号凭据安全，仅限私聊使用！\n"
    "若你刚刚在群内发送了 cred 或 token，请立即撤回消息，并前往森空岛重新登录以刷新凭据。"
)
_GROUP_SCAN_OFF_HINT = (
    "扫码登录默认仅私聊可用（二维码等于一次免密登录，不适合发群里）。\n"
    "管理员可在插件配置开启「允许群聊扫码登录」（AllowGroupScanLogin）后群内使用。"
)
# 群内扫码的明示风险文案（开关已开）
_GROUP_SCAN_NOTICE = (
    "⚠️ 二维码在群内公开：100 秒内群里任何人扫码，都会把 TA 的账号"
    "绑定到你名下。确认周围是熟人再扫。"
)


bind_sv = SV("ArknightsUID绑定", area="ALL", priority=5)
# 迁移存量部署：旧版 area="DIRECT" 已被持久化进 plugin_config_store，
# 仅改默认参数对老实例无效，必须在加载时显式纠偏为 ALL（交由 handler 内拒绝并提醒撤回）
if bind_sv.area == "DIRECT":
    bind_sv.set(area="ALL")
sync_sensitive_prefixes()

# 文本回复公共实现在 utils.card.send_text；保留模块级别名供调用点/测试 patch
from ArknightsUID.utils.card import send_text as _send_text  # noqa: E402

# 登录成功回执的纯文本格式化（命令侧卡片回退文本与网页登录推送共用）
from ._format import format_bind_success as _format_bind_success  # noqa: E402


async def _send_login_success(bot: Bot, ev: Event, roles: list[dict]) -> None:
    """登录成功回执：ark_login.html 卡片图；渲染失败回退纯文本清单。"""
    from ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.version import ArknightsUID_version

    default_uid = get_default_uid(ev.bot_id, ev.user_id)
    shown_uids: dict[str, str] = {}
    for role in roles:
        raw_id = str(role.get("role_id") or "")
        if raw_id and raw_id not in shown_uids:
            shown_uids[raw_id] = await hide_uid_for(raw_id, ev.user_id, ev.bot_id)
    context = {
        "version": ArknightsUID_version,
        "roles": [
            {
                "index": index,
                "nickname": role.get("nickname") or "未记录昵称",
                "uid": shown_uids.get(
                    str(role.get("role_id") or ""),
                    hide_uid(str(role.get("role_id") or "")),
                ),
                "is_default": str(role.get("role_id") or "") == default_uid,
            }
            for index, role in enumerate(roles, start=1)
        ],
    }
    await send_card_or_text(
        bot,
        ev,
        "ark_login.html",
        context,
        _format_bind_success(roles, shown_uids=shown_uids),
    )


@bind_sv.on_command(("绑定登录", "绑定"), block=True)
async def bind_credential(bot: Bot, ev: Event) -> None:
    if ev.group_id:
        return await _send_text(bot, ev, _PRIVATE_ONLY_HINT)
    if not (ev.text or "").strip():
        return await _send_text(
            bot, ev,
            "绑定明日方舟账号：\n"
            "• ark登录：网页登录\n"
            "• ark扫码登录：用森空岛扫码\n"
            "• ark绑定 <32 位 cred 或 24 位 token>：仅私聊发送\n"
            "已登录的账号用 ark账号 查看。",
        )

    kind, value = ark_auth.parse_credential(ev.text)
    if kind is None:
        return await _send_text(
            bot, ev, "请输入 32 位 cred 或 24 位 token，例如：ark绑定 <你的cred>"
        )

    hg_token = ""
    skland_user_id = ""
    if kind == "token":
        cred_info, err = await ark_auth.get_cred_by_token(value)
        if err == ark_auth.CRED_ERR_UNAVAILABLE:
            return await _send_text(
                bot, ev, "当前服务暂时无法使用 token 登录，请改用 cred 或 ark扫码登录。"
            )
        if not cred_info or not cred_info.get("cred"):
            return await _send_text(
                bot, ev, "token 兑换 cred 失败，请检查 token 是否正确。"
            )
        cred = cred_info["cred"]
        skland_user_id = cred_info.get("skland_user_id", "")
        hg_token = value  # token 即 hg_token，抽卡链要用
    else:
        cred = value

    roles, err = await ark_store.persist_ark_login(
        ev.user_id,
        ev.bot_id,
        ev.group_id,
        cred,
        hg_token=hg_token,
        skland_user_id=skland_user_id,
    )
    if err:
        return await _send_text(bot, ev, err)
    await _send_login_success(bot, ev, roles or [])


@scan_sv.on_fullmatch(("扫码登录", "扫码登陆"), block=True)
async def scan_login(bot: Bot, ev: Event) -> None:
    if ev.group_id and not bool(ArkConfig.get_config("AllowGroupScanLogin").data):
        return await _send_text(bot, ev, _GROUP_SCAN_OFF_HINT)

    scan_id = await ark_auth.get_scan_id()
    if not scan_id:
        return await _send_text(bot, ev, "获取扫码 ID 失败，请稍后重试。")

    scan_url = f"hypergryph://scan_login?scanId={scan_id}"
    qr_path = CACHE_PATH / "qr" / f"ark_scan_{ev.user_id}.png"
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    if get_qrcode_base64 is None:
        logger.warning("[ArknightsUID·绑定] 二维码工具不可用（core 版本兼容性问题）")
        return await _send_text(
            bot, ev, "二维码生成不可用（core 版本兼容性问题），请稍后重试或联系维护者。"
        )
    try:
        qr_img = await get_qrcode_base64(scan_url, qr_path, ev.bot_id)
    except Exception:
        return await _send_text(bot, ev, "二维码生成失败，请稍后重试。")
    await bot.send(qr_img)
    notice = f"{_GROUP_SCAN_NOTICE}\n" if ev.group_id else ""
    await _send_text(
        bot,
        ev,
        notice + "请使用森空岛 APP 扫码确认登录（100 秒内有效）……",
    )

    # 轮询扫码状态：2s/次，最多 50 次，且整体 deadline 3 分钟
    # （审计 #33：单次请求最长 25s，无 deadline 时最坏 50×(2+25)≈22.5
    # 分钟挂着命令任务与连接；3 分钟足够覆盖"100 秒有效"的扫码窗口
    # 与正常网络抖动）
    scan_code = ""
    deadline = asyncio.get_running_loop().time() + 180.0
    for _ in range(50):
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(2)
        scan_code = await ark_auth.get_scan_status(scan_id) or ""
        if scan_code:
            break
    if not scan_code:
        return await _send_text(bot, ev, "扫码超时或未确认，请重新使用 ark扫码登录。")

    token_info = await ark_auth.get_token_by_scan_code(scan_code)
    if not token_info or not token_info.get("token"):
        return await _send_text(bot, ev, "扫码换取 token 失败，请重试。")

    cred_info, err = await ark_auth.get_cred_by_token(token_info["token"])
    if err == ark_auth.CRED_ERR_UNAVAILABLE:
        # 扫码链路最终同样要过 OAuth token 兑换，405 受限时重扫也无用
        return await _send_text(
            bot, ev, "当前服务暂时无法完成扫码登录，请稍后再试或改用 ark绑定 <cred>。"
        )
    if not cred_info or not cred_info.get("cred"):
        return await _send_text(bot, ev, "换取 cred 失败，请重试。")

    roles, err = await ark_store.persist_ark_login(
        ev.user_id,
        ev.bot_id,
        ev.group_id,
        cred_info["cred"],
        hg_token=token_info["token"],
        skland_user_id=cred_info.get("skland_user_id", ""),
    )
    if err:
        return await _send_text(bot, ev, err)
    await _send_login_success(bot, ev, roles or [])


@web_login_sv.on_command(
    ("登录", "网页登录", "手机登录", "手机号登录"), block=True
)
async def phone_web_login(bot: Bot, ev: Event) -> None:
    """ark登录：手机号验证码网页登录（EndUID 同源协议）。

    ArkLoginUrl 未配置时在 web_login_entry 内回退扫码登录。
    实现与网页路由在 web_login.py；开关判定在 handler 内实时读取
    （与扫码同因：area="DIRECT" 是框架级静默拦截，不能挂在 bind_sv 下）。
    注意函数名不能叫 web_login：会遮蔽底部 `from . import web_login`
    的子模块导入（from-import 见属性已存在就不再加载模块，路由挂不上）。
    """
    from .web_login import web_login_entry

    await web_login_entry(bot, ev)


@bind_sv.on_command(("删除登录", "解除绑定"), block=True)
async def remove_login(bot: Bot, ev: Event) -> None:
    if ev.group_id:
        return await _send_text(bot, ev, _PRIVATE_ONLY_HINT)

    uid = extract_uid(ev.text)
    if uid:
        shown_uid = await hide_uid_for(uid, ev.user_id, ev.bot_id)
        removed = await ark_store.remove_ark_login(ev.user_id, ev.bot_id, uid=uid)
        if removed is None:
            return await _send_text(bot, ev, "读取或保存账号来源失败，未解除，请稍后重试。")
        if not removed:
            return await _send_text(
                bot,
                ev,
                f"未找到 UID {shown_uid} 的登录记录（可用账号见 ark绑定列表）。",
            )
        # 默认账号恰好指向被删 UID 时一并清除，防止 resolve 落到不存在的默认
        # （完整读-改-写事务下放线程：锁等待不再阻塞事件循环）
        if await asyncio.to_thread(clear_default_uid, ev.bot_id, ev.user_id, uid=uid):
            await _send_text(
                bot,
                ev,
                f"已解除本插件 UID {shown_uid} 的登录，默认账号已重置；重新登录可恢复。",
            )
        else:
            await _send_text(bot, ev, f"已解除本插件 UID {shown_uid} 的登录；EndUID 原记录不变，重新登录可恢复。")
    else:
        count = await ark_store.remove_ark_login(ev.user_id, ev.bot_id)
        if count is None:
            return await _send_text(bot, ev, "读取或保存账号来源失败，未解除，请稍后重试。")
        await asyncio.to_thread(clear_default_uid, ev.bot_id, ev.user_id)
        await _send_text(
            bot, ev,
            f"已解除本插件的 {count} 个明日方舟登录（含 EndUID 复用）；"
            "EndUID 原记录不变，重新登录可恢复。",
        )


@bind_sv.on_fullmatch(("查看绑定", "我的绑定"), block=True)
async def show_bind(bot: Bot, ev: Event) -> None:
    if ev.group_id:
        return await _send_text(bot, ev, "请在私聊中查看绑定。")
    from ArknightsUID.arknightsuid_account import show_ark_accounts

    await show_ark_accounts(bot, ev)


# ---------------------------------------------------------------------------
# 用户自定义干员别名（ark别名）：查询命令（ark干员/面板/养成/练度卡/模组）
# 的干员名参数支持用户级精确别名，优先级高于内建别名。
# ---------------------------------------------------------------------------
alias_sv = SV("ArknightsUID别名", priority=5)


from ArknightsUID.utils.user_alias import parse_alias_pair as _parse_alias_text


@alias_sv.on_fullmatch(("别名列表", "我的别名"), block=True)
async def list_user_alias(bot: Bot, ev: Event) -> None:
    from ArknightsUID.utils.database.models import ArkAlias

    from ArknightsUID.utils.gamedata.rogue_enemy import THEME_DIRS

    rows = await ArkAlias.list_aliases(bot_id=ev.bot_id, user_id=ev.user_id) or []
    if not rows:
        return await _send_text(bot, ev, "还没有设置别名。用法：ark别名 银羽毛=银灰")
    operators, themes = [], []
    for row in rows:
        if row.target.startswith("rogue:"):
            theme = row.target.removeprefix("rogue:")
            themes.append(f"- {row.alias} → {THEME_DIRS.get(theme, theme)}")
        else:
            operators.append(f"- {row.alias} → {row.target}")
    lines = ["「明日方舟」我的别名"]
    if operators:
        lines.extend(["【干员】", *operators])
    if themes:
        lines.extend(["【肉鸽期名】", *themes])
    await _send_text(bot, ev, "\n".join(lines))


# 注册顺序约束：别名列表/别名删除 必须先于「别名」注册。
# core 的 command 匹配是 startswith + 注册顺序优先，「ark别名列表」会被
# 先注册的「别名」抢走（text 变成"列表"）。长命令先注册，短命令兜底。
@alias_sv.on_command(("别名删除", "删除别名"), block=True)
async def delete_user_alias(bot: Bot, ev: Event) -> None:
    alias = (ev.text or "").strip().lower()
    if not alias:
        return await _send_text(bot, ev, "用法：ark别名删除 银羽毛")
    from ArknightsUID.utils.database.models import ArkAlias

    if await ArkAlias.delete_alias(bot_id=ev.bot_id, user_id=ev.user_id, alias=alias):
        await _send_text(bot, ev, f"已删除别名：{alias}")
    else:
        await _send_text(bot, ev, f"没有这个别名：{alias}")


@alias_sv.on_command(("别名",), block=True)
async def set_user_alias(bot: Bot, ev: Event) -> None:
    if not (ev.text or "").strip():
        return await list_user_alias(bot, ev)
    await _set_alias(bot, ev, automatic=True)


@alias_sv.on_command(("干员别名",), block=True)
async def set_operator_alias(bot: Bot, ev: Event) -> None:
    await _set_alias(bot, ev, automatic=False)


async def _set_alias(bot: Bot, ev: Event, *, automatic: bool) -> None:
    parsed = _parse_alias_text(ev.text)
    if not parsed:
        return await _send_text(
            bot,
            ev,
            "用法：ark别名 银羽毛=银灰，或 ark别名 小园=界园；"
            "ark别名列表 查看；ark别名删除 银羽毛 删除。",
        )
    alias, target = parsed

    from ArknightsUID.utils.database.models import ArkAlias

    from ArknightsUID.utils.gamedata.charinfo import (
        load_char_index,
        match_entries,
    )

    index = await load_char_index()
    if not index:
        return await _send_text(
            bot, ev, "干员索引不可用（首次使用需下载游戏数据），请稍后再试。"
        )
    candidates = match_entries(target, index)
    if automatic:
        from ArknightsUID.utils.gamedata.rogue_enemy import resolve_theme_dir

        theme = resolve_theme_dir(target)
        if theme and candidates:
            return await _send_text(
                bot, ev, f"「{target}」也可匹配干员，请用 ark干员别名 或 ark肉鸽别名 明确类型。"
            )
        existing = await ArkAlias.resolve_alias(
            bot_id=ev.bot_id, user_id=ev.user_id, alias=alias.lower(),
        )
        if (theme or candidates) and existing and existing.startswith("rogue:") != bool(theme):
            return await _send_text(
                bot, ev, f"「{alias}」已有另一类目标，请先用 ark别名删除 {alias} 删除旧别名。"
            )
        if theme:
            from ArknightsUID.arknightsuid_rogue import save_rogue_theme_alias

            return await save_rogue_theme_alias(bot, ev, alias, target)
    if not candidates:
        return await _send_text(bot, ev, f"未找到干员：{target}")
    if len(candidates) > 1:
        names = "、".join(str(c.get("name") or "") for c in candidates[:6])
        return await _send_text(
            bot, ev, f"「{target}」匹配到多位干员：{names}，请写全名。"
        )
    canonical = str(candidates[0].get("name") or target)

    # 别名不能是干员真名/呼号（避免查询歧义）
    lowered = alias.lower()
    for item in index.values():
        if (
            str(item.get("name") or "") == alias
            or str(item.get("appellation") or "").lower() == lowered
        ):
            return await _send_text(
                bot, ev, f"「{alias}」是干员本身的名称，不能作为别名。"
            )

    await ArkAlias.set_alias(
        bot_id=ev.bot_id,
        user_id=ev.user_id,
        alias=lowered,
        target=canonical,
    )
    await _send_text(
        bot,
        ev,
        f"已设置别名：{alias} → {canonical}（ark干员/面板/养成/练度卡/模组 均生效）",
    )


# 挂载网页登录路由（GET /ark/i/{auth}、POST /ark/sendCode、POST /ark/login）。
# 模块导入即注册到 core 的 FastAPI 实例，与 EndUID end_bind 同款做法。
from . import web_login  # noqa: E402,F401
