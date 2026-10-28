"""SQLite 读写辅助。

会话归属账号只存在 ``workbuddy.db`` 的 ``sessions.user_id`` 里，所以这里是整个工具的核心。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Sequence

DB_NAME = "workbuddy.db"

#: 参与迁移的表（按外键/依赖顺序无关，因为都没有强制外键）
MERGE_TABLES: tuple[str, ...] = (
    "sessions",
    "session_usage",
    "workspaces",
    "automations",
    "automation_runs",
    "automation_runtime_state",
    "buddy_snapshots",
)


class DbError(RuntimeError):
    pass


def db_path(data_dir: Path) -> Path:
    return data_dir / DB_NAME


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if not path.exists():
        raise DbError(f"找不到数据库：{path}")
    if readonly:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    else:
        con = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def table_names(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def has_table(con: sqlite3.Connection, table: str) -> bool:
    return table in table_names(con)


def quote(cols: Sequence[str]) -> str:
    return ", ".join(f'"{c}"' for c in cols)


def snapshot_db(src: Path, dst: Path) -> None:
    """用 SQLite backup API 生成一致性快照（自动包含 WAL 中未落盘的数据）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    src_con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=15)
    dst_con = sqlite3.connect(str(dst))
    try:
        with dst_con:
            src_con.backup(dst_con)
    finally:
        src_con.close()
        dst_con.close()


def session_counts(con: sqlite3.Connection, include_deleted: bool = False) -> dict[str, int]:
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    rows = con.execute(f"SELECT user_id, COUNT(*) FROM sessions {where} GROUP BY user_id")
    return {(r[0] or ""): r[1] for r in rows}


def all_session_ids(con: sqlite3.Connection, user_id: Optional[str] = None) -> list[str]:
    if user_id is None:
        rows = con.execute("SELECT id FROM sessions")
    else:
        rows = con.execute("SELECT id FROM sessions WHERE user_id = ?", (user_id,))
    return [r[0] for r in rows]


def fetch_sessions(con: sqlite3.Connection, user_id: str, include_deleted: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM sessions WHERE user_id = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    sql += " ORDER BY COALESCE(last_activity_at, updated_at) DESC"
    return list(con.execute(sql, (user_id,)))


def merge_tables(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    tables: Iterable[str] = MERGE_TABLES,
    overwrite: bool = False,
) -> dict[str, dict[str, int]]:
    """把 source 库里的行合并进 target 库。

    - 主键冲突时：默认 ``INSERT OR IGNORE``（保留本机已有数据），``overwrite=True`` 时覆盖。
    - ``workspaces`` 特殊处理：保留较大的 last_opened_at。
    """
    report: dict[str, dict[str, int]] = {}
    t_tables = table_names(target)
    s_tables = table_names(source)

    for table in tables:
        if table not in t_tables or table not in s_tables:
            report[table] = {"inserted": 0, "skipped": 1}
            continue

        s_cols = table_columns(source, table)
        t_cols = set(table_columns(target, table))
        cols = [c for c in s_cols if c in t_cols]
        if not cols:
            report[table] = {"inserted": 0, "skipped": 1}
            continue

        rows = list(source.execute(f'SELECT {quote(cols)} FROM "{table}"'))
        before = target.total_changes

        if table == "workspaces" and "last_opened_at" in cols:
            sql = (
                f'INSERT INTO "workspaces" ({quote(cols)}) VALUES ({",".join("?" * len(cols))}) '
                'ON CONFLICT("path") DO UPDATE SET last_opened_at = '
                'MAX("workspaces".last_opened_at, excluded.last_opened_at)'
            )
        else:
            verb = "INSERT OR REPLACE" if overwrite else "INSERT OR IGNORE"
            sql = f'{verb} INTO "{table}" ({quote(cols)}) VALUES ({",".join("?" * len(cols))})'

        target.execute("BEGIN")
        try:
            target.executemany(sql, [tuple(r) for r in rows])
            target.execute("COMMIT")
        except Exception:
            target.execute("ROLLBACK")
            raise

        changed = target.total_changes - before
        report[table] = {"inserted": changed, "total_in_bundle": len(rows)}

    return report


def reassign_sessions(con: sqlite3.Connection, target_user: str, dry_run: bool = False) -> dict[str, int]:
    """把全部会话（含已删除、含无归属）改写为 target_user，并同步 automations 的 owner。

    故意不动 updated_at / last_activity_at，避免打乱侧边栏顺序。
    """
    result: dict[str, int] = {}

    rows = list(
        con.execute(
            "SELECT user_id, COUNT(*) FROM sessions WHERE user_id IS NULL OR user_id <> ? GROUP BY user_id",
            (target_user,),
        )
    )
    result["sessions"] = sum(r[1] for r in rows)

    auto = 0
    if has_table(con, "automations"):
        auto = con.execute(
            "SELECT COUNT(*) FROM automations WHERE owner_user_id IS NULL OR owner_user_id <> ?",
            (target_user,),
        ).fetchone()[0]
    result["automations"] = auto

    if dry_run:
        return result

    con.execute("BEGIN")
    try:
        con.execute(
            "UPDATE sessions SET user_id = ? WHERE user_id IS NULL OR user_id <> ?",
            (target_user, target_user),
        )
        if has_table(con, "automations"):
            con.execute(
                "UPDATE automations SET owner_user_id = ? WHERE owner_user_id IS NULL OR owner_user_id <> ?",
                (target_user, target_user),
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return result


def keep_only_sessions(con: sqlite3.Connection, session_ids: Sequence[str]) -> None:
    """裁剪数据库：只保留指定会话（用于导出单条会话）。"""
    ids = list(session_ids)
    if not ids:
        con.execute("DELETE FROM sessions")
        con.execute("DELETE FROM session_usage")
        return
    marks = ",".join("?" * len(ids))
    con.execute(f"DELETE FROM sessions WHERE id NOT IN ({marks})", ids)
    if has_table(con, "session_usage"):
        con.execute(f"DELETE FROM session_usage WHERE session_id NOT IN ({marks})", ids)


def clear_sessions(con: sqlite3.Connection) -> int:
    """清空本机会话记录（用于 --replace 整体替换）。"""
    n = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    con.execute("DELETE FROM sessions")
    if has_table(con, "session_usage"):
        con.execute("DELETE FROM session_usage")
    return n


def integrity_ok(con: sqlite3.Connection) -> tuple[bool, str]:
    row = con.execute("PRAGMA integrity_check").fetchone()
    text = row[0] if row else "unknown"
    return text == "ok", text
