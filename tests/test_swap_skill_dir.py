import io
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
    """内置 = skills/jzl，自定义 = skills/jzl/custom；两边严格分开读，清单都来自目录扫描。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jzl_skilldir_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.custom = os.path.join(self.dir, "custom")
        os.makedirs(self.custom, exist_ok=True)
        self._saved_bundled = _SKILL._BUNDLED_SKILL_DIR
        self._saved_legacy = _SKILL._LEGACY_SKILL_DIR
        _SKILL._BUNDLED_SKILL_DIR = self.dir
        _SKILL._LEGACY_SKILL_DIR = os.path.join(self.dir, "__none__")

        def _restore():
            _SKILL._BUNDLED_SKILL_DIR = self._saved_bundled
            _SKILL._LEGACY_SKILL_DIR = self._saved_legacy
        self.addCleanup(_restore)

    def _write(self, where, name, text="技能正文"):
        with open(os.path.join(where, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_defaults_include_skill(self):
        pc = _MOD._SWAP_DEFAULTS["prompt_cfg"]
        self.assertIn("skill", pc)

    def test_parse_keeps_custom_skill(self):
        raw = '{"prompt_cfg": {"source": "自定义skill", "skill": "我的.md"}}'
        pc = _MOD._parse_swap_settings(raw)["prompt_cfg"]
        self.assertEqual(pc["skill"], "我的.md")
        self.assertTrue(pc["use_llm"])

    def test_load_builtin_and_custom_from_separate_folders(self):
        self._write(self.dir, "内置.md", "内置的技能")
        self._write(self.custom, "自定.md", "自定义的技能")
        self.assertEqual(_MOD._load_swap_skill("内置.md", "测试"), "内置的技能")
        self.assertEqual(_MOD._load_swap_skill("自定.md", "测试", custom=True), "自定义的技能")
        # 严格分目录：内置的读不到自定义的，反之亦然
        self.assertIsNone(_MOD._load_swap_skill("自定.md", "测试"))
        self.assertIsNone(_MOD._load_swap_skill("内置.md", "测试", custom=True))

    def test_folder_lists_are_dynamic(self):
        """增删文件后列表立刻变化（不写死清单）。"""
        self.assertEqual(_SKILL.list_builtin_skills(), [])
        self._write(self.dir, "新来的.md")
        self.assertEqual(_SKILL.list_builtin_skills(), ["新来的.md"])
        self.assertEqual(_SKILL.list_custom_skills(), [])
        self._write(self.custom, "也来了.txt")
        self.assertEqual(_SKILL.list_custom_skills(), ["也来了.txt"])
        os.remove(os.path.join(self.dir, "新来的.md"))
        self.assertEqual(_SKILL.list_builtin_skills(), [])

    def test_custom_dir_is_created_with_hint_file(self):
        shutil.rmtree(self.custom)
        d = _SKILL.ensure_custom_skill_dir()
        self.assertEqual(os.path.normcase(d), os.path.normcase(self.custom))
        self.assertTrue(os.path.isdir(d))
        # 说明文件不带 .md/.txt 扩展名 → 不会被当成 skill
        self.assertIn("README", os.listdir(d))
        self.assertEqual(_SKILL.list_custom_skills(), [])

    def test_custom_dir_param_is_used(self):
        """「📂 选择skill目录」：给了目录就只扫那一个，且能读到里面的文件（完全独立于插件）。"""
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "我的.md"), "w", encoding="utf-8") as f:
                f.write("我的自定义 skill 正文")
            self.assertEqual(_SKILL.list_custom_skills(tmp), ["我的.md"])
            self.assertEqual(_SKILL.read_custom_skill("我的.md", tmp), "我的自定义 skill 正文")
            self.assertEqual(_SKILL.resolve_custom_skill("我的.md", tmp),
                             os.path.join(tmp, "我的.md"))
            # 空目录 → 空清单（不回退到插件自带目录，否则就"不独立"了）
            empty = tempfile.mkdtemp()
            try:
                self.assertEqual(_SKILL.list_custom_skills(empty), [])
            finally:
                shutil.rmtree(empty, ignore_errors=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_custom_dir_still_validates_filename(self):
        """自定义目录里也不许用路径穿越读别的文件。"""
        tmp = tempfile.mkdtemp()
        try:
            self.assertRaises(ValueError, _SKILL.read_custom_skill, "../secrets.md", tmp)
            self.assertRaises(ValueError, _SKILL.read_custom_skill, "a.txt.exe", tmp)
            self.assertRaises(FileNotFoundError, _SKILL.read_custom_skill, "没有.md", tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_list_subdirs_helper(self):
        """目录浏览：只列子目录，且不接受不存在的路径（返回空而不是抛异常）。"""
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "sub_a"))
            os.makedirs(os.path.join(tmp, "sub_b"))
            with open(os.path.join(tmp, "file.md"), "w", encoding="utf-8") as f:
                f.write("x")
            cur, dirs, parent = _SKILL.list_subdirs(tmp)
            self.assertEqual(cur, os.path.abspath(tmp))
            self.assertEqual(dirs, ["sub_a", "sub_b"])      # 文件不在列表里
            self.assertTrue(parent)
            cur2, dirs2, _p2 = _SKILL.list_subdirs(os.path.join(tmp, "不存在"))
            self.assertEqual(dirs2, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_video_edit_node_has_no_audio_port(self):
        """参考音频只有人物替换有端口；视频编辑全部素材走总线。"""
        edit = getattr(_MOD, "JZL_MiniMaxVideoEdit", None)
        swap = getattr(_MOD, "JZL_MiniMaxCharacterSwap", None)
        if edit is None or swap is None:
            self.skipTest("需要 ComfyUI 环境")
        edit_ids = [i.id for i in edit.define_schema().inputs]
        swap_ids = [i.id for i in swap.define_schema().inputs]
        self.assertIn("ref_audio", swap_ids)
        self.assertNotIn("replace_audio", swap_ids)
        self.assertNotIn("ref_audio", edit_ids)
        self.assertNotIn("replace_audio", edit_ids)
        self.assertIn("bus", edit_ids)

    def test_swap_skills_routes_export_descriptions(self):
        """两条给前端下发清单的接口都必须带说明表，否则节点上全是「（暂无说明）」。"""
        with io.open(os.path.join(_ROOT, "__init__.py"), encoding="utf-8") as fh:
            src = fh.read()
        i = src.index('"/jzl/swap_skills"')
        self.assertIn('"descs": _rd()', src[i:i + 2600])
        self.assertIn("\"skill_descs\": skill_descs", src)
        self.assertIn("skill_descs = _jzl_read_descs()", src)

    def test_custom_skill_cannot_escape_dir(self):
        self._write(self.dir, "outside.md")
        for bad in ("../outside.md", "sub/outside.md", ""):
            # 底层读取器直接抛异常
            with self.assertRaises((ValueError, FileNotFoundError)):
                _SKILL.read_custom_skill(bad)
            # 节点侧吞掉异常并回退（不让一次坏配置炸掉整次生成）
            self.assertIsNone(_MOD._load_swap_skill(bad, "测试", custom=True))


if __name__ == "__main__":
    unittest.main()
