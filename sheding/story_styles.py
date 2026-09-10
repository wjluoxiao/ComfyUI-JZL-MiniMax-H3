# MiniMax-H3 故事风格设定
# =========================
# 风格为「叙事重心谱系」：忠于用户剧情，只决定哪些内容占更多镜头/时长/扩写比例。
# 真实来源 = 本目录 styles/*.md（每个文件一个风格，文件名即下拉选项）。
# 本文件仅负责从 styles/ 目录动态加载；styles 目录缺失时才用下方最小兜底。
# 直接修改 .md 文件，重启 ComfyUI 或重新加载工作流即可生效。

import os as _os
import re as _re

_STYLES_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "styles")

# ── 最小兜底（仅当 styles/ 目录不存在或无 .md 时使用，避免下拉为空）──
_FALLBACK = {
    "热血战斗": "动作重心：招式/对抗/打击反馈占最大篇幅，忠于剧情。",
    "悬疑惊悚": "悬念重心：信息差/伏笔/压迫感放大，忠于剧情。",
    "温馨日常": "情感重心：微动作/表情/语气/留白为镜头中心，忠于剧情。",
    "甜宠爽文": "反差爽点重心：揭晓/围观反应/浪漫时刻占最多篇幅，忠于剧情。",
    "宏大奇观": "世界奇观重心：环境/设定细节与角色「看见」占最多篇幅，忠于剧情。",
    "乡土喜乐": "喜剧重心：误会/夸张肢体/围观哄笑节奏欢快，忠于剧情。",
    "歌神舞台": "音乐节拍重心：表演/灯光/观众反应跟节拍，忠于剧情。",
}


# 旧工作流兼容 + 脏文件过滤：2026-09-07 风格 19→7 合并后，把已删除的旧风格名映射到合并后的新风格
# （使保存了旧风格名的旧工作流仍能取到新风格全文）。这些旧名同时是「styles/ 目录脏文件黑名单」：
# 升级后若旧 md 未删干净（覆盖式安装的常见残留），会自动忽略，避免下拉出现已废弃的老风格。
LEGACY_STYLE_MAP = {
    "古风武侠": "热血战斗",
    "修仙问道": "热血战斗",
    "悬疑推理": "悬疑惊悚",
    "恐怖惊悚": "悬疑惊悚",
    "谍战风云": "悬疑惊悚",
    "黑色电影": "悬疑惊悚",
    "历史权谋": "悬疑惊悚",
    "都市情感": "温馨日常",
    "校园青春": "温馨日常",
    "田园种田": "温馨日常",
    "奇幻冒险": "宏大奇观",
    "科幻未来": "宏大奇观",
    "末日废土": "宏大奇观",
    "霸总甜宠": "甜宠爽文",
    "逆袭打脸": "甜宠爽文",
    "穿越重生": "甜宠爽文",
    "乡村喜剧": "乡土喜乐",
}


def _load_styles_from_dir():
    """从 styles/ 目录读取全部 .md 风格文件；文件名去数字前缀即风格名。

    已废弃的旧风格名（LEGACY_STYLE_MAP 的键）会被跳过——即使升级时旧 md 未删干净，
    下拉也不会再出现这些老风格（兼容覆盖式安装的残留文件）。"""
    styles = {}
    if not _os.path.isdir(_STYLES_DIR):
        return styles
    names = [n for n in _os.listdir(_STYLES_DIR) if n.endswith(".md")]
    names.sort()
    for name in names:
        key = _re.sub(r"^\d+[-_]\s*", "", name[:-3]).strip()
        if not key or key in LEGACY_STYLE_MAP:  # 跳过空名与已废弃的旧风格
            continue
        try:
            with open(_os.path.join(_STYLES_DIR, name), encoding="utf-8") as _f:
                styles[key] = _f.read().strip()
        except Exception:
            continue
    return styles


STORY_STYLES = _load_styles_from_dir() or dict(_FALLBACK)


def resolve_style(name):
    """按风格名取风格全文；旧名自动映射到合并后的新风格。"""
    if not name:
        return STORY_STYLES.get(list(STORY_STYLES.keys())[0], "") if STORY_STYLES else ""
    if name in STORY_STYLES:
        return STORY_STYLES[name]
    mapped = LEGACY_STYLE_MAP.get(str(name).strip())
    if mapped and mapped in STORY_STYLES:
        return STORY_STYLES[mapped]
    return STORY_STYLES.get(name, name)


# ── 下拉显示标签（前端 combo getOptionLabel 使用）────────────────────────
# 说明：选项值仍存风格短名（工作流/后端解析零改动），仅下拉「显示文本」带统一备注。
# 备注从各风格 md 首行「推荐故事类型」提炼 4 个两字代表词，保证 7 个选项长度一致。
STYLE_LABELS = {
    "热血战斗": "热血战斗（适合 竞技/武侠/修仙/末世）",
    "悬疑惊悚": "悬疑惊悚（适合 推理/谍战/权谋/刑侦）",
    "温馨日常": "温馨日常（适合 都市/校园/亲情/治愈）",
    "甜宠爽文": "甜宠爽文（适合 霸总/逆袭/重生/豪门）",
    "宏大奇观": "宏大奇观（适合 奇幻/科幻/史诗/废土）",
    "乡土喜乐": "乡土喜乐（适合 乡村/市井/闹剧/群像）",
    "歌神舞台": "歌神舞台（适合 演唱/选秀/舞台/音乐）",
}


def style_label(name):
    """风格短名 → 下拉显示文本（带统一备注）；查不到则原样返回。"""
    if not name:
        return name
    return STYLE_LABELS.get(str(name).strip(), str(name))

