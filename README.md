# XMind MCP Server

> 让大模型真正“看懂”并“修改” XMind 思维导图的本地 MCP Server。

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/xmind-mcp-server.svg)](https://pypi.org/project/xmind-mcp-server/)
[![Downloads](https://img.shields.io/pypi/dm/xmind-mcp-server.svg)](https://pypi.org/project/xmind-mcp-server/)

XMind MCP Server 是一个基于 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 的本地服务。它把 XMind 文件解析、图片 OCR、节点编辑和文件写回能力封装成一套标准工具，供 Trae、Claude Desktop、Cursor 等支持 MCP 的客户端调用。

---

## ✨ 核心亮点

### 1. 不只是读文字，还能读图片

普通方式只能让模型看到节点的标题文字。XMind MCP Server 会对节点中的图片进行本地 OCR，把图片里的文字也喂给模型，真正“看懂”整份思维导图。

```text
📄 工作表: 操作系统
└─ [L0] [id: root-1] 操作系统
   └─ [L1] [id: abc-123] 📷图片
         ↳ [OCR] 中央处理器
                (CPU)
```

### 2. 既能读，也能写

支持通过自然语言指令让模型直接修改 XMind 文件：

- 添加子节点
- 修改标题 / 备注
- 删除节点
- 移动节点
- 保存回原文件或另存为新文件

### 3. 本地 OCR，零云端依赖

内置 RapidOCR（默认）和 PaddleOCR 两种后端，全部在本地运行：

- 不调用任何云端多模态 API
- 不消耗 Token
- 图片内容识别不上传到第三方

### 4. 自动硬件检测与最佳配置

首次启动自动检测 GPU、CPU、内存，并选择最适合的推理后端：

| 显卡 | Windows | Linux | macOS |
|------|---------|-------|-------|
| NVIDIA | CUDA ✅ | CUDA ✅ | CPU |
| AMD | DirectML ✅ | CPU | CPU |
| Intel | DirectML ✅ | CPU | CPU |
| 无独显 | CPU ✅ | CPU ✅ | CPU ✅ |

### 5. 安全写回，保留原文件结构

保存时不会破坏 XMind 文件中的样式、主题、附件、manifest 等资源，只修改 `content.json`，再安全重组 ZIP。

---

## 🆚 与纯 Python 脚本对比

| 能力 | 纯 Python 脚本 | XMind MCP Server |
|------|---------------|------------------|
| 读文本节点 | ✅ 可以 | ✅ 可以 |
| 找图片位置 | ⚠️ 容易漏（summary、attachment、notes 里的图） | ✅ 封装完整 |
| 理解图片内容 | ❌ 必须依赖多模态模型 | ✅ 本地 OCR，不依赖模型能力 |
| 写回 xmind | ⚠️ 要手动处理 manifest checksum、ZIP 重组 | ✅ 封装安全写回 |
| 批量处理 | ⚠️ 自己写循环 | ✅ 统一接口 |
| 被大模型调用 | ❌ 需要额外包装 | ✅ MCP 标准协议 |

---

## 📦 安装

### 从 PyPI 安装（推荐）

```bash
pip install xmind-mcp-server
```

带 RapidOCR 依赖：

```bash
pip install "xmind-mcp-server[rapidocr]"
```

### 从源码安装

```bash
git clone https://github.com/GarryWhite109909/xmind-mcp-server.git
cd xmind-mcp-server
pip install -e ".[dev]"
```

---

## 🚀 快速开始

### 1. 首次启动（自动配置）

```bash
xmind-mcp
```

首次运行会：

1. 检测操作系统、CPU、内存、显卡
2. 选择合适的 OCR 引擎和推理设备
3. 安装对应的推理依赖（如 onnxruntime-gpu / directml）
4. 写入配置到 `~/.xmind-mcp/config.json`

第二次运行直接启动，无需重复配置。

### 2. 在 Trae / Claude Desktop / Cursor 中配置

#### Trae

打开设置 → MCP，添加服务器：

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

---

## 🛠️ 工具一览

### 读取类

| 工具 | 说明 |
|------|------|
| `read_structure` | 读取节点树结构，默认输出层级 + 节点 ID |
| `read_all` | 节点树 + 图片 OCR + notes/labels/markers 元数据 |
| `list_images` | 列出所有含图片的节点 |
| `read_image_ocr` | 对指定节点的图片做 OCR |
| `find_node` | 按标题关键词搜索节点，返回 ID 和路径 |
| `export_markdown` | 导出为 Markdown 大纲 |
| `export_text` | 导出为纯文本大纲 |

### 结构编辑类

| 工具 | 说明 |
|------|------|
| `add_node` | 在指定父节点下添加子节点 |
| `add_nodes` | 批量添加多个子节点 |
| `update_node` | 更新节点标题 / 备注 |
| `delete_node` | 删除节点及其子节点 |
| `move_node` | 移动节点到新父节点下 |
| `duplicate_node` | 复制节点（含整棵子树）到新父节点 |
| `save_file` | 保存修改，可覆盖或另存为 |

### 美化 / 标记类（让笔记更美观易读）

| 工具 | 说明 |
|------|------|
| `set_node_style` | 设置字体、字号、颜色、加粗、斜体、背景色、边框、对齐 |
| `set_hyperlink` | 给节点添加 / 移除超链接 |
| `add_marker` / `remove_marker` | 添加 / 移除优先级、任务进度等图标 |
| `add_label` / `remove_label` | 添加 / 移除标签 |

所有编辑工具都支持 `auto_save=true`，设置后会自动保存，无需再手动调用 `save_file`。

### 系统类

| 工具 | 说明 |
|------|------|
| `get_hardware_info` | 查看硬件检测和 OCR 配置 |

---

## 💬 使用示例

### 例 1：让模型总结一份 XMind

> 请读取 `D:\\docs\\操作系统.xmind`，总结其中的核心概念，并识别所有图片里的文字。

模型会调用：

1. `read_all` 读取完整结构和图片 OCR
2. 基于返回内容生成总结

### 例 2：搜索节点并编辑

> 在 `D:\\docs\\操作系统.xmind` 中找到“进程”相关节点，给它们都加上一条备注“需重点复习”。

模型会调用：

1. `find_node` 搜索“进程”
2. `update_node` 更新备注（可设置 `auto_save=true`）

### 例 3：导出为 Markdown

> 把 `D:\\docs\\操作系统.xmind` 导出成 Markdown 大纲，保留节点 ID。

模型会调用 `export_markdown`，返回：

```markdown
# 操作系统

- 操作系统 `id:root-1`
  - 进程 `id:child-1`
  - 线程 `id:child-2`
```

### 例 4：让模型美化笔记

> 把 `D:\\docs\\操作系统.xmind` 里"重点"相关的节点标红加粗，并给"考试必考"的节点加优先级图标。

模型会调用：

1. `find_node` 找到"重点"相关节点
2. `set_node_style` 设置 `font_color=#FF0000`、`font_weight=bold`
3. `add_marker` 添加 `priority-1` 图标
4. `save_file` 保存

---

## ⚙️ 命令行参数

```bash
xmind-mcp                    # 启动 MCP Server（首次自动配置）
xmind-mcp --setup            # 重新检测硬件并配置
xmind-mcp --info             # 打印当前硬件和配置信息
xmind-mcp --engine rapidocr  # 指定 OCR 引擎
xmind-mcp --device cuda      # 强制指定推理设备
xmind-mcp --uninstall        # 一键卸载（依赖 + 模型 + 配置 + 本体）
```

切换 OCR 引擎：

```bash
xmind-mcp --setup --engine paddleocr
```

一键卸载：

```bash
xmind-mcp --uninstall
```

会依次卸载已安装的 OCR 依赖、删除模型缓存和配置文件，并询问是否同时卸载本包 `xmind-mcp-server`。

---

## 📁 本地文件位置

| 文件/目录 | 说明 |
|----------|------|
| `~/.xmind-mcp/config.json` | 硬件检测结果、OCR 引擎、推理设备配置 |
| `~/.xmind-mcp/ocr_cache.db` | OCR 结果缓存，避免同一张图重复识别 |
| `~/.xmind-mcp/models/` | OCR 模型下载缓存 |

> MCP 关闭时会自动释放 OCR 模型占用的内存 / 显存。

---

## ✅ 支持的 XMind 版本

- ✅ XMind Zen / XMind 2020+ / 2022 / 2024（JSON 格式）
- ❌ XMind 8 及更早（XML 格式，暂不支持）

---

## 🧪 开发 & 测试

```bash
git clone https://github.com/GarryWhite109909/xmind-mcp-server.git
cd xmind-mcp-server
pip install -e ".[dev]"
pytest
```

当前测试覆盖：文件解析、节点增删改查、图片加载、OCR、MCP Server 协议握手、Markdown 导出等。

---

## 📄 License

MIT © GarryWhite
