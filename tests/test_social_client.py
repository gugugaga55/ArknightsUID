"""新好友请求走真实签名流程；MockTransport 只替换外部 HTTP。"""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ArknightsUID.utils.skland import client as skland


@pytest.mark.asyncio
async def test_friend_get_signs_exact_query_and_refreshes_invalid_token(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"code": 220}
            if len(requests) == 1
            else {"code": 0, "data": {"list": [], "hasMore": False}},
        )

    monkeypatch.setattr(skland, "get_device_id", lambda **kwargs: "DEVICE")
    monkeypatch.setattr(skland.time, "time", lambda: 1_700_000_000)
    token = AsyncMock(side_effect=["OLD_TOKEN", "NEW_TOKEN"])
    monkeypatch.setattr(skland, "_token_for_cred", token)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(skland, "get_client", AsyncMock(return_value=client))
        result = await skland.get_game_friends(
            SimpleNamespace(uid="123456789", cred="CRED")
        )
    assert result["code"] == 0 and len(requests) == 2
    for request, key in zip(requests, ("OLD_TOKEN", "NEW_TOKEN")):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/game/friend/list"
        assert dict(request.url.params) == {"uid": "123456789"}
        assert request.headers["cred"] == "CRED"
        assert request.headers["vName"] == "2.2.0"
        assert request.headers["vCode"] == "200200057"
        assert request.headers["platform"] == "1"
        header_json = json.dumps(
            {
                "platform": "1",
                "timestamp": "1700000000",
                "dId": "DEVICE",
                "vName": "2.2.0",
            },
            separators=(",", ":"),
        )
        source = request.url.path + "uid=123456789" + "1700000000" + header_json
        digest = hmac.new(key.encode(), source.encode(), hashlib.sha256).hexdigest()
        assert request.headers["sign"] == hashlib.md5(digest.encode()).hexdigest()
        assert not request.content
    assert token.await_args_list[1].kwargs == {"force": True, "stale": "OLD_TOKEN"}


@pytest.mark.asyncio
async def test_friend_request_does_not_replay_auth_headers_on_redirect(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://other.example/friend"})

    monkeypatch.setattr(skland, "get_device_id", lambda **kwargs: "DEVICE")
    monkeypatch.setattr(skland, "_token_for_cred", AsyncMock(return_value="TOKEN"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        monkeypatch.setattr(skland, "get_client", AsyncMock(return_value=client))
        assert (
            await skland.get_game_friends(SimpleNamespace(uid="12345", cred="CRED"))
            is None
        )
    assert len(requests) == 1 and requests[0].url.host == "zonai.skland.com"
