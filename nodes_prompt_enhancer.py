"""JZL MiniMax — 提示词增强节点（独立文件，专一设定）。

串联在「剧本与镜头处理器」与「分段处理中心」之间：
  剧本输出 ──► 提示词增强 ──► 增强后剧本 ──► 分段处理中心
              BUS ──────────► 提示词增强（读取模型/API/偏好/风格等参数）

职责单一：独立运行一次 LLM，把剧本输出里每个分段的 detailed_description
字段重写、扩写并替换回原位置；其余字段（subject_definitions / summary /
retention_analysis / overall_soundscape / non_diegetic_music / 调度指令）原样保留。
"""

import re

from .llama_backend import LLAMA_CPP_STORAGE
from .nodes_llama import JZL_MiniMax_ScriptProcessor
from .sheding.prompt_enhancer_rules import build_enhancer_prompt, build_segment_position


class JZL_MiniMaxPromptEnhancer:
    """提示词增强 — 只润色 detailed_description，其余原样保留。"""

    _SEGMENT_INFO_KEYS = ["标题", "时长", "景别", "运镜", "角色", "场景", "道具", "动作描述", "氛围光影"]
    _SEGMENT_INFO_KEYS_EN = ["Title", "Duration", "Shot size", "Camera", "Characters", "Scene", "Props", "Action", "Atmosphere"]
    _CN_NUMS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
                "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
                "二十一", "二十二", "二十三", "二十四"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "script_text": ("STRING", {"multiline": True, "forceInput": True,
                    "tooltip": "从「剧本与镜头处理器」的「剧本输出」连线"}),
                "bus": ("JZL_H3_BUS", {"forceInput": True,
                    "tooltip": "从「剧本与镜头处理器」的 BUS 连线，传递模型/API/偏好/风格等参数"}),
                "force_offload": ("BOOLEAN", {"default": False, "label_on": "增强后卸载", "label_off": "不卸载",
                    "tooltip": "本地模型：增强完成后卸载。建议剧本处理器关闭卸载、本节点开启，等增强完成再卸载，省一次重复加载"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "step": 1, "control_after_generate": True,
                    "tooltip": "随机种子\n改 seed 可生成不同的润色结果；前端可选随机/递增/固定"}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("增强后剧本",)
    FUNCTION = "enhance"
    CATEGORY = "JZL/MiniMax"

    @classmethod
    def IS_CHANGED(cls, seed=0, **kwargs):
        return seed

    # ── 解析工具 ──────────────────────────────────────────────

    @staticmethod
    def _split_blocks(text):
        """分离统计表前缀与 [SHOT_START]...[SHOT_END] 块。"""
        text = text or ""
        blocks = re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', text, re.DOTALL)
        prefix = re.split(r'\[SHOT_START\]', text, maxsplit=1)[0]
        return prefix.rstrip(), [b.strip() for b in blocks]

    @staticmethod
    def _extract_field(h3_body, field_name):
        """提取 H3_PROMPT 六字段之一（按行首字段名定位，到下一字段行首结束）。"""
        m = re.search(rf'(?m)^{field_name}\s*:\s*', h3_body)
        if not m:
            return ""
        content_start = m.end()
        tail = h3_body[content_start:]
        next_m = re.search(r'(?m)^[a-z_]+\s*:\s*', tail)
        content_end = content_start + (next_m.start() if next_m else len(tail))
        return h3_body[content_start:content_end].strip()

    @classmethod
    def _extract_segment_info(cls, block):
        """提取分段信息九行（**标题** 等），返回 dict。"""
        info = {}
        for key in cls._SEGMENT_INFO_KEYS:
            m = re.search(rf'\*\*{key}\*\*\s*[：:]\s*(.*?)(?:\n|$)', block)
            info[key] = m.group(1).strip() if m else ""
        return info

    @staticmethod
    def _extract_dispatch(block):
        """提取三个调度指令段，供 LLM 对齐标签编号。"""
        parts = []
        for marker in ("===SCENE_INSTRUCTION===", "===VIDEO_INSTRUCTION===", "===AUDIO_INSTRUCTION==="):
            m = re.search(rf'{marker}\s*(.*?)(?====|\[SHOT_END\]|\Z)', block, re.DOTALL)
            if m:
                parts.append(f"{marker}\n{m.group(1).strip()}")
        return "\n".join(parts)

    @staticmethod
    def _collect_shots(text):
        """提取文本中出现的 [Shot N] 编号（按出现顺序去重）。"""
        ids = []
        for m in re.finditer(r'\[Shot\s+(\d+)\]', text or ""):
            n = int(m.group(1))
            if n not in ids:
                ids.append(n)
        return ids

    @classmethod
    def _sync_retention_shots(cls, h3_body, new_dd):
        """把 retention_analysis 里的 (出现在 [Shot ...]) / (appears in [Shot ...]) 引用
        同步为润色后 detailed_description 的实际镜头编号，避免两者不一致。"""
        shot_ids = cls._collect_shots(new_dd)
        if not shot_ids:
            return h3_body
        shot_list = ", ".join(f"[Shot {n}]" for n in shot_ids)

        ra_m = re.search(r'(?m)^retention_analysis\s*:\s*', h3_body)
        if not ra_m:
            return h3_body
        content_start = ra_m.end()
        tail = h3_body[content_start:]
        next_m = re.search(r'(?m)^[a-z_]+\s*:\s*', tail)
        content_end = content_start + (next_m.start() if next_m else len(tail))
        ra = h3_body[content_start:content_end]

        # 中文：<Subject 1> (出现在 [Shot 1], [Shot 2]): ...
        ra = re.sub(r'\(出现在[^)]*\)', f'(出现在 {shot_list})', ra)
        # 英文：<Subject 1> (appears in [Shot 1], [Shot 2]): ...
        ra = re.sub(r'\(appears in [^)]*\)', f'(appears in {shot_list})', ra)

        return h3_body[:content_start] + ra + h3_body[content_end:]

    @staticmethod
    def _normalize_h3_blank_lines(body):
        """兜底规范 H3 块字段间距：
        - 六个字段名行（subject_definitions / summary / retention_analysis / detailed_description /
          overall_soundscape / non_diegetic_music）之前保证一个空行；
        - 调度指令行（===SCENE_INSTRUCTION=== / ===VIDEO_INSTRUCTION=== / ===AUDIO_INSTRUCTION===）
          之前保证一个空行（防止 non_diegetic_music 与指令粘连同行）。
        已有空行则不会重复添加，绝不改动内容行。"""
        field_re = re.compile(
            r'^(?:subject_definitions|summary|retention_analysis|detailed_description|'
            r'overall_soundscape|non_diegetic_music)\s*:', re.I)
        dispatch_re = re.compile(r'^===.*INSTRUCTION===', re.I)
        lines = (body or "").split("\n")
        out = []
        for ln in lines:
            s = ln.strip()
            if (field_re.match(s) or dispatch_re.match(s)) and out and out[-1].strip():
                out.append("")
            out.append(ln)
        return "\n".join(out)

    # ── LLM 调用 ──────────────────────────────────────────────

    @staticmethod
    def _run_llm(bus, system_prompt, user_msg, force_offload=False, seed=0):
        """按 BUS 里的后端配置调用 LLM，返回生成文本。"""
        api_config = bus.get("api_config")
        llama_model = bus.get("llama_model")
        parameters = bus.get("parameters")
        save_states = bus.get("save_states", False)

        # 优先沿用剧本处理器实际用的后端（use_api 标志）；缺失时回退到 llm_backend 字符串判断
        use_api = bus.get("use_api")
        if use_api is None:
            llm_backend = str(bus.get("llm_backend") or "").lower()
            use_api = "api" in llm_backend and bool(api_config)

        if use_api and api_config:
            return JZL_MiniMax_ScriptProcessor._call_api(api_config, system_prompt, user_msg)
        if not use_api and llama_model is not None:
            if not LLAMA_CPP_STORAGE.llm or LLAMA_CPP_STORAGE.current_config != llama_model:
                print("[JZL-llama] 开始加载模型...")
                LLAMA_CPP_STORAGE.load_model(llama_model)
            try:
                _params = parameters.copy() if parameters else {}
                _params.pop("present_penalty", None)
                _params.pop("state_uid", None)
                output = LLAMA_CPP_STORAGE.llm.create_chat_completion(
                    messages=[{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_msg}],
                    seed=seed, **_params)
                return output["choices"][0]["message"]["content"]
            except Exception as e:
                return f"[LLM 错误] {e}"
            finally:
                if force_offload:
                    LLAMA_CPP_STORAGE.clean()
                elif not save_states:
                    LLAMA_CPP_STORAGE.clean_state()
        return "[错误] BUS 缺少 llama_model 或 api_config"

    # ── 单块润色 ──────────────────────────────────────────────

    @classmethod
    def _build_user_msg(cls, lang, info, subject_defs, dispatch, original_dd, original_music, preference="", seg_index=1, seg_total=1, seam_runway=0.0):
        if lang == "en":
            labels = cls._SEGMENT_INFO_KEYS_EN
            head = "[Segment info]"
            tail = "Please output the rewritten detailed_description body."
        else:
            labels = cls._SEGMENT_INFO_KEYS
            head = "【本分段信息】"
            tail = "请输出重写后的 detailed_description 正文。"
        pos_lines = build_segment_position(lang, seg_index, seg_total, seam_runway=seam_runway)
        info_lines = "\n".join(f"{lab}: {info.get(k) or ''}" for lab, k in zip(labels, cls._SEGMENT_INFO_KEYS))
        pref_lines = ""
        if preference and str(preference).strip():
            pref_lines = f"\n\n【镜头语言偏好（最高优先，逐条执行：景别/运镜/切镜/转场/音乐/字数）】\n{preference}"
        music_lines = f"\n\n【原 non_diegetic_music 值（据此判断是否需补写音乐）】\n{original_music or '- 空'}"
        return (
            f"{pos_lines}\n\n"
            f"{head}\n{info_lines}\n"
            f"{pref_lines}\n\n"
            f"【subject_definitions（标签编号以此为准）】\n{subject_defs or '- 无'}\n\n"
            f"【调度指令（标签编号以此为准）】\n{dispatch or '- 无'}\n\n"
            f"【原 detailed_description（在此基础润色）】\n{original_dd}\n"
            f"{music_lines}\n\n"
            f"{tail}"
        )

    @classmethod
    def _enhance_block(cls, block, system_prompt, bus, lang, seed, preference="", seg_index=1, seg_total=1):
        """润色单块的 detailed_description，返回新块；失败返回 None（调用方保留原块）。"""
        h3_m = re.search(r'(===H3_PROMPT===\n)(.*?)(?====|\Z)', block, re.DOTALL)
        if not h3_m:
            return None
        marker = h3_m.group(1)
        h3_body = h3_m.group(2)

        dd_m = re.search(r'(?m)^detailed_description\s*:\s*', h3_body)
        if not dd_m:
            return None
        content_start = dd_m.end()
        tail = h3_body[content_start:]
        next_m = re.search(r'(?m)^[a-z_]+\s*:\s*', tail)
        content_end = content_start + (next_m.start() if next_m else len(tail))
        original_dd = h3_body[content_start:content_end].strip()
        if not original_dd:
            return None

        original_music = cls._extract_field(h3_body, "non_diegetic_music")
        subject_defs = cls._extract_field(h3_body, "subject_definitions")
        dispatch = cls._extract_dispatch(block)
        info = cls._extract_segment_info(block)
        _runway = 0.0
        try:
            _runway = max(0.0, float((bus or {}).get("seam_runway") or 0.0))
        except Exception:
            _runway = 0.0
        user_msg = cls._build_user_msg(lang, info, subject_defs, dispatch, original_dd, original_music, preference, seg_index, seg_total, seam_runway=_runway)

        new_dd = cls._run_llm(bus, system_prompt, user_msg, False, seed)
        new_dd = (new_dd or "").strip()
        if not new_dd or new_dd.startswith(("[API", "[LLM", "[错误", "[读取")):
            return None  # LLM 失败，保留原块

        # 分离音乐标记：正文结束后若有 [NON_DIEGETIC_MUSIC] 行 → 剥离并写回 non_diegetic_music 字段
        new_music = None
        music_m = re.search(r'(?m)^\[NON_DIEGETIC_MUSIC\]\s*(.+)$', new_dd)
        if music_m:
            new_music = music_m.group(1).strip()
            new_dd = new_dd[:music_m.start()].rstrip()

        # 保证 detailed_description 与前后字段之间各空一行（原用单换行会吞掉字段间空行）
        new_h3_body = h3_body[:content_start].rstrip() + "\n\n" + new_dd + "\n\n" + h3_body[content_end:]
        # 若模型按偏好补写了音乐 → 替换 non_diegetic_music 字段
        if new_music:
            nm_m = re.search(r'(?m)^non_diegetic_music\s*:\s*', new_h3_body)
            if nm_m:
                n_start = nm_m.end()
                n_tail = new_h3_body[n_start:]
                n_next = re.search(r'(?m)^[a-z_]+\s*:\s*', n_tail)
                n_end = n_start + (n_next.start() if n_next else len(n_tail))
                new_h3_body = new_h3_body[:n_start] + new_music + new_h3_body[n_end:]
        new_h3_body = cls._sync_retention_shots(new_h3_body, new_dd)
        # 兜底：规范六字段之间、以及 non_diegetic_music 与调度指令之间的空行
        new_h3_body = cls._normalize_h3_blank_lines(new_h3_body)
        return block[:h3_m.start()] + marker + new_h3_body + block[h3_m.end():]

    # ── 主执行 ────────────────────────────────────────────────

    def enhance(self, script_text, bus, force_offload, seed):
        if not isinstance(bus, dict):
            return ("[错误] 请从「剧本与镜头处理器」的 BUS 连线",)
        if not script_text or not script_text.strip():
            return ("[错误] 请从「剧本与镜头处理器」的「剧本输出」连线",)

        lang = bus.get("lang", "zh")
        story_style = bus.get("story_style", "")
        segment_duration = bus.get("segment_duration", 8)
        preference = bus.get("preference", "")
        custom_rules = bus.get("custom_rules", "")

        system_prompt = build_enhancer_prompt(lang, story_style, segment_duration, preference, custom_rules)

        # 打印模式日志（API / 本地），与剧本处理器保持一致
        use_api = bus.get("use_api")
        if use_api is None:
            _llm_backend = str(bus.get("llm_backend") or "").lower()
            use_api = "api" in _llm_backend and bool(bus.get("api_config"))
        print("[JZL-增强] 正在使用API模式增强提示词中..." if use_api else "[JZL-增强] 正在使用本地模式增强提示词中...")

        prefix, blocks = self._split_blocks(script_text)
        if not blocks:
            return ("[错误] 剧本输出里没有找到 [SHOT_START]...[SHOT_END] 分段块",)

        enhanced_blocks = []
        failed = 0
        total = len(blocks)
        for idx, block in enumerate(blocks, 1):
            new_block = self._enhance_block(block, system_prompt, bus, lang, seed, preference, idx, total)
            if new_block is None:
                failed += 1
                enhanced_blocks.append(block)
            else:
                enhanced_blocks.append(new_block)
            seg_label = self._CN_NUMS[idx - 1] if idx <= len(self._CN_NUMS) else str(idx)
            print(f"[JZL-增强] 第{seg_label}段完成...")

        # 全部增强完成后，按需卸载本地模型（避免每段重复加载/卸载）
        if force_offload:
            try:
                LLAMA_CPP_STORAGE.clean()
            except Exception:
                pass

        # 统计表（前缀）原样保留 + 增强后的分段块
        parts = [prefix] if prefix else []
        parts += [f"[SHOT_START]\n{b}\n[SHOT_END]" for b in enhanced_blocks]
        result = "\n\n".join(parts)

        if failed:
            result += f"\n\n[⚠️ 提示词增强] {failed} 个分段润色失败，已保留原 detailed_description。"
        return (result,)
