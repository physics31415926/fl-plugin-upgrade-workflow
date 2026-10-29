import unittest
from fl_dashboard.monitoring.pr_discovery import adaptation_backend

class DiscoveryTests(unittest.TestCase):
    def test_description_identifies_arbitrary_branch(self):
        self.assertEqual(adaptation_backend({"title": "Runtime compatibility", "head": {"ref": "work/runtime"}, "body": "### Summary\nAdapt vllm-plugin-FL to the Kunlunxin P800 backend on vLLM 0.28.0.\n### Changes\nCUDA and iluvatar utilities are reused."}), "kunlunxin")

    def test_generic_fix_and_branch_alone_are_not_adaptation(self):
        for title in ("fix(metax): route reshape_and_cache_flash through FL dispatch", "CI workflow changes", "Update backend"):
            self.assertIsNone(adaptation_backend({"title": title, "head": {"ref": "adapt/metax-runtime"}, "body": "### Description\nPort of the same fix from main to vLLM 0.28. MetaX backend tests passed."}))

    def test_conflicting_purpose_is_not_assigned(self):
        self.assertIsNone(adaptation_backend({"title": "adapt(kunlunxin): runtime", "body": "### Summary\nAdapt vllm-plugin-FL to MetaX."}))

    def test_multiple_targets_not_assigned(self):
        self.assertIsNone(adaptation_backend({"body": "Adapt vllm-plugin-FL to Kunlunxin and MetaX."}))

    def test_nonpurpose_evidence_ignored(self):
        text = "Adapt vllm-plugin-FL to Kunlunxin."
        for body in ("<!--"+text+"-->", "> "+text, "| "+text+" |", "- [ ] "+text, "### Tests\n"+text, chr(96)*3+"\n"+text+"\n"+chr(96)*3):
            with self.subTest(body=body):
                self.assertIsNone(adaptation_backend({"body": body}))

    def test_alias_and_word_boundaries(self):
        self.assertEqual(adaptation_backend({"title": "upgrade(iluvatar): runtime"}), "corex")
        self.assertIsNone(adaptation_backend({"title": "adapt(kunlunxinplus): runtime"}))
        for alias in ("sunrise", "曦望"):
            self.assertEqual(adaptation_backend({"title": f"upgrade({alias}): runtime"}), "sunrise")
        self.assertIsNone(adaptation_backend({"title": "upgrade(sunriseplus): runtime"}))


    def test_sglang_feature_titles_and_vendor_first_descriptions(self):
        for alias, backend in (("hygon", "hygon"), ("iluvatar", "corex"), ("mthreads", "musa")):
            self.assertEqual(adaptation_backend({"title": f"feat({alias}): upgrade empty runtime to SGLang 0.5.18"}), backend)
            self.assertEqual(adaptation_backend({"body": f"Upgrade the {alias} empty backend to SGLang 0.5.18, adapt interfaces."}), backend)
        self.assertEqual(adaptation_backend({"title": "feat(iluvatar): complete SGLang 0.5.18 empty runtime adaptation"}), "corex")
        self.assertIsNone(adaptation_backend({"title": "feat(musa): add operator helper"}))

    def test_amd_and_rocm_aliases_identify_the_same_adaptation(self):
        for alias in ("amd", "rocm", "AMD ROCm"):
            with self.subTest(alias=alias):
                self.assertEqual(adaptation_backend({
                    "title": f"adapt({alias}): backend upgrade",
                    "head": {"ref": "adapt/amd-runtime-upgrade"},
                }), "amd")
        self.assertIsNone(adaptation_backend({"title": "adapt(amdgpuhelper): runtime"}))
        self.assertIsNone(adaptation_backend({"title": "adapt(rocmplus): runtime"}))
        self.assertIsNone(adaptation_backend({
            "title": "adapt(amd): backend upgrade",
            "head": {"ref": "adapt/cuda-runtime-upgrade"},
        }))

    def test_added_backend_purpose_identifies_amd_without_special_title(self):
        body = ("Adds AMD (ROCm/HIP) as a `vllm-plugin-FL` backend on vLLM 0.32, "
                "validated on AMD W7900D. CUDA utilities are reused.")
        self.assertEqual(adaptation_backend({"title": "New runtime support", "body": body}), "amd")
        self.assertEqual(adaptation_backend({
            "body": "### Summary\nThis PR adds ROCm as an SGLang-Plugin-FL backend for SGLang 0.5.18."
        }), "amd")

    def test_amd_evidence_or_dependencies_are_not_adaptation_intent(self):
        bodies = (
            "Adds AMD ROCm support libraries for vLLM 0.32.",
            "Adds an AMD test device as a CI runner for vLLM 0.32.",
            "Adds AMD examples and ROCm installation notes.",
            "### Tests\nAdds AMD as a vllm-plugin-FL backend on vLLM 0.32.",
            "### Summary\nValidated the CUDA change on AMD (ROCm/HIP).",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertIsNone(adaptation_backend({"title": "Update shared utilities", "body": body}))
        self.assertEqual(adaptation_backend({
            "body": "Adds MetaX as a vllm-plugin-FL backend on vLLM 0.32, reusing AMD ROCm helpers."
        }), "metax")
        self.assertIsNone(adaptation_backend({
            "body": "Adds AMD and MetaX as a vllm-plugin-FL backend on vLLM 0.32."
        }))
