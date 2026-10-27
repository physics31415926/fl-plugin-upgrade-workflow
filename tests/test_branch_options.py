"""Task baseline choices must not wait for PR or CI collection."""
from http.client import HTTPConnection
import json
import threading
import unittest
from unittest.mock import Mock, call

from fl_dashboard.monitoring.branch_monitor import BranchMonitor
from fl_dashboard.projects.project_registry import ProjectContext, ProjectRegistry
from fl_dashboard.services.flflow import make_server


class BranchOptionsTests(unittest.TestCase):
    def test_monitor_only_reads_local_remote_refs(self):
        monitor = object.__new__(BranchMonitor)
        monitor.branch = "release/next"
        monitor._git = Mock(side_effect=[Mock(stdout="upstream\n"), Mock(stdout="refs/remotes/upstream/main\nrefs/remotes/upstream/release/next\nrefs/remotes/upstream/HEAD\n")])
        monitor.remote = "upstream"
        monitor.transport = Mock(side_effect=AssertionError("No network collection"))
        monitor.status = Mock(side_effect=AssertionError("No status collection"))
        self.assertEqual(monitor.branch_options(), {"branch": "release/next", "branches": ["main", "release/next"]})
        self.assertEqual(monitor._git.call_args_list, [call("remote"),
            call("for-each-ref", "--format=%(refname)", "refs/remotes/upstream/")])
        monitor.transport.assert_not_called()
        monitor.status.assert_not_called()

    def test_http_branch_choices_are_project_scoped_and_do_not_collect_status(self):
        projects = []
        for name, branch in (("vllm", "release/next"), ("sglang", "dev/next")):
            monitor = Mock(config={}, branch=branch)
            monitor.branch_options.return_value = {"branch": branch, "branches": [branch, "main"]}
            monitor.status.side_effect = AssertionError("No status collection")
            projects.append(ProjectContext(name, name, monitor=monitor))
        projects.append(ProjectContext("unavailable", "Unavailable"))
        server = make_server(None, "127.0.0.1", 0, registry=ProjectRegistry(projects))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(connection.close)
        for project in projects[:2]:
            connection.request("GET", "/api/branches?project="+project.id)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            value = json.loads(response.read())
            self.assertEqual(value["branch"], project.monitor.branch)
            self.assertEqual(value["branches"], [project.monitor.branch, "main"])
            project.monitor.branch_options.assert_called_once_with()
            project.monitor.status.assert_not_called()
        for suffix, expected in (("?project=unavailable", 503), ("?project=missing", 400),
                                 ("?project=vllm&branch=untrusted", 400)):
            connection.request("GET", "/api/branches"+suffix)
            response=connection.getresponse()
            self.assertEqual(response.status, expected)
            response.read()


if __name__ == "__main__":
    unittest.main()
