#!/usr/bin/env python3
"""Release the Harness plugin in one command.

Steps, in order:

1. set the version in ``integrations/dsh-plugin/package.json`` and in the
   install examples of both READMEs;
2. build the reproducible tarball with ``pack_dsh_plugin.build``;
3. with ``--publish``: commit, tag ``dsh-plugin-v<version>``, push, and
   create the GitHub Release with the tarball attached (needs ``gh``);
4. with ``--sync``: install the tarball into every local place a Harness
   profile loads the plugin from:
   - ``pnpm add <tarball>`` in ``~/.dsh/fl-plugin-upgrade-workflow`` when it
     depends on the package;
   - copy the plugin files into each ``link:`` directory that a profile's
     ``package.json`` points at for this plugin (the step that was easy to
     forget by hand).

Without ``--publish`` nothing leaves the machine. Run tests first; this
script does not run them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "integrations" / "dsh-plugin"
READMES = [ROOT / "README.md", SOURCE / "README.md"]
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
PACKAGE_FILES = ["index.js", "client.js", "cordis.patch.yml", "icon.svg", "README.md", "package.json"]

sys.path.insert(0, str(ROOT / "scripts"))
from pack_dsh_plugin import build  # noqa: E402


def run(argv: list[str], cwd: Path = ROOT, dry: bool = False) -> str:
    print("$", " ".join(argv))
    if dry:
        return ""
    done = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, encoding="utf-8", errors="replace")
    if done.returncode:
        raise SystemExit(f"command failed ({done.returncode}): {' '.join(argv)}\n{done.stderr or done.stdout}")
    return done.stdout


def write_text(path: Path, text: str) -> None:
    # Keep the file's own line endings and no BOM.
    path.write_bytes(text.encode("utf-8"))


def set_version(version: str, name: str) -> None:
    package = SOURCE / "package.json"
    text = package.read_text(encoding="utf-8")
    text, count = re.subn(r'("version":\s*")[^"]+(")', rf"\g<1>{version}\g<2>", text, count=1)
    if count != 1:
        raise SystemExit("package.json has no version field")
    write_text(package, text)
    slug = name.lstrip("@").replace("/", "-")
    pattern = re.compile(re.escape(slug) + r"-\d+\.\d+\.\d+\.tgz")
    for readme in READMES:
        content = readme.read_text(encoding="utf-8")
        updated = pattern.sub(f"{slug}-{version}.tgz", content)
        if updated != content:
            write_text(readme, updated)


def profile_link_dirs(name: str) -> list[Path]:
    """Directories that a Harness profile links this plugin from."""
    profiles = Path.home() / ".dsh" / "profiles"
    found = []
    for manifest in profiles.glob("*/package.json"):
        try:
            deps = json.loads(manifest.read_text(encoding="utf-8")).get("dependencies") or {}
        except (OSError, ValueError):
            continue
        for spec in deps.values():
            if not isinstance(spec, str) or not spec.startswith("link:"):
                continue
            target = Path(spec[len("link:"):])
            try:
                linked = json.loads((target / "package.json").read_text(encoding="utf-8")).get("name")
            except (OSError, ValueError):
                continue
            # The dependency key may be an old alias; the linked package's own
            # name is what identifies it.
            if linked == name and target.resolve() != SOURCE.resolve():
                found.append(target)
    return found


def sync(tarball: Path, name: str, dry: bool) -> None:
    store = Path.home() / ".dsh" / "fl-plugin-upgrade-workflow"
    manifest = store / "package.json"
    if manifest.exists() and name in (json.loads(manifest.read_text(encoding="utf-8")).get("dependencies") or {}):
        pnpm = shutil.which("pnpm")
        runtime = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies"
        if pnpm:
            run([pnpm, "add", str(tarball)], cwd=store, dry=dry)
        elif (runtime / "node" / "bin" / "node.exe").exists():
            run([str(runtime / "node" / "bin" / "node.exe"), str(runtime / "pnpm" / "bin" / "pnpm.mjs"), "add", str(tarball)],
                cwd=store, dry=dry)
        else:
            print(f"skip {store}: no pnpm found")
    for target in profile_link_dirs(name):
        print(f"copy plugin files -> {target}")
        if dry:
            continue
        for item in PACKAGE_FILES:
            if (SOURCE / item).exists():
                shutil.copy2(SOURCE / item, target / item)
        if (SOURCE / "locale").is_dir():
            shutil.copytree(SOURCE / "locale", target / "locale", dirs_exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="new version, e.g. 0.7.1")
    parser.add_argument("--publish", action="store_true", help="commit, tag, push and create the GitHub Release")
    parser.add_argument("--sync", action="store_true", help="install into the local Harness profile locations")
    parser.add_argument("--dry-run", action="store_true", help="print git/gh/pnpm commands instead of running them")
    args = parser.parse_args(argv)
    if not VERSION.match(args.version):
        raise SystemExit("version must look like 1.2.3")
    name = json.loads((SOURCE / "package.json").read_text(encoding="utf-8"))["name"]
    tag = f"dsh-plugin-v{args.version}"
    if args.publish and not args.dry_run:
        if run(["git", "tag", "--list", tag]).strip():
            raise SystemExit(f"tag {tag} already exists")
    set_version(args.version, name)
    tarball = build(SOURCE, ROOT / "dist")
    if args.publish:
        run(["git", "add", "-A", "integrations/dsh-plugin", *[str(p.relative_to(ROOT)) for p in READMES]], dry=args.dry_run)
        status = run(["git", "status", "--porcelain", "--", "integrations/dsh-plugin", "README.md"], dry=args.dry_run)
        if status.strip() or args.dry_run:
            run(["git", "commit", "-m", f"chore: release dsh-plugin v{args.version}"], dry=args.dry_run)
        run(["git", "tag", tag], dry=args.dry_run)
        run(["git", "push", "origin", "HEAD", tag], dry=args.dry_run)
        run(["gh", "release", "create", tag, str(tarball), "--title", f"dsh-plugin v{args.version}",
             "--generate-notes"], dry=args.dry_run)
    if args.sync:
        sync(tarball, name, args.dry_run)
    print(f"done: {tarball.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
