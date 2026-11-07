"""把 XMind 工作表渲染成 PNG（近似效果，不依赖 XMind 本体）。

XMind 保存时会在文件里写 Thumbnails/thumbnail.png，但本工具新建或修改过的
文件没有这张图。这里用 matplotlib（已经是本项目的依赖）按节点树画一版近似图，
给模型或人快速看整体版式和配色。

刻意不追求像素级还原：边界、概要、关系线、图片内容都不画，只画主题框、文字
和分支连线。需要真实效果时仍然以 XMind 打开后看到的画面为准。
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .xmind_parser import TopicNode, XMindFile

# matplotlib 不是线程安全的，MCP 可能并发调用，这里串行化
_RENDER_LOCK = threading.Lock()

# 按优先级尝试的中文字体；都没有时退回默认字体（中文会显示成方框）
_CJK_FONTS = (
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "PingFang SC",
    "Heiti SC",
    "WenQuanYi Micro Hei",
    "Arial Unicode MS",
)

MAX_NODES = 500
NODE_V_GAP = 12.0      # 同级分支的垂直间距（单位：pt）
LEVEL_H_GAP = 46.0     # 层级之间的水平间距（单位：pt）
DEFAULT_FONT_SIZE = 14.0
DEFAULT_FILL = "#FFFFFF"
DEFAULT_BORDER = "#CCCCCC"
DEFAULT_TEXT = "#333333"
DEFAULT_LINE = "#9AA4B2"
MAX_TITLE_CHARS = 26
MARGIN = 40.0

StyleOf = Optional[Callable[[TopicNode], dict]]


@dataclass
class _Box:
    """布局中间结果：测量阶段的尺寸，定位阶段的坐标。"""

    node: TopicNode
    depth: int
    width: float
    height: float
    side: int = 1                 # 1=右侧, -1=左侧
    children: list["_Box"] = field(default_factory=list)
    extent: float = 0.0           # 子树占用的总高度
    y: float = 0.0
    x: float = 0.0


def _pick_font() -> Optional[str]:
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in _CJK_FONTS:
        if name in available:
            return name
    return None


def _text_width(text: str, font_size: float) -> float:
    """粗略估算文字宽度：中日韩字符按 1em，其余按 0.55em。"""
    units = sum(1.0 if ord(ch) > 0x2E7F else 0.55 for ch in text)
    return units * font_size + 24.0


def _font_size(style: dict) -> float:
    raw = str(style.get("fo:font-size") or "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    try:
        return min(max(float(digits), 9.0), 28.0)
    except ValueError:
        return DEFAULT_FONT_SIZE


def _is_bold(style: dict) -> bool:
    raw = str(style.get("fo:font-weight") or "").lower()
    if raw in ("bold", "bolder"):
        return True
    return raw.isdigit() and int(raw) >= 600


def _short(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        return "（无标题）"
    if len(text) <= MAX_TITLE_CHARS:
        return text
    return text[: MAX_TITLE_CHARS - 1] + "…"


def _build(node: TopicNode, depth: int, style_of: StyleOf, budget: list[int]) -> _Box:
    """把主题树转成布局树，节点超过 MAX_NODES 时截断。"""
    budget[0] -= 1
    style = style_of(node) if style_of else {}
    font_size = _font_size(style)
    box = _Box(
        node=node,
        depth=depth,
        width=_text_width(_short(node.title), font_size),
        height=max(24.0, font_size * 1.9),
    )
    for child in node.children:
        if budget[0] <= 0:
            break
        box.children.append(_build(child, depth + 1, style_of, budget))
    return box


def _measure(box: _Box) -> float:
    """自底向上算子树需要的高度。"""
    if not box.children:
        box.extent = box.height
        return box.extent
    total = sum(_measure(child) for child in box.children)
    total += NODE_V_GAP * (len(box.children) - 1)
    box.extent = max(box.height, total)
    return box.extent


def _place(box: _Box, top: float) -> None:
    """自顶向下定位：父节点在自己占的竖直区间里垂直居中。"""
    if not box.children:
        box.y = top + box.extent / 2
        return

    total = sum(child.extent for child in box.children)
    total += NODE_V_GAP * (len(box.children) - 1)
    cursor = top + (box.extent - total) / 2
    for child in box.children:
        _place(child, cursor)
        cursor += child.extent + NODE_V_GAP

    # 父节点对齐第一个子节点的中心到最后一个子节点的中心之间
    box.y = (box.children[0].y + box.children[-1].y) / 2


def _iter(box: _Box):
    yield box
    for child in box.children:
        yield from _iter(child)


def _level_offsets(boxes: list[_Box]) -> dict[int, float]:
    """每层用该层最宽的框决定列宽，避免文字互相压住。"""
    widest: dict[int, float] = {}
    for box in boxes:
        widest[box.depth] = max(widest.get(box.depth, 0.0), box.width)
    offsets: dict[int, float] = {0: 0.0}
    for depth in sorted(d for d in widest if d > 0):
        offsets[depth] = (
            offsets[depth - 1]
            + widest.get(depth - 1, 0.0) / 2
            + LEVEL_H_GAP
            + widest[depth] / 2
        )
    return offsets


def _color(style: dict, key: str, fallback: str) -> str:
    value = str(style.get(key) or "")
    return value[:7] if value.startswith("#") else fallback


def _is_dark(color: str) -> bool:
    """按相对亮度判断底色深浅，用于决定文字用黑还是白。"""
    try:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return False
    return 0.299 * r + 0.587 * g + 0.114 * b < 140


def _text_color(style: dict) -> str:
    explicit = str(style.get("fo:color") or "")
    if explicit.startswith("#"):
        return explicit[:7]
    # 主题里常见 "inherited"，此时按底色自动选黑/白
    fill = _color(style, "svg:fill", DEFAULT_FILL)
    return "#FFFFFF" if _is_dark(fill) else DEFAULT_TEXT


def _draw(
    boxes: list[_Box],
    style_of: StyleOf,
    font: Optional[str],
    max_width: int,
) -> bytes:
    # 延迟导入：没有调用 get_preview 时不必支付 matplotlib 的启动开销
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.path import Path as MplPath
    from matplotlib.patches import FancyBboxPatch, PathPatch

    min_x = min(box.x - box.width / 2 for box in boxes) - MARGIN
    max_x = max(box.x + box.width / 2 for box in boxes) + MARGIN
    min_y = min(box.y - box.height / 2 for box in boxes) - MARGIN
    max_y = max(box.y + box.height / 2 for box in boxes) + MARGIN

    width_pt = max(1.0, max_x - min_x)
    height_pt = max(1.0, max_y - min_y)
    dpi = max(60.0, min(220.0, max_width / (width_pt / 72.0)))
    dpi = min(dpi, 6000.0 / (height_pt / 72.0))

    fig = Figure(figsize=(width_pt / 72.0, height_pt / 72.0), dpi=dpi)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(min_x, max_x)
    # 反转 y 轴，让第一个分支排在图上侧，符合阅读顺序
    ax.set_ylim(max_y, min_y)
    ax.axis("off")
    fig.patch.set_facecolor("#FFFFFF")

    # 先画连线，后画框，避免线压在文字上
    for box in boxes:
        style = style_of(box.node) if style_of else {}
        color = _color(style, "line-color",
                       _color(style, "border-line-color", DEFAULT_LINE))
        for child in box.children:
            direction = 1.0 if child.x >= box.x else -1.0
            start_x = box.x + direction * box.width / 2
            end_x = child.x - direction * child.width / 2
            mid_x = (start_x + end_x) / 2
            path = MplPath(
                [(start_x, box.y), (mid_x, box.y), (mid_x, child.y), (end_x, child.y)],
                [MplPath.MOVETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4],
            )
            ax.add_patch(PathPatch(path, facecolor="none", edgecolor=color,
                                   linewidth=1.2))

    for box in boxes:
        style = style_of(box.node) if style_of else {}
        ax.add_patch(FancyBboxPatch(
            (box.x - box.width / 2, box.y - box.height / 2), box.width, box.height,
            boxstyle="round,pad=0,rounding_size=6",
            linewidth=1.2,
            edgecolor=_color(style, "border-line-color", DEFAULT_BORDER),
            facecolor=_color(style, "svg:fill", DEFAULT_FILL),
        ))
        ax.text(
            box.x, box.y, _short(box.node.title),
            ha="center", va="center",
            fontsize=_font_size(style),
            color=_text_color(style),
            fontfamily=font or "sans-serif",
            fontweight="bold" if _is_bold(style) else "normal",
        )

    buffer = io.BytesIO()
    canvas.print_png(buffer)
    return buffer.getvalue()


def _split_sides(root: _Box) -> tuple[list[_Box], list[_Box]]:
    """左右两侧按叶子数尽量均分，保证两侧高度接近。"""
    def leaves(box: _Box) -> int:
        return 1 if not box.children else sum(leaves(c) for c in box.children)

    total = sum(leaves(child) for child in root.children)
    right: list[_Box] = []
    left: list[_Box] = []
    acc = 0
    for child in root.children:
        if acc * 2 < total:
            right.append(child)
            acc += leaves(child)
        else:
            left.append(child)
    return right, left


def render_sheet_png(
    xm: XMindFile,
    sheet_index: int = 0,
    *,
    max_width: int = 1600,
    style_of: StyleOf = None,
) -> tuple[Optional[bytes], str]:
    """渲染一张工作表，返回 (PNG 字节, 说明)。失败时 PNG 为 None。"""
    if not (0 <= sheet_index < len(xm.sheets)):
        return None, f"工作表索引无效: {sheet_index}"

    root_node = xm.sheets[sheet_index].root
    two_sided = "map" in (root_node.structure_class or "").lower()
    budget = [MAX_NODES]
    root = _build(root_node, 0, style_of, budget)

    notes: list[str] = []
    if budget[0] <= 0:
        notes.append(f"节点超过 {MAX_NODES}，只画了前 {MAX_NODES} 个")

    offsets = _level_offsets(list(_iter(root)))

    if two_sided and root.children:
        right, left = _split_sides(root)
        for child in right:
            for box in _iter(child):
                box.side = 1
        for child in left:
            for box in _iter(child):
                box.side = -1

        # 先把两侧各自排成一列，再让较矮的一侧居中
        _measure(group_pseudo := _Box(root_node, 0, 0, 0, children=right))
        _place(group_pseudo, 0.0)
        _measure(left_pseudo := _Box(root_node, 0, 0, 0, children=left))
        _place(left_pseudo, 0.0)

        right_ext = group_pseudo.extent if right else 0.0
        left_ext = left_pseudo.extent if left else 0.0
        for child in left:
            for box in _iter(child):
                box.y += (right_ext - left_ext) / 2

        all_boxes = list(_iter(root))
        ys = [box.y for box in all_boxes if box is not root]
        root.y = (min(ys) + max(ys)) / 2 if ys else 0.0
        root.side = 0
        # 根节点自己也要参与，x 由 offsets[0]=0 决定
        boxes = [root] + [b for child in root.children for b in _iter(child)]
    else:
        _measure(root)
        _place(root, 0.0)
        boxes = list(_iter(root))
        for box in boxes:
            if box is not root:
                box.side = 1

    for box in boxes:
        box.x = box.side * offsets.get(box.depth, 0.0)

    font = _pick_font()
    if font is None:
        notes.append("系统缺中文字体，中文可能显示为方框")
    if not two_sided:
        notes.append("非思维导图布局按右侧树状近似绘制")
    notes.append("不包含边界、概要、关系线、图片内容")

    with _RENDER_LOCK:
        try:
            png = _draw(boxes, style_of, font, max_width)
        except Exception as exc:  # 渲染失败不应该让工具直接崩
            return None, f"渲染失败: {type(exc).__name__}: {exc}"
    return png, "；".join(notes)
