"""命令行入口。"""

from __future__ import annotations

import json as jsonlib
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from . import accounts as acc
from .config import default_data_dir, human_bytes
from .exporter import export as do_export
from .importer import import_bundle
from .listing import list_sessions
from .mover import move_session
from .reassign import reassign as do_reassign
from .verify import describe, verify as do_verify

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="WorkBuddy（国际版）会话工具：导出/导入会话、改会话工作目录，并把会话归属账号改成当前登录账号。",
)
console = Console()
_STATE: dict[str, Optional[Path]] = {"data_dir": None}


def _ver_cb(value: bool) -> None:
    if value:
        console.print(f"wb-tool {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    data_dir: Optional[Path] = typer.Option(
        None, "--data-dir", "-d", help="WorkBuddy 数据目录，默认 ~/.workbuddy-ai"
    ),
    version: bool = typer.Option(False, "--version", callback=_ver_cb, is_eager=True, help="显示版本"),
) -> None:
    _STATE["data_dir"] = data_dir


def _data_dir() -> Path:
    return (_STATE["data_dir"] or default_data_dir()).expanduser().resolve()


def _fail(message: str, code: int = 1) -> None:
    console.print(f"[red]错误：[/red]{message}")
    raise typer.Exit(code)


def _fmt_ms(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"


def _parse_remap(values: Optional[list[str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in values or []:
        if "=" not in raw:
            raise ValueError(f"--remap-path 需要 旧路径=新路径 的形式：{raw}")
        old, new = raw.split("=", 1)
        out.append((old.strip(), new.strip()))
    return out


# --------------------------------------------------------------------------- list


@app.command("list")
def cmd_list(
    account: Optional[str] = typer.Option(None, "--account", "-a", help="只看某个账号的会话"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="只显示前 N 条"),
    all_: bool = typer.Option(False, "--all", help="包含已删除的会话"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """列出所有会话（标题 + UUID）。"""
    try:
        rows = list_sessions(
            _data_dir(), account=account, include_deleted=all_, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))

    if as_json:
        payload = [
            {
                "uuid": r.id,
                "title": r.title,
                "account": r.account,
                "cwd": r.cwd,
                "status": r.status,
                "updated_at": r.updated_at,
                "files": r.files,
                "bytes": r.bytes,
                "deleted": r.deleted,
            }
            for r in rows
        ]
        console.print_json(jsonlib.dumps(payload, ensure_ascii=False))
        return

    if not rows:
        console.print("[yellow]没有会话[/yellow]")
        return

    if console.width >= 100:
        table = Table(box=None, pad_edge=False, title=f"共 {len(rows)} 条会话")
        table.add_column("UUID", style="cyan", no_wrap=True)
        table.add_column("标题", overflow="ellipsis", no_wrap=True)
        table.add_column("账号", no_wrap=True)
        table.add_column("更新", no_wrap=True, justify="right")
        for r in rows:
            table.add_row(
                r.id,
                r.title + ("（已删除）" if r.deleted else ""),
                r.account[:8],
                _fmt_ms(r.updated_at),
            )
        console.print(table)
    else:
        # 窄终端：一行一条，UUID + 标题，第二行放元信息，保证不折行
        console.print(f"[bold]共 {len(rows)} 条会话[/bold]")
        for r in rows:
            title = r.title + ("（已删除）" if r.deleted else "")
            console.print(f"[cyan]{r.id}[/cyan]  {title}", no_wrap=True, overflow="ellipsis")
            console.print(
                f"  {r.account[:8]} · {_fmt_ms(r.updated_at)} · {human_bytes(r.bytes)}",
                style="dim",
                no_wrap=True,
            )

    console.print(
        f"合计 {human_bytes(sum(r.bytes for r in rows))}；"
        "导出单条：wb-tool export --session <UUID>"
    )


# --------------------------------------------------------------------------- status


@app.command("status")
def cmd_status() -> None:
    """查看本机账号与会话概况。"""
    data_dir = _data_dir()
    if not data_dir.is_dir():
        _fail(f"数据目录不存在：{data_dir}")

    infos = acc.collect_accounts(data_dir)
    hit = acc.current_account(data_dir)

    console.print(f"数据目录：[bold]{data_dir}[/bold]")
    if hit:
        console.print(
            f"当前登录账号：[bold green]{hit.account_id}[/bold green]"
            f"（来源 {acc.SOURCE_LABEL.get(hit.source, hit.source)}，{_fmt_ms(hit.ts)}）"
        )
    else:
        console.print("[yellow]未能自动判定当前登录账号[/yellow]")

    table = Table(title="账号与会话", box=None, pad_edge=False)
    table.add_column("账号 ID", style="cyan", no_wrap=True)
    table.add_column("会话", justify="right")
    table.add_column("账号目录", justify="center")
    table.add_column("记忆", justify="center")
    table.add_column("判定来源")

    for info in sorted(infos.values(), key=lambda i: -i.last_ts):
        mark = " [green]←当前[/green]" if hit and info.account_id == hit.account_id else ""
        table.add_row(
            info.account_id + mark,
            str(info.session_count),
            "有" if info.has_dir else "-",
            "有" if info.has_memory else "-",
            acc.SOURCE_LABEL.get(info.best_source, info.best_source),
        )
    if infos:
        console.print(table)
    else:
        console.print("[yellow]没有发现任何账号记录[/yellow]")


# --------------------------------------------------------------------------- export


@app.command("export")
def cmd_export(
    out: Optional[str] = typer.Option(None, "--out", "-o", help="输出目录（默认 ./wb-migration-<时间戳>）"),
    session: Optional[list[str]] = typer.Option(
        None, "--session", "-s", help="只导出指定会话 UUID，可多次指定"
    ),
    lite: bool = typer.Option(False, "--lite", help="只带会话正文与记忆，跳过各类索引/文件快照"),
    with_optional: bool = typer.Option(False, "--with-optional", help="同时带上 connectors/ 与 plugins/（含凭据）"),
    zip_: bool = typer.Option(False, "--zip", help="打包成 zip"),
    overwrite: bool = typer.Option(False, "--overwrite", help="允许覆盖已存在的非空输出目录"),
) -> None:
    """导出全部会话，或用 --session 导出指定会话。"""
    try:
        res = do_export(
            data_dir=_data_dir(),
            out=out,
            lite=lite,
            with_optional=with_optional,
            make_zip=zip_,
            overwrite=overwrite,
            session_ids=session,
            log=lambda m: console.print(m),
        )
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))

    console.print()
    scope = f"{len(res.session_ids)} 条指定会话" if res.session_ids else "全部会话"
    console.print(
        f"[green]导出完成[/green]（{scope}）共 {res.total_files} 个文件 / {human_bytes(res.total_bytes)}"
    )
    console.print(f"迁移包：[bold]{res.bundle}[/bold]")
    if res.archive:
        console.print(f"压缩包：[bold]{res.archive}[/bold]")
    console.print("在另一台机器执行：wb-tool import <迁移包路径> --to-current")


# --------------------------------------------------------------------------- import


@app.command("import")
def cmd_import(
    bundle: Path = typer.Argument(..., help="迁移包目录或 zip 路径"),
    replace: bool = typer.Option(
        False, "--replace", help="用迁移包里的会话整体替换本机会话（清空后导入）"
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖本机同名文件（默认跳过）"),
    overwrite_db: bool = typer.Option(False, "--overwrite-db", help="数据库主键冲突时覆盖本机记录"),
    remap: Optional[list[str]] = typer.Option(
        None, "--remap-path", help="路径重映射，形如 'C:/Users/old=C:/Users/new'，可多次指定"
    ),
    account: Optional[str] = typer.Option(None, "--account", help="导入后把会话归属改为该账号 UUID"),
    to_current: bool = typer.Option(
        False, "--to-current", "--to-latest", help="导入后把会话归属改为当前登录账号"
    ),
    merge_memory: bool = typer.Option(False, "--merge-memory", help="同时合并各账号的记忆文件"),
    force: bool = typer.Option(False, "--force", help="跳过客户端运行检查"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印计划，不落盘"),
) -> None:
    """把迁移包导入本机（请先完全退出 WorkBuddy）。"""
    try:
        mappings = _parse_remap(remap)
        res = import_bundle(
            bundle_path=bundle,
            data_dir=_data_dir(),
            overwrite=overwrite,
            overwrite_db=overwrite_db,
            replace=replace,
            remap=mappings,
            account=account,
            to_current=to_current,
            merge_memory=merge_memory,
            force=force,
            dry_run=dry_run,
            log=lambda m: console.print(m),
        )
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))

    console.print()
    console.print(
        f"[green]导入完成[/green] 复制 {res.copied_files} 个文件，跳过 {res.skipped_files} 个已存在文件"
    )
    if res.backup_dir:
        console.print(f"备份目录：[bold]{res.backup_dir}[/bold]")
    for warn in res.warnings:
        console.print(f"[yellow]! {warn}[/yellow]")
    if res.reassign:
        console.print(
            f"会话归属已改为 [bold]{res.reassign.target}[/bold]"
            f"（{res.reassign.sessions_changed} 条）"
        )
    console.print("建议接着执行：wb-tool verify")


# --------------------------------------------------------------------------- move


@app.command("move")
def cmd_move(
    session: str = typer.Argument(..., help="会话 UUID（可只写前几位）"),
    new_cwd: str = typer.Argument(..., help="新的工作目录，如 D:/work/new-proj"),
    add_workspace: bool = typer.Option(
        True, "--add-workspace/--no-add-workspace", help="把新目录写进最近打开的工作区列表"
    ),
    force: bool = typer.Option(False, "--force", help="跳过客户端运行检查"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印计划，不落盘"),
    yes: bool = typer.Option(False, "--yes", "-y", help="不再确认，直接执行"),
) -> None:
    """把某个会话的工作目录改到新路径（数据库 + 正文目录 + 侧边栏一起改）。"""
    data_dir = _data_dir()
    try:
        if dry_run:
            move_session(session, new_cwd, data_dir=data_dir, dry_run=True, log=console.print)
            console.print("[yellow]dry-run：未做任何修改[/yellow]")
            return

        if not yes:
            try:
                ok = typer.confirm(
                    f"将把会话 {session} 的工作目录改为 {new_cwd}，请先完全退出 WorkBuddy。继续？",
                    default=False,
                )
            except (EOFError, KeyboardInterrupt):
                ok = False
            if not ok:
                console.print("已取消")
                raise typer.Exit(0)

        res = move_session(
            session,
            new_cwd,
            data_dir=data_dir,
            force=force,
            add_workspace=add_workspace,
            log=lambda m: console.print(m),
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))

    console.print()
    for warn in res.warnings:
        console.print(f"[yellow]! {warn}[/yellow]")
    console.print(
        f"[green]完成[/green] {res.title} 的工作目录 -> [bold]{res.new_cwd}[/bold]"
    )
    if res.backup_dir:
        console.print(f"备份目录：{res.backup_dir}")
    console.print("建议接着执行：wb-tool verify")


# --------------------------------------------------------------------------- reassign


@app.command("reassign")
def cmd_reassign(
    account: Optional[str] = typer.Option(
        None, "--account", help="目标账号 UUID（默认取当前登录账号）"
    ),
    merge_memory: bool = typer.Option(False, "--merge-memory", help="把其它账号的记忆并入目标账号"),
    force: bool = typer.Option(False, "--force", help="跳过客户端运行检查"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示将改动的数量"),
    yes: bool = typer.Option(False, "--yes", "-y", help="不再确认，直接执行"),
) -> None:
    """把所有会话的归属账号改成当前登录账号。"""
    data_dir = _data_dir()
    try:
        if dry_run:
            do_reassign(data_dir=data_dir, account=account, dry_run=True, force=force, log=console.print)
            console.print("[yellow]dry-run：未做任何修改[/yellow]")
            return

        if not yes:
            try:
                ok = typer.confirm(
                    f"将把 {data_dir} 下所有会话的归属账号改写为当前登录账号，确认继续？",
                    default=False,
                )
            except (EOFError, KeyboardInterrupt):
                ok = False
            if not ok:
                console.print("已取消")
                raise typer.Exit(0)

        res = do_reassign(
            data_dir=data_dir,
            account=account,
            merge_memory=merge_memory,
            force=force,
            dry_run=False,
            assume_yes=yes,
            log=lambda m: console.print(m),
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _fail(str(exc))

    console.print()
    console.print(
        f"目标账号：[bold green]{res.target}[/bold green]"
        f"（来源 {acc.SOURCE_LABEL.get(res.source, res.source)}）"
    )
    if res.before:
        console.print("改写前分布：")
        for uid, cnt in sorted(res.before.items(), key=lambda kv: -kv[1]):
            console.print(f"  - {uid or '(未归属)'}  {cnt} 条")
    console.print(
        f"[green]完成[/green] 会话 {res.sessions_changed} 条，自动化任务 {res.automations_changed} 个"
    )
    if res.backup:
        console.print(f"数据库备份：{res.backup}")
    console.print("建议接着执行：wb-tool verify")


# --------------------------------------------------------------------------- verify


@app.command("verify")
def cmd_verify() -> None:
    """检查会话文件与数据库是否一致。"""
    rep = do_verify(data_dir=_data_dir())
    console.print(describe(rep))
    if rep.problems:
        raise typer.Exit(2)


if __name__ == "__main__":  # pragma: no cover
    app()
