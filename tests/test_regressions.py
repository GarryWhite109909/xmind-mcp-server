"""回归测试：覆盖审查中发现并修复的问题。"""

import base64
import json
import zipfile
from pathlib import Path

import pytest

from xmind_mcp.config import Config
from xmind_mcp.ocr.base import OcrResult
from xmind_mcp.tools import XMindTools, _effective_style, _parse_markdown_outline
from xmind_mcp.xmind_parser import parse_xmind
from xmind_mcp.xmind_writer import XMindWriter, list_backups


@pytest.fixture
def config() -> Config:
    return Config(ocr_engine="rapidocr", device="cpu", initialized=True)


PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)


def _make_xmind(path: Path, content: list[dict], extra: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))
        for name, data in (extra or {}).items():
            zf.writestr(name, data)


def test_move_node_invalid_position_keeps_node(tmp_path: Path):
    """move_node 的 before_id/after_id 无效时，节点不能从原位置消失。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "a", "title": "A", "children": {"attached": [{"id": "a1", "title": "A1"}]}},
        {"id": "b", "title": "B"},
    ]}}}]
    p = tmp_path / "move.xmind"
    _make_xmind(p, content)
    xm = parse_xmind(p)
    w = XMindWriter(xm)

    with pytest.raises(ValueError):
        w.move_node("a1", "b", before_id="not-exist")

    # 下一次 refresh / save 后 a1 必须还在
    w.xm.refresh_from_raw()
    assert "a1" in [n.id for n in w.xm.all_topics()]
    out = tmp_path / "out.xmind"
    w.save(out)
    xm2 = parse_xmind(out)
    assert "a1" in [n.id for n in xm2.all_topics()]


def test_delete_node_remaps_boundary_range(tmp_path: Path):
    """删除兄弟节点后，边界区间要按节点 ID 重算，不能错位。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "a", "title": "A"},
        {"id": "b", "title": "B"},
        {"id": "c", "title": "C"},
        {"id": "d", "title": "D"},
    ]}, "boundaries": [{"id": "bd", "range": "(1,2)"}]}}]
    p = tmp_path / "del.xmind"
    _make_xmind(p, content)
    w = XMindWriter(parse_xmind(p))
    w.delete_node("a")
    raw = w.xm.raw_content[0]["rootTopic"]
    assert raw["boundaries"][0]["range"] == "(0,1)"


def test_add_node_remaps_boundary_range(tmp_path: Path):
    """在兄弟前面插入节点后，边界区间要整体后移。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "a", "title": "A"},
        {"id": "b", "title": "B"},
        {"id": "c", "title": "C"},
    ]}, "boundaries": [{"id": "bd", "range": "(1,2)"}]}}]
    p = tmp_path / "add.xmind"
    _make_xmind(p, content)
    w = XMindWriter(parse_xmind(p))
    w.add_node("root", "X", before_id="a")
    raw = w.xm.raw_content[0]["rootTopic"]
    assert raw["boundaries"][0]["range"] == "(2,3)"


def test_move_node_remaps_boundary_range(tmp_path: Path):
    """把节点移出父节点后，原父节点上的边界区间要重算。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "p", "title": "P", "children": {"attached": [
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B"},
            {"id": "c", "title": "C"},
        ]}, "boundaries": [{"id": "bd", "range": "(1,2)"}]},
        {"id": "q", "title": "Q", "children": {"attached": [{"id": "x", "title": "X"}]}},
    ]}}}]
    p = tmp_path / "mv.xmind"
    _make_xmind(p, content)
    w = XMindWriter(parse_xmind(p))
    assert w.move_node("a", "q")
    p_raw = w.xm.find_topic("p")._raw
    assert p_raw["boundaries"][0]["range"] == "(0,1)"


def test_effective_style_includes_theme_level(tmp_path: Path):
    """get_node 的继承样式必须包含主题层级默认样式（mainTopic 等）。"""
    theme = {"mainTopic": {"id": "t1", "properties": {"fo:font-size": "20pt"}}}
    content = [{"id": "s", "title": "S", "theme": theme, "rootTopic": {
        "id": "root", "title": "R", "children": {"attached": [{"id": "b", "title": "B"}]},
    }}]
    p = tmp_path / "theme.xmind"
    _make_xmind(p, content)
    xm = parse_xmind(p)
    child = xm.find_topic("b")
    assert _effective_style(child, xm)["fo:font-size"] == "20pt"


def test_effective_style_floating_topic_theme(tmp_path: Path):
    """浮动主题继承 floatingTopic 主题样式。"""
    theme = {"floatingTopic": {"id": "t1", "properties": {"fo:font-size": "16pt"}}}
    content = [{"id": "s", "title": "S", "theme": theme, "rootTopic": {
        "id": "root", "title": "R",
        "children": {"attached": [], "detached": [{"id": "f", "title": "F"}]},
    }}]
    p = tmp_path / "float.xmind"
    _make_xmind(p, content)
    xm = parse_xmind(p)
    assert _effective_style(xm.find_topic("f"), xm)["fo:font-size"] == "16pt"


def test_repair_missing_content_json_rebuilds_and_keeps_attachments(tmp_path: Path):
    """content.json 缺失时 repair 应重建空白画布，并保留附件资源。"""
    broken = tmp_path / "broken.xmind"
    with zipfile.ZipFile(broken, "w") as zf:
        zf.writestr("metadata.json", "{}")
        zf.writestr("attachments/images/pic.png", base64.b64decode(PNG_B64))

    repaired = tmp_path / "repaired.xmind"
    writer = XMindWriter.repair(broken, repaired)
    assert repaired.exists()
    assert len(writer.xm.all_topics()) == 1  # 重建的空白根主题
    with zipfile.ZipFile(repaired) as zf:
        names = zf.namelist()
        assert "content.json" in names
        assert "metadata.json" in names
        assert "attachments/images/pic.png" in names
    xm = parse_xmind(repaired)
    assert xm.sheets[0].root.title == "未命名"


def test_repair_non_zip_file_creates_valid_xmind(tmp_path: Path):
    """完全不是 zip 的文件也能 repair 成一个可打开的空白 XMind。"""
    broken = tmp_path / "broken.xmind"
    broken.write_bytes(b"this is not a zip file")
    repaired = tmp_path / "repaired.xmind"
    XMindWriter.repair(broken, repaired)
    xm = parse_xmind(repaired)
    assert xm.sheets[0].root.title == "未命名"


def test_parse_markdown_flat_list_becomes_siblings():
    """平铺列表的兄弟条目不能错挂成父子。"""
    root, topics = _parse_markdown_outline("- apple\n- banana\n- cherry\n")
    assert root == "未命名"
    assert [t["title"] for t in topics] == ["apple", "banana", "cherry"]
    assert all(not t["children"] for t in topics)


def test_parse_markdown_multiple_h1_becomes_siblings():
    """多个 # 一级标题应保持兄弟关系。"""
    root, topics = _parse_markdown_outline("# A\n# B\n# C\n")
    assert root == "未命名"
    assert [t["title"] for t in topics] == ["A", "B", "C"]


def test_set_node_style_atomic_on_error(tmp_path: Path):
    """样式参数非法时，不能留下部分修改。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "c1", "title": "C1"},
    ]}}}]
    p = tmp_path / "style.xmind"
    _make_xmind(p, content)
    w = XMindWriter(parse_xmind(p))

    with pytest.raises(ValueError):
        w.set_node_style("c1", font_color="#FF0000", shape="不存在的形状")

    raw = w.xm.find_topic("c1")._raw
    assert "style" not in raw or "fo:color" not in raw.get("style", {}).get("properties", {})


def test_apply_matching_atomic_on_invalid_style(tmp_path: Path):
    """apply_matching 的样式参数非法时，不能改到一半。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "c1", "title": "重点A"},
        {"id": "c2", "title": "重点B"},
    ]}}}]
    p = tmp_path / "match.xmind"
    _make_xmind(p, content)
    w = XMindWriter(parse_xmind(p))

    with pytest.raises(ValueError):
        w.apply_matching("重点", style={"font_color": "#FF0000", "shape": "不存在的形状"})

    for node_id in ("c1", "c2"):
        raw = w.xm.find_topic(node_id)._raw
        assert "style" not in raw or "fo:color" not in raw.get("style", {}).get("properties", {})


async def test_read_all_ocr_respects_max_depth(config: Config, tmp_path: Path):
    """read_all 的 max_depth 之外的图片不应触发 OCR。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "img1", "title": "一层图", "image": {"src": "xap:images/a.png"}},
        {"id": "n", "title": "分支", "children": {"attached": [
            {"id": "img2", "title": "二层图", "image": {"src": "xap:images/b.png"}},
        ]}},
    ]}}}]
    p = tmp_path / "ocr.xmind"
    _make_xmind(p, content, {"attachments/images/a.png": base64.b64decode(PNG_B64),
                             "attachments/images/b.png": base64.b64decode(PNG_B64)})

    tools = XMindTools(config)
    calls = []

    def fake_recognize(image):
        calls.append(image)
        return OcrResult(text="TEXT", lines=[])

    tools.ocr.recognize = fake_recognize
    result = await tools.call_tool("read_all", {"file_path": str(p), "max_depth": 1})
    assert len(calls) == 1
    assert "TEXT" in result[0].text


async def test_read_all_truncates_long_notes(config: Config, tmp_path: Path):
    """超长备注要截断，避免撑爆上下文。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "c1", "title": "C1", "notes": {"plain": {"content": "长" * 5000}}},
    ]}}}]
    p = tmp_path / "notes.xmind"
    _make_xmind(p, content)
    tools = XMindTools(config)
    result = await tools.call_tool("read_all", {"file_path": str(p)})
    text = result[0].text
    assert "已截断" in text
    assert len(text) < 5000


async def test_call_tool_status_marks_errors(config: Config, sample_xmind: Path, tmp_path: Path):
    """MCP 调用失败时要能区分 isError。"""
    tools = XMindTools(config)

    _, err = await tools.call_tool_status("no_such_tool", {})
    assert err is True

    _, err = await tools.call_tool_status("read_structure", {"file_path": str(sample_xmind)})
    assert err is False

    _, err = await tools.call_tool_status(
        "read_structure", {"file_path": str(tmp_path / "不存在.xmind")}
    )
    assert err is True


def test_create_file_backs_up_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """create_file 覆盖已有文件前要自动备份。"""
    from xmind_mcp import xmind_writer

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(xmind_writer, "BACKUP_DIR", backup_dir)
    p = tmp_path / "created.xmind"
    XMindWriter.create(p, "第一次")
    XMindWriter.create(p, "第二次")
    backups = list(backup_dir.glob("created-*.xmind"))
    assert len(backups) == 1
    with zipfile.ZipFile(backups[0]) as zf:
        content = json.loads(zf.read("content.json"))
        assert content[0]["rootTopic"]["title"] == "第一次"


def test_list_and_restore_backup(tmp_path: Path):
    """自动备份可以列出，并且能恢复。"""
    p = tmp_path / "bk.xmind"
    XMindWriter.create(p, "第一次")
    xm = parse_xmind(p)
    root_id = xm.sheets[0].root.id
    w = XMindWriter(xm)
    assert w.update_node(root_id, title="第二次")
    w.save()

    backups = list_backups(str(p))
    assert len(backups) == 1
    backup_path = backups[0]["path"]

    from xmind_mcp.xmind_writer import restore_backup
    restore_backup(str(p), backup_path)
    xm2 = parse_xmind(p)
    assert xm2.sheets[0].root.title == "第一次"
    # 恢复前当前文件也被备份了 → 现在有两份
    assert len(list_backups(str(p))) == 2


def test_sheet_management(tmp_path: Path):
    """新增/重命名/移动/删除工作表。"""
    p = tmp_path / "sheets.xmind"
    XMindWriter.create(p, "根")
    w = XMindWriter(parse_xmind(p))
    assert len(w.xm.raw_content) == 1

    idx = w.add_sheet(title="第二页", root_title="新根")
    assert idx == 1
    assert len(w.xm.raw_content) == 2
    assert w.xm.sheets[1].title == "第二页"

    assert w.rename_sheet(1, "改名页")
    assert w.xm.sheets[1].title == "改名页"

    assert w.move_sheet(1, 0)
    assert w.xm.sheets[0].title == "改名页"

    assert w.delete_sheet(0)
    assert len(w.xm.raw_content) == 1
    # 最后一张不能删
    assert not w.delete_sheet(0)


def test_batch_update_titles_and_delete_empty(tmp_path: Path):
    """批量改标题（含正则）与删除空节点。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "章节1A", "children": {"attached": [
        {"id": "c1", "title": "章节1B"},
        {"id": "c2", "title": ""},
        {"id": "c3", "title": "   "},
    ]}}}]
    p = tmp_path / "batch.xmind"
    _make_xmind(p, content)
    w = XMindWriter(parse_xmind(p))

    changes = w.batch_update_titles("章节", "章")
    assert len(changes) == 2
    assert w.xm.find_topic("root").title == "章1A"
    assert w.xm.find_topic("c1").title == "章1B"

    changes2 = w.batch_update_titles(r"\d+", "X", use_regex=True)
    assert len(changes2) == 2
    assert w.xm.find_topic("root").title == "章XA"
    assert w.xm.find_topic("c1").title == "章XB"

    deleted = w.delete_empty_nodes()
    assert set(deleted) == {"c2", "c3"}
    assert [n.id for n in w.xm.all_topics()] == ["root", "c1"]


def test_merge_duplicate_nodes_moves_children(tmp_path: Path):
    """重复兄弟节点合并后，子节点要并入保留节点。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "a1", "title": "重复", "children": {"attached": [{"id": "k1", "title": "孩子1"}]}},
        {"id": "a2", "title": "重复", "children": {"attached": [{"id": "k2", "title": "孩子2"}]}},
        {"id": "b", "title": "唯一"},
    ]}}}]
    p = tmp_path / "merge.xmind"
    _make_xmind(p, content)
    w = XMindWriter(parse_xmind(p))
    merged = w.merge_duplicate_nodes()
    assert merged == [("a1", "a2")]
    raw = w.xm.find_topic("a1")._raw
    children = [c["id"] for c in raw["children"]["attached"]]
    assert children == ["k1", "k2"]
    assert w.xm.find_topic("a2") is None


def test_notes_children_conversion(tmp_path: Path):
    """备注与子节点互转。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "c1", "title": "C1", "notes": {"plain": {"content": "第一行\n第二行\n\n第三行"}}},
    ]}}}]
    p = tmp_path / "conv.xmind"
    _make_xmind(p, content)
    w = XMindWriter(parse_xmind(p))

    assert w.notes_to_children("c1") == 3
    c1 = w.xm.find_topic("c1")
    assert [c.title for c in c1.children] == ["第一行", "第二行", "第三行"]
    assert "notes" not in c1._raw

    assert w.children_to_notes("c1") == 3
    c1 = w.xm.find_topic("c1")
    assert c1.notes == "第一行\n第二行\n第三行"
    assert not c1.children


def test_auto_number(tmp_path: Path):
    """自动编号：decimal 与 hierarchical。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "a", "title": "A", "children": {"attached": [
            {"id": "a1", "title": "A1"},
            {"id": "a2", "title": "A2"},
        ]}},
        {"id": "b", "title": "B"},
    ]}}}]
    p = tmp_path / "num.xmind"
    _make_xmind(p, content)

    w = XMindWriter(parse_xmind(p))
    assert w.auto_number("decimal") == 4
    assert w.xm.find_topic("a").title == "1. A"
    assert w.xm.find_topic("a1").title == "1. A1"
    assert w.xm.find_topic("a2").title == "2. A2"
    assert w.xm.find_topic("b").title == "2. B"

    w = XMindWriter(parse_xmind(p))
    assert w.auto_number("hierarchical") == 4
    assert w.xm.find_topic("a").title == "1. A"
    assert w.xm.find_topic("a1").title == "1.1. A1"
    assert w.xm.find_topic("a2").title == "1.2. A2"


def test_remove_unused_attachments(tmp_path: Path):
    """未引用的附件要在保存时移除，被引用的保留。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "img", "title": "有图", "image": {"src": "xap:resources/used.png"}},
    ]}}}]
    p = tmp_path / "att.xmind"
    _make_xmind(p, content, {
        "resources/used.png": base64.b64decode(PNG_B64),
        "resources/orphan.png": base64.b64decode(PNG_B64),
    })
    w = XMindWriter(parse_xmind(p))
    removed = w.remove_unused_attachments()
    assert removed == ["resources/orphan.png"]
    out = tmp_path / "out.xmind"
    w.save(out)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "resources/used.png" in names
        assert "resources/orphan.png" not in names


async def test_preview_modes_do_not_modify(config: Config, tmp_path: Path):
    """preview=true 时 apply_to_matching / apply_style_preset 不修改文件。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "c1", "title": "重点A"},
    ]}}}]
    p = tmp_path / "preview.xmind"
    _make_xmind(p, content)
    tools = XMindTools(config)

    result = await tools.call_tool("apply_to_matching", {
        "file_path": str(p),
        "keyword": "重点",
        "style": {"font_color": "#FF0000"},
        "preview": True,
    })
    assert "将修改 1 个节点" in result[0].text
    xm = parse_xmind(p)
    assert "style" not in xm.find_topic("c1")._raw

    result = await tools.call_tool("apply_style_preset", {
        "file_path": str(p),
        "preset": "清新蓝",
        "preview": True,
    })
    assert "将影响全部 2 个节点" in result[0].text
    xm = parse_xmind(p)
    assert "style" not in xm.find_topic("root")._raw


async def test_analyze_style_and_export_svg(config: Config, sample_xmind: Path):
    """analyze_style 输出主题/统计，export_svg 生成 SVG 文件。"""
    tools = XMindTools(config)
    result = await tools.call_tool("analyze_style", {"file_path": str(sample_xmind)})
    assert "样式审计" in result[0].text
    assert "节点总数" in result[0].text

    svg_path = sample_xmind.with_suffix(".svg")
    result = await tools.call_tool("export_svg", {"file_path": str(sample_xmind)})
    assert svg_path.exists()
    assert "<svg" in svg_path.read_text(encoding="utf-8")
    assert "根主题" in svg_path.read_text(encoding="utf-8")


async def test_list_attachments_tool(config: Config, tmp_path: Path):
    """list_attachments 区分被引用与未引用资源。"""
    content = [{"id": "s", "title": "S", "rootTopic": {"id": "root", "title": "R", "children": {"attached": [
        {"id": "img", "title": "有图", "image": {"src": "xap:resources/used.png"}},
    ]}}}]
    p = tmp_path / "att.xmind"
    _make_xmind(p, content, {
        "resources/used.png": base64.b64decode(PNG_B64),
        "resources/orphan.png": base64.b64decode(PNG_B64),
    })
    tools = XMindTools(config)
    result = await tools.call_tool("list_attachments", {"file_path": str(p)})
    text = result[0].text
    assert "被引用 (1)" in text
    assert "未被引用 (1)" in text
    assert "orphan.png" in text
