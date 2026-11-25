"""活动 / 公告查询 + 推送订阅（P4）。

- `ark活动`：进行中的限时活动 + 限定卡池清单（PRTS SMW，与日历同源）
- `ark公告 [编号]`：明日方舟官方公告列表（森空岛 web 公共接口）
- `订阅方舟活动` / `取消订阅方舟活动`（pm=3，群聊）：gs_subscribe 订阅
- `订阅公告` / `取消订阅公告`（群聊/私聊）：仅官方公告的专项推送，
  与「订阅方舟活动」（活动+卡池+公告合并）互不影响、各自独立基线
- 定时轮询（间隔/开关见插件配置 AnnCheckMinute / AnnPushEnable）：新活动 / 新卡池 / 新公告 → 推送订阅群

推送状态（已见条目键）落 MAIN_PATH/push_state.json（活动合并订阅）与
ann_push_state.json（公告专项订阅），条目上限防膨胀。
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import random
import re
import time
from collections.abc import Callable

from gsuid_core.aps import scheduler
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.sv import SV

from ArknightsUID.utils.arg_utils import split_account
from ArknightsUID.utils.card import send_card_or_text
from ArknightsUID.utils.cultivate import parse_inventory
from ArknightsUID.utils.enduid.adapter import fetch_cultivate_player, resolve_credential
from ArknightsUID.utils.gamedata.craft import craft_vs_farm, format_craft_tree, load_formulas
from ArknightsUID.utils.config import ArkConfig
from ArknightsUID.utils.file_lock import LockTimeoutError, file_locked
from ArknightsUID.utils.gamedata.charinfo import load_item_index
from ArknightsUID.utils.gamedata.itemicon import item_icon_b64
from ArknightsUID.utils.image import data_uri_bytes
from ArknightsUID.utils.path import MAIN_PATH
from ArknightsUID.utils.prts.client import (
    query_activities,
    query_gacha_pools,
    query_page,
)
from ArknightsUID.utils.skland.web import (
    get_ann_detail,
    get_ark_ann_list,
)
from ArknightsUID.utils.yituliu import (
    fetch_drop_matrix,
    fetch_stage_info,
    format_stage_board,
    item_drop_stats,
    latest_matrix_by_item,
    rank_stages,
)
from ArknightsUID.version import ArknightsUID_version

import json
from pathlib import Path

push_sv = SV("ArknightsUID活动", priority=5)
push_sub_sv = SV("订阅方舟活动", pm=3)

TASK_NAME = "订阅方舟活动"
_STATE_PATH: Path = MAIN_PATH / "push_state.json"
# 已见条目键上限（超出裁剪最旧的，防状态文件膨胀）
_STATE_LIMIT = 400

# 公告专项订阅（仅公告，不掺活动/卡池；session 模式群聊/私聊各一条）
ANN_TASK_NAME = "订阅方舟公告"
_ANN_STATE_PATH: Path = MAIN_PATH / "ann_push_state.json"
# 公告推送单轮上限：超过的留下轮增量推（与活动合并推送同款防刷屏）
_ANN_PUSH_LIMIT = 5
# 公告检测窗口：拉取比单轮上限更宽的列表——两次轮询间新发超过窗口数时，
# 最老的会被新条目顶出窗口且分页拉不到，静默漏推（审计 #39）。
_ANN_FETCH_LIMIT = 30


def _activity_key(kind: str, title: str) -> str:
    return f"{kind}:{title}"


# 推送状态读-改-写锁：定时任务在"load → 发送(await) → save"整段持有，
# 防止两轮推送并发重复推/互相覆盖；命令侧写盘由 save_* 的合并语义兜底
# （多进程部署仍需外部协调，见 save_push_state 注释）
_push_state_lock = asyncio.Lock()


def _session_key(subscribe) -> str:
    """订阅会话标识：群聊按群、私聊按用户（存在 bot_id 时按机器人隔离）。

    私聊必须用 user_id：session 模式订阅不写 uid（恒 None），
    按 uid 拼键会让所有私聊订阅者共用 "direct:None"，首个用户投递
    成功后其余私聊订阅者对该公告永久跳过（审计 #39）。
    """
    bot_id = str(getattr(subscribe, "bot_id", "") or "")
    prefix = f"{bot_id}:" if bot_id else ""
    if getattr(subscribe, "group_id", None):
        return f"{prefix}group:{subscribe.group_id}"
    user_id = str(getattr(subscribe, "user_id", "") or "")
    return f"{prefix}direct:{user_id or getattr(subscribe, 'uid', '') or 'unknown'}"


def _same_bot(subscribe, ev: Event) -> bool:
    """多机器人部署隔离：两侧都有 bot_id 时必须一致；单测缺省时不阻断。"""
    ev_bot = str(getattr(ev, "bot_id", "") or "")
    sub_bot = str(getattr(subscribe, "bot_id", "") or "")
    if ev_bot and sub_bot:
        return sub_bot == ev_bot
    return True


async def _deliver_per_session(
    datas,
    keys: list[str],
    build_message,
    delivered: dict[str, list[str]],
    log_tag: str,
) -> tuple[dict[str, list[str]], bool]:
    """按订阅会话投递条目（审计 #9：部分成功不再造成永久漏发）。

    delivered[key] = 已确认收到的会话集合（跨轮持久化在状态文件里）。
    某条目对某会话已投递则跳过——A 成功 B 失败时，下轮只给 B 补发，
    不给 A 重复刷屏；条目对全部会话投递完成后由调用方推进全局 seen。

    返回 (更新后的 delivered, 本轮是否有任何发送成功)。
    """
    any_sent = False
    for subscribe in datas:
        session = _session_key(subscribe)
        pending = [key for key in keys if session not in (delivered.get(key) or [])]
        if not pending:
            continue
        try:
            built = build_message(pending)
            if inspect.isawaitable(built):
                built = await built
            sent = await subscribe.send(built)
            if sent == -1:
                # gsuid_core 的 send 在 bot 离线时返回 -1 而不抛异常：
                # 按失败记账（不记 delivered），否则该会话这批条目
                # 被标记已投递后永久漏推（审计 #39）
                logger.warning(
                    f"[ArknightsUID·{log_tag}] 推送失败 session={session}: "
                    "bot 会话不可用（send 返回 -1）"
                )
                continue
            any_sent = True
            for key in pending:
                delivered.setdefault(key, []).append(session)
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·{log_tag}] 推送失败 session={session}: "
                f"{exc.__class__.__name__}"
            )
        await asyncio.sleep(random.uniform(1, 3))
    return delivered, any_sent


def _merge_seen_state(current_state: dict, incoming: dict) -> dict:
    """落盘前合并：fresh 磁盘状态优先，incoming 的 seen 追加并集（保序）。

    旧快照整文件覆盖会丢掉"轮询期间其他写入者（命令/另一轮任务）刚标的
    条目"——seen 是标记集合，并集语义下重复无害、丢失即漏推/重推。
    """
    merged = dict(current_state) if isinstance(current_state, dict) else {}
    if isinstance(incoming, dict):
        merged.update(
            {
                k: v
                for k, v in incoming.items()
                if k not in ("seen", "delivered", "touch")
            }
        )
        # delivered（per-session 投递进度，审计 #9）：按条目并集合并
        incoming_delivered = incoming.get("delivered")
        if isinstance(incoming_delivered, dict):
            merged_delivered = dict(merged.get("delivered") or {})
            for key, sessions in incoming_delivered.items():
                if not isinstance(sessions, list):
                    continue
                current_sessions = merged_delivered.get(key) or []
                merged_delivered[key] = list(
                    dict.fromkeys([*current_sessions, *sessions])
                )
            merged["delivered"] = merged_delivered
        incoming_seen = incoming.get("seen")
        if isinstance(incoming_seen, list):
            fresh_seen = merged.get("seen")
            seen_list = list(fresh_seen) if isinstance(fresh_seen, list) else []
            seen_set = {str(key) for key in seen_list}
            for key in incoming_seen:
                if str(key) not in seen_set:
                    seen_set.add(str(key))
                    seen_list.append(key)
            touch = incoming.get("touch")
            if isinstance(touch, list) and touch:
                touch_set = {str(k) for k in touch if str(k) in seen_set}
                if touch_set:
                    kept = [k for k in seen_list if str(k) not in touch_set]
                    refreshed = [
                        k for k in dict.fromkeys(str(k) for k in touch) if k in touch_set
                    ]
                    seen_list = kept + refreshed
            merged["seen"] = seen_list
        # delivered 只对"未晋升 seen"的条目有意义；已见键的进度是死数据。
        # 并集合并无法表达删除——调用方"清掉 done 键进度"的意图会被磁盘
        # 态复活，状态文件只增不减（审计 #39）。这里按最终 seen 集合过滤，
        # 既让删除生效，也天然约束 delivered 只保留在途条目。
        seen_keys = {str(key) for key in (merged.get("seen") or [])}
        merged_delivered = merged.get("delivered")
        if isinstance(merged_delivered, dict):
            merged_delivered = {
                str(k): v
                for k, v in merged_delivered.items()
                if str(k) not in seen_keys and isinstance(v, list)
            }
            if len(merged_delivered) > _STATE_LIMIT:
                merged_delivered = dict(list(merged_delivered.items())[-_STATE_LIMIT:])
            merged["delivered"] = merged_delivered
    return merged


class PushStateUnavailable(RuntimeError):
    """状态文件瞬时不可读（权限/IO/等锁超时）：调用方跳过本轮，不得当首次基线重建。"""


def _valid_seen_state(state) -> bool:
    """状态形状校验：顶层 dict；seen（若有）为 list；delivered（若有）为 dict。"""
    if not isinstance(state, dict):
        return False
    if not isinstance(state.get("seen"), list) and "seen" in state:
        return False
    if not isinstance(state.get("delivered"), dict) and "delivered" in state:
        return False
    return True


def _sanitize_seen_state(state: dict) -> dict:
    """delivered 值级容错：非 list 的进度项丢弃（仅投递进度，可重建）。"""
    delivered = state.get("delivered")
    if isinstance(delivered, dict):
        bad = [k for k, v in delivered.items() if not isinstance(v, list)]
        for k in bad:
            delivered.pop(k)
        if bad:
            logger.warning(
                f"[ArknightsUID·推送] delivered 含 {len(bad)} 个非列表进度项，已丢弃"
            )
    return state


def _quarantine_corrupt(path: Path, label: str, reason: str) -> bool:
    """隔离损坏的状态文件（须在 file_locked 内调用）；备份失败返回 False。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}-{os.getpid()}")
    try:
        os.replace(path, backup)
    except OSError as exc:
        logger.error(
            f"[ArknightsUID·{label}] 损坏文件备份失败（{reason}），"
            f"保留原件不覆盖，下轮重试: {exc.__class__.__name__}"
        )
        return False
    logger.warning(
        f"[ArknightsUID·{label}] 状态文件损坏（{reason}），已隔离为 {backup.name}，"
        "按首次运行重建基线——产品取舍：损坏期间的新条目可能漏推，"
        "不做从损坏文件的无损恢复"
    )
    return True


def _decode_seen_state(raw: str) -> tuple[dict | None, str]:
    """已成功读到的文本 → (有效状态, "")；内容损坏 → (None, 原因)。

    内容损坏 = JSON 语法 / 结构形状 / 非法 UTF-8 解码结果；读取本身的
    OSError 不经过这里（那是瞬时错误，不等于文件损坏）。
    """
    try:
        state = json.loads(raw)
    except ValueError:
        return None, "非合法 JSON"
    if not _valid_seen_state(state):
        return None, "结构不符（非对象或 seen/delivered 形状错误）"
    return _sanitize_seen_state(state), ""


def _load_seen_state(path: Path, label: str) -> dict:
    """推送状态三态读取：

    - 缺文件 → {}（首次运行建基线，不推送）
    - 读取 OSError（权限/IO，首次与锁内复核同口径）→ 抛
      PushStateUnavailable：调用方跳过本轮，绝不能当首次基线去覆盖
      （那会丢 seen 造成漏推/重推），也**不隔离**——读失败不等于损坏
    - 内容损坏（非法 UTF-8 / JSON 语法 / []/null/数字逃出 dict 契约）
      → 锁内复核仍损坏才隔离原件（.corrupt-*），返回 {} 重建有效基线；
      复核发现其他进程已重建好文件则直接采用；备份失败抛
      PushStateUnavailable（不破坏原件，下轮重试）
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        state, reason = None, f"非法 UTF-8（{exc.__class__.__name__}）"
    except OSError as exc:
        logger.warning(
            f"[ArknightsUID·{label}] 状态读取失败（本轮跳过，不重建基线）: "
            f"{exc.__class__.__name__}"
        )
        raise PushStateUnavailable(str(path)) from exc
    else:
        state, reason = _decode_seen_state(raw)
    if state is not None:
        return state

    # 损坏处理与隔离同受文件事务互斥保护；锁竞争视同瞬时不可用
    try:
        with file_locked(path):
            # 锁内复核：读到加锁之间其他进程可能已重建好文件；
            # 复核读取的 OSError 是瞬时错误——不隔离、保留原件、跳过本轮
            try:
                fresh_raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return {}
            except UnicodeDecodeError:
                fresh_state = None
            except OSError as exc:
                logger.warning(
                    f"[ArknightsUID·{label}] 损坏复核读取失败"
                    f"（不隔离、保留原件，本轮跳过）: {exc.__class__.__name__}"
                )
                raise PushStateUnavailable(str(path)) from exc
            else:
                fresh_state, _fresh_reason = _decode_seen_state(fresh_raw)
            if fresh_state is not None:
                return fresh_state
            if not _quarantine_corrupt(path, label, reason):
                raise PushStateUnavailable(str(path))
        return {}
    except PushStateUnavailable:
        raise
    except LockTimeoutError:
        logger.warning(f"[ArknightsUID·{label}] 损坏处理等锁超时，本轮跳过")
        raise PushStateUnavailable(str(path)) from None


def _save_seen_state(path: Path, state: dict, label: str) -> bool:
    """锁内"三态重读 → 合并 → 原子写"；返回是否落盘成功。

    瞬时不可读/隔离失败/等锁超时 → False（不覆盖磁盘，不报成功）。
    """
    from ArknightsUID.utils.file_lock import unique_tmp

    try:
        # 读-合并-写三步全在跨进程文件锁内（审计 #10：多 worker 部署
        # 下另一进程的旧快照不能再整文件覆盖；合并语义见 _merge_seen_state）
        with file_locked(path):
            try:
                fresh = _load_seen_state(path, label)
            except PushStateUnavailable:
                return False
            merged = _merge_seen_state(fresh, state)
            seen = merged.get("seen")
            if isinstance(seen, list) and len(seen) > _STATE_LIMIT:
                merged["seen"] = seen[-_STATE_LIMIT:]
            path.parent.mkdir(parents=True, exist_ok=True)
            # 唯一 tmp + os.replace 原子写：崩溃不留半文件（半文件会被
            # 隔离重建基线，见 _load_seen_state）
            tmp = unique_tmp(path)
            tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            return True
    except LockTimeoutError:
        logger.warning(
            f"[ArknightsUID·{label}] 等锁超时，本轮不落盘（事务失败不报成功）"
        )
        return False
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·{label}] 状态写入失败: {exc.__class__.__name__}: {exc}"
        )
        return False


def load_push_state() -> dict:
    return _load_seen_state(_STATE_PATH, "推送")


def save_push_state(state: dict) -> bool:
    return _save_seen_state(_STATE_PATH, state, "推送")


def _load_ann_state() -> dict:
    return _load_seen_state(_ANN_STATE_PATH, "公告推送")


def _save_ann_state(state: dict) -> bool:
    return _save_seen_state(_ANN_STATE_PATH, state, "公告推送")


async def _await_commit(write_op: Callable[[], bool]) -> bool:
    """等待线程内落盘事务真正结束；协程被取消也先持锁等它提交。

    to_thread 的线程不可取消：直接 await 时协程取消会立刻退出
    ``async with _push_state_lock``，而迟到线程仍在等锁/写盘——下一轮
    检查得以读到旧 seen 重复发送。这里 shield 持锁等待底层事务完成
    （期间重复取消同样等待），结束后再传播取消。
    """
    task = asyncio.ensure_future(asyncio.to_thread(write_op))
    pending_cancel: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            pending_cancel = pending_cancel or exc
            if task.done():
                raise pending_cancel from None
            continue
        if pending_cancel is not None:
            raise pending_cancel from None
        return result


def diff_new_announcements(
    anns: list[dict[str, str]], seen: list[str]
) -> list[dict[str, str]]:
    """公告列表中未见过的（保持新→旧原序；无 id 的条目跳过）。"""
    seen_set = {str(key) for key in seen}
    return [ann for ann in anns if ann.get("id") and str(ann["id"]) not in seen_set]


def format_ann_push_message(fresh: list[dict[str, str]]) -> str:
    """新公告 → 推送文案（标题+日期+链接，单轮最多 5 条）。"""
    lines = ["「明日方舟」新公告"]
    for ann in fresh[:_ANN_PUSH_LIMIT]:
        date = f"（{ann['date']}）" if ann.get("date") else ""
        lines.append(f"《{ann['title']}》{date}")
        if ann.get("url"):
            lines.append(f"  {ann['url']}")
    if len(fresh) > _ANN_PUSH_LIMIT:
        lines.append(f"……（本轮共 {len(fresh)} 条，其余下轮继续推）")
    lines.append("发送「ark公告」查看列表与正文。")
    return "\n".join(lines)


async def _safe_ann_detail(ann_id: str) -> dict | None:
    """公告详情拉取（推送链路用）：任何异常/空结果静默降级为 None。"""
    if not ann_id:
        return None
    try:
        detail = await get_ann_detail(ann_id)
        return detail if isinstance(detail, dict) and detail else None
    except Exception:
        logger.debug("[ArknightsUID·公告推送] 详情拉取失败", exc_info=True)
        return None


async def _build_ann_push_segments(
    fresh: list[dict[str, str]],
    index_by_id: dict[str, int] | None = None,
) -> dict[str, list]:
    """每条新公告 → 消息段列表：公告详情卡图片（+ 超长原图）。

    对齐 EndUID 订阅推送：公告本体直接出图（与 ark公告 N 同版式），
    不再发链接文本。某条详情拉取/渲染失败 → 回退官方封面图；封面也
    没有 → 文字行兜底（任何情况下不漏公告）。
    """
    from gsuid_core.segment import MessageSegment

    shown = fresh[:_ANN_PUSH_LIMIT]
    details = await asyncio.gather(
        *(_safe_ann_detail(str(ann.get("id") or "")) for ann in shown)
    )
    segments: dict[str, list] = {}
    for ann, detail in zip(shown, details):
        ann_id = str(ann.get("id") or "")
        card: bytes | None = None
        longs: list[bytes] = []
        if detail:
            card, longs = await _render_ann_detail_card(
                detail, (index_by_id or {}).get(ann_id)
            )
        if card:
            segs = [MessageSegment.image(card)]
            segs.extend(MessageSegment.image(b) for b in longs)
            segments[ann_id] = segs
            continue
        # 降级链：官方封面图 → 文字行（标题+日期+链接）
        b64 = await _announce_image_b64(ann.get("cover", ""), quality=80)
        if b64.startswith("data:image/"):
            try:
                segments[ann_id] = [MessageSegment.image(data_uri_bytes(b64))]
                continue
            except Exception as exc:
                logger.debug(
                    f"[ArknightsUID·公告推送] 封面转消息段失败: "
                    f"{exc.__class__.__name__}"
                )
        date = f"（{ann['date']}）" if ann.get("date") else ""
        text = f"「明日方舟」新公告：《{ann['title']}》{date}"
        if ann.get("url"):
            text += f"\n{ann['url']}"
        segments[ann_id] = [MessageSegment.text(text)]
    return segments


async def build_ann_push_payload(
    fresh: list[dict[str, str]],
    index_by_id: dict[str, int] | None = None,
) -> list:
    """新公告 → 扁平消息段列表（按公告顺序展开各条的卡图/超长图/兜底）。"""
    segments = await _build_ann_push_segments(fresh, index_by_id)
    return [
        seg
        for ann in fresh[:_ANN_PUSH_LIMIT]
        for seg in segments.get(str(ann.get("id") or ""), [])
    ]


# 文本回复公共实现在 utils.card.send_text；保留模块级别名供调用点/测试 patch
from ArknightsUID.utils.card import send_text as _send_text  # noqa: E402


def _format_ann_list(anns: list[dict[str, str]]) -> str:
    if not anns:
        return "公告获取失败（森空岛 web 接口不可用或分类待校正），请稍后重试。"
    lines = ["「明日方舟」官方公告（森空岛）"]
    for index, ann in enumerate(anns, start=1):
        date = f" · {ann['date']}" if ann.get("date") else ""
        lines.append(f"{index}. {ann['title']}{date}")
        if ann.get("url"):
            lines.append(f"   {ann['url']}")
    lines.append("发送「ark公告 编号」查看对应公告正文。")
    return "\n".join(lines)


# 文本详情：段落/单段长度上限（聊天窗口可读性）
_DETAIL_MAX_PARAS = 20
_DETAIL_PARA_CHARS = 200


def _format_ann_detail(detail: dict) -> str:
    lines = ["「明日方舟」公告详情（森空岛）", detail.get("title", "")]
    meta = [
        m
        for m in (
            detail.get("userName", ""),
            detail.get("date", ""),
            detail.get("userIpLocation", ""),
        )
        if m
    ]
    if meta:
        lines.append("  " + " · ".join(meta))
    paras = detail.get("paragraphs") or []
    for para in paras[:_DETAIL_MAX_PARAS]:
        lines.append(
            para[:_DETAIL_PARA_CHARS] + ("…" if len(para) > _DETAIL_PARA_CHARS else "")
        )
    if len(paras) > _DETAIL_MAX_PARAS:
        lines.append(f"……（共 {len(paras)} 段）")
    images = detail.get("images") or []
    videos = detail.get("videos") or []
    extra = []
    if images:
        extra.append(f"{len(images)} 图")
    if videos:
        extra.append(f"{len(videos)} 视频")
    if extra:
        lines.append(
            "（含 " + "+".join(extra) + "，超长原图已单独发送，完整内容见链接）"
        )
    if detail.get("id"):
        lines.append(f"https://www.skland.com/article?id={detail['id']}")
    return "\n".join(lines)


# 公告配图高宽比阈值：超过视为超长图（维护公告整图），不内嵌卡片，
# 下载后按原画质单独追发（对齐 EndUID ann_detail_card 的 5 倍阈值）
_ANN_LONG_IMAGE_RATIO = 5.0
# 详情卡内嵌配图上限（防纯图帖把卡片撑爆渲染）；追发超长图上限（防刷屏）
_ANN_CARD_IMAGE_LIMIT = 12
_ANN_LONG_IMAGE_LIMIT = 6
# 公告图宽高合理上限：超过按无效处理（接口异常/脏数据，不进卡片布局）
_ANN_IMG_MAX_SIDE = 100_000


def _safe_img_side(value: object) -> int:
    """公告图宽高安全解析：仅接受有限数值（含数字字符串），异常回 0。

    接口是外部输入：None/列表/字典/"auto"/NaN 等此前会让 int() 直接
    抛 ValueError/TypeError，公告命令整体失败。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        number = float(value.strip())
    else:
        return 0
    if not math.isfinite(number) or number <= 0 or number > _ANN_IMG_MAX_SIDE:
        return 0
    return int(number)


def split_ann_images(detail: dict) -> tuple[list[dict], list[str]]:
    """公告配图 →（内嵌卡片的普通图, 单独追发的超长图直链）。

    与视频封面重复的图片跳过（EndUID 同规则）；无尺寸信息的图按普通图
    处理（内嵌显示宽度由卡片约束）。宽高经 _safe_img_side 归一，脏数据
    不再让命令抛异常。
    """
    video_cover_urls = {
        str(v.get("coverUrl") or "") for v in detail.get("videos") or []
    }
    normal: list[dict] = []
    longs: list[str] = []
    for img in detail.get("images") or []:
        if not isinstance(img, dict):
            continue
        url = str(img.get("url") or "")
        if not url or url in video_cover_urls:
            continue
        width = _safe_img_side(img.get("width"))
        height = _safe_img_side(img.get("height"))
        if width > 0 and height / width > _ANN_LONG_IMAGE_RATIO:
            longs.append(url)
        else:
            normal.append({"url": url, "width": width, "height": height})
    return normal[:_ANN_CARD_IMAGE_LIMIT], longs[:_ANN_LONG_IMAGE_LIMIT]


async def _announce_image_b64(
    url: str,
    quality: int = 80,
    cover_size: tuple | None = None,
    anchor: str = "center",
) -> str:
    """公告配图直链 → base64（下载缓存 + webp 压缩；失败空串降级）。

    cover_size 传 (宽, 高) 时裁切到该尺寸（列表封面缩略用）；
    anchor="top" 顶部锚定（竖长封面取标题区，见 show_ark_announcements）。
    """
    if not url:
        return ""
    try:
        from ArknightsUID.utils.render import get_image_b64_with_cache
        from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

        return await get_image_b64_with_cache(
            url,
            CACHE_PATH / "announce",
            quality=quality,
            cover_size=cover_size,
            anchor=anchor,
        )
    except Exception as e:
        logger.debug(f"[ArknightsUID] 公告配图下载失败: {e}")
        return ""


async def _announce_long_image_bytes(url: str) -> bytes:
    """超长公告图 → 独立追发用图片 bytes（下载缓存 + JPEG；失败空跳过）。"""
    if not url:
        return b""
    try:
        from gsuid_core.utils.image.convert import convert_img

        from ArknightsUID.utils.image import pic_download_from_url
        from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

        img = await pic_download_from_url(CACHE_PATH / "announce", url)
        return await convert_img(img)
    except Exception as e:
        logger.warning(f"[ArknightsUID] 公告超长图处理失败: {e.__class__.__name__}")
        return b""


async def _build_ann_detail_context(
    detail: dict, index: int | None
) -> tuple[dict, list[bytes]]:
    """公告详情 → (详情卡上下文, 超长图列表)。

    配图/视频封面/作者头像/超长图并发下载（此前全串行，首查要等几十次
    网络请求）；单张失败静默剔除。命令（ark公告 N）与订阅推送共用。
    """
    # 配图三路（对齐 EndUID）：普通图全部内嵌卡片、视频封面作封面位、
    # 超长图（维护整图）下载后按原画质在卡片后单独追发
    normal_images, long_urls = split_ann_images(detail)

    async def _card_image(img: dict) -> dict | None:
        b64 = await _announce_image_b64(img["url"])
        if not b64:
            return None
        return {"b64": b64, "width": img["width"], "height": img["height"]}

    async def _video_cover(video: dict) -> dict | None:
        b64 = await _announce_image_b64(str(video.get("coverUrl") or ""))
        return {"b64": b64} if b64 else None

    (
        card_images_raw,
        video_covers_raw,
        avatar_b64,
        long_images_raw,
    ) = await asyncio.gather(
        asyncio.gather(*(_card_image(img) for img in normal_images)),
        asyncio.gather(*(_video_cover(v) for v in detail.get("videos") or [])),
        _announce_image_b64(str(detail.get("userAvatar") or ""), quality=70),
        asyncio.gather(*(_announce_long_image_bytes(u) for u in long_urls)),
    )
    long_images = [b for b in long_images_raw if b]
    context = {
        "version": ArknightsUID_version,
        "index": index,
        "title": detail.get("title", ""),
        "date": detail.get("date", ""),
        "author": detail.get("userName", ""),
        "avatar": avatar_b64,
        "ip": detail.get("userIpLocation", ""),
        "url": f"https://www.skland.com/article?id={detail.get('id', '')}",
        "paragraphs": [p[:400] for p in (detail.get("paragraphs") or [])[:30]],
        "images": [c for c in card_images_raw if c],
        "videos": [v for v in video_covers_raw if v],
        "longImageCount": len(long_images),
        "imageCount": len(detail.get("images") or []),
        "videoCount": len(detail.get("videos") or []),
    }
    return context, long_images


async def _render_ann_detail_card(
    detail: dict, index: int | None
) -> tuple[bytes | None, list[bytes]]:
    """公告详情 → (详情卡图片, 超长图列表)；渲染失败 (None, []) 由调用方降级。"""
    try:
        from ArknightsUID.utils.render import (
            ark_templates,
            render_html,
        )

        context, long_images = await _build_ann_detail_context(detail, index)
        img = await render_html(ark_templates, "ark_announce_detail.html", context)
        if not img:
            return None, []
        return img, long_images
    except Exception:
        logger.debug("[ArknightsUID·公告推送] 详情卡渲染失败", exc_info=True)
        return None, []


async def _show_ann_detail(bot: Bot, ev: Event, index: int) -> None:
    anns = await get_ark_ann_list()
    if not anns:
        return await _send_text(bot, ev, "公告列表获取失败，请稍后重试。")
    if not 1 <= index <= len(anns):
        return await _send_text(
            bot, ev, f"编号超出范围（1~{len(anns)}），请先发送「ark公告」查看列表。"
        )
    ann = anns[index - 1]
    detail = await get_ann_detail(ann["id"])
    if not detail:
        return await _send_text(
            bot, ev, f"公告详情获取失败：{ann['title']}\n{ann.get('url', '')}"
        )
    context, long_images = await _build_ann_detail_context(detail, index)
    await send_card_or_text(
        bot,
        ev,
        "ark_announce_detail.html",
        context,
        _format_ann_detail(detail),
        extra_images=long_images,
    )


async def _activity_banner_b64(url: str) -> str:
    """活动标题图直链 → base64（下载缓存 + webp 压缩；失败空串降级）。"""
    if not url:
        return ""
    try:
        from ArknightsUID.utils.render import get_image_b64_with_cache
        from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

        return await get_image_b64_with_cache(url, CACHE_PATH / "activity", quality=82)
    except Exception as e:
        logger.debug(f"[ArknightsUID] 活动标题图下载失败: {e}")
        return ""


@push_sv.on_command(
    (
        "活动列表",
        "方舟活动",
        "活动",
    ),
    block=True,
)
async def show_ark_activities(bot: Bot, ev: Event) -> None:
    from ArknightsUID.arknightsuid_wiki import send_calendar

    await send_calendar(bot, ev)


@push_sv.on_command(
    (
        "公告",
        "方舟公告",
        "官方公告",
        "gg",
    ),
    block=True,
)
async def show_ark_announcements(bot: Bot, ev: Event) -> None:
    index_text = ev.text.strip()
    if index_text.isdigit():
        return await _show_ann_detail(bot, ev, int(index_text))
    anns = await get_ark_ann_list()
    if not anns:
        return await _send_text(bot, ev, _format_ann_list(anns))
    # 列表卡内嵌官方封面 + 作者头像（对齐 EndUID ann_list_card：
    # 封面裁切 400x224（16:9，与展示框 132x74 同比例）缩略；方舟封面
    # 多竖长图（更新前瞻等整图为 1000x4000+），顶部锚定取标题区切片，
    # 居中裁只会截到中段一条；纯文本公告无封面时按空串降级为纯文字行）
    ann_rows: list[dict] = []
    for ann in anns:
        ann_rows.append(
            {
                "title": ann["title"],
                "date": ann.get("date", ""),
                "url": ann.get("url", ""),
                "author": ann.get("author", ""),
                "cover": await _announce_image_b64(
                    ann.get("cover", ""),
                    quality=60,
                    cover_size=(400, 224),
                    anchor="top",
                ),
                "avatar": await _announce_image_b64(ann.get("avatar", ""), quality=70),
            }
        )
    context = {
        "version": ArknightsUID_version,
        "anns": ann_rows,
    }
    await send_card_or_text(
        bot, ev, "ark_announce.html", context, _format_ann_list(anns)
    )


# 注册顺序约束：计划（含别名 刷图计划）必须先于 刷图推荐（含别名 刷图）注册，
# 否则「ark刷图计划」会被短词「刷图」按 startswith 抢走。


@push_sv.on_command(("计划", "刷图计划"), block=True)
async def show_farm_plan(bot: Bot, ev: Event) -> None:
    """ark计划 材料:数量 [...] → 直刷 vs 合成 规划（去哪刷/多少理智）。"""
    text = ev.text.strip()
    targets = _parse_plan_targets(text)
    if not targets:
        return await _send_text(
            bot,
            ev,
            "用法：ark计划 材料:数量 [材料:数量 …]（如 ark计划 聚酸酯块:10 龙门币:20000）",
        )

    from ArknightsUID.utils.gamedata.craft import (
        craft_vs_farm,
        load_formulas,
    )

    item_index = await load_item_index()
    matrix = await fetch_drop_matrix()
    stages = await fetch_stage_info()
    if not item_index or not matrix or not stages:
        return await _send_text(
            bot, ev, "数据源不可用（gamedata/一图流拉取失败），请稍后再试。"
        )

    from ArknightsUID.utils.yituliu import latest_matrix_by_item

    matrix_by_item = latest_matrix_by_item(matrix)
    stage_meta = {str(s.get("stageId", "")): s for s in stages}
    formulas = await load_formulas()

    lines: list[str] = ["「明日方舟」刷图规划"]
    total_farm = 0.0
    unknown: list[str] = []
    card_rows: list[dict] = []
    for name, count in targets[:6]:
        item_id = _match_item_id(name, item_index)
        if not item_id:
            unknown.append(name)
            continue
        result = craft_vs_farm(
            item_id, count, formulas, item_index, matrix_by_item, stage_meta
        )
        head = f"◆ {result['name']} ×{count}"
        options: list[str] = []
        farm_row = None
        craft_row = None
        if result["farm"]:
            options.append(
                f"直刷 {result['farm']['stage_code']}（{result['farm']['ap_expect']:.0f} 理智/件"
                f"≈{result['farm_sanity']:.0f} 理智，需 {count / result['farm']['rate']:.0f} 把）"
            )
            farm_row = {
                "stage_code": result["farm"]["stage_code"],
                "ap_expect": result["farm"]["ap_expect"],
                "sanity": result["farm_sanity"],
                "times": count / result["farm"]["rate"],
            }
            if result["farm_sanity"] is not None:
                total_farm += result["farm_sanity"]
        if result["craft"] is not None:
            if result["craft_sanity"] is not None:
                leaves = " + ".join(
                    f"{item_index[lid]['name']}×{ln}" if lid in item_index else lid
                    for lid, ln in result["craft_leaves"].items()
                )
                options.append(
                    f"合成（叶子材料 {leaves} ≈{result['craft_sanity']:.0f} 理智）"
                )
                craft_row = {
                    "sanity": result["craft_sanity"],
                    "leaves": leaves,
                }
            else:
                options.append("合成可行，但部分子材料无掉落数据")
                craft_row = {"sanity": None, "leaves": "部分子材料无掉落数据"}
        note = "" if options else "没有样本充足的掉落关卡"
        if not options:
            lines.append(f"{head}：没有样本充足的掉落关卡")
        else:
            lines.append(f"{head}：" + "｜".join(options))
        card_rows.append(
            {
                "name": result["name"],
                "count": count,
                "icon": "",
                "farm": farm_row,
                "craft": craft_row,
                "note": note,
            }
        )
    if unknown:
        lines.append(f"未识别：{'、'.join(unknown)}（支持精确材料名）")
    lines.append(f"直刷合计 ≈{total_farm:.0f} 理智")
    lines.append("数据：ArknightsGameData + ark.yituliu.cn（样本>300 关卡）")
    # 材料图标（受限并发批量挂载，失败空串降级）
    try:
        from ArknightsUID.utils.gamedata.itemicon import item_icon_b64
        from ArknightsUID.utils.image import gather_b64

        icons = await gather_b64(
            item_icon_b64, [str(row.get("name") or "") for row in card_rows]
        )
        for row, icon in zip(card_rows, icons):
            row["icon"] = icon
    except Exception as e:
        logger.debug(f"[ArknightsUID] 计划卡材料图标获取失败: {e}")
    await send_card_or_text(
        bot,
        ev,
        "ark_farm_plan.html",
        {
            "rows": card_rows,
            "total_farm": total_farm,
            "unknown": unknown,
            "version": ArknightsUID_version,
        },
        "\n".join(lines),
    )


@push_sv.on_command(
    (
        "刷图推荐",
        "刷图",
    ),
    block=True,
)
async def show_stage_recommend(bot: Bot, ev: Event) -> None:
    query = ev.text.strip()
    records = await fetch_stage_info()
    if not records:
        return await _send_text(bot, ev, "一图流关卡数据获取失败，请稍后重试。")
    if query:
        rows = rank_stages(records, query)
        if not rows:
            return await _send_text(
                bot,
                ev,
                f"未找到分组「{query}」，可选：主线 / 别传 / 限时活动 / 资源收集",
            )
        lines = [f"「明日方舟」{query} · 刷图效率榜（一图流 spm）"]
        for record in rows:
            lines.append(
                f"  {record.get('stageCode', '?')} {record.get('zoneName', '')} "
                f"spm {float(record.get('spm') or 0):.2f} · {record.get('apCost', '?')} 理智"
            )
        max_spm = max(float(r.get("spm") or 0) for r in rows) or 1.0
        context = {
            "version": ArknightsUID_version,
            "group": query,
            "rows": [
                {
                    "stage_code": str(r.get("stageCode", "?")),
                    "zone": str(r.get("zoneName", "")),
                    "spm": float(r.get("spm") or 0),
                    "ap_cost": r.get("apCost", "?"),
                    "spm_pct": round(float(r.get("spm") or 0) / max_spm * 100),
                }
                for r in rows
            ],
        }
        return await send_card_or_text(
            bot, ev, "ark_stage_rank.html", context, "\n".join(lines)
        )
    await _send_text(bot, ev, format_stage_board(records))


@push_sv.on_command(("材料掉率", "掉率"), block=True)
async def show_drop_rate(bot: Bot, ev: Event) -> None:
    await show_material_overview(bot, ev, view="drop")


@push_sub_sv.on_fullmatch(("订阅方舟活动", "订阅活动"))
async def subscribe_ark_activities(bot: Bot, ev: Event) -> None:
    if ev.group_id is None:
        return await bot.send("请在群聊中订阅")

    data = await gs_subscribe.get_subscribe(TASK_NAME)
    resubscribed = False
    if data:
        for subscribe in data:
            if subscribe.group_id == ev.group_id and _same_bot(subscribe, ev):
                await gs_subscribe.delete_subscribe("session", TASK_NAME, ev)
                resubscribed = True
                break
    await gs_subscribe.add_subscribe(
        "session", task_name=TASK_NAME, event=ev, extra_message=""
    )
    # 首次订阅即初始化基线，避免把存量活动当新活动轰炸
    try:
        state = await asyncio.to_thread(load_push_state)
    except PushStateUnavailable:
        # 瞬时不可读：不当首次基线重建（磁盘可能有在途 delivered 进度，
        # 重建会把未投完的条目晋升为已见 → 部分会话永久漏推）——
        # 与定时任务同口径跳过，下轮按真实状态处理（PushStateUnavailable
        # 契约：调用方跳过本轮，不得当首次基线重建）
        state = None
    if state is not None and ("seen" not in state or not state.get("seen")):
        entries = await collect_current_entries()
        if entries:
            # 必须落盘才成基线；失败只降级为"订阅已建立、基线未保存"，
            # 由下轮定时任务重建（不推送存量），不误报订阅失败
            saved = await _await_commit(
                lambda: save_push_state({"seen": list(entries.keys())})
            )
            if not saved:
                logger.warning(
                    "[ArknightsUID·推送] 订阅已建立，但基线保存失败："
                    "下轮定时任务会重建基线（期间新动态不推送，不轰炸存量）"
                )
    await bot.send(
        "已重新订阅方舟活动推送！" if resubscribed else "成功订阅方舟活动推送！"
    )


@push_sub_sv.on_fullmatch(("取消订阅方舟活动", "取消订阅活动", "退订方舟活动", "退订活动"))
async def unsubscribe_ark_activities(bot: Bot, ev: Event) -> None:
    if ev.group_id is None:
        return await bot.send("请在群聊中取消订阅")
    data = await gs_subscribe.get_subscribe(TASK_NAME)
    if data:
        for subscribe in data:
            if subscribe.group_id == ev.group_id and _same_bot(subscribe, ev):
                await gs_subscribe.delete_subscribe("session", TASK_NAME, ev)
                return await bot.send("成功取消订阅方舟活动！")
    await bot.send("未曾订阅方舟活动！")


@push_sv.on_command(
    (
        "订阅公告",
        "订阅方舟公告",
    ),
    block=True,
)
async def subscribe_ark_announcements(bot: Bot, ev: Event) -> None:
    """订阅官方公告专项推送（session 模式：群聊按群、私聊按人各一条）。"""
    await gs_subscribe.add_subscribe(
        "session", task_name=ANN_TASK_NAME, event=ev, extra_message=""
    )
    # 首次订阅建基线：存量公告不当新公告轰炸（已有基线则不动）
    try:
        has_baseline = bool((await asyncio.to_thread(_load_ann_state)).get("seen"))
    except PushStateUnavailable:
        # 瞬时不可读：不当首次基线重建（防在途 delivered 被晋升为已见
        # 漏推）——与定时任务同口径跳过，下轮按真实状态处理
        has_baseline = None
    if has_baseline is False:
        try:
            current = await get_ark_ann_list(limit=_ANN_FETCH_LIMIT)
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·公告推送] 基线拉取失败: {exc.__class__.__name__}"
            )
            current = []
        if current:
            saved = await _await_commit(
                lambda: _save_ann_state(
                    {"seen": [a["id"] for a in current if a.get("id")]}
                )
            )
            if not saved:
                logger.warning(
                    "[ArknightsUID·公告推送] 订阅已建立，但基线保存失败："
                    "下轮定时任务会重建基线（期间新公告不推送，不轰炸存量）"
                )

    lines = ["已订阅官方公告推送（仅公告；活动/卡池请用 ark订阅方舟活动）"]
    try:
        latest = await get_ark_ann_list(limit=3)
    except Exception:
        latest = []
    if latest:
        lines.append("最近公告：")
        for index, ann in enumerate(latest, start=1):
            date = f" · {ann['date']}" if ann.get("date") else ""
            lines.append(f"{index}. {ann['title']}{date}")
    lines.append("新公告发布后会在这里推送。取消：ark取消订阅公告")
    await _send_text(bot, ev, "\n".join(lines))


@push_sv.on_command(
    (
        "取消订阅公告",
        "取消方舟公告",
        "退订公告推送",
        "退订方舟公告",
        "退订公告",
    ),
    block=True,
)
async def unsubscribe_ark_announcements(bot: Bot, ev: Event) -> None:
    data = await gs_subscribe.get_subscribe(ANN_TASK_NAME) or []
    # session 模式：群聊按 group_id 定位本群订阅；私聊只匹配"私聊行"
    # （group_id 为空的行）。不能按 user_id 匹配——群订阅行的 user_id 存
    # 的是发起人，私聊按 user_id 匹配/删除会连带删掉该用户创建的群订阅，
    # 整群推送静默消失（审计 #39）。
    if ev.group_id:
        mine = [s for s in data if s.group_id == ev.group_id and _same_bot(s, ev)]
        if not mine:
            return await _send_text(bot, ev, "未曾订阅官方公告。")
        await gs_subscribe.delete_subscribe("session", ANN_TASK_NAME, ev)
        return await _send_text(bot, ev, "已取消官方公告推送。")

    mine = [
        s
        for s in data
        if not getattr(s, "group_id", None)
        and s.user_id == ev.user_id
        and _same_bot(s, ev)
    ]
    if not mine:
        return await _send_text(bot, ev, "未曾订阅官方公告。")
    # delete_subscribe 私聊分支按 user_id 整删，无法区分私聊行与该用户
    # 创建的群订阅行——按行 id 精确删（ Subscribe.delete_row 支持任意列过滤）
    from gsuid_core.utils.database.models import Subscribe

    for row in mine:
        await Subscribe.delete_row(id=row.id)
    await _send_text(bot, ev, "已取消官方公告推送。")


async def collect_current_entries() -> dict[str, list[dict[str, str]]]:
    """汇总当前活动/卡池/公告 → {key: 展示行}。单项失败跳过（不阻塞推送）。"""
    entries: dict[str, list[dict[str, str]]] = {}
    try:
        for act in await query_activities(limit=20):
            entries.setdefault(_activity_key("活动", act.title), []).append(
                {
                    "kind": "活动",
                    "title": act.title,
                    "detail": f"{act.start} ~ {act.end}",
                }
            )
    except Exception as exc:
        logger.warning(f"[ArknightsUID·推送] 活动拉取失败: {exc.__class__.__name__}")
    try:
        for pool in await query_gacha_pools(limit=10):
            entries.setdefault(_activity_key("卡池", pool.title), []).append(
                {
                    "kind": "卡池",
                    "title": pool.title,
                    "detail": f"{pool.start} ~ {pool.end}",
                }
            )
    except Exception as exc:
        logger.warning(f"[ArknightsUID·推送] 卡池拉取失败: {exc.__class__.__name__}")
    try:
        for ann in await get_ark_ann_list(limit=_ANN_FETCH_LIMIT):
            if ann.get("id"):
                key = _activity_key("公告", ann["id"])
                entries.setdefault(key, []).append(
                    {"kind": "公告", "title": ann["title"], "detail": ""}
                )
    except Exception as exc:
        logger.warning(f"[ArknightsUID·推送] 公告拉取失败: {exc.__class__.__name__}")
    return entries


def diff_new_entries(
    entries: dict[str, list[dict[str, str]]], seen: list[str]
) -> list[dict[str, str]]:
    """当前条目中未见过的（保留插入序；条目附带其去重 key）。"""
    seen_set = set(seen)
    return [
        {**items[0], "key": key}
        for key, items in entries.items()
        if key not in seen_set
    ]


def _push_check_minutes() -> int:
    """AnnCheckMinute 配置 → 轮询间隔（分钟），异常值回落默认 30。"""
    try:
        value = int(ArkConfig.get_config("AnnCheckMinute").data or 30)
    except Exception:
        value = 30
    return value if value > 0 else 30


def _push_enabled() -> bool:
    """AnnPushEnable 配置 → 推送总开关（False 时定时任务空转）。"""
    try:
        return bool(ArkConfig.get_config("AnnPushEnable").data)
    except Exception:
        return True


async def _push_new_announcements(datas) -> None:
    """公告专项订阅：新公告 → 逐订阅推送（独立 seen 基线，与活动订阅互不影响）。"""
    try:
        anns = await get_ark_ann_list(limit=_ANN_FETCH_LIMIT)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·公告推送] 公告拉取失败: {exc.__class__.__name__}"
        )
        return
    if not anns:
        return

    async with _push_state_lock:
        try:
            state = await asyncio.to_thread(_load_ann_state)
        except PushStateUnavailable:
            # 瞬时 IO/权限/等锁超时：跳过本轮（不当首次基线重建，防漏推）
            return
        seen: list[str] = [str(key) for key in (state.get("seen") or [])]
        if not seen:
            # 无基线（首次运行/状态丢失）：只建基线不推送
            saved = await _await_commit(
                lambda: _save_ann_state(
                    {"seen": [a["id"] for a in anns if a.get("id")]}
                )
            )
            if saved:
                logger.info("[ArknightsUID·公告推送] 基线初始化完成，下轮开始检测")
            else:
                logger.warning(
                    "[ArknightsUID·公告推送] 基线初始化落盘失败，本轮未建立基线；"
                    "下轮重新初始化（期间新公告不推送，不会误发存量）"
                )
            return

        seen_set = set(seen)
        active_ann_seen = [
            str(a["id"]) for a in anns if a.get("id") and str(a["id"]) in seen_set
        ]
        fresh = diff_new_announcements(anns, seen)
        if not fresh:
            if (
                len(seen) > _STATE_LIMIT - 50
                and active_ann_seen
                and set(seen[:60]) & set(active_ann_seen)
            ):
                await _await_commit(
                    lambda: _save_ann_state({"seen": seen, "touch": active_ann_seen})
                )
            logger.info("[ArknightsUID·公告推送] 无新增")
            return

        # 单轮最多推 5 条；其余留下轮增量推
        fresh_ids = [str(ann["id"]) for ann in fresh[:_ANN_PUSH_LIMIT]]
        logger.info(
            f"[ArknightsUID·公告推送] 推送 {len(fresh_ids)} 条新公告"
            f"（会话 {len(datas)} 个）"
        )
        delivered: dict[str, list[str]] = {
            str(k): list(v or [])
            for k, v in (state.get("delivered") or {}).items()
            if isinstance(v, list)
        }
        delivered_before = {k: list(v) for k, v in delivered.items()}

        # 公告本体直接出图（对齐 EndUID 订阅推送）：每条新公告渲染详情卡，
        # 与 ark公告 N 同版式；一轮只构建一次，各订阅会话复用同一批消息段
        index_by_id = {str(a["id"]): i + 1 for i, a in enumerate(anns) if a.get("id")}
        segments_by_id = await _build_ann_push_segments(
            [a for a in fresh if str(a["id"]) in fresh_ids], index_by_id
        )

        async def _build_ann_message(ids: list[str]) -> list:
            segs = [seg for ann_id in ids for seg in segments_by_id.get(ann_id, [])]
            if segs:
                return segs
            # 构建阶段整批失败（理论兜底）：退回纯文字清单
            return format_ann_push_message([a for a in fresh if str(a["id"]) in ids])

        delivered, _any = await _deliver_per_session(
            datas, fresh_ids, _build_ann_message, delivered, "公告推送"
        )
        # 条目对全部会话投递完成 → 推进全局 seen 并清掉其投递进度；
        # 部分完成 → seen 不动、delivered 落盘，下轮只补发未覆盖会话
        sessions = {_session_key(sub) for sub in datas}
        done = [
            key
            for key in fresh_ids
            if sessions and sessions.issubset(set(delivered.get(key) or []))
        ]
        if done or delivered != delivered_before:
            saved = await _await_commit(
                lambda: _save_ann_state(
                    {
                        "seen": seen + done,
                        "touch": active_ann_seen + done,
                        "delivered": {
                            k: v for k, v in delivered.items() if k not in done
                        },
                    }
                )
            )
            if not saved:
                logger.warning(
                    "[ArknightsUID·公告推送] 投递进度落盘失败：本轮已发送的条目"
                    "下轮可能重复推送（delivered 合并语义按并集自愈），"
                    "seen 以下轮磁盘态为准"
                )
        else:
            # 全部投递失败且无新进度：不落盘（seen/delivered 均未变），
            # 下轮整批重试
            logger.warning(
                "[ArknightsUID·公告推送] 全部订阅推送失败，本轮不落盘，下轮重试"
            )


@scheduler.scheduled_job("interval", minutes=_push_check_minutes(), id="ark_push_check")
async def check_ark_activities() -> None:
    """定时检测新活动/卡池/公告并推送订阅群。"""
    if not _push_enabled():
        logger.debug("[ArknightsUID·推送] AnnPushEnable 已关闭，跳过本轮检查")
        return
    datas = await gs_subscribe.get_subscribe(TASK_NAME)
    ann_datas = await gs_subscribe.get_subscribe(ANN_TASK_NAME)
    if not datas and not ann_datas:
        return
    logger.info("[ArknightsUID·推送] 定时任务：检查活动/卡池/公告更新..")

    if ann_datas:
        await _push_new_announcements(ann_datas)
    if not datas:
        return

    entries = await collect_current_entries()
    if not entries:
        logger.info("[ArknightsUID·推送] 本次未拉到任何条目，跳过")
        return

    async with _push_state_lock:
        try:
            state = await asyncio.to_thread(load_push_state)
        except PushStateUnavailable:
            # 瞬时 IO/权限/等锁超时：跳过本轮（不当首次基线重建，防漏推）
            return
        seen: list[str] = list(state.get("seen") or [])
        if not seen:
            # 无基线（首次运行/状态丢失）：只建基线不推送
            saved = await _await_commit(
                lambda: save_push_state({"seen": list(entries.keys())})
            )
            if saved:
                logger.info("[ArknightsUID·推送] 基线初始化完成，下轮开始检测")
            else:
                logger.warning(
                    "[ArknightsUID·推送] 基线初始化落盘失败，本轮未建立基线；"
                    "下轮重新初始化（期间新动态不推送，不会误发存量）"
                )
            return

        seen_set = set(seen)
        active_entry_seen = [k for k in entries.keys() if k in seen_set]
        fresh = diff_new_entries(entries, seen)
        if not fresh:
            if (
                len(seen) > _STATE_LIMIT - 50
                and active_entry_seen
                and set(seen[:60]) & set(active_entry_seen)
            ):
                await _await_commit(
                    lambda: save_push_state({"seen": seen, "touch": active_entry_seen})
                )
            logger.info("[ArknightsUID·推送] 无新增")
            return

        # 单轮最多推 8 条；seen 只标实际推过的 key（多余的留下轮增量推，
        # 否则一次性全标会让第 9 条起永久漏推）
        fresh_keys = [k for k in entries.keys() if k not in seen_set]
        pushed_keys = fresh_keys[:8]

        fresh_by_key = {item["key"]: item for item in fresh if item.get("key")}
        logger.info(
            f"[ArknightsUID·推送] 推送 {len(pushed_keys)} 条新动态"
            f"（会话 {len(datas)} 个）"
        )

        def _build_entry_message(keys: list[str]) -> str:
            lines = ["「明日方舟」新动态"]
            for key in keys:
                item = fresh_by_key.get(key)
                if not item:
                    continue
                detail = f"（{item['detail']}）" if item.get("detail") else ""
                lines.append(f"[{item['kind']}] {item['title']}{detail}")
            return "\n".join(lines)

        delivered: dict[str, list[str]] = {
            str(k): list(v or [])
            for k, v in (state.get("delivered") or {}).items()
            if isinstance(v, list)
        }
        delivered_before = {k: list(v) for k, v in delivered.items()}
        delivered, _any = await _deliver_per_session(
            datas, pushed_keys, _build_entry_message, delivered, "推送"
        )
        # 条目对全部会话投递完成 → 推进全局 seen；部分完成 → 下轮补发
        sessions = {_session_key(sub) for sub in datas}
        done = [
            key
            for key in pushed_keys
            if sessions and sessions.issubset(set(delivered.get(key) or []))
        ]
        if done or delivered != delivered_before:
            saved = await _await_commit(
                lambda: save_push_state(
                    {
                        "seen": seen + done,
                        "touch": active_entry_seen + done,
                        "delivered": {
                            k: v for k, v in delivered.items() if k not in done
                        },
                    }
                )
            )
            if not saved:
                logger.warning(
                    "[ArknightsUID·推送] 投递进度落盘失败：本轮已发送的条目"
                    "下轮可能重复推送（delivered 合并语义按并集自愈），"
                    "seen 以下轮磁盘态为准"
                )
        logger.info("[ArknightsUID·推送] 推送完毕")


# ---------------------------------------------------------------------------
# 材料合成树（ark合成）与刷图规划（ark计划）
# ---------------------------------------------------------------------------


def _match_item_id(query: str, item_index: dict) -> str | None:
    """材料名 → itemId：精确 → 唯一前缀。歧义/无匹配返回 None。"""
    if query in item_index:
        return query
    exact = [iid for iid, info in item_index.items() if info.get("name") == query]
    if len(exact) == 1:
        return exact[0]
    prefix = [
        iid
        for iid, info in item_index.items()
        if str(info.get("name", "")).startswith(query)
    ]
    return prefix[0] if len(prefix) == 1 else None


def _parse_plan_targets(text: str) -> list[tuple[str, int]]:
    """「材料:数量 材料:数量 …」→ [(name, count)]；数量默认 1。"""
    targets: list[tuple[str, int]] = []
    for token in (text or "").split():
        if ":" in token or "：" in token:
            name, _, count_text = token.replace("：", ":").partition(":")
        else:
            name, count_text = token, "1"
        try:
            count = max(1, int(count_text))
        except ValueError:
            count = 1
        if name:
            targets.append((name, count))
    return targets


@push_sv.on_command(("合成树", "合成"), block=True)
async def show_craft_tree(bot: Bot, ev: Event) -> None:
    await show_material_overview(bot, ev, view="craft")


async def _material_inventory(ev: Event, uid: str, index: int) -> dict[str, int] | str | None:
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return credential if uid or index else None
    response = await fetch_cultivate_player(credential)
    owned = parse_inventory(response)
    return owned if owned is not None else "仓库暂不可用，按未持有估算。"


def _material_options(
    result: dict, *, stats_available: bool, formulas_available: bool
) -> list[str]:
    lines = []
    farm, tree = result["farm"], result["craft"]
    farm_sanity, craft_sanity = result["farm_sanity"], result["craft_sanity"]
    if farm:
        lines.append(
            f"推荐直刷 {farm['stage_code']}：约 {farm['ap_expect']:.1f} 理智/件"
            f"（所需数量约 {farm_sanity:g} 理智）"
        )
    elif stats_available:
        lines.append("暂无样本充足的开放掉落关卡。")
    else:
        lines.append("一图流掉落数据暂不可用。")

    if tree:
        cost = "、".join(f"{row['name']}×{row['count']}" for row in tree["costs"])
        lines.append(f"单次配方：{cost} → {tree['output']} 件")
        if result["stock_craftable"] is not None:
            lines.append(f"库存子材料可合 {result['stock_craftable']}/{result['need']} 件（另需龙门币）")
        if craft_sanity is not None:
            lines.append(f"合成所缺底层材料：约 {craft_sanity:g} 理智")
        elif result["craft_missing_rows"]:
            lines.append("部分合成材料缺掉率，暂时无法估算总理智。")
        missing = result["craft_missing_rows"]
        if missing and (result["stock_craftable"] is not None or not farm):
            options = "、".join(
                f"{row['name']}×{row['count']}" + (f"（刷 {row['stage']}）" if row["stage"] else "")
                for row in missing[:8]
            )
            lines.append(f"合成路线还缺：{options}" + ("……" if len(missing) > 8 else ""))
    elif formulas_available:
        lines.append("没有加工站配方。")
    else:
        lines.append("加工站配方暂不可用。")

    if farm_sanity is not None and craft_sanity is not None:
        difference = abs(farm_sanity - craft_sanity)
        if difference < 0.1:
            lines.append("直刷和合成的期望理智相近。")
        else:
            route = "直刷" if farm_sanity < craft_sanity else "合成"
            lines.append(f"{route}预计少用 {difference:.1f} 理智（不折算龙门币和副产物）。")
    return lines


async def show_material_overview(bot: Bot, ev: Event, *, view: str = "overview") -> None:
    try:
        raw, uid, index = split_account(ev.text or "", legacy_uid=False, legacy_index=False)
    except ValueError as exc:
        return await _send_text(bot, ev, str(exc))
    query, separator, amount = raw.replace("：", ":").rpartition(":")
    if not separator:
        query, amount = raw, "1"
    query, amount = query.strip(), amount.strip()
    if not query or not re.fullmatch(r"[0-9]{1,4}", amount) or int(amount) == 0:
        return await _send_text(
            bot, ev, "用法：ark材料 材料名[:目标数量] [2号|UID:数字]，例如 ark材料 固源岩组:10"
        )
    total = int(amount)
    items = await load_item_index()
    item_id = _match_item_id(query, items)
    if not item_id:
        candidates = [
            str(row["name"]) for row in items.values()
            if str(row.get("name") or "").startswith(query)
        ]
        if len(candidates) > 1:
            return await _send_text(bot, ev, f"「{query}」匹配到多种材料：{'、'.join(candidates[:8])}，请写全名。")
        if view == "overview" and (page := await query_page(query)):
            notes = [page.summary] if page.summary else []
            notes.extend(f"{key}：{value}" for key, value in list(page.fields.items())[:5])
            return await send_card_or_text(
                bot, ev, "ark_drop_rate.html",
                {"version": ArknightsUID_version, "item_name": page.title,
                 "header": "百科资料", "summary_lines": notes, "rows": [],
                 "sources": ["PRTS Wiki"]},
                "\n".join([page.title, *notes, page.source_url]),
            )
        return await _send_text(bot, ev, f"未找到材料「{query}」，请核对名称或稍后重试。")

    item_name = str(items[item_id].get("name") or query)
    async def item_summary():
        return await query_page(item_name) if view == "overview" else None

    matrix, stages, formulas, inventory, page = await asyncio.gather(
        fetch_drop_matrix(), fetch_stage_info(), load_formulas(),
        _material_inventory(ev, uid, index), item_summary(),
    )
    matrix_by_item = latest_matrix_by_item(matrix or [])
    stage_meta = {str(stage.get("stageId") or ""): stage for stage in stages or []}
    owned = inventory.get(item_id, 0) if isinstance(inventory, dict) else None
    need = max(0, total - (owned or 0)) if separator else 1
    lines = [f"目标 {total} 件 · 缺 {need} 件"] if separator else ["按 1 件估算"]
    if owned is not None:
        lines.append(f"当前账号持有 {owned} 件")
    elif isinstance(inventory, str):
        lines.append(inventory)
    if not need:
        lines.append("仓库现有数量已达目标；下列为每件的获取参考。")
    if page and page.summary:
        snippet = page.summary.strip()
        lines.append(snippet[:160] + ("…" if len(snippet) > 160 else ""))

    result = craft_vs_farm(
        item_id, need or 1, formulas, items, matrix_by_item, stage_meta,
        inventory=inventory if isinstance(inventory, dict) else None,
    )
    lines += _material_options(
        result, stats_available=bool(matrix and stages), formulas_available=bool(formulas),
    )
    tree = result["craft"]
    recipe = format_craft_tree(tree, items) if tree and (view == "craft" or (view == "drop" and not result["farm"])) else []
    stats = item_drop_stats(item_id, matrix_by_item, stage_meta, top=8) if view == "drop" else []
    max_rate = max((row["rate"] for row in stats), default=1.0)
    rows = [{**row, "rate_pct": round(row["rate"] / max_rate * 100)} for row in stats]
    if view == "overview":
        lines.append(f"展开详情：ark材料掉率 {item_name} / ark合成 {item_name}")
    sources = ["ArknightsGameData（配方）", "一图流（开放关卡的掉落样本）"]
    if page:
        sources.append("PRTS Wiki（简介）")
    context = {
        "version": ArknightsUID_version,
        "item_name": item_name,
        "item_icon": await item_icon_b64(item_name),
        "header": {"overview": "材料全景", "drop": "掉落关卡 · 按掉率降序", "craft": "加工站合成树"}[view],
        "summary_lines": lines,
        "recipe": recipe,
        "rows": rows,
        "sources": sources,
    }
    details = [
        f"  {row['stage_code']} · 每把 {row['rate']:.3f} 件 · "
        f"{row['ap_expect']:.1f} 理智/件 · 样本 {row['times']:,} 把"
        for row in rows
    ]
    await send_card_or_text(
        bot, ev, "ark_drop_rate.html", context,
        "\n".join([f"「明日方舟」{item_name} · {context['header']}", *lines, *recipe, *details, "数据：" + " / ".join(sources)]),
    )
