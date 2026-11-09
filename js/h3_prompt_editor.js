import { app } from "../../scripts/app.js";

// ============================================================
// JZL_MiniMaxH3PromptEditor — H3 手写提示词快捷插入
//   点击按钮 → 在当前光标处另起一行插入对应模板
// ============================================================

const NODE_TYPE = "JZL_MiniMaxH3PromptEditor";
const TEXT_WIDGET = "prompt_text";
const PICKER_ID = "jzl-h3-prompt-picker";

// 完整示例文本（与节点默认文本框内容一致，供「示例文本」按钮插入）
const EXAMPLE_TEXT = `[SHOT_START]

===H3_PROMPT===
subject_definitions:
<Subject 1> 是 <Picture 1> 中的龟仙屋，临海沙滩上的粉色两层小屋，红色尖顶屋顶，墙面印有 KAME HOUSE 字样，四周棕榈树环绕，面朝蔚蓝大海。
<Subject 2> 是 <Picture 2> 中的孙悟空，标志性的黑色刺猬头爆炸发型，身穿橙色龟仙流武道服，蓝色腰带，深蓝色内衣和护腕，脚穿蓝红相间武道靴，有一条棕色的猴子尾巴。
<Subject 3> 是 <Picture 3> 中的比克，标志性绿色皮肤、尖长精灵耳，头部缠绕白色绷带，高大健壮的那美克星人，身穿紫色武道长裤，绿色腰带，身披宽大白色斗篷，双臂缠绕绷带。

summary:
[reference generation + audio reference] 目标视频展现 <Subject 2> 与 <Subject 3> 在 <Subject 1> 的龟仙屋前收拾行囊、并肩启程前往武道会的场景。

retention_analysis:
<Subject 1> (出现在 [Shot 1], [Shot 2], [Shot 3]): fully_preserved - 粉色两层小屋、红色尖顶、KAME HOUSE 字样、棕榈树环绕。
<Subject 2> (出现在 [Shot 1], [Shot 2], [Shot 3]): fully_preserved - 黑色刺猬头、橙色龟仙流武道服、棕色猴子尾巴。
<Subject 3> (出现在 [Shot 1], [Shot 2], [Shot 3]): fully_preserved - 绿色皮肤、白色绷带、紫色武道长裤、白色斗篷。
<Audio 1>: reference - <Subject 2> 的对话遵循 <Audio 1> 的音色。
<Audio 2>: reference - <Subject 3> 的对话遵循 <Audio 2> 的音色。

detailed_description:

正午阳光从海面方向斜照，暖金色调铺满整座沙滩，强烈的明暗对比勾勒出角色肌肉轮廓与体积感，海风持续掀起衣摆与斗篷下摆。写实电影级 3D 渲染风格，高饱和暖色主导，画面充满热血少年出发前的昂扬张力。

[Shot 1] 全景镜头确立 <Subject 1> 的龟仙屋全貌，粉色两层小屋的红色尖顶在暖阳下泛着微光，墙面 "KAME HOUSE" 字样清晰可辨，四周棕榈树叶被海风掀起向右侧剧烈摇晃。摄影机以小幅慢速向后拉远，<Subject 2> 孙悟空蹲在门廊前，把最后一件橙色武道服用力塞进行囊，随即直起身，黑色刺猬头在海风里微微晃动，棕色猴子尾巴在身后轻摆。他转头望向门框方向，以参考自 <Audio 1> 的年轻音色说道：<d>[中文] 我收拾好了。</d>

[Shot 2] At 00:04.500, 镜头切至 <Subject 3> 比克的中景，他双臂抱胸靠在门框旁，白色斗篷被风掀起一角，绿色皮肤在逆光下轮廓分明，尖长精灵耳微微颤动。他微微颔首，头部白色绷带随动作轻动，以参考自 <Audio 2> 的低沉音色应道：<d>[中文] 等你很久了。</d>

[Shot 3] At 00:08.000, 镜头拉回全景，<Subject 2> 孙悟空背起行囊，肩带在橙色武道服上勒出褶皱，<Subject 3> 比克从门框旁站直身，白色斗篷下摆被风掀起，两人并肩迈步走向海滩方向。摄影机以中速向后拉远，身后龟仙屋渐远，棕榈树影在地面拉长，海面波光粼粼，暖阳将两人的影子投射在沙滩上，步伐坚定有力。
overall_soundscape: 海风呼啸掠过棕榈叶，海浪拍打沙滩的哗啦声，行囊布料摩擦的沙沙声。

non_diegetic_music: N/A

===SCENE_INSTRUCTION===
{"slots":["场景:场景A","角色:角色A","角色:角色D"]}

===AUDIO_INSTRUCTION===
{"slots":["音频:音频A","音频:音频D"]}
[SHOT_END]`;

// [按钮标签, 插入内容, 是否末尾换行]
const TAGS = [
    ["1. 示例文本", EXAMPLE_TEXT, true],
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

function getPromptWidget(node) {
    return (node.widgets || []).find(w => w.name === TEXT_WIDGET) || null;
}

function findTextarea(w) {
    const el = w.element || w.inputEl;
    if (!el) return null;
    if (el.tagName === "TEXTAREA") return el;
    if (typeof el.querySelector === "function") {
        return el.querySelector("textarea") || null;
    }
    return null;
}

function bindCursorTracking(w) {
    const ta = findTextarea(w);
    if (!ta || ta.__jzlH3Bound) return;
    ta.__jzlH3Bound = true;
    const rec = () => {
        w._jzlSelStart = ta.selectionStart;
        w._jzlSelEnd = ta.selectionEnd;
    };
    for (const ev of ["focus", "click", "keyup", "mouseup"]) {
        ta.addEventListener(ev, rec);
    }
}

function insertTag(node, w, tag, trailing) {
    bindCursorTracking(w);
    const ta = findTextarea(w);
    const text = String(w.value ?? "");
    let start, end;

    if (typeof w._jzlSelStart === "number") {
        start = w._jzlSelStart;
        end = typeof w._jzlSelEnd === "number" ? w._jzlSelEnd : start;
    } else if (ta && document.activeElement === ta) {
        start = ta.selectionStart ?? text.length;
        end = ta.selectionEnd ?? start;
    } else {
        start = text.length;
        end = text.length;
    }
    if (!Number.isFinite(start) || start < 0) start = text.length;
    if (!Number.isFinite(end) || end < start) end = start;

    const prefix = start > 0 && text[start - 1] !== "\n" ? "\n" : "";
    const inserted = prefix + tag + (trailing ? "\n" : "");
    const next = text.slice(0, start) + inserted + text.slice(end);
    const pos = start + inserted.length;

    w.value = next;
    if (ta) {
        ta.value = next;
        try { ta.focus(); ta.setSelectionRange(pos, pos); } catch (_) {}
        ta.dispatchEvent(new Event("input", { bubbles: true }));
    }
    if (w.callback) w.callback(next);

    w._jzlSelStart = pos;
    w._jzlSelEnd = pos;
    node.graph?.setDirtyCanvas?.(true, true);
}

function closePicker() {
    document.getElementById(PICKER_ID)?.remove();
    if (pickerEscHandler) {
        window.removeEventListener("keydown", pickerEscHandler);
        pickerEscHandler = null;
    }
}

let pickerEscHandler = null;

function showPicker(node, w) {
    closePicker();
    const overlay = document.createElement("div");
    overlay.id = PICKER_ID;
    overlay.style.cssText = "position:fixed;left:0;top:0;right:0;bottom:0;background:rgba(0,0,0,.45);z-index:99999;display:flex;align-items:center;justify-content:center;";
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closePicker(); });

    const panel = document.createElement("div");
    panel.style.cssText = "background:var(--jz-bg);border:1px solid var(--jz-border);border-radius:10px;padding:14px;max-width:640px;width:92%;box-shadow:0 10px 40px rgba(0,0,0,.6);";

    const title = document.createElement("div");
    title.textContent = "添加元素（插入到当前光标处，自动另起一行）";
    title.style.cssText = "color:var(--jz-text-strong);font-size:14px;font-weight:600;margin-bottom:10px;";
    panel.appendChild(title);

    const grid = document.createElement("div");
    grid.style.cssText = "display:grid;grid-template-columns:repeat(3,1fr);gap:6px;";
    for (const [label, tag, trailing] of TAGS) {
        const b = document.createElement("button");
        b.textContent = label;
        b.style.cssText = "padding:7px 6px;border:1px solid var(--jz-border);border-radius:6px;background:var(--jz-bg-raise);color:var(--jz-text-strong);cursor:pointer;font-size:12px;line-height:1;";
        b.addEventListener("mouseenter", () => { b.style.background = "#3a3a40"; });
        b.addEventListener("mouseleave", () => { b.style.background = "var(--jz-bg-raise)"; });
        b.addEventListener("click", () => { insertTag(node, w, tag, trailing); closePicker(); });
        grid.appendChild(b);
    }
    panel.appendChild(grid);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);

    pickerEscHandler = (e) => { if (e.key === "Escape") closePicker(); };
    window.addEventListener("keydown", pickerEscHandler);
}

app.registerExtension({
    name: "JZL.H3PromptEditor",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_TYPE) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const self = this;
            const w = getPromptWidget(self);
            if (!w) return result;

            setTimeout(() => bindCursorTracking(w), 300);

            const btn = self.addWidget("button", "➕ 添加元素", "➕ 添加元素", () => {
                showPicker(self, w);
            }, { serialize: false });

            // 把「添加元素」按钮移到文本框上方
            const arr = self.widgets;
            const i = arr.indexOf(btn);
            if (i > -1) arr.splice(i, 1);
            const ti = arr.findIndex(x => x.name === TEXT_WIDGET);
            arr.splice(ti < 0 ? 0 : ti, 0, btn);
            self.setDirtyCanvas?.(true, true);

            return result;
        };
    },
});
