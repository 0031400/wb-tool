"""改单个会话的工作目录（cwd）。

一次改动涉及四处，缺一处客户端就会"看不见"或读不到正文：

1. ``workbuddy.db`` → ``sessions.cwd``：界面显示的工作目录，权威值；
2. ``projects/<cwd-slug>/``：会话正文 ``<sid>.jsonl`` 及其附属文件所在目录；
3. ``workspaces.path``：最近打开的工作区列表；
4. ``<accountId>/sidebar-list-snapshot.json`` → ``item.space.name``：侧边栏里的工作区标签。

正文 jsonl 与各类索引里残留的旧绝对路径属于**历史记录**（当时确实操作过那个文件），不改写。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import db as dbmod
from .config import BACKUP_DIR_NAME, default_data_dir, normalize_cwd, slugify_cwd
from .guard import ensure_app_closed
from .sidebar import update_session_space


@dataclass
class ProjectMove:
    """``projects/`` 目录层面的搬运计划。"""

    mode: str = "none"  # none | rename | move
    src_dir: Optional[Path] = None
    dst_dir: Optional[Path] = None
    entries: list[str] = field(default_factory=list)
    siblings: int = 0


@dataclass
class MoveResult:
    session_id: str
    title: str
    account: str
    old_cwd: str
    new_cwd: str
    plan: ProjectMove = field(default_factory=ProjectMove)
    db_updated: bool = False
    workspace_added: bool = False
    snapshot_paths: list[Path] = field(default_factory=list)
    backup_dir: Optional[Path] = None
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)


def normalize_target(raw: str) -> str:
    """把用户输入统一成数据库里的形式：绝对路径 + 正斜杠 + 无尾斜杠。"""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("新的工作目录不能为空")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    s = str(path.resolve()).replace("\\", "/")
    if len(s) > 3:  # 保留 "C:/" 这类盘符根
        s = s.rstrip("/")
    return s


def _resolve_session(con: sqlite3.Connection, key: str) -> sqlite3.Row:
    key = (key or "").strip()
    if not key:
        raise ValueError("需要提供会话 UUID")
    row = con.execute(
        "SELECT id, cwd, title, custom_title, user_id, deleted_at FROM sessions WHERE id = ?", (key,)
    ).fetchone()
    if row:
        return row
    rows = list(
        con.execute(
            "SELECT id, cwd, title, custom_title, user_id, deleted_at FROM sessions WHERE id LIKE ?",
            (key + "%",),
        )
    )
    if not rows:
        raise LookupError(f"找不到会话：{key}")
    if len(rows) > 1:
        cand = "\n  ".join(f"{r['id']}  {r['title'] or ''}" for r in rows)
        raise LookupError(f"前缀 {key} 匹配到 {len(rows)} 个会话，请写完整 UUID：\n  {cand}")
    return rows[0]


def _session_entries(directory: Path, sid: str) -> list[Path]:
    """目录里属于该会话的条目：``<sid>.jsonl``、``<sid>.file-rollback.ndjson``、``<sid>/`` 等。"""
    if not directory.is_dir():
        return []
    out = []
    for p in sorted(directory.iterdir()):
        name = p.name
        if name == sid or name.startswith(sid + "."):
            out.append(p)
    return out


def plan_project_move(data_dir: Path, sid: str, old_cwd: str, new_cwd: str) -> ProjectMove:
    """决定 ``projects/`` 下要改名整个目录，还是只搬这一个会话的文件。"""
    projects = data_dir / "projects"
    plan = ProjectMove()

    old_slug = slugify_cwd(old_cwd) if old_cwd else ""
    new_slug = slugify_cwd(new_cwd)
    src = projects / old_slug if old_slug else None
    entries = _session_entries(src, sid) if src else []

    # cwd 为空、或正文不在预期目录里：全盘找一遍
    if not entries and projects.is_dir():
        for d in sorted(p for p in projects.iterdir() if p.is_dir()):
            found = _session_entries(d, sid)
            if found:
                src, entries = d, found
                break

    if not entries:
        # 没有正文文件（例如纯云端会话），只改数据库
        return plan

    plan.src_dir = src
    plan.dst_dir = projects / new_slug
    plan.entries = [p.name for p in entries]

    others = [p for p in src.iterdir() if p.name not in set(plan.entries)] if src else []
    plan.siblings = len(others)
    plan.mode = "rename" if not others and new_slug != src.name else "move"
    if new_slug == src.name:
        plan.mode = "none"
    return plan


def _move_entries(plan: ProjectMove, backup_root: Path, log=print) -> None:
    assert plan.src_dir and plan.dst_dir

    if plan.mode == "rename":
        if not plan.dst_dir.exists():
            plan.src_dir.rename(plan.dst_dir)
            log(f"[move] projects/{plan.src_dir.name} -> projects/{plan.dst_dir.name}")
            return
        # 目标目录已存在：合并后删掉旧目录
        shutil.copytree(plan.src_dir, plan.dst_dir, dirs_exist_ok=True, copy_function=shutil.copy2)
        shutil.rmtree(plan.src_dir)
        log(f"[move] projects/{plan.src_dir.name} 并入 projects/{plan.dst_dir.name}")
        return

    plan.dst_dir.mkdir(parents=True, exist_ok=True)

    for name in plan.entries:
        s = plan.src_dir / name
        t = plan.dst_dir / name
        if t.exists():
            b = backup_root / "files" / t.name
            b.parent.mkdir(parents=True, exist_ok=True)
            if t.is_dir():
                shutil.copytree(t, b, dirs_exist_ok=True)
            else:
                shutil.copy2(t, b)
            if t.is_dir():
                shutil.rmtree(t)
            else:
                t.unlink()
            log(f"[warn] 目标已存在同名条目，已备份并覆盖：{name}")
        shutil.move(str(s), str(t))
        log(f"[move] {name} -> projects/{plan.dst_dir.name}/")

    try:
        if not any(plan.src_dir.iterdir()):
            plan.src_dir.rmdir()
    except OSError:
        pass


def move_session(
    session: str,
    new_cwd: str,
    data_dir: Optional[Path] = None,
    force: bool = False,
    dry_run: bool = False,
    add_workspace: bool = True,
    log=print,
) -> MoveResult:
    data_dir = (data_dir or default_data_dir()).resolve()
    db_file = dbmod.db_path(data_dir)
    if not db_file.is_file():
        raise FileNotFoundError(f"{data_dir} 下没有 {dbmod.DB_NAME}")

    target = normalize_target(new_cwd)

    if not dry_run:
        ensure_app_closed(data_dir, force=force)

    con = dbmod.connect(db_file, readonly=dry_run)
    try:
        ok, msg = dbmod.integrity_ok(con)
        if not ok:
            raise RuntimeError(f"数据库完整性检查失败：{msg}")

        row = _resolve_session(con, session)
        sid = row["id"]
        old_cwd = normalize_cwd(row["cwd"] or "")
        result = MoveResult(
            session_id=sid,
            title=row["custom_title"] or row["title"] or "(无标题)",
            account=row["user_id"] or "",
            old_cwd=old_cwd,
            new_cwd=target,
            dry_run=dry_run,
        )
        if row["deleted_at"] is not None:
            result.warnings.append("该会话已被删除（回收站），改动只影响数据库记录")

        if old_cwd and old_cwd.lower() == target.lower():
            result.warnings.append("新旧目录相同，无需改动")
            return result

        if not Path(target).is_dir():
            result.warnings.append(f"目标目录当前不存在：{target}（客户端打开时可能报错）")

        result.plan = plan_project_move(data_dir, sid, old_cwd, target)

        log(f"会话：[bold]{result.title}[/bold]（{sid}）")
        log(f"目录：{old_cwd or '(空)'}  ->  {target}")
        if result.plan.mode == "rename":
            log(f"正文：整个 projects/{result.plan.src_dir.name} 改名（该目录只含这一个会话）")
        elif result.plan.mode == "move":
            log(
                f"正文：搬 {len(result.plan.entries)} 个条目到 projects/{result.plan.dst_dir.name}"
                f"（该目录还有 {result.plan.siblings} 个其它会话的条目，保持原地）"
            )
        else:
            log("正文：无需搬动")
            if result.plan.src_dir is None:
                result.warnings.append(
                    "projects/ 下没找到这个会话的 jsonl（可能正文还没落地），本次只改了数据库；"
                    "之后正文进来时请再执行一次 wb-tool relocate 对齐目录名"
                )

        if dry_run:
            log("[dry] 未做任何修改")
            return result

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_root = data_dir / BACKUP_DIR_NAME / stamp
        backup_root.mkdir(parents=True, exist_ok=True)
        result.backup_dir = backup_root
        shutil.copy2(db_file, backup_root / f"{dbmod.DB_NAME}.before-move")
        log(f"[db]   已备份 -> {backup_root / f'{dbmod.DB_NAME}.before-move'}")

        if result.plan.mode in ("rename", "move"):
            _move_entries(result.plan, backup_root, log=log)

        now_ms = int(time.time() * 1000)
        con.execute("BEGIN")
        try:
            con.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (target, sid))
            if add_workspace and dbmod.has_table(con, "workspaces"):
                before = con.total_changes
                con.execute(
                    "INSERT INTO workspaces(path, last_opened_at) VALUES(?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET last_opened_at = "
                    "MAX(workspaces.last_opened_at, excluded.last_opened_at)",
                    (target, now_ms),
                )
                result.workspace_added = con.total_changes > before
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        result.db_updated = True

        result.snapshot_paths = update_session_space(data_dir, sid, target, backup_root)
        for p in result.snapshot_paths:
            log(f"[acc]  侧边栏快照已更新：{p}")

        (backup_root / f"move-{sid}.json").write_text(
            json.dumps(
                {
                    "session": sid,
                    "old_cwd": old_cwd,
                    "new_cwd": target,
                    "project_mode": result.plan.mode,
                    "project_src": str(result.plan.src_dir or ""),
                    "project_dst": str(result.plan.dst_dir or ""),
                    "entries": result.plan.entries,
                    "at": datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        con.close()

    return result
