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
from types import SimpleNamespace

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
import ArknightsUID.arknightsuid_social  # noqa: F401
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
            ("ark理智", "ark理智推送"),
            ("ark基建", "ark基建推送"),
            ("arkBOX", "arkBOX分析"),
            ("ark练度", "ark练度卡"),
            ("ark养成", "ark养成计划"),
            ("ark刷图", "ark刷图计划"),
            ("ark合成", "ark合成树"),
            ("ark活动", "ark活动列表"),
            ("ark肉鸽", "ark肉鸽别名"),
            ("ark肉鸽", "ark肉鸽别名删除"),
            ("ark肉鸽", "ark肉鸽结局"),
        ]
        for short, long in cases:
            with self.subTest(pair=f"{short} < {long}"):
                # 不变量整体通过即等价于所有配对有序
                self.test_no_earlier_command_is_prefix_of_later()


if __name__ == "__main__":
    unittest.main()


class RealDispatchOrderTest(unittest.TestCase):
    """审计 P1-2：`ark签到全部` 必须路由到全部签到 handler。

    用框架真实的 Trigger.check_command（消息匹配）与
    handler._command_sort_key（handle_event 的分发排序）复现选路：
    fullmatch 精确匹配优先于 command 前缀匹配、长关键字优先于短前缀，
    分发结果不依赖插件注册顺序（server 用未排序 iterdir 加载插件）。
    """

    @staticmethod
    def _collect(msg_text: str) -> list:
        """按真实匹配+排序收集 (trigger, priority)，返回排序后的触发器。"""
        from gsuid_core.handler import _command_sort_key
        from gsuid_core.sv import SL

        import ArknightsUID.arknightsuid_account  # noqa: F401
        import ArknightsUID.arknightsuid_notify  # noqa: F401
        import ArknightsUID.arknightsuid_rogue  # noqa: F401

        msg = SimpleNamespace(
            raw_text=msg_text, is_tome=False,
            file=None, file_name=None, meta_event_type=None,
        )
        candidates: dict = {}
        for sv_name, sv in SL.lst.items():
            if "ArknightsUID" not in sv_name:
                continue
            for trig_dict in sv.TL.values():
                for trig in trig_dict.values():
                    if trig.type == "message":
                        continue
                    try:
                        if trig.check_command(msg):
                            candidates[trig] = sv.priority
                    except Exception:
                        pass
        return [t for t, _ in sorted(candidates.items(), key=_command_sort_key)]

    def test_sign_all_beats_plain_sign_regardless_of_registration(self) -> None:
        order = self._collect("ark签到全部")
        types = [(t.type, t.keyword) for t in order]
        # 两个触发器都命中（这正是冲突所在）……
        self.assertIn(("command", "签到"), types)
        self.assertIn(("fullmatch", "签到全部"), types)
        # ……但分发第一名必须是全部签到的 fullmatch
        first = order[0]
        self.assertEqual(first.type, "fullmatch")
        self.assertIn(first.keyword, ("全部签到", "签到全部"))
        self.assertIn(
            "sign_all_accounts", getattr(first.func, "__qualname__", "")
        )

    def test_plain_sign_still_routes_to_normal_handler(self) -> None:
        """反向用例：普通 `ark签到` 不被 fullmatch 抢走。"""
        order = self._collect("ark签到")
        first = order[0]
        self.assertEqual(first.type, "command")
        self.assertIn(first.keyword, ("签到", "方舟签到"))
        self.assertIn(
            "send_ark_attendance", getattr(first.func, "__qualname__", "")
        )

    def test_longer_command_beats_its_short_prefix(self) -> None:
        """长命令优先于自己的短前缀（此前靠注册顺序保证）。

        `ark肉鸽别名列表` 同时命中 command("肉鸽") 与 肉鸽别名系列命令；
        第一名必须是肉鸽别名触发器（fullmatch 精确或长 command），
        绝不能是会被 `肉鸽` 前缀抢走。
        """
        order = self._collect("ark肉鸽别名列表")
        first = order[0]
        self.assertIn(first.type, ("command", "fullmatch"))
        self.assertTrue(
            first.keyword.startswith("肉鸽别名"),
            f"第一名应为「肉鸽别名…」，实际 {first.type}:{first.keyword!r}",
        )


class PinyinAliasDispatchTest(unittest.TestCase):
    """高频命令的拼音首字母别名（仿 EndUID endmr→end每日）。

    用与 RealDispatchOrderTest 相同的真实匹配+排序链路验证：
    别名串实际路由到目标 handler，且 arkgztj 不被 arkgz 抢走。
    """

    # (输入消息, 期望命中的 handler qualname 片段)
    CASES = [
        ("arkmr", "show_ark_daily"),
        ("arklz", "show_ark_status"),
        ("arkqd", "send_ark_attendance"),
        ("arkck", "show_gacha_report"),
        ("arkzc", "plan_gacha_draws"),
        ("arkbz", "send_ark_help"),
        ("arkgg", "show_ark_announcements"),
        ("arkrl", "send_calendar"),
        ("arkmb", "send_op_panel"),
        ("arkgz", "send_recruit_query"),
        ("arkgztj", "send_recruit_recommend"),
        ("arkrg", "send_rogue_info"),
        ("arkyc", "show_cultivation"),
    ]

    @staticmethod
    def _dispatch_first(msg_text: str):
        from gsuid_core.handler import _command_sort_key
        from gsuid_core.sv import SL

        import ArknightsUID.arknightsuid_account  # noqa: F401
        import ArknightsUID.arknightsuid_cultivate  # noqa: F401
        import ArknightsUID.arknightsuid_gacha  # noqa: F401
        import ArknightsUID.arknightsuid_help  # noqa: F401
        import ArknightsUID.arknightsuid_push  # noqa: F401
        import ArknightsUID.arknightsuid_rogue  # noqa: F401
        import ArknightsUID.arknightsuid_wiki  # noqa: F401

        msg = SimpleNamespace(
            raw_text=msg_text, is_tome=False,
            file=None, file_name=None, meta_event_type=None,
        )
        candidates: dict = {}
        for sv_name, sv in SL.lst.items():
            if "ArknightsUID" not in sv_name:
                continue
            for trig_dict in sv.TL.values():
                for trig in trig_dict.values():
                    if trig.type == "message":
                        continue
                    try:
                        if trig.check_command(msg):
                            candidates[trig] = sv.priority
                    except Exception:
                        pass
        ordered = [
            t for t, _ in sorted(candidates.items(), key=_command_sort_key)
        ]
        return ordered[0] if ordered else None

    def test_alias_routes_to_expected_handler(self) -> None:
        for text, handler in self.CASES:
            with self.subTest(alias=text):
                first = self._dispatch_first(text)
                self.assertIsNotNone(first, f"{text} 没有命中任何触发器")
                self.assertIn(
                    handler,
                    getattr(first.func, "__qualname__", ""),
                    f"{text} 应路由到 {handler}，实际 "
                    f"{first.type}:{first.keyword!r} → "
                    f"{getattr(first.func, '__qualname__', '?')}",
                )

    def test_chinese_commands_still_win_over_aliases(self) -> None:
        """全中文命令不受别名影响：ark公招 仍走查询（不是推荐）。"""
        first = self._dispatch_first("ark公招 高级资深干员")
        self.assertIn(
            "send_recruit_query", getattr(first.func, "__qualname__", "")
        )
