import { app } from "../../scripts/app.js";

// ============================================================
// JZL_MiniMaxMusicLyricsEditor — 官方段落标签 + 人声角色标注
//   符号规范来源：MiniMax-Music3 README.md
//     「Lyrics define the words to be sung and may include explicit
//       section tags such as [Intro], [Verse], [Pre-Chorus], [Chorus],
//       [Post-Chorus], [Bridge], [Instrumental], [Solo], and [Outro]」
//   ⚠ 「人声角色」组为社区惯例，官方文档未定义语法；段落标签
//     与歌词文本必须在同一行会降低识别率，故插入时强制独占一行。
// ============================================================

const NODE_TYPE = "JZL_MiniMaxMusicLyricsEditor";
const TEXT_WIDGET = "lyrics_text";
const PICKER_ID = "jzl-lyrics-tag-picker";

// 官方 README 明列的 9 个段落标签
//   按钮文案 = 中文含义 + 实际标签（插进去的是第二个元素，中文只是给人看的）
const OFFICIAL_TAGS = [
    ["前奏 [Intro]", "[Intro]"],
    ["主歌 [Verse]", "[Verse]"],
    ["预副歌 [Pre-Chorus]", "[Pre-Chorus]"],
    ["副歌 [Chorus]", "[Chorus]"],
    ["后副歌 [Post-Chorus]", "[Post-Chorus]"],
    ["桥段 [Bridge]", "[Bridge]"],
    ["器乐间奏 [Instrumental]", "[Instrumental]"],
    ["独奏 [Solo]", "[Solo]"],
    ["尾奏 [Outro]", "[Outro]"],
];

// 编号 / 终段变体（官方示例仅用 [Verse]，编号为常见扩展写法）
const EXTRA_TAGS = [
    ["主歌1 [Verse 1]", "[Verse 1]"],
    ["主歌2 [Verse 2]", "[Verse 2]"],
    ["副歌1 [Chorus 1]", "[Chorus 1]"],
    ["副歌2 [Chorus 2]", "[Chorus 2]"],
    ["最终副歌 [Final Chorus]", "[Final Chorus]"],
];

// 人声角色标注（社区惯例，官方未文档化）—— 配合双人节点做男女对唱 / 合唱
//   ⚠️ 官方预处理（comfy/ldm/minimax_music/prompt.py）只把 [方括号] 当标签，
//      圆括号 (Singer A): 不会被识别，会被当成歌词内容唱出来 → 一律用 [ ]。
//   kind="block"  → 独占一行的段落标签
//   kind="inline" → 光标处行内插入（真实预处理会把 [标签] 自动拆到独立行）
const VOCAL_TAGS = [
    ["段落·男声 [Verse: Male Vocal]", "[Verse: Male Vocal]", "block"],
    ["段落·女声 [Verse: Female Vocal]", "[Verse: Female Vocal]", "block"],
    ["段落·对唱 [Chorus: Duet]", "[Chorus: Duet]", "block"],
    ["段落·合唱 [Chorus: Full Choir]", "[Chorus: Full Choir]", "block"],
    ["行·角色A [Singer A]", "[Singer A] ", "inline"],
    ["行·角色B [Singer B]", "[Singer B] ", "inline"],
    ["行·齐唱 [Together]", "[Together] ", "inline"],
];

function getLyricsWidget(node) {
    return (node.widgets || []).find(w => w.name === TEXT_WIDGET) || null;
}

function findTextarea(w) {
    const el = w.element || w.inputEl;
    if (!el) return null;
    if (el.tagName === "TEXTAREA") return el;
    if (typeof el.querySelector === "function") {
        return el.querySelector("textarea") || el.querySelector("input[type='text']") || null;
    }
    return null;
}

function bindCursorTracking(w) {
    const ta = findTextarea(w);
    if (!ta || ta.__jzlLyricsBound) return;
    ta.__jzlLyricsBound = true;
    const rec = () => {
        w._jzlSelStart = ta.selectionStart;
        w._jzlSelEnd = ta.selectionEnd;
    };
    for (const ev of ["focus", "click", "keyup", "mouseup"]) {
        ta.addEventListener(ev, rec);
    }
}

function insertTag(node, w, tag, inline) {
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

    // 段落标签独占一行；行级角色标注在光标处行内插入
    const prefix = (!inline && start > 0 && text[start - 1] !== "\n") ? "\n" : "";
    const inserted = inline ? tag : prefix + tag + "\n";
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

function closeTagPicker() {
    document.getElementById(PICKER_ID)?.remove();
    if (pickerEscHandler) {
        window.removeEventListener("keydown", pickerEscHandler);
        pickerEscHandler = null;
    }
}

let pickerEscHandler = null;

function makeButton(node, w, item) {
    const [label, tag, kind] = item;
    const inline = kind === "inline";
    const b = document.createElement("button");
    b.textContent = label;
    b.style.cssText = "padding:7px 6px;border:1px solid var(--jz-border);border-radius:6px;background:var(--jz-bg-raise);color:var(--jz-text-strong);cursor:pointer;font-size:12px;line-height:1.25;text-align:left;";
    b.addEventListener("mouseenter", () => { b.style.background = "#3a3a40"; });
    b.addEventListener("mouseleave", () => { b.style.background = "var(--jz-bg-raise)"; });
    b.addEventListener("click", () => { insertTag(node, w, tag, inline); closeTagPicker(); });
    if (inline) b.style.borderColor = "#6a5acd";
    return b;
}

function addGroup(panel, title, note, items, node, w, accent) {
    const h = document.createElement("div");
    h.textContent = title;
    h.style.cssText = `color:${accent || "#9ad"};font-size:12px;font-weight:600;margin:10px 0 6px;`;
    panel.appendChild(h);
    if (note) {
        const n = document.createElement("div");
        n.textContent = note;
        n.style.cssText = "color:var(--jz-text-dim);font-size:11px;margin:-3px 0 6px;line-height:1.35;";
        panel.appendChild(n);
    }
    const grid = document.createElement("div");
    grid.style.cssText = "display:grid;grid-template-columns:repeat(3,1fr);gap:6px;";
    for (const item of items) grid.appendChild(makeButton(node, w, item));
    panel.appendChild(grid);
}

function showTagPicker(node, w) {
    closeTagPicker();
    const overlay = document.createElement("div");
    overlay.id = PICKER_ID;
    overlay.style.cssText = "position:fixed;left:0;top:0;right:0;bottom:0;background:rgba(0,0,0,.45);z-index:99999;display:flex;align-items:center;justify-content:center;";
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) closeTagPicker(); });

    const panel = document.createElement("div");
    panel.style.cssText = "background:var(--jz-bg);border:1px solid var(--jz-border);border-radius:10px;padding:14px;max-width:700px;width:92%;max-height:86vh;overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,.6);";

    const title = document.createElement("div");
    title.textContent = "添加元素（插入到当前光标处）";
    title.style.cssText = "color:var(--jz-text-strong);font-size:14px;font-weight:600;margin-bottom:4px;";
    panel.appendChild(title);

    const sub = document.createElement("div");
    sub.textContent = "段落标签会独占一行并自动换行；行级角色标注直接插在光标处，请接着写歌词。";
    sub.style.cssText = "color:var(--jz-text-dim);font-size:11px;margin-bottom:8px;line-height:1.35;";
    panel.appendChild(sub);

    addGroup(panel, "① 官方段落标签（MiniMax 官方 README 明列）", null,
        OFFICIAL_TAGS, node, w, "#8fd18f");
    addGroup(panel, "② 编号 / 终段变体（官方示例的扩展写法）", null,
        EXTRA_TAGS, node, w, "#9ad");
    addGroup(panel, "③ 人声角色 —— 对唱 / 合唱（社区惯例，官方未文档化）",
        "⚠️ 只有 [方括号] 会被识别为标签；用 (Singer A): 这种圆括号写法会被当成歌词唱出来！" +
        "段落标签标注整段由谁唱；[Singer A] 插在句首可逐句指定角色。",
        VOCAL_TAGS, node, w, "#e0a96d");

    overlay.appendChild(panel);
    document.body.appendChild(overlay);

    pickerEscHandler = (e) => { if (e.key === "Escape") closeTagPicker(); };
    window.addEventListener("keydown", pickerEscHandler);
}

app.registerExtension({
    name: "JZL.MusicLyricsEditor",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_TYPE) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const self = this;
            const w = getLyricsWidget(self);
            if (!w) return result;

            setTimeout(() => bindCursorTracking(w), 300);

            const btn = self.addWidget("button", "➕ 添加元素", "➕ 添加元素", () => {
                showTagPicker(self, w);
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
