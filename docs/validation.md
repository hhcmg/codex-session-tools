# 本次验证记录

日期：2026-10-19。以下记录发布前的本地验证；远端 Windows / Ubuntu × Python 3.10 / 3.13 的最新结果见 [GitHub Actions](https://github.com/hhcmg/codex-session-tools/actions/workflows/tests.yml)。

| 环境 | 结果 | 跳过原因 |
| --- | --- | --- |
| Windows / Python 3.13 | 118 项，97 通过、21 跳过，零失败 | 21 项 Linux 专用文件锁与进程验证 |
| WSL Ubuntu 24.04 / Python 3.12.3 | 107 项，99 通过、8 跳过，零失败 | 未安装 Tkinter，整个 GUI 模块作为一项跳过；7 项 Windows PowerShell 测试跳过 |

Windows 覆盖两个 GUI 页签、确认与取消、后台任务、小窗口布局、过期选择、刷新失败、Python CLI 及 PowerShell 启动器。Linux 实际运行临时文件锁和 `/proc` 相关测试，所有终止信号调用均模拟。

GUI 已检查默认窗口和 900 × 760 最小窗口，截图使用虚构数据，见 `docs/images/`。本次没有通过真实会话执行删除或终止真实 Codex 后端。

另对本机默认目录数据库的主库 DDL 做过只读结构核对，必要结构通过；不含 WAL 中的未合并变更，不构成应用版本端到端兼容保证。

打包通过本机 setuptools 构建后端生成 wheel，并检查包内远端助手与控制台入口。源代码 ZIP 由 Git 未忽略的项目文件生成，排除数据库、缓存、测试临时目录和 Git 元数据。
