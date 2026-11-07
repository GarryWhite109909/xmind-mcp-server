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

import hashlib
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
    # 所在工作表索引（用于查询该表的主题样式）
    sheet_index: int = 0
    # 节点在文件中的真实类别：root / attached / detached / summary / callout
    # summary 和 callout 是 XMind 的视觉元素（概要/批注），不是普通主题，
    # 模型看到后应避免把它们当普通子主题来编辑。
    topic_class: str = "attached"
    # XMind 的 class 属性：topic / importantTopic（重要主题）/ minorTopic 等
    xmind_class: str = ""
    children: list["TopicNode"] = field(default_factory=list)
    notes: str = ""
    # 图片：src 是引用路径，data 是二进制内容（调用 load_image 后填充）
    image_src: str = ""
    image_data: Optional[bytes] = None
    markers: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    parent_id: str = ""
    # 样式属性：{"style": {...}, "properties": {...}} 的原始 dict
    style: dict = field(default_factory=dict)
    # 布局结构（根主题上的 structureClass），如 org.xmind.ui.map.clockwise
    structure_class: str = ""
    # 超链接目标 URL
    hyperlink: str = ""
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
    # 解析时文件的 SHA256，保存前用于发现“别人改过这个文件”
    source_digest: str = ""
    # 节点索引缓存：refresh_from_raw 后重建，find_topic 从 O(n) 降到 O(1)
    _topic_index: dict[str, TopicNode] = field(default_factory=dict, repr=False)
    _topics_cache: list[TopicNode] = field(default_factory=list, repr=False)

    def _reindex(self) -> None:
        """重建节点列表和 ID 索引。"""
        topics: list[TopicNode] = []
        for sheet in self.sheets:
            self._collect(sheet.root, topics)
        self._topics_cache = topics
        self._topic_index = {t.id: t for t in topics if t.id}

    def all_topics(self) -> list[TopicNode]:
        """遍历所有工作表的所有节点（返回副本，避免调用方改动缓存）。"""
        return list(self._topics_cache)

    @staticmethod
    def _collect(node: TopicNode, out: list[TopicNode]) -> None:
        out.append(node)
        for child in node.children:
            XMindFile._collect(child, out)

    def find_topic(self, topic_id: str) -> Optional[TopicNode]:
        """按 ID 查找节点。"""
        return self._topic_index.get(topic_id)

    def find_topics_with_image(self) -> list[TopicNode]:
        """找出所有有图片的节点。"""
        return [t for t in self.all_topics() if t.image_src]

    def refresh_from_raw(self) -> None:
        """根据修改后的 raw_content 重新生成解析树。"""
        self.sheets = []
        for sheet_index, sheet_dict in enumerate(self.raw_content):
            root_dict = sheet_dict.get("rootTopic", {})
            root_node = _parse_topic(
                root_dict,
                level=0,
                topic_class="root",
                sheet_index=sheet_index,
            )
            self.sheets.append(XMindSheet(
                id=sheet_dict.get("id", ""),
                title=sheet_dict.get("title", ""),
                root=root_node,
            ))
        self._reindex()


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
        markers = markers.get("markerId", [])
    if isinstance(markers, str):
        return [markers] if markers else []
    if isinstance(markers, list):
        return [
            str(m["markerId"]) if isinstance(m, dict) and m.get("markerId") else str(m)
            for m in markers
        ]
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


def _parse_style(topic_dict: dict) -> dict:
    """提取节点样式 dict。"""
    style = topic_dict.get("style")
    if isinstance(style, dict):
        return style
    return {}


def _parse_hyperlink(topic_dict: dict) -> str:
    """提取节点超链接 URL。"""
    hyperlink = topic_dict.get("hyperlink")
    if isinstance(hyperlink, dict):
        return hyperlink.get("link", "")
    if isinstance(hyperlink, str):
        return hyperlink
    return ""


def _parse_topic(
    topic_dict: dict,
    level: int = 0,
    parent_id: str = "",
    topic_class: str = "attached",
    sheet_index: int = 0,
) -> TopicNode:
    """递归解析一个 topic 字典为 TopicNode。"""
    node = TopicNode(
        id=topic_dict.get("id", ""),
        # 兼容标题为 null 的畸形文件：统一归一化为空字符串，
        # 避免 find_node / analyze_file / apply_matching 等下游直接 .lower()/.strip() 崩溃
        title=str(topic_dict.get("title") or ""),
        level=level,
        sheet_index=sheet_index,
        topic_class=topic_class,
        xmind_class=topic_dict.get("class", ""),
        notes=_parse_notes(topic_dict),
        image_src=_parse_image_src(topic_dict),
        markers=_parse_markers(topic_dict),
        labels=_parse_labels(topic_dict),
        parent_id=parent_id,
        style=_parse_style(topic_dict),
        structure_class=topic_dict.get("structureClass", ""),
        hyperlink=_parse_hyperlink(topic_dict),
        _raw=topic_dict,
    )

    children = topic_dict.get("children", {})
    for key in ("attached", "detached", "summary", "callout"):
        child_list = children.get(key, []) if isinstance(children, dict) else []
        for child_dict in child_list:
            if isinstance(child_dict, dict):
                child_node = _parse_topic(
                    child_dict,
                    level=level + 1,
                    parent_id=node.id,
                    topic_class=key,
                    sheet_index=sheet_index,
                )
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


def _xap_entry_candidates(src: str) -> list[str]:
    """列出 xap: 引用可能对应的 zip 路径（兼容不同版本的存储位置）。"""
    if not src or src.startswith("data:"):
        return []
    zip_path = _resolve_image_path(src)
    filename = Path(zip_path).name
    candidates = [
        zip_path,
        src,
        f"attachments/{zip_path}",
        f"attachments/{src}",
        f"attachments/images/{filename}",
        f"resources/{filename}",
    ]
    return list(dict.fromkeys(candidates))


def resolve_xap_entry(src: str, names: set[str]) -> Optional[str]:
    """把 xap: 引用解析成 zip 内真实文件名；找不到返回 None。"""
    for candidate in _xap_entry_candidates(src):
        if candidate in names:
            return candidate
    return None


def collect_xap_refs(value: object) -> set[str]:
    """收集 content.json 里所有 xap: 引用（图片、附件、超链接都可能用）。"""
    refs: set[str] = set()

    def _walk(item: object) -> None:
        if isinstance(item, dict):
            for child in item.values():
                _walk(child)
        elif isinstance(item, list):
            for child in item:
                _walk(child)
        elif isinstance(item, str) and item.startswith("xap:"):
            refs.add(item[4:])

    _walk(value)
    return refs


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

    entry = resolve_xap_entry(src, set(zf.namelist()))
    return zf.read(entry) if entry else None


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

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
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
    for sheet_index, sheet_dict in enumerate(content_raw):
        root_dict = sheet_dict.get("rootTopic", {})
        root_node = _parse_topic(
            root_dict,
            level=0,
            topic_class="root",
            sheet_index=sheet_index,
        )
        sheets.append(XMindSheet(
            id=sheet_dict.get("id", ""),
            title=sheet_dict.get("title", ""),
            root=root_node,
        ))

    xm = XMindFile(
        path=file_path,
        sheets=sheets,
        raw_content=content_raw,
        zip_namelist=namelist,
        _zip_bytes=zip_bytes,
        source_digest=hashlib.sha256(zip_bytes).hexdigest(),
    )
    xm._reindex()
    return xm


def load_preview_image(xm: XMindFile) -> Optional[bytes]:
    """读取 XMind 保存时生成的整图缩略图。

    XMind 每次保存都会写 Thumbnails/thumbnail.png，内容就是用户在软件里
    看到的渲染结果，比任何自绘示意都准确。文件被本工具改过并保存后，
    这张图会失效，因此写入器保存时会主动移除它。
    """
    if not xm._zip_bytes:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(xm._zip_bytes)) as zf:
            names = {name.lower(): name for name in zf.namelist()}
            for candidate in ("thumbnails/thumbnail.png", "thumbnail.png"):
                actual = names.get(candidate)
                if actual:
                    return zf.read(actual)
    except (KeyError, zipfile.BadZipFile, OSError):
        return None
    return None


def load_topic_image(topic: TopicNode, xm: XMindFile) -> Optional[bytes]:
    """加载某个节点的图片数据（从 zip 中提取）。"""
    if not topic.image_src:
        return None
    if topic.image_data is not None:
        return topic.image_data

    if not xm._zip_bytes:
        return None
    with zipfile.ZipFile(io.BytesIO(xm._zip_bytes)) as zf:
        data = load_image_data(topic.image_src, zf)
    topic.image_data = data  # 缓存到节点上
    return data


def topic_ids_in_subtree(topic_dict: dict) -> set[str]:
    """收集一个 raw topic dict 及其所有子节点的 ID（用于删除时清理引用）。"""
    ids = set()
    if not isinstance(topic_dict, dict) or not topic_dict.get("id"):
        return ids
    ids.add(topic_dict["id"])
    children = topic_dict.get("children", {})
    if isinstance(children, dict):
        for key in ("attached", "detached", "summary", "callout"):
            for child in children.get(key, []):
                if isinstance(child, dict):
                    ids |= topic_ids_in_subtree(child)
    return ids
