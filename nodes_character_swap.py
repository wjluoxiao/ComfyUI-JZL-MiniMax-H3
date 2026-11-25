"""JZL MiniMax H3 — 🎭 人物替换（长视频逐段 vid2vid 换人）

把一段**已有视频**里的演员换成参考图里的演员：源片按 17k+5 切成段，每段以
「源片同名段（多取重叠帧）」当 `<Video 1>` 参考视频、定妆图当 `<Picture 1>` 走 ref2va 重新生成；
第 2 段起把上一段**生成结果**的像素尾窗钉成本段首帧 keyframe（永不降噪、每步重注入），
段首重叠帧解码后按**像素域**裁掉 ⇒ 成片时长 = 源片时长、成片段界无缝。

「重叠帧数」是同一个数：钉帧窗口 = 段首丢弃量 = 上一段结尾取出量。段首生成位置 0 对应源片
「本段起点 − 重叠」，取的正是上一段已产出的那段内容 → 同一条时间轴，既不抢跑也不跳帧。

为什么换人的接缝比「无限时长」更容易做（也是本节点与导演台的关键差异）：
  · 无限时长没有“真值”，段 2+ 只能靠上段尾帧 + 新提示词往下编 → 画面/色彩/节奏会缓慢漂移；
  · 换人有**逐帧真值**：每段都重新对位同一段源片（`ref_videos`），漂移在段内就被真值钳住，
    不存在跨段累计漂移。keyframe 钉帧只负责“画面不跳”，**对位**由源片参考负责。
  · 因此这里的重叠不是“靠重叠区猜”，而是**源片切片重叠**：重叠段对应的源片帧 = 上一段已经产出的
    那一段内容，天然就是同一个时间轴，不会累积误差。

音视频事实（沿用导演台已验证的结论，改错会直接重影/闪烁）：
  · 生成帧数恒为 17k+5；重叠恒为 17m+5 ⇒ 生成−重叠 = 17(k−m)，段首裁切后必落在 token 边界；
  · 裁切必须在**像素域**做：latent 域裁切会让 VAE 解码的「5 token = 17 帧」分组相位错位 → 周期性重影；
  · 成片要 17k+5 才不被编码器截掉 ⇒ 生成量减重叠后多出的余量从**尾部**丢掉（不丢中间）。
"""

import json
import math
import os
import re
import sys
import time

import torch

import folder_paths
from comfy_api.latest import io

# 采样/放大器的进度条走 stderr，本文件的日志走 stdout。stdout 重定向到日志文件时默认是块缓冲，
# 日志会成批晚到、和控制台里的进度条对不上，看起来就像「某一次采样的日志被吞了」。改成行缓冲。
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# 复用导演台（nodes_asset_manager.py）里已经踩过坑的全部实现，绝不另写一套
from .nodes_asset_manager import (
    SAMPLE_MODE_NORMAL,
    SAMPLE_MODE_SELFLIFT,
    SEGMENT_DURATION_CHOICES,
    SEAM_DEFAULT_WINDOW,
    SEAM_WINDOW_CHOICES,
    VIDEO_FPS,
    _cond_from_parts,
    _decode_av,
    _empty_av_latent,
    _encode_ref_to_video,
    _log_once,
    _next_batch,
    _next_counter,
    _parse_duration_spec,
    _resolve_gen_size,
    _resolve_length,
    _resolve_seed,
    _run_upscaler,
    _safe_story_name,
    _seam_resolve_window,
    _save_segment_mp4,
    _seam_video_tensor,
    _split_av_latent,
)
from .nodes import _resize, align_frame_count
from . import h3_windows as _win

TAG = "人物替换"

# 参考视频只给这几档（都是实测/推导出来有意义的档，不给自由输入）：
#   面积倍数：token 数 ∝ 面积，0.5 约省一半编码，0.25 已经明显糊（再低换人就失效）
#   强制帧率：参考帧数必须 5+17k，太低会掉到 5 帧 = 等于没参考，所以下限 8fps

# 落盘前缀默认值：「文件夹/文件名前缀」，例如 视频/人物替换 → output/视频/人物替换_2次生成分段1_00001.mp4
DEFAULT_OUT_PREFIX = "视频/人物替换"


def _resolve_out_prefix(prefix, out_root, fallback_name="人物替换"):
    """把「视频/人物替换」拆成 (落盘目录, 文件名前缀)。

    前几段是 output 下的相对目录（可多级），最后一段是文件名前缀。
    `..` / 盘符 / 前导斜杠都会被丢掉，保证不会写到 output 之外。
    fallback_name：前缀被清空时的兜底文件名（人物替换 / 视频编辑 各自用自己那个）。
    """
    raw = str(prefix or "").replace("\\", "/").strip().strip("/")
    parts = [_safe_story_name(p) for p in raw.split("/")
             if p and p not in (".", "..") and not p.endswith(":")]
    if not parts:
        return out_root, _safe_story_name(fallback_name)
    name = parts[-1]
    sub = os.path.join(out_root, *parts[:-1]) if len(parts) > 1 else out_root
    return sub, name

# 单块解码的帧数上限：解码把 latent 变成 IMAGE float32（约 5.9 MB/帧），
# 超过这个量就分块解，保证内存不随片长增长（1200 帧 ≈ 7 GB @480x864 / 3 GB @352x608）
DECODE_FRAMES_BUDGET = 1200

# 固定段长选项（区间值对换人没有意义：段长决定源片切点，必须确定）
_SEGMENT_SECONDS_OPTIONS = tuple(x for x in SEGMENT_DURATION_CHOICES if x.startswith("固定"))


DEFAULT_PROMPT = (
    "[SHOT_START]\n"
    "\n"
    "===H3_PROMPT===\n"
    "subject_definitions:\n"
    "<Subject 1> 是 <Picture 1> 中的人物。身份、五官、脸型、发型、发色、肤色、体型、服装、配饰与配色"
    "全部以 <Picture 1> 为准；<Picture 1> 只是参考图，不作为画面内容出现。\n"
    "\n"
    "summary:\n"
    "[reference generation + audio reference] 目标视频是 <Video 1> 的重制版：把 <Video 1> 里的人换成 "
    "<Subject 1>，其余一律沿用 <Video 1>。\n"
    "\n"
    "retention_analysis:\n"
    "<Picture 1>: fully_preserved —— 身份与外观（长相、发型、服装、配饰、配色）。\n"
    "<Video 1>: attribute_transfer —— 动作、表情、口型、走位、时机、机位、景别、景深、镜头运动；\n"
    "            fully_preserved —— 场景、道具、背景、光线方向、整体色调；\n"
    "            partially_preserved —— 画面里的人物被 <Subject 1> 取代，位置与遮挡关系不变。\n"
    "\n"
    "detailed_description:\n"
    "<Subject 1> 以 <Video 1> 中人物的同一姿态、同一动作节奏、同一表情完成整段表演，"
    "口型开合与停顿与 <Video 1> 一致；镜头、景别、景深、运动与 <Video 1> 一致；"
    "场景、道具、背景、光线与色彩与 <Video 1> 一致。只替换人物本身：脸、发型、服装、配饰换成 "
    "<Picture 1> 的，动作与场景不换。\n"
    "{video_desc}"
    "{image_desc}"
    "\n"
    "[Shot 1] 沿用 <Video 1> 的表演与运镜。\n"
    "\n"
    "overall_soundscape: 沿用 <Video 1> 的原始环境声。\n"
    "\n"
    "non_diegetic_music: N/A\n"
    "[SHOT_END]"
)

# 提示词来源（「📝 提示词预设 → 提示词来源」下拉）
PROMPT_SOURCES = ("系统默认", "内置skill", "内置skill·动物", "内置skill·非活物", "自定义skill")
BUILTIN_SWAP_SKILL = "人物替换提示词.md"
BUILTIN_ANIMAL_SKILL = "动物替换提示词.md"
BUILTIN_INANIMATE_SKILL = "非活物替换提示词.md"
BUILTIN_MULTI_SKILL = "多人替换提示词.md"
# 旧档位名（老工作流里存的值，仍被 LEGACY_SOURCE_ALIAS 认）
ANIMAL_SOURCE = "内置skill·动物"
INANIMATE_SOURCE = "内置skill·非活物"
MULTI_SOURCE = "内置skill·多人"

# 动物拟人替换的兜底模板（LLM 不可用 / 生成失败时用）：
# 与人物模板的关键差别 —— 明确「人类主体被整体替换掉、画面里不再有人、也不多出一只动物」。
DEFAULT_ANIMAL_PROMPT = (
    "[SHOT_START]\n"
    "\n"
    "===H3_PROMPT===\n"
    "subject_definitions:\n"
    "<Subject 1> 是 <Picture 1> 中的动物。物种、品种、毛色、花纹、体型、耳朵与尾巴形态、眼睛、"
    "配饰与配色全部以 <Picture 1> 为准；<Picture 1> 只是参考图，不作为画面内容单独出现。"
    "<Subject 1> 在本段视频里**替代 <Video 1> 中原本的那一个人类角色**，以拟人化方式表演："
    "保留动物的外观与毛发质感，采用人类的站姿、手势、步态与表情。\n"
    "\n"
    "summary:\n"
    "[reference generation + audio reference] 目标视频是 <Video 1> 的重制版：把 <Video 1> 里唯一的人类主体"
    "整体替换为 <Subject 1>（<Picture 1> 中的动物），由它拟人化地完成同一段表演，其余一律沿用 <Video 1>。"
    "画面中不出现任何人类，也不出现第二只动物。\n"
    "\n"
    "retention_analysis:\n"
    "<Picture 1>: fully_preserved —— 物种、毛色花纹、体型、耳朵尾巴形态、眼神、配饰与配色。\n"
    "<Video 1>: attribute_transfer —— 动作、姿态、表情、口型开合与停顿、走位、时机、机位、景别、景深、镜头运动；\n"
    "            fully_preserved —— 场景、道具、背景、光线方向、整体色调、构图与遮挡关系；\n"
    "            replaced —— 画面里的人类主体被 <Subject 1> 完全取代（同位置、同轮廓比例、同遮挡关系），"
    "替换后画面内不再有人。\n"
    "\n"
    "detailed_description:\n"
    "<Subject 1> 以 <Video 1> 中那个人物的同一姿态、同一动作节奏、同一表情完成整段拟人化表演："
    "直立或按其原本姿势站立、用前肢做出对应的手势、头部与眼神随台词变化，口型开合与停顿与 <Video 1> 一致；"
    "镜头、景别、景深、运动与 <Video 1> 一致；场景、道具、背景、光线与色彩与 <Video 1> 一致。"
    "只替换主体本身：人类角色整体换成 <Picture 1> 的动物，动作与场景不换；"
    "画面中只有这一只动物，没有人类，也没有第二只动物。\n"
    "{video_desc}"
    "{image_desc}"
    "\n"
    "[Shot 1] 沿用 <Video 1> 的表演与运镜，由 <Subject 1> 拟人化完成。\n"
    "\n"
    "overall_soundscape: 沿用 <Video 1> 的原始环境声。\n"
    "\n"
    "non_diegetic_music: N/A\n"
    "[SHOT_END]"
)

# 非活物（玩具/玩偶/毛绒玩具/木头人/火柴人/机器人/砖头人…）替换的兜底模板。
# 关键差别：主体是无生命的物件 → 必须写清「保持材质、由它占住原主体位置、做同一套动作」，
# 且不能留下人类/动物本体，也不能把它当成道具塞进画面里。
DEFAULT_INANIMATE_PROMPT = (
    "[SHOT_START]\n"
    "\n"
    "===H3_PROMPT===\n"
    "subject_definitions:\n"
    "<Subject 1> 是 <Picture 1> 中的非活物／人工造物（玩偶、毛绒玩具、木头人、火柴人、机器人、砖头人……按实际写清）。"
    "外形、材质、颜色、纹理、比例、结构细节（关节、缝线、螺丝、砖块堆叠、木纹、毛绒质感）与配饰全部以 <Picture 1> 为准；"
    "<Picture 1> 只是参考图，不作为画面内容单独出现。<Subject 1> 在本段视频里**替代 <Video 1> 中原本的那个主体**"
    "（无论原本是人还是动物），占住它的位置、做出它的动作，但一直保持自己无生命的材质与结构。\n"
    "\n"
    "summary:\n"
    "[reference generation + audio reference] 目标视频是 <Video 1> 的重制版：把 <Video 1> 里唯一的主体"
    "整体替换为 <Subject 1>（<Picture 1> 中的非活物），由它以拟人化方式完成同一段表演，其余一律沿用 <Video 1>。"
    "画面中不出现原本的人类或动物，也不出现第二个同类物件。\n"
    "\n"
    "retention_analysis:\n"
    "<Picture 1>: fully_preserved —— 外形、材质、颜色、纹理、比例、结构细节与配饰。\n"
    "<Video 1>: attribute_transfer —— 动作、姿态、节奏、走位、时机、机位、景别、景深、镜头运动；\n"
    "            fully_preserved —— 场景、道具、背景、光线方向、整体色调、构图与遮挡关系；\n"
    "            replaced —— 画面里的主体被 <Subject 1> 完全取代（同位置、同轮廓比例、同遮挡关系），"
    "替换后画面内不再有人类和动物。\n"
    "\n"
    "detailed_description:\n"
    "<Subject 1> 以 <Video 1> 中那个主体的同一姿态、同一动作节奏完成整段拟人化表演："
    "按自身结构活动（玩偶/木偶靠关节摆动、火柴人靠细线状肢体、机器人靠机械关节、砖头人靠砖块层层堆叠位移），"
    "动作幅度与 <Video 1> 一致；不做人类式的皮肤、肌肉或毛发变化，材质始终保持 <Picture 1> 的样子。"
    "镜头、景别、景深、运动与 <Video 1> 一致；场景、道具、背景、光线与色彩与 <Video 1> 一致。"
    "只替换主体本身，动作与场景不换；画面中只有这一个物件，没有人类、没有动物，也没有第二个同类物件。\n"
    "{video_desc}"
    "{image_desc}"
    "\n"
    "[Shot 1] 沿用 <Video 1> 的表演与运镜，由 <Subject 1> 拟人化完成。\n"
    "\n"
    "overall_soundscape: 沿用 <Video 1> 的原始环境声。\n"
    "\n"
    "non_diegetic_music: N/A\n"
    "[SHOT_END]"
)

# 多人替换的兜底模板（LLM 不可用 / 生成失败时用）：
# 关键差别：人数必须保持不变，每个 <Subject i> 都要锚定「画面里第 i 个主体」的位置/原角色。
DEFAULT_MULTI_PROMPT = (
    "[SHOT_START]\n"
    "\n"
    "===H3_PROMPT===\n"
    "subject_definitions:\n"
    "<Subject 1> 是 <Picture 1> 中的人物。身份、五官、脸型、发型、发色、肤色、体型、服装、配饰与配色全部以 "
    "<Picture 1> 为准；它对应 <Video 1> 画面里的第 1 个主体（按画面从左到右 / 从前到后的顺序）；"
    "<Picture 1> 只是参考图，不作为画面内容出现。\n"
    "<Subject 2> 是 <Picture 2> 中的人物，外观全部以 <Picture 2> 为准；它对应 <Video 1> 画面里的第 2 个主体。\n"
    "<Subject 3> 是 <Picture 3> 中的人物，外观全部以 <Picture 3> 为准；它对应 <Video 1> 画面里的第 3 个主体。\n"
    "（画面里有几个主体就有几个 <Subject i>，与定妆图张数一致；多余人物的行请按同样句式补全或删掉）\n"
    "\n"
    "summary:\n"
    "[reference generation + audio reference] 目标视频是 <Video 1> 的重制版：把 <Video 1> 里的每一个主体**按位置逐一**"
    "换成对应的定妆图人物（第 i 个主体 → <Subject i>），**人数保持不变**，其余一律沿用 <Video 1>。"
    "不新增角色，也不让任何角色消失。\n"
    "\n"
    "retention_analysis:\n"
    "<Picture 1..N>: fully_preserved —— 各自的身份与外观（长相、发型、服装、配饰、配色），彼此不混用、不互换。\n"
    "<Video 1>: attribute_transfer —— 每个人的动作、表情、口型、走位、时机、机位、景别、景深、镜头运动；\n"
    "            fully_preserved —— 场景、道具、背景、光线方向、整体色调、人数、站位与遮挡关系；\n"
    "            replaced —— 画面里每个主体分别被对应的 <Subject i> 取代，一个不多一个不少，位置与遮挡不变。\n"
    "\n"
    "detailed_description:\n"
    "画面里原本的每个人都在原来的位置、以原来的姿态与节奏表演，只是外貌换成了对应定妆图那个人："
    "第 1 个主体像 <Picture 1>、第 2 个主体像 <Picture 2>，依此类推；彼此身份与服装不混用、不互换，"
    "人数、站位、遮挡关系、动作时机全部保持与 <Video 1> 一致。口型开合与停顿与 <Video 1> 一致；"
    "镜头、景别、景深、运动与 <Video 1> 一致；场景、道具、背景、光线与色彩与 <Video 1> 一致。"
    "只替换人本身，动作与场景不换；不新增角色，也不让任何角色消失。\n"
    "{video_desc}"
    "{image_desc}"
    "\n"
    "[Shot 1] 沿用 <Video 1> 的表演与运镜，多人同时按对应 <Picture> 替换。\n"
    "\n"
    "overall_soundscape: 沿用 <Video 1> 的原始环境声。\n"
    "\n"
    "non_diegetic_music: N/A\n"
    "[SHOT_END]"
)


def _fill_desc(template, video_desc, image_desc):
    """把「视频描述 / 图片描述」填进模板；空的就整行去掉，不留空段落。"""
    out = str(template or "")
    v = str(video_desc or "").strip()
    i = str(image_desc or "").strip()
    out = out.replace("{video_desc}", (f"视频描述补充：{v}\n" if v else ""))
    out = out.replace("{image_desc}", (f"图片描述补充：{i}\n" if i else ""))
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


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


def _as_audio_dict(audio):
    """把各种 AUDIO 表示统一成 {"waveform": [B,C,L], "sample_rate": int}；取不到返回 None。

    节点只用它做「按帧号切片落盘」，**从不参与参考编码，也从不参与 latent 音频解码**。
    """
    if audio is None:
        return None
    if isinstance(audio, dict):
        wf = audio.get("waveform")
        if wf is None:
            return None
        return {"waveform": wf, "sample_rate": int(audio.get("sample_rate", 32000) or 32000)}
    wf = getattr(audio, "waveform", None)
    if wf is not None:
        return {"waveform": wf, "sample_rate": int(getattr(audio, "sample_rate", 32000) or 32000)}
    if isinstance(audio, (list, tuple)) and audio and hasattr(audio[0], "shape"):
        sr = int(audio[1]) if len(audio) > 1 else 32000
        return {"waveform": audio[0], "sample_rate": sr}
    if hasattr(audio, "shape") and getattr(audio, "ndim", 0) in (2, 3):
        return {"waveform": audio, "sample_rate": 32000}
    return None


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


def _load_swap_skill(name, tag=TAG, skill_dir=""):
    """读 skill 文件。

    skill_dir 非空 = 用户自己选的目录（「📂 选择 skill 文件目录」），直接从那里读；
    为空时退回插件自带 skills/jzl（与「✨ MiniMax-skill加载」同一目录与规则）。
    """
    try:
        from .nodes_skill import _read_skill, read_skill_in_dir
        if str(skill_dir or "").strip():
            return read_skill_in_dir(skill_dir, name)
        return _read_skill(name)
    except Exception as e:
        print(f"[JZL-{tag}] ⚠️ skill 读取失败（{name or '未选择'}）：{e}"
              f" → 本次回退「系统默认」提示词", flush=True)
        return None


def _swap_llm_call(prompt_cfg, system_prompt, user_msg, tag=TAG):
    """调 LLM 生成提示词。失败返回 None，由调用方回退系统默认（绝不因 LLM 挂掉而中断生成）。

    「本地模型(CLIP)」与「在线API」两条分支**完全独立**：
      · 走本地时只读 prompt_cfg["llm"]，绝不碰 api（不校验、不请求 → 不会出现 401）；
      · 走在线时只读 prompt_cfg["api"]，绝不碰 clip。
    后端判定统一走 _resolve_llm_backend，与面板显示的勾选项永远一致。
    """
    api_mode = _is_api_backend(prompt_cfg)
    backend_label = "在线API" if api_mode else "本地模型"
    print(f"[JZL-{tag}] LLM 后端：{backend_label}（只使用该后端的参数）", flush=True)
    try:
        if api_mode:
            from .nodes_llama import JZL_MiniMax_ScriptProcessor
            api = prompt_cfg.get("api") or {}
            if not str(api.get("model") or "").strip():
                print(f"[JZL-{tag}] ⚠️ 在线 API 未填模型名 → 回退「系统默认」提示词", flush=True)
                return None
            text = JZL_MiniMax_ScriptProcessor._call_api(
                json.dumps(api, ensure_ascii=False), system_prompt, user_msg)
        else:
            # 本地 = 官方「Load CLIP」+「Generate Text」（models/text_encoders 的 CLIP 文本编码器）
            llm = prompt_cfg.get("llm") or {}
            clip_name = str(llm.get("clip_name") or "").strip()
            if not clip_name:
                print(f"[JZL-{tag}] ⚠️ 未选择本地 CLIP 模型 → 回退「系统默认」提示词", flush=True)
                return None
            import comfy.sd
            import folder_paths as _fp
            from comfy_extras.nodes_textgen import TextGenerateV2
            # 模型可能放在 models/text_encoders 或 models/clip → 两处都找
            clip_path = _fp.get_full_path("text_encoders", clip_name) or _fp.get_full_path("clip", clip_name)
            if not clip_path:
                print(f"[JZL-{tag}] ⚠️ 找不到 CLIP 模型「{clip_name}」"
                      f"（models/text_encoders 和 models/clip 里都没有）→ 回退「系统默认」提示词", flush=True)
                return None
            # 生成过程中的进度条会走 ComfyUI 的 PROGRESS_BAR_HOOK，它要读这两个属性；
            # 而 last_prompt_id 只在 prompt_worker 里赋值 → 在 HTTP 路由（LLM验证）里必须自己补
            try:
                from server import PromptServer as _PS
                _ps = _PS.instance
                if getattr(_ps, "last_prompt_id", None) is None:
                    _ps.last_prompt_id = ""
                if not hasattr(_ps, "last_node_id"):
                    _ps.last_node_id = None
            except Exception:
                pass
            _type_name = str(llm.get("clip_type") or "qwen_image")
            clip_obj = comfy.sd.load_clip(
                ckpt_paths=[clip_path],
                embedding_directory=_fp.get_folder_paths("embeddings"),
                clip_type=getattr(comfy.sd.CLIPType, _type_name.upper(),
                                  comfy.sd.CLIPType.STABLE_DIFFUSION))
            try:
                out = TextGenerateV2.execute(
                    clip=clip_obj,
                    prompt=user_msg,
                    max_length=int(llm.get("max_length") or 2048),
                    sampling_mode={
                        "sampling_mode": "on",
                        "temperature": float(llm.get("temperature") or 0.7),
                        "top_k": int(llm.get("top_k") or 64),
                        "top_p": float(llm.get("top_p") or 0.95),
                        "min_p": float(llm.get("min_p") or 0.05),
                        "repetition_penalty": float(llm.get("repetition_penalty") or 1.05),
                        "presence_penalty": float(llm.get("presence_penalty") or 0.0),
                        "seed": int(llm.get("seed") or 0),
                    },
                    thinking=bool(llm.get("thinking")),
                    use_default_template=bool(llm.get("use_default_template", True)),
                    system_prompt=system_prompt,
                )
                text = out.args[0] if hasattr(out, "args") else out[0]
            finally:
                # 生成完立刻把文本编码器退回，给后面的视频模型腾显存
                try:
                    import comfy.model_management as _mm
                    _mm.unload_all_models()
                    _mm.soft_empty_cache()
                    import torch as _t
                    if _t.cuda.is_available():
                        _t.cuda.empty_cache()
                except Exception:
                    pass
    except Exception as e:
        print(f"[JZL-{tag}] ⚠️ LLM 调用失败（{type(e).__name__}: {e}）"
              f" → 回退「系统默认」提示词", flush=True)
        return None
    text = str(text or "").strip()
    if not text or text.startswith(("[API 错误]", "[LLM 错误]", "[API 配置错误]")):
        hint = ""
        if api_mode and ("401" in text or "403" in text or "Unauthorized" in text
                         or "Authorization" in text):
            hint = ("\n   ↳ 这是**在线 API** 的鉴权错误：请检查「LLM 后端 = 在线API」里的 "
                    "API Key / Base URL / 模型名；若本来想用本地 CLIP，请把后端切到「本地模型」。")
        print(f"[JZL-{tag}] ⚠️ LLM 返回异常（后端={backend_label}）：{text[:160] or '(空)'}"
              f"{hint} → 回退「系统默认」提示词", flush=True)
        return None
    return text


# 提示词来源（「📝 提示词预设 → 提示词来源」）现在是**两个分支**（与「本地模型 / 在线API」同一交互）：
#   · 内置skill   → 再选一个具体的内置 skill（pc.builtin）
#   · 自定义skill → 加载/选择一个本地 .md 文件（pc.skill）
# 「系统默认」只在 LLM 关闭（或回退）时使用，不再出现在下拉里。
PROMPT_SOURCES = ("系统默认", "内置skill", "自定义skill")
PROMPT_LLM_SOURCES = ("内置skill", "自定义skill")

# 旧值 → 新分支的内置 skill 名（老工作流兼容：以前每种内置 skill 各占一个来源档位）
LEGACY_SOURCE_ALIAS = {
    "内置skill·动物": "动物替换",
    "内置skill·非活物": "非活物替换",
    "内置skill·多人": "多人替换",
}

# 内置 skill 名 → (skill 文件, 兜底模板(None=人物模板), LLM 指令里额外的一段约束(None=通用换人话术))
BUILTIN_SKILLS = {
    "人物替换": (BUILTIN_SWAP_SKILL, None, None),
    "动物替换": (
        BUILTIN_ANIMAL_SKILL, DEFAULT_ANIMAL_PROMPT,
        "请根据下面的素材描述，写一段用于「**用参考图中的动物替换视频里的那个人**」的 MiniMax-H3 提示词。\n"
        "重点：这是**替换**，不是加入 —— 原本那个人完全消失，由这只动物占住他的位置、拟人化地做同样的动作；\n"
        "画面里不能出现人类，也不能出现第二只动物，更不能写成动物与人互动。",
    ),
    "非活物替换": (
        BUILTIN_INANIMATE_SKILL, DEFAULT_INANIMATE_PROMPT,
        "请根据下面的素材描述，写一段用于「**用参考图中的非活物（玩具/玩偶/毛绒玩具/木头人/火柴人/机器人/砖头人…）"
        "替换视频里的主体**」的 MiniMax-H3 提示词。\n"
        "重点：这是**替换**，不是加入、不是当道具 —— 原本那个人（或动物）完全消失，由这个物件占住他的位置、"
        "以自身结构做同一套拟人化动作；材质始终保持参考图的样子（毛绒/木质/塑料/金属/砖块），\n"
        "不能长出人类或动物的皮肤毛发、不能有人类的手脚，画面里不能再有原本的人类/动物，也不能出现第二个同类物件。",
    ),
    "多人替换": (
        BUILTIN_MULTI_SKILL, DEFAULT_MULTI_PROMPT,
        "请根据下面的素材描述，写一段用于「**把视频里的多个人分别换成对应的多张定妆图**」的 MiniMax-H3 提示词。\n"
        "重点：这是**逐一对位替换** —— 原来有几个人，换完还是几个人；每个人的位置、遮挡、动作与时机都不变，\n"
        "不许新增角色、不许有人消失、不许把两个参考图的身份混到一个人身上；\n"
        "必须在 subject_definitions 里写清每个 <Subject i> 对应画面里的哪个位置/哪个原角色。",
    ),
}
DEFAULT_BUILTIN_SKILL = "人物替换"


def _builtin_skill_file(name):
    """「内置 skill」下拉里的一个值 → 实际要读的文件名。

    两种值都认：
    · 内置四档（人物替换 / 动物替换 / 非活物替换 / 多人替换）→ 对应的自带 skill 文件；
    · 用户用「📥 导入Skill文件」放进内置目录的自定义文件，值就是文件名本身（.md/.txt）。
    认不出来的返回 ""（调用方回退默认内置 skill）。
    """
    key = str(name or "").strip()
    if not key:
        return ""
    if key in BUILTIN_SKILLS:
        return BUILTIN_SKILLS[key][0]
    if os.path.splitext(key)[1].lower() not in (".md", ".markdown", ".txt"):
        return ""
    try:
        from .nodes_skill import _skill_path
        _skill_path(key)                      # 只认内置目录里真实存在、非符号链接的文件
        return key
    except Exception:
        return ""


def _resolve_prompt_kind(prompt_cfg):
    """(来源分支, 内置 skill 名, 实际 skill 文件, 兜底模板, LLM 附加指令) —— 面板的两个分支 + 老值兼容。"""
    source = str(prompt_cfg.get("source") or "系统默认")
    builtin = str(prompt_cfg.get("builtin") or "")
    if source in LEGACY_SOURCE_ALIAS:                    # 老工作流：来源就是某个内置 skill
        builtin = LEGACY_SOURCE_ALIAS[source]
        source = "内置skill"
    if source not in PROMPT_LLM_SOURCES:
        source = "系统默认"
    if source != "内置skill":
        return source, "", "", None, None
    skill_file = _builtin_skill_file(builtin)
    if not skill_file:
        builtin = DEFAULT_BUILTIN_SKILL
        skill_file, template, lead = BUILTIN_SKILLS[builtin]
        return source, builtin, skill_file, template, lead
    if builtin in BUILTIN_SKILLS:
        _f, template, lead = BUILTIN_SKILLS[builtin]
    else:
        # 导入的 skill：没有专用兜底模板 / 附加指令 → 走通用换人话术（模板用系统默认）
        template, lead = None, None
    return source, builtin, skill_file, template, lead


def _build_swap_prompt(prompt_cfg, video_desc, image_desc, tag=TAG):
    """按「📝 提示词预设」产出最终提示词。

    系统默认 = 内置模板 + 用户填的「视频描述 / 图片描述」；
    内置skill（人物/动物/非活物/多人，以及用户导入到内置目录的 .md）= 对应 skill 当 LLM 设定词；
    自定义skill = 用户选的目录里的 .md 当 LLM 设定词。
    """
    source, builtin, skill_file, template, lead = _resolve_prompt_kind(prompt_cfg)
    fallback = _fill_desc(template or DEFAULT_PROMPT, video_desc, image_desc)
    if not bool(prompt_cfg.get("use_llm")) or source == "系统默认":
        return fallback
    if source == "内置skill":
        skill_name = skill_file
        skill_dir = ""
    else:
        skill_name = prompt_cfg.get("skill") or ""
        skill_dir = prompt_cfg.get("skill_dir") or ""
    skill = _load_swap_skill(skill_name, tag, skill_dir)
    if not skill:
        return fallback
    _miss = ("（用户未填写，请按通用替换场景处理）" if source == "内置skill"
             else "（用户未填写，请按通用换人场景处理）")
    v = str(video_desc or "").strip() or _miss
    i = str(image_desc or "").strip() or _miss
    lead = lead or "请根据下面的素材描述，写一段用于「把视频里的人换成参考图中的人」的 MiniMax-H3 提示词。"
    user_msg = (f"{lead}\n"
                "必须严格使用 ===H3_PROMPT=== 结构，并用 [SHOT_START] / [SHOT_END] 包裹。\n\n"
                f"【视频描述】\n{v}\n\n【图片描述】\n{i}")
    text = _swap_llm_call(prompt_cfg, skill, user_msg, tag)
    if not text:
        return fallback
    if "[SHOT_START]" not in text:
        text = "[SHOT_START]\n\n" + text + "\n[SHOT_END]"
    elif "[SHOT_END]" not in text:
        print(f"[JZL-{tag}] ⚠️ 提示词结尾没有 [SHOT_END]：多半是撞上 max_length 被截断了"
              f"（当前 {len(text)} 字）。把「max_length」调大再验证一次。", flush=True)
        text = text + "\n[SHOT_END]"
    label = f"内置skill·{builtin}" if source == "内置skill" else source
    print(f"[JZL-{tag}] 提示词：{label} 生成完成（{len(text)} 字）", flush=True)
    return text


def _resolve_overlap(want, seg_len, enabled=True):
    """校验「重叠帧数」，不合法直接报错（不静默降级）。

    重叠必须同时是 17k+5（钉帧/前缀帧数只能取 5/22/39/56，才能和模型
    「5 token = 17 帧」的分组对齐）**且小于段长**（否则上一段凑不出这么多帧当衔接）。
    """
    if not enabled:
        return 0
    try:
        want = int(want)
    except Exception:
        raise ValueError(f"「重叠帧数」必须是整数（收到 {want!r}）")
    choices = "/".join(str(v) for v in SEAM_WINDOW_CHOICES)
    if want not in SEAM_WINDOW_CHOICES:
        raise ValueError(
            f"「重叠帧数」只能是 {choices} 帧（这些值和模型的 5 token = 17 帧分组对齐），"
            f"当前是 {want} 帧。请在「🎭 采样解码设置 → 分段衔接」里重新选一个。")
    if want > int(seg_len) - 1:
        smaller = [v for v in SEAM_WINDOW_CHOICES if v < int(seg_len)]
        hint = f"或把重叠帧数改成 {max(smaller)} 帧" if smaller else ""
        raise ValueError(
            f"「重叠帧数」{want} 帧 ≥ 每段帧数 {int(seg_len)}：每段必须留一部分是新画面。"
            f"请把「每段时长」调大，{hint}")
    return want


def _second_area_scale(gp):
    """「放大倍数」= **面积倍率**（宽高比永远跟随一采）。返回线性缩放系数。"""
    area = max(1.0, float(gp.get("upscale_scale") or 1.0))
    return math.sqrt(area)


def _second_canvas(gp, width, height):
    """二采目标画布（像素，32 对齐）。

    · 放大倍数（默认）：一采画布 × sqrt(面积倍率) → 宽高比与一采完全一致，绝不会压扁竖屏；
    · 目标分辨率：固定 W×H（用户明确要这个尺寸时才用；比例与一采差太多会告警）。
    """
    if str(gp.get("second_mode") or "放大倍数") == "目标分辨率":
        tw = max(32, int(round(float(gp["second_width"]) / 32.0)) * 32)
        th = max(32, int(round(float(gp["second_height"]) / 32.0)) * 32)
        return tw, th
    s = _second_area_scale(gp)
    tw = max(32, int(round(width * s / 32.0)) * 32)
    th = max(32, int(round(height * s / 32.0)) * 32)
    return tw, th


# 源片帧率的候选值：用「原声真实时长」反推，命中这些常见帧率才采信
_SOURCE_FPS_CANDIDATES = (8.0, 10.0, 12.0, 15.0, 16.0, 20.0, 23.976, 24.0, 25.0,
                          29.97, 30.0, 48.0, 50.0, 59.94, 60.0)


def _detect_source_fps(n_frames, source_audio, tol=0.03):
    """用原声真实时长反推源视频帧率，返回 (fps, 原声秒数)。

    模型的时间轴固定按 24fps 解释帧数，所以 12fps 的 10 秒视频（120 帧）会被当成 5 秒。
    有了原声的真实秒数就能反推：120 帧 / 10 秒 = 12fps → 升帧到 24fps 才是真正的 10 秒。
    没接原声、或算出来不像任何常见帧率，就按 24fps 处理（返回 VIDEO_FPS）。
    """
    wf = source_audio.get("waveform") if isinstance(source_audio, dict) else None
    if wf is None:
        return float(VIDEO_FPS), None
    sr = int(source_audio.get("sample_rate", 32000) or 32000)
    dur = float(wf.shape[-1]) / float(sr)
    if dur <= 0.2 or int(n_frames) < 5:
        return float(VIDEO_FPS), dur
    est = float(n_frames) / dur
    best = min(_SOURCE_FPS_CANDIDATES, key=lambda f: abs(f - est))
    if abs(best - est) / best <= tol:
        return float(best), dur
    return float(VIDEO_FPS), dur


def _resample_to_24(frames, src_fps, dst_fps=float(VIDEO_FPS)):
    """按时间轴把源片重采样到 dst_fps（最近邻取样，返回新张量）。

    只改帧数，不改时长：12fps 120 帧（10 秒）→ 24fps 240 帧（还是 10 秒）。
    """
    n = int(frames.shape[0])
    target = max(5, int(round(n * float(dst_fps) / float(src_fps))))
    pos = torch.arange(target, dtype=torch.float64) * (float(src_fps) / float(dst_fps))
    idx = pos.floor().clamp_(0, n - 1).long()
    return frames.index_select(0, idx)


def _cache_source(frames, max_gb, tag=TAG):
    """可选：把源片一次性转成 uint8 缓存（内存降到 1/4），供逐段切片使用。

    为什么不一次性 resize 成画布：那要额外一份 float32 副本，长片上峰值反而更高。
    uint8 用「预分配 + 逐块填充」，峰值只有「原张量 + 目标张量」，填完立刻丢掉原张量。
    代价：1/255 量化误差（源片只是参考/对齐用，不影响生成质量）。
    """
    if frames is None or not hasattr(frames, "shape"):
        return frames, False
    total, h, w = int(frames.shape[0]), int(frames.shape[1]), int(frames.shape[2])
    need_gb = total * h * w * 3 * 4 / float(1024 ** 3)
    if max_gb <= 0 or need_gb <= float(max_gb):
        return frames, False
    try:
        out = torch.empty((total, h, w, 3), dtype=torch.uint8)
        step = 24
        with torch.no_grad():
            for s in range(0, total, step):
                out[s:s + step] = (frames[s:s + step].float().clamp(0.0, 1.0) * 255.0).round().to(torch.uint8)
        print(f"[JZL-{tag}] 源片常驻内存 {need_gb:.1f}GB > {float(max_gb):.1f}GB → 已转 uint8 缓存"
              f"（{need_gb / 4.0:.1f}GB，量化误差 1/255，不影响生成）")
        return out, True
    except Exception as e:
        print(f"[JZL-{tag}] ⚠️ 源片转 uint8 失败（{type(e).__name__}: {e}）→ 沿用 float32")
        return frames, False


def _slice_source(frames, start, count, uint8_cached, width, height):
    """从（可能是 uint8 缓存的）源片里切一段并按画布尺寸缩放。"""
    clip = _slice_frames(frames, start, count)
    if uint8_cached:
        clip = clip.float().div(255.0)
    return _resize_chunked(clip, width, height)


def _first_pass_desc(sd):
    """一采参数的一行摘要（控制台里两次采样必须有各自的日志，否则分不清跑到哪一步）。"""
    d = sd or {}
    return (f"{d.get('sampler') or 'res_multistep'} {int(d.get('steps', 4) or 4)} 步"
            f"｜{d.get('scheduler') or 'simple'}"
            f"｜降噪 {float(d.get('denoise', 1.0) or 1.0):.2f}")


def _emit_ui(**payload):
    """把「本次实际使用的提示词 / 随机种子」实时推给前端面板（预览提示词）。

    走 PromptServer.send_sync：节点开始跑就推，面板不用等整条片子结束。
    任何异常都吞掉 —— 推送失败绝不影响生成。
    payload 的值会被包成单元素列表（ComfyUI 的 ui 约定）。
    """
    try:
        from server import PromptServer
        ps = getattr(PromptServer, "instance", None)
        if ps is None:
            return
        data = {}
        for k, v in payload.items():
            data[k] = list(v) if isinstance(v, (list, tuple)) else [str(v)]
        ps.send_sync("jzl_swap_ui", data)
    except Exception:
        pass


def _sep_line():
    """一行分隔线（日志分段用，纯排版）。"""
    print(f"[JZL-{TAG}] ─────────────────────────────────────────────", flush=True)


def _hdr(line):
    """阶段标题：分隔线 + 「开始 XXX…（速度取决于硬件能力，请耐心等待！）」。"""
    _sep_line()
    print(f"[JZL-{TAG}]   {line}", flush=True)


def _fmt_secs(sec):
    """秒 → 「49.6 秒」/「3 分 36 秒」/「1 小时 12 分」，日志里更好读。"""
    try:
        sec = float(sec)
    except Exception:
        return "?"
    if sec < 60:
        return f"{sec:.1f} 秒"
    m, s = divmod(int(round(sec)), 60)
    if m < 60:
        return f"{m} 分 {s} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分"


def _prompt_cfg_label(pc):
    """「📝 提示词预设」的一句话摘要（配置总览用）。"""
    pc = pc or {}
    src, builtin, _f, _tpl, _lead = _resolve_prompt_kind(pc)
    if src == "系统默认":
        return "关（系统默认模板，不调用 LLM）"
    backend = "在线API" if _is_api_backend(pc) else "本地模型"
    if src == "内置skill":
        return f"开（内置skill·{builtin} / {backend}）"
    return f"开（自定义skill：{pc.get('skill') or '未选择'} / {backend}）"


class _Heartbeat:
    """长任务心跳：每 interval 秒打一行「仍在做…已用 xx 秒」，避免用户以为节点卡死。

    实现只用 1 个 daemon 线程 + Event；任何异常都吞掉；退出时最多等 1 秒（绝不拖慢主流程）。
    只在**没有 tqdm 在跑**的阶段使用（文本编码 / VAE 参考编码），不会打乱进度条。
    """

    def __init__(self, label_fn, interval=20.0):
        self._fn = label_fn
        self._interval = max(5.0, float(interval))
        self._stop = None
        self._thread = None

    def __enter__(self):
        import threading
        self._stop = threading.Event()
        t0 = time.time()

        def _run():
            while not self._stop.wait(self._interval):
                try:
                    print(f"[JZL-{TAG}]     ⏳ {self._fn()}｜已用 {_fmt_secs(time.time() - t0)}",
                          flush=True)
                except Exception:
                    return

        self._thread = threading.Thread(target=_run, name="jzl-swap-heartbeat", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=1.0)
        except Exception:
            pass
        return False


def _plan_line(label, steps, n_win):
    """一行采样计划。

    步数一律取**实际 sigmas 条数**（不是面板上的「步数」）：降噪 < 1 时两者本来就不一样，
    二采又常常用显式 sigmas —— 打印面板值会让人以为「设 3 步却跑了 8 步」。
    """
    steps, n_win = max(1, int(steps)), max(1, int(n_win))
    if n_win == 1:
        print(f"[JZL-{TAG}]   {label}计划：{steps} 步 × 1 个窗口 = {steps} 次模型前向", flush=True)
        return
    print(f"[JZL-{TAG}]   {label}计划：{steps} 步 × {n_win} 个窗口 = {steps * n_win} 次模型前向；"
          f"进度条 {steps * n_win} 格（1 格 = 1 个窗口，每步按顺序算全部 {n_win} 个窗口）",
          flush=True)


# ═══════════════════════════════════════════════════════════════
#  节点配置（manager_settings JSON）——所有可调项都收进面板，节点表面只留必需口
# ═══════════════════════════════════════════════════════════════

SEGMENT_SECONDS_OPTIONS = list(_SEGMENT_SECONDS_OPTIONS)

_SWAP_DEFAULTS = {
    "gen_params": {
        "segment": "固定5秒",
        "aspect_ratio": "跟随源片（推荐）",
        "megapixels": 1.0,
        # 参考图只用「参考图放大」这一个旋钮（= 「JZL - 🎬 MiniMax H3 参考编码」的 ref_scale，
        # 面积倍率：参考图最终像素面积 = 生成画布面积 × 倍率）。不另设 max/短边上限那套。
        "scale_factor": 1.0,
        # 二采放大：默认按「面积倍数」，宽高比永远跟随一采 → 不会把竖屏压成横屏。
        # 2.0 = 二采像素面积是一采的 2 倍（参考工作流就是 0.5MP → 1.0MP）。
        "upscale_scale": 2.0,
        "second_mode": "放大倍数",        # 放大倍数（面积倍率，默认 2.0）| 目标分辨率
        "second_width": 1280,
        "second_height": 704,
        # 声音来源：使用原声（默认，原声只用于切片落盘）| 生成音频（解码模型音轨）
        "audio_mode": "使用原声",
        # 编码设置（都相对「生成分辨率」，1.0 = 与生成画布同尺寸，边长 32 对齐）：
        #   video_shrink  ：送进 VAE 的参考视频编码画布比例（<1 更快）
        #   vision_shrink ：送进文本编码器(Qwen) 的参考帧比例（只影响文本编码速度）
        "video_shrink": 1.0,
        "vision_shrink": 1.0,
    },
    "sample_decode": {
        "sample_mode": "常规采样",
        "sampler": "res_multistep", "scheduler": "simple",
        "steps": 5, "cfg": 1.0, "denoise": 1.0,
        "seed": 0, "seed_mode": "randomize",
        "decode_cleanup": "不做清理",
        "selflift": {"steps": 8, "transition_step": 6, "lowres_scale": 0.5, "rho": 0.6,
                     "w_min": 0.5, "w_max": 1.0, "upscaler_model": "none",
                     "upscaler_unload": True, "highres_tiling": False},
        # 分段衔接：重叠 22 帧 = 上一段结尾的 22 帧铺进本段开头（并在成片里丢掉）。
        # 没有「过渡帧数」——那是为了迁就「落盘必须 17k+5」造出来的，参考工作流没有这个概念。
        # 分段衔接：重叠 22 帧（窗口之间的重叠区，按斜坡混合）
        "seam": {"enabled": True, "window": 22},
        # 二次采样：默认开，用参考工作流的显式 sigmas（几乎重跑整条 schedule，才能真正提清晰度）。
        # 想用「步数 + 降噪」就切到「调度器」模式，参数全部由面板控制。
        "second": {"enabled": True, "sampler": "euler", "scheduler": "simple",
                   "sigmas_mode": "custom",
                   "custom_sigmas": "0.909375, 0.725, 0.421875, 0.0000",
                   "steps": 3, "denoise": 0.30, "precision": "fp32",
                   "upscaler_model": "", "device": "cuda", "enable_chunking": True,
                   "tiling": "关闭", "tiling_tiles": 2, "tiling_overlap": 128},
    },
    "save": {},
    # 📝 提示词预设：LLM 增强 + 提示词来源
    "prompt_cfg": {
        "use_llm": False,                  # 仅由 source 推导（系统默认→False，其余→True），面板不再提供开关
        "builtin": "人物替换",             # 内置skill 分支用哪一个（人物替换/动物替换/非活物替换/多人替换）
        "source": "系统默认",              # 系统默认 | 内置skill | 自定义skill
        "skill": "",                       # 自定义 skill 文件名（用户选的目录里的某个 .md/.txt）
        "skill_dir": "",                   # 自定义 skill 目录（「📂 选择 skill 文件目录」选的绝对路径）
        # 后端默认必须与面板显示一致（面板缺值时显示「本地模型」）：
        # 若这里默认在线API，就会出现「界面选的是 CLIP，跑起来却请求 API 报 401」
        "llm_backend": "本地模型 [local]",   # 本地模型 [local] | 在线API [api]
        # 本地文本生成 = 官方「Load CLIP」+「Generate Text」（CLIP 文本编码器，不是 gguf）
        "llm": {"clip_name": "", "clip_type": "qwen_image", "max_length": 2048,
                "temperature": 0.7, "top_k": 64, "top_p": 0.95, "min_p": 0.05,
                "repetition_penalty": 1.05, "presence_penalty": 0.0, "seed": 0,
                "thinking": False, "use_default_template": True},
        "api": {"provider": "OpenAI 兼容 (OpenAI/DeepSeek/Qwen/GLM/Kimi/Ollama/vLLM/LM Studio)",
                "model": "deepseek-flash", "api_key": "", "base_url": "https://api.deepseek.com",
                "temperature": 0.6, "max_tokens": 81920, "thinking": "disabled"},
    },
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


LLM_BACKEND_LOCAL = "本地模型 [local]"
LLM_BACKEND_API = "在线API [api]"


def _resolve_llm_backend(pc):
    """判定 LLM 后端：本地 CLIP 还是在线 API。

    ⚠️ 这里绝对不能有「缺值时默认走 API」的行为：面板在 llm_backend 缺失时显示的是
    **本地模型**，若后端默认成在线 API，就会出现「界面选的是 CLIP，跑起来却去请求
    API 并报 401」这种界面与实际不一致的错。
    所以只有明确写了 api / 在线 才走 API，其余（缺值 / 空串 / 脏值 / 老值 local）一律走本地
    —— 本地不需要任何凭据，绝不会凭空报鉴权错误。
    """
    raw = str((pc or {}).get("llm_backend") or "").strip().lower()
    if "api" in raw or "在线" in raw:
        return LLM_BACKEND_API
    return LLM_BACKEND_LOCAL


def _is_api_backend(pc):
    return _resolve_llm_backend(pc) == LLM_BACKEND_API


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
            for grp in ("gen_params", "sample_decode", "save", "prompt_cfg"):
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
        "upscale_scale": _cfg_num(gp, "upscale_scale", 2.0, 1.0, 4.0),
        "second_mode": _cfg_opt(gp, "second_mode", "放大倍数", ("目标分辨率", "放大倍数")),
        "second_width": int(_cfg_num(gp, "second_width", 1280, 32, 4096)),
        "second_height": int(_cfg_num(gp, "second_height", 704, 32, 4096)),
        "audio_mode": _cfg_opt(gp, "audio_mode", "使用原声", ("使用原声", "生成音频")),
        # 编码设置：相对「生成分辨率」的比例（1.0 = 同尺寸）
        "video_shrink": _cfg_num(gp, "video_shrink", 1.0, 0.2, 1.0),
        "vision_shrink": _cfg_num(gp, "vision_shrink", 1.0, 0.2, 1.0),
    }
    # 只剩两个旋钮：窗口长度（每段时长）与重叠帧数
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
        "seed_mode": str(sd.get("seed_mode") or "randomize"),
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
            "window": int(_seam_resolve_window(_cfg_num(seam, "window", SEAM_DEFAULT_WINDOW))),
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
    # 落盘固定为「逐段落盘」（长片必须，否则整片解码爆内存）；
    # 合并交给下游「XB-BOX - 🎬 视频批量拼接」，所以这里不再有保存模式开关。
    del sv

    # 📝 提示词预设：LLM 开关 + 来源 + 后端配置（结构与导演台的 llm/api 同构）
    pc = sd.get("prompt_cfg") or cfg.get("prompt_cfg") or {}
    # 「LLM 增强」不再单独开关：来源 = 系统默认 → 纯模板（不调 LLM）；内置skill / 自定义skill → 调 LLM。
    # 老工作流里的 use_llm 仍接受（仅在来源=系统默认却显式开了 LLM 的极端旧配置下生效），
    # 但正常路径完全由 source 决定。
    # 来源现在是两个分支（内置skill / 自定义skill）；老工作流的「内置skill·动物」等值映射回内置名
    _raw_src = str(pc.get("source") or "系统默认")
    _builtin = str(pc.get("builtin") or "")
    if _raw_src in LEGACY_SOURCE_ALIAS:
        _builtin = LEGACY_SOURCE_ALIAS[_raw_src]
        _raw_src = "内置skill"
    source = _cfg_opt({"source": _raw_src}, "source", "系统默认", PROMPT_SOURCES)
    # 内置 skill 可以是四档之一，也可以是用户「📥 导入Skill文件」放到内置目录里的文件名
    if not _builtin_skill_file(_builtin):
        _builtin = DEFAULT_BUILTIN_SKILL
    use_llm = (source != "系统默认")
    api = pc.get("api") or {}
    llm = pc.get("llm") or {}
    cfg["prompt_cfg"] = {
        "use_llm": use_llm,
        "source": source,
        "builtin": _builtin,
        "skill": str(pc.get("skill") or ""),
        "skill_dir": str(pc.get("skill_dir") or ""),
        "llm_backend": _resolve_llm_backend(pc),
        "llm": {
            "clip_name": str(llm.get("clip_name") or ""),
            "clip_type": str(llm.get("clip_type") or "qwen_image"),
            "max_length": int(_cfg_num(llm, "max_length", 2048, 1, 32768)),
            "temperature": _cfg_num(llm, "temperature", 0.7, 0.01, 2.0),
            "top_k": int(_cfg_num(llm, "top_k", 64, 0, 1000)),
            "top_p": _cfg_num(llm, "top_p", 0.95, 0.0, 1.0),
            "min_p": _cfg_num(llm, "min_p", 0.05, 0.0, 1.0),
            "repetition_penalty": _cfg_num(llm, "repetition_penalty", 1.05, 0.0, 5.0),
            "presence_penalty": _cfg_num(llm, "presence_penalty", 0.0, 0.0, 5.0),
            "seed": int(_cfg_num(llm, "seed", 0, 0, 0xFFFFFFFFFFFF)),
            "thinking": _cfg_bool(llm, "thinking", False),
            "use_default_template": _cfg_bool(llm, "use_default_template", True),
        },
        "api": {
            "provider": str(api.get("provider") or ""),
            "model": str(api.get("model") or ""),
            "api_key": str(api.get("api_key") or ""),
            "base_url": str(api.get("base_url") or ""),
            "temperature": _cfg_num(api, "temperature", 0.6, 0.0, 2.0),
            "max_tokens": int(_cfg_num(api, "max_tokens", 81920, 1, 262144)),
            "thinking": str(api.get("thinking") or "disabled"),
        },
    }
    return cfg


# ═══════════════════════════════════════════════════════════════
#  节点
# ═══════════════════════════════════════════════════════════════

def _bar_enabled():
    try:
        import comfy.utils
        return bool(comfy.utils.PROGRESS_BAR_ENABLED)
    except Exception:
        return False


def _ram_gb():
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        free, total = torch.cuda.mem_get_info()
        return (total - free) / float(2 ** 30), total / float(2 ** 30)
    except Exception:
        return None


def _free_before_sampling(tag=TAG):
    """采样前把 VAE / 文本编码器退回内存，把整张卡的显存留给 UNET。

    长片窗口采样时，32B 的文本编码器（int4 也有十几 GB）如果还驻留，
    就会和 UNET 抢显存 → 反复换出换入，表现就是「一步几十秒还卡」。这里只是
    把「当前不需要的模型」卸掉，UNET 会在采样开始时正常加载。
    """
    try:
        import comfy.model_management as mm
    except Exception:
        return
    before = _ram_gb()
    try:
        mm.unload_all_models()
        mm.soft_empty_cache()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
    except Exception as e:
        print(f"[JZL-{tag}] ⚠️ 释放显存失败（{type(e).__name__}: {e}）", flush=True)
    after = _ram_gb()
    if before and after:
        print(f"[JZL-{tag}] 采样前释放显存：{before[0]:.1f} → {after[0]:.1f} GB"
              f"（共 {after[1]:.1f} GB，给 UNET 留出 {after[1] - after[0]:.1f} GB）", flush=True)
    elif after:
        print(f"[JZL-{tag}] 采样前已用 {after[0]:.1f}/{after[1]:.1f} GB", flush=True)


def _log_peak(tag=TAG, label="采样峰值"):
    """打印这一步的显存占用：torch 峰值 + 整张卡已用（判断会不会 OOM 看后者）。"""
    try:
        import torch
        if not torch.cuda.is_available():
            return
        peak = torch.cuda.max_memory_allocated() / float(2 ** 30)
        torch.cuda.reset_peak_memory_stats()
        now = _ram_gb()
        tail = f"｜卡上已用 {now[0]:.1f}/{now[1]:.1f} GB" if now else ""
        print(f"[JZL-{tag}] {label}显存：torch 峰值 {peak:.1f} GB{tail}", flush=True)
    except Exception:
        pass


def _crop_audio_frames(audio, keep_frames, fps=VIDEO_FPS):
    """把一段音频裁到指定的帧数（用于非「保留原声」时按解码块对齐）。"""
    if not isinstance(audio, dict) or audio.get("waveform") is None:
        return None
    sr = int(audio.get("sample_rate", 32000) or 32000)
    wf = audio["waveform"]
    n = int(round(int(keep_frames) / float(fps) * sr))
    n = max(1, min(n, int(wf.shape[-1])))
    return {"waveform": wf[..., :n], "sample_rate": sr}


def _decode_chunk(vae, audio_vae, samples, chunk, sample_decode=None, skip_audio=True):
    """解码一个「解码块」：切 latent → 解码 → 丢掉块首的上下文帧。

    块首多解了 34 帧（2 个 17 帧组）前文，是因为 VAE 时序因果：从中间直接解开头会灰掉。
    """
    from comfy_extras.nodes_lt import LTXVConcatAVLatent

    video_latent, audio_latent = _split_av_latent(samples)
    v0, v1 = chunk["video"]
    a0, a1 = chunk["audio"]
    v_part = dict(video_latent)
    v_part["samples"] = video_latent["samples"][:, :, int(v0):int(v1)]
    a_part = dict(audio_latent)
    a_part["samples"] = audio_latent["samples"][..., int(a0):int(a1)]
    part = LTXVConcatAVLatent.execute(v_part, a_part).args[0]
    image, audio = _decode_av(vae, audio_vae, part, sample_decode, tag=TAG,
                             skip_audio=skip_audio)
    if image is None:
        raise RuntimeError("VAE 解码返回空画面（检查「视觉VAE」是否接对、是否与模型匹配）")
    drop = int(chunk["drop_frames"])
    keep = int(chunk["keep_frames"])
    if drop:
        image = image[drop:]
    if audio is not None and audio.get("waveform") is not None:
        audio = _crop_audio_frames(audio, image.shape[0])
        if drop:
            sr = int(audio.get("sample_rate", 32000))
            wf = audio["waveform"]
            x0 = int(round(drop / float(VIDEO_FPS) * sr))
            audio = dict(audio)
            audio["waveform"] = wf[..., x0:]
    if image.shape[0] > keep:
        image = image[:keep]
    return image, audio


def _second_pass_windowed(model, samples, window_conds, windows, total_aligned,
                          sec_w, sec_h, gp, second_cfg, sample_decode, seed):
    """二采：把整片 latent 放大 → 用同一套窗口再跑一次低噪采样。

    窗口的 token 位置只取决于帧数，所以画布放大后窗口计划完全复用，只需换一套
    按新画布编码的参考条件。
    """
    import re as _re

    import torch as _torch

    from comfy_extras.nodes_lt import LTXVConcatAVLatent

    video_latent, audio_latent = _split_av_latent(samples)

    if str(gp.get("second_mode") or "放大倍数") == "目标分辨率":
        mode_val = {"mode": "target dimensions", "width": int(sec_w), "height": int(sec_h)}
    else:
        mode_val = {"mode": "scale by multiplier", "scale": float(_second_area_scale(gp))}
    # 2D / 3D 架构由 _run_upscaler 按权重键名自动分派（*2d_conv* → 2D 网络，*3d_conv* → 3D 网络）
    _t = time.time()
    up, model_file = _run_upscaler(video_latent, second_cfg, mode_val, tag=TAG, align=32)
    try:
        tv = _seam_video_tensor(up)
        pw, ph = int(tv.shape[-1]) * 16, int(tv.shape[-2]) * 16
    except Exception:
        pw, ph = sec_w, sec_h
    print(f"[JZL-{TAG}]   二采放大：{os.path.basename(model_file or '内置模型')} "
          f"{sec_w}x{sec_h} → {pw}x{ph}｜{time.time() - _t:.1f} 秒", flush=True)
    merged = LTXVConcatAVLatent.execute(up, audio_latent).args[0]

    ws2 = _win.WindowSet(windows, window_conds, total_aligned)
    cond = _win.windowed_conditioning(ws2)
    cfg = float(sample_decode.get("cfg", 1.0) or 1.0)
    # 注意：Concat AV Latent 的输出本身就是 LATENT dict（{"samples": NestedTensor}），
    # 直接交给 sample_windowed；再包一层 {"samples": dict} 会让 prepare_noise 拿 dict 当张量用
    # → AttributeError: 'dict' object has no attribute 'is_nested'
    if str(second_cfg.get("sigmas_mode") or "scheduler") == "custom":
        text = second_cfg.get("custom_sigmas") or "0.909375, 0.725, 0.421875, 0.0000"
        vals = [float(x) for x in _re.findall(r"[-+]?(?:\d*\.*\d+)", text or "")]
        if len(vals) < 2:
            raise ValueError("自定义 Sigmas 无效（至少 2 个，末位通常为 0）")
        sampler = second_cfg.get("sampler") or "euler"
        print(f"[JZL-{TAG}]   二采：{sampler}｜自定义 sigmas {text}｜{pw}x{ph}", flush=True)
        _plan_line("二采", len(vals) - 1, len(windows))
        _t = time.time()
        out = _win.sample_windowed(model, cond, merged, seed,
                                   sampler_name=sampler, cfg=cfg,
                                   sigmas=_torch.FloatTensor(vals),
                                   disable_pbar=not _bar_enabled(), tag=TAG)
    else:
        _steps2 = int(second_cfg.get("steps", 3) or 3)
        _den2 = float(second_cfg.get("denoise", 0.3) or 0.3)
        sig2, _sampler2 = _win.make_sigmas(
            model, second_cfg.get("sampler") or "euler",
            second_cfg.get("scheduler") or "simple", _steps2, _den2)
        print(f"[JZL-{TAG}]   二采：{_sampler2} {_steps2} 步｜"
              f"{second_cfg.get('scheduler') or 'simple'}｜"
              f"降噪 {_den2:.2f}｜{pw}x{ph}", flush=True)
        _plan_line("二采", max(1, int(sig2.numel()) - 1), len(windows))
        _t = time.time()
        out = _win.sample_windowed(model, cond, merged, seed,
                                   steps=_steps2, cfg=cfg,
                                   sampler_name=_sampler2,
                                   scheduler=second_cfg.get("scheduler") or "simple",
                                   denoise=_den2, sigmas=sig2,
                                   disable_pbar=not _bar_enabled(), tag=TAG)
    print(f"[JZL-{TAG}]   二采完成：{time.time() - _t:.1f} 秒", flush=True)
    return out


class JZL_MiniMaxCharacterSwap(io.ComfyNode):
    """长视频人物替换：整片一次采样、逐窗口预测后在重叠区混合；保留原声。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_MiniMaxCharacterSwap",
            display_name="JZL - 🎭 MiniMax-H3人物替换",
            category="JZL/MiniMax",
            description=("把源视频里的人换成参考图里的人（长视频自动分窗口）：整片只采样一次，"
                         "每一步内部逐窗口用「源片同名段」当 <Video 1>、定妆图当 <Picture 1> 各预测一次，"
                         "再在重叠区按斜坡混合 ⇒ 成片时长 = 源片时长、段界无缝、不累积漂移。"
                         "长片自动分块解码逐段落盘；默认保留原声。"),
            inputs=[
                io.Model.Input("model", display_name="主模型"),
                io.Clip.Input("clip", display_name="CLIP"),
                io.Vae.Input("vae", display_name="视觉VAE"),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True,
                    tooltip="「🎨 视频参数设置 → 声音来源」选「生成音频」时必须接，用来解码模型音轨；"
                            "选「使用原声」时完全不使用它（latent 音频不解码），可以留空，"
                            "省一个音频 VAE 的加载与显存。"),
                io.Image.Input("source_video",
                    tooltip="源视频帧（IMAGE 批次，按 24fps 解释）。用「加载视频 / GetVideoComponents」接入；"
                            "源片不是 24fps 请先转码，否则时长会对不上"),
                io.Audio.Input("source_audio", optional=True,
                    tooltip="源视频原声（与源视频同一条时间轴）。「声音来源=使用原声」时用它按帧号切片落盘；"
                            "无论哪种模式，源音频都**不参与参考编码**"),
                io.Autogrow.Input("ref_images", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        io.Image.Input("ref_image", tooltip="新角色定妆图（<Picture N>）。建议正面清晰、单人、同画幅"),
                        prefix="ref_image_", min=0, max=9)),
                io.String.Input("story_name", default=DEFAULT_OUT_PREFIX, display_name="分段输出",
                    tooltip="文件名前缀，格式「文件夹/视频名称」，例如 视频/人物替换 → "
                            "落盘到 output/视频/ 下，文件名以 人物替换 开头。"
                            "（在「🎬 生成视频管理」面板里修改）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True,
                    socketless=True, default="",
                    tooltip="本节点完整配置 JSON（由 🎨 视频参数设置 / 🎭 采样解码设置 / 🎬 生成视频管理 写入），随工作流保存"),
                # 提示词固定放在最底部（节点表面从上到下：模型类端口 → 素材端口 → 按钮区 → 提示词）
                io.String.Input("video_desc", multiline=True, default="",
                    display_name="视频描述",
                    tooltip="这段源视频里发生了什么：人物/动作/场景/镜头/光线/节奏。\n"
                            "开启「📝 提示词预设 → 启用 LLM 增强提示词」时，它会连同「图片描述」一起交给 "
                            "LLM 生成完整 H3 提示词；关闭时它会被填进内置提示词的 detailed_description 段。"),
                io.String.Input("image_desc", multiline=True, default="",
                    display_name="图片描述",
                    tooltip="参考图（定妆图）里的人长什么样：发型/脸型/服装/配饰/配色/年龄气质。\n"
                            "越具体越像。同样只在「LLM 增强」开启或被填进模板时使用。"),
            ],
            outputs=[
                io.Video.Output(
                    "videos", display_name="批量视频", is_output_list=True,
                    tooltip="本次落盘的每一段（VIDEO 指向文件，不占内存）。"
                            "直接接「XB-BOX - 🎬 视频批量拼接」勾选「拼接保存」即可无损拼成一整条。"),
            ],
            is_output_node=True,
        )

    @classmethod
    def fingerprint_inputs(cls, manager_settings="", video_desc="", image_desc="", story_name="", **kwargs):
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
    def execute(cls, source_video, video_desc="", image_desc="", story_name=DEFAULT_OUT_PREFIX,
                manager_settings="", ref_images=None, source_audio=None,
                model=None, clip=None, vae=None, audio_vae=None) -> io.NodeOutput:
        """整片一次采样：按窗口切 latent，逐窗口预测后在重叠区按斜坡混合。

        不再「一段生成完再拼下一段」—— 那会让每段都重新走一遍 VAE / 文本编码器 / UNET
        （模型反复换出换入 → 越跑越慢），并让身份与色温逐段漂移。
        """

        cfg = _parse_swap_settings(manager_settings)
        gp, sd = cfg["gen_params"], cfg["sample_decode"]
        # 📝 提示词预设：系统默认模板 / 内置skill / 自定义skill（后两者自动走 LLM）
        prompt = _build_swap_prompt(cfg.get("prompt_cfg") or {}, video_desc, image_desc)
        # 「📝 预览提示词」：节点一开始就把最终提示词推给面板（等同一根串联的「预览任意」）
        _emit_ui(jzl_swap_prompt=[prompt],
                 jzl_swap_prompt_src=[_prompt_cfg_label(cfg.get("prompt_cfg") or {})])
        seam_cfg, second_cfg = sd["seam"], sd["second"]
        aspect_ratio = gp["aspect_ratio"]
        megapixels = float(gp["megapixels"])
        ref_scale = float(gp["scale_factor"])
        seed, seed_mode = sd["seed"], sd["seed_mode"]

        frames_all = _as_4d(source_video)
        if frames_all is None or int(frames_all.shape[0]) < 5:
            raise ValueError("「源视频」为空或不足 5 帧：请把视频（IMAGE 批次，24fps 解释）接进来")
        if model is None or clip is None or vae is None:
            raise ValueError("必须连接「主模型 + CLIP + 视觉VAE」才能生成")
        source_audio = _as_audio_dict(source_audio)
        # 「🎨 视频参数设置 → 声音来源」显式选择，不再靠「接没接原声」猜：
        #   使用原声 → 源音频只按帧号切片落盘；不进参考编码，latent 音频不解码，音频 VAE 不传。
        #   生成音频 → 解码模型自己生成的音轨（需要「音频VAE」）；源音频依旧不进参考编码。
        use_model_audio = (str(gp.get("audio_mode") or "使用原声") == "生成音频")
        if use_model_audio and audio_vae is None:
            _log_once(f"[JZL-{TAG}] ⚠️ 选了「生成音频」但没接「音频VAE」→ 本次回退："
                      f"能拿到源片原声就用原声，否则成片无声")
            use_model_audio = False
        keep_original_audio = source_audio is not None and not use_model_audio
        audio_vae_eff = audio_vae if use_model_audio else None
        skip_model_audio = not use_model_audio
        if use_model_audio:
            print(f"[JZL-{TAG}] 音频：生成音频（解码模型音轨；源音频不参与参考编码，只作画面参考）",
                  flush=True)
        elif keep_original_audio:
            print(f"[JZL-{TAG}] 音频：使用原声（只按帧号切片落盘）"
                  f"｜源音频不参与参考编码｜latent 音频不解码｜音频VAE 不使用", flush=True)
        else:
            _log_once(f"[JZL-{TAG}] ⚠️ 没接「源视频原声」→ 成片将没有声音"
                      f"（把 AUDIO 接上可保留原音，或在「视频参数设置 → 声音来源」选「生成音频」）")

        total_src = int(frames_all.shape[0])
        src_h, src_w = int(frames_all.shape[1]), int(frames_all.shape[2])
        frames_in_shape = tuple(frames_all.shape)

        # 源片先落成「一份可切片的缓存」，后面所有窗口都从它切
        frames_src, src_is_u8 = _cache_source(frames_all, 8.0)
        if src_is_u8:
            frames_all = None

        # 模型时间轴固定按 24fps 解释帧数；源片不是 24fps 就按「原声真实时长」反推帧率并升帧，
        # 否则 12fps 的 10 秒视频（120 帧）会被当成 5 秒，和 10 秒的原声完全对不上。
        src_fps, audio_dur = _detect_source_fps(total_src, source_audio)
        if abs(src_fps - float(VIDEO_FPS)) > 0.01:
            n_before = total_src
            frames_src = _resample_to_24(frames_src, src_fps)
            total_src = int(frames_src.shape[0])
            print(f"[JZL-{TAG}] 源片帧率 {src_fps:g}fps"
                  f"（{n_before} 帧 / 原声 {audio_dur:.2f} 秒）→ 已升帧到 {VIDEO_FPS}fps："
                  f"{total_src} 帧 = {total_src / float(VIDEO_FPS):.2f} 秒（时长不变）", flush=True)
        elif audio_dur is not None:
            print(f"[JZL-{TAG}] 源片帧率：{total_src} 帧 / 原声 {audio_dur:.2f} 秒"
                  f" ≈ {total_src / max(audio_dur, 1e-6):.1f}fps → 按 {VIDEO_FPS}fps 处理", flush=True)

        if str(aspect_ratio).startswith("跟随源片"):
            width, height = _canvas_for_ratio(src_w, src_h, megapixels)
        else:
            width, height = _resolve_gen_size(aspect_ratio, megapixels)

        # 窗口：每段时长 = 单个窗口一次生成的长度；整片按 17 帧网格切窗口
        dur_lo, _dur_hi, _dur_desc = _parse_duration_spec(gp["segment"])
        window_frames = int(_resolve_length(dur_lo))
        overlap = _resolve_overlap(int(seam_cfg["window"]), window_frames, True)
        total_aligned = align_frame_count(total_src)
        windows = _win.plan_windows(total_aligned, window_frames, overlap)

        frame_base = 0          # 源片切片起点（整片跑时恒为 0）

        ref_imgs = []
        for img in (ref_images or {}).values():
            if img is None:
                continue
            ref_imgs.append(_as_4d(img)[:1])
        if not ref_imgs:
            print(f"[JZL-{TAG}] ⚠️ 没有接任何定妆图「ref_image_N」：只能靠提示词猜身份，换人效果会很差")

        # 本节点整片是「联合窗口采样」：每一步切成若干窗口分别预测再混合。
        # SelfLift 的渐进分辨率是整段一次采样（内部改用低分辨率模型跑前缀），插不进窗口混合，
        # 所以这里直接按常规采样跑，并且明确告警（面板已不再提供 SelfLift 选项）。
        if str(sd.get("sample_mode") or SAMPLE_MODE_NORMAL) == SAMPLE_MODE_SELFLIFT:
            print(f"[JZL-{TAG}] ⚠️「采样分类=SelfLift采样」不适用于本节点（联合窗口采样必须整段同一分辨率）"
                  f" → 本次按常规采样执行，步数用「采样步数」那一项", flush=True)
            sd = dict(sd)
            sd["sample_mode"] = SAMPLE_MODE_NORMAL

        use_second = bool(second_cfg["enabled"])
        sec_w, sec_h = _second_canvas(gp, width, height)
        if use_second and sec_w * sec_h < width * height:
            print(f"[JZL-{TAG}] ⚠️ 二采画布 {sec_w}×{sec_h} 比一采 {width}×{height} 还小"
                  f"（{sec_w * sec_h / float(width * height):.2f}x 面积）→ 二采只会更糊，本次跳过")
            use_second = False

        out_root = os.path.abspath(folder_paths.get_output_directory())
        out_dir, safe_story = _resolve_out_prefix(
            story_name, out_root, getattr(cls, "OUT_FALLBACK", DEFAULT_OUT_PREFIX.split("/")[-1]))
        os.makedirs(out_dir, exist_ok=True)
        batch = _next_batch(out_dir, safe_story)
        counter0 = _next_counter(out_dir, f"{safe_story}_{batch}次生成分段")

        _sep_line()
        print(f"[JZL-{TAG}]   源片输入：帧数 {frames_in_shape[0]} × 宽 {src_w} × 高 {src_h}"
              f"（{total_src} 帧 = {total_src / float(VIDEO_FPS):.2f} 秒 @{VIDEO_FPS}fps）", flush=True)
        print(f"[JZL-{TAG}]   成片参数：宽 {width} ｜ 高 {height}｜单段上限 {window_frames} 帧"
              f"｜重叠 {overlap} 帧", flush=True)
        # 视频帧数按 24fps 解释；用「原声时长」做交叉校验 —— 两者对不上就说明接进来的视频
        # 不是你以为的那条（帧率被改写、多条视频被拼接、或文件本身更长）
        if keep_original_audio:
            _wf = source_audio.get("waveform") if isinstance(source_audio, dict) else None
            _sr = int((source_audio or {}).get("sample_rate", 32000) or 32000)
            if _wf is not None:
                _aud_s = float(_wf.shape[-1]) / float(_sr)
                _vid_s = total_src / float(VIDEO_FPS)
                if abs(_aud_s - _vid_s) > 0.5:
                    print(f"[JZL-{TAG}] ⚠️ 视频与原声长度不一致：视频 {_vid_s:.2f} 秒"
                          f"（{total_src} 帧 @24fps），原声 {_aud_s:.2f} 秒"
                          f"（差 {abs(_aud_s - _vid_s):.2f} 秒）。\n"
                          f"    常见原因：①「加载视频」的 fps 设错（把 30fps 视频当 24fps 载入会变长/变短）；"
                          f"② 接进来的是多条视频拼起来的；③ 选错文件。"
                          f"建议先确认加载节点显示的帧数/时长。", flush=True)
        print(f"[JZL-{TAG}]   分段信息：{_win.describe(windows, total_aligned)}"
              f"（整片对齐到 {total_aligned} 帧，成片再裁回 {total_src} 帧）", flush=True)
        print(f"[JZL-{TAG}]   采样信息：整段一次采样跑完，逐窗口预测后在重叠区按斜坡混合；"
              f"窗口之间不传递已解码画面 → 不累积漂移", flush=True)
        print(f"[JZL-{TAG}]   参考信息：{len(ref_imgs)} 张定妆图（放大 {ref_scale:.1f}×，面积倍率）"
              f"｜音频：{('生成音频（模型音轨）' if use_model_audio else ('使用原声' if keep_original_audio else '无（没接原声/音频VAE）'))}"
              f"｜二采：{(str(sec_w) + '×' + str(sec_h)) if use_second else '关闭'}", flush=True)
        print(f"[JZL-{TAG}]   落盘信息：{out_dir}{os.sep}{safe_story}_…mp4"
              f"（「分段输出」= 文件夹/{safe_story}）", flush=True)
        # 配置总览：把用户最关心的三项（LLM 增强 / 采样步数 / 分段数）放在最前面，
        # 出问题时一眼能看出这次到底按什么配置跑的。
        print(f"[JZL-{TAG}]   本次配置：LLM增强={_prompt_cfg_label(cfg.get('prompt_cfg') or {})}"
              f"｜采样 {sd.get('sampler') or 'res_multistep'} "
              f"{int(sd.get('steps', 4) or 4)} 步"
              f"｜分段 {len(windows)} 段（单段上限 {window_frames} 帧）"
              f"｜画布 {width}×{height}｜二采 {'开' if use_second else '关'}"
              f"｜声音 {'生成音频' if use_model_audio else ('使用原声' if keep_original_audio else '无')}",
              flush=True)

        ref_names = {"image": [f"定妆图{k + 1}" for k in range(len(ref_imgs))],
                     "video": ["源片"], "audio": ["源片原声"]}
        sample_decode = dict(sd)
        sample_decode.pop("seam", None)

        # 「🎨 编码设置」：三个比例都相对生成分辨率（1.0 = 与生成画布同尺寸，边长 32 对齐）
        video_shrink = float(gp.get("video_shrink", 1.0) or 1.0)
        vision_shrink = float(gp.get("vision_shrink", 1.0) or 1.0)
        if abs(video_shrink - 1.0) > 1e-6 or abs(vision_shrink - 1.0) > 1e-6:
            print(f"[JZL-{TAG}]   编码设置：参考图放大 {ref_scale:.2f}×｜输入视频 ×{video_shrink:.2f}"
                  f"｜文本视觉 ×{vision_shrink:.2f}（相对生成分辨率，不改生成画布）", flush=True)

        # 画布去重：二采画布与一采相同（面积倍数=1.0 / 目标分辨率=一采）时不再重复编码一遍
        canvases = [(width, height)]
        if use_second and (sec_w, sec_h) not in canvases:
            canvases.append((sec_w, sec_h))
        elif use_second:
            print(f"[JZL-{TAG}]   二采画布与一采相同（{sec_w}x{sec_h}）→ 复用同一份编码，不重复编码",
                  flush=True)

        # ═══ 阶段 1：VAE 编码全部窗口的参考素材（VAE 只加载这一次）═══
        jobs = {c: [] for c in canvases}
        _enc_t = time.time()
        ref_n_last = 0
        _n_win_total = len(windows) * len(canvases)
        _done_win = 0
        _hdr(f"开始参考素材编码…（共 {_n_win_total} 段，速度取决于硬件能力，请耐心等待！）")
        for cw, ch in canvases:
            for i, window in enumerate(windows):
                src_start = frame_base + window.frame_start
                seg_prompt = _fill_prompt(prompt, i, len(windows), src_start, window.frames)
                clip_frames = _slice_source(frames_src, src_start, window.frames,
                                            src_is_u8, cw, ch)
                ref_n_last = int(clip_frames.shape[0])
                # 源片原声不参与编码：只作为最后落盘的音轨
                _t_one = time.time()
                with _Heartbeat(lambda: f"仍在编码参考素材 第 {_done_win + 1}/{_n_win_total} 段"
                                        f"（{cw}x{ch}｜参考 {ref_n_last} 帧）"):
                    _encode_ref_to_video(
                        clip, vae, audio_vae_eff, seg_prompt, cw, ch, window.frames,
                        ref_images=ref_imgs, ref_videos=[clip_frames],
                        ref_video_audios=[],
                        ref_audios=[], ref_image_size="match", ref_scale=ref_scale,
                        slot_names=ref_names, probe=None, defer=jobs[(cw, ch)],
                    video_shrink=video_shrink, vision_shrink=vision_shrink)
                _done_win += 1
                print(f"[JZL-{TAG}]   参考素材 第 {_done_win}/{_n_win_total} 段编码完成"
                      f"（{cw}x{ch}｜参考 {ref_n_last} 帧）｜耗时 {_fmt_secs(time.time() - _t_one)}",
                      flush=True)
                try:
                    del clip_frames
                except Exception:
                    pass
            _n_win = len(jobs[(cw, ch)])
            print(f"[JZL-{TAG}]   ✅ 参考素材编码完成（{cw}x{ch}）：{_n_win} 个窗口"
                  f"｜本画布 {_fmt_secs(time.time() - _enc_t)}", flush=True)
            _enc_t = time.time()

        # ═══ 阶段 2：文本编码（文本编码器只加载这一次，全部窗口一起做完）═══
        # 32B 文本编码器 int4 也有十几 GB：先把 VAE 等模型卸掉再进阶段 2，
        # 否则 24G 卡上会和它抢显存 → 反复换入换出，表现就是「编码卡住十几分钟」。
        _hdr(f"开始提示词编码…（共 {len(windows) * len(canvases)} 段；"
             f"单段可能几十秒~数分钟，速度取决于硬件能力，请耐心等待！）")
        try:
            import comfy.model_management as _mm
            _mm.unload_all_models()
            _mm.soft_empty_cache()
            import torch as _torch
            if _torch.cuda.is_available():
                _torch.cuda.empty_cache()
            _r = _ram_gb()
            if _r:
                print(f"[JZL-{TAG}]   编码前释放显存：已用 {_r[0]:.1f}/{_r[1]:.1f} GB"
                      f"（给 {len(windows)} 个窗口的文本编码腾空间）", flush=True)
        except Exception as _fe:
            print(f"[JZL-{TAG}]   ⚠️ 文本编码前释放显存失败：{type(_fe).__name__}: {_fe}", flush=True)

        conds = {}
        _t2 = time.time()
        _t_all = _t2
        _n_txt_total = sum(len(jobs[c]) for c in canvases)
        _done_txt = 0
        _txt_times = []
        for cw, ch in canvases:
            got = []
            for _i, j in enumerate(jobs[(cw, ch)], 1):
                _nvis = sum(int(it["data"].shape[0]) for it in j["ref_items"] if it.get("type") == "video")
                _t_one = time.time()
                with _Heartbeat(lambda: f"仍在编码提示词 第 {_done_txt + 1}/{_n_txt_total} 段"
                                        f"（{cw}x{ch}｜视觉 {_nvis} 帧）"):
                    got.append(_cond_from_parts(j["clip"], j["prompt"], j["ref_items"], j["ref_blocks"]))
                _dt = time.time() - _t_one
                _txt_times.append(_dt)
                _done_txt += 1
                _eta = (sum(_txt_times) / len(_txt_times)) * (_n_txt_total - _done_txt)
                print(f"[JZL-{TAG}]   第 {_done_txt}/{_n_txt_total} 段编码完成"
                      f"（{cw}x{ch}｜视觉 {_nvis} 帧）｜耗时 {_fmt_secs(_dt)}"
                      f"｜累计 {_fmt_secs(time.time() - _t_all)}"
                      + (f"｜预计剩余 {_fmt_secs(_eta)}" if _done_txt < _n_txt_total else ""),
                      flush=True)
            conds[(cw, ch)] = got
            jobs[(cw, ch)] = None
        print(f"[JZL-{TAG}]   ✅ 全部编码完成 ｜总耗时 {_fmt_secs(time.time() - _t_all)}"
              f"（{len(windows)} 个窗口 × {len(canvases)} 种画布）", flush=True)
        _enc_t = time.time()

        # 文本编码器（32B int4 ≈ 十几 GB）用完就放：不清掉引用的话它会一直驻留显存
        # （ComfyUI 的 unload_all_models 不管 CLIP），采样时就只剩几 GB 余量
        clip = None
        import gc
        gc.collect()

        # ═══ 阶段 3：一采（整片一次采样）═══
        seed_i = int(_resolve_seed(seed_mode, int(seed or 0), 0))
        print(f"[JZL-{TAG}]   随机种子：{seed_i}（{seed_mode}）", flush=True)
        _emit_ui(jzl_swap_seed=[str(seed_i)])
        ws1 = _win.WindowSet(windows, conds[(width, height)], total_aligned)
        conditioning = _win.windowed_conditioning(ws1)
        latent, _frames = _empty_av_latent(width, height, total_aligned)
        _free_before_sampling()
        _hdr(f"开始采样…（{len(windows)} 段逐段预测后混合；每步要算全部 {len(windows)} 段，"
             f"进度会一格一格走，请耐心等待！）")
        print(f"[JZL-{TAG}]   采样信息：{_first_pass_desc(sample_decode)}"
              f"｜画布 {width}×{height}｜整片 {total_aligned} 帧 / {len(windows)} 段", flush=True)
        _sig1, _sampler1 = _win.make_sigmas(
            model,
            sample_decode.get("sampler") or "res_multistep",
            sample_decode.get("scheduler") or "simple",
            int(sample_decode.get("steps", 4) or 4),
            float(sample_decode.get("denoise", 1.0) or 1.0))
        _steps = max(1, int(_sig1.numel()) - 1)
        _plan_line("一采", _steps, len(windows))
        _t = time.time()
        samples = _win.sample_windowed(
            model, conditioning, latent, seed_i,
            steps=_steps,
            cfg=float(sample_decode.get("cfg", 1.0) or 1.0),
            sampler_name=_sampler1,
            scheduler=sample_decode.get("scheduler") or "simple",
            denoise=float(sample_decode.get("denoise", 1.0) or 1.0),
            sigmas=_sig1,
            disable_pbar=not _bar_enabled(), tag=TAG)
        _cost = time.time() - _t
        _fwd = max(1, _steps * len(windows))
        print(f"[JZL-{TAG}]   ✅ 一采完成：{_fmt_secs(_cost)}"
              f"（{_steps} 步 × {len(windows)} 段 = {_fwd} 次前向，"
              f"平均 {_cost / _fwd:.1f} 秒/段/步）", flush=True)
        _log_peak(label="一采")

        # ═══ 阶段 3b：二采（放大 latent 后按同一套窗口再跑一次）═══
        if use_second:
            try:
                samples = _second_pass_windowed(
                    model, samples, conds[(sec_w, sec_h)], windows, total_aligned,
                    sec_w, sec_h, gp, second_cfg, sample_decode, seed_i)
            except Exception as _se:
                print(f"[JZL-{TAG}] ⚠️ 二采失败，退回一采结果"
                      f"（{type(_se).__name__}: {_se}）", flush=True)

        # ═══ 阶段 4：解码 / 音轨 / 落盘 ═══
        # 长片按「解码块」分组解码：每块解完立刻落盘并释放，峰值内存恒定在一块的量级
        # （整片一次性解成 IMAGE 约 5.9 MB/帧，5 分钟片子要 18 GB 以上）
        chunks = _win.plan_decode_chunks(total_aligned, DECODE_FRAMES_BUDGET)
        grouped = len(chunks) > 1
        out_files = []

        _hdr(f"开始解码…（共 {len(chunks)} 块；速度取决于硬件能力，请耐心等待！）")
        if grouped:
            print(f"[JZL-{TAG}]   解码方式：整片分 {len(chunks)} 块（每块 ≤ {DECODE_FRAMES_BUDGET} 帧，"
                  f"块间带 34 帧上下文）→ 逐块解码落盘，内存不随片长增长", flush=True)
            total_out = 0
            for i, ch in enumerate(chunks):
                _t = time.time()
                n = min(int(ch["keep_frames"]), total_src - int(ch["frame_start"]))
                if n <= 0:
                    continue
                image, chunk_audio = _decode_chunk(vae, audio_vae_eff, samples, ch, sample_decode,
                                                   skip_audio=skip_model_audio)
                image = image[:n]
                if use_model_audio:
                    audio = _crop_audio_frames(chunk_audio, n)
                elif keep_original_audio:
                    audio = _slice_audio(source_audio, int(ch["frame_start"]), n)
                else:
                    audio = None
                path = _save_segment_mp4(image, audio, out_dir, i + 1, story_name=safe_story,
                                         counter=counter0 + i, batch=batch)
                out_files.append(path)
                total_out += n
                print(f"[JZL-{TAG}]   第 {i + 1}/{len(chunks)} 块已落盘：{path}"
                      f"（{n} 帧，源片 {ch['frame_start']}..{ch['frame_start'] + n}）"
                      f"｜耗时 {_fmt_secs(time.time() - _t)}", flush=True)
                del image, audio, chunk_audio
                try:
                    import torch as _tc
                    _tc.cuda.empty_cache()
                except Exception:
                    pass
                gc.collect()
        else:
            _t = time.time()
            with _Heartbeat(lambda: f"仍在解码（{total_src} 帧 @ {width}x{height}）"):
                image, audio = _decode_av(vae, audio_vae_eff, samples, sample_decode, tag=TAG,
                                          skip_audio=skip_model_audio)
            if image is None:
                raise RuntimeError("VAE 解码返回空画面（检查「视觉VAE」是否接对、是否与模型匹配）")
            raw = int(image.shape[0])
            print(f"[JZL-{TAG}]   解码完成：{raw} 帧｜耗时 {_fmt_secs(time.time() - _t)}", flush=True)

            if raw > total_src:
                image = image[:total_src]
                print(f"[JZL-{TAG}]   对齐源片长度：{raw} → {total_src} 帧"
                      f"（补出来的 {raw - total_src} 帧是对齐余量）", flush=True)
            elif raw < total_src:
                print(f"[JZL-{TAG}]   ⚠️ 解码只有 {raw} 帧 < 源片 {total_src} 帧", flush=True)
            total_out = int(image.shape[0])
            offset = 0

            if use_model_audio:
                audio = _slice_audio(audio, offset, total_out)
            elif keep_original_audio:
                audio = _slice_audio(source_audio, offset, total_out)
            else:
                audio = None

            # 分段落盘：按窗口起点切（重叠区归后一段，正好不重不漏）
            bounds = [w.frame_start for w in windows] + [total_out]
            pieces = []
            for i in range(len(windows)):
                a, b = bounds[i], min(bounds[i + 1], total_out)
                if b > a:
                    pieces.append((a, b))
            if len(pieces) > 1:
                for i, (a, b) in enumerate(pieces):
                    piece_audio = None
                    if audio is not None and audio.get("waveform") is not None:
                        piece_audio = _slice_audio(audio, a, b - a)
                    path = _save_segment_mp4(image[a:b], piece_audio, out_dir, i + 1,
                                             story_name=safe_story, counter=counter0 + i, batch=batch)
                    out_files.append(path)
                    print(f"[JZL-{TAG}] 分段 {i + 1}/{len(pieces)} 已落盘：{path}"
                          f"（{b - a} 帧）", flush=True)
            else:
                lbl = f"{batch}次生成成片"
                only = _save_segment_mp4(image, audio, out_dir, 0, story_name=safe_story,
                                         counter=_next_counter(out_dir, f"{safe_story}_{lbl}"),
                                         batch=batch, label=lbl)
                out_files = [only]
                print(f"[JZL-{TAG}] 成片已落盘：{only}", flush=True)

        videos = []
        try:
            from comfy_api.input_impl import VideoFromFile
            for _p in out_files:
                videos.append(VideoFromFile(_p))
        except Exception as _ve:
            print(f"[JZL-{TAG}] ⚠️ 无法把落盘文件包成 VIDEO 输出：{_ve}", flush=True)

        print(f"[JZL-{TAG}] ─────────────────────────────────────────────")
        print(f"[JZL-{TAG}] 成片合计 {total_out} 帧 = {total_out / float(VIDEO_FPS):.2f} 秒"
              f"（源片 {total_src} 帧 = {total_src / float(VIDEO_FPS):.2f} 秒"
              f"，{len(windows)} 个窗口一次采样"
              + (f"｜{len(chunks)} 块解码" if grouped else "") + "）", flush=True)
        if grouped or len(out_files) > 1:
            print(f"[JZL-{TAG}] 已落盘 {len(out_files)} 段：把「分段视频」接到 "
                  f"「XB-BOX - 🎬 视频批量拼接」勾选「拼接保存」即可无损拼成一整条", flush=True)
        return io.NodeOutput(videos, ui={"jzl_swap_prompt": [prompt],
                                         "jzl_swap_seed": [str(seed_i)]})


# ─────────────────────────────────────────────────────────────────────────────
#  🎬 视频编辑（JZL_MiniMaxVideoEdit）
#  与「🎭 人物替换」1:1 同一套实现，唯一区别是**素材怎么进来**：
#    人物替换要接 4 类端口（模型 / 源视频 / 源原声 / 定妆图×9）；
#    视频编辑只要一条「素材总线」—— 内部就是 ref2va 参考总线（解包）那 18 个端口，
#    所以把「JZL - 📥 MiniMax 素材加载」接上来，视频 / 原声 / 图片一次全进。
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_VIDEO_EDIT_OUT_PREFIX = "视频/视频编辑"

# ref2va 参考总线的端口名（与 nodes_ref2va_bus.BUS_KEYS 完全一致；tests 里有对拍用例防漂移）
_BUS_IMAGE_KEYS = tuple(f"ref_image_{i}" for i in range(9))
_BUS_VIDEO_KEYS = tuple(f"ref_video_{i}" for i in range(3))
_BUS_VIDEO_AUDIO_KEYS = tuple(f"ref_video_audio_{i}" for i in range(3))
_BUS_AUDIO_KEYS = tuple(f"ref_audio_{i}" for i in range(3))


def _bus_media(bus):
    """把 ref2va 参考总线拆成「源视频 / 源原声 / 定妆图」三样。

    取第一个非空的 ref_video_N 当要编辑的源片（通常就是「素材加载」的视频1），
    与它同槽位的 ref_video_audio_N 就是源原声；参考图按 1~9 原样交出去（None 由上游跳过）。
    返回 (源视频, 源原声, 参考图 dict, 源视频槽位下标)。
    """
    bus = bus if isinstance(bus, dict) else {}
    ref_images = {k: bus.get(k) for k in _BUS_IMAGE_KEYS}
    source_video, source_audio, slot = None, None, -1
    for i, key in enumerate(_BUS_VIDEO_KEYS):
        video = bus.get(key)
        if video is None:
            continue
        source_video, source_audio, slot = video, bus.get(_BUS_VIDEO_AUDIO_KEYS[i]), i
        break
    return source_video, source_audio, ref_images, slot


class JZL_MiniMaxVideoEdit(JZL_MiniMaxCharacterSwap):
    """视频编辑：整片一次采样 + 逐窗口重叠混合，素材全部从「ref2va 参考总线」进。"""

    # 前缀被清空时的兜底文件名（人物替换 → 视频编辑）
    OUT_FALLBACK = "视频编辑"

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_MiniMaxVideoEdit",
            display_name="JZL - 🎭 MiniMax-H3视频编辑",
            category="JZL/MiniMax",
            description=("与「🎭 MiniMax-H3人物替换」1:1 同一套流程（整片一次采样、逐窗口重叠混合、"
                         "长片分块解码逐段落盘、保留原声），素材改为从一条「素材总线」进："
                         "把「JZL - 📥 MiniMax 素材加载」接上，源视频 / 原声 / 定妆图 一次全部带进来。"),
            inputs=[
                io.Model.Input("model", display_name="主模型"),
                io.Clip.Input("clip", display_name="CLIP"),
                io.Vae.Input("vae", display_name="视觉VAE"),
                io.Vae.Input("audio_vae", display_name="音频VAE", optional=True,
                    tooltip="「🎨 视频参数设置 → 声音来源」选「生成音频」时必须接，用来解码模型音轨；"
                            "选「使用原声」时完全不使用它（latent 音频不解码），可以留空。"),
                io.Custom("JZL_REF2VA_BUS").Input("bus", display_name="素材总线",
                    tooltip="接「JZL - 📥 MiniMax 素材加载」的输出。\n"
                            "视频1 → 要编辑的源视频（同槽位的音轨 → 源原声）；图片 1~9 → 替换/编辑用的定妆图。\n"
                            "（内部已完成 ref2va参考总线（解包），不需要再单独接解包节点）"),
                io.String.Input("story_name", default=DEFAULT_VIDEO_EDIT_OUT_PREFIX, display_name="分段输出",
                    tooltip="文件名前缀，格式「文件夹/视频名称」，例如 视频/视频编辑 → "
                            "落盘到 output/视频/ 下，文件名以 视频编辑 开头。（在「🎬 生成视频管理」面板里修改）"),
                io.String.Input("manager_settings", display_name="节点配置", multiline=True,
                    socketless=True, default="",
                    tooltip="本节点完整配置 JSON（由 🎨 视频参数设置 / 🎭 采样解码设置 / 🎬 生成视频管理 写入），随工作流保存"),
                io.String.Input("video_desc", multiline=True, default="",
                    display_name="视频描述",
                    tooltip="这段源视频里发生了什么：主体/动作/场景/镜头/光线/节奏。\n"
                            "「📝 提示词预设 → 提示词来源」选「内置skill / 自定义skill」时会交给 LLM 写完整提示词；"
                            "选「系统默认」时被填进内置模板的 detailed_description 段。"),
                io.String.Input("image_desc", multiline=True, default="",
                    display_name="图片描述",
                    tooltip="参考图里的主体长什么样：发型/脸型/服装/配饰/配色/年龄气质。越具体越像。"),
            ],
            outputs=[
                io.Video.Output(
                    "videos", display_name="批量视频", is_output_list=True,
                    tooltip="本次落盘的每一段（VIDEO 指向文件，不占内存）。"
                            "直接接「XB-BOX - 🎬 视频批量拼接」勾选「拼接保存」即可无损拼成一整条。"),
            ],
            is_output_node=True,
        )

    @classmethod
    def execute(cls, bus=None, video_desc="", image_desc="",
                story_name=DEFAULT_VIDEO_EDIT_OUT_PREFIX, manager_settings="",
                model=None, clip=None, vae=None, audio_vae=None) -> io.NodeOutput:
        # 整段流程复用父类（1:1 同一套实现）；把日志标签临时切成「视频编辑」，
        # 否则这个节点跑起来满屏都是「人物替换」，排障时容易看错节点。
        global TAG
        _saved_tag, TAG = TAG, "视频编辑"
        try:
            source_video, source_audio, ref_images, slot = _bus_media(bus)
            if source_video is None:
                raise ValueError("「素材总线」里没有源视频：请把「JZL - 📥 MiniMax 素材加载」的"
                                 "「视频1」（或任一视频槽位）填上素材后再运行")
            n_img = sum(1 for v in ref_images.values() if v is not None)
            print(f"[JZL-{TAG}] 素材总线：源视频 = 视频{slot + 1}"
                  f"｜源原声 = {'有' if source_audio is not None else '无'}"
                  f"｜参考图 {n_img} 张", flush=True)
            return super().execute(
                source_video=source_video, video_desc=video_desc, image_desc=image_desc,
                story_name=story_name, manager_settings=manager_settings,
                ref_images=ref_images, source_audio=source_audio,
                model=model, clip=clip, vae=vae, audio_vae=audio_vae)
        finally:
            TAG = _saved_tag
