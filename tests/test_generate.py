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

    def rows(self):
        outputs, problems = self.generate()
        self.assertEqual(problems, [])
        return outputs["plugins/index.json"]["plugins"]

    def test_lists_each_plugin_with_its_files(self):
        [row] = self.rows()
        self.assertEqual(row["slug"], "echo")
        self.assertEqual(row["name"], "Echo")
        self.assertEqual(row["summary"], "Says back what it's given")
        self.assertEqual(row["entry"], "plugin.py")
        self.assertEqual(row["kinds"], ["echo"])
        self.assertEqual([f["path"] for f in row["files"]], ["manifest.json", "plugin.py"])
        self.assertEqual(row["sha256"], generate.folder_hash(row["files"]))

    def test_hash_follows_content(self):
        before = self.rows()[0]["sha256"]
        (self.tree / "plugins/echo/plugin.py").write_text("changed\n")
        self.assertNotEqual(before, self.rows()[0]["sha256"])

    def test_ignores_pycache(self):
        before = self.rows()
        (self.tree / "plugins/echo/__pycache__").mkdir()
        (self.tree / "plugins/echo/__pycache__/plugin.pyc").write_bytes(b"\0")
        self.assertEqual(before, self.rows())

    def test_scaffold_without_plugins_writes_nothing(self):
        empty = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(generate.generate(empty), ({}, []))
        finally:
            shutil.rmtree(empty)

    def test_rejects_missing_entry(self):
        (self.tree / "plugins/echo/plugin.py").unlink()
        _, problems = self.generate()
        self.assertEqual(problems, ["plugins/echo: entry 'plugin.py' is missing"])

    def test_rejects_repeated_kinds(self):
        path = self.tree / "plugins/echo/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["kinds"].append(manifest["kinds"][0])
        path.write_text(json.dumps(manifest))
        _, problems = self.generate()
        self.assertEqual(problems, ["plugins/echo: kind names repeat"])

    def test_rejects_non_slug_folder(self):
        shutil.copytree(self.tree / "plugins/echo", self.tree / "plugins/Echo2")
        _, problems = self.generate()
        self.assertEqual(problems, ["plugins/Echo2: folder name is not a lowercase slug"])


if __name__ == "__main__":
    unittest.main()
