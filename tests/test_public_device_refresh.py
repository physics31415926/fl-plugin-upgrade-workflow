"""Anonymous device refreshes remain scoped, coalesced and publicly projected."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import Mock, patch

from fl_dashboard.services.public_devices import MAX_RESPONSE, PublicDeviceRefresher, read_public_devices


CONTROL = "http://127.0.0.1:8766"


def snapshot(project="vllm", **fields):
    return {"project_id": project, "hosts": [], "refreshing": False, **fields}


class FakeResponse:
    def __init__(self, value, status=200, raw=None):
        self.status = status
        self.raw = json.dumps(value).encode() if raw is None else raw
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.raw[:size]


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class PublicDeviceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.refresher = PublicDeviceRefresher(CONTROL, clock=lambda: self.now)

    def test_fixed_post_uses_empty_body_and_response_keeps_public_projection(self):
        before = FakeConnection(FakeResponse(snapshot()))
        response = FakeResponse(snapshot(refreshing=True, read_only=False, device_refresh=False,
                                         public_refresh_retry_after=99999, private="SECRET", hosts=[
            {"id": "gpu", "ssh_connection": {"key": "SECRET"}, "node": {}, "accel": {}}]), status=202)
        after = FakeConnection(response)
        with patch("fl_dashboard.services.public_devices.HTTPConnection", side_effect=[before, after]) as factory:
            result = self.refresher.refresh("vllm")
        self.assertEqual(("GET", "/api/devices?project=vllm", None), before.requests[0][:3])
        self.assertEqual(("POST", "/api/devices/refresh?project=vllm", b"{}"), after.requests[0][:3])
        headers = after.requests[0][3]
        self.assertEqual(CONTROL, headers["Origin"])
        self.assertEqual("127.0.0.1:8766", headers["Host"])
        self.assertEqual("application/json", headers["Content-Type"])
        self.assertEqual("same-origin", headers["Sec-Fetch-Site"])
        self.assertNotIn("Cookie", headers)
        self.assertTrue(result["read_only"])
        self.assertTrue(result["device_refresh"])
        self.assertEqual(30, result["public_refresh_retry_after"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertTrue(before.closed and after.closed)
        self.assertEqual([MAX_RESPONSE + 1], response.read_sizes)
        factory.assert_called_with("127.0.0.1", 8766, timeout=8)

    def test_cooldown_is_shared_across_projects_and_available_in_cached_reads(self):
        def read(url, project):
            return snapshot(project)
        with patch("fl_dashboard.services.public_devices.read_public_devices", side_effect=read), \
                patch("fl_dashboard.services.public_devices._request_devices", side_effect=lambda url, project, **kw: snapshot(project, refreshing=True)) as post:
            self.assertEqual(0, self.refresher.snapshot("vllm")["public_refresh_retry_after"])
            self.refresher.refresh("vllm")
            self.now += 5.2
            second = self.refresher.refresh("sglang")
            self.assertEqual("sglang", second["project_id"])
            self.assertEqual(25, second["public_refresh_retry_after"])
            self.assertEqual(25, self.refresher.snapshot("vllm")["public_refresh_retry_after"])
            self.assertEqual(1, post.call_count)
            self.now = 130
            self.refresher.refresh("sglang")
            self.assertEqual(2, post.call_count)

    def test_already_running_sweep_is_reused_without_post(self):
        with patch("fl_dashboard.services.public_devices.read_public_devices", return_value=snapshot(refreshing=True)), \
                patch("fl_dashboard.services.public_devices._request_devices") as post:
            result = self.refresher.refresh("vllm")
        post.assert_not_called()
        self.assertTrue(result["refreshing"])
        self.assertEqual(30, result["public_refresh_retry_after"])

    def test_simultaneous_visitors_cannot_duplicate_post_or_block_cache_reads(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def post(url, project, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test did not release POST")
            return snapshot(project, refreshing=True)
        with patch("fl_dashboard.services.public_devices.read_public_devices", side_effect=lambda url, project: snapshot(project)), \
                patch("fl_dashboard.services.public_devices._request_devices", side_effect=post) as write, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.refresher.refresh, "vllm")
            self.assertTrue(entered.wait(1))
            self.now = 140  # The busy claim remains effective even if its timer expires.
            other = pool.submit(self.refresher.refresh, "sglang")
            second = other.result(timeout=1)
            self.assertEqual("sglang", second["project_id"])
            self.assertEqual(1, second["public_refresh_retry_after"])
            self.assertEqual(1, write.call_count)
            release.set()
            self.assertTrue(first.result(timeout=1)["refreshing"])

    def test_failed_post_releases_busy_claim_but_keeps_cooldown_and_hides_diagnostic(self):
        with patch("fl_dashboard.services.public_devices.read_public_devices", side_effect=lambda url, project: snapshot(project)), \
                patch("fl_dashboard.services.public_devices._request_devices", side_effect=OSError("PRIVATE_FAILURE")) as post:
            with self.assertRaises(ValueError) as raised:
                self.refresher.refresh("vllm")
            self.assertNotIn("PRIVATE_FAILURE", str(raised.exception))
            self.assertFalse(self.refresher._busy)
            self.assertEqual(30, self.refresher.refresh("sglang")["public_refresh_retry_after"])
            self.assertEqual(1, post.call_count)
            self.now = 130
            with self.assertRaises(ValueError):
                self.refresher.refresh("sglang")
            self.assertEqual(2, post.call_count)

    def test_invalid_project_or_control_target_cannot_reach_network(self):
        with patch("fl_dashboard.services.public_devices.HTTPConnection") as connection:
            for project in ("", "../escape", "vllm&host=elsewhere", "vllm?project=other", {}, None):
                with self.subTest(project=project), self.assertRaises(ValueError):
                    self.refresher.refresh(project)
            for url in ("https://127.0.0.1:8766", "http://external.invalid:8766", CONTROL + "/api/devices"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    PublicDeviceRefresher(url)
            connection.assert_not_called()
        self.assertEqual(0, self.refresher.retry_after())

    def test_cooldown_configuration_is_bounded(self):
        for cooldown in (True, None, 0, -1, 301, float("inf"), float("nan"), "30"):
            with self.subTest(cooldown=cooldown), self.assertRaises(ValueError):
                PublicDeviceRefresher(CONTROL, cooldown=cooldown)

    def test_public_get_never_posts_and_ignores_upstream_privileges(self):
        connection = FakeConnection(FakeResponse(snapshot(read_only=False, device_refresh=False, sessions=True)))
        with patch("fl_dashboard.services.public_devices.HTTPConnection", return_value=connection):
            result = read_public_devices(CONTROL, "vllm")
        self.assertEqual("GET", connection.requests[0][0])
        self.assertTrue(result["read_only"] and result["device_refresh"])
        self.assertNotIn("sessions", result)

    def test_upstream_redirect_oversize_or_malformed_data_is_not_relayed(self):
        cases = [FakeResponse({}, status=302, raw=b"PRIVATE_REDIRECT"),
                 FakeResponse({}, raw=b"PRIVATE_INVALID_JSON"),
                 FakeResponse(snapshot("sglang")),
                 FakeResponse({}, raw=b"x" * (MAX_RESPONSE + 1))]
        for response in cases:
            connection = FakeConnection(response)
            with self.subTest(status=response.status, size=len(response.raw)), \
                    patch("fl_dashboard.services.public_devices.HTTPConnection", return_value=connection), \
                    self.assertRaises(ValueError) as raised:
                read_public_devices(CONTROL, "vllm")
            self.assertNotIn("PRIVATE", str(raised.exception))
            self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
