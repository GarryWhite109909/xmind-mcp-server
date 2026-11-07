"""MCP 工具定义和处理逻辑。

工具列表：
  读取类:
    read_structure    — 读取节点树结构（默认带 ID）
    read_all          — 读取结构 + 图片 OCR + 节点元数据
    list_images       — 列出所有含图片的节点
    read_image_ocr    — 对指定节点的图片做 OCR
    get_image         — 把节点原图作为图片返回给多模态模型
    get_preview       — 返回 XMind 自带的整图缩略图
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
    set_branch_style  — 设置分支连线类型/终点样式/粗细/颜色
    set_theme_style   — 设置主题级默认样式（中心/分支/细分/自由主题）
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
import io
import json
import os
import re
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Optional

from mcp import types

from .config import Config
from .formula import render_latex_png, try_extract_single_line
from .hardware.installer import verify_installed
from .image_payload import DEFAULT_MAX_SIDE, PreparedImage, prepare_image
from .xmind_parser import (
    parse_xmind,
    load_preview_image,
    load_topic_image,
    XMindFile,
    TopicNode,
)
from .xmind_props import THEME_LEVELS
from .xmind_writer import XMindWriter, list_backups, restore_backup
from .ocr.manager import OCRManager


# ---------- 文件缓存 ----------

class XMindWorkspace:
    """缓存已解析的 XMind 文件，避免每次工具调用都重新解析。"""

    def __init__(self):
        # {path: (mtime, XMindFile, XMindWriter)}
        self._cache: dict[str, tuple[float, XMindFile, XMindWriter]] = {}
        # MCP 可能并发调用，缓存读写加锁避免同一文件被两个请求同时改写
        self._lock = threading.Lock()

    def get(self, path: str) -> tuple[XMindFile, XMindWriter]:
        path = os.path.abspath(path)
        mtime = os.path.getmtime(path)

        with self._lock:
            cached = self._cache.get(path)
            if cached and cached[0] == mtime:
                return cached[1], cached[2]

            xm = parse_xmind(path)
            writer = XMindWriter(xm)
            self._cache[path] = (mtime, xm, writer)
            return xm, writer

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._cache.pop(os.path.abspath(path), None)

    def refresh_after_save(self, path: str) -> tuple[XMindFile, XMindWriter]:
        """保存后刷新缓存。"""
        self.invalidate(path)
        return self.get(path)


# ---------- 格式化辅助 ----------

# 单次输出里备注/OCR 文本的最大长度，防止超长内容撑爆模型上下文
MAX_NOTE_CHARS = 2000
MAX_OCR_CHARS = 2000

# 一次读取最多内联多少张图，超出部分只给坐标提示
MAX_INLINE_IMAGES = 8

# auto 表示“跟随配置”，其余是显式模式
_IMAGE_MODES = ("off", "ocr", "inline", "both", "auto")


def _text_block(text: str) -> types.TextContent:
    return types.TextContent(type="text", text=text)


def _image_block(image: PreparedImage) -> types.ImageContent:
    # mcp 2.x 的字段名是 mime_type（序列化时自动转成 mimeType）
    return types.ImageContent(type="image", data=image.base64, mime_type=image.mime_type)


def _content_blocks(result) -> list:
    """handler 可以返回纯文本，也可以返回带图片的内容块列表。"""
    if isinstance(result, str):
        return [_text_block(result)]
    return list(result)


def _resolve_image_mode(value: Optional[str], config: Config) -> str:
    """把 auto 解析成配置里的模式；非法值一律按配置处理。"""
    mode = (value or "auto").lower()
    if mode not in _IMAGE_MODES:
        mode = "auto"
    if mode == "auto":
        mode = (getattr(config, "image_mode", "") or "inline").lower()
        if mode not in ("off", "ocr", "inline", "both"):
            mode = "inline"
    return mode


def _prepare_image_arg(data: Optional[bytes], max_side) -> Optional[PreparedImage]:
    if not data:
        return None
    return prepare_image(data, max_side=max_side or DEFAULT_MAX_SIDE)


def _truncate(text: str, limit: int, hint: str = "已截断") -> str:
    """超长文本截断并标注，保留完整内容的获取路径由调用方提示。"""
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"…（{hint}）"


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
    text = _truncate(text or "", MAX_NOTE_CHARS, "备注过长已截断，可用 get_node 查看完整内容")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
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


def _iter_visible_topics(
    xm: XMindFile,
    max_depth: Optional[int] = None,
    max_nodes: Optional[int] = None,
):
    """按输出顺序遍历“会被渲染出来”的节点（与树格式化共用相同的截断规则）。

    供 read_all 决定哪些图片需要 OCR：max_depth/max_nodes 之外的图片
    不会出现在输出里，也就不必花时间识别。
    """
    count = 0
    for sheet in xm.sheets:
        stack = [sheet.root]
        while stack:
            node = stack.pop()
            if max_nodes is not None and count >= max_nodes:
                return
            count += 1
            yield node
            if max_depth is not None and node.level >= max_depth:
                continue
            for child in reversed(node.children):
                stack.append(child)


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
    """合并“工作表主题里的层级/类型默认样式 + 节点自身样式”。

    顺序：主题层级默认样式（中心/分支/第三层/细分/自由主题）
    → 主题类型样式（importantTopic 等）
    → 节点自身样式（后者覆盖前者）。
    例如 importantTopic 主题自带紫色填充/加粗，节点自身只写了字体和形状，
    合并后模型才能看到这个节点实际长什么样。
    """
    props: dict[str, str] = {}
    if node.sheet_index < len(xm.raw_content):
        sheet = xm.raw_content[node.sheet_index]
        theme = sheet.get("theme") if isinstance(sheet, dict) else None
        if isinstance(theme, dict):
            theme_keys: list[str] = []
            if node.topic_class == "detached":
                theme_keys.append("floatingTopic")
            elif node.topic_class not in ("summary", "callout"):
                level_key = {
                    0: "centralTopic",
                    1: "mainTopic",
                    2: "subTopic",
                }.get(node.level, "level3")
                theme_keys.append(level_key)
            if node.xmind_class and node.xmind_class != "topic":
                theme_keys.append(node.xmind_class)
            for key in theme_keys:
                entry = theme.get(key)
                if isinstance(entry, dict):
                    entry_props = entry.get("properties")
                    if isinstance(entry_props, dict):
                        props.update(entry_props)
    node_props = (node.style or {}).get("properties")
    if isinstance(node_props, dict):
        props.update(node_props)
    return props


# ---------- 工具定义 ----------

# 只读工具：不改文件、不写盘，客户端可以放心自动批准
_READ_ONLY_TOOLS = {
    "read_structure", "read_all", "list_images", "read_image_ocr",
    "get_image", "get_preview", "find_node", "get_node",
    "analyze_file", "analyze_style", "export_markdown", "export_text",
    "list_sheets", "list_attachments", "list_backups", "get_hardware_info",
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
            "一步到位：读取节点树 + 图片 + notes/labels/markers/超链接/样式元数据。默认把每个图片节点的原图作为图片一起返回（适合多模态模型）；纯文本模型用 image_mode=ocr 只要文字。",
            {
                "file_path": file_path_prop,
                "include_ids": {"type": "boolean", "description": "是否在每行后输出节点 ID（默认 true）"},
                "clean": {"type": "boolean", "description": "是否去掉 📷/📝/🔖 等内部标记（默认 false）"},
                "include_style": {"type": "boolean", "description": "是否输出每个节点的样式属性（默认 false）"},
                "max_depth": {"type": "integer", "description": "只输出到该层级（可选，根主题为 L0）"},
                "max_nodes": {"type": "integer", "description": "最多输出多少个节点，超出截断（可选）"},
                "image_mode": {
                    "type": "string",
                    "enum": ["auto", "inline", "ocr", "both", "off"],
                    "description": "图片处理方式：inline=返回原图（默认），ocr=只给识别文字，both=两者，off=都不给",
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
            "read_image_ocr",
            "对指定节点的图片做 OCR 识别，返回图片中的文字。需要先用 read_structure 或 list_images 获取 node_id。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "包含图片的节点 ID"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "get_image",
            "读取节点的图片本身并作为图片返回，多模态模型可直接看图（图表、截图、手写内容比 OCR 文本准得多）。默认把长边压到 1024、转 JPEG；需要文字时加 include_ocr=true。",
            {
                "file_path": file_path_prop,
                "node_id": {"type": "string", "description": "包含图片的节点 ID"},
                "max_side": {"type": "integer", "description": "长边像素上限（默认 1024）"},
                "include_ocr": {"type": "boolean", "description": "是否同时返回 OCR 文本（默认 false）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "get_preview",
            "返回 XMind 保存时生成的整图缩略图（Thumbnails/thumbnail.png）。这是 XMind 自己渲染的画面，最接近用户在软件里看到的效果，适合让模型先看一眼整体版式和配色。",
            {
                "file_path": file_path_prop,
                "max_side": {"type": "integer", "description": "长边像素上限（默认 1024）"},
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
            "get_hardware_info",
            "获取当前机器的硬件检测结果和 OCR 引擎配置信息。",
            {},
            [],
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

    async def call_tool(self, name: str, arguments: dict) -> list:
        messages, _ = await self.call_tool_status(name, arguments)
        return messages

    async def call_tool_status(self, name: str, arguments: dict) -> tuple[list, bool]:
        """调用工具并返回 (内容块, 是否错误)。错误信息同时让 MCP 标记 isError。

        内容块可以是 TextContent，也可以是 ImageContent —— 多模态客户端
        才能看到节点原图，而不是只能读 OCR 转出来的文字。
        """
        handler = self._handlers.get(name)
        if handler is None:
            return [_text_block(f"未知工具: {name}")], True

        try:
            result = await handler(self, arguments)
            return _content_blocks(result), False
        except FileNotFoundError as e:
            return [_text_block(f"❌ 文件不存在或路径错误: {e}")], True
        except PermissionError as e:
            return [_text_block(f"❌ 文件被占用或无权限访问: {e}")], True
        except Exception as e:
            return [_text_block(f"❌ 错误: {type(e).__name__}: {e}")], True

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

    async def _get_image(self, args: dict) -> list:
        """把节点图片作为图片内容返回，交给多模态模型直接看。"""
        xm, _ = self.workspace.get(args["file_path"])
        node = xm.find_topic(args["node_id"])
        if node is None:
            return [_text_block(f"找不到节点: {args['node_id']}")]
        if not node.image_src:
            return [_text_block(f"节点「{_clean_title(node.title)}」没有图片。")]

        image_data = load_topic_image(node, xm)
        if not image_data:
            return [_text_block(f"图片加载失败: {node.image_src}")]

        max_side = args.get("max_side") or self.config.image_max_side
        prepared = _prepare_image_arg(image_data, max_side)
        if prepared is None:
            return [_text_block(
                f"图片无法解码（{node.image_src}），可能是不支持的格式。"
            )]

        title = _clean_title(node.title) or "（无标题）"
        caption = (
            f"📷 节点「{title}」(ID: {node.id}) 的图片："
            f"{prepared.source_width}×{prepared.source_height} → "
            f"{prepared.width}×{prepared.height}，{prepared.size_kb}KB"
        )
        blocks: list = [_text_block(caption), _image_block(prepared)]

        if args.get("include_ocr"):
            try:
                result = await asyncio.to_thread(self.ocr.recognize, image_data)
                text = _truncate(result.text or "（未识别到文字）", MAX_OCR_CHARS)
                blocks.append(_text_block(f"↳ [OCR] {text}"))
            except Exception as e:
                blocks.append(_text_block(f"↳ [OCR] 失败: {type(e).__name__}: {e}"))
        return blocks

    async def _get_preview(self, args: dict) -> list:
        """返回 XMind 自己渲染的整图缩略图。"""
        xm, _ = self.workspace.get(args["file_path"])
        image_data = load_preview_image(xm)
        if not image_data:
            return [_text_block(
                "该文件没有内嵌缩略图。可能原因：文件由本工具保存过（保存时移除了过期缩略图），"
                "或 XMind 从未保存过该文件。\n"
                "可选做法：在 XMind 里打开并保存一次；或用 export_svg 导出示意图；"
                "或直接用 read_all 读取结构。"
            )]

        max_side = args.get("max_side") or self.config.image_max_side
        prepared = _prepare_image_arg(image_data, max_side)
        if prepared is None:
            return [_text_block("缩略图存在但无法解码，文件可能已损坏，可用 repair_file 修复。")]

        sheet_titles = "、".join(s.title for s in xm.sheets) or "（无工作表）"
        caption = (
            f"🖼️ 文件缩略图（XMind 保存时渲染）：{prepared.source_width}×"
            f"{prepared.source_height} → {prepared.width}×{prepared.height}，"
            f"{prepared.size_kb}KB\n工作表：{sheet_titles}\n"
            "注意：这是整份文件最近一次保存时的画面，多工作表只展示其中一张。"
        )
        return [_text_block(caption), _image_block(prepared)]

    async def _read_all(self, args: dict):
        xm, _ = self.workspace.get(args["file_path"])
        include_ids = args.get("include_ids", True)
        clean = args.get("clean", False)
        include_style = args.get("include_style", False)
        max_depth = args.get("max_depth")
        max_nodes = args.get("max_nodes")
        image_mode = _resolve_image_mode(args.get("image_mode"), self.config)
        max_side = args.get("image_max_side") or self.config.image_max_side
        try:
            max_images = max(0, int(args.get("max_images", MAX_INLINE_IMAGES)))
        except (TypeError, ValueError):
            max_images = MAX_INLINE_IMAGES

        # 只处理会出现在输出里的图片节点，
        # 避免 max_depth/max_nodes 截断后仍全量识别或内联
        visible_ids = {n.id for n in _iter_visible_topics(xm, max_depth, max_nodes)}
        image_nodes = [n for n in xm.find_topics_with_image() if n.id in visible_ids]

        # OCR 只在显式要求时跑：多模态模型看原图比 OCR 文本准得多，
        # 还省掉 CPU 上每张 0.5~2 秒的识别时间。
        ocr_results: dict[str, str] = {}
        if image_mode in ("ocr", "both"):
            for node in image_nodes:
                try:
                    image_data = load_topic_image(node, xm)
                    if image_data:
                        result = await asyncio.to_thread(self.ocr.recognize, image_data)
                        ocr_results[node.id] = _truncate(
                            result.text or "（未识别到文字）",
                            MAX_OCR_CHARS,
                            "OCR 结果过长已截断，可用 read_image_ocr 查看完整内容",
                        )
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

        text = "\n".join(lines)
        if image_mode not in ("inline", "both") or not image_nodes:
            return text

        # 图片附在文本之后：客户端按顺序渲染，模型能对照节点 ID 找到对应图
        blocks: list = [_text_block(text)]
        deferred = []
        sent = 0
        for node in image_nodes:
            title = _clean_title(node.title) or "（无标题）"
            if sent >= max_images:
                deferred.append(f"节点 {node.id}「{title}」")
                continue
            prepared = _prepare_image_arg(load_topic_image(node, xm), max_side)
            if prepared is None:
                deferred.append(f"节点 {node.id}「{title}」（无法解码）")
                continue
            blocks.append(_text_block(
                f"📷 图 {sent + 1} ← 节点 {node.id}「{title}」"
                f"（{prepared.width}×{prepared.height}，{prepared.size_kb}KB）"
            ))
            blocks.append(_image_block(prepared))
            sent += 1

        if deferred:
            blocks.append(_text_block(
                f"另有 {len(deferred)} 张图未内联（超过 max_images={max_images} 或无法解码）："
                + "；".join(deferred)
                + "\n可调大 max_images，或用 get_image 单张读取。"
            ))
        return blocks

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
        return f"✅ 已保存到: {saved_path}{self._preview_note(writer)}"

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
            "importance", "font_color", "font_size", "font_family", "font_weight",
            "font_style", "font_transform", "font_decoration",
            "background_color", "fill_pattern", "border_color", "border_width", "border_pattern",
            "shape", "line_color", "line_width", "line_pattern", "text_align",
        )}
        success = writer.set_node_style(args["node_id"], **kwargs)
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 节点 {args['node_id']} 样式已更新。")

    async def _set_branch_style(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        kwargs = {k: args.get(k) for k in (
            "line_type", "arrow_end", "line_color", "line_width", "line_pattern",
        )}
        success = writer.set_branch_style(args["node_id"], **kwargs)
        if not success:
            return f"❌ 找不到节点: {args['node_id']}"
        return self._auto_save_msg(file_path, writer, args, f"✅ 节点 {args['node_id']} 的分支样式已更新。")

    async def _set_theme_style(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        kwargs = {k: args.get(k) for k in (
            "font_color", "font_size", "font_family", "font_weight",
            "font_style", "font_transform", "font_decoration",
            "background_color", "fill_pattern", "border_color", "border_width", "border_pattern",
            "shape", "line_color", "line_width", "line_pattern", "text_align",
        )}
        success = writer.set_theme_style(
            sheet_index=args.get("sheet_index", 0),
            level=args.get("level", "分支主题"),
            **kwargs,
        )
        if not success:
            return "❌ 工作表索引无效。"
        return self._auto_save_msg(file_path, writer, args, f"✅ 主题层级「{args.get('level', '分支主题')}」的默认样式已更新。")

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
        if args.get("preview"):
            nodes = xm.all_topics()
            root_count = sum(1 for n in nodes if n.level == 0)
            branch_count = sum(1 for n in nodes if n.level == 1)
            leaf_count = len(nodes) - root_count - branch_count
            return (
                f"🔍 预览：预设「{args['preset']}」将影响全部 {len(nodes)} 个节点。\n"
                f"  中心主题: {root_count}，分支主题: {branch_count}，其他: {leaf_count}\n"
                "  未做任何修改，确认后请去掉 preview=true 再执行。"
            )
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
        root_title, topics, root_meta = _parse_markdown_outline(markdown)
        writer = XMindWriter.create(
            file_path,
            root_title,
            sheet_title=args.get("sheet_title", root_title or "画布 1"),
            topics=topics,
            root_meta=root_meta,
        )
        xm = writer.xm
        eq_count = _inject_equations(xm, writer, topics, root_meta)
        self.workspace._cache[os.path.abspath(file_path)] = (
            os.path.getmtime(file_path),
            xm,
            writer,
        )
        node_count = len(xm.all_topics())
        eq_note = f"\n公式: {eq_count} 个（已渲染为图片）" if eq_count else ""
        return f"✅ 已从 Markdown 创建文件: {file_path}\n根主题: {root_title}\n节点总数: {node_count}{eq_note}"

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

        root_title, topics, root_meta = _parse_markdown_outline(markdown)
        writer = XMindWriter.create(
            output_path,
            root_title,
            sheet_title=args.get("sheet_title", root_title or "画布 1"),
            topics=topics,
            root_meta=root_meta,
        )
        xm = writer.xm
        eq_count = _inject_equations(xm, writer, topics, root_meta)
        self.workspace._cache[os.path.abspath(output_path)] = (
            os.path.getmtime(output_path),
            xm,
            writer,
        )
        node_count = len(xm.all_topics())
        eq_note = f"\n公式: {eq_count} 个（已渲染为图片）" if eq_count else ""
        return (
            f"✅ 已原样转换 Markdown: {output_path}\n"
            f"来源文件: {md_path}\n根主题: {root_title}\n节点总数: {node_count}{eq_note}"
        )

    async def _repair_file(self, args: dict) -> str:
        file_path = args["file_path"]
        output_path = args.get("output_path")
        writer = XMindWriter.repair(file_path, output_path)
        # 修复后让缓存重新从磁盘读取（覆盖原文件时 mtime 变化也会触发，
        # 这里显式失效，避免旧对象残留）
        self.workspace.invalidate(file_path)
        saved = str(output_path) if output_path else file_path
        node_count = len(writer.xm.all_topics())
        return f"✅ 已修复文件: {saved}\n节点总数: {node_count}"

    async def _apply_to_matching(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        if args.get("preview"):
            keyword = args["keyword"].lower()
            matched = [
                n for n in xm.all_topics()
                if keyword in (n.title or "").lower()
            ]
            if not matched:
                return f"❌ 没有找到标题包含 '{args['keyword']}' 的节点。"
            lines = [f"🔍 预览：将修改 {len(matched)} 个节点："]
            for n in matched[:50]:
                lines.append(f"  • {n.id}  {_clean_title(n.title) or '（无标题）'}")
            if len(matched) > 50:
                lines.append(f"  …（其余 {len(matched) - 50} 个省略）")
            lines.append("未做任何修改，确认后请去掉 preview=true 再执行。")
            return "\n".join(lines)
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

    @staticmethod
    def _preview_note(writer: XMindWriter) -> str:
        if not getattr(writer, "dropped_preview", False):
            return ""
        return "\nℹ️ 已移除过期缩略图（内容已变，旧图会误导预览）；XMind 下次保存时会重新生成。"

    def _auto_save_msg(self, file_path: str, writer: XMindWriter, args: dict, msg: str) -> str:
        """编辑后统一处理保存提示。"""
        if args.get("auto_save"):
            saved_path = writer.save()
            self.workspace.refresh_after_save(saved_path)
            return f"{msg}\n✅ 已自动保存到: {saved_path}{self._preview_note(writer)}"
        return f"{msg}\n⚠️ 修改未保存，请调用 save_file 或设置 auto_save=true。"

    async def _list_backups(self, args: dict) -> str:
        backups = list_backups(args["file_path"])
        if not backups:
            return "该文件还没有自动备份。"
        lines = [f"找到 {len(backups)} 份备份（新到旧）："]
        for i, backup in enumerate(backups, 1):
            lines.append(
                f"{i}. {backup['name']}（{backup['mtime']}，{backup['size']} bytes）\n"
                f"   路径: {backup['path']}"
            )
        return "\n".join(lines)

    async def _restore_backup(self, args: dict) -> str:
        file_path = args["file_path"]
        try:
            saved = restore_backup(file_path, args["backup_path"])
        except (FileNotFoundError, PermissionError, OSError) as e:
            return f"❌ 恢复失败: {type(e).__name__}: {e}"
        self.workspace.invalidate(file_path)
        return (
            f"✅ 已从备份恢复: {saved}\n"
            "⚠️ 恢复前的当前文件也已自动备份，可用 list_backups 找回。"
        )

    async def _export_svg(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        output = args.get("output_path") or str(Path(args["file_path"]).with_suffix(".svg"))
        svg, stats = _build_svg(xm)
        Path(output).write_text(svg, encoding="utf-8")
        return (
            f"✅ 已导出 SVG 示意图: {output}\n"
            f"工作表: {stats['sheets']}，节点: {stats['nodes']}\n"
            "说明: SVG 是给人工查看的示意图；模型请用 analyze_style 获取结构化样式反馈。"
        )

    async def _analyze_style(self, args: dict) -> str:
        from collections import Counter

        xm, _ = self.workspace.get(args["file_path"])
        sheet_index = args.get("sheet_index", 0)
        if sheet_index < 0 or sheet_index >= len(xm.sheets):
            return "❌ 工作表索引无效。"
        sheet_raw = xm.raw_content[sheet_index]
        theme = sheet_raw.get("theme") if isinstance(sheet_raw, dict) else None
        lines = [f"🎨 工作表「{sheet_raw.get('title', '')}」样式审计"]

        if isinstance(theme, dict):
            lines.append("")
            lines.append("主题层级默认样式：")
            label_map = {value: key for key, value in THEME_LEVELS.items()}
            for key, label in label_map.items():
                entry = theme.get(key)
                if isinstance(entry, dict):
                    props = entry.get("properties")
                    if isinstance(props, dict) and props:
                        prop_text = ", ".join(f"{k}={v}" for k, v in props.items())
                        lines.append(f"  • {label}: {prop_text}")

        nodes = [n for n in xm.all_topics() if n.sheet_index == sheet_index]
        fills: Counter = Counter()
        colors: Counter = Counter()
        fonts: Counter = Counter()
        shapes: Counter = Counter()
        branch_lines: Counter = Counter()
        node_style_count = 0
        for n in nodes:
            eff = _effective_style(n, xm)
            if eff.get("svg:fill"):
                fills[eff["svg:fill"]] += 1
            if eff.get("fo:color"):
                colors[eff["fo:color"]] += 1
            if eff.get("fo:font-family"):
                fonts[eff["fo:font-family"]] += 1
            if eff.get("shape-class"):
                shapes[eff["shape-class"]] += 1
            if eff.get("line-class"):
                branch_lines[eff["line-class"]] += 1
            if (n.style or {}).get("properties"):
                node_style_count += 1

        def _counter_text(title: str, counter: Counter) -> str:
            if not counter:
                return f"{title}: 无"
            items = ", ".join(f"{k}×{v}" for k, v in counter.most_common(10))
            return f"{title}: {items}"

        lines.append("")
        lines.append(f"节点总数: {len(nodes)}，带节点级样式: {node_style_count}")
        lines.append(_counter_text("背景色", fills))
        lines.append(_counter_text("文字色", colors))
        lines.append(_counter_text("字体", fonts))
        lines.append(_counter_text("形状", shapes))
        lines.append(_counter_text("分支线型", branch_lines))

        if args.get("include_node_styles"):
            max_nodes = min(int(args.get("max_nodes", 50) or 50), 200)
            lines.append("")
            lines.append(f"节点有效样式（前 {min(max_nodes, len(nodes))} 个）：")
            for n in nodes[:max_nodes]:
                eff = _effective_style(n, xm)
                if eff:
                    title = _clean_title(n.title) or "（无标题）"
                    lines.append(
                        f"  • {n.id} {title}: {json.dumps(eff, ensure_ascii=False)}"
                    )
        return "\n".join(lines)

    async def _list_attachments(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        referenced = {
            n.image_src[4:]
            for n in xm.all_topics()
            if n.image_src and n.image_src.startswith("xap:")
        }
        entries: list[str] = []
        try:
            with zipfile.ZipFile(io.BytesIO(xm._zip_bytes)) as zf:
                entries = [name for name in zf.namelist() if not name.endswith("/")]
        except zipfile.BadZipFile:
            return "❌ 文件不是有效的 xmind 包。"
        attachments = [name for name in entries if name.startswith(("attachments/", "resources/"))]
        used = [name for name in attachments if name in referenced]
        unused = [name for name in attachments if name not in referenced]
        lines = [f"附件/资源共 {len(attachments)} 个："]
        lines.append(f"被引用 ({len(used)})：")
        lines.extend(f"  • {name}" for name in used[:50])
        if len(used) > 50:
            lines.append(f"  …（其余 {len(used) - 50} 个）")
        lines.append(f"未被引用 ({len(unused)})：")
        lines.extend(f"  • {name}" for name in unused[:50])
        if len(unused) > 50:
            lines.append(f"  …（其余 {len(unused) - 50} 个）")
        return "\n".join(lines)

    async def _remove_unused_attachments(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        unused = writer.remove_unused_attachments()
        if not unused:
            return "没有发现未引用的附件/图片资源。"
        msg = (
            f"标记移除 {len(unused)} 个未引用资源：\n"
            + "\n".join(f"  • {name}" for name in unused[:50])
            + ("\n  …" if len(unused) > 50 else "")
            + "\n保存后生效。"
        )
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _batch_update_titles(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        try:
            changes = writer.batch_update_titles(
                args["find"],
                args["replace"],
                use_regex=args.get("use_regex", False),
            )
        except ValueError as e:
            return f"❌ {e}"
        if not changes:
            return "没有标题包含匹配内容。"
        lines = [f"已修改 {len(changes)} 个标题："]
        for tid, old, new in changes[:50]:
            lines.append(f"  • {tid}: {old!r} → {new!r}")
        if len(changes) > 50:
            lines.append(f"  …（其余 {len(changes) - 50} 个省略）")
        return self._auto_save_msg(file_path, writer, args, "\n".join(lines))

    async def _delete_empty_nodes(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        deleted = writer.delete_empty_nodes()
        if not deleted:
            return "没有发现空标题节点。"
        msg = f"已删除 {len(deleted)} 个空标题节点（含其子树）。"
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _merge_duplicate_nodes(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        merged = writer.merge_duplicate_nodes()
        if not merged:
            return "没有发现重复标题的兄弟节点。"
        lines = [f"已合并 {len(merged)} 组重复节点："]
        for kept_id, removed_id in merged[:50]:
            lines.append(f"  • {removed_id} → 并入 {kept_id}")
        if len(merged) > 50:
            lines.append(f"  …（其余 {len(merged) - 50} 组省略）")
        return self._auto_save_msg(file_path, writer, args, "\n".join(lines))

    async def _notes_to_children(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        try:
            count = writer.notes_to_children(
                args["node_id"],
                keep_notes=args.get("keep_notes", False),
            )
        except ValueError as e:
            return f"❌ {e}"
        msg = f"已将备注的 {count} 行转换为子节点。"
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _children_to_notes(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        try:
            count = writer.children_to_notes(
                args["node_id"],
                keep_children=args.get("keep_children", False),
            )
        except ValueError as e:
            return f"❌ {e}"
        msg = f"已将 {count} 个子节点标题合并到备注。"
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _auto_number(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        try:
            count = writer.auto_number(mode=args.get("mode", "decimal"))
        except ValueError as e:
            return f"❌ {e}"
        msg = f"已为 {count} 个节点编号。"
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _list_sheets(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        lines = [f"共 {len(xm.sheets)} 张工作表："]
        for i, sheet in enumerate(xm.sheets):
            node_count = sum(1 for n in xm.all_topics() if n.sheet_index == i)
            root_title = _clean_title(sheet.root.title) or "（无标题）"
            lines.append(f"  {i}. {sheet.title}（根主题: {root_title}，节点: {node_count}）")
        return "\n".join(lines)

    async def _add_sheet(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        index = writer.add_sheet(
            title=args.get("title", "画布"),
            root_title=args.get("root_title", "中心主题"),
        )
        msg = f"✅ 已新增工作表（索引 {index}）。"
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _rename_sheet(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        if not writer.rename_sheet(args["sheet_index"], args["title"]):
            return "❌ 工作表索引无效。"
        msg = f"✅ 工作表 {args['sheet_index']} 已重命名为「{args['title']}」。"
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _delete_sheet(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        if not writer.delete_sheet(args["sheet_index"]):
            return "❌ 删除失败：索引无效或文件只剩一张工作表。"
        msg = f"✅ 已删除工作表 {args['sheet_index']}。"
        return self._auto_save_msg(file_path, writer, args, msg)

    async def _move_sheet(self, args: dict) -> str:
        file_path = args["file_path"]
        _, writer = self.workspace.get(file_path)
        if not writer.move_sheet(args["sheet_index"], args["new_index"]):
            return "❌ 移动失败：索引无效。"
        msg = f"✅ 工作表已从 {args['sheet_index']} 移动到 {args['new_index']}。"
        return self._auto_save_msg(file_path, writer, args, msg)

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
        "get_image": _get_image,
        "get_preview": _get_preview,
        "find_node": _find_node,
        "add_node": _add_node,
        "update_node": _update_node,
        "delete_node": _delete_node,
        "move_node": _move_node,
        "save_file": _save_file,
        "export_markdown": _export_markdown,
        "export_text": _export_text,
        "set_node_style": _set_node_style,
        "set_branch_style": _set_branch_style,
        "set_theme_style": _set_theme_style,
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
        "list_backups": _list_backups,
        "restore_backup": _restore_backup,
        "export_svg": _export_svg,
        "analyze_style": _analyze_style,
        "list_attachments": _list_attachments,
        "remove_unused_attachments": _remove_unused_attachments,
        "batch_update_titles": _batch_update_titles,
        "delete_empty_nodes": _delete_empty_nodes,
        "merge_duplicate_nodes": _merge_duplicate_nodes,
        "notes_to_children": _notes_to_children,
        "children_to_notes": _children_to_notes,
        "auto_number": _auto_number,
        "list_sheets": _list_sheets,
        "add_sheet": _add_sheet,
        "rename_sheet": _rename_sheet,
        "delete_sheet": _delete_sheet,
        "move_sheet": _move_sheet,
    }


def _export_node_md(node: TopicNode, depth: int, lines: list[str], include_ids: bool) -> None:
    """递归导出 Markdown 大纲。"""
    indent = "  " * depth
    title = _clean_title(node.title) or "📷图片"
    id_part = f" `id:{node.id}`" if include_ids else ""
    lines.append(f"{indent}- {_md_escape(title)}{id_part}")
    for child in node.children:
        _export_node_md(child, depth + 1, lines, include_ids)


def _strip_inline_markup(text: str) -> tuple[str, bool, bool]:
    """去掉行内的加粗/斜体标记，返回 (干净文本, 是否加粗, 是否斜体)。

    支持 **粗体** / __粗体__ / *斜体* / ***粗斜体***；
    代码块内容不会走到这里（由调用方用 kind="fence" 跳过）。
    """
    bold = False
    italic = False

    def _bold_italic(m: re.Match) -> str:
        nonlocal bold, italic
        bold = True
        italic = True
        return m.group(1)

    def _bold(m: re.Match) -> str:
        nonlocal bold
        bold = True
        return m.group(1)

    def _italic(m: re.Match) -> str:
        nonlocal italic
        italic = True
        return m.group(1)

    text = re.sub(r"\*\*\*(.+?)\*\*\*", _bold_italic, text, flags=re.S)
    text = re.sub(r"\*\*(.+?)\*\*", _bold, text, flags=re.S)
    text = re.sub(r"__(.+?)__", _bold, text, flags=re.S)
    text = re.sub(r"\*([^*]+?)\*", _italic, text, flags=re.S)
    return text.strip(), bold, italic


def _split_table_row(line: str) -> list[str]:
    """拆分表格行，忽略最外层管道符，去掉单元格首尾空格。"""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _is_table_separator(line: str) -> bool:
    """判断是否为 markdown 表格分隔行（如 |---|---|）。"""
    if "|" not in line:
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(
        re.fullmatch(r":?-{1,}:?", cell.replace(" ", ""))
        for cell in cells
    )


def _parse_table_block(lines: list[str]) -> dict:
    """把连续表格行解析成 XMind 风格的行节点 + 单元格子节点。

    与 XMind 软件导入保持一致：
    - 行节点标题 = 第一列内容；
    - 子节点 = 其余列内容，labels = 对应列的表头（帮助模型/用户识别列含义）。
    返回 {"header": 表头单元格列表, "rows": 行节点列表}。
    """
    header = _split_table_row(lines[0])
    rows: list[dict] = []
    for line in lines[2:]:
        cells = _split_table_row(line)
        if not cells:
            continue
        row: dict = {"title": cells[0], "children": []}
        for col in range(1, len(cells)):
            child: dict = {"title": cells[col], "children": []}
            if col < len(header) and header[col]:
                child["labels"] = [header[col]]
            row["children"].append(child)
        rows.append(row)
    return {"header": header, "rows": rows}


def _apply_table_meta(parent: dict, header: list[str]) -> None:
    """把父节点标记成 XMind 原生电子表格结构。

    只有 structureClass + 列定义扩展，XMind 才会把这一支渲染成真正的
    表格（spreadsheet），否则只是普通的“一堆分支”。
    """
    if not header:
        return
    parent["structureClass"] = "org.xmind.ui.spreadsheet"
    columns = [{"name": "leading-column", "content": header[0]}] + [
        {"name": "column", "content": cell}
        for cell in header[1:]
        if cell
    ]
    parent["extensions"] = [{
        "provider": "org.xmind.ui.spreadsheet",
        "content": [{"name": "columns", "content": columns}],
    }]


def _parse_markdown_outline(markdown: str) -> tuple[str, list[dict], dict]:
    """把 Markdown 解析成 create_file 用的嵌套结构。

    处理规则（对齐 XMind 软件导入行为）：
    - 标题/列表：按层级挂树，行首的 **加粗** 不再被误当成列表符号吞掉；
    - 行内 **加粗** / *斜体*：去掉标记，并把样式写进节点（fo:font-weight/italic）；
    - 表格（表头 + 分隔行 + 数据行）：行节点 + 单元格子节点，单元格带上列名标签；
    - 引用块（> 开头）：变成“前一个节点”的备注；
    - 代码块内容原样保留，不做行内解析；
    - 纯分隔线（--- / *** / ___）不产生节点。
    """
    items: list[dict] = []
    in_fence = False
    current_heading = 0
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        stripped_line = raw_line.strip()
        if stripped_line.startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if not stripped_line:
            i += 1
            continue
        # 纯分隔线不是内容，跳过
        if not in_fence and re.fullmatch(r"[-*_]{3,}", stripped_line):
            i += 1
            continue

        indent = len(raw_line) - len(raw_line.lstrip())
        if in_fence:
            # 代码块内容：原样保留，不做行内解析
            base = current_heading + 1 if current_heading else 0
            items.append({"level": base + indent // 2, "text": stripped_line, "kind": "fence"})
            i += 1
            continue
        if stripped_line.startswith("#"):
            heading = stripped_line.lstrip("#")
            level = len(stripped_line) - len(heading)
            current_heading = level
            items.append({"level": level, "text": heading.strip(), "kind": "text"})
            i += 1
            continue
        # 表格：当前行像表头，且下一行是分隔行
        if (
            "|" in stripped_line
            and i + 1 < len(lines)
            and _is_table_separator(lines[i + 1].strip())
        ):
            block = [stripped_line]
            # 从分隔行开始收集（分隔行也是表格的一部分，_parse_table_block 需要它）
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if "|" in next_line:
                    block.append(next_line)
                    j += 1
                else:
                    break
            base = current_heading + 1 if current_heading else 0
            parsed = _parse_table_block(block)
            for row_index, row in enumerate(parsed["rows"]):
                item: dict = {"level": base + indent // 2, "kind": "topic", "topic": row}
                if row_index == 0:
                    item["table_meta"] = parsed["header"]
                items.append(item)
            i = j
            continue
        if stripped_line.startswith(">"):
            # 引用块 → 前一个节点的备注
            note_text, _, _ = _strip_inline_markup(stripped_line.lstrip(">").strip())
            items.append({"kind": "note", "text": note_text})
            i += 1
            continue
        if stripped_line.startswith("$$"):
            # 块级公式：$$...$$（可跨多行），随后渲染成图片插入节点
            seg = stripped_line[2:].strip()
            if "$$" in seg:
                latex = seg.split("$$", 1)[0].strip()
                i += 1
            else:
                latex = seg
                i += 1
                while i < len(lines):
                    nxt = lines[i].strip()
                    if "$$" in nxt:
                        rest = nxt.split("$$", 1)[0].strip()
                        if rest:
                            latex += "\n" + rest
                        i += 1
                        break
                    latex += "\n" + nxt
                    i += 1
            base = current_heading + 1 if current_heading else 0
            items.append({"level": base + indent // 2, "kind": "equation", "latex": latex.strip()})
            continue
        # 列表/普通文本：只认“符号 + 空格”的列表标记，
        # 避免把行首 **加粗** 当成 * 列表符号吃掉
        text = re.sub(r"^(?:[-+>]\s+|[-*+]\s+)+", "", stripped_line).strip()
        base = current_heading + 1 if current_heading else 0
        items.append({"level": base + indent // 2, "text": text, "kind": "text"})
        i += 1

    if not items:
        raise ValueError("Markdown 内容为空，无法创建文件")

    # 整体归一化：最小层级变成 1
    min_level = min(item["level"] for item in items if item.get("level") is not None)

    # 先挂到虚拟根上：标准大纲算法，层级跳变时自动成为上层节点的子节点
    virtual: dict = {"title": "", "children": []}
    stack: list[tuple[int, dict]] = [(0, virtual)]
    last_topic: Optional[dict] = None
    for item in items:
        if item["kind"] == "note":
            if last_topic is not None:
                existing = last_topic.get("notes") or ""
                last_topic["notes"] = (existing + "\n" if existing else "") + item["text"]
                # 引用块转成的备注要直接显示在画布上（XMind 的 notesDisplay: block）
                last_topic["notesDisplay"] = "block"
            else:
                # 没有前置节点时兜底成普通节点
                note_node: dict = {"title": item["text"], "children": []}
                virtual["children"].append(note_node)
                last_topic = note_node
            continue

        level = max(item["level"] - min_level + 1, 1)
        while len(stack) > 1 and level <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if item["kind"] == "topic":
            child = item["topic"]
            table_header = item.get("table_meta")
            if table_header:
                _apply_table_meta(parent, table_header)
        elif item["kind"] == "equation":
            # 公式节点：标题暂存 LaTeX，_formula 标记用于导入后渲染成图片
            child = {"title": item["latex"], "_formula": item["latex"], "children": []}
        else:
            text, bold, italic = _strip_inline_markup(item["text"])
            child = {"title": text, "children": []}
            if bold or italic:
                props: dict[str, str] = {}
                if bold:
                    props["fo:font-weight"] = "bold"
                if italic:
                    props["fo:font-style"] = "italic"
                child["style"] = {"properties": props}
        parent["children"].append(child)
        stack.append((level, child))
        last_topic = child

    top_level = virtual["children"]
    if len(top_level) == 1:
        # 只有一个顶层条目（典型：# 根主题 + 各层子节点），它就是中心主题
        real_root = top_level[0]
        root_meta = {
            key: real_root[key]
            for key in ("structureClass", "extensions", "notesDisplay", "style")
            if real_root.get(key)
        }
        # 中心主题本身是一个公式节点（如 md 只有一行 $$...$$ 时）也要能渲染
        if real_root.get("_formula"):
            root_meta["_formula"] = real_root["_formula"]
        return real_root["title"], real_root["children"], root_meta
    # 多个顶层条目（平铺列表 / 多个 # 标题）→ 自动加“未命名”中心主题，
    # 所有顶层条目保持兄弟关系，不被错误嵌套
    return "未命名", top_level, {}


def _render_formula_to_temp(latex: str) -> Optional[str]:
    """把 LaTeX 公式渲染成临时 PNG，返回路径；失败返回 None。"""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    if render_latex_png(latex, path):
        return path
    try:
        os.remove(path)
    except OSError:
        pass
    return None


def _inject_equations(xm: XMindFile, writer: XMindWriter, topics: list[dict], root_meta: dict) -> int:
    """把 topics/root 结构里标记了 _formula 的节点渲染成图片并插入生成的 xmind。

    公式节点在 XMindWriter.create 时被 _build_topic 创建（_formula 标记被忽略），
    标题暂存为 LaTeX 文本。这里按路径定位到对应节点，渲染图片后插入，
    并清空标题（图片即内容）。渲染失败时保留 LaTeX 文本，不影响导入。

    返回成功插入的公式数量。
    """
    eqs: list[tuple[list[int], str]] = []
    # 中心主题本身是公式（如 md 只有一行 $$...$$）
    if root_meta.get("_formula"):
        eqs.append(([], root_meta["_formula"]))

    def walk(node_list: list[dict], path: list[int]) -> None:
        for idx, child in enumerate(node_list):
            latex = child.get("_formula")
            if latex:
                eqs.append((path + [idx], latex))
            walk(child.get("children") or [], path + [idx])

    walk(topics, [])
    if not eqs:
        return 0

    root = (xm.raw_content[0] or {}).get("rootTopic") or {}
    inserted = 0
    for path, latex in eqs:
        topic: Any = root
        ok_nav = True
        for idx in path:
            kids = (topic.get("children") or {}).get("attached") or []
            if idx < len(kids):
                topic = kids[idx]
            else:
                ok_nav = False
                break
        if not ok_nav or not topic.get("id"):
            continue
        png = _render_formula_to_temp(latex)
        if not png:
            continue
        try:
            ok, _ = writer.add_image(topic["id"], png)
        finally:
            try:
                os.remove(png)
            except OSError:
                pass
        if ok:
            topic["title"] = ""  # 图片即内容，清空暂存的 LaTeX 标题
            inserted += 1
    if inserted:
        xm.refresh_from_raw()
        # add_image 只更新内存和 _extra_files，必须 save 才把图片写进磁盘 zip
        try:
            writer.save()
        except Exception as exc:  # 保存失败不影响导入结果，但磁盘上无图片
            print(
                f"[xmind-mcp] 公式图片保存失败: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    return inserted


def _build_svg(xm: XMindFile) -> tuple[str, dict]:
    """把节点树画成一张示意图 SVG（给人工预览用，不是 XMind 的像素级渲染）。

    布局：根在左侧，子节点逐层向右展开；方块颜色尽量贴近节点的有效样式。
    """
    MARGIN = 40
    BOX_H = 34
    GAP = 12
    LEVEL_W = 240

    def _esc(text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _height(node: TopicNode) -> int:
        if not node.children:
            return 1
        return sum(_height(child) for child in node.children)

    def _box_width(title: str) -> int:
        return min(320, max(110, 28 + len(title) * 15))

    def _fill(node: TopicNode) -> str:
        color = _effective_style(node, xm).get("svg:fill") or "#FFFFFF"
        return color[:7]

    def _stroke(node: TopicNode) -> str:
        color = _effective_style(node, xm).get("border-line-color") or "#CCCCCC"
        return color[:7]

    def _text_color(node: TopicNode) -> str:
        color = _effective_style(node, xm).get("fo:color") or "#333333"
        return color[:7]

    def _font_size(node: TopicNode) -> int:
        value = _effective_style(node, xm).get("fo:font-size") or "14pt"
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        try:
            return min(max(int(digits), 8), 24)
        except ValueError:
            return 14

    elements: list[str] = []
    node_count = 0
    max_x = MARGIN
    y_cursor = MARGIN

    for sheet in xm.sheets:
        sheet_height = _height(sheet.root) * (BOX_H + GAP) - GAP
        elements.append(
            f'<text x="{MARGIN}" y="{y_cursor - 10}" font-size="16" font-weight="bold" '
            f'fill="#444444">{_esc(sheet.title)}</text>'
        )
        start_y = y_cursor

        def _place(node: TopicNode, x: int, y_top: int) -> None:
            nonlocal node_count, max_x
            node_count += 1
            block = _height(node) * (BOX_H + GAP) - GAP
            title = _clean_title(node.title) or ("📷图片" if node.image_src else "（无标题）")
            width = _box_width(title)
            y = y_top + (block - BOX_H) / 2
            shape_class = _effective_style(node, xm).get("shape-class") or ""
            rx = 24 if ("circle" in shape_class or "ellipse" in shape_class) else 6
            tooltip = ""
            if node.notes:
                tooltip = f"<title>{_esc(_truncate(node.notes, 300))}</title>"

            elements.append(
                f'<rect x="{x:.0f}" y="{y:.0f}" width="{width}" height="{BOX_H}" rx="{rx}" '
                f'fill="{_fill(node)}" stroke="{_stroke(node)}" stroke-width="1.5">{tooltip}</rect>'
            )
            max_chars = max(1, (width - 20) // 15)
            shown = title if len(title) <= max_chars else title[: max_chars - 1] + "…"
            font_px = _font_size(node)
            elements.append(
                f'<text x="{x + width / 2:.0f}" y="{y + BOX_H / 2 + font_px * 0.35:.0f}" '
                f'font-size="{font_px}" text-anchor="middle" fill="{_text_color(node)}">'
                f"{_esc(shown)}</text>"
            )
            max_x = max(max_x, x + width)

            child_x = x + width + 60
            child_y_top = y_top
            for child in node.children:
                child_block = _height(child) * (BOX_H + GAP) - GAP
                child_y = child_y_top + (child_block - BOX_H) / 2
                elements.append(
                    f'<line x1="{x + width:.0f}" y1="{y + BOX_H / 2:.0f}" '
                    f'x2="{child_x:.0f}" y2="{child_y + BOX_H / 2:.0f}" '
                    'stroke="#AAAAAA" stroke-width="1.5"/>'
                )
                _place(child, child_x, child_y_top)
                child_y_top += child_block + GAP

        _place(sheet.root, MARGIN, start_y)
        y_cursor = start_y + sheet_height + 60

    canvas_w = int(max_x + MARGIN)
    canvas_h = int(y_cursor)
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_w}" height="{canvas_h}" '
        f'viewBox="0 0 {canvas_w} {canvas_h}">\n'
        f'<rect width="100%" height="100%" fill="#FAFAFA"/>\n'
        + "\n".join(elements)
        + "\n</svg>\n"
    )
    return svg, {"sheets": len(xm.sheets), "nodes": node_count}
