"""Documentation links must resolve, and the two entry points must stay split.

The README is the first thing a new user reads, so a dead link there costs more
than one in a reference page. These checks are cheap and catch the usual
breakage from moving a section between documents.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

DOCS = [
    "README.md",
    "docs/STANDALONE.md",
    "docs/DEPLOYMENT.md",
    "docs/REFERENCE.md",
    "docs/ARCHITECTURE.md",
    "integrations/dsh-plugin/README.md",
]

LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def slug(heading: str) -> str:
    """GitHub's heading anchor: lowercased, punctuation dropped, spaces hyphenated."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text)


def anchors(path: Path) -> set[str]:
    return {slug(m.group(1)) for m in HEADING.finditer(path.read_text(encoding="utf-8"))}


class DocLinkTests(unittest.TestCase):
    def test_relative_links_resolve(self) -> None:
        checked = 0
        for name in DOCS:
            source = REPO / name
            for label, target in LINK.findall(source.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                # `../../releases` is a GitHub repo-relative link, not a file.
                if target.startswith("../../"):
                    continue
                checked += 1
                rel, _, anchor = target.partition("#")
                destination = (source.parent / rel).resolve() if rel else source
                self.assertTrue(
                    destination.exists(),
                    f"{name}: [{label}]({target}) points at nothing",
                )
                if anchor and destination.suffix == ".md":
                    self.assertIn(
                        slug(anchor),
                        anchors(destination),
                        f"{name}: [{label}]({target}) has no such heading",
                    )
        # A guard that silently checks nothing is worse than no guard.
        self.assertGreater(checked, 15)

    def test_homepage_covers_the_harness_plugin(self) -> None:
        """The homepage is the Harness story; standalone use lives in its own doc."""
        home = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/STANDALONE.md", home, "the homepage must point at the other doc")
        for needle in ("dsh plugin install", "repoPath", "DeepSeek Harness"):
            self.assertIn(needle, home)
        # Browser-only operation must not be re-explained on the homepage, or the
        # two documents drift apart.
        for needle in ("dashboard.py configure", "/access-shares", "/monitor?project="):
            self.assertNotIn(needle, home, f"{needle} belongs in docs/STANDALONE.md")

    def test_install_instructions_use_an_absolute_path(self) -> None:
        """The Harness installer refuses a relative spec, so the docs must not show one.

        A bare filename is worse than refused: it parses as an npm package name
        and fails with "not found", which reads like a broken package rather
        than an incomplete path. Both READMEs point at the GUI dialog first,
        because installing should not require opening a terminal.
        """
        for name in ("README.md", "integrations/dsh-plugin/README.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertIn("添加插件", text, f"{name}: must describe the GUI install entry")
            self.assertIn("绝对", text, f"{name}: must require an absolute path")
            # A relative spec in a copyable command is the trap being avoided.
            self.assertNotIn(
                "install ./",
                text,
                f"{name}: a relative path is refused by the installer",
            )
            # The published asset is local-*, never deepseek-*.
            self.assertNotIn(
                "deepseek-fl-upgrade-workflow",
                text,
                f"{name}: stale artifact name",
            )
            if "dsh plugin install" in text:
                self.assertIn("local-fl-upgrade-workflow", text, f"{name}: names the real asset")

    def test_standalone_doc_is_self_contained(self) -> None:
        """Someone who never installs Harness must be able to start from it alone."""
        alone = (REPO / "docs" / "STANDALONE.md").read_text(encoding="utf-8")
        self.assertIn("../README.md", alone, "must link back to the homepage")
        for needle in ("dashboard.py configure", "dashboard.py start", "Python 3.11+"):
            self.assertIn(needle, alone)
        # The page table is the reason this document exists.
        for needle in ("/monitor?project=", "/devices?project=", "/access-shares"):
            self.assertIn(needle, alone)


if __name__ == "__main__":
    unittest.main()
