/**
 * JZL MiniMax — 漫剧短剧管理器（第一版）
 * ============================================================
 * 参照「短剧管理器」网页原型，融合 JZL MiniMax H3 节点真实参数：
 *   ① 生成模型管理（本地 LLM 模型加载）
 *   ② 参考资源管理（图片/视频/音频资产池）
 *   ③ 提示词管理（提示词增强 + LLM 后端）
 *   ④ 生成参数管理（海螺H3视频参数）
 *   ⑤ 生成偏好管理（镜头语言偏好）
 *   ⑥ 视频保存管理（输出目录 + 命名规则）
 * 配置统一持久化到 jzl_manager.json（ComfyUI user 目录）。
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_TYPE = "JZL_MiniMaxAssetManager";
const MINI_NODE_TYPE = "JZL_MiniMaxAssetManagerMini";
const MAX_NODE_TYPE = "JZL_MiniMaxAssetManagerMax";
// ♾️ 无限时长：与 Max 同一套面板/落盘语义，额外多一个「无限时长衔接⚠实验性功能」设置分区
const INF_NODE_TYPE = "JZL_MiniMaxAssetManagerInfinite";
// 🎬 短剧导演台：克隆「无限时长」，按钮改为「2 + 4 + 4」三排（多了「生成设定」「视频参数」两个面板）
const SHORTDRAMA_NODE_TYPE = "JZL_MiniMaxAssetManagerShortDrama";
// 🎭 人物替换：长视频换人（源片逐段 ref2va 重生成）。选项全部收进三个按钮面板：
//    🎨 视频参数设置 / 🎭 采样解码设置 / 🎬 生成视频管理
const SWAP_NODE_TYPE = "JZL_MiniMaxCharacterSwap";
const VIEWER_NODE_TYPE = "JZL_MiniMaxVideoViewer";
const SCRIPT_VIEWER_NODE_TYPE = "JZL_MiniMaxScriptViewer";

// ── 纯文本模式（仅提示词输出 / 故事扩写模式）自动静音 ─────────────────
// 上游：model/clip/vae/audio_vae；下游：除「剧本文本输出(已处理剧本/已拆解剧本)」外的其它输出所连节点。
const JZL_PURE_MUTE_INPUTS = new Set(["model", "clip", "vae", "audio_vae"]);
const JZL_PURE_TEXT_MODES = new Set(["仅提示词输出", "故事扩写模式"]);
const JZL_MODE_ACTIVE = 0;   // 活跃（ComfyUI node.mode）
const JZL_MODE_MUTE = 2;     // 静音
// run_mode 下拉显示带表情前缀（🛠️拆解 / 📚扩写 / 📝仅提示词 / 🪄穿透）；比较前剥表情归一化。
const JZL_RUN_MODE_EMOJIS = ["🛠️", "📚", "📝", "🪄"];
function jzlNormalizeRunMode(v) {
    // 先去掉 VS15/VS16 与零宽连接符：同一表情在不同来源里可能带这些不可见字符
    let s = String(v || "").replace(/[\uFE0E\uFE0F\u200D]/g, "").trim();
    if (s.includes("穿透") || s.includes("直通")) return "穿透生成模式";
    if (s.includes("仅提示词") || s.includes("纯提示词")) return "仅提示词输出";
    if (s.includes("扩写") || s.includes("扩展")) return "故事扩写模式";
    if (s.includes("拆解")) return "故事拆解模式";
    for (const e of JZL_RUN_MODE_EMOJIS) { if (s.startsWith(e)) { s = s.slice(e.length).trim(); break; } }
    return s;
}

function jzlCollectMuteUpstream(node) {
    const graph = node.graph || (window.app && window.app.graph);
    const list = [];
    if (!graph) return list;
    const seen = new Set();
    for (const inp of node.inputs || []) {
        if (!inp || !JZL_PURE_MUTE_INPUTS.has(inp.name)) continue;
        let cur = node;
        let slot = (node.inputs || []).indexOf(inp);
        for (let k = 0; k < 20; k++) {
            const ci = (cur.inputs || [])[slot];
            const link = (ci && ci.link != null)
                ? ((graph.links && graph.links[ci.link]) || (graph._links && graph._links.get && graph._links.get(ci.link)))
                : null;
            if (!link) break;
            const src = graph.getNodeById ? graph.getNodeById(link.origin_id) : null;
            if (!src) break;
            if (src.type && String(src.type).includes("Reroute") && !seen.has(src.id)) {
                seen.add(src.id); cur = src; slot = 0; continue;
            }
            if (!seen.has(src.id)) { seen.add(src.id); list.push(src); }
            break;
        }
    }
    return list;
}

// 收集本节点除「剧本文本输出端口(STRING/script: 已处理剧本/已拆解剧本)」外的下游节点
// （用于纯文本模式静音）。剧本文本端口是要保活输出文本的链路，其下游永不 mute。
// 识别依据：该端口 type 为 STRING 且 name/display_name 含 "script"；三节点的剧本文本端口均如此，
// 而图像/音频/模型/条件/Latent/VAE/放大参数等输出 type 均非 STRING。
function jzlCollectMuteDownstream(node) {
    const graph = node.graph || (window.app && window.app.graph);
    const list = [];
    if (!graph || !node) return list;
    const seen = new Set();
    const isScriptOut = (out) => {
        if (!out) return false;
        const nm = String(out.name || "") + "|" + String(out.display_name || "") + "|" + String(out.label || "");
        const ty = String(out.type || "");
        return (ty.toUpperCase().indexOf("STRING") >= 0) && (nm.indexOf("script") >= 0 || nm.indexOf("剧本") >= 0 || nm.indexOf("已处理") >= 0 || nm.indexOf("已拆解") >= 0);
    };
    const addTarget = (targetId) => {
        if (targetId == null) return;
        let cur = graph.getNodeById ? graph.getNodeById(targetId) : null;
        for (let k = 0; k < 20 && cur; k++) {
            if (cur.type && String(cur.type).includes("Reroute")) {
                if (seen.has(cur.id)) return;
                seen.add(cur.id);
                // Reroute 只有一个输出，继续沿下游
                const rout = (cur.outputs || [])[0];
                const rl = rout && rout.link != null
                    ? ((graph.links && graph.links[rout.link]) || (graph._links && graph._links.get && graph._links.get(rout.link)))
                    : null;
                cur = rl ? (graph.getNodeById ? graph.getNodeById(rl.target_id) : null) : null;
                continue;
            }
            if (!seen.has(cur.id)) { seen.add(cur.id); list.push(cur); }
            return;
        }
    };
    for (const out of node.outputs || []) {
        if (isScriptOut(out)) continue;      // 剧本文本链路保活（永不 mute）
        const links = (out.links != null)
            ? (Array.isArray(out.links) ? out.links : [out.links])
            : [];
        for (const lid of links) {
            const link = (graph.links && graph.links[lid]) || (graph._links && graph._links.get && graph._links.get(lid));
            if (link) addTarget(link.target_id);
        }
    }
    return list;
}

// 无状态同步：每次 run_mode 变化都把所有上游(model/clip/vae/audio_vae) + 非 script 下游
// 按当前模式全量重设——纯文本模式（仅提示词输出/故事扩写模式）→ 全部静音(mode=2)；
// 其它模式 → 全部恢复活跃(mode=0)。即时生效、无记录状态残留（幂等）。
function jzlSetPurePromptMute(node, pure) {
    const graph = node.graph || (window.app && window.app.graph);
    if (!graph || !node) return;
    const want = pure ? JZL_MODE_MUTE : JZL_MODE_ACTIVE;
    let changed = false;
    for (const n of jzlCollectMuteUpstream(node)) {
        if (n && n.mode !== want) { try { n.mode = want; } catch (_) {} changed = true; }
    }
    for (const n of jzlCollectMuteDownstream(node)) {
        if (n && n.mode !== want) { try { n.mode = want; } catch (_) {} changed = true; }
    }
    if (changed) graph.setDirtyCanvas?.(true, true);
}

const MANAGER_ENDPOINT = "/jzl/manager";
const RESHOOT_ENDPOINT = "/jzl/reshoot/load";
const UPLOAD_ENDPOINT = "/jzl/upload_asset";
const ASSET_PREVIEW_ENDPOINT = "/jzl/asset_preview";
const AUDIO_PREVIEW_ENDPOINT = "/jzl/audio_preview";

// ── 下拉选项（与后端节点保持一致的选项列表） ─────────────────────
const OPTIONS = {
    llmBackend: ["本地模型 [local]", "在线API [api]"],
    providers: [
        "OpenAI 兼容 (OpenAI/DeepSeek/Qwen/GLM/Kimi/Ollama/vLLM/LM Studio)",
        "Anthropic",
        "Google Gemini",
    ],
    shotSize: ["根据剧情", "随机组合", "远景为主", "全景为主", "中景为主", "近景为主", "特写为主", "大特写为主"],
    // 🔭 视角机位（视角/角度走自然语言注入；官方仅对「运镜」给了封闭词表）
    angleView: [
        "根据剧情", "随机组合",
        "视线平视", "高角度俯拍", "鸟瞰顶视", "低角度仰拍", "虫眼极低", "航拍视角",
        "第一人称POV", "过肩镜头", "第三人称跟随", "偷窥视角", "监控摄像头", "手机自拍",
        "正反打", "侧面视角", "背身视角", "四分之三侧", "镜像反射",
        "斜角荷兰角", "希区柯克变焦", "鱼眼广角", "框中框构图", "长焦压缩视角",
    ],
    cameraMove: ["根据剧情", "随机组合", "固定机位", "推拉", "摇移", "俯仰", "升降", "环绕", "跟拍", "手持晃动", "旋转", "一镜到底"],
    cutRhythm: ["根据剧情", "随机组合", "一镜到底", "2~5镜", "5~9镜", "9~13镜", "13~18镜"],
    transition: ["随机", "硬切", "叠化", "淡入淡出", "擦除"],
    music: [
        "禁止音乐 / No Music", "不指定 / Unspecified",
        "史诗战争 / Epic Orchestral", "动作追逐 / Action Chase",
        "紧张悬疑 / Tense Suspense", "恐怖惊悚 / Horror Atmosphere",
        "温馨治愈 / Warm & Gentle", "浪漫爱情 / Romantic Strings",
        "悲伤抒情 / Melancholic", "轻松喜剧 / Light Comedy",
        "古风武侠 / Chinese Wuxia", "科幻未来 / Sci-fi Electronic",
        "神秘探索 / Mysterious Adventure", "史诗悲剧 / Tragic Epic",
    ],
    creativeReq: ["无特别要求", "节奏紧凑", "舒缓留白", "情感细腻", "明快轻松", "多反转结局", "开放式结局", "强冲突"],
    detailLength: ["标准 (350-500字)", "精简 (200-350字)", "详细 (500-800字)", "超详细 (800-1200字)"],
    style: [
        "不指定 / Unspecified", "电影感 / Cinematic", "实拍 / Live-action",
        "复古胶片 / Vintage film", "黑白电影 / Black & White", "纪录片 / Documentary",
        "极简广告 / Minimalist commercial", "微距摄影 / Macro photography", "航拍 / Aerial drone",
        "二维动画 / 2D-animated", "三维CG / 3D CG", "日系二次元 / Anime",
        "美式漫画 / American Comic", "皮克斯3D / Pixar-style 3D", "定格动画 / Stop-motion",
        "手绘发光 / Hand-drawn glow", "像素艺术 / Pixel art", "赛博朋克 / Cyberpunk",
        "蒸汽朋克 / Steampunk", "故障艺术 / Glitch art", "羊毛毡 / Wool felt",
        "折纸 / Origami", "水彩 / Watercolor", "粘土动画 / Claymation",
        "水墨 / Ink wash", "油画 / Oil painting", "纸艺拼贴 / Paper collage",
        "剪纸 / Paper cutout", "铅笔素描 / Pencil sketch", "浮世绘 / Ukiyo-e",
        "敦煌壁画 / Dunhuang Murals", "青花瓷 / Blue-white Porcelain", "工笔画 / Gongbi Painting",
        "皮影戏 / Shadow Puppetry", "中国风插画 / Chinese Illustration", "年画 / New Year Painting",
        "布艺 / Fabric Art", "蜡笔画 / Crayon drawing", "哥特萝莉 / Gothic Lolita",
    ],
    aspectRatio: [
        "1:1 (Square)", "2:3 (Portrait Photo)", "3:2 (Photo)", "3:4 (Portrait Standard)",
        "4:5 (Portrait Tall)", "4:3 (Standard)", "5:4 (Landscape Tall)",
        "9:16 (Portrait Widescreen)", "16:9 (Widescreen)", "21:9 (Ultrawide)",
    ],
    assetTypes: ["角色", "场景", "道具", "分镜", "音效", "音乐", "其他"],
    presetModes: [
        "纯文本生成音视频[英文]-T2VA [EN]", "纯文本生成音视频[中文]-T2VA [ZH]",
        "首帧图生成音视频[英文]-I2VA [EN]", "首帧图生成音视频[中文]-I2VA [ZH]",
        "首尾帧生成音视频[英文]-FL2VA [EN]", "首尾帧生成音视频[中文]-FL2VA [ZH]",
        "尾帧图生成音视频[英文]-L2VA [EN]", "尾帧图生成音视频[中文]-L2VA [ZH]",
    ],
    aspectShort: ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9", "4:5", "5:4"],
    cuts: ["不指定 / Unspecified", "不切镜 / Single Shot", "1 次切镜 / 1 Cut", "2 次切镜 / 2 Cuts", "3 次切镜 / 3 Cuts", "4 次切镜 / 4 Cuts", "5 次切镜 / 5 Cuts", "6 次切镜 / 6 Cuts", "7 次切镜 / 7 Cuts", "8 次切镜 / 8 Cuts", "9 次切镜 / 9 Cuts"],
    inferenceModes: ["one by one", "images", "video"],
    samplers: ["res_multistep", "res_multistep_cfg_pp", "res_multistep_ancestral", "res_multistep_ancestral_cfg_pp",
        "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp",
        "heun", "heunpp2", "exp_heun_2_x0", "exp_heun_2_x0_sde",
        "dpm_2", "dpm_2_ancestral", "lms", "dpm_fast", "dpm_adaptive",
        "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp", "dpmpp_sde", "dpmpp_sde_gpu",
        "dpmpp_2m", "dpmpp_2m_cfg_pp", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu",
        "dpmpp_2m_sde_heun", "dpmpp_2m_sde_heun_gpu", "dpmpp_3m_sde", "dpmpp_3m_sde_gpu",
        "ddpm", "lcm", "ipndm", "ipndm_v", "deis",
        "gradient_estimation", "gradient_estimation_cfg_pp", "er_sde", "seeds_2", "seeds_3",
        "sa_solver", "sa_solver_pece", "ddim", "uni_pc", "uni_pc_bh2"],
    schedulers: ["simple", "sgm_uniform", "karras", "exponential", "ddim_uniform", "beta", "normal", "linear_quadratic", "kl_optimal"],
    seedModes: ["randomize", "fixed", "increment"],
    decodeCleanup: ["卸载显存模型", "不做清理"],
};

const PANELS = {
    assets: { label: "📁 参考素材管理" },
    prompt: { label: "🤖 大语言模型配置" },
    preference: { label: "🎭 采样解码设置" },
    preference_settings: { label: "🎯 镜头参数预设" },
    prompt_elements: { label: "➕ 常用提示词元素" },
    align: { label: "🎯 参考元素切换" },
    save_settings: { label: "💾 视频保存设置" },
    help: { label: "📖 节点使用说明" },
    video_manager: { label: "🎬 生成视频管理" },
    gen_settings: { label: "🎛️ 运行参数设置" },
    video_params: { label: "🎨 视频参数设置" },
};

// 节点表面按钮定义（前端 addWidget 添加）
const PANEL_BUTTONS = [
    { widget: "btn_assets", label: "📁 参考素材管理", panel: "assets" },
    { widget: "btn_prompt", label: "🤖 大语言模型配置", panel: "prompt" },
    { widget: "btn_pref", label: "🎭 采样解码设置", panel: "preference" },
    { widget: "btn_save", label: "💾 视频保存设置", panel: "save_settings" },
    { widget: "btn_elements", label: "➕ 常用提示词元素", panel: "prompt_elements" },
    { widget: "btn_preference", label: "🎯 镜头参数预设", panel: "preference_settings" },
    { widget: "btn_align", label: "🎯 参考元素切换", panel: "align" },
    { widget: "btn_help", label: "📖 节点使用说明", panel: "help" },
];

const KIND_LABEL = { image: "图片", video: "视频", audio: "音频" };
// 每种资产类型不同的分类选项
const ASSET_TYPES_BY_KIND = {
    image: ["角色", "场景", "道具", "自定义"],
    video: ["主体", "运镜", "特效", "其他"],
    audio: ["角色音色", "背景配乐", "念白", "音效", "其他"],
};
const ASSET_TYPES = ASSET_TYPES_BY_KIND.image;
// 资产类型 → 调度槽位类型（与后端 _asset_type_for_slot 一致；「自定义」→「其他」官方识别）
function slotTypeOf(kind, type) {
    if (kind === "image") return { "角色": "角色", "场景": "场景", "道具": "道具", "自定义": "其他", "分镜": "分镜", "其他": "其他" }[type] || "其他";
    if (kind === "video") return "视频";
    // 音频子分类各自独立编号空间 → 各自独立前缀（与后端 _AUDIO_SLOT_PREFIX 一致）
    return { "角色音色": "音色", "背景配乐": "配乐", "念白": "念白" }[type] || "音频";
}

let modal = null;

// ── V3 widget 值读写 ──────────────────────────────────────
// V3 combo widget 的 callback 是空函数，直接赋值 value 才会同步 _state.value（序列化/执行都读它）
function readWidgetValue(w) {
    return w ? (w._state?.value ?? w.value) : undefined;
}
function setWidgetValue(w, val) {
    if (!w) return;
    w.value = val;
    if (w._state) w._state.value = val;
    w._node?.setDirtyCanvas?.(true, true);
}

// ── 「生成视频数量」双形态读写 ────────────────────────────────
// 该参数有两套 schema：下拉（Pro/Max/无限时长/Mini，值形如「剧情决定」/「生成1段」）
// 与数值（短剧导演台，0 = 剧情决定、1~60 = 固定段数）。跨形态写值会被 ComfyUI 校验拒绝
// （如把「生成1段」写进 INT → Failed to convert an input value to a INT value），
// 故统一经这两个助手读写：按 widget 形态自动选「数字」或「生成N段」。
// ⚠️ 判形态必须**以 widget.type 为准**：不能靠「当前值是不是数字」反推 ——
// 下拉 widget 恰好被写坏了数字时，那种反推会误判成数值型而放弃自愈。
function videoCountIsNumeric(w) {
    if (!w) return false;
    if (w.type === "number") return true;
    if (w.type === "combo") return false;
    // 退化情形（拿不到 type）：看选项列表里有没有「生成N段」这类下拉项
    const opts = Array.isArray(w.options) ? w.options : (w.options?.values || []);
    return !opts.some((o) => /生成\d+段|剧情决定/.test(String(o)));
}

// 下拉型段数选项 → [{n, text}]；用于把任意段数吸附到磁盘/节点实际允许的选项
function videoCountComboOptions(w) {
    const opts = Array.isArray(w?.options) ? w.options : (w?.options?.values || []);
    const out = [];
    for (const o of opts) {
        const s = String(o);
        const m = s.match(/生成(\d+)段/);
        if (m) out.push({ n: Number(m[1]), text: s });
    }
    return out;
}

// 提示词里 [SHOT_START]…[SHOT_END] 分段块数（穿透生成的段数对齐用，与后端 _pt_seg_count 同规则）。
function countShotBlocks(text) {
    const m = String(text ?? "").match(/\[SHOT_START\][\s\S]*?\[SHOT_END\]/g);
    return m ? m.length : 0;
}

// ── Nodes 2.0（设置 →「Comfy.VueNodes.Enabled」）下 DOM widget 的尺寸与滚轮 ─────────
// 前端源码事实（comfyui-frontend 1.55.x）：
//   · 每个 widget 排进 CSS grid 的一行；DOM widget 的 hasLayoutSize 恒为 true
//     （deriveWidgetRenderState: typeof computeLayoutSize === 'function'，而 DOMWidgetImpl 原型就有），
//     于是行高 = `auto` = 内容的 max-content ⇒ 内容一长（长剧本 / 视频宫格）节点就被撑到最大。
//   · 我们的容器 `height:100%` + 内部 `flex:1; min-height:0` 因祖先全链 `min-height:auto`
//     拿不到确定高度 ⇒ 内部 `overflow:auto` 永远 clientHeight == scrollHeight ⇒ 没有滚动条。
//   · 滚轮：前端 `wheelCapturedByFocusedElement(e) = e.target.closest('[data-capture-wheel="true"]')
//     且 document.activeElement 在该元素内` —— 不满足就把滚轮forward给画布（= 缩放画布）。
//
// 修法（两条都不依赖 JS 反推节点高度，避免「只能变大不能变小」的自锁）：
//   ① 高度：把容器 max-height 绑到前端自带的 CSS 变量 `--node-height`（= 节点高度 + 固定余量），
//      减去一次性标定出的 chrome（标题栏 + 原生输入行 + 内边距）。纯 CSS 跟随节点尺寸，
//      拖动缩小时上限同步变小 ⇒ 可以自由拉伸，且内部滚动条稳定存在。
//   ② 滚轮：给滚动容器打 `data-capture-wheel="true"` + `tabindex="-1"`，并在指针进入/按下时
//      聚焦它 ⇒ 前端不再把滚轮转给画布，浏览器原生滚动生效（与官方 Textarea 同一套机制）。
const JZL_WHEEL_PROBE = 3000;   // 标定 chrome 时把容器临时撑到这个高度

// 所有绑定者的 apply 回调（供 MutationObserver 同步触发）。
// ⚠️ 为什么需要它：用户切换「Nodes 2.0」时前端会**整块卸载/重建** `.lg-node`。
//   · 切到 2.0：max-height 还没写上 → 内容（长剧本 / 视频宫格）把卡片撑到内容高度 → 下一拍才被压回
//   · 切回经典：calc 里的 `--node-height` 消失 → 回落到兜底值 → 卡片先变长再还原
//   两者都表现为「节点像弹簧一样变长一下再缩回去」。MutationObserver 的回调在**绘制前**执行，
//   在它里面同步重算 ⇒ 用户一帧都看不到中间态。
const JZL_DOM_FIT_APPLIERS = new Set();
let JZL_DOM_FIT_OBSERVER = null;
function watchDomFit(apply) {
    JZL_DOM_FIT_APPLIERS.add(apply);
    if (JZL_DOM_FIT_OBSERVER) return;
    try {
        JZL_DOM_FIT_OBSERVER = new MutationObserver(() => {
            for (const fn of JZL_DOM_FIT_APPLIERS) { try { fn(); } catch (_) {} }
        });
        // 只盯结构变化（节点 DOM 的增删）；属性变化太吵，用 500ms 轮询兜住内容尺寸变化
        JZL_DOM_FIT_OBSERVER.observe(document.body, { childList: true, subtree: true });
    } catch (_) { JZL_DOM_FIT_OBSERVER = null; }
}

function bindDomWidgetMaxHeight(node, el, opts = {}) {
    if (!node || !el) return () => {};
    const minH = Number(opts.minHeight) || 0;
    const cache = { K: 0 };
    const rootEl = () => (el.closest ? el.closest(".lg-node") : null);
    // 首次绑定时的行内原样（切回经典模式必须原样还原）：
    //   ⚠️ 不还原的话，`calc(var(--node-height, 3000px) - K)` 在经典模式下取 fallback 3000px
    //   （经典渲染器不写 --node-height）→ 面板被撑到 ~2850px → 用户看到「关掉 Nodes 2.0 节点被拉很长」。
    const orig = { max: el.style.maxHeight, min: el.style.minHeight, flex: el.style.flex, h: el.style.height };
    let applied = false;
    const restore = () => {
        if (!applied) return;
        applied = false;
        try {
            if (el.style.maxHeight !== orig.max) el.style.maxHeight = orig.max;
            if (el.style.minHeight !== orig.min) el.style.minHeight = orig.min;
            if (el.style.flex !== orig.flex) el.style.flex = orig.flex;
            if (el.style.height !== orig.h) el.style.height = orig.h;
        } catch (_) {}
    };

    // chrome = 节点根部高度 − 容器高度（与容器内容无关）。用固定 3000px 撑开容器测，
    // 任何内容状态下都成立（早期版本用「空内容 / 4000px 两点拟合」，空内容时量不出来 → 静默失败）。
    const calibrate = () => {
        const root = rootEl();
        if (!root) return null;                                // 经典渲染器没有 .lg-node → 不接管
        const keep = { max: el.style.maxHeight, min: el.style.minHeight, flex: el.style.flex, h: el.style.height };
        try {
            el.style.maxHeight = "";
            el.style.minHeight = "0px";
            el.style.flex = "0 0 auto";
            el.style.height = JZL_WHEEL_PROBE + "px";
            const rh = root.clientHeight, eh = el.clientHeight;
            if (!eh || rh <= eh) return false;                 // 量不到 → 稍后重试
            cache.K = Math.max(40, Math.round(rh - eh));
            return true;
        } finally {
            el.style.maxHeight = keep.max;
            el.style.minHeight = keep.min;
            el.style.flex = keep.flex;
            el.style.height = keep.h;
        }
    };

    let tries = 0;
    const apply = () => {
        try {
            const root = rootEl();
            if (!root) { restore(); return; }              // 经典模式：还原原生布局（含撤销已写入的 calc）
            // `--node-height` 是前端在 2.0 下写在 .lg-node 上的（= 节点高度 + 30）。
            // 拿不到它就别写 calc：否则 calc 会落到兜底值（3000px），出现「先变长再缩回」的弹簧效果。
            let vh = "";
            try { vh = getComputedStyle(root).getPropertyValue("--node-height").trim(); } catch (_) {}
            if (!vh) return;
            if (!cache.K) {
                const ok = calibrate();
                if (ok === null) { restore(); return; }
                if (!ok) {
                    if (++tries <= 40) setTimeout(apply, 150);  // DOM 挂载/工作流加载时序
                    return;
                }
            }
            const h = `calc(var(--node-height) - ${cache.K}px)`;
            applied = true;
            if (el.style.maxHeight !== h) el.style.maxHeight = h;
            // definite：同时给 height。不加这一条时容器高度由内容决定（max-height 只是裁剪），
            // 面板内部 flex:1 的提示词框会按「内容高度」分配剩余空间 → 把资产窗/重拍条挤出可视区，
            // 表现为面板右侧冒出滚动条（用户报障）。给了确定高度后 flex 才会真正压缩提示词框。
            if (opts.definite && el.style.height !== h) el.style.height = h;
            if (minH > 0 && el.style.minHeight !== minH + "px") el.style.minHeight = minH + "px";
        } catch (_) {}
    };
    try { requestAnimationFrame(() => { apply(); setTimeout(apply, 300); setTimeout(apply, 1200); }); } catch (_) { apply(); }
    // ① 结构变化（切 2.0 时 .lg-node 重建）→ MutationObserver 在绘制前同步重算，消除弹簧闪动
    watchDomFit(apply);
    // ② 内容/尺寸变化 → 500ms 兜底轮询；节点被删掉后自动停表，避免残留计时器
    let watch = null;
    try {
        watch = setInterval(() => {
            try {
                if (!el.isConnected && !node.graph) { clearInterval(watch); watch = null; JZL_DOM_FIT_APPLIERS.delete(apply); return; }
            } catch (_) { /* ignore */ }
            apply();
        }, 500);
    } catch (_) {}
    return apply;
}

// 让某个滚动容器接管滚轮（Nodes 2.0 下否则会缩放整个画布）
function enableWheelScroll(box) {
    if (!box || box.__jzlWheel) return box;
    box.__jzlWheel = true;
    try {
        box.setAttribute("data-capture-wheel", "true");
        if (!box.hasAttribute("tabindex")) box.setAttribute("tabindex", "-1");
        box.style.outline = "none";
        const focus = () => {
            if (!box.closest || !box.closest(".lg-node")) return;   // 经典渲染器：不抢焦点
            const ae = document.activeElement;
            // 只抢「没有人正在用」的情况：焦点在 body / 本容器，避免打断提示词输入
            if (ae && ae !== document.body && ae !== box && box.contains(ae)) return;
            if (ae && ae !== document.body && !box.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) return;
            try { box.focus({ preventScroll: true }); } catch (_) { try { box.focus(); } catch (__) {} }
        };
        box.addEventListener("pointerenter", focus, true);
        box.addEventListener("pointerdown", focus, true);
    } catch (_) {}
    return box;
}
// 返回段数：0 = 剧情决定/自动；n = 固定 n 段。两套写法（数字 / 「生成N段」）都能解析。
function readVideoCount(w) {
    const raw = String(readWidgetValue(w) ?? "").trim();
    if (!raw || /剧情决定|自动/.test(raw)) return 0;
    const m = raw.match(/\d+/);
    return m ? (Number(m[0]) || 0) : 0;
}

// 按 widget 形态写入段数：数值型写 Number（并按 min/max 夹取），下拉型写合法选项文本
// （0 → 「剧情决定」；非 0 → 吸附到最近的「生成N段」选项，避免写入不在选项内的值被校验拒绝）。
function setVideoCount(w, n) {
    if (!w) return n;
    let num = Math.max(0, Math.round(Number(n) || 0));
    if (videoCountIsNumeric(w)) {
        const lo = Number.isFinite(Number(w.options?.min)) ? Number(w.options.min) : 0;
        const hi = Number.isFinite(Number(w.options?.max)) ? Number(w.options.max) : 60;
        num = Math.min(hi, Math.max(lo, num));
        setWidgetValue(w, num);
        return num;
    }
    const cand = videoCountComboOptions(w);
    if (num <= 0 || !cand.length) {
        setWidgetValue(w, "剧情决定");
        return "剧情决定";
    }
    let best = cand[0];
    for (const c of cand) {
        if (Math.abs(c.n - num) < Math.abs(best.n - num)) best = c;
    }
    setWidgetValue(w, best.text);
    return best.text;
}

// 自愈：旧版工作流可能把下拉用的字符串存进了数值 widget（如 INT 里存「生成1段」）→
// ComfyUI 校验直接拒绝整个 prompt（重拍/穿透都会失败）。加载与提交前各归一化一次。
function healVideoCountWidget(node) {
    const w = (node?.widgets || []).find((x) => x.name === "video_count");
    if (!w) return false;
    const raw = readWidgetValue(w);
    if (videoCountIsNumeric(w)) {
        // 数值型：非数字（旧工作流存了下拉文本）→ 解析成数字；越界 → 夹取到 min/max。
        // 两者都会被 ComfyUI 的 INT 校验拒绝（Failed to convert… / 超出范围），故一并归一化。
        if (typeof raw === "number" && Number.isFinite(raw)) {
            const lo = Number.isFinite(Number(w.options?.min)) ? Number(w.options.min) : 0;
            const hi = Number.isFinite(Number(w.options?.max)) ? Number(w.options.max) : 60;
            if (raw >= lo && raw <= hi) return false;
            setVideoCount(w, raw);
            return true;
        }
        setVideoCount(w, readVideoCount(w));
        return true;
    }
    // 下拉型：值必须是选项之一，否则吸附回合法选项（数字或非法文本都算）
    const opts = Array.isArray(w.options) ? w.options : (w.options?.values || []);
    if (typeof raw === "string" && opts.includes(raw)) return false;
    if (!opts.length) return false;
    setVideoCount(w, readVideoCount(w));
    return true;
}

// ── @ 引用素材（按节点独立缓存） ─────────────────────────────
let mentionMenu = null;
let mentionState = null;  // { editable, start, end }
let promptCaretPos = null;  // 记忆的光标偏移（null=末尾），供资产显示窗点击插入定位
function notifyAssetsChanged(node) {
    if (!node) return;
    loadManager(node).then((data) => {
        cacheAssets(node, data.settings);
        try { node.__jzlRefresh?.(); } catch (_) {}
    }).catch(() => {});
}

const KIND_PREFIX = { image: "图片", video: "视频", audio: "音频" };
const KIND_ICON = { image: "🖼️", video: "🎬", audio: "🎧" };

// @资产 富文本着色：图片按类型（角色/场景/道具/自定义）分色，视频/音频各一色
const ASSET_COLORS = {
    image: { "角色": "#4da3ff", "场景": "#5ecf8a", "道具": "#ffb84d", "自定义": "#ffd166", "分镜": "#ffd166", "其他": "#9aa7b8" },
    video: "#c792ea",
    audio: "#ff8fa3",
};
function assetColor(item) {
    if (item.kind === "image") return ASSET_COLORS.image[item.type] || ASSET_COLORS.image["其他"];
    return ASSET_COLORS[item.kind] || "#9aa7b8";
}

function rememberCaret(editable) {
    // 把编辑器的实时光标偏移记到自身 __lastCaret（每个编辑器独立记忆，避免全局/闭包记忆过期）。
    // 点击素材卡片会 blur 编辑器导致 selection 丢失，因此在 mousedown（blur 前）调用本函数捕获。
    if (!editable) return;
    // 普通 <textarea>（重拍编辑框）：直接用 selectionStart
    if (String(editable.tagName || "").toUpperCase() === "TEXTAREA") {
        try {
            if (typeof editable.selectionStart === "number") editable.__lastCaret = editable.selectionStart;
        } catch (_) {}
        return;
    }
    const sel = window.getSelection();
    if (sel && sel.rangeCount && editable.contains(sel.anchorNode)) {
        editable.__lastCaret = caretOffset(editable);
    }
}

// 在普通 <textarea> 的光标处插入素材名（保留滚动位置，并触发 input 让上层同步保存）
function insertAssetIntoTextarea(textarea, item, caretRef) {
    if (!textarea) return;
    const name = (item && (item.matchName || item.display || item.token || "") || "").trim();
    if (!name) return;
    const txt = String(textarea.value || "");
    let off = null;
    try {
        if (typeof textarea.selectionStart === "number") off = textarea.selectionStart;
    } catch (_) {}
    if (typeof off !== "number" && typeof textarea.__lastCaret === "number") off = textarea.__lastCaret;
    if (typeof off !== "number" && caretRef && typeof caretRef.value === "number") off = caretRef.value;
    if (typeof off !== "number") off = txt.length;
    off = Math.max(0, Math.min(txt.length, Math.floor(off)));
    const scroll = textarea.scrollTop;
    textarea.value = txt.slice(0, off) + name + txt.slice(off);
    const pos = off + name.length;
    try { textarea.setSelectionRange(pos, pos); } catch (_) {}
    textarea.__lastCaret = pos;
    if (caretRef) caretRef.value = pos;
    try { textarea.scrollTop = scroll; } catch (_) {}
    textarea.focus();
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

// contenteditable 光标辅助
function caretOffset(editable) {
    // 与 getPromptText 一致：token 按 dataset.jzlName（资产名）计长，
    // 避免「显示简称/底层全名」两种视图不一致导致 @ 检测定位错误
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount || !editable.contains(sel.anchorNode)) return getPromptText(editable).length;
    const range = sel.getRangeAt(0);
    const pre = document.createRange();
    pre.selectNodeContents(editable);
    pre.setEnd(range.endContainer, range.endOffset);
    const tmp = document.createElement("div");
    tmp.appendChild(pre.cloneContents());
    let base = getPromptText(tmp).length;
    // 关键修正：光标位于某个「未闭合」的块级元素（DIV/P）内部时，
    // getPromptText 会给该元素末尾多加 1 个换行（\n 在元素之后）。
    // 但光标在元素内部时该换行尚未出现 → 需减去 1，才能与 setCaretToOffset 的
    // 遍历定位完全对称（否则点击资产窗插入会整体后移 1 字符 → 「素材换行错位」）。
    let node = range.endContainer;
    let subtract = 0;
    while (node && node !== editable) {
        if (node.nodeType === 1 && (node.tagName === "DIV" || node.tagName === "P")) {
            // 空行 <div><br></div>（光标在 br 后）→ 内容已完整（br 已代表该行换行），不减
            const onlyBr = node.childNodes.length === 1 && node.firstChild && node.firstChild.nodeType === 1 && node.firstChild.tagName === "BR";
            if (!onlyBr) subtract = 1;
            break;
        }
        node = node.parentNode;
    }
    return base - subtract;
}
function getPromptText(editable) {
    // 精确纯文本：普通文本拼接；资产 token 输出其全名(dataset.jzlAsset)；<br>/块级转 \n
    let out = "";
    const visit = (node) => {
        for (const child of node.childNodes) {
            if (child.nodeType === 3) {
                out += child.textContent;
            } else if (child.nodeType === 1) {
                if (child.classList?.contains("jzl-asset-token")) {
                    out += child.dataset.jzlName || child.dataset.jzlAsset || "";
                } else if (child.tagName === "BR") {
                    out += "\n";
                } else if (child.tagName === "DIV" || child.tagName === "P") {
                    // 块级元素行尾若为占位 <br>（Chrome 的行尾占位，不产生可见空行），不追加 div 换行：
                    //  - <div><br></div>（空行）→ 1 个换行
                    //  - <div>文本<br></div>（文本+行尾占位 br）→ "文本\n"（1 个换行）
                    //  - <div>文本</div> → "文本\n"
                    // 否则会算成 2 个换行 → 光标偏移翻倍、插入错位多出一行
                    const lastNode = child.lastChild;
                    const trailingBr = lastNode && lastNode.nodeType === 1 && lastNode.tagName === "BR";
                    if (trailingBr && child.childNodes.length === 1) {
                        out += "\n";
                    } else {
                        visit(child);
                        if (!trailingBr) out += "\n";
                    }
                } else {
                    visit(child);
                }
            }
        }
    };
    visit(editable);
    return out;
}

function insertSpanAtCaret(editable, span) {
    const sel = window.getSelection();
    if (sel && sel.rangeCount && editable.contains(sel.anchorNode)) {
        const range = sel.getRangeAt(0);
        range.deleteContents();
        range.insertNode(span);
        range.setStartAfter(span);
        range.collapse(true);
        sel.removeAllRanges();
        sel.addRange(range);
    } else {
        editable.appendChild(span);
        const r = document.createRange();
        r.selectNodeContents(editable);
        r.collapse(false);
        sel?.removeAllRanges();
        sel?.addRange(r);
    }
}
function setCaretToOffset(editable, offset) {
    // 与 caretOffset/getPromptText 保持一致：资产 token 整体按 dataset.jzlName（资产名）长度计。
    // 否则光标定位会错位 → 点击资产窗插入跑到错误位置。
    const sel = window.getSelection();
    const range = document.createRange();
    let remaining = offset;
    let found = false;
    const walk = (parent) => {
        for (const child of Array.from(parent.childNodes)) {
            if (found) return;
            if (child.nodeType === 3) {
                const len = child.textContent.length;
                if (remaining <= len) {
                    range.setStart(child, remaining);
                    range.collapse(true);
                    found = true;
                    return;
                }
                remaining -= len;
            } else if (child.nodeType === 1) {
                if (child.classList && child.classList.contains("jzl-asset-token")) {
                    const len = (child.dataset.jzlName || child.dataset.jzlAsset || "").length;
                    if (remaining <= len) {
                        range.setStartAfter(child);
                        range.collapse(true);
                        found = true;
                        return;
                    }
                    remaining -= len;
                } else if (child.tagName === "BR") {
                    if (remaining <= 1) {
                        range.setStartAfter(child);
                        range.collapse(true);
                        found = true;
                        return;
                    }
                    remaining -= 1;
                } else if (child.tagName === "DIV" || child.tagName === "P") {
                    // 空行 <div><br></div>：只计 1 个换行；定位在 br 前（div 内开头）→ 素材填进该行，
                    // br 变行尾占位（若插在 br 后，行首 br 会产生额外空行 → 「汉字和素材之间多一行」）
                    const lastNode = child.lastChild;
                    const trailingBr = lastNode && lastNode.nodeType === 1 && lastNode.tagName === "BR";
                    const onlyBr = trailingBr && child.childNodes.length === 1;
                    if (onlyBr) {
                        if (remaining <= 1) {
                            range.setStart(child, 0);
                            range.collapse(true);
                            found = true;
                            return;
                        }
                        remaining -= 1;
                        continue;
                    }
                    walk(child);
                    if (found) return;
                    // 块级元素按「子内容 + 1 个换行」计（与 getPromptText 完全一致）：
                    // walk 后 remaining = 定位点相对「子内容末尾」的剩余偏移
                    //  - 0 → 定位点恰在子内容末尾 → 元素内部末尾（行尾占位 br 时定位 br 前）
                    //  - 1 → 定位点恰在元素换行位置 → 元素之后（必须 return，否则多推进 1 字符
                    //        会错位到下一个元素开头 → 「素材换行错位」）
                    //  - >1 → 消耗该换行后继续下一个节点
                    if (remaining === 0) {
                        const last = child.lastChild;
                        if (last) {
                            // 行尾占位 br：定位 br 前，避免 br 跑到素材前产生额外空行
                            if (last.nodeType === 1 && last.tagName === "BR") {
                                const before = last.previousSibling;
                                if (before) range.setStartAfter(before);
                                else range.setStart(child, 0);
                            } else {
                                range.setStartAfter(last);
                            }
                            range.collapse(true);
                            found = true;
                            return;
                        }
                        // 空块级元素：视为一个换行，定位元素之后
                        range.setStartAfter(child);
                        range.collapse(true);
                        found = true;
                        return;
                    }
                    if (remaining === 1) {
                        range.setStartAfter(child);
                        range.collapse(true);
                        found = true;
                        return;
                    }
                    remaining -= 1;
                } else {
                    walk(child);
                    if (found) return;
                }
            }
        }
    };
    walk(editable);
    if (found) {
        sel.removeAllRanges();
        sel.addRange(range);
        return range;
    }
    range.selectNodeContents(editable);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
    return range;
}

function assetFullName(kind, index, item) {
    return [KIND_PREFIX[kind] + (index + 1), item?.type, item?.name].filter(Boolean).join(" ");
}

function cacheAssets(node, settings) {
    if (node) node.__jzlAssets = (settings?.assets) || { images: [], videos: [], audios: [] };
}

// 槽位（类型+字母）分配：与后端 _build_asset_intro 完全一致
// 手选字母（A-Z）优先；缺失/非法按类型自动从 A 起顺延；同类同字母冲突顺延下一字母
// 条目在「素材描述 / 槽位表」里使用的名称（与后端 _asset_display_name 一致）。
// 「音频 → 角色音色」的名称来自所绑定角色，没有独立名称字段。
function assetDisplayName(kind, item) {
    if (kind === "audio" && String(item?.type || "") === "角色音色") return audioVoiceLabel(item);
    return (item?.name || "").trim();
}

function buildSlotMap(assets) {
    const map = {};
    const counters = {};
    const used = {};
    const kinds = [["image", "images"], ["video", "videos"], ["audio", "audios"]];
    for (const [kind, key] of kinds) {
        (assets?.[key] || []).forEach((item, i) => {
            if (item?.enabled === false) return;
            if (!assetDisplayName(kind, item)) return;
            const st = slotTypeOf(kind, item?.type || "");
            if (!st) return;
            let letter = (item?.letter || "").trim().toUpperCase();
            if (!/^[A-Z]$/.test(letter)) letter = String.fromCharCode(65 + (counters[st] || 0));
            let slot = st + letter;
            while (used[slot] && letter < "Z") {
                letter = String.fromCharCode(letter.charCodeAt(0) + 1);
                slot = st + letter;
            }
            used[slot] = true;
            counters[st] = (counters[st] || 0) + 1;
            map[`${kind}:${i}`] = slot;
        });
    }
    // ⭐ 角色音色已回归「音频 → 角色音色」子分类，随 audios 列表统一编号，不再需要单独的 voice: 槽位。

    return map;
}

function collectMentionItems(node) {
    const kindMap = { images: "image", videos: "video", audios: "audio" };
    const cache = node?.__jzlAssets || { images: [], videos: [], audios: [] };
    const slotMap = buildSlotMap(cache);
    const items = [];
    for (const kindKey of ["images", "videos", "audios"]) {
        const kind = kindMap[kindKey];
        let displayIndex = 0;  // 显示连续编号（跳过空槽位，避免跳号：音频1、音频3）
        (cache[kindKey] || []).forEach((item, idx) => {
            if (item?.enabled === false) return;
            if (!(item?.path || "").trim()) return;  // 空槽口（未上传素材）不显示在主界面
            const name = assetFullName(kind, displayIndex, item);  // 全名（图片1 角色 碗碗）
            displayIndex++;
            if (!name) return;
            const cleanName = (item?.name || "").trim();  // 资产自定义名（严格匹配用，一字不差）
            const slot = slotMap[`${kind}:${idx}`] || "";
            items.push({
                name,                    // 全名（带空格）→ 后端匹配用
                token: name.replace(/\s+/g, ""),  // 去空格全名（后端提取用）
                display: cleanName || name.replace(/\s+/g, ""),  // 简称（输入框显示）
                matchName: cleanName,     // 严格匹配名：提示词里精确出现该名字 → 着色/对齐（第3/5点）
                slot,                     // 槽位（类型+字母，如「角色A」，与后端 _build_asset_intro 一致）
                slotLabel: (slot ? slot + cleanName : (cleanName || name.replace(/\s+/g, ""))),  // 显示文本（角色A兔子）
                kind, type: item?.type || "", path: item?.path || "",
                description: item?.description || "",  // 详细描述（hover 用）
            });
        });
    }
    return items;
}

function closeMentionMenu() {
    if (mentionMenu) { mentionMenu.remove(); mentionMenu = null; }
    mentionState = null;
}

function openMentionMenu(editable, start, end, query, node) {
    const q = (query || "").toLowerCase();
    const all = collectMentionItems(node);
    // 过滤：@角色 → 只显示类型=角色；@孙悟空 → 只显示名称含"孙悟空"
    const items = all.filter((it) => {
        if (!q) return true;
        return it.type.toLowerCase().includes(q) || it.name.toLowerCase().includes(q);
    });
    if (!items.length) { closeMentionMenu(); return; }

    closeMentionMenu();
    mentionState = { editable, start, end };

    // 定位到光标后面（而非输入框底部）：取当前光标 range 的矩形
    let rect = editable.getBoundingClientRect();
    try {
        const sel = window.getSelection();
        if (sel && sel.rangeCount) {
            const r = sel.getRangeAt(0).cloneRange();
            r.collapse(true);
            if (editable.contains(r.startContainer)) {
                const cr = r.getBoundingClientRect();
                if (cr.width > 0 || cr.height > 0) rect = cr;
            }
        }
    } catch (_) {}
    const menu = document.createElement("div");
    menu.style.cssText = "position:fixed;z-index:10001;background:#1e1e1e;border:1px solid #444;border-radius:8px;max-height:340px;max-width:460px;overflow-y:auto;box-shadow:0 8px 20px rgba(0,0,0,0.5);padding:8px;";
    menu.style.left = rect.left + "px";
    menu.style.top = (rect.bottom + 4) + "px";

    // 自动列数：根据数量整理宫格
    const cols = items.length <= 3 ? items.length : Math.min(5, Math.max(3, Math.ceil(Math.sqrt(items.length))));
    const grid = document.createElement("div");
    grid.style.cssText = `display:grid;grid-template-columns:repeat(${cols},74px);gap:8px;`;

    for (const item of items) {
        const cell = document.createElement("div");
        cell.style.cssText = "display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer;border-radius:6px;padding:4px;border:1px solid transparent;";

        const thumb = document.createElement("div");
        thumb.style.cssText = "width:64px;height:64px;border-radius:5px;border:1px solid #444;background:#111;display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:26px;";
        if (item.kind === "image") {
            const img = document.createElement("img");
            img.style.cssText = "width:100%;height:100%;object-fit:cover;";
            img.src = item.path ? `${ASSET_PREVIEW_ENDPOINT}?path=${encodeURIComponent(item.path)}` : "/extensions/ComfyUI-JZL-MiniMax-H3/icon.png";
            img.onerror = () => { img.src = "/extensions/ComfyUI-JZL-MiniMax-H3/icon.png"; };
            thumb.appendChild(img);
        } else {
            thumb.textContent = item.kind === "video" ? "🎬" : "🎧";
        }

        const typeTag = document.createElement("span");
        typeTag.style.cssText = "font-size:10px;color:#8ab8dd;line-height:1;";
        typeTag.textContent = item.type || KIND_LABEL[item.kind] || "";

        const label = document.createElement("span");
        label.style.cssText = "font-size:11px;color:#ddd;line-height:1.2;max-width:70px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center;";
        label.textContent = item.slotLabel || item.name;

        cell.append(thumb, typeTag, label);
        cell.addEventListener("mousedown", (e) => { e.preventDefault(); e.stopPropagation(); });
        cell.addEventListener("click", () => chooseMention(item));
        cell.addEventListener("mouseenter", () => { cell.style.border = "1px solid #5b9bd5"; cell.style.background = "#2a2a2a"; });
        cell.addEventListener("mouseleave", () => { cell.style.border = "1px solid transparent"; cell.style.background = "transparent"; });
        grid.appendChild(cell);
    }

    menu.appendChild(grid);
    document.body.appendChild(menu);
    mentionMenu = menu;

    const dismiss = (e) => {
        if (!menu.contains(e.target)) closeMentionMenu();
    };
    setTimeout(() => document.addEventListener("mousedown", dismiss, { once: true }), 0);
}

function chooseMention(item) {
    if (!mentionState) return;
    const { editable, start, end } = mentionState;
    const token = item.token;  // 去空格全名（后端匹配用）
    closeMentionMenu();
    editable.focus();
    // 删除 @…到光标，替换为锁死着色 span（整体不可编辑，只能整体删除）
    const sRange = setCaretToOffset(editable, start);
    const eRange = setCaretToOffset(editable, end);
    sRange.setEnd(eRange.endContainer, eRange.endOffset);
    sRange.deleteContents();
    const span = makeAssetToken(token, item);
    sRange.insertNode(span);
    const caretRange = document.createRange();
    caretRange.setStartAfter(span);
    caretRange.collapse(true);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(caretRange);
    const nameLen = (item.matchName || item.display || token).length;
    promptCaretPos = start + nameLen;
    editable.dispatchEvent(new Event("input", { bubbles: true }));
    editable.dispatchEvent(new Event("change"));
    // @ 添加素材 → 自动激活「显示引用」并保存
    const _n = editable.__node;
    if (_n) { _n.__jzlAlignMode = "ref"; _n.__updateAlignStatus?.(); saveAlignMode(_n, "ref"); }
}

function makeAssetToken(token, item) {
    const span = document.createElement("span");
    span.dataset.jzlAsset = token;  // 全名（去空格），后端匹配用（旧工作流兼容）
    span.dataset.jzlName = (item.matchName || item.display || item.name || "").trim();  // 资产名 → 纯文本输出（清洗类型+编号）
    span.dataset.jzlKind = item.kind || "";
    span.contentEditable = "false";
    span.className = "jzl-asset-token";
    span.title = item.slot ? `${item.slot} ${item.name || token}` : (item.name || token);  // hover 显示槽位+全名
    // 图片：内嵌缩略图；视频/音频：CSS 伪元素图标（不占用文本）
    if (item.kind === "image") {
        const img = document.createElement("img");
        img.className = "jzl-asset-thumb";
        img.src = item.path ? `${ASSET_PREVIEW_ENDPOINT}?path=${encodeURIComponent(item.path)}` : "/extensions/ComfyUI-JZL-MiniMax-H3/icon.png";
        img.onerror = () => { img.src = "/extensions/ComfyUI-JZL-MiniMax-H3/icon.png"; };
        span.appendChild(img);
    }
    const text = document.createElement("span");
    text.textContent = item.slotLabel || item.display || token;  // 显示：类型+字母+名称（如「角色A兔子」）
    text.style.color = assetColor(item);
    span.appendChild(text);
    return span;
}

function findAssetByToken(token, node) {
    const t = (token || "").replace(/\s+/g, "").toLowerCase();
    if (!t) return null;
    const items = collectMentionItems(node);
    return items.find(it => it.token.toLowerCase() === t)
        || items.find(it => {
            const n = it.token.toLowerCase();
            return n && (n.includes(t) || t.includes(n));
        });
}

function renderPromptFromText(promptBox, text, node) {
    // 把纯文本里的「资产名」重新渲染成「缩略图 + 彩色文字」锁死 token：
    // 1) 严格一字不差匹配资产自定义名（最长优先，避免部分重叠）
    // 2) 旧工作流遗留的 图片N… 全名 token 兜底（自动迁移为纯文本）
    promptBox.innerHTML = "";
    const source = text || "";
    const items = collectMentionItems(node)
        .filter((it) => (it.matchName || "").length)
        .sort((a, b) => b.matchName.length - a.matchName.length);
    const legacyRe = /(?:图片|视频|音频)\d+[^\s@，。；,.、]*/g;
    let last = 0;
    let i = 0;
    while (i < source.length) {
        // 1) 严格资产名匹配（一字不差）
        let hit = null;
        for (const it of items) {
            if (source.startsWith(it.matchName, i)) { hit = it; break; }
        }
        if (hit) {
            if (i > last) promptBox.appendChild(document.createTextNode(source.slice(last, i)));
            promptBox.appendChild(makeAssetToken(hit.token, hit));
            i += hit.matchName.length;
            last = i;
            continue;
        }
        // 2) 旧工作流遗留全名 token（图片N 类型 名称）
        legacyRe.lastIndex = i;
        const m = legacyRe.exec(source);
        if (m && m.index === i) {
            if (i > last) promptBox.appendChild(document.createTextNode(source.slice(last, i)));
            const item = findAssetByToken(m[0], node);
            if (item) {
                promptBox.appendChild(makeAssetToken(m[0], item));
            } else {
                promptBox.appendChild(document.createTextNode(m[0]));
            }
            i += m[0].length;
            last = i;
            continue;
        }
        i++;
    }
    if (last < source.length) promptBox.appendChild(document.createTextNode(source.slice(last)));
}

// 参考元素切换（双向）：正向=纯文本按素材名一字不差对齐成 @token；逆向=已有 @token 时清洗成纯文本（只保留名称）
function toggleAlignPrompt(node) {
    const box = node?.__promptBox;
    if (!box) return;
    const ip = (node.widgets || []).find((w) => w.name === "internal_prompt");
    // 切换方向由当前模式（__jzlAlignMode）决定，而不是由「有无 @token」决定：
    // 空文本/无素材时也始终能双向切换（避免空框永远无 token → 卡在正向切不回纯文本）
    if (node.__jzlAlignMode === "text") {
        // 正向：纯文本 → 按素材名一字不差对齐成 @token
        renderPromptFromText(box, getPromptText(box), node);
        if (ip) setWidgetValue(ip, getPromptText(box));
        box.dispatchEvent(new Event("input", { bubbles: true }));
        node.setDirtyCanvas?.(true, true);
        node.__jzlAlignMode = "ref";
        node.__updateAlignStatus?.();
        saveAlignMode(node, "ref");
        notify("✅ 已切换为显示引用（按素材名一字不差着色）", "success");
    } else {
        // 逆向：@素材 → 纯文本（getPromptText 只输出名称，自动清洗类型+编号）
        const text = getPromptText(box);
        box.innerHTML = "";
        box.appendChild(document.createTextNode(text));
        if (ip) setWidgetValue(ip, text);
        box.dispatchEvent(new Event("input", { bubbles: true }));
        node.setDirtyCanvas?.(true, true);
        node.__jzlAlignMode = "text";
        node.__updateAlignStatus?.();
        saveAlignMode(node, "text");
        notify("✅ 已切换为纯文本（只保留素材名称）", "success");
    }
}

function saveAlignMode(node, mode) {
    // 把「参考元素切换」状态写入 manager_settings（随工作流保存，刷新不丢失）
    const msW = (node?.widgets || []).find((w) => w.name === "manager_settings");
    if (!msW) return;
    const raw = readWidgetValue(msW);
    let s = {};
    try { s = raw ? JSON.parse(raw) : {}; } catch (_) {}
    s.align_mode = mode;
    setWidgetValue(msW, JSON.stringify(s));
}

function insertAssetToken(editable, item, caretRef) {
    // 在内部编辑窗口（contenteditable）光标处插入素材 token。
    // caretRef: 可选 { value: number|null } 局部光标记忆（放大编辑器用，与主框全局 promptCaretPos 隔离）
    // 根本方案：不再手工 setCaretToOffset + insertNode 操作 DOM——Chrome contenteditable 会生成
    // <div><br></div>、行尾占位 <br> 等结构，在这些结构上做 DOM 定位必然与 getPromptText 计数错位
    // （表现为「多一行/光标跑到下一行/素材删不掉」的反复打地鼠）。
    // 改为：纯文本偏移（getPromptText/caretOffset 天然精确）→ 插入素材名 → renderPromptFromText
    // 整体重建为「textNode(含 \n) + token」干净结构 → 光标定位。DOM 不累积 div/br，三处计数统一。
    if (!editable) return;
    const node = editable.__node;
    const text = getPromptText(editable);
    // 1) 计算插入偏移：优先实时光标（IME 也拿得到）；否则编辑器自记忆 __lastCaret（点素材 mousedown 时捕获）；
    //    再回退 caretRef/promptCaretPos/末尾
    let offset = null;
    const _sel = window.getSelection();
    if (_sel && _sel.rangeCount && editable.contains(_sel.anchorNode)) {
        offset = caretOffset(editable);
    }
    if (typeof offset !== "number" && typeof editable.__lastCaret === "number") {
        offset = editable.__lastCaret;
    }
    if (typeof offset !== "number") {
        const pos = caretRef ? caretRef.value : promptCaretPos;
        offset = (typeof pos === "number") ? pos : text.length;
    }
    offset = Math.max(0, Math.min(text.length, Math.floor(offset)));
    // 2) 在纯文本偏移处插入素材名（渲染时匹配着色）
    const insertName = (item.matchName || item.display || item.token || "").trim();
    const newText = text.slice(0, offset) + insertName + text.slice(offset);
    // 3) 整体重渲染为 token（textNode + token，无 div/br）
    if (node) {
        renderPromptFromText(editable, newText, node);
    } else {
        editable.innerHTML = "";
        editable.appendChild(document.createTextNode(newText));
    }
    // 4) 光标定位到插入素材名之后
    const target = offset + insertName.length;
    setCaretToOffset(editable, target);
    editable.focus();
    editable.dispatchEvent(new Event("input", { bubbles: true }));
    editable.dispatchEvent(new Event("change"));
    const newPos = target;
    editable.__lastCaret = newPos;
    if (caretRef) caretRef.value = newPos; else promptCaretPos = newPos;
    // 点击添加素材 → 自动激活「显示引用」并保存
    const _n = editable.__node;
    if (_n) { _n.__jzlAlignMode = "ref"; _n.__updateAlignStatus?.(); saveAlignMode(_n, "ref"); }
}

function renderAssetWindow(windowBox, textarea, node, onInsert, insertFn) {
    // onInsert: 点击插入后的额外回调（放大编辑器同步显示模式用）；insertFn: 自定义插入回调（放大编辑器传局部光标记忆）
    windowBox.innerHTML = "";
    const items = collectMentionItems(node);
    if (!items.length) {
        windowBox.append(el("div", "font-size:11px;color:#777;padding:4px;", "（暂无素材，到「引用资产设置」添加）"));
        windowBox.style.height = "30px";
        windowBox.__onResize?.();
        return;
    }
    // 自动换行宫格：始终显示全部素材，多行时窗口与节点自动增高（按节点宽度自动换行，不遮挡）
    const grid = document.createElement("div");
    grid.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;padding:3px;align-content:flex-start;";
    for (const item of items) {
        const cell = document.createElement("div");
        cell.style.cssText = "flex:0 0 78px;width:78px;cursor:pointer;border-radius:6px;overflow:hidden;border:1px solid #333;background:#111;";
        cell.title = [item.name, item.description].filter(Boolean).join("\n");  // hover 显示完整内容
        // 缩略图（图片）或图标（视频/音频）
        const thumb = document.createElement("div");
        thumb.style.cssText = "width:78px;height:52px;display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:24px;background:#000;";
        if (item.kind === "image") {
            const img = document.createElement("img");
            img.style.cssText = "width:100%;height:100%;object-fit:cover;";
            img.src = item.path ? `${ASSET_PREVIEW_ENDPOINT}?path=${encodeURIComponent(item.path)}` : "";
            img.onerror = () => { img.remove(); };
            thumb.appendChild(img);
        } else {
            thumb.textContent = item.kind === "video" ? "🎬" : "🎧";
        }
        // 名称标签（类型 + 自定义名）——字号 11px 可显示约 7 个字（如「角色A小白兔」），超长省略号
        const label = document.createElement("div");
        label.style.cssText = "width:100%;height:22px;line-height:22px;font-size:11px;color:#cdd8e2;background:rgba(10,20,30,.7);text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0 3px;box-sizing:border-box;";
        label.textContent = item.slotLabel || item.display || "";
        cell.append(thumb, label);
        cell.addEventListener("mousedown", (e) => {
            e.stopPropagation();
            // blur 前捕获编辑器的实时光标，保证点击插入用最新位置（防止记忆过期错位）
            rememberCaret(textarea);
        });
        cell.addEventListener("click", () => { (insertFn || insertAssetToken)(textarea, item); onInsert && onInsert(); });
        grid.appendChild(cell);
    }
    windowBox.appendChild(grid);
    // 高度自适应内容（多行换行时增高），并通知节点调整整体高度；
    // 用 scrollHeight 取完整内容高度（含可能被裁剪的多行），保证素材不被吞。
    // 若该窗自身设了 max-height（节点表面那块 180px）→ 到顶就不再增高，改为本窗内部滚动。
    let h = Math.max(grid.scrollHeight || grid.offsetHeight, 30);
    try {
        const cap = parseFloat(getComputedStyle(windowBox).maxHeight);
        if (Number.isFinite(cap) && cap > 0) h = Math.min(h, cap);
    } catch (_) {}
    windowBox.style.height = h + "px";
    windowBox.__onResize?.();
}

function notify(msg, type = "success") {
    try {
        if (app?.ui?.toast) { app.ui.toast.add({ text: msg, type }); return; }
    } catch (_) {}
    console.log(`[JZL Asset] ${msg}`);
}

// 通用 modal 确认框（与 buildModal 同风格，非浏览器原生 confirm）：点击确认后执行 onOk
function confirmClearAssets(onOk) {
    const overlay = el("div", "position:fixed;inset:0;background:rgba(0,0,0,0.78);z-index:10050;display:flex;align-items:center;justify-content:center;");
    const box = el("div", "background:#1c1c1e;border:1px solid #333;border-radius:8px;width:360px;max-width:90vw;box-shadow:0 20px 40px rgba(0,0,0,0.6);padding:20px;box-sizing:border-box;");
    box.append(el("div", "font-size:15px;font-weight:700;color:#eee;margin-bottom:10px;", "🗑 清除全部素材"));
    box.append(el("div", "font-size:13px;color:#ddd;line-height:1.7;margin-bottom:18px;white-space:pre-line;", "确定要一键清除所有已添加的素材吗？\n（图片 / 视频 / 音频全部清空，此操作不可撤销！）"));
    const row = el("div", "display:flex;justify-content:flex-end;gap:10px;");
    const cancelBtn = el("button", "background:transparent;border:1px solid var(--border-color,#555);color:#fff;border-radius:4px;padding:7px 18px;font-size:13px;cursor:pointer;", "取消");
    const okBtn = el("button", "background:#7a2a2a;color:#fff;border:none;border-radius:4px;padding:7px 18px;font-size:13px;font-weight:600;cursor:pointer;", "🗑 确认清除");
    cancelBtn.addEventListener("click", () => overlay.remove());
    okBtn.addEventListener("click", () => { overlay.remove(); onOk && onOk(); });
    row.append(cancelBtn, okBtn);
    box.append(row);
    overlay.append(box);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.append(overlay);
}

// ── 🔄 重拍模式：底部折叠区（类似 MiniMax-H3 导演台「全局提示词 & 参考素材」）──
// 展开后锁定上方主提示词；结构：提示词选择(加载最后一次LLM拆解/增强) + 段号输入(1~99 对应每段) +
// 提示词显示窗(可二度修改)。运行仅用当前段提示词生成一段视频（穿透模式单段）。
function extractShots(script) {
    const out = [];
    const re = /\[SHOT_START\]([\s\S]*?)\[SHOT_END\]/g;
    let m;
    while ((m = re.exec(script || ""))) out.push(m[1].replace(/^[\s\n]+/, "").replace(/[\s\n]+$/, ""));
    return out;
}

// 把编辑后的各段正文写回整份剧本：保留统计表前缀、段与段之间的其它文字、以及尾部内容。
function jzlApplyShotBodies(script, bodies) {
    const src = String(script == null ? "" : script);
    const list = bodies || [];
    const re = /\[SHOT_START\][\s\S]*?\[SHOT_END\]/g;
    let out = "", last = 0, i = 0, m;
    while ((m = re.exec(src))) {
        out += src.slice(last, m.index);
        if (i < list.length) {
            out += "[SHOT_START]\n" + String(list[i] == null ? "" : list[i]).replace(/^\n+|\n+$/g, "") + "\n[SHOT_END]";
        } else {
            out += m[0];
        }
        last = m.index + m[0].length;
        i++;
    }
    out += src.slice(last);
    return out;
}

// ── 🔖 重拍：单镜头大编辑界面 ──────────────────────────────────────
// 相当于「📝 生成剧本查看器 → 📖 编辑剧本」的大界面，但**一次只显示一个镜头**（◀▶ 或下拉切换），
// 顶部可切「单选 / 多选」：单选 = 只重拍当前段；多选 = 勾选任意多段一起重拍。
// 保存时把编辑内容写回整份剧本的对应段（保留统计表前缀/尾部文字），再由外层保存到磁盘或节点配置。
function openReshootShotEditor(ctx) {
    const { self, state, onChanged, onSaveFile } = ctx || {};
    document.querySelectorAll(".jzl-shoteditor").forEach((m) => m.remove());
    if (!state || !Array.isArray(state.shots) || !state.shots.length) {
        notify("还没有载入剧本：先点「📝 选择剧本」，或展开重拍模式自动载入", "warning");
        return;
    }
    const bodies = state.shots.map((s) => String(s || ""));
    let cur = Math.max(0, Math.min(Number.isInteger(state.selected) ? state.selected : 0, bodies.length - 1));
    let mode = state.mode === "multi" ? "multi" : "single";
    const picked = new Set(
        Array.isArray(state.selectedShots) && state.selectedShots.length
            ? state.selectedShots.filter((i) => Number.isInteger(i) && i >= 0 && i < bodies.length)
            : [cur]
    );
    if (!picked.size) picked.add(cur);
    let dirty = false;

    const mkBtn = (label, css) => el("button",
        "flex:0 0 auto;height:30px;padding:0 12px;border-radius:5px;font-size:12px;cursor:pointer;white-space:nowrap;" +
        (css || "border:1px solid #5b9bd5;background:#3a3a3a;color:#eee;"), label);
    const segTitle = (i) => jzlSegmentLabel("[SHOT_START]\n" + bodies[i] + "\n[SHOT_END]") || `第 ${i + 1} 段`;

    const overlay = el("div", "position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:100004;display:flex;align-items:center;justify-content:center;");
    overlay.classList.add("jzl-shoteditor");
    const box = el("div", "width:94%;max-width:1360px;height:90vh;background:var(--comfy-menu-bg,#242424);border:1px solid #555;border-radius:8px;display:flex;flex-direction:column;padding:12px;box-sizing:border-box;box-shadow:0 10px 44px rgba(0,0,0,.6);");

    // ── 头部：标题 + 单选/多选 + 段导航 + 保存/关闭 ──
    const head = el("div", "display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;flex:0 0 auto;");
    head.appendChild(el("span", "font-size:14px;font-weight:600;color:#eee;white-space:nowrap;", "🔖 重拍镜头编辑"));
    const modeChip = (label, val) => {
        const b = mkBtn(label, "border:1px solid #5b9bd5;background:#3a3a3a;color:#ddd;");
        b.dataset.mode = val;
        b.addEventListener("click", () => {
            mode = val;
            state.mode = mode;
            if (mode === "single") { picked.clear(); picked.add(cur); }
            syncModeChrome();
            renderSide();
            renderNav();
        });
        return b;
    };
    const singleChip = modeChip("◉ 单选", "single");
    const multiChip = modeChip("☑ 多选", "multi");
    const navPrev = mkBtn("◀", "width:30px;padding:0;border:1px solid #5b9bd5;background:#3a3a3a;color:#eee;");
    navPrev.title = "上一段";
    const navInfo = el("span", "font-size:12px;color:#e8a87c;white-space:nowrap;min-width:160px;text-align:center;", "");
    const navNext = mkBtn("▶", "width:30px;padding:0;border:1px solid #5b9bd5;background:#3a3a3a;color:#eee;");
    navNext.title = "下一段";
    const navSel = document.createElement("select");
    navSel.style.cssText = "flex:1 1 220px;min-width:0;background:#1d1d1d;color:#ddd;border:1px solid #444;border-radius:5px;padding:5px 4px;font-size:12px;";
    navSel.title = "直接跳到某一段";
    const saveBtn = mkBtn("💾 保存剧本", "border:1px solid #5ecf8a;background:#2d5a3a;color:#fff;font-weight:600;");
    const closeBtn = mkBtn("关闭");
    head.append(singleChip, multiChip, el("span", "width:6px;", ""), navPrev, navInfo, navNext, navSel, saveBtn, closeBtn);

    // ── 主体：左 = 段列表（多选时显示勾选框），中 = 只显示当前这一段，右 = 资产显示窗 ──
    const bodyWrap = el("div", "flex:1 1 auto;display:flex;gap:10px;min-height:0;");
    const side = el("div", "flex:0 0 224px;display:none;flex-direction:column;min-height:0;border:1px solid #333;border-radius:5px;background:#1a1a1a;padding:6px;box-sizing:border-box;");
    const sideHead = el("div", "font-size:12px;color:#9fd6a4;margin-bottom:6px;flex:0 0 auto;", "");
    const sideTools = el("div", "display:flex;gap:6px;margin-bottom:6px;flex:0 0 auto;");
    const allBtn = mkBtn("全选", "height:24px;padding:0 8px;border:1px solid #5b9bd5;background:#3a3a3a;color:#ddd;");
    const noneBtn = mkBtn("清空", "height:24px;padding:0 8px;border:1px solid #666;background:#333;color:#ccc;");
    sideTools.append(allBtn, noneBtn);
    const sideList = el("div", "flex:1 1 auto;overflow-y:auto;min-height:0;scrollbar-gutter:stable;");
    side.append(sideHead, sideTools, sideList);

    const main = el("div", "flex:1 1 auto;display:flex;flex-direction:column;min-width:0;min-height:0;");
    const curHead = el("div", "display:flex;align-items:center;gap:8px;margin-bottom:6px;flex:0 0 auto;flex-wrap:wrap;");
    const curTag = el("span", "font-size:12px;color:#ffd98a;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 200px;min-width:0;", "");
    const copyOneBtn = mkBtn("📋 复制本段", "border:1px solid #666;background:#333;color:#ddd;");
    // 🎯 显示引用：把素材名着色成 token（与「生成剧本查看器 → 编辑剧本」完全一致的开关）
    const refBtn = mkBtn("🎯 显示引用", "border:1px solid #5b9bd5;background:#2a4a6a;color:#cfe3f7;");
    refBtn.title = "参考元素切换（纯文本 / 显示引用着色）";
    curHead.append(curTag, refBtn, copyOneBtn);
    // 编辑器：contenteditable（与放大编辑界面同构）→ 支持「显示引用」着色与素材 token 插入
    const editor = document.createElement("div");
    editor.contentEditable = "true";
    editor.spellcheck = false;
    editor.style.cssText = "flex:1 1 auto;min-height:0;width:100%;box-sizing:border-box;background:#161616;color:#ddd;border:1px solid #444;border-radius:5px;padding:10px 12px;font-size:13px;overflow-y:auto;outline:none;white-space:pre-wrap;word-break:break-word;line-height:1.6;";
    editor.dataset.placeholder = "这里只显示当前这一段的内容（不含 [SHOT_START]/[SHOT_END]，保存时会自动补回）；点右侧素材可插入 @引用";
    editor.addEventListener("mousedown", (e) => e.stopPropagation());
    const foot = el("div", "font-size:11px;color:#888;margin-top:6px;flex:0 0 auto;white-space:pre-wrap;", "");
    main.append(curHead, editor, foot);

    // ── 右侧：📁 资产显示窗（缩略图墙，点击把素材名插入到光标处）──
    // 宽度/内边距/滚动条槽位与「📖 编辑剧本」大界面完全一致：284px ⇒ 3 列 78px 卡片不被滚动条挤成 2 列
    const assetCol = el("div", "flex:0 0 284px;display:flex;flex-direction:column;min-height:0;");
    const assetHead = el("div", "font-size:12px;color:#bbb;margin-bottom:6px;flex:0 0 auto;", "📁 资产显示窗（点击插入）");
    const assetBox = el("div", "flex:1 1 auto;overflow-y:auto;min-height:0;scrollbar-gutter:stable;border:1px solid #333;border-radius:5px;background:#1a1a1a;padding:4px;box-sizing:border-box;");
    assetCol.append(assetHead, assetBox);

    bodyWrap.append(side, main, assetCol);
    box.append(head, bodyWrap);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    // ── 编辑器渲染（纯文本 / 显示引用 两种模式）──
    let refMode = false;
    const renderEditor = () => {
        if (refMode) {
            renderPromptFromText(editor, bodies[cur], self);
        } else {
            editor.innerHTML = "";
            editor.appendChild(document.createTextNode(bodies[cur] || ""));
        }
    };
    // 回车/粘贴统一成纯文本 \n（与放大编辑界面同规则，避免 Chrome 生成 div/br 破坏计数）
    editor.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key !== "Enter" || e.shiftKey) return;
        if (e.isComposing || e.keyCode === 229) return;
        e.preventDefault();
        const sel = window.getSelection();
        let r0 = null;
        if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) r0 = sel.getRangeAt(0);
        if (!r0) { r0 = document.createRange(); r0.selectNodeContents(editor); r0.collapse(false); }
        r0.deleteContents();
        const tn = document.createTextNode("\n");
        r0.insertNode(tn);
        const r2 = document.createRange();
        r2.setStartAfter(tn);
        r2.collapse(true);
        if (sel) { sel.removeAllRanges(); sel.addRange(r2); }
        editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    editor.addEventListener("paste", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const t = (e.clipboardData || window.clipboardData)?.getData("text/plain") || "";
        if (!t) return;
        const sel = window.getSelection();
        let range = null;
        if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) range = sel.getRangeAt(0);
        if (!range) { range = document.createRange(); range.selectNodeContents(editor); range.collapse(false); }
        range.deleteContents();
        range.insertNode(document.createTextNode(String(t).replace(/\r\n/g, "\n")));
        range.collapse(false);
        if (sel) { sel.removeAllRanges(); sel.addRange(range); }
        editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    renderEditor();

    // 渲染资产窗：点击素材 → 按光标位置把素材名插进编辑器（保持当前显示模式）
    const insertItem = (item) => {
        const name = String((item && (item.matchName || item.display || item.token)) || "").trim();
        if (!name) return;
        const text = getPromptText(editor);
        let off = null;
        const sel = window.getSelection();
        if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) {
            try { off = caretOffset(editor); } catch (_) {}
        }
        if (typeof off !== "number" && typeof editor.__lastCaret === "number") off = editor.__lastCaret;
        if (typeof off !== "number") off = text.length;
        off = Math.max(0, Math.min(text.length, Math.floor(off)));
        const newText = text.slice(0, off) + name + text.slice(off);
        bodies[cur] = newText;
        if (refMode) renderPromptFromText(editor, newText, self);
        else { editor.innerHTML = ""; editor.appendChild(document.createTextNode(newText)); }
        const pos = off + name.length;
        try { setCaretToOffset(editor, pos); } catch (_) {}
        editor.__lastCaret = pos;
        editor.focus();
        editor.dispatchEvent(new Event("input", { bubbles: true }));
    };
    try {
        renderAssetWindow(assetBox, editor, self, null, (ed, item) => insertItem(item));
        assetBox.style.height = "";   // renderAssetWindow 会写死 px 高度，这里交回 flex 控制
    } catch (e) {
        assetBox.appendChild(el("div", "font-size:11px;color:#777;padding:4px;", "（资产窗加载失败：" + ((e && e.message) || e) + "）"));
    }

    const flushEdit = () => { bodies[cur] = getPromptText(editor); };
    editor.addEventListener("input", () => { dirty = true; flushEdit(); renderSide(); renderNav(); });
    refBtn.addEventListener("click", () => {
        flushEdit();
        refMode = !refMode;
        renderEditor();
        refBtn.textContent = refMode ? "🎯 纯文本" : "🎯 显示引用";
        refBtn.style.background = refMode ? "#2f5a3a" : "#2a4a6a";
        notify(refMode ? "✅ 已切换为显示引用（素材名着色）" : "✅ 已切换为纯文本", "success");
    });

    // Esc 关闭（挂在 document 上：overlay 本身不可聚焦，挂在它上面收不到按键）
    const onKey = (e) => {
        if (e.key !== "Escape") return;
        if (!document.body.contains(overlay)) { document.removeEventListener("keydown", onKey, true); return; }
        e.stopPropagation();
        if (dirty && !window.confirm("有未保存的修改，确定关闭？")) return;
        close();
    };
    document.addEventListener("keydown", onKey, true);

    const syncModeChrome = () => {
        singleChip.style.background = mode === "single" ? "#2f5a3a" : "#3a3a3a";
        singleChip.style.color = mode === "single" ? "#dff5e6" : "#ddd";
        multiChip.style.background = mode === "multi" ? "#2f5a3a" : "#3a3a3a";
        multiChip.style.color = mode === "multi" ? "#dff5e6" : "#ddd";
        side.style.display = mode === "multi" ? "flex" : "none";
        sideHead.textContent = `勾选要重拍的镜头（已选 ${picked.size} / ${bodies.length}）`;
    };

    const renderNav = () => {
        navInfo.textContent = `第 ${cur + 1} / ${bodies.length} 段`;
        navSel.innerHTML = "";
        bodies.forEach((s, i) => {
            const o = document.createElement("option");
            o.value = String(i);
            o.textContent = `${picked.has(i) ? "✅ " : ""}第 ${i + 1} 段 · ${segTitle(i)}`;
            if (i === cur) o.selected = true;
            navSel.appendChild(o);
        });
        curTag.textContent = `正在编辑：第 ${cur + 1} 段 · ${segTitle(cur)}`;
        const targets = mode === "multi" ? Array.from(picked).sort((a, b) => a - b) : [cur];
        foot.textContent =
            `本次重拍：第 ${targets.map((i) => i + 1).join("、")} 段（共 ${targets.length} 段）· ` +
            `保存后写回整份剧本的第 ${cur + 1} 段${dirty ? " · ⚠️ 有未保存修改" : ""}`;
    };

    const renderSide = () => {
        if (mode !== "multi") { sideList.innerHTML = ""; return; }
        sideHead.textContent = `勾选要重拍的镜头（已选 ${picked.size} / ${bodies.length}）`;
        sideList.innerHTML = "";
        bodies.forEach((s, i) => {
            const row = el("div", "display:flex;align-items:center;gap:7px;padding:5px 6px;border-radius:4px;cursor:pointer;font-size:12px;" +
                (i === cur ? "background:#2a3a4a;color:#cfe3f7;" : "color:#ddd;"));
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.style.cssText = "flex:0 0 auto;width:14px;height:14px;cursor:pointer;accent-color:#5b9bd5;";
            cb.checked = picked.has(i);
            cb.addEventListener("click", (e) => e.stopPropagation());
            cb.addEventListener("change", () => {
                if (cb.checked) picked.add(i); else picked.delete(i);
                if (!picked.size) { picked.add(i); cb.checked = true; }   // 至少留一段
                renderSide(); renderNav();
            });
            const lbl = el("span", "flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;",
                `第 ${i + 1} 段 · ${segTitle(i)}`);
            row.append(cb, lbl);
            row.addEventListener("click", () => setCur(i));
            sideList.appendChild(row);
        });
    };

    const setCur = (i) => {
        flushEdit();
        cur = Math.max(0, Math.min(i, bodies.length - 1));
        renderEditor();
        if (mode === "single") { picked.clear(); picked.add(cur); }
        renderNav(); renderSide();
    };
    navPrev.addEventListener("click", () => setCur(cur - 1));
    navNext.addEventListener("click", () => setCur(cur + 1));
    navSel.addEventListener("change", () => setCur(parseInt(navSel.value, 10) || 0));
    allBtn.addEventListener("click", () => { bodies.forEach((_s, i) => picked.add(i)); renderSide(); renderNav(); });
    noneBtn.addEventListener("click", () => { picked.clear(); picked.add(cur); renderSide(); renderNav(); });
    copyOneBtn.addEventListener("click", () => {
        flushEdit();
        copyTextToClipboard(`[SHOT_START]\n${bodies[cur]}\n[SHOT_END]`, `第 ${cur + 1} 段`, `✅ 已复制第 ${cur + 1} 段`);
    });

    const close = () => { document.removeEventListener("keydown", onKey, true); overlay.remove(); };

    saveBtn.addEventListener("click", async () => {
        flushEdit();
        const targets = (mode === "multi" ? Array.from(picked) : [cur]).sort((a, b) => a - b);
        // 与 jzlApplyShotBodies 的落盘格式保持一致（去掉首尾空行），避免「节点里的 shots」与「重建后的剧本」不一致
        const norm = bodies.map((b) => String(b == null ? "" : b).replace(/^\n+|\n+$/g, ""));
        state.shots = norm;
        state.selected = cur;
        state.mode = mode;
        state.selectedShots = targets;
        state.script = jzlApplyShotBodies(state.script || "", norm);
        if (typeof onChanged === "function") onChanged();
        // 已指定具体剧本文件 → 同步写回磁盘（与「📝 生成剧本查看器」一致）
        if (typeof onSaveFile === "function") {
            try { await onSaveFile(state.script); } catch (_) {}
        }
        dirty = false;
        renderNav();
        notify(`✅ 已保存第 ${cur + 1} 段｜本次重拍第 ${targets.map((i) => i + 1).join("、")} 段`, "success");
        close();
        // 保存后关掉大界面，让用户直接点「▶ 运行」重拍
    });
    closeBtn.addEventListener("click", () => {
        if (dirty && !window.confirm("有未保存的修改，确定关闭？")) return;
        close();
    });
    overlay.addEventListener("click", (e) => {
        if (e.target !== overlay) return;
        if (dirty && !window.confirm("有未保存的修改，确定关闭？")) return;
        close();
    });

    // ── 初始化 ──
    syncModeChrome();
    renderNav();
    renderSide();
}

function createReshootSection(ctx) {
    const { self, promptBox, ipWidget, resizeNode, isMini, reshootSegWidget, container } = ctx;

    // 折叠头
    const header = el("div", "flex:0 0 auto;display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none;font-size:13px;font-weight:600;color:#e8a87c;border:1px solid #5b9bd5;border-radius:6px;padding:6px 8px;background:#2a2a2a;margin-top:4px;box-sizing:border-box;");
    const chevron = el("span", "font-size:11px;transition:transform .15s;", "▶");
    header.appendChild(chevron);
    header.appendChild(el("span", "", "🔄 重拍模式"));
    header.appendChild(el("span", "font-size:11px;color:#888;margin-left:auto;font-weight:400;",
        isMini ? "展开后锁定上方提示词；📝 选择剧本 / 📖 编辑剧本（单镜头大界面，可单选或多选重拍）"
               : "展开后锁定上方提示词；📝 选择剧本 / 📖 编辑剧本（单镜头大界面）+ 点「运行」只重拍选中的镜头"));

    // 主体（默认收起）
    const body = el("div", "flex:0 0 auto;display:none;margin-top:6px;border:1px solid #444;border-radius:6px;padding:8px;background:#222;box-sizing:border-box;");

    // 第一行：提示词来源状态显示 + 选择剧本（读盘任意剧本文档）+ 编辑剧本（单镜头大界面）+ 上传本地文件
    const selRow = el("div", "display:flex;align-items:center;gap:6px;margin-bottom:8px;flex-wrap:wrap;");
    const pickBtn = el("button", "flex:0 0 auto;height:28px;padding:0 10px;border-radius:5px;border:1px solid #5b9bd5;background:#2a4a6a;color:#cfe3f7;font-size:12px;cursor:pointer;white-space:nowrap;", "📝 选择剧本");
    pickBtn.title = "和「📝 生成剧本查看器」一样：在 output/jzl 下浏览并挑选任意剧本文档（.txt/.md）";
    const editBtn = el("button", "flex:0 0 auto;height:28px;padding:0 10px;border-radius:5px;border:1px solid #c99a4a;background:#4a3a20;color:#ffe6b8;font-size:12px;cursor:pointer;white-space:nowrap;", "📖 编辑剧本");
    editBtn.title = "大界面：一次只显示一个镜头，可切换、可编辑保存，并可单选/多选要重拍的镜头";
    const uploadBtn = el("button", "flex:0 0 auto;height:28px;padding:0 10px;border-radius:5px;border:1px solid #5b9bd5;background:#3a3a3a;color:#eee;font-size:12px;cursor:pointer;white-space:nowrap;", "📂 上传提示词");
    uploadBtn.title = "选择本地 .txt/.md 提示词文件作为重拍提示词源";
    const loadStatus = el("span", "font-size:11px;color:#888;flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", "提示词来源：最后一次 LLM 拆解");
    selRow.append(pickBtn, editBtn, uploadBtn, loadStatus);

    // 重拍视频编号（左右箭头切换 1~99）+ 提示词显示窗（可编辑）
    const segRow = el("div", "display:flex;align-items:center;gap:8px;margin-bottom:6px;");
    segRow.appendChild(el("span", "font-size:12px;color:#bbb;white-space:nowrap;", "重拍视频编号"));
    const segPrev = el("button", "width:26px;height:26px;flex:0 0 auto;border-radius:4px;border:1px solid #5b9bd5;background:#3a3a3a;color:#eee;font-size:12px;cursor:pointer;line-height:1;padding:0;", "◀");
    const segLabel = el("span", "font-size:12px;color:#e8a87c;white-space:nowrap;min-width:150px;text-align:center;", "当前选择重拍第 0 段视频");
    const segNext = el("button", "width:26px;height:26px;flex:0 0 auto;border-radius:4px;border:1px solid #5b9bd5;background:#3a3a3a;color:#eee;font-size:12px;cursor:pointer;line-height:1;padding:0;", "▶");
    const segTag = el("span", "font-size:11px;color:#888;white-space:nowrap;", "（共 0 段）");
    segPrev.addEventListener("mousedown", (e) => e.stopPropagation());
    segNext.addEventListener("mousedown", (e) => e.stopPropagation());
    segPrev.addEventListener("click", () => setSeg(state.selected));          // ◀ 上一段（setSeg 1-based：当前段号-1）
    segNext.addEventListener("click", () => setSeg(state.selected + 2));      // ▶ 下一段（setSeg 1-based：当前段号+1）
    segRow.append(segPrev, segLabel, segNext, segTag);
    // 提示词显示窗（可编辑，默认显示段号对应段的完整提示词）
    const editWrap = el("div", "position:relative;width:100%;");
    editWrap.setAttribute("data-capture-wheel", "true");   // 同主提示词框：聚焦后滚轮归本控件，不被画布抢走缩放
    const edit = document.createElement("textarea");
    edit.spellcheck = false;
    edit.style.cssText = "width:100%;height:190px;box-sizing:border-box;background:#161616;color:#ddd;border:1px solid #444;border-radius:5px;padding:6px 8px;font-size:12px;resize:none;outline:none;overflow-y:auto;white-space:pre-wrap;word-break:break-word;line-height:1.5;";
    edit.placeholder = "当前段的完整提示词显示在这里，可在此二度修改…";
    edit.addEventListener("mousedown", (e) => e.stopPropagation());
    editWrap.appendChild(edit);
    // 复制 / 放大编辑 / 资产窗：放到「重拍视频编号」行右端（编辑框上方外侧），不再悬浮在框内遮挡正文
    const eActions = el("div", "display:flex;gap:4px;margin-left:auto;z-index:6;");
    const eCopy = el("button", "width:22px;height:22px;border-radius:4px;border:1px solid #555;background:rgba(30,30,30,.92);color:#ccc;font-size:12px;cursor:pointer;line-height:1;padding:0;", "📋");
    eCopy.title = "复制全部文本";
    const eAsset = el("button", "width:22px;height:22px;border-radius:4px;border:1px solid #555;background:rgba(30,30,30,.92);color:#ccc;font-size:12px;cursor:pointer;line-height:1;padding:0;", "📁");
    eAsset.title = "显示/隐藏资产显示窗（点击素材插入 @引用到编辑框光标处）";
    const eZoom = el("button", "width:22px;height:22px;border-radius:4px;border:1px solid #555;background:rgba(30,30,30,.92);color:#ccc;font-size:12px;cursor:pointer;line-height:1;padding:0;", "⛶");
    eZoom.title = "放大编辑（打开大界面）";
    eActions.append(eCopy, eAsset, eZoom);
    segRow.appendChild(eActions);
    eCopy.addEventListener("mousedown", (e) => e.stopPropagation());
    eCopy.addEventListener("click", (e) => { e.stopPropagation(); copyTextToClipboard(edit.value, "重拍提示词"); });
    eZoom.addEventListener("mousedown", (e) => e.stopPropagation());
    eZoom.addEventListener("click", (e) => {
        e.stopPropagation();
        openTextZoomEditor("🔄 重拍提示词（放大编辑）", () => edit.value, (text) => {
            edit.value = text;
            if (state) { state.prompt = text; commit(); }
        }, self);
    });

    // 📁 资产显示窗（折叠）：点击素材 → 插入到编辑框光标处（textarea 版插入，保持纯文本）
    const assetBar = el("div", "display:none;margin-top:6px;border:1px solid #333;border-radius:5px;background:#1a1a1a;padding:4px;box-sizing:border-box;max-height:132px;overflow-y:auto;scrollbar-gutter:stable;");
    let assetBarReady = false;
    eAsset.addEventListener("mousedown", (e) => e.stopPropagation());
    eAsset.addEventListener("click", (e) => {
        e.stopPropagation();
        const show = assetBar.style.display === "none";
        assetBar.style.display = show ? "block" : "none";
        eAsset.style.background = show ? "rgba(42,74,106,.95)" : "rgba(30,30,30,.92)";
        eAsset.style.borderColor = show ? "#5b9bd5" : "#555";
        if (show && !assetBarReady) {
            assetBarReady = true;
            try {
                renderAssetWindow(assetBar, edit, self, null, (ed, item) => insertAssetIntoTextarea(ed, item));
                assetBar.style.height = "";   // renderAssetWindow 会写死 px 高度 → 交回 CSS（max-height + 滚动）
            } catch (err) {
                assetBar.appendChild(el("div", "font-size:11px;color:#777;padding:4px;", "（资产窗加载失败：" + ((err && err.message) || err) + "）"));
            }
        }
        if (show) setTimeout(() => { try { resizeNode(); } catch (_) {} }, 30);
    });

    body.append(selRow, segRow, editWrap, assetBar);

    // 状态（持久到 manager_settings.reshoot，刷新不丢失；source 记录提示词来源显示）
    // mode/selectedShots：单选 = 只重拍当前段；多选 = 重拍勾选的多段（在「📖 编辑剧本」里勾选）
    const state = self.__reshootState || (self.__reshootState = {
        script: "", shots: [], selected: 0, prompt: "", expanded: false, source: "",
        mode: "single", selectedShots: [], pick: "",
    });
    if (!Array.isArray(state.selectedShots)) state.selectedShots = [];
    if (!state.mode) state.mode = "single";

    // 本次要重拍的段索引（多选 = 勾选集合，单选 = 当前段）
    const reshootTargets = () => {
        const n = state.shots.length;
        let list = state.mode === "multi" ? (state.selectedShots || []) : [state.selected];
        list = list.filter((i) => Number.isInteger(i) && i >= 0 && i < n);
        if (!list.length && n) list = [Math.max(0, Math.min(state.selected || 0, n - 1))];
        return list.slice().sort((a, b) => a - b);
    };
    const targetsText = () => {
        const t = reshootTargets();
        return t.length ? `第 ${t.map((i) => i + 1).join("、")} 段` : "（无）";
    };

    const readSettings = async () => (await loadManager(self)).settings;
    const commit = async () => {
        try {
            const s = await readSettings();
            s.reshoot = {
                script: state.script, shots: state.shots,
                selected: state.selected, prompt: state.prompt, expanded: expanded,
                source: state.source,
                mode: state.mode, selectedShots: reshootTargets(), pick: state.pick || "",
            };
            await saveManager(self, s);
        } catch (_) {}
    };

    const setSeg = (n) => {
        if (!state.shots.length) {
            state.selected = 0; state.prompt = ""; edit.value = ""; segTag.textContent = "（共 0 段）"; segLabel.textContent = "当前选择重拍第 0 段视频";
            return;
        }
        let idx = (Number.isInteger(n) ? n : parseInt(n, 10) || 1) - 1;
        if (idx < 0) idx = 0;
        if (idx >= state.shots.length) idx = state.shots.length - 1;
        state.selected = idx;
        state.prompt = state.shots[idx].trim();
        edit.value = state.prompt;
        segLabel.textContent = state.mode === "multi"
            ? `重拍范围：${targetsText()}（共 ${reshootTargets().length} 段）`
            : `当前选择重拍第 ${idx + 1} 段视频`;
        segTag.textContent = `（共 ${state.shots.length} 段）`;
        // Mini：段号同步到「重拍视频编号」参数（编码选段用）
        if (isMini && reshootSegWidget) setWidgetValue(reshootSegWidget, idx + 1);
        commit();
    };
    segPrev.title = "上一段";
    segNext.title = "下一段";

    // 下方的紧凑编辑框与「📖 编辑剧本」共用同一份 state.shots：
    // 在这里改字 → 直接写回当前段并重建整份剧本（大界面再打开时看到的就是最新内容）。
    edit.addEventListener("input", () => {
        if (!state.shots.length) return;
        const i = Math.max(0, Math.min(state.selected || 0, state.shots.length - 1));
        state.shots[i] = edit.value;
        state.prompt = edit.value;
        state.script = jzlApplyShotBodies(state.script || "", state.shots);
        clearTimeout(edit.__jzlT);
        edit.__jzlT = setTimeout(() => { commit(); }, 400);
    });

    // 锁定 / 解锁：主提示词框 + 节点其它全部参数（重拍模式下其它参数不可操作，点击提示）
    const LOCK_TIP = "请关闭重拍模式！";
    const lockHint = el("span", "font-size:11px;color:#ffb84d;white-space:nowrap;", "🔒 其它参数已锁定");
    lockHint.style.display = "none";
    lockHint.title = LOCK_TIP;
    header.appendChild(lockHint);
    const lockCtx = { handler: null };

    const setLocked = (locked) => {
        self.__jzlReshootLocked = !!locked;
        // 1) 主提示词框
        if (locked) {
            promptBox.contentEditable = "false";
            promptBox.style.background = "#1d1d1d";
            promptBox.style.color = "#777";
            promptBox.dataset.placeholder = "🔒 重拍模式已锁定：上方提示词不可编辑（仅重拍选中镜头）";
        } else {
            promptBox.contentEditable = "true";
            promptBox.style.background = "#2a2a2a";
            promptBox.style.color = "#ddd";
            promptBox.dataset.placeholder = "输入故事/剧本提示词，用 @ 引用素材…";
        }
        // 2) 节点表面参数 widget（新版前端每个 widget 有 DOM element）→ 鼠标不可操作
        for (const w of (self.widgets || [])) {
            if (!w) continue;
            const nm = w.name || "";
            if (nm === "jzl_manager" || nm === "internal_prompt" || nm === "manager_settings") continue;
            const elm = w.element;
            if (!elm || !elm.style) continue;
            if (locked) {
                if (w.__jzlLockBak === undefined) {
                    w.__jzlLockBak = {
                        pe: elm.style.pointerEvents, op: elm.style.opacity, cur: elm.style.cursor,
                        dis: !!w.disabled, optDis: w.options ? !!w.options.disabled : undefined,
                    };
                }
                elm.style.pointerEvents = "none";
                elm.style.opacity = "0.45";
                elm.style.cursor = "not-allowed";
                w.disabled = true;              // 新版前端：彻底阻止下拉/输入（值仍可被重拍拦截程序临时改写）
                if (!w.options) w.options = {};
                w.options.disabled = true;
                if (!w.__jzlLockClick) {
                    w.__jzlLockClick = (e) => {
                        if (!self.__jzlReshootLocked) return;
                        e.preventDefault();
                        e.stopPropagation();
                        notify(LOCK_TIP, "error");
                    };
                    elm.addEventListener("mousedown", w.__jzlLockClick, true);
                    elm.addEventListener("click", w.__jzlLockClick, true);
                }
            } else if (w.__jzlLockBak !== undefined) {
                elm.style.pointerEvents = w.__jzlLockBak.pe || "";
                elm.style.opacity = w.__jzlLockBak.op || "";
                elm.style.cursor = w.__jzlLockBak.cur || "";
                w.disabled = !!w.__jzlLockBak.dis;
                if (w.options && w.__jzlLockBak.optDis !== undefined) w.options.disabled = !!w.__jzlLockBak.optDis;
                if (w.__jzlLockClick) {
                    elm.removeEventListener("mousedown", w.__jzlLockClick, true);
                    elm.removeEventListener("click", w.__jzlLockClick, true);
                    delete w.__jzlLockClick;
                }
                delete w.__jzlLockBak;
            }
        }
        // 3) DOM widget 内除重拍区以外的部分 → 同步变暗 + 不可点
        if (container) {
            for (const c of Array.from(container.children || [])) {
                if (c === header || c === body) continue;
                if (locked) {
                    if (c.__jzlLockBak === undefined) c.__jzlLockBak = { pe: c.style.pointerEvents, op: c.style.opacity };
                    c.style.pointerEvents = "none";
                    c.style.opacity = "0.45";
                } else if (c.__jzlLockBak !== undefined) {
                    c.style.pointerEvents = c.__jzlLockBak.pe || "";
                    c.style.opacity = c.__jzlLockBak.op || "";
                    delete c.__jzlLockBak;
                }
            }
        }
        lockHint.style.display = locked ? "" : "none";
        // 4) 点锁定区 → 提示「请关闭重拍模式！」（容器捕获阶段；重拍区放行）
        if (locked && !lockCtx.handler) {
            lockCtx.handler = (e) => {
                if (!self.__jzlReshootLocked) return;
                if (header.contains(e.target) || body.contains(e.target)) return;
                e.preventDefault();
                e.stopPropagation();
                notify(LOCK_TIP, "error");
            };
            container?.addEventListener("mousedown", lockCtx.handler, true);
            container?.addEventListener("click", lockCtx.handler, true);
        } else if (!locked && lockCtx.handler) {
            container?.removeEventListener("mousedown", lockCtx.handler, true);
            container?.removeEventListener("click", lockCtx.handler, true);
            lockCtx.handler = null;
        }
        setTimeout(() => { try { resizeNode(); } catch (_) {} }, 30);
    };

    // 加载提示词源（默认LLM拆解 或 本地文件）→ 切段 + 生产规范校验 + 填充界面。
    // 重拍提示词与上方主提示词完全独立：绝不写入 internal_prompt / 主编辑框。
    const applyScript = async (script, meta) => {
        if (!script || !String(script).trim()) {
            notify("提示词为空", "error");
            loadStatus.textContent = "⚠️ 提示词为空";
            loadStatus.style.color = "#ffb84d";
            return;
        }
        script = String(script).trim();
        const shots = extractShots(script);
        state.script = script; state.shots = shots; state.selected = 0;
        state.selectedShots = [];        // 新剧本 → 重拍范围重置为「当前段」
        state.mode = "single";
        state.source = (meta && meta.display) ? meta.display : "";
        // 生产规范校验：段数 / H3_PROMPT 标记 / 与「生成视频数量」一致性
        const vcW = (self.widgets || []).find((w) => w.name === "video_count");
        const vcRaw = String(readWidgetValue(vcW) ?? "").trim();
        const vcAuto = /剧情决定|自动/.test(vcRaw);           // 「剧情决定」：段数由 LLM 按剧情时间线自动定
        const vc = (vcRaw.match(/\d+/) || [0])[0] * 1 || 6;
        const warns = [];
        if (!shots.length) warns.push("未识别到 [SHOT_START] 分段");
        if (!vcAuto && shots.length && shots.length !== vc) warns.push(`识别到 ${shots.length} 段，与「生成视频数量」${vc} 不一致`);
        const noH3 = shots.filter((s) => s.indexOf("===H3_PROMPT===") < 0).length;
        if (noH3) warns.push(`${noH3} 段缺少 ===H3_PROMPT=== 标记`);
        if (warns.length) {
            // 状态行始终显示提示词来源；规范问题用 toast 弹出 + 行尾 ⚠ 标记
            notify("⚠️ " + warns.join("；"), "error");
            loadStatus.textContent = (state.source || `已加载 ${shots.length} 段`) + " ⚠️";
            loadStatus.style.color = "#ffb84d";
            loadStatus.title = "生产规范提示：\n" + warns.join("\n");
        } else if (state.source) {
            loadStatus.textContent = state.source;
            loadStatus.style.color = "#5ecf8a";
            loadStatus.title = "";
        } else {
            loadStatus.textContent = `已加载 ${shots.length} 段`;
            loadStatus.style.color = "#5ecf8a";
            loadStatus.title = "";
        }
        // 默认选中第1段
        setSeg(1);
        await commit();
        try { resizeNode(); } catch (_) {}
    };

    // 默认来源：最后一次 LLM 拆解/增强（后端 output/jzl/最近提示词.json），展开时自动加载
    const autoLoad = async () => {
        state.pick = "";   // 回到「跟随最后一次拆解」模式
        try {
            const resp = await api.fetchApi(RESHOOT_ENDPOINT);
            const data = await resp.json().catch(() => ({}));
            if (data && data.script) {
                const disp = `提示词来源：最后一次 LLM 拆解（${data.story_name || "未命名"}${data.time ? " · " + data.time : ""}）`;
                loadStatus.textContent = disp;
                loadStatus.style.color = "#5ecf8a";
                await applyScript(data.script, { display: disp });
            } else {
                loadStatus.textContent = "未找到最后一次 LLM 拆解提示词，可点「📝 选择剧本」或「📂 上传提示词」";
                loadStatus.style.color = "#888";
            }
        } catch (_) {
            loadStatus.textContent = "加载失败，可点「📝 选择剧本」或「📂 上传提示词」";
            loadStatus.style.color = "#888";
        }
    };

    // 📝 选择剧本：与「📝 生成剧本查看器」同一套浏览逻辑（output/jzl 下任意 .txt/.md）
    pickBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    pickBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const storyW = (self.widgets || []).find((w) => w.name === "story_name");
        const story = String(storyW ? readWidgetValue(storyW) || "" : "").trim();
        openScriptPicker(story, state.pick || "", async (choice) => {
            if (choice && choice.latest) { await autoLoad(); return; }
            const rel = String((choice && choice.rel) || "");
            if (!rel) return;
            try {
                const r = await api.fetchApi(`/jzl/fs_read?path=${encodeURIComponent(rel)}`);
                const d = await r.json().catch(() => ({}));
                if (!(d && d.ok)) { notify("❌ 读取失败：" + ((d && d.error) || "未知错误"), "error"); return; }
                state.pick = rel;
                await applyScript(d.text || "", { display: `剧本来源：${rel}` });
                loadStatus.textContent = `剧本来源：${rel}`;
                loadStatus.style.color = "#5ecf8a";
            } catch (err) { notify("❌ 读取失败：" + ((err && err.message) || err), "error"); }
        });
    });

    // 📖 编辑剧本：大界面（一次只显示一个镜头，可切换/编辑/保存，并可单选多选重拍范围）
    editBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    editBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (!state.shots.length) { notify("还没有载入剧本：先点「📝 选择剧本」或「📂 上传提示词」", "warning"); return; }
        openReshootShotEditor({
            self,
            state,
            onChanged: () => {
                // 大界面里改了当前段 → 同步到紧凑编辑框与状态行
                edit.value = state.shots[state.selected] || "";
                edit.dataset.idx = String(state.selected);
                segLabel.textContent = state.mode === "multi"
                    ? `重拍范围：${targetsText()}（共 ${reshootTargets().length} 段）`
                    : `当前选择重拍第 ${state.selected + 1} 段视频`;
                segTag.textContent = `（共 ${state.shots.length} 段）`;
                if (isMini && reshootSegWidget) setWidgetValue(reshootSegWidget, state.selected + 1);
                commit();
            },
            onSaveFile: async (script) => {
                if (!state.pick) {
                    notify("ℹ️ 当前剧本不是从文件载入的（跟随最后一次拆解/本地上传），已保存在节点配置里", "warning");
                    return;
                }
                try {
                    const r = await api.fetchApi("/jzl/fs_write", {
                        method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ path: state.pick, text: script || "" }),
                    });
                    const d = await r.json().catch(() => ({}));
                    if (d && d.ok) notify("✅ 剧本已写回文件：" + state.pick, "success");
                    else notify("⚠️ 节点内已保存，但写回磁盘失败：" + ((d && d.error) || "未知错误"), "warning");
                } catch (err) {
                    notify("⚠️ 节点内已保存，但写回磁盘失败：" + ((err && err.message) || err), "warning");
                }
            },
        });
    });

    // 上传提示词：选择本地 .txt/.md 提示词文件（FileReader 读取内容作为重拍提示词源）
    uploadBtn.addEventListener("click", () => {
        const input = document.createElement("input");
        input.type = "file";
        input.accept = ".txt,.text,.md,text/plain";
        input.style.display = "none";
        const done = () => { if (input.parentNode) input.parentNode.removeChild(input); };
        input.addEventListener("change", () => {
            const file = input.files && input.files[0];
            if (!file) { done(); return; }
            const reader = new FileReader();
            reader.onload = async () => {
                done();
                const txt = String(reader.result || "").replace(/^\uFEFF/, "");  // 容错 UTF-8 BOM
                state.pick = "";   // 本地上传不写盘
                try { await applyScript(txt, { name: file.name, from: "upload", display: `提示词来源：本地文件 ${file.name}` }); }
                catch (e) { notify(`解析失败：${e && e.message ? e.message : e}`, "error"); }
            };
            reader.onerror = () => { done(); notify("文件读取失败", "error"); };
            reader.readAsText(file, "utf-8");
        });
        input.addEventListener("cancel", done);
        document.body.appendChild(input);
        input.click();
    });
    uploadBtn.addEventListener("mousedown", (e) => e.stopPropagation());

    // 折叠展开
    let expanded = false;
    const toggle = () => {
        expanded = !expanded;
        state.expanded = expanded;
        chevron.textContent = expanded ? "▼" : "▶";
        body.style.display = expanded ? "block" : "none";
        setLocked(expanded);
        commit();
        // 首次展开且无数据：自动加载默认来源（最后一次 LLM 拆解）
        if (expanded && !state.shots.length && !state.autoLoaded) { state.autoLoaded = true; autoLoad(); }
        setTimeout(() => { try { resizeNode(); } catch (_) {} }, 30);
    };
    header.addEventListener("mousedown", (e) => e.stopPropagation());
    header.addEventListener("click", toggle);

    // 重拍运行拦截：仅 Pro 生效（重拍模式展开且已加载提示词时，点「运行」自动以穿透模式生成选中段）。
    // 单选 = 只生成当前段；多选 = 把勾选的若干段拼成一份提示词，一次生成多段。
    // apply 在 queuePrompt 提交前临时改参数，restore 在提交后恢复（不污染主提示词/普通模式）。
    // Mini 不做运行拦截（用「重拍视频编号」参数选段编码），挂空补丁避免 graph_to_prompt 处理。
    if (!isMini) {
        self.__reshootBuildRunPatch = () => {
            if (!expanded || !state.shots.length) return null;   // 收起或无数据时不拦截
            const idxs = reshootTargets();
            if (!idxs.length) return null;
            const blocks = idxs
                .map((i) => ({ i, body: String(state.shots[i] || "").trim() }))
                .filter((x) => x.body)
                .map((x) => `[SHOT_START]\n${x.body}\n[SHOT_END]`);
            if (!blocks.length) return null;
            const prompt = blocks.join("\n\n");
            const rmW = (self.widgets || []).find((w) => w.name === "run_mode");
            const vcW = (self.widgets || []).find((w) => w.name === "video_count");
            const backupRM = rmW ? readWidgetValue(rmW) : null;
            const backupVC = vcW ? readWidgetValue(vcW) : null;
            const backupIP = ipWidget ? readWidgetValue(ipWidget) : "";
            return {
                apply() {
                    if (rmW) setWidgetValue(rmW, "🪄 穿透生成模式");
                    // ⭐ 段数按选中段数写入：按 widget 形态写入（数值型写数字，下拉型写「生成N段」）。
                    //    跨形态写值会被 ComfyUI 校验拒绝（历史上这里固定写「生成1段」→ 短剧导演台直接报
                    //    Failed to convert an input value to a INT value）。
                    setVideoCount(vcW, blocks.length);
                    if (ipWidget) setWidgetValue(ipWidget, prompt);
                },
                restore() {
                    try {
                        if (rmW && backupRM !== null) setWidgetValue(rmW, backupRM);
                        if (vcW && backupVC !== null) setWidgetValue(vcW, backupVC);
                        if (ipWidget) setWidgetValue(ipWidget, backupIP);
                    } catch (_) {}
                },
            };
        };
    } else {
        self.__reshootBuildRunPatch = () => null;
    }

    // 刷新时恢复（由 onNodeCreated 的 loadManager.then 调用）
    const refresh = (settings) => {
        const rs = settings && settings.reshoot;
        if (!rs) return;
        state.script = rs.script || "";
        state.shots = Array.isArray(rs.shots) ? rs.shots : extractShots(rs.script);
        state.selected = Number.isInteger(rs.selected) ? Math.max(0, Math.min(rs.selected, state.shots.length - 1)) : 0;
        state.prompt = rs.prompt || (state.shots[state.selected] || "");
        state.expanded = !!rs.expanded;
        state.source = rs.source || "";
        state.mode = rs.mode === "multi" ? "multi" : "single";
        state.pick = rs.pick || "";
        state.selectedShots = Array.isArray(rs.selectedShots)
            ? rs.selectedShots.filter((i) => Number.isInteger(i) && i >= 0 && i < state.shots.length)
            : [];
        edit.value = state.prompt || (state.shots[state.selected] || "");
        segTag.textContent = state.shots.length ? `（共 ${state.shots.length} 段）` : "（共 0 段）";
        segLabel.textContent = !state.shots.length ? "当前选择重拍第 0 段视频"
            : (state.mode === "multi"
                ? `重拍范围：${targetsText()}（共 ${reshootTargets().length} 段）`
                : `当前选择重拍第 ${state.selected + 1} 段视频`);
        if (state.source) { loadStatus.textContent = state.source; loadStatus.style.color = "#5ecf8a"; }
        if (state.expanded) {
            expanded = true;
            chevron.textContent = "▼";
            body.style.display = "block";
            setLocked(true);
            if (!state.shots.length && !state.autoLoaded) { state.autoLoaded = true; autoLoad(); }
            setTimeout(() => { try { resizeNode(); } catch (_) {} }, 60);
        }
    };

    return { header, body, refresh };
}

async function loadLists() {
    // 模型/风格等列表仍从后端拉（仅列表，不含节点配置）
    // ⚠️ 用「默认值 + 后端字段全量透传」的写法：以后后端新增列表字段（如 selflift_upscaler_models /
    //    semantic_bridge_adapters）前端无需再改这里——曾因漏加一行导致下拉拿不到数据、显示「未找到」并置灰。
    //    唯一必须排除的是 settings：节点配置要取工作流内的 manager_settings，不能被全局配置覆盖。
    const fallback = {
        llm_models: [], mmproj_models: ["None"], chat_handlers: ["None"],
        diffusion_models: [], clip_models: [], vae_models: [], lora_models: [],
        story_styles: [], story_style_labels: {}, save_dir: "",
        upscaler_models: [], selflift_upscaler_models: ["none"], semantic_bridge_adapters: [],
    };
    try {
        const resp = await api.fetchApi(MANAGER_ENDPOINT);
        const data = (await resp.json().catch(() => ({}))) || {};
        delete data.settings;
        return { ...fallback, ...data };
    } catch (_) {
        return { ...fallback };
    }
}

// 资产归档：把非 jzl/ 开头的素材路径（源文件绝对路径 / 历史遗留 / 导入 txt 记录的原路径）
// 交给后端复制到 input/jzl/{kind} 并统一为相对路径（jzl/{kind}/xxx），回写 manager_settings。
// 之后读取/导出都用 input/jzl 里的副本，原文件删除也毫无影响。
async function archiveAssets(node, settings) {
    const assets = settings?.assets;
    if (!assets || typeof assets !== "object") return false;
    const needs = ["images", "videos", "audios"].some((k) =>
        (assets[k] || []).some((it) => {
            const p = (it?.path || "").trim();
            return p && !/^jzl[\\/]/i.test(p);  // 有非 jzl/ 相对路径的素材 → 需要归档
        })
    );
    if (!needs) return false;  // 全部已是相对路径 → 无需归档（幂等）
    try {
        const resp = await api.fetchApi("/jzl/archive_assets", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ assets }),
        });
        const data = await resp.json().catch(() => ({}));
        if (data?.ok && data.assets) {
            settings.assets = data.assets;
            if (node) node.__jzlAssets = data.assets;
            try { await saveManager(node, settings); } catch (_) {}
            return true;
        }
    } catch (_) {}
    return false;
}

async function loadManager(node) {
    // 节点独立配置：优先读工作流内保存的 manager_settings；空则全新默认（不继承其他节点/全局）。
    // 先 await loadLists 再读 widget：configure 在 onNodeCreated 之后才恢复 manager_settings 值，
    // 若在 await 前读取会拿到空值 → 刷新后资产窗/提示词着色丢失（第4点修复）。
    const lists = await loadLists();
    const w = node?.widgets?.find((x) => x.name === "manager_settings");
    const raw = w ? readWidgetValue(w) : "";
    let settings = null;
    if (raw && typeof raw === "string" && raw.trim()) {
        try { const s = JSON.parse(raw); if (s && typeof s === "object") settings = s; } catch (_) {}
    }
    settings = settings || defaultSettings();
    // 递归补齐缺失键：面板里读的是「?? 默认值」，若对象里根本没这个键，保存时也写不进去 →
    // 后端只能回退，出现「面板显示 8 步、后端按 4 步跑」这类静默错配（selflift.steps 就踩过）。
    fillDefaults(settings, defaultSettings());
    if (migrateLegacyDefaults(settings)) {
        try { await saveManager(node, settings); } catch (_) {}  // 迁移后立即落盘，避免刷新又变回旧值
    }
    // 资产归档：把源文件绝对路径素材复制到 input/jzl 并统一为相对路径（原文件删除不影响）
    try { await archiveAssets(node, settings); } catch (_) {}
    return { settings, ...lists };
}

// 旧默认值 → 新默认值的一次性迁移。返回是否发生了改动。
// 只改写「与我们上一版内置默认完全相同」的字段（说明用户从未手动改过），避免覆盖用户自己的配置。
function migrateLegacyDefaults(s) {
    let changed = false;
    // ① 旧默认 API 值
    const LEGACY = [
        // [路径, 旧默认, 新默认]
        [["enhance", "api", "model"], "deepseek-v4-flash", "deepseek-flash"],
        [["enhance", "api", "base_url"], "https://api.deepseek.com/v1", "https://api.deepseek.com"],
    ];
    for (const [path, oldVal, newVal] of LEGACY) {
        let o = s;
        for (let i = 0; i < path.length - 1 && o; i++) {
            o = o[path[i]];
            if (!o || typeof o !== "object") { o = null; }
        }
        if (!o) continue;
        const k = path[path.length - 1];
        if (o[k] === oldVal) { o[k] = newVal; changed = true; }
    }
    // ② 角色音色回归音频分类：把旧配置里挂在「图片 · 角色」上的 voice_path 迁成「音频 · 角色音色」条目
    if (migrateLegacyVoice(s)) changed = true;
    // ③ skills.expand_source → expand_source_mode
    //    fillDefaults 已先把新键补成默认值，所以这里只要旧键还在，就以旧值为准（旧键=用户此前选过的值）
    const sk = s.skills;
    if (sk && typeof sk === "object" && sk.expand_source !== undefined) {
        sk.expand_source_mode = sk.expand_source;
        delete sk.expand_source;
        changed = true;
    }
    // ④ 「不拆解」已从 UI 移除（该职责属于穿透模式），旧配置统一修正为 story_decompose=true
    const en = s.enhance;
    if (en && typeof en === "object" && en.story_decompose === false) {
        en.story_decompose = true;
        changed = true;
    }
    return changed;
}

function migrateLegacyVoice(s) {
    const a = s?.assets;
    if (!a || !Array.isArray(a.images)) return false;
    a.audios = Array.isArray(a.audios) ? a.audios : [];
    let changed = false;
    for (const img of a.images) {
        const vp = String(img?.voice_path || "").trim();
        if (!vp) continue;
        const roleName = String(img?.name || "").trim();
        a.audios.push({
            type: "角色音色",
            letter: String(img.voice_letter || "").trim(),
            character: roleName,
            name: String(img.voice_name || "").trim() || (roleName ? `${roleName}音色` : ""),
            description: "",
            path: vp,
            enabled: img.enabled !== false,
        });
        changed = true;
        delete img.voice_path;
        delete img.voice_letter;
        delete img.voice_name;
        delete img.voice_name_at;
    }
    if (changed) {
        // 迁移后重新按序补编号：音频三个子分类共用「音频」编号空间，整体去重
        const used = new Set();
        a.audios.forEach((it) => {
            if (it?.enabled === false) return;
            const L = String(it?.letter || "").trim().toUpperCase();
            if (/^[A-Z]$/.test(L) && !used.has(L)) { it.letter = L; used.add(L); return; }
            it.letter = "";
        });
        a.audios.forEach((it) => {
            if (it?.enabled === false || String(it?.letter || "").trim()) return;
            const free = nextFreeSlotLetter("audio", a, it.type || "");
            if (free) it.letter = free;
        });
    }
    return changed;
}

async function saveManager(node, value) {
    // 短剧导演台已移除「无限时长衔接」：保存时顺手清掉历史配置里的 sample_decode.infinite
    if (value && value.sample_decode && node?.type === SHORTDRAMA_NODE_TYPE) {
        delete value.sample_decode.infinite;
    }
    // 🎭 人物替换：只存本节点用到的三组键（避免把导演台的全套配置塞进换人节点的工作流 JSON）
    if (value && node?.type === SWAP_NODE_TYPE) {
        value = pruneSwapSettings(value);
    }
    const w = node?.widgets?.find((x) => x.name === "manager_settings");
    if (w) setWidgetValue(w, JSON.stringify(value));
    try { node?.__jzlUpdateInfo?.(); } catch (_) {}  // 保存后实时同步节点「实时显示」
    try { node?.__jzlUpdateDisplay?.(); } catch (_) {}  // 生成详情（无限时长节点含「无限时长：开/关」）同步刷新
    return value;
}

// 🎭 人物替换的配置裁剪：只保留面板真正读写的键（与后端 _parse_swap_settings 一一对应）
function pruneSwapSettings(v) {
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
        gen_params: pick(v.gen_params, ["segment", "aspect_ratio", "megapixels",
            "scale_factor", "upscale_scale", "second_mode", "second_width", "second_height",
            "audio_mode", "max_segments"]),
        sample_decode: Object.assign(
            pick(sd, ["sample_mode", "sampler", "scheduler", "steps", "cfg", "denoise",
                "seed", "seed_mode", "decode_cleanup"]),
            {
                // 与短剧导演台同一套键：SelfLift 采样参数整组保留
                selflift: pick(sl, ["steps", "transition_step", "lowres_scale", "rho", "w_min", "w_max",
                    "upscaler_model", "upscaler_unload", "highres_tiling"]),
                seam: pick(sd.seam, ["enabled", "window", "settle", "ref_video_tail",
                    "guide_mode", "pin_second", "append_prev_video"]),
                second: pick(sec, ["enabled", "sampler", "scheduler", "sigmas_mode", "custom_sigmas",
                    "steps", "denoise", "precision", "upscaler_model", "device", "enable_chunking",
                    "tiling", "tiling_tiles", "tiling_overlap"]),
            }),
        save: pick(v.save, ["mode", "auto_merge_delete"]),
    };
}

async function chooseFile(kind) {
    // 浏览器原生文件选择 + multipart 上传到后端（与官方「加载图像」一致）。
    // 云机/无桌面环境也完全可用：文件从用户浏览器直传服务器，不依赖 tkinter 弹窗。
    const accept = { image: "image/*", video: "video/*", audio: "audio/*" }[kind] || "";
    const input = document.createElement("input");
    input.type = "file";
    input.accept = accept;
    input.style.display = "none";
    const done = (p) => { if (input.parentNode) input.parentNode.removeChild(input); return p; };
    const chosen = new Promise((resolve) => {
        input.addEventListener("change", async () => {
            const file = input.files && input.files[0];
            if (!file) { resolve(null); return; }
            const fd = new FormData();
            fd.append("file", file);
            fd.append("kind", kind);
            try {
                const resp = await api.fetchApi(UPLOAD_ENDPOINT, { method: "POST", body: fd });
                const data = await resp.json().catch(() => ({}));
                if (data?.path) { resolve(data.path); return; }
                notify(data?.error || "上传失败", "error");
                resolve(null);
            } catch (_) {
                notify("上传失败", "error");
                resolve(null);
            }
        });
        // 用户在文件对话框点「取消」时正常结束并清理 input（现代浏览器均支持 cancel 事件）
        input.addEventListener("cancel", () => resolve(null));
    });
    document.body.appendChild(input);
    input.click();
    return done(await chosen);
}

// 拖拽上传辅助：按文件类型归类 / multipart 上传 / 文件名→素材名
function detectAssetKindByFile(file) {
    try {
        const ext = String((file && file.name) || "").split(".").pop().toLowerCase();
        if (["png", "jpg", "jpeg", "webp", "gif", "bmp"].includes(ext)) return "image";
        if (["mp4", "mov", "webm", "avi", "mkv"].includes(ext)) return "video";
        if (["wav", "mp3", "flac", "ogg", "m4a", "aac", "opus", "wma"].includes(ext)) return "audio";
    } catch (_) {}
    return null;
}
function assetNameFromFilename(filename) {
    try { return String(filename || "").replace(/\.[^.]+$/, ""); } catch (_) { return ""; }
}
async function uploadFileObject(file, kind) {
    try {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("kind", kind);
        const resp = await api.fetchApi(UPLOAD_ENDPOINT, { method: "POST", body: fd });
        const data = await resp.json().catch(() => ({}));
        if (data && data.path) return data.path;
        notify(data?.error || "上传失败", "error");
    } catch (_) { notify("上传失败", "error"); }
    return null;
}

function el(tag, css, text) {
    const e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text !== undefined) e.textContent = text;
    return e;
}

// ── 文本复制 / 放大编辑（主提示词框与重拍编辑窗右上角悬浮按钮）──
// ── 编辑文本 Ctrl/⌘+滚轮缩放（全局共享字号记忆）──────────────
// 放大编辑弹窗 + 各二级弹窗(footer 字号 A−/A+)共用同一份字号：任一弹窗调整 → localStorage 记忆 → 其它弹窗/下次打开保持一致。
const JZL_ZOOM_MIN = 8, JZL_ZOOM_MAX = 40;
let __jzlZoomSize = null;  // null=用户从未调过 → 不干预各编辑区原有字号
function jzlZoomGet() {
    if (__jzlZoomSize == null) {
        try { const v = parseInt(localStorage.getItem("jzl_text_zoom") || "", 10); if (Number.isFinite(v)) __jzlZoomSize = v; } catch (_) {}
    }
    return __jzlZoomSize;  // null = 从未设置
}
function jzlZoomDisplay() { return jzlZoomGet() == null ? 13 : jzlZoomGet(); }
function jzlZoomSet(v) {
    __jzlZoomSize = Math.max(JZL_ZOOM_MIN, Math.min(JZL_ZOOM_MAX, Math.round(v)));
    try { localStorage.setItem("jzl_text_zoom", String(__jzlZoomSize)); } catch (_) {}
}
function jzlZoomBump(delta) { jzlZoomSet((jzlZoomGet() == null ? 13 : jzlZoomGet()) + delta); }
function jzlZoomApplyTo(el) {
    // 仅当用户设置过字号才覆盖（未设置时保留各编辑区默认字号，避免打开就改变原样）
    if (!el) return;
    if (jzlZoomGet() == null) return;
    const px = jzlZoomGet() + "px";
    if (el.tagName === "TEXTAREA" || el.isContentEditable) el.style.fontSize = px;
}
function jzlZoomApplyContainer(container) {
    if (!container || !container.querySelectorAll) return;
    container.querySelectorAll("textarea, [contenteditable='true']").forEach((el) => jzlZoomApplyTo(el));
}
function jzlSyncZoomVal(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll(".jzl-zoom-val").forEach((el) => { el.textContent = String(jzlZoomDisplay()); });
}
// 容器级 Ctrl/⌘+滚轮委托：光标在编辑元素(或其子孙)上时才缩放，返回是否消费
function jzlBindWheelZoom(container, onZoom) {
    if (!container) return;
    container.addEventListener("wheel", (e) => {
        if (!(e.ctrlKey || e.metaKey)) return;
        if (!e.target || !e.target.closest) return;
        if (!e.target.closest("textarea, [contenteditable='true'], .jzl-zoomable")) return;
        e.preventDefault();
        e.stopPropagation();
        const delta = e.deltaY < 0 ? 1 : -1;
        jzlZoomBump(delta);
        jzlZoomApplyContainer(container);
        jzlSyncZoomVal(container);
        onZoom && onZoom(jzlZoomDisplay());
    }, { passive: false });
}
// 共享字号行控件（A− / 当前值 / A+）：点击步进并应用到 container 内全部文本编辑区
function jzlBuildZoomRow(container) {
    const row = el("span", "display:inline-flex;align-items:center;gap:4px;margin-left:2px;user-select:none;");
    const minus = el("button", "width:22px;height:22px;line-height:1;border-radius:4px;border:1px solid #5b9bd5;background:#2a4a6a;color:#cfe3f7;font-size:11px;cursor:pointer;padding:0;", "A−");
    const val = el("span", "min-width:26px;text-align:center;font-size:12px;color:#e8a87c;font-variant-numeric:tabular-nums;", String(jzlZoomDisplay()));
    val.classList.add("jzl-zoom-val");
    const plus = el("button", "width:22px;height:22px;line-height:1;border-radius:4px;border:1px solid #5b9bd5;background:#2a4a6a;color:#cfe3f7;font-size:11px;cursor:pointer;padding:0;", "A+");
    minus.title = "缩小编辑字号（Ctrl+滚轮 同样可调）";
    plus.title = "放大编辑字号（Ctrl+滚轮 同样可调）";
    const step = (d) => {
        minus.disabled = plus.disabled = true;
        try {
            jzlZoomBump(d);
            jzlZoomApplyContainer(container);
            jzlSyncZoomVal(container);
        } finally { minus.disabled = plus.disabled = false; }
    };
    minus.addEventListener("mousedown", (e) => e.stopPropagation());
    plus.addEventListener("mousedown", (e) => e.stopPropagation());
    minus.addEventListener("click", (e) => { e.stopPropagation(); step(-1); });
    plus.addEventListener("click", (e) => { e.stopPropagation(); step(1); });
    row.append(minus, val, plus);
    return row;
}
// ── 界面整体缩放（弹窗本体比例缩放，独立于网页/画布缩放；字号 A± 与之叠加）──
const JZL_UI_ZOOM_MIN = 0.5, JZL_UI_ZOOM_MAX = 3.0, JZL_UI_ZOOM_STEP = 0.05;  // 每次 ±5%
let __jzlUIZoom = null;  // null = 未设置过 → 弹窗保持 100%
function jzlUIGet() {
    if (__jzlUIZoom == null) {
        try { const v = parseFloat(localStorage.getItem("jzl_ui_zoom") || ""); if (Number.isFinite(v) && v > 0) __jzlUIZoom = v; } catch (_) {}
    }
    return __jzlUIZoom == null ? 1 : __jzlUIZoom;
}
function jzlUISet(v) {
    __jzlUIZoom = Math.max(JZL_UI_ZOOM_MIN, Math.min(JZL_UI_ZOOM_MAX, Math.round(v / JZL_UI_ZOOM_STEP) * JZL_UI_ZOOM_STEP));
    __jzlUIZoom = Math.round(__jzlUIZoom * 100) / 100;
    try { localStorage.setItem("jzl_ui_zoom", String(__jzlUIZoom)); } catch (_) {}
}
function jzlUIStep(dir) {
    const cur = jzlUIGet();
    jzlUISet(cur + dir * JZL_UI_ZOOM_STEP);
    return jzlUIGet();
}
function jzlUIApply(el) {
    if (!el) return;
    const z = jzlUIGet();
    el.dataset.jzlUiTarget = "1";  // 供窗口 resize 时重算高度上限
    // 记录元素【原始】高度上限（仅首次）：'none'/空 → 视为无上限
    if (el.dataset.jzlUiMaxH == null) {
        const cs = parseFloat(getComputedStyle(el).maxHeight);
        el.dataset.jzlUiMaxH = Number.isFinite(cs) ? String(cs) : "none";
    }
    el.style.zoom = String(z);  // zoom 同时影响布局尺寸（Chromium），弹窗整体按比例缩放
    // 高度封顶：zoom 会把 max-height(vh/px) 一并放大 → 弹窗上下溢出屏幕。
    // 上限 = min(原始上限, 视口高度×0.92 / zoom)，使【视觉高度】恒定≤视口的 92%：
    // 放大到上限后高度不再增长、只横向变宽（与「提示词放大编辑」一致）。
    // 注意必须用 min() 而不是直接清空——清空会把内联的 max-height（如 dialog 的 90vh）一起丢掉，
    // 导致 100%（zoom=1）时反而没有上限、素材多就撑出屏幕。
    const _orig = parseFloat(el.dataset.jzlUiMaxH);
    const _cap = Math.round((window.innerHeight * 0.92) / z);
    el.style.maxHeight = (Number.isFinite(_orig) ? Math.min(_orig, _cap) : _cap) + "px";
    // 固定高度同理：vh 形式的 height 会随 zoom 一起放大，按 92/z vh 重新封顶（保持固定高度不跳动）
    if (el.dataset.jzlUiOrigH == null) el.dataset.jzlUiOrigH = el.style.height || "";
    const _oh = String(el.dataset.jzlUiOrigH).trim();
    const _vh = /^([\d.]+)vh$/i.exec(_oh);
    if (_vh) {
        el.style.height = Math.min(parseFloat(_vh[1]), 92 / z).toFixed(2) + "vh";
    }
}
// 窗口尺寸变化时重算已打开弹窗的高度上限（保证视觉高度始终不超过视口）
window.addEventListener("resize", () => {
    document.querySelectorAll("[data-jzl-ui-target]").forEach((el) => { try { jzlUIApply(el); } catch (_) {} });
});
// 构建「界面缩放」行：− / 百分比 / ＋，作用于 target（弹窗本体），与字号 A± 独立记忆
function jzlBuildUIRow(target) {
    const row = el("span", "display:inline-flex;align-items:center;gap:4px;margin-left:2px;user-select:none;");
    const lab = el("span", "font-size:11px;color:#888;white-space:nowrap;", "界面");
    const minus = el("button", "width:22px;height:22px;line-height:1;border-radius:4px;border:1px solid #8a6d3b;background:#5a4a2a;color:#ffd98a;font-size:13px;cursor:pointer;padding:0;", "−");
    const val = el("span", "min-width:40px;text-align:center;font-size:12px;color:#ffd98a;font-variant-numeric:tabular-nums;", Math.round(jzlUIGet() * 100) + "%");
    val.classList.add("jzl-ui-zoom-val");
    const plus = el("button", "width:22px;height:22px;line-height:1;border-radius:4px;border:1px solid #8a6d3b;background:#5a4a2a;color:#ffd98a;font-size:13px;cursor:pointer;padding:0;", "+");
    minus.title = "缩小整个界面（不缩放网页/画布）";
    plus.title = "放大整个界面（不缩放网页/画布）";
    const step = (d) => {
        minus.disabled = plus.disabled = true;
        try {
            jzlUIStep(d);
            jzlUIApply(target);
            target.querySelectorAll && target.querySelectorAll(".jzl-ui-zoom-val").forEach((el) => { el.textContent = Math.round(jzlUIGet() * 100) + "%"; });
        } finally { minus.disabled = plus.disabled = false; }
    };
    minus.addEventListener("mousedown", (e) => e.stopPropagation());
    plus.addEventListener("mousedown", (e) => e.stopPropagation());
    minus.addEventListener("click", (e) => { e.stopPropagation(); step(-1); });
    plus.addEventListener("click", (e) => { e.stopPropagation(); step(1); });
    row.append(lab, minus, val, plus);
    return row;
}
// 弹窗本体绑定 Ctrl/⌘+滚轮 = 界面缩放（抢在浏览器整页缩放前 preventDefault）
function jzlBindUIWheel(target) {
    if (!target) return;
    target.addEventListener("wheel", (e) => {
        if (!(e.ctrlKey || e.metaKey)) return;
        e.preventDefault();
        e.stopPropagation();
        jzlUIStep(e.deltaY < 0 ? 1 : -1);
        jzlUIApply(target);
        target.querySelectorAll && target.querySelectorAll(".jzl-ui-zoom-val").forEach((el) => { el.textContent = Math.round(jzlUIGet() * 100) + "%"; });
    }, { passive: false });
}
function copyTextToClipboard(text, label, successMsg) {
    if (!text) { notify("没有可复制的内容", "warning"); return; }
    const done = () => notify(successMsg || `已复制${label ? ` ${label}` : ""}`, "success");
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(() => { fallbackCopyText(text); done(); });
        } else {
            fallbackCopyText(text); done();
        }
    } catch (_) { fallbackCopyText(text); done(); }
}
function fallbackCopyText(text) {
    try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;top:-999px;opacity:0;";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
    } catch (_) {}
}
// ── 剧本分段辅助（编辑界面配色 + 复制单镜头）──
// 只认标准分段块 [SHOT_START]…[SHOT_END]；返回 [{start,end,text,index}]（index 从 0 开始）。
function jzlShotSegments(text) {
    const src = String(text == null ? "" : text);
    const re = /\[SHOT_START\][\s\S]*?\[SHOT_END\]/g;
    const out = [];
    let m;
    while ((m = re.exec(src))) out.push({ start: m.index, end: m.index + m[0].length, text: m[0], index: out.length });
    return out;
}

// 分段标签：`Video_003 · 荒漠峡谷对峙`（取不到标题就退回 Video 号或首行）
function jzlSegmentLabel(segText) {
    const t = String(segText || "");
    const vid = (t.match(/###\s*(Video_\d+)/i) || [])[1] || "";
    let title = (t.match(/\*\*(?:标题|Title)\*\*\s*[:：]\s*(.+)/) || [])[1] || "";
    title = title.trim().replace(/\s+/g, " ");
    if (vid && title) return `${vid} · ${title}`;
    if (vid) return vid;
    if (title) return title;
    const firstLine = t.split("\n").map((s) => s.trim()).find((s) => s && !/^\[SHOT_START\]$/i.test(s)) || "";
    return firstLine.slice(0, 40);
}

// 用 <span> 给每个分段块上底色（单/双数交替），保留纯文本可编辑性：
// getPromptText 对 SPAN 不加换行，所以存回去与原文逐字一致（不像 DIV 会多出 \n）。
function jzlRenderScriptBands(editor, text) {
    const src = String(text == null ? "" : text);
    editor.innerHTML = "";
    const segs = jzlShotSegments(src);
    if (!segs.length) { editor.appendChild(document.createTextNode(src)); return 0; }
    let last = 0;
    segs.forEach((sg) => {
        if (sg.start > last) editor.appendChild(document.createTextNode(src.slice(last, sg.start)));
        const span = document.createElement("span");
        span.className = "jzl-shot-band";
        const odd = sg.index % 2 === 0;   // 第 1、3、5… 段
        span.style.background = odd ? "rgba(91,155,213,.22)" : "rgba(207,154,74,.20)";
        span.style.boxShadow = odd ? "inset 3px 0 0 #5b9bd5" : "inset 3px 0 0 #c99a4a";
        span.style.borderRadius = "3px";
        span.style.whiteSpace = "pre-wrap";
        span.textContent = sg.text;
        editor.appendChild(span);
        last = sg.end;
    });
    if (last < src.length) editor.appendChild(document.createTextNode(src.slice(last)));
    return segs.length;
}

// 「📋 复制单镜头」：弹出段列表 → 点第几个就复制那一段（含 [SHOT_START]/[SHOT_END]），
// 可直接粘到「🪄 穿透生成模式」只重拍这一段。
function jzlOpenShotCopyPicker(anchorBtn, getCurrentText) {
    document.querySelectorAll(".jzl-shotpick").forEach((m) => m.remove());
    const segs = jzlShotSegments(getCurrentText());
    if (!segs.length) { notify("当前内容里没有 [SHOT_START]…[SHOT_END] 分段", "warning"); return; }
    const pop = el("div", "position:fixed;z-index:100010;background:#1e1e20;border:1px solid #5b9bd5;border-radius:8px;box-shadow:0 10px 34px rgba(0,0,0,.65);padding:8px;min-width:300px;max-width:min(560px,90vw);max-height:62vh;overflow:auto;");
    pop.classList.add("jzl-shotpick");
    pop.appendChild(el("div", "font-size:12px;color:#9fd6a4;margin:2px 2px 8px;", "选择要复制的镜头（复制后可粘到「🪄 穿透生成模式」单独重拍）"));
    segs.forEach((sg) => {
        const row = el("div", "display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:5px;cursor:pointer;font-size:12px;color:#ddd;");
        row.append(el("span", "flex:0 0 auto;color:#ffd98a;font-weight:600;", `第 ${sg.index + 1} 段`),
            el("span", "flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", jzlSegmentLabel(sg.text)));
        row.addEventListener("mouseenter", () => { row.style.background = "#2c2c31"; });
        row.addEventListener("mouseleave", () => { row.style.background = ""; });
        row.addEventListener("click", () => {
            copyTextToClipboard(sg.text, `第 ${sg.index + 1} 段`,
                `✅ 已复制第 ${sg.index + 1} 段（可粘到「🪄 穿透生成模式」单独重拍）`);
            pop.remove();
        });
        pop.appendChild(row);
    });
    document.body.appendChild(pop);
    try {
        const r = anchorBtn.getBoundingClientRect();
        pop.style.left = Math.max(8, Math.min(window.innerWidth - pop.offsetWidth - 8, r.left)) + "px";
        pop.style.top = Math.max(8, Math.min(window.innerHeight - pop.offsetHeight - 8, r.bottom + 6)) + "px";
    } catch (_) { pop.style.left = "80px"; pop.style.top = "80px"; }
    const off = (e) => {
        if (!pop.contains(e.target) && e.target !== anchorBtn) {
            pop.remove();
            document.removeEventListener("mousedown", off, true);
        }
    };
    setTimeout(() => document.addEventListener("mousedown", off, true), 0);
}

function openTextZoomEditor(title, getText, onSave, node) {
    document.querySelectorAll(".jzl-zoom-editor").forEach((m) => m.remove());
    const modal = el("div", "position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:99999;display:flex;align-items:center;justify-content:center;");
    modal.classList.add("jzl-zoom-editor");
    const box = el("div", "width:84%;max-width:1040px;height:92%;max-height:920px;background:var(--comfy-menu-bg,#242424);border:1px solid var(--border-color,#555);border-radius:8px;display:flex;flex-direction:column;padding:12px;box-sizing:border-box;box-shadow:0 8px 40px rgba(0,0,0,.5);");
    const head = el("div", "display:flex;align-items:center;gap:8px;margin-bottom:10px;flex:0 0 auto;flex-wrap:wrap;");
    head.appendChild(el("span", "font-size:14px;font-weight:600;color:#eee;flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", title));
    // 主体：左 = contenteditable 编辑器，右 = 资产显示窗（点击插入 @）
    const body = el("div", "flex:1 1 auto;display:flex;min-height:0;");
    // 内容区：contenteditable（支持 @token 富文本显示）
    const editor = document.createElement("div");
    editor.contentEditable = "true";
    editor.spellcheck = false;
    editor.style.cssText = "flex:1 1 auto;min-width:0;width:100%;box-sizing:border-box;background:#161616;color:#ddd;border:1px solid #444;border-radius:5px;padding:10px 12px;font-size:13px;overflow-y:auto;outline:none;white-space:pre-wrap;word-break:break-word;line-height:1.6;";
    editor.addEventListener("mousedown", (e) => e.stopPropagation());
    // 初始显示模式：跟随节点参考元素模式（ref=显示引用着色；text=纯文本）
    let alignMode = node ? (node.__jzlAlignMode === "ref" ? "ref" : "text") : "text";
    // 含 [SHOT_START] 分段的内容默认开启「分段配色」（单/双数段不同底色）；无分段时按钮不出现
    const _initText = String(getText() || "");
    const hasShots = jzlShotSegments(_initText).length > 0;
    // 剧本内容优先用纯文本 + 分段配色打开（可随时点「🎯 显示引用」切回素材名着色）
    if (hasShots) alignMode = "text";
    let bandOn = hasShots;
    const renderPlain = () => {
        if (bandOn && jzlRenderScriptBands(editor, getText())) return;
        editor.innerHTML = "";
        editor.appendChild(document.createTextNode(getText() || ""));
    };
    const renderInitial = () => {
        if (node && alignMode === "ref") renderPromptFromText(editor, getText(), node);
        else renderPlain();
    };
    renderInitial();
    // 参考元素切换（在「复制」前面）：纯文本 / 显示引用
    const aBtn = el("button", "flex:0 0 auto;height:28px;padding:0 10px;border-radius:5px;border:1px solid #5b9bd5;background:#2a4a6a;color:#cfe3f7;font-size:12px;cursor:pointer;white-space:nowrap;",
        node && alignMode === "ref" ? "🎯 纯文本" : "🎯 显示引用");
    aBtn.title = "参考元素切换（纯文本 / 显示引用）";
    aBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    aBtn.addEventListener("click", () => {
        if (alignMode === "text") {
            renderPromptFromText(editor, getPromptText(editor), node);
            alignMode = "ref";
            aBtn.textContent = "🎯 纯文本";
            notify("✅ 已切换为显示引用（素材名着色）", "success");
        } else {
            alignMode = "text";
            renderPlain();
            aBtn.textContent = "🎯 显示引用";
            notify("✅ 已切换为纯文本", "success");
        }
    });
    // 🎨 分段配色开关：单/双数 [SHOT_START] 段落不同底色，方便定位编辑
    const bBtn = el("button", "flex:0 0 auto;height:28px;padding:0 10px;border-radius:5px;border:1px solid #5b9bd5;background:" + (bandOn ? "#2f5a3a" : "#3a3a3a") + ";color:#cfe3f7;font-size:12px;cursor:pointer;white-space:nowrap;",
        bandOn ? "🎨 分段配色：开" : "🎨 分段配色：关");
    bBtn.title = "给每个 [SHOT_START]…[SHOT_END] 段落交替上底色（单/双数不同色），便于定位与编辑";
    bBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    bBtn.addEventListener("click", () => {
        bandOn = !bandOn;
        if (!bandOn && alignMode === "ref") {
            alignMode = "text";
            aBtn.textContent = "🎯 显示引用";
        }
        renderPlain();
        bBtn.textContent = bandOn ? "🎨 分段配色：开" : "🎨 分段配色：关";
        bBtn.style.background = bandOn ? "#2f5a3a" : "#3a3a3a";
        if (bandOn && !jzlShotSegments(getPromptText(editor)).length) {
            notify("当前内容没有 [SHOT_START] 分段，配色不生效", "warning");
        }
    });
    const cBtn = el("button", "flex:0 0 auto;height:28px;padding:0 12px;border-radius:5px;border:1px solid #5b9bd5;background:#3a3a3a;color:#eee;font-size:12px;cursor:pointer;", "📋 复制");
    cBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    cBtn.addEventListener("click", () => copyTextToClipboard(getPromptText(editor), ""));
    // 📋 复制单镜头：选第几段就复制那一段（含 [SHOT_START]/[SHOT_END]）→ 粘到穿透模式单独重拍
    const oneBtn = el("button", "flex:0 0 auto;height:28px;padding:0 10px;border-radius:5px;border:1px solid #c99a4a;background:#4a3a20;color:#ffe6b8;font-size:12px;cursor:pointer;white-space:nowrap;", "📋 复制单镜头");
    oneBtn.title = "复制单个分段（含 [SHOT_START]…[SHOT_END]）→ 粘到「🪄 穿透生成模式」只重拍这一段";
    oneBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    oneBtn.addEventListener("click", () => jzlOpenShotCopyPicker(oneBtn, () => getPromptText(editor)));
    const sBtn = el("button", "flex:0 0 auto;height:28px;padding:0 12px;border-radius:5px;border:1px solid #5ecf8a;background:#2d5a3a;color:#fff;font-size:12px;cursor:pointer;font-weight:600;", "💾 保存");
    const xBtn = el("button", "flex:0 0 auto;height:28px;padding:0 12px;border-radius:5px;border:1px solid #666;background:#3a3a3a;color:#ccc;font-size:12px;cursor:pointer;", "取消");
    // 放大编辑弹窗支持全局共享字号（A−/A+）+ 界面整体缩放（−/＋）：字号改文字、界面改整个弹窗
    // 顺序要求：字号放大 / 界面放大 调节按钮 排在「显示引用」按钮【前面】
    const zoomRow = jzlBuildZoomRow(box);
    zoomRow.title = "调整编辑字号（全局共享，各编辑界面一致）";
    const uiRow = jzlBuildUIRow(box);
    uiRow.title = "调整整个弹窗界面大小（不缩放网页/画布）";
    head.append(zoomRow, uiRow);
    if (node) head.append(aBtn);
    if (hasShots) head.append(bBtn, oneBtn);
    head.append(cBtn, sBtn, xBtn);
    // 资产显示窗（点击素材插入 @ 引用到放大编辑器；仅当节点存在时显示）
    if (node) {
        editor.__node = node;  // insertAssetToken 内部会自动激活主节点「显示引用」模式
        const zoomCaret = { value: null };  // 放大编辑器独立光标记忆（与主框全局 promptCaretPos 隔离）
        const syncZoomCaret = () => { zoomCaret.value = caretOffset(editor); rememberCaret(editor); };
        editor.addEventListener("input", syncZoomCaret);   // 中文输入法(IME)只触发 input → 必须监听
        editor.addEventListener("keyup", syncZoomCaret);
        editor.addEventListener("click", syncZoomCaret);
        // 回车：统一插入 \n 文本，避免 Chrome 生成 <div><br></div> 干扰（根本修复）
        // 与主提示词框同规则：选区异常时兜底插到末尾，保证「按一次就换行」（旧写法会吞掉第一次回车）
        editor.addEventListener("keydown", (e) => {
            if (e.key !== "Enter" || e.shiftKey) return;
            if (e.isComposing || e.keyCode === 229) return;   // 输入法组合中：交给输入法
            e.preventDefault();
            e.stopPropagation();
            const _sel = window.getSelection();
            let r0 = null;
            if (_sel && _sel.rangeCount && editor.contains(_sel.anchorNode)) {
                r0 = _sel.getRangeAt(0);
            } else {
                r0 = document.createRange();
                r0.selectNodeContents(editor);
                r0.collapse(false);
            }
            r0.deleteContents();
            const tn = document.createTextNode("\n");
            r0.insertNode(tn);
            const r2 = document.createRange();
            r2.setStartAfter(tn);
            r2.collapse(true);
            if (_sel) { _sel.removeAllRanges(); _sel.addRange(r2); }
            editor.dispatchEvent(new Event("input", { bubbles: true }));
        });
        // 粘贴：统一为含 \n 的 textNode（不生成 br/div）
        editor.addEventListener("paste", (e) => {
            e.stopPropagation();
            e.preventDefault();
            const _t = (e.clipboardData || window.clipboardData)?.getData("text/plain") || "";
            if (!_t) return;
            const _sel = window.getSelection();
            let _range = null;
            if (_sel && _sel.rangeCount && editor.contains(_sel.anchorNode)) _range = _sel.getRangeAt(0);
            if (!_range) { _range = document.createRange(); _range.selectNodeContents(editor); _range.collapse(false); }
            _range.deleteContents();
            _range.insertNode(document.createTextNode(String(_t).replace(/\r\n/g, "\n")));
            _range.collapse(false);
            _sel.removeAllRanges();
            _sel.addRange(_range);
            editor.dispatchEvent(new Event("input", { bubbles: true }));
        });
        const assetCol = el("div", "flex:0 0 284px;display:flex;flex-direction:column;margin-left:10px;min-height:0;");
        const assetTitle = el("div", "font-size:12px;color:#bbb;margin-bottom:6px;flex:0 0 auto;", "📁 资产显示窗（点击插入）");
        // 素材多时出现滚动条会占 ~17px 宽度，把 3 列挤成 2 列：scrollbar-gutter:stable 让滚动条占固定 gutter 不压缩内容宽度，
        // 配 284px 列宽（284-2边框-8内边距-17滚动条=257px ≥ 3列所需 246px），有无滚动条都稳定 3 列。
        const assetScroll = el("div", "flex:1 1 auto;overflow-y:auto;min-height:0;scrollbar-gutter:stable;border:1px solid #333;border-radius:5px;background:#1a1a1a;padding:4px;box-sizing:border-box;");
        const assetBox = el("div", "min-height:30px;");
        assetScroll.appendChild(assetBox);
        assetCol.append(assetTitle, assetScroll);
        body.append(editor, assetCol);
        renderAssetWindow(assetBox, editor, node, () => {
            // 点击资产插入后 → 放大编辑器同步切到「显示引用」（与主节点行为一致）
            alignMode = "ref";
            aBtn.textContent = "🎯 纯文本";
        }, (ed, item) => insertAssetToken(ed, item, zoomCaret));
    } else {
        body.appendChild(editor);
    }
    const close = () => modal.remove();
    sBtn.addEventListener("click", () => {
        try { onSave(getPromptText(editor)); } catch (e) { notify("保存失败：" + e.message, "error"); return; }
        close();
        notify("已保存", "success");
    });
    xBtn.addEventListener("click", close);
    modal.addEventListener("mousedown", (e) => { if (e.target === modal) close(); });
    modal.style.overflow = "auto";  // 界面放大后超高可滚动查看
    box.append(head, body);
    modal.appendChild(box);
    document.body.appendChild(modal);
    // 打开时应用已记忆的全局字号 + 界面缩放（未设置过则保持默认）
    jzlZoomApplyTo(editor);
    jzlUIApply(box);
    jzlBindUIWheel(box);   // 弹窗内 Ctrl/⌘+滚轮 = 界面缩放（抢在浏览器整页缩放前）
    jzlBindWheelZoom(box); // 弹窗内编辑文本上 Ctrl/⌘+滚轮 = 字号缩放（与界面缩放可叠加）
    setTimeout(() => editor.focus(), 50);
}

function makeSectionTitle(text) {
    return el("div", "margin:14px 0 4px;font-size:14px;font-weight:600;color:var(--fg-color,#eee);border-bottom:1px solid var(--border-color,#444);padding-bottom:4px;", text);
}

function makeCountRow(labelText, kind, value, onChange) {
    const row = el("div", "display:flex;align-items:center;gap:6px;");
    row.append(el("span", "font-size:12px;color:var(--fg-color,#ddd);white-space:nowrap;", labelText));
    const dec = el("button", "width:26px;height:26px;background:var(--comfy-input-bg,#2a2a2a);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#555);border-radius:5px;font-size:12px;line-height:1;cursor:pointer;", "◀");
    dec.title = "减少数量";
    const inp = el("input", "width:44px;text-align:center;background:var(--comfy-input-bg,#1d1d1d);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);border-radius:4px;padding:4px 0;font-size:13px;");
    inp.type = "number";
    inp.min = "0";
    inp.max = "64";
    inp.step = "1";
    inp.value = value;
    inp.className = "jzl-count-input";
    const inc = el("button", "width:26px;height:26px;background:var(--comfy-input-bg,#2a2a2a);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#555);border-radius:5px;font-size:12px;line-height:1;cursor:pointer;", "▶");
    inc.title = "增加数量";
    const apply = (n) => {
        n = Math.max(0, Math.min(64, Math.floor(n)));
        inp.value = n;
        onChange(n);
    };
    dec.addEventListener("click", () => apply((parseInt(inp.value, 10) || 0) - 1));
    inc.addEventListener("click", () => apply((parseInt(inp.value, 10) || 0) + 1));
    inp.addEventListener("change", () => apply(parseInt(inp.value, 10) || 0));
    row.append(dec, inp, inc);
    return row;
}

function showFullImage(path) {
    // 弹窗显示原图（非缩略图）
    const overlay = el("div", "position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:10002;display:flex;align-items:center;justify-content:center;cursor:zoom-out;");
    const img = document.createElement("img");
    img.src = api.apiURL(`/jzl/asset_full?path=${encodeURIComponent(path)}`);
    img.style.cssText = "max-width:94vw;max-height:94vh;object-fit:contain;border-radius:4px;box-shadow:0 10px 40px rgba(0,0,0,0.6);";
    img.onerror = () => { img.remove(); overlay.append(el("div", "color:#999;font-size:14px;", "原图加载失败")); };
    overlay.appendChild(img);
    overlay.addEventListener("click", () => overlay.remove());
    document.body.appendChild(overlay);
}

function showVideoPreview(path) {
    // 弹窗预览视频（浏览器原生控制条，支持播放/暂停/全屏；点空白处关闭）
    const overlay = el("div", "position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:10002;display:flex;align-items:center;justify-content:center;cursor:zoom-out;");
    const v = document.createElement("video");
    v.src = api.apiURL(`/jzl/video_preview?path=${encodeURIComponent(path)}`);
    v.controls = true;
    v.autoplay = true;
    v.playsInline = true;
    v.style.cssText = "max-width:94vw;max-height:94vh;object-fit:contain;border-radius:4px;background:#000;outline:none;box-shadow:0 10px 40px rgba(0,0,0,0.6);";
    v.onerror = () => { v.remove(); overlay.append(el("div", "color:#999;font-size:14px;", "视频加载失败")); };
    overlay.appendChild(v);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
}

// ── 编号的唯一性范围 = **槽位类型**，不是「条目类型」 ──────────────
// 槽位名（视频A / 音频A）由槽位类型决定：音频的「角色音色 / 背景配乐 / 念白」都映射到「音频」，
// 所以它们的编号必须整体互斥，否则会出现两个「音频A」（与后端 _build_asset_intro 的计数器不一致）。
function fullAssetList(kind, assets) {
    const key = kind === "image" ? "images" : (kind === "video" ? "videos" : "audios");
    return (assets?.[key]) || [];
}

function slotTypePeers(kind, assets, item) {
    const st = slotTypeOf(kind, item?.type || "");
    return fullAssetList(kind, assets).filter(
        (it) => it !== item && it?.enabled !== false && slotTypeOf(kind, it?.type || "") === st
    );
}

function nextFreeSlotLetter(kind, assets, type) {
    const used = new Set();
    const st = slotTypeOf(kind, type || "");
    fullAssetList(kind, assets).forEach((it) => {
        if (it?.enabled === false) return;
        if (slotTypeOf(kind, it?.type || "") !== st) return;
        const L = String(it?.letter || "").trim().toUpperCase();
        if (/^[A-Z]$/.test(L)) used.add(L);
    });
    for (let i = 0; i < 26; i++) {
        const L = String.fromCharCode(65 + i);
        if (!used.has(L)) return L;
    }
    return "";
}

// ⭐ 音频「角色音色」子分类：绑定角色名（图片里的角色），删除/改名时前端联动。
function audioVoiceLabel(item) {
    const ch = String(item?.character || "").trim();
    if (ch) return `${ch}音色`;
    return String(item?.name || "").trim();
}

function roleNameList(assets) {
    return (assets?.images || [])
        .filter((it) => it?.enabled !== false && String(it?.type || "") === "角色")
        .map((it) => String(it?.name || "").trim())
        .filter(Boolean);
}

// 角色名变化（改名/删除）后，重算所有「音频 · 角色音色」的「指定角色名」下拉选项。
// 这些下拉在别的分区里，无法靠本分区的重新渲染刷新，故用 DOM 标记统一触发。
function refreshRoleSels(root) {
    const r = (root && root.querySelectorAll) ? root : document;
    r.querySelectorAll("select[data-jzl-role-sync]").forEach((sel) => {
        try { sel.__jzlRefresh?.(); } catch (_) {}
    });
}

function makeAssetCard(kind, index, item, list, onEdit, onDelete, isLast, refreshAll, opts) {
    const refreshAllFn = refreshAll || (() => {});
    const isImage = kind === "image";
    // 图片在「角色/场景/道具」分区里渲染时，类型已由分区决定 → 不再重复显示类型下拉（仅「其他」分区保留，便于改回三类）
    const showType = !(opts && opts.showType === false);
    const isRole = isImage && String(item?.type || "") === "角色";
    const row = el("div", "display:flex;align-items:stretch;gap:8px;margin:5px 0;padding:6px 8px;background:var(--comfy-menu-bg,#232323);border:1px solid var(--border-color,#3a3a3a);border-radius:6px;");

    // ── 预览区（固定在每张卡片最前面；图片放大以便一眼看清角色/场景/道具）──
    const thumbPx = isImage ? 78 : 56;
    const thumb = el("div", `flex:0 0 ${thumbPx}px;width:${thumbPx}px;height:${thumbPx}px;border-radius:5px;border:1px solid var(--border-color,#444);background:#000;display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:22px;`);
    let imgEl = null;
    let refreshAudio = null;  // 音频试听刷新（kind==="audio" 时赋值）
    let refreshVideo = null;  // 视频已选反馈（kind==="video" 时赋值）
    const refreshThumb = () => {
        if (kind !== "image" || !imgEl) return;
        const p = (item.path || "").trim();
        if (p) {
            imgEl.src = api.apiURL(`/jzl/asset_preview?path=${encodeURIComponent(p)}`);
            imgEl.style.display = "";
        } else {
            imgEl.style.display = "none";  // 空槽口纯黑
        }
    };
    if (kind === "image") {
        imgEl = el("img", "width:100%;height:100%;object-fit:cover;");
        imgEl.alt = "";
        imgEl.draggable = false;   // 禁止缩略图原生拖拽，避免与手动排序拖拽冲突
        imgEl.onerror = () => { imgEl.style.display = "none"; };
        thumb.append(imgEl);
        thumb.title = "点击查看原图";
        thumb.style.cursor = "pointer";
        thumb.addEventListener("click", () => {
            const p = (item.path || "").trim();
            if (p) showFullImage(p);
        });
    } else if (kind === "audio") {
        // 音频：空槽黑底🎧；已选蓝底 + ▶/⏸ 试听
        const playBtn = el("button", "width:100%;height:100%;border:none;background:transparent;color:#fff;font-size:20px;cursor:pointer;line-height:1;padding:0;", "🎧");
        thumb.appendChild(playBtn);
        refreshAudio = () => {
            const p = (item.path || "").trim();
            if (p) {
                thumb.style.background = "#2d5a88";  // 蓝色：已选
                playBtn.textContent = "▶";
                playBtn.title = "试听（点击播放/暂停）";
            } else {
                thumb.style.background = "#000";     // 黑色：空槽
                playBtn.textContent = "🎧";
                playBtn.title = "空槽位（未选择音频）";
            }
        };
        let audioEl = null;
        playBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            const p = (item.path || "").trim();
            if (!p) return;
            if (audioEl && !audioEl.paused) {
                audioEl.pause();
                playBtn.textContent = "▶";
                return;
            }
            if (!audioEl) {
                audioEl = new Audio();
                audioEl.onended = () => { playBtn.textContent = "▶"; };
                audioEl.onerror = () => { playBtn.textContent = "▶"; };
            }
            // 每次点击都把 src 同步到当前路径：上传/替换后立即生效（避免播旧音频，需保存重开才能播）
            const wantSrc = api.apiURL(`${AUDIO_PREVIEW_ENDPOINT}?path=${encodeURIComponent(p)}`);
            if (audioEl.src !== wantSrc) audioEl.src = wantSrc;
            audioEl.play().then(() => { playBtn.textContent = "⏸"; }).catch(() => { playBtn.textContent = "▶"; });
        });
        thumb.title = "音频试听";
        refreshAudio();
    } else {
        // 视频：空槽黑底🎬；已选蓝底 + 点击缩略图弹窗预览播放
        refreshVideo = () => {
            const p = (item.path || "").trim();
            if (p) {
                thumb.style.background = "#2d5a88";
                thumb.style.cursor = "pointer";
                thumb.title = "已选择视频（点击预览播放）：" + p;
            } else {
                thumb.style.background = "#000";
                thumb.style.cursor = "default";
                thumb.title = "空槽位（未选择视频）";
            }
        };
        thumb.textContent = "🎬";
        thumb.addEventListener("click", () => {
            const p = (item.path || "").trim();
            if (p) showVideoPreview(p);
        });
        refreshVideo();
    }
    row.append(thumb);

    // ── 右侧：图片用两行（上行=编号/名称/上传；下行=详细描述）；视频·音频保持原一行 ──
    const right = el("div", "flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:6px;justify-content:center;");
    const line1 = el("div", "display:flex;align-items:center;gap:6px;min-width:0;");
    const line2 = isImage ? el("div", "display:flex;align-items:center;gap:6px;min-width:0;") : null;

    // 启用开关
    const chk = el("input", "flex:0 0 auto;width:18px;height:18px;accent-color:#2d9d6f;cursor:pointer;");
    chk.type = "checkbox";
    chk.checked = item.enabled !== false;
    chk.title = "启用/禁用该素材";
    chk.addEventListener("change", () => { item.enabled = chk.checked; onEdit(); refreshAllFn(); });

    // 类型下拉（仅在「其他（自定义）」分区显示；各子分类由分区固定类型）
    const typeSel = el("select", "flex:0 0 58px;background:var(--comfy-input-bg,#1d1d1d);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);border-radius:4px;padding:5px 2px;font-size:12px;");
    for (const t of (ASSET_TYPES_BY_KIND[kind] || ASSET_TYPES)) {
        const o = el("option", "", t);
        if (t === item.type) o.selected = true;
        typeSel.append(o);
    }

    // 字母编号（A-Z）：同一类型已被其他素材占用的字母置灰，防止同类同编号
    const letterSel = el("select", "flex:0 0 42px;width:42px;background:var(--comfy-input-bg,#1d1d1d);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);border-radius:4px;padding:4px 1px;font-size:12px;text-align:center;cursor:pointer;");
    letterSel.title = "编号（26 字母 A-Z，同一类型内唯一）";
    const usedLetters = () => {
        const used = new Set();
        // 唯一性范围按槽位类型（音频的三个子分类共用「音频」编号空间；视频同理）
        slotTypePeers(kind, opts?.assets, item).forEach((it) => {
            const L = String(it?.letter || "").toUpperCase();
            if (L) used.add(L);
        });
        return used;
    };
    const renderLetters = () => {
        const used = usedLetters();
        letterSel.innerHTML = "";
        // 编号必选（不再提供「—」空白项）：新增/丢失的编号由 nextFreeLetter 自动补「靠前的未被占用字母」
        // 26 字母全部显示；已被同类其他素材占用的置灰禁用（可见但不可选），切换/换编号后实时刷新
        const cur = (item.letter || "").trim().toUpperCase();
        for (let i = 0; i < 26; i++) {
            const L = String.fromCharCode(65 + i);
            const o = el("option", "", L);
            if (L === cur) o.selected = true;
            if (used.has(L) && L !== cur) o.disabled = true;
            letterSel.append(o);
        }
    };
    const slotTag = el("span", "font-size:11px;color:#ffd166;white-space:nowrap;flex:0 0 auto;min-width:42px;text-align:center;font-weight:600;", "");
    const renderSlot = () => {
        const st = slotTypeOf(kind, item?.type || "");
        const L = (item?.letter || "").trim().toUpperCase();
        const tag = L || "?";
        slotTag.textContent = `${st}${tag}`;
        slotTag.title = `调度槽位：${st}${tag}（未指定编号时生成自动分配；ref 描述与调度 slots 用）`;
    };
    typeSel.addEventListener("change", () => { item.type = typeSel.value; renderLetters(); renderSlot(); onEdit(); refreshAllFn(); });
    letterSel.addEventListener("change", () => { item.letter = letterSel.value; renderSlot(); onEdit(); refreshAllFn(); });
    renderLetters();
    renderSlot();

    // 名称输入（「角色音色」的名称由绑定角色决定 → 不显示名称框，见下方 characterSel）
    const nameInp = el("input", "flex:1 1 120px;width:0;min-width:80px;background:var(--comfy-input-bg,#1d1d1d);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);border-radius:4px;padding:6px;font-size:12px;");
    nameInp.type = "text";
    nameInp.placeholder = isRole ? "角色名（如：孙悟空）" : (kind === "image" ? "名称（如：龟仙屋）" : "名称（如：孙悟空）");
    nameInp.value = item.name || "";
    nameInp.addEventListener("change", () => {
        const old = item.name;
        item.name = nameInp.value.trim();
        // 角色改名 → 联动画像音色的绑定角色名 + 刷新跨分区的「指定角色名」下拉
        if (isRole && old !== item.name) {
            syncVoiceCharacter(old, item.name);
            refreshRoleSels(nameInp.closest("section.jzl-modal") || document);
        }
        onEdit();
    });
    // 角色改名/删除时，把「音频 → 角色音色」里绑定的旧角色名跟着改（删角色则回退「未指定」）
    function syncVoiceCharacter(oldName, newName) {
        const from = String(oldName || "").trim();
        if (!from) return;
        (opts?.assets?.audios || []).forEach((a) => {
            if (String(a?.type || "") !== "角色音色") return;
            if (String(a?.character || "").trim() === from) a.character = String(newName || "").trim();
        });
    }

    // 详细描述输入（路径只由「上传素材」按钮设置，不再显示路径输入框）
    const descInp = el("input", "flex:1 1 120px;width:0;background:var(--comfy-input-bg,#1d1d1d);color:var(--fg-color,#ccc);border:1px solid var(--border-color,#444);border-radius:4px;padding:6px;font-size:11px;");
    descInp.type = "text";
    descInp.placeholder = isImage ? "图片详细描述（如：黑色刺猬头、橙色龟仙流武道服…）" : "详细描述";
    descInp.value = item.description || "";
    descInp.addEventListener("change", () => { item.description = descInp.value.trim(); onEdit(); });

    // ⭐ 音频「角色音色」子分类：指定角色名（未指定 + 图片里的角色名列表）。
    //    角色被改名 → 选项文字联动（syncVoiceCharacter 已同步本条的 character）；
    //    角色被删除 → 本项自动回退「未指定」，避免留下悬空引用。
    let characterSel = null;
    let syncCharacterSel = null;
    if (kind === "audio" && String(item.type || "") === "角色音色") {
        characterSel = el("select", "flex:0 1 132px;min-width:92px;background:var(--comfy-input-bg,#1d1d1d);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);border-radius:4px;padding:5px 2px;font-size:12px;");
        characterSel.title = "指定该音色属于哪个角色（选项来自「图片 · 角色」里的角色名；角色改名会联动）";
        syncCharacterSel = () => {
            const names = roleNameList(opts?.assets);
            let cur = String(item.character || "").trim();
            if (cur && !names.includes(cur)) { item.character = ""; cur = ""; }   // 角色已被删除/改名 → 回退未指定
            // 角色互斥：已被**其他**「角色音色」占用的角色不可再选（可见但置灰，与编号下拉一致）
            const taken = new Set();
            (opts?.assets?.audios || []).forEach((a) => {
                if (a === item) return;
                if (String(a?.type || "") !== "角色音色") return;
                if (a?.enabled === false) return;
                const ch = String(a?.character || "").trim();
                if (ch) taken.add(ch);
            });
            characterSel.innerHTML = "";
            for (const n of ["", ...names]) {
                const isTaken = n && taken.has(n) && n !== cur;
                const o = el("option", "", n ? (isTaken ? `${n}（已被其他音色占用）` : n) : "未指定");
                o.value = n;
                if (n === cur) o.selected = true;
                if (isTaken) o.disabled = true;
                characterSel.append(o);
            }
        };
        syncCharacterSel();
        characterSel.dataset.jzlRoleSync = "1";
        characterSel.__jzlRefresh = syncCharacterSel;
        // 打开下拉前重算选项：角色刚被改名/删除时也能即时反映（值本身由 syncVoiceCharacter/删除钩子联动）
        characterSel.addEventListener("mousedown", (e) => { e.stopPropagation(); syncCharacterSel(); });
        characterSel.addEventListener("change", () => {
            item.character = characterSel.value;
            onEdit();
            refreshRoleSels(characterSel.closest("section.jzl-modal") || document);  // 角色被占用 → 其他音色下拉同步置灰
            refreshAllFn();
        });
    }

    // 上传 / 替换：有素材显示「替换素材」，无素材显示「上传素材」
    const pickBtn = el("button", "flex:0 0 auto;background:#2a4a6a;color:#cfe3f7;border:1px solid #5b9bd5;border-radius:4px;padding:6px 8px;font-size:12px;cursor:pointer;", "上传素材");
    const refreshPickLabel = () => {
        const has = !!String(item.path || "").trim();
        pickBtn.textContent = has ? "替换素材" : "上传素材";
        pickBtn.title = has ? "重新选择文件替换当前素材（名称/描述/类型/编号保留）" : "选择文件";
    };
    pickBtn.addEventListener("click", async () => {
        const p = await chooseFile(kind);
        if (p) {
            item.path = p;
            // 名称留空时自动以文件名命名（去扩展名）；「角色音色」的名称来自角色，不自动填名
            const autoName = !(characterSel);
            if (autoName && !(item.name || "").trim()) {
                const autoNm = assetNameFromFilename(String(p || "").split(/[\\/]/).pop());
                if (autoNm) { item.name = autoNm; if (nameInp) nameInp.value = autoNm; }
            }
            refreshThumb(); if (refreshAudio) refreshAudio(); if (typeof refreshVideo === "function") refreshVideo();
            refreshPickLabel(); onEdit();
        }
    });
    refreshPickLabel();

    // 删除槽位：每张卡片都可删（排序已支持任意顺序，无需限制在末位）
    const delBtn = el("button", "flex:0 0 auto;background:transparent;color:#e08a8a;border:1px solid #844;border-radius:4px;padding:6px 8px;font-size:12px;cursor:pointer;", "删除槽位");
    delBtn.title = "删除该槽位";
    delBtn.addEventListener("click", onDelete);

    // ── 组装 ──
    const isVoice = !!(characterSel);
    // 图片（角色/场景/道具）：第一行 = 编号 + 名称 + 上传/替换 + 删除槽位；第二行 = 详细描述
    if (isImage) {
        line1.append(chk);
        if (showType) line1.append(typeSel);
        line1.append(letterSel, slotTag, nameInp, pickBtn, delBtn);
        line2.append(descInp);
        right.append(line1, line2);
    } else {
        // 视频 / 音频：预览已前置；音频「角色音色」用「指定角色名」代替名称框
        line1.append(chk);
        if (showType) line1.append(typeSel);
        line1.append(letterSel, slotTag);
        if (isVoice) line1.append(characterSel, descInp, pickBtn, delBtn);
        else line1.append(nameInp, descInp, pickBtn, delBtn);
        right.append(line1);
    }
    row.append(right);

    // 拖放替换：把文件拖到本素材「预览」窗口范围 → 仅替换素材文件（保留名称/简介/类型/编号/启用）
    thumb.title = `${thumb.title || "预览"}（拖文件到此窗口可替换素材）`;
    thumb.addEventListener("dragover", (e) => {
        const _files = e.dataTransfer && e.dataTransfer.files;
        if (!_files || !_files.length) return;   // 内部排序拖动：不拦截，交由区块插入式排序处理
        e.preventDefault();
        e.stopPropagation();
        try { e.dataTransfer.dropEffect = "copy"; } catch (_) {}
        thumb.style.borderColor = "#5ecf8a";
    });
    thumb.addEventListener("dragleave", () => { thumb.style.borderColor = ""; });
    thumb.addEventListener("drop", async (e) => {
        thumb.style.borderColor = "";
        const files = (e.dataTransfer && e.dataTransfer.files) ? Array.from(e.dataTransfer.files) : [];
        if (!files.length) return;   // 内部排序拖拽：不 preventDefault / stopPropagation，让 drop 冒泡到区块插入式排序
        e.preventDefault();
        e.stopPropagation();
        const file = files[0];
        const fkind = detectAssetKindByFile(file);
        if (fkind !== kind) { notify(`类型不符：预览为${KIND_LABEL[kind]}，拖入的是${KIND_LABEL[fkind || "未知"]}`, "warning"); return; }
        const p = await uploadFileObject(file, kind);
        if (p) {
            item.path = p;   // 仅替换素材文件，名称/简介/类型/编号/启用均保留
            refreshThumb();
            if (refreshAudio) refreshAudio();
            if (typeof refreshVideo === "function") refreshVideo();
            refreshPickLabel();
            onEdit();
            notify(`已替换 ${KIND_LABEL[kind]} 素材（保留名称与简介）`, "success");
        }
    });

    refreshThumb();
    return row;
}

// subType 非空时本区块只显示该类型的素材（图片大分类下的「角色/场景/道具」小分类）；
// 增删改与拖拽排序一律作用在 parentList 上，view 只负责显示过滤。
function renderAssetSection(c, kind, list, title, subType, opts) {
    const fireEdit = () => c.dispatchEvent(new Event("change", { bubbles: true }));
    c.append(makeSectionTitle(title));
    const box = el("div", "");
    const inView = (it) => (opts && typeof opts.inViewFn === "function")
        ? !!opts.inViewFn(it)
        : (!subType || String(it?.type || "") === subType);
    const view = () => (list || []).filter(inView);
    // 把本类型的条目按新顺序重排回 parentList，其他类型条目位置不变
    const applyOrder = (newOrder) => {
        const pos = [];
        list.forEach((it, i) => { if (inView(it)) pos.push(i); });
        pos.forEach((at, k) => { if (k < newOrder.length) list[at] = newOrder[k]; });
    };
    // ── 拖拽排序（插入式）：拖起某素材 → 该卡片淡蓝高亮；拖到任意两素材之间 → 出现虚线占位框（与素材栏等大）→ 松开即插入 ──
    let dragFrom = -1;       // 被拖素材原 index（相对 view）
    let dropIndex = -1;      // 插入位置（0..len，相对 view）
    let dragEl = null;       // 被拖卡片元素
    let ghost = null;        // 虚线占位框
    let floatEl = null;      // 跟随鼠标的被拖素材浮动克隆（拖拽时可见）
    let _down = false;       // 卡片上已按下（潜在拖拽）
    let _drag = false;       // 移动超阈值，进入拖拽
    let _sx = 0, _sy = 0;    // 按下起点
    const _cards = () => Array.from(box.querySelectorAll(".jzl-asset-card"));
    const clearDrag = () => {
        dragFrom = -1;
        dropIndex = -1;
        if (dragEl) { try { dragEl.style.background = ""; dragEl.style.opacity = ""; } catch (_) {} dragEl = null; }
        if (ghost) { try { ghost.remove(); } catch (_) {} ghost = null; }
        if (floatEl) { try { floatEl.remove(); } catch (_) {} floatEl = null; }
    };
    const placeGhost = (clientY) => {
        if (ghost) { ghost.remove(); ghost = null; }
        const cs = _cards();
        let idx = cs.length;
        for (let k = 0; k < cs.length; k++) {
            const r = cs[k].getBoundingClientRect();
            if (clientY < r.top + r.height / 2) { idx = k; break; }
        }
        const h = (cs[0] && cs[0].offsetHeight > 0) ? cs[0].offsetHeight : 46;
        ghost = el("div", `height:${h}px;border:2px dashed #5b9bd5;border-radius:6px;margin:4px 0;box-sizing:border-box;background:rgba(91,155,213,.12);`);
        ghost.style.pointerEvents = "none";
        const ref = cs[idx];
        if (ref) box.insertBefore(ghost, ref);
        else box.insertBefore(ghost, box.querySelector(".jzl-asset-add") || null);
        dropIndex = idx;
    };
    // ── 排序用手动鼠标事件（HTML5 DnD 的 drop 在「面板上传/缩略图替换」多层 dragover/drop 干扰下不派发，弃用）──
    const cardDown = (e, i, card) => {
        if (e.button !== 0) return;
        const t = e.target;
        if (t && t.closest && t.closest("input,select,button,textarea")) return;  // 控件交互，不拖拽
        _down = true;
        _drag = false;
        dragFrom = i;
        dragEl = card;
        _sx = e.clientX;
        _sy = e.clientY;
        document.addEventListener("mousemove", onDocMove);
        document.addEventListener("mouseup", onDocUp);
    };
    // 浮动克隆：把被拖素材卡克隆一份跟随鼠标（半透明+阴影），解决拖拽时看不到素材、只见虚线框
    const ensureFloat = (e) => {
        if (!floatEl && dragEl) {
            floatEl = dragEl.cloneNode(true);
            floatEl.style.position = "fixed";
            floatEl.style.zIndex = "99999";
            floatEl.style.pointerEvents = "none";
            floatEl.style.opacity = "0.85";
            floatEl.style.boxShadow = "0 10px 28px rgba(0,0,0,.5)";
            floatEl.style.transform = "scale(1.02)";
            floatEl.style.width = dragEl.offsetWidth ? dragEl.offsetWidth + "px" : "auto";
            const r0 = dragEl.getBoundingClientRect();
            floatEl.style.left = (e.clientX - r0.width / 2) + "px";
            floatEl.style.top = (e.clientY - 14) + "px";
            document.body.appendChild(floatEl);
        }
        if (floatEl) {
            const r0 = dragEl.getBoundingClientRect();
            floatEl.style.left = (e.clientX - r0.width / 2) + "px";
            floatEl.style.top = (e.clientY - 14) + "px";
        }
    };
    const onDocMove = (e) => {
        if (!_down) return;
        if (!_drag && (Math.abs(e.clientX - _sx) > 4 || Math.abs(e.clientY - _sy) > 4)) _drag = true;
        if (!_drag) return;
        e.preventDefault();
        if (dragEl) { dragEl.style.opacity = "0.35"; dragEl.style.background = "#2b3a4a"; }
        ensureFloat(e);
        placeGhost(e.clientY);
    };
    const onDocUp = (e) => {
        document.removeEventListener("mousemove", onDocMove);
        document.removeEventListener("mouseup", onDocUp);
        if (!_down) return;
        _down = false;
        if (_drag && dragFrom >= 0) {
            let inside = true;
            try {
                const r = box.getBoundingClientRect();
                inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
            } catch (_) {}
            if (inside && dropIndex >= 0) commitMove();
            else clearDrag();
        } else {
            clearDrag();
        }
        _drag = false;
    };
    const commitMove = () => {
        if (dragFrom < 0) return;
        const vs = view();
        let to = (dropIndex >= 0) ? dropIndex : vs.length;
        if (dragFrom < to) to = to - 1;
        const moved = vs.splice(dragFrom, 1)[0];
        if (to > vs.length) to = vs.length;
        vs.splice(to, 0, moved);
        applyOrder(vs);
        clearDrag();
        renderList();
        fireEdit();
    };
    const renderList = () => {
        box.innerHTML = "";
        const vs = view();
        let _letterFixed = false;
        vs.forEach((item, i) => {
            // 编号丢失/未指定（如音频偶尔丢号）→ 自动补「靠前的未被占用字母」（同槽位类型内唯一），并回写保存
            if (!String(item.letter || "").trim()) {
                const _L = nextFreeSlotLetter(kind, opts?.assets, item.type || "");
                if (_L) { item.letter = _L; _letterFixed = true; }
            }
            const isLast = i === vs.length - 1;
            const card = makeAssetCard(kind, i, item, vs, fireEdit, () => {
                const at = list.indexOf(item);
                if (at >= 0) list.splice(at, 1);
                // 删除「图片 · 角色」时，音频「角色音色」里指向该角色的绑定回退为「未指定」
                if (kind === "image" && String(item?.type || "") === "角色") {
                    const gone = String(item?.name || "").trim();
                    if (gone) {
                        (opts?.assets?.audios || []).forEach((a) => {
                            if (String(a?.type || "") === "角色音色" && String(a?.character || "").trim() === gone) a.character = "";
                        });
                    }
                    refreshRoleSels(box.closest("section.jzl-modal") || document);
                }
                renderList();
                fireEdit();
            }, isLast, renderList, opts);
            card.classList.add("jzl-asset-card");
            card.style.cursor = "grab";
            // 行首拖拽把手（明确按住此区排序，避免误触控件）
            const hdl = el("span", "flex:0 0 18px;text-align:center;color:#7a8ba0;font-size:13px;cursor:grab;user-select:none;flex-shrink:0;", "⠿");
            hdl.title = "按住拖动排序";
            card.prepend(hdl);
            card.addEventListener("mousedown", (e) => cardDown(e, i, card));
            box.append(card);
        });
        const _addLabel = subType ? `+ 添加${subType}` : `+ 添加${KIND_LABEL[kind]}`;
        const addBtn = el("button", "margin-top:4px;width:100%;padding:6px;background:#2a3a4a;color:#9fc3e8;border:1px dashed #5b9bd5;border-radius:6px;font-size:12px;cursor:pointer;", _addLabel);
        addBtn.classList.add("jzl-asset-add");
        addBtn.addEventListener("click", () => {
            const _t0 = subType || (ASSET_TYPES_BY_KIND[kind] || ASSET_TYPES)[0];
            list.push({ type: _t0, name: "", path: "", enabled: true, letter: nextFreeSlotLetter(kind, opts?.assets, _t0) });
            renderList();
            fireEdit();
        });
        box.appendChild(addBtn);
        if (_letterFixed) setTimeout(() => { try { fireEdit(); } catch (_) {} }, 0);   // 补号后立即持久化
    };
    renderList();
    c.append(box);
}

function openModal(node, panelId) {
    if (modal) { modal.overlay?.querySelector?.("input,button,select")?.focus?.(); return; }

    loadManager(node).then((data) => {
        buildModal(node, data, panelId);
    }).catch((e) => {
        notify("读取管理器配置失败：" + e.message, "error");
        buildModal(node, { settings: null, llm_models: [], mmproj_models: ["None"], chat_handlers: ["None"] }, panelId);
    });
}

function ensureManagerStyle() {
    if (document.getElementById("jzl-asset-style")) return;
    const st = el("style", "");
    st.id = "jzl-asset-style";
    st.textContent = [
        ".jzl-count-input::-webkit-inner-spin-button,",
        ".jzl-count-input::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}",
        ".jzl-count-input{-moz-appearance:textfield;appearance:textfield}",
        // 弹窗原生控件（对齐 ComfyUI 输入框主题）
        ".jzl-input{box-sizing:border-box;background:var(--comfy-input-bg,#2a2a2a);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);border-radius:4px;padding:6px 10px;font-size:13px;outline:none;}",
        ".jzl-input:hover{background:var(--comfy-input-bg-hover,#3a3a3a);}",
        ".jzl-input:focus{border-color:#5b9bd5;}",
        ".jzl-input option{background:var(--comfy-menu-bg,#2a2a2a);}",
        "[contenteditable][data-placeholder]:empty:before{content:attr(data-placeholder);color:#666;font-size:12px;}",
        "[contenteditable]:focus{outline:none;border-color:#5b9bd5;}",
        ".jzl-asset-token{display:inline-flex;align-items:center;gap:2px;padding:0 3px;margin:0 1px;border-radius:3px;background:#2f3540;user-select:none;cursor:default;vertical-align:middle;}",
        ".jzl-asset-token .jzl-asset-thumb{display:inline-block;width:20px;height:20px;object-fit:cover;border-radius:3px;border:1px solid #555;vertical-align:middle;}",
        ".jzl-asset-token[data-jzl-kind='video']::before{content:'🎬';font-size:12px;}",
        ".jzl-asset-token[data-jzl-kind='audio']::before{content:'🎧';font-size:12px;}",
        // 弹窗内勾选框统一 18px 橙色（素材库卡片用内联样式绿色覆盖，不受影响）
        ".jzl-modal input[type='checkbox']{width:18px;height:18px;accent-color:#f59e0b;cursor:pointer;}",
    ].join("\n");
    document.head.append(st);
}

// ── ➕ 提示词元素（融合 JZL_MiniMaxH3PromptEditor 手写提示词全套模板）──
const H3_EXAMPLE_TEXT = `[SHOT_START]

===H3_PROMPT===
subject_definitions:
<Subject 1> 是 <Picture 1> 中的龟仙屋，临海沙滩上的粉色两层小屋，红色尖顶屋顶，墙面印有 KAME HOUSE 字样，四周棕榈树环绕，面朝蔚蓝大海。
<Subject 2> 是 <Picture 2> 中的孙悟空，标志性的黑色刺猬头爆炸发型，身穿橙色龟仙流武道服，蓝色腰带，深蓝色内衣和护腕，脚穿蓝红相间武道靴，有一条棕色的猴子尾巴。

summary:
[reference generation + audio reference] 目标视频展现 <Subject 2> 与 <Subject 3> 在 <Subject 1> 的龟仙屋前收拾行囊、并肩启程前往武道会的场景。

retention_analysis:
<Subject 1> (出现在 [Shot 1], [Shot 2], [Shot 3]): fully_preserved - 粉色两层小屋、红色尖顶、KAME HOUSE 字样、棕榈树环绕。

detailed_description:

正午阳光从海面方向斜照，暖金色调铺满整座沙滩，写实电影级 3D 渲染风格，高饱和暖色主导，画面充满热血少年出发前的昂扬张力。

[Shot 1] 全景镜头确立 <Subject 1> 的龟仙屋全貌，粉色两层小屋的红色尖顶在暖阳下泛着微光，墙面 "KAME HOUSE" 字样清晰可辨。<Subject 2> 孙悟空蹲在门廊前，把最后一件橙色武道服用力塞进行囊，随即直起身，黑色刺猬头在海风里微微晃动。他转头望向门框方向，以参考自 <Audio 1> 的年轻音色说道：<d>[中文] 我收拾好了。</d>
overall_soundscape: 海风呼啸掠过棕榈叶，海浪拍打沙滩的哗啦声，行囊布料摩擦的沙沙声。

non_diegetic_music: N/A

===SCENE_INSTRUCTION===
{"slots":["场景:场景A","角色:角色A","角色:角色D"]}

===AUDIO_INSTRUCTION===
{"slots":["音频:音频A","音频:音频D"]}
[SHOT_END]`;

// [按钮标签, 插入内容, 是否末尾换行]
const H3_TAGS = [
    ["1. 示例文本", H3_EXAMPLE_TEXT, true],
    ["2. 段落开头", "[SHOT_START]\n===H3_PROMPT===", true],
    ["3. 段落结尾", "[SHOT_END]", true],
    ["4. 主体描述", "subject_definitions:\n<Subject 1> 是 <Picture 1> 中的**，***。\n<Subject 2> 是 <Picture 2> 中的**，***。\n<Subject 3> 是 <Picture 3> 中的**，***。", true],
    ["5. 视频摘要", "summary:\n[reference generation] 目标视频展现 ***。", true],
    ["6. 详细描述", "detailed_description:\n目标视频采用***风格，***。", true],
    ["7. 新建分镜", "[Shot 1] ", false],
    ["8. 整体声效", "overall_soundscape: 海浪拍打沙滩的哗啦声，棕榈叶沙沙作响，远处海鸥鸣叫。", true],
    ["9. 背景音效", "non_diegetic_music: N/A", true],
    ["10. 参考图片调用", '===SCENE_INSTRUCTION===\n{"slots":["场景:场景A","角色:角色A","道具:道具A"]}', true],
    ["11. 参考视频调用", '===VIDEO_INSTRUCTION===\n{"slots":["视频:视频A","视频:视频B"]}', true],
    ["12. 参考音频调用", '===AUDIO_INSTRUCTION===\n{"slots":["音频:音频A","音频:音频B"]}', true],
];

function closePromptElements() {
    document.getElementById("jzl-prompt-elements")?.remove();
    if (window.__jzlElEsc) {
        window.removeEventListener("keydown", window.__jzlElEsc);
        window.__jzlElEsc = null;
    }
}

function insertPromptElement(editable, text, trailing) {
    // 在内部编辑窗口（contenteditable）光标处插入提示词元素模板（自动另起一行）
    if (!editable) return;
    editable.focus();
    const sel = window.getSelection();
    let range = null;
    if (sel && sel.rangeCount) {
        const r0 = sel.getRangeAt(0);
        if (editable.contains(r0.startContainer)) range = r0;
    }
    if (!range) {
        range = document.createRange();
        range.selectNodeContents(editable);
        range.collapse(false);
    }
    const tmp = document.createElement("div");
    const pr = document.createRange();
    pr.selectNodeContents(editable);
    pr.setEnd(range.startContainer, range.startOffset);
    tmp.appendChild(pr.cloneContents());
    const prefix = getPromptText(tmp);
    let insert = "";
    if (prefix && !prefix.endsWith("\n")) insert += "\n";
    insert += text;
    if (trailing) insert += "\n";
    const tn = document.createTextNode(insert);
    range.deleteContents();
    range.insertNode(tn);
    const caret = document.createRange();
    caret.setStartAfter(tn);
    caret.collapse(true);
    sel.removeAllRanges();
    sel.addRange(caret);
    editable.dispatchEvent(new Event("input", { bubbles: true }));
    editable.dispatchEvent(new Event("change"));
}

// ── 原生提示词 textarea：@ 素材菜单（悬浮窗定位在光标后面）──
function getPromptTextarea(w) {
    if (!w) return null;
    if (w.element?.tagName === "TEXTAREA") return w.element;
    if (w.element?.querySelector) {
        const ta = w.element.querySelector("textarea");
        if (ta) return ta;
    }
    if (w.inputEl?.tagName === "TEXTAREA") return w.inputEl;
    return null;
}

function setupInternalPrompt(node, box, ipWidget) {
    // 提示词来源只有节点内编辑框（外部提示词端口已删除）：输入同步到 internal_prompt
    const syncInternal = () => {
        if (ipWidget) setWidgetValue(ipWidget, getPromptText(box));
        // 穿透生成的「生成详情 → 段数」随提示词里的 [SHOT_START] 段数联动
        try { node?.__jzlUpdateDisplay?.(); } catch (_) {}
    };
    box.addEventListener("input", () => {
        syncInternal();
        const offset = caretOffset(box);
        box.__lastCaret = offset;  // 自记忆（点素材 mousedown 也用它）
        promptCaretPos = offset;  // 记忆光标位置，供资产窗点击插入
        const before = getPromptText(box).slice(0, offset);
        const m = before.match(/@([^@\s]*)$/);
        if (m) openMentionMenu(box, offset - m[0].length, offset, m[1], node);
        else closeMentionMenu();
    });
    box.addEventListener("keyup", () => { rememberCaret(box); promptCaretPos = caretOffset(box); });
    box.addEventListener("click", () => { rememberCaret(box); promptCaretPos = caretOffset(box); });
    // 回车：统一插入「\n」文本（不生成 Chrome 的 <div><br></div>），保证 DOM 只有 textNode+token
    // ⚠️ 必须「无条件插入」：旧写法是 preventDefault() 之后才判断选区是否在框内，条件不满足时
    //    既没换行、又吞掉了浏览器默认换行 → 表现为「第一次回车没反应，要按两次」。
    //    现在选区异常（焦点在框外/无 range）时兜底插到末尾，保证按一次就换行。
    const insertNewline = () => {
        const _sel = window.getSelection();
        let r0 = null;
        if (_sel && _sel.rangeCount && box.contains(_sel.anchorNode)) {
            r0 = _sel.getRangeAt(0);
        } else {
            r0 = document.createRange();
            r0.selectNodeContents(box);
            r0.collapse(false);
        }
        r0.deleteContents();
        const tn = document.createTextNode("\n");
        r0.insertNode(tn);
        const r2 = document.createRange();
        r2.setStartAfter(tn);
        r2.collapse(true);
        if (_sel) { _sel.removeAllRanges(); _sel.addRange(r2); }
        syncInternal();
        box.dispatchEvent(new Event("input", { bubbles: true }));
    };
    box.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" || e.shiftKey) return;   // Shift+Enter 与 Enter 都换行
        if (e.isComposing || e.keyCode === 229) return; // 输入法组合中：交给输入法（避免吞掉选词回车）
        e.preventDefault();
        e.stopPropagation();
        insertNewline();
    });
    box.addEventListener("change", syncInternal);
    // 粘贴：阻止 ComfyUI 画布把剪贴板里的「节点 JSON」粘贴到画布，只插入纯文本到提示词
    box.addEventListener("paste", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const text = (e.clipboardData || window.clipboardData)?.getData("text/plain") || "";
        if (!text) return;
        const sel = window.getSelection();
        let range = null;
        if (sel && sel.rangeCount) {
            const r0 = sel.getRangeAt(0);
            if (box.contains(r0.startContainer)) range = r0;
        }
        if (!range) {
            range = document.createRange();
            range.selectNodeContents(box);
            range.collapse(false);
        }
        const frag = document.createDocumentFragment();
        String(text).replace(/\r\n/g, "\n").split("\n").forEach((line, idx) => {
            if (idx > 0) frag.appendChild(document.createTextNode("\n"));
            frag.appendChild(document.createTextNode(line));
        });
        range.deleteContents();
        range.insertNode(frag);
        range.collapse(false);
        sel.removeAllRanges();
        sel.addRange(range);
        box.dispatchEvent(new Event("input", { bubbles: true }));
        box.dispatchEvent(new Event("change"));
    });
    box.addEventListener("blur", () => setTimeout(closeMentionMenu, 150));
    // internal_prompt 是内部存储字段：拒绝接线（幽灵防护）
    const origCC = node.onConnectionsChange;
    node.onConnectionsChange = function (type, index, connected, link_info) {
        const r = origCC?.apply(this, arguments);
        if (type === 1 && this.inputs && this.inputs[index]) {
            const inp = this.inputs[index];
            if (inp.name === "internal_prompt" && connected) {
                try {
                    const lid = link_info?.id != null ? link_info.id : (typeof link_info === "number" ? link_info : null);
                    inp.link = null;
                    inp.socketless = true;
                    if (lid != null && this.graph && this.graph.links) {
                        const l = this.graph.links[lid];
                        if (l) {
                            const origin = this.graph.getNodeById(l.origin_id);
                            if (origin && origin.outputs && origin.outputs[l.origin_slot] && Array.isArray(origin.outputs[l.origin_slot].links)) {
                                const ol = origin.outputs[l.origin_slot].links;
                                const idx = ol.indexOf(lid);
                                if (idx >= 0) ol.splice(idx, 1);
                            }
                            delete this.graph.links[lid];
                        }
                    }
                    this.setDirtyCanvas?.(true, true);
                } catch (_) {}
            }
        }
        return r;
    };
    syncInternal();
}

function caretCoords(ta) {
    // 用镜像 div 精确计算 textarea 光标位置（@ 菜单定位在光标后面）
    const pos = ta.selectionStart ?? ta.value.length;
    const mirror = document.createElement("div");
    const cs = getComputedStyle(ta);
    mirror.style.cssText = `position:fixed;left:-10000px;top:0;visibility:hidden;white-space:pre-wrap;word-break:break-word;` +
        `font-size:${cs.fontSize};font-family:${cs.fontFamily};line-height:${cs.lineHeight};` +
        `width:${ta.clientWidth}px;box-sizing:border-box;` +
        `padding-top:${cs.paddingTop};padding-right:${cs.paddingRight};padding-bottom:${cs.paddingBottom};padding-left:${cs.paddingLeft};` +
        `border:${cs.borderWidth} ${cs.borderStyle} ${cs.borderColor};`;
    mirror.textContent = ta.value.slice(0, pos);
    const mark = document.createElement("span");
    mark.textContent = "\u200b";
    mirror.appendChild(mark);
    document.body.appendChild(mirror);
    const mr = mark.getBoundingClientRect();
    const mm = mirror.getBoundingClientRect();
    const tr = ta.getBoundingClientRect();
    document.body.removeChild(mirror);
    return { x: tr.left + (mr.left - mm.left), y: tr.top + (mr.top - mm.top) };
}

function openMentionMenuAtCaret(node, ta, start, end, query) {
    const q = (query || "").toLowerCase();
    const items = collectMentionItems(node).filter((it) => {
        if (!q) return true;
        return it.type.toLowerCase().includes(q) || it.name.toLowerCase().includes(q);
    });
    if (!items.length) { closeMentionMenu(); return; }
    closeMentionMenu();
    mentionState = { ta, start, end, mode: "textarea" };

    const pos = caretCoords(ta);
    const menu = document.createElement("div");
    menu.style.cssText = "position:fixed;z-index:10001;background:#1e1e1e;border:1px solid #444;border-radius:8px;max-height:340px;max-width:460px;overflow-y:auto;box-shadow:0 8px 20px rgba(0,0,0,0.5);padding:8px;";
    menu.style.left = pos.x + "px";
    menu.style.top = (pos.y + 6) + "px";

    const cols = items.length <= 3 ? items.length : Math.min(5, Math.max(3, Math.ceil(Math.sqrt(items.length))));
    const grid = document.createElement("div");
    grid.style.cssText = `display:grid;grid-template-columns:repeat(${cols},74px);gap:8px;`;
    for (const item of items) {
        const cell = document.createElement("div");
        cell.style.cssText = "display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer;border-radius:6px;padding:4px;border:1px solid transparent;";
        const thumb = document.createElement("div");
        thumb.style.cssText = "width:64px;height:64px;border-radius:5px;border:1px solid #444;background:#111;display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:26px;";
        if (item.kind === "image") {
            const img = document.createElement("img");
            img.style.cssText = "width:100%;height:100%;object-fit:cover;";
            img.src = item.path ? `${ASSET_PREVIEW_ENDPOINT}?path=${encodeURIComponent(item.path)}` : "";
            img.onerror = () => { img.src = "/extensions/ComfyUI-JZL-MiniMax-H3/icon.png"; };
            thumb.appendChild(img);
        } else {
            thumb.textContent = item.kind === "video" ? "🎬" : "🎧";
        }
        const typeTag = document.createElement("span");
        typeTag.style.cssText = "font-size:10px;color:#8ab8dd;line-height:1;";
        typeTag.textContent = item.type || KIND_LABEL[item.kind] || "";
        const label = document.createElement("span");
        label.style.cssText = "font-size:11px;color:#ddd;line-height:1.2;max-width:70px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center;";
        label.textContent = item.slotLabel || item.name;
        cell.append(thumb, typeTag, label);
        cell.addEventListener("mousedown", (e) => { e.preventDefault(); e.stopPropagation(); });
        cell.addEventListener("click", () => chooseMentionText(ta, item));
        cell.addEventListener("mouseenter", () => { cell.style.border = "1px solid #5b9bd5"; cell.style.background = "#2a2a2a"; });
        cell.addEventListener("mouseleave", () => { cell.style.border = "1px solid transparent"; cell.style.background = "transparent"; });
        grid.appendChild(cell);
    }
    menu.appendChild(grid);
    document.body.appendChild(menu);
    mentionMenu = menu;
}

function chooseMentionText(ta, item) {
    if (!mentionState || mentionState.mode !== "textarea") return;
    const { start, end } = mentionState;
    closeMentionMenu();
    const token = item.token;
    const text = ta.value;
    const next = text.slice(0, start) + token + " " + text.slice(end);
    ta.value = next;
    const pos = start + token.length + 1;
    try { ta.focus(); ta.setSelectionRange(pos, pos); } catch (_) {}
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    ta.dispatchEvent(new Event("change"));
}

function showPromptElements(self) {
    const box = self?.__promptBox;
    if (!box) return;
    closePromptElements();
    const overlay = document.createElement("div");
    overlay.id = "jzl-prompt-elements";
    overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:99999;display:flex;align-items:center;justify-content:center;";
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closePromptElements(); });
    const panel = document.createElement("div");
    panel.style.cssText = "background:#1b1b1f;border:1px solid #444;border-radius:10px;padding:14px;max-width:640px;width:92%;max-height:80vh;overflow-y:auto;box-shadow:0 10px 40px rgba(0,0,0,.6);";
    const title = document.createElement("div");
    title.textContent = "➕ 提示词元素（插入到提示词光标处，自动另起一行）";
    title.style.cssText = "color:#eee;font-size:14px;font-weight:600;margin-bottom:10px;";
    panel.appendChild(title);
    const grid = document.createElement("div");
    grid.style.cssText = "display:grid;grid-template-columns:repeat(3,1fr);gap:6px;";
    for (const [label, tag, trailing] of H3_TAGS) {
        const b = document.createElement("button");
        b.textContent = label;
        b.style.cssText = "padding:7px 6px;border:1px solid #555;border-radius:6px;background:#2a2a2e;color:#eee;cursor:pointer;font-size:12px;line-height:1;";
        b.addEventListener("mouseenter", () => { b.style.background = "#3a3a40"; });
        b.addEventListener("mouseleave", () => { b.style.background = "#2a2a2e"; });
        b.addEventListener("click", () => { insertPromptElement(box, tag, trailing); closePromptElements(); });
        grid.appendChild(b);
    }
    panel.appendChild(grid);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    window.__jzlElEsc = (e) => { if (e.key === "Escape") closePromptElements(); };
    window.addEventListener("keydown", window.__jzlElEsc);
}

// ── 表单控件 ──────────────────────────────────────────────
function field(labelText, control) {
    // 原生感：无卡片背景，标签 + 控件对齐（对齐官方设置面板）
    const g = el("div", "display:flex;align-items:center;gap:12px;margin-bottom:10px;");
    const lab = el("label", "flex:0 0 160px;font-size:13px;color:var(--descrip-text,#999);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;", labelText);
    control.style.flex = "1 1 0";
    control.style.minWidth = "0";
    // 复选框/开关是 label 包裹，不加输入框样式；真正的输入控件才有 .jzl-input（等宽由 flex 保证）
    if (control.tagName === "INPUT" || control.tagName === "SELECT" || control.tagName === "TEXTAREA") {
        control.classList.add("jzl-input");
    }
    g.append(lab, control);
    return g;
}

function selectControl(options, value, onChange) {
    const s = el("select", "flex:1 1 0;min-width:0;");
    s.className = "jzl-input";
    for (const o of options) {
        // 兼容字符串选项与 { value, label } 选项（label 仅显示用，value 保持后端识别值）
        const v = (typeof o === "object" && o !== null && o.value !== undefined) ? String(o.value) : String(o);
        const t = (typeof o === "object" && o !== null && o.label) ? String(o.label) : String(o);
        const op = el("option", "", t);
        op.value = v;
        if (v === String(value)) op.selected = true;
        s.append(op);
    }
    s.addEventListener("change", () => onChange(s.value));
    return s;
}

function textControl(value, placeholder, onChange) {
    const i = el("input", "flex:1 1 0;min-width:0;");
    i.className = "jzl-input";
    i.type = "text";
    i.value = value || "";
    if (placeholder) i.placeholder = placeholder;
    i.addEventListener("change", () => onChange(i.value.trim()));
    return i;
}

function passwordControl(value, placeholder, onChange) {
    // 密码型输入：明文不显示（API Key 等敏感字段）
    const i = el("input", "flex:1 1 0;min-width:0;");
    i.className = "jzl-input";
    i.type = "password";
    i.value = value || "";
    if (placeholder) i.placeholder = placeholder;
    i.addEventListener("change", () => onChange(i.value.trim()));
    return i;
}

function numberControl(value, opts, onChange) {
    const i = el("input", "flex:1 1 0;min-width:0;");
    i.className = "jzl-input";
    i.type = "number";
    i.value = value;
    if (opts) { i.min = opts.min; i.max = opts.max; i.step = opts.step; }
    i.addEventListener("change", () => onChange(parseFloat(i.value) || 0));
    return i;
}

function checkboxControl(value, labelText, onChange) {
    const w = el("label", "display:flex;align-items:center;gap:10px;cursor:pointer;font-size:13px;color:var(--fg-color,#ddd);");
    const c = el("input", "width:18px;height:18px;accent-color:#f59e0b;cursor:pointer;");
    c.type = "checkbox";
    c.checked = !!value;
    c.addEventListener("change", () => onChange(c.checked));
    w.append(c, el("span", "", labelText));
    return w;
}

// control_after_generate（生成后控制）：随机/固定/增加/减少
const SEED_CONTROL_KEYS = ["randomize", "fixed", "increment", "decrement"];
const SEED_CONTROL_LABEL = { randomize: "随机", fixed: "固定", increment: "增加", decrement: "减少" };

function seedControlRow(p, fieldKey = "seed", modeKey = "seed_control", labelText = "种子") {
    // 原生感：标签 + 数字输入 + control_after_generate 按钮（点击循环 随机/固定/增加/减少）
    const g = el("div", "display:flex;align-items:center;gap:12px;margin-bottom:10px;");
    const lab = el("label", "flex:0 0 160px;font-size:13px;color:var(--descrip-text,#999);white-space:nowrap;", labelText);
    const i = el("input", "flex:1;min-width:0;");
    // 种子是 64 位整数（可达 9.2e18，> Number.MAX_SAFE_INTEGER）→ 用 text 存字符串，避免精度丢失/显示异常
    i.type = "text";
    i.inputMode = "numeric";
    i.value = (p[fieldKey] === undefined || p[fieldKey] === null) ? "0" : String(p[fieldKey]);
    i.className = "jzl-input jzl-count-input";
    i.placeholder = "0 = 随机（运行后回填真实种子）";
    i.addEventListener("change", () => {
        const v = String(i.value || "").replace(/[^0-9]/g, "");
        p[fieldKey] = v === "" ? 0 : v;      // 以字符串保存（Python int() 可直接解析）
        i.value = String(p[fieldKey]);
    });
    const ctrl = el("button", "flex:1 1 0;min-width:0;background:#b45309;color:#fff;border:1px solid #d97706;border-radius:4px;padding:6px 10px;font-size:12px;cursor:pointer;white-space:nowrap;text-align:center;");
    const curKey = () => (SEED_CONTROL_LABEL[p[modeKey]] ? p[modeKey] : "randomize");
    const render = () => {
        ctrl.textContent = "🔁 " + SEED_CONTROL_LABEL[curKey()];
        ctrl.title = "生成后控制（control_after_generate）：随机=每次运行自动换新种子；固定=锁定当前值；递增=每次运行+1。点击切换";
    };
    ctrl.addEventListener("click", () => {
        const next = SEED_CONTROL_KEYS[(SEED_CONTROL_KEYS.indexOf(curKey()) + 1) % SEED_CONTROL_KEYS.length];
        p[modeKey] = next;
        render();
        g.dispatchEvent(new Event("change", { bubbles: true }));  // 触发面板自动保存
    });
    render();
    g.append(lab, i, ctrl);
    return g;
}

// 递归补齐缺失键（用户值优先，只在键缺失/为 null 时填默认值）。
// 必须递归：只按顶层键合并的话，旧配置里的 sample_decode 会整块盖掉默认值，
// 导致后加的键（selflift.steps / second.tiling_tiles …）永远不存在 →
// 面板显示的是「?? 默认值」，保存时却写不进去 → 后端只能回退，出现「面板 8 步、后端 4 步」。
function fillDefaults(target, defaults) {
    for (const [k, dv] of Object.entries(defaults || {})) {
        const tv = target[k];
        if (tv === undefined || tv === null) {
            target[k] = (dv && typeof dv === "object") ? JSON.parse(JSON.stringify(dv)) : dv;
        } else if (dv && typeof dv === "object" && !Array.isArray(dv) && typeof tv === "object" && !Array.isArray(tv)) {
            fillDefaults(tv, dv);
        }
    }
    return target;
}

function defaultSettings() {
    return {
        auto_save: true,
        align_mode: "text",
        // 视频保存设置（并入节点配置，不占 schema widget → 节点表面无隐藏保存接口）
        save: { mode: "分段保存", auto_save: false, auto_merge: false, auto_merge_delete: false },
        models: {
            fl2va: { model: "", loras: [] },
            ref2va: { model: "", loras: [] },
            common: { clip: "", video_vae: "", audio_vae: "" },
        },
        assets: { images: [], videos: [], audios: [] },
        // Skill 设定（强化层可替换；强制格式层不受影响）
        // mode/expand/style 的 *_source: builtin | user；*_skill 存 `builtin:<名>` / `user:<名>`
        // passthrough_mode: strict（严格对齐）| free（自由模式）
        skills: {
            mode_source: "builtin", mode_skill: "",
            // expand_source_mode = 扩写 skill 来源（内置/自定义）；与 enhance.expand_source（素材/自由）无关
            expand_source_mode: "builtin", expand_skill: "",
            style_source: "builtin", style_skill: "",
            passthrough_mode: "strict",
        },
        // 「生成设定 → 语义修正」：语义桥（内部调用内置副本，不依赖用户另装该节点包）
        semantic_bridge: { enabled: false, adapter: "", alpha: 0.10, magnitude_match: "per_token" },
        enhance: {
            story_decompose: true,
            enabled: false,
            llm_backend: "在线API [api]",
            force_offload: false,
            seed: 0, seed_control: "randomize",
            llm: {
                model: "", mmproj: "None", chat_handler: "None",
                backend: "llama-cpp-python", n_ctx: 32768, vram_limit: -1,
                image_min_tokens: 0, image_max_tokens: 0, max_tokens: 8192,
                top_k: 40, top_p: 0.9, min_p: 0.05, typical_p: 1.0,
                temperature: 0.6, repeat_penalty: 1.05, frequency_penalty: 0.0,
                present_penalty: 0.0, mirostat_mode: 0, mirostat_eta: 0.1,
                mirostat_tau: 5.0, gpu_device: "auto",
            },
            api: {
                provider: "OpenAI 兼容 (OpenAI/DeepSeek/Qwen/GLM/Kimi/Ollama/vLLM/LM Studio)",
                model: "deepseek-flash", api_key: "", base_url: "https://api.deepseek.com",
                temperature: 0.6, max_tokens: 81920, thinking: "disabled",
            },
            preference: {
                shot_size: "随机组合", camera_move: "随机组合", cut_rhythm: "随机",
                angle_view: "根据剧情",
                transition: "随机", music_style: "禁止音乐 / No Music",
                creative_req: "无特别要求", detail_length: "标准 (350-500字)", custom: "",
            },
            custom_prompt: "", system_prompt: "", expand_words: 500, expand_source: "素材",
            inference_mode: "one by one", max_frames: 24, max_size: 256,
        },
        gen_params: {
            aspect_ratio: "16:9 (Widescreen)", megapixels: 1.0, multiple: 32, duration: 5,
            width: 0, height: 0, scale_factor: 1.0, upscale_scale: 1.0,
            // 🎭 人物替换专用（其它导演台不读这些键，互不影响）
            segment: "固定5秒", audio_mode: "保留原声（推荐）", max_segments: 60,
            second_mode: "目标分辨率", second_width: 1280, second_height: 704,
        },
        sample_decode: {
            sampler: "res_multistep", scheduler: "simple", steps: 4, cfg: 1.0,
            seed: 0, seed_mode: "randomize",
            // 采样分类：常规采样（K采样器 + 可选二采）/ SelfLift采样（内置 SelfLift 渐进分辨率采样）
            sample_mode: "常规采样",
            selflift: {
                // SelfLift 有独立的步数：默认 8（低分辨率前缀 6 + 高分辨率收尾 2），与常规采样的 4 步互不干扰
                steps: 8,
                transition_step: 6, lowres_scale: 0.5, rho: 0.6, w_min: 0.5, w_max: 1.0,
                upscaler_model: "none", upscaler_unload: true, highres_tiling: false,
            },
            decode_video: "XB-BOX - VAE解码（原版优化）", decode_cleanup: "卸载显存模型",
            infinite: {
                window: 22, audio_link: true, audio_no_trim: false, settle: 12,
                trim_mode: "像素域裁切（推荐）", guide_source: "一采latent尾部（推荐）",
                second_guide: "first", luma_match: true,
            },
            // 🎭 人物替换的分段衔接（与「无限时长衔接」同一套窗口/过渡语义，但接的是「上一段生成结果」）
            seam: { enabled: true, window: 22, settle: 12, ref_video_tail: 0,
                    // pixels = 上段解码像素按本采画布重编码（官方 AddGuide 同款，不糊）
                    // latent = 上段一采 latent 尾部（旧实现，二采会被插值糊掉）
                    guide_mode: "pixels", pin_second: true, append_prev_video: false },
            second: {
                enabled: false,
                upscaler_model: "",
                device: "cuda", precision: "fp32",
                sampler: "euler", scheduler: "simple", steps: 3, denoise: 0.3,
                sigmas_mode: "scheduler",
                custom_sigmas: "0.8500, 0.6316, 0.3158, 0.0000",
                // 二采空间分块（自研 forward 级切块 + 升余弦融合）：关闭 / 自动 / 手动
                tiling: "关闭", tiling_tiles: 2, tiling_overlap: 128,
            },
        },
    };
}

// ── 配置面板 ──────────────────────────────────────────────
// 槽位类型顺序（与后端 _build_asset_intro 的 _SLOT_TYPE_ORDER 一致）
const REF_SLOT_ORDER = { "角色": 0, "场景": 1, "道具": 2, "分镜": 3, "其他": 4, "视频": 0, "音色": 0, "配乐": 1, "念白": 2, "音频": 3 };

// 按勾选素材生成只读的三路描述文本（供 LLM 拆解/增强使用，与后端 _build_asset_intro 完全一致）
function buildRefIntro(assets) {
    const out = { image: [], video: [], audio: [] };
    const slotMap = buildSlotMap(assets || {});
    const collect = (kind, key, pool) => {
        ((assets?.[key]) || []).forEach((item, i) => {
            if (item?.enabled === false) return;
            // 名称与槽位表共用 assetDisplayName（「角色音色」的名称来自绑定角色）
            const name = assetDisplayName(kind, item);
            if (!name) return;
            const st = slotTypeOf(kind, item?.type || "");
            const slot = slotMap[`${kind}:${i}`] || st;
            const desc = (item?.description || "").trim();
            pool.push({ order: REF_SLOT_ORDER[st] ?? 9, L: slot.slice(st.length), text: `${slot} = ${name}${desc ? `（${desc}）` : ""}` });
        });
    };
    collect("image", "images", out.image);
    collect("video", "videos", out.video);
    collect("audio", "audios", out.audio);
    const fmt = (arr) => arr.slice().sort((a, b) => a.order - b.order || a.L.localeCompare(b.L)).map((x) => x.text).join("\n");
    return { image: fmt(out.image), video: fmt(out.video), audio: fmt(out.audio) };
}

function renderAssetsTools(c, s, build, node) {
    c.append(makeSectionTitle("素材库导入导出"));
    const row = el("div", "display:flex;align-items:center;gap:8px;margin:0 0 4px;");
    const exp = el("button", "flex:1 1 0;background:#2a4a6a;color:#cfe3f7;border:1px solid #5b9bd5;border-radius:4px;padding:7px 10px;font-size:12px;cursor:pointer;white-space:nowrap;", "⬆️ 导出素材库");
    const imp = el("button", "flex:1 1 0;background:#3a5a2a;color:#d7f0c8;border:1px solid #6b9b5b;border-radius:4px;padding:7px 10px;font-size:12px;cursor:pointer;white-space:nowrap;", "⬇️ 导入素材库");
    const status = el("div", "font-size:11px;color:#9fd6a4;margin:0 0 10px;min-height:14px;white-space:pre-wrap;", "");
    const say = (msg, isErr) => {
        status.textContent = msg;
        status.style.color = isErr ? "#e08a8a" : "#9fd6a4";
        try { notify(msg, isErr ? "error" : "success"); } catch (_) {}
    };
    exp.title = "把当前已添加的素材保存为 output/jzl/{故事名}/参考素材_{故事名}_{年月日时分秒}.txt（未填故事名则存 output/jzl/）";
    imp.title = "选择素材库 txt 文件并载入（覆盖当前列表）";
    exp.addEventListener("click", async () => {
        exp.disabled = true; say("正在导出…");
        try {
            // 故事名取节点「故事名称」widget（未填则后端用时间戳命名）
            const sw = (node?.widgets || []).find((x) => x.name === "story_name");
            const storyName = sw ? readWidgetValue(sw) : "";
            const resp = await api.fetchApi("/jzl/export_assets", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ assets: s.assets || { images: [], videos: [], audios: [] }, story_name: storyName }),
            });
            const data = await resp.json().catch(() => ({}));
            if (data?.ok) {
                const rel = data.rel
                    ? String(data.rel)
                    : (data.path ? "jzl/" + String(data.path).split(/[\\/]/).pop() : "jzl/参考素材_*.txt");
                say(`✅ 已导出：output/${rel}`);
            }
            else say(`❌ ${data?.error || ("导出失败（HTTP " + resp.status + "）——请确认已重启 ComfyUI 且新版已部署")}`, true);
            console.log("[JZL 素材库] export resp", resp.status, data);
        } catch (e) { say(`❌ 导出失败：${(e && e.message) || e}`, true); console.error(e); }
        exp.disabled = false;
    });
    imp.addEventListener("click", () => {
        if (!document.__jzlImportInput) {
            const inp = document.createElement("input");
            inp.type = "file";
            inp.accept = ".txt,text/plain";
            inp.style.display = "none";
            document.body.appendChild(inp);
            document.__jzlImportInput = inp;
        }
        const inp = document.__jzlImportInput;
        inp.value = "";
        inp.onchange = async () => {
            const file = inp.files && inp.files[0];
            if (!file) return;
            imp.disabled = true; say(`正在导入 ${file.name}…`);
            try {
                const text = await new Promise((resolve, reject) => {
                    const r = new FileReader();
                    r.onload = () => resolve(r.result || "");
                    r.onerror = () => reject(r.error);
                    r.readAsText(file, "utf-8");
                });
                const resp = await api.fetchApi("/jzl/import_assets", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ text }),
                });
                const data = await resp.json().catch(() => ({}));
                if (data?.ok && data.assets) {
                    s.assets = data.assets;
                    build?.();
                    c.dispatchEvent(new Event("change", { bubbles: true }));  // 触发自动保存
                    const n = (data.assets.images || []).length + (data.assets.videos || []).length + (data.assets.audios || []).length;
                    say(`✅ 已从 ${file.name} 导入 ${n} 个素材并保存`);
                } else {
                    say(`❌ ${data?.error || ("导入失败（HTTP " + resp.status + "）")}`, true);
                }
                console.log("[JZL 素材库] import resp", resp.status, data);
            } catch (e) { say(`❌ 导入失败：${(e && e.message) || e}`, true); console.error(e); }
            imp.disabled = false;
        };
        inp.click();
    });
    row.append(exp, imp);
    c.append(row, status);
}

function renderAssetsPanel(c, s, mode) {
    const assets = s.assets;
    // 素材引用提示（生成路径固定 ref2va 多参考，无 fl2va 首尾帧/首帧路径）
    c.append(el("div", "background:#2b3a4a;border:1px solid #5b9bd5;border-radius:6px;padding:8px 12px;margin-bottom:8px;font-size:12px;color:#cfe3f7;", `素材引用上限：图片 ≤9 / 视频 ≤3 / 音频 ≤3（生成路径固定为「多参考生成音视频 REF2VA」：图片=身份参考、音频=音色/配乐/念白参考，音频严格使用上传的文件。没有首尾帧/首帧（fl2va）路径；视频未被引用时，视频与视频音轨端口自动关闭）`));
    c.append(el("div", "background:#1f2a33;border:1px dashed #5b9bd5;border-radius:6px;padding:6px 12px;margin-bottom:8px;font-size:12px;color:#9fc3e8;", `🖱 可直接把图片/视频/音频文件拖拽到本面板上传，自动按文件名命名素材（也可点卡片「上传素材」）。`));

    // 🖼️ 图片大分类 → 「角色」「场景」「道具」三个小分类（各自独立卡片列表与编号；编号逻辑沿用原 A-Z 同类型唯一）。
    // 「角色」卡片额外带「音色」组（编号与音频区共用 A-Z）；三类都不显示类型下拉（类型由分区决定）。
    // opts.assets = 完整素材配置：音频卡与角色音色卡需要跨列表计算共享编号占用。
    renderAssetSection(c, "image", assets.images, "🖼️ 图片 · 角色", "角色", { showType: false, assets });
    renderAssetSection(c, "image", assets.images, "🖼️ 图片 · 场景", "场景", { showType: false, assets });
    renderAssetSection(c, "image", assets.images, "🖼️ 图片 · 道具", "道具", { showType: false, assets });
    // 旧数据的「自定义」等非三类图片：仅在有内容时显示，并保留类型下拉以便改回三类
    const _imgOthers = (assets.images || []).filter((it) => !["角色", "场景", "道具"].includes(String(it?.type || "")));
    if (_imgOthers.length) renderAssetSection(c, "image", assets.images, "🖼️ 图片 · 其他（自定义）", "自定义", { showType: true, assets });
    renderAssetSection(c, "video", assets.videos, "🎬 视频", null, { assets });
    // 🎧 音频大分类 → 「角色音色」「背景配乐」「念白」三个小分类。
    // 三者**各自独立编号**（音色A / 配乐A / 念白A），因此各有独立前缀，不会撞出重复槽位名。
    renderAssetSection(c, "audio", assets.audios, "🎧 音频 · 角色音色", "角色音色", { showType: false, assets });
    renderAssetSection(c, "audio", assets.audios, "🎧 音频 · 背景配乐", "背景配乐", { showType: false, assets });
    renderAssetSection(c, "audio", assets.audios, "🎧 音频 · 念白", "念白", { showType: false, assets });
    // 旧数据的「音效 / 其他」等非三小类音频：仅在有内容时显示，并保留类型下拉以便改回三类
    const _audOthers = (assets.audios || []).filter((it) => !["角色音色", "背景配乐", "念白"].includes(String(it?.type || "")));
    if (_audOthers.length) {
        renderAssetSection(c, "audio", assets.audios, "🎧 音频 · 其他（自定义）", null, {
            showType: true, assets,
            inViewFn: (it) => !["角色音色", "背景配乐", "念白"].includes(String(it?.type || "")),
        });
    }

    // 底部：只读的「传给 LLM 的素材描述」（按勾选素材自动生成，顺序：角色→场景→道具→自定义，字母 A/B/C）
    c.append(makeSectionTitle("📄 传给 LLM 的素材描述（只读，按勾选自动生成）"));
    const mkRef = (label, getVal) => {
        const row = el("div", "display:flex;align-items:flex-start;gap:10px;margin-bottom:8px;");
        const lab = el("div", "flex:0 0 72px;font-size:12px;color:var(--descrip-text,#999);padding-top:6px;", label);
        const ta = el("textarea", "flex:1;min-width:0;resize:vertical;background:var(--comfy-input-bg,#1d1d1d);color:var(--fg-color,#ccc);border:1px solid var(--border-color,#444);border-radius:4px;padding:6px;font-size:11px;line-height:1.5;");
        ta.readOnly = true;
        const set = () => { const v = getVal(); ta.value = v; ta.rows = Math.max(2, Math.min(8, v.split("\n").length)); };
        set();
        row.append(lab, ta);
        return { row, set };
    };
    const ri = mkRef("图片描述", () => buildRefIntro(s.assets).image);
    const rv = mkRef("视频描述", () => buildRefIntro(s.assets).video);
    const ra = mkRef("音频描述", () => buildRefIntro(s.assets).audio);
    c.append(ri.row, rv.row, ra.row);
    // 素材变化（change 冒泡到 c）时刷新只读描述；委托模式避免重建时重复/失效监听
    c.__jzlRefIntroRefresh = () => { ri.set(); rv.set(); ra.set(); };
    if (!c.__jzlRefIntroBound) {
        c.__jzlRefIntroBound = true;
        c.addEventListener("change", () => { try { c.__jzlRefIntroRefresh?.(); } catch (_) {} });
    }
}

function renderAutoSave(c, s) {
    // 自动保存已移至弹窗底部，与「取消/保存」按钮同行（见 buildModal footer）
}

function renderPromptPanel(c, s, d, node) {
    const p = s.enhance;
    renderAutoSave(c, s);

    // ── 输出语言（最顶优先选择；读写 manager_settings.enhance.prompt_lang，随工作流保存）──
    c.append(makeSectionTitle("输出语言"));
    c.append(field("输出语言", selectControl(["中文 [ZH]", "英文 [EN]"], p.prompt_lang || "中文 [ZH]", v => { p.prompt_lang = v; })));

    // ── 提示词增强（拆解后是否再润色 detailed_description）──
    // 「不拆解」已移除：它只适用于「已拆解提示词」，而那正是穿透模式的职责（拆解模式还会直接报错）。
    // 因此这里只剩一个复选框，控制 enhance.enabled；story_decompose 恒为 true（迁移时自动修正）。
    c.append(makeSectionTitle("提示词增强"));
    const _rmNow = jzlNormalizeRunMode(
        readWidgetValue((node?.widgets || []).find((w) => w.name === "run_mode")) || "");
    const _enhApplies = (_rmNow === "故事拆解模式" || _rmNow === "仅提示词输出");
    if (_enhApplies) {
        const enhCard = jzlPanelCard();
        enhCard.append(field("拆解后增强", checkboxControl(p.enabled === true,
            "拆解后再用 LLM 润色每个分段的 detailed_description（故事风格在这一步权重最高；耗时约翻倍）",
            v => { p.enabled = !!v; if (v) p.story_decompose = true; })));
        enhCard.append(el("div", "font-size:12px;color:var(--descrip-text,#999);line-height:1.7;margin:0 0 8px;",
            "关闭 = 仅拆解后直接生成（默认，最快）。\n"
            + "开启 = 拆解 → 增强 → 生成；故事风格只能在这一步真正决定成片质感。"));
        c.append(enhCard);
    } else {
        c.append(el("div", "font-size:12px;color:var(--descrip-text,#999);line-height:1.7;margin:0 0 8px;white-space:pre-line;",
            _rmNow === "故事扩写模式"
                ? "当前为「故事扩写模式」：只把故事扩写成正文，不做拆解与增强，本项不适用。"
                : "当前为「穿透生成模式」：跳过全部 LLM 步骤，本项不适用。"));
    }

    // ── 故事扩写设置（📚 故事扩写模式专用）──
    c.append(makeSectionTitle("故事扩写设置"));
    const expandRow = el("div", "display:flex;flex-direction:column;gap:6px;margin-bottom:10px;");
    const expandCbs = [];
    const expandOpts = [
        { val: "素材", label: "使用参考扩写", tip: "严格按照「参考素材管理」勾选的角色/场景/道具进行扩写" },
        { val: "自由", label: "自由发挥扩写", tip: "在用户输入故事的基础上，按所选故事风格自由发挥扩写" },
    ];
    const mkExpandCb = (opt) => {
        const lab = el("label", "display:flex;align-items:flex-start;gap:8px;font-size:13px;color:var(--descrip-text,#999);cursor:pointer;");
        const rb = el("input", "width:16px;height:16px;accent-color:#f59e0b;margin-top:1px;cursor:pointer;");
        rb.type = "radio";
        rb.name = "jzl_expand_source";
        rb.value = opt.val;
        rb.checked = (p.expand_source || "素材") === opt.val;
        const span = el("span", "", opt.label + (opt.tip ? `（${opt.tip}）` : ""));
        rb.addEventListener("change", () => { if (rb.checked) { p.expand_source = opt.val; } });
        lab.append(rb, span);
        expandRow.append(lab);
        expandCbs.push(rb);
        return rb;
    };
    expandOpts.forEach(mkExpandCb);
    // 扩写字数（放「故事扩写设置」最底部，与上方两单选同属一个分区）
    const wordsRow = el("div", "display:flex;align-items:center;gap:12px;margin-top:2px;");
    wordsRow.append(el("label", "flex:0 0 160px;font-size:13px;color:var(--descrip-text,#999);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;", "扩写字数（扩写模式）"));
    wordsRow.append(numberControl(p.expand_words ?? 500, { min: 100, max: 3000, step: 50 }, v => { p.expand_words = Math.round(v); }));
    expandRow.append(wordsRow);
    c.append(expandRow);
    // 迁移兼容：旧配置无 expand_source → 默认「素材」（初次打开渲染已默认，无需额外处理）

    // ── LLM 后端（方形勾选单选：本地 / 在线API，切换只显示对应设置区块；样式与「故事拆解」一致）──
    c.append(makeSectionTitle("LLM 后端"));
    const backendRow = el("div", "display:flex;align-items:center;gap:16px;min-width:0;");
    const backendCbs = [];
    const mkBackendCb = (val, label) => {
        const lab = el("label", "display:flex;align-items:center;gap:6px;font-size:13px;color:var(--descrip-text,#999);cursor:pointer;");
        const c = el("input", "width:18px;height:18px;accent-color:#f59e0b;cursor:pointer;");
        c.type = "checkbox";
        c.checked = (String(p.llm_backend || "").includes("api") ? "在线API [api]" : "本地模型 [local]") === val;
        c.addEventListener("change", () => {
            if (c.checked) {
                p.llm_backend = val;
                backendCbs.forEach((x) => { if (x !== c) x.checked = false; });
                syncBackend();
            } else if (backendCbs.every((x) => !x.checked)) {
                c.checked = true;  // 保持至少一个选中
            }
        });
        lab.append(c, el("span", "", label));
        backendRow.append(lab);
        backendCbs.push(c);
        return c;
    };
    c.append(field("LLM后端", backendRow));

    // ── 本地 LLM 模型 ──
    const localBox = el("div", "");
    localBox.append(makeSectionTitle("本地 LLM 模型"));
    // 强制卸载：仅本地模型有意义（在线 API 无「驻留显存」概念）→ 放在本区块首行，选 API 时随区块一起隐藏
    localBox.append(field("强制卸载", checkboxControl(p.force_offload === true, "LLM 用完即卸载（增强开启时等增强后再卸）", v => { p.force_offload = v; })));
    const llm = p.llm || (p.llm = {});
    const llmModels = (d?.llm_models && d.llm_models.length) ? d.llm_models : ["（未找到本地 LLM 模型）"];
    const mmprojModels = (d?.mmproj_models && d.mmproj_models.length) ? d.mmproj_models : ["None"];
    const chatHandlers = (d?.chat_handlers && d.chat_handlers.length) ? d.chat_handlers : ["None"];
    // 关键修复：下拉默认显示第一个模型，但 llm.model 仍为空 → 之前点保存会存成空串触发「未选择本地 LLM 模型」。
    // 打开面板时把「所见即所得」同步进存储：不切换直接保存也会持久化当前显示的模型。
    if (!llm.model && llmModels.length && !llmModels[0].startsWith("（")) llm.model = llmModels[0];
    localBox.append(field("模型", selectControl(llmModels, llm.model || llmModels[0], v => { llm.model = v; })));
    localBox.append(field("视觉模块 mmproj", selectControl(mmprojModels, llm.mmproj || "None", v => { llm.mmproj = v; })));
    localBox.append(field("Chat Handler", selectControl(chatHandlers, llm.chat_handler || "None", v => { llm.chat_handler = v; })));
    localBox.append(field("推理后端", selectControl(["llama-cpp-python", "llama-server"], llm.backend || "llama-cpp-python", v => { llm.backend = v; })));
    localBox.append(field("上下文长度 n_ctx", numberControl(llm.n_ctx ?? 32768, { min: 1024, max: 262144, step: 128 }, v => { llm.n_ctx = Math.round(v); })));
    localBox.append(field("显存上限 vram_limit (GB)", numberControl(llm.vram_limit ?? -1, { min: -1, max: 1024, step: 1 }, v => { llm.vram_limit = Math.round(v); })));
    localBox.append(field("图像最小 tokens", numberControl(llm.image_min_tokens ?? 0, { min: 0, max: 4096, step: 32 }, v => { llm.image_min_tokens = Math.round(v); })));
    localBox.append(field("图像最大 tokens", numberControl(llm.image_max_tokens ?? 0, { min: 0, max: 4096, step: 32 }, v => { llm.image_max_tokens = Math.round(v); })));
    localBox.append(field("max_tokens", numberControl(llm.max_tokens ?? 8192, { min: 0, max: 262144, step: 1 }, v => { llm.max_tokens = Math.round(v); })));
    localBox.append(field("top_k", numberControl(llm.top_k ?? 40, { min: 0, max: 1000, step: 1 }, v => { llm.top_k = Math.round(v); })));
    localBox.append(field("top_p", numberControl(llm.top_p ?? 0.9, { min: 0, max: 1, step: 0.01 }, v => { llm.top_p = v; })));
    localBox.append(field("min_p", numberControl(llm.min_p ?? 0.05, { min: 0, max: 1, step: 0.01 }, v => { llm.min_p = v; })));
    localBox.append(field("typical_p", numberControl(llm.typical_p ?? 1.0, { min: 0, max: 1, step: 0.01 }, v => { llm.typical_p = v; })));
    localBox.append(field("temperature", numberControl(llm.temperature ?? 0.6, { min: 0, max: 2, step: 0.01 }, v => { llm.temperature = v; })));
    localBox.append(field("repeat_penalty", numberControl(llm.repeat_penalty ?? 1.05, { min: 0, max: 10, step: 0.01 }, v => { llm.repeat_penalty = v; })));
    localBox.append(field("frequency_penalty", numberControl(llm.frequency_penalty ?? 0.0, { min: 0, max: 1, step: 0.01 }, v => { llm.frequency_penalty = v; })));
    localBox.append(field("present_penalty", numberControl(llm.present_penalty ?? 0.0, { min: 0, max: 2, step: 0.01 }, v => { llm.present_penalty = v; })));
    localBox.append(field("mirostat_mode", numberControl(llm.mirostat_mode ?? 0, { min: 0, max: 2, step: 1 }, v => { llm.mirostat_mode = Math.round(v); })));
    localBox.append(field("mirostat_eta", numberControl(llm.mirostat_eta ?? 0.1, { min: 0, max: 1, step: 0.01 }, v => { llm.mirostat_eta = v; })));
    localBox.append(field("mirostat_tau", numberControl(llm.mirostat_tau ?? 5.0, { min: 0, max: 10, step: 0.01 }, v => { llm.mirostat_tau = v; })));
    localBox.append(field("GPU 设备（本地 LLM）", selectControl([
        { value: "auto", label: "跟随 ComfyUI（推荐·自动同卡）" },
        { value: "0", label: "GPU 0（手动）" },
        { value: "1", label: "GPU 1（手动）" },
        { value: "2", label: "GPU 2（手动）" },
        { value: "3", label: "GPU 3（手动）" },
    ], llm.gpu_device || "auto", v => { llm.gpu_device = v; })));
    c.append(localBox);

    // ── 在线 API ──
    const apiBox = el("div", "");
    apiBox.append(makeSectionTitle("在线 API"));
    const api = p.api || (p.api = {});
    apiBox.append(field("服务商", selectControl(OPTIONS.providers, api.provider || OPTIONS.providers[0], v => { api.provider = v; })));
    apiBox.append(field("模型", textControl(api.model || "deepseek-flash", "deepseek-flash…", v => { api.model = v; })));
    apiBox.append(field("API Key", passwordControl(api.api_key || "", "sk-…（已隐藏）", v => { api.api_key = v; })));
    apiBox.append(field("Base URL", textControl(api.base_url || "https://api.deepseek.com", "https://api.deepseek.com", v => { api.base_url = v; })));
    apiBox.append(field("temperature", numberControl(api.temperature ?? 0.6, { min: 0, max: 2, step: 0.01 }, v => { api.temperature = v; })));
    apiBox.append(field("max_tokens", numberControl(api.max_tokens ?? 81920, { min: 1, max: 262144, step: 1 }, v => { api.max_tokens = Math.round(v); })));
    apiBox.append(field("thinking", selectControl(["disabled", "enabled"], api.thinking || "disabled", v => { api.thinking = v; })));
    c.append(apiBox);

    const syncBackend = () => {
        const isApi = String(p.llm_backend || "").includes("api");
        localBox.style.display = isApi ? "none" : "block";
        apiBox.style.display = isApi ? "block" : "none";
    };
    mkBackendCb("本地模型 [local]", "本地模型");
    mkBackendCb("在线API [api]", "在线API");
    syncBackend();

    // ── 随机种子（剧本处理器与提示词增强共享，含生成后控制） ──
    c.append(makeSectionTitle("随机种子"));
    c.append(seedControlRow(p));

    // ── 指令推理 ──
    c.append(makeSectionTitle("指令推理"));
    c.append(field("自定义提示词", textControl(p.custom_prompt || "", "选填：自定义增强指令…", v => { p.custom_prompt = v; })));
    c.append(field("系统提示词", textControl(p.system_prompt || "", "选填…", v => { p.system_prompt = v; })));
    c.append(field("推理模式", selectControl(OPTIONS.inferenceModes, p.inference_mode || OPTIONS.inferenceModes[0], v => { p.inference_mode = v; })));
    c.append(field("最大帧数", numberControl(p.max_frames ?? 24, { min: 2, max: 1024, step: 1 }, v => { p.max_frames = Math.round(v); })));
    c.append(field("最大尺寸", numberControl(p.max_size ?? 256, { min: 128, max: 16384, step: 64 }, v => { p.max_size = Math.round(v); })));
}

// ── 共用控件：互斥勾选组（每个选项一行：前面名称、后面勾选与说明；点选即切换，不允许全不选）──
function checkboxGroup(options, value, onChange) {
    const box = el("div", "display:flex;flex-direction:column;gap:4px;margin-bottom:10px;");
    const items = [];
    for (const o of options) {
        const val = (typeof o === "object" && o && o.value !== undefined) ? String(o.value) : String(o);
        const txt = (typeof o === "object" && o && o.label) ? String(o.label) : val;
        const cb = checkboxControl(String(value) === val, txt, (on) => {
            const i = cb.querySelector("input");
            if (!on) { if (i) i.checked = true; return; }   // 单选语义：至少保留一项
            for (const [, b2, v2] of items) {
                const i2 = b2.querySelector("input");
                if (i2) i2.checked = (v2 === val);
            }
            onChange(val);
        });
        items.push([txt, cb, val]);
        box.append(cb);
    }
    return box;
}

// ── 共用控件：多宫格方框勾选（选中橙色高亮，与节点内勾选框同色系）──
// BOX_SEL / BOX_IDLE：选中 / 未选中的边框与底色
const BOX_SEL = { border: "#f59e0b", bg: "#8a5208", fg: "#fff" };
const BOX_IDLE = { border: "#555", bg: "#2a2a2a", fg: "#ddd" };
function _boxPaint(b, on) {
    const c = on ? BOX_SEL : BOX_IDLE;
    b.style.borderColor = c.border;
    b.style.background = c.bg;
    b.style.color = c.fg;
}
function bigBoxGrid(options, value, onChange, cols, height) {
    const grid = el("div", `display:grid;grid-template-columns:repeat(${cols || 5},1fr);gap:8px;margin-bottom:10px;`);
    const cells = [];
    for (const o of options) {
        const val = String(o);
        const b = el("button", `height:${height || 26}px;box-sizing:border-box;border-radius:6px;border:2px solid ${BOX_IDLE.border};`
            + `background:${BOX_IDLE.bg};color:${BOX_IDLE.fg};font-size:13px;font-weight:600;cursor:pointer;`
            + "display:flex;align-items:center;justify-content:center;white-space:nowrap;overflow:hidden;"
            + "text-overflow:ellipsis;padding:0 6px;");
        b.type = "button";
        b.textContent = val.split(" ")[0];   // 短名（完整名在悬停提示里）
        b.title = val;
        b.addEventListener("mousedown", (e) => e.stopPropagation());
        b.addEventListener("click", () => {
            for (const [v2, b2] of cells) _boxPaint(b2, v2 === val);
            onChange(val);
        });
        if (val === String(value)) _boxPaint(b, true);
        cells.push([val, b]);
        grid.append(b);
    }
    return grid;
}

// ── 共用控件：左右三角步进（◀ 数值 ▶；隐藏原生上下小箭头）──
function stepperControl(value, opts, onChange) {
    const o = opts || {};
    const step = (o.step !== undefined) ? Number(o.step) : 1;
    const dig = String(step).includes(".") ? String(step).split(".")[1].length : 0;
    const wrap = el("div", "display:flex;align-items:center;gap:6px;flex:1 1 0;min-width:0;");
    const mkBtn = (dir) => {
        const b = el("button", "width:32px;height:30px;flex:0 0 auto;border-radius:5px;border:1px solid #555;"
            + "background:#2a2a2a;color:#9fd6a4;font-size:13px;line-height:1;cursor:pointer;"
            + "display:flex;align-items:center;justify-content:center;");
        b.type = "button";
        b.textContent = dir < 0 ? "◀" : "▶";
        b.title = dir < 0 ? `减小 ${step}` : `增大 ${step}`;
        b.addEventListener("mousedown", (e) => e.stopPropagation());
        return b;
    };
    const left = mkBtn(-1), right = mkBtn(1);
    const inp = el("input", "flex:1 1 0;min-width:0;text-align:center;");
    inp.className = "jzl-input jzl-count-input";   // jzl-count-input：隐藏原生上下箭头
    inp.type = "number";
    inp.value = value;
    if (o.min !== undefined) inp.min = o.min;
    if (o.max !== undefined) inp.max = o.max;
    inp.step = step;
    inp.addEventListener("mousedown", (e) => e.stopPropagation());
    const apply = (raw) => {
        let n = Number(raw);
        if (!isFinite(n)) n = (o.min !== undefined) ? o.min : 0;
        if (o.min !== undefined) n = Math.max(o.min, n);
        if (o.max !== undefined) n = Math.min(o.max, n);
        n = Number(Number(n).toFixed(dig));   // 消除 0.30000000000000004 类浮点误差
        inp.value = n;
        onChange(n);
    };
    left.addEventListener("click", () => apply(Number(inp.value) - step));
    right.addEventListener("click", () => apply(Number(inp.value) + step));
    inp.addEventListener("change", () => apply(inp.value));
    wrap.append(left, inp, right);
    return wrap;
}

// ── 弹窗内读写节点表面 widget 的通用辅助（🎬 短剧导演台：运行模式/风格/名称/视频参数）──
function panelWidgetIO(node) {
    const wOf = (n) => (node?.widgets || []).find((x) => x.name === n);
    const cur = (n, dflt) => {
        const v = readWidgetValue(wOf(n));
        return (v === undefined || v === null || v === "") ? dflt : v;
    };
    const set = (n, v) => {
        const w = wOf(n);
        if (!w) return;
        setWidgetValue(w, v);
        try { w.callback?.call(w, v); } catch (_) {}
        try { node?.__jzlUpdateDisplay?.(); } catch (_) {}
        try { node?.graph?.setDirtyCanvas?.(true, true); } catch (_) {}
    };
    const comboOptions = (n, fallback) => {
        const o = wOf(n)?.options;
        let arr = [];
        if (Array.isArray(o)) arr = o;
        else if (o && Array.isArray(o.values)) arr = o.values;
        return arr.length ? arr.slice() : (fallback || []);
    };
    return { cur, set, comboOptions };
}

function renderPreferenceSettingsPanel(c, s) {
    const p = s.enhance.preference || (s.enhance.preference = {});
    renderAutoSave(c, s);
    // 每组选项改成「多宫格方框勾选」（与 📐 视频参数 同一交互，选中橙色高亮）
    const rows = [
        ["🎞️ 景别偏好", "shot_size", OPTIONS.shotSize],
        ["🔭 视角机位", "angle_view", OPTIONS.angleView],
        ["🎥 运镜偏好", "camera_move", OPTIONS.cameraMove],
        ["✂️ 切镜节奏", "cut_rhythm", OPTIONS.cutRhythm],
        ["🔀 转场偏好", "transition", OPTIONS.transition],
        ["🎵 音乐风格", "music_style", OPTIONS.music],
        ["💡 创作要求", "creative_req", OPTIONS.creativeReq],
        ["📏 详细描述字数", "detail_length", OPTIONS.detailLength],
    ];
    for (const [label, key, opts] of rows) {
        c.append(makeSectionTitle(label));
        c.append(bigBoxGrid(opts, p[key] || opts[0], v => { p[key] = v; }, 4, 24));
    }
    c.append(makeSectionTitle("✍️ 自定义镜头语言"));
    c.append(field("自定义镜头语言", textControl(p.custom || "", "选填。自由描述镜头要求…", v => { p.custom = v; })));
}

// ── 🎛️ 生成设定（🎬 短剧导演台）：运行模式 / 故事风格（勾选 + 说明）· 故事名称 ──
// 运行模式注释（每条字数保持一致，便于扫读；与「🤖 大语言模型配置 → 开关」同一形式展示）
const RUN_MODE_TIPS = {
    "🛠️ 故事拆解模式": "使用 LLM 模型，对提示词按设定拆解，然后采样生成视频。",
    "📚 故事扩写模式": "使用 LLM 模型，把故事扩写为丰满正文，只输出文本不生成视频。",
    "🪄 穿透生成模式": "跳过 LLM 模型，直接用现有提示词，按提示词段数出片。",
    "📝 仅提示词输出": "使用 LLM 模型，拆解并增强提示词，只输出剧本不生成视频。",
};

// 故事风格注释（每条字数保持一致，便于扫读；与运行模式注释同一形式展示）
const STORY_STYLE_TIPS = {
    // 「禁用风格」只是不做风格侧重，拆解与增强仍会正常执行（不要误当成增强开关）
    "禁用风格": "不做风格侧重（纯按剧情与镜头偏好），仍会正常拆解与增强",
    "热血战斗": "适合竞技/武侠/修仙/末世",
    "悬疑惊悚": "适合推理/恐怖/犯罪/悬疑",
    "温馨日常": "适合生活/亲情/治愈系",
    "甜宠爽文": "适合都市/言情/逆袭打脸",
    "宏大奇观": "适合科幻/奇幻/大场面",
    "乡土喜乐": "适合乡村/喜剧/民俗生活",
    "歌神舞台": "适合音乐/舞台/歌舞表演",
};

// ── Skill 选择（强化层：拆解 / 扩写 / 故事风格）─────────────────────
// 内置 skill 随插件发布；自定义 skill 存 ComfyUI user/jzl_skills/<kind>/（不随插件升级丢失）
// 分层：skill 只改「怎么强化」，改不了输出格式与调度指令（强制层永远最后生效）。
const JZL_SKILL_KEYS = {
    mode: ["mode_source", "mode_skill"],
    expand: ["expand_source_mode", "expand_skill"],
    style: ["style_source", "style_skill"],
};
const JZL_SKILL_LABEL = { mode: "拆解 skill", expand: "扩写 skill", style: "故事风格 skill" };
const JZL_SKILL_CACHE = { mode: null, expand: null, style: null };

async function jzlFetchSkills(kind) {
    try {
        const r = await api.fetchApi(`/jzl/skills?kind=${encodeURIComponent(kind)}`);
        const j = await r.json();
        if (j && j.ok) {
            JZL_SKILL_CACHE[kind] = j.items || [];
            return { ok: true, items: JZL_SKILL_CACHE[kind], dirs: j.dirs || {} };
        }
        return { ok: false, items: [], error: (j && j.error) || "读取失败" };
    } catch (e) {
        return { ok: false, items: [], error: String((e && e.message) || e) };
    }
}

async function jzlImportSkill(kind, file) {
    const fd = new FormData();
    fd.append("kind", kind);
    fd.append("file", file, file.name || "skill.txt");
    const r = await api.fetchApi("/jzl/skills/import", { method: "POST", body: fd });
    return await r.json();
}

async function jzlReadSkill(kind, id) {
    const r = await api.fetchApi(`/jzl/skills/read?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`);
    return await r.json();
}

function _jzlSmallBtn(text, title) {
    const b = el("button", "flex:0 0 auto;padding:6px 10px;border-radius:5px;border:1px solid #555;"
        + "background:#2a2a2a;color:#ddd;font-size:12px;cursor:pointer;white-space:nowrap;", text);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("mousedown", (e) => e.stopPropagation());
    return b;
}

// ── 运行参数面板的视觉分组：让「分类 → 子选项」一眼可分 ──
// 分类 = 卡片（浅底+描边）；子选项 = 卡片内带橙色左条的缩进区。
function jzlPanelCard() {
    return el("div", "background:rgba(255,255,255,.025);border:1px solid #3a3a3a;border-radius:8px;"
        + "padding:12px 12px 4px;margin:0 0 14px;");
}

/** 可选 chip（带边框的方框勾选，选中橙色高亮）。返回元素，元素上挂 __set(on) 用于外部同步。 */
function jzlChoiceChip(label, checked, onChange, title) {
    const chip = el("label", "display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:6px;"
        + "border:2px solid " + (checked ? BOX_SEL.border : BOX_IDLE.border) + ";"
        + "background:" + (checked ? BOX_SEL.bg : BOX_IDLE.bg) + ";color:" + (checked ? BOX_SEL.fg : BOX_IDLE.fg) + ";"
        + "font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;user-select:none;");
    if (title) chip.title = title;
    const cb = el("input", "width:16px;height:16px;accent-color:#f59e0b;cursor:pointer;margin:0;");
    cb.type = "checkbox";
    cb.checked = !!checked;
    cb.addEventListener("change", () => onChange(cb.checked));
    chip.append(cb, el("span", "", label));
    chip.__set = (on) => {
        cb.checked = !!on;
        _boxPaint(chip, !!on);
    };
    return chip;
}

/** 子选项区：橙色左条 + 淡色底 + 小标题，与上层「分类」形成清晰层级。 */
function jzlSubPanel(labelText, hintText) {
    const box = el("div", "margin:10px 0 12px;padding:8px 12px 2px;border-left:3px solid #f59e0b;"
        + "background:rgba(245,158,11,.06);border-radius:0 6px 6px 0;");
    if (labelText) {
        box.append(el("div", "font-size:12px;font-weight:600;color:#f0b64a;margin-bottom:6px;letter-spacing:.02em;", labelText));
    }
    if (hintText) {
        box.append(el("div", "font-size:12px;color:var(--descrip-text,#999);line-height:1.7;margin-bottom:8px;white-space:pre-line;", hintText));
    }
    return box;
}

/**
 * 强化层 skill 选择器：内置 / 自定义 + 选择 skill + 导入 + 预览框。
 *
 * bare=true 时只渲染「选择 skill / 导入 / 预览」三件套（调用方已自带 内置/自定义 开关，
 * 例如「故事风格」），避免出现套娃的内置/自定义勾选。
 */
function skillSelector(kind, s, mark, opts) {
    const bare = !!(opts && opts.bare);
    const sk = s.skills || (s.skills = {});
    const [srcKey, idKey] = JZL_SKILL_KEYS[kind];
    const label = JZL_SKILL_LABEL[kind] || "skill";
    const wrap = bare ? el("div", "margin:0 0 4px;") : jzlSubPanel(`本模式的强化设置（${label}）`);
    const userMode = () => bare || String(sk[srcKey] || "builtin") === "user";

    const row = el("div", "display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;");
    const customBox = el("div", "");

    let sel, hint, preview, previewBtn;
    let chipBuiltin = null, chipCustom = null;
    const sync = () => {
        const isUser = String(sk[srcKey] || "builtin") === "user";
        if (chipBuiltin) chipBuiltin.__set(!isUser);
        if (chipCustom) chipCustom.__set(isUser);
        customBox.style.display = userMode() ? "" : "none";
    };

    if (!bare) {
        chipBuiltin = jzlChoiceChip(`内置 ${label}`, !userMode(), (on) => {
            if (!on) { sync(); return; }            // 单选语义：不允许全不选
            sk[srcKey] = "builtin";
            sync(); mark();
        }, "使用随插件发布的内置设定词（严格保留现有行为）");
        chipCustom = jzlChoiceChip("自定义 skill", userMode(), (on) => {
            if (!on) { sync(); return; }
            sk[srcKey] = "user";
            sync(); mark();
            loadList();
        }, "使用你自己导入的 skill（下拉选择 / 导入 / 预览）");
        row.append(chipBuiltin, chipCustom);
    }

    // 选择 skill（下拉）→ 导入 → 预览框
    sel = selectControl([], "", (v) => { sk[idKey] = v; mark(); syncPreview(); });
    const importBtn = _jzlSmallBtn("📥 导入", "上传本地 .txt / .md 作为自定义 skill");
    const btnRow = el("div", "display:flex;align-items:center;gap:8px;margin:0 0 6px;");
    previewBtn = _jzlSmallBtn("👁 预览", "查看所选 skill 的完整内容（只读）");
    btnRow.append(importBtn, previewBtn);
    hint = el("div", "font-size:12px;color:var(--descrip-text,#999);line-height:1.7;margin:0 0 6px;white-space:pre-line;");
    preview = el("pre", "display:none;margin:0 0 8px;padding:8px 10px;max-height:220px;overflow:auto;"
        + "background:var(--comfy-input-bg,#1d1d1d);border:1px solid var(--border-color,#444);border-radius:4px;"
        + "font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-word;");
    customBox.append(field("选择 skill", sel), btnRow, hint, preview);
    wrap.append(row, customBox);

    const syncPreview = () => {
        const has = !!sk[idKey];
        previewBtn.disabled = !has;
        previewBtn.style.opacity = has ? "" : "0.5";
    };

    const loadList = async (focusId) => {
        const res = await jzlFetchSkills(kind);
        const users = (res.items || []).filter((it) => it.source === "user");
        sel.innerHTML = "";
        if (!users.length) {
            const op = el("option", "", "（还没有自定义 skill，点「📥 导入」添加）");
            op.value = "";
            sel.append(op);
            sel.disabled = true;
            hint.textContent = res.ok
                ? `存放位置：${(res.dirs && res.dirs.user) || "ComfyUI/user/jzl_skills/"}`
                : `读取 skill 列表失败：${res.error || "未知错误"}`;
            sk[idKey] = "";
            syncPreview();
            return;
        }
        sel.disabled = false;
        for (const it of users) {
            const op = el("option", "", it.description ? `${it.name} — ${it.description}` : it.name);
            op.value = it.id;
            op.title = it.path || "";
            sel.append(op);
        }
        const wanted = focusId || sk[idKey];
        if (wanted && users.some((it) => it.id === wanted)) {
            sk[idKey] = wanted;
        } else if (userMode() || focusId) {
            sk[idKey] = users[0].id;      // 选中项已失效 → 落到第一个可用 skill
        }
        sel.value = sk[idKey] || users[0].id;
        hint.textContent = `存放位置：${(res.dirs && res.dirs.user) || "ComfyUI/user/jzl_skills/"}\n`
            + "skill 只影响「怎么强化」，不会改变输出格式与调度指令。";
        syncPreview();
    };

    importBtn.addEventListener("click", () => {
        const inp = document.createElement("input");
        inp.type = "file";
        inp.accept = ".txt,.md,.markdown,text/plain,text/markdown";
        inp.style.display = "none";
        inp.addEventListener("change", async () => {
            const file = (inp.files || [])[0];
            try { document.body.removeChild(inp); } catch (_) {}
            if (!file) return;
            hint.textContent = `正在导入「${file.name}」…`;
            const res = await jzlImportSkill(kind, file);
            if (!res || !res.ok) {
                hint.textContent = `导入失败：${(res && res.error) || "未知错误"}`;
                return;
            }
            JZL_SKILL_CACHE[kind] = res.items || [];
            sk[srcKey] = "user";
            sk[idKey] = res.id || "";
            sync();
            await loadList(res.id);
            mark();
            hint.textContent = `已导入「${res.id.replace(/^user:/, "")}」（已自动选中）`;
        });
        document.body.append(inp);
        inp.click();
    });

    previewBtn.addEventListener("click", async () => {
        if (preview.style.display !== "none") { preview.style.display = "none"; return; }
        const id = sk[idKey];
        if (!id) return;
        preview.textContent = "读取中…";
        preview.style.display = "block";
        const res = await jzlReadSkill(kind, id);
        preview.textContent = (res && res.ok) ? res.text : `读取失败：${(res && res.error) || "未知错误"}`;
    });

    sync();
    syncPreview();
    loadList();      // 始终预取列表：用户随时切到「自定义」都能立刻看到下拉内容
    return wrap;
}

/** 穿透生成模式：严格对齐 / 自由模式。 */
function passthroughSelector(s, mark) {
    const sk = s.skills || (s.skills = {});
    const wrap = jzlSubPanel("本模式的校验方式");
    const row = el("div", "display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;");
    const isFree = () => String(sk.passthrough_mode || "strict") === "free";
    let chipStrict, chipFree;
    const sync = () => {
        const free = isFree();
        chipStrict.__set(!free);
        chipFree.__set(free);
    };
    chipStrict = jzlChoiceChip("严格对齐", !isFree(), (on) => {
        if (!on) { sync(); return; }
        sk.passthrough_mode = "strict"; sync(); mark();
    }, "提示词必须符合现有格式要求（[SHOT_START] + ===H3_PROMPT===），否则报错终止");
    chipFree = jzlChoiceChip("自由模式", isFree(), (on) => {
        if (!on) { sync(); return; }
        sk.passthrough_mode = "free"; sync(); mark();
    }, "无任何约束：跳过格式校验，有分段块就按段数生成，没有就整段按 1 段生成");
    row.append(chipStrict, chipFree);
    wrap.append(row, el("div", "font-size:12px;color:var(--descrip-text,#999);line-height:1.7;margin:0 0 8px;white-space:pre-line;",
        "严格对齐：必须含 [SHOT_START]…[SHOT_END] 与 ===H3_PROMPT===，否则报错终止。\n"
        + "自由模式：跳过格式校验；有分段块就按段数生成，没有就整段按 1 段生成。"));
    sync();
    return wrap;
}

function renderGenSettingsPanel(c, s, node, d) {
    const { cur, set, comboOptions } = panelWidgetIO(node);
    renderAutoSave(c, s);
    const sk = s.skills || (s.skills = {});
    const mark = () => c.dispatchEvent(new Event("change", { bubbles: true }));   // 触发面板自动保存

    // ── 分类顺序：故事名称 → 运行模式 → 故事风格 → 语义修正 ──
    c.append(makeSectionTitle("故事名称"));
    c.append(field("故事名称", textControl(String(cur("story_name", "")),
        "用于保存命名 / output/jzl/{故事名} / 生成视频管理", v => set("story_name", v))));

    // ── 运行模式：一排 chip（单选）→ 选中项下方才展开该模式的强化设置 ──
    c.append(makeSectionTitle("运行模式"));
    const modes = comboOptions("run_mode", Object.keys(RUN_MODE_TIPS));
    const curMode = String(cur("run_mode", ""));
    const sameMode = (a, b) => jzlNormalizeRunMode(a) === jzlNormalizeRunMode(b);
    const tipFor = (m) => RUN_MODE_TIPS[m]
        || RUN_MODE_TIPS[Object.keys(RUN_MODE_TIPS).find((k) => sameMode(k, m))]
        || "（无说明）";

    const modeCard = jzlPanelCard();
    const modeRow = el("div", "display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;");
    const modeChips = [];
    const modeHint = el("div", "font-size:12px;color:var(--descrip-text,#999);line-height:1.7;margin:0 0 2px;");
    const subBox = el("div", "");
    let subKind = null;
    const syncModeChips = () => {
        const now = cur("run_mode", "");
        for (const [v, chip] of modeChips) chip.__set(sameMode(v, now));
    };
    const renderSub = () => {
        const nm = jzlNormalizeRunMode(cur("run_mode", ""));
        modeHint.textContent = tipFor(modes.find((m) => sameMode(m, cur("run_mode", ""))) || nm);
        const kind = (nm === "故事拆解模式" || nm === "仅提示词输出") ? "mode"
            : (nm === "故事扩写模式") ? "expand"
                : (nm === "穿透生成模式") ? "pass" : "";
        if (kind !== subKind) {
            subKind = kind;
            subBox.innerHTML = "";
            if (kind === "mode") subBox.append(skillSelector("mode", s, mark));
            else if (kind === "expand") subBox.append(skillSelector("expand", s, mark));
            else if (kind === "pass") subBox.append(passthroughSelector(s, mark));
        }
    };
    for (const m of modes) {
        const chip = jzlChoiceChip(m, sameMode(curMode, m), (on) => {
            if (!on) { syncModeChips(); return; }   // 单选语义：至少保留一项
            // 优先写回下拉里真实存在的选项字符串（表情/变体符不一致时也能命中）
            const actual = modes.find((o) => sameMode(o, m)) || m;
            set("run_mode", actual);
            syncModeChips();
            renderSub();
        }, tipFor(m));
        modeChips.push([m, chip]);
        modeRow.append(chip);
    }
    modeCard.append(modeRow, modeHint, subBox);
    c.append(modeCard);
    renderSub();

    // ── 故事风格：内置 8 风格 / 自定义 skill（chip 切换，内容区缩进）──
    c.append(makeSectionTitle("故事风格"));
    const styleCard = jzlPanelCard();
    const styleRow = el("div", "display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;");
    const builtinStyleBox = el("div", "");
    const customStyleBox = el("div", "");
    const styleUser = () => String(sk.style_source || "builtin") === "user";
    let chipStyleB, chipStyleC;
    const syncStyle = () => {
        const u = styleUser();
        chipStyleB.__set(!u);
        chipStyleC.__set(u);
        builtinStyleBox.style.display = u ? "none" : "";
        customStyleBox.style.display = u ? "" : "none";
    };
    chipStyleB = jzlChoiceChip("内置风格", !styleUser(), (on) => {
        if (!on) { syncStyle(); return; }
        sk.style_source = "builtin"; syncStyle(); mark();
    }, "使用内置的 8 种故事风格");
    chipStyleC = jzlChoiceChip("自定义 skill", styleUser(), (on) => {
        if (!on) { syncStyle(); return; }
        sk.style_source = "user"; syncStyle(); mark();
    }, "使用导入的故事风格 skill（下拉 / 导入 / 预览）");
    styleRow.append(chipStyleB, chipStyleC);
    // 生效范围注释（故事风格的实际作用点）
    const styleNote = el("div", "font-size:12px;color:#c9a86a;line-height:1.7;margin:0 0 8px;white-space:pre-line;",
        "⚠️ 仅在开启「提示词增强」时才真正决定成片质感（增强把故事风格作为最高权重锚点，逐句落到画面）；"
        + "拆解阶段只作导演视角参考，故事扩写阶段作为扩写基调。");
    const styleContent = el("div", "padding-left:10px;border-left:2px solid #3a3a3a;");
    // 内置风格：名称 + 注释（勾选框在前）；未收录的风格只显示名称
    const styleOpts = comboOptions("story_style", []).map((v) => {
        const name = String(v);
        const tip = STORY_STYLE_TIPS[name];
        return { value: name, label: tip ? `${name}：${tip}` : name };
    });
    builtinStyleBox.append(checkboxGroup(styleOpts, String(cur("story_style", "")), v => set("story_style", v)));
    // 自定义风格：只渲染「选择 skill / 导入 / 预览」，不再嵌套第二层内置/自定义
    customStyleBox.append(skillSelector("style", s, mark, { bare: true }));
    styleContent.append(builtinStyleBox, customStyleBox);
    styleCard.append(styleRow, styleNote, styleContent);
    c.append(styleCard);
    syncStyle();

    // ── 语义修正（语义桥）：勾选决定开启；内部调用内置副本，不依赖用户另装该节点包 ──
    const sb = s.semantic_bridge || (s.semantic_bridge = { enabled: false, adapter: "", alpha: 0.10, magnitude_match: "per_token" });
    c.append(makeSectionTitle("语义修正"));
    c.append(field("开启语义桥", checkboxControl(!!sb.enabled,
        "增强语义结构和增加提示遵循度（需要 models/semantic_bridge/ 下的适配器）",
        v => { sb.enabled = !!v; syncSemantic(); })));
    // 适配器：只读磁盘 models/semantic_bridge/ 的实时列表（含二级子目录）——与上游节点下拉同口径。
    // 磁盘上一个都没有时不伪造假项，显示说明并置灰（与「二采 latent放大模型」同一策略）。
    const sbAdapters = (d && Array.isArray(d.semantic_bridge_adapters)) ? d.semantic_bridge_adapters : [];
    const _SB_NONE = "（未找到适配器：请把 MiniMaxH3_SemanticBridge_v1.safetensors 放入 models/semantic_bridge/）";
    const sbCur = sbAdapters.includes(sb.adapter) ? sb.adapter : (sbAdapters[0] || _SB_NONE);
    if (sbAdapters.length && sbCur !== sb.adapter) sb.adapter = sbCur;   // 旧值失效 → 落到磁盘上可用的那个并写回
    const sbSel = selectControl(sbAdapters.length ? sbAdapters : [_SB_NONE], sbCur, v => {
        if (!sbAdapters.length) return;
        sb.adapter = v;
    });
    if (!sbAdapters.length) { try { sbSel.disabled = true; sbSel.style.opacity = "0.6"; } catch (_) {} }
    const fAdapter = field("适配器", sbSel);
    const fAlpha = field("修正强度 alpha", stepperControl(Number(sb.alpha ?? 0.10), { min: 0, max: 1, step: 0.01 },
        v => { sb.alpha = v; }));
    const fMatch = field("幅度匹配", bigBoxGrid(["per_token", "global", "none"], String(sb.magnitude_match || "per_token"),
        v => { sb.magnitude_match = v; }, 3, 26));
    const syncSemantic = () => {
        const on = !!sb.enabled;
        for (const f of [fAdapter, fAlpha, fMatch]) f.style.display = on ? "flex" : "none";
    };
    c.append(fAdapter, fAlpha, fMatch);
    syncSemantic();
}

// ── 📐 视频参数（🎬 短剧导演台）：画幅比例 / 百万像素 / 每段时长 / 段数 / 参考与放大 ──
// 画幅比例表：与后端 nodes_asset_manager.ASPECT_RATIOS 保持一致
const JZL_ASPECT_RATIOS = {
    "1:1 (Square)": [1, 1], "2:3 (Portrait Photo)": [2, 3], "3:2 (Photo)": [3, 2],
    "3:4 (Portrait Standard)": [3, 4], "4:5 (Portrait Tall)": [4, 5], "4:3 (Standard)": [4, 3],
    "5:4 (Landscape Tall)": [5, 4], "9:16 (Portrait Widescreen)": [9, 16],
    "16:9 (Widescreen)": [16, 9], "21:9 (Ultrawide)": [21, 9],
};

// 画幅比例 + 百万像素 → 生成分辨率（与后端 _resolve_gen_size 同一公式：sqrt(MP×1024²/比例) 后按 32 对齐）
function jzlCalcResolution(aspectRatio, megapixels) {
    const [wr, hr] = JZL_ASPECT_RATIOS[String(aspectRatio || "")] || [16, 9];
    const mp = Number(megapixels);
    const total = (Number.isFinite(mp) && mp > 0 ? mp : 1) * 1024 * 1024;
    const scale = Math.sqrt(total / (wr * hr));
    const align = (v) => Math.max(32, Math.round((v * scale) / 32) * 32);
    return [align(wr), align(hr)];
}

// ══════════════════════════════════════════════════════════════
//  🎭 人物替换（JZL_MiniMaxCharacterSwap）专用面板
//  选项全部收进 3 个按钮：🎨 视频参数设置 / 🎭 采样解码设置 / 🎬 生成视频管理
//  写的是 manager_settings 里的 gen_params / sample_decode / save —— 与后端
//  _parse_swap_settings() 同一套键名，不做任何翻译（避免「面板显示一套、实际跑另一套」）。
// ══════════════════════════════════════════════════════════════
const SWAP_SEGMENT_OPTIONS = Array.from({ length: 17 }, (_, i) => `固定${i + 4}秒`);
const SWAP_AUDIO_OPTIONS = ["保留原声（推荐）", "使用生成音轨", "静音"];
const SWAP_WINDOW_OPTIONS = [
    { value: 5, label: "5 帧（≈0.21s，最省）" },
    { value: 22, label: "22 帧（≈0.92s，推荐）" },
    { value: 39, label: "39 帧（≈1.63s）" },
    { value: 56, label: "56 帧（≈2.33s，最稳最慢）" },
];
const SWAP_SETTLE_OPTIONS = [
    { value: 12, label: "12 帧（推荐）" },
    { value: 29, label: "29 帧（衔接仍卡时用）" },
    { value: 46, label: "46 帧（最顽固时用）" },
];

function swapFrameLen(segmentText) {
    // 与后端 _resolve_length 同规则：秒 → 24fps 帧数 → 吸附到 17k+5
    const m = String(segmentText || "").match(/(\d+(?:\.\d+)?)/);
    let n = m ? Math.max(5, Math.round(parseFloat(m[1]) * 24)) : 124;
    while (n % 17 !== 5) n += 1;
    return n;
}

function renderSwapVideoParamsPanel(c, s, node) {
    const gp = s.gen_params || (s.gen_params = {});
    renderAutoSave(c, s);
    const touch = () => { try { node?.__jzlUpdateDisplay?.(); } catch (_) {} };

    c.append(makeSectionTitle("画幅比例"));
    c.append(bigBoxGrid(["跟随源片（推荐）"].concat(OPTIONS.aspectRatio),
        String(gp.aspect_ratio || "跟随源片（推荐）"), v => { gp.aspect_ratio = v; refreshRes(); touch(); }, 5, 26));

    c.append(makeSectionTitle("分辨率与段长"));
    c.append(field("百万像素（MP）", stepperControl(Number(gp.megapixels ?? 0.4),
        { min: 0.1, max: 3.6, step: 0.1 }, v => { gp.megapixels = v; refreshRes(); touch(); })));

    const resBox = el("div", "font-size:13px;color:#c9a86a;padding:6px 10px;text-align:center;"
        + "background:var(--comfy-input-bg,#2a2a2a);border:1px solid var(--border-color,#444);"
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
    };
    refreshRes();

    c.append(field("每段时长", selectControl(SWAP_SEGMENT_OPTIONS, String(gp.segment || "固定5秒"),
        v => { gp.segment = v; showSegLen(); touch(); })));
    const segHint = el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;", "");
    c.append(segHint);
    const showSegLen = () => {
        const f = swapFrameLen(gp.segment || "固定5秒");
        segHint.textContent = `每段生成 ${f} 帧 = ${(f / 24).toFixed(2)} 秒（自动对齐到模型支持的帧数）。`
            + "段越短接缝越多、越慢；5~8 秒最稳。这只是切段粒度，成片总时长等于源片。";
    };
    showSegLen();

    c.append(makeSectionTitle("音频"));
    c.append(field("音频方式", selectControl(SWAP_AUDIO_OPTIONS, String(gp.audio_mode || SWAP_AUDIO_OPTIONS[0]),
        v => { gp.audio_mode = v; touch(); })));
    c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;",
        "保留原声 = 成片用源片原声（对白/音效不变，推荐；同时把原声作为 <Video 1> 音轨参考）。\n"
        + "使用生成音轨 = 用模型重新生成的声音（台词/节奏可能与原片不同）。静音 = 不要音轨。"));

    c.append(makeSectionTitle("参考与放大"));
    c.append(field("参考图放大", stepperControl(Number(gp.scale_factor ?? 1.0),
        { min: 1.0, max: 5.0, step: 0.1 }, v => { gp.scale_factor = v; })));
    c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;",
        "参考图面积 = 画布面积 × 倍率（1.0 = 同面积，2.0 = 面积翻倍）。脸不够像就加，越大越慢。"));

    c.append(makeSectionTitle("二采放大"));
    c.append(field("二采放大方式", selectControl(
        [{ value: "目标分辨率", label: "目标分辨率（推荐，1280×704）" },
         { value: "放大倍数", label: "放大倍数（按一采画布乘倍数）" }],
        String(gp.second_mode || "目标分辨率"), v => { gp.second_mode = v; syncSecMode(); touch(); })));
    const fSecW = field("二采目标宽度", stepperControl(Number(gp.second_width ?? 1280),
        { min: 32, max: 4096, step: 32 }, v => { gp.second_width = Math.round(v); touch(); }));
    const fSecH = field("二采目标高度", stepperControl(Number(gp.second_height ?? 704),
        { min: 32, max: 4096, step: 32 }, v => { gp.second_height = Math.round(v); touch(); }));
    const fSecScale = field("二采latent放大", stepperControl(Number(gp.upscale_scale ?? 1.0),
        { min: 1.0, max: 4.0, step: 0.05 }, v => { gp.upscale_scale = v; touch(); }));
    c.append(fSecW, fSecH, fSecScale);
    const syncSecMode = () => {
        const isTgt = String(gp.second_mode || "目标分辨率") === "目标分辨率";
        fSecW.style.display = isTgt ? "flex" : "none";
        fSecH.style.display = isTgt ? "flex" : "none";
        fSecScale.style.display = isTgt ? "none" : "flex";
    };
    syncSecMode();
    c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;",
        "换人脸是小目标，建议 1280×704。二采目标比一采画布还小只会更糊（节点会提示）。"));

    c.append(makeSectionTitle("安全阀"));
    c.append(field("最大段数", stepperControl(Number(gp.max_segments ?? 60),
        { min: 1, max: 200, step: 1 }, v => { gp.max_segments = Math.round(v); })));
    c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;",
        "源片切出的段数超过它就直接报错（避免误接一部两小时的片子跑一晚上）。"));
}

function renderSwapSeamSection(c, s, node) {
    // 🎭 人物替换独有：分段衔接。放在「100% 复刻的采样解码设置」之后，
    //    语义与「无限时长衔接」同源，但接的是「上一段生成结果」而不是新剧情。
    const sd = s.sample_decode || (s.sample_decode = {});
    const seam = sd.seam || (sd.seam = {});
    const touch = () => { try { node?.__jzlUpdateDisplay?.(); } catch (_) {} };

    c.append(makeSectionTitle("分段衔接"));
    c.append(field("启用衔接", checkboxControl(seam.enabled !== false,
        "开：第 2 段起先接住上一段的结尾画面再往下生成，段与段之间不跳。关：每段单独生成",
        v => { seam.enabled = !!v; touch(); })));
    c.append(field("衔接帧数", selectControl(SWAP_WINDOW_OPTIONS, Number(seam.window ?? 22),
        v => { seam.window = Number(v); touch(); })));
    c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;",
        "接住上一段结尾的多少帧当本段开场。越大越稳、越慢；22 帧（约 0.9 秒）最稳。"));
    c.append(field("过渡帧数", selectControl(SWAP_SETTLE_OPTIONS, Number(seam.settle ?? 12),
        v => { seam.settle = Number(v); touch(); })));
    c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;",
        "接住之后再多生成几帧让模型启动，这几帧不进成片。加大它专治接缝「卡一下」。"));
    c.append(field("参考帧数", stepperControl(Number(seam.ref_video_tail ?? 0),
        { min: 0, max: 600, step: 17 }, v => { seam.ref_video_tail = Math.max(0, Math.round(v)); })));
    c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;",
        "给模型看多少帧源片当参考。0 = 整段都给（动作最准、最慢）；填 39~56 只看开头一小段，省显存。"));
    const appendPrevCb = checkboxControl(!!seam.append_prev_video,
        "把上一段结尾也拼进参考视频，让模型看到「已经生成成什么样」",
        v => { seam.append_prev_video = !!v; });
    c.append(field("引导来源", selectControl(
        [{ value: "pixels", label: "像素尾帧（推荐）" },
         { value: "latent", label: "latent 尾帧（旧，二采会糊）" }],
        String(seam.guide_mode || "pixels"), v => {
            seam.guide_mode = v;
            appendPrevCb.style.opacity = (v === "pixels") ? "1" : "0.45";
            touch();
        })));
    c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;",
        "衔接画面按像素传还是按 latent 传。像素会按本段分辨率重新编码，所以清晰。"));
    c.append(field("二采钉帧", checkboxControl(seam.pin_second !== false,
        "二采也按二采分辨率重新接一次；关掉只有一采接着，二采会糊",
        v => { seam.pin_second = !!v; touch(); })));
    appendPrevCb.title = "把上一段结尾也拼进参考视频，让模型看到「已经生成成什么样」";
    c.append(field("拼接参考", appendPrevCb));
    appendPrevCb.style.opacity = (String(seam.guide_mode || "pixels") === "pixels") ? "1" : "0.45";
    c.append(el("div", "font-size:12px;color:var(--descrip-text,#999);margin:-4px 0 8px 172px;line-height:1.7;white-space:pre-line;",
        "衔接帧数 + 过渡帧数 = 每段前面要多生成的长度（这一段不进成片）。\n"
        + "例：22 + 12 = 34 帧 → 5 秒的段实际生成 158 帧，成片仍然只取 124 帧。\n"
        + "衔接取的是「上段结尾往前 34 帧」起的那 22 帧，所以和本段参考视频对得上，不会跳帧。"));
}

function renderSwapPrefPanel(c, s, d, node) {
    // ⭐ 100% 复刻「JZL - 🤖 MiniMax-H3短剧导演台」的 🎭 采样解码设置：
    //    直接调用同一个 renderPrefPanel（采样分类 / 随机种子 / 采样 / 二次采样（含 Sigmas 模式、空间分块）/
    //    SelfLift 采样 / 解码），保证两个节点的面板逐项一致、改一处两边同步。
    //    唯一差别：本节点不显示「♾️ 无限时长衔接」（renderPrefPanel 内部以 node.type 判断，自动跳过）。
    renderPrefPanel(c, s, d, node);
    renderSwapSeamSection(c, s, node);
}

function renderSwapHelpPanel(c) {
    const p = (txt, style) => c.append(el("p", style || "margin:6px 0;line-height:1.8;font-size:13px;color:var(--fg-color,#ddd);", txt));
    const h = (txt) => c.append(el("div", "margin:14px 0 6px;font-size:14px;font-weight:700;color:#c9a86a;", txt));
    c.append(el("div", "font-size:15px;font-weight:700;color:var(--fg-color,#eee);margin-bottom:6px;", "🎭 MiniMax-H3 人物替换 · 使用说明"));
    p("把一段已有视频里的演员换成你给的定妆图（不是局部换脸，而是逐段重生成：动作/机位/场景保留、人换掉）。长视频自动分段、自动衔接，成片时长恒等于源片。");

    h("① 接线");
    p("源视频 ← 「加载视频 / GetVideoComponents」的 IMAGE 输出（按 24fps 解释，非 24fps 请先转码）");
    p("源视频原声 ← 同一条时间轴上的 AUDIO（「保留原声」时必需）");
    p("定妆图 ref_image_0..8 ← 新演员参考图（<Picture 1..9>），建议正面清晰、单人");
    p("主模型 / CLIP / 视觉VAE / 音频VAE ← 与导演台一致；建议用 ref2va 变体模型");
    p("端口顺序：主模型 → CLIP → 视觉VAE → 音频VAE → 源视频 → 源视频原声 → 定妆图；提示词在最底部。");

    h("② 四个按钮");
    p("🎨 视频参数设置：画幅/像素、每段时长、音频方式、参考图放大、二采放大、最大段数");
    p("🎭 采样解码设置：与「短剧导演台」100% 相同（采样分类/种子/一采/二采/SelfLift/解码），"
        + "末尾多一段本节点独有的「分段衔接」：衔接帧数 / 过渡帧数 / 参考帧数");
    p("🎬 生成视频管理：输出名称（output/jzl/{名称}/）、保存方式、本批视频宫格预览");
    p("📖 节点使用说明：本页");
    p("保存后重新执行节点生效（「自动保存」开着时改完即存）。提示词固定在最底部的大输入框里。");

    h("③ 为什么接缝比「无限时长」稳");
    p("无限时长没有真值，段 2+ 只能靠上段尾帧往下编 → 会缓慢漂移；换人有逐帧真值：每段都重新对位同一段源片（<Video 1>），"
        + "跑道那几十帧的源片正好是上一段已产出的内容 → 同一条时间轴、不累积误差。衔接只负责「画面不跳」。");

    h("④ 调参经验");
    p("脸不像 → 参考图放大 1.5~2.0、百万像素 0.6~1.0、二采倍数 1.5");
    p("接缝跳一下 → 衔接帧数加到 39；还不行把过渡帧数加到 29；或每段时长改 8 秒（接缝更少）");
    p("动作对不上 → 参考帧数设 0、采样步数 8~10");
    p("太慢 / 爆显存 → 参考帧数 39~56、段长 8~10 秒、二采先只跑一采看效果");

    h("⑤ 已知取舍");
    p("H3 是整帧重生成：背景/道具会被重画（招牌字、路人、光线可能变）。要「只换人不换景」需要掩码回流后处理（后续版本）。");
    p("换人后口型由模型重新生成，与原台词不保证逐字对位 —— 所以默认「保留原声」只换画面。");
    p("成本：每段都带整段参考视频时约等于同长度再算一遍（≈2×），可用「参考帧数」压到 1.2~1.3×。");
}

function renderVideoParamsPanel(c, s, node) {
    const { cur, set, comboOptions } = panelWidgetIO(node);
    renderAutoSave(c, s);

    // 分辨率预览跟随「画幅比例 / 百万像素」实时刷新（两个控件的回调里调用）
    let refreshRes = () => {};

    c.append(makeSectionTitle("画幅比例"));
    c.append(bigBoxGrid(comboOptions("aspect_ratio", OPTIONS.aspectRatio),
        String(cur("aspect_ratio", "16:9 (Widescreen)")), v => { set("aspect_ratio", v); refreshRes(); }, 5, 26));

    c.append(makeSectionTitle("分辨率与时长"));
    c.append(field("百万像素（MP）", stepperControl(parseFloat(cur("megapixels", 0.4)) || 0.4,
        { min: 0.1, max: 3.6, step: 0.1 }, v => { set("megapixels", v); refreshRes(); })));

    const resBox = el("div", "font-size:13px;color:#c9a86a;padding:6px 10px;text-align:center;"
        + "background:var(--comfy-input-bg,#2a2a2a);border:1px solid var(--border-color,#444);"
        + "border-radius:4px;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;");
    resBox.title = "根据上面的「画幅比例」与「百万像素（MP）」计算出的生成分辨率（宽 × 高，32 对齐）";
    c.append(field("分辨率", resBox));
    refreshRes = () => {
        const [W, H] = jzlCalcResolution(cur("aspect_ratio", "16:9 (Widescreen)"), parseFloat(cur("megapixels", 0.4)) || 0.4);
        resBox.textContent = `${W} × ${H}`;
    };
    refreshRes();

    c.append(field("每段视频时长", selectControl(comboOptions("duration", ["固定5秒"]),
        String(cur("duration", "固定5秒")), v => set("duration", v))));

    c.append(makeSectionTitle("生成视频数量"));
    // 段数双形态：短剧导演台是数值输入（0=剧情决定），Pro/Max/无限时长/Mini 是下拉（剧情决定/生成N段）。
    // 读写都走 readVideoCount / setVideoCount，避免把数字写进下拉或把「生成N段」写进数值输入。
    const _vcW = (node?.widgets || []).find((x) => x.name === "video_count");
    const _vcNum = videoCountIsNumeric(_vcW);
    const _setVC = (v) => {
        const val = setVideoCount(_vcW, Math.round(v));
        try { _vcW?.callback?.call(_vcW, val); } catch (_) {}
        try { node?.__jzlUpdateDisplay?.(); } catch (_) {}
        try { node?.graph?.setDirtyCanvas?.(true, true); } catch (_) {}
    };
    c.append(field("生成视频数量", stepperControl(readVideoCount(_vcW),
        { min: 0, max: 60, step: 1 }, _setVC)));
    c.append(el("div", "font-size:12px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.7;white-space:pre-line;",
        "参数：0 = LLM 按剧情时间线长度，自动计算需要生成多少段视频。\n"
        + "参数：1~60 = 设置固定生成数量，最多支持一次生成60段视频！\n"
        + "🪄 穿透生成模式下忽略此设定：按提示词实际段数生成。"
        + (_vcNum ? "" : "（本节点该参数以下拉形式保存，面板里按数值显示与修改）")));

    c.append(makeSectionTitle("参考与放大"));
    c.append(field("参考数值放大", stepperControl(parseFloat(cur("scale_factor", 1.0)) || 1.0,
        { min: 1.0, max: 5.0, step: 0.1 }, v => set("scale_factor", v))));
    c.append(field("二采latent放大", stepperControl(parseFloat(cur("upscale_scale", 1.0)) || 1.0,
        { min: 1.0, max: 4.0, step: 0.1 }, v => set("upscale_scale", v))));
}

function renderPrefPanel(c, s, d, node) {
    const p = s.sample_decode;
    const sec = p.second || (p.second = {});
    renderAutoSave(c, s);

    // ── 采样分类（切换方式参照「🤖 大语言模型配置 → LLM 后端」：方形勾选单选，切换只显示对应区块）──
    c.append(makeSectionTitle("采样分类"));
    const sl = p.selflift || (p.selflift = {
        steps: 8, transition_step: 6, lowres_scale: 0.5, rho: 0.6, w_min: 0.5, w_max: 1.0,
        upscaler_model: "none", upscaler_unload: true, highres_tiling: false,
    });
    const modeRow = el("div", "display:flex;align-items:center;gap:16px;min-width:0;flex-wrap:wrap;");
    const modeCbs = [];
    const mkModeCb = (val, label) => {
        const lab = el("label", "display:flex;align-items:center;gap:6px;font-size:13px;color:var(--descrip-text,#999);cursor:pointer;");
        const cb = el("input", "width:18px;height:18px;accent-color:#f59e0b;cursor:pointer;");
        cb.type = "checkbox";
        cb.checked = (String(p.sample_mode || "常规采样") === val);
        cb.addEventListener("change", () => {
            if (cb.checked) {
                p.sample_mode = val;
                modeCbs.forEach((x) => { if (x !== cb) x.checked = false; });
                syncSampleMode();
            } else if (modeCbs.every((x) => !x.checked)) {
                cb.checked = true;   // 保持至少一个选中
            }
        });
        lab.append(cb, el("span", "", label));
        modeRow.append(lab);
        modeCbs.push(cb);
    };
    c.append(field("采样分类", modeRow));
    mkModeCb("常规采样", "常规采样：K采样器 + 可选二次采样（现有链路）");
    mkModeCb("SelfLift采样", "SelfLift采样：低分辨率前缀 → 提升 → 高分辨率收尾（内置 SelfLift，替代二次采样）");

    // 随机种子：常规采样与 SelfLift 采样共用（两种分类下都生效）
    c.append(makeSectionTitle("随机种子"));
    c.append(seedControlRow(p, "seed", "seed_mode", "随机种子"));

    // 常规采样区块（K 采样器 + 二次采样）
    const normalBox = el("div", "");
    // SelfLift 区块
    const slBox = el("div", "");

    // ── 采样（一采）──
    normalBox.append(makeSectionTitle("采样"));
    normalBox.append(field("K采样器", selectControl(OPTIONS.samplers, p.sampler || OPTIONS.samplers[0], v => { p.sampler = v; })));
    const normSchedSel = selectControl(OPTIONS.schedulers, p.scheduler || OPTIONS.schedulers[0], v => { p.scheduler = v; });
    normalBox.append(field("调度器", normSchedSel));
    const normStepsStepper = stepperControl(p.steps ?? 4, { min: 1, max: 200, step: 1 }, v => { p.steps = Math.round(v); });
    normalBox.append(field("采样步数", normStepsStepper));
    normalBox.append(field("降噪系数", stepperControl(p.denoise ?? 1.0, { min: 0, max: 1, step: 0.05 }, v => { p.denoise = v; })));

    // ── 二次采样（开关 + 设置；不折叠，设置项始终显示；种子与一采共享）──
    normalBox.append(makeSectionTitle("二次采样"));
    normalBox.append(field("启用二次采样", checkboxControl(!!sec.enabled,
        "一采后➡️分离latent➡️ 视频latent放大➡️合并latent➡️二次采样",
        v => { sec.enabled = v; })));
    normalBox.append(field("运行设备", selectControl(["cuda", "rocm", "cpu"], sec.device || "cuda", v => { sec.device = v; })));
    // ── latent 放大模型（只读磁盘 latent_upscale_models 目录及其子目录的实时列表：有就是有、没有就是没有）──
    // 列表来自后端 folder_paths.get_filename_list("latent_upscale_models")（含子目录，返回相对路径）；
    // 不提供「自动」项、不做任何硬编码兜底（未下载就不显示）；选中项即存储值（所见即所得）。
    const upModels = (d && Array.isArray(d.upscaler_models)) ? d.upscaler_models : [];
    const _NO_MODEL = "（未找到放大模型：请把权重放入 models/latent_upscale_models/ 或子目录）";
    const _miniFirst = upModels.find((n) => /minimax_h3_latent_upscaler_3d/i.test(n));
    const _curModel = upModels.includes(sec.upscaler_model) ? sec.upscaler_model
                    : (upModels.length ? (_miniFirst || upModels[0]) : _NO_MODEL);
    // 旧配置存的空值（"自动"）或已被删除的文件 → 自动落到磁盘上的有效项并写回
    if (upModels.length && _curModel !== sec.upscaler_model) {
        sec.upscaler_model = _curModel;
        const _m0 = String(_curModel).match(/(fp32|fp16|bf16)/i);
        if (_m0) sec.precision = _m0[1].toLowerCase();
    }
    const upSel = selectControl(upModels.length ? upModels : [_NO_MODEL], _curModel, v => {
        if (!upModels.length) return;
        sec.upscaler_model = v;
        const m = String(v).match(/(fp32|fp16|bf16)/i);   // 选到标准精度文件 → 同步精度，保持一致
        if (m) sec.precision = m[1].toLowerCase();
    });
    if (!upModels.length) { try { upSel.disabled = true; upSel.style.opacity = "0.6"; } catch (_) {} }
    normalBox.append(field("latent放大模型", upSel));
    // 精度：仅作为加载精度参数（不再联动清空模型选择——那会把用户的手动选择打回「自动」）
    normalBox.append(field("精度", selectControl(["fp32", "fp16", "bf16"], sec.precision || "fp32", v => { sec.precision = v; })));

    // ── Sigmas 模式（单选：调度器 / 自定义）──
    // 调度器=用 调度器+采样步数+降噪系数 生成 sigmas 二采；自定义=直接用下方输入的自定义 sigmas 序列（对齐 1190 ManualSigmas）
    const sigModeRow = el("div", "display:flex;align-items:center;gap:16px;min-width:0;");
    const mkSigRadio = (val, label) => {
        const lab = el("label", "display:flex;align-items:center;gap:5px;font-size:13px;color:var(--descrip-text,#999);cursor:pointer;");
        const r = el("input", "accent-color:#f59e0b;cursor:pointer;");
        r.type = "radio"; r.name = "jzl_second_sigmas"; r.value = val;
        r.checked = (sec.sigmas_mode || "scheduler") === val;
        r.addEventListener("change", () => { if (r.checked) { sec.sigmas_mode = val; syncSigMode(); } });
        lab.append(r, el("span", "", label));
        sigModeRow.append(lab);
        return r;
    };
    normalBox.append(field("Sigmas模式", sigModeRow));
    normalBox.append(field("K采样器", selectControl(OPTIONS.samplers, sec.sampler || "euler", v => { sec.sampler = v; })));
    const fScheduler = field("调度器", selectControl(OPTIONS.schedulers, sec.scheduler || "simple", v => { sec.scheduler = v; }));
    const fSteps = field("采样步数", stepperControl(sec.steps ?? 3, { min: 1, max: 200, step: 1 }, v => { sec.steps = Math.round(v); }));
    const fDenoise = field("降噪系数", stepperControl(sec.denoise ?? 0.3, { min: 0, max: 1, step: 0.05 }, v => { sec.denoise = v; }));
    const fCustom = field("自定义Sigmas", textControl(sec.custom_sigmas || "0.8500, 0.6316, 0.3158, 0.0000", "0.8500, 0.6316, 0.3158, 0.0000", v => { sec.custom_sigmas = v; }));
    normalBox.append(fScheduler, fSteps, fDenoise, fCustom);
    const syncSigMode = () => {
        const custom = (sec.sigmas_mode || "scheduler") === "custom";
        // 注意：field 容器是 flex 布局，恢复显示必须显式设回 "flex"（设 "" 会移除 display 声明，
        // 回落到块级布局，导致标签/控件错位、控件变窄贴边）
        fScheduler.style.display = custom ? "none" : "flex";
        fSteps.style.display = custom ? "none" : "flex";
        fDenoise.style.display = custom ? "none" : "flex";
        fCustom.style.display = custom ? "flex" : "none";
    };
    mkSigRadio("scheduler", "调度器");
    mkSigRadio("custom", "自定义");
    syncSigMode();

    // ── 二采空间分块（省显存；只在二采生效，一采恒为整画布）──
    // 自研前向级分块：沿长边切块 → 逐块前向 → 升余弦窗融合回整画布。
    // 因为采样器每步只调一次前向、拿到的是融合后结果，等价于「每步先融合全部块、再推进」，
    // 且对任意采样器都成立（不像手写 euler 循环那样只覆盖 euler 系）。
    const fTileTiles = field("分块数", stepperControl(sec.tiling_tiles ?? 2, { min: 2, max: 8, step: 1 }, v => {
        sec.tiling_tiles = Math.max(2, Math.round(v));
    }));
    // 空间分块：三选一（方形勾选单选，切换方式与「采样分类」「LLM 后端」一致）
    const TILING_MODES = ["关闭", "自动", "手动"];
    const tilingRow = el("div", "display:flex;align-items:center;gap:16px;min-width:0;flex-wrap:wrap;");
    const tilingCbs = [];
    const mkTilingCb = (val) => {
        const lab = el("label", "display:flex;align-items:center;gap:6px;font-size:13px;color:var(--descrip-text,#999);cursor:pointer;");
        const cb = el("input", "width:18px;height:18px;accent-color:#f59e0b;cursor:pointer;");
        cb.type = "checkbox";
        cb.checked = (sec.tiling || "关闭") === val;
        cb.addEventListener("change", () => {
            if (cb.checked) {
                sec.tiling = val;
                tilingCbs.forEach((x) => { if (x !== cb) x.checked = false; });
                syncTiling();
            } else if (tilingCbs.every((x) => !x.checked)) {
                cb.checked = true;   // 保持至少一个选中
            }
        });
        lab.append(cb, el("span", "", val));
        tilingRow.append(lab);
        tilingCbs.push(cb);
    };
    normalBox.append(field("空间分块", tilingRow));
    TILING_MODES.forEach(mkTilingCb);
    const fTileOverlap = field("分块重叠像素", stepperControl(sec.tiling_overlap ?? 128, { min: 0, max: 512, step: 32 }, v => {
        sec.tiling_overlap = Math.max(0, Math.round(v));
    }));
    normalBox.append(fTileTiles);
    normalBox.append(fTileOverlap);
    const tilingHint = el("div", "font-size:11px;color:#c9a86a;line-height:1.6;margin:-2px 0 8px;", "");
    normalBox.append(tilingHint);
    const syncTiling = () => {
        const mode = sec.tiling || "关闭";
        // 勾选状态与配置对齐（防其它代码路径改过 sec.tiling）
        tilingCbs.forEach((cb, i) => { cb.checked = TILING_MODES[i] === mode; });
        // 注意：field 容器是 flex 布局，恢复显示必须显式设回 "flex"
        fTileTiles.style.display = mode === "手动" ? "flex" : "none";
        fTileOverlap.style.display = mode === "关闭" ? "none" : "flex";
        if (mode === "关闭") { tilingHint.textContent = ""; return; }
        tilingHint.textContent = mode === "自动"
            ? "自动：按可用显存选最少块数（显存够就不分块）。音频不参与分块（每块喂完整音频），视频按升余弦窗融合。"
            : `手动：固定 ${sec.tiling_tiles ?? 2} 块。块数越多越省显存、但重复计算越多（每块都算一遍完整音频）。`;
    };
    syncTiling();

    // ── SelfLift 采样（内置副本，参数对齐「SelfLift Progressive Sampler (MiniMax H3)」节点）──
    slBox.append(makeSectionTitle("SelfLift 采样"));
    slBox.append(field("采样器", el("div", "font-size:13px;color:var(--fg-color,#ddd);padding:6px 10px;background:var(--comfy-input-bg,#2a2a2a);border:1px solid var(--border-color,#444);border-radius:4px;", "euler（SelfLift 固定标准 Euler，NFE 数不变）")));
    const slSchedSel = selectControl(OPTIONS.schedulers, p.scheduler || OPTIONS.schedulers[0], v => { p.scheduler = v; });
    slBox.append(field("调度器", slSchedSel));
    const slStepsStepper = stepperControl(Number(sl.steps ?? 8), { min: 2, max: 200, step: 1 }, v => {
        sl.steps = Math.max(2, Math.round(v)); syncSlHint();
    });
    slBox.append(field("采样步数", slStepsStepper));
    slBox.append(field("CFG", stepperControl(Number(p.cfg ?? 1.0), { min: 0, max: 100, step: 0.1 }, v => { p.cfg = v; })));
    const fTStep = field("低分辨率步数", stepperControl(Number(sl.transition_step ?? 6), { min: 1, max: 200, step: 1 }, v => {
        sl.transition_step = Math.max(1, Math.round(v)); syncSlHint();
    }));
    slBox.append(fTStep);
    slBox.append(field("低分辨率缩放", stepperControl(Number(sl.lowres_scale ?? 0.5), { min: 0.25, max: 1, step: 0.05 }, v => { sl.lowres_scale = v; })));
    // 修正强度上下限：默认值对齐上游 SelfLift 节点（w_min=0.5 / w_max=1.0）
    slBox.append(field("修正强度下限", stepperControl(Number(sl.w_min ?? 0.5), { min: 0, max: 1, step: 0.05 }, v => { sl.w_min = v; })));
    slBox.append(field("修正强度上限", stepperControl(Number(sl.w_max ?? 1.0), { min: 0, max: 1, step: 0.05 }, v => { sl.w_max = v; })));
    // 外部 H3 放大模型：与「二采 latent 放大模型」同一目录（models/latent_upscale_models）——
    // 列表由后端 _selflift_upscaler_options() 给出：只列名字含 h3 的（含二级子目录，如 MiniMax-H3\xxx.safetensors），
    // 并带 none（= 纯 SelfLift：nearest 提升 + 像素锚点修正）。后端不可用时本地按 h3 过滤兜底。
    const _slBackend = (d && Array.isArray(d.selflift_upscaler_models)) ? d.selflift_upscaler_models : null;
    const slUpH3 = _slBackend ? _slBackend.filter((n) => n !== "none")
                              : upModels.filter((n) => /h3/i.test(n));
    const slUpOpts = ["none"].concat(slUpH3);
    // 旧配置里的值（换目录/改名/只写了文件名）不在选项里 → 自动落到磁盘上可用的那个（与「二采 latent放大模型」同一策略）
    const slUpCur = slUpOpts.includes(sl.upscaler_model) ? sl.upscaler_model : (slUpH3[0] || "none");
    sl.upscaler_model = slUpCur;
    // ⭐ rho 与放大模型联动（上游口径）：
    //    选了外部放大模型 → rho 应为 0（只用 latent 提升、跳过 VAE 往返；实测过渡 41s → 1.7s）
    //    放大模型 = none  → rho 应为 0.6（SelfLift-zero 直接路线，官方建议起点）
    const RHO_WITH_UPSCALER = 0, RHO_SELFLIFT_ONLY = 0.6;
    const rhoStepper = stepperControl(Number(sl.rho ?? RHO_SELFLIFT_ONLY), { min: 0, max: 1, step: 0.05 }, v => {
        sl.rho = v; syncSlHint();
    });
    const slUpSel = selectControl(slUpOpts, slUpCur, v => {
        const had = String(sl.upscaler_model || "none");
        sl.upscaler_model = v;
        // 只在「两边都是我们推荐的默认值」时才自动改写 rho —— 用户手动调过的值不覆盖
        const fromDefault = (had === "none") === (Number(sl.rho) === RHO_SELFLIFT_ONLY);
        if (fromDefault) {
            sl.rho = (v === "none") ? RHO_SELFLIFT_ONLY : RHO_WITH_UPSCALER;
            const inp = rhoStepper.querySelector("input");
            if (inp) inp.value = sl.rho;
        }
        syncSlHint();
    });
    slBox.append(field("放大模型", slUpSel));
    slBox.append(field("修正比例 rho", rhoStepper));
    slBox.append(field("放大后卸载", checkboxControl(sl.upscaler_unload !== false,
        "放大完立即把放大模型从显存卸载，再进入高分辨率阶段", v => { sl.upscaler_unload = !!v; })));
    slBox.append(field("高分辨率分块", checkboxControl(!!sl.highres_tiling,
        "高分辨率（= 二采）阶段切块省显存（只保留首块音频；分块时跳过 TST 诊断）", v => { sl.highres_tiling = !!v; })));
    const slHint = el("div", "font-size:11px;color:#c9a86a;line-height:1.6;margin:-2px 0 8px;white-space:pre-line;", "");
    slBox.append(slHint);
    const syncSlHint = () => {
        const steps = Math.max(2, Math.round(Number(sl.steps ?? 8)));
        const ts = Math.max(1, Math.round(Number(sl.transition_step ?? 6)));
        const msgs = [];
        if (ts > steps - 1) msgs.push(`⚠️ 低分辨率步数 ${ts} 超出「采样步数 ${steps}」允许范围（1~${steps - 1}），运行时会自动钳制为 ${steps - 1}`);
        if (Number(sl.rho ?? 0) <= 0 && String(sl.upscaler_model || "none") === "none") msgs.push("⚠️ rho=0 且 放大模型=none 会同时关闭修正与外部提升 → 运行时直接报错，请改其一");
        else if (Number(sl.rho ?? 0) > 0 && String(sl.upscaler_model || "none") !== "none") msgs.push("ℹ️ rho>0 同时选外部放大模型属于混合实验路径（论文未验证），推荐只用其一");
        if (!slUpH3.length) msgs.push("ℹ️ models/latent_upscale_models（含二级子目录）下未找到 H3 放大模型 → 只能用 none（纯 SelfLift）");
        msgs.push(`过渡步数 = 低分辨率 ${Math.min(ts, steps - 1)} 步 + 高分辨率 ${steps - Math.min(ts, steps - 1)} 步`);
        msgs.push("rho 随放大模型自动联动（选模型→0、none→0.6），手动改过则不再覆盖");
        msgs.push(`放大模型在「过渡」阶段用一次（低分辨率 → 高清），高分辨率 ${steps - Math.min(ts, steps - 1)} 步 = 二采`);
        slHint.textContent = msgs.join("\n");
    };
    syncSlHint();

    c.append(normalBox, slBox);
    // 采样分类切换：只显示当前分类对应的区块（常规=采样+二次采样；SelfLift=SelfLift 参数）
    // 调度器 / 采样步数是两种分类共用的一份配置，但两个区块各渲染了一套控件 →
    // 切换时把当前配置值同步到即将显示的控件里，避免看到另一套控件留下的旧值
    const _syncStepper = (wrapEl, v) => { const i = wrapEl?.querySelector("input"); if (i) i.value = v; };
    const _syncSelect = (selEl, v) => { if (selEl) selEl.value = v; };
    const syncSampleMode = () => {
        const isSL = String(p.sample_mode || "常规采样") === "SelfLift采样";
        normalBox.style.display = isSL ? "none" : "";
        slBox.style.display = isSL ? "" : "none";
        if (isSL) {
            _syncSelect(slSchedSel, p.scheduler || OPTIONS.schedulers[0]);
            // SelfLift 步数是独立配置（默认 8），不再与常规采样的步数互相镜像
            if (Number(sl.steps ?? 8) < 2) sl.steps = 2;   // SelfLift 至少 2 步
            _syncStepper(slStepsStepper, Math.max(2, Math.round(Number(sl.steps ?? 8))));
            syncSlHint();
        } else {
            _syncSelect(normSchedSel, p.scheduler || OPTIONS.schedulers[0]);
            _syncStepper(normStepsStepper, Number(p.steps ?? 4));
        }
    };
    syncSampleMode();

    // ── 解码 ──
    c.append(makeSectionTitle("解码"));
    // 视频解码固定 XB-BOX 优化版（=原版+显存清理，与 1146 一致）；清理选项完整保留
    c.append(field("视频解码", el("div", "font-size:13px;color:var(--fg-color,#ddd);padding:6px 10px;background:var(--comfy-input-bg,#2a2a2a);border:1px solid var(--border-color,#444);border-radius:4px;", "XB-BOX - VAE解码（原版优化）")));
    c.append(field("解码前清理", selectControl(OPTIONS.decodeCleanup, p.decode_cleanup || OPTIONS.decodeCleanup[0], v => { p.decode_cleanup = v; })));
    c.append(field("音频解码", el("div", "font-size:13px;color:var(--fg-color,#ddd);padding:6px 10px;background:var(--comfy-input-bg,#2a2a2a);border:1px solid var(--border-color,#444);border-radius:4px;", "VAE解码（音频）")));

    // ── ♾️ 无限时长衔接⚠实验性功能（仅「JZL - 🤖 MiniMax-H3无限时长」显示；短剧导演台已移除该功能）──
    // 后端读取 sample_decode.infinite = { enabled, window, audio_no_trim, settle, trim_mode, guide_source, second_guide }
    const isInfNode = !!node && node.type === INF_NODE_TYPE;
    if (isInfNode) {
        const inf = p.infinite || (p.infinite = { enabled: true, window: 22, audio_link: true, audio_no_trim: false, luma_match: true });
        c.append(makeSectionTitle("无限时长衔接⚠实验性功能"));
        // ⭐ 总开关：关闭后本节点按「无衔接」独立生成每一段（等价 Max 语义）
        c.append(field("启用跨段衔接", checkboxControl(inf.enabled !== false,
            "开启：第 2 段起用上段 latent 尾部衔接（画面连续）；关闭：每段独立生成（等价 Max）",
            v => { inf.enabled = v; })));
        // 窗口取值恒为 17m+5（5/22/39/56）：段长恒为 17k+5，两者相减必是 17 的倍数 → 相位自动对齐
        const SEAM_WINDOWS = ["5", "22", "39", "56"];
        const _nearest = (opts, v, dflt) => {
            const n = parseInt(v, 10);
            if (!isFinite(n)) return dflt;
            return opts.reduce((a, b) => Math.abs(parseInt(b, 10) - n) < Math.abs(parseInt(a, 10) - n) ? b : a, dflt);
        };
        const _w = _nearest(SEAM_WINDOWS, inf.window, "22");
        if (String(inf.window ?? "") !== _w) { inf.window = parseInt(_w, 10); }   // 非列表值就近归一（与后端 _seam_resolve_window 一致）
        c.append(field("衔接窗口（帧）", selectControl(SEAM_WINDOWS, _w, v => {
            inf.window = parseInt(v, 10) || 22;
        })));
        // 裁切方式：像素域（默认）= latent 完全不裁 + 解码后丢帧；latent 域 = 先裁 latent 再解码（少解码量），
        // 但丢弃 token 数必须是 5 的倍数（= 丢弃帧数是 17 的倍数 → 过渡需为 12/29/46），不满足自动回退像素域
        const TRIM_MODES = ["像素域裁切（推荐）", "latent域裁切（相位安全）", "不裁切（仅诊断）"];
        const _tmNorm = (v) => {
            const s = String(v || "");
            if (s.indexOf("不裁") >= 0) return TRIM_MODES[2];
            if (s.indexOf("latent") >= 0) return TRIM_MODES[1];   // 兼容旧值「latent域裁切（旧·会闪烁）」
            return TRIM_MODES[0];
        };
        c.append(field("裁切方式", selectControl(TRIM_MODES, _tmNorm(inf.trim_mode),
            v => { inf.trim_mode = v; })));
        // 过渡丢弃帧数（settling burn-in）：钉住窗口之外再多丢的帧，吸掉「模仿引导→启动新动作」的卡顿（hold→pop）
        // 只提供 12 / 29 / 46：这三个值使「窗口(17m+5) + 过渡」恒为 17 的倍数
        //   ⇒ 生成帧数(17K+5) − 丢弃 = 落盘帧数(17k+5) 能精确等于设定时长（12 对全部窗口档都成立，默认）
        const SETTLE_OPTS = ["12", "29", "46"];
        // 旧值（0/5/8/17/22 等，含 null）就近归一 → 后端 _seam_snap_settle 用同一套规则再兜底
        const _st = _nearest(SETTLE_OPTS, inf.settle, "12");
        if (String(inf.settle ?? "") !== _st) { inf.settle = parseInt(_st, 10); }
        c.append(field("过渡丢弃帧数（12/29/46）", selectControl(SETTLE_OPTS, _st, v => {
            inf.settle = parseInt(v, 10) || 0;
        })));
        // 引导来源：一采 latent 尾部切片直传（默认）vs 官方 AddGuide 同款尾帧→VAE重编码（A/B 诊断）
        const GUIDE_SOURCES = ["一采latent尾部（推荐）", "上段尾帧→VAE重编码（官方同款）"];
        c.append(field("引导来源", selectControl(GUIDE_SOURCES,
            GUIDE_SOURCES.includes(inf.guide_source) ? inf.guide_source : GUIDE_SOURCES[0],
            v => { inf.guide_source = v; })));
        // 二采引导来源（仅开启二采时生效）：first = 复用上段一采 latent（内部插值对齐二采画布，零额外内存）；
        // second = 用上段**二采** latent 尾部重建（与二采画布同尺寸、同 refined 版本，更准；CPU 常驻一份尾部切片）
        const SEC_GUIDES = ["上段一采latent（插值对齐二采画布）", "上段二采latent尾部（同画布真实尾部）"];
        const _sg2Norm = (v) => { const s = String(v || ""); return (s === "second" || s.indexOf("二采") >= 0) ? SEC_GUIDES[1] : SEC_GUIDES[0]; };
        c.append(field("二采引导来源", selectControl(SEC_GUIDES, _sg2Norm(inf.second_guide),
            v => { inf.second_guide = (v === SEC_GUIDES[1]) ? "second" : "first"; })));
        c.append(el("div", "font-size:12px;color:var(--descrip-text,#999);margin:-4px 0 8px 172px;line-height:1.7;white-space:pre-line;",
            "第 2 段起，把上一段【一采 latent】的尾部窗口钉进本段首帧（keyframe 行永不降噪、每步重注入）→ 画面/动作/色调跨段连续；每段仍用各自的新提示词 → 剧情持续推进。\n"
            + "· 窗口只支持 17m+5（5 / 22 / 39 / 56），22 = 官方 Motion Context 基线；段长恒为 17k+5，两者相减必是 17 的倍数 → 相位自动对齐。\n"
            + "· 【时长账】本节点每个数值都按「落盘时长」显示：**落盘时长 = 你设定的时长（× 段数）**。段 2+ 生成时会多出「窗口 + 过渡」帧的段首衔接跑道（默认 22+12 = 34 帧 ≈ 1.42 秒）——这段是承接上一段末帧的延续画面，成片里已经裁掉；LLM 会按此写剧本（第 2 段起 **时长** = 设定 + 跑道），所以设定时长不会被切内容。\n"
            + "· 【过渡自动对齐】只有「窗口 + 过渡」是 17 的倍数时，生成帧数(17K+5) − 丢弃 = 落盘帧数(17k+5) 才能**精确**等于你设定的帧数（见上一条，下拉只提供 12 / 29 / 46）。\n"
            + "· 【段首承接】段 2+ 的 [Shot 1] 就是「承接上一段末帧的延续段」（同机位/同景别/同光线/同姿态，持续跑道秒数）——这段会被裁掉，所以新内容从跑道之后开始（第一次切镜 ≈ 跑道秒数处）。这是为了避开「钉子压住段首 → 模型先模仿引导再启动新动作」的 hold→pop 卡顿。\n"
            + "· 【限制】单段生成上限 15 秒：设定时长 + 跑道 超过 15 秒时（设定 > 13.6 秒左右）该段落盘会比设定略短约一个跑道长度。\n"
            + "· 【裁切方式】默认「像素域裁切」：latent 一律不裁（解码器必须看到完整 5k+2 序列，否则「5 token = 17 帧」的分组相位错位 → 画面周期性重影/闪烁），解码后再丢段首衔接帧 → 落盘的 mp4 已经是裁好的（第 1 段不裁），拼接只是文件级首尾相接。\n"
            + "· 「latent域裁切（相位安全）」：先裁 latent 再解码（解码量更少、保留帧不含被裁内容），但丢弃 token 数必须是 5 的倍数（= 丢弃帧数是 17 的倍数 ⇒ 过渡需为 12 / 29 / 46，默认 12 满足）；条件不满足时自动回退像素域裁切（保证不闪烁）。\n"
            + "· 【过渡丢弃帧数】默认 12（官方 Director 经验值）：钉子会「压住」段首，模型先模仿/保持引导内容、之后才启动新动作，这段启动过渡留在成片里就是接缝处的「卡一下」（官方注释称 hold→pop）。丢弃量 = 窗口 + 过渡；卡顿感重就调大到 29，想让成片少裁几帧就保持 12。下拉只提供 **12 / 29 / 46**——只有它们能让「窗口 + 过渡」是 17 的倍数（生成帧数与落盘帧数都是 17 的倍数加 5），从而「落盘时长 = 你设定的时长」；其它值（含历史配置里的 0/5/8/17/22）会自动就近归一到这三个值。\n"
            + "· 【音频】接缝音频按同一丢弃量裁波形（音画等长）；拼接保存时视频 -c copy 无损、音频由各段无损 WAV 拼成整条后只编码一次 AAC → 段边界无 AAC encoder delay 误差（约 20–30ms/接缝）。\n"
            + "· 【接缝亮度匹配（只做亮度）】默认开启：段首衔接帧被裁掉后，本段第一个画面是模型新生成的内容，其整体曝光可能与上一段末帧差一截（灯光/昼夜/室内外切换的段边界尤其明显）→ 接缝处亮度一跳。开启后把本段**开头 12 帧的平均亮度**从「上段末帧亮度」平滑渐入到「本段自身亮度」：每帧加同一个 delta（R=G=B，加性）、单帧限幅 ±0.10、三项差都小于 0.008 时**完全不动作**。只动亮度、不动 RGB 混合、不动构图，因此不会拖影/重影（乘法增益、长 RGB 混合、低频谱亮度场这些做法官方实测会「画面花 / 一闪一闪」，已弃用）。日志会打印「上段末帧亮度 → 本段渐入亮度」。\n"
            + "· 【引导来源】默认「一采 latent 尾部」：与下段目标同画布同 latent 空间，零 VAE 往返、零累积漂移；若画面异常可切「尾帧→VAE 重编码」对照（官方 AddGuide 同款）。\n"
            + "· 【二采引导来源】（仅开启二采时生效）：默认「上段一采 latent」——复用同一份引导、内部插值对齐放大后的二采画布（零额外内存）；也可切「上段二采 latent 尾部」= 用上一段**二采（refined）**latent 的同画布真实尾部做引导，与最终成片尾部版本完全一致（更准），代价是 CPU 常驻一份尾部切片（只留配置窗口所需 token，通常几~几十 MB）。两种都只影响二采这一步，一采引导恒用上段一采 latent。\n"
            + "· 1 段时无上段，自动跳过衔接（行为与「短剧导演台Max」完全一致）。\n"
            + "· 二采开关不影响裁切/帧账：段间引导恒用上段一采 latent，二采结果只用于解码落盘（二采这一步用哪份引导由上面「二采引导来源」决定）。"));
        c.append(field("音频跨段衔接", checkboxControl(!!inf.audio_link,
            "把上一段音频 latent 的尾部也钉进本段首帧（对白/音乐/环境声能接上）",
            v => { inf.audio_link = !!v; })));
        // 「音频同步裁剪」【默认勾选】：存 `audio_no_trim=true` 才表示关闭（仅作对比实验）；
        // 历史字段 `audio_trim=false` 视为误存（那时关掉会直接造成段尾画面冻结/音画错位）→ 一律按勾选处理
        if (inf.audio_trim !== undefined) { delete inf.audio_trim; }
        c.append(field("音频同步裁剪", checkboxControl(!inf.audio_no_trim,
            "裁剪段首衔接帧时，同步裁掉对应音频长度（窗口+过渡 秒）→ 音画对齐【默认开启·建议保持】。取消勾选 = 本段音频不做时间轴裁剪（仅作对比实验）：音频会整体后移造成音画错位；落盘时仍会强制与画面等长，不会让段尾画面被冻结",
            v => { if (v) { delete inf.audio_no_trim; } else { inf.audio_no_trim = true; } })));
        c.append(field("接缝亮度匹配", checkboxControl((inf.luma_match === undefined) ? true : !!inf.luma_match,
            "【默认开启·建议保持】只做亮度：把本段开头 12 帧的平均亮度朝上一段末帧平滑过渡（加性、单帧限幅 ±0.10），消除接缝亮度跳变。不做任何 RGB 混合（官方实测 RGB 混合会拖影），因此不会影响构图/细节；关闭 = 完全不动画面像素",
            v => { inf.luma_match = !!v; })));
    }
}

// ── 💾 视频保存设置面板（读写 manager_settings.save，不占节点 schema widget） ──
// 保存模式/分段自动保存/自动合并等已并入节点配置 settings.save，随工作流保存，
// 不再作为 schema 输入 → 节点表面无隐藏保存接口（悬停不显示、也不能接线）。
function renderSaveSettingsPanel(c, settings, node, d) {
    const save = settings.save || (settings.save = { mode: "分段保存", auto_save: false, auto_merge: false, auto_merge_delete: false });
    // Max 专用：恒定逐段即时落盘 + 最后读盘拼接（复用 save.mode，仅渲染/语义不同，无逐段落盘开关——Max 总是落盘）
    // 无限时长节点沿用同一套落盘/拼接语义（逐段即时落盘 + 读盘 concat）
    const isMaxNode = !!node && (node.type === MAX_NODE_TYPE || node.type === INF_NODE_TYPE || node.type === SHORTDRAMA_NODE_TYPE);
    if (isMaxNode) {
        const _saveTitle = node.type === INF_NODE_TYPE ? "保存设置（无限时长）"
            : (node.type === SHORTDRAMA_NODE_TYPE ? "保存设置（短剧导演台）" : "保存设置（Max）");
        c.append(makeSectionTitle(_saveTitle));
        const mkc = (label, checked, title, on) => field(label, checkboxControl(!!checked, title, on));
        mkc("分段保存（默认）", save.mode !== "拼接保存", "每段生成完立即落盘，输出本次各段 mp4", (v) => { if (v) save.mode = "分段保存"; });
        mkc("拼接保存", save.mode === "拼接保存", "全部段生成后读盘 concat 合并为一个完整视频", (v) => { if (v) save.mode = "拼接保存"; });
        mkc("拼接保存后删除分段视频", !!save.auto_merge_delete, "拼接保存成功后自动删除各分段 mp4（需勾选拼接保存）", (v) => { save.auto_merge_delete = !!v; });
        c.append(el("div", "font-size:12px;color:var(--descrip-text,#999);margin:-2px 0 8px 172px;line-height:1.7;white-space:pre-line;",
            "Max 恒定：每段【调度→编码→采样→解码】完成立即 ffmpeg 落盘 output/jzl/{故事名}/ 并释放显存/内存（跑 N 段与 1 段占用不叠加）。\n默认分段保存；勾选拼接保存=全部生成后读盘 concat 合并；再勾选删除分段=合并成功即清理各分段 mp4（仅保留合并结果）。"));
        const loc = el("div", "display:flex;align-items:center;gap:8px;font-size:12px;color:var(--descrip-text,#999);margin:0 0 6px 172px;line-height:1.6;");
        loc.append(el("span", "white-space:nowrap;color:var(--fg-color,#ccc);", "落盘位置："));
        loc.append(el("code", "font-family:monospace;background:var(--comfy-input-bg,#2a2a2a);border:1px solid var(--border-color,#444);border-radius:4px;padding:3px 8px;color:#9fd6a4;user-select:all;", "output/jzl/{故事名}/"));
        c.append(loc);
        return;
    }
    // 保存/合并位置固定为运行中 ComfyUI 的 output/jzl（运行时可推导，不写死盘符，不可修改，只读显示）
    const saveDir = (d && d.save_dir) || "output/jzl";
    const locRow = (label) => {
        const row = el("div", "display:flex;align-items:center;gap:8px;font-size:12px;color:var(--descrip-text,#999);margin:0 0 6px 172px;line-height:1.6;");
        row.append(el("span", "white-space:nowrap;color:var(--fg-color,#ccc);", label));
        row.append(el("code", "font-family:monospace;background:var(--comfy-input-bg,#2a2a2a);border:1px solid var(--border-color,#444);border-radius:4px;padding:3px 8px;color:#9fd6a4;user-select:all;", saveDir));
        return row;
    };

    // 保存设置（原有下游 VHS 保存逻辑，已移入面板）
    c.append(makeSectionTitle("保存设置"));
    c.append(field("保存模式", selectControl(["分段保存", "拼接保存"], save.mode,
        (v) => { save.mode = v; })));
    c.append(el("div", "font-size:12px;color:var(--descrip-text,#999);margin:-4px 0 6px 172px;line-height:1.6;",
        "分段保存=按顺序批量输出每段图像/音频（接「视频保存分配→VHS」）；拼接保存=全部生成后拼接成一段再输出。此选项只影响下游 VHS 保存方式，保留原有逻辑。"));

    // 分段视频自动保存（ffmpeg 逐段落盘，位置固定 output/jzl）
    c.append(makeSectionTitle("ffmpeg 逐段落盘"));
    const asRow = field("分段视频自动保存", checkboxControl(!!save.auto_save,
        "每段跑完立刻用 ffmpeg 落盘 mp4（不依赖下游 VHS）",
        (v) => { save.auto_save = !!v; sync(); }));
    const asLoc = locRow("保存位置：");
    c.append(asRow, asLoc);

    // 分段视频自动合并（合并输出位置固定 output/jzl）
    c.append(makeSectionTitle("分段视频自动合并"));
    const amRow = field("分段视频自动合并", checkboxControl(!!save.auto_merge,
        "把落盘的 mp4 按顺序拼接为完整一个视频（无论保存模式；未开自动保存时内部临时落盘）",
        (v) => { save.auto_merge = !!v; sync(); }));
    const amDelRow = field("合并后删除分段视频", checkboxControl(!!save.auto_merge_delete,
        "合并完成后删除各分段 mp4（仅保留合并结果；未开自动保存时的临时分段本就会清理）",
        (v) => { save.auto_merge_delete = !!v; }));
    const amLoc = locRow("合并输出位置：");
    c.append(amRow, amLoc, amDelRow);

    const sync = () => {
        const as = !!save.auto_save;
        const am = !!save.auto_merge;
        asLoc.style.display = as ? "" : "none";
        amLoc.style.display = am ? "" : "none";
        amDelRow.style.display = am ? "" : "none";
    };
    sync();
}

// ── 📖 节点使用说明面板 ────────────────────────────────
// 使用说明内置回退 HTML（OpenPose 式内嵌：后端 /jzl/usage_md 不可用时也能显示；完整文档见 docs/USAGE.md）
const USAGE_FALLBACK_HTML = `
<div style="font-size:13px;line-height:1.7;color:var(--fg-color,#ddd);">
<h3 style="margin:0 0 8px;font-size:16px;font-weight:700;color:var(--fg-color,#eee);border-bottom:1px solid var(--border-color,#444);padding-bottom:4px;">MiniMax-H3 生成管理器 · 使用说明</h3>
<p style="margin:6px 0;">一个节点完成 <b>剧本分段 → 参考调度 → 编码 → 采样 → 解码</b> 全流程；支持 ffmpeg 逐段落盘 / 自动合并。</p>
<h4 style="margin:10px 0 4px;color:var(--fg-color,#eee);">一、快速上手</h4>
<ol style="padding-left:20px;margin:4px 0;">
<li>添加节点 <b>JZL/MiniMax → JZL - 🤖 MiniMax-H3短剧导演台Pro</b></li>
<li>接好 <b>主模型 / CLIP / 视觉VAE / 音频VAE</b>（含视频或音频参考时必须接「音频VAE」）</li>
<li>在 <b>📝 提示词</b> 框写故事 / 剧本（可用 @ 引用素材），并在「📁 参考素材管理」配置素材</li>
<li>设定参数后运行（「生成视频数量」默认「<b>剧情决定</b>」= LLM 按剧情时间线长度 ÷ 每段时长自动定段数）</li>
</ol>
<p style="margin:4px 0;">不接模型也能运行：只做剧本分段 + 调度，不采样解码。</p>
<h4 style="margin:10px 0 4px;color:var(--fg-color,#eee);">二、运行模式</h4>
<ul style="padding-left:20px;margin:4px 0;">
<li><b>🛠️ 故事拆解模式</b>：已有满意的故事，直接拆段生成视频（不改剧情、不新增对白）</li>
<li><b>📚 故事扩写模式</b>：故事太短，先扩写成丰满正文（纯文本，不生成视频）</li>
<li><b>📝 仅提示词输出</b>：只想先看拆解 + 增强后的提示词（纯文本，无需接模型）</li>
<li><b>🪄 穿透生成模式</b>：已有满意的拆解提示词，直接出片（跳过 LLM 拆解与增强）</li>
</ul>
<p style="margin:4px 0;">推荐流程：📝 仅提示词输出 调提示词 → 满意后切 🪄 穿透生成模式 出片。</p>
<p style="margin:4px 0;">⚠️ 拆解 / 扩写吃<b>自然语言故事</b>；含已拆解标签（===H3_PROMPT===、[SHOT_START]、subject_definitions:、### Video_001 等）会报错终止，请改用 🪄 穿透生成模式。</p>
<p style="margin:4px 0;">⚠️ 穿透 / 重拍只接受<b>拆解产物</b>：需含 [SHOT_START]…[SHOT_END] 且每段含 ===H3_PROMPT=== 六段正文，格式不对直接终止；段内 <b>时长：N</b>（英文 <b>Duration：N</b>）决定该段时长，缺失时回退节点「每段视频时长」。</p>
<p style="margin:4px 0;">🔒 <b>穿透生成段数自动对齐</b>：一律按提示词里实际的 [SHOT_START] 段数生成，<b>「生成视频数量」设定被忽略</b>（不再因两者不一致而终止 / 多生成空占位段），上限 60 段。</p>
<h4 style="margin:10px 0 4px;color:var(--fg-color,#eee);">三、素材与参考调度</h4>
<ul style="padding-left:20px;margin:4px 0;">
<li>上限：图片 ≤9 / 视频 ≤3 / 音频 ≤3，总参考 ≤12（官方上限）</li>
<li>素材分区：图片 → 角色 / 场景 / 道具；音频 → 角色音色 / 背景配乐 / 念白</li>
<li>音频三小类<b>各自独立编号</b>（音色A / 配乐A / 念白A）；图片三类、视频各自独立</li>
<li><b>角色音色</b>：把音频指定给某个角色（描述为 音色A = {角色名}音色）；<b>一个角色只能被一个音色占用</b>；角色改名联动，角色删除回退「未指定」</li>
<li>用 @素材名 引用，或由调度指令（类型:槽位名）自动匹配</li>
<li>生成路径固定为<b>多参考（REF2VA）</b>：图片 = 身份参考、音频 = 音色/配乐/念白参考（严格使用你上传的音频文件）；<b>不存在首尾帧 / 首帧（fl2va）路径</b>。视频未被引用时，视频与视频音轨端口自动关闭（避免音频误入音轨通道）</li>
</ul>
<h4 style="margin:10px 0 4px;color:var(--fg-color,#eee);">四、界面按钮</h4>
<p style="margin:4px 0;">🎛️ 运行参数设置 ｜ 🎨 视频参数设置 ｜ 📁 参考素材管理 ｜ 🤖 大语言模型配置 ｜ 🎭 采样解码设置 ｜ 🎬 生成视频管理 ｜ ➕ 常用提示词元素 ｜ 🎯 镜头参数预设 ｜ 🎯 参考元素切换 ｜ 📖 节点使用说明</p>
<p style="margin:4px 0;color:#999;">「🎛️ 运行参数设置」「🎨 视频参数设置」为「🎬 短剧导演台」专用：把运行模式 / 故事风格 / 故事名称、画幅 / 像素 / 时长 / 段数收进面板，节点表面更清爽。</p>
<ul style="padding-left:20px;margin:4px 0;">
<li><b>保存模式</b>：分段保存（逐段输出接 VHS）/ 拼接保存（合并为一段再输出），仅影响下游 VHS</li>
<li><b>分段视频自动保存</b>：每段跑完立即 ffmpeg 落盘 mp4，不依赖下游 VHS，中途崩溃不丢已生成段落</li>
<li><b>分段视频自动合并</b>：把落盘的各段按顺序拼成完整视频（未开自动保存时临时落盘，合并后清理）</li>
</ul>
<h4 style="margin:10px 0 4px;color:var(--fg-color,#eee);">五、采样与解码</h4>
<ul style="padding-left:20px;margin:4px 0;">
<li>采样：res_multistep / euler 等 K 采样器，调度器 simple / karras 等，步数 / CFG / 降噪 / 种子可调</li>
<li>解码：视频 XB-BOX - VAE解码（原版优化）；音频 VAE解码（音频）</li>
<li><b>解码前清理</b> 卸载显存模型：每段解码前把采样模型退回内存、腾显存给 VAE 解码（对齐 XB-BOX；代价是下一段要重载模型，分段越多越慢）</li>
<li>逐段顺序生成：每段「采样 → 解码 → 写入总线 →（可选落盘）」完成后才进入下一段</li>
</ul>
<h4 style="margin:10px 0 4px;color:var(--fg-color,#eee);">六、输出</h4>
<ul style="padding-left:20px;margin:4px 0;">
<li><b>图像</b> IMAGE 列表：每段视频帧（分段保存）或拼接后一段（拼接保存）</li>
<li><b>音频</b> AUDIO 列表：每段音频（与图像一一对应）</li>
<li><b>已处理剧本</b> STRING：全部 LLM 处理后的文本</li>
</ul>
<h4 style="margin:10px 0 4px;color:var(--fg-color,#eee);">七、常见问题</h4>
<ul style="padding-left:20px;margin:4px 0;">
<li>没生成视频？检查 CLIP / VAE / model 是否接好；含视频或音频参考还需接 audio_vae</li>
<li>⭐ 报 too many values to unpack (expected 3) / 提示「本段未落盘任何视频」？多为<b>视觉 / 音频 VAE 接反</b>，按提示换口即可</li>
<li>落盘失败怎么查？控制台给出「❗落盘失败原因」+「❗环境自检报告」路径（output/jzl/_诊断/），把报告发回即可定位</li>
<li>ffmpeg 不可用？按 imageio-ffmpeg 自带 → PATH → 常见安装目录 查找，都找不到时自动改用 PyAV 编码；「拼接保存」仍需要 ffmpeg</li>
<li>50 系报 memory_efficient_attention_forward？xformers 未编译 sm120，节点已自动回退 PyTorch SDPA</li>
</ul>
</div>
`;

function renderHelpPanel(c) {
    const box = el("div", "font-size:13px;line-height:1.7;color:var(--fg-color,#ddd);");
    c.append(box);
    api.fetchApi("/jzl/usage_md")
        .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
        .then((md) => { box.innerHTML = mdToHtml(md); })
        .catch(() => { box.innerHTML = USAGE_FALLBACK_HTML; });  // API 不可用时显示内置说明

    // 底部：支持与打赏 + 其他仓库 + 联系交流
    const qrWrap = el("div", "margin-top:18px;border-top:1px solid var(--border-color,#444);padding-top:14px;");
    qrWrap.append(el("div", "font-size:13px;font-weight:600;color:var(--fg-color,#eee);margin-bottom:10px;", "💰 支持与打赏"));
    qrWrap.append(el("div", "font-size:15px;font-weight:700;color:#f5a623;margin-bottom:12px;line-height:1.8;white-space:pre-line;",
        "本项目使用付费 AI 工具进行开发、调试和测试维护。\n如果这个工具对你有用，帮助到了你，欢迎打赏支持，你的支持将加速开发进度。"));
    const qrRow = el("div", "display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap;");
    const qrFiles = [["DS01.png", "微信"], ["DS02.png", "支付宝"]];
    qrFiles.forEach(([f, label]) => {
        const cell = el("div", "flex:1;min-width:200px;text-align:center;");
        const img = el("img", "width:180px;height:180px;object-fit:contain;background:#fff;border:1px solid var(--border-color,#444);border-radius:8px;");
        img.alt = label;
        img.src = api.apiURL(`/jzl/usage_qr/${f}`);
        img.onerror = () => { img.style.display = "none"; };
        const ph = el("div", "font-size:13px;font-weight:600;color:var(--fg-color,#ddd);margin-top:6px;", label);
        cell.append(img, ph);
        qrRow.append(cell);
    });
    qrWrap.append(qrRow);

    // 我的 ComfyUI 节点仓库
    qrWrap.append(el("div", "font-size:13px;font-weight:600;color:var(--fg-color,#eee);margin:16px 0 6px;", "我的 ComfyUI 节点仓库"));
    const repoRow = el("div", "display:flex;gap:10px;flex-wrap:wrap;");
    const mkRepo = (label, url) => {
        const a = el("a", "display:inline-block;background:#2a4a6a;color:#cfe3f7;border:1px solid #5b9bd5;border-radius:6px;padding:7px 14px;font-size:12px;text-decoration:none;cursor:pointer;", label);
        a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.addEventListener("mousedown", (e) => e.stopPropagation());
        return a;
    };
    repoRow.append(
        mkRepo("🧰 小白工具箱（XB_ToolBox）", "https://github.com/wjluoxiao/XB_ToolBox"),
        mkRepo("🎬 ComfyUI-JZL-MiniMax-H3", "https://github.com/wjluoxiao/ComfyUI-JZL-MiniMax-H3"),
    );
    qrWrap.append(repoRow);

    // 模型与工作流下载
    qrWrap.append(el("div", "font-size:13px;font-weight:600;color:var(--fg-color,#eee);margin:16px 0 6px;", "模型与工作流下载"));
    const dlRow = el("div", "display:flex;gap:10px;flex-wrap:wrap;");
    dlRow.append(
        mkRepo("🟢机制罗的模型总仓库", "https://pan.quark.cn/s/6ec0a5f56d08"),
        mkRepo("🟢机制罗的工作流仓库", "https://pan.quark.cn/s/65b5abcb7879"),
    );
    qrWrap.append(dlRow);

    // 联系与交流
    qrWrap.append(el("div", "font-size:13px;font-weight:600;color:var(--fg-color,#eee);margin:16px 0 6px;", "📮 联系与交流"));
    const bili = mkRepo("B站主页：space.bilibili.com/302329373", "https://space.bilibili.com/302329373");
    bili.style.background = "#2a3a5a";
    qrWrap.append(bili);
    const qqRow = el("div", "display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--descrip-text,#999);line-height:1.8;margin-top:4px;");
    qqRow.append(el("span", "", "⚡️充电股东群：136812541"));
    qqRow.append(el("span", "", "🤖QQ交流群1：231033995"));
    qqRow.append(el("span", "", "🤖QQ交流群2：56798547"));
    qrWrap.append(qqRow);
    c.append(qrWrap);
}

// 📥 加载视频（仅 Max）：保存模式(勾选：分段保存默认 / 拼接保存 / 拼接后删除分段) +
// 自动识别(跟随节点故事名) 或 选择已识别文件夹 加载 output/jzl/{故事名}/ 视频，6 列多宫格点击预览。
function renderVideoManagerPanel(c, node, settings, d) {
    const getStory = () => {
        const sw = (node?.widgets || []).find((x) => x.name === "story_name");
        return sw ? String(readWidgetValue(sw) || "").trim() : "";
    };
    const say = (msg, isErr) => {
        try { notify(msg, isErr ? "error" : "success"); } catch (_) {}
    };
    const AUTO_VAL = "__auto__";
    let refresh = null;   // 「📥 加载视频」的刷新函数（下面赋值；输出名称改动时也要触发）
    // ── 🎭 人物替换：输出名称（落盘目录 output/jzl/{输出名称}/）放在最前面 ──
    //    该节点把 story_name 从节点表面收进本面板（与导演台把故事名收进「运行参数设置」同一做法）
    if (node?.type === SWAP_NODE_TYPE) {
        const nameW = (node?.widgets || []).find((x) => x.name === "story_name");
        const nameInp = el("input", "flex:1 1 0;min-width:0;");
        nameInp.className = "jzl-input";
        nameInp.type = "text";
        nameInp.value = String(readWidgetValue(nameW) ?? "") || "人物替换";
        nameInp.placeholder = "人物替换";
        nameInp.title = "落盘目录：output/jzl/{输出名称}/（下方宫格按此名称加载本批视频）";
        nameInp.addEventListener("mousedown", (e) => e.stopPropagation());
        nameInp.addEventListener("change", () => {
            const val = String(nameInp.value || "").trim() || "人物替换";
            if (nameW) setWidgetValue(nameW, val);
            try { node?.__jzlUpdateDisplay?.(); } catch (_) {}
            try { c.dispatchEvent(new Event("change", { bubbles: true })); } catch (_) {}
            if (refresh) refresh();   // 换名字后立刻切到该目录的视频
        });
        const nameRow = field("输出名称", nameInp);
        nameRow.title = "换人成片与各分段都落在 output/jzl/{输出名称}/";
        c.append(nameRow);
        c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:-4px 0 10px 172px;line-height:1.6;",
            "落盘目录：output/jzl/{输出名称}/。「生成视频查看器」读同名文件夹即可看到本批结果。"));
    }
    // ── 保存模式：默认「分段保存」，可勾选「拼接保存」「拼接保存后删除分段视频」──
    if (!settings.save) settings.save = { mode: "分段保存", auto_save: false, auto_merge: false, auto_merge_delete: false };
    const save = settings.save;
    const saveBox = el("div", "display:flex;align-items:center;gap:10px;margin:0 0 4px;flex-wrap:wrap;");
    saveBox.append(el("span", "font-size:12px;color:var(--fg-color,#ccc);white-space:nowrap;", "保存模式"));
    const mkCb = (text, title) => {
        const lab = el("label", "display:flex;align-items:center;gap:4px;font-size:12px;color:var(--fg-color,#ddd);white-space:nowrap;cursor:pointer;");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.style.cssText = "width:15px;height:15px;accent-color:#f59e0b;cursor:pointer;";
        cb.addEventListener("mousedown", (e) => e.stopPropagation());
        lab.append(cb, el("span", "", text));
        lab.title = title || text;
        return { lab, cb };
    };
    const seg = mkCb("分段保存", "必选：每段生成完立即落盘，输出本次各段 mp4（Max 恒定逐段落盘，不可取消）");
    const mer = mkCb("拼接保存", "附加：全部段生成后，再把本批各段读盘 concat 合并为一个完整视频（分段仍保留）");
    const del = mkCb("拼接保存后删除分段视频", "拼接保存成功后自动删除本批各分段 mp4，仅保留合并结果（需勾选拼接保存）");
    // 分段保存=必选：Max 恒定逐段落盘，勾选框固定勾选且不可取消；拼接保存为附加选项
    seg.cb.disabled = true;
    seg.cb.checked = true;
    seg.lab.style.opacity = "1";
    mer.cb.checked = save.mode === "拼接保存";
    del.cb.checked = !!save.auto_merge_delete;
    const syncDel = () => {
        del.cb.disabled = !mer.cb.checked;
        del.lab.style.opacity = mer.cb.checked ? "" : "0.45";
        if (!mer.cb.checked && del.cb.checked) { del.cb.checked = false; save.auto_merge_delete = false; }
    };
    mer.cb.addEventListener("change", () => { save.mode = mer.cb.checked ? "拼接保存" : "分段保存"; syncDel(); c.dispatchEvent(new Event("change", { bubbles: true })); });
    del.cb.addEventListener("change", () => { save.auto_merge_delete = !!del.cb.checked; c.dispatchEvent(new Event("change", { bubbles: true })); });
    syncDel();
    saveBox.append(seg.lab, mer.lab, del.lab);
    c.append(saveBox);
    c.append(el("div", "font-size:11px;color:var(--descrip-text,#999);margin:0 0 10px 56px;line-height:1.6;",
        "Max 恒定：每段【调度→编码→采样→解码】完成立即 ffmpeg 落盘 output/jzl/{故事名}/ 并释放显存/内存（跑 N 段与 1 段占用不叠加）。分段保存为必选项（恒保留本批各段）；勾选拼接保存=生成后另把本批分段读盘 concat 合并为一个完整视频；再勾选删除分段=合并成功后清理本批各分段 mp4。"));

    // ── 📥 加载视频：自动识别（跟随节点故事名）/ 选择已识别文件夹，6 列宫格 ──
    const top = el("div", "display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap;");
    top.append(el("span", "font-size:13px;font-weight:600;color:var(--fg-color,#eee);white-space:nowrap;", "📥 加载视频"));
    const sel = document.createElement("select");
    sel.style.cssText = "min-width:200px;max-width:360px;background:var(--comfy-input-bg,#1d1d1d);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);border-radius:4px;padding:6px 8px;font-size:12px;";
    sel.title = "自动识别=跟随节点「故事名称」自动对齐加载；或选择已识别到的故事文件夹";
    sel.addEventListener("mousedown", (e) => e.stopPropagation());
    sel.addEventListener("change", () => refresh && refresh());
    const rf = el("button", "background:#2a4a6a;color:#cfe3f7;border:1px solid #5b9bd5;border-radius:4px;padding:6px 12px;font-size:12px;cursor:pointer;", "🔄 刷新目录");
    rf.title = "刷新视频列表（按当前加载的故事文件夹）";
    rf.addEventListener("mousedown", (e) => e.stopPropagation());
    rf.addEventListener("click", () => { say("正在刷新…"); refresh && refresh(); });
    top.append(sel, rf);
    top.append(el("span", "font-size:11px;color:var(--descrip-text,#999);", "目录：output/jzl/{故事名}/（每行 6 列）"));
    c.append(top);
    // 6 列宫格（行数按视频数量自动换行，行高自适应）
    const grid = el("div", "display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;align-content:start;min-height:120px;");
    c.append(grid);

    refresh = async () => {
        const autoMode = String(sel.value || "") === AUTO_VAL;
        const sn = autoMode ? getStory() : String(sel.value || "").trim();
        grid.innerHTML = "";
        if (!sn) {
            grid.append(el("div", "font-size:12px;color:#888;padding:8px;grid-column:1/-1;", autoMode ? "自动识别：请在节点「故事名称」填写故事名，将自动对齐加载该故事的视频" : "请从上方下拉选择已识别到的故事文件夹，或先跑一次「Max」生成"));
            return;
        }
        try {
            const resp = await api.fetchApi(`/jzl/generated_videos?story_name=${encodeURIComponent(sn)}`);
            const data = await resp.json().catch(() => ({}));
            const vids = (data && data.ok && data.videos) || [];
            if (!vids.length) {
                grid.append(el("div", "font-size:12px;color:#888;padding:8px;grid-column:1/-1;", `output/jzl/${(data && data.story) || sn}/ 下暂无 mp4（运行一次「Max」生成后即会即时落盘）。`));
                return;
            }
            vids.forEach((v) => {
                const card = el("div", "cursor:pointer;border-radius:6px;border:1px solid #333;background:#111;overflow:hidden;");
                card.title = `${v.name}\n（点击预览播放）`;
                const im = document.createElement("img");
                im.style.cssText = "width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#000;";
                im.src = api.apiURL(`/jzl/video_thumb?path=${encodeURIComponent(v.path)}`);
                im.onerror = () => { im.remove(); const ic = el("div", "width:100%;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;font-size:24px;background:#000;", "🎬"); card.insertBefore(ic, card.firstChild); };
                const lb = el("div", "font-size:10px;color:#cdd8e2;text-align:center;padding:4px 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", v.name);
                card.append(im, lb);
                card.addEventListener("mousedown", (e) => e.stopPropagation());
                card.addEventListener("click", () => { if (v.path) showVideoPreview(v.path); });
                grid.append(card);
            });
        } catch (e) {
            grid.append(el("div", "font-size:12px;color:#e08a8a;padding:8px;grid-column:1/-1;", `加载失败：${(e && e.message) || e}`));
        }
    };

    // 下拉：✨ 自动识别（跟随节点故事名）+ 已识别故事文件夹（每项点选即加载）
    const fillStories = () => {
        sel.innerHTML = "";
        const cur = getStory();
        const autoOpt = document.createElement("option");
        autoOpt.value = AUTO_VAL;
        autoOpt.textContent = cur ? `✨ 自动识别（${cur}）` : "✨ 自动识别";
        autoOpt.title = "自动对齐节点「故事名称」，加载该故事文件夹下的视频";
        sel.appendChild(autoOpt);
        api.fetchApi("/jzl/generated_videos").then((r) => r.json().catch(() => ({}))).then((data) => {
            if (data && data.ok && Array.isArray(data.stories)) {
                data.stories.forEach((s) => {
                    if (!s || !s.story) return;
                    const o = document.createElement("option");
                    o.value = s.story;
                    o.textContent = `📂 ${s.story}${s.count ? `（${s.count}）` : ""}`;
                    sel.appendChild(o);
                });
            }
            sel.value = sel.value || AUTO_VAL;
            refresh && refresh();
        }).catch(() => { sel.value = sel.value || AUTO_VAL; refresh && refresh(); });
    };
    fillStories();
}

// 轻量 Markdown → HTML（标题/列表/代码块/加粗/行内代码/链接，够用即可）
function mdToHtml(md) {
    const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inline = (s) => {
        let out = esc(s);
        out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
        out = out.replace(/`([^`]+)`/g, "<code style='background:var(--comfy-input-bg,#222);padding:1px 4px;border-radius:3px;'>$1</code>");
        out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
        return out;
    };
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    let html = "";
    let inCode = false;
    let codeBuf = [];
    let inUl = false;
    const closeUl = () => { if (inUl) { html += "</ul>\n"; inUl = false; } };
    for (const raw of lines) {
        const t = raw.trim();
        if (t.startsWith("```")) {
            if (inCode) { html += "<pre style='background:var(--comfy-input-bg,#1d1d1d);padding:10px;border-radius:6px;overflow-x:auto;font-size:12px;line-height:1.5;'>" + esc(codeBuf.join("\n")) + "</pre>\n"; codeBuf = []; inCode = false; }
            else { closeUl(); inCode = true; }
            continue;
        }
        if (inCode) { codeBuf.push(raw); continue; }
        if (!t) { closeUl(); continue; }
        if (/^#{1,6}\s/.test(t)) {
            closeUl();
            const lvl = t.match(/^#+/)[0].length;
            html += `<h${lvl} style="margin:14px 0 6px;font-size:${lvl === 1 ? 18 : 15}px;font-weight:700;color:var(--fg-color,#eee);border-bottom:1px solid var(--border-color,#444);padding-bottom:3px;">${inline(t.replace(/^#+\s*/, ""))}</h${lvl}>\n`;
            continue;
        }
        if (/^[-*]\s/.test(t)) {
            if (!inUl) { html += "<ul style='padding-left:20px;'>\n"; inUl = true; }
            html += `<li style="margin:3px 0;">${inline(t.replace(/^[-*]\s*/, ""))}</li>\n`;
            continue;
        }
        closeUl();
        html += `<p style="margin:6px 0;">${inline(t)}</p>\n`;
    }
    if (inCode) html += "<pre style='background:var(--comfy-input-bg,#1d1d1d);padding:10px;border-radius:6px;overflow-x:auto;font-size:12px;line-height:1.5;'>" + esc(codeBuf.join("\n")) + "</pre>\n";
    closeUl();
    return html;
}

function buildModal(node, data, panelId) {
    ensureManagerStyle();
    const settings = (data && data.settings) ? data.settings : defaultSettings();
    const d = data || { llm_models: [], mmproj_models: ["None"], chat_handlers: ["None"] };
    const title = (PANELS[panelId] && PANELS[panelId].label) || "🎬 JZL - 🤖 MiniMax-H3短剧导演台Pro";

    const overlay = el("div", "position:fixed;inset:0;background:rgba(0,0,0,0.78);z-index:9999;display:flex;align-items:center;justify-content:center;overflow:auto;");
    // 固定为最大高度：切换面板时窗口尺寸不跳动；内容不足时底部留空（panelBox 仍是 flex:1）
    const dialog = el("section", "background:#1c1c1e;border:1px solid #333;border-radius:8px;width:820px;"
        + "height:90vh;max-height:90vh;display:flex;flex-direction:column;box-shadow:0 20px 40px rgba(0,0,0,0.6);flex:0 0 auto;");
    dialog.className = "jzl-modal";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-label", title);

    // 头部
    const header = el("div", "padding:20px;border-bottom:1px solid var(--border-color,#444);");
    header.append(el("div", "font-size:18px;font-weight:700;color:var(--fg-color,#eee);", title));
    header.append(el("div", "font-size:12px;color:var(--descrip-text,#999);margin-top:5px;", "根据本地环境配置参数，保存后重新执行节点生效。"));
    dialog.append(header);

    // 单面板内容：flex:1 + min-height:0 → 始终占满 dialog 剩余高度，面板之间切换不改变窗口大小
    const panelBox = el("div", "padding:16px 20px;overflow-y:auto;overflow-x:hidden;flex:1 1 auto;min-height:0;");
    switch (panelId) {
        case "assets": {
            const buildAssets = () => {
                panelBox.innerHTML = "";
                renderAutoSave(panelBox, settings);
                renderAssetsTools(panelBox, settings, buildAssets, node);
                renderAssetsPanel(panelBox, settings, d.mode);
            };
            panelBox.__jzlBuild = buildAssets;  // 供底部「清除」按钮清空后重渲染资产面板
            buildAssets();
            // 拖拽上传：文件拖到本面板 → 按文件类型归类上传，自动以文件名命名素材
            if (!panelBox.__jzlDropBound) {
                panelBox.__jzlDropBound = true;
                let _dragDepth = 0;
                panelBox.addEventListener("dragover", (e) => { e.preventDefault(); try { e.dataTransfer.dropEffect = "copy"; } catch (_) {} });
                panelBox.addEventListener("dragenter", (e) => { e.preventDefault(); _dragDepth++; panelBox.style.boxShadow = "inset 0 0 0 2px #5b9bd5"; });
                panelBox.addEventListener("dragleave", () => { if (--_dragDepth <= 0) { _dragDepth = 0; panelBox.style.boxShadow = ""; } });
                panelBox.addEventListener("drop", async (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    _dragDepth = 0;
                    panelBox.style.boxShadow = "";
                    const files = (e.dataTransfer && e.dataTransfer.files) ? Array.from(e.dataTransfer.files) : [];
                    if (!files.length) return;
                    let added = 0;
                    for (const file of files) {
                        const kind = detectAssetKindByFile(file);
                        if (!kind) continue;
                        const list = kind === "image" ? settings.assets.images : kind === "video" ? settings.assets.videos : settings.assets.audios;
                        const p = await uploadFileObject(file, kind);
                        if (p) {
                            const _t1 = (ASSET_TYPES_BY_KIND[kind] || ASSET_TYPES)[0];
                            list.push({ type: _t1, name: assetNameFromFilename(file.name), path: p, enabled: true, letter: nextFreeSlotLetter(kind, settings.assets, _t1) });
                            added++;
                        }
                    }
                    if (added) {
                        try { panelBox.__jzlBuild?.(); } catch (_) {}
                        panelBox.dispatchEvent(new Event("change", { bubbles: true }));
                        notify(`已拖拽上传 ${added} 个素材（自动以文件名命名）`, "success");
                    }
                });
            }
            break;
        }
        case "prompt": renderPromptPanel(panelBox, settings, d, node); break;
        case "preference":
            // 🎭 人物替换：100% 复用短剧导演台的采样解码设置（同一函数）+ 追加本节点独有的「段间衔接」
            if (node?.type === SWAP_NODE_TYPE) renderSwapPrefPanel(panelBox, settings, d, node);
            else renderPrefPanel(panelBox, settings, d, node);
            break;
        case "preference_settings": renderPreferenceSettingsPanel(panelBox, settings); break;
        case "gen_settings": renderGenSettingsPanel(panelBox, settings, node, d); break;
        case "video_params":
            if (node?.type === SWAP_NODE_TYPE) renderSwapVideoParamsPanel(panelBox, settings, node);
            else renderVideoParamsPanel(panelBox, settings, node);
            break;
        case "save_settings": renderSaveSettingsPanel(panelBox, settings, node, d); break;
        case "video_manager": renderVideoManagerPanel(panelBox, node, settings, d); break;
        case "help":
            if (node?.type === SWAP_NODE_TYPE) renderSwapHelpPanel(panelBox);
            else renderHelpPanel(panelBox);
            break;
        default: renderAutoSave(panelBox, settings);
    }
    dialog.append(panelBox);

    // 底部
    const error = el("div", "color:#e55;font-size:12px;margin:0 20px;min-height:16px;");
    const footer = el("div", "padding:15px 20px;border-top:1px solid var(--border-color,#444);display:flex;justify-content:flex-end;gap:10px;background:#18181a;border-bottom-left-radius:8px;border-bottom-right-radius:8px;");
    const cancelBtn = el("button", "background:transparent;border:1px solid var(--border-color,#555);color:#fff;border-radius:4px;padding:8px 20px;font-size:14px;cursor:pointer;", "取消");
    const saveBtn = el("button", "background:#2d5a88;color:#fff;border:none;border-radius:4px;padding:8px 20px;font-size:14px;font-weight:600;cursor:pointer;", "💾 保存");
    // 自动保存：统一移到弹窗底部，与「取消/保存」同行并靠左（样式与其他勾选框一致）
    const autoSaveRow = el("label", "display:flex;align-items:center;gap:6px;font-size:13px;color:var(--descrip-text,#999);cursor:pointer;");
    const autoSaveCb = el("input", "width:18px;height:18px;accent-color:#f59e0b;cursor:pointer;");
    autoSaveCb.type = "checkbox";
    autoSaveCb.checked = settings.auto_save !== false;
    autoSaveCb.addEventListener("change", () => { settings.auto_save = autoSaveCb.checked; });
    autoSaveRow.append(autoSaveCb, el("span", "", "自动保存"));
    // 字号 A−/A+（全局共享记忆，紧跟「自动保存」后面）；界面缩放 −/＋（整个弹窗本体，不缩放网页）
    const footerZoomRow = jzlBuildZoomRow(dialog);
    footerZoomRow.title = "调整本面板文本编辑字号（全局共享）";
    const footerUIRow = jzlBuildUIRow(dialog);
    footerUIRow.title = "调整整个弹窗界面大小（不缩放网页/画布）";
    const footerLeft = el("div", "display:flex;align-items:center;gap:16px;margin-right:auto;flex-wrap:wrap;");
    footerLeft.append(autoSaveRow, footerZoomRow, footerUIRow);
    footer.append(footerLeft, cancelBtn, saveBtn);
    // 「清除」按钮（仅参考素材管理面板显示）：一键清空所有素材，需二次弹窗确认
    if (panelId === "assets") {
        const clearBtn = el("button", "background:#7a2a2a;color:#ffd0d0;border:1px solid #a44;border-radius:4px;padding:8px 18px;font-size:14px;font-weight:600;cursor:pointer;", "🗑 清除");
        clearBtn.title = "一键清除所有已添加的素材（图片/视频/音频），需二次确认";
        clearBtn.addEventListener("click", () => {
            confirmClearAssets(async () => {
                settings.assets = { images: [], videos: [], audios: [] };
                if (node) node.__jzlAssets = { images: [], videos: [], audios: [] };
                try { panelBox.__jzlBuild?.(); } catch (_) {}   // 重渲染资产面板（清空后的空列表）
                await doSave(true, true);                       // 静默保存 + 刷新资产窗/提示词着色
                notify("🧹 已清除全部素材", "success");
            });
        });
        footer.insertBefore(clearBtn, cancelBtn);
    }
    dialog.append(error, footer);
    if (panelId === "help") { saveBtn.style.display = "none"; autoSaveRow.style.display = "none"; footerZoomRow.style.display = "none"; footerUIRow.style.display = "none"; }  // 使用说明面板无需保存/自动保存/字号/界面缩放

    overlay.style.overflow = "auto";  // 界面放大后超高可滚动查看
    overlay.append(dialog);
    document.body.append(overlay);
    // 打开时应用已记忆的全局字号 + 界面缩放（未设置过则保持默认）；Ctrl/⌘+滚轮 = 界面缩放（抢在浏览器整页缩放前）
    jzlZoomApplyContainer(dialog);
    jzlUIApply(dialog);
    jzlBindUIWheel(dialog);
    jzlBindWheelZoom(dialog);

    const close = () => { overlay.remove(); modal = null; };

    // 保存
    let autoSaveTimer = null;
    const doSave = async (silent, refreshAssets=false) => {
        try {
            await saveManager(node, settings);
            if (!silent) notify("配置已保存，重新执行节点生效");
            // 只有「引用资产设置」面板才需要实时刷新资产窗/提示词着色，其余面板只存不刷（避免卡顿）
            if (refreshAssets) notifyAssetsChanged(node);
            return true;
        } catch (e) {
            error.textContent = "保存失败：" + e.message;
            return false;
        }
    };
    saveBtn.addEventListener("click", async () => {
        saveBtn.disabled = true;
        saveBtn.textContent = "保存中…";
        const ok = await doSave(false, panelId === "assets");
        if (ok) close();
        else { saveBtn.disabled = false; saveBtn.textContent = "💾 保存"; }
    });

    // 自动保存：任何控件 change 后 1200ms 防抖保存（降低频繁整包 POST 的卡顿）
    panelBox.addEventListener("change", () => {
        if (settings.auto_save !== false) {
            clearTimeout(autoSaveTimer);
            autoSaveTimer = setTimeout(() => doSave(true, panelId === "assets"), 1200);
        }
    });

    cancelBtn.addEventListener("click", close);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });

    modal = { overlay, dialog, close, __data: settings };
}

// ── 🔄 重拍运行拦截（一次性 patch app.graphToPrompt）────────────────
// V3 前端 app.graphToPrompt 是 async（返回 Promise，内部 await 后才把 widget 值写入 workflow）。
// 同步 apply/restore 会因 await 时序失效；这里改为包装 Promise：
//   apply（同步，改 widget）→ 调用原生 graphToPrompt（async，生成 workflow 时读到穿透单段）
//   → Promise resolve 后 restore（恢复原参数）。
// 这样用户点 ComfyUI「运行」时，重拍模式会自动只用选中段生成一段视频（穿透生成）。
if (!app.__jzlReshootGraphPatched) {
    app.__jzlReshootGraphPatched = true;
    const _origGTP = app.graphToPrompt ? app.graphToPrompt.bind(app) : null;
    if (_origGTP) {
        app.graphToPrompt = function (...args) {
            const _g = this && this.graph ? this.graph : app.graph;
            const _ns = _g ? (_g._nodes || _g.nodes || []) : [];
            // ⭐ 提交前归一化「生成视频数量」：旧工作流/旧版前端可能把下拉用的「生成N段」
            //   存进了短剧导演台的数值输入 → ComfyUI 校验拒绝整个 prompt
            //   （Failed to convert an input value to a INT value: video_count, 生成1段）。
            //   这里以 widget 形态为准静默改写为等价合法值（幂等，不改变用户意图）。
            for (const _n of _ns) {
                if (_n && typeof _n.healVideoCount === "function") { try { _n.healVideoCount(); } catch (_) {} }
            }
            const _patches = [];
            for (const _n of _ns) {
                if (_n && typeof _n.__reshootBuildRunPatch === "function") {
                    const _p = _n.__reshootBuildRunPatch();
                    if (_p) _patches.push(_p);
                }
            }
            if (!_patches.length) return _origGTP.apply(this, args);
            for (const _p of _patches) { try { _p.apply(); } catch (_) {} }
            const _restore = () => { for (const _p of _patches) { try { _p.restore(); } catch (_) {} } };
            try {
                const _pr = _origGTP.apply(this, args);
                if (_pr && typeof _pr.then === "function") {
                    return _pr.then((_r) => { _restore(); return _r; }, (_e) => { _restore(); throw _e; });
                }
                _restore();   // 同步返回兜底
                return _pr;
            } catch (e) {
                _restore();
                throw e;
            }
        };
    }
}

// 🎭 人物替换的默认提示词（与后端 nodes_character_swap.DEFAULT_PROMPT 保持一致；
//   仅用于「旧工作流值错位」修复时还原，不作为执行依据）
const SWAP_DEFAULT_PROMPT = "把 <Video 1> 里的人物替换为 <Picture 1>。\n"
    + "严格保持 <Video 1> 的动作、表情、机位、场景、道具、光线与色彩不变。";

// 看起来像不像「节点配置 JSON」（只判断开头几个键，够用于错位识别）
function swapLooksLikeConfig(v) {
    return typeof v === "string" && /^\s*\{\s*"(auto_save|gen_params|sample_decode|save|models|assets)"/.test(v);
}

// console 在某些宿主/早期前端里可能不可用；日志失败绝不能中断修复流程
function swapLog(msg) {
    try { console.log(msg); } catch (_) {}
}

// 看起来像不像「换人提示词正文」（用于把错位到 story_name 的正文搬回来）
function swapLooksLikePrompt(v) {
    const s = String(v || "");
    return s.length >= 12 && (/<Video\s*\d>/.test(s) || /替换/.test(s) || /保持/.test(s));
}

// 旧版「人物替换」节点的值/端口修复：
//  ① 端口 source_frames → source_video（改名 + 移位后按位置还原会把连线挂到幽灵端口上，这里迁走连线）
//  ② 端口顺序归位（旧工作流按自身 inputs 顺序还原，会和 schema 顺序不一致 → 强制排成标准顺序）
//  ③ widget 值按名纠正：旧版 widgets_values = [prompt, story_name, display_info, manager_settings]，
//     新版 = [story_name, manager_settings, prompt] → 旧工作流载入后整体错位一格
//     （典型症状：提示词框里显示一串 {"auto_save":...} JSON，输出名称变成提示词正文）
const SWAP_INPUT_ORDER = ["model", "clip", "vae", "audio_vae", "source_video", "source_audio",
    "ref_images", "story_name", "manager_settings", "prompt"];

function reorderSwapInputs(self) {
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

function healSwapNodeLegacy(self) {
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
                        setWidgetValue(cand, "人物替换");
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
                setWidgetValue(snW, "人物替换");
                swapLog("[JZL-人物替换] 已把错位的提示词正文移回提示词框");
            }
        }
        // ⑤ 提示词仍为空（错位后彻底丢失）→ 填回默认提示词，避免用户面对空白框
        if (prW && !String(readWidgetValue(prW) || "").trim()) {
            setWidgetValue(prW, SWAP_DEFAULT_PROMPT);
        }
        try { self.__jzlSwapSyncPrompt?.(); } catch (_) {}
        try { self.__jzlUpdateDisplay?.(); } catch (_) {}
    } catch (_) {}
}

// ── 🎭 人物替换：节点表面只留「4 个按钮 + 两行方案信息」，其余全部收进面板；
//    提示词固定在最底部（DOM 按钮块插到 prompt widget 之前） ──
function setupSwapNode(self) {
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
        { label: "📖 节点使用说明", panel: "help" },
    ];
    for (const b of buttons) {
        const btn = document.createElement("button");
        btn.textContent = b.label;
        btn.style.cssText = ["width:100%;", `height:${btnH}px;`, "box-sizing:border-box;",
            "display:flex;", "align-items:center;", "justify-content:center;",
            "border-radius:6px;", "border:2px solid #5b9bd5;", "background:#3a3a3a;",
            "color:#eee;", "font-size:14px;", "cursor:pointer;", "white-space:nowrap;",
            "overflow:hidden;", "text-overflow:ellipsis;"].join("");
        btn.addEventListener("mousedown", (e) => e.stopPropagation());
        btn.addEventListener("mouseenter", () => { btn.style.background = "#4a4a4a"; });
        btn.addEventListener("mouseleave", () => { btn.style.background = "#3a3a3a"; });
        btn.addEventListener("click", () => openModal(self, b.panel));
        grid.appendChild(btn);
    }
    container.appendChild(grid);

    // 两行方案信息（只读）：第 1 行＝画布/段长/衔接；第 2 行＝音频/二采/采样/输出名称
    const infoRow = el("div", "flex:0 0 auto;font-size:11px;color:#c9a86a;line-height:1.55;white-space:pre-line;"
        + "overflow:hidden;min-height:28px;word-break:break-all;", "");
    infoRow.title = "当前方案（只读）：画布 / 每段帧数 / 分段衔接 / 音频 / 二采 / 采样步数 / 输出名称 —— 在四个按钮面板里修改";
    infoRow.addEventListener("mousedown", (e) => e.stopPropagation());
    container.appendChild(infoRow);
    self.__jzlSwapInfo = infoRow;

    // ── 提示词（DOM textarea，flex:1 占满剩余高度；隐藏原生 prompt widget 由这里代理）──
    // 与导演台同一套做法：原生 widget 的渲染高度跟着节点乱跳，改成自己的 textarea 才能「跟着节点变高」。
    const promptLabel = el("div", "flex:0 0 auto;font-size:12px;color:#bbb;", "📝 提示词");
    promptLabel.title = "换人提示词。占位符：{seg} 本段序号 / {total} 总段数 / {start} 起始帧 / {frames} 本段帧数";
    promptLabel.addEventListener("mousedown", (e) => e.stopPropagation());
    container.appendChild(promptLabel);

    const promptWrap = el("div", "flex:1 1 auto;min-height:60px;display:flex;position:relative;");
    promptWrap.setAttribute("data-capture-wheel", "true");   // 滚轮归本框（不被画布缩放抢走）
    const promptTa = document.createElement("textarea");
    promptTa.className = "jzl-input";
    promptTa.spellcheck = false;
    promptTa.placeholder = "把 <Video 1> 里的人物替换为 <Picture 1>…";
    promptTa.style.cssText = "width:100%;height:100%;box-sizing:border-box;resize:none;"
        + "background:#2a2a2a;color:#ddd;border:1px solid #444;border-radius:4px;"
        + "padding:6px 8px;font-size:12px;line-height:1.5;font-family:inherit;overflow-y:auto;white-space:pre-wrap;";
    promptTa.addEventListener("mousedown", (e) => e.stopPropagation());
    promptWrap.appendChild(promptTa);
    container.appendChild(promptWrap);

    const promptW = () => (self.widgets || []).find((w) => w && w.name === "prompt");
    // 隐藏原生 prompt widget（值仍照常序列化 → 执行端读到的还是它）
    const hideNativePrompt = () => {
        const pw = promptW();
        if (!pw) return;
        pw.hidden = true;
        if (!pw.options) pw.options = {};
        pw.options.hidden = true;
        try { delete pw.computeSize; } catch (_) { pw.computeSize = undefined; }
        pw.computeSize = () => [0, -4];
    };
    hideNativePrompt();
    // widget → textarea（工作流载入 / 修复后回填）
    const syncPromptFromWidget = () => {
        const pw = promptW();
        const v = String(readWidgetValue(pw) ?? "");
        if (promptTa.value !== v) promptTa.value = v;
    };
    // textarea → widget（用户输入）
    promptTa.addEventListener("input", () => {
        const pw = promptW();
        if (pw) setWidgetValue(pw, promptTa.value);
        try { self.graph?.setDirtyCanvas?.(true, false); } catch (_) {}
    });
    promptTa.addEventListener("change", () => {
        const pw = promptW();
        if (pw) setWidgetValue(pw, promptTa.value);
    });
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
    const storyName = () => {
        const sw = (self.widgets || []).find((x) => x.name === "story_name");
        return String(readWidgetValue(sw) || "").trim();
    };

    // 两行方案信息：与「🎨/🎭」面板写进 manager_settings 的值实时同步
    const updateDisplay = () => {
        const s = readCfg();
        const gp = s.gen_params || {};
        const sd = s.sample_decode || {};
        const seam = sd.seam || {};
        const sec = sd.second || {};
        const dv = (x) => `<span style="color:#9fd6a4">${x}</span>`;
        const ar = String(gp.aspect_ratio || "跟随源片（推荐）");
        let canvasTxt = "跟随源片";
        if (!ar.startsWith("跟随源片")) {
            const [W, H] = jzlCalcResolution(ar, Number(gp.megapixels ?? 0.4));
            canvasTxt = `${W}×${H}`;
        } else {
            canvasTxt = `跟随源片·${Number(gp.megapixels ?? 0.4).toFixed(2)}MP`;
        }
        const f = swapFrameLen(gp.segment || "固定5秒");
        const up = Number(gp.upscale_scale ?? 1.0);
        const isSL = String(sd.sample_mode || "常规采样") === "SelfLift采样";
        const secTgt = String(gp.second_mode || "目标分辨率") === "目标分辨率";
        const secOn = sec.enabled !== false;
        const secTxt = isSL ? "跳过(SelfLift)"
            : (!secOn ? "关闭"
                : (secTgt ? `${gp.second_width ?? 1280}×${gp.second_height ?? 704}`
                          : `×${up.toFixed(2)}`));
        const seamTxt = seam.enabled === false ? "关闭" : `窗口${seam.window ?? 22}+过渡${seam.settle ?? 12}`;
        const guideTxt = String(seam.guide_mode || "pixels") === "latent" ? "latent(旧)" : "像素";
        const pinTxt = seam.pin_second === false ? "1采" : "1采+2采";
        const audioTxt = String(gp.audio_mode || "保留原声（推荐）").replace("（推荐）", "");
        const sampTxt = isSL
            ? `SelfLift ${sd.selflift?.steps ?? 8}步/${sd.scheduler || "simple"}`
            : `${sd.sampler || "res_multistep"} ${sd.steps ?? 5}步/${sd.scheduler || "simple"}`;
        const seedTxt = String(sd.seed_mode || "fixed") === "randomize" ? "随机" : String(sd.seed ?? 0);
        const nameTxt = storyName() || "人物替换";
        infoRow.innerHTML =
            `画布：${dv(canvasTxt)} 丨 每段：${dv(`${f}帧/${(f / 24).toFixed(2)}秒`)}`
            + ` 丨 衔接：${dv(seamTxt)} 丨 引导：${dv(guideTxt)}·${dv(pinTxt)}`
            + `\n参考图：${dv(`放大${Number(gp.scale_factor ?? 1).toFixed(1)}×`)} 丨 二采：${dv(secTxt)} 丨 采样：${dv(sampTxt)}`
            + ` 丨 音频：${dv(audioTxt)} 丨 种子：${dv(seedTxt)} 丨 输出：${dv(nameTxt)}`;
    };
    self.__jzlUpdateDisplay = updateDisplay;
    setTimeout(updateDisplay, 0);
    setTimeout(updateDisplay, 300);
    try { if (!Number.isFinite(self.size?.[1]) || self.size[1] < 400) self.setSize?.([560, 520]); } catch (_) {}
}

app.registerExtension({
    name: "JZL.MiniMaxAssetManager",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        // 🎬 生成视频查看器（独立节点）：DOM 2 列多宫格预览 + 顶部 复制剧本/查看剧本
        if (nodeData?.name === VIEWER_NODE_TYPE) {
            const _vOrig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const _r = _vOrig?.apply(this, arguments);
                try { setupVideoViewerNode(this); } catch (e) { console.error("[JZL-Viewer]", e); }
                return _r;
            };
            const _vExec = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (msg) {
                const _r = _vExec?.apply(this, arguments);
                // 本节点执行后自动同步一次：跟随「短剧导演台Max」故事名 → 读最新视频 / 「故事拆解」剧本（直接读盘，不经端口）
                try { if (this.__viewerRefresh) this.__viewerRefresh(); } catch (_e) {}
                return _r;
            };
            return;
        }
        // 📝 生成剧本查看器（独立节点）：剧本显示窗 + 顶部 刷新/复制/编辑/选择剧本（可进子文件夹）
        if (nodeData?.name === SCRIPT_VIEWER_NODE_TYPE) {
            const _sOrig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const _r = _sOrig?.apply(this, arguments);
                try { setupScriptViewerNode(this); } catch (e) { console.error("[JZL-ScriptViewer]", e); }
                return _r;
            };
            const _sExec = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (msg) {
                const _r = _sExec?.apply(this, arguments);
                // 执行后自动同步一次：跟随导演台故事名 → 读最新剧本（优先已增强，否则故事拆解）
                try { if (this.__scriptRefresh) this.__scriptRefresh(); } catch (_e) {}
                return _r;
            };
            return;
        }
        const isMini = nodeData?.name === MINI_NODE_TYPE;
        const isShortDrama = nodeData?.name === SHORTDRAMA_NODE_TYPE;
        // 🎭 人物替换：独立节点（4 个按钮 + 两行方案信息），走自己的 setup，不混进导演台那套资产/提示词 DOM
        if (nodeData?.name === SWAP_NODE_TYPE) {
            const _swOrig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const _r = _swOrig?.apply(this, arguments);
                try { setupSwapNode(this); } catch (e) { console.error("[JZL-Swap]", e); }
                // 值/端口修复要在 configure 之后再跑一次（工作流载入时 widgets_values 是异步还原的）
                const self = this;
                [0, 60, 400].forEach((ms) => setTimeout(() => { try { healSwapNodeLegacy(self); } catch (_) {} }, ms));
                return _r;
            };
            const _swCfg = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function (info) {
                const _r = _swCfg?.apply(this, arguments);
                try { healSwapNodeLegacy(this); } catch (e) { console.error("[JZL-Swap-heal]", e); }
                const self = this;
                setTimeout(() => { try { healSwapNodeLegacy(self); } catch (_) {} }, 200);
                return _r;
            };
            return;
        }
        // 无限时长 / 短剧导演台 与 Max 共用按钮布局 + 保存/采样解码面板
        const isMax = nodeData?.name === MAX_NODE_TYPE || nodeData?.name === INF_NODE_TYPE || isShortDrama;
        if (nodeData?.name !== NODE_TYPE && !isMini && !isMax) return;
        const orig = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = orig?.apply(this, arguments);
            const self = this;
            // Mini：删除「视频保存设置」「采样解码设置」按钮（只做资产管理+编码）
            // Max：去掉「视频保存设置」（已融合进「生成视频管理」面板）→ 8 按钮 4 列 2 排
            // Max 按钮顺序（4 列 2 排）：「生成视频管理」放到「常用提示词元素」前面
            const _btnOf = (wid) => PANEL_BUTTONS.find((b) => b.widget === wid);
            const buttons = isMini
                ? PANEL_BUTTONS.filter((b) => b.widget !== "btn_save" && b.widget !== "btn_pref")
                : (isShortDrama
                    // 🎬 短剧导演台：第一排 2 个（新面板，各占半排）+ 第二排 4 + 第三排 4
                    ? [
                        { widget: "btn_gen_settings", label: "🎛️ 运行参数设置", panel: "gen_settings", span: 2 },
                        { widget: "btn_video_params", label: "🎨 视频参数设置", panel: "video_params", span: 2 },
                        _btnOf("btn_assets"),
                        _btnOf("btn_prompt"),
                        _btnOf("btn_pref"),
                        { widget: "btn_video_manager", label: "🎬 生成视频管理", panel: "video_manager" },
                        _btnOf("btn_elements"),
                        _btnOf("btn_preference"),
                        _btnOf("btn_align"),
                        _btnOf("btn_help"),
                    ].filter(Boolean)
                    : (isMax
                        ? [
                            _btnOf("btn_assets"),
                            _btnOf("btn_prompt"),
                            _btnOf("btn_pref"),
                            { widget: "btn_video_manager", label: "🎬 生成视频管理", panel: "video_manager" },
                            _btnOf("btn_elements"),
                            _btnOf("btn_preference"),
                            _btnOf("btn_align"),
                            _btnOf("btn_help"),
                        ].filter(Boolean)
                        : PANEL_BUTTONS));
            self.__jzlButtons = buttons;

            // 纯文本模式（「仅提示词输出」/「故事扩写模式」）→ 自动静音 上游(model/clip/vae/audio_vae)
            // + 下游（除剧本文本端口：已处理剧本/已拆解剧本链路保活）：切到这些模式只跑 LLM 文本处理，
            // 不触发上游模型加载/参考编码、不让下游编码/采样空跑；切回其它模式自动恢复。
            try {
                const _rmW = (self.widgets || []).find((w) => w.name === "run_mode");
                if (_rmW) {
                    const _origRmCb = _rmW.callback;
                    _rmW.callback = function (val) {
                        _origRmCb?.apply?.(this, arguments);
                        try {
                            // 以 widget 当前值判断（val 参数在某些 ComfyUI 版本可能为空/不一致，兜底取 _rmW.value）
                            const _cur = (val !== undefined && val !== null && String(val).trim() !== "")
                                ? String(val).trim()
                                : String(_rmW.value != null ? _rmW.value : "").trim();
                            jzlSetPurePromptMute(self, JZL_PURE_TEXT_MODES.has(jzlNormalizeRunMode(_cur)));
                        } catch (_e) {}
                    };
                }
                // 值恢复（configure / 图加载）后按当前模式执行一次
                setTimeout(() => {
                    try {
                        const _rw = (self.widgets || []).find((w) => w.name === "run_mode");
                        if (_rw) jzlSetPurePromptMute(self, JZL_PURE_TEXT_MODES.has(jzlNormalizeRunMode(String(_rw.value || ""))));
                    } catch (_e) {}
                }, 300);
            } catch (_e) {}

            // 1. 隐藏内部存储 widget（internal_prompt）与旧 mode
            const vcWidget = (self.widgets || []).find((w) => w.name === "video_count");
            const ipWidget = (self.widgets || []).find((w) => w.name === "internal_prompt");
            // 🎬 短剧导演台：运行模式 / 故事风格 / 故事名称 / 生成详情 / 画幅比例~生成数量 全部移入面板 → 节点表面隐藏
            const hiddenNames = isShortDrama
                ? new Set(["mode", "internal_prompt", "manager_settings",
                           "run_mode", "story_style", "story_name", "display_info",
                           "aspect_ratio", "megapixels", "duration",
                           "scale_factor", "upscale_scale", "video_count"])
                : new Set(["mode", "internal_prompt", "manager_settings"]);
            for (const w of self.widgets || []) {
                if (!w) continue;
                const nm = w.name || "";
                if (nm === "jzl_manager") continue;  // DOM widget 保留
                if (!hiddenNames.has(nm) && !nm.startsWith("btn_")) continue;
                w.hidden = true;
                if (!w.options) w.options = {};
                w.options.hidden = true;
                // 关键：无条件强制布局高度为 0（原生 widget 没有 computeSize，必须无条件赋值）
                w.computeSize = () => [0, -4];
            }
            // Nodes 2.0：上面是在 onNodeCreated 阶段改的可见性，仅改 type/hidden/options.hidden 不会让 DOM 立刻重渲染
            // （实测：触发 node:slot-label:changed 内部事件即可刷新节点数据）
            try { if (Array.isArray(self.widgets)) self.widgets = self.widgets.slice(); } catch (_) {}
            try { self.graph?.trigger?.("node:slot-label:changed", { nodeId: self.id, slotType: 2 }); } catch (_) {}

            // 2. 单个 DOM widget：按钮区 + 生成参数(节点表面) + 视频数量 + 加速模式 + 提示词输入
            ensureManagerStyle();
            const container = document.createElement("div");
            // ⚠️ overflow-y:auto（不是 hidden）：🎬 短剧导演台 比其它导演台多 1 排按钮 + 1 行「生成详情」，
            // 用户把节点拉矮时若用 hidden，最下面的「重拍模式」会被直接裁掉、滚动也够不着。
            // 改成可纵向滚动后：高矮都能滚到全部内容（横向仍锁死，避免出现左右滚动条）。
            container.style.cssText = "width:100%;height:100%;display:flex;flex-direction:column;gap:6px;padding:8px;box-sizing:border-box;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;";

            // 按钮区（Mini 6 按钮 3 列 2 排；Max 8 按钮 4 列 2 排；Pro 8 按钮 4 列 2 排；短剧导演台 10 按钮 4 列 3 排）
            // 🎬 短剧导演台按钮更多更密 → 高度 +20%（30→36px）、字号加大，便于点选
            const btnH = isShortDrama ? 36 : 30;
            const btnFont = isShortDrama ? 16 : 14;
            const btnRowH = btnH + 6;   // 行高 = 按钮高 + 行间距（与 gap 一致）
            const btnGrid = document.createElement("div");
            const btnCols = isMax ? 4 : (isMini ? 3 : 4);
            self.__jzlBtnCols = btnCols;
            btnGrid.style.cssText = `display:grid;grid-template-columns:repeat(${btnCols},1fr);grid-auto-rows:${btnH}px;gap:6px;flex:0 0 auto;`;
            for (const b of buttons) {
                const btn = document.createElement("button");
                btn.textContent = b.label;
                btn.style.cssText = [
                    "width:100%;", `height:${btnH}px;`, "box-sizing:border-box;",
                    "display:flex;", "align-items:center;", "justify-content:center;",
                    "border-radius:6px;", "border:2px solid #5b9bd5;",
                    "background:#3a3a3a;", "color:#eee;", `font-size:${btnFont}px;`,
                    "cursor:pointer;", "white-space:nowrap;", "overflow:hidden;", "text-overflow:ellipsis;",
                ].join("");
                if (b.widget === "btn_align") { btn.dataset.alignBtn = "1"; self.__btnAlign = btn; }
                if (b.span) btn.style.gridColumn = `span ${b.span}`;   // 生成设定 / 视频参数：各占半排
                btn.addEventListener("mousedown", (e) => e.stopPropagation());
                btn.addEventListener("mouseenter", () => { if (btn.dataset.alignBtn) self.__alignBtnBg?.(true); else btn.style.background = "#4a4a4a"; });
                btn.addEventListener("mouseleave", () => { if (btn.dataset.alignBtn) self.__alignBtnBg?.(false); else btn.style.background = "#3a3a3a"; });
                btn.addEventListener("click", () => {
                    if (b.panel === "prompt_elements") { showPromptElements(self); return; }
                    if (b.panel === "align") { toggleAlignPrompt(self); return; }
                    openModal(self, b.panel);
                });
                btnGrid.appendChild(btn);
            }
            container.appendChild(btnGrid);

            let infoRow = null;
            if (isShortDrama) {
                // 🎬 短剧导演台：把「生成设定」三项（运行模式/风格/故事名）与「生成详情」显示在节点上
                // （这些参数在节点表面的原生 widget 已隐藏，改由面板设置、这里只读展示）
                const genRow = el("div", "flex:0 0 auto;font-size:11px;color:#9fb8d6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:14px;", "");
                genRow.title = "生成设定（运行模式 / 故事风格 / 故事名称）—— 在「🎛️ 运行参数设置」面板里修改";
                genRow.addEventListener("mousedown", (e) => e.stopPropagation());
                container.appendChild(genRow);
                self.__jzlGenRow = genRow;

                const detailRow = el("div", "flex:0 0 auto;font-size:11px;color:#c9a86a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:14px;", "");
                detailRow.title = "生成详情（分辨率 / 每段帧数 / 段数 / 成片总长），只读显示";
                detailRow.addEventListener("mousedown", (e) => e.stopPropagation());
                container.appendChild(detailRow);
                self.__jzlDetailRow = detailRow;
            }

            // 生成信息状态行（实时显示，纯只读；值用绿色高亮；参数保存后自动同步）
            const _jzlValColor = "#9fd6a4";  // 结果值高亮色（区别于标签）
            infoRow = el("div", "flex:0 0 auto;font-size:11px;color:#c9a86a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:14px;", "");
            infoRow.title = "当前生成配置（LLM语言/后端/增强 + 采样步数/二采/采样种子），只读显示";
            infoRow.addEventListener("mousedown", (e) => e.stopPropagation());
            container.appendChild(infoRow);
            const _jzlVal = (x) => `<span style="color:${_jzlValColor}">${x}</span>`;
            const updateInfo = () => {
                try {
                    const msW = (self.widgets || []).find((x) => x.name === "manager_settings");
                    let s = {};
                    if (msW) { try { s = JSON.parse(readWidgetValue(msW) || "{}"); } catch (_) {} }
                    const enh = s.enhance || {};
                    const sd = s.sample_decode || {};
                    const sec = sd.second || {};
                    const lang = String(enh.prompt_lang || "中文 [ZH]");
                    const langShort = lang.includes("EN") ? "英文[EN]" : "中文[ZH]";
                    const mode = String(enh.llm_backend || "").includes("api") ? "API" : "本地";
                    const enhOn = enh.enabled ? "开启" : "关闭";
                    const _isSL = String(sd.sample_mode || "常规采样") === "SelfLift采样";
                    // 步数按当前采样分类取：SelfLift 有自己的独立步数（默认 8），未设置时回退共用值
                    const steps = _isSL ? (sd.selflift?.steps ?? sd.steps ?? 8) : (sd.steps ?? 4);
                    // SelfLift 模式下二采被跳过（SelfLift 自身即渐进分辨率采样），显示上要说清
                    const secOn = !sec.enabled ? "关闭" : (_isSL ? "跳过(SelfLift)" : "开启");
                    const seedMode = String(sd.seed_mode || sd.seed_control || "randomize");
                    // 种子以字符串保存（可超 2^53，Number() 会丢精度）→ 直接按字符串显示
                    const _sTxt = (v) => (v === undefined || v === null || v === "") ? "" : String(v);
                    const sampSeed = _sTxt(sd.seed);
                    const llmSeed = _sTxt(enh.seed);
                    // 随机种：显示「本轮实际用过的种子」——有采样种子优先；纯文本模式（不采样）用 LLM 种子；
                    // 都没有（随机模式且尚未运行）才显示「随机(运行后更新)」，避免一直停在误导性的 0
                    const seedTxt = sampSeed || llmSeed || "随机(运行后更新)";
                    infoRow.innerHTML = `LLM语言：${_jzlVal(langShort)} 丨 LLM模式：${_jzlVal(mode)} 丨 文本增强：${_jzlVal(enhOn)} 丨 采样：${_jzlVal(_isSL ? "SelfLift" : "常规")} 丨 采样步数：${_jzlVal(steps)} 丨 二采功能：${_jzlVal(secOn)} 丨 随机种：${_jzlVal(seedTxt)}`;
                    // 🎬 短剧导演台：生成设定三项（运行模式 / 故事风格 / 故事名称）同步到节点上的只读行
                    if (self.__jzlGenRow) {
                        const _gv = (n) => {
                            const w = (self.widgets || []).find((x) => x.name === n);
                            const v = readWidgetValue(w);
                            return (v === undefined || v === null) ? "" : String(v).trim();
                        };
                        let _sbOn = false;
                        try {
                            const _cfg = JSON.parse(readWidgetValue((self.widgets || []).find((x) => x.name === "manager_settings")) || "{}");
                            _sbOn = !!((_cfg.semantic_bridge || {}).enabled);
                        } catch (_) {}
                        self.__jzlGenRow.textContent = `生成模式：${_gv("run_mode") || "-"} 丨 风格：${_gv("story_style") || "-"} 丨 故事：${_gv("story_name") || "-"} 丨 语义修正：${_sbOn ? "开" : "关"}`;
                    }
                } catch (_) {}
            };
            self.__jzlUpdateInfo = updateInfo;  // 设置面板保存后由 saveManager 触发刷新

            // 提示词：内部编辑窗口（DOM 富文本 @ 着色）。可接外部：用节点顶部「提示词·接线」输入框（同 CLIP Text Encode 的左上圆点）连上游文本，连线后以上游为准。
            // 📝 提示词标签行：左侧标签/状态，右端放「复制 / 放大编辑」按钮（在框外，不遮挡正文）
            const promptLabel = el("div", "flex:0 0 auto;display:flex;align-items:center;gap:8px;font-size:12px;color:#bbb;", "📝 提示词（用 @ 引用素材）");
            const alignStatus = el("span", "font-size:11px;margin-left:8px;", "");
            const extStatus = el("span", "font-size:11px;margin-left:8px;color:#8a9fb8;white-space:nowrap;", "⇶ 可接上游文本");
            extStatus.title = "提示词接线：把上游 STRING 文本节点输出拖到节点顶部「提示词·接线」输入框（左上角官方圆点，同 CLIP Text Encode）；连线后运行以上游文本为提示词（未接时用本框内容）";
            // 🎬 短剧导演台已移除「提示词·接线」，不再显示接线角标
            if (isShortDrama) promptLabel.append(alignStatus);
            else promptLabel.append(alignStatus, extStatus);
            container.appendChild(promptLabel);
            const promptBox = document.createElement("div");
            promptBox.contentEditable = "true";
            promptBox.spellcheck = false;
            promptBox.dataset.placeholder = "输入故事/剧本提示词，用 @ 引用素材…";
            promptBox.innerText = ipWidget?.value ?? "";
            promptBox.style.cssText = "width:100%;flex:1 1 auto;min-height:60px;box-sizing:border-box;background:#2a2a2a;color:#ddd;border:1px solid #444;border-radius:4px;padding:6px 8px;font-size:12px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;";
            promptBox.addEventListener("mousedown", (e) => e.stopPropagation());
            // 右上角悬浮：复制全部文本 / 放大编辑
            const promptWrap = el("div", "position:relative;flex:1 1 auto;display:flex;flex-direction:column;min-height:60px;");
            // Nodes 2.0：告知前端「滚轮归本控件」（官方判定：e.target.closest('[data-capture-wheel="true"]')
            // 且 document.activeElement 在该元素内 —— 见前端 settingStore 的 wheelCapturedByFocusedElement）。
            // 不加这个标记时，滚轮会被前端转发给画布（实测：框 scrollTop 不变、画布缩放 0.382→0.347）。
            promptWrap.setAttribute("data-capture-wheel", "true");
            promptWrap.appendChild(promptBox);
            // 复制 / 放大编辑：放到标签行右端（框外右上角），不再悬浮在框内遮挡正文
            const pActions = el("div", "display:flex;gap:4px;margin-left:auto;z-index:6;");
            const pCopy = el("button", "width:22px;height:22px;border-radius:4px;border:1px solid #555;background:rgba(30,30,30,.92);color:#ccc;font-size:12px;cursor:pointer;line-height:1;padding:0;", "📋");
            pCopy.title = "复制全部文本";
            const pZoom = el("button", "width:22px;height:22px;border-radius:4px;border:1px solid #555;background:rgba(30,30,30,.92);color:#ccc;font-size:12px;cursor:pointer;line-height:1;padding:0;", "⛶");
            pZoom.title = "放大编辑（打开大界面）";
            pActions.append(pCopy, pZoom);
            promptLabel.appendChild(pActions);
            pCopy.addEventListener("mousedown", (e) => e.stopPropagation());
            pCopy.addEventListener("click", (e) => { e.stopPropagation(); copyTextToClipboard(getPromptText(promptBox), "提示词"); });
            pZoom.addEventListener("mousedown", (e) => e.stopPropagation());
            pZoom.addEventListener("click", (e) => {
                e.stopPropagation();
                openTextZoomEditor("📝 提示词（放大编辑）", () => getPromptText(promptBox), (text) => {
                    if (ipWidget) setWidgetValue(ipWidget, text);
                    if (self.__jzlAlignMode !== "text") renderPromptFromText(promptBox, text, self);
                    else { promptBox.innerHTML = ""; promptBox.appendChild(document.createTextNode(text)); }
                }, self);
            });
            container.appendChild(promptWrap);
            self.__promptBox = promptBox;
            promptBox.__node = self;
            setupInternalPrompt(self, promptBox, ipWidget);

            // 外部提示词接线状态角标（onDraw 每帧同步）
            const updateExt = () => {
                try {
                    const _inp = (self.inputs || []).find((i) => i.name === "external_prompt");
                    const _on = !!(_inp && _inp.link != null);
                    const _t = _on ? "⇶ 外部提示词已接入（输入框已锁定）" : "⇶ 可接上游文本";
                    if (extStatus.textContent !== _t) {
                        extStatus.textContent = _t;
                        extStatus.style.color = _on ? "#6fce8a" : "#8a9fb8";
                    }
                    // 接入外部提示词 → 节点内大提示词框锁定不可编辑（以上游文本为准）；断开「提示词·接线」后恢复
                    if (promptBox.isContentEditable === _on) {
                        if (_on) {
                            promptBox.contentEditable = "false";
                            promptBox.style.background = "#202020";
                            promptBox.style.color = "#8a8a8a";
                            promptBox.style.cursor = "not-allowed";
                            promptBox.title = "已接入外部提示词：本框锁定不可编辑（以上游文本为准）；断开「提示词·接线」后可恢复";
                            try { promptBox.blur(); } catch (_b) {}
                        } else {
                            promptBox.contentEditable = "true";
                            promptBox.style.background = "#2a2a2a";
                            promptBox.style.color = "#ddd";
                            promptBox.style.cursor = "";
                            promptBox.title = "";
                        }
                    }
                    // 放大编辑在锁定时禁用（改了大框也不生效，避免误导）
                    if (pZoom.disabled !== _on) {
                        pZoom.disabled = _on;
                        pZoom.style.opacity = _on ? "0.4" : "";
                        pZoom.style.cursor = _on ? "not-allowed" : "pointer";
                    }
                } catch (_) {}
            };
            self.__jzlUpdateExt = updateExt;
            updateExt();

            // 参考元素切换状态（纯文本/显示引用）：默认「纯文本」（关闭）；只由「🎯 参考元素切换」按钮手动切换，
            // 资产变化（新增/修改素材）时保持当前模式、不自动切换。
            self.__jzlAlignMode = "text";
            const alignBtnBg = (hover) => {
                const b = self.__btnAlign;
                if (!b) return;
                const ref = self.__jzlAlignMode !== "text";
                b.style.background = ref ? (hover ? "#e8930c" : "#d97706") : (hover ? "#4a4a4a" : "#3a3a3a");
                b.style.borderColor = ref ? "#f59e0b" : "#5b9bd5";
            };
            const updateAlignStatus = () => {
                if (alignStatus) {
                    const ref = self.__jzlAlignMode !== "text";
                    alignStatus.textContent = `参考元素：${ref ? "显示引用" : "纯文本"}`;
                    alignStatus.style.color = ref ? "#5ecf8a" : "#bbb";
                    alignStatus.title = "由「🎯 参考元素切换」手动控制；已随工作流保存，刷新不丢失";
                }
                alignBtnBg(false);
            };
            self.__updateAlignStatus = updateAlignStatus;
            self.__alignBtnBg = alignBtnBg;
            updateAlignStatus();

            // Bug 1 修复：V3 的 migrateWidgetsValues 在加载时会按 schema 重排 widgets_values，
            // 但 addDOMWidget 生成的 jzl_manager 混在 widgets 数组里（schema 无此项）导致索引错位，
            // internal_prompt 会拿到 jzl_manager 的空值 → 刷新/重载后提示词丢失。
            // 这里在 configure 前后备份并回填 internal_prompt 值（按保存顺序的 widgets 下标取原始值）。
            const baseConfigure = self.configure.bind(self);
            self.configure = function (info) {
                let rawIP, rawMS;
                if (info && Array.isArray(info.widgets_values)) {
                    const ipIdx = (self.widgets || []).findIndex((w) => w.name === "internal_prompt");
                    if (ipIdx >= 0 && ipIdx < info.widgets_values.length) rawIP = info.widgets_values[ipIdx];
                    const msIdx = (self.widgets || []).findIndex((w) => w.name === "manager_settings");
                    if (msIdx >= 0 && msIdx < info.widgets_values.length) rawMS = info.widgets_values[msIdx];
                }
                const r = baseConfigure(info);
                if (rawMS !== undefined && rawMS !== null) {
                    const ms = (self.widgets || []).find((w) => w.name === "manager_settings");
                    if (ms) { ms._state.value = rawMS; ms.value = rawMS; }
                }
                if (rawIP !== undefined && rawIP !== null) {
                    const ip = (self.widgets || []).find((w) => w.name === "internal_prompt");
                    if (ip) {
                        ip._state.value = rawIP;
                        ip.value = rawIP;
                    }
                    const box = self.__promptBox;
                    if (box && rawIP && !getPromptText(box)) {
                        renderPromptFromText(box, String(rawIP), self);
                        if (ipWidget) setWidgetValue(ipWidget, String(rawIP));
                    }
                }
                // configure 可能重建端口 → 兜底清理幽灵接口（内部存储字段无端口）
                setTimeout(killGhost, 0);
                // 工作流加载后按真实 DOM 重算尺寸 + 锁定最小宽度（防版本差异导致的漂移）
                setTimeout(() => { try { lockMinWidth(); refreshSize?.(); } catch (_) {} }, 120);
                return r;
            };

            // 清理内部存储字段的幽灵接口（socketless 内部字段不应渲染端口；旧工作流残留连线也会渲染幽灵口）。
            // 覆盖：internal_prompt / prompt_input(旧) / display_info / manager_settings，widget 值保留供 execute 取值。
            // 多次重试覆盖工作流加载时序（链接在节点 configure 之后才连上）。
            const killGhost = () => {
                try {
                    for (const nm of ["internal_prompt", "prompt_input", "display_info", "manager_settings"]) {
                        const inp = (self.inputs || []).find((i) => i.name === nm);
                        if (!inp) continue;
                        // 1. 断开残留连线（旧工作流曾把「字符串A」接到 internal_prompt）
                        if (inp.link != null) {
                            const linkId = inp.link;
                            inp.link = null;
                            const g = self.graph;
                            if (g && g.links && g.links[linkId]) {
                                const link = g.links[linkId];
                                const origin = g.getNodeById(link.origin_id);
                                if (origin && origin.outputs && origin.outputs[link.origin_slot] && Array.isArray(origin.outputs[link.origin_slot].links)) {
                                    const ol = origin.outputs[link.origin_slot].links;
                                    const idx = ol.indexOf(linkId);
                                    if (idx >= 0) ol.splice(idx, 1);
                                }
                                delete g.links[linkId];
                            }
                            console.log(`[JZL-管理器] 已清理 ${nm} 幽灵连线`);
                        }
                        // 2. 彻底移除端口（内部存储字段，无端口；widget 值保留供 execute 取值）
                        const idx = self.inputs.indexOf(inp);
                        if (idx >= 0) {
                            self.inputs.splice(idx, 1);
                            (self.inputs || []).forEach((i, k) => { i.slot = k; });
                        }
                    }
                    self.setDirtyCanvas?.(true, true);
                } catch (_) {}
            };
            self.__jzlKillGhost = killGhost;
            [0, 150, 500, 1200].forEach((d) => setTimeout(killGhost, d));

            // 资产显示窗（缩略图墙，点击插入 @引用；自动换行，多行时节点同步增高）
            // 高度上限 180px：超出后**本窗自己滚动**（避免素材一多就把整个节点顶高、也不给节点加滚动条）
            const windowTitle = el("div", "flex:0 0 auto;font-size:12px;color:#bbb;margin-top:2px;", "📁 资产显示窗（点击插入）");
            const windowBox = el("div", "min-height:30px;max-height:180px;overflow-y:auto;overflow-x:hidden;flex:0 0 auto;scrollbar-width:thin;");
            enableWheelScroll(windowBox);   // Nodes 2.0：滚轮归本窗
            const resizeNodeForContent = () => {
                requestAnimationFrame(() => {
                    try {
                        // scrollHeight 取完整内容高度（含多行），避免 overflow 把多行素材吞掉
                        const assetsH = windowBox.scrollHeight || windowBox.offsetHeight || 30;
                        const btnRows = Math.ceil((self.__jzlButtons || PANEL_BUTTONS).length / (self.__jzlBtnCols || 4));
                        const reshootH = (self.__reshootBody && self.__reshootBody.style.display !== "none") ? (self.__reshootBody.offsetHeight || 0) : 0;  // 重拍区展开时计入节点高度
                        const minDom = btnRows * (btnRowH + 4) + 10 + 16 + 72 + 22 + assetsH + reshootH + 20;  // 按钮N行 + gap + 信息行 + 提示词min + 资产窗标题 + 重拍区 + padding
                        const y = (self.widgets || []).find((w) => w.name === "jzl_manager")?.y;
                        const need = (Number.isFinite(y) ? y : 220) + minDom;
                        // ⚠️ Nodes 2.0：节点高度由 DOM 内容决定（多行素材会自动掉高），
                        //    而 2.0 里 widget.y 会被前端重算（实测可达 1300+），
                        //    用 y + minDom 反推节点高度会把节点凭空顶高（实测拖完松手被顶到 1573）
                        //    → 2.0 下直接不做这个 setSize。
                        if ((() => { try { return app?.ui?.settings?.getSettingValue?.("Comfy.VueNodes.Enabled") === true; } catch (_) { return false; } })()) return;
                        if (self.size && need > (self.size[1] || 0) + 1) {
                            self.setSize([self.size[0], need]);
                            self.setDirtyCanvas?.(true, true);
                            // 节点增高后 DOM 重排延迟 → 二次增高确保多行素材全部可见
                            setTimeout(() => {
                                self.setSize?.([self.size[0], need]);
                                self.setDirtyCanvas?.(true, true);
                            }, 80);
                        }
                    } catch (_) {}
                });
            };
            windowBox.__onResize = resizeNodeForContent;
            container.appendChild(windowTitle);
            container.appendChild(windowBox);

            // 🔄 重拍模式（Pro + Mini）：提示词来源 + 重拍视频编号 + 提示词显示窗（可编辑）
            // Mini 的重拍视频编号会同步到「重拍视频编号」参数（reshoot_segment）作为编码选段
            const reshootSegW = isMini ? (self.widgets || []).find((w) => w.name === "reshoot_segment") : null;
            const reshootSection = createReshootSection({
                self, promptBox, ipWidget, resizeNode: resizeNodeForContent,
                isMini: !!isMini, reshootSegWidget: reshootSegW, container,
            });
            container.appendChild(reshootSection.header);
            container.appendChild(reshootSection.body);
            self.__reshootBody = reshootSection.body;
            self.__reshootRefresh = reshootSection.refresh;

            // ── Nodes 2.0 面板布局适配（DOM 渲染）──────────────────────────────
            // 2.0 下节点高度由 DOM 内容决定：容器 style 里的 height:100% 解析不到确定高度，
            // 提示词框（flex:1）会跟着文本一路长高（实测 60 行 → 框 972px、节点 1647px），
            // 于是「没有滚动条、内容把节点撑爆」。这里给提示词区一个确定高度，
            // 文本框的 overflow-y:auto 才有边界；默认框高 280，用户拖大/拖小节点时框跟着变。
            const IS_NODES2_LAYOUT = (() => {
                try { return app?.ui?.settings?.getSettingValue?.("Comfy.VueNodes.Enabled") === true; } catch (_) { return false; }
            })();
            // 用户在设置里切换「Nodes 2.0」开关时**不会刷新页面**，所以判定不能只在初始化时做一次
            // （旧写法缓存成常量：切回经典模式后 px 固定高度仍然生效 → 用户报「高度被锁死」）。
            const isNodes2Now = () => {
                try { return app?.ui?.settings?.getSettingValue?.("Comfy.VueNodes.Enabled") === true; } catch (_) { return false; }
            };
            // 上限实际取消：原来是 900，用户把节点拉得比 900 还高时输入框就不再跟着长（报障 ①）。
            // 下拉时的高度同时被指针位移约束，不存在跑飞风险，这里给一个名义上限即可。
            const PROMPT_H2_MIN = 160, PROMPT_H2_MAX = 100000, PROMPT_H2_DEFAULT = 280;
            const origH2 = { wrap: null, box: null };      // 首次改写前的原样（切回经典模式要原样还原）            // Nodes 2.0：面板已有界定高度（见 bindDomWidgetMaxHeight）→ 提示词框改成 **flex 填满剩余空间**。
            //   这样「🔄 重拍模式」与资产窗被顶到节点下边缘，节点拉高时只有输入框变大，不再是下方留白。
            //   （旧写法给固定 px，节点拉高后多出来的空间全留在面板底部 → 用户报「下面空白变大」。）
            const applyPromptH2 = (h) => {
                if (!origH2.wrap) {
                    origH2.wrap = { height: promptWrap.style.height, minHeight: promptWrap.style.minHeight, flex: promptWrap.style.flex };
                    origH2.box = { height: promptBox.style.height, maxHeight: promptBox.style.maxHeight, minHeight: promptBox.style.minHeight, flex: promptBox.style.flex };
                }
                promptWrap.style.flex = "1 1 auto";
                promptWrap.style.height = "";
                // 下限压到 0：面板高度已确定，提示词框是唯一可压缩的项 ⇒ 节点再矮也不会
                // 让资产窗/重拍条被挤出去而在面板右侧冒出滚动条（节点极矮时框会很扁，拉高即恢复）
                promptWrap.style.minHeight = "0px";
                promptBox.style.flex = "1 1 auto";
                promptBox.style.height = "";
                promptBox.style.maxHeight = "";
                promptBox.style.minHeight = "0px";
            };
            // 切回经典（canvas）模式：把行内样式还原成面板原本的值，交回原有的 flex 满高布局
            const restorePromptH2 = () => {
                try {
                    if (!origH2.wrap) return;
                    promptWrap.style.height = origH2.wrap.height;
                    promptWrap.style.minHeight = origH2.wrap.minHeight;
                    promptWrap.style.flex = origH2.wrap.flex;
                    promptBox.style.height = origH2.box.height;
                    promptBox.style.maxHeight = origH2.box.maxHeight;
                    promptBox.style.minHeight = origH2.box.minHeight;
                    promptBox.style.flex = origH2.box.flex;
                } catch (_) {}
            };
            // 滚动条鼠标滚轮：2.0 下节点就在画布 DOM 里，滚轮会被画布的缩放处理抢走
            //（实测：在提示词框上滚轮 → 框 scrollTop 不变、画布 scale 0.382→0.347）。
            // 这里在面板层拦下「确实能滚」的滚轮事件；已经到顶/到底时放行，保留画布缩放手感。
            const wheelCanScroll2 = (t, dy) => {
                let el = t;
                while (el && el !== container.parentElement) {
                    let cs;
                    try { cs = getComputedStyle(el); } catch (_) { break; }
                    const oy = cs.overflowY;
                    if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 1) {
                        if (dy > 0 && el.scrollTop + el.clientHeight < el.scrollHeight - 1) return true;
                        if (dy < 0 && el.scrollTop > 0) return true;
                        return false;
                    }
                    el = el.parentElement;
                }
                return false;
            };
            try {
                container.addEventListener("wheel", (e) => {
                    if (wheelCanScroll2(e.target, e.deltaY)) e.stopPropagation();
                }, true);
            } catch (_) {}
            // ⚠️ 高度口径踩过的坑（都实测过）：
            //   ① 「节点高度 − 面板顶部偏移 − 面板其余内容」→ 容器**下方**还有一片 DOM
            //      （原生 widget 行 + 高级输入按钮，实测约 233px）没被计入，公式系统性偏大 → 顶到上限 900。
            //   ② 「节点高度增量」跟随 → 2.0 下写框高会把节点撑高，增量被当成用户拖拽 → 自激到 900。
            //   ③ 「container.clientHeight − scrollHeight」→ 内容比容器矮时 scrollHeight 被钳到 clientHeight，
            //      差值恒为 0 → 拖大节点输入框纹丝不动。
            //   ④ 引导阶段（节点刚创建的那几十毫秒）节点高度会临时远大于内容（实测 --node-height 还是 510、
            //      容器 822、内容只有 311）→ 只要在引导期做「填满空白」，默认值就被顶到上限 900。
            // 现方案：默认固定 280；用户拖尺寸手柄时**用指针位移实时改框高**
            // （不能等拖完再按节点高度算：2.0 下节点高度被内容撑住、不允许小于内容，
            //  不主动缩内容的话用户往回拖根本拖不动 —— 实测节点卡在内容高度）。
            // 拖完之后再用「容器剩余空白」把可能的零头补齐（该口径写一次即收敛）。
            let sized2 = false, userSized2 = false, dragState2 = null;
            let relayoutForNodes2 = () => {};
            let hookHandle2Ref = null;
            const onDragMove2 = (e) => {
                if (!dragState2) return;
                try {
                    const scale = Number(app?.canvas?.ds?.scale) || 1;
                    const d = (e.clientY - dragState2.y) / scale * dragState2.sign;
                    const boxH = promptWrap.offsetHeight || PROMPT_H2_DEFAULT;
                    const h = Math.max(PROMPT_H2_MIN, Math.min(PROMPT_H2_MAX, Math.round(dragState2.box + d)));
                    if (Math.abs(boxH - h) > 1) applyPromptH2(h);
                } catch (_) {}
            };
            const onDragEnd2 = () => {
                dragState2 = null;
                userSized2 = true;
                try {
                    window.removeEventListener("pointermove", onDragMove2, true);
                    window.removeEventListener("pointerup", onDragEnd2, true);
                    window.removeEventListener("pointercancel", onDragEnd2, true);
                } catch (_) {}
                setTimeout(() => { try { relayoutForNodes2(); } catch (_) {} }, 80);
            };
            try {
                hookHandle2Ref = () => {
                    const nodeEl2 = container.closest ? container.closest("[data-node-id]") : null;
                    if (!nodeEl2 || nodeEl2.__jzlHandleHook2) return;
                    nodeEl2.__jzlHandleHook2 = true;
                    nodeEl2.addEventListener("pointerdown", (e) => {
                        const hd = (e.target && e.target.closest) ? e.target.closest('[aria-label*="调整大小"]') : null;
                        if (!hd) return;
                        const corner = (hd.getAttribute("data-corner") || "").toUpperCase();
                        if (corner.indexOf("N") < 0 && corner.indexOf("S") < 0) return;   // 只处理能改高度的角
                        dragState2 = {
                            y: e.clientY,
                            box: promptWrap.offsetHeight || PROMPT_H2_DEFAULT,
                            sign: corner.indexOf("N") >= 0 ? -1 : 1,      // 拖上边时指针向上 = 节点变高
                        };
                        window.addEventListener("pointermove", onDragMove2, true);
                        window.addEventListener("pointerup", onDragEnd2, true);
                        window.addEventListener("pointercancel", onDragEnd2, true);
                    }, true);
                };
                hookHandle2Ref();
                [0, 150, 500, 1200].forEach((d) => setTimeout(hookHandle2Ref, d));
            } catch (_) {}
            relayoutForNodes2 = () => {
                try {
                    if (!isNodes2Now()) {                // 切回经典模式：撤销 px 固定高度，恢复原有 flex 布局
                        if (sized2) { sized2 = false; restorePromptH2(); }
                        return;
                    }
                    if (!container.isConnected) return;
                    if (!sized2) {                       // 首次：固定默认高度（280）
                        sized2 = true;
                        applyPromptH2(PROMPT_H2_DEFAULT);
                        return;
                    }
                    if (dragState2) return;              // 拖拽进行中由指针位移接管
                    // 拖完之后不再做「填满剩余空白」：2.0 下节点高度会被前端/其它布局代码临时抬高
                    // （实测松手 200ms 后节点被顶到 1573），一旦去填就会被顶到上限，
                    // 而且再也缩不回来（节点不允许小于内容）→ 用户会遇到「拖回去也不变小」。
                    // 高度完全由用户拖拽（指针位移）决定，最稳。
                } catch (_) {}
            };
            {
                // 2.0 开关是运行时可切的（不需要刷新页面）：轮询确认模式，
                // 切到 2.0 → 挂手柄监听 + 应用 px 高度；切回经典 → 撤掉 px 高度恢复 flex 布局
                let mode2 = IS_NODES2_LAYOUT;
                const modeTimer2 = setInterval(() => {
                    try {
                        const now = isNodes2Now();
                        if (now === mode2) {
                            if (now && typeof hookHandle2Ref === "function") hookHandle2Ref();
                            return;
                        }
                        mode2 = now;
                        if (now) { sized2 = false; try { hookHandle2Ref?.(); } catch (_) {} relayoutForNodes2(); }
                        else { sized2 = false; restorePromptH2(); }
                    } catch (_) {}
                }, 1000);
                const __onRemoved2 = self.onRemoved;
                self.onRemoved = function () {
                    try { clearInterval(modeTimer2); } catch (_) {}
                    return __onRemoved2?.apply(this, arguments);
                };
            }
            self.__jzlRelayout2 = relayoutForNodes2;
            {
                // 两种模式都装调度：经典模式下 relayoutForNodes2 会直接返回（零开销）
                // Nodes 2.0：面板高度 = 节点高度（见 bindDomWidgetMaxHeight），默认高度要够放
                // 「按钮区 + 提示词 + 资产窗 + 重拍模式」，否则一见面就是面板滚动条。
                // 实测不滚所需高度：短剧导演台 620；Pro/Max/无限时长 831（原生输入行更多）；Mini 891。
                if (IS_NODES2_LAYOUT) {
                    const _dfltH = isShortDrama ? 620 : (isMini ? 900 : 860);
                    self.setSize?.([self.size?.[0] || 600, Math.max(Number(self.size?.[1]) || 0, _dfltH)]);
                }
                [0, 50, 150, 400, 900, 1600].forEach((d) => setTimeout(relayoutForNodes2, d));
                // ⚠️ 重排绝不能在 ResizeObserver 回调里同步执行："写高度 → 布局变 → observer 又触发"
                // 会刷出 "ResizeObserver loop completed with undelivered notifications"（实测节点拖拽时 6 条）。
                // 丢到下一帧 + 同帧去重后警告完全消失，收敛行为不变。
                let raf2 = 0;
                const deferRelayout2 = () => {
                    if (raf2) return;
                    raf2 = requestAnimationFrame(() => { raf2 = 0; relayoutForNodes2(); });
                };
                self.__jzlRelayoutDefer2 = deferRelayout2;
                try { new ResizeObserver(deferRelayout2).observe(container); } catch (_) {}
                const __origOnResize2 = self.onResize;
                self.onResize = function () {
                    const r = __origOnResize2?.apply(this, arguments);
                    deferRelayout2();
                    return r;
                };
            }

            // 缓存资产名并渲染资产显示窗；内部提示词重新渲染成着色 token 并同步到 internal_prompt
            const refreshAssets = () => {
                renderAssetWindow(windowBox, self.__promptBox, self);
                // 用 internal_prompt 持久值作为来源重渲染（配置恢复晚于 loadManager 时也能正确着色，修复刷新后无缩略图）
                const ipVal = ipWidget ? readWidgetValue(ipWidget) : "";
                // 仅在「显示引用」模式才重新着色对齐；「纯文本」模式保持纯文本——素材变化不自动切换模式
                if (self.__jzlAlignMode !== "text") {
                    renderPromptFromText(self.__promptBox, ipVal || getPromptText(self.__promptBox), self);
                    if (ipWidget) setWidgetValue(ipWidget, getPromptText(self.__promptBox));
                }
                updateAlignStatus();
            };

            // 生成详情只读显示：监听原生参数 widget（画幅/MP/时长/段数）变化后联动刷新
            const updateDisplay = () => {
                const gv = (nm) => {
                    const w = (self.widgets || []).find((x) => x.name === nm);
                    return w ? readWidgetValue(w) : null;
                };
                const ar = gv("aspect_ratio") || "16:9 (Widescreen)";
                const mp = parseFloat(gv("megapixels")) || 1.0;
                // duration 兼容：「固定N秒」/「A~B秒」新选项，以及旧值（"5" / "5-5" / "4-10"）
                const durRaw = ((gv("duration") ?? "固定5秒") + "").trim();
                const durFix = durRaw.match(/固定\s*(\d+(?:\.\d+)?)\s*秒/);
                const durM = durRaw.match(/(\d+(?:\.\d+)?)\s*[~\-—–]\s*(\d+(?:\.\d+)?)/);
                const DUR_MAX = 20;   // 每段时长上限（秒）：选项已扩展到 固定20秒 / 15~20秒 / 4~20秒
                let dlo, dhi;
                if (durFix) {
                    const dv = Math.max(4, Math.min(DUR_MAX, parseFloat(durFix[1]) || 8));
                    dlo = dv; dhi = dv;
                } else if (durM) {
                    dlo = Math.max(4, Math.min(DUR_MAX, parseFloat(durM[1]) || 8));
                    dhi = Math.max(dlo, Math.min(DUR_MAX, parseFloat(durM[2]) || dlo));
                } else {
                    const dv = Math.max(4, Math.min(DUR_MAX, parseFloat(durRaw) || 8));
                    dlo = dv; dhi = dv;
                }
                const vcRaw = String(gv("video_count") ?? "").trim();
                // 「剧情决定」：字符串「剧情决定」或数值 0（短剧导演台的 0 = 剧情决定）
                let vcAuto = /剧情决定|自动/.test(vcRaw) || (/^\d+(\.\d+)?$/.test(vcRaw) && Number(vcRaw) === 0);
                let count = (vcRaw.match(/\d+/) || [0])[0] * 1 || 6;
                // 🪄 穿透生成：段数按提示词里实际的 [SHOT_START] 段数算（后端同规则，忽略「生成视频数量」）
                if (/穿透生成/.test(String(gv("run_mode") ?? ""))) {
                    const ptTxt = String(gv("internal_prompt") ?? "") || (self.__promptBox ? getPromptText(self.__promptBox) : "");
                    const ptN = Math.min(60, countShotBlocks(ptTxt));
                    if (ptN) { vcAuto = false; count = ptN; }
                }
                // 动态参数用范围简写（如 1~60）
                const cntLo = vcAuto ? 1 : count, cntHi = vcAuto ? 60 : count;
                const cntTxt = vcAuto ? `${cntLo}~${cntHi}` : `${count}`;
                const [W, H] = jzlCalcResolution(ar, mp);
                const calcF = (d) => { const b = Math.max(5, Math.round(d * 24)); return b + (5 - (b % 17)) % 17; };
                const flo = calcF(dlo), fhi = calcF(dhi);
                const ds = dlo === dhi ? `${dlo}` : `${dlo}~${dhi}`;
                const fs = flo === fhi ? `${flo}` : `${flo}~${fhi}`;
                const tfs = (flo * cntLo === fhi * cntHi) ? `${flo * cntLo}` : `${flo * cntLo}~${fhi * cntHi}`;
                const ts = (dlo * cntLo === dhi * cntHi) ? `${dlo * cntLo}` : `${dlo * cntLo}~${dhi * cntHi}`;
                const disp = (self.widgets || []).find((x) => x.name === "display_info");
                if (!disp) return;
                let out = `分辨率：${W}x${H}丨每段时长：${ds}s丨每段帧数：${fs}丨共计段数：${cntTxt}丨总帧数：${tfs}丨总时长：${ts}秒`;
                // ♾️ 无限时长：段 2+ 的**生成时长 = 设定时长 + 段首衔接跑道**（跑道 = 窗口 + 过渡，成片裁掉）
                //    ⇒ 落盘时长 = 设定时长（节点的每个数值都按「落盘」显示，不再扣减）。
                if (self.type === INF_NODE_TYPE && (vcAuto || count > 1)) {
                    try {
                        let win = 22, settle = 12, tm = "";
                        const ms = gv("manager_settings");
                        if (ms) {
                            const cfg = JSON.parse(ms);
                            const inf = (cfg && cfg.sample_decode && cfg.sample_decode.infinite) || {};
                            win = parseInt(inf.window, 10) || 22;
                            settle = parseInt(inf.settle, 10) || 0;
                            tm = String(inf.trim_mode || "");
                        }
                        const noTrim = tm.indexOf("不裁") >= 0;
                        const lat = tm.indexOf("latent") >= 0;
                        // 运行时会把「窗口 + 过渡」对齐到 17 的倍数（保证落盘时长精确 = 设定时长）→ 这里按同规则显示
                        const snapSettle = (w, s) => {
                            let d = w + Math.max(0, s);
                            let sn = Math.round(d / 17) * 17;
                            if (sn < w) sn = Math.ceil(w / 17) * 17;
                            return Math.max(0, sn - w);
                        };
                        const settleEff = snapSettle(win, settle);
                        const per = noTrim ? 0 : (win + settleEff);
                        const latOK = lat && per > 0 && (per % 17 === 0);
                        const runwaySec = (per / 24).toFixed(2);
                        // 成片总长 = 设定时长 × 段数（跑道已裁掉）
                        const t1 = flo * cntLo, t2 = fhi * cntHi;
                        const s1 = (t1 / 24).toFixed(2), s2 = (t2 / 24).toFixed(2);
                        const tt = t1 === t2 ? `${t1} 帧 ≈ ${s1} 秒` : `${t1}~${t2} 帧 ≈ ${s1}~${s2} 秒`;
                        const cutDesc = noTrim
                            ? "不裁切（诊断）"
                            : (lat
                                ? (latOK ? "latent域裁切" : "latent域裁切（条件不足→回退像素域）")
                                : "像素域裁切") + `：窗口 ${win} + 过渡 ${settleEff}${settleEff !== settle ? `（配置 ${settle} 已对齐）` : ""}`;
                        const genNote = per > 0 ? `丨后续时长 = 设定 + ${runwaySec}s` : "";
                        out = `分辨率：${W}x${H}丨每段帧数：${fs}（落盘）丨段数：${cntTxt}丨${cutDesc}${genNote}丨成片总长：${tt}`;
                    } catch (_) { /* 解析失败则退回默认文案 */ }
                }
                // 🎬 短剧导演台：精简格式（分辨率 / 每段帧数 / 段数 / 成片总长）
                if (self.type === SHORTDRAMA_NODE_TYPE) {
                    const t1 = flo * cntLo, t2 = fhi * cntHi;
                    const s1 = (t1 / 24).toFixed(2), s2 = (t2 / 24).toFixed(2);
                    const tt = (flo === fhi && cntLo === cntHi)
                        ? `${t1} 帧 ≈ ${s1} 秒` : `${t1}~${t2} 帧 ≈ ${s1}~${s2} 秒`;
                    out = `分辨率：${W}x${H}丨每段帧数：${fs}丨段数：${cntTxt}丨成片总长：${tt}`;
                }
                setWidgetValue(disp, out);
                // DOM 里的「生成详情」紧跟状态行上方（节点表面 display_info 已隐藏）
                if (self.__jzlDetailRow) self.__jzlDetailRow.textContent = out;
            };
            self.__jzlUpdateDisplay = updateDisplay;   // 设置面板保存后由 saveManager 触发刷新
            const watchChange = (nm) => {
                const w = (self.widgets || []).find((x) => x.name === nm);
                if (!w) return;
                const orig = w.callback;
                w.callback = function (...args) {
                    const r = orig ? orig.apply(this, args) : undefined;
                    setTimeout(() => { updateDisplay(); updateInfo(); }, 0);   // 生成详情 + 生成设定行都跟着刷新
                    return r;
                };
            };
            // 生成设定三项（短剧导演台的 run_mode/story_style/story_name 显示在节点只读行上）
            ["aspect_ratio", "megapixels", "duration", "video_count",
             "run_mode", "story_style", "story_name"].forEach(watchChange);

            loadManager(self).then((data) => {
                // ⭐ 段数 widget 自愈：旧工作流可能把下拉用的「生成N段」存进了短剧导演台的数值输入
                // → ComfyUI 校验会拒绝整个 prompt。加载时静默归一化为该 widget 的合法形态。
                try { healVideoCountWidget(self); } catch (_) {}
                cacheAssets(self, data.settings);
                // 恢复「参考元素切换」保存的状态（显示引用/纯文本），刷新不丢失
                self.__jzlAlignMode = (data?.settings && data.settings.align_mode === "text") ? "text" : "ref";
                updateDisplay();
                refreshAssets();
                updateInfo();
                // 恢复重拍模式状态（拆解提示词/选中段/编辑内容/展开锁定），刷新不丢失
                self.__reshootRefresh?.(data.settings);
            }).catch(() => {});
            self.__jzlRefresh = refreshAssets;  // 弹窗保存资产后实时刷新本节点
            // 提交前自愈钩子（由 graphToPrompt 包装调用）：段数 widget 形态归一化
            self.healVideoCount = () => { try { return healVideoCountWidget(self); } catch (_) { return false; } };

            // 生成后控制回写：后端返回新 seed → 更新本节点 manager_settings
            const prevOnExecuted = self.onExecuted;
            self.onExecuted = function (message) {
                const r = prevOnExecuted?.apply(this, arguments);
                try {
                    const msW = (self.widgets || []).find((x) => x.name === "manager_settings");
                    // ComfyUI V3 会把 execute 返回的 ui 每个值打包成数组（见 execution.py
                    // get_output_from_returns: ui = {k: [...]}），必须解包取 [0]。
                    const unp = (v) => (Array.isArray(v) ? v[0] : v);
                    // 后端返回的业务错误（如未填故事名称）→ 弹错误提示
                    const merr = unp(message?.manager_error);
                    if (merr) { try { notify(`❌ ${merr}`, "error"); } catch (_) {} }
                    const _seedTxt = (v) => (v === undefined || v === null || v === "") ? "" : String(v);
                    const su = unp(message?.seed_update) || {};
                    const suSeed = _seedTxt(su.seed);
                    if (suSeed && msW) {
                        const raw = readWidgetValue(msW);
                        let s = {};
                        try { s = raw ? JSON.parse(raw) : {}; } catch (_) {}
                        if (!s.enhance) s.enhance = {};
                        s.enhance.seed = suSeed;   // 字符串保存：种子可超 2^53，数字会丢精度
                        s.enhance.seed_control = su.seed_control || s.enhance.seed_control || "randomize";
                        setWidgetValue(msW, JSON.stringify(s));
                        updateInfo();
                    }
                    // 采样种子回写（randomize 一采实际种子）→ 面板刷新后显示真实种子
                    const sus = unp(message?.seed_update_sample) || {};
                    const susSeed = _seedTxt(sus.seed);
                    if (susSeed && msW) {
                        const raw = readWidgetValue(msW);
                        let s = {};
                        try { s = raw ? JSON.parse(raw) : {}; } catch (_) {}
                        if (!s.sample_decode) s.sample_decode = {};
                        s.sample_decode.seed = susSeed;
                        if (sus.seed_control) s.sample_decode.seed_mode = sus.seed_control;
                        setWidgetValue(msW, JSON.stringify(s));
                        updateInfo();
                    }
                } catch (_) {}
                return r;
            };

            // 节点最小宽度 600：min_width 在新版前端可能不生效（只在拖动时 clamp，初始化/加载不撑宽），
            // 这里主动兜底锁定：创建 / 工作流加载 / 缩放 / 拖拽后都检查，宽度 < 600 一律拉回 600
            self.min_width = 600;
            self.minWidth = 600;
            const lockMinWidth = () => {
                try {
                    const w = Number.isFinite(self.size?.[0]) ? self.size[0] : 0;
                    if (w >= 600) return;
                    const h = Number.isFinite(self.size?.[1]) ? self.size[1] : 300;
                    self.setSize?.([600, Math.max(h, 300)]);
                } catch (_) {}
            };
            lockMinWidth();

            // 3. addDOMWidget：不 unshift；固定高度（按钮区 + 提示词 + 资产窗）
            const widget = self.addDOMWidget?.("jzl_manager", "JZL_MANAGER", container, {
                serialize: false,
                hideOnZoom: false,
            });
            if (widget) {
                try { delete widget.computeSize; } catch { widget.computeSize = undefined; }
                // DOM widget 填满节点高度（Goohai 同款）：节点拉大 → 输入框自动跟随
                widget.options = widget.options || {};
                widget.options.serialize = false;
                widget.options.getMinHeight = () => 300;
                widget.options.getHeight = () => "100%";
                // 对齐 Director：computeLayoutSize 返回 minWidth（参与节点最小宽度计算，
                // 属性面板等重排不会把节点/overlay 算窄）
                widget.computeLayoutSize = () => ({ minHeight: 300, maxHeight: undefined, minWidth: 600 });
                // Director 同款：DOM widget 自身每次被绘制时同步宽度（最精准时机，绘制前宽度已对，杜绝闪压）；
                // afterResize 在节点尺寸变化后同步
                widget.options.onDraw = () => {
                    try { syncDomWidth(); } catch (_) {}
                    try { self.__jzlUpdateExt?.(); } catch (_) {}
                };
                widget.options.afterResize = () => { try { syncDomWidth(); } catch (_) {} };
            }

            // —— 关键防挤压（对齐 MiniMaxH3Director 的 ensureDirectorDomWidgetWidth + patchDirectorDomWidgetLayout）：
            //    ComfyUI 的 DomWidgets 用 widget.width ?? node.width 计算 overlay 宽度，属性面板等重排时
            //    若只依赖 node.width 会被算错压缩。做法：主动把 widget.width 钉为节点完整宽度，并在
            //    canvas.onDrawForeground（每帧绘制）里持续同步——任何重排触发重绘后，下一帧即纠正，
            //    打开属性面板也不会再挤压 UI。——
            const syncDomWidth = () => {
                try {
                    const fullW = Number.isFinite(self.size?.[0]) ? self.size[0] : 0;
                    if (widget && fullW && widget.width !== fullW) widget.width = fullW;
                } catch (_) {}
                // Nodes 2.0：节点尺寸变化后同步「面板可用高度」上限（见 bindDomWidgetMaxHeight）
                try { self._jzlApplyMaxH?.(); } catch (_) {}
            };
            syncDomWidth();
            // Nodes 2.0：面板高度界定到「节点高度 − chrome」，这样提示词框才能 flex 填满剩余空间、
            // 「🔄 重拍模式」始终吸附节点下边缘（否则 2.0 行高 auto ⇒ 面板被内容撑开、按钮飘在中间）。
            // 经典模式该绑定自动空转（closest('.lg-node') 为空）。
            const applyPanelMaxH = bindDomWidgetMaxHeight(self, container, { minHeight: 260, definite: true });
            self._jzlApplyMaxH = applyPanelMaxH;
            self._jzlSyncWidth = syncDomWidth;
            self._jzlDomWidget = widget;

            // 节点 resize 时触发重绘，让 DOM widget（输入框）跟随高度
            let _resizeTimer = null;
            const prevOnResize = self.onResize;
            self.onResize = function (...args) {
                const rr = prevOnResize?.apply(this, args);
                try {
                    syncDomWidth();  // 节点尺寸一变立即把 overlay 宽度钉为节点宽度（防挤压）
                    self.setDirtyCanvas?.(true, true);
                    // 节点宽度调整期间会频繁触发 → 防抖：调整结束后 200ms，按「最终宽度」重新渲染资产窗
                    // 并重算节点高度，保证换行后的多行素材全部显示、节点自动增高（修复调窄后第三行被吞）
                    clearTimeout(_resizeTimer);
                    _resizeTimer = setTimeout(() => {
                        lockMinWidth();  // 拖拽后锁定最小宽度 600（拖不窄）
                        renderAssetWindow(windowBox, self.__promptBox, self);  // 内部会调 windowBox.__onResize 重算高度
                        self.__updateAlignStatus?.();
                        self.setDirtyCanvas?.(true, true);
                    }, 200);
                } catch (_) {}
                return rr;
            };

            // 4. 刷新节点尺寸（隐藏 widget 后需重算），并多次调用应对异步布局
            const refreshSize = () => {
                try {
                    lockMinWidth();  // 初始化尺寸重算时同时锁定最小宽度 600
                    syncDomWidth();  // 同步 overlay 宽度 = 节点宽度（防属性面板挤压）
                    const size = self.computeSize?.();
                    if (Array.isArray(size) && size.length >= 2 && Number.isFinite(size[1])) {
                        // 只在节点高度未设置/过小时初始化，避免覆盖用户拖拽或工作流保存的高度；
                        // 最小高度保证 DOM 提示词框与资产窗不被截断（修复「提示词接口不显示」）
                        if (!self.size || !Number.isFinite(self.size[1]) || self.size[1] < 420) {
                            self.setSize?.([self.size?.[0] || size[0], Math.max(size[1], 520)]);
                        }
                    }
                } catch { /* ignore */ }
                self.setDirtyCanvas?.(true, true);
            };
            refreshSize();
            requestAnimationFrame(refreshSize);
            setTimeout(refreshSize, 50);

            // 每帧绘制前景时同步本节点 overlay 宽度（对齐 Director 的 patchDirectorDomWidgetLayout）：
            // 属性面板等任何画布重排触发重绘后，下一帧即把 widget.width 纠正为节点宽度，杜绝挤压
            try {
                const _cv = self.graph?.canvas ?? window.app?.canvas;
                if (_cv && !_cv.__jzlDomWidthPatch) {
                    _cv.__jzlDomWidthPatch = true;
                    const _prevDraw = _cv.onDrawForeground;
                    _cv.onDrawForeground = function (ctx) {
                        const _r = _prevDraw?.apply(this, arguments);
                        try {
                            const _g = self.graph ?? (window.app && window.app.graph);
                            const _ns = _g ? (_g._nodes || _g.nodes || []) : [];
                            for (const _n of _ns) {
                                if (_n && _n._jzlDomWidget && _n._jzlSyncWidth) _n._jzlSyncWidth();
                            }
                        } catch (_) {}
                        return _r;
                    };
                }
            } catch (_) {}
            return r;
        };

        // 工作流加载后恢复内部提示词窗口内容，并检查外部提示词端口连接状态
        const origConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            const r = origConfigure?.apply(this, arguments);
            const self = this;
            const box = self.__promptBox;
            if (!box) return r;
            const ip = (self.widgets || []).find((w) => w.name === "internal_prompt");
            const v = ip?.value ?? "";
            if (v && !getPromptText(box)) {
                box.innerText = v;
                renderPromptFromText(box, getPromptText(box), self);
            }
            // 幽灵接口兜底：configure 后清理内部存储字段端口
            try { self.__jzlKillGhost?.(); } catch (_) {}
            return r;
        };
    },
});

// ── 🎬 生成视频查看器（独立节点 DOM）────────────────────────
// 画布内 2 列多宫格预览 output/jzl/{故事名} 视频（点击预览播放）+ 顶部
// 「📋 复制剧本 / 📖 编辑剧本（放大编辑）」。
// 0 个物理输入端口：自动识别 = 跟随同图「短剧导演台Max」的「故事名称」→ 同名文件夹；
// 剧本 = 直接读盘 output/jzl/{故事名}/第N次生成/故事拆解/ 最后一次生成文件（后端 /jzl/story_latest_script）。
// ── 「输出目录」内置文件浏览器（跨环境：浏览/进入/下载/删除/在线编辑文本）──
// 后端接口 /jzl/fs_list|fs_download|fs_delete|fs_read|fs_write|fs_open（沙箱限制在 output/jzl）。
// ── 文件预览弹窗（图片/视频/音频/文本；点击文件名触发）──
function openFilePreview(rel, name, ext) {
    document.querySelectorAll(".jzl-fspreview").forEach((m) => m.remove());
    const _u = (inline) => {
        const q = `/jzl/fs_download?path=${encodeURIComponent(rel)}${inline ? "&inline=1" : ""}`;
        return (api && api.apiURL) ? api.apiURL(q) : q;
    };
    const mkBtn = (label, bg, fg) => el("button", `height:26px;padding:0 10px;border-radius:5px;border:1px solid #5b9bd5;background:${bg || "#3a3a3a"};color:${fg || "#eee"};font-size:12px;cursor:pointer;`, label);
    const overlay = el("div", "position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:100001;display:flex;align-items:center;justify-content:center;overflow:auto;");
    overlay.classList.add("jzl-fspreview");
    const box = el("div", "max-width:94vw;max-height:92vh;background:#1c1c1e;border:1px solid #555;border-radius:8px;display:flex;flex-direction:column;padding:10px;box-sizing:border-box;box-shadow:0 8px 40px rgba(0,0,0,.6);");
    const head = el("div", "display:flex;align-items:center;gap:8px;margin-bottom:8px;");
    head.appendChild(el("span", "flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;color:#eee;", name));
    const dl = mkBtn("⬇️ 下载", "#2a4a6a", "#cfe3f7");
    dl.addEventListener("click", () => window.open(_u(false), "_blank"));
    const close = mkBtn("关闭");
    close.addEventListener("click", () => overlay.remove());
    head.append(dl, close);
    box.appendChild(head);
    const body = el("div", "flex:1 1 auto;min-height:0;max-height:84vh;overflow:auto;display:flex;align-items:center;justify-content:center;background:#111;border-radius:6px;padding:6px;box-sizing:border-box;");
    box.appendChild(body);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });

    const imgExt = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "avif", "svg"];
    const vidExt = ["mp4", "webm", "mov", "mkv", "m4v"];
    const audExt = ["mp3", "wav", "m4a", "flac", "ogg", "aac"];
    const txtExt = ["txt", "json", "md", "csv", "srt", "log"];
    if (imgExt.includes(ext)) {
        const im = el("img", "max-width:90vw;max-height:82vh;object-fit:contain;");
        im.src = _u(true);
        im.onerror = () => { body.textContent = "❌ 图片加载失败"; };
        body.appendChild(im);
    } else if (vidExt.includes(ext)) {
        const v = document.createElement("video");
        v.controls = true;
        v.autoplay = true;
        v.style.cssText = "max-width:90vw;max-height:82vh;outline:none;background:#000;";
        v.src = _u(true);
        v.onerror = () => { body.textContent = "❌ 视频无法播放（浏览器可能不支持该编码，可下载后用播放器打开）"; };
        body.appendChild(v);
    } else if (audExt.includes(ext)) {
        const a = document.createElement("audio");
        a.controls = true;
        a.autoplay = true;
        a.style.cssText = "width:min(680px,86vw);";
        a.src = _u(true);
        body.appendChild(a);
    } else if (txtExt.includes(ext)) {
        body.style.alignItems = "flex-start";
        body.style.justifyContent = "flex-start";
        const pre = el("pre", "margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.6;color:#ddd;width:100%;");
        pre.textContent = "加载中…";
        body.appendChild(pre);
        api.fetchApi(`/jzl/fs_read?path=${encodeURIComponent(rel)}`)
            .then((r) => r.json())
            .then((d) => { pre.textContent = (d && d.ok) ? (d.text || "") : ("❌ " + ((d && d.error) || "读取失败")); })
            .catch((e) => { pre.textContent = "❌ 读取失败：" + ((e && e.message) || e); });
    } else {
        body.textContent = "该文件类型不支持在线预览，请点「⬇️ 下载」。";
    }
}

function openOutputBrowser(startRel) {
    document.querySelectorAll(".jzl-fsbrowser").forEach((m) => m.remove());
    const fmtSize = (n) => {
        n = Number(n) || 0;
        if (n < 1024) return n + " B";
        if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
        if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
        return (n / 1073741824).toFixed(2) + " GB";
    };
    const fmtTime = (t) => { try { const d = new Date((Number(t) || 0) * 1000); return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; } catch (_) { return ""; } };
    let cur = String(startRel || "").replace(/^\/+|\/+$/g, "");
    let parent = "";
    const picked = new Set();   // 勾选的文件（rel）→ 批量打包下载；切目录后保留
    const mkBtn = (label, bg, fg) => el("button", `flex:0 0 auto;height:26px;padding:0 10px;border-radius:5px;border:1px solid #5b9bd5;background:${bg || "#3a3a3a"};color:${fg || "#eee"};font-size:12px;cursor:pointer;`, label);
    const overlay = el("div", "position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:100000;display:flex;align-items:center;justify-content:center;overflow:auto;");
    overlay.classList.add("jzl-fsbrowser");
    const box = el("div", "width:880px;max-width:94vw;max-height:92vh;background:var(--comfy-menu-bg,#242424);border:1px solid #555;border-radius:8px;display:flex;flex-direction:column;padding:12px;box-sizing:border-box;box-shadow:0 8px 40px rgba(0,0,0,.5);");
    const head = el("div", "display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;");
    head.appendChild(el("span", "font-size:14px;font-weight:600;color:#eee;white-space:nowrap;", "📁 输出目录"));
    const pathBox = el("span", "flex:1 1 140px;min-width:0;font-size:12px;color:#9fd6a4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", "");
    const upBtn = mkBtn("⬆️ 上级");
    const refreshBtn2 = mkBtn("🔄 刷新");
    const sysBtn = mkBtn("🖥️ 资源管理器", "#2a4a6a", "#cfe3f7");
    sysBtn.title = "在服务器本机用系统文件管理器打开（仅桌面环境；云机/无桌面会提示，请直接用下方列表浏览）";
    const closeBtn = mkBtn("关闭");
    head.append(pathBox, upBtn, refreshBtn2, sysBtn, closeBtn);
    // 批量下载条（勾选框在同一排右侧）：勾选文件 → 打包成一个 zip 下载（云机不用装任何东西）
    const batchRow = el("div", "display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;padding:6px 8px;border:1px solid #3a4a5a;border-radius:5px;background:#1d2530;");
    const selAllCb = document.createElement("input");
    selAllCb.type = "checkbox";
    selAllCb.style.cssText = "flex:0 0 auto;width:14px;height:14px;cursor:pointer;accent-color:#5b9bd5;";
    selAllCb.title = "全选 / 取消全选（本目录所有文件）";
    const selLabel = el("span", "flex:0 0 auto;font-size:12px;color:#cfe3f7;cursor:pointer;", "全选本目录");
    const countLabel = el("span", "flex:1 1 auto;min-width:0;font-size:12px;color:#9fd6a4;", "已选 0 个文件");
    const clearBtn = mkBtn("✖️ 清空");
    const zipBtn = mkBtn("⬇️ 批量下载（打包 zip）", "#2f6b46", "#dff5e6");
    zipBtn.title = "把勾选的文件打包成一个 zip 下载（视频不重复压缩，只做拷贝）";
    batchRow.append(selAllCb, selLabel, countLabel, clearBtn, zipBtn);
    box.appendChild(head);
    box.appendChild(batchRow);
    const list = el("div", "flex:1 1 auto;overflow-y:auto;min-height:140px;border:1px solid #333;border-radius:5px;background:#1a1a1a;padding:6px;");
    box.appendChild(list);
    box.appendChild(el("div", "font-size:11px;color:#888;margin-top:6px;white-space:pre-wrap;", "勾选文件后可「批量下载」成一个 zip；文本文件（.txt/.json/.md/.csv/.srt/.log）可在线「编辑」并保存；点文件名可预览。云机无需系统文件管理器。"));
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    let visibleFiles = [];   // 当前目录的文件（供全选 / 计数）
    const rowChecks = new Map();  // rel → checkbox（全选/清空时直接改 DOM，不重新拉目录）
    const syncSel = () => {
        const n = picked.size;
        countLabel.textContent = `已选 ${n} 个文件`;
        zipBtn.style.opacity = n ? "1" : "0.55";
        zipBtn.disabled = n === 0;
        const allOn = visibleFiles.length > 0 && visibleFiles.every((f) => picked.has(f.rel));
        selAllCb.checked = allOn;
        selAllCb.indeterminate = !allOn && visibleFiles.some((f) => picked.has(f.rel));
        clearBtn.style.opacity = n ? "1" : "0.55";
        clearBtn.disabled = n === 0;
        rowChecks.forEach((cb, rel) => { cb.checked = picked.has(rel); });
    };
    selLabel.addEventListener("click", () => selAllCb.click());
    selAllCb.addEventListener("change", () => {
        if (selAllCb.checked) visibleFiles.forEach((f) => picked.add(f.rel));
        else visibleFiles.forEach((f) => picked.delete(f.rel));
        syncSel();
    });
    clearBtn.addEventListener("click", () => { picked.clear(); syncSel(); });
    zipBtn.addEventListener("click", () => {
        const paths = Array.from(picked);
        if (!paths.length) { notify("请先勾选要下载的文件", "warning"); return; }
        // 表单 POST 让浏览器自己把响应落盘（不占内存、支持几百 MB 的成片）
        const action = (api && api.apiURL) ? api.apiURL("/jzl/fs_zip") : "/jzl/fs_zip";
        const form = document.createElement("form");
        form.method = "POST";
        form.action = action;
        form.style.display = "none";
        const inp = document.createElement("input");
        inp.type = "hidden";
        inp.name = "paths";
        inp.value = JSON.stringify(paths);
        form.appendChild(inp);
        document.body.appendChild(form);
        form.submit();
        setTimeout(() => form.remove(), 2000);
        notify(`⬇️ 正在打包 ${paths.length} 个文件，完成后浏览器会自动下载…`, "success");
    });
    syncSel();   // 初始态：未选任何文件 → 批量按钮置灰

    const load = async (keepScroll) => {
        if (!keepScroll) list.textContent = "加载中…";
        let data = {};
        try {
            const r = await api.fetchApi(`/jzl/fs_list?path=${encodeURIComponent(cur)}`);
            data = await r.json().catch(() => ({}));
        } catch (e) { list.textContent = "❌ 加载失败：" + ((e && e.message) || e); return; }
        if (!data || !data.ok) { list.textContent = "❌ " + ((data && data.error) || "无法读取目录"); return; }
        cur = data.rel || "";
        parent = data.parent || "";
        pathBox.textContent = "output/jzl/" + (cur ? cur + "/" : "");
        list.innerHTML = "";
        visibleFiles = data.files || [];
        rowChecks.clear();
        for (const dd of data.dirs || []) {
            const row = el("div", "display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:4px;cursor:pointer;");
            row.addEventListener("mouseenter", () => { row.style.background = "#242424"; });
            row.addEventListener("mouseleave", () => { row.style.background = ""; });
            row.append(el("span", "flex:0 0 auto;font-size:13px;color:#ffd98a;", "📁"),
                el("span", "flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:#ddd;", dd.name),
                el("span", "flex:0 0 auto;font-size:11px;color:#666;", fmtTime(dd.mtime)));
            row.addEventListener("click", () => { cur = dd.rel; load(); });
            list.appendChild(row);
        }
        for (const ff of data.files || []) {
            const row = el("div", "display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:4px;");
            // 勾选框（在文件最前面）→ 批量打包下载
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.style.cssText = "flex:0 0 auto;width:14px;height:14px;cursor:pointer;accent-color:#5b9bd5;";
            cb.title = "勾选后可「⬇️ 批量下载（打包 zip）」";
            cb.checked = picked.has(ff.rel);
            cb.addEventListener("mousedown", (e) => e.stopPropagation());
            cb.addEventListener("change", () => {
                if (cb.checked) picked.add(ff.rel); else picked.delete(ff.rel);
                syncSel();
            });
            rowChecks.set(ff.rel, cb);
            const icon = ff.ext === "mp4" ? "🎬" : (["png", "jpg", "jpeg", "webp", "gif", "bmp"].includes(ff.ext) ? "🖼️" : (["txt", "json", "md", "csv", "srt", "log"].includes(ff.ext) ? "📄" : "📦"));
            const nameEl = el("span", "flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:#ddd;cursor:pointer;", ff.name);
            nameEl.title = "点击预览（图片/视频/音频/文本）";
            nameEl.addEventListener("click", () => openFilePreview(ff.rel, ff.name, ff.ext));
            row.append(cb,
                el("span", "flex:0 0 auto;font-size:13px;", icon),
                nameEl,
                el("span", "flex:0 0 auto;font-size:11px;color:#888;", fmtSize(ff.size)),
                el("span", "flex:0 0 auto;font-size:11px;color:#666;", fmtTime(ff.mtime)));
            const dl = mkBtn("⬇️ 下载", "#2a4a6a", "#cfe3f7");
            dl.addEventListener("click", () => { const u = `/jzl/fs_download?path=${encodeURIComponent(ff.rel)}`; window.open((api && api.apiURL) ? api.apiURL(u) : u, "_blank"); });
            row.appendChild(dl);
            if (["txt", "json", "md", "csv", "srt", "log"].includes(ff.ext)) {
                const ed = mkBtn("✏️ 编辑");
                ed.addEventListener("click", async () => {
                    try {
                        const rr = await api.fetchApi(`/jzl/fs_read?path=${encodeURIComponent(ff.rel)}`);
                        const rd = await rr.json().catch(() => ({}));
                        if (!rd || !rd.ok) { notify((rd && rd.error) || "读取失败", "error"); return; }
                        openTextZoomEditor("📄 " + ff.name, () => rd.text || "", async (text) => {
                            try {
                                const wr = await api.fetchApi("/jzl/fs_write", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: ff.rel, text: text || "" }) });
                                const wd = await wr.json().catch(() => ({}));
                                notify(wd && wd.ok ? "✅ 已保存" : ("❌ " + ((wd && wd.error) || "保存失败")), wd && wd.ok ? "success" : "error");
                            } catch (e2) { notify("保存失败：" + ((e2 && e2.message) || e2), "error"); }
                        }, null);
                    } catch (e) { notify("读取失败：" + ((e && e.message) || e), "error"); }
                });
                row.appendChild(ed);
            }
            const del = mkBtn("🗑 删除", "#7a2a2a", "#ffd0d0");
            del.addEventListener("click", async () => {
                if (!window.confirm(`确定删除「${ff.name}」？此操作不可恢复。`)) return;
                try {
                    const rr = await api.fetchApi("/jzl/fs_delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: ff.rel }) });
                    const rd = await rr.json().catch(() => ({}));
                    if (rd && rd.ok) { picked.delete(ff.rel); notify("🗑 已删除 " + ff.name, "success"); load(); }
                    else notify((rd && rd.error) || "删除失败", "error");
                } catch (e) { notify("删除失败：" + ((e && e.message) || e), "error"); }
            });
            row.appendChild(del);
            list.appendChild(row);
        }
        if (!(data.dirs || []).length && !(data.files || []).length) {
            list.appendChild(el("div", "font-size:12px;color:#888;padding:8px;", "（空目录）"));
        }
        syncSel();
    };

    upBtn.addEventListener("click", () => { cur = parent; load(); });
    refreshBtn2.addEventListener("click", () => load());
    sysBtn.addEventListener("click", async () => {
        try {
            const r = await api.fetchApi("/jzl/fs_open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: cur }) });
            const data = await r.json().catch(() => ({}));
            if (data && data.ok) notify("🖥️ 已在本机打开文件夹", "success");
            else notify((data && data.error) || "无法打开（云机请用列表浏览）", "error");
        } catch (e) { notify("无法打开：" + ((e && e.message) || e), "error"); }
    });
    closeBtn.addEventListener("click", () => overlay.remove());
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    load();
}

function setupVideoViewerNode(self) {
    // socketless story_name widget：隐藏 + 持久化「当前故事」（自动识别/手动选择时写入）
    const _storyW = (self.widgets || []).find((x) => x.name === "story_name");
    for (const w of self.widgets || []) {
        if (!w) continue;
        if ((w.name || "") === "story_name") {
            w.hidden = true;
            if (!w.options) w.options = {};
            w.options.hidden = true;
            w.computeSize = () => [0, -4];
        }
    }
    // Nodes 2.0：同上的刷新（仅改可见性数据不会立刻重渲染）
    try { if (Array.isArray(self.widgets)) self.widgets = self.widgets.slice(); } catch (_) {}
    try { self.graph?.trigger?.("node:slot-label:changed", { nodeId: self.id, slotType: 2 }); } catch (_) {}
    // 剧本（由磁盘最后一次生成文件填充：开启增强=已增强剧本，未开启=故事拆解；复制/编辑基于它）；当前故事初值 = 上次持久化的 story_name
    self.__viewerScript = "";
    self.__viewerSource = "";   // 剧本来源：已增强剧本 / 故事拆解
    self.__viewerStory = String(_storyW ? String(readWidgetValue(_storyW) || "").trim() : "");
    const _setStoryWidget = (sn) => {
        if (_storyW && String(readWidgetValue(_storyW) || "").trim() !== sn) {
            try { setWidgetValue(_storyW, sn); } catch (_) {}
        }
    };

    const container = document.createElement("div");
    container.style.cssText = "width:100%;height:100%;display:flex;flex-direction:column;gap:6px;padding:8px;box-sizing:border-box;overflow:hidden;";

    // 顶行（同一排）：故事下拉（自动识别=跟随 Max 故事名）+ 刷新 + 复制剧本 + 编辑剧本
    const ctrlRow = el("div", "display:flex;align-items:center;gap:6px;flex:0 0 auto;");
    const storySel = document.createElement("select");
    storySel.title = "故事文件夹：自动识别=跟随「短剧导演台Max」的故事名称同名文件夹；或手动选择 output/jzl 下已有故事";
    storySel.style.cssText = "flex:0 1 130px;min-width:0;background:#1d1d1d;color:#ddd;border:1px solid #444;border-radius:4px;padding:4px 3px;font-size:12px;";
    storySel.addEventListener("mousedown", (e) => e.stopPropagation());
    storySel.addEventListener("change", () => refresh());
    const AUTO_VAL = "__auto__";   // storySel 首项「✨ 自动识别」
    const refreshBtn = el("button", "flex:0 0 auto;height:26px;padding:0 8px;border-radius:5px;border:1px solid #5b9bd5;background:#2a4a6a;color:#cfe3f7;font-size:12px;cursor:pointer;", "🔄 刷新目录");
    refreshBtn.title = "重新同步「短剧导演台Max」的故事名并读取视频/剧本";
    refreshBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    refreshBtn.addEventListener("click", () => { refresh().then(() => { try { notify("🔄 已同步（视频 + 最后一次" + (self.__viewerSource || "剧本") + "）", "success"); } catch (_) {} }); });
    const copyBtn = el("button", "flex:0 0 auto;height:26px;padding:0 8px;border-radius:5px;border:1px solid #5b9bd5;background:#3a3a3a;color:#eee;font-size:12px;cursor:pointer;", "📋 复制剧本");
    copyBtn.title = "复制当前已处理剧本全文";
    const viewBtn = el("button", "flex:0 0 auto;height:26px;padding:0 8px;border-radius:5px;border:1px solid #5b9bd5;background:#2a4a6a;color:#cfe3f7;font-size:12px;cursor:pointer;", "📖 编辑剧本");
    viewBtn.title = "放大编辑当前已处理剧本（可改后保存）";
    const dirBtn = el("button", "flex:0 0 auto;height:26px;padding:0 8px;border-radius:5px;border:1px solid #8a6d3b;background:#5a4a2a;color:#ffd98a;font-size:12px;cursor:pointer;", "📁 输出目录");
    dirBtn.title = "打开当前故事的输出目录（output/jzl/{故事名}）：浏览 / 下载 / 删除 / 在线编辑文本（云机同样可用）";
    const storyName = el("span", "flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:#9fd6a4;", "");
    storyName.title = "当前加载的故事文件夹（自动识别=跟随「短剧导演台Max」的故事名称同名文件夹）";
    ctrlRow.append(storySel, storyName, refreshBtn, copyBtn, viewBtn, dirBtn);
    container.appendChild(ctrlRow);
    // 状态行（剧本字数 / 提示）
    const scriptStat = el("div", "font-size:11px;color:#888;flex:0 0 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-height:14px;", "");
    container.appendChild(scriptStat);
    const updateScriptStat = () => {
        const t = self.__viewerScript || "";
        if (t.trim()) { scriptStat.textContent = `📄 已处理剧本 ${t.length} 字${self.__viewerSource ? "（" + self.__viewerSource + "）" : ""}`; scriptStat.style.color = "#9fd6a4"; }
        else { scriptStat.textContent = "尚未获取剧本：先跑一次「短剧导演台Max」，再点「🔄 刷新目录」自动读取（开启增强读已增强剧本，否则读故事拆解）"; scriptStat.style.color = "#888"; }
    };
    self.__viewerUpdate = updateScriptStat;

    copyBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    copyBtn.addEventListener("click", () => {
        const t = self.__viewerScript || "";
        if (!t.trim()) { notify("目前没有剧本存在", "warning"); return; }
        copyTextToClipboard(t, "已处理剧本", "复制成功");
    });
    viewBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    // Viewer 编辑剧本的资产预览窗数据源：从同图「短剧导演台」（Pro/Max/Infinite/Mini）节点同步素材缓存
    viewBtn.addEventListener("click", () => {
        syncViewerAssetsFromManager(self);   // 打开前先取同图导演台素材 → 编辑界面资产预览窗有内容（复刻重拍模式放大编辑）
        // 无剧本也打开空白编辑界面；保存后写回 __viewerScript（可再复制到别处）
        openTextZoomEditor("📖 已处理剧本（编辑）", () => self.__viewerScript || "", (text) => {
            self.__viewerScript = text || "";
            updateScriptStat();
            notify("✅ 剧本已保存（可用「📋 复制剧本」复制到别处）", "success");
        }, self);
    });
    dirBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    // 桌面端（Windows/macOS/Linux 有图形界面）：点击 = 直接用系统文件管理器打开文件夹，不弹内置界面；
    // 云机/无桌面（os.startfile 无效）→ 自动回退内置文件浏览器，保证浏览/下载/删除仍可用。
    dirBtn.addEventListener("click", async () => {
        const sn = String(self.__viewerStory || "").trim();
        let lastErr = "";
        const _tryOpen = async (p) => {
            try {
                const r = await api.fetchApi("/jzl/fs_open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: p }) });
                let data = {};
                try { data = await r.json(); } catch (_) { data = {}; }
                if (data && data.ok) return true;
                lastErr = (data && data.error) || ("HTTP " + r.status + (r.status === 404 ? "（后端路由未注册 → 请重启 ComfyUI）" : ""));
                return false;
            } catch (e) { lastErr = (e && e.message) || String(e); return false; }
        };
        try { notify("📁 正在打开输出目录…", "success"); } catch (_) {}
        if (await _tryOpen(sn)) { notify("🖥️ 已在本机打开输出文件夹", "success"); return; }
        if (sn && await _tryOpen("")) { notify("该故事目录暂不存在，已打开 output/jzl 根目录", "warning"); return; }
        try { notify("⚠️ 系统文件夹打不开（云机/无桌面，或后端未重启）→ 已改用内置浏览器。原因：" + lastErr, "warning"); } catch (_) {}
        openOutputBrowser(sn);   // 一定兜底：网页内置文件浏览器（浏览/下载/删除/在线编辑）
    });

    // 视频宫格（2 列，右侧留滚动条位，随节点拉大卡片自适应放大）
    const scroll = el("div", "flex:1 1 auto;overflow-y:auto;min-height:0;scrollbar-gutter:stable;border:1px solid #333;border-radius:5px;background:#1a1a1a;padding:6px;box-sizing:border-box;");
    enableWheelScroll(scroll);   // Nodes 2.0：滚轮归本窗（否则会缩放画布）
    const grid = el("div", "display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;align-content:start;");
    scroll.appendChild(grid);
    container.appendChild(scroll);

    // 自动识别：读同图「短剧导演台Max」节点上的「故事名称」widget（同名文件夹）
    const _findMaxStoryName = () => {
        try {
            const g = self.graph || (window.app && window.app.graph);
            if (!g || !g._nodes) return "";
            for (const n of g._nodes) {
                if (n && (n.type === MAX_NODE_TYPE || n.type === INF_NODE_TYPE || n.type === SHORTDRAMA_NODE_TYPE)) {
                    const w = (n.widgets || []).find((x) => x.name === "story_name");
                    if (w) { const v = String(readWidgetValue(w) || "").trim(); if (v) return v; }
                }
            }
        } catch (_) {}
        return "";
    };
    const refresh = async () => {
        // 故事解析：自动识别 → 跟随 Max 的「故事名称」；手动 → 下拉选择值
        const autoMode = String(storySel.value || "") === AUTO_VAL;
        let sn = "";
        if (autoMode) {
            sn = _findMaxStoryName() || String(self.__viewerStory || "").trim();
        } else {
            sn = String(storySel.value || "").trim();
        }
        if (sn) { self.__viewerStory = sn; _setStoryWidget(sn); }
        storyName.textContent = sn ? `📺 ${sn}` : "";
        storyName.style.color = sn ? "#9fd6a4" : "#666";
        storyName.title = sn ? `已加载：output/jzl/${sn}/` : "自动识别：未找到「短剧导演台Max」（图中无 Max 或未填故事名称）";
        grid.innerHTML = "";
        if (!sn) {
            grid.append(el("div", "font-size:12px;color:#888;padding:6px;", autoMode ? "自动识别：未找到「短剧导演台Max」的故事名称（图中无 Max，或 Max 的「故事名称」为空）" : "暂无故事：从上方下拉选择已识别到的文件夹，或先跑一次「Max」生成"));
            updateScriptStat();
            return;
        }
        try {
            // 并行：视频列表 + 最后一次生成的故事拆解剧本
            const [vr, sr] = await Promise.all([
                api.fetchApi(`/jzl/generated_videos?story_name=${encodeURIComponent(sn)}`),
                api.fetchApi(`/jzl/story_latest_script?story_name=${encodeURIComponent(sn)}`),
            ]);
            const data = await vr.json().catch(() => ({}));
            const sdata = await sr.json().catch(() => ({}));
            const vids = (data && data.ok && data.videos) || [];
            if (!vids.length) {
                grid.append(el("div", "font-size:12px;color:#888;padding:6px;", `output/jzl/${sn}/ 暂无 mp4（跑一次「Max」后即会即时落盘）`));
            } else {
                vids.forEach((v) => {
                    const card = el("div", "cursor:pointer;border-radius:6px;border:1px solid #333;background:#111;overflow:hidden;");
                    card.title = `${v.name}\n（点击预览播放）`;
                    const im = document.createElement("img");
                    im.style.cssText = "width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#000;";
                    im.src = api.apiURL(`/jzl/video_thumb?path=${encodeURIComponent(v.path)}`);
                    im.onerror = () => { im.remove(); const ic = el("div", "width:100%;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;font-size:30px;background:#000;", "🎬"); card.insertBefore(ic, card.firstChild); };
                    const lb = el("div", "font-size:11px;color:#cdd8e2;text-align:center;padding:4px 3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", v.name);
                    card.append(im, lb);
                    card.addEventListener("mousedown", (e) => e.stopPropagation());
                    card.addEventListener("click", () => { if (v.path) showVideoPreview(v.path); });
                    grid.append(card);
                });
            }
            // 剧本：以磁盘「最后一次生成文件」为准（开启增强=已增强剧本，否则=故事拆解；空则不覆盖用户已编辑内容）
            const sc = (sdata && sdata.ok && sdata.script) || "";
            if (sc) { self.__viewerScript = sc; self.__viewerSource = (sdata && sdata.source) || ""; }
            updateScriptStat();
        } catch (e) {
            grid.append(el("div", "font-size:12px;color:#e88;padding:6px;", `加载失败：${(e && e.message) || e}`));
        }
    };

    // addDOMWidget（填满节点、拉大自适应）
    const widget = self.addDOMWidget?.("viewer_body", "JZL_VIDEO_VIEWER", container, { serialize: false, hideOnZoom: false });
    if (widget) {
        try { delete widget.computeSize; } catch { widget.computeSize = undefined; }
        widget.options = widget.options || {};
        widget.options.serialize = false;
        widget.options.getMinHeight = () => 220;
        widget.options.getHeight = () => "100%";
        widget.computeLayoutSize = () => ({ minHeight: 220, maxHeight: undefined, minWidth: 300 });
        widget.options.onDraw = () => { try { syncW(); } catch (_) {} };
        widget.options.afterResize = () => { try { syncW(); } catch (_) {} };
    }
    // Nodes 2.0：视频宫格会把节点撑到内容高度（详见 bindDomWidgetMaxHeight 注释）→ 限高后内部滚动
    const applyMaxH = bindDomWidgetMaxHeight(self, container, { minHeight: 200 });
    const syncW = () => {
        try {
            const fw = Number.isFinite(self.size?.[0]) ? self.size[0] : 0;
            if (widget && fw && widget.width !== fw) widget.width = fw;
        } catch (_) {}
        try { applyMaxH(); } catch (_) {}
    };
    syncW();
    self._jzlViewerWidget = widget;
    try { if (!Number.isFinite(self.size?.[1]) || self.size[1] < 300) self.setSize?.([420, 360]); } catch (_) {}

    updateScriptStat();
    // 故事下拉：首项「✨ 自动识别」（跟随 Max 故事名）+ 手动选择（列出 output/jzl 下已有故事）
    const autoStories = async () => {
        try {
            const r = await api.fetchApi("/jzl/generated_videos");
            const d = await r.json().catch(() => ({}));
            const list = (d && d.ok && d.stories) || [];
            const prev = storySel.value;
            storySel.innerHTML = "";
            const autoOpt = document.createElement("option");
            autoOpt.value = AUTO_VAL;
            autoOpt.textContent = "✨ 自动识别";
            autoOpt.title = "跟随「短剧导演台Max」的「故事名称」同名文件夹（改故事名后点刷新即同步）";
            storySel.appendChild(autoOpt);
            // 已识别文件夹平铺列出
            list.forEach((s) => { const o = document.createElement("option"); o.value = s.story; o.textContent = `📂 ${s.story}`; storySel.appendChild(o); });
            // 恢复选择：上次持久化故事在列表中则选中，否则自动识别
            const saved = String(_storyW ? String(readWidgetValue(_storyW) || "").trim() : "");
            storySel.value = (saved && list.some((s) => s.story === saved)) ? saved : AUTO_VAL;
            refresh();
        } catch (_) {}
    };
    // 供外部/「Max 执行完成」钩子调用：重新同步（跟随 Max 故事名 + 读最新视频/拆解剧本）
    self.__viewerRefresh = () => { refresh().catch(() => {}); };
    // 兼容：直接给故事名（手动指定）
    self.__viewerSetStory = (story) => {
        const s = String(story || "").trim();
        if (s) { self.__viewerStory = s; _setStoryWidget(s); storySel.value = AUTO_VAL; refresh(); }
        else autoStories();
    };
    autoStories();
    refresh();
}

// ── 📝 生成剧本查看器（独立节点 DOM）────────────────────────
// 自动同步「导演台」的「故事名称」→ 直接读盘 output/jzl/{故事名}/
// 最后一次生成或增强的剧本（优先「已增强剧本」目录，未增强则「故事拆解」）。
// 顶部：🔄 刷新目录 / 📋 复制剧本 / 📖 编辑剧本 / 📝 选择剧本（可进子文件夹挑任意剧本文档）。
// 下方：只读显示窗，直接展示剧本文本全文。
const SCRIPT_VIEWER_MANAGER_TYPES = new Set([
    MINI_NODE_TYPE, MAX_NODE_TYPE, INF_NODE_TYPE, SHORTDRAMA_NODE_TYPE, NODE_TYPE,
]);

// 把同图「导演台」节点的素材缓存搬到查看器节点上，供放大编辑器的「📁 资产显示窗」点击插入 @引用。
// ① 优先用导演台已缓存的 __jzlAssets（面板渲染时写入，最新）；
// ② 导演台本次还没缓存（面板没打开过）→ 直接解析它的 manager_settings.widgets 现场取素材。
// 返回是否取到素材。
function syncViewerAssetsFromManager(target) {
    try {
        const g = target?.graph || (window.app && window.app.graph);
        if (!g || !g._nodes) return false;
        const mgrs = g._nodes.filter((n) => n && SCRIPT_VIEWER_MANAGER_TYPES.has(n.type));
        const nonEmpty = (a) => !!(a && ((a.images || []).length || (a.videos || []).length || (a.audios || []).length));
        for (const n of mgrs) {
            if (nonEmpty(n.__jzlAssets)) { target.__jzlAssets = n.__jzlAssets; return true; }
        }
        for (const n of mgrs) {
            const w = (n.widgets || []).find((x) => x.name === "manager_settings");
            if (!w) continue;
            try {
                const st = JSON.parse(readWidgetValue(w) || "{}");
                if (nonEmpty(st && st.assets)) { target.__jzlAssets = st.assets; return true; }
            } catch (_) {}
        }
    } catch (_) {}
    return false;
}

// 「选择剧本」弹窗：在 output/jzl/{故事名} 下浏览，支持进入子文件夹挑选剧本文档。
// 后端复用 /jzl/fs_list（沙箱在 output/jzl，返回 dirs + files 与 rel 路径）。
function openScriptPicker(story, currentRel, onPick) {
    document.querySelectorAll(".jzl-scriptpicker").forEach((m) => m.remove());
    const SCRIPT_EXT = ["txt", "md"];
    let cur = String(story || "").replace(/^\/+|\/+$/g, "");
    const mkBtn = (label, bg, fg) => el("button", `flex:0 0 auto;height:26px;padding:0 10px;border-radius:5px;border:1px solid #5b9bd5;background:${bg || "#3a3a3a"};color:${fg || "#eee"};font-size:12px;cursor:pointer;`, label);
    const fmtTime = (t) => { try { const d = new Date((Number(t) || 0) * 1000); return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; } catch (_) { return ""; } };
    const overlay = el("div", "position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:100002;display:flex;align-items:center;justify-content:center;overflow:auto;");
    overlay.classList.add("jzl-scriptpicker");
    const box = el("div", "width:760px;max-width:94vw;max-height:88vh;background:var(--comfy-menu-bg,#242424);border:1px solid #555;border-radius:8px;display:flex;flex-direction:column;padding:12px;box-sizing:border-box;box-shadow:0 8px 40px rgba(0,0,0,.5);");
    const head = el("div", "display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;");
    head.appendChild(el("span", "font-size:14px;font-weight:600;color:#eee;white-space:nowrap;", "📝 选择剧本"));
    const pathBox = el("span", "flex:1 1 160px;min-width:0;font-size:12px;color:#9fd6a4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", "");
    const upBtn = mkBtn("⬆️ 上级");
    const latestBtn = mkBtn("✨ 最新生成（优先增强）", "#2a4a6a", "#cfe3f7");
    latestBtn.title = "自动读取该故事编号最大批次：优先「已增强剧本」，未增强则「故事拆解」";
    const closeBtn = mkBtn("关闭");
    head.append(pathBox, upBtn, latestBtn, closeBtn);
    box.appendChild(head);
    const list = el("div", "flex:1 1 auto;overflow-y:auto;min-height:160px;max-height:70vh;border:1px solid #333;border-radius:5px;background:#1a1a1a;padding:6px;");
    box.appendChild(list);
    box.appendChild(el("div", "font-size:11px;color:#888;margin-top:6px;white-space:pre-wrap;", "📂 点文件夹进入子目录；📄 点剧本文件即载入下方显示窗（仅列 .txt/.md）。"));
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    closeBtn.addEventListener("click", () => overlay.remove());
    upBtn.addEventListener("click", () => { const p = cur.includes("/") ? cur.slice(0, cur.lastIndexOf("/")) : ""; load(p); });
    latestBtn.addEventListener("click", () => {
        onPick({ latest: true });
        overlay.remove();
    });

    const mkRow = (icon, name, meta, onClick, color) => {
        const row = el("div", "display:flex;align-items:center;gap:8px;padding:5px 7px;border-radius:4px;cursor:pointer;font-size:12px;color:" + (color || "#ddd") + ";");
        row.addEventListener("mouseenter", () => { row.style.background = "#2a2a2a"; });
        row.addEventListener("mouseleave", () => { row.style.background = ""; });
        row.append(el("span", "flex:0 0 auto;", icon),
                   el("span", "flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;", name),
                   el("span", "flex:0 0 auto;font-size:11px;color:#777;", meta || ""));
        row.addEventListener("click", onClick);
        return row;
    };

    const load = async (rel) => {
        list.textContent = "加载中…";
        let data = {};
        try {
            const r = await api.fetchApi(`/jzl/fs_list?path=${encodeURIComponent(rel || "")}`);
            data = await r.json().catch(() => ({}));
        } catch (e) { list.textContent = "❌ 加载失败：" + ((e && e.message) || e); return; }
        if (!data || !data.ok) { list.textContent = "❌ " + ((data && data.error) || "无法读取目录"); return; }
        cur = data.rel || "";
        pathBox.textContent = "output/jzl/" + (cur ? cur + "/" : "");
        upBtn.disabled = !cur;
        upBtn.style.opacity = cur ? "1" : ".45";
        list.innerHTML = "";
        const dirs = (data.dirs || []).slice().sort((a, b) => (Number(b.mtime) || 0) - (Number(a.mtime) || 0));
        const files = (data.files || [])
            .filter((f) => SCRIPT_EXT.includes(String(f.ext || "").toLowerCase()))
            .sort((a, b) => (Number(b.mtime) || 0) - (Number(a.mtime) || 0));   // 最新修改优先
        if (!dirs.length && !files.length) {
            list.append(el("div", "font-size:12px;color:#888;padding:8px;", "此目录下没有子文件夹或剧本文档（.txt/.md）"));
        }
        dirs.forEach((d) => list.append(mkRow("📂", d.name, fmtTime(d.mtime), () => load(d.rel), "#cfe3f7")));
        files.forEach((f) => {
            const isCur = String(f.rel) === String(currentRel || "");
            list.append(mkRow("📄", f.name, (isCur ? "✅ 当前  " : "") + fmtTime(f.mtime),
                              () => { onPick({ rel: f.rel, name: f.name }); overlay.remove(); },
                              isCur ? "#9fd6a4" : "#ddd"));
        });
    };
    load(cur);
}

function setupScriptViewerNode(self) {
    // socketless story_name widget：隐藏 + 持久化「当前故事」
    const _storyW = (self.widgets || []).find((x) => x.name === "story_name");
    for (const w of self.widgets || []) {
        if ((w && (w.name || "") === "story_name")) {
            w.hidden = true;
            if (!w.options) w.options = {};
            w.options.hidden = true;
            w.computeSize = () => [0, -4];
        }
    }
    try { if (Array.isArray(self.widgets)) self.widgets = self.widgets.slice(); } catch (_) {}
    try { self.graph?.trigger?.("node:slot-label:changed", { nodeId: self.id, slotType: 2 }); } catch (_) {}

    // 剧本内容（默认 = 最后一次生成/增强；可用「选择剧本」覆盖为任意文件）
    self.__scriptText = "";
    self.__scriptSource = "";              // 已增强剧本 / 故事拆解 / 指定文件
    self.__scriptPick = String(self.__scriptPick || "");   // 手动挑选的 rel 路径（相对 output/jzl），空=跟随最新
    self.__scriptStory = String(_storyW ? String(readWidgetValue(_storyW) || "").trim() : "");
    const _setStoryWidget = (sn) => {
        if (_storyW && String(readWidgetValue(_storyW) || "").trim() !== sn) {
            try { setWidgetValue(_storyW, sn); } catch (_) {}
        }
    };

    const container = document.createElement("div");
    container.style.cssText = "width:100%;height:100%;display:flex;flex-direction:column;gap:6px;padding:8px;box-sizing:border-box;overflow:hidden;";

    // 顶行：故事下拉（自动识别=跟随导演台故事名）+ 刷新目录 + 复制剧本 + 编辑剧本 + 选择剧本
    const ctrlRow = el("div", "display:flex;align-items:center;gap:6px;flex:0 0 auto;");
    const storySel = document.createElement("select");
    storySel.title = "故事文件夹：自动识别=跟随「导演台」的故事名称同名文件夹；或手动选择 output/jzl 下已有故事";
    storySel.style.cssText = "flex:0 1 130px;min-width:0;background:#1d1d1d;color:#ddd;border:1px solid #444;border-radius:4px;padding:4px 3px;font-size:12px;";
    storySel.addEventListener("mousedown", (e) => e.stopPropagation());
    storySel.addEventListener("change", () => refresh());
    const AUTO_VAL = "__auto__";
    const refreshBtn = el("button", "flex:0 0 auto;height:26px;padding:0 8px;border-radius:5px;border:1px solid #5b9bd5;background:#2a4a6a;color:#cfe3f7;font-size:12px;cursor:pointer;", "🔄 刷新目录");
    refreshBtn.title = "重新同步导演台的故事名并读取最新剧本（优先已增强，否则故事拆解；已手动选择剧本时重读该文件）";
    refreshBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    refreshBtn.addEventListener("click", () => { refresh().then(() => { try { notify("🔄 已同步（" + (self.__scriptSource || "剧本") + "）", "success"); } catch (_) {} }); });
    const copyBtn = el("button", "flex:0 0 auto;height:26px;padding:0 8px;border-radius:5px;border:1px solid #5b9bd5;background:#3a3a3a;color:#eee;font-size:12px;cursor:pointer;", "📋 复制剧本");
    copyBtn.title = "复制当前显示的剧本全文";
    const viewBtn = el("button", "flex:0 0 auto;height:26px;padding:0 8px;border-radius:5px;border:1px solid #5b9bd5;background:#2a4a6a;color:#cfe3f7;font-size:12px;cursor:pointer;", "📖 编辑剧本");
    viewBtn.title = "放大编辑当前剧本（可改后保存；已指定文件时同时写回磁盘）";
    const pickBtn = el("button", "flex:0 0 auto;height:26px;padding:0 8px;border-radius:5px;border:1px solid #8a6d3b;background:#5a4a2a;color:#ffd98a;font-size:12px;cursor:pointer;", "📝 选择剧本");
    pickBtn.title = "在 output/jzl/{故事名} 下挑选剧本文档（支持进入子文件夹；也可选「最新生成（优先增强）」）";
    const storyName = el("span", "flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:#9fd6a4;", "");
    ctrlRow.append(storySel, storyName, refreshBtn, copyBtn, viewBtn, pickBtn);
    container.appendChild(ctrlRow);

    // 状态行：来源 + 字数
    const stat = el("div", "font-size:11px;color:#888;flex:0 0 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-height:14px;", "");
    container.appendChild(stat);
    const updateStat = () => {
        const t = self.__scriptText || "";
        if (t.trim()) {
            stat.textContent = `📝 ${self.__scriptSource || "剧本"}｜${t.length} 字${self.__scriptPick ? "（已指定文件）" : ""}`;
            stat.style.color = "#9fd6a4";
        } else {
            stat.textContent = "尚未获取剧本：先跑一次导演台，或点「📝 选择剧本」手动挑选";
            stat.style.color = "#888";
        }
    };
    self.__scriptUpdateStat = updateStat;

    // 显示窗：只读全文（等宽、可滚动、随节点拉大）
    const scroll = el("div", "flex:1 1 auto;min-height:0;overflow:auto;border:1px solid #333;border-radius:6px;background:#141414;padding:8px;box-sizing:border-box;");
    enableWheelScroll(scroll);   // Nodes 2.0：滚轮归本窗（否则会缩放画布）
    const pre = el("pre", "margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.65;color:#ddd;font-family:Consolas,Menlo,monospace;");
    scroll.appendChild(pre);
    container.appendChild(scroll);
    const renderText = () => {
        const t = self.__scriptText || "";
        pre.textContent = t.trim() ? t : "（暂无剧本内容）\n\n自动读取顺序：\n  output/jzl/{故事名}/第N次生成/已增强剧本/*.txt   ← 开启过增强时优先\n  output/jzl/{故事名}/第N次生成/故事拆解/*.txt     ← 未增强时\n也可点「📝 选择剧本」在子文件夹里挑选任意剧本文件。";
    };

    copyBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    copyBtn.addEventListener("click", () => {
        const t = self.__scriptText || "";
        if (!t.trim()) { notify("目前没有剧本内容", "warning"); return; }
        copyTextToClipboard(t, "剧本", "复制成功");
    });
    viewBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    viewBtn.addEventListener("click", () => {
        // ⭐ 打开前先把同图导演台的素材缓存同步过来 → 放大编辑器右侧「📁 资产显示窗」才有素材可点击插入 @引用
        const got = syncViewerAssetsFromManager(self);
        if (!got) notify("ℹ️ 图中未找到导演台素材：资产显示窗为空（先在同图放一个导演台并添加素材）", "warning");
        openTextZoomEditor("📖 剧本（编辑）", () => self.__scriptText || "", async (text) => {
            self.__scriptText = text || "";
            renderText();
            updateStat();
            // 已指定具体文件 → 同步写回磁盘，避免「改了但没保存」
            if (self.__scriptPick) {
                try {
                    const r = await api.fetchApi("/jzl/fs_write", {
                        method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ path: self.__scriptPick, text: self.__scriptText }),
                    });
                    const d = await r.json().catch(() => ({}));
                    if (d && d.ok) notify("✅ 剧本已保存到文件：" + self.__scriptPick, "success");
                    else notify("⚠️ 已更新显示，但写回磁盘失败：" + ((d && d.error) || "未知错误"), "warning");
                    return;
                } catch (e) { notify("⚠️ 已更新显示，但写回磁盘失败：" + ((e && e.message) || e), "warning"); return; }
            }
            notify("✅ 剧本已更新（未指定文件，仅本次显示/复制用）", "success");
        }, self);
    });
    pickBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    pickBtn.addEventListener("click", () => {
        const sn = String(self.__scriptStory || "").trim();
        if (!sn) { notify("请先确定故事名（自动识别导演台，或从上方下拉选择）", "warning"); return; }
        openScriptPicker(sn, self.__scriptPick, async (choice) => {
            if (choice.latest) {
                self.__scriptPick = "";
                await refresh(true);
                notify("✅ 已载入最新剧本（优先增强）", "success");
                return;
            }
            self.__scriptPick = choice.rel || "";
            try {
                const r = await api.fetchApi(`/jzl/fs_read?path=${encodeURIComponent(self.__scriptPick)}`);
                const d = await r.json().catch(() => ({}));
                if (!(d && d.ok)) { notify("❌ 读取失败：" + ((d && d.error) || "未知错误"), "error"); return; }
                self.__scriptText = d.text || "";
                self.__scriptSource = "指定文件 " + (choice.name || self.__scriptPick);
                renderText(); updateStat();
                notify("✅ 已载入剧本：" + (choice.name || self.__scriptPick), "success");
            } catch (e) { notify("❌ 读取失败：" + ((e && e.message) || e), "error"); }
        });
    });

    // 自动识别：读同图「导演台」节点上的「故事名称」widget
    const _findStoryName = () => {
        try {
            const g = self.graph || (window.app && window.app.graph);
            if (!g || !g._nodes) return "";
            for (const n of g._nodes) {
                if (n && SCRIPT_VIEWER_MANAGER_TYPES.has(n.type)) {
                    const w = (n.widgets || []).find((x) => x.name === "story_name");
                    if (w) { const v = String(readWidgetValue(w) || "").trim(); if (v) return v; }
                }
            }
        } catch (_) {}
        return "";
    };

    // forceDefault=true → 忽略手动指定的文件，直接回到「最新生成（优先增强）」
    const refresh = async (forceDefault) => {
        syncViewerAssetsFromManager(self);   // 顺带同步导演台素材缓存（放大编辑器资产显示窗用）
        const autoMode = String(storySel.value || "") === AUTO_VAL;
        let sn = "";
        if (autoMode) { sn = _findStoryName() || String(self.__scriptStory || "").trim(); }
        else { sn = String(storySel.value || "").trim(); }
        const storyChanged = sn && self.__scriptStory && sn !== self.__scriptStory;
        if (sn) { self.__scriptStory = sn; _setStoryWidget(sn); }
        if (forceDefault || storyChanged) self.__scriptPick = "";
        storyName.textContent = sn ? `📺 ${sn}` : "";
        storyName.style.color = sn ? "#9fd6a4" : "#666";
        storyName.title = sn ? `已加载：output/jzl/${sn}/` : "自动识别：未找到导演台（图中无导演台或未填故事名称）";
        if (!sn) { self.__scriptText = ""; self.__scriptSource = ""; renderText(); updateStat(); return; }
        // 已手动指定文件 → 重读该文件（内容可能刚被编辑）；否则读「最新生成（优先增强）」
        if (self.__scriptPick) {
            try {
                const r = await api.fetchApi(`/jzl/fs_read?path=${encodeURIComponent(self.__scriptPick)}`);
                const d = await r.json().catch(() => ({}));
                if (d && d.ok) {
                    self.__scriptText = d.text || "";
                    self.__scriptSource = "指定文件 " + self.__scriptPick.split("/").pop();
                    renderText(); updateStat(); return;
                }
                self.__scriptPick = "";      // 文件已不存在 → 回退到最新
            } catch (_) { self.__scriptPick = ""; }
        }
        try {
            const r = await api.fetchApi(`/jzl/story_latest_script?story_name=${encodeURIComponent(sn)}`);
            const d = await r.json().catch(() => ({}));
            const sc = (d && d.ok && d.script) || "";
            if (sc) {
                self.__scriptText = sc;
                self.__scriptSource = (d.source || "剧本") + (d.batch ? `（第${d.batch}次生成` + (d.file ? `｜${d.file}` : "") + "）" : "");
            } else {
                self.__scriptText = "";
                self.__scriptSource = "";
            }
            renderText(); updateStat();
        } catch (e) {
            self.__scriptText = "";
            self.__scriptSource = "";
            pre.textContent = "❌ 加载失败：" + ((e && e.message) || e);
            updateStat();
        }
    };

    // addDOMWidget（填满节点、拉大自适应）
    const widget = self.addDOMWidget?.("script_viewer_body", "JZL_SCRIPT_VIEWER", container, { serialize: false, hideOnZoom: false });
    if (widget) {
        try { delete widget.computeSize; } catch { widget.computeSize = undefined; }
        widget.options = widget.options || {};
        widget.options.serialize = false;
        widget.options.getMinHeight = () => 220;
        widget.options.getHeight = () => "100%";
        widget.computeLayoutSize = () => ({ minHeight: 220, maxHeight: undefined, minWidth: 300 });
        widget.options.onDraw = () => { try { syncW(); } catch (_) {} };
        widget.options.afterResize = () => { try { syncW(); } catch (_) {} };
    }
    // Nodes 2.0：长剧本会把节点撑到内容高度（详见 bindDomWidgetMaxHeight 注释）→ 限高后内部滚动
    const applyMaxH = bindDomWidgetMaxHeight(self, container, { minHeight: 200 });
    const syncW = () => {
        try {
            const fw = Number.isFinite(self.size?.[0]) ? self.size[0] : 0;
            if (widget && fw && widget.width !== fw) widget.width = fw;
        } catch (_) {}
        try { applyMaxH(); } catch (_) {}
    };
    syncW();
    self._jzlScriptWidget = widget;
    try { if (!Number.isFinite(self.size?.[1]) || self.size[1] < 300) self.setSize?.([420, 420]); } catch (_) {}

    updateStat();
    renderText();
    // 故事下拉：首项「✨ 自动识别」（跟随导演台故事名）+ 手动选择（列出 output/jzl 下已有故事）
    const autoStories = async () => {
        try {
            const r = await api.fetchApi("/jzl/generated_videos");
            const d = await r.json().catch(() => ({}));
            const list = (d && d.ok && d.stories) || [];
            storySel.innerHTML = "";
            const autoOpt = document.createElement("option");
            autoOpt.value = AUTO_VAL;
            autoOpt.textContent = "✨ 自动识别";
            autoOpt.title = "跟随「导演台」的「故事名称」同名文件夹（改故事名后点刷新即同步）";
            storySel.appendChild(autoOpt);
            list.forEach((s) => { const o = document.createElement("option"); o.value = s.story; o.textContent = `📂 ${s.story}`; storySel.appendChild(o); });
            const saved = String(_storyW ? String(readWidgetValue(_storyW) || "").trim() : "");
            storySel.value = (saved && list.some((s) => s.story === saved)) ? saved : AUTO_VAL;
            refresh();
        } catch (_) {}
    };
    // 供「导演台执行完成」钩子调用：重新同步（跟随故事名 + 读最新剧本）
    self.__scriptRefresh = () => { refresh().catch(() => {}); };
    self.__scriptSetStory = (story) => {
        const s = String(story || "").trim();
        if (s) { self.__scriptStory = s; _setStoryWidget(s); storySel.value = AUTO_VAL; refresh(); }
        else autoStories();
    };
    autoStories();
    refresh();
}
