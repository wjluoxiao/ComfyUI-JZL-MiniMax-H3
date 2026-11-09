/**
 * JZL 列表分发 — 动态输出端口 + 显示框
 * ======================================
 * 复刻 XB 列表分发的动态输出管理，「输出数量」重建输出端口和显示框。
 * 输出端口名：分段1、分段2、...
 */

import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

const NODE_TYPE = "JZL_ListDispatcher";
const MAX_OUTPUTS = 99;

function setupDynamicOutputs(node) {
    const rebuild = () => {
        const wCount = node.widgets?.find(w => w.name === "输出数量");
        if (!wCount) return;
        const target = Math.min(MAX_OUTPUTS, Math.max(1, parseInt(wCount.value, 10) || 1));

        let current = (node.outputs || []).length;

        while (current > target) {
            const last = node.outputs[current - 1];
            if (!last.links || last.links.length === 0) {
                node.removeOutput(current - 1);
                current--;
            } else break;
        }

        while (current < target) {
            const idx = current + 1;
            node.addOutput(`分段${idx}`, "STRING");
            current++;
        }

        let displays = (node.widgets || []).filter(w => w.name && w.name.startsWith("显示_"));
        while (displays.length > target) {
            const w = displays.pop();
            w.onRemove?.();
            const i = node.widgets.indexOf(w);
            if (i >= 0) node.widgets.splice(i, 1);
        }
        while (displays.length < target) {
            const idx = displays.length + 1;
            const res = ComfyWidgets["STRING"](node, `显示_${idx}`, ["STRING", { multiline: true }], app);
            const dw = res.widget;
            // Nodes 2.0：只读要走 options.read_only（Vue 部件没有 inputEl 可改样式）
            try { if (dw.options) dw.options.read_only = true; } catch (_) {}
            if (dw.inputEl) {
                dw.inputEl.readOnly = true;
                dw.inputEl.style.backgroundColor = "var(--jz-bg-raise)";
                dw.inputEl.style.color = "var(--jz-text-dim)";
                dw.inputEl.style.fontSize = "12px";
                dw.inputEl.style.border = "1px solid var(--jz-border)";
            }
            displays.push(dw);
        }

        const sz = node.computeSize();
        if (node.size && sz[0] < node.size[0]) sz[0] = node.size[0];
        node.setSize(sz);
        node.graph?.setDirtyCanvas?.(true, true);
    };

    if (!(node.widgets || []).find(w => w.name === "更新输出")) {
        node.addWidget("button", "更新输出", null, rebuild);
    }

    const wCount = node.widgets?.find(w => w.name === "输出数量");
    if (wCount) {
        const origCb = wCount.callback;
        wCount.callback = function (value, canvas) {
            if (origCb) origCb.apply(this, arguments);
            if (!canvas) rebuild();
        };
    }

    return rebuild;
}

app.registerExtension({
    name: "JZL.MiniMax.ListDispatcher",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_TYPE) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated?.apply(this, arguments);
            const rebuild = setupDynamicOutputs(this);

            // 新建节点时 widgets/outputs 可能尚未就绪，setTimeout 兜底立即重建一次
            setTimeout(() => rebuild(), 50);

            const origConfigure = this.configure;
            this.configure = function (info) {
                if (origConfigure) origConfigure.apply(this, arguments);
                rebuild();
            };

            return r;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            if (onExecuted) onExecuted.apply(this, arguments);
            if (message?.displays) {
                const displays = (this.widgets || [])
                    .filter(w => w.name && w.name.startsWith("显示_"))
                    .sort((a, b) => parseInt(a.name.slice(3)) - parseInt(b.name.slice(3)));
                for (let i = 0; i < displays.length; i++) {
                    const v = i < message.displays.length ? message.displays[i] : "(空)";
                    if (displays[i].value !== v) displays[i].value = v;
                }
                const sz = this.computeSize();
                if (this.size && sz[0] < this.size[0]) sz[0] = this.size[0];
                this.setSize(sz);
                this.graph?.setDirtyCanvas?.(true, true);
            }
        };
    },

    async loadedGraphNode(node) {
        if (node.type !== NODE_TYPE) return;
        setTimeout(() => {
            const rebuild = setupDynamicOutputs(node);
            rebuild();
        }, 300);
    },
});
