"""把全部会话的归属账号改写为指定账号（默认：最新登录账号）。"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import accounts as acc
from . import db as dbmod
from .config import BACKUP_DIR_NAME, default_data_dir
from .guard import ensure_app_closed
from .sidebar import write_snapshot


@dataclass
class ReassignResult:
    target: str
    source: str
    sessions_changed: int = 0
    automations_changed: int = 0
    snapshot_path: Optional[Path] = None
    snapshot_items: int = 0
    backup: Optional[Path] = None
    memory_merged: bool = False
    before: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False


def _merge_memory(data_dir: Path, target: str, backup_root: Path) -> bool:
    """把其它账号的 memory md 里独有的行并入目标账号的 memory。"""
    mem_dir = data_dir / "memory"
    if not mem_dir.is_dir():
        return False
    target_file = mem_dir / f"{target}_memory.md"
    others = [f for f in mem_dir.glob("*_memory.md") if f.stem.split("_")[0].lower() != target.lower()]
    if not others:
        return False

    existing = target_file.read_text(encoding="utf-8", errors="replace") if target_file.is_file() else ""
    seen = {line.strip() for line in existing.splitlines() if line.strip()}
    extra: list[str] = []
    for f in others:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s in seen:
                continue
            seen.add(s)
            extra.append(s)
    if not extra:
        return False

    backup_root.mkdir(parents=True, exist_ok=True)
    if target_file.is_file():
        shutil.copy2(target_file, backup_root / target_file.name)
    block = ["", f"<!-- merged by wb-tool {datetime.now():%Y-%m-%d %H:%M:%S} -->", *extra, ""]
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(existing.rstrip("\n") + "\n" + "\n".join(block), encoding="utf-8")
    return True


def reassign(
    data_dir: Optional[Path] = None,
    account: Optional[str] = None,
    merge_memory: bool = False,
    force: bool = False,
    dry_run: bool = False,
    assume_yes: bool = False,
    backup_root: Optional[Path] = None,
    log=print,
) -> ReassignResult:
    data_dir = (data_dir or default_data_dir()).resolve()
    db_file = dbmod.db_path(data_dir)
    if not db_file.is_file():
        raise FileNotFoundError(f"{data_dir} 下没有 {dbmod.DB_NAME}")

    target, source = acc.resolve_target(data_dir, account)
    result = ReassignResult(target=target, source=source, dry_run=dry_run)

    if not dry_run:
        ensure_app_closed(data_dir, force=force)

    con = dbmod.connect(db_file, readonly=dry_run)
    try:
        ok, msg = dbmod.integrity_ok(con)
        if not ok:
            raise RuntimeError(f"数据库完整性检查失败：{msg}")

        result.before = dbmod.session_counts(con, include_deleted=True)

        if dry_run:
            changed = dbmod.reassign_sessions(con, target, dry_run=True)
            result.sessions_changed = changed["sessions"]
            result.automations_changed = changed["automations"]
            log(f"[dry] 将把 {result.sessions_changed} 条会话归属改为 {target}")
            return result

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_root = backup_root or (data_dir / BACKUP_DIR_NAME / stamp)
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / f"{dbmod.DB_NAME}.before-reassign"
        shutil.copy2(db_file, backup)
        result.backup = backup
        log(f"[db]   已备份 -> {backup}")

        changed = dbmod.reassign_sessions(con, target, dry_run=False)
        result.sessions_changed = changed["sessions"]
        result.automations_changed = changed["automations"]

        path, count = write_snapshot(data_dir, target, con, dry_run=False, backup_root=backup_root)
        result.snapshot_path = path
        result.snapshot_items = count
        log(f"[acc]  侧边栏快照已重建：{path}（{count} 条）")

        if merge_memory:
            result.memory_merged = _merge_memory(data_dir, target, backup_root)
            log(f"[acc]  账号记忆{'已合并' if result.memory_merged else '无需合并'}")
    finally:
        con.close()

    return result
