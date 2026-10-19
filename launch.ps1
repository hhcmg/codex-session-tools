param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ToolArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Quote-NativeArgument([string]$Value) {
    # ProcessStartInfo uses the Windows CRT argument rules. Escape quotes and
    # trailing backslashes explicitly so Windows PowerShell 5.1 preserves them.
    $quoted = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $quoted = [regex]::Replace($quoted, '(\\+)$', '$1$1')
    return '"' + $quoted + '"'
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'codex_session_tools\__main__.py') -PathType Leaf)) {
        throw '缺少 codex_session_tools 包。请完整解压项目后再运行。'
    }
    $needGui = $ToolArguments.Count -eq 0 -or $ToolArguments[0] -eq 'gui'
    $candidates = @(
        @{ Name = 'py.exe'; Prefix = @('-3') },
        @{ Name = 'python.exe'; Prefix = @() },
        @{ Name = 'python3.exe'; Prefix = @() }
    )
    $selected = $null
    $versionFound = $false
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $command) { continue }
        # Avoid invoking the Microsoft Store placeholder when Python is absent.
        if ($command.Source -like '*\Microsoft\WindowsApps\python*.exe') { continue }
        try {
            $probe = @($candidate.Prefix) + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)')
            & $command.Source @probe 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { continue }
            $versionFound = $true
            if ($needGui) {
                $probe = @($candidate.Prefix) + @('-c', 'import tkinter as tk; root = tk.Tk(); root.withdraw(); root.destroy()')
                & $command.Source @probe 2>$null | Out-Null
                if ($LASTEXITCODE -ne 0) { continue }
            }
            $selected = @{ Command = $command.Source; Prefix = @($candidate.Prefix) }
            break
        } catch {
            continue
        }
    }
    if ($null -eq $selected) {
        if ($versionFound -and $needGui) {
            throw '已找到 Python 3.10+，但 Tkinter 不可用。请在 Python 安装器中选择 Modify，启用 Tcl/Tk and IDLE，再重新运行；命令行入口不需要 Tkinter。'
        }
        throw '未找到 Python 3.10 或更新版本。请从 https://www.python.org/downloads/windows/ 安装 Python，启用 Tcl/Tk and IDLE、Python Launcher 和 Add Python to PATH，再重新打开本工具。远端解锁也可使用无需本地 Python 的 Unlock-Codex.cmd。'
    }
    Push-Location -LiteralPath $PSScriptRoot
    try {
        # Do not pass user arguments through a shell or Invoke-Expression.
        $nativeArgs = @($selected.Prefix) + @('-X', 'utf8', '-m', 'codex_session_tools') + @($ToolArguments)
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $selected.Command
        $startInfo.Arguments = ($nativeArgs | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
        $startInfo.WorkingDirectory = $PSScriptRoot
        $startInfo.UseShellExecute = $false
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        try {
            if (-not $process.Start()) { throw '无法启动 Python。' }
            $process.WaitForExit()
            $exitStatus = $process.ExitCode
        } finally {
            $process.Dispose()
        }
    } finally {
        Pop-Location
    }
    exit $exitStatus
} catch {
    Write-Host ('无法启动 Codex 会话维护工具：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
