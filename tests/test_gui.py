"""Tk smoke and state tests. Uses synthetic rows and mocked service calls only."""

import threading
import time
import unittest
from unittest.mock import patch

try:
    import tkinter as tk
except ImportError:
    raise unittest.SkipTest("Tkinter unavailable")
from codex_session_tools import gui


ROW = {"host_id": "demo-host", "thread_id": "00000000-0000-0000-0000-000000000001",
       "display_title": "合成测试会话", "source_updated_at": 1700000000, "_deletable": True}


class GuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest("GUI display unavailable: " + str(exc))
        self.root.withdraw()
        self.app = gui.MaintenanceApp(self.root)
        self.root.update_idletasks()
        self.errors = patch.object(gui.messagebox, "showerror").start()
        self.warnings = patch.object(gui.messagebox, "showwarning").start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.app.local.busy = self.app.remote.busy = False
        self.app.close()

    def synchronous(self):
        def run(operation, callback):
            try:
                result = operation()
            except Exception as error:
                callback(None, error)
            else:
                callback(result, None)
        self.app.submit = run

    def select_row(self):
        tab = self.app.local
        tab.rows = [ROW]
        tab.filter_rows()
        tab.tree.selection_set(tab.tree.get_children()[0])
        tab.selection_changed()
        return tab

    def test_two_tabs_and_no_automatic_connections(self):
        self.assertEqual(len(self.app.notebook.tabs()), 2)
        self.assertFalse(self.app.local.rows)
        self.assertIsNone(self.app.remote.snapshot)
        self.assertIn("disabled", self.app.local.remove_button.state())
        self.assertIn("disabled", self.app.remote.unlock_button.state())

    def test_minimum_window_keeps_table_actions_and_status_visible(self):
        self.root.geometry("900x760")
        self.root.deiconify()
        self.root.update()
        tab = self.app.local
        self.assertGreaterEqual(tab.tree.winfo_height(), 100)
        self.assertLessEqual(tab.remove_button.winfo_rooty() + tab.remove_button.winfo_height(),
                             self.root.winfo_rooty() + self.root.winfo_height() - 25)
        self.app.notebook.select(self.app.remote)
        self.root.update()
        self.assertGreaterEqual(self.app.remote.details.winfo_height(), 100)

    def test_path_change_invalidates_selection(self):
        tab = self.select_row()
        tab.db.set("different-synthetic.db")
        self.assertFalse(tab.rows)
        self.assertFalse(tab.visible)
        self.assertIn("disabled", tab.remove_button.state())

    @patch.object(gui.local_catalog, "list_entries", side_effect=RuntimeError("synthetic read failure"))
    def test_refresh_failure_clears_old_rows(self, listing):
        self.synchronous()
        tab = self.select_row()
        tab.reload()
        self.assertFalse(tab.visible)
        self.assertIn("读取失败", tab.status.get())

    @patch.object(gui, "confirm", return_value=True)
    @patch.object(gui.local_catalog, "remove_entry")
    @patch.object(gui.local_catalog, "list_entries", side_effect=RuntimeError("synthetic read failure"))
    def test_delete_committed_refresh_failed_is_reported_distinctly(self, listing, removal, confirmation):
        self.synchronous()
        tab = self.select_row()
        tab.remove()
        removal.assert_called_once()
        self.assertIn("删除已完成", tab.status.get())
        self.assertIn("刷新失败", tab.status.get())
        self.assertFalse(tab.visible)
        self.warnings.assert_called_once()

    @patch.object(gui, "confirm", return_value=False)
    @patch.object(gui.local_catalog, "remove_entry")
    def test_cancel_never_removes(self, removal, confirmation):
        self.select_row().remove()
        removal.assert_not_called()

    def test_unknown_title_or_time_does_not_break_list(self):
        tab = self.app.local
        tab.rows = [{**ROW, "display_title": None, "source_updated_at": None, "_deletable": False}]
        tab.filter_rows()
        self.assertEqual(len(tab.visible), 1)

    def test_remote_parameter_change_invalidates_snapshot(self):
        tab = self.app.remote
        tab.server.set("demo")
        tab.snapshot = {"ready": True, "owner": {"pid": 123}}
        tab.inspected_inputs = tab.inputs()
        tab.update_actions()
        self.assertNotIn("disabled", tab.unlock_button.state())
        tab.home.set("/synthetic/codex")
        self.assertIsNone(tab.snapshot)
        self.assertIn("disabled", tab.unlock_button.state())

    @patch.object(gui, "confirm", return_value=True)
    @patch.object(gui.remote_client, "unlock_remote", return_value={"status": "verification_error"})
    def test_remote_execution_consumes_snapshot_even_when_unverified(self, unlock, confirmation):
        self.synchronous()
        tab = self.app.remote
        tab.server.set("demo")
        tab.snapshot = {"ready": True, "owner": {"pid": 123}}
        tab.inspected_inputs = tab.inputs()
        tab.unlock()
        self.assertIsNone(tab.snapshot)
        self.assertIn("无法确认", tab.status.get())
        self.assertIn("disabled", tab.unlock_button.state())

    def test_worker_does_not_block_tk_and_completes_on_main_thread(self):
        main_thread = threading.get_ident()
        release, started = threading.Event(), threading.Event()
        completions = []

        def operation():
            started.set()
            release.wait(2)
            return threading.get_ident()

        def done(value, error):
            completions.append((value, error, threading.get_ident()))

        self.app.submit(operation, done)
        self.assertTrue(started.wait(1))
        self.root.update()
        self.assertFalse(completions)
        release.set()
        deadline = time.monotonic() + 3
        while not completions and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(completions)
        self.assertNotEqual(completions[0][0], main_thread)
        self.assertEqual(completions[0][2], main_thread)

    def test_confirmation_dialog_opens_and_cancel_is_false(self):
        def cancel_dialog():
            for child in self.root.winfo_children():
                if isinstance(child, gui.ConfirmDialog):
                    child.cancel()
        self.root.after(100, cancel_dialog)
        self.assertFalse(gui.confirm(self.root, "合成确认框", "Synthetic only", "REMOVE demo", "Synthetic acknowledgment"))


if __name__ == "__main__":
    unittest.main()
