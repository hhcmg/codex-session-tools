import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_session_tools import cli
from codex_session_tools.presentation import display_time, remote_result, safe_text


ROW = {"host_id": "demo-host", "thread_id": "00000000-0000-0000-0000-000000000001",
       "display_title": "示例会话", "source_updated_at": 1700000000, "_deletable": True}


class FrontendTests(unittest.TestCase):
    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(args)
        return code, out.getvalue(), err.getvalue()

    @patch.object(cli.local_catalog, "remove_entry")
    @patch.object(cli.local_catalog, "list_entries", return_value=[ROW])
    def test_cleanup_defaults_to_read_only(self, listing, removal):
        code, out, _ = self.run_cli(["cleanup", "--db", "synthetic.db"])
        self.assertEqual(code, 0)
        self.assertIn(ROW["thread_id"], out)
        removal.assert_not_called()

    @patch.object(cli.local_catalog, "list_entries")
    def test_cleanup_requires_exact_identity_before_read(self, listing):
        code, _, _ = self.run_cli(["cleanup", "--remove"])
        self.assertEqual(code, 1)
        listing.assert_not_called()

    @patch.object(cli.local_catalog, "remove_entry")
    @patch.object(cli.local_catalog, "list_entries", return_value=[ROW])
    @patch("sys.stdin.isatty", return_value=False)
    def test_piped_confirmation_cannot_remove(self, tty, listing, removal):
        code, _, err = self.run_cli(["cleanup", "--remove", "--host-id", ROW["host_id"], "--thread-id", ROW["thread_id"]])
        self.assertEqual(code, 1)
        self.assertIn("交互式", err)
        removal.assert_not_called()

    @patch.object(cli.local_catalog, "remove_entry")
    @patch.object(cli.local_catalog, "list_entries", return_value=[ROW])
    @patch("sys.stdin.isatty", return_value=True)
    @patch("builtins.input", return_value="cancel")
    def test_cancel_does_not_remove(self, prompt, tty, listing, removal):
        code, _, _ = self.run_cli(["cleanup", "--remove", "--host-id", ROW["host_id"], "--thread-id", ROW["thread_id"]])
        self.assertEqual(code, 0)
        removal.assert_not_called()

    @patch.object(cli.local_catalog, "remove_entry")
    @patch.object(cli.local_catalog, "list_entries", return_value=[ROW])
    @patch("sys.stdin.isatty", return_value=True)
    @patch("builtins.input", return_value="REMOVE " + ROW["thread_id"])
    def test_confirmed_cleanup_passes_original_snapshot(self, prompt, tty, listing, removal):
        code, _, _ = self.run_cli(["cleanup", "--remove", "--host-id", ROW["host_id"], "--thread-id", ROW["thread_id"]])
        self.assertEqual(code, 0)
        self.assertIs(removal.call_args.args[1], ROW)

    @patch.object(cli.local_catalog, "list_entries", return_value=[{**ROW, "_catalog_snapshot": {"private": "hidden"}}])
    def test_json_does_not_export_internal_snapshot(self, listing):
        code, out, _ = self.run_cli(["cleanup", "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn("private", out)
        self.assertNotIn("_catalog_snapshot", out)

    @patch.object(cli.remote_client, "inspect_remote")
    @patch("builtins.input")
    def test_inspect_only_requires_arguments_without_prompting(self, prompt, inspection):
        self.assertEqual(self.run_cli(["unlock", "--inspect-only"])[0], 1)
        prompt.assert_not_called()
        inspection.assert_not_called()

    @patch.object(cli.remote_client, "unlock_remote")
    @patch.object(cli.remote_client, "inspect_remote", return_value={"status": "blocked", "ready": False, "blockers": ["unknown"]})
    @patch("builtins.input")
    def test_blocked_remote_does_not_prompt_or_signal(self, prompt, inspection, unlock):
        self.assertEqual(self.run_cli(["unlock", "--server", "demo", "--thread-id", ROW["thread_id"]])[0], 2)
        prompt.assert_not_called()
        unlock.assert_not_called()

    @patch.object(cli.remote_client, "unlock_remote")
    @patch.object(cli.remote_client, "inspect_remote", return_value={"status": "locked", "ready": True})
    def test_inspect_only_never_unlocks(self, inspection, unlock):
        self.assertEqual(self.run_cli(["unlock", "--server", "demo", "--thread-id", ROW["thread_id"], "--inspect-only"])[0], 0)
        unlock.assert_not_called()

    def test_invalid_times_and_control_characters(self):
        self.assertIn("未知", display_time(None))
        self.assertIn("无效", display_time(1700000000000))
        self.assertNotIn("\x1b", safe_text("\x1b[31mhello\nworld"))

    def test_remote_partial_outcomes_are_not_success(self):
        for value in ("still_locked", "verification_error", "unexpected"):
            self.assertEqual(remote_result({"status": value})[1], 2)
        self.assertEqual(remote_result({"status": "unlocked", "history_unchanged": False})[1], 3)
        self.assertIn("历史发生变化", remote_result({"status": "still_locked", "history_unchanged": False})[0])


if __name__ == "__main__":
    unittest.main()
