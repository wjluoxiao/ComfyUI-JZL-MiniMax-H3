"""JZL MiniMax — Llama 后端支撑（模型加载存储 + GPU 检测）

从 XB_ToolBox 的 nodes_llama.py 抽取，供「模型加载器 Pro」与「编剧链」独立使用，
不依赖 XB_ToolBox。两个包的 LLAMA_CPP_STORAGE 相互独立，互不干扰。
"""

import os
import gc

import torch

import folder_paths
import comfy.model_management as mm

from .support_llama.gguf_layers import get_layer_count
from .llama_server_backend import LlamaServerBackend


# =============================================================================
# A卡 / N卡 检测工具
# =============================================================================

def is_rocm() -> bool:
    """检测是否为 AMD ROCm 环境"""
    try:
        return torch.cuda.is_available() and hasattr(torch.version, "hip") and torch.version.hip is not None
    except Exception:
        return False


def is_nvidia() -> bool:
    """检测是否为 NVIDIA CUDA 环境"""
    try:
        return torch.cuda.is_available() and not is_rocm()
    except Exception:
        return False


def get_gpu_name() -> str:
    """获取 GPU 名称"""
    try:
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return "Unknown"


def get_vram_gb() -> float:
    """获取 GPU 显存大小 (GB)"""
    try:
        if torch.cuda.is_available():
            return torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    except Exception:
        pass
    return 0.0


def get_amd_arch() -> str:
    """获取 AMD GPU 架构代号"""
    if not is_rocm():
        return ""
    try:
        raw = torch.cuda.get_device_properties(0).gcnArchName
        return raw.split(":")[0] if raw else "unknown"
    except Exception:
        return "unknown"


# AMD 架构显存效率系数: 某些AMD架构的GGML offload效率与N卡不同
_AMD_VRAM_FACTOR = {
    "gfx1201": 1.55,  # RDNA4
    "gfx1200": 1.55,
    "gfx1151": 1.50,  # RDNA3.5
    "gfx1150": 1.50,
    "gfx1103": 1.50,  # RDNA3
    "gfx1102": 1.50,
    "gfx1101": 1.50,
    "gfx1100": 1.50,
    "gfx1037": 1.65,  # RDNA2
    "gfx1036": 1.65,
    "gfx1035": 1.65,
    "gfx1034": 1.65,
    "gfx1032": 1.65,
    "gfx1031": 1.65,
    "gfx1030": 1.65,
    "gfx1012": 1.70,  # RDNA1
    "gfx1011": 1.70,
    "gfx1010": 1.70,
    "gfx942": 1.40,   # CDNA3 (MI300X)
    "gfx90a": 1.40,   # CDNA2
    "gfx908": 1.45,   # CDNA
    "gfx906": 1.70,   # Vega
}


def get_vram_factor() -> float:
    """获取当前 GPU 的显存系数"""
    if is_nvidia():
        return 1.55
    if is_rocm():
        arch = get_amd_arch()
        for k, v in _AMD_VRAM_FACTOR.items():
            if arch.startswith(k):
                return v
        return 1.60  # AMD 默认
    return 1.55  # CPU / fallback


def comfy_gpu_desc() -> str:
    """ComfyUI 当前使用的 torch 设备描述（含显卡型号），供日志与本地 LLM 对齐显卡用。"""
    try:
        dev = mm.get_torch_device()
        if dev is None:
            return "CPU"
        if str(dev.type).startswith("cuda"):
            idx = int(getattr(dev, "index", 0) or 0)
            try:
                return f"{dev} ({torch.cuda.get_device_name(idx)})"
            except Exception:
                return f"{dev}"
        return str(dev)
    except Exception:
        return "unknown"


def print_gpu_info():
    """打印 GPU 信息用于调试"""
    gpu_type = "ROCm (AMD)" if is_rocm() else ("CUDA (NVIDIA)" if is_nvidia() else "CPU")
    gpu_name = get_gpu_name()
    vram = get_vram_gb()
    arch = get_amd_arch() if is_rocm() else ""
    factor = get_vram_factor()
    arch_str = f", arch={arch}" if arch else ""
    print(f"[JZL-llama] GPU: {gpu_type} {gpu_name} VRAM={vram:.1f}GB{arch_str}, factor={factor}")


def _quiet_native_load_logs():
    """压制 llama.cpp 原生库加载期的固定刷屏（如 Qwen-VL 的 load_hparams 提示）。

    只按子串过滤这几条与生成无关的加载提示，其余原生日志（设备/显存/错误）照常输出；
    llama-cpp-python 未提供该接口时静默跳过。
    """
    try:
        from llama_cpp import _logger as _native_logger
        _native_logger.add_log_filters(["load_hparams", "issues/16842"])
    except Exception:
        pass


# =============================================================================
# Chat Handler 列表（静态）
# llama-server 模式下视觉格式由 --mmproj + GGUF 内置模板决定，
# chat_handler 仅作为「是否启用视觉」的开关语义，不再构造 Python handler。
# =============================================================================

chat_handlers = [
    "None",
    "LLaVA-1.5", "LLaVA-1.6", "Moondream2", "nanoLLaVA", "llama3-Vision-Alpha", "MiniCPM-v2.6",
    "Gemma3", "Gemma4",
    "Qwen2.5-VL", "MinerU2.5-Pro",
    "Qwen3-VL", "Qwen3-VL-Thinking",
    "Qwen3.5", "Qwen3.5-Thinking", "Qwen3.6", "Qwen3.6-Thinking",
    "Qwen3.8", "Qwen3.8-Thinking",
    "GLM-4.6V", "GLM-4.6V-Thinking", "GLM-4.1V-Thinking", "LFM2-VL",
    "LFM2.5-VL", "Granite-Docling",
    "MiniCPM-v4.5", "MiniCPM-v4.5-Thinking",
    "MiniCPM-v4.6", "MiniCPM-v4.6-Thinking",
    "PaddleOCR-VL-1.5", "Qwen3-ASR", "Step3-VL",
    "DeepSeek-OCR",
]


# =============================================================================
# AnyType / 存储类
# =============================================================================

class AnyType(str):
    def __ne__(self, __value: object) -> bool:
        return False


def _import_llama_cpp_handlers():
    """惰性导入 llama-cpp-python（仅当用户切换到该后端时才 import）。"""
    import importlib

    try:
        from llama_cpp import Llama
    except Exception as e:
        raise RuntimeError(
            "未安装 llama-cpp-python，无法使用「llama-cpp-python」后端。\n"
            "建议切回「llama-server」后端（推荐）；若确需 llama-cpp-python：\n"
            "N卡: https://github.com/JamePeng/llama-cpp-python/releases\n"
            "A卡: 请使用 ROCm/HIP 编译的 llama-cpp-python"
        ) from e

    from llama_cpp.llama_chat_format import (
        Llava15ChatHandler, Llava16ChatHandler, MoondreamChatHandler,
        NanoLlavaChatHandler, Llama3VisionAlphaChatHandler, MiniCPMv26ChatHandler,
    )

    def _try(attr):
        try:
            return getattr(importlib.import_module("llama_cpp.llama_chat_format"), attr)
        except Exception:
            return None

    handlers = {
        "LLaVA-1.5": Llava15ChatHandler,
        "LLaVA-1.6": Llava16ChatHandler,
        "Moondream2": MoondreamChatHandler,
        "nanoLLaVA": NanoLlavaChatHandler,
        "llama3-Vision-Alpha": Llama3VisionAlphaChatHandler,
        "MiniCPM-v2.6": MiniCPMv26ChatHandler,
        "Gemma3": _try("Gemma3ChatHandler"),
        "Gemma4": _try("Gemma4ChatHandler"),
        "Qwen2.5-VL": _try("Qwen25VLChatHandler"),
        "MinerU2.5-Pro": _try("Qwen25VLChatHandler"),
        "Qwen3-VL": _try("Qwen3VLChatHandler"),
        "Qwen3-VL-Thinking": _try("Qwen3VLChatHandler"),
        "Qwen3.5": _try("Qwen35ChatHandler"),
        "Qwen3.5-Thinking": _try("Qwen35ChatHandler"),
        "Qwen3.6": _try("Qwen35ChatHandler"),
        "Qwen3.6-Thinking": _try("Qwen35ChatHandler"),
        "Qwen3-ASR": _try("Qwen3ASRChatHandler"),
        "GLM-4.6V": _try("GLM46VChatHandler"),
        "GLM-4.6V-Thinking": _try("GLM46VChatHandler"),
        "GLM-4.1V-Thinking": _try("GLM41VChatHandler"),
        "LFM2-VL": _try("LFM2VLChatHandler"),
        "LFM2.5-VL": _try("LFM25VLChatHandler"),
        "Granite-Docling": _try("GraniteDoclingChatHandler"),
        "MiniCPM-v4.5": _try("MiniCPMv45ChatHandler"),
        "MiniCPM-v4.5-Thinking": _try("MiniCPMv45ChatHandler"),
        "MiniCPM-v4.6": _try("MiniCPMv46ChatHandler"),
        "MiniCPM-v4.6-Thinking": _try("MiniCPMv46ChatHandler"),
        "PaddleOCR-VL-1.5": _try("PaddleOCRChatHandler"),
        "Step3-VL": _try("Step3VLChatHandler"),
        "DeepSeek-OCR": _try("MTMDChatHandler"),
    }
    return Llama, handlers


class LLAMA_CPP_STORAGE:
    llm = None
    chat_handler = None
    current_config = None
    messages = {}
    sys_prompts = {}
    backend = None  # "llama-server" 或 "llama-cpp-python"

    @classmethod
    def clean_state(cls, id=-1):
        if id == -1:
            cls.messages.clear()
            cls.sys_prompts.clear()
        else:
            cls.messages.pop(f"{id}", None)
            cls.sys_prompts.pop(f"{id}", None)

    @classmethod
    def clean(cls, all=False):
        try:
            cls.llm.close()
        except Exception:
            pass

        if cls.backend == "llama-cpp-python":
            try:
                cls.chat_handler._exit_stack.close()
            except Exception:
                pass

        cls.llm = None
        cls.chat_handler = None
        cls.current_config = None
        cls.backend = None
        if all:
            cls.clean_state()

        gc.collect()
        mm.soft_empty_cache()

    @classmethod
    def load_model(cls, config, purpose=""):
        """加载本地 LLM。purpose = 本次加载服务的 LLM 步骤（仅用于日志，如「拆解剧本」/「增强提示词」）。"""
        print(f"[JZL-llama] 开始加载本地模型{purpose}中...")
        backend = str(config.get("backend") or "llama-cpp-python").strip().lower()
        if backend == "llama-cpp-python":
            cls._load_model_llama_cpp(config)
        else:
            cls._load_model_server(config)

    @staticmethod
    def _resolve_model_path(model, label="本地 LLM 模型"):
        """把配置里的模型名解析成真实存在的绝对路径。

        容错三种写法：绝对路径 / 二级子目录名（LLM/GPT4/x.gguf）/ 纯文件名。
        都找不到时抛 FileNotFoundError 并列出 models/LLM 下现有模型——比 llama_cpp 抛出的
        「ValueError: Model path does not exist: ...」更能直接告诉用户该改哪个下拉。
        """
        name = str(model or "").strip()
        if not name:
            raise FileNotFoundError(
                f"未选择{label}（请在「大语言模型配置 → 本地 LLM 模型」里选择并保存后重新执行节点）")
        cands = []
        if os.path.isabs(name):
            cands.append(name)
        cands.append(os.path.join(folder_paths.models_dir, "LLM", name))
        try:
            full = folder_paths.get_full_path("LLM", name)
        except Exception:
            full = None
        if full:
            cands.append(full)
        for p in cands:
            if p and os.path.isfile(p):
                return p
        try:
            avail = list(folder_paths.get_filename_list("LLM"))
        except Exception:
            avail = []
        shown = "、".join(avail[:8]) + ("…" if len(avail) > 8 else "") if avail else "models/LLM 下没有可用文件"
        raise FileNotFoundError(
            f"{label}不存在：{name}（models/LLM 下现有：{shown}）。"
            "请在「大语言模型配置 → 本地 LLM 模型」重新选择并保存（保存后重新执行节点）。")

    @staticmethod
    def model_available(model):
        """模型是否真实可加载（不抛异常的探测版，供节点在执行前给出友好提示）。"""
        try:
            LLAMA_CPP_STORAGE._resolve_model_path(model)
            return True
        except Exception:
            return False

    @classmethod
    def _load_model_server(cls, config):
        cls.clean(all=True)
        cls.backend = "llama-server"
        model = config["model"]
        mmproj = config["mmproj"]
        chat_handler = config["chat_handler"]
        n_ctx = config["n_ctx"]
        vram_limit = config["vram_limit"]
        image_max_tokens = config["image_max_tokens"]
        image_min_tokens = config["image_min_tokens"]
        n_gpu_layers = -1

        model_path = cls._resolve_model_path(model)

        # A卡/N卡统一的显存感知层数计算
        vram_factor = get_vram_factor()
        if vram_limit != -1:
            gguf_layers = get_layer_count(model_path) or 32
            gguf_size = os.path.getsize(model_path) * vram_factor / (1024 ** 3)
            gguf_layer_size = gguf_size / gguf_layers

        mmproj_path = None
        if mmproj and mmproj != "None":
            mmproj_path = cls._resolve_model_path(mmproj, label="视觉模块 mmproj ")
            if chat_handler == "None":
                raise ValueError('"chat_handler" 不能为 None! (加载了 mmproj 视觉模块)')

            if vram_limit != -1:
                mmproj_size = os.path.getsize(mmproj_path) * vram_factor / (1024 ** 3)
                n_gpu_layers = max(1, int((vram_limit - mmproj_size) / gguf_layer_size))
        elif vram_limit != -1:
            n_gpu_layers = max(1, int(vram_limit / gguf_layer_size))

        cls.chat_handler = chat_handler

        _gpu_device = config.get("gpu_device")
        if isinstance(_gpu_device, str):
            _gpu_device = None if _gpu_device in ("auto", "None", "") else _gpu_device
        cls.llm = LlamaServerBackend(
            model_path,
            mmproj_path=mmproj_path,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            image_min_tokens=image_min_tokens,
            image_max_tokens=image_max_tokens,
            device=_gpu_device,
        )
        # 记录「真正加载成功的配置」——必须放在加载完成之后：加载失败时若已写入，
        # 之后再用同一份坏配置就会因为「config 相同」而跳过重载、复用一个空 backend。
        cls.current_config = config.copy()

    @classmethod
    def _load_model_llama_cpp(cls, config):
        Llama, handlers = _import_llama_cpp_handlers()
        _quiet_native_load_logs()

        def get_handler(name):
            if name == "None":
                return None
            handler = handlers.get(name)
            if handler is None:
                raise ValueError(
                    f'未知或当前 llama-cpp-python 版本不支持的模型类型: "{name}"'
                )
            return handler

        cls.clean(all=True)
        cls.backend = "llama-cpp-python"
        model = config["model"]
        mmproj = config["mmproj"]
        chat_handler = config["chat_handler"]
        n_ctx = config["n_ctx"]
        vram_limit = config["vram_limit"]
        image_max_tokens = config["image_max_tokens"]
        image_min_tokens = config["image_min_tokens"]
        n_gpu_layers = -1

        model_path = cls._resolve_model_path(model)
        handler = get_handler(chat_handler)

        # A卡/N卡统一的显存感知层数计算
        vram_factor = get_vram_factor()
        if vram_limit != -1:
            gguf_layers = get_layer_count(model_path) or 32
            gguf_size = os.path.getsize(model_path) * vram_factor / (1024 ** 3)
            gguf_layer_size = gguf_size / gguf_layers

        if mmproj and mmproj != "None":
            mmproj_path = cls._resolve_model_path(mmproj, label="视觉模块 mmproj ")
            if handler is None:
                raise ValueError('"chat_handler" 不能为 None! (加载了 mmproj 视觉模块)')

            if vram_limit != -1:
                mmproj_size = os.path.getsize(mmproj_path) * vram_factor / (1024 ** 3)
                n_gpu_layers = max(1, int((vram_limit - mmproj_size) / gguf_layer_size))

            think_mode = "Thinking" in chat_handler
            kwargs = {"clip_model_path": mmproj_path, "verbose": False}
            if chat_handler in ["Qwen3-VL", "Qwen3-VL-Thinking"]:
                kwargs["force_reasoning"] = think_mode
                kwargs["image_max_tokens"] = image_max_tokens
                kwargs["image_min_tokens"] = image_min_tokens
            elif chat_handler in ["MiniCPM-v4.5", "GLM-4.6V", "Qwen3.5"]:
                kwargs["enable_thinking"] = think_mode

            try:
                cls.chat_handler = handler(**kwargs)
            except Exception as e:
                raise RuntimeError(f"{e}\n请更新 llama-cpp-python 版本")
        else:
            if vram_limit != -1:
                n_gpu_layers = max(1, int(vram_limit / gguf_layer_size))
            cls.chat_handler = None

        cls.llm = Llama(
            model_path,
            chat_handler=cls.chat_handler,
            n_gpu_layers=n_gpu_layers,
            n_ctx=n_ctx,
            verbose=False
        )
        # 记录「真正加载成功的配置」——放在加载完成之后（见 _load_model_server 的说明）
        cls.current_config = config.copy()


any_type = AnyType("*")

# 模型卸载钩子
if not hasattr(mm, "unload_all_models_backup"):
    mm.unload_all_models_backup = mm.unload_all_models

    def patched_unload_all_models(*args, **kwargs):
        LLAMA_CPP_STORAGE.clean(all=True)
        result = mm.unload_all_models_backup(*args, **kwargs)
        return result

    mm.unload_all_models = patched_unload_all_models

# LLM 模型文件夹注册
llm_extensions = ['.ckpt', '.pt', '.bin', '.pth', '.safetensors', '.gguf']
folder_paths.folder_names_and_paths["LLM"] = ([os.path.join(folder_paths.models_dir, "LLM")], llm_extensions)

# 打印 GPU 信息
print_gpu_info()

# 检测 llama-server 运行时是否已安装（未安装只提示，不阻塞加载）
try:
    from .llama_server_backend import runtime_is_installed
    if not runtime_is_installed():
        print("[JZL-llama] ================================================")
        print("[JZL-llama] ⚠️ 未检测到 llama.cpp 本地运行时（llama-server）")
        print("[JZL-llama]    首次使用本地模型前，请双击 install_runtime.bat")
        print("[JZL-llama]    或在本目录执行：python install_runtime.py")
        print("[JZL-llama] ================================================")
except Exception:
    pass
