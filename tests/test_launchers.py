"""Windows launcher regressions. All remote processes and responses are fakes."""

import base64
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None
THREAD_ID = "aaaaaaaa-1111-4111-8111-111111111111"
MARKER = "TEST_RESULT:"


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def json_b64(value):
    return base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")


@unittest.skipUnless(POWERSHELL, "Windows PowerShell 5.1 is required")
class WindowsLauncherTests(unittest.TestCase):
    def run_ps(self, script):
        preamble = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)\n"
        encoded = base64.b64encode((preamble + script).encode("utf-16le")).decode("ascii")
        return subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )

    def result(self, script):
        process = self.run_ps(script)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        records = [line[len(MARKER):] for line in process.stdout.splitlines() if line.startswith(MARKER)]
        self.assertEqual(len(records), 1, process.stdout + process.stderr)
        return json.loads(records[0])

    def unlock_functions(self):
        # Extract function declarations without running the script's entry point.
        return """
$tokens=$null; $parseErrors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile(SOURCE,[ref]$tokens,[ref]$parseErrors)
if ($parseErrors.Count) { throw 'Invalid PowerShell source' }
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
Set-StrictMode -Version Latest
$Server='fixture-server'; $ThreadId='THREAD'; $CodexHome=$null; $InspectOnly=$true; $LogPath=$null
$script:Utf8=[Text.UTF8Encoding]::new($false)
$script:AuditPath=$null; $script:HelperPath=HELPER
""".replace("SOURCE", ps_literal(ROOT / "Unlock-Codex.ps1")).replace(
            "THREAD", THREAD_ID
        ).replace("HELPER", ps_literal(ROOT / "codex_session_tools" / "remote_helper.py"))

    def test_scripts_have_bom_and_parse_in_windows_powershell(self):
        paths = [ROOT / "launch.ps1", ROOT / "Unlock-Codex.ps1"]
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        script = "$paths=@(" + ",".join(map(ps_literal, paths)) + ")\n" + """
foreach ($path in $paths) {
    $tokens=$null; $parseErrors=$null
    [System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$parseErrors) | Out-Null
    if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
}
Write-Output ('TEST_RESULT:' + ($PSVersionTable.PSVersion.Major | ConvertTo-Json -Compress))
"""
        self.assertEqual(self.result(script), 5)

    def test_launcher_preserves_arguments_and_exit_code(self):
        scratch = ROOT / ".test-temp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="launcher-", dir=scratch) as directory:
            root = Path(directory).resolve()
            # Verify the recursive TemporaryDirectory cleanup stays inside this workspace.
            self.assertEqual(root.parent, scratch.resolve())
            shutil.copyfile(ROOT / "launch.ps1", root / "launch.ps1")
            package = root / "codex_session_tools"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "__main__.py").write_text(
                "import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False)); sys.exit(7)",
                encoding="utf-8",
            )
            arguments = ["cleanup", "--query", "中文 space \"quote\" & $(noop); apostrophe' \\", "", "tail\\\\"]
            script = "$forwardArgs=@(" + ",".join(map(ps_literal, arguments)) + "); & " + ps_literal(
                root / "launch.ps1"
            ) + " @forwardArgs; exit $LASTEXITCODE"
            process = self.run_ps(script)
            self.assertEqual(process.returncode, 7, process.stdout + process.stderr)
            self.assertEqual(json.loads(process.stdout), arguments)

    def test_launcher_resolves_py_selected_interpreter_before_forwarding(self):
        # CPython's Windows launcher consumes -3 from the raw command line only
        # when it is unquoted (PC/launcher2.c, parseCommandLine). A real executable
        # shim reproduces that distinction even on machines without py.exe.
        scratch = ROOT / ".test-temp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="launcher-py-", dir=scratch) as directory:
            root = Path(directory).resolve()
            self.assertEqual(root.parent, scratch.resolve())
            shutil.copyfile(ROOT / "launch.ps1", root / "launch.ps1")
            package = root / "codex_session_tools"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "__main__.py").write_text(
                "import json, sys; print(json.dumps({'argv': sys.argv[1:], 'executable': sys.executable}, "
                "ensure_ascii=False)); sys.exit(7)", encoding="utf-8",
            )
            shim_dir = root / "模拟 launcher with spaces"
            shim_dir.mkdir()
            shim = shim_dir / "py.exe"
            source = root / "LauncherShim.cs"
            source.write_text(r'''
using System;
using System.Diagnostics;
using System.IO;
public static class LauncherShim {
    public static int Main(string[] args) {
        string raw = Environment.CommandLine;
        int offset = raw[0] == '"' ? raw.IndexOf('"', 1) + 1 : raw.IndexOf(' ');
        if (offset < 0) offset = raw.Length;
        string tail = raw.Substring(offset).TrimStart();
        File.AppendAllText(Environment.GetEnvironmentVariable("CODEX_TEST_PY_LOG"), tail + "\n");
        if (!tail.StartsWith("-3 ", StringComparison.Ordinal)) {
            Console.Error.WriteLine("Unknown option: -3 (quoted launcher selector)");
            return 2;
        }
        ProcessStartInfo info = new ProcessStartInfo(
            Environment.GetEnvironmentVariable("CODEX_TEST_PYTHON"), tail.Substring(3));
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        using (Process child = Process.Start(info)) {
            string output = child.StandardOutput.ReadToEnd();
            string error = child.StandardError.ReadToEnd();
            child.WaitForExit();
            Console.Out.Write(output);
            Console.Error.Write(error);
            return child.ExitCode;
        }
    }
}
''', encoding="utf-8")
            log = root / "launcher-calls.txt"
            arguments = ["cleanup", "--query", "中文 \"quote\" & $(noop) \\", "", "tail\\\\"]
            script = "\n".join([
                "$env:CODEX_TEST_PYTHON=" + ps_literal(sys.executable),
                "$env:CODEX_TEST_PY_LOG=" + ps_literal(log),
                "$env:CODEX_TEST_PY_SHIM=" + ps_literal(shim),
                "Add-Type -TypeDefinition ([IO.File]::ReadAllText(" + ps_literal(source)
                + ")) -Language CSharp -OutputAssembly $env:CODEX_TEST_PY_SHIM -OutputType ConsoleApplication",
                # First prove the shim reproduces the raw quoted-selector failure.
                "$bad=[Diagnostics.ProcessStartInfo]::new($env:CODEX_TEST_PY_SHIM, '\"-3\" -c pass')",
                "$bad.UseShellExecute=$false; $bad.RedirectStandardError=$true; $bad.CreateNoWindow=$true",
                "$check=[Diagnostics.Process]::Start($bad); $errorText=$check.StandardError.ReadToEnd(); $check.WaitForExit()",
                "if ($check.ExitCode -ne 2 -or $errorText -notlike '*Unknown option: -3*') { throw 'Shim did not reproduce quoted-selector failure' }; $check.Dispose()",
                "function Get-Command([string]$Name, $CommandType, $ErrorAction) {",
                "    if ($Name -eq 'py.exe') { return [pscustomobject]@{Source=$env:CODEX_TEST_PY_SHIM} }",
                "    throw 'Unexpected interpreter fallback'",
                "}",
                "$forwardArgs=@(" + ",".join(map(ps_literal, arguments)) + ")",
                "& " + ps_literal(root / "launch.ps1") + " @forwardArgs; exit $LASTEXITCODE",
            ])
            process = self.run_ps(script)
            self.assertEqual(process.returncode, 7, process.stdout + process.stderr +
                             (log.read_text(encoding="utf-8-sig") if log.exists() else "No launcher log"))
            result = json.loads(process.stdout)
            self.assertEqual(result["argv"], arguments)
            self.assertEqual(Path(result["executable"]).resolve(), Path(sys.executable).resolve())
            calls = log.read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(len(calls), 2, calls)  # Failure demonstration, then one discovery probe.
            self.assertTrue(calls[0].startswith('"-3" '), calls)
            self.assertTrue(calls[1].startswith("-3 -c "), calls)

    def test_deep_links_normalize_and_reject_invalid_paths(self):
        valid = [THREAD_ID, THREAD_ID.upper(), "codex://threads/" + THREAD_ID,
                 "codex://threads/" + THREAD_ID + "/?view=review#details",
                 "CODEX://THREADS/" + THREAD_ID.upper() + "#details"]
        invalid = ["https://threads/" + THREAD_ID, "codex://user@threads/" + THREAD_ID,
                   "codex://threads:22/" + THREAD_ID, "codex://threads//" + THREAD_ID,
                   "codex://threads/other/../" + THREAD_ID,
                   "codex://threads/" + THREAD_ID + "/extra", "{" + THREAD_ID + "}",
                   THREAD_ID.replace("-", ""), "codex://threads/%61" + THREAD_ID[1:]]
        script = self.unlock_functions() + """
$fixture=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('FIXTURE')) | ConvertFrom-Json
$valid=@($fixture.valid | ForEach-Object { ConvertTo-ThreadId $_ })
$rejected=@($fixture.invalid | ForEach-Object { try { ConvertTo-ThreadId $_ | Out-Null; $false } catch { $true } })
Write-Output ('TEST_RESULT:' + (@{valid=$valid; rejected=$rejected} | ConvertTo-Json -Compress))
""".replace("FIXTURE", json_b64({"valid": valid, "invalid": invalid}))
        result = self.result(script)
        self.assertEqual(result["valid"], [THREAD_ID] * len(valid))
        self.assertEqual(result["rejected"], [True] * len(invalid))

    def test_inspection_never_mutates_and_blocks_unknown_activity(self):
        base = {"status": "locked", "server_hostname": "fixture-host", "user": "fixture-user", "uid": 1000,
                "codex_home": "/tmp/fictional-codex", "thread_id": THREAD_ID,
                "target": {"title": "Fixture title"}, "owner": {"pid": 1234},
                "affected": [{"id": THREAD_ID, "title": "Fixture title", "lifecycle": "completed",
                              "last_event": "task_complete", "last_time": "2026-01-01"}],
                "blockers": [], "ready": True}
        cases = [
            ("already free", "unlocked", None, False, "completed", 0),
            ("owner unknown", "blocked", None, False, "unknown", 2),
            ("unknown despite ready", "locked", {"pid": 1234}, True, "unknown", 2),
            ("active despite ready", "locked", {"pid": 1234}, True, "active", 2),
            ("safe inspect only", "locked", {"pid": 1234}, True, "completed", 0),
        ]
        for name, status, owner, ready, lifecycle, expected in cases:
            with self.subTest(name=name):
                fixture = copy.deepcopy(base)
                fixture.update(status=status, owner=owner, ready=ready)
                fixture["affected"][0]["lifecycle"] = lifecycle
                script = self.unlock_functions() + """
$fixture=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('FIXTURE')) | ConvertFrom-Json
$script:Calls=0
function Invoke-Remote([hashtable]$Request) {
    $script:Calls++
    if ($Request.action -ne 'inspect') { throw 'Unexpected mutation' }
    return $fixture
}
$code=Run-Unlock
Write-Output ('TEST_RESULT:' + (@{code=$code; calls=$script:Calls; log=$script:AuditPath} | ConvertTo-Json -Compress))
""".replace("FIXTURE", json_b64(fixture))
                self.assertEqual(self.result(script), {"code": expected, "calls": 1, "log": None})

    def protocol_result(self, stdout, *, exit_code=0, wait=True, action="unlock"):
        fixture = {"stdout": stdout, "stderr": "fixture stderr", "exit_code": exit_code, "wait": wait, "action": action}
        script = self.unlock_functions() + """
$fixture=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('FIXTURE')) | ConvertFrom-Json
$script:Starts=0; $script:Kills=0; $script:WrittenInput=''
$outSource=[System.Threading.Tasks.TaskCompletionSource[string]]::new()
$outSource.SetResult([string]$fixture.stdout); $script:OutTask=$outSource.Task
$errSource=[System.Threading.Tasks.TaskCompletionSource[string]]::new()
$errSource.SetResult([string]$fixture.stderr); $script:ErrTask=$errSource.Task
$inputStub=[pscustomobject]@{}
$inputStub | Add-Member ScriptMethod Write { param($Text) $script:WrittenInput += [string]$Text }
$inputStub | Add-Member ScriptMethod Close {}
$outputStub=[pscustomobject]@{}
$outputStub | Add-Member ScriptMethod ReadToEndAsync { return $script:OutTask }
$errorStub=[pscustomobject]@{}
$errorStub | Add-Member ScriptMethod ReadToEndAsync { return $script:ErrTask }
$script:FakeProcess=[pscustomobject]@{StartInfo=$null; StandardInput=$inputStub; StandardOutput=$outputStub; StandardError=$errorStub; ExitCode=[int]$fixture.exit_code}
$script:FakeProcess | Add-Member ScriptMethod Start { $script:Starts++; return $true }
$script:FakeProcess | Add-Member ScriptMethod WaitForExit { param($Timeout) return [bool]$fixture.wait }
$script:FakeProcess | Add-Member ScriptMethod Kill { $script:Kills++ }
$script:FakeProcess | Add-Member ScriptMethod Dispose {}
# Intercept process construction; no executable is started, even for timeout tests.
function New-Object([string]$TypeName) {
    if ($TypeName -eq 'System.Diagnostics.Process') { return $script:FakeProcess }
    return Microsoft.PowerShell.Utility\\New-Object -TypeName $TypeName
}
function Get-Command([string]$Name, $CommandType, $ErrorAction) {
    if ($Name -ne 'ssh.exe') { throw 'Unexpected executable lookup' }
    return [pscustomobject]@{Source='fixture-ssh.exe'}
}
try { $result=Invoke-Remote @{action=[string]$fixture.action}; $outcome=@{ok=$true; result=$result} }
catch { $outcome=@{ok=$false; error=$_.Exception.Message} }
$outcome.starts=$script:Starts; $outcome.kills=$script:Kills
$outcome.wroteRequest=$script:WrittenInput.StartsWith("REQUEST_B64 = '")
Write-Output ('TEST_RESULT:' + ($outcome | ConvertTo-Json -Depth 10 -Compress))
""".replace("FIXTURE", json_b64(fixture))
        return self.result(script)

    def test_protocol_accepts_version_one_and_explicit_unknown_verification(self):
        for result in ({"status": "unlocked", "history_unchanged": True, "signalled_pid": 1234,
                        "signal": "SIGTERM", "lock": {"held": False}},
                       {"status": "verification_error", "outcome_unknown": True, "signalled_pid": 1234,
                        "message": "Fixture verification failed"}):
            with self.subTest(status=result["status"]):
                record = "CODEX_SESSION_JSON:" + json.dumps({"version": 1, "ok": True, "result": result})
                self.assertEqual(self.protocol_result(record),
                                 {"ok": True, "result": result, "starts": 1, "kills": 0, "wroteRequest": True})

    def test_protocol_refusal_preserves_known_or_unknown_outcome(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                record = "CODEX_SESSION_JSON:" + json.dumps({"version": 1, "ok": False,
                    "error": {"message": "Fixture refusal", "outcome_unknown": unknown}})
                result = self.protocol_result(record)
                self.assertFalse(result["ok"])
                self.assertIn("Fixture refusal", result["error"])
                self.assertEqual("-InspectOnly" in result["error"], unknown)
                self.assertEqual(result["starts"], 1)

    def test_transport_and_protocol_failures_are_unknown_without_retry(self):
        cases = [("not JSON", 0, True),
                 ('CODEX_SESSION_JSON:{"version":2,"ok":true}', 0, True),
                 ('CODEX_SESSION_JSON:{"version":"1","ok":true}', 0, True),
                 ("CODEX_SESSION_JSON:{broken", 0, True),
                 ('CODEX_SESSION_JSON:{}\nCODEX_SESSION_JSON:{}', 0, True),
                 ('CODEX_SESSION_JSON:{"version":1,"ok":false,"error":{"message":"missing outcome"}}', 0, True),
                 ('CODEX_SESSION_JSON:{"version":1,"ok":true,"result":{"status":"unlocked"}}', 0, True),
                 ('CODEX_SESSION_JSON:{"version":1,"ok":true,"result":{"status":"unlocked",'
                  '"history_unchanged":true,"signalled_pid":1234,"signal":"SIGTERM","lock":{"held":true}}}', 0, True),
                 ("", 255, True), ("", 0, False)]
        for stdout, exit_code, wait in cases:
            with self.subTest(stdout=stdout, exit_code=exit_code, wait=wait):
                result = self.protocol_result(stdout, exit_code=exit_code, wait=wait)
                self.assertFalse(result["ok"])
                self.assertIn("-InspectOnly", result["error"])
                self.assertEqual(result["starts"], 1)
                self.assertEqual(result["kills"], 0 if wait else 1)
                self.assertTrue(result["wroteRequest"])


if __name__ == "__main__":
    unittest.main()
