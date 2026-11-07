"""Golden 测试：写入器产出的结构必须与真实 XMind 文件的结构一致。

golden 数据从仓库内真实文件提取：
- tests/对比.xmind（XMind 26 导出：矩阵图 / 树型表格 / 电子表格扩展）
- examples/属性验证.xmind、examples/高级功能验证.xmind（样式属性键、概要/边界/关系线/图片）
- tests/第四章 文件管理.xmind（XMind 的 Markdown 导入：显式备注结构）

如果真实文件更新导致 golden 与文件不一致，对应测试会失败，
提醒先更新本文件里的常量，再改代码。
"""

import base64
import json
import zipfile
from pathlib import Path

import pytest

from xmind_mcp.config import Config
from xmind_mcp.tools import XMindTools, _parse_markdown_outline
from xmind_mcp.xmind_parser import parse_xmind
from xmind_mcp.xmind_writer import XMindWriter


PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)


@pytest.fixture
def config() -> Config:
    return Config(ocr_engine="rapidocr", device="cpu", initialized=True)

# ---- golden 常量（取自 tests/对比.xmind）----
MATRIX_STRUCTURE_CLASS = "org.xmind.ui.spreadsheet"
MATRIX_COLUMNS = [
    ("column", "标签 1"),
    ("column", "标签 2"),
    ("column", "标签 3"),
    ("column", "标签 4"),
    ("column", "标签 5"),
    ("leading-column", ""),
]
TREETABLE_STRUCTURE_CLASS = "org.xmind.ui.treetable.toptitle"
TABLE_IMPORT_COLUMNS = [
    ("leading-column", "结构"),
    ("column", "顺序访问"),
    ("column", "随机访问"),
    ("column", "碎片"),
    ("column", "增长"),
]

# ---- 真实 XMind 文件里的字段集合 ----
REAL_STYLE_KEYS = {
    "alignment-by-level", "arrow-begin-class", "arrow-end-class",
    "border-line-color", "border-line-pattern", "border-line-width",
    "color-list", "fill-pattern", "fo:color", "fo:font-family",
    "fo:font-size", "fo:font-style", "fo:font-weight", "fo:text-align",
    "fo:text-decoration", "fo:text-transform", "line-class", "line-color",
    "line-pattern", "line-tapered", "line-width", "multi-line-colors",
    "shape-class", "svg:fill",
}
REAL_SUMMARY_KEYS = {"id", "range", "topicId"}
REAL_BOUNDARY_KEYS = {"id", "range", "title"}  # 真实文件里边界可带 title
REAL_RELATIONSHIP_KEYS = {"end1Id", "end2Id", "id", "title"}
REAL_IMAGE_KEYS = {"src"}


def _load(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read("content.json"))


def _walk_all(topic: dict):
    yield topic
    for key in ("attached", "detached", "summary", "callout"):
        for child in topic.get("children", {}).get(key, []):
            yield from _walk_all(child)


def _extension_columns(topic: dict) -> list[tuple[str, str]]:
    extensions = topic.get("extensions") or []
    if not extensions:
        return []
    columns = extensions[0]["content"][0]["content"]
    return [(col.get("name", ""), col.get("content", "")) for col in columns]


def test_golden_matches_real_comparison_file():
    """golden 常量本身要与 tests/对比.xmind 里的真实结构一致。"""
    path = Path(__file__).with_name("对比.xmind")
    assert path.exists()
    root = _load(path)[0]["rootTopic"]

    matrix = None
    treetable = None
    table = None
    for topic in _walk_all(root):
        sc = topic.get("structureClass") or ""
        columns = _extension_columns(topic)
        if sc == "org.xmind.ui.spreadsheet" and columns and columns[-1] == ("leading-column", ""):
            matrix = topic
        elif sc == "org.xmind.ui.treetable.toptitle":
            treetable = topic
        elif sc == "org.xmind.ui.spreadsheet" and columns and columns[0][0] == "leading-column":
            table = topic

    assert matrix is not None, "对比.xmind 里没找到矩阵图节点"
    assert matrix["structureClass"] == MATRIX_STRUCTURE_CLASS
    assert _extension_columns(matrix) == MATRIX_COLUMNS

    assert treetable is not None, "对比.xmind 里没找到树型表格节点"
    assert treetable["structureClass"] == TREETABLE_STRUCTURE_CLASS
    assert treetable.get("extensions") in (None, [])

    assert table is not None, "对比.xmind 里没找到表格导入节点"
    assert _extension_columns(table) == TABLE_IMPORT_COLUMNS


def test_set_sheet_layout_matches_golden(tmp_path: Path):
    """set_sheet_layout 切矩阵图/树型表格要产出和真实文件一致的结构。"""
    path = tmp_path / "layout.xmind"
    XMindWriter.create(path, "根")
    writer = XMindWriter(parse_xmind(path))

    assert writer.set_sheet_layout(0, "矩阵图")
    root = writer.xm.raw_content[0]["rootTopic"]
    assert root["structureClass"] == MATRIX_STRUCTURE_CLASS
    assert _extension_columns(root) == MATRIX_COLUMNS

    assert writer.set_sheet_layout(0, "树型表格")
    root = writer.xm.raw_content[0]["rootTopic"]
    assert root["structureClass"] == TREETABLE_STRUCTURE_CLASS
    assert "extensions" not in root  # 树型表格不需要扩展，且要清掉旧的

    assert writer.set_sheet_layout(0, "org.xmind.ui.logic.right")
    root = writer.xm.raw_content[0]["rootTopic"]
    assert root["structureClass"] == "org.xmind.ui.logic.right"
    assert "extensions" not in root


def test_writer_style_keys_are_real_xmind_keys(tmp_path: Path):
    """set_node_style 写出的属性键必须是真实 XMind 用过的键。"""
    path = tmp_path / "style.xmind"
    XMindWriter.create(path, "根")
    writer = XMindWriter(parse_xmind(path))
    root_id = writer.xm.sheets[0].root.id
    writer.add_node(root_id, "子")
    child_id = writer.xm.sheets[0].root.children[0].id
    writer.set_node_style(
        child_id,
        font_color="#FF0000",
        font_size="14pt",
        font_family="Arial",
        font_weight="bold",
        font_style="italic",
        font_transform="uppercase",
        font_decoration="underline",
        background_color="#FFFFFF",
        fill_pattern="solid",
        border_color="#000000",
        border_width="2pt",
        border_pattern="dash",
        shape="org.xmind.topicShape.circle",
        line_color="#123456",
        line_width="3pt",
        line_pattern="dot",
        text_align="center",
    )
    props = writer.xm.find_topic(child_id).style["properties"]
    assert set(props) <= REAL_STYLE_KEYS


def test_writer_structure_shapes_match_real(tmp_path: Path):
    """概要/边界/关系线/图片/备注的字段形状要与真实文件一致。"""
    path = tmp_path / "shapes.xmind"
    XMindWriter.create(path, "根")
    writer = XMindWriter(parse_xmind(path))
    root_id = writer.xm.sheets[0].root.id
    writer.add_node(root_id, "子")
    child_id = writer.xm.sheets[0].root.children[0].id

    writer.add_summary(root_id, child_id, child_id, "概要")
    writer.add_boundary(root_id, child_id, child_id, "边界")
    writer.add_relationship(0, root_id, child_id, "关系")
    writer.set_hyperlink(child_id, "https://example.com")
    writer.add_marker(child_id, "priority-1")
    writer.add_label(child_id, "重点")
    writer.update_node(child_id, notes="备注")

    root = writer.xm.raw_content[0]["rootTopic"]
    assert set(root["summaries"][0]) == REAL_SUMMARY_KEYS
    assert set(root["boundaries"][0]) <= REAL_BOUNDARY_KEYS
    assert set(writer.xm.raw_content[0]["relationships"][0]) == REAL_RELATIONSHIP_KEYS

    child = writer.xm.find_topic(child_id)
    assert set(child._raw["hyperlink"]) == {"link", "title"}
    # 官方格式：markers 是 [{"markerId": "..."}]，xmind-cli validate 会检查
    assert child._raw["markers"] == [{"markerId": "priority-1"}]
    assert child._raw["labels"] == ["重点"]
    assert "plain" in child._raw["notes"]  # update_node 默认侧边栏备注，plain 是 XMind 兼容子集

    png = tmp_path / "pic.png"
    png.write_bytes(base64.b64decode(PNG_B64))
    writer.add_image(child_id, png)
    assert set(child._raw["image"]) == REAL_IMAGE_KEYS


def test_markdown_table_extension_matches_real():
    """Markdown 表格导入的列扩展要与 XMind 软件导入的真实结构一致。"""
    root, topics, _ = _parse_markdown_outline(
        "# 根\n"
        "## 对比\n"
        "| 结构 | 顺序访问 | 随机访问 | 碎片 | 增长 |\n"
        "|------|---------|---------|------|------|\n"
        "| 连续 | 快 | 快 | 外部 | 难 |\n"
    )
    section = topics[0]
    assert section["structureClass"] == MATRIX_STRUCTURE_CLASS
    extensions = section["extensions"][0]
    assert extensions["provider"] == "org.xmind.ui.spreadsheet"
    columns = extensions["content"][0]["content"]
    assert [(c["name"], c["content"]) for c in columns] == TABLE_IMPORT_COLUMNS


async def test_blockquote_note_matches_real(tmp_path: Path, config):
    """引用块备注的结构要与 XMind Markdown 导入（第四章 文件管理.xmind）一致。"""
    tools = XMindTools(config)
    path = tmp_path / "note.xmind"
    result = await tools.call_tool("import_markdown", {
        "file_path": str(path),
        "markdown": "# 根\n- 注意：要点\n> 补充说明\n",
    })
    assert "已从 Markdown 创建" in result[0].text
    xm = parse_xmind(path)
    note_node = next(n for n in xm.all_topics() if n.title == "注意：要点")
    raw = note_node._raw
    assert raw["notesDisplay"] == "block"
    assert set(raw["notes"]) == {"plain", "realHTML"}
    assert raw["notes"]["realHTML"]["content"].startswith("<div>")
