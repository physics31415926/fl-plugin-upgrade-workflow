"""Create private, clean monitor configuration without starting services or agents."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from fl_dashboard.agents.codex_runtime import resolve_codex_executable

from fl_dashboard.paths import APP_ROOT
PROJECTS = {
    "vllm": ("vLLM-Plugin-FL", "vllm-plugin-FL", "examples/branch-monitor.example.json", "branch-monitor.json"),
    "sglang": ("SGLang-Plugin-FL", "sglang-plugin-FL", "examples/branch-monitor.sglang.example.json", "branch-monitor.sglang.json"),
}
# Positive projection prevents demonstration PRs, historical acceptance and owner
# connections from becoming a new user's live configuration.
DEFAULT_FIELDS = ("models", "modes", "backend_labels", "backend_ci_platforms", "case_dir", "case_sources")


class ConfigurationError(ValueError):
    pass


def git(repo: Path, *arguments: str, allowed=(0,)) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo), *arguments],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except FileNotFoundError as exc:
        raise ConfigurationError("Git is required; install it and make it available on PATH.") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigurationError("Unable to read local Git metadata.") from exc
    if result.returncode not in allowed:
        # Git errors may contain credential-bearing remote URLs. Never repeat them.
        raise ConfigurationError(f"Git metadata check failed ({arguments[0]}).")
    return result


def enclosing_checkout(directory: Path) -> Path | None:
    # Discover the worktree root before invoking Git so safe.directory names the
    # actual checkout, including on Windows with a different checkout owner.
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def checkout(value: str | Path) -> Path:
    directory = Path(value).expanduser().resolve()
    if not directory.is_dir():
        raise ConfigurationError("Repository path must be an existing Git checkout directory.")
    repo = enclosing_checkout(directory)
    if repo is None:
        raise ConfigurationError("Repository path must be a Git working tree, not a bare repository.")
    inside = git(repo, "rev-parse", "--is-inside-work-tree", allowed=(0, 128))
    if inside.returncode or inside.stdout.strip() != "true":
        raise ConfigurationError("Repository path must be a Git working tree, not a bare repository.")
    return Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()


def remote_names(repo: Path) -> list[str]:
    return git(repo, "remote").stdout.splitlines()


def default_remote(names: list[str]) -> str | None:
    return next((name for name in ("upstream", "origin") if name in names), names[0] if len(names) == 1 else None)


def validate_remote(value: str, names: list[str]) -> str:
    if value not in names:
        raise ConfigurationError("Selected remote does not exist; choose one listed by 'git remote'.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value):
        raise ConfigurationError("Remote name must use letters, digits, '.', '_' or '-' and at most 100 characters for monitor and agent compatibility.")
    return value


def remote_branches(repo: Path, remote: str) -> list[str]:
    prefix = f"refs/remotes/{remote}/"
    refs = git(repo, "for-each-ref", "--format=%(refname)", prefix).stdout.splitlines()
    return sorted(ref[len(prefix):] for ref in refs if ref.startswith(prefix) and ref != prefix + "HEAD")


def default_branch(repo: Path, remote: str, branches: list[str]) -> str | None:
    current = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1)).stdout.strip()
    if current in branches:
        return current
    prefix = f"refs/remotes/{remote}/"
    head = git(repo, "symbolic-ref", "--quiet", prefix + "HEAD", allowed=(0, 1)).stdout.strip()
    if head.startswith(prefix) and head[len(prefix):] in branches:
        return head[len(prefix):]
    return branches[0] if len(branches) == 1 else None


def validate_branch(repo: Path, remote: str, value: str) -> str:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) or ".." in value
            or "//" in value or value.endswith("/")):
        raise ConfigurationError("Branch name cannot be used by the monitor.")
    ref = f"refs/remotes/{remote}/{value}"
    if git(repo, "check-ref-format", ref, allowed=(0, 1)).returncode:
        raise ConfigurationError("Target branch is not a valid Git branch name.")
    if git(repo, "show-ref", "--verify", "--quiet", ref, allowed=(0, 1)).returncode:
        raise ConfigurationError(
            f"Target remote branch '{remote}/{value}' is absent from local refs. "
            "Fetch this remote yourself, then rerun configure.py; the wizard never fetches."
        )
    if git(repo, "rev-parse", "--verify", ref + "^{commit}", allowed=(0, 128)).returncode:
        raise ConfigurationError("Target remote branch must point to a commit.")
    return value


def github_slug(url: str) -> str:
    """Extract only owner/repository; neither credentials nor remote URL survive."""
    try:
        if "://" in url:
            parsed = urlsplit(url)
            if parsed.scheme not in ("https", "http", "ssh", "git") or parsed.hostname != "github.com":
                return ""
            path = parsed.path
        else:
            match = re.fullmatch(r"(?:[^@/:]+@)?github\.com:(.+)", url)
            if not match:
                return ""
            path = match.group(1)
        path = path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return path if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) else ""
    except ValueError:
        return ""


def validate_slug(value: str) -> str:
    if value and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ConfigurationError("GitHub repository must be owner/repository, or an empty string.")
    return value


def cli_path(value: str | None) -> str | None:
    candidate = resolve_codex_executable(value)
    if value and not candidate:
        raise ConfigurationError("--codex-executable must identify an existing CLI file (native .exe on Windows).")
    return candidate


def ask(label: str, default: str | None = None) -> str:
    answer = input(f"{label}" + (f" [{default}]" if default is not None else "") + ": ").strip()
    answer = answer or default or ""
    if len(answer) >= 2 and answer[0] == answer[-1] == '"':
        answer = answer[1:-1]
    return answer


def project_ids(value: list[str]) -> list[str]:
    if not value or any(item not in PROJECTS for item in value) or len(set(value)) != len(value):
        raise ConfigurationError("Choose vllm, sglang, or both once each.")
    return value


def build_project(project_id: str, args, *, interactive: bool, app_root: Path, executable: str | None) -> dict:
    label, dirname, template_name, _ = PROJECTS[project_id]
    value = getattr(args, project_id + "_repo")
    if value is None and interactive:
        suggested = app_root.parent / dirname
        value = ask(f"{label} Git checkout", str(suggested) if suggested.is_dir() else None)
    if not value:
        raise ConfigurationError(f"Provide --{project_id}-repo with the plugin's Git checkout path.")
    repo = checkout(value)
    names = remote_names(repo)
    if not names:
        raise ConfigurationError(f"{label} has no configured Git remote.")
    remote = getattr(args, project_id + "_remote")
    if remote is None:
        suggested = default_remote(names)
        remote = ask(f"{label} remote ({', '.join(names)})", suggested) if interactive else suggested
    if not remote:
        raise ConfigurationError(f"Choose --{project_id}-remote; no unambiguous default remote exists.")
    remote = validate_remote(remote, names)
    branches = remote_branches(repo, remote)
    if not branches:
        raise ConfigurationError(f"No local remote branches exist for '{remote}'. Fetch this remote yourself, then rerun configure.py.")
    branch = getattr(args, project_id + "_branch")
    if branch is None:
        suggested = default_branch(repo, remote, branches)
        branch = ask(f"{label} target remote branch", suggested) if interactive else suggested
    if not branch:
        raise ConfigurationError(f"Provide --{project_id}-branch; no unambiguous local remote-ref exists.")
    branch = validate_branch(repo, remote, branch)
    slug = getattr(args, project_id + "_github_repo")
    if slug is None:
        # Read but never output or persist the original URL.
        slug = github_slug(git(repo, "remote", "get-url", remote).stdout.strip())
    slug = validate_slug(slug)
    try:
        template = json.loads((app_root / template_name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Cannot read the bundled {project_id} example defaults.") from exc
    if not isinstance(template, dict):
        raise ConfigurationError("Example monitor defaults must be a JSON object.")
    config = {key: copy.deepcopy(template[key]) for key in DEFAULT_FIELDS if key in template}
    # Pin the engine's own defaults explicitly so every provider uses the same
    # source directories, even if an older provider has a vLLM-only default.
    config.setdefault("case_dir", "examples" if project_id == "sglang" else "tools/adaptation-gate-cases")
    config.setdefault("case_sources", ["examples", "tests/e2e_tests/concurrent"]
                      if project_id == "sglang" else [config["case_dir"]])
    for field in ("models", "modes"):
        override = getattr(args, f"{project_id}_{field}")
        if override is not None:
            config[field] = override
        if not isinstance(config.get(field), list) or not config[field] or any(
                not isinstance(item, str) or not item.strip() for item in config[field]):
            raise ConfigurationError(f"{project_id} {field} must contain nonempty names.")
    config.update(engine=project_id, project_label=label, repo=str(repo), remote=remote,
                  branch=branch, github_repo=slug,
                  sessions_dir=f"runs/{project_id}/codex-sessions",
                  claude_sessions_dir=f"runs/{project_id}/claude-sessions",
                  evidence_dir=f"runs/{project_id}/gate-evidence")
    if executable:
        config["codex_executable"] = executable
    return config


def private_targets(paths: list[Path]) -> None:
    """Reject files that would expose machine paths in any enclosing Git worktree."""
    parent = paths[0].parent
    while not parent.exists():
        parent = parent.parent
    enclosing = enclosing_checkout(parent)
    if enclosing is None:
        return  # A local directory outside a Git worktree is also private configuration.
    # An enclosing checkout exists: every Git failure is an error, never an
    # excuse to skip the ignore or tracked-file protections.
    root = Path(git(enclosing, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    for path in paths:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ConfigurationError("Configuration output cannot cross its enclosing Git checkout.") from exc
        if not git(root, "ls-files", "--error-unmatch", "--", relative, allowed=(0, 1)).returncode:
            raise ConfigurationError("Refusing to write tracked configuration files; use an ignored runtime directory.")
        if git(root, "check-ignore", "--quiet", "--no-index", "--", relative, allowed=(0, 1)).returncode:
            raise ConfigurationError(
                "Configuration output is not Git-ignored. Use --output-dir runs/configured "
                "inside the app checkout, or a private directory outside Git."
            )


def write_configs(output: Path, documents: dict[str, dict], *, overwrite: bool) -> list[Path]:
    paths = [output / name for name in documents]
    private_targets(paths)
    for path in paths:
        if path.exists() or path.is_symlink():
            if not overwrite:
                raise ConfigurationError(
                    f"Configuration already exists: {path.name}. Edit existing files, choose another "
                    "--output-dir, or explicitly use --overwrite. No configuration was changed."
                )
            if path.is_symlink() or not path.is_file():
                raise ConfigurationError("Existing configuration targets must be ordinary files.")
    output.mkdir(parents=True, exist_ok=True)
    staged = []
    created = []
    try:
        for path in paths:
            fd, name = tempfile.mkstemp(prefix=".configure-", suffix=".tmp", dir=output)
            staged.append(Path(name))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(documents[path.name], stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        for path, temporary in zip(paths, staged):
            if overwrite:
                os.replace(temporary, path)
            else:
                # Exclusive link creation cannot silently replace a file created
                # between validation and publication, even by another wizard.
                os.link(temporary, path)
                created.append(path)
    except OSError as exc:
        if not overwrite:
            for path in created:
                path.unlink(missing_ok=True)
        raise ConfigurationError("Unable to publish configuration files; check directory access and existing files.") from exc
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
    return paths


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--non-interactive", action="store_true", help="Never prompt; require any ambiguous values explicitly.")
    result.add_argument("--projects", nargs="+", choices=tuple(PROJECTS), help="Selected projects (default: both).")
    result.add_argument("--output-dir", type=Path, help="Private output directory (default: runs/configured under this app).")
    result.add_argument("--port", type=int, help="Loopback dashboard port (default: 8766).")
    result.add_argument("--overwrite", action="store_true", help="Explicitly replace existing generated files.")
    result.add_argument("--codex-executable", help="Optional local CLI path; otherwise detect on PATH without running it.")
    for project_id in PROJECTS:
        result.add_argument(f"--{project_id}-repo", help="Plugin Git checkout path.")
        result.add_argument(f"--{project_id}-remote", help="Remote name (default: upstream, then origin, then the sole remote).")
        result.add_argument(f"--{project_id}-branch", help="Target branch available in local refs/remotes/<remote>/.")
        result.add_argument(f"--{project_id}-github-repo", help="Override owner/repository; use an empty string to disable GitHub status.")
        result.add_argument(f"--{project_id}-models", nargs="+", help="Override model names from example defaults.")
        result.add_argument(f"--{project_id}-modes", nargs="+", choices=("eager", "graph"), help="Override inference modes.")
    return result


def configure(args, *, app_root: Path = APP_ROOT) -> tuple[list[Path], int]:
    interactive = not args.non_interactive
    selected = args.projects
    if selected is None:
        selected = ask("Projects (vllm sglang)", "vllm sglang").replace(",", " ").split() if interactive else list(PROJECTS)
    selected = project_ids(selected)
    output = args.output_dir
    if output is None:
        default = app_root / "runs" / "configured"
        output = Path(ask("Private output directory", str(default))) if interactive else default
    output = output.expanduser().resolve()
    port = args.port
    if port is None:
        try:
            port = int(ask("Loopback dashboard port", "8766")) if interactive else 8766
        except ValueError as exc:
            raise ConfigurationError("Port must be an integer from 1 to 65535.") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("Port must be an integer from 1 to 65535.")
    # Desktop updates replace versioned CLI directories. Only an explicit
    # override is persisted; automatic discovery stays dynamic at runtime.
    executable = cli_path(args.codex_executable) if args.codex_executable else None
    documents = {}
    rows = []
    for project_id in selected:
        label, _, _, name = PROJECTS[project_id]
        documents[name] = build_project(project_id, args, interactive=interactive, app_root=app_root, executable=executable)
        rows.append({"id": project_id, "label": label, "monitor_config": name})
    documents["projects.json"] = {"projects": rows}
    documents["launch.json"] = {
        "python": str(Path(sys.executable).resolve()),
        "arguments": ["-u", "-B", str(app_root / "scripts" / "flflow.py"), "serve", "--projects-config",
                      str(output / "projects.json"), "--host", "127.0.0.1", "--port", str(port)],
        "working_directory": str(app_root),
        "log_file": str(output / "runs" / "local" / "dashboard.log"),
    }
    return write_configs(output, documents, overwrite=args.overwrite), port


def display_command(arguments: list[str]) -> str:
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        paths, port = configure(args)
    except (ConfigurationError, OSError, EOFError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Configuration cancelled; no service or agent was started.", file=sys.stderr)
        return 130
    print("Created private configuration:")
    for path in paths:
        print(f"  {path}")
    print("No Git fetch, service, GPU command, or agent task was started.")
    output = paths[0].parent
    default_output = APP_ROOT / "runs" / "configured"
    launch = output / "launch.json"
    command = ["python", "-B", "scripts/start_dashboard.py"]
    if output != default_output:
        command.extend(["--config", str(launch)])
    print("Start from the app directory:")
    print("  " + display_command(command))
    if os.name == "nt":
        print("Windows hidden launcher (optional):")
        quoted = str(launch).replace("'", "''")
        print(f"  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\start_local_dashboard.ps1 -ConfigPath '{quoted}'")
    print(f"Open http://127.0.0.1:{port}/")
    print("Rerun python -B scripts/configure.py to choose a different configuration directory; use --overwrite only to replace its files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
