"""测试 XMind 文件写入器。"""

from pathlib import Path

import pytest

from xmind_mcp.xmind_parser import parse_xmind
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
