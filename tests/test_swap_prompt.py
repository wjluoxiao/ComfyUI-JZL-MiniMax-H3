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

    def test_animal_skill_available_and_loadable(self):
        files = _SKILL._skill_files()
        self.assertIn("动物替换提示词.md", files)
        self.assertIn(_MOD.ANIMAL_SOURCE, _MOD.PROMPT_SOURCES)
        self.assertIn("===H3_PROMPT===", _SKILL._read_skill(_MOD.BUILTIN_ANIMAL_SKILL))

    def test_animal_skill_forbids_adding_a_second_animal(self):
        """skill 必须明确「替换」而不是「加入/互动」——这是这个 skill 的唯一目的。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_ANIMAL_SKILL)
        for needle in ("替代", "不出现第二只动物", "不出现任何人类", "互动"):
            self.assertIn(needle, text)

    def test_animal_source_falls_back_to_animal_template(self):
        out = _MOD._build_swap_prompt({"use_llm": False, "source": _MOD.ANIMAL_SOURCE},
                                      "男人坐在桌前喝水", "一只狸花猫，带白手套")
        # 必须是「动物替换」模板，不能退回人物模板（否则会写出人类角色）
        self.assertIn("动物", out)
        self.assertIn("不出现第二只动物", out)
        self.assertIn("狸花猫", out)
        self.assertNotIn("{video_desc}", out)
        self.assertNotIn("<Subject 1> 是 <Picture 1> 中的人物", out)

    def test_animal_source_kept_when_llm_on(self):
        cfg = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill·动物"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], _MOD.ANIMAL_SOURCE)

    def test_inanimate_skill_available_and_loadable(self):
        files = _SKILL._skill_files()
        self.assertIn("非活物替换提示词.md", files)
        self.assertIn(_MOD.INANIMATE_SOURCE, _MOD.PROMPT_SOURCES)
        text = _SKILL._read_skill(_MOD.BUILTIN_INANIMATE_SKILL)
        self.assertIn("===H3_PROMPT===", text)
        # 硬约束：替换 / 不变活物 / 不出现第二个同类物件 / 不是当道具
        for needle in ("替代", "非活物", "不出现第二个同类物件", "道具"):
            self.assertIn(needle, text)

    def test_inanimate_source_falls_back_to_inanimate_template(self):
        out = _MOD._build_swap_prompt({"use_llm": False, "source": _MOD.INANIMATE_SOURCE},
                                      "男人坐在桌前喝水", "一只毛绒兔子玩偶，灰色绒毛、黑豆眼")
        self.assertIn("非活物", out)
        self.assertIn("毛绒兔子玩偶", out)
        self.assertIn("不出现第二个同类物件", out)
        # 不能退回人物模板
        self.assertNotIn("<Subject 1> 是 <Picture 1> 中的人物", out)
        self.assertNotIn("{video_desc}", out)

    def test_inanimate_source_kept_when_llm_on(self):
        cfg = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill·非活物"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], _MOD.INANIMATE_SOURCE)

    def test_three_builtin_kinds_map_to_distinct_templates(self):
        """三种内置 skill 的兜底模板必须各不相同（别都退回人物模板）。"""
        outs = {}
        for src in ("内置skill", _MOD.ANIMAL_SOURCE, _MOD.INANIMATE_SOURCE):
            outs[src] = _MOD._build_swap_prompt({"use_llm": False, "source": src}, "某人在做事", "某参考图")
        self.assertEqual(len(set(outs.values())), 3)

    def test_schema_has_two_descriptions_last(self):
        schema = _MOD.JZL_MiniMaxCharacterSwap.define_schema()
        names = [getattr(i, "name", None) or getattr(i, "id", None) or "" for i in schema.inputs]
        self.assertIn("video_desc", names)
        self.assertIn("image_desc", names)
        self.assertNotIn("prompt", names)
        self.assertEqual(names[-1], "image_desc")


if __name__ == "__main__":
    unittest.main()
