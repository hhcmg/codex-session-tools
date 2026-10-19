# 验证与发布

最近一次本地验证结果见 [验证记录](validation.md)。

## 本地验证

```powershell
python -m unittest discover -s tests -v
python -m codex_session_tools --help
python -m codex_session_tools cleanup --help
python -m codex_session_tools unlock --help
```

Windows 测试覆盖 GUI 和启动器；Linux 测试覆盖真实临时文件锁。Linux 下无图形桌面时 GUI 用例会跳过；有 Xvfb 时运行：

```sh
xvfb-run -a python -m unittest discover -s tests -v
```

部分受管沙箱限制默认 TEMP/TMP，导致 SQLite 无法打开临时文件。这是环境问题；可将临时目录设到当前工作区：

```powershell
New-Item -ItemType Directory -Force .test-temp | Out-Null
$env:TEMP = (Resolve-Path .test-temp).Path
$env:TMP = $env:TEMP
python -m unittest discover -s tests -v
```

`.test-temp/` 已忽略。普通 Windows 或 GitHub runner 不需要此调整。

## 必须保持的回归场景

- 正常单条删除、同编号不同主机隔离、非 ChatGPT 记录保护。
- 标题、时间、其他字段、主机、结构、数据库路径或文件身份变化时拒绝旧快照。
- 扫描 checkpoint 插入/更新，计数器异常、约束失败、二次进程检查失败时全事务回滚。
- 数据库缺失、异常字段、无法可靠检测进程时拒绝修改。
- GUI 删除成功但刷新失败时正确显示部分结果、清空过期选择；后台任务不阻塞 Tk。
- 未确认、取消、管道输入、只检查模式都不执行修改。
- 远端活动未知、任务未完成、PID/指纹/锁/历史变化时拒绝终止。
- SSH 超时、断连、损坏协议及 SIGTERM 后验证失败，均不能触发自动重试。
- 锁被重新获取、inode 被替换、历史哈希变化时不能宣称完整成功。
- PowerShell 参数透传、Unicode、空格、返回码和同一个远端协议。

测试只使用合成数据和测试进程，真实 SIGTERM 调用被模拟。不要用真实会话验证破坏性分支。

## GUI 验收

检查两个页签、初始空态、表格选中态、搜索、错误状态、长标题、窄窗口和确认弹窗。最小窗口为 900 × 760；各说明随窗口宽度换行，表格和结果区可滚动。截图只使用虚构数据，放在 `docs/images/`。

## 发布顺序

1. 运行 Windows 和 Linux 测试，确认跳过项符合平台限制。
2. 检查 README 的截图、命令、兼容范围、许可证和 CHANGELOG。
3. 检查待提交文件；不包含数据库、WAL/SHM、历史、日志、SSH 配置、`.env` 或真实服务器信息。
4. 上传仓库后查看 GitHub Actions 四个矩阵任务，不能仅凭配置文件声称 CI 通过。
5. 首次发布标记为预览版，使用 `v0.1.0` 标签，明确尚未完成真实账户的端到端验证。

可选的 wheel 构建命令：

```powershell
python -m pip wheel . --no-deps --wheel-dir dist
```

源码仓库和源码 ZIP 包含 Windows 启动脚本；wheel 通过 `codex-session-tools` 控制台入口运行，独立 PowerShell 脚本不安装到 Python 包中。构建和发布不是 GUI 运行依赖。
