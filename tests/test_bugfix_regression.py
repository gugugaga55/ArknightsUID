"""2026-08 全量 bug 审查修复的回归测试。

每一项对应一次已确认（真实数据/脚本复现）的缺陷，防止回退：
- reply_safety 中文邻接脱敏失效（\b 在汉字旁不成立）
- 语音台词正则嵌套 {{DrName}} 跨条错位
- player service 非法 uid 抛 TypeError
- 抽卡增量同步停止阈值跨类别污染
- 理智推送发送失败不回滚状态 → 周期通知永久丢失
- 肉鸽难度修正丢算 add 模式 / res 未钳位
- find_stage 同期重名关卡无变体信息
- 公告时间戳宿主时区渲染差一天
- 模组类型大小写（WAH-Y）与全角（ｙ）判定
- account_switch 落盘失败仍报成功
- refresh 锁 prune 回收有 waiter 的锁
- remove_ark_login 删不存在的 UID 报成功
- 活动 push 全失败仍标 seen / 超量条目全标
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class ReplySafetyCJKTest(unittest.TestCase):
    def test_cjk_adjacent_assignment_redacted(self) -> None:
        from ArknightsUID.utils.reply_safety import safe_external_message

        # 字段名紧贴中文（无空格）也必须脱敏
        out = safe_external_message("获取token=xyz失败")
        self.assertNotIn("xyz", out)
        out = safe_external_message("获取cookie=ABC123失败")
        self.assertNotIn("ABC123", out)

    def test_word_boundary_still_respected(self) -> None:
        from ArknightsUID.utils.reply_safety import safe_external_message

        # 字段名是更长 ASCII 词的一部分时不算字段（保持原 \b 意图）
        out = safe_external_message("mytoken=abc")
        self.assertNotIn("已隐藏", out)
        self.assertIn("abc", out)

    def test_space_adjacent_still_redacted(self) -> None:
        from ArknightsUID.utils.reply_safety import safe_external_message

        out = safe_external_message("登录失败 cookie=ABC")
        self.assertNotIn("ABC", out)


class VoiceLinesParseTest(unittest.TestCase):
    WIKI = (
        "|标题8=信赖提升后交谈2\n"
        "|台词8={{VoiceData/word|中文|{{DrName}}，该休息了。}}"
        "{{VoiceData/word|英文|Doctor, take a break.}}\n"
        "|标题9=信赖提升后交谈3\n"
        "|台词9={{VoiceData/word|中文|博士，我们的脚下，是一条漫长的道路。}}"
        "{{VoiceData/word|英文|A long road.}}\n"
    )

    def test_nested_template_no_misalignment(self) -> None:
        from ArknightsUID.utils.prts.client import parse_voice_lines

        lines = parse_voice_lines(self.WIKI)
        self.assertEqual(len(lines), 2, "嵌套模板不得吞条目")
        self.assertEqual(lines[0]["title"], "信赖提升后交谈2")
        self.assertIn("博士", lines[0]["text"], "DrName 应展开为 博士")
        self.assertEqual(lines[1]["title"], "信赖提升后交谈3")
        self.assertIn("漫长的道路", lines[1]["text"], "台词9 不得错配给台词8")


class PlayerServiceInvalidUidTest(unittest.TestCase):
    def test_cache_age_invalid_uid_returns_none(self) -> None:
        from ArknightsUID.utils.player import service

        self.assertIsNone(service._cache_age_seconds("abc123"))

    def test_read_cached_invalid_uid_returns_none(self) -> None:
        from ArknightsUID.utils.player import service

        async def run():
            return await service.read_cached_player_info("abc123")

        self.assertIsNone(asyncio.run(run()))


class GachaCategoryStopTest(unittest.TestCase):
    def test_per_category_threshold(self) -> None:
        """限定池最新记录（t=20）不得截断常驻池的翻页。"""
        from ArknightsUID.utils.hypergryph import gacha

        local = [
            {"category": "normal", "gacha_ts": 10},
            {"category": "limited", "gacha_ts": 20},
        ]
        pages = {
            # 常驻池两页：t19..16（全部 < 20），旧实现第一页就 stop 丢 t16
            ("normal", None): (
                [
                    {"gachaTs": 19, "pos": 1},
                    {"gachaTs": 18, "pos": 2},
                    {"gachaTs": 17, "pos": 3},
                ],
                True,
            ),
            ("normal", 17): (
                [
                    {"gachaTs": 16, "pos": 4},
                    {"gachaTs": 9, "pos": 5},
                ],
                False,
            ),
            ("limited", None): ([{"gachaTs": 20, "pos": 1}], False),
        }

        async def fake_login(cred):
            return {"auth": True}

        async def fake_categories(cred, auth):
            return [{"id": "normal"}, {"id": "limited"}]

        async def fake_page(cred, auth, category, gacha_ts=None, pos=None):
            items, has_more = pages[(category, gacha_ts)]
            return {"list": items, "hasMore": has_more}

        with mock.patch.object(gacha, "load_local_logs", return_value=local), \
             mock.patch.object(gacha, "save_local_logs") as save, \
             mock.patch("ArknightsUID.utils.hypergryph.client.login_gacha_chain", fake_login), \
             mock.patch("ArknightsUID.utils.hypergryph.client.get_gacha_categories", fake_categories), \
             mock.patch("ArknightsUID.utils.hypergryph.client.get_gacha_history_page", fake_page):
            merged, err = asyncio.run(gacha.sync_gacha_logs(SimpleNamespace(uid="12345678")))

        self.assertEqual(err, "")
        ts = {r["gacha_ts"] for r in merged if r["category"] == "normal"}
        self.assertIn(16, ts, "常驻池第二页新增（早于限定池最新）不得丢失")
        self.assertIn(17, ts)
        save.assert_called_once()


class SanityPushRollbackTest(unittest.TestCase):
    def _fake_subscribe(self, fail: bool):
        async def send(message):
            if fail:
                raise RuntimeError("bot offline")
            return True

        return SimpleNamespace(
            bot_id="bot", user_id="u1", uid="100000001", send=send
        )

    def _run(self, prev_state, ap, ap_max, fail):
        from ArknightsUID import arknightsuid_notify as notify

        state = dict(prev_state)
        saved: dict = {}

        def fake_load():
            return state

        def fake_save(new, merge_from=None, **kwargs):
            # 兑现 merge_from 契约：回调在锁内返回合并后的完整状态
            merged = merge_from() if merge_from is not None else new
            saved.update(merged)

        data = SimpleNamespace(name="博士", ap_max=ap_max)

        async def run():
            await notify.check_sanity_push()

        with mock.patch.object(notify, "gs_subscribe") as sub, \
             mock.patch.object(notify, "resolve_credential", return_value=SimpleNamespace(uid="100000001")), \
             mock.patch.object(notify, "fetch_cached_player_info", return_value={"code": 0}), \
             mock.patch.object(notify, "parse_player_info", return_value=data), \
             mock.patch.object(notify, "effective_ap", return_value=ap), \
             mock.patch.object(notify, "load_push_state", fake_load), \
             mock.patch.object(notify, "save_push_state", fake_save):
            sub.get_subscribe = mock.AsyncMock(return_value=[self._fake_subscribe(fail)])
            asyncio.run(run())
        return saved

    KEY = "bot|u1|100000001"

    def test_send_fail_keeps_notified_false(self) -> None:
        saved = self._run({}, ap=130, ap_max=130, fail=True)
        # 状态本就未变化（False→False）：无落盘；关键是绝不能落 True
        self.assertNotEqual(
            saved.get(self.KEY, {}).get("notified"), True,
            "回满当轮发送失败：notified 不得落 True（否则永久漏发）",
        )

    def test_send_ok_sets_notified(self) -> None:
        saved = self._run({}, ap=130, ap_max=130, fail=False)
        self.assertTrue(saved[self.KEY]["notified"])

    def test_warn_round_resets_notified(self) -> None:
        # 满理智后只花 1 点（仍在 85% 预警带内）：预警轮必须把 notified
        # 复位为 False，否则随后回满会被判"已通知过"而漏发
        saved = self._run(
            {self.KEY: {"notified": True, "warned": False}},
            ap=99, ap_max=100, fail=False,
        )
        self.assertFalse(saved[self.KEY]["notified"])
        self.assertTrue(saved[self.KEY]["warned"])


class RogueModsTest(unittest.TestCase):
    def test_add_final_mode(self) -> None:
        from ArknightsUID.utils.gamedata.rogue_enemy import _mods_apply

        mods = [
            {"key": "res", "value": 10, "mode": "add"},
            {"key": "res", "value": 10, "mode": "add"},
        ]
        # 水月 N12：两条 res+10 平加累积
        self.assertEqual(_mods_apply(0.0, mods), 20.0)

    def test_mixed_initial_mul_and_add(self) -> None:
        from ArknightsUID.utils.gamedata.rogue_enemy import _mods_apply

        mods = [
            {"key": "hp", "value": 0.5, "mode": "mul", "order": "initial"},
            {"key": "hp", "value": 2.0, "mode": "mul"},  # final 连乘
            {"key": "hp", "value": 20, "mode": "add", "order": "initial"},
            {"key": "hp", "value": 100, "mode": "add"},
        ]
        # ((100 + 20) * (1 + 0.5) + 100) * 2.0 = 560
        self.assertEqual(_mods_apply(100.0, mods), 560.0)

    def test_stats_res_clamped(self) -> None:
        from ArknightsUID.utils.gamedata.rogue_enemy import _stats_with_mods

        base_block = {"hp": 1000, "atk": 100, "def": 50, "res": 95}
        mods = [{"key": "res", "value": 10, "mode": "add"}]
        stats = _stats_with_mods(base_block, mods, difficulty=12)
        self.assertEqual(stats["res"], 100, "res 须钳制到 100")


class FindStageVariantsTest(unittest.TestCase):
    def test_variants_counted(self) -> None:
        from ArknightsUID.utils.gamedata import rogue_enemy

        meta = {
            "level_rogue5_sv-10": {"name": "迷惘", "code": "SV-10"},
            "level_rogue5_sv-10-b": {"name": "迷惘", "code": "SV-10"},
            "level_rogue5_other": {"name": "别关", "code": "SV-11"},
        }

        async def fake_load(theme_dir):
            return meta if theme_dir == "sui" else {}

        with mock.patch.object(rogue_enemy, "_load_stage_meta", fake_load):
            stage = asyncio.run(rogue_enemy.find_stage("迷惘"))
        self.assertIsNotNone(stage)
        self.assertEqual(stage["variants"], 2)
        self.assertEqual(stage["level_id"], "level_rogue5_sv-10")


class AnnTimezoneTest(unittest.TestCase):
    def test_format_ts_beijing_regardless_of_host_tz(self) -> None:
        import calendar
        import os
        import time

        from ArknightsUID.utils.skland import web

        # 北京 2026-08-31 07:00 = UTC 2026-08-30 23:00：UTC 宿主旧实现
        # 会渲染成 08-30。timegm 给出与本地时区无关的绝对 epoch。
        ts = calendar.timegm((2026, 8, 30, 23, 0, 0, 0, 0, 0))
        with mock.patch.dict(os.environ, {"TZ": "UTC"}):
            time.tzset()
            try:
                self.assertEqual(web._format_ts(ts), "2026-08-31")
            finally:
                del os.environ["TZ"]
                time.tzset()


class ModuleTypeTest(unittest.TestCase):
    def test_fullwidth_y_normalized(self) -> None:
        from ArknightsUID.arknightsuid_wiki import _parse_panel_query

        for token in ("Y模组", "y模", "Ｙ模组", "ｙ模组"):
            cfg = _parse_panel_query(f"银灰 {token}")
            self.assertEqual(cfg["module_type"], "Y", f"token={token!r}")

    def test_lowercase_typeicon(self) -> None:
        from ArknightsUID.utils.gamedata.oppanel import find_module

        choices = [
            {"id": "a", "name": "甲", "type": "X"},
            {"id": "b", "name": "乙", "type": "Y"},
        ]
        picked = find_module(choices, "Y")
        self.assertEqual(picked["type"], "Y")


class AccountSwitchPersistTest(unittest.TestCase):
    def test_set_default_uid_save_failure_returns_false(self) -> None:
        from ArknightsUID.utils import account_switch

        # /dev/null 下无法 mkdir，写盘必失败
        bad = Path("/dev/null/ark-switch-test/x.json")
        with mock.patch.object(account_switch, "_STATE_PATH", bad), \
             mock.patch.object(account_switch, "_load", return_value={}):
            self.assertFalse(account_switch.set_default_uid("bot", "u", "123"))


class RefreshLockPruneTest(unittest.TestCase):
    def test_lock_with_waiters_not_recycled(self) -> None:
        from ArknightsUID.utils.skland import client

        async def scenario():
            lock_b = asyncio.Lock()
            # 模拟「已释放但 waiter 未恢复」窗口：_locked=False + 有 waiter
            fut = asyncio.get_running_loop().create_future()
            lock_b._waiters = [fut]  # CPython 实现细节
            client._refresh_locks["credB"] = lock_b
            client._prune_token_state(keep="credA")
            return "credB" in client._refresh_locks

        kept = asyncio.run(scenario())
        self.assertTrue(kept, "有排队的锁不得被 prune 回收")

    def test_idle_lock_recycled(self) -> None:
        from ArknightsUID.utils.skland import client

        async def scenario():
            lock = asyncio.Lock()  # 空闲且无 waiter
            client._refresh_locks["credIdle"] = lock
            client._prune_token_state(keep="credA")
            return "credIdle" in client._refresh_locks

        kept = asyncio.run(scenario())
        self.assertFalse(kept)


class RemoveLoginMissingTest(unittest.TestCase):
    def test_remove_missing_uid_returns_zero(self) -> None:
        from ArknightsUID.utils.skland import store

        async def run():
            # uid 分支的真实路径是 delete_by_user（select_rows 不经过）：
            # mock 其返回 0（uid 不存在删除 0 行）
            with mock.patch.object(
                store.ArkUser, "delete_by_user", new=mock.AsyncMock(return_value=0)
            ), mock.patch.object(
                store.ArkBind, "delete_uid", new=mock.AsyncMock(return_value=0)
            ) as bind_del:
                count = await store.remove_ark_login("u", "bot", uid="999")
            bind_del.assert_not_awaited()
            return count

        self.assertEqual(asyncio.run(run()), 0)


class DailyCardDivZeroTest(unittest.TestCase):
    def test_zero_total_renders_without_crash(self) -> None:
        """weekly/campaign total=0（字段缺失）不得让整卡 ZeroDivisionError。"""
        from ArknightsUID.utils.player.context import build_daily_context
        from ArknightsUID.utils.player.parse import PlayerData
        from ArknightsUID.utils.render import ark_templates

        d = PlayerData(
            name="刀客塔", daily_done=2, daily_total=3,
            weekly_total=0, campaign_total=0,
        )
        ctx = build_daily_context(d, "v0")
        self.assertIsNotNone(ctx)
        html = ark_templates.get_template("ark_daily.html").render(
            font_css_url="x", **ctx
        )
        self.assertIn("DAILY", html)


class AnnualPityTextTest(unittest.TestCase):
    def test_worst_pity_counts_inclusive(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _annual_report

        records = [
            {"category": "normal", "gacha_ts": 1700000000, "rarity": 6,
             "name": "甲", "is_new": True},
            {"category": "normal", "gacha_ts": 1700000100, "rarity": 4,
             "name": "乙", "is_new": False},
            {"category": "normal", "gacha_ts": 1700000200, "rarity": 6,
             "name": "丙", "is_new": False},
        ]
        report = _annual_report(records, "2023")
        self.assertIsNotNone(report)
        # 丙第 2 抽出货（此前文案"前垫了 2 抽"多算 1）
        self.assertIn("丙 第 2 抽才出货", report)
        self.assertNotIn("前垫了", report)


class TrainingIdleStateTest(unittest.TestCase):
    def test_idle_sentinel_renders_idle(self) -> None:
        """remaining=-1（空闲哨兵）+ trainee 残留指派 → 空闲，不是受训中。"""
        import asyncio as _aio

        from ArknightsUID.utils.player import context as pctx

        building = SimpleNamespace(
            available=True,
            training_trainee_id="char_1035_wisdel",
            training_trainee="维什戴尔",
            training_trainer="假日威龙陈",
            training_trainer_id="char_1013_chen2",
            training_target_skill=-1,
            training_speed=0.0,
        )
        data = SimpleNamespace(name="博士", uid="123", chars=[])
        with mock.patch.object(pctx, "training_remaining_secs", return_value=-1), \
             mock.patch.object(pctx, "training_progress_pct", return_value=None), \
             mock.patch.object(pctx, "format_training_remaining", return_value="未知"), \
             mock.patch.object(pctx, "_mastery_target", return_value=(None, None, None)), \
             mock.patch("ArknightsUID.utils.gamedata.charinfo.load_skill_names",
                        new=mock.AsyncMock(return_value={})), \
             mock.patch.object(pctx, "char_portrait_b64", new=mock.AsyncMock(return_value="")), \
             mock.patch.object(pctx, "char_avatar_b64", new=mock.AsyncMock(return_value="")):
            ctx = _aio.run(pctx.build_training_context(data, building, "v0"))
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["state"], "空闲")
        self.assertEqual(ctx["state_en"], "IDLE")
        self.assertEqual(ctx["speed"], 1.0)


class ActivityPushSeenTest(unittest.TestCase):
    def _run(self, fail: bool, fresh_count: int):
        from ArknightsUID import arknightsuid_push as push

        entries = {f"k{i}": [{"kind": "活动", "title": f"T{i}", "detail": ""}]
                   for i in range(fresh_count)}
        state: dict = {}
        sends: list[int] = []

        async def fake_collect():
            return entries

        def fake_send_state(new):
            state.update(new)

        sub = SimpleNamespace(
            group_id="g1",
            send=mock.AsyncMock(side_effect=RuntimeError("offline") if fail else None),
        )

        async def run():
            await push.check_ark_activities()

        with mock.patch.object(push, "_push_enabled", return_value=True), \
             mock.patch.object(push, "gs_subscribe") as gs, \
             mock.patch.object(push, "collect_current_entries", fake_collect), \
             mock.patch.object(push, "load_push_state", return_value={"seen": ["old"]}), \
             mock.patch.object(push, "save_push_state", fake_send_state):
            # 按任务名区分：合并订阅返回 [sub]，公告专项订阅返回空
            # （否则公告分支会发起真实 get_ark_ann_list 网络请求）
            gs.get_subscribe = mock.AsyncMock(
                side_effect=lambda task_name, **kw: (
                    [sub] if task_name == push.TASK_NAME else []
                )
            )
            asyncio.run(run())
        if not fail:
            sends.append(sub.send.await_count)
        return state, sub.send.await_count

    def test_all_fail_keeps_seen(self) -> None:
        state, _ = self._run(fail=True, fresh_count=3)
        self.assertEqual(
            state, {},
            "全部投递失败：不得调用落盘（下轮整批重试）",
        )

    def test_overflow_marks_only_pushed(self) -> None:
        state, calls = self._run(fail=False, fresh_count=10)
        self.assertEqual(calls, 1)
        self.assertEqual(len(state["seen"]), 1 + 8,
                         "只标实际推送的前 8 条 + 旧 seen")


if __name__ == "__main__":
    unittest.main()


class PushPartialDeliveryTest(unittest.IsolatedAsyncioTestCase):
    """审计 #9：部分会话失败不得因全局 seen 推进而永久漏发。

    A 成功 B 失败 → seen 不推进、delivered 记 A；下轮只补发 B；
    B 也成功后 seen 才推进，且 A 不重复收。
    """

    def _run_round(self, tmp_path, datas, entries):
        import ArknightsUID.arknightsuid_push as push

        with mock.patch.object(push, "collect_current_entries",
                               new=mock.AsyncMock(return_value=entries)), \
             mock.patch.object(push, "load_push_state",
                               new=lambda: json.loads(
                                   tmp_path.read_text(encoding="utf-8"))
                               if tmp_path.exists() else {}), \
             mock.patch.object(push, "save_push_state",
                               new=lambda state: tmp_path.write_text(
                                   json.dumps(state, ensure_ascii=False),
                                   encoding="utf-8")), \
             mock.patch.object(push, "gs_subscribe") as sub_mod, \
             mock.patch.object(push.asyncio, "sleep", new=mock.AsyncMock()):

            async def get_subscribe(task_name, *args, **kwargs):
                # 公告专项订阅为空：只跑活动链，不打真实公告接口
                return datas if task_name == push.TASK_NAME else []

            sub_mod.get_subscribe = get_subscribe
            asyncio.run(push.check_ark_activities())
        return json.loads(tmp_path.read_text(encoding="utf-8")) \
            if tmp_path.exists() else {}

    @staticmethod
    def _sub(group: str, fail: bool):
        async def send(message):
            if fail:
                raise RuntimeError("down")
        return SimpleNamespace(group_id=group, uid=None, send=send)

    def test_partial_success_then_retry_only_failed_session(self):
        import tempfile
        from pathlib import Path

        entries = {k: [{"kind": "活动", "title": f"T{k}", "detail": ""}]
                   for k in ("k1",)}
        a_sent: list[str] = []
        b_sent: list[str] = []

        async def send_a(message):
            a_sent.append(str(message))

        sub_a = SimpleNamespace(group_id="gA", uid=None, send=send_a)
        sub_b = self._sub("gB", fail=True)

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "s.json"
            state_path.write_text(json.dumps({"seen": ["old"]}), encoding="utf-8")
            state = self._run_round(state_path, [sub_a, sub_b], entries)
            # A 已收、B 失败：seen 不推进；delivered 记录 A 的会话
            self.assertEqual(state.get("seen"), ["old"])
            self.assertEqual(state.get("delivered", {}).get("k1"), ["group:gA"])
            self.assertEqual(len(a_sent), 1)

            # 第二轮：B 恢复成功 → 只给 B 补发（A 不重复），seen 推进
            async def send_b2(message):
                b_sent.append(str(message))

            sub_b2 = SimpleNamespace(group_id="gB", uid=None, send=send_b2)
            state2 = self._run_round(state_path, [sub_a, sub_b2], entries)
            self.assertEqual(state2.get("seen"), ["old", "k1"])
            self.assertEqual(state2.get("delivered", {}), {})
            self.assertEqual(len(a_sent), 1, "A 不得重复收")
            self.assertEqual(len(b_sent), 1, "B 补发一次")
            self.assertIn("T{k1}".replace("{k1}", "k1"), b_sent[0])
