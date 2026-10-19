import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_session_tools import cli
from codex_session_tools.presentation import display_time, remote_result, safe_text
from tests.fixtures import make_database


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

    def run_legacy_encoding_cli(self, args, *, console_entry=False):
        environment = os.environ.copy()
        environment.update(PYTHONIOENCODING="cp1252:strict", PYTHONUTF8="0")
        entry = (["-c", "from codex_session_tools.cli import main; raise SystemExit(main())"]
                 if console_entry else ["-m", "codex_session_tools"])
        return subprocess.run([sys.executable, *entry, *args], env=environment,
                              cwd=Path(__file__).resolve().parents[1],
                              capture_output=True, timeout=20)

    def test_help_is_utf8_before_argparse_for_module_and_console_entry(self):
        # Capture bytes: letting subprocess decode with the host's encoding could
        # hide the Windows pipe failure or turn this into a decoding-only test.
        for console_entry in (False, True):
            with self.subTest(console_entry=console_entry):
                result = self.run_legacy_encoding_cli(["--help"], console_entry=console_entry)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Codex 会话维护工具", result.stdout.decode("utf-8"))
                self.assertEqual(result.stderr, b"")

    def test_argparse_and_operation_errors_use_utf8_stderr(self):
        for arguments, code, expected in [
            (["--未知参数"], 2, "--未知参数"),
            (["cleanup", "--remove"], 1, "移除需要同时提供"),
        ]:
            with self.subTest(arguments=arguments):
                result = self.run_legacy_encoding_cli(arguments)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(expected, result.stderr.decode("utf-8"))
                self.assertNotIn(b"Traceback", result.stderr)
                self.assertEqual(result.stdout, b"")

    def test_chinese_catalog_json_roundtrips_with_legacy_pipe_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            database = make_database(Path(directory) / "synthetic.db")
            connection = sqlite3.connect(database)
            try:
                connection.execute("UPDATE local_thread_catalog SET display_title=? WHERE host_id=?",
                                   ("中文会话与符号 🧪", "cloud-a"))
                connection.commit()
            finally:
                connection.close()
            result = self.run_legacy_encoding_cli(["cleanup", "--db", str(database), "--json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(next(row["display_title"] for row in rows if row["host_id"] == "cloud-a"),
                         "中文会话与符号 🧪")
        self.assertEqual(result.stderr, b"")

    def test_custom_text_capture_is_preserved_when_reconfigure_is_unsupported(self):
        class CapturedText(io.StringIO):
            def reconfigure(self, **kwargs):
                raise io.UnsupportedOperation("This capture already accepts Unicode text")

        out, err = CapturedText(), CapturedText()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["cleanup", "--remove"]), 1)
            self.assertIs(sys.stdout, out)
            self.assertIs(sys.stderr, err)
        self.assertIn("移除需要同时提供", err.getvalue())
        self.assertFalse(out.closed)
        self.assertFalse(err.closed)

    def test_pythonw_missing_output_streams_preserve_exit_status(self):
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            for arguments, expected in [(["--help"], 0), (["cleanup", "--help"], 0),
                                        (["--version"], 0), (["--未知参数"], 2)]:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(SystemExit) as stopped:
                        cli.main(arguments)
                    self.assertEqual(stopped.exception.code, expected)
            self.assertEqual(cli.main(["cleanup", "--remove"]), 1)


if __name__ == "__main__":
    unittest.main()
