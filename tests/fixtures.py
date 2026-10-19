"""Entirely synthetic catalog fixtures; never open a real Codex database."""
import sqlite3
from contextlib import closing
from pathlib import Path


SCHEMA = """
CREATE TABLE local_thread_catalog_hosts (
    host_id TEXT PRIMARY KEY, host_kind TEXT NOT NULL, label TEXT
);
CREATE TABLE local_thread_catalog (
    host_id TEXT NOT NULL, thread_id TEXT NOT NULL, display_title TEXT,
    source_updated_at INTEGER, extra_state TEXT,
    PRIMARY KEY (host_id, thread_id)
);
CREATE TABLE local_thread_catalog_sync_state (
    host_id TEXT PRIMARY KEY, observation_sequence INTEGER NOT NULL
);
CREATE TABLE local_thread_catalog_metadata (
    id INTEGER PRIMARY KEY, catalog_revision INTEGER NOT NULL
);
CREATE TABLE local_thread_catalog_scan_checkpoints (host_id TEXT PRIMARY KEY, cursor TEXT);
CREATE TABLE local_thread_catalog_scan_entries (
    host_id TEXT NOT NULL, thread_id TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host_id, thread_id)
);
CREATE TABLE synthetic_history (body TEXT NOT NULL);
"""


def make_database(path: Path, schema: str = SCHEMA) -> Path:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(schema)
        conn.executemany("INSERT INTO local_thread_catalog_hosts VALUES(?,?,?)", [
            ("cloud-a", "chatgpt", "Synthetic ChatGPT host"),
            ("cloud-b", "chatgpt", "Second synthetic host"),
            ("local-a", "local", "Synthetic local host"),
        ])
        conn.executemany("INSERT INTO local_thread_catalog VALUES(?,?,?,?,?)", [
            ("cloud-a", "thread-one", "Synthetic title", 100, "old state"),
            ("cloud-b", "thread-one", "Same ID on another host", 99, None),
            ("local-a", "local-thread", "Do not remove", 98, None),
        ])
        conn.executemany("INSERT INTO local_thread_catalog_sync_state VALUES(?,?)", [
            ("cloud-a", 8), ("cloud-b", 4), ("local-a", 1),
        ])
        conn.execute("INSERT INTO local_thread_catalog_metadata VALUES(1,12)")
        conn.execute("INSERT INTO synthetic_history VALUES('Synthetic immutable history')")
        conn.commit()
    return path
