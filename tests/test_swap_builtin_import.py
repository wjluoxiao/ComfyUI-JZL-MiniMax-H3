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


@unittest.skipIf(_MOD is None or _SKILL is None, "需要 ComfyUI 环境才能加载节点模块")
class ImportedBuiltinSkillTest(unittest.TestCase):
    """「📥 导入Skill文件」：导入到内置目录的 skill 也能被「内置skill」分支选用。"""

    def setUp(self):
        # 临时把内置目录指到临时文件夹，避免污染仓库里的 skills/jzl
        self.dir = tempfile.mkdtemp(prefix="jzl_builtin_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._saved_bundled = _SKILL._BUNDLED_SKILL_DIR
        self._saved_legacy = _SKILL._LEGACY_SKILL_DIR
        _SKILL._BUNDLED_SKILL_DIR = self.dir
        _SKILL._LEGACY_SKILL_DIR = os.path.join(self.dir, "__none__")

        def _restore():
            _SKILL._BUNDLED_SKILL_DIR = self._saved_bundled
            _SKILL._LEGACY_SKILL_DIR = self._saved_legacy
        self.addCleanup(_restore)

    def _write_scene(self, scene, body="技能正文"):
        """在临时内置目录里造一份 `<场景>skill.md`（模拟插件自带的九份）。"""
        name = scene + "skill.md"
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(body)
        return name

    def _write_all_scenes(self):
        for scene in _MOD.BUILTIN_SKILLS:
            self._write_scene(scene)

    def _write(self, name, text="技能正文"):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_every_builtin_scene_resolves_from_folder(self):
        """内置名 → 目录里真实存在的文件（清单来自目录扫描，不写死）。"""
        self._write_all_scenes()
        for name in _MOD.BUILTIN_SKILLS:
            resolved = _MOD._builtin_skill_file(name)
            self.assertTrue(resolved, name)
            self.assertIn(resolved, _SKILL.list_builtin_skills(), name)

    def test_imported_file_resolves_when_present(self):
        """往内置目录里丢一个新文件，立刻就能被解析（不写死清单）。"""
        self._write("雨夜情绪.md")
        self.assertEqual(_MOD._builtin_skill_file("雨夜情绪.md"), "雨夜情绪.md")
        self.assertIn("雨夜情绪.md", _SKILL.list_builtin_skills())
        self.assertEqual(_MOD._builtin_skill_file("雨夜情绪"), "雨夜情绪.md")   # 短名也认

    def test_imported_file_missing_returns_empty(self):
        self.assertEqual(_MOD._builtin_skill_file("不存在.md"), "")

    def test_non_skill_extension_rejected(self):
        self._write("bad.json")
        for bad in ("bad.json", "bad", "", None, 123):
            self.assertEqual(_MOD._builtin_skill_file(bad), "")

    def test_path_traversal_rejected(self):
        outside = os.path.join(self.dir, "..", "evil.md")
        try:
            with open(outside, "w", encoding="utf-8") as f:
                f.write("x")
        except OSError:
            self.skipTest("无法在上级目录写文件")
        self.addCleanup(lambda: os.path.exists(outside) and os.remove(outside))
        self.assertEqual(_MOD._builtin_skill_file("../evil.md"), "")

    def test_resolve_prompt_kind_uses_imported_file(self):
        """目录里新放的文件也能被解析；没有元数据 → lead/模板为 None（走通用话术）。"""
        self._write("雨夜情绪.md")
        src, skill_file, tpl, lead = _MOD._resolve_prompt_kind(
            {"source": "内置skill", "builtin": "雨夜情绪.md"})
        self.assertEqual(src, "内置skill")
        self.assertEqual(skill_file, "雨夜情绪.md")
        self.assertIsNone(lead)
        self.assertIsNone(tpl)

    def test_resolve_prompt_kind_keeps_builtin_lead(self):
        self._write_all_scenes()
        src, skill_file, tpl, lead = _MOD._resolve_prompt_kind(
            {"source": "内置skill", "builtin": "动物替换人"})
        self.assertEqual(skill_file, _MOD.BUILTIN_SKILLS["动物替换人"][0])
        self.assertIsNotNone(lead)
        self.assertIsNotNone(tpl)

    def test_legacy_builtin_name_resolves_to_new_one(self):
        """老工作流存的是「动物替换」→ 落到动物替换人那份真正的文件。"""
        self._write_all_scenes()
        src, skill_file, tpl, lead = _MOD._resolve_prompt_kind(
            {"source": "内置skill", "builtin": "动物替换"})
        self.assertEqual(skill_file, _MOD.BUILTIN_SKILLS["动物替换人"][0])
        self.assertIsNotNone(lead)
        self.assertIsNotNone(tpl)

    def test_resolve_prompt_kind_falls_back_to_first_file(self):
        """配置里的文件名被删了 → 回退到目录里的第一个内置 skill（不是写死的名字）。"""
        self._write_all_scenes()
        src, skill_file, _tpl, _lead = _MOD._resolve_prompt_kind(
            {"source": "内置skill", "builtin": "已被删掉.md"})
        self.assertEqual(skill_file, _SKILL.list_builtin_skills()[0])

    def test_parse_settings_accepts_imported_builtin(self):
        self._write("雨夜情绪.md")
        pc = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"source": "内置skill", "builtin": "雨夜情绪.md"}}')["prompt_cfg"]
        self.assertEqual(pc["builtin"], "雨夜情绪.md")

    def test_parse_settings_resets_unknown_builtin(self):
        pc = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"source": "内置skill", "builtin": "子虚乌有.md"}}')["prompt_cfg"]
        self.assertEqual(pc["builtin"], _MOD.DEFAULT_BUILTIN_SKILL)

    def test_build_prompt_reads_imported_skill(self):
        """真正走一遍：LLM 关掉时用兜底模板（不该崩），LLM 打开时把导入的 skill 当设定词。"""
        self._write("雨夜情绪.md", "这是导入的 skills 设定词")
        # 关闭 LLM → 只出模板，且不因「导入 skill」而报错
        out = _MOD._build_swap_prompt({"use_llm": False, "source": "内置skill",
                                       "builtin": "雨夜情绪.md"}, "雨夜街头", "黑色长发")
        for needle in ("===H3_PROMPT===", "雨夜街头", "黑色长发", "[SHOT_END]"):
            self.assertIn(needle, out)

        # 打开 LLM：拦截 LLM 调用，确认 system 里拿到的是导入文件的内容
        seen = {}

        def _fake_llm(prompt_cfg, system_prompt, user_msg, tag=""):
            seen["system"] = system_prompt
            seen["user"] = user_msg
            return "[SHOT_START]\n\nOK\n[SHOT_END]"
        saved = _MOD._swap_llm_call
        _MOD._swap_llm_call = _fake_llm
        try:
            out2 = _MOD._build_swap_prompt({"use_llm": True, "source": "内置skill",
                                            "builtin": "雨夜情绪.md"}, "雨夜街头", "黑色长发")
        finally:
            _MOD._swap_llm_call = saved
        self.assertEqual(seen.get("system"), "这是导入的 skills 设定词")
        self.assertIn("把视频里的人换成参考图中的人", seen.get("user", ""))   # 通用话术
        self.assertIn("[SHOT_START]", out2)

    def test_import_endpoint_reuses_bundled_dir(self):
        """导入端点把文件写进内置目录（_skill_dirs()[0]），与「内置skill」下拉同源。"""
        self.assertEqual(_SKILL._skill_dirs()[0], self.dir)
        self.assertEqual(_SKILL.list_skills_in_dir(self.dir), [])


@unittest.skipIf(_SKILL is None, "需要 ComfyUI 环境才能加载 nodes_skill")
class UploadToBuiltinDirTest(unittest.TestCase):
    """云机路径：浏览器选中文件 → multipart → 服务器落盘到内置 skill 目录。

    完全不碰 tkinter：只要服务器进程对 skills/jzl 有写权限，就能上传。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jzl_upload_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _save(self, data, name):
        return _SKILL.save_skill_upload(data, name, dirs=[self.dir])

    def test_saves_and_lists(self):
        res = self._save("我的技能正文".encode("utf-8"), "雨夜情绪.md")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["id"], "雨夜情绪.md")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "雨夜情绪.md")))
        self.assertEqual(res["items"], ["雨夜情绪.md"])
        with open(os.path.join(self.dir, "雨夜情绪.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "我的技能正文")

    def test_same_name_gets_numbered(self):
        self._save(b"one", "a.md")
        res = self._save(b"two", "a.md")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["id"], "a_2.md")
        self.assertEqual(sorted(res["items"]), ["a.md", "a_2.md"])
        res3 = self._save(b"three", "a.md")
        self.assertEqual(res3["id"], "a_3.md")

    def test_rejects_bad_extension(self):
        for bad in ("x.exe", "x.json", "x", "x.md.exe"):
            res = self._save(b"data", bad)
            self.assertFalse(res["ok"], bad)
            self.assertIn("只支持", res["error"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_rejects_empty_content(self):
        res = self._save("   \n\n".encode("utf-8"), "e.md")
        self.assertFalse(res["ok"])
        self.assertIn("内容为空", res["error"])

    def test_decodes_gbk_upload(self):
        res = self._save("中文技能".encode("gbk"), "gbk.md")
        self.assertTrue(res["ok"], res)
        with open(os.path.join(self.dir, "gbk.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "中文技能")

    def test_decodes_utf8_bom(self):
        res = self._save("中文".encode("utf-8-sig"), "bom.md")
        self.assertTrue(res["ok"], res)
        with open(os.path.join(self.dir, "bom.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "中文")     # BOM 已剥掉

    def test_size_cap(self):
        big = b"a" * (2 * 1024 * 1024)
        res = self._save(big, "big.md")
        self.assertTrue(res["ok"], res)
        # 只写前 1MB（不落 2MB 文件）
        size = os.path.getsize(os.path.join(self.dir, "big.md"))
        self.assertLessEqual(size, 1024 * 1024 + 16)

    def test_path_traversal_filename_is_stripped(self):
        res = self._save(b"x", "../../evil.md")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["id"], "evil.md")             # basename 化
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "evil.md")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "..", "evil.md")))

    def test_txt_extension_ok(self):
        res = self._save(b"txt skill", "notes.txt")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["id"], "notes.txt")

    def test_empty_filename_falls_back(self):
        res = self._save(b"body", "")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["id"], "自定义skill.md")

    def test_created_file_is_visible_to_builtin_picker(self):
        """落盘后立刻能被「内置 skill」下拉识别（list_skills_in_dir 同源）。"""
        self._save(b"skill body", "新技能.md")
        self.assertIn("新技能.md", _SKILL.list_skills_in_dir(self.dir))
        self.assertEqual(_MOD._builtin_skill_file("新技能.md") if _MOD else "", "")  # 未进真实内置目录 → 不认，符合预期


if __name__ == "__main__":
    unittest.main()
