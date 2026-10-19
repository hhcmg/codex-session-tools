# 兼容性与数据边界

## 支持范围

| 部分 | 环境 |
| --- | --- |
| 统一 GUI / Python CLI | Python 3.10+；GUI 另需 Tkinter |
| 本地写入 | Windows；能可靠查询进程，Codex / ChatGPT 已完全退出 |
| 独立远端 PowerShell 入口 | Windows PowerShell 5.1+、OpenSSH，无需本机 Python |
| 远端助手 | Linux、Python 3.9+、`/proc`、`fcntl.flock`、`os.pidfd_open` 和 `signal.pidfd_send_signal` |

Linux 可运行 Python CLI 的远端功能；本地删除不会在非 Windows 系统绕过进程检查。Python 3.10/3.13 是 CI 目标，不能据此宣称所有 Python、Linux 或 Codex 版本均已实测。

## 本地数据库

目前处理 `sqlite/codex-dev.db` 中以下目录表：

- `local_thread_catalog_hosts`
- `local_thread_catalog`
- `local_thread_catalog_sync_state`
- `local_thread_catalog_metadata`
- `local_thread_catalog_scan_checkpoints`
- `local_thread_catalog_scan_entries`

代码要求必要列、预期唯一键、整型计数器，以及不会造成未预期副作用的写入结构。目录触发器、相关外键级联、不明确的唯一键或冲突策略等会使检查拒绝通过，即使某些未来版本可能合法使用它们。

读取快照绑定数据库来源、文件身份、完整表结构、主机记录与目标目录行。删除时重新检查所有快照，在同一事务中处理扫描标记、版本计数和删除。缺失数据库不会被自动创建。

这些检查验证的是已知结构与操作前提，不是官方支持承诺，也不能从表结构推断同步机制完全未变。没有修改其他会话正文、云端会话或 Codex 任务的代码路径。当前不提供备份和恢复；不要将它当作常规会话删除工具。

开发期间仅对本机主数据库做过只读 DDL 核对，必要结构通过。由于该次核对使用 immutable 主库快照，不包含尚在 WAL 中的结构变更，也没有执行真实清理，因此不记录为某个 Codex 版本的端到端验证。

## 远端数据库与文件

远端只使用 `CODEX_HOME` 下编号最大的 `state_N.sqlite`，不从旧数据库补找缺失记录。最新库不兼容时停止，避免混用不同版本的状态。

只读查询 `threads` 表的 `id`、`title` 和 `rollout_path`。历史路径必须位于指定 Codex 目录内，且为当前用户拥有的 JSONL 文件。活动判断只识别近期日志中的已知生命周期事件；无法可靠判断时阻止执行。

写锁采用 `thread-writer-locks/<UUID>.lock`，以实际 FLOCK、inode 和内核锁信息识别持锁者。仅允许同一用户拥有的 `codex app-server` 后端。检查范围是当前 Codex 目录中该后端持有的会话锁，不包含所有服务器或该后端可能执行的其他活动。

SIGTERM 请求后端退出，不保证马上退出或永久释放锁。目标历史 SHA-256 可能因为应用正常写入而改变；这种情况需要人工核对，不能直接推断为损坏。对其他会话只检查执行前状态，不声称对它们做了完整性验证。

## 记录新的兼容验证

请在可丢弃的测试环境中记录操作系统、Python 版本、Codex 版本、表结构和前后状态；不要公开数据库、JSONL 历史、真实标题、编号或主机地址。先补对应合成测试，再扩大支持范围。
