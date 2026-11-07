"""发布形态守卫：交付给用户的版本不包含 OCR / 推理依赖。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_ocr_runtime_dependencies():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    for forbidden in ("rapidocr", "onnxruntime", "paddleocr", "paddlepaddle"):
        assert forbidden not in pyproject


def test_no_ocr_package_or_tool_module():
    package = ROOT / "src" / "xmind_mcp"
    assert not (package / "ocr").exists()
    assert not (package / "hardware").exists()

    tools = (package / "tools.py").read_text(encoding="utf-8")
    assert "read_image_ocr" not in tools
    assert "OCRManager" not in tools
