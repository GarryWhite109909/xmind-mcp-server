"""LaTeX 公式渲染：把 Markdown 里的数学公式渲染成 PNG 图片。

用 matplotlib 的 mathtext 引擎（纯 Python，无需安装 LaTeX 发行版）。
mathtext 支持大多数常用数学语法（分数、根号、求和、积分、希腊字母、
矩阵、上下标等），但不支持 \\text{} / 多行环境等高级命令。

渲染失败时返回 False，调用方应保留原始 LaTeX 文本，避免导入崩溃。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("xmind_mcp.formula")


def render_latex_png(latex: str, out_path: "str | Path", fontsize: float = 20, dpi: int = 200) -> bool:
    """用 matplotlib mathtext 把一块 LaTeX 公式渲染成 PNG。

    Args:
        latex: 公式体（不含 $$ 包裹）。
        out_path: 输出 PNG 路径。
        fontsize / dpi: 渲染尺寸控制。

    Returns:
        是否渲染成功。失败时不会创建（或留下空）文件，并打日志。
    """
    fig = None
    plt = None
    try:
        import matplotlib

        matplotlib.use("Agg")  # 无界面后端，禁止弹窗
        import matplotlib.pyplot as plt

        # mathtext 需要 $...$ 包裹才能渲染数学模式
        expr = f"${latex}$"
        fig = plt.figure()
        fig.patch.set_alpha(0.0)
        fig.text(0.0, 0.0, expr, fontsize=fontsize)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            out_path,
            dpi=dpi,
            bbox_inches="tight",
            pad_inches=0.05,
            transparent=True,
        )
        return True
    except Exception as exc:  # mathtext 不支持某些命令时抛异常
        logger.warning("公式渲染失败，保留 LaTeX 文本: %s (%s: %s)", latex, type(exc).__name__, exc)
        try:
            out_path = Path(out_path)
            if out_path.exists():
                out_path.unlink()
        except Exception:
            pass
        return False
    finally:
        # 成功和失败都释放 figure，避免批量导入时累积内存
        if fig is not None and plt is not None:
            try:
                plt.close(fig)
            except Exception:
                pass