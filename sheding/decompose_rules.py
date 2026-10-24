# MiniMax-H3 分段拆解规则（强化层「执行规则」）
# ==============================================
# 真实来源 = sheding/skills/mode/10-标准拆解.md 的 <!-- JZL:RULES --> 段。
# 直接编辑那个 .md 即可生效；本文件只负责加载（缺失时用 _FALLBACK 兜底）。

import sys as _sys

try:
    from .skills_loader import MODE, list_skills, parse_mode_skill, pick_language, read_skill
except Exception:  # pragma: no cover
    MODE, list_skills, parse_mode_skill, pick_language, read_skill = "mode", None, None, None, None

_FALLBACK = (
    "### 拆解前的全局统筹: 先按剧情弧线给每段定实际秒数与切镜数；"
    "### 拆解原则: 分段是情节段落，不是单个动作；禁止新增原文不存在的剧情/对白/角色/场景/道具。"
)
_FALLBACK_EN = (
    "### Global planning: assign each segment its actual seconds and shot count along the story arc first.\n"
    "### Principle: a segment is a plot beat, not a single action; never add plot / dialogue / characters / "
    "scenes / props absent from the source."
)


def _load_builtin_rules(lang="zh"):
    is_en = str(lang).lower().startswith("en")
    if read_skill is None:
        return _FALLBACK_EN if is_en else _FALLBACK
    try:
        items = [it for it in list_skills(MODE, include_user=False) if it["source"] == "builtin"]
        if not items:
            return _FALLBACK_EN if is_en else _FALLBACK
        raw = read_skill(MODE, items[0]["id"])
        text = pick_language(raw, lang) if pick_language else raw
        _task, rules = parse_mode_skill(text)
        return rules or (_FALLBACK_EN if is_en else _FALLBACK)
    except Exception as exc:
        _sys.stderr.write(f"[JZL-skill] ⚠️ 读取内置拆解 skill 失败：{exc}（使用兜底规则）\n")
        return _FALLBACK_EN if is_en else _FALLBACK


def build_decompose_rules(lang="zh"):
    return _load_builtin_rules(lang)


DECOMPOSE_RULES = _load_builtin_rules("zh")
