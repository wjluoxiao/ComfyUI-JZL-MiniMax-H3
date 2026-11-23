"""H3 联合窗口采样：一次采样跑完整片，逐窗口预测后在重叠区混合。

为什么必须这样（而不是「分段生成 → 拼接」）：
  · 分段生成的每一段都要「解码 → VAE 重编码 → 下一段」，段与段之间的身份、色温、锐度会
    逐段累积漂移；而且每段都要重新走一遍 VAE / 文本编码器 / UNET，在 16G 卡上会让
    32B 文本编码器被反复换出换入 —— 表现就是「第一段正常、第二段卡死」。
  · 这里改成：整片 latent **一次采样**；每一步把整片切成若干带重叠的窗口，
    每个窗口用它自己的条件（参考视频按窗口切片 + 参考图 + 提示词）预测，
    再把各窗口的预测按线性斜坡加权平均（权重和为 1）。窗口之间**不传递任何
    已解码结果**，所以质量不会逐段复合劣化。

几何约束（沿用官方 H3 的 17 帧网格）：
  · 一段 5 + 17N 帧 = 2 + 5N 个视频 latent token；
  · 每个 17 帧是一个 VAE 组（token 跨度 1,4,4,4,4）；
  · 所以窗口必须从 17 帧的整数倍开始、长度为 5 + 17M 帧，这样每个窗口都和
    「单独生成一段」有完全相同的 token 布局；
  · 音频 40 token/秒 = 每视频帧 5/3 个 token。
"""

import math

import torch

GROUP_FRAMES = 17
GROUP_TOKENS = 5
AUDIO_PER_FRAME = 40.0 / 24.0
# 条件里携带「整套窗口」的键名（我们自己用，不与别的插件冲突）
WINDOWS_KEY = "jzl_h3_windows"
# 最后一个窗口至少这么长（5+17k，约 2 秒）
MIN_LAST_WINDOW_FRAMES = 56


def groups_of(frames):
    """一段 5+17N 帧的 17 帧组数 N。"""
    frames = int(frames)
    if frames < 5 or (frames - 5) % GROUP_FRAMES:
        raise ValueError(f"帧数 {frames} 不是 5+17N（H3 只支持这种长度）")
    return (frames - 5) // GROUP_FRAMES


def frames_of(groups):
    return 5 + GROUP_FRAMES * int(groups)


def video_tokens(frames):
    return 2 + GROUP_TOKENS * groups_of(frames)


def audio_tokens(frames):
    return int(round(int(frames) * AUDIO_PER_FRAME))


def overlap_groups_of(overlap_frames):
    """重叠 5+17k 帧折成 k（22→1，39→2，56→3）。"""
    return max(1, int(math.ceil((int(overlap_frames) - 5) / float(GROUP_FRAMES))))


class Window:
    """一个窗口；帧号相对整片起点，token 号相对整片 latent。"""

    __slots__ = ("frame_start", "frames", "video_start", "video_len", "audio_start", "audio_len")

    def __init__(self, frame_start, frames, video_start, video_len, audio_start, audio_len):
        self.frame_start = int(frame_start)
        self.frames = int(frames)
        self.video_start = int(video_start)
        self.video_len = int(video_len)
        self.audio_start = int(audio_start)
        self.audio_len = int(audio_len)

    @property
    def video_end(self):
        return self.video_start + self.video_len

    @property
    def audio_end(self):
        return self.audio_start + self.audio_len

    def __repr__(self):
        return (f"Window(帧 {self.frame_start}..{self.frame_start + self.frames}"
                f"｜视频 token {self.video_start}..{self.video_end}"
                f"｜音频 {self.audio_start}..{self.audio_end})")


def _window(frame_start, frames, total_frames):
    return Window(
        frame_start, frames,
        GROUP_TOKENS * (int(frame_start) // GROUP_FRAMES), video_tokens(frames),
        _audio_start(frame_start, frames, total_frames), audio_tokens(frames),
    )


def _audio_start(frame_start, frames, total_frames):
    """窗口的第一个音频 token。

    结束于整片末尾的窗口直接取末尾那段音频：它的起点向下取整后，可能把整片
    最后几个 token 落到任何窗口之外。
    """
    full_audio, length_a = audio_tokens(total_frames), audio_tokens(frames)
    if int(frame_start) + int(frames) >= int(total_frames):
        return full_audio - length_a
    return min(int(round(int(frame_start) * AUDIO_PER_FRAME)), full_audio - length_a)


def plan_windows(total_frames, window_frames, overlap_frames):
    """把整片按 17 帧网格切成带重叠的窗口，从第 0 帧铺到最后一帧。

    每个窗口长 ``window_frames``（向下对齐到 5+17M），起点比上一个窗口的结尾早
    ``overlap_frames``（5+17k），因此相邻窗口正好重叠这么多。最后一个窗口只铺到
    片尾；如果那样会短于 MIN_LAST_WINDOW_FRAMES，就让它提前开始（重叠更多）。
    整片不长于一个窗口时就是单窗口。
    """
    total = int(total_frames)
    total_groups = groups_of(total)
    window_groups = max(2, (max(5, int(window_frames)) - 5) // GROUP_FRAMES)
    overlap_groups = overlap_groups_of(overlap_frames)
    if overlap_groups >= window_groups:
        raise ValueError(
            f"重叠 {overlap_frames} 帧必须小于窗口 {frames_of(window_groups)} 帧"
            f"（按 17 帧对齐后）。请调大「每段时长」或调小「重叠帧数」。")
    if total_groups <= window_groups:
        return [_window(0, total, total)]
    stride = window_groups - overlap_groups
    shortest_last = max(groups_of(MIN_LAST_WINDOW_FRAMES), overlap_groups + 1)
    spans, start = [], 0
    while start + window_groups < total_groups:
        spans.append((start, window_groups))
        start += stride
    if total_groups - start < shortest_last:
        start = total_groups - shortest_last
        if spans:
            start = max(start, spans[-1][0] + 1)
    spans.append((start, total_groups - start))
    return [_window(GROUP_FRAMES * s, frames_of(n), total) for s, n in spans]


def sub_windows(windows, first, last):
    """取 [first, last] 这几个窗口，重定位成一个能独立采样的窗口集。

    重跑单个窗口时用：把「窗口 k−1 / k / k+1」当成一小段独立的片子来算，
    左右各带一个邻居是为了重叠区还能正常混合。返回 (窗口列表, 新总帧数)。
    """
    sel = list(windows[int(first):int(last) + 1])
    if not sel:
        raise ValueError("重跑窗口的范围为空")
    base_frame = sel[0].frame_start
    base_v = sel[0].video_start
    base_a = sel[0].audio_start
    out = [Window(w.frame_start - base_frame, w.frames, w.video_start - base_v, w.video_len,
                  w.audio_start - base_a, w.audio_len) for w in sel]
    total = sel[-1].frame_start + sel[-1].frames - base_frame
    groups_of(total)          # 校验：重定位后仍然是 5+17N
    return out, total


def plan_decode_chunks(total_frames, chunk_frames=1200, pad_groups=2):
    """把整片按 17 帧组切成若干「解码块」（长片分组解码用，避免整片一次性解成 IMAGE）。

    每块带 ``pad_groups`` 组前文上下文：VAE 是时序因果的，从中间直接解会出现开头几帧发灰，
    所以往前多解 ``pad_groups × 17`` 帧再丢掉，块与块之间就不会有接缝。

    返回 dict 列表（字段全部是「整片 latent 坐标系」）：
      video/audio: 要喂给解码的 token 区间；drop_frames: 开头丢多少帧；
      keep_frames: 这一块实际贡献多少帧；frame_start: 这一块在源片里的起始帧。
    """
    total = int(total_frames)
    groups = groups_of(total)
    per = max(1, (max(5, int(chunk_frames)) - 5) // GROUP_FRAMES)
    n_chunks = max(1, int(math.ceil(groups / float(per))))
    per = max(1, int(math.ceil(groups / float(n_chunks))))     # 均分，避免最后一块只剩几帧
    c = float(AUDIO_PER_FRAME)
    out, a = [], 0
    while a < groups:
        b = min(groups, a + per)
        pad = min(int(pad_groups), a)
        ctx = GROUP_FRAMES * pad
        body = frames_of(b - a)
        f_start = GROUP_FRAMES * a
        keep = GROUP_FRAMES * (b - a) if b < groups else body
        out.append({
            "video": (GROUP_TOKENS * (a - pad), GROUP_TOKENS * a + video_tokens(body)),
            "audio": (int(round(GROUP_FRAMES * (a - pad) * c)),
                      int(round((f_start + keep) * c))),
            "drop_frames": ctx,
            "keep_frames": keep,
            "frame_start": f_start,
            "audio_drop": int(round(f_start * c)) - int(round(GROUP_FRAMES * (a - pad) * c)),
        })
        a = b
    return out


class _WindowProgress:
    """把采样循环的 tqdm 换成「按窗口计数」：进度条 1 格 = 1 次模型前向。

    采样器只知道自己走了几步（4 步），但每一步内部其实算了 N 个窗口，
    所以进度条总数 = 步数 × N，每算完一个窗口就 +1 格。
    """

    def __init__(self, steps, n_win, tag="人物替换", disable=False):
        from tqdm.auto import tqdm
        self.tag = tag
        self.steps = max(1, int(steps))
        self.n_win = max(1, int(n_win))
        self.done = 0
        self.step = 0
        self.bar = tqdm(total=self.steps * self.n_win, smoothing=1.0, disable=bool(disable),
                        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")

    def __iter__(self):
        for i in range(self.steps):
            self.step = i
            self._set_note(0)
            yield i
            target = (i + 1) * self.n_win
            if self.done < target:                       # 兜底：窗口没 tick 满就补齐
                self.bar.update(target - self.done)
                self.done = target

    def _set_note(self, window_index):
        seg = f"段 {window_index}/{self.n_win}" if window_index else f"{self.n_win} 段并行"
        try:
            self.bar.set_postfix_str(f"步 {self.step + 1}/{self.steps}｜{seg}", refresh=False)
        except Exception:
            pass

    def tick(self, window_index):
        self.done += 1
        self._set_note(window_index)
        self.bar.update(1)

    def write(self, text):
        try:
            self.bar.write(text)
        except Exception:
            print(text, flush=True)

    def set_postfix_str(self, text, refresh=True):
        try:
            self.bar.set_postfix_str(text, refresh=refresh)
        except Exception:
            pass

    def close(self):
        try:
            self.bar.close()
        except Exception:
            pass


def install_window_progress(guider):
    """把 ``comfy.k_diffusion.sampling.trange`` 临时换成按窗口计数的进度条。返回恢复函数。"""
    try:
        import comfy.k_diffusion.sampling as _kds
    except Exception:
        return lambda: None
    n_win = len(guider.runs)
    holder = []

    def _factory(*args, **kwargs):
        steps = int(args[0]) if args else int(kwargs.get("total", 1))
        bar = _WindowProgress(steps, n_win, tag=guider.tag,
                              disable=bool(kwargs.get("disable", False)))
        holder.append(bar)
        guider._bar = bar
        if n_win > 1:
            # 只说明「每步按顺序算哪几段」，不再重复总数（节点侧已经打过「采样计划」，重复容易被读成段数）
            bar.write(f"[JZL-{guider.tag}]   每步按顺序算 {n_win} 段："
                      + " → ".join(f"段{k + 1}" for k in range(n_win)))
        return bar

    original = getattr(_kds, "trange", None)
    _kds.trange = _factory

    def _restore():
        if original is not None:
            _kds.trange = original
        for bar in holder:
            bar.close()
        guider._bar = None
    return _restore


def describe(windows, total_frames):
    """一行日志：窗口数 + 每个窗口的帧区间。"""
    parts = []
    for w in windows:
        parts.append(f"{w.frame_start}+{w.frames}")
    return (f"{len(windows)} 个窗口（起点+长度）：{' / '.join(parts)}"
            f"｜整片 {total_frames} 帧")


def _ramp(length, overlap, rising):
    """``length`` 个权重，在前/后 ``overlap`` 个位置上做线性斜坡。

    斜坡取值 (i+1)/(overlap+1)：永远不为 0，且同长度的升/降斜坡相加恰好为 1。
    """
    weights = torch.ones(int(length), dtype=torch.float32)
    overlap = max(0, min(int(overlap), int(length)))
    if overlap:
        ramp = torch.arange(1, overlap + 1, dtype=torch.float32) / (overlap + 1)
        if rising:
            weights[:overlap] = ramp
        else:
            weights[int(length) - overlap:] = ramp.flip(0)
    return weights


def window_weights(windows, stream):
    """每个窗口在时间轴上的混合权重（``stream`` = "video" / "audio"）。"""
    def bounds(window):
        if stream == "video":
            return window.video_start, window.video_end
        return window.audio_start, window.audio_end

    out = []
    for index, window in enumerate(windows):
        start, end = bounds(window)
        weights = torch.ones(end - start, dtype=torch.float32)
        if index > 0:
            weights = torch.minimum(weights, _ramp(end - start, bounds(windows[index - 1])[1] - start, True))
        if index + 1 < len(windows):
            weights = torch.minimum(weights, _ramp(end - start, end - bounds(windows[index + 1])[0], False))
        out.append(weights)
    return out


def blend(parts, windows, weights, total, stream, dim, buffers=None):
    """把各窗口的预测按权重放回整片时间轴并做加权平均。

    ``buffers``: 传 (acc, counter) 可复用同一块显存（每步都会调用，反复分配会碎片化）。
    """
    first = parts[0]
    shape = list(first.shape)
    shape[dim] = int(total)
    if buffers is not None:
        acc, counter = buffers
        acc.zero_()
        counter.zero_()
    else:
        acc = torch.zeros(shape, dtype=torch.float32, device=first.device)
        counter = torch.zeros(int(total), dtype=torch.float32, device=first.device)
    view = [1] * len(shape)
    for part, window, weight in zip(parts, windows, weights):
        start = window.video_start if stream == "video" else window.audio_start
        weight = weight.to(first.device)
        view[dim] = int(weight.numel())
        acc.narrow(dim, start, int(part.shape[dim])).add_(part.float() * weight.view(view))
        counter[start:start + int(part.shape[dim])] += weight
    if bool((counter <= 0).any()):
        raise ValueError("有帧位置没有被任何窗口覆盖（窗口规划有洞）")
    view[dim] = int(total)
    return (acc / counter.view(view)).to(first.dtype)


class WindowSet:
    """一次采样需要的全部东西：窗口 + 每窗口的条件 + 整片长度。"""

    __slots__ = ("windows", "conditionings", "total_frames")

    def __init__(self, windows, conditionings, total_frames):
        self.windows = list(windows)
        self.conditionings = list(conditionings)
        self.total_frames = int(total_frames)
        if len(self.windows) != len(self.conditionings):
            raise ValueError("窗口数与条件数不一致")

    @property
    def video_total(self):
        return video_tokens(self.total_frames)

    @property
    def audio_total(self):
        return audio_tokens(self.total_frames)


def windowed_conditioning(window_set):
    """把整套窗口挂到第一个窗口的条件上（采样器从这里读到它）。"""
    import node_helpers
    return node_helpers.conditioning_set_values(window_set.conditionings[0], {WINDOWS_KEY: window_set})


def window_set_of(conditioning):
    for entry in conditioning or ():
        if isinstance(entry, (list, tuple)) and len(entry) > 1 and isinstance(entry[1], dict):
            found = entry[1].get(WINDOWS_KEY)
            if isinstance(found, WindowSet):
                return found
    return None


def make_guider(model, conditioning, cfg=1.0, negative=None, tag="人物替换"):
    """构造「每次模型调用 = 逐窗口预测 + 混合」的 guider。"""
    import comfy.model_patcher
    import comfy.patcher_extension
    import comfy.sampler_helpers
    import comfy.samplers
    import comfy.utils

    window_set = window_set_of(conditioning)
    if window_set is None:
        raise ValueError("条件里没有窗口计划：请用窗口模式构建条件")
    use_negative = negative is not None and len(negative or []) > 0 and float(cfg) > 1.0

    class JzlH3WindowGuider(comfy.samplers.CFGGuider):
        def __init__(self, model_patcher):
            super().__init__(model_patcher)
            self.model_options = comfy.model_patcher.create_model_options_clone(model_patcher.model_options)
            self.window_set = window_set
            self.tag = tag
            self.window_raw = []
            for cond in window_set.conditionings:
                entry = {"positive": cond}
                if use_negative:
                    entry["negative"] = negative
                self.window_raw.append(entry)
            self.set_cfg(float(cfg) if use_negative else 1.0)
            self.inner_set_conds(self.window_raw[0])   # 外层 cond 用第一个窗口的（只用于模型加载/hook）

            ratio = max(w.video_len for w in window_set.windows) / float(window_set.video_total)

            def window_memory(executor, model, noise_shape, conds, *args, **kwargs):
                # 显存按「单窗口」估算（视频占大头），否则会按整片估 → 直接判 OOM
                shape = list(noise_shape)
                shape[-1] = max(1, int(math.ceil(int(shape[-1]) * ratio)))
                return executor(model, tuple(shape), conds, *args, **kwargs)

            comfy.patcher_extension.add_wrapper_with_key(
                comfy.patcher_extension.WrappersMP.PREPARE_SAMPLING,
                "jzl_h3_windows", window_memory, self.model_options, is_model_options=True)

        def inner_sample(self, noise, latent_image, device, sampler, sigmas, denoise_mask, callback,
                         disable_pbar, seed, latent_shapes=None):
            if latent_shapes is None or len(latent_shapes) < 2:
                raise ValueError("窗口采样需要 H3 的音视频懒张量（NestedTensor）")
            self.full_shapes = latent_shapes
            video_shape, audio_shape = latent_shapes[0], latent_shapes[1]
            if (int(video_shape[2]) != self.window_set.video_total
                    or int(audio_shape[-1]) != self.window_set.audio_total):
                raise ValueError(
                    "latent 长度与窗口计划不一致："
                    f"latent 视频 {int(video_shape[2])} / 音频 {int(audio_shape[-1])}，"
                    f"计划 视频 {self.window_set.video_total} / 音频 {self.window_set.audio_total}")
            self.inner_model.latent_shapes = latent_shapes
            if latent_image is not None and torch.count_nonzero(latent_image) > 0:
                latent_image = self.inner_model.process_latent_in(latent_image)

            def cut(packed, window):
                if packed is None:
                    return None, None
                video, audio = comfy.utils.unpack_latents(packed, latent_shapes)
                return comfy.utils.pack_latents([
                    video[:, :, window.video_start:window.video_end],
                    audio[..., window.audio_start:window.audio_end],
                ])

            self.runs = []
            for raw, window in zip(self.window_raw, self.window_set.windows):
                w_noise, w_shapes = cut(noise, window)
                w_latent, _ = cut(latent_image, window)
                w_mask, _ = cut(denoise_mask, window)
                conds = {k: [dict(item) for item in comfy.sampler_helpers.convert_cond(v)]
                         for k, v in raw.items()}
                conds = comfy.samplers.process_conds(self.inner_model, w_noise, conds, device,
                                                     w_latent, w_mask, seed, latent_shapes=w_shapes)
                self.runs.append((window, conds, w_shapes))
            # 还原整片形状：音频缩放与 inpaint 都按整片算
            self.inner_model.latent_shapes = latent_shapes
            self.weights_v = window_weights(self.window_set.windows, "video")
            self.weights_a = window_weights(self.window_set.windows, "audio")
            # 混合缓冲区：第一次预测时按实际设备/形状分配，之后每步复用（避免显存碎片）
            self._buf_v = None
            self._buf_a = None
            self._bar = None
            self.conds = self.runs[0][1]

            extra_options = comfy.model_patcher.create_model_options_clone(self.model_options)
            extra_options.setdefault("transformer_options", {})["sample_sigmas"] = sigmas
            extra_args = {"model_options": extra_options, "seed": seed}
            executor = comfy.patcher_extension.WrapperExecutor.new_class_executor(
                sampler.sample, sampler,
                comfy.patcher_extension.get_all_wrappers(
                    comfy.patcher_extension.WrappersMP.SAMPLER_SAMPLE,
                    extra_args["model_options"], is_model_options=True))
            restore = install_window_progress(self)
            try:
                samples = executor.execute(self, sigmas, extra_args, callback, noise, latent_image,
                                           denoise_mask, disable_pbar)
            finally:
                restore()
            return self.inner_model.process_latent_out(samples.to(torch.float32))

        def predict_noise(self, x, timestep, model_options={}, seed=None):
            import comfy.utils
            video, audio = comfy.utils.unpack_latents(x, self.full_shapes)
            parts_v, parts_a = [], []
            for window, conds, shapes in self.runs:
                self.conds = conds
                packed, _ = comfy.utils.pack_latents([
                    video[:, :, window.video_start:window.video_end],
                    audio[..., window.audio_start:window.audio_end],
                ])
                out = comfy.samplers.sampling_function(
                    self.inner_model, packed, timestep, conds.get("negative"), conds.get("positive"),
                    self.cfg, model_options=model_options, seed=seed)
                out_v, out_a = comfy.utils.unpack_latents(out, shapes)
                parts_v.append(out_v)
                parts_a.append(out_a)
                del packed, out
                if self._bar is not None:
                    self._bar.tick(len(parts_v))
            windows = self.window_set.windows
            if self._buf_v is None:
                dev = parts_v[0].device
                vshape = list(parts_v[0].shape)
                vshape[2] = self.window_set.video_total
                ashape = list(parts_a[0].shape)
                ashape[-1] = self.window_set.audio_total
                self._buf_v = (torch.zeros(vshape, dtype=torch.float32, device=dev),
                               torch.zeros(self.window_set.video_total, dtype=torch.float32, device=dev))
                self._buf_a = (torch.zeros(ashape, dtype=torch.float32, device=dev),
                               torch.zeros(self.window_set.audio_total, dtype=torch.float32, device=dev))
            video = blend(parts_v, windows, self.weights_v, self.window_set.video_total, "video", 2,
                          buffers=self._buf_v)
            audio = blend(parts_a, windows, self.weights_a, self.window_set.audio_total, "audio",
                          audio.ndim - 1, buffers=self._buf_a)
            return comfy.utils.pack_latents([video, audio])[0]

    return JzlH3WindowGuider(model)


def make_sigmas(model, sampler_name, scheduler, steps, denoise):
    """按 KSampler 同一套规则算 sigmas（含 denoise 截断与 penultimate 丢弃）。"""
    import comfy.samplers
    sampler_name = sampler_name if sampler_name in comfy.samplers.KSampler.SAMPLERS \
        else comfy.samplers.KSampler.SAMPLERS[0]
    scheduler = scheduler if scheduler in comfy.samplers.KSampler.SCHEDULERS \
        else comfy.samplers.KSampler.SCHEDULERS[0]
    sampling = model.get_model_object("model_sampling")
    steps = max(1, int(steps))
    discard = sampler_name in comfy.samplers.KSampler.DISCARD_PENULTIMATE_SIGMA_SAMPLERS

    def calc(n):
        extra = 1 if discard else 0
        sig = comfy.samplers.calculate_sigmas(sampling, scheduler, n + extra)
        if discard:
            sig = torch.cat([sig[:-2], sig[-1:]])
        return sig

    denoise = float(denoise)
    if denoise >= 0.9999:
        sigmas = calc(steps)
    elif denoise <= 0.0:
        sigmas = torch.FloatTensor([])
    else:
        sigmas = calc(int(steps / denoise))[-(steps + 1):]
    return sigmas.to(model.load_device), sampler_name


def sample_windowed(model, conditioning, latent, seed, steps=4, cfg=1.0, sampler_name="res_multistep",
                    scheduler="simple", denoise=1.0, negative=None, disable_pbar=True, sigmas=None,
                    tag="人物替换"):
    """一次采样跑完整片（内部逐窗口预测 + 混合）。

    ``sigmas`` 给定时直接用（二采的显式 sigmas），否则按 KSampler 规则算。
    """
    import comfy.model_management
    import comfy.sample
    import comfy.samplers

    if sigmas is None:
        sigmas, sampler_name = make_sigmas(model, sampler_name, scheduler, steps, denoise)
    else:
        if sampler_name not in comfy.samplers.KSampler.SAMPLERS:
            sampler_name = "euler"
        sigmas = sigmas.to(model.load_device)
    if int(sigmas.numel()) < 2:
        raise ValueError("采样步数 / 降噪太小，sigmas 为空")
    latent_image = latent["samples"]
    noise = comfy.sample.prepare_noise(latent_image, int(seed), None)
    guider = make_guider(model, conditioning, cfg=cfg, negative=negative, tag=tag)
    sampler = comfy.samplers.sampler_object(sampler_name)
    samples = guider.sample(noise, latent_image, sampler, sigmas, denoise_mask=None,
                            callback=None, disable_pbar=disable_pbar, seed=int(seed))
    return samples.to(device=comfy.model_management.intermediate_device(),
                      dtype=comfy.model_management.intermediate_dtype())
