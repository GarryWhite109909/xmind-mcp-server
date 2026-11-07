"""测试 MCP 工具层。"""

import asyncio
import base64
import json
import zipfile
from pathlib import Path

import pytest

from xmind_mcp.config import Config
from xmind_mcp.tool_schemas import TOOLSETS, build_tool_list
from xmind_mcp.tools import XMindTools
from xmind_mcp.xmind_parser import parse_xmind


@pytest.fixture
def config() -> Config:
    return Config()


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


async def test_list_attachments_recognizes_xap_images(config: Config, xmind_with_image: Path):
    """xap:images/test.png 对应 attachments/images/test.png，应标为已引用。"""
    tools = XMindTools(config)
    result = await tools.call_tool("list_attachments", {"file_path": str(xmind_with_image)})
    text = result[0].text
    assert "attachments/images/test.png" in text
    assert "未被引用 (0)" in text


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


async def test_import_markdown_keeps_all_content(config: Config, tmp_path: Path):
    """逐行原样转换：表格、代码块、引用都不能丢。"""
    tools = XMindTools(config)
    path = tmp_path / "verbatim.xmind"
    md = (
        "# 根主题\n"
        "## 小节\n"
        "- 列表项：**加粗内容**\n"
        "- | 列A | 列B |\n"
        "- | 1 | 2 |\n"
        "- > 引用文字：重点内容\n"
        "- 代码示例：\n"
        "  ```python\n"
        "  print('hello')\n"
        "  ```\n"
    )
    result = await tools.call_tool("import_markdown", {
        "file_path": str(path),
        "markdown": md,
    })
    assert "已从 Markdown 创建" in result[0].text

    read = await tools.call_tool("read_all", {"file_path": str(path)})
    text = read[0].text
    for expected in (
        "加粗内容",
        "列A",
        "引用文字：重点内容",
        "print('hello')",
    ):
        assert expected in text, f"丢失内容: {expected}"

    xm = parse_xmind(path)
    titles = [n.title for n in xm.all_topics()]
    assert "print('hello')" in titles


async def test_import_markdown_file_tool(config: Config, tmp_path: Path):
    """import_markdown_file 直接读 .md 文件，逐行原样转换。"""
    tools = XMindTools(config)
    md_path = tmp_path / "笔记.md"
    md_path.write_text(
        "# 第一章 测试\n"
        "## 1.1 概述\n"
        "- 定义：原样保留的句子\n"
        "- 代码示例：\n"
        "  ```python\n"
        "  int x = 1;\n"
        "  ```\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "笔记.xmind"
    result = await tools.call_tool("import_markdown_file", {
        "file_path": str(md_path),
        "output_path": str(out_path),
    })
    assert "已原样转换 Markdown" in result[0].text
    assert out_path.exists()

    xm = parse_xmind(out_path)
    titles = [n.title for n in xm.all_topics()]
    assert "第一章 测试" in titles
    assert "定义：原样保留的句子" in titles
    assert "int x = 1;" in titles


async def test_import_markdown_file_handles_bom(config: Config, tmp_path: Path):
    """带 UTF-8 BOM 的 Markdown 也要能识别第一个标题。"""
    tools = XMindTools(config)
    md_path = tmp_path / "bom.md"
    md_path.write_text("# BOM 标题\n- 子项\n", encoding="utf-8-sig")
    out_path = tmp_path / "bom.xmind"

    result = await tools.call_tool("import_markdown_file", {
        "file_path": str(md_path),
        "output_path": str(out_path),
    })
    assert "已原样转换 Markdown" in result[0].text
    xm = parse_xmind(out_path)
    assert xm.sheets[0].root.title == "BOM 标题"


async def test_import_markdown_renders_block_equations(config: Config, tmp_path: Path):
    """块级 $$...$$ 公式应被渲染成图片插入节点，行内 $...$ 保持文本。"""
    tools = XMindTools(config)
    path = tmp_path / "equations.xmind"
    md = (
        "# 高数\n"
        "$$\n"
        "E = mc^2\n"
        "$$\n"
        "$x^2$ 是行内公式\n"
    )
    result = await tools.call_tool("import_markdown", {
        "file_path": str(path),
        "markdown": md,
    })
    assert "公式: 1 个" in result[0].text

    # 磁盘 zip 里应有渲染出的图片资源
    import zipfile
    with zipfile.ZipFile(path) as zf:
        res = [n for n in zf.namelist() if n.startswith("resources/") and n.endswith(".png")]
        assert len(res) == 1
        assert zf.read(res[0])  # 非空

        content = json.loads(zf.read("content.json"))
    root = content[0]["rootTopic"]
    attached = root["children"]["attached"]
    # 第一个子节点是公式（图片，标题被清空），第二个是行内公式文本
    assert "image" in attached[0]
    assert attached[0]["image"]["src"].startswith("xap:resources/")
    assert attached[1]["title"] == "$x^2$ 是行内公式"


async def test_import_markdown_keeps_unrenderable_formula_text(config: Config, tmp_path: Path):
    """mathtext 渲染不了的公式应保留原始 LaTeX 文本，不导致导入失败。"""
    tools = XMindTools(config)
    path = tmp_path / "bad_equation.xmind"
    # \begin{aligned} 多行环境是 mathtext 不支持的命令，应原样保留
    md = "# 根\n$$\n\\begin{aligned} a &= b \\end{aligned}\n$$\n"
    result = await tools.call_tool("import_markdown", {
        "file_path": str(path),
        "markdown": md,
    })
    assert "已从 Markdown 创建" in result[0].text
    xm = parse_xmind(path)
    titles = [n.title for n in xm.all_topics()]
    assert any("aligned" in (t or "") for t in titles)


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

    await tools.call_tool("get_node", {
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


async def test_set_node_style_tool_chinese(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("set_node_style", {
        "file_path": str(sample_xmind),
        "node_id": "child-1",
        "importance": "极其重要",
        "shape": "菱形",
        "border_width": "极粗",
        "font_transform": "全大写",
        "font_decoration": "删除线",
        "background_color": "4E0D58 40%",
        "auto_save": True,
    })
    assert "样式已更新" in result[0].text

    xm, _ = tools.workspace.get(str(sample_xmind))
    raw = xm.find_topic("child-1")._raw
    assert raw["class"] == "importantTopic"
    props = raw["style"]["properties"]
    assert props["shape-class"] == "org.xmind.topicShape.diamond"
    assert props["border-line-width"] == "8pt"
    assert props["fo:text-transform"] == "uppercase"
    assert props["fo:text-decoration"] == "line-through"
    assert props["svg:fill"] == "#4E0D5866"


async def test_set_branch_style_tool(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("set_branch_style", {
        "file_path": str(sample_xmind),
        "node_id": "child-1",
        "line_type": "圆弧",
        "arrow_end": "圆点",
        "line_width": "细",
    })
    assert "分支样式已更新" in result[0].text

    xm, _ = tools.workspace.get(str(sample_xmind))
    props = xm.find_topic("child-1").style["properties"]
    assert props["line-class"] == "org.xmind.branchConnection.bight"
    assert props["arrow-end-class"] == "org.xmind.arrowShape.dot"
    assert props["line-width"] == "2pt"


async def test_set_theme_style_tool(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    result = await tools.call_tool("set_theme_style", {
        "file_path": str(sample_xmind),
        "level": "中心主题",
        "font_size": 30,
        "shape": "圆形",
        "background_color": "#000000",
    })
    assert "默认样式已更新" in result[0].text

    xm, _ = tools.workspace.get(str(sample_xmind))
    props = xm.raw_content[0]["theme"]["centralTopic"]["properties"]
    assert props["fo:font-size"] == "30pt"
    assert props["shape-class"] == "org.xmind.topicShape.circle"
    assert props["svg:fill"] == "#000000"


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


def test_tool_annotations_mark_read_only_and_destructive():
    """客户端靠 annotations 决定自动批准还是弹确认。"""
    tools = {t.name: t for t in build_tool_list()}

    assert tools["read_all"].annotations.read_only_hint is True
    assert tools["get_image"].annotations.read_only_hint is True
    assert tools["delete_node"].annotations.read_only_hint is False
    assert tools["delete_node"].annotations.destructive_hint is True
    assert tools["set_node_style"].annotations.destructive_hint is False
    assert tools["set_node_style"].annotations.open_world_hint is False


def test_every_listed_tool_has_handler():
    """工具列表和 handler 注册表必须一一对应，避免改名后调用落空。"""
    listed = {t.name for t in build_tool_list()}
    assert listed == set(XMindTools._handlers)


def test_read_only_flags_match_read_tools():
    """read 组里的工具必须都标记为只读，只读集合里也不能有列表外的工具。"""
    read_tools = build_tool_list("read")
    assert all(t.annotations.read_only_hint for t in read_tools)
    assert {t.name for t in read_tools} <= {t.name for t in build_tool_list()}


def test_toolsets_cover_every_tool_exactly_once():
    """分组必须覆盖全部工具，且一个工具只属于一组。"""
    all_names = {t.name for t in build_tool_list()}
    grouped = [name for group in TOOLSETS.values() for name in group]
    assert len(grouped) == len(set(grouped)), "同一个工具出现在多个分组里"
    assert set(grouped) == all_names


def test_build_tool_list_filters_by_toolset():
    read_only = build_tool_list("read")
    assert {t.name for t in read_only} == TOOLSETS["read"]
    assert all(t.annotations.read_only_hint for t in read_only)

    combined = build_tool_list("read,export")
    assert len(combined) == len(TOOLSETS["read"]) + len(TOOLSETS["export"])

    # 写错组名时给全量工具，而不是一个都没有
    assert len(build_tool_list("不存在的组")) == 55
    assert len(build_tool_list("all")) == 55


def test_legacy_config_fields_are_ignored(tmp_path: Path, monkeypatch):
    """旧版配置里的 ocr_engine/device 不应阻止加载，ocr 模式归一化为 inline。"""
    from xmind_mcp import config as config_module

    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({
            "ocr_engine": "rapidocr",
            "device": "cuda",
            "initialized": True,
            "image_mode": "ocr",
            "toolset": "read",
            "image_max_side": "not-a-number",
            "allowed_roots": "D:\\docs",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)

    config = Config.load()
    assert config.image_mode == "inline"
    assert config.toolset == "read"
    assert config.image_max_side == 1024
    assert config.allowed_roots == ["D:\\docs"]
    assert not hasattr(config, "ocr_engine")


async def test_concurrent_writes_are_serialized(config: Config, sample_xmind: Path):
    """并发写同一个文件时串行执行，两个修改都不能丢。"""
    tools = XMindTools(config)
    await asyncio.gather(
        tools.call_tool("add_node", {
            "file_path": str(sample_xmind), "parent_id": "root-1", "title": "并发A",
        }),
        tools.call_tool("add_node", {
            "file_path": str(sample_xmind), "parent_id": "root-1", "title": "并发B",
        }),
    )
    result = await tools.call_tool("read_structure", {"file_path": str(sample_xmind)})
    assert "并发A" in result[0].text
    assert "并发B" in result[0].text


async def test_restore_backup_only_accepts_listed_backups(
    config: Config, sample_xmind: Path, tmp_path: Path
):
    from xmind_mcp.xmind_writer import list_backups

    tools = XMindTools(config)
    await tools.call_tool("save_file", {"file_path": str(sample_xmind)})
    backups = list_backups(str(sample_xmind))
    assert backups

    ok = await tools.call_tool(
        "restore_backup",
        {"file_path": str(sample_xmind), "backup_path": backups[0]["path"]},
    )
    assert "已从备份恢复" in ok[0].text

    evil = tmp_path / "evil.xmind"
    evil.write_bytes(b"not a backup")
    blocks, _err = await tools.call_tool_status(
        "restore_backup",
        {"file_path": str(sample_xmind), "backup_path": str(evil)},
    )
    assert "不是该文件的自动备份" in blocks[0].text


async def test_allowed_roots_blocks_outside_paths(tmp_path: Path, sample_xmind: Path):
    """配置了 allowed_roots 后，目录内的文件可用，目录外直接拒绝。"""
    config = Config(allowed_roots=[str(sample_xmind.parent)])
    tools = XMindTools(config)

    _, err = await tools.call_tool_status(
        "read_structure", {"file_path": str(sample_xmind)}
    )
    assert err is False

    outside = tmp_path.parent / "definitely_outside.xmind"
    blocks, err = await tools.call_tool_status(
        "read_structure", {"file_path": str(outside)}
    )
    assert err is True
    assert "不在允许范围" in blocks[0].text


async def test_allowed_roots_empty_means_unrestricted(sample_xmind: Path):
    """默认不配置 allowed_roots 时保持旧行为。"""
    tools = XMindTools(Config())
    _, err = await tools.call_tool_status(
        "read_structure", {"file_path": str(sample_xmind)}
    )
    assert err is False


async def test_save_file_reports_conflict(config: Config, sample_xmind: Path):
    tools = XMindTools(config)
    _, writer = tools.workspace.get(str(sample_xmind))
    writer.update_node("child-1", title="内存里的修改")
    sample_xmind.write_bytes(b"another process wrote this")

    blocks, _ = await tools.call_tool_status(
        "save_file", {"file_path": str(sample_xmind)}
    )
    assert "被其他程序修改" in blocks[0].text
    assert "另存" in blocks[0].text
    assert sample_xmind.read_bytes() == b"another process wrote this"
