// ══════════════════════════════════════════════════════════════════════════════
//  🎭 JZL MiniMax-H3 人物替换 / 视频编辑 —— 两个节点的**全部**前端实现
//   · JZL_MiniMaxCharacterSwap（图片/视频/音频 各自独立端口进）
//   · JZL_MiniMaxVideoEdit  （素材全部从一条 ref2va 参考总线进，内部已解包）
//
//  这两个节点共用同一套面板（🎨 视频参数设置 / 🎭 采样解码设置 / 🎬 生成视频管理 /
//  📝 提示词预设），差异只有「素材怎么进来」和日志标签，所以写在一个文件里；
//  但它们**不与其他节点共享任何节点级代码** —— 本文件只 import ../asset_manager.js
//  里的通用 UI 基件（按钮、输入框、弹窗骨架、采样解码面板），导演台/素材管理器
//  那边的代码改动不会碰到这两个节点。
// ══════════════════════════════════════════════════════════════════════════════

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
    OPTIONS, _jzlSmallBtn, bigBoxGrid, bindDomWidgetMaxHeight, checkboxControl, defaultSettings,
    el, ensureManagerStyle, field, jzlCalcResolution, jzlSplitOutPrefix, makeSectionTitle,
    numberControl, openModal, passwordControl, readWidgetValue, registerPanelOverride,
    registerSettingsHook, registerSwapLikeType, renderAutoSave, renderPrefPanel, selectControl,
    setWidgetValue, stepperControl, textControl,
} from "./asset_manager.js";

// 两个节点类型：同一套面板/流程，只有「素材怎么进来」和「日志标签/落盘兜底名」不同
export const SWAP_NODE_TYPE = "JZL_MiniMaxCharacterSwap";
export const VIDEO_EDIT_NODE_TYPE = "JZL_MiniMaxVideoEdit";
export const SWAP_NODE_TYPES = [SWAP_NODE_TYPE, VIDEO_EDIT_NODE_TYPE];

// manager_settings 结构版本：用来让「一次性迁移」只跑一次（否则用户手动关掉的选项会被反复改回来）
const SWAP_SETTINGS_SCHEMA = 3;

// 该节点「分段输出」被清空 / 提示词正文跑到 story_name 上时的兜底名字
function swapDefaultName(node) {
    return (node && node.type === VIDEO_EDIT_NODE_TYPE) ? "视频编辑" : "人物替换";
}

export function swapDefaults() {
    const d = defaultSettings();
    d._schema = SWAP_SETTINGS_SCHEMA;
    const gp = d.gen_params;
    gp.megapixels = 1.0;
    gp.second_mode = "放大倍数";
    gp.upscale_scale = 2.0;
    const sd = d.sample_decode;
    Object.assign(sd.second, {
        enabled: true,
        sampler: "euler", scheduler: "simple",
        sigmas_mode: "custom",
        custom_sigmas: "0.909375, 0.725, 0.421875, 0.0000",
    });
    sd.seam.window = 22;
    delete sd.seam.settle;
    return d;
}

// 🎭 人物替换的配置裁剪：只保留面板真正读写的键（与后端 _parse_swap_settings 一一对应）
export function pruneSwapSettings(v) {
    const pick = (o, keys) => {
        const out = {};
        if (!o || typeof o !== "object") return out;
        for (const k of keys) if (o[k] !== undefined) out[k] = o[k];
        return out;
    };
    const sd = v.sample_decode || {};
    const sl = sd.selflift || {};
    const sec = sd.second || {};
    return {
        auto_save: v.auto_save !== false,
        // 配置结构版本：必须保留，否则「一次性迁移」每次都会被当成老配置重跑
        _schema: Number(v._schema || 0),
        gen_params: pick(v.gen_params, ["segment", "aspect_ratio", "megapixels",
            "scale_factor", "upscale_scale", "second_mode", "second_width", "second_height",
            "audio_mode", "video_shrink", "vision_shrink"]),
        // 📝 提示词预设：LLM 开关 / 来源 / skill / 后端配置
        prompt_cfg: pick(v.prompt_cfg, ["use_llm", "source", "builtin", "skill", "skill_dir", "llm_backend", "llm", "api"]),
        sample_decode: Object.assign(
            pick(sd, ["sample_mode", "sampler", "scheduler", "steps", "cfg", "denoise",
                "seed", "seed_mode", "decode_cleanup"]),
            {
                // 与短剧导演台同一套键：SelfLift 采样参数整组保留
                selflift: pick(sl, ["steps", "transition_step", "lowres_scale", "rho", "w_min", "w_max",
                    "upscaler_model", "upscaler_unload", "highres_tiling"]),
                seam: pick(sd.seam, ["window"]),
                second: pick(sec, ["enabled", "sampler", "scheduler", "sigmas_mode", "custom_sigmas",
                    "steps", "denoise", "precision", "upscaler_model", "device", "enable_chunking",
                    "tiling", "tiling_tiles", "tiling_overlap"]),
            }),
        save: {},
    };
}

// ══════════════════════════════════════════════════════════════
//  🎭 人物替换（JZL_MiniMaxCharacterSwap）专用面板
//  选项全部收进 3 个按钮：🎨 视频参数设置 / 🎭 采样解码设置 / 🎬 生成视频管理
//  写的是 manager_settings 里的 gen_params / sample_decode / save —— 与后端
//  _parse_swap_settings() 同一套键名，不做任何翻译（避免「面板显示一套、实际跑另一套」）。
// ══════════════════════════════════════════════════════════════
export const SWAP_SEGMENT_OPTIONS = Array.from({ length: 17 }, (_, i) => `固定${i + 4}秒`);
export const SWAP_AUDIO_OPTIONS = ["保留原声（推荐）", "使用生成音轨", "静音"];
export const SWAP_WINDOW_OPTIONS = [
    { value: 5, label: "5 帧（≈0.21s，最少）" },
    { value: 22, label: "22 帧（≈0.92s，推荐）" },
    { value: 39, label: "39 帧（≈1.63s）" },
    { value: 56, label: "56 帧（≈2.33s，最多）" },
];

export function swapFrameLen(segmentText) {
    // 与后端 _resolve_length 同规则：秒 → 24fps 帧数 → 吸附到 17k+5
    const m = String(segmentText || "").match(/(\d+(?:\.\d+)?)/);
    let n = m ? Math.max(5, Math.round(parseFloat(m[1]) * 24)) : 124;
    while (n % 17 !== 5) n += 1;
    return n;
}

export function renderSwapVideoParamsPanel(c, s, node) {
    const gp = s.gen_params || (s.gen_params = {});
    renderAutoSave(c, s);
    const touch = () => { try { node?.__jzlUpdateDisplay?.(); } catch (_) {} };

    c.append(makeSectionTitle("画幅比例"));
    c.append(bigBoxGrid(["跟随源片（推荐）"].concat(OPTIONS.aspectRatio),
        String(gp.aspect_ratio || "跟随源片（推荐）"), v => { gp.aspect_ratio = v; refreshRes(); touch(); }, 5, 26));

    c.append(makeSectionTitle("分辨率与段长"));
    c.append(field("百万像素（MP）", stepperControl(Number(gp.megapixels ?? 0.4),
        { min: 0.1, max: 3.6, step: 0.1 }, v => { gp.megapixels = v; refreshRes(); touch(); })));

    const resBox = el("div", "font-size:13px;color:var(--jz-warn);padding:6px 10px;text-align:center;"
        + "background:var(--comfy-input-bg,var(--jz-bg-raise));border:1px solid var(--border-color,var(--jz-border));"
        + "border-radius:4px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;");
    resBox.title = "生成画布（32 对齐）。「跟随源片」= 执行时按源片宽高比 + 上面的 MP 预算实时计算";
    c.append(field("分辨率", resBox));
    const refreshRes = () => {
        const ar = String(gp.aspect_ratio || "跟随源片（推荐）");
        if (ar.startsWith("跟随源片")) {
            resBox.textContent = "跟随源片宽高比（执行时计算）";
        } else {
            const [W, H] = jzlCalcResolution(ar, Number(gp.megapixels ?? 0.4));
            resBox.textContent = `${W} × ${H}`;
        }
        if (syncSecRef) syncSecRef();
    };
    let syncSecRef = null;
    refreshRes();

    c.append(field("每段时长", selectControl(SWAP_SEGMENT_OPTIONS, String(gp.segment || "固定5秒"),
        v => { gp.segment = v; showSegLen(); touch(); })));
    const segHint = el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 10px 172px;line-height:1.7;", "");
    c.append(segHint);
    const showSegLen = () => {
        const f = swapFrameLen(gp.segment || "固定5秒");
        segHint.textContent = `单窗口一次生成上限 ${f} 帧 = ${(f / 24).toFixed(2)} 秒（自动对齐到模型支持的帧数）。`
            + "\n显存小就调小（4 秒），显存大可以调大（10~15 秒）；整片会自动切成若干个这么长的窗口，且只采样一次。";
    };
    showSegLen();

    c.append(makeSectionTitle("声音"));
    c.append(field("声音来源", selectControl(["使用原声", "生成音频"], String(gp.audio_mode || "使用原声"),
        v => { gp.audio_mode = v; showAudioHint(); touch(); })));
    const audioHint = el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;", "");
    c.append(audioHint);
    function showAudioHint() {
        const gen = String(gp.audio_mode || "使用原声") === "生成音频";
        audioHint.textContent = gen
            ? "解码模型生成的音轨（需要接「音频VAE」）。源片原声依旧不参与参考编码。\n"
              + "注意：模型生成的音频是它自己「编」的，与源片对白/音效无关。"
            : "用源片原声：原声不参与参考编码，只在最后按帧号精确切给每段落盘（对白/音效原样保留）。\n"
              + "此时不需要接「音频VAE」，latent 里的音频也完全不解码。";
    }
    showAudioHint();

    // ── 编码设置：三个比例旋钮都相对「生成分辨率」，1.0 = 同尺寸，边长 32 对齐 ──
    c.append(makeSectionTitle("编码设置"));
    c.append(field("参考图片放大", stepperControl(Number(gp.scale_factor ?? 1.0),
        { min: 1.0, max: 5.0, step: 0.1 }, v => { gp.scale_factor = v; syncEncHint(); })));
    c.append(field("输入视频缩小", stepperControl(Number(gp.video_shrink ?? 1.0),
        { min: 0.2, max: 1.0, step: 0.05 }, v => { gp.video_shrink = v; syncEncHint(); })));
    c.append(field("文本视觉缩小", stepperControl(Number(gp.vision_shrink ?? 1.0),
        { min: 0.2, max: 1.0, step: 0.05 }, v => { gp.vision_shrink = v; syncEncHint(); })));
    const encHint = el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;", "");
    c.append(encHint);
    function syncEncHint() {
        const k = Number(gp.scale_factor ?? 1.0);
        const v = Number(gp.video_shrink ?? 1.0);
        const t = Number(gp.vision_shrink ?? 1.0);
        encHint.textContent =
            `参考图片放大 ×${k.toFixed(2)}：定妆图按生成分辨率等比放大（1.0 = 与画布同尺寸）。脸不够像就加，越大越慢。`
            + `\n输入视频缩小 ×${v.toFixed(2)}：参考视频的编码尺寸 = 生成分辨率 × 该值。越小越快，细节越少。`
            + `\n文本视觉缩小 ×${t.toFixed(2)}：送进文本编码器的参考帧尺寸 = 编码尺寸 × 该值。越小文本编码越快，不改变生成画布。`
            + "\n三项边长均对齐 32；1.0 表示不改变（默认与原版一致）。";
        if (syncSecRef) syncSecRef();
    }
    syncEncHint();

    c.append(makeSectionTitle("二采放大"));
    c.append(field("二采放大方式", selectControl(
        [{ value: "放大倍数", label: "面积倍数（推荐，宽高比自动跟随一采）" },
         { value: "目标分辨率", label: "目标分辨率（固定宽×高，竖屏慎用）" }],
        String(gp.second_mode || "放大倍数"), v => { gp.second_mode = v; syncSecMode(); touch(); })));
    const fSecW = field("二采目标宽度", stepperControl(Number(gp.second_width ?? 1280),
        { min: 32, max: 4096, step: 32 }, v => { gp.second_width = Math.round(v); touch(); }));
    const fSecH = field("二采目标高度", stepperControl(Number(gp.second_height ?? 704),
        { min: 32, max: 4096, step: 32 }, v => { gp.second_height = Math.round(v); touch(); }));
    const fSecScale = field("面积倍数", stepperControl(Number(gp.upscale_scale ?? 2.0),
        { min: 1.0, max: 4.0, step: 0.1 }, v => { gp.upscale_scale = v; touch(); }));
    c.append(fSecScale, fSecW, fSecH);
    const secBox = el("div", "font-size:11px;color:var(--jz-warn);padding:5px 10px;"
        + "background:var(--comfy-input-bg,var(--jz-bg-raise));border:1px solid var(--border-color,var(--jz-border));"
        + "border-radius:4px;font-variant-numeric:tabular-nums;");
    c.append(field("二采画布", secBox));
    const syncSecMode = () => {
        const isTgt = String(gp.second_mode || "放大倍数") === "目标分辨率";
        fSecW.style.display = isTgt ? "flex" : "none";
        fSecH.style.display = isTgt ? "flex" : "none";
        fSecScale.style.display = isTgt ? "none" : "flex";
        // 预览二采画布：面积倍数模式按一采画布 × sqrt(倍数)，比例永远不变
        try {
            const ar = String(gp.aspect_ratio || "跟随源片（推荐）");
            const mp = Number(gp.megapixels ?? 1.0) || 1.0;
            if (isTgt) {
                const tw = Math.round(Number(gp.second_width ?? 1280) / 32) * 32;
                const th = Math.round(Number(gp.second_height ?? 704) / 32) * 32;
                secBox.textContent = `${tw} × ${th}（固定尺寸，比例可能被改）`;
            } else if (ar.startsWith("跟随源片")) {
                secBox.textContent = `一采面积 × ${Number(gp.upscale_scale ?? 2.0).toFixed(2)}`
                    + `（宽高比跟随源片，执行时计算）`;
            } else {
                const [W, H] = jzlCalcResolution(ar, mp);
                const s = Math.sqrt(Math.max(1, Number(gp.upscale_scale ?? 2.0)));
                const tw = Math.round(W * s / 32) * 32;
                const th = Math.round(H * s / 32) * 32;
                const a = (tw * th) / Math.max(1, W * H);
                secBox.textContent = `${tw} × ${th}　（面积 ${a.toFixed(2)}x）`
                    + (a < 1 ? "　⚠️ 比一采小，只会更糊" : "");
            }
        } catch (_) {
            secBox.textContent = "执行时计算";
        }
    };
    syncSecMode();
    syncSecRef = syncSecMode;
    c.append(el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;",
        "面积倍数 = 二采像素面积 ÷ 一采像素面积。2.0 就是「面积翻倍」（参考工作流 0.5MP → 1.0MP）。\n"
        + "必须 > 1.0 才有提清晰度的意义；< 1.0 等于降级，会更糊。\n"
        + "想省时间：把「百万像素」降到 0.5，二采面积倍数保持 2.0 → 二采 1.0MP（参考工作流同款）。"));

}

export function renderSwapSavePanel(c, s, node) {
    // 🎭 人物替换：落盘恒为「逐段落盘」（长片必须分块解码，否则整片 IMAGE 会爆内存），
    //    合并交给下游「XB-BOX - 🎬 视频批量拼接」，所以这里没有保存模式开关。
    c.append(makeSectionTitle("保存"));
    c.append(el("div", "font-size:12px;color:var(--descrip-text,var(--jz-text-dim));margin:0 0 10px 172px;line-height:1.8;white-space:pre-line;",
        "本节点恒为逐段落盘：短片（≤ 约 50 秒）落 1 个文件；更长的片按解码块切成多个文件，\n"
        + "每块解完立刻写盘并释放内存 —— 内存不随片长增长，5 分钟、10 分钟都跑得动。\n\n"
        + "要把分段拼成一整条：把输出端口「批量视频」接到「XB-BOX - 🎬 视频批量拼接」，\n"
        + "勾选「拼接保存」（stream copy 无损，不重新编码）；不想留分段就再勾「拼接后删除分段视频」。"));
    const _pfxW = (node?.widgets || []).find((w) => w.name === "story_name");
    const _pfx = jzlSplitOutPrefix(String(_pfxW?.value ?? "") || "视频/人物替换");
    c.append(el("div", "font-size:12px;color:var(--descrip-text,var(--jz-text-dim));margin:0 0 8px 172px;line-height:1.8;",
        "落盘：「分段输出」= 文件夹/视频名称 → output/" + (_pfx.dir ? _pfx.dir + "/" : "")
        + _pfx.name + "_2次生成分段1_00001.mp4"));
}

export function renderSwapSeamSection(c, s, node) {
    // 🎭 人物替换独有的「分段衔接」：只有「重叠帧数」一个旋钮。
    //    整片是一次采样跑完的，重叠区只在窗口之间做混合，不再有「上段传递下段」。
    const sd = s.sample_decode || (s.sample_decode = {});
    const seam = sd.seam || (sd.seam = {});
    const touch = () => { try { node?.__jzlUpdateDisplay?.(); } catch (_) {} };

    c.append(makeSectionTitle("分段衔接"));
    c.append(field("重叠帧数", selectControl(SWAP_WINDOW_OPTIONS, Number(seam.window ?? 22),
        v => { seam.window = Number(v); touch(); })));
    c.append(el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;",
        "相邻两个窗口重叠多少帧。重叠区里两个窗口的预测按斜坡加权平均，所以看不出接缝。\n"
        + "只能选 5 / 22 / 39 / 56（和模型的 17 帧分组对齐）。默认 22；接缝仍看得出就加到 39。"));
    c.append(el("div", "font-size:12px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 8px 172px;line-height:1.7;white-space:pre-line;",
        "怎么跑的：整片 latent 一次采样；每一步都把它切成若干个「≤ 每段时长」的窗口，\n"
        + "每个窗口用自己的参考视频切片 + 定妆图 + 提示词预测，再在重叠区混合成一整片。\n"
        + "窗口之间不传递任何已生成的画面 → 不会越往后越不像；每个模型只加载一次 → 不会越跑越慢。"));
}

export function renderSwapPrefPanel(c, s, d, node) {
    // ⭐ 100% 复刻「JZL - 🤖 MiniMax-H3短剧导演台」的 🎭 采样解码设置：
    //    直接调用同一个 renderPrefPanel（采样分类 / 随机种子 / 采样 / 二次采样（含 Sigmas 模式、空间分块）/
    //    SelfLift 采样 / 解码），保证两个节点的面板逐项一致、改一处两边同步。
    //    唯一差别：本节点不显示「♾️ 无限时长衔接」（renderPrefPanel 内部以 node.type 判断，自动跳过）。
    renderPrefPanel(c, s, d, node);
    renderSwapSeamSection(c, s, node);
}

export function renderSwapPromptPanel(c, s, d, node) {
    // 📝 提示词预设：LLM 增强状态（由来源派生，只读）→ 提示词来源 → skill → LLM 后端/参数 → 预览提示词 → 使用说明（折叠）
    const pc = s.prompt_cfg || (s.prompt_cfg = {});
    const touch = () => { try { node?.__jzlUpdateDisplay?.(); } catch (_) {} };
    const commit = () => {
        try { c.dispatchEvent(new Event("change", { bubbles: true })); } catch (_) {}
        touch();
    };

    // ── ① LLM 增强状态：不再单独开关，完全由「提示词来源」决定 ──
    c.append(makeSectionTitle("LLM 增强"));
    const llmState = el("div", "font-size:13px;color:var(--fg-color,var(--jz-text));padding:6px 10px;"
        + "background:var(--comfy-input-bg,var(--jz-bg-raise));border:1px solid var(--border-color,var(--jz-border));"
        + "border-radius:4px;", "");
    c.append(field("当前状态", llmState));
    const syncLlmState = () => {
        const src = String(pc.source || "系统默认");
        const on = src !== "系统默认";
        pc.use_llm = on;
        llmState.textContent = (on ? "✅ 开启" : "⛔ 关闭") + `（来源：${src}）`
            + (on ? "" : " —— 用内置模板，不加载任何 LLM");
        llmState.style.color = on ? "var(--jz-ok, #57d977)" : "var(--jz-text-dim)";
    };

    // ── ② 提示词来源：两个分支（方形勾选单选，与「LLM 后端」的 本地/在线 同一交互）──
    c.append(makeSectionTitle("提示词来源"));
    // 老工作流的「内置skill·动物」等值 → 分支 + 内置名
    const BUILTIN_ALIAS = { "内置skill·动物": "动物替换", "内置skill·非活物": "非活物替换", "内置skill·多人": "多人替换" };
    if (BUILTIN_ALIAS[String(pc.source || "")]) {
        pc.builtin = BUILTIN_ALIAS[pc.source];
        pc.source = "内置skill";
    }
    if (!["系统默认", "内置skill", "自定义skill"].includes(String(pc.source || ""))) pc.source = "系统默认";
    const BUILTIN_HINT = {
        "人物替换": "把视频里的人换成参考图中的人（最常用）。",
        "动物替换": "用参考图里的**动物替换**视频里的人，动物拟人化表演；不是加入画面、不与原人互动。",
        "非活物替换": "用参考图里的**物件替换**视频里的主体（玩具/玩偶/毛绒玩具/木头人/火柴人/机器人/砖头人…），"
            + "保持自身材质做拟人化动作；不是当道具、也不变活物。",
        "多人替换": "**多人同时替换**：每个人分别换成对应那张定妆图（图1→第1个人、图2→第2个人…），"
            + "人数与站位不变、身份不串味。",
    };
    const BUILTIN_NAMES = ["人物替换", "动物替换", "非活物替换", "多人替换"];
    // 内置目录里除四档自带 skill 之外的文件 = 用户「📥 导入Skill文件」放进去的，也出现在这个下拉里
    const BUILTIN_FILES = ["人物替换提示词.md", "动物替换提示词.md", "非活物替换提示词.md", "多人替换提示词.md"];
    const importedSkills = ((d?.prompt_skills || []).filter(
        (f) => f && !f.startsWith("（") && !BUILTIN_FILES.includes(String(f))));
    const builtinOptions = BUILTIN_NAMES.map((n) => ({ value: n, label: n })).concat(
        importedSkills.map((f) => ({ value: String(f), label: String(f).replace(/\.[^.]+$/, "") + "（导入）" })));
    if (!builtinOptions.some((o) => o.value === String(pc.builtin || ""))) pc.builtin = "人物替换";

    const srcRow = el("div", "display:flex;align-items:center;gap:16px;min-width:0;flex-wrap:wrap;");
    const srcCbs = [];
    const mkSrc = (val, label) => {
        const lab = el("label", "display:flex;align-items:center;gap:6px;font-size:13px;color:var(--descrip-text,var(--jz-text-dim));cursor:pointer;");
        const cb = el("input", "width:18px;height:18px;accent-color:#f59e0b;cursor:pointer;");
        cb.type = "checkbox";
        cb.checked = (String(pc.source) === val);
        cb.addEventListener("change", () => {
            if (cb.checked) {
                pc.source = val;
                srcCbs.forEach((x) => { if (x !== cb) x.checked = false; });
                syncSource();
                commit();
            } else if (srcCbs.every((x) => !x.checked)) {
                cb.checked = true;
            }
        });
        lab.append(cb, el("span", "", label));
        srcRow.append(lab);
        srcCbs.push(cb);
        return cb;
    };
    c.append(field("来源", srcRow));
    const cbSystem = mkSrc("系统默认", "系统默认");
    const cbBuiltin = mkSrc("内置skill", "内置skill");
    const cbCustom = mkSrc("自定义skill", "自定义skill");
    const srcRowWrap = srcRow.parentElement;
    const srcHint = el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;", "");
    c.append(srcHint);

    // ── 内置分支：选具体哪一个内置 skill（第一排右侧带「📥 导入Skill文件」）──
    const builtinBox = el("div", "");
    const builtinSel = selectControl(builtinOptions, String(pc.builtin),
        (v) => { pc.builtin = v; syncSource(); commit(); });
    const btnImportSkill = _jzlSmallBtn("📥 导入Skill文件",
        "把一个 .md/.txt 导入到本节点的内置 skill 目录（skills/jzl），导入后直接在左边的下拉里选择使用");
    const builtinRow = el("div", "display:flex;align-items:center;gap:8px;min-width:0;flex-wrap:wrap;");
    builtinRow.append(builtinSel, btnImportSkill);
    builtinBox.append(field("内置 skill", builtinRow));
    const importMsg = el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 8px 172px;line-height:1.7;", "");
    builtinBox.append(importMsg);
    const builtinHint = el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;", "");
    builtinBox.append(builtinHint);

    // 导入：浏览器选文件 → POST /jzl/swap_skill_import（后端存进插件 skills/jzl，同名自动加序号）
    const importInput = document.createElement("input");
    importInput.type = "file";
    importInput.accept = ".md,.markdown,.txt";
    importInput.style.display = "none";
    const doImportSkill = async (file) => {
        if (!file) return;
        btnImportSkill.disabled = true;
        const old = btnImportSkill.textContent;
        btnImportSkill.textContent = "导入中…";
        try {
            const fd = new FormData();
            fd.append("file", file, file.name || "自定义skill.md");
            const r = await api.fetchApi("/jzl/swap_skill_import", { method: "POST", body: fd });
            const j = await r.json().catch(() => ({}));
            if (!j || !j.ok) throw new Error((j && j.error) || "导入失败");
            const newId = String(j.id || "");
            // 把新文件补进下拉并选中（不重渲染面板，避免丢焦点）
            const label = newId.replace(/\.[^.]+$/, "") + "（导入）";
            let op = null;
            for (const c2 of builtinSel.children) {
                if (String(c2.value) === newId) { op = c2; break; }
            }
            if (!op) {
                op = document.createElement("option");
                op.value = newId; op.textContent = label;
                builtinSel.append(op);
            }
            builtinSel.value = newId;
            pc.builtin = newId;
            syncSource();
            commit();
            importMsg.textContent = `✅ 已导入「${newId}」并设为当前内置 skill`;
            importMsg.style.color = "var(--jz-ok, #57d977)";
        } catch (e) {
            importMsg.textContent = "❌ " + String((e && e.message) || e);
            importMsg.style.color = "var(--jz-error, #e55)";
        } finally {
            btnImportSkill.disabled = false;
            btnImportSkill.textContent = old;
        }
    };
    btnImportSkill.addEventListener("click", () => importInput.click());
    importInput.addEventListener("change", () => {
        const f = importInput.files && importInput.files[0];
        importInput.value = "";
        doImportSkill(f);
    });
    builtinBox.append(importInput);
    c.append(builtinBox);

    // ── 自定义分支：先「📂 选择 skill 文件目录」，再在它下面的「已有 skill」里选具体文件 ──
    //    改成目录之后：技能可以放在任何位置，不必再复制进插件目录；
    //    换目录 → 下拉自动列出该目录下的 .md/.txt（点「🔄 刷新」可重新扫描）。
    const PLUGIN_DIR = String(d?.prompt_skills_dir || "skills/jzl");
    const pluginSkills = (d?.prompt_skills && d.prompt_skills.length) ? d.prompt_skills : [];
    const customBox = el("div", "");

    // ① 目录行：选择按钮 + 可手填(云机/无桌面)的路径框 + 刷新
    const dirInput = document.createElement("input");
    dirInput.className = "jzl-input";
    dirInput.type = "text";
    dirInput.spellcheck = false;
    dirInput.placeholder = "skill 目录（绝对路径），也可手填后点 🔄 刷新";
    dirInput.value = String(pc.skill_dir || "");
    dirInput.style.cssText = "flex:1 1 0;min-width:0;font-size:12px;"
        + "font-family:ui-monospace,Consolas,monospace;padding:6px 10px;"
        + "background:var(--comfy-input-bg,var(--jz-bg-raise));color:var(--jz-text);"
        + "border:1px solid var(--jz-border);border-radius:4px;";
    dirInput.addEventListener("mousedown", (e) => e.stopPropagation());
    const btnPickDir = _jzlSmallBtn("📂 选择 skill 文件目录",
        "选一个文件夹（里面的 .md/.txt 会出现在下面的「已有 skill」）；云机无桌面时可在右边手填路径");
    const btnRefresh = _jzlSmallBtn("🔄 刷新", "重新扫描该目录下的 .md/.txt");
    const dirRow = el("div", "display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap;");
    dirRow.append(el("label", "flex:0 0 160px;font-size:13px;color:var(--descrip-text,var(--jz-text-dim));white-space:nowrap;", "skill 目录"));
    dirRow.append(dirInput, btnPickDir, btnRefresh);
    customBox.append(dirRow);

    const dirMsg = el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 8px 172px;line-height:1.7;", "");
    customBox.append(dirMsg);

    // ② 「已有 skill」下拉：内容 = 当前目录扫描结果（未选目录时先显示插件自带的）
    const skillSel = selectControl(pluginSkills.length ? pluginSkills : ["（该目录下没有 .md/.txt）"],
        String(pc.skill || (pluginSkills[0] || "")), (v) => {
            pc.skill = String(v).startsWith("（") ? "" : v;
            syncSource();
            commit();
        });
    if (!pc.skill && pluginSkills.length) pc.skill = pluginSkills[0];
    customBox.append(field("已有 skill", skillSel));

    customBox.append(el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;",
        "上面选目录，下面选该目录里的哪个文件。整份文件作为 LLM 的设定词（.md / .txt，≤1MB，UTF-8）。\n"
        + "没选目录时这里显示插件自带的 skills/jzl。技能文件留在原位置即可，不会被复制进插件目录。"));

    // 用扫描结果重建下拉（保留当前选择；文件消失则退回第一个）
    const fillSkills = (items) => {
        const list = (items && items.length) ? items : [];
        skillSel.innerHTML = "";
        if (!list.length) {
            const op = document.createElement("option");
            op.value = ""; op.textContent = "（该目录下没有 .md/.txt）";
            skillSel.append(op);
            pc.skill = "";
            return;
        }
        for (const it of list) {
            const op = document.createElement("option");
            op.value = String(it); op.textContent = String(it);
            if (String(it) === String(pc.skill || "")) op.selected = true;
            skillSel.append(op);
        }
        if (!list.includes(String(pc.skill || ""))) pc.skill = list[0];
        skillSel.value = pc.skill;
    };

    // 目录变了：记录 + 重新列文件 + 立即保存
    const applyDir = (dir, items, note) => {
        const v = String(dir || "").trim();
        if (v) pc.skill_dir = v;
        dirInput.value = String(pc.skill_dir || "");
        fillSkills(items || []);
        syncSource();
        commit();
        if (note) {
            dirMsg.textContent = note;
            dirMsg.style.color = "var(--jz-ok, #57d977)";
        }
    };

    // 选目录：先问后端弹系统目录框；无桌面时后端会返回 error，提示用户手填
    const pickDir = async () => {
        btnPickDir.disabled = true;
        const old = btnPickDir.textContent;
        btnPickDir.textContent = "选择中…";
        try {
            const r = await api.fetchApi("/jzl/swap_skill_dir", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({}),
                timeoutMs: 120000,      // 系统弹窗是阻塞的，用户可能要想一会儿
            });
            const j = await r.json().catch(() => ({}));
            if (j && j.error) {
                dirMsg.textContent = "⚠️ " + String(j.error);
                dirMsg.style.color = "var(--jz-warn)";
                return;
            }
            if (!j || !j.dir) return;                      // 用户取消了，什么都不做
            applyDir(j.dir, j.items || [], `✅ 目录：${j.dir}（${(j.items || []).length} 个 skill）`);
        } catch (e) {
            dirMsg.textContent = "❌ " + String((e && e.message) || e);
            dirMsg.style.color = "var(--jz-error, #e55)";
        } finally {
            btnPickDir.disabled = false;
            btnPickDir.textContent = old;
        }
    };

    // 刷新：按路径框里的目录（或手填的）重新扫描
    const refreshDir = async () => {
        const want = String(dirInput.value || "").trim();
        if (!want) {
            dirMsg.textContent = "⚠️ 先选目录，或在路径框里填一个目录";
            dirMsg.style.color = "var(--jz-warn)";
            return;
        }
        btnRefresh.disabled = true;
        try {
            const r = await api.fetchApi(`/jzl/swap_skill_list?dir=${encodeURIComponent(want)}`, { timeoutMs: null });
            const j = await r.json().catch(() => ({}));
            if (!j || j.ok === false) {
                dirMsg.textContent = "⚠️ " + String((j && j.error) || "扫描失败");
                dirMsg.style.color = "var(--jz-warn)";
                return;
            }
            applyDir(j.dir || want, j.items || [], `✅ 目录：${j.dir || want}（${(j.items || []).length} 个 skill）`);
        } catch (e) {
            dirMsg.textContent = "❌ " + String((e && e.message) || e);
            dirMsg.style.color = "var(--jz-error, #e55)";
        } finally {
            btnRefresh.disabled = false;
        }
    };

    btnPickDir.addEventListener("click", () => { pickDir(); });
    btnRefresh.addEventListener("click", () => { refreshDir(); });
    // 手改路径后回车 = 扫描
    dirInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); refreshDir(); }
    });
    dirInput.addEventListener("change", () => { refreshDir(); });
    c.append(customBox);

    const syncSource = () => {
        const SRC_ORDER = ["系统默认", "内置skill", "自定义skill"];
        // 三个选项始终可点；LLM 开不开由选择本身决定（不再有独立开关）
        srcCbs.forEach((cb) => { cb.disabled = false; });
        srcRowWrap.style.opacity = "";
        srcCbs.forEach((cb, i) => { cb.checked = (String(pc.source || "系统默认") === SRC_ORDER[i]); });
        pc.use_llm = (String(pc.source || "系统默认") !== "系统默认");
        if (typeof syncLlmState === "function") syncLlmState();
        const isBuiltin = pc.source === "内置skill";
        builtinBox.style.display = isBuiltin ? "block" : "none";
        customBox.style.display = (pc.source === "自定义skill") ? "block" : "none";
        if (pc.source === "自定义skill") {
            // 提示语里直接点出「目录 + 文件」，省得再去上面两行找
            const dir = String(pc.skill_dir || "").trim();
            srcHint.textContent = dir
                ? `用该目录里的「${String(pc.skill || "（未选择）")}」作为 LLM 设定词，由 LLM 写提示词。`
                : "用你选的 .md 作为 LLM 设定词，由 LLM 写提示词（还没选目录时用插件自带 skills/jzl）。";
            return;
        }
        if (pc.source === "系统默认") {
            srcHint.textContent = "只用内置模板（把两段描述填进 detailed_description），不调用 LLM。";
            return;
        }
        const name = String(pc.builtin || "人物替换");
        const isImported = !BUILTIN_NAMES.includes(name);
        builtinHint.textContent = isImported
            ? "（导入的 skill）文件放在插件目录 skills/jzl/ 里，整份作为 LLM 设定词；"
              + "没有专用兜底模板，LLM 失败时回退系统默认模板。"
            : (BUILTIN_HINT[name] || "");
        srcHint.textContent = isImported
            ? `用导入的 skill「${name}」作为 LLM 设定词，由 LLM 写提示词。`
            : "用插件自带 skill（" + name + "）作为 LLM 设定词，由 LLM 写提示词。";
    };
    // ── ③ LLM 后端 ──
    const llm = pc.llm || (pc.llm = {});
    const apiCfg = pc.api || (pc.api = {});   // 注意：不能叫 api，会遮蔽 ComfyUI 的 fetchApi 模块
    // 缺值/脏值时显式落成「本地模型」：后端也按同一规则判定，
    // 否则会出现「界面勾的是 CLIP，跑起来却去请求 API 报 401」的错位。
    const rawBackend = String(pc.llm_backend || "").toLowerCase();
    const isApiBackend = /api|在线/.test(rawBackend);
    if (!/local|本地|api|在线/.test(rawBackend)) {
        pc.llm_backend = "本地模型 [local]";
    }
    c.append(makeSectionTitle("LLM 后端"));
    const backendRow = el("div", "display:flex;align-items:center;gap:16px;min-width:0;");
    const backendCbs = [];
    const mkBackend = (val, label) => {
        const lab = el("label", "display:flex;align-items:center;gap:6px;font-size:13px;color:var(--descrip-text,var(--jz-text-dim));cursor:pointer;");
        const cb = el("input", "width:18px;height:18px;accent-color:#f59e0b;cursor:pointer;");
        cb.type = "checkbox";
        cb.checked = (isApiBackend ? "在线API [api]" : "本地模型 [local]") === val;
        cb.addEventListener("change", () => {
            if (cb.checked) {
                pc.llm_backend = val;
                backendCbs.forEach((x) => { if (x !== cb) x.checked = false; });
                syncBackend();
                commit();
            } else if (backendCbs.every((x) => !x.checked)) {
                cb.checked = true;
            }
        });
        lab.append(cb, el("span", "", label));
        backendRow.append(lab);
        backendCbs.push(cb);
    };
    c.append(field("后端", backendRow));

    // 本地：官方「Load CLIP」+「Generate Text」（models/text_encoders ∪ models/clip）
    const localBox = el("div", "");
    const clipModels = (d?.all_clip_models && d.all_clip_models.length) ? d.all_clip_models
        : ((d?.clip_models && d.clip_models.length) ? d.clip_models : ["（models/text_encoders 与 models/clip 里没有模型）"]);
    if (!llm.clip_name && clipModels.length && !String(clipModels[0]).startsWith("（")) llm.clip_name = clipModels[0];
    const clipTypes = (d?.clip_types && d.clip_types.length) ? d.clip_types : ["qwen_image"];
    localBox.append(field("CLIP 模型", selectControl(clipModels, String(llm.clip_name || clipModels[0]),
        (v) => { llm.clip_name = String(v).startsWith("（") ? "" : v; commit(); })));
    localBox.append(field("CLIP 类型", selectControl(clipTypes, String(llm.clip_type || "qwen_image"),
        (v) => { llm.clip_type = v; commit(); })));
    localBox.append(field("max_length", numberControl(llm.max_length ?? 2048, { min: 1, max: 32768, step: 1 },
        (v) => { llm.max_length = Math.round(v); })));
    localBox.append(field("temperature", numberControl(llm.temperature ?? 0.7, { min: 0.01, max: 2, step: 0.01 },
        (v) => { llm.temperature = v; })));
    localBox.append(field("top_k", numberControl(llm.top_k ?? 64, { min: 0, max: 1000, step: 1 },
        (v) => { llm.top_k = Math.round(v); })));
    localBox.append(field("top_p", numberControl(llm.top_p ?? 0.95, { min: 0, max: 1, step: 0.01 },
        (v) => { llm.top_p = v; })));
    localBox.append(field("min_p", numberControl(llm.min_p ?? 0.05, { min: 0, max: 1, step: 0.01 },
        (v) => { llm.min_p = v; })));
    localBox.append(field("repetition_penalty", numberControl(llm.repetition_penalty ?? 1.05, { min: 0, max: 5, step: 0.01 },
        (v) => { llm.repetition_penalty = v; })));
    localBox.append(field("presence_penalty", numberControl(llm.presence_penalty ?? 0.0, { min: 0, max: 5, step: 0.01 },
        (v) => { llm.presence_penalty = v; })));
    localBox.append(field("seed", numberControl(llm.seed ?? 0, { min: 0, max: 4294967295, step: 1 },
        (v) => { llm.seed = Math.round(v); })));
    localBox.append(field("thinking", checkboxControl(llm.thinking === true, "模型支持时开启 thinking（推理模式）",
        (v) => { llm.thinking = !!v; })));
    localBox.append(field("默认模板", checkboxControl(llm.use_default_template !== false, "用模型自带的 chat 模板（关掉则 system_prompt 被忽略）",
        (v) => { llm.use_default_template = !!v; })));
    localBox.append(el("div", "font-size:11px;color:var(--descrip-text,var(--jz-text-dim));margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;",
        "与官方「Load CLIP」+「Generate Text」同一条链路：模型来自 models/text_encoders，**类型必须与模型配套**（选错会加载失败）。\n"
        + "建议用 qwen3vl_4b_* 这类小模型写提示词（快、省显存）；qwen3vl_32b_minimax_h3_* 是主模型配套的大 CLIP，也能生成但很慢很吃显存。\n"
        + "生成完会自动把文本编码器从显存退回，不占用后面视频模型的显存。"));
    c.append(localBox);

    // 在线 API
    const apiBox = el("div", "");
    apiBox.append(field("服务商", selectControl(OPTIONS.providers, String(apiCfg.provider || OPTIONS.providers[0]),
        (v) => { apiCfg.provider = v; })));
    apiBox.append(field("模型", textControl(apiCfg.model || "deepseek-flash", "deepseek-flash…",
        (v) => { apiCfg.model = v; })));
    apiBox.append(field("API Key", passwordControl(apiCfg.api_key || "", "sk-…（已隐藏）",
        (v) => { apiCfg.api_key = v; })));
    apiBox.append(field("Base URL", textControl(apiCfg.base_url || "https://apiCfg.deepseek.com", "https://apiCfg.deepseek.com",
        (v) => { apiCfg.base_url = v; })));
    apiBox.append(field("temperature", numberControl(apiCfg.temperature ?? 0.6, { min: 0, max: 2, step: 0.01 },
        (v) => { apiCfg.temperature = v; })));
    apiBox.append(field("max_tokens", numberControl(apiCfg.max_tokens ?? 81920, { min: 1, max: 262144, step: 1 },
        (v) => { apiCfg.max_tokens = Math.round(v); })));
    apiBox.append(field("thinking", selectControl(["disabled", "enabled"], apiCfg.thinking || "disabled",
        (v) => { apiCfg.thinking = v; })));
    c.append(apiBox);

    const syncBackend = () => {
        const isApi = /api|在线/.test(String(pc.llm_backend || "").toLowerCase());
        localBox.style.display = isApi ? "none" : "block";
        apiBox.style.display = isApi ? "block" : "none";
    };
    mkBackend("本地模型 [local]", "本地模型");
    mkBackend("在线API [api]", "在线API");
    syncBackend();
    syncSource();

    // ── ④ 预览提示词：等于串联了一个「预览任意」——节点跑起来就把最终提示词显示在这里 ──
    c.append(makeSectionTitle("预览提示词"));
    const btnCopy = el("button",
        "background:var(--jz-bg-raise);color:var(--jz-text);border:1px solid var(--jz-border);border-radius:5px;"
        + "padding:4px 12px;font-size:12px;cursor:pointer;white-space:nowrap;line-height:1.4;",
        "📋 复制");
    btnCopy.type = "button";
    btnCopy.title = "把下面的提示词复制到剪贴板";
    btnCopy.addEventListener("mousedown", (e) => e.stopPropagation());
    const btnClear = el("button",
        "background:transparent;color:var(--jz-text-dim);border:1px solid var(--jz-border);border-radius:5px;"
        + "padding:4px 12px;font-size:12px;cursor:pointer;white-space:nowrap;line-height:1.4;",
        "🧹 清空");
    btnClear.type = "button";
    btnClear.title = "清空预览框（不影响已保存的配置）";
    btnClear.addEventListener("mousedown", (e) => e.stopPropagation());
    const valRow = el("div", "display:flex;align-items:center;gap:8px;margin:0 0 8px 172px;flex-wrap:wrap;");
    const valState = el("span", "font-size:12px;color:var(--descrip-text,var(--jz-text-dim));", "尚未运行");
    valRow.append(btnCopy, btnClear, valState);
    c.append(valRow);

    const previewBox = document.createElement("textarea");
    previewBox.readOnly = true;
    previewBox.spellcheck = false;
    previewBox.placeholder = "运行节点后，这里实时显示本次喂给模型的最终提示词（只读）。";
    previewBox.style.cssText = "width:100%;height:220px;box-sizing:border-box;resize:vertical;"
        + "background:var(--jz-bg-raise);color:var(--jz-text);border:1px solid var(--jz-border);border-radius:4px;"
        + "padding:8px;font-size:12px;line-height:1.55;font-family:ui-monospace,Consolas,monospace;"
        + "overflow:auto;white-space:pre-wrap;";
    previewBox.addEventListener("mousedown", (e) => e.stopPropagation());
    c.append(previewBox);

    // 节点跑起来时后端会推 jzl_swap_ui（提示词 / 真实种子）；面板打开前的推送缓存在 node 上
    const applyPreview = (txt, src) => {
        const v = String(txt || "");
        if (!v) return;
        node.__jzlLastPrompt = v;
        previewBox.value = v;
        valState.textContent = (src ? `${src} · ` : "") + `${v.length} 字`
            + (node.__jzlLastPromptTime ? ` · ${node.__jzlLastPromptTime}` : "");
        valState.style.color = "var(--jz-ok, #57d977)";
    };
    if (node.__jzlLastPrompt) applyPreview(node.__jzlLastPrompt, node.__jzlLastPromptSrc);
    node.__jzlApplyPrompt = applyPreview;

    btnCopy.addEventListener("click", async () => {
        const txt = previewBox.value || "";
        if (!txt) { valState.textContent = "还没有内容（先运行一次节点）"; return; }
        try {
            await navigator.clipboard.writeText(txt);
            valState.textContent = "📋 已复制";
            valState.style.color = "var(--jz-ok, #57d977)";
        } catch (_) {
            previewBox.select();
            try { document.execCommand("copy"); valState.textContent = "📋 已复制"; } catch (__) {
                valState.textContent = "复制失败，请手动 Ctrl+C";
            }
        }
    });
    btnClear.addEventListener("click", () => {
        previewBox.value = "";
        node.__jzlLastPrompt = "";
        valState.textContent = "已清空（下次运行会重新填充）";
        valState.style.color = "var(--descrip-text,var(--jz-text-dim))";
    });

    // ── ⑤ 使用说明（折叠，避免占满面板）──
    const det = document.createElement("details");
    det.style.cssText = "margin:16px 0 8px;";
    const sum = document.createElement("summary");
    sum.textContent = "📖 节点使用说明（点开）";
    sum.style.cssText = "cursor:pointer;font-size:14px;font-weight:700;color:var(--jz-warn);";
    det.appendChild(sum);
    const body = el("div", "");
    det.appendChild(body);
    c.append(det);
    const p = (txt) => body.append(el("p", "margin:6px 0;line-height:1.8;font-size:13px;color:var(--fg-color,var(--jz-text));", txt));
    const h = (txt) => body.append(el("div", "margin:12px 0 6px;font-size:13px;font-weight:700;color:var(--jz-warn);", txt));

    h("① 接线");
    p("源视频 ← 「加载视频 / GetVideoComponents」的 IMAGE 输出（按 24fps 解释。源片不是 24fps 时，节点会用原声真实时长自动反推帧率并升/降帧，时长不变）。");
    p("源视频原声 ← 同一条时间轴上的 AUDIO。它不参与参考编码，只在「声音来源 = 使用原声」时按帧号切给每段落盘。");
    p("定妆图 ref_image_0..8 ← 新演员参考图（<Picture 1..9>），建议正面清晰、单人。");
    p("主模型 / CLIP / 视觉VAE ← 与导演台一致，建议用 ref2va 变体模型。音频VAE 只在「声音来源 = 生成音频」时需要。");
    p("输出只有一个端口「批量视频」：每段一个 VIDEO，接「XB-BOX - 🎬 视频批量拼接」勾选「拼接保存」即可无损拼成一整条。");

    h("② 两个描述 + 两个提示词来源分支");
    p("🎬 视频描述：源视频里发生了什么（主体/动作/场景/镜头/光线/节奏）。");
    p("🖼 图片描述：参考图里主体长什么样 —— 人物：脸型/发型/服装/配饰/配色/气质；动物：品种/毛色花纹/体型/耳朵尾巴/眼神；"
        + "非活物：类别/材质/颜色/纹理/比例/结构细节/配饰。越具体越像。");
    p("来源分两个分支（和「LLM 后端 = 本地模型 / 在线API」一样选一个）：");
    p("① 内置skill（插件自带）→ 再选四个内置 skill 之一：\n"
        + "   · 人物替换：把视频里的人换成参考图中的人（最常用）。\n"
        + "   · 动物替换：用参考图里的动物**替换**视频里的人，动物拟人化表演；不是加入画面、不与原人互动。\n"
        + "   · 非活物替换：用参考图里的**物件替换**视频里的主体（玩具/玩偶/毛绒玩具/木头人/火柴人/机器人/砖头人…），"
        + "保持自身材质做拟人化动作；不是当道具、也不变活物。\n"
        + "   · 多人替换：**多人同时替换**，每个人分别换成对应那张定妆图（图1→第1个人、图2→第2个人…），人数与站位不变、身份不串味。\n"
        + "   右边的「📥 导入Skill文件」可以把你自己写的 .md/.txt 存进插件的内置 skill 目录（skills/jzl/），"
        + "导入完直接出现在同一个下拉里（显示为「xxx（导入）」），选中即可用。");
    p("② 自定义skill → 先点「📂 选择 skill 文件目录」选一个文件夹（云机无桌面时直接在路径框里填绝对路径，回车即扫描），"
        + "再在它下面的「已有 skill」里选该目录中的哪个 .md/.txt。\n"
        + "   · 技能文件留在原位不会复制进插件目录，换目录点「🔄 刷新」即可重新列出。\n"
        + "   · 没选目录时「已有 skill」显示插件自带的 skills/jzl，行为与以前一致。");
    p("LLM 关闭时两个分支都不可操作，自动用「系统默认」模板（把两段描述填进 detailed_description，不需要任何模型）。");
    p("LLM 调用失败会自动回退到同名内置模板（人物→人物模板、动物→动物模板…）并打警告，不会中断生成。");

    h("③ 怎么跑的（进度条为什么是 12 格）");
    p("整片只有一条降噪轨迹：4 个采样步。每一步内部按顺序算 3 个窗口，所以进度条总数 = 步数 × 段数 = 12 格，右侧实时显示「步 k/4｜段 i/3」。");
    p("每个窗口用自己的「源片同名段」+ 定妆图 + 提示词各预测一次，再在重叠区按斜坡权重混合回整片，混合后的 latent 就是下一步的输入。");
    p("窗口之间不传递任何已解码画面 → 不会越往后越不像；模型只加载一次（VAE 一次、文本编码器一次）→ 不会越跑越慢。");
    p("长片（超过 1200 帧）自动分块解码：每块往前多解 34 帧上下文再丢掉，解完立刻写盘并释放，内存不随片长增长。");

    h("④ 调参经验");
    p("脸不像 → 参考图放大 2.0~3.0、百万像素 0.6~1.0、二采面积倍数 1.5~2.0。");
    p("接缝跳一下 → 重叠帧数加到 39。");
    p("某一段不满意 → 调种子或「重叠帧数」后整片重跑（本节点是联合窗口采样，没有「只重跑第几段」）。");
    p("动作对不上 → 采样步数 8~10。");
    p("太慢 / 爆显存 → 每段时长改 8 秒、二采先关掉只跑一采；源片是 1080p 时建议在加载端先缩到 720p 附近。");

    h("⑤ 落盘与命名");
    p("「分段输出」= 文件名前缀，写法「文件夹/视频名称」：填 视频/人物替换 → 落盘到 output/视频/，文件名 人物替换_2次生成分段1_00001.mp4；只写一个词就直接落在 output 根目录。");
    p("同一前缀连续跑会依次变成 分段2、分段3…，批次号也会 +1，不会覆盖上一次结果。");

    h("⑥ 已知取舍");
    p("H3 是整帧重生成：背景/道具会被重画（招牌字、路人、光线可能变）。要「只换人不换景」需要掩码回流后处理（后续版本）。");
    p("换人后口型由模型重新生成，与原台词不保证逐字对位 —— 所以默认「使用原声」只换画面。");
    p("时间随段数线性增长：耗时 ≈ 步数 × 段数 × 单窗单步耗时（画布越小越快）。");
}

// 🎭 人物替换的默认提示词（与后端 nodes_character_swap.DEFAULT_PROMPT 保持一致）
// 模型按 ===H3_PROMPT=== 结构理解提示词；只写一句「把人物换成 <Picture 1>」会被参考视频压过去。
// retention_analysis 用三种保留动词（fully_preserved / attribute_transfer / partially_preserved），
// 这是模型训练时见过的写法，比全部写 fully_preserved 更能区分「换谁」和「保留什么」。
export const SWAP_DEFAULT_PROMPT = [
    "[SHOT_START]",
    "",
    "===H3_PROMPT===",
    "subject_definitions:",
    "<Subject 1> 是 <Picture 1> 中的人物。身份、五官、脸型、发型、发色、肤色、体型、服装、配饰与配色"
        + "全部以 <Picture 1> 为准；<Picture 1> 只是参考图，不作为画面内容出现。",
    "",
    "summary:",
    "[reference generation + audio reference] 目标视频是 <Video 1> 的重制版：把 <Video 1> 里的人换成 "
        + "<Subject 1>，其余一律沿用 <Video 1>。",
    "",
    "retention_analysis:",
    "<Picture 1>: fully_preserved —— 身份与外观（长相、发型、服装、配饰、配色）。",
    "<Video 1>: attribute_transfer —— 动作、表情、口型、走位、时机、机位、景别、景深、镜头运动；",
    "            fully_preserved —— 场景、道具、背景、光线方向、整体色调；",
    "            partially_preserved —— 画面里的人物被 <Subject 1> 取代，位置与遮挡关系不变。",
    "",
    "detailed_description:",
    "<Subject 1> 以 <Video 1> 中人物的同一姿态、同一动作节奏、同一表情完成整段表演，口型开合与停顿"
        + "与 <Video 1> 一致；镜头、景别、景深、运动与 <Video 1> 一致；场景、道具、背景、光线与色彩与 "
        + "<Video 1> 一致。只替换人物本身：脸、发型、服装、配饰换成 <Picture 1> 的，动作与场景不换。",
    "",
    "[Shot 1] 沿用 <Video 1> 的表演与运镜。",
    "",
    "overall_soundscape: 沿用 <Video 1> 的原始环境声。",
    "",
    "non_diegetic_music: N/A",
    "[SHOT_END]",
].join("\n");

// 历史版本的默认提示词：老工作流里存的是这几句，命中就自动升级成结构化版本
export const SWAP_LEGACY_PROMPTS = [
    "把 <Video 1> 里的人物替换为 <Picture 1>。\n严格保持 <Video 1> 的动作、表情、机位、场景、道具、光线与色彩不变。",
    "把 <Video 1> 里的人物替换为 <Picture 1>。\n严格保持 <Video 1> 的动作、表情、机位、场景、道具、光线与色彩不变。\n",
];

export function swapPromptLooksLegacy(v) {
    const s = String(v || "").trim();
    return !!s && SWAP_LEGACY_PROMPTS.some((p) => p.trim() === s);
}

// 看起来像不像「节点配置 JSON」（只判断开头几个键，够用于错位识别）
export function swapLooksLikeConfig(v) {
    return typeof v === "string" && /^\s*\{\s*"(auto_save|gen_params|sample_decode|save|models|assets)"/.test(v);
}

// console 在某些宿主/早期前端里可能不可用；日志失败绝不能中断修复流程
export function swapLog(msg) {
    try { console.log(msg); } catch (_) {}
}

// 看起来像不像「换人提示词正文」（用于把错位到 story_name 的正文搬回来）
export function swapLooksLikePrompt(v) {
    const s = String(v || "");
    return s.length >= 12 && (/<Video\s*\d>/.test(s) || /替换/.test(s) || /保持/.test(s));
}

// 旧版「人物替换」节点的值/端口修复：
//  ① 端口 source_frames → source_video（改名 + 移位后按位置还原会把连线挂到幽灵端口上，这里迁走连线）
//  ② 端口顺序归位（旧工作流按自身 inputs 顺序还原，会和 schema 顺序不一致 → 强制排成标准顺序）
//  ③ widget 值按名纠正：旧版 widgets_values = [prompt, story_name, display_info, manager_settings]，
//     新版 = [story_name, manager_settings, prompt] → 旧工作流载入后整体错位一格
//     （典型症状：提示词框里显示一串 {"auto_save":...} JSON，分段输出变成提示词正文）
export const SWAP_INPUT_ORDER = ["model", "clip", "vae", "audio_vae", "source_video", "source_audio",
    "ref_images", "story_name", "manager_settings", "prompt"];

export function reorderSwapInputs(self) {
    const ins = self.inputs || [];
    if (ins.length < 2) return;
    const rank = (n) => { const i = SWAP_INPUT_ORDER.indexOf(n); return i < 0 ? 999 : i; };
    const order = ins.map((v, i) => ({ v, i }))
        .sort((a, b) => (rank(a.v.name) - rank(b.v.name)) || (a.i - b.i))
        .map((x) => x.v);
    if (order.every((v, i) => v === ins[i])) return;
    ins.length = 0;
    order.forEach((v) => ins.push(v));
    ins.forEach((inp, idx) => {
        inp.slot = idx;
        if (inp.link == null) return;
        const lk = (self.graph && self.graph.links && self.graph.links[inp.link])
            || (self.graph && self.graph._links && self.graph._links.get && self.graph._links.get(inp.link));
        if (lk) lk.target_slot = idx;
    });
    self.setDirtyCanvas?.(true, true);
    swapLog("[JZL-人物替换] 端口顺序已归位为标准顺序");
}

export function healSwapNodeLegacy(self) {
    // ① 幽灵端口 source_frames：把连线迁到 source_video 后删除
    try {
        const ins = self.inputs || [];
        const oldIdx = ins.findIndex((i) => i && i.name === "source_frames");
        if (oldIdx >= 0) {
            const linkId = ins[oldIdx].link;
            ins.splice(oldIdx, 1);                       // 先删，再按新数组算目标槽位（否则下标会错一位）
            ins.forEach((i, k) => { i.slot = k; });
            const newIdx = ins.findIndex((i) => i && i.name === "source_video");
            if (newIdx >= 0 && linkId != null) {
                ins[newIdx].link = linkId;
                const lk = (self.graph && self.graph.links && self.graph.links[linkId])
                    || (self.graph && self.graph._links && self.graph._links.get && self.graph._links.get(linkId));
                if (lk) { lk.target_slot = newIdx; lk.target_id = self.id; }
                swapLog("[JZL-人物替换] 已把旧端口 source_frames 的连线迁移到 source_video");
            }
            self.setDirtyCanvas?.(true, true);
        }
        reorderSwapInputs(self);   // ② 端口顺序归位（旧工作流按自身顺序还原 → 与 schema 不一致）
    } catch (_) {}

    // ③ widget 值错位修复
    try {
        const wOf = (n) => (self.widgets || []).find((w) => w && w.name === n);
        const msW = wOf("manager_settings");
        const prW = wOf("prompt");
        const snW = wOf("story_name");
        if (!msW) return;
        // 配置 JSON 跑到别的 widget 上了 → 搬回 manager_settings
        if (!swapLooksLikeConfig(readWidgetValue(msW))) {
            for (const cand of [prW, snW]) {
                if (!cand) continue;
                const v = readWidgetValue(cand);
                if (swapLooksLikeConfig(v)) {
                    setWidgetValue(msW, v);
                    if (cand === prW) {
                        // prompt 里是 JSON ⇒ 提示词原文丢了（被挤到 story_name），下面第 ④ 步会还原
                        setWidgetValue(prW, "");
                    } else {
                        setWidgetValue(cand, swapDefaultName(self));
                    }
                    swapLog("[JZL-人物替换] 已把错位的节点配置 JSON 移回 manager_settings");
                    break;
                }
            }
        }
        // ④ 提示词正文跑到 story_name 上（旧顺序 [prompt, story_name, ...]）→ 还原
        if (snW) {
            const sv = String(readWidgetValue(snW) || "");
            if (swapLooksLikePrompt(sv) && !swapLooksLikeConfig(sv)) {
                if (prW && !String(readWidgetValue(prW) || "").trim()) setWidgetValue(prW, sv);
                setWidgetValue(snW, swapDefaultName(self));
                swapLog("[JZL-人物替换] 已把错位的提示词正文移回提示词框");
            }
        }
        // ⑤ 提示词仍为空（错位后彻底丢失）→ 填回默认提示词，避免用户面对空白框
        if (prW && !String(readWidgetValue(prW) || "").trim()) {
            setWidgetValue(prW, SWAP_DEFAULT_PROMPT);
        }
        // ⑥ 老版本的两句默认提示词 → 升级成结构化版本（模型只认 ===H3_PROMPT=== 结构，
        //    一句「把人物换成 <Picture 1>」会被参考视频压过去，导致换人不生效）
        if (prW && swapPromptLooksLegacy(readWidgetValue(prW))) {
            setWidgetValue(prW, SWAP_DEFAULT_PROMPT);
            swapLog("[JZL-人物替换] 默认提示词已升级为结构化格式（===H3_PROMPT===）");
        }
        try { self.__jzlSwapSyncPrompt?.(); } catch (_) {}
        try { self.__jzlUpdateDisplay?.(); } catch (_) {}
    } catch (_) {}
}

// ── 🎭 人物替换：节点表面只留「4 个按钮 + 两行方案信息」，其余全部收进面板；
//    提示词固定在最底部（DOM 按钮块插到 prompt widget 之前） ──
export function setupSwapNode(self) {
    ensureManagerStyle();
    // 内部/面板管理的 widget 不占节点表面：manager_settings（配置 JSON）、story_name（已移入 🎬 生成视频管理）
    for (const w of self.widgets || []) {
        if (w && (w.name === "manager_settings" || w.name === "story_name")) {
            w.hidden = true;
            if (!w.options) w.options = {};
            w.options.hidden = true;
            w.computeSize = () => [0, -4];
        }
    }

    const container = document.createElement("div");
    // 整块 DOM 填满节点（与导演台同款）：内部 flex 列布局 —— 按钮/信息固定，提示词区 flex:1 吸收剩余高度。
    // 这样无论节点多高，空白都由**提示词输入框**吃掉，不会出现一块空紫色区域。
    container.style.cssText = "width:100%;height:100%;display:flex;flex-direction:column;gap:6px;"
        + "padding:2px 0;box-sizing:border-box;overflow:hidden;";

    const btnH = 32;
    const grid = document.createElement("div");
    grid.style.cssText = `flex:0 0 auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));`
        + `grid-auto-rows:${btnH}px;gap:6px;`;
    const buttons = [
        { label: "🎨 视频参数设置", panel: "video_params" },
        { label: "🎭 采样解码设置", panel: "preference" },
        { label: "🎬 生成视频管理", panel: "video_manager" },
        { label: "📝 提示词预设", panel: "help" },
    ];
    for (const b of buttons) {
        const btn = document.createElement("button");
        btn.textContent = b.label;
        btn.style.cssText = ["width:100%;", `height:${btnH}px;`, "box-sizing:border-box;",
            "display:flex;", "align-items:center;", "justify-content:center;",
            "border-radius:6px;", "border:2px solid #5b9bd5;", "background:var(--jz-border);",
            "color:var(--jz-text-strong);", "font-size:14px;", "cursor:pointer;", "white-space:nowrap;",
            "overflow:hidden;", "text-overflow:ellipsis;"].join("");
        btn.addEventListener("mousedown", (e) => e.stopPropagation());
        btn.addEventListener("mouseenter", () => { btn.style.background = "#4a4a4a"; });
        btn.addEventListener("mouseleave", () => { btn.style.background = "var(--jz-border)"; });
        btn.addEventListener("click", () => openModal(self, b.panel));
        grid.appendChild(btn);
    }
    container.appendChild(grid);

    // 两行方案信息（只读）：第 1 行＝画布/段长/衔接；第 2 行＝音频/二采/采样/分段输出
    const infoRow = el("div", "flex:0 0 auto;font-size:11px;color:var(--jz-warn);line-height:1.55;white-space:pre-line;"
        + "overflow:hidden;min-height:28px;word-break:break-all;", "");
    infoRow.title = "当前方案（只读）：画布 / 每段帧数 / 分段衔接 / 音频 / 二采 / 采样步数 / 分段输出 —— 在四个按钮面板里修改";
    infoRow.addEventListener("mousedown", (e) => e.stopPropagation());
    container.appendChild(infoRow);
    self.__jzlSwapInfo = infoRow;

    // ── 提示词（两栏 DOM textarea：视频描述 / 图片描述；代理原生 video_desc / image_desc widget）──
    // 与导演台同一套做法：原生 widget 的渲染高度跟着节点乱跳，改成自己的 textarea 才能「跟着节点变高」。
    const descWrap = el("div", "flex:1 1 auto;min-height:90px;display:flex;flex-direction:column;gap:4px;");
    const mkDesc = (label, wname, placeholder, tip) => {
        const lb = el("div", "flex:0 0 auto;font-size:12px;color:var(--jz-text-dim);", label);
        lb.title = tip;
        lb.addEventListener("mousedown", (e) => e.stopPropagation());
        const box = el("div", "flex:1 1 auto;min-height:40px;display:flex;position:relative;");
        box.setAttribute("data-capture-wheel", "true");   // 滚轮归本框（不被画布缩放抢走）
        const ta = document.createElement("textarea");
        ta.className = "jzl-input";
        ta.spellcheck = false;
        ta.placeholder = placeholder;
        ta.style.cssText = "width:100%;height:100%;box-sizing:border-box;resize:none;"
            + "background:var(--jz-bg-raise);color:var(--jz-text);border:1px solid var(--jz-border);border-radius:4px;"
            + "padding:6px 8px;font-size:12px;line-height:1.5;font-family:inherit;overflow-y:auto;white-space:pre-wrap;";
        ta.addEventListener("mousedown", (e) => e.stopPropagation());
        box.appendChild(ta);
        descWrap.append(lb, box);
        return { ta, wname };
    };
    const videoField = mkDesc("🎬 视频描述", "video_desc",
        "这段源视频里发生了什么：人物、动作、场景、镜头、光线、节奏…",
        "「📝 提示词预设 → 提示词来源」选「内置skill / 自定义skill」时，它连同「图片描述」一起交给 LLM 写完整 H3 提示词；选「系统默认」时填进内置模板");
    const imageField = mkDesc("🖼 图片描述", "image_desc",
        "参考图里的人长什么样：脸型、发型、发色、服装、配饰、配色、年龄气质…（越具体越像）",
        "描述得越具体，换人越像");
    container.appendChild(descWrap);

    const descFields = [videoField, imageField];
    const widgetOf = (name) => (self.widgets || []).find((w) => w && w.name === name);
    // 隐藏原生 widget（值仍照常序列化 → 执行端读到的还是它）
    const hideNativeDesc = () => {
        for (const f of descFields) {
            const w = widgetOf(f.wname);
            if (!w) continue;
            w.hidden = true;
            if (!w.options) w.options = {};
            w.options.hidden = true;
            try { delete w.computeSize; } catch (_) { w.computeSize = undefined; }
            w.computeSize = () => [0, -4];
        }
    };
    hideNativeDesc();
    // widget → textarea（工作流载入 / 修复后回填）
    const syncPromptFromWidget = () => {
        for (const f of descFields) {
            const v = String(readWidgetValue(widgetOf(f.wname)) ?? "");
            if (f.ta.value !== v) f.ta.value = v;
        }
    };
    // textarea → widget（用户输入）
    for (const f of descFields) {
        f.ta.addEventListener("input", () => {
            const w = widgetOf(f.wname);
            if (w) setWidgetValue(w, f.ta.value);
            try { self.graph?.setDirtyCanvas?.(true, false); } catch (_) {}
        });
        f.ta.addEventListener("change", () => {
            const w = widgetOf(f.wname);
            if (w) setWidgetValue(w, f.ta.value);
        });
    }
    self.__jzlSwapSyncPrompt = syncPromptFromWidget;
    syncPromptFromWidget();

    const widget = self.addDOMWidget?.("jzl_swap_buttons", "JZL_SWAP_BUTTONS", container, {
        serialize: false,
        hideOnZoom: false,
    });
    if (widget) {
        // 与导演台同款：删掉 computeSize（让它参与「剩余空间分配」而不是固定高度），
        // getHeight 100% → 容器填满节点；内部提示词块 flex:1 → 空白永远是提示词框在长。
        try { delete widget.computeSize; } catch (_) { widget.computeSize = undefined; }
        widget.options = widget.options || {};
        widget.options.serialize = false;
        widget.options.getMinHeight = () => 300;
        widget.options.getHeight = () => "100%";
        widget.computeLayoutSize = () => ({ minHeight: 300, maxHeight: undefined, minWidth: 420 });
        const syncW = () => {
            try {
                const fw = Number.isFinite(self.size?.[0]) ? self.size[0] : 0;
                if (fw && widget.width !== fw) widget.width = fw;
            } catch (_) {}
            try { self._jzlSwapApplyMaxH?.(); } catch (_) {}
        };
        widget.options.onDraw = () => { try { syncW(); } catch (_) {} };
        widget.options.afterResize = () => { try { syncW(); } catch (_) {} };
        // Nodes 2.0：把容器 max-height 绑到 --node-height（否则 Vue 网格会把面板按内容撑开/撑爆）
        try {
            self._jzlSwapApplyMaxH = bindDomWidgetMaxHeight(self, container, { minHeight: 260, definite: true });
        } catch (_) {}
        syncW();
    }

    const readCfg = () => {
        let s = {};
        const msW = (self.widgets || []).find((x) => x.name === "manager_settings");
        if (msW) { try { s = JSON.parse(readWidgetValue(msW) || "{}") || {}; } catch (_) { s = {}; } }
        return s;
    };

    // 两行方案信息：与「🎨/🎭」面板写进 manager_settings 的值实时同步
    const updateDisplay = () => {
        const s = readCfg();
        const gp = s.gen_params || {};
        const sd = s.sample_decode || {};
        const seam = sd.seam || {};
        const sec = sd.second || {};
        const pc = s.prompt_cfg || {};
        const dv = (x) => `<span style="color:var(--jz-ok)">${x}</span>`;
        const sep = " | ";
        const ar = String(gp.aspect_ratio || "跟随源片（推荐）");
        const arTxt = ar.startsWith("跟随源片")
            ? "跟随源片"
            : (ar.replace(/\s*\(.*\)\s*$/, "") || ar);
        const canvasTxt = ar.startsWith("跟随源片")
            ? `画布·${Number(gp.megapixels ?? 0.4).toFixed(2)}MP`
            : (() => { const [W, H] = jzlCalcResolution(ar, Number(gp.megapixels ?? 0.4)); return `${W}×${H}`; })();
        const f = swapFrameLen(gp.segment || "固定5秒");
        const secPerSeg = `${Math.round(f / 24)}秒/每段`;
        // 声音来源：直接显示面板里选的那一项（使用原声 / 生成音频），一眼知道成片有没有声音、是谁的声音
        const audioTxt = String(gp.audio_mode || "使用原声") === "生成音频" ? "生成音频" : "使用原声";
        // LLM 增强由「提示词来源」派生（与后端 use_llm 同规则）；
        // 后面直接跟上来源名，省得再去面板里翻「到底是内置哪一套 skill」。
        const llmSrc = String(pc.source || "系统默认");
        const customName = String(pc.skill || "").trim();
        const btName = String(pc.builtin || "人物替换").replace(/\.[^.]+$/, "");   // 导入的 skill 去掉扩展名更好看
        const llmTxt = llmSrc === "系统默认" ? "LLM关闭 丨 系统默认"
            : llmSrc === "内置skill" ? `LLM开启 丨 内置skill:${btName}`
            : (customName ? `LLM开启 丨 自定义skill:${customName.replace(/\.[^.]+$/, "")}` : "LLM开启 丨 自定义skill");
        // 本节点固定常规采样（联合窗口采样）：旧工作流里存的「SelfLift采样」一律按常规显示
        const secTgt = String(gp.second_mode || "放大倍数") === "目标分辨率";
        const secOn = sec.enabled !== false;
        const secTxt = !secOn ? "二采关闭"
            : (secTgt ? `二采${gp.second_width ?? 1280}×${gp.second_height ?? 704}`
                      : `二采面积×${Number(gp.upscale_scale ?? 2.0).toFixed(2)}`);
        const seedRaw = String(sd.seed ?? "").trim();
        const seedTxt = ((seedRaw === "" || seedRaw === "0") && String(sd.seed_mode || "randomize") === "randomize")
            ? "随机" : (seedRaw || "0");
        infoRow.innerHTML =
            `视频参数: ${dv(arTxt)}${sep}${dv(canvasTxt)}${sep}${dv(secPerSeg)}`
            + `${sep}${dv(`重叠${seam.window ?? 22}帧`)}${sep}${dv(audioTxt)}${sep}${dv(llmTxt)}`
            + `\n采样参数: ${dv(secTxt)}${sep}${dv(sd.scheduler || "simple")}`
            + `${sep}${dv(sd.sampler || OPTIONS.samplers[0])}${sep}${dv(`${sd.steps ?? 4}步`)}${sep}种子:${dv(seedTxt)}`;
    };
    self.__jzlUpdateDisplay = updateDisplay;
    setTimeout(updateDisplay, 0);
    setTimeout(updateDisplay, 300);
    try { if (!Number.isFinite(self.size?.[1]) || self.size[1] < 400) self.setSize?.([560, 520]); } catch (_) {}
}

// ── 后端推送（提示词 / 真实种子）：节点一跑起来就更新面板，无需手动点「验证」──
// 只用一个全局监听器；面板打开时把值写到 node.__jzlLastPrompt / 种子行上。
export function bindSwapUiEvents() {
    const bind = (apiMod) => {
        if (!apiMod || !apiMod.addEventListener || apiMod.__jzlSwapUiBound) return;
        apiMod.__jzlSwapUiBound = true;
        apiMod.addEventListener("jzl_swap_ui", (ev) => {
            const d = (ev && ev.detail) || {};
            const pickOne = (v) => (Array.isArray(v) ? (v.length ? v[v.length - 1] : "") : (v || ""));
            const prompt = String(pickOne(d.jzl_swap_prompt) || "");
            const src = String(pickOne(d.jzl_swap_prompt_src) || "");
            const seed = String(pickOne(d.jzl_swap_seed) || "");
            const nodes = (window.app && window.app.graph && window.app.graph._nodes) || [];
            for (const n of nodes) {
                if (!n || !SWAP_NODE_TYPES.includes(n.type)) continue;
                if (prompt) {
                    n.__jzlLastPrompt = prompt;
                    n.__jzlLastPromptSrc = src;
                    try { n.__jzlLastPromptTime = new Date().toLocaleTimeString(); } catch (_) {}
                    try { n.__jzlApplyPrompt?.(prompt, src); } catch (_) {}
                }
                if (seed) {
                    try { n.__jzlApplySeed?.(seed); } catch (_) {}
                }
            }
        });
    };
    try { bind(api); } catch (_) {}
    // 兜底：某些前端版本在扩展加载后才把 api 挂到 window.comfyAPI
    try { bind(window.comfyAPI && window.comfyAPI.api); } catch (_) {}
}

// 🎭 人物替换 / 视频编辑 的配置结构升级：只做**一次**（用 _schema 打标记）。
// 绝不能每次加载都跑：否则用户手动关掉的二采 / 改回 scheduler，下次打开又被强行改回去，
// 表现为「保存了无效」。版本号已是最新时整段跳过。
// 由 asset_manager 的 migrateLegacyDefaults 通过 registerSettingsHook 的 migrate 钩子调用。
export function migrateSwapSettings(s) {
    if (!s || typeof s !== "object") return false;
    if (Number(s._schema || 0) >= SWAP_SETTINGS_SCHEMA) return false;
    let changed = false;
        const sd = s.sample_decode;
        const seam = sd?.seam;
        if (seam && typeof seam === "object") {
            if (seam.settle !== undefined) { delete seam.settle; changed = true; }
            // 老的「5 帧窗口 + 12 帧过渡」→ 「22 帧重叠」
            if (Number(seam.window) === 5) { seam.window = 22; changed = true; }
            // 老的「引导来源」→ 新的「衔接通道」，一律升级到遮罩前缀（更好）
            if (seam.guide_mode !== undefined) { delete seam.guide_mode; changed = true; }
            if (String(seam.transport || "") !== "prefix") { seam.transport = "prefix"; changed = true; }
            if (!seam.repair) { seam.repair = "auto"; changed = true; }
        }
        const gp = s.gen_params;
        if (gp && typeof gp === "object") {
            // 二采从「固定分辨率」改为「面积倍数 x2」，与参考工作流（0.5MP → 1.0MP）同量级
            if (String(gp.second_mode || "") === "目标分辨率" && Number(gp.upscale_scale) === 1.0) {
                gp.second_mode = "放大倍数";
                gp.upscale_scale = 2.0;
                changed = true;
            }
            for (const dead of ["merge_tail", "ref_inject", "ref_inject_every",
                                "audio_mode", "max_segments", "source_cache_gb"]) {
                if (gp[dead] !== undefined) { delete gp[dead]; changed = true; }
            }
        }
        // 二采的「开关」绝不动：那是用户的明确意图（强行打开 = 关不掉，就是之前那个 bug）。
        // 只升级「参数语义」：老默认 scheduler + denoise 0.3 等于几乎不重绘，换脸会被抹平。
        const sec = sd?.second;
        if (sec && typeof sec === "object") {
            if (String(sec.sigmas_mode || "scheduler") === "scheduler" && Number(sec.denoise) <= 0.35) {
                sec.sigmas_mode = "custom";
                sec.custom_sigmas = "0.909375, 0.725, 0.421875, 0.0000";
                changed = true;
            }
        }
        s._schema = SWAP_SETTINGS_SCHEMA;
        changed = true;
    return changed;
}


// ══════════════════════════════════════════════════════════════════════════════
//  注册：把「专属面板 / 专属配置处理」挂进素材管理器的通用弹窗
// ══════════════════════════════════════════════════════════════════════════════
for (const t of SWAP_NODE_TYPES) {
    registerSwapLikeType(t);                       // 共用函数据此判断「该按这套布局处理」
    registerPanelOverride(t, {                     // 弹窗里按面板 id 接管渲染
        preference: (c, s, d, node) => renderSwapPrefPanel(c, s, d, node),
        video_params: (c, s, node) => renderSwapVideoParamsPanel(c, s, node),
        save_settings: (c, s, node) => renderSwapSavePanel(c, s, node),
        help: (c, s, d, node) => renderSwapPromptPanel(c, s, d, node),
    });
    registerSettingsHook(t, {                      // 该类型自己的默认值 / 保存裁剪 / 一次性迁移
        defaults: swapDefaults,
        prune: pruneSwapSettings,
        migrate: migrateSwapSettings,
    });
}

app.registerExtension({
    name: "JZL.MiniMaxCharacterSwap",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!SWAP_NODE_TYPES.includes(nodeData?.name)) return;

        const _origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = _origCreated?.apply(this, arguments);
            try { setupSwapNode(this); } catch (e) { console.error("[JZL-Swap]", e); }
            // 值/端口修复要在 configure 之后再跑一次（工作流载入时 widgets_values 是异步还原的）
            const self = this;
            [0, 60, 400].forEach((ms) => setTimeout(() => {
                try { healSwapNodeLegacy(self); } catch (_) {}
            }, ms));
            return r;
        };

        const _origCfg = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const r = _origCfg?.apply(this, arguments);
            try { healSwapNodeLegacy(this); } catch (e) { console.error("[JZL-Swap-heal]", e); }
            const self = this;
            setTimeout(() => { try { healSwapNodeLegacy(self); } catch (_) {} }, 200);
            return r;
        };

        // 跑完把本次真实种子写回 manager_settings（与原生 seed 节点一致：面板显示的就是用掉的那个）
        const _origExec = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (msg) {
            const r = _origExec?.apply(this, arguments);
            try {
                const one = (v) => (Array.isArray(v) ? (v.length ? v[v.length - 1] : "") : v);
                const seed = String(one(msg?.jzl_swap_seed) ?? "");
                const msW = (this.widgets || []).find((x) => x.name === "manager_settings");
                if (seed && msW) {
                    const raw = readWidgetValue(msW);
                    let s = {};
                    try { s = raw ? JSON.parse(raw) : {}; } catch (_) {}
                    if (!s.sample_decode) s.sample_decode = {};
                    s.sample_decode.seed = seed;
                    setWidgetValue(msW, JSON.stringify(s));
                    try { this.__jzlUpdateDisplay?.(); } catch (_) {}
                }
            } catch (_) {}
            return r;
        };
    },
});

// 后端实时推送（提示词 / 真实种子）→ 面板
bindSwapUiEvents();
