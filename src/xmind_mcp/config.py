"""配置管理：读写 ~/.xmind-mcp/config.json。

只保存与运行行为有关的少数选项；图片默认直接交给多模态模型，
不涉及 OCR 引擎或推理设备。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


CONFIG_DIR = Path.home() / ".xmind-mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"
# 覆盖保存前的自动备份目录
BACKUP_DIR = CONFIG_DIR / "backups"

# 图片处理方式：inline（默认，把原图交给多模态模型）| off（只给文字结构）
IMAGE_MODES = ("inline", "off")
DEFAULT_IMAGE_MAX_SIDE = 1024


@dataclass
class Config:
    """可选的运行配置。所有字段都有默认值，未初始化也能直接启动。"""

    # 读取导图时图片怎么处理：inline / off
    image_mode: str = "inline"
    # 内联图片的长边上限（像素），越大越贵
    image_max_side: int = DEFAULT_IMAGE_MAX_SIDE
    # 只加载哪些工具组：all（默认）| read | edit | style | export | maintenance
    # 可组合，如 "read,export"。只读场景建议只留 read，能省掉大部分 schema 开销。
    toolset: str = "all"
    # 只允许读写这些目录下的文件（留空 = 不限制）
    allowed_roots: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        """读取配置文件，不存在或损坏时返回默认实例。"""
        if not CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        # 只接收当前认识的字段，旧版本遗留的 ocr_engine/device 等自动忽略
        config = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        config.image_mode = normalize_image_mode(config.image_mode)
        try:
            config.image_max_side = int(config.image_max_side)
        except (TypeError, ValueError):
            config.image_max_side = DEFAULT_IMAGE_MAX_SIDE
        if isinstance(config.allowed_roots, str):
            config.allowed_roots = [config.allowed_roots]
        elif not isinstance(config.allowed_roots, list):
            config.allowed_roots = []
        return config

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def normalize_image_mode(value: object) -> str:
    """把任意（含旧版 ocr/both）图片模式归一化为 inline 或 off。

    旧配置里的 ocr/both 一律按 inline 处理：图片直接交给多模态模型，
    既不丢内容，也不要求用户重新配置。
    """
    mode = str(value or "").strip().lower()
    return mode if mode in IMAGE_MODES else "inline"
