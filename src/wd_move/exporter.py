"""导出：把会话数据打包成一个可搬运的 bundle 目录（可选再压成 zip）。

支持两种粒度：
- 全部会话（默认）
- 指定会话（``--session <uuid>``，可多次指定）
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from . import accounts as acc
from . import db as dbmod
from .config import (
    CORE_DIRS,
    OPTIONAL_DIRS,
    account_dirs,
    default_data_dir,
    human_bytes,
)

MANIFEST_NAME = "manifest.json"
BUNDLE_FORMAT = 1

#: 轻量模式下仍然包含的目录
LITE_DIRS = ("projects", "memory", "audit-log")

#: 以 ``<sessionId>/`` 目录形式存放的索引
ID_DIRS = ("changes-detail", "file-history")
#: 以 ``<sessionId>.json`` 形式存放的索引
ID_JSON_DIRS = ("artifact-index", "changes-index", "file-tree-manifests")


@dataclass
class ItemStat:
    name: str
    kind: str
    files: int = 0
    bytes: int = 0
    sha256: str = ""

    def as_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "files": self.files, "bytes": self.bytes}
        if self.sha256:
            d["sha256"] = self.sha256
        return d


@dataclass
class ExportResult:
    bundle: Path
    archive: Optional[Path]
    manifest: dict
    items: list[ItemStat] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(i.bytes for i in self.items)

    @property
    def total_files(self) -> int:
        return sum(i.files for i in self.items)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_stat(path: Path) -> tuple[int, int]:
    files = size = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            fp = Path(root) / n
            try:
                size += fp.stat().st_size
                files += 1
            except OSError:
                pass
    return files, size


def app_version(data_dir: Path) -> str:
    f = data_dir / "last-launch.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return f"{data.get('version', '?')} ({str(data.get('build', ''))[:8]})"
    except (OSError, ValueError):
        return "unknown"


def select_dirs(lite: bool, with_optional: bool) -> list[str]:
    dirs = list(LITE_DIRS) if lite else list(CORE_DIRS)
    if with_optional:
        dirs += [d for d in OPTIONAL_DIRS if d not in dirs]
    return dirs


def default_bundle_path(out: Optional[str], stamp: Optional[str] = None) -> Path:
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    if out:
        p = Path(out).expanduser()
        return p.with_suffix("") if p.suffix.lower() == ".zip" else p
    return Path.cwd() / f"wb-migration-{stamp}"


def session_artifacts(data_dir: Path, session_id: str) -> list[Path]:
    """一条会话在磁盘上散落的所有文件/目录。"""
    out: list[Path] = []
    projects = data_dir / "projects"
    if projects.is_dir():
        for pd in projects.iterdir():
            if not pd.is_dir():
                continue
            for p in (pd / f"{session_id}.jsonl", pd / f"{session_id}.file-rollback.ndjson"):
                if p.is_file():
                    out.append(p)
            d = pd / session_id
            if d.is_dir():
                out.append(d)
    for name in ID_JSON_DIRS:
        p = data_dir / name / f"{session_id}.json"
        if p.is_file():
            out.append(p)
    for name in ID_DIRS:
        d = data_dir / name / session_id
        if d.is_dir():
            out.append(d)
    return out


def export(
    data_dir: Optional[Path] = None,
    out: Optional[str] = None,
    lite: bool = False,
    with_optional: bool = False,
    make_zip: bool = False,
    overwrite: bool = False,
    session_ids: Optional[Sequence[str]] = None,
    log=print,
) -> ExportResult:
    data_dir = (data_dir or default_data_dir()).resolve()
    db_file = dbmod.db_path(data_dir)
    if not db_file.is_file():
        raise FileNotFoundError(f"{data_dir} 下没有 {dbmod.DB_NAME}，请确认 --data-dir 是否正确")

    only = [s.strip().lower() for s in (session_ids or [])]

    # 校验会话是否存在
    if only:
        con = dbmod.connect(db_file, readonly=True)
        try:
            known = {r[0] for r in con.execute("SELECT id FROM sessions")}
        finally:
            con.close()
        missing = [s for s in only if s not in known]
        if missing:
            raise ValueError("这些会话在本机不存在：" + ", ".join(missing))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if out:
        bundle = default_bundle_path(out)
    elif only:
        bundle = Path.cwd() / f"wb-session-{only[0][:8]}-{stamp}"
    else:
        bundle = default_bundle_path(None, stamp)

    if bundle.exists() and any(bundle.iterdir()):
        if not overwrite:
            raise FileExistsError(f"目标目录已存在且非空：{bundle}（加 --overwrite 覆盖）")
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True, exist_ok=True)

    items: list[ItemStat] = []

    # 1) 数据库一致性快照
    dst_db = bundle / "db" / dbmod.DB_NAME
    dbmod.snapshot_db(db_file, dst_db)
    if only:
        fcon = dbmod.connect(dst_db)
        try:
            dbmod.keep_only_sessions(fcon, only)
        finally:
            fcon.close()
    items.append(ItemStat("db/" + dbmod.DB_NAME, "db", 1, dst_db.stat().st_size, _sha256(dst_db)))
    log(f"[db]   快照 {dbmod.DB_NAME} -> {human_bytes(dst_db.stat().st_size)}")

    # 2) 文件
    if only:
        copied = 0
        size = 0
        for sid in only:
            for src in session_artifacts(data_dir, sid):
                rel = src.relative_to(data_dir)
                dst = bundle / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
                else:
                    shutil.copy2(src, dst)
                f, s = _dir_stat(src) if src.is_dir() else (1, src.stat().st_size)
                copied += f
                size += s
        items.append(ItemStat(f"{len(only)} 个会话", "sessions", copied, size))
        log(f"[sess] {len(only)} 条会话  {copied} 个文件  {human_bytes(size)}")
    else:
        for name in select_dirs(lite, with_optional):
            src = data_dir / name
            if not src.is_dir():
                continue
            dst = bundle / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
            files, size = _dir_stat(dst)
            items.append(ItemStat(name, "dir", files, size))
            log(f"[dir]  {name:<22} {files:>6} 个文件  {human_bytes(size)}")

        for d in account_dirs(data_dir):
            dst = bundle / d.name
            shutil.copytree(d, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
            files, size = _dir_stat(dst)
            items.append(ItemStat(d.name, "account-dir", files, size))
            log(f"[acc]  {d.name}  {files} 个文件  {human_bytes(size)}")

    # 3) manifest
    accounts: dict[str, dict] = {}
    con = dbmod.connect(db_file, readonly=True)
    try:
        counts = dbmod.session_counts(con)
        total_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        deleted = con.execute("SELECT COUNT(*) FROM sessions WHERE deleted_at IS NOT NULL").fetchone()[0]
        for uid, cnt in counts.items():
            accounts[uid or "(未归属)"] = {"sessions": cnt}
        titles = {
            r[0]: {"title": r[1], "cwd": r[2]}
            for r in con.execute("SELECT id, title, cwd FROM sessions")
            if only and r[0] in only
        }
    finally:
        con.close()

    current = acc.current_account(data_dir)
    manifest = {
        "tool": "wd-move",
        "tool_version": __version__,
        "format": BUNDLE_FORMAT,
        "kind": "sessions" if only else "all",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "hostname": socket.gethostname(),
            "platform": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "data_dir": str(data_dir),
            "app_version": app_version(data_dir),
        },
        "sessions": {
            "total": len(only) if only else total_sessions,
            "deleted": 0 if only else deleted,
            "by_account": accounts,
        },
        "session_ids": only,
        "session_titles": titles,
        "current_account": current.account_id if current else None,
        "mode": "sessions" if only else ("lite" if lite else "full"),
        "with_optional": with_optional,
        "items": [i.as_dict() for i in items],
    }
    (bundle / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"[ok]   bundle 已生成：{bundle}")

    archive: Optional[Path] = None
    if make_zip:
        archive = Path(shutil.make_archive(str(bundle), "zip", root_dir=bundle.parent, base_dir=bundle.name))
        log(f"[ok]   压缩包：{archive}  {human_bytes(archive.stat().st_size)}")

    return ExportResult(bundle=bundle, archive=archive, manifest=manifest, items=items, session_ids=only)
