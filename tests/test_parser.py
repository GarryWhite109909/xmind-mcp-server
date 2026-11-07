"""测试 XMind 文件解析器。"""

import json
import zipfile
from pathlib import Path

import pytest

from xmind_mcp.xmind_parser import parse_xmind, load_topic_image


def test_parse_sample(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    assert len(xm.sheets) == 1
    assert xm.sheets[0].title == "测试工作表"
    root = xm.sheets[0].root
    assert root.title == "根主题"
    assert len(root.children) == 2


def test_find_topic(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    node = xm.find_topic("grand-1")
    assert node is not None
    assert node.title == "孙节点"
    assert node.level == 2


def test_find_topic_not_found(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    assert xm.find_topic("nonexistent") is None


def test_find_topics_with_image_no_images(sample_xmind: Path):
    xm = parse_xmind(sample_xmind)
    assert xm.find_topics_with_image() == []


def test_parse_markers_accepts_legacy_dict_shape(tmp_path: Path):
    """旧结构 markers={"markerId": "priority-1"} 也要归一化成列表。"""
    path = tmp_path / "markers.xmind"
    content = [{"id": "s", "title": "S", "rootTopic": {
        "id": "root", "title": "R",
        "markers": {"markerId": "priority-1"},
    }}]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))

    node = parse_xmind(path).sheets[0].root
    assert node.markers == ["priority-1"]


def test_old_xml_format_raises(tmp_path: Path):
    path = tmp_path / "old.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.xml", "<xml></xml>")

    with pytest.raises(ValueError, match="旧版 XMind 8"):
        parse_xmind(path)


def test_invalid_file_raises(tmp_path: Path):
    path = tmp_path / "not_xmind.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "hello")

    with pytest.raises(ValueError, match="不是有效的 XMind 文件"):
        parse_xmind(path)


def test_parse_real_xmind(real_xmind: Path):
    """解析仓库自带的测试文件。"""
    xm = parse_xmind(real_xmind)
    assert len(xm.sheets) >= 1
    root = xm.sheets[0].root
    assert root.title == "中心主题"
    # 文件中有一个带图片的节点
    image_nodes = xm.find_topics_with_image()
    assert len(image_nodes) == 1


def test_load_topic_image(real_xmind: Path):
    """能从真实文件中加载图片二进制。"""
    xm = parse_xmind(real_xmind)
    image_nodes = xm.find_topics_with_image()
    assert image_nodes
    image_data = load_topic_image(image_nodes[0], xm)
    assert image_data is not None
    assert len(image_data) > 0


def test_parse_important_topic_metadata(real_xmind: Path):
    """真实文件里“分支主题 1”是 importantTopic：圆形、正方舒体、12pt。"""
    xm = parse_xmind(real_xmind)
    node = xm.find_topic("3c7ba4dfdb0a36747e6d5aa2bf")
    assert node is not None
    assert node.xmind_class == "importantTopic"
    assert node.sheet_index == 0
    props = node.style.get("properties", {})
    assert props.get("shape-class") == "org.xmind.topicShape.circle"
    assert props.get("fo:font-family") == "FZShuTi"
    assert props.get("fo:font-size") == "12pt"
