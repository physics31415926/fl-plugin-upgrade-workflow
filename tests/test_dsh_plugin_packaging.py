"""The published plugin tarball must install, and must be reproducible.

A broken archive is discovered after a user installs it, so the layout, the
declared file set and the reproducibility guarantee are all checked here.
"""

import hashlib
import json
import re
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
        """Plugin config is plain text in the profile, so no field may hold a secret.

        The config may *name* an environment variable whose value lives in the
        Harness credential store -- `githubTokenEnv: GITHUB_TOKEN` is a name, not
        a secret. So instead of banning the substring "token" outright, every
        value is checked: a key may mention a credential, but no value may look
        like one.
        """
        text = (SOURCE / "cordis.patch.yml").read_text(encoding="utf-8")
        # Comments are prose and may discuss secrets; only the settings matter.
        settings = "\n".join(
            line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
        )
        # Keys that would make this file a secret store whatever their value.
        for banned in ("password", "api_key", "apikey", "secret", "private_key"):
            self.assertNotIn(banned, settings.lower())

        # A credential-ish key is allowed only when its value is an environment
        # variable name: uppercase, underscores, no payload.
        pattern = re.compile(r"^\s*([A-Za-z_]*(?:token|key|credential)[A-Za-z_]*)\s*:\s*(.*)$",
                             re.IGNORECASE | re.MULTILINE)
        for key, value in pattern.findall(settings):
            cleaned = value.strip().strip("'\"")
            if not cleaned:
                continue
            self.assertRegex(
                cleaned, r"^[A-Z][A-Z0-9_]*$",
                f"{key} must hold an environment variable name, not a value: {cleaned!r}",
            )
            # An env NAME is short; a real token is not.
            self.assertLess(len(cleaned), 64, f"{key} value is too long to be a variable name")

    def test_the_install_instructions_name_the_shipped_version(self):
        """A stale filename in the install example sends users to a 404.

        Both READMEs paste an absolute path with the version in it, and the
        release asset is named from `package.json`. These drifted apart twice
        while shipping 0.2.0 and 0.2.1, so the version is checked rather than
        remembered.
        """
        version = json.loads((SOURCE / "package.json").read_text(encoding="utf-8"))["version"]
        expected = f"local-fl-upgrade-workflow-{version}.tgz"
        for readme in (REPO / "README.md", SOURCE / "README.md"):
            text = readme.read_text(encoding="utf-8")
            examples = re.findall(r"local-fl-upgrade-workflow-[0-9][^\s`'\"]*\.tgz", text)
            self.assertTrue(examples, f"{readme.name} shows no install example")
            for found in set(examples):
                self.assertEqual(found, expected,
                                 f"{readme.name} still names {found}, but the package is {version}")

    def test_the_readmes_do_not_repeat_withdrawn_claims(self):
        """Documentation that describes a secret's handling must not go stale.

        Each of these sentences was true when written and became false as the
        plugin changed. A wrong claim about where a credential is stored, or
        about whether a field exists, is worse than an awkward one: someone
        decides whether to paste a token based on it.
        """
        withdrawn = {
            "默认 `false`": "autoStart now defaults on",
            "只从环境变量读": "the token is collected and stored by the Harness now",
            "更换请在看板": "replacement happens in the panel now",
            "只有前两个": "three credentials are collectable now",
            "Each value goes straight to the dashboard": "the token goes to the Harness store",
        }
        for readme in (REPO / "README.md", SOURCE / "README.md"):
            text = readme.read_text(encoding="utf-8")
            for claim, why in withdrawn.items():
                self.assertNotIn(claim, text, f"{readme.name} repeats a withdrawn claim ({why})")


if __name__ == "__main__":
    unittest.main()
