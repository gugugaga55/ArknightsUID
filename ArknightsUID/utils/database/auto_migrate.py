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
from sqlalchemy.ext.asyncio import AsyncEngine

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import engine


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


def _default_clause(column) -> str | None:
    server_default = column.server_default
    if server_default is not None:
        arg = getattr(server_default, "arg", None)
        text_val = getattr(arg, "text", None)
        if text_val is not None:
            return text_val
        if arg is not None:
            return str(arg)
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
    return None


def _add_column_sql(table_name: str, column, dialect) -> str:
    type_sql = column.type.compile(dialect=dialect)
    sql = f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {type_sql}'
    default_clause = _default_clause(column)
    if default_clause is not None:
        sql += f" DEFAULT {default_clause}"
        if not column.nullable:
            sql += " NOT NULL"
    return sql


async def auto_add_missing_columns(
    module_name: str,
    log_prefix: str = "[自动补列]",
) -> None:
    tables = _plugin_tables(module_name)
    if not tables:
        return

    def _existing_columns(sync_conn, table_name):
        insp = sa_inspect(sync_conn)
        if not insp.has_table(table_name):
            return None
        return {col["name"] for col in insp.get_columns(table_name)}

    pending: list[str] = []
    try:
        async with engine.connect() as conn:
            dialect = conn.dialect
            for table in tables:
                existing = await conn.run_sync(_existing_columns, table.name)
                if existing is None:
                    continue
                for column in table.columns:
                    if column.name not in existing:
                        pending.append(_add_column_sql(table.name, column, dialect))
    except Exception as e:
        logger.warning(f"{log_prefix} 读取表结构失败: {e}")
        return

    for sql in pending:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
            logger.info(f"{log_prefix} {sql}")
        except Exception as e:
            logger.warning(f"{log_prefix} 执行失败: {sql} -> {e}")


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
        """待删重复行：每组保留 id 最大的一行（返回行含前三个数据列
        供审计日志展示）。"""
        from sqlalchemy import func, select as sa_select

        keep_max = (
            sa_select(func.max(table.c.id)).group_by(*group_cols).scalar_subquery()
        )
        preview_cols = [
            table.c.id,
            *[table.c[name] for name in sorted(unique_cols)[:2]],
        ]
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


async def ensure_ark_alias_unique_index(
    engine_override: AsyncEngine | None = None,
    log_prefix: str = "[ARK·别名唯一索引]",
) -> bool:
    """ArkAlias (bot_id, user_id, alias) 唯一索引迁移（审计 #37）。

    背景：set_alias 旧实现"先查后插"，并发设置同一别名会产生重复行，
    读取/删除按 .first() 命中不确定。唯一索引 + set_alias 的撞约束收敛
    使其具备数据库级原子性。
    """
    from ..database.models import ArkAlias

    return await ensure_table_unique_index(
        ArkAlias.__table__,
        "ux_ark_alias_key",
        {"bot_id", "user_id", "alias"},
        log_prefix,
        engine_override,
    )
