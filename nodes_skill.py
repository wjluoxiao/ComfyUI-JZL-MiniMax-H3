"""ComfyUI loader for the bundled JZL MiniMax H3 prompt skills."""

import hashlib
import os
import re

from comfy_api.latest import io


_BUNDLED_SKILL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "jzl")
# 历史版本把 skill 目录写死成了某个安装路径；仅当它确实存在且与插件自身目录不同时才作为附加目录，
# 顺序上**插件自身目录优先**（运行时就是当前安装，跨机器安装也能正确读写）。
_LEGACY_SKILL_DIR = r"Z:\AI_work\ComfyUI\custom_nodes\ComfyUI-JZL-MiniMax-H3\skills\jzl"
# 自定义 skill 的专用子目录（放在内置目录下面，用户直接往这里丢 .md 就会被识别）
_CUSTOM_SUBDIR = "custom"
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


def builtin_skill_dir():
    """内置 skill 目录 = 插件自带 skills/jzl（里面的 .md/.txt 就是「内置skill」下拉的全部内容）。"""
    return _skill_dirs()[0]


def custom_skill_dirs():
    """「自定义skill」专用子目录（每个候选根目录下的 custom/），优先插件自身那个。"""
    out = []
    for root in _skill_dirs():
        out.append(os.path.join(root, _CUSTOM_SUBDIR))
    return out


def ensure_custom_skill_dir():
    """确保自定义 skill 目录存在（启动时/导入时调用）。

    用户点开面板就能看到这个文件夹在哪，往里丢文件即可；不存在的层级会逐级创建。
    """
    for d in custom_skill_dirs():
        try:
            os.makedirs(d, exist_ok=True)
            # 放一个说明文件，用户打开文件夹就知道该怎么用（不会被当成 skill：扩展名不是 .md/.txt）
            hint = os.path.join(d, "README")   # 故意不带扩展名：.md/.txt 会被当成 skill 列进下拉
            if not os.path.exists(hint):
                with open(hint, "w", encoding="utf-8", newline="\n") as f:
                    f.write(
                        "把自定义 skill 放在这个文件夹里（.md 或 .txt，UTF-8，≤1MB）。\n"
                        "节点里的「自定义skill」下拉会自动列出这里的所有文件，增删文件后点「🔄 刷新」即可。\n"
                    )
            return d
        except OSError:
            continue
    return ""


_DESC_FILE = os.path.join(_CUSTOM_SUBDIR, "..", "_meta", "skill说明.txt")
_DESC_NO_HINT = "（暂无说明）"


def skill_desc_path(directory=None):
    """skill 说明文件路径：<内置目录>/_meta/skill说明.txt（放在子目录里，不会被当成 skill 列出来）。"""
    root = str(directory or builtin_skill_dir() or "")
    return os.path.normpath(os.path.join(root, "_meta", "skill说明.txt"))


def read_skill_descriptions(directory=None):
    """读 {短名: 说明}。

    文件格式（一行一条，`=` 或制表符分隔，`#` 开头是注释）：
        01-单人物替换 = 把视频里那一个主体人物整体换成参考图中的人
    键会同时登记「带编号的文件名」「去掉编号的短名」，所以配置里存哪种都能查到。
    """
    out = {}
    path = skill_desc_path(directory)
    try:
        if not os.path.isfile(path):
            return out
        size = os.path.getsize(path)
        if size > _MAX_SKILL_BYTES:
            return out
        with open(path, "rb") as stream:
            text = stream.read(_MAX_SKILL_BYTES).decode("utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = "\t" if "\t" in line else ("=" if "=" in line else None)
        if not sep:
            continue
        left, right = line.split(sep, 1)
        key = os.path.splitext(left.strip())[0]
        desc = right.strip()
        if not key or not desc:
            continue
        out[key] = desc
        out.setdefault(strip_skill_number(key), desc)
    return out


def strip_skill_number(name):
    """去掉文件名的 `01-` / `02-` 这类排序编号。"""
    stem = str(name or "")
    return re.sub(r"^\d{1,3}-", "", stem)


def skill_description(name, directory=None):
    """查一个 skill 的说明；查不到返回「（暂无说明）」。"""
    descs = read_skill_descriptions(directory)
    key = os.path.splitext(str(name or ""))[0]
    return (descs.get(key)
            or descs.get(strip_skill_number(key))
            or descs.get(key.replace("skill", ""))
            or _DESC_NO_HINT)


def list_builtin_skills():
    """内置 skill 文件名列表（动态扫描 skills/jzl，不含子目录）。"""
    return list_skills_in_dir(builtin_skill_dir())


def list_custom_skills(directory=None):
    """自定义 skill 文件名列表。

    directory 给了就只扫那一个目录（用户自己指定的 skill 目录，随节点配置保存）；
    没给就退回默认的几个候选目录（skills/jzl/custom）。
    """
    if str(directory or "").strip():
        return list_skills_in_dir(str(directory).strip())
    names = set()
    for d in custom_skill_dirs():
        names.update(list_skills_in_dir(d))
    return sorted(names, key=str.casefold)


def list_subdirs(path=None, max_entries=400):
    """目录浏览用：返回 (当前目录, 子目录列表, 父目录)。

    只列**目录**（skill 是文件，用户要选的是"存 skill 的文件夹"）。
    给空 path 时从「用户主目录」起步；不可读的项跳过，绝不抛异常给前端。
    """
    cur = str(path or "").strip()
    if not cur:
        cur = os.path.expanduser("~")
    cur = os.path.abspath(os.path.expanduser(cur))
    dirs = []
    try:
        with os.scandir(cur) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if entry.name.startswith("."):
                    continue
                dirs.append(entry.name)
                if len(dirs) >= max_entries:
                    break
    except OSError:
        dirs = []
    dirs.sort(key=str.casefold)
    parent = os.path.dirname(cur)
    if parent == cur:
        parent = ""
    return cur, dirs, parent


def resolve_builtin_skill(name):
    """内置 skill 名 → 绝对路径（只认内置目录里真实存在、非符号链接的文件）。找不到抛异常。"""
    return _skill_path_in(builtin_skill_dir(), name)


def resolve_custom_skill(name, directory=None):
    """自定义 skill 名 → 绝对路径（在自定义目录里找）。找不到抛异常。"""
    if str(directory or "").strip():
        return _skill_path_in(str(directory).strip(), name)
    last = None
    for d in custom_skill_dirs():
        try:
            return _skill_path_in(d, name)
        except Exception as exc:      # 换下一个候选目录继续找
            last = exc
    raise last or FileNotFoundError(f"自定义 skill 不存在：{name}")


def read_builtin_skill(name):
    return _read_skill_in(builtin_skill_dir(), name)


def read_custom_skill(name, directory=None):
    if str(directory or "").strip():
        return _read_skill_in(str(directory).strip(), name)
    last = None
    for d in custom_skill_dirs():
        try:
            return _read_skill_in(d, name)
        except Exception as exc:
            last = exc
    raise last or FileNotFoundError(f"自定义 skill 不存在：{name}")


def _skill_path_in(directory, name):
    filename = str(name or "").strip()
    if not filename or os.path.basename(filename) != filename:
        raise ValueError("请选择一个 .md 或 .txt 文件。")
    if os.path.splitext(filename)[1].lower() not in _SKILL_EXTENSIONS:
        raise ValueError("skill 文件只支持 .md 和 .txt 格式。")
    path = os.path.join(str(directory or ""), filename)
    if os.path.islink(path) or not os.path.isfile(path):
        raise FileNotFoundError(f"skill 文件不存在或不允许读取：{filename}")
    return path


def _read_skill_in(directory, name):
    path = _skill_path_in(directory, name)
    size = os.path.getsize(path)
    if size > _MAX_SKILL_BYTES:
        raise ValueError(f"skill 文件超过 1 MiB 限制：{name}")
    with open(path, "rb") as stream:
        raw = stream.read(_MAX_SKILL_BYTES + 1)
    if len(raw) > _MAX_SKILL_BYTES:
        raise ValueError(f"skill 文件超过 1 MiB 限制：{name}")
    try:
        text = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise ValueError(f"skill 文件必须使用 UTF-8 编码：{name}") from exc
    if not text:
        raise ValueError(f"skill 文件内容为空：{name}")
    return text


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
        default = files[0] if files else ""
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
