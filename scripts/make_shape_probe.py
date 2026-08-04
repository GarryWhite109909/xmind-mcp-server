"""生成《形状对照.xmind》：把疑似“切角矩形/竖胶囊”的候选形状都摆出来，供在 XMind 里对照确认。"""

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
        "形状对照.xmind",
    )
    writer = XMindWriter.create(
        path,
        "形状对照",
        sheet_title="形状对照",
        topics=[
            {"title": "A: cutdiamond（切角菱形，切角矩形候选）"},
            {"title": "B: trapezoid（梯形）"},
            {"title": "C: roundedhexagon（竖六边形，竖胶囊候选）"},
            {"title": "D: ellipticrectangle（圆角竖六边形，竖胶囊候选）"},
            {"title": "E: singlebreakangle（单折角，旧误配）"},
            {"title": "F: hexagon（六边形）"},
            {"title": "G: shield（盾牌）"},
            {"title": "H: card（卡片）"},
            {"title": "I: ellipserect（跑道/胶囊）"},
        ],
    )
    shapes = [
        ("A", "org.xmind.topicShape.cutdiamond"),
        ("B", "org.xmind.topicShape.trapezoid"),
        ("C", "org.xmind.topicShape.roundedhexagon"),
        ("D", "org.xmind.topicShape.ellipticrectangle"),
        ("E", "org.xmind.topicShape.singlebreakangle"),
        ("F", "org.xmind.topicShape.hexagon"),
        ("G", "org.xmind.topicShape.shield"),
        ("H", "org.xmind.topicShape.card"),
        ("I", "org.xmind.topicShape.ellipserect"),
    ]
    for node, (label, shape_id) in zip(writer.xm.sheets[0].root.children, shapes):
        writer.set_node_style(node.id, shape=shape_id, font_size=14)

    writer.save(path)
    print("created:", os.path.abspath(path))


if __name__ == "__main__":
    main()
