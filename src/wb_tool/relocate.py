"""修复 ``projects/<slug>`` 目录名与 ``sessions.cwd`` 不一致的问题。

客户端（``SessionStore``）是用 **cwd 算出的 slug** 去拼正文路径的：
``projects/<slugify(cwd)>/<sessionId>.jsonl``。目录名一旦和 cwd 对不上，
侧边栏还能列出会话（来自数据库），点进去就是"暂无对话记录"——
因为文件按路径找不到，日志里会出现
``deserializeSessionFromPath failed ... ENOENT``。

会撞上的典型场景：

- 先 ``move`` 改了 cwd，但当时正文还没落地（目录是空的），搬不动；
- 导入的包是旧机器导的，slug 按旧 cwd 算，本机导入后又用 ``--remap-path`` 改了 cwd；
- 手工改过数据库 / 手动拷过目录。
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import db as dbmod
from .config import BACKUP_DIR_NAME, default_data_dir, slugify_cwd
from .guard import ensure_app_closed


@dataclass
class Relocation:
    src: Path
    dst: Path
    entries: list[str] = field(default_factory=list)
    whole_dir: bool = False


@dataclass
class RelocateResult:
    checked_dirs: int = 0
    relocations: list[Relocation] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    backup_dir: Optional[Path] = None
    dry_run: bool = False


def _session_ids(directory: Path) -> list[str]:
    out: list[str] = []
    for p in sorted(directory.glob("*.jsonl")):
        if p.name.endswith(".file-rollback.ndjson"):
            continue
        out.append(p.stem)
    return out


def plan(data_dir: Path, con: sqlite3.Connection) -> RelocateResult:
    """算出所有需要搬动的目录，不做任何修改。"""
    result = RelocateResult()
    projects = data_dir / "projects"
    if not projects.is_dir():
        return result

    for d in sorted(p for p in projects.iterdir() if p.is_dir()):
        sids = _session_ids(d)
        if not sids:
            continue
        result.checked_dirs += 1

        targets: dict[str, list[str]] = {}
        for sid in sids:
            row = con.execute("SELECT cwd FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row is None:
                result.unknown.append(sid)
                continue
            targets.setdefault(slugify_cwd(row[0] or ""), []).append(sid)

        if not targets:
            continue
        if len(targets) == 1:
            (slug, owners), = targets.items()
            if slug == d.name:
                continue
            # 整个目录里都是同一个 cwd：目录里没有别家的东西就直接改名
            result.relocations.append(
                Relocation(
                    src=d,
                    dst=projects / slug,
                    entries=[p.name for p in d.iterdir()],
                    whole_dir=True,
                )
            )
            continue

        # 同一个目录里混了不同 cwd 的会话：按会话拆开搬
        for slug, owners in targets.items():
            if slug == d.name:
                continue
            entries: list[str] = []
            for sid in owners:
                for p in d.iterdir():
                    if p.name == sid or p.name.startswith(sid + "."):
                        entries.append(p.name)
            if entries:
                result.relocations.append(
                    Relocation(src=d, dst=projects / slug, entries=entries, whole_dir=False)
                )
    return result


def apply(data_dir: Path, plans: list[Relocation], backup_root: Path, log=print) -> None:
    for rel in plans:
        if rel.whole_dir and not rel.dst.exists():
            try:
                rel.dst.parent.mkdir(parents=True, exist_ok=True)
                rel.src.rename(rel.dst)
                log(f"[move] projects/{rel.src.name} -> projects/{rel.dst.name}")
                continue
            except OSError:
                pass  # 跨卷或被占用，退化成逐个搬

        rel.dst.mkdir(parents=True, exist_ok=True)

        for name in rel.entries:
            s = rel.src / name
            t = rel.dst / name
            if not s.exists():
                continue
            if t.exists():
                b = backup_root / "files" / rel.dst.name / name
                b.parent.mkdir(parents=True, exist_ok=True)
                if t.is_dir():
                    shutil.copytree(t, b, dirs_exist_ok=True)
                    shutil.rmtree(t)
                else:
                    shutil.copy2(t, b)
                    t.unlink()
                log(f"[warn] 目标已有同名条目，已备份并覆盖：{name}")
            shutil.move(str(s), str(t))
            log(f"[move] {name} -> projects/{rel.dst.name}/")

        try:
            if not any(rel.src.iterdir()):
                rel.src.rmdir()
        except OSError:
            pass


def relocate(
    data_dir: Optional[Path] = None,
    force: bool = False,
    dry_run: bool = False,
    log=print,
) -> RelocateResult:
    data_dir = (data_dir or default_data_dir()).resolve()
    db_file = dbmod.db_path(data_dir)
    if not db_file.is_file():
        raise FileNotFoundError(f"{data_dir} 下没有 {dbmod.DB_NAME}")

    if not dry_run:
        ensure_app_closed(data_dir, force=force)

    con = dbmod.connect(db_file, readonly=True)
    try:
        result = plan(data_dir, con)
    finally:
        con.close()
    result.dry_run = dry_run

    if not result.relocations:
        log("[ok]   所有 projects 目录名都与 cwd 一致，无需修复")
        return result

    if dry_run:
        for rel in result.relocations:
            log(f"[dry] projects/{rel.src.name} -> projects/{rel.dst.name}（{len(rel.entries)} 项）")
        return result

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = data_dir / BACKUP_DIR_NAME / stamp
    backup_root.mkdir(parents=True, exist_ok=True)
    result.backup_dir = backup_root
    apply(data_dir, result.relocations, backup_root, log=log)
    return result
