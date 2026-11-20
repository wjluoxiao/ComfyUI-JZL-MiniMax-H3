import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class _Schema:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _NodeOutput:
    def __init__(self, *values):
        self.values = values


class _Input:
    @staticmethod
    def Input(name, **kwargs):
        return {"name": name, **kwargs}


class _Output:
    @staticmethod
    def Output(name, **kwargs):
        return {"name": name, **kwargs}


def _load_module():
    io = types.SimpleNamespace(
        ComfyNode=type("ComfyNode", (), {}),
        Combo=_Input,
        String=_Output,
        Schema=_Schema,
        NodeOutput=_NodeOutput,
    )
    comfy_api = types.ModuleType("comfy_api")
    comfy_api.__path__ = []
    latest = types.ModuleType("comfy_api.latest")
    latest.io = io
    comfy_api.latest = latest
    module_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "nodes_skill.py")
    spec = importlib.util.spec_from_file_location("nodes_skill_test_target", module_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"comfy_api": comfy_api, "comfy_api.latest": latest}):
        spec.loader.exec_module(module)
    return module


skill_node = _load_module()


class SkillLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_dirs = skill_node._skill_dirs
        skill_node._skill_dirs = lambda: [self.temp_dir.name]

    def tearDown(self):
        skill_node._skill_dirs = self.original_dirs
        self.temp_dir.cleanup()

    def test_lists_only_markdown_and_text_files(self):
        for name in ("motion.md", "notes.TXT", "ignored.json"):
            with open(os.path.join(self.temp_dir.name, name), "w", encoding="utf-8") as stream:
                stream.write("content")
        self.assertEqual(skill_node._skill_files(), ["motion.md", "notes.TXT"])

    def test_loads_markdown_and_utf8_bom_text(self):
        with open(os.path.join(self.temp_dir.name, "motion.md"), "w", encoding="utf-8") as stream:
            stream.write("  规则一  ")
        with open(os.path.join(self.temp_dir.name, "avatar.txt"), "w", encoding="utf-8-sig") as stream:
            stream.write("  规则二  ")
        self.assertEqual(skill_node.JZL_MiniMaxSkillLoader.execute("motion.md").values, ("规则一",))
        self.assertEqual(skill_node.JZL_MiniMaxSkillLoader.execute("avatar.txt").values, ("规则二",))

    def test_schema_exposes_system_prompt_output_and_default_skill(self):
        for name in ("人物替换.md", "动作迁移.md", "数字人.md"):
            with open(os.path.join(self.temp_dir.name, name), "w", encoding="utf-8") as stream:
                stream.write(name)
        schema = skill_node.JZL_MiniMaxSkillLoader.define_schema()
        self.assertEqual(schema.node_id, "JZL_MiniMaxSkillLoader")
        self.assertEqual(schema.inputs[0]["default"], "人物替换.md")
        self.assertEqual(schema.outputs[0]["name"], "system_prompt")

    def test_preferred_skill_directory_wins_for_duplicate_names(self):
        preferred = os.path.join(self.temp_dir.name, "preferred")
        bundled = os.path.join(self.temp_dir.name, "bundled")
        os.makedirs(preferred)
        os.makedirs(bundled)
        for directory, text in ((preferred, "preferred"), (bundled, "bundled")):
            with open(os.path.join(directory, "shared.md"), "w", encoding="utf-8") as stream:
                stream.write(text)
        skill_node._skill_dirs = lambda: [preferred, bundled]
        self.assertEqual(skill_node.JZL_MiniMaxSkillLoader.execute("shared.md").values, ("preferred",))

    def test_rejects_traversal_and_unsupported_extensions(self):
        with self.assertRaises(ValueError):
            skill_node._read_skill("..\\outside.md")
        with self.assertRaises(ValueError):
            skill_node._read_skill("prompt.json")

    def test_fingerprint_changes_with_file_contents(self):
        path = os.path.join(self.temp_dir.name, "motion.md")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("first")
        first = skill_node.JZL_MiniMaxSkillLoader.fingerprint_inputs("motion.md")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("second")
        second = skill_node.JZL_MiniMaxSkillLoader.fingerprint_inputs("motion.md")
        self.assertNotEqual(first, second)

    def test_empty_skill_is_reported(self):
        with open(os.path.join(self.temp_dir.name, "empty.md"), "w", encoding="utf-8") as stream:
            stream.write(" \n ")
        with self.assertRaisesRegex(ValueError, "内容为空"):
            skill_node.JZL_MiniMaxSkillLoader.execute("empty.md")


if __name__ == "__main__":
    unittest.main()
