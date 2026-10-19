<#
.SYNOPSIS
Inspect a remote Codex session write lock and optionally request SIGTERM.
.DESCRIPTION
Requires Windows PowerShell 5.1+ and OpenSSH; no local Python installation.
The remote host needs Linux, Python 3.9+, and pidfd support. Unknown activity
blocks unlocking. No automatic retry, lock-file removal, or SIGKILL is used.
Logs are disabled unless -LogPath is provided. Logs contain server aliases,
thread IDs, process IDs and outcomes; review them before sharing.
.EXAMPLE
.\Unlock-Codex.ps1 -Server research-server -ThreadId 11111111-1111-4111-8111-111111111111 -InspectOnly
.EXAMPLE
.\Unlock-Codex.ps1 -Server research-server -ThreadId 11111111-1111-4111-8111-111111111111 -CodexHome /home/user/.codex
#>
[CmdletBinding()]
param(
    [string]$Server,
    [string]$ThreadId,
    [string]$CodexHome,
    [switch]$InspectOnly,
    [string]$LogPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $script:Utf8
$script:HelperPath = Join-Path $PSScriptRoot 'codex_session_tools\remote_helper.py'
$script:AuditPath = $null

function Write-Audit([string]$Message) {
    if (-not $script:AuditPath) { return }
    try {
        $line = ('{0:o} {1}' -f (Get-Date), $Message) + [Environment]::NewLine
        [System.IO.File]::AppendAllText($script:AuditPath, $line, $script:Utf8)
    } catch {
        Write-Warning ('无法写入可选日志：' + $_.Exception.Message)
    }
}

function Safe-Text([object]$Value, [int]$MaxLength = 0) {
    # Remote titles, paths, process arguments and SSH errors are untrusted text.
    $text = ([string]$Value -replace '[\x00-\x1f\x7f-\x9f]', ' ')
    if ($MaxLength -gt 0 -and $text.Length -gt $MaxLength) { return $text.Substring(0, $MaxLength) + '…' }
    return $text
}

function Has-Property([object]$Value, [string]$Name) {
    return $null -ne $Value -and $Value.PSObject.Properties.Name -contains $Name
}

function ConvertTo-ThreadId([string]$Value) {
    $value = $Value.Trim()
    if ($value.Contains('://')) {
        $uri = $null
        if (-not [Uri]::TryCreate($value, [UriKind]::Absolute, [ref]$uri) -or
            $uri.Scheme -ine 'codex' -or $uri.Authority -ine 'threads') {
            throw '会话链接必须采用 codex://threads/<UUID> 格式。'
        }
        # Use the original path so URI dot-segment/percent normalization cannot
        # turn an invalid path into an apparently valid UUID. Query/fragment are UI state.
        $link = [regex]::Match($value, '^codex://threads/(?<id>[^?#]*)(?:[?#].*)?$', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if (-not $link.Success) { throw '会话链接格式无效。' }
        $value = $link.Groups['id'].Value
        if ($value.EndsWith('/')) { $value = $value.Substring(0, $value.Length - 1) }
    }
    if ($value -notmatch '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
        throw 'thread_id 格式错误，请复制带连字符的完整 UUID。'
    }
    return $value.ToLowerInvariant()
}

function Invoke-Remote([hashtable]$Request) {
    $json = ConvertTo-Json -InputObject $Request -Depth 50 -Compress
    $encoded = [Convert]::ToBase64String($script:Utf8.GetBytes($json))
    $helper = [System.IO.File]::ReadAllText($script:HelperPath, $script:Utf8)
    $inputScript = "REQUEST_B64 = '$encoded'`n" + $helper + "`n"
    $mayHaveSignalled = $Request.action -eq 'unlock'
    $knownRefusal = $false
    $unknownSuffix = if ($mayHaveSignalled) { '执行结果未知；请重新运行 -InspectOnly 只读检查，不要自动重试解锁。' } else { '' }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = (Get-Command ssh.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    # Server is validated before this call; all other command arguments are fixed.
    # Request data travels inside base64 JSON on stdin, never in a remote shell command.
    $startInfo.Arguments = "-T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o ConnectionAttempts=1 -o ClearAllForwardings=yes -o ServerAliveInterval=10 -o ServerAliveCountMax=2 $Server python3 -"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = $script:Utf8
    $startInfo.StandardErrorEncoding = $script:Utf8
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw '无法启动 ssh.exe。' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.Write($inputScript)
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(60000)) {
            $process.Kill()
            throw ('SSH 操作超过 60 秒。' + $unknownSuffix)
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw ('SSH 失败：' + (Safe-Text $stderr 1200) + '。请确认别名、已验证的主机密钥、密钥或 ssh-agent 登录以及远端 python3。' + $unknownSuffix)
        }
        $prefix = 'CODEX_SESSION_JSON:'
        $jsonLines = @($stdout -split '\r?\n' | Where-Object { $_.StartsWith($prefix) })
        if ($jsonLines.Count -ne 1) { throw ('远端未返回唯一的有效结果。' + $unknownSuffix) }
        try {
            $envelope = $jsonLines[0].Substring($prefix.Length) | ConvertFrom-Json
        } catch {
            throw ('远端结果不是有效 JSON。' + $unknownSuffix)
        }
        if (-not (Has-Property $envelope 'version') -or
            ($envelope.version -isnot [int] -and $envelope.version -isnot [long]) -or $envelope.version -ne 1 -or
            -not (Has-Property $envelope 'ok') -or $envelope.ok -isnot [bool]) {
            throw ('远端协议版本或格式不受支持。' + $unknownSuffix)
        }
        if (-not $envelope.ok) {
            if (-not (Has-Property $envelope 'error') -or -not (Has-Property $envelope.error 'message') -or
                $envelope.error.message -isnot [string] -or -not (Has-Property $envelope.error 'outcome_unknown') -or
                $envelope.error.outcome_unknown -isnot [bool]) {
                throw ('远端错误结果格式不完整。' + $unknownSuffix)
            }
            $message = Safe-Text $envelope.error.message
            if ((Has-Property $envelope.error 'outcome_unknown') -and $envelope.error.outcome_unknown -is [bool]) {
                $knownRefusal = -not $envelope.error.outcome_unknown
            }
            if (-not $knownRefusal -and $mayHaveSignalled) {
                $message += ' 执行结果未知；请运行 -InspectOnly，不要自动重试解锁。'
            }
            throw $message
        }
        if (-not (Has-Property $envelope 'result') -or -not (Has-Property $envelope.result 'status') -or
            $envelope.result.status -isnot [string]) {
            throw ('远端结果缺少必要字段。' + $unknownSuffix)
        }
        $result = $envelope.result
        if ($mayHaveSignalled) {
            if ($result.status -notin @('already_unlocked', 'unlocked', 'still_locked', 'verification_error')) {
                throw ('远端执行状态无法识别。' + $unknownSuffix)
            }
            if ($result.status -eq 'verification_error') {
                if (-not (Has-Property $result 'message') -or $result.message -isnot [string]) {
                    throw ('远端验证结果缺少必要字段。' + $unknownSuffix)
                }
            } elseif ($result.status -ne 'already_unlocked') {
                if (-not (Has-Property $result 'history_unchanged') -or $result.history_unchanged -isnot [bool] -or
                    -not (Has-Property $result 'signalled_pid') -or
                    ($result.signalled_pid -isnot [int] -and $result.signalled_pid -isnot [long]) -or
                    -not (Has-Property $result 'signal') -or $result.signal -cne 'SIGTERM' -or
                    -not (Has-Property $result 'lock') -or -not (Has-Property $result.lock 'held') -or
                    $result.lock.held -isnot [bool] -or $result.lock.held -ne ($result.status -eq 'still_locked')) {
                    throw ('远端执行结果不完整或锁状态矛盾。' + $unknownSuffix)
                }
            }
        }
        return $result
    } catch {
        # A transport error after sending an unlock request cannot establish outcome.
        $message = $_.Exception.Message
        if ($mayHaveSignalled -and -not $knownRefusal -and $message -notlike '*只读检查*' -and $message -notlike '*-InspectOnly*') {
            $message += ' ' + $unknownSuffix
        }
        throw $message
    } finally {
        $process.Dispose()
    }
}

function Run-Unlock {
    Write-Host ''
    Write-Host 'Codex 会话维护工具 · 远端写锁处理' -ForegroundColor Cyan
    Write-Host '检查 → 展示影响范围 → 输入 UNLOCK PID → 复核并发送 SIGTERM → 验证'
    Write-Host ''
    if ($InspectOnly -and (-not $Server -or -not $ThreadId)) {
        throw '仅检查模式需要同时提供 -Server 和 -ThreadId。'
    }
    if (-not $Server) { $script:Server = (Read-Host '输入 SSH 服务器别名，例如 research-server').Trim() }
    if (-not $ThreadId) { $script:ThreadId = (Read-Host '输入 thread_id 或 codex://threads/<UUID> 链接').Trim() }
    $script:Server = $Server.Trim()
    $script:ThreadId = ConvertTo-ThreadId $ThreadId
    if ($Server -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
        throw '服务器必须是 SSH 别名或主机名，仅含字母、数字、点、下划线、连字符，并以字母或数字开头。用户名、端口请配置到 SSH config。'
    }
    if (-not (Test-Path -LiteralPath $script:HelperPath -PathType Leaf)) { throw '缺少 codex_session_tools/remote_helper.py，请保留完整项目目录。' }
    if ($LogPath) {
        $script:AuditPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LogPath)
        if (-not (Test-Path -LiteralPath ([System.IO.Path]::GetDirectoryName($script:AuditPath)) -PathType Container)) {
            throw '-LogPath 的父目录不存在，请先创建目录。'
        }
        Write-Host ('已启用日志：{0}（包含服务器、会话编号和 PID，分享前请脱敏）' -f $script:AuditPath)
    }
    $request = @{ action = 'inspect'; thread_id = $ThreadId }
    if ($CodexHome) { $request.codex_home = $CodexHome }
    Write-Host ('正在检查 {0}，请稍候……' -f $Server)
    $inspection = Invoke-Remote $request
    if ($inspection.status -notin @('locked', 'unlocked', 'blocked')) { throw ('未知的检查状态：' + (Safe-Text $inspection.status)) }
    Write-Host ('服务器：{0} / {1}，用户：{2} (UID {3})' -f $Server, (Safe-Text $inspection.server_hostname), (Safe-Text $inspection.user), $inspection.uid)
    Write-Host ('Codex 目录：{0}' -f (Safe-Text $inspection.codex_home))
    Write-Host ('目标：{0}' -f (Safe-Text $inspection.target.title))
    Write-Host ('thread_id：{0}' -f $ThreadId)
    if ($inspection.status -eq 'unlocked') {
        Write-Host '检查时没有进程持有这个写锁，无需结束任何进程。请在目标应用点击“重试”。' -ForegroundColor Green
        Write-Audit "server=$Server thread=$ThreadId result=already_unlocked"
        return 0
    }
    if ($null -ne $inspection.owner) {
        Write-Host ('持锁进程 PID：{0}' -f $inspection.owner.pid) -ForegroundColor Yellow
        if (Has-Property $inspection.owner 'exe') {
            Write-Host ('程序：{0}' -f (Safe-Text $inspection.owner.exe))
            Write-Host ('命令：{0}' -f (Safe-Text ($inspection.owner.argv -join ' ')))
        }
    }
    Write-Host ''
    Write-Host '该后端在所选 Codex 目录中持有的全部会话：' -ForegroundColor Yellow
    $unsafeActivity = @($inspection.affected).Count -eq 0
    foreach ($task in @($inspection.affected)) {
        $stateText = switch ($task.lifecycle) {
            'active' { $unsafeActivity = $true; '发现未完成任务，禁止解锁' }
            'completed' { '最后一项任务已结束' }
            default { $unsafeActivity = $true; '活动状态未知，禁止解锁' }
        }
        Write-Host ('  {0}  {1}' -f (Safe-Text $task.id), (Safe-Text $task.title))
        Write-Host ('    {0}；最后事件：{1} {2}' -f $stateText, (Safe-Text $task.last_event), (Safe-Text $task.last_time))
    }
    if ($inspection.ready -isnot [bool] -or -not $inspection.ready -or $unsafeActivity -or $null -eq $inspection.owner) {
        Write-Host '存在未完成任务、未知活动状态或未通过身份检查，本次不执行解锁：' -ForegroundColor Red
        foreach ($reason in @($inspection.blockers)) { Write-Host ('  ' + (Safe-Text $reason)) }
        Write-Host '请先在原应用中停止或等待任务完成。活动状态仍无法确认时，保持只读检查。'
        Write-Audit "server=$Server thread=$ThreadId result=blocked"
        return 2
    }
    if ($InspectOnly) {
        Write-Host '仅检查模式：未结束任何进程。' -ForegroundColor Cyan
        return 0
    }
    if ([Console]::IsInputRedirected) {
        throw '解锁需要交互式终端确认；不接受管道输入或自动确认。请使用终端，或加 -InspectOnly 仅检查。'
    }
    Write-Host ''
    Write-Host '操作会请求该 Codex 后端退出，它管理的其他会话也可能断开。' -ForegroundColor Yellow
    Write-Host '请先关闭原应用中的该会话，避免自动重新占锁。工具不删除历史或锁文件。'
    $required = 'UNLOCK ' + $inspection.owner.pid
    $answer = Read-Host ('确认可停止这个后端后，输入 {0}；直接回车取消' -f $required)
    if ($answer -cne $required) {
        Write-Host '已取消，未结束任何进程。'
        Write-Audit "server=$Server thread=$ThreadId result=cancelled"
        return 0
    }
    Write-Audit ('server={0} thread={1} confirmed_pid={2}' -f $Server, $ThreadId, $inspection.owner.pid)
    Write-Host '正在重新核对状态并执行……'
    $result = Invoke-Remote @{ action = 'unlock'; confirmed = $true; expected = $inspection }
    Write-Audit ('server={0} thread={1} result={2}' -f $Server, $ThreadId, (Safe-Text $result.status))
    if ($result.status -eq 'already_unlocked') {
        Write-Host '确认期间写锁已释放，未结束任何进程。请点击“重试”。' -ForegroundColor Green
        return 0
    }
    if ($result.status -eq 'verification_error') {
        Write-Warning ('已向 PID {0} 发送 SIGTERM，但无法确认最终结果：{1}' -f $result.signalled_pid, (Safe-Text $result.message))
        Write-Host '请重新执行 -InspectOnly 只读检查，不要自动重试解锁。'
        return 2
    }
    if ($result.status -notin @('unlocked', 'still_locked')) {
        throw '执行后的状态无法识别，结果未知。请运行 -InspectOnly，不要自动重试解锁。'
    }
    if ($result.history_unchanged -eq $true) {
        Write-Host '目标会话历史文件的路径和 SHA-256 与执行前相同（此验证只覆盖目标会话）。' -ForegroundColor Green
    } else {
        Write-Warning '目标会话历史在执行期间发生变化，不能宣称未变；请检查该会话。'
    }
    if ($result.status -eq 'unlocked') {
        Write-Host '检查时写锁已释放。请在要继续使用的应用中点击“重试”。' -ForegroundColor Green
        if ($result.history_unchanged -ne $true) { return 3 }
        return 0
    }
    Write-Warning ('已向 PID {0} 发送 SIGTERM，但锁仍被占用或已被重新获取；当前锁 PID：{1}。' -f $result.signalled_pid, (Safe-Text ($result.current_lock_pids -join ', ')))
    Write-Host '本次已停止。请运行 -InspectOnly；工具不会自动重试或升级为 kill -9。'
    return 2
}

try {
    $exitStatus = Run-Unlock
    exit $exitStatus
} catch {
    Write-Host ('未能完成：' + (Safe-Text $_.Exception.Message)) -ForegroundColor Red
    Write-Audit ('server={0} thread={1} result=error' -f (Safe-Text $Server), (Safe-Text $ThreadId))
    exit 1
}
