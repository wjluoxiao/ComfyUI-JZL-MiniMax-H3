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


def _resolve_segment_count(label):
    """分段数解析：标准标签（「6段」）→ SEGMENT_COUNT_OPTIONS；数字字符串（「6」/「1」）→ 精确整数；否则 4。

    管理器把 video_count 的任意 1-48 精确传给剧本处理器（不限于 4/6/9/12/16/20/24）。
    """
    if isinstance(label, str) and label in SEGMENT_COUNT_OPTIONS:
        return SEGMENT_COUNT_OPTIONS[label]
    if isinstance(label, str) and label.strip().isdigit():
        return max(1, min(48, int(label.strip())))
    if isinstance(label, (int, float)):
        return max(1, min(48, int(label)))
    return 4
def _parse_duration_spec(duration):
    """时长设置解析：支持 int/float 或字符串 "5"/"5-5"/"5~5"/"4-10"。返回 (lo, hi, desc)。
    lo==hi 表示每段固定该秒；lo<hi 为区间（拆解时每段按剧情弧线在区间内取值，写进各段 **时长**）。"""
    v = duration if duration is not None else 8
    s = str(v).strip()
    m = re.search(r"^\s*(\d+(?:\.\d+)?)\s*[~\-—]\s*(\d+(?:\.\d+)?)\s*$", s)
    if m:
        lo = float(m.group(1)); hi = float(m.group(2))
    else:
        try:
            n = float(s); lo = n; hi = n
        except Exception:
            lo = 8; hi = 8
    lo = max(4.0, min(15.0, lo)); hi = max(lo, min(15.0, hi))
    if lo > hi:
        lo, hi = hi, lo
    if lo == hi:
        return (lo, hi, f"统一 {int(lo)} 秒")
    return (lo, hi, f"{int(lo)}~{int(hi)} 秒区间（每段由你按剧情弧线给实际秒数，写进该段 **时长** 字段）")




# ═══════════════════════════════════════════════════════════════
#  V2 融合骨架（剧本与镜头处理器专用）
#  占位符: {Mode_Instruction} {Story_Style} {Segment_Count} {Timing_Plan}
#          {Decompose_Rules} {Reference_Intro} {H3_Shot_Rules} {User_Story}
# ═══════════════════════════════════════════════════════════════

SCRIPT_SKELETON_V2 = '''# Role: 顶级短视频编剧 & MiniMax H3 分段提示词工程师
你是专业的短视频编剧和 MiniMax H3 视频提示词撰写专家。你的任务分两步：
第一步：把用户故事按「情节」拆解为恰好 {Segment_Count} 个分段——每个分段是一段独立视频片段（时长见 B「限制框架」；区间设置时按剧情弧线给每段实际秒数，写进该段 **时长** 字段）。
第二步：把每个分段当成一段独立视频去润色，写出一条可直接送入 MiniMax H3 模型生成视频的完整提示词（六段 Ref2VA + 调度指令，格式见 D）。

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
【任务】{Mode_Instruction}
【分段数】恰好生成 {Segment_Count} 个分段；每段是一段 {Segment_Duration} 的独立视频（实际秒数写进该段 **时长** 字段）。

【镜头语言偏好（景别/运镜/切镜/转场/音乐/字数，逐条执行）】
{Preference_Block}

【全局节奏统筹（先按弧线给每段定实际秒数与 [Shot N] 数）】
{Timing_Plan}

## C. 执行与审查流程（严格按顺序，一次指令内完成）
第 1 步 · 理解：通读 A（故事/风格/素材对照表），识别剧情弧线、人物与台词、本批可调用素材及其「类型:槽位名」。
第 2 步 · 规划：按 B 的统筹与偏好，给每段定实际秒数与 [Shot N] 数（按弧线分配，开场少/中段推进/高潮多/收束回落）。
第 3 步 · 执行：逐段输出 [SHOT_START]...[SHOT_END]，每段含（一）分段信息 +（二）六段 Ref2VA + 调度指令（格式见 D）。执行时遵守：
{Decompose_Rules}
   - 段边界 = 大切镜：下段 [Shot 1] 紧接上段剧情时间线、画面自足（可换景别/机位/空间）；禁「承接上段末帧/上段…继续」伪引用；人物/物理状态跨段连续（禁回退/重演上段已演事件）；段尾（非末段）用 1-2 句写清物理末态作剧情交接。
第 4 步 · 审查（写完全部后自检一遍，不满足即改）：
   - 六段字段齐全、字段间各空一行、无 markdown 代码块；
   - <Subject N>/<Picture N>/<Video N>/<Audio N> 与 subject_definitions 一致，且与 SCENE/VIDEO/AUDIO_INSTRUCTION.slots 编号严格同源（slots[0]=<Picture 1>…）；
   - 角色/场景/道具 三字段只用素材声明的元素；slots 只写「类型:槽位名」，严禁自造/缩写/用素材名当槽位名；
   - 台词原样在 <d>（保留原语言）；无脑补台词/内心独白/语气词；
   - 每个 [Shot N] 时间戳落在该段时长内、[Shot 1] 无时间戳；
   - 每个 [Shot N] 都写明该镜景别+运镜，且段内不同 [Shot N] 用**不同景别+不同运镜**混合（长视频 4-6 种递进、短视频 2-3 种，随剧情推进变换），严禁整段只复用一种景别或一种运镜；
   - 六段正文语言 = 用户所选输出语言（中文[ZH]=中文、英文[EN]=英文；字段名/标记保持英文），仅 <d>/画面文字保留原语言；
   - retention_analysis 程度标记正确、破折号后列举已定义特征（无「保留」二字）；
   - 无「承接上段末帧」「上段…继续」伪引用；无状态回退/重演。

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


def _extract_detail_length(preference_text):
    """从偏好设定词里提取「详细描述字数」的数字范围（如 800-1200），默认 350-500。"""
    if not preference_text:
        return "350-500"
    m = re.search(r'详细描述字数[:：][^\n]*\((\d+-\d+)\s*[字词]', preference_text)
    if m:
        return m.group(1)
    return "350-500"


def _build_material_table(ref_image_intro, ref_video_intro, ref_audio_intro):
    """解析用户素材描述，生成「标签对照表」，供 LLM 严格按槽位名写 slots。

    用户描述每行格式：「类型+字母 = 素材名（描述）」，如「角色A = 孙悟空（橙色武道服）」。
    返回对照表文本；无素材时返回空串。
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
            m = re.match(r'^(角色|场景|道具|视频|音频|分镜|音效|音乐|其他)\s*([A-Za-z])\s*[=＝:：]\s*(.+)', line)
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
            "- 调度指令 slots 写「类型:槽位名」，如 场景:场景A、角色:角色A、道具:道具A、视频:视频A、音频:音频A，严禁写素材名（孙悟空）或自造槽位。\n"
            "- subject_definitions 写素材名（<Subject N> 是 <Picture N> 中的孙悟空），括号里的外貌描述原样复述。\n"
            "- 音频槽位对应说话人音色：本段谁开口说话，AUDIO_INSTRUCTION.slots 就写对应角色的「音频:音频X」，无对话分段音频 slots 写空。")

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
) -> str:
    """剧本与镜头处理器 — 拼装一次成型的 System Prompt（融合拆解 + 六段 Ref2VA 规范）。

    lang: "zh" / "en"；segment_duration: 每段视频时长(秒)，约束时间戳范围。
    """
    from ..sheding.mode_instructions import MODE_INSTRUCTIONS as _mi
    from ..sheding.story_styles import resolve_style as _resolve_style
    from ..sheding.decompose_rules import DECOMPOSE_RULES as _dr
    from ..sheding.h3_shot_rules import H3_SHOT_RULES_ZH, H3_SHOT_RULES_EN

    mode_instruction = _mi.get(mode, list(_mi.values())[0] if _mi else "")
    style = _resolve_style(story_style)
    segment_count = _resolve_segment_count(segment_count_label)
    duration_lo, duration_hi, duration_desc = _parse_duration_spec(segment_duration)

    schedule_rules = _build_schedule_rules(lang, enable_scene, enable_props, enable_video, enable_audio)

    # 用 replace 而非 format：rules 文本内含 {视觉描述} 等示意大括号，不能走 format
    rules = (H3_SHOT_RULES_ZH if lang == "zh" else H3_SHOT_RULES_EN)
    rules = rules.replace("{Segment_Count}", str(segment_count))
    rules = rules.replace("{Segment_Duration}", duration_desc)
    rules = rules.replace("{Schedule_Rules}", schedule_rules)
    rules = rules.replace("{Style_Directing}", "- 本段导演语法：按『A. 故事风格』中该风格的「## 核心导演语法」逐条执行（此处不重复注入）。")
    rules = rules.replace("{Detail_Length}", _extract_detail_length(preference))
    rules = rules.replace("{Preference_Directing}", "- 本段镜头偏好：按『B. 限制框架』的镜头偏好逐条执行（此处不重复注入）。")
    rules = rules.replace("{Custom_Rules_Directing}", (custom_rules or "").strip() or "- （无自定义规则）")

    # 参考素材说明：生成「标签对照表」（槽位名 → 素材名 → 描述），LLM 严格按表写 slots
    reference_intro = _build_material_table(ref_image_intro, ref_video_intro, ref_audio_intro)

    # 顶层高权重偏好块（§1.5）：完整偏好文本；无偏好时给一句占位
    pref_block = (preference or "").strip()
    if not pref_block:
        pref_block = "- 无额外镜头语言偏好：景别/运镜/切镜/转场/音乐/字数按剧情与「故事风格」自然发挥。"

    # 全局节奏统筹（§2.5）：时长区间 + 按弧线分摊时长与切镜预算（规则级导演规划）
    if duration_lo == duration_hi:
        _dur_line = f"- 时长设定：全局固定，每段视频 **时长** 统一为 {int(duration_lo)} 秒。"
    else:
        _dur_line = (f"- 时长设定：每段视频时长在 {int(duration_lo)}~{int(duration_hi)} 秒区间内。"
                     f"正式拆解前，先按剧情弧线给每段分配一个实际秒数（开场与收束相对短、发展适中、高潮最长），"
                     f"并把该秒数写进每段元数据「**时长**」字段（取整秒，不得超区间）。")
    timing_plan = (
        "把整部短剧当成一部连续影片来做导演统筹：\n"
        + _dur_line + "\n"
        + f"- 切镜预算：每段 [Shot N] 数量 = 该段时长 ÷ 每镜时长（一镜通常 ≥0.8~1 秒；正常语速对白约 4~5 字/秒）。"
        "先估算整片可用切镜总量（约 {segment_count} 段时长之和 ÷ 每镜时长），再按剧情弧线把切镜密度分配到各段："
        "开场段少镜长镜（建立）、中段推进（单段 2~4 镜）、高潮段多镜快切、收束段回落；"
        "对白密集的段自动减镜加长（先保证每句台词+说话人神态+听者反应完整，再补动作/运镜/环境）。"
        "同一动作只切一次，禁止为凑镜数硬切或重复。\n"
        "- 大切镜衔接：每段是独立生成的一段——段与段之间是一次大切镜/换场：段 N+1 的 [Shot 1] 是紧接上段剧情时间线的下一镜"
        "（可换景别/机位/空间），画面自足，**禁止写“承接上段末帧/上段…继续”等伪画面引用**；"
        "但剧情/人物物理状态必须跨段连续：上段结尾谁在什么位置、什么姿态、发现/触发了什么，下段必须基于该状态继续"
        "（禁止状态回退：上段已站起/已发现/已下台阶，下段不得又蹲回/装没发现/再踏上同一级台阶），"
        "禁止把上段已演过的事件重演一遍。段尾（非末段）用 1-2 句交代清楚结束瞬间的物理末态作剧情状态交接。"
    ).format(segment_count=segment_count)

    return SCRIPT_SKELETON_V2.format(
        Mode_Instruction=mode_instruction.format(Segment_Count=segment_count),
        Story_Style=style,
        Segment_Count=segment_count,
        Segment_Duration=duration_desc,
        Decompose_Rules=_dr,
        Reference_Intro=reference_intro,
        H3_Shot_Rules=rules,
        Preference_Block=pref_block,
        Timing_Plan=timing_plan,
        User_Story=user_story,
        User_Tags=user_tags,
    )
