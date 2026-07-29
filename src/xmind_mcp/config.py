"""配置管理：读写 ~/.xmind-mcp/config.json，记录硬件检测结果和用户选择。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


CONFIG_DIR = Path.home() / ".xmind-mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Config:
    """首次启动后持久化的配置。"""

    # OCR 引擎：rapidocr | paddleocr
    ocr_engine: str = "rapidocr"
    # 推理设备：cuda | directml | openvino | rocm | cpu
    device: str = "cpu"
    # 检测到的显卡信息（可能多张）
    gpus: list[dict] = field(default_factory=list)
    # CPU 逻辑核心数
    cpu_count: int = 0
    # 内存 GB
    ram_gb: float = 0.0
    # 操作系统：windows | linux | macos
    os: str = ""
    # OCR 模型缓存目录
    model_cache_dir: str = ""
    # 是否已完成首次安装
    initialized: bool = False
    # 安装时用的 pip 包列表（用于二次验证）
    installed_packages: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        """读取配置文件，不存在则返回默认实例。"""
        if not CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            # 兼容字段缺失
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError):
            return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @property
    def is_ready(self) -> bool:
        """配置是否完整且已初始化。"""
        return self.initialized and bool(self.ocr_engine) and bool(self.device)
