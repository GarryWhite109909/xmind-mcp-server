"""XMind 文件写入器：修改 content.json 并重新打包成 .xmind。

核心思路：
- 在原始 content.json（raw_content）的 dict 上做增量修改
- 每次修改后调用 XMindFile.refresh_from_raw() 重新生成解析树，避免内存树和 raw_content 不同步
- 保存时：打开原始 zip → 复制所有文件 → 替换 content.json → 写新 zip
- 这样能保留样式、主题、附件等所有未修改的资源
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from typing import Optional, Union

from .xmind_parser import XMindFile, TopicNode, _parse_topic


def _find_topic_and_parent(
    raw_content: list[dict],
    topic_id: str,
) -> tuple[Optional[dict], Optional[dict]]:
    """在 raw_content 中按 ID 查找 topic dict 及其父 topic dict。

    Returns:
        (topic_dict, parent_dict) — parent_dict 为 None 表示这是根主题。
        如果找不到，返回 (None, None)。
    """
    def _search(topic: dict, parent: Optional[dict]) -> tuple[Optional[dict], Optional[dict]]:
        if topic.get("id") == topic_id:
            return (topic, parent)
        children = topic.get("children", {})
        for key in ("attached", "detached", "summary", "callout"):
            for child in children.get(key, []):
                found = _search(child, topic)
                if found[0] is not None:
                    return found
        return (None, None)

    for sheet in raw_content:
        root = sheet.get("rootTopic", {})
        found = _search(root, None)
        if found[0] is not None:
            return found
    return (None, None)


def _ensure_children_attached(topic: dict) -> list:
    """确保 topic 有 children.attached 列表，返回该列表。"""
    if "children" not in topic or not isinstance(topic["children"], dict):
        topic["children"] = {}
    if "attached" not in topic["children"]:
        topic["children"]["attached"] = []
    return topic["children"]["attached"]


def _remove_topic_from_parent(parent: dict, topic_id: str) -> bool:
    """从父节点的所有子节点列表中移除指定 id 的 topic。"""
    children = parent.get("children", {})
    removed = False
    for key in ("attached", "detached", "summary", "callout"):
        lst = children.get(key, [])
        if lst:
            new_lst = [c for c in lst if c.get("id") != topic_id]
            if len(new_lst) != len(lst):
                children[key] = new_lst
                removed = True
    return removed


class XMindWriter:
    """修改 XMind 文件并保存。"""

    def __init__(self, xm: XMindFile):
        self.xm = xm

    def add_node(
        self,
        parent_id: str,
        title: str,
        notes: str = "",
        markers: Optional[list[str]] = None,
    ) -> TopicNode:
        """在指定父节点下添加子节点。"""
        parent_dict, _ = _find_topic_and_parent(self.xm.raw_content, parent_id)
        if parent_dict is None:
            raise ValueError(f"找不到父节点: {parent_id}")

        new_id = str(uuid.uuid4())
        new_topic = {
            "id": new_id,
            "title": title,
            "children": {"attached": [], "detached": []},
        }
        if notes:
            new_topic["notes"] = {"text": notes}
        if markers:
            new_topic["markers"] = {"markerId": markers}

        attached_list = _ensure_children_attached(parent_dict)
        attached_list.append(new_topic)

        self.xm.refresh_from_raw()
        new_node = self.xm.find_topic(new_id)
        if new_node is None:
            raise RuntimeError("新增节点后刷新解析树失败")
        return new_node

    def update_node(
        self,
        node_id: str,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        markers: Optional[list[str]] = None,
    ) -> bool:
        """更新节点属性，None 表示不修改。"""
        topic_dict, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        if title is not None:
            topic_dict["title"] = title
        if notes is not None:
            topic_dict["notes"] = {"text": notes}
        if markers is not None:
            topic_dict["markers"] = {"markerId": markers}

        self.xm.refresh_from_raw()
        return True

    def delete_node(self, node_id: str) -> bool:
        """删除节点及其所有子节点。不能删除根主题。"""
        topic_dict, parent_dict = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False
        if parent_dict is None:
            raise ValueError("不能删除根主题")

        _remove_topic_from_parent(parent_dict, node_id)
        self.xm.refresh_from_raw()
        return True

    def move_node(self, node_id: str, new_parent_id: str) -> bool:
        """把节点移动到新父节点下。"""
        topic_dict, old_parent_dict = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None or old_parent_dict is None:
            return False  # 不能移动根主题或找不到节点

        new_parent_dict, _ = _find_topic_and_parent(self.xm.raw_content, new_parent_id)
        if new_parent_dict is None:
            return False

        # 不能移动到自己或自己的后代下
        def _is_descendant(parent: dict, child_id: str) -> bool:
            children = parent.get("children", {})
            for key in ("attached", "detached", "summary", "callout"):
                for child in children.get(key, []):
                    if child.get("id") == child_id:
                        return True
                    if _is_descendant(child, child_id):
                        return True
            return False

        if new_parent_id == node_id or _is_descendant(topic_dict, new_parent_id):
            raise ValueError("不能把节点移动到自己或自己的后代下")

        # 从旧父节点移除
        _remove_topic_from_parent(old_parent_dict, node_id)

        # 加到新父节点的 attached
        new_attached = _ensure_children_attached(new_parent_dict)
        new_attached.append(topic_dict)

        self.xm.refresh_from_raw()
        return True

    def add_nodes(self, parent_id: str, titles: list[str]) -> list[TopicNode]:
        """在指定父节点下批量添加多个子节点。"""
        parent_dict, _ = _find_topic_and_parent(self.xm.raw_content, parent_id)
        if parent_dict is None:
            raise ValueError(f"找不到父节点: {parent_id}")

        attached_list = _ensure_children_attached(parent_dict)
        new_ids = []
        for title in titles:
            new_id = str(uuid.uuid4())
            attached_list.append({
                "id": new_id,
                "title": title,
                "children": {"attached": [], "detached": []},
            })
            new_ids.append(new_id)

        self.xm.refresh_from_raw()
        nodes = [n for n in self.xm.all_topics() if n.id in new_ids]
        return nodes

    def duplicate_node(self, node_id: str, new_parent_id: str) -> Optional[TopicNode]:
        """把节点（含其整棵子树）复制到新父节点下，生成新的 ID。"""
        topic_dict, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return None

        new_parent_dict, _ = _find_topic_and_parent(self.xm.raw_content, new_parent_id)
        if new_parent_dict is None:
            raise ValueError(f"找不到新父节点: {new_parent_id}")

        # 深拷贝并重新生成所有子节点 ID
        def _rewrite_ids(topic: dict) -> dict:
            import copy
            new_topic = copy.deepcopy(topic)
            new_topic["id"] = str(uuid.uuid4())
            children = new_topic.get("children", {})
            if isinstance(children, dict):
                for key in ("attached", "detached", "summary", "callout"):
                    lst = children.get(key, [])
                    if lst:
                        children[key] = [_rewrite_ids(c) for c in lst]
            return new_topic

        copied = _rewrite_ids(topic_dict)
        attached_list = _ensure_children_attached(new_parent_dict)
        attached_list.append(copied)

        self.xm.refresh_from_raw()
        return self.xm.find_topic(copied["id"])

    def set_node_style(
        self,
        node_id: str,
        *,
        font_color: Optional[str] = None,
        font_size: Optional[str] = None,
        font_family: Optional[str] = None,
        font_weight: Optional[str] = None,
        font_style: Optional[str] = None,
        background_color: Optional[str] = None,
        border_color: Optional[str] = None,
        text_align: Optional[str] = None,
    ) -> bool:
        """设置节点样式（字体/颜色等）。值传 None 表示不修改该项。"""
        topic_dict, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        style = topic_dict.get("style")
        if not isinstance(style, dict):
            style = {}
            topic_dict["style"] = style
        props = style.get("properties")
        if not isinstance(props, dict):
            props = {}
            style["properties"] = props

        mapping = {
            "font_color": "fo:color",
            "font_size": "fo:font-size",
            "font_family": "fo:font-family",
            "font_weight": "fo:font-weight",
            "font_style": "fo:font-style",
            "background_color": "fo:background-color",
            "border_color": "fo:border-color",
            "text_align": "fo:text-align",
        }
        for kw, fo_key in mapping.items():
            value = locals().get(kw)
            if value is not None:
                props[fo_key] = value

        self.xm.refresh_from_raw()
        return True

    def set_hyperlink(self, node_id: str, url: str, title: str = "") -> bool:
        """给节点设置超链接。url 为空则移除超链接。"""
        topic_dict, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        if not url:
            topic_dict.pop("hyperlink", None)
        else:
            topic_dict["hyperlink"] = {"link": url, "title": title or url}

        self.xm.refresh_from_raw()
        return True

    def add_marker(self, node_id: str, marker_id: str) -> bool:
        """给节点添加一个标记图标（如 priority-1、task-done）。"""
        topic_dict, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        markers = topic_dict.get("markers")
        if not isinstance(markers, dict):
            markers = {"markerId": []}
            topic_dict["markers"] = markers
        marker_list = markers.get("markerId")
        if not isinstance(marker_list, list):
            marker_list = []
            markers["markerId"] = marker_list
        if marker_id not in marker_list:
            marker_list.append(marker_id)

        self.xm.refresh_from_raw()
        return True

    def remove_marker(self, node_id: str, marker_id: str) -> bool:
        """移除节点的指定标记图标。"""
        topic_dict, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        markers = topic_dict.get("markers")
        if isinstance(markers, dict) and isinstance(markers.get("markerId"), list):
            markers["markerId"] = [m for m in markers["markerId"] if m != marker_id]
            if not markers["markerId"]:
                topic_dict.pop("markers", None)

        self.xm.refresh_from_raw()
        return True

    def add_label(self, node_id: str, label: str) -> bool:
        """给节点添加一个标签。"""
        topic_dict, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        labels = topic_dict.get("labels")
        if not isinstance(labels, list):
            labels = []
            topic_dict["labels"] = labels
        if label not in labels:
            labels.append(label)

        self.xm.refresh_from_raw()
        return True

    def remove_label(self, node_id: str, label: str) -> bool:
        """移除节点的一个标签。"""
        topic_dict, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        labels = topic_dict.get("labels")
        if isinstance(labels, list):
            topic_dict["labels"] = [l for l in labels if l != label]
            if not topic_dict["labels"]:
                topic_dict.pop("labels", None)

        self.xm.refresh_from_raw()
        return True

    def save(self, output_path: Optional[Union[str, Path]] = None) -> str:
        """保存文件。output_path 为 None 时覆盖原文件。

        Returns:
            实际保存的路径
        """
        if output_path is None:
            output_path = self.xm.path
        output_path = str(output_path)

        # 序列化修改后的 content.json
        new_content = json.dumps(self.xm.raw_content, ensure_ascii=False, indent=2)

        # 读取原始 zip，复制所有文件，替换 content.json
        source_zip = io.BytesIO(self.xm._zip_bytes)
        with zipfile.ZipFile(source_zip, "r") as zin:
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename == "content.json":
                        zout.writestr("content.json", new_content)
                    else:
                        zout.writestr(item, zin.read(item.filename))

        # 更新内部缓存
        with open(output_path, "rb") as f:
            self.xm._zip_bytes = f.read()
        self.xm.path = output_path

        return output_path
