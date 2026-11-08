# -*- coding: utf-8 -*-
"""JZL Skill 统一加载器
======================

skill = 一段可替换的「提示词强化设定词」（不是输出格式规范）。

分层约定（优先级从低到高）::

    ① 强化层  ← skill 控制（怎么理解故事 / 补哪些细节 / 怎么设计动作）
    ② 风格层  ← 内置 8 风格 或 自定义风格 skill
    ③ 偏好层  ← 镜头语言偏好 + 自定义润色规范
    ④ 强制层  ← 输出格式 + 调度指令 + 时间戳（用户不可改，永远最后生效）

目录::

    <插件>/sheding/skills/<kind>/*.md      内置 skill（随插件发布）
    <ComfyUI user>/jzl_skills/<kind>/*.md  用户导入的 skill

kind:
    mode   故事拆解模式 / 仅提示词输出 共用（任务 + 执行规则）
    expand 故事扩写模式（扩写规则；不涉及格式与调度）
    style  自定义故事风格（风格层全文）

skill id: ``"builtin:<文件名去扩展名>"`` / ``"user:<文件名去扩展名>"``
"""

import os
import re

MODE = "mode"
EXPAND = "expand"
STYLE = "style"

KINDS = (MODE, EXPAND, STYLE)
KIND_LABELS = {
    MODE: "拆解 skill",
    EXPAND: "扩写 skill",
    STYLE: "故事风格 skill",
}

# mode skill 内的分段标记（用户不加标记时，「全文」视为执行规则）
TASK_MARK = "<!-- JZL:TASK -->"
RULES_MARK = "<!-- JZL:RULES -->"
_MARK_RE = re.compile(r"<!--\s*JZL\s*:\s*(TASK|RULES)\s*-->", re.IGNORECASE)

# 语言分段标记（可选）：同一个 skill 文件里放中英两套
LANG_MARK = "<!-- JZL:LANG:ZH --> / <!-- JZL:LANG:EN -->"
_LANG_RE = re.compile(r"<!--\s*JZL\s*:\s*LANG\s*:\s*(ZH|EN)\s*-->", re.IGNORECASE)

# 支持 .txt 与 .md（.markdown 一并放行）：内置 skill 用 .md 作模板，用户可导入任意一种。
_EXT_ALLOW = {".md", ".txt", ".markdown"}
_NAME_MAX = 80


def read_text(path, limit=None):
    """按 UTF-8(BOM) → GBK 顺序读取文本；手工放进目录的 GBK txt 也能正常加载。"""
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(path, encoding=enc) as f:
                return f.read() if limit is None else f.read(limit)
        except UnicodeDecodeError:
            continue
        except Exception:
            return None
    try:                                  # 最后兜底：替换非法字节，至少能显示
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read() if limit is None else f.read(limit)
    except Exception:
        return None


# ── 目录 ────────────────────────────────────────────────────────
def plugin_skills_root():
    """插件内置 skill 根目录：<插件>/sheding/skills。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def user_skills_root():
    """用户导入 skill 根目录：<ComfyUI user>/jzl_skills（取不到 user 目录时回退插件目录）。"""
    try:
        import folder_paths
        base = folder_paths.get_user_directory()
    except Exception:
        base = None
    if not base:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills_user")
    return os.path.join(base, "jzl_skills")


def skills_dir(kind, user=False):
    if kind not in KINDS:
        raise ValueError(f"未知 skill 类型：{kind}")
    root = user_skills_root() if user else plugin_skills_root()
    return os.path.join(root, kind)


def style_dir(lang="zh", user=False):
    """故事风格的英文译文单独放 `<kind>/en/`（中文仍在 `<kind>/` 根目录）。

    用户自定义风格目录同样支持 `en/` 子目录；文件同名即视为同一风格的英文版。
    """
    base = skills_dir(STYLE, user=user)
    return os.path.join(base, "en") if str(lang or "").lower().startswith("en") else base


def ensure_user_dir(kind):
    path = skills_dir(kind, user=True)
    os.makedirs(path, exist_ok=True)
    return path


# ── 名称工具 ────────────────────────────────────────────────────
def safe_skill_name(raw):
    """把用户提供的文件名规范成安全的 skill 名（不含扩展名）。

    去掉路径分隔符与控制字符、去掉 Windows 保留字符，限制长度；保留中文。
    """
    name = os.path.basename(str(raw or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(name)
    if ext.lower() in _EXT_ALLOW:
        name = stem
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return name[:_NAME_MAX]


def make_skill_id(source, name):
    src = "user" if str(source).lower() == "user" else "builtin"
    return f"{src}:{name}"


def split_skill_id(skill_id):
    """→ (source, name)；无效返回 ('', '')。"""
    s = str(skill_id or "").strip()
    if ":" not in s:
        return ("", "")
    src, name = s.split(":", 1)
    src = src.strip().lower()
    name = name.strip()
    if src not in ("builtin", "user") or not name:
        return ("", "")
    return (src, name)


def _skill_files(kind, user):
    """返回 [(name, abs_path)]，按文件名排序。"""
    path = skills_dir(kind, user)
    if not os.path.isdir(path):
        return []
    out = []
    for fn in sorted(os.listdir(path), key=lambda s: s.lower()):
        stem, ext = os.path.splitext(fn)
        if ext.lower() not in _EXT_ALLOW or stem.startswith("."):
            continue
        fp = os.path.join(path, fn)
        if os.path.isfile(fp):
            out.append((stem, fp))
    return out


def _describe(text):
    """从正文里提炼一行简介：第一个标题行，否则第一行非空文本。"""
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("<!--"):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s).strip()
        if s:
            return s[:60]
    return ""


def list_skills(kind, include_builtin=True, include_user=True):
    """列出某类 skill。

    返回 [{id, name, source, description, size, mtime, path}]，内置在前、用户在后。
    """
    if kind not in KINDS:
        return []
    out = []
    for source, want in (("builtin", include_builtin), ("user", include_user)):
        if not want:
            continue
        for name, fp in _skill_files(kind, user=(source == "user")):
            try:
                st = os.stat(fp)
            except Exception:
                continue
            head = read_text(fp, 2048) or ""
            out.append({
                "id": make_skill_id(source, name),
                "name": name,
                "source": source,
                "description": _describe(head),
                "size": int(st.st_size),
                "mtime": int(st.st_mtime),
                "path": fp,
            })
    return out


def skill_path(kind, skill_id):
    """skill id → 绝对路径；找不到返回 None。"""
    source, name = split_skill_id(skill_id)
    if not source:
        return None
    for n, fp in _skill_files(kind, user=(source == "user")):
        if n == name:
            return fp
    return None


def _style_file_in(name, lang, user):
    """在 `style/en/`（或其他语言目录）里按同名找风格文件；找不到返回 None。"""
    path = style_dir(lang, user=user)
    if not os.path.isdir(path):
        return None
    for fn in os.listdir(path):
        stem, ext = os.path.splitext(fn)
        if ext.lower() in _EXT_ALLOW and stem == name:
            fp = os.path.join(path, fn)
            if os.path.isfile(fp):
                return fp
    return None


def read_skill(kind, skill_id, lang=None):
    """读取 skill 全文；不存在返回 None。

    lang="zh"/"en" 时按语言取：
    · 故事风格：英文优先取 `style/en/<同名>`（没有译文再回退中文文件）；
    · 其他类型：双语文件（带 LANG 标记）取对应段，单语文件原样返回。
    """
    source, name = split_skill_id(skill_id)
    fp = None
    if kind == STYLE and source and str(lang or "").lower().startswith("en"):
        fp = _style_file_in(name, "en", source == "user")
    fp = fp or skill_path(kind, skill_id)
    if not fp:
        return None
    text = read_text(fp)
    if not text:
        return None
    if lang:
        text = pick_language(text, lang)
    return text.strip() if text else None


# ── 语言分段（同一文件内双语）──────────────────────────────────
def split_languages(text):
    """按 ``<!-- JZL:LANG:ZH -->`` / ``<!-- JZL:LANG:EN -->`` 切分 → {ZH:..., EN:...}。

    无标记返回 {}（单语文件）。标记之前的标题/说明不参与取用。
    """
    body = str(text or "")
    if not _LANG_RE.search(body):
        return {}
    parts = _LANG_RE.split(body)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].upper()
        chunk = parts[i + 1].strip()
        if chunk:
            out[key] = chunk
    return out


def pick_language(text, lang="zh"):
    """按语言取正文：

    · 双语（有 LANG 标记）→ 取对应语言；该语言缺失时回退另一种；
    · 单语（无标记）→ 原样返回（纯中文 / 纯英文的 skill 都能直接用）。
    """
    langs = split_languages(text)
    if not langs:
        return str(text or "").strip()
    key = "EN" if str(lang or "").lower().startswith("en") else "ZH"
    other = "ZH" if key == "EN" else "EN"
    return (langs.get(key) or langs.get(other) or "").strip()


def default_skill_id(kind):
    """第一个内置 skill 的 id（无内置时回退第一个用户 skill）。"""
    for item in list_skills(kind):
        if item["source"] == "builtin":
            return item["id"]
    items = list_skills(kind)
    return items[0]["id"] if items else ""


def import_skill(kind, filename, text):
    """把上传内容保存为用户 skill。返回 (skill_id, err)。"""
    if kind not in KINDS:
        return "", f"未知 skill 类型：{kind}"
    name = safe_skill_name(filename)
    if not name:
        return "", "文件名无效（只支持 .txt / .md 文本文件）"
    body = str(text or "").strip()
    if not body:
        return "", "文件内容为空"
    if len(body) > 400_000:
        return "", "文件过大（上限 400KB）"
    # 保留用户原始扩展名（.txt 存 .txt、.md 存 .md），方便他们在文件管理器里认出自己的文件
    ext = os.path.splitext(os.path.basename(str(filename or "")))[1].lower()
    if ext not in _EXT_ALLOW:
        ext = ".md"
    try:
        folder = ensure_user_dir(kind)
        dest_name = name
        dest = os.path.join(folder, dest_name + ext)
        i = 2
        while os.path.exists(dest):
            dest_name = f"{name} ({i})"
            dest = os.path.join(folder, dest_name + ext)
            i += 1
        with open(dest, "w", encoding="utf-8", newline="\n") as f:
            f.write(body + "\n")
        return make_skill_id("user", dest_name), None
    except Exception as exc:
        return "", f"保存失败：{exc}"


def delete_skill(kind, skill_id):
    """删除用户 skill（内置 skill 不允许删除）。返回错误信息或 None。"""
    source, name = split_skill_id(skill_id)
    if source != "user":
        return "内置 skill 不能删除"
    fp = skill_path(kind, skill_id)
    if not fp:
        return "skill 不存在"
    try:
        os.remove(fp)
        return None
    except Exception as exc:
        return f"删除失败：{exc}"


# ── mode skill 解析 ─────────────────────────────────────────────
def parse_mode_skill(text):
    """拆解 skill 文本 → (task, rules)。

    · 含 ``<!-- JZL:TASK -->`` / ``<!-- JZL:RULES -->`` 标记：按标记切分；
      标记之前的文字视为标题/说明，不参与注入。
    · 无标记：全文作为 rules，task 返回空串（调用方用通用任务句兜底）。
    """
    body = str(text or "").strip()
    if not body:
        return ("", "")
    if not _MARK_RE.search(body):
        return ("", body)
    # re.split 带捕获组 → [前导, 标记1, 段1, 标记2, 段2, ...]
    parts = _MARK_RE.split(body)
    task, rules = "", ""
    for i in range(1, len(parts) - 1, 2):
        mark = parts[i].upper()
        chunk = parts[i + 1].strip()
        if not chunk:
            continue
        if mark == "TASK":
            task = (task + "\n\n" + chunk).strip() if task else chunk
        else:
            rules = (rules + "\n\n" + chunk).strip() if rules else chunk
    return (task, rules or body)
