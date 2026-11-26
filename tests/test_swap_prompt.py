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

    BUILTIN_FILES_9 = (
        "单人物替换skill.md", "多人物替换skill.md",
        "单物品替换skill.md", "多物品替换skill.md",
        "单动物替换skill.md", "动物替换人skill.md",
        "人替换动物skill.md", "非活物替换skill.md",
        "单背景替换skill.md", "视频动作迁移skill.md",
        "风格化转绘skill.md", "人物换装skill.md",
        "视频物体消除skill.md", "视频人物换脸skill.md",
        "素模渲染skill.md", "局部材质替换skill.md",
    )
    OFFICIAL_SECTIONS = ("subject_definitions:", "summary:", "retention_analysis:",
                         "detailed_description:", "overall_soundscape:", "non_diegetic_music:")

    def test_builtin_skills_all_available(self):
        files = _SKILL._skill_files()
        for name in self.BUILTIN_FILES_9:
            self.assertIn(name, files, name)
            text = _SKILL._read_skill(name)
            self.assertIn("===H3_PROMPT===", text, name)
            self.assertIn("[SHOT_START]", text, name)
            self.assertIn("[SHOT_END]", text, name)

    def test_every_skill_uses_official_six_sections(self):
        """八份 skill 必须都是官方 Ref2VA 六段式（字段名 + 顺序）。"""
        for name in self.BUILTIN_FILES_9:
            text = _SKILL._read_skill(name)
            pos = [text.find(sec) for sec in self.OFFICIAL_SECTIONS]
            self.assertTrue(all(x >= 0 for x in pos), f"{name} 缺少段落：{pos}")
            self.assertEqual(pos, sorted(pos), f"{name} 六段顺序不对：{pos}")

    def test_every_skill_uses_video_editing_contract(self):
        """我们是「编辑源视频 + 保留原声」→ 必须用官方 video editing 任务前缀与固定首句。"""
        for name in self.BUILTIN_FILES_9:
            text = _SKILL._read_skill(name)
            self.assertIn("[video editing + audio reuse]", text, name)
            self.assertIn("The target video is an edited version of <Video 1>.", text, name)
            # 可见内容标记与音频标记不能串用
            self.assertIn("partially_preserved", text, name)

    def test_every_skill_has_anti_hallucination_rule(self):
        """长视频分段的核心约束：主体未出现在本段时不许凭空生成。"""
        for name in self.BUILTIN_FILES_9:
            text = _SKILL._read_skill(name)
            self.assertIn("不许凭空生成", text, name)
            self.assertIn("appears only where", text, name)

    def test_every_skill_forbids_position_words(self):
        """多人物/多物品必须要求「用视觉特征锁定」，不许用左右位置词。"""
        for name in ("多人物替换skill.md", "多物品替换skill.md"):
            text = _SKILL._read_skill(name)
            self.assertIn("位置词", text, name)
            self.assertIn("视觉特征", text, name)

    def test_builtin_skill_names_match_files(self):
        """每个内置 skill 名都必须能真的读到文件（否则 LLM 分支会静默回退）。"""
        self.assertEqual(set(_MOD.BUILTIN_SKILLS),
                         {"单人物替换", "多人物替换", "单物品替换", "多物品替换",
                          "单动物替换", "动物替换人", "人替换动物", "非活物替换",
                          "单背景替换", "视频动作迁移", "风格化转绘", "人物换装",
                          "视频物体消除", "视频人物换脸", "素模渲染", "局部材质替换"})
        for name, (fname, _tpl, _lead) in _MOD.BUILTIN_SKILLS.items():
            self.assertIn("===H3_PROMPT===", _SKILL._read_skill(fname), name)
            self.assertTrue(name in fname or fname in name, f"{name} ↔ {fname} 名字对不上")

    def test_animal_skill_forbids_adding_a_second_animal(self):
        text = _SKILL._read_skill(_MOD.BUILTIN_ANIMAL_SKILL)
        for needle in ("替换", "第二只动物", "人类", "no human skin"):
            self.assertIn(needle, text)

    def test_animal_swap_skill_replaces_whole_animal(self):
        text = _SKILL._read_skill(_MOD.BUILTIN_ANIMAL_SWAP_SKILL)
        for needle in ("整只", "毛色", "花纹", "第二只动物", "手部姿势"):
            self.assertIn(needle, text)

    def test_human_skill_removes_animal_features(self):
        text = _SKILL._read_skill(_MOD.BUILTIN_HUMAN_SKILL)
        for needle in ("正常人类解剖结构", "尾巴", "爪子", "四足"):
            self.assertIn(needle, text)

    def test_inanimate_skill_forbids_prop_and_alive(self):
        text = _SKILL._read_skill(_MOD.BUILTIN_INANIMATE_SKILL)
        for needle in ("非活物", "第二个同类物件", "当道具", "no human skin"):
            self.assertIn(needle, text)

    def test_object_skills_lock_occlusion_and_scale(self):
        for name in (_MOD.BUILTIN_OBJECT_SKILL, _MOD.BUILTIN_MULTI_OBJECT_SKILL):
            text = _SKILL._read_skill(name)
            for needle in ("遮挡", "接触点", "透视", "运动模糊"):
                self.assertIn(needle, text, f"{name} 缺少 {needle}")

    def test_background_skill_locks_foreground_and_parallax(self):
        """换背景是反向锁定：前景 100% 保留 + 光影融合 + 视差，且参考图是环境不是主体。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_BG_SKILL)
        for needle in ("前景主体", "100%", "轮廓光", "接触阴影", "视差", "平面贴图", "环境"):
            self.assertIn(needle, text, f"背景 skill 缺少 {needle}")

    def test_multi_skill_keeps_headcount(self):
        text = _SKILL._read_skill(_MOD.BUILTIN_MULTI_SKILL)
        for needle in ("人数", "不混用", "<Subject", "<Picture"):
            self.assertIn(needle, text)

    def test_person_skill_demands_full_body(self):
        text = _SKILL._read_skill(_MOD.BUILTIN_SWAP_SKILL)
        for needle in ("全身", "鞋", "配饰", "背景路人"):
            self.assertIn(needle, text)

    def test_action_transfer_skill_drops_source_look(self):
        """动作迁移 = 只借动作：源片外观/背景必须整条丢弃，身份与环境全来自参考图。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_ACTION_SKILL)
        for needle in ("motion driver", "丢弃", "视差", "平面贴图", "口型", "运镜",
                       "大幅度", "各分段"):
            self.assertIn(needle, text, f"动作迁移 skill 缺少 {needle}")

    def test_action_transfer_fallback_template_drops_source_look(self):
        """兜底模板也要写清「源片只提供动作」，否则 LLM 失败时会退化成人物替换。"""
        out = _MOD._build_swap_prompt(
            {"use_llm": False, "source": "内置skill", "builtin": "视频动作迁移"}, "", "")
        for needle in ("supplies motion, expression and camera work only",
                       "motion driver): partially_preserved",
                       "fully discarded", "never the person in <Video 1>",
                       "natural parallax"):
            self.assertIn(needle, out)

    def test_action_transfer_name_resolves_not_to_other_skill(self):
        """`动作迁移` 只映射到 `视频动作迁移skill.md`，不会串到别的 skill 上。"""
        self.assertEqual(_MOD._canonical_builtin("视频动作迁移"), _MOD.BUILTIN_ACTION_SKILL)
        self.assertEqual(_MOD._canonical_builtin("视频动作迁移skill"), _MOD.BUILTIN_ACTION_SKILL)
        self.assertEqual(_MOD._canonical_builtin("视频动作迁移skill.md"), _MOD.BUILTIN_ACTION_SKILL)

    def test_style_skill_forces_whole_frame_restyle(self):
        """风格化转绘：全片统一画风（防半真半假）+ 结构锁定 + 五类风格锚点。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_STYLE_SKILL)
        for needle in ("半真半假", "实拍", "构图比例", "发型轮廓", "笔触", "色彩体系", "各分段"):
            self.assertIn(needle, text, f"风格化转绘 skill 缺少 {needle}")

    def test_dress_skill_keeps_identity(self):
        """人物换装：脸/发型 100% 不变，参考图只给衣服。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_DRESS_SKILL)
        for needle in ("只提供服装", "发型", "100%", "褶皱", "遮挡关系", "不许凭空生成"):
            self.assertIn(needle, text, f"人物换装 skill 缺少 {needle}")

    def test_erase_skill_is_no_trace_and_two_way(self):
        """物体消除：无痕补全 + 视觉特征定位（不用位置词）+ 在则删/不在则不变。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_ERASE_SKILL)
        for needle in ("视觉特征", "无痕", "阴影", "视差", "严格保持原样", "不许凭空生成"):
            self.assertIn(needle, text, f"物体消除 skill 缺少 {needle}")

    def test_faceswap_skill_keeps_hair_and_outfit(self):
        """换脸：只换面部身份，发型/头饰/服装全部保留。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_FACESWAP_SKILL)
        for needle in ("只提供面部身份", "发型", "头饰", "服装", "口型", "贴脸", "背影"):
            self.assertIn(needle, text, f"换脸 skill 缺少 {needle}")

    def test_clay_skill_bans_previs_words_in_output(self):
        """素模渲染：正文严禁出现白模/灰模/线稿等词，且 <Video 1> 只作几何驱动。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_CLAY_SKILL)
        for needle in ("几何骨架", "完全丢弃", "严禁出现", "主光方向"):
            self.assertIn(needle, text, f"素模渲染 skill 缺少 {needle}")
        out = _MOD._build_swap_prompt(
            {"use_llm": False, "source": "内置skill", "builtin": "素模渲染"}, "", "")
        for bad in ("white model", "grey model", "line art", "wireframe", "previs",
                    "mannequin", "untextured", "clay"):
            self.assertNotIn(bad, out.lower(), f"素模渲染兜底正文出现了禁用词 {bad}")

    def test_material_skill_locks_geometry(self):
        """局部材质替换：几何锁定 + 物理光学 + 不溢出。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_MATERIAL_SKILL)
        for needle in ("几何", "折射", "反射", "不溢出", "粗糙度", "不许凭空生成"):
            self.assertIn(needle, text, f"局部材质替换 skill 缺少 {needle}")

    def test_no_skill_mentions_audio_tag(self):
        """源音轨从不进参考编码 → 16 份 skill 里都不许出现 `<Audio 1>`（模型看不到这个标签）。"""
        for name in self.BUILTIN_FILES_9:
            text = _SKILL._read_skill(name)
            # 只禁止「真的用了这个标签」：定义行（表行 / retention 行）；
            # 说明句里出现「不要写 <Audio 1>」这种字样是允许的。
            self.assertNotIn("| `<Audio 1>` |", text, name)
            for ln in text.splitlines():
                self.assertFalse(ln.lstrip().startswith("<Audio 1>"), f"{name}: {ln[:60]}")

    def test_no_fallback_template_mentions_audio_tag(self):
        """兜底模板同样不许出现 `<Audio 1> ... fully_copy`（否则模型会去「复刻声音」）。"""
        for name in _MOD.BUILTIN_SKILLS:
            out = _MOD._build_swap_prompt(
                {"use_llm": False, "source": "内置skill", "builtin": name}, "某人在做事", "某参考图")
            self.assertNotIn("<Audio 1>", out, name)
            self.assertNotIn("fully_copy", out, name)

    def test_strip_audio_tag_drops_user_skill_lines(self):
        """用户自定义 skill 里若还写着 `<Audio 1>` 行，也要在出提示词前截掉。"""
        raw = ("[SHOT_START]\n\n===H3_PROMPT===\nretention_analysis:\n"
               "<Subject 1> (appears in [Shot 1]): fully_preserved - x\n"
               "<Audio 1> (source soundtrack): fully_copy - y\n\n"
               "overall_soundscape: z\n[SHOT_END]")
        out = _MOD._strip_audio_tag(raw)
        self.assertNotIn("<Audio 1>", out)
        self.assertIn("fully_preserved - x", out)
        self.assertIn("overall_soundscape: z", out)

    def test_audio_mode_switches_soundscape(self):
        """使用原声 = 原样沿用；生成音频 = 改写声音段，不写「沿用」。"""
        cfg = {"use_llm": False, "source": "内置skill", "builtin": "单人物替换"}
        keep = _MOD._build_swap_prompt(cfg, "", "", audio_mode="使用原声")
        gen = _MOD._build_swap_prompt(cfg, "", "", audio_mode="生成音频")
        self.assertIn("retained unchanged", keep)
        self.assertNotIn("retained unchanged", gen)
        self.assertIn("Sound is generated as part of this video", gen)
        # 其余六段不受影响
        for sec in self.OFFICIAL_SECTIONS:
            self.assertIn(sec, gen)

    def test_each_builtin_has_distinct_fallback_template(self):
        """十六个内置 skill 的兜底模板必须各不相同（别都退回同一份）。"""
        outs = {}
        for name in _MOD.BUILTIN_SKILLS:
            outs[name] = _MOD._build_swap_prompt(
                {"use_llm": False, "source": "内置skill", "builtin": name},
                "某人在做事", "某参考图")
        self.assertEqual(len(set(outs.values())), 16)
        for name, out in outs.items():
            pos = [out.find(sec) for sec in self.OFFICIAL_SECTIONS]
            self.assertTrue(all(x >= 0 for x in pos), f"{name} 兜底模板缺段：{pos}")
            self.assertEqual(pos, sorted(pos), f"{name} 兜底模板六段顺序不对")
            self.assertIn("[video editing + audio reuse]", out, name)
            self.assertIn("The target video is an edited version of <Video 1>.", out, name)

    def test_branch_source_and_legacy_alias(self):
        """来源是两分支；老工作流的「内置skill·动物」映射成 内置skill + 动物替换人。"""
        cfg = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill·动物"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "内置skill")
        self.assertEqual(cfg["prompt_cfg"]["builtin"], "动物替换人skill.md")
        cfg2 = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill", "builtin": "多人替换"}}')
        self.assertEqual(cfg2["prompt_cfg"]["builtin"], "多人物替换skill.md")   # 老 builtin 名 → 目录里的真实文件
        # 未知 builtin → 回退默认内置 skill
        cfg3 = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill", "builtin": "不存在"}}')
        # 认不出来的值 → 回退到目录里的第一个内置 skill（不再写死某个名字）
        self.assertEqual(cfg3["prompt_cfg"]["builtin"], _MOD._default_builtin_file())

    def test_legacy_builtin_names_map_to_new_ones(self):
        """老工作流里存的旧内置名全部仍能解析到目录里的真实文件（不写死名字）。"""
        for old, new in (("人物替换", "单人物替换"), ("多人替换", "多人物替换"),
                         ("动物替换", "动物替换人")):
            resolved = _MOD._canonical_builtin(old)
            self.assertTrue(resolved, old)
            self.assertEqual(_MOD._skill_key(resolved), new)

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
        self.assertIn("多人物替换", _MOD._prompt_cfg_label(
            {"use_llm": True, "source": "内置skill", "builtin": "多人物替换"}))
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
