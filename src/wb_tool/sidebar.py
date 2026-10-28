"""侧边栏快照（``<accountId>/sidebar-list-snapshot.json``）的合并与重建。

会话归属账号改完之后，目标账号的侧边栏列表必须包含这些会话，否则界面上看不到。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .config import SIDEBAR_SNAPSHOT, account_dirs
from .accounts import account_dir


def _read_items(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    out: dict[str, dict] = {}
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            out[it["id"]] = it
    return out


def collect_known_items(data_dir: Path) -> dict[str, dict]:
    """收集所有账号快照里的条目，用来保留 isPinned / state / isUserDefinedTitle 等字段。"""
    known: dict[str, dict] = {}
    for d in account_dirs(data_dir):
        for sid, item in _read_items(d / SIDEBAR_SNAPSHOT).items():
            known.setdefault(sid, item)
    return known


def _state_for(status: Optional[str]) -> str:
    if status in ("error", "failed"):
        return "error"
    if status in ("working", "running", "Pending"):
        return "running"
    return "idle"


def build_items(
    con: sqlite3.Connection,
    target_user: str,
    known: Optional[dict[str, dict]] = None,
) -> list[dict]:
    known = known or {}
    rows = list(
        con.execute(
            "SELECT id, cwd, title, custom_title, status, transport, conversation_origin, "
            "last_activity_at, updated_at, is_playground FROM sessions "
            "WHERE user_id = ? AND deleted_at IS NULL "
            "ORDER BY COALESCE(last_activity_at, updated_at) DESC",
            (target_user,),
        )
    )
    items: list[dict] = []
    for r in rows:
        sid = r["id"]
        prev = known.get(sid, {})
        prev_space = prev.get("space") if isinstance(prev.get("space"), dict) else {}
        last = r["last_activity_at"] or r["updated_at"] or 0
        item = dict(prev)
        item.update(
            {
                "id": sid,
                "title": r["custom_title"] or r["title"] or "未命名会话",
                "state": prev.get("state") or _state_for(r["status"]),
                "transport": r["transport"] or prev.get("transport") or "local",
                "kind": prev.get("kind") or ("playground" if r["is_playground"] else "task"),
                "lastActivityAt": last,
                "isPinned": bool(prev.get("isPinned", False)),
                "conversationOrigin": r["conversation_origin"] or prev.get("conversationOrigin") or "local",
                "updatedAt": r["updated_at"] or last,
                # cwd 是权威值（可能被 --remap-path 改过），只沿用旧的 space 类型
                "space": {
                    "type": prev_space.get("type", "local"),
                    "name": r["cwd"] or prev_space.get("name"),
                },
            }
        )
        items.append(item)

    items.sort(key=lambda it: (it.get("isPinned", False), it.get("lastActivityAt") or 0), reverse=True)
    return items


def update_session_space(
    data_dir: Path,
    session_id: str,
    new_cwd: str,
    backup_root: Optional[Path] = None,
) -> list[Path]:
    """把各账号快照里该会话的 ``space.name`` 改成新目录，返回被改写的快照文件。

    只动命中的那一条，保留 isPinned / state 等字段，也不重排顺序。
    """
    changed: list[Path] = []
    for d in account_dirs(data_dir):
        path = d / SIDEBAR_SNAPSHOT
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue

        hit = False
        for it in items:
            if not isinstance(it, dict) or it.get("id") != session_id:
                continue
            space = it.get("space")
            space = dict(space) if isinstance(space, dict) else {"type": "local"}
            if space.get("name") == new_cwd:
                continue
            space["name"] = new_cwd
            it["space"] = space
            hit = True

        if not hit:
            continue
        if backup_root is not None:
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_root / f"sidebar-{d.name}.json")
        data["savedAt"] = int(time.time() * 1000)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        changed.append(path)
    return changed


def write_snapshot(
    data_dir: Path,
    target_user: str,
    con: sqlite3.Connection,
    dry_run: bool = False,
    backup_root: Optional[Path] = None,
) -> tuple[Path, int]:
    """重建目标账号的侧边栏快照，返回 (路径, 条目数)。"""
    d = account_dir(data_dir, target_user)
    path = d / SIDEBAR_SNAPSHOT
    known = collect_known_items(data_dir)
    items = build_items(con, target_user, known)

    if not dry_run:
        d.mkdir(parents=True, exist_ok=True)
        if path.exists() and backup_root is not None:
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_root / f"sidebar-{target_user}.json")
        payload = {"version": 3, "savedAt": int(time.time() * 1000), "items": items}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    return path, len(items)
