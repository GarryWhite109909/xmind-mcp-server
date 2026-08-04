"""MCP 工具定义和处理逻辑。

工具列表：
  读取类:
    read_structure    — 读取节点树结构（默认带 ID）
    read_all          — 读取结构 + 图片 OCR + 节点元数据
    list_images       — 列出所有含图片的节点
    read_image_ocr    — 对指定节点的图片做 OCR
    find_node         — 按关键词搜索节点，返回 ID 和路径

  编辑类:
    add_node          — 添加子节点
    update_node       — 更新节点标题/备注
    delete_node       — 删除节点
    move_node         — 移动节点到新父节点
    save_file         — 保存修改

  导出类:
    export_markdown   — 导出为 Markdown 大纲

  系统类:
    get_hardware_info — 获取硬件检测结果
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from mcp import types

from .config import Config
from .xmind_parser import parse_xmind, load_topic_image, XMindFile, TopicNode
from .xmind_writer import XMindWriter
from .ocr.manager import OCRManager


# ---------- 文件缓存 ----------

class XMindWorkspace:
    """缓存已解析的 XMind 文件，避免每次工具调用都重新解析。"""

    def __init__(self):
        # {path: (mtime, XMindFile, XMindWriter)}
        self._cache: dict[str, tuple[float, XMindFile, XMindWriter]] = {}

    def get(self, path: str) -> tuple[XMindFile, XMindWriter]:
        path = os.path.abspath(path)
        mtime = os.path.getmtime(path)

        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1], cached[2]

        xm = parse_xmind(path)
        writer = XMindWriter(xm)
        self._cache[path] = (mtime, xm, writer)
        return xm, writer

    def invalidate(self, path: str) -> None:
        self._cache.pop(os.path.abspath(path), None)

    def refresh_after_save(self, path: str) -> tuple[XMindFile, XMindWriter]:
        """保存后刷新缓存。"""
        self.invalidate(path)
        return self.get(path)


# ---------- 格式化辅助 ----------

def _clean_title(title: str) -> str:
    """清理节点标题中的换行和多余空白。"""
    if not title:
        return ""
    return " ".join(title.replace("\r", " ").replace("\n", " ").split())


def _format_badges(node: TopicNode, show_images: bool, clean: bool) -> str:
    """生成节点徽章字符串。clean=True 时不显示内部标记。"""
    if clean:
        return ""
    badges = []
    if show_images and node.image_src:
        badges.append("📷图片")
    if node.notes:
        badges.append("📝备注")
    if node.markers:
        badges.append(f"🔖{','.join(node.markers)}")
    if node.labels:
        badges.append(f"🏷️{','.join(node.labels)}")
    return f" {' '.join(badges)}" if badges else ""


def _format_node_line(node: TopicNode, include_ids: bool, show_images: bool, clean: bool) -> str:
    """格式化单个节点的显示行。"""
    title = _clean_title(node.title)
    if not title and node.image_src:
        title = "📷图片"

    id_part = f" [id: {node.id}]" if include_ids else ""
    badges = _format_badges(node, show_images, clean)
    return f"[L{node.level}]{id_part} {title}{badges}"


def format_topic_tree(
    xm: XMindFile,
    *,
    show_images: bool = True,
    include_ids: bool = True,
    clean: bool = False,
) -> str:
    """把节点树格式化成带缩进的文本。"""
    lines = []
    for i, sheet in enumerate(xm.sheets):
        if i > 0:
            lines.append("")
        lines.append(f"📄 工作表: {sheet.title}")
        _format_node(
            sheet.root,
            prefix="",
            is_last=True,
            lines=lines,
            show_images=show_images,
            include_ids=include_ids,
            clean=clean,
        )
    return "\n".join(lines)


def _format_node(
    node: TopicNode,
    prefix: str,
    is_last: bool,
    lines: list[str],
    show_images: bool,
    include_ids: bool,
    clean: bool,
) -> None:
    connector = "└─ " if is_last else "├─ "
    line = _format_node_line(node, include_ids, show_images, clean)
    lines.append(f"{prefix}{connector}{line}")

    child_prefix = prefix + ("   " if is_last else "│  ")
    for j, child in enumerate(node.children):
        _format_node(
            child,
            child_prefix,
            is_last=(j == len(node.children) - 1),
            lines=lines,
            show_images=show_images,
            include_ids=include_ids,
            clean=clean,
        )


def _build_node_path(node: TopicNode, xm: XMindFile) -> str:
    """构建节点的标题路径，如 '根主题/分支主题/子节点'。"""
    parts = []
    current = node
    while current:
        parts.append(_clean_title(current.title) or current.id)
        # 通过 parent_id 在树中查找父节点
        if not current.parent_id:
            break
        current = xm.find_topic(current.parent_id)
        if current is None or current.id == node.id:
            break
    return " / ".join(reversed(parts))


# ---------- 工具定义 ----------

def _tool(name: str, description: str, properties: dict, required: list[str] = None) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    )


def build_tool_list() -> list[types.Tool]:
    file_path_prop = {"type": "string", "description": ".xmind 文件路径"}

    return [
        _tool(
            "read_structure",
            "读取 XMind 文件的节点树结构，返回带层级和节点 ID 的文本。",
            {
                "file_path": file_path_prop,
                "include_ids": {"type": "boolean", "description": "是否在每行后输出节点 ID（默认 true）"},
                "clean": {"type": "boolean", "description": "是否去掉 📷/📝/🔖 等内部标记（默认 false）"},
            },
            ["file_path"],
        ),
        _tool(
            "read_all",
            "一步到位：读取节点树 + 对所有图片节点做 OCR + 输出 notes/labels/markers 元数据。",
            {
                "file_path": file_path_prop,
                "include_ids": {"type": "boolean", "description": "是否在每行后输出节点 ID（默认 true）"},
                "clean": {"type": "boolean", "description": "是否去掉 📷/📝/🔖 等内部标记（默认 false）"},
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
            "read_image_ocr",
            "对指定节点的图片做 OCR 识别，返回图片中的文字。需要先用 read_structure 或 list_images 获取 node_id。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "包含图片的节点 ID"},
            },
            ["file_path", "node_id"],
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
            "add_node",
            "在指定父节点下添加子节点。修改不会自动保存，除非设置 auto_save=true。",
            {
                "file_path": file_path_prop,
                "parent_id": {"type": "string", "description": "父节点 ID"},
                "title": {"type": "string", "description": "新节点标题"},
                "notes": {"type": "string", "description": "节点备注（可选）"},
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
            "把节点移动到新的父节点下。修改不会自动保存，除非设置 auto_save=true。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要移动的节点 ID"},
                "new_parent_id": {"type": "string", "description": "新父节点 ID"},
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
            "export_markdown",
            "把 XMind 节点树导出为 Markdown 大纲。",
            {
                "file_path": file_path_prop,
                "include_ids": {"type": "boolean", "description": "是否在标题后附加 ID（默认 false）"},
            },
            ["file_path"],
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
            "设置节点样式：字体、字号、文字颜色、加粗、斜体、背景色、边框色、对齐。传 None 表示不修改该项。颜色用 #RRGGBB 十六进制。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "要设置样式的节点 ID"},
                "font_color": {"type": "string", "description": "文字颜色，如 #FF0000"},
                "font_size": {"type": "string", "description": "字号，如 12pt / 16pt"},
                "font_family": {"type": "string", "description": "字体族，如 Arial / 微软雅黑"},
                "font_weight": {"type": "string", "description": "加粗，如 bold / normal"},
                "font_style": {"type": "string", "description": "斜体，如 italic / normal"},
                "background_color": {"type": "string", "description": "背景色，如 #FFF3BF"},
                "border_color": {"type": "string", "description": "边框色，如 #000000"},
                "text_align": {"type": "string", "description": "对齐，如 left / center / right"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id"],
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
            "get_hardware_info",
            "获取当前机器的硬件检测结果和 OCR 引擎配置信息。",
            {},
            [],
        ),
    ]


# ---------- 工具处理 ----------

class XMindTools:
    """工具处理入口，管理文件缓存和 OCR。"""

    def __init__(self, config: Config):
        self.config = config
        self.workspace = XMindWorkspace()
        self.ocr = OCRManager(config)

    def close(self) -> None:
        """释放 OCR 模型等资源。"""
        self.ocr.unload()

    async def call_tool(self, name: str, arguments: dict) -> list[types.TextContent]:
        handler = self._handlers.get(name)
        if handler is None:
            return [types.TextContent(type="text", text=f"未知工具: {name}")]

        try:
            result = await handler(self, arguments)
            return [types.TextContent(type="text", text=result)]
        except FileNotFoundError as e:
            return [types.TextContent(type="text", text=f"❌ 文件不存在或路径错误: {e}")]
        except PermissionError as e:
            return [types.TextContent(type="text", text=f"❌ 文件被占用或无权限访问: {e}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"❌ 错误: {type(e).__name__}: {e}")]

    # ---- 各工具实现 ----

    async def _read_structure(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        include_ids = args.get("include_ids", True)
        clean = args.get("clean", False)
        return format_topic_tree(xm, include_ids=include_ids, clean=clean)

    async def _list_images(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        nodes = xm.find_topics_with_image()
        if not nodes:
            return "文件中没有包含图片的节点。"
        lines = [f"找到 {len(nodes)} 个图片节点："]
        for n in nodes:
            title = _clean_title(n.title) or "📷图片"
            lines.append(f"  • ID: {n.id}  | 标题: {title}  | 图片: {n.image_src[:60]}")
        return "\n".join(lines)

    async def _read_image_ocr(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        node = xm.find_topic(args["node_id"])
        if not node:
            return f"找不到节点: {args['node_id']}"
        if not node.image_src:
            return f"节点 '{_clean_title(node.title)}' 没有图片。"

        image_data = load_topic_image(node, xm)
        if not image_data:
            return f"图片加载失败: {node.image_src}"

        result = self.ocr.recognize(image_data)
        header = f"📷 节点「{_clean_title(node.title)}」的图片 OCR 结果（{result.elapsed:.2f}s）："
        body = result.text if result.text else "（未识别到文字）"
        return f"{header}\n{body}"

    async def _read_all(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        include_ids = args.get("include_ids", True)
        clean = args.get("clean", False)
        image_nodes = xm.find_topics_with_image()

        # 先做所有图片的 OCR
        ocr_results: dict[str, str] = {}
        if image_nodes:
            for node in image_nodes:
                image_data = load_topic_image(node, xm)
                if image_data:
                    result = self.ocr.recognize(image_data)
                    ocr_results[node.id] = result.text or "（未识别到文字）"

        # 格式化输出
        lines = []
        for i, sheet in enumerate(xm.sheets):
            if i > 0:
                lines.append("")
            lines.append(f"📄 工作表: {sheet.title}")
            self._format_node_with_ocr(
                sheet.root, "", True, lines, ocr_results,
                include_ids=include_ids, clean=clean,
            )
        return "\n".join(lines)

    @staticmethod
    def _format_node_with_ocr(
        node: TopicNode,
        prefix: str,
        is_last: bool,
        lines: list[str],
        ocr_map: dict[str, str],
        include_ids: bool,
        clean: bool,
    ) -> None:
        connector = "└─ " if is_last else "├─ "
        line = _format_node_line(node, include_ids, show_images=True, clean=clean)
        lines.append(f"{prefix}{connector}{line}")

        child_prefix = prefix + ("   " if is_last else "│  ")

        # 内联 OCR 结果
        if node.id in ocr_map and not clean:
            ocr_text = ocr_map[node.id]
            lines.append(f"{child_prefix}   ↳ [OCR] {ocr_text}")

        # 输出节点元数据
        if not clean:
            if node.notes:
                notes = _clean_title(node.notes)
                lines.append(f"{child_prefix}   📝 备注: {notes}")
            if node.labels:
                lines.append(f"{child_prefix}   🏷️ 标签: {', '.join(node.labels)}")

        for j, child in enumerate(node.children):
            XMindTools._format_node_with_ocr(
                child,
                child_prefix,
                j == len(node.children) - 1,
                lines,
                ocr_map,
                include_ids=include_ids,
                clean=clean,
            )

    async def _find_node(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        keyword = args["keyword"].lower()
        matches = [n for n in xm.all_topics() if keyword in n.title.lower()]
        if not matches:
            return f"未找到包含关键词 '{args['keyword']}' 的节点。"

        lines = [f"找到 {len(matches)} 个匹配节点："]
        for n in matches:
            path = _build_node_path(n, xm)
            title = _clean_title(n.title) or "（无标题）"
            lines.append(f"\n• ID: {n.id}\n  标题: {title}\n  层级: L{n.level}\n  路径: {path}")
        return "\n".join(lines)

    async def _add_node(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        new_node = writer.add_node(
            parent_id=args["parent_id"],
            title=args["title"],
            notes=args.get("notes", ""),
        )
        msg = f"✅ 已添加节点：ID={new_node.id}, 标题='{new_node.title}'"
        if args.get("auto_save"):
            saved_path = writer.save()
            self.workspace.refresh_after_save(saved_path)
            msg += f"\n✅ 已自动保存到: {saved_path}"
        else:
            msg += "\n⚠️ 修改未保存，请调用 save_file 或设置 auto_save=true。"
        return msg

    async def _update_node(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.update_node(
            node_id=args["node_id"],
            title=args.get("title"),
            notes=args.get("notes"),
        )
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"

        msg = f"✅ 节点 {args['node_id']} 已更新。"
        if args.get("auto_save"):
            saved_path = writer.save()
            self.workspace.refresh_after_save(saved_path)
            msg += f"\n✅ 已自动保存到: {saved_path}"
        else:
            msg += "\n⚠️ 修改未保存，请调用 save_file 或设置 auto_save=true。"
        return msg

    async def _delete_node(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        try:
            success = writer.delete_node(args["node_id"])
        except ValueError as e:
            return f"❌ {e}"

        if not success:
            return f"❌ 找不到节点: {args['node_id']}"

        msg = f"✅ 节点 {args['node_id']} 及其子节点已删除。"
        if args.get("auto_save"):
            saved_path = writer.save()
            self.workspace.refresh_after_save(saved_path)
            msg += f"\n✅ 已自动保存到: {saved_path}"
        else:
            msg += "\n⚠️ 修改未保存，请调用 save_file 或设置 auto_save=true。"
        return msg

    async def _move_node(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.move_node(args["node_id"], args["new_parent_id"])
        if not success:
            return "❌ 移动失败：找不到节点或不能移动根主题。"

        msg = f"✅ 节点 {args['node_id']} 已移动到 {args['new_parent_id']} 下。"
        if args.get("auto_save"):
            saved_path = writer.save()
            self.workspace.refresh_after_save(saved_path)
            msg += f"\n✅ 已自动保存到: {saved_path}"
        else:
            msg += "\n⚠️ 修改未保存，请调用 save_file 或设置 auto_save=true。"
        return msg

    async def _save_file(self, args: dict) -> str:
        xm, writer = self.workspace.get(args["file_path"])
        output = args.get("output_path")
        saved_path = writer.save(output)
        self.workspace.refresh_after_save(saved_path)
        return f"✅ 已保存到: {saved_path}"

    async def _export_markdown(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        include_ids = args.get("include_ids", False)
        lines = []
        for sheet in xm.sheets:
            lines.append(f"# {sheet.title}")
            lines.append("")
            _export_node_md(sheet.root, 0, lines, include_ids)
            lines.append("")
        return "\n".join(lines).strip()

    async def _export_text(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        include_ids = args.get("include_ids", False)
        lines = []
        for sheet in xm.sheets:
            lines.append(f"📄 {sheet.title}")
            lines.append("")
            self._export_node_text(sheet.root, 0, lines, include_ids)
            lines.append("")
        return "\n".join(lines).strip()

    def _export_node_text(self, node: TopicNode, depth: int, lines: list[str], include_ids: bool) -> None:
        indent = "  " * depth
        title = _clean_title(node.title) or "📷图片"
        id_part = f" [{node.id}]" if include_ids else ""
        lines.append(f"{indent}- {title}{id_part}")
        for child in node.children:
            self._export_node_text(child, depth + 1, lines, include_ids)

    async def _set_node_style(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        kwargs = {k: args.get(k) for k in (
            "font_color", "font_size", "font_family", "font_weight",
            "font_style", "background_color", "border_color", "text_align",
        )}
        success = writer.set_node_style(args["node_id"], **kwargs)
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 节点 {args['node_id']} 样式已更新。")

    async def _set_hyperlink(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.set_hyperlink(args["node_id"], args["url"], args.get("title", ""))
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 节点 {args['node_id']} 超链接已{'移除' if not args['url'] else '设置'}。")

    async def _add_marker(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.add_marker(args["node_id"], args["marker_id"])
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 节点 {args['node_id']} 已添加标记 {args['marker_id']}。")

    async def _remove_marker(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.remove_marker(args["node_id"], args["marker_id"])
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 节点 {args['node_id']} 已移除标记 {args['marker_id']}。")

    async def _add_label(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.add_label(args["node_id"], args["label"])
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 节点 {args['node_id']} 已添加标签 '{args['label']}'。")

    async def _remove_label(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.remove_label(args["node_id"], args["label"])
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 节点 {args['node_id']} 已移除标签 '{args['label']}'。")

    async def _add_nodes(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        try:
            nodes = writer.add_nodes(args["parent_id"], args["titles"])
        except ValueError as e:
            return f"❌ {e}"
        if not nodes:
            return "❌ 未能添加任何节点。"
        ids = ", ".join(n.id for n in nodes)
        msg = f"✅ 已添加 {len(nodes)} 个节点：{ids}"
        return self._auto_save_msg(file_path, writer, args, msg, fresh_save=True)

    async def _duplicate_node(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        try:
            new_node = writer.duplicate_node(args["node_id"], args["new_parent_id"])
        except ValueError as e:
            return f"❌ {e}"
        if new_node is None:
            return f"❌ 找不到节点: {args['node_id']}"
        msg = f"✅ 已复制节点到 {args['new_parent_id']} 下，新 ID: {new_node.id}"
        return self._auto_save_msg(file_path, writer, args, msg, fresh_save=True)

    def _auto_save_msg(self, file_path: str, writer: XMindWriter, args: dict, msg: str, fresh_save: bool = False) -> str:
        """编辑后统一处理保存提示。"""
        if args.get("auto_save"):
            saved_path = writer.save()
            self.workspace.refresh_after_save(saved_path)
            return f"{msg}\n✅ 已自动保存到: {saved_path}"
        return f"{msg}\n⚠️ 修改未保存，请调用 save_file 或设置 auto_save=true。"

    async def _get_hardware_info(self, args: dict) -> str:
        c = self.config
        lines = [
            f"🖥️ 操作系统: {c.os}",
            f"🔧 OCR 引擎: {c.ocr_engine}",
            f"⚡ 推理设备: {c.device}",
            f"💾 内存: {c.ram_gb} GB",
            f"🧠 CPU 核心数: {c.cpu_count}",
        ]
        if c.gpus:
            for g in c.gpus:
                vram = f", 显存: {g['vram_mb']}MB" if g.get("vram_mb") else ""
                lines.append(f"🎮 显卡: {g['name']} ({g['vendor']}{vram})")
        else:
            lines.append("🎮 显卡: 未检测到")
        return "\n".join(lines)

    # ---- handler 注册表 ----
    _handlers = {
        "read_structure": _read_structure,
        "read_all": _read_all,
        "list_images": _list_images,
        "read_image_ocr": _read_image_ocr,
        "find_node": _find_node,
        "add_node": _add_node,
        "update_node": _update_node,
        "delete_node": _delete_node,
        "move_node": _move_node,
        "save_file": _save_file,
        "export_markdown": _export_markdown,
        "export_text": _export_text,
        "set_node_style": _set_node_style,
        "set_hyperlink": _set_hyperlink,
        "add_marker": _add_marker,
        "remove_marker": _remove_marker,
        "add_label": _add_label,
        "remove_label": _remove_label,
        "add_nodes": _add_nodes,
        "duplicate_node": _duplicate_node,
        "get_hardware_info": _get_hardware_info,
    }


def _export_node_md(node: TopicNode, depth: int, lines: list[str], include_ids: bool) -> None:
    """递归导出 Markdown 大纲。"""
    indent = "  " * depth
    title = _clean_title(node.title) or "📷图片"
    id_part = f" `id:{node.id}`" if include_ids else ""
    lines.append(f"{indent}- {title}{id_part}")
    for child in node.children:
        _export_node_md(child, depth + 1, lines, include_ids)
