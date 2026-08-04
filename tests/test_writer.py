"""测试 XMind 文件写入器。"""

import json
from pathlib import Path

import pytest

from xmind_mcp.xmind_parser import parse_xmind, load_topic_image
from xmind_mcp.xmind_writer import XMindWriter


def test_add_node_to_root(sample_xmind: Path):
    """关键回归测试：必须能给根主题添加子节点。"""
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    root = xm.sheets[0].root

    new_node = writer.add_node(parent_id=root.id, title="新增根子节点")
    assert new_node.title == "新增根子节点"
    assert new_node.parent_id == root.id
    assert new_node.level == 1

    # 内存树正确
    assert len(xm.sheets[0].root.children) == 3


def test_add_node_to_child(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)

    new_node = writer.add_node(parent_id="child-1", title="child-1 的子节点")
    assert new_node.parent_id == "child-1"
    assert new_node.level == 2

    # 必须挂在 child-1 下，而不是根下
    child1 = xm.find_topic("child-1")
    assert child1 is not None
    assert any(n.id == new_node.id for n in child1.children)
    assert not any(n.id == new_node.id for n in xm.sheets[0].root.children)


def test_update_node(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)

    ok = writer.update_node("child-1", title="已更新", notes="备注内容")
    assert ok

    node = xm.find_topic("child-1")
    assert node is not None
    assert node.title == "已更新"
    assert node.notes == "备注内容"
    # 新版 XMind 使用 plain.content 存备注
    assert xm.raw_content[0]["rootTopic"]["children"]["attached"][0]["notes"] == {
        "plain": {"content": "备注内容"}
    }


def test_update_node_preserves_existing_notes_format(sample_xmind: Path):
    """已有 text 格式备注的节点，更新时保持 text 格式。"""
    xm = parse_xmind(sample_xmind)
    topic = xm.raw_content[0]["rootTopic"]["children"]["attached"][0]
    topic["notes"] = {"text": "旧备注"}
    writer = XMindWriter(xm)
    writer.update_node("child-1", notes="新备注")
    assert topic["notes"] == {"text": "新备注"}


def test_update_node_not_found(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    assert writer.update_node("nonexistent", title="x") is False


def test_delete_node(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)

    ok = writer.delete_node("child-2")
    assert ok
    assert xm.find_topic("child-2") is None
    assert xm.find_topic("grand-1") is None  # 子节点一起删除


def test_delete_root_raises(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    with pytest.raises(ValueError, match="不能删除根主题"):
        writer.delete_node("root-1")


def test_move_node(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)

    ok = writer.move_node("grand-1", "child-1")
    assert ok

    child1 = xm.find_topic("child-1")
    child2 = xm.find_topic("child-2")
    grand1 = xm.find_topic("grand-1")
    assert child1 is not None and child2 is not None and grand1 is not None
    assert grand1.parent_id == "child-1"
    assert any(n.id == "grand-1" for n in child1.children)
    assert not any(n.id == "grand-1" for n in child2.children)


def test_move_node_with_position(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    writer.add_node(parent_id="root-1", title="新节点", after_id="child-1")

    titles = [n.title for n in xm.sheets[0].root.children]
    assert titles == ["子节点1", "新节点", "子节点2"]


def test_move_node_to_descendant_raises(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    with pytest.raises(ValueError, match="不能.*移动到自己或自己的后代"):
        writer.move_node("child-2", "grand-1")


def test_save_and_reload(sample_xmind: Path, tmp_path: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    new_node = writer.add_node(parent_id="root-1", title="保存测试节点")

    out = tmp_path / "out.xmind"
    saved = writer.save(out)
    assert saved == str(out)

    xm2 = parse_xmind(out)
    node = xm2.find_topic(new_node.id)
    assert node is not None
    assert node.title == "保存测试节点"
    assert node.parent_id == "root-1"


def test_add_nodes(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    nodes = writer.add_nodes("root-1", ["批量A", "批量B", "批量C"])
    assert len(nodes) == 3
    assert {n.title for n in nodes} == {"批量A", "批量B", "批量C"}
    assert all(n.parent_id == "root-1" for n in nodes)
    assert len(xm.sheets[0].root.children) == 5  # 原有2 + 新增3


def test_duplicate_node(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    new_node = writer.duplicate_node("child-2", "root-1")
    assert new_node is not None
    assert new_node.id != "child-2"
    assert new_node.title == "子节点2"
    # 子树被复制
    assert any(n.title == "孙节点" for n in new_node.children)
    # 原节点仍在
    assert xm.find_topic("child-2") is not None


def test_duplicate_node_to_descendant_raises(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    with pytest.raises(ValueError, match="不能把节点复制到自己或自己的后代下"):
        writer.duplicate_node("child-2", "grand-1")


def test_duplicate_node_not_found(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    assert writer.duplicate_node("nonexistent", "root-1") is None


def test_set_node_style(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    ok = writer.set_node_style("child-1", font_color="#FF0000", font_size="16pt", font_weight="bold")
    assert ok
    node = xm.find_topic("child-1")
    assert node is not None
    props = node.style.get("properties", {})
    assert props["fo:color"] == "#FF0000"
    assert props["fo:font-size"] == "16pt"
    assert props["fo:font-weight"] == "bold"


def test_set_node_style_uses_xmind_fields_for_fill_and_border(sample_xmind: Path):
    """背景/边框必须写 XMind 认识的字段，否则美化无效。"""
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    writer.set_node_style(
        "child-1",
        background_color="#FFF3BF",
        border_color="#000000",
        border_width="2pt",
        shape="org.xmind.topicShape.ellipse",
        line_color="#FF6B6B",
        line_width="3pt",
    )
    node = xm.find_topic("child-1")
    props = node.style.get("properties", {})
    assert props["svg:fill"] == "#FFF3BF"
    assert props["border-line-color"] == "#000000"
    assert props["border-line-width"] == "2pt"
    assert props["shape-class"] == "org.xmind.topicShape.ellipse"
    assert props["line-color"] == "#FF6B6B"
    assert props["line-width"] == "3pt"
    assert "fo:background-color" not in props
    assert "fo:border-color" not in props


def test_set_sheet_layout(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    ok = writer.set_sheet_layout(0, "org.xmind.ui.logic.right")
    assert ok
    assert xm.sheets[0].root.structure_class == "org.xmind.ui.logic.right"
    assert xm.raw_content[0]["rootTopic"]["structureClass"] == "org.xmind.ui.logic.right"


def test_add_image(sample_xmind: Path, tmp_path: Path):
    import base64
    import zipfile

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+h"
        "HgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    )
    image_path = tmp_path / "pic.png"
    image_path.write_bytes(base64.b64decode(png_b64))

    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    ok, src = writer.add_image("child-1", image_path)
    assert ok
    assert src.startswith("xap:resources/")

    out = tmp_path / "with_image.xmind"
    writer.save(out)

    xm2 = parse_xmind(out)
    node = xm2.find_topic("child-1")
    assert node.image_src == src
    data = load_topic_image(node, xm2)
    assert data == image_path.read_bytes()

    # manifest 里要登记新增资源
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert src.replace("xap:", "") in manifest["file-entries"]


def test_set_hyperlink(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    ok = writer.set_hyperlink("child-1", "https://example.com", "示例")
    assert ok
    node = xm.find_topic("child-1")
    assert node.hyperlink == "https://example.com"

    # 移除超链接
    writer.set_hyperlink("child-1", "")
    assert xm.find_topic("child-1").hyperlink == ""


def test_add_remove_marker(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    assert writer.add_marker("child-1", "priority-1")
    assert writer.add_marker("child-1", "task-done")
    node = xm.find_topic("child-1")
    assert "priority-1" in node.markers
    assert "task-done" in node.markers

    assert writer.remove_marker("child-1", "priority-1")
    assert "priority-1" not in xm.find_topic("child-1").markers


def test_add_remove_label(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    assert writer.add_label("child-1", "重点")
    assert writer.add_label("child-1", "复习")
    node = xm.find_topic("child-1")
    assert "重点" in node.labels and "复习" in node.labels

    assert writer.remove_label("child-1", "重点")
    assert "重点" not in xm.find_topic("child-1").labels


def test_delete_node_cleans_relationships(tmp_path: Path):
    """删除节点时清理 sheet 级 relationships 和 summaries 引用。"""
    import json
    import zipfile

    content = [{
        "id": "sheet-1",
        "title": "测试",
        "relationships": [
            {"id": "rel-1", "end1Id": "child-1", "end2Id": "child-2"},
            {"id": "rel-2", "end1Id": "child-2", "end2Id": "grand-1"},
            {"id": "rel-3", "end1Id": "root-1", "end2Id": "child-2"},
        ],
        "rootTopic": {
            "id": "root-1",
            "title": "根",
            "children": {
                "attached": [
                    {"id": "child-1", "title": "A", "children": {
                        "attached": [{"id": "grand-1", "title": "G"}],
                    }},
                    {"id": "child-2", "title": "B", "summaries": [
                        {"id": "sum-1", "topicId": "grand-1",
                         "range": {"startId": "child-1", "endId": "grand-1"}},
                    ]},
                ],
            },
        },
    }]
    path = tmp_path / "rel.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))

    xm = parse_xmind(path)
    writer = XMindWriter(xm)
    writer.delete_node("child-1")
    sheet = xm.raw_content[0]
    assert sheet.get("relationships") is None or all(
        "child-1" not in (r.get("end1Id"), r.get("end2Id"))
        for r in sheet["relationships"]
    )
    raw_child2 = xm.find_topic("child-2")._raw
    assert raw_child2.get("summaries") is None


def test_topic_class_parsed(tmp_path: Path):
    import json
    import zipfile

    content = [{
        "id": "s1",
        "title": "S",
        "rootTopic": {
            "id": "root",
            "title": "根",
            "children": {
                "attached": [{"id": "a1", "title": "普通"}],
                "detached": [{"id": "d1", "title": "浮动"}],
                "summary": [{"id": "sm1", "title": "概要"}],
                "callout": [{"id": "co1", "title": "批注"}],
            },
        },
    }]
    path = tmp_path / "classes.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.json", json.dumps(content))
    xm = parse_xmind(path)
    assert xm.find_topic("root").topic_class == "root"
    assert xm.find_topic("a1").topic_class == "attached"
    assert xm.find_topic("d1").topic_class == "detached"
    assert xm.find_topic("sm1").topic_class == "summary"
    assert xm.find_topic("co1").topic_class == "callout"


def test_create_file_and_save(tmp_path: Path):
    path = tmp_path / "new.xmind"
    writer = XMindWriter.create(
        str(path),
        "根主题",
        sheet_title="新画布",
        topics=[
            {"title": "分支A", "notes": "备注A", "children": [
                {"title": "叶子A1"},
            ]},
            {"title": "分支B"},
        ],
    )
    assert path.exists()
    nodes = writer.xm.all_topics()
    assert len(nodes) == 4
    root = writer.xm.sheets[0].root
    assert root.title == "根主题"
    assert root.topic_class == "root"
    assert [c.title for c in root.children] == ["分支A", "分支B"]
    assert root.children[0].children[0].title == "叶子A1"
    assert root.children[0].notes == "备注A"

    # 重新从磁盘解析
    xm2 = parse_xmind(path)
    assert xm2.sheets[0].root.title == "根主题"
    assert len(xm2.all_topics()) == 4


def test_apply_matching(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    writer.add_node(parent_id="root-1", title="重点A")
    writer.add_node(parent_id="root-1", title="重点B")

    matched = writer.apply_matching(
        "重点",
        style={"font_color": "#FF0000", "font_weight": "bold"},
        add_markers=["priority-1"],
        add_labels=["重点"],
    )
    assert len(matched) == 2
    for node_id in matched:
        node = xm.find_topic(node_id)
        props = node.style.get("properties", {})
        assert props["fo:color"] == "#FF0000"
        assert props["fo:font-weight"] == "bold"
        assert "priority-1" in node.markers
        assert "重点" in node.labels


def test_add_summary_and_reload(sample_xmind: Path, tmp_path: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    node = writer.add_summary("root-1", "child-1", "child-2", "分组")
    assert node.topic_class == "summary"

    raw_root = xm.raw_content[0]["rootTopic"]
    assert raw_root["summaries"][0]["range"] == "(0,1)"
    assert raw_root["summaries"][0]["topicId"] == node.id
    assert any(c.get("id") == node.id for c in raw_root["children"]["summary"])

    out = tmp_path / "sum.xmind"
    writer.save(out)
    xm2 = parse_xmind(out)
    assert xm2.find_topic(node.id).topic_class == "summary"


def test_delete_child_cleans_summary_range(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    writer.add_summary("root-1", "child-1", "child-2", "分组")
    writer.delete_node("child-1")

    raw_root = xm.raw_content[0]["rootTopic"]
    assert not raw_root.get("summaries")
    assert not raw_root.get("children", {}).get("summary")


def test_add_boundary_and_remove(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    ok = writer.add_boundary("root-1", "child-1", "child-2", "边界")
    assert ok

    raw_root = xm.raw_content[0]["rootTopic"]
    boundaries = raw_root["boundaries"]
    assert boundaries[0]["range"] == "(0,1)"
    assert boundaries[0]["title"] == "边界"
    boundary_id = boundaries[0]["id"]

    assert writer.remove_boundary(boundary_id)
    assert not raw_root.get("boundaries")


def test_add_relationship_and_remove(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    ok = writer.add_relationship(0, "child-1", "grand-1", "关联")
    assert ok

    relationships = xm.raw_content[0]["relationships"]
    assert relationships[0]["end1Id"] == "child-1"
    assert relationships[0]["end2Id"] == "grand-1"
    relationship_id = relationships[0]["id"]

    assert writer.remove_relationship(relationship_id)
    assert not xm.raw_content[0].get("relationships")


def test_add_relationship_invalid_endpoint_raises(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    with pytest.raises(ValueError, match="必须存在于文件中"):
        writer.add_relationship(0, "child-1", "nonexistent")


def test_apply_style_preset(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    count = writer.apply_style_preset("清新蓝")
    assert count == 4  # 根 + child-1 + child-2 + grand-1

    root = xm.find_topic("root-1")
    assert root.style["properties"]["svg:fill"] == "#E8F1FB"
    assert root.style["properties"]["fo:color"] == "#1F4E79"
    child = xm.find_topic("child-1")
    assert child.style["properties"]["svg:fill"] == "#D6E6F5"
    grand = xm.find_topic("grand-1")
    assert grand.style["properties"]["svg:fill"] == "#FFFFFF"


def test_apply_style_preset_unknown_raises(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    writer = XMindWriter(xm)
    with pytest.raises(ValueError, match="未知样式预设"):
        writer.apply_style_preset("不存在的预设")
