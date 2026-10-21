"""好友和公开头像：业务命令、边界解析、缓存及模板契约。"""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ArknightsUID import arknightsuid_social as social
from ArknightsUID.utils.path import TEMPLATES_PATH
from ArknightsUID.utils.skland import catalog, friends


def friend_row(index=1):
    return {
        "uid": str(900000000 + index),
        "name": f"好友{index}",
        "level": 120,
        "avatar": {
            "type": "icon",
            "id": "1",
            "url": f"https://bbs.hycdn.cn/{index}.png",
        },
        "lastOnlineTs": 1_700_000_000,
        "sklandUser": {"userId": "internal-id", "nickname": "森空博士"},
    }


def event(text=""):
    return SimpleNamespace(text=text, user_id="owner", bot_id="onebot", group_id=None)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send(self, message, **kwargs):
        self.sent.append(message)


@pytest.fixture
def credential():
    return SimpleNamespace(
        uid="123456789", nickname="博士", cred="SECRET_CRED", token="SECRET_TOKEN"
    )


@pytest.fixture
def social_replies(monkeypatch):
    text, card = AsyncMock(), AsyncMock()
    monkeypatch.setattr(social, "send_text", text)
    monkeypatch.setattr(social, "send_card_or_text", card)
    monkeypatch.setattr(social, "get_image_b64_with_cache", AsyncMock(return_value=""))
    return text, card


@pytest.mark.parametrize("wrapped", [False, True])
def test_friend_models_preserve_official_fields(wrapped):
    rows = [friend_row()]
    payload = {"list": rows, "hasMore": False} if wrapped else rows
    result = friends.parse_friend_list(payload)
    assert result.complete
    assert result.friends[0].uid == "900000001"
    assert result.friends[0].last_online_ts == 1_700_000_000
    assert result.friends[0].avatar_url == rows[0]["avatar"]["url"]
    assert result.friends[0].skland_nickname == "森空博士"


@pytest.mark.asyncio
async def test_friend_cache_is_account_scoped_and_preserves_empty_result(
    monkeypatch, tmp_path, credential
):
    monkeypatch.setattr(friends, "PLAYER_PATH", tmp_path)
    fetch = AsyncMock(return_value={"code": 0, "data": {"list": [], "hasMore": False}})
    monkeypatch.setattr(friends, "get_game_friends", fetch)
    assert (await friends.fetch_friends(credential)).friends == []
    assert (await friends.fetch_friends(credential)).friends == []
    fetch.assert_awaited_once()
    other = SimpleNamespace(uid="987654321")
    await friends.fetch_friends(other)
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_friend_force_bypasses_cache(monkeypatch, tmp_path, credential):
    monkeypatch.setattr(friends, "PLAYER_PATH", tmp_path)
    fetch = AsyncMock(
        side_effect=[
            {"code": 0, "data": [friend_row(1)]},
            {"code": 0, "data": [friend_row(2)]},
        ]
    )
    monkeypatch.setattr(friends, "get_game_friends", fetch)
    await friends.fetch_friends(credential)
    result = await friends.fetch_friends(credential, force=True)
    assert result.friends[0].name == "好友2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {"wrong": []},
        {"list": [None]},
        {"list": [{"uid": "1"}]},
        {"list": [{"uid": "", "name": "x"}]},
    ],
)
async def test_bad_friend_payload_is_not_empty_success_or_cached(
    monkeypatch, tmp_path, credential, data
):
    monkeypatch.setattr(friends, "PLAYER_PATH", tmp_path)
    monkeypatch.setattr(
        friends, "get_game_friends", AsyncMock(return_value={"code": 0, "data": data})
    )
    assert "无法解析" in await friends.fetch_friends(credential)
    assert not (tmp_path / credential.uid / "friends.json").exists()


@pytest.mark.asyncio
async def test_incomplete_friend_list_is_reported_and_not_cached(
    monkeypatch, tmp_path, credential
):
    monkeypatch.setattr(friends, "PLAYER_PATH", tmp_path)
    monkeypatch.setattr(
        friends,
        "get_game_friends",
        AsyncMock(
            return_value={"code": 0, "data": {"list": [friend_row()], "hasMore": True}}
        ),
    )
    result = await friends.fetch_friends(credential)
    assert not result.complete and len(result.friends) == 1
    assert not (tmp_path / credential.uid / "friends.json").exists()


@pytest.mark.asyncio
async def test_friend_failure_does_not_echo_credential(
    monkeypatch, tmp_path, credential
):
    monkeypatch.setattr(friends, "PLAYER_PATH", tmp_path)
    monkeypatch.setattr(
        friends,
        "get_game_friends",
        AsyncMock(
            return_value={
                "code": 10002,
                "message": "cred=SECRET_CRED token=SECRET_TOKEN",
            }
        ),
    )
    text = await friends.fetch_friends(credential)
    assert "10002" in text and "SECRET" not in text


@pytest.mark.asyncio
async def test_public_catalog_requests_correct_source_and_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "CATALOG_CACHE_DIR", tmp_path)
    fetch = AsyncMock(
        side_effect=[
            {
                "code": 0,
                "data": {"list": [{"id": 2, "title": "明日方舟"}], "hasMore": False},
            },
            {
                "code": 0,
                "data": {
                    "list": [{"id": 200232, "url": "https://bbs.hycdn.cn/avatar.png"}],
                    "hasMore": False,
                },
            },
        ]
    )
    monkeypatch.setattr(catalog, "web_public_get", fetch)
    assert await catalog.load_avatar_catalog() == [{"id": 2, "title": "明日方舟"}]
    rows = await catalog.load_avatar_catalog(2)
    assert await catalog.load_avatar_catalog(2) == rows
    assert fetch.await_args_list[0].args == ("/web/v1/user/avatar/category/list", {})
    assert fetch.await_args_list[1].args == ("/web/v1/user/avatars", {"categoryId": 2})
    assert fetch.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"code": 10002},
        {"code": 0, "data": {"list": [{}]}},
        {"code": 0, "data": {"list": [], "hasMore": True}},
    ],
)
async def test_failed_or_incomplete_avatar_catalog_is_not_cached(
    monkeypatch, tmp_path, payload
):
    monkeypatch.setattr(catalog, "CATALOG_CACHE_DIR", tmp_path)
    monkeypatch.setattr(catalog, "web_public_get", AsyncMock(return_value=payload))
    assert isinstance(await catalog.load_avatar_catalog(2), str)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_friend_command_paginates_and_masks_all_rendered_uids(
    monkeypatch, credential, social_replies
):
    text, card = social_replies
    resolve = AsyncMock(return_value=credential)
    monkeypatch.setattr(social, "resolve_or_reply", resolve)
    monkeypatch.setattr(social, "hide_uid_for", AsyncMock(return_value="12***89"))
    batch = friends.parse_friend_list([friend_row(i) for i in range(1, 22)])
    monkeypatch.setattr(social, "fetch_friends", AsyncMock(return_value=batch))
    await social.show_game_friends(None, event("2 页2"))
    resolve.assert_awaited_once()
    assert resolve.await_args.args[2:] == ("", 2)
    context, fallback = card.await_args.args[3:5]
    assert context["page"] == 2 and context["pages"] == 3 and len(context["rows"]) == 10
    assert context["rows"][0]["name"] == "好友11"
    assert "123456789" not in str(context) + fallback
    assert "900000011" not in str(context) + fallback
    assert "internal-id" not in str(context) + fallback
    assert context["rows"][0]["last_online"] == "2023-11-15 06:13"
    text.assert_not_awaited()


@pytest.mark.asyncio
async def test_friend_search_does_not_select_friends_uid_as_own_account(
    monkeypatch, credential, social_replies
):
    _, card = social_replies
    resolve = AsyncMock(return_value=credential)
    monkeypatch.setattr(social, "resolve_or_reply", resolve)
    monkeypatch.setattr(social, "hide_uid_for", AsyncMock(return_value="12***89"))
    monkeypatch.setattr(
        social,
        "fetch_friends",
        AsyncMock(
            return_value=friends.parse_friend_list([friend_row(1), friend_row(2)])
        ),
    )
    await social.show_game_friends(None, event("搜索:900000002"))
    assert resolve.await_args.args[2:] == ("", 0)
    assert [row["name"] for row in card.await_args.args[3]["rows"]] == ["好友2"]
    assert "900000002" not in str(card.await_args.args[3])


@pytest.mark.asyncio
async def test_unbound_friend_command_does_not_request_api(monkeypatch):
    monkeypatch.setattr(social, "resolve_or_reply", AsyncMock(return_value=None))
    fetch = AsyncMock()
    monkeypatch.setattr(social, "fetch_friends", fetch)
    await social.show_game_friends(None, event())
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_avatar_gallery_has_stable_ids_and_template_contract(
    monkeypatch, social_replies
):
    _, card = social_replies
    all_rows = [
        {"id": 200000 + i, "url": f"https://bbs.hycdn.cn/{i}.png"} for i in range(34)
    ]
    fetch = AsyncMock(side_effect=[[{"id": 2, "title": "明日方舟"}], all_rows])
    monkeypatch.setattr(social, "load_avatar_catalog", fetch)
    await social.show_avatar_catalog(None, event("2"))
    context = card.await_args.args[3]
    assert context["pages"] == 3 and len(context["rows"]) == 16
    assert context["rows"][0]["id"] == 200016
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_PATH)),
        undefined=StrictUndefined,
        autoescape=True,
    )
    html = env.get_template("ark_social.html").render(
        font_css_url="about:blank", kaomoji_font_css="", **context
    )
    assert "200016" in html and "头像" in html
    fetch.assert_any_await(2)


@pytest.mark.asyncio
async def test_avatar_lookup_sends_cached_image_bytes(monkeypatch):
    monkeypatch.setattr(
        social,
        "load_avatar_catalog",
        AsyncMock(
            side_effect=[
                [{"id": 2, "title": "明日方舟"}],
                [{"id": 200232, "url": "https://bbs.hycdn.cn/avatar.png"}],
            ]
        ),
    )
    raw = b"\x89PNG\r\n\x1a\nimage"
    image = AsyncMock(
        return_value="data:image/png;base64," + base64.b64encode(raw).decode()
    )
    monkeypatch.setattr(social, "get_image_b64_with_cache", image)
    bot = FakeBot()
    await social.show_avatar_image(bot, event("200232"))
    assert bot.sent == [raw]
    assert image.await_args.args[0] == "https://bbs.hycdn.cn/avatar.png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler, args",
    [
        ("show_game_friends", "页0"),
        ("show_avatar_catalog", "页0"),
        ("show_avatar_image", ""),
        ("show_avatar_image", "https://private.example/"),
    ],
)
async def test_bad_user_arguments_reply_before_any_fetch(
    monkeypatch, social_replies, handler, args
):
    text, card = social_replies
    fetch = AsyncMock()
    monkeypatch.setattr(social, "fetch_friends", fetch)
    monkeypatch.setattr(social, "load_avatar_catalog", fetch)
    await getattr(social, handler)(None, event(args))
    fetch.assert_not_awaited()
    card.assert_not_awaited()
    text.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_empty_friend_result_does_not_claim_no_friends(
    monkeypatch, credential, social_replies
):
    _, card = social_replies
    monkeypatch.setattr(social, "resolve_or_reply", AsyncMock(return_value=credential))
    monkeypatch.setattr(social, "hide_uid_for", AsyncMock(return_value="12***89"))
    monkeypatch.setattr(
        social, "fetch_friends", AsyncMock(return_value=friends.FriendList([], False))
    )
    await social.show_game_friends(None, event())
    context, text = card.await_args.args[3:5]
    assert context["note"] and "尚不完整" in text
    assert "暂无游戏好友" not in text


@pytest.mark.asyncio
async def test_unknown_avatar_category_does_not_download_images(
    monkeypatch, social_replies
):
    text, card = social_replies
    fetch = AsyncMock(return_value=[{"id": 2, "title": "明日方舟"}])
    monkeypatch.setattr(social, "load_avatar_catalog", fetch)
    await social.show_avatar_catalog(None, event("分类:999"))
    fetch.assert_awaited_once_with()
    card.assert_not_awaited()
    assert "未找到" in text.await_args.args[2]


@pytest.mark.asyncio
async def test_avatar_page_out_of_range_does_not_request_thumbnails(
    monkeypatch, social_replies
):
    text, card = social_replies
    monkeypatch.setattr(
        social,
        "load_avatar_catalog",
        AsyncMock(
            side_effect=[
                [{"id": 2, "title": "明日方舟"}],
                [{"id": 1, "url": "https://bbs.hycdn.cn/1.png"}],
            ]
        ),
    )
    image = AsyncMock()
    monkeypatch.setattr(social, "get_image_b64_with_cache", image)
    await social.show_avatar_catalog(None, event("2"))
    image.assert_not_awaited()
    card.assert_not_awaited()
    assert "共 1 页" in text.await_args.args[2]


@pytest.mark.asyncio
async def test_friend_render_fallback_is_readable_and_safe(monkeypatch, credential):
    from ArknightsUID.utils import render

    monkeypatch.setattr(social, "resolve_or_reply", AsyncMock(return_value=credential))
    monkeypatch.setattr(social, "hide_uid_for", AsyncMock(return_value="12***89"))
    row = friend_row()
    row["lastOnlineTs"] = 0
    row["sklandUser"]["nickname"] = ""
    monkeypatch.setattr(
        social,
        "fetch_friends",
        AsyncMock(return_value=friends.parse_friend_list([row])),
    )
    monkeypatch.setattr(social, "get_image_b64_with_cache", AsyncMock(return_value=""))
    monkeypatch.setattr(render, "render_html", AsyncMock(return_value=None))
    bot = FakeBot()
    await social.show_game_friends(bot, event())
    assert len(bot.sent) == 1
    assert (
        "好友1" in bot.sent[0] and "未提供" in bot.sent[0] and "未授权" in bot.sent[0]
    )
    assert "900000001" not in bot.sent[0] and "123456789" not in bot.sent[0]


@pytest.mark.asyncio
async def test_friend_template_escapes_external_names(
    monkeypatch, credential, social_replies
):
    _, card = social_replies
    monkeypatch.setattr(social, "resolve_or_reply", AsyncMock(return_value=credential))
    monkeypatch.setattr(social, "hide_uid_for", AsyncMock(return_value="12***89"))
    row = friend_row()
    row["name"] = "<script>alert(1)</script>"
    monkeypatch.setattr(
        social,
        "fetch_friends",
        AsyncMock(return_value=friends.parse_friend_list([row])),
    )
    await social.show_game_friends(None, event())
    context = card.await_args.args[3]
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_PATH)),
        undefined=StrictUndefined,
        autoescape=True,
    )
    html = env.get_template("ark_social.html").render(
        font_css_url="about:blank", kaomoji_font_css="", **context
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ---- 2026-10-12 修复回归 ----


@pytest.mark.asyncio
async def test_stale_cache_schema_refetches_instead_of_crashing(
    monkeypatch, tmp_path, credential
):
    """历史缓存字段结构与当前模型不一致时按缓存缺失处理，不让命令崩溃。"""
    monkeypatch.setattr(friends, "PLAYER_PATH", tmp_path)
    await friends.cache_write(
        tmp_path / credential.uid / "friends.json", [{"foo": "bar"}]
    )
    fetch = AsyncMock(
        return_value={"code": 0, "data": {"list": [friend_row()], "hasMore": False}}
    )
    monkeypatch.setattr(friends, "get_game_friends", fetch)
    result = await friends.fetch_friends(credential)
    assert result.complete and result.friends[0].name == "好友1"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_bare_list_with_envelope_has_more_is_incomplete(
    monkeypatch, tmp_path, credential
):
    """data 为裸列表、hasMore 落在信封上时，不得当作完整目录缓存。"""
    monkeypatch.setattr(friends, "PLAYER_PATH", tmp_path)
    monkeypatch.setattr(
        friends,
        "get_game_friends",
        AsyncMock(
            return_value={"code": 0, "hasMore": True, "data": [friend_row()]}
        ),
    )
    result = await friends.fetch_friends(credential)
    assert not result.complete and len(result.friends) == 1
    assert not (tmp_path / credential.uid / "friends.json").exists()


@pytest.mark.parametrize(
    "text, uid, index",
    [
        ("UID:1234", "1234", 0),
        ("uid:100000000", "100000000", 0),
        ("账号 3", "", 3),
        ("账号:123456789", "", 123456789),
        ("123456789", "123456789", 0),
        ("2", "", 2),
    ],
)
def test_explicit_account_prefix_wins_over_length_heuristic(text, uid, index):
    """显式 UID/账号前缀优先于位数启发式；裸数字沿用原启发式。"""
    parsed_uid, parsed_index, page, keyword = social.parse_friend_args(text)
    assert (parsed_uid, parsed_index, page, keyword) == (uid, index, 1, "")


def test_data_uri_bytes_tolerates_raw_base64():
    from ArknightsUID.utils.image import data_uri_bytes

    raw = b"\x89PNG-fake-bytes"
    encoded = base64.b64encode(raw).decode()
    assert data_uri_bytes(f"data:image/png;base64,{encoded}") == raw
    assert data_uri_bytes(encoded) == raw
