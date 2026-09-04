"""肉鸽结局前置条件查询（roguelike_topic_table endings × PRTS 攻略口径）。

数据分两层：
- 结局名单/名称/描述：roguelike_topic_table（ArknightsGameData，权威），
  每期 details[tid].endings 按 id 与下方攻略条目合并——表有而攻略没有的
  结局（新 DLC 刚上线）显示「攻略整理中」，攻略有而表没有的按兜底名显示。
- 前置条件链：人工整理的攻略数据（ENDING_GUIDES，来源 PRTS 各主题页
  「结局」章节，2026-08 核对）。游戏表内没有结构化的结局触发条件
  （choices 只有 lockedCoverDesc 富文本碎片，入口场景在事件图上不可达），
  这部分只能内置维护：新 DLC 结局上线后随版本手动补条目。

关键藏品按「名字」在各期 items 里反查（iconId → tomimi 图标），名字不在
表内时仅显示无图 chip；攻略条目里的 ending_id 是表内结局 id 的 join 键。
"""

from __future__ import annotations

import time
from typing import Any

from ._table_cache import ensure_rogue_topic_table
from .rogue_enemy import THEME_ALIASES, THEME_DIRS, resolve_theme_dir

# 期目录 → 主题表编号（表 rogue_N 从傀影起编；玩家侧数据从灰蕈迷境起编，
# 与 _THEME_ROGUE_NUM 同一推位规律，本模块自带映射避免私有跨模块导入）
THEME_TABLE_IDS = {
    "phantom": "rogue_1",
    "mizuki": "rogue_2",
    "sami": "rogue_3",
    "sarkaz": "rogue_4",
    "sui": "rogue_5",
    "black": "rogue_6",
}

GUIDE_SOURCE_NOTE = "结局名单 ArknightsGameData · 攻略口径 PRTS Wiki"

# 攻略整理时间（新 DLC 结局上线后更新条目时同步改这里，便于排查过期数据）
GUIDE_UPDATED = "2026-08-30"

# ---------------------------------------------------------------------------
# 内置攻略数据（PRTS 各主题页「结局」章节人工摘编）
# 字段：ending_id 表内结局 id / name 兜底名 / label 徽标 / unlock 前置解锁 /
# steps 达成步骤 / key_relics 关键藏品名（items 按名反查图标）/
# final 最终关卡与 BOSS / note 特殊口径。
# ---------------------------------------------------------------------------
ENDING_GUIDES: dict[str, dict[str, Any]] = {
    "phantom": {
        "endings": [
            {
                "ending_id": "ro_ending_1",
                "name": "舞会终场",
                "label": "默认",
                "unlock": "无前置",
                "steps": [
                    "常规探索路线，不持有【恍悟】【初幕】时通过第5层险路恶敌即可",
                ],
                "key_relics": [],
                "final": "ISW-DF 覆水难收（卢西恩·“猩红血钻”）",
            },
            {
                "ending_id": "ro_ending_2",
                "name": "滑稽喜剧",
                "label": "",
                "unlock": "正式调查及以上难度达成过「舞会终场」",
                "steps": [
                    "第4层古堡馈赠前可能遇事件「解脱？」：选“太可怜了，仔细看看”（耗1希望）获得【残破的玩偶】",
                    "（可选）「疯狂玩偶」连续3次“护住玩偶”（共损3生命）后收下，获得【无用的剪刀】",
                    "（可选）「解脱」选“询问对方姓名”获得【空白遗书】，降低最终战难度",
                    "持有【残破的玩偶】（不持恍悟/初幕）通过第5层，险路恶敌被替换",
                ],
                "key_relics": ["残破的玩偶", "无用的剪刀", "空白遗书"],
                "final": "ISW-DF 别无所求（伤心的大锁）",
            },
            {
                "ending_id": "ro_ending_3",
                "name": "盛大揭幕",
                "label": "",
                "unlock": "达成过「滑稽喜剧」",
                "steps": [
                    "第3层险路恶敌前可能遇事件「赴宴」：选“白得一套衣服”获得【替罪领巾】",
                    "持【替罪领巾】通过第3层险路恶敌，战利品里选【替补演员】",
                    "第5层前可能遇「追随者集会」：选“推门而入”获得【恍悟】",
                    "持有【恍悟】通过第5层，进入第6层隐藏区域",
                ],
                "key_relics": ["替罪领巾", "替补演员", "恍悟"],
                "final": "ISW-DF 诸事不顺（“剧团喉舌”）",
            },
            {
                "ending_id": "ro_ending_4",
                "name": "沉默之章",
                "label": "拓展Ⅱ",
                "unlock": "内容拓展Ⅱ上线后，正式调查及以上难度完成任一结局",
                "steps": [
                    "第1层后至第3层险路恶敌前可能遇「别打扰他们」：选“靠近听听他们在说什么”，战斗 ISW-SP 自缚胜后获得【死囚之舞】",
                    "随后可能遇「复仇剧」：选“看看对方在写什么”，战斗 ISW-SP 观察胜后获得【初幕】",
                    "持有【初幕】通过第5层，进入第6层隐藏区域",
                ],
                "key_relics": ["死囚之舞", "初幕"],
                "final": "ISW-DF 再启新篇（“剧作家”）",
            },
        ],
        "notes": [],
    },
    "mizuki": {
        "endings": [
            {
                "ending_id": "ro2_ending_1",
                "name": "平凡即是喜乐",
                "label": "默认",
                "unlock": "无前置",
                "steps": [
                    "常规探索路线，不持有【深蓝之心】【决心】【观望】【犹疑】时通过第5层险路恶敌即可",
                ],
                "key_relics": [],
                "final": "ISW-DF 认知即重担",
            },
            {
                "ending_id": "ro2_ending_2",
                "name": "静谧时代",
                "label": "骑士线",
                "unlock": "波涛迭起及以上难度达成过「平凡即是喜乐」",
                "steps": [
                    "第3层末险路恶敌前可能遇事件「狂徒妄念」：接受骑士帮助获得【海潮的气息】，此后“猎潮的骑士”现身非隐藏层战斗",
                    "（可选）第5层前可能遇「骑士仍在」：选“挣脱”得【骑士骨血】（削弱最终战），或“反抗”扣2目标生命换【荒诞命运】",
                    "抵达第5层前骑士必须一直存活；若中途死亡获得【退行的罗辛南特】，本次探索永久失去本结局",
                    "第5层险路恶敌变为「“命运的宠儿”」，通关即达成",
                ],
                "key_relics": ["海潮的气息", "骑士骨血", "荒诞命运"],
                "final": "ISW-DF “命运的宠儿”（最后的骑士）",
            },
            {
                "ending_id": "ro2_ending_3",
                "name": "息潮的代价",
                "label": "深蓝之心线",
                "unlock": "波涛迭起及以上难度达成过「静谧时代」（解锁「开端」事件）",
                "steps": [
                    "第3层末触发事件「开端」：选“决定潜入深海”获得【主教的研究】（每场战斗后灯火自动减少）",
                    "持有【主教的研究】且灯火≥20时，第5层末险路恶敌前可能遇「深蓝之心」：选“搜寻园林”获得【深蓝之心】",
                    "（可选）「赞歌」选“亲吻猎人们的额头并取走遗物”获得【绝唱】（2个启示）",
                    "持有【深蓝之心】通过第5层，进入第6层",
                ],
                "key_relics": ["主教的研究", "深蓝之心", "绝唱"],
                "final": "ISW-DF 大群所向（伊莎玛拉，腐化之心）",
            },
            {
                "ending_id": "ro2_ending_4",
                "name": "如星空般深蓝",
                "label": "拓展Ⅱ",
                "unlock": "内容拓展Ⅱ后，波涛迭起及以上完成任一结局（需先完成「静谧时代」解锁「开端」）",
                "steps": [
                    "第3层末触发事件「开端」：选「“深蓝”？！」获得【深蓝回忆】（灯火-100，随机1名干员立即排异）",
                    "（可选）「深蓝仪式」选“不作任何改动”获得【深蓝之树】",
                    "第4-5层末险路恶敌前可能遇「升华」：三选一获得【决心】/【观望】/【犹疑】（灯火≥50 才能选观望，≥90 才能选决心）",
                    "持其一通过第5层，进入第6层",
                ],
                "key_relics": ["深蓝回忆", "深蓝之树", "决心", "观望", "犹疑"],
                "final": "ISW-DF 人之光辉（伊祖米克，生态泉源）",
            },
        ],
        "notes": [],
    },
    "sami": {
        "endings": [
            {
                "ending_id": "ro3_ending_1",
                "name": "越过群山",
                "label": "默认",
                "unlock": "无前置",
                "steps": [
                    "第5层倒数第2节点固定为命运所指「走过萨米的臂膀」（选【前进】与否不影响本结局）",
                    "不持有【无垠赠礼】【维度流质】时通过第5层险路恶敌即可",
                ],
                "key_relics": [],
                "final": "ISW-DF 巍峨银凇 / 深寒造像（12+）（“萨米的意志”）",
            },
            {
                "ending_id": "ro3_ending_2",
                "name": "直至冬夜降临",
                "label": "",
                "unlock": "达成过「越过群山」",
                "steps": [
                    "【失与得】节点选使坍缩值+4的选项，获得密文板【空无】与【伤痕】",
                    "抵达第5层倒数第3个节点前宣告组合【空无+伤痕】，把右侧节点变为命运所指，触发「木裂前兆」：选【搜寻营地】获得【树痕之盔】",
                    "「走过萨米的臂膀」改选【戴上头盔】（此步选【前进】则失去本结局）",
                    "不持有【无垠赠礼】【维度流质】通过第5层",
                ],
                "key_relics": ["树痕之盔"],
                "final": "ISW-DF 萨米之熵 / 虚无之偶（12+）（“木裂”埃克提尔尼尔）",
            },
            {
                "ending_id": "ro3_ending_3",
                "name": "自深处的一瞥",
                "label": "",
                "unlock": "达成过「直至冬夜降临」",
                "steps": [
                    "【先行一步】节点选“听取凛视的意见”，获得【路网】（此后普通/紧急作战出现额外敌人）",
                    "下一层凛视在通路末端画出【紧急作战→命运所指】序列；不走此路则本次探索永久失去本结局",
                    "命运所指事件「悲观者囚笼」：选“捧起祭坛上的花”获得【无垠赠礼】",
                    "持有【无垠赠礼】（不持维度流质）通过第5层，进入第6层【远见之构】",
                ],
                "key_relics": ["路网", "无垠赠礼"],
                "final": "ISW-DF 园丁 / 哨兵（12+）→ 无垠回荡克雷松",
            },
            {
                "ending_id": "ro3_ending_4",
                "name": "终始",
                "label": "拓展Ⅱ",
                "unlock": "内容拓展Ⅱ上线后完成任一结局",
                "steps": [
                    "获得【视界邀约】（诡意行商大概率以1源石锭出售）",
                    "不期而遇事件「变化」：选“出示视界邀约”进入战斗【黄沙幻境】，通关获得【维度流质】",
                    "持有【维度流质】通过第5层，进入隐藏层【永恒之尘】（第7层）",
                    "层内经「重构」三阶段修复与「探索者的一小步」事件推进至最终战",
                ],
                "key_relics": ["视界邀约", "维度流质"],
                "final": "ISW-DF 时光之沙 / 迈入永恒（12+）（历法之王 & 大地之鞭）",
            },
            {
                "ending_id": "ro3_ending_c",
                "name": "短暂光芒",
                "label": "深入调查",
                "unlock": "深入调查模式（非常规探索）",
                "steps": [
                    "「深入调查」模式专属结局，常规探索无法达成",
                    "在深入调查中通过最终的险路恶敌后固定达成",
                ],
                "key_relics": [],
                "final": "深入调查最终险路恶敌（与结局一共用 BOSS）",
            },
        ],
        "notes": [],
    },
    "sarkaz": {
        "endings": [
            {
                "ending_id": "ro4_ending_1",
                "name": "憧憬未来",
                "label": "默认",
                "unlock": "无前置",
                "steps": [
                    "第5层倒数第2节点固定为命运所指，事件「尾页」：选择不报出魔王的名讳",
                    "不持有【时与光】【阿纳萨羯磨】【无终之钥】时通过第5层即可",
                ],
                "key_relics": [],
                "final": "ISW-DF 紧急授课（持有滚动先祖时为思维矫正）",
            },
            {
                "ending_id": "ro4_ending_2",
                "name": "双王记",
                "label": "",
                "unlock": "达成过「憧憬未来」",
                "steps": [
                    "第2层命运所指「戴冠式」：选“两人为对方戴上冠冕”，获得遗愿【纯白花瓣】与【无字地契】",
                    "在【去伪存真】解读这两缕遗愿，获得【巴别塔誓言】（解读前两者会持续占用思维负荷）",
                    "（可选）「离去者的庭院」查看阿米娅纪念碑获得【十戒】，最终战中尊主残影生命减半",
                    "「尾页」选报出魔王名讳：第5层险路恶敌变为朝谒（或魂灵朝谒），不持三件结局藏品通过第5层",
                ],
                "key_relics": ["纯白花瓣", "无字地契", "巴别塔誓言", "十戒"],
                "final": "特雷西斯与特蕾西娅（黑冠形态）",
            },
            {
                "ending_id": "ro4_ending_3",
                "name": "天使之城",
                "label": "",
                "unlock": "达成过「憧憬未来」，且历史重构点亮【置换现实】",
                "steps": [
                    "【失与得】选【一个方盒】获得【先知长角】（与【一片莲瓣】互斥，选过莲瓣后此选项永久消失）",
                    "准备至少2缕构想＋1次节点刷新机会（通常共3缕）",
                    "刷新节点：受【先知长角】影响必出命运所指「待诉说的真相」，耗2缕构想进入特殊诡谲断章（2紧急作战→2命运所指→险路恶敌时光凯旋）",
                    "事件「扣响异音」中获得【时与光】（代价：年代更频繁，所有思绪负荷+1）",
                    "不持有【无终之钥】通过第6层【辉光天顶】",
                ],
                "key_relics": ["先知长角", "时与光"],
                "final": "时光凯旋（霜星“冬痕”与爱国者）→ 圣城",
            },
            {
                "ending_id": "ro4_ending_4",
                "name": "遁入阇那",
                "label": "拓展Ⅰ",
                "unlock": "内容拓展Ⅰ上线后完成任一结局；历史重构点亮【置换现实】",
                "steps": [
                    "【失与得】选【一片莲瓣】获得【片瓣】（获得时清空源石锭，与【一个方盒】互斥）",
                    "持有至少2缕构想时进入【狭路相逢】，耗2构想进入特殊2v2战斗「轻舟共渡」（仅有飞行单位）",
                    "击败固定阵容（托生莲座×2）后选择获得【阿纳萨羯磨】（代价：领袖与精英生命+50%）；失败可在后续狭路相逢耗2构想重试",
                    "不持有【无终之钥】通过第6层【逍遥兰若】",
                ],
                "key_relics": ["片瓣", "阿纳萨羯磨"],
                "final": "授法",
            },
            {
                "ending_id": "ro4_ending_5",
                "name": "无瑕之日",
                "label": "拓展Ⅱ",
                "unlock": "内容拓展Ⅱ上线后完成任一结局",
                "steps": [
                    "不期而遇事件「空无前兆」：选“试着接受它”获得【预言显像】；此后「终曲合声」现身战斗（生命跨局继承），击破后掉落遗愿【绝望】",
                    "（可选）在去伪存真中解读：绝望→拯救→停止（三档对应最终战强化程度）",
                    "「孤注一掷」事件投入思绪铸造钥匙，获得【无终之钥】与【终结的骨架/躯壳/实相】（按投入项决定档位）",
                    "持有【无终之钥】通过当前最终层，进入第Ω层【无终安息】（仅1个险路恶敌节点「不容拒绝」）",
                ],
                "key_relics": ["预言显像", "无终之钥"],
                "final": "不容拒绝（博卓卡斯替→特雷西斯→凯尔希&菈玛莲&歌利亚→“阿米娅”）",
            },
        ],
        "notes": ["表内另收录失败结局「无迹可寻」（特殊结算演出，不占常规结局编号）"],
    },
    "sui": {
        "endings": [
            {
                "ending_id": "ro5_ending_1",
                "name": "依律镇抚",
                "label": "默认",
                "unlock": "无前置",
                "steps": [
                    "第5层倒数第2节点固定为命运所指（祠堂口事件）：选直接行仪",
                    "不持有【云与漆】【无墨长卷】【“小磨唧”】时通过第5层即可",
                ],
                "key_relics": [],
                "final": "ISW-DF 破岁阵祀（与“易”对决）",
            },
            {
                "ending_id": "ro5_ending_2",
                "name": "长卷留痕",
                "label": "",
                "unlock": "达成过「依律镇抚」",
                "steps": [
                    "是非境（岁兽残识）的【抉择】节点可能遇事件「榆蝶」（多在距中心2格以外）：选【出钱】付30源石锭获得【隐蝶墨】",
                    "持有后每场战斗遭遇“岁躯”（生命跨战斗继承），将其打至50%生命以下使其逃跑，战斗结束自动获得【墨化残碑】",
                    "第5层祠堂口改选奉碑行仪，险路恶敌被替换",
                ],
                "key_relics": ["隐蝶墨", "墨化残碑"],
                "final": "ISW-DF 昔字如烟（“岁”）",
            },
            {
                "ending_id": "ro5_ending_3",
                "name": "黑白入玄",
                "label": "",
                "unlock": "达成过「长卷留痕」",
                "steps": [
                    "是非境第一个【杂疑】高概率遇事件「执棋」：已有5名以上伺烛客时选【掀翻棋盘】→【取走棋盘】，获得【忘生珍珑】（干员部署流失50%技力）",
                    "【指点迷津】（未持有无墨长卷时）选额外选项，进入特殊战斗「分明」，通关获得【云与漆】",
                    "持有【云与漆】通过第5层，进入隐藏层【明灭顶】；层内命运所指「收笔」通过后所有非伺烛客干员被禁用",
                    "不持有【“小磨唧”】通过第6层",
                ],
                "key_relics": ["忘生珍珑", "云与漆"],
                "final": "ISW-DF 谋岁者（“望”）",
            },
            {
                "ending_id": "ro5_ending_4",
                "name": "无中生有",
                "label": "拓展Ⅰ",
                "unlock": "内容拓展Ⅰ上线后完成任一可用结局",
                "steps": [
                    "进入岁兽残识，第一个【易与】以5目标生命购入【无封长盒】（负面：领袖/精英防御+30%、攻速+20）",
                    "未持有【云与漆】时【先行一步】出现“提请起盒”，令干员入卷（无法参战直至回归）；首次入卷获得【无墨长卷】",
                    "持有【无墨长卷】时第5层最终BOSS变化，通过后进入明灭顶见证【末狩】",
                    "层内误入奇境「古今交汇」获7烛火进入【今昔境】；命运所指「大狩维天」通过后入卷干员回归",
                    "不持有【“小磨唧”】通过第6层",
                ],
                "key_relics": ["无封长盒", "无墨长卷"],
                "final": "ISW-DF 天数将易（持墨化残碑时为往昔难忆）→ 末狩 → 定乾坤",
            },
            {
                "ending_id": "ro5_ending_5",
                "name": "落子无悔",
                "label": "拓展Ⅱ",
                "unlock": "内容拓展Ⅱ上线后完成任一结局",
                "steps": [
                    "岁兽残识【故肆】的【点睛】消耗5目标生命上限选【点墨生器】，获得【追忆仪】（失去全部源石锭与希望，此后获取减半）",
                    "【误入奇境】出现“凝视仪器”，进入 ISW-DF 录忆，通关获得【“小磨唧”】（费用自然回复-50%）",
                    "（可选）指点迷津【拆解岁护】获【不息】+9票券；再在是非境故肆获【不赦】",
                    "通过最终层后进入【来去处】（仅2节点），命运所指「谋定」决定最终战形态",
                ],
                "key_relics": ["追忆仪", "“小磨唧”"],
                "final": "ISW-DF 定乾坤 / 止变变体（依持有藏品决定，难度极高）",
            },
        ],
        "notes": [],
    },
    "black": {
        "endings": [
            {
                "ending_id": "ro6_ending_1",
                "name": "强制重启",
                "label": "默认",
                "unlock": "无前置",
                "steps": [
                    "常规路线：不持有【怦然信标】时通过第5层【卡德霍之颅】险路恶敌即可",
                ],
                "key_relics": [],
                "final": "险路恶敌 永无安宁 / 痛苦将息（追猎）（卡德霍，黑流之源）",
            },
            {
                "ending_id": "ro6_ending_2",
                "name": "维度重构",
                "label": "",
                "unlock": "达成过「强制重启」",
                "steps": [
                    "集齐两件【沙盘】：第2-4层「线人与线索」的“线人”选【我受雇于梅兰德】得沙盘α（部署费用+2）；或第1-3层诡意行商1源石锭购沙盘β（费用+3）",
                    "抵达第5层时地图刷新3个命运所指（2个“好奇心与死”、1个“窥视箱中”，外观无法分辨）；同持两沙盘时正确的被标记为<谜题与谜底>",
                    "「窥视箱中」选【保护证据，查缴那些机器！】，进入险路恶敌战斗",
                    "不持有【怦然信标】通过第5层",
                ],
                "key_relics": ["沙盘α", "沙盘β"],
                "final": "险路恶敌 混沌源阶理论（洛伦茨，扰动之谬＋源阶方）",
            },
            {
                "ending_id": "ro6_ending_3",
                "name": "纠缠，调和",
                "label": "",
                "unlock": "达成过「强制重启」",
                "steps": [
                    "第2-4层【先行一步】选【尝试探索树的源头】送出一名干员；下一层其归来带回2希望与【怦然信标】（我方生命-40%、敌方生命+20%）",
                    "（可选，用于削弱BOSS）第6层前经 <神明之殇>/<文明之烬>/<光明之末> 标记节点换取【击坠“神明”】【焚毁“文明”】【湮没“光明”】",
                    "持有【怦然信标】通过第5层，进入隐藏第6层【源流交汇处】",
                    "（可选）第6层命运所指「调谐仪式」提交上述藏品进一步削弱BOSS（【源私钥】可替代任一）",
                ],
                "key_relics": ["怦然信标", "击坠“神明”", "焚毁“文明”", "湮没“光明”"],
                "final": "第6层关卡 畸症（玻利瓦尔，症结之核）",
            },
        ],
        "notes": [],
    },
}

# ---------------------------------------------------------------------------
# 表内结局合并 / 索引 / 匹配
# ---------------------------------------------------------------------------
_CACHE_TTL = 6 * 3600.0
_cache: dict[str, Any] = {"ts": 0.0, "themes": {}}


def _merge_theme(
    theme_dir: str,
    table: dict[str, Any] | None,
) -> dict[str, Any]:
    """一期主题表 endings × 攻略条目 → 展示行 + 主题名。

    行字段：ending_id/name/desc/label/unlock/steps/key_relics(名)/final/
    note/has_guide；表有而攻略没有 → has_guide=False（显示「攻略整理中」），
    攻略有而表没有（表拉取失败）→ 用攻略兜底名。
    """
    guide = ENDING_GUIDES.get(theme_dir) or {}
    curated = guide.get("endings") or []
    table_id = THEME_TABLE_IDS.get(theme_dir, "")
    detail = {}
    theme_name = THEME_DIRS.get(theme_dir, theme_dir)
    if table:
        detail = (table.get("details") or {}).get(table_id) or {}
        topic = (table.get("topics") or {}).get(table_id) or {}
        theme_name = str(topic.get("name") or theme_name)
    table_endings = detail.get("endings") or {}
    items = detail.get("items") or {}

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in curated:
        eid = str(entry.get("ending_id") or "")
        seen.add(eid)
        t = table_endings.get(eid) or {}
        rows.append(
            {
                "ending_id": eid,
                "name": str(t.get("name") or entry.get("name") or eid),
                "desc": str(t.get("desc") or ""),
                "label": str(entry.get("label") or ""),
                "unlock": str(entry.get("unlock") or ""),
                "steps": [str(s) for s in (entry.get("steps") or [])],
                "key_relics": [
                    _relic_chip(name, items) for name in entry.get("key_relics") or []
                ],
                "final": str(entry.get("final") or ""),
                "note": str(entry.get("note") or ""),
                "has_guide": True,
            }
        )
    # 表内新增结局（攻略未收录）：名单仍展示，标注「攻略整理中」
    if isinstance(table_endings, dict):
        for eid, t in table_endings.items():
            if eid in seen or not isinstance(t, dict):
                continue
            rows.append(
                {
                    "ending_id": str(eid),
                    "name": str(t.get("name") or eid),
                    "desc": str(t.get("desc") or ""),
                    "label": "",
                    "unlock": "",
                    "steps": [],
                    "key_relics": [],
                    "final": "",
                    "note": "",
                    "has_guide": False,
                }
            )
    return {
        "theme_name": theme_name,
        "rows": rows,
        "notes": [str(n) for n in (guide.get("notes") or [])],
    }


def _relic_chip(name: str, items: dict[str, Any]) -> dict[str, str]:
    """藏品名 → {name, item_id}（item_id 供图标下载；表内查不到时仅显示名）。"""
    for item_id, item in items.items():
        if isinstance(item, dict) and str(item.get("name") or "") == name:
            return {"name": name, "item_id": str(item.get("iconId") or item_id)}
    return {"name": name, "item_id": ""}


async def load_all_endings() -> dict[str, dict[str, Any]]:
    """{期目录: {theme_name, rows, notes}}；表拉取失败时退化为攻略兜底数据。

    进程内 6h memo（表 ensure 自身有 7 天 TTL + single-flight，这里只省
    六期合并的重复计算）。
    """
    now = time.time()
    if _cache["themes"] and now - float(_cache["ts"]) < _CACHE_TTL:
        return _cache["themes"]
    table = await ensure_rogue_topic_table()
    themes = {
        theme_dir: _merge_theme(theme_dir, table) for theme_dir in THEME_TABLE_IDS
    }
    _cache["themes"] = themes
    _cache["ts"] = now
    return themes


async def load_theme_endings(theme_dir: str) -> dict[str, Any] | None:
    themes = await load_all_endings()
    return themes.get(theme_dir)


def find_theme(query: str, extra_aliases: dict[str, str] | None = None) -> str | None:
    """期名（用户别名 > 内建别名 > 显示名/目录名）→ 期目录。"""
    text = (query or "").strip()
    if not text:
        return None
    hit = (extra_aliases or {}).get(text.lower())
    if hit:
        return hit
    if text in THEME_ALIASES:
        return THEME_ALIASES[text]
    return resolve_theme_dir(text)


def _match_hits(query: str, themes: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """跨期按结局名匹配（精确 → 唯一前缀 → 唯一子串），命中带期目录。"""
    q = (query or "").strip()
    if not q:
        return []
    exact: list[dict[str, str]] = []
    prefix: list[dict[str, str]] = []
    contains: list[dict[str, str]] = []
    for theme_dir, data in themes.items():
        for row in data["rows"]:
            name = str(row.get("name") or "")
            if not name:
                continue
            hit = {"theme_dir": theme_dir, "name": name}
            if name == q:
                exact.append(hit)
            elif name.startswith(q):
                prefix.append(hit)
            elif q in name:
                contains.append(hit)
    for hits in (exact, prefix):
        if hits:
            return hits[:6]
    return contains[:6]


def _relic_hits(query: str, themes: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """关键藏品名反查结局（精确 → 唯一前缀 → 唯一子串）。"""
    q = (query or "").strip()
    if not q:
        return []
    exact: list[dict[str, str]] = []
    prefix: list[dict[str, str]] = []
    contains: list[dict[str, str]] = []
    for theme_dir, data in themes.items():
        for row in data["rows"]:
            for chip in row.get("key_relics") or []:
                name = str(chip.get("name") or "")
                if not name:
                    continue
                hit = {
                    "theme_dir": theme_dir,
                    "name": str(row.get("name") or ""),
                    "relic_name": name,
                }
                if name == q:
                    exact.append(hit)
                elif name.startswith(q):
                    prefix.append(hit)
                elif q in name:
                    contains.append(hit)
    for hits in (exact, prefix):
        if hits:
            return hits[:6]
    return contains[:6]


async def resolve_query(
    query: str, extra_aliases: dict[str, str] | None = None
) -> tuple[str, list[str], str] | str:
    """`[期名] | 结局名 | 关键藏品名` → (期目录, 结局名子集, 匹配方式) 或提示 str。

    匹配方式 theme/ending/relic（聚焦提示用）；子集为空列表时表示整期展示，
    藏品命中时子集为藏品对应的结局名。
    """
    text = (query or "").strip()
    themes = await load_all_endings()
    if not themes:
        return "结局数据不可用（主题表拉取失败），请稍后再试。"

    theme_dir = find_theme(text, extra_aliases)
    if theme_dir:
        return theme_dir, [], "theme"

    for matcher, via in ((_match_hits, "ending"), (_relic_hits, "relic")):
        hits = matcher(text, themes)
        if not hits:
            continue
        if len(hits) == 1:
            return hits[0]["theme_dir"], [hits[0]["name"]], via
        shown = "、".join(
            f"「{h['name']}」"
            + (f"←{h['relic_name']}" if h.get("relic_name") else "")
            + f"（{themes[h['theme_dir']]['theme_name']}）"
            for h in hits
        )
        return f"找到 {len(hits)} 个候选：{shown}（请用完整名）"
    known = "、".join(str(data["theme_name"]) for data in themes.values())
    return f"未找到「{text}」。可查期名（{known}）、结局名或关键藏品名（如：ark肉鸽结局 巴别塔誓言）。"


def theme_usage() -> str:
    """用法提示里的期名列表（旧 → 新）。"""
    names = "、".join(THEME_DIRS[dir_] for dir_ in THEME_TABLE_IDS)
    return (
        "用法：ark肉鸽结局 [期名/结局名/关键藏品名]（如 ark肉鸽结局 萨卡兹、"
        f"ark肉鸽结局 双王记、ark肉鸽结局 巴别塔誓言）。期名：{names}"
    )
