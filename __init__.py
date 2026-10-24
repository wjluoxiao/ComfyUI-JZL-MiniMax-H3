"""JZL MiniMax — 节点注册入口（V1 经典 API）

采用 V1 的 NODE_CLASS_MAPPINGS 注册，兼容两种节点实现：
- V3 节点（io.ComfyNode，如参考编码）：执行层按类继承自动识别，Autogrow 照常生效
- V1 节点（经典 API，如漫剧调度四节点）：保留 **kwargs 动态端口与按名分类
"""

import os
import re
import sys
import time
import platform
# ── 云机多段生成显存碎片抑制（须早于任何 CUDA 分配执行才生效）──
# NVIDIA(CUDA) 构建且外部未显式配置时启用 expandable_segments，缓解缓存分配器碎片
# 导致的 CUDA error: invalid argument / 分配失败（本地 ROCm 构建不适用，自动跳过）。
try:
    if os.environ.get("PYTORCH_CUDA_ALLOC_CONF") is None:
        import torch as _jzl_t
        if getattr(_jzl_t.version, "hip", None) is None:
            os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
except Exception:
    pass

from server import PromptServer
from aiohttp import web

# ═══════════════════════════════════════════════════════════════════════════
#  加载自检与加载日志
#  目的：把「解压到 custom_nodes 了，但节点列表里一个都没有」变成一条能自解释的日志。
#  设计：按功能分组导入 → 单组失败不拖垮其他组；核心组失败才判定整包未加载。
# ═══════════════════════════════════════════════════════════════════════════

_JZL_PACK_DIR = os.path.dirname(os.path.abspath(__file__))
_JZL_PACK_NAME = os.path.basename(_JZL_PACK_DIR)
_JZL_LOAD_T0 = time.perf_counter()
_JZL_LOAD_FAILS = []      # [(组名, 模块名, 异常)]

# 我们对 ComfyUI 的硬性要求（写在这里是为了失败时能直接告诉用户要升到哪个版本）
_JZL_REQ_COMFY = "0.29.0（含官方 MiniMax H3 支持）"
_JZL_REQ_COMFY_API = "0.3.47（comfy_api.latest）"


def _jzl_comfy_version():
    try:
        import comfyui_version
        return str(getattr(comfyui_version, "__version__", "?"))
    except Exception:
        return "?"


def _jzl_pack_version():
    """从本目录 pyproject.toml 读版本（避免与常量重复导致漂移）。"""
    try:
        with open(os.path.join(_JZL_PACK_DIR, "pyproject.toml"), "r", encoding="utf-8") as f:
            m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', f.read())
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def _jzl_runtime_desc():
    desc = [f"ComfyUI {_jzl_comfy_version()}", f"Python {platform.python_version()}"]
    try:
        import torch
        desc.append(f"torch {torch.__version__}")
        try:
            if torch.cuda.is_available():
                desc.append(torch.cuda.get_device_name(0))
        except Exception:
            pass
    except Exception:
        desc.append("torch 未就绪")
    return " ｜ ".join(desc)


# ── 导入分组：(组名, 是否核心组, ((模块, (属性…)), …)) ──────────────────────
# 核心组（官方编码链路 + 端口）失败 ⇒ 整包判定未加载（节点全缺，必须显式报错，不能静默半残）。
_JZL_IMPORT_GROUPS = (
    ("核心编码/端口", True, (
        (".nodes", ("JZL_MiniMaxH3ReferenceToVideo", "JZL_MiniMaxH3ReferenceToVideo2",
                    "JZL_MiniMaxH3CondSync", "JZL_MiniMaxH3ImageToVideoDual")),
        (".nodes_hailuo_video", ("JZL_HailuoH3VideoParams", "JZL_HailuoH3VideoParamsPro")),
        (".nodes_list_dispatcher", ("JZL_ListDispatcher",)),
    )),
    ("桥段调度", False, (
        (".story_nodes", ("JZL_MiniMax_ShotFormatter", "JZL_MiniMax_SceneDispatcher",
                          "JZL_MiniMax_VideoDispatcher", "JZL_MiniMax_AudioDispatcher",
                          "JZL_MiniMax_SceneDispatcher2", "JZL_MiniMax_VideoDispatcher2",
                          "JZL_MiniMax_AudioDispatcher2")),
    )),
    ("LLM/剧本", False, (
        (".nodes_llama", ("JZL_LlamaModelLoaderPro", "JZL_MiniMax_ScriptProcessor",
                          "JZL_MiniMaxPreset", "JZL_MiniMaxRef2vaPreset",
                          "JZL_MiniMaxH3Preference", "JZL_MiniMaxAPISettings",
                          "_read_api_settings", "_write_api_settings")),
        (".nodes_prompt_enhancer", ("JZL_MiniMaxPromptEnhancer",)),
    )),
    ("导演台/资产管理", False, (
        (".nodes_asset_manager", ("JZL_MiniMaxAssetManager", "JZL_MiniMaxAssetManagerMax",
                                  "JZL_MiniMaxAssetManagerInfinite", "JZL_MiniMaxAssetManagerShortDrama",
                                  "JZL_MiniMaxAssetManagerMini", "JZL_MiniMaxVideoSaveDistributor",
                                  "JZL_MiniMaxVideoViewer", "JZL_MiniMaxScriptViewer",
                                  "_read_asset_settings", "_write_asset_settings",
                                  "_read_manager_settings", "_write_manager_settings",
                                  "_resolve_asset_path", "_safe_story_name", "_load_last_script")),
    )),
    ("音乐", False, (
        (".nodes_music", ("JZL_MiniMaxMusicCaption", "JZL_MiniMaxMusicCaptionDuet")),
        (".nodes_music_lyrics", ("JZL_MiniMaxMusicLyricsEditor",)),
    )),
    ("提示词/总线", False, (
        (".nodes_h3_editor", ("JZL_MiniMaxH3PromptEditor",)),
        (".nodes_ref_bus", ("JZL_MiniMaxH3RefBusOut", "JZL_MiniMaxH3RefBusIn")),
        (".nodes_ref2va_bus", ("JZL_MiniMaxH3Ref2vaBusOut", "JZL_MiniMaxH3Ref2vaBusIn")),
    )),
)

# 路由处理函数用到的辅助函数：对应模块导入失败时打桩，避免访问端点时 NameError
_JZL_ROUTE_STUBS = {
    "_read_api_settings": lambda: {},
    "_write_api_settings": lambda settings: settings or {},
    "_read_asset_settings": lambda: {},
    "_write_asset_settings": lambda settings: settings or {},
    "_read_manager_settings": lambda: {},
    "_write_manager_settings": lambda settings: settings or {},
    "_resolve_asset_path": lambda path: os.path.abspath(str(path or "")),
    "_safe_story_name": lambda name: re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name or "")).strip()[:64],
    "_load_last_script": lambda: None,
}


def _jzl_do_imports():
    """按组导入节点模块（单组失败不影响其他组）；返回核心组的异常（None = 核心正常）。"""
    import importlib
    fatal = None
    for group, required, modules in _JZL_IMPORT_GROUPS:
        for mod_name, attrs in modules:
            try:
                mod = importlib.import_module(mod_name, __package__)
            except Exception as e:
                _JZL_LOAD_FAILS.append((group, mod_name, e))
                if required and fatal is None:
                    fatal = e
                continue
            for attr in attrs:
                try:
                    globals()[attr] = getattr(mod, attr)
                except Exception as e:
                    _JZL_LOAD_FAILS.append((group, f"{mod_name}.{attr}", e))
                    if required and fatal is None:
                        fatal = e
    return fatal


def _jzl_install_route_stubs():
    for name, stub in _JZL_ROUTE_STUBS.items():
        if name not in globals():
            globals()[name] = stub


# 常见导入失败 → 处置建议（按关键字匹配异常文本，命中第一条）
_JZL_FAIL_HINTS = (
    ("No module named 'comfy_api'",
     f"ComfyUI 版本过旧：需要 ≥ {_JZL_REQ_COMFY_API} 才能用官方 V3 节点机制。请更新 ComfyUI 后重启。"),
    ("No module named 'comfy.audio'",
     "ComfyUI 版本过旧（comfy.audio 自 0.37.0 起才存在）。本插件已内置回退，若仍报此行说明还有其它新旧差异，建议更新 ComfyUI。"),
    ("No module named 'comfy",
     f"ComfyUI 核心模块缺失或版本过旧：请确认 ComfyUI ≥ {_JZL_REQ_COMFY}，并确认插件放在 custom_nodes 下（不要嵌套两层同名目录）。"),
    ("No module named 'comfyui_version'",
     "当前 python 不是 ComfyUI 自带的那个：请用 ComfyUI 自己的 python 启动（便携包 = python_embeded\\python.exe）。"),
    ("torchaudio",
     "torchaudio 缺失或与 torch 版本不匹配（手动升级 torch 后最常见）。请按 torch 版本重装："
     "pip install --force-reinstall torchaudio"),
    ("No module named 'PIL'",
     "缺少 Pillow：pip install Pillow"),
    ("No module named 'numpy'",
     "缺少 numpy：pip install numpy"),
    ("No module named 'llama_cpp'",
     "本插件已不再依赖 llama-cpp-python；出现此行说明目录里混入了旧版文件，请删掉插件目录后重新解压。"),
    ("cannot import name",
     "多为「覆盖安装后残留旧 .py / __pycache__」或 ComfyUI 与插件版本不匹配：请删掉本插件目录后重新解压，"
     "并删除目录内所有 __pycache__ 再重启。"),
)

_JZL_FAIL_FOOTER = (
    "  排查步骤：\n"
    "   1) 确认目录层级（最常见的坑）：\n"
    "      custom_nodes\\{0}\\__init__.py        ← 正确\n"
    "      custom_nodes\\{0}\\{0}\\__init__.py    ← 错误（解压时多套了一层，包不会被加载）\n"
    "   2) 确认用 ComfyUI 自带的 python 启动（便携包 = python_embeded\\python.exe）。\n"
    "   3) 跑一次自检并把输出发我：\n"
    "      <你的 ComfyUI python> \"{1}\"\n"
    "   4) 覆盖安装后请删除本插件目录下所有 __pycache__ 再重启。"
)


def _jzl_report_load_result(fatal):
    """加载日志：成功一行；局部失败逐条列出；整体失败打印带处置建议的整段报告。"""
    n_nodes = len(NODE_CLASS_MAPPINGS)
    if fatal is None and not _JZL_LOAD_FAILS:
        print(f"[JZL MiniMax H3] ✅ 加载成功：{n_nodes} 个节点 ｜ v{_jzl_pack_version()} ｜ "
              f"{_jzl_runtime_desc()} ｜ {time.perf_counter() - _JZL_LOAD_T0:.2f}s")
        print(f"[JZL MiniMax H3] 路径：{_JZL_PACK_DIR}")
        return

    _all = " ".join(f"{type(e).__name__}: {e}" for _, _, e in _JZL_LOAD_FAILS)
    _hint = next((h for k, h in _JZL_FAIL_HINTS if k in _all), "")
    _self_check = os.path.join(_JZL_PACK_DIR, "jzl_diagnose.py")

    if fatal is not None:
        print("\n" + "=" * 78)
        print(f"[JZL MiniMax H3] ❌ 节点包加载失败：{_JZL_PACK_NAME}（节点列表里不会出现本插件）")
        print(f"  路径：{_JZL_PACK_DIR}")
        print(f"  环境：v{_jzl_pack_version()} ｜ {_jzl_runtime_desc()}")
        print(f"  失败模块：{', '.join(sorted({m for _, m, _ in _JZL_LOAD_FAILS}))}")
        print(f"  原因：{type(fatal).__name__}: {fatal}")
        if _hint:
            print(f"  ⮕ 处置：{_hint}")
        print(_JZL_FAIL_FOOTER.format(_JZL_PACK_NAME, _self_check))
        print("=" * 78 + "\n")
        return

    _ok_groups = len(_JZL_IMPORT_GROUPS) - len({g for g, _, _ in _JZL_LOAD_FAILS})
    print(f"[JZL MiniMax H3] ⚠️ 部分模块加载失败（{_ok_groups} 组正常，已加载 {n_nodes} 个节点）"
          f" ｜ v{_jzl_pack_version()} ｜ {_jzl_runtime_desc()}")
    for group, mod, err in _JZL_LOAD_FAILS:
        print(f"[JZL MiniMax H3]    ✗ [{group}] {mod} → {type(err).__name__}: {err}")
    if _hint:
        print(f"[JZL MiniMax H3]    ⮕ 处置：{_hint}")
    print(f"[JZL MiniMax H3]    自检：<ComfyUI python> \"{_self_check}\"")


_JZL_FATAL_IMPORT_ERROR = _jzl_do_imports()
_jzl_install_route_stubs()

WEB_DIRECTORY = os.path.join(_JZL_PACK_DIR, "js")

# 节点表：(node_id, 显示名)。注册时按 globals() 里是否存在该类筛选 ——
# 这样任一模块导入失败时，其余节点照样注册（不会再「一个模块挂掉、整包消失」）。
_JZL_NODE_TABLE = (
    ("JZL_MiniMaxH3ReferenceToVideo", "JZL - 🎬 MiniMax H3 参考编码"),
    ("JZL_MiniMaxH3ReferenceToVideo2", "JZL - 🎬 MiniMax H3 参考编码2"),
    ("JZL_MiniMaxH3CondSync", "JZL - 🌊 海螺H3二采条件同步"),
    ("JZL_MiniMaxH3ImageToVideoDual", "JZL - 🎬 MiniMax H3 二采编码"),
    ("JZL_HailuoH3VideoParams", "JZL - 🌊 海螺H3视频参数"),
    ("JZL_HailuoH3VideoParamsPro", "JZL - 🌊 海螺H3视频参数Pro"),
    ("JZL_ListDispatcher", "JZL - 📋 列表分发"),
    ("JZL_MiniMax_ShotFormatter", "JZL - 📋 分段处理中心"),
    ("JZL_MiniMax_SceneDispatcher", "JZL - 🎯 场景元素调度"),
    ("JZL_MiniMax_VideoDispatcher", "JZL - 🎬 视频调度"),
    ("JZL_MiniMax_AudioDispatcher", "JZL - 🎧 音频调度"),
    ("JZL_MiniMax_SceneDispatcher2", "JZL - 🎯 场景元素调度2"),
    ("JZL_MiniMax_VideoDispatcher2", "JZL - 🎬 视频调度2"),
    ("JZL_MiniMax_AudioDispatcher2", "JZL - 🎧 音频调度2"),
    ("JZL_LlamaModelLoaderPro", "JZL - 🚀 模型加载Pro"),
    ("JZL_MiniMax_ScriptProcessor", "JZL - 🎬 剧本与镜头处理器"),
    ("JZL_MiniMaxPreset", "JZL - ✨ MiniMax-fl2va提示词预设"),
    ("JZL_MiniMaxRef2vaPreset", "JZL - ✨ MiniMax-ref2va提示词预设"),
    ("JZL_MiniMaxH3Preference", "JZL - 🎯 MiniMax H3 偏好设置"),
    ("JZL_MiniMaxAPISettings", "JZL - 🌐 LLM-API 设置"),
    ("JZL_MiniMaxMusicCaption", "JZL - 🎵 MiniMax Music3 提示词预设"),
    ("JZL_MiniMaxMusicCaptionDuet", "JZL - 🎵 MiniMax Music3 提示词预设（双人）"),
    ("JZL_MiniMaxMusicLyricsEditor", "JZL - 🎵 MiniMax Music3 歌词编辑"),
    ("JZL_MiniMaxH3PromptEditor", "JZL - 📝 MiniMax H3 手写提示词"),
    ("JZL_MiniMaxH3RefBusOut", "JZL - 🔗 MiniMax H3 参考总线（打包）"),
    ("JZL_MiniMaxH3RefBusIn", "JZL - 🔗 MiniMax H3 参考总线（解包）"),
    ("JZL_MiniMaxH3Ref2vaBusOut", "JZL - 🔗 MiniMax H3 ref2va参考总线（打包）"),
    ("JZL_MiniMaxH3Ref2vaBusIn", "JZL - 🔗 MiniMax H3 ref2va参考总线（解包）"),
    ("JZL_MiniMaxPromptEnhancer", "JZL - ✨ 提示词增强"),
    ("JZL_MiniMaxAssetManager", "JZL - 🤖 MiniMax-H3短剧导演台Pro"),
    ("JZL_MiniMaxAssetManagerMax", "JZL - 🤖 MiniMax-H3短剧导演台Max"),
    ("JZL_MiniMaxAssetManagerInfinite", "JZL - 🤖 MiniMax-H3无限时长"),
    ("JZL_MiniMaxAssetManagerShortDrama", "JZL - 🤖 MiniMax-H3短剧导演台"),
    ("JZL_MiniMaxAssetManagerMini", "JZL - 🤖 MiniMax-H3短剧导演台Mini"),
    ("JZL_MiniMaxVideoSaveDistributor", "JZL - 💾 视频保存分配"),
    ("JZL_MiniMaxVideoViewer", "JZL - 🎬 生成视频查看器"),
    ("JZL_MiniMaxScriptViewer", "JZL - 📝 生成剧本查看器"),
)

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
for _node_id, _node_display in _JZL_NODE_TABLE:
    _node_cls = globals().get(_node_id)
    if _node_cls is not None:
        NODE_CLASS_MAPPINGS[_node_id] = _node_cls
        NODE_DISPLAY_NAME_MAPPINGS[_node_id] = _node_display

_jzl_report_load_result(_JZL_FATAL_IMPORT_ERROR)

if _JZL_FATAL_IMPORT_ERROR is not None:
    # 核心模块没起来 → 不做半残注册，让 ComfyUI 明确标记本包 IMPORT FAILED
    # （原因与处置建议已在上面的报告里打印）
    raise _JZL_FATAL_IMPORT_ERROR

# ── 后端端点（分段处理中心「重拍」文件选择器） ─────────────────

HAS_TKINTER = False
try:
    import tkinter as tk
    from tkinter import filedialog
    HAS_TKINTER = True
except ImportError:
    pass  # 静默放行，不要让整个节点加载失败


@PromptServer.instance.routes.post("/jzl/choose_txt_file")
async def choose_txt_file(request):
    """选择 TXT 文件, 默认打开 MiniMax 故事输出目录"""
    if not HAS_TKINTER:
        return web.json_response({"path": "", "error": "当前环境缺少弹窗依赖"})
    try:
        data = await request.json()
        default_dir = data.get("default_dir", "")
    except Exception:
        default_dir = ""

    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        if default_dir and os.path.isdir(default_dir):
            file_path = filedialog.askopenfilename(
                initialdir=default_dir,
                title="选择分段提示词文件",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )
        else:
            file_path = filedialog.askopenfilename(
                title="选择分段提示词文件",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )
        root.destroy()
        return web.json_response({"path": file_path})
    except Exception as e:
        return web.json_response({"path": "", "error": f"弹窗调用失败: {str(e)}"})


@PromptServer.instance.routes.post("/jzl/minimax_default_dir")
async def minimax_default_dir(request):
    """返回 MiniMax 默认输出目录路径"""
    try:
        import folder_paths
        default_dir = os.path.join(folder_paths.get_output_directory(), "jzl")
        if not os.path.isdir(default_dir):
            os.makedirs(default_dir, exist_ok=True)
        return web.json_response({"dir": default_dir})
    except Exception:
        return web.json_response({"dir": ""})


# ── 后端端点（API 设置弹窗读写，Key 不明文进工作流） ─────────────────

@PromptServer.instance.routes.get("/jzl/api_settings")
async def jzl_api_settings_get(request):
    """读取 API 设置（弹窗打开时回显当前配置）"""
    return web.json_response({"ok": True, "settings": _read_api_settings()})


@PromptServer.instance.routes.post("/jzl/api_settings")
async def jzl_api_settings_post(request):
    """保存 API 设置到磁盘（存 ComfyUI user 目录，不进工作流）"""
    try:
        payload = await request.json()
        settings = _write_api_settings(payload if isinstance(payload, dict) else {})
        return web.json_response({"ok": True, "settings": settings})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


# ── 后端端点（漫剧资产管理弹窗读写 + 文件选择 + 图片预览） ─────────────────
# 路径解析统一用 nodes_asset_manager._resolve_asset_path（预览路由与资产池加载共用）

@PromptServer.instance.routes.post("/jzl/upload_asset")
async def jzl_upload_asset(request):
    """浏览器文件上传（替代 tkinter 弹窗，云机/无桌面环境可用）。

    前端用 <input type=file> 选文件后，以 multipart 表单 POST 到此接口
    （字段：file + kind），按类型分别保存到 ComfyUI input/jzl/image、/video、
    /audio 三个文件夹，返回 input 相对路径（如 jzl/image/xxx.png）——与官方
    「加载图像」一致：素材统一导入 input 文件夹、按相对路径引用，工作流可移植。
    """
    try:
        post = await request.post()
        file = post.get("file")
        kind = (post.get("kind") or "image").strip()
        if kind not in {"image", "video", "audio"}:
            kind = "image"
        if not file or not getattr(file, "file", None):
            return web.json_response({"error": "未收到文件"}, status=400)
        filename = os.path.basename((file.filename or "").strip())
        if not filename:
            return web.json_response({"error": "文件名为空"}, status=400)
        ext = os.path.splitext(filename)[1].lower()
        allow = {
            "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"},
            "video": {".mp4", ".mov", ".webm", ".avi", ".mkv"},
            "audio": {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"},
        }.get(kind, set())
        if ext not in allow:
            return web.json_response({"error": f"{kind} 类型不支持扩展名 {ext}"}, status=400)
        import folder_paths
        # input/jzl 下按类型分三个英文文件夹：image / video / audio
        sub = os.path.join("jzl", kind)
        out_dir = os.path.join(folder_paths.get_input_directory(), sub)
        os.makedirs(out_dir, exist_ok=True)
        # 重名自动加 (N)，与官方 /upload/image 一致
        dest = os.path.join(out_dir, filename)
        i = 1
        split = os.path.splitext(filename)
        while os.path.exists(dest):
            filename = f"{split[0]} ({i}){split[1]}"
            dest = os.path.join(out_dir, filename)
            i += 1
        with open(dest, "wb") as f:
            f.write(file.file.read())
        # input 相对路径（官方 LoadImage 同款），统一正斜杠，跨平台可移植
        rel = os.path.join(sub, filename).replace("\\", "/")
        return web.json_response({
            "ok": True,
            "path": rel,
            "name": filename,
            "subfolder": sub,
            "type": "input",
        })
    except Exception as exc:
        return web.json_response({"error": f"上传失败：{exc}"}, status=500)


@PromptServer.instance.routes.get("/jzl/asset_preview")
async def jzl_asset_preview(request):
    """图片资产缩略图预览：返回长边 256 的 JPEG（仅允许图片扩展名）。"""
    path = (request.query.get("path") or "").strip()
    if not path:
        return web.json_response({"error": "缺少 path"}, status=400)
    ext = os.path.splitext(path)[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        return web.json_response({"error": "仅支持图片预览"}, status=400)
    path = _resolve_asset_path(path)
    if not path:
        return web.json_response({"error": "文件不存在"}, status=404)
    try:
        from PIL import Image, ImageOps
        import io
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((256, 256), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return web.Response(body=buf.getvalue(), content_type="image/jpeg")
    except Exception as exc:
        return web.json_response({"error": f"预览失败：{exc}"}, status=500)


@PromptServer.instance.routes.get("/jzl/asset_full")
async def jzl_asset_full(request):
    """图片资产原图：返回原图 PNG（无损，用于弹窗查看大图；超长边 4096 防内存爆）。"""
    path = (request.query.get("path") or "").strip()
    if not path:
        return web.json_response({"error": "缺少 path"}, status=400)
    ext = os.path.splitext(path)[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        return web.json_response({"error": "仅支持图片"}, status=400)
    path = _resolve_asset_path(path)
    if not path:
        return web.json_response({"error": "文件不存在"}, status=404)
    try:
        from PIL import Image, ImageOps
        import io
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        if max(img.size) > 4096:
            img.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return web.Response(body=buf.getvalue(), content_type="image/png")
    except Exception as exc:
        return web.json_response({"error": f"原图加载失败：{exc}"}, status=500)


@PromptServer.instance.routes.get("/jzl/audio_preview")
async def jzl_audio_preview(request):
    """音频资产试听：返回音频文件本体（支持 Range 请求，供 <audio> 播放/拖动）。"""
    path = (request.query.get("path") or "").strip()
    if not path:
        return web.json_response({"error": "缺少 path"}, status=400)
    ext = os.path.splitext(path)[1].lower()
    if ext not in {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}:
        return web.json_response({"error": "仅支持音频预览"}, status=400)
    path = _resolve_asset_path(path)
    if not path:
        return web.json_response({"error": "文件不存在"}, status=404)
    try:
        return web.FileResponse(path)
    except Exception as exc:
        return web.json_response({"error": f"音频读取失败：{exc}"}, status=500)


@PromptServer.instance.routes.get("/jzl/video_preview")
async def jzl_video_preview(request):
    """视频资产预览：返回视频文件本体（支持 Range 请求，供 <video> 播放/拖动）。"""
    path = (request.query.get("path") or "").strip()
    if not path:
        return web.json_response({"error": "缺少 path"}, status=400)
    ext = os.path.splitext(path)[1].lower()
    if ext not in {".mp4", ".mov", ".webm", ".avi", ".mkv"}:
        return web.json_response({"error": "仅支持视频预览"}, status=400)
    path = _resolve_asset_path(path)
    if not path:
        return web.json_response({"error": "文件不存在"}, status=404)
    try:
        return web.FileResponse(path)
    except Exception as exc:
        return web.json_response({"error": f"视频读取失败：{exc}"}, status=500)


@PromptServer.instance.routes.get("/jzl/generated_videos")
async def jzl_generated_videos(request):
    """生成视频管理：列出 output/jzl/{故事名}/ 下全部 mp4。

    传 story_name → 返回该故事名下视频列表；不传 → 返回 jzl 下所有含视频的故事文件夹。
    （Max 节点「🎬 生成视频管理」面板读取同一目录：落盘目录 = output/jzl/{故事名}/）
    """
    try:
        import folder_paths
        out_root = os.path.abspath(folder_paths.get_output_directory())
        jzl_dir = os.path.join(out_root, "jzl")
        story_name = (request.query.get("story_name") or "").strip()
        if not story_name:
            # 无故事名 → 列出 jzl 下所有含 mp4 的故事文件夹（供下拉选择）
            stories = []
            if os.path.isdir(jzl_dir):
                for d in sorted(os.listdir(jzl_dir)):
                    dp = os.path.join(jzl_dir, d)
                    if not os.path.isdir(dp):
                        continue
                    n = sum(1 for f in os.listdir(dp) if f.lower().endswith(".mp4"))
                    if n:
                        stories.append({"story": d, "count": n})
            return web.json_response({"ok": True, "stories": stories})
        safe = _safe_story_name(story_name)
        d = os.path.join(jzl_dir, safe)
        videos = []
        if os.path.isdir(d):
            for fn in os.listdir(d):
                fp = os.path.join(d, fn)
                if fn.lower().endswith(".mp4") and os.path.isfile(fp):
                    try:
                        mtime = os.path.getmtime(fp)
                    except Exception:
                        mtime = 0
                    videos.append({
                        "name": fn,
                        "path": fp,  # 绝对路径（video_preview/video_thumb 可直接用）
                        "mtime": mtime,
                        "size": os.path.getsize(fp) if os.path.isfile(fp) else 0,
                    })
        # 最新生成的排前面
        videos.sort(key=lambda v: v["mtime"], reverse=True)
        return web.json_response({"ok": True, "story": safe, "videos": videos})
    except Exception as exc:
        return web.json_response({"error": f"视频列表失败：{exc}"}, status=500)


@PromptServer.instance.routes.get("/jzl/story_latest_script")
async def jzl_story_latest_script(request):
    """读取 {故事名} 最后一次生成的剧本文本，返回内容（供「生成视频查看器」复制/编辑剧本）。

    目录结构：output/jzl/{故事名}/第NNNNN次生成/{故事拆解|已增强剧本}/*.txt。
    取编号最大的「第N次生成」批次 → 优先读其「已增强剧本」目录（该批次开启过增强时增强结果写在这里），
    否则读「故事拆解」目录（未开启增强时只有拆解结果）。优先「生成故事拆解/增强后剧本」，否则最新。
    """
    try:
        import folder_paths
        out_root = os.path.abspath(folder_paths.get_output_directory())
    except Exception:
        return web.json_response({"ok": False, "error": "无法定位 output 目录"}, status=500)
    story_name = (request.query.get("story_name") or "").strip()
    if not story_name:
        return web.json_response({"ok": False, "error": "缺少 story_name"}, status=400)
    safe = _safe_story_name(story_name)
    story_dir = os.path.join(out_root, "jzl", safe)
    if not os.path.isdir(story_dir):
        return web.json_response({"ok": True, "story": safe, "script": "", "batch": None, "file": None, "source": None})
    # 找编号最大的「第N次生成」批次目录
    batches = []
    for _dn in os.listdir(story_dir):
        _dp = os.path.join(story_dir, _dn)
        if os.path.isdir(_dp) and _dn.startswith("第") and _dn.endswith("次生成"):
            try:
                batches.append((int(_dn[1:-len("次生成")]), _dp))
            except Exception:
                pass
    if not batches:
        return web.json_response({"ok": True, "story": safe, "script": "", "batch": None, "file": None, "source": None})
    batches.sort(key=lambda x: x[0])
    _batch_no, latest_dir = batches[-1]

    # 目录优先级：已增强剧本（开启过增强） > 故事拆解（未增强）
    def _pick_from(_dir):
        if not os.path.isdir(_dir):
            return None, None, ""
        files = []
        for _fn in os.listdir(_dir):
            _fp = os.path.join(_dir, _fn)
            if os.path.isfile(_fp) and (_fn.lower().endswith(".txt") or _fn.lower().endswith(".md")):
                try:
                    files.append((os.path.getmtime(_fp), _fp, _fn))
                except Exception:
                    pass
        if not files:
            return None, None, ""
        files.sort(key=lambda x: x[0], reverse=True)
        # 优先语义前缀文件（增强后剧本 / 生成故事拆解），否则最新
        for _mt, _fp, _fn in files:
            if ("增强后剧本" in _fn) or ("生成故事拆解" in _fn):
                with open(_fp, "r", encoding="utf-8-sig") as _f:
                    return _fn, _fp, _f.read()
        _mt, _fp, _fn = files[0]
        with open(_fp, "r", encoding="utf-8-sig") as _f:
            return _fn, _fp, _f.read()

    script = ""
    picked = None
    source = None
    enhanced_dir = os.path.join(latest_dir, "已增强剧本")
    decompose_dir = os.path.join(latest_dir, "故事拆解")
    # 优先已增强剧本目录（该批次存在 = 开启过增强）
    if os.path.isdir(enhanced_dir) and any(
            _fn.lower().endswith((".txt", ".md"))
            for _fn in os.listdir(enhanced_dir)
            if os.path.isfile(os.path.join(enhanced_dir, _fn))):
        picked, _fp, script = _pick_from(enhanced_dir)
        source = "已增强剧本"
    if (not script or picked is None) and os.path.isdir(decompose_dir):
        picked, _fp, script = _pick_from(decompose_dir)
        source = "故事拆解"
    return web.json_response({"ok": True, "story": safe, "script": script, "batch": _batch_no, "file": picked, "source": source})


@PromptServer.instance.routes.get("/jzl/video_thumb")
async def jzl_video_thumb(request):
    """生成视频管理缩略图：用 ffmpeg 抽 mp4 首帧返回 JPEG（供多宫格展示）。"""
    path = (request.query.get("path") or "").strip()
    if not path:
        return web.json_response({"error": "缺少 path"}, status=400)
    if not (str(path).lower().endswith(".mp4") and os.path.isfile(path)):
        return web.json_response({"error": "文件不存在或非 mp4"}, status=404)
    try:
        import io
        import imageio_ffmpeg
        import subprocess
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        proc = subprocess.Popen(
            [ffmpeg, "-y", "-ss", "0", "-i", path,
             "-frames:v", "1", "-vf", "scale=min(256\\,iw):-2",
             "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        data = proc.stdout.read()
        proc.wait(timeout=30)
        if not data:
            return web.json_response({"error": "抽帧失败"}, status=500)
        return web.Response(body=data, content_type="image/jpeg")
    except Exception as exc:
        return web.json_response({"error": f"缩略图失败：{exc}"}, status=500)


@PromptServer.instance.routes.get("/jzl/manager")
async def jzl_manager_get(request):
    """读取短剧管理器统一配置 + MiniMax-H3 模型列表 + 本地 LLM 模型列表"""
    try:
        import folder_paths
        from .llama_backend import chat_handlers as _ch
        from .nodes_asset_manager import _list_models, _selflift_upscaler_options, _semantic_bridge_adapters
        # 与 schema 下拉同源：优先 sheding.story_styles（含 styles/*.md），缺失才回退 presets.script
        try:
            from .sheding.story_styles import STORY_STYLES as _story_styles, STYLE_LABELS as _style_labels
        except Exception:
            from .presets.script import STORY_STYLES as _story_styles
            _style_labels = {}
        all_llms = folder_paths.get_filename_list("LLM")
        llm_list = [f for f in all_llms if "mmproj" not in f.lower()]
        mmproj_list = ["None"] + [f for f in all_llms if "mmproj" in f.lower()]
        diff_models = _list_models("diffusion_models")
        clip_models = _list_models("clip")
        vae_models = _list_models("vae")
        lora_models = _list_models("loras")
        story_styles = list(_story_styles.keys())
        # 下拉显示标签：短名 → 「短名（适合 xxx）」，供前端 combo getOptionLabel 使用
        story_style_labels = {k: (_style_labels.get(k) or k) for k in story_styles}
        save_dir = os.path.join(folder_paths.get_output_directory(), "jzl")
        upscaler_models = folder_paths.get_filename_list("latent_upscale_models")
        # SelfLift「放大模型」下拉：none + 磁盘上的 H3 放大模型（含二级子目录）
        selflift_upscaler_models = _selflift_upscaler_options()
        # 「生成设定 → 语义修正」适配器下拉：models/semantic_bridge/ 下的 .safetensors
        semantic_bridge_adapters = _semantic_bridge_adapters()
    except Exception:
        llm_list, mmproj_list, _ch = [], ["None"], ["None"]
        diff_models = clip_models = vae_models = lora_models = []
        story_styles = []
        story_style_labels = {}
        save_dir = "output/jzl"
        upscaler_models = []
        selflift_upscaler_models = ["none"]
        semantic_bridge_adapters = []
    return web.json_response({
        "ok": True,
        "settings": _read_manager_settings(),
        "llm_models": llm_list,
        "mmproj_models": mmproj_list,
        "chat_handlers": _ch,
        "diffusion_models": diff_models,
        "clip_models": clip_models,
        "vae_models": vae_models,
        "lora_models": lora_models,
        "story_styles": story_styles,
        "story_style_labels": story_style_labels,
        "save_dir": save_dir,
        "upscaler_models": upscaler_models,
        "selflift_upscaler_models": selflift_upscaler_models,
        "semantic_bridge_adapters": semantic_bridge_adapters,
    })


@PromptServer.instance.routes.post("/jzl/manager")
async def jzl_manager_post(request):
    """保存短剧管理器统一配置到磁盘"""
    try:
        payload = await request.json()
        settings = payload if isinstance(payload, dict) else {}
        _write_manager_settings(settings)
        return web.json_response({"ok": True, "settings": settings})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


@PromptServer.instance.routes.get("/jzl/assets")
async def jzl_assets_get(request):
    """读取资产配置（弹窗打开时回显）"""
    return web.json_response({"ok": True, "settings": _read_asset_settings()})


@PromptServer.instance.routes.post("/jzl/assets")
async def jzl_assets_post(request):
    """保存资产配置到磁盘"""
    try:
        payload = await request.json()
        settings = payload if isinstance(payload, dict) else {}
        _write_asset_settings(settings)
        return web.json_response({"ok": True, "settings": settings})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


@PromptServer.instance.routes.post("/jzl/export_assets")
async def jzl_export_assets(request):
    """把当前素材库导出为 output/jzl/{故事名}/参考素材_{故事名}_{年月日时分秒}.txt
    （UTF-8，可读格式，供跨机器导入）。故事名取自节点「故事名称」widget：
    有故事名 → 存入 output/jzl/{故事名}/ 子文件夹；无故事名 → 存 output/jzl/ 根目录。"""
    try:
        import folder_paths
        payload = await request.json()
        assets = payload.get("assets") if isinstance(payload, dict) else None
        story_name = (payload.get("story_name") if isinstance(payload, dict) else "") or ""
        if not isinstance(assets, dict):
            return web.json_response({"error": "缺少素材数据"}, status=400)
        import datetime
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        safe_story = _safe_story_name(story_name)
        _has_story = bool(str(story_name).strip())
        base_dir = os.path.join(folder_paths.get_output_directory(), "jzl")
        # 有故事名 → output/jzl/{故事名}/；无故事名 → output/jzl/
        out_dir = os.path.join(base_dir, safe_story) if _has_story else base_dir
        os.makedirs(out_dir, exist_ok=True)
        if _has_story:
            fname = f"参考素材_{safe_story}_{ts}.txt"
        else:
            fname = f"参考素材_{ts}.txt"
        path = os.path.join(out_dir, fname)

        def _cl(v):
            return (str(v) if v is not None else "").replace("|", "／").strip()

        lines = [
            "# JZL 素材库（MiniMax-H3短剧导演台Pro）v1",
            "# 每行：类型 | 编号 | 名称 | 描述 | 路径 | 启用(1/0)",
            "",
        ]
        for key, label in (("images", "图片"), ("videos", "视频"), ("audios", "音频")):
            lines.append(f"## {label}")
            for item in assets.get(key) or []:
                if not isinstance(item, dict):
                    continue
                lines.append(f"{_cl(item.get('type'))} | {_cl(item.get('letter'))} | {_cl(item.get('name'))} | "
                             f"{_cl(item.get('description'))} | {_cl(item.get('path'))} | "
                             f"{'1' if item.get('enabled', True) else '0'}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        try:
            rel = os.path.relpath(path, folder_paths.get_output_directory()).replace("\\", "/")
        except Exception:
            rel = ""
        return web.json_response({"ok": True, "path": path, "rel": rel})
    except Exception as exc:
        return web.json_response({"error": f"导出失败：{exc}"}, status=500)


# ── 输出目录文件浏览（跨环境：本机可调系统资源管理器；云机用网页列表 浏览/下载/删除/在线编辑文本）──
_JZL_FS_TEXT_EXT = {".txt", ".json", ".md", ".csv", ".srt", ".log"}


def _jzl_fs_root():
    import folder_paths
    return os.path.realpath(os.path.join(folder_paths.get_output_directory(), "jzl"))


def _jzl_fs_resolve(rel):
    """把相对 output/jzl 的路径解析为绝对路径并做沙箱校验；越界返回 None。"""
    root = _jzl_fs_root()
    rel = str(rel or "").replace("\\", "/").strip().strip("/")
    p = os.path.realpath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep):
        return None
    return p


@PromptServer.instance.routes.get("/jzl/fs_list")
async def jzl_fs_list(request):
    """列出 output/jzl 下的目录内容（供「输出目录」内置文件浏览器）。"""
    try:
        root = _jzl_fs_root()
        abs_p = _jzl_fs_resolve(request.query.get("path", ""))
        if abs_p is None or not os.path.isdir(abs_p):
            return web.json_response({"ok": False, "error": "目录不存在或越界"}, status=400)
        dirs, files = [], []
        for name in sorted(os.listdir(abs_p), key=lambda s: s.lower()):
            fp = os.path.join(abs_p, name)
            try:
                st = os.stat(fp)
            except Exception:
                continue
            relp = os.path.relpath(fp, root).replace("\\", "/")
            if os.path.isdir(fp):
                dirs.append({"name": name, "rel": relp, "mtime": int(st.st_mtime)})
            else:
                files.append({"name": name, "rel": relp, "size": int(st.st_size),
                              "mtime": int(st.st_mtime),
                              "ext": os.path.splitext(name)[1].lower().lstrip(".")})
        cur = os.path.relpath(abs_p, root).replace("\\", "/")
        if cur == ".":
            cur = ""
        parent = ""
        if cur:
            parent = os.path.relpath(os.path.dirname(abs_p), root).replace("\\", "/")
            if parent == ".":
                parent = ""
        return web.json_response({"ok": True, "rel": cur, "parent": parent, "dirs": dirs, "files": files})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


@PromptServer.instance.routes.get("/jzl/fs_download")
async def jzl_fs_download(request):
    abs_p = _jzl_fs_resolve(request.query.get("path", ""))
    if abs_p is None or not os.path.isfile(abs_p):
        return web.Response(status=404, text="file not found")
    # inline=1 → 浏览器内联显示（图片/视频/音频预览）；否则作为附件下载
    inline = str(request.query.get("inline", "")).lower() in ("1", "true", "yes")
    headers = {} if inline else {"Content-Disposition": 'attachment; filename="%s"' % os.path.basename(abs_p)}
    return web.FileResponse(abs_p, headers=headers)


@PromptServer.instance.routes.post("/jzl/fs_zip")
async def jzl_fs_zip(request):
    """把勾选的多个文件/目录打包成一个 zip 下载（云机批量下载用）。

    表单字段 paths = JSON 数组（相对 output/jzl 的路径）。文件按原文件名放入 zip，
    同名自动加 (2)(3)…；目录递归打包并保留其相对路径。视频本身已是压缩格式，用
    ZIP_STORED 不重复压缩（只做拷贝，速度取决于磁盘）。打包在临时文件里完成，
    响应结束后立即删除，不占长期空间。
    """
    import asyncio
    import json as _json
    import tempfile
    import time as _time
    import zipfile

    try:
        post = await request.post()
        raw = post.get("paths", "")
        try:
            wanted = _json.loads(raw) if isinstance(raw, str) else list(raw or [])
        except Exception:
            return web.json_response({"ok": False, "error": "paths 不是合法的 JSON 数组"}, status=400)
        paths = [str(p) for p in (wanted or []) if str(p or "").strip()]
        if not paths:
            return web.json_response({"ok": False, "error": "没有选中任何文件"}, status=400)

        root = _jzl_fs_root()
        items = []
        for rel in paths:
            abs_p = _jzl_fs_resolve(rel)
            if abs_p and abs_p != root and os.path.exists(abs_p):
                items.append((os.path.relpath(abs_p, root).replace("\\", "/"), abs_p))
        if not items:
            return web.json_response({"ok": False, "error": "选中的文件都已不存在"}, status=404)

        def _build(tmp_path):
            used = set()

            def _unique(name):
                """同一批里同名文件自动加 (2)(3)…，避免互相覆盖。"""
                if name not in used:
                    used.add(name)
                    return name
                stem, ext = os.path.splitext(name)
                n = 2
                while f"{stem} ({n}){ext}" in used:
                    n += 1
                uniq = f"{stem} ({n}){ext}"
                used.add(uniq)
                return uniq

            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for arc, abs_p in items:
                    if os.path.isdir(abs_p):
                        base = os.path.dirname(arc) or os.path.basename(abs_p.rstrip("/\\"))
                        for dirpath, _dirnames, filenames in os.walk(abs_p):
                            for fn in filenames:
                                full = os.path.join(dirpath, fn)
                                relf = os.path.relpath(full, abs_p).replace("\\", "/")
                                zf.write(full, _unique(f"{base}/{relf}"))
                        continue
                    zf.write(abs_p, _unique(os.path.basename(arc)))
            return os.path.getsize(tmp_path)

        fd, tmp = tempfile.mkstemp(prefix="jzl_batch_", suffix=".zip")
        os.close(fd)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _build, tmp)   # 不阻塞事件循环（ComfyUI 界面不卡）
            fname = f"jzl_batch_{_time.strftime('%Y%m%d_%H%M%S')}_{len(items)}files.zip"
            resp = web.StreamResponse(status=200, headers={
                "Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{fname}"',
                "Content-Length": str(os.path.getsize(tmp)),
                "Cache-Control": "no-store",
            })
            await resp.prepare(request)
            try:
                with open(tmp, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        await resp.write(chunk)
                await resp.write_eof()
            except Exception:
                pass
            return resp
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"打包失败：{exc}"}, status=500)


@PromptServer.instance.routes.post("/jzl/fs_delete")
async def jzl_fs_delete(request):
    try:
        import shutil
        payload = await request.json()
        root = _jzl_fs_root()
        abs_p = _jzl_fs_resolve(payload.get("path", ""))
        if abs_p is None or abs_p == root or not os.path.exists(abs_p):
            return web.json_response({"ok": False, "error": "路径无效或不允许删除 output/jzl 根目录"}, status=400)
        if os.path.isdir(abs_p):
            shutil.rmtree(abs_p)
        else:
            os.remove(abs_p)
        return web.json_response({"ok": True})
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"删除失败：{exc}"}, status=500)


@PromptServer.instance.routes.get("/jzl/fs_read")
async def jzl_fs_read(request):
    abs_p = _jzl_fs_resolve(request.query.get("path", ""))
    if abs_p is None or not os.path.isfile(abs_p):
        return web.json_response({"ok": False, "error": "文件不存在"}, status=404)
    if os.path.splitext(abs_p)[1].lower() not in _JZL_FS_TEXT_EXT:
        return web.json_response({"ok": False, "error": "仅支持编辑文本文件"}, status=400)
    try:
        with open(abs_p, "r", encoding="utf-8", errors="replace") as f:
            return web.json_response({"ok": True, "text": f.read()})
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"读取失败：{exc}"}, status=500)


@PromptServer.instance.routes.post("/jzl/fs_write")
async def jzl_fs_write(request):
    try:
        payload = await request.json()
        abs_p = _jzl_fs_resolve(payload.get("path", ""))
        if abs_p is None:
            return web.json_response({"ok": False, "error": "路径越界"}, status=400)
        if os.path.splitext(abs_p)[1].lower() not in _JZL_FS_TEXT_EXT:
            return web.json_response({"ok": False, "error": "仅支持写入文本文件"}, status=400)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
        with open(abs_p, "w", encoding="utf-8", newline="\n") as f:
            f.write(str(payload.get("text", "")))
        return web.json_response({"ok": True})
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"保存失败：{exc}"}, status=500)


def _jzl_win_focus_folder(path):
    """Windows：把刚打开的资源管理器窗口带到前台。

    os.startfile 从 ComfyUI 进程（通常处于后台）调起时，受 Windows 前台锁限制
    只在后台弹出、不抢占焦点。这里枚举 CabinetWClass 窗口，匹配目标文件夹名后
    用 AttachThreadInput + SetForegroundWindow 强制激活置顶。失败静默（不影响已打开窗口）。"""
    try:
        import ctypes
        import time
        from ctypes import wintypes
        time.sleep(0.35)  # 等资源管理器窗口建立
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        base = os.path.basename(os.path.normpath(path)).lower()
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _l):
            try:
                if user32.IsWindowVisible(hwnd):
                    cls = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(hwnd, cls, 64)
                    if cls.value == "CabinetWClass":  # 资源管理器主窗口
                        buf = ctypes.create_unicode_buffer(512)
                        user32.GetWindowTextW(hwnd, buf, 512)
                        found.append((hwnd, buf.value.lower()))
            except Exception:
                pass
            return True

        user32.EnumWindows(_enum, 0)
        target = None
        for hwnd, title in reversed(found):   # 从最新打开的开始匹配
            if base and (base in title or title in base):
                target = hwnd
                break
        if target is None and found:
            target = found[-1][0]
        if target is None:
            return
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_cur = kernel32.GetCurrentThreadId()
        attached = False
        try:
            if tid_fg and tid_fg != tid_cur:
                attached = bool(user32.AttachThreadInput(tid_fg, tid_cur, True))
            user32.ShowWindow(target, 9)          # SW_RESTORE
            user32.SetForegroundWindow(target)
            user32.BringWindowToTop(target)
        finally:
            if attached:
                user32.AttachThreadInput(tid_fg, tid_cur, False)
    except Exception:
        pass


@PromptServer.instance.routes.post("/jzl/fs_open")
async def jzl_fs_open(request):
    """在服务器本机用系统文件管理器打开目录。仅桌面环境有效；云机/无桌面会失败（前端提示用内置列表浏览）。"""
    try:
        import subprocess
        import sys
        payload = await request.json()
        abs_p = _jzl_fs_resolve(payload.get("path", ""))
        if abs_p is None or not os.path.isdir(abs_p):
            return web.json_response({"ok": False, "error": "目录不存在或越界"}, status=400)
        if sys.platform.startswith("win"):
            open_fn = getattr(os, "startfile", None)
            if open_fn is None:
                raise RuntimeError("当前系统不支持 startfile")
            open_fn(abs_p)
            _jzl_win_focus_folder(abs_p)   # startfile 常在后台弹出 → 尝试把该资源管理器窗口置顶/激活
        elif sys.platform == "darwin":
            subprocess.Popen(["open", abs_p])
        else:
            subprocess.Popen(["xdg-open", abs_p])
        return web.json_response({"ok": True})
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"无法在本机打开系统文件夹（云机/无桌面环境请用列表浏览）：{exc}"}, status=500)


def _parse_assets_text(text):
    """把素材库 txt 文本解析为 assets（图片/视频/音频三段）。"""
    assets = {"images": [], "videos": [], "audios": []}
    cur = None
    _map = {"图片": "images", "视频": "videos", "音频": "audios"}
    text = (text or "").lstrip("\ufeff")  # 容忍带 BOM 的文件
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            cur = _map.get(line[3:].strip())
            continue
        if line.startswith("#"):
            continue
        if cur is None:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6:
            continue
        typ, letter, name, desc, p, enabled = parts[:6]
        assets[cur].append({
            "type": typ, "letter": letter, "name": name,
            "description": desc, "path": p,
            "enabled": enabled == "1",
        })
    return assets


def _archive_asset(path, kind):
    """把资产文件归档到 input/jzl/{kind}/ 并返回 input 相对路径（jzl/{kind}/xxx）。

    - 已是 jzl/ 相对路径 → 原样（已归档）
    - 绝对路径 / 可解析的相对路径 → 若文件在 input/jzl 内则转相对；否则复制到
      input/jzl/{kind}/ 返回相对路径（复制后原文件可删，跨机器/原文件删除均不影响）
    - 找不到文件 → 原样返回（保留，加载时另行提示）
    """
    p = (path or "").strip()
    if not p:
        return p
    if p.startswith("jzl/") or p.startswith("jzl\\"):
        return p.replace("\\", "/")
    import folder_paths, shutil
    in_dir = os.path.abspath(folder_paths.get_input_directory())
    jzl_dir = os.path.join(in_dir, "jzl")

    def _abs():
        if os.path.isfile(p):
            return os.path.abspath(p)
        r = _resolve_asset_path(p)
        return os.path.abspath(r) if r and os.path.isfile(r) else ""

    abs_p = _abs()
    if not abs_p:
        return p
    if abs_p.startswith(jzl_dir + os.sep):
        return os.path.relpath(abs_p, in_dir).replace("\\", "/")
    try:
        kind_dir = os.path.join(jzl_dir, kind)
        os.makedirs(kind_dir, exist_ok=True)
        filename = os.path.basename(abs_p)
        dest = os.path.join(kind_dir, filename)
        i = 1
        split = os.path.splitext(filename)
        while os.path.exists(dest):
            filename = f"{split[0]} ({i}){split[1]}"
            dest = os.path.join(kind_dir, filename)
            i += 1
        shutil.copy2(abs_p, dest)
        return os.path.join("jzl", kind, filename).replace("\\", "/")
    except Exception:
        return p


@PromptServer.instance.routes.post("/jzl/import_assets")
async def jzl_import_assets(request):
    """从上传的 txt 文本解析素材库并返回（无 text 时兼容读取默认导出文件）。
    解析后统一归档到 input/jzl（复制文件 + 相对路径），原文件删除也不影响。"""
    try:
        import folder_paths
        payload = {}
        try:
            payload = await request.json() or {}
        except Exception:
            payload = {}
        text = payload.get("text") if isinstance(payload, dict) else None
        if text is None:
            # 兼容：无文本时读取默认导出文件
            path = os.path.join(folder_paths.get_output_directory(), "jzl", "素材库.txt")
            if not os.path.isfile(path):
                return web.json_response({"error": "output/jzl/素材库.txt 不存在"}, status=404)
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read()
        assets = _parse_assets_text(text)
        _kind_map = {"images": "image", "videos": "video", "audios": "audio"}
        for _key, _kind in _kind_map.items():
            for _item in assets.get(_key) or []:
                if isinstance(_item, dict):
                    _item["path"] = _archive_asset(_item.get("path"), _kind)
        return web.json_response({"ok": True, "assets": assets})
    except Exception as exc:
        return web.json_response({"error": f"导入失败：{exc}"}, status=500)


@PromptServer.instance.routes.post("/jzl/archive_assets")
async def jzl_archive_assets(request):
    """把资产 path 统一归档为 input/jzl 相对路径（复制文件到 input/jzl/{kind}）。

    供前端加载/保存资产时调用：无论素材来自上传、导入 txt（可能记录源文件绝对路径）
    还是历史遗留，都归档到 input/jzl 并返回 jzl/{kind}/xxx 相对路径 —— 之后读取/导出
    都用 input/jzl 里的副本，原文件删除也不影响。"""
    try:
        payload = await request.json() or {}
        assets = payload.get("assets") if isinstance(payload, dict) else None
        if not isinstance(assets, dict):
            return web.json_response({"error": "缺少素材数据"}, status=400)
        _kind_map = {"images": "image", "videos": "video", "audios": "audio"}
        archived = {"images": [], "videos": [], "audios": []}
        for key, kind in _kind_map.items():
            for item in assets.get(key) or []:
                if not isinstance(item, dict):
                    continue
                item = dict(item)
                item["path"] = _archive_asset(item.get("path"), kind)
                archived[key].append(item)
        return web.json_response({"ok": True, "assets": archived})
    except Exception as exc:
        return web.json_response({"error": f"归档失败：{exc}"}, status=500)


@PromptServer.instance.routes.post("/jzl/choose_asset_file")
async def choose_asset_file(request):
    """tkinter 文件选择器：按 kind(image/video/audio) 过滤文件类型"""
    if not HAS_TKINTER:
        return web.json_response({"path": "", "error": "当前环境缺少弹窗依赖"})
    try:
        data = await request.json()
        kind = data.get("kind", "image")
    except Exception:
        kind = "image"

    filetypes = {
        "image": [("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"), ("所有文件", "*.*")],
        "video": [("视频", "*.mp4 *.mov *.webm *.avi *.mkv"), ("所有文件", "*.*")],
        "audio": [("音频", "*.wav *.mp3 *.flac *.ogg *.m4a"), ("所有文件", "*.*")],
    }.get(kind, [("所有文件", "*.*")])
    titles = {"image": "选择图片", "video": "选择视频", "audio": "选择音频"}

    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        file_path = filedialog.askopenfilename(title=titles.get(kind, "选择文件"), filetypes=filetypes)
        root.destroy()
        return web.json_response({"path": file_path})
    except Exception as e:
        return web.json_response({"path": "", "error": f"弹窗调用失败: {str(e)}"})


@PromptServer.instance.routes.post("/jzl/choose_directory")
async def choose_directory(request):
    """tkinter 目录选择器：只允许选择 ComfyUI/output 目录内的文件夹（用于 ffmpeg 落盘/合并输出位置）"""
    if not HAS_TKINTER:
        return web.json_response({"path": "", "error": "当前环境缺少弹窗依赖"})
    try:
        import folder_paths
        out_root = os.path.abspath(folder_paths.get_output_directory())
        initial = os.path.join(out_root, "jzl")
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        dir_path = filedialog.askdirectory(
            title="选择文件夹（仅限 ComfyUI/output 目录内）",
            initialdir=initial if os.path.isdir(initial) else out_root)
        root.destroy()
        if not dir_path:
            return web.json_response({"path": ""})
        ap = os.path.abspath(dir_path)
        if ap != out_root and not ap.startswith(out_root + os.sep):
            return web.json_response({"path": "", "error": "只能选择 ComfyUI/output 目录内的文件夹"})
        return web.json_response({"path": dir_path})
    except Exception as e:
        return web.json_response({"path": "", "error": f"弹窗调用失败: {str(e)}"})


@PromptServer.instance.routes.get("/jzl/usage_md")
async def jzl_usage_md(request):
    """返回使用说明文档内容（不依赖前端静态资源服务，读取扩展目录 docs/USAGE.md）"""
    try:
        p = os.path.join(os.path.dirname(__file__), "docs", "USAGE.md")
        with open(p, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/plain; charset=utf-8")
    except Exception as e:
        return web.Response(status=404, text=f"文档读取失败: {str(e)}")


@PromptServer.instance.routes.get("/jzl/reshoot/load")
async def jzl_reshoot_load(request):
    """重拍模式：读取最后一次 LLM 拆解/增强后的完整提示词（output/jzl/最近提示词.json）。

    返回 {story_name, script, shots[], shot_count, time}；无记录时 shots 为空。
    """
    data = _load_last_script() or {"story_name": "", "script": "", "shots": [], "shot_count": 0, "time": ""}
    return web.json_response(data)


@PromptServer.instance.routes.get("/jzl/music_recipes")
async def jzl_music_recipes(request):
    """返回音乐提示词预设节点的「一键配方」表（单一数据源 = nodes_music._RECIPES）。

    前端拿到后：选定配方 → 自动填值并锁定；选「自定义 / Custom」→ 解锁。
    """
    try:
        return web.json_response({
            "single": JZL_MiniMaxMusicCaption._RECIPES,
            "duet": JZL_MiniMaxMusicCaptionDuet._RECIPES,
        })
    except Exception as e:
        return web.json_response({"single": {}, "duet": {}, "error": str(e)}, status=500)


@PromptServer.instance.routes.get("/jzl/usage_qr/{name}")
async def jzl_usage_qr(request):
    """返回 docs 目录下的收款二维码图片（OpenPose 式 FileResponse，不依赖前端静态服务）。

    用法：/jzl/usage_qr/DS01.png —— 只允许访问 docs 目录内的文件。
    """
    name = os.path.basename(request.match_info.get("name", ""))
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "docs"))
    full = os.path.abspath(os.path.join(base, name))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        return web.json_response({"error": "Not found"}, status=404)
    ext = os.path.splitext(name)[1].lower()
    ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "webp": "image/webp", "gif": "image/gif"}.get(ext, "application/octet-stream")
    return web.FileResponse(full, headers={"Content-Type": ctype})


# ── Skill 管理（拆解 / 扩写 / 故事风格 的「强化层」设定词）────────────
# 内置 skill 随插件发布在 sheding/skills/<kind>/；用户导入的存 ComfyUI user/jzl_skills/<kind>/
# 详见 sheding/skills_loader.py 与 sheding/skills/README.md

def _jzl_skills():
    from .sheding import skills_loader
    return skills_loader


def _jzl_decode_text(raw):
    """上传文本解码：UTF-8(BOM) → GBK → Latin-1 兜底（中文用户常见 GBK txt）。"""
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return ""


@PromptServer.instance.routes.get("/jzl/skills")
async def jzl_skills_list(request):
    """列出某类 skill；不传 kind 时返回类型表。"""
    kind = (request.query.get("kind") or "").strip()
    try:
        sk = _jzl_skills()
        if not kind:
            return web.json_response({"ok": True,
                                      "kinds": {k: sk.KIND_LABELS[k] for k in sk.KINDS}})
        if kind not in sk.KINDS:
            return web.json_response({"ok": False, "error": f"未知 skill 类型：{kind}"}, status=400)
        return web.json_response({
            "ok": True,
            "kind": kind,
            "label": sk.KIND_LABELS[kind],
            "items": sk.list_skills(kind),
            "dirs": {"builtin": sk.skills_dir(kind, user=False),
                     "user": sk.skills_dir(kind, user=True)},
        })
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


@PromptServer.instance.routes.get("/jzl/skills/read")
async def jzl_skills_read(request):
    """读取 skill 全文（用于面板内预览）。"""
    kind = (request.query.get("kind") or "").strip()
    skill_id = (request.query.get("id") or "").strip()
    try:
        sk = _jzl_skills()
        if kind not in sk.KINDS:
            return web.json_response({"ok": False, "error": f"未知 skill 类型：{kind}"}, status=400)
        text = sk.read_skill(kind, skill_id)
        if text is None:
            return web.json_response({"ok": False, "error": "skill 不存在"}, status=404)
        return web.json_response({"ok": True, "kind": kind, "id": skill_id, "text": text})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


@PromptServer.instance.routes.post("/jzl/skills/import")
async def jzl_skills_import(request):
    """浏览器上传 .txt/.md → 保存为用户 skill（同名自动加序号）。"""
    try:
        post = await request.post()
        kind = (post.get("kind") or "").strip()
        file = post.get("file")
        sk = _jzl_skills()
        if kind not in sk.KINDS:
            return web.json_response({"ok": False, "error": f"未知 skill 类型：{kind}"}, status=400)
        if not file or not getattr(file, "file", None):
            return web.json_response({"ok": False, "error": "未收到文件"}, status=400)
        filename = os.path.basename((file.filename or "").strip())
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".txt", ".md", ".markdown"):
            return web.json_response({"ok": False, "error": "只支持 .txt / .md 文本文件"}, status=400)
        raw = file.file.read(1_048_576)      # 上限 1MB
        text = _jzl_decode_text(raw)
        skill_id, err = sk.import_skill(kind, filename, text)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)
        return web.json_response({"ok": True, "id": skill_id,
                                  "items": sk.list_skills(kind)})
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"导入失败：{exc}"}, status=500)


@PromptServer.instance.routes.post("/jzl/skills/delete")
async def jzl_skills_delete(request):
    """删除用户 skill（内置 skill 不允许删除）。"""
    try:
        payload = await request.json()
        kind = str(payload.get("kind") or "").strip()
        skill_id = str(payload.get("id") or "").strip()
        sk = _jzl_skills()
        if kind not in sk.KINDS:
            return web.json_response({"ok": False, "error": f"未知 skill 类型：{kind}"}, status=400)
        err = sk.delete_skill(kind, skill_id)
        if err:
            return web.json_response({"ok": False, "error": err}, status=400)
        return web.json_response({"ok": True, "items": sk.list_skills(kind)})
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"删除失败：{exc}"}, status=500)
