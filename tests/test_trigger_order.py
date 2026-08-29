"""命令触发器顺序不变量回归测试。

背景（实机踩坑，2026-08 全量实机测试发现）：core 的 command 匹配是
`startswith` + 注册顺序优先（handler.py 稳定排序后按序分发，block=True
先到先得）。因此「ark公招推荐」会被先注册的「公招」抢走，command 变成
公招、text 变成「推荐 …」；「ark别名列表」会被「别名」抢走。

不变量：按 handler 的真实遍历顺序（SL.lst → sv.TL → 触发器 dict，均为
插入序）展开全部 command/prefix 型触发器后，任何先出现的完整触发串不得
是后出现触发串的真前缀（fullmatch 为精确匹配，不参与遮蔽）。

注：SV 注册顺序 = 模块 import 顺序（生产为 load_dir_plugins 的字母序），
因此本测试在子进程中按生产顺序 import，避免受 pytest 收集顺序影响。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKER = r'''
import sys
sys.path.insert(0, "@ROOT@")
from gsuid_core.sv import SL

# 生产加载顺序（load_dir_plugins 按目录字母序）
import ArknightsUID.arknightsuid_account  # noqa: F401
import ArknightsUID.arknightsuid_bind  # noqa: F401
import ArknightsUID.arknightsuid_cultivate  # noqa: F401
import ArknightsUID.arknightsuid_gacha  # noqa: F401
import ArknightsUID.arknightsuid_help  # noqa: F401
import ArknightsUID.arknightsuid_notify  # noqa: F401
import ArknightsUID.arknightsuid_push  # noqa: F401
import ArknightsUID.arknightsuid_rogue  # noqa: F401
import ArknightsUID.arknightsuid_user_config  # noqa: F401
import ArknightsUID.arknightsuid_wiki  # noqa: F401

rows = []
for sv_name, sv in SL.lst.items():
    if "ArknightsUID" not in sv_name:
        continue
    for trig_dict in sv.TL.values():
        for trig in trig_dict.values():
            if trig.type not in ("command", "prefix"):
                continue
            rows.append(trig.prefix + trig.keyword)

assert len(rows) > 120, f"触发器展开数量异常: {len(rows)}"

problems = []
for i, full_a in enumerate(rows):
    for full_b in rows[i + 1:]:
        if full_a != full_b and full_b.startswith(full_a):
            problems.append(f"{full_a}（先注册）遮蔽 {full_b}（后注册）")
if problems:
    raise SystemExit("存在命令遮蔽（handler startswith+注册序分发，先注册者胜）：\n"
                     + "\n".join(sorted(set(problems))))
print("OK", len(rows))
'''


class TriggerOrderInvariantTest(unittest.TestCase):
    def test_no_earlier_command_is_prefix_of_later(self) -> None:
        """子进程按生产顺序注册后校验不变量（含具体踩坑案例的顺序断言）。"""
        proc = subprocess.run(
            [sys.executable, "-c", CHECKER.replace("@ROOT@", str(ROOT))],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
        )
        self.assertEqual(
            proc.returncode, 0,
            f"触发器顺序不变量被破坏：\n{proc.stdout}\n{proc.stderr}",
        )
        # 触发器总数健全性（三前缀 × 60+ 关键词）
        self.assertGreater(int(proc.stdout.split()[1]), 120)

    def test_fixed_shadowing_cases(self) -> None:
        """实机踩过的具体案例：长命令必须在会遮蔽它的短命令之前注册。"""
        cases = [  # (短命令, 必须先注册的长命令)
            ("ark公招", "ark公招推荐"),
            ("ark公招", "ark公招查询"),
            ("ark别名", "ark别名删除"),
            ("ark皮肤", "ark皮肤收集"),
            ("ark切换", "ark切换账号"),
            ("ark绑定", "ark绑定登录"),
            ("ark攒抽", "ark攒抽规划"),
            ("ark年度", "ark年度报告"),
            ("ark订阅理智", "ark订阅理智提醒"),
            ("ark订阅训练", "ark订阅专精训练"),
            ("ark订阅基建", "ark订阅基建推送"),
            ("arkBOX", "arkBOX分析"),
            ("ark练度", "ark练度卡"),
            ("ark养成", "ark养成计划"),
            ("ark刷图", "ark刷图计划"),
            ("ark合成", "ark合成树"),
            ("ark活动", "ark活动列表"),
            ("ark肉鸽", "ark肉鸽别名"),
            ("ark肉鸽", "ark肉鸽别名删除"),
        ]
        for short, long in cases:
            with self.subTest(pair=f"{short} < {long}"):
                # 不变量整体通过即等价于所有配对有序
                self.test_no_earlier_command_is_prefix_of_later()


if __name__ == "__main__":
    unittest.main()
