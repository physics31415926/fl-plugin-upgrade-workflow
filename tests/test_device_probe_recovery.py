"""Bounded probe recovery tests; only one local fixture subprocess is launched."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fl_dashboard.resources.fleet import DeviceFleet, MAX_OUTPUT, PROBE_TIMEOUT
from fl_dashboard.resources.telemetry import SENTINEL


class DeviceProbeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.runner = Mock()
        self.fleet = DeviceFleet(SimpleNamespace(projects=[]), {}, runner=self.runner,
                                 clock=lambda: 1000, monotonic=lambda: 1000)
        self.addCleanup(self.fleet.close)
        self.fleet._retry_pause = Mock(return_value=False)
        self.profile = {"alias": "fixture-device", "key": "a" * 64, "source_epoch": 0}
        self.marker = (SENTINEL + "\n").encode()
        self.good_view = {"reachable": True, "verdict": "free", "error": None,
                          "collection_complete": True, "accel": {"total": 1, "free": 1}}
        self.sample = object()
        parser = patch("fl_dashboard.resources.fleet.parse_telemetry",
                       side_effect=lambda *args: (copy.deepcopy(self.good_view), self.sample))
        self.parser = parser.start()
        self.addCleanup(parser.stop)

    def result(self, returncode=0, stdout=None, stderr=b""):
        return subprocess.CompletedProcess(["fixture-ssh"], returncode,
                                           self.marker if stdout is None else stdout, stderr)

    def probe(self, *attempts):
        self.runner.side_effect = list(attempts)
        return self.fleet._probe_device(self.profile, ["fixture-ssh"], None, 999)

    def test_transient_reset_recovers_once_using_the_same_bounded_probe(self):
        recovered = self.probe(self.result(255, b"", b"Connection reset by peer"), self.result())
        self.assertFalse(recovered["error"])
        self.assertTrue(recovered["view"]["reachable"])
        self.assertIs(self.sample, recovered["sample"])
        self.assertEqual(2, self.runner.call_count)
        self.fleet._retry_pause.assert_called_once_with(self.profile["key"])
        first, second = self.runner.call_args_list
        self.assertEqual(first, second)
        self.assertEqual(["fixture-ssh", "bash -s"], first.args[0])
        self.assertEqual(45, PROBE_TIMEOUT)
        self.assertEqual(PROBE_TIMEOUT, first.kwargs["timeout"])
        self.assertEqual(MAX_OUTPUT, first.kwargs["output_limit"])
        self.assertIsInstance(first.kwargs["input"], bytes)

    def test_repeated_transport_failure_stops_after_two_and_hides_diagnostics(self):
        error = self.result(255, b"", b"Connection reset by PRIVATE_SECRET_VALUE")
        failed = self.probe(error, error)
        self.assertEqual(2, self.runner.call_count)
        self.assertEqual("transport", failed["view"]["error_kind"])
        self.assertFalse(failed["view"]["reachable"])
        self.assertIn("已自动重试一次", failed["view"]["error"])
        self.assertNotIn("PRIVATE_SECRET_VALUE", json.dumps(failed))
        self.parser.assert_not_called()

    def test_authentication_and_host_key_errors_are_not_retried(self):
        for stderr, expected in ((b"Permission denied (publickey)", "authentication"),
                                 (b"Host key verification failed", "host_key")):
            with self.subTest(expected=expected):
                self.runner.reset_mock()
                failed = self.probe(self.result(255, b"", stderr))
                self.assertEqual(1, self.runner.call_count)
                self.assertEqual(expected, failed["view"]["error_kind"])
                self.assertEqual("down", failed["view"]["verdict"])
        self.fleet._retry_pause.assert_not_called()

    def test_remote_permission_error_after_marker_preserves_reachability(self):
        failed = self.probe(self.result(1, stderr=b"Permission denied PRIVATE_SECRET_VALUE"))
        self.assertTrue(failed["view"]["reachable"])
        self.assertEqual("unknown", failed["view"]["verdict"])
        self.assertEqual("remote_command", failed["view"]["error_kind"])
        self.assertNotIn("认证失败", failed["view"]["error"])
        self.assertNotIn("PRIVATE_SECRET_VALUE", json.dumps(failed))
        self.fleet._retry_pause.assert_not_called()
        self.parser.assert_not_called()

    def test_incomplete_smi_result_remains_unknown_without_reconnecting(self):
        self.good_view.update(verdict="unknown", collection_complete=False, error="厂商指标不完整")
        failed = self.probe(self.result())
        self.assertTrue(failed["error"])
        self.assertTrue(failed["view"]["reachable"])
        self.assertEqual("厂商指标不完整", failed["view"]["error"])
        self.assertEqual(1, self.runner.call_count)
        self.fleet._retry_pause.assert_not_called()

    def test_missing_probe_response_can_recover_but_never_means_device_down(self):
        missing = {"reachable": False, "verdict": "down", "error": "missing marker"}
        self.parser.side_effect = [(missing, None), (copy.deepcopy(self.good_view), self.sample)]
        recovered = self.probe(self.result(stdout=b"gateway message"), self.result())
        self.assertFalse(recovered["error"])
        self.assertEqual(2, self.runner.call_count)
        self.parser.side_effect = lambda *args: (missing, None)
        self.runner.reset_mock()
        failed = self.probe(self.result(stdout=b""), self.result(stdout=b""))
        self.assertEqual(2, self.runner.call_count)
        self.assertIsNone(failed["view"]["reachable"])
        self.assertEqual("unknown", failed["view"]["verdict"])
        self.assertEqual("protocol", failed["view"]["error_kind"])

    def test_parser_rejection_with_marker_does_not_crash_or_retry(self):
        self.parser.side_effect = lambda *args: ({"reachable": False}, None)
        failed = self.probe(self.result())
        self.assertEqual("parsing", failed["view"]["error_kind"])
        self.assertEqual("unknown", failed["view"]["verdict"])
        self.assertEqual(1, self.runner.call_count)
        self.fleet._retry_pause.assert_not_called()

    def test_timeout_preserves_connection_stage_and_retries_only_once(self):
        for output, expected in ((None, None), (self.marker, True)):
            with self.subTest(connected=expected):
                self.runner.reset_mock()
                error = subprocess.TimeoutExpired("private-command", 45, output=output,
                                                  stderr=b"PRIVATE_SECRET_VALUE")
                failed = self.probe(error, error)
                self.assertEqual(2, self.runner.call_count)
                self.assertIs(expected, failed["view"]["reachable"])
                self.assertEqual("unknown", failed["view"]["verdict"])
                self.assertEqual("timeout", failed["view"]["error_kind"])
                self.assertNotIn("PRIVATE_SECRET_VALUE", json.dumps(failed))
                self.assertNotIn("private-command", json.dumps(failed))

    def test_epoch_change_between_attempts_cancels_reconnect(self):
        def change_epoch(key):
            self.fleet._source_epoch += 1
            return False
        self.fleet._retry_pause.side_effect = change_epoch
        failed = self.probe(self.result(255, b"", b"Connection closed"))
        self.assertEqual(1, self.runner.call_count)
        self.assertEqual("cancelled", failed["view"]["error_kind"])
        self.assertIsNone(failed["view"]["reachable"])

    def test_close_during_backoff_cancels_reconnect(self):
        def stop(key):
            self.fleet.close()
            return True
        self.fleet._retry_pause.side_effect = stop
        failed = self.probe(self.result(255, b"", b"Connection closed"))
        self.assertEqual(1, self.runner.call_count)
        self.assertEqual("cancelled", failed["view"]["error_kind"])

    def test_invalid_authorization_cancels_before_any_probe(self):
        with patch.object(self.fleet, "_reconcile_source", return_value={"epoch": 0, "valid": False}):
            failed = self.probe()
        self.runner.assert_not_called()
        self.assertEqual("cancelled", failed["view"]["error_kind"])

    def test_combined_output_limit_does_not_retry_or_publish_raw_output(self):
        failed = self.probe(self.result(stdout=b"x" * MAX_OUTPUT, stderr=b"PRIVATE_SECRET_VALUE"))
        self.assertEqual(1, self.runner.call_count)
        self.assertEqual("output_limit", failed["view"]["error_kind"])
        self.assertIsNone(failed["view"]["reachable"])
        self.assertNotIn("PRIVATE_SECRET_VALUE", json.dumps(failed))
        self.fleet._retry_pause.assert_not_called()

    def test_local_timeout_carries_bounded_partial_connection_marker(self):
        code = "import sys,time;print('jms-top-ok',flush=True);sys.stderr.write('fixture detail');sys.stderr.flush();time.sleep(10)"
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.fleet._run_probe([sys.executable, "-c", code], input=b"", timeout=2, output_limit=1024)
        self.assertIn(self.marker.strip(), caught.exception.output)
        self.assertLessEqual(len(caught.exception.output) + len(caught.exception.stderr), 1024)
        self.assertEqual(set(), self.fleet._processes)


if __name__ == "__main__":
    unittest.main()
