# XMind MCP Server

> 让大模型真正“看懂”并“修改 & 美化” XMind 思维导图的本地 MCP Server。图片直接交给多模态模型，不需要 OCR、不下载模型、不依赖 GPU。

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/xmind-mcp-server.svg)](https://pypi.org/project/xmind-mcp-server/)
[![Downloads](https://img.shields.io/pypi/dm/xmind-mcp-server.svg)](https://pypi.org/project/xmind-mcp-server/)

XMind MCP Server 基于 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)，把 XMind 文件解析、图片读取、节点编辑和文件写回封装成标准工具，供 Trae、Claude Desktop、Cursor 等支持 MCP 的客户端调用。

> 当前版本不包含 OCR。现在主流模型都支持图片输入，直接看图比 OCR 文本更准，也省掉了模型下载、GPU 检测和一堆运行时依赖。

***

## ✨ 核心亮点

### 1. 图片原图直给模型

`read_all` / `get_image` 把节点图片内联返回，模型看到的是原图：图表、截图、手写批注里的版面关系都能理解。长边默认压到 1024 并转 JPEG，控制 token 成本。

```text
📄 工作表: 操作系统
└─ [L0] [id: root-1] 操作系统
   └─ [L1] [id: abc-123] 📷图片     ← 图片作为 ImageContent 一起返回
```

图片默认内联；只想看结构时用 `image_mode=off`，或配置 `image_max_side` 控制清晰度与成本。
如果客户端模型不支持图片输入，请改用 `read_structure` 或 `read_all(image_mode="off")` 读文字结构。

`get_preview` 会返回 XMind 保存时生成的整图缩略图——这是 XMind 自己渲染的画面，比任何自绘示意都接近软件里的效果。文件没有缩略图时（例如本工具新建或改过的文件），它会用 matplotlib 现画一张近似图：主题框、文字、分支连线和配色都有，但不含边界、概要、关系线和图片内容。可以用 `source=render` 强制自绘、`sheet=N` 指定工作表。

### 2. 既能读，也能写

- 添加 / 修改 / 删除 / 移动 / 复制节点
- 修改标题、备注、标记、标签、超链接
- 保存回原文件或另存为新文件
- 批量替换标题、清理空节点、合并重复节点、自动编号

### 3. 安全写回，保留原文件结构

保存时不会破坏 XMind 文件中的样式、主题、附件、manifest 等资源，只修改 `content.json`，再安全重组 ZIP。**覆盖保存前会自动备份到 `~/.xmind-mcp/backups/`（每个文件保留最近 20 份）**。

写盘采用“临时文件 + 原子替换”：中途崩溃或磁盘写满时，原文件保持完整，不会留下半个打不开的 xmind。

保存时会移除过期的 `Thumbnails/thumbnail.png`（内容已变，旧缩略图会误导预览），XMind 下次打开保存时会重新生成。

写入结构用 XMind 官方的 [`@xmindltd/xmind-cli`](https://github.com/xmindltd/xmind-cli) 校验过：`create`、`import_markdown`、修改 XMind 原生文件后保存三类产物都是 0 error / 0 warning。与官方格式对齐的约定（非根主题不写 `class: "topic"`、每个 `style` 带 `id`、`markers` 为 `[{"markerId": ...}]`）由单元测试守住。

### 4. Markdown 公式自动渲染

从 Markdown 导入时，自动识别块级 `$$...$$` 数学公式，用 matplotlib 渲染成图片插入对应节点——**免费版 XMind 也能显示公式**，不再是一串乱码 LaTeX 文本。行内公式 `$...$` 保持为文本；mathtext 渲染不了的公式会原样保留，不报错不中断。

### 5. 工具组与路径限制

- `toolset`：只向客户端暴露指定工具组，减少 `tools/list` 的 schema 开销。
- `allowed_roots`：限制可读写的目录，防止模型访问任意路径。

***

## 🆚 与纯 Python 脚本对比

| 能力 | 纯 Python 脚本 | XMind MCP Server |
| --- | --- | --- |
| 读文本节点 | ✅ 可以 | ✅ 可以 |
| 找图片位置 | ⚠️ 容易漏（summary、attachment、notes 里的图） | ✅ 封装完整 |
| 理解图片内容 | ❌ 需要自己接图片 / OCR | ✅ 原图直给多模态模型 |
| 写回 xmind | ⚠️ 要手动处理 manifest checksum、ZIP 重组 | ✅ 封装安全写回 |
| 批量处理 | ⚠️ 自己写循环 | ✅ 统一接口 |
| 被大模型调用 | ❌ 需要额外包装 | ✅ MCP 标准协议 |

***

## 📦 安装

### 从 PyPI 安装（推荐）

```bash
pip install xmind-mcp-server
```

运行时依赖只有 `mcp`、`Pillow` 和 `matplotlib`（Markdown 公式与预览图）。没有 OCR 模型，没有 onnxruntime，不需要首次联网下载。

### 从源码安装

```bash
git clone https://github.com/GarryWhite109909/xmind-mcp-server.git
cd xmind-mcp-server
pip install -e ".[dev]"
```

***

## 🚀 快速开始

### 1. 启动

```bash
xmind-mcp
```

直接启动 MCP Server，没有首次配置流程。配置文件是可选的，不写也能用（全部使用默认值）。

### 2. 在 Trae / Claude Desktop / Cursor 中配置

#### Trae

打开设置 → MCP，手动添加：

```json
{
  "mcpServers": {
    "xmind-mcp": {
      "command": "xmind-mcp",
      "args": []
    }
  }
}
```

如果 `xmind-mcp` 不在系统 PATH，使用 Python 解释器绝对路径：

```json
{
  "mcpServers": {
    "xmind-mcp": {
      "command": "C:\\Users\\<你的用户名>\\.miniconda\\python.exe",
      "args": ["-m", "xmind_mcp"]
    }
  }
}
```

> 💡 修改代码后，需要在 Trae 中禁用再启用该 MCP，或重启 Trae，才能加载最新版本。

#### Claude Desktop

编辑 `claude_desktop_config.json`：

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "xmind-mcp": {
      "command": "xmind-mcp",
      "args": []
    }
  }
}
```

#### Cursor

在 MCP 设置中添加同样的 `mcpServers` 配置即可。

***

## 🛠️ 工具一览

共 55 个工具，可用 `toolset` 按组加载。

### 读取类

| 工具 | 说明 |
| --- | --- |
| `read_structure` | 读取节点树结构，默认输出层级 + 节点 ID |
| `read_all` | 节点树 + 图片 + notes/labels/markers/超链接/样式元数据，支持 `max_depth` / `max_nodes` / `image_mode` 防爆上下文 |
| `list_images` | 列出所有含图片的节点 |
| `get_image` | 读取节点原图并作为图片返回（多模态模型首选） |
| `get_preview` | 返回整图预览：优先 XMind 自带缩略图，没有就自绘近似图（`source`/`sheet` 可控） |
| `find_node` | 按标题关键词搜索节点，返回 ID 和路径 |
| `get_node` | 读取单个节点完整信息（含主题类型、节点自身样式、主题继承样式、超链接、父节点） |
| `analyze_file` | 文件统计/体检：节点数、深度、重复标题、空节点等 |
| `analyze_style` | 样式审计：主题层级默认样式、颜色/字体/形状使用统计、节点有效样式 |
| `list_sheets` | 列出所有工作表 |

### 结构编辑类

| 工具 | 说明 |
| --- | --- |
| `add_node` | 在指定父节点下添加子节点 |
| `add_nodes` | 批量添加多个子节点 |
| `update_node` | 更新节点标题 / 备注 |
| `delete_node` | 删除节点及其子节点 |
| `move_node` | 移动节点到新父节点下，可指定 `before_id` / `after_id` 插入位置 |
| `duplicate_node` | 复制节点（含整棵子树）到新父节点 |
| `save_file` | 保存修改，可覆盖或另存为 |
| `create_file` | 从零新建 XMind 文件（支持嵌套 JSON 结构） |
| `import_markdown` | 把传入的 Markdown 文本逐行原样转成 XMind（不概括、不精简） |
| `import_markdown_file` | 直接读取磁盘上的 .md 文件，逐行原样转成 XMind（表格、代码块、引用都保留） |
| `batch_update_titles` | 按关键词/正则批量替换标题 |
| `delete_empty_nodes` | 删除空标题节点（含子树） |
| `merge_duplicate_nodes` | 合并标题相同的兄弟节点（子节点并入后删除重复项） |
| `notes_to_children` | 备注逐行转为子节点 |
| `children_to_notes` | 子节点标题合并为备注 |
| `auto_number` | 自动编号（decimal / hierarchical） |
| `add_sheet` / `rename_sheet` | 新增 / 重命名工作表 |
| `delete_sheet` / `move_sheet` | 删除 / 排序工作表 |

### 美化 / 标记类

| 工具 | 说明 |
| --- | --- |
| `set_node_style` | 字体、字号、颜色、加粗/斜体、删除线/下划线、大小写变换、背景/边框色、线宽预设（极细~极粗）、填充/边框线型、形状（12 种）、重要程度（极其重要/重要/删去/默认）、对齐；颜色支持 #RRGGBBAA 和「4E0D58 60%」透明度写法 |
| `set_branch_style` | 分支连线类型（圆角折线/折线/直线/曲线/圆弧/手绘/圆角手绘）、终点样式（圆点/三角形/菱形/双箭头等）、粗细、颜色、线型 |
| `set_theme_style` | 主题级默认样式：中心主题/分支主题/第三层级主题/细分主题/自由主题 |
| `set_sheet_layout` | 切换工作表布局（思维导图/逻辑图/括号图/组织结构图/树形图/时间轴/鱼骨图/矩阵图/树型表格） |
| `apply_style_preset` | 一键套用配色预设（清新蓝 / 暖阳橙 / 莫兰迪绿，支持 preview 预览） |
| `apply_to_matching` | 按标题关键词批量设置样式、标记、标签（支持 preview 预览） |
| `set_hyperlink` | 给节点添加 / 移除超链接 |
| `add_image` | 给节点插入本地图片 |
| `add_summary` | 给连续子节点区间添加概要 |
| `add_boundary` | 给连续子节点区间添加边界 |
| `add_relationship` / `remove_relationship` | 添加 / 删除关系线 |
| `remove_boundary` | 删除边界 |
| `add_marker` / `remove_marker` | 添加 / 移除优先级、任务进度等图标 |
| `add_label` / `remove_label` | 添加 / 移除标签 |

结构、样式和批量编辑工具都支持 `auto_save=true`，设置后会自动保存，无需再手动调用 `save_file`。

### 导出 / 维护类

| 工具 | 说明 |
| --- | --- |
| `export_markdown` | 导出为 Markdown 大纲 |
| `export_text` | 导出为纯文本大纲 |
| `export_svg` | 导出节点树示意图 SVG（供人工预览） |
| `list_attachments` | 列出附件/图片资源，标注被引用与未引用 |
| `remove_unused_attachments` | 移除未被引用的附件/图片，减小文件体积 |
| `repair_file` | 修复打不开的 XMind 文件（补全必需组成部分；content.json 缺失/损坏时重建空白画布并保留附件，覆盖前自动备份） |
| `list_backups` / `restore_backup` | 列出 / 恢复自动备份（恢复前会再备份当前文件） |

***

## 💬 使用示例

### 例 1：让模型总结一份 XMind

> 请读取 `D:\docs\操作系统.xmind`，总结其中的核心概念，并看看里面的图片。

模型会调用 `read_all`：节点结构和图片原图一起返回，模型直接看图理解图表或截图。

### 例 2：搜索节点并编辑

> 在 `D:\docs\操作系统.xmind` 中找到“进程”相关节点，给它们都加上一条备注“需重点复习”。

模型会调用：

1. `find_node` 搜索“进程”
2. `update_node` 更新备注（可设置 `auto_save=true`）

### 例 3：导出为 Markdown

> 把 `D:\docs\操作系统.xmind` 导出成 Markdown 大纲，保留节点 ID。

模型调用 `export_markdown`，返回：

```markdown
# 操作系统

- 操作系统 `id:root-1`
  - 进程 `id:child-1`
  - 线程 `id:child-2`
```

### 例 4：让模型美化笔记

> 把 `D:\docs\操作系统.xmind` 里“重点”相关的节点标红加粗，并给“考试必考”的节点加优先级图标。

模型会调用：

1. `find_node` 找到“重点”相关节点
2. `set_node_style` 设置 `font_color=#FF0000`、`font_weight=bold`
3. `add_marker` 添加 `priority-1` 图标
4. `save_file` 保存

### 例 5：从 Markdown 新建一份思维导图

> 把下面这份大纲转成 `D:\docs\新笔记.xmind`：
>
> ```
> # Python 学习路线
> - 基础语法
>   - 变量与类型
> - 常用库
> ```

模型会调用 `import_markdown`，一步生成 XMind 文件。

### 例 6：批量美化 + 体检

> 分析 `D:\docs\操作系统.xmind`，把标题含“重点”的节点全部标红加粗并加优先级图标，然后告诉我有没有重复标题或空节点。

模型会调用：

1. `analyze_file` 先体检
2. `apply_to_matching` 批量设置样式和标记（可 `auto_save=true`）
3. 保存

### 例 7：结构化视觉元素

> 把 `D:\docs\操作系统.xmind` 的“进程”和“线程”加一个概要，给“重点章节”加边界，并在“进程”和“内存”之间画一条关系线，最后套用“清新蓝”配色。

模型会调用 `add_summary`、`add_boundary`、`add_relationship`、`apply_style_preset`，这些都是 XMind 原生视觉元素，可以在 XMind 里继续调整。

### 例 8：导入带公式的 Markdown

> 把一份含数学公式的 Markdown 笔记转成 `D:\docs\高数.xmind`。

```markdown
# 高数笔记

质能方程：

$$
E = mc^2
$$

**牛顿第二定律**
$$
F = ma
$$
```

模型调用 `import_markdown` 时，会自动识别块级 `$$...$$` 公式，用 matplotlib 渲染成图片并插入对应节点（免费版 XMind 也能显示）。行内公式 `$...$` 保持为文本；mathtext 渲染不了的公式会原样保留不报错。

### 例 9：看图改图

> 先看一眼 `D:\docs\操作系统.xmind` 现在的样子，然后把颜色改柔和一点。

模型会调用 `get_preview` 拿到整图判断当前配色和版式（优先用 XMind 保存的缩略图，没有时自绘一张），再用 `analyze_style` 看具体色值，最后 `set_theme_style` / `apply_style_preset` 调整并保存。保存会移除旧缩略图，避免预览显示过期画面。

### 例 10：把截图/照片转成导图

> 这是我拍的板书，帮我整理成思维导图。

模型先 `get_image` 读取图片（多模态直接看），再按识别出的层级调用 `create_file` 落盘。

***

## ⚙️ 命令行参数

```bash
xmind-mcp                    # 启动 MCP Server
xmind-mcp --version          # 打印版本号
xmind-mcp --info             # 打印当前配置
xmind-mcp --toolset read     # 只加载读工具（可逗号组合，如 read,export）
xmind-mcp --uninstall        # 清理配置与旧版 OCR 残留
xmind-mcp --uninstall --yes  # 全自动卸载（含本包，跳过所有确认）
```

### 工具组与路径限制

`~/.xmind-mcp/config.json` 里有两个可选配置：

```json
{
  "image_mode": "inline",
  "image_max_side": 1024,
  "toolset": "read",
  "allowed_roots": ["D:\\docs"]
}
```

- **`image_mode`**：`inline`（默认，把原图交给多模态模型）/ `off`（只给节点结构，不返回图片）。
- **`image_max_side`**：内联图片的长边上限，默认 1024。
- **`toolset`**：只向客户端暴露指定工具组，减少 `tools/list` 的 schema 开销。
  可选 `all`（默认）/ `read` / `edit` / `style` / `export` / `maintenance`，支持逗号组合。
  全量 55 个工具的 schema 约 39KB；只加载 `read` 组约 6.4KB。
  也可以用 `xmind-mcp --toolset read` 临时覆盖。
- **`allowed_roots`**：限制可读写的目录，防止模型访问任意路径。
  留空表示不限制；配置后，所有 `*_path` 参数必须落在这些目录内，越界会直接返回错误并提示如何放行。

### 一键卸载

```bash
xmind-mcp --uninstall
xmind-mcp --uninstall --yes   # 全自动：连本包一起卸载，不询问
```

卸载流程：

1. 检测旧版（0.4 及以前）安装的 OCR / 运行时依赖，确认后再卸载（这些是通用库，可能被其他项目使用）；
2. 停止正在运行的 XMind MCP Server（交互模式下先询问）；
3. 删除配置文件 `~/.xmind-mcp/config.json`；**自动备份目录 `backups/` 永远保留**；
4. 检测旧版 OCR 模型缓存（`~/.cache/rapidocr`、`~/.cache/rapidocr_onnxruntime`、`~/.paddleocr`），确认后再删除；
5. 询问（或 `--yes` 直接执行）是否同时卸载本包 `xmind-mcp-server` 及其直接依赖 `mcp`、`Pillow`、`matplotlib`。

非交互环境（无终端输入）默认保留依赖和本包，只删除配置文件。

***

## 📁 本地文件位置

| 文件/目录 | 说明 |
| --- | --- |
| `~/.xmind-mcp/config.json` | 可选配置（图片模式、工具组、允许目录） |
| `~/.xmind-mcp/backups/` | 覆盖保存前的自动备份（每个文件保留最近 20 份） |

***

## ✅ 支持的 XMind 版本

- ✅ XMind Zen / XMind 2020+ / 2022 / 2024 / 2026（JSON 格式）
- ❌ XMind 8 及更早（XML 格式，暂不支持）

***

## 🧪 开发 & 测试

```bash
git clone https://github.com/GarryWhite109909/xmind-mcp-server.git
cd xmind-mcp-server
pip install -e ".[dev]"
ruff check src tests
pytest
```

当前测试覆盖：文件解析、节点增删改查、图片内联与预览、样式与视觉元素、文件写回、MCP Server 协议握手、Markdown 导入导出等。

***

## 📄 License

MIT © GarryWhite
