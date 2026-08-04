"""生成《属性验证.xmind》样例：覆盖重要程度/形状/填充/边框/线宽/字号/文字效果/分支样式/主题级样式。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from xmind_mcp.xmind_writer import XMindWriter  # noqa: E402


def main() -> None:
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "examples",
        "属性验证.xmind",
    )

    writer = XMindWriter.create(
        path,
        "属性验证",
        sheet_title="属性验证",
        topics=[
            {
                "title": "极其重要·圆形·粗边框",
                "children": [{"title": "跑道·24号·首字母大写"}, {"title": "切角矩形·斜体"}],
            },
            {"title": "重要·菱形·点线边框", "children": [{"title": "六边形·36号"}]},
            {"title": "删去·下划线·删除线", "children": [{"title": "竖胶囊·48号"}]},
            {"title": "默认·双下划线·右对齐", "children": [{"title": "细分支·极细线"}]},
        ],
    )

    root = writer.xm.sheets[0].root
    kids = root.children

    # 主题级样式
    writer.set_theme_style(
        0, "中心主题",
        font_size=24, font_weight="加粗", background_color="4E0D58 15%",
        border_color="#4E0D58", border_width="粗", shape="圆角矩形",
    )
    writer.set_theme_style(0, "分支主题", font_size=18, shape="圆角矩形")
    writer.set_theme_style(0, "第三层级主题", font_size=14)

    # 分支节点
    writer.set_node_style(
        kids[0].id, importance="极其重要", shape="圆形",
        background_color="4E0D58 25%", border_color="#4E0D58",
        border_width="极粗", font_color="#FFFFFF", font_size=18,
    )
    writer.set_branch_style(kids[0].id, line_type="曲线", line_width="中等", line_color="#4E0D58")

    writer.set_node_style(
        kids[1].id, importance="重要", shape="菱形",
        background_color="4E0D58 40%", border_pattern="点线", border_width="细",
        text_align="居中",
    )

    writer.set_node_style(
        kids[2].id, importance="删去", shape="下划线",
        font_decoration="删除线", font_transform="全大写", background_color="#888888",
    )

    writer.set_node_style(
        kids[3].id, shape="双下划线", font_weight="加粗",
        text_align="右对齐", border_width="中等", border_pattern="虚线",
    )

    # 孙节点
    a1, a2 = kids[0].children[0], kids[0].children[1]
    writer.set_node_style(
        a1.id, shape="跑道", font_size=24,
        font_transform="首字母大写", font_decoration="下划线",
    )
    writer.set_node_style(
        a2.id, shape="切角矩形", font_size=12, font_style="斜体",
        background_color="4E0D58 60%",
    )

    b1 = kids[1].children[0]
    writer.set_node_style(b1.id, shape="六边形", font_size=36)
    writer.set_branch_style(b1.id, line_type="圆角折线", arrow_end="双箭头", line_width="极粗")

    c1 = kids[2].children[0]
    writer.set_node_style(c1.id, shape="竖胶囊", font_size=48)

    d1 = kids[3].children[0]
    writer.set_node_style(d1.id, shape="椭圆", border_width="极细", font_size=8)

    writer.save(path)
    print("created:", os.path.abspath(path), os.path.getsize(path))
    print("nodes:", len(writer.xm.all_topics()))


if __name__ == "__main__":
    main()
