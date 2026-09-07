from gsuid_core.bot import Bot
from gsuid_core.help.utils import register_help
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ArknightsUID.utils.card import send_card_or_text
from ArknightsUID.version import ArknightsUID_version

help_sv = SV("ArknightsUID帮助", priority=5)

register_help("ArknightsUID", "ark帮助")

# 命令分组（ark_help.html / ark_help_overview.html 的上下文与文本帮助同源，
# 增改命令三处同步）。blurb 是总览卡上的一行说明。
HELP_GROUPS = [
    {
        "name": "账号",
        "en": "ACCOUNT",
        "blurb": "绑定 / 登录 / 多账号切换",
        "cmds": [
            {
                "cmd": "ark绑定 <凭据>",
                "desc": "森空岛登录凭据绑定（仅私聊）",
                "eg": "ark绑定 <森空岛凭据>",
            },
            {
                "cmd": "ark扫码登录",
                "desc": "森空岛 APP 扫码（仅私聊）",
                "eg": "ark扫码登录",
            },
            {
                "cmd": "ark登录",
                "desc": "手机号验证码网页登录（未配置 ArkLoginUrl 时回退扫码）",
                "eg": "ark登录",
            },
            {"cmd": "ark账号", "desc": "绑定账号列表（标注默认）", "eg": "ark账号"},
            {
                "cmd": "ark切换 <序号|UID>",
                "desc": "设置默认查询账号",
                "eg": "ark切换 2",
            },
            {"cmd": "ark查看绑定", "desc": "本地绑定账号列表", "eg": "ark查看绑定"},
            {
                "cmd": "ark删除登录 [UID]",
                "desc": "删除本地登录（仅私聊）",
                "eg": "ark删除登录",
            },
            {
                "cmd": "ark设置隐藏UID / ark取消隐藏UID",
                "desc": "仅对自己隐藏/强制显示 UID（覆盖全局设置）",
                "eg": "ark设置隐藏UID",
            },
            {
                "cmd": "ark刷新",
                "desc": "清游戏数据缓存，下次查询重新下载",
                "eg": "ark刷新",
            },
        ],
    },
    {
        "name": "状态",
        "en": "STATUS",
        "blurb": "理智 / 日常 / 基建等状态卡",
        "cmds": [
            {"cmd": "ark理智 / ark便签", "desc": "理智与基础状态卡", "eg": "ark理智 2"},
            {"cmd": "ark日常 / ark每日", "desc": "日常周常进度卡", "eg": "ark日常"},
            {"cmd": "ark签到", "desc": "森空岛每日签到", "eg": "ark签到 2"},
            {"cmd": "ark剿灭", "desc": "剿灭合成玉进度卡", "eg": "ark剿灭"},
            {
                "cmd": "ark基建",
                "desc": "基建总览：无人机/制造/训练/换班",
                "eg": "ark基建",
            },
            {
                "cmd": "ark训练室",
                "desc": "训练室专精进度卡（技能/专精目标/剩余/完成时刻）",
                "eg": "ark训练室",
            },
            {"cmd": "ark练度统计", "desc": "全干员练度统计卡", "eg": "ark练度统计"},
            {"cmd": "arkBOX", "desc": "BOX 稀有度/精英化分析卡", "eg": "arkBOX"},
            {"cmd": "ark皮肤", "desc": "皮肤收集进度卡（缺哪些时装）", "eg": "ark皮肤"},
        ],
    },
    {
        "name": "干员",
        "en": "OPERATOR",
        "blurb": "练度 / 面板 / 模组 / 别名",
        "cmds": [
            {
                "cmd": "ark练度卡 <干员>",
                "desc": "单干员练度卡（技能效果/别名）",
                "eg": "ark练度卡 银灰",
            },
            {
                "cmd": "ark面板 <干员> [精2 Lv45 潜6 满好 X模 技能2 专3]",
                "desc": "干员面板模拟（默认你的练度；任意等级/潜能/模组/技能档数值）",
                "eg": "ark面板 银灰 精2 满级 潜6 X模",
            },
            {
                "cmd": "ark模组 <干员>",
                "desc": "干员模组详情：类型/解锁条件/各级属性增量/解锁材料",
                "eg": "ark模组 银灰",
            },
            {
                "cmd": "ark语音 <干员> [关键词]",
                "desc": "干员中文台词（PRTS 语音记录，可按内容过滤）",
                "eg": "ark语音 银灰 合约",
            },
            {
                "cmd": "ark干员 <干员名>",
                "desc": "干员资料卡（技能详情/我的练度，支持别名）",
                "eg": "ark干员 银灰",
            },
            {
                "cmd": "ark专精材料 <干员名>",
                "desc": "技能专精材料清单卡",
                "eg": "ark专精材料 阿米娅",
            },
            {
                "cmd": "ark别名 <别名>=<干员>",
                "desc": "自定义干员别名（干员/面板/养成/练度卡/模组生效）",
                "eg": "ark别名 银羽毛=银灰",
            },
            {
                "cmd": "ark别名列表 / ark别名删除 <别名>",
                "desc": "查看 / 删除我的自定义别名",
                "eg": "ark别名删除 银羽毛",
            },
        ],
    },
    {
        "name": "肉鸽",
        "en": "ROGUE",
        "blurb": "进度 / 战绩 / 敌人 / 关卡 / 结局",
        "cmds": [
            {
                "cmd": "ark肉鸽",
                "desc": "集成战略进度卡（各期通关/声望/收藏合计）",
                "eg": "ark肉鸽",
            },
            {
                "cmd": "ark肉鸽战绩 [期名]",
                "desc": "单期战绩明细：最高难度/得分/结算阵容/结局（森空岛战绩页）",
                "eg": "ark肉鸽战绩 界园",
            },
            {
                "cmd": "ark肉鸽详情 [期名] <#局号> [收藏]",
                "desc": "单局战况：开局/结算阵容、藏品清单、结局回顾、种子（局号见战绩卡）",
                "eg": "ark肉鸽详情 界园 3",
            },
            {
                "cmd": "ark肉鸽敌人 <名字> [期名] [N难度] [关名] [紧急]",
                "desc": "肉鸽敌人面板：机制/免疫/难度 buff（期名支持 水月/界园/树海 等简称）",
                "eg": "ark肉鸽敌人 汉科 树海 N15",
            },
            {
                "cmd": "ark肉鸽关卡 <关名> [N难度] [紧急]",
                "desc": "肉鸽关卡出怪面板：该难度下全体敌人折算数值 + 紧急作战",
                "eg": "ark肉鸽关卡 拆东补西 N15 紧急",
            },
            {
                "cmd": "ark肉鸽结局 [期名/结局名/藏品名]",
                "desc": "各期肉鸽结局前置条件：解锁链/关键藏品/最终BOSS（支持按关键藏品名反查）",
                "eg": "ark肉鸽结局 萨卡兹",
            },
            {
                "cmd": "ark藏品 <名字>",
                "desc": "肉鸽藏品效果查询",
                "eg": "ark藏品 钝爪-典训",
            },
            {
                "cmd": "ark肉鸽别名 <别名>=<期名>",
                "desc": "自定义肉鸽期名（肉鸽战绩/敌人/关卡/结局期名参数生效）",
                "eg": "ark肉鸽别名 树海=沉沦者的黑流树海",
            },
            {
                "cmd": "ark肉鸽别名列表 / ark肉鸽别名删除 <别名>",
                "desc": "查看 / 删除我的肉鸽期名",
                "eg": "ark肉鸽别名删除 树海",
            },
        ],
    },
    {
        "name": "抽卡",
        "en": "GACHA",
        "blurb": "记录 / 规划 / 模拟寻访",
        "cmds": [
            {"cmd": "ark抽卡记录", "desc": "抽卡记录统计卡", "eg": "ark抽卡记录"},
            {"cmd": "ark兑换码 <code>", "desc": "礼包码兑换", "eg": "ark兑换码 TFBUIT"},
            {"cmd": "ark养成 <干员>", "desc": "养成材料缺口计算", "eg": "ark养成 银灰"},
            {
                "cmd": "ark养成计划 <干员…>",
                "desc": "多干员合并养成缺口+刷图建议",
                "eg": "ark养成计划 银灰 42",
            },
            {
                "cmd": "ark攒抽 [参数]",
                "desc": "攒抽规划（自动垫刀/卡池）",
                "eg": "ark攒抽 玉:60000 石:30 垫刀:20",
            },
            {
                "cmd": "ark年度 [年份]",
                "desc": "寻访年度报告（本地记录总结）",
                "eg": "ark年度 2026",
            },
            {
                "cmd": "ark十连 / ark单抽",
                "desc": "寻访模拟（官方概率+保底，娱乐）",
                "eg": "ark十连",
            },
            {
                "cmd": "ark导出抽卡 / ark删除抽卡记录",
                "desc": "本地寻访记录导出 JSON / 清空存档（隐私清理）",
                "eg": "ark导出抽卡",
            },
        ],
    },
    {
        "name": "Wiki",
        "en": "PRTS",
        "blurb": "百科 / 图鉴 / 公招 / 日历",
        "cmds": [
            {"cmd": "arkwiki <词条>", "desc": "PRTS 百科查询", "eg": "arkwiki 源石"},
            {
                "cmd": "ark材料 / ark敌人",
                "desc": "材料 / 敌方资料卡",
                "eg": "ark材料 固源岩",
            },
            {
                "cmd": "ark关卡 <关卡>",
                "desc": "关卡资料卡（敌方数值/地图）",
                "eg": "ark关卡 1-7",
            },
            {
                "cmd": "ark公招 <标签>",
                "desc": "公招标签组合查询",
                "eg": "ark公招 高级资深干员",
            },
            {
                "cmd": "ark公招推荐 <标签…/截图>",
                "desc": "输标签或发公招截图→最优组合排行",
                "eg": "ark公招推荐 高级资深干员 术师 输出",
            },
            {
                "cmd": "ark危机合约",
                "desc": "危机合约赛季时间线（18 期全录）",
                "eg": "ark危机合约",
            },
            {"cmd": "ark日历", "desc": "活动/卡池日历", "eg": "ark日历"},
        ],
    },
    {
        "name": "刷图",
        "en": "FARM",
        "blurb": "效率榜 / 掉率 / 合成规划",
        "cmds": [
            {
                "cmd": "ark刷图推荐 [分组]",
                "desc": "关卡效率榜（一图流）",
                "eg": "ark刷图推荐 主线",
            },
            {
                "cmd": "ark材料掉率 <材料>",
                "desc": "材料掉落关卡与期望理智",
                "eg": "ark材料掉率 固源岩组",
            },
            {
                "cmd": "ark合成 <材料>",
                "desc": "加工站合成树（子材料递归）",
                "eg": "ark合成 聚酸酯块",
            },
            {
                "cmd": "ark计划 <材料:数量 …>",
                "desc": "刷图规划：直刷 vs 合成，多少理智",
                "eg": "ark计划 聚酸酯块:10",
            },
        ],
    },
    {
        "name": "资讯",
        "en": "NEWS",
        "blurb": "活动 / 公告 / 每日抽签",
        "cmds": [
            {"cmd": "ark活动", "desc": "进行中活动/卡池清单", "eg": "ark活动"},
            {
                "cmd": "ark公告 [编号]",
                "desc": "官方公告列表/正文（森空岛）",
                "eg": "ark公告 2",
            },
            {"cmd": "ark抽签", "desc": "明日方舟主题每日运势", "eg": "ark抽签"},
            {
                "cmd": "ark帮助搜索 <关键词>",
                "desc": "按关键词找命令",
                "eg": "ark帮助搜索 肉鸽",
            },
        ],
    },
    {
        "name": "订阅",
        "en": "PUSH",
        "blurb": "自动提醒（订阅/取消成对）",
        "cmds": [
            {
                "cmd": "ark订阅方舟活动 / ark取消订阅方舟活动",
                "desc": "群聊新动态推送",
                "eg": "ark订阅方舟活动",
            },
            {
                "cmd": "ark订阅公告 / ark取消订阅公告",
                "desc": "官方新公告推送（群聊/私聊）",
                "eg": "ark订阅公告",
            },
            {
                "cmd": "ark订阅理智 [序号] / ark取消订阅理智",
                "desc": "理智回满推送提醒",
                "eg": "ark订阅理智",
            },
            {
                "cmd": "ark订阅训练 [序号] [提前量] / ark取消订阅训练",
                "desc": "专精剩余时间预警（可设提前量）+完成提醒",
                "eg": "ark订阅训练 5小时",
            },
            {
                "cmd": "ark订阅剿灭 [序号] / ark取消订阅剿灭",
                "desc": "周五晚剿灭未完成提醒",
                "eg": "ark订阅剿灭",
            },
            {
                "cmd": "ark订阅基建 [序号] / ark取消订阅基建",
                "desc": "无人机/贸易/换班/公招推送",
                "eg": "ark订阅基建",
            },
            {
                "cmd": "ark订阅签到 [序号] / ark取消订阅签到",
                "desc": "每日按配置时间自动签到并回报",
                "eg": "ark订阅签到",
            },
            {
                "cmd": "ark全部签到",
                "desc": "手动签到全部登录账号（管理员）",
                "eg": "ark全部签到",
            },
        ],
    },
    {
        "name": "管理",
        "en": "ADMIN",
        "blurb": "缓存 / 资源 / 依赖维护",
        "cmds": [
            {
                "cmd": "ark指令别名 <别名>=<命令>",
                "desc": "给指令加触发词，仅 bot 主人可管理（如 招募=公招推荐）",
                "eg": "ark指令别名 招募=公招推荐",
            },
            {
                "cmd": "ark指令别名列表 / ark删除指令别名 <别名>",
                "desc": "查看 / 删除指令别名（内置：ark招募 ≡ ark公招推荐）",
                "eg": "ark删除指令别名 招募",
            },
            {
                "cmd": "ark清理缓存",
                "desc": "清理过期缓存文件（另每天 03:30 自动）",
                "eg": "ark清理缓存",
            },
            {
                "cmd": "ark下载资源 [分类] [校验]",
                "desc": "预下载全部渲染图片（重跑只补新增；「校验」验存量坏档）",
                "eg": "ark下载资源 / ark下载资源 立绘 校验",
            },
            {
                "cmd": "ark更新记录",
                "desc": "查看插件最近更新（git 日志）",
                "eg": "ark更新记录",
            },
            {
                "cmd": "ark安装依赖 [名字]",
                "desc": "兜底补装可选依赖（ocr/takumi，装完即生效）",
                "eg": "ark安装依赖 ocr",
            },
            {
                "cmd": "ark删除无效凭据",
                "desc": "清理已失效的本地登录行",
                "eg": "ark删除无效凭据",
            },
        ],
    },
]

# 总览卡「从这里开始」核心命令（新用户最常用的最小集）
HELP_CORE_CMDS = [
    {"cmd": "ark绑定", "desc": "森空岛凭据绑定（私聊）"},
    {"cmd": "ark理智", "desc": "理智与基础状态"},
    {"cmd": "ark日常", "desc": "日常周常进度"},
    {"cmd": "ark肉鸽战绩", "desc": "肉鸽战绩明细"},
    {"cmd": "ark抽卡记录", "desc": "抽卡记录统计"},
    {"cmd": "arkwiki", "desc": "PRTS 百科查询"},
]


def help_view(query: str) -> tuple[str, dict | None]:
    """ark帮助 的参数解析 → (视图, 分组)。

    视图：overview（无参，总览）/ all（全部，全量）/ group（单组明细）/
    unknown（未命中，调用方回提示）。
    """
    q = (query or "").strip().lower()
    if not q:
        return "overview", None
    if q in ("全部", "all", "全量", "full"):
        return "all", None
    if q.isdigit():
        index = int(q)
        if 1 <= index <= len(HELP_GROUPS):
            return "group", HELP_GROUPS[index - 1]
        return "unknown", None
    for group in HELP_GROUPS:
        if q in (str(group["name"]).lower(), str(group["en"]).lower()):
            return "group", group
    return "unknown", None


def _group_text(group: dict) -> str:
    lines = [f"「明日方舟」{group['name']}（{group['en']}）—— {group.get('blurb', '')}"]
    for cmd in group["cmds"]:
        lines.append(f"{cmd['cmd']} - {cmd['desc']}")
    lines.append("ark帮助 - 返回总览；ark帮助 全部 - 全量命令")
    return "\n".join(lines)


def _overview_text() -> str:
    total = sum(len(g["cmds"]) for g in HELP_GROUPS)
    lines = ["「明日方舟」ArknightsUID", ""]
    lines.append("自有森空岛登录（优先），无则回退 EndUID；不会回显登录凭据。")
    lines.append("")
    lines.append(
        f"共 {total} 条命令 / {len(HELP_GROUPS)} 组，「ark帮助 分组名」看明细："
    )
    for i, group in enumerate(HELP_GROUPS, start=1):
        lines.append(
            f"{i:02d} {group['name']}（{len(group['cmds'])}）{group.get('blurb', '')}"
        )
    lines.append("")
    lines.append("常用：" + " · ".join(c["cmd"] for c in HELP_CORE_CMDS))
    lines.append("ark帮助 全部 - 全量命令；ark帮助搜索 <关键词> - 按词检索")
    return "\n".join(lines)


def _help_text() -> str:
    lines = ["「明日方舟」ArknightsUID", ""]
    lines.append("自有森空岛登录（优先），无则回退 EndUID；不会回显登录凭据。")
    lines.append("")
    for group in HELP_GROUPS:
        lines.append(f"—— {group['name']} ——")
        for cmd in group["cmds"]:
            lines.append(f"{cmd['cmd']} - {cmd['desc']}")
        lines.append("")
    lines.append("ark帮助 <分组> - 单组明细；ark帮助搜索 <关键词> - 按词检索")
    return "\n".join(lines)


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
    "状态": ["mmz_2", "mmz_1", "mmz_5", "mmz_3", "mmz_4", "mmz_6"],
    "干员": ["mmz_3", "vvm_4", "bss_8", "mmz_5", "bss_2", "mmz_2"],
    "肉鸽": ["vvm_2", "bss_4", "vvm_6", "mmz_6", "bss_6", "vvm_4"],
    "抽卡": ["vvm_6", "vvm_1", "vvm_5", "vvm_2", "vvm_3", "vvm_4"],
    "Wiki": ["mmz_5", "bss_8", "vvm_4", "mmz_3", "bss_2", "vvm_5"],
    "刷图": ["bss_4", "mmz_4", "vvm_3", "bss_5", "mmz_1", "vvm_1"],
    "资讯": ["vvm_1", "bss_2", "vvm_6", "mmz_1", "bss_5", "vvm_3"],
    "订阅": ["bss_5", "vvm_3", "mmz_6", "bss_1", "vvm_5", "mmz_4"],
    "管理": ["mmz_6", "bss_4", "vvm_5", "mmz_2", "bss_6", "vvm_4"],
}
_CHIBI_ALL = [
    f"{g}_{i}"
    for g, ids in (
        ("mmz", (1, 2, 3, 4, 5, 6)),
        ("bss", (1, 2, 4, 5, 6, 8)),
        ("vvm", (1, 2, 3, 4, 5, 6)),
    )
    for i in ids
]


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


def _overview_context() -> dict:
    """帮助总览卡上下文（组清单 + 核心命令）。

    每组配一个 Q 版代表图标（组池第一个；烘焙资产，与全量帮助卡同源）。
    """
    return {
        "version": ArknightsUID_version,
        "core_cmds": HELP_CORE_CMDS,
        "groups": [
            {
                "idx": f"{i:02d}",
                "name": g["name"],
                "en": g["en"],
                "blurb": g.get("blurb", ""),
                "count": len(g["cmds"]),
                "icon": (
                    f"assets/chibi/"
                    f"{_GROUP_CHIBI_POOLS.get(str(g['name']), _CHIBI_ALL)[0]}.b64"
                ),
            }
            for i, g in enumerate(HELP_GROUPS, start=1)
        ],
        "total": sum(len(g["cmds"]) for g in HELP_GROUPS),
    }


# 「帮助搜索」关键字长于「帮助」（core 前缀分发按关键字降序），
# ark帮助搜索 恒先命中本处理器，不会被 ark帮助 抢走
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
            bot, ev, f"没有命中「{keyword}」的命令；ark帮助 查看总览。"
        )
    lines = [f"「{keyword}」命中 {len(hits)} 条命令："]
    lines.extend(f"- {h}" for h in hits[:15])
    if len(hits) > 15:
        lines.append(f"…共 {len(hits)} 条，仅显示前 15 条")
    await _send_help_text(bot, ev, "\n".join(lines))


@help_sv.on_command(("帮助", "help", "方舟帮助", "bz"), block=True)
async def send_ark_help(bot: Bot, ev: Event) -> None:
    """两级帮助：无参=总览卡；「全部」=全量卡；分组名/序号=单组明细。"""
    view, group = help_view(ev.text)
    if view == "overview":
        await send_card_or_text(
            bot, ev, "ark_help_overview.html", _overview_context(), _overview_text()
        )
        return
    if view == "all":
        # 全量卡逐命令带 Q 版图标（烘焙资产，无运行时下载）
        context = {
            "version": ArknightsUID_version,
            "groups": assign_group_icons(HELP_GROUPS),
        }
        await send_card_or_text(bot, ev, "ark_help.html", context, HELP_TEXT)
        return
    if view == "group" and group is not None:
        context = {
            "version": ArknightsUID_version,
            "groups": assign_group_icons([group]),
            "focus": str(group["name"]),
        }
        await send_card_or_text(bot, ev, "ark_help.html", context, _group_text(group))
        return
    names = "、".join(str(g["name"]) for g in HELP_GROUPS)
    await _send_help_text(
        bot,
        ev,
        f"未找到分组「{(ev.text or '').strip()}」。可用：{names}；"
        "ark帮助 看总览，ark帮助 全部 看全量。",
    )


async def _send_help_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


# ---------------------------------------------------------------------------
# ark抽签：明日方舟主题每日运势（本地数据，签名按 用户+日期 确定）
# ---------------------------------------------------------------------------
fortune_sv = SV("ArknightsUID抽签", priority=5)

_FORTUNE_LEVELS = ["大吉", "中吉", "小吉", "平", "小凶", "凶"]
_FORTUNE_GOOD = [
    "刷理智",
    "公开招募",
    "打肉鸽",
    "合成材料",
    "搓玉",
    "基建换班",
    "攒合成玉",
    "清理任务",
    "打危机合约",
    "专精训练",
    "抽卡（垫刀已就绪）",
]
_FORTUNE_BAD = [
    "十连",
    "赌黄票兑换",
    "硬搓低星材料",
    "越级打 N15",
    "拆基建",
    "凌晨四点抽卡",
    "清空理智药",
    "连打剿灭",
    "赌模组任务",
]
# 干员池降级兜底（索引不可用时依然可抽）
_FORTUNE_FALLBACK_CHARS = [
    "阿米娅",
    "凯尔希",
    "银灰",
    "陈",
    "W",
    "史尔特尔",
    "艾雅法拉",
    "泥岩",
    "缪尔赛思",
    "逻各斯",
    "维什戴尔",
    "荒芜拉普兰德",
]


def _daily_seed(user_id: str, today: str) -> int:
    import hashlib

    return int(hashlib.sha256(f"{user_id}:{today}".encode()).hexdigest()[:8], 16)


def _draw_fortune(seed: int) -> dict[str, str | list[str]]:
    import random

    rng = random.Random(seed)
    return {
        "level": rng.choice(_FORTUNE_LEVELS),
        "char": rng.choice(_FORTUNE_FALLBACK_CHARS),
        "good": rng.sample(_FORTUNE_GOOD, 3),
        "bad": rng.sample(_FORTUNE_BAD, 2),
    }


# 运势等级 → 卡片主色档（good=吉 金 / plain=平 蓝 / bad=凶 红）
_FORTUNE_TONES = {
    "大吉": "good",
    "中吉": "good",
    "小吉": "good",
    "平": "plain",
    "小凶": "bad",
    "凶": "bad",
}


@fortune_sv.on_fullmatch(("抽签", "方舟抽签", "运势"), block=True)
async def send_daily_fortune(bot: Bot, ev: Event) -> None:
    from datetime import date

    today = date.today().isoformat()
    seed = _daily_seed(f"{ev.bot_id}:{ev.user_id}", today)
    fortune = _draw_fortune(seed)

    # 同行干员的卡片展示信息（索引不可用时只留名字，头像/星级整格隐藏）
    char_id = ""
    rarity = 0
    profession = ""

    # 有索引时从全干员池抽（六星更受运势眷顾的既视感）
    try:
        from ArknightsUID.utils.gamedata.charinfo import load_char_index

        index = await load_char_index()
        if index:
            import random

            entries = [
                (cid, str(v.get("name") or ""))
                for cid, v in index.items()
                if str(v.get("name") or "")
            ]
            if entries:
                char_id, name = random.Random(seed + 1).choice(entries)
                fortune["char"] = name
                info = index.get(char_id) or {}
                rarity = int(info.get("rarity") or 0)
                profession = str(info.get("profession") or "")
    except Exception:
        pass

    # 同行干员初始头像（磁盘缓存，失败空串降级）
    avatar = ""
    if char_id:
        try:
            from ArknightsUID.utils.player.context import char_avatar_b64

            avatar = await char_avatar_b64(char_id)
        except Exception:
            avatar = ""

    level = str(fortune["level"])
    context = {
        "date": today,
        "level": level,
        "tone": _FORTUNE_TONES.get(level, "plain"),
        "char": str(fortune["char"]),
        "avatar": avatar,
        "rarity": rarity,
        "profession": profession,
        "good": [str(x) for x in fortune["good"]],
        "bad": [str(x) for x in fortune["bad"]],
        "version": ArknightsUID_version,
    }

    lines = [
        f"「明日方舟」今日运势（{today}）",
        f"{level} · 今日同行干员：{fortune['char']}",
        f"宜：{'、'.join(str(x) for x in fortune['good'])}",
        f"忌：{'、'.join(str(x) for x in fortune['bad'])}",
        "（同一人一天内结果固定；纯属娱乐）",
    ]
    await send_card_or_text(bot, ev, "ark_fortune.html", context, "\n".join(lines))
