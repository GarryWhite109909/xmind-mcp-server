"""MCP 工具处理器：参数校验、文件缓存、结果格式化。

工具 schema 与分组见 tool_schemas；Markdown 导入见 markdown_import；
读写细节在 xmind_parser / xmind_writer。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import zipfile
from pathlib import Path
from typing import Optional

from mcp import types

from .config import Config, normalize_image_mode
from .image_payload import DEFAULT_MAX_SIDE, PreparedImage, prepare_image
from .markdown_import import _inject_equations, _parse_markdown_outline
from .render import render_sheet_png
from .tool_schemas import _READ_ONLY_TOOLS
from .xmind_parser import (
    parse_xmind,
    load_preview_image,
    load_topic_image,
    collect_xap_refs,
    resolve_xap_entry,
    XMindFile,
    TopicNode,
)
from .xmind_props import THEME_LEVELS
from .xmind_writer import (
    FileChangedError,
    XMindWriter,
    file_digest,
    list_backups,
    restore_backup,
)


# ---------- 文件缓存 ----------

def _has_unsaved_changes(xm: XMindFile) -> bool:
    """内存里的 content 是否与最近一次读写的 zip 不同。"""
    if not xm._zip_bytes:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(xm._zip_bytes)) as zf:
            baseline = json.loads(zf.read("content.json"))
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile):
        return False
    normalize = lambda data: json.dumps(data, sort_keys=True, ensure_ascii=False)  # noqa: E731
    return normalize(baseline) != normalize(xm.raw_content)


def _cache_key(path: str) -> str:
    """统一缓存键：Windows 下大小写不敏感，避免同一文件缓存两份。"""
    return os.path.normcase(os.path.abspath(path))


class XMindWorkspace:
    """缓存已解析的 XMind 文件，避免每次工具调用都重新解析。"""

    def __init__(self):
        # {path: (mtime, XMindFile, XMindWriter)}
        self._cache: dict[str, tuple[float, XMindFile, XMindWriter]] = {}
        # MCP 可能并发调用，缓存读写加锁避免同一文件被两个请求同时改写
        self._lock = threading.Lock()

    def get(self, path: str) -> tuple[XMindFile, XMindWriter]:
        path = _cache_key(path)
        mtime = os.path.getmtime(path)

        with self._lock:
            cached = self._cache.get(path)
            if cached and cached[0] == mtime:
                return cached[1], cached[2]

            if cached:
                disk_digest = file_digest(path)
                if disk_digest and disk_digest == cached[1].source_digest:
                    # 内容没变，只是 mtime 变了（touch、同步盘等），继续用缓存
                    self._cache[path] = (mtime, cached[1], cached[2])
                    return cached[1], cached[2]
                if disk_digest and _has_unsaved_changes(cached[1]):
                    # 磁盘变了 + 内存里有未保存的修改：重新解析会静默丢掉用户的编辑
                    raise FileChangedError(
                        f"文件已被其他程序修改，而当前会话还有未保存的修改: {path}\n"
                        "请先用 save_file(output_path='新路径.xmind') 另存你的修改，再重新读取文件。"
                    )

            xm = parse_xmind(path)
            writer = XMindWriter(xm)
            self._cache[path] = (mtime, xm, writer)
            return xm, writer

    def get_cached(self, path: str) -> Optional[tuple[XMindFile, XMindWriter]]:
        """取缓存里的实例，不检查磁盘。

        只给保存用：文件被外部改过时，用户仍需要能把内存里的修改另存出去。
        """
        with self._lock:
            cached = self._cache.get(_cache_key(path))
            return (cached[1], cached[2]) if cached else None

    def put(self, path: str, xm: XMindFile, writer: XMindWriter) -> None:
        """把新建/导入得到的实例放入缓存。"""
        with self._lock:
            self._cache[_cache_key(path)] = (os.path.getmtime(path), xm, writer)

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._cache.pop(_cache_key(path), None)

    def refresh_after_save(self, path: str) -> tuple[XMindFile, XMindWriter]:
        """保存后刷新缓存。"""
        self.invalidate(path)
        return self.get(path)


# ---------- 格式化辅助 ----------

# 单次输出里备注文本的最大长度，防止超长内容撑爆模型上下文
MAX_NOTE_CHARS = 2000

# 一次读取最多内联多少张图，超出部分只给坐标提示
MAX_INLINE_IMAGES = 8

# auto 表示“跟随配置”，其余是显式模式
_IMAGE_MODES = ("off", "inline", "auto")


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
    """把 auto 解析成配置里的模式；非法值和旧版 ocr/both 都按 inline 处理。"""
    mode = str(value or "auto").strip().lower()
    if mode not in _IMAGE_MODES:
        mode = "auto"
    if mode == "auto":
        mode = normalize_image_mode(getattr(config, "image_mode", ""))
    return mode


def _normalize_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(path)))


def check_path_allowed(path: str, allowed_roots: list[str]) -> None:
    """allowed_roots 非空时，路径必须落在其中一个目录内。

    留空表示不限制，保持旧行为——否则升级后所有既有路径都会失效。
    """
    if not allowed_roots:
        return
    target = _normalize_path(path)
    for root in allowed_roots:
        root_path = _normalize_path(root)
        prefix = root_path if root_path.endswith(os.sep) else root_path + os.sep
        if target == root_path or target.startswith(prefix):
            return
    raise PermissionError(
        f"路径不在允许范围内: {path}\n"
        f"当前 allowed_roots = {allowed_roots}。"
        f"如确需访问，请在 ~/.xmind-mcp/config.json 的 allowed_roots 里加上对应目录。"
    )


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
    """清理节点标题中的换行和多余空白。兼容 JSON 里非字符串的畸形标题。"""
    title = str(title or "")
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
    include_metadata: bool = False,
    include_style: bool = False,
) -> str:
    """把节点树格式化成带缩进的文本。

    include_metadata=True 时额外输出备注/标签/超链接/主题类型，
    供 read_all 使用；read_structure 保持原来的精简输出。
    """
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
            include_metadata=include_metadata,
            include_style=include_style,
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
    include_metadata: bool = False,
    include_style: bool = False,
) -> None:
    if max_nodes is not None and state["nodes"] >= max_nodes:
        state["truncated"] = True
        return
    state["nodes"] += 1
    connector = "└─ " if is_last else "├─ "
    line = _format_node_line(node, include_ids, show_images, clean)
    lines.append(f"{prefix}{connector}{line}")

    child_prefix = prefix + ("   " if is_last else "│  ")
    if include_metadata and not clean:
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
            include_metadata=include_metadata,
            include_style=include_style,
        )


def _iter_visible_topics(
    xm: XMindFile,
    max_depth: Optional[int] = None,
    max_nodes: Optional[int] = None,
):
    """按输出顺序遍历“会被渲染出来”的节点（与树格式化共用相同的截断规则）。

    供 read_all 决定要内联哪些图片：max_depth/max_nodes 之外的图片
    不会出现在输出里，也就不必读取和压缩。
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
    seen: set[str] = set()
    current = node
    while current and current.id not in seen:
        seen.add(current.id)
        parts.append(_clean_title(current.title) or current.id)
        # 通过 parent_id 在树中查找父节点
        if not current.parent_id:
            break
        current = xm.find_topic(current.parent_id)
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


# ---------- 工具处理 ----------

class XMindTools:
    """工具处理入口，管理文件缓存。"""

    def __init__(self, config: Config):
        self.config = config
        self.workspace = XMindWorkspace()
        # 写工具串行执行：MCP 客户端可能并发调用，两个写操作同时改
        # 同一份缓存会把彼此的修改写坏。读工具不加锁，保持响应速度。
        self._write_lock = asyncio.Lock()

    def _guard_paths(self, arguments: dict) -> None:
        """所有以 _path 结尾的参数都受 allowed_roots 约束。"""
        roots = getattr(self.config, "allowed_roots", None) or []
        if isinstance(roots, str):  # 配置文件里写成单个字符串也能用
            roots = [roots]
        if not roots:
            return
        for key, value in arguments.items():
            if key.endswith("_path") and isinstance(value, str) and value:
                check_path_allowed(value, roots)

    async def call_tool(self, name: str, arguments: dict) -> list:
        messages, _ = await self.call_tool_status(name, arguments)
        return messages

    async def call_tool_status(self, name: str, arguments: dict) -> tuple[list, bool]:
        """调用工具并返回 (内容块, 是否错误)。错误信息同时让 MCP 标记 isError。

        内容块可以是 TextContent，也可以是 ImageContent —— 多模态客户端
        才能看到节点原图。
        """
        handler = self._handlers.get(name)
        if handler is None:
            return [_text_block(f"未知工具: {name}")], True

        try:
            self._guard_paths(arguments)
        except PermissionError as e:
            return [_text_block(f"❌ {e}")], True

        try:
            if name in _READ_ONLY_TOOLS:
                result = await handler(self, arguments)
            else:
                async with self._write_lock:
                    result = await handler(self, arguments)
            return _content_blocks(result), False
        except FileChangedError as e:
            return [_text_block(f"❌ {e}")], True
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
        return [_text_block(caption), _image_block(prepared)]

    async def _render_preview(
        self, xm: XMindFile, sheet_index: int, max_side: int
    ) -> tuple[Optional[bytes], str]:
        """在后台线程里用 matplotlib 画一张近似图（matplotlib 非线程安全）。"""
        style_of = lambda node: _effective_style(node, xm)  # noqa: E731

        def _run():
            return render_sheet_png(
                xm, sheet_index, max_width=max_side, style_of=style_of
            )

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # 渲染是尽力而为，不能把整个工具弄挂
            return None, f"{type(exc).__name__}: {exc}"

    async def _get_preview(self, args: dict) -> list:
        """返回整图预览：优先 XMind 自己保存的缩略图，没有就自己画一张。"""
        xm, _ = self.workspace.get(args["file_path"])
        source = str(args.get("source") or "auto").lower()
        if source not in ("auto", "embedded", "render"):
            source = "auto"
        try:
            sheet_index = int(args.get("sheet", 0))
        except (TypeError, ValueError):
            sheet_index = 0
        max_side = args.get("max_side") or self.config.image_max_side

        embedded = load_preview_image(xm) if source in ("auto", "embedded") else None
        sheet_titles = "、".join(s.title for s in xm.sheets) or "（无工作表）"

        if embedded:
            prepared = _prepare_image_arg(embedded, max_side)
            if prepared is None:
                return [_text_block(
                    "缩略图存在但无法解码，文件可能已损坏，可用 repair_file 修复；"
                    "也可以传 source=\"render\" 让工具自己画一张。"
                )]
            caption = (
                f"🖼️ 文件缩略图（XMind 保存时渲染）：{prepared.source_width}×"
                f"{prepared.source_height} → {prepared.width}×{prepared.height}，"
                f"{prepared.size_kb}KB\n工作表：{sheet_titles}\n"
                "注意：这是整份文件最近一次保存时的画面，多工作表只展示其中一张。"
            )
            return [_text_block(caption), _image_block(prepared)]

        if source == "embedded":
            return [_text_block(
                "该文件没有内嵌缩略图。可能原因：文件由本工具保存过（保存时移除了过期缩略图），"
                "或 XMind 从未保存过该文件。\n"
                "可选做法：用 source=\"render\" 让工具自己画一张；在 XMind 里打开并保存一次；"
                "或用 export_svg 导出示意图；或直接用 read_all 读取结构。"
            )]

        if not (0 <= sheet_index < len(xm.sheets)):
            return [_text_block(
                f"工作表索引 {sheet_index} 超出范围（共 {len(xm.sheets)} 张）。"
            )]

        rendered, note = await self._render_preview(xm, sheet_index, max_side)
        if rendered is None:
            return [_text_block(
                f"该文件没有内嵌缩略图，自绘预览也失败了：{note}\n"
                "可选做法：定位到具体工作表用 export_svg；或直接用 read_all 读取结构。"
            )]
        prepared = _prepare_image_arg(rendered, max_side)
        if prepared is None:
            return [_text_block(f"自绘预览已生成但无法编码为图片：{note}")]

        sheet_title = xm.sheets[sheet_index].title or f"#{sheet_index}"
        caption = (
            f"🖼️ 自绘预览（近似效果）：{prepared.width}×{prepared.height}，"
            f"{prepared.size_kb}KB\n工作表：{sheet_title}（全部：{sheet_titles}）\n"
            f"说明：{note}。版式和配色接近 XMind，但不是逐像素还原。"
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
        # 避免 max_depth/max_nodes 截断后仍读取和压缩图片
        image_nodes: list[TopicNode] = []
        if image_mode == "inline":
            visible_ids = {n.id for n in _iter_visible_topics(xm, max_depth, max_nodes)}
            image_nodes = [n for n in xm.find_topics_with_image() if n.id in visible_ids]

        text = format_topic_tree(
            xm,
            include_ids=include_ids,
            clean=clean,
            max_depth=max_depth,
            max_nodes=max_nodes,
            include_metadata=True,
            include_style=include_style,
        )
        if image_mode != "inline" or not image_nodes:
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
        return self._auto_save_msg(
            file_path, writer, args,
            f"✅ 已添加节点：ID={new_node.id}, 标题='{new_node.title}'",
        )

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

        return self._auto_save_msg(
            file_path, writer, args, f"✅ 节点 {args['node_id']} 已更新。"
        )

    async def _delete_node(self, args: dict) -> str:
        file_path = args["file_path"]
        xm, writer = self.workspace.get(file_path)
        try:
            success = writer.delete_node(args["node_id"])
        except (ValueError, TypeError) as e:
            return f"❌ {e}"

        if not success:
            return f"❌ 找不到节点: {args['node_id']}"

        return self._auto_save_msg(
            file_path, writer, args,
            f"✅ 节点 {args['node_id']} 及其子节点已删除。",
        )

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

        return self._auto_save_msg(
            file_path, writer, args,
            f"✅ 节点 {args['node_id']} 已移动到 {args['new_parent_id']} 下。",
        )

    async def _save_file(self, args: dict) -> str:
        file_path = args["file_path"]
        # 保存必须拿到带未保存修改的那个实例，所以绕过 get() 的冲突检查：
        # 冲突时覆盖会失败，但另存到新路径要能用。
        cached = self.workspace.get_cached(file_path)
        xm, writer = cached if cached else self.workspace.get(file_path)
        output = args.get("output_path")
        try:
            saved_path = writer.save(output)
        except FileChangedError as e:
            return (
                f"❌ {e}\n"
                "你的修改仍在内存中：可以用 save_file(output_path='另一个路径.xmind') 另存，"
                "或重新读取文件后再改。"
            )
        if _cache_key(saved_path) != _cache_key(file_path):
            # 另存通常还要继续改原文件，缓存要留着；只有原文件在编辑期间
            # 被外部改过时，才丢掉这个已经冲突的缓存。
            current = file_digest(file_path)
            if current and writer.xm.source_digest and current != writer.xm.source_digest:
                self.workspace.invalidate(file_path)
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
        self.workspace.put(file_path, xm, writer)
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
        self.workspace.put(file_path, xm, writer)
        node_count = len(xm.all_topics())
        eq_note = f"\n公式: {eq_count} 个（已渲染为图片）" if eq_count else ""
        return f"✅ 已从 Markdown 创建文件: {file_path}\n根主题: {root_title}\n节点总数: {node_count}{eq_note}"

    async def _import_markdown_file(self, args: dict) -> str:
        md_path = args["file_path"]
        output_path = args["output_path"]

        raw = Path(md_path).read_bytes()
        markdown = ""
        # 先试 utf-8-sig：Windows 记事本保存的 Markdown 常带 BOM，
        # 不去掉会让第一个 "# 标题" 识别失败。
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
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
        self.workspace.put(output_path, xm, writer)
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
            "📊 文件统计",
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
        if not args.get("auto_save"):
            return f"{msg}\n⚠️ 修改未保存，请调用 save_file 或设置 auto_save=true。"
        try:
            saved_path = writer.save()
        except FileChangedError as e:
            return (
                f"{msg}\n❌ 自动保存失败: {e}\n"
                "修改仍在内存中，可用 save_file(output_path='另一个路径.xmind') 另存。"
            )
        self.workspace.refresh_after_save(saved_path)
        return f"{msg}\n✅ 已自动保存到: {saved_path}{self._preview_note(writer)}"

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
        backup_path = args["backup_path"]
        # 只允许恢复 list_backups 为该文件列出的备份，避免把任意文件
        # 复制到目标路径（restore_backup 本质是文件覆盖操作）。
        known = {_cache_key(item["path"]) for item in list_backups(file_path)}
        if _cache_key(backup_path) not in known:
            return (
                "❌ backup_path 不是该文件的自动备份。"
                "请先调用 list_backups 获取可用的备份路径。"
            )
        try:
            saved = restore_backup(file_path, backup_path)
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
            try:
                max_nodes = int(args.get("max_nodes", 50) or 50)
            except (TypeError, ValueError):
                max_nodes = 50
            max_nodes = min(max(max_nodes, 1), 200)
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
        referenced = collect_xap_refs(xm.raw_content)
        if not xm._zip_bytes:
            return "❌ 文件内容为空，无法读取附件。"
        try:
            with zipfile.ZipFile(io.BytesIO(xm._zip_bytes)) as zf:
                entries = {name for name in zf.namelist() if not name.endswith("/")}
        except zipfile.BadZipFile:
            return "❌ 文件不是有效的 xmind 包。"
        attachments = sorted(
            name for name in entries if name.startswith(("attachments/", "resources/"))
        )
        referenced_entries = {
            entry for src in referenced if (entry := resolve_xap_entry(src, entries))
        }
        used = [name for name in attachments if name in referenced_entries]
        unused = [name for name in attachments if name not in referenced_entries]
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
        counts: dict[int, int] = {}
        for node in xm.all_topics():
            counts[node.sheet_index] = counts.get(node.sheet_index, 0) + 1
        lines = [f"共 {len(xm.sheets)} 张工作表："]
        for i, sheet in enumerate(xm.sheets):
            root_title = _clean_title(sheet.root.title) or "（无标题）"
            lines.append(
                f"  {i}. {sheet.title}（根主题: {root_title}，节点: {counts.get(i, 0)}）"
            )
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

    # ---- handler 注册表 ----
    _handlers = {
        "read_structure": _read_structure,
        "read_all": _read_all,
        "list_images": _list_images,
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


def _build_svg(xm: XMindFile) -> tuple[str, dict]:
    """把节点树画成一张示意图 SVG（给人工预览用，不是 XMind 的像素级渲染）。

    布局：根在左侧，子节点逐层向右展开；方块颜色尽量贴近节点的有效样式。
    """
    MARGIN = 40
    BOX_H = 34
    GAP = 12

    def _esc(text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    height_cache: dict[int, int] = {}

    def _height(node: TopicNode) -> int:
        """叶子数（即竖直占位）；缓存避免大图重复递归。"""
        key = id(node)
        if key in height_cache:
            return height_cache[key]
        if not node.children:
            height_cache[key] = 1
        else:
            height_cache[key] = sum(_height(child) for child in node.children)
        return height_cache[key]

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
