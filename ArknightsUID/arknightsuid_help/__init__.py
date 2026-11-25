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
    """本次展示用的命令前缀：按宿主触发器注册同款构造规则推导。

    - force_prefix 未禁用且有值 → force 首选；否则自定义 prefix；
      全空且 allow_empty_prefix=True → 空串（合法裸命令）；
      显式清空 force/prefix 且 allow_empty=False → 宿主实际注册裸命令，
      展示同样为空串（不得擅自补 ark）。
    - 尚未完成真实注册（SV 先于本包 __init__ 导入产生占位 Plugins：
      force/prefix/allow_empty 全空与宿主 plugins_sample 默认同形）→
      暂用 ark 默认展示。占位与显式清空同形，靠本包自身就绪标记区分。
    - 不用宿主 get_plugin_available_prefix：它在 disable_force_prefix=
      False、force_prefix 为空而 prefix 非空时也返回空串，会让展示误判
      为裸命令（此时实际触发键带 prefix）。
    """
    from gsuid_core.sv import SL

    plugin = SL.plugins.get("ArknightsUID")
    if plugin is None:
        return _DEFAULT_PREFIX
    prefixes: list[str] = []
    if not plugin.disable_force_prefix:
        prefixes.extend(p for p in plugin.force_prefix if p)
    prefixes.extend(p for p in plugin.prefix if p)
    if plugin.allow_empty_prefix:
        prefixes.append("")
    if not prefixes:
        # 读模块当前字段（非 import 时冻结值）：支持"先占位 help 导入、
        # 后完成真实注册"的生命周期顺序
        return "" if _registration.registered else _DEFAULT_PREFIX
    for prefix in prefixes:
        if prefix:
            return prefix
    return ""  # 仅 allow_empty_prefix：合法裸命令


def _swap_prefix(text: str, prefix: str) -> str:
    """默认前缀 ark 的命令串 → 当前生效前缀版本（每个「 / 」备选各自换）。"""
    if prefix == _DEFAULT_PREFIX:
        return text
    parts = text.split(" / ")
    swapped = []
    for part in parts:
        if part.startswith(_DEFAULT_PREFIX):
            part = prefix + part[len(_DEFAULT_PREFIX) :]
        swapped.append(part)
    return " / ".join(swapped)


def _display_groups(prefix: str) -> list[dict]:
    """HELP_GROUPS 的展示副本：cmd/eg 按当前前缀重写（原数据不动）。"""
    return [
        {
            **group,
            "cmds": [
                {
                    **cmd,
                    "cmd": _swap_prefix(cmd["cmd"], prefix),
                    "eg": _swap_prefix(cmd.get("eg", ""), prefix),
                }
                for cmd in group["cmds"]
            ],
        }
        for group in HELP_GROUPS
    ]


def _display_group(group: dict, prefix: str) -> dict:
    """单个分组的展示副本（send_ark_help 单组明细视图用）。"""
    return {
        **group,
        "cmds": [
            {
                **cmd,
                "cmd": _swap_prefix(cmd["cmd"], prefix),
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
# 增改命令三处同步）。blurb 是总览卡上的一行说明。
HELP_GROUPS = [
    {
        "name": "账号",
        "en": "ACCOUNT",
        "blurb": "绑定 / 登录 / 多账号切换",
        "cmds": [
            {
                "cmd": "ark绑定 [凭据]",
                "desc": "无参查看登录方式，也可在私聊中绑定凭据",
                "eg": "ark绑定",
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
            {"cmd": "ark账号 / ark查看绑定", "desc": "账号列表（含复用账号与默认标记；查看绑定仅私聊）", "eg": "ark账号"},
            {"cmd": "ark好友 [序号|UID] [页N] [搜索:关键词]", "desc": "游戏好友与最近在线时间", "eg": "ark好友 页2"},
            {"cmd": "ark头像分类 / ark头像列表 [页码] [分类:N]", "desc": "森空岛官方头像目录，无需登录", "eg": "ark头像列表 2"},
            {"cmd": "ark头像 <ID> [分类:N]", "desc": "按目录 ID 查看官方头像图片", "eg": "ark头像 200232"},
            {
                "cmd": "ark切换 <序号|UID>",
                "desc": "设置默认查询账号",
                "eg": "ark切换 2",
            },
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
                "cmd": "ark设置背景 + 图片 / 干员名 [皮肤名] / 重置",
                "desc": "状态卡背景：上传图或干员皮肤立绘（管理员另有全局背景指令）",
                "eg": "ark设置背景 阿米娅 报童",
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
            {
                "cmd": "ark理智 / ark日常 / ark便签",
                "desc": "理智·日常·周常合并状态卡（背景可自定义）",
                "eg": "ark理智 2",
            },
            {"cmd": "ark签到 [2号|全部]", "desc": "签到默认账号、指定账号或自己的全部账号", "eg": "ark签到 全部"},
            {"cmd": "ark剿灭", "desc": "剿灭合成玉进度卡", "eg": "ark剿灭"},
            {
                "cmd": "ark基建",
                "desc": "基建总览：无人机/制造/训练/换班（实时数据，每次查询重新拉取）",
                "eg": "ark基建",
            },
            {
                "cmd": "ark训练室",
                "desc": "训练室专精进度卡（技能/专精目标/剩余/完成时刻；实时数据）",
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
                "cmd": "ark专精 <干员名> [技能2|二技能] [专1|专精2|专三]",
                "desc": "技能专精效果与材料（也可用 ark专精材料 或倒序 ark银灰 专精）",
                "eg": "ark专精 塔露拉 二技能 专三",
            },
            {
                "cmd": "ark别名 <别名>=<干员或期名>",
                "desc": "自定义干员或肉鸽期名别名，目标有歧义时提示选类型",
                "eg": "ark别名 银羽毛=银灰",
            },
            {
                "cmd": "ark别名列表 / ark别名删除 <别名>",
                "desc": "按类型查看或删除我的别名",
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
                "cmd": "ark肉鸽 [期名] [第N局]",
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
                "cmd": "ark肉鸽关卡 <关名> [期名] [分支N] [N难度] [紧急]",
                "desc": "肉鸽关卡出怪面板与地图；同名关卡需选分支，支持难度/紧急",
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
        "name": "卫戍",
        "en": "STRONGHOLD",
        "blurb": "战绩 / 攻略",
        "cmds": [
            {
                "cmd": "ark卫戍 [序号]",
                "desc": "卫戍协议战绩卡：通关/最速/升金/击杀统计与最佳盟约（每日更新）",
                "eg": "ark卫戍",
            },
            {
                "cmd": "ark卫戍攻略 [盟约|策略|干员|组合]",
                "desc": "总览卡/盟约·策略·干员查询；多词条 + 组合筛选社区攻略（AND）",
                "eg": "ark卫戍攻略 炎+不屈+阿米娅",
            },
        ],
    },
    {
        "name": "抽卡",
        "en": "GACHA",
        "blurb": "记录 / 规划 / 模拟寻访",
        "cmds": [
            {
                "cmd": "ark抽卡记录 [页:N]",
                "desc": "抽卡统计卡：全局摘要 + 按卡池分组（每页 3 池，页:2 翻页）",
                "eg": "ark抽卡记录 页:2",
            },
            {
                "cmd": "ark导入抽卡 <链接>",
                "desc": "小黑盒导出链接导入（仅私聊；与官方记录合并去重）",
                "eg": "ark导入抽卡 <链接>",
            },
            {"cmd": "ark兑换码 <码…> [2号|全部]", "desc": "批量兑换礼包码，支持空格或换行", "eg": "ark兑换码 码1 码2 2号"},
            {"cmd": "ark养成 <干员…> [目标]", "desc": "按目标计算缺口并给刷图建议，支持多干员", "eg": "ark养成 银灰 精2 3技能 专3 X模 模1"},
            {
                "cmd": "ark养成计划 <干员…>",
                "desc": "养成规划别名；不同目标用分号分隔",
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
                "desc": "新池首十抽模拟（基础概率，首十抽至少五星）",
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
                "cmd": "ark材料 <材料[:数量]>",
                "desc": "材料全景：掉落、配方与当前账号库存",
                "eg": "ark材料 固源岩组:10",
            },
            {"cmd": "ark敌人 <敌人>", "desc": "敌方资料卡", "eg": "ark敌人 源石虫"},
            {
                "cmd": "ark关卡 <关卡>",
                "desc": "关卡资料卡（敌方数值/地图）",
                "eg": "ark关卡 1-7",
            },
            {
                "cmd": "ark公招 <标签…或截图>",
                "desc": "查交集或自动推荐，支持高资/快活/小车等简称",
                "eg": "ark公招 高资 术师",
            },
            {
                "cmd": "ark公招推荐 <标签…/截图>",
                "desc": "强制推荐组合；只查交集用 ark公招查询",
                "eg": "ark公招推荐 高级资深干员 术师 输出",
            },
            {
                "cmd": "ark危机合约",
                "desc": "危机合约赛季时间线（18 期全录）",
                "eg": "ark危机合约",
            },
            {"cmd": "ark日历 / ark活动", "desc": "活动、卡池与维护日历", "eg": "ark日历"},
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
            {"cmd": "ark活动进度 [活动名] [2号]", "desc": "森空岛个人活动关卡通关进度", "eg": "ark活动进度"},
            {"cmd": "ark活动战绩 [玩法] [2号]", "desc": "引航者试炼/保全派驻/生息演算个人摘要", "eg": "ark活动战绩 引航者"},
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
            {"cmd": "ark订阅 [2号] / ark我的订阅", "desc": "个人提醒与当前会话订阅总览", "eg": "ark订阅 2号"},
            {
                "cmd": "ark订阅日常 [2号] [含签到] / ark退订日常 [2号]",
                "desc": "批量管理选中账号的个人提醒，自动签到另选",
                "eg": "ark订阅日常 2号 含签到",
            },
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
                "desc": "查看 / 删除指令别名（内置：招募 ≡ 公招推荐）",
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


def _group_text(group: dict, prefix: str) -> str:
    lines = [f"「明日方舟」{group['name']}（{group['en']}）—— {group.get('blurb', '')}"]
    for cmd in group["cmds"]:
        lines.append(f"{_swap_prefix(cmd['cmd'], prefix)} - {cmd['desc']}")
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
            lines.append(f"{_swap_prefix(cmd['cmd'], prefix)} - {cmd['desc']}")
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
            shown_eg = _swap_prefix(cmd.get("eg", ""), prefix)
            haystack = (
                f"{cmd['cmd']} {shown} {cmd['desc']} {cmd.get('eg', '')} {shown_eg}"
            ).lower()
            if lowered in haystack:
                hits.append(f"[{group['name']}] {shown} - {cmd['desc']}")
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
