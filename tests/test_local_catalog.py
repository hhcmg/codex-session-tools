import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_session_tools import local_catalog as catalog
from tests.fixtures import make_database


class LocalCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = make_database(Path(self.temp.name) / "synthetic.db")

    def execute(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(sql, parameters)
            conn.commit()

    def query(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, parameters).fetchall()

    def dump(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return tuple(conn.iterdump())

    def selected(self):
        return next(row for row in catalog.list_entries(self.db) if row["host_id"] == "cloud-a")

    def remove(self, selected=None, check=None):
        return catalog.remove_entry(self.db, self.selected() if selected is None else selected,
                                    check=(lambda: []) if check is None else check)

    def test_listing_filters_host_kind_and_never_changes_database(self):
        before = self.db.read_bytes()
        rows = catalog.list_entries(self.db)
        self.assertEqual({row["host_id"] for row in rows}, {"cloud-a", "cloud-b"})
        self.assertTrue(all(row["_deletable"] for row in rows))
        self.assertEqual(before, self.db.read_bytes())

    def test_normal_delete_advances_counters_and_preserves_other_host_and_history(self):
        calls = []
        result = self.remove(check=lambda: calls.append(1) or [])
        self.assertEqual(result, {"host_id": "cloud-a", "thread_id": "thread-one", "removed": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.query("SELECT host_id,thread_id FROM local_thread_catalog ORDER BY host_id"),
                         [("cloud-b", "thread-one"), ("local-a", "local-thread")])
        self.assertEqual(self.query("SELECT observation_sequence FROM local_thread_catalog_sync_state WHERE host_id='cloud-a'"), [(9,)])
        self.assertEqual(self.query("SELECT catalog_revision FROM local_thread_catalog_metadata"), [(13,)])
        self.assertEqual(self.query("SELECT * FROM synthetic_history"), [("Synthetic immutable history",)])
        self.assertEqual(self.query("SELECT * FROM local_thread_catalog_scan_entries"), [])

    def test_non_chatgpt_target_is_refused(self):
        row = self.selected()
        row.update(host_id="local-a", thread_id="local-thread")
        before = self.dump()
        with self.assertRaises(catalog.CatalogError):
            self.remove(row)
        self.assertEqual(self.dump(), before)

    def test_changed_host_kind_is_refused(self):
        row = self.selected()
        self.execute("UPDATE local_thread_catalog_hosts SET host_kind='local' WHERE host_id='cloud-a'")
        before = self.dump()
        with self.assertRaises(catalog.CatalogError):
            self.remove(row)
        self.assertEqual(self.dump(), before)

    def test_missing_database_is_not_created(self):
        missing = Path(self.temp.name) / "does-not-exist.db"
        with self.assertRaises(catalog.CatalogError):
            catalog.list_entries(missing)
        with self.assertRaises(catalog.CatalogError):
            catalog.remove_entry(missing, self.selected(), check=lambda: [])
        self.assertFalse(missing.exists())

    def test_missing_required_table_is_refused(self):
        self.execute("DROP TABLE local_thread_catalog_sync_state")
        with self.assertRaisesRegex(catalog.CatalogError, "缺少普通表"):
            catalog.list_entries(self.db)

    def test_missing_required_column_is_refused(self):
        self.execute("ALTER TABLE local_thread_catalog RENAME COLUMN source_updated_at TO obsolete_time")
        with self.assertRaisesRegex(catalog.CatalogError, "缺少字段"):
            catalog.list_entries(self.db)

    def test_missing_unique_key_is_refused(self):
        self.execute("DROP TABLE local_thread_catalog_scan_entries")
        self.execute("CREATE TABLE local_thread_catalog_scan_entries(host_id TEXT, thread_id TEXT, removed INTEGER)")
        with self.assertRaisesRegex(catalog.CatalogError, "唯一键"):
            catalog.list_entries(self.db)

    def test_partial_unique_key_is_refused(self):
        self.execute("DROP TABLE local_thread_catalog_scan_entries")
        self.execute("CREATE TABLE local_thread_catalog_scan_entries(host_id TEXT, thread_id TEXT, removed INTEGER)")
        self.execute("CREATE UNIQUE INDEX partial_key ON local_thread_catalog_scan_entries(host_id,thread_id) WHERE removed=1")
        with self.assertRaisesRegex(catalog.CatalogError, "唯一键"):
            catalog.list_entries(self.db)

    def test_case_insensitive_unique_key_is_refused(self):
        self.execute("DROP TABLE local_thread_catalog_scan_entries")
        self.execute("CREATE TABLE local_thread_catalog_scan_entries(host_id TEXT COLLATE NOCASE, thread_id TEXT, removed INTEGER, PRIMARY KEY(host_id,thread_id))")
        with self.assertRaisesRegex(catalog.CatalogError, "唯一键"):
            catalog.list_entries(self.db)

    def test_schema_change_requires_new_snapshot(self):
        row = self.selected()
        self.execute("ALTER TABLE local_thread_catalog ADD COLUMN new_optional TEXT")
        before = self.dump()
        with self.assertRaisesRegex(catalog.CatalogError, "发生变化"):
            self.remove(row)
        self.assertEqual(self.dump(), before)

    def test_different_database_with_identical_contents_is_refused(self):
        row = self.selected()
        another = make_database(Path(self.temp.name) / "another.db")
        before = another.read_bytes()
        with self.assertRaisesRegex(catalog.CatalogError, "数据库路径或文件"):
            catalog.remove_entry(another, row, check=lambda: [])
        self.assertEqual(another.read_bytes(), before)

    def test_replaced_database_file_requires_refresh(self):
        row = self.selected()
        replacement = make_database(Path(self.temp.name) / "replacement.db")
        replacement.replace(self.db)
        with self.assertRaisesRegex(catalog.CatalogError, "数据库路径或文件"):
            self.remove(row)

    def test_change_to_title_timestamp_or_other_field_requires_refresh(self):
        for column, value in (("display_title", "New title"), ("source_updated_at", 101), ("extra_state", "Changed")):
            with self.subTest(column=column):
                row = self.selected()
                self.execute(f"UPDATE local_thread_catalog SET {column}=? WHERE host_id='cloud-a'", (value,))
                before = self.dump()
                with self.assertRaisesRegex(catalog.CatalogError, "发生变化"):
                    self.remove(row)
                self.assertEqual(self.dump(), before)

    def test_modified_display_fields_cannot_disagree_with_snapshot(self):
        row = self.selected()
        row["display_title"] = "Not the inspected title"
        with self.assertRaisesRegex(catalog.CatalogError, "发生变化"):
            self.remove(row)

    def test_missing_snapshot_is_refused(self):
        row = self.selected()
        del row["_catalog_snapshot"]
        with self.assertRaisesRegex(catalog.CatalogError, "快照"):
            self.remove(row)

    def test_checkpoint_tombstone_insert(self):
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        self.remove()
        self.assertEqual(self.query("SELECT host_id,thread_id,removed FROM local_thread_catalog_scan_entries"),
                         [("cloud-a", "thread-one", 1)])

    def test_checkpoint_tombstone_update_preserves_extra_fields_and_other_host(self):
        self.execute("ALTER TABLE local_thread_catalog_scan_entries ADD COLUMN detail TEXT")
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        self.execute("INSERT INTO local_thread_catalog_scan_entries VALUES('cloud-a','thread-one',0,'retain')")
        self.execute("INSERT INTO local_thread_catalog_scan_entries VALUES('cloud-b','thread-one',0,'other')")
        self.remove()
        self.assertEqual(self.query("SELECT * FROM local_thread_catalog_scan_entries ORDER BY host_id"),
                         [("cloud-a", "thread-one", 1, "retain"), ("cloud-b", "thread-one", 0, "other")])

    def test_second_process_check_failure_rolls_back_delete_counters_and_tombstone(self):
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        checks = iter([[], ["codex"]])
        before = self.dump()
        with self.assertRaisesRegex(catalog.CatalogError, "完全退出"):
            self.remove(check=lambda: next(checks))
        self.assertEqual(self.dump(), before)

    def test_process_check_exception_rolls_back_existing_tombstone_update(self):
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        self.execute("INSERT INTO local_thread_catalog_scan_entries VALUES('cloud-a','thread-one',0)")
        calls = []
        def checker():
            calls.append(1)
            if len(calls) == 2:
                raise OSError("synthetic inspection failure")
            return []
        before = self.dump()
        with self.assertRaisesRegex(catalog.CatalogError, "检查失败"):
            self.remove(check=checker)
        self.assertEqual(self.dump(), before)

    def test_keyboard_interrupt_after_writes_rolls_back(self):
        calls = []
        def checker():
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return []
        before = self.dump()
        with self.assertRaises(KeyboardInterrupt):
            self.remove(check=checker)
        self.assertEqual(self.dump(), before)

    def test_first_process_check_failure_does_not_open_database(self):
        row = self.selected()
        with patch.object(catalog, "_connect") as connect:
            with self.assertRaisesRegex(catalog.CatalogError, "完全退出"):
                self.remove(row, check=lambda: ["chatgpt"])
            connect.assert_not_called()

    def test_default_checker_is_used_without_test_injection(self):
        row = self.selected()
        with patch.object(catalog, "running_apps", return_value=["codex"]) as check:
            with self.assertRaises(catalog.CatalogError):
                catalog.remove_entry(self.db, row)
            check.assert_called_once_with()

    def test_unreliable_checker_result_is_refused(self):
        for value in (None, False, "", {}, [None]):
            with self.subTest(value=value), self.assertRaisesRegex(catalog.CatalogError, "可靠结果"):
                self.remove(check=lambda: value)

    def test_null_title_and_timestamp_are_preserved_and_snapshot_checked(self):
        self.execute("UPDATE local_thread_catalog SET display_title=NULL,source_updated_at=NULL WHERE host_id='cloud-a'")
        row = self.selected()
        self.assertIsNone(row["display_title"])
        self.assertIsNone(row["source_updated_at"])
        self.assertTrue(row["_deletable"])
        self.remove(row)

    def test_empty_identity_is_displayed_but_not_deletable(self):
        self.execute("UPDATE local_thread_catalog SET thread_id='' WHERE host_id='cloud-a'")
        row = self.selected()
        self.assertFalse(row["_deletable"])
        with self.assertRaises(catalog.CatalogError):
            self.remove(row)

    def test_control_and_format_characters_disable_identity_deletion(self):
        for character in ("\x00", "\n", "\r", "\x1b", "\x7f", "\x85", "\x9f", "\u200b", "\u200e", "\u202e", "\u2066"):
            with self.subTest(character=repr(character)):
                self.execute("UPDATE local_thread_catalog SET thread_id=? WHERE host_id='cloud-a'", ("thread" + character + "one",))
                row = self.selected()
                self.assertFalse(row["_deletable"])
                before = self.dump()
                with self.assertRaises(catalog.CatalogError):
                    self.remove(row)
                self.assertEqual(self.dump(), before)

    def test_host_identity_with_control_character_is_not_deletable(self):
        host_id = "cloud\u202ea"
        for table in ("local_thread_catalog_hosts", "local_thread_catalog", "local_thread_catalog_sync_state"):
            self.execute(f"UPDATE {table} SET host_id=? WHERE host_id='cloud-a'", (host_id,))
        row = next(item for item in catalog.list_entries(self.db) if item["host_id"] == host_id)
        self.assertFalse(row["_deletable"])
        with self.assertRaises(catalog.CatalogError):
            self.remove(row)

    def test_normal_chinese_and_english_identity_remains_supported(self):
        thread_id = "English-会话-一"
        self.execute("UPDATE local_thread_catalog SET thread_id=? WHERE host_id='cloud-a'", (thread_id,))
        row = self.selected()
        self.assertTrue(row["_deletable"])
        self.assertEqual(self.remove(row)["thread_id"], thread_id)

    def test_null_identity_is_displayed_but_not_deletable(self):
        self.execute("DROP TABLE local_thread_catalog")
        self.execute("CREATE TABLE local_thread_catalog(host_id TEXT, thread_id TEXT, display_title TEXT, source_updated_at INTEGER, PRIMARY KEY(host_id,thread_id))")
        self.execute("INSERT INTO local_thread_catalog VALUES('cloud-a',NULL,'Invalid identity',100)")
        row = self.selected()
        self.assertFalse(row["_deletable"])
        with self.assertRaises(catalog.CatalogError):
            self.remove(row)

    def test_anomalous_counter_data_is_visible_but_refuses_deletion(self):
        for value in ("not-a-number", 1.25, -1, (1 << 63) - 1):
            with self.subTest(value=value):
                self.execute("UPDATE local_thread_catalog_sync_state SET observation_sequence=? WHERE host_id='cloud-a'", (value,))
                row = self.selected()
                self.assertFalse(row["_deletable"])
                before = self.dump()
                with self.assertRaises(catalog.CatalogError):
                    self.remove(row)
                self.assertEqual(self.dump(), before)

    def test_counter_data_is_rechecked_after_inspection(self):
        row = self.selected()
        self.execute("UPDATE local_thread_catalog_metadata SET catalog_revision='invalid'")
        before = self.dump()
        with self.assertRaisesRegex(catalog.CatalogError, "数据异常"):
            self.remove(row)
        self.assertEqual(self.dump(), before)

    def test_missing_counter_record_disables_deletion(self):
        self.execute("DELETE FROM local_thread_catalog_metadata")
        self.assertFalse(self.selected()["_deletable"])

    def test_nullable_or_noninteger_counter_schema_is_rejected(self):
        for declaration in ("INTEGER", "TEXT NOT NULL"):
            with self.subTest(declaration=declaration):
                self.execute("DROP TABLE local_thread_catalog_metadata")
                self.execute(f"CREATE TABLE local_thread_catalog_metadata(id INTEGER PRIMARY KEY,catalog_revision {declaration})")
                with self.assertRaisesRegex(catalog.CatalogError, "非空整型"):
                    catalog.list_entries(self.db)

    def test_extra_required_scan_column_with_default_is_supported(self):
        self.execute("ALTER TABLE local_thread_catalog_scan_entries ADD COLUMN version INTEGER NOT NULL DEFAULT 7")
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        self.remove()
        self.assertEqual(self.query("SELECT removed,version FROM local_thread_catalog_scan_entries"), [(1, 7)])

    def test_extra_required_scan_column_without_default_refuses_new_tombstone(self):
        self.execute("ALTER TABLE local_thread_catalog_scan_entries ADD COLUMN version INTEGER NOT NULL")
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        before = self.dump()
        with self.assertRaisesRegex(catalog.CatalogError, "没有默认值"):
            self.remove()
        self.assertEqual(self.dump(), before)

    def test_existing_tombstone_supports_extra_required_column_without_default(self):
        self.execute("ALTER TABLE local_thread_catalog_scan_entries ADD COLUMN version INTEGER NOT NULL")
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        self.execute("INSERT INTO local_thread_catalog_scan_entries VALUES('cloud-a','thread-one',0,3)")
        self.remove()
        self.assertEqual(self.query("SELECT removed,version FROM local_thread_catalog_scan_entries"), [(1, 3)])

    def test_invalid_tombstone_rolls_back(self):
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        self.execute("INSERT INTO local_thread_catalog_scan_entries VALUES('cloud-a','thread-one',9)")
        before = self.dump()
        with self.assertRaisesRegex(catalog.CatalogError, "检查点数据异常"):
            self.remove()
        self.assertEqual(self.dump(), before)

    def test_related_trigger_is_refused_without_running_it(self):
        self.execute("CREATE TRIGGER surprising_delete AFTER DELETE ON local_thread_catalog BEGIN DELETE FROM synthetic_history; END")
        before = self.dump()
        with self.assertRaisesRegex(catalog.CatalogError, "触发器"):
            catalog.list_entries(self.db)
        self.assertEqual(self.dump(), before)

    def test_incoming_delete_cascade_is_refused(self):
        self.execute("CREATE TABLE cascade_child(host_id TEXT,thread_id TEXT,FOREIGN KEY(host_id,thread_id) REFERENCES local_thread_catalog(host_id,thread_id) ON DELETE CASCADE)")
        with self.assertRaisesRegex(catalog.CatalogError, "外键级联"):
            catalog.list_entries(self.db)

    def test_incoming_update_cascade_is_refused(self):
        self.execute("CREATE TABLE cascade_child(value INTEGER REFERENCES local_thread_catalog_metadata(catalog_revision) ON UPDATE CASCADE)")
        with self.assertRaisesRegex(catalog.CatalogError, "外键级联"):
            catalog.list_entries(self.db)

    def test_silent_replace_conflict_policy_is_refused(self):
        self.execute("DROP TABLE local_thread_catalog_scan_entries")
        self.execute("CREATE TABLE local_thread_catalog_scan_entries(host_id TEXT,thread_id TEXT,removed INTEGER,PRIMARY KEY(host_id,thread_id) ON CONFLICT REPLACE)")
        with self.assertRaisesRegex(catalog.CatalogError, "冲突策略"):
            catalog.list_entries(self.db)

    def test_database_constraint_failure_rolls_back(self):
        self.execute("DROP TABLE local_thread_catalog_scan_entries")
        self.execute("CREATE TABLE local_thread_catalog_scan_entries(host_id TEXT,thread_id TEXT,removed INTEGER CHECK(removed=0),PRIMARY KEY(host_id,thread_id))")
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        before = self.dump()
        with self.assertRaisesRegex(catalog.CatalogError, "全部修改已回滚"):
            self.remove()
        self.assertEqual(self.dump(), before)

    def test_delete_foreign_key_restriction_rolls_back_already_written_counters_and_tombstone(self):
        self.execute("CREATE TABLE referencing_child(host_id TEXT,thread_id TEXT,FOREIGN KEY(host_id,thread_id) REFERENCES local_thread_catalog(host_id,thread_id) ON DELETE RESTRICT)")
        self.execute("INSERT INTO referencing_child VALUES('cloud-a','thread-one')")
        self.execute("INSERT INTO local_thread_catalog_scan_checkpoints VALUES('cloud-a','cursor')")
        before = self.dump()
        with self.assertRaisesRegex(catalog.CatalogError, "全部修改已回滚"):
            self.remove()
        self.assertEqual(self.dump(), before)

    def test_default_database_respects_codex_home(self):
        with patch.dict(os.environ, {"CODEX_HOME": self.temp.name}):
            self.assertEqual(catalog.default_db(), Path(self.temp.name) / "sqlite" / "codex-dev.db")


if __name__ == "__main__":
    unittest.main()
