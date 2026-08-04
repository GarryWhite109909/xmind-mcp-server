"""测试 fixtures。"""

import json
import zipfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_backup_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把自动备份目录重定向到临时目录，避免测试污染用户主目录。"""
    from xmind_mcp import xmind_writer

    monkeypatch.setattr(xmind_writer, "BACKUP_DIR", tmp_path / "backups")


@pytest.fixture
def sample_xmind(tmp_path: Path) -> Path:
    """创建一个用于测试的临时 XMind 文件。"""
    content = [{
        "id": "sheet-1",
        "title": "测试工作表",
        "rootTopic": {
            "id": "root-1",
            "title": "根主题",
            "children": {
                "attached": [
                    {"id": "child-1", "title": "子节点1"},
                    {"id": "child-2", "title": "子节点2", "children": {"attached": [
                        {"id": "grand-1", "title": "孙节点"},
                    ]}},
                ]
            },
        },
    }]
    path = tmp_path / "test.xmind"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))
    return path


@pytest.fixture
def real_xmind() -> Path:
    """仓库自带的测试 XMind 文件。"""
    return Path(__file__).with_name("中心主题.xmind").resolve()
