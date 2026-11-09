/**
 * JZL MiniMax-H3 — 主题变量
 * ============================================================
 * 各节点的 DOM 面板（导演台、素材管理器、歌词编辑器等）原本把底色写死成深色，
 * 在 ComfyUI 浅色主题下会变成一整块黑。这里统一注入一组 CSS 变量，
 * 面板样式改用 var(--jz-*) 后即可自动跟随明暗主题切换，无需刷新页面。
 */

import { app } from "../../scripts/app.js";

const PALETTE = {
    // 名称: [深色值, 浅色值]
    "--jz-bg": ["#1b1b1f", "#ffffff"],
    "--jz-bg-soft": ["#1e1e1e", "#f4f5f7"],
    "--jz-bg-raise": ["#2a2a2e", "#e9eaee"],
    "--jz-bg-deep": ["#111111", "#f7f8fa"],
    "--jz-border": ["#3a3a3a", "#c9ced6"],
    "--jz-text": ["#dddddd", "#23262a"],
    "--jz-text-strong": ["#eeeeee", "#15181c"],
    "--jz-text-dim": ["#888888", "#5c626b"],
    "--jz-accent-bg": ["#2a4a6a", "#2f6fbf"],
    "--jz-accent-green": ["#2f5a3a", "#1f7a4a"],
    "--jz-accent-text": ["#cfe3f7", "#ffffff"],
    "--jz-on-accent": ["#cfe3f7", "#ffffff"],
    // 语义文字色：深色主题用浅色文字，浅色主题换成同色系的深色
    "--jz-info": ["#cfe3f7", "#1b5e9c"],
    "--jz-ok": ["#9fd6a4", "#1f7a3a"],
    "--jz-warn": ["#c9a86a", "#8a5f10"],
    "--jz-error": ["#e08a8a", "#a32626"],
    "--jz-orange": ["#e8a87c", "#8a4a1f"],
    "--jz-tint-info": ["#2a3a4a", "#e3edf9"],
    "--jz-tint-warn": ["#5a4a2a", "#fdf1d6"],
    "--jz-tint-ok": ["#2d5a3a", "#e7f5e9"],
    "--jz-tint-error": ["#7a2a2a", "#fdeaea"],
    "--jz-panel-2": ["#2f3540", "#eceef2"],
    "--jz-inset": ["#141414", "#f5f6f8"],
    "--jz-float": ["#2a2a2e", "#ffffff"],
    "--jz-btn-warn": ["#b45309", "#c2410c"],
    "--jz-btn-primary": ["#2d5a88", "#1f6fd0"],
    "--jz-btn-info": ["#2a3a4a", "#1f6fd0"],
    "--jz-btn-ok": ["#2d5a3a", "#1f7a3a"],
    "--jz-btn-error": ["#7a2a2a", "#b3261e"],
};

function prefersLight() {
    const html = document.documentElement;
    const body = document.body;
    if (html?.classList?.contains("light-theme")) return true;
    if (html?.classList?.contains("dark-theme")) return false;
    if (body?.classList?.contains("light-theme")) return true;
    if (body?.classList?.contains("dark-theme")) return false;
    try {
        const palette = app?.ui?.settings?.getSettingValue?.("Comfy.ColorPalette");
        if (palette === "light") return true;
        if (palette === "dark") return false;
    } catch (_) { /* ignore */ }
    return false;
}

function applyTheme() {
    const light = prefersLight();
    const root = document.documentElement;
    for (const [name, [dark, bright]] of Object.entries(PALETTE)) {
        root.style.setProperty(name, light ? bright : dark);
    }
    root.dataset.jzTheme = light ? "light" : "dark";
}

let installed = false;

function install() {
    if (installed) return;
    installed = true;
    applyTheme();

    const observer = new MutationObserver(applyTheme);
    observer.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["class", "data-theme"],
    });
    if (document.body) {
        observer.observe(document.body, { attributes: true, attributeFilter: ["class"] });
    }
}

app.registerExtension({
    name: "JZL.MiniMaxH3.Theme",
    async setup() {
        install();
    },
});
