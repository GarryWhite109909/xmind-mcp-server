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


async def test_save_as_then_save_original(config: Config, sample_xmind: Path, tmp_path: Path):
    """回归：另存为 B 后，再保存原文件必须写回原文件，而不是 B。"""
    tools = XMindTools(config)
    out = tmp_path / "b.xmind"

    await tools.call_tool("add_node", {
        "file_path": str(sample_xmind),
        "parent_id": "root-1",
        "title": "新节点A",
    })
    await tools.call_tool("save_file", {
        "file_path": str(sample_xmind),
        "output_path": str(out),
    })
    r = await tools.call_tool("save_file", {"file_path": str(sample_xmind)})
    assert "已保存" in r[0].text
    assert str(out) not in r[0].text

    read = await tools.call_tool("read_structure", {"file_path": str(sample_xmind)})
    assert "新节点A" in read[0].text

    read_b = await tools.call_tool("read_structure", {"file_path": str(out)})
    assert "新节点A" in read_b[0].text


async def test_read_all_preserves_notes_newlines(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    xm, writer = tools.workspace.get(str(sample_xmind))
    writer.update_node("child-1", notes="第一行\n第二行")

    result = await tools.call_tool("read_all", {"file_path": str(sample_xmind)})
    text = result[0].text
    assert "第一行" in text
    assert "第二行" in text


async def test_export_markdown_escapes_special_chars(config: Config, tmp_path: Path):
    import json
    import zipfile

    content = [{
        "id": "s1", "title": "工作表",
        "rootTopic": {"id": "root", "title": "根", "children": {"attached": [
            {"id": "n1", "title": "标题 # 和 *星号*"},
        ]}},
    }]
    path = tmp_path / "esc.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))

    tools = XMindTools(config)
    result = await tools.call_tool("export_markdown", {"file_path": str(path)})
    text = result[0].text
    assert "\\# 和 \\*星号\\*" in text


async def test_read_all_max_nodes(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("read_all", {
        "file_path": str(sample_xmind),
        "max_nodes": 1,
    })
    assert "已按 max_nodes 截断" in result[0].text


async def test_create_file_tool(config: Config, tmp_path: Path):
    tools = XMindTools(config)
    path = tmp_path / "created.xmind"
    result = await tools.call_tool("create_file", {
        "file_path": str(path),
        "root_title": "新建根",
        "topics": [{"title": "分支A", "children": [{"title": "A1"}]}],
    })
    assert "已创建" in result[0].text

    read = await tools.call_tool("read_structure", {"file_path": str(path)})
    assert "新建根" in read[0].text
    assert "分支A" in read[0].text
    assert "A1" in read[0].text


async def test_import_markdown_tool(config: Config, tmp_path: Path):
    tools = XMindTools(config)
    path = tmp_path / "imported.xmind"
    md = "# 根主题\n- 分支A\n  - 叶子A1\n- 分支B\n"
    result = await tools.call_tool("import_markdown", {
        "file_path": str(path),
        "markdown": md,
    })
    assert "已从 Markdown 创建" in result[0].text

    read = await tools.call_tool("read_structure", {"file_path": str(path)})
    text = read[0].text
    assert "根主题" in text
    assert "分支A" in text
    assert "叶子A1" in text
    assert "分支B" in text


async def test_repair_file_tool(config: Config, tmp_path: Path):
    """repair_file 能把缺 metadata.json 的文件补全并恢复读取。"""
    import json
    import zipfile

    broken = tmp_path / "broken.xmind"
    with zipfile.ZipFile(broken, "w") as zf:
        zf.writestr("content.json", json.dumps([{
            "id": "s1",
            "title": "旧画布",
            "rootTopic": {
                "id": "root",
                "title": "根主题",
                "children": {"attached": [{"id": "a1", "title": "分支A"}]},
            },
        }], ensure_ascii=False))

    tools = XMindTools(config)
    result = await tools.call_tool("repair_file", {"file_path": str(broken)})
    assert "已修复" in result[0].text

    with zipfile.ZipFile(broken) as zf:
        assert "metadata.json" in zf.namelist()
        assert "content.xml" in zf.namelist()

    read = await tools.call_tool("read_structure", {"file_path": str(broken)})
    assert "根主题" in read[0].text
    assert "分支A" in read[0].text


async def test_get_node_tool(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    xm, writer = tools.workspace.get(str(sample_xmind))
    writer.update_node("child-1", notes="备注内容")

    result = await tools.call_tool("get_node", {
        "file_path": str(sample_xmind),
        "node_id": "child-1",
    })
    text = result[0].text
    assert "子节点1" in text
    assert "父节点: root-1" in text
    assert "备注" in text


async def test_apply_to_matching_tool(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    await tools.call_tool("add_node", {
        "file_path": str(sample_xmind),
        "parent_id": "root-1",
        "title": "重点A",
    })
    await tools.call_tool("add_node", {
        "file_path": str(sample_xmind),
        "parent_id": "root-1",
        "title": "重点B",
    })
    result = await tools.call_tool("apply_to_matching", {
        "file_path": str(sample_xmind),
        "keyword": "重点",
        "style": {"font_color": "#FF0000"},
        "add_markers": ["priority-1"],
    })
    assert "2 个节点" in result[0].text

    detail = await tools.call_tool("get_node", {
        "file_path": str(sample_xmind),
        "node_id": "root-1",
    })
    # 通过读树确认两个重点节点都改到了
    read = await tools.call_tool("read_all", {
        "file_path": str(sample_xmind),
        "include_style": True,
    })
    assert "重点A" in read[0].text
    assert "重点B" in read[0].text
    assert "#FF0000" in read[0].text


async def test_analyze_file_tool(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("analyze_file", {"file_path": str(sample_xmind)})
    text = result[0].text
    assert "节点总数" in text
    assert "重复标题: 无" in text
    assert "最大深度" in text
    assert "概要: 0" in text
    assert "边界: 0" in text
    assert "关系线: 0" in text


async def test_set_sheet_layout_tool(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("set_sheet_layout", {
        "file_path": str(sample_xmind),
        "layout": "org.xmind.ui.logic.right",
        "auto_save": True,
    })
    assert "布局已更新" in result[0].text
    detail = await tools.call_tool("get_node", {
        "file_path": str(sample_xmind),
        "node_id": "root-1",
    })
    assert "org.xmind.ui.logic.right" in detail[0].text


async def test_add_image_tool(config: Config, sample_xmind: Path, tmp_path: Path):
    import base64

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+h"
        "HgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    )
    image_path = tmp_path / "pic.png"
    image_path.write_bytes(base64.b64decode(png_b64))

    tools = XMindTools(config)
    result = await tools.call_tool("add_image", {
        "file_path": str(sample_xmind),
        "node_id": "child-1",
        "image_path": str(image_path),
        "auto_save": True,
    })
    assert "已插入图片" in result[0].text

    listed = await tools.call_tool("list_images", {"file_path": str(sample_xmind)})
    assert "child-1" in listed[0].text


async def test_add_summary_tool(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("add_summary", {
        "file_path": str(sample_xmind),
        "parent_id": "root-1",
        "start_id": "child-1",
        "end_id": "child-2",
        "title": "分组",
        "auto_save": True,
    })
    assert "已添加概要" in result[0].text

    read = await tools.call_tool("read_structure", {"file_path": str(sample_xmind)})
    assert "分组" in read[0].text
    assert "summary" in read[0].text


async def test_add_relationship_tool(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("add_relationship", {
        "file_path": str(sample_xmind),
        "start_id": "child-1",
        "end_id": "grand-1",
        "title": "关联",
        "auto_save": True,
    })
    assert "已添加关系线" in result[0].text

    analyze = await tools.call_tool("analyze_file", {"file_path": str(sample_xmind)})
    assert "关系线: 1" in analyze[0].text


async def test_apply_style_preset_tool(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("apply_style_preset", {
        "file_path": str(sample_xmind),
        "preset": "清新蓝",
        "auto_save": True,
    })
    assert "4 个节点" in result[0].text

    read = await tools.call_tool("read_all", {
        "file_path": str(sample_xmind),
        "include_style": True,
    })
    assert "#E8F1FB" in read[0].text


async def test_get_node_shows_important_topic_effective_style(
    config: Config,
    real_xmind: Path,
):
    """模型必须能看到 importantTopic 节点继承了主题的紫色填充/加粗。"""
    tools = XMindTools(config)
    result = await tools.call_tool("get_node", {
        "file_path": str(real_xmind),
        "node_id": "3c7ba4dfdb0a36747e6d5aa2bf",
    })
    text = result[0].text
    assert "主题类型: importantTopic" in text
    assert "FZShuTi" in text
    assert "org.xmind.topicShape.circle" in text
    assert "12pt" in text
    # 主题继承的样式（节点自身没写，但实际显示为紫色加粗）
    assert "#7F00AC" in text


async def test_read_all_shows_important_topic_marker(
    config: Config,
    real_xmind: Path,
):
    tools = XMindTools(config)
    result = await tools.call_tool("read_all", {"file_path": str(real_xmind)})
    assert "importantTopic" in result[0].text
