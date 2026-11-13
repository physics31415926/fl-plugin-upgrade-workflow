import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_dsh_plugin as release  # noqa: E402

NAME = "@physics31415926/fl-upgrade-workflow"


class ReleaseScriptTests(unittest.TestCase):
    def test_finds_linked_copies_by_package_name_not_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            linked = home / "plugin-copy"
            other = home / "other"
            for path, name in ((linked, NAME), (other, "something-else")):
                path.mkdir()
                (path / "package.json").write_text(json.dumps({"name": name}), encoding="utf-8")
            profile = home / ".dsh" / "profiles" / "desktop"
            profile.mkdir(parents=True)
            (profile / "package.json").write_text(json.dumps({"dependencies": {
                "@local/fl-upgrade-workflow": "link:" + linked.as_posix(),
                "other": "link:" + other.as_posix(),
                "pinned": "1.0.0",
                "self": "link:" + release.SOURCE.as_posix(),
            }}), encoding="utf-8")
            with patch.object(release.Path, "home", return_value=home):
                self.assertEqual(release.profile_link_dirs(NAME), [linked])

    def test_set_version_updates_package_and_readme_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "package.json").write_text('{\n  "name": "x",\n  "version": "0.1.0"\n}\n', encoding="utf-8")
            readme = source / "README.md"
            readme.write_text("pnpm add ./physics31415926-fl-upgrade-workflow-0.1.0.tgz\n", encoding="utf-8")
            with patch.object(release, "SOURCE", source), patch.object(release, "READMES", [readme]):
                release.set_version("0.2.3", NAME)
            self.assertIn('"version": "0.2.3"', (source / "package.json").read_text(encoding="utf-8"))
            self.assertIn("workflow-0.2.3.tgz", readme.read_text(encoding="utf-8"))

    def test_rejects_malformed_version(self):
        with self.assertRaises(SystemExit):
            release.main(["v1"])


if __name__ == "__main__":
    unittest.main()
