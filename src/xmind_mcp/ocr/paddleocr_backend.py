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
        """释放 PaddleOCR / Paddle 占用的内存和显存。

        关键点：
        - Paddle 的 GPU 显存由其内部 allocator 池管理，仅 del 模型对象
          不会把显存还给驱动，必须显式调用 empty_cache()。
        - PaddleOCR 对象内部持有 text_detector / text_recognizer 等子网络，
          先把它们置 None 断开引用，再 del 顶层对象，确保 GC 能回收。
        - 任何一步失败都不阻塞后续清理。
        """
        if self._ocr is None:
            return

        ocr = self._ocr
        self._ocr = None

        # 1. 断开 PaddleOCR 内部子网络引用（不同版本属性名不一致，逐个尝试）
        for attr_name in (
            "text_detector", "text_recognizer", "angle_classifier",
            "cls", "det", "rec", "net",
        ):
            attr = getattr(ocr, attr_name, None)
            if attr is not None:
                try:
                    setattr(ocr, attr_name, None)
                except Exception:
                    pass

        # 2. 删除顶层对象 + 强制 GC
        del ocr
        try:
            import gc
            gc.collect()
        except Exception:
            pass

        # 3. 清空 Paddle GPU 显存池（CPU 模式下为 no-op）
        if self._device == "cuda":
            try:
                import paddle
                paddle.device.cuda.empty_cache()
            except Exception:
                # paddle 未安装或非 GPU 版本时忽略
                pass
