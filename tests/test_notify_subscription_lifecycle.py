"""订阅生命周期回归：真实命令与轮询，外部数据库/查询/发送用确定性桩。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID import arknightsuid_notify as notify
from test_notify_race_regressions import DONE_PAYLOAD


KEY = "bot|user|12345"
CRED = SimpleNamespace(uid="12345", nickname="测试")
TASKS = {
    "sanity": notify.TASK_NAME,
    "training": notify.TRAIN_TASK_NAME,
    "building": notify.BUILDING_TASK_NAME,
}


def event(text=""):
    return SimpleNamespace(text=text, user_id="user", bot_id="bot")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, filename in (
        ("_TRAIN_STATE_PATH", "train.json"),
        ("_STATE_PATH", "sanity.json"),
        ("_BUILDING_STATE_PATH", "building.json"),
        ("_SUB_EPOCH_PATH", "epoch.json"),
    ):
        monkeypatch.setattr(notify, name, tmp_path / filename)
    live = {}
    send = AsyncMock()

    async def add(kind, task_name, event, uid):
        live[task_name] = SimpleNamespace(
            uid=uid, bot_id=event.bot_id, user_id=event.user_id, send=send
        )

    async def delete(kind, task_name, event, uid=None):
        live.pop(task_name, None)

    async def get(task_name):
        return [live[task_name]] if task_name in live else []

    monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", add)
    monkeypatch.setattr(notify.gs_subscribe, "delete_subscribe", delete)
    monkeypatch.setattr(notify.gs_subscribe, "get_subscribe", get)
    monkeypatch.setattr(notify, "resolve_or_reply", AsyncMock(return_value=CRED))
    monkeypatch.setattr(notify, "resolve_credential", AsyncMock(return_value=CRED))
    monkeypatch.setattr(
        notify, "fetch_cached_player_info", AsyncMock(return_value=DONE_PAYLOAD)
    )
    reply = AsyncMock()
    monkeypatch.setattr(notify, "_send_text", reply)
    monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())
    return SimpleNamespace(live=live, reply=reply, send=send, get=get)


@pytest.mark.parametrize("chain", ["sanity", "training", "building"])
def test_epoch_failure_does_not_create_subscription(env, monkeypatch, chain):
    real_write = notify._write_state_atomic

    def fail_epoch(path, state, merge_from=None):
        if path == notify._SUB_EPOCH_PATH:
            raise PermissionError("模拟代次文件不可写")
        return real_write(path, state, merge_from=merge_from)

    monkeypatch.setattr(notify, "_write_state_atomic", fail_epoch)
    asyncio.run(getattr(notify, f"subscribe_{chain}_push")(None, event()))
    assert env.live == {}
    assert "失败" in env.reply.await_args.args[2]


def test_cancelled_query_cannot_use_a_new_subscription(env, monkeypatch):
    async def scenario():
        started = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def fetch(credential):
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            await release[index].wait()
            return "模拟查询失败"

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        old = asyncio.create_task(notify.subscribe_training_push(None, event("1小时")))
        await started[0].wait()
        await notify.unsubscribe_training_push(None, event("12345"))
        new = asyncio.create_task(notify.subscribe_training_push(None, event("5小时")))
        await started[1].wait()
        release[0].set()
        await old
        try:
            assert not notify._load_train_state()
            assert "未生效" in env.reply.await_args.args[2]
        finally:
            release[1].set()
            await new
        assert notify._load_train_state()[KEY]["warn_window"] == 18000

    asyncio.run(scenario())


@pytest.mark.parametrize("chain", ["sanity", "training", "building"])
def test_subscription_query_returning_old_session_is_rejected(env, monkeypatch, chain):
    async def scenario():
        task = TASKS[chain]
        old_send, new_send = AsyncMock(), AsyncMock()
        env.live[task] = SimpleNamespace(
            uid="12345", bot_id="bot", user_id="user", send=old_send
        )
        captured, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def get(task_name):
            nonlocal calls
            calls += 1
            rows = await env.get(task_name)
            if calls == 1:
                captured.set()
                await release.wait()
            return rows

        payload = {
            **DONE_PAYLOAD,
            "data": {
                **DONE_PAYLOAD["data"],
                "status": {
                    "name": "测试",
                    "uid": "12345",
                    "ap": {"current": 135, "max": 135},
                },
                "building": {
                    **DONE_PAYLOAD["data"]["building"],
                    "labor": {"value": 200, "maxValue": 200},
                },
            },
        }
        monkeypatch.setattr(
            notify, "fetch_cached_player_info", AsyncMock(return_value=payload)
        )
        monkeypatch.setattr(notify.gs_subscribe, "get_subscribe", get)
        poll = asyncio.create_task(getattr(notify, f"check_{chain}_push")())
        await captured.wait()
        await getattr(notify, f"unsubscribe_{chain}_push")(None, event("12345"))
        await getattr(notify, f"subscribe_{chain}_push")(None, event())
        env.live[task].send = new_send
        release.set()
        await poll
        assert old_send.await_count == 0
        await getattr(notify, f"check_{chain}_push")()
        assert new_send.await_count == 1

    asyncio.run(scenario())


def test_cancel_all_rejects_stale_alive_result_without_state_row(env, monkeypatch):
    async def scenario():
        env.live[notify.TRAIN_TASK_NAME] = SimpleNamespace(
            uid="12345", bot_id="bot", user_id="user", send=env.send
        )
        captured, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def get(task):
            nonlocal calls
            calls += 1
            rows = await env.get(task)
            if calls == 2:
                captured.set()
                await release.wait()
            return rows

        monkeypatch.setattr(notify.gs_subscribe, "get_subscribe", get)
        poll = asyncio.create_task(notify.check_training_push())
        await captured.wait()
        await notify.unsubscribe_training_push(None, event())
        release.set()
        await poll
        assert notify.TRAIN_TASK_NAME not in env.live
        assert not notify._load_train_state()

    asyncio.run(scenario())


@pytest.mark.parametrize("chain", ["sanity", "training", "building"])
def test_cancel_all_invalidates_command_still_resolving_account(
    env, monkeypatch, chain
):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def resolve(*args, **kwargs):
            started.set()
            await release.wait()
            return CRED

        monkeypatch.setattr(notify, "resolve_or_reply", resolve)
        old = asyncio.create_task(
            getattr(notify, f"subscribe_{chain}_push")(None, event())
        )
        await started.wait()
        await getattr(notify, f"unsubscribe_{chain}_push")(None, event())
        release.set()
        await old
        assert TASKS[chain] not in env.live
        assert "未生效" in env.reply.await_args.args[2]

    asyncio.run(scenario())


def test_cancelled_training_update_keeps_existing_notifications(env, monkeypatch):
    async def scenario():
        await notify.subscribe_training_push(None, event("1小时"))
        started = asyncio.Event()

        async def fetch(credential):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        update = asyncio.create_task(
            notify.subscribe_training_push(None, event("5小时"))
        )
        await started.wait()
        update.cancel()
        with pytest.raises(asyncio.CancelledError):
            await update
        monkeypatch.setattr(
            notify, "fetch_cached_player_info", AsyncMock(return_value=DONE_PAYLOAD)
        )
        await notify.check_training_push()
        assert env.send.await_count == 1
        assert notify._load_train_state()[KEY]["warn_window"] == 3600

    asyncio.run(scenario())


@pytest.mark.parametrize("chain", ["sanity", "training", "building"])
def test_database_failure_keeps_existing_notifications(env, monkeypatch, chain):
    async def scenario():
        payload = {
            **DONE_PAYLOAD,
            "data": {
                **DONE_PAYLOAD["data"],
                "status": {
                    "name": "测试",
                    "uid": "12345",
                    "ap": {"current": 135, "max": 135},
                },
                "building": {
                    **DONE_PAYLOAD["data"]["building"],
                    "labor": {"value": 200, "maxValue": 200},
                },
            },
        }
        monkeypatch.setattr(
            notify, "fetch_cached_player_info", AsyncMock(return_value=payload)
        )
        await getattr(notify, f"subscribe_{chain}_push")(None, event())
        monkeypatch.setattr(
            notify.gs_subscribe,
            "add_subscribe",
            AsyncMock(side_effect=RuntimeError("模拟数据库故障")),
        )
        with pytest.raises(RuntimeError):
            await getattr(notify, f"subscribe_{chain}_push")(None, event())
        await getattr(notify, f"check_{chain}_push")()
        assert env.send.await_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["cancel", "database"])
def test_legacy_subscription_recovers_without_prior_intent(env, monkeypatch, failure):
    async def scenario():
        env.live[notify.TRAIN_TASK_NAME] = SimpleNamespace(
            uid="12345", bot_id="bot", user_id="user", send=env.send
        )
        notify._save_train_state(
            {KEY: {"notified": False, "trainee": "", "warn_window": 3600}}
        )
        if failure == "database":
            monkeypatch.setattr(
                notify.gs_subscribe,
                "add_subscribe",
                AsyncMock(side_effect=RuntimeError("模拟数据库故障")),
            )
            with pytest.raises(RuntimeError):
                await notify.subscribe_training_push(None, event("5小时"))
        else:
            started = asyncio.Event()

            async def fetch(credential):
                started.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
            update = asyncio.create_task(
                notify.subscribe_training_push(None, event("5小时"))
            )
            await started.wait()
            update.cancel()
            with pytest.raises(asyncio.CancelledError):
                await update
            monkeypatch.setattr(
                notify, "fetch_cached_player_info", AsyncMock(return_value=DONE_PAYLOAD)
            )
        await notify.check_training_push()
        assert env.send.await_count == 1
        assert notify._load_train_state()[KEY]["warn_window"] == 3600

    asyncio.run(scenario())


def test_newer_intent_invalidates_old_commit_waiting_on_database(env, monkeypatch):
    async def scenario():
        old_entered, new_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_add = notify.gs_subscribe.add_subscribe
        old_task = None

        async def add(*args, **kwargs):
            if asyncio.current_task() is old_task:
                old_entered.set()
                await release.wait()
            await real_add(*args, **kwargs)

        async def fetch(credential):
            if asyncio.current_task() is not old_task:
                new_started.set()
            return "模拟查询失败"

        monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", add)
        monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
        old_task = asyncio.create_task(notify.subscribe_training_push(None, event("1小时")))
        await old_entered.wait()
        new_task = asyncio.create_task(notify.subscribe_training_push(None, event("5小时")))
        await new_started.wait()
        release.set()
        await asyncio.gather(old_task, new_task)
        assert notify._load_train_state()[KEY]["warn_window"] == 18000
        old_replies = [args.args[2] for args in env.reply.await_args_list if args.args[1].text == "1小时"]
        assert len(old_replies) == 1 and "未生效" in old_replies[0]

    asyncio.run(scenario())
