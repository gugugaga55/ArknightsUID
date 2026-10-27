"""网页登录会话：持久化、跨进程限流和带租约的提交状态机。

会话只保存加密的回执路由信息；链接令牌仅存 SHA-256，手机号、验证码和
长期登录凭据从不进入此表。SQLite 与账号数据库不是同一个事务：一旦进入
finalizing 就不自动重放账号写入；中断后要求新链接，避免假称跨库 exactly-once。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..crypto import EncryptionUnavailableError, decrypt_secret, encrypt_secret

SESSION_TTL = 180.0
SESSION_MAX = 20
SEND_COOLDOWN = 60
SEND_MAX = 5
LOGIN_MAX_ATTEMPTS = 5
LEASE_SECONDS = 30.0
INTERRUPTED = "上次登录保存过程已中断，请重新发起登录，勿重复提交旧链接。"
EXPIRED = "登录会话已超时或已结束，请重新发起命令"


class SessionUnavailable(RuntimeError):
    """数据库或加密不可用；不得回退到各进程独立内存状态。"""


class SessionRejected(RuntimeError):
    """可直接展示的会话状态、次数或冷却提示。"""


@dataclass(frozen=True, slots=True)
class SessionClaim:
    auth_hash: str = field(repr=False)
    owner: str = field(repr=False)
    operation: str
    state: dict[str, Any] = field(repr=False)


class WebSessionStore:
    """同步短事务接口；异步处理器须用 asyncio.to_thread 调用。

    每个操作独立打开/关闭连接，没有跨 await 的数据库锁。clock 使用墙上
    时间使持久记录可跨进程/重启；expires_at 从创建起固定，不被重试延长。
    """

    def __init__(
        self,
        path: Path,
        *,
        ttl: float = SESSION_TTL,
        maxsize: int = SESSION_MAX,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = LEASE_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.ttl = ttl
        self.maxsize = maxsize
        self.clock = clock
        self.lease_seconds = lease_seconds

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=2000")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS web_sessions ("
                "auth_hash TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, "
                "payload TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, "
                "phase TEXT NOT NULL, sms_count INTEGER NOT NULL DEFAULT 0, "
                "last_sms_at REAL, attempts INTEGER NOT NULL DEFAULT 0, "
                "claim_id TEXT, claim_until REAL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except (sqlite3.Error, OSError, EncryptionUnavailableError) as exc:
            if connection is not None:
                connection.rollback()
            raise SessionUnavailable("登录会话暂不可用，请稍后重新发起登录。") from exc
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _encode(state: dict[str, Any]) -> str:
        return encrypt_secret(json.dumps(state, ensure_ascii=False))

    @staticmethod
    def _payload(row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(decrypt_secret(row["payload"]))
        except (ValueError, TypeError) as exc:
            raise SessionUnavailable("登录会话无法读取，请重新发起登录。") from exc
        if not isinstance(value, dict):
            raise SessionUnavailable("登录会话无法读取，请重新发起登录。")
        return value

    def _state(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            **self._payload(row),
            "phase": row["phase"],
            "send_count": row["sms_count"],
            "last_send_ts": row["last_sms_at"] or 0.0,
            "attempts": row["attempts"],
            "expires_at": row["expires_at"],
        }

    def _cleanup(self, connection: sqlite3.Connection, now: float) -> None:
        # 已开始的账号写入可以完成，但过期/被顶替的会话不能再获得新提交权。
        connection.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))

    def _read(
        self, connection: sqlite3.Connection, auth_hash: str, now: float
    ) -> sqlite3.Row | None:
        self._cleanup(connection, now)
        row = connection.execute(
            "SELECT * FROM web_sessions WHERE auth_hash = ?", (auth_hash,)
        ).fetchone()
        if (
            row is not None
            and row["claim_until"] is not None
            and row["claim_until"] <= now
        ):
            if row["phase"] in ("sending", "checking"):
                connection.execute(
                    "UPDATE web_sessions SET phase='init', claim_id=NULL, claim_until=NULL "
                    "WHERE auth_hash=?",
                    (auth_hash,),
                )
            elif row["phase"] == "finalizing":
                payload = self._payload(row)
                payload["error_msg"] = INTERRUPTED
                connection.execute(
                    "UPDATE web_sessions SET phase='failed', payload=?, "
                    "claim_id=NULL, claim_until=NULL WHERE auth_hash=?",
                    (self._encode(payload), auth_hash),
                )
            row = connection.execute(
                "SELECT * FROM web_sessions WHERE auth_hash=?", (auth_hash,)
            ).fetchone()
        return row

    def create(self, token: str, state: dict[str, Any]) -> None:
        # 只接收回执元数据，不让调用方误把验证码/凭据塞进持久会话。
        payload = {
            key: state.get(key)
            for key in ("flow", "user_id", "bot_id", "bot_self_id", "group_id")
        }
        encrypted = self._encode(payload)
        now = self.clock()
        # 沿用原行为：同 flow/user 新链接撤销旧链接（不按群建立独立登录）。
        owner_hash = self._hash(f"{payload['flow']}:{payload['user_id']}")
        with self._transaction() as connection:
            self._cleanup(connection, now)
            connection.execute(
                "DELETE FROM web_sessions WHERE owner_hash=?", (owner_hash,)
            )
            connection.execute(
                "INSERT INTO web_sessions(auth_hash,owner_hash,payload,created_at,expires_at,phase) "
                "VALUES(?,?,?,?,?,'init')",
                (self._hash(token), owner_hash, encrypted, now, now + self.ttl),
            )
            connection.execute(
                "DELETE FROM web_sessions WHERE auth_hash IN ("
                "SELECT auth_hash FROM web_sessions ORDER BY created_at DESC, rowid DESC "
                "LIMIT -1 OFFSET ?)",
                (self.maxsize,),
            )

    def get(self, token: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = self._read(connection, self._hash(token), self.clock())
            return self._state(row) if row is not None else None

    def delete(self, token: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM web_sessions WHERE auth_hash=?", (self._hash(token),)
            )

    def claim(self, token: str, operation: str) -> SessionClaim:
        if operation not in ("sending", "checking"):
            raise ValueError("Unknown login operation")
        auth_hash, owner, now = (
            self._hash(token),
            secrets.token_urlsafe(16),
            self.clock(),
        )
        rejection = ""
        claim = None
        with self._transaction() as connection:
            row = self._read(connection, auth_hash, now)
            if row is None or row["phase"] == "done":
                rejection = EXPIRED
            elif row["phase"] == "failed":
                rejection = self._payload(row).get("error_msg") or EXPIRED
            elif row["phase"] != "init":
                rejection = "登录请求正在处理中，请稍候，勿重复提交。"
            elif operation == "sending" and row["sms_count"] >= SEND_MAX:
                rejection = "获取验证码次数过多，请重新发起登录"
            elif (
                operation == "sending"
                and row["last_sms_at"] is not None
                and now - row["last_sms_at"] < SEND_COOLDOWN
            ):
                wait = max(1, int(SEND_COOLDOWN - (now - row["last_sms_at"])))
                rejection = f"请 {wait} 秒后再获取验证码"
            elif operation == "checking" and row["attempts"] >= LOGIN_MAX_ATTEMPTS:
                rejection = "验证码尝试次数过多，请重新发起登录"
                payload = self._payload(row)
                payload["error_msg"] = rejection
                connection.execute(
                    "UPDATE web_sessions SET phase='failed',payload=? WHERE auth_hash=?",
                    (self._encode(payload), auth_hash),
                )
            else:
                connection.execute(
                    "UPDATE web_sessions SET phase=?,claim_id=?,claim_until=?,"
                    "sms_count=sms_count+?,attempts=attempts+?,"
                    "last_sms_at=CASE WHEN ? THEN ? ELSE last_sms_at END WHERE auth_hash=?",
                    (
                        operation,
                        owner,
                        now + self.lease_seconds,
                        int(operation == "sending"),
                        int(operation == "checking"),
                        int(operation == "sending"),
                        now,
                        auth_hash,
                    ),
                )
                claim = SessionClaim(auth_hash, owner, operation, self._state(row))
        if rejection:
            raise SessionRejected(rejection)
        assert claim is not None
        return claim

    def _owned(
        self, connection: sqlite3.Connection, claim: SessionClaim, now: float
    ) -> sqlite3.Row | None:
        row = self._read(connection, claim.auth_hash, now)
        if row is not None and row["claim_id"] == claim.owner:
            return row
        return None

    def renew(self, claim: SessionClaim) -> bool:
        with self._transaction() as connection:
            row = self._owned(connection, claim, self.clock())
            if row is None:
                return False
            connection.execute(
                "UPDATE web_sessions SET claim_until=? WHERE auth_hash=?",
                (self.clock() + self.lease_seconds, claim.auth_hash),
            )
            return True

    def begin_finalization(self, claim: SessionClaim) -> bool:
        with self._transaction() as connection:
            row = self._owned(connection, claim, self.clock())
            if row is None or row["phase"] != "checking":
                return False
            connection.execute(
                "UPDATE web_sessions SET phase='finalizing',claim_until=? WHERE auth_hash=?",
                (self.clock() + self.lease_seconds, claim.auth_hash),
            )
            return True

    def complete(self, claim: SessionClaim, *, error: str = "") -> bool:
        with self._transaction() as connection:
            row = self._owned(connection, claim, self.clock())
            if row is None or (not error and row["phase"] != "finalizing"):
                return False
            payload = self._payload(row)
            if error:
                payload["error_msg"] = error
            connection.execute(
                "UPDATE web_sessions SET phase=?,payload=?,claim_id=NULL,claim_until=NULL "
                "WHERE auth_hash=?",
                ("failed" if error else "done", self._encode(payload), claim.auth_hash),
            )
            return True

    def release(self, claim: SessionClaim) -> bool:
        """结束可重试步骤；finalizing 中断只能变 failed，绝不回 init。"""
        with self._transaction() as connection:
            row = self._owned(connection, claim, self.clock())
            if row is None:
                return False
            payload = self._payload(row)
            phase = "init"
            if row["phase"] == "finalizing":
                phase = "failed"
                payload["error_msg"] = INTERRUPTED
            connection.execute(
                "UPDATE web_sessions SET phase=?,payload=?,claim_id=NULL,claim_until=NULL "
                "WHERE auth_hash=?",
                (phase, self._encode(payload), claim.auth_hash),
            )
            return True
