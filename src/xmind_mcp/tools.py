"""MCP 工具定义和处理逻辑。

工具列表：
  读取类:
    read_structure    — 读取节点树结构（默认带 ID）
    read_all          — 读取结构 + 图片 OCR + 节点元数据
    list_images       — 列出所有含图片的节点
    read_image_ocr    — 对指定节点的图片做 OCR
    find_node         — 按关键词搜索节点，返回 ID 和路径
    get_node          — 读取单个节点的完整信息（含样式、超链接、父节点）
    analyze_file      — 统计/体检：节点数、深度、重复标题、空节点等

  编辑类:
    add_node          — 添加子节点
    add_nodes         — 批量添加子节点
    update_node       — 更新节点标题/备注
    delete_node       — 删除节点
    move_node         — 移动节点到新父节点
    duplicate_node    — 复制节点（含子树）
    save_file         — 保存修改
    create_file       — 从零新建 XMind 文件
    import_markdown   — 从 Markdown 大纲新建 XMind 文件
    import_markdown_file — 直接读取 .md 文件，逐行原样转成 XMind（不概括、不精简）
    repair_file       — 修复打不开的 XMind 文件（补全必需组成部分）
    apply_to_matching — 按关键词批量设置样式/标记/标签

  美化类:
    set_node_style    — 设置节点字体/颜色/背景/边框等
    set_sheet_layout  — 切换工作表的布局结构（顺时针/逻辑图等）
    apply_style_preset— 一键套用配色预设（清新蓝/暖阳橙/莫兰迪绿）
    set_hyperlink     — 设置/移除超链接
    add_image         — 给节点插入图片
    add_summary       — 给连续子节点区间加概要
    add_boundary      — 给连续子节点区间加边界
    add_relationship  — 添加关系线
    remove_relationship — 删除关系线
    remove_boundary   — 删除边界
    add_marker        — 添加标记图标
    remove_marker     — 移除标记图标
    add_label         — 添加标签
    remove_label      — 移除标签

  导出类:
    export_markdown   — 导出为 Markdown 大纲
    export_text       — 导出为纯文本大纲

  系统类:
    get_hardware_info — 获取硬件检测结果
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from mcp import types

from .config import Config
from .hardware.installer import verify_installed
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
    image_as_title = not title and bool(node.image_src)
    if image_as_title:
        title = "📷图片"

    id_part = f" [id: {node.id}]" if include_ids else ""
    class_part = ""
    if not clean and node.topic_class not in ("root", "attached"):
        # summary/callout/detached 不是普通子主题，明确标出来避免模型误操作
        class_part = f" [类型: {node.topic_class}]"
    # 标题已经是 📷图片 时不再重复输出图片徽章
    badges = _format_badges(node, show_images and not image_as_title, clean)
    return f"[L{node.level}]{id_part}{class_part} {title}{badges}"


def _format_multiline(text: str, prefix: str) -> str:
    """保留多行文本的换行，并为续行加上树形缩进前缀。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    lines = text.split("\n")
    return "\n".join(lines[0:1] + [f"{prefix}{line}" for line in lines[1:]])


def _md_escape(text: str) -> str:
    """转义 Markdown 标题中的特殊字符，避免导出的大纲被破坏。"""
    return (
        text.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("#", "\\#")
    )


def format_topic_tree(
    xm: XMindFile,
    *,
    show_images: bool = True,
    include_ids: bool = True,
    clean: bool = False,
    max_depth: Optional[int] = None,
    max_nodes: Optional[int] = None,
) -> str:
    """把节点树格式化成带缩进的文本。"""
    lines = []
    state = {"nodes": 0, "truncated": False}
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
            max_depth=max_depth,
            max_nodes=max_nodes,
            state=state,
        )
    if state["truncated"]:
        lines.append("…（输出已按 max_nodes 截断）")
    return "\n".join(lines)


def _format_node(
    node: TopicNode,
    prefix: str,
    is_last: bool,
    lines: list[str],
    show_images: bool,
    include_ids: bool,
    clean: bool,
    max_depth: Optional[int],
    max_nodes: Optional[int],
    state: dict,
) -> None:
    if max_nodes is not None and state["nodes"] >= max_nodes:
        state["truncated"] = True
        return
    state["nodes"] += 1
    connector = "└─ " if is_last else "├─ "
    line = _format_node_line(node, include_ids, show_images, clean)
    lines.append(f"{prefix}{connector}{line}")

    if max_depth is not None and node.level >= max_depth:
        return
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
            max_depth=max_depth,
            max_nodes=max_nodes,
            state=state,
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


def _effective_style(node: TopicNode, xm: XMindFile) -> dict:
    """合并“工作表主题里该主题类型的样式 + 节点自身样式”。

    例如 importantTopic 主题自带紫色填充/加粗，节点自身只写了字体和形状，
    合并后模型才能看到这个节点实际长什么样。
    """
    props: dict[str, str] = {}
    if node.xmind_class and node.sheet_index < len(xm.raw_content):
        sheet = xm.raw_content[node.sheet_index]
        theme = sheet.get("theme") if isinstance(sheet, dict) else None
        if isinstance(theme, dict):
            class_theme = theme.get(node.xmind_class)
            if isinstance(class_theme, dict):
                class_props = class_theme.get("properties")
                if isinstance(class_props, dict):
                    props.update(class_props)
    node_props = (node.style or {}).get("properties")
    if isinstance(node_props, dict):
        props.update(node_props)
    return props


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
    md_path_prop = {"type": "string", "description": "要转换的 Markdown 文件路径（.md）"}
    output_xmind_prop = {"type": "string", "description": "生成的 .xmind 文件路径"}

    return [
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
            "一步到位：读取节点树 + 对所有图片节点做 OCR + 输出 notes/labels/markers/超链接/样式元数据。",
            {
                "file_path": file_path_prop,
                "include_ids": {"type": "boolean", "description": "是否在每行后输出节点 ID（默认 true）"},
                "clean": {"type": "boolean", "description": "是否去掉 📷/📝/🔖 等内部标记（默认 false）"},
                "include_style": {"type": "boolean", "description": "是否输出每个节点的样式属性（默认 false）"},
                "max_depth": {"type": "integer", "description": "只输出到该层级（可选，根主题为 L0）"},
                "max_nodes": {"type": "integer", "description": "最多输出多少个节点，超出截断（可选）"},
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
            "修复打不开的 XMind 文件：自动补全 metadata.json、content.xml、默认主题、revisionId 等 XMind 必需内容，不改变已有主题和样式。覆盖保存前会自动备份原文件。",
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
                        "background_color": {"type": "string"},
                        "border_color": {"type": "string"},
                        "border_width": {"type": "string"},
                        "shape": {"type": "string"},
                        "line_color": {"type": "string"},
                        "line_width": {"type": "string"},
                        "text_align": {"type": "string"},
                    },
                },
                "add_markers": {"type": "array", "items": {"type": "string"}, "description": "要添加的标记 ID 列表"},
                "remove_markers": {"type": "array", "items": {"type": "string"}, "description": "要移除的标记 ID 列表"},
                "add_labels": {"type": "array", "items": {"type": "string"}, "description": "要添加的标签列表"},
                "remove_labels": {"type": "array", "items": {"type": "string"}, "description": "要移除的标签列表"},
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
            "设置节点样式：字体、字号、文字颜色、加粗、斜体、背景色、边框色/宽度、节点形状、连线颜色/宽度、对齐。传 None 表示不修改该项。颜色用 #RRGGBB 十六进制。",
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
                "border_width": {"type": "string", "description": "边框宽度，如 2pt"},
                "shape": {"type": "string", "description": "节点形状，如 org.xmind.topicShape.roundedRect / ellipse"},
                "line_color": {"type": "string", "description": "分支连线颜色，如 #FF6B6B"},
                "line_width": {"type": "string", "description": "分支连线宽度，如 2pt"},
                "text_align": {"type": "string", "description": "对齐，如 left / center / right"},
                "auto_save": {"type": "boolean", "description": "是否自动保存（默认 false）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "set_sheet_layout",
            "切换工作表的布局结构。常用：org.xmind.ui.map.clockwise（顺时针思维导图）、org.xmind.ui.map.unclockwise（逆时针）、org.xmind.ui.logic.right（逻辑图向右）、org.xmind.ui.tree.right（树形图向右）、org.xmind.ui.org-chart.down（组织结构图向下）。",
            {
                "file_path": file_path_prop,
                "sheet_index": {"type": "integer", "description": "工作表索引，从 0 开始（默认 0）"},
                "layout": {"type": "string", "description": "布局结构 ID（默认 org.xmind.ui.map.clockwise）"},
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
        return format_topic_tree(
            xm,
            include_ids=include_ids,
            clean=clean,
            max_depth=args.get("max_depth"),
            max_nodes=args.get("max_nodes"),
        )

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

        try:
            result = await asyncio.to_thread(self.ocr.recognize, image_data)
        except Exception as e:
            return f"❌ OCR 失败: {type(e).__name__}: {e}"
        header = f"📷 节点「{_clean_title(node.title)}」的图片 OCR 结果（{result.elapsed:.2f}s）："
        body = result.text if result.text else "（未识别到文字）"
        return f"{header}\n{body}"

    async def _read_all(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        include_ids = args.get("include_ids", True)
        clean = args.get("clean", False)
        include_style = args.get("include_style", False)
        max_depth = args.get("max_depth")
        max_nodes = args.get("max_nodes")
        image_nodes = xm.find_topics_with_image()

        # 先做所有图片的 OCR
        ocr_results: dict[str, str] = {}
        if image_nodes:
            for node in image_nodes:
                try:
                    image_data = load_topic_image(node, xm)
                    if image_data:
                        result = await asyncio.to_thread(self.ocr.recognize, image_data)
                        ocr_results[node.id] = result.text or "（未识别到文字）"
                except Exception as e:
                    ocr_results[node.id] = f"（OCR 失败: {type(e).__name__}: {e}）"

        # 格式化输出
        lines = []
        state = {"nodes": 0, "truncated": False}
        for i, sheet in enumerate(xm.sheets):
            if i > 0:
                lines.append("")
            lines.append(f"📄 工作表: {sheet.title}")
            self._format_node_with_ocr(
                sheet.root, "", True, lines, ocr_results,
                include_ids=include_ids, clean=clean,
                include_style=include_style,
                max_depth=max_depth, max_nodes=max_nodes, state=state,
            )
        if state["truncated"]:
            lines.append("…（输出已按 max_nodes 截断）")
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
        include_style: bool,
        max_depth: Optional[int],
        max_nodes: Optional[int],
        state: dict,
    ) -> None:
        if max_nodes is not None and state["nodes"] >= max_nodes:
            state["truncated"] = True
            return
        state["nodes"] += 1
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
                notes = _format_multiline(node.notes, child_prefix + "     ")
                lines.append(f"{child_prefix}   📝 备注: {notes}")
            if node.labels:
                lines.append(f"{child_prefix}   🏷️ 标签: {', '.join(node.labels)}")
            if node.hyperlink:
                lines.append(f"{child_prefix}   🔗 链接: {node.hyperlink}")
            if node.xmind_class and node.xmind_class != "topic":
                lines.append(f"{child_prefix}   📌 主题类型: {node.xmind_class}")
            if include_style and node.style:
                props = node.style.get("properties", {})
                if props:
                    style_text = ", ".join(f"{k}={v}" for k, v in props.items())
                    lines.append(f"{child_prefix}   🎨 样式: {style_text}")

        if max_depth is not None and node.level >= max_depth:
            return
        for j, child in enumerate(node.children):
            XMindTools._format_node_with_ocr(
                child,
                child_prefix,
                j == len(node.children) - 1,
                lines,
                ocr_map,
                include_ids=include_ids,
                clean=clean,
                include_style=include_style,
                max_depth=max_depth,
                max_nodes=max_nodes,
                state=state,
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
        try:
            new_node = writer.add_node(
                parent_id=args["parent_id"],
                title=args["title"],
                notes=args.get("notes", ""),
                before_id=args.get("before_id"),
                after_id=args.get("after_id"),
            )
        except ValueError as e:
            return f"❌ {e}"
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
        except (ValueError, TypeError) as e:
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
        success = writer.move_node(
            args["node_id"],
            args["new_parent_id"],
            before_id=args.get("before_id"),
            after_id=args.get("after_id"),
        )
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
            lines.append(f"# {_md_escape(sheet.title)}")
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
            "font_style", "background_color", "border_color", "border_width",
            "shape", "line_color", "line_width", "text_align",
        )}
        success = writer.set_node_style(args["node_id"], **kwargs)
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 节点 {args['node_id']} 样式已更新。")

    async def _set_sheet_layout(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.set_sheet_layout(
            sheet_index=args.get("sheet_index", 0),
            layout=args.get("layout", "org.xmind.ui.map.clockwise"),
        )
        if not success:
            return "❌ 工作表索引无效。"
        return self._auto_save_msg(file_path, writer, args, "✅ 工作表布局已更新。")

    async def _add_image(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        ok, src = writer.add_image(args["node_id"], args["image_path"])
        if not ok:
            return f"❌ {src}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 已插入图片: {src}")

    async def _add_summary(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        try:
            node = writer.add_summary(
                args["parent_id"],
                args["start_id"],
                args["end_id"],
                title=args.get("title", "概要"),
            )
        except ValueError as e:
            return f"❌ {e}"
        return self._auto_save_msg(
            file_path, writer, args,
            f"✅ 已添加概要 ID={node.id}，覆盖 {args['start_id']} ~ {args['end_id']}。",
        )

    async def _add_boundary(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        try:
            success = writer.add_boundary(
                args["parent_id"],
                args["start_id"],
                args["end_id"],
                title=args.get("title", ""),
            )
        except ValueError as e:
            return f"❌ {e}"
        if not success:
            return "❌ 找不到父节点。"
        return self._auto_save_msg(
            file_path, writer, args,
            f"✅ 已添加边界，覆盖 {args['start_id']} ~ {args['end_id']}。",
        )

    async def _add_relationship(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        try:
            success = writer.add_relationship(
                args.get("sheet_index", 0),
                args["start_id"],
                args["end_id"],
                title=args.get("title", ""),
            )
        except ValueError as e:
            return f"❌ {e}"
        if not success:
            return "❌ 工作表索引无效。"
        return self._auto_save_msg(
            file_path, writer, args,
            f"✅ 已添加关系线: {args['start_id']} ↔ {args['end_id']}。",
        )

    async def _remove_relationship(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.remove_relationship(args["relationship_id"])
        if not success:
            return "❌ 找不到该关系线。"
        return self._auto_save_msg(
            file_path, writer, args, f"✅ 已删除关系线 {args['relationship_id']}。",
        )

    async def _remove_boundary(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        success = writer.remove_boundary(args["boundary_id"])
        if not success:
            return "❌ 找不到该边界。"
        return self._auto_save_msg(
            file_path, writer, args, f"✅ 已删除边界 {args['boundary_id']}。",
        )

    async def _apply_style_preset(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        try:
            count = writer.apply_style_preset(args["preset"])
        except ValueError as e:
            return f"❌ {e}"
        return self._auto_save_msg(
            file_path, writer, args, f"✅ 已按预设「{args['preset']}」更新 {count} 个节点。",
        )

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
        return self._auto_save_msg(file_path, writer, args, msg)

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
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _get_node(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        node = xm.find_topic(args["node_id"])
        if node is None:
            return f"❌ 找不到节点: {args['node_id']}"

        lines = [
            f"ID: {node.id}",
            f"标题: {node.title or '（无标题）'}",
            f"层级: L{node.level}",
            f"类型: {node.topic_class}",
            f"主题类型: {node.xmind_class or 'topic'}",
            f"父节点: {node.parent_id or '（根主题）'}",
            f"路径: {_build_node_path(node, xm)}",
        ]
        if node.notes:
            lines.append(f"备注:\n{node.notes}")
        if node.markers:
            lines.append(f"标记: {', '.join(node.markers)}")
        if node.labels:
            lines.append(f"标签: {', '.join(node.labels)}")
        if node.hyperlink:
            lines.append(f"超链接: {node.hyperlink}")
        if node.structure_class:
            lines.append(f"布局: {node.structure_class}")
        if node.image_src:
            lines.append(f"图片: {node.image_src}")
        if node.style:
            props = node.style.get("properties", {})
            if props:
                lines.append(f"样式(节点自身): {json.dumps(props, ensure_ascii=False)}")
        effective = _effective_style(node, xm)
        if effective:
            lines.append(f"样式(含主题继承): {json.dumps(effective, ensure_ascii=False)}")
        raw = node._raw or {}
        if raw.get("summaries"):
            lines.append(f"概要({len(raw['summaries'])}): {json.dumps(raw['summaries'], ensure_ascii=False)}")
        if raw.get("boundaries"):
            lines.append(f"边界({len(raw['boundaries'])}): {json.dumps(raw['boundaries'], ensure_ascii=False)}")
        if node.children:
            children = [f"{c.title or '（无标题）'} ({c.id})" for c in node.children]
            lines.append(f"子节点 ({len(node.children)}): {', '.join(children)}")
        return "\n".join(lines)

    async def _create_file(self, args: dict) -> str:
        file_path = args["file_path"]
        topics = args.get("topics") or []
        writer = XMindWriter.create(
            file_path,
            args["root_title"],
            sheet_title=args.get("sheet_title", "画布 1"),
            topics=topics,
        )
        xm = writer.xm
        self.workspace._cache[os.path.abspath(file_path)] = (
            os.path.getmtime(file_path),
            xm,
            writer,
        )
        node_count = len(xm.all_topics())
        return f"✅ 已创建文件: {file_path}\n根主题: {args['root_title']}\n节点总数: {node_count}"

    async def _import_markdown(self, args: dict) -> str:
        file_path = args["file_path"]
        markdown = args["markdown"]
        root_title, topics = _parse_markdown_outline(markdown)
        writer = XMindWriter.create(
            file_path,
            root_title,
            sheet_title=args.get("sheet_title", root_title or "画布 1"),
            topics=topics,
        )
        xm = writer.xm
        self.workspace._cache[os.path.abspath(file_path)] = (
            os.path.getmtime(file_path),
            xm,
            writer,
        )
        node_count = len(xm.all_topics())
        return f"✅ 已从 Markdown 创建文件: {file_path}\n根主题: {root_title}\n节点总数: {node_count}"

    async def _import_markdown_file(self, args: dict) -> str:
        md_path = args["file_path"]
        output_path = args["output_path"]

        raw = Path(md_path).read_bytes()
        markdown = ""
        for encoding in ("utf-8", "gb18030"):
            try:
                markdown = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not markdown:
            raise ValueError(f"无法读取 Markdown 文件（不是 UTF-8 或 GBK 编码）: {md_path}")

        root_title, topics = _parse_markdown_outline(markdown)
        writer = XMindWriter.create(
            output_path,
            root_title,
            sheet_title=args.get("sheet_title", root_title or "画布 1"),
            topics=topics,
        )
        xm = writer.xm
        self.workspace._cache[os.path.abspath(output_path)] = (
            os.path.getmtime(output_path),
            xm,
            writer,
        )
        node_count = len(xm.all_topics())
        return (
            f"✅ 已原样转换 Markdown: {output_path}\n"
            f"来源文件: {md_path}\n根主题: {root_title}\n节点总数: {node_count}"
        )

    async def _repair_file(self, args: dict) -> str:
        file_path = args["file_path"]
        output_path = args.get("output_path")
        writer = XMindWriter.repair(file_path, output_path)
        saved = str(output_path) if output_path else file_path
        node_count = len(writer.xm.all_topics())
        return f"✅ 已修复文件: {saved}\n节点总数: {node_count}"

    async def _apply_to_matching(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        style = args.get("style") or None
        try:
            matched = writer.apply_matching(
                args["keyword"],
                style=style,
                add_markers=args.get("add_markers"),
                remove_markers=args.get("remove_markers"),
                add_labels=args.get("add_labels"),
                remove_labels=args.get("remove_labels"),
            )
        except (ValueError, TypeError) as e:
            return f"❌ {e}"
        if not matched:
            return f"❌ 没有找到标题包含 '{args['keyword']}' 的节点。"
        msg = f"✅ 已对 {len(matched)} 个节点应用修改：{', '.join(matched)}"
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _analyze_file(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        nodes = xm.all_topics()
        if not nodes:
            return "文件中没有节点。"

        max_depth = max(n.level for n in nodes)
        total = len(nodes)
        with_notes = sum(1 for n in nodes if n.notes)
        with_images = len(xm.find_topics_with_image())
        with_markers = sum(1 for n in nodes if n.markers)
        with_labels = sum(1 for n in nodes if n.labels)
        with_hyperlink = sum(1 for n in nodes if n.hyperlink)
        empty_titles = [n for n in nodes if not n.title.strip()]
        non_attached = [n for n in nodes if n.topic_class not in ("root", "attached")]

        relationship_count = 0
        summary_count = 0
        boundary_count = 0

        def _count_visuals(topic: dict) -> None:
            nonlocal summary_count, boundary_count
            summary_count += len(topic.get("summaries") or [])
            boundary_count += len(topic.get("boundaries") or [])
            children = topic.get("children", {})
            if isinstance(children, dict):
                for key in ("attached", "detached", "summary", "callout"):
                    for child in children.get(key, []):
                        if isinstance(child, dict):
                            _count_visuals(child)

        for sheet in xm.raw_content:
            relationship_count += len(sheet.get("relationships") or [])
            if isinstance(sheet.get("rootTopic"), dict):
                _count_visuals(sheet["rootTopic"])

        title_counts: dict[str, int] = {}
        for n in nodes:
            t = n.title.strip().lower()
            if t:
                title_counts[t] = title_counts.get(t, 0) + 1
        duplicates = {t: c for t, c in title_counts.items() if c > 1}

        class_counts: dict[str, int] = {}
        for n in nodes:
            if n.xmind_class and n.xmind_class != "topic":
                class_counts[n.xmind_class] = class_counts.get(n.xmind_class, 0) + 1

        lines = [
            f"📊 文件统计",
            f"工作表: {len(xm.sheets)}",
            f"节点总数: {total}（含根主题）",
            f"最大深度: L{max_depth}",
            f"带备注: {with_notes}",
            f"带图片: {with_images}",
            f"带标记: {with_markers}",
            f"带标签: {with_labels}",
            f"带超链接: {with_hyperlink}",
            f"概要: {summary_count}",
            f"边界: {boundary_count}",
            f"关系线: {relationship_count}",
            f"空标题节点: {len(empty_titles)}",
            f"概要/批注/浮动节点: {len(non_attached)}",
        ]
        if duplicates:
            dup_text = "、".join(f"「{t}」x{c}" for t, c in list(duplicates.items())[:20])
            lines.append(f"重复标题: {dup_text}")
        else:
            lines.append("重复标题: 无")
        if class_counts:
            class_text = ", ".join(f"{k} x{v}" for k, v in class_counts.items())
            lines.append(f"主题类型: {class_text}")
        if empty_titles:
            ids = ", ".join(n.id for n in empty_titles[:20])
            lines.append(f"空标题节点 ID: {ids}")
        if non_attached:
            ids = ", ".join(f"{n.id}({n.topic_class})" for n in non_attached[:20])
            lines.append(f"特殊节点 ID: {ids}")
        return "\n".join(lines)

    def _auto_save_msg(self, file_path: str, writer: XMindWriter, args: dict, msg: str) -> str:
        """编辑后统一处理保存提示。"""
        if args.get("auto_save"):
            saved_path = writer.save()
            self.workspace.refresh_after_save(saved_path)
            return f"{msg}\n✅ 已自动保存到: {saved_path}"
        return f"{msg}\n⚠️ 修改未保存，请调用 save_file 或设置 auto_save=true。"

    async def _get_hardware_info(self, args: dict) -> str:
        c = self.config
        ok, msg = verify_installed(c.ocr_engine, c.device)
        lines = [
            f"🖥️ 操作系统: {c.os}",
            f"🔧 OCR 引擎: {c.ocr_engine}",
            f"⚡ 推理设备: {c.device}",
            f"✅ 设备状态: {'可用 - ' + msg if ok else '不可用 - ' + msg}",
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
        "set_sheet_layout": _set_sheet_layout,
        "apply_style_preset": _apply_style_preset,
        "add_image": _add_image,
        "add_summary": _add_summary,
        "add_boundary": _add_boundary,
        "add_relationship": _add_relationship,
        "remove_relationship": _remove_relationship,
        "remove_boundary": _remove_boundary,
        "set_hyperlink": _set_hyperlink,
        "add_marker": _add_marker,
        "remove_marker": _remove_marker,
        "add_label": _add_label,
        "remove_label": _remove_label,
        "add_nodes": _add_nodes,
        "duplicate_node": _duplicate_node,
        "get_node": _get_node,
        "create_file": _create_file,
        "import_markdown": _import_markdown,
        "import_markdown_file": _import_markdown_file,
        "repair_file": _repair_file,
        "apply_to_matching": _apply_to_matching,
        "analyze_file": _analyze_file,
        "get_hardware_info": _get_hardware_info,
    }


def _export_node_md(node: TopicNode, depth: int, lines: list[str], include_ids: bool) -> None:
    """递归导出 Markdown 大纲。"""
    indent = "  " * depth
    title = _clean_title(node.title) or "📷图片"
    id_part = f" `id:{node.id}`" if include_ids else ""
    lines.append(f"{indent}- {_md_escape(title)}{id_part}")
    for child in node.children:
        _export_node_md(child, depth + 1, lines, include_ids)


def _parse_markdown_outline(markdown: str) -> tuple[str, list[dict]]:
    """把 Markdown 逐行原样解析成 create_file 用的嵌套结构。

    这是“原封不动”模式：除了去掉标题/列表/引用/围栏代码的标记符号外，
    所有文字（表格行、代码块内容、行内格式）都会保留，不做概括或精简。
    纯分隔线（--- / *** / ___）不产生节点。
    """
    items: list[tuple[int, str]] = []
    in_fence = False
    current_heading = 0
    for raw_line in markdown.splitlines():
        stripped_line = raw_line.strip()
        if stripped_line.startswith("```"):
            in_fence = not in_fence
            continue  # 围栏标记本身不保留，里面的内容保留
        if not stripped_line:
            continue
        # 纯分隔线不是内容，跳过
        if not in_fence and re.fullmatch(r"[-*_]{3,}", stripped_line):
            continue

        indent = len(raw_line) - len(raw_line.lstrip())
        if in_fence:
            # 代码块内容：原样保留，挂在最近的标题/列表下面
            base = current_heading + 1 if current_heading else 0
            items.append((base + indent // 2, stripped_line))
        elif stripped_line.startswith("#"):
            heading = stripped_line.lstrip("#")
            level = len(stripped_line) - len(heading)
            text = heading.strip()
            current_heading = level
            items.append((level, text))
        else:
            # 去掉行首的列表符号和引用符号，保留其余原文
            text = re.sub(r"^(?:[-*+>]\s*)+", "", stripped_line).strip()
            # 列表行挂在“最近的标题”下面；没有标题时第一行列表就是根
            base = current_heading + 1 if current_heading else 0
            items.append((base + indent // 2, text))

    if not items:
        raise ValueError("Markdown 内容为空，无法创建文件")

    # 整体归一化：最小层级变成 1，保证第一项是根
    min_level = min(level for level, _ in items)
    normalized = [(max(level - min_level + 1, 1), text) for level, text in items]

    # 第一项作为根主题，其余按层级挂到树上
    root_level, root_title = normalized[0]
    root: dict = {"title": root_title, "children": []}
    stack: list[tuple[int, dict]] = [(root_level, root)]
    for level, text in normalized[1:]:
        while len(stack) > 1 and level <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        child: dict = {"title": text, "children": []}
        parent["children"].append(child)
        stack.append((level, child))

    return root_title, root["children"]
