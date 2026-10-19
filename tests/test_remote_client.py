"""Transport tests use mocks only: no SSH connection or application processes."""

import base64
import copy
import json
import os
import subprocess
import unittest
from unittest import mock

from codex_session_tools import remote_client as client


TID = "00000000-0000-4000-8000-000000000001"


def inspection():
    return {"status": "locked", "ready": True, "blockers": [], "warnings": [],
            "codex_home": "/tmp/synthetic-codex", "thread_id": TID,
            "owner": {"pid": 123}, "target": {"title": "Synthetic conversation"},
            "affected": [{"id": TID, "lifecycle": "completed"}],
            "fingerprint": "a" * 64, "lock": {"held": True}}


def response(result=None, *, error=None):
    payload = {"version": 1, "ok": error is None}
    payload["result" if error is None else "error"] = result if error is None else error
    return client.RESPONSE_PREFIX + json.dumps(payload)


def completed(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(["ssh"], returncode, stdout, stderr)


class InputTests(unittest.TestCase):
    def test_uuid_and_links(self):
        for value in (TID, "  " + TID.upper() + "  ", "codex://threads/" + TID,
                      "codex://threads/" + TID + "/?view=review#fragment"):
            with self.subTest(value=value):
                self.assertEqual(client.normalize_thread_id(value), TID)

    def test_rejects_invalid_or_ambiguous_ids(self):
        for value in (None, "", "123", TID.replace("-", ""), "{" + TID + "}",
                      "https://threads/" + TID, "codex://elsewhere/" + TID,
                      "codex://threads/" + TID + "/extra", "codex://threads//" + TID):
            with self.subTest(value=value), self.assertRaises(client.RemoteError):
                client.normalize_thread_id(value)

    def test_server_aliases(self):
        for alias in ("research-server", "192.0.2.10", "example.org", "lab_1"):
            self.assertEqual(client.validate_server(" " + alias + " "), alias)

    def test_rejects_ssh_options_and_shell_syntax(self):
        for alias in (None, "", "-oProxyCommand=bad", "server other", "a;b", "$(bad)",
                      "user@host", "host:22", "host\nnext", "host/path", "服务器"):
            with self.subTest(alias=alias), self.assertRaises(client.RemoteError):
                client.validate_server(alias)


class TransportTests(unittest.TestCase):
    @mock.patch.object(client.subprocess, "run")
    def test_inspect_fixed_command_and_base64_payload(self, run):
        run.return_value = completed("SSH banner\n" + response(inspection()) + "\n")
        strange_path = "/srv/a space/$(must-stay-data)"
        self.assertEqual(client.inspect_remote("research-server", TID, strange_path), inspection())
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[-2:], ["research-server", "python3 -"])
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("-T", command)
        self.assertIn("ClearAllForwardings=yes", command)
        self.assertIn("ConnectionAttempts=1", command)
        self.assertIn("ServerAliveInterval=10", command)
        self.assertIn("ServerAliveCountMax=2", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("PasswordAuthentication=no", command)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 60)
        self.assertNotIn(strange_path, " ".join(command))
        first_line = kwargs["input"].splitlines()[0]
        encoded = first_line.split("'", 2)[1]
        request = json.loads(base64.b64decode(encoded))
        self.assertEqual(request, {"action": "inspect", "thread_id": TID, "codex_home": strange_path})
        self.assertIn("def verify_after_signal", kwargs["input"])
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        run.assert_called_once()

    @mock.patch.object(client.subprocess, "run")
    def test_unlock_success_and_explicit_confirmation(self, run):
        result = {"status": "unlocked", "signalled_pid": 123, "signal": "SIGTERM",
                  "history_unchanged": True, "lock": {"held": False}}
        run.return_value = completed(response(result))
        self.assertEqual(client.unlock_remote("research-server", inspection()), result)
        script = run.call_args.kwargs["input"]
        request = json.loads(base64.b64decode(script.splitlines()[0].split("'", 2)[1]))
        self.assertIs(request["confirmed"], True)
        self.assertEqual(request["expected"], inspection())

    @mock.patch.object(client.subprocess, "run")
    def test_timeout_unlock_is_unknown_and_not_retried(self, run):
        run.side_effect = subprocess.TimeoutExpired("ssh", 60)
        with self.assertRaises(client.RemoteError) as caught:
            client.unlock_remote("research-server", inspection())
        self.assertTrue(caught.exception.outcome_unknown)
        run.assert_called_once()

    @mock.patch.object(client.subprocess, "run")
    def test_inspection_timeout_has_no_mutation_uncertainty(self, run):
        run.side_effect = subprocess.TimeoutExpired("ssh", 60)
        with self.assertRaises(client.RemoteError) as caught:
            client.inspect_remote("research-server", TID)
        self.assertFalse(caught.exception.outcome_unknown)

    @mock.patch.object(client.subprocess, "run")
    def test_connection_lost_after_unlock_is_unknown(self, run):
        run.return_value = completed("", returncode=255, stderr="Connection closed")
        with self.assertRaises(client.RemoteError) as caught:
            client.unlock_remote("research-server", inspection())
        self.assertTrue(caught.exception.outcome_unknown)
        run.assert_called_once()

    @mock.patch.object(client.subprocess, "run")
    def test_response_for_different_thread_is_rejected(self, run):
        wrong = inspection()
        wrong["thread_id"] = "00000000-0000-4000-8000-000000000099"
        wrong["affected"][0]["id"] = wrong["thread_id"]
        run.return_value = completed(response(wrong))
        with self.assertRaises(client.RemoteError) as caught:
            client.inspect_remote("research-server", TID)
        self.assertFalse(caught.exception.outcome_unknown)

    @mock.patch.object(client.subprocess, "run")
    def test_result_for_different_pid_is_unknown(self, run):
        result = {"status": "unlocked", "signalled_pid": 999, "signal": "SIGTERM",
                  "history_unchanged": True, "lock": {"held": False}}
        run.return_value = completed(response(result))
        with self.assertRaises(client.RemoteError) as caught:
            client.unlock_remote("research-server", inspection())
        self.assertTrue(caught.exception.outcome_unknown)

    @mock.patch.object(client.subprocess, "run")
    def test_absent_ssh_is_known_not_started(self, run):
        run.side_effect = FileNotFoundError("ssh")
        with self.assertRaises(client.RemoteError) as caught:
            client.unlock_remote("research-server", inspection())
        self.assertFalse(caught.exception.outcome_unknown)

    @mock.patch.object(client.subprocess, "run")
    def test_refused_or_unknown_snapshots_never_start_ssh(self, run):
        cases = []
        blocked = inspection()
        blocked.update(status="blocked", ready=False, blockers=["Task activity unknown"])
        cases.append(blocked)
        invalid_ready = inspection()
        invalid_ready["affected"][0]["lifecycle"] = "unknown"
        cases.append(invalid_ready)
        cases.append({"ready": True})
        for case in cases:
            with self.subTest(case=case), self.assertRaises(client.RemoteError):
                client.unlock_remote("research-server", case)
        run.assert_not_called()


class ProtocolTests(unittest.TestCase):
    def test_bad_protocol_after_unlock_is_unknown(self):
        for stdout in ("", "noise", "CODEX_SESSION_JSON:bad-json",
                       response({"status": "unlocked"}),
                       response({"status": "already_unlocked"}) + "\n" + response({"status": "already_unlocked"}),
                       'CODEX_SESSION_JSON:{"version":2,"ok":true,"result":{}}',
                       'CODEX_SESSION_JSON:{"version":true,"ok":true,"result":{}}',
                       'CODEX_SESSION_JSON:{"version":1,"ok":false,"error":{}}'):
            with self.subTest(stdout=stdout), self.assertRaises(client.RemoteError) as caught:
                client._parse_response(stdout, unlocking=True)
            self.assertTrue(caught.exception.outcome_unknown)

    def test_remote_refusal_preserves_known_outcome(self):
        data = response(error={"message": "Activity changed", "outcome_unknown": False})
        with self.assertRaises(client.RemoteError) as caught:
            client._parse_response(data, unlocking=True)
        self.assertFalse(caught.exception.outcome_unknown)

    def test_verification_failure_is_unknown(self):
        data = response({"status": "verification_error", "message": "Cannot read history"})
        with self.assertRaises(client.RemoteError) as caught:
            client._parse_response(data, unlocking=True)
        self.assertTrue(caught.exception.outcome_unknown)

    def test_relock_and_changed_history_remain_visible(self):
        result = {"status": "still_locked", "signalled_pid": 123, "signal": "SIGTERM",
                  "history_unchanged": False, "lock": {"held": True}, "current_lock_pids": [456]}
        self.assertEqual(client._parse_response(response(result), unlocking=True), result)

    def test_inconsistent_ready_inspection_is_rejected(self):
        for change in ({"blockers": ["busy"]}, {"fingerprint": "bad"}, {"affected": []},
                       {"status": "unlocked"}, {"lock": {"held": False}}):
            result = copy.deepcopy(inspection())
            result.update(change)
            with self.subTest(change=change), self.assertRaises(client.RemoteError):
                client._parse_response(response(result), unlocking=False)

    def test_unlocked_inspection_cannot_claim_lock_is_held(self):
        result = inspection()
        result.update(status="unlocked", ready=False, affected=[])
        with self.assertRaises(client.RemoteError):
            client._parse_response(response(result), unlocking=False)


if __name__ == "__main__":
    unittest.main()
