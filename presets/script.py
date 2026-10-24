"""
MiniMax-H3 剧本与镜头处理器 — System Prompt 零件库
====================================================
节点 JZL_MiniMax_ScriptProcessor 的后台零件库。

故事风格/镜头数量选项在此维护（fallback），正式设定词在 shed/ 目录。
"""

import re

# ═══════════════════════════════════════════════════════════════
#  零件库 2: {Story_Style} — 故事风格
# ═══════════════════════════════════════════════════════════════

STORY_STYLES = {
    "禁用风格": ("中性基线：不套用任何风格侧重，纯按剧情与镜头语言偏好分配篇幅（见 styles/00-禁用风格.md）。"),
    "热血战斗": ("动作重心：招式/对抗/打击反馈占最大篇幅，忠于用户剧情（见 styles/01-热血战斗.md）。"),
    "悬疑惊悚": ("悬念重心：信息差/伏笔/压迫感放大，忠于用户剧情（见 styles/02-悬疑惊悚.md）。"),
    "温馨日常": ("情感重心：微动作/表情/语气/留白为镜头中心，忠于用户剧情（见 styles/03-温馨日常.md）。"),
    "甜宠爽文": ("反差爽点重心：揭晓/围观反应/浪漫时刻占最多篇幅，忠于用户剧情（见 styles/04-甜宠爽文.md）。"),
    "宏大奇观": ("世界奇观重心：环境/设定细节与角色「看见」占最多篇幅，忠于用户剧情（见 styles/05-宏大奇观.md）。"),
    "乡土喜乐": ("喜剧重心：误会/夸张肢体/围观哄笑节奏欢快，忠于用户剧情（见 styles/06-乡土喜乐.md）。"),
    "歌神舞台": ("音乐节拍重心：表演/灯光/观众反应跟节拍，忠于用户剧情（见 styles/07-歌神舞台.md）。"),
}


# ═══════════════════════════════════════════════════════════════
#  零件库 3: {Shot_Count} — 故事长度选项
# ═══════════════════════════════════════════════════════════════

SEGMENT_COUNT_OPTIONS = {
    "4段": 4,
    "6段": 6,
    "9段": 9,
    "12段": 12,
    "16段": 16,
    "20段": 20,
    "24段": 24,
}


SEGMENT_COUNT_AUTO_LABEL = "剧情决定"
SEGMENT_COUNT_MAX = 56


def _resolve_segment_count(label):
    """分段数解析：标准标签（「6段」）→ SEGMENT_COUNT_OPTIONS；数字字符串/数字 → 精确整数；
    「剧情决定」→ None（由 LLM 按剧情时间线长度 ÷ 每段时长自动定段数）；其它 → 4。
    """
    if isinstance(label, str) and label.strip() in (SEGMENT_COUNT_AUTO_LABEL, "自动", "auto"):
        return None
    if isinstance(label, str) and label in SEGMENT_COUNT_OPTIONS:
        return SEGMENT_COUNT_OPTIONS[label]
    if isinstance(label, str) and label.strip().isdigit():
        return max(1, min(SEGMENT_COUNT_MAX, int(label.strip())))
    if isinstance(label, (int, float)):
        return max(1, min(SEGMENT_COUNT_MAX, int(label)))
    return 4


def _parse_duration_spec(duration):
    """时长设置解析：支持「固定N秒」/「A~B秒」，并兼容「N秒」/「Ns」/int/float/"5"/"5-5"/"4-10"/"4~10"。
    返回 (lo, hi, desc)。lo==hi 表示每段固定该秒；lo<hi 为区间（拆解时每段按剧情弧线在区间内取值，写进各段 **时长**）。"""
    s = str(duration if duration is not None else 8).strip()
    lo = hi = None
    # 区间优先（"4~10秒" / "4-10" / "4—10"），否则 "10秒" 会先命中单值分支
    m = re.search(r"(\d+(?:\.\d+)?)\s*[~\-—–－]\s*(\d+(?:\.\d+)?)", s)
    if m:
        lo = float(m.group(1)); hi = float(m.group(2))
    if lo is None:
        m = (re.search(r"固定\s*(\d+(?:\.\d+)?)\s*秒", s)
             or re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|s(?:ec(?:onds?)?)?)\b", s, re.IGNORECASE))
        if m:
            lo = hi = float(m.group(1))
    if lo is None:
        try:
            n = float(s); lo = n; hi = n
        except Exception:
            lo = 8.0; hi = 8.0
    lo = max(4.0, min(60.0, lo)); hi = max(lo, min(60.0, hi))
    if lo > hi:
        lo, hi = hi, lo
    if lo == hi:
        return (lo, hi, f"统一 {int(lo)} 秒")
    return (lo, hi, f"{int(lo)}~{int(hi)} 秒区间（每段由你按剧情弧线给实际秒数，写进该段 **时长** 字段）")


MODEL_MAX_SEGMENT_SEC = 60.0   # 单段生成上限（秒）——超过它「设定时长 + 段首衔接」无法完整生成


def _duration_spec_with_runway(duration, seam_runway=0.0):
    """时长设置 + 段首衔接跑道 → **(生成时长 lo, 生成时长 hi, desc)**。

    「无限时长」节点启用段间衔接时：段 2 起的生成时长 = 设定时长 + 跑道（跑道 = 段首承接上一段末帧的
    延续画面，成片会整段裁掉）⇒ 落盘时长仍 = 用户设定的时长。
    返回的 lo/hi 是**生成时长**（= 写进各段「**时长**」字段的那个值），desc 把两段式写法讲清楚。
    跑道 ≤ 0（未启用衔接）时行为与 _parse_duration_spec 完全一致。
    """
    lo, hi, _base_desc = _parse_duration_spec(duration)
    try:
        runway = float(seam_runway or 0.0)
    except Exception:
        runway = 0.0
    if runway <= 0.0:
        return lo, hi, _base_desc

    runway_txt = f"{runway:.2f}".rstrip("0").rstrip(".")
    gen_lo, gen_hi = lo + runway, hi + runway
    capped = gen_hi > MODEL_MAX_SEGMENT_SEC
    if capped:                       # 单段上限：生成不能超过 60 秒 → 落盘会略短于设定
        gen_hi = MODEL_MAX_SEGMENT_SEC
        gen_lo = min(gen_lo, gen_hi)
    gen_lo_txt = f"{gen_lo:.2f}".rstrip("0").rstrip(".")
    gen_hi_txt = f"{gen_hi:.2f}".rstrip("0").rstrip(".")
    if lo == hi:
        seg1_txt = f"{int(lo)}"
        segn_txt = gen_lo_txt
        head = (f"第 1 段 {int(lo)} 秒（正常开场，无段首衔接）；第 2 段起 {segn_txt} 秒"
                f"（= 落盘 {int(lo)} 秒 + 段首衔接 {runway_txt} 秒）")
    else:
        seg1_txt = f"{int(lo)}~{int(hi)}"
        segn_txt = gen_lo_txt if gen_lo == gen_hi else f"{gen_lo_txt}~{gen_hi_txt}"
        head = (f"第 1 段 {int(lo)}~{int(hi)} 秒（按剧情弧线分配，正常开场，无段首衔接）；"
                f"第 2 段起 {segn_txt} 秒（= 落盘 {int(lo)}~{int(hi)} 秒 + 段首衔接 {runway_txt} 秒）")
    tail = (f"。**段首衔接段**：第 2 段起，本段开头 {runway_txt} 秒必须承接上一段末帧画面"
            f"（同机位/同景别/同光线/同角色姿态与动作进行方向，动作可轻微延续），这段在成片里会被**整段裁掉**；"
            f"所以 ①第 2 段起的 [Shot 1] 就是这段延续画面，其后第一次切镜的时间戳约在 {runway_txt} 秒处，"
            f"本段第一条新内容也从这里开始；②每段「**时长**」字段写**实际生成秒数**"
            f"（第 1 段写 {seg1_txt}，第 2 段起写 {segn_txt}，允许 1 位小数）；"
            f"③成片落盘时长 = 生成时长 − {runway_txt} 秒 ≈ 你设定的时长。")
    if capped:
        tail += (f" ⚠️ 注意：设定时长 + 衔接 {runway_txt} 秒已超出单段上限 {MODEL_MAX_SEGMENT_SEC:g} 秒，"
                 f"本段按 {MODEL_MAX_SEGMENT_SEC:g} 秒生成，落盘会比设定略短约 {runway_txt} 秒。")
    return gen_lo, gen_hi, head + tail




# ═══════════════════════════════════════════════════════════════
#  V2 融合骨架（剧本与镜头处理器专用）
#  占位符: {Skill_Task} {Skill_Rules} {Story_Style} {Segment_Count} {Timing_Plan}
#          {Reference_Intro} {H3_Shot_Rules} {User_Story}
#  分层（优先级从低到高）：
#    ①强化层 {Skill_Task} + {Skill_Rules} ← 内置或自定义 skill
#    ②风格层 {Story_Style}
#    ③偏好层 {Preference_Block} + {Custom_Rules_Block}
#    ④强制层 {H3_Shot_Rules}（永远最后，skill 不可覆盖）
# ═══════════════════════════════════════════════════════════════

SCRIPT_SKELETON_V2 = '''# Role: 顶级短视频编剧 & MiniMax H3 分段提示词工程师
你是专业的短视频编剧和 MiniMax H3 视频提示词撰写专家。你的任务分两步：
第一步：把用户故事按「情节」拆解为 {Segment_Count} 个分段（段数规则见 B「分段数」）——每个分段是一段独立视频片段（时长见 B「限制框架」；区间设置时按剧情弧线给每段实际秒数，写进该段 **时长** 字段）。
第二步：把每个分段当成一段独立视频去润色，写出一条可直接送入 MiniMax H3 模型生成视频的完整提示词（六段 Ref2VA + 调度指令，格式见 D）。

{Duration_Hard_Rule}

这是你的工作契约：A = 你拿到的材料（原材料，用于理解，不得修改）；B = 必须遵守的限制框架（硬约束）；C = 执行与审查流程（严格顺序）；D = 输出格式权威（严格遵循）。

## 语言设定（CRITICAL — 正文语言跟随用户所选输出语言；字段名/标记保持英文原样）
- 用户 LLM 输出语言为「中文 [ZH]」：六段正文（subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music 的内容）用**中文**写作；为「英文 [EN]」：正文用**英文**。字段名（subject_definitions 等）与关系标记（fully_preserved / fully_copy / reference / weak_reference 等）始终**英文原样**。
- 仅 <d> 内台词/歌词/旁白按用户原语言（如 <d>[中文] 原文。</d>）、画面可见文字（横幅/招牌/字幕等）用英文双引号包裹原文字，保留原语言。
- 下方示例仅示意结构与写法；实际正文语言按「用户所选输出语言」。

## A. 你拿到的材料（原材料，用于理解与取用，不得修改）
【用户故事】
{User_Story}

【所选故事风格】（作为导演视角与镜头语言来源；从中取用该风格的「视觉风格 / 色调与光线 / 摄影语言 / 核心导演语法」）
{Story_Style}
{User_Tags}

【参考素材标签对照表】（slots 唯一来源；白名单/槽位名规则见 D 与下方拆解规则）
{Reference_Intro}

## B. 必须遵守的限制框架（硬约束；逐条核对，违反即失败）
【任务】{Skill_Task}
【分段数】{Segment_Count_Spec}

【镜头语言偏好（景别/运镜/切镜/转场/音乐/字数，逐条执行）】
{Preference_Block}

【自定义润色规范（用户自定义规则，与偏好同等强制，逐条执行；无则忽略本块）】
{Custom_Rules_Block}

【全局节奏统筹（先按弧线给每段定实际秒数与 [Shot N] 数）】
{Timing_Plan}

## C. 执行与审查流程（严格按顺序，一次指令内完成）
第 1 步 · 理解：通读 A（故事/风格/素材对照表），识别剧情弧线、人物与台词、本批可调用素材及其「类型:槽位名」。
第 2 步 · 规划：按 B 的统筹与偏好，给每段定实际秒数与 [Shot N] 数（按弧线分配，开场少/中段推进/高潮多/收束回落）；**同时先把「[视觉锚点表]」与「[状态账本]」定下来**（跨段非素材元素的固定规格 + 每个元素逐段的状态演进链，见 D 段 §4.0 / §4.0b）。
第 3 步 · 执行：逐段输出 [SHOT_START]...[SHOT_END]，每段含（一）分段信息 +（二）六段 Ref2VA + 调度指令（格式见 D）。执行时遵守：
{Skill_Rules}
   - {Seam_Edge_Rule}
第 4 步 · 审查（写完全部后自检一遍，不满足即改）：
   - 六段字段齐全、字段间各空一行、无 markdown 代码块；
   - <Subject N>/<Picture N>/<Video N>/<Audio N> 与 subject_definitions 一致，且与 SCENE/VIDEO/AUDIO_INSTRUCTION.slots 编号严格同源（slots[0]=<Picture 1>…）；
   - 角色/场景/道具 三字段只用素材声明的元素；slots 只写「类型:槽位名」，严禁自造/缩写/用素材名当槽位名；
   - 台词原样在 <d>（保留原语言）；无脑补台词/内心独白/语气词；
   - 每段开头 **1 秒**内无任何 <d> 台词（第一句台词最早时间 ≥ 1 秒）；镜内台词时长与镜头时长匹配（按 4~5 字/秒 估算，放不下则拉长该镜或拆句 <scenetrans>）；
   - 每个 [Shot N] 时间戳落在该段时长内、[Shot 1] 无时间戳；
   - 每个 [Shot N] 都写明该镜景别+运镜，且段内不同 [Shot N] 用**不同景别+不同运镜**混合（长视频 4-6 种递进、短视频 2-3 种，随剧情推进变换），严禁整段只复用一种景别或一种运镜；
   - 六段正文语言 = 用户所选输出语言（中文[ZH]=中文、英文[EN]=英文；字段名/标记保持英文），仅 <d>/画面文字保留原语言；
   - 镜头语言偏好（景别/运镜/切镜/转场/音乐/字数）与自定义润色规范已逐条落实（景别档位、切镜数、转场、字数等均与偏好一致）；
   - retention_analysis 程度标记正确、破折号后列举已定义特征（无「保留」二字）；
   - **三段复述一致**：subject_definitions（定义特征清单）→ detailed_description（<Subject N> **本块内首次出现的那一镜**复述同一清单，且**只复述一次**，后续 [Shot N] 只写标签+动作）→ retention_analysis（再列举）三处颜色/材质/数量一致，正文无未定义的新外貌/道具/配色；
   - 🔖 **视觉锚点表**：第一个 [SHOT_START] 之前已输出 `[视觉锚点表]`；表内每个元素在本段出现时都已**逐字复述**规格短语（无同义改写）；本段没有的元素没有凭空写成还在；
   - 🧾 **状态账本 + 状态交接**：第一个 [SHOT_START] 之前已输出 `[状态账本]`；本段 `**状态交接**` 逐元素写了「上段末态 → 本段末态」，且与账本该段一致（道具位置/朝向/完好度、角色服装破损/伤口血迹、特效出现-持续-结束、场景破坏累积）；
   - 🚫 **无因变化四禁**：本段没有无因消失、无因复位、无因换位换手、无因形变（[Shot 1] 已落继承态、段末已落末态）；
   - 🔗 **段间继承**：第 2 段起 [Shot 1] 无"重开场"（没再写全景+环境全貌），已写明继承的地标方位/光位时段/地形痕迹/在场人物位置；
   - 🎯 **资产调度完备**：本段画面里能看见的已声明角色/道具（含被注视、在场、背景指代、被操作的对象）全部进了 **角色/**道具** 字段与 slots，正文与 slots 一一对应（多列或漏列都算不合格）；
   - **风格逐句渗透**：风格声明只用一句（句式「本段为 <媒介/渲染> 的 <风格> 风格」），且每个环境/特效/道具名词都带本风格定语；
   - **镜内分拍 + 句间因果**：一镜多拍已用空行分拍（每拍「触发→动作→结果」），每句新动作都由上一句因果引发；
   - **副运动 + 可见事实密度**：每次主体动作带至少 1 处附属物副运动（行囊/衣摆/发丝/道具/烟雾/尘土）；每个 [Shot N] 平均每秒 ≥ 2 个可拍到的事实；
   - **固定 ref2va（无首帧/尾帧锚定）**：subject_definitions 里参考图只写「普通参考」，严禁 `<Picture N> is the first frame of [Shot 1]` / `is the last frame of`；summary 严禁 `keyframe completion`；retention 严禁 `([Shot 1] first frame)`；
   - **声音两字段**：overall_soundscape 按事件时间顺序写（主导事件声→环境底噪，带「材质+动作+强度」）；non_diegetic_music 默认 N/A，指定音乐时写「体裁/速度 + 三件乐器（含演奏法）+ matching 画面事件」；
   - {Seam_Check_Rule}

## D. 输出格式权威（六段 Ref2VA + 调度指令，严格遵循）
{H3_Shot_Rules}'''


def _build_schedule_rules(lang, enable_scene, enable_props, enable_video, enable_audio):
    """根据 4 个调度开关生成约束文本（注入 H3_Shot_Rules 的 {Schedule_Rules} 占位符）。"""
    if lang == "zh":
        lines = ["### 4.5 调度开关约束（必须遵守）"]
        if enable_scene:
            lines.append("- 场景调度已启用：SCENE_INSTRUCTION 的 slots 中包含「场景:槽位名」元素（槽位名与素材节点标题一致），提示词用对应的 <Picture N> 引用背景图。")
        else:
            lines.append("- 场景调度已关闭：分段信息「**场景**」写「无」，slots 不包含「场景:」元素，提示词用纯文本描述环境、不写背景参考标签。")
        if enable_props:
            lines.append("- 道具调度已启用：SCENE_INSTRUCTION 的 slots 中包含「道具:槽位名」元素，提示词用对应的 <Picture N> 引用道具图。")
        else:
            lines.append("- 道具调度已关闭：分段信息「**道具**」写「无」，slots 不包含「道具:」元素，提示词用纯文本描述道具、不写道具参考标签。")
        if enable_video:
            lines.append("- 视频调度已启用：每个镜头输出 ===VIDEO_INSTRUCTION=== 段，slots 中含「视频:槽位名」元素，提示词用 <Video N> 引用。")
        else:
            lines.append("- 视频调度已关闭：不输出 ===VIDEO_INSTRUCTION=== 段，不写 <Video N> 标签，动作/运镜直接用纯文本描述。")
        if enable_audio:
            lines.append("- 音频调度已启用：每个镜头输出 ===AUDIO_INSTRUCTION=== 段，slots 中含「音频:槽位名」元素，提示词用 <Audio N> 引用。")
        else:
            lines.append("- 音频调度已关闭：不输出 ===AUDIO_INSTRUCTION=== 段，不写 <Audio N> 标签，对话直接用 (S1)/(S2) 纯文本描述。")
    else:
        lines = ["### 4.5 Scheduling Toggle Rules (MUST follow)"]
        if enable_scene:
            lines.append("- Scene scheduling ENABLED: SCENE_INSTRUCTION slots include \"scene:<slotName>\" elements (slot name matches the material node title); reference the background via the matching <Picture N>.")
        else:
            lines.append("- Scene scheduling DISABLED: write \"none\" in the **Scene** field; slots contain no \"scene:\" element; describe the environment in plain text without any background label.")
        if enable_props:
            lines.append("- Prop scheduling ENABLED: SCENE_INSTRUCTION slots include \"prop:<slotName>\" elements; reference props via matching <Picture N>.")
        else:
            lines.append("- Prop scheduling DISABLED: write \"none\" in the **Props** field; slots contain no \"prop:\" element; describe props in plain text without any prop label.")
        if enable_video:
            lines.append("- Video scheduling ENABLED: output ===VIDEO_INSTRUCTION=== with \"video:<slotName>\" slots; use <Video N> in the prompt.")
        else:
            lines.append("- Video scheduling DISABLED: do NOT output ===VIDEO_INSTRUCTION=== nor use <Video N>; describe action/camera in plain text.")
        if enable_audio:
            lines.append("- Audio scheduling ENABLED: output ===AUDIO_INSTRUCTION=== with \"audio:<slotName>\" slots; use <Audio N> in the prompt.")
        else:
            lines.append("- Audio scheduling DISABLED: do NOT output ===AUDIO_INSTRUCTION=== nor use <Audio N>; describe dialogue with (S1)/(S2) in plain text.")
    return "\n".join(lines)


def _extract_style_directing(style_text):
    """提取风格设定里的「核心导演语法」段落（写动作时必须逐条执行的部分）。"""
    if not style_text:
        return ""
    m = re.search(r'## 核心导演语法(.*?)(?=(?<!#)## )', style_text, re.DOTALL)
    if not m:
        return ""
    return ("## 核心导演语法" + m.group(1)).rstrip()


def _extract_detail_length(preference_text, duration_lo=None, duration_hi=None):
    """从偏好设定词里提取「详细描述字数」的数字范围（如 800-1200），默认 350-500。

    兼容中英双语偏好文本：中文「详细描述字数：…(350-500字)」/ 英文「Detailed-description length: 350-500 words」。

    duration_lo/duration_hi 给出且为**固定时长**时按「每秒字数」缩放上限（基准：8 秒 ≈ 350-500 字，
    即下限 ≈44 字/秒、上限 ≈70 字/秒），避免"5 秒段也要写满 500 字"把镜头数顶爆、内容排到时长之外。
    区间时长不缩放（模型会按每段实际秒数自行分配）。
    """
    lo, hi = 350, 500
    text = preference_text or ""
    m = re.search(r'详细描述字数[:：][^\n]*\((\d+)-(\d+)\s*[字词]', text)
    if not m:
        m = re.search(r'Detailed-description length:[^\n]*?(\d+)-(\d+)\s*words', text, re.IGNORECASE)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))

    try:
        dlo = float(duration_lo)
        dhi = float(duration_hi)
    except (TypeError, ValueError):
        return f"{lo}-{hi}"
    if abs(dhi - dlo) > 1e-6:          # 区间时长：不缩放
        return f"{lo}-{hi}"

    cap_lo = max(100, int(round(dlo * 44)))
    cap_hi = max(120, int(round(dhi * 70)))
    lo2, hi2 = min(lo, cap_lo), min(hi, cap_hi)
    if hi2 <= lo2:
        hi2 = lo2 + max(50, int(round(dhi * 20)))
    return f"{lo2}-{hi2}"


def _build_material_table(ref_image_intro, ref_video_intro, ref_audio_intro):
    """解析用户素材描述，生成「标签对照表」，供 LLM 严格按槽位名写 slots。

    用户描述每行格式：「类型+字母 = 素材名（描述）」，如「角色A = 孙悟空（橙色武道服）」
    「音色A = 小兔子音色」。音频子分类（角色音色/背景配乐/念白）各有独立前缀，
    不在白名单里会被整行丢弃 → 对照表缺音频槽位 → LLM 只能把 AUDIO_INSTRUCTION 写空。
    """
    blocks = []
    for kind, intro in (("图片", ref_image_intro), ("视频", ref_video_intro), ("音频", ref_audio_intro)):
        if not intro or not intro.strip():
            continue
        rows = []
        for line in str(intro).splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r'^(角色|场景|道具|视频|音频|音色|配乐|念白|分镜|音效|音乐|其他)\s*([A-Za-z])\s*[=＝:：]\s*(.+)', line)
            if not m:
                continue
            typ, slot, rest = m.group(1), m.group(2).upper(), m.group(3).strip()
            if not rest:
                continue
            rows.append(f"- {typ}{slot} = {rest}")
        if rows:
            blocks.append(f"【{kind}】\n" + "\n".join(rows))
    if not blocks:
        return ""
    return ("## 3.5 素材标签对照表（CRITICAL — 唯一素材来源，slots 只能从这里取「类型:槽位名」）\n"
            + "\n".join(blocks)
            + "\n\n## 3.6 素材白名单（CRITICAL — 顶部字段与调度严禁引用对照表之外的素材）\n"
            "- 每段顶部元数据（**角色**/**场景**/**道具**/**视频**/**音频**/**音效**/**音乐**/**其他**）里列出的名称，必须全部来自上方对照表中的「素材名」或「类型:槽位名」（如 场景:场景B、角色:角色A），严禁自造或新增。\n"
            "- 剧情需要但对照表中没有的素材（如某道具/场景/角色），一律不得写入顶部字段或调度 slots：该字段写「无」，只允许在 detailed_description 剧情正文中作环境描写。\n"
            "- 调度指令 slots 写「类型:槽位名」，如 场景:场景A、角色:角色A、道具:道具A、视频:视频A、音频:音色A（音频类类型前缀固定「音频」，槽位名按子分类可能是 音色A/配乐A/念白A/音频A），严禁写素材名（孙悟空）或自造槽位。\n"
            "- subject_definitions 写素材名（<Subject N> 是 <Picture N> 中的孙悟空），括号里的外貌描述原样复述。\n"
            "- 音频槽位对应说话人音色：本段谁开口说话，AUDIO_INSTRUCTION.slots 就写对应槽位（角色音色→「音频:音色X」、其它音频→「音频:配乐X/念白X/音频X」，一律照抄对照表里的槽位名），无对话分段音频 slots 写空。")

def build_shot_prompt(
    user_story: str,
    mode: str = "拆解模式 (Decompose)",
    story_style: str = "热血战斗",
    segment_count_label: str = "4段",
    lang: str = "zh",
    segment_duration: int = 8,
    ref_image_intro: str = "",
    ref_video_intro: str = "",
    ref_audio_intro: str = "",
    enable_scene: bool = True,
    enable_props: bool = True,
    enable_video: bool = True,
    enable_audio: bool = True,
    user_tags: str = "",
    preference: str = "",
    custom_rules: str = "",
    seam_runway: float = 0.0,
    skill_task: str = "",
    skill_rules: str = "",
) -> str:
    """剧本与镜头处理器 — 拼装一次成型的 System Prompt（融合拆解 + 六段 Ref2VA 规范）。

    lang: "zh" / "en"；segment_duration: 每段视频时长(秒)，约束时间戳范围。
    seam_runway: 「无限时长」节点的段首衔接跑道（秒）。> 0 时段 2+ 的生成时长 = 设定时长 + 跑道，
    段首跑道承接上一段末帧、成片裁掉 ⇒ 落盘时长 = 设定时长（详见 _duration_spec_with_runway）。
    skill_task / skill_rules: 「强化层」设定词（内置 skill 或用户在面板选的自定义 skill）。
    两者为空时使用内置 skill；**无论 skill 怎么换，D 段输出格式权威都不受影响**。
    """
    from ..sheding.mode_instructions import build_mode_instruction as _mode_instr
    from ..sheding.story_styles import resolve_style as _resolve_style
    from ..sheding.decompose_rules import build_decompose_rules as _decomp_rules
    from ..sheding.h3_shot_rules import H3_SHOT_RULES_ZH, H3_SHOT_RULES_EN
    from . import prompt_texts_en as _en

    is_en = str(lang or "").lower().startswith("en")
    # 内置 skill 按语言取段（中文 / 英文 / 单语文件原样）；自定义 skill 已在调用方按语言取好
    mode_instruction = (skill_task or "").strip() or _mode_instr(mode, lang)
    decompose_rules = (skill_rules or "").strip() or _decomp_rules(lang)
    # 风格：英文模式优先取 skills/style/en/ 的译文（缺失自动回退中文）
    style = _resolve_style(story_style, lang)
    segment_count = _resolve_segment_count(segment_count_label)   # None = 「剧情决定」（按剧情时间线自动定段数）
    if is_en and segment_count is None:
        seg_count_txt = "several"
        seg_count_ref = "all"
        seg_count_spec = _en.SEG_COUNT_SPEC_AUTO_EN
    elif is_en:
        seg_count_txt = str(segment_count)
        seg_count_ref = str(segment_count)
        seg_count_spec = _en.seg_count_spec_fixed_en(segment_count)
    elif segment_count is None:
        seg_count_txt = "若干"
        seg_count_ref = "全部"
        seg_count_spec = (
            "**剧情决定（段数由你按剧情时间线长度自动计算）**：\n"
            "  - 只按**剧情发展的时间线长度**估算整部戏需要演绎的总时长（人物动作/事件推进/对白所需时间之和，**绝不是字数**）；\n"
            "  - 段数 = 剧情总时长 ÷ 每段时长（四舍五入取整）；**区间时长时优先用足每段时长（取区间上限）**，宁少段、勿多段；\n"
            "  - 硬约束：段数 ≥ 1 且 ≤ 56；且每段实际时长不得低于区间下限（或固定时长）——**剧情很短时宁可用 1 段长镜，"
            "绝不允许切成很多短段**（防止短剧情生成超多视频）；\n"
            "  - 示例：剧情约 120 秒 + 时长区间 4~60 秒 ⇒ 2 段 × 60 秒（**不是** 60 段 × 4 秒）；剧情约 8 秒 ⇒ 1 段；\n"
            "  - 切分点必须落在剧情时间线的自然节点（事件/场景/情绪转折）上，不得为凑段数硬切；\n"
            "  - 每段是一段独立视频片段；**每段的实际生成秒数见下方「时长设定」**，必须写进该段 **时长** 字段。"
        )
    else:
        seg_count_txt = str(segment_count)
        seg_count_ref = str(segment_count)
        seg_count_spec = (f"恰好生成 {segment_count} 个分段；每段是一段独立视频片段"
                          f"（**每段的实际生成秒数见下方「时长设定」**，必须写进该段 **时长** 字段）。")
    try:
        seam_runway = max(0.0, float(seam_runway or 0.0))
    except Exception:
        seam_runway = 0.0
    duration_lo, duration_hi, duration_desc = _duration_spec_with_runway(segment_duration, seam_runway)
    seam_runway_txt = ((f"{seam_runway:.2f}".rstrip("0").rstrip(".") + " 秒"
                        f"（≈{int(round(seam_runway * 24))} 帧）") if seam_runway > 0 else "0 秒")
    if is_en:
        seam_runway_txt = ((f"{seam_runway:.2f}".rstrip("0").rstrip('.') + " s"
                            f" (≈{int(round(seam_runway * 24))} frames)") if seam_runway > 0 else "0 s")
        duration_desc = _en.duration_desc_en(duration_lo, duration_hi, seam_runway, seam_runway_txt)

    # ── BUG① 修复：seam=0 时不得注入「延续段 / 从 0 秒起算」等无效指令 ──
    #   衔接关闭 / 单段 / 不裁切诊断档：统一改用「大切镜/换场」文案。
    _seam_on = seam_runway > 0

    # 详细描述字数：固定时长时按「每秒字数」缩放（8 秒 ≈ 350-500 字为基准）。
    # 不缩放的话，「5 秒段必须写满 350-500 字」会逼着 LLM 硬加镜头，内容必然排到时长之外。
    detail_length_txt = _extract_detail_length(preference, duration_lo, duration_hi)

    # ⏱️ 时长硬预算：由时长直接推出「镜头数上限 / 最晚时间戳 / 建议镜数」，写进提示词最有约束力的位置。
    #    只写「时间戳落在本段时长内」这种没有数字的软约束时，LLM 会把内容排到远超时长的地方
    #    （真实案例：5 秒的分段写了 10 镜、时间戳排到 18.5 秒）。
    _dur_cap_lo, _dur_cap_hi = int(duration_lo), int(duration_hi)
    _shot_cap = max(1, int(round(_dur_cap_hi / 0.8)))                  # 每镜最短 0.8 秒
    _shot_sug_lo = max(2, int(_dur_cap_lo // 4))                       # 一镜 2~4 秒的常规节奏
    _shot_sug_hi = max(_shot_sug_lo, int(-(-_dur_cap_hi // 2)))        # ceil(dur/2)
    _last_ts = round(max(0.0, _dur_cap_hi - 0.8), 1)                   # 最后一镜的时间戳上限
    duration_seconds = (f"{_dur_cap_lo}" if _dur_cap_lo == _dur_cap_hi else f"{_dur_cap_lo}~{_dur_cap_hi}")
    if is_en:
        _vc = ("the new content of a segment (segment 2+ also carries the seam hand-off seconds)"
               if _seam_on else "each segment")
        duration_budget = (
            f"- HARD duration budget (overshooting = FAIL): {_vc} lasts {duration_seconds} seconds ⇒ "
            f"a good rhythm is {_shot_sug_lo}-{_shot_sug_hi} shots (2-4 s each), hard cap **{_shot_cap}** [Shot N]; "
            f"the LAST [Shot N] timestamp must be ≤ **{_last_ts} s** and every timestamp must be strictly less than "
            f"the segment duration (if §1.5 fixes a cut count, that count wins — the timestamps still have to fit). "
            f"If the content does not fit, CUT shots and compress the action — never place content beyond the "
            f"duration, and never inflate the shot count to fill words.")
    else:
        _vc = ("本段新内容（第 2 段起另含段首衔接秒数）" if _seam_on else "本段")
        duration_budget = (
            f"- ⏱️ 时长硬预算（越界即不合格）：{_vc} 时长 {duration_seconds} 秒 ⇒ "
            f"一镜 2~4 秒的常规节奏是 {_shot_sug_lo}~{_shot_sug_hi} 镜，**硬上限 {_shot_cap} 个 [Shot N]**；"
            f"最后一个 [Shot N] 的时间戳必须 ≤ **{_last_ts} 秒**，所有时间戳一律小于本段时长"
            f"（§1.5 若明确指定了切镜数，以 §1.5 为准，但时间戳仍必须落在时长内）。"
            f"内容放不下就**减镜头、压缩动作**，绝不把内容排到时长之外，也绝不为凑字数加镜头。")

    # 时长铁律放在提示词最顶部（模型对开头最敏感），和 B 段的硬预算一起形成双重约束
    if is_en:
        duration_hard_rule = (
            f"⚠️ DURATION IRON RULE: every segment is **{duration_seconds} seconds** long (hard budget in B "
            f"\"Global rhythm plan\"). A segment's detailed_description may only contain what can actually play in "
            f"that time — **any [Shot N] timestamp ≥ the segment duration makes the segment invalid**. If it does not "
            f"fit, cut shots and compress the action; never add shots and never run past the duration.")
    else:
        duration_hard_rule = (
            f"⚠️ 时长铁律：每段视频就是 **{duration_seconds} 秒**（硬预算见 B「全局节奏统筹」）。"
            f"分段的 detailed_description 只能写这段时间里真演得完的内容——**任何 [Shot N] 时间戳 ≥ 本段时长，"
            f"该分段即不合格**。放不下就减镜头、压动作，绝不许加镜头、绝不许超时。")

    # 骨架（SCRIPT_SKELETON_V2）与 DECOMPOSE_RULES 始终为中文，故这两套变体与 lang 无关
    if _seam_on:
        seam_edge_rule = (
            "段边界 = 大切镜 + **段首衔接**（见上方时长设定）：第 2 段起的 [Shot 1] 是「承接上一段末帧画面的延续段」，"
            f"持续 {seam_runway_txt}（同机位/同景别/同光线/同角色姿态与动作方向）——这段在成片里会被整段裁掉，所以本段"
            f"新内容从 {seam_runway_txt} 之后开始（第一次切镜时间戳约在 {seam_runway_txt} 处，可换景别/机位/空间）；"
            "人物/物理状态跨段连续（禁回退/重演上段已演事件）；段尾（非末段）用 1-2 句写清物理末态作剧情交接。")
        seam_check_rule = (
            "第 2 段起 [Shot 1] 是承接上段末帧的延续画面（时长 = 段首衔接秒数）、其后才有本段新内容；"
            "无状态回退/重演上段已演事件。")
        seam_decompose_bullet = (
            "段首承接 + 大切镜：整片是分段生成再拼接（相邻段之间有物理衔接）。第 2 段起，段首要写一个「承接上一段"
            f"末帧画面的延续段」（同机位/同景别/同光线/同角色姿态与动作方向，持续 {seam_runway_txt}）；这段在成片里"
            f"会被整段裁掉 → 本段新内容从 {seam_runway_txt} 之后开始。同时保证人物物理状态跨段连续（上段末的姿态/"
            "位置/已发现事实必须延续），禁止状态回退与重演上段已演事件；段尾（非末段）交代清楚物理末态作剧情交接。")
        seam_principle_2 = (
            "段首承接 + 剧情状态连续: 段与段之间在成片里是连续的（下段开头钉住了上段末帧）。关键在于“剧情/物理状态"
            "必须连续”：上段结束时谁在什么位置、什么姿态、是否受伤、发现了什么、做了什么，下段的**段首延续段**必须"
            "与之完全一致（同机位/同景别/同光线/同姿态），本段新内容则必须基于该状态继续（可换机位、可从不同景别"
            "呈现同一状态），严禁状态回退（上段已站起/已发现/已下台阶，下段不得又蹲回起点/装作没发现/再踏上同一级"
            "台阶）、严禁重演上段已演过的事件（同一动作只演一次）。若中途切去别的空间、之后再切回某场景，回到该场景"
            "时的机位/景别/光线语言要与该场景此前的镜头一致（保持原有镜头语言）。")
        seam_self_contained = (
            f"**段首衔接**：第 2 段起的开头 {seam_runway_txt} 是「承接上一段末帧的延续段」（同机位/同景别/同光线/"
            "同角色姿态与动作方向，只轻微延续）——这段成片会裁掉，所以要写具体（不能写“承接上段末帧/上段…继续”这类"
            "抽象伪引用），本段新内容写在它之后；第 1 段正常开场。")
        seam_scheme_a = (
            f"段首承接(方案 A, 成片裁掉): 第 2 段起, [Shot 1] 必须写成\"上一段末帧画面的延续\"——同机位/同景别/"
            f"同光线/同角色身体姿态与动作进行方向, 动作只轻微延续, 持续 {seam_runway_txt}, 并把上段末态的具体画面"
            f"写出来(可仅微动); 这段在成片里会被整段裁掉, 其后才切镜进入本段新内容(第一次切镜时间戳 ≈ {seam_runway_txt})。")
        seam_no_pseudo_ref = (
            "禁抽象伪引用: 不要写\"承接上段末帧/接住上段结尾/上段继续\"这类抽象词——延续段要写具体画面内容(谁在哪/"
            "什么姿态/什么动作进行到哪一步/光线环境如何), 因为下段开头真的会被钉住上段末帧。")
        seam_ban_pseudo_ref = (
            "禁止抽象跨段画面词(不得写“承接上段末帧/上段…继续/接着刚才”这类抽象伪画面词): 段首延续段要**写具体画面**"
            "(谁在哪/什么姿态/什么动作进行到哪一步/光线环境如何), 因为下段开头真的被钉住上段末帧; 新内容则本段自足建立。")
    else:
        seam_edge_rule = (
            "段边界 = 大切镜/换场：第 2 段起的 [Shot 1] 是紧接上段剧情时间线的下一镜（可换景别/机位/空间），画面自足，"
            "禁止写“承接上段末帧/上段…继续”这类伪画面引用；人物/物理状态跨段连续（禁回退/重演上段已演事件）；"
            "段尾（非末段）用 1-2 句写清物理末态作剧情交接。")
        seam_check_rule = (
            "第 2 段起 [Shot 1] 是紧接上段剧情时间线的下一镜（大切镜/换场，画面自足，禁止伪画面引用）、"
            "其后正常推进本段新内容；无状态回退/重演上段已演事件。")
        seam_decompose_bullet = (
            "段首承接 + 大切镜：整片是分段生成再拼接（相邻段之间是一次大切镜/换场）。第 2 段起，段首 [Shot 1] 直接写"
            "紧接上段剧情时间线的下一镜（可换景别/机位/空间），画面自足，禁止写“承接上段末帧/上段…继续”等伪画面"
            "引用；同时保证人物物理状态跨段连续（上段末的姿态/位置/已发现事实必须延续），禁止状态回退与重演上段已演"
            "事件；段尾（非末段）交代清楚物理末态作剧情交接。")
        seam_principle_2 = (
            "段际承接 + 剧情状态连续: 段与段之间按大切镜/换场处理。关键在于“剧情/物理状态必须连续”：上段结束时谁在"
            "什么位置、什么姿态、是否受伤、发现了什么、做了什么，下段的开场画面必须基于该状态继续（可换机位、可从"
            "不同景别呈现同一状态），严禁状态回退（上段已站起/已发现/已下台阶，下段不得又蹲回起点/装作没发现/再踏上"
            "同一级台阶）、严禁重演上段已演过的事件（同一动作只演一次）。若中途切去别的空间、之后再切回某场景，回到"
            "该场景时的机位/景别/光线语言要与该场景此前的镜头一致（保持原有镜头语言）。")
        seam_self_contained = (
            "**段际衔接**：段与段之间按大切镜/换场处理，第 2 段起直接写紧接上段剧情时间线的下一镜（可换景别/机位/"
            "空间），画面自足，禁止写“承接上段末帧/上段…继续”这类伪画面引用；第 1 段按正常开场。")
        seam_scheme_a = (
            "段间大切镜: 第 2 段起, [Shot 1] 直接写紧接上段剧情时间线的下一镜（可换景别/机位/空间），画面自足, "
            "禁止写“承接上段末帧/上段…继续”这类伪画面词。")
        seam_no_pseudo_ref = (
            "禁抽象伪引用: 不要写\"承接上段末帧/接住上段结尾/上段继续\"这类抽象词——每段开场直接写具体画面内容"
            "(谁在哪/什么姿态/什么动作进行到哪一步/光线环境如何)。")
        seam_ban_pseudo_ref = (
            "禁止抽象跨段画面词(不得写“承接上段末帧/上段…继续/接着刚才”这类抽象伪画面词): 每段开场要**写具体画面**"
            "(谁在哪/什么姿态/什么动作进行到哪一步/光线环境如何), 新内容本段自足建立。")

    if lang == "zh":
        if _seam_on:
            seam_duration_note = (
                "写本段**实际生成**秒数：第 1 段 = 设定时长（全局固定就写那个值、区间就按弧线取值）；"
                f"第 2 段起 = 设定时长 + 段首衔接 {seam_runway_txt}（即上方时长设定给出的那个值，允许 1 位小数）")
            seam_handoff_rule = (
                f"- - 段首承接 + 剧情状态连续（CRITICAL — 本次任务一次输出 {seg_count_txt} 个 Video_XXX 分段时适用；"
                "单独生成单个分段忽略）：整片是**分段连续生成再拼接**的，段与段之间有物理画面衔接（下一段的开头会被"
                "钉住上一段的末帧，因此段首必须写成衔接画面）。①非首段：本段 [Shot 1] 就是「承接上一段末帧画面的"
                "延续段」——与上段末帧**同机位/同景别/同光线/同角色身体姿态与动作进行方向**，动作只做轻微延续"
                f"（不要重新站位、不要重新起势），持续 {seam_runway_txt}；这一段在成片里会被**整段裁掉**，所以本段"
                f"新内容从 {seam_runway_txt} 之后开始（第一次切镜 ≈ 在 {seam_runway_txt} 处，可换景别/机位/空间）。"
                "②首段没有延续段，按正常开场写。③剧情状态必须跨段连续：上段结束时的剧情状态（谁在哪/姿态/已发现"
                "什么/刚发生什么）就是延续段的画面内容；禁止重演上段已演的事件、禁止状态回退（上段已站起/已发现/"
                "已下台阶，本段不得又蹲回/装作没发现/再踏上同一级台阶）。④非末段：本段最后一个 [Shot N] 用 1-2 句"
                "写清本段结束时的物理末态（谁/在哪/姿态/正进行动作），作剧情状态交接；禁止用对视/一笑/定格/转身等"
                "封闭收尾给中段画句号。⑤只有首段可完整开场、只有末段可真正收束。⑥切去别的空间再切回某场景时，"
                "保持该场景此前的机位/景别/光线语言一致。⑦时间戳：延续段也占时间（[Shot 1] 无时间戳，其后切镜"
                f"时间戳从 {seam_runway_txt} 起算），全部落在本段 **时长** 内。")
        else:
            seam_duration_note = (
                "写本段**实际生成**秒数：= 设定时长（全局固定就写那个值、区间就按弧线取值，允许 1 位小数）")
            seam_handoff_rule = (
                f"- - 段际承接 + 剧情状态连续（CRITICAL — 本次任务一次输出 {seg_count_txt} 个 Video_XXX 分段时适用；"
                "单独生成单个分段忽略）：整片是分段生成再拼接的，段与段之间按**大切镜/换场**处理。①非首段：本段 "
                "[Shot 1] 是紧接上段剧情时间线的下一镜（可换景别/机位/空间），画面自足；**禁止**写“承接上段末帧/"
                "上段…继续”等伪画面引用。②首段按正常开场写。③剧情状态必须跨段连续：上段结束时的剧情状态（谁在哪/"
                "姿态/已发现什么/刚发生什么）就是本段开场的基础；禁止重演上段已演的事件、禁止状态回退（上段已站起/"
                "已发现/已下台阶，本段不得又蹲回/装作没发现/再踏上同一级台阶）。④非末段：本段最后一个 [Shot N] 用 "
                "1-2 句写清本段结束时的物理末态（谁/在哪/姿态/正进行动作），作剧情状态交接；禁止用对视/一笑/定格/"
                "转身等封闭收尾给中段画句号。⑤只有首段可完整开场、只有末段可真正收束。⑥切去别的空间再切回某场景"
                "时，保持该场景此前的机位/景别/光线语言一致。⑦时间戳：本段没有延续段，[Shot 1] 无时间戳；其后切镜"
                "时间戳从本段开头起算，全部落在本段 **时长** 内。")
    else:
        if _seam_on:
            seam_duration_note = (
                "write this segment's ACTUAL GENERATED seconds: the 1st segment = the configured duration (the fixed "
                "value, or a value picked by story arc when a range is set); from the 2nd segment on = configured "
                f"duration + the head hand-off {seam_runway_txt} (the value given by the duration setting above, "
                "1 decimal allowed)")
            seam_handoff_rule = (
                f"- - Head hand-off + story-state continuity (CRITICAL — applies when this task outputs {seg_count_txt} "
                "Video_XXX segments in one pass; ignore for single-segment generation): the whole film is generated "
                "segment-by-segment and then concatenated, and adjacent segments are physically stitched (each "
                "segment's opening is pinned to the previous segment's last frame), so the opening MUST be written as a "
                "hand-off shot. 1) If not the first: this segment's [Shot 1] IS the continuation of the previous "
                "segment's last frame — SAME camera position / shot size / lighting / character body pose and direction "
                f"of motion, with the action only slightly carried forward (no re-staging, no re-winding up), lasting "
                f"{seam_runway_txt}; this head section is DISCARDED entirely in the final cut, so this segment's NEW "
                f"content starts after {seam_runway_txt} (the first cut happens at ≈ {seam_runway_txt}, where you may "
                "change shot size / camera / space). 2) The first segment has no hand-off section — write a normal "
                "opening. 3) Story state MUST be continuous across segments: the previous segment's end state (who was "
                "where / in what pose / what had just been discovered or happened) IS the content of the hand-off "
                "shot; never replay an event already shown, never regress state (already stood up / discovered / stepped "
                "down -> do not squat back / act unaware / step onto the same stair again). 4) If not the last: close "
                "the final [Shot N] by stating the physical end-state in 1-2 sentences (who / where / pose / action in "
                "progress) as the story-state hand-off; never close a middle segment with a tableau (lock eyes / smile "
                "/ freeze / turn away). 5) Only the first segment may fully open the story, only the last may truly "
                "close it. 6) When cutting back to a scene visited earlier, keep that scene's camera/shot-size/lighting "
                "language consistent. 7) Timestamps: the hand-off section occupies time too ([Shot 1] has no timestamp; "
                f"later cuts start counting from {seam_runway_txt}) and all of them must fall within this segment's "
                "**Duration**.")
        else:
            seam_duration_note = (
                "write this segment's ACTUAL GENERATED seconds: = the configured duration (the fixed value, or a value "
                "picked by story arc when a range is set; 1 decimal allowed)")
            seam_handoff_rule = (
                f"- - Segment hand-off + story-state continuity (CRITICAL — applies when this task outputs {seg_count_txt} "
                "Video_XXX segments in one pass; ignore for single-segment generation): the segments are generated "
                "independently and then concatenated, so each segment boundary is a HARD CUT / scene change. 1) If not "
                "the first: this segment's [Shot 1] is the next shot that directly follows the previous segment's story "
                "timeline (you may change shot size / camera / space); the segment must stand on its own, and you MUST "
                "NOT write pseudo-references such as \"continuing from the previous segment's last frame\" / "
                "\"the previous segment continues\". 2) Write a normal opening for the first segment. 3) Story "
                "state MUST be continuous across segments: the previous segment's end state (who was where / in what "
                "pose / what had just been discovered or happened) is the basis of this segment's opening; never replay "
                "an event already shown, never regress state (already stood up / discovered / stepped down -> do not "
                "squat back / act unaware / step onto the same stair again). 4) If not the last: close the final "
                "[Shot N] by stating the physical end-state in 1-2 sentences (who / where / pose / action in progress) "
                "as the story-state hand-off; never close a middle segment with a tableau (lock eyes / smile / freeze / "
                "turn away). 5) Only the first segment may fully open the story, only the last may truly close it. "
                "6) When cutting back to a scene visited earlier, keep that scene's camera/shot-size/lighting language "
                "consistent. 7) Timestamps: this segment has no hand-off section ([Shot 1] has no timestamp); later "
                "cuts are timed from the start of this segment and all of them must fall within this segment's **Duration**.")


    schedule_rules = _build_schedule_rules(lang, enable_scene, enable_props, enable_video, enable_audio)

    # 用 replace 而非 format：rules 文本内含 {视觉描述} 等示意大括号，不能走 format
    rules = (H3_SHOT_RULES_ZH if lang == "zh" else H3_SHOT_RULES_EN)
    rules = rules.replace("{Seam_Duration_Note}", seam_duration_note)
    rules = rules.replace("{Seam_Handoff_Rule}", seam_handoff_rule)
    rules = rules.replace("{Segment_Count}", seg_count_txt)
    rules = rules.replace("{Segment_Duration}", duration_desc)
    rules = rules.replace("{Duration_Seconds}", duration_seconds)
    rules = rules.replace("{Seam_Runway}", seam_runway_txt)
    # 自定义 skill 也可能用到这些占位符（未使用的占位符原样保留，不影响输出）
    decompose_rules = decompose_rules.replace("{Segment_Count}", seg_count_txt)
    decompose_rules = decompose_rules.replace("{Segment_Duration}", duration_desc)
    decompose_rules = decompose_rules.replace("{Duration_Seconds}", duration_seconds)
    decompose_rules = decompose_rules.replace("{Seam_Runway}", seam_runway_txt)
    decompose_rules = decompose_rules.replace("{Detail_Length}", detail_length_txt)
    mode_instruction = mode_instruction.replace("{Segment_Count}", seg_count_txt)
    mode_instruction = mode_instruction.replace("{Segment_Duration}", duration_desc)
    mode_instruction = mode_instruction.replace("{Duration_Seconds}", duration_seconds)
    mode_instruction = mode_instruction.replace("{Seam_Runway}", seam_runway_txt)
    rules = rules.replace("{Schedule_Rules}", schedule_rules)
    _style_directing = ("- Directing grammar for this segment: execute the Story Style's \"## Core directing grammar\" "
                        "item by item (not re-injected here)." if is_en else
                        "- 本段导演语法：按『A. 故事风格』中该风格的「## 核心导演语法」逐条执行（此处不重复注入）。")
    rules = rules.replace("{Style_Directing}", _style_directing)
    rules = rules.replace("{Detail_Length}", detail_length_txt)
    _pref_directing = ("- Camera-language preference for this segment: execute the preference in \"## B. Constraint "
                       "framework\" item by item (not re-injected here)." if is_en else
                       "- 本段镜头偏好：按『B. 限制框架』的镜头偏好逐条执行（此处不重复注入）。")
    rules = rules.replace("{Preference_Directing}", _pref_directing)
    _custom_directing = ("- (no custom rules)" if is_en else "- （无自定义规则）")
    rules = rules.replace("{Custom_Rules_Directing}", (custom_rules or "").strip() or _custom_directing)

    # 参考素材说明：生成「标签对照表」（槽位名 → 素材名 → 描述），LLM 严格按表写 slots
    reference_intro = (_en.material_table_en(ref_image_intro, ref_video_intro, ref_audio_intro, re)
                       if is_en else _build_material_table(ref_image_intro, ref_video_intro, ref_audio_intro))

    # 顶层高权重偏好块（§1.5）：完整偏好文本；无偏好时给一句占位
    pref_block = (preference or "").strip()
    if not pref_block:
        pref_block = ("- No extra camera-language preference: shot size / camera move / cut rhythm / transition / "
                      "music / word count follow the story and the Story Style naturally." if is_en else
                      "- 无额外镜头语言偏好：景别/运镜/切镜/转场/音乐/字数按剧情与「故事风格」自然发挥。")

    # 顶层高权重自定义规范块（§1.6）：完整自定义润色规范文本；无则给占位
    custom_block = (custom_rules or "").strip() or ("- (no custom polish rules)" if is_en else "- （无自定义润色规范）")

    # 全局节奏统筹（§2.5）：时长区间 + 按弧线分摊时长与切镜预算（规则级导演规划）
    if seam_runway > 0:
        # 衔接启用：时长描述已由 _duration_spec_with_runway 写成「第 1 段 / 第 2 段起」两段式，直接用它
        _dur_line = f"- 时长设定：{duration_desc}"
    elif duration_lo == duration_hi:
        _dur_line = f"- 时长设定：全局固定，每段视频 **时长** 统一为 {int(duration_lo)} 秒。"
    else:
        _dur_line = (f"- 时长设定：每段视频时长在 {int(duration_lo)}~{int(duration_hi)} 秒区间内。"
                     f"正式拆解前，先按剧情弧线给每段分配一个实际秒数（开场与收束相对短、发展适中、高潮最长），"
                     f"并把该秒数写进每段元数据「**时长**」字段（取整秒，不得超区间）。")
    if seam_runway > 0:
        _seam_line = (
            f"- 段首衔接（本片按「无限时长」分段生成，段与段之间有物理衔接）：第 2 段起的 [Shot 1] 必须是"
            f"「承接上一段末帧画面的延续段」——同机位/同景别/同光线/同角色身体姿态与动作进行方向，动作只做轻微延续，"
            f"持续 {seam_runway_txt}；这一段在成片里会被**整段裁掉**，因此本段新内容从 {seam_runway_txt} 之后开始"
            f"（第一次切镜的时间戳约在 {seam_runway_txt} 处，可换景别/机位/空间）。第 1 段没有衔接段，按正常开场写。"
            f"剧情/物理状态仍然跨段连续：上段结尾谁在什么位置、什么姿态、发现/触发了什么，段首延续段必须与之完全一致"
            f"（禁止状态回退：上段已站起/已发现/已下台阶，延续段不得又蹲回/装没发现/再踏上同一级台阶），"
            f"也禁止把上段已演过的事件重演一遍（同一动作只演一次）。段尾（非末段）用 1-2 句写清结束瞬间的物理末态作剧情交接。")
    else:
        _seam_line = (
            "- 大切镜衔接：每段是独立生成的一段——段与段之间是一次大切镜/换场：段 N+1 的 [Shot 1] 是紧接上段剧情时间线的下一镜"
            "（可换景别/机位/空间），画面自足，**禁止写“承接上段末帧/上段…继续”等伪画面引用**；"
            "但剧情/人物物理状态必须跨段连续：上段结尾谁在什么位置、什么姿态、发现/触发了什么，下段必须基于该状态继续"
            "（禁止状态回退：上段已站起/已发现/已下台阶，下段不得又蹲回/装没发现/再踏上同一级台阶），"
            "禁止把上段已演过的事件重演一遍。段尾（非末段）用 1-2 句交代清楚结束瞬间的物理末态作剧情状态交接。")
    if is_en:
        timing_plan = _en.timing_plan_en(duration_lo, duration_hi, duration_desc, _seam_on,
                                         seam_runway_txt, seg_count_ref)
        timing_plan = duration_budget + "\n" + timing_plan
        seam_edge_rule = _en.seam_edge_rule_en(_seam_on, seam_runway_txt)
        seam_check_rule = _en.seam_check_rule_en(_seam_on)
        _sv = _en.decompose_seam_variants_en(_seam_on, seam_runway_txt)
        seam_principle_2 = _sv["Seam_Principle_2"]
        seam_decompose_bullet = _sv["Seam_Decompose_Bullet"]
        seam_self_contained = _sv["Seam_SelfContained"]
        seam_scheme_a = _sv["Seam_Scheme_A"]
        seam_no_pseudo_ref = _sv["Seam_No_Pseudo_Ref"]
        seam_ban_pseudo_ref = _sv["Seam_Ban_Pseudo_Ref"]
    else:
        timing_plan = (
            "把整部短剧当成一部连续影片来做导演统筹：\n"
            + _dur_line + "\n"
            + duration_budget + "\n"
            + f"- 切镜预算：每段 [Shot N] 数量 = 该段时长 ÷ 每镜时长（一镜通常 ≥0.8~1 秒；正常语速对白约 4~5 字/秒）。"
            "先估算整片可用切镜总量（{segment_count} 段时长之和 ÷ 每镜时长），再按剧情弧线把切镜密度分配到各段："
            "开场段少镜长镜（建立）、中段推进（单段 2~4 镜）、高潮段多镜快切、收束段回落；"
            "对白密集的段自动减镜加长（先保证每句台词+说话人神态+听者反应完整，再补动作/运镜/环境）。"
            "同一动作只切一次，禁止为凑镜数硬切或重复。\n"
            + _seam_line
        ).format(segment_count=seg_count_ref)

    # 衔接相关段落只在「强化层」里替换；自定义 skill 没有这些占位符时原样保留
    _skill_rules = (decompose_rules
                    .replace("{Seam_Principle_2}", seam_principle_2)
                    .replace("{Seam_Decompose_Bullet}", seam_decompose_bullet)
                    .replace("{Seam_SelfContained}", seam_self_contained)
                    .replace("{Seam_Scheme_A}", seam_scheme_a)
                    .replace("{Seam_No_Pseudo_Ref}", seam_no_pseudo_ref)
                    .replace("{Seam_Ban_Pseudo_Ref}", seam_ban_pseudo_ref)
                    .replace("{Seam_Runway}", seam_runway_txt))

    template = _en.SCRIPT_SKELETON_V2_EN if is_en else SCRIPT_SKELETON_V2
    return template.format(
        Skill_Task=mode_instruction,
        Story_Style=style,
        Segment_Count=seg_count_txt,
        Segment_Count_Spec=seg_count_spec,
        Segment_Duration=duration_desc,
        Skill_Rules=_skill_rules,
        Reference_Intro=reference_intro,
        H3_Shot_Rules=rules,
        Preference_Block=pref_block,
        Custom_Rules_Block=custom_block,
        Timing_Plan=timing_plan,
        User_Story=user_story,
        User_Tags=user_tags,
        Seam_Runway=seam_runway_txt,
        Seam_Edge_Rule=seam_edge_rule,
        Seam_Check_Rule=seam_check_rule,
        Duration_Hard_Rule=duration_hard_rule,
    )
