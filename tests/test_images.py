"""图片通道测试：内联原图、缩略图预览、保存时清理过期缩略图。"""

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from xmind_mcp.config import Config
from xmind_mcp.image_payload import prepare_image
from xmind_mcp.ocr.base import OcrResult
from xmind_mcp.tools import XMindTools
from xmind_mcp.xmind_parser import parse_xmind
from xmind_mcp.xmind_writer import XMindWriter


@pytest.fixture
def config() -> Config:
    return Config(ocr_engine="rapidocr", device="cpu", initialized=True)


def _png(width: int = 64, height: int = 48, color=(220, 60, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _make_xmind(path: Path, *,
                image: bytes | None = None,
                thumbnail: bytes | None = None,
                with_image_node: bool = True) -> Path:
    children = [{"id": "child-1", "title": "普通节点"}]
    if with_image_node and image is not None:
        children.insert(0, {
            "id": "img-node",
            "title": "图表",
            "image": {"src": "xap:attachments/images/pic.png"},
        })

    content = [{
        "id": "sheet-1",
        "title": "图片测试",
        "rootTopic": {"id": "root-1", "title": "根主题",
                      "children": {"attached": children}},
    }]

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))
        zf.writestr("metadata.json", json.dumps({"dataStructureVersion": "3"}))
        zf.writestr(
            "manifest.json",
            json.dumps({"file-entries": {"content.json": {}}}),
        )
        if image is not None:
            zf.writestr("attachments/images/pic.png", image)
        if thumbnail is not None:
            zf.writestr("Thumbnails/thumbnail.png", thumbnail)
    return path


def test_prepare_image_downscales_long_side():
    prepared = prepare_image(_png(3000, 2000), max_side=512)
    assert prepared is not None
    assert prepared.mime_type == "image/jpeg"
    assert max(prepared.width, prepared.height) <= 512
    assert prepared.source_width == 3000
    assert prepared.data.startswith(b"\xff\xd8")  # JPEG SOI


def test_prepare_image_keeps_small_image_size():
    prepared = prepare_image(_png(200, 120), max_side=1024)
    assert prepared is not None
    assert (prepared.width, prepared.height) == (200, 120)


def test_prepare_image_clamps_absurd_max_side():
    """误传巨大 max_side 时不能把整张原图原样发出去。"""
    prepared = prepare_image(_png(4000, 3000), max_side=100_000)
    assert prepared is not None
    assert max(prepared.width, prepared.height) == 2048


def test_prepare_image_rejects_garbage():
    assert prepare_image(b"not an image") is None
    assert prepare_image(b"") is None


async def test_get_image_returns_image_block(config: Config, tmp_path: Path):
    path = _make_xmind(tmp_path / "img.xmind", image=_png(800, 600))
    tools = XMindTools(config)

    blocks = await tools.call_tool("get_image", {"file_path": str(path), "node_id": "img-node"})

    images = [b for b in blocks if getattr(b, "type", "") == "image"]
    assert len(images) == 1
    assert images[0].mime_type == "image/jpeg"
    decoded = base64.b64decode(images[0].data)
    assert Image.open(io.BytesIO(decoded)).size == (800, 600)


async def test_get_image_without_image_returns_text(config: Config, tmp_path: Path):
    path = _make_xmind(tmp_path / "plain.xmind")
    tools = XMindTools(config)

    blocks = await tools.call_tool("get_image", {"file_path": str(path), "node_id": "child-1"})
    assert all(getattr(b, "type", "") == "text" for b in blocks)
    assert "没有图片" in blocks[0].text


async def test_get_preview_returns_thumbnail(config: Config, tmp_path: Path):
    path = _make_xmind(tmp_path / "thumb.xmind", thumbnail=_png(500, 300))
    tools = XMindTools(config)

    blocks = await tools.call_tool("get_preview", {"file_path": str(path)})

    images = [b for b in blocks if getattr(b, "type", "") == "image"]
    assert len(images) == 1
    decoded = base64.b64decode(images[0].data)
    assert Image.open(io.BytesIO(decoded)).size == (500, 300)


async def test_get_preview_without_thumbnail_explains(config: Config, tmp_path: Path):
    path = _make_xmind(tmp_path / "nothumb.xmind")
    tools = XMindTools(config)

    blocks = await tools.call_tool("get_preview", {"file_path": str(path)})
    assert all(getattr(b, "type", "") == "text" for b in blocks)
    assert "没有内嵌缩略图" in blocks[0].text


async def test_read_all_inlines_images_by_default(config: Config, tmp_path: Path):
    path = _make_xmind(tmp_path / "readall.xmind", image=_png(400, 300))
    tools = XMindTools(config)

    blocks = await tools.call_tool("read_all", {"file_path": str(path)})

    assert blocks[0].type == "text"
    assert "img-node" in blocks[0].text
    assert any(b.type == "image" for b in blocks[1:])


async def test_read_all_ocr_mode_stays_text_only(config: Config, tmp_path: Path):
    path = _make_xmind(tmp_path / "ocr.xmind", image=_png(400, 300))
    tools = XMindTools(config)
    tools.ocr.recognize = lambda image, use_cache=True: OcrResult(
        text="识别出的文字", lines=[], elapsed=0.0
    )

    blocks = await tools.call_tool(
        "read_all", {"file_path": str(path), "image_mode": "ocr"}
    )
    assert len(blocks) == 1
    assert blocks[0].type == "text"
    assert "识别出的文字" in blocks[0].text


async def test_read_all_respects_max_images(config: Config, tmp_path: Path):
    path = _make_xmind(tmp_path / "limit.xmind", image=_png(400, 300))
    tools = XMindTools(config)

    blocks = await tools.call_tool(
        "read_all", {"file_path": str(path), "max_images": 0}
    )
    assert all(b.type == "text" for b in blocks)
    assert "未内联" in blocks[-1].text


def test_save_drops_stale_thumbnail(tmp_path: Path):
    path = _make_xmind(
        tmp_path / "save.xmind",
        image=_png(),
        thumbnail=_png(600, 400),
    )
    writer = XMindWriter(parse_xmind(path))
    writer.save()

    with zipfile.ZipFile(path) as zf:
        names = [n.lower() for n in zf.namelist()]
        manifest = json.loads(zf.read("manifest.json"))

    assert "thumbnails/thumbnail.png" not in names
    assert "thumbnails/thumbnail.png" not in {
        k.lower() for k in manifest.get("file-entries", {})
    }
    assert writer.dropped_preview is True


async def test_save_file_mentions_dropped_preview(config: Config, tmp_path: Path):
    path = _make_xmind(tmp_path / "note.xmind", thumbnail=_png(600, 400))
    tools = XMindTools(config)

    result = await tools.call_tool("save_file", {"file_path": str(path)})
    assert "已移除过期缩略图" in result[0].text
