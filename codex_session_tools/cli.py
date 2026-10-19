"""Independent CLI frontends; importing this module does not import Tkinter."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from . import local_catalog, remote_client
from .presentation import local_details, remote_details, remote_result, safe_text


def configure_output():
    """Use UTF-8 for terminals and pipes without replacing host-owned streams."""
    for stream in (sys.stdout, sys.stderr):
        # pythonw can provide None; embeddings and test capture often use StringIO.
        # Neither has a byte encoding to change. Rewrapping .buffer would also
        # bypass capture and risk closing the original stream when collected.
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, TypeError, ValueError):
            # A custom stream may expose but not implement reconfiguration.
            # Preserve its text handling instead of assuming it has a .buffer.
            pass


def parser():
    root = argparse.ArgumentParser(description="Codex 会话维护工具（非官方）")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command")
    commands.add_parser("gui", help="启动两个页签的统一界面")
    cleanup = commands.add_parser("cleanup", help="检查/移除本机 ChatGPT 目录记录")
    cleanup.add_argument("--db", type=Path, default=local_catalog.default_db(), help="本机 codex-dev.db 路径")
    cleanup.add_argument("--query", default="", help="按标题筛选")
    cleanup.add_argument("--host-id", help="选择要移除的记录所属主机")
    cleanup.add_argument("--thread-id", help="选择要移除的记录编号")
    cleanup.add_argument("--remove", action="store_true", help="展示记录后交互确认移除；默认只列出")
    cleanup.add_argument("--json", action="store_true", help="仅检查：输出显示字段 JSON，不含内部快照")
    unlock = commands.add_parser("unlock", help="通过 SSH 检查/释放远端 Linux 会话写锁")
    unlock.add_argument("--server", help="SSH 配置别名")
    unlock.add_argument("--thread-id", help="UUID 或 codex://threads/ 链接")
    unlock.add_argument("--codex-home", help="远端 Codex 目录，默认使用远端环境")
    unlock.add_argument("--inspect-only", action="store_true", help="只检查，不询问或发送终止信号")
    unlock.add_argument("--json", action="store_true", help="只检查并输出 JSON（可能含标题和目录路径）")
    return root


def confirm(phrase, notice):
    if not sys.stdin.isatty():
        raise ValueError("修改操作需要交互式终端确认；不接受管道输入或自动确认。")
    print(notice)
    return input(f"输入 {phrase} 确认；其他输入取消：").strip() == phrase


def cleanup_command(args):
    if args.remove and args.json:
        raise ValueError("--json 只适用于检查，不能与 --remove 同用。")
    if args.remove and (not args.host_id or not args.thread_id):
        raise ValueError("移除需要同时提供 --host-id 和 --thread-id。")
    rows = local_catalog.list_entries(args.db)
    rows = [r for r in rows if args.query.casefold() in safe_text(r.get("display_title"), "").casefold()
            and (not args.host_id or r["host_id"] == args.host_id)
            and (not args.thread_id or r["thread_id"] == args.thread_id)]
    if args.json:
        print(json.dumps([{k: r.get(k) for k in ("host_id", "thread_id", "display_title", "source_updated_at")}
                          for r in rows], ensure_ascii=True, indent=2, default=safe_text))
        return 0
    print(f"数据库：{args.db.resolve()}\n找到 {len(rows)} 条目录记录；本工具无法判断云端是否已删除。")
    for row in rows:
        print("\n" + local_details(row))
        if not row.get("_deletable", True):
            print("禁止移除：" + safe_text(row.get("_blocked_reason")))
    if not args.remove:
        return 0
    if len(rows) != 1:
        raise ValueError("必须精确匹配一条记录，请检查主机编号和会话编号。")
    row = rows[0]
    if not row.get("_deletable", True):
        raise ValueError(safe_text(row.get("_blocked_reason")))
    notice = "请完全退出 Codex / ChatGPT。确认目标已在网页端删除；本工具不备份，也不提供恢复。"
    if not confirm("REMOVE " + row["thread_id"], notice):
        print("已取消，未修改数据库。")
        return 0
    local_catalog.remove_entry(args.db, row)
    print("已移除该本地目录记录。重新打开 Codex 检查；后续同步仍可能使其重新出现。")
    return 0


def unlock_command(args):
    if (args.inspect_only or args.json) and (not args.server or not args.thread_id):
        raise ValueError("仅检查模式需要 --server 和 --thread-id，不会交互索取参数。")
    server = args.server or input("SSH 服务器别名：").strip()
    tid = args.thread_id or input("会话编号或 codex://threads/ 链接：").strip()
    snapshot = remote_client.inspect_remote(server, tid, args.codex_home)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=True, indent=2))
        return 2 if snapshot.get("status") == "blocked" else 0
    print(remote_details(snapshot))
    if snapshot.get("status") == "unlocked":
        print("检查时未持有写锁，无需结束进程。")
        return 0
    if not snapshot.get("ready"):
        print("当前检查不允许执行解锁。")
        return 2
    if args.inspect_only:
        print("仅检查完成，未向进程发送信号。")
        return 0
    phrase = "UNLOCK " + str(snapshot["owner"]["pid"])
    if not confirm(phrase, "将向整个后端发送 SIGTERM；上面列出的其他会话可能断开。请关闭原应用中的目标会话，避免重新占锁。"):
        print("已取消，未向进程发送信号。")
        return 0
    result = remote_client.unlock_remote(server, snapshot)
    message, code = remote_result(result)
    print(message)
    return code


def main(argv=None):
    configure_output()
    args = parser().parse_args(argv)
    try:
        if args.command in (None, "gui"):
            from .gui import main as gui_main
            gui_main()
            return 0
        if args.command == "cleanup":
            return cleanup_command(args)
        return unlock_command(args)
    except remote_client.RemoteError as exc:
        print("远端操作未完成：" + safe_text(exc), file=sys.stderr)
        if exc.outcome_unknown:
            print("执行结果未知：请重新运行 --inspect-only；不要自动重试解锁。", file=sys.stderr)
            return 2
        return 1
    except (local_catalog.CatalogError, ValueError, OSError, EOFError) as exc:
        print("未能完成：" + safe_text(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作被中断。若已确认远端解锁，结果可能未知，请仅检查确认。", file=sys.stderr)
        return 130
