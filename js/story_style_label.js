/**
 * 故事风格下拉「显示备注」— 值仍保存短名（向后兼容）
 * =====================================================
 * ComfyUI 前端 ComboWidget 支持 options.getOptionLabel(value)：
 *   - 节点上显示 / 下拉菜单项 → 用格式化后的长文本（如「热血战斗（适合 竞技/武侠/修仙/末世）」）
 *   - 选中/保存/序列化 → 仍是原始短名（如「热血战斗」），后端解析、旧工作流完全不受影响。
 *
 * 标签映射由后端 /jzl/manager 返回（story_style_labels，与 schema 下拉同源），
 * 本脚本启动时拉取一次；拉取完成后统一刷新已存在节点上的 story_style combo。
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

let STYLE_LABELS = null; // short name -> display label（null=尚未拉取成功）

async function loadStyleLabels() {
    try {
        const resp = await api.fetchApi("/jzl/manager");
        const data = await resp.json().catch(() => ({}));
        STYLE_LABELS = data?.story_style_labels || null;
        if (STYLE_LABELS && Object.keys(STYLE_LABELS).length) {
            // 数据就绪：让所有已存在节点的 story_style combo 按新标签重绘
            try {
                const nodes = app.graph?._nodes || [];
                for (const n of nodes) n?.setDirtyCanvas?.(true, true);
            } catch (_) {}
        }
    } catch (_) {
        // 拉取失败则保持原样显示（仅退化为不带备注，不影响功能）
    }
}

function attachStyleLabel(widget) {
    if (!widget || widget.__jzlStyleLabelDone) return;
    // 仅处理 combo 下拉（含 V3 io.Combo 与 V1/V2 INPUT_TYPES combo）
    if (widget.type !== "combo") return;
    widget.__jzlStyleLabelDone = true;

    const prev = widget.options?.getOptionLabel;
    const fn = (value) => {
        if (STYLE_LABELS && value != null && STYLE_LABELS[String(value)] != null) {
            return STYLE_LABELS[String(value)];
        }
        if (typeof prev === "function") {
            try { return prev(value); } catch (_) {}
        }
        return value;
    };

    const opts = widget.options || {};
    // 原地写入 getOptionLabel，保留 values / serialize 等既有配置
    try {
        opts.getOptionLabel = fn;
        widget.options = opts;
    } catch (_) {
        try { widget.options = { ...opts, getOptionLabel: fn }; } catch (_) {}
    }
}

app.registerExtension({
    name: "JZL.StoryStyleLabel",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const orig = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = orig?.apply(this, arguments);
            const self = this;
            // 节点重建/加载（configure）后可能重新生成 combo widget，两处都尝试挂载
            setTimeout(() => {
                try {
                    const w = (self.widgets || []).find((x) => x?.name === "story_style");
                    if (w) attachStyleLabel(w);
                } catch (_) {}
            }, 0);
            return r;
        };
    },
});

void loadStyleLabels();
