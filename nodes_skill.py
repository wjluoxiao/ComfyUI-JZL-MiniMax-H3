"""ComfyUI loader for the bundled JZL MiniMax H3 prompt skills."""

import hashlib
import os

from comfy_api.latest import io


_BUNDLED_SKILL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "jzl")
# 历史版本把 skill 目录写死成了某个安装路径；仅当它确实存在且与插件自身目录不同时才作为附加目录，
# 顺序上**插件自身目录优先**（运行时就是当前安装，跨机器安装也能正确读写）。
_LEGACY_SKILL_DIR = r"Z:\AI_work\ComfyUI\custom_nodes\ComfyUI-JZL-MiniMax-H3\skills\jzl"
_SKILL_EXTENSIONS = {".md", ".txt"}
_MAX_SKILL_BYTES = 1024 * 1024
_EMPTY_SKILL = "（未找到 .md/.txt skill 文件）"


def _skill_dirs():
    dirs = [_BUNDLED_SKILL_DIR]
    try:
        if os.path.isdir(_LEGACY_SKILL_DIR) and os.path.normcase(_LEGACY_SKILL_DIR) != os.path.normcase(_BUNDLED_SKILL_DIR):
            dirs.append(_LEGACY_SKILL_DIR)
    except Exception:
        pass
    return dirs


def _skill_files():
    names = set()
    for directory in _skill_dirs():
        try:
            names.update(
                entry.name for entry in os.scandir(directory)
                if entry.is_file(follow_symlinks=False)
                and os.path.splitext(entry.name)[1].lower() in _SKILL_EXTENSIONS
            )
        except FileNotFoundError:
            continue
    return sorted(names, key=str.casefold) or [_EMPTY_SKILL]


def _skill_path(name):
    filename = str(name or "").strip()
    if not filename or filename == _EMPTY_SKILL or os.path.basename(filename) != filename:
        raise ValueError("请选择技能目录中的 .md 或 .txt 文件。")
    if os.path.splitext(filename)[1].lower() not in _SKILL_EXTENSIONS:
        raise ValueError("技能文件只支持 .md 和 .txt 格式。")
    for directory in _skill_dirs():
        path = os.path.join(directory, filename)
        if os.path.islink(path):
            continue
        if os.path.isfile(path):
            return path
    raise FileNotFoundError(f"技能文件不存在或不允许读取：{filename}")


def _read_skill(filename):
    path = _skill_path(filename)
    size = os.path.getsize(path)
    if size > _MAX_SKILL_BYTES:
        raise ValueError(f"技能文件超过 1 MiB 限制：{filename}")
    with open(path, "rb") as stream:
        raw = stream.read(_MAX_SKILL_BYTES + 1)
    if len(raw) > _MAX_SKILL_BYTES:
        raise ValueError(f"技能文件超过 1 MiB 限制：{filename}")
    try:
        text = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(f"技能文件必须使用 UTF-8 编码：{filename}") from exc
    if not text:
        raise ValueError(f"技能文件内容为空：{filename}")
    return text


def save_skill_upload(raw, filename, dirs=None, max_bytes=_MAX_SKILL_BYTES, decoder=None):
    """把浏览器上传的一段字节存成内置目录里的 skill 文件。

    云机/无桌面环境的关键路径：文件由**用户浏览器**选中后 multipart 上传，服务器只负责落盘，
    完全不依赖 tkinter/系统弹窗。

    返回 {"ok", "id"(存下来的文件名), "path"(绝对路径), "items"(目录下全部 skill), "error"}。
    同名不覆盖，自动加 `_2`、`_3` 后缀。
    """
    name = os.path.basename(str(filename or "").strip()) or "自定义skill.md"
    ext = os.path.splitext(name)[1].lower()
    if ext not in _SKILL_EXTENSIONS | {".markdown"}:
        return {"ok": False, "id": "", "path": "", "items": [], "error": "只支持 .md / .markdown / .txt"}
    candidates = list(dirs if dirs is not None else _skill_dirs())
    target = next((d for d in candidates if os.path.isdir(d)), candidates[-1] if candidates else "")
    if not target:
        return {"ok": False, "id": "", "path": "", "items": [], "error": "找不到可写入的 skill 目录"}
    try:
        os.makedirs(target, exist_ok=True)
        data = raw.read(max_bytes) if hasattr(raw, "read") else raw[:max_bytes]
        text = (decoder or _decode_text)(data)
        if not str(text or "").strip():
            return {"ok": False, "id": "", "path": "", "items": [],
                    "error": "文件内容为空或无法解码（需 UTF-8 / GBK 文本）"}
        out = os.path.join(target, name)
        base, _e = os.path.splitext(name)
        n = 2
        while os.path.exists(out):
            out = os.path.join(target, f"{base}_{n}{_e}")
            n += 1
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return {"ok": True, "id": os.path.basename(out), "path": out,
                "items": list_skills_in_dir(target), "error": ""}
    except Exception as exc:
        return {"ok": False, "id": "", "path": "", "items": [], "error": f"导入失败：{exc}"}


def _decode_text(raw):
    """尽力解码上传内容：UTF-8（含 BOM）优先，失败再试 GBK。"""
    for enc in ("utf-8-sig", "gbk", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def list_skills_in_dir(directory):
    """列出指定目录下的 .md/.txt 技能文件名（供「选择 skill 文件目录」的「已有 skill」下拉用）。

    目录不存在 / 不可读时返回空列表（调用方负责给出提示），绝不抛异常。
    """
    try:
        d = str(directory or "").strip()
        if not d or not os.path.isdir(d):
            return []
        names = [
            entry.name for entry in os.scandir(d)
            if entry.is_file(follow_symlinks=False)
            and os.path.splitext(entry.name)[1].lower() in _SKILL_EXTENSIONS
        ]
        return sorted(names, key=str.casefold)
    except OSError:
        return []


def read_skill_in_dir(directory, filename):
    """从用户自己选的目录里读一个技能文件全文。

    与 `_read_skill()` 同一套限制（只允许 .md/.txt、≤1 MiB、UTF-8、内容非空），
    只是把搜索范围换成用户明确指定的目录 —— 支持把技能放在任意位置，不必复制进插件目录。
    """
    d = str(directory or "").strip()
    name = str(filename or "").strip()
    if not d or not os.path.isdir(d):
        raise FileNotFoundError(f"技能目录不存在：{d or '（未选择）'}")
    if not name or os.path.basename(name) != name:
        raise ValueError("请选择技能目录中的 .md 或 .txt 文件。")
    if os.path.splitext(name)[1].lower() not in _SKILL_EXTENSIONS:
        raise ValueError("技能文件只支持 .md 和 .txt 格式。")
    path = os.path.join(d, name)
    if os.path.islink(path) or not os.path.isfile(path):
        raise FileNotFoundError(f"技能文件不存在或不允许读取：{name}")
    size = os.path.getsize(path)
    if size > _MAX_SKILL_BYTES:
        raise ValueError(f"技能文件超过 1 MiB 限制：{name}")
    with open(path, "rb") as stream:
        raw = stream.read(_MAX_SKILL_BYTES + 1)
    if len(raw) > _MAX_SKILL_BYTES:
        raise ValueError(f"技能文件超过 1 MiB 限制：{name}")
    try:
        text = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(f"技能文件必须使用 UTF-8 编码：{name}") from exc
    if not text:
        raise ValueError(f"技能文件内容为空：{name}")
    return text


class JZL_MiniMaxSkillLoader(io.ComfyNode):
    """Load a local Markdown or text skill as an LLM system prompt."""

    @classmethod
    def define_schema(cls):
        files = _skill_files()
        default = "人物替换.md" if "人物替换.md" in files else files[0]
        return io.Schema(
            node_id="JZL_MiniMaxSkillLoader",
            display_name="JZL - ✨ MiniMax-skill加载",
            category="JZL/MiniMax",
            description="读取 Z:\\AI_work\\ComfyUI\\custom_nodes\\ComfyUI-JZL-MiniMax-H3\\skills\\jzl 或插件自带 skills/jzl 中的 .md/.txt 技能全文，输出为 STRING，连接到 XB-llama「指令推理」的 system_prompt。",
            inputs=[
                io.Combo.Input(
                    "skill_file",
                    options=files,
                    default=default,
                    tooltip="优先从 Z:\\AI_work\\ComfyUI\\custom_nodes\\ComfyUI-JZL-MiniMax-H3\\skills\\jzl 读取；目录不可用时使用插件自带 skills/jzl。编辑文件后重新运行即可读取新内容；新增文件需重启 ComfyUI刷新列表。",
                ),
            ],
            outputs=[
                io.String.Output("system_prompt", display_name="系统提示词"),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, skill_file="", **kwargs):
        try:
            path = _skill_path(skill_file)
            with open(path, "rb") as stream:
                digest = hashlib.sha256(stream.read(_MAX_SKILL_BYTES + 1)).hexdigest()
            return digest
        except (OSError, ValueError):
            return f"invalid:{skill_file}"

    @classmethod
    def execute(cls, skill_file):
        return io.NodeOutput(_read_skill(skill_file))
