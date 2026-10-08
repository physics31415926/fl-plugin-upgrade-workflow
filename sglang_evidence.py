"""Discover SGLang source cases and archive outputs from their original functions.

The upstream examples do not persist JSON. This collector's explicit archive
schema retains their request inputs/outputs; the monitor rechecks every output
against definitions read from the selected Git commit. It does not run on CI
or treat a successful process exit as sufficient evidence.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


DEFAULT_SOURCES = ["examples", "tests/e2e_tests/concurrent"]


def _assignments(text, source):
    tree = ast.parse(text, filename=source)
    literals = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                literals[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    return tree, literals


def _env_default(tree, name):
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        for call in ast.walk(node.value):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "get" and
                    len(call.args) >= 2 and isinstance(call.args[0], ast.Constant) and call.args[0].value == name):
                try:
                    return ast.literal_eval(call.args[1])
                except (ValueError, TypeError):
                    pass
    return None


def _example_mode(tree):
    switches = {keyword.value.value for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Engine"
                for keyword in node.keywords if keyword.arg == "disable_cuda_graph" and
                isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, bool)}
    if len(switches) != 1:
        raise ValueError("Cannot derive one Engine CUDA graph mode from source")
    return "eager" if next(iter(switches)) else "graph"


def _block(text, key, indent):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == key + ":" and len(line) - len(line.lstrip()) == indent:
            result = []
            for child in lines[index + 1:]:
                if child.strip() and not child.lstrip().startswith("#") and len(child) - len(child.lstrip()) <= indent:
                    break
                result.append(child)
            return "\n".join(result)
    return ""


def _yaml_scalar(value):
    # Only the scalar/list subset used by source model acceptance definitions.
    # No tags, anchors, merges or object construction are accepted.
    value = value.strip()
    if not value or value.startswith(("&", "*", "!", "|", ">")):
        raise ValueError("Unsupported acceptance YAML scalar")
    if value in ("true", "false"):
        return value == "true"
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        if value.startswith("[") and value.endswith("]"):
            return [_yaml_scalar(item) for item in value[1:-1].split(",") if item.strip()]
        if any(char in value for char in "{}[]\n"):
            raise ValueError("Unsupported acceptance YAML value")
        return value.split(" #", 1)[0].strip()


def _yaml_field(text, key, default=None):
    match = re.search(r"^\s*" + re.escape(key) + r":\s*(.+)$", text, re.M)
    return _yaml_scalar(match[1]) if match else default


def _yaml_records(text):
    result = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            current = {}
            result.append(current)
            stripped = stripped[2:]
        if current is None or ":" not in stripped:
            raise ValueError("Unsupported acceptance YAML sequence")
        key, raw = stripped.split(":", 1)
        current[key] = _yaml_scalar(raw)
    return result


def _text_specs(prompts, expected=None, case_sensitive=False):
    result = []
    for item in prompts:
        if isinstance(item, str):
            prompt, values = item, (expected or {}).get(item, [])
        elif isinstance(item, dict):
            prompt, values = item.get("text"), item.get("expected", [])
        else:
            raise ValueError("Unsupported text request definition")
        if isinstance(values, str):
            values = [values]
        if not isinstance(prompt, str) or not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError("Invalid text request definition")
        result.append({"kind": "text", "prompt": prompt, "expected": values, "case_sensitive": case_sensitive})
    return result


def _vl_specs(cases):
    result = []
    for item in cases:
        if (not isinstance(item, dict) or not isinstance(item.get("image"), str) or
                not isinstance(item.get("question"), str) or not isinstance(item.get("expected"), list) or
                not all(isinstance(v, str) for v in item["expected"])):
            raise ValueError("Invalid image request definition")
        result.append({"kind": "vl", "image": Path(item["image"]).name,
                       "question": item["question"], "expected": item["expected"], "case_sensitive": False})
    return result


def request_specs(case):
    kind, count = case["request_kind"], case["requests"]
    text, vl = case.get("text_cases", []), case.get("vl_cases", [])
    if case["family"] == "offline":
        return text if kind == "text" else vl
    if kind == "text":
        return [text[i % len(text)] for i in range(count)]
    if kind == "vl":
        return [vl[i % len(vl)] for i in range(count)]
    n_text = count // 2
    return ([text[i % len(text)] for i in range(n_text)] +
            [vl[i % len(vl)] for i in range(count - n_text)])


def _case(source, blob, model, scenario, family, kind, count, text, vl, mode="eager", definitions=None, environment=None):
    if not isinstance(count, int) or isinstance(count, bool) or not (1 <= count <= 4096):
        raise ValueError("Request count must be between 1 and 4096")
    if (kind in ("text", "mixed") and not text) or (kind in ("vl", "mixed") and not vl):
        raise ValueError("Acceptance source has no request definitions")
    test = "test_concurrent" if family == "pytest_concurrent" else ("run_engine" if family == "offline" else "run_" + kind + "_concurrent")
    entrypoint = (["python", "-m", "pytest", source + "::test_concurrent", "-v", "-s"]
                  if family == "pytest_concurrent" else ["python", source] + (["--mode", kind] if family == "concurrent" else []))
    label = (("离线" if family == "offline" else "并发") + {"text": "文本", "vl": "图片", "mixed": "混合"}[kind]
             + f" {count} 请求 · " + ("pytest" if family == "pytest_concurrent" else "example"))
    return {"scenario": scenario, "label": label, "requests": count, "source": source, "file": source, "test": test,
            "source_blob": blob, "models": [model], "modes": [mode], "model": model,
            "family": family, "request_kind": kind, "text_cases": text, "vl_cases": vl,
            "entrypoint": entrypoint, "environment": environment or {"MODEL_PATH": "<model-path>"},
            "definition_files": definitions or [], "evidence_format": "sglang_raw_requests"}


def discover_cases(read_source, list_paths, read_blob, sha, models, sources):
    """Read every source/config from one immutable Git commit, without imports."""
    cases, warnings, seen = [], [], set()
    for prefix in sources:
        paths = list_paths(sha, prefix)
        if not paths:
            warnings.append({"source": prefix, "detail": "所选分支不存在该用例来源"})
            continue
        parsed = False
        for source in paths:
            if source in seen:
                parsed = True
                continue
            if not (source.endswith("_offline_inference.py") or source.endswith("_concurrent.py") or
                    source.endswith("test_concurrent_smoke.py")):
                continue
            seen.add(source)
            try:
                tree, values = _assignments(read_source(sha, source), source)
                blob = read_blob(sha, source)
                if source.endswith("test_concurrent_smoke.py"):
                    helper = "tests/e2e_tests/plugin_utils.py"
                    default_text = _text_specs(values.get("_DEFAULT_TEXT_CASES", []))
                    default_vl = _vl_specs(values.get("_DEFAULT_VL_CASES", []))
                    for config_file in list_paths(sha, "tests/models"):
                        if not config_file.endswith((".yaml", ".yml")):
                            continue
                        config_text = read_source(sha, config_file)
                        llm = _block(config_text, "llm", 0)
                        model = Path(str(_yaml_field(llm, "model", ""))).name
                        if model not in models:
                            continue
                        concurrent = _block(config_text, "concurrent", 0)
                        if not concurrent:
                            continue
                        count = _yaml_field(concurrent, "concurrent_n", 4)
                        kinds = _yaml_field(concurrent, "modes", ["text"])
                        text_block, vl_block = _block(concurrent, "text", 2), _block(concurrent, "vl", 2)
                        text_values = _yaml_records(_block(text_block, "prompts", 4)) if text_block else []
                        vl_values = _yaml_records(_block(vl_block, "cases", 4)) if vl_block else []
                        text = _text_specs(text_values) if text_values else default_text
                        vl = _vl_specs(vl_values) if vl_values else default_vl
                        mode = "eager" if _yaml_field(llm, "disable_cuda_graph", True) is True else "graph"
                        model_group, config_name = Path(config_file).parent.name, Path(config_file).stem
                        definitions = [{"file": config_file, "blob": read_blob(sha, config_file)},
                                       {"file": helper, "blob": read_blob(sha, helper)}]
                        for kind in kinds:
                            if kind not in ("text", "vl", "mixed"):
                                raise ValueError("Unsupported concurrent YAML mode")
                            env = {"FL_TEST_MODEL": model_group, "FL_TEST_CASE": config_name,
                                   "FL_CONCURRENT_MODES": kind, "FL_CONCURRENT_N": str(count)}
                            cases.append(_case(source, blob, model, "pytest_" + model_group + "_" + config_name + "_" + kind,
                                               "pytest_concurrent", kind, count, text, vl, mode, definitions, env))
                        parsed = True
                else:
                    model = Path(str(_env_default(tree, "MODEL_PATH") or "")).name
                    if model not in models:
                        continue
                    offline = source.endswith("_offline_inference.py")
                    text = _text_specs(values.get("TEXT_PROMPTS", []), values.get("TEXT_EXPECTED", {}), offline)
                    vl = _vl_specs(values.get("VL_CASES", []))
                    mode = _example_mode(tree)
                    stem = Path(source).stem
                    if offline:
                        for kind, specs in (("text", text), ("vl", vl)):
                            cases.append(_case(source, blob, model, stem + "_" + kind, "offline", kind, len(specs), text, vl, mode))
                    else:
                        count = int(_env_default(tree, "CONCURRENT_N"))
                        kinds = values.get("ALL_MODES", [])
                        for kind in kinds:
                            if kind not in ("text", "vl", "mixed"):
                                raise ValueError("Unsupported source concurrent mode")
                            cases.append(_case(source, blob, model, stem + "_" + kind, "concurrent", kind, count, text, vl, mode,
                                               environment={"MODEL_PATH": "<model-path>", "CONCURRENT_N": str(count)}))
                    parsed = True
            except (ValueError, TypeError, SyntaxError, RuntimeError) as exc:
                warnings.append({"source": source, "detail": "无法解析该分支的真实用例定义: " + str(exc)})
        if not parsed:
            warnings.append({"source": prefix, "detail": "来源中未找到配置模型的可解析验收用例"})
    scenarios = [case["scenario"] for case in cases]
    if len(set(scenarios)) != len(scenarios):
        raise ValueError("Duplicate SGLang acceptance scenario")
    for model in models:
        if not any(model in case["models"] for case in cases):
            warnings.append({"source": ", ".join(sources), "model": model, "detail": "所选分支缺少该配置模型的验收定义"})
    return cases, warnings


def _input(spec):
    return {key: spec[key] for key in (("kind", "prompt") if spec["kind"] == "text" else ("kind", "image", "question"))}


def _checks(spec, output):
    text = output.get("text") if isinstance(output, dict) else None
    nonempty = isinstance(text, str) and bool(text.strip()) and not output.get("error")
    haystack = text if spec["case_sensitive"] else (text.lower() if isinstance(text, str) else "")
    expected = not spec["expected"] or any((v if spec["case_sensitive"] else v.lower()) in haystack for v in spec["expected"])
    return {"nonempty": bool(nonempty), "expected": bool(nonempty and expected)}


def validate_result(value, manifest, case):
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("evidence_type") != "sglang_raw_requests":
        return "failed", "Expected SGLang raw-request archive schema", 0
    identity = value.get("case", {})
    if not isinstance(identity, dict):
        return "failed", "Missing SGLang case identity", 0
    expected_identity = {"scenario": case["scenario"], "source": case["source"], "source_blob": case["source_blob"],
                         "commit": manifest.get("commit"), "backend": manifest.get("backend"), "mode": manifest.get("mode"),
                         "request_count": case["requests"], "definition_files": case.get("definition_files", [])}
    if any(identity.get(key) != expected for key, expected in expected_identity.items()):
        return "failed", "SGLang result source, identity, or request count differs from selected commit", 0
    model_path = identity.get("model_path")
    if not isinstance(model_path, str) or Path(model_path).name != manifest.get("model"):
        return "failed", "SGLang result model differs from manifest", 0
    execution = value.get("execution")
    if (not isinstance(execution, dict) or execution.get("completed") is not True or
            type(execution.get("exit_code")) is not int or execution.get("exit_code") != 0 or
            execution.get("plugin_loaded") is not True or execution.get("plugin_active") is not True):
        return "failed", "SGLang execution incomplete, failed, or plugin activation unverified", 0
    rows, summary = value.get("requests"), value.get("summary")
    if not isinstance(rows, list) or not isinstance(summary, dict):
        return "failed", "SGLang raw request outputs or summary missing", 0
    specs = request_specs(case)
    passed = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or index >= len(specs):
            continue
        checks = _checks(specs[index], row.get("output"))
        if (row.get("input") == _input(specs[index]) and row.get("status") == "passed" and
                row.get("checks") == checks and all(checks.values())):
            passed += 1
    count = len(specs)
    if (len(rows) != count or passed != count or summary.get("request_count") != count or
            summary.get("passed_count") != count or summary.get("failed_count") != 0 or
            summary.get("skipped_count") != 0 or summary.get("passed") is not True):
        return "failed", "One or more SGLang raw requests failed, skipped, or differ from source definitions", passed
    return "passed", "All SGLang source inputs and output assertions verified", passed


def _actual_pairs(case, raw_pairs):
    specs = request_specs(case)
    if len(raw_pairs) != len(specs):
        raise ValueError("Source returned an unexpected number of requests")
    result = []
    for spec, (label, text) in zip(specs, raw_pairs):
        if isinstance(label, str):
            actual_input = {"kind": "text", "prompt": label}
        elif isinstance(label, dict):
            actual_input = {"kind": "vl", "image": Path(label.get("image", "")).name, "question": label.get("question")}
        else:
            raise ValueError("Source returned an unsupported request identity")
        result.append((spec, actual_input, text))
    return result


def _git(repo, *items):
    return subprocess.run(["git", "-c", "safe.directory=" + str(repo), "-C", str(repo), *items],
                          check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def collect(repo, commit, branch, backend, case, model_path, output_dir, run_id=None, source_cache=None):
    """Call unchanged source functions; preserve their raw returned output text."""
    run_id = run_id or uuid.uuid4().hex
    manifest = {"schema_version": 1, "engine": "sglang", "run_id": run_id, "backend": backend,
                "branch": branch, "commit": commit, "model": case["model"], "mode": case["modes"][0], "results_dir": "results"}
    run = Path(output_dir) / run_id
    manifest_path = run / "manifest.json"
    if (run / "results" / (case["scenario"] + ".json")).exists():
        raise ValueError("This run already contains the scenario; preserve its original evidence and choose a new run")
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        raise ValueError("Run manifest identity differs; preserve the original and choose a new run")
    os.environ["MODEL_PATH"] = model_path
    for key, value in case.get("environment", {}).items():
        if value != "<model-path>":
            os.environ[key] = value
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location("_flflow_sglang_original_case", repo / case["source"])
    module = importlib.util.module_from_spec(spec)
    pairs, execution_error, actual_model_path = [], None, None
    try:
        spec.loader.exec_module(module)
        if case["family"] == "pytest_concurrent":
            actual_model_path = module._CFG.model
            actual_model = module._CFG.engine_kwargs().get("model_path")
            if actual_model_path != model_path or actual_model != model_path:
                raise ValueError("Actual pytest model config differs from the requested model artifact")
            actual_graph_switch = module._engine_kwargs().get("disable_cuda_graph", True)
            if actual_graph_switch is not (case["modes"][0] == "eager"):
                raise ValueError("Actual pytest graph mode differs from selected source definition")
        else:
            actual_model_path = module.MODEL_PATH
            if actual_model_path != model_path:
                raise ValueError("Actual example model differs from the requested model artifact")
        kind = case["request_kind"]
        if case["family"] == "offline":
            cached = (source_cache or {}).get(case["source"])
            text_outputs, vl_outputs = cached if cached is not None else module.run_engine()
            if source_cache is not None:
                source_cache[case["source"]] = (text_outputs, vl_outputs)
            specs = request_specs(case)
            if len(text_outputs if kind == "text" else vl_outputs) != len(specs):
                raise ValueError("Source returned an unexpected number of requests")
            pairs = _actual_pairs(case, list(zip(module.TEXT_PROMPTS, text_outputs)) if kind == "text"
                                  else list(zip(module.VL_CASES, vl_outputs)))
            module.validate(text_outputs, vl_outputs)
        elif case["family"] == "concurrent":
            engine = module._make_engine()
            try:
                outputs = getattr(module, "run_" + kind + "_concurrent")(engine)
                if kind == "mixed":
                    text_pairs, vl_pairs = outputs
                    raw = text_pairs + vl_pairs
                    if len(raw) != case["requests"]:
                        raise ValueError("Source returned an unexpected number of concurrent requests")
                    pairs = _actual_pairs(case, raw)
                    module.validate_text(text_pairs)
                    module.validate_vl(vl_pairs)
                else:
                    if len(outputs) != case["requests"]:
                        raise ValueError("Source returned an unexpected number of concurrent requests")
                    pairs = _actual_pairs(case, outputs)
                    getattr(module, "validate_" + ("text" if kind == "text" else "vl"))(outputs)
            finally:
                engine.shutdown()
        else:
            name = "_run_" + kind + "_concurrent"
            original = getattr(module, name)
            def capture(engine):
                nonlocal pairs
                outputs = original(engine)
                raw = (outputs[0] + outputs[1] if kind == "mixed" else outputs)
                if len(raw) != case["requests"]:
                    raise ValueError("Source returned an unexpected number of pytest requests")
                pairs = _actual_pairs(case, raw)
                return outputs
            setattr(module, name, capture)
            module.test_concurrent()
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
            raise
        execution_error = type(exc).__name__ + ": " + str(exc)
    try:
        if _git(repo, "rev-parse", "HEAD") != commit or _git(repo, "status", "--porcelain"):
            execution_error = "Source repository changed during collection; pinned execution cannot be accepted"
    except (OSError, subprocess.CalledProcessError) as exc:
        execution_error = "Post-run source identity could not be checked: " + type(exc).__name__
    try:
        import sglang_fl
        loaded, active = bool(sglang_fl.is_plugin_loaded()), bool(sglang_fl.is_plugin_active())
    except Exception:
        loaded = active = False
    rows = []
    for definition, actual_input, text in pairs:
        output = {"text": text, "error": None}
        checks = _checks(definition, output)
        rows.append({"input": actual_input, "output": output, "checks": checks,
                     "status": "passed" if all(checks.values()) else ("skipped" if text is None else "failed")})
    passed = sum(row["status"] == "passed" for row in rows)
    skipped = sum(row["status"] == "skipped" for row in rows)
    value = {"schema_version": 1, "evidence_type": "sglang_raw_requests",
             "case": {"scenario": case["scenario"], "model_path": actual_model_path, "source": case["source"],
                      "source_blob": case["source_blob"], "commit": commit, "backend": backend,
                      "mode": manifest["mode"], "request_count": case["requests"], "definition_files": case["definition_files"]},
             "execution": {"completed": execution_error is None, "exit_code": 0 if execution_error is None else 1,
                           "error": execution_error, "plugin_loaded": loaded, "plugin_active": active},
             "requests": rows, "summary": {"request_count": len(rows), "passed_count": passed,
                       "failed_count": len(rows) - passed - skipped, "skipped_count": skipped,
                       "passed": execution_error is None and len(rows) == case["requests"] and passed == len(rows)}}
    (run / "results").mkdir(parents=True, exist_ok=True)
    if (run / "results" / (case["scenario"] + ".json")).exists():
        raise ValueError("This run already contains the scenario; preserve its original evidence and choose a new run")
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous != manifest:
            raise ValueError("Run manifest identity differs; preserve the original and choose a new run")
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run / "results" / (case["scenario"] + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status, reason, _ = validate_result(value, manifest, case)
    return {"status": status, "reason": reason, "manifest": str(run / "manifest.json"), "scenario": case["scenario"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--mode", choices=("eager", "graph"), default="eager")
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    def git(*items):
        return _git(repo, *items)
    if git("rev-parse", "HEAD") != args.commit or git("status", "--porcelain"):
        parser.error("The source repository must be clean and HEAD must equal --commit")
    model = Path(args.model_path).name
    cases, warnings = discover_cases(lambda sha, path: git("show", sha + ":" + path),
                                    lambda sha, prefix: git("ls-tree", "-r", "--name-only", sha, "--", prefix).splitlines(),
                                    lambda sha, path: git("rev-parse", sha + ":" + path), args.commit, [model], DEFAULT_SOURCES)
    selected = [case for case in cases if args.mode in case["modes"] and (args.scenario == "all" or case["scenario"] == args.scenario)]
    if not selected:
        parser.error("Scenario not found in the supplied commit: " + json.dumps(warnings, ensure_ascii=False))
    run_id = args.run_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id) or ".." in run_id:
        parser.error("Invalid run-id")
    source_cache = {}
    results = [collect(repo, args.commit, args.branch, args.backend, case, args.model_path, args.output_dir,
                       run_id, source_cache) for case in selected]
    print(json.dumps({"run_id": run_id, "results": results, "warnings": warnings}, ensure_ascii=False))
    return 0 if not warnings and all(result["status"] == "passed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
