"""将真实 GsCore 的数据路径重定向到临时目录，须在导入配置/插件前调用。"""

from pathlib import Path


def block_external_network(monkeypatch) -> list[str]:
    """拒绝默认 HTTP 和直接 TCP 连接，保留 MockTransport。"""
    import socket

    import httpx

    requests: list[str] = []

    def reject_request(self, request):
        requests.append(f"{request.method} {request.url.host}{request.url.path}")
        raise AssertionError("离线测试未模拟 HTTP 请求")

    async def reject_async_request(self, request):
        return reject_request(self, request)

    def reject_connect(self, address):
        requests.append(f"socket {address}")
        raise AssertionError("离线测试未模拟 socket 连接")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_request)
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", reject_async_request
    )
    monkeypatch.setattr(socket.socket, "connect", reject_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_connect)
    return requests


def isolate_data_root(root: Path) -> None:
    import gsuid_core.data_store as data_store

    original = data_store.gs_data_path
    root.mkdir(parents=True, exist_ok=True)
    # 保留真实 get_res_path 实现，并迁移其模块级派生路径；不替换核心模块。
    for name, value in list(vars(data_store).items()):
        if isinstance(value, Path) and value.is_relative_to(original):
            isolated = root / value.relative_to(original)
            if value.is_dir():
                isolated.mkdir(parents=True, exist_ok=True)
            setattr(data_store, name, isolated)
