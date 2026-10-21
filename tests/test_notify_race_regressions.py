"""凭据预警与通知并发缺陷回归（2026-10-05）。

A1：maybe_warn_credential 的「冷却重读 → 发送 → 成功记账」必须按
    (bot_id, user_id, uid) 互斥；发送异常 / 返回 -1 / 任务取消不记账；
    不同键保持并行，锁不随历史账号积累。
A2：训练轮询的合并不得覆盖命令侧写入——轮询处理期间改提前量或退订后
    重新订阅，旧一轮快照不允许写回；同键被其他写者更新时不得破坏其字段。

全部走真实 check_training_push / subscribe_training_push /
maybe_warn_credential，仅模拟外部接口与发送端，Event 控制交错（不依赖
随机等待）；未模拟的 HTTP 由 conftest 在传输层拒绝。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID import arknightsuid_notify as notify

KEY = "bot|user|555"
CRED = SimpleNamespace(uid="555", nickname="测试")

# 训练已完成（remainSecs=0，在训史尔特尔）：一轮轮询必产生状态变更
DONE_PAYLOAD = {
    "code": 0,
    "data": {
        "status": {"name": "测试", "uid": "555"},
        "charInfoMap": {"char_172_svrash": {"name": "史尔特尔"}},
        "building": {
            "control": {"level": 5, "chars": []},
            "training": {
                "trainee": {"charId": "char_172_svrash", "targetSkill": 2},
                "remainSecs": 0,
                "lastUpdateTime": 0,
                "speed": 0.5,
            },
        },
    },
}

ARMED = {"notified": False, "trainee": "", "warned": False, "warn_window": 3600}


def _fake_sub(send) -> SimpleNamespace:
    return SimpleNamespace(uid="555", bot_id="bot", user_id="user", send=send)


@pytest.fixture
def train_env(tmp_path, monkeypatch):
    """状态文件隔离 + 公共外部接口桩。"""
    monkeypatch.setattr(notify, "_TRAIN_STATE_PATH", tmp_path / "train.json")
    monkeypatch.setattr(notify, "CRED_WARN_STATE_PATH", tmp_path / "cred.json")
    monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
    monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
    monkeypatch.setattr(notify.gs_subscribe, "delete_subscribe", AsyncMock())
    monkeypatch.setattr(notify, "resolve_credential", AsyncMock(return_value=CRED))
    monkeypatch.setattr(notify, "resolve_or_reply", AsyncMock(return_value=CRED))
    monkeypatch.setattr(notify, "_send_text", AsyncMock())
    monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())
    return tmp_path


def _install_blocking_poll(monkeypatch, started: asyncio.Event):
    """轮询首次拉取挂起（Event 精确制造交错窗口），返回 fetch 供测试放行；
    轮询订阅的发送用 AsyncMock（推送成功、走真实记账路径）。"""
    calls = {"n": 0}

    async def fetch(credential, force: bool = False):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            await fetch.release.wait()
            return DONE_PAYLOAD
        return "模拟：暂时无法查询训练室"

    fetch.release = asyncio.Event()
    monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
    monkeypatch.setattr(
        notify.gs_subscribe,
        "get_subscribe",
        AsyncMock(return_value=[_fake_sub(AsyncMock())]),
    )
    return fetch


# ============================ A1：凭据提醒去重 ============================


def test_same_key_concurrent_warning_sends_once(train_env):
    """同一 (bot,user,uid) 并发触发：只提醒一次、只记一笔冷却（修复前发 2 条）。"""

    async def scenario():
        sends: list[str] = []
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release = asyncio.Event()

        async def send(message: str) -> None:
            sends.append(message)
            if len(sends) == 1:
                first_entered.set()
            else:
                second_entered.set()
            await release.wait()

        sub = _fake_sub(send)
        t1 = asyncio.create_task(notify.maybe_warn_credential(sub, "555", "凭据失效"))
        await first_entered.wait()  # t1 已通过冷却检查、正在发送（尚未记账）
        t2 = asyncio.create_task(notify.maybe_warn_credential(sub, "555", "凭据失效"))
        try:
            # 未修复：t2 也能通过冷却检查并进入 send；修复后：t2 在键锁上等待
            await asyncio.wait_for(second_entered.wait(), 0.2)
        except TimeoutError:
            pass
        release.set()
        await asyncio.gather(t1, t2)

        assert len(sends) == 1
        assert len(notify._load_cred_warn_state()) == 1

    asyncio.run(scenario())


def test_different_keys_stay_parallel(train_env):
    """不同 (bot,user,uid) 的提醒互不串行：两条都进入 send 才能继续。"""

    async def scenario():
        entered: list[int] = []
        barrier = asyncio.Barrier(2)

        async def send(message: str) -> None:
            entered.append(1)
            await barrier.wait()

        await asyncio.gather(
            notify.maybe_warn_credential(_fake_sub(send), "555", "x"),
            notify.maybe_warn_credential(_fake_sub(send), "556", "x"),
        )
        assert len(entered) == 2
        assert len(notify._load_cred_warn_state()) == 2

    asyncio.run(scenario())


def test_send_failure_keeps_retry_available(train_env):
    """发送异常不记账：冷却不落盘，同键可立即重试。"""
    calls = {"n": 0}

    async def send(message: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("网络故障")

    async def scenario():
        sub = _fake_sub(send)
        await notify.maybe_warn_credential(sub, "555", "x")
        assert calls["n"] == 1
        assert notify._load_cred_warn_state() == {}

        await notify.maybe_warn_credential(sub, "555", "x")
        assert calls["n"] == 2
        assert len(notify._load_cred_warn_state()) == 1

    asyncio.run(scenario())


def test_send_minus_one_keeps_retry_available(train_env):
    """send 返回 -1（bot 会话不可用）经 _CheckedSubscribe 视为失败，不记账。"""
    calls = {"n": 0}

    async def send(message: str):
        calls["n"] += 1
        return -1 if calls["n"] == 1 else 0

    async def scenario():
        sub = _fake_sub(send)
        await notify.maybe_warn_credential(sub, "555", "x")
        assert notify._load_cred_warn_state() == {}

        await notify.maybe_warn_credential(sub, "555", "x")
        assert calls["n"] == 2
        assert len(notify._load_cred_warn_state()) == 1

    asyncio.run(scenario())


def test_cancelled_warning_not_recorded_and_lock_released(train_env):
    """任务取消不记账，且键锁随之释放（同键立即可重试，不挂死）。"""

    async def scenario():
        entered = asyncio.Event()

        async def send(message: str) -> None:
            entered.set()
            await asyncio.Event().wait()  # 挂起直到被取消

        sub = _fake_sub(send)
        task = asyncio.create_task(notify.maybe_warn_credential(sub, "555", "x"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert notify._load_cred_warn_state() == {}

        retry_send = AsyncMock()
        await asyncio.wait_for(
            notify.maybe_warn_credential(_fake_sub(retry_send), "555", "x"), 1
        )
        assert retry_send.await_count == 1

    asyncio.run(scenario())


def test_cooldown_blocks_after_success(train_env):
    """成功记账后，冷却窗口内的同键调用不再发送。"""

    async def scenario():
        send = AsyncMock()
        await notify.maybe_warn_credential(_fake_sub(send), "555", "x")
        await notify.maybe_warn_credential(_fake_sub(send), "555", "x")
        assert send.await_count == 1

    asyncio.run(scenario())


# ==================== A2：旧轮询不得覆盖新订阅设置 ====================


def test_lead_change_survives_inflight_poll(train_env, monkeypatch):
    """轮询处理期间把提前量 1h 改 5h：命令写入不被旧轮询覆盖（修复前回滚 3600）。"""

    async def scenario():
        notify._save_train_state({KEY: dict(ARMED)})
        started = asyncio.Event()
        fetch = _install_blocking_poll(monkeypatch, started)

        poll = asyncio.create_task(notify.check_training_push())
        await started.wait()
        await notify.subscribe_training_push(
            None, SimpleNamespace(text="5小时", user_id="user", bot_id="bot")
        )
        assert notify._load_train_state()[KEY]["warn_window"] == 18000

        fetch.release.set()
        await poll
        assert notify._load_train_state()[KEY]["warn_window"] == 18000

    asyncio.run(scenario())


def test_resubscribe_reset_not_restored_by_inflight_poll(train_env, monkeypatch):
    """退订后立即重新订阅：复位行（notified=False、空在训、新提前量）不被
    旧一轮快照恢复成已通知状态。"""

    async def scenario():
        notify._save_train_state({KEY: dict(ARMED)})
        started = asyncio.Event()
        fetch = _install_blocking_poll(monkeypatch, started)

        poll = asyncio.create_task(notify.check_training_push())
        await started.wait()
        await notify.unsubscribe_training_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        await notify.subscribe_training_push(
            None, SimpleNamespace(text="5小时", user_id="user", bot_id="bot")
        )

        fetch.release.set()
        await poll
        row = notify._load_train_state()[KEY]
        assert row["notified"] is False
        assert row["trainee"] == ""
        assert row["warn_window"] == 18000

    asyncio.run(scenario())


def test_same_key_foreign_field_not_clobbered(train_env, monkeypatch):
    """其他写者更新同键字段：轮询发现基底已变则本轮整体弃写，不破坏他人字段。"""

    async def scenario():
        notify._save_train_state({KEY: dict(ARMED)})
        started = asyncio.Event()
        fetch = _install_blocking_poll(monkeypatch, started)

        poll = asyncio.create_task(notify.check_training_push())
        await started.wait()
        # 另一写者（模拟未来共享状态文件的功能）更新同键的无关字段
        rows = notify._load_train_state()
        rows[KEY]["future_feature_flag"] = "on"
        notify._save_train_state(rows)

        fetch.release.set()
        await poll
        final = notify._load_train_state()[KEY]
        assert final["future_feature_flag"] == "on"
        # 基底已变：本轮弃写（通知标记留待下一轮基于新状态重新评估）
        assert final["notified"] is False
        assert final["warn_window"] == 3600

    asyncio.run(scenario())


def test_normal_round_still_records_completion(train_env, monkeypatch):
    """无并发干扰的常规轮：完成推送照常落盘，用户提前量保持原值。"""
    row = dict(ARMED)
    row["warn_window"] = 7200  # 用户自定义提前量 2 小时
    notify._save_train_state({KEY: row})

    async def fetch(credential, force: bool = False):
        return DONE_PAYLOAD

    monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
    monkeypatch.setattr(
        notify.gs_subscribe,
        "get_subscribe",
        AsyncMock(return_value=[_fake_sub(AsyncMock())]),
    )
    asyncio.run(notify.check_training_push())

    final = notify._load_train_state()[KEY]
    assert final["notified"] is True
    assert final["trainee"] == "char_172_svrash"
    assert final["warn_window"] == 7200


# ============ 第二轮复核补充：P3 比较范围 + 三链复位保护 ============

IN_PROGRESS_PAYLOAD = {
    "code": 0,
    "data": {
        "status": {"name": "测试", "uid": "555"},
        "charInfoMap": {"char_172_svrash": {"name": "史尔特尔"}},
        "building": {
            "control": {"level": 5, "chars": []},
            "training": {
                "trainee": {"charId": "char_172_svrash", "targetSkill": 2},
                "remainSecs": 100000,
                "lastUpdateTime": 0,
                "speed": 0.5,
            },
        },
    },
}


def test_unchanged_state_not_rewritten(train_env, monkeypatch):
    """P3：训练状态未变（在训、远超预警窗口）不得判为有变化、重写状态文件。

    new_state 已不含 warn_window，与含该字段的整行 prev 比较永不相等——
    必须只比较轮询负责的字段。"""

    async def scenario():
        notify._save_train_state(
            {
                KEY: {
                    "notified": False,
                    "trainee": "char_172_svrash",
                    "warned": False,
                    "warn_window": 3600,
                }
            }
        )
        monkeypatch.setattr(
            notify,
            "fetch_cached_player_info",
            AsyncMock(return_value=IN_PROGRESS_PAYLOAD),
        )
        monkeypatch.setattr(
            notify.gs_subscribe,
            "get_subscribe",
            AsyncMock(return_value=[_fake_sub(AsyncMock())]),
        )
        writes = {"n": 0}
        real_write = notify._write_state_atomic

        def counted_write(path, state, merge_from=None):
            writes["n"] += 1
            real_write(path, state, merge_from=merge_from)

        monkeypatch.setattr(notify, "_write_state_atomic", counted_write)
        await notify.check_training_push()
        assert writes["n"] == 0
        # 状态内容保持原样
        assert notify._load_train_state()[KEY]["warn_window"] == 3600
        assert notify._load_train_state()[KEY]["notified"] is False

    asyncio.run(scenario())


def test_same_settings_resubscribe_not_clobbered(train_env, monkeypatch):
    """P2：旧轮询发送后，退订再以相同设置重订阅——命令行与基底值完全相等，
    值比较无法区分"没动过"与"重置过"，需订阅版本标识使基底比对失效。"""

    async def scenario():
        # 与订阅命令写入完全同值的行（订阅时查询失败的真实形态，无 warned）
        base = {"notified": False, "trainee": "", "warn_window": 18000}
        notify._save_train_state({KEY: dict(base)})
        started = asyncio.Event()
        fetch = _install_blocking_poll(monkeypatch, started)

        poll = asyncio.create_task(notify.check_training_push())
        await started.wait()
        await notify.unsubscribe_training_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        await notify.subscribe_training_push(
            None, SimpleNamespace(text="5小时", user_id="user", bot_id="bot")
        )
        assert notify._load_train_state()[KEY]["notified"] is False

        fetch.release.set()
        await poll
        final = notify._load_train_state()[KEY]
        assert final["notified"] is False  # 修复前：旧轮询写回 True，下轮漏提醒
        assert final["trainee"] == ""
        assert final["warn_window"] == 18000

    asyncio.run(scenario())


def test_sanity_resubscribe_reset_not_restored(tmp_path, monkeypatch):
    """P2：理智轮询发送后重订阅复位，旧 patch 不得把 notified 写回 True。"""

    async def scenario():
        monkeypatch.setattr(notify, "_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        key = "bot|user|12345"
        notify.save_push_state({key: {"campaign_week": "2026-W40", "notified": False}})
        sub = SimpleNamespace(
            uid="12345", bot_id="bot", user_id="user", send=AsyncMock()
        )
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[sub])
        )
        monkeypatch.setattr(
            notify,
            "resolve_credential",
            AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
        )
        called = {"n": 0}

        async def fetch(credential):
            called["n"] += 1
            if called["n"] == 1:
                # 轮询发送前，用户重订阅触发复位（notified/warned 被清）
                notify._reset_sanity_state("bot", "user", "12345")
            # 理智满：本轮会推送并试图落 notified=True
            return {
                "code": 0,
                "data": {
                    "status": {
                        "name": "博士",
                        "uid": "12345",
                        "ap": {"current": 135, "max": 135},
                    }
                },
            }

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())
        await notify.check_sanity_push()

        row = notify.load_push_state()[key]
        assert row.get("notified") is not True  # 修复前：True，复位被覆盖
        assert row["campaign_week"] == "2026-W40"  # 剿灭标记保留

    asyncio.run(scenario())


def test_building_resubscribe_first_round_not_suppressed(tmp_path, monkeypatch):
    """P2：基建退订→重订阅后，在途轮询不得用旧标记重建状态行
    （否则新订阅的首轮提醒被压掉）。轮询在 fetch 处挂起制造真实交错，
    退订/重订阅全部发生在轮询合并之前。"""

    async def scenario():
        monkeypatch.setattr(notify, "_BUILDING_STATE_PATH", tmp_path / "b.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        key = "bot|user|555"
        notify._save_building_state(
            {key: {"labor": True, "trading": False, "tired": False}}
        )
        sub = SimpleNamespace(uid="555", bot_id="bot", user_id="user", send=AsyncMock())
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[sub])
        )
        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
        monkeypatch.setattr(notify.gs_subscribe, "delete_subscribe", AsyncMock())
        monkeypatch.setattr(notify, "_send_text", AsyncMock())
        monkeypatch.setattr(notify, "resolve_or_reply", AsyncMock(return_value=CRED))
        monkeypatch.setattr(
            notify,
            "resolve_credential",
            AsyncMock(return_value=SimpleNamespace(uid="555", nickname="测试")),
        )
        payload = {
            "code": 0,
            "data": {
                "status": {
                    "name": "测试",
                    "uid": "555",
                    "level": 120,
                    "ap": {"max": 135},
                },
                "chars": [],
                "recruit": [{"startTs": 1, "finishTs": 100, "state": 2}],
                "building": {
                    "control": {"level": 5, "chars": []},
                    "labor": {"value": 100, "maxValue": 200},
                    "manufactures": [],
                    "tradings": [
                        {
                            "slotId": "t1",
                            "level": 3,
                            "chars": [],
                            "strategy": "O_GOLD",
                            "stock": [],
                            "stockLimit": 0,
                        },
                    ],
                    "powers": [],
                    "dormitories": [],
                },
            },
        }
        prev_read = asyncio.Event()
        release = asyncio.Event()

        async def fetch(credential, force: bool = False):
            # 轮询先读 prev 再 fetch：进入 fetch 即保证旧状态行已读入基底；
            # 挂起直到退订/重订阅完成，制造真实交错（否则轮询先结束、
            # 断言平凡通过，测不到合并逻辑）
            prev_read.set()
            await release.wait()
            return payload

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())

        poll = asyncio.create_task(notify.check_building_push())
        await prev_read.wait()
        # 轮询读后、合并前：退订（清行）+ 重订阅（不写行）
        await notify.unsubscribe_building_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        await notify.subscribe_building_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        release.set()
        await poll

        # 旧标记不得重建：状态行保持为空，新订阅首轮由下一轮重新评估
        assert key not in notify._load_building_state()

    asyncio.run(scenario())


def test_building_first_round_resubscribe_not_suppressed(tmp_path, monkeypatch):
    """P2：首次订阅尚无状态行（基底与磁盘都为空），旧轮询发送后退订再
    订阅——不得误判为"真新订阅"写回 labor=True，压掉首轮提醒。"""

    async def scenario():
        monkeypatch.setattr(notify, "_BUILDING_STATE_PATH", tmp_path / "b.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        key = "bot|user|555"
        sub = SimpleNamespace(uid="555", bot_id="bot", user_id="user", send=AsyncMock())
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[sub])
        )
        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
        monkeypatch.setattr(notify.gs_subscribe, "delete_subscribe", AsyncMock())
        monkeypatch.setattr(notify, "_send_text", AsyncMock())
        monkeypatch.setattr(notify, "resolve_or_reply", AsyncMock(return_value=CRED))
        monkeypatch.setattr(
            notify,
            "resolve_credential",
            AsyncMock(return_value=SimpleNamespace(uid="555", nickname="测试")),
        )
        payload = {
            "code": 0,
            "data": {
                "status": {
                    "name": "测试",
                    "uid": "555",
                    "level": 120,
                    "ap": {"max": 135},
                },
                "chars": [],
                "recruit": [{"startTs": -1, "finishTs": -1, "state": 1}],
                "building": {
                    "control": {"level": 5, "chars": []},
                    "labor": {"value": 200, "maxValue": 200},
                    "manufactures": [],
                    "tradings": [
                        {
                            "slotId": "t1",
                            "level": 3,
                            "chars": [],
                            "strategy": "O_GOLD",
                            "stock": [],
                            "stockLimit": 0,
                        },
                    ],
                    "powers": [],
                    "dormitories": [],
                },
            },
        }
        prev_read = asyncio.Event()
        release = asyncio.Event()

        async def fetch(credential, force: bool = False):
            prev_read.set()
            await release.wait()
            return payload

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())

        poll = asyncio.create_task(notify.check_building_push())
        await prev_read.wait()
        await notify.unsubscribe_building_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        await notify.subscribe_building_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        release.set()
        await poll

        # 代次已变：不得以"真新订阅"名义写回 labor=True
        assert key not in notify._load_building_state()

    asyncio.run(scenario())


def test_sanity_reset_row_without_sanity_fields_not_restored(tmp_path, monkeypatch):
    """P2：初始行只有 campaign_week（无 notified/warned），复位不改变视图——
    值比较失效，必须靠订阅代次拦截旧轮询写回。"""

    async def scenario():
        monkeypatch.setattr(notify, "_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        key = "bot|user|12345"
        notify.save_push_state({key: {"campaign_week": "2026-W40"}})
        sub = SimpleNamespace(
            uid="12345", bot_id="bot", user_id="user", send=AsyncMock()
        )
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[sub])
        )
        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
        monkeypatch.setattr(
            notify,
            "resolve_credential",
            AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
        )
        monkeypatch.setattr(
            notify,
            "resolve_or_reply",
            AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
        )
        monkeypatch.setattr(notify, "_send_text", AsyncMock())
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(credential):
            started.set()
            await release.wait()
            return {
                "code": 0,
                "data": {
                    "status": {
                        "name": "博士",
                        "uid": "12345",
                        "ap": {"current": 135, "max": 135},
                    }
                },
            }

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())

        poll = asyncio.create_task(notify.check_sanity_push())
        await started.wait()
        # 重订阅（复位）发生在轮询发送之前：行内无理智字段，视图不变
        await notify.subscribe_sanity_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        release.set()
        await poll

        row = notify.load_push_state()[key]
        assert row.get("notified") is not True  # 修复前：旧 patch 写回 True
        assert row["campaign_week"] == "2026-W40"

    asyncio.run(scenario())


def test_sanity_absent_row_resubscribe_not_restored(tmp_path, monkeypatch):
    """P2：初始无行同样有该漏洞——基底与磁盘都为空时不得按"真新订阅"
    写回旧 patch 的 notified=True。"""

    async def scenario():
        monkeypatch.setattr(notify, "_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        key = "bot|user|12345"
        sub = SimpleNamespace(
            uid="12345", bot_id="bot", user_id="user", send=AsyncMock()
        )
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[sub])
        )
        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
        monkeypatch.setattr(
            notify,
            "resolve_credential",
            AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
        )
        monkeypatch.setattr(
            notify,
            "resolve_or_reply",
            AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
        )
        monkeypatch.setattr(notify, "_send_text", AsyncMock())
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(credential):
            started.set()
            await release.wait()
            return {
                "code": 0,
                "data": {
                    "status": {
                        "name": "博士",
                        "uid": "12345",
                        "ap": {"current": 135, "max": 135},
                    }
                },
            }

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())

        poll = asyncio.create_task(notify.check_sanity_push())
        await started.wait()
        await notify.subscribe_sanity_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        release.set()
        await poll

        row = notify.load_push_state().get(key)
        assert not row or row.get("notified") is not True  # 修复前：notified True

    asyncio.run(scenario())


def test_stale_subscribe_command_cannot_overwrite_newer_settings(
    train_env, monkeypatch
):
    """P2：旧「1小时」订阅命令查询挂起 → 新「5小时」订阅完成 → 旧命令恢复。
    订阅关系仍在（存活检查通过），必须再比订阅代次，否则 18000 被覆盖回
    3600 且旧 sub_ver/快照覆盖新设置。不退订、纯两次订阅交错同样成立。"""

    async def scenario():
        started = asyncio.Event()
        calls = {"n": 0}

        async def fetch(credential, force: bool = False):
            calls["n"] += 1
            if calls["n"] == 1:
                started.set()
                await fetch.release.wait()
                return DONE_PAYLOAD  # 旧命令的订阅状态查询（挂起）
            return "模拟：暂时无法查询训练室"  # 新命令的查询走失败路径

        fetch.release = asyncio.Event()
        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(
            notify.gs_subscribe,
            "get_subscribe",
            AsyncMock(return_value=[_fake_sub(AsyncMock())]),
        )

        old_task = asyncio.create_task(
            notify.subscribe_training_push(
                None, SimpleNamespace(text="1小时", user_id="user", bot_id="bot")
            )
        )
        await started.wait()
        # 新命令在旧命令挂起期间完整执行
        await notify.subscribe_training_push(
            None, SimpleNamespace(text="5小时", user_id="user", bot_id="bot")
        )
        assert notify._load_train_state()[KEY]["warn_window"] == 18000

        fetch.release.set()
        await old_task
        # 旧命令必须因代次变化放弃写入
        assert notify._load_train_state()[KEY]["warn_window"] == 18000

    asyncio.run(scenario())


def test_subscribe_cancelled_midflow_leaves_no_orphan(train_env, monkeypatch):
    """P2：订阅命令的查询期间被退订——恢复后不得写孤儿状态，回执如实说明。"""

    async def scenario():
        started = asyncio.Event()
        calls = {"n": 0}

        async def fetch(credential, force: bool = False):
            calls["n"] += 1
            if calls["n"] == 1:
                started.set()
                await fetch.release.wait()
                return DONE_PAYLOAD
            return "模拟：暂时无法查询训练室"

        fetch.release = asyncio.Event()
        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        # 退订后订阅关系为空（轮询未运行，get_subscribe 只被存活检查调用）
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[])
        )
        send_text = AsyncMock()
        monkeypatch.setattr(notify, "_send_text", send_text)

        task = asyncio.create_task(
            notify.subscribe_training_push(
                None, SimpleNamespace(text="5小时", user_id="user", bot_id="bot")
            )
        )
        await started.wait()
        await notify.unsubscribe_training_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        fetch.release.set()
        await task

        assert KEY not in notify._load_train_state()  # 修复前：孤儿状态行
        reply = send_text.await_args_list[-1].args[2]
        assert "未生效" in reply

    asyncio.run(scenario())


# ============ 第四轮复核：代次写失败 / 后发起者胜 / 无行键取消全部 / 旧会话 ============


def test_epoch_write_failure_aborts_subscribe_with_reply(tmp_path, monkeypatch):
    """P2：代次写失败（如 PermissionError）不得让处理函数抛异常、无回执；
    订阅动作中止并如实告知失败（当前实现直接抛出）。"""

    async def scenario():
        monkeypatch.setattr(notify, "_BUILDING_STATE_PATH", tmp_path / "b.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        sub = SimpleNamespace(uid="555", bot_id="bot", user_id="user", send=AsyncMock())
        add_subscribe = AsyncMock()
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[sub])
        )
        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", add_subscribe)
        monkeypatch.setattr(notify, "resolve_or_reply", AsyncMock(return_value=CRED))
        send_text = AsyncMock()
        monkeypatch.setattr(notify, "_send_text", send_text)
        real_write = notify._write_state_atomic

        def failing_write(path, state, merge_from=None):
            if path == notify._SUB_EPOCH_PATH:
                raise PermissionError(13, "denied")
            return real_write(path, state, merge_from=merge_from)

        monkeypatch.setattr(notify, "_write_state_atomic", failing_write)

        # 不应抛异常；订阅不应添加；回执如实说明失败
        await notify.subscribe_building_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        assert add_subscribe.await_count == 0
        reply = send_text.await_args_list[-1].args[2]
        assert "失败" in reply

    asyncio.run(scenario())


def test_two_hanging_subscribes_last_started_wins(train_env, monkeypatch):
    """两条查询均挂起，正反两种完成顺序都保持后发起的 5 小时设置。"""

    async def scenario(old_first):
        started = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def fetch(credential, force=False):
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            await release[index].wait()
            return "模拟：暂时无法查询训练室"

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(
            notify.gs_subscribe,
            "get_subscribe",
            AsyncMock(return_value=[_fake_sub(AsyncMock())]),
        )
        old = asyncio.create_task(
            notify.subscribe_training_push(
                None, SimpleNamespace(text="1小时", user_id="user", bot_id="bot")
            )
        )
        await started[0].wait()
        new = asyncio.create_task(
            notify.subscribe_training_push(
                None, SimpleNamespace(text="5小时", user_id="user", bot_id="bot")
            )
        )
        await started[1].wait()
        tasks = [old, new]
        for index in [0, 1] if old_first else [1, 0]:
            release[index].set()
            await tasks[index]
        assert notify._load_train_state()[KEY]["warn_window"] == 18000

    for old_first in (True, False):
        asyncio.run(scenario(old_first))


def test_sanity_unsubscribe_all_covers_rowless_key(tmp_path, monkeypatch):
    """P2：理智「取消全部」对无状态行的键也必须更新代次——否则轮末
    存活查询取到旧结果后，旧轮询仍会按"真新订阅"重建孤儿状态。"""

    async def scenario():
        monkeypatch.setattr(notify, "_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        key = "bot|user|12345"
        sub = SimpleNamespace(
            uid="12345", bot_id="bot", user_id="user", send=AsyncMock()
        )
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[sub])
        )
        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
        monkeypatch.setattr(notify.gs_subscribe, "delete_subscribe", AsyncMock())
        monkeypatch.setattr(
            notify,
            "resolve_credential",
            AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
        )
        monkeypatch.setattr(notify, "_send_text", AsyncMock())
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(credential):
            started.set()
            await release.wait()
            return {
                "code": 0,
                "data": {
                    "status": {
                        "name": "博士",
                        "uid": "12345",
                        "ap": {"current": 135, "max": 135},
                    }
                },
            }

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())

        poll = asyncio.create_task(notify.check_sanity_push())
        await started.wait()
        # 无状态行时取消全部（reset 枚举不到行）：仍必须更新代次
        await notify.unsubscribe_sanity_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        release.set()
        await poll

        row = notify.load_push_state().get(key)
        assert not row or row.get("notified") is not True  # 修复前：notified True

    asyncio.run(scenario())


def test_building_unsubscribe_all_covers_rowless_key(tmp_path, monkeypatch):
    """P2：基建「取消全部」对无状态行的键同样必须更新代次。"""

    async def scenario():
        monkeypatch.setattr(notify, "_BUILDING_STATE_PATH", tmp_path / "b.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        key = "bot|user|555"
        sub = SimpleNamespace(uid="555", bot_id="bot", user_id="user", send=AsyncMock())
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[sub])
        )
        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
        monkeypatch.setattr(notify.gs_subscribe, "delete_subscribe", AsyncMock())
        monkeypatch.setattr(notify, "_send_text", AsyncMock())
        monkeypatch.setattr(notify, "resolve_or_reply", AsyncMock(return_value=CRED))
        monkeypatch.setattr(
            notify,
            "resolve_credential",
            AsyncMock(return_value=SimpleNamespace(uid="555", nickname="测试")),
        )
        payload = {
            "code": 0,
            "data": {
                "status": {
                    "name": "测试",
                    "uid": "555",
                    "level": 120,
                    "ap": {"max": 135},
                },
                "chars": [],
                "recruit": [{"startTs": -1, "finishTs": -1, "state": 1}],
                "building": {
                    "control": {"level": 5, "chars": []},
                    "labor": {"value": 200, "maxValue": 200},
                    "manufactures": [],
                    "tradings": [
                        {
                            "slotId": "t1",
                            "level": 3,
                            "chars": [],
                            "strategy": "O_GOLD",
                            "stock": [],
                            "stockLimit": 0,
                        },
                    ],
                    "powers": [],
                    "dormitories": [],
                },
            },
        }
        prev_read = asyncio.Event()
        release = asyncio.Event()

        async def fetch(credential, force: bool = False):
            prev_read.set()
            await release.wait()
            return payload

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())

        poll = asyncio.create_task(notify.check_building_push())
        await prev_read.wait()
        await notify.unsubscribe_building_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        release.set()
        await poll

        assert key not in notify._load_building_state()  # 修复前：labor=True 重建

    asyncio.run(scenario())


def test_training_poll_skips_stale_session_after_resubscribe(train_env, monkeypatch):
    """P2：轮询处理期间订阅被重订阅换会话——旧订阅对象不得收到推送，
    新会话下一轮正常；代次必须绑定订阅列表快照并在发送前验证。"""

    async def scenario():
        notify._save_train_state({KEY: dict(ARMED)})
        started = asyncio.Event()
        release = asyncio.Event()

        old_session_sent = 0

        async def old_send(message: str) -> None:
            nonlocal old_session_sent
            old_session_sent += 1

        old_sub = SimpleNamespace(
            uid="555", bot_id="bot", user_id="user", send=old_send
        )

        async def subscriptions(task):
            return [old_sub]

        monkeypatch.setattr(notify.gs_subscribe, "get_subscribe", subscriptions)

        poll_task = asyncio.create_task(notify.check_training_push())
        # fetch 按任务身份分发：轮询的查询挂起，命令的查询立即返回——
        # 不依赖"谁先调度"（call 计数在并发下不确定）
        poll_task_ref = {"task": poll_task}

        async def dispatch_fetch(credential, force: bool = False):
            if asyncio.current_task() is poll_task_ref["task"]:
                started.set()
                await release.wait()
                return DONE_PAYLOAD
            return "模拟：暂时无法查询训练室"

        monkeypatch.setattr(notify, "fetch_cached_player_info", dispatch_fetch)
        await started.wait()
        # 处理期间重订阅：新会话接管（get_subscribe 改为新 send）
        new_send = AsyncMock()
        new_sub = SimpleNamespace(
            uid="555", bot_id="bot", user_id="user", send=new_send
        )
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[new_sub])
        )
        await notify.subscribe_training_push(
            None, SimpleNamespace(text="5小时", user_id="user", bot_id="bot")
        )
        release.set()
        await poll_task

        # 旧会话不得收到推送（修复前：发给旧会话且落 notified）
        assert old_session_sent == 0
        # 新会话下一轮可正常提醒：标记未被旧轮询落盘
        assert notify._load_train_state()[KEY]["notified"] is False

    asyncio.run(scenario())


def test_sanity_poll_skips_stale_session_after_resubscribe(tmp_path, monkeypatch):
    """P2：理智链同样在发送前验证代次——重订阅换会话后旧会话不收推送、
    不落旧标记。"""

    async def scenario():
        monkeypatch.setattr(notify, "_STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        key = "bot|user|12345"
        old_sent = []

        async def old_send(message: str) -> None:
            old_sent.append(message)

        old_sub = SimpleNamespace(
            uid="12345", bot_id="bot", user_id="user", send=old_send
        )

        async def subscriptions(task):
            return [old_sub]

        monkeypatch.setattr(notify.gs_subscribe, "get_subscribe", subscriptions)
        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
        monkeypatch.setattr(
            notify,
            "resolve_credential",
            AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
        )
        monkeypatch.setattr(
            notify,
            "resolve_or_reply",
            AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
        )
        monkeypatch.setattr(notify, "_send_text", AsyncMock())
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(credential):
            started.set()
            await release.wait()
            return {
                "code": 0,
                "data": {
                    "status": {
                        "name": "博士",
                        "uid": "12345",
                        "ap": {"current": 135, "max": 135},
                    }
                },
            }

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())

        poll = asyncio.create_task(notify.check_sanity_push())
        await started.wait()
        # 处理期间重订阅：新会话接管
        new_send = AsyncMock()
        new_sub = SimpleNamespace(
            uid="12345", bot_id="bot", user_id="user", send=new_send
        )
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[new_sub])
        )
        await notify.subscribe_sanity_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        release.set()
        await poll

        assert not old_sent  # 旧会话不收到
        row = notify.load_push_state().get(key)
        assert not row or row.get("notified") is not True  # 不落旧标记

    asyncio.run(scenario())


def test_building_poll_skips_stale_session_after_resubscribe(tmp_path, monkeypatch):
    """P2：基建链同样在发送前验证代次。"""

    async def scenario():
        monkeypatch.setattr(notify, "_BUILDING_STATE_PATH", tmp_path / "b.json")
        monkeypatch.setattr(notify, "_SUB_EPOCH_PATH", tmp_path / "epoch.json")
        key = "bot|user|555"
        old_sent = []

        async def old_send(message: str) -> None:
            old_sent.append(message)

        old_sub = SimpleNamespace(
            uid="555", bot_id="bot", user_id="user", send=old_send
        )

        async def subscriptions(task):
            return [old_sub]

        monkeypatch.setattr(notify.gs_subscribe, "get_subscribe", subscriptions)
        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
        monkeypatch.setattr(notify.gs_subscribe, "delete_subscribe", AsyncMock())
        monkeypatch.setattr(notify, "_send_text", AsyncMock())
        monkeypatch.setattr(notify, "resolve_or_reply", AsyncMock(return_value=CRED))
        monkeypatch.setattr(
            notify,
            "resolve_credential",
            AsyncMock(return_value=SimpleNamespace(uid="555", nickname="测试")),
        )
        payload = {
            "code": 0,
            "data": {
                "status": {
                    "name": "测试",
                    "uid": "555",
                    "level": 120,
                    "ap": {"max": 135},
                },
                "chars": [],
                "recruit": [{"startTs": -1, "finishTs": -1, "state": 1}],
                "building": {
                    "control": {"level": 5, "chars": []},
                    "labor": {"value": 200, "maxValue": 200},
                    "manufactures": [],
                    "tradings": [
                        {
                            "slotId": "t1",
                            "level": 3,
                            "chars": [],
                            "strategy": "O_GOLD",
                            "stock": [],
                            "stockLimit": 0,
                        },
                    ],
                    "powers": [],
                    "dormitories": [],
                },
            },
        }
        prev_read = asyncio.Event()
        release = asyncio.Event()

        async def fetch(credential, force: bool = False):
            prev_read.set()
            await release.wait()
            return payload

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())

        poll = asyncio.create_task(notify.check_building_push())
        await prev_read.wait()
        new_send = AsyncMock()
        new_sub = SimpleNamespace(
            uid="555", bot_id="bot", user_id="user", send=new_send
        )
        monkeypatch.setattr(
            notify.gs_subscribe, "get_subscribe", AsyncMock(return_value=[new_sub])
        )
        await notify.subscribe_building_push(
            None, SimpleNamespace(text="", user_id="user", bot_id="bot")
        )
        release.set()
        await poll

        assert not old_sent  # 旧会话不收到
        row = notify._load_building_state().get(key)
        assert not row or row.get("labor") is not True  # 不落旧标记

    asyncio.run(scenario())
