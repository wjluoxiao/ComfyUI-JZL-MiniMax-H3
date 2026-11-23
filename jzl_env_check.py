"""JZL MiniMax 落盘环境自检（诊断用，可单独运行）。

用途：用户报「本段未落盘任何视频」时，让他在自己的 ComfyUI 环境里执行：

    <ComfyUI 的 python> custom_nodes/ComfyUI-JZL-MiniMax-H3/jzl_diagnose.py

把打印出来的报告（同时会落盘成 txt）发回来，就能看到失败机器上真实的环境：
ffmpeg 有没有 / 能不能编码、PyAV(av) 有没有、输出目录能不能写、磁盘剩余空间、
路径长度与非 ASCII 字符、torch/CUDA 等 —— 这些都是「生成成功但落盘失败」的直接原因。

本模块只依赖标准库（存在 av / torch 时做更强的实测编码），
不导入本插件其它模块，保证「插件本身导入失败」时也能跑。
"""

from __future__ import annotations

import os
import sys
import glob
import shutil
import inspect
import platform
import subprocess
import tempfile
import time
import traceback

# ── 落盘链路用到的固定信息（与 nodes_asset_manager.py 保持一致）─────────────
REQUIRED_VIDEO_ENCODERS = ("libx264", "mpeg4")
FALLBACK_NOTE = "ffmpeg 缺失时节点会回退 PyAV(av) 编码；两者都不可用才会落盘失败"


def _run(cmd, timeout=60):
    """执行命令，返回 (returncode, stdout+stderr 文本)。"""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, (p.stdout or b"").decode("utf-8", "ignore")
    except FileNotFoundError as e:
        return -2, f"可执行文件不存在：{e}"
    except subprocess.TimeoutExpired:
        return -3, f"超时（>{timeout}s）"
    except Exception as e:
        return -4, f"{type(e).__name__}: {e}"


def find_ffmpeg():
    """依次尝试 imageio-ffmpeg 自带 → PATH → 常见安装目录，返回 (路径|None, 说明)。"""
    tried = []
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p, f"imageio-ffmpeg 自带：{p}"
        tried.append(f"imageio-ffmpeg 返回的路径不存在：{p}")
    except Exception as e:
        tried.append(f"未安装 imageio-ffmpeg（{type(e).__name__}）")

    p = shutil.which("ffmpeg")
    if p:
        return p, f"PATH 中找到：{p}"
    tried.append("PATH 中没有 ffmpeg")

    pats = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffmpeg.exe"),
        "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg",
    ]
    for p in pats:
        if os.path.isfile(p):
            return p, f"常见安装目录：{p}"
    tried.append("常见安装目录也没有 ffmpeg.exe")

    return None, "；".join(tried)


def ffmpeg_probe(ff):
    """探测 ffmpeg 版本与必需编码器。"""
    out = {}
    if not ff:
        return out
    rc, txt = _run([ff, "-version"], timeout=25)
    out["version_rc"] = rc
    out["version"] = (txt.splitlines() or ["(无输出)"])[0].strip() if txt else ""
    rc2, txt2 = _run([ff, "-hide_banner", "-encoders"], timeout=30)
    out["encoders_rc"] = rc2
    enc = txt2 or ""
    out["encoders"] = {name: (name in enc) for name in REQUIRED_VIDEO_ENCODERS}
    out["has_aac"] = (" aac " in enc) or ("aac " in enc)
    return out


def pyav_probe():
    """探测 PyAV（ComfyUI 自带依赖），返回版本与可用的编码器。"""
    out = {"installed": False}
    try:
        import av
        out["installed"] = True
        out["version"] = getattr(av, "__version__", "?")
        out["libav"] = getattr(getattr(av, "library_versions", {}), "get", lambda *_: None)("libavcodec") or "?"
        codecs = []
        try:
            codecs = [str(c) for c in av.codecs_available]
        except Exception:
            pass
        out["libx264"] = "libx264" in codecs
        out["aac"] = "aac" in codecs
        out["_codec_count"] = len(codecs)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def dir_probe(path):
    """目录可写性 / 剩余空间 / 路径长度 / 非 ASCII 等落盘相关检查。"""
    out = {"path": path, "exists": os.path.isdir(path)}
    try:
        out["abs"] = os.path.abspath(path)
        out["len"] = len(out["abs"])
        out["non_ascii"] = [c for c in out["abs"] if ord(c) > 127][:8]
    except Exception as e:
        out["abs_error"] = f"{type(e).__name__}: {e}"
    try:
        if not out["exists"]:
            os.makedirs(path, exist_ok=True)
            out["created"] = True
        out["writable"] = os.access(path, os.W_OK)
        probe = os.path.join(path, f"._jzl_write_test_{os.getpid()}.tmp")
        with open(probe, "wb") as f:
            f.write(b"jzl")
        os.remove(probe)
        out["write_test"] = "OK"
    except Exception as e:
        out["write_test"] = f"失败：{type(e).__name__}: {e}"
    try:
        du = shutil.disk_usage(path if os.path.isdir(path) else os.path.dirname(path) or ".")
        out["free_gb"] = round(du.free / (1024 ** 3), 2)
        out["total_gb"] = round(du.total / (1024 ** 3), 2)
    except Exception as e:
        out["free_gb"] = f"无法获取：{e}"
    return out


def _smoke_raw_video(workdir, frames=3, w=64, h=64):
    raw = os.path.join(workdir, "smoke.raw")
    with open(raw, "wb") as f:
        f.write(bytes([16, 32, 64]) * (w * h * frames))
    return raw


def smoke_test_ffmpeg(ff, workdir):
    """真机实测：用 3 帧假画面跑一遍 ffmpeg 落盘，分别测 libx264 / mpeg4。"""
    results = {}
    if not ff:
        return {"skipped": "未找到 ffmpeg"}
    raw = _smoke_raw_video(workdir)
    for name, args in (("libx264", ["-crf", "18"]), ("mpeg4", ["-q:v", "5"])):
        out = os.path.join(workdir, f"smoke_{name}.mp4")
        cmd = [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", "64x64", "-r", "24", "-i", raw, "-c:v", name, "-pix_fmt", "yuv420p"] + args + [out]
        rc, txt = _run(cmd, timeout=60)
        ok = rc == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
        results[name] = {"ok": ok, "size": os.path.getsize(out) if os.path.isfile(out) else 0,
                         "error": "" if ok else (txt or "").strip()[-300:]}
    return results


def smoke_test_pyav(workdir):
    """真机实测：用 PyAV 编码 3 帧 H.264 + 1 段 AAC，验证回退编码器可用。"""
    try:
        import av
        import numpy as np
    except Exception as e:
        return {"skipped": f"缺少 av/numpy：{type(e).__name__}: {e}"}
    out = os.path.join(workdir, "smoke_pyav.mp4")
    try:
        with av.open(out, mode="w", format="mp4") as container:
            vs = container.add_stream("libx264", rate=24)
            vs.width, vs.height, vs.pix_fmt = 64, 64, "yuv420p"
            arr = np.zeros((64, 64, 3), dtype="uint8")
            arr[:, :, 1] = 40
            for i in range(3):
                frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
                frame.pts = i
                for pkt in vs.encode(frame):
                    container.mux(pkt)
            for pkt in vs.encode(None):
                container.mux(pkt)
        ok = os.path.isfile(out) and os.path.getsize(out) > 0
        return {"ok": ok, "size": os.path.getsize(out) if ok else 0}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}"}


_COMIFY_ROOT_CACHE = {"probed": False, "root": None}


def comfy_root():
    """定位 ComfyUI 根目录（含 folder_paths.py + comfy/），用于探测版本与依赖 API。

    顺序：环境变量 JZL_COMFY_ROOT → 从本文件向上找 → sys.path → 当前工作目录向上找。
    """
    if _COMIFY_ROOT_CACHE["probed"]:
        return _COMIFY_ROOT_CACHE["root"]

    def _is_root(p):
        return bool(p) and os.path.isfile(os.path.join(p, "folder_paths.py")) \
            and os.path.isdir(os.path.join(p, "comfy"))

    root = None
    env = os.environ.get("JZL_COMFY_ROOT")
    if _is_root(env):
        root = os.path.abspath(env)
    if root is None:
        cur = os.path.dirname(os.path.abspath(__file__))
        for _ in range(6):
            cur = os.path.dirname(cur)
            if _is_root(cur):
                root = cur
                break
    if root is None:
        for p in list(sys.path):
            if _is_root(p):
                root = os.path.abspath(p)
                break
    if root is None:
        cur = os.path.abspath(os.getcwd())
        for _ in range(4):
            if _is_root(cur):
                root = cur
                break
            cur = os.path.dirname(cur)
    _COMIFY_ROOT_CACHE["probed"] = True
    _COMIFY_ROOT_CACHE["root"] = root
    if root and root not in sys.path:
        sys.path.insert(0, root)   # 与 ComfyUI 自身一致，便于 import comfy.* 做 API 体检
    return root


def comfy_output_dir():
    """尽力解析 ComfyUI 的 output 目录（不导入 ComfyUI 也能给个合理猜测）。"""
    try:
        import folder_paths
        return folder_paths.get_output_directory()
    except Exception:
        pass
    root = comfy_root()
    if root:
        cand = os.path.join(root, "output")
        if os.path.isdir(cand):
            return cand
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.abspath(os.path.join(here, "..", "..", "output"))
    return cand if os.path.isdir(cand) else os.path.join(tempfile.gettempdir(), "jzl_output_probe")


def _comfyui_version():
    """ComfyUI 版本（comfyui_version.py / comfy 包元数据 / git 描述）。"""
    comfy_root()
    for mod in ("comfyui_version", "comfy.comfy_version"):
        try:
            import importlib
            m = importlib.import_module(mod)
            for attr in ("__version__", "version", "VERSION"):
                v = getattr(m, attr, None)
                if v:
                    return str(v)
        except Exception:
            pass
    try:
        from importlib.metadata import version as _v
        return _v("comfyui")
    except Exception:
        pass
    try:
        root = comfy_root()
        if root:
            import subprocess as _sp
            p = _sp.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                        stdout=_sp.PIPE, stderr=_sp.DEVNULL, timeout=15)
            if p.returncode == 0:
                return "git " + p.stdout.decode("utf-8", "ignore").strip()
    except Exception:
        pass
    return "未知（无法探测；请手动告知 ComfyUI 版本/提交号）"


def _list_custom_nodes():
    """已安装的 custom_nodes 列表（别人的电脑上常因第三方包冲突/版本漂移而失败）。"""
    root = comfy_root()
    cn = os.path.join(root, "custom_nodes") if root else os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")), "")
    try:
        names = sorted(n for n in os.listdir(cn) if os.path.isdir(os.path.join(cn, n)))
    except Exception:
        return "(无法列举 custom_nodes)"
    return f"{len(names)} 个 → " + ", ".join(names)


def _api_probe():
    """探测本节点依赖的官方 API 在「这台机器」上的真实签名/返回元数。

    这类「元数/签名随 ComfyUI 版本变化」的问题，正是「只有别人的电脑报错、
    开发者本机永远复现不了」的典型原因（例如 too many values to unpack）。
    """
    comfy_root()
    out = []
    try:
        import inspect
        import comfy.utils as _u
        out.append(f"common_upscale   : {inspect.signature(_u.common_upscale)}")
    except Exception as e:
        out.append(f"common_upscale   : ❌ 不可探测（{type(e).__name__}: {e}）")
    try:
        import inspect
        import comfy.sample as _s
        out.append(f"comfy.sample     : {inspect.signature(_s.sample)}")
        out.append(f"sample_custom    : {inspect.signature(_s.sample_custom)}")
    except Exception as e:
        out.append(f"comfy.sample     : ❌ 不可探测（{type(e).__name__}: {e}）")
    try:
        import comfy_extras.nodes_minimax_h3 as _m
        n = len(_m.temporal_shape(120))
        out.append(f"官方 temporal_shape: 返回 {n} 个值（本包期望 3；≠3 即为版本漂移）")
        for attr in ("align_frame_count", "video_latent_t", "adapt_canvas"):
            if not hasattr(_m, attr):
                out.append(f"官方缺函数       : {attr} ❌（版本过旧会让本包行为异常）")
    except Exception as e:
        out.append(f"官方 MiniMax H3   : ❌ 不可探测（{type(e).__name__}: {e}）"
                   f" → 若报 ModuleNotFoundError，说明 ComfyUI 过旧、不含官方 MiniMax H3 支持")
    try:
        import inspect
        import comfy.ldm.minimax.model as _mm
        out.append(f"PackedLayout     : {inspect.signature(_mm.PackedLayout.__init__)}")
    except Exception as e:
        out.append(f"PackedLayout     : ❌ 不可探测（{type(e).__name__}: {e}）")
    try:
        import inspect
        eq = _import_upscaler_execute()
        if eq is None:
            out.append("放大节点         : ❌ 内置 2D/3D 放大网络都不可用（二采不可用，请检查插件文件完整性）")
        else:
            out.append(f"Upscaler.execute : {sorted(inspect.signature(eq).parameters)}")
    except Exception as e:
        out.append(f"放大节点         : ❌ 不可探测（{type(e).__name__}: {e}）")
    out.append("── 关键函数实际来源（若指向别的 custom_nodes，即为第三方包改写了 ComfyUI）──")
    for ln in _source_probe():
        out.append(ln)
    return out


def _source_probe():
    """列出关键依赖函数「真正来自哪个文件」，用于抓第三方节点改写/猴补丁（monkeypatch）。

    别人的电脑上装了 H3 相关的第三方包（Spectrum / LongVideo / PDD-Acc / LTXVideo 等）时，
    它们可能替换 ComfyUI 的 tokenize / encode / sample / PackedLayout → 只有那些机器报错。
    """
    targets = [
        ("comfy.utils.common_upscale", "comfy.utils", "common_upscale"),
        ("comfy.sample.sample", "comfy.sample", "sample"),
        ("comfy.sample.sample_custom", "comfy.sample", "sample_custom"),
        ("node_helpers.conditioning_set_values", "node_helpers", "conditioning_set_values"),
        ("comfy.ldm.minimax.model.PackedLayout", "comfy.ldm.minimax.model", "PackedLayout"),
        ("CLIP.tokenize", "comfy.sd", "CLIP.tokenize"),
        ("CLIP.encode_from_tokens_scheduled", "comfy.sd", "CLIP.encode_from_tokens_scheduled"),
        ("VAE.encode", "comfy.sd", "VAE.encode"),
        ("VAE.decode", "comfy.sd", "VAE.decode"),
        ("comfy_extras.nodes_minimax_h3.temporal_shape", "comfy_extras.nodes_minimax_h3", "temporal_shape"),
    ]
    out = []
    for label, mod_name, attr_path in targets:
        try:
            import importlib
            obj = importlib.import_module(mod_name)
            for part in attr_path.split("."):
                obj = getattr(obj, part)
            fn = getattr(obj, "__func__", obj)
            try:
                src = inspect.getsourcefile(fn) or "?"
                line = inspect.getsourcelines(fn)[1]
                out.append(f"  {label:<44}: {src}:{line}")
            except Exception:
                out.append(f"  {label:<44}: (无法取源) {getattr(fn, '__module__', '?')}"
                           f".{getattr(fn, '__qualname__', '?')}")
        except Exception as e:
            out.append(f"  {label:<44}: ❌ 不可探测（{type(e).__name__}）")
    return out


def _load_pack_nodes():
    """按文件路径加载本包 nodes.py（包目录名常带连字符，无法用 import 语句导入）。"""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "nodes.py")
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("_jzl_pack_nodes", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"[jzl_env_check] 加载本包 nodes.py 失败：{type(e).__name__}: {e}")
        return None


def _helper_drift_probe():
    """对拍官方 MiniMax H3 helper 与本包内置副本（nodes.py）的输出，抓「版本漂移」。

    这是「只有别人的电脑报错」的最高频成因：本包内置了一份官方 helper 副本，
    官方改了返回元数/画布规则后，老副本就会在特定输入下崩（如 too many values to unpack）。
    """
    out = []
    try:
        if not comfy_root():
            return ["❌ 未找到 ComfyUI 根目录 → 无法对拍（请用 ComfyUI 的 python 运行本脚本）"]
        import importlib
        off = importlib.import_module("comfy_extras.nodes_minimax_h3")
        ours = _load_pack_nodes()
        if ours is None:
            return ["⚠️ 无法加载本包 nodes.py（非致命，仅跳过对拍）"]
        bad = 0
        for ln in (5, 22, 39, 120, 192):
            try:
                a, b = tuple(ours.temporal_shape(ln)), tuple(off.temporal_shape(ln))
                flag = "✅" if a == b else "❌"
                bad += a != b
                out.append(f"  temporal_shape({ln:<4}): {flag} 本包={a} 官方={b}")
            except Exception as e:
                out.append(f"  temporal_shape({ln:<4}): ❌ 异常 {type(e).__name__}: {e}")
        for wh in ((832, 480), (480, 832), (1280, 720)):
            try:
                a, b = tuple(ours.adapt_canvas(*wh)), tuple(off.adapt_canvas(*wh))
                flag = "✅" if a == b else "❌"
                bad += a != b
                out.append(f"  adapt_canvas{str(wh):<16}: {flag} 本包={a} 官方={b}")
            except Exception as e:
                out.append(f"  adapt_canvas{wh}: ❌ 异常 {type(e).__name__}: {e}")
        out.append("  " + ("❌ 存在不一致 → 这就是报错根因方向（请把本报告发回）" if bad
                           else "✅ 全部一致 → 本包内置副本与官方同步，漂移不是本次失败原因"))
    except Exception as e:
        out.append(f"❌ 对拍失败（{type(e).__name__}: {e}）"
                   f"；若为 ModuleNotFoundError，说明该 ComfyUI 不含官方 MiniMax H3 支持")
    return out


def _import_upscaler_execute():
    """放大网络的 execute（内置 2D / 3D 副本优先；用户另装的上游节点只是兜底）。"""
    # 1) 本插件内置副本（二采实际用的就是这两份，装不装上游节点包都一样）
    try:
        from .nodes_asset_manager import _import_upscaler_3d
        return _import_upscaler_3d().execute
    except Exception:
        pass
    # 2) 兜底：用户自装的上游节点
    try:
        from importlib import import_module
        for name in ("Comfyui_Minimax_h3_latent_Upscaler.nodes.minimax_h3_latent_upscaler_3d",
                     "ComfyUI_Minimax_h3_latent_Upscaler.nodes.minimax_h3_latent_upscaler_3d",
                     "minimax_h3_latent_upscaler_3d"):
            try:
                m = import_module(name)
                if hasattr(m, "MinimaxH3LatentUpscaler3D"):
                    return m.MinimaxH3LatentUpscaler3D.execute
            except Exception:
                continue
    except Exception:
        pass
    return None


def _pack_files():
    """本包节点文件的修改时间与大小（确认用户在跑哪一版、有没有装混）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    items = []
    for fn in ("nodes_asset_manager.py", "nodes.py", "nodes_llama.py", "__init__.py",
               "llama_backend.py", "jzl_env_check.py"):
        p = os.path.join(here, fn)
        try:
            st = os.stat(p)
            items.append(f"{fn}={time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}/{st.st_size}B")
        except Exception:
            items.append(f"{fn}=缺失")
    return " | ".join(items)


def build_report(extra_lines=(), out_dir=None):
    """生成完整报告文本；(文本, 落盘路径)。extra_lines 用于附加节点运行时的上下文。"""
    work = tempfile.mkdtemp(prefix="_jzl_diag_")
    lines = []
    add = lines.append

    add("=" * 78)
    add("JZL MiniMax 落盘环境自检报告")
    add("=" * 78)
    add(f"时间            : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    add(f"平台            : {platform.platform()}")
    add(f"架构/位数        : {platform.machine()} / {platform.architecture()[0]}")
    add(f"Python          : {sys.version.split()[0]}  ({sys.executable})")
    add(f"工作目录         : {os.getcwd()}")
    add(f"插件目录         : {os.path.dirname(os.path.abspath(__file__))}")
    add(f"说明            : {FALLBACK_NOTE}")
    if extra_lines:
        add("")
        add("── 运行上下文 ──")
        for ln in extra_lines:
            add(str(ln))

    add("")
    add("── ① ffmpeg 可执行文件 ──")
    ff, why = find_ffmpeg()
    add(f"解析结论         : {ff or '❌ 未找到'}")
    add(f"解析过程         : {why}")
    if ff:
        info = ffmpeg_probe(ff)
        add(f"版本             : {info.get('version')}")
        for name, ok in (info.get("encoders") or {}).items():
            add(f"编码器 {name:<8}: {'✅ 有' if ok else '❌ 无'}")
        add(f"aac 音频编码器    : {'✅ 有' if info.get('has_aac') else '❌ 无'}")
        smoke = smoke_test_ffmpeg(ff, work)
        add(f"实机编码下载测试   : {smoke}")

    add("")
    add("── ② PyAV(av) 回退编码器 ──")
    pav = pyav_probe()
    if pav.get("installed"):
        add(f"版本             : {pav.get('version')}  (codec 数量 {pav.get('_codec_count')})")
        add(f"libx264         : {'✅ 有' if pav.get('libx264') else '❌ 无'}")
        add(f"aac             : {'✅ 有' if pav.get('aac') else '❌ 无'}")
    else:
        add(f"❌ 不可用         : {pav.get('error')}")
    add(f"实机编码测试       : {smoke_test_pyav(work)}")

    add("")
    add("── ③ 输出目录（落盘目标） ──")
    out_root = out_dir or comfy_output_dir()
    add(f"output 根目录     : {out_root}")
    add(f"检测结果         : {dir_probe(out_root)}")
    jzl = os.path.join(out_root, "jzl")
    add(f"jzl 子目录        : {dir_probe(jzl)}")
    add(f"临时目录          : {dir_probe(tempfile.gettempdir())}")

    add("")
    add("── ④ 运行环境（torch / 显卡） ──")
    try:
        import torch
        add(f"torch           : {torch.__version__}  hip={getattr(torch.version, 'hip', None)} "
            f"cuda={getattr(torch.version, 'cuda', None)}")
        add(f"cuda 可用        : {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            add(f"显卡             : {torch.cuda.get_device_name(0)} "
                f"cap={torch.cuda.get_device_capability(0)} 显存 "
                f"{torch.cuda.get_device_properties(0).total_memory / (1024 ** 3):.1f} GB")
    except Exception as e:
        add(f"torch 不可用      : {type(e).__name__}: {e}")

    add("")
    add("── ⑤ 相关包 ──")
    for name in ("imageio-ffmpeg", "av", "numpy", "Pillow"):
        try:
            from importlib.metadata import version as _v
            add(f"{name:<16}: {_v(name)}")
        except Exception as e:
            add(f"{name:<16}: ❌ 未安装/不可探测（{type(e).__name__}）")

    add("")
    add("── ⑥ 版本与依赖 API 体检（排查「别人机器才报错」的关键）──")
    add(f"ComfyUI 根目录    : {comfy_root() or '❌ 未找到（若本报告里 comfy.* 全是不可探测，请用 ComfyUI 的 python 运行本脚本）'}")
    add(f"ComfyUI 版本     : {_comfyui_version()}")
    add(f"custom_nodes 数量: {_list_custom_nodes()}")
    for ln in _api_probe():
        add(ln)
    add(f"本包节点文件      : {_pack_files()}")
    add("")
    add("── ⑦ 官方 helper 与「本包内置副本」对拍（版本漂移看门狗）──")
    for ln in _helper_drift_probe():
        add(ln)
    add("")
    add("── ⑧ 结论 ──")
    if ff:
        smoke_ok = any(v.get("ok") for v in (smoke_test_ffmpeg(ff, work) or {}).values() if isinstance(v, dict))
        add("✅ 有 ffmpeg" + ("且实测可编码 → 落盘失败不是 ffmpeg 缺失导致" if smoke_ok
                            else "但实测编码失败 → 看上面编码器/错误"))
    elif pav.get("installed") and pav.get("libx264"):
        add("⚠️ 没有 ffmpeg，但 PyAV 可用 → 新版节点会自动回退 PyAV 落盘；"
            "若仍失败请把本报告 + 完整控制台日志发回")
    else:
        add("❌ 既没有 ffmpeg、PyAV 也不可用 → 这就是落盘失败的直接原因。"
            "修复：在 ComfyUI 环境执行 pip install imageio-ffmpeg（或安装 ffmpeg 并加入 PATH）")

    add("=" * 78)
    text = "\n".join(lines)

    # 报告落盘（放用户 output 目录，方便直接找到并发回）
    try:
        base = os.path.join(out_root, "_诊断")
        os.makedirs(base, exist_ok=True)
        fp = os.path.join(base, f"环境自检_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        fp = os.path.join(tempfile.gettempdir(), f"jzl_env_{int(time.time())}.txt")
        try:
            with open(fp, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            fp = "(无法写入报告文件)"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return text, fp


def _safe_stdout():
    """Windows 控制台默认 GBK，报告里的 ✅/❌ 会直接抛 UnicodeEncodeError → 统一改成 utf-8 + 容错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None):
    _safe_stdout()
    text, fp = build_report()
    print(text)
    print(f"\n[报告已保存] {fp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
