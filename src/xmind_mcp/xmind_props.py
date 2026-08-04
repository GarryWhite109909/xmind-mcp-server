"""XMind 属性字典：把用户可读的中文/常见名称映射成 XMind 内部字段值。

所有值都取自 XMind 26 实际写入文件的字段（从安装包资源和真实样张中提取），
保证写进 content.json 后 XMind 能正确识别。
"""

from __future__ import annotations

import re
from typing import Mapping, Optional


# 主题重要程度 → topic 的 class 字段
IMPORTANCE: dict[str, str] = {
    "极其重要": "importantTopic",
    "重要": "minorTopic",
    "删去": "expiredTopic",
    "默认": "topic",
}

# 主题形状 → shape-class
SHAPES: dict[str, str] = {
    "矩形": "org.xmind.topicShape.rect",
    "圆角矩形": "org.xmind.topicShape.roundedRect",
    "下划线": "org.xmind.topicShape.underline",
    "双下划线": "org.xmind.topicShape.doubleunderline",
    "圆形": "org.xmind.topicShape.circle",
    "椭圆": "org.xmind.topicShape.ellipse",
    "椭圆形": "org.xmind.topicShape.ellipse",
    "菱形": "org.xmind.topicShape.diamond",
    "平行四边形": "org.xmind.topicShape.parallelogram",
    "跑道": "org.xmind.topicShape.ellipserect",
    "跑道形": "org.xmind.topicShape.ellipserect",
    "胶囊": "org.xmind.topicShape.ellipserect",
    "胶囊形": "org.xmind.topicShape.ellipserect",
    # 切角矩形：矩形切掉下角形成的六边形（尖尖朝下），用户用 XMind 实测确认为 roundedhexagon。
    "切角矩形": "org.xmind.topicShape.roundedhexagon",
    "切角菱形": "org.xmind.topicShape.cutdiamond",
    "六边形": "org.xmind.topicShape.hexagon",
    "竖胶囊": "org.xmind.topicShape.ellipticrectangle",
    "竖胶囊形": "org.xmind.topicShape.ellipticrectangle",
    "竖向胶囊": "org.xmind.topicShape.ellipticrectangle",
}

# 主题填充样式 → fill-pattern
FILL_PATTERNS: dict[str, str] = {
    "无": "none",
    "纯色": "solid",
    "手绘": "solid-hand-drawn",
    "交叉线": "crossing",
    "细交叉线": "crossing-thin",
    "影线": "hachure",
    "细影线": "hachure-thin",
}

# 边框/线条线型 → border-line-pattern / line-pattern
BORDER_PATTERNS: dict[str, str] = {
    "无": "none",
    "实线": "solid",
    "纯线": "solid",
    "虚线": "dash",
    "点线": "dot",
    "点划线": "dash-dot",
    "手绘实线": "handdrawn-solid",
    "手绘虚线": "handdrawn-dash",
}

# 线宽预设（极细/细/中等/粗/极粗）→ pt
LINE_WIDTH_PRESETS: dict[str, str] = {
    "极细": "1pt",
    "细": "2pt",
    "中等": "3pt",
    "粗": "5pt",
    "极粗": "8pt",
}

# 字号预设（XMind 快捷字号）
FONT_SIZES: dict[str, str] = {
    "8": "8pt",
    "10": "10pt",
    "12": "12pt",
    "14": "14pt",
    "18": "18pt",
    "24": "24pt",
    "36": "36pt",
    "48": "48pt",
    "60": "60pt",
}

# 文字变换 → fo:text-transform
TEXT_TRANSFORMS: dict[str, str] = {
    "默认": "manual",
    "无": "none",
    "全大写": "uppercase",
    "全小写": "lowercase",
    "首字母大写": "capitalize",
}

# 文字装饰（可叠加，用 + 连接，如 "下划线+删除线"）→ fo:text-decoration
TEXT_DECORATIONS: dict[str, str] = {
    "无": "none",
    "下划线": "underline",
    "删除线": "line-through",
}

# 对齐 → fo:text-align
TEXT_ALIGNS: dict[str, str] = {
    "默认": "inherited",
    "左对齐": "left",
    "居中": "center",
    "右对齐": "right",
}

# 字重/斜体 → fo:font-weight / fo:font-style
FONT_WEIGHTS: dict[str, str] = {
    "正常": "normal",
    "普通": "normal",
    "加粗": "bold",
    "粗体": "bold",
}
FONT_STYLES: dict[str, str] = {
    "正常": "normal",
    "斜体": "italic",
}

# 布局结构 → structureClass
STRUCTURES: dict[str, str] = {
    "思维导图": "org.xmind.ui.map.clockwise",
    "逻辑图": "org.xmind.ui.logic.right",
    "括号图": "org.xmind.ui.brace.right",
    "组织结构图": "org.xmind.ui.org-chart.down",
    "树形图": "org.xmind.ui.tree.right",
    "时间轴": "org.xmind.ui.timeline.horizontal",
    "鱼骨图": "org.xmind.ui.fishbone.rightHeaded",
    "矩阵图": "org.xmind.ui.grid",
    "树型表格": "org.xmind.ui.treetable",
}

# 分支连线类型 → line-class
BRANCH_LINES: dict[str, str] = {
    "圆角折线": "org.xmind.branchConnection.roundedElbow",
    "折线": "org.xmind.branchConnection.elbow",
    "直线": "org.xmind.branchConnection.straight",
    "曲线": "org.xmind.branchConnection.curve",
    "圆弧": "org.xmind.branchConnection.bight",
    "手绘": "org.xmind.branchConnection.fold",
    "圆角手绘": "org.xmind.branchConnection.roundedfold",
    "无": "org.xmind.branchConnection.none",
    "括号": "org.xmind.branchConnection.brace",
    "箭头曲线": "org.xmind.branchConnection.arrowedCurve",
}

# 终点/起点样式 → arrow-end-class / arrow-begin-class
ARROW_ENDS: dict[str, str] = {
    "无": "org.xmind.arrowShape.none",
    "圆点": "org.xmind.arrowShape.dot",
    "三角形": "org.xmind.arrowShape.triangle",
    "矛头": "org.xmind.arrowShape.spearhead",
    "方形": "org.xmind.arrowShape.square",
    "菱形": "org.xmind.arrowShape.diamond",
    "鱼骨": "org.xmind.arrowShape.herringbone",
    "双箭头": "org.xmind.arrowShape.doublearrow",
    "反三角": "org.xmind.arrowShape.antiTriangle",
    "附着": "org.xmind.arrowShape.attached",
    "钩形": "org.xmind.arrowShape.hook",
}

# 主题级样式的层级 → sheet.theme 的键
THEME_LEVELS: dict[str, str] = {
    "中心主题": "centralTopic",
    "分支主题": "mainTopic",
    "第三层级主题": "subTopic",
    "细分主题": "level3",
    "自由主题": "floatingTopic",
}


def resolve(mapping: Mapping[str, str], value: object, *, label: str = "值") -> Optional[str]:
    """把用户输入解析成 XMind 内部 ID。

    支持中文名称、XMind 内部 ID、以及去掉命名空间前缀的简写
    （如 ellipse / topicShape.ellipse）。
    """
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    if key in mapping:
        return mapping[key]
    if key in mapping.values():
        return key
    # 完整的 XMind 内部 ID 直接放行（高级用法）
    if key.startswith("org.xmind."):
        return key
    # 简写：ellipse / topicShape.ellipse / branchConnection.curve
    short = key.split(".")[-1]
    for mapped in mapping.values():
        if mapped.split(".")[-1] == short:
            return mapped
    raise ValueError(f"{label}不支持「{key}」，可选: {'、'.join(mapping)}")


_HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")
_HEX_COLOR_ALPHA = re.compile(r"^#?([0-9a-fA-F]{8})$")
_COLOR_WITH_PERCENT = re.compile(r"^#?([0-9a-fA-F]{6})\s*(\d{1,3})%?$")


def normalize_color(color: object) -> Optional[str]:
    """把颜色归一化成 XMind 的 #RRGGBB 或 #RRGGBBAA。

    支持：
      #4E0D58 / 4E0D58（6 位）
      #4E0D5866 / 4E0D5866（8 位，最后两位是不透明度）
      4E0D58 60%（6 位颜色 + 不透明度百分比，60% ≈ #99）
    """
    if color is None:
        return None
    text = str(color).strip()
    if not text:
        return None
    m = _COLOR_WITH_PERCENT.match(text)
    if m:
        rgb = m.group(1).upper()
        percent = int(m.group(2))
        percent = max(0, min(100, percent))
        alpha = round(percent * 255 / 100)
        return f"#{rgb}{alpha:02X}"
    m = _HEX_COLOR_ALPHA.match(text)
    if m:
        return "#" + m.group(1).upper()
    m = _HEX_COLOR.match(text)
    if m:
        return "#" + m.group(1).upper()
    raise ValueError(f"颜色格式不支持「{text}」，请用 #RRGGBB、#RRGGBBAA 或「4E0D58 60%」")


def resolve_text_decoration(value: object) -> Optional[str]:
    """解析文字装饰，支持叠加，如「下划线+删除线」。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in TEXT_DECORATIONS:
        return TEXT_DECORATIONS[text]
    parts = []
    for part in re.split(r"[\s+、,，]+", text):
        part = part.strip()
        if not part:
            continue
        if part not in TEXT_DECORATIONS:
            raise ValueError(f"文字装饰不支持「{part}」，可选: {'、'.join(TEXT_DECORATIONS)}")
        resolved = TEXT_DECORATIONS[part]
        if resolved != "none" and resolved not in parts:
            parts.append(resolved)
    return " ".join(parts) if parts else "none"


def resolve_font_size(value: object) -> Optional[str]:
    """字号：接受数字（8/12）、预设键、或 12pt / 16px 写法。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in FONT_SIZES:
        return FONT_SIZES[text]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(pt|px)?", text)
    if m:
        return f"{m.group(1)}pt"
    raise ValueError(f"字号不支持「{text}」，可选: {'、'.join(FONT_SIZES)} 或任意数字+pt")


def resolve_width(value: object, *, label: str = "线宽") -> Optional[str]:
    """线宽：接受极细/细/中等/粗/极粗，或 1pt / 2px / 1 写法。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in LINE_WIDTH_PRESETS:
        return LINE_WIDTH_PRESETS[text]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(pt|px)?", text)
    if m:
        return f"{m.group(1)}pt"
    raise ValueError(f"{label}不支持「{text}」，可选: {'、'.join(LINE_WIDTH_PRESETS)} 或数字+pt")
