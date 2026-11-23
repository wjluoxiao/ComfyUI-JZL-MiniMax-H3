import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_mod():
    try:
        import importlib
        import types
        sys.path.insert(0, r"Z:\AI_work\ComfyUI")
        import folder_paths  # noqa: F401
    except Exception:
        return None, None
    import importlib
    import types
    if "jzlswap" not in sys.modules:
        pkg = types.ModuleType("jzlswap")
        pkg.__path__ = [_ROOT]
        sys.modules["jzlswap"] = pkg
    try:
        return (importlib.import_module("jzlswap.nodes_character_swap"),
                importlib.import_module("jzlswap.nodes_skill"))
    except Exception:
        return None, None


_MOD, _SKILL = _load_mod()


@unittest.skipIf(_MOD is None, "需要 ComfyUI 环境才能加载 nodes_character_swap")
class SwapPromptPresetTest(unittest.TestCase):
    """📝 提示词预设：LLM 开关 / 来源联动 / 默认模板填描述 / 内置 skill。"""

    def test_defaults_are_system_template_with_llm_off(self):
        pc = _MOD._parse_swap_settings("").get("prompt_cfg") or {}
        self.assertFalse(pc.get("use_llm"))
        self.assertEqual(pc.get("source"), "系统默认")

    def test_llm_off_forces_system_default(self):
        cfg = _MOD._parse_swap_settings('{"prompt_cfg": {"use_llm": false, "source": "内置skill"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "系统默认")

    def test_llm_on_keeps_source(self):
        for src in ("内置skill", "自定义skill", "系统默认"):
            cfg = _MOD._parse_swap_settings(
                '{"prompt_cfg": {"use_llm": true, "source": "%s"}}' % src)
            self.assertEqual(cfg["prompt_cfg"]["source"], src)

    def test_system_template_fills_descriptions(self):
        out = _MOD._build_swap_prompt({"use_llm": False, "source": "系统默认"},
                                      "女子在雨夜街头撑伞", "黑色及腰长发、米白针织衫")
        for needle in ("雨夜街头", "米白针织衫", "===H3_PROMPT===", "<Subject 1>", "[SHOT_END]"):
            self.assertIn(needle, out)
        self.assertNotIn("{video_desc}", out)
        self.assertNotIn("{image_desc}", out)

    def test_empty_descriptions_leave_no_blank_block(self):
        out = _MOD._build_swap_prompt({"use_llm": False, "source": "系统默认"}, "", "")
        self.assertNotIn("{video_desc}", out)
        self.assertNotIn("{image_desc}", out)
        self.assertNotIn("\n\n\n", out)

    def test_builtin_skill_available(self):
        files = _SKILL._skill_files()
        self.assertIn("人物替换提示词.md", files)
        self.assertIn("===H3_PROMPT===", _SKILL._read_skill("人物替换提示词.md"))

    def test_schema_has_two_descriptions_last(self):
        schema = _MOD.JZL_MiniMaxCharacterSwap.define_schema()
        names = [getattr(i, "name", None) or getattr(i, "id", None) or "" for i in schema.inputs]
        self.assertIn("video_desc", names)
        self.assertIn("image_desc", names)
        self.assertNotIn("prompt", names)
        self.assertEqual(names[-1], "image_desc")


if __name__ == "__main__":
    unittest.main()
