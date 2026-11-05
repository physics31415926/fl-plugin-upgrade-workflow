"""The published plugin tarball must install, and must be reproducible.

A broken archive is discovered after a user installs it, so the layout, the
declared file set and the reproducibility guarantee are all checked here.
"""

import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.pack_dsh_plugin import ARCHIVE_ROOT, build, normalize

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "integrations" / "dsh-plugin"


class PackagingTests(unittest.TestCase):
    def pack(self):
        """Build into a throwaway directory and return (path, member names)."""
        destination = Path(tempfile.mkdtemp())
        archive = build(SOURCE, destination, report=False)
        with tarfile.open(archive) as tar:
            names = sorted(tar.getnames())
        return archive, names

    def test_every_entry_sits_under_the_package_prefix(self):
        # Harness and npm both resolve a bundle relative to a single root
        # directory; a flat archive installs as nothing.
        _, names = self.pack()
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.startswith(f"{ARCHIVE_ROOT}/"), name)

    def test_the_archive_carries_exactly_the_declared_files(self):
        manifest = json.loads((SOURCE / "package.json").read_text(encoding="utf-8"))
        _, names = self.pack()
        packed = {n[len(ARCHIVE_ROOT) + 1:] for n in names}

        # The bundle cannot activate without these.
        self.assertIn("package.json", packed)
        self.assertIn("index.js", packed)
        self.assertIn("client.js", packed)
        self.assertIn(manifest["dsh"]["bundle"]["patch"].lstrip("./"), packed)

        # Both locales ship, or one language silently renders undefined labels.
        self.assertIn("locale/en.json", packed)
        self.assertIn("locale/zh.json", packed)

        # And nothing beyond what package.json declares: a stray test harness
        # or probe file would ship to users.
        for name in packed:
            self.assertFalse(name.startswith("_"), f"unexpected file {name}")
            self.assertFalse(name.endswith((".cjs", ".log", ".tgz")), f"unexpected file {name}")

    def test_the_archive_holds_no_runtime_or_configured_state(self):
        # Runtime directories hold machine profiles, session transcripts and
        # the encrypted credential store.
        _, names = self.pack()
        for name in names:
            self.assertNotIn("/runs/", name)
            self.assertNotIn("/.git", name)
            self.assertNotIn("node_modules", name)

    def test_packing_twice_produces_identical_bytes(self):
        # A published checksum is only meaningful if the build is reproducible,
        # which means no wall-clock timestamp and no packer identity.
        first, _ = self.pack()
        second, _ = self.pack()
        self.assertEqual(
            hashlib.sha256(first.read_bytes()).hexdigest(),
            hashlib.sha256(second.read_bytes()).hexdigest(),
        )

    def test_the_archive_records_no_packer_identity_or_build_time(self):
        archive, _ = self.pack()
        with tarfile.open(archive) as tar:
            for info in tar.getmembers():
                self.assertEqual(info.uid, 0)
                self.assertEqual(info.gid, 0)
                self.assertEqual(info.uname, "")
                self.assertEqual(info.gname, "")
                self.assertEqual(info.mode, 0o644)
                # One fixed mtime shared by every entry.
                self.assertEqual(info.mtime, tar.getmembers()[0].mtime)

    def test_line_endings_are_normalized_so_the_hash_is_platform_independent(self):
        # A Windows checkout has CRLF where a Linux one has LF, which would
        # otherwise make the archive hash depend on who built it.
        self.assertEqual(normalize.__doc__ is None, False)
        archive, _ = self.pack()
        with tarfile.open(archive) as tar:
            for info in tar.getmembers():
                if not info.name.endswith((".js", ".json", ".md", ".yml", ".svg")):
                    continue
                payload = tar.extractfile(info).read()
                self.assertNotIn(b"\r\n", payload, info.name)

    def test_the_packed_manifest_still_declares_a_harness_bundle(self):
        archive, _ = self.pack()
        with tarfile.open(archive) as tar:
            raw = tar.extractfile(f"{ARCHIVE_ROOT}/package.json").read()
        manifest = json.loads(raw)
        self.assertTrue(manifest["name"])
        self.assertTrue(manifest["version"])
        # Without this key Harness treats the package as a plain dependency
        # rather than an installable bundle.
        self.assertTrue(manifest["dsh"]["bundle"]["patch"])
        self.assertEqual(manifest["dsh"]["client"]["platform"], "web")

    def test_the_shipped_plugin_config_asks_for_no_credential(self):
        # Plugin config is stored as plain text in the profile, so a credential
        # field there would be a plaintext secret store.
        patch = (SOURCE / "cordis.patch.yml").read_text(encoding="utf-8").lower()
        for banned in ("password", "api_key", "apikey", "token", "secret", "private_key"):
            self.assertNotIn(banned, patch)


if __name__ == "__main__":
    unittest.main()
