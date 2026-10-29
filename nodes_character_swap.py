"""JZL MiniMax H3 — 🎭 人物替换（长视频逐段 vid2vid 换人）

把一段**已有视频**里的演员换成参考图里的演员：源片按 17k+5 切成段，每段以
「源片同名段（含衔接跑道）」当 `<Video 1>` 参考视频、定妆图当 `<Picture 1>` 走 ref2va 重新生成；
第 2 段起把上一段**生成结果**的一采 latent 尾窗钉成本段首帧 keyframe（永不降噪、每步重注入），
段首「跑道 + 过渡」帧解码后按**像素域**裁掉 ⇒ 成片时长 = 源片时长、成片段界无缝。

为什么换人的接缝比「无限时长」更容易做（也是本节点与导演台的关键差异）：
  · 无限时长没有“真值”，段 2+ 只能靠上段尾帧 + 新提示词往下编 → 画面/色彩/节奏会缓慢漂移；
  · 换人有**逐帧真值**：每段都重新对位同一段源片（`ref_videos`），漂移在段内就被真值钳住，
    不存在跨段累计漂移。keyframe 钉帧只负责“画面不跳”，**对位**由源片参考负责。
  · 因此这里的重叠不是“靠重叠区猜”，而是**源片切片重叠**：跑道对应的源片帧 = 上一段已经产出的
    那一段内容，天然就是同一个时间轴，不会累积误差。

音视频事实（沿用导演台已验证的结论，改错会直接重影/闪烁）：
  · 段长恒为 17k+5；窗口恒为 17m+5 ⇒ 段长−窗口 = 17(k−m)，尾窗起点必落在 token 边界（相位自动对齐）；
  · 丢弃量（窗口 + 过渡）必须是 17 的倍数 ⇒ 过渡只能取 12/29/46（`_seam_snap_settle` 自动对齐）；
  · 裁切必须在**像素域**做：latent 域裁切会让 VAE 解码的「5 token = 17 帧」分组相位错位 → 周期性重影。
"""

import json
import math
import os

import node_helpers
import torch

import folder_paths
from comfy_api.latest import io

# 复用导演台（nodes_asset_manager.py）里已经踩过坑的全部实现，绝不另写一套
from .nodes_asset_manager import (
    SAMPLE_MODE_NORMAL,
    SAMPLE_MODE_SELFLIFT,
    SEGMENT_DURATION_CHOICES,
    SEAM_DEFAULT_SETTLE,
    SEAM_DEFAULT_WINDOW,
    VIDEO_FPS,
    _clean_cache,
    _decode_av,
    _encode_ref_to_video,
    _fmt_call_info,
    _import_upscaler_3d,
    _log_once,
    _maybe_wrap_tiling,
    _merge_mp4_concat,
    _next_batch,
    _next_counter,
    _parse_duration_spec,
    _resolve_gen_size,
    _resolve_length,
    _resolve_seed,
    _resolve_upscaler_model,
    _safe_story_name,
    _sample_av,
    _sample_av_custom_sigmas,
    _sample_first_pass,
    _save_segment_mp4,
    _seam_apply_guide,
    _seam_fit_guides_to_canvas,
    _seam_frame_luma,
    _seam_luma_ramp,
    _seam_plan,
    _seam_resolve_window,
    _seam_snap_settle,
    _seam_trim_decoded,
)
from .nodes import _resize, align_frame_count

TAG = "人物替换"

# 固定段长选项（区间值对换人没有意义：段长决定源片切点，必须确定）
_SEGMENT_SECONDS_OPTIONS = tuple(x for x in SEGMENT_DURATION_CHOICES if x.startswith("固定"))

AUDIO_MODES = ("保留原声（推荐）", "使用生成音轨", "静音")

DEFAULT_PROMPT = (
    "把 <Video 1> 里的人物替换为 <Picture 1>。\n"
    "严格保持 <Video 1> 的动作、表情、机位、场景、道具、光线与色彩不变。"
)


# ═══════════════════════════════════════════════════════════════
#  小工具（只做输入整形，不含任何生成逻辑）
# ═══════════════════════════════════════════════════════════════

def _as_4d(x):
    """IMAGE → [B,H,W,C]（ComfyUI 的 IMAGE 一般是 4 维，个别节点会给 3 维）。"""
    if x is None:
        return None
    if not hasattr(x, "shape"):
        return None
    if len(x.shape) == 3:
        return x.unsqueeze(0)
    return x


def _canvas_for_ratio(src_w, src_h, megapixels):
    """跟随源片宽高比 → 生成画布（MP 预算，32 对齐）。与官方 ResolutionSelector 同一套数学。"""
    ratio = max(1e-6, float(src_w) / float(max(1, src_h)))
    total = max(0.05, float(megapixels or 0.4)) * 1024 * 1024
    w = math.sqrt(total * ratio)
    h = math.sqrt(total / ratio)
    return (max(32, int(round(w / 32.0)) * 32), max(32, int(round(h / 32.0)) * 32))


def _resize_chunked(frames, width, height, chunk=24):
    """整段源片缩放到生成画布（分批做，避免一次性对几百帧做 lanczos 时显存/内存峰值翻倍）。"""
    frames = _as_4d(frames)
    total = int(frames.shape[0])
    if total <= chunk:
        return _resize(frames, width, height, "disabled")
    outs = []
    for s in range(0, total, chunk):
        outs.append(_resize(frames[s:s + chunk], width, height, "disabled"))
    return torch.cat(outs, dim=0)


def _slice_frames(frames, start, count):
    """按帧号切源片；越界时用末帧补齐（保证每段参考视频帧数精确 = 生成帧数）。"""
    total = int(frames.shape[0])
    start = int(start)
    count = int(count)
    src_start = max(0, min(start, total))
    piece = frames[src_start:src_start + count]
    lack = count - int(piece.shape[0])
    if lack > 0:
        pad = frames[total - 1:total].expand(lack, *frames.shape[1:])
        piece = torch.cat([piece, pad], dim=0)
    return piece


def _slice_audio(audio, start_frames, keep_frames, fps=VIDEO_FPS):
    """按帧号切音频（与视频同一条 24fps 时间轴）。越界自动钳制，剩下的由落盘端补静音。"""
    if not isinstance(audio, dict):
        return None
    wf = audio.get("waveform")
    if wf is None:
        return None
    sr = int(audio.get("sample_rate", 32000) or 32000)
    total = int(wf.shape[-1])
    if total <= 0:
        return None
    a = int(round(max(0, int(start_frames)) / float(fps) * sr))
    b = a + int(round(max(1, int(keep_frames)) / float(fps) * sr))
    a = max(0, min(a, total - 1))
    b = max(a + 1, min(b, total))
    out = dict(audio)
    out["waveform"] = wf[..., a:b].contiguous()
    return out


def _fill_prompt(prompt, index, count, start_frame, keep_frames):
    """提示词占位符：{seg} / {total} / {start} / {frames}（不用 str.format，避免提示词里的花括号报错）。"""
    txt = (prompt or "").strip() or DEFAULT_PROMPT
    return (txt.replace("{seg}", str(int(index) + 1))
               .replace("{total}", str(int(count)))
               .replace("{start}", str(int(start_frame)))
               .replace("{frames}", str(int(keep_frames))))


def _store_pixels(image, width, height, count):
    """把一段解码结果的尾部 count 帧存成 uint8 像素（供下一段钉帧 / 参考视频拼接）。

    存像素而不是 latent：钉帧每次采样都要按本次画布重新 vae.encode，
    latent 跨分辨率只能插值，会让 keyframe 变糊并传染整段。
    uint8 存 22 帧 @1280×704 约 59MB，只保留一份（每段覆盖）。
    """
    if image is None or not hasattr(image, "shape") or int(image.shape[0]) < 1:
        return None
    total = int(image.shape[0])
    n = max(1, min(int(count), total))
    tail = image[total - n:]
    tail = _resize(tail, int(width), int(height), "disabled")
    return (tail.detach().clamp(0.0, 1.0) * 255.0).to(torch.uint8).cpu().contiguous()


def _pixels_to_frames(px, width, height):
    """uint8 像素尾帧 → 指定画布的 float 帧（供 vae.encode / 参考视频用）。"""
    if px is None:
        return None
    frames = px.to(dtype=torch.float32).div(255.0)
    if int(frames.shape[1]) != int(height) or int(frames.shape[2]) != int(width):
        frames = _resize(frames, int(width), int(height), "disabled")
    return frames.contiguous()


def _apply_pixel_guide(positive, prev_pixels, vae, width, height, window):
    """把上一段解码像素的最后 window 帧按本采画布 vae.encode 成首帧 keyframe。

    像素先缩放到本采分辨率再编码，所以 keyframe 是清晰的；
    若直接拿一采的 latent 尾部跨分辨率使用，只能插值 ⇒ 糊。
    """
    if prev_pixels is None or vae is None:
        return positive
    frames = _pixels_to_frames(prev_pixels, width, height)
    n = int(frames.shape[0])
    if n >= 5:
        while n % 17 != 5:      # 官方同款：多帧裁到 17k+5
            n -= 1
        frames = frames[:n]
    else:
        frames = frames[:1]
    keyframe = {"resolved_frame_index": 0, "latent": vae.encode(frames)}
    keyframes = list((positive[0][1] or {}).get("minimax_keyframes") or [])
    keyframes.append(keyframe)
    return node_helpers.conditioning_set_values(positive, {"minimax_keyframes": keyframes})


def _second_canvas(gp, width, height):
    """二采目标画布：目标分辨率模式固定 W×H（32 对齐）；倍数模式 = 一采画布 × 倍数。"""
    if str(gp.get("second_mode") or "目标分辨率") == "目标分辨率":
        tw = max(32, int(round(float(gp["second_width"]) / 32.0)) * 32)
        th = max(32, int(round(float(gp["second_height"]) / 32.0)) * 32)
        return tw, th
    s = float(gp.get("upscale_scale") or 1.0)
    tw = max(32, int(round(width * s / 32.0)) * 32)
    th = max(32, int(round(height * s / 32.0)) * 32)
    return tw, th


def _run_second_pass(model, positive, samples, sample_decode, second_cfg, gp, width, height, seed, tag=TAG):
    """二采：latent 放大（目标分辨率 / 倍数）→ 合并音视频 → 再采样。

    与导演台 `_run_second_sampling` 同源，额外支持「目标分辨率」模式。
    """
    try:
        from comfy_extras.nodes_lt import LTXVSeparateAVLatent, LTXVConcatAVLatent
    except Exception as e:
        raise RuntimeError(f"缺少官方节点 LTXVSeparateAVLatent/LTXVConcatAVLatent（comfy-core 自带）：{e}")

    up_cls = _import_upscaler_3d()
    sep = LTXVSeparateAVLatent.execute({"samples": samples})
    video_latent, audio_latent = sep.args[0], sep.args[1]

    if str(gp.get("second_mode")) == "目标分辨率":
        tw, th = _second_canvas(gp, width, height)
        mode_val = {"mode": "target dimensions", "width": int(tw), "height": int(th)}
    else:
        mode_val = {"mode": "scale by multiplier", "scale": float(gp.get("upscale_scale") or 1.0)}

    try:
        import inspect as _inspect
        params = set(_inspect.signature(up_cls.execute).parameters.keys())
    except Exception:
        params = set()
    kwargs = {
        "latent": video_latent,
        "model_name": _resolve_upscaler_model(second_cfg.get("precision") or "fp32",
                                              second_cfg.get("upscaler_model") or ""),
        "mode": mode_val,
        "align": 32,
        "device": second_cfg.get("device") or "cuda",
        "precision": second_cfg.get("precision") or "fp32",
    }
    chunk = bool(second_cfg.get("enable_chunking", True))
    if "enable_chunking" in params:
        kwargs["enable_chunking"] = chunk
    if "enable_temporal_chunking" in params:
        kwargs["enable_temporal_chunking"] = chunk
    if "force_unload" in params:
        kwargs["force_unload"] = False
    up = up_cls.execute(**kwargs)
    up_latent = up.args[0]

    # 引导 latent（latent 模式才会需要插值对齐；像素模式已按目标画布重编码，这里是空操作）
    positive = _seam_fit_guides_to_canvas(positive, up_latent, tag=tag)
    merged = LTXVConcatAVLatent.execute(up_latent, audio_latent).args[0]

    cfg = float(sample_decode.get("cfg", 1.0) or 1.0)
    model2 = _maybe_wrap_tiling(model, second_cfg, up_latent, audio_latent, tag=tag)
    if str(second_cfg.get("sigmas_mode") or "scheduler") == "custom":
        return _sample_av_custom_sigmas(model2, positive, merged,
                                        second_cfg.get("custom_sigmas") or "0.8500, 0.6316, 0.3158, 0.0000",
                                        second_cfg.get("sampler") or "euler", seed, cfg)
    sd2 = dict(sample_decode or {})
    sd2["sampler"] = second_cfg.get("sampler") or "euler"
    sd2["scheduler"] = second_cfg.get("scheduler") or "simple"
    sd2["steps"] = int(second_cfg.get("steps", 3) or 3)
    sd2["denoise"] = float(second_cfg.get("denoise", 0.3) or 0.3)
    return _sample_av(model2, positive, merged, sd2, seed)


def _plan_source_segments(total_frames, seg_len, runway, max_segments):
    """切段计划：每段「生成帧数 / 源片起点 / 成片保留帧数」。

    · i=0：没有前文可承接 → 生成帧数 = 段长（不留跑道）；
    · i>0：生成帧数 = 生成主体 + 跑道，源片起点 = 段起点 − 跑道
      （跑道这段源片正好是上一段已产出的内容 → 与上一段同源，不会累积漂移）；
    · 末段不足一段：按剩余帧数生成（吸附到 17k+5），别为一两帧白跑一整段；
      但若剩余太少装不下跑道，则抬到「跑道 + 17 帧」的下限，保证钉帧窗口放得下。
    """
    seg_len = int(seg_len)
    count = max(1, int(math.ceil(total_frames / float(seg_len))))
    if count > int(max_segments):
        raise ValueError(
            f"源片 {total_frames} 帧 ÷ 每段 {seg_len} 帧 = {count} 段，超过「最大段数」{int(max_segments)}。"
            f"请把「每段时长」调大（或分段处理源片）后重试。")
    plan = []
    for i in range(count):
        start = i * seg_len
        keep = min(seg_len, total_frames - start)
        body = seg_len if keep >= seg_len else align_frame_count(max(5, int(keep)))
        if i > 0 and int(runway) > 0:
            body = align_frame_count(max(body, int(runway) + 17))
            gen = body + int(runway)
            src_start = start - int(runway)
        else:
            gen = body
            src_start = start
        plan.append({"index": i, "start": int(start), "keep": int(keep),
                     "gen": int(gen), "src_start": int(src_start)})
    return plan


# ═══════════════════════════════════════════════════════════════
#  节点配置（manager_settings JSON）——所有可调项都收进面板，节点表面只留必需口
# ═══════════════════════════════════════════════════════════════

SEGMENT_SECONDS_OPTIONS = list(_SEGMENT_SECONDS_OPTIONS)

_SWAP_DEFAULTS = {
    "gen_params": {
        "segment": "固定5秒",
        "aspect_ratio": "跟随源片（推荐）",
        "megapixels": 1.0,               # 默认 1MP（≈1344×768）；低于 0.6MP 人脸细节会明显变糊
        # 参考图只用「参考图放大」这一个旋钮（= 「JZL - 🎬 MiniMax H3 参考编码」的 ref_scale，
        # 面积倍率：参考图最终像素面积 = 生成画布面积 × 倍率）。不另设 max/短边上限那套。
        "scale_factor": 1.0,
        "upscale_scale": 1.0,            # 二采（放大倍数模式）
        "second_mode": "目标分辨率",      # 目标分辨率（默认 1280×704）| 放大倍数
        "second_width": 1280,
        "second_height": 704,
        "audio_mode": "保留原声（推荐）",
        "max_segments": 60,
    },
    "sample_decode": {
        "sample_mode": "常规采样",
        "sampler": "res_multistep", "scheduler": "simple",
        "steps": 5, "cfg": 1.0, "denoise": 1.0,
        "seed": 0, "seed_mode": "fixed",
        "decode_cleanup": "不做清理",
        "selflift": {"steps": 8, "transition_step": 6, "lowres_scale": 0.5, "rho": 0.6,
                     "w_min": 0.5, "w_max": 1.0, "upscaler_model": "none",
                     "upscaler_unload": True, "highres_tiling": False},
        # 段间衔接（与「无限时长」同一套语义：窗口/过渡/钉帧）
        "seam": {"enabled": True, "window": 22, "settle": 12, "ref_video_tail": 0,
                 # 引导来源：pixels = 上段解码像素按本采画布重编码（清晰）
                 #           latent = 上段一采 latent 尾部（旧实现，二采会糊）
                 "guide_mode": "pixels",
                 # ⭐ 一采与二采各自钉一次帧（二采用二采画布重编码，避免插值发糊）
                 "pin_second": True,
                 # ⭐ 把上一段画面也拼进参考视频
                 "append_prev_video": False},
        # 二次采样（换人脸是小目标，默认开）
        "second": {"enabled": True, "sampler": "euler", "scheduler": "simple",
                   "sigmas_mode": "scheduler",
                   "custom_sigmas": "0.8500, 0.6316, 0.3158, 0.0000",
                   "steps": 3, "denoise": 0.30, "precision": "fp32",
                   "upscaler_model": "", "device": "cuda", "enable_chunking": True,
                   "tiling": "关闭", "tiling_tiles": 2, "tiling_overlap": 128},
    },
    "save": {"mode": "拼接保存", "auto_merge_delete": False},
}


def _cfg_num(d, key, default, lo=None, hi=None):
    try:
        v = float((d or {}).get(key, default))
    except Exception:
        v = float(default)
    if lo is not None:
        v = max(float(lo), v)
    if hi is not None:
        v = min(float(hi), v)
    return v


def _cfg_bool(d, key, default):
    v = (d or {}).get(key, None)
    return bool(default) if v is None else bool(v)


def _cfg_opt(d, key, default, options):
    v = str((d or {}).get(key, default) or default)
    return v if v in options else default


def _parse_swap_settings(raw):
    """manager_settings JSON → 规范化配置（缺键/脏值一律安全回退默认值）。

    与前端 `defaultSettings()` 里的 `gen_params / sample_decode / save` 同一条路径，
    面板写什么、后端就读什么（不做任何键名翻译，避免「面板显示 8 步、实际跑 5 步」）。
    """
    cfg = json.loads(json.dumps(_SWAP_DEFAULTS))   # 深拷贝默认
    if isinstance(raw, str) and raw.strip():
        try:
            got = json.loads(raw)
        except Exception:
            print(f"[JZL-{TAG}] ⚠️ 节点配置 JSON 解析失败 → 本次全部使用默认配置")
            got = None
        if isinstance(got, dict):
            for grp in ("gen_params", "sample_decode", "save"):
                src = got.get(grp)
                if isinstance(src, dict):
                    for k, v in src.items():
                        if isinstance(v, dict) and isinstance(cfg[grp].get(k), dict):
                            cfg[grp][k].update({kk: vv for kk, vv in v.items() if vv is not None})
                        elif v is not None:
                            cfg[grp][k] = v
    gp, sd, sv = cfg["gen_params"], cfg["sample_decode"], cfg["save"]
    cfg["gen_params"] = {
        "segment": _cfg_opt(gp, "segment", "固定5秒", SEGMENT_SECONDS_OPTIONS),
        "aspect_ratio": str(gp.get("aspect_ratio") or "跟随源片（推荐）"),
        "megapixels": _cfg_num(gp, "megapixels", 1.0, 0.1, 4.0),
        "scale_factor": _cfg_num(gp, "scale_factor", 1.0, 1.0, 5.0),
        "upscale_scale": _cfg_num(gp, "upscale_scale", 1.0, 1.0, 4.0),
        "second_mode": _cfg_opt(gp, "second_mode", "目标分辨率", ("目标分辨率", "放大倍数")),
        "second_width": int(_cfg_num(gp, "second_width", 1280, 32, 4096)),
        "second_height": int(_cfg_num(gp, "second_height", 704, 32, 4096)),
        "audio_mode": _cfg_opt(gp, "audio_mode", AUDIO_MODES[0], AUDIO_MODES),
        "max_segments": int(_cfg_num(gp, "max_segments", 60, 1, 200)),
    }
    seam = sd.get("seam") or {}
    second = sd.get("second") or {}
    selflift = sd.get("selflift") or {}
    # ⚠️ 采样解码这一段与「短剧导演台」100% 同构：面板渲染的是同一套键，
    #    后端只做「钳制 + 兜底」，不再另造键名，否则会出现「面板显示 SelfLift、实际按常规跑」。
    cfg["sample_decode"] = {
        # 采样分类：常规采样（K采样器 + 可选二采）/ SelfLift采样（内置 SelfLift 渐进分辨率）
        "sample_mode": _cfg_opt(sd, "sample_mode", SAMPLE_MODE_NORMAL,
                                (SAMPLE_MODE_NORMAL, SAMPLE_MODE_SELFLIFT)),
        "sampler": str(sd.get("sampler") or "res_multistep"),
        "scheduler": str(sd.get("scheduler") or "simple"),
        "steps": int(_cfg_num(sd, "steps", 5, 1, 200)),
        "cfg": _cfg_num(sd, "cfg", 1.0, 0.0, 100.0),
        "denoise": _cfg_num(sd, "denoise", 1.0, 0.0, 1.0),
        "seed": sd.get("seed", 0),
        "seed_mode": str(sd.get("seed_mode") or "fixed"),
        "decode_cleanup": str(sd.get("decode_cleanup") or "不做清理"),
        "selflift": {
            "steps": int(_cfg_num(selflift, "steps", 8, 2, 200)),
            "transition_step": int(_cfg_num(selflift, "transition_step", 6, 1, 200)),
            "lowres_scale": _cfg_num(selflift, "lowres_scale", 0.5, 0.25, 1.0),
            "rho": _cfg_num(selflift, "rho", 0.6, 0.0, 1.0),
            "w_min": _cfg_num(selflift, "w_min", 0.5, 0.0, 1.0),
            "w_max": _cfg_num(selflift, "w_max", 1.0, 0.0, 1.0),
            "upscaler_model": str(selflift.get("upscaler_model") or "none"),
            "upscaler_unload": _cfg_bool(selflift, "upscaler_unload", True),
            "highres_tiling": _cfg_bool(selflift, "highres_tiling", False),
        },
        "seam": {
            # 缺失/None → 默认开；只有显式 false 才关（与 _seam_flag 同一套语义）
            "enabled": _cfg_bool(seam, "enabled", True),
            "window": int(_seam_resolve_window(_cfg_num(seam, "window", SEAM_DEFAULT_WINDOW))),
            "settle": int(_cfg_num(seam, "settle", SEAM_DEFAULT_SETTLE, 0, 46)),
            "ref_video_tail": int(_cfg_num(seam, "ref_video_tail", 0, 0, 600)),
            # 引导来源：pixels（按本采画布重编码）/ latent（旧实现）
            "guide_mode": ("latent" if "latent" in str(seam.get("guide_mode") or "").lower()
                           or "latent" in str(seam.get("guide_mode") or "") else "pixels"),
            "pin_second": _cfg_bool(seam, "pin_second", True),
            "append_prev_video": _cfg_bool(seam, "append_prev_video", False),
        },
        "second": {
            "enabled": _cfg_bool(second, "enabled", True),
            "sampler": str(second.get("sampler") or "euler"),
            "scheduler": str(second.get("scheduler") or "simple"),
            "sigmas_mode": str(second.get("sigmas_mode") or "scheduler"),
            "custom_sigmas": str(second.get("custom_sigmas") or "0.8500, 0.6316, 0.3158, 0.0000"),
            "steps": int(_cfg_num(second, "steps", 3, 1, 200)),
            "denoise": _cfg_num(second, "denoise", 0.30, 0.0, 1.0),
            "precision": str(second.get("precision") or "fp32"),
            "upscaler_model": str(second.get("upscaler_model") or ""),
            "device": str(second.get("device") or "cuda"),
            "enable_chunking": _cfg_bool(second, "enable_chunking", True),
            "tiling": str(second.get("tiling") or "关闭"),
            "tiling_tiles": int(_cfg_num(second, "tiling_tiles", 2, 2, 8)),
            "tiling_overlap": int(_cfg_num(second, "tiling_overlap", 128, 0, 512)),
        },
    }
    cfg["save"] = {
        "mode": _cfg_opt(sv, "mode", "拼接保存", ("拼接保存", "分段保存")),
        "auto_merge_delete": _cfg_bool(sv, "auto_merge_delete", False),
    }
    return cfg


# ═══════════════════════════════════════════════════════════════
#  节点
# ═══════════════════════════════════════════════════════════════

class JZL_MiniMaxCharacterSwap(io.ComfyNode):
    """长视频人物替换：源片逐段 ref2va 重生成 + 段间钉帧衔接 + 跑道裁切 + 原声回流。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_MiniMaxCharacterSwap",
            display_name="JZL - 🎭 MiniMax-H3人物替换",
            category="JZL/MiniMax",
            description=("把源视频里的人换成参考图里的人（长视频自动分段）：每段拿「源片同名段」当 "
                         "<Video 1> 参考、定妆图当 <Picture 1>，第 2 段起用上一段生成结果的尾窗钉成本段首帧，"
                         "段首跑道+过渡帧自动裁掉 ⇒ 成片时长 = 源片时长、段界无缝。默认保留原声。"),
            inputs=[
                io.Model.Input("model", display_name="主模型"),
                io.Clip.Input("clip", display_name="CLIP"),
                io.Vae.Input("vae", display_name="视觉VAE"),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True),
                io.Image.Input("source_video",
                    tooltip="源视频帧（IMAGE 批次，按 24fps 解释）。用「加载视频 / GetVideoComponents」接入；"
                            "源片不是 24fps 请先转码，否则时长会对不上"),
                io.Audio.Input("source_audio", optional=True,
                    tooltip="源视频原声（与源视频同一条时间轴）。「保留原声」时用它的对应片段落盘，"
                            "同时作为 <Video 1> 的音轨参考（音色/节奏线索）"),
                io.Autogrow.Input("ref_images", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        io.Image.Input("ref_image", tooltip="新角色定妆图（<Picture N>）。建议正面清晰、单人、同画幅"),
                        prefix="ref_image_", min=0, max=9)),
                io.String.Input("story_name", default="人物替换", display_name="输出名称",
                    tooltip="落盘目录 output/jzl/{名称}/（在「🎬 生成视频管理」面板里修改）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True,
                    socketless=True, default="",
                    tooltip="本节点完整配置 JSON（由 🎨 视频参数设置 / 🎭 采样解码设置 / 🎬 生成视频管理 写入），随工作流保存"),
                # 提示词固定放在最底部（节点表面从上到下：模型类端口 → 素材端口 → 按钮区 → 提示词）
                io.String.Input("prompt", multiline=True, default=DEFAULT_PROMPT,
                    tooltip="换人提示词。可用占位符：{seg}=本段序号、{total}=总段数、{start}=本段在源片里的起始帧、"
                            "{frames}=本段帧数。参考图按顺序是 <Picture 1..9>，源片固定是 <Video 1>"),
            ],
            outputs=[
                io.String.Output("merged_video", display_name="成片路径"),
                io.String.Output("segment_files", display_name="分段视频", is_output_list=True),
            ],
            is_output_node=True,
        )

    @classmethod
    def fingerprint_inputs(cls, manager_settings="", prompt="", story_name="", **kwargs):
        """配置变了就必须重跑（所有可调项都在 manager_settings 里；种子随机时每轮都算新输入）。"""
        try:
            cfg = _parse_swap_settings(manager_settings)
            if str(cfg["sample_decode"]["seed_mode"]) == "randomize":
                import time as _t
                return f"random@{_t.time_ns()}"
            return json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        except Exception:
            return "no-settings"

    @classmethod
    def execute(cls, source_video, prompt, story_name="人物替换",
                manager_settings="", ref_images=None, source_audio=None,
                model=None, clip=None, vae=None, audio_vae=None) -> io.NodeOutput:

        # ── 所有可调项都来自「🎨 视频参数设置 / 🎭 采样解码设置 / 🎬 生成视频管理」三个面板 ──
        cfg = _parse_swap_settings(manager_settings)
        gp, sd, sv = cfg["gen_params"], cfg["sample_decode"], cfg["save"]
        seam_cfg, second_cfg = sd["seam"], sd["second"]
        segment_seconds = gp["segment"]
        aspect_ratio = gp["aspect_ratio"]
        megapixels = float(gp["megapixels"])
        ref_scale = float(gp["scale_factor"])
        upscale_scale = float(gp["upscale_scale"])
        audio_mode = gp["audio_mode"]
        max_segments = int(gp["max_segments"])
        seam_enabled = bool(seam_cfg["enabled"])
        window = int(seam_cfg["window"])
        ref_video_tail = int(seam_cfg["ref_video_tail"])
        guide_mode = seam_cfg["guide_mode"]
        pin_second = bool(seam_cfg["pin_second"])
        append_prev = bool(seam_cfg["append_prev_video"])
        save_mode = sv["mode"]
        auto_merge_delete = bool(sv["auto_merge_delete"])
        seed, seed_mode = sd["seed"], sd["seed_mode"]

        frames_all = _as_4d(source_video)
        if frames_all is None or int(frames_all.shape[0]) < 5:
            raise ValueError("「源视频」为空或不足 5 帧：请把视频（IMAGE 批次，24fps 解释）接进来")
        if model is None or clip is None or vae is None:
            raise ValueError("必须连接「主模型 + CLIP + 视觉VAE」才能生成")
        keep_original_audio = str(audio_mode).startswith("保留原声")
        mute = str(audio_mode).startswith("静音")
        if keep_original_audio and not isinstance(source_audio, dict):
            _log_once(f"[JZL-{TAG}] ⚠️ 选了「保留原声」但没连「源视频原声」→ 本次改用生成音轨")

        total_src = int(frames_all.shape[0])
        src_h, src_w = int(frames_all.shape[1]), int(frames_all.shape[2])

        # ① 画布
        if str(aspect_ratio).startswith("跟随源片"):
            width, height = _canvas_for_ratio(src_w, src_h, megapixels)
        else:
            width, height = _resolve_gen_size(aspect_ratio, megapixels)

        # ② 段长 / 窗口 / 过渡 / 跑道
        dur_lo, _dur_hi, _dur_desc = _parse_duration_spec(segment_seconds)
        seg_len = int(_resolve_length(dur_lo))
        settle = int(_seam_snap_settle(window, int(seam_cfg["settle"])))
        runway = int(window + settle) if seam_enabled else 0
        plan = _plan_source_segments(total_src, seg_len, runway, max_segments)
        count = len(plan)
        if count == 1:
            runway = 0   # 单段没有衔接，也就没有跑道

        # ③ 定妆图（参考图）只解析一次：每段都重新注入 → 抗长链漂移（导演台已验证）
        ref_imgs = []
        for k, img in enumerate((ref_images or {}).values()):
            if img is None:
                continue
            ref_imgs.append(_as_4d(img)[:1])
        if not ref_imgs:
            print(f"[JZL-{TAG}] ⚠️ 没有接任何定妆图「ref_image_N」：模型只能靠提示词猜身份，换人效果会很差")

        # ④ 二采目标画布（目标分辨率模式固定 W×H，默认 1280×704）
        use_second = (bool(second_cfg["enabled"])
                      and str(sd["sample_mode"]) != SAMPLE_MODE_SELFLIFT
                      and (str(gp["second_mode"]) == "目标分辨率" or float(upscale_scale) > 1.01))
        sec_w, sec_h = _second_canvas(gp, width, height)
        if use_second and int(gp["second_width"]) * int(gp["second_height"]) < width * height:
            print(f"[JZL-{TAG}] ⚠️ 二采目标 {sec_w}×{sec_h} 比一采画布 {width}×{height} 还小 → "
                  f"二采只会更糊，建议调大二采目标或降低百万像素")

        out_root = os.path.abspath(folder_paths.get_output_directory())
        safe_story = _safe_story_name(story_name or "人物替换")
        out_dir = os.path.join(out_root, "jzl", safe_story)
        os.makedirs(out_dir, exist_ok=True)
        batch = _next_batch(out_dir, safe_story)
        counter0 = _next_counter(out_dir, f"{safe_story}_{batch}次生成分段")

        print(f"[JZL-{TAG}] ─────────────────────────────────────────────")
        print(f"[JZL-{TAG}] 源片 {total_src} 帧（{total_src / float(VIDEO_FPS):.2f} 秒）｜画布 {width}x{height}"
              f"｜每段 {seg_len} 帧（{seg_len / float(VIDEO_FPS):.2f} 秒）｜段数 {count}")
        if runway > 0:
            print(f"[JZL-{TAG}] 段间衔接：窗口 {window} 帧 + 过渡 {settle} 帧 = 跑道 {runway} 帧"
                  f"（第 2 段起生成时长 = 段长 + 跑道，成片裁掉 → 落盘精确 = 段长）；"
                  f"源片同步前移 {runway} 帧当参考 → 与上一段已产出的内容同源，不会累积漂移")
        print(f"[JZL-{TAG}] 参考：{len(ref_imgs)} 张定妆图（参考图放大 {ref_scale:.1f}×，面积倍率）"
              f"｜音频方式：{audio_mode}"
              f"｜二采：{(str(sec_w) + '×' + str(sec_h)) if use_second else '关闭'}")
        print(f"[JZL-{TAG}] 衔接引导：{'像素尾帧重编码' if guide_mode == 'pixels' else '一采latent尾部（旧）'}"
              f"｜钉帧：{'一采+二采各一次' if pin_second else '仅一采'}"
              f"{'｜参考视频拼接上段输出' if append_prev else ''}")
        print(f"[JZL-{TAG}] 落盘目录：{out_dir}")

        ref_names = {
            "image": [f"定妆图{k + 1}" for k in range(len(ref_imgs))],
            "video": ["源片"],
            "audio": ["源片原声"],
        }
        # ⚠️ 直接透传整份 sample_decode（含 sample_mode / selflift / second）——
        #    与「短剧导演台」调同一套后端实现，SelfLift 采样与二采空间分块都能原样生效。
        sample_decode = dict(sd)
        sample_decode.pop("seam", None)
        second_run = dict(second_cfg)
        second_run["enabled"] = True
        if str(sd["sample_mode"]) == SAMPLE_MODE_SELFLIFT:
            print(f"[JZL-{TAG}] 采样分类 = SelfLift采样（步数 {sd['selflift']['steps']}，"
                  f"低分辨率步数 {sd['selflift']['transition_step']}）→ 二采不生效（SelfLift 自身即渐进分辨率）")

        saved_files, errors = [], []
        merge_wavs = [] if save_mode == "拼接保存" else None
        prev_latent, prev_luma = None, None
        # 上一段落盘后的尾部像素（uint8），供「像素钉帧」与「参考视频拼接上段输出」
        prev_pixels = None
        total_out = 0

        def _run_segment(seg):
            """单段：源片参考 → ref2va 编码 → 钉帧 → 采样 → 解码 → 裁跑道 → 落盘。"""
            nonlocal prev_latent, prev_luma, prev_pixels, total_out
            i = seg["index"]
            idx = i + 1
            start, kept_target = seg["start"], seg["keep"]
            gen_len, src_start = seg["gen"], seg["src_start"]
            try:
                _clean_cache()
            except Exception:
                pass

            seg_prompt = _fill_prompt(prompt, i, count, start, kept_target)

            # ① 源片参考片段：源片 [src_start, src_start+gen_len) → 生成画布尺寸
            #    只在需要时才缩放这一段（不整片缓存画布副本，长片内存立刻省一半以上）
            src_clip = _resize_chunked(_slice_frames(frames_all, src_start, gen_len), width, height)
            ref_tail = int(ref_video_tail or 0)
            if ref_tail >= 5:
                n = min(int(src_clip.shape[0]), align_frame_count(ref_tail))
                src_clip = src_clip[int(src_clip.shape[0]) - n:]
            # ①b 可选：把「上一段已产出的画面」也拼进参考视频
            #     官方编码器会把参考视频截断到 gen_len 帧，所以这里用「源片主体 + 上段尾窗」拼满
            if append_prev and i > 0 and prev_pixels is not None:
                _pv = _pixels_to_frames(prev_pixels, width, height)
                _pv = _pv[-min(int(_pv.shape[0]), max(5, int(window))):]
                keep_src = max(5, int(gen_len) - int(_pv.shape[0]))
                while keep_src % 17 != 5:
                    keep_src -= 1
                src_clip = torch.cat([src_clip[:keep_src], _pv.to(dtype=src_clip.dtype,
                                                                 device=src_clip.device)], dim=0)
            # 参考视频必须是 17k+5（官方编码器会向下吸附；这里先说清楚实际送进去多少帧）
            ref_video_frames = int(src_clip.shape[0])

            # ② 原声片段：既作 <Video 1> 的音轨参考（音色/节奏），也是「保留原声」的成片音轨
            ref_audio_clip = _slice_audio(source_audio, src_start, gen_len) if keep_original_audio else None
            ref_video_audios = [ref_audio_clip] if (ref_audio_clip is not None and audio_vae is not None) else []

            print(f"[JZL-{TAG}] 分段 {idx}/{count}：源片 [{src_start}, {src_start + gen_len})"
                  f"→ 生成 {gen_len} 帧，成片保留 [{start}, {start + kept_target}) 共 {kept_target} 帧"
                  f"（参考视频 {ref_video_frames} 帧{' + 原声' if ref_video_audios else ''}"
                  f"{' + 上段尾窗' if append_prev and i > 0 and prev_pixels is not None else ''}）")

            # ③ ref2va 编码（每段都重新注入同一组定妆图）
            #    与「JZL - 🎬 MiniMax H3 参考编码」完全同一套逻辑与参数：
            #    ref_image_size="match" + ref_scale（参考图放大 / 面积倍率）
            probe = {}
            positive, latent = _encode_ref_to_video(
                clip, vae, audio_vae, seg_prompt, width, height, gen_len,
                ref_images=ref_imgs, ref_videos=[src_clip], ref_video_audios=ref_video_audios,
                ref_audios=[], ref_image_size="match", ref_scale=float(ref_scale),
                slot_names=ref_names, probe=probe)
            _info = _fmt_call_info(probe)
            print(f"[JZL-{TAG}]   参考调用：{_info if _info else '（仅文本，无参考）'}")

            # ④ 段间衔接：把上一段「生成结果」的尾窗钉成本段首帧
            seam_plan = None
            if i > 0 and seam_enabled:
                # 窗口合法性按【本次目标 latent】算（像素模式下不依赖上段 latent）
                seam_plan = _seam_plan(latent["samples"], window)
                if seam_plan is None:
                    print(f"[JZL-{TAG}]   分段 {idx}：⚠️ 目标段长放不下 {window} 帧窗口 → 本段按独立生成处理")
                elif int(seam_plan["window"]) != window:
                    print(f"[JZL-{TAG}]   分段 {idx}：窗口按段长自动调整为 {seam_plan['window']} 帧"
                          f"（配置 {window} 帧）")

            if seam_plan is not None and guide_mode == "pixels" and prev_pixels is not None:
                positive = _apply_pixel_guide(positive, prev_pixels, vae, width, height,
                                             seam_plan["window"])
                print(f"[JZL-{TAG}]   衔接（像素）：上段末 {int(prev_pixels.shape[0])} 帧 → 按 "
                      f"{width}x{height} 重编码钉成本段首帧（清晰，不插值）")
            elif seam_plan is not None and prev_latent is not None:
                positive, _kf = _seam_apply_guide(positive, prev_latent, seam_plan,
                                                 audio_link=True, vae=vae, guide_source="latent")
                positive = _seam_fit_guides_to_canvas(positive, latent["samples"], tag=TAG)
                print(f"[JZL-{TAG}]   衔接（latent）：上段尾窗 {seam_plan['window']} 帧"
                      f"（token {seam_plan['trim_tokens']}）已钉成本段首帧")

            seed_i = int(_resolve_seed(seed_mode, int(seed or 0), i))

            # ⑤ 一采
            first = _sample_first_pass(model, positive, latent, sample_decode, seed_i, vae, tag=TAG)
            samples = first
            positive2 = positive

            # ⑥ 二采（可选）：latent 放大 + 补采，结果只用于解码落盘
            if use_second:
                # ⑥b 二采也要钉帧：用同一批像素按二采画布重新编码，
                #     否则二采只能把一采分辨率的 keyframe 插值放大 → 糊
                if (pin_second and seam_plan is not None and guide_mode == "pixels"
                        and prev_pixels is not None):
                    positive2 = _apply_pixel_guide(positive, prev_pixels, vae, sec_w, sec_h,
                                                   seam_plan["window"])
                    print(f"[JZL-{TAG}]   衔接（二采·像素）：按 {sec_w}x{sec_h} 重编码钉成本段首帧")
                try:
                    samples = _run_second_pass(model, positive2, first, sample_decode, second_run,
                                               gp, width, height, seed_i, tag=TAG)
                except Exception as _se:
                    print(f"[JZL-{TAG}]   分段 {idx}：⚠️ 二采失败，本段退回一采结果（{type(_se).__name__}: {_se}）")
                    samples = first

            # ⑦ 解码
            image, audio = _decode_av(vae, audio_vae, samples, sample_decode, tag=TAG)
            if image is None:
                raise RuntimeError("VAE 解码返回空画面（检查「视觉VAE」是否接对、是否与模型匹配）")
            raw_frames = int(image.shape[0])

            # ⑧ 裁掉段首「窗口 + 过渡」（像素域：保住 VAE 解码的 5 token = 17 帧分组相位）
            trim_plan = seam_plan
            if trim_plan is None and i > 0 and runway > 0:
                # 没钉上帧，但跑道帧照样生成了 → 必须照丢，否则成片开头会重复上一段内容
                trim_plan = {"window": window}
                print(f"[JZL-{TAG}]   分段 {idx}：⚠️ 本段未钉帧，但仍有 {runway} 帧跑道 → 只裁跑道不裁衔接"
                      f"（接缝可能跳一下，属预期）")
            if trim_plan is not None and int(trim_plan.get("window", 0)) > 0:
                image, audio = _seam_trim_decoded(image, audio, trim_plan, settle_frames=settle,
                                                  trim_audio=True, tag=TAG)
                kept = int(image.shape[0])
                print(f"[JZL-{TAG}]   像素域裁切：解码 {raw_frames} 帧 - 窗口 {int(trim_plan['window'])}"
                      f" - 过渡 {settle} = {kept} 帧")
                if kept < kept_target:
                    print(f"[JZL-{TAG}]   分段 {idx}：⚠️ 裁切后只有 {kept} 帧 < 目标 {kept_target} 帧"
                          f"（生成 {raw_frames} 帧，跑道 {runway} 帧）→ 本段会偏短，请核对段长/窗口设置")

            # ⑨ 末段不足一段：截到源片剩余帧数（音画同源同量）
            if int(image.shape[0]) > kept_target:
                image = image[:kept_target]
            audio_keep = int(image.shape[0])

            # ⑩ 音轨策略
            if mute:
                audio = None
            elif keep_original_audio and isinstance(source_audio, dict):
                audio = _slice_audio(source_audio, start, audio_keep)
            # 否则保留模型生成的音轨（已在 ⑧ 里随画面同步裁过）

            # ⑪ 接缝亮度匹配（只动亮度、加性、限幅）——必须在落盘前做，拼接是 -c copy 不重编码
            if prev_luma is not None and trim_plan is not None:
                image = _seam_luma_ramp(image, prev_luma)[0]

            # ⑫ 落盘
            wav_keep = None
            if merge_wavs is not None and audio is not None:
                wav_keep = os.path.join(out_dir, f"._jzl_swap{i + 1}_{counter0 + i}_{os.getpid()}.wav")
                merge_wavs.append(wav_keep)
            path = _save_segment_mp4(image, audio, out_dir, idx, story_name=safe_story,
                                     counter=counter0 + i, batch=batch, keep_wav=wav_keep)
            saved_files.append(path)
            total_out += audio_keep
            print(f"[JZL-{TAG}] 分段 {idx}/{count} 已落盘：{path}（{audio_keep} 帧 = "
                  f"{audio_keep / float(VIDEO_FPS):.2f} 秒，种子 {seed_i}）")

            # ⑬ 给下一段留状态：
            #    · 像素尾帧（uint8，按二采画布存 → 下段一采/二采都能用清晰像素重编码）
            #    · 一采 latent（仅 latent 引导模式需要）
            #    · 本段落盘后末帧亮度（接缝亮度匹配）
            store_w, store_h = (sec_w, sec_h) if use_second else (width, height)
            try:
                prev_pixels = _store_pixels(image, store_w, store_h, max(5, int(window)))
            except Exception as _pe:
                prev_pixels = None
                print(f"[JZL-{TAG}]   分段 {idx}：⚠️ 保存尾部像素失败（{_pe}）→ 下一段改用 latent 引导")
            if guide_mode != "pixels":
                try:
                    prev_latent = first.to("cpu") if hasattr(first, "to") else first
                except Exception:
                    prev_latent = first
            try:
                prev_luma = _seam_frame_luma(image[-1]) if audio_keep > 0 else prev_luma
            except Exception:
                prev_luma = None

            try:
                del image, audio, samples, first, positive, positive2, latent, src_clip
            except Exception:
                pass
            _clean_cache()

        for seg in plan:
            try:
                _run_segment(seg)
            except Exception as e:
                # 单段失败不拖垮整片：记录后继续下一段（上一段好结果仍是下一段的衔接源）
                errors.append(f"第{seg['index'] + 1}段失败：{type(e).__name__}: {e}")
                print(f"[JZL-{TAG}] ⚠️ 第{seg['index'] + 1}段失败：{type(e).__name__}: {e}")
                try:
                    import traceback as _tb
                    print(_tb.format_exc())
                except Exception:
                    pass
                try:
                    _clean_cache()
                except Exception:
                    pass

        # ⑭ 拼接
        merged_path = ""
        out_files = list(saved_files)
        if save_mode == "拼接保存" and saved_files:
            try:
                merged_path = os.path.join(
                    out_dir,
                    f"{safe_story}_{batch}次生成分段合并_"
                    f"{_next_counter(out_dir, f'{safe_story}_{batch}次生成分段合并'):05d}.mp4")
                merged_path = _merge_mp4_concat(saved_files, merged_path, audio_wavs=merge_wavs)
                out_files = [merged_path]
                print(f"[JZL-{TAG}] 拼接完成（{len(saved_files)} 段）：{merged_path}")
                if auto_merge_delete:
                    _del = 0
                    for _sp in saved_files:
                        try:
                            if os.path.exists(_sp):
                                os.remove(_sp)
                                _del += 1
                        except Exception:
                            pass
                    if _del:
                        print(f"[JZL-{TAG}] 已按配置删除 {_del} 个分段视频（仅保留合并结果）")
            except Exception as _me:
                errors.append(f"拼接失败：{_me}（各分段已保留）")
                merged_path = ""
        if merge_wavs:
            for _w in merge_wavs:
                try:
                    if os.path.exists(_w):
                        os.remove(_w)
                except Exception:
                    pass

        print(f"[JZL-{TAG}] ─────────────────────────────────────────────")
        print(f"[JZL-{TAG}] 成片合计 {total_out} 帧 = {total_out / float(VIDEO_FPS):.2f} 秒"
              f"（源片 {total_src} 帧 = {total_src / float(VIDEO_FPS):.2f} 秒，{count} 段）")
        if abs(total_out - total_src) > 2:
            print(f"[JZL-{TAG}] ⚠️ 成片与源片差 {total_out - total_src:+d} 帧"
                  f"（{count} 段 × 段长 {seg_len} 未整除源片长度时的正常取整，差值上限 = 段长）")
        for e in errors:
            print(f"[JZL-{TAG}] {e}")
        return io.NodeOutput(merged_path, list(out_files))
