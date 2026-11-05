"""Pack the DeepSeek Harness plugin into an installable tarball.

The plugin is a Harness bundle, and Harness installs a bundle from an npm-style
tarball: every entry under a single `package/` prefix. `npm pack` would produce
one, but requiring Node to ship a file this repository already contains is a
dependency nobody needs, so this builds the same layout with the standard
library.

The output is reproducible: entries are sorted, timestamps and ownership are
fixed, and only the files `package.json` declares are included. Two runs on the
same source therefore produce byte-identical archives, which is what makes a
published checksum worth anything.

    python -B scripts/pack_dsh_plugin.py
    python -B scripts/pack_dsh_plugin.py --output-dir dist
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

# Every path in the archive sits under this prefix, which is what the Harness
# installer and npm both expect.
ARCHIVE_ROOT = "package"

# A fixed timestamp keeps the archive reproducible. Using zero would be
# tempting, but some extractors reject a 1970 mtime outright.
FIXED_MTIME = 1_577_836_800  # 2020-01-01T00:00:00Z

SOURCE_DIR = Path(__file__).resolve().parents[1] / "integrations" / "dsh-plugin"


def expand(patterns: list[str], root: Path) -> list[Path]:
    """Resolve the `files` globs in package.json to actual paths.

    A declared file that does not exist is an error rather than a warning: the
    plugin would install and then fail to activate, which is a far worse place
    to discover it.
    """
    found: list[Path] = []
    for pattern in patterns:
        matches = sorted(root.glob(pattern)) if "*" in pattern else [root / pattern]
        real = [m for m in matches if m.is_file()]
        if not real:
            raise SystemExit(f"error: package.json declares {pattern!r}, which matches no file")
        found.extend(real)
    # package.json is always shipped; npm includes it whether or not it is
    # listed, and the bundle is unusable without it.
    manifest = root / "package.json"
    if manifest not in found:
        found.append(manifest)
    return sorted(set(found))


def normalize(member: Path) -> bytes:
    """Read one file with line endings normalized to LF.

    Every shipped file is text, and a Windows checkout of this repository has
    CRLF where a Linux one has LF. Packing the bytes as they sit on disk would
    make the archive hash depend on who built it, which would defeat publishing
    a checksum at all. The runtime does not care either way.
    """
    return member.read_bytes().replace(b"\r\n", b"\n")


def build(source: Path, destination: Path, report: bool = True) -> Path:
    """Write the tarball and return its path.

    `report` is off for callers that only want the artifact, so the test suite
    does not interleave packer output with its own.
    """
    manifest_path = source / "package.json"
    if not manifest_path.is_file():
        raise SystemExit(f"error: no package.json in {source}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    name = str(manifest.get("name") or "")
    version = str(manifest.get("version") or "")
    if not name or not version:
        raise SystemExit("error: package.json needs both a name and a version")

    patch = (manifest.get("dsh") or {}).get("bundle", {}).get("patch")
    if not patch:
        raise SystemExit("error: package.json declares no dsh.bundle.patch, so Harness would not treat this as a bundle")

    members = expand(list(manifest.get("files") or []), source)

    # A scope becomes a path segment in the filename, exactly as npm does it.
    stem = name.replace("@", "").replace("/", "-")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{stem}-{version}.tgz"

    # Build the tar in memory, then gzip it with mtime=0, because the gzip
    # header itself carries a timestamp that would otherwise vary per run.
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for member in members:
            relative = member.relative_to(source).as_posix()
            info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{relative}")
            payload = normalize(member)
            info.size = len(payload)
            info.mtime = FIXED_MTIME
            # Fixed ownership and permissions: whoever packs it must not leak
            # their account into the artifact, and 0644 is what npm ships.
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(payload))

    with open(archive, "wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as zipped:
            zipped.write(raw.getvalue())

    if report:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        print(f"packed {archive}")
        print(f"  files  {len(members)}")
        print(f"  bytes  {archive.stat().st_size}")
        print(f"  sha256 {digest}")
        for member in members:
            print(f"    {member.relative_to(source).as_posix()}")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=SOURCE_DIR,
        help="plugin source directory (default: integrations/dsh-plugin)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / "dist",
        help="where to write the tarball (default: ./dist)",
    )
    args = parser.parse_args(argv)
    build(args.source_dir.resolve(), args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
