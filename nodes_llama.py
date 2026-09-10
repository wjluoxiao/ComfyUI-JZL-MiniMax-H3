"""JZL MiniMax — Llama 模型加载器 + 编剧链节点（V1 经典 API）

漫剧创作链的前半段：
  剧本编剧 → 分段词生成器 → （story_nodes.py 的分段处理中心/调度）

依赖 llama_backend.py 的 LLAMA_CPP_STORAGE，与 XB_ToolBox 完全解耦。
"""

import os
import re
import json
import urllib.request
from datetime import datetime

import folder_paths

from .llama_backend import LLAMA_CPP_STORAGE, chat_handlers
from .story_nodes import normalize_slots
from .support_llama.presets.minimax_t2va import MINIMAX_T2VA_EN, MINIMAX_T2VA_ZH
from .support_llama.presets.minimax_i2va import MINIMAX_I2VA_EN, MINIMAX_I2VA_ZH
from .support_llama.presets.minimax_fl2va import MINIMAX_FL2VA_EN, MINIMAX_FL2VA_ZH
from .support_llama.presets.minimax_l2va import MINIMAX_L2VA_EN, MINIMAX_L2VA_ZH
from .support_llama.presets.minimax_ref2va import MINIMAX_REF2VA_EN, MINIMAX_REF2VA_ZH


# ═══════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════

def _get_output_dir(story_name="", subfolder=""):
    try:
        base = folder_paths.get_output_directory()
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "output")
    safe_name = re.sub(r'[<>:"/\\|?*\s]', '_', (story_name or "untitled").strip())
    out_dir = os.path.join(base, "jzl", safe_name, subfolder)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _safe_path(output_dir, prefix, shot_num=None, ext="txt"):
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    fn = f"{prefix}_{shot_num:03d}_{ts}.{ext}" if shot_num is not None else f"{prefix}_{ts}.{ext}"
    return os.path.join(output_dir, fn)


def _find_latest_version(base_dir):
    """找到最新版本文件夹(第NNN次分段词), 返回路径和下一个编号"""
    if not os.path.isdir(base_dir):
        return os.path.join(base_dir, "第001次分段词"), 1
    pat = re.compile(r'第(\d{3})次分段词')
    versions = []
    for f in os.listdir(base_dir):
        m = pat.match(f)
        if m and os.path.isdir(os.path.join(base_dir, f)):
            versions.append((int(m.group(1)), f))
    if versions:
        versions.sort(reverse=True)
        return os.path.join(base_dir, versions[0][1]), versions[0][0] + 1
    return os.path.join(base_dir, "第001次分段词"), 1


def _next_generation_dir(story_name=""):
    """output/jzl/{故事名}/ 下新建「第NNNNN次生成」批次目录（5位编号递增），返回路径。

    每次工作流运行（拆解/增强）归入一个新批次；H3提示词 / 故事拆解 / 已增强剧本
    三个同级子目录都在批次内，方便按批次统一查看。"""
    try:
        base = folder_paths.get_output_directory()
    except Exception:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "..", "output")
    safe_name = re.sub(r'[<>:"/\\|?*\s]', '_', (story_name or "untitled").strip())
    root = os.path.join(base, "jzl", safe_name)
    os.makedirs(root, exist_ok=True)
    pat = re.compile(r'^第(\d{5})次生成$')
    nums = []
    try:
        for f in os.listdir(root):
            m = pat.match(f)
            if m and os.path.isdir(os.path.join(root, f)):
                nums.append(int(m.group(1)))
    except OSError:
        pass
    nxt = (max(nums) + 1) if nums else 1
    gen_dir = os.path.join(root, f"第{nxt:05d}次生成")
    os.makedirs(gen_dir, exist_ok=True)
    return gen_dir


def _api_settings_file():
    """API 设置持久化文件路径（存 ComfyUI user 目录，API Key 不明文进工作流）"""
    try:
        base = folder_paths.get_user_directory()
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "jzl_minimax_h3_api.json")


def _read_api_settings():
    """读取 API 设置（无配置返回空字典）"""
    try:
        with open(_api_settings_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_api_settings(data):
    """保存 API 设置到磁盘（成功返回规范化后的字典）"""
    try:
        with open(_api_settings_file(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════
#  节点 1: Llama 模型加载器 Pro
# ═══════════════════════════════════════════════════════════════

class JZL_LlamaModelLoaderPro:
    """Llama 模型加载器 Pro — 合并模型选择与推理参数，支持折叠高级选项"""

    @classmethod
    def INPUT_TYPES(s):
        all_llms = folder_paths.get_filename_list("LLM")
        model_list = [f for f in all_llms if "mmproj" not in f.lower()]
        mmproj_list = [f for f in all_llms if "mmproj" in f.lower()] or ["None"]

        return {
            "required": {
                "model": (model_list,),
                "mmproj": (mmproj_list,),
                "chat_handler": (chat_handlers, {"default": "None"}),
                "advanced_settings": ("BOOLEAN", {
                    "default": False,
                    "label_on": "高级参数 ▾",
                    "label_off": "高级参数 ▸",
                    "tooltip": "开启后显示上下文长度、显存上限、图像token 及全部推理参数"
                }),
                "n_ctx": ("INT", {
                    "default": 32768,
                    "min": 1024, "max": 262144, "step": 128,
                    "tooltip": "上下文长度上限\nQwen3.5-9B 原生 262144（256K）；短篇 32768，56 段调到 131072-262144"
                }),
                "vram_limit": ("INT", {
                    "default": -1,
                    "min": -1, "max": 1024, "step": 1,
                    "tooltip": "显存使用上限(GB), -1=不限制\n参考值, 实际可能略超"
                }),
                "image_min_tokens": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 32}),
                "image_max_tokens": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 32}),
                "max_tokens": ("INT", {"default": 8192, "min": 0, "max": 262144, "step": 1,
                    "tooltip": "生成 Token 上限（Qwen3.5-9B 上限 262144）\n6 段约 12K，56 段约 128K，请按段数调整"}),
                "top_k": ("INT", {"default": 40, "min": 0, "max": 1000, "step": 1,
                    "tooltip": "词汇库检索范围\n40 配合 0.60 温度，兼顾格式严谨与词汇多样"}),
                "top_p": ("FLOAT", {"default": 0.9, "min": 0.0, "max": 1.0, "step": 0.01}),
                "min_p": ("FLOAT", {"default": 0.05, "min": 0.0, "max": 1.0, "step": 0.01}),
                "typical_p": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "temperature": ("FLOAT", {"default": 0.6, "min": 0.0, "max": 2.0, "step": 0.01,
                    "tooltip": "温度\n0.60 确保 [SHOT_START] 格式严谨，减少幻觉"}),
                "repeat_penalty": ("FLOAT", {"default": 1.05, "min": 0.0, "max": 10.0, "step": 0.01,
                    "tooltip": "重复惩罚\n1.05 轻微防句式复读，同时保留台词（太高会让 LLM 省略已在故事里写过的台词）"}),
                "frequency_penalty": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "present_penalty": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 2.0, "step": 0.01}),
                "mirostat_mode": ("INT", {"default": 0, "min": 0, "max": 2, "step": 1}),
                "mirostat_eta": ("FLOAT", {"default": 0.1, "min": 0.0, "max": 1.0, "step": 0.01}),
                "mirostat_tau": ("FLOAT", {"default": 5.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "state_uid": ("INT", {
                    "default": -1, "min": -1, "max": 999999, "step": 1,
                    "tooltip": "使用特定 ID 保存对话状态 (-1 = 使用节点 unique_id)"
                }),
                "backend": (["llama-server", "llama-cpp-python"], {
                    "default": "llama-cpp-python",
                    "tooltip": "本地推理后端：llama-cpp-python（默认，进程内）或 llama-server（子进程，需先运行 install_runtime.bat）"
                }),
                "gpu_device": (["auto", "0", "1", "2", "3"], {
                    "default": "auto",
                    "tooltip": "GPU 设备（仅 llama-server 后端）：auto = 自动跟随 ComfyUI 当前显卡；也可手动指定索引"
                }),
            }
        }

    RETURN_TYPES = ("LLAMACPPMODEL", "LLAMACPPARAMS")
    RETURN_NAMES = ("llama_model", "parameters")
    FUNCTION = "loadmodel"
    CATEGORY = "JZL/MiniMax"

    def loadmodel(self, model, mmproj, chat_handler, advanced_settings,
                  n_ctx, vram_limit, image_min_tokens, image_max_tokens,
                  max_tokens, top_k, top_p, min_p, typical_p,
                  temperature, repeat_penalty, frequency_penalty, present_penalty,
                  mirostat_mode, mirostat_eta, mirostat_tau, state_uid, backend, gpu_device):
        custom_config = {
            "model": model,
            "mmproj": mmproj,
            "chat_handler": chat_handler,
            "n_ctx": n_ctx,
            "vram_limit": vram_limit,
            "image_min_tokens": image_min_tokens,
            "image_max_tokens": image_max_tokens,
            "backend": backend,
            "gpu_device": gpu_device
        }

        parameters = {
            "max_tokens": max_tokens,
            "top_k": top_k,
            "top_p": top_p,
            "min_p": min_p,
            "typical_p": typical_p,
            "temperature": temperature,
            "repeat_penalty": repeat_penalty,
            "frequency_penalty": frequency_penalty,
            "present_penalty": present_penalty,
            "mirostat_mode": mirostat_mode,
            "mirostat_eta": mirostat_eta,
            "mirostat_tau": mirostat_tau,
            "state_uid": state_uid,
        }

        # 惰性加载：本节点只产出配置，真正加载由「剧本与镜头处理器」在「本地模型」模式下按需执行，
        # 这样选择「在线API」时彻底不加载本地模型（不占显存、不打本地加载日志）。
        return (custom_config, parameters)


# ═══════════════════════════════════════════════════════════════
#  节点 1.5: MiniMax H3 偏好设置（镜头语言偏好）
# ═══════════════════════════════════════════════════════════════

class JZL_MiniMaxH3Preference:
    """MiniMax H3 偏好设置 — 实时生成镜头语言设定词，接剧本处理器的 preference 输入。

    基于官方 h3-prompt-writing skill 的运镜/切镜/转场词汇（base-en.txt 4.2/4.3 节）。
    「随机组合」不是瞎猜：给 LLM 一个候选范围，让它根据剧情与故事风格在范围内自由选择。
    """

    _SHOT_SIZES = ["根据剧情", "随机组合", "远景为主", "全景为主", "中景为主", "近景为主", "特写为主"]
    _SHOT_SIZE_HINTS = {
        "根据剧情": "景别根据剧情需要主动搭配：不同 [Shot N] 用最合适的景别，并按剧情递进（建立→推进→高潮→收束）安排远景/全景/中景/近景/特写的层次变化，禁止整段只用一两种",
        "随机组合": "一个视频内多种景别混合使用（远景/全景/中景/近景/特写随剧情递进变化）。不同 [Shot N] 用不同景别，禁止整段只用一种景别；根据时长与剧情灵活搭配——长视频（10秒以上）用 4-6 种景别递进（建立→推进→高潮→收束），短视频用 2-3 种即可，绝不要只用一两种",
        "远景为主": "以远景（Extreme Long / Long）为主，角色在画面中占比较小，突出环境、空间关系与整体氛围",
        "全景为主": "以全景为主，完整呈现角色全身与场景全貌，交代人物与空间的关系",
        "中景为主": "以中景为主，角色腰部以上入画，兼顾肢体动作与面部表情",
        "近景为主": "以近景为主，角色胸部以上入画，突出表情、情绪与细节",
        "特写为主": "以特写为主，聚焦面部、手部或关键道具细节，强调关键瞬间与冲击力",
    }

    _CAMERA_MOVES = ["根据剧情", "随机组合", "固定机位", "推拉", "摇移", "俯仰", "升降", "环绕", "跟拍", "手持晃动", "旋转", "一镜到底"]
    _CAMERA_HINTS = {
        "根据剧情": "运镜根据剧情需要主动搭配：不同 [Shot N] 用最合适的运镜，随剧情起伏变换运镜类型（建立用全景摇移、对峙用环绕、爆发用快速推拉、追踪用跟拍），禁止整段只用一两种",
        "随机组合": "一个视频内多种运镜混合使用（推/拉/摇/移/跟/环绕/手持等穿插）。不同 [Shot N] 用不同运镜，禁止整段只用一种运镜；根据时长与剧情灵活搭配——长视频用更多种运镜（4-6种）配合剧情起伏，短视频用 2-3 种即可，绝不要只用一两种",
        "固定机位": "以固定机位（Static Shot）为主，通过主体动作与构图变化叙事，不依赖镜头运动",
        "推拉": "以推拉镜头（Push In / Pull Out）为主，推镜强调、拉镜揭示环境",
        "摇移": "以摇移镜头（Pan / Truck）为主，横向展示空间与主体关系",
        "俯仰": "以俯仰镜头（Tilt）为主，纵向展示高度差与空间纵深",
        "升降": "以升降镜头（Pedestal Up / Down）为主，展示空间层次与规模",
        "环绕": "以环绕镜头（Arc Shot）为主，围绕主体运动，突出对峙或审视感",
        "跟拍": "以跟拍（Tracking Shot）为主，镜头跟随运动主体，强化速度与连贯性",
        "手持晃动": "以手持晃动（Shake Slightly / Shake Strongly）为主，增强临场感与紧张感",
        "旋转": "以旋转镜头（Roll）为主，制造动感、眩晕或心理失衡",
        "一镜到底": "全程一镜到底，只用运镜改变视角，禁止切镜（无 [Shot N] 时间戳）",
    }

    _CUT_RHYTHMS = ["根据剧情", "随机组合", "一镜到底", "2~5镜", "5~9镜", "9~13镜", "13~18镜"]
    _CUT_HINTS = {
        "根据剧情": "切镜次数根据剧情决定：打斗/追逐/爆发默认高频快切（每个动作 2-4 秒切一镜），抒情/静态才放慢；不得均分切镜，长短镜随剧情起伏交替",
        "随机组合": "一个视频内切镜节奏混合：打斗快切（2-4 秒一镜）与长镜穿插，蓄力极静、爆发极动，不得均分切镜。根据剧情起伏调节——紧张段落切快、舒缓段落切慢，长短镜交替，禁止全程同一节奏",
        "一镜到底": "一镜到底：本镜只写一个 [Shot 1]，禁止切镜，只靠运镜改变视角",
        "2~5镜": "把本镜拆成 2-5 个 [Shot N] 切镜（单镜内部时间戳切镜）",
        "5~9镜": "把本镜拆成 5-9 个 [Shot N] 切镜（单镜内部时间戳切镜）",
        "9~13镜": "把本镜拆成 9-13 个 [Shot N] 切镜（单镜内部时间戳切镜）",
        "13~18镜": "把本镜拆成 13-18 个 [Shot N] 切镜（单镜内部时间戳切镜）",
    }

    _TRANSITIONS = ["随机", "硬切", "叠化", "淡入淡出", "擦除"]
    _TRANSITION_HINTS = {
        "硬切": "全部使用硬切（cut），不额外加转场特效，切镜必须引入新信息（主体/空间/状态/视角/时间）",
        "叠化": "使用叠化（cross-dissolve）过渡，适合时间流逝或情绪衔接",
        "淡入淡出": "使用淡入淡出（fade）过渡，适合开场、收尾或段落切换",
        "擦除": "使用擦除（wipe）过渡，适合空间切换或节奏明快的段落",
    }

    _CREATIVE_REQS = [
        "无特别要求", "节奏紧凑", "舒缓留白", "情感细腻", "明快轻松",
        "多反转结局", "开放式结局", "强冲突",
    ]
    _CREATIVE_HINTS = {
        "无特别要求": "",
        "节奏紧凑": "节奏紧凑，画面信息密度高，每个镜头都推进剧情，不拖沓",
        "舒缓留白": "节奏舒缓，多留白与呼吸感，情感在静默与细节中沉淀",
        "情感细腻": "情感细腻，注重角色内心戏、微表情与情绪留白",
        "明快轻松": "基调明快轻松，节奏轻快活泼，气氛欢快不压抑",
        "多反转结局": "剧情多反转，层层递进，不断推翻观众预期",
        "开放式结局": "开放式结局，留白给观众想象空间",
        "强冲突": "角色矛盾尖锐直接，冲突强烈，张力十足",
    }

    _DETAIL_LENGTHS = [
        "标准 (350-500字)",
        "精简 (200-350字)",
        "详细 (500-800字)",
        "超详细 (800-1200字)",
    ]

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "景别偏好": (s._SHOT_SIZES, {"default": "随机组合"}),
                "运镜偏好": (s._CAMERA_MOVES, {"default": "随机组合"}),
                "切镜节奏": (s._CUT_RHYTHMS, {"default": "随机"}),
                "转场偏好": (s._TRANSITIONS, {"default": "随机"}),
                "音乐风格": (JZL_MiniMaxPreset._MUSIC, {"default": "禁止音乐 / No Music"}),
                "创作要求": (s._CREATIVE_REQS, {"default": "无特别要求"}),
                "详细描述字数": (s._DETAIL_LENGTHS, {"default": "标准 (350-500字)"}),
                "自定义镜头语言": ("STRING", {"default": "", "multiline": True,
                    "placeholder": "选填。自由描述镜头要求，如：多用低角度仰拍、结尾慢动作定格、关键道具给特写..."}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("偏好设定词",)
    FUNCTION = "build"
    CATEGORY = "JZL/MiniMax"

    def build(self, 景别偏好, 运镜偏好, 切镜节奏, 转场偏好, 音乐风格, 创作要求, 详细描述字数, 自定义镜头语言):
        lines = ["## 镜头语言偏好（本批分段必须严格遵守）"]

        lines.append(f"- 景别：{self._SHOT_SIZE_HINTS.get(景别偏好, 景别偏好)}")

        lines.append(f"- 运镜：{self._CAMERA_HINTS.get(运镜偏好, 运镜偏好)}")

        lines.append(f"- 切镜：{self._CUT_HINTS.get(切镜节奏, 切镜节奏)}")

        if 转场偏好 == "随机":
            lines.append("- 转场：根据剧情与故事风格，在「硬切 / 叠化 / 淡入淡出 / 擦除」范围内自由选择")
        else:
            lines.append(f"- 转场：{self._TRANSITION_HINTS.get(转场偏好, 转场偏好)}")

        if "不指定" in 音乐风格:
            pass
        elif "禁止音乐" in 音乐风格:
            lines.append("- 背景音乐：禁止任何背景音乐，non_diegetic_music 必须严格输出 \"N/A\"，不得写任何配乐/旋律/节奏")
        else:
            zh_name = 音乐风格.split(" / ")[-1]
            hint = JZL_MiniMaxPreset._MUSIC_HINTS.get(音乐风格, "")
            lines.append(f"- 背景音乐风格：{zh_name} — {hint}")

        if 创作要求 == "无特别要求":
            lines.append("- 创作要求：按故事风格自然发挥")
        else:
            lines.append(f"- 创作要求：{self._CREATIVE_HINTS.get(创作要求, 创作要求)}")

        lines.append(f"- 详细描述字数：{详细描述字数}")

        custom = (自定义镜头语言 or "").strip()
        if custom:
            lines.append(f"- 自定义：{custom}")
        return ("\n".join(lines),)


# ═══════════════════════════════════════════════════════════════
#  节点 1.6: API 设置（对接剧本处理器的 API 输入）
# ═══════════════════════════════════════════════════════════════

def _api_call_openai(base_url, api_key, model, messages, temperature, max_tokens, thinking=None):
    """OpenAI 兼容 API（OpenAI/DeepSeek/Qwen/GLM/Kimi/Ollama/vLLM/LM Studio）。"""
    url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    payload_dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if thinking in ("enabled", "disabled"):
        payload_dict["thinking"] = {"type": thinking}
    payload = json.dumps(payload_dict).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    with urllib.request.urlopen(req, timeout=1800) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "choices" not in data or not data["choices"]:
        return f"[API 错误] 响应无 choices：{json.dumps(data, ensure_ascii=False)[:500]}"
    return data["choices"][0]["message"]["content"]


def _api_call_anthropic(api_key, model, messages, temperature, max_tokens):
    url = "https://api.anthropic.com/v1/messages"
    system = ""
    chat = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            chat.append({"role": m["role"], "content": m["content"]})
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": chat,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    with urllib.request.urlopen(req, timeout=1800) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = [b.get("text", "") for b in data.get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
    if not parts:
        return f"[API 错误] 响应无文本：{json.dumps(data, ensure_ascii=False)[:500]}"
    return "".join(parts)


def _api_call_gemini(api_key, model, messages, temperature, max_tokens):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    system = ""
    user = ""
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            user += m["content"] + "\n"
    combined = (system + "\n\n" + user).strip() if system else user.strip()
    payload = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": combined}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return f"[API 错误] 响应格式异常：{json.dumps(data, ensure_ascii=False)[:500]}"


class JZL_MiniMaxAPISettings:
    """API 设置 — 点击「打开设置」在弹窗中配置（API Key 掩码不明文），执行时输出 JSON 配置接剧本处理器的 api_config。"""

    _PROVIDERS = [
        "OpenAI 兼容 (OpenAI/DeepSeek/Qwen/GLM/Kimi/Ollama/vLLM/LM Studio)",
        "Anthropic",
        "Google Gemini",
    ]

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "open_settings": ("BOOLEAN", {"default": False,
                    "label_on": "⚙️ 打开 API 设置…", "label_off": "⚙️ 打开 API 设置…",
                    "tooltip": "点击后在弹窗中配置 provider/模型/API Key/地址/温度/Token，保存即生效"}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("api_config",)
    FUNCTION = "build"
    CATEGORY = "JZL/MiniMax"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 弹窗中保存配置后，配置内容变化会触发本节点重新执行，下游拿到最新配置
        import json as _json
        return _json.dumps(_read_api_settings(), sort_keys=True, ensure_ascii=False)

    def build(self, open_settings):
        settings = _read_api_settings()
        config = {
            "provider": settings.get("provider", self._PROVIDERS[0]),
            "model": (settings.get("model") or "").strip(),
            "api_key": (settings.get("api_key") or "").strip(),
            "base_url": (settings.get("base_url") or "").strip(),
            "temperature": settings.get("temperature", 0.6),
            "max_tokens": settings.get("max_tokens", 81920),
            "thinking": settings.get("thinking"),
        }
        return (json.dumps(config, ensure_ascii=False),)


# ═══════════════════════════════════════════════════════════════
#  节点 2: 剧本与镜头处理器 (剧本编剧 + 分段词生成器 合一)
#  故事 → N 段（每段三合一：H3提示词 + 场景指令 + 音视频指令）
# ═══════════════════════════════════════════════════════════════

class JZL_MiniMax_ScriptProcessor:
    """剧本与镜头处理器 — 一次 LLM 调用：故事拆解 + 每段 H3 提示词 + 调度指令。"""

    _FIELD_PATTERNS = [
        ("characters", r'\*\*角色\*\*[：:]\s*(.+?)(?:\n|$)'),
        ("scene", r'\*\*场景\*\*[：:]\s*(.+?)(?:\n|$)'),
        ("props", r'\*\*道具\*\*[：:]\s*(.+?)(?:\n|$)'),
        ("camera", r'\*\*运镜\*\*[：:]\s*(.+?)(?:\n|$)'),
        ("action", r'\*\*动作描述\*\*[：:]\s*(.+?)(?:\n|$)'),
    ]

    @classmethod
    def INPUT_TYPES(cls):
        from .presets.script import STORY_STYLES, SEGMENT_COUNT_OPTIONS
        try:
            from .sheding.story_styles import STORY_STYLES as _ss
        except ImportError:
            _ss = STORY_STYLES
        return {
            "required": {
                "llm_backend": (["本地模型 [local]", "在线API [api]"], {"default": "本地模型 [local]"}),
                "mode": (["拆解模式 (Decompose)", "生成模式 (Generate)"], {"default": "拆解模式 (Decompose)"}),
                "story_style": (list(_ss.keys()), {"default": list(_ss.keys())[0] if _ss else "热血战斗"}),
                "use_custom_rule": ("BOOLEAN", {"default": False,
                    "label_on": "自定义规则", "label_off": "默认规则",
                    "tooltip": "关闭=使用默认分段规则；开启=启用下方自定义规则（粘贴文本 / 填文件路径 / 浏览选文件）"}),
                "story_name": ("STRING", {"default": "", "placeholder": "故事名称"}),
                "story_input": ("STRING", {"multiline": True, "default": ""}),
                "segment_count": (list(SEGMENT_COUNT_OPTIONS.keys()), {"default": list(SEGMENT_COUNT_OPTIONS.keys())[0] if SEGMENT_COUNT_OPTIONS else "4段"}),
                "segment_duration": ("INT", {"default": 8, "min": 4, "max": 15, "step": 1,
                    "tooltip": "每段视频时长(秒)，强制每段视频长度。与「海螺H3视频参数」的时长联动"}),
                "prompt_lang": (["中文 [ZH]", "英文 [EN]"], {"default": "中文 [ZH]"}),
                "ref_image_intro": ("STRING", {"multiline": True, "default": "",
                    "placeholder": "参考图片介绍，例：图1主角特写，图2背景街道..."}),
                "ref_video_intro": ("STRING", {"multiline": True, "default": "",
                    "placeholder": "参考视频介绍，例：视频1运镜参考，视频2动作参考..."}),
                "ref_audio_intro": ("STRING", {"multiline": True, "default": "",
                    "placeholder": "参考音频介绍，例：音频1男主音色，音频2女主音色..."}),
                "enable_scene": ("BOOLEAN", {"default": True, "label_on": "启用场景", "label_off": "禁用场景",
                    "tooltip": "启用后统计表和分段里才会输出场景分类调度指令"}),
                "enable_props": ("BOOLEAN", {"default": True, "label_on": "启用道具", "label_off": "禁用道具",
                    "tooltip": "启用后统计表和分段里才会输出道具分类调度指令"}),
                "enable_video": ("BOOLEAN", {"default": True, "label_on": "启用视频", "label_off": "禁用视频",
                    "tooltip": "启用后分段里才会输出参考视频调度指令"}),
                "enable_audio": ("BOOLEAN", {"default": True, "label_on": "启用音频", "label_off": "禁用音频",
                    "tooltip": "启用后分段里才会输出参考音频调度指令"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff, "step": 1, "control_after_generate": True,
                    "tooltip": "随机种子\n改 seed 可生成不同结果；前端可选随机/递增"}),
                "force_offload": ("BOOLEAN", {"default": False}),
                "save_states": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "llama_model": ("LLAMACPPMODEL",),
                "parameters": ("LLAMACPPARAMS",),
                "api_config": ("STRING", {"forceInput": True,
                    "tooltip": "在线API 模式：从「JZL - 🌐 LLM-API 设置」节点连线，自动读取弹窗中保存的配置"}),
                "preference": ("STRING", {"default": "", "forceInput": True,
                    "tooltip": "从「JZL - 🎯 MiniMax H3 偏好设置」节点连线"}),
                "custom_rule_path": ("STRING", {"default": "",
                    "placeholder": "选填。可直接粘贴规则文本，或填文件路径（.txt / .py），或点「📂 浏览」选文件；与官方格式重叠的描述会被自动清洗"}),
            },
        }

    RETURN_TYPES = ("STRING", "JZL_H3_BUS")
    RETURN_NAMES = ("剧本输出", "BUS")
    FUNCTION = "execute"
    CATEGORY = "JZL/MiniMax"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 所有关键生成参数都参与缓存签名：只返回 seed 会导致切换后端/风格/时长等命中缓存，
        # 剧本处理器不重跑，BUS 里仍是旧的后端配置（例如切成 API 后增强节点仍用本地模型）
        return (
            kwargs.get("seed", 0),
            kwargs.get("llm_backend"),
            kwargs.get("mode"),
            kwargs.get("story_style"),
            kwargs.get("segment_count"),
            kwargs.get("segment_duration"),
            kwargs.get("prompt_lang"),
            kwargs.get("use_custom_rule"),
            kwargs.get("enable_scene"),
            kwargs.get("enable_props"),
            kwargs.get("enable_video"),
            kwargs.get("enable_audio"),
            kwargs.get("story_name"),
            kwargs.get("story_input"),
            kwargs.get("preference"),
            kwargs.get("custom_rule_path"),
        )

    @classmethod
    def _parse_four_in_one(cls, content):
        """解析四段格式, 返回 (h3_prompt, scene_info, video_info, audio_info)"""
        h3, scene, video, audio = "", "{}", "{}", "{}"
        for section in re.split(r'\n(?====)', content):
            section = section.strip()
            if section.startswith("===H3_PROMPT==="):
                h3 = section[len("===H3_PROMPT===\n"):].strip()
            elif section.startswith("===SCENE_INSTRUCTION==="):
                scene = section[len("===SCENE_INSTRUCTION===\n"):].strip()
            elif section.startswith("===VIDEO_INSTRUCTION==="):
                video = section[len("===VIDEO_INSTRUCTION===\n"):].strip()
            elif section.startswith("===AUDIO_INSTRUCTION==="):
                audio = section[len("===AUDIO_INSTRUCTION===\n"):].strip()
        return h3, scene, video, audio

    @classmethod
    def _extract_scene_info(cls, shot, shot_num, enable_scene=True, enable_props=True):
        # 容错：LLM 未输出 SCENE_INSTRUCTION 段时，从结构化字段重建 slots（场景→角色→道具）
        chars, scene, props = "", "", ""
        for key, pat in cls._FIELD_PATTERNS:
            m = re.search(pat, shot)
            if not m:
                continue
            val = m.group(1).strip()
            if key == "characters":
                chars = val
            elif key == "scene":
                scene = val
            elif key == "props":
                props = val
        slots = []
        if enable_scene and scene:
            slots.append(f"场景:{scene}")
        if chars:
            for c in chars.replace("、", ",").split(","):
                c = c.strip()
                if c and c != "无":
                    slots.append(f"角色:{c}")
        if enable_props and props:
            for p in props.replace("、", ",").split(","):
                p = p.strip()
                if p and p != "无":
                    slots.append(f"道具:{p}")
        return json.dumps({"slots": slots}, ensure_ascii=False)

    @classmethod
    def _extract_video_info(cls, shot, shot_num):
        # 容错：视频素材名无法从结构化字段推断，slots 置空（调度节点全收兜底）
        return json.dumps({"slots": []}, ensure_ascii=False)

    @classmethod
    def _extract_audio_info(cls, shot, shot_num):
        return json.dumps({"slots": []}, ensure_ascii=False)

    @classmethod
    def _parse_material_intro(cls, ref_image_intro, ref_video_intro, ref_audio_intro):
        """从用户素材声明（图片/视频/音频描述）解析角色/场景/道具清单。

        只统计用户声明的元素（如「角色A = 孙悟空（描述）」→ 角色「孙悟空」），
        不含 LLM 在分段里幻想的元素。
        """
        chars, scenes, props = [], [], []
        for intro in (ref_image_intro, ref_video_intro, ref_audio_intro):
            if not intro:
                continue
            for line in str(intro).splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'^(角色|场景|道具)\s*[A-Za-z]?\s*[=＝:：]\s*(.+)', line)
                if not m:
                    continue
                typ = m.group(1)
                name = re.sub(r'[（(].*?[)）]', '', m.group(2)).strip()
                if not name:
                    continue
                if typ == "角色" and name not in chars:
                    chars.append(name)
                elif typ == "场景" and name not in scenes:
                    scenes.append(name)
                elif typ == "道具" and name not in props:
                    props.append(name)
        return chars, scenes, props

    @classmethod
    def _build_stat_table(cls, chars, scenes, props, segment_count):
        """统计表：只显示用户素材声明里的角色/场景/道具（不含 LLM 幻想元素）。"""
        lines = ["[Statistical table]"]
        lines.append(f"角色共{len(chars)}个：{'、'.join(chars) if chars else '无'}")
        lines.append(f"场景共{len(scenes)}个：{'、'.join(scenes) if scenes else '无'}")
        lines.append(f"道具共{len(props)}个：{'、'.join(props) if props else '无'}")
        lines.append(f"分段共{segment_count}个")
        return "\n".join(lines)

    @classmethod
    def _build_slot_map(cls, ref_image_intro, ref_video_intro, ref_audio_intro):
        """构建槽位映射：type_map["角色A"]=(类型,素材名)、name_map["孙悟空"]="角色A"，用于 slots 二次纠错。"""
        type_map, name_map = {}, {}
        for intro in (ref_image_intro, ref_video_intro, ref_audio_intro):
            if not intro:
                continue
            for line in str(intro).splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'^(角色|场景|道具|视频|音频|分镜|音效|音乐|其他)\s*([A-Za-z])\s*[=＝:：]\s*(.+)', line)
                if not m:
                    continue
                typ, slot, rest = m.group(1), m.group(2).upper(), m.group(3).strip()
                name = re.sub(r'[（(].*?[)）]', '', rest).strip()
                key = f"{typ}{slot}"
                type_map[key] = (typ, name)
                if name and name not in name_map:
                    name_map[name] = key
        return type_map, name_map

    @classmethod
    def _fix_slots_by_map(cls, info_json, name_map):
        """按「素材名→槽位名」映射二次纠错 slots（误写素材名/错类型槽位拉回正确槽位）。"""
        try:
            d = json.loads(info_json) if isinstance(info_json, str) else (info_json or {})
        except Exception:
            return info_json
        if not isinstance(d, dict) or not isinstance(d.get("slots"), list):
            return info_json
        fixed, changed = [], False
        for slot in d["slots"]:
            if not isinstance(slot, str) or ":" not in slot:
                fixed.append(slot)
                continue
            typ, name = slot.split(":", 1)
            typ, name = typ.strip(), name.strip().rstrip(":：")
            # name 是素材名 → 反查正确槽位 key（如「孙悟空」→「角色A」）
            if name in name_map:
                key = name_map[name]
                m = re.match(r'^(角色|场景|道具|视频|音频|分镜|音效|音乐|其他)([A-Za-z])$', key)
                if m:
                    fixed.append(f"{m.group(1)}:{key}")
                    changed = True
                    continue
            fixed.append(f"{typ}:{name}")
        if changed:
            d["slots"] = fixed
            return json.dumps(d, ensure_ascii=False)
        return info_json

    @staticmethod
    def _load_custom_rules(path):
        """读取自定义分段提示词：值是存在的文件路径则读文件，否则直接把值本身当规则内容。"""
        if not path or not path.strip():
            return ""
        p = path.strip()
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                return f"[读取自定义规则失败] {e}"
        # 不是文件路径 → 直接把输入文本当规则内容（支持粘贴规则或从文本节点连线）
        return p

    @staticmethod
    def _clean_custom_rules(text):
        """清洗自定义分段提示词：剥离与官方六段格式/标签/调度重叠的描述，只保留润色要求。"""
        if not text:
            return ""
        strip_markers = [
            "subject_definitions", "summary", "retention_analysis",
            "detailed_description", "overall_soundscape", "non_diegetic_music",
            "integrated_multimodal_description",
            "[shot_start]", "[shot_end]", "[shot",
            "===h3_prompt===", "===scene_instruction===", "===video_instruction===", "===audio_instruction===",
            "<subject", "<picture", "<video", "<audio",
            "scene_instruction", "video_instruction", "audio_instruction",
            "slots", "(s1)", "(s2)", "<d>", "<scenetrans>", "<cutoff>",
        ]
        kept = []
        for line in (text or "").splitlines():
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if any(mk in low for mk in strip_markers):
                continue
            kept.append(s)
        return "\n".join(kept)

    @staticmethod
    def _validate_slots(scene_info, video_info, audio_info, shot_num=None):
        """校验调度指令 slots 槽位名是否符合契约，返回告警列表。

        合法格式（与调度节点 _match_name 的宽松匹配一致）：
        - 「类型:槽位名」；类型 = 场景/角色/道具/视频/音频（或 scene/character/prop/video/audio）
        - 槽位名 = A~Z 字母（如「音频:D」）或 类型前缀+A~Z（如「音频:音频D」）
        - 尾冒号/尾空格自动容错
        """
        warnings = []
        zh_types = ("场景", "角色", "道具", "视频", "音频")
        en_types = ("scene", "character", "prop", "video", "audio")
        name_pat = re.compile(r'^(?:场景|角色|道具|视频|音频|scene|character|prop|video|audio)?[A-Z]$', re.IGNORECASE)
        for label, info in (("场景", scene_info), ("视频", video_info), ("音频", audio_info)):
            try:
                d = json.loads(info) if isinstance(info, str) else (info or {})
            except Exception:
                continue
            for slot in (d.get("slots") or []):
                if not isinstance(slot, str) or ":" not in slot:
                    continue
                typ, name = slot.split(":", 1)
                typ = typ.strip()
                name = name.strip().rstrip(":：")
                if (typ not in zh_types and typ not in en_types) or not name_pat.match(name):
                    warnings.append(f"[⚠️ 槽位名异常] 第{shot_num if shot_num else '?'}段 {label}调度 slots 含「{slot}」——槽位名必须是 A~Z 字母（如「音频:D」）或 类型+A~Z（如「音频:音频D」），不是素材名/描述")
        return warnings

    @staticmethod
    def _has_dialogue(h3_text):
        """六段提示词里是否有实际对白（<d> 标签）。"""
        return bool(re.search(r'<d>', h3_text or ""))

    @staticmethod
    def _filter_scene_slots(scene_json, enable_scene, enable_props):
        """按开关过滤 SCENE_INSTRUCTION 的 slots：删场景/道具元素，返回 JSON 字符串。"""
        try:
            d = json.loads(scene_json) if isinstance(scene_json, str) else (dict(scene_json) if isinstance(scene_json, dict) else {})
        except Exception:
            return scene_json
        filtered = []
        for s in (d.get("slots") or []):
            if isinstance(s, str) and ":" in s:
                typ = s.split(":", 1)[0].strip()
                if typ == "场景" and not enable_scene:
                    continue
                if typ == "道具" and not enable_props:
                    continue
            filtered.append(s)
        d["slots"] = filtered
        return json.dumps(d, ensure_ascii=False)

    @staticmethod
    def _filter_scene_instruction_text(text, enable_scene, enable_props):
        """对块文本里的 ===SCENE_INSTRUCTION=== 段按开关过滤 slots。"""
        if enable_scene and enable_props:
            return text
        m = re.search(r'(===SCENE_INSTRUCTION===\s*)(\{[^{}]*\})', text, flags=re.DOTALL)
        if not m:
            return text
        filtered = JZL_MiniMax_ScriptProcessor._filter_scene_slots(m.group(2), enable_scene, enable_props)
        return text[:m.start()] + m.group(1) + filtered + text[m.end():]

    @staticmethod
    def _clean_shot_text(shot_text, has_dialogue, enable_scene, enable_props, enable_video, enable_audio):
        """按开关/无对话情况清理分段块文本：删除禁用段、无对话分段去掉说话人ID与音频。"""
        t = shot_text
        if not has_dialogue:
            t = re.sub(r'\s*\((S\d+)\)', '', t)
            t = re.sub(r'<Audio \d+>[^\n]*\n?', '', t)
            t = re.sub(r'(===AUDIO_INSTRUCTION===\s*\{[^}]*?"slots"\s*:\s*)\[[^\]]*\]', r'\1[]', t, flags=re.DOTALL)
        t = JZL_MiniMax_ScriptProcessor._filter_scene_instruction_text(t, enable_scene, enable_props)
        if not enable_video:
            t = re.sub(r'===VIDEO_INSTRUCTION===.*?(?====|\[SHOT_END\])', '', t, flags=re.DOTALL)
        if not enable_audio:
            t = re.sub(r'===AUDIO_INSTRUCTION===.*?(?====|\[SHOT_END\])', '', t, flags=re.DOTALL)
        return t.strip()

    @staticmethod
    def _call_api(config, system_prompt, user_msg):
        """根据 API 配置调用大模型 API，返回生成文本。"""
        try:
            cfg = json.loads(config) if isinstance(config, str) else (config or {})
        except Exception:
            return "[API 配置错误] 无法解析 API 配置 JSON"
        provider = cfg.get("provider", "")
        model = (cfg.get("model") or "").strip()
        api_key = (cfg.get("api_key") or "").strip()
        base_url = (cfg.get("base_url") or "").strip()
        temperature = cfg.get("temperature", 0.6)
        max_tokens = cfg.get("max_tokens", 81920)
        thinking = cfg.get("thinking")  # "enabled" / "disabled" / None
        print(f"[JZL-API] 调用 {provider}，模型：{model}")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]
        try:
            if "Anthropic" in provider:
                return _api_call_anthropic(api_key, model, messages, temperature, max_tokens)
            if "Gemini" in provider:
                return _api_call_gemini(api_key, model, messages, temperature, max_tokens)
            return _api_call_openai(base_url, api_key, model, messages, temperature, max_tokens, thinking)
        except Exception as e:
            return f"[API 错误] {e}"

    def execute(self, mode, story_name, story_input, story_style, use_custom_rule,
                segment_count, segment_duration, prompt_lang, ref_image_intro, ref_video_intro, ref_audio_intro,
                enable_scene, enable_props, enable_video, enable_audio,
                seed, force_offload, save_states,
                llm_backend="local", llama_model=None, parameters=None, api_config=None, preference=None, custom_rule_path=None, gen_dir=None):
        from .presets.script import build_shot_prompt, SEGMENT_COUNT_OPTIONS, _resolve_segment_count, _parse_duration_spec

        if not story_input or not story_input.strip():
            return ("[错误] 请输入故事内容", {})

        lang = "zh" if "ZH" in prompt_lang else "en"
        custom_rules = ""
        if use_custom_rule:
            custom_rules = self._clean_custom_rules(self._load_custom_rules(custom_rule_path))
        system_prompt = build_shot_prompt(
            user_story=story_input.strip(), mode=mode, story_style=story_style,
            segment_count_label=segment_count, lang=lang, segment_duration=segment_duration,
            ref_image_intro=ref_image_intro, ref_video_intro=ref_video_intro, ref_audio_intro=ref_audio_intro,
            enable_scene=enable_scene, enable_props=enable_props,
            enable_video=enable_video, enable_audio=enable_audio,
            preference=(preference or "").strip(),
            custom_rules=custom_rules,
        )
        segment_count = _resolve_segment_count(segment_count)
        _dlo, _dhi, _ddesc = _parse_duration_spec(segment_duration)
        _dur_clause = f"每段视频固定 {_dlo} 秒" if _dlo == _dhi else f"每段视频时长在 {_dlo}~{_dhi} 秒区间内，由你按剧情弧线给每段分配实际秒数（写进该段 **时长** 字段）"
        user_msg = f"请生成恰好 {segment_count} 个分段，{_dur_clause}，输出 [SHOT_START]...[SHOT_END] 完整块（分段信息 + 六段提示词 + 调度指令）。"

        if "api" in str(llm_backend) and api_config:
            print("[JZL-API] 使用在线 API 生成，跳过本地模型加载")
            result = self._call_api(api_config, system_prompt, user_msg)
        elif "local" in str(llm_backend) and llama_model is not None:
            if not LLAMA_CPP_STORAGE.llm or LLAMA_CPP_STORAGE.current_config != llama_model:
                print("[JZL-llama] 开始加载模型...")
                LLAMA_CPP_STORAGE.load_model(llama_model)
            try:
                _params = parameters.copy() if parameters else {}
                _params.pop("present_penalty", None)
                _params.pop("state_uid", None)
                output = LLAMA_CPP_STORAGE.llm.create_chat_completion(
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_msg}],
                    seed=seed, **_params)
                result = output["choices"][0]["message"]["content"]
            except Exception as e:
                result = f"[LLM 错误] {e}"
            finally:
                if force_offload:
                    LLAMA_CPP_STORAGE.clean()
                elif not save_states:
                    LLAMA_CPP_STORAGE.clean_state()
        else:
            return ("[错误] 请连接 llama_model，或切换到在线API 并从「JZL - 🌐 LLM-API 设置」节点连线 api_config", {})

        # 解析 N 段 → 四段（H3提示词 / 场景 / 视频 / 音频），保存 TXT
        shots = re.findall(r'\[SHOT_START\](.*?)\[SHOT_END\]', result or "", re.DOTALL)
        shots = [s.strip() for s in shots]
        h3_list, scene_list, video_list, audio_list = [], [], [], []
        cleaned_shots = []
        slot_warnings = []
        _, _slot_name_map = self._build_slot_map(ref_image_intro, ref_video_intro, ref_audio_intro)
        if shots:
            if gen_dir is None:
                gen_dir = _next_generation_dir(story_name)
            h3_dir = os.path.join(gen_dir, "H3提示词")
            os.makedirs(h3_dir, exist_ok=True)
            for i, shot in enumerate(shots):
                shot_num = i + 1
                h3_text, scene_info, video_info, audio_info = self._parse_four_in_one(shot)
                if not h3_text:
                    h3_text = f"[解析失败] 第{shot_num}段缺少 ===H3_PROMPT==="
                if scene_info == "{}":
                    scene_info = self._extract_scene_info(shot, shot_num, enable_scene, enable_props)
                if video_info == "{}":
                    video_info = self._extract_video_info(shot, shot_num)
                if audio_info == "{}":
                    audio_info = self._extract_audio_info(shot, shot_num)
                # 场景/道具开关：过滤 slots（无论 LLM 是否输出）
                scene_info = self._filter_scene_slots(scene_info, enable_scene, enable_props)
                # 清洗槽位名小错误（缺类型前缀「道具:C」→「道具:道具C」、尾冒号）
                scene_info = normalize_slots(scene_info)
                video_info = normalize_slots(video_info)
                audio_info = normalize_slots(audio_info)
                # 二次纠错：素材名反查槽位名（对照表），修正「角色:孙悟空」→「角色:角色A」等
                scene_info = self._fix_slots_by_map(scene_info, _slot_name_map)
                video_info = self._fix_slots_by_map(video_info, _slot_name_map)
                audio_info = self._fix_slots_by_map(audio_info, _slot_name_map)
                slot_warnings.extend(self._validate_slots(scene_info, video_info, audio_info, shot_num))
                # 无对话分段：清空音频调度、删除 <Audio N> 定义与误用的 (Sx)
                has_dialogue = self._has_dialogue(h3_text)
                if not has_dialogue:
                    audio_info = json.dumps({"slots": []}, ensure_ascii=False)
                    h3_text = re.sub(r'<Audio \d+>[^\n]*\n?', '', h3_text)
                    h3_text = re.sub(r'\s*\((S\d+)\)', '', h3_text)
                h3_list.append(h3_text)
                scene_list.append(scene_info)
                video_list.append(video_info)
                audio_list.append(audio_info)
                cleaned_shots.append(self._clean_shot_text(shot, has_dialogue, enable_scene, enable_props, enable_video, enable_audio))

                ts = datetime.now().strftime("%Y%m%d%H%M%S")
                txt_path = os.path.join(h3_dir, f"{shot_num:03d}分段_{ts}.txt")
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"===H3_PROMPT===\n{h3_text}\n")
                    f.write(f"===SCENE_INSTRUCTION===\n{scene_info}\n")
                    if enable_video:
                        f.write(f"===VIDEO_INSTRUCTION===\n{video_info}\n")
                    if enable_audio:
                        f.write(f"===AUDIO_INSTRUCTION===\n{audio_info}\n")

        # 统计表：只统计用户素材声明里的角色/场景/道具（不含 LLM 幻想元素），分段数用「要求的数量」
        stat_chars, stat_scenes, stat_props = self._parse_material_intro(ref_image_intro, ref_video_intro, ref_audio_intro)
        stat_table = self._build_stat_table(stat_chars, stat_scenes, stat_props, segment_count)

        # 槽位名校验告警（发现「天空」这类把描述当槽位名的情况，提示用户）
        if slot_warnings:
            stat_table = stat_table + "\n" + "\n".join(slot_warnings)

        # 块数校验：LLM 实际输出分段数 ≠ 要求数时告警，防止静默丢段
        actual_count = len(shots)
        if actual_count != segment_count:
            stat_table = (stat_table +
                          f"\n[⚠️ 分段数量不符] 要求 {segment_count} 段，实际解析到 {actual_count} 段。"
                          "请检查 LLM 输出是否被截断，或重新生成。")

        # 提取生成模式的故事正文（「【故事】」到第一个 [SHOT_START] 之间，仅供用户查看）
        story_body = ""
        if "生成" in mode:
            m = re.search(r'【故事】\s*(.*?)(?=\[SHOT_START\]|$)', result or "", re.DOTALL)
            if m:
                story_body = m.group(1).strip()

        # 剧本输出：故事内容块（生成模式）+ 统计表 + 清理后的分段原文
        if shots:
            cleaned_result = "\n\n".join(f"[SHOT_START]\n{s}\n[SHOT_END]" for s in cleaned_shots)
            if story_body:
                script_output = (f"[生成模式扩展后的故事内容]\n****\n{story_body}\n*****\n\n"
                                 + stat_table + "\n\n" + cleaned_result)
            else:
                script_output = stat_table + "\n\n" + cleaned_result
        else:
            script_output = result

        # 保存剧本（含统计表）→ output/jzl/{故事名}/第NNNNN次生成/故事拆解/
        if gen_dir is None:
            gen_dir = _next_generation_dir(story_name)
        story_dir = os.path.join(gen_dir, "故事拆解")
        os.makedirs(story_dir, exist_ok=True)
        prefix = "生成故事拆解" if "生成" in mode else "原始故事拆解"
        with open(_safe_path(story_dir, prefix), "w", encoding="utf-8") as f:
            f.write(script_output)

        # BUS 输出：把模型/API/偏好/风格等参数打包，供「提示词增强」节点独立运行 LLM
        # use_api = 剧本处理器实际使用的后端（增强节点直接沿用，避免字符串判断偏差）
        bus = {
            "llm_backend": llm_backend,
            "use_api": bool("api" in str(llm_backend) and api_config),
            "llama_model": llama_model,
            "parameters": parameters,
            "api_config": api_config,
            "preference": preference,
            "story_style": story_style,
            "mode": mode,
            "segment_count": segment_count,
            "segment_duration": int(_dlo),
            "segment_duration_desc": _ddesc,
            "prompt_lang": prompt_lang,
            "lang": lang,
            "custom_rules": custom_rules,
            "enable_scene": enable_scene,
            "enable_props": enable_props,
            "enable_video": enable_video,
            "enable_audio": enable_audio,
            "seed": seed,
            "force_offload": force_offload,
            "save_states": save_states,
            "story_name": story_name,
        }
        return (script_output, bus)


class JZL_MiniMaxPreset:
    """MiniMax H3 提示词预设 — T2VA/I2VA/FL2VA/L2VA + 动态参数注入"""

    _STYLES = [
        "不指定 / Unspecified",
        # 🎬 电影感
        "电影感 / Cinematic", "实拍 / Live-action",
        "复古胶片 / Vintage film", "黑白电影 / Black & White",
        "纪录片 / Documentary",
        # 📷 商业摄影
        "极简广告 / Minimalist commercial",
        "微距摄影 / Macro photography",
        "航拍 / Aerial drone",
        # 🎨 动画
        "二维动画 / 2D-animated", "三维CG / 3D CG",
        "日系二次元 / Anime", "美式漫画 / American Comic",
        "皮克斯3D / Pixar-style 3D", "定格动画 / Stop-motion",
        "手绘发光 / Hand-drawn glow", "像素艺术 / Pixel art",
        # 🚀 科幻前卫
        "赛博朋克 / Cyberpunk", "蒸汽朋克 / Steampunk",
        "故障艺术 / Glitch art",
        # 🧶 特殊材质
        "羊毛毡 / Wool felt", "折纸 / Origami",
        # 🖌️ 美术
        "水彩 / Watercolor", "粘土动画 / Claymation",
        "水墨 / Ink wash", "油画 / Oil painting",
        "纸艺拼贴 / Paper collage", "剪纸 / Paper cutout",
        "铅笔素描 / Pencil sketch", "浮世绘 / Ukiyo-e",
        # 🏮 中国风
        "敦煌壁画 / Dunhuang Murals",
        "青花瓷 / Blue-white Porcelain",
        "工笔画 / Gongbi Painting",
        "皮影戏 / Shadow Puppetry",
        "中国风插画 / Chinese Illustration",
        "年画 / New Year Painting",
        # 🧵 手工布艺
        "布艺 / Fabric Art",
        "蜡笔画 / Crayon drawing",
        "哥特萝莉 / Gothic Lolita",
    ]

    _STYLE_HINTS = {
        "电影感 / Cinematic": "cinematic lighting with shallow depth of field, film grain, and professional color grading",
        "实拍 / Live-action": "photorealistic live-action footage with natural lighting and authentic set design",
        "复古胶片 / Vintage film": "vintage film stock with warm color grading, subtle grain, and nostalgic atmosphere",
        "黑白电影 / Black & White": "high-contrast black-and-white cinematography with dramatic shadows",
        "纪录片 / Documentary": "observational documentary style with natural handheld camera work and candid framing",
        "极简广告 / Minimalist commercial": "clean minimalist product cinematography with smooth dolly moves, soft even lighting, and uncluttered compositions",
        "微距摄影 / Macro photography": "extreme close-up macro lens with razor-thin depth of field, revealing fine textures and details",
        "航拍 / Aerial drone": "sweeping aerial drone shots with wide vistas, slow majestic reveals, and expansive landscape views",
        "二维动画 / 2D-animated": "traditional 2D hand-drawn animation with expressive line art and fluid character motion",
        "三维CG / 3D CG": "high-quality 3D rendering with realistic materials, global illumination, and smooth animation",
        "日系二次元 / Anime": "Japanese anime cel-shading with vibrant saturated colors, clean linework, and expressive character designs",
        "美式漫画 / American Comic": "American comic book style with bold black ink outlines, halftone dot shading, and dynamic compositions",
        "皮克斯3D / Pixar-style 3D": "Pixar-quality 3D with smooth curved surfaces, rich vibrant colors, expressive character animation, and polished lighting",
        "定格动画 / Stop-motion": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a stop-motion animation. Characters are handcrafted puppets with visible material textures, moving with tactile frame-by-frame stutter. Environments are miniature physical sets with real fabrics, painted backdrops, and practical lighting. EVERYTHING is a physical model under a camera.",
        "手绘发光 / Hand-drawn glow": "GLOBAL MATERIAL OVERRIDE — the entire visual world is rough hand-drawn line art on dark paper. Characters and environments are sketched with glowing neon-colored outlines that flicker and pulse organically. Light trails follow movement like afterimages. The world itself is a living drawing, every line redrawn in real-time.",
        "像素艺术 / Pixel art": "GLOBAL MATERIAL OVERRIDE — the entire visual world is built from visible pixel blocks. Characters, environments, water, fire, smoke, sky — EVERYTHING is composed of crisp square pixels with a limited retro color palette. Motion is frame-by-frame at low FPS with deliberate pixel-level changes. Particles are individual pixel dots. The pixel grid IS the universe.",
        "赛博朋克 / Cyberpunk": "high-contrast neon-lit cyberpunk cityscape with rain-slicked streets, holographic displays, and chrome cybernetics",
        "蒸汽朋克 / Steampunk": "intricate brass machinery and Victorian-era steam technology with copper pipes, gears, and sepia tones",
        "故障艺术 / Glitch art": "digital glitch distortion with RGB color channel split, scan lines, data corruption artifacts, and VHS noise",
        "羊毛毡 / Wool felt": "GLOBAL MATERIAL OVERRIDE — the entire visual world is handcrafted from fuzzy wool felt. Characters have soft felt textile bodies with visible fiber textures and stitched seams. Wind ripples through felt grass, felt water flows with fiber movement, felt clouds drift across a felt sky. Environments are sewn felt dioramas. DO NOT place felt toys in a real scene — everything IS felt.",
        "折纸 / Origami": "GLOBAL MATERIAL OVERRIDE — the entire universe is constructed from folded paper. Characters are origami figures with sharp clean creases and geometric folded anatomy. Paper birds flap creased wings, paper water ripples in folded layers, paper fire crackles as curling sheets. The world itself is paper — all matter is folded, creased, and crisp.",
        "水彩 / Watercolor": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D watercolor painting on paper. Characters are NOT real people — their bodies are translucent color washes, their faces are soft pigment blooms on wet paper, their edges dissolve into the paper grain. Hair is bleeding pigment streaks, skin is the white of paper with tinted wash. Rain falls as pigment droplets, light diffuses through layered washes. NO realistic skin, NO 3D — only wet pigment on paper.",
        "粘土动画 / Claymation": "GLOBAL MATERIAL OVERRIDE — the entire visual world is hand-sculpted clay. Characters are NOT real people — their bodies are clay with rounded tactile surfaces, visible fingerprints, and tool marks. Hair is sculpted clay strands, skin is smooth plasticine, clothing is pressed clay sheets. Clay water splashes in sculpted droplets, clay smoke rolls in malleable puffs. NO real skin — only clay shaped by human hands.",
        "水墨 / Ink wash": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D ink wash painting on xuan rice paper. Characters are NOT real people — their bodies are fluid black brushstrokes, their faces are ink lines on paper, their clothing is graded ink washes. Hair flows as sweeping brushstrokes, skin tone is the white of the paper itself with ink shading. Water splashes as flying ink drops, wind leaves brushstroke trails, mist is spreading ink on wet paper. NO realistic skin, NO realistic fabric, NO 3D — only ink and paper.",
        "油画 / Oil painting": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D oil painting on canvas. Characters are NOT real people — their bodies are thick oil paint applied with palette knives, their faces are built from layered brushstrokes, their clothing is impasto pigment. Hair is swept paint, skin is blended oil color on canvas, eyes are precise brush dabs. Water ripples in heavy oil strokes, fire is palette-knife texture, clouds are smeared white paint. NO realistic skin, NO real fabric, NO 3D — only oil paint on canvas.",
        "纸艺拼贴 / Paper collage": "GLOBAL MATERIAL OVERRIDE — the entire visual world is layered torn paper. Characters are NOT real people — their bodies are cut from textured paper with torn edges, their faces are printed paper fragments, their clothing is different paper types (newsprint, craft, tissue). Paper birds flap torn-edge wings, paper water ripples in layered sheets. NO real skin — only paper.",
        "剪纸 / Paper cutout": "GLOBAL MATERIAL OVERRIDE — the entire visual world is Chinese paper cutout art. Characters are NOT real people — their bodies are intricate red paper silhouettes cut with symmetrical patterns, moving with articulated paper joints. Shadows cast dramatic shapes through the paper lattice. NO real skin — only cut paper.",
        "铅笔素描 / Pencil sketch": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D graphite pencil drawing on textured paper. Characters are NOT real people — their bodies are graphite lines, hatching, and cross-hatching on paper. Faces are sketched pencil marks, hair is sweeping graphite strokes, skin tone is the white of paper with varying pencil pressure. Motion is lines erasing and redrawing. Eraser marks leave ghost trails. NO real skin, NO 3D — only pencil on paper.",
        "浮世绘 / Ukiyo-e": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D Japanese ukiyo-e woodblock print. Characters are NOT real people — their bodies are flat color areas with bold black outlines printed on washi paper. Faces are printed woodblock features, hair is carved-line black ink, clothing is flat color blocks. NO real skin, NO 3D — only woodblock ink on paper.",
        "敦煌壁画 / Dunhuang Murals": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D animated Dunhuang cave mural on a fresco wall. Characters are NOT real people — their bodies are mineral pigment paintings (ochre, turquoise, lapis lazuli) with weathered fresco cracks. Flying deities trail faded pigment ribbons. NO real skin, NO 3D — only ancient mural pigment on plaster.",
        "青花瓷 / Blue-white Porcelain": "GLOBAL MATERIAL OVERRIDE — the entire universe is living 3D blue-and-white porcelain. Characters are NOT real people — their bodies are white-glazed porcelain with cobalt-blue hand-painted patterns flowing across their skin as features and clothing. Porcelain birds take flight with clicking ceramic wings, porcelain water flows as liquid glaze, porcelain trees bloom with cobalt flowers. NO real skin — only glazed ceramic.",
        "工笔画 / Gongbi Painting": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D gongbi painting on flat silk. Characters are NOT real people — their bodies are ultra-fine brush outlines filled with flat mineral color washes on silk. Every hair and petal is individually painted. Silk fibers visible beneath the pigment. NO real skin, NO 3D — only brush and silk.",
        "皮影戏 / Shadow Puppetry": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D shadow puppet theater on a translucent screen. Characters are NOT real people — their bodies are intricately carved leather silhouettes with articulated joints, illuminated by warm amber backlighting. NO real skin, NO 3D — only leather shadows on a screen.",
        "中国风插画 / Chinese Illustration": "modern Chinese illustration blending traditional ink aesthetics with contemporary digital art, featuring elegant flowing lines, poetic composition, and dreamlike color harmony",
        "年画 / New Year Painting": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D vibrant Chinese folk New Year woodblock print. Characters are NOT real people — their bodies are bold primary color blocks with thick black outlines on flat printed paper. Door gods step out of frames and walk, carp leap as printed patterns. NO real skin, NO 3D — only folk print on paper.",
        "布艺 / Fabric Art": "GLOBAL MATERIAL OVERRIDE — the entire visual world is constructed from sewn fabric and textiles. Characters are NOT real people — they are cloth dolls with stitched seams, button eyes, embroidered facial features, yarn hair, and patchwork clothing. Environments are quilted fabric landscapes: grass is green felt, water is flowing blue silk, clouds are tufted cotton, trees are embroidered tapestry. Every surface shows visible thread, stitching, and fabric grain. NO realistic skin, NO real materials — only fabric and thread.",
        "蜡笔画 / Crayon drawing": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D wax crayon drawing on textured paper. Characters are NOT real people — their bodies are waxy crayon strokes on paper with the paper grain visible through the wax. Bright colors have the distinctive grainy, slightly uneven crayon texture. Lines are thick and waxy with visible stroke direction. Paper texture shows through all color areas. NO 3D depth, NO CG, NO realistic skin — only crayon on paper.",
        "哥特萝莉 / Gothic Lolita": "Gothic Lolita fashion and atmosphere — NOT a material override but a costume and world style. Characters wear elaborate dark Victorian-inspired Lolita clothing: lace-trimmed black dresses, ruffled petticoats, corsets, platform boots, ornate headpieces with ribbons and roses. Architecture is moody Gothic with pointed arches, stained glass, wrought iron. Dramatic chiaroscuro lighting with deep shadows. Color palette: black, deep purple, burgundy, ivory, silver accents. Atmosphere is darkly romantic and theatrical.",
    }

    _STYLE_HINTS_ZH = {
        "电影感 / Cinematic": "电影感的灯光，浅景深、胶片颗粒、专业调色。",
        "实拍 / Live-action": "照片级实拍素材，自然光线与真实布景。",
        "复古胶片 / Vintage film": "复古胶片质感，暖色调、轻微颗粒，怀旧氛围。",
        "黑白电影 / Black & White": "高对比度黑白电影摄影，戏剧性阴影。",
        "纪录片 / Documentary": "观察式纪录片风格，自然手持摄影与随性构图。",
        "极简广告 / Minimalist commercial": "干净极简的产品电影摄影，平滑推轨、柔和均匀光线、简洁构图。",
        "微距摄影 / Macro photography": "极限特写微距镜头，极浅景深，揭示细腻的纹理与细节。",
        "航拍 / Aerial drone": "开阔航拍无人机镜头，宽大视野、缓慢宏大的揭示、广阔景观。",
        "二维动画 / 2D-animated": "传统二维手绘动画，表现力线条与流畅的角色运动。",
        "三维CG / 3D CG": "高质量三维渲染，逼真材质、全局光照、流畅动画。",
        "日系二次元 / Anime": "日本动漫赛璐璐上色，鲜艳饱和色彩、干净线条、表现力角色设计。",
        "美式漫画 / American Comic": "美式漫画风格，粗犷黑色墨水描边、半调网点阴影、动感构图。",
        "皮克斯3D / Pixar-style 3D": "皮克斯级三维，平滑曲面、丰富鲜艳色彩、表现力角色动画、精致灯光。",
        "定格动画 / Stop-motion": "全局材质覆盖——整个视觉世界是定格动画。角色是手工制作的偶，带可见材质纹理，逐帧触点式卡顿运动。环境是微缩实体布景，用真实布料、手绘背景、实拍灯光。一切都是摄影机下的实体模型。",
        "手绘发光 / Hand-drawn glow": "全局材质覆盖——整个视觉世界是暗纸上的粗糙手绘线稿。角色与环境以发光的霓虹色描边勾勒，有机地闪烁脉动。光迹如残影般跟随运动。世界本身是活的画，每条线被实时重绘。",
        "像素艺术 / Pixel art": "全局材质覆盖——整个视觉世界由可见像素块构成。角色、环境、水、火、烟、天空——一切由清晰的方形像素组成，使用有限的复古调色板。运动为低帧率逐帧，刻意像素级变化。粒子是单个像素点。像素网格就是宇宙本身。",
        "赛博朋克 / Cyberpunk": "高对比霓虹赛博朋克都市，雨湿街道、全息屏与镀铬义体。",
        "蒸汽朋克 / Steampunk": "精密的黄铜机械与维多利亚蒸汽科技，铜管、齿轮、棕褐色调。",
        "故障艺术 / Glitch art": "数字故障扭曲，RGB 通道分离、扫描线、数据损坏伪影、VHS 噪点。",
        "羊毛毡 / Wool felt": "全局材质覆盖——整个视觉世界由毛茸的羊毛毡手工制成。角色有柔软的毡布身体，可见纤维纹理与缝合线迹。风拂动毡草，毡水随纤维流动，毡云飘过毡天空。环境是缝制毡立体布景。不要把毡玩具放进真实场景——一切都是毡。",
        "折纸 / Origami": "全局材质覆盖——整个宇宙由折纸构成。角色是折纸人偶，有锐利干净的折痕与几何折叠的形体。纸鸟扇动带折痕的翅膀，纸水以折叠层涟漪，纸火如卷曲纸片爆裂。世界本身是纸——万物皆被折叠、压痕、干脆。",
        "水彩 / Watercolor": "全局材质覆盖——整个视觉世界是纸上的二维水彩画。角色不是真人——身体是半透明色洗，脸是湿纸上的柔和色晕，边缘溶入纸纹。头发是晕染色带，皮肤是纸的白注染色洗。雨如颜料水滴落，光透过层叠色洗扩散。没有写实皮肤、没有三维——只有纸上的湿颜料。",
        "粘土动画 / Claymation": "全局材质覆盖——整个视觉世界是手塑粘土。角色不是真人——身体是带圆润触感的粘土，可见指纹与工具痕。头发是塑形粘土丝，皮肤是光滑的橡皮泥，衣服是压制的粘土片。粘土水以雕塑液滴飞溅，粘土烟以可塑团翻滚。没有真实皮肤——只有人手塑形的粘土。",
        "水墨 / Ink wash": "全局材质覆盖——整个视觉世界是宣纸上的二维水墨画。角色不是真人——身体是流动的黑色笔触，脸是纸上的墨线，衣服是分级墨洗。头发如挥洒笔触流动，肤色是纸本白加墨晕。水如飞溅墨点，风留笔触轨迹，雾是湿纸上的晕墨。没有写实皮肤、没有真实布料、没有三维——只有墨与纸。",
        "油画 / Oil painting": "全局材质覆盖——整个视觉世界是画布上的二维油画。角色不是真人——身体是刮刀堆叠的厚油彩，脸由层叠笔触构建，衣服是厚涂颜料。头发是扫过的颜料，皮肤是画布上调和的油彩，眼睛是精确的笔触点。水以厚重油彩涟漪，火是刮刀质感，云是涂抹的白漆。没有写实皮肤、没有真实布料、没有三维——只有画布上的油彩。",
        "纸艺拼贴 / Paper collage": "全局材质覆盖——整个视觉世界是层叠撕纸。角色不是真人——身体从带纹理的纸上裁出、撕边，脸是印刷纸片，衣服是不同纸张（新闻纸、牛皮纸、薄页纸）。纸鸟扇动撕边翅膀，纸水在层叠纸上涟漪。没有真实皮肤——只有纸。",
        "剪纸 / Paper cutout": "全局材质覆盖——整个视觉世界是中国剪纸艺术。角色不是真人——身体是精巧的红色纸剪影，剪出对称图案，用纸关节活动。阴影透过纸格投下戏剧形状。没有真实皮肤——只有剪纸。",
        "铅笔素描 / Pencil sketch": "全局材质覆盖——整个视觉世界是纹理纸上的二维石墨铅笔素描。角色不是真人——身体是石墨线条、排线与交叉排线，落在纸上。脸是速写铅笔痕，头发是扫过的石墨笔触，肤色是纸的白随手压力变化。运动是线条擦除重画。橡皮痕留下幽灵尾迹。没有写实皮肤、没有三维——只有纸上的铅笔。",
        "浮世绘 / Ukiyo-e": "全局材质覆盖——整个视觉世界是二维日本浮世绘木版画。角色不是真人——身体是印在和纸上的平涂色块配粗黑轮廓。脸是印刷的木刻五官，头发是雕线黑墨，衣服是平涂色块。没有写实皮肤、没有三维——只有和纸上的木刻墨。",
        "敦煌壁画 / Dunhuang Murals": "全局材质覆盖——整个视觉世界是壁画墙上的二维动画敦煌洞窟壁画。角色不是真人——身体是矿物颜料绘画（赭石、绿松石、青金石）配风化石壁裂纹。飞天拖曳褪色颜料飘带。没有写实皮肤、没有三维——只有灰泥上的古壁画矿物颜料。",
        "青花瓷 / Blue-white Porcelain": "全局材质覆盖——整个宇宙是活的三维青花瓷。角色不是真人——身体是白釉瓷，钴蓝手绘图案作为五官与衣服在皮肤上流动。瓷鸟以咔哒瓷翼起飞，瓷水如液态釉流动，瓷树绽放钴蓝花。没有真实皮肤——只有釉面瓷。",
        "工笔画 / Gongbi Painting": "全局材质覆盖——整个视觉世界是平绢上的二维工笔画。角色不是真人——身体是极细笔描边填平涂矿物色洗，落于绢上。每根发丝每片花瓣都被单独描绘。颜料下可见绢丝纤维。没有写实皮肤、没有三维——只有笔与绢。",
        "皮影戏 / Shadow Puppetry": "全局材质覆盖——整个视觉世界是半透明幕上的二维皮影戏。角色不是真人——身体是精巧雕刻的皮影剪影，带关节，由温暖琥珀背光照亮。没有写实皮肤、没有三维——只有幕上的皮革影。",
        "中国风插画 / Chinese Illustration": "现代中国插画，融合传统水墨美学与当代数字艺术，优雅流畅线条、诗意构图、梦幻色彩和谐。",
        "年画 / New Year Painting": "全局材质覆盖——整个视觉世界是二维鲜艳的中国民间年画木版印刷。角色不是真人——身体是平印纸上的大胆原色块配粗黑轮廓。门神走出画框行走，鲤鱼作为印刷图案跃起。没有写实皮肤、没有三维——只有民间印刷在纸上。",
        "布艺 / Fabric Art": "全局材质覆盖——整个视觉世界由缝制布料与纺织品构成。角色不是真人——是有缝线的布偶、纽扣眼、刺绣五官、纱线头发与拼布衣服。环境是绗缝布景：草是绿毡、水是流动蓝绸、云是簇绒棉、树是刺绣挂毯。每面可见线、缝线与布纹。没有写实皮肤、没有真实材质——只有布与线。",
        "蜡笔画 / Crayon drawing": "全局材质覆盖——整个视觉世界是纹理纸上的二维蜡笔画。角色不是真人——身体是纸上的蜡质蜡笔笔触，纸纹透过蜡可见。亮色具有独特的颗粒、稍不均匀的蜡笔质感。线条粗且蜡质，可见笔触方向。所有色区透出纸纹。没有三维深度、没有数字CG、没有写实皮肤——只有纸上的蜡笔。",
        "哥特萝莉 / Gothic Lolita": "哥特萝莉服饰与氛围——不是材质覆盖而是服装与世界风格。角色穿精致暗黑维多利亚风萝莉装：蕾丝黑裙、荷叶衬裙、束腰、厚底靴、带丝带与玫瑰的华丽头饰。建筑是阴郁哥特，尖拱、彩窗、锻铁。戏剧性明暗光线，深阴影。调色：黑、深紫、酒红、象牙白、银点缀。氛围暗黑浪漫且戏剧化。",
    }

    _MUSIC = [
        "禁止音乐 / No Music",
        "不指定 / Unspecified",
        # � 电影配乐（按场景/情绪，符合官方 non_diegetic_music 写法：乐器+速度+节奏+动态）
        "史诗战争 / Epic Orchestral",
        "动作追逐 / Action Chase",
        "紧张悬疑 / Tense Suspense",
        "恐怖惊悚 / Horror Atmosphere",
        "温馨治愈 / Warm & Gentle",
        "浪漫爱情 / Romantic Strings",
        "悲伤抒情 / Melancholic",
        "轻松喜剧 / Light Comedy",
        "古风武侠 / Chinese Wuxia",
        "科幻未来 / Sci-fi Electronic",
        "神秘探索 / Mysterious Adventure",
        "史诗悲剧 / Tragic Epic",
    ]

    _MUSIC_HINTS = {
        "禁止音乐 / No Music": "ABSOLUTELY NO background music of any kind. non_diegetic_music MUST be \"N/A\". Do not add any score, melody, or rhythm.",
        "史诗战争 / Epic Orchestral": "a full orchestral score with powerful brass, thundering timpani, and swelling strings at a moderate tempo, building in intensity",
        "动作追逐 / Action Chase": "driving percussion and fast string ostinatos at a fast tempo with sudden dynamic swells",
        "紧张悬疑 / Tense Suspense": "low sustained string drones with sparse dissonant piano notes and sudden percussive stabs at a slow tempo",
        "恐怖惊悚 / Horror Atmosphere": "deep low-frequency drones with sparse metallic scrapes and sudden dissonant swells at a very slow tempo",
        "温馨治愈 / Warm & Gentle": "sparse solo piano notes at a slow tempo with soft sustained chords and a gentle fade at the end",
        "浪漫爱情 / Romantic Strings": "lush violins and gentle cello at a slow tempo with harp glissandos, gradually swelling and fading",
        "悲伤抒情 / Melancholic": "a slow solo cello melody with sparse piano accompaniment, gradually decreasing in volume",
        "轻松喜剧 / Light Comedy": "playful pizzicato strings and light woodwinds at a brisk tempo with bouncy rhythmic accents",
        "古风武侠 / Chinese Wuxia": "guqin and erhu with flowing pentatonic melodies at a slow tempo, joined by sparse percussion",
        "科幻未来 / Sci-fi Electronic": "a low electronic pulse with atmospheric synth pads at a slow tempo and subtle rhythmic layers",
        "神秘探索 / Mysterious Adventure": "warm woodwinds and soft strings at a moderate tempo with gentle dynamic swells",
        "史诗悲剧 / Tragic Epic": "a slow orchestral theme with muted brass and low strings, fading out softly",
    }

    _MUSIC_HINTS_ZH = {
        "禁止音乐 / No Music": "绝对没有任何背景音乐；non_diegetic_music 必须严格输出 \"N/A\"，不得添加任何配乐/旋律/节奏。",
        "史诗战争 / Epic Orchestral": "史诗管弦乐配乐，强劲铜管、轰鸣定音鼓、层层递进的弦乐，中速推进、气势不断攀升。",
        "动作追逐 / Action Chase": "急促打击乐与快速弦乐固定音型，快节奏，随动作起伏陡然增强。",
        "紧张悬疑 / Tense Suspense": "低音持续弦乐长音，配稀疏不和谐钢琴单音与突然的打击乐重击，缓慢节奏，营造紧张。",
        "恐怖惊悚 / Horror Atmosphere": "低沉低频持续音、稀疏金属刮擦声与突然的不和谐膨胀，极慢节奏，营造阴森。",
        "温馨治愈 / Warm & Gentle": "稀疏钢琴独奏，慢速，柔和持续和弦，结尾轻柔淡出。",
        "浪漫爱情 / Romantic Strings": "丰满小提琴与温柔大提琴，慢速，竖琴滑奏，渐强渐淡。",
        "悲伤抒情 / Melancholic": "缓慢的独奏大提琴旋律，配稀疏钢琴伴奏，音量渐渐减弱。",
        "轻松喜剧 / Light Comedy": "俏皮拨弦弦乐与轻快木管，轻快节奏，活泼跳跃的重音。",
        "古风武侠 / Chinese Wuxia": "古琴与二胡，五声旋律流动，缓慢节奏，配稀疏打击乐。",
        "科幻未来 / Sci-fi Electronic": "低频电子脉冲，合成器氛围垫音，缓慢节奏，微妙的节奏层。",
        "神秘探索 / Mysterious Adventure": "温暖木管与柔和弦乐，中速，柔和起伏的动态。",
        "史诗悲剧 / Tragic Epic": "缓慢管弦乐主题，弱奏铜管与低音弦乐，结尾轻柔淡出。",
    }

    _ASPECTS = [
        "16:9", "9:16", "4:3", "3:4", "1:1", "21:9", "4:5", "5:4",
    ]

    _ASPECT_HINTS = {
        "16:9": "standard widescreen — use wide establishing shots, horizontal subject placement, cinematic scope",
        "9:16": "vertical portrait — center subjects vertically, stack elements top-to-bottom, leave breathing room above and below",
        "4:3": "classic Academy ratio — balanced framed composition, suited for dialogue and character-focused shots",
        "3:4": "tall portrait — vertical emphasis, subjects fill the frame from top to bottom, dramatic low/high angles",
        "1:1": "square format — symmetrical center-weighted composition, subjects centered in frame",
        "21:9": "ultrawide cinematic — emphasize sweeping horizontal space, panoramic landscapes, subjects placed off-center with vast negative space",
        "4:5": "portrait tall — slightly taller than 3:4, ideal for social media portrait, subjects framed with vertical breathing room",
        "5:4": "landscape tall — slightly taller than standard, balanced composition with modest horizontal emphasis",
    }

    _CUTS = [
        "不指定 / Unspecified",
        "不切镜 / Single Shot",
        "1 次切镜 / 1 Cut",
        "2 次切镜 / 2 Cuts",
        "3 次切镜 / 3 Cuts",
        "4 次切镜 / 4 Cuts",
        "5 次切镜 / 5 Cuts",
        "6 次切镜 / 6 Cuts",
        "7 次切镜 / 7 Cuts",
        "8 次切镜 / 8 Cuts",
        "9 次切镜 / 9 Cuts",
    ]

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "预设模式": ([
                    "纯文本生成音视频[英文]-T2VA [EN]", "纯文本生成音视频[中文]-T2VA [ZH]",
                    "首帧图生成音视频[英文]-I2VA [EN]", "首帧图生成音视频[中文]-I2VA [ZH]",
                    "首尾帧生成音视频[英文]-FL2VA [EN]", "首尾帧生成音视频[中文]-FL2VA [ZH]",
                    "尾帧图生成音视频[英文]-L2VA [EN]", "尾帧图生成音视频[中文]-L2VA [ZH]",
                ],),
                "视频时长": ("INT", {"default": 8, "min": 4, "max": 15, "step": 1,
                    "tooltip": "视频时长 (秒), MiniMax H3 支持 4–15 秒"}),
                "视觉风格": (s._STYLES, {"default": "不指定 / Unspecified"}),
                "音乐风格": (s._MUSIC, {"default": "禁止音乐 / No Music"}),
                "画面比例": (s._ASPECTS, {"default": "16:9"}),
                "切镜次数": (s._CUTS, {"default": "不指定 / Unspecified"}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("system_prompt",)
    FUNCTION = "build"
    CATEGORY = "JZL/MiniMax"

    def build(self, 预设模式, 视频时长, 视觉风格, 音乐风格, 画面比例, 切镜次数):
        preset = 预设模式
        duration = 视频时长
        style = 视觉风格
        music = 音乐风格
        aspect = 画面比例
        cuts = 切镜次数
        match preset:
            case "纯文本生成音视频[英文]-T2VA [EN]":   base = MINIMAX_T2VA_EN
            case "纯文本生成音视频[中文]-T2VA [ZH]":   base = MINIMAX_T2VA_ZH
            case "首帧图生成音视频[英文]-I2VA [EN]":   base = MINIMAX_I2VA_EN
            case "首帧图生成音视频[中文]-I2VA [ZH]":   base = MINIMAX_I2VA_ZH
            case "首尾帧生成音视频[英文]-FL2VA [EN]":   base = MINIMAX_FL2VA_EN
            case "首尾帧生成音视频[中文]-FL2VA [ZH]":   base = MINIMAX_FL2VA_ZH
            case "尾帧图生成音视频[英文]-L2VA [EN]":   base = MINIMAX_L2VA_EN
            case "尾帧图生成音视频[中文]-L2VA [ZH]":   base = MINIMAX_L2VA_ZH
            case _: raise ValueError(f'未知预设: "{preset}"')

        is_zh = "[ZH]" in preset
        params = []

        if is_zh:
            params.append("## 目标视频参数（必须严格遵守）")
            params.append(f"- 视频时长：正好 {duration} 秒（镜头切分和时间戳必须精确落在此范围内，最后一个镜头必须在第{duration}秒前结束）")
            if "不指定" not in style:
                en_name = style.split(" / ")[0]
                zh_name = style.split(" / ")[-1]
                hint = self._STYLE_HINTS_ZH.get(style, "")
                params.append(f"- 视觉风格：{zh_name} ({en_name}) — {hint}")
                params.append(f"  ⚠️ [Shot 1] 必须以 \"{en_name}\" 开头，然后立即用1-2句话详细描述{zh_name}在画面中的具体视觉呈现——材质、光影、色彩、动作特征。严禁只写风格名称就跳到下一句！必须写出该风格\"长什么样\"。")
                params.append(f"  正确示例: \"[Shot 1] 粘土动画，画面中的角色呈现手工泥塑的圆润质感，表面可见细微指痕和工具刮痕，动作带有定格动画特有的逐帧卡顿节奏...\"")
                params.append(f"  错误示例: \"[Shot 1] 粘土动画，中全景镜头...\"（只写了名称，没有视觉描述）")
            if "不指定" not in music:
                zh_name = music.split(" / ")[-1]
                hint = self._MUSIC_HINTS_ZH.get(music, "")
                params.append(f"- 背景音乐风格：{zh_name} — {hint}")
                if "禁止音乐" in music:
                    params.append(f"  ⚠️ 整个视频不得出现任何背景音乐。non_diegetic_music 必须严格输出 \"N/A\"。")
            if "不指定" not in cuts:
                if "不切镜" in cuts:
                    params.append(f"- 切镜：固定镜头，不切镜。整段视频只有 [Shot 1] 一个镜头。仅通过运镜（摇摄、俯仰、横移、变焦、跟拍、推拉）改变视角。严禁输出 [Shot N] 时间戳。")
                else:
                    n = int(cuts.split(" ")[0])
                    max_ok = max(1, duration // 2)
                    if n > max_ok:
                        params.append(f"- ⚠️ 切镜次数 {n} 对于 {duration} 秒视频过多（合理上限 {max_ok} 次）。请根据实际可用时长自行削减到合理范围，保证每个镜头至少 2 秒。")
                    params.append(f"- 切镜：正好 {n} 次（共 {n+1} 个镜头）。所有镜头必须在 {duration} 秒内完成。切镜时机遵循叙事节奏——紧张段落切快、抒情段落切慢，不可均分。每次切镜必须以 [Shot N] At MM:SS.mmm 开头，时间戳严格递增。")
            hint = self._ASPECT_HINTS.get(aspect, "")
            params.append(f"- 画面比例：{aspect} — {hint}")
            params.append(f"  所有镜头构图、主体位置、留白空间必须匹配 {aspect} 比例。")
        else:
            params.append("## Target Video Parameters (MUST follow exactly)")
            params.append(f"- Duration: exactly {duration} seconds (all shot timestamps MUST fall within this range; the final shot must end before the {duration}-second mark)")
            if "Unspecified" not in style:
                en_name = style.split(" / ")[0]
                hint = self._STYLE_HINTS.get(style, "")
                params.append(f"- Visual style: {en_name} — {hint}")
                params.append(f"  ⚠️ [Shot 1] MUST begin with \"{en_name}\" and immediately elaborate with 1-2 sentences of concrete visual description — textures, lighting, colors, motion characteristics that define this style. Do NOT just name the style and move on. Show what the style actually looks like.")
                params.append(f"  Correct: \"[Shot 1] Claymation, the characters have the rounded tactile quality of hand-sculpted clay with visible fingerprints and tool marks, their movements carrying the distinctive frame-by-frame stutter of stop-motion...\"")
                params.append(f"  Wrong: \"[Shot 1] Claymation, a medium-wide shot...\" (name only, no visual description)")
            if "Unspecified" not in music:
                en_name = music.split(" / ")[0]
                hint = self._MUSIC_HINTS.get(music, "")
                params.append(f"- Background music style: {en_name} — {hint}")
                if "No Music" in music:
                    params.append(f"  ⚠️ The video must have absolutely no background music. non_diegetic_music MUST be \"N/A\".")
            if "Unspecified" not in cuts:
                if "Single Shot" in cuts:
                    params.append(f"- Cuts: Single continuous shot — NO cuts. The entire video is only [Shot 1]. Use camera movement only (pan, tilt, truck, zoom, tracking, push/pull) to change viewpoint. Do NOT output any [Shot N] timestamps.")
                else:
                    n = int(cuts.split(" ")[0])
                    max_ok = max(1, duration // 2)
                    if n > max_ok:
                        params.append(f"- ⚠️ {n} cuts is excessive for a {duration}-second video (reasonable max: {max_ok}). Reduce to a feasible number, ensuring at least 2 seconds per shot.")
                    params.append(f"- Cuts: exactly {n} cut(s) (meaning {n+1} total shots). All shots must fit within {duration} seconds. Cut timing must follow narrative rhythm — faster cuts for tense/action moments, longer holds for calm/emotional moments. Do NOT space cuts evenly. Every cut MUST begin with [Shot N] At MM:SS.mmm with strictly increasing timestamps.")
            hint = self._ASPECT_HINTS.get(aspect, "")
            params.append(f"- Aspect ratio: {aspect} — {hint}")
            params.append(f"  All shot compositions, subject placement, and negative space must be framed for {aspect}.")
        params.append("")
        param_block = "\n".join(params)

        marker = "## "
        idx = base.find(marker)
        if idx > 0:
            result = base[:idx].rstrip() + "\n\n" + param_block + base[idx:]
        else:
            result = param_block + "\n" + base

        return (result,)


class JZL_MiniMaxRef2vaPreset:
    """MiniMax H3 Ref2VA 全引用模式专用 — 多模态参考 + 风格策略"""

    _STYLES = [
        "保持统一风格 / Consistent Style",
        "多种风格混搭 / Mixed Styles",
        "多种风格转换 / Style Transformation",
    ]

    _STYLE_HINTS = {
        "保持统一风格 / Consistent Style": "STRICT STYLE CONSISTENCY — all characters, environments, and visual elements must share the exact same visual style derived from the reference images. Every frame must look like it belongs to a single unified visual universe. No character should look like they came from a different artwork.",
        "多种风格混搭 / Mixed Styles": "STYLE MIXING — different characters or elements may retain their distinct visual styles from their respective references. Examples: a live-action person interacting with a 2D-animated character; two pixel-art characters walking through a photorealistic background; a clay figure next to an origami figure. CRITICAL: each reference element must remain visually STABLE — a real person must stay real, an anime character must stay anime, clay must stay clay throughout the video. The challenge is making them coexist naturally in the same space.",
        "多种风格转换 / Style Transformation": "STYLE TRANSFORMATION — the ENTIRE frame undergoes a smooth, visible transition from one visual style to another over the course of the video. Examples: live-action gradually becomes 2D-animated; claymation transforms into origami; watercolor washes over a realistic scene. ALL shapes, proportions, and spatial relationships must be preserved during the transformation — only the rendering style changes. The transformation must be smooth and continuous, not an abrupt switch.",
    }

    _STYLE_HINTS_ZH = {
        "保持统一风格 / Consistent Style": "严格风格一致——所有角色、环境与视觉元素必须共享由参考图得出的完全相同的视觉风格。每帧都必须像属于同一个统一视觉宇宙。没有任何角色看起来像来自另一幅作品。",
        "多种风格混搭 / Mixed Styles": "风格混搭——不同角色或元素可保留各自参考中的独特风格。示例：真人角色与二维动画角色互动；两个像素艺术角色走过照片级背景；粘土人偶旁边放折纸人偶。关键：每个参考元素必须在视觉上保持稳定——真人在整个视频里保持真实，动漫角色保持动漫，粘土保持粘土。挑战是让它们在同一个空间自然共存。",
        "多种风格转换 / Style Transformation": "风格转换——整个画面在视频过程中从一种视觉风格平滑、可见地过渡到另一种。示例：真人逐渐变成二维动画；粘土动画变成折纸；水彩晕染写实场景。转换期间所有形状、比例与空间关系必须保持——只有渲染风格变化。转换必须平滑连续，而非生硬切换。",
    }

    _CUTS = JZL_MiniMaxPreset._CUTS
    _MUSIC = JZL_MiniMaxPreset._MUSIC
    _MUSIC_HINTS = JZL_MiniMaxPreset._MUSIC_HINTS
    _MUSIC_HINTS_ZH = JZL_MiniMaxPreset._MUSIC_HINTS_ZH
    _ASPECTS = JZL_MiniMaxPreset._ASPECTS
    _ASPECT_HINTS = JZL_MiniMaxPreset._ASPECT_HINTS

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "预设模式": ([
                    "多参考生成音视频[英文]-Ref2VA [EN]",
                    "多参考生成音视频[中文]-Ref2VA [ZH]",
                ],),
                "参考图片介绍": ("STRING", {"default": "", "multiline": True,
                    "placeholder": "输入参考图片描述，例：图一男主角特写，图二女主角全身，图三背景街道..."}),
                "参考视频介绍": ("STRING", {"default": "", "multiline": True,
                    "placeholder": "输入参考视频描述，例：视频一运镜参考，视频二角色动作参考..."}),
                "参考音频介绍": ("STRING", {"default": "", "multiline": True,
                    "placeholder": "输入参考音频描述，例：音频一男主音色参考，音频二女主音色参考..."}),
                "视频时长": ("INT", {"default": 8, "min": 4, "max": 15, "step": 1,
                    "tooltip": "视频时长 (秒), MiniMax H3 支持 4–15 秒"}),
                "视觉风格": (s._STYLES, {"default": "保持统一风格 / Consistent Style"}),
                "音乐风格": (s._MUSIC, {"default": "禁止音乐 / No Music"}),
                "画面比例": (s._ASPECTS, {"default": "16:9"}),
                "切镜次数": (s._CUTS, {"default": "不指定 / Unspecified"}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("system_prompt",)
    FUNCTION = "build"
    CATEGORY = "JZL/MiniMax"

    def build(self, 预设模式, 参考图片介绍, 参考视频介绍, 参考音频介绍, 视频时长, 视觉风格, 音乐风格, 画面比例, 切镜次数):
        preset = 预设模式
        imgs = 参考图片介绍
        vids = 参考视频介绍
        auds = 参考音频介绍
        duration = 视频时长
        style = 视觉风格
        music = 音乐风格
        aspect = 画面比例
        cuts = 切镜次数

        match preset:
            case "多参考生成音视频[英文]-Ref2VA [EN]":   base = MINIMAX_REF2VA_EN
            case "多参考生成音视频[中文]-Ref2VA [ZH]":   base = MINIMAX_REF2VA_ZH
            case _: raise ValueError(f'未知预设: "{preset}"')

        is_zh = "[ZH]" in preset
        params = []

        # ── 参考素材注入 ──
        if is_zh:
            params.append("## 目标视频参数（必须严格遵守）")
            if imgs.strip():
                params.append(f"- 参考图片说明：{imgs.strip()}")
            if vids.strip():
                params.append(f"- 参考视频说明：{vids.strip()}")
            if auds.strip():
                params.append(f"- 参考音频说明：{auds.strip()}")
            params.append(f"- 视频时长：正好 {duration} 秒（镜头切分和时间戳必须精确落在此范围内，最后一个镜头必须在第{duration}秒前结束）")
            hint = self._STYLE_HINTS_ZH.get(style, "")
            params.append(f"- 视觉风格策略：{style.split('/')[-1].strip()} ({style.split('/')[0].strip()}) — {hint}")
            if "不指定" not in music:
                zh_name = music.split(" / ")[-1]
                hint_m = self._MUSIC_HINTS_ZH.get(music, "")
                params.append(f"- 背景音乐风格：{zh_name} — {hint_m}")
                if "禁止音乐" in music:
                    params.append(f"  ⚠️ 整个视频不得出现任何背景音乐。non_diegetic_music 必须严格输出 \"N/A\"。")
            if "不指定" not in cuts:
                if "不切镜" in cuts:
                    params.append(f"- 切镜：固定镜头，不切镜。整段视频只有 [Shot 1] 一个镜头。仅通过运镜改变视角。严禁输出 [Shot N] 时间戳。")
                else:
                    n = int(cuts.split(" ")[0])
                    max_ok = max(1, duration // 2)
                    if n > max_ok:
                        params.append(f"- ⚠️ 切镜次数 {n} 对于 {duration} 秒视频过多（合理上限 {max_ok} 次）。请自行削减。")
                    params.append(f"- 切镜：正好 {n} 次（共 {n+1} 个镜头）。切镜时机遵循叙事节奏，不可均分。每次切镜必须以 [Shot N] At MM:SS.mmm 开头。")
            hint_a = self._ASPECT_HINTS.get(aspect, "")
            params.append(f"- 画面比例：{aspect} — {hint_a}")
            params.append(f"  所有镜头构图、主体位置、留白空间必须匹配 {aspect} 比例。")
        else:
            params.append("## Target Video Parameters (MUST follow exactly)")
            if imgs.strip():
                params.append(f"- Reference image notes: {imgs.strip()}")
            if vids.strip():
                params.append(f"- Reference video notes: {vids.strip()}")
            if auds.strip():
                params.append(f"- Reference audio notes: {auds.strip()}")
            params.append(f"- Duration: exactly {duration} seconds (all shot timestamps MUST fit within this range; the final shot must end before the {duration}-second mark)")
            hint = self._STYLE_HINTS.get(style, "")
            params.append(f"- Visual style strategy: {style.split('/')[0].strip()} — {hint}")
            if "Unspecified" not in music:
                en_name = music.split(" / ")[0]
                hint_m = self._MUSIC_HINTS.get(music, "")
                params.append(f"- Background music style: {en_name} — {hint_m}")
                if "No Music" in music:
                    params.append(f"  ⚠️ The video must have absolutely no background music. non_diegetic_music MUST be \"N/A\".")
            if "Unspecified" not in cuts:
                if "Single Shot" in cuts:
                    params.append(f"- Cuts: Single continuous shot — NO cuts. Only [Shot 1]. Use camera movement only.")
                else:
                    n = int(cuts.split(" ")[0])
                    max_ok = max(1, duration // 2)
                    if n > max_ok:
                        params.append(f"- ⚠️ {n} cuts is excessive for a {duration}-second video (reasonable max: {max_ok}). Reduce.")
                    params.append(f"- Cuts: exactly {n} cut(s) (meaning {n+1} total shots). Cut timing follows narrative rhythm — do NOT space evenly. Every cut MUST begin with [Shot N] At MM:SS.mmm.")
            hint_a = self._ASPECT_HINTS.get(aspect, "")
            params.append(f"- Aspect ratio: {aspect} — {hint_a}")
            params.append(f"  All shot compositions must be framed for {aspect}.")
        params.append("")
        param_block = "\n".join(params)

        marker = "## "
        idx = base.find(marker)
        if idx > 0:
            result = base[:idx].rstrip() + "\n\n" + param_block + base[idx:]
        else:
            result = param_block + "\n" + base

        return (result,)
