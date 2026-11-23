# 第三方代码声明（Third-Party Notices）

本仓库包含下列第三方代码副本（vendored）。其版权归原作者所有，遵循其原始协议。

---

## MinimaxH3LatentUpscaler3D（MiniMax H3 latent 放大节点）

| 项 | 内容 |
|---|---|
| 文件 | `jzl_h3_latent_upscaler_3d.py`（上游路径 `nodes/minimax_h3_latent_upscaler_3d.py`） |
| 来源 | https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler |
| 协议 | **MIT License**，Copyright (c) 2026 LBH-123-AI<br>协议全文：`docs/third_party/Comfyui_Minimax_h3_latent_Upscaler_LICENSE.txt` |
| 取用版本 | 仓库提交 `d7c01b9`，文件 SHA-256 `744063B43E0F3EEC23E2485CB7C65503069946CA9690906ECB548D7515CB89E2` |

**本地改动**：仅在本文件顶部追加「来源 / 协议 / 版本」说明注释，代码本体未作任何修改。

**用途**：本插件（导演台）的「二采 latent 放大」直接调用该类的 `execute()`。内置副本后：

- 用户**无需**再另行安装该节点包即可使用二采放大；
- 不会再因为机器上存在多份同名 `minimax_h3_latent_upscaler_3d.py`（其中一份无法被独立加载）而跳过可用副本、误报「缺少 Minimax H3 Latent Upscaler (3D) 节点」。

**不会造成重复节点**：本插件**不导出**该文件的 `NODE_CLASS_MAPPINGS`，只取类来内部调用；用户若自行安装了同名节点，仍由用户自己那份对外提供节点。

**模型权重仍需自行下载**：内置的是节点实现代码，不含权重。请把
`minimax_h3_latent_upscaler_3d_fp32.pth` / `_fp16.safetensors` / `_bf16.safetensors`
放到 `ComfyUI/models/latent_upscale_models/`。

**升级方式**：从上游拉取新版本覆盖 `jzl_h3_latent_upscaler_3d.py`（保留顶部注释块），并同步更新本文件中的提交号与 SHA-256。

**代码查找顺序**（`nodes_asset_manager.py` 的 `_import_upscaler_3d()`）：

1. 本插件内置副本（首选，行为确定、不受用户环境影响）
2. ComfyUI 全局注册表（用户自装的该节点，若已成功加载）
3. 扫描 `custom_nodes` 下所有同名副本逐个尝试（含包路径 / 裸模块名 / 按文件加载）
4. 全部失败时，报错中列出每份副本的失败原因，便于定位

---

## MinimaxH3LatentUpscaler2D（MiniMax H3 latent 放大节点 · 2D 架构）

| 项 | 内容 |
|---|---|
| 文件 | `jzl_h3_latent_upscaler_2d.py`（上游路径 `nodes/minimax_h3_latent_upscaler_2d.py`） |
| 来源 | https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler （与 3D 同一仓库） |
| 协议 | **MIT License**，Copyright (c) 2026 LBH-123-AI<br>协议全文：`docs/third_party/Comfyui_Minimax_h3_latent_Upscaler_LICENSE.txt` |
| 取用版本 | 上游 2D 节点（2026-11-10 版，与本机安装副本一致），文件 SHA-256 `6CD97F6DCE1891BDA1C06BCC1ECAE19C900E02ECFF99407984B889A98C84ACF6` |
| 依赖 | `einops`（已写入 `requirements.txt`；缺它时只影响 2D 权重，3D 权重照常可用） |

**本地改动**：仅在本文件顶部追加「来源 / 协议 / 版本」说明注释，代码本体未作任何修改。

**用途**：`*_2d_conv_*` 那套权重（`resizer.*` + 独立 `temporal_blocks`）与 3D 那套网络结构不同，
内置 3D 加载器读不了它的 state_dict。本插件把 2D 实现也内置了一份，由
`nodes_asset_manager.py::_run_upscaler()` 按权重键名自动分派：

- `resizer.*` / `*.temporal_blocks.*` → `MinimaxH3LatentUpscalerNode2D`（本文件）
- `conv_in.weight` → `MinimaxH3LatentUpscaler3D`（3D 文件）

**独立性**：与 3D 一致 —— **不导出** `NODE_CLASS_MAPPINGS`，只内部调用；用户装不装
「Comfyui_Minimax_h3_latent_Upscaler」都不影响本插件（找不到内置副本时才回落到全局注册表）。

**模型权重仍需自行下载**：`minimax_h3_latent_upscaler_2d_conv_v1_{fp32.pth,fp16,bf16.safetensors}`
放到 `ComfyUI/models/latent_upscale_models/`（可与 3D 权重放同一目录）。

**代码查找顺序**（`nodes_asset_manager.py` 的 `_import_upscaler_2d()`）：
内置副本 → 按文件加载 → ComfyUI 全局注册表，全失败时报错说明缺什么。

---

## comfyui-SelfLift（SelfLift 渐进分辨率采样）

| 项 | 内容 |
|---|---|
| 目录 | `vendor_selflift/`（整包搬运：`nodes.py` / `selflift.py` / `h3_upscaler.py` / `h3_tiling.py` / `h3_tst.py` / `diagnostics.py`） |
| 来源 | ComfyUI 插件 comfyui-SelfLift（`custom_nodes/comfyui-SelfLift`），节点显示名 `SelfLift Progressive Sampler (MiniMax H3)` |
| 论文 | SelfLift — https://arxiv.org/abs/2609.02036 ；TST — https://arxiv.org/abs/2609.08505 |
| 上游未附协议文件 | 本仓库仅按用户要求做本地副本，未改动上游代码；升级请用上游版本覆盖 |

**本地改动**：仅在各文件顶部追加「来源 / 校验」注释。

**用途**：导演台「🎭 采样解码设置 → 采样分类 = SelfLift采样」内部调用 `SelfLiftH3Sampler`。
**不会重复注册节点**：本插件不导出该副本的 `NODE_CLASS_MAPPINGS`。
**依赖**：`psutil`（`diagnostics.py`，ComfyUI 自带）；外部 H3 latent 放大权重仍然可选，放在
`ComfyUI/models/latent_upscale_models/`（未下载时用 `none` = nearest 提升 + 像素锚点修正）。

---

## MiniMax_H3_Semantic_Bridge（语义桥 / 语义修正）

| 项 | 内容 |
|---|---|
| 目录 | `vendor_semantic_bridge/`（`nodes.py`） |
| 来源 | ComfyUI 插件 MiniMax_H3_Semantic_Bridge v1.0（`custom_nodes/MiniMax_H3_Semantic_Bridge`），节点显示名 `MiniMax H3 Image to Video + Semantic Bridge` |
| 上游未附协议文件 | 本仓库仅按用户要求做本地副本，未改动上游代码；升级请用上游版本覆盖 |

**本地改动**：仅在文件顶部追加「来源 / 校验」注释。

**用途**：导演台「🎛️ 运行参数设置 → 语义修正」内部调用 `_apply_distilled_bridge`，对 H3 文本条件
（`[B,T,5120]`）做蒸馏学生网络的混合修正：`hybrid = h + alpha * (student(rms_norm(h)) - h)`。
**不会重复注册节点**：本插件不导出该副本的 `NODE_CLASS_MAPPINGS`。
**适配器**：`ComfyUI/models/semantic_bridge/MiniMaxH3_SemanticBridge_v1.safetensors`（推荐 `alpha=0.10`、`per_token`）。

