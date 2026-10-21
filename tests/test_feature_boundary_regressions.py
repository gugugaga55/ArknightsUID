"""订阅、登录存储、年度统计和用户背景下载的行为边界。"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from ArknightsUID import arknightsuid_gacha as gacha
from ArknightsUID import arknightsuid_notify as notify
from ArknightsUID.utils import background, http
from ArknightsUID.utils.player import context
from ArknightsUID.utils.player.parse import PlayerData
from ArknightsUID.utils.skland import store

BJ = timezone(timedelta(hours=8))


def stamp(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=BJ).timestamp())


@pytest.fixture
def utc_host(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("此用例通过 tzset 模拟 UTC 部署")
    with monkeypatch.context() as patch:
        patch.setenv("TZ", "UTC")
        time.tzset()
        try:
            yield
        finally:
            patch.undo()
            time.tzset()


@pytest.mark.parametrize(
    "action, text",
    [
        ("subscribe_sanity_push", ""),
        ("unsubscribe_sanity_push", "12345"),
        ("unsubscribe_sanity_push", ""),
    ],
)
def test_sanity_subscription_changes_preserve_campaign_marker(
    tmp_path, monkeypatch, action, text
):
    monkeypatch.setattr(notify, "_STATE_PATH", tmp_path / "state.json")
    notify.save_push_state(
        {
            "bot|user|12345": {
                "campaign_week": "2026-W40",
                "notified": True,
                "warned": True,
            }
        }
    )
    monkeypatch.setattr(
        notify,
        "resolve_or_reply",
        AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
    )
    monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
    monkeypatch.setattr(notify.gs_subscribe, "delete_subscribe", AsyncMock())
    monkeypatch.setattr(notify, "_send_text", AsyncMock())
    event = SimpleNamespace(bot_id="bot", user_id="user", text=text)
    asyncio.run(getattr(notify, action)(None, event))
    assert notify.load_push_state() == {"bot|user|12345": {"campaign_week": "2026-W40"}}


def test_failed_account_delete_keeps_bindings(monkeypatch):
    monkeypatch.setattr(
        store,
        "list_local_accounts",
        AsyncMock(return_value=[SimpleNamespace(uid="12345")]),
    )
    monkeypatch.setattr(store.ArkUser, "delete_by_user", AsyncMock(return_value=None))
    remove = AsyncMock()
    monkeypatch.setattr(store.ArkBind, "delete_uid", remove)
    assert asyncio.run(store.remove_ark_login("user", "bot")) == 0
    remove.assert_not_awaited()


def test_login_and_unbind_cannot_leave_orphan_binding(monkeypatch):
    async def scenario():
        users, binds = set(), set()
        written, resume = asyncio.Event(), asyncio.Event()
        roles = [
            {
                "role_id": "12345",
                "nickname": "博士",
                "record_uid": "record",
                "server_id": "1",
            }
        ]
        monkeypatch.setattr(
            store, "list_ark_roles_by_cred", AsyncMock(return_value=roles)
        )

        async def upsert(**kwargs):
            users.add(kwargs["uid"])
            written.set()
            await resume.wait()
            return True

        async def insert(user, bot, uid, **kwargs):
            binds.add(uid)
            return 0

        async def delete_user(user, bot, uid=""):
            count = len(users)
            users.clear()
            return count

        async def list_users(*args):
            return [SimpleNamespace(uid=uid) for uid in users]

        async def delete_bind(user, bot, uid):
            binds.discard(uid)

        monkeypatch.setattr(store.ArkUser, "upsert_by_uid", upsert)
        monkeypatch.setattr(store.ArkBind, "insert_uid", insert)
        monkeypatch.setattr(store.ArkUser, "delete_by_user", delete_user)
        monkeypatch.setattr(store, "list_local_accounts", list_users)
        monkeypatch.setattr(store.ArkBind, "delete_uid", delete_bind)
        login = asyncio.create_task(
            store.persist_ark_login("user", "bot", None, "FAKE-CRED")
        )
        await written.wait()
        remove = asyncio.create_task(store.remove_ark_login("user", "bot"))
        await asyncio.sleep(0)
        resume.set()
        await asyncio.gather(login, remove)
        assert users == binds == set()

    asyncio.run(scenario())


def record(when, name, rarity=3, category="1"):
    return {
        "gacha_ts": stamp(when),
        "name": name,
        "rarity": rarity,
        "category": category,
        "pos": 0,
        "basis": "0",
    }


def test_annual_report_uses_beijing_year(utc_host):
    result = gacha._annual_report([record("2026-01-01 00:30:00", "新年抽卡")], "2026")
    assert result is not None and "总抽数：1" in result


def test_annual_pity_includes_previous_year_draws(utc_host):
    records = [
        record("2025-12-30 12:00:00", "上次六星", 5),
        record("2025-12-31 12:00:00", "垫刀1"),
        record("2025-12-31 12:01:00", "垫刀2"),
        record("2026-01-01 12:00:00", "今年六星", 5),
    ]
    result = gacha._annual_report(records, "2026")
    assert "总抽数：1" in result
    assert "今年六星 第 3 抽" in result


def test_status_time_and_registration_date_use_beijing(utc_host):
    timestamp = stamp("2026-01-01 00:30:00")
    assert context.format_dt(timestamp) == "01-01 00:30"
    data = PlayerData(name="博士", uid="12345", ap_max=135, register_ts=timestamp)
    assert context.build_status_context(data, "test")["register_date"] == "2026年1月1日"


def test_campaign_week_uses_beijing_boundary(utc_host):
    assert notify._iso_week_key(stamp("2026-01-05 00:30:00")) == "2026-W02"


@pytest.mark.parametrize("mode", ["private", "redirect", "too_large", "valid"])
def test_background_upload_uses_shared_download_policy(tmp_path, monkeypatch, mode):
    raw = BytesIO()
    Image.new("RGB", (4, 4), "blue").save(raw, "PNG")
    body = raw.getvalue()
    if mode == "too_large":
        body += b"\0" * (http.SAFE_DOWNLOAD_MAX_BYTES + 1)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if mode == "redirect" and request.url.host == "8.8.8.8":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/image"})
        return httpx.Response(200, content=body)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(http, "get_client", AsyncMock(return_value=client))
            url = (
                "http://127.0.0.1/image"
                if mode == "private"
                else "https://8.8.8.8/image"
            )
            saved = await background.save_bg_from_url(url, tmp_path / "bg.webp")
            assert saved is (mode == "valid")
        assert (tmp_path / "bg.webp").exists() is (mode == "valid")
        if mode == "private":
            assert calls == []
        if mode == "redirect":
            assert calls == ["https://8.8.8.8/image"]

    asyncio.run(scenario())


def test_inflight_sanity_poll_does_not_restore_unsubscribed_fields(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(notify, "_STATE_PATH", tmp_path / "state.json")
    key = "bot|user|12345"
    notify.save_push_state({key: {"campaign_week": "2026-W40", "notified": False}})
    active = True
    sub = SimpleNamespace(bot_id="bot", user_id="user", uid="12345", send=AsyncMock())

    async def subscriptions(task):
        return [sub] if active else []

    async def fetch(credential):
        nonlocal active
        active = False
        notify._reset_sanity_state("bot", "user", "12345")
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

    monkeypatch.setattr(notify.gs_subscribe, "get_subscribe", subscriptions)
    monkeypatch.setattr(
        notify,
        "resolve_credential",
        AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
    )
    monkeypatch.setattr(notify, "fetch_cached_player_info", fetch)
    monkeypatch.setattr(notify.asyncio, "sleep", AsyncMock())
    asyncio.run(notify.check_sanity_push())
    assert notify.load_push_state() == {key: {"campaign_week": "2026-W40"}}


def test_other_game_dates_use_beijing(utc_host):
    from ArknightsUID.utils.gamedata.crisis import _fmt_ts
    from ArknightsUID.utils.rogue.records import format_run_ts

    timestamp = stamp("2026-01-01 00:30:00")
    assert _fmt_ts(timestamp) == "2026-01-01"
    assert format_run_ts(str(timestamp)) == "01-01 00:30"


def test_fortune_resets_on_beijing_midnight(utc_host, monkeypatch):
    from ArknightsUID import arknightsuid_help as help_module
    from ArknightsUID.utils.gamedata import charinfo
    from ArknightsUID.utils import time_utils

    monkeypatch.setattr(time_utils.time, "time", lambda: stamp("2026-01-01 00:30:00"))
    monkeypatch.setattr(charinfo, "load_char_index", AsyncMock(return_value={}))
    send = AsyncMock()
    monkeypatch.setattr(help_module, "send_card_or_text", send)
    asyncio.run(
        help_module.send_daily_fortune(
            None, SimpleNamespace(bot_id="bot", user_id="user")
        )
    )
    assert send.await_args.args[3]["date"] == "2026-01-01"


def test_sign_subscription_reports_configured_schedule(monkeypatch):
    monkeypatch.setattr(notify, "_SIGN_HOUR", 20)
    monkeypatch.setattr(notify, "_SIGN_MINUTE", 30)
    monkeypatch.setattr(
        notify,
        "resolve_or_reply",
        AsyncMock(return_value=SimpleNamespace(uid="12345", nickname="博士")),
    )
    monkeypatch.setattr(notify.gs_subscribe, "add_subscribe", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(notify, "_send_text", send)
    asyncio.run(notify.subscribe_sign_push(None, SimpleNamespace(text="")))
    assert "20:30" in send.await_args.args[2]
