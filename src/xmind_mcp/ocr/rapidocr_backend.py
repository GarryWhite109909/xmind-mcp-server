"""RapidOCR (基于 ONNX Runtime) 后端实现。

优势：跨平台、DirectML 支持 Windows 上所有显卡、安装简单。
"""

from __future__ import annotations

import io
import sys
import time
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

        kwargs = {}
        if self._device == "cuda":
            # 让 RapidOCR 为 det/cls/rec 三个模型都启用 CUDA
            kwargs.update(
                det_use_cuda=True,
                cls_use_cuda=True,
                rec_use_cuda=True,
            )
        elif self._device == "directml":
            # DirectML 需要 rapidocr-onnxruntime >= 1.3.23；老版本没有 use_dml 开关，
            # 传进去会被静默忽略，导致用户以为在用 DirectML 实际在跑 CPU。
            if _rapidocr_supports_dml():
                kwargs.update(
                    det_use_dml=True,
                    cls_use_dml=True,
                    rec_use_dml=True,
                )
            else:
                print(
                    "[xmind-mcp/ocr] 当前 rapidocr-onnxruntime 版本不支持 DirectML，"
                    "已回退 CPU。请升级 rapidocr-onnxruntime>=1.3.23 或使用 --device cpu。",
                    file=sys.stderr,
                    flush=True,
                )

        self._ocr = RapidOCR(**kwargs)

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
        """释放 RapidOCR / onnxruntime 占用的内存和显存。

        关键点：
        - RapidOCR 内部含 det/cls/rec 三个独立 InferenceSession，
          每个 session 在 CUDA/DirectML EP 下都会占显存。
        - onnxruntime 没有公开的 session.close()，显存释放依赖
          session 对象被 GC 回收，因此先断开 RapidOCR 对内部 session
          的所有引用，再 del 顶层对象 + gc.collect()。
        - 不同 RapidOCR 版本内部属性名不一致，逐个尝试。
        """
        if self._ocr is None:
            return

        ocr = self._ocr
        self._ocr = None

        # 1. 断开 RapidOCR 内部所有 InferenceSession 引用
        #    （不同版本属性名不同，全部尝试一遍）
        for attr_name in (
            "session", "session_det", "session_cls", "session_rec",
            "text_detector", "text_recognizer", "angle_classifier",
            "det", "cls", "rec",
        ):
            attr = getattr(ocr, attr_name, None)
            if attr is not None:
                try:
                    setattr(ocr, attr_name, None)
                except Exception:
                    pass

        # 2. 删除顶层对象 + 强制 GC
        #    onnxruntime 的 InferenceSession 在被 GC 时会释放底层显存
        del ocr
        try:
            import gc
            gc.collect()
        except Exception:
            pass


def _rapidocr_supports_dml() -> bool:
    """检测当前 rapidocr-onnxruntime 是否支持 DirectML 开关。"""
    try:
        from importlib.metadata import version

        raw = version("rapidocr-onnxruntime")
        parts = []
        for piece in raw.split(".")[:3]:
            digits = "".join(ch for ch in piece if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        while len(parts) < 3:
            parts.append(0)
        return parts >= [1, 3, 23]
    except Exception:
        return False
