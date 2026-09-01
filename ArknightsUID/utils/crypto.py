"""凭据静态加密（at-rest encryption）。

威胁模型：gsuid_core 数据目录（数据库文件）被非授权读取——备份泄漏、
共享主机误配置、误同步到网盘等。本模块让 ArkUser 表里的 cookie(cred) /
token / hg_token 以密文落库，拿到数据库文件不再等于拿到全部账号。

不能防御：能读进程内存、或同时拿到密钥文件的攻击者（密钥与数据同机
存放是可用性取舍；更高安全要求请用环境变量注入密钥并把数据目录与
密钥分开存放）。

密钥来源（优先级从高到低）：
1. 环境变量 ``ARK_UID_ENC_KEY``：urlsafe-base64 的 32 字节 Fernet key
   （``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``）
2. 插件数据目录下密钥文件 ``MAIN_PATH/ark_cred_key``（首次使用自动生成，
   权限 0600；目录本身由 path.init_dir 收紧为 0700）

密文格式：``enc:v1:<fernet token>``。读取侧对无该前缀的值原样返回——
兼容历史明文行，无需停机迁移；该行下次写入时自动升级为密文。
解密失败（换密钥/损坏）返回空串，下游视为凭据失效引导用户重新登录，
重新登录后自愈为新密钥下的密文。

失败语义：加密侧出错（无可用密钥等）抛 ``EncryptionUnavailableError``，
调用方必须转为可读错误并**拒绝落库**——静默明文降级会让 cred/token 直接
写进数据库文件，破坏本模块的威胁模型；解密侧出错返回空串（宁可失效不可
错配）。
"""


from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from gsuid_core.logger import logger

from .path import MAIN_PATH

ENV_KEY_NAME = "ARK_UID_ENC_KEY"
ENC_PREFIX = "enc:v1:"


class EncryptionUnavailableError(RuntimeError):
    """凭据加密不可用（密钥缺失/初始化失败/加密异常）。

    调用方应中止本次凭据落库并提示用户检查密钥，而不是明文存储。
    """

_lock = threading.Lock()
_fernet = None
_fernet_failed = False


def _key_file() -> Path:
    return MAIN_PATH / "ark_cred_key"


def _load_or_create_key_file() -> bytes | None:
    """读密钥文件；不存在则 O_EXCL 原子创建，避免并发双写互相覆盖。

    空/半截文件自愈（审计 #39）：首建时进程在 open 与 write 之间被杀
    会留下 0 字节文件，此后每次启动 O_EXCL 都撞 FileExistsError、重试
    也只读到空——加密永久不可用，必须人工删文件。现在等待窗结束仍为
    空才判"上次写穿失败的残骸"安全重建；等待窗内绝不 unlink（另一
    进程可能持有 fd 正在写，unlink 会让双方各持一把不同的钥匙）。
    """
    path = _key_file()
    try:
        data = path.read_bytes().strip()
        if data:
            return data
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(f"[ArknightsUID·加密] 读取密钥文件失败: {exc.__class__.__name__}")
        return None

    if path.exists():
        # 并发首建（另一进程刚 O_EXCL 建了还没写完）：短暂重试读取。
        # 一次读到空就放弃会让本进程永久落入明文模式（_fernet_failed
        # 粘滞），宁可多等 150ms 也不能粘住。
        for _ in range(3):
            time.sleep(0.05)
            try:
                data = path.read_bytes().strip()
            except OSError:
                return None
            if data:
                return data
        # 仍为空：上次写穿失败的残骸，安全重建
        try:
            path.unlink()
        except OSError:
            return None
        logger.warning(
            "[ArknightsUID·加密] 检测到空密钥文件（上次写入未完成？），已重建"
        )

    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        return key
    except FileExistsError:
        # 重建窗口里被并发者抢先：以对方的钥匙为准
        try:
            return path.read_bytes().strip() or None
        except OSError:
            return None
    except OSError as exc:
        logger.warning(
            f"[ArknightsUID·加密] 创建密钥文件失败: {exc.__class__.__name__}"
        )
        return None


def _rebuild_corrupt_key_file() -> bytes | None:
    """密钥文件内容非法（非空但不是合法 Fernet key）时安全重建。

    非法的 key 材料本就解不开任何密文，替换无损失（受影响密文会
    解密为空串，下游引导重新登录自愈）。返回新 key；失败返回 None。
    """
    from cryptography.fernet import Fernet

    path = _key_file()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return None
    logger.warning(
        "[ArknightsUID·加密] 密钥文件内容损坏，已重建（历史密文将无法解开，"
        "受影响账号需重新登录）"
    )
    key = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        return key
    except OSError:
        return None


def _get_fernet():
    """懒初始化 Fernet（进程级缓存；失败只告警一次）。"""
    global _fernet, _fernet_failed
    if _fernet is not None or _fernet_failed:
        return _fernet
    with _lock:
        if _fernet is not None or _fernet_failed:
            return _fernet
        from cryptography.fernet import Fernet

        env_key = (os.environ.get(ENV_KEY_NAME) or "").strip()
        if env_key:
            try:
                _fernet = Fernet(env_key.encode())
                return _fernet
            except Exception:
                logger.warning(
                    f"[ArknightsUID·加密] {ENV_KEY_NAME} 非法（应为 Fernet key），"
                    "回退密钥文件"
                )
        try:
            file_key = _load_or_create_key_file()
            if not file_key:
                raise ValueError("无可用密钥")
            _fernet = Fernet(file_key)
        except ValueError:
            # 内容非空但不是合法 Fernet key（半截写/手工损坏）：这样的
            # 内容本就解不开任何密文，安全重建一次（审计 #39）；重建
            # 后仍失败才落粘滞失败
            rebuilt = _rebuild_corrupt_key_file()
            if rebuilt is not None:
                _fernet = Fernet(rebuilt)
            else:
                _fernet_failed = True
                logger.warning(
                    "[ArknightsUID·加密] 凭据加密初始化失败，登录/凭据回写将被拒绝"
                    f"（检查 {ENV_KEY_NAME} 或 {_key_file()} 可写性）"
                )
        except Exception as exc:
            _fernet_failed = True
            logger.warning(
                "[ArknightsUID·加密] 凭据加密初始化失败，登录/凭据回写将被拒绝"
                f"（检查 {ENV_KEY_NAME} 或 {_key_file()} 可写性）: "
                f"{exc.__class__.__name__}"
            )
        return _fernet


def _reset_cache() -> None:
    """仅供测试：清空密钥缓存与失败标记。"""
    global _fernet, _fernet_failed
    with _lock:
        _fernet = None
        _fernet_failed = False


def encrypt_secret(value: str) -> str:
    """加密敏感字段；空串与已加密值原样返回。

    加密不可用（无密钥/初始化失败/加密异常）时抛 EncryptionUnavailableError，
    由调用方拒绝落库——明文降级会把 cred/token 直接写进数据库文件，
    破坏本模块的威胁模型（见文件头）。
    """
    text = value or ""
    if not text or text.startswith(ENC_PREFIX):
        return text
    fernet = _get_fernet()
    if fernet is None:
        raise EncryptionUnavailableError(
            f"凭据加密不可用（检查 {ENV_KEY_NAME} 或 {_key_file()} 可写性）"
        )
    try:
        return ENC_PREFIX + fernet.encrypt(text.encode("utf-8")).decode("ascii")
    except Exception as exc:
        raise EncryptionUnavailableError(
            f"加密失败: {exc.__class__.__name__}"
        ) from exc


def decrypt_secret(value: str | None) -> str:
    """解密敏感字段；历史明文原样返回，密文损坏返回空串（记 warning）。"""
    text = (value or "").strip()
    if not text or not text.startswith(ENC_PREFIX):
        return text
    fernet = _get_fernet()
    if fernet is None:
        return ""
    from cryptography.fernet import InvalidToken

    try:
        return fernet.decrypt(text[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        # 只打前缀片段定位问题，绝不打出完整密文/明文
        logger.warning(
            f"[ArknightsUID·加密] 解密失败（密钥变更或数据损坏）: {text[:16]!r}…"
        )
        return ""
    except Exception as exc:
        logger.warning(f"[ArknightsUID·加密] 解密异常: {exc.__class__.__name__}")
        return ""


def cred_equals(stored: str | None, plain: str) -> bool:
    """等值比较落库值与明文凭据（cookie 加密后无法用 SQL 等值匹配）。"""
    if not plain:
        return False
    return decrypt_secret(stored) == plain
