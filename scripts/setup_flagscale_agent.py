"""Set up and refresh the FlagScale-Agent skills for the DeepSeek Harness preset.

The FlagScale Agent preset reads its workflow skills from one skills root. Each
skill there is a link into a git checkout of FlagScale-Agent, plus one generated
`flagscale-knowledge` skill that indexes the checkout's knowledge corpus. This
script does every step in one command:

1. clone FlagScale-Agent if the checkout is missing, otherwise fast-forward it;
2. link each `flagscale_agent/skills/<name>/SKILL.md` into the skills root
   (adding new skills, pruning removed ones);
3. regenerate the knowledge-index skill from `knowledge_config.yaml`.

Run it again at any time to update. `--install-schedule` registers a daily run
so the clone keeps pulling on its own.

Usage:
    python scripts/setup_flagscale_agent.py
    python scripts/setup_flagscale_agent.py --checkout /path/to/FlagScale-Agent
    python scripts/setup_flagscale_agent.py --branch main
    python scripts/setup_flagscale_agent.py --install-schedule

Defaults live under the Harness home ($DSH_HOME, else ~/.dsh):
    checkout     <home>/FlagScale-Agent
    skills root  <home>/flagscale-preset/skills

The preset's `customSkillDirs` must point at the skills root.

A checkout with uncommitted changes is never pulled. Pulls are fast-forward
only: a branch that cannot fast-forward needs a merge, and this script does not
make that decision. A real directory in the skills root (not a link) is never
deleted.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_URL = "https://github.com/flagos-ai/FlagScale-Agent.git"
KNOWLEDGE_SKILL = "flagscale-knowledge"
TASK_NAME = "FlagScaleAgentSkillsUpdate"


def harness_home() -> Path:
    value = os.environ.get("DSH_HOME", "").strip()
    return Path(value) if value else Path.home() / ".dsh"


def log(message: str) -> None:
    print(message, flush=True)


def git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True)


# --- 1. clone or pull -------------------------------------------------------

def clone(checkout: Path, branch: str | None) -> bool:
    if checkout.exists() and any(checkout.iterdir()):
        log(f"error: {checkout} exists but is not a git checkout; move it or pass --checkout")
        return False
    checkout.parent.mkdir(parents=True, exist_ok=True)
    argv = ["git", "clone"]
    if branch:
        argv += ["--branch", branch]
    argv += [REPO_URL, str(checkout)]
    log(f"cloning {REPO_URL} -> {checkout}")
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"error: git clone failed: {result.stderr.strip()}")
        return False
    return True


def pull(checkout: Path, branch: str | None) -> bool:
    before = git(checkout, "rev-parse", "HEAD").stdout.strip()
    current = git(checkout, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    log(f"checkout {checkout.name} on {current} at {before[:10]}")

    status = git(checkout, "status", "--porcelain")
    if status.returncode != 0 or status.stdout.strip():
        # Pulling over local edits could merge into someone's work in progress.
        log("error: the checkout has uncommitted changes; commit or stash them first")
        return False

    if branch and branch != current:
        fetched = git(checkout, "fetch", "origin", branch)
        if fetched.returncode != 0:
            log(f"error: git fetch {branch} failed: {fetched.stderr.strip()}")
            return False
        switched = git(checkout, "checkout", branch)
        if switched.returncode != 0:
            log(f"error: cannot check out {branch}: {switched.stderr.strip()}")
            return False

    pulled = git(checkout, "pull", "--ff-only")
    if pulled.returncode != 0:
        log(f"error: git pull --ff-only failed: {pulled.stderr.strip()}")
        return False
    after = git(checkout, "rev-parse", "HEAD").stdout.strip()
    log("already up to date" if before == after else f"updated {before[:10]} -> {after[:10]}")
    return True


# --- 2. link skills ---------------------------------------------------------

def link_target(path: Path) -> Path | None:
    """Where a link points, or None when `path` is a real directory."""
    try:
        if path.is_symlink():
            return Path(os.readlink(path)).resolve()
        resolved = path.resolve()
        return resolved if resolved != path.absolute() else None
    except OSError:
        return None


def remove_link(path: Path) -> None:
    # A Windows junction and a POSIX symlink both remove as an empty directory
    # entry; neither touches the target's contents.
    if path.is_symlink():
        path.unlink()
    else:
        os.rmdir(path)


def make_link(link: Path, source: Path) -> bool:
    if os.name == "nt":
        # A junction needs no elevation, unlike a directory symlink.
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(source)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log(f"error: mklink failed for {source.name}: {result.stderr.strip()}")
            return False
        return True
    os.symlink(source, link, target_is_directory=True)
    return True


def link_skills(checkout: Path, root: Path) -> bool:
    upstream = checkout / "flagscale_agent" / "skills"
    if not upstream.is_dir():
        log(f"error: no skills directory in the checkout: {upstream}")
        return False
    root.mkdir(parents=True, exist_ok=True)
    sources = sorted(d for d in upstream.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())
    if not sources:
        log("error: no skills with a SKILL.md in the checkout")
        return False

    created = reused = replaced = pruned = 0
    for source in sources:
        link = root / source.name
        if link.exists() or link.is_symlink():
            target = link_target(link)
            if target == source.resolve():
                reused += 1
                continue
            if target is None:
                log(f"error: refusing to replace a real directory: {link}")
                return False
            remove_link(link)
            replaced += 1
        if not make_link(link, source):
            return False
        created += 1

    keep = {s.name for s in sources} | {KNOWLEDGE_SKILL}
    for entry in root.iterdir():
        if entry.name not in keep and link_target(entry) is not None:
            remove_link(entry)
            pruned += 1

    log(f"skills: linked={created} reused={reused} replaced={replaced} pruned={pruned}")
    return True


# --- 3. knowledge index -----------------------------------------------------

def parse_groups(text: str) -> list[tuple[str, str, list[str]]]:
    """Read `know-*` groups, their description and doc list, without a YAML dependency."""
    groups: list[tuple[str, str, list[str]]] = []
    name: str | None = None
    description = ""
    docs: list[str] = []
    in_docs = False
    for raw in text.splitlines():
        header = re.match(r"^(know-[a-z0-9-]+):\s*$", raw)
        if header:
            if name:
                groups.append((name, description, docs))
            name, description, docs, in_docs = header.group(1), "", [], False
            continue
        if name is None:
            continue
        desc = re.match(r'^\s{2}description:\s*"?(.*?)"?\s*$', raw)
        if desc:
            description, in_docs = desc.group(1), False
            continue
        if re.match(r"^\s{2}docs:\s*$", raw):
            in_docs = True
            continue
        item = re.match(r"^\s{4}-\s*(.+?)\s*$", raw)
        if in_docs and item:
            docs.append(item.group(1))
            continue
        if raw and not raw.startswith((" ", "#")):
            groups.append((name, description, docs))
            name, description, docs, in_docs = None, "", [], False
    if name:
        groups.append((name, description, docs))
    return groups


def write_knowledge_skill(checkout: Path, root: Path) -> bool:
    knowledge = checkout / "flagscale_agent" / "knowledge"
    config = knowledge / "knowledge_config.yaml"
    if not config.is_file():
        log(f"error: missing {config}")
        return False
    groups = parse_groups(config.read_text(encoding="utf-8"))
    if not groups:
        log("error: no knowledge groups parsed")
        return False

    total = sum(len(d) for _, _, d in groups)
    docs_root = (knowledge / "docs").as_posix()
    index_root = (knowledge / "indexes").as_posix()
    rows = "\n".join(f"| `{n}` | {len(d)} | {desc} |" for n, desc, d in groups)
    body = f"""---
name: {KNOWLEDGE_SKILL}
description: >-
  Index of the FlagScale-Agent knowledge corpus: {len(groups)} groups of deep
  technical documentation on Megatron-LM-FL, TransformerEngine-FL, NCCL,
  FlashAttention, PyTorch distributed, CUDA kernels, profiling, MoE training,
  linear attention, Energon data loading and cluster infrastructure. Load this
  to find which document answers an infrastructure question before searching
  the tree or guessing.
whenToUse: >-
  Before acting on any FlagScale, Megatron, TransformerEngine, NCCL, CUDA,
  profiling, MoE or distributed-training question, and whenever a FlagScale
  workflow skill tells you to consult knowledge.
---

# FlagScale knowledge corpus

{len(groups)} groups, {total} documents. These are plain Markdown files on disk:
read them with the ordinary file tools. There is no `load_knowledge` tool in
this environment; that is FlagScale-Agent's own CLI tool.

## How to use this

1. Pick a group from the table by subject.
2. Read its index first. It lists every section with line numbers, so you can
   open just the part you need: `{index_root}/<group>.idx`
3. Read the document under `{docs_root}/<relative path from the index>`

## Groups

| Group | Docs | Subject |
|---|---|---|
{rows}
"""
    target = root / KNOWLEDGE_SKILL
    target.mkdir(parents=True, exist_ok=True)
    # Explicit UTF-8 and LF, so the frontmatter parser sees exactly this text.
    with io.open(target / "SKILL.md", "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    log(f"knowledge index: groups={len(groups)} docs={total}")
    return True


# --- periodic update --------------------------------------------------------

def install_schedule(checkout: Path, root: Path, hour: int) -> bool:
    """Register a daily run of this script, so the clone keeps pulling."""
    script = Path(__file__).resolve()
    python = Path(sys.executable).resolve()
    args = f'"{script}" --checkout "{checkout}" --skills-root "{root}" --log'
    if os.name == "nt":
        # schtasks caps /TR at 261 characters, which a full python path plus
        # two directory arguments easily exceeds. A launcher file keeps the
        # task command short and the real arguments readable.
        launcher = root.parent / "update-skills.cmd"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(f'@echo off\r\n"{python}" {args}\r\n', encoding="utf-8")
        result = subprocess.run(
            ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", f"{hour:02d}:00",
             "/TN", TASK_NAME, "/TR", f'"{launcher}"'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log(f"error: schtasks failed: {(result.stderr or result.stdout).strip()}")
            return False
        log(f"scheduled: Windows task '{TASK_NAME}' runs daily at {hour:02d}:00")
        log(f"remove it with: schtasks /Delete /TN {TASK_NAME} /F")
        return True
    # No portable scheduler to call; print the crontab line instead of editing it.
    log("add this line with `crontab -e` to update daily:")
    log(f"0 {hour} * * * {python} {args}")
    return True


def main() -> int:
    home = harness_home()
    parser = argparse.ArgumentParser(description="Set up and refresh the FlagScale-Agent preset skills.")
    parser.add_argument("--checkout", type=Path, default=home / "FlagScale-Agent",
                        help="FlagScale-Agent checkout (cloned here if missing)")
    parser.add_argument("--skills-root", type=Path, default=home / "flagscale-preset" / "skills",
                        help="the preset's customSkillDirs entry")
    parser.add_argument("--branch", help="branch to clone or switch to (default: keep the current one)")
    parser.add_argument("--no-pull", action="store_true", help="re-link and re-index without touching git")
    parser.add_argument("--install-schedule", action="store_true", help="register a daily update run")
    parser.add_argument("--hour", type=int, default=9, help="hour of the daily run (default 9)")
    parser.add_argument("--log", action="store_true", help="append output to <skills-root>/../update.log")
    args = parser.parse_args()

    checkout = args.checkout.expanduser().resolve()
    root = args.skills_root.expanduser().resolve()

    if args.log:
        log_file = root.parent / "update.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = io.open(log_file, "a", encoding="utf-8")
        log(f"\n=== {datetime.now().isoformat(timespec='seconds')} ===")

    if not shutil.which("git"):
        log("error: git was not found on PATH")
        return 1

    if not args.no_pull:
        if (checkout / ".git").exists():
            if not pull(checkout, args.branch):
                return 1
        elif not clone(checkout, args.branch):
            return 1

    if not link_skills(checkout, root):
        return 1
    if not write_knowledge_skill(checkout, root):
        return 1
    if args.install_schedule and not install_schedule(checkout, root, args.hour):
        return 1

    log(f"done. skills root: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
