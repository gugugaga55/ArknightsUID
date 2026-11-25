"""热重载安全的自动补列。

trans_adapter 里的 exec_list 只在 @on_core_start_before 执行；插件热重载 (reload_plugin)
不会重跑 start-before 钩子，于是「模型已加新列、实表还没有」时全表 ORM 查询会报 no such
column。这里配合 @on_core_start（reload 会重跑本插件该钩子）兜底：逐表对比模型列与实表列，
把模型有、实表缺的列 ALTER ADD 补上。幂等；表不存在则跳过（create_all 负责建表）。每条 ALTER
独立事务，互不影响。

表清单不手写：从调用方模块名推出本插件包前缀，扫 SQLModel 子类收集该前缀下的 table 即可，
新增表/列自动覆盖。
"""

from sqlmodel import SQLModel
from sqlalchemy import text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import Column as SaColumn
from sqlalchemy.schema import CreateColumn, DefaultClause, DDLElement
from sqlalchemy.sql import literal, true, false
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import AsyncEngine
from typing import NamedTuple
import re

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import engine


class MigrationResult(NamedTuple):
    """自动补列结果：调用方据此决定依赖缺列的后续操作。

    ok=False 时 failures 为 {表名: [列/原因...]}——读取表结构整体失败时
    用 "<inspect>" 汇总，此时所有表的补列状态未知。
    """

    ok: bool
    failures: dict[str, list[str]]

    @property
    def summary(self) -> str:
        if self.ok:
            return "全部补列成功"
        return "; ".join(
            f"{table}: {', '.join(items)}" for table, items in self.failures.items()
        )


def _plugin_prefix(module_name: str) -> str:
    marker = ".utils.database.models"
    if module_name.endswith(marker):
        return module_name[: -len(marker)]
    return module_name.rsplit(".", 1)[0]


def _plugin_tables(module_name: str) -> list:
    prefix = _plugin_prefix(module_name) + "."
    names = set()
    stack = list(SQLModel.__subclasses__())
    seen_cls = set()
    while stack:
        cls = stack.pop()
        if id(cls) in seen_cls:
            continue
        seen_cls.add(id(cls))
        stack.extend(cls.__subclasses__())
        table = getattr(cls, "__table__", None)
        if table is None:
            continue
        if (cls.__module__ or "").startswith(prefix):
            names.add(table.name)
    return [
        SQLModel.metadata.tables[name]
        for name in names
        if name in SQLModel.metadata.tables
    ]


def _usable_server_default(column) -> DefaultClause | None:
    """迁移 DDL 可用的服务端默认子句（无则 None）。

    - server_default 优先：表达式（false()/func.now()）、TextClause、字符串
      字面量都交给目标方言编译；callable 无法在 DDL 求值，视为不可用
      （不擅自执行）。
    - 无 server_default 时允许提升标量 Python default：布尔 → true()/false()
      按方言编译（固定写 0/1 在 PostgreSQL BOOLEAN 列上非法），数值/字符串
      → literal()（引号按方言转义，不再手拼 "''"）。
    """
    server_default = column.server_default
    if server_default is not None:
        return None if callable(server_default.arg) else server_default
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            return DefaultClause(true() if value else false())
        if isinstance(value, (int, float, str)):
            return DefaultClause(literal(value))
    return None


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validated_identifier(name: str, *, what: str) -> str:
    """DDL 标识符白名单校验：仅允许字母/数字/下划线的常规标识符。

    表/列名来自本插件模型元数据（静态定义），本不该有意外字符；这里
    显式收窄后再经方言 preparer 引用，杜绝任何来源的注入可能——不合规
    名字直接拒绝该列的补列（记入迁移失败，不产生 DDL）。
    """
    if not _IDENTIFIER_RE.fullmatch(str(name or "")):
        raise ValueError(f"非法{what}标识符: {name!r}")
    return str(name)


class AddColumnDDL(DDLElement):
    """ALTER TABLE ... ADD COLUMN 的可执行 DDL 构造。

    SQLAlchemy 没有公开的 ADD COLUMN 构造；经官方 ext.compiler 扩展点
    挂接方言编译，执行侧传构造对象（按连接方言编译）。标识符先过
    _validated_identifier 白名单、再交 preparer 按方言引用——DDL 标识符
    无法用绑定参数，白名单 + 方言引用即等价收窄。
    """

    def __init__(self, table_name: str, column) -> None:
        self.table_name = table_name
        self.column = column


@compiles(AddColumnDDL)
def _compile_add_column(element, compiler, **kw) -> str:
    # 回填语义：有可用常量默认才保留列自身 nullable（旧行按默认回填，
    # NOT NULL 可加）；无默认时补列改为可空，不为旧行强加 NOT NULL。
    # 标识符经白名单校验后使用校验值，非标识符名字在此抛 ValueError
    # （由调用方按迁移失败记账，不产生 DDL）。
    table_name = _validated_identifier(element.table_name, what="表")
    column_name = _validated_identifier(element.column.name, what="列")
    default_clause = _usable_server_default(element.column)
    clone = SaColumn(
        column_name,
        element.column.type,
        nullable=bool(element.column.nullable) if default_clause is not None else True,
        server_default=default_clause,
    )
    return " ".join(
        (
            "ALTER TABLE",
            compiler.preparer.quote(table_name),
            "ADD COLUMN",
            compiler.process(CreateColumn(clone)),
        )
    )


def _add_column_sql(table_name: str, column, dialect) -> str:
    """方言感知的 ADD COLUMN DDL 预览（日志/测试用；执行走 AddColumnDDL）。

    标识符/类型/默认值全部经 SQLAlchemy 按目标方言编译——旧实现硬编码
    双引号标识符，MySQL 默认 sql_mode（无 ANSI_QUOTES）下补列 DDL 直接
    失败；布尔/字符串默认值手拼也会在 PostgreSQL 上产生非法 DDL。
    """
    return str(AddColumnDDL(table_name, column).compile(dialect=dialect))


async def auto_add_missing_columns(
    module_name: str,
    log_prefix: str = "[自动补列]",
) -> MigrationResult:
    """逐表补齐模型有、实表缺的列；返回可消费的结果。

    每条 ALTER 独立事务、互不影响；失败只记录不抛出（不破坏启动后续
    钩子），由调用方按结果跳过依赖缺列的操作。
    """
    tables = _plugin_tables(module_name)
    if not tables:
        return MigrationResult(True, {})
    failures: dict[str, list[str]] = {}

    def _existing_columns(sync_conn, table_name):
        insp = sa_inspect(sync_conn)
        if not insp.has_table(table_name):
            return None
        return {col["name"] for col in insp.get_columns(table_name)}

    pending: list[tuple[str, str]] = []
    expected: dict[str, set[str]] = {}
    try:
        async with engine.connect() as conn:
            dialect = conn.dialect
            for table in tables:
                try:
                    existing = await conn.run_sync(_existing_columns, table.name)
                except Exception as e:
                    failures.setdefault(table.name, []).append(
                        f"读取表结构失败: {e.__class__.__name__}"
                    )
                    continue
                if existing is None:
                    continue
                for column in table.columns:
                    if column.name not in existing:
                        try:
                            sql = _add_column_sql(table.name, column, dialect)
                        except Exception as e:
                            failures.setdefault(table.name, []).append(
                                f"{column.name}: 生成补列SQL失败({e.__class__.__name__})"
                            )
                            continue
                        pending.append((table.name, sql))
                        expected.setdefault(table.name, set()).add(column.name)
    except Exception as e:
        logger.warning(f"{log_prefix} 连接/读取表结构失败: {e}")
        return MigrationResult(False, {"<inspect>": [f"{e.__class__.__name__}: {e}"]})

    for _table_name, sql in pending:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
            logger.info(f"{log_prefix} {sql}")
        except Exception as e:
            logger.warning(f"{log_prefix} 执行失败: {sql} -> {e}")

    # 复核补列结果：不信任"没抛异常=成功"——重查实表列，
    # 仍缺的按失败记账，调用方据此跳过依赖缺列的后续操作。
    if expected:
        try:
            still_missing = await _recheck_missing_columns(expected)
        except Exception as e:
            logger.warning(f"{log_prefix} 补列结果复核失败: {e}")
            still_missing = {table: list(cols) for table, cols in expected.items()}
        for table, columns in still_missing.items():
            failures.setdefault(table, []).extend(
                f"{column}: 补列后仍缺失" for column in columns
            )
    return MigrationResult(not failures, failures)


async def _recheck_missing_columns(
    expected: dict[str, set[str]],
) -> dict[str, list[str]]:
    """重查实表列，返回 {表: [补列后仍缺失的列]}（纯 inspection，无 DDL）。"""

    def _columns(sync_conn, table_name: str):
        insp = sa_inspect(sync_conn)
        if not insp.has_table(table_name):
            return None
        return {col["name"] for col in insp.get_columns(table_name)}

    missing: dict[str, list[str]] = {}
    async with engine.connect() as conn:
        for table_name, columns in expected.items():
            existing = await conn.run_sync(_columns, table_name)
            if existing is None:
                missing[table_name] = list(columns)
                continue
            gap = sorted(columns - existing)
            if gap:
                missing[table_name] = gap
    return missing


async def ensure_table_unique_index(
    table,
    index_name: str,
    unique_cols: set,
    log_prefix: str,
    engine_override: AsyncEngine | None = None,
) -> bool:
    """通用唯一索引迁移（ArkUser / ArkAlias 共用），幂等。

    跨库兼容（SQLite/MySQL/PostgreSQL）：
    - 等价性同时检查唯一索引与表级唯一约束（SQLite 的 UniqueConstraint
      出现在 get_unique_constraints 而 get_indexes 不返回）
    - DDL 走 SQLAlchemy Index(checkfirst=True)，标识符按方言正确引用
      （不用硬编码双引号——MySQL 默认模式下双引号是字符串字面量）
    - 去重先 SELECT 待删行（打审计日志）再按 id 分批 DELETE，
      规避 MySQL 1093（DELETE 目标表不能同时出现在子查询）

    返回 True 表示唯一性就绪；失败（如仍残留重复行）返回 False，下次
    启动重试。
    """
    target = engine_override or engine

    def _index_state(sync_conn) -> int:
        """0=表不存在（建表交给 create_all，新约束随之生效）；1=等价唯一性就绪；2=缺失。"""
        insp = sa_inspect(sync_conn)
        if not insp.has_table(table.name):
            return 0
        for idx in insp.get_indexes(table.name):
            if idx.get("unique") and set(idx["column_names"] or []) == unique_cols:
                return 1
        for con in insp.get_unique_constraints(table.name):
            if set(con.get("column_names") or []) == unique_cols:
                return 1
        return 2

    group_cols = [table.c[name] for name in sorted(unique_cols)]

    def _collect_dupe_rows(sync_conn):
        """待删重复行：每组保留最优行（ArkUser 优先保留有效凭据行，再按最新 id；
        返回行含前三个数据列供审计日志展示）。"""
        from sqlalchemy import func, select as sa_select, tuple_ as sa_tuple

        dup_groups = (
            sa_select(*group_cols)
            .group_by(*group_cols)
            .having(func.count(table.c.id) > 1)
        )
        preview_cols = [
            table.c.id,
            *[table.c[name] for name in sorted(unique_cols)[:2]],
        ]
        if (
            table.name == "ArkUser"
            and "cookie" in table.c
            and "login_disabled" in table.c
        ):
            groups = sync_conn.execute(dup_groups).fetchall()
            if not groups:
                return []
            select_cols = [
                table.c.id,
                *group_cols,
                table.c.cookie,
                table.c.login_disabled,
                *preview_cols[1:],
            ]
            cand_rows = sync_conn.execute(
                sa_select(*select_cols).where(
                    sa_tuple(*group_cols).in_([tuple(g) for g in groups])
                )
            ).fetchall()
            n_group = len(group_cols)
            by_group: dict[tuple, list] = {}
            for r in cand_rows:
                key = tuple(r[1 : 1 + n_group])
                by_group.setdefault(key, []).append(r)
            to_drop = []
            for items in by_group.values():
                if len(items) <= 1:
                    continue
                winner = max(
                    items,
                    key=lambda r: (
                        not bool(r[1 + n_group + 1]) and bool(r[1 + n_group]),
                        not bool(r[1 + n_group + 1]),
                        bool(r[1 + n_group]),
                        int(r[0]),
                    ),
                )
                for r in items:
                    if r[0] != winner[0]:
                        to_drop.append((r[0], *r[1 + n_group + 2 :]))
            return to_drop

        keep_max = (
            sa_select(func.max(table.c.id)).group_by(*group_cols).scalar_subquery()
        )
        rows = sync_conn.execute(
            sa_select(*preview_cols).where(table.c.id.not_in(keep_max))
        ).fetchall()
        return [tuple(r) for r in rows]

    def _delete_ids(sync_conn, ids: list) -> None:
        from sqlalchemy import delete as sa_delete

        for start in range(0, len(ids), 200):  # 分批，避免超长 IN 列表
            sync_conn.execute(
                sa_delete(table).where(table.c.id.in_(ids[start : start + 200]))
            )

    def _create_unique_index(sync_conn) -> None:
        from sqlalchemy import Index as SaIndex

        SaIndex(
            index_name,
            *[table.c[name] for name in sorted(unique_cols)],
            unique=True,
        ).create(sync_conn, checkfirst=True)

    try:
        async with target.connect() as conn:
            state = await conn.run_sync(_index_state)
    except Exception as e:
        logger.warning(f"{log_prefix} 读取索引状态失败: {e}")
        return False
    if state in (0, 1):
        return True  # 新库表级约束已生效 / 上次迁移产物已就绪

    try:
        dupes: list = []
        async with target.begin() as conn:
            dupes = await conn.run_sync(_collect_dupe_rows)
        if dupes:
            # 审计日志：删了哪些行（id/uid/昵称），便于误删追溯
            preview = "; ".join(
                " ".join(f"{str(v)[:14]}" for v in row) for row in dupes[:10]
            )
            logger.warning(
                f"{log_prefix} 清理历史重复行 {len(dupes)} 行"
                f"（保留每组最新写入）: {preview}"
            )
            async with target.begin() as conn:
                await conn.run_sync(_delete_ids, [row[0] for row in dupes])
        async with target.begin() as conn:
            await conn.run_sync(_create_unique_index)
        logger.info(f"{log_prefix} ({', '.join(sorted(unique_cols))}) 唯一索引就绪")
        return True
    except Exception as e:
        # 失败后复查实际状态：存在等价唯一性即视为就绪，其余下次启动重试
        try:
            async with target.connect() as conn:
                if await conn.run_sync(_index_state) == 1:
                    return True
        except Exception:
            pass
        logger.warning(f"{log_prefix} 创建唯一索引失败（下次启动重试）: {e}")
        return False


async def ensure_ark_user_unique_index(
    engine_override: AsyncEngine | None = None,
    log_prefix: str = "[ARK·唯一索引]",
) -> bool:
    """ArkUser (bot_id, user_id, uid) 唯一索引迁移。

    背景：旧版登录经基类 insert_data 的 update 分支只按 (user_id, bot_id)
    更新，重登会把该用户全部账号行改写成当前 uid 并留下重复行。模型新定义
    的 UniqueConstraint 只对新建库生效，存量库由本迁移补齐等价唯一索引。
    upsert_by_uid 在索引缺失时靠 flush 撞约束兜底，索引存在才真正具备
    数据库级原子性。
    """
    from ..database.models import ArkUser

    return await ensure_table_unique_index(
        ArkUser.__table__,
        "ux_ark_user_bot_uid",
        {"bot_id", "user_id", "uid"},
        log_prefix,
        engine_override,
    )


async def ensure_ark_command_alias_unique_index(
    engine_override: AsyncEngine | None = None,
    log_prefix: str = "[ARK·指令别名唯一索引]",
) -> bool:
    """ArkCommandAlias (alias) 唯一索引迁移。"""
    from ..database.models import ArkCommandAlias

    return await ensure_table_unique_index(
        ArkCommandAlias.__table__,
        "ux_ark_cmd_alias",
        {"alias"},
        log_prefix,
        engine_override,
    )


async def ensure_ark_alias_unique_index(
    engine_override: AsyncEngine | None = None,
    log_prefix: str = "[ARK·别名唯一索引]",
    *,
    include_command_alias: bool = True,
) -> bool:
    """ArkAlias (bot_id, user_id, alias) 与 ArkCommandAlias (alias) 唯一索引迁移。

    背景：set_alias 旧实现"先查后插"，并发设置同一别名会产生重复行，
    读取/删除按 .first() 命中不确定。唯一索引 + set_alias 的撞约束收敛
    使其具备数据库级原子性。
    """
    from ..database.models import ArkAlias

    ok_user_alias = await ensure_table_unique_index(
        ArkAlias.__table__,
        "ux_ark_alias_key",
        {"bot_id", "user_id", "alias"},
        log_prefix,
        engine_override,
    )
    ok_cmd_alias = True
    if include_command_alias:
        ok_cmd_alias = await ensure_ark_command_alias_unique_index(engine_override)
    return ok_user_alias and ok_cmd_alias
