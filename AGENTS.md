# XMind MCP Server — 项目规则

本地 MCP Server：解析 / 编辑 / 写回 XMind（JSON 格式），把节点图片作为
ImageContent 直接交给多模态模型。**不包含 OCR，也不要重新引入 OCR、
模型推理、GPU 检测或启动时 pip 安装。**

## 常用命令

```bash
# 创建环境并安装（开发）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# .venv/bin/python -m pip install -e ".[dev]"         # POSIX

# 跑测试（已有 .venv 时）
.venv/Scripts/python.exe -m pytest -q
# .venv/bin/python -m pytest -q

# 静态检查
.venv/Scripts/ruff.exe check src tests
# ruff check src tests

# 启动（stdio，给 MCP 客户端用）
xmind-mcp
xmind-mcp --info
xmind-mcp --toolset read

# 可选：用 XMind 官方 CLI 校验生成的结构（需要先登录）
xmind validate path/to/file.xmind
```

发布前至少跑：`ruff check src tests` 和 `pytest -q`。测试不允许联网、不允许依赖真实 XMind 安装。

## 目录地图

| 路径 | 职责 |
| --- | --- |
| `src/xmind_mcp/tool_schemas.py` | 工具 schema、分组（TOOLSETS）、只读/破坏性标注 |
| `src/xmind_mcp/tools.py` | 工具 handler、文件缓存、树格式化、SVG 导出 |
| `src/xmind_mcp/markdown_import.py` | Markdown → XMind 大纲解析与公式注入 |
| `src/xmind_mcp/xmind_parser.py` | 解析 zip / content.json，节点树与 ID 索引 |
| `src/xmind_mcp/xmind_writer.py` | 修改 raw_content、保存（备份 / 冲突检测 / 原子写） |
| `src/xmind_mcp/xmind_props.py` | 中文样式名 → XMind 内部值的映射与校验 |
| `src/xmind_mcp/render.py` | 自绘 PNG 预览 |
| `src/xmind_mcp/formula.py` | Markdown 块级公式 → PNG |
| `src/xmind_mcp/image_payload.py` | 图片缩放 / JPEG / base64 负载 |
| `src/xmind_mcp/config.py` | `~/.xmind-mcp/config.json` |
| `src/xmind_mcp/cli.py` | 启动、`--info`、`--uninstall` |
| `src/xmind_mcp/server.py` | MCP stdio 握手与 tools/list、tools/call |
| `tests/` | pytest；`test_golden.py` 守住官方文件结构 |

## 硬约定

1. **所有写操作走 `XMindWriter`。** 不要直接重写 zip；`save()` 负责备份、
   跨进程冲突检测、清理过期缩略图、原子替换。
2. **改完 `raw_content` 必须调用 `xm.refresh_from_raw()`。** 节点树和
   `find_topic` 的 ID 索引都由它重建；漏调用会让后续查询看到旧树。
3. **保存前不要绕过冲突检测。** 覆盖原文件时若磁盘摘要与
   `xm.source_digest` 不同，必须抛 `FileChangedError`，让调用方另存。
4. **工具要在五处登记：** `tool_schemas.py` 的 `build_tool_list()`、`TOOLSETS`、
   `_READ_ONLY_TOOLS`（只读工具）、`_DESTRUCTIVE_TOOLS`（会删/覆盖的工具），
   以及 `tools.py` 的 `XMindTools._handlers`。
   `test_every_listed_tool_has_handler` 会抓漂移；工具总数变化时同步
   `test_server.py` 和 README。
5. **路径参数一律以 `_path` 结尾。** `_guard_paths` 只校验这个后缀；
   新工具如果叫 `file` / `target` 会绕过 `allowed_roots`。
6. **图片只走 `types.ImageContent`。** 统一用 `prepare_image()` 缩放、转 JPEG、
   控制体积；不要在工具里直接返回原始 base64。
7. **版本号两处同步：** `pyproject.toml` 与 `src/xmind_mcp/__init__.py`。
8. **配置向后兼容：** `Config.load()` 忽略不认识的字段，旧的
   `image_mode=ocr/both` 归一化为 `inline`；不要因为旧配置报错。
9. **用户数据默认保留：** 备份目录 `~/.xmind-mcp/backups/` 不随卸载删除；
   删除/覆盖用户文件前先备份，并在返回文案里说明。
10. **对外文案用中文，标识符用英文。** 工具描述要告诉模型“什么时候用、
    参数收什么”，不要写营销句。

## 代码风格

- Python 3.10+，`from __future__ import annotations`，类型标注优先。
- 遵循现有风格：模块级常量、`_private` 辅助函数、少量依赖注入。
- 不做无请求的重构；但遇到重复的树遍历 / 格式化 / 保存逻辑应合并。
- 新逻辑先写能失败的测试，再实现；测试用 `tmp_path`，不写用户主目录。
- 不 mock 掉被验证的核心行为（解析、写回、官方结构规则）。

## 反模式

- ❌ 启动时 `pip install`、探测 GPU、下载模型（旧版 OCR 流程）。
- ❌ `zipfile.ZipFile(output_path, "w")` 直接写目标文件（崩溃会毁文件）。
- ❌ 每次 `find_topic` 全树遍历（已用 `_topic_index`，别再退化）。
- ❌ 在 `tools.py` 里再写一份树格式化 / 备注缩进逻辑。
- ❌ 改动 JSON 结构后不跑 `test_golden.py` / 官方 `xmind validate`。
- ❌ 把 `class: "topic"` 写到非根主题、给 `style` 省略 `id`、
  `markers` 写成字符串数组——这三条会被官方校验拒绝。

## 完成标准

1. `ruff check src tests` 与 `pytest -q` 全绿。
2. 新增/修改的 MCP 工具在 `tools/list` 里可见，read-only / destructive
   标注正确。
3. 生成或修改的 `.xmind` 能被 XMind 打开；结构类改动最好过一遍官方
   `xmind validate`。
4. 交付说明写清：改了什么、怎么验证、哪些没验证。
