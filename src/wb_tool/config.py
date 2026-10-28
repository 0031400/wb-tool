"""路径、目录约定与常量。

WorkBuddy（国际版）的数据根目录默认是 ``~/.workbuddy-ai``，其中与本工具相关的结构：

    workbuddy.db                    会话元数据（sessions.user_id 决定会话归属账号）
    projects/<cwd-slug>/<sid>.jsonl 会话正文
    artifact-index/ changes-index/ changes-detail/   会话产生的索引
    file-history/ file-tree-manifests/ blobs/        文件快照
    memory/<accountId>_memory.md    每个账号的长期记忆
    <accountId>/sidebar-list-snapshot.json  每个账号的侧边栏会话列表
    connectors/<accountId>/         每个账号的连接器配置
    logs/                           登录记录（用于判定"最新登录账号"）
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

APP_DIR_NAME = ".workbuddy-ai"

#: 会话相关、默认随迁移一起带走的目录
CORE_DIRS = (
    "projects",
    "artifact-index",
    "changes-index",
    "changes-detail",
    "file-history",
    "file-tree-manifests",
    "blobs",
    "memory",
    "audit-log",
)

#: 含凭据/体积较大，默认不带，需显式 --with-optional
OPTIONAL_DIRS = (
    "connectors",
    "plugins",
)

#: 明确不迁移的目录（本机运行时状态、缓存、二进制、鉴权）
SKIP_DIRS = frozenset(
    {
        "app",
        "binaries",
        "cache",
        "cli-env",
        "editor-sdk-sandbox-ws",
        "local_storage",
        "logs",
        "pending-telemetry",
        "security",
        "sessions",
        ".workbuddy-sqlite-migrations",
    }
)

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

DB_NAME = "workbuddy.db"
SIDEBAR_SNAPSHOT = "sidebar-list-snapshot.json"
BACKUP_DIR_NAME = ".wb-tool-backup"
#: 改名前（wd-move）留下的备份目录，仅用于提示，不自动迁移
LEGACY_BACKUP_DIR_NAME = ".wd-move-backup"


def default_data_dir() -> Path:
    """数据根目录，可用 WORKBUDDY_HOME 覆盖。"""
    env = os.environ.get("WORKBUDDY_HOME") or os.environ.get("WORKBUDDY_AI_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / APP_DIR_NAME).resolve()


def is_account_dir(path: Path) -> bool:
    return path.is_dir() and bool(UUID_RE.match(path.name))


def account_dirs(data_dir: Path) -> list[Path]:
    """数据根目录下的 ``<accountId>/`` 目录。"""
    if not data_dir.is_dir():
        return []
    return sorted(p for p in data_dir.iterdir() if is_account_dir(p))


def slugify_cwd(cwd: str) -> str:
    """把会话的 cwd 转成 ``projects/`` 下的目录名。

    例：``D:/file/python/tg-one`` -> ``d-file-python-tg-one``
        ``C:\\Users\\lxz\\WorkBuddy AI\\2026-10-19`` -> ``c-Users-lxz-WorkBuddy AI-2026-10-19``
    """
    s = cwd.strip().replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/", s)
    if m:
        s = s[0].lower() + "/" + s[3:]
    s = s.strip("/")
    s = re.sub(r"[/:]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s or "root"


def normalize_cwd(cwd: str) -> str:
    """统一成反斜杠无关的形式，便于比较与重映射。"""
    return cwd.replace("\\", "/").rstrip("/")


def remap_cwd(cwd: Optional[str], mappings: list[tuple[str, str]]) -> Optional[str]:
    """按 ``旧前缀 -> 新前缀`` 规则改写路径（大小写不敏感，保留剩余部分原始大小写）。"""
    if not cwd:
        return cwd
    norm = normalize_cwd(cwd)
    for old, new in mappings:
        old_n = normalize_cwd(old)
        new_n = normalize_cwd(new)
        if norm.lower() == old_n.lower():
            return new_n
        if norm.lower().startswith(old_n.lower() + "/"):
            return new_n + norm[len(old_n):]
    return cwd


def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f}{u}" if u != "B" else f"{int(f)}B"
        f /= 1024
    return f"{n}B"
