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

    def test_llm_flag_derived_from_source(self):
        """LLM 开关由「提示词来源」派生：只有系统默认才不调 LLM，旧的 use_llm 字段不再能改来源。"""
        cfg = _MOD._parse_swap_settings('{"prompt_cfg": {"use_llm": false, "source": "内置skill"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "内置skill")
        self.assertTrue(cfg["prompt_cfg"]["use_llm"])
        cfg = _MOD._parse_swap_settings('{"prompt_cfg": {"use_llm": true, "source": "系统默认"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "系统默认")
        self.assertFalse(cfg["prompt_cfg"]["use_llm"])

    def test_llm_on_keeps_source(self):
        for src in ("内置skill", "自定义skill"):
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

    def test_builtin_skills_all_available(self):
        files = _SKILL._skill_files()
        for name in ("人物替换提示词.md", "动物替换提示词.md",
                     "非活物替换提示词.md", "多人替换提示词.md"):
            self.assertIn(name, files)
            self.assertIn("===H3_PROMPT===", _SKILL._read_skill(name))

    def test_builtin_skill_names_match_files(self):
        """每个内置 skill 名都必须能真的读到文件（否则 LLM 分支会静默回退）。"""
        self.assertEqual(set(_MOD.BUILTIN_SKILLS),
                         {"人物替换", "动物替换", "非活物替换", "多人替换"})
        for name, (fname, _tpl, _lead) in _MOD.BUILTIN_SKILLS.items():
            self.assertIn("===H3_PROMPT===", _SKILL._read_skill(fname), name)

    def test_animal_skill_forbids_adding_a_second_animal(self):
        text = _SKILL._read_skill(_MOD.BUILTIN_ANIMAL_SKILL)
        for needle in ("替代", "不出现第二只动物", "不出现任何人类", "互动"):
            self.assertIn(needle, text)

    def test_inanimate_skill_forbids_prop_and_alive(self):
        text = _SKILL._read_skill(_MOD.BUILTIN_INANIMATE_SKILL)
        for needle in ("替代", "非活物", "不出现第二个同类物件", "道具"):
            self.assertIn(needle, text)

    def test_multi_skill_keeps_headcount(self):
        text = _SKILL._read_skill(_MOD.BUILTIN_MULTI_SKILL)
        for needle in ("人数", "不混用", "<Subject 1>", "<Picture 2>"):
            self.assertIn(needle, text)

    def test_each_builtin_has_distinct_fallback_template(self):
        """四种内置 skill 的兜底模板必须各不相同（别都退回人物模板）。"""
        outs = {}
        for name in _MOD.BUILTIN_SKILLS:
            outs[name] = _MOD._build_swap_prompt(
                {"use_llm": False, "source": "内置skill", "builtin": name},
                "某人在做事", "某参考图")
        self.assertEqual(len(set(outs.values())), 4)
        self.assertIn("人数", outs["多人替换"])

    def test_branch_source_and_legacy_alias(self):
        """来源是两分支；老工作流的「内置skill·动物」自动映射成 内置skill + builtin。"""
        cfg = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill·动物"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "内置skill")
        self.assertEqual(cfg["prompt_cfg"]["builtin"], "动物替换")
        cfg2 = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill", "builtin": "多人替换"}}')
        self.assertEqual(cfg2["prompt_cfg"]["builtin"], "多人替换")
        # 未知 builtin → 回退人物替换
        cfg3 = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill", "builtin": "不存在"}}')
        self.assertEqual(cfg3["prompt_cfg"]["builtin"], "人物替换")

    def test_custom_skill_branch_kept(self):
        cfg = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "自定义skill", "skill": "我的.md"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "自定义skill")
        self.assertEqual(cfg["prompt_cfg"]["skill"], "我的.md")

    def test_system_default_branch_works_with_llm_on(self):
        """「系统默认」恢复成可选分支：LLM 开着也能选它 → 只用模板、不调 LLM。"""
        cfg = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "系统默认"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "系统默认")
        out = _MOD._build_swap_prompt(
            {"use_llm": True, "source": "系统默认"}, "女子在雨夜街头撑伞", "黑色及腰长发")
        self.assertIn("雨夜街头", out)
        self.assertIn("===H3_PROMPT===", out)

    def test_prompt_cfg_label(self):
        self.assertIn("关", _MOD._prompt_cfg_label({"use_llm": False}))
        self.assertIn("系统默认", _MOD._prompt_cfg_label({"use_llm": True, "source": "系统默认"}))
        self.assertIn("多人替换", _MOD._prompt_cfg_label(
            {"use_llm": True, "source": "内置skill", "builtin": "多人替换"}))
        self.assertIn("我的.md", _MOD._prompt_cfg_label(
            {"use_llm": True, "source": "自定义skill", "skill": "我的.md"}))

    def test_fmt_secs(self):
        self.assertEqual(_MOD._fmt_secs(49.6), "49.6 秒")
        self.assertEqual(_MOD._fmt_secs(216), "3 分 36 秒")
        self.assertEqual(_MOD._fmt_secs(4325), "1 小时 12 分")

    def test_schema_has_two_descriptions_last(self):
        schema = _MOD.JZL_MiniMaxCharacterSwap.define_schema()
        names = [getattr(i, "name", None) or getattr(i, "id", None) or "" for i in schema.inputs]
        self.assertIn("video_desc", names)
        self.assertIn("image_desc", names)
        self.assertNotIn("prompt", names)
        self.assertEqual(names[-1], "image_desc")


if __name__ == "__main__":
    unittest.main()
