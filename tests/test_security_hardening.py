"""安全加固回归测试：2026-08 安全审计修复点。

覆盖：
- 回复脱敏共享化（utils/reply_safety）+ gacha 兑换码回复接入
- bind 成功文案 / notify 推送文案不泄漏明文 UID
- role_id 非纯数字拒写入（防 uid 拼路径逃逸）+ 路径入口防御
- PRTS Cargo where 单引号转义
- cultivate charId 兜底白名单
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ArknightsUID.utils.reply_safety import safe_external_message

SENTINEL_CRED = "SENTINEL_CRED_DO_NOT_LEAK"
SENTINEL_TOKEN = "SENTINEL_TOKEN_DO_NOT_LEAK"


class ReplySafetySharedTest(unittest.TestCase):
    def test_shared_module_redacts_assignments(self) -> None:
        raw = f"登录失败 cred={SENTINEL_CRED} 请重试"
        self.assertNotIn(SENTINEL_CRED, safe_external_message(raw))

    def test_account_module_keeps_alias(self) -> None:
        from ArknightsUID.arknightsuid_account import _safe_external_message

        self.assertNotIn(
            SENTINEL_TOKEN, _safe_external_message(f"token: {SENTINEL_TOKEN}")
        )


class GiftReplyRedactionTest(unittest.TestCase):
    def test_error_message_with_token_redacted(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _gift_reply

        reply = _gift_reply(
            {"code": 1001, "message": f"token={SENTINEL_TOKEN} 无效"}
        )
        self.assertNotIn(SENTINEL_TOKEN, reply)
        self.assertIn("已隐藏敏感内容", reply)

    def test_error_message_with_cred_field_redacted(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _gift_reply

        reply = _gift_reply({"code": 1001, "message": f"cred: {SENTINEL_CRED}"})
        self.assertNotIn(SENTINEL_CRED, reply)

    def test_success_message_normal_text_kept(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _gift_reply

        reply = _gift_reply({"code": 0, "message": "兑换成功，已发放到邮箱"})
        self.assertIn("兑换成功", reply)


class ReplyNoLeakTest(unittest.TestCase):
    def test_bind_success_masks_uid(self) -> None:
        from ArknightsUID.arknightsuid_bind import _format_bind_success

        raw_uid = "100000012345"
        text = _format_bind_success([{"nickname": "博士", "role_id": raw_uid}])
        self.assertNotIn(raw_uid, text)
        self.assertIn("10***45", text)

    def test_push_message_uses_masked_uid(self) -> None:
        from ArknightsUID.arknightsuid_notify import format_push_message
        from ArknightsUID.utils.enduid.adapter import hide_uid

        raw_uid = "100000012345"
        text = format_push_message("博士", 130, hide_uid(raw_uid))
        self.assertNotIn(raw_uid, text)
        self.assertIn("10***45", text)


class StoreRoleValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_persist_rejects_non_digit_role_id(self) -> None:
        from ArknightsUID.utils.skland import store as ark_store

        roles = [
            {
                "role_id": "../../evil",
                "nickname": "x",
                "record_uid": "r",
                "server_id": "1",
            }
        ]
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred", new=mock.AsyncMock(return_value=roles)
        ), mock.patch.object(
            ark_store.ArkUser, "upsert_by_uid", new=mock.AsyncMock(return_value=True)
        ) as m_user, mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ):
            result, err = await ark_store.persist_ark_login("u", "b", None, "CRED")

        self.assertIsNone(result)
        self.assertIsNotNone(err)
        m_user.assert_not_awaited()

    async def test_persist_filters_bad_role_keeps_good(self) -> None:
        from ArknightsUID.utils.skland import store as ark_store

        good = {"role_id": "111", "nickname": "a", "record_uid": "r1", "server_id": "1"}
        bad = {"role_id": "../evil", "nickname": "b", "record_uid": "r2", "server_id": "1"}
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred",
            new=mock.AsyncMock(return_value=[good, bad]),
        ), mock.patch.object(
            ark_store.ArkUser, "upsert_by_uid", new=mock.AsyncMock(return_value=True)
        ) as m_user, mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ):
            result, err = await ark_store.persist_ark_login("u", "b", None, "CRED")

        self.assertIsNone(err)
        self.assertEqual(result, [good])
        self.assertEqual(m_user.await_count, 1)


class UidPathGuardTest(unittest.TestCase):
    def test_cache_path_rejects_traversal(self) -> None:
        from ArknightsUID.utils.player import service as player_service

        with self.assertRaises(ValueError):
            player_service._cache_path("../../evil")

    def test_logs_path_rejects_traversal(self) -> None:
        from ArknightsUID.utils.hypergryph import gacha as hg_gacha

        with self.assertRaises(ValueError):
            hg_gacha._logs_path("../../evil")

    def test_valid_uid_stays_inside_player_path(self) -> None:
        from ArknightsUID.utils.player import service as player_service

        path = player_service._cache_path("100000012345")
        self.assertEqual(path.parent, player_service.PLAYER_PATH / "100000012345")


class CargoWhereEscapeTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_base_skill_escapes_quotes(self) -> None:
        from ArknightsUID.utils.prts import client as prts_client

        captured: dict = {}

        async def fake_api(params, **_kw):
            captured.update(params)
            return {"cargoquery": [{"title": {"room": "贸易站", "description": "d"}}]}

        with mock.patch.object(prts_client, "_api", new=fake_api):
            await prts_client.query_base_skill("贸易站' OR name LIKE '%")

        # 单引号 doubling 后无法再闭合字符串字面量构造 OR 注入
        self.assertEqual(captured["where"], "name='贸易站'' OR name LIKE ''%'")

    async def test_query_base_skill_escapes_backslash(self) -> None:
        from ArknightsUID.utils.prts import client as prts_client

        captured: dict = {}

        async def fake_api(params, **_kw):
            captured.update(params)
            return {"cargoquery": [{"title": {"room": "r", "description": "d"}}]}

        with mock.patch.object(prts_client, "_api", new=fake_api):
            await prts_client.query_base_skill("a\\b'")

        self.assertEqual(captured["where"], "name='a\\\\b'''")

    async def test_query_base_skill_normal_name_unchanged(self) -> None:
        from ArknightsUID.utils.prts import client as prts_client

        captured: dict = {}

        async def fake_api(params, **_kw):
            captured.update(params)
            return {"cargoquery": [{"title": {"room": "r", "description": "d"}}]}

        with mock.patch.object(prts_client, "_api", new=fake_api):
            await prts_client.query_base_skill("雷达部署")

        self.assertEqual(captured["where"], "name='雷达部署'")


class CharIdWhitelistTest(unittest.TestCase):
    def test_accepts_canonical_char_id(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _CHAR_ID_RE

        self.assertIsNotNone(_CHAR_ID_RE.fullmatch("char_123_silverash"))

    def test_rejects_param_pollution_and_traversal(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _CHAR_ID_RE

        for bad in ("char_x&foo=bar", "银灰", "../../evil", "char_1#", "a b"):
            self.assertIsNone(_CHAR_ID_RE.fullmatch(bad), bad)


class SafeCacheStemTest(unittest.TestCase):
    """审计 #7：第三方资源字段 → 安全缓存文件名。"""

    def test_clean_values_pass_through_unchanged(self) -> None:
        """白名单值原样返回（存量缓存文件名兼容，不触发重下）。"""
        from ArknightsUID.utils.gamedata.path_safety import safe_cache_stem

        for good in ("ro1_b-8", "enemy_2001_bocar", "char_002_amiya", "源石虫"):
            self.assertEqual(safe_cache_stem(good), good)

    def test_traversal_and_separators_neutralized(self) -> None:
        from ArknightsUID.utils.gamedata.path_safety import safe_cache_stem

        for evil in ("../evil", "../../target", "/abs/path", "a/b/c", ".."):
            stem = safe_cache_stem(evil)
            self.assertNotIn("/", stem, evil)
            self.assertNotIn("\\", stem, evil)
            self.assertFalse(stem.startswith("."), evil)
            # 替换发生 → 必须带内容哈希后缀区分不同原始值
            self.assertIn("~", stem, evil)

    def test_overlong_values_truncated_with_hash(self) -> None:
        from ArknightsUID.utils.gamedata.path_safety import safe_cache_stem

        stem = safe_cache_stem("a" * 300)
        self.assertLessEqual(len(stem), 64 + 9)
        # 不同长值截断后仍可区分
        self.assertNotEqual(stem, safe_cache_stem("a" * 299))

    def test_blank_falls_back(self) -> None:
        from ArknightsUID.utils.gamedata.path_safety import safe_cache_stem

        self.assertEqual(safe_cache_stem(""), "_blank")
        self.assertEqual(safe_cache_stem("   "), "_blank")

    def test_resolve_inside_rejects_escape(self) -> None:
        from ArknightsUID.utils.gamedata.path_safety import resolve_inside

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inside = resolve_inside(base, "x.json")
            self.assertEqual(inside.parent, base.resolve())
            # 注意 "..\\..\\x" 在 POSIX 上是合法单文件名，不构成越界
            for evil in ("../x.json", "a/../../x", ".."):
                with self.assertRaises(ValueError):
                    resolve_inside(base, evil)


class GamedataPathEscapeTest(unittest.IsolatedAsyncioTestCase):
    """审计 #7：被污染的 levelId / 敌人 key / iconId 不得逃出缓存目录。"""

    async def test_stage_file_uses_sanitized_url_and_path(self) -> None:
        from ArknightsUID.utils.gamedata import rogue_enemy

        captured: dict = {}

        async def fake_fetch(url, cache_path):
            captured["url"], captured["path"] = url, Path(cache_path)
            return {}

        with mock.patch.object(
            rogue_enemy, "_fetch_url_json_cached", new=fake_fetch
        ):
            await rogue_enemy._load_stage_file("../../evil")

        self.assertNotIn("..", str(captured["url"]))
        cache_root = Path(rogue_enemy._STAGE_CACHE_DIR).resolve()
        self.assertEqual(
            Path(captured["path"]).resolve().parent, cache_root,
            f"缓存路径逃出目录: {captured['path']}",
        )

    async def test_enemy_icon_malicious_key_stays_in_cache_dir(self) -> None:
        from ArknightsUID.utils.gamedata import rogue_enemy

        with tempfile.TemporaryDirectory() as tmp:
            icons = Path(tmp) / "icons"
            with mock.patch.object(
                rogue_enemy, "IS_ENEMY_CACHE_DIR", tmp
            ), mock.patch.object(
                rogue_enemy,
                "download_bytes",
                new=mock.AsyncMock(return_value=b"fake-webp"),
            ):
                result = await rogue_enemy.enemy_icon_b64("../../../etc/passwd")

            self.assertTrue(result.startswith("data:image/webp;base64,"))
            # 全部落盘文件都在 icons 一层内，无嵌套/越界
            for path in icons.rglob("*"):
                self.assertEqual(path.parent, icons, f"越界文件: {path}")

    async def test_relic_icon_malicious_id_stays_in_cache_dir(self) -> None:
        from ArknightsUID.utils.gamedata import rogue_relic

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                rogue_relic, "RELIC_ICON_CACHE_DIR", tmp
            ), mock.patch.object(
                rogue_relic,
                "download_bytes",
                new=mock.AsyncMock(return_value=b"fake-webp"),
            ):
                result = await rogue_relic.relic_icon_b64("..\\..\\system32")

            self.assertTrue(result.startswith("data:image/webp;base64,"))
            for path in Path(tmp).rglob("*"):
                self.assertEqual(path.parent, Path(tmp), f"越界文件: {path}")


if __name__ == "__main__":
    unittest.main()


class SafeUrlBasenameTest(unittest.TestCase):
    """审计 P2-3：通用图片缓存入口的 URL basename 清洗。"""

    def test_normal_names_unchanged(self) -> None:
        """正常/percent-encoded 文件名原样返回（存量缓存兼容）。"""
        from ArknightsUID.utils.gamedata.path_safety import safe_url_basename

        self.assertEqual(safe_url_basename("https://cdn.x/a/shot.png"), "shot.png")
        self.assertEqual(
            safe_url_basename(
                "https://media.prts.wiki/a/ab/%E9%81%93%E5%85%B7_%E5%B8%A6%E6%A1%86.png"
            ),
            "%E9%81%93%E5%85%B7_%E5%B8%A6%E6%A1%86.png",
        )

    def test_query_fragment_stripped(self) -> None:
        from ArknightsUID.utils.gamedata.path_safety import safe_url_basename

        self.assertEqual(
            safe_url_basename("https://cdn.x/a/b.png?sign=abc#frag"), "b.png"
        )

    def test_traversal_and_empty_segments_rejected(self) -> None:
        from ArknightsUID.utils.gamedata.path_safety import safe_url_basename

        self.assertEqual(safe_url_basename("https://cdn.x/a/"), "")
        self.assertEqual(safe_url_basename("https://cdn.x/.."), "")
        # 尾段为 ../x 形态（真分隔符）不会出现在 urlsplit 之后段里，
        # 但 Windows 反斜杠混入时必须被清洗且不带路径能力
        stem = safe_url_basename("https://cdn.x/a/..%5Cevil.png")
        self.assertNotIn("/", stem)
        self.assertNotIn("\\", stem)
        self.assertNotIn("..", stem)

    def test_bulk_and_image_entries_use_safe_basename(self) -> None:
        """源码级：缓存入口不得再用裸 split 拼路径（#7/#20 双契约）。"""
        import inspect

        from ArknightsUID.utils.image import pic_download_from_url
        from ArknightsUID.utils.render import render_utils
        from ArknightsUID.utils.resource import bulk_download

        # #7 路径安全：统一走安全命名函数（cache_names_for_url 内部即
        # safe_url_basename + URL 哈希，#20）
        self.assertIn(
            "cache_names_for_url", inspect.getsource(pic_download_from_url)
        )
        self.assertIn(
            "cache_names_for_url", inspect.getsource(render_utils.get_image_b64_with_cache)
        )
        self.assertIn(
            "cache_names_for_url", inspect.getsource(bulk_download.download_target)
        )
