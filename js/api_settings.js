import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ============================================================
// JZL_MiniMaxAPISettings — 点击「打开 API 设置」弹窗配置
//   参照 MiniMax H3 Easy 的 prompt_optimizer_settings modal 模式：
//   后端存磁盘（ComfyUI user 目录），Key 用 password 掩码，不明文进工作流
// ============================================================

const NODE_TYPE = "JZL_MiniMaxAPISettings";
const ENDPOINT = "/jzl/api_settings";
const PROVIDERS = [
    "OpenAI 兼容 (OpenAI/DeepSeek/Qwen/GLM/Kimi/Ollama/vLLM/LM Studio)",
    "Anthropic",
    "Google Gemini",
];

let settingsModal = null;

function getWidget(node, name) { return node.widgets?.find((w) => w.name === name); }
function asBoolean(v) { return v === true || v === "true" || Number(v) === 1; }

function notify(msg, type = "success") {
    try {
        if (app?.ui?.toast) { app.ui.toast.add({ text: msg, type }); return; }
    } catch (_) {}
    console.log(`[JZL API] ${msg}`);
}

async function loadSettings() {
    const resp = await api.fetchApi(ENDPOINT);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data?.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
    return data.settings || {};
}

async function saveSettings(value) {
    const resp = await api.fetchApi(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(value),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data?.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
    return data.settings || value;
}

function resetToggle(node) {
    const widget = getWidget(node, "open_settings");
    if (!widget) return;
    widget.value = false;
    if (widget._state) widget._state.value = false;
    node.setDirtyCanvas?.(true, true);
}

function makeRow(labelText, control) {
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:10px;margin:7px 0;";
    const label = document.createElement("span");
    label.style.cssText = "flex:0 0 88px;color:var(--fg-color,var(--jz-text));font-size:13px;white-space:nowrap;";
    label.textContent = labelText;
    row.append(label, control);
    return row;
}

function makeInput(type, value, placeholder) {
    const el = document.createElement("input");
    el.type = type;
    el.value = value ?? "";
    el.placeholder = placeholder || "";
    el.style.cssText = "flex:1;background:var(--comfy-input-bg,var(--jz-bg));color:var(--fg-color,var(--jz-text));" +
        "border:1px solid var(--border-color,var(--jz-border));border-radius:4px;padding:6px 8px;font-size:13px;width:0;";
    el.spellcheck = false;
    el.autocomplete = "off";
    return el;
}

function makeSelect(options, value) {
    const sel = document.createElement("select");
    sel.style.cssText = "flex:1;background:var(--comfy-input-bg,var(--jz-bg));color:var(--fg-color,var(--jz-text));" +
        "border:1px solid var(--border-color,var(--jz-border));border-radius:4px;padding:6px 8px;font-size:13px;width:0;";
    options.forEach((opt) => {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = opt;
        sel.append(o);
    });
    sel.value = options.includes(value) ? value : options[0];
    return sel;
}

function makeButton(text, primary) {
    const btn = document.createElement("button");
    btn.type = primary ? "submit" : "button";
    btn.textContent = text;
    btn.style.cssText = "background:var(--comfy-button-bg,var(--jz-border));color:var(--fg-color,var(--jz-text-strong));" +
        "border:1px solid var(--border-color,var(--jz-border));border-radius:4px;padding:6px 16px;cursor:pointer;font-size:13px;" +
        (primary ? "background:#3a7ca5;border-color:#3a7ca5;color:var(--jz-on-accent);" : "");
    return btn;
}

function makeSwitch(initialChecked) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.role = "switch";
    toggle.checked = Boolean(initialChecked);
    toggle.style.cssText = "position:relative;width:40px;height:22px;border-radius:11px;" +
        "border:1px solid var(--border-color,var(--jz-border));background:var(--comfy-input-bg,var(--jz-border));" +
        "cursor:pointer;flex:0 0 auto;padding:0;";
    const thumb = document.createElement("span");
    thumb.style.cssText = "position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;" +
        "background:var(--jz-text-dim);transition:left .15s,background .15s;";
    const render = () => {
        toggle.setAttribute("aria-checked", toggle.checked ? "true" : "false");
        thumb.style.left = toggle.checked ? "20px" : "2px";
        thumb.style.background = toggle.checked ? "#4caf50" : "var(--jz-text-dim)";
    };
    toggle.append(thumb);
    toggle.addEventListener("click", () => { toggle.checked = !toggle.checked; render(); });
    render();
    return toggle;
}

async function openSettings(node) {
    if (settingsModal) {
        settingsModal.dialog?.querySelector?.("input,button,select")?.focus?.();
        return;
    }

    let cache = {};
    try {
        cache = await loadSettings();
    } catch (e) {
        notify(String(e?.message || e), "error");
    }

    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10000;" +
        "display:flex;align-items:center;justify-content:center;";
    const dialog = document.createElement("section");
    dialog.style.cssText = "background:var(--comfy-menu-bg,#202020);border:1px solid var(--border-color,var(--jz-border));" +
        "border-radius:8px;padding:18px 20px;width:min(580px,92vw);max-height:88vh;overflow:auto;color:var(--fg-color,var(--jz-text));";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-label", "LLM-API 设置");

    const title = document.createElement("div");
    title.textContent = "LLM-API 设置";
    title.style.cssText = "font-size:16px;font-weight:600;margin-bottom:4px;";
    const subtitle = document.createElement("div");
    subtitle.textContent = "配置保存在本地（ComfyUI user 目录），不会写入工作流。";
    subtitle.style.cssText = "font-size:12px;color:var(--fg-color,var(--jz-text-dim));margin-bottom:12px;";

    const provider = makeSelect(PROVIDERS, cache.provider);
    const model = makeInput("text", cache.model, "模型名：deepseek-v4-flash / deepseek-v4-pro / gpt-5.5-pro / qwen3.6-max ...");
    const apiKey = makeInput("password", cache.api_key, "API Key（Ollama/vLLM/LM Studio 本地可留空）");
    apiKey.autocomplete = "new-password";
    const baseUrl = makeInput("text", cache.base_url, "OpenAI 兼容 base_url，留空用默认 https://api.openai.com/v1");
    const temperature = makeInput("number", cache.temperature ?? 0.6, "0.0 ~ 2.0");
    temperature.min = "0"; temperature.max = "2"; temperature.step = "0.01";
    const maxTokens = makeInput("number", cache.max_tokens ?? 81920, "1 ~ 262144");
    maxTokens.min = "1"; maxTokens.max = "262144"; maxTokens.step = "1";

    const thinkingLabel = document.createElement("div");
    thinkingLabel.style.cssText = "display:flex;align-items:center;gap:10px;margin:7px 0;";
    const thinkingText = document.createElement("span");
    thinkingText.style.cssText = "flex:0 0 88px;color:var(--fg-color,var(--jz-text));font-size:13px;white-space:nowrap;";
    thinkingText.textContent = "思考模式";
    const thinking = makeSwitch(cache.thinking === "enabled");
    const thinkingHint = document.createElement("span");
    thinkingHint.style.cssText = "flex:1;color:var(--fg-color,var(--jz-text-dim));font-size:12px;";
    thinkingHint.textContent = "DeepSeek V4 开启=先思考再回答（更稳但更慢更费 token），关闭=更快更省";
    thinkingLabel.append(thinkingText, thinking, thinkingHint);

    const form = document.createElement("form");
    form.id = "jzl-api-settings-form";
    form.append(
        makeRow("Provider", provider),
        makeRow("模型名", model),
        makeRow("API Key", apiKey),
        makeRow("Base URL", baseUrl),
        makeRow("温度", temperature),
        makeRow("最大Token", maxTokens),
        thinkingLabel,
    );

    const error = document.createElement("div");
    error.style.cssText = "color:#e06c75;font-size:12px;margin-top:8px;";
    error.hidden = true;

    const footer = document.createElement("div");
    footer.style.cssText = "display:flex;justify-content:flex-end;gap:8px;margin-top:14px;";
    const cancelBtn = makeButton("取消", false);
    const saveBtn = makeButton("保存", true);
    saveBtn.setAttribute("form", "jzl-api-settings-form");
    footer.append(cancelBtn, saveBtn);

    dialog.append(title, subtitle, form, error, footer);
    overlay.append(dialog);
    document.body.append(overlay);

    const close = () => {
        document.removeEventListener("keydown", onKeyDown, true);
        overlay.remove();
        settingsModal = null;
        resetToggle(node);
    };
    const onKeyDown = (event) => {
        if (event.key === "Escape") { event.preventDefault(); close(); }
    };
    settingsModal = { dialog, close };
    document.addEventListener("keydown", onKeyDown, true);
    overlay.addEventListener("pointerdown", (event) => { if (event.target === overlay) close(); });
    cancelBtn.addEventListener("click", close);

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saveBtn.disabled) return;
        saveBtn.disabled = true;
        error.hidden = true;
        try {
            await saveSettings({
                provider: provider.value,
                model: model.value.trim(),
                api_key: apiKey.value.trim(),
                base_url: baseUrl.value.trim(),
                temperature: Number(temperature.value),
                max_tokens: Number(maxTokens.value),
                thinking: thinking.checked ? "enabled" : "disabled",
            });
            notify("API 设置已保存");
            close();
            // 配置已变：标记相关节点脏，下次执行时 IS_CHANGED 会触发重跑
            (app.graph?._nodes || []).forEach((n) => {
                if (n.type === NODE_TYPE) n.setDirtyCanvas?.(true, true);
            });
        } catch (e) {
            error.textContent = String(e?.message || e);
            error.hidden = false;
            saveBtn.disabled = false;
        }
    });

    setTimeout(() => model.focus?.(), 50);
}

app.registerExtension({
    name: "jzl.minimaxApiSettings",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_TYPE) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated?.apply(this, arguments);
            const widget = getWidget(this, "open_settings");
            if (widget && !widget.__jzlApiSettingsBound) {
                widget.__jzlApiSettingsBound = true;
                const original = widget.callback;
                widget.callback = (value) => {
                    original?.call(widget, value);
                    resetToggle(this);
                    if (asBoolean(value)) openSettings(this);
                    this.setDirtyCanvas?.(true, true);
                };
            }
            return r;
        };
    },
});
