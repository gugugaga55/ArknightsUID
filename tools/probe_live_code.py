"""真实 cred 探测：refresh + player_info 的实际返回 code（排障专用，一次性）。

用法（dev venv，插件根目录）：
  python tools/probe_live_code.py <32位cred> [uid]

输出只打 code/message 与掩码 token，不打完整凭据。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ArknightsUID.utils.enduid.adapter import ArkCredential  # noqa: E402
from ArknightsUID.utils.skland import client as skland_client  # noqa: E402


def mask(value: str) -> str:
    return f"{value[:6]}...{value[-4:]}（len={len(value)}）" if value else "<empty>"


async def main(cred: str, uid: str) -> None:
    print("=== 1. refresh_token ===")
    try:
        token = await skland_client.refresh_token(cred)
    except skland_client.CredentialInvalidError as exc:
        print(f"  CredentialInvalidError: {exc}")
        return
    print(f"  token: {mask(token) if token else 'FAIL（见上方 warning）'}")
    if not token:
        return

    cred_obj = ArkCredential(
        uid=uid, nickname="", server_id="1", cookie_status="",
        cred=cred, token=token, hg_token="", skland_user_id="",
    )

    print("\n=== 2. get_player_info（did=''，现行默认）===")
    resp = await skland_client.get_player_info(cred_obj)
    if resp is None:
        print("  resp=None（网络/HTTP 层失败）")
    else:
        print(f"  code={resp.get('code')!r} message={resp.get('message')!r}")
        data = resp.get("data") or {}
        print(f"  data.status.name={(data.get('status') or {}).get('name')!r}")

    print("\n=== 3. get_player_info（use_device_id=True，dId 对照组）===")
    resp2 = await skland_client._signed_request(
        cred_obj,
        url=skland_client.GAME_PLAYER_INFO_URL,
        method="GET",
        uid=uid,
        game_id=skland_client.ARKNIGHTS_CHANNEL_OFFICIAL,
        params={"uid": uid, "gameId": skland_client.ARKNIGHTS_CHANNEL_OFFICIAL},
        use_device_id=True,
    )
    if resp2 is None:
        print("  resp=None（网络/HTTP 层失败）")
    else:
        print(f"  code={resp2.get('code')!r} message={resp2.get('message')!r}")

    print("\n=== 4. get_binding（did=''）===")
    resp3 = await skland_client.get_binding(cred_obj)
    if resp3 is None:
        print("  resp=None")
    else:
        print(f"  code={resp3.get('code')!r} message={resp3.get('message')!r}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
