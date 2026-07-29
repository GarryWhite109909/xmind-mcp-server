"""OCR 引擎抽象层：统一接口，底层可切换 RapidOCR / PaddleOCR。"""

from .base import OCRBackend, OcrResult
from .manager import OCRManager

__all__ = ["OCRBackend", "OcrResult", "OCRManager"]
