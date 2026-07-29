# xmind-mcp-server

让大模型（LLM）能真正**理解和操作 XMind 文件**的 MCP Server。

## 为什么需要这个工具

普通 MCP 只能让模型看到 XMind 的节点文字，但：

- ❌ 节点里的**图片内容看不到**
- ❌ 模型**不能修改** XMind 文件

本工具解决这两个问题：

- ✅ 自动 OCR 识别节点图片中的文字，喂给模型
- ✅ 支持增删改节点、保存回 `.xmind` 文件

## 功能

### 读取
- `read_structure` — 读取节点树（带层级、备注、图片标记）
- `list_images` — 列出所有含图片的节点
- `read_image_ocr` — 对指定节点的图片做 OCR
- `read_all` — 一步到位：节点树 + 所有图片 OCR

### 编辑
- `add_node` — 添加子节点
- `update_node` — 更新节点标题/备注
- `delete_node` — 删除节点
- `move_node` — 移动节点
- `save_file` — 保存修改

### 系统
- `get_hardware_info` — 查看硬件检测和 OCR 配置

## 快速开始

### 安装

```bash
pip install xmind-mcp-server
```

### 首次运行（自动检测硬件 + 安装 OCR 依赖）

```bash
xmind-mcp
```

首次运行会自动：
1. 检测 GPU（NVIDIA / AMD / Intel）、CPU、内存
2. 根据硬件选择最佳 OCR 后端和推理设备
3. 安装对应的依赖包
4. 写入配置到 `~/.xmind-mcp/config.json`

第二次运行直接启动，无需重复安装。

### 在 Trae / Claude Desktop 中配置

在 MCP 配置文件中添加：

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

## 硬件支持

| 显卡 | Windows | Linux | macOS |
|------|---------|-------|-------|
| NVIDIA | CUDA ✅ | CUDA ✅ | CPU |
| AMD | DirectML ✅ | CPU | CPU |
| Intel | DirectML ✅ | CPU | CPU |
| 无独显 | CPU ✅ | CPU ✅ | CPU ✅ |

### OCR 引擎

| 引擎 | 特点 | GPU 加速 |
|------|------|----------|
| RapidOCR（默认） | 轻量、跨平台、DirectML 支持全显卡 | NVIDIA / AMD / Intel |
| PaddleOCR | 生态成熟、模型丰富 | 仅 NVIDIA |

切换引擎：

```bash
xmind-mcp --setup --engine paddleocr
```

## 命令行参数

```
xmind-mcp                    启动 MCP Server（首次自动配置）
xmind-mcp --setup            重新检测硬件并配置
xmind-mcp --info             打印当前硬件和配置信息
xmind-mcp --engine rapidocr  指定 OCR 引擎
xmind-mcp --device cuda      强制指定推理设备
```

## 支持的 XMind 版本

- ✅ XMind Zen / XMind 2020+ / 2022 / 2024（JSON 格式）
- ❌ XMind 8 及更早（XML 格式，暂不支持）

## 开发

```bash
git clone https://github.com/GarryWhite109909/xmind-mcp-server.git
cd xmind-mcp-server
pip install -e ".[dev]"
pytest
```

## License

MIT
