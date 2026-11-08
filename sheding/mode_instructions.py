# MiniMax-H3 剧本编剧模式设定
# ==============================
# 内置「强化层」设定词已迁移为文件：
#     sheding/skills/mode/10-标准拆解.md
#   该文件用 <!-- JZL:TASK --> / <!-- JZL:RULES --> 标记切成「任务」与「执行规则」两段。
#   直接编辑那个 .md 即可生效（重启 ComfyUI 或重新加载工作流）。
#   用户也可以在节点面板导入自定义拆解 skill 覆盖它（强化层可换，强制格式层不可换）。
#
# 本文件只负责加载；_FALLBACK_TASK 仅在 .md 缺失时兜底。

import sys as _sys

try:
    from .skills_loader import MODE, list_skills, parse_mode_skill, pick_language, read_skill
except Exception:  # pragma: no cover - 极端情况下被单独加载
    MODE, list_skills, parse_mode_skill, pick_language, read_skill = "mode", None, None, None, None

# 旧版「生成模式 (Generate)」仅被独立的「剧本与镜头处理器」节点使用（管理器始终走拆解模式），
# 不属于可切换 skill 的范围，原文保留在本文件中。
_GENERATE_INSTRUCTION = (
    "你收到了一句简短的创作灵感——可能是一句话，也可能是一个小片段。"
    "你需要像一个经验丰富的短视频编剧那样，分两步完成工作。\n\n"
    "## 第一步：写出完整故事正文（必须输出，禁止只在脑中构思）\n"
    "在回复的最开头，先以「【故事】」作为小标题，输出一段 200-500 字的完整故事正文。\n"
    "故事正文必须是连贯、可读的故事叙述——像小说一样按情节自然展开，用段落文字写，禁止写成大纲/列表/要点/小标题。故事要交代清楚：谁在什么情境下、因为什么冲突、经历了怎样的发展、最终如何收束。\n\n"
    "故事正文写完后空一行，再进入第二步输出分段块。禁止写出毫无冲突的流水账（如\"他早上起床，刷牙，出门\"），每个故事必须有戏剧张力。\n"
    "【扩容 ≠ 复述】（CRITICAL）：扩容故事必须在完整保留用户输入的所有对话、台词与剧情（绝不删减）的基础上，主动扩展、明显变丰满——补充场景描写、角色心理与神态、动作细节、环境氛围、情节过渡、冲突的递进与展开，让故事篇幅明显超过原文。禁止只把原文复述一遍（那等于没完成任务）。用户输入的每一句台词都要原样保留，并在后续分段里用 <d> 标签重现；严禁给角色新增原文没有的台词——扩展的是画面与细节，不是台词。\n\n"
    "## 第二步：拆解为分段\n"
    "基于你刚刚写出的故事正文，将其按「情节」拆解为恰好 {Segment_Count} 个分段（每个分段 = 一段独立视频，[SHOT_START]...[SHOT_END] 块）。拆解时遵循：\n"
    "- 开场 1-2 镜：建立时间、地点、氛围，引入主角。使用远景或全景。\n"
    "- 发展段：推进冲突，展示角色行动与反应。交替使用中景和近景。\n"
    "- 高潮段：冲突爆发的瞬间，善用特写和动态运镜。\n"
    "- 结局 1-2 镜：故事的收束，留给观众的情绪落点。\n\n"
    "每个分段的动作描述和提示词都要承接故事正文的情节，确保故事有完整的起承转合。"
)

# .md 缺失时的最小兜底（与内置 md 的「任务」段同义）
_FALLBACK_TASK = (
    "你收到了一篇完整的短篇故事。你的任务不是创作，而是像一个顶级的影视分段导演那样，"
    "将这篇故事按「情节」精确拆解为 {Segment_Count} 个分段（每个分段 = 一段独立视频，对应一个情节段落）。\n"
    "严禁新增原文不存在的剧情/角色/场景/道具/对话，原文台词必须原样保留。"
)


_FALLBACK_TASK_EN = (
    "You are given a complete short story. Your job is not to create, but to cut the story by PLOT into exactly "
    "{Segment_Count} segments (each segment = one independent video clip, corresponding to one plot beat).\n"
    "Never add plot / characters / scenes / props / dialogue that are absent from the source; every existing line "
    "must be preserved verbatim."
)


def _load_builtin_task(lang="zh"):
    if read_skill is None:
        return _FALLBACK_TASK_EN if str(lang).lower().startswith("en") else _FALLBACK_TASK
    try:
        items = [it for it in list_skills(MODE, include_user=False) if it["source"] == "builtin"]
        if not items:
            return _FALLBACK_TASK_EN if str(lang).lower().startswith("en") else _FALLBACK_TASK
        raw = read_skill(MODE, items[0]["id"])
        # 双语文件按语言取段；单语文件（纯中文/纯英文）原样使用
        text = pick_language(raw, lang) if pick_language else raw
        task, _rules = parse_mode_skill(text)
        if task:
            return task
        return _FALLBACK_TASK_EN if str(lang).lower().startswith("en") else _FALLBACK_TASK
    except Exception as exc:
        _sys.stderr.write(f"[JZL-skill] ⚠️ 读取内置拆解 skill 失败：{exc}（使用兜底文本）\n")
        return _FALLBACK_TASK_EN if str(lang).lower().startswith("en") else _FALLBACK_TASK


def build_mode_instruction(mode="拆解模式 (Decompose)", lang="zh"):
    """按语言取任务指令：拆解模式走 skill，生成模式保留旧文案。"""
    if str(mode or "").startswith("生成"):
        return _GENERATE_INSTRUCTION
    return _load_builtin_task(lang)


MODE_INSTRUCTIONS = {
    "拆解模式 (Decompose)": _load_builtin_task("zh"),
    "生成模式 (Generate)": _GENERATE_INSTRUCTION,
}
