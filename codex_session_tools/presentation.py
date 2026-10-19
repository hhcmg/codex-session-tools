"""Display helpers shared by the GUI and command-line frontends."""

import datetime as dt
import re


def safe_text(value, fallback="—"):
    if value is None or value == "":
        return fallback
    # Titles and process arguments are data, not terminal control sequences.
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value))


def display_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "时间未知"
    try:
        return dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return "时间无效（需 Unix 秒）"


def local_details(row):
    return (
        f"标题：{safe_text(row.get('display_title'), '(无标题)')}\n"
        f"更新时间：{display_time(row.get('source_updated_at'))}（本机时区）\n"
        f"主机编号：{safe_text(row.get('host_id'))}\n"
        f"会话编号：{safe_text(row.get('thread_id'))}"
    )


def remote_details(snapshot):
    owner = snapshot.get("owner") or {}
    target = snapshot.get("target") or {}
    lines = [
        f"服务器：{safe_text(snapshot.get('server_hostname'))}",
        f"用户：{safe_text(snapshot.get('user'))} / UID {safe_text(snapshot.get('uid'))}",
        f"Codex 目录：{safe_text(snapshot.get('codex_home'))}",
        f"目标：{safe_text(target.get('title'))}",
        f"会话编号：{safe_text(snapshot.get('thread_id'))}",
        f"持锁 PID：{safe_text(owner.get('pid'))}",
        f"程序：{safe_text(owner.get('exe'))}",
        "",
        "此后端在该 Codex 目录中持锁的会话：",
    ]
    states = {"active": "未完成", "completed": "最后任务已结束", "unknown": "未知"}
    for item in snapshot.get("affected", []):
        lines.extend([
            f"  {safe_text(item.get('title'))}  [{states.get(item.get('lifecycle'), '未知')}]",
            f"  {safe_text(item.get('id'))}",
        ])
    if not snapshot.get("affected"):
        lines.append("  无已识别的持锁会话")
    for field, label in (("blockers", "禁止执行"), ("warnings", "提示")):
        for reason in snapshot.get(field, []):
            lines.append(f"{label}：{safe_text(reason)}")
    return "\n".join(lines)


def remote_result(result):
    status = result.get("status")
    if status == "already_unlocked":
        return "检查时写锁已释放，未向进程发送信号。请返回应用重试。", 0
    if status == "unlocked":
        if result.get("history_unchanged") is True:
            return "检查时写锁已释放，目标历史路径和 SHA-256 未变。请返回应用重试。", 0
        return "检查时写锁已释放，但目标历史发生变化或无法确认。请检查会话后再继续。", 3
    if status == "still_locked":
        message = "已发送 SIGTERM，但写锁仍存在或已被重新获取。请重新检查；不会继续结束其他进程。"
        if result.get("history_unchanged") is not True:
            message += " 目标历史发生变化或无法确认，请核对会话。"
        return message, 2
    if status == "verification_error":
        return "已发送 SIGTERM，但最终结果无法确认。请重新执行仅检查，不要直接重试解锁。", 2
    return "收到无法识别的执行结果。请重新执行仅检查，确认实际状态。", 2
