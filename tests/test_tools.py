"""测试 MCP 工具层。"""

import base64
import json
import zipfile
from pathlib import Path

import pytest

from xmind_mcp.config import Config
from xmind_mcp.tools import XMindTools, format_topic_tree


@pytest.fixture
def config() -> Config:
    return Config(
        ocr_engine="rapidocr",
        device="cpu",
        initialized=True,
    )


@pytest.fixture
def xmind_with_image(tmp_path: Path) -> Path:
    """创建一个带图片节点的测试 XMind 文件。"""
    # 1x1 像素透明 PNG
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    png_bytes = base64.b64decode(png_b64)

    content = [{
        "id": "sheet-1",
        "title": "图测试",
        "rootTopic": {
            "id": "root-1",
            "title": "根主题",
            "children": {
                "attached": [
                    {
                        "id": "img-node",
                        "title": "图片节点",
                        "image": {"src": "xap:images/test.png"},
                    },
                ]
            },
        },
    }]

    path = tmp_path / "image.xmind"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))
        zf.writestr("attachments/images/test.png", png_bytes)
    return path


async def test_read_structure(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("read_structure", {"file_path": str(sample_xmind)})
    assert len(result) == 1
    assert "根主题" in result[0].text
    assert "子节点1" in result[0].text
    # 默认输出 ID
    assert "[id: root-1]" in result[0].text
    assert "[id: child-1]" in result[0].text


async def test_read_structure_clean(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("read_structure", {
        "file_path": str(sample_xmind),
        "clean": True,
        "include_ids": False,
    })
    text = result[0].text
    assert "根主题" in text
    assert "[id:" not in text
    assert "🔖" not in text


async def test_read_structure_newline_title(config: Config, sample_xmind: Path):
    """标题中的换行应被清理为单个空格。"""
    tools = XMindTools(config)
    # 手动构造一个带换行的节点
    xm, writer = tools.workspace.get(str(sample_xmind))
    writer.add_node(parent_id="root-1", title="第一行\n第二行")
    result = await tools.call_tool("read_structure", {"file_path": str(sample_xmind)})
    assert "第一行 第二行" in result[0].text


async def test_list_images(config: Config, xmind_with_image: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("list_images", {"file_path": str(xmind_with_image)})
    assert "图片节点" in result[0].text
    assert "img-node" in result[0].text


async def test_read_image_ocr(config: Config, xmind_with_image: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("read_image_ocr", {
        "file_path": str(xmind_with_image),
        "node_id": "img-node",
    })
    assert "OCR" in result[0].text


async def test_find_node(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("find_node", {
        "file_path": str(sample_xmind),
        "keyword": "子节点1",
    })
    text = result[0].text
    assert "child-1" in text
    assert "L1" in text
    assert "根主题 / 子节点1" in text


async def test_find_node_not_found(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("find_node", {
        "file_path": str(sample_xmind),
        "keyword": "不存在",
    })
    assert "未找到" in result[0].text


async def test_add_and_save(config: Config, sample_xmind: Path, tmp_path: Path):
    tools = XMindTools(config)
    add_result = await tools.call_tool("add_node", {
        "file_path": str(sample_xmind),
        "parent_id": "root-1",
        "title": "工具新增节点",
    })
    assert "已添加节点" in add_result[0].text

    out = tmp_path / "saved.xmind"
    save_result = await tools.call_tool("save_file", {
        "file_path": str(sample_xmind),
        "output_path": str(out),
    })
    assert "已保存" in save_result[0].text

    # 重新读取验证
    read_result = await tools.call_tool("read_structure", {"file_path": str(out)})
    assert "工具新增节点" in read_result[0].text


async def test_add_node_auto_save(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("add_node", {
        "file_path": str(sample_xmind),
        "parent_id": "root-1",
        "title": "自动保存节点",
        "auto_save": True,
    })
    assert "已自动保存" in result[0].text

    # 重新读取原文件验证
    read_result = await tools.call_tool("read_structure", {"file_path": str(sample_xmind)})
    assert "自动保存节点" in read_result[0].text


async def test_export_markdown(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("export_markdown", {
        "file_path": str(sample_xmind),
        "include_ids": True,
    })
    text = result[0].text
    assert "# 测试工作表" in text
    assert "- 根主题" in text
    assert "- 子节点1" in text
    assert "`id:root-1`" in text


async def test_file_not_found(config: Config):
    tools = XMindTools(config)
    result = await tools.call_tool("read_structure", {"file_path": "不存在的文件.xmind"})
    assert "文件不存在" in result[0].text


async def test_get_hardware_info(config: Config):
    tools = XMindTools(config)
    result = await tools.call_tool("get_hardware_info", {})
    assert "OCR 引擎" in result[0].text
