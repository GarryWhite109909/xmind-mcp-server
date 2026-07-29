"""OCR 管理器：根据配置选后端，带图片 hash 缓存避免重复识别。"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from ..config import Config, CONFIG_DIR
from .base import OCRBackend, OcrResult, ImageInput
from .rapidocr_backend import RapidOCRBackend
from .paddleocr_backend import PaddleOCRBackend


CACHE_DB = CONFIG_DIR / "ocr_cache.db"


def _image_hash(image: ImageInput) -> str:
    """计算图片内容的 SHA256，用作缓存 key。"""
    if isinstance(image, str):
        with open(image, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    if isinstance(image, bytes):
        return hashlib.sha256(image).hexdigest()
    if isinstance(image, Image.Image):
        buf = io.BytesIO()
        fmt = image.format or "PNG"
        image.save(buf, format=fmt)
        return hashlib.sha256(buf.getvalue()).hexdigest()
    # numpy array
    import numpy as np
    if isinstance(image, np.ndarray):
        return hashlib.sha256(image.tobytes()).hexdigest()
    return str(hash(image))


class OCRManager:
    """统一 OCR 入口，管理后端实例和结果缓存。"""

    def __init__(self, config: Config):
        self._config = config
        self._backend: Optional[OCRBackend] = None
        self._cache_conn: Optional[sqlite3.Connection] = None

    @property
    def engine_name(self) -> str:
        return self._config.ocr_engine

    @property
    def device(self) -> str:
        return self._config.device

    def _get_backend(self) -> OCRBackend:
        """懒加载后端实例。"""
        if self._backend is not None:
            return self._backend

        engine = self._config.ocr_engine
        device = self._config.device

        if engine == "paddleocr":
            self._backend = PaddleOCRBackend(device=device)
        else:
            self._backend = RapidOCRBackend(device=device)

        return self._backend

    def _get_cache(self) -> sqlite3.Connection:
        """打开持久化缓存数据库（SQLite）。"""
        if self._cache_conn is not None:
            return self._cache_conn
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._cache_conn = sqlite3.connect(str(CACHE_DB))
        self._cache_conn.execute("""
            CREATE TABLE IF NOT EXISTS ocr_cache (
                img_hash TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        self._cache_conn.commit()
        return self._cache_conn

    def _cache_get(self, img_hash: str) -> Optional[str]:
        conn = self._get_cache()
        row = conn.execute(
            "SELECT text FROM ocr_cache WHERE img_hash = ?", (img_hash,)
        ).fetchone()
        return row[0] if row else None

    def _cache_put(self, img_hash: str, text: str) -> None:
        conn = self._get_cache()
        conn.execute(
            "INSERT OR REPLACE INTO ocr_cache (img_hash, text, created_at) VALUES (?, ?, ?)",
            (img_hash, text, time.time()),
        )
        conn.commit()

    def recognize(self, image: ImageInput, use_cache: bool = True) -> OcrResult:
        """识别图片，默认走缓存。"""
        img_hash = _image_hash(image)

        if use_cache:
            cached = self._cache_get(img_hash)
            if cached is not None:
                return OcrResult(text=cached, lines=[], elapsed=0.0)

        backend = self._get_backend()
        result = backend.recognize(image)

        if use_cache and result.text:
            self._cache_put(img_hash, result.text)

        return result

    def recognize_text(self, image: ImageInput, use_cache: bool = True) -> str:
        """便捷方法：只返回文本。"""
        return self.recognize(image, use_cache=use_cache).text

    def clear_cache(self) -> int:
        """清空 OCR 缓存，返回删除条数。"""
        conn = self._get_cache()
        cur = conn.execute("DELETE FROM ocr_cache")
        conn.commit()
        return cur.rowcount

    def close(self) -> None:
        if self._cache_conn:
            self._cache_conn.close()
            self._cache_conn = None
