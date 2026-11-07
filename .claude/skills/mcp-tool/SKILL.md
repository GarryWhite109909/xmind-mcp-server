---
name: mcp-tool
description: 新增、删除或重命名本项目 MCP 工具时的完整改动清单。涉及 build_tool_list、TOOLSETS、handler、read-only/destructive 标注、测试与 README 时使用。
---

# 改动 MCP 工具

工具分散在 5 个地方，漏一处就会出现“schema 里有、调用报未知工具”或
“工具组统计不对”。一次改完，再跑测试。

## 清单

1. **schema**：`tool_schemas.py` 的 `build_tool_list()` 里加/删 `_tool(...)`。
   - 描述写清楚触发场景和参数含义，不要写营销句。
   - `required` 只放真正必需的参数。
2. **工具组**：`tool_schemas.py` 的 `TOOLSETS` 对应组里加/删名字。
   `read` / `edit` / `style` / `export` / `maintenance` 必须覆盖全部工具且不重叠。
3. **handler**：`XMindTools._handlers` 里注册同名方法。
   方法签名统一 `async def _name(self, args: dict)`。
4. **只读标注**：不改文件、不写盘的加进 `tool_schemas.py` 的 `_READ_ONLY_TOOLS`。
   只读工具并发执行不加锁；写工具由 `XMindTools._write_lock` 串行。
5. **破坏性标注**：会删除/覆盖内容的加进 `_DESTRUCTIVE_TOOLS`。

改完同步：

- `tests/test_server.py` 里的工具总数（当前 55）。
- `README.md` 的工具表和总数。
- 如果有新参数，确认以 `_path` 结尾的路径参数会被 `allowed_roots` 校验。

## 测试

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check src tests
.venv/Scripts/python.exe -m pytest -q tests/test_tools.py -k toolset
```

现有守卫：

- `test_every_listed_tool_has_handler`：schema 与 handler 必须一一对应。
- `test_toolsets_cover_every_tool_exactly_once`：分组覆盖全部且不重叠。
- `test_read_only_flags_match_read_tools`：`read` 组必须是只读。
- `test_build_tool_list_filters_by_toolset`：过滤与非法组名行为。

新工具至少补一条行为测试：成功路径 + 典型失败（找不到节点、参数非法、
路径越界）。

## 设计约定

- 返回值可以是 `str`，也可以是 `list[TextContent | ImageContent]`。
  图片必须用 `_image_block(prepared)`，不要手写 base64。
- 编辑类工具统一支持 `auto_save`，用 `self._auto_save_msg(...)` 收尾。
- 错误不要抛给客户端裸异常：handler 内返回 `❌ 原因`，或让
  `call_tool_status` 的异常分支统一包装。
- 参数校验先全部做完再写数据，保证失败时没有部分修改
  （参考 `_prepare_style_updates` 的原子性）。
- 工具名用 `snake_case` 动词短语；已有命名风格优先，不要造同义词。

## 删除工具

删除是破坏性变更：

- 同步删 schema、分组、handler、read-only/destructive 集合、README、测试计数。
- 在 README / 变更说明里写清楚替代工具（例如 OCR 工具已移除，
  图片改用 `get_image` / `read_all`）。
- 旧客户端仍可能传旧参数：能兼容就归一化（如 `image_mode=ocr` → `inline`），
  不要直接 500。
