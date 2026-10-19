"""Conservative, offline edits of Codex's local ChatGPT catalog.

This is an unofficial structural compatibility check, not a supported Codex API.
Only catalog rows, their counters and resumable-scan tombstones are written.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Callable
import unicodedata


class CatalogError(RuntimeError):
    """An unsafe, stale or unsupported catalog operation was refused."""


_TABLES = {
    "local_thread_catalog_hosts": {"host_id", "host_kind"},
    "local_thread_catalog": {
        "host_id", "thread_id", "display_title", "source_updated_at"
    },
    "local_thread_catalog_sync_state": {"host_id", "observation_sequence"},
    "local_thread_catalog_metadata": {"id", "catalog_revision"},
    "local_thread_catalog_scan_checkpoints": {"host_id"},
    "local_thread_catalog_scan_entries": {"host_id", "thread_id", "removed"},
}
_KEYS = {
    "local_thread_catalog_hosts": ("host_id",),
    "local_thread_catalog": ("host_id", "thread_id"),
    "local_thread_catalog_sync_state": ("host_id",),
    "local_thread_catalog_metadata": ("id",),
    "local_thread_catalog_scan_checkpoints": ("host_id",),
    "local_thread_catalog_scan_entries": ("host_id", "thread_id"),
}
_MAX_INTEGER = (1 << 63) - 1
_STALE = "记录或数据库结构已发生变化，请刷新列表后重新选择。"


def default_db() -> Path:
    """Return the configured catalog path without opening or creating it."""
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sqlite" / "codex-dev.db"


def running_apps() -> list[str]:
    """Fail closed if Windows process enumeration cannot be completed."""
    if sys.platform != "win32":
        raise CatalogError("本地清理目前只支持 Windows，无法在此系统核实应用已退出。")
    command = (
        "$ErrorActionPreference='Stop'; "
        "Get-Process | Select-Object -ExpandProperty ProcessName"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CatalogError("无法检查应用是否退出；未执行清理，请稍后重试。") from exc
    if result.returncode:
        raise CatalogError("无法检查应用是否退出；未执行清理，请稍后重试。")
    names = {line.strip().lower() for line in result.stdout.decode("utf-8", errors="replace").splitlines()}
    if not any(names):
        raise CatalogError("应用进程列表为空，无法可靠确认退出状态；未执行清理。")
    return sorted(names & {"codex", "chatgpt"})


def _require_closed(check: Callable[[], list[str]]) -> None:
    try:
        apps = check()
    except CatalogError:
        raise
    except Exception as exc:
        raise CatalogError("应用状态检查失败；未执行清理。") from exc
    if not isinstance(apps, (list, tuple, set)) or any(not isinstance(item, str) for item in apps):
        raise CatalogError("应用状态检查未返回可靠结果；未执行清理。")
    if apps:
        raise CatalogError("请先完全退出 Codex / ChatGPT 桌面应用。仅关闭窗口可能仍会在后台运行。")


def _connect(db: str | Path, *, write: bool = False) -> sqlite3.Connection:
    # URI mode is essential: sqlite3.connect(path) silently creates a missing DB.
    try:
        path = Path(db).expanduser().resolve()
        conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                               uri=True, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        if not write:
            conn.execute("PRAGMA query_only=ON")
        return conn
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise CatalogError("无法打开现有目录数据库。请检查路径、文件权限及数据库是否被占用。") from exc


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {row[1]: tuple(row) for row in conn.execute(f"PRAGMA table_xinfo({_quoted(table)})")}


def _has_unique_key(conn: sqlite3.Connection, table: str, key: tuple[str, ...], columns: dict) -> bool:
    primary = tuple(name for name, col in sorted(columns.items(), key=lambda item: item[1][5]) if col[5])
    # INTEGER PRIMARY KEY has no index_list entry; other primary keys do.
    if primary == key and len(key) == 1 and columns[key[0]][2].upper().strip() == "INTEGER":
        return True
    for index in conn.execute(f"PRAGMA index_list({_quoted(table)})"):
        if not index[2] or index[4]:  # not unique, or partial
            continue
        parts = [tuple(row) for row in conn.execute(f"PRAGMA index_xinfo({_quoted(index[1])})") if row[5]]
        if len(parts) != len(key) or {row[2] for row in parts} != set(key):
            continue
        if all(row[1] >= 0 and str(row[4]).upper() == "BINARY" for row in parts):
            return True
    return False


def validate_schema(conn: sqlite3.Connection) -> None:
    """Require known tables, unambiguous keys and safe write semantics.

    Extra ordinary columns are allowed. A new scan tombstone can only be inserted
    if any additional NOT NULL columns have defaults; this is checked when needed.
    Incoming cascading foreign keys and triggers can modify unrelated data, so
    they are rejected even though their presence could be legitimate in Codex.
    """
    for table, required in _TABLES.items():
        definition = conn.execute("SELECT type, sql FROM sqlite_schema WHERE name=?", (table,)).fetchone()
        if definition is None or definition[0] != "table" or not definition[1]:
            raise CatalogError(f"数据库结构不受支持：缺少普通表 {table}。")
        if "VIRTUAL TABLE" in definition[1].upper():
            raise CatalogError(f"数据库结构不受支持：{table} 不是普通目录表。")
        if re.search(r"\bON\s+CONFLICT\s+(?:REPLACE|IGNORE)\b", definition[1], re.IGNORECASE):
            raise CatalogError(f"数据库结构不受支持：{table} 具有可能静默替换或忽略写入的冲突策略。")
        columns = _columns(conn, table)
        missing = sorted(required - columns.keys())
        if missing:
            raise CatalogError(f"数据库结构不受支持：{table} 缺少字段 {', '.join(missing)}。")
        if any(columns[name][6] for name in required):
            raise CatalogError(f"数据库结构不受支持：{table} 的必要字段不能是生成列。")
        if not _has_unique_key(conn, table, _KEYS[table], columns):
            raise CatalogError(f"数据库结构不受支持：{table} 缺少预期的完整唯一键。")
    for table, counter in (("local_thread_catalog_sync_state", "observation_sequence"),
                           ("local_thread_catalog_metadata", "catalog_revision")):
        col = _columns(conn, table)[counter]
        if "INT" not in col[2].upper() or not col[3]:
            raise CatalogError(f"数据库结构不受支持：{counter} 必须是非空整型计数器。")
    removed = _columns(conn, "local_thread_catalog_scan_entries")["removed"]
    if "INT" not in removed[2].upper():
        raise CatalogError("数据库结构不受支持：removed 必须是整型标记。")
    for row in conn.execute("SELECT name, tbl_name FROM sqlite_schema WHERE type='trigger'"):
        if row[1] in _TABLES:
            raise CatalogError(f"数据库结构不受支持：目录表存在触发器 {row[0]}，已拒绝修改。")
    # Deletion and counter/tombstone updates must not cascade into other data.
    written = {"local_thread_catalog", "local_thread_catalog_sync_state",
               "local_thread_catalog_metadata", "local_thread_catalog_scan_entries"}
    for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
        for foreign in conn.execute(f"PRAGMA foreign_key_list({_quoted(row[0])})"):
            referenced = str(foreign[2]).casefold()
            delete_cascade = referenced == "local_thread_catalog" and foreign[6].upper() not in {"NO ACTION", "RESTRICT"}
            update_cascade = referenced in written and foreign[5].upper() not in {"NO ACTION", "RESTRICT"}
            if delete_cascade or update_cascade:
                raise CatalogError("数据库结构不受支持：删除目录记录可能触发外键级联修改。")


def _schema_signature(conn: sqlite3.Connection) -> str:
    rows = [tuple(row) for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
    )]
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _snapshot(row: dict) -> str:
    # Preserve every SQLite value and type, including NULL and BLOB, without
    # storing history content or relying on display formatting for comparison.
    values = [(name, type(value).__name__, value.hex() if isinstance(value, bytes) else value)
              for name, value in sorted(row.items())]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


def _database_snapshot(db: str | Path) -> str:
    try:
        path = Path(db).expanduser().resolve()
        info = path.stat()
    except (OSError, ValueError) as exc:
        raise CatalogError("无法访问所选数据库文件，请重新检查路径并刷新列表。") from exc
    identity = (os.path.normcase(str(path)), info.st_dev, info.st_ino)
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()


def _valid_identity(value: object) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and not any(unicodedata.category(character) in {"Cc", "Cf"} for character in value))


def _counter_value(conn: sqlite3.Connection, table: str, key: str, value: object, counter: str) -> int:
    rows = conn.execute(f"SELECT {_quoted(counter)} FROM {_quoted(table)} WHERE {_quoted(key)}=? COLLATE BINARY", (value,)).fetchall()
    if len(rows) != 1 or type(rows[0][0]) is not int or not 0 <= rows[0][0] < _MAX_INTEGER:
        raise CatalogError(f"目录数据异常：{counter} 缺失、不是有效整数或无法继续递增；已禁止清理。")
    return rows[0][0]


def _host_row(conn: sqlite3.Connection, host_id: object) -> dict | None:
    rows = conn.execute("SELECT * FROM local_thread_catalog_hosts WHERE host_id=? COLLATE BINARY", (host_id,)).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def _check_target_data(conn: sqlite3.Connection, row: dict) -> None:
    if not _valid_identity(row.get("host_id")) or not _valid_identity(row.get("thread_id")):
        raise CatalogError("目录记录缺少明确的主机或会话编号；已禁止清理。")
    host = _host_row(conn, row["host_id"])
    if host is None or host.get("host_kind") != "chatgpt":
        raise CatalogError("目标不是明确的 ChatGPT 目录记录；已禁止清理。")
    _counter_value(conn, "local_thread_catalog_sync_state", "host_id", row["host_id"], "observation_sequence")
    _counter_value(conn, "local_thread_catalog_metadata", "id", 1, "catalog_revision")


def list_entries(db: str | Path) -> list[dict]:
    """Read a consistent catalog snapshot; never create or write the database.

    Preserve each returned dictionary for remove_entry. ``_deletable`` and
    ``_blocked_reason`` allow a UI to display malformed data without enabling it.
    NULL titles/timestamps are preserved for the caller to format safely.
    """
    database_snapshot = _database_snapshot(db)
    conn = _connect(db)
    try:
        conn.execute("BEGIN")
        validate_schema(conn)
        signature = _schema_signature(conn)
        rows = conn.execute("""SELECT t.* FROM local_thread_catalog t
            WHERE EXISTS (SELECT 1 FROM local_thread_catalog_hosts h
                WHERE h.host_id=t.host_id COLLATE BINARY AND h.host_kind='chatgpt' COLLATE BINARY)
            ORDER BY t.source_updated_at DESC, t.host_id, t.thread_id""").fetchall()
        result = []
        for record in rows:
            row = dict(record)
            host = _host_row(conn, row["host_id"])
            item = {name: row[name] for name in ("host_id", "thread_id", "display_title", "source_updated_at")}
            item.update(_catalog_snapshot=_snapshot(row), _host_snapshot=_snapshot(host or {}),
                        _schema_signature=signature, _database_snapshot=database_snapshot,
                        _deletable=True, _blocked_reason="")
            try:
                _check_target_data(conn, row)
            except CatalogError as exc:
                item.update(_deletable=False, _blocked_reason=str(exc))
            result.append(item)
        if _database_snapshot(db) != database_snapshot:
            raise CatalogError("数据库文件已被替换，请重新刷新列表。")
        return result
    except CatalogError:
        raise
    except sqlite3.Error as exc:
        raise CatalogError("无法读取目录数据库；请检查文件格式、访问权限和数据库占用情况。") from exc
    finally:
        conn.close()


def _tombstone(conn: sqlite3.Connection, host_id: str, thread_id: str) -> None:
    if not conn.execute("SELECT 1 FROM local_thread_catalog_scan_checkpoints WHERE host_id=? COLLATE BINARY", (host_id,)).fetchone():
        return
    existing = conn.execute("SELECT removed FROM local_thread_catalog_scan_entries WHERE host_id=? COLLATE BINARY AND thread_id=? COLLATE BINARY",
                            (host_id, thread_id)).fetchall()
    if existing:
        if len(existing) != 1 or type(existing[0][0]) is not int or existing[0][0] not in (0, 1):
            raise CatalogError("扫描检查点数据异常；已取消清理。")
        conn.execute("UPDATE local_thread_catalog_scan_entries SET removed=1 WHERE host_id=? COLLATE BINARY AND thread_id=? COLLATE BINARY", (host_id, thread_id))
        return
    for name, col in _columns(conn, "local_thread_catalog_scan_entries").items():
        if name not in {"host_id", "thread_id", "removed"} and col[3] and col[4] is None and not col[6]:
            raise CatalogError(f"扫描检查点结构不受支持：新增字段 {name} 没有默认值；已取消清理。")
    conn.execute("INSERT INTO local_thread_catalog_scan_entries(host_id,thread_id,removed) VALUES(?,?,1)", (host_id, thread_id))


def remove_entry(db: str | Path, selected: dict, check: Callable[[], list[str]] | None = None) -> dict:
    """Remove exactly one previously inspected row after two process checks.

    Schema, host and every selected catalog field must still match the read
    snapshot. All counters and tombstones are committed in the same transaction.
    ``check`` is an injectable process checker intended for synthetic tests.
    """
    if not isinstance(selected, dict) or not _valid_identity(selected.get("host_id")) or not _valid_identity(selected.get("thread_id")):
        raise CatalogError("请选择具有明确主机和会话编号的记录。")
    required_snapshot = ("_catalog_snapshot", "_host_snapshot", "_schema_signature", "_database_snapshot")
    if any(not isinstance(selected.get(key), str) for key in required_snapshot):
        raise CatalogError("缺少完整检查快照，请刷新列表后重新选择。")
    if selected.get("_deletable") is not True:
        raise CatalogError(selected.get("_blocked_reason") or "该记录未通过检查，请刷新列表。")
    if _database_snapshot(db) != selected["_database_snapshot"]:
        raise CatalogError("数据库路径或文件已发生变化，请刷新列表后重新选择。")
    checker = running_apps if check is None else check
    _require_closed(checker)
    conn = _connect(db, write=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        validate_schema(conn)
        if _schema_signature(conn) != selected["_schema_signature"]:
            raise CatalogError(_STALE)
        host_id, thread_id = selected["host_id"], selected["thread_id"]
        records = conn.execute("SELECT * FROM local_thread_catalog WHERE host_id=? COLLATE BINARY AND thread_id=? COLLATE BINARY", (host_id, thread_id)).fetchall()
        if len(records) != 1:
            raise CatalogError(_STALE)
        row = dict(records[0])
        host = _host_row(conn, host_id)
        if _snapshot(row) != selected["_catalog_snapshot"] or _snapshot(host or {}) != selected["_host_snapshot"]:
            raise CatalogError(_STALE)
        # Also compare public confirmation fields, so modifying a displayed
        # title/time without retaining its original snapshot cannot pass.
        if any(row.get(key) != selected.get(key) for key in ("display_title", "source_updated_at")):
            raise CatalogError(_STALE)
        _check_target_data(conn, row)
        _tombstone(conn, host_id, thread_id)
        revision = conn.execute("UPDATE local_thread_catalog_metadata SET catalog_revision=catalog_revision+1 WHERE id=1")
        sequence = conn.execute("UPDATE local_thread_catalog_sync_state SET observation_sequence=observation_sequence+1 WHERE host_id=? COLLATE BINARY", (host_id,))
        deleted = conn.execute("DELETE FROM local_thread_catalog WHERE host_id=? COLLATE BINARY AND thread_id=? COLLATE BINARY", (host_id, thread_id))
        if (revision.rowcount, sequence.rowcount, deleted.rowcount) != (1, 1, 1):
            raise CatalogError("目录数据发生变化，已取消全部修改。")
        _require_closed(checker)
        if _database_snapshot(db) != selected["_database_snapshot"]:
            raise CatalogError("数据库路径或文件已发生变化，已取消全部修改。")
        conn.commit()
        return {"host_id": host_id, "thread_id": thread_id, "removed": True}
    except BaseException as exc:
        conn.rollback()
        if isinstance(exc, sqlite3.Error):
            raise CatalogError("数据库拒绝本次修改，全部修改已回滚。请检查结构兼容性和数据库占用情况。") from exc
        raise
    finally:
        conn.close()
