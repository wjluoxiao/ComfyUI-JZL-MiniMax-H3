"""JZL MiniMax — 漫剧资产管理节点（modal 配置 + 全局资产池 + 无线传输）。

一个节点替代原来的「24×LoadImage + 8×LoadAudio + 参考总线」子图：
- modal 弹窗动态管理 图片/视频/音频 槽位（数量可调，每项可选文件、类型、开关、自定义名）
- execute 时加载所有「启用」的资产 → 写入全局资产池 JZL_ASSET_POOL
- 输出「资产清单」（JSON 字符串，只含名字/类型/启用状态，不含 tensor）供调度器连线
- 调度器（SceneDispatcher/AudioDispatcher/VideoDispatcher）按资产名从全局池取 tensor

资产名格式：`图片1角色孙悟空` / `视频1分镜战斗参考` / `音频1角色孙悟空`
（前缀 + 序号 + 类型 + 自定义名）
"""

import os
import re
import json
import glob
import math
import time
import wave
import shutil
import subprocess
import tempfile
import logging

import torch
import torchaudio
import folder_paths
import node_helpers
import comfy.model_management
import comfy.sample
import comfy.samplers
import comfy.utils
import comfy.nested_tensor
# comfy.audio 是 ComfyUI 0.37.0（2026-09-21）才加入的核心模块：**不能**在模块顶层硬导入，
# 否则 0.29.0~0.36.x（已有 MiniMax H3 支持）的用户会 ModuleNotFoundError → 整个节点包不加载（一个节点都没有）。
# 缺失时自动回退 torchaudio（见 _encode_ref_audio），语义一致。
try:
    import comfy.audio as _comfy_audio
except Exception:
    _comfy_audio = None
from PIL import Image, ImageOps
from comfy_api.latest import io

# 复用 nodes.py 100% 复刻官方的模块级辅助（编码/画布/帧数）
from .nodes import (
    CANVAS_MULTIPLE,
    REF_IMAGE_SHORT_EDGE,
    _empty_av_latent,
    _resize,
    adapt_canvas,
    temporal_shape,
    align_frame_count,
)

# ── 日志静音：压制 ComfyUI 模型加载/显存管理的重复 INFO 噪音 ────────
# （如「Model X prepared for dynamic VRAM loading…」「Requested to load X」「0 models unloaded」），
# 它们来自 comfy.model_patcher / comfy.model_management 的 logging，与 JZL 自己的 print 日志无关，
# 只留 WARNING/ERROR 与 [JZL-*] 日志，避免运行期刷屏。
class _QuietModelLoad(logging.Filter):
    _NOISY = ("prepared for dynamic VRAM loading", "Requested to load", "0 models unloaded")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(n in msg for n in self._NOISY)


if not any(isinstance(f, _QuietModelLoad) for f in logging.getLogger().filters):
    logging.getLogger().addFilter(_QuietModelLoad())

# ── 全局资产池（无线传输核心）──────────────────────────────
# key = 资产名（如「图片1角色孙悟空」），value = {"kind": "image"|"audio"|"video", "data": tensor}
JZL_ASSET_POOL = {}

# ── 生成总线池（生成管理器 → 视频保存分配 无线传输）────────
# key = 组序号（0..11），value = {"image": tensor, "audio": dict|None}
JZL_BUS_POOL = {}

# ── 调度槽位映射（槽位名「角色A」→ 资产名「图片1 角色 孙悟空」）────────
JZL_SLOT_MAP = {}

# 类型下拉统一列表（图片/视频/音频共用）
ASSET_TYPES = ["角色", "场景", "道具", "分镜", "音效", "音乐", "其他"]

# 视频抽帧：24fps，最多抽 240 帧（超出均匀采样）
VIDEO_FPS = 24
MAX_VIDEO_FRAMES = 240

# 画幅比例选项（与「海螺H3视频参数」/ 官方 ResolutionSelector 一致）
ASPECT_RATIO_OPTIONS = [
    "1:1 (Square)", "2:3 (Portrait Photo)", "3:2 (Photo)", "3:4 (Portrait Standard)",
    "4:5 (Portrait Tall)", "4:3 (Standard)", "5:4 (Landscape Tall)",
    "9:16 (Portrait Widescreen)", "16:9 (Widescreen)", "21:9 (Ultrawide)",
]
ASPECT_RATIOS = {
    "1:1 (Square)": (1, 1), "2:3 (Portrait Photo)": (2, 3), "3:2 (Photo)": (3, 2),
    "3:4 (Portrait Standard)": (3, 4), "4:5 (Portrait Tall)": (4, 5), "4:3 (Standard)": (4, 3),
    "5:4 (Landscape Tall)": (5, 4), "9:16 (Portrait Widescreen)": (9, 16),
    "16:9 (Widescreen)": (16, 9), "21:9 (Ultrawide)": (21, 9),
}


# 每段视频时长选项（导演台节点共用：Pro / Max / Mini / 无限时长 / 短剧导演台）
# 固定 4~20 秒 + 常用区间；区间时由 LLM 按剧情弧线给每段实际秒数
# ⚠️ 保留历史值「4~15秒」：老工作流存的就是这个字符串，从选项列表删掉会让它们加载失败
SEGMENT_DURATION_CHOICES = (
    "固定4秒", "固定5秒", "固定6秒", "固定7秒", "固定8秒", "固定9秒", "固定10秒",
    "固定11秒", "固定12秒", "固定13秒", "固定14秒", "固定15秒",
    "固定16秒", "固定17秒", "固定18秒", "固定19秒", "固定20秒",
    "4~9秒", "7~12秒", "10~15秒", "15~20秒", "4~15秒", "4~20秒",
)
SEGMENT_DURATION_DEFAULT = "固定5秒"
SEGMENT_DURATION_MAX_SEC = 20   # 单段时长上限（秒）：选项已到「固定20秒 / 15~20秒 / 4~20秒」

# 生成视频数量选项（Pro / Max / Mini / 无限时长 用下拉）：默认「剧情决定」= LLM 按剧情时间线长度 ÷ 每段时长自动定段数
# 「短剧导演台」改为数值输入：0 = 剧情决定，1~60 = 固定段数
VIDEO_COUNT_CHOICES = (
    "剧情决定",
    "生成1段", "生成2段", "生成3段", "生成4段", "生成6段", "生成9段",
    "生成12段", "生成16段", "生成20段", "生成26段", "生成32段", "生成40段",
    "生成48段", "生成56段", "生成60段",
)
VIDEO_COUNT_DEFAULT = "剧情决定"
VIDEO_COUNT_FALLBACK = 6   # 「剧情决定」在拆解前的占位段数（拆解后按实际分段数覆盖）
SEGMENT_COUNT_MAX = 60     # 段数上限（「短剧导演台」数值输入 1~60）

# 采样分类（「🎭 采样解码设置 → 采样分类」）：常规采样（现有 K 采样器链路）/ SelfLift采样
SAMPLE_MODE_NORMAL = "常规采样"
SAMPLE_MODE_SELFLIFT = "SelfLift采样"
SELFLIFT_STEPS_DEFAULT = 8   # SelfLift 独立步数默认值（与常规采样的 steps 互不干扰）


def _parse_video_count(video_count):
    """video_count → ('auto', None)【剧情决定】或 ('fixed', N)。

    兼容三种写法：数值 0（短剧导演台的「剧情决定」）、字符串「剧情决定」、旧字符串「生成N段」。
    """
    s = str(video_count if video_count is not None else VIDEO_COUNT_DEFAULT).strip()
    if (not s) or s == "剧情决定":
        return ("auto", None)
    m = re.search(r"\d+", s)
    if m:
        n = int(m.group(0))
        if n <= 0:
            return ("auto", None)      # 0 = 剧情决定
        return ("fixed", max(1, min(SEGMENT_COUNT_MAX, n)))
    return ("auto", None)


def _vc_count(video_count, default=VIDEO_COUNT_FALLBACK):
    """拆解前可用的段数：'剧情决定' → 占位 default（拆解后会用实际分段数覆盖）。"""
    _mode, n = _parse_video_count(video_count)
    return n if n else default


def _story_style_options():
    """故事风格选项列表（供 schema combo 使用）。

    优先读 sheding.story_styles（20 个全量风格，含外部 md 长文，与官方 ScriptProcessor 完全一致）；
    缺失时回退 presets.script（8 个精简 fallback）。保证管理器风格列表与剧本处理器一致、不缺斤少两。
    """
    try:
        from .sheding.story_styles import STORY_STYLES
        keys = list(STORY_STYLES.keys())
        if keys:
            return keys
    except Exception:
        pass
    try:
        from .presets.script import STORY_STYLES
        keys = list(STORY_STYLES.keys())
        if keys:
            return keys
    except Exception:
        pass
    return ["热血战斗"]


# ── ⑤采样解码 / 偏好设置 原生 widget 选项 ────────────────
SAMPLER_OPTIONS = [
    "res_multistep", "res_multistep_cfg_pp", "res_multistep_ancestral", "res_multistep_ancestral_cfg_pp",
    "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp",
    "heun", "heunpp2", "exp_heun_2_x0", "exp_heun_2_x0_sde",
    "dpm_2", "dpm_2_ancestral", "lms", "dpm_fast", "dpm_adaptive",
    "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp", "dpmpp_sde", "dpmpp_sde_gpu",
    "dpmpp_2m", "dpmpp_2m_cfg_pp", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu",
    "dpmpp_2m_sde_heun", "dpmpp_2m_sde_heun_gpu", "dpmpp_3m_sde", "dpmpp_3m_sde_gpu",
    "ddpm", "lcm", "ipndm", "ipndm_v", "deis",
    "gradient_estimation", "gradient_estimation_cfg_pp", "er_sde", "seeds_2", "seeds_3",
    "sa_solver", "sa_solver_pece", "ddim", "uni_pc", "uni_pc_bh2",
]
SCHEDULER_OPTIONS = ["simple", "sgm_uniform", "karras", "exponential", "ddim_uniform", "beta", "normal", "linear_quadratic", "kl_optimal"]
SEED_MODE_OPTIONS = ["randomize", "fixed", "increment", "decrement"]
# 解码：固定 XB-BOX 优化版（=原版 + 显存清理）；cleanup 两级（对齐 1146 的「卸载显存模型」）
DECODE_VIDEO_OPTIONS = ["XB-BOX - VAE解码（原版优化）"]
DECODE_CLEANUP_OPTIONS = ["卸载显存模型", "不做清理"]
DECODE_AUDIO_OPTIONS = ["VAE解码（音频）"]


def _preference_options():
    """镜头语言偏好选项（与 JZL_MiniMaxH3Preference / JZL_MiniMaxPreset 保持一致）。"""
    try:
        from .nodes_llama import JZL_MiniMaxH3Preference as _P, JZL_MiniMaxPreset as _Pr
        return (list(_P._SHOT_SIZES), list(_P._CAMERA_MOVES), list(_P._CUT_RHYTHMS),
                list(_P._TRANSITIONS), list(_P._CREATIVE_REQS), list(_P._DETAIL_LENGTHS),
                list(_Pr._MUSIC))
    except Exception:
        return (["随机组合"], ["随机组合"], ["随机"], ["随机"], ["无特别要求"],
                ["标准 (350-500字)"], ["禁止音乐 / No Music"])


# ── 运行模式（四模式统一 + 表情前缀显示）──────────────────────────
# 下拉显示带表情（🛠️拆解 / 📚扩写 / 📝仅提示词 / 🪄穿透），保存值 = 显示文本（含表情）；
# execute 内用 _normalize_run_mode 剥离表情前缀与旧别名，统一回纯名比较，避免每处判断改。
RUN_MODE_PLAIN = ["故事拆解模式", "故事扩写模式", "穿透生成模式", "仅提示词输出"]
RUN_MODE_EMOJI = ["🛠️ 故事拆解模式", "📚 故事扩写模式", "🪄 穿透生成模式", "📝 仅提示词输出"]
_RM_EMOJI_TO_PLAIN = dict(zip(RUN_MODE_EMOJI, RUN_MODE_PLAIN))
_RM_ALIASES = {
    "拆解故事模式": "故事拆解模式",        # 旧 UI 名
    "直通模式": "穿透生成模式",            # 旧名
    "纯提示词生成": "仅提示词输出",        # 旧名
    "故事扩展模式": "故事扩写模式",        # 旧「扩写+拆解」→ 新「只扩写」语义
    "故事拆解模式🛠️": "故事拆解模式",      # 防御：部分旧工作流表情在后
}


def _normalize_run_mode(run_mode):
    """run_mode 归一化：剥离表情/变体选择符 + 旧别名 → 纯模式名。

    注意：不同输入法/工作流里同一表情可能带 U+FE0F（VS16）或零宽连接符，
    直接 startswith("🪄") 会残留 U+FE0F 导致识别失败并回退成「故事拆解模式」。
    这里先做字符清洗，再按关键字归一，最后才走精确映射兜底。
    """
    raw = "" if run_mode is None else str(run_mode).strip()
    rm = raw
    # 去掉变体选择符（VS15/VS16）与零宽连接符，避免表情比较不相等
    for ch in ("\ufe0e", "\ufe0f", "\u200d"):
        rm = rm.replace(ch, "")
    rm = rm.strip()

    result = None
    # 关键字归一（最稳：兼容表情前后缀、旧名、本地化差异）
    if "穿透" in rm or "直通" in rm:
        result = "穿透生成模式"
    elif "仅提示词" in rm or "纯提示词" in rm:
        result = "仅提示词输出"
    elif "扩写" in rm or "扩展" in rm:
        result = "故事扩写模式"
    elif "拆解" in rm:
        result = "故事拆解模式"

    if result is None:
        # 表情前缀剥离（🛠️ 📚 📝 🪄 及其后的空格）
        for emoji in ("🛠️", "📚", "📝", "🪄"):
            if rm.startswith(emoji):
                rm = rm[len(emoji):].lstrip()
                break
        rm = _RM_ALIASES.get(rm, rm)              # 旧别名
        rm = _RM_EMOJI_TO_PLAIN.get(rm, rm)       # 后置表情
        if rm not in RUN_MODE_PLAIN:
            rm = _RM_EMOJI_TO_PLAIN.get(str(rm).replace(" ", ""), rm)
        result = rm if rm in RUN_MODE_PLAIN else "故事拆解模式"

    # 非标准输入留一条诊断（正常含表情的值不会刷屏）
    _known = set(RUN_MODE_EMOJI) | set(RUN_MODE_PLAIN) | set(_RM_ALIASES)
    if not raw:
        print("[JZL] ⚠️ 运行模式为空 → 已按「故事拆解模式」处理")
    elif raw not in _known:
        print(f"[JZL] ℹ️ 运行模式原始值={run_mode!r} → 归一化为「{result}」")
    return result


def _asset_settings_file():
    """资产配置持久化文件路径（存 ComfyUI user 目录）。"""
    try:
        base = folder_paths.get_user_directory()
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "jzl_assets.json")


def _read_asset_settings():
    """读取资产配置，无配置返回默认结构。"""
    try:
        with open(_asset_settings_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"images": [], "videos": [], "audios": []}


def _write_asset_settings(data):
    """保存资产配置到磁盘。"""
    try:
        with open(_asset_settings_file(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ── 短剧管理器统一配置（模型/资产/文本增强/生成参数/采样解码） ──────
MANAGER_DEFAULTS = {
    "auto_save": True,  # 弹窗「自动保存」开关（面板改动自动保存行为，非分段视频自动保存）
    "save": {  # 视频保存设置（存 manager_settings，不占节点 schema widget → 节点表面无隐藏接口）
        "mode": "分段保存",
        "auto_save": False,   # 分段视频自动保存（ffmpeg 逐段落盘）
        "auto_merge": False,  # 分段视频自动合并
        "auto_merge_delete": False,  # 合并后删除分段视频
    },
    "models": {
        "fl2va": {
            "model": "",
            "loras": [],  # [{"name": "", "strength": 1.0}, ...]
        },
        "ref2va": {
            "model": "",
            "loras": [],
        },
        "common": {
            "clip": "",
            "video_vae": "",
            "audio_vae": "",
        },
    },
    "assets": {"images": [], "videos": [], "audios": []},
    # 「生成设定 → 语义修正」：语义桥（内部调用内置副本 vendor_semantic_bridge）
    "semantic_bridge": {
        "enabled": False,
        "adapter": "",
        "alpha": 0.10,
        "magnitude_match": "per_token",
    },
    "enhance": {
        "story_decompose": True,
        "enabled": False,
        "llm_backend": "在线API [api]",
        "force_offload": False,
        "seed": 0,
        "seed_control": "randomize",
        "llm": {
            "model": "",
            "mmproj": "None",
            "chat_handler": "None",
            "backend": "llama-cpp-python",
            "n_ctx": 32768,
            "vram_limit": -1,
            "image_min_tokens": 0,
            "image_max_tokens": 0,
            "max_tokens": 8192,
            "top_k": 40,
            "top_p": 0.9,
            "min_p": 0.05,
            "typical_p": 1.0,
            "temperature": 0.6,
            "repeat_penalty": 1.05,
            "frequency_penalty": 0.0,
            "present_penalty": 0.0,
            "mirostat_mode": 0,
            "mirostat_eta": 0.1,
            "mirostat_tau": 5.0,
            "gpu_device": "auto",
        },
        "api": {
            "provider": "OpenAI 兼容 (OpenAI/DeepSeek/Qwen/GLM/Kimi/Ollama/vLLM/LM Studio)",
            "model": "deepseek-flash",
            "api_key": "",
            "base_url": "https://api.deepseek.com",
            "temperature": 0.6,
            "max_tokens": 81920,
            "thinking": "disabled",
        },
        "preference": {
            "shot_size": "随机组合",
            "camera_move": "随机组合",
            "cut_rhythm": "随机",
            "transition": "随机",
            "music_style": "禁止音乐 / No Music",
            "creative_req": "无特别要求",
            "detail_length": "标准 (350-500字)",
            "custom": "",
        },
        "custom_prompt": "",
        "system_prompt": "",
        "expand_words": 500,  # 故事扩写模式的目标字数（「剧本拆解配置」可改）
        "expand_source": "素材",  # 故事扩写模式素材范围：素材=按参考素材扩写(禁杜撰)/自由=按风格自由发挥
        "inference_mode": "one by one",
        "max_frames": 24,
        "max_size": 256,
    },
    "gen_params": {
        "aspect_ratio": "16:9 (Widescreen)",
        "megapixels": 1.0,
        "multiple": 32,
        "duration": 8,
        "width": 0,
        "height": 0,
        "scale_factor": 1.0,
        "upscale_scale": 1.5,
    },
    "sample_decode": {
        "sampler": "res_multistep",
        "scheduler": "simple",
        "steps": 4,
        "cfg": 1.0,
        "seed": 0,
        "seed_mode": "randomize",
        "decode_video": "XB-BOX - VAE解码（原版优化）",
        "decode_cleanup": "卸载显存模型",
        # 采样分类（常规采样 / SelfLift采样）；SelfLift 用内置副本 vendor_selflift
        "sample_mode": SAMPLE_MODE_NORMAL,
        "selflift": {
            # SelfLift 有独立的步数（默认 8 = 低分辨率前缀 6 + 高分辨率收尾 2），与常规采样的 4 步互不干扰
            "steps": 8,
            "transition_step": 6,
            "lowres_scale": 0.5,
            "rho": 0.6,
            "w_min": 0.5,
            "w_max": 1.0,
            "upscaler_model": "none",
            "upscaler_unload": True,
            "highres_tiling": False,
        },
        "second": {
            "enabled": False,
            "upscaler_model": "minimax_h3_latent_upscaler_3d_fp32.pth",
            "device": "cuda",
            "precision": "fp32",
            "sampler": "euler",
            "scheduler": "simple",
            "steps": 3,
            "denoise": 0.3,
            "sigmas_mode": "scheduler",
            "custom_sigmas": "0.8500, 0.6316, 0.3158, 0.0000",
            # 二采空间分块（省显存）：关闭 / 自动（按显存预算选块数）/ 手动（指定块数）
            "tiling": "关闭",
            "tiling_tiles": 2,
            "tiling_overlap": 128,
        },
    },
}


def _list_models(category):
    """列出 models/<category>/MiniMax-H3 下的模型文件（相对路径，如 MiniMax-H3/xxx.safetensors）。"""
    try:
        files = folder_paths.get_filename_list(category)
        return [f for f in files if "minimax-h3" in f.lower() or "minimax_h3" in f.lower()]
    except Exception:
        return []


def _manager_settings_file():
    """短剧管理器配置持久化文件（存 ComfyUI user 目录）。"""
    try:
        base = folder_paths.get_user_directory()
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "jzl_manager.json")


def _merge_manager_settings(data):
    """节点配置与默认值合并（递归补齐缺失键），并做旧配置兼容迁移。

    必须是**递归**补齐：只按顶层键 `update` 的话，用户旧配置里的 `sample_decode.selflift`
    会整块覆盖默认值，导致后加的键（如 `selflift.steps`）永远缺失 → 面板显示 8 而后端只能回退。
    """
    def _fill(target, defaults):
        for k, dv in defaults.items():
            if k not in target or target[k] is None:
                target[k] = json.loads(json.dumps(dv, ensure_ascii=False))
            elif isinstance(dv, dict) and isinstance(target[k], dict):
                _fill(target[k], dv)
        return target

    merged = _fill(json.loads(json.dumps(data, ensure_ascii=False)) if isinstance(data, dict) else {},
                   MANAGER_DEFAULTS)
    # 旧配置兼容：enhance.llm.model 为空时，从旧 models.llm_* 字段补齐（①生成模型管理时代的结构）
    llm = merged.get("enhance", {}).get("llm") or {}
    if not (llm.get("model") or "").strip():
        legacy = merged.get("models") or {}
        if legacy.get("llm_model"):
            llm["model"] = legacy.get("llm_model")
        if legacy.get("mmproj"):
            llm["mmproj"] = legacy.get("mmproj")
        if legacy.get("chat_handler"):
            llm["chat_handler"] = legacy.get("chat_handler")
        if legacy.get("n_ctx") is not None:
            llm["n_ctx"] = int(legacy.get("n_ctx"))
        if legacy.get("vram_limit") is not None:
            llm["vram_limit"] = int(legacy.get("vram_limit"))
    # 旧配置兼容：sample_decode.decode_video → decode_cleanup（原版 VAEDecode = 不做清理）
    raw_sd = (data or {}).get("sample_decode") or {}
    if "decode_video" in raw_sd and not raw_sd.get("decode_cleanup"):
        merged["sample_decode"]["decode_cleanup"] = (
            "不做清理" if (raw_sd.get("decode_video") or "") == "VAE解码" else "卸载显存模型")
    return merged


def _read_manager_settings():
    """读取全局短剧管理器配置（旧工作流/无节点配置时回退用）。"""
    try:
        with open(_manager_settings_file(), "r", encoding="utf-8") as f:
            return _merge_manager_settings(json.load(f))
    except Exception:
        return _merge_manager_settings({})


def _parse_node_manager_settings(raw):
    """节点独立配置：解析工作流内保存的 manager_settings JSON；空/非法回退全局配置。"""
    if raw and isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and any(k in data for k in ("assets", "enhance", "sample_decode")):
                return _merge_manager_settings(data)
        except Exception:
            pass
    return _read_manager_settings()


def _write_manager_settings(data):
    """保存短剧管理器配置到磁盘。"""
    try:
        with open(_manager_settings_file(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ── 重拍模式：最后一次 LLM 拆解/增强提示词 保存 / 读取 / 校验 ──────
# 拆解/增强后的完整提示词（含 [SHOT_START]~[SHOT_END] 分段）保存到 output/jzl/最近提示词.json，
# 供重拍模式「提示词选择」加载（跨刷新持久，独立于工作流内的 internal_prompt）。

def _last_script_file():
    try:
        base = os.path.abspath(folder_paths.get_output_directory())
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "output")
    d = os.path.join(base, "jzl")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "最近提示词.json")


def _save_last_script(story_name, script):
    """保存最后一次 LLM 拆解/增强后的完整提示词（供重拍模式「提示词选择」加载）。"""
    try:
        data = {
            "story_name": (story_name or "").strip(),
            "script": script or "",
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(_last_script_file(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[JZL-管理器] 保存最近提示词失败：{e}")


def _load_last_script():
    """读取最后一次拆解/增强提示词，返回 dict（无则 None）。含 shots 段落数组。"""
    try:
        # utf-8-sig 容错：素材库/外部工具可能以带 BOM 的 UTF-8 写入（如 PowerShell 默认），
        # 与 /jzl/import_assets 的素材库解析保持一致的容错策略
        with open(_last_script_file(), "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        if not d or not isinstance(d.get("script"), str) or not d["script"].strip():
            return None
        shots = [s.strip() for s in re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', d["script"], re.DOTALL)]
        d["shots"] = shots
        d["shot_count"] = len(shots)
        return d
    except Exception:
        return None


def _shot_blocks(script):
    """提示词里的 [SHOT_START]…[SHOT_END] 分段块列表。"""
    return [s.strip() for s in re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', script or "", re.DOTALL)]


def _pt_seg_count(script):
    """穿透生成模式的生成段数：提示词里有几段就生成几段（0 = 未识别到分段块）。

    不使用「生成视频数量」设定值：设定值与剧本实际段数不一致时，旧逻辑会直接报错终止，
    或按设定值多生成空占位段。现在一律以提示词实际分段数为准（上限 SEGMENT_COUNT_MAX）。
    """
    return min(SEGMENT_COUNT_MAX, len(_shot_blocks(script)))


def _validate_script(script, video_count=6):
    """生产规范校验：重拍模式加载提示词时检查，返回错误/警告描述列表（空 = 通过）。"""
    errs = []
    if not script or not script.strip():
        return ["提示词为空"]
    shots = [s.strip() for s in re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', script or "", re.DOTALL)]
    if not shots:
        return ["未识别到 [SHOT_START]~[SHOT_END] 分段（拆解结果可能损坏）"]
    n = _vc_count(video_count)
    if len(shots) != n:
        errs.append(f"识别到 {len(shots)} 段，与当前「生成视频数量」{n} 不一致")
    noh3 = [i + 1 for i, s in enumerate(shots) if "===H3_PROMPT===" not in s]
    if noh3:
        errs.append(f"第 {', '.join(str(x) for x in noh3[:6])}{'…' if len(noh3) > 6 else ''} 段缺少 ===H3_PROMPT=== 标记")
    return errs


def _check_decomposed_script(script, video_count, auto_count=False, tag="管理器"):
    """穿透生成模式前置校验：必须使用「拆解模式」拆解好的提示词。

    返回 (err_msg | None, 实际分段数)。err_msg 非空 → 调用方报错终止、不生成。
    ① 必须有 [SHOT_START]…[SHOT_END] 分段块；② 每段必须含 ===H3_PROMPT=== 六段正文。
    ③ 段数一律自动对齐提示词实际分段数（不再受「生成视频数量」约束）：设定值与剧本不一致时
       既不报错终止，也不会按设定值多生成空占位段，只打印一行提示。
    """
    shots = _shot_blocks(script)
    if not shots:
        return ("提示词格式不正确：未识别到 [SHOT_START]…[SHOT_END] 分段块。"
                "穿透生成模式必须使用「拆解模式」拆解好的提示词（纯文本故事 / 手写草稿不能直接生成）。", 0)
    noh3 = [i + 1 for i, s in enumerate(shots) if "===H3_PROMPT===" not in s]
    if noh3:
        return (f"提示词格式不正确：第 {', '.join(str(x) for x in noh3[:6])}{'…' if len(noh3) > 6 else ''} 段"
                f"缺少 ===H3_PROMPT=== 六段提示词正文（不是「拆解模式」的输出）。", len(shots))
    pt_n = min(SEGMENT_COUNT_MAX, len(shots))
    if len(shots) > SEGMENT_COUNT_MAX:
        print(f"[JZL-{tag}] ⚠️ 提示词有 {len(shots)} 段，超过段数上限 {SEGMENT_COUNT_MAX} 段 → "
              f"只生成前 {SEGMENT_COUNT_MAX} 段")
    if not auto_count:
        try:
            want = int(video_count)
        except Exception:
            want = 0
        if want and want != pt_n:
            print(f"[JZL-{tag}] 🔒 穿透生成：提示词实际 {pt_n} 段，已忽略「生成{want}段」设定 → "
                  f"按提示词 {pt_n} 段自动对齐（不报错、也不会多生成空占位段）")
    miss = [i + 1 for i, s in enumerate(shots) if not re.search(r"\*\*(?:时长|Duration)\*\*\s*[:：]", s)]
    if miss:
        print(f"[JZL-{tag}] 提示：第 {', '.join(str(x) for x in miss[:6])}{'…' if len(miss) > 6 else ''} 段未写 "
              f"**时长**/**Duration** 字段 → 该段按节点设定时长生成（用「拆解模式」产物才能逐段动态时长）")
    return (None, pt_n)


_SCRIPT_TAG_HINTS = (
    (r'===H3_PROMPT===', "H3 六段提示词标记（===H3_PROMPT===）"),
    (r'(?m)^\s*(?:subject_definitions|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music)\s*[:：]',
     "六段字段（subject_definitions / detailed_description …）"),
    (r'\[SHOT_START\]|\[SHOT_END\]', "分段块标记（[SHOT_START]…[SHOT_END]）"),
    (r'===\s*(?:SCENE|VIDEO|AUDIO)_INSTRUCTION\s*===', "调度指令（===SCENE_INSTRUCTION=== …）"),
    (r'(?m)^\s*###\s*Video_\d+', "分段标题（### Video_001）"),
    (r'\*\*(?:时长|Duration)\*\*\s*[:：]', "分段信息字段（**时长** / **Duration**）"),
)


def _check_natural_language_input(text):
    """故事拆解 / 故事扩写模式输入校验：必须是自然语言故事，不能是已拆解的提示词。

    命中任一六段式/分段标签 → 返回错误描述；自然语言（无标签）→ None。
    """
    t = str(text or "")
    if not t.strip():
        return None
    hits = [name for pat, name in _SCRIPT_TAG_HINTS if re.search(pat, t)]
    if not hits:
        return None
    return ("提示词格式不正确：故事拆解模式与故事扩写必须使用自然语言提示词，不得包含 "
            + "、".join(hits) + "。若要直接用已拆解的提示词生成视频，请改用「🪄 穿透生成模式」")


# ── 生成核心：分段 / 编码 / 采样 / 解码 ──────────────────────

def _slot_name(slot):
    """调度槽位（如「角色A」/「角色:角色A」）→ 纯名字。"""
    if isinstance(slot, str) and ":" in slot:
        return slot.split(":", 1)[-1].strip()
    return str(slot).strip()


def _parse_four_in_one(content):
    """解析分段四段格式，返回 (h3_prompt, scene, video, audio)。复刻分段处理中心。"""
    h3, scene, video, audio = "", "{}", "{}", "{}"
    for section in re.split(r'\n(?====)', content or ""):
        section = section.strip()
        if section.startswith("===H3_PROMPT==="):
            h3 = section[len("===H3_PROMPT==="):].strip()
        elif section.startswith("===SCENE_INSTRUCTION==="):
            scene = section[len("===SCENE_INSTRUCTION==="):].strip()
        elif section.startswith("===VIDEO_INSTRUCTION==="):
            video = section[len("===VIDEO_INSTRUCTION==="):].strip()
        elif section.startswith("===AUDIO_INSTRUCTION==="):
            audio = section[len("===AUDIO_INSTRUCTION==="):].strip()
    return h3, scene, video, audio


def _collect_slots_ex(raw, kind, limit):
    """按调度指令从全局资产池取 kind 类资产，返回 [(槽位名, 数据)]（≤limit）。

    与「场景/视频/音频调度」节点共用同一套规则（story_nodes 的 normalize_slots /
    _parse_slots / _get_from_pool / _match_name）：槽位名先经 JZL_SLOT_MAP 精确映射为
    资产名，再走共享池匹配（精确→模糊），保证管理器与真实调度节点行为一致。
    保留槽位名是为了打印「真实调用信息」（槽位名=image N / Audio N）。
    """
    from .story_nodes import _parse_slots, _get_from_pool, normalize_slots
    try:
        normalized = normalize_slots(raw) if isinstance(raw, str) else raw
    except Exception:
        normalized = raw
    out = []
    for slot in _parse_slots(normalized):
        if len(out) >= limit:
            break
        name = _slot_name(slot)
        asset_name = JZL_SLOT_MAP.get(name, name)
        data = _get_from_pool(asset_name, kind=kind)
        if data is not None:
            out.append((name, data))
        else:
            # LLM 引用了槽位但池里没有（素材未上传文件/文件缺失）→ 本段退化成无该参考。
            # 不提示的话用户只会看到「参考 0 图」，无从判断原因。
            _log_once(f"[JZL-素材] ⚠️ 参考槽位「{name}」未载入素材池（{asset_name}）"
                      f"→ 该参考已忽略；请检查对应素材是否已上传文件")
    return out


def _collect_slots(raw, kind, limit):
    """同 _collect_slots_ex，只取数据（供不需要打印调用信息的调用点使用）。"""
    return [d for _, d in _collect_slots_ex(raw, kind, limit)]


def _collect_refs_with_names(scene, vid, aud, mention_names):
    """按调度指令 + @引用收集参考素材，返回 (ref_images, ref_videos, ref_audios, names)。

    names = {"image"/"video"/"audio": [槽位名…]}，与三个列表同序 —— 供编码阶段回填
    「真实调用信息」。总数上限 12：超出时先丢视频、再丢图片，**音频最后才丢**
    （音色/配乐/念白是用户明确引用的参考，不能被静默丢掉；9图+3音频=12 正常不会触发）。
    """
    img_pairs = _collect_slots_ex(scene, "image", 9)
    vid_pairs = _collect_slots_ex(vid, "video", 3)
    aud_pairs = _collect_slots_ex(aud, "audio", 3)
    ref_images = [d for _, d in img_pairs]
    ref_videos = [d for _, d in vid_pairs]
    ref_audios = [d for _, d in aud_pairs]
    names = {"image": [n for n, _ in img_pairs],
             "video": [n for n, _ in vid_pairs],
             "audio": [n for n, _ in aud_pairs]}
    for mname in (mention_names or []):
        kind, data = _get_asset_by_name(mname)
        if data is None:
            continue
        if kind == "image" and len(ref_images) < 9:
            ref_images.append(data)
            names["image"].append(mname)
        elif kind == "video" and len(ref_videos) < 3:
            ref_videos.append(data)
            names["video"].append(mname)
        elif kind == "audio" and len(ref_audios) < 3:
            ref_audios.append(data)
            names["audio"].append(mname)
    dropped = []
    while len(ref_images) + len(ref_videos) + len(ref_audios) > 12:
        if ref_videos:
            ref_videos.pop()
            dropped.append(names["video"].pop())
        elif ref_images:
            ref_images.pop()
            dropped.append(names["image"].pop())
        else:
            ref_audios.pop()
            dropped.append(names["audio"].pop())
    if dropped:
        _log_once(f"[JZL-素材] ⚠️ 参考总数超过 12 上限 → 已丢弃：{'、'.join(dropped)}"
                  f"（丢弃优先级：视频 → 图片 → 音频）")
    return ref_images, ref_videos, ref_audios, names


def _match_asset(a, b):
    """资产名模糊匹配（去空格互相包含，或分词交集）。"""
    sa, sb = (a or "").strip().lower(), (b or "").strip().lower()
    if not sa or not sb:
        return False
    if sa in sb or sb in sa:
        return True
    # 去空格后互相包含（@引用无空格 vs 资产名带空格）
    na = re.sub(r'\s+', '', sa)
    nb = re.sub(r'\s+', '', sb)
    if na and nb and (na in nb or nb in na):
        return True
    ta = {t for t in re.split(r'[-\s_（(）):：,，、/]+', sa) if t}
    tb = {t for t in re.split(r'[-\s_（(）):：,，、/]+', sb) if t}
    return bool(ta & tb)


def _extract_mentions(text):
    """从提示词里提取资产引用（「图片N…」「视频N…」「音频N…」格式），
    返回 list[str]，并把引用从文本移除（引用仅用于匹配参考素材，不进入模型文本）。

    前端富文本插入的是去空格资产名（如「图片1角色碗碗」），本函数按资产名前缀识别。
    """
    names = []
    cleaned = text or ""

    def _repl(m):
        names.append(m.group(0))
        return ""

    cleaned = re.sub(r'(?:图片|视频|音频)\d+[^\s@，。；,.、]*', _repl, cleaned)
    return names, cleaned


def _get_asset_by_name(name):
    """按名字从资产池精确/模糊取资产，返回 (kind, tensor)。"""
    if not JZL_ASSET_POOL or not name:
        return None, None
    if name in JZL_ASSET_POOL:
        item = JZL_ASSET_POOL[name]
        return item.get("kind"), item.get("data")
    for key, item in JZL_ASSET_POOL.items():
        if _match_asset(name, key):
            return item.get("kind"), item.get("data")
    return None, None


def _encode_ref_audio(audio_vae, audio):
    """复刻官方 ref 音频编码：waveform → 归一化 latent。

    重采样走 `comfy.audio.resample`（与官方节点一致）：语义同 torchaudio 的带限 sinc，
    但不依赖 torchaudio —— 上游已把 torchaudio.functional.resample 换掉，本插件跟随。
    """
    waveform = audio["waveform"]  # [B, C, L]
    sr = audio["sample_rate"]
    vae_sr = getattr(audio_vae, "audio_sample_rate", 32000)
    if sr != vae_sr:
        _rs = getattr(_comfy_audio, "resample", None)
        if _rs is not None:
            waveform = _rs(waveform, sr, vae_sr)
        else:                                       # ComfyUI < 0.37.0 没有 comfy.audio → 回退 torchaudio
            waveform = torchaudio.functional.resample(waveform, sr, vae_sr)
    z = audio_vae.encode(waveform[:1].movedim(1, -1))  # [1, 32, 2, T]
    return z, z.shape[-1]


def _vae_kind(vae):
    """MiniMax H3 有两种 VAE：视觉(MiniMaxH3VideoVAE) / 音频(MiniMaxH3AudioVAE)。

    返回 'video' / 'audio' / 'unknown'（按 first_stage_model 的类名与模块判定）。
    """
    try:
        fsm = getattr(vae, "first_stage_model", None)
        if fsm is None:
            return "unknown"
        mod = f"{type(fsm).__module__ or ''}".lower()
        name = f"{type(fsm).__name__ or ''}".lower()
        if "audio" in mod or "audio" in name:
            return "audio"
        if "minimax" in mod or "minimax" in name or name.endswith("vae"):
            return "video"
        return "unknown"
    except Exception:
        return "unknown"


def _vae_type_name(vae):
    try:
        return type(getattr(vae, "first_stage_model", vae)).__name__
    except Exception:
        return "?"


def _assert_vae_roles(vae, audio_vae):
    """接线自检：视觉/音频 VAE 接错端口时给出可操作报错，而不是晦涩的 unpack 错误。

    真实案例（N 卡用户现场日志确认）：把 checkpoint 加载器的「音频VAE」接到本节点「视觉VAE」口，
    图像编码于是走到 comfy/ldm/minimax/audio_vae.py 的 encode()（它期望 [B,2,L] 波形张量），
    抛 `too many values to unpack (expected 3)`，最终表现为「本段未落盘任何视频」。
    """
    if vae is not None and _vae_kind(vae) == "audio":
        raise RuntimeError(
            f"[接线错误] 「视觉VAE」口接的是**音频 VAE**（{_vae_type_name(vae)}）：\n"
            f"  图像编码会走音频 VAE，它期望 [B,2,L] 波形张量，所以报 too many values to unpack (expected 3)。\n"
            f"  修法：把 checkpoint 加载器的「视觉 / 视频 VAE」（MiniMaxH3VideoVAE）接到本节点「视觉VAE」口，\n"
            f"        把「音频 VAE」（MiniMaxH3AudioVAE）接到「音频VAE」口 —— 两个口不能接反。\n"
            f"  当前识别：视觉VAE口={_vae_type_name(vae)}，音频VAE口={_vae_type_name(audio_vae) if audio_vae is not None else '未接'}")
    if audio_vae is not None and _vae_kind(audio_vae) == "video":
        raise RuntimeError(
            f"[接线错误] 「音频VAE」口接的是**视觉 VAE**（{_vae_type_name(audio_vae)}）：\n"
            f"  音频编解码需要音频 VAE（MiniMaxH3AudioVAE）。\n"
            f"  修法：把「音频 VAE」接到「音频VAE」口，「视觉 VAE」接到「视觉VAE」口 —— 两个口不能接反。\n"
            f"  当前识别：视觉VAE口={_vae_type_name(vae) if vae is not None else '未接'}，音频VAE口={_vae_type_name(audio_vae)}")


def _check_official_drift():
    """一次性对拍：本包内置的官方 MiniMax H3 辅助函数 vs 用户机器上官方模块的输出。

    本包（nodes.py）为兼容内置了一份官方 helpers。若用户的 ComfyUI 版本改动了这些函数
    （返回元数 / 画布规则 / 帧数对齐变了），就会出现「只有别人的电脑报错、开发者本机永远复现不了」
    的怪问题（典型症状就是 `too many values to unpack (expected N)`）。这里直接对拍并明确点名。
    """
    if getattr(_check_official_drift, "_done", False):
        return
    _check_official_drift._done = True
    try:
        from . import nodes as _ours
        import comfy_extras.nodes_minimax_h3 as _off
    except Exception:
        return
    diffs = []
    try:
        for _len in (5, 22, 39, 120, 192):
            a, b = _ours.temporal_shape(_len), _off.temporal_shape(_len)
            if tuple(a) != tuple(b):
                diffs.append(f"temporal_shape({_len}): 本包={tuple(a)} 官方={tuple(b)}")
    except Exception as e:
        diffs.append(f"temporal_shape 对拍异常：{type(e).__name__}: {e}")
    try:
        for _wh in ((832, 480), (480, 832), (1280, 720)):
            a, b = _ours.adapt_canvas(*_wh), _off.adapt_canvas(*_wh)
            if tuple(a) != tuple(b):
                diffs.append(f"adapt_canvas{_wh}: 本包={tuple(a)} 官方={tuple(b)}")
    except Exception as e:
        diffs.append(f"adapt_canvas 对拍异常：{type(e).__name__}: {e}")
    if diffs:
        print("[JZL-管理器] ⚠️ 官方 MiniMax H3 代码与「本包内置版本」不一致（ComfyUI 版本漂移）：")
        for d in diffs[:6]:
            print(f"[JZL-管理器]    - {d}")
        print("[JZL-管理器]    这很可能就是「只有别人的电脑报错」的原因，请把这几行连同完整日志回传。")


def _encode_ref_to_video(clip, vae, audio_vae, prompt, width, height, length,
                         ref_images=None, ref_videos=None, ref_video_audios=None,
                         ref_audios=None, ref_image_size="match", ref_scale=1.0,
                         slot_names=None, probe=None):
    """复刻官方 MiniMaxH3ReferenceToVideo 编码（va2va / ref2va）。

    slot_names/probe：可选，用于回填「真实调用信息」——probe 会被写入
    {"images": [(槽位名, 0基图序号)], "videos": [(槽位名, <Video N> 的 N)], "audios": [(槽位名, <Audio N> 的 N)]}，
    记录的是**实际进入编码的项**（被跳过的项不会出现），供调用方打印真实调用结果。
    """
    _check_official_drift()
    _assert_vae_roles(vae, audio_vae)
    latent, frame_count = _empty_av_latent(width, height, length)

    names = slot_names or {}
    _in_names = list(names.get("image") or [])
    _vd_names = list(names.get("video") or [])
    _au_names = list(names.get("audio") or [])
    if probe is not None:
        probe.setdefault("images", [])
        probe.setdefault("videos", [])
        probe.setdefault("audios", [])

    def _nm(lst, pos):
        return lst[pos] if 0 <= pos < len(lst) else f"#{pos}"

    ref_items = []   # tokenizer 呈现顺序
    ref_blocks = []  # DiT payload 顺序
    # <Audio N> 编号必须与 tokenizer 的 counters["audio"] 完全一致：视频自带音轨也占一个编号，
    # 且排在独立音频之前（否则「有视频音轨」分段里独立音频的 N 会偏小，日志与提示词对不上）。
    _audio_n = 0

    for pos, img in enumerate(ref_images or []):
        if img is None:
            continue
        h, w = img.shape[1], img.shape[2]
        if ref_image_size == "match":
            scale = min(1.0, math.sqrt(ref_scale * (width * height) / (w * h)))
        else:
            scale = min(1.0, REF_IMAGE_SHORT_EDGE / min(w, h))
        tw = max(CANVAS_MULTIPLE, round(w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        th = max(CANVAS_MULTIPLE, round(h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        resized = _resize(img[:1], tw, th, "disabled")
        z = vae.encode(resized)
        ref_items.append({"type": "image", "data": resized})
        ref_blocks.append({"kind": "image", "latent_h": th // 16, "latent_w": tw // 16, "latent": z})
        if probe is not None:
            probe["images"].append((_nm(_in_names, pos), len(probe["images"])))

    # 视频未被引用时，视频音轨端口必须彻底关闭：否则音频会被当成「视频音轨」送进音轨通道，
    # 结果既丢了独立音频参考（ref_audios），又让音轨与并不存在的视频配对。
    ref_video_audios = (ref_video_audios or []) if (ref_videos and len(ref_videos) > 0) else []
    for pos, video_frames in enumerate(ref_videos or []):
        if video_frames is None:
            continue
        soundtrack = ref_video_audios[pos] if pos < len(ref_video_audios) else None
        vh, vw = video_frames.shape[1], video_frames.shape[2]
        cw, ch = adapt_canvas(vw, vh)
        if vw * vh < cw * ch:
            cw = max(CANVAS_MULTIPLE, round(vw / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            ch = max(CANVAS_MULTIPLE, round(vh / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        frames = _resize(video_frames, cw, ch, "disabled")
        if frames.shape[0] > frame_count:
            frames = frames[:frame_count]
        n = frames.shape[0]
        if n < 5:
            continue  # 官方要求 ≥5 帧，不足则跳过该参考
        while n % 17 != 5:
            n -= 1
        frames = frames[:n]
        z = vae.encode(frames)
        audio_latent, ref_audio_t = (None, 0)
        if soundtrack is not None:
            audio_latent, ref_audio_t = _encode_ref_audio(audio_vae, soundtrack)
            ref_items.append({"type": "audio"})
            _audio_n += 1          # 音轨占用一个 <Audio N> 编号
        sample_idx = list(range(0, frames.shape[0], 12))  # FPS//2 = 12 (2fps)
        qwen_frames = frames[sample_idx]
        ref_items.append({"type": "video", "data": qwen_frames,
                          "timestamps": [i / 2.0 for i in range(len(sample_idx))]})
        ref_blocks.append({"kind": "video_audio" if ref_audio_t else "video",
                           "latent_t": z.shape[2], "latent_h": ch // 16, "latent_w": cw // 16,
                           "ref_audio_t": ref_audio_t, "latent": z, "audio_latent": audio_latent})
        if probe is not None:
            probe["videos"].append((_nm(_vd_names, pos), len(probe["videos"]) + 1))

    for pos, audio in enumerate(ref_audios or []):
        if audio is None:
            continue
        audio_latent, ref_audio_t = _encode_ref_audio(audio_vae, audio)
        ref_items.append({"type": "audio"})
        ref_blocks.append({"kind": "audio", "ref_audio_t": ref_audio_t, "audio_latent": audio_latent})
        _audio_n += 1
        if probe is not None:
            probe["audios"].append((_nm(_au_names, pos), _audio_n, _audio_src_desc(audio, ref_audio_t)))

    tokens = clip.tokenize(prompt, minimax_ref_items=ref_items)
    cond = clip.encode_from_tokens_scheduled(tokens)
    if ref_blocks:
        cond = node_helpers.conditioning_set_values(cond, {"minimax_refs": ref_blocks})
    return cond, latent


def _audio_src_desc(audio, ref_audio_t):
    """音频来源描述：源时长 → 编码出的 latent token 数（证明「确实用了这个文件」）。"""
    try:
        sr = int(audio.get("sample_rate") or 0)
        wf = audio.get("waveform")
        n = int(wf.shape[-1]) if wf is not None else 0
        if sr > 0 and n > 0:
            return f"源 {n / float(sr):.2f}s → {int(ref_audio_t)} token"
    except Exception:
        pass
    return f"{int(ref_audio_t or 0)} token"


def _fmt_call_info(probe):
    """把编码阶段回填的**真实**调用记录格式化为一行：槽位名=image N / Video N / Audio N。

    image N 是 0 基图序号（对应提示词里 <Picture N+1>）；Video/Audio N 即提示词里的
    <Video N>/<Audio N> 编号。记录来自实际进入编码的项，被跳过/未载入的参考不会出现。
    """
    if not probe:
        return ""
    parts = []

    def _emit(items, kind):
        for it in (items or []):
            it = tuple(it)
            nm, ix = it[0], it[1]
            extra = it[2] if len(it) > 2 else None
            parts.append(f"{nm}={kind} {ix}" + (f"（{extra}）" if extra else ""))

    _emit(probe.get("images"), "image")
    _emit(probe.get("videos"), "Video")
    _emit(probe.get("audios"), "Audio")
    return " 丨 ".join(parts)


def _encode_refs(tag, seg_label, clip, vae, audio_vae, prompt, width, height, length,
                 ref_images, ref_videos, ref_audios, ref_names, ref_scale=1.0, model=None):
    """ref2va 编码统一入口：编码后打印**真实**调用信息（槽位名=image N / Video N / Audio N）。

    生成路径固定 ref2va（多参考）——不存在 fl2va 首尾帧路径。视频未被引用时，
    视频与视频音轨端口由 _encode_ref_to_video 强制关闭，音频只会走独立音频端口。
    """
    probe = {}
    cond, latent = _encode_ref_to_video(
        clip, vae, audio_vae, prompt, width, height, length,
        ref_images=ref_images, ref_videos=ref_videos,
        ref_video_audios=[],       # 管理器不产出「视频音轨」；无视频时编码函数会硬关该端口
        ref_audios=ref_audios,     # 音频素材（角色音色/配乐/念白）都是独立参考音频
        ref_scale=ref_scale, slot_names=ref_names, probe=probe)
    info = _fmt_call_info(probe)
    print(f"[JZL-{tag}] {seg_label}调用信息：{info if info else '（纯文本，无参考素材）'}")
    if model is not None:
        _warn_audio_ref_variant(model, bool(ref_audios), tag)
    return cond, latent


def _model_ckpt_name(model):
    """反查当前加载的 checkpoint 文件名（用于判断 t2va / fl2va / ref2va 变体）。

    ComfyUI 的 load_torch_file 会给每个权重的 storage 打上来源路径
    （comfy.storage.annotate_state_dict → untyped_storage()._comfy_source_path），
    这里顺着权重读回来；读不到就返回 None（此时不做变体断言，只给通用提示）。
    """
    try:
        real = getattr(model, "model", model)
        for i, p in enumerate(real.parameters()):
            if i > 800:
                break
            st = getattr(p, "untyped_storage", None)
            if st is None:
                continue
            path = getattr(st(), "_comfy_source_path", None)
            if path:
                return os.path.basename(str(path))
    except Exception:
        pass
    return None


def _warn_audio_ref_variant(model, has_audio_refs, tag):
    """音色参考只有 ref2va 一条通路，这里把「模型变体不匹配」这个静默失败点说出来。

    H3 的 t2va / fl2va / ref2va 是**同一套架构的三个任务变体**（ComfyUI 源码
    comfy/ldm/minimax/model.py 顶部注明：t2va/fl2va = [text | cond rows | audio | video]，
    ref2va = [text | reference blocks | audio | video]）。参考**图片**能通过 tokenizer 的
    视觉嵌入进入文本流，所以在任何变体上都"看起来生效"；而参考**音频**在 tokenizer 里
    只有一个 `<Audio N>: ` 文本标签、没有任何嵌入，完全依赖 ref2va 的 reference blocks
    → 加载 fl2va/t2va 变体时音色必然完全无效（且不报错）。故此处点名提示。
    """
    if not has_audio_refs:
        return
    name = _model_ckpt_name(model) or ""
    low = name.lower()
    if low and "ref2va" not in low and ("fl2va" in low or "t2va" in low):
        try:
            alts = [n for n in folder_paths.get_filename_list("diffusion_models")
                    if "ref2va" in n.lower()]
        except Exception:
            alts = []
        hint = ("；磁盘上可用的 ref2va 模型：" + "、".join(alts[:4])) if alts else ""
        _log_once(f"[JZL-{tag}] ⚠️ 当前加载的模型是「{name}」（fl2va/t2va 变体）→ **不支持参考素材**。"
                  f"参考音频只有 ref2va 的 reference blocks 一条通路，所以音色会完全无效"
                  f"（参考图片还能走视觉嵌入，所以看起来正常）。请改用 ref2va 变体模型{hint}")
    else:
        _log_once(f"[JZL-{tag}] ℹ️ 音色参考只通过 ref2va 的 reference blocks 生效"
                  f"（<Audio N> 在文本里只是个标签、没有嵌入）。若音色未生效，"
                  f"请确认当前加载的是 ref2va 变体模型——fl2va/t2va 会静默忽略参考音频")


def _sample_av(model, positive, latent, sample_decode, seed):
    """MiniMax H3 采样：NestedTensor(视频+音频) → 去噪 latent。"""
    _ensure_safe_attention(model)   # 50 系 + xformers：cutlass attention 不可用 → 自动回退 PyTorch SDPA
    steps = int(sample_decode.get("steps", 4) or 4)
    cfg = float(sample_decode.get("cfg", 1.0) or 1.0)
    denoise = float(sample_decode.get("denoise", 1.0) or 1.0)
    sampler_name = sample_decode.get("sampler", "res_multistep") or "res_multistep"
    scheduler = sample_decode.get("scheduler", "simple") or "simple"

    latent_image = latent["samples"]
    negative = []  # 空 negative（cfg=1 时不参与；[] 可安全通过 convert_cond）
    noise = comfy.sample.prepare_noise(latent_image, seed, None)
    disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED
    samples = comfy.sample.sample(
        model, noise, steps, cfg, sampler_name, scheduler,
        positive, negative, latent_image,
        denoise=denoise, disable_pbar=disable_pbar, seed=seed,
    )
    return samples


def _sample_av_custom_sigmas(model, positive, latent, sigmas_text, sampler_name, seed, cfg=1.0):
    """自定义西格玛二采：按 1190 工作流 ManualSigmas → SamplerCustomAdvanced 方式采样。

    直接把 sigmas 序列（如 "0.8500, 0.6316, 0.3158, 0.0000"）喂给采样器，不走调度器；
    sampler 用二采「K采样器」，cfg 沿用一采，种子与一采共享。
    """
    import re as _re
    latent_image = latent["samples"]
    negative = []  # 空 negative（cfg=1 时不参与）
    values = [float(x) for x in _re.findall(r"[-+]?(?:\d*\.*\d+)", sigmas_text or "")]
    if len(values) < 2:
        raise RuntimeError("自定义 Sigmas 无效：请填写形如 0.8500, 0.6316, 0.3158, 0.0000（至少 2 个，末位通常为 0）")
    sigmas = torch.FloatTensor(values)
    noise = comfy.sample.prepare_noise(latent_image, seed, None)
    sampler = comfy.samplers.sampler_object(sampler_name or "euler")
    disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED
    samples = comfy.sample.sample_custom(
        model, noise, float(cfg), sampler, sigmas, positive, negative, latent_image,
        disable_pbar=disable_pbar, seed=seed,
    )
    return samples


_JZL_UP_MODEL_INFO = {}
# 每段都会重算/重复触发的日志 → 同一句只打印一次
_JZL_LOG_ONCE_SEEN = set()


class _JzlQuietSelfLift(logging.Filter):
    """精简内置 SelfLift 副本的 INFO 日志：只去掉逐行刷屏，保留采样进度与耗时。

    上游一次运行会打十几行 INFO。这里仅屏蔽「每步一行 / 起止重复行 / 毫秒级子项」，
    保留 `[SelfLift plan]`、`[SelfLift upscaler]`、各阶段 `*_total` 与 `paired_lifts` ——
    仍能看出本次跑到哪一步、慢在哪个阶段。需要完整日志时设环境变量 SELFLIFT_VERBOSE=1。
    """

    _MUTE = (
        "] low_resolution step ",            # 低分辨率每步一行（tqdm 进度条已给出进度）
        "] high_resolution step ",           # 高分辨率每步一行
        "] low_resolution start",            # 与 low_resolution total 重复
        "] high_resolution start",           # 与 high_resolution total 重复
        "] transition start",                # 与 transition total 重复
        "] transition prepare_endpoint:",    # 毫秒级子项（已含在 transition total）
        "] transition correction_and_debug:",
        "] transition renoise:",
    )

    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        if os.environ.get("SELFLIFT_VERBOSE", "0") == "1":
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if not msg.startswith("[SelfLift "):
            return True
        return not any(s in msg for s in self._MUTE)


_JZL_FILTER_INSTANCE = _JzlQuietSelfLift()
_JZL_FILTERED_HANDLERS = set()


def _install_log_filters():
    """把 SelfLift 静音过滤器挂到 root logger 及其 handlers（幂等；每次调用都补挂新出现的 handler）。"""
    try:
        root = logging.getLogger()
        if _JZL_FILTER_INSTANCE not in root.filters:
            root.addFilter(_JZL_FILTER_INSTANCE)
        for h in list(root.handlers):
            if id(h) not in _JZL_FILTERED_HANDLERS:
                h.addFilter(_JZL_FILTER_INSTANCE)
                _JZL_FILTERED_HANDLERS.add(id(h))
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  SelfLift 采样（内置副本 vendor_selflift，不依赖用户另装 comfyui-SelfLift）
# ═══════════════════════════════════════════════════════════════

def _sample_av_selflift(model, positive, latent, sample_decode, seed, vae, tag="管理器"):
    """SelfLift 渐进分辨率采样：低分辨率跑 transition_step 步 → 提升到全分辨率 → 收尾。

    与「SelfLift Progressive Sampler (MiniMax H3)」节点同一份实现（内置副本 vendor_selflift）：
      · sampler 必须是标准 Euler（SelfLift 复用过渡步的预测，NFE 数不变）
      · sigmas 由「调度器 + 采样步数」生成（等同 BasicScheduler）
      · latent_image = 目标分辨率的 H3 AV latent（定义尺寸与时长）
      · vae = H3 视频 VAE（过渡处像素锚点用；rho=0 时跳过 VAE 往返）
    """
    _install_log_filters()   # 屏蔽上游每个阶段/每步的 INFO 刷屏（要完整日志设 SELFLIFT_VERBOSE=1）
    try:
        from .vendor_selflift.nodes import SelfLiftH3Sampler
    except Exception as _e:
        raise RuntimeError(
            f"SelfLift 采样不可用：内置副本 vendor_selflift 加载失败（{type(_e).__name__}: {_e}）"
        ) from _e

    sl = dict((sample_decode or {}).get("selflift") or {})
    # SelfLift 步数是独立配置（默认 8），与常规采样的 steps 互不干扰
    steps = int(sl.get("steps") or SELFLIFT_STEPS_DEFAULT)
    scheduler = (sample_decode or {}).get("scheduler", "simple") or "simple"
    cfg = float((sample_decode or {}).get("cfg", 1.0) or 1.0)
    denoise = float((sample_decode or {}).get("denoise", 1.0) or 1.0)

    if steps < 2:
        raise RuntimeError("SelfLift 采样至少需要 2 步（低分辨率前缀 + 高分辨率收尾）")

    # transition_step 合法区间：[1, 步数-1]（末步必须是高分辨率收尾）
    ts_raw = int(sl.get("transition_step", 6) or 6)
    ts = max(1, min(ts_raw, steps - 1))
    if ts != ts_raw:
        _log_once(f"[JZL-{tag}] ⚠️ SelfLift 过渡步数 {ts_raw} 超出「采样步数 {steps}」允许范围（1~{steps - 1}）→ 已钳制为 {ts}")
    if denoise != 1.0:
        _log_once(f"[JZL-{tag}] ⚠️ SelfLift 采样忽略「降噪系数 {denoise}」（调度由 采样步数+调度器 决定）")

    latent_image = dict(latent or {})
    sigmas = comfy.samplers.calculate_sigmas(
        model.get_model_object("model_sampling"), scheduler, steps)
    sampler = comfy.samplers.sampler_object("euler")   # SelfLift 只接受标准 Euler

    up_raw = str(sl.get("upscaler_model") or "none").strip()
    if up_raw and up_raw.lower() != "none":
        # 二级子目录嗅探：用户/旧配置可能只存了文件名或旧路径 → 统一解析成磁盘上真实存在的相对路径
        _resolved = _resolve_upscaler_entry(up_raw)
        if _resolved is None:
            _avail = [e for e in _selflift_upscaler_options() if e != "none"]
            if _avail:
                up = _avail[0]
                _log_once(f"[JZL-{tag}] ⚠️ SelfLift 放大模型「{up_raw}」在磁盘上找不到 → 已自动改用：{up}")
            else:
                up = "none"
                _log_once(f"[JZL-{tag}] ⚠️ SelfLift 放大模型「{up_raw}」找不到，且 models/latent_upscale_models 下没有 H3 放大模型"
                          f"（含二级子目录）→ 改用 none（nearest 直接提升）")
        else:
            _ok, _p, _sz, _why, _fmt = _upscaler_file_check(_resolved)
            if not _ok:
                raise RuntimeError(
                    f"SelfLift 放大模型不可用：{_resolved}\n  路径：{_p or '（解析失败）'}\n"
                    f"  大小：{_sz} 字节（{_sz / 1048576:.2f} MB）\n  原因：{_why}\n"
                    f"  可用选项：{_selflift_upscaler_options()}")
            up = _resolved
            _JZL_UP_MODEL_INFO.clear()
            _JZL_UP_MODEL_INFO.update({"name": up, "path": _p, "size": _sz, "bad": False, "why": ""})
    else:
        up = "none"
    rho = float(sl.get("rho", 0.6))
    # 修正强度上下限：默认值对齐上游 SelfLift 节点（w_min=0.5 / w_max=1.0）
    w_min, w_max = float(sl.get("w_min", 0.5)), float(sl.get("w_max", 1.0))
    if rho <= 0.0 and up == "none":
        raise RuntimeError(
            "SelfLift 采样参数无效：rho=0 且 放大模型=none 会同时关闭「SelfLift 修正」与「外部 latent 提升」，"
            "请把 rho 调到 >0（推荐 0.6）或选择一个外部 H3 放大模型")
    _routes = ("外部 latent 提升" if up != "none" else "nearest 直接提升") + \
              (" + 像素锚点修正" if (rho > 0.0 and w_max > 0.0) else "")
    # 「二次采样」在 SelfLift 下无意义（SelfLift 自身即渐进分辨率采样）→ 并进本行说明，不再另占一行
    _skip2 = "（已跳过二次采样）" if (sample_decode or {}).get("second", {}).get("enabled") else ""
    # 「高分辨率分块」+ 衔接 keyframe + 参考素材：上游 h3_tiling 会在 _embed_and_pack 行数不匹配处崩。
    # 内置副本已打补丁（JZL_LOCAL_PATCH_TILE_KEYFRAME）；若补丁因覆盖升级消失则本次自动关闭分块。
    highres_tiling = bool(sl.get("highres_tiling", False))
    if highres_tiling and _selflift_seam_needs_tiling_patch(positive) and not _selflift_tiling_keyframe_patched():
        highres_tiling = False
        _log_once(f"[JZL-{tag}] ⚠️ 本次自动关闭 SelfLift「高分辨率分块」：vendor_selflift/h3_tiling.py 缺少"
                  f"「分块时同步裁窄衔接 keyframe」补丁（多为用上游文件覆盖过该目录所致）；"
                  f"开着会因 cond 行数不匹配直接崩。请按该文件顶部说明重新套用补丁后重启 ComfyUI。"
                  f"本次改用整画布高分辨率（显存占用更高）")
    print(f"[JZL-{tag}] SelfLift 采样：步数 {steps}（低分辨率 {ts} 步 + 高分辨率 {steps - ts} 步）｜"
          f"低分辨率缩放 {sl.get('lowres_scale', 0.5)}｜rho {rho}｜权重 {w_min}/{w_max}｜"
          f"放大模型 {up}｜路径 {_routes}{_skip2}")

    out = SelfLiftH3Sampler().sample(
        model=model, positive=positive, negative=[], vae=vae, latent_image=latent_image,
        sampler=sampler, sigmas=sigmas, seed=int(seed), cfg=cfg,
        transition_step=ts,
        lowres_scale=float(sl.get("lowres_scale", 0.5)),
        rho=rho, w_min=w_min, w_max=w_max,
        upscaler_model=up,
        highres_tiling=highres_tiling,
        upscaler_unload=bool(sl.get("upscaler_unload", True)),
    )[0]
    # SelfLift 节点声明 RETURN_TYPES=("LATENT",)，其 progressive_sample 返回的是
    # LATENT dict（{"samples": NestedTensor}）；本插件内部统一用 NestedTensor（与 _sample_av 一致），
    # 否则下游 _decode_av 会把 dict 当张量传给 vae.decode → AttributeError: 'dict' object has no attribute 'shape'
    if isinstance(out, dict):
        out = out.get("samples", out)
    return out


def _selflift_mode(sample_decode):
    """「采样分类」是否为 SelfLift采样。"""
    return str((sample_decode or {}).get("sample_mode") or SAMPLE_MODE_NORMAL) == SAMPLE_MODE_SELFLIFT


# 内置 vendor_selflift 的「分块时同步裁窄 keyframe cond」补丁标记（见 h3_tiling.py 顶部「本地补丁」）
_JZL_SL_TILING_PATCH_MARK = "JZL_LOCAL_PATCH_TILE_KEYFRAME"
_JZL_SL_TILING_PATCHED = {"checked": False, "value": True}


def _selflift_tiling_keyframe_patched():
    """内置 SelfLift 副本的 _tile_payload 是否带该补丁。

    用户若按 vendor_selflift/h3_tiling.py 顶部说明「用上游新版覆盖本目录」→ 补丁消失。
    检测失败一律按「已修」处理（不误伤正常路径）。
    """
    if not _JZL_SL_TILING_PATCHED["checked"]:
        ok = True
        try:
            import inspect as _inspect
            from .vendor_selflift import h3_tiling as _h3t
            ok = _JZL_SL_TILING_PATCH_MARK in _inspect.getsource(_h3t._tile_payload)
        except Exception:
            ok = True
        _JZL_SL_TILING_PATCHED.update({"checked": True, "value": ok})
    return _JZL_SL_TILING_PATCHED["value"]


def _selflift_seam_needs_tiling_patch(positive):
    """本次采样是否会踩到该缺陷：positive 同时带衔接 keyframe 与参考素材（缺一则行数仍一致）。"""
    try:
        meta = (positive or [])[0][1] or {}
        return bool(meta.get("minimax_keyframes")) and bool(meta.get("minimax_refs"))
    except Exception:
        return False


def _sample_first_pass(model, positive, latent, sample_decode, seed, vae, tag="管理器"):
    """一采入口：按「采样分类」分派 常规采样 / SelfLift采样。"""
    if str((sample_decode or {}).get("sample_mode") or SAMPLE_MODE_NORMAL) == SAMPLE_MODE_SELFLIFT:
        return _sample_av_selflift(model, positive, latent, sample_decode, seed, vae, tag=tag)
    return _sample_av(model, positive, latent, sample_decode, seed)


def _semantic_bridge_on(manager):
    """「生成设定 → 语义修正」是否开启（语义桥）。"""
    return bool(((manager or {}).get("semantic_bridge") or {}).get("enabled"))


def _apply_semantic_bridge(positive, manager, tag="管理器"):
    """语义修正（语义桥）：用内置副本 vendor_semantic_bridge 修正 H3 文本条件 [B,T,5120]。

    与「MiniMax H3 Image to Video + Semantic Bridge」节点同一份实现：
    hybrid = h + alpha * (student(rms_norm(h)) - h)，按 per_token / global 做幅度匹配。
    适配器放在 ComfyUI/models/semantic_bridge/（推荐 MiniMaxH3_SemanticBridge_v1.safetensors，alpha=0.10）。
    """
    cfg = (manager or {}).get("semantic_bridge") or {}
    if not cfg.get("enabled"):
        return positive
    try:
        from .vendor_semantic_bridge.nodes import _apply_distilled_bridge, _list_safetensors
    except Exception as _e:
        raise RuntimeError(
            f"语义修正不可用：内置副本 vendor_semantic_bridge 加载失败（{type(_e).__name__}: {_e}）"
        ) from _e

    adapter = str(cfg.get("adapter") or "").strip()
    if not adapter:
        _avail = [a for a in _list_safetensors() if not a.startswith("NO_DISTILLED_ADAPTER_FOUND")]
        adapter = _avail[0] if _avail else ""
    if not adapter:
        raise RuntimeError(
            "语义修正已开启，但未找到适配器。请把 MiniMaxH3_SemanticBridge_v1.safetensors 放入 "
            "ComfyUI/models/semantic_bridge/ 后重启，或在「运行参数设置 → 语义修正」里指定适配器文件名")
    alpha = float(cfg.get("alpha", 0.10))
    mm = str(cfg.get("magnitude_match") or "per_token")
    # 内置副本首次加载适配器时会 print("[MiniMax H3 Semantic Bridge] Loaded adapter: …")，
    # 与下面本插件的日志重复 → 首次调用时把它的 stdout 吃掉（只影响这一次加载，不碰 vendor 源码）
    if not getattr(_apply_semantic_bridge, "_logged", False):
        import contextlib as _ctx
        import io as _io
        with _ctx.redirect_stdout(_io.StringIO()):
            out = _apply_distilled_bridge(positive, adapter, alpha, mm)
        _apply_semantic_bridge._logged = True
        print(f"[JZL-{tag}] 语义修正（语义桥）：适配器 {adapter}｜alpha {alpha}｜幅度匹配 {mm}")
    else:
        out = _apply_distilled_bridge(positive, adapter, alpha, mm)
    return out


def _detect_weight_format(p):
    """探测权重文件真实格式：'zip'（PyTorch .pth）/ 'safetensors' / ''（未知或损坏）。"""
    try:
        _sz = os.path.getsize(p)
        with open(p, "rb") as f:
            _head = f.read(16)
    except Exception:
        return ""
    if _head[:2] == b"PK":
        return "zip"
    try:
        if len(_head) >= 9:
            _n = int.from_bytes(_head[:8], "little")
            if 0 < _n < _sz and _head[8:9] == b"{":
                return "safetensors"
    except Exception:
        pass
    return ""


def _list_upscaler_entries():
    """latent_upscale_models 下全部权重（**含二级/更深子目录**），返回相对路径列表。

    主路径走 folder_paths.get_filename_list（ComfyUI 自身就是递归的）；万一该版本不递归，
    这里再按 os.walk 兜底扫盘一次——保证「模型放在 MiniMax-H3\\ 这类二级文件夹里」也能被看到。
    """
    _KEY = "latent_upscale_models"
    _EXT = (".pth", ".safetensors")
    out = []
    try:
        out = [n for n in folder_paths.get_filename_list(_KEY)
               if os.path.splitext(n)[1].lower() in _EXT]
    except Exception:
        out = []
    if not out:
        try:
            bases = folder_paths.get_folder_paths(_KEY)
        except Exception:
            bases = [os.path.join(folder_paths.models_dir, _KEY)]
        seen = set()
        for _b in bases:
            for _root, _dirs, _files in os.walk(_b):
                for _f in _files:
                    if os.path.splitext(_f)[1].lower() in _EXT:
                        _rel = os.path.relpath(os.path.join(_root, _f), _b)
                        if _rel not in seen:
                            seen.add(_rel)
                            out.append(_rel)
        out = sorted(out)
    return out


def _semantic_bridge_adapters():
    """语义桥可用适配器（models/semantic_bridge/ 下的 .safetensors，含二级子目录）。

    沿用内置副本 vendor_semantic_bridge 的列表口径（与上游节点下拉完全一致），
    并过滤掉它用于占位的 NO_DISTILLED_ADAPTER_FOUND。
    """
    try:
        from .vendor_semantic_bridge.nodes import _list_safetensors
        items = list(_list_safetensors())
    except Exception:
        items = []
    return [n for n in items if not str(n).startswith("NO_DISTILLED_ADAPTER_FOUND")]


def _resolve_upscaler_entry(name):
    """把放大模型名解析成「相对路径」——支持只填文件名（自动在子目录里嗅探）。

    依次尝试：① 完整相对路径（含子目录，正/反斜杠都认）→ ② basename 嗅探（同名多个时优先 h3）
    → ③ basename 部分匹配。返回 None 表示磁盘上确实没有。
    """
    name = str(name or "").strip()
    if not name:
        return None
    norm = name.replace("/", os.sep).replace("\\\\", os.sep)
    entries = _list_upscaler_entries()
    # ① 完整相对路径
    for e in entries:
        if e.replace("/", os.sep) == norm:
            return e
    # ② 只给了文件名 → 在子目录里按 basename 嗅探
    base = os.path.basename(norm).lower()
    hits = [e for e in entries if os.path.basename(e).lower() == base]
    if hits:
        h3 = [e for e in hits if "h3" in e.lower()]
        return (h3 or hits)[0]
    # ③ 去掉扩展名后的部分匹配（容忍用户漏写 / 写错扩展名的情况留给后续校验报错）
    stem = os.path.splitext(base)[0]
    if stem:
        part = [e for e in entries if stem in os.path.basename(e).lower()]
        if part:
            h3 = [e for e in part if "h3" in e.lower()]
            return (h3 or part)[0]
    return None


def _selflift_upscaler_options():
    """SelfLift「放大模型」下拉选项：["none"] + 磁盘上可用的 **H3** 放大模型（含二级子目录）。

    与上游 SelfLift 节点 _upscaler_input() 同一口径：只列名字含 h3 的，避免把 Ltx 等
    其它架构的放大模型混进来（选中后会在内部报架构不符）。
    """
    return ["none"] + [e for e in _list_upscaler_entries() if "h3" in os.path.basename(e).lower()]


def _upscaler_file_check(name):
    """校验 latent 放大模型文件能否被官方加载器读取：返回 (ok, abspath, size, reason, fmt)。

    官方 `_load_raw_sd`：`.safetensors` 走 safetensors.torch.load_file，**其它扩展名一律 torch.load**
    → 真实格式与扩展名不符（例如 safetensors 内容却叫 .pth）、或文件损坏/半包，都会报
    PytorchStreamReader failed reading zip archive: failed finding central directory（2026-09-23 云机报障）。

    典型坏文件：Git LFS 指针（几十~几百字节）、下载中断的 .pth（zip 结构不完整）
    → torch.load 会报 `PytorchStreamReader failed reading zip archive: failed finding central directory`。
    """
    # 只填了文件名时先在二级子目录里嗅探出相对路径（否则 get_full_path 会解析不到）
    _resolved = _resolve_upscaler_entry(name)
    if _resolved:
        name = _resolved
    try:
        p = folder_paths.get_full_path("latent_upscale_models", name)
    except Exception:
        p = None
    if not p or not os.path.isfile(p):
        return (False, str(p or ""), 0, "文件不存在", "")
    try:
        size = os.path.getsize(p)
    except Exception:
        size = 0
    if size < 1024 * 1024:
        return (False, p, size, f"文件过小（{size} 字节，疑似下载未完成 / Git LFS 指针文件）", "")
    low = p.lower()
    _fmt = _detect_weight_format(p)
    _want = "safetensors" if low.endswith(".safetensors") else "zip"
    if _fmt == "":
        return (False, p, size, "无法识别的权重格式（文件损坏、下载未完成，或不是模型权重）", "")
    if _fmt == _want:
        return (True, p, size, "", _fmt)
    _ext = os.path.splitext(p)[1]
    if _fmt == "safetensors":
        return (False, p, size,
                f"内容是 safetensors 格式，但扩展名是 {_ext}——官方加载器按扩展名判断，会用 torch.load 读它并报 "
                f"PytorchStreamReader failed；把它改名为 .safetensors 即可直接使用", _fmt)
    return (False, p, size,
            f"内容是 PyTorch zip（.pth）格式，但扩展名是 {_ext}——官方加载器会用 safetensors 读它；"
            f"把它改名为 .pth 即可直接使用", _fmt)


def _log_once(msg):
    """同一条日志只打印一次（二采放大模型解析、解码前清理等每段都会重复触发）。"""
    if msg in _JZL_LOG_ONCE_SEEN:
        return
    _JZL_LOG_ONCE_SEEN.add(msg)
    print(msg)


def _report_save_failure(tag, out_root, out_dir, saved, errors, ctx_lines=()):
    """落盘 0 个视频时的自检：打印「第一原因 + 报告路径」，并写完整环境报告供用户回传。

    用户报「本段未落盘任何视频」时，控制台这两行 + output/jzl/_诊断/ 下的报告，
    就能直接看出是「没接 model/CLIP/VAE」还是「没装 ffmpeg / 目录不可写 / 磁盘满」。
    """
    first = ""
    for e in (errors or []):
        s = str(e).strip()
        if s:
            first = s.splitlines()[0][:200]
            break
    ctx = [f"节点={tag}", f"落盘目录={out_dir}", f"成功落盘视频数={len(saved)}",
           f"ffmpeg={_resolve_ffmpeg() or '❌ 未找到'}（{_ffmpeg_reason()}）",
           f"回退编码器={_pyav_reason()}"]
    ctx += [str(x) for x in (ctx_lines or [])]
    if errors:
        ctx.append(f"本次错误（{len(errors)} 条）：")
        ctx += [f"  - {str(e)[:400]}" for e in errors[:12]]
    if _JZL_SEG_TRACEBACKS:
        ctx.append("分段失败调用栈（最外层失败点在每段最后一行）：")
        ctx += list(_JZL_SEG_TRACEBACKS)
        _JZL_SEG_TRACEBACKS.clear()
    try:
        from .jzl_env_check import build_report
        _text, fp = build_report(ctx, out_dir=out_root)
    except Exception as e:
        fp = f"（自检报告生成失败：{type(e).__name__}: {e}）"
    if first:
        print(f"[JZL-{tag}] ❗落盘失败原因：{first}")
    print(f"[JZL-{tag}] ❗环境自检报告（请把该文件发回，或运行 jzl_diagnose.py）：{fp}")
    return fp


def _resolve_upscaler_model(precision="fp32", explicit_model=""):
    """按精度解析 MiniMax H3 latent 放大模型文件名（官方 fp32/fp16/bf16 是同一权重的三套精度格式）。

    背景：官方把放大模型按精度拆成三个文件（fp32.pth / fp16.safetensors / bf16.safetensors），
    很多用户只下载其中一套（例如只装了 fp16）→ 若代码锁死加载 fp32.pth 必然 "Model file not found"。
    规则：
    - 若用户显式指定了非 MiniMax 标准模型（自定义其它放大模型）→ 尊重用户选择；
    - 候选顺序：显式指定 → 所选精度 → 其它精度（同权重不同精度，配合 precision 参数运行等价）；
    - ⭐ 每个候选都做**文件可用性校验**（存在性/大小/zip 或 safetensors 结构），跳过损坏或未下载完整的
      文件（云机常见：Git LFS 指针、下载中断）→ 避免 torch.load 报看不懂的 zip 错误；
    - 全部候选都不可用：打印每个候选的具体问题，并返回首选名（让官方节点报错，同时我们已打印原因）。
    """
    prec = str(precision or "fp32").strip().lower()
    if prec not in ("fp32", "fp16", "bf16"):
        prec = "fp32"
    exp = (explicit_model or "").strip()
    _key = {"fp32": "_fp32", "fp16": "_fp16", "bf16": "_bf16"}[prec]

    try:
        _names = folder_paths.get_filename_list("latent_upscale_models")
    except Exception:
        _names = []
    _std = []
    for _n in _names:
        _base = os.path.basename(_n)
        if (_base.startswith("minimax_h3_latent_upscaler_3d")
                and _base.lower().endswith((".pth", ".safetensors"))):
            _std.append(_n)

    # 自定义非 MiniMax 模型：尊重用户（但也校验，并给出提示）
    if exp and "minimax_h3_latent_upscaler_3d" not in exp:
        _ok, _p, _sz, _why, _fmt = _upscaler_file_check(exp)
        if _ok:
            _log_once(f"[JZL-管理器] 二采放大模型：{exp}（自定义，{_sz / 1048576:.1f} MB）")
        else:
            _log_once(f"[JZL-管理器] ⚠️ 二采放大模型（自定义）{exp} 校验未通过：{_why}")
        _JZL_UP_MODEL_INFO.clear()
        _JZL_UP_MODEL_INFO.update({"name": exp, "path": _p, "size": _sz, "bad": not _ok, "why": _why})
        return exp

    cands = []
    if exp:
        cands.append(exp)
    cands += [n for n in _std if _key in os.path.basename(n)]
    cands += [n for n in _std if _key not in os.path.basename(n)]
    _seen = set()
    cands = [c for c in cands if not (c in _seen or _seen.add(c))]

    _bad = []
    _fmt_mismatch = []       # 内容正确但扩展名不符 → 提示改名，并作为兵底候选
    for c in cands:
        _ok, _p, _sz, _why, _fmt = _upscaler_file_check(c)
        if _ok:
            _log_once(f"[JZL-管理器] 二采放大模型：{c}（{_sz / 1048576:.1f} MB）")
            if exp and c != exp:
                _log_once(f"[JZL-管理器] 指定的放大模型 {exp} 不可用，已自动改用：{c}")
            elif (not exp) and (_key not in os.path.basename(c)):
                _log_once(f"[JZL-管理器] 未找到 minimax_h3_latent_upscaler_3d_{prec} 权重，自动改用磁盘现有模型：{c}")
            _JZL_UP_MODEL_INFO.clear()
            _JZL_UP_MODEL_INFO.update({"name": c, "path": _p, "size": _sz, "bad": False, "why": ""})
            return c
        if _fmt:
            _fmt_mismatch.append((c, _p, _sz, _why))
        _bad.append((c, _p, _sz, _why))
        _log_once(f"[JZL-管理器] 跳过不可用的放大模型候选：{c} → {_p or '路径解析失败'}"
                f"（{_sz} 字节）: {_why}")

    if _fmt_mismatch:
        _c, _p2, _sz2, _why2 = _fmt_mismatch[0]
        _log_once(f"[JZL-管理器] ⚠️ 放大权重扩展名与内容不符：{_c} → {_p2}（{_sz2} 字节）")
        _log_once(f"[JZL-管理器]    {_why2}")
        _JZL_UP_MODEL_INFO.clear()
        _JZL_UP_MODEL_INFO.update({"name": _c, "path": _p2, "size": _sz2, "bad": True, "why": _why2})
        return _c

    if _bad:
        _log_once(f"[JZL-管理器] ⚠️ 放大模型候选全部不可用（共 {len(_bad)} 个）：")
        for c, p, sz, why in _bad[:6]:
            _log_once(f"[JZL-管理器]    - {c} → {p or '路径解析失败'}（{sz} 字节）: {why}")
        _log_once("[JZL-管理器]    请重新下载权重放入 ComfyUI/models/latent_upscale_models/"
                "（官方三套精度：fp32.pth / fp16.safetensors / bf16.safetensors）")
        _JZL_UP_MODEL_INFO.clear()
        _JZL_UP_MODEL_INFO.update({"name": _bad[0][0], "path": _bad[0][1], "size": _bad[0][2],
                                   "bad": True, "why": _bad[0][3]})
        return _bad[0][0]

    _ext = ".pth" if prec == "fp32" else ".safetensors"
    _log_once(f"[JZL-管理器] 警告: latent_upscale_models 目录没有 minimax_h3_latent_upscaler_3d 模型, "
            f"请先下载 {prec} 精度权重放入 ComfyUI/models/latent_upscale_models/")
    return f"minimax_h3_latent_upscaler_3d_{prec}{_ext}"


def _import_upscaler_3d():
    """获取 MinimaxH3LatentUpscaler3D 类（二采 latent 放大用）。

    查找顺序（越靠前越确定）：
      ① 本插件**内置副本**（`jzl_h3_latent_upscaler_3d.py`）——用户无需另装该节点包，
         行为不受用户环境影响，也彻底避免「多份同名副本」问题；
      ② ComfyUI 全局注册表（用户自装的该节点，若已成功加载）——兜底，尊重用户装的那份；
      ③ 扫 custom_nodes 下**所有**同名副本逐个尝试（包路径 / 裸模块名 / 按文件加载）。
         旧版找到第一个就 break：一旦那份额外副本无法独立加载（例如内部用相对导入，
         只有作为自己插件包的一员才成立），就会跳过后面能用的副本、误报「没安装」；
      ④ 全部失败时，把「每份副本为何失败」汇总进报错，避免误导排查方向。

    内置副本仅用于内部调用，**不导出**它的 NODE_CLASS_MAPPINGS（不会与用户自装的同名节点重复注册）。
    来源与协议见 THIRD_PARTY_NOTICES.md（MIT, Copyright (c) 2026 LBH-123-AI）。
    """
    import importlib.util
    import sys as _sys

    # 1) 内置副本（首选）
    try:
        from .jzl_h3_latent_upscaler_3d import MinimaxH3LatentUpscaler3D as _VENDORED
        return _VENDORED
    except Exception as _ev:
        _vendor_err = f"{type(_ev).__name__}: {_ev}"
        try:
            _vf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jzl_h3_latent_upscaler_3d.py")
            _vspec = importlib.util.spec_from_file_location("_jzl_vendored_up3d", _vf)
            _vmod = importlib.util.module_from_spec(_vspec)
            _vspec.loader.exec_module(_vmod)
            return _vmod.MinimaxH3LatentUpscaler3D
        except Exception as _ev2:
            _vendor_err += f"；按文件加载 → {type(_ev2).__name__}: {_ev2}"

    # 2) ComfyUI 全局注册表（用户自装的那份）
    try:
        from nodes import NODE_CLASS_MAPPINGS as _GLOBAL_MAP
        _cls = _GLOBAL_MAP.get("MinimaxH3LatentUpscaler3D")
        if _cls is not None:
            return _cls
    except Exception as _e:
        _global_err = f"{type(_e).__name__}: {_e}"
    else:
        _global_err = "未注册（该插件可能加载失败）"

    # 3) 收集 custom_nodes 下所有同名副本
    _cands = []
    _cn = ""
    try:
        _base = os.path.abspath(getattr(
            folder_paths, "base_path", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        _cn = os.path.join(_base, "custom_nodes")
        if os.path.isdir(_cn):
            for _root, _dirs, _files in os.walk(_cn):
                if "minimax_h3_latent_upscaler_3d.py" in _files:
                    _cands.append(os.path.join(_root, "minimax_h3_latent_upscaler_3d.py"))
    except Exception:
        _cands, _cn = [], ""

    _fails = []
    for _f in _cands:
        _why = []
        _mod = None

        # 3a) 包路径导入（把 custom_nodes 当包根；连字符目录名会被跳过）
        _parts = os.path.relpath(_f, _cn).replace(os.sep, "/").split("/")
        _parts[-1] = _parts[-1][:-3] if _parts[-1].endswith(".py") else _parts[-1]
        if all(_p.isidentifier() for _p in _parts):
            _ins = _cn not in _sys.path
            if _ins:
                _sys.path.insert(0, _cn)
            try:
                _mod = importlib.import_module(".".join(_parts))
            except Exception as _e:
                _why.append(f"包路径 {'.'.join(_parts)} → {type(_e).__name__}: {_e}")
            finally:
                if _ins:
                    try:
                        _sys.path.remove(_cn)
                    except ValueError:
                        pass
        else:
            _why.append(f"目录名不可作包名（{_parts[0]}），跳过包路径导入")

        # 3b) 裸模块名 / 按文件加载（不复用别的副本已缓存的同名模块）
        if _mod is None or not hasattr(_mod, "MinimaxH3LatentUpscaler3D"):
            _cached = _sys.modules.get("minimax_h3_latent_upscaler_3d")
            if _cached is not None and os.path.abspath(getattr(_cached, "__file__", "") or "") != os.path.abspath(_f):
                _sys.modules.pop("minimax_h3_latent_upscaler_3d", None)
            _pdir = os.path.dirname(_f)
            _ins = _pdir not in _sys.path
            if _ins:
                _sys.path.insert(0, _pdir)
            try:
                _mod = importlib.import_module("minimax_h3_latent_upscaler_3d")
            except Exception as _e:
                _why.append(f"裸模块名 → {type(_e).__name__}: {_e}")
                try:
                    _spec = importlib.util.spec_from_file_location("_jzl_upscaler_3d", _f)
                    _m2 = importlib.util.module_from_spec(_spec)
                    _spec.loader.exec_module(_m2)
                    _mod = _m2
                except Exception as _e2:
                    _why.append(f"按文件加载 → {type(_e2).__name__}: {_e2}")
                    _mod = None
            finally:
                if _ins:
                    try:
                        _sys.path.remove(_pdir)
                    except ValueError:
                        pass

        if _mod is not None and hasattr(_mod, "MinimaxH3LatentUpscaler3D"):
            return getattr(_mod, "MinimaxH3LatentUpscaler3D")
        _fails.append((_f, "；".join(_why) or "模块内没有 MinimaxH3LatentUpscaler3D 类"))

    # 4) 全部失败：给出可诊断的报错（不再让用户猜「到底装没装」）
    _msg = ("缺少「Minimax H3 Latent Upscaler (3D)」自定义节点"
            "（Comfyui_Minimax_h3_latent_Upscaler），请先在 custom_nodes 安装")
    _msg += f"\n  [JZL诊断] 插件内置副本：{_vendor_err}"
    _msg += f"\n  [JZL诊断] 全局注册表：{_global_err}"
    if _fails:
        _msg += f"\n  [JZL诊断] 磁盘上找到 {len(_fails)} 份同名副本，但都无法加载："
        for _p, _r in _fails[:5]:
            _msg += f"\n    - {_p}\n        {_r}"
        _msg += ("\n    说明节点其实已安装，只是这份副本无法被独立加载（常见于多装了一份第三方副本）。"
                 "建议删除多余的同名副本、只保留官方 Comfyui_Minimax_h3_latent_Upscaler 后重启 ComfyUI。")
    elif not _cands:
        _msg += "\n  [JZL诊断] custom_nodes 下没有找到 minimax_h3_latent_upscaler_3d.py。"
    raise RuntimeError(_msg)


def _run_second_sampling(model, positive, samples, sample_decode, second, upscale_scale, seed, tag="管理器"):
    """复刻 1180 二采链路：一采 latent → LTXV分离音视频 → 视频latent放大(MinimaxH3LatentUpscaler3D)
    → LTXVConcatAVLatent 合并 → 按二采参数再次采样。

    ref2va 的一采/二采共用同一个 positive：参考素材是身份参考，不受输出分辨率绑定；
    只有 fl2va（首/尾帧 keyframe 按输出分辨率嵌入）才需要在放大分辨率重新编码 positive，
    管理器始终 ref2va，直接复用一采的 positive。种子与一采共享，cfg 沿用一采。
    """
    try:
        from comfy_extras.nodes_lt import LTXVSeparateAVLatent, LTXVConcatAVLatent
    except Exception as e:
        raise RuntimeError(f"缺少官方节点 LTXVSeparateAVLatent/LTXVConcatAVLatent（comfy-core 自带，请升级 ComfyUI）：{e}")
    MinimaxH3LatentUpscaler3D = _import_upscaler_3d()

    # 1) 分离音视频潜空间
    sep = LTXVSeparateAVLatent.execute({"samples": samples})
    video_latent, audio_latent = sep.args[0], sep.args[1]

    # 2) 视频 latent 放大（scale by multiplier，倍数=二采放大；对齐 32）
    #    签名自适应：云机可能是官方原版（enable_temporal_chunking/force_unload），
    #    本地是定制版（enable_chunking）——用 inspect 检测实际参数名，按需传参，兼容两者。
    try:
        import inspect as _inspect
        _params = set(_inspect.signature(MinimaxH3LatentUpscaler3D.execute).parameters.keys())
    except Exception:
        _params = set()
    _up_kwargs = {
        "latent": video_latent,
        "model_name": _resolve_upscaler_model(second.get("precision") or "fp32", second.get("upscaler_model") or ""),
        "mode": {"mode": "scale by multiplier", "scale": float(upscale_scale or 1.0)},
        "align": 32,
        "device": second.get("device") or "cuda",
        "precision": second.get("precision") or "fp32",
    }
    _chunk = bool(second.get("enable_chunking", True))
    if "enable_chunking" in _params:
        _up_kwargs["enable_chunking"] = _chunk
    if "enable_temporal_chunking" in _params:
        _up_kwargs["enable_temporal_chunking"] = _chunk
    if "force_unload" in _params:
        _up_kwargs["force_unload"] = True
    if "keep_proportion" in _params:
        _up_kwargs["keep_proportion"] = True
    if "offload_after_upscale" in _params:
        _up_kwargs["offload_after_upscale"] = True
    try:
        up = MinimaxH3LatentUpscaler3D.execute(**_up_kwargs)
    except Exception as _ue:
        _umsg = str(_ue)
        if ("PytorchStreamReader" in _umsg) or ("central directory" in _umsg) or ("zip archive" in _umsg):
            _info = dict(_JZL_UP_MODEL_INFO or {})
            _p = _info.get("path") or ""
            _sz = int(_info.get("size") or 0)
            _why = str(_info.get("why") or "")
            raise RuntimeError(
                f"{_umsg}\n"
                f"  [JZL提示] 这是**放大模型权重文件本身有问题**（torch.load 读不出 zip 结构），不是本节点的 bug：\n"
                f"    实际加载：{_up_kwargs.get('model_name')}\n"
                f"    绝对路径：{_p or '（未能解析）'}\n"
                f"    文件大小：{_sz} 字节（{_sz / 1048576:.2f} MB）\n"
                f"    {('节点校验结论：' + _why) if _why else '若该文件明显偏小（几百 KB）→ 是 Git LFS 指针 / 下载未完成，请重新下载'}\n"
                f"    也可在「采样与解码设置」把二采「精度」切到已下载完整的那一套（fp32.pth / fp16.safetensors / bf16.safetensors），"
                f"或直接指定放大模型文件。") from _ue
        raise
    up_latent = up.args[0]

    # 2.5) ♾️ 无限时长衔接：二采画布被放大，而 positive 里的 keyframe 引导 latent 取自「一采画布」
    #      → PackedLayout 按二采画布算 cond 行数、引导 latent 按自己 h/w 排行 → 行数不匹配必崩。
    #      此处把引导 latent 对齐到二采画布（尺寸一致时是空操作，纯 ref2va 无 keyframe 时也是空操作）。
    positive = _seam_fit_guides_to_canvas(positive, up_latent, tag=tag)

    # 3) 合并音视频潜空间
    merged = LTXVConcatAVLatent.execute(up_latent, audio_latent).args[0]

    # 4) 二次采样：与一采共用 positive（ref2va 参考不受输出分辨率绑定；种子与一采共享，cfg 沿用一采）
    #    Sigmas 模式：scheduler=调度器+步数+降噪；custom=直接用自定义 sigmas 序列（对齐 1190 ManualSigmas→sample_custom）
    cfg = float(sample_decode.get("cfg", 1.0) or 1.0)
    _model2 = _maybe_wrap_tiling(model, second, up_latent, audio_latent, tag=tag)
    if (second.get("sigmas_mode") or "scheduler") == "custom":
        return _sample_av_custom_sigmas(
            _model2, positive, merged,
            second.get("custom_sigmas") or "0.8500, 0.6316, 0.3158, 0.0000",
            second.get("sampler") or "euler", seed, cfg)
    second_sd = dict(sample_decode or {})
    second_sd["sampler"] = second.get("sampler") or "euler"
    second_sd["scheduler"] = second.get("scheduler") or "simple"
    second_sd["steps"] = int(second.get("steps", 3) or 3)
    second_sd["denoise"] = float(second.get("denoise", 0.3) or 0.3)
    return _sample_av(_model2, positive, merged, second_sd, seed)


def _maybe_wrap_tiling(model, second, up_latent, audio_latent, tag="管理器"):
    """二采空间分块：按配置给 model 包一层「前向内切块 + 升余弦融合」（省显存）。

    只在二采生效（一采保持整画布）；放大环节不变 —— 分块解决的是「放大后画布太大采不动」。
    返回可能被包装过的 model；任何异常都退回原 model（宁可慢也不要跑不起来）。
    """
    mode = str((second or {}).get("tiling") or "关闭")
    if mode == "关闭":
        return model
    try:
        from . import jzl_tiling as _tiling
    except Exception as e:
        print(f"[JZL-{tag}] ⚠️ 空间分块模块加载失败，本次按整画布二采：{type(e).__name__}: {e}")
        return model
    try:
        v = up_latent["samples"] if isinstance(up_latent, dict) else up_latent
        a = audio_latent["samples"] if isinstance(audio_latent, dict) else audio_latent
        if v is None or a is None:
            return model
        overlap_px = int((second or {}).get("tiling_overlap", 128) or 0)
        tiles = int((second or {}).get("tiling_tiles", 2) or 2) if mode == "手动" else 0
        return _tiling.wrap_model(model, (tuple(int(x) for x in v.shape), tuple(int(x) for x in a.shape)),
                                  tiles=tiles, overlap_px=overlap_px, tag=tag)
    except Exception as e:
        print(f"[JZL-{tag}] ⚠️ 空间分块初始化失败，本次按整画布二采：{type(e).__name__}: {e}")
        return model


def _clean_cache():
    """轻量碎片清理：gc.collect（RAM）+ soft_empty_cache + empty_cache（不卸载模型）。

    管理器在分段循环里每段解码/释放后调用——若卸载模型会导致下一段重新加载（慢 + 重复刷屏
    「prepared for dynamic VRAM loading」）；轻量清理既腾出缓存碎片（gc 回收 Python/RAM、
    empty_cache 并拢空闲显存块，配合 PYTORCH_CUDA_ALLOC_CONF=expandable_segments 抑制碎片）
    又不打断模型驻留。云机多段跑长图时每段调用可避免内存/显存碎片累积崩溃。
    """
    try:
        import gc
        gc.collect()
        import comfy.model_management as mm
        mm.soft_empty_cache()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception as e:
        print(f"[JZL-管理器] 显存清理失败：{e}")


def _cleanup_vram(level="卸载显存模型", tag="管理器", label="显存清理完成（卸载显存模型）"):
    """全量显存清理（对齐 XB-BOX「VAE解码（原版优化）」的 L2：unload_all_models 把模型退回内存）。

    label：日志文案——解码前用「解码前卸载显存模型…」，生成结束用默认文案。
    """
    if level != "卸载显存模型":
        return
    try:
        import comfy.model_management as mm
        mm.soft_empty_cache()
        mm.unload_all_models()
        mm.soft_empty_cache()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        import gc
        gc.collect()
        print(f"[JZL-{tag}] {label}")
    except Exception as e:
        print(f"[JZL-{tag}] 显存清理失败：{e}")


def _gpu_diag_hint():
    """遇到 CUDA/ROCm 内核不匹配（no kernel image）时，附上 torch/CUDA/显卡诊断信息。"""
    try:
        import torch as _t
        parts = [f"torch={_t.__version__} cuda={getattr(_t.version, 'cuda', None)}"]
        try:
            if _t.cuda.is_available():
                parts.append(f"gpu={_t.cuda.get_device_name(0)} cap={_t.cuda.get_device_capability(0)}")
            else:
                parts.append("gpu=cuda不可用")
        except Exception:
            pass
        return " | ".join(parts)
    except Exception:
        return ""


def _attention_diag_hint(err_text=""):
    """attention 后端（xformers）与显卡不兼容时的专项提示；不命中返回空串。"""
    t = str(err_text or "")
    low = t.lower()
    if ("memory_efficient_attention" not in low) and ("xformers" not in low) and ("capability <= (9, 0)" not in t):
        return ""
    return ("\n  [JZL提示] attention 后端与显卡不兼容：xformers 的 cutlass attention 只编译到 sm90 及以下，"
            "而你的 GPU 是 50 系（sm120）→ 该算子在 xformers 里不存在。三种解法（任一）：\n"
            "    ① 工作流里若用了「Model Attention Backend」节点，把 backend 选为 \"pytorch attention\"（推荐）；\n"
            "    ② 启动 ComfyUI 时加参数 --use-pytorch-cross-attention；\n"
            "    ③ 卸载 xformers（pip uninstall xformers）。")


def _oom_diag_hint(err_text="", sample_decode=None, width=0, height=0, length=0):
    """显存不足（OOM）时的专项提示；不命中返回空串。

    关键事实（本插件的日志实测）：一次前向的 MLP 激活 ≈ token 数 × ffn_hidden(14336) × 4B。
    H3 高分辨率阶段的 token 数 ≈ (帧数/17)*... 见 _packed_token_estimate：
    1664×928 × 294 帧 → 视频 131196 行 + 参考/衔接 ≈ 1.5 万行 → 单个激活缓冲就 8 GiB。
    所以「采样步数」几乎不影响占用，**画布面积 × 时长** 才是决定项；SelfLift 的
    「高分辨率分块」正是按空间切块来压低这个 token 数。
    """
    t = str(err_text or "")
    if ("out of memory" not in t.lower()) and ("OutOfMemoryError" not in t) and ("would exceed allowed memory" not in t):
        return ""
    tips = [f"\n  [JZL提示] 显存不足（OOM）：本次画布约 {int(width)}x{int(height)}、{int(length)} 帧"
            f"，参考/衔接条件 token 会额外叠加。"]
    items = []
    if sample_decode and _selflift_mode(sample_decode):
        sl = (sample_decode or {}).get("selflift") or {}
        if not sl.get("highres_tiling", False):
            items.append("【最有效】勾选「🎭 采样解码设置 → SelfLift 采样 → 高分辨率分块」："
                         "它把高分辨率阶段按空间切块，token 数与单块面积成正比，能直接把峰值降下来"
                         "（日志会打印 `[SelfLift tiling plan] tiles=N`）。")
        else:
            items.append("「高分辨率分块」已开启 → 可把「采样步数」里的**低分辨率步数**调小"
                         "（低分辨率阶段同样占显存），或再降画布/时长。")
        items.append(f"降低「每段视频时长」或「百万像素」：占用 ≈ 画布面积 × 时长，"
                     f"当前 {int(length)} 帧偏长（124 帧只有它的 42%）。")
        if sl.get("transition_step"):
            items.append("段间衔接的引导 keyframe 也会占用条件 token → 把「无限时长衔接⚠实验性功能 → 窗口」"
                         "调小（如 22 → 5）可省一部分。")
    else:
        items.append("降低「每段视频时长」或「百万像素」（占用 ≈ 画布面积 × 时长）。")
        items.append("若开了二次采样，勾选「二次采样 → 空间分块 = 自动」，让二采按块采样。")
    items.append("加环境变量 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True 可显著减少碎片导致的 OOM"
                 "（你的日志里出现过「已分配 6.25 GiB 但仅剩 0.43 GiB 空闲」，是典型碎片特征）。")
    _marks = "①②③④⑤⑥⑦⑧⑨"
    for k, it in enumerate(items):
        tips.append(f"\n    {_marks[k] if k < len(_marks) else str(k + 1)} {it}")
    return "".join(tips)


def _ensure_safe_attention(model=None, tag="管理器"):
    """Blackwell（sm100+）上 xformers 的 cutlass attention 不可用（只 <= sm90）→ 采样前自动回退 PyTorch SDPA。

    只在检测到风险时生效；同进程内只打印一次（幂等）。返回 True 表示已回退。
    """
    try:
        import torch as _t
        if not _t.cuda.is_available():
            return False
        _major, _minor = _t.cuda.get_device_capability(0)
    except Exception:
        return False
    if _major < 10:
        return False
    reverted = False
    try:
        from comfy.ldm.modules import attention as _attn
        # 只在 attention 实际绑定为 xformers 时才回退（sage/flash 不受影响）
        if getattr(getattr(_attn, "optimized_attention", None), "__name__", "") == "attention_xformers":
            _attn.optimized_attention = _attn.attention_pytorch
            _attn.optimized_attention_masked = _attn.attention_pytorch
            reverted = True
    except Exception:
        pass
    if reverted:
        try:
            import comfy.model_management as _mm
            _mm.XFORMERS_IS_AVAILABLE = False
        except Exception:
            pass
    if reverted and not getattr(_ensure_safe_attention, "_done", False):
        _ensure_safe_attention._done = True
        print(f"[JZL-{tag}] ⚠️ 检测到 xformers 在你的 GPU（sm{_major}{_minor}）上不受支持"
              f"（xformers cutlass attention 仅支持 ≤ sm90）→ 采样已自动改用 PyTorch SDPA attention"
              f"（本进程内生效，无需卸载 xformers）。")
        print(f"[JZL-{tag}]    如仍有 attention 报错：① 把工作流「Model Attention Backend」节点改为 \"pytorch attention\"；"
              f"② 或用 --use-pytorch-cross-attention 启动；③ 或卸载 xformers。")
    # 模型级 override（Model Attention Backend 节点）会盖过上述回退 → 单独提醒
    try:
        if model is not None:
            _ov = ((getattr(model, "model_options", None) or {}).get("transformer_options") or {}).get("optimized_attention_override")
            _nm = (getattr(_ov, "__name__", "") or "").lower()
            if _ov is not None and ("kitchen" in _nm or "xformers" in _nm) and not getattr(_ensure_safe_attention, "_ov_warned", False):
                _ensure_safe_attention._ov_warned = True
                print(f"[JZL-{tag}] ⚠️ 检测到模型被「Model Attention Backend」节点强制指定了 attention 后端（{_nm or 'custom'}）："
                      f"它会覆盖本节点的自动回退 → 请把该节点 backend 改为 \"pytorch attention\"。")
    except Exception:
        pass
    return reverted


_JZL_SEG_ERR_SEEN = {}
_JZL_SEG_TRACEBACKS = []   # 本次运行的分段失败调用栈（写入自检报告，便于远程定位具体失败行）

def _log_seg_error(tag, idx, err, tb=None):
    """分段失败日志：同一原因只详列一次（避免多段重复刷屏）；首次附带调用栈。

    ⚠️ 必须带调用栈：像 `too many values to unpack (expected 3)` 这种错误，
    只有 traceback 才能指出是哪一行（本包 / ComfyUI / 第三方节点）——否则只能靠猜。
    """
    try:
        key = f"{tag}|{str(err)[:180]}"
        prev = _JZL_SEG_ERR_SEEN.get(key)
        if prev is not None:
            print(f"[JZL-{tag}] 第{idx}段生成失败（与第 {prev} 段相同原因，不再重复打印）：{str(err).splitlines()[0][:200]}")
        else:
            _JZL_SEG_ERR_SEEN[key] = idx
            print(f"[JZL-{tag}] 第{idx}段生成失败：{err}")
            if tb:
                print(f"[JZL-{tag}] 第{idx}段调用栈（最外层失败点在最后一行，请把这几行一起发回）：")
                for _ln in str(tb).rstrip().splitlines()[-14:]:
                    print(f"[JZL-{tag}]   {_ln}")
    except Exception:
        print(f"[JZL-{tag}] 第{idx}段生成失败：{err}")


def _seg_fail_context(idx, tb):
    """记录分段失败的完整调用栈（自检报告用；每次运行最多留 6 条）。"""
    try:
        txt = f"── 第{idx}段 ──\n{str(tb).rstrip()}"
        if len(_JZL_SEG_TRACEBACKS) < 6:
            _JZL_SEG_TRACEBACKS.append(txt)
        return txt
    except Exception:
        return ""


def _decode_av(vae, audio_vae, samples, sample_decode=None, tag="管理器"):
    """NestedTensor latent → (IMAGE [T,H,W,C], AUDIO dict|None)。

    「解码前清理=卸载显存模型」时完全对齐 XB-BOX「VAE解码（原版优化）」：解码前先
    unload_all_models 把采样模型退回内存（制造显存真空区），再解码，显存峰值最低；
    代价是下一段采样需要把模型搬回显存（分段越多越慢，日志会显示重载）。
    未开启时不做卸载（分段间另有 _clean_cache 轻量清理）。
    """
    if (sample_decode or {}).get("decode_cleanup") == "卸载显存模型":
        _cleanup_vram("卸载显存模型", tag=tag,
                      label="解码前卸载显存模型")
    _assert_vae_roles(vae, audio_vae)
    if isinstance(samples, dict):        # 兼容 LATENT dict（SelfLift 等节点返回的就是这种）
        samples = samples.get("samples", samples)
    if getattr(samples, "is_nested", False):
        tensors = samples.tensors
        video_z = tensors[0]
        audio_z = tensors[1] if len(tensors) > 1 else None
    else:
        video_z = samples
        audio_z = None

    image = vae.decode(video_z)  # [B, T, H, W, C]
    if image.ndim == 5:
        image = image[0]  # [T, H, W, C]

    audio = None
    if audio_z is not None:
        if audio_vae is None:
            # 采样结果含音频潜空间但没接「音频VAE」→ 只解码视频，别在这里崩掉整段
            _log_once(f"[JZL-{tag}] ⚠️ 本段含音频潜空间但未连接「音频VAE」→ 只解码视频（该段无声）")
        else:
            waveform = audio_vae.first_stage_model.decode(audio_z)  # [B, 2, L]
            audio = {"waveform": waveform, "sample_rate": getattr(audio_vae, "audio_sample_rate", 32000)}
    return image, audio


# ── ♾️ 无限时长：段间衔接（上段一采 latent 尾部 → 下段 keyframe 引导）────────
# 全部为官方源码事实，不是估计：
#  · 视频 latent 的 T 轴不均匀：token k 覆盖 FRAME_PER_TOKEN[k%5]=(1,4,4,4,4) 帧，每 5 token = 17 帧；
#    align_frame_count 保证段长恒为 17k+5 → 段长 L=17k+5、窗口 W=17m+5 时 L-W=17(k-m) 必为 17 的倍数，
#    即尾窗起点永远落在 token 边界（token 索引 = 5(k-m)，必为 5 的倍数）→ **相位自动对齐**，无需手工调。
#  · 音频 latent 与视频共享同一条时间轴（1 个音频 latent 步 = 1 单位；视频 token 跨度 = FRAME_RESCALE×
#    FRAME_PER_TOKEN），所以窗口 W 帧对应的音频 latent 步数 = round(W × 5/3)。
#  · keyframe 的 cond 行永不降噪（PackedLayout 里 img_update=False）、每步重注入 → 段首 W 帧解码后
#    会原样出现在段首，必须裁掉；音频同步裁掉对应步数，否则音画错位 W/24 秒。
#  · 引导取「一采 latent」而非像素帧重编码：一采 latent 与下一段目标 latent 同画布同空间
#    （vae.encode 输出与采样输出同 latent 空间，scale_factor=1）→ 零往返损失、零累积漂移、省一次
#    VAE encode；二采（空间放大）只影响解码落盘，不影响该 latent，所以二采开关下都成立。
SEAM_WINDOW_CHOICES = (5, 22, 39, 56)   # 全部是 17m+5；22 = 官方 Motion Context 基线
SEAM_DEFAULT_WINDOW = 22
SEAM_DEFAULT_SETTLE = 12                # 过渡丢弃帧数默认（官方 Director 经验值）；= 使 窗口+过渡 ≡ 0 (mod 17)
SEAM_SETTLE_CHOICES = (0, 5, 8, 12, 17, 22, 29, 46)   # 面板可选值（运行时会对齐到 12/29/46，见 _seam_snap_settle）


def _seam_span_constants():
    """官方 DiT 的帧/token 换算常量（延迟导入，避免 comfy.ldm.minimax 不可用时拖垮模块导入）。"""
    try:
        from comfy.ldm.minimax.model import FRAME_PER_TOKEN, FRAME_RESCALE
        return tuple(FRAME_PER_TOKEN), float(FRAME_RESCALE)
    except Exception:
        return (1, 4, 4, 4, 4), 5.0 / 3.0


def _seam_frames_for_tokens(tokens):
    """latent token 数 → 覆盖的像素帧数（= 官方 _video_t_spans 的整数版）。"""
    fpt, _ = _seam_span_constants()
    return sum(fpt[k % len(fpt)] for k in range(max(0, int(tokens))))


def _seam_tokens_for_frames(frames):
    """像素帧数 → token 数；只有帧数正好是 17m+5 时才有精确解，否则返回 None。"""
    fpt, _ = _seam_span_constants()
    want = int(frames)
    k, acc = 0, 0
    while acc < want and k < 8192:
        acc += fpt[k % len(fpt)]
        k += 1
    return k if acc == want else None


def _seam_resolve_window(value):
    """把配置里的窗口值对齐到最近的合法窗口（17m+5，取自 SEAM_WINDOW_CHOICES）。"""
    try:
        want = int(float(value))
    except Exception:
        want = SEAM_DEFAULT_WINDOW
    return int(min(SEAM_WINDOW_CHOICES, key=lambda g: (abs(g - want), -g)))


def _seam_settle_cfg(value):
    """过渡丢弃帧数解析：**缺失/None/非法 → 默认 12**；只有显式 0 才是 0。

    坑：写成 `int(value or 0)` 时 `settle: null`（前端某些历史配置会存 null）会被算成 **0**
    → 不丢启动过渡 → 接缝处又出现 hold→pop「卡一下」。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return SEAM_DEFAULT_SETTLE
    try:
        return max(0, min(56, int(float(value))))
    except Exception:
        return SEAM_DEFAULT_SETTLE


def _seam_flag(value, default=True):
    """布尔配置解析：**缺失/None → default**；只有显式 false 才为假。

    坑：`bool(None)` = False，会把「没设置」当成「用户关了」（如 audio_link/音频同步裁剪 →
    静默变成音画错位/段尾冻结）。
    """
    if value is None:
        return bool(default)
    return bool(value)


def _seam_plan(latent, want_window):
    """按 latent 实际形状算衔接方案。

    返回 {window, trim_tokens, trim_audio, frames_total, adjusted}；不可衔接时返回 None。
    window 必须满足：是 17m+5、能完整落在 token 边界、且 < 段长；否则自动降到最大可用窗口。
    """
    if latent is None:
        return None
    tensors = latent.tensors if getattr(latent, "is_nested", False) else [latent]
    if not tensors or not hasattr(tensors[0], "shape") or len(tensors[0].shape) < 3:
        return None
    tokens_total = int(tensors[0].shape[2])
    frames_total = _seam_frames_for_tokens(tokens_total)
    _fpt, rescale = _seam_span_constants()
    usable = []
    for w in SEAM_WINDOW_CHOICES:
        t = _seam_tokens_for_frames(w)
        if not t or t >= tokens_total:
            continue
        if (frames_total - w) % 17 != 0:   # 兜底：非 17k+5 段长时拒绝衔接（正常路径恒成立）
            continue
        usable.append((w, t))
    if not usable:
        return None
    want = _seam_resolve_window(want_window)
    window, trim_tokens = min(usable, key=lambda it: (abs(it[0] - want), -it[0]))
    return {
        "window": int(window),
        "trim_tokens": int(trim_tokens),
        "trim_audio": int(round(window * rescale)),
        "frames_total": int(frames_total),
        "adjusted": int(window) != int(want),
    }


def _seam_latent_drop(window, settle_frames):
    """「latent 域裁切」的**相位安全**丢弃量（不可精确裁时返回 None → 调用方回退像素域）。

    为什么有约束（官方 VAE 源码事实）：视频 VAE 解码按「5 token = 17 帧」分组，要求 z 的索引 0
    落在分组边界上（`(z_len + token_drop) % 5 == 0`）。因此从 latent 域裁切时：
      · 丢弃的 token 数必须是 5 的倍数 ⇔ **丢弃帧数（窗口 W + 过渡 S）必须是 17 的倍数**；
      · 因 W = 17m+5 ≡ 5 (mod 17)，所以只有 S = 12 + 17k（12 / 29 / 46）能成立；
        好在 S=12（默认值）对全部窗口档（5 / 22 / 39 / 56）都成立。
    不满足时若强行按「窗口 token 数」裁（如 S=0 时裁 7 token）→ 剩 40 token，(40+3)%5=3
    → 分组相位错位 → 每 17 帧的拼装/混合位置整体偏移 = 周期性重影/闪烁（旧「latent域裁切」档的病根）。

    返回 {frames, tokens, audio_steps}；None = 该窗口+过渡组合无法在 latent 域精确裁切。
    """
    total = int(window) + max(0, int(settle_frames or 0))
    if total <= 0 or total % 17 != 0:
        return None
    tokens = 5 * (total // 17)
    pin_tokens = _seam_tokens_for_frames(int(window)) or 0
    if tokens < pin_tokens:   # 必须能把钉住的窗口整段丢干净（否则残留重复内容）
        return None
    _fpt, rescale = _seam_span_constants()
    return {"frames": int(total), "tokens": int(tokens), "audio_steps": int(round(total * rescale))}


def _seam_snap_settle(window, settle):
    """把「过渡丢弃帧数」对齐到合法值，使 `窗口 + 过渡` 成为 **17 的倍数**。

    为什么必须对齐（「设定时长 = 落盘时长」的数学前提）：
      · 生成帧数恒为 17K+5、落盘目标帧数恒为 17k+5（都由 align_frame_count 对齐）
      · 落盘 = 生成 − 丢弃 ⇒ 要精确等于 17k+5，必须 **丢弃 ≡ 0 (mod 17)**
      · 丢弃 = 窗口(17m+5) + 过渡 ⇒ **过渡 ≡ 12 (mod 17)**，即 12 / 29 / 46（默认 12 对全部窗口档都成立）
    另一好处：这组值同时满足「latent 域裁切」要求的「丢弃 token 数是 5 的倍数」✓（两套约束天然一致）。
    对齐后的值 ≥ 0 且保证 `丢弃 ≥ 窗口`（必须把钉住的窗口整段丢掉）。
    """
    w = int(window)
    try:
        s = int(float(settle))
    except Exception:
        s = SEAM_DEFAULT_SETTLE
    s = max(0, int(s))
    snapped = ((w + s + 8) // 17) * 17          # 就近取整到 17 的倍数
    if snapped < w:                              # 至少要盖住钉住的窗口
        snapped = ((w + 16) // 17) * 17
    return int(max(0, snapped - w))


def _seam_runway_seconds(window, settle, enabled=True):
    """段首衔接跑道（秒）= (窗口 + 过渡) / 帧率。

    无限时长节点里，段 2+ 的**生成时长 = 设定时长 + 跑道**：段首这一段是「承接上一段末帧」的
    锁定内容 + 模型启动过渡，成片会整段裁掉 → 落盘时长精确 = 用户设定时长。
    """
    if not enabled:
        return 0.0
    return (int(window) + int(settle)) / float(VIDEO_FPS or 24)


# ♾️ 接缝亮度匹配（只做亮度）
# 依据官方 Director（ComfyUI_MiniMaxH3_Director/director/segment_continuity.py）**实测保留下来**的那一种做法：
#   · 乘法增益（gain）→ 局部对比被压 → 「画面花」；官方已关（CONTINUITY_SEAM_SOFTEN_* = 0）
#   · 开口处做长 RGB 混合 / 亮度渐变 → post-seam brightness pump「一闪一闪」；官方已关（OPENING_LUMA_BLEND = 0）
#   · 低频谱亮度空间场（opening Y-map）也被官方关掉（权重 0）
#   · 只剩「**全局加性亮度渐入**」：每帧加同一个 delta(R=G=B)、单帧限幅、逐帧从「上段末帧亮度」过渡到
#     「本段自身亮度」 → 保留局部对比、不复制轮廓（不重影）。官方常量：12 帧 / 上限 0.10 / 触发阈值 0.008。
SEAM_LUMA_FRAMES = 12        # 亮度过渡帧数（官方 CONTINUITY_SEAM_ADD_LUMA_FRAMES）
SEAM_LUMA_MAX_DELTA = 0.10   # 单帧最大加减亮度（0..1 单位；官方 CONTINUITY_SEAM_ADD_LUMA_MAX）
SEAM_LUMA_EPS = 0.008        # 触发阈值（官方 CONTINUITY_OPENING_EXPOSURE_SPIKE）：三项差都小于它就不动画面


def _seam_frame_luma(frame):
    """一帧的 Rec.601 平均亮度（0..1）——与官方 `_additive_opening_luma` 同一套系数。"""
    f = frame.float()
    return float((f[..., 0] * 0.299 + f[..., 1] * 0.587 + f[..., 2] * 0.114).mean().item())


def _seam_luma_ramp(image, prev_luma, frames=SEAM_LUMA_FRAMES, max_delta=SEAM_LUMA_MAX_DELTA, inplace=True):
    """接缝亮度匹配：把本段开头若干帧的平均亮度朝「上一段末帧亮度」平滑过渡（**只动亮度**）。

    为什么需要（我们此前完全空白的维度）：段首衔接帧被裁掉后，本段第一个画面是模型新生成的内容，
    其整体曝光/环境光与上一段末帧可能差一截（尤其灯光/昼夜/室内外切换的段边界）→ 接缝处亮度一跳。

    做法（严格对齐官方保留实现）：
      · y_start = 上段末帧亮度；y_end = 本段第 n 帧自身亮度（n = min(12, 段长−1)）
      · 第 i 帧 target = y_start*(1−t) + y_end*t（t = i/n）→ delta = clamp(target − 当前帧亮度, ±max_delta)
      · 三通道加同一个 delta（加性，不做乘法增益）→ 保留局部对比，clamp 到 0..1
      · 三项差（接缝差 / 开头两帧跳变 / 首尾差）都 < SEAM_LUMA_EPS 时**完全不动作**（画面零改动）

    默认 inplace（不 clone）：一段 124 帧 × 768×1344×3 的 float32 约 1.5GB，二采放大后更大，
    clone 会让单段显存峰值翻倍；这里只对每帧做一次「当帧临时张量 + 原地赋值」，峰值 = 1 帧。
    返回 (image, applied, info)。
    """
    if image is None or not hasattr(image, "shape") or len(getattr(image, "shape", ())) != 4:
        return image, False, {}
    if prev_luma is None or float(max_delta) <= 0:
        return image, False, {}
    total = int(image.shape[0])
    if total < 3:
        return image, False, {}
    n = min(int(frames), total - 1)
    if n < 2:
        return image, False, {}

    y_start = float(prev_luma)
    y0 = _seam_frame_luma(image[0])
    y1 = _seam_frame_luma(image[1])
    y2 = _seam_frame_luma(image[2])
    y_end = _seam_frame_luma(image[n])
    info = {"frames": int(n), "y_start": y_start, "y0": y0, "y_end": y_end,
            "seam_gap": abs(y0 - y_start), "spike": abs(y1 - y0) + abs(y2 - y1),
            "max_delta": float(max_delta)}
    if (info["seam_gap"] < SEAM_LUMA_EPS and info["spike"] < SEAM_LUMA_EPS
            and abs(y_end - y_start) < SEAM_LUMA_EPS):
        info["applied"] = False
        return image, False, info

    out = image if inplace else image.clone()
    dtype = out.dtype
    with torch.no_grad():
        for i in range(n):
            t = float(i) / float(n)
            target = y_start * (1.0 - t) + y_end * t
            cur = _seam_frame_luma(out[i])
            delta = max(-float(max_delta), min(float(max_delta), target - cur))
            if abs(delta) < 1e-5:
                continue
            out[i] = (out[i].float() + delta).clamp(0.0, 1.0).to(dtype=dtype)
    info["applied"] = True
    return out, True, info


def _seam_guide_from_pixels(prev_latent, plan, vae):
    """官方 AddGuide 同款引导路径：上段尾部 latent 切片 → 解码成像素帧 → vae.encode 重新编码。

    用途：A/B 诊断「采样 latent 直传」是否与 DiT 条件行分布不匹配（默认不用，仅面板可选）。
    相位安全：只解码尾部 5m+2 个 token，起点 = 5(k−m) 必为 5 的倍数 → 落在 VAE 解码分组边界上
    （实测 7 token → 恰好 22 帧）；编码回 22 帧得到同样 5m+2 token 的引导 latent，与本段布局一致。
    """
    tensors = prev_latent.tensors if getattr(prev_latent, "is_nested", False) else [prev_latent]
    video = tensors[0]
    t0 = int(video.shape[2]) - int(plan["trim_tokens"])
    if t0 < 0:
        return None
    tail = video[:, :, t0:].contiguous()
    if hasattr(tail, "device") and tail.device.type != "cpu":
        tail = tail.cpu()
    try:
        frames = vae.decode(tail)
        if frames is not None and getattr(frames, "ndim", 0) == 5:
            frames = frames[0]
        return vae.encode(frames)
    except Exception as e:
        raise RuntimeError(f"引导来源「上段尾帧→VAE重编码」失败：{e}")


def _seam_apply_guide(positive, prev_latent, plan, audio_link=True, vae=None, guide_source="latent"):
    """把上一段一采 latent 的尾部（+可选音频尾部）作为 keyframe 钉进 positive。

    keyframe 结构 = 官方 MiniMaxH3AddGuide 写入的同一结构（resolved_frame_index + latent + audio_latent），
    以 conditioning dict 的形式并入 minimax_keyframes，与 minimax_refs 并存（Max/官方链路已验证可行）。
    返回 (新的 positive, keyframe 或 None)。
    """
    if prev_latent is None or plan is None:
        return positive, None
    tensors = prev_latent.tensors if getattr(prev_latent, "is_nested", False) else [prev_latent]
    video = tensors[0]
    t0 = int(video.shape[2]) - int(plan["trim_tokens"])
    if t0 < 0:
        return positive, None
    guide_z = None
    if guide_source == "pixel" and vae is not None:
        guide_z = _seam_guide_from_pixels(prev_latent, plan, vae)
    if guide_z is None:
        guide_z = video[:, :, t0:].contiguous()
    keyframe = {"resolved_frame_index": 0, "latent": guide_z}
    if audio_link and len(tensors) > 1 and int(plan["trim_audio"]) > 0:
        audio = tensors[1]
        a0 = int(audio.shape[-1]) - int(plan["trim_audio"])
        if a0 >= 1:
            keyframe["audio_latent"] = audio[..., a0:].contiguous()
    keyframes = list((positive[0][1] or {}).get("minimax_keyframes") or [])
    keyframes.append(keyframe)
    return node_helpers.conditioning_set_values(positive, {"minimax_keyframes": keyframes}), keyframe


def _seam_keep_tail(latent, window, audio_window=None):
    """只保留 latent 尾部「配置窗口」所需的切片（转 CPU），供下一段做**同画布**二采引导。

    为什么切片：二采画布被放大后整份 latent 在 CPU 常驻可能几十~几百 MB，而引导只需要
    「配置窗口」对应的 token（W=17m+5 → 5m+2 个 token）。切片起点 = T−(5m+2)，在原始 latent 里
    是 5 的倍数 → 内部 token 几何/相位都不变；即使下一段窗口按段长降级，`_seam_apply_guide`
    再从切片尾部取所需 token，取出的起点仍是 5 的倍数（差值为 5(m−m')）。
    音频同理只留 round(W×5/3) 步尾部。返回 NestedTensor（CPU）或 None。
    """
    if latent is None:
        return None
    tensors = latent.tensors if getattr(latent, "is_nested", False) else [latent]
    if not tensors:
        return None
    _fpt, rescale = _seam_span_constants()
    out = []
    tk = _seam_tokens_for_frames(int(window)) or 0
    v = tensors[0]
    if tk > 0 and int(v.shape[2]) > tk:
        v = v[:, :, int(v.shape[2]) - tk:]
    out.append(v.detach().contiguous().to("cpu"))
    if len(tensors) > 1:
        a = tensors[1]
        a_steps = int(round(int(audio_window if audio_window is not None else window) * rescale))
        if a_steps > 0 and int(a.shape[-1]) > a_steps:
            a = a[..., int(a.shape[-1]) - a_steps:]
        out.append(a.detach().contiguous().to("cpu"))
    return comfy.nested_tensor.NestedTensor(out)


def _seam_trim_head(samples, plan, trim_audio=True, drop_tokens=None, drop_audio=None):
    """在 latent 域裁掉段首被钉住的窗口（+过渡）对应的时间跨度。

    ⚠️ 只能裁**相位安全**的量：丢弃 token 数必须是 5 的倍数（= 丢弃帧数是 17 的倍数），
    否则剩余 latent 的索引 0 落在 VAE 解码「5 token = 17 帧」分组内部 → 周期性重影/闪烁。
    安全量由 `_seam_latent_drop(窗口, 过渡)` 给出；不给 drop_tokens 时退回「只裁窗口 token」
    （= 旧行为，S=0 时必闪，仅作 A/B 对照，由调用方保证不再默认走到）。
    """
    if plan is None:
        return samples
    tensors = samples.tensors if getattr(samples, "is_nested", False) else [samples]
    video = tensors[0]
    tr = int(plan["trim_tokens"]) if drop_tokens is None else int(drop_tokens)
    tr = max(0, tr)
    if int(video.shape[2]) - tr <= 0:
        raise RuntimeError(f"衔接裁剪后无剩余帧（latent token {int(video.shape[2])} − 裁剪 {tr}）")
    out = [video[:, :, tr:].contiguous()]
    if len(tensors) > 1:
        audio = tensors[1]
        a = int(plan["trim_audio"]) if trim_audio else 0
        if trim_audio and drop_audio is not None:
            a = max(0, int(drop_audio))
        if 0 < a < int(audio.shape[-1]):
            out.append(audio[..., a:].contiguous())
        else:
            out.append(audio)
    return comfy.nested_tensor.NestedTensor(out)


def _seam_trim_audio_frames(audio, drop_frames, keep_frames, fps=VIDEO_FPS):
    """波形域按「丢弃帧数 / 保留帧数」裁切（像素域与 latent 域**共用同一套音画对齐规则**）。

    两种裁切模式只在「视频怎么裁」上不同（像素域丢解码帧 / latent 域丢 token），音频一律按
    「丢 drop 帧、保留 keep 帧」换算时间轴 → 保证音画严格等长、模式之间结果一致。
    """
    if audio is None or not isinstance(audio, dict):
        return audio
    wf = audio.get("waveform")
    if wf is None:
        return audio
    fps = float(fps or VIDEO_FPS)
    sr = int(audio.get("sample_rate", 32000))
    total = int(wf.shape[-1])
    start = int(round(max(0, int(drop_frames)) / fps * sr))
    end = start + int(round(max(0, int(keep_frames)) / fps * sr))
    start = max(0, min(start, total - 1))
    end = max(start + 1, min(end, total))
    audio = dict(audio)
    audio["waveform"] = wf[..., start:end].contiguous()
    return audio


def _seam_trim_decoded(image, audio, plan, fps=VIDEO_FPS, settle_frames=0, trim_audio=True, tag="无限时长"):
    """【默认路径】像素/波形域裁切：latent 完全不裁，解码后再丢掉段首 window 帧 + 过渡帧。

    为什么必须像素域裁（官方源码事实）：MiniMax H3 视频 VAE 解码按时序分块——tokens_chunk_size=5，
    每块取 5+token_overlap(2)=7 个 token 解码 28 帧，剥掉块首 frame_pre_padding=3 帧、块间用
    frame_overlap=5 帧混合。这套规则**要求 z 的索引 0 落在「5 token = 17 帧」的分组边界上**。
    从 latent 域裁掉段首窗口会让切片起点落在分组内部（实测 40 token 时 (40+3)%5=3≠0）→
    每 17 帧的拼装/混合位置整体偏移 → 周期性重影/抖动（闪烁）。

    settle_frames（过渡丢弃）：被钉住的窗口帧会“压住”段首，模型先模仿/保持引导内容、之后才启动
    新动作，这段启动过渡留在成片里就是接缝处的“卡一下”（官方注释称之为 hold→pop）。官方
    ComfyUI_MiniMaxH3_Director 的做法是「锁住帧 + 再丢 settling 帧（默认 12）后再导出」，此处同款：
    共丢 window + settle 帧，音频按“视频实际保留帧数”裁波形 → 音画严格等长。
    像素域裁切的另一个好处：丢弃量可以是任意帧数，不受 17k+5 网格限制。

    trim_audio=False（面板「音频同步裁剪」关）：只用于对比实验 —— 画面裁了、音频不裁 → 音画错位
    drop/fps 秒，必须显式打印警告（此前该开关在默认的像素域路径下被完全忽略，属 bug）。
    实际丢弃量 drop 会按“至少保留 1 帧”钳制（段长不足以丢满 window+settle 时不裁空、且音画同源同量）。
    """
    if plan is None:
        return image, audio
    w = int(plan["window"]) + max(0, int(settle_frames or 0))
    fps = float(fps or VIDEO_FPS)
    drop = w
    if image is not None:
        frames = int(image.shape[0])
        if drop >= frames:  # 段长装不下丢弃量：钳制为「至少保留 1 帧」，避免落盘 0 帧空视频
            print(f"[JZL-{tag}] ⚠️ 衔接丢弃量 {drop} 帧 ≥ 本段解码帧数 {frames}：钳制为丢 {max(0, frames - 1)} 帧"
                  f"（请调小「衔接窗口 / 过渡丢弃帧数」）")
            drop = max(0, frames - 1)
        if drop > 0:
            image = image[drop:]
    if not trim_audio:
        print(f"[JZL-{tag}] ⚠️ 「音频同步裁剪」已关闭：画面丢 {drop} 帧而音频不裁 → 音画错位 "
              f"{drop / fps:.3f}s（仅作对比实验，正式生成请保持开启）")
        return image, audio
    keep = int(image.shape[0]) if image is not None else 0
    audio = _seam_trim_audio_frames(audio, drop, keep, fps)   # 与画面丢帧同源同量（不是 window+settle 原值）
    return image, audio


def _seam_video_tensor(obj):
    """从 latent 容器里取出视频张量（兼容三种形态，用于读取画布 h/w）。

    · ComfyUI latent dict：`{"samples": NestedTensor|Tensor}`（官方 LTXV 分离/放大/合并节点都用这种）
    · NestedTensor：取其 `tensors[0]`（视频流）
    · 纯 tensor：直接返回
    ⚠️ 坑：`MinimaxH3LatentUpscaler3D.execute()` 返回的是 `io.NodeOutput({"samples": out})`，
    所以
    `up.args[0]` 是 **dict** 而不是张量——直接访问 `.shape` 会抛 `'dict' object has no attribute 'shape'`
    （曾因此让引导画布对齐静默失效 → 二采 + 衔接第 2 段起采样报布局行数不匹配）。
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        obj = obj.get("samples")
    if obj is None:
        return None
    if getattr(obj, "is_nested", False):
        tensors = getattr(obj, "tensors", None) or []
        return tensors[0] if tensors else None
    return obj


def _seam_fit_guides_to_canvas(positive, target_video, tag="无限时长"):
    """把 positive 里 keyframe 引导 latent 的空间尺寸对齐到「本次采样目标画布」。

    为什么必须做（官方源码事实）：PackedLayout 组装序列时，keyframe 的视频 cond 行**按目标画布**
    计算行数（`n = vt * frame_rows`，frame_rows 来自目标 latent 的 latent_h/latent_w，见
    `_frame_grid(latent_h, latent_w)`），而 `_cond_video_rows` 是按引导 latent **自己的** h/w
    patchify 出行的（`patchify_video(z)` → vt*(h//2)*(w//2) 行）。两者不一致时
    `all_video_rows[~img_update] = cond_video_rows` 会直接形状不匹配报错。
    （refs 不受影响：它们在 PackedLayout 里按自己声明的 latent_h/latent_w 单独排行。）

    什么时候不一致：**二采（latent 空间放大）开启且 upscale_scale > 1** —— 二采画布被放大，
    而引导 latent 取自「一采画布」（上一段一采 latent 尾部）→ 第 2 段起二采必然崩。
    这里把引导 latent 空间插值到目标画布（时间轴不变；引导帧最终会被裁掉，只作条件使用）。
    尺寸一致时是零开销的空操作。target_video 可为 latent dict / NestedTensor / 纯 tensor。
    """
    try:
        if positive is None or target_video is None:
            return positive
        _tv = _seam_video_tensor(target_video)
        if _tv is None or not hasattr(_tv, "shape"):
            return positive
        th, tw = int(_tv.shape[-2]), int(_tv.shape[-1])
        meta = positive[0][1] or {}
        kfs = list(meta.get("minimax_keyframes") or [])
        if not kfs:
            return positive
        out_kfs, changed = [], False
        for kf in kfs:
            z = kf.get("latent")
            if z is None or (int(z.shape[-2]) == th and int(z.shape[-1]) == tw):
                out_kfs.append(kf)
                continue
            zf = torch.nn.functional.interpolate(
                z.to(torch.float32), size=(int(z.shape[2]), th, tw),
                mode="trilinear", align_corners=False).to(z.dtype)
            nk = dict(kf)
            nk["latent"] = zf
            out_kfs.append(nk)
            changed = True
            print(f"[JZL-{tag}] 引导 latent 画布对齐：{int(z.shape[-2])}x{int(z.shape[-1])} → {th}x{tw}"
                  f"（{int(z.shape[2])} token，时间轴不变）")
        if not changed:
            return positive
        return node_helpers.conditioning_set_values(positive, {"minimax_keyframes": out_kfs})
    except Exception as e:
        print(f"[JZL-{tag}] ⚠️ 引导 latent 画布对齐失败（按原样继续）：{e}")
        return positive


_FFMPEG_CACHE = {"probed": False, "path": None, "reason": ""}


def _find_ffmpeg_executable():
    """ffmpeg 可执行文件候选：imageio-ffmpeg 自带 → PATH → 常见安装目录。返回 (路径|None, 说明)。"""
    tried = []
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p, f"imageio-ffmpeg 自带（{p}）"
        tried.append(f"imageio-ffmpeg 返回的路径不存在（{p}）")
    except Exception as e:
        tried.append(f"未安装 imageio-ffmpeg（{type(e).__name__}）")

    p = shutil.which("ffmpeg")
    if p:
        return p, f"PATH 中的 ffmpeg（{p}）"
    tried.append("PATH 中没有 ffmpeg")

    for c in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
              r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
              os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffmpeg.exe"),
              "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"):
        if os.path.isfile(c):
            return c, f"常见安装目录（{c}）"
    tried.append("常见安装目录也没有 ffmpeg.exe")
    return None, "；".join(tried)


def _resolve_ffmpeg():
    """解析可用的 ffmpeg 可执行文件（进程内缓存一次）。

    ⚠️ 用户机常见故障：ComfyUI 官方依赖只装 PyAV(av)、不装 imageio-ffmpeg，
    而 ffmpeg 又不在 PATH → 旧代码回退成字符串 "ffmpeg" 交给子进程，直接
    「[WinError 2] 系统找不到指定的文件」，逐段落盘全失败（表现为「本段未落盘任何视频」）。
    因此这里找不到就明确返回 None，由调用方改用 PyAV 落盘。
    """
    if _FFMPEG_CACHE["probed"]:
        return _FFMPEG_CACHE["path"]
    _FFMPEG_CACHE["probed"] = True
    path, reason = _find_ffmpeg_executable()
    _FFMPEG_CACHE["path"] = path
    _FFMPEG_CACHE["reason"] = reason
    return path


def _ffmpeg_reason():
    """ffmpeg 解析结论（诊断/报错文案用）。"""
    _resolve_ffmpeg()
    return _FFMPEG_CACHE["reason"]


def _ffmpeg_short_desc():
    """落盘编码器的简短来源描述（运行横幅用，一行说清用的是什么）。"""
    _resolve_ffmpeg()
    if _FFMPEG_CACHE["path"]:
        r = _FFMPEG_CACHE["reason"] or ""
        if r.startswith("imageio-ffmpeg"):
            src = "imageio-ffmpeg 自带"
        elif r.startswith("PATH"):
            src = "系统 PATH"
        else:
            src = "常见安装目录"
        return f"ffmpeg（{src}）"
    av = _pyav_module()
    if av is not None:
        return f"PyAV {getattr(av, '__version__', '?')}（未找到 ffmpeg 可执行文件，已回退）"
    return "无（ffmpeg 与 PyAV 都不可用）"


def _is_ffmpeg_missing_error(msg):
    """判断异常文本是否为「ffmpeg 可执行文件不存在」（Windows 中文报错也要认）。"""
    s = (msg or "").lower()
    return any(k in s for k in (
        "winerror 2", "winerror 3", "系统找不到指定", "系统找不到文件",
        "no such file", "not found", "errno 2", "cannot find",
        "不是内部或外部命令", "is not recognized",
    ))


def _pyav_module():
    try:
        import av
        return av
    except Exception:
        return None


def _pyav_reason():
    """PyAV 可用性说明（诊断/报错文案用）。"""
    av = _pyav_module()
    if av is None:
        return "PyAV(av) 未安装"
    return f"PyAV(av) {getattr(av, '__version__', '?')} 可用"


def _save_segment_mp4_pyav(img, wf_fit, wf_sr, path, fps=VIDEO_FPS, keep_wav=None):
    """PyAV(av) 落盘回退：ffmpeg 可执行文件缺失/不可用时用官方依赖 av 直接编码 mp4（H.264+AAC）。

    img: uint8 numpy [T,H,W,3]；wf_fit: 已对齐画面的 float tensor [C,L] 或 None。
    ComfyUI 官方 requirements 必装 av，且自带 libx264/aac，因此这是「没装 ffmpeg」用户的可靠兜底。
    """
    av = _pyav_module()
    if av is None:
        raise RuntimeError(
            "落盘失败：既没有 ffmpeg 可执行文件，PyAV(av) 也不可用。"
            "请在本环境执行 pip install imageio-ffmpeg（或安装 ffmpeg 并加入 PATH）")
    import numpy as np

    T, H, W = int(img.shape[0]), int(img.shape[1]), int(img.shape[2])
    fps = int(fps or VIDEO_FPS)
    if wf_fit is not None and keep_wav:
        try:
            _write_wav(wf_fit, wf_sr, keep_wav)
        except Exception as e:
            print(f"[JZL-管理器] ⚠️ 无损 WAV 写盘失败（不影响 mp4 落盘）：{e}")
    try:
        with av.open(path, mode="w", format="mp4") as container:
            vstream = container.add_stream("libx264", rate=fps)
            vstream.width, vstream.height = W, H
            vstream.pix_fmt = "yuv420p"
            vstream.options = {"crf": "18", "preset": "medium"}
            astream = container.add_stream("aac", rate=int(wf_sr)) if wf_fit is not None else None

            for i in range(T):
                frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(img[i]), format="rgb24")
                frame.pts = i
                for packet in vstream.encode(frame):
                    container.mux(packet)
            for packet in vstream.encode(None):
                container.mux(packet)

            if astream is not None:
                arr = wf_fit.detach().cpu().float().numpy()
                if arr.ndim == 3:      # [B, C, L] → [C, L]（PyAV 音频帧只接受 2 维）
                    arr = arr[0]
                if arr.ndim == 1:      # [L] → [1, L]
                    arr = arr[None, :]
                layout = "stereo" if int(arr.shape[0]) >= 2 else "mono"
                aframe = av.AudioFrame.from_ndarray(np.ascontiguousarray(arr), format="fltp", layout=layout)
                aframe.sample_rate = int(wf_sr)
                aframe.pts = 0
                for packet in astream.encode(aframe):
                    container.mux(packet)
                for packet in astream.encode(None):
                    container.mux(packet)
    except Exception as e:
        raise RuntimeError(f"PyAV 落盘失败（{_pyav_reason()}）：{type(e).__name__}: {e}")
    return path


def _write_wav(waveform, sample_rate, path):
    """torch 音频 [B,C,L] → 16bit PCM wav 文件（作为 ffmpeg 音频输入）。"""
    w = waveform.detach().cpu()
    if w.ndim == 3:
        w = w[0]  # [C,L]
    if w.ndim != 2:
        raise ValueError(f"不支持的音频维度：{tuple(w.shape)}")
    data = (w.float().clamp(-1.0, 1.0) * 32767.0).to(torch.int16)
    frames = data.transpose(0, 1).contiguous().numpy()  # [L,C] 交错
    with wave.open(path, "wb") as f:
        f.setnchannels(int(data.shape[0]))
        f.setsampwidth(2)
        f.setframerate(int(sample_rate))
        f.writeframes(frames.tobytes())


def _safe_story_name(name):
    """故事名 → 安全文件名片段（去除路径非法字符，空则用 story）。"""
    s = re.sub(r'[\\/:*?"<>|]', "_", str(name or "")).strip()
    return s or "story"


def _next_counter(folder, prefix):
    """扫描 folder 下 prefix 开头的 .mp4，返回下一个 5 位编号（max+1，至少 1）。"""
    if not os.path.isdir(folder):
        return 1
    mx = 0
    try:
        for fn in os.listdir(folder):
            if fn.startswith(prefix) and fn.endswith(".mp4"):
                m = re.search(r"_(\d{5})\.mp4$", fn)
                if m:
                    mx = max(mx, int(m.group(1)))
    except Exception:
        pass
    return mx + 1


def _next_batch(folder, story):
    """扫描 folder 下已存在的「{故事名}_{N}次生成分段/分段合并」文件名，返回本次生成次数 N
    （已有最大 N + 1，至少 1）。同一故事每次完整生成批次号 +1，用于文件名区分多次结果。"""
    if not os.path.isdir(folder):
        return 1
    mx = 0
    pat = re.compile(rf"^{re.escape(str(story))}_(\d+)次生成分段(?:合并)?")
    try:
        for fn in os.listdir(folder):
            m = pat.match(fn)
            if m:
                mx = max(mx, int(m.group(1)))
    except Exception:
        pass
    return mx + 1


def _fit_audio_to_frames(waveform, sample_rate, frames, fps=VIDEO_FPS):
    """把音频波形对齐到「正好 frames 帧」的长度（长了裁掉尾巴，短了补静音）。

    ⚠️ 为什么必须在落盘前做（实测踩坑）：mp4 容器时长 = max(视频时长, 音频时长)。
    只要音频比画面长（例如把「音频同步裁剪」关掉、或音频 latent 的 40Hz 网格取整误差），
    容器就会比画面长 → 播放/拼接时**最后一帧被冻结**（每段结尾"卡一下"，拼接后每个接缝都多出这段冻结时间）。
    实测案例：4 段 5 秒视频，画面 124/102/102/102 帧（17.9s），音频 5.18s×4 = 20.7s，
    合并成片容器 20.7s 而实际只有 430 帧 → 成片里凭空多出约 1.87s 的冻结画面。
    这里统一按画面帧数对齐 → 从根上消除该现象。返回 (waveform, 调整秒数)。
    """
    if waveform is None:
        return waveform, 0.0
    sr = int(sample_rate or 32000)
    fps = float(fps or VIDEO_FPS)
    want = int(round(max(0, int(frames)) / fps * sr))
    if want <= 0:
        return waveform, 0.0
    have = int(waveform.shape[-1])
    if have == want:
        return waveform, 0.0
    if have > want:
        out = waveform[..., :want].contiguous()
    else:
        pad = torch.zeros(waveform.shape[:-1] + (want - have,), dtype=waveform.dtype, device=waveform.device)
        out = torch.cat([waveform, pad], dim=-1)
    return out, (have - want) / float(sr)


def _save_segment_mp4(image, audio, out_dir, index, story_name="story", counter=1, fps=VIDEO_FPS, batch=None,
                      keep_wav=None):
    """把单段 (IMAGE [T,H,W,C] float 0-1, AUDIO dict|None) 用 ffmpeg 落盘 mp4（临时 raw 文件方案）。

    文件名规则：{故事名}_{N}次生成分段{序号}_{5位编号}.mp4（batch=N 时）；batch=None 保持旧名
    {故事名}_分段{序号}_{5位编号}.mp4（兼容 Pro 等旧调用）。编号按目录已有文件递增。
    返回保存的文件绝对路径；失败抛异常（由调用方捕获并记录日志）。
    - 帧数据先整体写入临时 rawvideo 文件，再让 ffmpeg 读取——不经过 stdin 管道，
      彻底规避 ffmpeg 提前退出导致的「flush of closed file」竞态（Linux 下 stdin 行为与 Windows 不同）；
    - libx264 不可用（精简版 ffmpeg）时自动回退 mpeg4；
    - ffmpeg 出错时给出 stderr 真实原因。
    - keep_wav：把本段音频的无损 WAV 写到该路径并**不删除**（供拼接时“音频统一编码一次”，
      避免每段独立 AAC 编码 + -c copy 拼接带来的段边界 encoder delay/priming 误差）。
    """
    if image is None:
        raise ValueError("图像为空，无法落盘")
    img = image.detach().cpu().float().clamp(0.0, 1.0).mul(255.0).to(torch.uint8).numpy()
    T, H, W, C = img.shape
    if C >= 4:
        img = img[:, :, :, :3]  # 只取 RGB（ffmpeg rgb24 需 3 通道）
    os.makedirs(out_dir, exist_ok=True)
    _seg = f"{int(batch)}次生成分段" if batch else "分段"
    path = os.path.join(out_dir, f"{story_name}_{_seg}{int(index)}_{int(counter):05d}.mp4")
    ff = _resolve_ffmpeg()

    # ★ 音频强制对齐画面帧数（容器时长 = max(视频,音频)：音频偏长 → 段尾画面被冻结，拼接后表现为“结尾卡顿”）
    wf_fit, wf_sr = None, 32000
    if audio is not None:
        _w = audio.get("waveform")
        if _w is not None:
            wf_sr = int(audio.get("sample_rate", 32000))
            wf_fit, _adj = _fit_audio_to_frames(_w, wf_sr, T, fps)
            if abs(_adj) >= (1.0 / float(fps or VIDEO_FPS)):
                print(f"[JZL-管理器] 分段 {int(index)}：音频比画面{'长' if _adj > 0 else '短'} "
                      f"{abs(_adj):.3f}s → 已按 {int(T)} 帧（{int(T) / float(fps or VIDEO_FPS):.3f}s）对齐"
                      f"（否则容器被音频拉长 → 段尾画面冻结，拼接后每段结尾“卡一下”）")

    # 没有 ffmpeg 可执行文件 → 直接用 PyAV（ComfyUI 官方依赖 av）落盘，不依赖外部 ffmpeg
    if ff is None:
        _log_once(f"[JZL-管理器] 未找到 ffmpeg 可执行文件（{_ffmpeg_reason()}）→ 自动改用 {_pyav_reason()} 落盘")
        return _save_segment_mp4_pyav(img, wf_fit, wf_sr, path, fps, keep_wav=keep_wav)

    # 临时 rawvideo 文件（视频帧整体写入，避免 stdin 管道竞态）
    raw_path = os.path.join(out_dir, f"._jzl_raw_{int(index)}_{os.getpid()}.raw")
    try:
        with open(raw_path, "wb") as _rf:
            _rf.write(img.tobytes())

        def _run(vcodec, quality_args):
            cmd = [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{W}x{H}", "-r", str(fps), "-i", raw_path]
            tmp_wav = None
            try:
                if wf_fit is not None:
                    tmp_wav = (keep_wav if keep_wav
                               else os.path.join(tempfile.gettempdir(), f"_jzl_seg{int(index)}_{os.getpid()}.wav"))
                    _write_wav(wf_fit, wf_sr, tmp_wav)
                    cmd += ["-i", tmp_wav, "-c:a", "aac", "-b:a", "192k"]
                cmd += ["-c:v", vcodec, "-pix_fmt", "yuv420p"] + quality_args + ["-movflags", "+faststart"]
                # 注意：不要加 -shortest —— 实测它会因为音频输入先 EOF 而**切掉最后一帧视频**
                # （124 帧段变成 123 帧）；音频已由 _fit_audio_to_frames 对齐到画面帧数，
                # 残余的 AAC 尾部填充仅 ~13ms（<1 帧），远小于画面冻结的观感阈值。
                cmd += [path]
                try:
                    _p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"ffmpeg 落盘超时（{vcodec}）：{path}")
                if _p.returncode != 0:
                    _err = (_p.stderr or b"").decode("utf-8", "ignore")[-600:]
                    raise RuntimeError(f"ffmpeg 落盘失败（{vcodec}）：{_err.strip() or '未知错误'}")
                return path
            finally:
                # keep_wav 交给调用方（拼接后统一编码）负责清理
                if tmp_wav and not keep_wav and os.path.exists(tmp_wav):
                    try:
                        os.remove(tmp_wav)
                    except Exception:
                        pass

        try:
            return _run("libx264", ["-crf", "18"])
        except Exception as e:
            msg = str(e)
            # libx264 编码器缺失/不支持 → 回退 mpeg4（兼容性最广，mp4 容器仍可带 aac 音频）
            if "libx264" in msg or "encoder" in msg.lower():
                print(f"[JZL-管理器] libx264 不可用，回退 mpeg4 编码落盘：{msg}")
                try:
                    return _run("mpeg4", ["-q:v", "5"])
                except Exception as e2:
                    print(f"[JZL-管理器] ⚠️ ffmpeg 两种编码器都失败，改用 {_pyav_reason()} 落盘：{e2}")
                    return _save_segment_mp4_pyav(img, wf_fit, wf_sr, path, fps, keep_wav=keep_wav)
            # ffmpeg 可执行文件不存在（Windows 中文报错是「系统找不到指定的文件」/WinError 2）
            if _is_ffmpeg_missing_error(msg):
                print(f"[JZL-管理器] ⚠️ ffmpeg 不可用（{msg.splitlines()[0][:140] if msg else '未知原因'}）"
                      f"，改用 {_pyav_reason()} 落盘")
                return _save_segment_mp4_pyav(img, wf_fit, wf_sr, path, fps, keep_wav=keep_wav)
            raise
    finally:
        try:
            if os.path.exists(raw_path):
                os.remove(raw_path)
        except Exception:
            pass


def _concat_escape(p):
    """ffmpeg concat demuxer 的 Windows 路径写法：反斜杠→正斜杠（盘符冒号不转义，ffmpeg 对单字母盘符有特殊处理）。"""
    return p.replace("\\", "/")


def _concat_wavs(wav_paths, out_wav):
    """把多段无损 WAV（同采样率/声道/位深）按顺序拼成一个 WAV（PCM 直接续写，不重采样）。

    用途：拼接保存时「音频统一编码一次」——避免每段独立 AAC 编码 + concat -c copy 带来的
    段边界 encoder delay/priming 误差（约 20–30ms/接缝，长链会听出轻微音频不连续）。
    """
    valid = [p for p in (wav_paths or []) if p and os.path.exists(p)]
    if not valid:
        return None
    with wave.open(valid[0], "rb") as f0:
        nch, sw, sr = f0.getnchannels(), f0.getsampwidth(), f0.getframerate()
    with wave.open(out_wav, "wb") as out:
        out.setnchannels(nch)
        out.setsampwidth(sw)
        out.setframerate(sr)
        for p in valid:
            with wave.open(p, "rb") as f:
                out.writeframes(f.readframes(f.getnframes()))
    return out_wav


def _merge_mp4_concat(paths, out_path, audio_wavs=None):
    """把多个 mp4 按顺序用 concat demuxer 拼接，返回输出路径。

    audio_wavs 给出「各段无损 WAV」时走**音频统一编码**路径：视频仍 `-c copy` 无损拼，
    音频由这些 WAV 拼成整条音轨后**只编码一次 AAC** 再复用到合并文件 → 段边界不再有
    AAC encoder delay/priming 误差（各分段自己的 mp4 仍带独立 AAC，分段保存照旧可播）。
    段数不齐或任何异常都自动回退原来的「视频+音频都 -c copy」路径。
    """
    if not paths:
        raise ValueError("没有可合并的分段文件")
    ff = _resolve_ffmpeg()
    if ff is None:
        raise RuntimeError(
            f"未找到 ffmpeg 可执行文件，无法合并分段（{_ffmpeg_reason()}）。"
            f"各分段视频已保留、可直接使用；要启用自动合并请在本环境执行 pip install imageio-ffmpeg")
    out_path = os.path.abspath(out_path)
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    _tag = f"{os.getpid()}_{int(time.time())}"
    list_path = os.path.join(out_dir, f"_jzl_concat_{_tag}.txt")
    wav_all = os.path.join(out_dir, f"_jzl_concat_audio_{_tag}.wav")
    try:
        with open(list_path, "w", encoding="utf-8") as f:
            for p in paths:
                f.write(f"file '{_concat_escape(os.path.abspath(p))}'\n")

        if audio_wavs:
            _have = [p for p in audio_wavs if p and os.path.exists(p)]
            if len(_have) != len(paths):
                print(f"[JZL-管理器] ⚠️ 分段音频不齐（{len(_have)}/{len(paths)}）"
                      f"→ 回退逐段拼接（音轨可能有段边界误差）")
            else:
                try:
                    if _concat_wavs(_have, wav_all):
                        cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                               "-i", wav_all, "-map", "0:v:0", "-map", "1:a:0",
                               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                               "-movflags", "+faststart", out_path]
                        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                        if proc.returncode == 0:
                            print("[JZL-管理器] 拼接：视频 -c copy 无损 + 音频统一编码一次"
                                  "（段边界无 AAC 误差）")
                            return out_path
                        raise RuntimeError(proc.stderr.decode("utf-8", "ignore")[-400:])
                except Exception as _ae:
                    print(f"[JZL-管理器] ⚠️ 音频统一编码拼接失败，回退逐段拼接：{_ae}")

        cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
               "-c", "copy", "-movflags", "+faststart", out_path]
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 合并失败：{proc.stderr.decode('utf-8', 'ignore')[-500:]}")
        return out_path
    finally:
        for _f in (list_path, wav_all):
            try:
                if os.path.exists(_f):
                    os.remove(_f)
            except Exception:
                pass


def _concat_images(images):
    """拼接多段视频帧 [T,H,W,C] → 单段 [sum(T),H,W,C]（跳过 None）。"""
    valid = [img for img in (images or []) if img is not None]
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0]
    return torch.cat(valid, dim=0)


def _concat_audios(audios):
    """拼接多段音频 waveform [B,C,L] → [B,C,sum(L)]（跳过 None，统一采样率）。"""
    valid = [a for a in (audios or []) if a is not None and a.get("waveform") is not None]
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0]
    sr = valid[0].get("sample_rate", 32000)
    waves = []
    for a in valid:
        w = a.get("waveform")
        a_sr = a.get("sample_rate", 32000)
        if a_sr != sr:
            w = torchaudio.functional.resample(w, a_sr, sr)
        waves.append(w)
    return {"waveform": torch.cat(waves, dim=-1), "sample_rate": sr}


def _resolve_gen_size(aspect_ratio, megapixels):
    """生成分辨率：官方 ResolutionSelector 公式（画幅×MP → sqrt → 对齐倍数固定 32）。"""
    ratio = ASPECT_RATIOS.get(aspect_ratio, (16, 9))
    mp = float(megapixels or 1.0)
    multiple = 32  # 对齐倍数底层锁定 32
    total = mp * 1024 * 1024
    scale = math.sqrt(total / (ratio[0] * ratio[1]))
    return (
        max(32, round(ratio[0] * scale / multiple) * multiple),
        max(32, round(ratio[1] * scale / multiple) * multiple),
    )


def _seg_len_for(raw, duration):
    """本段视频帧长：优先读分段块里「**时长**: N」（英文块为「**Duration**: N」）元数据（秒）→ 17k+5 对齐帧数；
    无该元数据（直通/纯文本单段）回退统一 duration。区间化后各段时长各异，靠它逐段生效。"""
    m = re.search(r"\*\*(?:时长|Duration)\*\*\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)", raw or "")
    sec = float(m.group(1)) if m else float(duration or 8)
    sec = max(4.0, min(SEGMENT_DURATION_MAX_SEC, sec))
    return _resolve_length(sec)


def _parse_duration_spec(duration):
    """时长解析：支持新选项「固定N秒」/「A~B秒」，并兼容旧值（8 / '5' / '5-5' / '4-10' / '4~10'）→ (lo, hi, desc)。
    lo==hi 为固定秒数；lo<hi 为区间（拆解时每段按剧情弧线在区间内取实际秒数）。"""
    _t = str(duration if duration is not None else SEGMENT_DURATION_DEFAULT).strip()
    lo = hi = None
    _fx = re.search(r'固定\s*(\d+(?:\.\d+)?)\s*秒', _t)
    if _fx:
        lo = hi = float(_fx.group(1))
    if lo is None:
        _rg = re.search(r'(\d+(?:\.\d+)?)\s*[~\-—–－]\s*(\d+(?:\.\d+)?)', _t)
        if _rg:
            lo = float(_rg.group(1)); hi = float(_rg.group(2))
    if lo is None:
        try:
            _x = float(_t); lo = _x; hi = _x
        except Exception:
            lo = 8.0; hi = 8.0
    lo = max(4.0, min(SEGMENT_DURATION_MAX_SEC, lo)); hi = max(lo, min(SEGMENT_DURATION_MAX_SEC, hi))
    if lo > hi:
        lo, hi = hi, lo
    if lo == hi:
        return (lo, hi, f'统一 {int(lo)} 秒')
    return (lo, hi, f'{int(lo)}~{int(hi)} 秒区间（每段按剧情弧线给实际秒数，写进各段 **时长**）')


def _resolve_length(duration):
    """时长（秒）→ 24fps 帧数 → 对齐 17k+5。"""
    duration = float(duration or 8)
    return align_frame_count(max(5, round(duration * 24)))


def _resolve_seed(seed_mode, base_seed, index):
    if seed_mode == "fixed":
        return base_seed
    if seed_mode == "increment":
        return base_seed + index
    if seed_mode == "decrement":
        return base_seed - index
    # 0xffffffffffffffff(2^64-1) 超出 C int64 上界会溢出；用 int64 最大值 2^63-1
    return torch.randint(0, 0x7fffffffffffffff, (1,)).item()


# ── ③提示词：资产介绍 / 启用判断 / 偏好 / LLM 调用 ─────────────

_IMAGE_SLOT_TYPES = ("角色", "场景", "道具", "分镜", "其他")

# 音频子分类 → 槽位名前缀（各自独立编号空间）。
# 三个子分类必须用不同前缀，否则会撞出多个「音频A」，LLM 调度素材时无法区分。
_AUDIO_SLOT_PREFIX = {
    "角色音色": "音色",
    "背景配乐": "配乐",
    "念白": "念白",
}

# ref_intro 输出排序：类型顺序（角色→场景→道具→分镜→其他；视频/音频）→ 字母顺序
_SLOT_TYPE_ORDER = {
    "角色": 0, "场景": 1, "道具": 2, "分镜": 3, "其他": 4,
    "视频": 0,
    "音色": 0, "配乐": 1, "念白": 2, "音频": 3,
}


def _asset_type_for_slot(kind, typ):
    """资产类型 → 调度槽位类型（build_shot_prompt 对照表识别的类型）。

    「自定义」→「其他」：官方 material_table/slot_map 只识别 角色/场景/道具/分镜/视频/音频/音效/音乐/其他。
    音频子分类各自带独立前缀（音色/配乐/念白），其余音频统一走「音频」。
    """
    if kind == "image":
        if typ == "自定义":
            return "其他"
        return typ if typ in _IMAGE_SLOT_TYPES else "其他"
    if kind == "video":
        return "视频"
    if kind == "audio":
        return _AUDIO_SLOT_PREFIX.get(typ, "音频")
    return None


def _audio_voice_label(item):
    """「角色音色」条目的展示名：指定了角色 →「{角色名}音色」，否则回退条目名。

    角色改名由前端联动（下拉选项来自图片里的角色列表），这里只做展示与资产池命名。
    """
    ch = (item.get("character") or "").strip()
    if ch:
        return f"{ch}音色"
    return (item.get("name") or "").strip()


def _asset_display_name(kind, item):
    """条目在「素材描述 / 资产池」里使用的名称。

    「音频 → 角色音色」没有独立名称字段（名称由所绑定的角色决定）→ 用 {角色名}音色；
    其余类型一律用用户填写的名称。**资产池 key 与槽位描述必须共用本函数，否则槽位映射取不到数据。**
    """
    typ = (item.get("type") or "").strip()
    if kind == "audio" and typ == "角色音色":
        return _audio_voice_label(item)
    return (item.get("name") or "").strip()


def _asset_name(kind, index, item, name_override=None):
    """生成资产名：图片1 角色 孙悟空 / 视频1 主体 跳舞的美女 / 音频1 角色音色 孙悟空音色。"""
    prefix = {"image": "图片", "video": "视频", "audio": "音频"}.get(kind, "资产")
    typ = (item.get("type") or "").strip()
    name = _asset_display_name(kind, item) if name_override is None else (name_override or "").strip()
    return " ".join(x for x in (f"{prefix}{index + 1}", typ, name) if x)


def _build_asset_intro(assets_cfg):
    """从勾选素材生成三路 ref_intro（槽位格式）+ 槽位→资产名映射。

    返回 (ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset)。
    槽位按类型独立从 A 编号（角色A/场景A/道具A/视频A/音频A…），
    输出排序：类型顺序（角色→场景→道具→自定义→其他）→ 字母顺序（A/B/C…）。
    资产名 = 图片N 类型 名称（与 JZL_ASSET_POOL key 一致）。
    """
    slot_to_asset = {}
    counters = {}
    entries = {"image": [], "video": [], "audio": []}
    for kind, key in (("image", "images"), ("video", "videos"), ("audio", "audios")):
        for i, item in enumerate(assets_cfg.get(key) or []):
            if not item.get("enabled", True):
                continue
            typ = (item.get("type") or "").strip()
            name = _asset_display_name(kind, item)   # 「角色音色」的名称来自绑定角色
            if not name:
                continue
            slot_type = _asset_type_for_slot(kind, typ)
            if not slot_type:
                continue
            # 编号：用户手选 26 字母（A-Z）；缺失/非法时按类型自动兜底编号（旧资产兼容）
            letter = (item.get("letter") or "").strip().upper()
            if not re.match(r'^[A-Z]$', letter):
                letter = chr(ord("A") + counters.get(slot_type, 0))
            slot = f"{slot_type}{letter}"
            # 同类同字母冲突兜底：已被占用则顺延下一个可用字母（旧配置/手动重复）
            while slot in slot_to_asset and letter < "Z":
                letter = chr(ord(letter) + 1)
                slot = f"{slot_type}{letter}"
            counters[slot_type] = counters.get(slot_type, 0) + 1
            slot_to_asset[slot] = _asset_name(kind, i, item)
            desc = (item.get("description") or "").strip()
            # 槽位格式（官方解析）：角色A = 名称（描述）
            text = f"{slot} = {name}"
            if desc:
                text += f"（{desc}）"
            entries[kind].append((_SLOT_TYPE_ORDER.get(slot_type, 9), letter, text))

    out = {}
    for kind in ("image", "video", "audio"):
        entries[kind].sort(key=lambda e: (e[0], e[1]))
        out[kind] = [e[2] for e in entries[kind]]
    return (
        "\n".join(out["image"]),
        "\n".join(out["video"]),
        "\n".join(out["audio"]),
        slot_to_asset,
    )


def _normalize_dispatch_slots(script_text, slot_to_asset):
    """调度指令规范化：LLM 误把 slots 写成「素材名」（如 角色:兔子）时，用槽位映射反查纠正为「槽位名」（角色:角色A）。

    官方设定词要求 slots 写「类型:槽位名」且严禁写素材名，但本地模型（如 Qwen3.5-9B）有时不严格遵循——
    拆解模式里故事直接出现角色名时尤其容易照抄。这里是确定性的兜底，不依赖 LLM 自觉；
    找不到映射的槽位原样保留，不影响正常输出。
    """
    if not script_text or not slot_to_asset:
        return script_text
    # 反向映射：素材名 → 槽位名（slot_to_asset 值为「图片N 类型 素材名」）
    name_to_slot = {}
    for slot, asset in slot_to_asset.items():
        m = re.match(r'^(?:图片|视频|音频)\d+\s+[^\s]+\s+(.+)$', asset or "")
        full = m.group(1).strip() if m else (asset or "").strip()
        if full:
            name_to_slot.setdefault(full, slot)
            if " " in full:
                name_to_slot.setdefault(full.split()[-1], slot)  # 短名兜底（如「兔子」）

    def _fix(raw):
        try:
            d = json.loads(raw)
        except Exception:
            return raw
        slots = d.get("slots")
        if not isinstance(slots, list):
            return raw
        new_slots, changed = [], False
        for s in slots:
            if not isinstance(s, str):
                new_slots.append(s)
                continue
            if ":" in s:
                typ, val = s.split(":", 1)
                typ, val = typ.strip(), val.strip()
            else:
                typ, val = "", s.strip()
            target = name_to_slot.get(val)
            if target and (not typ or target.startswith(typ)):
                new_slots.append(f"{typ}:{target}" if typ else target)
                changed = True
            else:
                new_slots.append(s)
        if changed:
            d["slots"] = new_slots
            return json.dumps(d, ensure_ascii=False)
        return raw

    pat = re.compile(r'===(SCENE_INSTRUCTION|VIDEO_INSTRUCTION|AUDIO_INSTRUCTION)===\s*\n([^\n]+)')
    return pat.sub(lambda m: f"==={m.group(1)}===\n{_fix(m.group(2).strip())}", script_text)


def _normalize_scene_slots(script_text, slot_to_asset):
    """场景槽位校正：LLM 拆解时可能把「场景」槽位标错（如第二段兔子家写成 场景:场景A）。

    用 H3_PROMPT 的 subject_definitions 描述（<Subject N> 是 <Picture M> 中的{描述}）匹配「场景」素材名，
    若 slots 里「场景」槽位指向的素材与描述不符，则确定性纠正为该素材对应的槽位名。
    只校正「场景」类型（每段场景唯一、匹配最可靠）；找不到映射或无法解析的块原样保留。
    """
    if not script_text or not slot_to_asset:
        return script_text
    # 「场景」素材名 → 槽位名（含短名兜底）
    scene_slots = {}
    for slot, asset in slot_to_asset.items():
        if not slot.startswith("场景"):
            continue
        m = re.match(r'^(?:图片|视频|音频)\d+\s+[^\s]+\s+(.+)$', asset or "")
        full = m.group(1).strip() if m else (asset or "").strip()
        if full:
            scene_slots.setdefault(full, slot)
            if " " in full:
                scene_slots.setdefault(full.split()[-1], slot)
    if not scene_slots:
        return script_text

    def _process(block):
        m_scene = re.search(r'===SCENE_INSTRUCTION===\s*\n([^\n]+)', block)
        if not m_scene:
            return block
        try:
            d = json.loads(m_scene.group(1).strip())
        except Exception:
            return block
        slots = d.get("slots")
        if not isinstance(slots, list):
            return block
        # 顶部「**场景**: 」字段（英文块为「**Scene**: 」，与每段标题/场景元数据对齐，最可靠）；回退 subject_definitions 描述
        top_scenes = []
        m_top = re.search(r'^\*\*(?:场景|Scene)\*\*\s*[:：]\s*(.+)$', block, re.M)
        if m_top:
            top_scenes = [x.strip() for x in re.split(r'[,，、/;；]', m_top.group(1)) if x.strip()]
        # subject_definitions: <Subject N> 是 <Picture M> 中的{描述}
        descs = []  # descs[M-1] = 描述
        m_h3 = re.search(r'===H3_PROMPT===(.*?)(?:===|\Z)', block, re.S)
        if m_h3:
            for sm in re.finditer(r'<Subject\s+\d+>\s*是\s*<Picture\s+(\d+)>\s*中的(.*)', m_h3.group(1)):
                try:
                    pic = int(sm.group(1))
                except Exception:
                    continue
                desc = sm.group(2).strip()
                while len(descs) < pic:
                    descs.append("")
                descs[pic - 1] = desc
        if not top_scenes and not descs:
            return block
        # 图片类槽位顺序 = <Picture> 序号顺序（官方约定）
        new_slots, changed, img_idx = list(slots), False, 0
        for i, s in enumerate(slots):
            if not isinstance(s, str) or ":" not in s:
                continue
            typ, val = s.split(":", 1)
            typ, val = typ.strip(), val.strip()
            if typ not in ("场景", "角色", "道具", "分镜", "其他"):
                continue
            desc = descs[img_idx] if img_idx < len(descs) else ""
            img_idx += 1
            if typ != "场景":
                continue
            # 候选：顶部「场景」字段优先，回退描述
            cands = top_scenes if top_scenes else ([desc] if desc else [])
            if not cands:
                continue
            # 匹配场景素材名（长名优先）
            hit = None
            for cand in cands:
                for nm, slot in scene_slots.items():
                    if _match_asset(nm, cand):
                        if hit is None or len(nm) > len(hit[0]):
                            hit = (nm, slot)
            if not hit:
                continue
            cur = slot_to_asset.get(val) or slot_to_asset.get(f"场景:{val}") or ""
            cm = re.match(r'^(?:图片|视频|音频)\d+\s+[^\s]+\s+(.+)$', cur or "")
            cur_name = cm.group(1).strip() if cm else (cur or "").strip()
            if cur_name and _match_asset(cur_name, hit[0]):
                continue  # 已正确，无需改
            new_slots[i] = f"场景:{hit[1]}"
            changed = True
        if changed:
            d["slots"] = new_slots
            return block.replace(m_scene.group(0),
                                 f"===SCENE_INSTRUCTION===\n{json.dumps(d, ensure_ascii=False)}")
        return block

    pat = re.compile(r'\[SHOT_START\].*?\[SHOT_END\]', re.S)
    return pat.sub(lambda m: _process(m.group(0)), script_text)


def _prune_fantasy_assets(script_text, slot_to_asset):
    """幻想素材清理：顶部元数据字段与调度 slots 中，凡不在「传给 LLM 的素材描述」范围内的名称一律清除。

    LLM 可能把剧情需要的元素（如木栅栏）当道具写进每段顶部列表 / 调度 slots，但素材描述里没有 → 幻觉。
    依据：已知素材名集合（slot_to_asset 值中的名称）+ 已知槽位集合（slot_to_asset 键）。
    只清理「顶部字段 + 调度 slots」，不动 detailed_description 剧情正文。
    """
    if not script_text or not slot_to_asset:
        return script_text
    known_names = set()
    known_slots = set(slot_to_asset.keys())
    for asset in slot_to_asset.values():
        m = re.match(r'^(?:图片|视频|音频)\d+\s+[^\s]+\s+(.+)$', asset or "")
        full = m.group(1).strip() if m else (asset or "").strip()
        if full:
            known_names.add(full)
            if " " in full:
                known_names.add(full.split()[-1])

    def _keep(item):
        item = (item or "").strip()
        if not item or item in ("无", "none", "None", "NONE", "n/a", "N/A", "NA", "-", "—"):
            return True
        if ":" in item:
            _t, v = item.split(":", 1)
            return v.strip() in known_slots
        return any(_match_asset(nm, item) for nm in known_names)

    def _fix_fields(block):
        _en = bool(re.search(r'\*\*(?:Title|Duration|Shot Size|Camera Movement|Mood & Lighting)\*\*', block))
        _empty, _sep = ("none", ", ") if _en else ("无", "、")

        def _repl(m):
            key = m.group(1).strip()
            items = [x.strip() for x in re.split(r'[,，、/;；]', m.group(2)) if x.strip()]
            kept = [x for x in items if _keep(x)]
            return f"**{key}**: " + (_sep.join(kept) if kept else _empty)
        return re.sub(r'^\*\*((?:角色|场景|道具|视频|音频|音色|配乐|念白|音效|音乐|其他|Characters|Scene|Props|Video|Audio|Voice|Music|Sound|Other))\*\*\s*[:：]\s*(.+)$',
                      _repl, block, flags=re.M)

    def _fix_slots(raw):
        try:
            d = json.loads(raw)
        except Exception:
            return raw
        slots = d.get("slots")
        if not isinstance(slots, list):
            return raw
        kept = [s for s in slots if _keep(s)]
        if len(kept) == len(slots):
            return raw
        d["slots"] = kept
        return json.dumps(d, ensure_ascii=False)

    def _process(block):
        block = _fix_fields(block)
        pat = re.compile(r'===(SCENE_INSTRUCTION|VIDEO_INSTRUCTION|AUDIO_INSTRUCTION)===\s*\n([^\n]+)')
        block = pat.sub(lambda m: f"==={m.group(1)}===\n{_fix_slots(m.group(2).strip())}", block)
        return block

    pat = re.compile(r'\[SHOT_START\].*?\[SHOT_END\]', re.S)
    return pat.sub(lambda m: _process(m.group(0)), script_text)


def _detect_enables(story, assets_cfg):
    """按勾选素材类型 + 提示词 @引用 智能判断四个调度开关。"""
    enable_scene = enable_props = enable_video = enable_audio = False
    for item in (assets_cfg.get("images") or []):
        if item.get("enabled", True):
            t = (item.get("type") or "").strip()
            if t == "场景":
                enable_scene = True
            elif t == "道具":
                enable_props = True
    for item in (assets_cfg.get("videos") or []):
        if item.get("enabled", True):
            enable_video = True
    for item in (assets_cfg.get("audios") or []):
        if item.get("enabled", True):
            enable_audio = True
    # 提示词 @引用兜底（视频/音频类型无歧义；图片类型按场景/道具字样判断）
    text = story or ""
    if re.search(r'视频\d+', text):
        enable_video = True
    if re.search(r'音频\d+', text):
        enable_audio = True
    if re.search(r'图片\d+[^\s@，。；,.、]*场景', text):
        enable_scene = True
    if re.search(r'图片\d+[^\s@，。；,.、]*道具', text):
        enable_props = True
    return enable_scene, enable_props, enable_video, enable_audio


def _seg_label(auto_count, count):
    """日志用分段数标签：「剧情决定」或「N段」。"""
    return "剧情决定" if auto_count else f"{count}段"


def _llm_backend_label(enhance):
    """日志用后端标签：API / 本地。"""
    return "API" if "api" in str((enhance or {}).get("llm_backend", "")) else "本地"


def _run_banner(tag, run_mode, seg_label, story_style, story_name, enhance, extra=""):
    """四个导演台共用的运行横幅：只打关键参数（运行模式 / 分段 / 风格 / 故事 / 后端 / 增强）。"""
    print(f"[JZL-{tag}] 运行模式={run_mode} | 分段={seg_label} | 风格「{story_style or '热血战斗'}」 | "
          f"故事「{story_name or ''}」 | 后端={_llm_backend_label(enhance)} | "
          f"增强={'开' if (enhance or {}).get('enabled', False) else '关'}{extra}")
    # 会生成视频的模式下顺带报一次落盘编码器（固定位置，避免首次落盘时才突然冒出来）
    if "仅提示词" not in str(run_mode):
        print(f"[JZL-{tag}] 落盘编码器：{_ffmpeg_short_desc()}")


def _build_preference(enhance):
    """偏好设置（镜头语言）+ 自定义提示词 → preference 字符串。"""
    pref_cfg = enhance.get("preference") or {}
    parts = []
    try:
        from .nodes_llama import JZL_MiniMaxH3Preference
        parts.append(JZL_MiniMaxH3Preference().build(
            pref_cfg.get("shot_size", "随机组合"),
            pref_cfg.get("camera_move", "随机组合"),
            pref_cfg.get("cut_rhythm", "随机"),
            pref_cfg.get("transition", "随机"),
            pref_cfg.get("music_style", "禁止音乐 / No Music"),
            pref_cfg.get("creative_req", "无特别要求"),
            pref_cfg.get("detail_length", "标准 (350-500字)"),
            pref_cfg.get("custom", ""),
            视角机位=pref_cfg.get("angle_view", "根据剧情"),
        )[0])
    except Exception as _e:
        print(f"[JZL-偏好] 构建偏好失败：{_e}")
    custom = (enhance.get("custom_prompt") or "").strip()
    if custom:
        parts.append(custom)
    return "\n".join(parts)


def _llm_local_config(enhance):
    """本地 LLM 模型配置（custom_config）+ 推理参数（parameters）。"""
    c = enhance.get("llm") or {}
    custom_config = {
        "model": (c.get("model") or "").strip(),
        "mmproj": c.get("mmproj") or "None",
        "chat_handler": c.get("chat_handler") or "None",
        "n_ctx": int(c.get("n_ctx", 32768)),
        "vram_limit": int(c.get("vram_limit", -1)),
        "image_min_tokens": int(c.get("image_min_tokens", 0)),
        "image_max_tokens": int(c.get("image_max_tokens", 0)),
        "backend": c.get("backend") or "llama-cpp-python",
        "gpu_device": c.get("gpu_device") or "auto",
    }
    parameters = {
        "max_tokens": int(c.get("max_tokens", 8192)),
        "top_k": int(c.get("top_k", 40)),
        "top_p": float(c.get("top_p", 0.9)),
        "min_p": float(c.get("min_p", 0.05)),
        "typical_p": float(c.get("typical_p", 1.0)),
        "temperature": float(c.get("temperature", 0.6)),
        "repeat_penalty": float(c.get("repeat_penalty", 1.05)),
        "frequency_penalty": float(c.get("frequency_penalty", 0.0)),
        "present_penalty": float(c.get("present_penalty", 0.0)),
        "mirostat_mode": int(c.get("mirostat_mode", 0)),
        "mirostat_eta": float(c.get("mirostat_eta", 0.1)),
        "mirostat_tau": float(c.get("mirostat_tau", 5.0)),
        "state_uid": -1,
    }
    return custom_config, parameters


def _llm_chat(enhance, system_prompt, user_msg, seed):
    """按 enhance 配置调用 LLM（本地/API），返回生成文本。"""
    from .llama_backend import LLAMA_CPP_STORAGE
    from .nodes_llama import JZL_MiniMax_ScriptProcessor

    if "api" in str(enhance.get("llm_backend", "")):
        api_cfg = enhance.get("api") or {}
        return JZL_MiniMax_ScriptProcessor._call_api(
            json.dumps(api_cfg, ensure_ascii=False), system_prompt, user_msg)

    custom_config, parameters = _llm_local_config(enhance)
    if not custom_config["model"]:
        return "[错误] 未选择本地 LLM 模型（请在「文本增强设置」里配置）"
    if not LLAMA_CPP_STORAGE.llm or LLAMA_CPP_STORAGE.current_config != custom_config:
        LLAMA_CPP_STORAGE.load_model(custom_config, "故事扩写")
    try:
        _params = parameters.copy()
        _params.pop("present_penalty", None)
        _params.pop("state_uid", None)
        output = LLAMA_CPP_STORAGE.llm.create_chat_completion(
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_msg}],
            seed=seed, **_params)
        return output["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[LLM 错误] {e}"


def _llm_finish(enhance):
    """最后一个 LLM 步骤后：按 force_offload 决定是否卸载本地模型。"""
    if "api" in str(enhance.get("llm_backend", "")):
        return
    try:
        from .llama_backend import LLAMA_CPP_STORAGE
        if LLAMA_CPP_STORAGE.llm is None:
            return  # 本轮未加载本地模型，无需清理（避免无谓 soft_empty_cache）
        if enhance.get("force_offload", False):
            LLAMA_CPP_STORAGE.clean()
        else:
            LLAMA_CPP_STORAGE.clean_state()
    except Exception:
        pass


def _persist_seed_control(manager, enhance, seed_control, current_seed, used_seed):
    """control_after_generate：更新 seed 并写回全局（旧工作流回退），返回新 seed（None=不变）。"""
    if seed_control == "randomize":
        new_seed = int(used_seed)
    elif seed_control == "increment":
        new_seed = current_seed + 1
    elif seed_control == "decrement":
        new_seed = current_seed - 1
    else:
        return None
    if int(enhance.get("seed", 0) or 0) == new_seed:
        return None
    enhance["seed"] = new_seed
    manager["enhance"] = enhance
    try:
        _write_manager_settings(manager)
    except Exception:
        pass
    return new_seed


def _build_seed_ui(manager, enhance, seed_control, current_seed, used_seed):
    """构造 control_after_generate 的 ui 回传（前端据此更新本节点 manager_settings 里的 seed）。

    注意：ui 的每个值都必须用 list 包一层——ComfyUI execution.get_output_from_returns 会对每个
    value 再展开一次（`ui = {k: [y for x in uis for y in x[k]] ...}`）：传裸 dict 会被迭代成
    key 列表（["seed", "seed_control"]），传裸 str 会被拆成单字符，前端永远收到错数据。
    同理 manager_error 也必须写成 ["..."]，否则错误提示只显示第一个字。
    """
    ret = _persist_seed_control(manager, enhance, seed_control, current_seed, used_seed)
    if ret is None:
        return None
    return {"seed_update": [{"seed": str(int(ret)), "seed_control": seed_control}]}


def _enhance_bus(enhance):
    """构造给「提示词增强」节点复用的 BUS dict（本地/API 双后端）。"""
    is_api = "api" in str(enhance.get("llm_backend", ""))
    custom_config, parameters = _llm_local_config(enhance)
    return {
        "use_api": is_api,
        "save_states": False,
        "api_config": json.dumps(enhance.get("api") or {}, ensure_ascii=False) if is_api else None,
        "llama_model": custom_config if not is_api else None,
        "parameters": parameters if not is_api else None,
    }


def _run_story_expander(story, manager, story_style, story_name, prompt_lang, seed, expand_words=500,
                        expand_source="素材", ref_image_intro="", ref_video_intro="", ref_audio_intro="",
                        gen_dir=None):
    """故事扩写：只按「故事风格 + 扩写字数」把用户故事扩写为丰满正文，不拆解。

    expand_source: "素材"=读取素材清单按素材扩写(禁杜撰)；"自由"=自由发挥。
    ref_image_intro/ref_video_intro/ref_audio_intro: 参考素材描述（"素材"模式使用）。
    返回 (expanded_text, err)。扩写正文存 gen_dir/故事扩写/（无 gen_dir 时自动新建批次）。
    """
    from .sheding.story_expand_rules import build_expand_prompt
    try:
        from .sheding.story_styles import resolve_style as _resolve_style
    except ImportError:
        _resolve_style = lambda s: s or "热血战斗"

    enhance = manager.get("enhance") or {}
    lang = "zh" if "ZH" in str(prompt_lang or "") else "en"
    expand_words = max(100, min(3000, int(expand_words or 500)))
    expand_source = str(expand_source or "素材")

    style_name = (story_style or "热血战斗").strip()
    style_text = _resolve_style(style_name)
    # 素材模式：拼三段素材描述（图片=角色/场景/道具，视频/音频仅作参考说明，扩写正文主要用图片素材的角色场景道具）
    material_intro = "\n".join(x for x in (ref_image_intro, ref_video_intro, ref_audio_intro) if x and x.strip())
    system_prompt = build_expand_prompt(lang, style_text, expand_words,
                                        expand_source=expand_source, material_intro=material_intro)

    # 与拆解/增强共享 seed/生成后控制；强制卸载统一由外部 _llm_finish 处理
    output = _llm_chat(enhance, system_prompt, (story or "").strip(), seed)
    if output.startswith(("[错误]", "[API 错误]", "[API 配置错误]", "[LLM 错误]")):
        return (story or "").strip(), output
    expanded = output.strip()
    if not expanded:
        return (story or "").strip(), "[错误] 扩写结果为空"

    # 落盘 → gen_dir/故事扩写/扩写后故事_{时间戳}.txt
    if gen_dir is None:
        from .nodes_llama import _next_generation_dir
        gen_dir = _next_generation_dir(story_name)
    try:
        from .nodes_llama import _safe_path
        _dir = os.path.join(gen_dir, "故事扩写")
        os.makedirs(_dir, exist_ok=True)
        _p = _safe_path(_dir, "扩写后故事")
        with open(_p, "w", encoding="utf-8") as f:
            f.write(expanded)
        print(f"[JZL-扩写] 已保存扩写故事：{_p}")
    except Exception as _se:
        print(f"[JZL-扩写] 落盘失败：{_se}")
    return expanded, None


def _run_script_processor(story, manager, video_count, story_style, story_name, duration, prompt_lang, seed,
                          ref_image_intro="", ref_video_intro="", ref_audio_intro="",
                          enable_scene=True, enable_props=True, enable_video=True, enable_audio=True,
                          mode="生成模式 (Generate)", gen_dir=None, seam_runway=0.0):
    """剧本与镜头处理器：直接严格调用 JZL_MiniMax_ScriptProcessor.execute（100% 复刻官方逻辑/格式）。

    返回 (script_output, err)。script_output = 统计表 + [SHOT_START] 分段块（生成模式含「【故事】」正文块），
    与「JZL - 🎬 剧本与镜头处理器」节点「剧本输出」端口内容完全一致。
    seam_runway：「无限时长」衔接跑道（秒）→ 第 2 段起的生成时长 = 设定时长 + 跑道（成片裁掉跑道 ⇒ 落盘 = 设定）。
    """
    from .nodes_llama import JZL_MiniMax_ScriptProcessor

    enhance = manager.get("enhance") or {}
    is_api = "api" in str(enhance.get("llm_backend", ""))
    custom_config, parameters = _llm_local_config(enhance)
    _vc_auto = bool(manager.get("_jzl_auto_count"))
    _vc_mode, _vc_n = _parse_video_count(video_count)
    count = _vc_n if _vc_n else VIDEO_COUNT_FALLBACK
    if _vc_mode == "auto":
        _vc_auto = True

    # 本地后端未选模型：直接给出友好错误（官方 ScriptProcessor 内部不校验空模型，会尝试加载而崩）
    if not is_api and not (custom_config.get("model") or "").strip():
        return story, "[错误] 未选择本地 LLM 模型（请在「文本增强设置」里配置）"

    # video_count → 精确分段数（数字字符串）；「剧情决定」→ 传特殊标签，由 LLM 按剧情时间线长度自动定段数
    seg_label = "剧情决定" if _vc_auto else str(count)

    # 自定义规则：管理器「系统提示词」直接喂给官方 ScriptProcessor 的 custom_rule_path（支持纯文本）
    custom_rule_text = (enhance.get("system_prompt") or "").strip()
    # 强制卸载与「开启增强」联动：增强开启时拆解后不强制卸载（保留模型给增强用，等增强完再卸，省重复加载）
    enhance_enabled = bool(enhance.get("enabled", False))
    script_force_offload = bool(enhance.get("force_offload", False)) and not enhance_enabled

    script_output, _bus = JZL_MiniMax_ScriptProcessor().execute(
        mode=mode,
        story_name=(story_name or "").strip(),
        story_input=(story or "").strip(),
        story_style=story_style or "热血战斗",
        use_custom_rule=bool(custom_rule_text),
        segment_count=seg_label,
        segment_duration=duration,
        prompt_lang=prompt_lang or "中文 [ZH]",
        ref_image_intro=ref_image_intro,
        ref_video_intro=ref_video_intro,
        ref_audio_intro=ref_audio_intro,
        enable_scene=enable_scene, enable_props=enable_props,
        enable_video=enable_video, enable_audio=enable_audio,
        seed=seed,
        force_offload=script_force_offload,
        save_states=False,
        llm_backend=enhance.get("llm_backend") or "本地模型 [local]",
        llama_model=custom_config if not is_api else None,
        parameters=parameters if not is_api else None,
        api_config=json.dumps(enhance.get("api") or {}, ensure_ascii=False) if is_api else None,
        preference=_build_preference(enhance),
        custom_rule_path=custom_rule_text or None,
        gen_dir=gen_dir,
        seam_runway=seam_runway,
        # 增强开启时这次加载会一直服务到增强结束 → 日志按「增强提示词」命名（与增强共用同一个本地模型）
        llm_purpose=("增强提示词" if enhance_enabled else "拆解剧本"),
    )
    if script_output.startswith(("[错误]", "[API 错误]", "[API 配置错误]", "[LLM 错误]")):
        return story, script_output
    return script_output, None


def _run_prompt_enhancer(segmented_text, manager, duration, story_style, prompt_lang, seed, story_name="", gen_dir=None,
                         seam_runway=0.0):
    """提示词增强：对拆解剧本二次润色（润色每个分段的 detailed_description）。

    直接复用官方 JZL_MiniMaxPromptEnhancer 节点逻辑（sheding/prompt_enhancer_rules 的规范）。
    与第一次 LLM 拆解共享 seed/生成后控制；强制卸载由外部 _llm_finish 统一在增强后处理。
    偏好设置(preference)在此注入 → 增强器按偏好逐条落实（二次润色）。
    增强成功后把结果保存到 gen_dir/已增强剧本/（无 gen_dir 时自动新建批次）。
    """
    from .nodes_prompt_enhancer import JZL_MiniMaxPromptEnhancer
    try:
        from .sheding.story_styles import resolve_style as _resolve_style
    except ImportError:
        _resolve_style = lambda s: s or "热血战斗"

    enhance = manager.get("enhance") or {}
    lang = "zh" if "ZH" in str(prompt_lang or "") else "en"
    _dlo, _dhi, _ddesc = _parse_duration_spec(duration)
    duration = int(_dlo)

    # 注入完整故事风格文本（视觉风格/色调光线/摄影语言/核心导演语法），增强器按风格逐条落实
    style_name = (story_style or "热血战斗").strip()
    style_text = _resolve_style(style_name)

    bus = _enhance_bus(enhance)
    bus.update({
        "lang": lang,
        "story_style": style_text,
        "segment_duration": duration,
        "seam_runway": float(seam_runway or 0.0),
        "preference": _build_preference(enhance),
        "custom_rules": (enhance.get("system_prompt") or "").strip(),
    })
    try:
        result = JZL_MiniMaxPromptEnhancer().enhance(segmented_text, bus, False, seed)[0]
    except Exception as e:
        return segmented_text, f"提示词增强失败：{e}"
    if result.startswith("[错误]"):
        return segmented_text, result
    # 保存增强后的剧本 → output/jzl/{故事名}/第NNNNN次生成/已增强剧本/增强后剧本_{时间戳}.txt
    if gen_dir is None:
        from .nodes_llama import _next_generation_dir
        gen_dir = _next_generation_dir(story_name)
    try:
        from .nodes_llama import _safe_path
        _dir = os.path.join(gen_dir, "已增强剧本")
        os.makedirs(_dir, exist_ok=True)
        _p = _safe_path(_dir, "增强后剧本")
        with open(_p, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"[JZL-增强] 已保存增强后剧本：{_p}")
    except Exception as _se:
        print(f"[JZL-增强] 保存增强后剧本失败：{_se}")
    return result, None


def _resolve_asset_path(path):
    """资产路径 → 服务器绝对路径：绝对路径直接用；相对路径按 input/output 目录解析
    （兼容官方 LoadImage 式相对路径 jzl/image/xxx.png，以及旧 jzl_assets/ 与历史绝对路径）。"""
    p = (path or "").strip()
    if not p:
        return ""
    if os.path.isfile(p):
        return p
    try:
        in_dir = folder_paths.get_input_directory()
        out_dir = folder_paths.get_output_directory()
        candidates = (
            os.path.join(in_dir, p),
            os.path.join(out_dir, p),
            os.path.join(in_dir, "jzl", p),
            os.path.join(in_dir, "jzl_assets", p),
        )
        for c in candidates:
            if os.path.isfile(c):
                return c
    except Exception:
        pass
    return ""


def _load_assets_into_pool(assets):
    """把配置的资产加载进 JZL_ASSET_POOL，返回 (manifest, errors)。"""
    manifest, errors = [], []

    for i, item in enumerate(assets.get("images", []) or []):
        if not item.get("enabled", True):
            continue
        path = _resolve_asset_path(item.get("path"))
        if not path:
            continue
        try:
            data = _load_image(path)
            name = _asset_name("image", i, item)
            JZL_ASSET_POOL[name] = {"kind": "image", "data": data}
            manifest.append({"name": name, "kind": "image", "type": item.get("type", "")})
        except Exception as e:
            errors.append(f"图片{i + 1}加载失败：{e}")

    for i, item in enumerate(assets.get("videos", []) or []):
        if not item.get("enabled", True):
            continue
        path = _resolve_asset_path(item.get("path"))
        if not path:
            continue
        data, err = _load_video(path)
        name = _asset_name("video", i, item)
        if data is None:
            errors.append(f"视频{i + 1}加载失败：{err}")
            continue
        JZL_ASSET_POOL[name] = {"kind": "video", "data": data}
        manifest.append({"name": name, "kind": "video", "type": item.get("type", "")})

    for i, item in enumerate(assets.get("audios", []) or []):
        if not item.get("enabled", True):
            continue
        path = _resolve_asset_path(item.get("path"))
        if not path:
            continue
        try:
            data = _load_audio(path)
            name = _asset_name("audio", i, item)
            JZL_ASSET_POOL[name] = {"kind": "audio", "data": data}
            manifest.append({"name": name, "kind": "audio", "type": item.get("type", "")})
        except Exception as e:
            errors.append(f"音频{i + 1}加载失败：{e}")

    _report_asset_pool(manifest, errors, assets)
    return manifest, errors


def _report_asset_pool(manifest, errors, assets_cfg):
    """打印素材池装载摘要。

    只在「有素材没进池」时输出（正常运行不刷屏）。**未上传文件/文件丢失的素材会被静默跳过**，
    但给 LLM 的素材描述仍会列出该槽位 → LLM 引用后取不到 → 本段退化成纯文本生成，
    而用户从日志上完全看不出来。这里把原因显式打出来。
    """
    try:
        total = sum(len(assets_cfg.get(k) or []) for k in ("images", "videos", "audios"))
        if not errors and len(manifest) >= total:
            return
        kinds = {}
        for m in manifest:
            kinds[m.get("kind")] = kinds.get(m.get("kind"), 0) + 1
        parts = [f"{lbl}{kinds.get(k, 0)}个" for k, lbl in (("image", "图片"), ("video", "视频"), ("audio", "音频"))]
        print(f"[JZL-素材] 载入：{'/'.join(parts)}（配置共 {total} 个）")
        missing = []
        for _kind, _key, _lbl in (("image", "images", "图片"), ("video", "videos", "视频"), ("audio", "audios", "音频")):
            for _it in (assets_cfg.get(_key) or []):
                if not _it.get("enabled", True):
                    continue
                _nm = _asset_display_name(_kind, _it)
                if not _nm:
                    continue
                if _it.get("path") and _resolve_asset_path(_it.get("path")):
                    continue
                missing.append(f"{_lbl}{(_it.get('type') or '').strip()}{_nm}")
        if missing:
            print(f"[JZL-素材] ⚠️ 未上传文件/文件缺失（不会作为参考，但仍写进了给 LLM 的素材描述）："
                  f"{'、'.join(missing[:8])}" + ("…" if len(missing) > 8 else ""))
        for _e in errors[:6]:
            print(f"[JZL-素材] ⚠️ {_e}")
    except Exception:
        pass


def _find_ffmpeg():
    """查找 ffmpeg 可执行文件：系统 PATH → imageio_ffmpeg。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _load_image(path):
    """加载图片 → IMAGE tensor [1, H, W, C]（RGB, 0-1）。"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # 处理 EXIF 旋转
    img = img.convert("RGB")
    import numpy as np
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None]  # [1, H, W, C]


def _load_audio(path):
    """加载音频 → AUDIO dict {"waveform": [1, C, L], "sample_rate": int}。

    优先 soundfile（与官方 LoadAudio 一致，扩展名大小写不敏感、兼容性最好），
    torchaudio 兜底（修复 .WAV 大写扩展名触发 _AudioDecoder() takes no arguments）。
    """
    import numpy as np
    # 1) soundfile（官方 LoadAudio 同款）
    try:
        import soundfile as sf
        data, sample_rate = sf.read(path, dtype="float32")  # [L, C] 或 [L]
        if data.ndim == 1:
            data = data[None, ...]
        else:
            data = data.T
        return {"waveform": torch.from_numpy(np.ascontiguousarray(data))[None], "sample_rate": int(sample_rate)}
    except Exception:
        pass
    # 2) torchaudio 兜底
    waveform, sample_rate = torchaudio.load(path)  # [C, L]
    return {"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate}


def _load_video(path):
    """加载视频 → IMAGE 序列 [T, H, W, C]（ffmpeg 抽帧，24fps，最多 240 帧）。"""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None, "未找到 ffmpeg（请安装 imageio-ffmpeg）"

    tmp = tempfile.mkdtemp(prefix="jzl_video_")
    try:
        out_pattern = os.path.join(tmp, "f_%05d.png")
        # fps=24 抽帧，短边缩放到 768 控制内存（scale 表达式内逗号用反斜杠转义，
        # 否则 ffmpeg 会把它当作 filter 分隔符导致抽帧失败——修复视频参考素材加载失败）
        cmd = [
            ffmpeg, "-loglevel", "error", "-i", path,
            "-vf", "fps=%d,scale=min(768\\,iw):-2" % VIDEO_FPS,
            "-frames:v", str(MAX_VIDEO_FRAMES),
            out_pattern,
        ]
        subprocess.run(cmd, check=True, timeout=600)
        frames = sorted(glob.glob(os.path.join(tmp, "f_*.png")))
        if not frames:
            return None, "视频抽帧失败（无帧）"
        if len(frames) > MAX_VIDEO_FRAMES:
            # 均匀采样
            idx = torch.linspace(0, len(frames) - 1, MAX_VIDEO_FRAMES).long()
            frames = [frames[i] for i in idx.tolist()]
        tensors = [_load_image(f)[0] for f in frames]  # 每个 [H, W, C]
        return torch.stack(tensors), None  # [T, H, W, C]
    except Exception as e:
        return None, f"视频加载失败：{e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class JZL_MiniMaxAssetManager(io.ComfyNode):
    """JZL - 🤖 MiniMax-H3短剧导演台Pro — 生成模式切换 + 模型/资产/参数/采样解码配置，输出 BUS。

    融合原 1140 工作流核心链路：
    分段处理中心 → 列表分发(每段独立提示词) → 场景/视频/音频调度(从资产池按名取参考)
    → 官方 ImageToVideo / ReferenceToVideo 编码 → 采样 → VAE 解码(视频+音频)
    → 写入生成总线池 JZL_BUS_POOL，输出 BUS(JSON) 给「视频保存分配」节点。

    V3 节点（io.ComfyNode）：配置入口按钮由前端 JS addDOMWidget 在节点表面添加。
    """

    @classmethod
    def define_schema(cls):
        story_styles = _story_style_options()
        return io.Schema(
            node_id="JZL_MiniMaxAssetManager",
            display_name="JZL - 🤖 MiniMax-H3短剧导演台Pro",
            category="JZL/MiniMax",
            description="MiniMax-H3 生成管理器：分段 + 调度 + 编码 + 采样 + 解码一体化，输出生成总线。",
            inputs=[
                # 运行模式（顶层切换，等同「JZL - 🎬 剧本与镜头处理器」）
                io.Combo.Input("run_mode", options=RUN_MODE_EMOJI,
                    default=RUN_MODE_EMOJI[0], display_name="运行模式",
                    tooltip="故事拆解模式=按情节把故事拆解为N段（不创意扩展）；故事扩写模式=只按风格+字数把故事扩写为丰满正文（不拆解，纯文本经「已处理剧本」输出）；穿透生成模式=跳过LLM拆解与增强，直接用提示词生成（含[SHOT_START]块则逐段，否则单段）；仅提示词输出=拆解+增强的完整分段剧本文本经「已处理剧本」输出，不生成视频"),
                # 生成参数（原生 widget，与「海螺H3视频参数」一致）
                io.String.Input("display_info", display_name="生成详情", default="分辨率：832x480丨每段帧数：192丨共计段数：6丨总帧数：1152丨总时长：48秒",
                    multiline=False, socketless=True,
                    tooltip="只读显示：当前画幅/MP/时长/段数计算出的分辨率、每段帧数、段数、总帧数、总时长（对齐倍数固定 32）"),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIO_OPTIONS, default="16:9 (Widescreen)",
                    display_name="画幅比例", tooltip="画幅比例（分辨率按 MP×1024² 公式自动计算，对齐倍数固定 32）"),
                io.Float.Input("megapixels", display_name="百万像素（MP）", default=0.4, min=0.1, max=16.0, step=0.1,
                    tooltip="总像素数（MP），画幅×MP 决定分辨率"),
                io.Combo.Input("duration", options=list(SEGMENT_DURATION_CHOICES), default=SEGMENT_DURATION_DEFAULT,
                    display_name="每段视频时长", tooltip="每段视频时长（秒）：固定 4~15 秒 或 区间（区间时 LLM 按剧情弧线给每段实际秒数）；等同「剧本与镜头处理器」的每段视频时长"),
                io.Float.Input("scale_factor", display_name="参考数值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="参考图放大系数"),
                io.Float.Input("upscale_scale", display_name="二采latent放大", default=1.0, min=1.0, max=4.0, step=0.05,
                    tooltip="二采（Ref2va）放大倍数"),
                io.Combo.Input("video_count", options=list(VIDEO_COUNT_CHOICES), default=VIDEO_COUNT_DEFAULT,
                    display_name="生成视频数量", tooltip="生成视频数量：默认「剧情决定」= LLM 按剧情时间线长度 ÷ 每段时长自动定段数（优先用足每段时长、防短剧情生成超多段）；也可固定 1/2/3/4/6/9/12/16/20/26/32/40/48/56 段（统一控制：提示词拆解段数 / 分发列表数 / 采样数 / 分段保存数）。🪄 穿透生成模式下忽略此设定：一律按提示词实际 [SHOT_START] 段数生成（上限 60 段）"),
                # ③提示词：剧本处理器参数（主界面显示）
                io.Combo.Input("story_style", options=story_styles, default=story_styles[0],
                    display_name="故事风格", tooltip="故事风格（剧本处理器按此风格拆解与润色）"),
                io.String.Input("story_name", display_name="故事名称", default="机智罗",
                    tooltip="故事名称（用于保存命名 / 日志）"),
                io.String.Input("external_prompt", display_name="提示词·接线（可连上游文本）", default="",
                    tooltip="提示词接线输入（与 CLIP Text Encode 同类）：此输入框左上角圆点可拖线连接上游 STRING 文本节点；连线后以上游文本为提示词（优先于「节点内提示词」大框；未接且留空则用大框内容）"),
                io.Model.Input("model", display_name="主模型", optional=True),
                io.Clip.Input("clip", display_name="CLIP", optional=True),
                io.Vae.Input("vae", display_name="视觉VAE", optional=True),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True),
                io.String.Input("internal_prompt", display_name="节点内提示词", multiline=True,
                    socketless=True,
                    tooltip="节点内编辑的提示词（提示词来源，随工作流保存）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True,
                    socketless=True, default="",
                    tooltip="本节点独立保存的完整配置 JSON（资产/增强/采样解码），随工作流保存，节点间互不影响"),
            ],
            outputs=[
                io.Image.Output(display_name="图像", is_output_list=True),
                io.Audio.Output(display_name="音频", is_output_list=True),
                io.String.Output("script", display_name="已处理剧本",
                    tooltip="全部LLM处理后的剧本/提示词文本：纯提示词生成模式=LLM处理结果；其余模式=拆解+增强后的分段文本"),
            ],
            is_output_node=True,  # 一键生成器自带落盘/输出，单节点即可运行（否则前端报 prompt_no_outputs）
        )

    @classmethod
    def fingerprint_inputs(cls, manager_settings="", **kwargs):
        # 等价于 V1 的 IS_CHANGED：本节点工作流内保存的配置变化必须触发重跑（节点间互不影响）。
        # 关键：LLM 或采样任一为 randomize 时每次都返回唯一指纹 → 节点必然重跑，
        # 保证「随机种子」每次生成不同结果（否则配置内容不变会命中缓存、生成被跳过）。
        try:
            cfg = _parse_node_manager_settings(manager_settings)
            enhance = cfg.get("enhance") or {}
            sample_decode = cfg.get("sample_decode") or {}
            llm_random = (enhance.get("seed_control") or "randomize") == "randomize"
            samp_random = (sample_decode.get("seed_mode") or "randomize") == "randomize"
            if llm_random or samp_random:
                return f"random@{time.time_ns()}"
            # 固定种子时：生成相关输入变化也必须触发重跑（否则改段数/模式/风格/画幅/提示词被缓存跳过 =「生成视频数量失效」）
            gen_keys = ["run_mode", "video_count", "story_style", "duration",
                        "aspect_ratio", "megapixels", "scale_factor", "upscale_scale",
                        "internal_prompt", "external_prompt"]
            gen_sig = {k: kwargs.get(k) for k in gen_keys}
            return f"{cfg}|{gen_sig}"
        except Exception:
            return "no-manager"

    @classmethod
    def execute(cls, run_mode="拆解故事模式", video_count=VIDEO_COUNT_DEFAULT, aspect_ratio="16:9 (Widescreen)",
                megapixels=1.0, duration="5-5", scale_factor=1.0, upscale_scale=1.5, display_info="",
                story_style="热血战斗", story_name="",
                external_prompt=None,
                internal_prompt=None, manager_settings="",
                clip=None, vae=None, audio_vae=None, model=None) -> io.NodeOutput:
        _vc_auto = _parse_video_count(video_count)[0] == "auto"   # 「剧情决定」标记（manager 建后写入）
        video_count = _vc_count(video_count)     # 「剧情决定」→ 占位 6（拆解后按实际分段数覆盖）
        run_mode = _normalize_run_mode(run_mode)
        pure_prompt = run_mode == "仅提示词输出"
        expand_mode = run_mode == "故事扩写模式"
        passthrough = run_mode == "穿透生成模式"
        # 拆解类（故事拆解/仅提示词输出）统一 Decompose；故事扩写模式不走拆解
        story_mode = "拆解模式 (Decompose)"

        # 故事名称必填：不填直接终止（生成命名 / 保存目录都依赖故事名）
        story_name = (story_name or "").strip()
        if not story_name:
            print("[JZL-管理器] ⚠️ 必须填写「故事名称」才能生成（已终止）")
            # 输出用 [None] 占位（非空列表）：即使旧版 ComfyUI 不识别 block_execution，
            # 也不会让下游 slice_dict 对空列表 v[-1] 崩溃
            return io.NodeOutput([None], [None], "", block_execution="必须填写「故事名称」后才能生成",
                                  ui={"manager_error": ["必须填写「故事名称」后才能生成"]})

        # 节点独立配置：工作流内保存的 manager_settings 优先；空则回退全局（旧工作流兼容）
        manager = _parse_node_manager_settings(manager_settings)
        manager["_jzl_auto_count"] = _vc_auto          # 「剧情决定」标记（供 _run_script_processor/日志用）
        duration_lo, duration_hi, duration_desc = _parse_duration_spec(duration)
        enhance = manager.get("enhance") or {}
        assets_cfg = manager.get("assets") or {}
        sample_decode = manager.get("sample_decode") or {}
        # 视频保存配置（存 manager_settings.save：保存模式/分段自动保存/自动合并/合并后删除）
        _save_cfg = manager.get("save") or {}
        save_mode = (_save_cfg.get("mode") or "分段保存").strip() or "分段保存"
        auto_save = bool(_save_cfg.get("auto_save"))
        auto_merge = bool(_save_cfg.get("auto_merge"))
        auto_merge_delete = bool(_save_cfg.get("auto_merge_delete"))
        # 输出语言（在「剧本拆解配置」面板最顶「输出语言」分类中配置，存 manager_settings）
        prompt_lang = (enhance.get("prompt_lang") or "中文 [ZH]").strip() or "中文 [ZH]"

        # 提示词：仅节点内编辑（外部提示词端口已删除）；防御旧工作流误连非字符串
        if not isinstance(internal_prompt, str):
            internal_prompt = ""
        prompt_input = (internal_prompt or "").strip()
        if isinstance(external_prompt, str) and external_prompt.strip():
            prompt_input = external_prompt.strip()  # 接线提示词优先：外部文本覆盖节点内提示词

        # 随机种子（LLM 剧本/增强）+ 生成后控制（control_after_generate）
        seed_control = (enhance.get("seed_control") or "randomize").strip() or "randomize"
        current_seed = int(enhance.get("seed", 0) or 0)
        if seed_control == "randomize":
            llm_seed = int(torch.randint(0, 0x7fffffffffffffff, (1,)).item())
        else:
            llm_seed = current_seed
        # 穿透生成：段数按提示词实际分段数定（横幅与实际生成段数保持一致）
        _pt_seg = _pt_seg_count(prompt_input) if passthrough else 0
        _seg_lbl = f"{_pt_seg}段" if _pt_seg else _seg_label(manager.get("_jzl_auto_count"), _vc_count(video_count))
        _run_banner("管理器", run_mode, _seg_lbl, story_style, story_name, enhance,
                    f" | 扩写≈{int(enhance.get('expand_words') or 500)}字" if expand_mode else "")

        # 故事扩写模式：只按「故事风格 + 扩写字数」把故事扩写为丰满正文，不拆解；
        # 纯文本经「已处理剧本」端口输出 + 落盘；无需 model/clip/vae/audio_vae。
        if expand_mode:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput([None], [None], "")
            _ew = int(enhance.get("expand_words") or 500)
            _esrc = str(enhance.get("expand_source") or "素材")
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            _xri, _xrv, _xra, _ = _build_asset_intro(assets_cfg)
            expanded, x_err = _run_story_expander(
                prompt_input, manager, story_style, story_name, prompt_lang, llm_seed,
                expand_words=_ew, expand_source=_esrc,
                ref_image_intro=_xri, ref_video_intro=_xrv, ref_audio_intro=_xra,
                gen_dir=gen_dir)
            if x_err:
                print(f"[JZL-管理器] LLM/API 出错，终止：{x_err}")
                return io.NodeOutput([None], [None], "", block_execution=f"⚠️ LLM/API 出错，已终止：{x_err}")
            if expanded:
                _save_last_script(story_name, expanded)
            _llm_finish(enhance)
            _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
            return io.NodeOutput([None], [None], expanded, ui=_seed_ui)

        # 仅提示词输出：按「拆解模式(Decompose)」拆解 + 增强（不含扩写），只输出纯文本
        # （经「已处理剧本」端口），彻底去掉编码/采样/解码/视频保存；无需接 model/clip/vae/audio_vae。
        if pure_prompt:
            if not prompt_input:
                _llm_finish(enhance)
                # 图像/音频用 [None] 占位（不输出空列表，避免下游 slice_dict 崩溃）；剧本文本正常
                return io.NodeOutput([None], [None], "")
            _pcount = _vc_count(video_count)
            ri, rv, ra, _ = _build_asset_intro(assets_cfg)
            _pe, _pp, _pv, _pa = _detect_enables(prompt_input, assets_cfg)
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            processed, p_err = _run_script_processor(
                prompt_input, manager, _pcount, story_style, story_name, duration, prompt_lang, llm_seed,
                ref_image_intro=ri, ref_video_intro=rv, ref_audio_intro=ra,
                enable_scene=_pe, enable_props=_pp, enable_video=_pv, enable_audio=_pa,
                mode="拆解模式 (Decompose)", gen_dir=gen_dir)
            if p_err:
                # LLM/API 出错必须终止
                print(f"[JZL-管理器] LLM/API 出错，终止：{p_err}")
                return io.NodeOutput([None], [None], "", block_execution=f"⚠️ LLM/API 出错，已终止：{p_err}")
            # 增强（可选，与拆解模式一致）
            if enhance.get("enabled", False):
                processed, p_err2 = _run_prompt_enhancer(processed, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir)
                if p_err2:
                    print(f"[JZL-管理器] LLM/API 出错，终止：{p_err2}")
                    return io.NodeOutput([None], [None], "", block_execution=f"⚠️ LLM/API 出错，已终止：{p_err2}")
            # 重拍模式：保存最后一次 LLM 处理后的完整提示词（供「提示词选择」加载）
            if processed:
                _save_last_script(story_name, processed)
            _llm_finish(enhance)
            _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
            return io.NodeOutput([None], [None], processed, ui=_seed_ui)

        # 清空重建池，避免旧资产/总线残留
        JZL_ASSET_POOL.clear()
        JZL_BUS_POOL.clear()
        JZL_SLOT_MAP.clear()

        manifest, errors = _load_assets_into_pool(assets_cfg)

        width, height = _resolve_gen_size(aspect_ratio, megapixels)
        length = _resolve_length(duration_hi)
        count = video_count


        # ── ③提示词：资产介绍 + 启用判断 + 槽位映射（供调度匹配）──
        ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset = _build_asset_intro(assets_cfg)
        JZL_SLOT_MAP.update(slot_to_asset)
        enable_scene, enable_props, enable_video, enable_audio = _detect_enables(prompt_input, assets_cfg)

        # ① 故事拆解（剧本处理器）：故事 → 分段（输入已是分段文本时跳过；直通模式跳过 LLM）
        # 故事拆解 / 故事扩写：输入必须是自然语言故事；含六段式标签（已拆解提示词）→ 报错终止
        if run_mode in ("故事拆解模式", "故事扩写模式"):
            _nl_err = _check_natural_language_input(prompt_input)
            if _nl_err:
                print(f"[JZL-管理器] ❌ {_nl_err}（已终止）")
                return io.NodeOutput([None], [None], prompt_input or "",
                                     block_execution=f"❌ {_nl_err}",
                                     ui={"manager_error": [_nl_err]})
        has_shots = bool(re.search(r'\[SHOT_START\]', prompt_input or ""))
        # 每次运行归入一个新批次：output/jzl/{故事名}/第NNNNN次生成/（拆解+增强共用同一批次）
        gen_dir = None
        if not passthrough and enhance.get("story_decompose", True) and not has_shots and prompt_input:
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_script_processor(
                prompt_input, manager, count, story_style, story_name, duration, prompt_lang, llm_seed,
                ref_image_intro=ref_image_intro, ref_video_intro=ref_video_intro, ref_audio_intro=ref_audio_intro,
                enable_scene=enable_scene, enable_props=enable_props,
                enable_video=enable_video, enable_audio=enable_audio, mode=story_mode, gen_dir=gen_dir)
            if err:
                # LLM/API 出错必须终止（不再用错误提示词继续生成）
                print(f"[JZL-管理器] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput([None], [None], prompt_input or "", block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        # ② 提示词增强（润色 detailed_description）：开启增强时执行（直通模式跳过）
        if not passthrough and enhance.get("enabled", False):
            if gen_dir is None:
                from .nodes_llama import _next_generation_dir
                gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_prompt_enhancer(prompt_input, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir)
            if err:
                # LLM/API 出错必须终止
                print(f"[JZL-管理器] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput([None], [None], prompt_input or "", block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        # 穿透生成模式：必须使用「拆解模式」拆解好的提示词；格式不对直接终止；段数自动对齐提示词实际分段数
        if passthrough:
            _pt_err, _pt_n = _check_decomposed_script(prompt_input, video_count,
                                                      auto_count=bool(manager.get("_jzl_auto_count")), tag="管理器")
            if _pt_err:
                print(f"[JZL-管理器] ❌ {_pt_err}（已终止）")
                return io.NodeOutput([None], [None], prompt_input or "",
                                     block_execution=f"❌ {_pt_err}",
                                     ui={"manager_error": [_pt_err]})
            count = _pt_n          # 以提示词实际分段数为准（忽略「生成视频数量」设定，避免多生成空占位段）

        # 调度指令规范化：LLM 可能把 slots 写成素材名（角色:兔子），用槽位映射反查纠正为槽位名（角色:角色A），
        # 保证下游调度严格按「类型:槽位名」匹配（对齐官方设定词「严禁写素材名」）
        prompt_input = _normalize_dispatch_slots(prompt_input, slot_to_asset)
        # 场景槽位校正：LLM 可能把「场景」槽位标错（第二段兔子家写成 场景:场景A），
        # 用每段顶部「**场景**: 」字段（回退 subject_definitions 描述）匹配场景素材名，确定性纠正为正确槽位
        prompt_input = _normalize_scene_slots(prompt_input, slot_to_asset)
        # 幻想素材清理：顶部字段/调度 slots 中不在素材描述范围内的名称一律清除（如幻觉道具「木栅栏」）
        prompt_input = _prune_fantasy_assets(prompt_input, slot_to_asset)

        # 重拍模式：保存最后一次 LLM 拆解/增强后的完整提示词（供「提示词选择」加载）
        if not passthrough and prompt_input:
            _save_last_script(story_name, prompt_input)

        # 卸载本地 LLM（最后一个 LLM 步骤之后，按 force_offload）
        _llm_finish(enhance)

        # 生成后控制（control_after_generate）：随机/递增时把新 seed 写回配置，下次运行生效
        _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)

        # ── 分段（融合分段处理中心）：按 [SHOT_START] 块切分 ──
        shots = re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', prompt_input or "", re.DOTALL)
        if manager.get("_jzl_auto_count") and shots:
            count = max(1, min(SEGMENT_COUNT_MAX, len(shots)))   # 「剧情决定」：以实际拆解出的分段数为准

        base_seed = int(sample_decode.get("seed", 0) or 0)
        seed_mode = sample_decode.get("seed_mode", "randomize") or "randomize"

        bus_items = []
        saved_segments = []
        _JZL_SEG_TRACEBACKS.clear()   # 本次运行的分段失败调用栈（诊断报告用）
        _JZL_SEG_ERR_SEEN.clear()     # 每轮重置去重表：否则本轮第 1 段失败会被上一轮抑制掉调用栈
        # 自动合并时同时收集各段无損 WAV → 合并时「音频统一编码一次」（消除段边界 AAC encoder delay 误差）
        _merge_wavs = [] if (auto_merge and auto_merge_path) else None
        # 保存/合并路径固定为运行中 ComfyUI 的 output/jzl（运行时可推导，不写死盘符，不可修改）
        _out_root = os.path.abspath(folder_paths.get_output_directory())
        _jzl_dir = os.path.join(_out_root, "jzl")
        _safe_story = _safe_story_name(story_name)
        # 逐段落盘 / 自动合并 都归入 output/jzl/{故事名}/（视频名称与保存文件夹均与故事名一致）
        auto_save_path = os.path.join(_jzl_dir, _safe_story)
        auto_merge_path = os.path.join(_jzl_dir, _safe_story)
        _batch = _next_batch(auto_save_path, _safe_story)  # 本次生成批次号（文件名「第 N 次生成」，与 Max 统一）
        _seg_start = _next_counter(auto_save_path, f"{_safe_story}_{_batch}次生成分段")
        if auto_save:
            try:
                os.makedirs(auto_save_path, exist_ok=True)
            except Exception as _me:
                errors.append(f"无法创建自动保存目录：{auto_save_path}（{_me}）")
                auto_save = False
        first_seed = None
        for i in range(count):
            raw = shots[i].strip() if i < len(shots) else ""
            h3, scene, vid, aud = _parse_four_in_one(raw)
            if not h3:
                # 直通/无四段标记：用当前块原文（直通多块时每块独立成段）；无块回退整段提示词
                h3 = raw.strip() if raw.strip() else (
                    prompt_input.strip() if i == 0 and prompt_input else "[未找到H3提示词]")

            # @ 引用：从提示词提取 @资产名（移除标记），匹配为参考素材
            mention_names, h3 = _extract_mentions(h3)

            # 参考提取（融合场景/视频/音频调度2：从资产池按名匹配）
            ref_images, ref_videos, ref_audios, ref_names = _collect_refs_with_names(scene, vid, aud, mention_names)

            # 生成模式：始终 ref2va（对齐 1146 分镜组：图片全部作 <Picture> 身份参考，不用首尾帧）
            mode = "纯文本生成音视频-T2VA"
            if ref_videos or ref_images or ref_audios:
                mode = "多参考生成音视频-REF2VA"

            can_generate = clip is not None and vae is not None and model is not None
            if (ref_videos or ref_audios) and audio_vae is None:
                can_generate = False

            seed = _resolve_seed(seed_mode, base_seed, i)
            if first_seed is None:
                first_seed = seed
            if not can_generate:
                need = "CLIP / VAE / model"
                if (ref_videos or ref_audios) and audio_vae is None:
                    need = "CLIP / VAE / model / audio_vae（本段含视频或音频参考）"
                bus_items.append({
                    "index": i, "mode": mode, "prompt": h3,
                    "has_image": False, "has_audio": False,
                    "error": f"未连接 {need}，仅完成分段",
                })
                continue

            print(f"[JZL-管理器] 分段 {i + 1}/{count} 生成中（{mode}，种子 {seed}，"
                  f"参考 {len(ref_images)}图/{len(ref_videos)}视频/{len(ref_audios)}音频）")
            try:
                # 始终 ref2va 编码（对齐 1146）；音频素材全部作为独立参考音频（官方 ref_audio_N 顺序 = 说话顺序）
                positive, latent = _encode_refs(
                    "管理器", f"分段 {i + 1}/{count} ",
                    clip, vae, audio_vae, h3, width, height, _seg_len_for(raw, duration_lo),
                    ref_images, ref_videos, ref_audios, ref_names, ref_scale=scale_factor, model=model)
                positive = _apply_semantic_bridge(positive, manager, tag="管理器")

                samples = _sample_first_pass(model, positive, latent, sample_decode, seed, vae, tag="管理器")
                _second = (sample_decode or {}).get("second") or {}
                if _second.get("enabled") and not _selflift_mode(sample_decode):
                    if (_second.get("sigmas_mode") or "scheduler") == "custom":
                        print(f"[JZL-管理器] 分段 {i + 1}/{count} 二次采样：latent 放大 {upscale_scale}x（自定义 Sigmas，{_second.get('sampler', 'euler')}）…")
                    else:
                        print(f"[JZL-管理器] 分段 {i + 1}/{count} 二次采样：latent 放大 {upscale_scale}x（{_second.get('sampler', 'euler')}/{_second.get('scheduler', 'simple')}，{_second.get('steps', 3)}步，denoise {_second.get('denoise', 0.3)}）…")
                    samples = _run_second_sampling(model, positive, samples, sample_decode, _second, upscale_scale, seed, tag="管理器")
                image, audio = _decode_av(vae, audio_vae, samples, sample_decode, tag="管理器")

                JZL_BUS_POOL[i] = {"image": image, "audio": audio}
                # 分段视频自动保存：每段解码完立即用 ffmpeg 落盘 mp4（不依赖下游 VHS）
                _saved_msg = ""
                if auto_save and auto_save_path:
                    try:
                        _wav_keep = None
                        if _merge_wavs is not None and audio is not None:
                            _wav_keep = os.path.join(
                                auto_save_path, f"._jzl_seg{i + 1}_{_seg_start + i}_{os.getpid()}.wav")
                            _merge_wavs.append(_wav_keep)
                        _sp = _save_segment_mp4(image, audio, auto_save_path, i + 1,
                                                story_name=_safe_story, counter=_seg_start + i, batch=_batch,
                                                keep_wav=_wav_keep)
                        saved_segments.append(_sp)
                        _saved_msg = f" | 已保存 {_sp}"
                    except Exception as _se:
                        errors.append(f"第{i + 1}段自动保存失败：{_se}")
                _frames = int(image.shape[0]) if image is not None else 0
                print(f"[JZL-管理器] 分段 {i + 1}/{count} 完成：{_frames} 帧 + "
                      f"音频{'✓' if audio is not None else '✗'}{_saved_msg}")
                bus_items.append({
                    "index": i, "mode": mode, "prompt": h3,
                    "has_image": True, "has_audio": audio is not None,
                    "frames": _frames,
                })
            except Exception as e:
                err = str(e)
                _tb_txt = ""
                try:
                    import traceback as _tb
                    _tb_txt = _tb.format_exc()
                except Exception:
                    pass
                _seg_fail_context(i + 1, _tb_txt)
                if "no kernel image" in err:
                    err += (f"\n  [JZL提示] CUDA/ROCm 内核与显卡架构不匹配（no kernel image）：当前 torch 没有该 GPU 的编译内核。"
                            f"诊断：{_gpu_diag_hint()}\n"
                            f"  请确认云机显卡型号与 torch/CUDA 版本匹配（本地 Z 盘 torch 是 ROCm 版 2.9.1+rocm7.13 + AMD 核显，"
                            f"无法用于其他架构显卡生成）。")
                elif "CUDA error" in err or "cudaError" in err or "out of memory" in err:
                    err += (f"\n  [JZL提示] CUDA 运行时错误，诊断：{_gpu_diag_hint()}\n"
                            f"  可能原因：显存不足、驱动/算子在 Blackwell(sm120) 不兼容、dtype/精度、"
                            f"或某算子非法参数（如 attention 后端/参考张量）。")
                _oom_hint = _oom_diag_hint(err, sample_decode, width, height,
                                       _seg_len_for(raw, duration_lo))
                if _oom_hint:
                    err += _oom_hint
                _attn_hint = _attention_diag_hint(err)
                if _attn_hint:
                    err += _attn_hint
                errors.append(f"第{i + 1}段生成失败：{err}")
                _log_seg_error("管理器", i + 1, err, _tb_txt)
                bus_items.append({
                    "index": i, "mode": mode, "prompt": h3,
                    "has_image": False, "has_audio": False,
                    "error": f"生成失败：{err}",
                })

        # 采样种子回写：randomize 时把实际使用的种子（第 1 段）回传前端 → 刷新后显示真实种子
        if seed_mode == "randomize" and first_seed is not None:
            _samp_ui = {"seed_update_sample": [{"seed": str(int(first_seed)), "seed_control": seed_mode}]}
            print(f"[JZL-管理器] 采样种子（本轮第 1 段）={int(first_seed)} → 已回传前端显示")
            if _seed_ui:
                _seed_ui = dict(_seed_ui)
                _seed_ui.update(_samp_ui)
            else:
                _seed_ui = _samp_ui

        # 生成结束：按「解码前清理」配置执行最终显存清理（卸载显存模型时释放）
        if (sample_decode or {}).get("decode_cleanup") == "卸载显存模型":
            _cleanup_vram("卸载显存模型", tag="管理器")

        # 分段视频自动合并：把 ffmpeg 落盘的各段 mp4 按顺序拼接为完整一个视频（无论保存模式）。
        # 未开「自动保存」时内部临时落盘再合并，合并后清理临时分段文件。
        if auto_merge and auto_merge_path:
            _segs = list(saved_segments)
            _merge_dir = None
            if not _segs:
                _merge_dir = os.path.join(tempfile.gettempdir(), f"_jzl_merge_{int(time.time())}")
                os.makedirs(_merge_dir, exist_ok=True)
                for i in range(count):
                    _item = JZL_BUS_POOL.get(i)
                    if _item and _item.get("image") is not None:
                        _wav_keep = None
                        if _merge_wavs is not None and _item.get("audio") is not None:
                            _wav_keep = os.path.join(_merge_dir, f"._jzl_seg{i + 1}_{os.getpid()}.wav")
                            _merge_wavs.append(_wav_keep)
                        try:
                            _segs.append(_save_segment_mp4(_item["image"], _item.get("audio"), _merge_dir, i + 1,
                                                           batch=_batch, keep_wav=_wav_keep))
                        except Exception as _me:
                            errors.append(f"第{i + 1}段合并前临时落盘失败：{_me}")
            if _segs:
                try:
                    _merge_out = os.path.join(
                        auto_merge_path,
                        f"{_safe_story}_{_batch}次生成分段合并_{_next_counter(auto_merge_path, f'{_safe_story}_{_batch}次生成分段合并'):05d}.mp4")
                    _merged = _merge_mp4_concat(_segs, _merge_out, audio_wavs=_merge_wavs)
                    print(f"[JZL-管理器] 分段已合并：{_merged}")
                    if auto_merge_delete:
                        _del_n = 0
                        for _p in saved_segments:
                            try:
                                os.remove(_p)
                                _del_n += 1
                            except Exception:
                                pass
                        print(f"[JZL-管理器] 已删除 {_del_n} 个分段视频（仅保留合并结果）")
                except Exception as _me2:
                    errors.append(f"合并失败：{_me2}")
            else:
                errors.append("合并失败：没有可合并的分段文件")
            if _merge_wavs:   # 先清临时无损 WAV（_merge_dir 内的随 rmtree 一起清）
                for _w in _merge_wavs:
                    try:
                        if os.path.exists(_w):
                            os.remove(_w)
                    except Exception:
                        pass
            if _merge_dir:
                shutil.rmtree(_merge_dir, ignore_errors=True)

        # ── 输出：图像 + 音频（is_output_list=True，恒为列表） ──
        images, audios = [], []
        for i in range(count):
            item = JZL_BUS_POOL.get(i)
            if item:
                images.append(item.get("image"))
                audios.append(item.get("audio"))

        if errors:
            for e in errors:
                print(f"[JZL-管理器] {e}")

        if save_mode == "拼接保存":
            image = _concat_images(images)
            audio = _concat_audios(audios)
            if image is None:
                image = _empty_image()
            if audio is None:
                audio = _empty_audio()
            return io.NodeOutput([image], [audio], prompt_input, ui=_seed_ui)

        # 分段保存：按顺序输出每段（失败的段跳过，错误见日志）
        if not images:
            # 无任何有效生成（未接模型/CLIP/VAE 或全部分段失败）→ 阻止下游，避免空列表让下游 slice_dict 崩溃
            print("[JZL-管理器] ⚠️ 本段未落盘任何视频（未连接模型/CLIP/VAE 或全部分段失败）")
            _report_save_failure(
                "管理器", _out_root, auto_save_path, saved_segments, errors,
                [f"已连接：model={model is not None} CLIP={clip is not None} VAE={vae is not None} "
                 f"audio_vae={audio_vae is not None}",
                 f"计划段数={count} 实际分段块={len(shots)} 运行模式={run_mode}",
                 f"保存模式={save_mode} 自动保存={auto_save}"])
            return io.NodeOutput([None], [None], prompt_input,
                                 block_execution="无有效生成结果（未连接模型/CLIP/VAE，或所有分段生成失败），已阻止下游执行",
                                 ui=_seed_ui)
        return io.NodeOutput(images, audios, prompt_input, ui=_seed_ui)


class JZL_MiniMaxAssetManagerMax(io.ComfyNode):
    """JZL - 🤖 MiniMax-H3短剧导演台Max — 复刻「8888-一键短剧工作流V2」逐段独立生成链路。

    与 Pro / Mini 共享全部 sheding 设定文档与风格配置；差异在生成与保存：
    - **复刻 8888 流程**：LLM 拆解/增强出完整剧本 → 逐段（该段提示词→参考调度→ref2va 编码
      → 第一次采样 → video latent 放大 → 第二次采样 → 解码）→ **每段跑完立即 ffmpeg 落盘
      mp4，随即释放该段显存/内存** → 再跑下一段，直到全部跑完。**跑 N 段与跑 1 段硬件占用
      不叠加**（不再把解码 tensor 累积在内存、最后一次性输出/保存 → 不再 OOM）。
    - **拼接保存**：全部段落盘完成后，最后读盘本次生成的各段 mp4，按顺序 concat 拼成一个视频。
    - **生成视频管理**：新增「🎬 生成视频管理」面板，查看 {故事名} 文件夹下全部视频（多宫格
      缩略图 + 点击预览）。
    """

    @classmethod
    def define_schema(cls):
        story_styles = _story_style_options()
        return io.Schema(
            node_id="JZL_MiniMaxAssetManagerMax",
            display_name="JZL - 🤖 MiniMax-H3短剧导演台Max",
            category="JZL/MiniMax",
            description="MiniMax-H3 生成管理器 Max：复刻 8888 一键短剧流程，逐段完整链路 + 每段即时落盘 + 内存释放（跑 100 段与跑 1 段占用不叠加），支持生成视频管理与读盘拼接。",
            inputs=[
                io.Combo.Input("run_mode", options=RUN_MODE_EMOJI,
                    default=RUN_MODE_EMOJI[0], display_name="运行模式",
                    tooltip="故事拆解模式=按情节把故事拆解为N段（不创意扩展）；故事扩写模式=只按风格+字数把故事扩写为丰满正文（不拆解，纯文本经「已处理剧本」输出）；穿透生成模式=跳过LLM拆解与增强，直接用提示词生成（含[SHOT_START]块则逐段，否则单段）；仅提示词输出=拆解+增强的完整分段剧本文本经「已处理剧本」输出，不生成视频"),
                io.String.Input("display_info", display_name="生成详情", default="分辨率：832x480丨每段帧数：192丨共计段数：6丨总帧数：1152丨总时长：48秒",
                    multiline=False, socketless=True,
                    tooltip="只读显示：当前画幅/MP/时长/段数计算出的分辨率、每段帧数、段数、总帧数、总时长（对齐倍数固定 32）"),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIO_OPTIONS, default="16:9 (Widescreen)",
                    display_name="画幅比例", tooltip="画幅比例（分辨率按 MP×1024² 公式自动计算，对齐倍数固定 32）"),
                io.Float.Input("megapixels", display_name="百万像素（MP）", default=0.4, min=0.1, max=16.0, step=0.1,
                    tooltip="总像素数（MP），画幅×MP 决定分辨率"),
                io.Combo.Input("duration", options=list(SEGMENT_DURATION_CHOICES), default=SEGMENT_DURATION_DEFAULT,
                    display_name="每段视频时长", tooltip="每段视频时长（秒）：固定 4~15 秒 或 区间（区间时 LLM 按剧情弧线给每段实际秒数）；等同「剧本与镜头处理器」的每段视频时长"),
                io.Float.Input("scale_factor", display_name="参考数值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="参考图放大系数"),
                io.Float.Input("upscale_scale", display_name="二采latent放大", default=1.0, min=1.0, max=4.0, step=0.05,
                    tooltip="二采（Ref2va）放大倍数"),
                io.Combo.Input("video_count", options=list(VIDEO_COUNT_CHOICES), default=VIDEO_COUNT_DEFAULT,
                    display_name="生成视频数量", tooltip="生成视频数量：默认「剧情决定」= LLM 按剧情时间线长度 ÷ 每段时长自动定段数（优先用足每段时长、防短剧情生成超多段）；也可固定 1/2/3/4/6/9/12/16/20/26/32/40/48/56 段。逐段即时落盘，段数再多也不叠加内存。🪄 穿透生成模式下忽略此设定：一律按提示词实际 [SHOT_START] 段数生成（上限 60 段）"),
                io.Combo.Input("story_style", options=story_styles, default=story_styles[0],
                    display_name="故事风格", tooltip="故事风格（剧本处理器按此风格拆解与润色）"),
                io.String.Input("story_name", display_name="故事名称", default="机智罗",
                    tooltip="故事名称（用于保存命名 / 落盘目录 output/jzl/{故事名} / 生成视频管理）"),
                io.String.Input("external_prompt", display_name="提示词·接线（可连上游文本）", default="",
                    tooltip="提示词接线输入（与 CLIP Text Encode 同类）：此输入框左上角圆点可拖线连接上游 STRING 文本节点；连线后以上游文本为提示词（优先于「节点内提示词」大框；未接且留空则用大框内容）"),
                io.Model.Input("model", display_name="主模型", optional=True),
                io.Clip.Input("clip", display_name="CLIP", optional=True),
                io.Vae.Input("vae", display_name="视觉VAE", optional=True),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True),
                io.String.Input("internal_prompt", display_name="节点内提示词", multiline=True,
                    socketless=True,
                    tooltip="节点内编辑的提示词（提示词来源，随工作流保存）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True,
                    socketless=True, default="",
                    tooltip="本节点独立保存的完整配置 JSON（资产/增强/采样解码/保存），随工作流保存，节点间互不影响"),
            ],
            outputs=[
                io.String.Output("script", display_name="已处理剧本",
                    tooltip="全部 LLM 处理后的剧本/提示词文本（供下游展示/保存用；「生成视频查看器」不再接收该端口，改直接读盘）"),
            ],
            is_output_node=True,  # Max 自带逐段落盘+读盘拼接，单节点即可运行；folder 输出已删（查看器直接读盘）
        )

    @classmethod
    def fingerprint_inputs(cls, manager_settings="", **kwargs):
        try:
            cfg = _parse_node_manager_settings(manager_settings)
            enhance = cfg.get("enhance") or {}
            sample_decode = cfg.get("sample_decode") or {}
            llm_random = (enhance.get("seed_control") or "randomize") == "randomize"
            samp_random = (sample_decode.get("seed_mode") or "randomize") == "randomize"
            if llm_random or samp_random:
                return f"random@{time.time_ns()}"
            gen_keys = ["run_mode", "video_count", "story_style", "duration",
                        "aspect_ratio", "megapixels", "scale_factor", "upscale_scale",
                        "internal_prompt", "external_prompt"]
            gen_sig = {k: kwargs.get(k) for k in gen_keys}
            return f"{cfg}|{gen_sig}"
        except Exception:
            return "no-manager"

    @classmethod
    def execute(cls, run_mode="拆解故事模式", video_count=VIDEO_COUNT_DEFAULT, aspect_ratio="16:9 (Widescreen)",
                megapixels=1.0, duration="5-5", scale_factor=1.0, upscale_scale=1.5, display_info="",
                story_style="热血战斗", story_name="",
                external_prompt=None,
                internal_prompt=None, manager_settings="",
                clip=None, vae=None, audio_vae=None, model=None) -> io.NodeOutput:
        _vc_auto = _parse_video_count(video_count)[0] == "auto"   # 「剧情决定」标记（manager 建后写入）
        video_count = _vc_count(video_count)     # 「剧情决定」→ 占位 6（拆解后按实际分段数覆盖）
        """复刻 8888：LLM 拆解 → 逐段完整链路（调度+编码+一采+latent放大+二采+解码）→
        每段即时 ffmpeg 落盘 → 释放该段显存/内存 → 下一段。跑 N 段与跑 1 段硬件占用不叠加。"""
        run_mode = _normalize_run_mode(run_mode)
        pure_prompt = run_mode == "仅提示词输出"
        expand_mode = run_mode == "故事扩写模式"
        passthrough = run_mode == "穿透生成模式"
        # 拆解类（故事拆解/仅提示词输出）统一 Decompose；故事扩写模式不走拆解
        story_mode = "拆解模式 (Decompose)"

        story_name = (story_name or "").strip()
        if not story_name:
            print("[JZL-Max] ⚠️ 必须填写「故事名称」才能生成（已终止）")
            return io.NodeOutput("", block_execution="必须填写「故事名称」后才能生成",
                                  ui={"manager_error": ["必须填写「故事名称」后才能生成"]})

        manager = _parse_node_manager_settings(manager_settings)
        manager["_jzl_auto_count"] = _vc_auto          # 「剧情决定」标记（供 _run_script_processor/日志用）
        duration_lo, duration_hi, duration_desc = _parse_duration_spec(duration)
        enhance = manager.get("enhance") or {}
        assets_cfg = manager.get("assets") or {}
        sample_decode = manager.get("sample_decode") or {}
        # 保存配置：mode = 分段保存(每段一个 mp4) / 拼接保存(全部段落盘后读盘 concat 成一个)
        _save_cfg = manager.get("save") or {}
        save_mode = (_save_cfg.get("mode") or "分段保存").strip() or "分段保存"
        auto_merge_delete = bool(_save_cfg.get("auto_merge_delete"))  # 「拼接保存后删除分段视频」（仅拼接保存时生效）
        prompt_lang = (enhance.get("prompt_lang") or "中文 [ZH]").strip() or "中文 [ZH]"

        if not isinstance(internal_prompt, str):
            internal_prompt = ""
        prompt_input = (internal_prompt or "").strip()
        if isinstance(external_prompt, str) and external_prompt.strip():
            prompt_input = external_prompt.strip()  # 接线提示词优先：外部文本覆盖节点内提示词

        seed_control = (enhance.get("seed_control") or "randomize").strip() or "randomize"
        current_seed = int(enhance.get("seed", 0) or 0)
        if seed_control == "randomize":
            llm_seed = int(torch.randint(0, 0x7fffffffffffffff, (1,)).item())
        else:
            llm_seed = current_seed
        # 穿透生成：段数按提示词实际分段数定（横幅与实际生成段数保持一致）
        _pt_seg = _pt_seg_count(prompt_input) if passthrough else 0
        _seg_lbl = f"{_pt_seg}段" if _pt_seg else _seg_label(manager.get("_jzl_auto_count"), _vc_count(video_count))
        _run_banner("Max", run_mode, _seg_lbl, story_style, story_name, enhance,
                    f" | 扩写≈{int(enhance.get('expand_words') or 500)}字" if expand_mode else "")

        # 故事扩写模式：只按「故事风格 + 扩写字数」扩写正文，不拆解；纯文本输出，不生成视频
        if expand_mode:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput("")
            _ew = int(enhance.get("expand_words") or 500)
            _esrc = str(enhance.get("expand_source") or "素材")
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            _xri, _xrv, _xra, _ = _build_asset_intro(assets_cfg)
            expanded, x_err = _run_story_expander(
                prompt_input, manager, story_style, story_name, prompt_lang, llm_seed,
                expand_words=_ew, expand_source=_esrc,
                ref_image_intro=_xri, ref_video_intro=_xrv, ref_audio_intro=_xra,
                gen_dir=gen_dir)
            if x_err:
                print(f"[JZL-Max] LLM/API 出错，终止：{x_err}")
                return io.NodeOutput("", block_execution=f"⚠️ LLM/API 出错，已终止：{x_err}")
            if expanded:
                _save_last_script(story_name, expanded)
            _llm_finish(enhance)
            _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
            return io.NodeOutput(expanded, ui=_seed_ui)

        # 仅提示词输出：拆解 + 增强（不含扩写），只 LLM 处理，不生成视频
        if pure_prompt:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput("")
            _pcount = _vc_count(video_count)
            ri, rv, ra, _ = _build_asset_intro(assets_cfg)
            _pe, _pp, _pv, _pa = _detect_enables(prompt_input, assets_cfg)
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            processed, p_err = _run_script_processor(
                prompt_input, manager, _pcount, story_style, story_name, duration, prompt_lang, llm_seed,
                ref_image_intro=ri, ref_video_intro=rv, ref_audio_intro=ra,
                enable_scene=_pe, enable_props=_pp, enable_video=_pv, enable_audio=_pa,
                mode="拆解模式 (Decompose)", gen_dir=gen_dir)
            if p_err:
                # LLM/API 出错必须终止
                print(f"[JZL-Max] LLM/API 出错，终止：{p_err}")
                return io.NodeOutput("", block_execution=f"⚠️ LLM/API 出错，已终止：{p_err}")
            if enhance.get("enabled", False):
                processed, p_err2 = _run_prompt_enhancer(processed, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir)
                if p_err2:
                    print(f"[JZL-Max] LLM/API 出错，终止：{p_err2}")
                    return io.NodeOutput("", block_execution=f"⚠️ LLM/API 出错，已终止：{p_err2}")
            if processed:
                _save_last_script(story_name, processed)
            _llm_finish(enhance)
            _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
            return io.NodeOutput(processed, ui=_seed_ui)

        # 清空重建池，避免旧资产残留
        JZL_ASSET_POOL.clear()
        JZL_SLOT_MAP.clear()
        manifest, errors = _load_assets_into_pool(assets_cfg)

        width, height = _resolve_gen_size(aspect_ratio, megapixels)
        length = _resolve_length(duration_hi)
        count = video_count

        ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset = _build_asset_intro(assets_cfg)
        JZL_SLOT_MAP.update(slot_to_asset)
        enable_scene, enable_props, enable_video, enable_audio = _detect_enables(prompt_input, assets_cfg)

        # 故事拆解 / 故事扩写：输入必须是自然语言故事；含六段式标签（已拆解提示词）→ 报错终止
        if run_mode in ("故事拆解模式", "故事扩写模式"):
            _nl_err = _check_natural_language_input(prompt_input)
            if _nl_err:
                print(f"[JZL-Max] ❌ {_nl_err}（已终止）")
                return io.NodeOutput(prompt_input or "", block_execution=f"❌ {_nl_err}",
                                     ui={"manager_error": [_nl_err]})
        has_shots = bool(re.search(r'\[SHOT_START\]', prompt_input or ""))
        gen_dir = None
        if not passthrough and enhance.get("story_decompose", True) and not has_shots and prompt_input:
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_script_processor(
                prompt_input, manager, count, story_style, story_name, duration, prompt_lang, llm_seed,
                ref_image_intro=ref_image_intro, ref_video_intro=ref_video_intro, ref_audio_intro=ref_audio_intro,
                enable_scene=enable_scene, enable_props=enable_props,
                enable_video=enable_video, enable_audio=enable_audio, mode=story_mode, gen_dir=gen_dir)
            if err:
                # LLM/API 出错必须终止（不再用错误提示词继续生成）
                print(f"[JZL-Max] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput(prompt_input or "", block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        if not passthrough and enhance.get("enabled", False):
            if gen_dir is None:
                from .nodes_llama import _next_generation_dir
                gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_prompt_enhancer(prompt_input, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir)
            if err:
                # LLM/API 出错必须终止
                print(f"[JZL-Max] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput(prompt_input or "", block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        # 穿透生成模式：必须使用「拆解模式」拆解好的提示词；格式不对直接终止；段数自动对齐提示词实际分段数
        if passthrough:
            _pt_err, _pt_n = _check_decomposed_script(prompt_input, video_count,
                                                      auto_count=bool(manager.get("_jzl_auto_count")), tag="Max")
            if _pt_err:
                print(f"[JZL-Max] ❌ {_pt_err}（已终止）")
                return io.NodeOutput(prompt_input or "", block_execution=f"❌ {_pt_err}",
                                     ui={"manager_error": [_pt_err]})
            count = _pt_n

        prompt_input = _normalize_dispatch_slots(prompt_input, slot_to_asset)
        prompt_input = _normalize_scene_slots(prompt_input, slot_to_asset)
        prompt_input = _prune_fantasy_assets(prompt_input, slot_to_asset)

        if not passthrough and prompt_input:
            _save_last_script(story_name, prompt_input)

        _llm_finish(enhance)
        _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)

        # ── 分段：按 [SHOT_START] 块切分（等同 8888 里「列表分发按编号」）──
        shots = re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', prompt_input or "", re.DOTALL)
        if manager.get("_jzl_auto_count") and shots:
            count = max(1, min(SEGMENT_COUNT_MAX, len(shots)))   # 「剧情决定」：以实际拆解出的分段数为准
        base_seed = int(sample_decode.get("seed", 0) or 0)
        seed_mode = sample_decode.get("seed_mode", "randomize") or "randomize"

        saved_files = []          # 本次运行逐段落盘成功的 mp4 绝对路径（只存路径字符串，不存 tensor）
        _JZL_SEG_TRACEBACKS.clear()   # 本次运行的分段失败调用栈（诊断报告用）
        _JZL_SEG_ERR_SEEN.clear()     # 每轮重置去重表：否则本轮第 1 段失败会被上一轮抑制掉调用栈
        _out_root = os.path.abspath(folder_paths.get_output_directory())
        _jzl_dir = os.path.join(_out_root, "jzl")
        _safe_story = _safe_story_name(story_name)
        # Max 恒定落盘目录：output/jzl/{故事名}/（生成视频管理读同一目录）
        out_video_dir = os.path.join(_jzl_dir, _safe_story)
        try:
            os.makedirs(out_video_dir, exist_ok=True)
        except Exception as _me:
            errors.append(f"无法创建输出目录：{out_video_dir}（{_me}）")
        _batch = _next_batch(out_video_dir, _safe_story)   # 本次生成次数（第 N 次生成，文件名批次号）
        _seg_start = _next_counter(out_video_dir, f"{_safe_story}_{_batch}次生成分段")
        first_seed = None
        # 拼接保存时同时收集各段无損 WAV → 合并时「音频统一编码一次」（消除段边界 AAC encoder delay 误差）
        _merge_wavs = [] if save_mode == "拼接保存" else None
        for i in range(count):
            # 释放上一段残留（进入下一段前再清一次缓存碎片，保证占用不随段数上涨）
            try:
                _clean_cache()
            except Exception:
                pass
            raw = shots[i].strip() if i < len(shots) else ""
            h3, scene, vid, aud = _parse_four_in_one(raw)
            if not h3:
                h3 = raw.strip() if raw.strip() else (
                    prompt_input.strip() if i == 0 and prompt_input else "[未找到H3提示词]")

            # @ 引用 + 参考提取（复刻 8888：按每段调度指令从资产池按名取参考元素）
            mention_names, h3 = _extract_mentions(h3)
            ref_images, ref_videos, ref_audios, ref_names = _collect_refs_with_names(scene, vid, aud, mention_names)

            mode = "纯文本生成音视频-T2VA"
            if ref_videos or ref_images or ref_audios:
                mode = "多参考生成音视频-REF2VA"
            can_generate = clip is not None and vae is not None and model is not None
            if (ref_videos or ref_audios) and audio_vae is None:
                can_generate = False
            seed = _resolve_seed(seed_mode, base_seed, i)
            if first_seed is None:
                first_seed = seed
            if not can_generate:
                _need = "model/CLIP/VAE"
                if (ref_videos or ref_audios) and audio_vae is None:
                    _need = "model/CLIP/VAE/audio_vae（本段含视频或音频参考）"
                errors.append(f"第{i + 1}段跳过：未连接 {_need}，仅完成剧本分段（未生成视频）")
                continue

            print(f"[JZL-Max] 分段 {i + 1}/{count} 生成中（{mode}，种子 {seed}，"
                  f"参考 {len(ref_images)}图/{len(ref_videos)}视频/{len(ref_audios)}音频）")
            try:
                # 编码（ref2va，复刻 8888 ReferenceToVideo2）
                positive, latent = _encode_refs(
                    "Max", f"分段 {i + 1}/{count} ",
                    clip, vae, audio_vae, h3, width, height, _seg_len_for(raw, duration_lo),
                    ref_images, ref_videos, ref_audios, ref_names, ref_scale=scale_factor, model=model)
                positive = _apply_semantic_bridge(positive, manager, tag="Max")
                # 第一次采样（按「采样分类」分派：常规采样 / SelfLift采样）
                samples = _sample_first_pass(model, positive, latent, sample_decode, seed, vae, tag="Max")
                # latent 放大 + 第二次采样（可选，复刻 8888 一采→LTXV分离→Upscaler3D→合并→二采）
                _second = (sample_decode or {}).get("second") or {}
                if _second.get("enabled") and not _selflift_mode(sample_decode):
                    print(f"[JZL-Max] 分段 {i + 1}/{count} 二次采样：latent 放大 {upscale_scale}x（{_second.get('sampler', 'euler')}/{_second.get('scheduler', 'simple')}，{_second.get('steps', 3)}步，denoise {_second.get('denoise', 0.3)}）…")
                    samples = _run_second_sampling(model, positive, samples, sample_decode, _second, upscale_scale, seed, tag="Max")
                image, audio = _decode_av(vae, audio_vae, samples, sample_decode, tag="Max")

                # ★ Max 恒定：每段解码完立即 ffmpeg 落盘（复刻 8888 里 VHS 每分镜独立落盘）
                #   拼接保存时额外保留一份无損 WAV，供最后音频统一编码一次
                _wav_keep = None
                if _merge_wavs is not None and audio is not None:
                    _wav_keep = os.path.join(
                        out_video_dir, f"._jzl_seg{i + 1}_{_seg_start + i}_{os.getpid()}.wav")
                    _merge_wavs.append(_wav_keep)
                _sp = _save_segment_mp4(image, audio, out_video_dir, i + 1,
                                        story_name=_safe_story, counter=_seg_start + i, batch=_batch,
                                        keep_wav=_wav_keep)
                saved_files.append(_sp)
                _frames = int(image.shape[0]) if image is not None else 0
                print(f"[JZL-Max] 分段 {i + 1}/{count} 已落盘：{_sp}（{_frames} 帧）")

                # ★ Max 恒定：释放该段全部显存/内存（解码帧/音频/latent/条件全部丢弃）→ 跑下段占用不叠加
                try:
                    del image, audio, samples, positive, latent
                except Exception:
                    pass
                _clean_cache()
            except Exception as e:
                err = str(e)
                _tb_txt = ""
                try:
                    import traceback as _tb
                    _tb_txt = _tb.format_exc()
                except Exception:
                    pass
                _seg_fail_context(i + 1, _tb_txt)
                if "no kernel image" in err:
                    err += (f"\n  [JZL提示] CUDA/ROCm 内核与显卡架构不匹配（no kernel image）：当前 torch 没有该 GPU 的编译内核。"
                            f"诊断：{_gpu_diag_hint()}\n"
                            f"  请确认云机显卡型号与 torch/CUDA 版本匹配。")
                elif "CUDA error" in err or "cudaError" in err or "out of memory" in err:
                    err += (f"\n  [JZL提示] CUDA 运行时错误，诊断：{_gpu_diag_hint()}\n"
                            f"  可能原因：显存不足、驱动/算子在 Blackwell(sm120) 不兼容、dtype/精度、"
                            f"或某算子非法参数（如 attention 后端/参考张量）。")
                _oom_hint = _oom_diag_hint(err, sample_decode, width, height,
                                       _seg_len_for(raw, duration_lo))
                if _oom_hint:
                    err += _oom_hint
                _attn_hint = _attention_diag_hint(err)
                if _attn_hint:
                    err += _attn_hint
                errors.append(f"第{i + 1}段生成失败：{err}")
                _log_seg_error("Max", i + 1, err, _tb_txt)

        # 采样种子回写：randomize 时把第 1 段实际种子回传前端
        if seed_mode == "randomize" and first_seed is not None:
            _samp_ui = {"seed_update_sample": [{"seed": str(int(first_seed)), "seed_control": seed_mode}]}
            print(f"[JZL-Max] 采样种子（本轮第 1 段）={int(first_seed)} → 已回传前端显示")
            if _seed_ui:
                _seed_ui = dict(_seed_ui)
                _seed_ui.update(_samp_ui)
            else:
                _seed_ui = _samp_ui

        # 生成结束：按「解码前清理」配置执行最终显存清理
        if (sample_decode or {}).get("decode_cleanup") == "卸载显存模型":
            _cleanup_vram("卸载显存模型", tag="Max")

        # ── 保存模式：
        #  分段保存：每段已即时落盘 → video_paths = saved_files（生成视频管理可逐个查看）
        #  拼接保存：全部段落盘完成后，最后读盘本次生成的各段 mp4，按顺序 concat 成一个完整视频
        out_files = list(saved_files)
        if save_mode == "拼接保存" and saved_files:
            try:
                _merge_out = os.path.join(
                    out_video_dir,
                    f"{_safe_story}_{_batch}次生成分段合并_{_next_counter(out_video_dir, f'{_safe_story}_{_batch}次生成分段合并'):05d}.mp4")
                _merged = _merge_mp4_concat(saved_files, _merge_out, audio_wavs=_merge_wavs)
                print(f"[JZL-Max] 拼接保存完成（{len(saved_files)} 段按顺序合并）：{_merged}")
                if auto_merge_delete:
                    _del_cnt = 0
                    for _sp in saved_files:
                        try:
                            if os.path.exists(_sp):
                                os.remove(_sp); _del_cnt += 1
                        except Exception:
                            pass
                    print(f"[JZL-Max] 已按配置删除 {_del_cnt} 个分段视频（仅保留合并结果）")
                out_files = [_merged]
            except Exception as _me:
                errors.append(f"拼接保存失败：{_me}（已保留各分段视频）")

        # 清理临时无損 WAV（已合入成片或不再需要）
        if _merge_wavs:
            for _w in _merge_wavs:
                try:
                    if os.path.exists(_w):
                        os.remove(_w)
                except Exception:
                    pass

        if errors:
            for e in errors:
                print(f"[JZL-Max] {e}")

        # Max 0 输出：视频已即时落盘到 output/jzl/{故事名}/；剧本已写「最近提示词.json」+ 第N次生成/故事拆解/
        # （「生成视频查看器」改直接读盘获取故事名/剧本/视频，不再经端口传递）
        print(f"[JZL-Max] 本次落盘：{out_video_dir}（视频 {len(saved_files)} 个）")
        if not saved_files:
            print("[JZL-Max] ⚠️ 本段未落盘任何视频（未连接模型/CLIP/VAE 或全部分段失败）")
            _report_save_failure(
                "Max", _out_root, out_video_dir, saved_files, errors,
                [f"已连接：model={model is not None} CLIP={clip is not None} VAE={vae is not None} "
                 f"audio_vae={audio_vae is not None}",
                 f"计划段数={count} 实际分段块={len(shots)} 运行模式={run_mode}",
                 f"保存模式={save_mode}"])

        return io.NodeOutput(prompt_input, ui=_seed_ui)


class JZL_MiniMaxAssetManagerInfinite(io.ComfyNode):
    """JZL - 🤖 MiniMax-H3无限时长 — 段间 latent 引导衔接的逐段生成长视频链路。

    与「短剧导演台Max」唯一差异在分段循环（LLM 拆解 / 资产 / 参数 / 逐段落盘 / 读盘拼接 / 显存释
    放全部沿用同一套）：
    · 每段仍按剧情拆解出的「新提示词」独立生成 → 剧情持续往前推进（不再是同一段提示词重复）；
    · 第 2 段起，把上一段**一采 latent 的尾部窗口**（默认 22 帧 = 官方 Motion Context 基线）作为
      keyframe 钉进本段时间轴起点（cond 行永不降噪、每步重注入）→ 画面 / 动作 / 色调跨段连续；
    · 段首被钉住的窗口帧解码后裁掉；音频同步裁掉 round(窗口×5/3) 步（否则音画错位 W/24 秒）；
    · 1 段时无上段 → 自动跳过衔接，行为与 Max 完全一致（单段 / 多段同一套逻辑）。
    数学保证：段长恒为 17k+5、窗口恒为 17m+5 ⇒ 段长−窗口 = 17(k−m) 必为 17 的倍数 ⇒ 尾窗起点必落
    在 latent token 边界（token 索引 5(k−m)）⇒ **相位自动对齐**，无需任何手工对齐参数。
    """

    @classmethod
    def define_schema(cls):
        story_styles = _story_style_options()
        return io.Schema(
            node_id="JZL_MiniMaxAssetManagerInfinite",
            display_name="JZL - 🤖 MiniMax-H3无限时长",
            category="JZL/MiniMax",
            description=("MiniMax-H3 无限时长：逐段「新剧情提示词」独立生成，并把上一段一采 latent 的尾部窗口"
                         "钉成本段首帧引导（永不降噪、每步重注入）→ 画面连续 + 剧情推进；段首被钉住的窗口帧"
                         "自动裁掉（音频同步裁剪）。逐段即时落盘 + 读盘 concat，跑 1 段与跑 N 段逻辑同一套、"
                         "硬件占用不叠加。"),
            inputs=[
                io.Combo.Input("run_mode", options=RUN_MODE_EMOJI,
                    default=RUN_MODE_EMOJI[0], display_name="运行模式",
                    tooltip="与「短剧导演台Max」一致：故事拆解模式=按情节拆解为N段（每段一个独立新提示词，无限时长按此逐段推进剧情）；故事扩写模式=只扩写正文（纯文本输出）；穿透生成模式=跳过 LLM 直接用提示词生成；仅提示词输出=只输出拆解+增强后的分段剧本，不生成视频"),
                io.String.Input("display_info", display_name="生成详情", default="分辨率：832x480丨每段帧数：192丨共计段数：6丨总帧数：1152丨总时长：48秒",
                    multiline=False, socketless=True,
                    tooltip="只读显示：当前画幅/MP/时长/段数计算出的分辨率、每段帧数、段数、总帧数、总时长（对齐倍数固定 32）"),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIO_OPTIONS, default="16:9 (Widescreen)",
                    display_name="画幅比例", tooltip="画幅比例（分辨率按 MP×1024² 公式自动计算，对齐倍数固定 32）"),
                io.Float.Input("megapixels", display_name="百万像素（MP）", default=0.4, min=0.1, max=16.0, step=0.1,
                    tooltip="总像素数（MP），画幅×MP 决定分辨率"),
                io.Combo.Input("duration", options=list(SEGMENT_DURATION_CHOICES), default=SEGMENT_DURATION_DEFAULT,
                    display_name="每段视频时长", tooltip="每段视频时长（秒）：固定 4~20 秒 或 区间。等同「剧本与镜头处理器」的每段视频时长。无限时长下每段会往后损失「衔接窗口」帧长（默认 22 帧≈0.92 秒，被下一段复用），总时长 = Σ段长 − 窗口×(段数−1)"),
                io.Float.Input("scale_factor", display_name="参考数值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="参考图放大系数"),
                io.Float.Input("upscale_scale", display_name="二采latent放大", default=1.0, min=1.0, max=4.0, step=0.05,
                    tooltip="二采（Ref2va）放大倍数。开启二采不影响段间衔接：衔接恒用「一采 latent」，二采结果只用于解码落盘"),
                io.Combo.Input("video_count", options=list(VIDEO_COUNT_CHOICES), default=VIDEO_COUNT_DEFAULT,
                    display_name="生成视频数量", tooltip="段数：默认「剧情决定」= LLM 按剧情时间线长度 ÷ 每段时长自动定段数；1 段=普通单段生成（不启用衔接）；≥2 段=无限时长链路。逐段即时落盘，段数再多也不叠加内存。🪄 穿透生成模式下忽略此设定：一律按提示词实际 [SHOT_START] 段数生成（上限 60 段）"),
                io.Combo.Input("story_style", options=story_styles, default=story_styles[0],
                    display_name="故事风格", tooltip="故事风格（剧本处理器按此风格拆解与润色）"),
                io.String.Input("story_name", display_name="故事名称", default="机智罗",
                    tooltip="故事名称（用于保存命名 / 落盘目录 output/jzl/{故事名} / 生成视频管理）"),
                io.String.Input("external_prompt", display_name="提示词·接线（可连上游文本）", default="",
                    tooltip="提示词接线输入（与 CLIP Text Encode 同类）：连线后以上游文本为提示词（优先于「节点内提示词」大框）"),
                io.Model.Input("model", display_name="主模型", optional=True),
                io.Clip.Input("clip", display_name="CLIP", optional=True),
                io.Vae.Input("vae", display_name="视觉VAE", optional=True),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True),
                io.String.Input("internal_prompt", display_name="节点内提示词", multiline=True,
                    socketless=True,
                    tooltip="节点内编辑的提示词（提示词来源，随工作流保存）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True,
                    socketless=True, default="",
                    tooltip="本节点独立保存的完整配置 JSON（资产/增强/采样解码/无限时长衔接/保存），随工作流保存，节点间互不影响"),
            ],
            outputs=[
                io.String.Output("script", display_name="已处理剧本",
                    tooltip="全部 LLM 处理后的剧本/提示词文本（供下游展示/保存用；「生成视频查看器」直接读盘）"),
            ],
            is_output_node=True,  # 自带逐段落盘+读盘拼接，单节点即可运行
        )

    @classmethod
    def fingerprint_inputs(cls, manager_settings="", **kwargs):
        try:
            cfg = _parse_node_manager_settings(manager_settings)
            enhance = cfg.get("enhance") or {}
            sample_decode = cfg.get("sample_decode") or {}
            llm_random = (enhance.get("seed_control") or "randomize") == "randomize"
            samp_random = (sample_decode.get("seed_mode") or "randomize") == "randomize"
            if llm_random or samp_random:
                return f"random@{time.time_ns()}"
            gen_keys = ["run_mode", "video_count", "story_style", "duration",
                        "aspect_ratio", "megapixels", "scale_factor", "upscale_scale",
                        "internal_prompt", "external_prompt"]
            gen_sig = {k: kwargs.get(k) for k in gen_keys}
            return f"{cfg}|{gen_sig}"
        except Exception:
            return "no-manager"

    @classmethod
    def execute(cls, run_mode="拆解故事模式", video_count=VIDEO_COUNT_DEFAULT, aspect_ratio="16:9 (Widescreen)",
                megapixels=1.0, duration="5-5", scale_factor=1.0, upscale_scale=1.5, display_info="",
                story_style="热血战斗", story_name="",
                external_prompt=None,
                internal_prompt=None, manager_settings="",
                clip=None, vae=None, audio_vae=None, model=None) -> io.NodeOutput:
        _vc_auto = _parse_video_count(video_count)[0] == "auto"   # 「剧情决定」标记（manager 建后写入）
        video_count = _vc_count(video_count)     # 「剧情决定」→ 占位 6（拆解后按实际分段数覆盖）
        """无限时长：LLM 拆解（每段新剧情）→ 逐段【上段衔接引导 + 编码 + 一采 + 二采 + 解码】→
        每段即时落盘 → 裁掉段首被钉住的窗口帧 → 释放该段显存 → 下一段。跑 N 段与跑 1 段占用不叠加。"""
        run_mode = _normalize_run_mode(run_mode)
        pure_prompt = run_mode == "仅提示词输出"
        expand_mode = run_mode == "故事扩写模式"
        passthrough = run_mode == "穿透生成模式"
        story_mode = "拆解模式 (Decompose)"

        story_name = (story_name or "").strip()
        if not story_name:
            print("[JZL-无限时长] ⚠️ 必须填写「故事名称」才能生成（已终止）")
            return io.NodeOutput("", block_execution="必须填写「故事名称」后才能生成",
                                  ui={"manager_error": ["必须填写「故事名称」后才能生成"]})

        manager = _parse_node_manager_settings(manager_settings)
        manager["_jzl_auto_count"] = _vc_auto          # 「剧情决定」标记（供 _run_script_processor/日志用）
        duration_lo, duration_hi, duration_desc = _parse_duration_spec(duration)
        enhance = manager.get("enhance") or {}
        assets_cfg = manager.get("assets") or {}
        sample_decode = manager.get("sample_decode") or {}
        # ♾️ 衔接设置（面板「🎭 采样解码设置 → 无限时长衔接⚠实验性功能」；缺省即官方 Motion Context 基线）
        _inf = sample_decode.get("infinite") or {}
        # 「无限时长衔接⚠实验性功能」总开关：关闭后每段独立生成（等价 Max 语义）
        _seam_on = _inf.get("enabled", True) is not False
        seam_window_cfg = _seam_resolve_window(_inf.get("window", SEAM_DEFAULT_WINDOW))
        seam_audio_link = _seam_flag(_inf.get("audio_link"), True)
        # 「音频同步裁剪」：默认**开启**（画面丢了衔接帧，音频必须同量裁掉，否则音画错位）。
        # 存储语义：`audio_no_trim: true` 才表示关闭（仅供对比实验）；历史上的 `audio_trim: false`
        # 视为误存（那时关掉会直接造成段尾画面冻结/音画错位）→ 一律按开启处理。
        seam_trim_audio = not _seam_flag(_inf.get("audio_no_trim"), False)
        # 裁切方式：pixel（默认）= latent 不裁 + 解码后丢帧（保 VAE 解码分组相位）；
        # latent = 旧行为（已证会闪烁，仅作 A/B 对照）；none = 不裁（诊断用，成片会多出衔接帧）
        _tm = str(_inf.get("trim_mode") or "")
        seam_trim_mode = "latent" if "latent" in _tm else ("none" if "不裁" in _tm else "pixel")
        # 过渡丢弃帧数（settling burn-in）：钉住窗口之外再多丢几帧，吸掉“模仿引导→启动新动作”的卡顿（hold→pop，官方默认 12）
        seam_settle_cfg_raw = _seam_settle_cfg(_inf.get("settle"))
        # ⭐ 「设定时长 = 落盘时长」的硬前提：丢弃量（窗口 + 过渡）必须是 **17 的倍数**。
        #    生成帧数恒为 17K+5、落盘目标恒为 17k+5 ⇒ 丢弃 ≡ 0 (mod 17) ⇒ 过渡 ≡ 12 (mod 17) ⇒ 12/29/46。
        #    这组值同时满足「latent 域裁切要求丢弃 token 是 5 的倍数」→ 两套约束天然一致，不会互相打架。
        seam_settle = _seam_snap_settle(seam_window_cfg, seam_settle_cfg_raw)
        if seam_settle != seam_settle_cfg_raw:
            print(f"[JZL-无限时长] 过渡丢弃 {seam_settle_cfg_raw} 帧 → 自动对齐为 {seam_settle} 帧"
                  f"（只有「窗口 {seam_window_cfg} + 过渡」是 17 的倍数时，落盘帧数才能精确 = 设定帧数）")
        # 引导来源：latent（默认）= 上段一采 latent 尾部切片直传；pixel = 官方 AddGuide 同款（尾帧→VAE重编码）
        seam_guide_source = "pixel" if "重编码" in str(_inf.get("guide_source") or "") else "latent"
        # 二采引导源：first（默认）= 上段一采 latent 尾部（插值对齐二采画布）；
        #   second = 上段**二采** latent 尾部（与二采画布天然同画布、同 refined 版本，更准；
        #   代价：CPU 常驻一份尾部切片，只留“配置窗口”所需 token，通常几~几十 MB）
        _sg2 = str(_inf.get("second_guide") or "")
        seam_second_guide = "second" if ("二采" in _sg2 or _sg2.strip().lower() == "second") else "first"
        # ⭐ 段首衔接跑道（秒）= (窗口 + 过渡) / 24：段 2+ 的**生成时长 = 设定时长 + 跑道**。
        #    段首这一段是「承接上一段末帧」的锁定画面 + 模型启动过渡，成片会整段裁掉 ⇒ 落盘时长精确 = 用户设定。
        #    单段（count=1）或诊断档「不裁切」时跑道 = 0（没有衔接就没有额外生成内容）。
        # 提示词来源必须先解析：下方 _seg_count_probe 在穿透模式下要用它统计分段数
        if not isinstance(internal_prompt, str):
            internal_prompt = ""
        prompt_input = (internal_prompt or "").strip()
        if isinstance(external_prompt, str) and external_prompt.strip():
            prompt_input = external_prompt.strip()  # 接线提示词优先
        _seg_count_probe = (_pt_seg_count(prompt_input) if passthrough else 0) or _vc_count(video_count)
        seam_runway_sec = _seam_runway_seconds(seam_window_cfg, seam_settle,
                                               enabled=(_seam_on and _seg_count_probe > 1 and seam_trim_mode != "none"))
        if seam_runway_sec > 0:
            print(f"[JZL-无限时长] 段首衔接跑道 {seam_runway_sec:.3f} 秒（窗口 {seam_window_cfg} + 过渡 {seam_settle} "
                  f"= {seam_window_cfg + seam_settle} 帧/24fps）→ 第 2 段起生成时长 = 设定时长 + 跑道，成片裁掉 ⇒ 落盘 = 设定时长")
        # ♾️ 接缝亮度匹配（只做亮度）：把每段开头若干帧的亮度朝上段末帧平滑过渡（加性、限幅）
        # 缺省 = 开（与官方 Director 一致：该做法是他们试错后**保留**的那一种，只动亮度不动 RGB）
        seam_luma_on = _seam_flag(_inf.get("luma_match"), True)
        _save_cfg = manager.get("save") or {}
        save_mode = (_save_cfg.get("mode") or "分段保存").strip() or "分段保存"
        auto_merge_delete = bool(_save_cfg.get("auto_merge_delete"))
        prompt_lang = (enhance.get("prompt_lang") or "中文 [ZH]").strip() or "中文 [ZH]"

        seed_control = (enhance.get("seed_control") or "randomize").strip() or "randomize"
        current_seed = int(enhance.get("seed", 0) or 0)
        if seed_control == "randomize":
            llm_seed = int(torch.randint(0, 0x7fffffffffffffff, (1,)).item())
        else:
            llm_seed = current_seed
        _trim_label = {"pixel": "像素域裁切（保解码相位）", "latent": "latent域裁切（旧·诊断）",
                       "none": "不裁切（诊断）"}.get(seam_trim_mode, seam_trim_mode)
        # 只在「非默认配置」时追加提示，默认档（像素域裁切 / 亮度匹配开 / 一采 latent 引导）不再逐项刷屏
        _banner_extra = ""
        if seam_trim_mode != "pixel":
            _banner_extra += f" | 裁切={_trim_label}"
        if seam_runway_sec > 0 and not seam_luma_on:
            _banner_extra += " | 亮度匹配=关"
        if not seam_audio_link:
            _banner_extra += " | 音频衔接=关"
        if not seam_trim_audio:
            _banner_extra += " | 音频同步裁剪=关"
        if seam_guide_source == "pixel":
            _banner_extra += " | 引导=上段尾帧→VAE重编码"
        if seam_second_guide == "second":
            _banner_extra += " | 二采引导=上段二采latent尾部"
        if expand_mode:
            _banner_extra += f" | 扩写≈{int(enhance.get('expand_words') or 500)}字"
        # 穿透生成：段数按提示词实际分段数定（横幅与实际生成段数保持一致）
        _pt_seg = _pt_seg_count(prompt_input) if passthrough else 0
        _seg_lbl = f"{_pt_seg}段" if _pt_seg else _seg_label(manager.get("_jzl_auto_count"), _vc_count(video_count))
        _run_banner("无限时长", run_mode, _seg_lbl, story_style, story_name, enhance, _banner_extra)

        # ── 故事扩写模式：只按「故事风格 + 扩写字数」扩写正文，不拆解；纯文本输出，不生成视频 ──
        if expand_mode:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput("")
            _ew = int(enhance.get("expand_words") or 500)
            _esrc = str(enhance.get("expand_source") or "素材")
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            _xri, _xrv, _xra, _ = _build_asset_intro(assets_cfg)
            expanded, x_err = _run_story_expander(
                prompt_input, manager, story_style, story_name, prompt_lang, llm_seed,
                expand_words=_ew, expand_source=_esrc,
                ref_image_intro=_xri, ref_video_intro=_xrv, ref_audio_intro=_xra,
                gen_dir=gen_dir)
            if x_err:
                print(f"[JZL-无限时长] LLM/API 出错，终止：{x_err}")
                return io.NodeOutput("", block_execution=f"⚠️ LLM/API 出错，已终止：{x_err}")
            if expanded:
                _save_last_script(story_name, expanded)
            _llm_finish(enhance)
            _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
            return io.NodeOutput(expanded, ui=_seed_ui)

        # ── 仅提示词输出：拆解 + 增强（不含扩写），只 LLM 处理，不生成视频 ──
        if pure_prompt:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput("")
            _pcount = _vc_count(video_count)
            ri, rv, ra, _ = _build_asset_intro(assets_cfg)
            _pe, _pp, _pv, _pa = _detect_enables(prompt_input, assets_cfg)
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            processed, p_err = _run_script_processor(
                prompt_input, manager, _pcount, story_style, story_name, duration, prompt_lang, llm_seed,
                ref_image_intro=ri, ref_video_intro=rv, ref_audio_intro=ra,
                enable_scene=_pe, enable_props=_pp, enable_video=_pv, enable_audio=_pa,
                mode="拆解模式 (Decompose)", gen_dir=gen_dir)
            if p_err:
                print(f"[JZL-无限时长] LLM/API 出错，终止：{p_err}")
                return io.NodeOutput("", block_execution=f"⚠️ LLM/API 出错，已终止：{p_err}")
            if enhance.get("enabled", False):
                processed, p_err2 = _run_prompt_enhancer(processed, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir)
                if p_err2:
                    print(f"[JZL-无限时长] LLM/API 出错，终止：{p_err2}")
                    return io.NodeOutput("", block_execution=f"⚠️ LLM/API 出错，已终止：{p_err2}")
            if processed:
                _save_last_script(story_name, processed)
            _llm_finish(enhance)
            _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
            return io.NodeOutput(processed, ui=_seed_ui)

        # 清空重建池，避免旧资产残留
        JZL_ASSET_POOL.clear()
        JZL_SLOT_MAP.clear()
        manifest, errors = _load_assets_into_pool(assets_cfg)

        width, height = _resolve_gen_size(aspect_ratio, megapixels)
        length = _resolve_length(duration_hi)
        count = video_count

        ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset = _build_asset_intro(assets_cfg)
        JZL_SLOT_MAP.update(slot_to_asset)
        enable_scene, enable_props, enable_video, enable_audio = _detect_enables(prompt_input, assets_cfg)

        # 故事拆解 / 故事扩写：输入必须是自然语言故事；含六段式标签（已拆解提示词）→ 报错终止
        if run_mode in ("故事拆解模式", "故事扩写模式"):
            _nl_err = _check_natural_language_input(prompt_input)
            if _nl_err:
                print(f"[JZL-无限时长] ❌ {_nl_err}（已终止）")
                return io.NodeOutput(prompt_input or "", block_execution=f"❌ {_nl_err}",
                                     ui={"manager_error": [_nl_err]})
        has_shots = bool(re.search(r'\[SHOT_START\]', prompt_input or ""))
        # ⭐ 衔接跑道：段 2+ 的「生成时长 = 设定时长 + 跑道」（段首跑道会被裁掉 ⇒ 落盘时长 = 设定时长）。
        #    `gen_duration_lo` = 生成时长下限（块内缺 **时长** 字段时的兜底，也按生成时长算）。
        gen_duration_lo = duration_lo + seam_runway_sec
        if seam_runway_sec > 0 and has_shots:
            print(f"[JZL-无限时长] ⚠️ 你自带了 [SHOT_START] 剧本块：本节点会再裁掉段首 "
                  f"{seam_window_cfg + seam_settle} 帧跑道 → 落盘 = 生成 − 跑道；"
                  f"若要落盘 = 设定时长，请把每段 **时长** 写成「设定时长 + {seam_runway_sec:.2f} 秒」")
        gen_dir = None
        if not passthrough and enhance.get("story_decompose", True) and not has_shots and prompt_input:
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_script_processor(
                prompt_input, manager, count, story_style, story_name, duration, prompt_lang, llm_seed,
                ref_image_intro=ref_image_intro, ref_video_intro=ref_video_intro, ref_audio_intro=ref_audio_intro,
                enable_scene=enable_scene, enable_props=enable_props,
                enable_video=enable_video, enable_audio=enable_audio, mode=story_mode, gen_dir=gen_dir,
                seam_runway=seam_runway_sec)
            if err:
                print(f"[JZL-无限时长] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput(prompt_input or "", block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        if not passthrough and enhance.get("enabled", False):
            if gen_dir is None:
                from .nodes_llama import _next_generation_dir
                gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_prompt_enhancer(prompt_input, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir,
                                                     seam_runway=seam_runway_sec)
            if err:
                print(f"[JZL-无限时长] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput(prompt_input or "", block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        # 穿透生成模式：必须使用「拆解模式」拆解好的提示词；格式不对直接终止；段数自动对齐提示词实际分段数
        if passthrough:
            _pt_err, _pt_n = _check_decomposed_script(prompt_input, video_count,
                                                      auto_count=bool(manager.get("_jzl_auto_count")), tag="无限时长")
            if _pt_err:
                print(f"[JZL-无限时长] ❌ {_pt_err}（已终止）")
                return io.NodeOutput(prompt_input or "", block_execution=f"❌ {_pt_err}",
                                     ui={"manager_error": [_pt_err]})
            count = _pt_n

        prompt_input = _normalize_dispatch_slots(prompt_input, slot_to_asset)
        prompt_input = _normalize_scene_slots(prompt_input, slot_to_asset)
        prompt_input = _prune_fantasy_assets(prompt_input, slot_to_asset)

        if not passthrough and prompt_input:
            _save_last_script(story_name, prompt_input)

        _llm_finish(enhance)
        _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)

        # ── 分段：按 [SHOT_START] 块切分（每段一个独立新提示词 → 剧情持续推进）──
        shots = re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', prompt_input or "", re.DOTALL)
        if manager.get("_jzl_auto_count") and shots:
            count = max(1, min(SEGMENT_COUNT_MAX, len(shots)))   # 「剧情决定」：以实际拆解出的分段数为准
        base_seed = int(sample_decode.get("seed", 0) or 0)
        seed_mode = sample_decode.get("seed_mode", "randomize") or "randomize"

        saved_files = []
        _JZL_SEG_TRACEBACKS.clear()   # 本次运行的分段失败调用栈（诊断报告用）
        _JZL_SEG_ERR_SEEN.clear()     # 每轮重置去重表：否则本轮第 1 段失败会被上一轮抑制掉调用栈
        _out_root = os.path.abspath(folder_paths.get_output_directory())
        _jzl_dir = os.path.join(_out_root, "jzl")
        _safe_story = _safe_story_name(story_name)
        out_video_dir = os.path.join(_jzl_dir, _safe_story)
        try:
            os.makedirs(out_video_dir, exist_ok=True)
        except Exception as _me:
            errors.append(f"无法创建输出目录：{out_video_dir}（{_me}）")
        _batch = _next_batch(out_video_dir, _safe_story)
        _seg_start = _next_counter(out_video_dir, f"{_safe_story}_{_batch}次生成分段")
        first_seed = None
        # ♾️ 拼接保同时收集各段无損 WAV → 合并时「音频统一编码一次」（消除段边界 AAC 误差）
        _merge_wavs = [] if save_mode == "拼接保存" else None
        # ♾️ 衔接状态：只保留「上一段一采 latent」一份（CPU，千万级元素≈20MB，恒定不叠加）
        prev_latent = None
        # ♾️ 二采引导状态（仅 second 模式保留）：上段二采 latent 的**尾部切片**（CPU，只留配置窗口所需 token）
        prev_latent2 = None
        # ♾️ 接缝亮度匹配参考：上一段**落盘后**末帧的平均亮度（只存一个标量，不占内存）
        prev_luma = None
        total_frames = 0
        for i in range(count):
            try:
                _clean_cache()
            except Exception:
                pass
            raw = shots[i].strip() if i < len(shots) else ""
            if i > 0 and not raw:
                print(f"[JZL-无限时长] 分段 {i + 1}/{count}：⚠️ 剧本里没有第 {i + 1} 段"
                      f"（[SHOT_START] 块只有 {len(shots)} 个）→ 本段只能用占位提示词，请核对拆解结果")
            h3, scene, vid, aud = _parse_four_in_one(raw)
            if not h3:
                h3 = raw.strip() if raw.strip() else (
                    prompt_input.strip() if i == 0 and prompt_input else "[未找到H3提示词]")

            mention_names, h3 = _extract_mentions(h3)
            ref_images, ref_videos, ref_audios, ref_names = _collect_refs_with_names(scene, vid, aud, mention_names)

            mode = "纯文本生成音视频-T2VA"
            if ref_videos or ref_images or ref_audios:
                mode = "多参考生成音视频-REF2VA"
            can_generate = clip is not None and vae is not None and model is not None
            if (ref_videos or ref_audios) and audio_vae is None:
                can_generate = False
            seed = _resolve_seed(seed_mode, base_seed, i)
            if first_seed is None:
                first_seed = seed
            if not can_generate:
                _need = "model/CLIP/VAE"
                if (ref_videos or ref_audios) and audio_vae is None:
                    _need = "model/CLIP/VAE/audio_vae（本段含视频或音频参考）"
                errors.append(f"第{i + 1}段跳过：未连接 {_need}，仅完成剧本分段（未生成视频）")
                continue

            # ⭐ 段 2+ 的 **时长** 字段 = 生成时长（设定 + 跑道）；缺字段时兜底也用生成时长。
            #    落盘帧数 = 生成帧数 − 段首丢弃(窗口+过渡) ⇒ 逐段都会核对是否 ≈ 设定时长（见下方裁切日志）。
            seg_len = _seg_len_for(raw, gen_duration_lo)
            _saved_target = _seg_len_for(None, duration_lo)   # 本段落盘目标帧数（完整独立生成 = 设定时长）
            print(f"[JZL-无限时长] 分段 {i + 1}/{count} 生成中（{mode}，{seg_len} 帧"
                  f"{f'，含段首衔接 {seam_window_cfg + seam_settle} 帧' if (i > 0 and seam_runway_sec > 0) else ''}"
                  f"，种子 {seed}，参考 {len(ref_images)}图/{len(ref_videos)}视频/{len(ref_audios)}音频）")
            try:
                # ① 编码（ref2va，与官方 ReferenceToVideo 一致；**每段都重新注入原始参考**→ 抗长链漂移）
                positive, latent = _encode_refs(
                    "无限时长", f"分段 {i + 1}/{count} ",
                    clip, vae, audio_vae, h3, width, height, seg_len,
                    ref_images, ref_videos, ref_audios, ref_names, ref_scale=scale_factor, model=model)
                positive_base = positive   # 未注入引导的“干净” positive（二采可能要用另一份引导重建）

                # ② ♾️ 衔接：第 2 段起把上一段一采 latent 的尾部窗口钉成本段首帧引导
                seam_plan = None
                if i > 0 and _seam_on and prev_latent is not None:
                    # 本段至少要装得下「窗口 + 过渡」，否则整段会被裁空（成片变 0 帧 / 只剩 1 帧）
                    _drop_need = seam_window_cfg + seam_settle
                    if seg_len <= _drop_need:
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count}：⚠️ 本段 {seg_len} 帧装不下"
                              f"「衔接窗口 {seam_window_cfg} + 过渡 {seam_settle} = {_drop_need} 帧」"
                              f"→ 本段跳过衔接（该段落盘 {seg_len} 帧 = 设定时长，无跑道可裁；"
                              f"若落盘明显偏短，请检查剧本该段 **时长** 是否按「设定 + 跑道」写）")
                    else:
                        seam_plan = _seam_plan(prev_latent, seam_window_cfg)
                        if seam_plan is None:
                            print(f"[JZL-无限时长] 分段 {i + 1}/{count}：⚠️ 上段 latent 不满足衔接条件"
                                  f"（{seam_window_cfg} 帧窗口放不下或段长非 17k+5），本段按独立生成处理")
                if seam_plan is not None:
                    positive, _kf = _seam_apply_guide(positive, prev_latent, seam_plan,
                                                     audio_link=seam_audio_link,
                                                     vae=vae, guide_source=seam_guide_source)
                    # 画布一致性兜底：引导 latent 必须与本次采样目标同画布（PackedLayout 按目标画布算行数）
                    positive = _seam_fit_guides_to_canvas(positive, latent["samples"], tag="无限时长")
                    if _kf is not None and int(_kf["latent"].shape[2]) != int(seam_plan["trim_tokens"]):
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count}：⚠️ 引导 latent token 数 "
                              f"{int(_kf['latent'].shape[2])} ≠ 期望 {seam_plan['trim_tokens']}（衔接窗口可能不准）")
                    _a_txt = (f"＋音频尾部 {seam_plan['trim_audio']} 步"
                              if (_kf and "audio_latent" in _kf) else "（音频不衔接）")
                    if seam_plan["adjusted"]:
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count}：窗口按上段段长自动调整为 "
                              f"{seam_plan['window']} 帧（配置 {seam_window_cfg} 帧）")
                    print(f"[JZL-无限时长] 分段 {i + 1}/{count} 衔接：上段尾部 {seam_plan['window']} 帧"
                          f"（token {seam_plan['trim_tokens']}）{_a_txt}，解码后裁掉开头 {seam_plan['window']} 帧")

                positive = _apply_semantic_bridge(positive, manager, tag="无限时长")
                # ③ 第一次采样（衔接引导源 = 本段一采 latent，与下段目标 latent 同画布同空间）
                first_samples = _sample_first_pass(model, positive, latent, sample_decode, seed, vae, tag="无限时长")
                samples = first_samples

                # ④ latent 放大 + 第二次采样（可选）：二采结果只用于解码落盘
                #    二采引导源（面板「二采引导来源」）：
                #      first（默认）= 复用本段一采引导（上段一采 latent）→ 内部按二采画布插值对齐
                #      second        = 用上一段**二采** latent 尾部重建引导（与二采画布同画布、同 refined 版本）
                _second = (sample_decode or {}).get("second") or {}
                if _second.get("enabled") and not _selflift_mode(sample_decode):
                    print(f"[JZL-无限时长] 分段 {i + 1}/{count} 二次采样：latent 放大 {upscale_scale}x"
                          f"（{_second.get('sampler', 'euler')}/{_second.get('scheduler', 'simple')}，"
                          f"{_second.get('steps', 3)}步，denoise {_second.get('denoise', 0.3)}）…")
                    _pos2 = positive
                    if seam_second_guide == "second" and seam_plan is not None and prev_latent2 is not None:
                        _pos2 = _seam_apply_guide(positive_base, prev_latent2, seam_plan,
                                                 audio_link=seam_audio_link, vae=None,
                                                 guide_source="latent")[0]
                    samples = _run_second_sampling(model, _pos2, first_samples, sample_decode, _second, upscale_scale, seed, tag="无限时长")
                    _t1 = int(first_samples.tensors[0].shape[2]) if getattr(first_samples, "is_nested", False) else int(first_samples.shape[2])
                    _t2 = int(samples.tensors[0].shape[2]) if getattr(samples, "is_nested", False) else int(samples.shape[2])
                    if _t2 != _t1:
                        raise RuntimeError(
                            f"二次采样改变了时间轴长度（一采 {_t1} token → 二采 {_t2} token），"
                            f"无法保证段间衔接裁切正确；请关闭二次采样后重试")
                    # ♾️ second 模式：把本段二采 latent 的尾部切片（配置窗口所需 token）留给下一段做同画布引导
                    if seam_second_guide == "second":
                        prev_latent2 = _seam_keep_tail(samples, seam_window_cfg)
                else:
                    prev_latent2 = None

                # ⑤ ♾️ 裁掉段首被 keyframe 钉住的窗口帧
                #    默认「像素域」：latent 完全不裁（VAE 解码器必须看到完整 5k+2 序列 → 分组相位 0），解码后再丢帧
                #    「latent 域」：先裁 latent 再解码（解码量更少、保留帧不含被裁内容），但**丢弃 token 数必须是 5 的倍数**
                #    （= 丢弃帧数是 17 的倍数 ⇒ 过渡=12/29/46）；不满足时按方案 A 自动回退像素域（永不闪烁）
                seam_trim_in_latent = (seam_plan is not None and seam_trim_mode == "latent")
                seam_latent_drop = None
                if seam_trim_in_latent:
                    seam_latent_drop = _seam_latent_drop(seam_plan["window"], seam_settle)
                    if seam_latent_drop is None:
                        seam_trim_in_latent = False
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count}：latent 域裁切要求「窗口 "
                              f"{seam_plan['window']} + 过渡 {seam_settle}」是 17 的倍数（过渡需为 12/29/46）"
                              f"→ 自动回退像素域裁切（保证不闪烁）")
                    else:
                        samples = _seam_trim_head(samples, seam_plan, trim_audio=False,
                                                  drop_tokens=seam_latent_drop["tokens"])
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} latent 域裁切：丢开头 "
                              f"{seam_latent_drop['tokens']} token = {seam_latent_drop['frames']} 帧"
                              f"（窗口 {seam_plan['window']} + 过渡 {seam_settle}）")

                image, audio = _decode_av(vae, audio_vae, samples, sample_decode, tag="无限时长")
                _raw_frames = int(image.shape[0]) if image is not None else 0
                if seam_plan is not None and seam_latent_drop is not None:
                    # latent 域：视频已在 token 域裁好 → 音频在波形域按「同一丢弃帧数」裁（与像素域同一套音画规则）
                    if seam_trim_audio:
                        audio = _seam_trim_audio_frames(audio, seam_latent_drop["frames"], _raw_frames)
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} latent 域裁切完成：解码 {_raw_frames} 帧"
                              f"（少解码 {seam_latent_drop['frames']} 帧），音频同步裁剪")
                    else:
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count}：⚠️ 「音频同步裁剪」已关闭：画面已丢 "
                              f"{seam_latent_drop['frames']} 帧而音频不裁 → 音画错位 "
                              f"{seam_latent_drop['frames'] / 24.0:.3f}s（仅作对比实验）")
                elif seam_plan is not None:
                    if seam_trim_mode == "none":
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count}：⚠️ 诊断模式「不裁切」→ 成片开头会保留 "
                              f"{seam_plan['window']} 帧衔接帧（重复上段尾部），仅用于定位问题")
                    else:
                        image, audio = _seam_trim_decoded(image, audio, seam_plan,
                                                          settle_frames=seam_settle,
                                                          trim_audio=seam_trim_audio, tag="无限时长")
                        _kept = int(image.shape[0]) if image is not None else 0
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} 像素域裁切：解码 {_raw_frames} 帧"
                              f" − 衔接 {seam_plan['window']} − 过渡 {seam_settle} = 保留 {_kept} 帧"
                              f"（{_kept / 24.0:.2f}s）"
                              f"{'，音频同步裁剪' if seam_trim_audio else '，⚠️ 音频未裁剪（仅对比实验）'}")
                _frames = int(image.shape[0]) if image is not None else 0
                # ⭐ 帧账核对：落盘帧数应 ≈ 设定时长对应的帧数（生成 = 设定 + 跑道，裁掉跑道后回到设定）
                if i > 0 and seam_runway_sec > 0 and seam_plan is not None and seam_trim_mode != "none":
                    _delta = _frames - _saved_target
                    if abs(_delta) <= 2:
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} 落盘 {_frames} 帧"
                              f"（生成 {seg_len} − 裁切 {seg_len - _frames}）= {_frames / 24.0:.2f} 秒 ✓")
                    else:
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} 落盘 {_frames} 帧 = {_frames / 24.0:.2f} 秒；"
                              f"⚠️ 比设定时长差 {_delta:+d} 帧（{_delta / 24.0:+.2f}s）：该段 **时长** 可能没按"
                              f"「设定 + 跑道」写，或「设定时长 + 跑道」超出单段上限 20 秒")
                total_frames += _frames

                # ★ ♾️ 接缝亮度匹配（**只做亮度**）：把本段开头若干帧的平均亮度朝上一段末帧平滑过渡。
                #   只做加性（R=G=B）、单帧限幅 ±0.10、前 12 帧渐入、三项差都 < 0.008 时零改动。
                #   官方 Director 实测结论：乘法增益 / 长 RGB 混合 / 开口亮度渐变都出问题，只有这一种安全。
                #   必须在落盘**之前**做（拼接是 -c copy，不会重编码）；诊断档「不裁切」时不动像素。
                if (seam_luma_on and prev_luma is not None and seam_plan is not None
                        and seam_trim_mode != "none" and image is not None):
                    image = _seam_luma_ramp(image, prev_luma)[0]

                # ★ 每段解码完立即 ffmpeg 落盘（拼接保存时额外保留一份无損 WAV，供最后音频统一编码）
                _wav_keep = None
                if _merge_wavs is not None and audio is not None:
                    _wav_keep = os.path.join(
                        out_video_dir, f"._jzl_seg{i + 1}_{_seg_start + i}_{os.getpid()}.wav")
                    _merge_wavs.append(_wav_keep)   # 先登记：即使落盘失败也能在收尾时一起清理
                _sp = _save_segment_mp4(image, audio, out_video_dir, i + 1,
                                        story_name=_safe_story, counter=_seg_start + i, batch=_batch,
                                        keep_wav=_wav_keep)
                saved_files.append(_sp)
                print(f"[JZL-无限时长] 分段 {i + 1}/{count} 已落盘：{_sp}（{_frames} 帧）")

                # ★ 衔接源：本段一采 latent 转 CPU 留一份给下一段（二采不影响它）
                prev_latent = first_samples.to("cpu") if hasattr(first_samples, "to") else first_samples
                # ★ 接缝亮度匹配参考：只记「本段**落盘后**末帧的平均亮度」一个标量（零内存开销）
                if image is not None and hasattr(image, "shape") and int(image.shape[0]) > 0:
                    try:
                        prev_luma = _seam_frame_luma(image[-1])
                    except Exception:
                        prev_luma = None

                # ★ 释放该段全部显存/内存（跑 N 段与跑 1 段占用不叠加）
                try:
                    del image, audio, samples, positive, latent, first_samples
                except Exception:
                    pass
                _clean_cache()
            except Exception as e:
                err = str(e)
                _tb_txt = ""
                try:
                    import traceback as _tb
                    _tb_txt = _tb.format_exc()
                except Exception:
                    pass
                _seg_fail_context(i + 1, _tb_txt)
                if "no kernel image" in err:
                    err += (f"\n  [JZL提示] CUDA/ROCm 内核与显卡架构不匹配（no kernel image）：当前 torch 没有该 GPU 的编译内核。"
                            f"诊断：{_gpu_diag_hint()}\n"
                            f"  请确认云机显卡型号与 torch/CUDA 版本匹配。")
                elif "CUDA error" in err or "cudaError" in err or "out of memory" in err:
                    err += (f"\n  [JZL提示] CUDA 运行时错误，诊断：{_gpu_diag_hint()}\n"
                            f"  可能原因：显存不足、驱动/算子在 Blackwell(sm120) 不兼容、dtype/精度、或某算子非法参数"
                            f"（如 attention 后端/参考张量）。")
                _oom_hint = _oom_diag_hint(err, sample_decode, width, height,
                                       _seg_len_for(raw, duration_lo))
                if _oom_hint:
                    err += _oom_hint
                _attn_hint = _attention_diag_hint(err)
                if _attn_hint:
                    err += _attn_hint
                errors.append(f"第{i + 1}段生成失败：{err}")
                _log_seg_error("无限时长", i + 1, err, _tb_txt)

        # 采样种子回写：randomize 时把第 1 段实际种子回传前端
        if seed_mode == "randomize" and first_seed is not None:
            _samp_ui = {"seed_update_sample": [{"seed": str(int(first_seed)), "seed_control": seed_mode}]}
            print(f"[JZL-无限时长] 采样种子（本轮第 1 段）={int(first_seed)} → 已回传前端显示")
            if _seed_ui:
                _seed_ui = dict(_seed_ui)
                _seed_ui.update(_samp_ui)
            else:
                _seed_ui = _samp_ui

        if (sample_decode or {}).get("decode_cleanup") == "卸载显存模型":
            _cleanup_vram("卸载显存模型", tag="无限时长")

        # ── 保存模式：分段保存（每段已即时落盘）/ 拼接保存（读盘 concat 成一个）──
        out_files = list(saved_files)
        if save_mode == "拼接保存" and saved_files:
            try:
                _merge_out = os.path.join(
                    out_video_dir,
                    f"{_safe_story}_{_batch}次生成分段合并_{_next_counter(out_video_dir, f'{_safe_story}_{_batch}次生成分段合并'):05d}.mp4")
                _merged = _merge_mp4_concat(saved_files, _merge_out, audio_wavs=_merge_wavs)
                print(f"[JZL-无限时长] 拼接保存完成（{len(saved_files)} 段按顺序合并）：{_merged}")
                if auto_merge_delete:
                    _del_cnt = 0
                    for _sp in saved_files:
                        try:
                            if os.path.exists(_sp):
                                os.remove(_sp); _del_cnt += 1
                        except Exception:
                            pass
                    print(f"[JZL-无限时长] 已按配置删除 {_del_cnt} 个分段视频（仅保留合并结果）")
                out_files = [_merged]
            except Exception as _me:
                errors.append(f"拼接保存失败：{_me}（已保留各分段视频）")

        # 清理临时无損 WAV（已合入成片或不再需要）
        if _merge_wavs:
            for _w in _merge_wavs:
                try:
                    if os.path.exists(_w):
                        os.remove(_w)
                except Exception:
                    pass

        if errors:
            for e in errors:
                print(f"[JZL-无限时长] {e}")

        _dur = (total_frames / float(VIDEO_FPS)) if total_frames else 0.0
        print(f"[JZL-无限时长] 本次落盘：{out_video_dir}（视频 {len(saved_files)} 个）")
        print(f"[JZL-无限时长] 本次成片合计 {total_frames} 帧 = {_dur:.2f} 秒 @24fps（{count} 段）")
        if seam_runway_sec > 0 and saved_files:
            _set_frames = _seg_len_for(None, duration_lo)
            print(f"[JZL-无限时长] ⏱️ 时长对照：设定 {_set_frames} 帧/段 = {_set_frames / float(VIDEO_FPS):.2f} 秒/段"
                  f" × {len(saved_files)} 段 = {_set_frames * len(saved_files) / float(VIDEO_FPS):.2f} 秒"
                  f"（每段生成时多出段首衔接跑道 {seam_window_cfg + seam_settle} 帧 = {seam_runway_sec:.2f} 秒，已在成片中裁掉）")
        if not saved_files:
            print("[JZL-无限时长] ⚠️ 本段未落盘任何视频（未连接模型/CLIP/VAE 或全部分段失败）")
            _report_save_failure(
                "无限时长", _out_root, out_video_dir, saved_files, errors,
                [f"已连接：model={model is not None} CLIP={clip is not None} VAE={vae is not None} "
                 f"audio_vae={audio_vae is not None}",
                 f"计划段数={count} 实际分段块={len(shots)} 运行模式={run_mode}",
                 f"保存模式={save_mode}"])
        return io.NodeOutput(prompt_input, ui=_seed_ui)



class JZL_MiniMaxAssetManagerShortDrama(io.ComfyNode):
    """JZL - 🤖 MiniMax-H3短剧导演台 — 固定段数的逐段生成长视频链路。

    与「短剧导演台Max」唯一差异在分段循环（LLM 拆解 / 资产 / 参数 / 逐段落盘 / 读盘拼接 / 显存释
    放全部沿用同一套）：
    · 每段按剧情拆解出的「新提示词」独立生成 → 剧情持续往前推进；
    · 段与段之间是**大切镜/换场**：不做跨段 latent 引导、不裁段首衔接帧（无限时长衔接只保留给
      「JZL - 🤖 MiniMax-H3无限时长」节点）；
    · 逐段即时落盘 + 读盘拼接，跑 1 段与跑 N 段逻辑同一套、硬件占用不叠加。
    """

    @classmethod
    def define_schema(cls):
        story_styles = _story_style_options()
        return io.Schema(
            node_id="JZL_MiniMaxAssetManagerShortDrama",
            display_name="JZL - 🤖 MiniMax-H3短剧导演台",
            category="JZL/MiniMax",
            description=("MiniMax-H3 短剧导演台：逐段「新剧情提示词」独立生成（段间大切镜/换场，不做跨段 "
                         "latent 引导）。逐段即时落盘 + 读盘 concat，跑 1 段与跑 N 段逻辑同一套、"
                         "硬件占用不叠加。"),
            inputs=[
                io.Combo.Input("run_mode", options=RUN_MODE_EMOJI,
                    default=RUN_MODE_EMOJI[0], display_name="运行模式",
                    tooltip="与「短剧导演台Max」一致：故事拆解模式=按情节拆解为N段（每段一个独立新提示词，多段按此逐段推进剧情）；故事扩写模式=只扩写正文（纯文本输出）；穿透生成模式=跳过 LLM 直接用提示词生成；仅提示词输出=只输出拆解+增强后的分段剧本，不生成视频"),
                io.String.Input("display_info", display_name="生成详情", default="分辨率：864x480丨每段帧数：124丨段数：剧情决定丨成片总长：124~6944 帧 ≈ 5.17~289.33 秒",
                    multiline=False, socketless=True,
                    tooltip="只读显示：当前画幅/MP/时长/段数计算出的分辨率、每段帧数、段数、总帧数、总时长（对齐倍数固定 32）"),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIO_OPTIONS, default="16:9 (Widescreen)",
                    display_name="画幅比例", tooltip="画幅比例（分辨率按 MP×1024² 公式自动计算，对齐倍数固定 32）"),
                io.Float.Input("megapixels", display_name="百万像素（MP）", default=0.4, min=0.1, max=3.6, step=0.1,
                    tooltip="总像素数（MP），画幅×MP 决定分辨率"),
                io.Combo.Input("duration", options=list(SEGMENT_DURATION_CHOICES), default=SEGMENT_DURATION_DEFAULT,
                    display_name="每段视频时长", tooltip="每段视频时长（秒）：固定 4~20 秒 或 区间。等同「剧本与镜头处理器」的每段视频时长"),
                io.Float.Input("scale_factor", display_name="参考数值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="参考图放大系数"),
                io.Float.Input("upscale_scale", display_name="二采latent放大", default=1.0, min=1.0, max=4.0, step=0.1,
                    tooltip="二采（Ref2va）放大倍数。二采结果只用于解码落盘"),
                io.Int.Input("video_count", default=0, min=0, max=SEGMENT_COUNT_MAX, step=1,
                    display_name="生成视频数量",
                    tooltip="参数：0 = LLM 按剧情时间线长度，自动计算需要生成多少段视频。\n"
                            "参数：1~60 = 设置固定生成数量，最多支持一次生成60段视频！\n"
                            "🪄 穿透生成模式下忽略此设定：一律按提示词实际 [SHOT_START] 段数生成"),
                io.Combo.Input("story_style", options=story_styles, default=story_styles[0],
                    display_name="故事风格", tooltip="故事风格（剧本处理器按此风格拆解与润色）"),
                io.String.Input("story_name", display_name="故事名称", default="机智罗",
                    tooltip="故事名称（用于保存命名 / 落盘目录 output/jzl/{故事名} / 生成视频管理）"),
                io.Model.Input("model", display_name="主模型", optional=True),
                io.Clip.Input("clip", display_name="CLIP", optional=True),
                io.Vae.Input("vae", display_name="视觉VAE", optional=True),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True),
                io.String.Input("internal_prompt", display_name="节点内提示词", multiline=True,
                    socketless=True,
                    tooltip="节点内编辑的提示词（提示词来源，随工作流保存）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True,
                    socketless=True, default="",
                    tooltip="本节点独立保存的完整配置 JSON（资产/增强/采样解码/保存），随工作流保存，节点间互不影响"),
            ],
            outputs=[
                io.String.Output("script", display_name="已处理剧本",
                    tooltip="全部 LLM 处理后的剧本/提示词文本（供下游展示/保存用；「生成视频查看器」直接读盘）"),
            ],
            is_output_node=True,  # 自带逐段落盘+读盘拼接，单节点即可运行
        )

    @classmethod
    def fingerprint_inputs(cls, manager_settings="", **kwargs):
        try:
            cfg = _parse_node_manager_settings(manager_settings)
            enhance = cfg.get("enhance") or {}
            sample_decode = cfg.get("sample_decode") or {}
            llm_random = (enhance.get("seed_control") or "randomize") == "randomize"
            samp_random = (sample_decode.get("seed_mode") or "randomize") == "randomize"
            if llm_random or samp_random:
                return f"random@{time.time_ns()}"
            gen_keys = ["run_mode", "video_count", "story_style", "duration",
                        "aspect_ratio", "megapixels", "scale_factor", "upscale_scale",
                        "internal_prompt"]
            gen_sig = {k: kwargs.get(k) for k in gen_keys}
            return f"{cfg}|{gen_sig}"
        except Exception:
            return "no-manager"

    @classmethod
    def execute(cls, run_mode="拆解故事模式", video_count=VIDEO_COUNT_DEFAULT, aspect_ratio="16:9 (Widescreen)",
                megapixels=1.0, duration="5-5", scale_factor=1.0, upscale_scale=1.5, display_info="",
                story_style="热血战斗", story_name="",
                internal_prompt=None, manager_settings="",
                clip=None, vae=None, audio_vae=None, model=None) -> io.NodeOutput:
        _vc_auto = _parse_video_count(video_count)[0] == "auto"   # 「剧情决定」标记（manager 建后写入）
        video_count = _vc_count(video_count)     # 「剧情决定」→ 占位 6（拆解后按实际分段数覆盖）
        """短剧导演台：LLM 拆解（每段新剧情）→ 逐段【编码 + 一采 + 二采 + 解码】→
        每段即时落盘 → 释放该段显存 → 下一段（段间大切镜，不做跨段 latent 引导）。
        跑 N 段与跑 1 段占用不叠加。"""
        run_mode = _normalize_run_mode(run_mode)
        pure_prompt = run_mode == "仅提示词输出"
        expand_mode = run_mode == "故事扩写模式"
        passthrough = run_mode == "穿透生成模式"
        story_mode = "拆解模式 (Decompose)"

        story_name = (story_name or "").strip()
        if not story_name:
            print("[JZL-短剧导演台] ⚠️ 必须填写「故事名称」才能生成（已终止）")
            return io.NodeOutput("", block_execution="必须填写「故事名称」后才能生成",
                                  ui={"manager_error": ["必须填写「故事名称」后才能生成"]})

        manager = _parse_node_manager_settings(manager_settings)
        manager["_jzl_auto_count"] = _vc_auto          # 「剧情决定」标记（供 _run_script_processor/日志用）
        duration_lo, duration_hi, duration_desc = _parse_duration_spec(duration)
        enhance = manager.get("enhance") or {}
        assets_cfg = manager.get("assets") or {}
        sample_decode = manager.get("sample_decode") or {}
        # 提示词来源：接线提示词优先于节点内提示词
        if not isinstance(internal_prompt, str):
            internal_prompt = ""
        prompt_input = (internal_prompt or "").strip()
        _save_cfg = manager.get("save") or {}
        save_mode = (_save_cfg.get("mode") or "分段保存").strip() or "分段保存"
        auto_merge_delete = bool(_save_cfg.get("auto_merge_delete"))
        prompt_lang = (enhance.get("prompt_lang") or "中文 [ZH]").strip() or "中文 [ZH]"

        seed_control = (enhance.get("seed_control") or "randomize").strip() or "randomize"
        current_seed = int(enhance.get("seed", 0) or 0)
        if seed_control == "randomize":
            llm_seed = int(torch.randint(0, 0x7fffffffffffffff, (1,)).item())
        else:
            llm_seed = current_seed
        _banner_extra = ""
        if expand_mode:
            _banner_extra += f" | 扩写≈{int(enhance.get('expand_words') or 500)}字"
        # 穿透生成：段数按提示词实际分段数定（横幅与实际生成段数保持一致）
        _pt_seg = _pt_seg_count(prompt_input) if passthrough else 0
        _seg_lbl = f"{_pt_seg}段" if _pt_seg else _seg_label(manager.get("_jzl_auto_count"), _vc_count(video_count))
        _run_banner("短剧导演台", run_mode, _seg_lbl, story_style, story_name, enhance, _banner_extra)

        # ── 故事扩写模式：只按「故事风格 + 扩写字数」扩写正文，不拆解；纯文本输出，不生成视频 ──
        if expand_mode:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput("")
            _ew = int(enhance.get("expand_words") or 500)
            _esrc = str(enhance.get("expand_source") or "素材")
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            _xri, _xrv, _xra, _ = _build_asset_intro(assets_cfg)
            expanded, x_err = _run_story_expander(
                prompt_input, manager, story_style, story_name, prompt_lang, llm_seed,
                expand_words=_ew, expand_source=_esrc,
                ref_image_intro=_xri, ref_video_intro=_xrv, ref_audio_intro=_xra,
                gen_dir=gen_dir)
            if x_err:
                print(f"[JZL-短剧导演台] LLM/API 出错，终止：{x_err}")
                return io.NodeOutput("", block_execution=f"⚠️ LLM/API 出错，已终止：{x_err}")
            if expanded:
                _save_last_script(story_name, expanded)
            _llm_finish(enhance)
            _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
            return io.NodeOutput(expanded, ui=_seed_ui)

        # ── 仅提示词输出：拆解 + 增强（不含扩写），只 LLM 处理，不生成视频 ──
        if pure_prompt:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput("")
            _pcount = _vc_count(video_count)
            ri, rv, ra, _ = _build_asset_intro(assets_cfg)
            _pe, _pp, _pv, _pa = _detect_enables(prompt_input, assets_cfg)
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            processed, p_err = _run_script_processor(
                prompt_input, manager, _pcount, story_style, story_name, duration, prompt_lang, llm_seed,
                ref_image_intro=ri, ref_video_intro=rv, ref_audio_intro=ra,
                enable_scene=_pe, enable_props=_pp, enable_video=_pv, enable_audio=_pa,
                mode="拆解模式 (Decompose)", gen_dir=gen_dir)
            if p_err:
                print(f"[JZL-短剧导演台] LLM/API 出错，终止：{p_err}")
                return io.NodeOutput("", block_execution=f"⚠️ LLM/API 出错，已终止：{p_err}")
            if enhance.get("enabled", False):
                processed, p_err2 = _run_prompt_enhancer(processed, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir)
                if p_err2:
                    print(f"[JZL-短剧导演台] LLM/API 出错，终止：{p_err2}")
                    return io.NodeOutput("", block_execution=f"⚠️ LLM/API 出错，已终止：{p_err2}")
            if processed:
                _save_last_script(story_name, processed)
            _llm_finish(enhance)
            _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
            return io.NodeOutput(processed, ui=_seed_ui)

        # 清空重建池，避免旧资产残留
        JZL_ASSET_POOL.clear()
        JZL_SLOT_MAP.clear()
        manifest, errors = _load_assets_into_pool(assets_cfg)

        width, height = _resolve_gen_size(aspect_ratio, megapixels)
        length = _resolve_length(duration_hi)
        count = video_count

        ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset = _build_asset_intro(assets_cfg)
        JZL_SLOT_MAP.update(slot_to_asset)
        enable_scene, enable_props, enable_video, enable_audio = _detect_enables(prompt_input, assets_cfg)

        # 故事拆解 / 故事扩写：输入必须是自然语言故事；含六段式标签（已拆解提示词）→ 报错终止
        if run_mode in ("故事拆解模式", "故事扩写模式"):
            _nl_err = _check_natural_language_input(prompt_input)
            if _nl_err:
                print(f"[JZL-短剧导演台] ❌ {_nl_err}（已终止）")
                return io.NodeOutput(prompt_input or "", block_execution=f"❌ {_nl_err}",
                                     ui={"manager_error": [_nl_err]})
        has_shots = bool(re.search(r'\[SHOT_START\]', prompt_input or ""))
        gen_duration_lo = duration_lo
        gen_dir = None
        if not passthrough and enhance.get("story_decompose", True) and not has_shots and prompt_input:
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_script_processor(
                prompt_input, manager, count, story_style, story_name, duration, prompt_lang, llm_seed,
                ref_image_intro=ref_image_intro, ref_video_intro=ref_video_intro, ref_audio_intro=ref_audio_intro,
                enable_scene=enable_scene, enable_props=enable_props,
                enable_video=enable_video, enable_audio=enable_audio, mode=story_mode, gen_dir=gen_dir)
            if err:
                print(f"[JZL-短剧导演台] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput(prompt_input or "", block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        if not passthrough and enhance.get("enabled", False):
            if gen_dir is None:
                from .nodes_llama import _next_generation_dir
                gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_prompt_enhancer(prompt_input, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir)
            if err:
                print(f"[JZL-短剧导演台] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput(prompt_input or "", block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        # 穿透生成模式：必须使用「拆解模式」拆解好的提示词；格式不对直接终止；段数自动对齐提示词实际分段数
        if passthrough:
            _pt_err, _pt_n = _check_decomposed_script(prompt_input, video_count,
                                                      auto_count=bool(manager.get("_jzl_auto_count")), tag="短剧导演台")
            if _pt_err:
                print(f"[JZL-短剧导演台] ❌ {_pt_err}（已终止）")
                return io.NodeOutput(prompt_input or "", block_execution=f"❌ {_pt_err}",
                                     ui={"manager_error": [_pt_err]})
            count = _pt_n

        prompt_input = _normalize_dispatch_slots(prompt_input, slot_to_asset)
        prompt_input = _normalize_scene_slots(prompt_input, slot_to_asset)
        prompt_input = _prune_fantasy_assets(prompt_input, slot_to_asset)

        if not passthrough and prompt_input:
            _save_last_script(story_name, prompt_input)

        _llm_finish(enhance)
        _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)

        # ── 分段：按 [SHOT_START] 块切分（每段一个独立新提示词 → 剧情持续推进）──
        shots = re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', prompt_input or "", re.DOTALL)
        if manager.get("_jzl_auto_count") and shots:
            count = max(1, min(SEGMENT_COUNT_MAX, len(shots)))   # 「剧情决定」：以实际拆解出的分段数为准
        base_seed = int(sample_decode.get("seed", 0) or 0)
        seed_mode = sample_decode.get("seed_mode", "randomize") or "randomize"

        saved_files = []
        _JZL_SEG_TRACEBACKS.clear()   # 本次运行的分段失败调用栈（诊断报告用）
        _JZL_SEG_ERR_SEEN.clear()     # 每轮重置去重表：否则本轮第 1 段失败会被上一轮抑制掉调用栈
        _out_root = os.path.abspath(folder_paths.get_output_directory())
        _jzl_dir = os.path.join(_out_root, "jzl")
        _safe_story = _safe_story_name(story_name)
        out_video_dir = os.path.join(_jzl_dir, _safe_story)
        try:
            os.makedirs(out_video_dir, exist_ok=True)
        except Exception as _me:
            errors.append(f"无法创建输出目录：{out_video_dir}（{_me}）")
        _batch = _next_batch(out_video_dir, _safe_story)
        _seg_start = _next_counter(out_video_dir, f"{_safe_story}_{_batch}次生成分段")
        first_seed = None
        # ♾️ 拼接保同时收集各段无損 WAV → 合并时「音频统一编码一次」（消除段边界 AAC 误差）
        _merge_wavs = [] if save_mode == "拼接保存" else None
        total_frames = 0
        for i in range(count):
            try:
                _clean_cache()
            except Exception:
                pass
            raw = shots[i].strip() if i < len(shots) else ""
            if i > 0 and not raw:
                print(f"[JZL-短剧导演台] 分段 {i + 1}/{count}：⚠️ 剧本里没有第 {i + 1} 段"
                      f"（[SHOT_START] 块只有 {len(shots)} 个）→ 本段只能用占位提示词，请核对拆解结果")
            h3, scene, vid, aud = _parse_four_in_one(raw)
            if not h3:
                h3 = raw.strip() if raw.strip() else (
                    prompt_input.strip() if i == 0 and prompt_input else "[未找到H3提示词]")

            mention_names, h3 = _extract_mentions(h3)
            ref_images, ref_videos, ref_audios, ref_names = _collect_refs_with_names(scene, vid, aud, mention_names)

            mode = "纯文本生成音视频-T2VA"
            if ref_videos or ref_images or ref_audios:
                mode = "多参考生成音视频-REF2VA"
            can_generate = clip is not None and vae is not None and model is not None
            if (ref_videos or ref_audios) and audio_vae is None:
                can_generate = False
            seed = _resolve_seed(seed_mode, base_seed, i)
            if first_seed is None:
                first_seed = seed
            if not can_generate:
                _need = "model/CLIP/VAE"
                if (ref_videos or ref_audios) and audio_vae is None:
                    _need = "model/CLIP/VAE/audio_vae（本段含视频或音频参考）"
                errors.append(f"第{i + 1}段跳过：未连接 {_need}，仅完成剧本分段（未生成视频）")
                continue

            # ⭐ 段 2+ 的 **时长** 字段 = 生成时长（设定 + 跑道）；缺字段时兜底也用生成时长。
            #    落盘帧数 = 生成帧数 − 段首丢弃(窗口+过渡) ⇒ 逐段都会核对是否 ≈ 设定时长（见下方裁切日志）。
            seg_len = _seg_len_for(raw, gen_duration_lo)
            print(f"[JZL-短剧导演台] 分段 {i + 1}/{count} 生成中（{mode}，{seg_len} 帧"
                  f"，种子 {seed}，参考 {len(ref_images)}图/{len(ref_videos)}视频/{len(ref_audios)}音频）")
            try:
                # ① 编码（ref2va，与官方 ReferenceToVideo 一致；**每段都重新注入原始参考**→ 抗长链漂移）
                positive, latent = _encode_refs(
                    "短剧导演台", f"分段 {i + 1}/{count} ",
                    clip, vae, audio_vae, h3, width, height, seg_len,
                    ref_images, ref_videos, ref_audios, ref_names, ref_scale=scale_factor, model=model)
                positive = _apply_semantic_bridge(positive, manager, tag="短剧导演台")
                # ③ 第一次采样
                first_samples = _sample_first_pass(model, positive, latent, sample_decode, seed, vae, tag="短剧导演台")
                samples = first_samples

                # ④ latent 放大 + 第二次采样（可选）：二采结果只用于解码落盘
                _second = (sample_decode or {}).get("second") or {}
                if _second.get("enabled") and not _selflift_mode(sample_decode):
                    print(f"[JZL-短剧导演台] 分段 {i + 1}/{count} 二次采样：latent 放大 {upscale_scale}x"
                          f"（{_second.get('sampler', 'euler')}/{_second.get('scheduler', 'simple')}，"
                          f"{_second.get('steps', 3)}步，denoise {_second.get('denoise', 0.3)}）…")
                    samples = _run_second_sampling(model, positive, first_samples, sample_decode, _second, upscale_scale, seed, tag="短剧导演台")
                    _t1 = int(first_samples.tensors[0].shape[2]) if getattr(first_samples, "is_nested", False) else int(first_samples.shape[2])
                    _t2 = int(samples.tensors[0].shape[2]) if getattr(samples, "is_nested", False) else int(samples.shape[2])
                    if _t2 != _t1:
                        raise RuntimeError(
                            f"二次采样改变了时间轴长度（一采 {_t1} token → 二采 {_t2} token），"
                            f"请关闭二次采样后重试")

                image, audio = _decode_av(vae, audio_vae, samples, sample_decode, tag="短剧导演台")
                _frames = int(image.shape[0]) if image is not None else 0
                total_frames += _frames

                # ★ 每段解码完立即 ffmpeg 落盘（拼接保存时额外保留一份无損 WAV，供最后音频统一编码）
                _wav_keep = None
                if _merge_wavs is not None and audio is not None:
                    _wav_keep = os.path.join(
                        out_video_dir, f"._jzl_seg{i + 1}_{_seg_start + i}_{os.getpid()}.wav")
                    _merge_wavs.append(_wav_keep)   # 先登记：即使落盘失败也能在收尾时一起清理
                _sp = _save_segment_mp4(image, audio, out_video_dir, i + 1,
                                        story_name=_safe_story, counter=_seg_start + i, batch=_batch,
                                        keep_wav=_wav_keep)
                saved_files.append(_sp)
                print(f"[JZL-短剧导演台] 分段 {i + 1}/{count} 已落盘：{_sp}（{_frames} 帧）")

                # ★ 释放该段全部显存/内存（跑 N 段与跑 1 段占用不叠加）
                try:
                    del image, audio, samples, positive, latent, first_samples
                except Exception:
                    pass
                _clean_cache()
            except Exception as e:
                err = str(e)
                _tb_txt = ""
                try:
                    import traceback as _tb
                    _tb_txt = _tb.format_exc()
                except Exception:
                    pass
                _seg_fail_context(i + 1, _tb_txt)
                if "no kernel image" in err:
                    err += (f"\n  [JZL提示] CUDA/ROCm 内核与显卡架构不匹配（no kernel image）：当前 torch 没有该 GPU 的编译内核。"
                            f"诊断：{_gpu_diag_hint()}\n"
                            f"  请确认云机显卡型号与 torch/CUDA 版本匹配。")
                elif "CUDA error" in err or "cudaError" in err or "out of memory" in err:
                    err += (f"\n  [JZL提示] CUDA 运行时错误，诊断：{_gpu_diag_hint()}\n"
                            f"  可能原因：显存不足、驱动/算子在 Blackwell(sm120) 不兼容、dtype/精度、或某算子非法参数"
                            f"（如 attention 后端/参考张量）。")
                _oom_hint = _oom_diag_hint(err, sample_decode, width, height,
                                       _seg_len_for(raw, duration_lo))
                if _oom_hint:
                    err += _oom_hint
                _attn_hint = _attention_diag_hint(err)
                if _attn_hint:
                    err += _attn_hint
                errors.append(f"第{i + 1}段生成失败：{err}")
                _log_seg_error("短剧导演台", i + 1, err, _tb_txt)

        # 采样种子回写：randomize 时把第 1 段实际种子回传前端
        if seed_mode == "randomize" and first_seed is not None:
            _samp_ui = {"seed_update_sample": [{"seed": str(int(first_seed)), "seed_control": seed_mode}]}
            print(f"[JZL-短剧导演台] 采样种子（本轮第 1 段）={int(first_seed)} → 已回传前端显示")
            if _seed_ui:
                _seed_ui = dict(_seed_ui)
                _seed_ui.update(_samp_ui)
            else:
                _seed_ui = _samp_ui

        if (sample_decode or {}).get("decode_cleanup") == "卸载显存模型":
            _cleanup_vram("卸载显存模型", tag="短剧导演台")

        # ── 保存模式：分段保存（每段已即时落盘）/ 拼接保存（读盘 concat 成一个）──
        out_files = list(saved_files)
        if save_mode == "拼接保存" and saved_files:
            try:
                _merge_out = os.path.join(
                    out_video_dir,
                    f"{_safe_story}_{_batch}次生成分段合并_{_next_counter(out_video_dir, f'{_safe_story}_{_batch}次生成分段合并'):05d}.mp4")
                _merged = _merge_mp4_concat(saved_files, _merge_out, audio_wavs=_merge_wavs)
                print(f"[JZL-短剧导演台] 拼接保存完成（{len(saved_files)} 段按顺序合并）：{_merged}")
                if auto_merge_delete:
                    _del_cnt = 0
                    for _sp in saved_files:
                        try:
                            if os.path.exists(_sp):
                                os.remove(_sp); _del_cnt += 1
                        except Exception:
                            pass
                    print(f"[JZL-短剧导演台] 已按配置删除 {_del_cnt} 个分段视频（仅保留合并结果）")
                out_files = [_merged]
            except Exception as _me:
                errors.append(f"拼接保存失败：{_me}（已保留各分段视频）")

        # 清理临时无損 WAV（已合入成片或不再需要）
        if _merge_wavs:
            for _w in _merge_wavs:
                try:
                    if os.path.exists(_w):
                        os.remove(_w)
                except Exception:
                    pass

        if errors:
            for e in errors:
                print(f"[JZL-短剧导演台] {e}")

        _dur = (total_frames / float(VIDEO_FPS)) if total_frames else 0.0
        print(f"[JZL-短剧导演台] 本次落盘：{out_video_dir}（视频 {len(saved_files)} 个）")
        print(f"[JZL-短剧导演台] 本次成片合计 {total_frames} 帧 = {_dur:.2f} 秒 @24fps（{count} 段）")
        if not saved_files:
            print("[JZL-短剧导演台] ⚠️ 本段未落盘任何视频（未连接模型/CLIP/VAE 或全部分段失败）")
            _report_save_failure(
                "短剧导演台", _out_root, out_video_dir, saved_files, errors,
                [f"已连接：model={model is not None} CLIP={clip is not None} VAE={vae is not None} "
                 f"audio_vae={audio_vae is not None}",
                 f"计划段数={count} 实际分段块={len(shots)} 运行模式={run_mode}",
                 f"保存模式={save_mode}"])
        return io.NodeOutput(prompt_input, ui=_seed_ui)

class JZL_MiniMaxAssetManagerMini(io.ComfyNode):
    """JZL - 🤖 MiniMax-H3短剧导演台Mini — 只做「资产管理和编码」，采样/解码交给下游。

    与 Pro 共享全部 sheding 设定文档/风格/拆解增强逻辑；删除了「视频保存设置」与
    「采样解码设置」；输出「主模型 / 视觉VAE / 音频VAE（穿透输入）+ Latent放大参数 +
    正向条件[] + Latent[]（全部段编码，列表输出）+ 已拆解剧本」。执行：资产加载 →
    LLM 拆解/增强 → 逐段参考编码（ref2va）→ 输出 positive/latent 列表，供下游多段采样。
    """

    @classmethod
    def define_schema(cls):
        story_styles = _story_style_options()
        return io.Schema(
            node_id="JZL_MiniMaxAssetManagerMini",
            display_name="JZL - 🤖 MiniMax-H3短剧导演台Mini",
            category="JZL/MiniMax",
            description="MiniMax-H3 短剧导演台 Mini：只做资产管理 + LLM 拆解/增强 + 参考编码，采样/解码交给下游。输出主模型/视觉VAE/音频VAE（穿透）+ Latent放大参数 + 正向条件[] + Latent[]（全部段）+ 已拆解剧本。",
            inputs=[
                io.Combo.Input("run_mode", options=RUN_MODE_EMOJI,
                    default=RUN_MODE_EMOJI[0], display_name="运行模式",
                    tooltip="故事拆解模式=按情节把故事拆解为N段（不创意扩展）；故事扩写模式=只按风格+字数把故事扩写为丰满正文（不拆解，纯文本经「已拆解剧本」端口输出，不编码）；穿透生成模式=跳过LLM拆解与增强，直接用提示词生成（含[SHOT_START]块则逐段，否则单段）；仅提示词输出=拆解+增强的完整分段剧本文本经「已拆解剧本」输出，不编码"),
                io.String.Input("display_info", display_name="生成详情", default="分辨率：832x480丨每段帧数：192丨共计段数：6丨总帧数：1152丨总时长：48秒",
                    multiline=False, socketless=True,
                    tooltip="只读显示：当前画幅/MP/时长/段数计算出的分辨率、每段帧数、段数、总帧数、总时长（对齐倍数固定 32）"),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIO_OPTIONS, default="16:9 (Widescreen)",
                    display_name="画幅比例", tooltip="画幅比例（分辨率按 MP×1024² 公式自动计算，对齐倍数固定 32）"),
                io.Float.Input("megapixels", display_name="百万像素（MP）", default=0.4, min=0.1, max=16.0, step=0.1,
                    tooltip="总像素数（MP），画幅×MP 决定分辨率"),
                io.Combo.Input("duration", options=list(SEGMENT_DURATION_CHOICES), default=SEGMENT_DURATION_DEFAULT,
                    display_name="每段视频时长", tooltip="每段视频时长（秒）：固定 4~15 秒 或 区间"),
                io.Float.Input("scale_factor", display_name="参考数值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="参考图放大系数"),
                io.Combo.Input("video_count", options=list(VIDEO_COUNT_CHOICES), default=VIDEO_COUNT_DEFAULT,
                    display_name="生成视频数量", tooltip="生成视频数量：默认「剧情决定」= LLM 按剧情时间线长度 ÷ 每段时长自动定段数；也可固定 1/2/3/4/6/9/12/16/20/26/32/40/48/56 段（统一控制：提示词拆解段数）。🪄 穿透生成模式下忽略此设定：一律按提示词实际 [SHOT_START] 段数生成（上限 60 段）"),
                io.Float.Input("upscale_scale", display_name="二采latent放大", default=1.0, min=1.0, max=4.0, step=0.05,
                    tooltip="二采（Ref2va）放大倍数，从「Latent放大参数」输出给下游二采放大节点"),
                # ③提示词：剧本处理器参数（主界面显示）
                io.Combo.Input("story_style", options=story_styles, default=story_styles[0],
                    display_name="故事风格", tooltip="故事风格（剧本处理器按此风格拆解与润色）"),
                io.String.Input("story_name", display_name="故事名称", default="机智罗",
                    tooltip="故事名称（用于保存命名 / 日志）"),
                io.String.Input("external_prompt", display_name="提示词·接线（可连上游文本）", default="",
                    tooltip="提示词接线输入（与 CLIP Text Encode 同类）：此输入框左上角圆点可拖线连接上游 STRING 文本节点；连线后以上游文本为提示词（优先于「节点内提示词」大框；未接且留空则用大框内容）"),
                io.Model.Input("model", display_name="主模型", optional=True),
                io.Clip.Input("clip", display_name="CLIP", optional=True),
                io.Vae.Input("vae", display_name="视觉VAE", optional=True),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True),
                io.String.Input("internal_prompt", display_name="节点内提示词", multiline=True,
                    socketless=True,
                    tooltip="节点内编辑的提示词（提示词来源，随工作流保存）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True,
                    socketless=True, default="",
                    tooltip="本节点独立保存的完整配置 JSON（资产/增强），随工作流保存，节点间互不影响"),
            ],
            outputs=[
                io.Model.Output(display_name="主模型"),
                io.Vae.Output(display_name="视觉VAE"),
                io.Vae.Output(display_name="音频VAE"),
                io.Float.Output(display_name="Latent放大参数"),
                io.Conditioning.Output(display_name="正向条件", is_output_list=True,
                    tooltip="全部段的 ref2va 编码正向条件（每段一个），列表输出方便多段视频生成"),
                io.Latent.Output(display_name="Latent", is_output_list=True,
                    tooltip="全部段的 AV latent（每段一个），列表输出；配合同序号的「正向条件」逐段采样"),
                io.String.Output("script", display_name="已拆解剧本",
                    tooltip="全部LLM处理后的剧本/提示词文本（与 Pro 的「已处理剧本」一致）：拆解+增强后的分段文本"),
            ],
            is_output_node=True,  # Mini 自带拆解输出，单节点即可运行（否则前端报 prompt_no_outputs）
        )

    @classmethod
    def fingerprint_inputs(cls, manager_settings="", **kwargs):
        try:
            cfg = _parse_node_manager_settings(manager_settings)
            enhance = cfg.get("enhance") or {}
            llm_random = (enhance.get("seed_control") or "randomize") == "randomize"
            if llm_random:
                return f"random@{time.time_ns()}"
            gen_keys = ["run_mode", "video_count", "story_style", "duration",
                        "aspect_ratio", "megapixels", "scale_factor", "upscale_scale",
                        "internal_prompt", "external_prompt"]
            gen_sig = {k: kwargs.get(k) for k in gen_keys}
            return f"{cfg}|{gen_sig}"
        except Exception:
            return "no-manager"

    @classmethod
    def execute(cls, run_mode="故事拆解模式", video_count=VIDEO_COUNT_DEFAULT, aspect_ratio="16:9 (Widescreen)",
                megapixels=1.0, duration="5-5", scale_factor=1.0, display_info="",
                story_style="热血战斗", story_name="", upscale_scale=1.5,
                external_prompt=None,
                internal_prompt=None, manager_settings="",
                clip=None, vae=None, audio_vae=None, model=None) -> io.NodeOutput:
        _vc_auto = _parse_video_count(video_count)[0] == "auto"   # 「剧情决定」标记（manager 建后写入）
        video_count = _vc_count(video_count)     # 「剧情决定」→ 占位 6（拆解后按实际分段数覆盖）
        run_mode = _normalize_run_mode(run_mode)
        pure_prompt = run_mode == "仅提示词输出"
        expand_mode = run_mode == "故事扩写模式"
        passthrough = run_mode == "穿透生成模式"
        # 拆解类（故事拆解/仅提示词输出）统一 Decompose；故事扩写模式不走拆解
        story_mode = "拆解模式 (Decompose)"

        # 故事名称必填：生成命名 / 保存目录都依赖故事名。positive/latent 返回空列表（列表输出，
        # 下游按列表处理不会对 None 崩溃）
        story_name = (story_name or "").strip()
        if not story_name:
            print("[JZL-Mini] ⚠️ 必须填写「故事名称」才能生成（已终止）")
            # positive/latent 用 [None] 占位（非空列表）：即使旧版 ComfyUI 不识别 block_execution，
            # 也不会让下游采样 slice_dict 对空列表 v[-1] 崩溃
            return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], "",
                                 block_execution="必须填写「故事名称」后才能生成",
                                 ui={"manager_error": ["必须填写「故事名称」后才能生成"]})

        manager = _parse_node_manager_settings(manager_settings)
        manager["_jzl_auto_count"] = _vc_auto          # 「剧情决定」标记（供 _run_script_processor/日志用）
        duration_lo, duration_hi, duration_desc = _parse_duration_spec(duration)
        enhance = manager.get("enhance") or {}
        assets_cfg = manager.get("assets") or {}
        prompt_lang = (enhance.get("prompt_lang") or "中文 [ZH]").strip() or "中文 [ZH]"

        if not isinstance(internal_prompt, str):
            internal_prompt = ""
        prompt_input = (internal_prompt or "").strip()
        if isinstance(external_prompt, str) and external_prompt.strip():
            prompt_input = external_prompt.strip()  # 接线提示词优先：外部文本覆盖节点内提示词

        # 随机种子（LLM 剧本/增强）+ 生成后控制
        seed_control = (enhance.get("seed_control") or "randomize").strip() or "randomize"
        current_seed = int(enhance.get("seed", 0) or 0)
        if seed_control == "randomize":
            llm_seed = int(torch.randint(0, 0x7fffffffffffffff, (1,)).item())
        else:
            llm_seed = current_seed
        # 穿透生成：段数按提示词实际分段数定（横幅与实际生成段数保持一致）
        _pt_seg = _pt_seg_count(prompt_input) if passthrough else 0
        _seg_lbl = f"{_pt_seg}段" if _pt_seg else _seg_label(manager.get("_jzl_auto_count"), _vc_count(video_count))
        _run_banner("Mini", run_mode, _seg_lbl, story_style, story_name, enhance,
                    f" | 扩写≈{int(enhance.get('expand_words') or 500)}字" if expand_mode else "")

        # ── 纯文本模式提前返回（不编码/不生成，直接经「已拆解剧本」端口输出文本）──
        # 故事扩写模式：只扩写正文；仅提示词输出：拆解+增强。
        # 与 Pro/Max 的纯文本分支等价：解决此前 Mini 因未接 CLIP/VAE 走到编码被 block_execution
        # 挡住「已拆解剧本」下游的问题（需求2）。
        if expand_mode or pure_prompt:
            ri, rv, ra, _ = _build_asset_intro(assets_cfg)
            _pe, _pp, _pv, _pa = _detect_enables(prompt_input, assets_cfg)
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            if not prompt_input:
                _llm_finish(enhance)
                _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
                return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], "", ui=_seed_ui)
            if expand_mode:
                _ew = int(enhance.get("expand_words") or 500)
                _esrc = str(enhance.get("expand_source") or "素材")
                processed, err = _run_story_expander(
                    prompt_input, manager, story_style, story_name, prompt_lang, llm_seed,
                    expand_words=_ew, expand_source=_esrc,
                    ref_image_intro=ri, ref_video_intro=rv, ref_audio_intro=ra,
                    gen_dir=gen_dir)
            else:
                _pcount = _vc_count(video_count)
                processed, err = _run_script_processor(
                    prompt_input, manager, _pcount, story_style, story_name, duration, prompt_lang, llm_seed,
                    ref_image_intro=ri, ref_video_intro=rv, ref_audio_intro=ra,
                    enable_scene=_pe, enable_props=_pp, enable_video=_pv, enable_audio=_pa,
                    mode="拆解模式 (Decompose)", gen_dir=gen_dir)
                if not err and enhance.get("enabled", False):
                    processed, err = _run_prompt_enhancer(
                        processed, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir)
            if err:
                print(f"[JZL-Mini] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], prompt_input or "",
                                     block_execution=f"⚠️ LLM/API 出错，已终止：{err}")
            if processed:
                _save_last_script(story_name, processed)
            _llm_finish(enhance)
            _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)
            # 已拆解剧本正常输出（不 block 下游）；positive/latent 用 [None] 占位
            return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], processed, ui=_seed_ui)

        # 清空重建池，避免旧资产残留
        JZL_ASSET_POOL.clear()
        JZL_BUS_POOL.clear()
        JZL_SLOT_MAP.clear()
        manifest, errors = _load_assets_into_pool(assets_cfg)

        width, height = _resolve_gen_size(aspect_ratio, megapixels)
        length = _resolve_length(duration_hi)
        count = video_count

        ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset = _build_asset_intro(assets_cfg)
        JZL_SLOT_MAP.update(slot_to_asset)
        enable_scene, enable_props, enable_video, enable_audio = _detect_enables(prompt_input, assets_cfg)

        # 故事拆解 / 故事扩写：输入必须是自然语言故事；含六段式标签（已拆解提示词）→ 报错终止
        if run_mode in ("故事拆解模式", "故事扩写模式"):
            _nl_err = _check_natural_language_input(prompt_input)
            if _nl_err:
                print(f"[JZL-Mini] ❌ {_nl_err}（已终止）")
                return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], prompt_input or "",
                                     block_execution=f"❌ {_nl_err}",
                                     ui={"manager_error": [_nl_err]})
        # ① 故事拆解（剧本处理器）
        has_shots = bool(re.search(r'\[SHOT_START\]', prompt_input or ""))
        gen_dir = None
        if not passthrough and enhance.get("story_decompose", True) and not has_shots and prompt_input:
            from .nodes_llama import _next_generation_dir
            gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_script_processor(
                prompt_input, manager, count, story_style, story_name, duration, prompt_lang, llm_seed,
                ref_image_intro=ref_image_intro, ref_video_intro=ref_video_intro, ref_audio_intro=ref_audio_intro,
                enable_scene=enable_scene, enable_props=enable_props,
                enable_video=enable_video, enable_audio=enable_audio, mode=story_mode, gen_dir=gen_dir)
            if err:
                # LLM/API 出错必须终止（不再用错误提示词继续生成）
                print(f"[JZL-Mini] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], prompt_input or "",
                                     block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        # ② 提示词增强
        if not passthrough and enhance.get("enabled", False):
            if gen_dir is None:
                from .nodes_llama import _next_generation_dir
                gen_dir = _next_generation_dir(story_name)
            prompt_input, err = _run_prompt_enhancer(prompt_input, manager, duration, story_style, prompt_lang, llm_seed, story_name, gen_dir)
            if err:
                # LLM/API 出错必须终止
                print(f"[JZL-Mini] LLM/API 出错，终止生成：{err}")
                return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], prompt_input or "",
                                     block_execution=f"⚠️ LLM/API 出错，已终止：{err}")

        # 穿透生成模式：必须使用「拆解模式」拆解好的提示词；格式不对直接终止；段数自动对齐提示词实际分段数
        if passthrough:
            _pt_err, _pt_n = _check_decomposed_script(prompt_input, video_count,
                                                      auto_count=bool(manager.get("_jzl_auto_count")), tag="Mini")
            if _pt_err:
                print(f"[JZL-Mini] ❌ {_pt_err}（已终止）")
                return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], prompt_input or "",
                                     block_execution=f"❌ {_pt_err}",
                                     ui={"manager_error": [_pt_err]})
            count = _pt_n

        # 调度指令规范化 + 场景槽位校正 + 幻想素材清理（与 Pro 一致）
        prompt_input = _normalize_dispatch_slots(prompt_input, slot_to_asset)
        prompt_input = _normalize_scene_slots(prompt_input, slot_to_asset)
        prompt_input = _prune_fantasy_assets(prompt_input, slot_to_asset)

        # 重拍模式：保存最后一次 LLM 拆解/增强后的完整提示词（共享）
        if not passthrough and prompt_input:
            _save_last_script(story_name, prompt_input)

        _llm_finish(enhance)
        _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)

        # ── 分段 + 全部段编码（列表输出，方便多段视频生成）──
        shots = re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', prompt_input or "", re.DOTALL)
        if manager.get("_jzl_auto_count") and shots:
            count = max(1, min(SEGMENT_COUNT_MAX, len(shots)))   # 「剧情决定」：以实际拆解出的分段数为准
        if not shots and prompt_input and prompt_input.strip():
            shots = [prompt_input.strip()]

        can_encode = clip is not None and vae is not None
        if not can_encode:
            need = "CLIP / VAE"
            print(f"[JZL-Mini] ⚠️ 未连接 {need}，仅完成剧本拆解，未编码")
            # positive/latent 用 [None] 占位（非空列表）：即使旧版 ComfyUI 不识别 block_execution，
            # 也不会让下游采样 slice_dict 对空列表 v[-1] 崩溃
            return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], prompt_input,
                                 block_execution=f"未连接 {need}，未完成编码，已阻止下游执行（已拆解剧本已输出）",
                                 ui=_seed_ui)

        positives, latents = [], []
        for i, raw in enumerate(shots):
            raw = raw.strip()
            h3, scene, vid, aud = _parse_four_in_one(raw)
            if not h3:
                h3 = raw if raw else "[未找到H3提示词]"
            # @ 引用
            mention_names, h3 = _extract_mentions(h3)
            # 参考提取
            ref_images, ref_videos, ref_audios, ref_names = _collect_refs_with_names(scene, vid, aud, mention_names)
            # 本段含视频/音频参考但无 audio_vae → 跳过该段编码（列表该位留空）
            if (ref_videos or ref_audios) and audio_vae is None:
                print(f"[JZL-Mini] ⚠️ 第{i + 1}段含视频/音频参考但未连接 audio_vae，跳过编码")
                positives.append([])
                latents.append(None)
                continue
            print(f"[JZL-Mini] 编码第 {i + 1}/{len(shots)} 段（{width}x{height}，{length}帧，参考 {len(ref_images)}图/{len(ref_videos)}视频/{len(ref_audios)}音频）")
            positive, latent = _encode_refs(
                "Mini", f"第 {i + 1}/{len(shots)} 段 ",
                clip, vae, audio_vae, h3, width, height, _seg_len_for(raw, duration_lo),
                ref_images, ref_videos, ref_audios, ref_names, ref_scale=scale_factor, model=model)
            positive = _apply_semantic_bridge(positive, manager, tag="Mini")
            positives.append(positive)
            latents.append(latent)

        if errors:
            for e in errors:
                print(f"[JZL-Mini] {e}")

        # 无有效编码结果（无提示词导致 shots 为空，或全部段被跳过）→ 阻止下游，
        # 用 [None] 占位避免旧版 ComfyUI 对空列表 slice_dict 崩溃
        if not positives or not latents:
            return io.NodeOutput(model, vae, audio_vae, upscale_scale, [None], [None], prompt_input,
                                 block_execution="无有效提示词或未完成编码，已阻止下游执行（已拆解剧本已输出）",
                                 ui=_seed_ui)

        # 输出：主模型 / 视觉VAE / 音频VAE（穿透）+ Latent放大参数 + 正向条件[] + Latent[] + 已拆解剧本
        return io.NodeOutput(model, vae, audio_vae, upscale_scale, positives, latents, prompt_input, ui=_seed_ui)


class JZL_MiniMaxVideoSaveDistributor(io.ComfyNode):
    """视频保存分配 — 接收生成总线，拆成 ≤48 组「图像 + 音频」输出，每组接一个 Video Combine。

    与「JZL - 🤖 MiniMax-H3短剧导演台Pro」通过无线总线（JZL_BUS_POOL）传输：
    生成管理器把每段生成的 (IMAGE, AUDIO) 写入总线池，本节点按组序号读出，
    输出 48 组端口：图像1/音频1 … 图像48/音频48。
    """

    MAX_GROUPS = 48

    @classmethod
    def define_schema(cls):
        inputs = [
            io.String.Input("bus", display_name="生成总线",
                tooltip="接「JZL - 🤖 MiniMax-H3短剧导演台Pro」的「生成总线」输出"),
        ]
        outputs = []
        for i in range(cls.MAX_GROUPS):
            outputs.append(io.Image.Output(display_name=f"图像{i + 1}"))
            outputs.append(io.Audio.Output(display_name=f"音频{i + 1}"))
        return io.Schema(
            node_id="JZL_MiniMaxVideoSaveDistributor",
            display_name="JZL - 💾 视频保存分配",
            category="JZL/MiniMax",
            description="接收生成总线，拆成 ≤48 组「图像+音频」输出，每组接一个 Video Combine。",
            inputs=inputs,
            outputs=outputs,
        )

    @classmethod
    def execute(cls, bus) -> io.NodeOutput:
        groups = 0
        try:
            if bus:
                groups = int((json.loads(bus).get("groups") or 0) if isinstance(bus, str) else 0)
        except Exception:
            groups = 0

        out = []
        for i in range(cls.MAX_GROUPS):
            item = JZL_BUS_POOL.get(i)
            if i < groups and item:
                image = item.get("image")
                audio = item.get("audio")
                out.append(image if image is not None else _empty_image())
                out.append(audio if audio is not None else _empty_audio())
            else:
                out.append(None)
                out.append(None)
        return io.NodeOutput(*out)


def _empty_image():
    """空占位图像（未生成时保证端口有值，避免下游崩溃）。"""
    return torch.zeros((1, 32, 32, 3), dtype=torch.float32)


def _empty_audio():
    """空占位音频。"""
    return {"waveform": torch.zeros((1, 2, 1), dtype=torch.float32), "sample_rate": 32000}


class JZL_MiniMaxVideoViewer(io.ComfyNode):
    """JZL - 🎬 生成视频查看器：画布内直接查看某故事名生成的视频。

    等同「生成视频管理」面板的独立节点：DOM 2 列多宫格缩略图 + 点击预览播放，
    随节点拉大自适应放大（右侧留滚动条位置防挤压）。可接入「已处理剧本」，
    顶部「复制剧本 / 查看剧本（放大编辑）」操作该剧本。无采样解码，不参与生成。
    execute 仅列出 output/jzl/{故事名}/ 下的 mp4，并把接入的剧本文本经 ui 回传前端。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_MiniMaxVideoViewer",
            display_name="JZL - 🎬 生成视频查看器",
            category="JZL/MiniMax",
            description="画布末端查看节点：自动识别「短剧导演台Max」的故事名称，直接读盘 output/jzl/{故事名}/（视频 + 最后一次生成的故事拆解剧本）；DOM 2 列多宫格预览 + 顶部复制/编辑剧本。不再经端口传递（0 个物理输入端口）。",
            inputs=[
                io.String.Input("story_name", display_name="当前故事", default="", advanced=True, socketless=True,
                    tooltip="当前查看的故事（socketless 隐藏无端口）：前端自动识别 Max 故事名或手动下拉选择时写入；execute 按此列盘"),
            ],
            outputs=[io.String.Output("video_files", display_name="视频文件", is_output_list=True,
                tooltip="output/jzl/{故事名}/ 下的 mp4 绝对路径列表（本节点主要作画布末端查看，视频/剧本均直接读盘）")],
            is_output_node=True,  # 末端查看节点：标记输出节点 → 单节点可运行不报 prompt_no_outputs
        )

    @classmethod
    def execute(cls, story_name=""):
        # 末端查看节点：按 story_name（socketless 隐藏 widget，前端自动识别 Max 故事名/手动选择写入）列盘输出视频文件。
        # 剧本展示由前端 REST 直读磁盘（最后一次生成的故事拆解文件）完成，不依赖本 execute 回传。
        _sn = (story_name or "").strip()
        try:
            _d = os.path.join(os.path.abspath(folder_paths.get_output_directory()), "jzl", _safe_story_name(_sn))
            _files = []
            if _sn and os.path.isdir(_d):
                for _fn in sorted(os.listdir(_d)):
                    if _fn.lower().endswith(".mp4") and os.path.isfile(os.path.join(_d, _fn)):
                        _files.append(os.path.join(_d, _fn))
        except Exception:
            _files = []
        return io.NodeOutput(_files if _files else [None])


class JZL_MiniMaxScriptViewer(io.ComfyNode):
    """JZL - 📝 生成剧本查看器：画布内直接查看某故事名最后一次生成/增强的剧本。

    等同「生成视频查看器」的剧本侧：随图自动同步「导演台」的故事名称，
    读盘 output/jzl/{故事名}/ —— 优先「已增强剧本」目录，未开启增强则读「故事拆解」。
    顶部「刷新目录 / 复制剧本 / 编辑剧本 / 选择剧本（可进子文件夹）」，
    下方显示窗直接展示剧本文本。无采样解码，不参与生成。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_MiniMaxScriptViewer",
            display_name="JZL - 📝 生成剧本查看器",
            category="JZL/MiniMax",
            description="画布末端查看节点：自动识别「导演台」的故事名称，直接读盘 output/jzl/{故事名}/ 下最后一次生成或增强的剧本"
                        "（优先「已增强剧本」，未增强则「故事拆解」）；下方显示窗展示全文，「选择剧本」可进子文件夹挑选任意剧本文件。"
                        "不再经端口传递（0 个物理输入端口）。",
            inputs=[
                io.String.Input("story_name", display_name="当前故事", default="", advanced=True, socketless=True,
                    tooltip="当前查看的故事（socketless 隐藏无端口）：前端自动识别导演台故事名或手动下拉选择时写入；execute 按此列盘"),
            ],
            outputs=[io.String.Output("script_files", display_name="剧本文件", is_output_list=True,
                tooltip="output/jzl/{故事名}/ 下所有剧本文档（.txt/.md，含子文件夹）的绝对路径列表（本节点主要作画布末端查看，剧本直接读盘）")],
            is_output_node=True,  # 末端查看节点：标记输出节点 → 单节点可运行不报 prompt_no_outputs
        )

    @classmethod
    def execute(cls, story_name=""):
        # 末端查看节点：按 story_name（socketless 隐藏 widget，前端自动识别/手动选择写入）递归列盘输出剧本文件。
        # 剧本内容展示由前端 REST 直读磁盘（/jzl/story_latest_script 与 /jzl/fs_read）完成，不依赖本 execute 回传。
        _sn = (story_name or "").strip()
        _files = []
        try:
            if _sn:
                _d = os.path.join(os.path.abspath(folder_paths.get_output_directory()), "jzl", _safe_story_name(_sn))
                if os.path.isdir(_d):
                    _found = []
                    for _root, _dirs, _fns in os.walk(_d):
                        _dirs.sort()
                        for _fn in _fns:
                            if _fn.lower().endswith((".txt", ".md")):
                                _fp = os.path.join(_root, _fn)
                                try:
                                    _found.append((os.path.getmtime(_fp), _fp))
                                except Exception:
                                    _found.append((0.0, _fp))
                    # 最新修改的排前面（与「优先最后一次生成」的语义一致）
                    _found.sort(key=lambda t: t[0], reverse=True)
                    _files = [fp for _mt, fp in _found]
        except Exception:
            _files = []
        return io.NodeOutput(_files if _files else [None])
