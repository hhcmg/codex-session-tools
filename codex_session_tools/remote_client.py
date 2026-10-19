"""SSH transport for the standalone Linux helper (safe to import on Windows)."""

import base64
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
import uuid


PROTOCOL_VERSION = 1
RESPONSE_PREFIX = "CODEX_SESSION_JSON:"
SSH_TIMEOUT_SECONDS = 60
_SERVER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}\Z", re.ASCII)


class RemoteError(RuntimeError):
    """A remote failure; unknown outcomes must be inspected before any retry."""

    def __init__(self, message, *, outcome_unknown=False):
        self.outcome_unknown = bool(outcome_unknown)
        if self.outcome_unknown:
            message += " 解锁结果未知，请重新检查状态；不要自动重试解锁。"
        super().__init__(message)


def normalize_thread_id(value):
    """Accept a canonical UUID or a codex://threads/<UUID> link."""
    if not isinstance(value, str):
        raise RemoteError("会话编号必须是 UUID 或 codex://threads/ 链接。")
    value = value.strip()
    if value.lower().startswith("codex://"):
        try:
            link = urlsplit(value)
        except ValueError as exc:
            raise RemoteError("会话链接格式无效。") from exc
        if link.scheme.lower() != "codex" or link.netloc.lower() != "threads":
            raise RemoteError("会话链接必须采用 codex://threads/<UUID> 格式。")
        value = link.path.removeprefix("/").removesuffix("/")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise RemoteError("会话编号必须是完整 UUID。") from exc
    # Do not accept UUID forms without hyphens, braces, URNs or extra path segments.
    if str(parsed) != value.lower():
        raise RemoteError("会话编号必须使用带连字符的完整 UUID。")
    return str(parsed)


def validate_server(alias):
    """Only SSH config aliases/hostnames; user names and ports belong in SSH config."""
    if not isinstance(alias, str) or not _SERVER.fullmatch(alias.strip()):
        raise RemoteError("请输入 SSH 配置中的服务器别名或主机名（字母、数字、点、下划线和连字符）。")
    return alias.strip()


def _parse_response(stdout, *, unlocking):
    if not isinstance(stdout, str):
        raise RemoteError("SSH 返回了无法解码的响应。", outcome_unknown=unlocking)
    records = [line[len(RESPONSE_PREFIX):] for line in stdout.splitlines()
               if line.startswith(RESPONSE_PREFIX)]
    if len(records) != 1:
        raise RemoteError("远端响应缺失或重复。", outcome_unknown=unlocking)
    try:
        envelope = json.loads(records[0])
        if not isinstance(envelope, dict) or type(envelope.get("version")) is not int:
            raise ValueError("invalid envelope")
        if envelope["version"] != PROTOCOL_VERSION or type(envelope.get("ok")) is not bool:
            raise ValueError("unsupported protocol")
        if not envelope["ok"]:
            error = envelope.get("error")
            if (not isinstance(error, dict) or not isinstance(error.get("message"), str)
                    or type(error.get("outcome_unknown")) is not bool):
                raise ValueError("invalid error")
            raise RemoteError(error["message"], outcome_unknown=error["outcome_unknown"])
        result = envelope.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise ValueError("invalid result")
        if unlocking:
            if result["status"] not in {"already_unlocked", "unlocked", "still_locked", "verification_error"}:
                raise ValueError("invalid unlock status")
            if result["status"] == "verification_error":
                raise RemoteError(result.get("message", "已发送 SIGTERM，但无法验证结果。"), outcome_unknown=True)
            if result["status"] != "already_unlocked":
                if (type(result.get("history_unchanged")) is not bool
                        or type(result.get("signalled_pid")) is not int
                        or result.get("signal") != "SIGTERM"
                        or not isinstance(result.get("lock"), dict)
                        or type(result["lock"].get("held")) is not bool):
                    raise ValueError("incomplete unlock result")
                if result["lock"]["held"] != (result["status"] == "still_locked"):
                    raise ValueError("inconsistent lock result")
        else:
            _validate_snapshot(result)
        return result
    except RemoteError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RemoteError("远端协议响应格式损坏或版本不兼容。", outcome_unknown=unlocking) from exc


def _validate_snapshot(result):
    """Reject incomplete safety data before presenting an executable action."""
    if (result.get("status") not in {"unlocked", "locked", "blocked"}
            or type(result.get("ready")) is not bool
            or not isinstance(result.get("blockers"), list)
            or not all(isinstance(x, str) for x in result["blockers"])
            or not isinstance(result.get("warnings"), list)
            or not all(isinstance(x, str) for x in result["warnings"])
            or not isinstance(result.get("affected"), list)
            or not isinstance(result.get("target"), dict)
            or not isinstance(result.get("lock"), dict)
            or type(result["lock"].get("held")) is not bool
            or not isinstance(result.get("codex_home"), str)
            or not isinstance(result.get("thread_id"), str)):
        raise ValueError("incomplete snapshot")
    normalize_thread_id(result["thread_id"])
    if ((result["status"] == "unlocked" and result["lock"]["held"])
            or (result["status"] in {"locked", "blocked"} and not result["lock"]["held"])):
        raise ValueError("inconsistent inspection lock state")
    for affected in result["affected"]:
        if (not isinstance(affected, dict) or not isinstance(affected.get("id"), str)
                or affected.get("lifecycle") not in {"active", "completed", "unknown"}):
            raise ValueError("invalid affected session")
    if result["ready"]:
        owner = result.get("owner")
        if (result["status"] != "locked" or result["blockers"] or not result["lock"]["held"]
                or not isinstance(owner, dict) or type(owner.get("pid")) is not int
                or owner["pid"] <= 0 or not result["affected"]
                or any(item["lifecycle"] != "completed" for item in result["affected"])
                or result["thread_id"] not in {item["id"] for item in result["affected"]}
                or not isinstance(result.get("fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["fingerprint"])):
            raise ValueError("unsafe ready snapshot")
    elif result["status"] == "locked":
        raise ValueError("inconsistent readiness")


def _call(server, request):
    server = validate_server(server)
    unlocking = request["action"] == "unlock"
    try:
        helper = Path(__file__).with_name("remote_helper.py").read_text(encoding="utf-8")
        encoded = base64.b64encode(json.dumps(request, ensure_ascii=True).encode("utf-8")).decode("ascii")
    except (OSError, TypeError, ValueError) as exc:
        raise RemoteError("无法准备远端助手：" + str(exc)) from exc
    script = "REQUEST_B64 = '" + encoded + "'\n" + helper
    args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ClearAllForwardings=yes", "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=yes", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", server, "python3 -"]
    kwargs = {"input": script, "text": True, "encoding": "utf-8", "errors": "replace",
              "capture_output": True, "timeout": SSH_TIMEOUT_SECONDS, "shell": False}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(args, **kwargs)
    except FileNotFoundError as exc:
        raise RemoteError("找不到 ssh，请安装或启用 OpenSSH 客户端。") from exc
    except subprocess.TimeoutExpired as exc:
        raise RemoteError("SSH 操作超时。", outcome_unknown=unlocking) from exc
    except OSError as exc:
        raise RemoteError("SSH 无法启动或连接中断：" + str(exc), outcome_unknown=unlocking) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "SSH 未提供错误信息。").strip()[-2000:]
        raise RemoteError("SSH 失败：" + detail, outcome_unknown=unlocking)
    result = _parse_response(completed.stdout, unlocking=unlocking)
    if not unlocking and result["thread_id"] != request["thread_id"]:
        raise RemoteError("远端检查返回了不同的会话编号，请重新检查。")
    if (unlocking and result["status"] != "already_unlocked"
            and result["signalled_pid"] != request["expected"]["owner"]["pid"]):
        raise RemoteError("执行结果中的 PID 与确认对象不符。", outcome_unknown=True)
    return result


def inspect_remote(server, thread_id, codex_home=None):
    request = {"action": "inspect", "thread_id": normalize_thread_id(thread_id)}
    if codex_home is not None and str(codex_home).strip():
        request["codex_home"] = str(codex_home).strip()
    return _call(server, request)


def unlock_remote(server, snapshot):
    """Execute only after the caller has explicitly confirmed this exact snapshot."""
    try:
        _validate_snapshot(snapshot)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RemoteError("检查结果不完整，请重新检查。") from exc
    if not snapshot["ready"]:
        raise RemoteError("当前检查结果不允许解锁，请重新检查并处理阻止原因。")
    return _call(server, {"action": "unlock", "confirmed": True, "expected": snapshot})
