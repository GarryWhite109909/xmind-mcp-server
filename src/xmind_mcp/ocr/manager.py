"""OCR 管理器：根据配置选后端，带图片 hash 缓存避免重复识别。"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from ..config import Config, CONFIG_DIR
from .base import OCRBackend, OcrResult, ImageInput
from .rapidocr_backend import RapidOCRBackend
from .paddleocr_backend import PaddleOCRBackend


def _log(msg: str) -> None:
    """输出到 stderr，避免污染 MCP 的 stdio 通信。"""
    print(f"[xmind-mcp/ocr] {msg}", file=sys.stderr, flush=True)


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
        self._cache_disabled = False
        # OCR 后端初始化不是线程安全的（onnxruntime 会话创建），加锁保护。
        self._backend_lock = threading.Lock()

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

        with self._backend_lock:
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
        if self._cache_disabled:
            return None  # type: ignore[return-value]
        try:
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
        except Exception as e:
            # 缓存目录不可写（只读环境/权限不足）时禁用缓存，不影响 OCR 本身
            _log(f"OCR 缓存不可用，已禁用: {type(e).__name__}: {e}")
            self._cache_disabled = True
            if self._cache_conn is not None:
                try:
                    self._cache_conn.close()
                except Exception:
                    pass
                self._cache_conn = None
            return None  # type: ignore[return-value]

    def _cache_get(self, img_hash: str) -> Optional[str]:
        try:
            conn = self._get_cache()
            if conn is None:
                return None
            row = conn.execute(
                "SELECT text FROM ocr_cache WHERE img_hash = ?", (img_hash,)
            ).fetchone()
            return row[0] if row else None
        except Exception as e:
            _log(f"OCR 缓存读取失败，已跳过缓存: {type(e).__name__}: {e}")
            self._cache_disabled = True
            return None

    def _cache_put(self, img_hash: str, text: str) -> None:
        try:
            conn = self._get_cache()
            if conn is None:
                return
            conn.execute(
                "INSERT OR REPLACE INTO ocr_cache (img_hash, text, created_at) VALUES (?, ?, ?)",
                (img_hash, text, time.time()),
            )
            conn.commit()
        except Exception as e:
            _log(f"OCR 缓存写入失败，已禁用缓存: {type(e).__name__}: {e}")
            self._cache_disabled = True
            return

    def recognize(self, image: ImageInput, use_cache: bool = True) -> OcrResult:
        """识别图片，默认走缓存。"""
        img_hash = _image_hash(image)
        # 缓存带上引擎名：切换 RapidOCR/PaddleOCR 后不会复用旧引擎的结果。
        cache_key = f"{self._config.ocr_engine}:{img_hash}"

        if use_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return OcrResult(text=cached, lines=[], elapsed=0.0)

        backend = self._get_backend()
        result = backend.recognize(image)

        if use_cache and result.text:
            self._cache_put(cache_key, result.text)

        return result

    def recognize_text(self, image: ImageInput, use_cache: bool = True) -> str:
        """便捷方法：只返回文本。"""
        return self.recognize(image, use_cache=use_cache).text

    def clear_cache(self) -> int:
        """清空 OCR 缓存，返回删除条数。"""
        conn = self._get_cache()
        if conn is None:
            return 0
        cur = conn.execute("DELETE FROM ocr_cache")
        conn.commit()
        return cur.rowcount

    def close(self) -> None:
        """关闭缓存数据库连接。"""
        if self._cache_conn:
            self._cache_conn.close()
            self._cache_conn = None

    def unload(self) -> None:
        """释放所有 OCR 资源：关闭缓存 + 卸载模型。

        幂等：重复调用安全。失败不抛异常，但会记录到 stderr。
        """
        if self._cache_conn is not None:
            try:
                self._cache_conn.close()
            except Exception as e:
                _log(f"关闭缓存数据库失败: {type(e).__name__}: {e}")
            self._cache_conn = None

        if self._backend is not None:
            backend_name = type(self._backend).__name__
            try:
                _log(f"开始卸载 OCR 模型 ({backend_name})...")
                self._backend.close()
                _log(f"OCR 模型已卸载 ({backend_name})")
            except Exception as e:
                _log(f"卸载 OCR 模型失败 ({backend_name}): {type(e).__name__}: {e}")
            self._backend = None
