"""账号识别：找出"最新登录的账号"，并列出本机出现过的所有账号。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import db as dbmod
from .config import SIDEBAR_SNAPSHOT, account_dirs, is_account_dir

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
LOGIN_RE = re.compile(r"onLoginReady\s+ENTER:\s*userId=(" + UUID_RE.pattern + r")")
LOG_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{1,3})Z?\]")

#: 来源的中文短名
SOURCE_LABEL = {
    "manual": "手动指定",
    "sidebar-snapshot": "侧边栏快照",
    "plugin-status": "上报记录",
    "config-cache": "配置缓存",
    "login-log": "登录日志",
    "db-sessions": "会话记录",
    "account-dir": "账号目录",
}

#: 同时间戳时，来源的可信度（越小越可信）
SOURCE_RANK = {
    "manual": -1,
    "sidebar-snapshot": 0,
    "plugin-status": 1,
    "config-cache": 2,
    "login-log": 3,
    "db-sessions": 4,
}


@dataclass
class AccountHit:
    account_id: str
    ts: int
    source: str

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"AccountHit({self.account_id}, ts={self.ts}, {self.source})"


@dataclass
class AccountInfo:
    account_id: str
    last_ts: int = 0
    sources: set[str] = field(default_factory=set)
    session_count: int = 0
    has_dir: bool = False
    has_memory: bool = False
    hits: list[AccountHit] = field(default_factory=list)

    @property
    def best_source(self) -> str:
        if not self.hits:
            return "-"
        return sorted(self.hits, key=lambda h: (SOURCE_RANK.get(h.source, 9), -h.ts))[0].source


def _iso_to_ms(text: str) -> Optional[int]:
    try:
        dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return int(dt.timestamp() * 1000)


def scan_login_logs(data_dir: Path) -> list[AccountHit]:
    """扫描日志中的 ``onLoginReady ENTER: userId=<uuid>``，返回每一次登录事件。

    只认 ``edge-sync.log``（EdgeSync 登录就绪的唯一出处），并且要求该行以时间戳开头。
    这样可以排除"日志里恰好引用了这条字符串"的误判（例如工具输出被回显进日志）。
    """
    logs = data_dir / "logs"
    if not logs.is_dir():
        return []

    files = sorted(logs.rglob("edge-sync*.log"))
    if not files:
        files = sorted(logs.rglob("*.log"))

    hits: list[AccountHit] = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "onLoginReady" not in line:
                        continue
                    t = LOG_TS_RE.match(line.lstrip("\ufeff"))
                    if not t:
                        continue
                    ts = _iso_to_ms(t.group(1))
                    if ts is None:
                        continue
                    m = LOGIN_RE.search(line)
                    if not m:
                        continue
                    hits.append(AccountHit(m.group(1).lower(), ts, "login-log"))
        except OSError:
            continue
    return hits


def scan_config_cache(data_dir: Path) -> list[AccountHit]:
    """``local_storage/wb_entry_*.info`` 里有 ``[{userId, ts}]`` 的产品配置缓存。"""
    storage = data_dir / "local_storage"
    if not storage.is_dir():
        return []
    hits: list[AccountHit] = []
    for path in storage.glob("*entry_*.info"):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            uid = item.get("userId")
            ts = item.get("ts")
            if isinstance(uid, str) and UUID_RE.fullmatch(uid) and isinstance(ts, (int, float)):
                hits.append(AccountHit(uid.lower(), int(ts), "config-cache"))
    return hits


def scan_sidebar_snapshots(data_dir: Path) -> list[AccountHit]:
    hits: list[AccountHit] = []
    for d in account_dirs(data_dir):
        snap = d / SIDEBAR_SNAPSHOT
        if not snap.is_file():
            continue
        try:
            data = json.loads(snap.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        ts = data.get("savedAt") if isinstance(data, dict) else None
        if isinstance(ts, (int, float)):
            hits.append(AccountHit(d.name.lower(), int(ts), "sidebar-snapshot"))
    return hits


def scan_plugin_status(data_dir: Path) -> list[AccountHit]:
    """``plugin-status-daily.json``：运行中的客户端会为当前账号持续上报时间戳。"""
    path = data_dir / "plugin-status-daily.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    hits: list[AccountHit] = []
    for key, value in data.items():
        uid = str(key).split("-personal")[0]
        if not UUID_RE.fullmatch(uid) or not isinstance(value, dict):
            continue
        stamps = [v for v in value.values() if isinstance(v, (int, float))]
        if stamps:
            hits.append(AccountHit(uid.lower(), int(max(stamps)), "plugin-status"))
    return hits


def scan_db_sessions(data_dir: Path) -> list[AccountHit]:
    path = dbmod.db_path(data_dir)
    if not path.is_file():
        return []
    hits: list[AccountHit] = []
    try:
        con = dbmod.connect(path, readonly=True)
    except Exception:
        return []
    try:
        for row in con.execute(
            "SELECT user_id, MAX(COALESCE(last_activity_at, updated_at)) FROM sessions "
            "WHERE user_id IS NOT NULL GROUP BY user_id"
        ):
            if row[0] and row[1]:
                hits.append(AccountHit(str(row[0]).lower(), int(row[1]), "db-sessions"))
    except Exception:
        pass
    finally:
        con.close()
    return hits


def collect_accounts(data_dir: Path) -> dict[str, AccountInfo]:
    """汇总所有来源，得到本机出现过的账号。"""
    infos: dict[str, AccountInfo] = {}

    def add(hit: AccountHit) -> None:
        info = infos.setdefault(hit.account_id, AccountInfo(hit.account_id))
        info.hits.append(hit)
        info.sources.add(hit.source)
        info.last_ts = max(info.last_ts, hit.ts)

    for hit in scan_login_logs(data_dir):
        add(hit)
    for hit in scan_config_cache(data_dir):
        add(hit)
    for hit in scan_sidebar_snapshots(data_dir):
        add(hit)
    for hit in scan_plugin_status(data_dir):
        add(hit)
    for hit in scan_db_sessions(data_dir):
        add(hit)

    # 只有目录没有其它痕迹的账号也列出来
    for d in account_dirs(data_dir):
        info = infos.setdefault(d.name.lower(), AccountInfo(d.name.lower()))
        info.has_dir = True
        info.sources.add("account-dir")

    path = dbmod.db_path(data_dir)
    if path.is_file():
        try:
            con = dbmod.connect(path, readonly=True)
        except Exception:
            con = None
        if con is not None:
            try:
                for uid, cnt in dbmod.session_counts(con).items():
                    if not uid:
                        continue
                    info = infos.setdefault(uid.lower(), AccountInfo(uid.lower()))
                    info.session_count = cnt
            except Exception:
                pass
            finally:
                con.close()

    mem = data_dir / "memory"
    if mem.is_dir():
        for f in mem.glob("*_memory.md"):
            uid = f.name.split("_")[0].lower()
            if uid in infos:
                infos[uid].has_memory = True

    return infos


def current_account(data_dir: Path) -> Optional[AccountHit]:
    """判定**当前登录账号**。

    取最直接的信号：运行中的客户端会不断刷新当前账号的侧边栏快照和上报时间戳，
    所以"最近被写过的那一份"就是当前登录的账号。都取不到时退回登录日志/会话活动时间。
    """
    hits: list[AccountHit] = []
    hits += scan_sidebar_snapshots(data_dir)
    hits += scan_plugin_status(data_dir)
    if not hits:
        logs = scan_login_logs(data_dir)
        if logs:
            hits.append(logs[-1])
        hits += scan_config_cache(data_dir)
        hits += scan_db_sessions(data_dir)
    if not hits:
        return None
    hits.sort(key=lambda h: (h.ts, -SOURCE_RANK.get(h.source, 9)))
    return hits[-1]


#: 兼容旧名字
latest_account = current_account


def resolve_target(data_dir: Path, explicit: Optional[str] = None) -> tuple[str, str]:
    """返回 (账号 ID, 判定来源)。默认取当前登录账号。"""
    if explicit:
        account = explicit.strip().lower()
        if not UUID_RE.fullmatch(account):
            raise ValueError(f"账号 ID 格式不正确（应为 UUID）：{explicit}")
        return account, "manual"

    hit = current_account(data_dir)
    if not hit:
        raise ValueError(
            "无法自动判定当前登录账号（没有找到侧边栏快照/上报记录/登录日志）。"
            "请用 --account <UUID> 手动指定。"
        )
    return hit.account_id, hit.source


def account_dir(data_dir: Path, account_id: str) -> Path:
    """返回账号目录（大小写不敏感匹配已有目录）。"""
    for d in data_dir.iterdir() if data_dir.is_dir() else []:
        if is_account_dir(d) and d.name.lower() == account_id.lower():
            return d
    return data_dir / account_id
