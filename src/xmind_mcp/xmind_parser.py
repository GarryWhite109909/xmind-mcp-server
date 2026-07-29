"""XMind 文件解析器。

XMind 文件本质是 ZIP 包，内部结构（新版 JSON 格式）：
  content.json          — 主题树（核心数据）
  content.xml           — 旧版格式，新版不存在
  metadata.json         — 元数据
  manifest.json         — 清单
  attachments/          — 附件
  attachments/images/   — 图片附件
  markers/              — 标记图标

content.json 结构：
  [
    {
      "id": "sheet-id",
      "title": "工作表标题",
      "rootTopic": {
        "id": "topic-1",
        "title": "中心主题",
        "children": {
          "attached": [ {子topic}, ... ],
          "detached": [ ... ]          // 浮动主题
        },
        "notes": { "text": "..." },   // 备注（新版）或 "notes": { "plain": {...} }
        "image": { "src": "xap:images/xxx.png" },  // 图片引用
        "markers": { "markerId": [ "priority-1" ] }
      },
      "theme": {...}
    },
    ...  // 多个工作表
  ]

图片引用格式 "xap:images/xxx.png" → zip 内路径 "attachments/images/xxx.png"
也可能是 data:image/png;base64,xxxx 内嵌格式
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


@dataclass
class TopicNode:
    """一个 XMind 主题节点。"""

    id: str
    title: str
    level: int = 0  # 中心主题=0，第一层子主题=1，以此类推
    children: list["TopicNode"] = field(default_factory=list)
    notes: str = ""
    # 图片：src 是引用路径，data 是二进制内容（调用 load_image 后填充）
    image_src: str = ""
    image_data: Optional[bytes] = None
    markers: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    parent_id: str = ""
    # 在 XMind 文件中的原始 topic dict（用于写回时定位）
    _raw: Optional[dict] = field(default=None, repr=False)


@dataclass
class XMindSheet:
    """一个工作表。"""

    id: str
    title: str
    root: TopicNode


@dataclass
class XMindFile:
    """解析后的 XMind 文件。"""

    path: str
    sheets: list[XMindSheet]
    # 原始 content.json（用于修改后写回）
    raw_content: list[dict]
    # zip 内所有文件名列表
    zip_namelist: list[str]
    # 原始 zip 内容缓存（用于写回时保留未修改的资源）
    _zip_bytes: Optional[bytes] = field(default=None, repr=False)

    def all_topics(self) -> list[TopicNode]:
        """遍历所有工作表的所有节点。"""
        result = []
        for sheet in self.sheets:
            self._collect(sheet.root, result)
        return result

    @staticmethod
    def _collect(node: TopicNode, out: list[TopicNode]) -> None:
        out.append(node)
        for child in node.children:
            XMindFile._collect(child, out)

    def find_topic(self, topic_id: str) -> Optional[TopicNode]:
        """按 ID 查找节点。"""
        for topic in self.all_topics():
            if topic.id == topic_id:
                return topic
        return None

    def find_topics_with_image(self) -> list[TopicNode]:
        """找出所有有图片的节点。"""
        return [t for t in self.all_topics() if t.image_src]

    def refresh_from_raw(self) -> None:
        """根据修改后的 raw_content 重新生成解析树。"""
        self.sheets = []
        for sheet_dict in self.raw_content:
            root_dict = sheet_dict.get("rootTopic", {})
            root_node = _parse_topic(root_dict, level=0)
            self.sheets.append(XMindSheet(
                id=sheet_dict.get("id", ""),
                title=sheet_dict.get("title", ""),
                root=root_node,
            ))


# ---------- 解析逻辑 ----------

def _parse_notes(topic_dict: dict) -> str:
    """提取备注文本（兼容多种格式）。"""
    notes = topic_dict.get("notes")
    if not notes:
        return ""
    # 新版: {"text": "..."}
    if isinstance(notes, dict):
        if "text" in notes:
            return notes["text"]
        # 旧版: {"plain": {"content": "..."}}
        plain = notes.get("plain", {})
        if isinstance(plain, dict) and "content" in plain:
            return plain["content"]
    return str(notes)


def _parse_markers(topic_dict: dict) -> list[str]:
    """提取标记图标 ID。"""
    markers = topic_dict.get("markers")
    if not markers:
        return []
    if isinstance(markers, dict):
        return markers.get("markerId", [])
    if isinstance(markers, list):
        return [m.get("markerId", "") if isinstance(m, dict) else str(m) for m in markers]
    return []


def _parse_labels(topic_dict: dict) -> list[str]:
    """提取节点标签列表。"""
    labels = topic_dict.get("labels", [])
    if isinstance(labels, list):
        return [str(label) for label in labels]
    if isinstance(labels, str):
        return [labels]
    return []


def _parse_image_src(topic_dict: dict) -> str:
    """提取图片引用路径。"""
    image = topic_dict.get("image")
    if not image or not isinstance(image, dict):
        return ""
    return image.get("src", "")


def _parse_topic(topic_dict: dict, level: int = 0, parent_id: str = "") -> TopicNode:
    """递归解析一个 topic 字典为 TopicNode。"""
    node = TopicNode(
        id=topic_dict.get("id", ""),
        title=topic_dict.get("title", ""),
        level=level,
        notes=_parse_notes(topic_dict),
        image_src=_parse_image_src(topic_dict),
        markers=_parse_markers(topic_dict),
        labels=_parse_labels(topic_dict),
        parent_id=parent_id,
        _raw=topic_dict,
    )

    children = topic_dict.get("children", {})
    for key in ("attached", "detached", "summary", "callout"):
        child_list = children.get(key, []) if isinstance(children, dict) else []
        for child_dict in child_list:
            if isinstance(child_dict, dict):
                child_node = _parse_topic(child_dict, level=level + 1, parent_id=node.id)
                node.children.append(child_node)

    return node


def _resolve_image_path(src: str) -> str:
    """把 XMind 图片引用转成 zip 内路径。
    'xap:resources/xxx.png' → 'resources/xxx.png'
    'xap:images/xxx.png' → 'images/xxx.png'
    data:image/... 内嵌格式原样返回（load_image 时另行处理）
    """
    if src.startswith("data:"):
        return src
    if src.startswith("xap:"):
        # xap: 是 zip 内相对路径的 scheme 前缀
        return src[4:]
    return src


def load_image_data(src: str, zf: zipfile.ZipFile) -> Optional[bytes]:
    """从 zip 中读取图片二进制，支持内嵌 base64 和附件引用。"""
    if not src:
        return None

    # data:image/png;base64,xxxx
    if src.startswith("data:"):
        import base64
        match = re.match(r"data:image/[^;]+;base64,(.+)", src, re.DOTALL)
        if match:
            return base64.b64decode(match.group(1))
        return None

    zip_path = _resolve_image_path(src)
    try:
        return zf.read(zip_path)
    except KeyError:
        # 尝试其他可能路径（兼容不同 XMind 版本的存储位置）
        filename = Path(zip_path).name
        candidates = [
            zip_path,
            src,
            f"attachments/{zip_path}",
            f"attachments/{src}",
            f"attachments/images/{filename}",
            f"resources/{filename}",
        ]
        for candidate in candidates:
            try:
                return zf.read(candidate)
            except KeyError:
                continue
        return None


def parse_xmind(file_path: Union[str, Path]) -> XMindFile:
    """解析 XMind 文件。

    Args:
        file_path: .xmind 文件路径

    Returns:
        XMindFile 对象

    Raises:
        ValueError: 文件不是有效的 XMind 或格式不支持
    """
    file_path = str(file_path)

    with open(file_path, "rb") as f:
        zip_bytes = f.read()

    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    namelist = zf.namelist()

    # 检测格式
    if "content.json" not in namelist:
        if "content.xml" in namelist:
            raise ValueError(
                "检测到旧版 XMind 8 格式（content.xml），当前仅支持新版 JSON 格式。"
                "请在 XMind 2020+ 中另存为新格式。"
            )
        raise ValueError(f"不是有效的 XMind 文件：缺少 content.json，内部文件: {namelist}")

    content_raw = json.loads(zf.read("content.json"))
    if isinstance(content_raw, dict):
        content_raw = [content_raw]

    sheets: list[XMindSheet] = []
    for sheet_dict in content_raw:
        root_dict = sheet_dict.get("rootTopic", {})
        root_node = _parse_topic(root_dict, level=0)
        sheets.append(XMindSheet(
            id=sheet_dict.get("id", ""),
            title=sheet_dict.get("title", ""),
            root=root_node,
        ))

    return XMindFile(
        path=file_path,
        sheets=sheets,
        raw_content=content_raw,
        zip_namelist=namelist,
        _zip_bytes=zip_bytes,
    )


def load_topic_image(topic: TopicNode, xm: XMindFile) -> Optional[bytes]:
    """加载某个节点的图片数据（从 zip 中提取）。"""
    if not topic.image_src:
        return None
    if topic.image_data is not None:
        return topic.image_data

    zf = zipfile.ZipFile(io.BytesIO(xm._zip_bytes))
    data = load_image_data(topic.image_src, zf)
    topic.image_data = data  # 缓存到节点上
    return data
