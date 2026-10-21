"""离线防线覆盖真实默认传输，并且允许 MockTransport 协议测试。"""

import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from runtime_isolation import block_external_network


def test_sync_http_cannot_escape_even_when_caller_swallows_error(monkeypatch):
    requests = block_external_network(monkeypatch)
    with httpx.Client() as client:
        with pytest.raises(AssertionError, match="离线测试"):
            client.get("https://example.com/")
    assert requests == ["GET example.com/"]


@pytest.mark.asyncio
async def test_async_http_cannot_escape(monkeypatch):
    requests = block_external_network(monkeypatch)
    async with httpx.AsyncClient() as client:
        with pytest.raises(AssertionError, match="离线测试"):
            await client.get("https://example.com/")
    assert requests == ["GET example.com/"]


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_direct_socket_cannot_bypass_http_guard(monkeypatch, method):
    requests = block_external_network(monkeypatch)
    with socket.socket() as client:
        with pytest.raises(AssertionError, match="离线测试"):
            getattr(client, method)(("203.0.113.1", 443))
    assert len(requests) == 1
    assert "203.0.113.1" in requests[0]


@pytest.mark.asyncio
async def test_mock_transports_still_work(monkeypatch):
    requests = block_external_network(monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.com/").json() == {"ok": True}
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get("https://example.com/")).json() == {"ok": True}
    assert requests == []


@pytest.mark.parametrize("stage", ["collection", "test"])
def test_swallowed_http_attempt_still_fails_pytest(tmp_path, stage):
    attempt = textwrap.dedent('''
        import httpcore
        import httpx
        from unittest.mock import patch

        def attempt_http():
            with httpx.Client() as client:
                with patch.object(
                    client._transport._pool, "handle_request",
                    return_value=httpcore.Response(200),
                ):
                    try:
                        client.get("https://example.com/")
                    except AssertionError:
                        pass
    ''')
    test_file = tmp_path / "test_swallowed_network.py"
    call = "attempt_http()\n" if stage == "collection" else ""
    test_file.write_text(
        attempt + call + "def test_request():\n    "
        + ("pass\n" if stage == "collection" else "attempt_http()\n"),
        encoding="utf-8",
    )
    tests_root = Path(__file__).resolve().parent
    script = (
        f"import sys; sys.path.insert(0, {str(tests_root)!r}); "
        "import conftest; import pytest; "
        f"raise SystemExit(pytest.main(['-q', '--tb=short', '--confcutdir', "
        f"{str(tmp_path)!r}, {str(test_file)!r}], plugins=[conftest]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    message = "测试收集阶段尝试联网" if stage == "collection" else "请在业务边界模拟"
    assert message in result.stdout + result.stderr
