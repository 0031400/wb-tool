"""导入：把 bundle 合并进本机的 WorkBuddy 数据目录。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import db as dbmod
from .config import (
    BACKUP_DIR_NAME,
    default_data_dir,
    remap_cwd,
    slugify_cwd,
)
from .exporter import MANIFEST_NAME, BUNDLE_FORMAT
from .guard import ensure_app_closed

MANIFEST_REQUIRED = {"tool", "format"}


@dataclass
class ImportResult:
    bundle: Path
    data_dir: Path
    db_report: dict[str, dict[str, int]] = field(default_factory=dict)
    copied_files: int = 0
    skipped_files: int = 0
    backup_dir: Optional[Path] = None
    relocated: list[tuple[str, str]] = field(default_factory=list)
    remapped_sessions: int = 0
    reassign: Optional[dict] = None
    warnings: list[str] = field(default_factory=list)


def _looks_like_bundle(path: Path) -> bool:
    return (path / MANIFEST_NAME).is_file()


def _resolve_bundle(path: Path, workdir: Path) -> Path:
    """支持目录、bundle 目录的 zip。"""
    if path.is_dir():
        if _looks_like_bundle(path):
            return path
        # 可能是 bundle 的父目录
        for child in sorted(path.iterdir()):
            if child.is_dir() and _looks_like_bundle(child):
                return child
        raise FileNotFoundError(f"{path} 下没有 {MANIFEST_NAME}，不是有效的迁移包")

    if path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            zf.extractall(workdir)
        if _looks_like_bundle(workdir):
            return workdir
        for child in sorted(workdir.iterdir()):
            if child.is_dir() and _looks_like_bundle(child):
                return child
        raise FileNotFoundError(f"{path} 解压后没有 {MANIFEST_NAME}")

    raise FileNotFoundError(f"找不到迁移包：{path}")


def read_manifest(bundle: Path) -> dict:
    data = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
    missing = MANIFEST_REQUIRED - set(data)
    if missing:
        raise ValueError(f"manifest 缺少字段：{sorted(missing)}")
    if int(data.get("format", 0)) > BUNDLE_FORMAT:
        raise ValueError(
            f"迁移包格式 v{data['format']} 比当前工具（v{BUNDLE_FORMAT}）更新，请升级 wd-move"
        )
    return data


def _copy_tree(
    src: Path,
    dst: Path,
    data_dir: Path,
    overwrite: bool,
    backup_root: Path,
) -> tuple[int, int]:
    copied = skipped = 0
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            s = Path(root) / name
            t = target_dir / name
            if t.exists():
                if not overwrite:
                    skipped += 1
                    continue
                try:
                    b = backup_root / "files" / t.relative_to(data_dir)
                except ValueError:
                    b = backup_root / "files" / name
                b.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(t, b)
            shutil.copy2(s, t)
            copied += 1
    return copied, skipped


def apply_remap(con: sqlite3.Connection, mappings: list[tuple[str, str]]) -> int:
    """按前缀改写 sessions.cwd 与 workspaces.path。"""
    if not mappings:
        return 0
    changed = 0
    for row in list(con.execute("SELECT id, cwd FROM sessions")):
        new = remap_cwd(row[1], mappings)
        if new != row[1]:
            con.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (new, row[0]))
            changed += 1
    for row in list(con.execute("SELECT path, last_opened_at FROM workspaces")):
        new = remap_cwd(row[0], mappings)
        if new and new != row[0]:
            con.execute(
                "INSERT INTO workspaces(path, last_opened_at) VALUES(?, ?) "
                "ON CONFLICT(path) DO UPDATE SET last_opened_at = MAX(workspaces.last_opened_at, excluded.last_opened_at)",
                (new, row[1]),
            )
            con.execute("DELETE FROM workspaces WHERE path = ?", (row[0],))
            changed += 1
    return changed


def relocate_project_dirs(data_dir: Path, con: sqlite3.Connection) -> list[tuple[str, str]]:
    """cwd 变了之后，把 ``projects/<slug>`` 目录改成新的 slug。"""
    projects = data_dir / "projects"
    if not projects.is_dir():
        return []
    renamed: list[tuple[str, str]] = []
    for d in sorted(p for p in projects.iterdir() if p.is_dir()):
        sids = [f.stem for f in d.glob("*.jsonl")]
        if not sids:
            continue
        row = con.execute("SELECT cwd FROM sessions WHERE id = ?", (sids[0],)).fetchone()
        if not row or not row[0]:
            continue
        expected = slugify_cwd(row[0])
        if expected == d.name:
            continue
        target = projects / expected
        if target.exists():
            shutil.copytree(d, target, dirs_exist_ok=True, copy_function=shutil.copy2)
            shutil.rmtree(d)
        else:
            d.rename(target)
        renamed.append((d.name, expected))
    return renamed


def import_bundle(
    bundle_path: Path,
    data_dir: Optional[Path] = None,
    overwrite: bool = False,
    overwrite_db: bool = False,
    replace: bool = False,
    remap: Optional[list[tuple[str, str]]] = None,
    account: Optional[str] = None,
    to_current: bool = False,
    merge_memory: bool = False,
    force: bool = False,
    dry_run: bool = False,
    log=print,
) -> ImportResult:
    data_dir = (data_dir or default_data_dir()).resolve()
    bundle_path = Path(bundle_path).expanduser().resolve()
    remap = remap or []

    tmp_dir: Optional[Path] = None
    if bundle_path.is_file():
        tmp_dir = Path(tempfile.mkdtemp(prefix="wd-move-"))
        bundle = _resolve_bundle(bundle_path, tmp_dir)
    else:
        bundle = _resolve_bundle(bundle_path, data_dir)

    try:
        manifest = read_manifest(bundle)
        src_info = manifest.get("source", {})
        log(f"[info] 迁移包来自 {src_info.get('hostname', '?')} / {src_info.get('platform', '?')}")
        log(
            f"[info] 应用版本 {src_info.get('app_version', '?')}，"
            f"会话 {manifest.get('sessions', {}).get('total', 0)} 条，"
            f"模式 {manifest.get('mode', '?')}"
        )

        procs = ensure_app_closed(data_dir, force=force)
        if procs:
            log("[warn] --force 已跳过客户端运行检查，数据可能不一致")

        result = ImportResult(bundle=bundle, data_dir=data_dir)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_root = data_dir / BACKUP_DIR_NAME / stamp
        result.backup_dir = backup_root

        data_dir.mkdir(parents=True, exist_ok=True)
        target_db = dbmod.db_path(data_dir)
        bundle_db = bundle / "db" / dbmod.DB_NAME

        # 1) 数据库
        if not target_db.exists():
            if not bundle_db.is_file():
                raise FileNotFoundError("迁移包里没有 db/workbuddy.db")
            if dry_run:
                log("[dry] 本机没有数据库，将直接使用迁移包中的 workbuddy.db")
            else:
                backup_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundle_db, target_db)
                log("[db]   本机无数据库，已直接落位")
        else:
            if dry_run:
                log("[dry] 跳过数据库合并")
            else:
                backup_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target_db, backup_root / dbmod.DB_NAME)
                log(f"[db]   已备份原库 -> {backup_root / dbmod.DB_NAME}")

        if bundle_db.is_file() and not dry_run:
            tcon = dbmod.connect(target_db)
            scon = dbmod.connect(bundle_db, readonly=True)
            try:
                if replace:
                    cleared = dbmod.clear_sessions(tcon)
                    log(f"[db]   已清空本机会话记录 {cleared} 条（--replace）")
                result.db_report = dbmod.merge_tables(
                    tcon, scon, overwrite=overwrite_db or replace
                )
                for table, rep in result.db_report.items():
                    if rep.get("inserted"):
                        log(f"[db]   {table:<26} 新增 {rep['inserted']} 行")
                if remap:
                    result.remapped_sessions = apply_remap(tcon, remap)
                    if result.remapped_sessions:
                        log(f"[db]   路径重映射 {result.remapped_sessions} 处")
            finally:
                tcon.close()
                scon.close()

        # 2) 文件目录（按 bundle 顶层目录逐个合并，兼容整包与单会话包）
        for entry in sorted(bundle.iterdir()):
            if entry.name in {"db", MANIFEST_NAME} or not entry.is_dir():
                continue
            name = entry.name
            dst = data_dir / name
            if dry_run:
                log(f"[dry] 将合并目录 {name}")
                continue
            copied, skipped = _copy_tree(entry, dst, data_dir, overwrite, backup_root)
            result.copied_files += copied
            result.skipped_files += skipped
            log(f"[dir]  {name:<22} 复制 {copied:>6}  跳过 {skipped:>6}")

        # 3) cwd 变更后修正 projects 目录名
        if not dry_run and (remap or result.remapped_sessions):
            tcon = dbmod.connect(target_db)
            try:
                result.relocated = relocate_project_dirs(data_dir, tcon)
            finally:
                tcon.close()
            for old, new in result.relocated:
                log(f"[move] projects/{old} -> projects/{new}")

        # 4) 改写会话归属账号
        if account or to_current:
            from .reassign import reassign

            log("[acc]  开始改写会话归属账号 ...")
            result.reassign = reassign(
                data_dir=data_dir,
                account=account,
                merge_memory=merge_memory,
                force=force,
                dry_run=dry_run,
                assume_yes=True,
                backup_root=backup_root,
                log=log,
            )

        return result
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
