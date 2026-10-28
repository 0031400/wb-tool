# wd-move

把 **WorkBuddy（国际版）** 的会话搬来搬去，并把会话归属账号统一成**当前登录账号**。

- 基于 [uv](https://docs.astral.sh/uv/)，Python 3.10
- 只依赖 `typer` + `rich`
- 所有写操作都会先备份

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `wd-move list` | 列出所有会话（标题 + UUID） |
| `wd-move export` | 导出**全部**会话 |
| `wd-move export --session <UUID>` | 导出**某次**会话（可多次 `-s`） |
| `wd-move import <包>` | 把包里的会话**合并**进本机 |
| `wd-move import <包> --replace` | 用包里的会话**整体替换**本机会话 |
| `wd-move reassign` | 把所有会话的归属账号改成**当前登录账号** |
| `wd-move status` | 看本机账号分布、当前登录账号 |
| `wd-move verify` | 体检：文件与数据库是否对得上 |

## 它到底改了什么

WorkBuddy 的数据根目录是 `~/.workbuddy-ai`（Windows 下 `C:\Users\<你>\.workbuddy-ai`）：

| 路径 | 作用 |
| --- | --- |
| `workbuddy.db` → `sessions.user_id` | **会话归属哪个账号，只看这里** |
| `projects/<cwd-slug>/<sessionId>.jsonl` | 会话正文 |
| `artifact-index/` `changes-index/` `changes-detail/` `file-history/` `file-tree-manifests/` `blobs/` | 会话产生的索引与文件快照 |
| `memory/<accountId>_memory.md` | 每个账号的长期记忆 |
| `<accountId>/sidebar-list-snapshot.json` | 每个账号的侧边栏会话列表 |

所以：

- **迁移** = 搬 `workbuddy.db` 快照 + 上述目录；
- **改账号** = `UPDATE sessions SET user_id = <当前账号>` + 重建当前账号的侧边栏快照（否则界面上看不到）。

`logs/`、`cache/`、`app/`、`binaries/`、`security/`、`local_storage/`、`device-id`、`keyblob` 等本机运行时/鉴权数据**不迁移**。

## 安装

```bash
cd wd-move
uv sync
```

之后用 `uv run wd-move ...`，或 `uv tool install .` 后直接用 `wd-move ...`。

## 常用流程

### 看有哪些会话

```bash
uv run wd-move list            # 标题 + UUID + 账号 + 时间
uv run wd-move list -n 20      # 只看前 20 条
uv run wd-move list --json     # 给脚本用
```

### 全部会话搬家

```bash
# 旧机器
uv run wd-move export --zip -o D:/transfer/wb.zip

# 新机器（先完全退出 WorkBuddy）
uv run wd-move import D:/transfer/wb.zip --replace --to-current
```

`--replace` = 先清空本机会话记录再写入，适合"整机搬到新机器、不要旧数据"的场景；
不加 `--replace` 就是合并（同 UUID 保留本机那份）。

### 只搬一次会话

```bash
# 旧机器：导出单条
uv run wd-move export --session 65c65e14-89db-4acb-a672-0bb89a5ca5f0 --zip

# 新机器：导入（单会话包同样用 import）
uv run wd-move import ./wb-session-65c65e14-*.zip --to-current
```

单会话包只包含该会话的 `.jsonl`、它的 `tool-results/`、以及按会话 ID 命名的索引文件。

### 只改账号归属

```bash
uv run wd-move reassign --dry-run   # 先看会改多少条
uv run wd-move reassign --yes       # 改成当前登录账号
```

### 体检

```bash
uv run wd-move verify
```

## 参数速查

### export

| 参数 | 说明 |
| --- | --- |
| `-o/--out` | 输出目录（默认 `./wb-migration-<时间戳>`） |
| `-s/--session` | 只导出指定会话 UUID，可多次指定 |
| `--lite` | 只带会话正文 + 记忆，跳过索引/文件快照 |
| `--with-optional` | 同时带上 `connectors/` 与 `plugins/`（含凭据） |
| `--zip` | 额外打一个 zip |
| `--overwrite` | 允许覆盖非空输出目录 |

### import

| 参数 | 说明 |
| --- | --- |
| `--replace` | 用包里的会话整体替换本机会话（清空后导入） |
| `--to-current` | 导入后把归属改成当前登录账号（别名 `--to-latest`） |
| `--account <UUID>` | 导入后把归属改成指定账号 |
| `--remap-path 旧=新` | 两台机器用户名/盘符不同时改写 `cwd`，可多次指定 |
| `--overwrite` | 覆盖本机同名文件（默认跳过） |
| `--overwrite-db` | 数据库主键冲突时覆盖本机记录（默认保留本机） |
| `--merge-memory` | 把其它账号的记忆并入目标账号 |
| `--dry-run` | 只打印计划 |

跨机器换了用户名：

```bash
uv run wd-move import wb.zip --replace --to-current \
  --remap-path "C:/Users/lxz=C:/Users/newuser" \
  --remap-path "D:/file=D:/work"
```

导入时会自动把 `projects/<旧 slug>` 目录改名成新 `cwd` 对应的 slug，侧边栏快照里的工作区路径也一并更新。

## "当前登录账号"怎么判定

不需要翻登录日志——运行中的客户端会持续刷新**当前账号**的痕迹，取最近被写过的那一个即可：

1. `<accountId>/sidebar-list-snapshot.json` 的 `savedAt`
2. `plugin-status-daily.json` 里各账号的上报时间
3. 都取不到时，退回 `logs/**/edge-sync.log` 里最后一条 `onLoginReady ENTER: userId=...`

想指定别的账号就加 `--account <UUID>`。

## 安全设计

- 导出用 SQLite `backup` API 生成一致性快照，不会拷到写了一半的库；
- 写库前先 `PRAGMA integrity_check`；
- 改库前把 `workbuddy.db` 备份到 `.wd-move-backup/<时间戳>/`；
- 覆盖任何本机文件前都会先备份到同一目录；
- 客户端仍在运行直接拒绝执行（`--force` 才跳过；`--dry-run` 不受限）；
- 侧边栏快照用临时文件 + 原子替换写入。

## 回滚

```bash
# 关掉 WorkBuddy 后，把备份的库拷回去即可
cp ~/.workbuddy-ai/.wd-move-backup/<时间戳>/workbuddy.db ~/.workbuddy-ai/workbuddy.db
```

## 限制

- 只处理本地会话（`transport=local`），云端同步的会话由服务端管理。
- `--replace` 只清空数据库里的会话记录，磁盘上旧的 `.jsonl` 不会删除（`verify` 会把它们列为孤儿文件）。
- `plugins/` 与 `binaries/` 与机器相关，默认不迁移，插件需在新机器重装。
- 迁移前后请保持 WorkBuddy 版本接近；表结构差异过大时工具会跳过缺失字段并给出提示。
