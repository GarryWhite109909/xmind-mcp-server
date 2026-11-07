"""MCP 工具 schema 与分组定义。

build_tool_list() 是工具 schema 的唯一事实来源；新增/删除/重命名工具的
完整改动清单见 .claude/skills/mcp-tool/SKILL.md。
"""

from __future__ import annotations

from typing import Optional

from mcp import types


# ---------- 工具定义 ----------

# 工具分组：客户端只需要读时，可以只加载其中一组，省掉无关工具的 schema 开销
TOOLSETS: dict[str, set[str]] = {
    "read": {
        "read_structure", "read_all", "list_images",
        "get_image", "get_preview", "find_node", "get_node",
        "analyze_file", "analyze_style", "list_sheets",
    },
    "edit": {
        "add_node", "add_nodes", "update_node", "delete_node", "move_node",
        "duplicate_node", "create_file", "save_file",
        "import_markdown", "import_markdown_file",
        "batch_update_titles", "delete_empty_nodes", "merge_duplicate_nodes",
        "notes_to_children", "children_to_notes", "auto_number",
        "add_sheet", "rename_sheet", "delete_sheet", "move_sheet",
    },
    "style": {
        "set_node_style", "set_branch_style", "set_theme_style",
        "set_sheet_layout", "apply_style_preset", "apply_to_matching",
        "set_hyperlink", "add_image", "add_summary", "add_boundary",
        "add_relationship", "remove_relationship", "remove_boundary",
        "add_marker", "remove_marker", "add_label", "remove_label",
    },
    "export": {"export_markdown", "export_text", "export_svg"},
    "maintenance": {
        "repair_file", "list_backups", "restore_backup",
        "remove_unused_attachments", "list_attachments",
    },
}


def resolve_toolset(spec: str) -> Optional[set[str]]:
    """把 "read,style" 解析成工具名集合。

    all / 空 / 全部无法识别 都返回 None，表示不限制——配置写错时给全量工具，
    总比一个工具都没有强。
    """
    names = [part.strip().lower() for part in str(spec or "").split(",") if part.strip()]
    if not names or "all" in names:
        return None
    selected: set[str] = set()
    for name in names:
        selected |= TOOLSETS.get(name, set())
    return selected or None


# 只读工具：不改文件、不写盘，客户端可以放心自动批准
_READ_ONLY_TOOLS = {
    "read_structure", "read_all", "list_images",
    "get_image", "get_preview", "find_node", "get_node",
    "analyze_file", "analyze_style", "export_markdown", "export_text",
    "list_sheets", "list_attachments", "list_backups",
}

# 会删除或覆盖既有内容的工具，客户端应向用户确认
_DESTRUCTIVE_TOOLS = {
    "delete_node", "delete_sheet",
    "remove_marker", "remove_label", "remove_boundary", "remove_relationship",
    "delete_empty_nodes", "merge_duplicate_nodes", "remove_unused_attachments",
    "batch_update_titles", "restore_backup", "repair_file",
    "save_file", "create_file",
}


def _tool(name: str, description: str, properties: dict, required: list[str] = None) -> types.Tool:
    read_only = name in _READ_ONLY_TOOLS
    return types.Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
        annotations=types.ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=None if read_only else name in _DESTRUCTIVE_TOOLS,
            open_world_hint=False,
        ),
    )


def build_tool_list(toolset: str = "all") -> list[types.Tool]:
    file_path_prop = {"type": "string", "description": ".xmind 文件路径"}
    md_path_prop = {"type": "string", "description": "要转换的 Markdown 文件路径（.md）"}
    output_xmind_prop = {"type": "string", "description": "生成的 .xmind 文件路径"}

    tools = [
        _tool(
            "read_structure",
            "读取 XMind 文件的节点树结构，返回带层级和节点 ID 的文本。支持 max_depth/max_nodes 防止大文件撑爆上下文。",
            {
                "file_path": file_path_prop,
                "include_ids": {"type": "boolean", "description": "是否在每行后输出节点 ID（默认 true）"},
                "clean": {"type": "boolean", "description": "是否去掉 📷/📝/🔖 等内部标记（默认 false）"},
                "max_depth": {"type": "integer", "description": "只输出到该层级（可选，根主题为 L0）"},
                "max_nodes": {"type": "integer", "description": "最多输出多少个节点，超出截断（可选）"},
            },
            ["file_path"],
        ),
        _tool(
            "read_all",
            "一步到位：读取节点树 + 图片 + notes/labels/markers/超链接/样式元数据。默认把每个图片节点的原图作为图片一起返回，交给多模态模型直接看。",
            {
                "file_path": file_path_prop,
                "include_ids": {"type": "boolean", "description": "是否在每行后输出节点 ID（默认 true）"},
                "clean": {"type": "boolean", "description": "是否去掉 📷/📝/🔖 等内部标记（默认 false）"},
                "include_style": {"type": "boolean", "description": "是否输出每个节点的样式属性（默认 false）"},
                "max_depth": {"type": "integer", "description": "只输出到该层级（可选，根主题为 L0）"},
                "max_nodes": {"type": "integer", "description": "最多输出多少个节点，超出截断（可选）"},
                "image_mode": {
                    "type": "string",
                    "enum": ["auto", "inline", "off"],
                    "description": "图片处理方式：inline=返回原图（默认），off=不返回图片，只给节点结构",
                },
                "max_images": {"type": "integer", "description": "最多内联多少张图（默认 8）"},
                "image_max_side": {"type": "integer", "description": "内联图片长边上限（默认 1024）"},
            },
            ["file_path"],
        ),
        _tool(
            "list_images",
            "列出 XMind 文件中所有包含图片的节点（ID + 标题 + 图片引用）。",
            {"file_path": file_path_prop},
            ["file_path"],
        ),
        _tool(
            "get_image",
            "读取节点的图片本身并作为图片返回，多模态模型可直接看图（图表、截图、手写内容都能理解）。默认把长边压到 1024 并转 JPEG，控制 token 成本。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "包含图片的节点 ID"},
                "max_side": {"type": "integer", "description": "长边像素上限（默认 1024）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "get_preview",
            "返回整图预览，让模型先看一眼整体版式和配色。优先用 XMind 保存时生成的缩略图（最接近软件里的真实效果）；没有缩略图时自动用 matplotlib 自绘一张近似图。",
            {
                "file_path": file_path_prop,
                "max_side": {"type": "integer", "description": "长边像素上限（默认 1024）"},
                "source": {
                    "type": "string",
                    "enum": ["auto", "embedded", "render"],
                    "description": "auto=有缩略图用缩略图、没有就自绘（默认）；embedded=只要 XMind 缩略图；render=总是自绘",
                },
                "sheet": {
                    "type": "integer",
                    "description": "要自绘的工作表索引，从 0 开始（默认 0；缩略图模式下忽略）",
                },
            },
            ["file_path"],
        ),
        _tool(
            "find_node",
            "按标题关键词搜索节点，返回节点 ID、标题和层级路径，方便编辑工具使用。",
            {
                "file_path": file_path_prop,
                "keyword": {"type": "string", "description": "标题关键词（不区分大小写）"},
            },
            ["file_path", "keyword"],
        ),
        _tool(
            "get_node",
            "读取单个节点的完整信息：标题、层级、类型、父节点、路径、备注、标记、标签、超链接、图片、样式、子节点。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "节点 ID"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "analyze_file",
            "对 XMind 文件做统计和体检：节点数、最大深度、备注/图片/标记统计、重复标题、空标题、特殊节点。",
            {"file_path": file_path_prop},
            ["file_path"],
        ),
        _tool(
            "add_node",
            "在指定父节点下添加子节点，可用 before_id/after_id 指定插入到某个兄弟节点前后。",
            {
                "file_path": file_path_prop,
                "parent_id": {"type": "string", "description": "父节点 ID"},
                "title": {"type": "string", "description": "新节点标题"},
                "notes": {"type": "string", "description": "节点备注（可选）"},
                "before_id": {"type": "string", "description": "插到该兄弟节点之前（可选）"},
                "after_id": {"type": "string", "description": "插到该兄弟节点之后（可选）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "parent_id", "title"],
        ),
        _tool(
            "update_node",
            "更新节点属性。传入 None 或省略的字段不会被修改。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要更新的节点 ID"},
                "title": {"type": "string", "description": "新标题（可选）"},
                "notes": {"type": "string", "description": "新备注（可选）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "delete_node",
            "删除节点及其所有子节点。不能删除根主题。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要删除的节点 ID"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "move_node",
            "把节点移动到新的父节点下，可用 before_id/after_id 指定插入位置。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要移动的节点 ID"},
                "new_parent_id": {"type": "string", "description": "新父节点 ID"},
                "before_id": {"type": "string", "description": "插到该兄弟节点之前（可选）"},
                "after_id": {"type": "string", "description": "插到该兄弟节点之后（可选）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id", "new_parent_id"],
        ),
        _tool(
            "save_file",
            "保存对 XMind 文件的修改。可另存为新文件或覆盖原文件。示例: output_path='D:\\docs\\output.xmind'",
            {
                "file_path": {"type": "string", "description": "当前 .xmind 文件路径"},
                "output_path": {"type": "string", "description": "另存为的路径（可选，省略则覆盖原文件）"},
            },
            ["file_path"],
        ),
        _tool(
            "repair_file",
            "修复打不开的 XMind 文件：自动补全 metadata.json、content.xml、默认主题、revisionId 等 XMind 必需内容；若 content.json 缺失或损坏，会重建空白画布并保留 zip 内的附件资源。覆盖保存前会自动备份原文件。",
            {
                "file_path": file_path_prop,
                "output_path": {"type": "string", "description": "修复后另存的路径（可选，省略则覆盖原文件）"},
            },
            ["file_path"],
        ),
        _tool(
            "export_markdown",
            "把 XMind 节点树导出为 Markdown 大纲。",
            {
                "file_path": file_path_prop,
                "include_ids": {"type": "boolean", "description": "是否在标题后附加 ID（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "create_file",
            "从零新建一个 XMind 文件。topics 为嵌套结构：[{\"title\": \"...\", \"notes\": \"...\", \"children\": [...]}]。",
            {
                "file_path": file_path_prop,
                "root_title": {"type": "string", "description": "根主题标题"},
                "sheet_title": {"type": "string", "description": "工作表标题（可选，默认 画布 1）"},
                "topics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "notes": {"type": "string"},
                            "children": {"type": "array", "items": {"type": "object"}},
                        },
                    },
                    "description": "根主题下的子节点（可选）",
                },
            },
            ["file_path", "root_title"],
        ),
        _tool(
            "import_markdown",
            "把传入的 Markdown 文本逐行原样转成新的 XMind 文件：所有文字（标题、列表、表格、代码块、引用）都会保留，不做概括或精简。必须把完整原文传给 markdown 参数；如果 Markdown 在磁盘上，请改用 import_markdown_file。",
            {
                "file_path": file_path_prop,
                "markdown": {"type": "string", "description": "Markdown 完整原文，禁止先概括或精简"},
                "sheet_title": {"type": "string", "description": "工作表标题（可选，默认用第一个标题）"},
            },
            ["file_path", "markdown"],
        ),
        _tool(
            "import_markdown_file",
            "直接读取磁盘上的 Markdown 文件并逐行原样转成新的 XMind 文件：所有文字（标题、列表、表格、代码块、引用）都会保留，不做概括或精简。模型只需传文件路径，不要自行改写内容。",
            {
                "file_path": md_path_prop,
                "output_path": output_xmind_prop,
                "sheet_title": {"type": "string", "description": "工作表标题（可选，默认用第一个标题）"},
            },
            ["file_path", "output_path"],
        ),
        _tool(
            "apply_to_matching",
            "按标题关键词批量修改节点：设置样式、添加/移除标记、添加/移除标签。",
            {
                "file_path": file_path_prop,
                "keyword": {"type": "string", "description": "标题包含的关键词（不区分大小写）"},
                "style": {
                    "type": "object",
                    "description": "要设置的样式（可选），键与 set_node_style 一致",
                    "properties": {
                        "font_color": {"type": "string"},
                        "font_size": {"type": "string"},
                        "font_family": {"type": "string"},
                        "font_weight": {"type": "string"},
                        "font_style": {"type": "string"},
                        "font_transform": {"type": "string"},
                        "font_decoration": {"type": "string"},
                        "background_color": {"type": "string"},
                        "fill_pattern": {"type": "string"},
                        "border_color": {"type": "string"},
                        "border_width": {"type": "string"},
                        "border_pattern": {"type": "string"},
                        "shape": {"type": "string"},
                        "line_color": {"type": "string"},
                        "line_width": {"type": "string"},
                        "line_pattern": {"type": "string"},
                        "text_align": {"type": "string"},
                        "importance": {"type": "string"},
                    },
                },
                "add_markers": {"type": "array", "items": {"type": "string"}, "description": "要添加的标记 ID 列表"},
                "remove_markers": {"type": "array", "items": {"type": "string"}, "description": "要移除的标记 ID 列表"},
                "add_labels": {"type": "array", "items": {"type": "string"}, "description": "要添加的标签列表"},
                "remove_labels": {"type": "array", "items": {"type": "string"}, "description": "要移除的标签列表"},
                "preview": {"type": "boolean", "description": "只返回将影响的节点，不实际修改（默认 false）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "keyword"],
        ),
        _tool(
            "export_text",
            "把 XMind 节点树导出为纯文本大纲（带缩进层级）。",
            {
                "file_path": file_path_prop,
                "include_ids": {"type": "boolean", "description": "是否在每行后输出节点 ID（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "set_node_style",
            "设置节点样式：重要程度（极其重要/重要/删去/默认）、字体、字号（8/10/12/14/18/24/36/48/60）、文字颜色、加粗/斜体/删除线/下划线/大小写变换、背景色、边框色/线型/宽度、节点形状（矩形/圆角矩形/下划线/双下划线/圆形/椭圆形/菱形/平行四边形/跑道/切角矩形/六边形/竖胶囊）、连线颜色/线宽、对齐。传 None 表示不修改该项。颜色用 #RRGGBB、#RRGGBBAA 或「4E0D58 60%」透明度写法。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要设置样式的节点 ID"},
                "importance": {"type": "string", "description": "重要程度：极其重要 / 重要 / 删去 / 默认"},
                "font_color": {"type": "string", "description": "文字颜色，如 #FF0000 或 4E0D58 60%"},
                "font_size": {"type": "string", "description": "字号：8 / 10 / 12 / 14 / 18 / 24 / 36 / 48 / 60 或 16pt"},
                "font_family": {"type": "string", "description": "字体族，如 Arial / 微软雅黑"},
                "font_weight": {"type": "string", "description": "加粗：加粗 / 正常（或 bold / normal）"},
                "font_style": {"type": "string", "description": "斜体：斜体 / 正常（或 italic / normal）"},
                "font_transform": {"type": "string", "description": "大小写：默认 / 全大写 / 全小写 / 首字母大写"},
                "font_decoration": {"type": "string", "description": "文字装饰：下划线 / 删除线 / 无，可叠加如「下划线+删除线」"},
                "background_color": {"type": "string", "description": "背景色，如 #FFF3BF 或 4E0D58 60%"},
                "fill_pattern": {"type": "string", "description": "填充样式：无 / 纯色 / 手绘 / 交叉线 / 细交叉线 / 影线 / 细影线"},
                "border_color": {"type": "string", "description": "边框色，如 #000000"},
                "border_width": {"type": "string", "description": "边框宽度：极细 / 细 / 中等 / 粗 / 极粗，或 2pt"},
                "border_pattern": {"type": "string", "description": "边框线型：实线 / 虚线 / 点线 / 点划线 / 无"},
                "shape": {"type": "string", "description": "节点形状：矩形 / 圆角矩形 / 下划线 / 双下划线 / 圆形 / 椭圆形 / 菱形 / 平行四边形 / 跑道 / 切角矩形 / 六边形 / 竖胶囊"},
                "line_color": {"type": "string", "description": "分支连线颜色，如 #FF6B6B"},
                "line_width": {"type": "string", "description": "分支连线宽度：极细 / 细 / 中等 / 粗 / 极粗，或 2pt"},
                "line_pattern": {"type": "string", "description": "连线线型：实线 / 虚线 / 点线 / 点划线 / 无"},
                "text_align": {"type": "string", "description": "对齐：左对齐 / 居中 / 右对齐 / 默认"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "set_sheet_layout",
            "切换工作表的布局结构，支持中文名：思维导图 / 逻辑图 / 括号图 / 组织结构图 / 树形图 / 时间轴 / 鱼骨图 / 矩阵图 / 树型表格，也支持 XMind 内部 ID。",
            {
                "file_path": file_path_prop,
                "sheet_index": {"type": "integer", "description": "工作表索引，从 0 开始（默认 0）"},
                "layout": {"type": "string", "description": "布局结构（默认 思维导图）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "set_branch_style",
            "设置某个节点向外分支的连线样式：分支类型（圆角折线/折线/直线/曲线/圆弧/手绘/圆角手绘）、终点样式（圆点/三角形/菱形/双箭头等）、粗细（极细/细/中等/粗/极粗）、颜色、线型。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要设置分支样式的节点 ID"},
                "line_type": {"type": "string", "description": "分支类型：圆角折线 / 折线 / 直线 / 曲线 / 圆弧 / 手绘 / 圆角手绘"},
                "arrow_end": {"type": "string", "description": "终点样式：无 / 圆点 / 三角形 / 矛头 / 方形 / 菱形 / 鱼骨 / 双箭头 / 反三角 / 附着 / 钩形"},
                "line_color": {"type": "string", "description": "连线颜色，如 #FF6B6B 或 4E0D58 60%"},
                "line_width": {"type": "string", "description": "线宽：极细 / 细 / 中等 / 粗 / 极粗，或 2pt"},
                "line_pattern": {"type": "string", "description": "线型：实线 / 虚线 / 点线 / 点划线 / 无"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "set_theme_style",
            "设置主题级默认样式，影响整个层级的节点：中心主题 / 分支主题 / 第三层级主题 / 细分主题 / 自由主题。参数与 set_node_style 一致（font_color、font_size、shape、background_color 等，不含 importance）。",
            {
                "file_path": file_path_prop,
                "sheet_index": {"type": "integer", "description": "工作表索引，从 0 开始（默认 0）"},
                "level": {"type": "string", "description": "主题层级：中心主题 / 分支主题 / 第三层级主题 / 细分主题 / 自由主题"},
                "font_color": {"type": "string"},
                "font_size": {"type": "string"},
                "font_family": {"type": "string"},
                "font_weight": {"type": "string"},
                "font_style": {"type": "string"},
                "font_transform": {"type": "string"},
                "font_decoration": {"type": "string"},
                "background_color": {"type": "string"},
                "fill_pattern": {"type": "string"},
                "border_color": {"type": "string"},
                "border_width": {"type": "string"},
                "border_pattern": {"type": "string"},
                "shape": {"type": "string"},
                "line_color": {"type": "string"},
                "line_width": {"type": "string"},
                "line_pattern": {"type": "string"},
                "text_align": {"type": "string"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "add_image",
            "给节点插入一张本地图片（复制进 xmind 包并更新引用，支持 png/jpg/gif/bmp/webp）。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要插入图片的节点 ID"},
                "image_path": {"type": "string", "description": "本地图片的绝对路径"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id", "image_path"],
        ),
        _tool(
            "add_summary",
            "给父节点的连续子节点区间添加概要（summary）。start_id/end_id 必须是父节点的直属子节点。",
            {
                "file_path": file_path_prop,
                "parent_id": {"type": "string", "description": "父节点 ID"},
                "start_id": {"type": "string", "description": "区间起点子节点 ID"},
                "end_id": {"type": "string", "description": "区间终点子节点 ID"},
                "title": {"type": "string", "description": "概要文字（可选，默认 概要）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "parent_id", "start_id", "end_id"],
        ),
        _tool(
            "add_boundary",
            "给父节点的连续子节点区间添加边界（boundary）。start_id/end_id 必须是父节点的直属子节点。",
            {
                "file_path": file_path_prop,
                "parent_id": {"type": "string", "description": "父节点 ID"},
                "start_id": {"type": "string", "description": "区间起点子节点 ID"},
                "end_id": {"type": "string", "description": "区间终点子节点 ID"},
                "title": {"type": "string", "description": "边界标题（可选）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "parent_id", "start_id", "end_id"],
        ),
        _tool(
            "add_relationship",
            "在指定工作表中添加一条关系线（relationship），连接两个任意节点。",
            {
                "file_path": file_path_prop,
                "start_id": {"type": "string", "description": "起点节点 ID"},
                "end_id": {"type": "string", "description": "终点节点 ID"},
                "sheet_index": {"type": "integer", "description": "工作表索引（默认 0）"},
                "title": {"type": "string", "description": "关系线文字（可选）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "start_id", "end_id"],
        ),
        _tool(
            "remove_relationship",
            "按 ID 删除一条关系线。",
            {
                "file_path": file_path_prop,
                "relationship_id": {"type": "string", "description": "关系线 ID"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "relationship_id"],
        ),
        _tool(
            "remove_boundary",
            "按 ID 删除一个边界。",
            {
                "file_path": file_path_prop,
                "boundary_id": {"type": "string", "description": "边界 ID"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "boundary_id"],
        ),
        _tool(
            "apply_style_preset",
            "一键给整张图套用配色预设。可选：清新蓝、暖阳橙、莫兰迪绿。",
            {
                "file_path": file_path_prop,
                "preset": {
                    "type": "string",
                    "enum": ["清新蓝", "暖阳橙", "莫兰迪绿"],
                    "description": "配色预设名称",
                },
                "preview": {"type": "boolean", "description": "只返回将影响的节点统计，不实际修改（默认 false）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "preset"],
        ),
        _tool(
            "set_hyperlink",
            "给节点设置超链接。url 为空则移除超链接。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要设置超链接的节点 ID"},
                "url": {"type": "string", "description": "链接地址，如 https://example.com，为空则移除"},
                "title": {"type": "string", "description": "链接显示标题（可选，默认用 url）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id", "url"],
        ),
        _tool(
            "add_marker",
            "给节点添加标记图标。常用：priority-1~9（优先级）、task-start/task-oct/task-quarter/task-half/task-3q/task-done/task-pause（任务进度）、smiley-*、flag-*、star-*。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要添加标记的节点 ID"},
                "marker_id": {"type": "string", "description": "标记 ID，如 priority-1、task-done"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id", "marker_id"],
        ),
        _tool(
            "remove_marker",
            "移除节点的指定标记图标。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "节点 ID"},
                "marker_id": {"type": "string", "description": "要移除的标记 ID"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id", "marker_id"],
        ),
        _tool(
            "add_label",
            "给节点添加一个标签。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "节点 ID"},
                "label": {"type": "string", "description": "标签文本"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id", "label"],
        ),
        _tool(
            "remove_label",
            "移除节点的一个标签。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "节点 ID"},
                "label": {"type": "string", "description": "要移除的标签文本"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id", "label"],
        ),
        _tool(
            "add_nodes",
            "批量添加多个子节点到指定父节点下。",
            {
                "file_path": file_path_prop,
                "parent_id": {"type": "string", "description": "父节点 ID"},
                "titles": {"type": "array", "items": {"type": "string"}, "description": "要添加的子节点标题列表"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "parent_id", "titles"],
        ),
        _tool(
            "duplicate_node",
            "把节点（含其整棵子树）复制到新父节点下，生成新的 ID。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要复制的节点 ID"},
                "new_parent_id": {"type": "string", "description": "目标父节点 ID"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id", "new_parent_id"],
        ),
        _tool(
            "list_backups",
            "列出某个 xmind 文件已有的自动备份（新到旧），包含路径、大小和时间。",
            {"file_path": file_path_prop},
            ["file_path"],
        ),
        _tool(
            "restore_backup",
            "把指定备份恢复到目标文件（覆盖前会把当前文件再备份一份，保证可逆）。",
            {
                "file_path": file_path_prop,
                "backup_path": {"type": "string", "description": "要恢复的备份文件路径，可用 list_backups 获取"},
            },
            ["file_path", "backup_path"],
        ),
        _tool(
            "export_svg",
            "把节点树导出为示意图 SVG 文件（用于人工查看美化效果；模型可继续用 analyze_style 获取结构化反馈）。",
            {
                "file_path": file_path_prop,
                "output_path": {"type": "string", "description": "SVG 输出路径（可选，默认与源文件同名的 .svg）"},
            },
            ["file_path"],
        ),
        _tool(
            "analyze_style",
            "样式审计：输出当前工作表各主题层级的默认样式、整张图的颜色/字体/形状使用统计，可选列出每个节点的有效样式。",
            {
                "file_path": file_path_prop,
                "sheet_index": {"type": "integer", "description": "工作表索引（默认 0）"},
                "include_node_styles": {"type": "boolean", "description": "是否列出节点级有效样式（默认 false）"},
                "max_nodes": {"type": "integer", "description": "include_node_styles 时最多列出多少个节点（默认 50）"},
            },
            ["file_path"],
        ),
        _tool(
            "list_attachments",
            "列出 xmind 包内的附件/图片资源，标注哪些被节点引用、哪些未被引用。",
            {"file_path": file_path_prop},
            ["file_path"],
        ),
        _tool(
            "remove_unused_attachments",
            "找出未被任何节点引用的附件/图片，保存时移除，减小文件体积。",
            {
                "file_path": file_path_prop,
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "batch_update_titles",
            "按关键词或正则批量替换节点标题，返回被修改的节点。",
            {
                "file_path": file_path_prop,
                "find": {"type": "string", "description": "要查找的文本或正则表达式"},
                "replace": {"type": "string", "description": "替换为的文本"},
                "use_regex": {"type": "boolean", "description": "是否把 find 当作正则表达式（默认 false）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "find", "replace"],
        ),
        _tool(
            "delete_empty_nodes",
            "删除所有空标题的普通节点（含其子树），跳过根主题和概要/批注节点。",
            {
                "file_path": file_path_prop,
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "merge_duplicate_nodes",
            "合并标题相同的兄弟节点：保留第一个，其余节点的子节点并入后删除。",
            {
                "file_path": file_path_prop,
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "notes_to_children",
            "把节点备注的每一行转成一个子节点（默认转换后清空备注）。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要转换的节点 ID"},
                "keep_notes": {"type": "boolean", "description": "转换后是否保留原备注（默认 false）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "children_to_notes",
            "把节点的直接子节点标题合并成备注（默认转换后删除子节点）。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要转换的节点 ID"},
                "keep_children": {"type": "boolean", "description": "转换后是否保留子节点（默认 false）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "auto_number",
            "给节点自动编号：decimal 按每个父节点 1.2.3. 编号；hierarchical 按 1. / 1.1. / 1.1.1. 层级编号。已有编号会被替换。",
            {
                "file_path": file_path_prop,
                "mode": {"type": "string", "enum": ["decimal", "hierarchical"], "description": "编号方式（默认 decimal）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "list_sheets",
            "列出文件中的所有工作表：索引、标题、根主题、节点数。",
            {"file_path": file_path_prop},
            ["file_path"],
        ),
        _tool(
            "add_sheet",
            "新增一张工作表。",
            {
                "file_path": file_path_prop,
                "title": {"type": "string", "description": "工作表标题（可选，默认 画布）"},
                "root_title": {"type": "string", "description": "根主题标题（可选，默认 中心主题）"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "rename_sheet",
            "重命名工作表。",
            {
                "file_path": file_path_prop,
                "sheet_index": {"type": "integer", "description": "工作表索引"},
                "title": {"type": "string", "description": "新标题"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "sheet_index", "title"],
        ),
        _tool(
            "delete_sheet",
            "删除工作表（至少保留一张）。",
            {
                "file_path": file_path_prop,
                "sheet_index": {"type": "integer", "description": "要删除的工作表索引"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "sheet_index"],
        ),
        _tool(
            "move_sheet",
            "移动工作表顺序。",
            {
                "file_path": file_path_prop,
                "sheet_index": {"type": "integer", "description": "当前索引"},
                "new_index": {"type": "integer", "description": "目标索引"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "sheet_index", "new_index"],
        ),
    ]

    allowed = resolve_toolset(toolset)
    if allowed is None:
        return tools
    return [tool for tool in tools if tool.name in allowed]
