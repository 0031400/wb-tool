"""运行时检查：确认 WorkBuddy 客户端已退出，避免边跑边改数据库。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

APP_HINTS = ("workbuddy",)


def running_processes() -> list[str]:
    """返回本机看起来是 WorkBuddy 客户端的进程名列表。"""
    names: list[str] = []
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        for line in out.splitlines():
            if not line.strip():
                continue
            first = line.split('","')[0].strip().strip('"').lower()
            if any(h in first for h in APP_HINTS):
                names.append(first)
    else:
        try:
            out = subprocess.run(
                ["pgrep", "-af", "workbuddy"], capture_output=True, text=True, errors="replace", timeout=30
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        for line in out.splitlines():
            low = line.lower()
            if "wb-tool" in low or "wb_tool" in low or "wd-move" in low or "wd_move" in low:
                continue
            if any(h in low for h in APP_HINTS):
                names.append(line.strip())
    return names


def ensure_app_closed(data_dir: Path, force: bool = False) -> list[str]:
    """若客户端仍在运行则抛异常（除非 force）。返回命中的进程名供提示。"""
    procs = running_processes()
    if procs and not force:
        uniq = sorted(set(procs))
        raise RuntimeError(
            "检测到 WorkBuddy 客户端仍在运行，请完全退出后再执行：\n  "
            + "\n  ".join(uniq)
            + "\n（确认风险后可用 --force 跳过检查）"
        )
    return procs


def has_wal_activity(data_dir: Path) -> bool:
    wal = data_dir / "workbuddy.db-wal"
    return wal.is_file() and wal.stat().st_size > 0
