"""Markdown → XMind 导入：逐行原样转换，处理标题/列表/表格/引用/块级公式。"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from typing import Any, Optional

from .formula import render_latex_png
from .xmind_parser import XMindFile
from .xmind_writer import XMindWriter


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

    # 整体归一化：最小层级变成 1。只有引用块时没有 level，按 1 处理。
    levels = [item["level"] for item in items if item.get("level") is not None]
    min_level = min(levels) if levels else 1

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
