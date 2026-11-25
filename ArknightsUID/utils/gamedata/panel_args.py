"""面板与养成共用的干员条件语法。"""

from __future__ import annotations

import re
import unicodedata


# ark面板 条件解析的正则（模块级预编译）
_RE_PHASE = re.compile(r"^精([012])$")
_RE_LV = re.compile(r"^(?:[Ll][Vv]?(\d{1,3})|(\d{1,3})级|(满级))$")
_RE_POT = re.compile(r"^(?:潜(\d)|满潜)$")
# 技能序号三种写法：技能2 / 2技能 / 二技能（放宽 1-9 以准确给出越界提示）
_RE_SKILL = re.compile(r"^(?:技能([1-9])|([一二三四五六七八九1-9])技能)$")
# 专精档三种写法：专3 / 专精3 / 专三（中文数字）
_RE_MASTERY = re.compile(r"^专[精]?([1-9一二三四五六七八九])$")
_RE_SKILL_LV = re.compile(r"^([1-7])级$")
_RE_MODULE_TYPE = re.compile(r"^([XYABDxyabdＸＹＡＢＤｘｙａｂｄ])模组?$")
_RE_MODULE_LV = re.compile(r"^模组?([123])$")
_CN_NUM = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def parse_panel_query(text: str) -> dict:
    """`干员名 [精2] [Lv45/满级] [潜6/满潜] [技能2] [7级/专3] [X模/Y模/无模] [模3] [满好]`。

    「N级」1-7 视为技能等级（独立或在 技能K 后）；干员等级用 Lv45/
    45级（≥10）/满级。返回 {name, phase, level, potential, favor,
    skill_index, skill_level, mastery, module_type, no_module,
    module_level}，未指定的键为 None/False。
    """
    config: dict = {
        "name": "",
        "phase": None,
        "level": None,
        "potential": None,
        "favor": None,
        "skill_index": None,
        "skill_level": None,
        "mastery": None,
        "module_type": None,
        "no_module": False,
        "module_level": None,
    }
    tokens = (text or "").strip().split()
    if not tokens:
        return config
    kept: list[str] = []
    skill_context = False
    for token in tokens:
        if m := _RE_PHASE.match(token):
            config["phase"] = int(m.group(1))
        elif m := _RE_SKILL_LV.match(token):
            # 独立 N级（1-7）：视为技能等级——干员等级低于 10 实战无意义，
            # 指定低干员等级应写 Lv7，两位数以上（45级）在 _RE_LV 兜底
            config["skill_level"] = int(m.group(1))
        elif m := _RE_LV.match(token):
            if m.group(3) is not None:  # 满级
                config["level"] = -1
            else:
                lv = int(m.group(1) or m.group(2))
                if skill_context and lv <= 7:
                    config["skill_level"] = lv
                else:
                    config["level"] = lv
                    skill_context = False
        elif m := _RE_POT.match(token):
            config["potential"] = int(m.group(1)) if m.group(1) else -1  # -1=满潜
        elif m := _RE_SKILL.match(token):
            idx = m.group(1) or _CN_NUM.get(m.group(2)) or m.group(2)
            if idx:
                config["skill_index"] = int(idx)
                skill_context = True
        elif m := _RE_MASTERY.match(token):
            config["mastery"] = int(m.group(1)) if m.group(1).isdigit() else _CN_NUM[m.group(1)]
            skill_context = False
        elif m := _RE_MODULE_TYPE.match(token):
            # 全角 Ｘ/Ｙ 先 NFKC 归一半角再大写，否则 'Ｙ' != 'Y'
            # 永不命中模组类型，静默回退 X 模
            config["module_type"] = unicodedata.normalize("NFKC", m.group(1)).upper()
        elif _RE_MODULE_LV.match(token):
            config["module_level"] = int(_RE_MODULE_LV.match(token).group(1))
        elif token in ("无模", "无模组"):
            config["no_module"] = True
        elif token == "满好":
            config["favor"] = 200
        else:
            kept.append(token)
            skill_context = False
    config["name"] = " ".join(kept)
    return config
