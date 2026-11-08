# MiniMax H3 故事扩写模式设定（只扩写故事正文，不拆解）
# ======================================================================
# 内置「强化层」设定词已迁移为文件：
#     sheding/skills/expand/10-标准扩写.md
#   该文件用 <!-- JZL:LANG:ZH --> / <!-- JZL:LANG:EN --> 分语言存放扩写规则；
#   用户可在节点面板导入自定义扩写 skill 覆盖它。
#
# 本文件负责：① 加载内置规则；② 把「素材约束 + 故事风格」拼到规则后面。
# 素材约束与故事风格属于强化层之外的事实约束/风格层，任何 skill 都不会覆盖它们。

import re as _re
import sys as _sys

try:
    from .skills_loader import EXPAND, list_skills, read_skill
except Exception:  # pragma: no cover
    EXPAND, list_skills, read_skill = "expand", None, None

_LANG_MARK = _re.compile(r"<!--\s*JZL\s*:\s*LANG\s*:\s*(ZH|EN)\s*-->", _re.IGNORECASE)

_MATERIAL_ZH = '''## 参考素材约束（CRITICAL — 本模式必须按素材扩写，违反即失败）
下方是你可用的全部素材（角色/场景/道具，来自「参考素材管理」勾选项）。扩写时**只能使用下方素材里出现的角色、场景、道具**：
- **名称严格对齐（最高铁律）**：故事涉及的角色/场景/道具，每次出现都必须**原样写出素材清单里的完整准确名称**（逐字一致，禁止简称/意译/改写/只写描述不写名称）。写错或漏写名称会导致下游拆解无法调用该素材 = 失败。
- **场景先点名再引描述**：每一场戏必须先明确"故事发生在哪个场景"——用素材中该场景的**完整名称**点出地点，然后再按素材描述展开该处的环境/氛围/动作；不得只按素材描述写画面却不提场景名。
- **道具先点名再引描述**：凡写到素材中的道具，先写出其**完整名称**（如"青釭剑""传家玉佩"），再按其描述写用法/观感/细节；不得含糊地只写"一把剑"而漏掉素材名称。
- **角色同样**：人物一出现先写素材里的**完整角色名**，再按其描述写外貌/动作/神态。
- **严禁杜撰**：严禁编造素材里没有的角色/场景/道具；如果用户故事提到了素材里没有的人/地方/物件，要么改用最接近的素材元素替代，要么删去该元素，不得凭空新增。
- 扩写只能围绕素材元素展开画面/动作/环境/氛围细节，不得引入素材外的元素（如素材没有"医院"场景就不得写医院）。
- 素材的「名称（描述）」括号内的外观/特征原样采信用于描写。
- **外观术语逐字沿用（CRITICAL）**：素材描述里的服装/道具名称、配色、材质、造型等术语必须**原样沿用**，严禁同义改写或自由发挥（例：描述写「淡蓝色牛仔连衣裤」→ 不得写成「天蓝色背带裤」；写「米色围脖」→ 不得写成「米色围巾」）。描述与参考图一致时**就照描述写、不做额外发挥**；若开启识图且图与描述**明显不符**，以参考图为准校准。

## 可用素材清单
{MATERIAL_INTRO}'''

_MATERIAL_EN = '''## Reference-material constraint (CRITICAL — this mode MUST expand using the materials below)
The materials below are ALL you may use (characters / scenes / props from the enabled asset manager items). When expanding, ONLY use characters, scenes, and props that appear in the material list:
- EXACT NAME MATCHING (top rule): every time a character/scene/prop appears in the story, write out the FULL exact name exactly as it is in the material list, verbatim — no abbreviations, paraphrases, rewording, or describing an item without naming it. A wrong or missing name means downstream shot decomposition cannot reference that asset = failure.
- Scenes — name first, then describe: for every scene beat, first state where the story takes place using the scene's FULL material name, then develop that location from its material description; do not paint the setting without naming it.
- Props — name first, then describe: whenever a listed prop appears, write its FULL material name first (e.g. "Green Steel Sword", "heirloom jade pendant"), then describe its use/appearance/detail from the material; never vaguely write "a sword" and omit the material name.
- Characters — likewise: introduce a person by the FULL material character name first, then describe appearance/actions/expression from the material.
- NEVER invent: do NOT fabricate characters/scenes/props absent from the list. If the user story mentions someone/place/object NOT in the materials, either substitute the closest listed element or drop it — never add new ones.
- Expansion may only enrich visuals/actions/environment/atmosphere AROUND the listed elements; do not introduce out-of-list elements (e.g. no hospital if no "hospital" scene is listed).
- The material "name (description)" parentheticals are authoritative for appearance/features.
- EXACT APPEARANCE TERMS (CRITICAL): reuse the material description's terms for clothing / props / colors / materials / shapes VERBATIM — never reword them (e.g. "light-blue denim overalls" must NOT become "sky-blue suspender pants"; "beige neck warmer" must NOT become "beige scarf"). If the reference image and the description agree, simply follow the description and do NOT improvise. If image input is enabled and the image clearly contradicts the description, correct toward the image.

## Available materials
{MATERIAL_INTRO}'''

_STYLE_HEAD_ZH = "## 故事风格（按此扩写的基调/题材/氛围）"
_STYLE_HEAD_EN = "## Story Style (the tone/genre/atmosphere to expand toward)"

_FALLBACK_ZH = (
    "# Role: 短视频故事扩写师（专职一件事：把用户故事扩写成更丰满的正文）\n"
    "忠于用户原意、保留全部台词、扩写不复述、只输出故事正文。"
)
_FALLBACK_EN = (
    "# Role: Short-drama story expander (single job: expand the user's story into a fuller narrative)\n"
    "Stay faithful, keep all dialogue, expand rather than repeat, output only the narrative."
)


def _split_lang(text):
    """按 <!-- JZL:LANG:XX --> 切分为 {ZH: ..., EN: ...}；无标记返回 {}。"""
    if not text or not _LANG_MARK.search(text):
        return {}
    parts = _LANG_MARK.split(text)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        out[parts[i].upper()] = parts[i + 1].strip()
    return out


def load_builtin_expand_rules(lang="zh"):
    """内置扩写规则（按语言取值；自定义 md 没有分语言时整份返回）。"""
    if read_skill is None:
        return _FALLBACK_EN if lang == "en" else _FALLBACK_ZH
    try:
        items = [it for it in list_skills(EXPAND, include_user=False) if it["source"] == "builtin"]
        if not items:
            return _FALLBACK_EN if lang == "en" else _FALLBACK_ZH
        text = read_skill(EXPAND, items[0]["id"]) or ""
        langs = _split_lang(text)
        if langs:
            key = "EN" if lang == "en" else "ZH"
            return langs.get(key) or langs.get("ZH") or text.strip()
        return text.strip()
    except Exception as exc:
        _sys.stderr.write(f"[JZL-skill] ⚠️ 读取内置扩写 skill 失败：{exc}（使用兜底文本）\n")
        return _FALLBACK_EN if lang == "en" else _FALLBACK_ZH


def build_expand_prompt(lang, story_style, expand_words, expand_source="素材", material_intro="", skill_text=None):
    """拼装故事扩写节点的 System Prompt。

    skill_text: 自定义扩写 skill 全文（None/空 → 用内置规则）。
    expand_source: "素材"=按参考素材扩写（注入素材清单 + 禁杜撰铁律）；"自由"=自由发挥（无素材约束）。
    material_intro: 参考素材管理勾选项的素材描述（角色/场景/道具…），"素材"模式必须传入。
    """
    body = (skill_text or "").strip() or load_builtin_expand_rules(lang)
    body = body.replace("{EXPAND_WORDS}", str(int(expand_words or 500)))
    body = body.replace("{LANG_NAME}", "English" if lang == "en" else "简体中文")

    parts = [body]
    if str(expand_source or "素材") != "自由":
        material = (material_intro or "").strip()
        if not material:
            material = ("（当前「参考素材管理」没有勾选任何素材——没有可用的角色/场景/道具，请先勾选素材；"
                        "若坚持扩写则不得编造任何人物/场景名，仅描写用户输入故事已有的元素。）")
        mat_tpl = _MATERIAL_EN if lang == "en" else _MATERIAL_ZH
        parts.append(mat_tpl.replace("{MATERIAL_INTRO}", material))
    parts.append(("## Story Style (the tone/genre/atmosphere to expand toward)" if lang == "en" else "## 故事风格（按此扩写的基调/题材/氛围）")
                 + "\n" + ((story_style or "").strip() or ("- 无风格设定" if lang != "en" else "- no style set")))
    return "\n\n".join(parts)
