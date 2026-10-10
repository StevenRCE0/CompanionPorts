import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import generate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.tree = Path(tempfile.mkdtemp())
        shutil.copytree(FIXTURES, self.tree, dirs_exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tree)

    def generate(self):
        outputs, problems = generate.generate(self.tree)
        return {p.relative_to(self.tree).as_posix(): json.loads(t) for p, t in outputs.items()}, problems

    def edit(self, relative, change):
        path = self.tree / relative
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    def test_lists_every_plugin_and_action(self):
        outputs, problems = self.generate()
        self.assertEqual(problems, [])
        discovery = outputs["discovery/index.json"]
        self.assertEqual(
            [(a["slug"], a["type"]) for a in discovery["actions"]],
            [("echo", "plugin"), ("hosted-mcp", "mcp"), ("notes-skill", "skill")],
        )
        self.assertEqual(discovery["curation"]["featured"], ["echo"])
        hosted = next(a for a in discovery["actions"] if a["slug"] == "hosted-mcp")
        self.assertEqual(hosted["secrets"], ["Access token"])
        self.assertNotIn("mcp", hosted)

        plugins = outputs["plugins/index.json"]["plugins"]
        self.assertEqual(plugins[0]["entry"], "plugin.py")
        self.assertEqual(plugins[0]["kinds"], ["echo"])
        self.assertEqual(plugins[0]["sha256"], discovery["actions"][0]["sha256"])
        self.assertEqual([f["path"] for f in plugins[0]["files"]], ["manifest.json", "plugin.py"])
        self.assertEqual(plugins[0]["sha256"], generate.folder_hash(plugins[0]["files"]))

    def test_hash_follows_content(self):
        before, _ = self.generate()
        (self.tree / "plugins/echo/plugin.py").write_text("changed\n")
        after, _ = self.generate()
        self.assertNotEqual(
            before["plugins/index.json"]["plugins"][0]["sha256"],
            after["plugins/index.json"]["plugins"][0]["sha256"],
        )

    def test_ignores_pycache(self):
        before, _ = self.generate()
        (self.tree / "plugins/echo/__pycache__").mkdir()
        (self.tree / "plugins/echo/__pycache__/plugin.pyc").write_bytes(b"\0")
        after, _ = self.generate()
        self.assertEqual(before, after)

    def test_scaffold_without_content_writes_nothing(self):
        empty = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(generate.generate(empty), ({}, []))
        finally:
            shutil.rmtree(empty)

    def test_rejects_undeclared_secret(self):
        self.edit("discovery/hosted-mcp/action.json", lambda d: d.update(secrets=[]))
        _, problems = self.generate()
        self.assertEqual(problems, ["discovery/hosted-mcp: references ${secret:token} but doesn't declare it"])

    def test_rejects_missing_entry(self):
        (self.tree / "plugins/echo/plugin.py").unlink()
        _, problems = self.generate()
        self.assertEqual(problems, ["plugins/echo: entry 'plugin.py' is missing"])

    def test_rejects_skill_without_skill_md(self):
        (self.tree / "discovery/notes-skill/skill/SKILL.md").unlink()
        _, problems = self.generate()
        self.assertEqual(problems, ["discovery/notes-skill: skill root 'skill' has no SKILL.md"])

    def test_rejects_slug_shared_with_a_plugin(self):
        shutil.copytree(self.tree / "discovery/notes-skill", self.tree / "discovery/echo")
        _, problems = self.generate()
        self.assertIn("discovery/echo: slug is already a plugin", problems)

    def test_rejects_curation_of_unknown_slug(self):
        self.edit("curation.json", lambda d: d.update(featured=["missing"]))
        _, problems = self.generate()
        self.assertEqual(problems, ["curation.json: 'missing' isn't a listed action"])

    def test_rejects_non_slug_folder(self):
        shutil.copytree(self.tree / "plugins/echo", self.tree / "plugins/Echo2")
        _, problems = self.generate()
        self.assertEqual(problems, ["plugins/Echo2: folder name is not a lowercase slug"])


if __name__ == "__main__":
    unittest.main()
