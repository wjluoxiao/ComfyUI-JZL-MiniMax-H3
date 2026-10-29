# ComfyUI-JZL-MiniMax-H3

MiniMax H3 视频生成节点（基于 ComfyUI V3 `io.Schema` 机制）。

## 当前节点

| 节点 | node_id | 说明 |
|------|---------|------|
| 🎬 MiniMax H3 参考编码 | `JZL_MiniMaxH3ReferenceToVideo` | ref2va 参考条件编码，100% 复刻官方 `MiniMaxH3ReferenceToVideo` |
| 🎭 MiniMax-H3 人物替换 | `JZL_MiniMaxCharacterSwap` | 长视频换人：源片自动切段，每段以源片段为 `<Video 1>`、定妆图为 `<Picture N>` 走 ref2va 重生成；段间钉帧 + 跑道裁切 ⇒ 成片时长 = 源片时长、段界无缝（详见 [docs/USAGE.md](docs/USAGE.md#七人物替换长视频换人)） |

## 特性

- **V3 原生动态接口**：参考图 / 参考视频 / 视频音轨 / 独立音频 四组端口用 `io.Autogrow` 实现「连接后自动长出下一个空槽」，与官方一致。
- **参考值放大**（JZL 扩展）：仅 `match` 模式生效，`ref_scale` 范围 `1.0~5.0`，步长 `0.1`。最终像素面积 = 生成画布面积 × 倍率（面积倍率，非分辨率倍率）。`1.0` = 官方行为。

## 提示词标签

| 标签 | 含义 |
|------|------|
| `<Picture i>` | 第 i 张参考图 |
| `<Video k>` | 第 k 段参考视频 |
| `<Audio j>` | 第 j 条参考音频（视频音轨会在其 `<Video>` 前单独成标签） |

## 安装

把本目录放入 ComfyUI 的 `custom_nodes/` 下，或建立符号链接：

```powershell
New-Item -ItemType Junction -Path "D:\...\ComfyUI\custom_nodes\ComfyUI-JZL-MiniMax-H3" -Target "D:\AI_JZL\ComfyUI-JZL-MiniMax-H3"
```

> ⚠️ **解压最常见的坑：不要多套一层目录。** 正确的结构是
> `custom_nodes\ComfyUI-JZL-MiniMax-H3\__init__.py`。
> 若解压后变成 `custom_nodes\ComfyUI-JZL-MiniMax-H3\ComfyUI-JZL-MiniMax-H3\__init__.py`，
> ComfyUI 找不到入口文件，**一个节点都不会出现**（日志里是 `Cannot import ... module for custom nodes`）。

### 加载日志（排查「节点不显示」）

启动时本插件会打印一行加载结果，可直接判断：

- ✅ 成功：`[JZL MiniMax H3] ✅ 加载成功：37 个节点 ｜ v0.9.02 ｜ ComfyUI 0.41.2 ｜ …`
- ⚠️ 部分失败：`⚠️ 部分模块加载失败（N 组正常…）` 并在下方逐条列出失败的模块与处置建议（其余节点仍可用）
- ❌ 整体失败：`❌ 节点包加载失败：ComfyUI-JZL-MiniMax-H3（节点列表里不会出现本插件）`，
  附带**失败模块 / 原因 / 处置建议 / 排查步骤**；核心模块（官方编码链路）失败时该结构才会出现。

一行命令拿到完整环境报告（排障时把这行输出发回即可）：

```powershell
<你的 ComfyUI python> "custom_nodes\ComfyUI-JZL-MiniMax-H3\jzl_diagnose.py"
```

## 依赖

- **ComfyUI ≥ 0.29.0**（含官方 MiniMax H3 支持；`comfy_api.latest` 自 0.3.47 起、H3 核心自 0.29.0 起）
  - 推荐 **≥ 0.37.0**：低于它时缺 `comfy.audio`，音频重采样会**自动回退 torchaudio**（功能不受影响）。
  - ComfyUI **0.41.x 已实测通过**（加载 37 个节点）。
- `torch`、`torchaudio`（缺失或与 torch 版本不匹配会导致本插件整个不加载，日志会直接指出）

## 本地 LLM 运行时（llama-server）

「模型加载 Pro / 剧本与镜头处理器」的本地 LLM/VLM 推理不再依赖 `llama-cpp-python`，改用 `llama-server` 子进程（进程隔离、跑完即停、自动释放显存）。

首次使用本地模型前，请先运行安装器（仅用 Python 标准库，自动识别系统/架构/显卡，下载并校验固定 llama.cpp `b10436` 预编译运行时，零 pip 依赖）：

- **Windows：双击 `install_runtime.bat` 一键安装**（自动定位 Python）
- 或手动执行：

```powershell
cd custom_nodes\ComfyUI-JZL-MiniMax-H3
..\..\..\python_embeded\python.exe install_runtime.py   # Windows 便携包
# 或：python install_runtime.py
```

- `--dry-run` 只看检测结果与计划，不下载
- `--list-backends` 查看当前机器可用后端
- `--force` 强制重装

> 已安装的 `llama-cpp-python` 无需卸载，也不会冲突（本节点已不再引用它）；想省磁盘可自行 `pip uninstall llama-cpp-python`。
