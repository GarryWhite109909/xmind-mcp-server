"""OCR 后端抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Union

from PIL import Image


# 图片输入类型：PIL Image / 文件路径 / 二进制 bytes
ImageInput = Union[Image.Image, str, bytes]


@dataclass
class OcrResult:
    """单次 OCR 识别结果。"""

    # 识别出的完整文本（多行用 \n 连接）
    text: str
    # 带位置和置信度的明细
    lines: list[dict]  # [{"text": "...", "confidence": 0.95, "box": [[x,y],...]}]
    # 耗时秒
    elapsed: float = 0.0


class OCRBackend(ABC):
    """OCR 引擎后端抽象基类。"""

    @abstractmethod
    def recognize(self, image: ImageInput) -> OcrResult:
        """识别图片中的文字。"""
        ...

    @abstractmethod
    def close(self) -> None:
        """释放模型占用的内存/显存资源。"""
        ...

    def recognize_text(self, image: ImageInput) -> str:
        """只返回纯文本，便捷方法。"""
        return self.recognize(image).text
