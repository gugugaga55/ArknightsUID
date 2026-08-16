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
            {"cmd": "ark肉鸽", "desc": "集成战略进度卡", "eg": "ark肉鸽"},
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
            {"cmd": "ark藏品 <名字>", "desc": "肉鸽藏品效果查询", "eg": "ark藏品 钝爪-典训"},
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
            {"cmd": "订阅方舟活动", "desc": "群聊订阅新动态推送", "eg": "订阅方舟活动"},
            {"cmd": "ark订阅理智 [序号]", "desc": "理智回满推送提醒", "eg": "ark订阅理智"},
            {"cmd": "ark取消订阅理智", "desc": "取消理智回满推送", "eg": "ark取消订阅理智"},
            {"cmd": "ark订阅训练 [序号]", "desc": "专精训练完成提醒", "eg": "ark订阅训练"},
            {"cmd": "ark取消订阅训练", "desc": "取消专精训练推送", "eg": "ark取消订阅训练"},
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


@help_sv.on_fullmatch(("帮助", "help", "方舟帮助"), block=True)
async def send_ark_help(bot: Bot, ev: Event) -> None:
    figure = ""
    try:
        # 帮助卡装饰干员立绘（透明 PNG 浮空图；失败空串降级）
        from ..utils.player.context import char_portrait_b64

        figure = await char_portrait_b64("char_002_amiya", 5)
    except Exception:
        figure = ""
    context = {
        "version": ArknightsUID_version,
        "groups": HELP_GROUPS,
        "figure": figure,
    }
    await send_card_or_text(bot, ev, "ark_help.html", context, HELP_TEXT)
