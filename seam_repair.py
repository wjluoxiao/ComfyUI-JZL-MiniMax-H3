"""接缝修复（纯 CPU、像素/波形域）。

思路参考 H3-Continuum 的 seam guard：不重采样、不动 latent，只在**解码后**用很小的
局部手术把段边界修平，而且每步都"先打分、变差就撤回"，绝不硬拉。

四件事：
  1. `choose_cut`      —— 自适应切点：在段首丢弃量附近 ±几帧里挑接缝最不显眼的那一帧
  2. `apply_color_guard` —— 颜色护栏：只动段首 4 帧的亮度/色度，限制幅度且验证后回滚
  3. `blend_into`      —— 交叉淡化：低运动时把交界 1~2 帧做余弦过渡
  4. `audio_join`      —— 音频接缝：电平/直流匹配 + 8ms 交叠消音（长度不变）

所有函数都接受 [T,H,W,C] float32（0..1）或 [B,C,L] 波形，返回 CPU 张量，不碰 GPU。
"""

import math

import torch

# 比对用的小图边长：接缝是否明显与分辨率无关，缩到 96 足够且极快
COMPARE_SIZE = 96

# 自适应切点：最多回退几帧（回退 = 上一段少放几帧、本段多放几帧，总量不变）
MAX_REWIND = 3
# 接受门槛：相对改善 ≥10%、绝对改善 ≥0.003、且候选分数 ≤0.35（越大说明接缝越明显）
MIN_RELATIVE_IMPROVEMENT = 0.10
MIN_ABSOLUTE_IMPROVEMENT = 0.003
SAFE_SCORE_LIMIT = 0.35

# 颜色护栏：亮度增益 / 色度偏移 / 差异阈值（超出就不动）
GAIN_LIMIT = 0.12          # ±12% 之外判为换了场景
BIAS_LIMIT = 0.02          # 8bit 约 ±5
FADE_WEIGHTS = (1.0, 0.65, 0.30, 0.0)   # 只在前 4 帧上收敛回原样

# 交叉淡化：按运动量决定混几帧
MOTION_LOW = 0.015
MOTION_MID = 0.040

# 音频接缝：电平/直流限制 + 交叠时长
AUDIO_LEVEL_DB_LIMIT = 1.5
AUDIO_DC_LIMIT = 0.02
AUDIO_FADE_MS = 8.0


def _down(frames):
    """[T,H,W,C] → [T,S,S,3] float32 CPU（用于比对，不影响输出）。"""
    if frames is None or not torch.is_tensor(frames) or frames.ndim != 4:
        raise ValueError("接缝比对需要 [T,H,W,C] 的 IMAGE 张量")
    rgb = frames.detach().to(device="cpu", dtype=torch.float32)[..., :3]
    if int(rgb.shape[0]) < 1:
        raise ValueError("接缝比对收到空帧序列")
    if int(rgb.shape[1]) != COMPARE_SIZE or int(rgb.shape[2]) != COMPARE_SIZE:
        chw = rgb.permute(0, 3, 1, 2)
        chw = torch.nn.functional.interpolate(
            chw, size=(COMPARE_SIZE, COMPARE_SIZE), mode="bilinear", align_corners=False)
        rgb = chw.permute(0, 2, 3, 1)
    rgb = rgb.clamp(0.0, 1.0)
    if not bool(torch.isfinite(rgb).all()):
        raise ValueError("接缝比对收到 NaN/Inf")
    return rgb


def _luma(rgb):
    return torch.sum(rgb * rgb.new_tensor((0.2126, 0.7152, 0.0722)), dim=-1)


def boundary_metrics(prev_tail, next_head):
    """交界两侧各取 2 帧比较，返回各项误差 + 综合分（0 最好）。

    prev_tail: 上一段最后 2 帧；next_head: 本段（切点之后）最前 2 帧。
    """
    prev = _down(prev_tail)[-2:]
    nxt = _down(next_head)[:2]
    if int(prev.shape[0]) < 2 or int(nxt.shape[0]) < 2:
        raise ValueError("接缝打分需要两侧各 2 帧")
    prev_before, prev_edge = prev[0], prev[1]
    next_edge, next_after = nxt[0], nxt[1]

    pixel = float(torch.mean(torch.abs(prev_edge - next_edge)).item())
    prev_y, next_y = _luma(prev_edge), _luma(next_edge)
    luma = float(torch.mean(torch.abs(prev_y - next_y)).item())
    prev_chroma = prev_edge - prev_y.unsqueeze(-1)
    next_chroma = next_edge - next_y.unsqueeze(-1)
    chroma = float(torch.mean(torch.abs(prev_chroma - next_chroma)).item()) / 2.0
    prev_gx = prev_y[:, 1:] - prev_y[:, :-1]
    next_gx = next_y[:, 1:] - next_y[:, :-1]
    prev_gy = prev_y[1:, :] - prev_y[:-1, :]
    next_gy = next_y[1:, :] - next_y[:-1, :]
    edge = (float(torch.mean(torch.abs(prev_gx - next_gx)).item())
            + float(torch.mean(torch.abs(prev_gy - next_gy)).item())) / 4.0
    prev_delta = prev_edge - prev_before
    next_delta = next_after - next_edge
    temporal = float(torch.mean(torch.abs(prev_delta - next_delta)).item()) / 2.0
    motion = max(float(torch.mean(torch.abs(prev_delta)).item()),
                 float(torch.mean(torch.abs(next_delta)).item()))
    score = (0.35 * pixel + 0.20 * luma + 0.10 * chroma + 0.15 * edge + 0.20 * temporal)
    if not math.isfinite(score):
        raise ValueError("接缝分数不是有限值")
    return {
        "pixel": pixel, "luma": luma, "chroma": chroma, "edge": edge,
        "temporal": temporal, "motion": motion,
        "score": max(0.0, min(1.0, score)),
    }


def _candidate_ok(legacy, cand):
    """候选切点必须「整体明显更好、而且没有哪一项明显变差」，否则宁可不动。

    特别防一个坑：如果候选帧和上一段末帧几乎一模一样（回退到重复帧），
    `pixel` 会掉到极低，看起来分数很好 —— 但那其实是一帧静止。所以这里额外要求
    运动连续性（temporal）不能变差，且不允许出现逐帧完全相同的画面。
    """
    if cand["pixel"] <= 1e-4 and legacy["pixel"] > 1e-3:
        return False
    if cand["temporal"] > legacy["temporal"] * 1.05 + 1e-4:
        return False
    for key in ("pixel", "luma", "chroma", "edge"):
        if cand[key] > legacy[key] * 1.05 + 1e-4:
            return False
    return True


def choose_cut(prev_tail, next_raw, cut, max_rewind=MAX_REWIND):
    """在 `cut` 附近挑接缝最不显眼的切点。

    返回 (rewind, legacy_metrics, best_metrics)。rewind=0 表示保持原切点。
    语义：切点提前 `rewind` 帧 ⇒ 上一段少放 `rewind` 帧，本段多放 `rewind` 帧（总长不变）。
    """
    cut = int(cut)
    limit = min(int(max_rewind), max(0, int(next_raw.shape[0]) - 2))
    if cut < 0 or cut + 2 > int(next_raw.shape[0]) or int(prev_tail.shape[0]) < 2:
        return 0, None, None
    legacy = boundary_metrics(prev_tail[-2:], next_raw[cut:cut + 2])
    best_rewind, best = 0, legacy
    for rewind in range(1, limit + 1):
        if cut - rewind < 0:
            break
        try:
            cand = boundary_metrics(prev_tail[-2:], next_raw[cut - rewind:cut - rewind + 2])
        except Exception:
            continue
        if cand["score"] < best["score"] and _candidate_ok(legacy, cand):
            best_rewind, best = rewind, cand
    absolute = legacy["score"] - best["score"]
    relative = absolute / max(legacy["score"], 1e-12)
    ok = (best_rewind > 0
          and relative >= MIN_RELATIVE_IMPROVEMENT
          and absolute >= MIN_ABSOLUTE_IMPROVEMENT
          and best["score"] <= SAFE_SCORE_LIMIT)
    return (best_rewind, legacy, best) if ok else (0, legacy, legacy)


def choose_blend(metrics, max_blend=2):
    """按交界处的运动量决定交叉淡化几帧（运动大就别混，混了会糊）。"""
    if not metrics:
        return 0
    motion = float(metrics.get("motion", 0.0))
    if motion <= MOTION_LOW:
        return min(2, int(max_blend))
    if motion <= MOTION_MID:
        return min(1, int(max_blend))
    return 0


def apply_color_guard(prev_tail, cur_head):
    """把本段开头几帧的亮度/色度朝上一段末帧拉，幅度受限、验证后不满意就原样返回。

    prev_tail: 上一段最后 2 帧；cur_head: 本段（切点之后）前 4 帧（会被改写）。
    返回 (新 cur_head, 信息 dict|None)。
    """
    count = min(len(FADE_WEIGHTS), int(cur_head.shape[0]))
    if count < 2 or int(prev_tail.shape[0]) < 2:
        return cur_head, None
    try:
        before = boundary_metrics(prev_tail[-2:], cur_head[:2])
        prev_rgb = _down(prev_tail[-2:])
        cur_rgb = _down(cur_head[:2])
        prev_y = float(_luma(prev_rgb).mean().item())
        cur_y = float(_luma(cur_rgb).mean().item())
        if cur_y <= 1e-6:
            return cur_head, None
        requested_gain = prev_y / cur_y
        if not (1.0 - GAIN_LIMIT) <= requested_gain <= (1.0 + GAIN_LIMIT):
            return cur_head, None
        gain = max(1.0 - GAIN_LIMIT / 2, min(1.0 + GAIN_LIMIT / 2, requested_gain))
        prev_chroma = (prev_rgb - _luma(prev_rgb).unsqueeze(-1)).mean(dim=(0, 1, 2))
        cur_chroma = (cur_rgb - _luma(cur_rgb).unsqueeze(-1)).mean(dim=(0, 1, 2))
        requested_bias = prev_chroma - cur_chroma
        if float(torch.max(torch.abs(requested_bias)).item()) > BIAS_LIMIT * 4:
            return cur_head, None
        bias = requested_bias.clamp(-BIAS_LIMIT, BIAS_LIMIT)

        out = cur_head.clone()
        weights = out.new_tensor(FADE_WEIGHTS)[:count]
        luma_w = out.new_tensor((0.2126, 0.7152, 0.0722))
        for index in range(count):
            weight = float(weights[index].item())
            rgb = out[index, ..., :3]
            y = torch.sum(rgb * luma_w, dim=-1, keepdim=True)
            chroma = rgb - y
            frame_gain = 1.0 + (gain - 1.0) * weight
            out[index, ..., :3] = (y * frame_gain + chroma
                                   + bias.to(dtype=rgb.dtype) * weight).clamp(0.0, 1.0)
        after = boundary_metrics(prev_tail[-2:], out[:2])
        if after["score"] > before["score"] * 1.01:
            return cur_head, None
        return out, {
            "gain": gain, "bias": [float(v) for v in bias.tolist()],
            "before": before["score"], "after": after["score"],
        }
    except Exception:
        return cur_head, None


def blend_into(prev_frames, cur_frames, frames):
    """把 `prev_frames` 的最后 `frames` 帧与 `cur_frames` 同位置的前几帧做余弦过渡。

    两者必须表示同一批时刻（即被回退掉的那几帧），返回新的 prev 片段。
    """
    frames = int(frames)
    if frames <= 0:
        return prev_frames
    frames = min(frames, int(prev_frames.shape[0]), int(cur_frames.shape[0]))
    if frames <= 0:
        return prev_frames
    out = prev_frames.clone()
    for index in range(frames):
        progress = float(index + 1) / float(frames + 1)
        alpha = 0.5 - 0.5 * math.cos(math.pi * progress)
        out[index] = (out[index].float() * (1.0 - alpha)
                      + cur_frames[index].float() * alpha).to(out.dtype)
    return out


def frame_stats(frames):
    """一段画面的亮度均值/对比度/高频能量 —— 用来盯「越往后越硬/越灰」的漂移。"""
    if frames is None or not torch.is_tensor(frames) or frames.ndim != 4:
        return None
    try:
        rgb = _down(frames)
        y = _luma(rgb)
        gx = y[:, :, 1:] - y[:, :, :-1]
        gy = y[:, 1:, :] - y[:, :-1, :]
        return {
            "luma": float(y.mean().item()),
            "contrast": float(y.std().item()),
            "detail": float(0.5 * (gx.abs().mean() + gy.abs().mean()).item()),
        }
    except Exception:
        return None


def audio_join(prev_tail, cur_head, sample_rate):
    """音频接缝：把本段开头的电平和直流朝上一段末尾对齐，并在交界做 8ms 交叠消音。

    prev_tail: [B,C,L] 上一段最后一段波形（会被改写尾部）；cur_head: [B,C,M] 本段开头（会被改写）。
    两者长度都保持不变（不增不减），所以拼接时长不受影响。
    """
    sr = int(sample_rate or 32000)
    if not torch.is_tensor(prev_tail) or not torch.is_tensor(cur_head):
        return prev_tail, cur_head, None
    if prev_tail.ndim != 3 or cur_head.ndim != 3:
        return prev_tail, cur_head, None
    fade = int(round(sr * AUDIO_FADE_MS / 1000.0))
    if fade < 4 or int(prev_tail.shape[-1]) < fade or int(cur_head.shape[-1]) < fade:
        return prev_tail, cur_head, None
    try:
        before = prev_tail.detach().clone(), cur_head.detach().clone()
        prev = prev_tail.float()
        cur = cur_head.float()
        prev_rms = float(torch.sqrt(torch.mean(prev[..., -fade:].square())).item())
        cur_rms = float(torch.sqrt(torch.mean(cur[..., :fade].square())).item())
        level_db = 0.0
        if prev_rms > 1e-6 and cur_rms > 1e-6:
            requested = 20.0 * math.log10(prev_rms / cur_rms)
            if abs(requested) <= AUDIO_LEVEL_DB_LIMIT * 4:
                level_db = max(-AUDIO_LEVEL_DB_LIMIT, min(AUDIO_LEVEL_DB_LIMIT, requested))
        gain = 10.0 ** (level_db / 20.0)
        requested_dc = float((prev[..., -fade:].mean() - cur[..., :fade].mean()).item())
        dc = max(-AUDIO_DC_LIMIT, min(AUDIO_DC_LIMIT, requested_dc)) if abs(requested_dc) <= AUDIO_DC_LIMIT * 5 else 0.0

        head = cur_head.float().clone()
        head[..., :fade] = (head[..., :fade] * gain + dc)
        # 交界两侧各做半个余弦窗（合起来是一个完整的交叉淡化，但总长不变）
        ramp = torch.linspace(0.0, 1.0, fade, dtype=torch.float32, device=head.device)
        fade_out = torch.cos(ramp * (math.pi / 2.0))
        fade_in = torch.sin(ramp * (math.pi / 2.0))
        tail = prev_tail.float().clone()
        tail[..., -fade:] = tail[..., -fade:] * fade_out
        head[..., :fade] = head[..., :fade] * fade_in
        # 峰值护栏：交叠不应把局部峰值抬上去
        peak = max(float(prev_tail.float().abs().max().item()),
                   float(cur_head.float().abs().max().item()), 1e-6)
        if float(head.abs().max().item()) > peak * 1.02:
            head = head * ((peak * 1.02) / float(head.abs().max().item()))
        info = {"level_db": level_db, "dc": dc, "fade_samples": fade}
        if not (torch.isfinite(tail).all() and torch.isfinite(head).all()):
            return before[0], before[1], None
        return (tail.to(prev_tail.dtype), head.to(cur_head.dtype), info)
    except Exception:
        return prev_tail, cur_head, None


def motion_score(frames, limit=48):
    """源片整体运动量（0~1）：抽最多 `limit` 帧算相邻差分均值。用于建议重叠帧数。"""
    if frames is None or not torch.is_tensor(frames) or frames.ndim != 4:
        return None
    try:
        total = int(frames.shape[0])
        if total < 3:
            return None
        step = max(1, total // int(limit))
        idx = list(range(0, total, step))
        sample = frames[idx]
        rgb = _down(sample)
        delta = torch.mean(torch.abs(rgb[1:] - rgb[:-1]), dim=(1, 2, 3))
        return float(delta.mean().item())
    except Exception:
        return None


def suggest_overlap(score):
    """运动分数 → 建议重叠帧数（只做提示，不自动改）。"""
    if score is None:
        return None
    if score <= MOTION_LOW:
        return 5
    if score <= MOTION_MID:
        return 22
    return 39
