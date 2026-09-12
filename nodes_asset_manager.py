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
    """run_mode 归一化：剥离表情前缀/后缀 + 旧别名 → 纯模式名（故事拆解/故事扩写/穿透生成/仅提示词输出）。"""
    if not run_mode:
        return "故事拆解模式"
    rm = str(run_mode).strip()
    # 表情前缀剥离（🛠️ 📚 📝 🪄 及其后的空格）
    for emoji in ("🛠️", "📚", "📝", "🪄"):
        if rm.startswith(emoji):
            rm = rm[len(emoji):].lstrip()
            break
    # 旧别名
    rm = _RM_ALIASES.get(rm, rm)
    # 若剥后仍带表情（如「故事拆解模式🛠️」后置），再次 map
    rm = _RM_EMOJI_TO_PLAIN.get(rm, rm)
    if rm not in RUN_MODE_PLAIN:
        # 最后兜底：去所有空格后再查一次（防「🛠️ 故事拆解模式」没剥干净）
        rm = _RM_EMOJI_TO_PLAIN.get(str(rm).replace(" ", ""), rm)
    return rm if rm in RUN_MODE_PLAIN else "故事拆解模式"


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
            "model": "deepseek-v4-flash",
            "api_key": "",
            "base_url": "https://api.deepseek.com/v1",
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
    """节点配置与默认值合并（补齐缺失块），并做旧配置兼容迁移。"""
    merged = json.loads(json.dumps(MANAGER_DEFAULTS, ensure_ascii=False))
    if isinstance(data, dict):
        for k, v in data.items():
            if k in merged and isinstance(v, dict):
                merged[k].update(v)
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


def _validate_script(script, video_count=6):
    """生产规范校验：重拍模式加载提示词时检查，返回错误/警告描述列表（空 = 通过）。"""
    errs = []
    if not script or not script.strip():
        return ["提示词为空"]
    shots = [s.strip() for s in re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', script or "", re.DOTALL)]
    if not shots:
        return ["未识别到 [SHOT_START]~[SHOT_END] 分段（拆解结果可能损坏）"]
    n = max(1, min(48, int(video_count or 6)))
    if len(shots) != n:
        errs.append(f"识别到 {len(shots)} 段，与当前「生成视频数量」{n} 不一致")
    noh3 = [i + 1 for i, s in enumerate(shots) if "===H3_PROMPT===" not in s]
    if noh3:
        errs.append(f"第 {', '.join(str(x) for x in noh3[:6])}{'…' if len(noh3) > 6 else ''} 段缺少 ===H3_PROMPT=== 标记")
    return errs


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


def _collect_slots(raw, kind, limit):
    """按调度指令从全局资产池取 kind 类资产，返回 list[tensor]（≤limit）。

    与「场景/视频/音频调度」节点共用同一套规则（story_nodes 的 normalize_slots /
    _parse_slots / _get_from_pool / _match_name）：槽位名先经 JZL_SLOT_MAP 精确映射为
    资产名，再走共享池匹配（精确→模糊），保证管理器与真实调度节点行为一致。
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
            out.append(data)
    return out


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
    """复刻官方 ref 音频编码：waveform → 归一化 latent。"""
    waveform = audio["waveform"]  # [B, C, L]
    sr = audio["sample_rate"]
    vae_sr = getattr(audio_vae, "audio_sample_rate", 32000)
    if sr != vae_sr:
        waveform = torchaudio.functional.resample(waveform, sr, vae_sr)
    z = audio_vae.encode(waveform[:1].movedim(1, -1))  # [1, 32, 2, T]
    return z, z.shape[-1]


def _encode_ref_to_video(clip, vae, audio_vae, prompt, width, height, length,
                         ref_images=None, ref_videos=None, ref_video_audios=None,
                         ref_audios=None, ref_image_size="match", ref_scale=1.0):
    """复刻官方 MiniMaxH3ReferenceToVideo 编码（va2va / ref2va）。"""
    latent, frame_count = _empty_av_latent(width, height, length)

    ref_items = []   # tokenizer 呈现顺序
    ref_blocks = []  # DiT payload 顺序

    for img in (ref_images or []):
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

    ref_video_audios = ref_video_audios or []
    for idx, video_frames in enumerate(ref_videos or []):
        if video_frames is None:
            continue
        soundtrack = ref_video_audios[idx] if idx < len(ref_video_audios) else None
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
        sample_idx = list(range(0, frames.shape[0], 12))  # FPS//2 = 12 (2fps)
        qwen_frames = frames[sample_idx]
        ref_items.append({"type": "video", "data": qwen_frames,
                          "timestamps": [i / 2.0 for i in range(len(sample_idx))]})
        ref_blocks.append({"kind": "video_audio" if ref_audio_t else "video",
                           "latent_t": z.shape[2], "latent_h": ch // 16, "latent_w": cw // 16,
                           "ref_audio_t": ref_audio_t, "latent": z, "audio_latent": audio_latent})

    for audio in (ref_audios or []):
        if audio is None:
            continue
        audio_latent, ref_audio_t = _encode_ref_audio(audio_vae, audio)
        ref_items.append({"type": "audio"})
        ref_blocks.append({"kind": "audio", "ref_audio_t": ref_audio_t, "audio_latent": audio_latent})

    tokens = clip.tokenize(prompt, minimax_ref_items=ref_items)
    cond = clip.encode_from_tokens_scheduled(tokens)
    if ref_blocks:
        cond = node_helpers.conditioning_set_values(cond, {"minimax_refs": ref_blocks})
    return cond, latent


def _sample_av(model, positive, latent, sample_decode, seed):
    """MiniMax H3 采样：NestedTensor(视频+音频) → 去噪 latent。"""
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


def _resolve_upscaler_model(precision="fp32", explicit_model=""):
    """按精度解析 MiniMax H3 latent 放大模型文件名（官方 fp32/fp16/bf16 是同一权重的三套精度格式）。

    背景：官方把放大模型按精度拆成三个文件（fp32.pth / fp16.safetensors / bf16.safetensors），
    很多用户只下载其中一套（例如只装了 fp16）→ 若代码锁死加载 fp32.pth 必然 "Model file not found"。
    规则：
    - 若用户显式指定了非 MiniMax 标准模型（自定义其它放大模型）→ 尊重用户选择，不做精度联动；
    - 否则（显式是标准 MiniMax 名 / 为空）→ ①先精确扫描磁盘 latent_upscale_models 目录匹配所选精度
      （'_fp32/_fp16/_bf16' + .pth/.safetensors，兼容子目录/不同扩展名），命中返回磁盘真实文件名；
      ②精确精度文件缺失 → 回退到磁盘上任意 minimax_h3_latent_upscaler_3d_* 权重（同权重不同精度，配合
      precision 参数运行等价），并打印提示；③磁盘完全没有 → 兜底返回标准命名并提示需要下载。
    """
    prec = str(precision or "fp32").strip().lower()
    if prec not in ("fp32", "fp16", "bf16"):
        prec = "fp32"
    exp = (explicit_model or "").strip()
    if exp and "minimax_h3_latent_upscaler_3d" not in exp:
        return exp  # 用户自定义非 MiniMax 模型，尊重
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
    _key = {"fp32": "_fp32", "fp16": "_fp16", "bf16": "_bf16"}[prec]
    for _n in _std:  # ① 精确匹配所选精度
        if _key in os.path.basename(_n):
            return _n
    if _std:  # ② 缺失所选精度 → 回退任意可用 MiniMax 放大模型（同权重不同精度，配合 precision 运行等价）
        _fb = _std[0]
        print(f"[JZL-管理器] 未找到 minimax_h3_latent_upscaler_3d_{prec} 权重，自动改用磁盘现有模型：{_fb}（精度参数 {prec} 仍生效）")
        return _fb
    _ext = ".pth" if prec == "fp32" else ".safetensors"
    print(f"[JZL-管理器] 警告: latent_upscale_models 目录没有 minimax_h3_latent_upscaler_3d 模型, 请先下载 {prec} 精度权重放入 ComfyUI/models/latent_upscale_models/")
    return f"minimax_h3_latent_upscaler_3d_{prec}{_ext}"


def _import_upscaler_3d():
    """动态导入 MinimaxH3LatentUpscaler3D（兼容不同安装目录名）。

    云机与本地 custom node 目录名可能不一致（例如带连字符的目录无法用 import 语句导入），
    依次尝试：标准模块名 → 常见变体 → 扫描 custom_nodes 目录按文件路径加载。
    """
    import importlib.util
    import sys as _sys
    # 1) 常见模块名
    for _modname in (
        "Comfyui_Minimax_h3_latent_Upscaler.nodes.minimax_h3_latent_upscaler_3d",
        "ComfyUI_Minimax_h3_latent_Upscaler.nodes.minimax_h3_latent_upscaler_3d",
    ):
        try:
            _m = importlib.import_module(_modname)
            if hasattr(_m, "MinimaxH3LatentUpscaler3D"):
                return getattr(_m, "MinimaxH3LatentUpscaler3D")
        except Exception:
            pass
    # 2) 扫描 custom_nodes 目录，找到 minimax_h3_latent_upscaler_3d.py 按文件加载
    try:
        _base = os.path.abspath(getattr(folder_paths, "base_path", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        _cn = os.path.join(_base, "custom_nodes")
        _found = None
        if os.path.isdir(_cn):
            for _root, _dirs, _files in os.walk(_cn):
                if "minimax_h3_latent_upscaler_3d.py" in _files:
                    _found = os.path.join(_root, "minimax_h3_latent_upscaler_3d.py")
                    break
        if _found:
            _pdir = os.path.dirname(_found)
            if _pdir not in _sys.path:
                _sys.path.insert(0, _pdir)
            try:
                _mod = importlib.import_module("minimax_h3_latent_upscaler_3d")
            except Exception:
                _spec = importlib.util.spec_from_file_location("_jzl_upscaler_3d", _found)
                _mod = importlib.util.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
            if hasattr(_mod, "MinimaxH3LatentUpscaler3D"):
                return getattr(_mod, "MinimaxH3LatentUpscaler3D")
    except Exception:
        pass
    raise RuntimeError("缺少「Minimax H3 Latent Upscaler (3D)」自定义节点（Comfyui_Minimax_h3_latent_Upscaler），请先在 custom_nodes 安装")


def _run_second_sampling(model, positive, samples, sample_decode, second, upscale_scale, seed):
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
    up = MinimaxH3LatentUpscaler3D.execute(**_up_kwargs)
    up_latent = up.args[0]

    # 2.5) ♾️ 无限时长衔接：二采画布被放大，而 positive 里的 keyframe 引导 latent 取自「一采画布」
    #      → PackedLayout 按二采画布算 cond 行数、引导 latent 按自己 h/w 排行 → 行数不匹配必崩。
    #      此处把引导 latent 对齐到二采画布（尺寸一致时是空操作，纯 ref2va 无 keyframe 时也是空操作）。
    positive = _seam_fit_guides_to_canvas(positive, up_latent)

    # 3) 合并音视频潜空间
    merged = LTXVConcatAVLatent.execute(up_latent, audio_latent).args[0]

    # 4) 二次采样：与一采共用 positive（ref2va 参考不受输出分辨率绑定；种子与一采共享，cfg 沿用一采）
    #    Sigmas 模式：scheduler=调度器+步数+降噪；custom=直接用自定义 sigmas 序列（对齐 1190 ManualSigmas→sample_custom）
    cfg = float(sample_decode.get("cfg", 1.0) or 1.0)
    if (second.get("sigmas_mode") or "scheduler") == "custom":
        return _sample_av_custom_sigmas(
            model, positive, merged,
            second.get("custom_sigmas") or "0.8500, 0.6316, 0.3158, 0.0000",
            second.get("sampler") or "euler", seed, cfg)
    second_sd = dict(sample_decode or {})
    second_sd["sampler"] = second.get("sampler") or "euler"
    second_sd["scheduler"] = second.get("scheduler") or "simple"
    second_sd["steps"] = int(second.get("steps", 3) or 3)
    second_sd["denoise"] = float(second.get("denoise", 0.3) or 0.3)
    return _sample_av(model, positive, merged, second_sd, seed)


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


def _cleanup_vram(level="卸载显存模型"):
    """全量显存清理（复刻 XB-BOX VAE解码 的 L2：卸载显存模型），生成结束后统一调用。"""
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
    except Exception as e:
        print(f"[JZL-管理器] 显存清理失败：{e}")


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


def _decode_av(vae, audio_vae, samples, sample_decode=None):
    """NestedTensor latent → (IMAGE [T,H,W,C], AUDIO dict|None)。

    解码前做轻量缓存清理（不卸载模型，避免分段间重载导致日志刷屏/变慢）；
    「卸载显存模型」的全量卸载在生成结束后统一执行。日志风格对齐 XB-BOX VAE解码。
    """
    if (sample_decode or {}).get("decode_cleanup") == "卸载显存模型":
        print("[JZL-管理器] 🧹 解码前清理（卸载显存模型）...")
        _clean_cache()
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
        waveform = audio_vae.first_stage_model.decode(audio_z)  # [B, 2, L]
        audio = {"waveform": waveform, "sample_rate": getattr(audio_vae, "audio_sample_rate", 32000)}
    print("[JZL-管理器] ✅ 解码完成（XB-BOX - VAE解码（原版优化））")
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


def _seam_trim_decoded(image, audio, plan, fps=VIDEO_FPS, settle_frames=0, trim_audio=True):
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
            print(f"[JZL-无限时长] ⚠️ 衔接丢弃量 {drop} 帧 ≥ 本段解码帧数 {frames}：钳制为丢 {max(0, frames - 1)} 帧"
                  f"（请调小「衔接窗口 / 过渡丢弃帧数」）")
            drop = max(0, frames - 1)
        if drop > 0:
            image = image[drop:]
    if not trim_audio:
        print(f"[JZL-无限时长] ⚠️ 「音频同步裁剪」已关闭：画面丢 {drop} 帧而音频不裁 → 音画错位 "
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


def _seam_fit_guides_to_canvas(positive, target_video):
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
            print(f"[JZL-无限时长] 引导 latent 画布对齐：{int(z.shape[-2])}x{int(z.shape[-1])} → {th}x{tw}"
                  f"（{int(z.shape[2])} token，时间轴不变）")
        if not changed:
            return positive
        return node_helpers.conditioning_set_values(positive, {"minimax_keyframes": out_kfs})
    except Exception as e:
        print(f"[JZL-无限时长] ⚠️ 引导 latent 画布对齐失败（按原样继续）：{e}")
        return positive


def _ffmpeg_bin():
    """返回可用的 ffmpeg 可执行文件路径（优先 imageio_ffmpeg 自带的，回退 PATH 中的 ffmpeg）。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


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
    if not getattr(_save_segment_mp4, "_jzl_ver_printed", False):
        _save_segment_mp4._jzl_ver_printed = True
        print(f"[JZL-管理器] 落盘模块加载路径：{os.path.abspath(__file__)}")
    if image is None:
        raise ValueError("图像为空，无法落盘")
    img = image.detach().cpu().float().clamp(0.0, 1.0).mul(255.0).to(torch.uint8).numpy()
    T, H, W, C = img.shape
    if C >= 4:
        img = img[:, :, :, :3]  # 只取 RGB（ffmpeg rgb24 需 3 通道）
    os.makedirs(out_dir, exist_ok=True)
    _seg = f"{int(batch)}次生成分段" if batch else "分段"
    path = os.path.join(out_dir, f"{story_name}_{_seg}{int(index)}_{int(counter):05d}.mp4")
    ff = _ffmpeg_bin()

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
                    raise RuntimeError(f"ffmpeg 落盘失败（libx264 与 mpeg4 均不可用）：{msg} | {e2}")
            # 其他错误（如 ffmpeg 未安装）直接抛出并附诊断
            if "No such file" in msg or "not found" in msg.lower() or "Errno 2" in msg:
                raise RuntimeError(f"未找到 ffmpeg 可执行文件：{ff}（请安装 ffmpeg 或 pip install imageio-ffmpeg）")
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
    ff = _ffmpeg_bin()
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
    """本段视频帧长：优先读分段块里「**时长**: N」元数据（秒）→ 17k+5 对齐帧数；
    无该元数据（直通/纯文本单段）回退统一 duration。区间化后各段时长各异，靠它逐段生效。"""
    m = re.search(r"\*\*时长\*\*\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)", raw or "")
    sec = float(m.group(1)) if m else float(duration or 8)
    sec = max(4.0, min(15.0, sec))
    return _resolve_length(sec)


def _parse_duration_spec(duration):
    """时长设置解析：int/float 或 '5'/'5-5'/'4-10' → (lo, hi, desc)。"""
    v = duration if duration is not None else 8
    _t = str(v).strip()
    _r = re.search(r'^\s*(\d+(?:\.\d+)?)\s*[~\-—]\s*(\d+(?:\.\d+)?)\s*$', _t)
    if _r:
        lo = float(_r.group(1)); hi = float(_r.group(2))
    else:
        try:
            _x = float(_t); lo = _x; hi = _x
        except Exception:
            lo = 8; hi = 8
    lo = max(4.0, min(15.0, lo)); hi = max(lo, min(15.0, hi))
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

# ref_intro 输出排序：类型顺序（角色→场景→道具→分镜→其他；视频/音频）→ 字母顺序
_SLOT_TYPE_ORDER = {
    "角色": 0, "场景": 1, "道具": 2, "分镜": 3, "其他": 4,
    "视频": 0, "音频": 0,
}


def _asset_type_for_slot(kind, typ):
    """资产类型 → 调度槽位类型（build_shot_prompt 对照表识别的类型）。

    「自定义」→「其他」：官方 material_table/slot_map 只识别 角色/场景/道具/分镜/视频/音频/音效/音乐/其他。
    """
    if kind == "image":
        if typ == "自定义":
            return "其他"
        return typ if typ in _IMAGE_SLOT_TYPES else "其他"
    if kind == "video":
        return "视频"
    if kind == "audio":
        return "音频"
    return None


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
            name = (item.get("name") or "").strip()
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
            asset_name = _asset_name(kind, i, item)
            slot_to_asset[slot] = asset_name
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
        # 顶部「**场景**: 」字段（与每段标题/场景元数据对齐，最可靠）；回退 subject_definitions 描述
        top_scenes = []
        m_top = re.search(r'^\*\*场景\*\*\s*[:：]\s*(.+)$', block, re.M)
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
        if not item or item == "无":
            return True
        if ":" in item:
            _t, v = item.split(":", 1)
            return v.strip() in known_slots
        return any(_match_asset(nm, item) for nm in known_names)

    def _fix_fields(block):
        def _repl(m):
            key = m.group(1).strip()
            items = [x.strip() for x in re.split(r'[,，、/;；]', m.group(2)) if x.strip()]
            kept = [x for x in items if _keep(x)]
            return f"**{key}**: " + ("、".join(kept) if kept else "无")
        return re.sub(r'^\*\*((?:角色|场景|道具|视频|音频|音效|音乐|其他))\*\*\s*[:：]\s*(.+)$',
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
        )[0])
    except Exception as _e:
        print(f"[JZL-偏好] build 异常：{_e}")
    custom = (enhance.get("custom_prompt") or "").strip()
    if custom:
        parts.append(custom)
    _joined = "\n".join(parts)
    # 诊断：打印实际注入拆解/增强的偏好文本（确认面板值是否传到后端；可随时删除）
    _raw = {k: pref_cfg.get(k) for k in ("shot_size", "camera_move", "cut_rhythm", "transition",
                                          "music_style", "creative_req", "detail_length", "custom")}
    print(f"[JZL-偏好] 面板原始值={_raw}")
    print(f"[JZL-偏好] 注入文本(前240字)={_joined[:240]!r} | 总长={len(_joined)}")
    return _joined


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
        print("[JZL-llama] 开始加载模型...")
        LLAMA_CPP_STORAGE.load_model(custom_config)
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
    """构造 control_after_generate 的 ui 回传（前端据此更新本节点 manager_settings 里的 seed）。"""
    ret = _persist_seed_control(manager, enhance, seed_control, current_seed, used_seed)
    if ret is None:
        return None
    return {"seed_update": {"seed": ret, "seed_control": seed_control}}


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
    count = max(1, min(48, int(video_count or 6)))

    # 本地后端未选模型：直接给出友好错误（官方 ScriptProcessor 内部不校验空模型，会尝试加载而崩）
    if not is_api and not (custom_config.get("model") or "").strip():
        return story, "[错误] 未选择本地 LLM 模型（请在「文本增强设置」里配置）"

    # video_count → 精确分段数：直接传数字字符串（剧本处理器按数字精确解析，支持 1-24 任意值，
    # 不再就近取 4/6/9/12/16/20/24——否则 video_count=1/2/3 会被错误映射成 4 段）
    seg_label = str(count)

    # 自定义规则：管理器「系统提示词」直接喂给官方 ScriptProcessor 的 custom_rule_path（支持纯文本）
    custom_rule_text = (enhance.get("system_prompt") or "").strip()
    # 强制卸载与「开启增强」联动：增强开启时拆解后不强制卸载（保留模型给增强用，等增强完再卸，省重复加载）
    enhance_enabled = bool(enhance.get("enabled", False))
    script_force_offload = bool(enhance.get("force_offload", False)) and not enhance_enabled

    print(f"[JZL-剧本] 直接调用 JZL_MiniMax_ScriptProcessor | 模式={mode} | 分段={count}段 | "
          f"风格「{story_style}」 | 故事「{story_name or ''}」 | 后端={'API' if is_api else '本地'} | 增强={'开' if enhance_enabled else '关'}")
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
    print(f"[JZL-增强] 开启提示词增强 | 语言={lang} | 风格「{style_name}」 | 种子={seed}（与拆解共享）")
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

    return manifest, errors


def _asset_name(kind, index, item):
    """生成资产名：图片1 角色 孙悟空 / 视频1 主体 跳舞的美女 / 音频1 音色 孙悟空参考音色。"""
    prefix = {"image": "图片", "video": "视频", "audio": "音频"}.get(kind, "资产")
    typ = (item.get("type") or "").strip()
    name = (item.get("name") or "").strip()
    return " ".join(x for x in (f"{prefix}{index + 1}", typ, name) if x)


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
                    multiline=False, advanced=True, socketless=True,
                    tooltip="只读显示：当前画幅/MP/时长/段数计算出的分辨率、每段帧数、段数、总帧数、总时长（对齐倍数固定 32）"),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIO_OPTIONS, default="16:9 (Widescreen)",
                    display_name="画幅比例", tooltip="画幅比例（分辨率按 MP×1024² 公式自动计算，对齐倍数固定 32）"),
                io.Float.Input("megapixels", display_name="百万像素（MP）", default=0.4, min=0.1, max=16.0, step=0.1,
                    tooltip="总像素数（MP），画幅×MP 决定分辨率"),
                io.String.Input("duration", display_name="每段视频时长(秒,可区间)", default="5-5",
                    tooltip="每段视频时长（秒），等同「剧本与镜头处理器」的每段视频时长(秒)"),
                io.Float.Input("scale_factor", display_name="参考数值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="参考图放大系数"),
                io.Float.Input("upscale_scale", display_name="二采latent放大", default=1.0, min=1.0, max=4.0, step=0.05,
                    tooltip="二采（Ref2va）放大倍数"),
                io.Int.Input("video_count", display_name="生成视频数量", default=1, min=1, max=48,
                    tooltip="生成视频数量（分段数，支持 1~48 任意值；统一控制：提示词拆解段数 / 分发列表数 / 采样数 / 分段保存数）"),
                # ③提示词：剧本处理器参数（主界面显示）
                io.Combo.Input("story_style", options=story_styles, default=story_styles[0],
                    display_name="故事风格", tooltip="故事风格（剧本处理器按此风格拆解与润色）"),
                io.String.Input("story_name", display_name="故事名称", default="机智罗",
                    tooltip="故事名称（用于保存命名 / 日志）"),
                io.String.Input("external_prompt", display_name="提示词·接线（可连上游文本）", default="",
                    tooltip="提示词接线输入（与 CLIP Text Encode 同类）：此输入框左上角圆点可拖线连接上游 STRING 文本节点；连线后以上游文本为提示词（优先于「节点内提示词」大框；未接且留空则用大框内容）"),
                io.Model.Input("model", display_name="主模型", optional=True, advanced=True),
                io.Clip.Input("clip", display_name="CLIP", optional=True, advanced=True),
                io.Vae.Input("vae", display_name="视觉VAE", optional=True, advanced=True),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True, advanced=True),
                io.String.Input("internal_prompt", display_name="节点内提示词", multiline=True, advanced=True,
                    socketless=True,
                    tooltip="节点内编辑的提示词（提示词来源，随工作流保存）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True, advanced=True,
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
    def execute(cls, run_mode="拆解故事模式", video_count=6, aspect_ratio="16:9 (Widescreen)",
                megapixels=1.0, duration="5-5", scale_factor=1.0, upscale_scale=1.5, display_info="",
                story_style="热血战斗", story_name="",
                external_prompt=None,
                internal_prompt=None, manager_settings="",
                clip=None, vae=None, audio_vae=None, model=None) -> io.NodeOutput:
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
                                  ui={"manager_error": "必须填写「故事名称」后才能生成"})

        # 节点独立配置：工作流内保存的 manager_settings 优先；空则回退全局（旧工作流兼容）
        manager = _parse_node_manager_settings(manager_settings)
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
        _mode_hint = "（故事扩写模式：只扩写故事正文→已处理剧本输出）" if expand_mode else (
            "（仅提示词输出：拆解+增强→已处理剧本输出，不生成视频）" if pure_prompt else (
            "（穿透生成模式：跳过LLM拆解/增强，直接用提示词生成）" if passthrough else (
            "（故事拆解模式→生成）" if run_mode == "故事拆解模式" else "（故事扩写模式→生成）")))
        print(f"[JZL-管理器] 运行模式={run_mode} | LLM种子={llm_seed}({seed_control}) | "
              f"采样种子模式={sample_decode.get('seed_mode', 'randomize')} | 共{max(1, min(48, int(video_count or 6)))}段{_mode_hint}")

        # 故事扩写模式：只按「故事风格 + 扩写字数」把故事扩写为丰满正文，不拆解；
        # 纯文本经「已处理剧本」端口输出 + 落盘；无需 model/clip/vae/audio_vae。
        if expand_mode:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput([None], [None], "")
            _ew = int(enhance.get("expand_words") or 500)
            _esrc = str(enhance.get("expand_source") or "素材")
            print(f"[JZL-管理器] 故事扩写模式：扩写到约 {_ew} 字正文 | 范围={'按素材' if _esrc != '自由' else '自由发挥'}（纯文本，不拆解）")
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
            _pcount = max(1, min(48, int(video_count)))
            ri, rv, ra, _ = _build_asset_intro(assets_cfg)
            _pe, _pp, _pv, _pa = _detect_enables(prompt_input, assets_cfg)
            print(f"[JZL-管理器] 仅提示词输出：按「拆解模式(Decompose)」拆解为 {_pcount} 段 + 增强（纯文本，不生成视频）")
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
        count = max(1, min(48, int(video_count)))


        # ── ③提示词：资产介绍 + 启用判断 + 槽位映射（供调度匹配）──
        ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset = _build_asset_intro(assets_cfg)
        JZL_SLOT_MAP.update(slot_to_asset)
        enable_scene, enable_props, enable_video, enable_audio = _detect_enables(prompt_input, assets_cfg)

        # ① 故事拆解（剧本处理器）：故事 → 分段（输入已是分段文本时跳过；直通模式跳过 LLM）
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

        # 直通模式：无 [SHOT_START] 块 → 整段提示词当单段生成（不用 video_count 复制）
        if passthrough and not has_shots:
            count = 1

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

        base_seed = int(sample_decode.get("seed", 0) or 0)
        seed_mode = sample_decode.get("seed_mode", "randomize") or "randomize"

        bus_items = []
        saved_segments = []
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
            ref_images = _collect_slots(scene, "image", 9)
            ref_videos = _collect_slots(vid, "video", 3)
            ref_audios = _collect_slots(aud, "audio", 3)

            # @ 引用补充参考
            for mname in mention_names:
                kind, data = _get_asset_by_name(mname)
                if data is None:
                    continue
                if kind == "image" and len(ref_images) < 9:
                    ref_images.append(data)
                elif kind == "video" and len(ref_videos) < 3:
                    ref_videos.append(data)
                elif kind == "audio" and len(ref_audios) < 3:
                    ref_audios.append(data)

            # 12 参考总上限（官方文档）：图片 + 视频 + 音频（视频音轨随视频计）≤ 12
            # 兜底截断优先级（用户选择：图 > 音频 > 视频）——仅纯文本提示词（无前端 @去重限制）可能超限
            while len(ref_images) + len(ref_videos) + len(ref_audios) > 12:
                if ref_videos:
                    ref_videos.pop()
                elif ref_audios:
                    ref_audios.pop()
                elif ref_images:
                    ref_images.pop()

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

            print(f"[JZL-管理器] ── 分段 {i + 1}/{count} 生成开始（{mode}，种子 {seed}）──")
            try:
                # 始终 ref2va 编码（对齐 1146）；前 N 个音频与视频配对为音轨，其余为独立音频（不重复编码）
                positive, latent = _encode_ref_to_video(
                    clip, vae, audio_vae, h3, width, height, _seg_len_for(raw, duration_lo),
                    ref_images=ref_images, ref_videos=ref_videos,
                    ref_video_audios=ref_audios[:len(ref_videos)],
                    ref_audios=ref_audios[len(ref_videos):],
                    ref_scale=scale_factor)

                samples = _sample_av(model, positive, latent, sample_decode, seed)
                _second = (sample_decode or {}).get("second") or {}
                if _second.get("enabled"):
                    if (_second.get("sigmas_mode") or "scheduler") == "custom":
                        print(f"[JZL-管理器] 分段 {i + 1}/{count} 二次采样：视频latent放大 {upscale_scale}x（自定义 Sigmas：{_second.get('custom_sigmas') or ''}，{_second.get('sampler', 'euler')}）…")
                    else:
                        print(f"[JZL-管理器] 分段 {i + 1}/{count} 二次采样：视频latent放大 {upscale_scale}x（{_second.get('sampler', 'euler')}/{_second.get('scheduler', 'simple')}，{_second.get('steps', 3)}步，denoise {_second.get('denoise', 0.3)}）…")
                    samples = _run_second_sampling(model, positive, samples, sample_decode, _second, upscale_scale, seed)
                print(f"[JZL-管理器] 分段 {i + 1}/{count} 采样完成（{sample_decode.get('steps', 4)} 步 / {sample_decode.get('sampler', 'res_multistep')}）{'（含二次采样）' if _second.get('enabled') else ''}，开始解码…")
                image, audio = _decode_av(vae, audio_vae, samples, sample_decode)

                JZL_BUS_POOL[i] = {"image": image, "audio": audio}
                # 分段视频自动保存：每段解码完立即用 ffmpeg 落盘 mp4（不依赖下游 VHS）
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
                        print(f"[JZL-管理器] 分段 {i + 1}/{count} 已自动保存：{_sp}")
                    except Exception as _se:
                        errors.append(f"第{i + 1}段自动保存失败：{_se}")
                _frames = int(image.shape[0]) if image is not None else 0
                print(f"[JZL-管理器] 分段 {i + 1}/{count} 生成完成：图像 {_frames} 帧 + 音频{'✓' if audio is not None else '✗'}，已写入总线")
                bus_items.append({
                    "index": i, "mode": mode, "prompt": h3,
                    "has_image": True, "has_audio": audio is not None,
                    "frames": _frames,
                })
            except Exception as e:
                err = str(e)
                if "no kernel image" in err:
                    err += (f"\n  [JZL提示] CUDA/ROCm 内核与显卡架构不匹配（no kernel image）：当前 torch 没有该 GPU 的编译内核。"
                            f"诊断：{_gpu_diag_hint()}\n"
                            f"  请确认云机显卡型号与 torch/CUDA 版本匹配（本地 Z 盘 torch 是 ROCm 版 2.9.1+rocm7.13 + AMD 核显，"
                            f"无法用于其他架构显卡生成）。")
                elif "CUDA error" in err or "cudaError" in err or "RuntimeError" in err:
                    import traceback as _tb
                    _tb.print_exc()
                    err += (f"\n  [JZL提示] CUDA 运行时错误（非内核不匹配），诊断：{_gpu_diag_hint()}\n"
                            f"  可能原因：驱动/算子在 Blackwell(sm120) 不兼容、dtype/精度、或某算子非法参数（如 attention 后端/参考张量）。"
                            f"完整堆栈已打印到上方日志，请按其定位。")
                errors.append(f"第{i + 1}段生成失败：{err}")
                bus_items.append({
                    "index": i, "mode": mode, "prompt": h3,
                    "has_image": False, "has_audio": False,
                    "error": f"生成失败：{err}",
                })

        # 采样种子回写：randomize 时把实际使用的种子（第 1 段）回传前端 → 刷新后显示真实种子
        if seed_mode == "randomize" and first_seed is not None:
            _samp_ui = {"seed_update_sample": {"seed": int(first_seed), "seed_control": seed_mode}}
            if _seed_ui:
                _seed_ui = dict(_seed_ui)
                _seed_ui.update(_samp_ui)
            else:
                _seed_ui = _samp_ui

        # 生成结束：按「解码前清理」配置执行最终显存清理（卸载显存模型时释放，见日志确认）
        if (sample_decode or {}).get("decode_cleanup") == "卸载显存模型":
            _cleanup_vram("卸载显存模型")
            print("[JZL-管理器] 显存清理完成（卸载显存模型）")

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
                    print(f"[JZL-管理器] 分段视频已按顺序合并（音频统一编码一次）：{_merged}")
                    if auto_merge_delete:
                        _del_n = 0
                        for _p in saved_segments:
                            try:
                                os.remove(_p)
                                _del_n += 1
                            except Exception:
                                pass
                        print(f"[JZL-管理器] 已删除 {_del_n} 个分段视频（合并后清理）")
                except Exception as _me2:
                    errors.append(f"合并失败：{_me2}")
            else:
                errors.append("合并失败：没有可合并的分段文件")
            if _merge_wavs:   # 先清临时无损 WAV（_merge_dir 内的随 rmtree 一起清）
                _wrm = 0
                for _w in _merge_wavs:
                    try:
                        if os.path.exists(_w):
                            os.remove(_w); _wrm += 1
                    except Exception:
                        pass
                if _wrm:
                    print(f"[JZL-管理器] 已清理 {_wrm} 个分段临时音频（无损 WAV）")
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
                    multiline=False, advanced=True, socketless=True,
                    tooltip="只读显示：当前画幅/MP/时长/段数计算出的分辨率、每段帧数、段数、总帧数、总时长（对齐倍数固定 32）"),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIO_OPTIONS, default="16:9 (Widescreen)",
                    display_name="画幅比例", tooltip="画幅比例（分辨率按 MP×1024² 公式自动计算，对齐倍数固定 32）"),
                io.Float.Input("megapixels", display_name="百万像素（MP）", default=0.4, min=0.1, max=16.0, step=0.1,
                    tooltip="总像素数（MP），画幅×MP 决定分辨率"),
                io.String.Input("duration", display_name="每段视频时长(秒,可区间)", default="5-5",
                    tooltip="每段视频时长（秒），等同「剧本与镜头处理器」的每段视频时长(秒)"),
                io.Float.Input("scale_factor", display_name="参考数值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="参考图放大系数"),
                io.Float.Input("upscale_scale", display_name="二采latent放大", default=1.0, min=1.0, max=4.0, step=0.05,
                    tooltip="二采（Ref2va）放大倍数"),
                io.Int.Input("video_count", display_name="生成视频数量", default=1, min=1, max=48,
                    tooltip="生成视频数量（分段数，支持 1~48 任意值；逐段即时落盘，段数再多也不叠加内存）"),
                io.Combo.Input("story_style", options=story_styles, default=story_styles[0],
                    display_name="故事风格", tooltip="故事风格（剧本处理器按此风格拆解与润色）"),
                io.String.Input("story_name", display_name="故事名称", default="机智罗",
                    tooltip="故事名称（用于保存命名 / 落盘目录 output/jzl/{故事名} / 生成视频管理）"),
                io.String.Input("external_prompt", display_name="提示词·接线（可连上游文本）", default="",
                    tooltip="提示词接线输入（与 CLIP Text Encode 同类）：此输入框左上角圆点可拖线连接上游 STRING 文本节点；连线后以上游文本为提示词（优先于「节点内提示词」大框；未接且留空则用大框内容）"),
                io.Model.Input("model", display_name="主模型", optional=True, advanced=True),
                io.Clip.Input("clip", display_name="CLIP", optional=True, advanced=True),
                io.Vae.Input("vae", display_name="视觉VAE", optional=True, advanced=True),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True, advanced=True),
                io.String.Input("internal_prompt", display_name="节点内提示词", multiline=True, advanced=True,
                    socketless=True,
                    tooltip="节点内编辑的提示词（提示词来源，随工作流保存）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True, advanced=True,
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
    def execute(cls, run_mode="拆解故事模式", video_count=6, aspect_ratio="16:9 (Widescreen)",
                megapixels=1.0, duration="5-5", scale_factor=1.0, upscale_scale=1.5, display_info="",
                story_style="热血战斗", story_name="",
                external_prompt=None,
                internal_prompt=None, manager_settings="",
                clip=None, vae=None, audio_vae=None, model=None) -> io.NodeOutput:
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
                                  ui={"manager_error": "必须填写「故事名称」后才能生成"})

        manager = _parse_node_manager_settings(manager_settings)
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
        print(f"[JZL-Max] 运行模式={run_mode} | LLM种子={llm_seed}({seed_control}) | "
              f"采样种子模式={sample_decode.get('seed_mode', 'randomize')} | 共{max(1, min(48, int(video_count or 6)))}段（逐段即时落盘）")

        # 故事扩写模式：只按「故事风格 + 扩写字数」扩写正文，不拆解；纯文本输出，不生成视频
        if expand_mode:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput("")
            _ew = int(enhance.get("expand_words") or 500)
            _esrc = str(enhance.get("expand_source") or "素材")
            print(f"[JZL-Max] 故事扩写模式：扩写到约 {_ew} 字正文 | 范围={'按素材' if _esrc != '自由' else '自由发挥'}（纯文本，不生成视频）")
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
            _pcount = max(1, min(48, int(video_count)))
            ri, rv, ra, _ = _build_asset_intro(assets_cfg)
            _pe, _pp, _pv, _pa = _detect_enables(prompt_input, assets_cfg)
            print("[JZL-Max] 仅提示词输出：按「拆解模式(Decompose)」拆解 + 增强（纯文本，不生成视频）")
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
        count = max(1, min(48, int(video_count)))

        ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset = _build_asset_intro(assets_cfg)
        JZL_SLOT_MAP.update(slot_to_asset)
        enable_scene, enable_props, enable_video, enable_audio = _detect_enables(prompt_input, assets_cfg)

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

        if passthrough and not has_shots:
            count = 1

        prompt_input = _normalize_dispatch_slots(prompt_input, slot_to_asset)
        prompt_input = _normalize_scene_slots(prompt_input, slot_to_asset)
        prompt_input = _prune_fantasy_assets(prompt_input, slot_to_asset)

        if not passthrough and prompt_input:
            _save_last_script(story_name, prompt_input)

        _llm_finish(enhance)
        _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)

        # ── 分段：按 [SHOT_START] 块切分（等同 8888 里「列表分发按编号」）──
        shots = re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', prompt_input or "", re.DOTALL)
        base_seed = int(sample_decode.get("seed", 0) or 0)
        seed_mode = sample_decode.get("seed_mode", "randomize") or "randomize"

        saved_files = []          # 本次运行逐段落盘成功的 mp4 绝对路径（只存路径字符串，不存 tensor）
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
            ref_images = _collect_slots(scene, "image", 9)
            ref_videos = _collect_slots(vid, "video", 3)
            ref_audios = _collect_slots(aud, "audio", 3)
            for mname in mention_names:
                kind, data = _get_asset_by_name(mname)
                if data is None:
                    continue
                if kind == "image" and len(ref_images) < 9:
                    ref_images.append(data)
                elif kind == "video" and len(ref_videos) < 3:
                    ref_videos.append(data)
                elif kind == "audio" and len(ref_audios) < 3:
                    ref_audios.append(data)
            # 12 参考总上限（官方文档）：图 + 视频 + 音频（视频音轨随视频计）≤ 12，兜底截断 图>音频>视频
            while len(ref_images) + len(ref_videos) + len(ref_audios) > 12:
                if ref_videos:
                    ref_videos.pop()
                elif ref_audios:
                    ref_audios.pop()
                elif ref_images:
                    ref_images.pop()

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

            print(f"[JZL-Max] ── 分段 {i + 1}/{count} 生成开始（{mode}，种子 {seed}）──")
            try:
                # 编码（ref2va，复刻 8888 ReferenceToVideo2）
                positive, latent = _encode_ref_to_video(
                    clip, vae, audio_vae, h3, width, height, _seg_len_for(raw, duration_lo),
                    ref_images=ref_images, ref_videos=ref_videos,
                    ref_video_audios=ref_audios[:len(ref_videos)],
                    ref_audios=ref_audios[len(ref_videos):],
                    ref_scale=scale_factor)
                # 第一次采样
                samples = _sample_av(model, positive, latent, sample_decode, seed)
                # latent 放大 + 第二次采样（可选，复刻 8888 一采→LTXV分离→Upscaler3D→合并→二采）
                _second = (sample_decode or {}).get("second") or {}
                if _second.get("enabled"):
                    print(f"[JZL-Max] 分段 {i + 1}/{count} 二次采样：视频latent放大 {upscale_scale}x（{_second.get('sampler', 'euler')}/{_second.get('scheduler', 'simple')}，{_second.get('steps', 3)}步，denoise {_second.get('denoise', 0.3)}）…")
                    samples = _run_second_sampling(model, positive, samples, sample_decode, _second, upscale_scale, seed)
                print(f"[JZL-Max] 分段 {i + 1}/{count} 采样完成，开始解码…")
                image, audio = _decode_av(vae, audio_vae, samples, sample_decode)

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
                print(f"[JZL-Max] 分段 {i + 1}/{count} 已即时落盘：{_sp}（{_frames} 帧）")

                # ★ Max 恒定：释放该段全部显存/内存（解码帧/音频/latent/条件全部丢弃）→ 跑下段占用不叠加
                try:
                    del image, audio, samples, positive, latent
                except Exception:
                    pass
                _clean_cache()
                print(f"[JZL-Max] 分段 {i + 1}/{count} 完成，已释放该段显存/内存（跑{count}段与跑1段占用不叠加）")
            except Exception as e:
                err = str(e)
                if "no kernel image" in err:
                    err += (f"\n  [JZL提示] CUDA/ROCm 内核与显卡架构不匹配（no kernel image）：当前 torch 没有该 GPU 的编译内核。"
                            f"诊断：{_gpu_diag_hint()}\n"
                            f"  请确认云机显卡型号与 torch/CUDA 版本匹配。")
                elif "CUDA error" in err or "cudaError" in err or "RuntimeError" in err:
                    import traceback as _tb
                    _tb.print_exc()
                    err += (f"\n  [JZL提示] CUDA 运行时错误（非内核不匹配），诊断：{_gpu_diag_hint()}\n"
                            f"  可能原因：驱动/算子在 Blackwell(sm120) 不兼容、dtype/精度、或某算子非法参数（如 attention 后端/参考张量）。"
                            f"完整堆栈已打印到上方日志，请按其定位。")
                errors.append(f"第{i + 1}段生成失败：{err}")
                print(f"[JZL-Max] 第{i + 1}段生成失败：{err}")

        # 采样种子回写：randomize 时把第 1 段实际种子回传前端
        if seed_mode == "randomize" and first_seed is not None:
            _samp_ui = {"seed_update_sample": {"seed": int(first_seed), "seed_control": seed_mode}}
            if _seed_ui:
                _seed_ui = dict(_seed_ui)
                _seed_ui.update(_samp_ui)
            else:
                _seed_ui = _samp_ui

        # 生成结束：按「解码前清理」配置执行最终显存清理
        if (sample_decode or {}).get("decode_cleanup") == "卸载显存模型":
            _cleanup_vram("卸载显存模型")
            print("[JZL-Max] 显存清理完成（卸载显存模型）")

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
                print(f"[JZL-Max] 拼接保存完成（读盘 {len(saved_files)} 段 concat 合并，音频统一编码）：{_merged}")
                if auto_merge_delete:
                    _del_cnt = 0
                    for _sp in saved_files:
                        try:
                            if os.path.exists(_sp):
                                os.remove(_sp); _del_cnt += 1
                        except Exception:
                            pass
                    print(f"[JZL-Max] 已按「拼接保存后删除分段视频」删除 {_del_cnt} 个分段，仅保留合并结果：{_merged}")
                out_files = [_merged]
            except Exception as _me:
                errors.append(f"拼接保存失败：{_me}（已保留各分段视频）")

        # 清理临时无損 WAV（已合入成片或不再需要）
        if _merge_wavs:
            _wrm = 0
            for _w in _merge_wavs:
                try:
                    if os.path.exists(_w):
                        os.remove(_w); _wrm += 1
                except Exception:
                    pass
            if _wrm:
                print(f"[JZL-Max] 已清理 {_wrm} 个分段临时音频（无损 WAV）")

        if errors:
            for e in errors:
                print(f"[JZL-Max] {e}")

        # Max 0 输出：视频已即时落盘到 output/jzl/{故事名}/；剧本已写「最近提示词.json」+ 第N次生成/故事拆解/
        # （「生成视频查看器」改直接读盘获取故事名/剧本/视频，不再经端口传递）
        print(f"[JZL-Max] 本次落盘目录：{out_video_dir}（视频 {len(saved_files)} 个，剧本 {len(prompt_input)} 字）")
        if not saved_files:
            print("[JZL-Max] ⚠️ 本段未落盘任何视频（未连接模型/CLIP/VAE 或全部分段失败）")
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
                    multiline=False, advanced=True, socketless=True,
                    tooltip="只读显示：当前画幅/MP/时长/段数计算出的分辨率、每段帧数、段数、总帧数、总时长（对齐倍数固定 32）"),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIO_OPTIONS, default="16:9 (Widescreen)",
                    display_name="画幅比例", tooltip="画幅比例（分辨率按 MP×1024² 公式自动计算，对齐倍数固定 32）"),
                io.Float.Input("megapixels", display_name="百万像素（MP）", default=0.4, min=0.1, max=16.0, step=0.1,
                    tooltip="总像素数（MP），画幅×MP 决定分辨率"),
                io.String.Input("duration", display_name="每段视频时长(秒,可区间)", default="5-5",
                    tooltip="每段视频时长（秒），等同「剧本与镜头处理器」的每段视频时长(秒)。无限时长下每段会往后损失「衔接窗口」帧长（默认 22 帧≈0.92 秒，被下一段复用），总时长 = Σ段长 − 窗口×(段数−1)"),
                io.Float.Input("scale_factor", display_name="参考数值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="参考图放大系数"),
                io.Float.Input("upscale_scale", display_name="二采latent放大", default=1.0, min=1.0, max=4.0, step=0.05,
                    tooltip="二采（Ref2va）放大倍数。开启二采不影响段间衔接：衔接恒用「一采 latent」，二采结果只用于解码落盘"),
                io.Int.Input("video_count", display_name="生成视频数量", default=1, min=1, max=48,
                    tooltip="段数（1~48）。1 段=普通单段生成（不启用衔接）；≥2 段=无限时长链路。逐段即时落盘，段数再多也不叠加内存"),
                io.Combo.Input("story_style", options=story_styles, default=story_styles[0],
                    display_name="故事风格", tooltip="故事风格（剧本处理器按此风格拆解与润色）"),
                io.String.Input("story_name", display_name="故事名称", default="机智罗",
                    tooltip="故事名称（用于保存命名 / 落盘目录 output/jzl/{故事名} / 生成视频管理）"),
                io.String.Input("external_prompt", display_name="提示词·接线（可连上游文本）", default="",
                    tooltip="提示词接线输入（与 CLIP Text Encode 同类）：连线后以上游文本为提示词（优先于「节点内提示词」大框）"),
                io.Model.Input("model", display_name="主模型", optional=True, advanced=True),
                io.Clip.Input("clip", display_name="CLIP", optional=True, advanced=True),
                io.Vae.Input("vae", display_name="视觉VAE", optional=True, advanced=True),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True, advanced=True),
                io.String.Input("internal_prompt", display_name="节点内提示词", multiline=True, advanced=True,
                    socketless=True,
                    tooltip="节点内编辑的提示词（提示词来源，随工作流保存）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True, advanced=True,
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
    def execute(cls, run_mode="拆解故事模式", video_count=6, aspect_ratio="16:9 (Widescreen)",
                megapixels=1.0, duration="5-5", scale_factor=1.0, upscale_scale=1.5, display_info="",
                story_style="热血战斗", story_name="",
                external_prompt=None,
                internal_prompt=None, manager_settings="",
                clip=None, vae=None, audio_vae=None, model=None) -> io.NodeOutput:
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
                                  ui={"manager_error": "必须填写「故事名称」后才能生成"})

        manager = _parse_node_manager_settings(manager_settings)
        duration_lo, duration_hi, duration_desc = _parse_duration_spec(duration)
        enhance = manager.get("enhance") or {}
        assets_cfg = manager.get("assets") or {}
        sample_decode = manager.get("sample_decode") or {}
        # ♾️ 衔接设置（面板「🎭 采样解码设置 → 无限时长衔接」；缺省即官方 Motion Context 基线）
        _inf = sample_decode.get("infinite") or {}
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
        _seg_count_probe = max(1, min(48, int(video_count or 6)))
        seam_runway_sec = _seam_runway_seconds(seam_window_cfg, seam_settle,
                                               enabled=(_seg_count_probe > 1 and seam_trim_mode != "none"))
        if seam_runway_sec > 0:
            print(f"[JZL-无限时长] 段首衔接跑道 = {seam_runway_sec:.3f} 秒"
                  f"（窗口 {seam_window_cfg} + 过渡 {seam_settle} = {seam_window_cfg + seam_settle} 帧 / 24fps）"
                  f"→ 第 2 段起生成时长 = 设定时长 + 跑道，成片裁掉跑道 ⇒ **落盘时长 = 设定时长**")
        _save_cfg = manager.get("save") or {}
        save_mode = (_save_cfg.get("mode") or "分段保存").strip() or "分段保存"
        auto_merge_delete = bool(_save_cfg.get("auto_merge_delete"))
        prompt_lang = (enhance.get("prompt_lang") or "中文 [ZH]").strip() or "中文 [ZH]"

        if not isinstance(internal_prompt, str):
            internal_prompt = ""
        prompt_input = (internal_prompt or "").strip()
        if isinstance(external_prompt, str) and external_prompt.strip():
            prompt_input = external_prompt.strip()  # 接线提示词优先

        seed_control = (enhance.get("seed_control") or "randomize").strip() or "randomize"
        current_seed = int(enhance.get("seed", 0) or 0)
        if seed_control == "randomize":
            llm_seed = int(torch.randint(0, 0x7fffffffffffffff, (1,)).item())
        else:
            llm_seed = current_seed
        _trim_label = {"pixel": "像素域裁切（保解码相位）", "latent": "latent域裁切（旧·诊断）",
                       "none": "不裁切（诊断）"}.get(seam_trim_mode, seam_trim_mode)
        print(f"[JZL-无限时长] 运行模式={run_mode} | LLM种子={llm_seed}({seed_control}) | "
              f"采样种子模式={sample_decode.get('seed_mode', 'randomize')} | "
              f"共{max(1, min(48, int(video_count or 6)))}段 | 衔接窗口={seam_window_cfg}帧 | "
              f"引导来源={'上段尾帧→VAE重编码' if seam_guide_source == 'pixel' else '一采latent尾部'} | "
              f"裁切方式={_trim_label} | 过渡丢弃={seam_settle}帧 | "
              f"音频衔接={'开' if seam_audio_link else '关'} | 音频同步裁剪={'开' if seam_trim_audio else '关'} | "
              f"二采引导={'上段二采latent尾部' if seam_second_guide == 'second' else '上段一采latent（插值到二采画布）'}"
              f"（逐段即时落盘）")

        # ── 故事扩写模式：只按「故事风格 + 扩写字数」扩写正文，不拆解；纯文本输出，不生成视频 ──
        if expand_mode:
            if not prompt_input:
                _llm_finish(enhance)
                return io.NodeOutput("")
            _ew = int(enhance.get("expand_words") or 500)
            _esrc = str(enhance.get("expand_source") or "素材")
            print(f"[JZL-无限时长] 故事扩写模式：扩写到约 {_ew} 字正文 | 范围={'按素材' if _esrc != '自由' else '自由发挥'}（纯文本，不生成视频）")
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
            _pcount = max(1, min(48, int(video_count)))
            ri, rv, ra, _ = _build_asset_intro(assets_cfg)
            _pe, _pp, _pv, _pa = _detect_enables(prompt_input, assets_cfg)
            print("[JZL-无限时长] 仅提示词输出：按「拆解模式(Decompose)」拆解 + 增强（纯文本，不生成视频）")
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
        count = max(1, min(48, int(video_count)))

        ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset = _build_asset_intro(assets_cfg)
        JZL_SLOT_MAP.update(slot_to_asset)
        enable_scene, enable_props, enable_video, enable_audio = _detect_enables(prompt_input, assets_cfg)

        has_shots = bool(re.search(r'\[SHOT_START\]', prompt_input or ""))
        # ⭐ 衔接跑道：段 2+ 的「生成时长 = 设定时长 + 跑道」（段首跑道会被裁掉 ⇒ 落盘时长 = 设定时长）。
        #    `gen_duration_lo` = 生成时长下限（块内缺 **时长** 字段时的兜底，也按生成时长算）。
        gen_duration_lo = duration_lo + seam_runway_sec
        if seam_runway_sec > 0 and has_shots:
            print(f"[JZL-无限时长] ⚠️ 你自带了 [SHOT_START] 剧本块：各段 **时长** 按「生成时长」解释，"
                  f"本节点会再裁掉段首 {seam_window_cfg + seam_settle} 帧跑道 → 落盘 = 生成 − 跑道。"
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

        if passthrough and not has_shots:
            count = 1

        prompt_input = _normalize_dispatch_slots(prompt_input, slot_to_asset)
        prompt_input = _normalize_scene_slots(prompt_input, slot_to_asset)
        prompt_input = _prune_fantasy_assets(prompt_input, slot_to_asset)

        if not passthrough and prompt_input:
            _save_last_script(story_name, prompt_input)

        _llm_finish(enhance)
        _seed_ui = _build_seed_ui(manager, enhance, seed_control, current_seed, llm_seed)

        # ── 分段：按 [SHOT_START] 块切分（每段一个独立新提示词 → 剧情持续推进）──
        shots = re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', prompt_input or "", re.DOTALL)
        base_seed = int(sample_decode.get("seed", 0) or 0)
        seed_mode = sample_decode.get("seed_mode", "randomize") or "randomize"

        saved_files = []
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
        seam_window_used = 0
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
            ref_images = _collect_slots(scene, "image", 9)
            ref_videos = _collect_slots(vid, "video", 3)
            ref_audios = _collect_slots(aud, "audio", 3)
            for mname in mention_names:
                kind, data = _get_asset_by_name(mname)
                if data is None:
                    continue
                if kind == "image" and len(ref_images) < 9:
                    ref_images.append(data)
                elif kind == "video" and len(ref_videos) < 3:
                    ref_videos.append(data)
                elif kind == "audio" and len(ref_audios) < 3:
                    ref_audios.append(data)
            while len(ref_images) + len(ref_videos) + len(ref_audios) > 12:
                if ref_videos:
                    ref_videos.pop()
                elif ref_audios:
                    ref_audios.pop()
                elif ref_images:
                    ref_images.pop()

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
            print(f"[JZL-无限时长] ── 分段 {i + 1}/{count} 生成开始（{mode}，{seg_len} 帧"
                  f"{f'，含段首衔接 {seam_window_cfg + seam_settle} 帧' if (i > 0 and seam_runway_sec > 0) else ''}"
                  f"，种子 {seed}）──")
            try:
                # ① 编码（ref2va，与官方 ReferenceToVideo 一致；**每段都重新注入原始参考**→ 抗长链漂移）
                positive, latent = _encode_ref_to_video(
                    clip, vae, audio_vae, h3, width, height, seg_len,
                    ref_images=ref_images, ref_videos=ref_videos,
                    ref_video_audios=ref_audios[:len(ref_videos)],
                    ref_audios=ref_audios[len(ref_videos):],
                    ref_scale=scale_factor)
                positive_base = positive   # 未注入引导的“干净” positive（二采可能要用另一份引导重建）

                # ② ♾️ 衔接：第 2 段起把上一段一采 latent 的尾部窗口钉成本段首帧引导
                seam_plan = None
                if i > 0 and prev_latent is not None:
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
                    positive = _seam_fit_guides_to_canvas(positive, latent["samples"])
                    seam_window_used = seam_plan["window"]
                    if _kf is not None and int(_kf["latent"].shape[2]) != int(seam_plan["trim_tokens"]):
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count}：⚠️ 引导 latent token 数 "
                              f"{int(_kf['latent'].shape[2])} ≠ 期望 {seam_plan['trim_tokens']}（衔接窗口可能不准）")
                    _a_txt = (f"＋音频尾部 {seam_plan['trim_audio']} 步"
                              if (_kf and "audio_latent" in _kf) else "（音频不衔接）")
                    if seam_plan["adjusted"]:
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count}：窗口按上段段长自动调整为 "
                              f"{seam_plan['window']} 帧（配置 {seam_window_cfg} 帧）")
                    print(f"[JZL-无限时长] 分段 {i + 1}/{count} 衔接：上段尾部 "
                          f"{seam_plan['window']} 帧（latent token {seam_plan['trim_tokens']} 个）"
                          f"@{_kf['resolved_frame_index'] if _kf else 0} {_a_txt}；"
                          f"本段解码后将裁掉开头 {seam_plan['window']} 帧")

                # ③ 第一次采样（衔接引导源 = 本段一采 latent，与下段目标 latent 同画布同空间）
                first_samples = _sample_av(model, positive, latent, sample_decode, seed)
                samples = first_samples

                # ④ latent 放大 + 第二次采样（可选）：二采结果只用于解码落盘
                #    二采引导源（面板「二采引导来源」）：
                #      first（默认）= 复用本段一采引导（上段一采 latent）→ 内部按二采画布插值对齐
                #      second        = 用上一段**二采** latent 尾部重建引导（与二采画布同画布、同 refined 版本）
                _second = (sample_decode or {}).get("second") or {}
                if _second.get("enabled"):
                    print(f"[JZL-无限时长] 分段 {i + 1}/{count} 二次采样：视频latent放大 {upscale_scale}x"
                          f"（{_second.get('sampler', 'euler')}/{_second.get('scheduler', 'simple')}，"
                          f"{_second.get('steps', 3)}步，denoise {_second.get('denoise', 0.3)}）…")
                    _pos2 = positive
                    if seam_second_guide == "second" and seam_plan is not None and prev_latent2 is not None:
                        _pos2, _kf2 = _seam_apply_guide(positive_base, prev_latent2, seam_plan,
                                                        audio_link=seam_audio_link, vae=None,
                                                        guide_source="latent")
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} 二采引导：上段二采 latent 尾部 "
                              f"{seam_plan['trim_tokens']} token（与二采画布同尺寸，无需插值）")
                    samples = _run_second_sampling(model, _pos2, first_samples, sample_decode, _second, upscale_scale, seed)
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
                              f"{seam_latent_drop['tokens']} token（= {seam_latent_drop['frames']} 帧 = 窗口 "
                              f"{seam_plan['window']} + 过渡 {seam_settle}）→ VAE 解码相位 0（不闪烁）")

                print(f"[JZL-无限时长] 分段 {i + 1}/{count} 采样完成，开始解码…")
                image, audio = _decode_av(vae, audio_vae, samples, sample_decode)
                _raw_frames = int(image.shape[0]) if image is not None else 0
                if seam_plan is not None and seam_latent_drop is not None:
                    # latent 域：视频已在 token 域裁好 → 音频在波形域按「同一丢弃帧数」裁（与像素域同一套音画规则）
                    if seam_trim_audio:
                        audio = _seam_trim_audio_frames(audio, seam_latent_drop["frames"], _raw_frames)
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} latent 域裁切完成：解码 {_raw_frames} 帧"
                              f"（已少解码 {seam_latent_drop['frames']} 帧）；音频按同一丢弃量裁剪，音画等长")
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
                                                          trim_audio=seam_trim_audio)
                        _kept = int(image.shape[0]) if image is not None else 0
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} 像素域裁切：解码 {_raw_frames} 帧 "
                              f"→ 丢开头 {seam_plan['window']} 帧衔接帧 + {seam_settle} 帧启动过渡 = {seam_plan['window'] + seam_settle} 帧 "
                              f"→ 保留 {_kept} 帧（{_kept / 24.0:.3f}s）；"
                              f"音频{'按保留帧数同步裁剪，音画等长' if seam_trim_audio else '未裁剪（音频同步裁剪已关闭·仅对比实验）'}")
                _frames = int(image.shape[0]) if image is not None else 0
                # ⭐ 帧账核对：落盘帧数应 ≈ 设定时长对应的帧数（生成 = 设定 + 跑道，裁掉跑道后回到设定）
                if i > 0 and seam_runway_sec > 0 and seam_plan is not None and seam_trim_mode != "none":
                    _delta = _frames - _saved_target
                    _dmsg = (f"生成 {seg_len} 帧（含段首衔接 {seam_window_cfg + seam_settle} 帧）"
                             f" → 裁掉 {seg_len - _frames} 帧 → 落盘 {_frames} 帧 = {_frames / 24.0:.2f} 秒")
                    if abs(_delta) <= 2:
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} {_dmsg}（= 设定时长 ✓）")
                    else:
                        print(f"[JZL-无限时长] 分段 {i + 1}/{count} {_dmsg}；⚠️ 比设定时长差 {_delta:+d} 帧"
                              f"（{_delta / 24.0:+.2f}s）：该段 **时长** 可能没按「设定 + 跑道」写，"
                              f"或「设定时长 + 跑道」超出单段上限 15 秒（已自动取窗口上限）")
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
                print(f"[JZL-无限时长] 分段 {i + 1}/{count} 已即时落盘：{_sp}（{_frames} 帧"
                      f"{'，已裁掉段首 ' + str(seam_plan['window']) + ' 帧衔接帧' if seam_plan is not None else ''}）")

                # ★ 衔接源：本段一采 latent 转 CPU 留一份给下一段（二采不影响它）
                prev_latent = first_samples.to("cpu") if hasattr(first_samples, "to") else first_samples

                # ★ 释放该段全部显存/内存（跑 N 段与跑 1 段占用不叠加）
                try:
                    del image, audio, samples, positive, latent, first_samples
                except Exception:
                    pass
                _clean_cache()
                print(f"[JZL-无限时长] 分段 {i + 1}/{count} 完成，已释放该段显存/内存"
                      f"（跑{count}段与跑1段占用不叠加；衔接源保留 1 份一采 latent）")
            except Exception as e:
                err = str(e)
                if "no kernel image" in err:
                    err += (f"\n  [JZL提示] CUDA/ROCm 内核与显卡架构不匹配（no kernel image）：当前 torch 没有该 GPU 的编译内核。"
                            f"诊断：{_gpu_diag_hint()}\n"
                            f"  请确认云机显卡型号与 torch/CUDA 版本匹配。")
                elif "CUDA error" in err or "cudaError" in err or "RuntimeError" in err:
                    import traceback as _tb
                    _tb.print_exc()
                    err += (f"\n  [JZL提示] CUDA 运行时错误（非内核不匹配），诊断：{_gpu_diag_hint()}\n"
                            f"  可能原因：驱动/算子在 Blackwell(sm120) 不兼容、dtype/精度、或某算子非法参数"
                            f"（如 attention 后端/参考张量）。完整堆栈已打印到上方日志，请按其定位。")
                errors.append(f"第{i + 1}段生成失败：{err}")
                print(f"[JZL-无限时长] 第{i + 1}段生成失败：{err}")

        # 采样种子回写：randomize 时把第 1 段实际种子回传前端
        if seed_mode == "randomize" and first_seed is not None:
            _samp_ui = {"seed_update_sample": {"seed": int(first_seed), "seed_control": seed_mode}}
            if _seed_ui:
                _seed_ui = dict(_seed_ui)
                _seed_ui.update(_samp_ui)
            else:
                _seed_ui = _samp_ui

        if (sample_decode or {}).get("decode_cleanup") == "卸载显存模型":
            _cleanup_vram("卸载显存模型")
            print("[JZL-无限时长] 显存清理完成（卸载显存模型）")

        # ── 保存模式：分段保存（每段已即时落盘）/ 拼接保存（读盘 concat 成一个）──
        out_files = list(saved_files)
        if save_mode == "拼接保存" and saved_files:
            try:
                _merge_out = os.path.join(
                    out_video_dir,
                    f"{_safe_story}_{_batch}次生成分段合并_{_next_counter(out_video_dir, f'{_safe_story}_{_batch}次生成分段合并'):05d}.mp4")
                _merged = _merge_mp4_concat(saved_files, _merge_out, audio_wavs=_merge_wavs)
                print(f"[JZL-无限时长] 拼接保存完成（读盘 {len(saved_files)} 段 concat 合并，"
                      f"音频统一编码）：{_merged}")
                if auto_merge_delete:
                    _del_cnt = 0
                    for _sp in saved_files:
                        try:
                            if os.path.exists(_sp):
                                os.remove(_sp); _del_cnt += 1
                        except Exception:
                            pass
                    print(f"[JZL-无限时长] 已按「拼接保存后删除分段视频」删除 {_del_cnt} 个分段，仅保留合并结果：{_merged}")
                out_files = [_merged]
            except Exception as _me:
                errors.append(f"拼接保存失败：{_me}（已保留各分段视频）")

        # 清理临时无損 WAV（已合入成片或不再需要）
        if _merge_wavs:
            _wrm = 0
            for _w in _merge_wavs:
                try:
                    if os.path.exists(_w):
                        os.remove(_w); _wrm += 1
                except Exception:
                    pass
            if _wrm:
                print(f"[JZL-无限时长] 已清理 {_wrm} 个分段临时音频（无损 WAV）")

        if errors:
            for e in errors:
                print(f"[JZL-无限时长] {e}")

        _dur = (total_frames / float(VIDEO_FPS)) if total_frames else 0.0
        print(f"[JZL-无限时长] 本次落盘目录：{out_video_dir}（视频 {len(saved_files)} 个，剧本 {len(prompt_input)} 字）")
        print(f"[JZL-无限时长] 本次成片合计 {total_frames} 帧 = {_dur:.2f} 秒 @24fps"
              f"（{count} 段；衔接窗口 {seam_window_used or 0} 帧 × {max(0, count - 1)} 次衔接已计入裁剪）")
        if seam_runway_sec > 0 and saved_files:
            _set_frames = _seg_len_for(None, duration_lo)
            print(f"[JZL-无限时长] ⏱️ 时长对照：设定 {_set_frames} 帧/段 = {_set_frames / float(VIDEO_FPS):.2f} 秒/段"
                  f" × {len(saved_files)} 段 = {_set_frames * len(saved_files) / float(VIDEO_FPS):.2f} 秒"
                  f"（每段生成时多出段首衔接跑道 {seam_window_cfg + seam_settle} 帧 = {seam_runway_sec:.2f} 秒，已在成片中裁掉）")
        if not saved_files:
            print("[JZL-无限时长] ⚠️ 本段未落盘任何视频（未连接模型/CLIP/VAE 或全部分段失败）")
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
                    multiline=False, advanced=True, socketless=True,
                    tooltip="只读显示：当前画幅/MP/时长/段数计算出的分辨率、每段帧数、段数、总帧数、总时长（对齐倍数固定 32）"),
                io.Combo.Input("aspect_ratio", options=ASPECT_RATIO_OPTIONS, default="16:9 (Widescreen)",
                    display_name="画幅比例", tooltip="画幅比例（分辨率按 MP×1024² 公式自动计算，对齐倍数固定 32）"),
                io.Float.Input("megapixels", display_name="百万像素（MP）", default=0.4, min=0.1, max=16.0, step=0.1,
                    tooltip="总像素数（MP），画幅×MP 决定分辨率"),
                io.String.Input("duration", display_name="每段视频时长(秒,可区间)", default="5-5",
                    tooltip="每段视频时长（秒）"),
                io.Float.Input("scale_factor", display_name="参考数值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="参考图放大系数"),
                io.Int.Input("video_count", display_name="生成视频数量", default=1, min=1, max=48,
                    tooltip="生成视频数量（分段数，支持 1~48 任意值；统一控制：提示词拆解段数）"),
                io.Float.Input("upscale_scale", display_name="二采latent放大", default=1.0, min=1.0, max=4.0, step=0.05,
                    tooltip="二采（Ref2va）放大倍数，从「Latent放大参数」输出给下游二采放大节点"),
                # ③提示词：剧本处理器参数（主界面显示）
                io.Combo.Input("story_style", options=story_styles, default=story_styles[0],
                    display_name="故事风格", tooltip="故事风格（剧本处理器按此风格拆解与润色）"),
                io.String.Input("story_name", display_name="故事名称", default="机智罗",
                    tooltip="故事名称（用于保存命名 / 日志）"),
                io.String.Input("external_prompt", display_name="提示词·接线（可连上游文本）", default="",
                    tooltip="提示词接线输入（与 CLIP Text Encode 同类）：此输入框左上角圆点可拖线连接上游 STRING 文本节点；连线后以上游文本为提示词（优先于「节点内提示词」大框；未接且留空则用大框内容）"),
                io.Model.Input("model", display_name="主模型", optional=True, advanced=True),
                io.Clip.Input("clip", display_name="CLIP", optional=True, advanced=True),
                io.Vae.Input("vae", display_name="视觉VAE", optional=True, advanced=True),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True, advanced=True),
                io.String.Input("internal_prompt", display_name="节点内提示词", multiline=True, advanced=True,
                    socketless=True,
                    tooltip="节点内编辑的提示词（提示词来源，随工作流保存）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True, advanced=True,
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
    def execute(cls, run_mode="故事拆解模式", video_count=6, aspect_ratio="16:9 (Widescreen)",
                megapixels=1.0, duration="5-5", scale_factor=1.0, display_info="",
                story_style="热血战斗", story_name="", upscale_scale=1.5,
                external_prompt=None,
                internal_prompt=None, manager_settings="",
                clip=None, vae=None, audio_vae=None, model=None) -> io.NodeOutput:
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
                                 ui={"manager_error": "必须填写「故事名称」后才能生成"})

        manager = _parse_node_manager_settings(manager_settings)
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
        print(f"[JZL-Mini] 运行模式={run_mode} | LLM种子={llm_seed}({seed_control}) | 共{max(1, min(48, int(video_count or 6)))}段")

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
                print(f"[JZL-Mini] 故事扩写模式：扩写到约 {_ew} 字正文 | 范围={'按素材' if _esrc != '自由' else '自由发挥'}（纯文本，不编码）")
                processed, err = _run_story_expander(
                    prompt_input, manager, story_style, story_name, prompt_lang, llm_seed,
                    expand_words=_ew, expand_source=_esrc,
                    ref_image_intro=ri, ref_video_intro=rv, ref_audio_intro=ra,
                    gen_dir=gen_dir)
            else:
                print(f"[JZL-Mini] 仅提示词输出：按「拆解模式(Decompose)」拆解 + 增强（纯文本，不编码）")
                _pcount = max(1, min(48, int(video_count)))
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
        count = max(1, min(48, int(video_count)))

        ref_image_intro, ref_video_intro, ref_audio_intro, slot_to_asset = _build_asset_intro(assets_cfg)
        JZL_SLOT_MAP.update(slot_to_asset)
        enable_scene, enable_props, enable_video, enable_audio = _detect_enables(prompt_input, assets_cfg)

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

        if passthrough and not has_shots:
            count = 1

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
            ref_images = _collect_slots(scene, "image", 9)
            ref_videos = _collect_slots(vid, "video", 3)
            ref_audios = _collect_slots(aud, "audio", 3)
            for mname in mention_names:
                kind, data = _get_asset_by_name(mname)
                if data is None:
                    continue
                if kind == "image" and len(ref_images) < 9:
                    ref_images.append(data)
                elif kind == "video" and len(ref_videos) < 3:
                    ref_videos.append(data)
                elif kind == "audio" and len(ref_audios) < 3:
                    ref_audios.append(data)
            while len(ref_images) + len(ref_videos) + len(ref_audios) > 12:
                if ref_videos:
                    ref_videos.pop()
                elif ref_audios:
                    ref_audios.pop()
                elif ref_images:
                    ref_images.pop()
            # 本段含视频/音频参考但无 audio_vae → 跳过该段编码（列表该位留空）
            if (ref_videos or ref_audios) and audio_vae is None:
                print(f"[JZL-Mini] ⚠️ 第{i + 1}段含视频/音频参考但未连接 audio_vae，跳过编码")
                positives.append([])
                latents.append(None)
                continue
            print(f"[JZL-Mini] 编码第 {i + 1}/{len(shots)} 段（{width}x{height}，{length}帧，参考 {len(ref_images)}图/{len(ref_videos)}视频/{len(ref_audios)}音频）")
            positive, latent = _encode_ref_to_video(
                clip, vae, audio_vae, h3, width, height, _seg_len_for(raw, duration_lo),
                ref_images=ref_images, ref_videos=ref_videos,
                ref_video_audios=ref_audios[:len(ref_videos)],
                ref_audios=ref_audios[len(ref_videos):],
                ref_scale=scale_factor)
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
        print(f"[JZL-Viewer] execute：故事={_sn or '(空)'} 视频={len(_files)} 个")
        return io.NodeOutput(_files if _files else [None])
