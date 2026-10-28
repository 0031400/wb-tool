"""体检：检查会话文件与数据库是否对得上。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import accounts as acc
from . import db as dbmod
from .config import SIDEBAR_SNAPSHOT, default_data_dir, slugify_cwd


@dataclass
class VerifyReport:
    data_dir: Path
    db_ok: bool = False
    db_message: str = ""
    total_sessions: int = 0
    by_account: dict[str, int] = field(default_factory=dict)
    missing_files: list[str] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    misplaced_files: list[str] = field(default_factory=list)
    latest_account: Optional[str] = None
    latest_source: str = ""
    snapshot_items: Optional[int] = None
    problems: list[str] = field(default_factory=list)


def _session_files(data_dir: Path) -> dict[str, Path]:
    """sessionId -> jsonl 路径。"""
    out: dict[str, Path] = {}
    projects = data_dir / "projects"
    if not projects.is_dir():
        return out
    for p in projects.rglob("*.jsonl"):
        if p.name.endswith(".file-rollback.ndjson"):
            continue
        out.setdefault(p.stem, p)
    return out


def verify(data_dir: Optional[Path] = None, log=print) -> VerifyReport:
    data_dir = (data_dir or default_data_dir()).resolve()
    rep = VerifyReport(data_dir=data_dir)
    db_file = dbmod.db_path(data_dir)
    if not db_file.is_file():
        rep.problems.append(f"缺少 {dbmod.DB_NAME}")
        return rep

    con = dbmod.connect(db_file, readonly=True)
    try:
        rep.db_ok, rep.db_message = dbmod.integrity_ok(con)
        if not rep.db_ok:
            rep.problems.append(f"数据库完整性检查失败：{rep.db_message}")

        rep.by_account = dbmod.session_counts(con)
        rep.total_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        db_ids = {r[0] for r in con.execute("SELECT id FROM sessions")}
        cwd_of = {r[0]: (r[1] or "") for r in con.execute("SELECT id, cwd FROM sessions")}
    finally:
        con.close()

    files = _session_files(data_dir)
    rep.missing_files = sorted(sid for sid in db_ids if sid not in files)
    rep.orphan_files = sorted(sid for sid in files if sid not in db_ids)

    # 正文所在目录名必须等于 slugify(cwd)，否则客户端按路径找不到，界面显示"暂无对话记录"
    for sid, path in files.items():
        cwd = cwd_of.get(sid)
        if cwd is None:
            continue
        if path.parent.name != slugify_cwd(cwd):
            rep.misplaced_files.append(f"{sid} 在 projects/{path.parent.name}，cwd={cwd}")

    hit = acc.latest_account(data_dir)
    if hit:
        rep.latest_account = hit.account_id
        rep.latest_source = hit.source
        snap = data_dir / hit.account_id / SIDEBAR_SNAPSHOT
        if snap.is_file():
            try:
                data = json.loads(snap.read_text(encoding="utf-8", errors="replace"))
                rep.snapshot_items = len(data.get("items", []))
            except (OSError, ValueError):
                rep.problems.append("侧边栏快照无法解析")

    if rep.missing_files:
        rep.problems.append(f"{len(rep.missing_files)} 条会话缺少 jsonl 正文")
    if rep.orphan_files:
        rep.problems.append(f"{len(rep.orphan_files)} 个 jsonl 在数据库里没有记录")
    if rep.misplaced_files:
        rep.problems.append(
            f"{len(rep.misplaced_files)} 条会话的正文目录名与 cwd 对不上（界面会显示“暂无对话记录”），"
            "执行 wb-tool relocate 修复"
        )
    if len(rep.by_account) > 1:
        rep.problems.append(f"会话分散在 {len(rep.by_account)} 个账号下")

    return rep


def describe(rep: VerifyReport) -> str:
    lines = [f"数据目录：{rep.data_dir}"]
    lines.append(f"数据库：{'OK' if rep.db_ok else '异常'}（{rep.db_message}）")
    lines.append(f"会话总数：{rep.total_sessions}")
    for uid, cnt in sorted(rep.by_account.items(), key=lambda kv: -kv[1]):
        lines.append(f"  - {uid or '(未归属)'}  {cnt} 条")
    if rep.latest_account:
        lines.append(
            f"当前登录账号：{rep.latest_account}"
            f"（来源 {acc.SOURCE_LABEL.get(rep.latest_source, rep.latest_source)}）"
        )
    if rep.snapshot_items is not None:
        lines.append(f"该账号侧边栏条目：{rep.snapshot_items}")
    if rep.misplaced_files:
        lines.append("正文位置不对：")
        lines += [f"  - {p}" for p in rep.misplaced_files]
    if rep.problems:
        lines.append("问题：")
        lines += [f"  ! {p}" for p in rep.problems]
    else:
        lines.append("未发现问题")
    return "\n".join(lines)
