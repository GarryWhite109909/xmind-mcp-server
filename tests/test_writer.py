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
