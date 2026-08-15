from __future__ import annotations

import unittest
from pathlib import Path


SENTINEL_CRED = "SENTINEL_CRED_DO_NOT_LEAK"
SENTINEL_TOKEN = "SENTINEL_TOKEN_DO_NOT_LEAK"
SENTINEL_HG_TOKEN = "SENTINEL_HG_TOKEN_DO_NOT_LEAK"
SENTINEL_SKLAND_USER = "SENTINEL_SKLAND_USER_DO_NOT_LEAK"
SENSITIVE_VALUES = (
    SENTINEL_CRED,
    SENTINEL_TOKEN,
    SENTINEL_HG_TOKEN,
    SENTINEL_SKLAND_USER,
)
SENSITIVE_FIELD_NAMES = ("cookie", "cred", "token", "hg_token", "skland_user_id")


from ArknightsUID.arknightsuid_account import (  # noqa: E402
    ArkCredential,
    _format_accounts,
    _format_attendance,
    _format_player_info,
    _safe_external_message,
)
from ArknightsUID.arknightsuid_help import HELP_TEXT  # noqa: E402


def credential(
    *,
    uid: str = "123456789",
    nickname: str = "Doctor",
    cookie_status: str = "有效",
    cred: str = SENTINEL_CRED,
) -> ArkCredential:
    return ArkCredential(
        uid=uid,
        nickname=nickname,
        server_id="1",
        cookie_status=cookie_status,
        cred=cred,
        token=SENTINEL_TOKEN,
        hg_token=SENTINEL_HG_TOKEN,
        skland_user_id=SENTINEL_SKLAND_USER,
    )


def assert_reply_safe(testcase: unittest.TestCase, reply: str) -> None:
    for value in SENSITIVE_VALUES:
        testcase.assertNotIn(value, reply)
    lowered = reply.lower()
    for field_name in SENSITIVE_FIELD_NAMES:
        testcase.assertNotIn(field_name, lowered)


class AccountReplySafetyTest(unittest.TestCase):
    def test_format_accounts_masks_uids_and_hides_credentials_for_multiple_accounts(self) -> None:
        reply = _format_accounts(
            ["123456789", "987654321"],
            [
                credential(uid="123456789", nickname="Rhodes"),
                credential(
                    uid="987654321",
                    nickname="Expired",
                    cookie_status="无效",
                ),
            ],
        )

        self.assertIn("Rhodes", reply)
        self.assertIn("Expired", reply)
        self.assertIn("123***789", reply)
        self.assertIn("987***321", reply)
        self.assertIn("可用", reply)
        self.assertIn("凭据失效/缺失", reply)
        self.assertNotIn("123456789", reply)
        self.assertNotIn("987654321", reply)
        assert_reply_safe(self, reply)

    def test_format_accounts_masks_bound_uids_when_credentials_are_missing(self) -> None:
        reply = _format_accounts(["123456789"], [])

        self.assertIn("已找到 UID", reply)
        self.assertIn("123***789", reply)
        self.assertNotIn("123456789", reply)
        assert_reply_safe(self, reply)

    def test_format_accounts_empty_state_has_no_sensitive_fields(self) -> None:
        reply = _format_accounts([], [])

        self.assertEqual(reply, "「明日方舟」绑定账号列表\n未找到明日方舟账号。")
        assert_reply_safe(self, reply)

    def test_format_player_info_normal_data_hides_credential_fields(self) -> None:
        reply = _format_player_info(
            credential(uid="123456789", nickname="FallbackDoctor"),
            {
                "code": 0,
                "data": {
                    "status": {
                        "name": "Amiya",
                        "level": 120,
                        "mainStageProgress": "14-21",
                        "ap": {
                            "current": 42,
                            "max": 135,
                            "completeRecoveryTime": 1893456000,
                        },
                    },
                    "dailyCurrent": 3,
                    "dailyTotal": 5,
                    "weeklyCurrent": 9,
                    "weeklyTotal": 10,
                    "recruit": "空闲",
                },
            },
        )

        self.assertIn("「明日方舟」Amiya", reply)
        self.assertIn("UID：123***789", reply)
        self.assertIn("等级：120", reply)
        self.assertIn("主线进度：14-21", reply)
        self.assertIn("理智：42/135", reply)
        self.assertIn("日常：3/5", reply)
        self.assertIn("周常：9/10", reply)
        self.assertIn("公开招募：空闲", reply)
        self.assertNotIn("123456789", reply)
        assert_reply_safe(self, reply)

    def test_format_player_info_api_error_hides_credential_fields(self) -> None:
        reply = _format_player_info(
            credential(uid="123456789"),
            {"code": 10002, "message": "登录态失效"},
        )

        self.assertEqual(reply, "查询森空岛玩家信息失败：code=10002，message=登录态失效")
        assert_reply_safe(self, reply)

    def test_format_player_info_api_error_redacts_sensitive_echo_message(self) -> None:
        reply = _format_player_info(
            credential(uid="123456789"),
            {
                "code": 10002,
                "message": (
                    "登录态失效 "
                    f"cred={SENTINEL_CRED} token={SENTINEL_TOKEN} "
                    f"hg_token={SENTINEL_HG_TOKEN} "
                    f"skland_user_id={SENTINEL_SKLAND_USER}"
                ),
            },
        )

        self.assertIn("查询森空岛玩家信息失败：code=10002", reply)
        self.assertIn("登录态失效", reply)
        assert_reply_safe(self, reply)

    def test_format_player_info_field_missing_fallback_hides_credential_fields(self) -> None:
        reply = _format_player_info(
            credential(uid="123456789", nickname="FallbackDoctor"),
            {"code": 0, "data": {"unknown": "value"}},
        )

        self.assertIn("「明日方舟」FallbackDoctor", reply)
        self.assertIn("UID：123***789", reply)
        self.assertIn("森空岛请求成功，但当前版本尚未识别详细字段。", reply)
        self.assertNotIn("123456789", reply)
        assert_reply_safe(self, reply)

    def test_format_attendance_success_failure_and_missing_message_are_safe(self) -> None:
        account = credential(uid="123456789")

        success = _format_attendance(account, {"code": 0, "message": "签到完成"})
        failure = _format_attendance(account, {"code": 10001, "msg": "今日已签到"})
        missing = _format_attendance(account, {"code": 10002})

        self.assertEqual(success, "「明日方舟」签到请求成功\nUID：123***789\n签到完成")
        self.assertEqual(failure, "「明日方舟」签到失败\nUID：123***789\ncode=10001，message=今日已签到")
        self.assertEqual(missing, "「明日方舟」签到失败\nUID：123***789\ncode=10002，message=无")
        for reply in (success, failure, missing):
            self.assertNotIn("123456789", reply)
            assert_reply_safe(self, reply)

    def test_format_attendance_redacts_sensitive_echo_message(self) -> None:
        account = credential(uid="123456789")

        success = _format_attendance(
            account,
            {
                "code": 0,
                "message": f"签到完成 cookie={SENTINEL_CRED} token={SENTINEL_TOKEN}",
            },
        )
        failure = _format_attendance(
            account,
            {
                "code": 10001,
                "msg": (
                    "今日已签到 "
                    f"cred={SENTINEL_CRED} hg_token={SENTINEL_HG_TOKEN} "
                    f"skland_user_id={SENTINEL_SKLAND_USER}"
                ),
            },
        )

        self.assertIn("签到完成", success)
        self.assertIn("今日已签到", failure)
        for reply in (success, failure):
            self.assertNotIn("123456789", reply)
            assert_reply_safe(self, reply)

    def test_help_and_readme_keep_commands_without_sensitive_field_names(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        combined = f"{HELP_TEXT}\n{readme}"

        for command in (
            "ark账号",
            "ark绑定",
            "ark扫码登录",
            "ark查看绑定",
            "ark绑定列表",
            "ark理智",
            "ark每日",
            "ark签到",
            "arkwiki",
            "ark帮助",
        ):
            self.assertIn(command, combined)

        lowered = combined.lower()
        for field_name in SENSITIVE_FIELD_NAMES:
            self.assertNotIn(field_name, lowered)

    def test_safe_external_message_redacts_extended_sensitive_fields(self) -> None:
        # 验证扩充后的白名单能脱敏 access_token / refresh_token / password / secret
        # 等常见高敏感字段，覆盖驼峰、下划线、空格变体。
        raw = (
            "登录异常 "
            "access_token=SECRET_ACCESS "
            "accessToken='SECRET_CAMEL' "
            "refresh token=SECRET_REFRESH "
            "refresh_token=SECRET_REFRESH_UNDER "
            "password=SECRET_PWD "
            "passwd:SECRET_PASSWD "
            "secret=\"SECRET_SECRET\" "
            "正常文本保留"
        )
        redacted = _safe_external_message(raw)

        for leaked in (
            "SECRET_ACCESS",
            "SECRET_CAMEL",
            "SECRET_REFRESH",
            "SECRET_REFRESH_UNDER",
            "SECRET_PWD",
            "SECRET_PASSWD",
            "SECRET_SECRET",
            "access_token",
            "accessToken",
            "refresh_token",
            "password",
            "passwd",
            "secret",
        ):
            self.assertNotIn(leaked, redacted)
        self.assertIn("正常文本保留", redacted)
        assert_reply_safe(self, redacted)


if __name__ == "__main__":
    unittest.main()
