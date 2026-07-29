"""MCP 工具定义和处理逻辑。

工具列表：
  读取类:
    read_structure    — 读取节点树结构
    list_images       — 列出所有含图片的节点
    read_image_ocr    — 对指定节点的图片做 OCR
    read_all          — 读取结构 + 所有图片 OCR（一步到位）

  编辑类:
    add_node          — 添加子节点
    update_node       — 更新节点标题/备注
    delete_node       — 删除节点
    move_node         — 移动节点到新父节点
    save_file         — 保存修改

  系统类:
    get_hardware_info — 获取硬件检测结果
"""

from __future__ import annotations

import os
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


# ---------- 格式化 ----------

def format_topic_tree(xm: XMindFile, show_images: bool = True) -> str:
    """把节点树格式化成带缩进的文本。"""
    lines = []
    for i, sheet in enumerate(xm.sheets):
        if i > 0:
            lines.append("")
        lines.append(f"📄 工作表: {sheet.title}")
        _format_node(sheet.root, prefix="", is_last=True, lines=lines, show_images=show_images)
    return "\n".join(lines)


def _format_node(
    node: TopicNode,
    prefix: str,
    is_last: bool,
    lines: list[str],
    show_images: bool,
) -> None:
    connector = "└─ " if is_last else "├─ "
    badges = []
    if show_images and node.image_src:
        badges.append("📷图片")
    if node.notes:
        badges.append("📝备注")
    if node.markers:
        badges.append(f"🔖{','.join(node.markers)}")

    badge_str = f" {' '.join(badges)}" if badges else ""
    lines.append(f"{prefix}{connector}[L{node.level}] {node.title}{badge_str}")

    child_prefix = prefix + ("   " if is_last else "│  ")
    for j, child in enumerate(node.children):
        _format_node(
            child, child_prefix,
            is_last=(j == len(node.children) - 1),
            lines=lines, show_images=show_images,
        )


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
    return [
        _tool(
            "read_structure",
            "读取 XMind 文件的节点树结构，返回带层级的文本。图片节点会标记📷。",
            {"file_path": {"type": "string", "description": ".xmind 文件路径"}},
            ["file_path"],
        ),
        _tool(
            "list_images",
            "列出 XMind 文件中所有包含图片的节点（ID + 标题 + 图片引用）。",
            {"file_path": {"type": "string", "description": ".xmind 文件路径"}},
            ["file_path"],
        ),
        _tool(
            "read_image_ocr",
            "对指定节点的图片做 OCR 识别，返回图片中的文字。需要先用 read_structure 或 list_images 获取 node_id。",
            {
                "file_path": {"type": "string", "description": ".xmind 文件路径"},
                "node_id": {"type": "string", "description": "包含图片的节点 ID"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "read_all",
            "一步到位：读取节点树 + 对所有图片节点做 OCR。返回结构化文本，每个图片节点的图片内容以 [OCR] 标注。",
            {"file_path": {"type": "string", "description": ".xmind 文件路径"}},
            ["file_path"],
        ),
        _tool(
            "add_node",
            "在指定父节点下添加子节点。修改不会自动保存，需调用 save_file。",
            {
                "file_path": {"type": "string", "description": ".xmind 文件路径"},
                "parent_id": {"type": "string", "description": "父节点 ID"},
                "title": {"type": "string", "description": "新节点标题"},
                "notes": {"type": "string", "description": "节点备注（可选）"},
            },
            ["file_path", "parent_id", "title"],
        ),
        _tool(
            "update_node",
            "更新节点属性。传入 None 或省略的字段不会被修改。",
            {
                "file_path": {"type": "string", "description": ".xmind 文件路径"},
                "node_id": {"type": "string", "description": "要更新的节点 ID"},
                "title": {"type": "string", "description": "新标题（可选）"},
                "notes": {"type": "string", "description": "新备注（可选）"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "delete_node",
            "删除节点及其所有子节点。不能删除根主题。修改不会自动保存，需调用 save_file。",
            {
                "file_path": {"type": "string", "description": ".xmind 文件路径"},
                "node_id": {"type": "string", "description": "要删除的节点 ID"},
            },
            ["file_path", "node_id"],
        ),
        _tool(
            "move_node",
            "把节点移动到新的父节点下。修改不会自动保存，需调用 save_file。",
            {
                "file_path": {"type": "string", "description": ".xmind 文件路径"},
                "node_id": {"type": "string", "description": "要移动的节点 ID"},
                "new_parent_id": {"type": "string", "description": "新父节点 ID"},
            },
            ["file_path", "node_id", "new_parent_id"],
        ),
        _tool(
            "save_file",
            "保存对 XMind 文件的修改。可另存为新文件或覆盖原文件。",
            {
                "file_path": {"type": "string", "description": "当前 .xmind 文件路径"},
                "output_path": {"type": "string", "description": "另存为的路径（可选，省略则覆盖原文件）"},
            },
            ["file_path"],
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

    async def call_tool(self, name: str, arguments: dict) -> list[types.TextContent]:
        handler = self._handlers.get(name)
        if handler is None:
            return [types.TextContent(type="text", text=f"未知工具: {name}")]

        try:
            result = await handler(self, arguments)
            return [types.TextContent(type="text", text=result)]
        except Exception as e:
            return [types.TextContent(type="text", text=f"❌ 错误: {e}")]

    # ---- 各工具实现 ----

    async def _read_structure(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        return format_topic_tree(xm)

    async def _list_images(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        nodes = xm.find_topics_with_image()
        if not nodes:
            return "文件中没有包含图片的节点。"
        lines = [f"找到 {len(nodes)} 个图片节点："]
        for n in nodes:
            lines.append(f"  • ID: {n.id}  | 标题: {n.title}  | 图片: {n.image_src[:60]}")
        return "\n".join(lines)

    async def _read_image_ocr(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        node = xm.find_topic(args["node_id"])
        if not node:
            return f"找不到节点: {args['node_id']}"
        if not node.image_src:
            return f"节点 '{node.title}' 没有图片。"

        image_data = load_topic_image(node, xm)
        if not image_data:
            return f"图片加载失败: {node.image_src}"

        result = self.ocr.recognize(image_data)
        header = f"📷 节点「{node.title}」的图片 OCR 结果（{result.elapsed:.2f}s）："
        body = result.text if result.text else "（未识别到文字）"
        return f"{header}\n{body}"

    async def _read_all(self, args: dict) -> str:
        xm, _ = self.workspace.get(args["file_path"])
        image_nodes = xm.find_topics_with_image()

        # 先做所有图片的 OCR
        ocr_results: dict[str, str] = {}
        if image_nodes:
            for node in image_nodes:
                image_data = load_topic_image(node, xm)
                if image_data:
                    result = self.ocr.recognize(image_data)
                    ocr_results[node.id] = result.text or "（未识别到文字）"

        # 格式化输出，把 OCR 结果内联到节点下
        lines = []
        for i, sheet in enumerate(xm.sheets):
            if i > 0:
                lines.append("")
            lines.append(f"📄 工作表: {sheet.title}")
            self._format_node_with_ocr(sheet.root, "", True, lines, ocr_results)
        return "\n".join(lines)

    @staticmethod
    def _format_node_with_ocr(
        node: TopicNode, prefix: str, is_last: bool, lines: list[str], ocr_map: dict[str, str]
    ) -> None:
        connector = "└─ " if is_last else "├─ "
        badges = []
        if node.image_src:
            badges.append("📷图片")
        if node.notes:
            badges.append("📝备注")
        badge_str = f" {' '.join(badges)}" if badges else ""
        lines.append(f"{prefix}{connector}[L{node.level}] {node.title}{badge_str}")

        # 内联 OCR 结果
        if node.id in ocr_map:
            ocr_text = ocr_map[node.id]
            ocr_prefix = prefix + ("   " if is_last else "│  ")
            lines.append(f"{ocr_prefix}   ↳ [OCR] {ocr_text}")

        child_prefix = prefix + ("   " if is_last else "│  ")
        for j, child in enumerate(node.children):
            XMindTools._format_node_with_ocr(
                child, child_prefix, j == len(node.children) - 1, lines, ocr_map
            )

    async def _add_node(self, args: dict) -> str:
        xm, writer = self.workspace.get(args["file_path"])
        new_node = writer.add_node(
            parent_id=args["parent_id"],
            title=args["title"],
            notes=args.get("notes", ""),
        )
        return f"✅ 已添加节点：ID={new_node.id}, 标题='{new_node.title}'\n⚠️ 修改未保存，请调用 save_file。"

    async def _update_node(self, args: dict) -> str:
        xm, writer = self.workspace.get(args["file_path"])
        success = writer.update_node(
            node_id=args["node_id"],
            title=args.get("title"),
            notes=args.get("notes"),
        )
        if success:
            return f"✅ 节点 {args['node_id']} 已更新。\n⚠️ 修改未保存，请调用 save_file。"
        return f"❌ 找不到节点: {args['node_id']}"

    async def _delete_node(self, args: dict) -> str:
        xm, writer = self.workspace.get(args["file_path"])
        try:
            success = writer.delete_node(args["node_id"])
            if success:
                return f"✅ 节点 {args['node_id']} 及其子节点已删除。\n⚠️ 修改未保存，请调用 save_file。"
            return f"❌ 找不到节点: {args['node_id']}"
        except ValueError as e:
            return f"❌ {e}"

    async def _move_node(self, args: dict) -> str:
        xm, writer = self.workspace.get(args["file_path"])
        success = writer.move_node(args["node_id"], args["new_parent_id"])
        if success:
            return f"✅ 节点 {args['node_id']} 已移动到 {args['new_parent_id']} 下。\n⚠️ 修改未保存，请调用 save_file。"
        return "❌ 移动失败：找不到节点或不能移动根主题。"

    async def _save_file(self, args: dict) -> str:
        xm, writer = self.workspace.get(args["file_path"])
        output = args.get("output_path")
        saved_path = writer.save(output)
        self.workspace.refresh_after_save(saved_path)
        return f"✅ 已保存到: {saved_path}"

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
        "list_images": _list_images,
        "read_image_ocr": _read_image_ocr,
        "read_all": _read_all,
        "add_node": _add_node,
        "update_node": _update_node,
        "delete_node": _delete_node,
        "move_node": _move_node,
        "save_file": _save_file,
        "get_hardware_info": _get_hardware_info,
    }
