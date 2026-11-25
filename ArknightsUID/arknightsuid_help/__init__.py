from gsuid_core.bot import Bot
from gsuid_core.help.utils import plugins_help, register_help
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ArknightsUID.utils.card import send_card_or_text
from ArknightsUID.utils import registration as _registration
from ArknightsUID.version import ArknightsUID_version

help_sv = SV("ArknightsUID帮助", priority=5)

# HELP_GROUPS / HELP_CORE_CMDS 以默认前缀 ark 的规范形式存储（兼容既有
# 表示）；展示层按「当前生效前缀」生成副本，不在数据上原地二次加前缀
_DEFAULT_PREFIX = "ark"


def _effective_prefix() -> str:
    """兼容既有导入入口，委托 utils.registration.effective_prefix 单一事实源。"""
    return _registration.effective_prefix()


def _swap_prefix(text: str, prefix: str) -> str:
    """默认前缀 ark 的命令串 → 当前生效前缀版本。"""
    return _registration.swap_ark_prefix(text, prefix)


def _display_groups(prefix: str) -> list[dict]:
    """HELP_GROUPS 的展示副本：cmd/desc/eg 按当前前缀重写（原数据不动）。"""
    return [_display_group(group, prefix) for group in HELP_GROUPS]


def _display_group(group: dict, prefix: str) -> dict:
    """单个分组的展示副本（send_ark_help 单组明细视图用）。"""
    return {
        **group,
        "cmds": [
            {
                **cmd,
                "cmd": _swap_prefix(cmd["cmd"], prefix),
                "desc": _swap_prefix(cmd["desc"], prefix),
                "eg": _swap_prefix(cmd.get("eg", ""), prefix),
            }
            for cmd in group["cmds"]
        ],
    }


def _display_core_cmds(prefix: str) -> list[dict]:
    return [
        {**cmd, "cmd": _swap_prefix(cmd["cmd"], prefix)} for cmd in HELP_CORE_CMDS
    ]


def _register_plugin_help() -> None:
    """把本插件帮助入口注册进宿主插件帮助一览（按插件名替换旧记录）。

    宿主 register_help 只按整字典去重、不会按插件名更新——热重载/改前缀
    后直接追加会积累新旧入口，这里先按名清掉旧记录再注册当前前缀版本。
    """
    entry = f"{_effective_prefix()}帮助"
    data = plugins_help["插件帮助一览"]["data"]
    data[:] = [item for item in data if item.get("name") != "ArknightsUID"]
    register_help("ArknightsUID", entry)


_register_plugin_help()

# 命令分组（ark_help.html / ark_help_overview.html 的上下文与文本帮助同源，
# 增改命令三处同步）。blurb 是总览卡上的一行说明；keywords 供帮助搜索命中被合并的同义词。
HELP_GROUPS = [
    {
        "name": "账号",
        "en": "ACCOUNT",
        "blurb": "绑定 / 登录 / 多账号切换",
        "cmds": [
            {
                "cmd": "ark登录 / ark扫码登录",
                "desc": "网页验证码登录或森空岛 APP 扫码登录（无参 ark绑定 查看引导）",
                "eg": "ark登录",
                "keywords": "网页登录 手机登录",
            },
            {
                "cmd": "ark绑定 <凭据>",
                "desc": "私聊绑定森空岛登录凭据",
                "eg": "ark绑定",
                "keywords": "绑定登录",
            },
            {
                "cmd": "ark账号 / ark查看绑定 / ark切换 <序号|UID>",
                "desc": "查看已登录账号列表（查看绑定仅私聊）或切换默认查询账号",
                "eg": "ark切换 2",
                "keywords": "绑定列表 我的绑定 切换账号",
            },
            {
                "cmd": "ark删除登录 [UID]",
                "desc": "删除本插件本地登录记录（仅私聊）",
                "eg": "ark删除登录",
                "keywords": "解除绑定",
            },
            {
                "cmd": "ark好友 [序号|UID] [页N] [搜索:关键词]",
                "desc": "游戏好友列表与最近在线时间",
                "eg": "ark好友 页2",
            },
            {
                "cmd": "ark头像 [ID | 列表 [页码] | 分类]",
                "desc": "查看森空岛官方头像大图或浏览分类目录（无需登录）",
                "eg": "ark头像列表 2",
                "keywords": "ark头像列表 ark头像分类",
            },
            {
                "cmd": "ark设置隐藏UID / ark设置背景 / ark刷新",
                "desc": "个人 UID 显隐开关、自定义状态卡立绘背景、刷新游戏数据缓存",
                "eg": "ark设置背景 阿米娅 报童",
                "keywords": "ark取消隐藏UID 重置背景",
            },
        ],
    },
    {
        "name": "状态",
        "en": "STATUS",
        "blurb": "理智 / 日常 / 基建等状态卡",
        "cmds": [
            {
                "cmd": "ark理智 / ark日常 / ark便签",
                "desc": "理智·日常·周常合并状态卡（兼容 ark每日）",
                "eg": "ark理智 2",
                "keywords": "ark每日",
            },
            {
                "cmd": "ark签到 [2号|全部]",
                "desc": "签到默认账号、指定账号或自己名下全部账号",
                "eg": "ark签到 全部",
                "keywords": "ark签到全部",
            },
            {
                "cmd": "ark剿灭",
                "desc": "本周剿灭合成玉进度卡",
                "eg": "ark剿灭",
            },
            {
                "cmd": "ark基建 / ark训练室",
                "desc": "基建总览（无人机/制造/贸易/换班）与训练室专精实时进度卡",
                "eg": "ark基建",
                "keywords": "训练进度",
            },
            {
                "cmd": "ark练度统计 / arkBOX / ark皮肤",
                "desc": "全干员练度统计卡、BOX 稀有度分析卡与时装收集进度卡",
                "eg": "arkBOX",
                "keywords": "arkBOX分析 ark皮肤收集",
            },
        ],
    },
    {
        "name": "干员",
        "en": "OPERATOR",
        "blurb": "练度 / 面板 / 模组 / 别名",
        "cmds": [
            {
                "cmd": "ark干员 <干员> / ark练度卡 <干员>",
                "desc": "干员资料卡与个人练度详情（支持外号与自定义别名）",
                "eg": "ark干员 银灰",
            },
            {
                "cmd": "ark面板 <干员> [精2 满级 潜6 X模 技能2 专3]",
                "desc": "任意等级/潜能/模组/技能档位的干员面板数值模拟",
                "eg": "ark面板 银灰 精2 满级 潜6 X模",
            },
            {
                "cmd": "ark专精 <干员> [二技能 专三] / ark模组 <干员>",
                "desc": "技能专精材料与效果（支持倒序 ark银灰 专精）、模组属性与解锁消耗",
                "eg": "ark模组 银灰",
                "keywords": "ark专精材料",
            },
            {
                "cmd": "ark语音 <干员> [关键词]",
                "desc": "干员中文语音台词查询与按词过滤",
                "eg": "ark语音 银灰 合约",
            },
            {
                "cmd": "ark别名 [别名=目标 | 列表 | 删除]",
                "desc": "管理个人干员或肉鸽期名别名（支持 ark别名列表 / ark别名删除）",
                "eg": "ark别名 银羽毛=银灰",
                "keywords": "ark别名列表 ark别名删除 我的别名 干员别名 肉鸽别名",
            },
        ],
    },
    {
        "name": "肉鸽",
        "en": "ROGUE",
        "blurb": "进度 / 战绩 / 敌人 / 关卡 / 结局",
        "cmds": [
            {
                "cmd": "ark肉鸽 [期名] / ark肉鸽战绩 [期名] / ark肉鸽详情 [期名] <#局号>",
                "desc": "各期集成战略总览、单期战绩明细与单局阵容/藏品/种子回顾",
                "eg": "ark肉鸽战绩 界园",
                "keywords": "集成战略",
            },
            {
                "cmd": "ark肉鸽敌人 <名字> [期名] [N难度] [紧急]",
                "desc": "肉鸽敌人机制、抗性与难度 buff 面板（支持水月/界园/树海等简称）",
                "eg": "ark肉鸽敌人 汉科 树海 N15",
            },
            {
                "cmd": "ark肉鸽关卡 <关名> [期名] [分支N] [N难度] [紧急]",
                "desc": "肉鸽关卡出怪面板与路线图（支持同名分支选择与紧急加成）",
                "eg": "ark肉鸽关卡 拆东补西 N15 紧急",
            },
            {
                "cmd": "ark肉鸽结局 [期名|结局名|藏品名]",
                "desc": "各期结局解锁前置链、关键藏品反查与最终 BOSS",
                "eg": "ark肉鸽结局 萨卡兹",
            },
            {
                "cmd": "ark藏品 <名字>",
                "desc": "肉鸽藏品效果查询（期名自定义见 ark别名 / ark肉鸽别名）",
                "eg": "ark藏品 钝爪-典训",
                "keywords": "ark肉鸽别名 ark肉鸽别名列表 ark肉鸽别名删除 我的肉鸽别名",
            },
        ],
    },
    {
        "name": "卫戍",
        "en": "STRONGHOLD",
        "blurb": "总览 / 图鉴 / 攻略",
        "cmds": [
            {
                "cmd": "ark卫戍",
                "desc": "卫戍协议模式总览与图鉴攻略查询入口（兼容 卫戎 / 自走棋）",
                "eg": "ark卫戍",
                "keywords": "ark卫戎 ark自走棋 ark卫戍战绩",
            },
            {
                "cmd": "ark卫戍攻略 <盟约|策略|干员|道具|组合>",
                "desc": "盟约·策略·干员·装备图鉴查询；多词条用 + 组合筛选社区攻略",
                "eg": "ark卫戍攻略 炎+不屈+阿米娅",
                "keywords": "ark卫戎攻略 ark自走棋攻略 卫戍道具 卫戍装备",
            },
        ],
    },
    {
        "name": "抽卡",
        "en": "GACHA",
        "blurb": "记录 / 规划 / 模拟寻访",
        "cmds": [
            {
                "cmd": "ark抽卡记录 [页N] / ark年度 [年份]",
                "desc": "寻访统计按卡池分组分页与年度寻访总结报告",
                "eg": "ark抽卡记录 页2",
                "keywords": "抽卡分析 寻访记录 年度报告",
            },
            {
                "cmd": "ark导入抽卡 <链接> / ark导出抽卡 / ark删除抽卡记录",
                "desc": "私聊导入小黑盒导出链接（合并去重）、导出本地 JSON 或清空记录",
                "eg": "ark导出抽卡",
                "keywords": "小黑盒导入 小黑盒抽卡导入",
            },
            {
                "cmd": "ark养成 <干员…> [目标]",
                "desc": "计算目标练度材料缺口与刷图规划（兼容 ark养成计划，多目标分号分隔）",
                "eg": "ark养成 银灰 精2 3技能 专3 X模 模1",
                "keywords": "ark养成计划",
            },
            {
                "cmd": "ark攒抽 [玉:N 石:N 垫刀:N]",
                "desc": "目标卡池攒抽次数与资源折算规划",
                "eg": "ark攒抽 玉:60000 石:30 垫刀:20",
                "keywords": "ark攒抽规划",
            },
            {
                "cmd": "ark十连 / ark单抽 / ark兑换码 <码…> [2号|全部]",
                "desc": "寻访模拟抽卡；批量兑换礼包码（支持多账号）",
                "eg": "ark十连",
            },
        ],
    },
    {
        "name": "Wiki",
        "en": "PRTS",
        "blurb": "百科 / 图鉴 / 公招 / 日历",
        "cmds": [
            {
                "cmd": "arkwiki <词条> / ark材料 <材料[:数量]>",
                "desc": "PRTS 百科词条查询；材料掉落、加工配方与库存全景",
                "eg": "ark材料 固源岩组:10",
            },
            {
                "cmd": "ark敌人 <敌人> / ark关卡 <关卡>",
                "desc": "主线与活动敌方资料卡、关卡出怪数值与地图",
                "eg": "ark关卡 1-7",
            },
            {
                "cmd": "ark公招 <标签…或截图>",
                "desc": "公招交集与高星组合推荐（支持截图识别、ark公招推荐 / ark公招查询）",
                "eg": "ark公招 高资 术师",
                "keywords": "ark公招推荐 ark公招查询 公开招募 截图识别",
            },
            {
                "cmd": "ark日历 / ark活动",
                "desc": "进行中与近期限时活动、卡池及维护时间轴",
                "eg": "ark日历",
                "keywords": "ark活动列表",
            },
            {
                "cmd": "ark危机合约",
                "desc": "危机合约历届赛季时间线（18 期全录）",
                "eg": "ark危机合约",
            },
        ],
    },
    {
        "name": "刷图",
        "en": "FARM",
        "blurb": "效率榜 / 掉率 / 合成规划",
        "cmds": [
            {
                "cmd": "ark刷图推荐 [分组] / ark材料掉率 <材料>",
                "desc": "一图流关卡效率榜与单材料掉落期望理智排行",
                "eg": "ark材料掉率 固源岩组",
            },
            {
                "cmd": "ark合成 <材料>",
                "desc": "加工站合成树（子材料递归拆解）",
                "eg": "ark合成 聚酸酯块",
                "keywords": "ark合成树",
            },
            {
                "cmd": "ark计划 <材料:数量 …>",
                "desc": "刷图规划：直刷 vs 合成路径对比与总期望理智",
                "eg": "ark计划 聚酸酯块:10",
                "keywords": "ark刷图计划",
            },
        ],
    },
    {
        "name": "资讯",
        "en": "NEWS",
        "blurb": "活动 / 公告 / 每日抽签",
        "cmds": [
            {
                "cmd": "ark活动进度 [活动名] / ark活动战绩 [玩法]",
                "desc": "个人活动关卡通关进度与引航者/保全/生息演算摘要",
                "eg": "ark活动进度",
            },
            {
                "cmd": "ark公告 [编号]",
                "desc": "森空岛明日方舟官方公告列表与正文详情卡",
                "eg": "ark公告 2",
            },
            {
                "cmd": "ark抽签 / ark帮助搜索 <关键词>",
                "desc": "明日方舟每日运势签；按关键词检索任意插件命令",
                "eg": "ark帮助搜索 肉鸽",
                "keywords": "方舟抽签 运势 搜指令",
            },
        ],
    },
    {
        "name": "订阅",
        "en": "PUSH",
        "blurb": "自动提醒（订阅/退订成对）",
        "cmds": [
            {
                "cmd": "ark订阅 [2号] / ark订阅日常 / ark退订日常",
                "desc": "查看个人与本群订阅总览，或一键批量开关日常提醒",
                "eg": "ark订阅日常 2号 含签到",
                "keywords": "ark我的订阅 一键订阅 取消订阅日常",
            },
            {
                "cmd": "ark订阅理智 / ark订阅训练 / ark订阅基建 / ark订阅剿灭 / ark订阅签到",
                "desc": "单项个人提醒（支持 ark订阅 <子项> 与 ark退订 <子项> [2号]）",
                "eg": "ark订阅训练 5小时",
                "keywords": "取消订阅理智 取消订阅训练 取消订阅基建 取消订阅剿灭 取消订阅签到 退订理智 退订训练 退订基建 退订剿灭 退订签到 自动签到",
            },
            {
                "cmd": "ark订阅方舟活动 / ark订阅公告",
                "desc": "群聊活动新动态推送（群管）与官方公告专项推送（配对 ark退订活动/公告）",
                "eg": "ark订阅公告",
                "keywords": "取消订阅方舟活动 取消订阅活动 退订方舟活动 退订活动 取消订阅公告 退订公告",
            },
            {
                "cmd": "ark全部签到",
                "desc": "管理员手动签到全部有效登录账号",
                "eg": "ark全部签到",
                "keywords": "ark全服签到",
            },
        ],
    },
    {
        "name": "管理",
        "en": "ADMIN",
        "blurb": "缓存 / 资源 / 依赖维护",
        "cmds": [
            {
                "cmd": "ark指令别名 [别名=命令 | 列表] / ark删除指令别名 <别名>",
                "desc": "管理全局指令触发别名（仅 bot 主人；内置 招募≡公招推荐）",
                "eg": "ark指令别名 招募=公招推荐",
                "keywords": "ark指令别名列表",
            },
            {
                "cmd": "ark清理缓存 / ark删除无效凭据",
                "desc": "清理过期磁盘缓存（另每日自动执行）或移除已失效登录行",
                "eg": "ark清理缓存",
            },
            {
                "cmd": "ark下载资源 [分类] [校验]",
                "desc": "批量预下载渲染图片资源（增量补齐，「校验」重检坏档）",
                "eg": "ark下载资源 立绘 校验",
            },
            {
                "cmd": "ark安装依赖 [ocr|takumi] / ark更新记录",
                "desc": "聊天内补装可选依赖（装完即生效）或查看最近更新日志",
                "eg": "ark安装依赖 ocr",
                "keywords": "安装组件",
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


def _group_text(group: dict, prefix: str) -> str:
    lines = [f"「明日方舟」{group['name']}（{group['en']}）—— {group.get('blurb', '')}"]
    for cmd in group["cmds"]:
        lines.append(
            f"{_swap_prefix(cmd['cmd'], prefix)} - {_swap_prefix(cmd['desc'], prefix)}"
        )
    lines.append(f"{prefix}帮助 - 返回总览；{prefix}帮助 全部 - 全量命令")
    return "\n".join(lines)


def _overview_text(prefix: str) -> str:
    total = sum(len(g["cmds"]) for g in HELP_GROUPS)
    lines = ["「明日方舟」ArknightsUID", ""]
    lines.append("自有森空岛登录（优先），无则回退 EndUID；不会回显登录凭据。")
    lines.append("")
    lines.append(
        f"共 {total} 条命令 / {len(HELP_GROUPS)} 组，「{prefix}帮助 分组名」看明细："
    )
    for i, group in enumerate(HELP_GROUPS, start=1):
        lines.append(
            f"{i:02d} {group['name']}（{len(group['cmds'])}）{group.get('blurb', '')}"
        )
    lines.append("")
    lines.append("常用：" + " · ".join(_swap_prefix(c["cmd"], prefix) for c in HELP_CORE_CMDS))
    lines.append(f"{prefix}帮助 全部 - 全量命令；{prefix}帮助搜索 <关键词> - 按词检索")
    return "\n".join(lines)


def _help_text(prefix: str) -> str:
    lines = ["「明日方舟」ArknightsUID", ""]
    lines.append("自有森空岛登录（优先），无则回退 EndUID；不会回显登录凭据。")
    lines.append("")
    for group in HELP_GROUPS:
        lines.append(f"—— {group['name']} ——")
        for cmd in group["cmds"]:
            lines.append(
                f"{_swap_prefix(cmd['cmd'], prefix)} - {_swap_prefix(cmd['desc'], prefix)}"
            )
        lines.append("")
    lines.append(f"{prefix}帮助 - 查看本帮助")
    return "\n".join(lines)


def help_text() -> str:
    """全量帮助文本（按当前生效前缀生成；供测试与其他模块取用）。"""
    return _help_text(_effective_prefix())

# 帮助卡每条命令的 Q 版干员图标（EndUID 帮助图式：每条命令一个对应角色）。
# 资产为 templates/assets/chibi/ 下的烘焙 b64（PRTS 官方表情套组，零运行时依赖）。
# 按分组分角色、组内轮换姿势；组名未收录时用全量轮换兜底。
_GROUP_CHIBI_POOLS: dict[str, list[str]] = {
    "账号": ["bss_1", "bss_5", "bss_8", "bss_2", "bss_4", "bss_6"],
    "状态": ["mmz_2", "mmz_1", "mmz_5", "mmz_3", "mmz_4", "mmz_6"],
    "干员": ["mmz_3", "vvm_4", "bss_8", "mmz_5", "bss_2", "mmz_2"],
    "肉鸽": ["vvm_2", "bss_4", "vvm_6", "mmz_6", "bss_6", "vvm_4"],
    "卫戍": ["bss_6", "mmz_3", "vvm_2", "bss_1", "mmz_5", "vvm_6"],
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


def _overview_context(prefix: str) -> dict:
    """帮助总览卡上下文（组清单 + 核心命令，均按当前前缀生成展示副本）。

    每组配一个 Q 版代表图标（组池第一个；烘焙资产，与全量帮助卡同源）。
    """
    return {
        "version": ArknightsUID_version,
        "prefix": prefix,
        "core_cmds": _display_core_cmds(prefix),
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
# <前缀>帮助搜索 恒先命中本处理器，不会被 <前缀>帮助 抢走
@help_sv.on_command(("帮助搜索", "搜指令"), block=True)
async def search_ark_help(bot: Bot, ev: Event) -> None:
    """帮助搜索 关键词 → 命中命令的纯文本清单（按当前前缀展示）。"""
    prefix = _effective_prefix()
    keyword = (ev.text or "").strip()
    if not keyword:
        return await _send_help_text(
            bot, ev, f"用法：{prefix}帮助搜索 关键词（如 {prefix}帮助搜索 肉鸽）"
        )
    lowered = keyword.lower()
    hits: list[str] = []
    for group in HELP_GROUPS:
        for cmd in group["cmds"]:
            shown = _swap_prefix(cmd["cmd"], prefix)
            shown_desc = _swap_prefix(cmd["desc"], prefix)
            shown_eg = _swap_prefix(cmd.get("eg", ""), prefix)
            shown_kw = _swap_prefix(cmd.get("keywords", ""), prefix)
            haystack = (
                f"{cmd['cmd']} {shown} {cmd['desc']} {shown_desc} "
                f"{cmd.get('eg', '')} {shown_eg} {cmd.get('keywords', '')} {shown_kw}"
            ).lower()
            if lowered in haystack:
                hits.append(f"[{group['name']}] {shown} - {shown_desc}")
    if not hits:
        return await _send_help_text(
            bot, ev, f"没有命中「{keyword}」的命令；{prefix}帮助 查看总览。"
        )
    lines = [f"「{keyword}」命中 {len(hits)} 条命令："]
    lines.extend(f"- {h}" for h in hits[:15])
    if len(hits) > 15:
        lines.append(f"…共 {len(hits)} 条，仅显示前 15 条")
    await _send_help_text(bot, ev, "\n".join(lines))


@help_sv.on_command(("帮助", "help", "方舟帮助", "bz"), block=True)
async def send_ark_help(bot: Bot, ev: Event) -> None:
    """两级帮助：无参=总览卡；「全部」=全量卡；分组名/序号=单组明细。

    入口/文本/图上下文均按本次调用时的生效前缀生成（自定义 prefix 或
    禁 force_prefix 后不再展示失效的 ark 命令），并同步刷新宿主帮助
    一览里的入口记录。
    """
    _register_plugin_help()
    prefix = _effective_prefix()
    view, group = help_view(ev.text)
    if view == "overview":
        await send_card_or_text(
            bot,
            ev,
            "ark_help_overview.html",
            _overview_context(prefix),
            _overview_text(prefix),
        )
        return
    if view == "all":
        # 全量卡逐命令带 Q 版图标（烘焙资产，无运行时下载）
        context = {
            "version": ArknightsUID_version,
            "prefix": prefix,
            "groups": assign_group_icons(_display_groups(prefix)),
        }
        await send_card_or_text(bot, ev, "ark_help.html", context, _help_text(prefix))
        return
    if view == "group" and group is not None:
        context = {
            "version": ArknightsUID_version,
            "prefix": prefix,
            "groups": assign_group_icons([_display_group(group, prefix)]),
            "focus": str(group["name"]),
        }
        await send_card_or_text(
            bot, ev, "ark_help.html", context, _group_text(group, prefix)
        )
        return
    names = "、".join(str(g["name"]) for g in HELP_GROUPS)
    await _send_help_text(
        bot,
        ev,
        f"未找到分组「{(ev.text or '').strip()}」。可用：{names}；"
        f"{prefix}帮助 看总览，{prefix}帮助 全部 看全量。",
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
    from ArknightsUID.utils.time_utils import beijing_datetime

    today = beijing_datetime().date().isoformat()
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
