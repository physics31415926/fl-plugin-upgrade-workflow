"""The dashboard runs in the background; no child it starts may open a window.

On Windows every console program (git, gh, ssh, taskkill) started without
CREATE_NO_WINDOW by a process that has no console gets a new visible console
window. The dashboard polls git and gh every few seconds, so one missed flag
means a stream of terminal windows on the user's desktop.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPAWNERS = {"run", "Popen", "check_output", "call", "check_call"}
# Foreground launcher run from the user's own terminal: its child should
# share that terminal, so it is the one deliberate exception.
EXEMPT = {"fl_dashboard/deployment/start_dashboard.py"}


def spawn_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        func = getattr(node, "func", None)
        if (isinstance(node, ast.Call) and isinstance(func, ast.Attribute) and func.attr in SPAWNERS
                and isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
            yield node


class NoConsoleWindowTests(unittest.TestCase):
    def test_every_dashboard_child_process_hides_its_window(self):
        missing = []
        for path in sorted((ROOT / "fl_dashboard").rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative in EXEMPT:
                continue
            source = path.read_text(encoding="utf-8")
            for call in spawn_calls(path):
                splat = any(k.arg is None for k in call.keywords)
                if splat and 'kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW' in source:
                    continue
                flags = next((k.value for k in call.keywords if k.arg == "creationflags"), None)
                text = ast.unparse(flags) if flags is not None else ""
                if isinstance(flags, ast.Name):
                    # `flags = subprocess.CREATE_NO_WINDOW if ...` then `creationflags=flags`
                    text = " ".join(ast.unparse(a.value) for a in ast.walk(ast.parse(source))
                                    if isinstance(a, ast.Assign)
                                    and any(isinstance(t, ast.Name) and t.id == flags.id for t in a.targets))
                if "CREATE_NO_WINDOW" not in text:
                    missing.append(f"{relative}:{call.lineno}")
        self.assertEqual(missing, [], "subprocess calls without CREATE_NO_WINDOW")

    def test_restart_never_detaches_from_the_hidden_console(self):
        source = (ROOT / "fl_dashboard" / "services" / "code_version.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("DETACHED_PROCESS", code)


if __name__ == "__main__":
    unittest.main()
