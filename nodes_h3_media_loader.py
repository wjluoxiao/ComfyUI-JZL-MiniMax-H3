"""JZL MiniMax H3 — MiniMax 素材加载节点。

把「素材库」子工作流（9 张参考图 + 3 段参考视频 + 3 段参考音频）收成一个节点，
节点界面由前端 JS（js/h3_media_loader.js）绘制，参数统一存进 media_settings（JSON 字符串）。

输出与「JZL - 🔗 MiniMax H3 ref2va参考总线（打包）」完全一致：
一次产出 18 个固定接口打包成的 JZL_REF2VA_BUS，可直接接「解包」节点。

键名/顺序直接复用 nodes_ref2va_bus.BUS_KEYS —— 打包口若调整，本节点自动跟随，
不存在两处各写一份键名而错位的风险。

参数约定（与前端一致，后端做类型钳制，脏数据不会炸）：
  images[i] = {path, enabled}
  videos[i] = {path, enabled, force_rate, skip, width, height, cap}
  audios[i] = {path, enabled, start, duration}
未启用 / 未上传 / 文件丢失的槽位 → 对应总线键为 None（等同打包节点该口未连线）。
"""

import json
import os

import numpy as np
import torch

from .nodes_ref2va_bus import BUS_KEYS, BUS_TYPE

try:
    import folder_paths
except Exception:  # 非 ComfyUI 环境（单测）下允许导入
    folder_paths = None

IMAGE_COUNT = 9
AUDIO_COUNT = 3
VIDEO_COUNT = 3

# 布局（由前端「界面设置」写入，随工作流保存）
KIND_ORDER = ("image", "audio", "video")
KIND_LABELS = {"image": "参考图", "audio": "参考音频", "video": "参考视频"}
# 每类槽位可选数量档位（与前端「界面设置」一致）；槽位数组始终按上限长度保存，
# 只改「用几个」不丢已有素材，数量调回去素材还在。
KIND_COUNT_OPTIONS = {
    "image": (1, 2, 3, 4, 6, 8, 9),
    "audio": (1, 2, 3),
    "video": (1, 2, 3),
}

# 抽帧硬上限：防止误填（如 cap=0 且视频极长）时一次性吃光内存
MAX_VIDEO_FRAMES_HARD = 6000

# 加载出来的 IMAGE 张量超过这个体积（GB，CPU 内存）时给一条提示
VIDEO_MEM_WARN_GB = 4.0


def _warn_video_ram(name, frames, fw, fh, est_gb, resized, width, height):
    """提示「这次加载自己占了多少 CPU 内存」。

    注意：下游编码节点确实会按画布再缩放一次，但那是**之后**的事 ——
    这里的张量在加载阶段就已经分配好了，并且一直活到编码结束（总线值还引用着它），
    所以下游缩放**省不掉**这份内存，只能在本节点的「输出宽高 / 帧数上限」里省。
    """
    if resized:
        tip = ("已按「输出宽高」缩放到 %dx%d 仍然偏大，可继续调小宽高，或限制帧数上限"
               % (fw, fh))
    else:
        hw = max(2, int(round(fw * 0.5 / 2.0)) * 2)
        hh = max(2, int(round(fh * 0.5 / 2.0)) * 2)
        after = frames * hh * hw * 3 * 4 / float(1024 ** 3)
        tip = ("原始尺寸未缩放；把「输出宽高」设成 %dx%d 可降到约 %.1f GB"
               % (hw, hh, after))
    print("[JZL-素材加载] ⚠️ 视频 %s：%d 帧 %dx%d 要占 %.1f GB CPU 内存（float32）"
          "—— 这份内存是**加载时**就分配的，编码节点之后虽然会按画布再缩放，但那一步省不掉它。%s"
          % (name, frames, fw, fh, est_gb, tip))


# ── 配置 JSON：解析 / 归一化（前端写什么进来都不能把后端弄崩） ─────────────

def _default_layout():
    return {
        "order": list(KIND_ORDER),
        "counts": {k: KIND_COUNT_OPTIONS[k][-1] for k in KIND_ORDER},
        "hidden": {k: False for k in KIND_ORDER},
    }


def _snap_count(value, options):
    """把任意数字吸附到最近的可选档位（如 3 → 2，7 → 6），而不是粗暴地退回最大值。"""
    n = _as_int(value, options[-1], options[0], options[-1])
    return min(options, key=lambda o: (abs(o - n), o))


def _parse_layout(raw):
    """布局归一化：顺序=三模块的一个排列；数量=允许档位；至少保留一个模块可见。"""
    src = raw if isinstance(raw, dict) else {}

    order = []
    raw_order = src.get("order")
    if isinstance(raw_order, (list, tuple)):
        for k in raw_order:
            if isinstance(k, str) and k in KIND_ORDER and k not in order:
                order.append(k)
    for k in KIND_ORDER:
        if k not in order:
            order.append(k)

    raw_counts = src.get("counts") if isinstance(src.get("counts"), dict) else {}
    counts = {k: _snap_count(raw_counts.get(k), KIND_COUNT_OPTIONS[k]) for k in KIND_ORDER}

    raw_hidden = src.get("hidden") if isinstance(src.get("hidden"), dict) else {}
    hidden = {k: _as_bool(raw_hidden.get(k), False) for k in KIND_ORDER}
    if all(hidden.values()):
        hidden[order[0]] = False

    return {"order": order, "counts": counts, "hidden": hidden}


def _default_settings():
    return {
        "version": 2,
        "layout": _default_layout(),
        "images": [{"path": "", "enabled": True} for _ in range(IMAGE_COUNT)],
        "audios": [{"path": "", "enabled": True, "start": 0.0, "duration": 0.0}
                   for _ in range(AUDIO_COUNT)],
        "videos": [{"path": "", "enabled": True, "force_rate": 0.0, "skip": 0,
                    "width": 0, "height": 0, "cap": 0, "audio_enabled": True}
                   for _ in range(VIDEO_COUNT)],
    }


def _as_float(value, default=0.0, lo=None, hi=None):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    if lo is not None and f < lo:
        f = lo
    if hi is not None and f > hi:
        f = hi
    return f


def _as_int(value, default=0, lo=None, hi=None):
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _as_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("0", "false", "no", "off", "关", "关闭", "禁用"):
        return False
    if text in ("1", "true", "yes", "on", "开", "开启", "启用"):
        return True
    return default


def _as_path(value):
    return str(value or "").strip().replace("\\", "/")


def _slot_list(raw, key, count):
    """取出某一类槽位数组，长度不足补空、超长截断。"""
    items = raw.get(key) if isinstance(raw, dict) else None
    if not isinstance(items, list):
        items = []
    out = [it if isinstance(it, dict) else {} for it in items[:count]]
    while len(out) < count:
        out.append({})
    return out


def parse_settings(raw):
    """media_settings（JSON 字符串或 dict）→ 归一化配置。任何异常都退回默认配置。"""
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        try:
            data = json.loads(text) if text else {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    images = [{"path": _as_path(it.get("path")), "enabled": _as_bool(it.get("enabled"), True)}
              for it in _slot_list(data, "images", IMAGE_COUNT)]

    audios = [{
        "path": _as_path(it.get("path")),
        "enabled": _as_bool(it.get("enabled"), True),
        "start": _as_float(it.get("start"), 0.0, 0.0),
        "duration": _as_float(it.get("duration"), 0.0, 0.0),
    } for it in _slot_list(data, "audios", AUDIO_COUNT)]

    videos = [{
        "path": _as_path(it.get("path")),
        "enabled": _as_bool(it.get("enabled"), True),
        "force_rate": _as_float(it.get("force_rate"), 0.0, 0.0, 240.0),
        "skip": _as_int(it.get("skip"), 0, 0),
        "width": _as_int(it.get("width"), 0, 0),
        "height": _as_int(it.get("height"), 0, 0),
        "cap": _as_int(it.get("cap"), 0, 0),
        # 旧工作流没有该字段 → 默认开启（保持原有行为）
        "audio_enabled": _as_bool(it.get("audio_enabled"), True),
    } for it in _slot_list(data, "videos", VIDEO_COUNT)]

    return {"version": 2, "layout": _parse_layout(data.get("layout")),
            "images": images, "audios": audios, "videos": videos}


# ── 文件定位 / 加载 ────────────────────────────────────────────────

def resolve_media_path(path):
    """素材路径 → 绝对路径：绝对路径直接用；相对路径按 input/output 目录解析
    （与 /jzl/upload_asset 返回的 jzl/image/xxx.png 保持一致）。"""
    p = (path or "").strip()
    if not p:
        return ""
    p = p.replace("\\", "/")
    if os.path.isfile(p):
        return p
    if folder_paths is None:
        return ""
    try:
        in_dir = folder_paths.get_input_directory()
        out_dir = folder_paths.get_output_directory()
    except Exception:
        return ""
    for cand in (
        os.path.join(in_dir, p),
        os.path.join(out_dir, p),
        os.path.join(in_dir, "jzl", p),
    ):
        if os.path.isfile(cand):
            return cand
    return ""


def load_image(path):
    """图片 → IMAGE tensor [1, H, W, C]（RGB，0-1）。"""
    from PIL import Image, ImageOps
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # EXIF 旋转
    img = img.convert("RGB")
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return torch.from_numpy(arr)[None]


def _read_audio_file(path):
    """音频文件 → (waveform [C, L] tensor, sample_rate)。

    soundfile 覆盖 wav/flac/ogg；m4a/aac/mp3 等走 PyAV（ComfyUI 自带解码器）；
    torchaudio 兜底。
    """
    try:
        import soundfile as sf
        data, sample_rate = sf.read(path, dtype="float32")
        if data.ndim == 1:
            data = data[None, :]
        else:
            data = data.T
        return torch.from_numpy(np.ascontiguousarray(data)), int(sample_rate)
    except Exception:
        pass

    try:
        import av
        with av.open(path) as container:
            if not container.streams.audio:
                raise RuntimeError("没有音频流")
            stream = container.streams.audio[0]
            sample_rate = int(stream.codec_context.sample_rate or 44100)
            channels = int(stream.codec_context.channels or 1)
            chunks = [_av_audio_ndarray(frame, channels) for frame in container.decode(stream)]
        chunks = [c for c in chunks if c is not None and c.shape[1] > 0]
        if not chunks:
            raise RuntimeError("没有解出音频帧")
        return torch.from_numpy(np.ascontiguousarray(np.concatenate(chunks, axis=1))), sample_rate
    except Exception:
        pass

    import torchaudio
    waveform, sample_rate = torchaudio.load(path)  # [C, L]
    return waveform, int(sample_rate)


def _av_audio_ndarray(frame, channels):
    """PyAV 音频帧 → [C, L] float32（-1~1）。"""
    arr = frame.to_ndarray()
    if arr.ndim == 1:
        arr = arr[None, :]
    elif arr.shape[0] != channels:
        arr = arr.reshape(-1, channels).T
    if arr.dtype == np.int16:
        arr = arr.astype(np.float32) / 32768.0
    elif arr.dtype == np.int32:
        arr = arr.astype(np.float32) / 2147483648.0
    elif arr.dtype == np.uint8:
        arr = (arr.astype(np.float32) - 128.0) / 128.0
    else:
        arr = arr.astype(np.float32)
    return arr


def _make_audio(waveform, sample_rate):
    if waveform.dim() == 1:
        waveform = waveform.unsqueeze(0)
    return {"waveform": waveform.unsqueeze(0), "sample_rate": int(sample_rate)}


def load_audio(path, start=0.0, duration=0.0):
    """音频 → AUDIO dict，按 start（秒）/duration（秒，0=到结尾）切片。"""
    waveform, sample_rate = _read_audio_file(path)  # [C, L]
    total = waveform.shape[-1]
    s_idx = 0 if start <= 0 else int(round(start * sample_rate))
    s_idx = max(0, min(s_idx, total))
    if duration and duration > 0:
        e_idx = min(total, s_idx + int(round(duration * sample_rate)))
    else:
        e_idx = total
    if e_idx <= s_idx:
        raise ValueError(f"切片区间为空（开始 {start:g}s / 时长 {duration:g}s，"
                         f"素材总长 {total / max(1, sample_rate):.2f}s）")
    return _make_audio(waveform[:, s_idx:e_idx], sample_rate)


def _target_size(sw, sh, width, height):
    """输出尺寸：两边都填直接用；只填一边按比例算另一边并偶数对齐；都不填保持原尺寸。"""
    w, h = int(width or 0), int(height or 0)
    if w > 0 and h > 0:
        return w, h
    if w > 0 and sw > 0:
        return w, max(2, int(round(sh * w / float(sw) / 2.0)) * 2)
    if h > 0 and sh > 0:
        return max(2, int(round(sw * h / float(sh) / 2.0)) * 2), h
    return None


def load_video_frames(path, force_rate=0.0, skip=0, width=0, height=0, cap=0):
    """视频 → IMAGE 序列 [T, H, W, C]。

    参数含义与「XB-BOX - 🎬 加载视频」一致：
      force_rate   强制帧率（0 = 保持原帧率）
      skip         跳过前 N 帧（在改帧率之后计数）
      width/height 输出尺寸（0 = 保持原尺寸；只填一边则另一边按比例+偶数对齐）
      cap          帧数上限（0 = 不限制，另有 6000 帧安全上限）
    """
    import av

    rate = float(force_rate or 0)
    step = (1.0 / rate) if rate > 0 else 0.0
    skip = max(0, int(skip or 0))
    limit = int(cap or 0)
    hard_capped = limit <= 0 or limit > MAX_VIDEO_FRAMES_HARD
    if hard_capped:
        limit = MAX_VIDEO_FRAMES_HARD

    frames = []   # 逐帧累积 uint8 ndarray（不要在这里就转 float32：那会让峰值翻倍）
    with av.open(path) as container:
        if not container.streams.video:
            raise RuntimeError("该文件没有视频流")
        stream = container.streams.video[0]
        try:
            stream.thread_type = "AUTO"
        except Exception:
            pass
        target = _target_size(stream.codec_context.width or 0,
                              stream.codec_context.height or 0, width, height)
        time_base = float(stream.time_base) if stream.time_base else 0.0
        next_t = 0.0
        index = 0          # 改帧率后已见过的帧序号（skip / cap 都按它计数）
        for frame in container.decode(stream):
            if step > 0:
                t = float(frame.pts * time_base) if (frame.pts is not None and time_base) else index / rate
                if t + 1e-6 < next_t:
                    continue
                next_t += step
                while next_t <= t + 1e-6:
                    next_t += step
            if index < skip:
                index += 1
                continue
            if len(frames) >= limit:
                break
            index += 1
            if target:
                frame = frame.reformat(width=target[0], height=target[1],
                                       format="rgb24", interpolation="BILINEAR")
            else:
                frame = frame.reformat(format="rgb24")
            # 必须 copy：to_ndarray() 指向 PyAV 的帧缓冲，下一帧解码就被覆盖
            frames.append(np.array(frame.to_ndarray(), copy=True))   # [H, W, 3] uint8

    if not frames:
        raise RuntimeError("视频没有可用帧（检查「跳过帧数 / 帧数上限」）")
    if hard_capped and len(frames) >= limit:
        print("[JZL-素材加载] ⚠️ 视频 %s 已达安全上限 %d 帧（未填帧数上限），"
              "如需完整加载请在「视频设置」里调高帧数上限" % (os.path.basename(path), limit))
    fh, fw = int(frames[0].shape[0]), int(frames[0].shape[1])
    est_gb = len(frames) * fh * fw * 3 * 4 / float(1024 ** 3)
    if est_gb >= VIDEO_MEM_WARN_GB:
        _warn_video_ram(os.path.basename(path), len(frames), fw, fh, est_gb,
                        bool(target), width, height)
    # 先按 uint8 拼成整块再一次性转 float32：峰值从「2× 张量」降到「1.5× 张量」
    stacked = np.stack(frames)
    del frames
    video = torch.from_numpy(stacked).float()
    del stacked
    video.div_(255.0)
    return video


def _slice_audio(waveform, sample_rate, start_time=0.0, duration=0.0):
    """按秒切片（start_time/duration，0 = 到头）；区间为空返回 None。"""
    total = waveform.shape[-1]
    s_idx = 0 if start_time <= 0 else int(round(start_time * sample_rate))
    s_idx = max(0, min(s_idx, total))
    if duration and duration > 0:
        e_idx = min(total, s_idx + int(round(duration * sample_rate)))
    else:
        e_idx = total
    if e_idx <= s_idx:
        return None
    return waveform[..., s_idx:e_idx]


def load_video_audio(path, start_time=0.0, duration=0.0):
    """视频内嵌音轨 → AUDIO dict（无音轨返回 None）。

    start_time / duration 与「帧数上限 + 跳过帧数 + 强制帧率」对齐：
    视频只取前 N 帧时，音轨也只取对应那段，避免「画面裁了、声音没裁」。
    """
    try:
        import av
        with av.open(path) as container:
            if not container.streams.audio:
                return None
            stream = container.streams.audio[0]
            sample_rate = int(stream.codec_context.sample_rate or 44100)
            channels = int(stream.codec_context.channels or 1)
            chunks = [_av_audio_ndarray(frame, channels) for frame in container.decode(stream)]
        chunks = [c for c in chunks if c is not None and c.shape[1] > 0]
        if not chunks:
            return None
        waveform = torch.from_numpy(np.ascontiguousarray(np.concatenate(chunks, axis=1)))
        cut = _slice_audio(waveform, sample_rate, start_time=start_time, duration=duration)
        if cut is None or cut.shape[-1] == 0:
            return None
        return _make_audio(cut, sample_rate)
    except Exception:
        return None


def video_effective_rate(path, force_rate=0.0):
    """视频实际输出帧率：填了强制帧率用它，否则用源帧率. 读不到时按 24fps 估算。"""
    rate = float(force_rate or 0)
    if rate > 0:
        return rate
    try:
        import av
        with av.open(path) as container:
            if container.streams.video:
                st = container.streams.video[0]
                src = float(st.average_rate) if st.average_rate else 0.0
                if src > 0:
                    return src
    except Exception:
        pass
    return 24.0


# ── 节点 ──────────────────────────────────────────────────────────

class JZL_MiniMaxH3MediaLoader:
    """MiniMax 素材加载 — 9 图 + 3 音频 + 3 视频 → ref2va 参考总线（打包）。

    media_settings 由前端 DOM 面板读写（含每槽位的素材路径 / 开关 / 视频与音频参数），
    随工作流保存。执行时按槽位顺序装载素材并打包成 JZL_REF2VA_BUS，
    键名与「ref2va参考总线（打包）」完全一致，可直接接「解包」节点使用。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "media_settings": ("STRING", {
                    "default": json.dumps(_default_settings(), ensure_ascii=False),
                    "multiline": True,
                    "dynamicPrompts": False,
                    "tooltip": "素材配置 JSON（由节点面板自动维护：9 图 / 3 音频 / 3 视频的路径、开关与参数）",
                }),
            },
        }

    RETURN_TYPES = (BUS_TYPE,)
    RETURN_NAMES = ("ref2va参考总线",)
    FUNCTION = "load"
    CATEGORY = "JZL/MiniMax"
    OUTPUT_NODE = False

    @classmethod
    def IS_CHANGED(cls, media_settings="", **kwargs):
        """素材文件被替换（同名覆盖）也必须重跑，否则会用上一次的缓存结果。"""
        cfg = parse_settings(media_settings)
        sig = [media_settings if isinstance(media_settings, str) else json.dumps(cfg, sort_keys=True)]
        for group in ("images", "audios", "videos"):
            for item in cfg[group]:
                p = resolve_media_path(item.get("path"))
                if not p:
                    continue
                try:
                    st = os.stat(p)
                    sig.append("%s|%d|%d" % (p, int(st.st_mtime), st.st_size))
                except Exception:
                    sig.append(p)
        return "jzl_h3_media|" + "|".join(sig)

    def load(self, media_settings=""):
        cfg = parse_settings(media_settings)
        layout = cfg["layout"]
        counts = layout["counts"]
        hidden = layout["hidden"]
        bus = {key: None for key in BUS_KEYS}
        report = {"image": [0, 0], "audio": [0, 0], "video": [0, 0]}  # [载入数, 启用数]
        errors = []

        # 参考图 → ref_image_0..8（只取布局里启用的前 N 个；整块隐藏则全部留空）
        if not hidden["image"]:
            for i, item in enumerate(cfg["images"][:counts["image"]]):
                enabled = True   # 槽位不再支持「禁用」：界面已改为「加载/替换」，禁用会在总线留空洞
                path = resolve_media_path(item["path"])
                if enabled:
                    report["image"][1] += 1
                if not enabled or not path:
                    if enabled and item["path"]:
                        errors.append("图片%d：文件不存在（%s）" % (i + 1, item["path"]))
                    continue
                try:
                    bus["ref_image_%d" % i] = load_image(path)
                    report["image"][0] += 1
                except Exception as exc:
                    errors.append("图片%d：%s" % (i + 1, exc))

        # 参考视频 → ref_video_0..2 + ref_video_audio_0..2
        if not hidden["video"]:
            for i, item in enumerate(cfg["videos"][:counts["video"]]):
                enabled = True   # 槽位不再支持「禁用」：界面已改为「加载/替换」，禁用会在总线留空洞
                path = resolve_media_path(item["path"])
                if enabled:
                    report["video"][1] += 1
                if not enabled or not path:
                    if enabled and item["path"]:
                        errors.append("视频%d：文件不存在（%s）" % (i + 1, item["path"]))
                    continue
                try:
                    bus["ref_video_%d" % i] = load_video_frames(
                        path, force_rate=item["force_rate"], skip=item["skip"],
                        width=item["width"], height=item["height"], cap=item["cap"])
                    report["video"][0] += 1
                except Exception as exc:
                    errors.append("视频%d：%s" % (i + 1, exc))
                # 音轨跟着画面裁：跳过帧数 / 帧数上限 换算成秒，视频裁到哪音轨裁到哪
                # （否则画面只取 5 帧、声音却是整段，下游会以为「帧数上限没生效」）
                # audio_enabled=False → 该槽位直接传 None；键名固定是 ref_video_audio_i，
                # 只把这一格清空，其他视频的音轨仍然落在自己原来的槽位上（不会前移）。
                if not item["audio_enabled"]:
                    bus["ref_video_audio_%d" % i] = None
                    continue
                try:
                    rate = video_effective_rate(path, item["force_rate"])
                    a_start = (item["skip"] / rate) if rate > 0 else 0.0
                    a_dur = (item["cap"] / rate) if (item["cap"] > 0 and rate > 0) else 0.0
                    bus["ref_video_audio_%d" % i] = load_video_audio(
                        path, start_time=a_start, duration=a_dur)
                except Exception:
                    bus["ref_video_audio_%d" % i] = None

        # 独立音频 → ref_audio_0..2
        if not hidden["audio"]:
            for i, item in enumerate(cfg["audios"][:counts["audio"]]):
                enabled = True   # 槽位不再支持「禁用」：界面已改为「加载/替换」，禁用会在总线留空洞
                path = resolve_media_path(item["path"])
                if enabled:
                    report["audio"][1] += 1
                if not enabled or not path:
                    if enabled and item["path"]:
                        errors.append("音频%d：文件不存在（%s）" % (i + 1, item["path"]))
                    continue
                try:
                    bus["ref_audio_%d" % i] = load_audio(path, start=item["start"],
                                                         duration=item["duration"])
                    report["audio"][0] += 1
                except Exception as exc:
                    errors.append("音频%d：%s" % (i + 1, exc))

        # 摘要按「顺序 + 只有显示的模块」输出，与节点面板上看到的一致
        parts = []
        for kind in layout["order"]:
            if hidden[kind]:
                continue
            parts.append("%s %d/%d" % (KIND_LABELS[kind],
                                       report[kind][0], report[kind][1]))
        summary = " 丨 ".join(parts) if parts else "（未启用任何模块）"
        print("[JZL-素材加载] %s（数字为 载入/启用）" % summary)
        for err in errors[:10]:
            print("[JZL-素材加载] ⚠️ %s" % err)

        info = summary if not errors else summary + " 丨 异常 %d" % len(errors)
        return {"ui": {"media_info": [info], "media_errors": [errors[:10]]},
                "result": (bus,)}


NODE_CLASS_MAPPINGS = {"JZL_MiniMaxH3MediaLoader": JZL_MiniMaxH3MediaLoader}
NODE_DISPLAY_NAME_MAPPINGS = {"JZL_MiniMaxH3MediaLoader": "JZL - 📥 MiniMax 素材加载"}
