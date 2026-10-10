"""Historical notes are inspectable without granting current-source Gate passes."""
from __future__ import annotations

import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from acceptance_records import AcceptanceRecords
from branch_monitor import BranchMonitor, MonitorError


BRANCH = "0.4.0-dev"
GITHUB_REPO = "example/plugin"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


class AcceptanceRecordStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence"
        self.records = self.evidence / "ascend" / "records"
        self.records.mkdir(parents=True)
        self.store = AcceptanceRecords(self.evidence, "vllm", GITHUB_REPO)
        self.note = {
            "schema_version": 1, "id": "pr487-scope", "engine": "vllm",
            "backend": "ascend", "branch": BRANCH, "status": "partial", "pr_number": 487,
            "title": "Recorded experiment", "summary": "15 scenarios, 78 requests; one group skipped",
            "scope": "27B eager/graph and 35B eager", "recorded_at": "2026-09-22",
            "tested_commit": "a" * 40, "tested_tree": "b" * 40,
        }

    def write_note(self, **changes):
        value = dict(self.note, **changes)
        (self.records / "pr487-scope.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def write_archive(self, data=b"# Recorded workload\n15 scenarios passed; 35B graph skipped.\n"):
        path = self.records / "pr487.md"
        path.write_bytes(data)
        return {"file": path.name, "sha256": hashlib.sha256(data).hexdigest()}

    def test_verified_archive_returns_original_text_and_hides_internal_paths_in_metadata(self):
        original = b"\xef\xbb\xbf# Historical experiment\r\n78 requests passed.\r\n"
        self.write_note(archive=self.write_archive(original))
        entry = self.store.entries("ascend", BRANCH)[0]
        self.assertTrue(entry["archive_available"])
        public = self.store.public(entry)
        self.assertEqual(public["pr_url"], "https://github.com/example/plugin/pull/487")
        self.assertEqual(public["tested_commit"], "a" * 40)
        self.assertFalse(any(key.startswith("_") for key in public))
        self.assertNotIn(str(self.root), json.dumps(public))
        self.assertEqual(self.store.read_archive("ascend", BRANCH, "pr487-scope"),
                         {"filename": "pr487.md", "content": original.decode("utf-8-sig")})

    def test_branch_engine_backend_and_filename_identity_cannot_cross_projects(self):
        for changes in ({"branch": "main"}, {"engine": "sglang"}, {"backend": "cuda"},
                        {"id": "different-record"}):
            with self.subTest(changes=changes):
                self.write_note(**changes)
                entries = self.store.entries("ascend", BRANCH, {"number": 487})
                self.assertEqual([entry["source"] for entry in entries], ["pr_reference"])
                self.assertFalse(entries[0]["archive_available"])
                with self.assertRaises(ValueError):
                    self.store.read_archive("ascend", BRANCH, "pr487-scope")
        self.assertEqual(self.store.entries("../ascend", BRANCH), [])
        with self.assertRaisesRegex(ValueError, "Invalid acceptance record ID"):
            self.store.read_archive("ascend", BRANCH, "../../private")

    def test_digest_failure_and_missing_archive_keep_the_pr_reference(self):
        archive = self.write_archive()
        for candidate in ({**archive, "sha256": "0" * 64},
                          {**archive, "file": "missing.md"},
                          {**archive, "sha256": "not-a-digest"}):
            with self.subTest(archive=candidate):
                self.write_note(archive=candidate)
                entry = self.store.entries("ascend", BRANCH)[0]
                self.assertFalse(entry["archive_available"])
                self.assertEqual(entry["pr_url"], "https://github.com/example/plugin/pull/487")
                with self.assertRaisesRegex(ValueError, "use the PR reference"):
                    self.store.read_archive("ascend", BRANCH, "pr487-scope")

    def test_absent_local_record_offers_selected_pr_without_claiming_acceptance(self):
        entry = self.store.entries("ascend", BRANCH, {"number": 487})[0]
        self.assertEqual(entry["id"], "pr-487")
        self.assertEqual(entry["status"], "reference")
        self.assertEqual(entry["source"], "pr_reference")
        self.assertFalse(entry["archive_available"])
        self.assertIsNone(entry["tested_commit"])
        self.assertEqual(self.store.entries("ascend", BRANCH), [])

    def test_archive_path_cannot_escape_even_with_matching_digest(self):
        secret = self.root / "private.txt"
        secret.write_bytes(b"private local information")
        digest = hashlib.sha256(secret.read_bytes()).hexdigest()
        for name in ("../../../private.txt", str(secret)):
            with self.subTest(file=name):
                self.write_note(archive={"file": name, "sha256": digest})
                entry = self.store.entries("ascend", BRANCH)[0]
                self.assertFalse(entry["archive_available"])
                with self.assertRaises(ValueError):
                    self.store.read_archive("ascend", BRANCH, "pr487-scope")

    def test_archive_symlink_cannot_escape_even_with_matching_digest(self):
        secret = self.root / "private.txt"
        secret.write_bytes(b"private local information")
        link = self.records / "linked.md"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic-link creation requires privileges unavailable on this host")
        self.write_note(archive={"file": link.name, "sha256": hashlib.sha256(secret.read_bytes()).hexdigest()})
        self.assertFalse(self.store.entries("ascend", BRANCH)[0]["archive_available"])
        with self.assertRaises(ValueError):
            self.store.read_archive("ascend", BRANCH, "pr487-scope")

    def test_record_metadata_symlink_cannot_escape(self):
        outside = self.root / "outside-note.json"
        outside.write_text(json.dumps(self.note), encoding="utf-8")
        try:
            (self.records / "pr487-scope.json").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic-link creation requires privileges unavailable on this host")
        entries = self.store.entries("ascend", BRANCH, {"number": 487})
        self.assertEqual([entry["source"] for entry in entries], ["pr_reference"])

    def test_non_text_and_oversized_archives_do_not_become_available(self):
        for data in (b"\xff\xfe invalid UTF-8", b"x" * 65):
            with self.subTest(data=data), patch("acceptance_records.MAX_ARCHIVE_BYTES", 64):
                self.write_note(archive=self.write_archive(data))
                self.assertFalse(self.store.entries("ascend", BRANCH)[0]["archive_available"])

    def write_legacy(self, **changes):
        value = {"schema_version": 1, "evidence_type": "audited_record", "backend": "ascend",
                 "branch": BRANCH, "tested_commit": "c" * 40, "tree": "d" * 40,
                 "verified_at": "2026-09-24", "sources": [{"url": "https://github.com/example/plugin/pull/487"}],
                 "gate": {"status": "passed", "scenarios_passed": 20, "scenarios_total": 20, "requests_total": 104}}
        value.update(changes)
        path = self.evidence / "ascend" / "acceptance.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return value, path

    def test_legacy_acceptance_keeps_historical_metadata_and_original_archive(self):
        value, path = self.write_legacy()
        entry = self.store.entries("ascend", BRANCH, {"number": 487})[0]
        self.assertEqual(entry["id"], "historical-acceptance")
        self.assertEqual(entry["source"], "audited_record")
        self.assertTrue(entry["archive_available"])
        self.assertEqual(entry["tested_commit"], value["tested_commit"])
        self.assertEqual(entry["tested_tree"], value["tree"])
        self.assertEqual(entry["recorded_at"], value["verified_at"])
        self.assertIn("20/20", entry["summary"])
        self.assertIn("104", entry["summary"])
        original = self.store.read_archive("ascend", BRANCH, "historical-acceptance")
        self.assertEqual(original["content"], path.read_text(encoding="utf-8"))
        self.assertEqual(self.store.entries("ascend", "main"), [])
        self.assertEqual(self.store.entries("cuda", BRANCH), [])

    def test_legacy_record_retains_its_source_pr_when_selected_pr_changes(self):
        self.write_legacy()
        entry = self.store.entries("ascend", BRANCH, {"number": 999})[0]
        self.assertEqual(entry["pr_number"], 487)
        self.assertEqual(entry["pr_url"], "https://github.com/example/plugin/pull/487")
        self.assertEqual([record for record in self.store.entries("ascend", BRANCH, {"number": 999})
                          if record.get("pr_number") == 999], [])

    def test_legacy_multiple_sources_select_only_a_pr_really_in_the_archive(self):
        self.write_legacy(sources=[{"url": "https://github.com/example/plugin/pull/89"},
                                  {"url": "https://github.com/example/plugin/pull/95"}])
        self.assertEqual(self.store.entries("ascend", BRANCH, {"number": 95})[0]["pr_number"], 95)
        self.assertEqual(self.store.entries("ascend", BRANCH, {"number": 999})[0]["pr_number"], 89)
        self.assertEqual(self.store.entries("ascend", BRANCH)[0]["pr_number"], 89)

    def test_legacy_explicit_valid_pr_identity_is_authoritative(self):
        self.write_legacy(pr_number=487, sources=[{"url": "https://example.test/experiment"}])
        entry = self.store.entries("ascend", BRANCH, {"number": 999})[0]
        self.assertEqual(entry["pr_number"], 487)
        self.assertEqual(entry["pr_url"], "https://github.com/example/plugin/pull/487")
        for number in (True, "487", 0, -1, 10_000_001):
            with self.subTest(number=number):
                self.write_legacy(pr_number=number)
                self.assertEqual(self.store.entries("ascend", BRANCH, {"number": 999})[0]["pr_number"], 487)

    def test_legacy_untrusted_source_urls_cannot_borrow_the_selected_pr_identity(self):
        urls = [
            "http://github.com/example/plugin/pull/487",
            "https://github.com.evil.test/example/plugin/pull/487",
            "https://evil.test/github.com/example/plugin/pull/487",
            "https://github.com/other/plugin/pull/487",
            "https://account@github.com/example/plugin/pull/487",
            "https://github.com:443/example/plugin/pull/487",
            "https://github.com/example/plugin/pull/0",
            "https://github.com/example/plugin/pull/0487",
            "https://github.com/example/plugin/pull/10000001",
            "https://github.com/example/plugin/pull/" + "9" * 5000,
            "https://github.com/example/plugin/pull/487/files",
            "https://github.com/example/plugin/pull/487\n",
            "https://[github.com/example/plugin/pull/487",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.write_legacy(sources=[{"url": url}])
                entry = self.store.entries("ascend", BRANCH, {"number": 487})[0]
                self.assertIsNone(entry["pr_number"])
                self.assertIsNone(entry["pr_url"])
                self.assertTrue(entry["archive_available"])

    def test_legacy_same_repo_string_source_keeps_canonical_pr_details(self):
        self.write_legacy(sources=["https://GITHUB.COM/EXAMPLE/PLUGIN/pull/487/?view=checks#discussion"])
        entry = self.store.entries("ascend", BRANCH)[0]
        self.assertEqual(entry["pr_number"], 487)
        self.assertEqual(entry["pr_url"], "https://github.com/example/plugin/pull/487")

    def test_legacy_original_archive_reads_without_a_selected_pr(self):
        value, path = self.write_legacy()
        entry = self.store.entries("ascend", BRANCH)[0]
        self.assertEqual(entry["pr_number"], 487)
        self.assertEqual(self.store.read_archive("ascend", BRANCH, "historical-acceptance"),
                         {"filename": path.name, "content": path.read_text(encoding="utf-8")})
        self.write_legacy(sources=[{"url": "https://example.test/experiment"}])
        self.assertIsNone(self.store.entries("ascend", BRANCH)[0]["pr_number"])
        self.assertEqual(self.store.read_archive("ascend", BRANCH, "historical-acceptance")["filename"],
                         "acceptance.json")

    def test_legacy_vllm_audit_is_not_exposed_as_sglang_acceptance(self):
        self.write_legacy()
        sglang = AcceptanceRecords(self.evidence, "sglang", GITHUB_REPO)
        self.assertEqual(sglang.entries("ascend", BRANCH), [])
        with self.assertRaises(ValueError):
            sglang.read_archive("ascend", BRANCH, "historical-acceptance")


class AcceptanceMonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "plugin"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        files = {
            ".github/configs/platforms.yml": "platforms:\n  ascend:\n    enabled: true\n",
            "pyproject.toml": '[project]\nname="fixture"\ndependencies=["vllm==0.28.0"]\n',
            "tools/adaptation-gate-cases/test_text.py": 'def test_text_single():\n    run_case("text_single")\n',
        }
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        git(self.repo, "update-ref", "refs/remotes/upstream/" + BRANCH, self.head)
        self.evidence = self.root / "evidence"
        self.records = self.evidence / "ascend" / "records"
        self.records.mkdir(parents=True)
        self.config = {"repo": str(self.repo), "remote": "upstream", "branch": BRANCH,
                       "github_repo": GITHUB_REPO, "evidence_dir": str(self.evidence),
                       "models": ["Model-A"], "modes": ["eager"], "backend_prs": {"ascend": 487}}
        self.requests = []
        self.monitor = BranchMonitor(self.config, transport=self.transport)

    def transport(self, url):
        self.requests.append(url)
        if "/actions/runs?" in url:
            return {"workflow_runs": []}
        if "/pulls?" in url:
            return []
        if "/pulls/487" in url:
            return {"number": 487, "state": "open", "draft": False, "merged": False,
                    "base": {"ref": BRANCH}, "head": {"sha": self.head},
                    "html_url": "https://github.com/example/plugin/pull/487", "title": "Fixture"}
        if "/check-runs?" in url:
            return {"check_runs": []}
        raise AssertionError(f"Unexpected network request: {url}")

    def status(self):
        return next(b for b in self.monitor.status()["backends"] if b["id"] == "ascend")

    def write_record(self, *, tested_commit=None, tested_tree=None):
        text = "# Archived experiment\nHistorically passed; current Gate is independently checked.\n"
        archive = self.records / "experiment.md"
        archive.write_bytes(text.encode("utf-8"))
        record = {"schema_version": 1, "id": "experiment", "engine": "vllm", "backend": "ascend",
                  "branch": BRANCH, "status": "passed", "pr_number": 487, "title": "Historical workload",
                  "tested_commit": tested_commit or self.head, "tested_tree": tested_tree or self.tree,
                  "archive": {"file": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}}
        (self.records / "experiment.json").write_text(json.dumps(record), encoding="utf-8")
        return text

    def test_passed_record_with_matching_tree_does_not_satisfy_missing_current_gate(self):
        before = self.status()
        self.write_record()
        after = self.status()
        self.assertEqual(after["gate"], before["gate"])
        self.assertEqual(after["results"], before["results"])
        self.assertEqual(after["gate"]["status"], "missing")
        self.assertEqual(after["gate"]["passed"], 0)
        entry = after["acceptance_records"][0]
        self.assertEqual(entry["status"], "passed")
        self.assertTrue(entry["tree_matches_head"])
        self.assertIn("record=experiment", entry["archive_url"])
        self.assertNotIn("_archive_path", entry)

    def test_historical_record_does_not_mask_failed_current_raw_result(self):
        run = self.evidence / "ascend" / "current-run"
        results = run / "results"
        results.mkdir(parents=True)
        manifest = {"schema_version": 1, "run_id": "current-run", "backend": "ascend", "branch": BRANCH,
                    "commit": self.head, "model": "Model-A", "mode": "eager", "port": 8000, "results_dir": "results"}
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        raw = {"schema_version": 1, "case": {"scenario": "text_single", "model_path": "/models/Model-A",
                "served_model_name": "Model-A", "port": "8000", "concurrency": 1}, "input": [{}],
               "output": [{"passed": False, "checks": {"quality": False}}],
               "summary": {"request_count": 1, "passed_count": 0, "failed_count": 1, "passed": False}}
        (results / "text_single.json").write_text(json.dumps(raw), encoding="utf-8")
        before = self.status()["gate"]
        self.assertEqual(before["status"], "failed")
        self.write_record()
        after = self.status()
        self.assertEqual(after["gate"], before)
        self.assertEqual(after["gate"]["failed"], 1)
        self.assertEqual(after["acceptance_records"][0]["status"], "passed")

    def test_local_monitor_reads_original_archive_and_checks_record_identity(self):
        original = self.write_record()
        self.assertEqual(self.monitor.acceptance_record("ascend", "experiment"),
                         {"filename": "experiment.md", "content": original})
        self.assertEqual(self.requests, [])
        for kwargs in ({"backend": "cuda", "record_id": "experiment"},
                       {"backend": "ascend", "record_id": "experiment", "branch": "main"},
                       {"backend": "ascend", "record_id": "../private"},
                       {"backend": "ascend", "record_id": "experiment", "branch": "../../private"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MonitorError):
                self.monitor.acceptance_record(**kwargs)

    def test_localhost_http_archive_endpoint_reads_verified_original_without_network(self):
        import flflow
        original = self.write_record()
        server = flflow.make_server(None, "127.0.0.1", 0, monitor=self.monitor)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/acceptance-record?backend=ascend&record=experiment&branch=" + BRANCH)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/plain; charset=utf-8")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.read().decode("utf-8"), original)
            self.assertEqual(self.requests, [])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_source_tree_difference_remains_visible_on_historical_record(self):
        self.write_record(tested_tree="0" * 40)
        entry = self.status()["acceptance_records"][0]
        self.assertFalse(entry["tree_matches_head"])
        self.assertTrue(entry["archive_available"])
        self.assertEqual(entry["status"], "passed")
        self.assertEqual(self.status()["gate"]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
