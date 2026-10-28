"""会话列表：把数据库里的会话与磁盘上的文件对应起来。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import db as dbmod
from .config import default_data_dir

ID_JSON_DIRS = ("artifact-index", "changes-index", "file-tree-manifests")
ID_DIRS = ("changes-detail", "file-history")


@dataclass
class SessionRow:
    id: str
    title: str
    account: str
    cwd: str
    updated_at: int
    status: str
    files: int = 0
    bytes: int = 0
    path: str = ""
    deleted: bool = False


def size_index(data_dir: Path) -> dict[str, tuple[int, int, str]]:
    """sessionId -> (文件数, 字节数, 正文路径)。只走一遍目录。"""
    idx: dict[str, list] = {}

    def add(sid: str, files: int, size: int, path: str = "") -> None:
        cur = idx.setdefault(sid, [0, 0, ""])
        cur[0] += files
        cur[1] += size
        if path and not cur[2]:
            cur[2] = path

    projects = data_dir / "projects"
    if projects.is_dir():
        for pd in projects.iterdir():
            if not pd.is_dir():
                continue
            for entry in pd.iterdir():
                name = entry.name
                if entry.is_file() and name.endswith(".jsonl"):
                    sid = name[: -len(".jsonl")]
                    try:
                        add(sid, 1, entry.stat().st_size, str(entry))
                    except OSError:
                        pass
                elif entry.is_dir():
                    files = size = 0
                    for root, _d, names in os.walk(entry):
                        for n in names:
                            try:
                                size += (Path(root) / n).stat().st_size
                                files += 1
                            except OSError:
                                pass
                    add(entry.name, files, size)

    for name in ID_JSON_DIRS:
        d = data_dir / name
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            try:
                add(p.stem, 1, p.stat().st_size)
            except OSError:
                pass

    for name in ID_DIRS:
        d = data_dir / name
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not p.is_dir():
                continue
            files = size = 0
            for root, _d, names in os.walk(p):
                for n in names:
                    try:
                        size += (Path(root) / n).stat().st_size
                        files += 1
                    except OSError:
                        pass
            add(p.name, files, size)

    return {sid: (v[0], v[1], v[2]) for sid, v in idx.items()}


def list_sessions(
    data_dir: Optional[Path] = None,
    account: Optional[str] = None,
    include_deleted: bool = False,
    limit: Optional[int] = None,
    with_size: bool = True,
) -> list[SessionRow]:
    data_dir = (data_dir or default_data_dir()).resolve()
    db_file = dbmod.db_path(data_dir)
    if not db_file.is_file():
        raise FileNotFoundError(f"{data_dir} 下没有 {dbmod.DB_NAME}")

    sql = "SELECT id, title, custom_title, user_id, cwd, status, updated_at, last_activity_at, deleted_at FROM sessions"
    params: list = []
    where = []
    if account:
        where.append("user_id = ?")
        params.append(account.strip().lower())
    if not include_deleted:
        where.append("deleted_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(last_activity_at, updated_at) DESC"

    con = dbmod.connect(db_file, readonly=True)
    try:
        rows = list(con.execute(sql, params))
    finally:
        con.close()

    if limit:
        rows = rows[:limit]

    idx = size_index(data_dir) if with_size else {}
    out: list[SessionRow] = []
    for r in rows:
        files, size, path = idx.get(r["id"], (0, 0, ""))
        out.append(
            SessionRow(
                id=r["id"],
                title=r["custom_title"] or r["title"] or "(无标题)",
                account=r["user_id"] or "",
                cwd=r["cwd"] or "",
                updated_at=r["last_activity_at"] or r["updated_at"] or 0,
                status=r["status"] or "",
                files=files,
                bytes=size,
                path=path,
                deleted=r["deleted_at"] is not None,
            )
        )
    return out


def session_detail(data_dir: Path, session_id: str) -> Optional[SessionRow]:
    rows = list_sessions(data_dir, include_deleted=True, with_size=True)
    for r in rows:
        if r.id == session_id:
            return r
    return None
