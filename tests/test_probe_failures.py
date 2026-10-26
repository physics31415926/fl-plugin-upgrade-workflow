"""Synthetic SSH failures; no real connections or captured credentials."""
import json
import unittest

from fl_dashboard.resources.probe_failures import classify_failure


READY = "jms-top-ok\n##SYS\n"
SECRET = "PRIVATE_DIAGNOSTIC_SENTINEL_DO_NOT_EXPOSE"


class ProbeFailureTests(unittest.TestCase):
    def assert_failure(self, result, *, kind, reachable, retryable):
        self.assertEqual({"message", "reachable", "retryable", "kind"}, set(result))
        self.assertEqual(kind, result["kind"])
        self.assertIs(reachable, result["reachable"])
        self.assertIs(retryable, result["retryable"])
        self.assertIsInstance(result["message"], str)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_successful_start_is_left_to_parser_even_when_metrics_are_incomplete(self):
        for out in (READY, READY.encode(), "banner\njms-top-ok\n", b"\xff\njms-top-ok\n"):
            with self.subTest(out=out):
                self.assertIsNone(classify_failure(0, out, "Permission denied " + SECRET))

    def test_zero_exit_without_marker_is_unknown_protocol_not_offline(self):
        for output in ("", "gateway refused " + SECRET, "Permission denied " + SECRET,
                       "one\ntwo\nthree\njms-top-ok\n", "prefix-jms-top-ok"):
            for encode in (str, lambda value: value.encode()):
                with self.subTest(output=output, encoded=encode is not str):
                    self.assert_failure(classify_failure(0, encode(output), b""),
                                        kind="protocol", reachable=None, retryable=True)

    def test_host_key_errors_are_explicit_and_not_retried(self):
        for text in ("Host key verification failed.", "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!"):
            for out, err in (("", text), (text, "")):
                with self.subTest(text=text, stream="stderr" if err else "stdout"):
                    self.assert_failure(classify_failure(255, out, err + SECRET),
                                        kind="host_key", reachable=False, retryable=False)

    def test_authentication_errors_are_explicit_and_not_retried(self):
        for text in ("Permission denied (publickey,password).", "Authentication failed.",
                     "Too many authentication failures", "No supported authentication methods available"):
            with self.subTest(text=text):
                self.assert_failure(classify_failure(255, b"", (text + SECRET).encode()),
                                    kind="authentication", reachable=False, retryable=False)

    def test_known_transport_errors_are_retryable_without_exposing_details(self):
        for text in ("Connection reset by peer", "Connection closed by remote host", "Connection timed out",
                     "Operation timed out", "Connection refused", "Connection aborted", "Network is unreachable",
                     "No route to host", "kex_exchange_identification: read failed",
                     "ssh_exchange_identification: read failed", "banner exchange: rejected",
                     "Temporary failure in name resolution"):
            with self.subTest(text=text):
                self.assert_failure(classify_failure(255, "", text + " " + SECRET),
                                    kind="transport", reachable=False, retryable=True)

    def test_unknown_ssh_failure_does_not_assert_asset_is_down(self):
        for text in ("", SECRET, "Unexpected SSH diagnostic " + SECRET):
            with self.subTest(text=text):
                self.assert_failure(classify_failure(255, "", text),
                                    kind="connection", reachable=None, retryable=True)

    def test_ready_marker_wins_over_auth_and_network_text_after_connection(self):
        for text in ("Permission denied", "Host key verification failed", "Connection reset", SECRET):
            with self.subTest(text=text):
                result = classify_failure(255, READY, text + SECRET)
                self.assert_failure(result, kind="transport", reachable=True, retryable=True)
                self.assertIn("已连接设备，但采集", result["message"])
                self.assertNotIn("认证失败", result["message"])

    def test_non_ssh_exit_is_command_failure_even_for_permission_or_network_text(self):
        for code in (1, 2, 126, 127, -1):
            for output, reachable in ((READY, True), ("", None)):
                for text in ("Permission denied", "Host key verification failed", "Connection refused"):
                    with self.subTest(code=code, reached=reachable, text=text):
                        result = classify_failure(code, output, text + SECRET)
                        self.assert_failure(result, kind="remote_command", reachable=reachable, retryable=False)
                        self.assertNotIn("认证失败", result["message"])
                        if reachable:
                            self.assertIn("已连接设备，但采集", result["message"])

    def test_unexpected_stream_types_are_safe_and_raw_fields_are_never_returned(self):
        self.assert_failure(classify_failure(255, None, object()),
                            kind="connection", reachable=None, retryable=True)
        result = classify_failure(255, SECRET, b"Connection reset " + SECRET.encode())
        self.assertNotIn("stdout", result)
        self.assertNotIn("stderr", result)
        self.assertNotIn("returncode", result)
        self.assertNotIn(SECRET, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
