"""自绘预览渲染器测试。"""

import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from xmind_mcp.config import Config
from xmind_mcp.render import MAX_NODES, render_sheet_png
from xmind_mcp.xmind_parser import parse_xmind


def _make_xmind(path: Path, sheets: list[dict]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(sheets, ensure_ascii=False))
        zf.writestr("metadata.json", json.dumps({"dataStructureVersion": "3"}))
    return path


def _sheet(title: str, root: dict, sheet_id: str = "sheet-1") -> dict:
    return {"id": sheet_id, "title": title, "rootTopic": root}


def _tree(children: list[str], *, structure: str = "") -> dict:
    root = {
        "id": "root-1",
        "title": "根主题",
        "children": {"attached": [
            {"id": f"c-{i}", "title": title} for i, title in enumerate(children)
        ]},
    }
    if structure:
        root["structureClass"] = structure
    return root


def _png_size(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


def test_render_produces_png(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)

    png, note = render_sheet_png(xm)

    assert png is not None
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 1000
    assert "不包含边界" in note


def test_render_invalid_sheet_index(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)

    png, note = render_sheet_png(xm, sheet_index=3)

    assert png is None
    assert "索引无效" in note


def test_render_single_topic_sheet(tmp_path: Path):
    path = _make_xmind(tmp_path / "single.xmind", [_sheet("单节点", {
        "id": "root-1", "title": "只有根主题",
    })])

    png, _ = render_sheet_png(parse_xmind(path))

    assert png is not None
    width, height = _png_size(png)
    assert width > 0 and height > 0


def test_render_respects_max_width(tmp_path: Path):
    path = _make_xmind(tmp_path / "wide.xmind", [_sheet("宽图", _tree([
        "一很长很长很长很长很长很长很长很长很长很长的分支标题",
        "另一条同样很长很长很长很长很长很长很长很长的分支标题",
    ]))])
    xm = parse_xmind(path)

    narrow, _ = render_sheet_png(xm, max_width=600)
    wide, _ = render_sheet_png(xm, max_width=1200)

    assert narrow is not None and wide is not None
    assert _png_size(narrow)[0] <= 700
    assert _png_size(wide)[0] <= 1300
    assert _png_size(wide)[0] > _png_size(narrow)[0]


def test_render_truncates_huge_tree(tmp_path: Path):
    children = [f"节点{i}" for i in range(MAX_NODES + 80)]
    path = _make_xmind(tmp_path / "huge.xmind", [_sheet("大图", _tree(children))])

    png, note = render_sheet_png(parse_xmind(path))

    assert png is not None
    assert str(MAX_NODES) in note and "只画了前" in note


def test_render_map_layout_is_two_sided(tmp_path: Path):
    children = [f"分支{i}" for i in range(8)]
    map_path = _make_xmind(tmp_path / "map.xmind", [_sheet("脑图", _tree(
        children, structure="org.xmind.ui.map.clockwise"
    ))])
    tree_path = _make_xmind(tmp_path / "tree.xmind", [_sheet("逻辑图", _tree(children))])

    map_png, map_note = render_sheet_png(parse_xmind(map_path))
    tree_png, tree_note = render_sheet_png(parse_xmind(tree_path))

    assert map_png is not None and tree_png is not None
    assert "非思维导图布局" not in map_note
    assert "非思维导图布局" in tree_note
    map_ratio = _png_size(map_png)[0] / _png_size(map_png)[1]
    tree_ratio = _png_size(tree_png)[0] / _png_size(tree_png)[1]
    assert map_ratio > tree_ratio  # 左右分列比单侧树状更宽


async def test_get_preview_renders_requested_sheet(tmp_path: Path):
    from xmind_mcp.tools import XMindTools

    path = _make_xmind(tmp_path / "multi.xmind", [
        _sheet("第一张", _tree(["甲", "乙"]), "sheet-1"),
        _sheet("第二张", _tree(["丙", "丁"]), "sheet-2"),
    ])
    tools = XMindTools(Config())

    blocks = await tools.call_tool(
        "get_preview", {"file_path": str(path), "source": "render", "sheet": 1}
    )

    images = [b for b in blocks if getattr(b, "type", "") == "image"]
    assert len(images) == 1
    assert "第二张" in blocks[0].text


@pytest.mark.parametrize("sheet", [5, -1])
async def test_get_preview_out_of_range_sheet(tmp_path: Path, sheet: int):
    from xmind_mcp.tools import XMindTools

    path = _make_xmind(tmp_path / "one.xmind", [_sheet("唯一", _tree(["甲"]))])
    tools = XMindTools(Config())

    blocks = await tools.call_tool(
        "get_preview", {"file_path": str(path), "source": "render", "sheet": sheet}
    )

    assert all(getattr(b, "type", "") == "text" for b in blocks)
    assert "超出范围" in blocks[0].text
