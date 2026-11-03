"""NVIDIA registry and CI naming changes must keep one backend identity."""
import unittest
import test_discovered_backend_rows as fixture


class NvidiaAliasTests(unittest.TestCase):
    transport = fixture.DiscoveredBackendRowsTests.transport
    status = fixture.DiscoveredBackendRowsTests.status

    def setUp(self):
        fixture.DiscoveredBackendRowsTests.setUp(self)
        self.prs[0].update(title="adapt(nvidia): upgrade vLLM runtime", draft=False,
            body="## Tests\n| Test | Result |\n| --- | --- |\n| Runtime | PASS |",
            head={"ref": "adapt/cuda-runtime", "sha": "c" * 40})
        self.checks = []

    def monitor(self, registry, engine="vllm"):
        monitor = fixture.DiscoveredBackendRowsTests.monitor(self, engine)
        monitor.backend_prs = {"cuda": 901}
        monitor._registry = lambda sha: registry
        return monitor

    def test_registry_aliases_generate_one_row_and_keep_pr(self):
        for engine in ("vllm", "sglang"):
            for registry in ({"cuda": True}, {"nvidia": True}, {"cuda": False, "nvidia": True}):
                with self.subTest(engine=engine, registry=registry):
                    monitor = self.monitor(registry, engine)
                    snapshot = self.status(monitor)
                    self.assertEqual(len(snapshot["backends"]), 1)
                    row = snapshot["backends"][0]
                    self.assertEqual(row["id"], "cuda")
                    self.assertEqual(row["label"], "NVIDIA / 英伟达")
                    self.assertEqual(row["pr"]["number"], 901)
                    self.assertTrue(row["ci_enabled"])
                    self.assertEqual(row["acceptance"]["pr_number"], 901)
                    self.assertEqual(monitor.backend_prs, {"cuda": 901})

    def test_old_and_new_pr_ci_names_exclude_other_backends(self):
        for name in ("cuda", "nvidia"):
            with self.subTest(platform=name):
                self.checks = [{"name": job, "status": "completed", "conclusion": result}
                    for job, result in ((f"test-{name} / unit", "success"),
                        ("test-amd / unit", "failure"),
                        (f"test-{name} / notify / Notify", "failure"),
                        (f"test-{name} / Setup (runner)", "failure"),
                        ("test-nvidia-other / unit", "failure"))]
                row = self.status(self.monitor({"nvidia": True}))["backends"][0]
                self.assertEqual(row["ci"]["status"], "passed")
                self.assertEqual(row["ci"]["commit"], "c" * 40)

    def test_failure_under_either_alias_is_not_hidden(self):
        self.checks = [{"name": "test-cuda / unit", "status": "completed", "conclusion": "success"},
                       {"name": "test-nvidia / e2e", "status": "completed", "conclusion": "failure"}]
        row = self.status(self.monitor({"nvidia": True}))["backends"][0]
        self.assertEqual(row["ci"]["status"], "failed")

    def test_bare_job_alias_is_recognized(self):
        monitor = self.monitor({"nvidia": True})
        result = monitor._branch_backend_ci("cuda", [
            {"name": "test-nvidia", "status": "completed", "conclusion": "skipped"}], {})
        self.assertEqual(result["status"], "skipped")

    def test_custom_mapping_is_not_expanded(self):
        monitor = self.monitor({"custom": True})
        monitor.backend_ci_platforms = {"cuda": "custom"}
        self.assertEqual(monitor._ci_test_jobs("cuda", [{"name": "test-nvidia / unit"}]), [])
        self.assertEqual(monitor._ci_platforms("cuda"), ("custom",))
        job = {"name": "test-custom / unit"}
        self.assertEqual(monitor._ci_test_jobs("cuda", [job]), [job])

    def test_branch_switch_does_not_mutate_alias_mapping(self):
        monitor = self.monitor({})
        original = dict(monitor.backend_ci_platforms)
        monitor._registry = lambda sha: {"nvidia" if sha == "b" * 40 else "cuda": True}
        for branch in ("stable", "next-release", "stable"):
            snapshot = self.status(monitor, branch)
            self.assertEqual([row["id"] for row in snapshot["backends"]], ["cuda"])
            self.assertEqual(monitor.backend_ci_platforms, original)
            self.assertEqual(snapshot["backends"][0]["pr"] is not None, branch == "next-release")


if __name__ == "__main__":
    unittest.main()
