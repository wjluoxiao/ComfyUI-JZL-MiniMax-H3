import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_mods():
    try:
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


_MOD, _SKILL = _load_mods()


@unittest.skipIf(_SKILL is None, "需要 ComfyUI 环境才能加载 nodes_skill")
class SkillDirTest(unittest.TestCase):
    """「自定义skill → 📂 选择 skill 文件目录」：从用户指定目录列/读技能文件。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jzl_skill_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, name, text="技能正文"):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_list_only_skill_extensions(self):
        self._write("a.md")
        self._write("b.txt")
        self._write("c.json")          # 不是技能
        os.makedirs(os.path.join(self.dir, "sub.md"))   # 目录不算
        self.assertEqual(_SKILL.list_skills_in_dir(self.dir), ["a.md", "b.txt"])

    def test_list_is_sorted_and_safe_for_bad_dirs(self):
        for n in ("b.md", "A.md", "c.txt"):
            self._write(n)
        self.assertEqual(_SKILL.list_skills_in_dir(self.dir), ["A.md", "b.md", "c.txt"])
        for bad in (None, "", "   ", os.path.join(self.dir, "nope"), 123):
            self.assertEqual(_SKILL.list_skills_in_dir(bad), [])

    def test_read_returns_text(self):
        self._write("a.md", "  你好，技能  \n")
        self.assertEqual(_SKILL.read_skill_in_dir(self.dir, "a.md"), "你好，技能")

    def test_read_rejects_bad_inputs(self):
        self._write("a.md")
        self._write("c.json")
        cases = [
            (self.dir, "../a.md"),                     # 不许跳出目录
            (self.dir, "sub/a.md"),                    # 不许子路径
            (self.dir, ""),                            # 未选文件
            (self.dir, "c.json"),                      # 扩展名不对
            (self.dir, "missing.md"),                  # 不存在
            (os.path.join(self.dir, "nope"), "a.md"),  # 目录不存在
        ]
        for directory, name in cases:
            with self.assertRaises((ValueError, FileNotFoundError)):
                _SKILL.read_skill_in_dir(directory, name)

    def test_read_rejects_empty_file(self):
        self._write("empty.md", "   \n\n")
        with self.assertRaises(ValueError):
            _SKILL.read_skill_in_dir(self.dir, "empty.md")

    def test_symlink_is_refused(self):
        target = self._write("real.md")
        link = os.path.join(self.dir, "link.md")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("当前环境不支持创建符号链接")
        with self.assertRaises(FileNotFoundError):
            _SKILL.read_skill_in_dir(self.dir, "link.md")


@unittest.skipIf(_MOD is None, "需要 ComfyUI 环境才能加载 nodes_character_swap")
class SwapSkillDirWiringTest(unittest.TestCase):
    """prompt_cfg.skill_dir 一路传到 _load_swap_skill（后端读用户目录）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jzl_skilldir_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_defaults_include_skill_dir(self):
        pc = _MOD._SWAP_DEFAULTS["prompt_cfg"]
        self.assertIn("skill_dir", pc)
        self.assertEqual(pc["skill_dir"], "")

    def test_parse_keeps_skill_dir(self):
        raw = '{"prompt_cfg": {"source": "自定义skill", "skill": "a.md", "skill_dir": "E:/my skills"}}'
        pc = _MOD._parse_swap_settings(raw)["prompt_cfg"]
        self.assertEqual(pc["skill_dir"], "E:/my skills")
        self.assertEqual(pc["skill"], "a.md")
        self.assertTrue(pc["use_llm"])

    def test_parse_drops_non_string_skill_dir(self):
        pc = _MOD._parse_swap_settings('{"prompt_cfg": {"skill_dir": 123}}')["prompt_cfg"]
        self.assertEqual(pc["skill_dir"], "123")

    def test_load_swap_skill_prefers_chosen_dir(self):
        with open(os.path.join(self.dir, "only_here.md"), "w", encoding="utf-8") as f:
            f.write("这是用户目录里的技能")
        self.assertEqual(_MOD._load_swap_skill("only_here.md", "测试", self.dir),
                         "这是用户目录里的技能")
        # 不给目录时读不到（插件目录里没有这个文件）
        self.assertIsNone(_MOD._load_swap_skill("only_here.md", "测试", ""))

    def test_load_swap_skill_falls_back_to_plugin_dir(self):
        # 传了目录但文件只存在于插件目录 → 不回退（严格按用户选的目录走）
        self.assertIsNone(_MOD._load_swap_skill("人物替换提示词.md", "测试", self.dir))
        # 不传目录 → 走插件自带的 skills/jzl
        self.assertTrue(_MOD._load_swap_skill("人物替换提示词.md", "测试", ""))


if __name__ == "__main__":
    unittest.main()
