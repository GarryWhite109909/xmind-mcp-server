"""PaddleOCR 后端实现。

优势：生态成熟、模型丰富。
劣势：安装重、AMD/Intel GPU 不支持 GPU 加速。
"""

from __future__ import annotations

import time
import io
from typing import Optional

from PIL import Image

from .base import OCRBackend, OcrResult, ImageInput


class PaddleOCRBackend(OCRBackend):
    """PaddleOCR 后端。"""

    def __init__(self, device: str = "cpu", lang: str = "ch"):
        self._device = device
        self._lang = lang
        self._ocr = None

    def _ensure_loaded(self) -> None:
        """懒加载。"""
        if self._ocr is not None:
            return
        from paddleocr import PaddleOCR

        self._ocr = PaddleOCR(
            use_angle_cls=True,          # 方向分类，处理旋转文字
            lang=self._lang,             # 中文
            use_gpu=(self._device == "cuda"),
            show_log=False,
        )

    @staticmethod
    def _to_path_or_array(image: ImageInput):
        """PaddleOCR 接受文件路径或 ndarray。"""
        if isinstance(image, str):
            return image
        if isinstance(image, bytes):
            image = Image.open(io.BytesIO(image))
        if isinstance(image, Image.Image):
            import numpy as np
            if image.mode != "RGB":
                image = image.convert("RGB")
            return np.array(image)
        return image

    def recognize(self, image: ImageInput) -> OcrResult:
        self._ensure_loaded()
        img = self._to_path_or_array(image)

        t0 = time.time()
        result = self._ocr.ocr(img, cls=True)
        elapsed = time.time() - t0

        lines = []
        texts = []
        # PaddleOCR 返回 [[ [box, (text, conf)], ... ]]
        if result and result[0]:
            for item in result[0]:
                box, (text, conf) = item
                lines.append({
                    "text": text,
                    "confidence": float(conf),
                    "box": box,
                })
                texts.append(text)

        return OcrResult(
            text="\n".join(texts),
            lines=lines,
            elapsed=elapsed,
        )

    def close(self) -> None:
        """释放 PaddleOCR / Paddle 占用的内存和显存。"""
        if self._ocr is None:
            return
        try:
            import gc
            del self._ocr
            self._ocr = None
            gc.collect()
        except Exception:
            pass
