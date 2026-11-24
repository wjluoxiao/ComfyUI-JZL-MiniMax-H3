import { app } from "../../scripts/app.js";

// ============================================================
// JZL - 📥 MiniMax 素材加载
//   把「素材库」子工作流（9 图 + 3 音频 + 3 视频）收成一个节点：
//     上：3×3 九宫格参考图（正方形预览，长边自适应）
//     中：3 个音频（预览窗 宽:高 = 2:1，带播放进度条 + 音频设置）
//     下：3 个视频（正方形预览 + 视频设置）
//   所有配置都存进 media_settings（JSON 字符串 widget）→ 随工作流保存，
//   后端 nodes_h3_media_loader.py 解析后打包成 JZL_REF2VA_BUS。
// ============================================================

const NODE_TYPE = "JZL_MiniMaxH3MediaLoader";
const SETTINGS_WIDGET = "media_settings";
const DOM_WIDGET_NAME = "jzl_h3_media";

const UPLOAD_ENDPOINT = "/jzl/upload_asset";
const IMAGE_PREVIEW = "/jzl/asset_preview";
const AUDIO_PREVIEW = "/jzl/audio_preview";
const VIDEO_PREVIEW = "/jzl/video_preview";
const VIDEO_THUMB = "/jzl/asset_video_thumb";

const IMG_COUNT = 9;
const AUDIO_COUNT = 3;
const VIDEO_COUNT = 3;

// 三个模块：顺序可拖拽调整，数量可选，可整块隐藏（至少留一个）
const KIND_ORDER = ["image", "audio", "video"];
const KIND_META = {
    image: { icon: "🖼️", label: "参考图", ratio: "1 / 1", options: [1, 2, 4, 6, 9], max: 9 },
    audio: { icon: "🎧", label: "参考音频", ratio: "2 / 1", options: [1, 2, 3], max: 3 },
    video: { icon: "🎬", label: "参考视频", ratio: "1 / 1", options: [1, 2, 3], max: 3 },
};

const ACCEPT = {
    image: "image/*",
    audio: "audio/*",
    video: "video/*",
};

const VIDEO_FIELDS = [
    {
        key: "force_rate", label: "强制帧率", step: 0.01, min: 0, max: 240, unit: "fps",
        hint: "0 = 保持原帧率（与「XB-BOX 加载视频」一致）",
    },
    {
        key: "skip", label: "跳过帧数", step: 1, min: 0, unit: "帧",
        hint: "在改帧率之后计数，从第 N 帧开始取（视频音轨同步从对应时间开始）",
    },
    {
        key: "width", label: "输出宽度", step: 2, min: 0, unit: "px",
        hint: "0 = 保持原尺寸；只填一边时另一边按比例并偶数对齐",
    },
    {
        key: "height", label: "输出高度", step: 2, min: 0, unit: "px",
        hint: "0 = 保持原尺寸",
    },
    {
        key: "cap", label: "帧数上限", step: 1, min: 0, unit: "帧",
        hint: "0 = 不限制（后端另有 6000 帧安全上限）；视频音轨同步裁到相同长度",
    },
];

const AUDIO_FIELDS = [
    {
        key: "start", label: "开始时间", step: 0.01, min: 0, unit: "秒",
        hint: "步长 0.01 秒；◀ ▶ 微调（按住连续调）",
    },
    {
        key: "duration", label: "音频时长", step: 0.01, min: 0, unit: "秒",
        hint: "0 = 取到结尾；步长 0.01 秒；◀ ▶ 微调（按住连续调）",
    },
];

// ── 小工具 ────────────────────────────────────────────────

function el(tag, css, text) {
    const node = document.createElement(tag);
    if (css) node.style.cssText = css;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function readWidgetValue(w) {
    return w ? (w._state?.value ?? w.value) : undefined;
}

function setWidgetValue(node, w, value) {
    if (!w) return;
    w.value = value;
    if (w._state) w._state.value = value;
    node?.graph?.setDirtyCanvas?.(true, true);
}

function toNum(v, fallback = 0) {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
}

function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
}

function fmtTime(sec) {
    const s = Number.isFinite(sec) && sec > 0 ? sec : 0;
    const m = Math.floor(s / 60);
    const rest = s - m * 60;
    return `${m}:${rest < 10 ? "0" : ""}${rest.toFixed(2)}`;
}

function trimNum(v) {
    // 0.5 → "0.5"，10 → "10"（对话框回填用）
    return String(Math.round(Number(v) * 1000) / 1000);
}

// 步长的小数位数：0.01 → 2，2 → 0
function stepDecimals(step) {
    const s = String(step ?? 1);
    const i = s.indexOf(".");
    return i < 0 ? 0 : (s.length - i - 1);
}

// 隐藏 number 输入框自带的上下小三角（改用左右按钮微调）
function ensureStepperStyle() {
    if (document.getElementById("jzl-h3m-stepper-style")) return;
    const style = document.createElement("style");
    style.id = "jzl-h3m-stepper-style";
    style.textContent = [
        ".h3m-num::-webkit-outer-spin-button,",
        ".h3m-num::-webkit-inner-spin-button{-webkit-appearance:none;margin:0;}",
        ".h3m-num{-moz-appearance:textfield;appearance:textfield;}",
    ].join("");
    document.head.appendChild(style);
}

// 按住不放连续微调（调时长/帧率很省手）
function bindHoldRepeat(btn, step) {
    let delayTimer = null;
    let repeatTimer = null;
    const stop = () => {
        clearTimeout(delayTimer);
        clearInterval(repeatTimer);
        delayTimer = null;
        repeatTimer = null;
    };
    // pointer capture：光标移出按钮也能收到 pointerup，不需要往 window 上挂监听
    btn.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        try { btn.setPointerCapture?.(e.pointerId); } catch (_) {}
        step();
        delayTimer = setTimeout(() => {
            repeatTimer = setInterval(step, 90);
        }, 420);
    });
    ["pointerup", "pointercancel", "lostpointercapture", "blur"].forEach(
        (ev) => btn.addEventListener(ev, stop)
    );
    return stop;
}

let toastHost = null;
function notify(msg, isError = false) {
    try {
        if (!toastHost) {
            toastHost = el("div", [
                "position:fixed", "right:18px", "bottom:18px", "z-index:100000",
                "display:flex", "flex-direction:column", "gap:8px", "pointer-events:none",
            ].join(";"));
            document.body.appendChild(toastHost);
        }
        const box = el("div", [
            "max-width:420px", "padding:9px 14px", "border-radius:8px",
            `background:${isError ? "rgba(150,40,40,.94)" : "rgba(30,90,60,.94)"}`,
            "color:#fff", "font-size:12px", "line-height:1.45",
            "box-shadow:0 6px 20px rgba(0,0,0,.45)", "transition:opacity .25s",
        ].join(";"), msg);
        toastHost.appendChild(box);
        setTimeout(() => { box.style.opacity = "0"; }, 2400);
        setTimeout(() => { box.remove(); }, 2800);
    } catch (_) { /* ignore */ }
}

// ── 配置（与后端 parse_settings 一一对应） ─────────────────

function defaultSettings() {
    return {
        version: 2,
        layout: {
            order: [...KIND_ORDER],
            counts: { image: IMG_COUNT, audio: AUDIO_COUNT, video: VIDEO_COUNT },
            hidden: { image: false, audio: false, video: false },
        },
        images: Array.from({ length: IMG_COUNT }, () => ({ path: "", enabled: true })),
        audios: Array.from({ length: AUDIO_COUNT }, () => ({
            path: "", enabled: true, start: 0, duration: 0,
        })),
        videos: Array.from({ length: VIDEO_COUNT }, () => ({
            path: "", enabled: true, force_rate: 0, skip: 0, width: 0, height: 0, cap: 0,
        })),
    };
}

// 布局归一化：顺序必须是三个模块的一个排列；数量只能是允许档位；至少保留一个模块可见
function normalizeLayout(raw) {
    const src = (raw && typeof raw === "object") ? raw : {};

    let order = Array.isArray(src.order) ? src.order.filter((k) => KIND_ORDER.includes(k)) : [];
    order = [...new Set(order)];
    for (const k of KIND_ORDER) if (!order.includes(k)) order.push(k);
    if (order.length !== KIND_ORDER.length) order = [...KIND_ORDER];

    const rawCounts = (src.counts && typeof src.counts === "object") ? src.counts : {};
    const counts = {};
    for (const k of KIND_ORDER) {
        const opts = KIND_META[k].options;
        const n = clamp(Math.round(toNum(rawCounts[k], opts[opts.length - 1])), opts[0], opts[opts.length - 1]);
        // 吸附到最近的合法档位（如 3 → 2、7 → 6），不要粗暴退回最大值
        counts[k] = opts.reduce((best, o) => (Math.abs(o - n) < Math.abs(best - n) ? o : best), opts[0]);
    }

    const rawHidden = (src.hidden && typeof src.hidden === "object") ? src.hidden : {};
    const hidden = {};
    for (const k of KIND_ORDER) hidden[k] = rawHidden[k] === true;
    if (KIND_ORDER.every((k) => hidden[k])) hidden[order[0]] = false;   // 至少保留一个

    return { order, counts, hidden };
}

function normalizeSettings(raw) {
    const base = defaultSettings();
    let src = raw;
    if (typeof raw === "string") {
        try { src = raw.trim() ? JSON.parse(raw) : {}; } catch (_) { src = {}; }
    }
    if (!src || typeof src !== "object") src = {};

    base.layout = normalizeLayout(src.layout);

    const fill = (list, defs, map) => {
        const arr = Array.isArray(list) ? list : [];
        return defs.map((_, i) => {
            const it = (arr[i] && typeof arr[i] === "object") ? arr[i] : {};
            return map(it);
        });
    };

    base.images = fill(src.images, base.images, (it) => ({
        path: String(it.path || ""),
        enabled: it.enabled !== false,
    }));
    base.audios = fill(src.audios, base.audios, (it) => ({
        path: String(it.path || ""),
        enabled: it.enabled !== false,
        start: Math.max(0, toNum(it.start, 0)),
        duration: Math.max(0, toNum(it.duration, 0)),
    }));
    base.videos = fill(src.videos, base.videos, (it) => ({
        path: String(it.path || ""),
        enabled: it.enabled !== false,
        force_rate: Math.max(0, toNum(it.force_rate, 0)),
        skip: Math.max(0, Math.round(toNum(it.skip, 0))),
        width: Math.max(0, Math.round(toNum(it.width, 0))),
        height: Math.max(0, Math.round(toNum(it.height, 0))),
        cap: Math.max(0, Math.round(toNum(it.cap, 0))),
    }));
    return base;
}

function getSettingsWidget(node) {
    return (node.widgets || []).find((w) => w.name === SETTINGS_WIDGET) || null;
}

function readSettings(node) {
    const st = node.__h3m;
    if (st && st.settings) return st.settings;
    const w = getSettingsWidget(node);
    const s = normalizeSettings(readWidgetValue(w));
    if (st) st.settings = s;
    return s;
}

function persistSettings(node) {
    const st = node.__h3m;
    if (!st) return;
    setWidgetValue(node, st.widget, JSON.stringify(st.settings));
}

// 当前布局（顺序 / 数量 / 是否隐藏）—— 会随工作流保存，不会被重绘冲掉
function getLayout(node) {
    const s = readSettings(node);
    if (!s.layout) s.layout = normalizeLayout(null);
    return s.layout;
}

// 网格列数：1/2 个一行铺满；4 个 2 列（2×2）；3/6/9 个 3 列
function gridColumns(count) {
    if (count <= 1) return 1;
    if (count === 2 || count === 4) return 2;
    return 3;
}

// ── 上传 / 预览地址 ───────────────────────────────────────

function previewUrl(kind, path) {
    if (!path) return "";
    const base = kind === "image" ? IMAGE_PREVIEW : (kind === "audio" ? AUDIO_PREVIEW : VIDEO_PREVIEW);
    return `${base}?path=${encodeURIComponent(path)}`;
}

// 视频首帧缩略图：浏览器解不了 HEVC/H.265 时兜底显示
function videoThumbUrl(path) {
    if (!path) return "";
    return `${VIDEO_THUMB}?path=${encodeURIComponent(path)}`;
}

function fileNameOf(path) {
    const s = String(path || "");
    const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
    return i >= 0 ? s.slice(i + 1) : s;
}

async function uploadFile(kind, file) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", kind);
    const res = await fetch(UPLOAD_ENDPOINT, { method: "POST", body: fd });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok || !data || !data.ok || !data.path) {
        throw new Error((data && data.error) || `上传失败（HTTP ${res.status}）`);
    }
    return data;
}

function pickFile(node, kind, slotIndex) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ACCEPT[kind] || "*/*";
    input.style.display = "none";
    document.body.appendChild(input);
    input.addEventListener("change", async () => {
        const file = input.files && input.files[0];
        input.remove();
        if (!file) return;
        applyUploadedFile(node, kind, slotIndex, file);
    });
    // 用户取消时不留垃圾节点
    input.addEventListener("cancel", () => input.remove());
    input.click();
}

async function applyUploadedFile(node, kind, slotIndex, file) {
    try {
        const info = await uploadFile(kind, file);
        const settings = readSettings(node);
        const list = settings[`${kind}s`];
        if (!list || !list[slotIndex]) return;
        list[slotIndex].path = info.path;
        list[slotIndex].enabled = true;   // 新素材默认启用
        persistSettings(node);
        renderSlot(node, kind, slotIndex);
        notify(`已载入 ${kind === "image" ? "图片" : kind === "audio" ? "音频" : "视频"}${slotIndex + 1}：${info.name || file.name}`);
    } catch (err) {
        notify(`上传失败：${err.message || err}`, true);
    }
}

function bindDropTarget(node, holder, kind, slotIndex) {
    holder.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.stopPropagation();
        holder.style.outline = "2px solid var(--jz-btn-primary)";
    });
    const clear = () => { holder.style.outline = "none"; };
    holder.addEventListener("dragleave", clear);
    holder.addEventListener("drop", (e) => {
        e.preventDefault();
        e.stopPropagation();
        clear();
        const file = e.dataTransfer?.files?.[0];
        if (file) applyUploadedFile(node, kind, slotIndex, file);
    });
}

// ── 通用小部件：编号 / 开关 / 清除 ─────────────────────────

function numberBadge(text) {
    return el("div", [
        "position:absolute", "left:5px", "top:5px", "z-index:3",
        "min-width:16px", "height:16px", "padding:0 4px", "box-sizing:border-box",
        "display:flex", "align-items:center", "justify-content:center",
        "border-radius:5px", "background:rgba(0,0,0,.55)", "color:#fff",
        "font-size:10px", "font-weight:600", "line-height:1", "pointer-events:none",
    ].join(";"), text);
}

function toggleSwitch(enabled, onChange) {
    const btn = el("button", [
        "position:absolute", "right:5px", "top:5px", "z-index:4",
        "width:32px", "height:17px", "padding:0", "border-radius:9px",
        "border:1px solid rgba(255,255,255,.5)", "cursor:pointer",
        "display:flex", "align-items:center", "transition:background .15s",
        "background:rgba(0,0,0,.45)",
    ].join(";"));
    btn.title = "开启 / 关闭本槽位";
    const knob = el("span", [
        "width:13px", "height:13px", "border-radius:50%", "background:#fff",
        "position:absolute", "left:2px", "transition:left .15s, background .15s",
        "box-shadow:0 1px 3px rgba(0,0,0,.5)",
    ].join(";"));
    btn.appendChild(knob);
    const paint = (on) => {
        btn.style.background = on ? "rgba(45,160,95,.85)" : "rgba(0,0,0,.45)";
        knob.style.left = on ? "16px" : "2px";
        knob.style.background = on ? "#fff" : "rgba(255,255,255,.72)";
    };
    paint(!!enabled);
    btn.addEventListener("mousedown", (e) => e.stopPropagation());
    btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const next = !btn.__h3mOn;
        btn.__h3mOn = next;
        paint(next);
        onChange(next);
    });
    btn.__h3mOn = !!enabled;
    return btn;
}

function clearButton(onClick) {
    const btn = el("button", [
        "position:absolute", "right:5px", "bottom:5px", "z-index:4",
        "width:18px", "height:18px", "padding:0", "border-radius:5px",
        "border:1px solid rgba(255,255,255,.35)", "background:rgba(0,0,0,.5)",
        "color:#fff", "font-size:11px", "line-height:1", "cursor:pointer",
        "opacity:0", "transition:opacity .15s",
    ].join(";"), "✕");
    btn.title = "清除本槽位素材";
    btn.addEventListener("mousedown", (e) => e.stopPropagation());
    btn.addEventListener("click", (e) => {
        e.stopPropagation();
        onClick();
    });
    return btn;
}

function slotShell(css) {
    const cell = el("div", [
        "position:relative", "border-radius:8px", "overflow:hidden",
        "border:1px solid var(--jz-border)", "background:var(--jz-inset)",
        "box-sizing:border-box", "cursor:pointer",
        ...css,
    ].join(";"));
    cell.addEventListener("mouseenter", () => {
        cell.querySelectorAll("[data-h3m-clear]").forEach((b) => { b.style.opacity = "1"; });
    });
    cell.addEventListener("mouseleave", () => {
        cell.querySelectorAll("[data-h3m-clear]").forEach((b) => { b.style.opacity = "0"; });
    });
    return cell;
}

function applyEnabledLook(cell, enabled, hasMedia) {
    cell.style.opacity = enabled ? "1" : ".42";
    cell.style.filter = enabled ? "none" : "grayscale(85%)";
    cell.style.borderColor = hasMedia
        ? (enabled ? "var(--jz-border)" : "var(--jz-border)")
        : "var(--jz-border)";
}

// ── 图片槽位（正方形，长边自适应） ─────────────────────────

function renderImageSlot(node, index, cell) {
    const settings = readSettings(node);
    const item = settings.images[index];
    cell.innerHTML = "";

    const box = el("div", "position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:#0b0b0d;");
    cell.appendChild(box);

    if (item.path) {
        const img = el("img", "max-width:100%;max-height:100%;width:100%;height:100%;object-fit:contain;display:block;");
        img.src = previewUrl("image", item.path);
        img.draggable = false;
        img.onerror = () => {
            box.innerHTML = "";
            box.appendChild(el("div", "color:var(--jz-error);font-size:11px;padding:6px;text-align:center;",
                "图片无法预览\n" + fileNameOf(item.path)));
        };
        box.appendChild(img);
    } else {
        box.appendChild(el("div", "color:var(--jz-text-dim);font-size:12px;text-align:center;line-height:1.5;", "＋\n点击或拖入图片"));
    }

    // 文件名（底部渐隐条）
    if (item.path) {
        const nameRow = el("div", [
            "position:absolute", "left:0", "right:0", "bottom:0", "z-index:2",
            "padding:8px 5px 3px", "font-size:10px", "color:#eaeaea", "line-height:1.2",
            "background:linear-gradient(180deg, rgba(0,0,0,0) 0%, rgba(0,0,0,.72) 70%)",
            "white-space:nowrap", "overflow:hidden", "text-overflow:ellipsis",
            "pointer-events:none",
        ].join(";"), fileNameOf(item.path));
        nameRow.title = item.path;
        cell.appendChild(nameRow);
    }

    cell.appendChild(numberBadge(String(index + 1)));
    cell.appendChild(toggleSwitch(item.enabled, (on) => {
        const s = readSettings(node);
        s.images[index].enabled = on;
        persistSettings(node);
        renderSlot(node, "image", index);
    }));

    if (item.path) {
        const clr = clearButton(() => {
            const s = readSettings(node);
            s.images[index].path = "";
            persistSettings(node);
            renderSlot(node, "image", index);
        });
        clr.dataset.h3mClear = "1";
        cell.appendChild(clr);
    }

    applyEnabledLook(cell, item.enabled, !!item.path);
    cell.title = item.path ? `${item.path}\n点击可更换图片` : "点击选择图片（也可直接拖入）";
}

// ── 音频槽位（预览窗 2:1，带播放进度条） ────────────────────

function renderAudioSlot(node, index, cell) {
    const settings = readSettings(node);
    const item = settings.audios[index];
    cell.innerHTML = "";

    const body = el("div", [
        "position:absolute", "inset:0", "display:flex", "flex-direction:column",
        "justify-content:center", "gap:4px", "padding:5px 8px 24px", "box-sizing:border-box",
        "background:var(--jz-bg-soft)",
    ].join(";"));
    cell.appendChild(body);

    // 第一行：文件名 + 播放进度时间（同一行，2:1 的矮窗口才放得下）
    const nameRow = el("div", [
        "display:flex", "align-items:baseline", "gap:6px", "min-width:0",
    ].join(";"));
    const nameText = el("div", [
        "flex:1 1 auto", "min-width:0", "font-size:11px", "color:var(--jz-text)",
        "white-space:nowrap", "overflow:hidden", "text-overflow:ellipsis",
    ].join(";"), item.path ? `🎧 ${fileNameOf(item.path)}` : "🎧 点击或拖入音频");
    nameText.title = item.path || "";
    nameRow.appendChild(nameText);
    const timeRow = el("div", [
        "flex:0 0 auto", "font-size:9px", "color:var(--jz-text-dim)",
        "white-space:nowrap", "font-variant-numeric:tabular-nums",
    ].join(";"), "");
    nameRow.appendChild(timeRow);
    body.appendChild(nameRow);

    if (!item.path) {
        body.appendChild(el("div", "font-size:11px;color:var(--jz-text-dim);", "未载入素材"));
        cell.appendChild(numberBadge(String(index + 1)));
        cell.appendChild(toggleSwitch(item.enabled, (on) => {
            const s = readSettings(node);
            s.audios[index].enabled = on;
            persistSettings(node);
            renderSlot(node, "audio", index);
        }));
        applyEnabledLook(cell, item.enabled, false);
        return;
    }

    // 播放控制 + 进度条
    const row = el("div", "display:flex;align-items:center;gap:5px;min-width:0;");
    const audio = document.createElement("audio");
    audio.preload = "metadata";
    audio.src = previewUrl("audio", item.path);
    // 挂进槽位（display:none 不占位）：脱离 DOM 的 <audio> 重绘时抓不到，
    // 会一直播下去 —— 这就是「改了时长，旧的那条还在放被裁掉部分」的根源。
    audio.style.display = "none";
    audio.dataset.h3mAudio = "1";
    cell.appendChild(audio);

    const playBtn = el("button", [
        "flex:0 0 auto", "width:20px", "height:20px", "padding:0", "border-radius:50%",
        "border:1px solid var(--jz-border)", "background:var(--jz-bg-raise)",
        "color:var(--jz-text-strong)", "font-size:9px", "cursor:pointer", "line-height:1",
    ].join(";"), "▶");
    playBtn.title = "试听（按「开始时间 / 音频时长」预览）";
    row.appendChild(playBtn);

    const track = el("div", [
        "position:relative", "flex:1 1 auto", "min-width:0", "height:7px", "border-radius:4px",
        "background:var(--jz-bg-deep)", "border:1px solid var(--jz-border)",
        "cursor:pointer", "overflow:hidden",
    ].join(";"));
    const trimRange = el("div", [
        "position:absolute", "top:0", "bottom:0", "left:0", "width:100%",
        "background:rgba(90,160,230,.20)", "pointer-events:none",
    ].join(";"));
    const fill = el("div", [
        "position:absolute", "top:0", "bottom:0", "left:0", "width:0%",
        "background:var(--jz-btn-primary)", "pointer-events:none",
    ].join(";"));
    track.appendChild(trimRange);
    track.appendChild(fill);
    row.appendChild(track);
    body.appendChild(row);

    const start = Math.max(0, toNum(item.start, 0));
    const dur = Math.max(0, toNum(item.duration, 0));
    // 试听区间：start ~ start+dur（dur=0 → 到结尾）。播放只能在这个窗口里跑。
    const trimEnd = dur > 0 ? start + dur : Infinity;
    const inWindow = (t) => t >= start - 0.01 && t < trimEnd - 0.005;

    const paintTime = () => {
        const total = Number.isFinite(audio.duration) ? audio.duration : 0;
        const cur = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
        const pct = total > 0 ? clamp((cur / total) * 100, 0, 100) : 0;
        fill.style.width = `${pct}%`;
        const trimTo = dur > 0 ? Math.min(total || (start + dur), start + dur) : total;
        timeRow.textContent = `${fmtTime(cur)}/${fmtTime(total)} 取${trimNum(start)}–${trimTo.toFixed(2)}s`;
        timeRow.title = `播放进度 ${fmtTime(cur)} / ${fmtTime(total)}；实际取用 ${trimNum(start)}–${trimTo.toFixed(2)} 秒`;
    };

    const paintTrim = () => {
        const total = Number.isFinite(audio.duration) ? audio.duration : 0;
        if (total <= 0) { trimRange.style.left = "0%"; trimRange.style.width = "100%"; return; }
        const s = clamp(start, 0, total);
        const e = dur > 0 ? clamp(s + dur, 0, total) : total;
        trimRange.style.left = `${(s / total) * 100}%`;
        trimRange.style.width = `${Math.max(0, ((e - s) / total) * 100)}%`;
    };

    audio.addEventListener("loadedmetadata", () => {
        paintTrim();
        // 载入后光标默认停在裁剪起点（而不是 0），所见即所取
        if (start > 0 && start < (audio.duration || 0)) {
            try { audio.currentTime = start; } catch (_) {}
        }
        paintTime();
    });
    audio.addEventListener("timeupdate", () => {
        const cur = audio.currentTime || 0;
        if (cur >= trimEnd - 0.01) {
            // 到裁剪末尾：停下并回到起点，下次点播放重头放这一小段
            audio.pause();
            try { audio.currentTime = start; } catch (_) {}
        } else if (cur < start - 0.01) {
            // 进度被拖到裁剪区之前（或残留位置）：拉回起点
            try { audio.currentTime = start; } catch (_) {}
        }
        paintTime();
    });
    audio.addEventListener("play", () => {
        playBtn.textContent = "❚❚";
        // 关键：每次起播都确保落在裁剪窗口内（否则会放出被裁掉的部分）
        const cur = audio.currentTime || 0;
        if (!inWindow(cur)) {
            try { audio.currentTime = start; } catch (_) {}
        }
    });
    audio.addEventListener("pause", () => { playBtn.textContent = "▶"; });
    audio.addEventListener("ended", () => { playBtn.textContent = "▶"; });
    audio.addEventListener("error", () => { timeRow.textContent = "音频无法预览"; });

    playBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    playBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (audio.paused) {
            // 同时只允许一个音频在响
            document.querySelectorAll("audio[data-h3m-audio]").forEach((a) => {
                if (a !== audio) { try { a.pause(); } catch (_) {} }
            });
            audio.dataset.h3mAudio = "1";
            if (!inWindow(audio.currentTime || 0)) {
                try { audio.currentTime = start; } catch (_) { /* 元数据未就绪 */ }
            }
            audio.play().catch((err) => notify(`无法播放：${err.message || err}`, true));
        } else {
            audio.pause();
        }
    });

    let seeking = false;
    const seek = (ev) => {
        const rect = track.getBoundingClientRect();
        if (!rect.width || !Number.isFinite(audio.duration) || audio.duration <= 0) return;
        const ratio = clamp((ev.clientX - rect.left) / rect.width, 0, 1);
        try { audio.currentTime = ratio * audio.duration; } catch (_) {}
        paintTime();
    };
    // 用 pointer capture 拖动进度条：不在 window 上挂监听，重绘后不会累积泄漏
    track.addEventListener("pointerdown", (e) => {
        e.stopPropagation();
        seeking = true;
        try { track.setPointerCapture?.(e.pointerId); } catch (_) {}
        seek(e);
    });
    track.addEventListener("pointermove", (e) => { if (seeking) seek(e); });
    const endSeek = (e) => {
        seeking = false;
        try { track.releasePointerCapture?.(e.pointerId); } catch (_) {}
    };
    track.addEventListener("pointerup", endSeek);
    track.addEventListener("pointercancel", endSeek);
    // 进度条的 click 不能冒泡到格子（否则会被当成「换素材」）
    track.addEventListener("click", (e) => e.stopPropagation());

    // 底部「音频设置」
    const footer = el("div", [
        "position:absolute", "left:0", "right:0", "bottom:0", "height:22px", "z-index:3",
        "display:flex", "align-items:center", "justify-content:flex-end",
        "padding:0 6px", "box-sizing:border-box", "pointer-events:none",
    ].join(";"));
    const setBtn = el("button", [
        "pointer-events:auto", "height:17px", "padding:0 8px", "border-radius:5px",
        "border:1px solid var(--jz-border)", "background:var(--jz-btn-info)",
        "color:#fff", "font-size:10px", "cursor:pointer", "line-height:1",
    ].join(";"), "音频设置");
    setBtn.title = "设置开始时间 / 音频时长（秒，步长 0.01）";
    setBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    setBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openParamDialog(node, "audio", index);
    });
    footer.appendChild(setBtn);
    cell.appendChild(footer);

    cell.appendChild(numberBadge(String(index + 1)));
    cell.appendChild(toggleSwitch(item.enabled, (on) => {
        const s = readSettings(node);
        s.audios[index].enabled = on;
        persistSettings(node);
        renderSlot(node, "audio", index);
    }));

    const clr = clearButton(() => {
        const s = readSettings(node);
        s.audios[index].path = "";
        persistSettings(node);
        renderSlot(node, "audio", index);
    });
    clr.dataset.h3mClear = "1";
    clr.style.bottom = "26px";
    cell.appendChild(clr);

    applyEnabledLook(cell, item.enabled, true);
    paintTime();
}

// ── 视频槽位（正方形预览 + 底部「视频设置」） ────────────────

function videoSpecText(item) {
    const parts = [];
    parts.push(item.force_rate > 0 ? `${trimNum(item.force_rate)}fps` : "原帧率");
    if (item.skip > 0) parts.push(`跳${item.skip}`);
    parts.push((item.width || item.height) ? `${item.width || "auto"}×${item.height || "auto"}` : "原尺寸");
    parts.push(item.cap > 0 ? `${item.cap}帧` : "不限帧");
    return parts.join(" · ");
}

function renderVideoSlot(node, index, cell) {
    const settings = readSettings(node);
    const item = settings.videos[index];
    cell.innerHTML = "";

    const preview = el("div", [
        "position:absolute", "left:0", "right:0", "top:0", "bottom:22px",
        "display:flex", "align-items:center", "justify-content:center",
        "background:#0b0b0d",
    ].join(";"));
    cell.appendChild(preview);

    if (item.path) {
        const thumb = videoThumbUrl(item.path);
        const video = document.createElement("video");
        video.src = previewUrl("video", item.path);
        video.poster = thumb;
        video.muted = true;
        video.loop = true;
        video.playsInline = true;
        video.preload = "metadata";
        video.dataset.h3mVideo = "1";
        video.style.cssText = "width:100%;height:100%;object-fit:contain;display:block;";
        preview.appendChild(video);

        const playHint = el("div", [
            "position:absolute", "left:50%", "top:50%", "transform:translate(-50%,-50%)",
            "width:38px", "height:38px", "border-radius:50%", "background:rgba(0,0,0,.5)",
            "color:#fff", "font-size:15px", "display:flex", "align-items:center",
            "justify-content:center", "pointer-events:none", "z-index:2",
        ].join(";"), "▶");
        preview.appendChild(playHint);

        video.addEventListener("click", (e) => {
            e.stopPropagation();
            if (video.paused) {
                document.querySelectorAll("video[data-h3m-video]").forEach((v) => {
                    if (v !== video) { try { v.pause(); } catch (_) {} }
                });
                video.dataset.h3mVideo = "1";
                video.play().catch(() => {});
            } else {
                video.pause();
            }
        });
        video.addEventListener("play", () => { playHint.style.display = "none"; });
        video.addEventListener("pause", () => { playHint.style.display = "flex"; });
        // 浏览器不支持该编码（HEVC/H.265 等）→ 首帧缩略图兜底，预览窗不会空白
        video.addEventListener("error", () => {
            preview.innerHTML = "";
            const poster = el("img", "max-width:100%;max-height:100%;width:100%;height:100%;object-fit:contain;display:block;");
            poster.src = thumb;
            poster.draggable = false;
            preview.appendChild(poster);
            preview.appendChild(el("div", [
                "position:absolute", "left:0", "right:0", "bottom:0", "z-index:2",
                "padding:3px 5px", "font-size:9px", "color:#e8c98a", "text-align:center",
                "background:linear-gradient(0deg, rgba(0,0,0,.75), rgba(0,0,0,0))",
            ].join(";"), "首帧预览（浏览器不解该编码，不影响使用）"));
        });
    } else {
        preview.appendChild(el("div", "color:var(--jz-text-dim);font-size:12px;text-align:center;line-height:1.5;",
            "＋\n点击或拖入视频"));
    }

    // 底部栏：参数摘要 + 视频设置
    const footer = el("div", [
        "position:absolute", "left:0", "right:0", "bottom:0", "height:22px", "z-index:3",
        "display:flex", "align-items:center", "gap:4px", "padding:0 5px",
        "box-sizing:border-box", "background:var(--jz-bg-soft)",
        "border-top:1px solid var(--jz-border)",
    ].join(";"));
    const spec = el("div", [
        "flex:1 1 auto", "font-size:10px", "color:var(--jz-text-dim)",
        "white-space:nowrap", "overflow:hidden", "text-overflow:ellipsis",
    ].join(";"), videoSpecText(item));
    spec.title = "当前视频参数：" + videoSpecText(item);
    footer.appendChild(spec);

    const setBtn = el("button", [
        "flex:0 0 auto", "height:17px", "padding:0 8px", "border-radius:5px",
        "border:1px solid var(--jz-border)", "background:var(--jz-btn-info)",
        "color:#fff", "font-size:10px", "cursor:pointer", "line-height:1",
    ].join(";"), "视频设置");
    setBtn.title = "强制帧率 / 跳过帧数 / 输出宽高 / 帧数上限";
    setBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    setBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openParamDialog(node, "video", index);
    });
    footer.appendChild(setBtn);
    cell.appendChild(footer);

    cell.appendChild(numberBadge(String(index + 1)));
    cell.appendChild(toggleSwitch(item.enabled, (on) => {
        const s = readSettings(node);
        s.videos[index].enabled = on;
        persistSettings(node);
        renderSlot(node, "video", index);
    }));

    if (item.path) {
        const clr = clearButton(() => {
            const s = readSettings(node);
            s.videos[index].path = "";
            persistSettings(node);
            renderSlot(node, "video", index);
        });
        clr.dataset.h3mClear = "1";
        clr.style.bottom = "26px";
        cell.appendChild(clr);
    }

    applyEnabledLook(cell, item.enabled, !!item.path);
    cell.title = item.path ? `${item.path}\n点击可更换视频` : "点击选择视频（也可直接拖入）";
}

// ── 槽位渲染分发 ─────────────────────────────────────────

// 重绘前必须把旧播放器停掉：脱离 DOM 的 <audio>/<video> 仍会继续播放，
// 造成「改了音频时长，旧的那条还在放被裁掉的部分」这种假象。
function stopMediaIn(el) {
    if (!el || !el.querySelectorAll) return;
    el.querySelectorAll("audio, video").forEach((m) => {
        try { m.pause(); } catch (_) { /* ignore */ }
        try {
            m.removeAttribute("src");
            m.load();
        } catch (_) { /* ignore */ }
    });
}

function slotCell(node, kind, index) {
    const st = node.__h3m;
    return st?.cells?.[kind]?.[index] || null;
}

function renderSlot(node, kind, index) {
    const cell = slotCell(node, kind, index);
    if (!cell) return;
    const layout = getLayout(node);
    if (layout.hidden[kind] || index >= layout.counts[kind]) return;
    stopMediaIn(cell);
    if (kind === "image") renderImageSlot(node, index, cell);
    else if (kind === "audio") renderAudioSlot(node, index, cell);
    else renderVideoSlot(node, index, cell);
    node.setDirtyCanvas?.(true, true);
}

// 只渲染「当前布局里真正存在」的槽位
function renderAllSlots(node) {
    const layout = getLayout(node);
    for (const kind of layout.order) {
        if (layout.hidden[kind]) continue;
        for (let i = 0; i < layout.counts[kind]; i++) renderSlot(node, kind, i);
    }
    // 重绘后内容高度可能变化（无滚动条模式）→ 让节点高度重新贴合
    setTimeout(() => {
        try { node.__h3m?.fitPanelHeight?.(); } catch (_) {}
    }, 40);
}

// ── 参数对话框（视频 / 音频共用） ────────────────────────────

const DIALOG_ID = "jzl-h3m-param-dialog";

function closeDialog() {
    document.getElementById(DIALOG_ID)?.remove();
}

function openParamDialog(node, kind, index) {
    closeDialog();
    const settings = readSettings(node);
    const list = settings[`${kind}s`];
    const item = list[index];
    if (!item) return;

    const fields = kind === "video" ? VIDEO_FIELDS : AUDIO_FIELDS;
    const title = kind === "video" ? `🎬 视频${index + 1} 设置` : `🎧 音频${index + 1} 设置`;
    const sub = item.path ? fileNameOf(item.path) : "（未选择素材，参数会先保存，选好素材后生效）";

    const overlay = el("div", [
        "position:fixed", "inset:0", "background:rgba(0,0,0,.55)", "z-index:99999",
        "display:flex", "align-items:center", "justify-content:center",
    ].join(";"));
    overlay.id = DIALOG_ID;
    overlay.addEventListener("mousedown", (e) => {
        if (e.target === overlay) closeDialog();
    });

    const panel = el("div", [
        "background:var(--jz-bg)", "border:1px solid var(--jz-border)",
        "border-radius:10px", "padding:16px", "width:380px", "max-width:92vw",
        "box-shadow:0 12px 40px rgba(0,0,0,.6)", "color:var(--jz-text)",
    ].join(";"));
    overlay.appendChild(panel);

    panel.appendChild(el("div", "font-size:14px;font-weight:600;color:var(--jz-text-strong);margin-bottom:2px;", title));
    panel.appendChild(el("div", "font-size:11px;color:var(--jz-text-dim);margin-bottom:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;", sub));

    const inputs = {};
    ensureStepperStyle();
    for (const field of fields) {
        const row = el("div", "display:flex;align-items:center;gap:6px;margin-bottom:9px;");
        const label = el("div", "flex:0 0 72px;font-size:12px;color:var(--jz-text);", field.label);
        row.appendChild(label);

        const stepVal = Number(field.step ?? 1);
        const dec = stepDecimals(field.step);

        // ◀ ▶ 左右微调按钮（比数字框自带的上下三角好点、好按）
        const mkStepBtn = (text, title) => {
            const b = el("button", [
                "flex:0 0 auto", "width:22px", "height:26px", "padding:0",
                "display:flex", "align-items:center", "justify-content:center",
                "border-radius:6px", "border:1px solid var(--jz-border)",
                "background:var(--jz-bg-raise)", "color:var(--jz-text-strong)",
                "font-size:13px", "line-height:1", "cursor:pointer",
                "user-select:none", "touch-action:none",
            ].join(";"), text);
            b.title = title;
            return b;
        };
        const decBtn = mkStepBtn("◀", `减 ${stepVal}${field.unit || ""}（可按住连续调整）`);
        const incBtn = mkStepBtn("▶", `加 ${stepVal}${field.unit || ""}（可按住连续调整）`);
        row.appendChild(decBtn);

        const input = document.createElement("input");
        input.type = "number";
        input.className = "h3m-num";
        input.step = String(stepVal);
        if (field.min !== undefined) input.min = String(field.min);
        if (field.max !== undefined) input.max = String(field.max);
        input.value = trimNum(item[field.key] ?? 0);
        input.style.cssText = [
            "flex:1 1 auto", "min-width:0", "height:26px", "padding:0 8px",
            "box-sizing:border-box", "border-radius:6px", "text-align:center",
            "border:1px solid var(--jz-border)", "background:var(--jz-bg-deep)",
            "color:var(--jz-text-strong)", "font-size:12px", "outline:none",
        ].join(";");
        row.appendChild(input);

        const nudge = (dir) => {
            let v = toNum(input.value, 0) + dir * stepVal;
            if (field.min !== undefined) v = Math.max(field.min, v);
            if (field.max !== undefined) v = Math.min(field.max, v);
            // 按步长精度取整，杜绝 0.01 步进累积出 4.0100000001 这种尾巴
            v = dec === 0 ? Math.round(v) : Number(v.toFixed(dec));
            input.value = dec === 0 ? String(v) : v.toFixed(dec);
        };
        bindHoldRepeat(decBtn, () => nudge(-1));
        bindHoldRepeat(incBtn, () => nudge(1));

        if (field.unit) row.appendChild(el("div", "flex:0 0 auto;min-width:18px;font-size:11px;color:var(--jz-text-dim);", field.unit));
        row.appendChild(incBtn);
        panel.appendChild(row);
        if (field.hint) {
            panel.appendChild(el("div", "font-size:10px;color:var(--jz-text-dim);margin:-5px 0 9px 78px;line-height:1.3;", field.hint));
        }
        inputs[field.key] = { input, field, dec };
    }

    const btnRow = el("div", "display:flex;justify-content:flex-end;gap:8px;margin-top:12px;");
    const cancel = el("button", [
        "height:28px", "padding:0 14px", "border-radius:6px",
        "border:1px solid var(--jz-border)", "background:var(--jz-bg-raise)",
        "color:var(--jz-text)", "font-size:12px", "cursor:pointer",
    ].join(";"), "取消");
    cancel.addEventListener("click", closeDialog);
    const ok = el("button", [
        "height:28px", "padding:0 14px", "border-radius:6px",
        "border:1px solid var(--jz-border)", "background:var(--jz-btn-primary)",
        "color:#fff", "font-size:12px", "cursor:pointer",
    ].join(";"), "确定");
    btnRow.appendChild(cancel);
    btnRow.appendChild(ok);
    panel.appendChild(btnRow);

    const commit = () => {
        const latest = readSettings(node);
        const target = latest[`${kind}s`][index];
        for (const [key, { input, field, dec }] of Object.entries(inputs)) {
            let v = toNum(input.value, 0);
            if (field.min !== undefined) v = Math.max(field.min, v);
            if (field.max !== undefined) v = Math.min(field.max, v);
            // 按该字段步长的精度落库（音频 0.01 秒 → 两位小数）
            target[key] = dec === 0 ? Math.round(v) : Number(v.toFixed(dec));
        }
        persistSettings(node);
        closeDialog();
        renderSlot(node, kind, index);
    };
    ok.addEventListener("click", commit);
    panel.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); commit(); }
        if (e.key === "Escape") { e.preventDefault(); closeDialog(); }
    });

    document.body.appendChild(overlay);
    const first = Object.values(inputs)[0];
    setTimeout(() => { try { first?.input.focus(); first?.input.select?.(); } catch (_) {} }, 30);
}

// ── 界面设置（右击菜单）──────────────────────────────────────

const LAYOUT_DIALOG_ID = "jzl-h3m-layout-dialog";

function closeLayoutDialog() {
    document.getElementById(LAYOUT_DIALOG_ID)?.remove();
}

// 应用布局：写回 settings.layout → 持久化 → 重建面板 → 重绘 → 高度贴合
function applyLayout(node, layout) {
    const st = node.__h3m;
    if (!st) return;
    const settings = readSettings(node);
    settings.layout = normalizeLayout(layout);
    persistSettings(node);
    fillBody(node);
    renderAllSlots(node);
    const layoutNow = getLayout(node);
    if (st.dom && st.dom.element) {
        // 顺序/数量变化后整体高度要重算（可能变高或变矮）
        setTimeout(() => { try { st.fitPanelHeight?.(); } catch (_) {} }, 60);
        setTimeout(() => { try { st.fitPanelHeight?.(); } catch (_) {} }, 320);
    }
    node.setDirtyCanvas?.(true, true);
    return layoutNow;
}

function openLayoutDialog(node) {
    closeLayoutDialog();
    const layout = JSON.parse(JSON.stringify(getLayout(node)));   // 草稿，点确定才落库
    const original = JSON.stringify(layout);

    const overlay = el("div", [
        "position:fixed", "inset:0", "background:rgba(0,0,0,.55)", "z-index:99999",
        "display:flex", "align-items:center", "justify-content:center",
    ].join(";"));
    overlay.id = LAYOUT_DIALOG_ID;
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeLayoutDialog(); });

    const panel = el("div", [
        "background:var(--jz-bg)", "border:1px solid var(--jz-border)",
        "border-radius:10px", "padding:16px", "width:430px", "max-width:94vw",
        "box-shadow:0 12px 40px rgba(0,0,0,.6)", "color:var(--jz-text)",
    ].join(";"));
    overlay.appendChild(panel);

    panel.appendChild(el("div", "font-size:14px;font-weight:600;color:var(--jz-text-strong);margin-bottom:2px;", "⚙️ 界面设置"));
    panel.appendChild(el("div",
        "font-size:11px;color:var(--jz-text-dim);margin-bottom:12px;line-height:1.4;",
        "拖动 ⋮⋮ 交换三个模块的上下顺序；点数字改该模块槽位数量；关闭开关可整块隐藏（至少保留一个）。设置会随工作流保存。"));

    const listEl = el("div", "display:flex;flex-direction:column;gap:8px;");
    panel.appendChild(listEl);

    let dragKind = null;

    const moveTo = (kind, targetIndex) => {
        const from = layout.order.indexOf(kind);
        if (from < 0 || from === targetIndex) return false;
        layout.order.splice(from, 1);
        layout.order.splice(targetIndex, 0, kind);
        return true;
    };

    const renderRows = () => {
        listEl.innerHTML = "";
        layout.order.forEach((kind, idx) => {
            const meta = KIND_META[kind];
            const isHidden = !!layout.hidden[kind];

            const row = el("div", [
                "display:flex", "align-items:center", "gap:8px",
                "padding:8px 10px", "border-radius:8px",
                "border:1px solid var(--jz-border)", "background:var(--jz-bg-soft)",
                isHidden ? "opacity:.5" : "",
                dragKind === kind ? "border-color:var(--jz-btn-primary);box-shadow:0 0 0 1px var(--jz-btn-primary)" : "",
            ].join(";"));
            row.dataset.h3mRow = kind;

            const handle = el("div", [
                "flex:0 0 auto", "width:18px", "height:26px", "display:flex",
                "align-items:center", "justify-content:center",
                "cursor:grab", "color:var(--jz-text-dim)", "font-size:13px",
                "user-select:none", "touch-action:none",
            ].join(";"), "⋮⋮");
            handle.title = "按住拖动，交换模块上下顺序";
            row.appendChild(handle);

            row.appendChild(el("div", "flex:0 0 84px;font-size:12px;color:var(--jz-text-strong);",
                `${meta.icon} ${meta.label}`));

            const chips = el("div", "flex:1 1 auto;display:flex;gap:5px;flex-wrap:wrap;");
            for (const opt of meta.options) {
                const on = layout.counts[kind] === opt;
                const chip = el("button", [
                    "min-width:26px", "height:22px", "padding:0 7px", "border-radius:6px",
                    "font-size:11px", "line-height:1", "cursor:pointer",
                    "border:1px solid " + (on ? "var(--jz-btn-primary)" : "var(--jz-border)"),
                    "background:" + (on ? "var(--jz-btn-primary)" : "var(--jz-bg-deep)"),
                    "color:" + (on ? "#fff" : "var(--jz-text)"),
                ].join(";"), String(opt));
                chip.title = `${meta.label}槽位数量：${opt}`;
                chip.addEventListener("click", () => {
                    layout.counts[kind] = opt;
                    renderRows();
                });
                chips.appendChild(chip);
            }
            row.appendChild(chips);

            // 显隐开关
            const eye = el("button", [
                "flex:0 0 auto", "height:22px", "padding:0 8px", "border-radius:6px",
                "font-size:11px", "line-height:1", "cursor:pointer",
                "border:1px solid " + (isHidden ? "var(--jz-border)" : "var(--jz-btn-ok)"),
                "background:" + (isHidden ? "var(--jz-bg-deep)" : "var(--jz-btn-ok)"),
                "color:" + (isHidden ? "var(--jz-text-dim)" : "#fff"),
            ].join(";"), isHidden ? "已隐藏" : "显示中");
            eye.title = "关闭后将整块隐藏（至少保留一个模块）";
            eye.addEventListener("click", () => {
                if (!isHidden) {
                    const visible = KIND_ORDER.filter((k) => !layout.hidden[k]);
                    if (visible.length <= 1) {
                        notify("至少保留一个模块，不能全部关闭", true);
                        return;
                    }
                }
                layout.hidden[kind] = !isHidden;
                renderRows();
            });
            row.appendChild(eye);

            handle.addEventListener("pointerdown", (e) => {
                e.preventDefault();
                dragKind = kind;
                try { handle.setPointerCapture?.(e.pointerId); } catch (_) {}
                renderRows();
            });

            listEl.appendChild(row);
        });
    };

    renderRows();

    // 拖动定位：按指针所在的行实时换位（window 监听，行重绘也不会断）
    const onMove = (e) => {
        if (!dragKind) return;
        const rows = [...listEl.querySelectorAll("[data-h3m-row]")];
        let targetIndex = rows.length - 1;
        for (let i = 0; i < rows.length; i++) {
            const r = rows[i].getBoundingClientRect();
            if (e.clientY < r.top + r.height / 2) { targetIndex = i; break; }
        }
        if (moveTo(dragKind, targetIndex)) renderRows();
    };
    const onUp = () => {
        if (!dragKind) return;
        dragKind = null;
        renderRows();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);

    const cleanup = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
    };

    const btnRow = el("div", "display:flex;align-items:center;gap:8px;margin-top:14px;");
    const reset = el("button", [
        "height:28px", "padding:0 12px", "border-radius:6px",
        "border:1px solid var(--jz-border)", "background:var(--jz-bg-raise)",
        "color:var(--jz-text)", "font-size:12px", "cursor:pointer",
    ].join(";"), "恢复默认");
    reset.title = "顺序恢复为 图片 → 音频 → 视频，数量 9 / 3 / 3，全部显示";
    reset.addEventListener("click", () => {
        const def = normalizeLayout(null);
        layout.order = def.order;
        layout.counts = def.counts;
        layout.hidden = def.hidden;
        renderRows();
    });
    btnRow.appendChild(reset);
    btnRow.appendChild(el("div", "flex:1 1 auto;"));

    const cancel = el("button", [
        "height:28px", "padding:0 14px", "border-radius:6px",
        "border:1px solid var(--jz-border)", "background:var(--jz-bg-raise)",
        "color:var(--jz-text)", "font-size:12px", "cursor:pointer",
    ].join(";"), "取消");
    cancel.addEventListener("click", () => { cleanup(); closeLayoutDialog(); });

    const ok = el("button", [
        "height:28px", "padding:0 16px", "border-radius:6px",
        "border:1px solid var(--jz-border)", "background:var(--jz-btn-primary)",
        "color:#fff", "font-size:12px", "cursor:pointer",
    ].join(";"), "确定");
    ok.addEventListener("click", () => {
        cleanup();
        closeLayoutDialog();
        if (JSON.stringify(layout) === original) return;   // 没改就不动，避免无谓重排
        applyLayout(node, layout);
    });
    btnRow.appendChild(cancel);
    btnRow.appendChild(ok);
    panel.appendChild(btnRow);

    const onKey = (e) => {
        if (e.key === "Escape") { cleanup(); closeLayoutDialog(); window.removeEventListener("keydown", onKey); }
    };
    window.addEventListener("keydown", onKey);

    document.body.appendChild(overlay);
}

// ── 节点装配 ─────────────────────────────────────────────

function sectionTitle(text) {
    return el("div", [
        "flex:0 0 auto", "font-size:11px", "font-weight:600", "color:var(--jz-info)",
        "margin:2px 0 0", "letter-spacing:.3px",
    ].join(";"), text);
}

function gridStyle(count) {
    return [
        "flex:0 0 auto", "display:grid",
        `grid-template-columns:repeat(${gridColumns(count)}, minmax(0,1fr))`,
        "gap:6px",
    ].join(";");
}

function buildSlotGrid(node, kind, count) {
    const grid = el("div", gridStyle(count));
    node.__h3m.cells[kind] = [];
    for (let i = 0; i < count; i++) {
        const cell = slotShell([`aspect-ratio:${KIND_META[kind].ratio}`, "width:100%"]);
        bindDropTarget(node, cell, kind, i);
        cell.addEventListener("mousedown", (e) => {
            // 左键点空白处 = 选/换素材（开关、按钮自己 stopPropagation）
            if (e.button !== 0) return;
            e.stopPropagation();
        });
        cell.addEventListener("click", (e) => {
            if (e.button !== 0) return;
            e.stopPropagation();
            pickFile(node, kind, i);
        });
        node.__h3m.cells[kind].push(cell);
        grid.appendChild(cell);
    }
    return grid;
}

function headStatusText(layout) {
    const parts = [];
    for (const k of layout.order) {
        if (layout.hidden[k]) continue;
        parts.push(`${layout.counts[k]} ${KIND_META[k].label}`);
    }
    return `${parts.join(" 丨 ")} → ref2va 参考总线`;
}

// 按当前布局重建面板内容（顺序 / 数量 / 隐藏都在这里生效）
function fillBody(node) {
    const st = node.__h3m;
    const body = st?.body;
    if (!body) return;
    body.innerHTML = "";
    st.cells = { image: [], audio: [], video: [] };

    const layout = getLayout(node);
    for (const kind of layout.order) {
        if (layout.hidden[kind]) continue;
        const count = layout.counts[kind];
        const meta = KIND_META[kind];
        const sec = el("div", "flex:0 0 auto;display:flex;flex-direction:column;gap:4px;");
        sec.dataset.h3mSection = kind;
        sec.appendChild(sectionTitle(
            count > 1 ? `${meta.icon} ${meta.label} 1–${count}` : `${meta.icon} ${meta.label}`
        ));
        sec.appendChild(buildSlotGrid(node, kind, count));
        body.appendChild(sec);
    }

    if (st.status) {
        st.status.textContent = headStatusText(layout);
        st.status.title = "执行后显示实际载入数量";
        st.status.style.color = "";
    }
    st.renderedLayoutSig = JSON.stringify(layout);
}

function buildPanel(node) {
    const container = el("div", [
        "width:100%", "height:100%", "display:flex", "flex-direction:column", "gap:6px",
        "padding:8px", "box-sizing:border-box",
        // 不留滚动条：内容高度由 fitPanelHeight 反映到节点高度上（永远全部可见）
        "overflow:hidden", "background:var(--jz-bg)", "border-radius:8px",
    ].join(";"));
    container.addEventListener("pointerdown", (e) => e.stopPropagation());
    container.addEventListener("mousedown", (e) => e.stopPropagation());

    const head = el("div", "flex:0 0 auto;display:flex;align-items:center;gap:8px;");
    head.appendChild(el("div", "font-size:12px;font-weight:600;color:var(--jz-text-strong);", "📥 MiniMax 素材加载"));
    const status = el("div", [
        "flex:1 1 auto", "font-size:10px", "color:var(--jz-text-dim)",
        "white-space:nowrap", "overflow:hidden", "text-overflow:ellipsis", "text-align:right",
    ].join(";"), headStatusText(getLayout(node)));
    status.title = "执行后显示实际载入数量";
    head.appendChild(status);
    node.__h3m.status = status;
    container.appendChild(head);

    const body = el("div", [
        "flex:1 1 auto", "min-height:0", "display:flex", "flex-direction:column", "gap:6px",
    ].join(";"));
    node.__h3m.body = body;
    container.appendChild(body);

    fillBody(node);

    return container;
}

function setupMediaLoader(node) {
    if (node.__h3m) return;

    const widget = getSettingsWidget(node);
    node.__h3m = {
        settings: normalizeSettings(readWidgetValue(widget)),
        widget,
        cells: { image: [], audio: [], video: [] },
        status: null,
    };

    // 隐藏内部 JSON widget（存储用，不给用户看）
    const hideTargets = new Set([SETTINGS_WIDGET]);
    for (const w of node.widgets || []) {
        if (!w || !hideTargets.has(w.name)) continue;
        w.hidden = true;
        if (!w.options) w.options = {};
        w.options.hidden = true;
        w.computeSize = () => [0, -4];
    }
    try { if (Array.isArray(node.widgets)) node.widgets = node.widgets.slice(); } catch (_) {}

    const container = buildPanel(node);
    const dom = node.addDOMWidget?.(DOM_WIDGET_NAME, "JZL_H3_MEDIA", container, {
        serialize: false,
        hideOnZoom: false,
    });
    if (dom) {
        try { delete dom.computeSize; } catch (_) { dom.computeSize = undefined; }
        // 只让 media_settings 进 widgets_values：DOM 面板本身不落盘（否则会多一个空条目）
        dom.serialize = false;
        dom.options = dom.options || {};
        dom.options.serialize = false;
        dom.options.getMinHeight = () => 900;
        dom.options.getHeight = () => "100%";
        dom.computeLayoutSize = () => ({ minHeight: 900, maxHeight: undefined, minWidth: 640 });
    }
    node.__h3m.dom = dom;

    // 最小宽度锁定：3 列正方形格子挤到 640 以下就没法看了
    const lockWidth = () => {
        try {
            const w = Number(node.size?.[0]);
            if (Number.isFinite(w) && w < 640) {
                node.setSize?.([640, Math.max(node.size?.[1] || 1220, 900)]);
            }
        } catch (_) {}
    };
    node.min_width = 640;
    node.minWidth = 640;

    const syncWidth = () => {
        try {
            const fullW = Number.isFinite(node.size?.[0]) ? node.size[0] : 0;
            if (dom && fullW && dom.width !== fullW) dom.width = fullW;
        } catch (_) {}
    };

    // 面板不留滚动条：内容多高，节点就多高。
    // 宽度变化时精确贴合（可增可减）；只拖高度时只增不减，避免和用户拖拽打架。
    let fitTimer = null;
    let verifyTimer = null;
    let fitAllowShrink = true;
    let selfResizing = false;
    // overflow:hidden 时 scrollHeight 永远 ≥ clientHeight，量不出「内容比视口矮」→ 累加子元素高度。
    // 子元素用 getBoundingClientRect（会被画布缩放），除以容器自身的缩放系数换算回 CSS px，避免取整误差。
    const measureContentHeight = () => {
        const kids = container.children;
        if (!kids || !kids.length) return container.scrollHeight;
        const cs = getComputedStyle(container);
        const gap = parseFloat(cs.rowGap) || 0;
        const padTop = parseFloat(cs.paddingTop) || 0;
        const padBottom = parseFloat(cs.paddingBottom) || 0;
        const rectH = container.getBoundingClientRect().height;
        const scale = (rectH > 0 && container.clientHeight > 0)
            ? rectH / container.clientHeight
            : 1;
        const safeScale = (Number.isFinite(scale) && scale > 0.05) ? scale : 1;
        let h = 0;
        for (let i = 0; i < kids.length; i++) {
            const kh = kids[i].getBoundingClientRect().height;
            h += kh > 0 ? kh / safeScale : kids[i].offsetHeight;
        }
        h += gap * (kids.length - 1);
        return Math.ceil(h + padTop + padBottom);
    };
    const fitPanelHeight = () => {
        fitTimer = null;
        try {
            const need = measureContentHeight();   // 真实内容高度（不依赖 scrollHeight）
            const cur = Number(node.size?.[1]) || 0;
            const clientH = container.clientHeight;
            // DOM 还没布局好（clientHeight=0）时不能量，否则会把节点撑爆
            if (!need || !cur || clientH <= 0) return;
            // 节点高度 ↔ 面板高度是 1:1 的：差多少补多少，不用去猜标题栏高度（猜错会越调越大）
            const delta = need - clientH;
            const shouldResize = fitAllowShrink ? Math.abs(delta) > 2 : delta > 2;
            if (shouldResize) {
                // 自己改的尺寸不要再回头量一次（改完布局还没生效，会越量越大）
                selfResizing = true;
                node.setSize?.([node.size?.[0] || 700, cur + delta]);
                setTimeout(() => { selfResizing = false; }, 120);
                // 复核一次：aspect-ratio 取整可能让真实内容比预算多几像素，按实测差值补齐（只补一次，不会循环）
                clearTimeout(verifyTimer);
                verifyTimer = setTimeout(() => {
                    try {
                        const over = container.scrollHeight - container.clientHeight;
                        if (over > 1) {
                            node.setSize?.([node.size?.[0], (Number(node.size?.[1]) || 0) + over]);
                        }
                    } catch (_) {}
                }, 180);
            }
        } catch (_) {}
    };
    const scheduleFit = (allowShrink = false) => {
        fitAllowShrink = allowShrink;
        clearTimeout(fitTimer);
        fitTimer = setTimeout(() => requestAnimationFrame(fitPanelHeight), 30);
    };
    node.__h3m.fitPanelHeight = () => scheduleFit(false);

    let lastWidth = Number(node.size?.[0]) || 0;
    const prevResize = node.onResize;
    node.onResize = function (...args) {
        const r = prevResize?.apply(this, args);
        syncWidth();
        lockWidth();
        if (selfResizing) return r;   // 自己触发的尺寸变化，不重新量
        const w = Number(node.size?.[0]) || 0;
        const widthChanged = Math.abs(w - lastWidth) > 0.5;
        lastWidth = w;
        // 宽度变了 → 格子尺寸变了 → 高度精确贴合；否则只保证不被裁切
        scheduleFit(widthChanged);
        return r;
    };
    // 首次真正被绘制时（标签页切回来 / 后台加载的工作流）再贴合一次：
    // 之前 clientHeight 可能是 0，量不到内容高度
    if (dom) {
        let firstDrawn = false;
        dom.options.onDraw = () => {
            if (firstDrawn || container.clientHeight <= 0) return;
            firstDrawn = true;
            scheduleFit(true);
        };
    }

    // 首次布局：默认高度按「3 行图 + 1 行音频 + 1 行视频」给足，再贴合实际内容
    lockWidth();
    syncWidth();
    if (!Number.isFinite(node.size?.[1]) || node.size[1] < 1000) {
        try { node.setSize?.([node.size?.[0] || 700, 1220]); } catch (_) {}
    }
    renderAllSlots(node);
    lastWidth = Number(node.size?.[0]) || 0;
    requestAnimationFrame(syncWidth);
    setTimeout(() => { syncWidth(); lockWidth(); scheduleFit(false); }, 60);
    setTimeout(() => scheduleFit(true), 320);

    // 执行回传：显示实际载入数量（后端 ui.media_info）
    const prevExecuted = node.onExecuted;
    node.onExecuted = function (message) {
        const r = prevExecuted?.apply(this, arguments);
        try {
            const unp = (v) => (Array.isArray(v) ? v[0] : v);
            const info = unp(message?.media_info);
            if (info && node.__h3m?.status) {
                node.__h3m.status.textContent = String(info);
                node.__h3m.status.title = String(info);
                node.__h3m.status.style.color = "var(--jz-ok)";
            }
            const errs = unp(message?.media_errors);
            if (Array.isArray(errs) && errs.length) {
                notify(errs[0], true);
            }
        } catch (_) {}
        return r;
    };
}

// 工作流载入后 widgets_values 是异步还原的 → 重新读一次
// （只在配置真的变化时才重绘：避免重复渲染把正在加载的 <audio>/<video> 请求打断）
function refreshFromWidget(node) {
    const st = node.__h3m;
    if (!st) return;
    const widget = getSettingsWidget(node) || st.widget;
    if (!widget) return;
    const raw = readWidgetValue(widget);
    // 工作流还没把 widgets_values 还原进来（空串）→ 不拿默认布局去覆盖，
    // 否则会出现「先闪成默认 9/3/3，再跳回自己保存的布局」这种跳变
    if (typeof raw === "string" && !raw.trim()) return;
    st.widget = widget;
    const next = normalizeSettings(raw);
    const prevSig = JSON.stringify(st.settings || {});
    const nextSig = JSON.stringify(next);
    st.settings = next;
    if (prevSig === nextSig) return;

    // 布局（顺序/数量/隐藏）变了 → 面板结构要重建，不能只重绘槽位
    const layoutSig = JSON.stringify(next.layout);
    if (layoutSig !== st.renderedLayoutSig) {
        fillBody(node);
        renderAllSlots(node);
        setTimeout(() => { try { st.fitPanelHeight?.(); } catch (_) {} }, 60);
        return;
    }
    renderAllSlots(node);
}

app.registerExtension({
    name: "JZL.MiniMaxH3MediaLoader",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_TYPE) return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            try {
                setupMediaLoader(this);
            } catch (err) {
                console.error("[JZL-素材加载] 初始化失败", err);
            }
            return r;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const r = onConfigure?.apply(this, arguments);
            const self = this;
            [0, 120, 400].forEach((ms) => setTimeout(() => {
                try { refreshFromWidget(self); } catch (_) {}
            }, ms));
            return r;
        };

        // 右击菜单 → 界面设置
        const prevMenu = nodeType.prototype.getExtraMenuOptions;
        nodeType.prototype.getExtraMenuOptions = function (canvas, options) {
            const r = prevMenu?.apply(this, arguments);
            if (!Array.isArray(options)) return r;
            options.push(null);   // 分隔线
            options.push({
                content: "⚙️ 界面设置",
                callback: () => {
                    try {
                        if (this.__h3m) openLayoutDialog(this);
                    } catch (err) {
                        console.error("[JZL-素材加载] 界面设置打开失败", err);
                    }
                },
            });
            return r;
        };
    },
});
