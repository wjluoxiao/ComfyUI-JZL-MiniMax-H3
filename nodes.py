"""JZL MiniMax H3 节点实现。

Reference to Video (ref2va) — 100% 复刻官方 MiniMaxH3ReferenceToVideo，
并新增「参考值放大」选项（仅 match 模式，面积倍率）。

官方参考: comfy_extras/nodes_minimax_h3.py — MiniMaxH3ReferenceToVideo
"""

import math

import torch
import torchaudio

import nodes
import comfy.model_management
import comfy.nested_tensor
import comfy.utils
import node_helpers
from comfy_api.latest import io

CANVAS_MULTIPLE = 32
BASE_SHORT_EDGE = 768
MAX_PIXELS = 768 * 1344
REF_IMAGE_SHORT_EDGE = 2048
FPS = 24
AUDIO_LATENT_FPS = 40


def align_frame_count(n):
    while n % 17 != 5:
        n += 1
    return n


def video_latent_t(frame_count):
    return 2 if frame_count <= 5 else ((frame_count - 5) // 17) * 5 + 2


def temporal_shape(length):
    frame_count = align_frame_count(max(5, length))
    duration = frame_count / FPS
    return frame_count, video_latent_t(frame_count), round(duration * AUDIO_LATENT_FPS)


def adapt_canvas(width, height):
    """768-short-edge canvas with 768*1344 area cap, per-axis round to 32."""
    ratio = width / height
    if ratio >= 1.0:
        nom_w, nom_h = BASE_SHORT_EDGE * ratio, BASE_SHORT_EDGE
    else:
        nom_w, nom_h = BASE_SHORT_EDGE, BASE_SHORT_EDGE / ratio
    if nom_w * nom_h > MAX_PIXELS:
        s = math.sqrt(MAX_PIXELS / (nom_w * nom_h))
        nom_w, nom_h = nom_w * s, nom_h * s
    return (max(CANVAS_MULTIPLE, round(nom_w / CANVAS_MULTIPLE) * CANVAS_MULTIPLE),
            max(CANVAS_MULTIPLE, round(nom_h / CANVAS_MULTIPLE) * CANVAS_MULTIPLE))


def _resize(image, width, height, crop):
    # image [B, H, W, C] -> [B, height, width, 3]
    samples = image[..., :3].movedim(-1, 1)
    samples = comfy.utils.common_upscale(samples, width, height, "lanczos", crop)
    return samples.movedim(1, -1)


def _empty_av_latent(width, height, length, batch_size=1):
    frame_count, latent_t, audio_t = temporal_shape(length)
    video = torch.zeros([batch_size, 24, latent_t, height // 16, width // 16],
                        device=comfy.model_management.intermediate_device())
    audio = torch.zeros([batch_size, 32, 2, audio_t],
                        device=comfy.model_management.intermediate_device())
    return {"samples": comfy.nested_tensor.NestedTensor((video, audio))}, frame_count


class JZL_MiniMaxH3ReferenceToVideo(io.ComfyNode):
    """ref2va: prompt + reference images / videos / audio -> conditioning + AV latent.

    参考物按固定顺序进入呈现：图像 → 视频（每个视频音轨的 <Audio j> 标签紧挨在其
    <Video k> 之前）→ 独立音频。序号按类型从 1 计数，提示词中用 <Picture i> /
    <Video k> / <Audio j> 引用。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_MiniMaxH3ReferenceToVideo",
            description="<Picture i> / <Video k> / <Audio j> 参考条件编码（MiniMax H3 ref2va）。提示词中使用相同标签引用。",
            display_name="JZL - 🎬 MiniMax H3 参考编码",
            category="JZL/MiniMax",
            inputs=[
                io.Clip.Input("clip"),
                io.Vae.Input("vae"),
                io.Vae.Input("audio_vae"),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.Int.Input("width", default=1344, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("height", default=768, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("length", default=124, min=5, max=3600, step=17,
                    tooltip="24fps 帧数，吸附到模型 17k+5 网格（124 ≈ 5s，训练区间约 124-362）"),
                io.Combo.Input("ref_image_size", options=["match", "max"], default="match",
                    tooltip="参考图尺寸策略。match=按生成画布像素面积等比缩小；max=短边对齐 2048，身份保真度最高但更慢。参考 token 参与每个采样步，max 可能慢数倍。"),
                io.Float.Input("ref_scale", display_name="参考值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="仅 match 模式生效。参考图最终像素面积 = 生成画布面积 × 倍率（面积倍率，非分辨率倍率）。1.0=不放大，2.0=面积×2。越大保真度越高、越慢。"),
                io.Autogrow.Input("ref_images", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref_image", tooltip="参考图（若大于 2048 短边会被缩小，从不放大）"),
                        prefix="ref_image_", min=0, max=9)),
                io.Autogrow.Input("ref_videos", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref_video", tooltip="24fps 参考视频帧（2-15s）"),
                        prefix="ref_video_", min=0, max=3)),
                io.Autogrow.Input("ref_video_audios", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Audio.Input("ref_video_audio", tooltip="同序号参考视频的音轨"),
                        prefix="ref_video_audio_", min=0, max=3)),
                io.Autogrow.Input("ref_audios", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Audio.Input("ref_audio", tooltip="独立参考音频"),
                        prefix="ref_audio_", min=0, max=3)),
            ],
            outputs=[io.Conditioning.Output(display_name="positive"), io.Latent.Output()],
        )

    @staticmethod
    def _encode_ref_audio(audio_vae, audio):
        waveform = audio["waveform"]  # [B, C, L]
        sr = audio["sample_rate"]
        vae_sr = getattr(audio_vae, "audio_sample_rate", 32000)
        if sr != vae_sr:
            waveform = torchaudio.functional.resample(waveform, sr, vae_sr)
        z = audio_vae.encode(waveform[:1].movedim(1, -1))  # [1, 32, 2, T]
        return z, z.shape[-1]

    @classmethod
    def execute(cls, clip, vae, audio_vae, prompt, width, height, length, ref_image_size="match",
                ref_scale=1.0, ref_images=None, ref_videos=None, ref_video_audios=None, ref_audios=None) -> io.NodeOutput:
        latent, frame_count = _empty_av_latent(width, height, length)

        ref_items = []   # for the tokenizer presentation, in request order
        ref_blocks = []  # for the DiT payload, same order

        for img in (ref_images or {}).values():
            if img is None:
                continue
            h, w = img.shape[1], img.shape[2]
            if ref_image_size == "match":
                # aspect-preserving scale (down only) to the generation's pixel area
                # JZL 扩展：面积倍率 ref_scale（默认 1.0 = 官方行为）
                scale = min(1.0, math.sqrt(ref_scale * (width * height) / (w * h)))
            else:
                scale = min(1.0, REF_IMAGE_SHORT_EDGE / min(w, h))
            tw = max(CANVAS_MULTIPLE, round(w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            th = max(CANVAS_MULTIPLE, round(h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            resized = _resize(img[:1], tw, th, "disabled")
            z = vae.encode(resized)
            ref_items.append({"type": "image", "data": resized})
            ref_blocks.append({"kind": "image", "latent_h": th // 16, "latent_w": tw // 16, "latent": z})

        ref_video_audios = ref_video_audios or {}
        for name, video_frames in (ref_videos or {}).items():
            if video_frames is None:
                continue
            # index-paired soundtrack: ref_video_audio_N belongs to ref_video_N
            soundtrack = ref_video_audios.get("ref_video_audio_" + name.rsplit("_", 1)[-1])
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
                raise ValueError("MiniMax H3 reference videos need at least 5 frames (~0.2s at 24 fps)")
            while n % 17 != 5:
                n -= 1
            frames = frames[:n]
            z = vae.encode(frames)
            audio_latent, ref_audio_t = (None, 0)
            if soundtrack is not None:
                audio_latent, ref_audio_t = cls._encode_ref_audio(audio_vae, soundtrack)
                # the soundtrack gets its own <Audio j> label, emitted before <Video k>
                ref_items.append({"type": "audio"})
            # Qwen sees the video at 2 fps with timestamps
            sample_idx = list(range(0, frames.shape[0], FPS // 2))
            qwen_frames = frames[sample_idx]
            ref_items.append({"type": "video", "data": qwen_frames,
                              "timestamps": [i / 2.0 for i in range(len(sample_idx))]})
            ref_blocks.append({"kind": "video_audio" if ref_audio_t else "video",
                               "latent_t": z.shape[2], "latent_h": ch // 16, "latent_w": cw // 16,
                               "ref_audio_t": ref_audio_t, "latent": z, "audio_latent": audio_latent})

        for audio in (ref_audios or {}).values():
            if audio is None:
                continue
            audio_latent, ref_audio_t = cls._encode_ref_audio(audio_vae, audio)
            ref_items.append({"type": "audio"})
            ref_blocks.append({"kind": "audio", "ref_audio_t": ref_audio_t, "audio_latent": audio_latent})

        tokens = clip.tokenize(prompt, minimax_ref_items=ref_items)
        cond = clip.encode_from_tokens_scheduled(tokens)
        if ref_blocks:
            cond = node_helpers.conditioning_set_values(cond, {"minimax_refs": ref_blocks})
        return io.NodeOutput(cond, latent)


class JZL_MiniMaxH3ReferenceToVideo2(io.ComfyNode):
    """ref2va 固定端口版：18 个写死的参考输入（9 图 + 3 视频 + 3 视频音轨 + 3 音频）。

    与 JZL_MiniMaxH3ReferenceToVideo 逻辑完全一致，仅把 Autogrow 动态端口改为
    固定端口，便于固定布线。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_MiniMaxH3ReferenceToVideo2",
            description="<Picture i> / <Video k> / <Audio j> 参考条件编码（MiniMax H3 ref2va，固定端口版）。提示词中使用相同标签引用。",
            display_name="JZL - 🎬 MiniMax H3 参考编码2",
            category="JZL/MiniMax",
            inputs=[
                io.Clip.Input("clip"),
                io.Vae.Input("vae"),
                io.Vae.Input("audio_vae"),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.Int.Input("width", default=1344, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("height", default=768, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("length", default=124, min=5, max=3600, step=17,
                    tooltip="24fps 帧数，吸附到模型 17k+5 网格（124 ≈ 5s，训练区间约 124-362）"),
                io.Combo.Input("ref_image_size", options=["match", "max"], default="match",
                    tooltip="参考图尺寸策略。match=按生成画布像素面积等比缩小；max=短边对齐 2048，身份保真度最高但更慢。参考 token 参与每个采样步，max 可能慢数倍。"),
                io.Float.Input("ref_scale", display_name="参考值放大", default=1.0, min=1.0, max=5.0, step=0.1,
                    tooltip="仅 match 模式生效。参考图最终像素面积 = 生成画布面积 × 倍率（面积倍率，非分辨率倍率）。1.0=不放大，2.0=面积×2。越大保真度越高、越慢。"),
                io.Image.Input("ref_image_0", optional=True),
                io.Image.Input("ref_image_1", optional=True),
                io.Image.Input("ref_image_2", optional=True),
                io.Image.Input("ref_image_3", optional=True),
                io.Image.Input("ref_image_4", optional=True),
                io.Image.Input("ref_image_5", optional=True),
                io.Image.Input("ref_image_6", optional=True),
                io.Image.Input("ref_image_7", optional=True),
                io.Image.Input("ref_image_8", optional=True),
                io.Image.Input("ref_video_0", optional=True),
                io.Image.Input("ref_video_1", optional=True),
                io.Image.Input("ref_video_2", optional=True),
                io.Audio.Input("ref_video_audio_0", optional=True),
                io.Audio.Input("ref_video_audio_1", optional=True),
                io.Audio.Input("ref_video_audio_2", optional=True),
                io.Audio.Input("ref_audio_0", optional=True),
                io.Audio.Input("ref_audio_1", optional=True),
                io.Audio.Input("ref_audio_2", optional=True),
            ],
            outputs=[io.Conditioning.Output(display_name="positive"), io.Latent.Output()],
        )

    @classmethod
    def execute(cls, clip, vae, audio_vae, prompt, width, height, length, ref_image_size="match",
                ref_scale=1.0,
                ref_image_0=None, ref_image_1=None, ref_image_2=None, ref_image_3=None,
                ref_image_4=None, ref_image_5=None, ref_image_6=None, ref_image_7=None, ref_image_8=None,
                ref_video_0=None, ref_video_1=None, ref_video_2=None,
                ref_video_audio_0=None, ref_video_audio_1=None, ref_video_audio_2=None,
                ref_audio_0=None, ref_audio_1=None, ref_audio_2=None) -> io.NodeOutput:
        # 固定端口 → 组装成与原 Autogrow 相同的 dict 结构，复用原节点核心逻辑
        ref_images = {}
        for name, v in (
            ("ref_image_0", ref_image_0), ("ref_image_1", ref_image_1),
            ("ref_image_2", ref_image_2), ("ref_image_3", ref_image_3),
            ("ref_image_4", ref_image_4), ("ref_image_5", ref_image_5),
            ("ref_image_6", ref_image_6), ("ref_image_7", ref_image_7),
            ("ref_image_8", ref_image_8),
        ):
            if v is not None:
                ref_images[name] = v
        ref_videos = {}
        for name, v in (("ref_video_0", ref_video_0), ("ref_video_1", ref_video_1), ("ref_video_2", ref_video_2)):
            if v is not None:
                ref_videos[name] = v
        ref_video_audios = {}
        for name, v in (
            ("ref_video_audio_0", ref_video_audio_0), ("ref_video_audio_1", ref_video_audio_1),
            ("ref_video_audio_2", ref_video_audio_2),
        ):
            if v is not None:
                ref_video_audios[name] = v
        ref_audios = {}
        for name, v in (("ref_audio_0", ref_audio_0), ("ref_audio_1", ref_audio_1), ("ref_audio_2", ref_audio_2)):
            if v is not None:
                ref_audios[name] = v

        return JZL_MiniMaxH3ReferenceToVideo.execute(
            clip, vae, audio_vae, prompt, width, height, length,
            ref_image_size=ref_image_size, ref_scale=ref_scale,
            ref_images=ref_images, ref_videos=ref_videos,
            ref_video_audios=ref_video_audios, ref_audios=ref_audios)


class JZL_MiniMaxH3CondSync(io.ComfyNode):
    """MiniMax H3 二采条件同步。

    从放大后的 latent 自动读取目标空间尺寸，把 positive 里「首尾帧」
    （minimax_keyframes）的 latent 对齐到该尺寸；纯文本（t2va）与参考图/
    视频（ref2va 的 minimax_refs）原样透传。

    对齐策略（按优先级）：
      1. 接了 first_frame / last_frame 原图时，直接在目标分辨率编码原始像素，
         与二段原生编码逐值一致，零色偏；
      2. 没接原图时退回「解码→像素放大→重编码」的 VAE 往返。

    用于 latent 放大后的二次采样：复用一段的文本 token 与参考条件，
    只对齐关键帧，免去二段重新编码 Qwen3-VL 文本。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_MiniMaxH3CondSync",
            description=(
                "二采条件同步：从 latent 自动读取目标分辨率，把 MiniMax H3 首尾帧"
                "（minimax_keyframes）的 latent 对齐到该尺寸；纯文本与参考图"
                "（minimax_refs）原样透传。接上首尾帧原图时按目标分辨率直接编码"
                "原始像素（零色偏），否则退回「解码→像素放大→重编码」的 VAE 往返。"
                "配合 latent 放大节点用于二次采样，免去重新编码文本。"
            ),
            display_name="JZL - 🌊 海螺H3二采条件同步",
            category="JZL/MiniMax",
            inputs=[
                io.Conditioning.Input("positive", tooltip="一段采样的 positive（含文本 token 与可选首尾帧/参考）"),
                io.Vae.Input("vae", tooltip="视频 VAE，用于在目标分辨率重新编码首尾帧（原图或解码→放大→重编码）"),
                io.Latent.Input("latent", tooltip="放大后的 AV latent，自动读 video 空间尺寸作为对齐目标"),
                io.Image.Input("first_frame", optional=True,
                    tooltip="一段用的首帧原图（首尾帧生视频时）。接上后按目标分辨率直接编码原始像素，"
                            "不接则退回解码→放大→重编码。"),
                io.Image.Input("last_frame", optional=True,
                    tooltip="一段用的尾帧原图（首尾帧生视频时）。接上后按目标分辨率直接编码原始像素，"
                            "不接则退回解码→放大→重编码。"),
            ],
            outputs=[
                io.Conditioning.Output(display_name="positive"),
                io.Latent.Output(display_name="latent",
                    tooltip="首帧修复后的 AV latent（video 第 0 帧已替换为对齐后的干净首帧）"),
            ],
        )

    @staticmethod
    def _read_target_size(latent):
        samples = latent.get("samples") if isinstance(latent, dict) else latent
        if isinstance(samples, comfy.nested_tensor.NestedTensor):
            video = samples.tensors[0]
        else:
            video = samples
        return int(video.shape[-2]), int(video.shape[-1])

    @staticmethod
    def _encode_keyframe(vae, image, width, height, crop):
        """在目标分辨率直接编码关键帧原图（无 VAE 往返，零色偏）。

        原图尺寸已经等于目标时整个跳过缩放：_resize 里的 comfy.utils.lanczos
        会做一次 float32→uint8→float32 往返（8-bit 来源无损，浮点来源误差
        可达 1/255），没必要为一次空缩放付这个代价。但那次往返同时隐含了
        [0, 1] 截断，所以跳过缩放时要自己 clamp，否则超出范围的来源
        （调色 / 混合 / EXR）会带着范围外的值进 vae.encode。
        """
        img = image[:1]
        if img.shape[1] == height and img.shape[2] == width:
            return vae.encode(img[..., :3].clamp(0.0, 1.0))
        return vae.encode(_resize(img, width, height, crop))

    @staticmethod
    def _reencode_keyframe(vae, z, new_h, new_w):
        # z: [1, 24, T, H/16, W/16] -> 高保真放大：解码回像素 → 像素放大 → 再编码
        pixels = vae.decode(z)
        if pixels.dim() == 5:
            pixels = pixels.reshape(-1, pixels.shape[-3], pixels.shape[-2], pixels.shape[-1])
        # pixels: [B, H, W, C]
        p = pixels[..., :3].movedim(-1, 1)  # [B, C, H, W]
        p = comfy.utils.common_upscale(p, new_w * 16, new_h * 16, "lanczos", "disabled")
        p = p.movedim(1, -1)  # [B, H', W', C]
        return vae.encode(p)

    @staticmethod
    def _repair_first_frame(latent, first_latent, new_h, new_w):
        """用对齐后的干净首帧替换 video latent 第 0 帧，修复 upscaler 时间维边界污染。

        只要 positive 里带首帧关键帧就会执行，与是否接了 first_frame 原图无关：
        接了原图时 first_latent 是目标分辨率的精确编码，没接时是重编码对齐的 latent。
        仅尺寸本就相等时保持原始 latent。
        """
        if first_latent is None:
            return latent
        samples = latent.get("samples") if isinstance(latent, dict) else latent
        if isinstance(samples, comfy.nested_tensor.NestedTensor):
            video = samples.tensors[0]
            if video.ndim < 5 or video.shape[-2] != new_h or video.shape[-1] != new_w:
                return latent
            video = video.clone()
            ff = first_latent.to(device=video.device, dtype=video.dtype)
            video[:, :, 0:1, :, :] = ff
            new_samples = comfy.nested_tensor.NestedTensor([video] + list(samples.tensors[1:]))
            return {**latent, "samples": new_samples} if isinstance(latent, dict) else new_samples
        if isinstance(samples, torch.Tensor) and samples.ndim >= 5:
            video = samples.clone()
            ff = first_latent.to(device=video.device, dtype=video.dtype)
            video[:, :, 0:1, :, :] = ff
            if isinstance(latent, dict):
                return {**latent, "samples": video}
            return {"samples": video}
        return latent

    @classmethod
    def execute(cls, positive, vae, latent, first_frame=None, last_frame=None) -> io.NodeOutput:
        new_h, new_w = cls._read_target_size(latent)
        first_latent = None  # 对齐后的首帧 latent，用于修复 video 第 0 帧

        out = []
        for item in positive:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                out.append(item)
                continue
            cond, params = item
            new_params = dict(params) if isinstance(params, dict) else params
            if isinstance(new_params, dict):
                kfs = new_params.get("minimax_keyframes")
                if kfs:
                    synced = []
                    for kf in kfs:
                        nkf = dict(kf)  # 复制，避免污染原 conditioning
                        z = nkf.get("latent")
                        if z is not None:
                            # resolved_frame_index 0 是首帧（几何锚点，直接拉伸），
                            # 其余是尾帧（跟随帧，居中裁切），与一段编码时的约定一致
                            is_first = nkf.get("resolved_frame_index") == 0
                            image, crop = (first_frame, "disabled") if is_first else (last_frame, "center")
                            if image is not None:
                                # 接了原图就一律走精确编码：同分辨率二采时结果与二段
                                # 原生编码逐值一致，比原样透传更能保证零色偏
                                nkf["latent"] = cls._encode_keyframe(vae, image, new_w * 16, new_h * 16, crop)
                            elif z.shape[-2] != new_h or z.shape[-1] != new_w:
                                # 没接原图时退回「解码→像素放大→重编码」的 VAE 往返
                                nkf["latent"] = cls._reencode_keyframe(vae, z, new_h, new_w)
                            # 第 0 帧修复只要有首帧关键帧就做，与是否接原图、是否
                            # 发生尺寸对齐都无关（尺寸本就相等时即原始 latent）
                            if is_first and first_latent is None:
                                first_latent = nkf["latent"]
                        synced.append(nkf)
                    new_params["minimax_keyframes"] = synced
            out.append([cond, new_params])

        repaired = cls._repair_first_frame(latent, first_latent, new_h, new_w)
        return io.NodeOutput(out, repaired)


class JZL_MiniMaxH3ImageToVideoDual(io.ComfyNode):
    """MiniMax H3 二采编码：一个节点同时输出一段 + 二段 positive。

    fl2va/t2va: prompt (+ 可选首尾帧) -> 一段 conditioning + AV latent + 二段 conditioning。
    二段分辨率 = 一段分辨率 × upscale_scale（对齐 32），二段重新编码文本与首尾帧视觉 token，
    用于 latent 放大后的二次采样，避免复用低分辨率 positive 导致的首帧错位。
    """

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="JZL_MiniMaxH3ImageToVideoDual",
            description=(
                "MiniMax H3 二采编码（fl2va/t2va）：一次输出一段 positive + 空 latent + 二采 positive。"
                "二采按 upscale_scale 放大分辨率（对齐 32）并重新编码文本与首尾帧视觉 token，"
                "用于 latent 放大后的二次采样，避免复用低分辨率 positive 导致的首帧错位。"
            ),
            display_name="JZL - 🎬 MiniMax H3 二采编码",
            category="JZL/MiniMax",
            inputs=[
                io.Clip.Input("clip"),
                io.Vae.Input("vae"),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.Int.Input("width", default=1344, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("height", default=768, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("length", default=124, min=5, max=3600, step=17,
                    tooltip="24fps 帧数，吸附到模型 17k+5 网格（124 ≈ 5s，训练区间约 124-362）"),
                io.Image.Input("first_frame", optional=True, tooltip="首帧图（图生视频）"),
                io.Image.Input("last_frame", optional=True, tooltip="尾帧图（首尾帧生视频）"),
                io.Float.Input("upscale_scale", display_name="二采放大倍数", default=1.0, min=1.0, max=4.0, step=0.05,
                    tooltip="二采分辨率 = 一段分辨率 × 倍数（对齐 32，需与 latent 放大节点的 align 一致）。1.0 = 不二采。"),
            ],
            outputs=[
                io.Conditioning.Output(display_name="positive"),
                io.Latent.Output(),
                io.Conditioning.Output(display_name="positive_2"),
            ],
        )

    @classmethod
    def _encode(cls, clip, vae, prompt, width, height, length, first_frame, last_frame):
        """复刻官方 MiniMaxH3ImageToVideo 的编码逻辑。"""
        latent, frame_count = _empty_av_latent(width, height, length)

        images = []
        keyframes = []
        if first_frame is not None:
            img = _resize(first_frame[:1], width, height, "disabled")
            images.append(img)
            keyframes.append({"resolved_frame_index": 0, "image": img})
        if last_frame is not None:
            img = _resize(last_frame[:1], width, height, "center")
            images.append(img)
            keyframes.append({"resolved_frame_index": frame_count - 1, "image": img})

        tokens = clip.tokenize(prompt, images=images)
        cond = clip.encode_from_tokens_scheduled(tokens)

        if keyframes:
            for kf in keyframes:
                kf["latent"] = vae.encode(kf.pop("image"))
            cond = node_helpers.conditioning_set_values(cond, {
                "minimax_keyframes": keyframes,
                "minimax_frame_count": frame_count,
            })
        return cond, latent

    @classmethod
    def execute(cls, clip, vae, prompt, width, height, length,
                first_frame=None, last_frame=None, upscale_scale=1.0) -> io.NodeOutput:
        cond, latent = cls._encode(clip, vae, prompt, width, height, length, first_frame, last_frame)

        if upscale_scale > 1.0:
            w2 = max(CANVAS_MULTIPLE, round(width * upscale_scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            h2 = max(CANVAS_MULTIPLE, round(height * upscale_scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            cond2, _ = cls._encode(clip, vae, prompt, w2, h2, length, first_frame, last_frame)
        else:
            cond2 = cond

        return io.NodeOutput(cond, latent, cond2)
