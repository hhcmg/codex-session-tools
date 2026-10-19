"""Linux tests use temporary state databases and locks owned by test children.

Every signal call is mocked. No installed Codex process or user data is read.
"""

import base64
import copy
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_session_tools import remote_helper as helper


TID = "00000000-0000-4000-8000-000000000001"
OTHER_TID = "00000000-0000-4000-8000-000000000002"
HOLDER_CODE = """
import fcntl, sys
streams = [open(path, 'a+') for path in sys.argv[1:]]
for stream in streams:
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
print('READY', flush=True)
sys.stdin.readline()
for stream in streams:
    stream.close()
"""


def event(kind):
    return json.dumps({"type": "event_msg", "timestamp": "2026-01-01T00:00:00Z",
                       "payload": {"type": kind}}) + "\n"


class StandaloneTests(unittest.TestCase):
    def test_helper_compiles_after_request_prefix(self):
        source = Path(helper.__file__).read_text(encoding="utf-8")
        compile("REQUEST_B64 = 'e30='\n" + source, "sent-helper", "exec")

    def test_response_envelope_is_versioned_and_does_not_echo_request(self):
        with mock.patch.object(helper, "main", side_effect=helper.Refused("Synthetic refusal")):
            result = helper.envelope({"private": "not echoed"})
        self.assertEqual(result["version"], 1)
        self.assertFalse(result["ok"])
        self.assertFalse(result["error"]["outcome_unknown"])
        self.assertNotIn("private", json.dumps(result))

    def test_entry_point_prints_one_json_response(self):
        source = Path(helper.__file__).read_text(encoding="utf-8")
        # An unsupported action is rejected before any path/process inspection.
        encoded = base64.b64encode(b'{"action":"unsupported"}').decode("ascii")
        result = subprocess.run([sys.executable, "-"], input="REQUEST_B64 = '" + encoded + "'\n" + source,
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(helper.RESPONSE_PREFIX))
        payload = json.loads(lines[0][len(helper.RESPONSE_PREFIX):])
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["error"]["outcome_unknown"])


@unittest.skipUnless(sys.platform == "linux", "Linux /proc, flock and pidfd tests")
class LinuxHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-session-tools-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.locks = self.home / "thread-writer-locks"
        self.locks.mkdir()
        self.database = self.home / "state_5.sqlite"
        with sqlite3.connect(self.database) as conn:
            conn.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, rollout_path TEXT)")
        self.add_history(TID)
        # This applies even to tests that never call unlock: no real SIGTERM can
        # escape if a regression accidentally reaches the signalling branch.
        self.signal_mock = mock.patch.object(helper.signal, "pidfd_send_signal", create=True).start()
        self.addCleanup(mock.patch.stopall)

    def add_history(self, tid, contents=None):
        path = self.home / (tid + ".jsonl")
        path.write_text(event("task_complete") if contents is None else contents, encoding="utf-8")
        with sqlite3.connect(self.database) as conn:
            conn.execute("INSERT OR REPLACE INTO threads VALUES(?,?,?)",
                         (tid, "Synthetic conversation " + tid[-1], str(path)))
        return path

    def history(self, tid=TID):
        return self.home / (tid + ".jsonl")

    def lock_path(self, tid=TID):
        return self.locks / (tid + ".lock")

    def holder(self, tids=(TID,)):
        process = subprocess.Popen([sys.executable, "-c", HOLDER_CODE,
                                    *[str(self.lock_path(tid)) for tid in tids]],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)

        def cleanup():
            if process.poll() is None:
                process.communicate("release\n", timeout=5)
            else:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream:
                        stream.close()

        self.addCleanup(cleanup)
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, "Test lock-holder did not become ready")
        self.assertEqual(process.stdout.readline().strip(), "READY")
        return process

    def allow_test_holder(self, process):
        # Only the single child created by this test qualifies as a mock backend.
        patcher = mock.patch.object(helper, "is_codex_backend",
                                    side_effect=lambda info: bool(info and info["pid"] == process.pid))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ready_snapshot(self, process):
        if not hasattr(os, "pidfd_open"):
            self.skipTest("Kernel/Python has no pidfd_open")
        self.allow_test_holder(process)
        result = helper.snapshot(self.home, TID)
        self.assertTrue(result["ready"], result["blockers"])
        return result

    @staticmethod
    def release(process):
        process.stdin.write("release\n")
        process.stdin.flush()
        process.wait(timeout=5)

    def request(self, snap):
        return {"action": "unlock", "confirmed": True, "expected": snap}

    def test_actual_flock_and_kernel_owner(self):
        process = self.holder()
        record = helper.lock_record(self.lock_path())
        self.assertTrue(record["held"])
        owners = [item["pid"] for item in helper.kernel_locks() if item["inode"] == record["inode"]]
        self.assertIn(process.pid, owners)
        self.release(process)
        self.assertFalse(helper.lock_record(self.lock_path())["held"])

    def test_missing_and_idle_lock_never_signal(self):
        self.assertEqual(helper.snapshot(self.home, TID)["status"], "unlocked")
        self.lock_path().touch()
        self.assertEqual(helper.snapshot(self.home, TID)["status"], "unlocked")
        self.signal_mock.assert_not_called()

    def test_non_codex_owner_is_blocked(self):
        self.holder()
        snap = helper.snapshot(self.home, TID)
        self.assertFalse(snap["ready"])
        self.assertTrue(any("not a Codex" in reason for reason in snap["blockers"]))

    def test_active_target_is_blocked(self):
        self.history().write_text(event("task_started"), encoding="utf-8")
        process = self.holder()
        self.allow_test_holder(process)
        snap = helper.snapshot(self.home, TID)
        self.assertFalse(snap["ready"])
        self.assertEqual(snap["target"]["lifecycle"], "active")
        self.assertTrue(any("Unfinished" in reason for reason in snap["blockers"]))

    def test_other_active_session_blocks_backend(self):
        self.add_history(OTHER_TID, event("task_started"))
        process = self.holder((TID, OTHER_TID))
        self.allow_test_holder(process)
        snap = helper.snapshot(self.home, TID)
        self.assertEqual(len(snap["affected"]), 2)
        self.assertFalse(snap["ready"])
        self.assertTrue(any(OTHER_TID in reason for reason in snap["blockers"]))

    def test_unknown_or_damaged_events_cannot_become_idle(self):
        process = self.holder()
        self.allow_test_holder(process)
        for suffix in ("[]\n", "{partial", '{"type":"event_msg","payload":[]}\n',
                       '{"type":"event_msg","payload":null}\n',
                       '{"type":"response_item","payload":[]}\n',
                       '{"type":"event_msg","payload":"task_complete"}\n'):
            with self.subTest(suffix=suffix):
                self.history().write_text(event("task_complete") + suffix, encoding="utf-8")
                snap = helper.snapshot(self.home, TID)
                self.assertEqual(snap["target"]["lifecycle"], "unknown")
                self.assertFalse(snap["ready"])
                self.assertTrue(any("unknown" in reason for reason in snap["blockers"]))

    def test_missing_other_metadata_is_unknown_and_blocked(self):
        process = self.holder((TID, OTHER_TID))
        self.allow_test_holder(process)
        snap = helper.snapshot(self.home, TID)
        self.assertFalse(snap["ready"])
        self.assertTrue(any(OTHER_TID in reason for reason in snap["blockers"]))

    def test_latest_database_never_falls_back_to_old_metadata(self):
        with sqlite3.connect(self.home / "state_20.sqlite") as conn:
            conn.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, rollout_path TEXT)")
        self.assertEqual(helper.db_metadata(self.home, [TID]), {})
        with self.assertRaisesRegex(helper.Refused, "not found"):
            helper.snapshot(self.home, TID)

    def test_incompatible_latest_schema_is_refused(self):
        with sqlite3.connect(self.home / "state_20.sqlite") as conn:
            conn.execute("CREATE TABLE threads(id TEXT)")
        with self.assertRaisesRegex(helper.Refused, "Unsupported latest"):
            helper.db_metadata(self.home, [TID])

    def test_busy_database_does_not_signal(self):
        conn = sqlite3.connect(self.database)
        self.addCleanup(conn.close)
        conn.execute("BEGIN EXCLUSIVE")
        with self.assertRaises(sqlite3.OperationalError):
            helper.snapshot(self.home, TID)
        self.signal_mock.assert_not_called()

    def test_custom_codex_home_and_confirmation(self):
        with mock.patch.object(helper, "snapshot", return_value={"synthetic": True}) as snap:
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
                helper.main({"action": "inspect", "thread_id": TID})
            snap.assert_called_once_with(str(self.home), TID)
        with self.assertRaises(helper.Refused):
            helper.main({"action": "unlock", "confirmed": False})

    def test_lock_symlink_and_outside_history_are_refused(self):
        self.lock_path().symlink_to(self.history())
        with self.assertRaises(OSError):
            helper.lock_record(self.lock_path())
        with self.assertRaises(helper.Refused):
            helper.history_state({"id": TID, "rollout_path": "/outside.jsonl"}, self.home)

    def test_changed_fingerprint_refuses_before_pidfd_or_signal(self):
        process = self.holder()
        snap = self.ready_snapshot(process)
        self.history().write_text(event("task_complete") + event("turn_aborted"), encoding="utf-8")
        with mock.patch.object(helper.os, "pidfd_open") as pidfd:
            with self.assertRaisesRegex(helper.Refused, "changed after confirmation"):
                helper.unlock(self.request(snap))
            pidfd.assert_not_called()
        self.signal_mock.assert_not_called()

    def test_pid_identity_change_refuses_before_signal(self):
        process = self.holder()
        snap = self.ready_snapshot(process)
        changed = copy.deepcopy(snap["owner"])
        changed["start_ticks"] += 1
        with mock.patch.object(helper, "snapshot", return_value=snap):
            with mock.patch.object(helper, "identity", return_value=changed):
                with self.assertRaisesRegex(helper.Refused, "identity changed"):
                    helper.unlock(self.request(snap))
        self.signal_mock.assert_not_called()

    def test_affected_history_changes_at_last_check_refuse(self):
        process = self.holder()
        snap = self.ready_snapshot(process)
        self.history().write_text(event("task_started"), encoding="utf-8")
        with mock.patch.object(helper, "snapshot", return_value=snap):
            with self.assertRaisesRegex(helper.Refused, "affected history changed"):
                helper.unlock(self.request(snap))
        self.signal_mock.assert_not_called()

    def test_lock_owner_set_changes_at_last_check_refuse(self):
        process = self.holder()
        snap = self.ready_snapshot(process)
        with mock.patch.object(helper, "snapshot", return_value=snap):
            with mock.patch.object(helper, "kernel_locks", return_value=[]):
                with self.assertRaisesRegex(helper.Refused, "affected tasks changed"):
                    helper.unlock(self.request(snap))
        self.signal_mock.assert_not_called()

    def test_one_simulated_sigterm_and_verified_release(self):
        process = self.holder()
        snap = self.ready_snapshot(process)
        self.signal_mock.side_effect = lambda *args: self.release(process)
        result = helper.unlock(self.request(snap))
        self.assertEqual(result["status"], "unlocked")
        self.assertTrue(result["history_unchanged"])
        self.signal_mock.assert_called_once()
        self.assertEqual(self.signal_mock.call_args.args[1], helper.signal.SIGTERM)

    def test_history_hash_change_after_signal_is_reported(self):
        process = self.holder()
        snap = self.ready_snapshot(process)

        def simulate_signal(*args):
            self.release(process)
            self.history().write_text(event("turn_aborted"), encoding="utf-8")

        self.signal_mock.side_effect = simulate_signal
        result = helper.unlock(self.request(snap))
        self.assertEqual(result["status"], "unlocked")
        self.assertFalse(result["history_unchanged"])
        self.signal_mock.assert_called_once()

    def test_reacquisition_during_verification_does_not_resignal(self):
        process = self.holder()
        snap = self.ready_snapshot(process)
        self.release(process)
        original_history = helper.history_state

        def reacquire_after_hash(*args, **kwargs):
            result = original_history(*args, **kwargs)
            held = self.lock_path().open("rb")
            self.addCleanup(held.close)
            helper.fcntl.flock(held.fileno(), helper.fcntl.LOCK_EX)
            return result

        with mock.patch.object(helper, "history_state", side_effect=reacquire_after_hash):
            result = helper.verify_after_signal(snap, self.lock_path(), process.pid)
        self.assertEqual(result["status"], "still_locked")
        self.assertIn(os.getpid(), result["current_lock_pids"])
        self.signal_mock.assert_not_called()

    def test_still_held_lock_does_not_resignal(self):
        process = self.holder()
        snap = self.ready_snapshot(process)
        with mock.patch.object(helper, "UNLOCK_WAIT_SECONDS", 0):
            result = helper.unlock(self.request(snap))
        self.assertEqual(result["status"], "still_locked")
        self.signal_mock.assert_called_once()

    def test_replaced_inode_after_signal_reports_unknown_verification(self):
        process = self.holder()
        snap = self.ready_snapshot(process)

        def simulate_signal(*args):
            self.release(process)
            # Preserve the old inode at another temporary path to prevent reuse.
            self.lock_path().rename(self.home / "old-test-lock")
            self.lock_path().touch()

        self.signal_mock.side_effect = simulate_signal
        result = helper.unlock(self.request(snap))
        self.assertEqual(result["status"], "verification_error")
        self.assertTrue(result["outcome_unknown"])
        self.signal_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
