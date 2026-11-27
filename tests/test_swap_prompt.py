import contextlib
import inspect
import io
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

    def test_defaults_are_plain_input_with_llm_off(self):
        pc = _MOD._parse_swap_settings("").get("prompt_cfg") or {}
        self.assertFalse(pc.get("use_llm"))
        self.assertEqual(pc.get("source"), "界面输入")

    def test_llm_flag_derived_from_source(self):
        """LLM 开关由「提示词来源」派生：只有界面输入才不调 LLM，旧的 use_llm 字段不再能改来源。"""
        cfg = _MOD._parse_swap_settings('{"prompt_cfg": {"use_llm": false, "source": "内置skill"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "内置skill")
        self.assertTrue(cfg["prompt_cfg"]["use_llm"])
        cfg = _MOD._parse_swap_settings('{"prompt_cfg": {"use_llm": true, "source": "界面输入"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "界面输入")
        self.assertFalse(cfg["prompt_cfg"]["use_llm"])

    def test_llm_on_keeps_source(self):
        for src in ("内置skill", "自定义skill"):
            cfg = _MOD._parse_swap_settings(
                '{"prompt_cfg": {"use_llm": true, "source": "%s"}}' % src)
            self.assertEqual(cfg["prompt_cfg"]["source"], src)

    def test_plain_input_passes_user_text_verbatim(self):
        """「界面输入」= 不加载任何 skill，用户填的文本原样当提示词。"""
        out = _MOD._build_swap_prompt({"use_llm": False, "source": "界面输入"},
                                      "女子在雨夜街头撑伞", "黑色及腰长发、米白针织衫")
        self.assertEqual(out, "女子在雨夜街头撑伞\n\n黑色及腰长发、米白针织衫")
        # 不再有内置模板的痕迹
        for gone in ("===H3_PROMPT===", "[SHOT_START]", "subject_definitions:", "<Subject 1>"):
            self.assertNotIn(gone, out)

    def test_plain_input_empty_text_stays_empty(self):
        self.assertEqual(_MOD._build_swap_prompt({"use_llm": False, "source": "界面输入"}, "", ""), "")

    BUILTIN_FILES_9 = (
        "01-单人替换skill.md",
        "02-多人替换skill.md",
        "03-单物替换skill.md",
        "04-多物替换skill.md",
        "05-动物替换skill.md",
        "06-动物换人skill.md",
        "07-人换动物skill.md",
        "08-死物替换skill.md",
        "09-背景替换skill.md",
        "10-人物换装skill.md",
        "11-人物换脸skill.md",
        "12-动作迁移skill.md",
        "13-风格转绘skill.md",
        "14-物体消除skill.md",
        "15-素模渲染skill.md",
        "16-材质替换skill.md",
        "17-组合编辑skill.md",
        "18-添加物体skill.md",
        "19-人物消失skill.md",
        "20-局部移除skill.md",
        "21-黑白上色skill.md",
        "22-氛围改造skill.md",
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
        for name in ("02-多人替换skill.md", "04-多物替换skill.md"):
            text = _SKILL._read_skill(name)
            self.assertIn("位置词", text, name)
            self.assertIn("视觉特征", text, name)

    def test_builtin_skill_names_match_files(self):
        """每个内置 skill 名都必须能真的读到文件（否则 LLM 分支会静默回退）。"""
        self.assertEqual(set(_MOD.BUILTIN_SKILLS),
                         {"单人替换", "多人替换", "单物替换", "多物替换",
                          "动物替换", "动物换人", "人换动物", "死物替换",
                          "背景替换", "动作迁移", "风格转绘", "人物换装",
                          "物体消除", "人物换脸", "素模渲染", "材质替换",
                          "组合编辑", "添加物体", "人物消失", "局部移除",
                          "黑白上色", "氛围改造"})
        for name, (fname, _lead) in _MOD.BUILTIN_SKILLS.items():
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

    def test_action_transfer_name_resolves_not_to_other_skill(self):
        """`动作迁移` 只映射到 `视频动作迁移skill.md`，不会串到别的 skill 上。"""
        self.assertEqual(_MOD._canonical_builtin("动作迁移"), _MOD.BUILTIN_ACTION_SKILL)
        self.assertEqual(_MOD._canonical_builtin("动作迁移skill"), _MOD.BUILTIN_ACTION_SKILL)
        self.assertEqual(_MOD._canonical_builtin("12-动作迁移skill.md"), _MOD.BUILTIN_ACTION_SKILL)

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

    def test_audio_mode_rewrites_llm_output(self):
        """LLM 输出的总体声音段按「声音来源」改写：使用原声=沿用；生成音频=由模型生成。"""
        keep = _MOD._apply_audio_mode("overall_soundscape: " + _MOD._AUDIO_REUSE_LINE, "使用原声")
        self.assertIn("retained unchanged", keep)
        gen = _MOD._apply_audio_mode("overall_soundscape: " + _MOD._AUDIO_REUSE_LINE, "生成音频")
        self.assertNotIn("retained unchanged", gen)
        self.assertIn("Sound is generated as part of this video", gen)

    def test_no_hardcoded_fallback_templates_left(self):
        """内置模板已全部删除：模块里不应再有 DEFAULT_*_PROMPT / _fb_prompt。"""
        self.assertFalse(hasattr(_MOD, "_fb_prompt"))
        leftovers = [n for n in dir(_MOD) if n.startswith("DEFAULT_") and n.endswith("_PROMPT")]
        self.assertEqual(leftovers, [])

    def test_first_pass_defaults_to_euler_and_custom_sigmas(self):
        """一采默认：euler + 显式 sigmas（6 步从 1.0 降到 0），与二采同一套交互。"""
        sd = _MOD._parse_swap_settings("")["sample_decode"]
        self.assertEqual(sd["sampler"], "euler")
        self.assertEqual(sd["sigmas_mode"], "custom")
        vals = _MOD._sigmas_from_text(sd["custom_sigmas"])
        self.assertEqual(vals, [1.0, 0.972973, 0.923077, 0.8, 0.571429, 0.333333, 0.0])
        self.assertEqual(vals[0], 1.0)
        self.assertEqual(vals[-1], 0.0)

    def test_first_pass_scheduler_mode_still_available(self):
        """切回「调度器」模式后，仍按 调度器/步数/降噪 算 — 老配置不会失效。"""
        sd = _MOD._parse_swap_settings(
            '{"sample_decode": {"sigmas_mode": "scheduler", "sampler": "res_multistep", "steps": 8}}'
        )["sample_decode"]
        self.assertEqual(sd["sigmas_mode"], "scheduler")
        self.assertEqual(sd["sampler"], "res_multistep")
        self.assertIn("8 步", _MOD._first_pass_desc(sd))

    def test_sigmas_from_text_rejects_garbage(self):
        self.assertRaises(ValueError, _MOD._sigmas_from_text, "")
        self.assertRaises(ValueError, _MOD._sigmas_from_text, "1.0")

    def test_output_format_has_no_code_fence(self):
        """格式示例不能包在 ``` 里 —— 否则模型会把它当「数据格式」, 转头输出 JSON。"""
        for name in self.BUILTIN_FILES_9:
            text = _SKILL._read_skill(name)
            self.assertNotIn("```", text, f"{name} 还留着代码围栏")
            self.assertIn("严禁输出 JSON", text, name)
            self.assertIn("===H3_PROMPT===", text, name)

    def test_normalize_turns_json_into_sections(self):
        """LLM 若把六段塞进 JSON，后端必须还原成字段格式（不能直接送给模型）。"""
        bad = ('[SHOT_START]\n\n'
               '{"subject_definitions": "A.", "summary": "B.", "retention_analysis": "C.", '
               '"detailed_description": "D.", "overall_soundscape": "E.", '
               '"non_diegetic_music": "N/A"}\n[SHOT_END]')
        out = _MOD._normalize_llm_prompt(bad)
        self.assertNotIn("{", out)
        self.assertIn("===H3_PROMPT===", out)
        for sec in _MOD._H3_SECTIONS:
            self.assertIn(sec + ":", out)
        self.assertTrue(out.startswith("[SHOT_START]"))
        self.assertTrue(out.endswith("[SHOT_END]"))

    def test_normalize_strips_fence_and_keeps_good_output(self):
        self.assertEqual(
            _MOD._normalize_llm_prompt("```text\n[SHOT_START]\n\n===H3_PROMPT===\nx\n[SHOT_END]\n```"),
            "[SHOT_START]\n\n===H3_PROMPT===\nx\n[SHOT_END]")
        good = "[SHOT_START]\n\n===H3_PROMPT===\nsubject_definitions:\nx\n[SHOT_END]"
        self.assertEqual(_MOD._normalize_llm_prompt(good), good)

    def test_combo_skill_declares_each_change(self):
        """组合编辑：每件改动都要单独声明保留等级，且不许自作主张多做。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_COMBO_SKILL)
        for needle in ("改动清单", "每件事都要有自己的保留声明", "绝不自作主张加第四件",
                       "不许串味", "默认不变"):
            self.assertIn(needle, text, f"组合编辑 skill 缺少 {needle}")

    def test_add_object_skill_is_add_not_replace(self):
        """添加物体：只增不减（模型最容易做成替换）。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_ADD_SKILL)
        for needle in ("只加不换", "ADDED", "接触阴影", "一次只加一个", "不加人"):
            self.assertIn(needle, text, f"添加物体 skill 缺少 {needle}")

    def test_vanish_skill_removes_traces(self):
        """人物消失：连阴影/反光/倒影一起移除 + 双向约束。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_VANISH_SKILL)
        for needle in ("整人消失", "阴影", "反光", "倒影", "双向约束", "不补新人"):
            self.assertIn(needle, text, f"人物消失 skill 缺少 {needle}")

    def test_partial_remove_restores_occluded(self):
        """局部移除：关键不是删，而是还原被遮住的部分。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_PARTREMOVE_SKILL)
        for needle in ("还原被遮住的部分", "摘眼镜", "不做美化", "时序一致"):
            self.assertIn(needle, text, f"局部移除 skill 缺少 {needle}")

    def test_colorize_skill_allows_natural_color_only(self):
        """黑白上色：唯一允许用常识的地方是固有色，且只限颜色。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_COLORIZE_SKILL)
        for needle in ("固有色", "唯一例外", "只限颜色", "保留年代质感",
                       "不锐化", "跨帧、跨段颜色必须一致"):
            self.assertIn(needle, text, f"黑白上色 skill 缺少 {needle}")

    def test_atmosphere_skill_keeps_geometry(self):
        """氛围改造：只改光，实体零位移。"""
        text = _SKILL._read_skill(_MOD.BUILTIN_ATMOS_SKILL)
        for needle in ("实体不动，只改光", "光照物理自洽", "不加实体", "跨段氛围一致"):
            self.assertIn(needle, text, f"氛围改造 skill 缺少 {needle}")

    def test_renamed_skills_resolve_from_old_names(self):
        """老工作流里存的旧名字必须还能解析到改名后的文件。"""
        for old, new in (("视频动作迁移", "12-动作迁移skill.md"),
                         ("视频人物换脸", "11-人物换脸skill.md"),
                         ("风格化转绘", "13-风格转绘skill.md"),
                         ("视频物体消除", "14-物体消除skill.md"),
                         ("局部材质替换", "16-材质替换skill.md")):
            self.assertEqual(_MOD._canonical_builtin(old), new, old)

    def test_every_skill_has_description(self):
        """22 份 skill 都必须有说明（节点表面与下拉框下方显示），且长度合理。"""
        for name in self.BUILTIN_FILES_9:
            desc = _SKILL.skill_description(name)
            self.assertNotEqual(desc, _SKILL._DESC_NO_HINT, f"{name} 缺说明")
            self.assertGreaterEqual(len(desc), 10, f"{name} 说明太短：{desc}")
            self.assertLessEqual(len(desc), 45, f"{name} 说明太长：{desc}")

    def test_description_file_is_not_listed_as_skill(self):
        """说明文件放在 _meta 子目录里，绝不能被当成 skill 列进下拉。"""
        files = _SKILL.list_builtin_skills()
        self.assertFalse([f for f in files if "说明" in f], files)
        self.assertTrue(os.path.isfile(_SKILL.skill_desc_path()))

    def test_skill_number_is_stripped_everywhere(self):
        """文件名带 01- 编号，但元数据键 / 解析 / 显示名都必须回到不带编号的短名。"""
        self.assertEqual(_MOD._skill_key("01-单人替换skill.md"), "单人替换")
        self.assertEqual(_MOD._skill_key("22-氛围改造skill.md"), "氛围改造")
        for form in ("单人替换", "01-单人替换", "01-单人替换skill",
                     "01-单人替换skill.md"):
            self.assertEqual(_MOD._canonical_builtin(form), "01-单人替换skill.md", form)

    def test_skills_stay_plain_text(self):
        """skill 是给 LLM 读的纯文本：不许有表格 / 引用块 / 分隔线，加粗也要克制。"""
        import re as _re
        for name in self.BUILTIN_FILES_9:
            text = _SKILL._read_skill(name)
            self.assertNotIn("|", text.replace("||", ""), f"{name} 还留着表格")
            self.assertNotRegex(text, r"(?m)^---", f"{name} 还留着分隔线")
            self.assertNotRegex(text, r"(?m)^>", f"{name} 还留着引用块")
            bolds = text.count("**") // 2
            self.assertLessEqual(bolds, 16, f"{name} 加粗 {bolds} 处，太多")

    def test_every_skill_ends_with_output_contract(self):
        """每份 skill 结尾都要有「输出约定」，掐掉格式回声（模型照抄标题/表格/加粗）。"""
        for name in self.BUILTIN_FILES_9:
            text = _SKILL._read_skill(name).rstrip()
            self.assertIn("## 输出约定", text, name)
            self.assertIn("不要输出标题、表格、加粗、引用符号", text, name)

    def test_every_skill_declares_video_editing_is_not_t2v(self):
        """每份 skill 都必须讲清「视频编辑 ≠ 文生视频/图生视频」，否则 LLM 会自己补外观。"""
        for name in self.BUILTIN_FILES_9:
            text = _SKILL._read_skill(name)
            self.assertIn("视频编辑 ≠ 文生视频", text, name)
            self.assertIn("视觉特征只能来自用户填的两段文本", text, name)

    def test_every_skill_forbids_inventing_appearance(self):
        """每份 skill 都要有「用户没写的一个词都不许补」+ 自检里查无依据的外观词。"""
        for name in self.BUILTIN_FILES_9:
            text = _SKILL._read_skill(name)
            self.assertIn("一个词都不许补", text, name)
            self.assertIn("找不到依据", text, name)

    def test_no_skill_orders_llm_to_enumerate_appearance(self):
        """禁止「写清脸型/发型/…」这种命令式列举（会逼 LLM 编造）。"""
        import re as _re
        bad = _re.compile(r"写清(脸型|发型|发色|肤色|体型|五官)")
        for name in self.BUILTIN_FILES_9:
            for ln in _SKILL._read_skill(name).splitlines():
                self.assertFalse(bad.search(ln), f"{name}: {ln.strip()[:60]}")

    def test_guard_is_prepended_to_every_skill(self):
        """后端共享总纲必须拼在 skill 前面（内置 / 自定义 / 导入 全部生效）。"""
        self.assertIn("视觉特征白名单制", _MOD._PROMPT_GUARD)
        seen = {}

        def _fake_llm(prompt_cfg, system_prompt, user_msg, tag=""):
            seen["system"] = system_prompt
            return "[SHOT_START]\nOK\n[SHOT_END]"
        saved = _MOD._swap_llm_call
        _MOD._swap_llm_call = _fake_llm
        try:
            _MOD._build_swap_prompt(
                {"use_llm": True, "source": "内置skill", "builtin": "单人替换"},
                "把白模人物换成图1的女人", "")
        finally:
            _MOD._swap_llm_call = saved
        self.assertTrue(seen["system"].startswith(_MOD._PROMPT_GUARD))
        self.assertIn("单人替换提示词生成器", seen["system"])

    def test_empty_text_tells_llm_not_to_describe(self):
        """用户两段文本为空时，提示词里必须明确「不要描述外观」，而不是「按通用要求处理」。"""
        seen = {}

        def _fake_llm(prompt_cfg, system_prompt, user_msg, tag=""):
            seen["user"] = user_msg
            return "[SHOT_START]\nOK\n[SHOT_END]"
        saved = _MOD._swap_llm_call
        _MOD._swap_llm_call = _fake_llm
        try:
            _MOD._build_swap_prompt(
                {"use_llm": True, "source": "内置skill", "builtin": "单人替换"}, "", "")
        finally:
            _MOD._swap_llm_call = saved
        self.assertIn("不要描述任何外观细节", seen["user"])
        self.assertIn("宁可省略也不要编造", seen["user"])

    def test_llm_off_falls_back_to_user_text(self):
        """skill 分支在没有 LLM 时也回退「界面输入」原文（不再有内置模板）。"""
        out = _MOD._build_swap_prompt(
            {"use_llm": False, "source": "内置skill", "builtin": "单人替换"},
            "某人在做事", "某参考图")
        self.assertEqual(out, "某人在做事\n\n某参考图")

    def test_builtin_meta_is_two_tuple(self):
        """BUILTIN_SKILLS 现在是 (skill 文件, LLM 附加指令) 两项。"""
        for name, meta in _MOD.BUILTIN_SKILLS.items():
            self.assertEqual(len(meta), 2, name)
            self.assertTrue(meta[0].endswith("skill.md"), name)
            self.assertIsInstance(meta[1], str)

    def test_branch_source_and_legacy_alias(self):
        """来源是两分支；老工作流的「内置skill·动物」映射成 内置skill + 动物换人。"""
        cfg = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill·动物"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "内置skill")
        self.assertEqual(cfg["prompt_cfg"]["builtin"], "06-动物换人skill.md")
        cfg2 = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill", "builtin": "多人替换"}}')
        self.assertEqual(cfg2["prompt_cfg"]["builtin"], "02-多人替换skill.md")   # 老 builtin 名 → 目录里的真实文件
        # 未知 builtin → 回退默认内置 skill
        cfg3 = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "内置skill", "builtin": "不存在"}}')
        # 认不出来的值 → 回退到目录里的第一个内置 skill（不再写死某个名字）
        self.assertEqual(cfg3["prompt_cfg"]["builtin"], _MOD._default_builtin_file())

    def test_legacy_builtin_names_map_to_new_ones(self):
        """老工作流里存的旧内置名全部仍能解析到目录里的真实文件（不写死名字）。"""
        cases = (("人物替换", "单人替换"),
                 # 2026-11 四字档改名之前的中间名（这些名字用户的工作流里存过）
                 ("单人物替换", "单人替换"), ("多人物替换", "多人替换"),
                 ("单物品替换", "单物替换"), ("多物品替换", "多物替换"),
                 ("单动物替换", "动物替换"), ("动物替换人", "动物换人"),
                 ("人替换动物", "人换动物"), ("非活物替换", "死物替换"),
                 ("单背景替换", "背景替换"),
                 # 老文件名形式也要能落到新文件上
                 ("06-动物替换人skill.md", "动物换人"),
                 ("08-非活物替换skill.md", "死物替换"))
        for old, new in cases:
            resolved = _MOD._canonical_builtin(old)
            self.assertTrue(resolved, old)
            self.assertEqual(_MOD._skill_key(resolved), new, old)

    def test_custom_skill_branch_kept(self):
        cfg = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "自定义skill", "skill": "我的.md"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "自定义skill")
        self.assertEqual(cfg["prompt_cfg"]["skill"], "我的.md")

    def test_plain_input_branch_works_with_llm_on(self):
        """「界面输入」始终可选：LLM 开着也选它 → 直接透传文本、不调 LLM。"""
        cfg = _MOD._parse_swap_settings(
            '{"prompt_cfg": {"use_llm": true, "source": "界面输入"}}')
        self.assertEqual(cfg["prompt_cfg"]["source"], "界面输入")
        self.assertFalse(cfg["prompt_cfg"]["use_llm"])
        out = _MOD._build_swap_prompt(
            {"use_llm": True, "source": "界面输入"}, "女子在雨夜街头撑伞", "黑色及腰长发")
        self.assertEqual(out, "女子在雨夜街头撑伞\n\n黑色及腰长发")

    def test_prompt_cfg_label(self):
        self.assertIn("关", _MOD._prompt_cfg_label({"use_llm": False}))
        self.assertIn("界面输入", _MOD._prompt_cfg_label({"use_llm": True, "source": "界面输入"}))
        self.assertIn("多人替换", _MOD._prompt_cfg_label(
            {"use_llm": True, "source": "内置skill", "builtin": "多人替换"}))
        self.assertIn("我的.md", _MOD._prompt_cfg_label(
            {"use_llm": True, "source": "自定义skill", "skill": "我的.md"}))

    def test_log_tag_follows_current_global(self):
        """日志前缀必须取运行时的 TAG：视频编辑节点整条链路都该打「视频编辑」。

        用 `tag=TAG` 当默认参数会在函数定义时冻结成「人物替换」，所以这里锁死必须是 None。
        """
        for fn in (_MOD._load_swap_skill, _MOD._swap_llm_call, _MOD._build_swap_prompt,
                   _MOD._cache_source, _MOD._free_before_sampling, _MOD._log_peak):
            self.assertIsNone(inspect.signature(fn).parameters["tag"].default, fn.__name__)
        saved = _MOD.TAG
        try:
            _MOD.TAG = "视频编辑"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _MOD._load_swap_skill("回归测试-不存在的skill文件.md")
            self.assertIn("[JZL-视频编辑]", buf.getvalue())
        finally:
            _MOD.TAG = saved

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
