# wb-tool

把 **WorkBuddy（国际版）** 的会话搬来搬去、改某个会话的工作目录，并把会话归属账号统一成**当前登录账号**。

- 基于 [uv](https://docs.astral.sh/uv/)，Python 3.10
- 只依赖 `typer` + `rich`
- 所有写操作都会先备份

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `wb-tool list` | 列出所有会话（标题 + UUID） |
| `wb-tool export` | 导出**全部**会话 |
| `wb-tool export --session <UUID>` | 导出**某次**会话（可多次 `-s`） |
| `wb-tool import <包>` | 把包里的会话**合并**进本机 |
| `wb-tool import <包> --replace` | 用包里的会话**整体替换**本机会话 |
| `wb-tool move <UUID> <新目录>` | 改**某个会话**的工作目录（cwd） |
| `wb-tool relocate` | 修复 `projects/<slug>` 目录名与 cwd 对不上（"暂无对话记录"） |
| `wb-tool reassign` | 把所有会话的归属账号改成**当前登录账号** |
| `wb-tool status` | 看本机账号分布、当前登录账号 |
| `wb-tool verify` | 体检：文件与数据库是否对得上 |

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
- **改账号** = `UPDATE sessions SET user_id = <当前账号>` + 重建当前账号的侧边栏快照（否则界面上看不到）；
- **改工作目录** = `UPDATE sessions SET cwd = <新目录>` + 把 `projects/<旧 slug>/` 里属于该会话的正文搬到 `<新 slug>/` + 往 `workspaces` 加一行 + 改侧边栏条目的 `space.name`。

`logs/`、`cache/`、`app/`、`binaries/`、`security/`、`local_storage/`、`device-id`、`keyblob` 等本机运行时/鉴权数据**不迁移**。

## 安装

```bash
cd wb-tool
uv sync
```

之后用 `uv run wb-tool ...`，或 `uv tool install .` 后直接用 `wb-tool ...`。

## 常用流程

### 看有哪些会话

```bash
uv run wb-tool list            # 标题 + UUID + 账号 + 时间
uv run wb-tool list -n 20      # 只看前 20 条
uv run wb-tool list --json     # 给脚本用
```

### 全部会话搬家

```bash
# 旧机器
uv run wb-tool export --zip -o D:/transfer/wb.zip

# 新机器（先完全退出 WorkBuddy）
uv run wb-tool import D:/transfer/wb.zip --replace --to-current
```

`--replace` = 先清空本机会话记录再写入，适合"整机搬到新机器、不要旧数据"的场景；
不加 `--replace` 就是合并（同 UUID 保留本机那份）。

### 只搬一次会话

```bash
# 旧机器：导出单条
uv run wb-tool export --session 65c65e14-89db-4acb-a672-0bb89a5ca5f0 --zip

# 新机器：导入（单会话包同样用 import）
uv run wb-tool import ./wb-session-65c65e14-*.zip --to-current
```

单会话包只包含该会话的 `.jsonl`、它的 `tool-results/`、以及按会话 ID 命名的索引文件。

### 界面显示“暂无对话记录”

侧边栏能列出会话，点进去却是空的——九成是 **`projects/` 下的目录名和 `sessions.cwd` 对不上**。

客户端是用 cwd 反推目录名的：`projects/<slugify(cwd)>/<sessionId>.jsonl`，路径找不到就当作没有历史，
日志里会出现 `[SessionStore] deserializeSessionFromPath failed ... ENOENT`。

```bash
uv run wb-tool verify            # 会直接点名：哪条会话在哪个目录、cwd 是什么
uv run wb-tool relocate --dry-run
uv run wb-tool relocate          # 退出客户端后执行
```

`relocate` 会把目录名对齐到 cwd；同一目录里混了不同 cwd 的会话时，按会话拆开各搬各的。
备份落在 `.wb-tool-backup/<时间戳>/`。

**怎么撞上的**：先 `move` 改了 cwd，但那时正文还没落地（目录是空的，搬不动）；
或者导入的包是按旧机器 cwd 算的 slug，导入后又用 `--remap-path` 改了 cwd。
现在 `import` 结束时会自动跑一遍这个校验，`move` 找不到正文时也会提示补跑。

### 只改账号归属

```bash
uv run wb-tool reassign --dry-run   # 先看会改多少条
uv run wb-tool reassign --yes       # 改成当前登录账号
```

### 改某个会话的工作目录

会话的 cwd 在创建时就定死了，客户端界面里没有"改已有会话工作目录"的入口。
要改只能动数据，`move` 会把四处一起改好：

```bash
# 1) 完全退出 WorkBuddy（否则数据库会被客户端回写覆盖）
# 2) 先看计划
uv run wb-tool move e2c7ae29 D:/work/new-proj --dry-run

# 3) 真改（UUID 可以只写前几位，唯一匹配即可）
uv run wb-tool move e2c7ae29 D:/work/new-proj

# 4) 体检
uv run wb-tool verify
```

它做的事：

| 位置 | 动作 |
| --- | --- |
| `sessions.cwd` | 改成新路径（正斜杠形式，和客户端一致） |
| `projects/<旧 slug>/` | 该目录**只含这一个会话**时整个改名；还有别的会话时只搬 `<sid>.jsonl`、`<sid>.file-rollback.ndjson`、`<sid>/` 这几项，其余原地不动 |
| `workspaces.path` | 新增新目录（保留旧目录，其它会话可能还在用） |
| `<账号>/sidebar-list-snapshot.json` | 该条目的 `space.name` 改成新目录 |

改之前的 `workbuddy.db` 和侧边栏快照会备份到 `.wb-tool-backup/<时间戳>/`，
同目录下的 `move-<sid>.json` 记录了旧/新路径与搬动了哪些文件，想退回照着搬回去即可。

注意：

- 目标目录**不存在**只给警告、不自动创建（防止手滑打错路径建出一堆空目录）；
- 会话正文和各类索引里残留的旧绝对路径是历史记录，不改写；
- 客户端里那条会话的历史消息还在，只是"工作目录"变成了新路径，之后读写文件都在新目录。

### 体检

```bash
uv run wb-tool verify
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
uv run wb-tool import wb.zip --replace --to-current \
  --remap-path "C:/Users/lxz=C:/Users/newuser" \
  --remap-path "D:/file=D:/work"
```

导入时会自动把 `projects/<旧 slug>` 目录改名成新 `cwd` 对应的 slug，侧边栏快照里的工作区路径也一并更新。

### move

| 参数 | 说明 |
| --- | --- |
| `--dry-run` | 只打印计划（改哪几处、搬哪些文件） |
| `--force` | 跳过客户端运行检查 |
| `--add-workspace/--no-add-workspace` | 是否把新目录写进 `workspaces`（默认写） |
| `--yes` | 不再确认 |

### relocate

| 参数 | 说明 |
| --- | --- |
| `--dry-run` | 只打印要改的目录 |
| `--force` | 跳过客户端运行检查 |
| `--yes` | 不再确认 |

## "当前登录账号"怎么判定

不需要翻登录日志——运行中的客户端会持续刷新**当前账号**的痕迹，取最近被写过的那一个即可：

1. `<accountId>/sidebar-list-snapshot.json` 的 `savedAt`
2. `plugin-status-daily.json` 里各账号的上报时间
3. 都取不到时，退回 `logs/**/edge-sync.log` 里最后一条 `onLoginReady ENTER: userId=...`

想指定别的账号就加 `--account <UUID>`。

## 安全设计

- 导出用 SQLite `backup` API 生成一致性快照，不会拷到写了一半的库；
- 写库前先 `PRAGMA integrity_check`；
- 改库前把 `workbuddy.db` 备份到 `.wb-tool-backup/<时间戳>/`；
- 覆盖任何本机文件前都会先备份到同一目录；
- 客户端仍在运行直接拒绝执行（`--force` 才跳过；`--dry-run` 不受限）；
- 侧边栏快照用临时文件 + 原子替换写入。

## 回滚

```bash
# 关掉 WorkBuddy 后，把备份的库拷回去即可
cp ~/.workbuddy-ai/.wb-tool-backup/<时间戳>/workbuddy.db ~/.workbuddy-ai/workbuddy.db
```

## 限制

- 只处理本地会话（`transport=local`），云端同步的会话由服务端管理。
- `--replace` 只清空数据库里的会话记录，磁盘上旧的 `.jsonl` 不会删除（`verify` 会把它们列为孤儿文件）。
- `plugins/` 与 `binaries/` 与机器相关，默认不迁移，插件需在新机器重装。
- 迁移前后请保持 WorkBuddy 版本接近；表结构差异过大时工具会跳过缺失字段并给出提示。
