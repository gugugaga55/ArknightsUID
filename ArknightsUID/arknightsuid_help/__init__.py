from gsuid_core.bot import Bot
from gsuid_core.help.utils import register_help
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.version import ArknightsUID_version

help_sv = SV("ArknightsUID帮助", priority=5)

register_help("ArknightsUID", "ark帮助")

# 命令分组（ark_help.html 的 groups 上下文与 HELP_TEXT 同源，增改命令两处同步）
HELP_GROUPS = [
    {
        "name": "账号",
        "en": "ACCOUNT",
        "cmds": [
            {"cmd": "ark绑定 <凭据>", "desc": "森空岛登录凭据绑定（仅私聊）", "eg": "ark绑定 <森空岛凭据>"},
            {"cmd": "ark扫码登录", "desc": "森空岛 APP 扫码（仅私聊）", "eg": "ark扫码登录"},
            {"cmd": "ark账号", "desc": "绑定账号列表（标注默认）", "eg": "ark账号"},
            {"cmd": "ark切换 <序号|UID>", "desc": "设置默认查询账号", "eg": "ark切换 2"},
            {"cmd": "ark查看绑定", "desc": "本地绑定账号列表", "eg": "ark查看绑定"},
            {"cmd": "ark删除登录 [UID]", "desc": "删除本地登录（仅私聊）", "eg": "ark删除登录"},
        ],
    },
    {
        "name": "查询",
        "en": "QUERY",
        "cmds": [
            {"cmd": "ark理智 / ark便签", "desc": "理智与基础状态卡", "eg": "ark理智 2"},
            {"cmd": "ark日常 / ark每日", "desc": "日常周常进度卡", "eg": "ark日常"},
            {"cmd": "ark剿灭", "desc": "剿灭合成玉进度卡", "eg": "ark剿灭"},
            {"cmd": "ark基建", "desc": "基建总览：无人机/制造/训练/换班", "eg": "ark基建"},
            {"cmd": "ark练度统计", "desc": "全干员练度统计卡", "eg": "ark练度统计"},
            {"cmd": "arkBOX", "desc": "BOX 稀有度/精英化分析卡", "eg": "arkBOX"},
            {"cmd": "ark练度卡 <干员>", "desc": "单干员练度卡（技能效果/别名）", "eg": "ark练度卡 银灰"},
            {"cmd": "ark面板 <干员> [精2 Lv45 潜6 满好 X模 技能2 专3]", "desc": "干员面板模拟（默认你的练度；任意等级/潜能/模组/技能档数值）", "eg": "ark面板 银灰 精2 满级 潜6 X模"},
            {"cmd": "ark模组 <干员>", "desc": "干员模组详情：类型/解锁条件/各级属性增量/解锁材料", "eg": "ark模组 银灰"},
            {"cmd": "ark皮肤", "desc": "皮肤收集进度卡（缺哪些时装）", "eg": "ark皮肤"},
            {"cmd": "ark语音 <干员> [关键词]", "desc": "干员中文台词（PRTS 语音记录，可按内容过滤）", "eg": "ark语音 银灰 合约"},
            {"cmd": "ark别名 <别名>=<干员>", "desc": "自定义干员别名（干员/面板/养成/练度卡/模组生效）", "eg": "ark别名 银羽毛=银灰"},
            {"cmd": "ark肉鸽", "desc": "集成战略进度卡", "eg": "ark肉鸽"},
            {"cmd": "ark肉鸽敌人 <名字> [期名] [N难度] [关名] [紧急]", "desc": "肉鸽敌人面板：机制/免疫/难度 buff（可按关卡与紧急作战精确折算）", "eg": "ark肉鸽敌人 歌利亚 不容拒绝 N15"},
            {"cmd": "ark肉鸽关卡 <关名> [N难度] [紧急]", "desc": "肉鸽关卡出怪面板：该难度下全体敌人折算数值 + 紧急作战", "eg": "ark肉鸽关卡 拆东补西 N15 紧急"},
            {"cmd": "ark签到", "desc": "森空岛每日签到", "eg": "ark签到 2"},
        ],
    },
    {
        "name": "抽卡",
        "en": "GACHA",
        "cmds": [
            {"cmd": "ark抽卡记录", "desc": "抽卡记录统计卡", "eg": "ark抽卡记录"},
            {"cmd": "ark兑换码 <code>", "desc": "礼包码兑换", "eg": "ark兑换码 TFBUIT"},
            {"cmd": "ark养成 <干员>", "desc": "养成材料缺口计算", "eg": "ark养成 银灰"},
            {"cmd": "ark攒抽 [参数]", "desc": "攒抽规划（自动垫刀/卡池）", "eg": "ark攒抽 玉:60000 石:30 垫刀:20"},
            {"cmd": "ark年度 [年份]", "desc": "寻访年度报告（本地记录总结）", "eg": "ark年度 2026"},
            {"cmd": "ark十连 / ark单抽", "desc": "寻访模拟（官方概率+保底，娱乐）", "eg": "ark十连"},
            {"cmd": "ark导出抽卡", "desc": "本地寻访记录导出 JSON 存档", "eg": "ark导出抽卡"},
            {"cmd": "ark养成计划 <干员…>", "desc": "多干员合并养成缺口+刷图建议", "eg": "ark养成计划 银灰 42"},
        ],
    },
    {
        "name": "Wiki",
        "en": "PRTS",
        "cmds": [
            {"cmd": "arkwiki <词条>", "desc": "PRTS 百科查询", "eg": "arkwiki 源石"},
            {"cmd": "ark干员 <干员名>", "desc": "干员资料卡（技能详情/我的练度，支持别名）", "eg": "ark干员 银灰"},
            {"cmd": "ark专精材料 <干员名>", "desc": "技能专精材料清单卡", "eg": "ark专精材料 阿米娅"},
            {"cmd": "ark材料 / ark敌人", "desc": "材料 / 敌方资料卡", "eg": "ark材料 固源岩"},
            {"cmd": "ark关卡 <关卡>", "desc": "关卡资料卡（敌方数值/地图）", "eg": "ark关卡 1-7"},
            {"cmd": "ark公招 <标签>", "desc": "公招标签组合查询", "eg": "ark公招 高级资深干员"},
            {"cmd": "ark公招推荐 <标签…>", "desc": "输全部标签→最优组合排行", "eg": "ark公招推荐 高级资深干员 术师 输出"},
            {"cmd": "ark藏品 <名字>", "desc": "肉鸽藏品效果查询", "eg": "ark藏品 钝爪-典训"},
            {"cmd": "ark危机合约", "desc": "危机合约赛季时间线（18 期全录）", "eg": "ark危机合约"},
            {"cmd": "ark日历", "desc": "活动/卡池日历", "eg": "ark日历"},
        ],
    },
    {
        "name": "活动",
        "en": "PUSH",
        "cmds": [
            {"cmd": "ark活动", "desc": "进行中活动/卡池清单", "eg": "ark活动"},
            {"cmd": "ark公告", "desc": "官方公告（森空岛）", "eg": "ark公告"},
            {"cmd": "ark刷图推荐 [分组]", "desc": "关卡效率榜（一图流）", "eg": "ark刷图推荐 主线"},
            {"cmd": "ark材料掉率 <材料>", "desc": "材料掉落关卡与期望理智", "eg": "ark材料掉率 固源岩组"},
            {"cmd": "ark合成 <材料>", "desc": "加工站合成树（子材料递归）", "eg": "ark合成 聚酸酯块"},
            {"cmd": "ark计划 <材料:数量 …>", "desc": "刷图规划：直刷 vs 合成，多少理智", "eg": "ark计划 聚酸酯块:10"},
            {"cmd": "ark抽签", "desc": "明日方舟主题每日运势", "eg": "ark抽签"},
            {"cmd": "ark帮助搜索 <关键词>", "desc": "按关键词找命令", "eg": "ark帮助搜索 肉鸽"},
            {"cmd": "订阅方舟活动", "desc": "群聊订阅新动态推送", "eg": "订阅方舟活动"},
            {"cmd": "ark订阅理智 [序号]", "desc": "理智回满推送提醒", "eg": "ark订阅理智"},
            {"cmd": "ark取消订阅理智", "desc": "取消理智回满推送", "eg": "ark取消订阅理智"},
            {"cmd": "ark订阅训练 [序号]", "desc": "专精预警+完成提醒", "eg": "ark订阅训练"},
            {"cmd": "ark取消订阅训练", "desc": "取消专精训练推送", "eg": "ark取消订阅训练"},
            {"cmd": "ark订阅剿灭 [序号]", "desc": "周五晚剿灭未完成提醒", "eg": "ark订阅剿灭"},
            {"cmd": "ark取消订阅剿灭", "desc": "取消剿灭周提醒", "eg": "ark取消订阅剿灭"},
            {"cmd": "ark订阅基建 [序号]", "desc": "无人机/贸易/换班/公招推送", "eg": "ark订阅基建"},
            {"cmd": "ark取消订阅基建", "desc": "取消基建推送", "eg": "ark取消订阅基建"},
            {"cmd": "ark订阅签到 [序号]", "desc": "每日 08:05 自动签到并回报", "eg": "ark订阅签到"},
            {"cmd": "ark取消订阅签到", "desc": "取消自动签到", "eg": "ark取消订阅签到"},
        ],
    },
]


def _help_text() -> str:
    lines = ["「明日方舟」ArknightsUID", ""]
    lines.append("自有森空岛登录（优先），无则回退 EndUID；不会回显登录凭据。")
    lines.append("")
    for group in HELP_GROUPS:
        lines.append(f"—— {group['name']} ——")
        for cmd in group["cmds"]:
            lines.append(f"{cmd['cmd']} - {cmd['desc']}")
        lines.append("")
    lines.append("ark帮助 - 查看本帮助")
    return "\n".join(lines)


HELP_TEXT = _help_text()

# 帮助卡每条命令的 Q 版干员图标（EndUID 帮助图式：每条命令一个对应角色）。
# 资产为 templates/assets/chibi/ 下的烘焙 b64（PRTS 官方表情套组，零运行时依赖）。
# 按分组分角色、组内轮换姿势；组名未收录时用全量轮换兜底。
_GROUP_CHIBI_POOLS: dict[str, list[str]] = {
    "账号": ["bss_1", "bss_5", "bss_8", "bss_2", "bss_4", "bss_6"],
    "查询": ["mmz_2", "mmz_1", "mmz_5", "mmz_3", "mmz_4", "mmz_6"],
    "抽卡": ["vvm_6", "vvm_1", "vvm_5", "vvm_2", "vvm_3", "vvm_4"],
    "Wiki": ["mmz_3", "vvm_4", "bss_8", "mmz_5", "bss_2", "mmz_2"],
    "活动": ["vvm_1", "bss_2", "vvm_6", "mmz_1", "bss_5", "vvm_3"],
    "养成": ["mmz_6", "bss_4", "vvm_5", "mmz_2", "bss_6", "vvm_4"],
}
_CHIBI_ALL = [f"{g}_{i}" for g, ids in
              (("mmz", (1, 2, 3, 4, 5, 6)), ("bss", (1, 2, 4, 5, 6, 8)),
               ("vvm", (1, 2, 3, 4, 5, 6))) for i in ids]


def assign_group_icons(groups: list[dict]) -> list[dict]:
    """为每个命令格分配 Q 版干员图标（不改动 HELP_GROUPS 原数据）。"""
    result = []
    for g in groups:
        pool = _GROUP_CHIBI_POOLS.get(str(g.get("name", "")), _CHIBI_ALL)
        cmds = []
        for i, c in enumerate(g.get("cmds", [])):
            cmd = dict(c)
            cmd["icon"] = f"assets/chibi/{pool[i % len(pool)]}.b64"
            cmds.append(cmd)
        result.append({**g, "cmds": cmds})
    return result


@help_sv.on_fullmatch(("帮助", "help", "方舟帮助"), block=True)
async def send_ark_help(bot: Bot, ev: Event) -> None:
    # 标题贴纸与命令格 Q 版图标均为模板烘焙资产，无需运行时下载
    context = {
        "version": ArknightsUID_version,
        "groups": assign_group_icons(HELP_GROUPS),
    }
    await send_card_or_text(bot, ev, "ark_help.html", context, HELP_TEXT)


@help_sv.on_command(("帮助搜索", "搜指令"), block=True)
async def search_ark_help(bot: Bot, ev: Event) -> None:
    """ark帮助搜索 关键词 → 命中命令的纯文本清单（卡片帮助不打断）。"""
    keyword = (ev.text or "").strip()
    if not keyword:
        return await _send_help_text(
            bot, ev, "用法：ark帮助搜索 关键词（如 ark帮助搜索 肉鸽）"
        )
    lowered = keyword.lower()
    hits: list[str] = []
    for group in HELP_GROUPS:
        for cmd in group["cmds"]:
            haystack = f"{cmd['cmd']} {cmd['desc']} {cmd.get('eg', '')}".lower()
            if lowered in haystack:
                hits.append(f"[{group['name']}] {cmd['cmd']} - {cmd['desc']}")
    if not hits:
        return await _send_help_text(
            bot, ev, f"没有命中「{keyword}」的命令；ark帮助 查看全部。"
        )
    lines = [f"「{keyword}」命中 {len(hits)} 条命令："]
    lines.extend(f"- {h}" for h in hits[:15])
    if len(hits) > 15:
        lines.append(f"…共 {len(hits)} 条，仅显示前 15 条")
    await _send_help_text(bot, ev, "\n".join(lines))


async def _send_help_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


# ---------------------------------------------------------------------------
# ark抽签：明日方舟主题每日运势（本地数据，签名按 用户+日期 确定）
# ---------------------------------------------------------------------------
fortune_sv = SV("ArknightsUID抽签", priority=5)

_FORTUNE_LEVELS = ["大吉", "中吉", "小吉", "平", "小凶", "凶"]
_FORTUNE_GOOD = [
    "刷理智", "公开招募", "打肉鸽", "合成材料", "搓玉", "基建换班",
    "攒合成玉", "清理任务", "打危机合约", "专精训练", "抽卡（垫刀已就绪）",
]
_FORTUNE_BAD = [
    "十连", "赌黄票兑换", "硬搓低星材料", "越级打 N15", "拆基建",
    "凌晨四点抽卡", "清空理智药", "连打剿灭", "赌模组任务",
]
# 干员池降级兜底（索引不可用时依然可抽）
_FORTUNE_FALLBACK_CHARS = [
    "阿米娅", "凯尔希", "银灰", "陈", "W", "史尔特尔", "艾雅法拉", "泥岩",
    "缪尔赛思", "逻各斯", "维什戴尔", "荒芜拉普兰德",
]


def _daily_seed(user_id: str, today: str) -> int:
    import hashlib

    return int(
        hashlib.sha256(f"{user_id}:{today}".encode("utf-8")).hexdigest()[:8], 16
    )


def _draw_fortune(seed: int) -> dict[str, str | list[str]]:
    import random

    rng = random.Random(seed)
    return {
        "level": rng.choice(_FORTUNE_LEVELS),
        "char": rng.choice(_FORTUNE_FALLBACK_CHARS),
        "good": rng.sample(_FORTUNE_GOOD, 3),
        "bad": rng.sample(_FORTUNE_BAD, 2),
    }


@fortune_sv.on_fullmatch(("抽签", "方舟抽签", "运势"), block=True)
async def send_daily_fortune(bot: Bot, ev: Event) -> None:
    from datetime import date

    today = date.today().isoformat()
    seed = _daily_seed(f"{ev.bot_id}:{ev.user_id}", today)
    fortune = _draw_fortune(seed)

    # 有索引时从全干员池抽（六星更受运势眷顾的既视感）
    try:
        try:
            from ArknightsUID.utils.gamedata.charinfo import load_char_index
        except ModuleNotFoundError:
            from ArknightsUID.ArknightsUID.utils.gamedata.charinfo import (
                load_char_index,
            )

        index = await load_char_index()
        if index:
            import random

            names = [
                str(v.get("name") or "")
                for v in index.values()
                if str(v.get("name") or "")
            ]
            if names:
                fortune["char"] = random.Random(seed + 1).choice(names)
    except Exception:
        pass

    lines = [
        f"「明日方舟」今日运势（{today}）",
        f"{fortune['level']} · 今日同行干员：{fortune['char']}",
        f"宜：{'、'.join(str(x) for x in fortune['good'])}",
        f"忌：{'、'.join(str(x) for x in fortune['bad'])}",
        "（同一人一天内结果固定；纯属娱乐）",
    ]
    await _send_help_text(bot, ev, "\n".join(lines))
