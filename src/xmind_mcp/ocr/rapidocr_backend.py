"""RapidOCR (基于 ONNX Runtime) 后端实现。

优势：跨平台、DirectML 支持 Windows 上所有显卡、安装简单。
"""

from __future__ import annotations

import time
import io
from typing import Optional

from PIL import Image

from .base import OCRBackend, OcrResult, ImageInput


class RapidOCRBackend(OCRBackend):
    """RapidOCR 后端，基于 onnxruntime。"""

    def __init__(self, device: str = "cpu"):
        self._device = device
        self._ocr = None

    def _ensure_loaded(self) -> None:
        """懒加载：首次调用时才导入和初始化，避免启动慢。"""
        if self._ocr is not None:
            return
        from rapidocr_onnxruntime import RapidOCR

        # RapidOCR 内部用 onnxruntime 的 provider 自动选择
        # 这里可以通过参数指定，但 RapidOCR 构造参数随版本变化
        # 让它自动选即可，onnxruntime 会用安装的 provider
        self._ocr = RapidOCR()

    @staticmethod
    def _to_numpy(image: ImageInput):
        """把输入统一转成 numpy array（RapidOCR 接受 ndarray 或文件路径）。"""
        if isinstance(image, str):
            return image  # 文件路径直接传
        if isinstance(image, bytes):
            image = Image.open(io.BytesIO(image))
        if isinstance(image, Image.Image):
            import numpy as np
            if image.mode != "RGB":
                image = image.convert("RGB")
            return np.array(image)
        return image  # 假设已经是 ndarray

    def recognize(self, image: ImageInput) -> OcrResult:
        self._ensure_loaded()
        img = self._to_numpy(image)

        t0 = time.time()
        result, _ = self._ocr(img)
        elapsed = time.time() - t0

        lines = []
        texts = []
        if result:
            for item in result:
                # RapidOCR 返回 [box, text, confidence]
                box, text, conf = item
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
        """释放 RapidOCR / onnxruntime 占用的内存和显存。"""
        if self._ocr is None:
            return

        # 尝试释放 onnxruntime 内部 session
        try:
            session = getattr(self._ocr, "session", None)
            if session is not None:
                # 某些版本有 __del__，显式释放输入输出名引用
                session._inputs_meta = None
                session._outputs_meta = None
                del session
        except Exception:
            pass

        del self._ocr
        self._ocr = None

        # 强制触发 Python 垃圾回收，尽快释放显存
        try:
            import gc
            gc.collect()
        except Exception:
            pass
