"""把图片压成适合交给多模态模型的负载。

MCP 客户端拿到的内联图片是 base64，尺寸直接决定 token 成本和传输时间。
所有对外发送的图片统一经过这里：EXIF 方向纠正、透明底铺白、长边缩放、JPEG 压缩。
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageOps, UnidentifiedImageError

DEFAULT_MAX_SIDE = 1024
MAX_MAX_SIDE = 2048  # 再大对模型没有收益，只会烧 token
DEFAULT_MAX_BYTES = 1_500_000
MIN_MAX_SIDE = 256
JPEG_QUALITY = 85


@dataclass(frozen=True)
class PreparedImage:
    """压缩后的图片负载。"""

    data: bytes
    mime_type: str
    width: int
    height: int
    source_width: int
    source_height: int
    source_bytes: int

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def size_kb(self) -> int:
        return max(1, round(len(self.data) / 1024))


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """JPEG 不支持透明通道，统一铺白底（铺黑会把深色文字吞掉）。"""
    if image.mode == "P":
        image = image.convert("RGBA")
    if image.mode in ("RGBA", "LA"):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _encode_jpeg(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def prepare_image(
    data: bytes,
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Optional[PreparedImage]:
    """把任意图片转成 JPEG 负载；无法解码时返回 None，不抛异常。"""
    if not data:
        return None

    try:
        max_side = int(max_side or DEFAULT_MAX_SIDE)
    except (TypeError, ValueError):
        max_side = DEFAULT_MAX_SIDE
    max_side = min(MAX_MAX_SIDE, max(MIN_MAX_SIDE, max_side))

    try:
        with Image.open(io.BytesIO(data)) as opened:
            # 动图只取第一帧
            opened.seek(0)
            image = ImageOps.exif_transpose(opened)
            source_width, source_height = image.size
            image = _flatten_to_rgb(image)

            if max(image.size) > max_side:
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

            encoded = _encode_jpeg(image, JPEG_QUALITY)

            # 噪点多的图压完仍可能超标：先降质量，再降尺寸
            quality = JPEG_QUALITY
            while len(encoded) > max_bytes and quality > 60:
                quality -= 10
                encoded = _encode_jpeg(image, quality)
            while len(encoded) > max_bytes and max(image.size) > MIN_MAX_SIDE:
                image = image.resize(
                    (max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)),
                    Image.Resampling.LANCZOS,
                )
                encoded = _encode_jpeg(image, 75)

            return PreparedImage(
                data=encoded,
                mime_type="image/jpeg",
                width=image.width,
                height=image.height,
                source_width=source_width,
                source_height=source_height,
                source_bytes=len(data),
            )
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
