"""XMind 文件写入器：修改 content.json 并重新打包成 .xmind。

核心思路：
- 在原始 content.json（raw_content）的 dict 上做增量修改
- 每次修改后调用 XMindFile.refresh_from_raw() 重新生成解析树，避免内存树和 raw_content 不同步
- 保存时：打开原始 zip → 复制所有文件 → 替换 content.json → 写新 zip
- 这样能保留样式、主题、附件等所有未修改的资源
"""

from __future__ import annotations

import copy
import io
import hashlib
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional, Union

from . import __version__
from .config import BACKUP_DIR
from .xmind_parser import XMindFile, TopicNode, parse_xmind, topic_ids_in_subtree
from .xmind_props import (
    ARROW_ENDS,
    BORDER_PATTERNS,
    BRANCH_LINES,
    FILL_PATTERNS,
    FONT_STYLES,
    FONT_WEIGHTS,
    IMPORTANCE,
    SHAPES,
    STRUCTURES,
    TEXT_ALIGNS,
    TEXT_TRANSFORMS,
    THEME_LEVELS,
    normalize_color,
    resolve,
    resolve_font_size,
    resolve_text_decoration,
    resolve_width,
)


STYLE_PRESETS: dict[str, dict[str, dict[str, str]]] = {
    "清新蓝": {
        "root": {"background_color": "#E8F1FB", "font_color": "#1F4E79", "font_weight": "bold"},
        "branch": {"background_color": "#D6E6F5", "font_color": "#1F4E79"},
        "leaf": {"background_color": "#FFFFFF", "font_color": "#2E2E2E"},
    },
    "暖阳橙": {
        "root": {"background_color": "#FFF3E0", "font_color": "#B45309", "font_weight": "bold"},
        "branch": {"background_color": "#FFE4C4", "font_color": "#92400E"},
        "leaf": {"background_color": "#FFFFFF", "font_color": "#3F3F3F"},
    },
    "莫兰迪绿": {
        "root": {"background_color": "#E3EFE8", "font_color": "#2F5D50", "font_weight": "bold"},
        "branch": {"background_color": "#D3E5DA", "font_color": "#2F5D50"},
        "leaf": {"background_color": "#F5FAF7", "font_color": "#333333"},
    },
}


_STYLE_MAPPING: dict[str, tuple[str, object]] = {
    "font_color": ("fo:color", normalize_color),
    "font_size": ("fo:font-size", resolve_font_size),
    "font_family": ("fo:font-family", None),
    "font_weight": ("fo:font-weight", lambda v: resolve(FONT_WEIGHTS, v, label="字重")),
    "font_style": ("fo:font-style", lambda v: resolve(FONT_STYLES, v, label="字体样式")),
    "font_transform": ("fo:text-transform", lambda v: resolve(TEXT_TRANSFORMS, v, label="文字变换")),
    "font_decoration": ("fo:text-decoration", resolve_text_decoration),
    "background_color": ("svg:fill", normalize_color),
    "border_color": ("border-line-color", normalize_color),
    "border_width": ("border-line-width", lambda v: resolve_width(v, label="边框宽度")),
    "border_pattern": ("border-line-pattern", lambda v: resolve(BORDER_PATTERNS, v, label="边框线型")),
    "shape": ("shape-class", lambda v: resolve(SHAPES, v, label="形状")),
    "line_color": ("line-color", normalize_color),
    "line_width": ("line-width", lambda v: resolve_width(v, label="线宽")),
    "line_pattern": ("line-pattern", lambda v: resolve(BORDER_PATTERNS, v, label="线条线型")),
    "text_align": ("fo:text-align", lambda v: resolve(TEXT_ALIGNS, v, label="对齐")),
    "fill_pattern": ("fill-pattern", lambda v: resolve(FILL_PATTERNS, v, label="填充样式")),
}


def _prepare_style_updates(style: dict) -> dict:
    """把用户可读的样式参数全部校验并解析成 XMind 属性 dict。

    支持中文名称（形状/线宽/对齐等）、#RRGGBB、#RRGGBBAA、百分比透明度颜色。
    先完整校验再返回结果，调用方拿到后一次性写入，
    保证“参数非法时不做任何部分修改”（原子性）。
    """
    updates: dict[str, str] = {}
    for kw, (fo_key, normalizer) in _STYLE_MAPPING.items():
        value = style.get(kw)
        if value is None:
            continue
        if normalizer is not None:
            value = normalizer(value)
        if value is not None:
            updates[fo_key] = value
    return updates


# XMind 打开文件时的必需元数据。缺失 metadata.json 时 XMind 会直接报错拒绝打开。
DEFAULT_METADATA: dict = {
    "dataStructureVersion": "3",
    "layoutEngineVersion": "5",
    "creator": {"name": "xmind-mcp", "version": __version__},
}

# 内置默认主题（与 XMind 26 实际保存的默认主题结构一致），缓存避免重复读盘。
_DEFAULT_THEME: Optional[dict] = None


def _load_default_theme() -> dict:
    """读取内置默认主题。"""
    global _DEFAULT_THEME
    if _DEFAULT_THEME is None:
        theme_path = Path(__file__).with_name("default_theme.json")
        _DEFAULT_THEME = json.loads(theme_path.read_text(encoding="utf-8"))
    return _DEFAULT_THEME


def _new_theme() -> dict:
    """返回一份带全新 id 的默认主题副本（每个 sheet 独立，避免同文件内 id 冲突）。"""
    theme = copy.deepcopy(_load_default_theme())
    for value in theme.values():
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            value["id"] = str(uuid.uuid4())
    return theme


def _xml_text(value: str) -> str:
    """转义 XML 文本中的特殊字符。"""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _xml_attr(value: str) -> str:
    """转义 XML 属性值。"""
    return _xml_text(value).replace('"', "&quot;")


def _content_xml_stub(raw_content: list[dict]) -> str:
    """生成新版文件附带的最小 content.xml 占位（兼容旧版读取器；XMind 26 以 content.json 为准）。"""
    sheet = raw_content[0] if raw_content else {}
    if not isinstance(sheet, dict):
        sheet = {}
    root = sheet.get("rootTopic", {})
    if not isinstance(root, dict):
        root = {}
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        '<xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0" '
        'xmlns:fo="http://www.w3.org/1999/XSL/Format" '
        'xmlns:svg="http://www.w3.org/2000/svg" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" version="2.0">'
        f'<sheet id="{_xml_attr(sheet.get("id", ""))}">'
        f'<topic id="{_xml_attr(root.get("id", ""))}">'
        f"<title>{_xml_text(root.get('title', ''))}</title>"
        "</topic></sheet></xmap-content>"
    )


def _ensure_topic_class(topic: dict) -> None:
    """给缺失 class 的普通主题补上 "topic"（保留 importantTopic 等特殊类型；
    概要/批注节点靠 children 位置区分，不强加 class，避免显示异常）。"""
    if not isinstance(topic, dict):
        return
    topic.setdefault("class", "topic")
    children = topic.get("children", {})
    if isinstance(children, dict):
        for key in ("attached", "detached"):
            for child in children.get(key, []):
                _ensure_topic_class(child)


def _backup_file(path: str) -> Optional[str]:
    """覆盖保存前把原文件备份到 ~/.xmind-mcp/backups/，每个文件保留最近 20 份。"""
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stem = Path(path).stem
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        dest = BACKUP_DIR / f"{stem}-{timestamp}.xmind"
        # 同一秒内多次备份时避免文件名冲突（否则会互相覆盖）
        counter = 1
        while dest.exists():
            dest = BACKUP_DIR / f"{stem}-{timestamp}-{counter}.xmind"
            counter += 1
        shutil.copyfile(path, dest)

        # 只保留最近 20 份同名备份
        backups = sorted(
            BACKUP_DIR.glob(f"{stem}-*.xmind"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[20:]:
            old.unlink(missing_ok=True)
        return str(dest)
    except Exception:
        # 备份失败不能阻断保存；失败时静默降级（用户仍能正常保存）。
        return None


def list_backups(file_path: str) -> list[dict]:
    """列出某个 xmind 文件已有的自动备份（新到旧）。"""
    stem = Path(file_path).stem
    backups: list[dict] = []
    if not BACKUP_DIR.is_dir():
        return backups
    for p in sorted(
        BACKUP_DIR.glob(f"{stem}-*.xmind"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        st = p.stat()
        backups.append({
            "path": str(p),
            "name": p.name,
            "size": st.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        })
    return backups


def restore_backup(file_path: str, backup_path: str) -> str:
    """把指定备份恢复到目标文件（覆盖前先把当前文件也备份一份，保证可逆）。"""
    backup = Path(backup_path)
    if not backup.exists():
        raise FileNotFoundError(f"备份文件不存在: {backup_path}")
    target = Path(file_path)
    if target.exists():
        _backup_file(str(target))
    shutil.copyfile(backup, target)
    return str(target)


def _find_topic_and_parent(
    raw_content: list[dict],
    topic_id: str,
) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
    """在 raw_content 中按 ID 查找 topic dict 及其父 topic dict。

    Returns:
        (topic_dict, parent_dict, kind)
        - kind 是子节点所在的 children 键（attached/detached/summary/callout），
          根主题的 kind 为 None。
        如果找不到，返回 (None, None, None)。
    """
    def _search(
        topic: dict,
        parent: Optional[dict],
    ) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
        if topic.get("id") == topic_id:
            return (topic, parent, None)
        children = topic.get("children", {})
        for key in ("attached", "detached", "summary", "callout"):
            for child in children.get(key, []):
                if child.get("id") == topic_id:
                    return (child, topic, key)
                found = _search(child, topic)
                if found[0] is not None:
                    return found
        return (None, None, None)

    for sheet in raw_content:
        root = sheet.get("rootTopic", {})
        if root.get("id") == topic_id:
            return (root, None, None)
        for key in ("attached", "detached", "summary", "callout"):
            for child in root.get("children", {}).get(key, []):
                if child.get("id") == topic_id:
                    return (child, root, key)
        found = _search(root, None)
        if found[0] is not None:
            return found
    return (None, None, None)


def _set_notes(topic_dict: dict, notes: str) -> None:
    """写入备注，尽量保留文件原有的 notes 结构（plain/text），默认写新版格式。"""
    existing = topic_dict.get("notes")
    if isinstance(existing, dict):
        plain = existing.get("plain")
        if isinstance(plain, dict):
            plain["content"] = notes
            return
        if "text" in existing:
            existing["text"] = notes
            return
    topic_dict["notes"] = {"plain": {"content": notes}}


def _notes_real_html(text: str) -> str:
    """生成 XMind 画布显式备注用的 realHTML 内容。"""
    escaped = (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return "<div>" + escaped.replace("\n", "<br>") + "</div>"


def _raw_notes_text(topic_dict: dict) -> str:
    """从 raw topic dict 里取备注纯文本（兼容新版 text 与旧版 plain.content）。"""
    notes = topic_dict.get("notes")
    if not isinstance(notes, dict):
        return str(notes) if notes else ""
    if isinstance(notes.get("text"), str):
        return notes["text"]
    plain = notes.get("plain")
    if isinstance(plain, dict) and isinstance(plain.get("content"), str):
        return plain["content"]
    return ""


def _parse_range_indices(range_value: object) -> Optional[tuple[int, int]]:
    """解析 XMind 的 range 字段："(0,1)" 或带 startId/endId 的 dict。

    新版 XMind 里 summary/boundary 的 range 是字符串索引 "(i,j)"，
    指向父节点 children.attached 里的区间；旧一些的格式是 dict。
    """
    if isinstance(range_value, str):
        match = re.match(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", range_value)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _remove_summary_topic(topic: dict, summary_topic_id: str) -> None:
    """从父节点的 children.summary 中移除概要主题节点。"""
    children = topic.get("children", {})
    if isinstance(children, dict):
        summary_list = children.get("summary", [])
        children["summary"] = [
            c for c in summary_list
            if isinstance(c, dict) and c.get("id") != summary_topic_id
        ]
        if not children["summary"]:
            children.pop("summary", None)


def _cleanup_dangling_refs(
    raw_content: list[dict],
    removed_ids: set[str],
) -> None:
    """删除节点后，清理引用被删 ID 的 relationships / summaries / boundaries。

    XMind 里这些引用如果悬空，轻则打开报错，重则导致文件打不开，
    所以删除子树时要一并清掉。字符串索引区间（"(i,j)"）由
    _remap_visual_ranges 在调用方处理，这里只处理 dict 区间和 ID 引用。
    """
    if not removed_ids:
        return

    def _range_touches(range_dict: Optional[dict]) -> bool:
        if not isinstance(range_dict, dict):
            return False
        start = range_dict.get("startId")
        end = range_dict.get("endId")
        return start in removed_ids or end in removed_ids

    def _clean_topic(topic: dict) -> None:
        for key in ("summaries", "boundaries"):
            items = topic.get(key)
            if isinstance(items, list):
                keep: list[dict] = []
                for item in items:
                    if not isinstance(item, dict):
                        keep.append(item)
                        continue
                    if (
                        item.get("topicId") in removed_ids
                        or item.get("id") in removed_ids
                        or _range_touches(item.get("range"))
                    ):
                        # 概要条目被移除时，连带删掉对应的概要主题节点
                        summary_topic_id = item.get("topicId")
                        if summary_topic_id and summary_topic_id not in removed_ids:
                            _remove_summary_topic(topic, summary_topic_id)
                            removed_ids.add(summary_topic_id)
                        continue
                    keep.append(item)
                topic[key] = keep
                if not topic[key]:
                    topic.pop(key, None)

    for sheet in raw_content:
        if not isinstance(sheet, dict):
            continue

        # sheet 级 relationships
        rels = sheet.get("relationships")
        if isinstance(rels, list):
            sheet["relationships"] = [
                r for r in rels
                if isinstance(r, dict)
                and r.get("end1Id") not in removed_ids
                and r.get("end2Id") not in removed_ids
            ]
            if not sheet["relationships"]:
                sheet.pop("relationships", None)

        # 所有 topic 上的 summaries / boundaries
        def _walk(topic: dict) -> None:
            _clean_topic(topic)
            children = topic.get("children", {})
            if isinstance(children, dict):
                for key in ("attached", "detached", "summary", "callout"):
                    for child in children.get(key, []):
                        if isinstance(child, dict):
                            _walk(child)

        root = sheet.get("rootTopic")
        if isinstance(root, dict):
            _walk(root)


def _attached_ids(parent_dict: dict) -> list[str]:
    """返回父节点 attached 子节点的 ID 列表。"""
    attached = parent_dict.get("children", {}).get("attached", [])
    return [
        child.get("id")
        for child in attached
        if isinstance(child, dict) and child.get("id")
    ]


def _remap_visual_ranges(parent_dict: dict, old_attached_ids: list[str]) -> None:
    """结构变更后，按“原区间覆盖的节点 ID”重算 summary/boundary 的字符串索引区间。

    XMind 的 summary/boundary 用字符串索引 "(i,j)" 指向父节点 attached 子列表。
    删除/插入/移动兄弟节点后索引会整体漂移，如果不重算，视觉元素会悄悄
    绑到错误的节点上。这里先把旧区间翻译成节点 ID，再映射回新索引；
    区间内任一节点已不存在（被删除/移走）则整个条目失效并清除。
    """
    new_ids = _attached_ids(parent_dict)
    new_index = {tid: i for i, tid in enumerate(new_ids)}

    for key, topic_key in (("summaries", "topicId"), ("boundaries", None)):
        items = parent_dict.get(key)
        if not isinstance(items, list):
            continue
        keep: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                keep.append(item)
                continue
            rng = _parse_range_indices(item.get("range"))
            if rng is None:
                # dict 区间由 _cleanup_dangling_refs 按 ID 处理，这里原样保留
                keep.append(item)
                continue
            start, end = rng
            if start < 0 or end >= len(old_attached_ids):
                keep.append(item)
                continue
            covered = old_attached_ids[start : end + 1]
            covered = [tid for tid in covered if tid]
            if not covered or not all(tid in new_index for tid in covered):
                if key == "summaries" and topic_key:
                    summary_topic_id = item.get(topic_key)
                    if summary_topic_id:
                        _remove_summary_topic(parent_dict, summary_topic_id)
                continue
            item["range"] = f"({new_index[covered[0]]},{new_index[covered[-1]]})"
            keep.append(item)
        parent_dict[key] = keep
        if not keep:
            parent_dict.pop(key, None)


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
        # 新增的 zip 资源（如插入的图片），保存时写入
        self._extra_files: dict[str, bytes] = {}
        # 保存时要排除的 zip 条目（如删除的未引用附件）
        self._exclude_files: set[str] = set()

    @staticmethod
    def _new_sheet(
        title: str,
        root_title: str,
        children: Optional[list[dict]] = None,
    ) -> dict:
        """构造一张新工作表的完整 dict（含默认主题/扩展/revisionId）。"""
        root_id = str(uuid.uuid4())
        root = {
            "id": root_id,
            "title": root_title,
            "class": "topic",
            "titleUnedited": True,
            "structureClass": "org.xmind.ui.map.clockwise",
            "children": {"attached": children or [], "detached": []},
        }
        return {
            "id": str(uuid.uuid4()),
            "revisionId": str(uuid.uuid4()),
            "class": "sheet",
            "title": title,
            "rootTopic": root,
            "relationships": [],
            "topicOverlapping": "overlap",
            "arrangeableLayerOrder": [root_id],
            "zones": [],
            "extensions": [{
                "provider": "org.xmind.ui.skeleton.structure.style",
                "content": {"centralTopic": "org.xmind.ui.map.clockwise"},
            }],
            "theme": _new_theme(),
        }

    def add_node(
        self,
        parent_id: str,
        title: str,
        notes: str = "",
        markers: Optional[list[str]] = None,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
    ) -> TopicNode:
        """在指定父节点下添加子节点。"""
        parent_dict, _, parent_kind = _find_topic_and_parent(self.xm.raw_content, parent_id)
        if parent_dict is None:
            raise ValueError(f"找不到父节点: {parent_id}")
        if parent_kind in ("summary", "callout"):
            raise ValueError("不能给概要(summary)或批注(callout)节点添加子节点")
        if before_id is not None and after_id is not None:
            raise ValueError("before_id 和 after_id 不能同时指定")

        new_id = str(uuid.uuid4())
        new_topic = {
            "id": new_id,
            "title": title,
            "class": "topic",
            "children": {"attached": [], "detached": []},
        }
        if notes:
            new_topic["notes"] = {"plain": {"content": notes}}
        if markers:
            new_topic["markers"] = {"markerId": markers}

        attached_list = _ensure_children_attached(parent_dict)
        old_ids = _attached_ids(parent_dict)
        self._insert_topic(attached_list, new_topic, before_id, after_id)
        # 插入会改变兄弟索引，重算概要/边界区间，避免视觉元素错位
        _remap_visual_ranges(parent_dict, old_ids)

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
        topic_dict, _, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        if title is not None:
            topic_dict["title"] = title
        if notes is not None:
            _set_notes(topic_dict, notes)
        if markers is not None:
            topic_dict["markers"] = {"markerId": markers}

        self.xm.refresh_from_raw()
        return True

    def delete_node(self, node_id: str) -> bool:
        """删除节点及其所有子节点。不能删除根主题。"""
        topic_dict, parent_dict, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False
        if parent_dict is None:
            raise ValueError("不能删除根主题")

        removed_ids = topic_ids_in_subtree(topic_dict)
        old_ids = _attached_ids(parent_dict)
        _remove_topic_from_parent(parent_dict, node_id)
        _cleanup_dangling_refs(self.xm.raw_content, removed_ids)
        _remap_visual_ranges(parent_dict, old_ids)
        self.xm.refresh_from_raw()
        return True

    def move_node(
        self,
        node_id: str,
        new_parent_id: str,
        before_id: Optional[str] = None,
        after_id: Optional[str] = None,
    ) -> bool:
        """把节点移动到新父节点下，可选指定插入位置（before_id/after_id）。"""
        topic_dict, old_parent_dict, kind = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None or old_parent_dict is None:
            return False  # 不能移动根主题或找不到节点
        if kind in ("summary", "callout"):
            raise ValueError("不能移动概要(summary)或批注(callout)节点")

        new_parent_dict, _, new_parent_kind = _find_topic_and_parent(self.xm.raw_content, new_parent_id)
        if new_parent_dict is None:
            return False
        if new_parent_kind in ("summary", "callout"):
            raise ValueError("不能把节点移动到概要(summary)或批注(callout)节点下")
        if before_id is not None and after_id is not None:
            raise ValueError("before_id 和 after_id 不能同时指定")
        if node_id in (before_id, after_id):
            raise ValueError("插入位置节点无效：不能以自身为位置参考")

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

        # 先校验插入位置（必须是目标父节点的直属子节点），
        # 任何错误都不能先动树，否则节点会从旧位置消失（数据丢失）。
        siblings = [
            child.get("id")
            for child in new_parent_dict.get("children", {}).get("attached", [])
            if isinstance(child, dict)
        ]
        for ref_id, label in ((before_id, "before_id"), (after_id, "after_id")):
            if ref_id is not None and ref_id not in siblings:
                raise ValueError(
                    f"找不到 {label} 指定的节点: {ref_id}（必须是目标父节点的直属子节点）"
                )

        same_parent = old_parent_dict is new_parent_dict
        old_ids = _attached_ids(old_parent_dict)
        new_ids = _attached_ids(new_parent_dict)

        # 从旧父节点移除
        _remove_topic_from_parent(old_parent_dict, node_id)

        # 加到新父节点的 attached（支持指定位置）
        new_attached = _ensure_children_attached(new_parent_dict)
        self._insert_topic(new_attached, topic_dict, before_id, after_id)

        # 移动会让旧父/新父的兄弟索引都漂移，分别重算概要/边界区间
        if same_parent:
            _remap_visual_ranges(new_parent_dict, old_ids)
        else:
            _remap_visual_ranges(old_parent_dict, old_ids)
            _remap_visual_ranges(new_parent_dict, new_ids)

        self.xm.refresh_from_raw()
        return True

    def add_nodes(self, parent_id: str, titles: list[str]) -> list[TopicNode]:
        """在指定父节点下批量添加多个子节点。"""
        parent_dict, _, parent_kind = _find_topic_and_parent(self.xm.raw_content, parent_id)
        if parent_dict is None:
            raise ValueError(f"找不到父节点: {parent_id}")
        if parent_kind in ("summary", "callout"):
            raise ValueError("不能给概要(summary)或批注(callout)节点添加子节点")

        attached_list = _ensure_children_attached(parent_dict)
        old_ids = _attached_ids(parent_dict)
        new_ids = []
        for title in titles:
            new_id = str(uuid.uuid4())
            attached_list.append({
                "id": new_id,
                "title": title,
                "class": "topic",
                "children": {"attached": [], "detached": []},
            })
            new_ids.append(new_id)

        _remap_visual_ranges(parent_dict, old_ids)
        self.xm.refresh_from_raw()
        nodes = [n for n in self.xm.all_topics() if n.id in new_ids]
        return nodes

    def duplicate_node(self, node_id: str, new_parent_id: str) -> Optional[TopicNode]:
        """把节点（含其整棵子树）复制到新父节点下，生成新的 ID。"""
        topic_dict, _, kind = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return None
        if kind in ("summary", "callout"):
            raise ValueError("不能复制概要(summary)或批注(callout)节点")

        new_parent_dict, _, new_parent_kind = _find_topic_and_parent(self.xm.raw_content, new_parent_id)
        if new_parent_dict is None:
            raise ValueError(f"找不到新父节点: {new_parent_id}")
        if new_parent_kind in ("summary", "callout"):
            raise ValueError("不能把节点复制到概要(summary)或批注(callout)节点下")

        # 不能复制到自己或自己的后代下（否则会生成自嵌套的畸形树）
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
            raise ValueError("不能把节点复制到自己或自己的后代下")

        # 深拷贝并重新生成所有子节点 ID
        def _rewrite_ids(topic: dict) -> dict:
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
        importance: Optional[str] = None,
        **style: object,
    ) -> bool:
        """设置节点样式（字体/颜色/形状/填充/边框等）。值传 None 表示不修改该项。

        importance 设置主题重要程度（极其重要/重要/删去/默认），
        其余参数与 set_node_style 一致（支持中文名、#RRGGBB、#RRGGBBAA、百分比透明度）。
        """
        topic_dict, _, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        # 先完整校验所有参数，再一次性写入，避免参数非法时产生部分修改
        new_class = (
            resolve(IMPORTANCE, importance, label="重要程度")
            if importance is not None
            else None
        )
        updates = _prepare_style_updates(style)
        if new_class is not None:
            topic_dict["class"] = new_class
        if updates:
            topic_style = topic_dict.get("style")
            if not isinstance(topic_style, dict):
                topic_style = {}
                topic_dict["style"] = topic_style
            props = topic_style.get("properties")
            if not isinstance(props, dict):
                props = {}
                topic_style["properties"] = props
            props.update(updates)

        self.xm.refresh_from_raw()
        return True

    @staticmethod
    def _set_topic_style_dict(topic_dict: dict, updates: dict) -> None:
        """把已解析好的样式属性直接写入 topic dict（批量操作内部使用）。"""
        if not updates:
            return
        topic_style = topic_dict.get("style")
        if not isinstance(topic_style, dict):
            topic_style = {}
            topic_dict["style"] = topic_style
        props = topic_style.get("properties")
        if not isinstance(props, dict):
            props = {}
            topic_style["properties"] = props
        props.update(updates)

    def set_sheet_layout(
        self,
        sheet_index: int = 0,
        layout: str = "思维导图",
    ) -> bool:
        """切换工作表根主题的布局结构，支持中文名（逻辑图/括号图/时间轴等）。

        表格类布局（矩阵图/电子表格）除了 structureClass，还必须写
        org.xmind.ui.spreadsheet 的列定义扩展，否则 XMind 只会渲染成
        普通分支；切回非表格布局时要把旧扩展清掉，避免残留。
        """
        if sheet_index < 0 or sheet_index >= len(self.xm.raw_content):
            return False
        root = self.xm.raw_content[sheet_index].get("rootTopic", {})
        if not root:
            return False
        resolved = resolve(STRUCTURES, layout, label="布局结构")
        root["structureClass"] = resolved
        if resolved == "org.xmind.ui.spreadsheet":
            root["extensions"] = [{
                "provider": "org.xmind.ui.spreadsheet",
                "content": [{
                    "name": "columns",
                    "content": [
                        {"name": "column", "content": f"标签 {i}"}
                        for i in range(1, 6)
                    ] + [{"name": "leading-column", "content": ""}],
                }],
            }]
        else:
            root.pop("extensions", None)
        self.xm.refresh_from_raw()
        return True

    def add_sheet(self, title: str = "画布", root_title: str = "中心主题") -> int:
        """新增一张工作表，返回其索引。"""
        self.xm.raw_content.append(self._new_sheet(title, root_title))
        self.xm.refresh_from_raw()
        return len(self.xm.raw_content) - 1

    def rename_sheet(self, sheet_index: int, title: str) -> bool:
        """重命名工作表。"""
        if sheet_index < 0 or sheet_index >= len(self.xm.raw_content):
            return False
        self.xm.raw_content[sheet_index]["title"] = title
        self.xm.refresh_from_raw()
        return True

    def delete_sheet(self, sheet_index: int) -> bool:
        """删除工作表（至少保留一张）。"""
        if sheet_index < 0 or sheet_index >= len(self.xm.raw_content):
            return False
        if len(self.xm.raw_content) <= 1:
            return False
        del self.xm.raw_content[sheet_index]
        self.xm.refresh_from_raw()
        return True

    def move_sheet(self, sheet_index: int, new_index: int) -> bool:
        """移动工作表顺序（new_index 为最终位置）。"""
        count = len(self.xm.raw_content)
        if sheet_index < 0 or sheet_index >= count or new_index < 0 or new_index >= count:
            return False
        sheet = self.xm.raw_content.pop(sheet_index)
        self.xm.raw_content.insert(new_index, sheet)
        self.xm.refresh_from_raw()
        return True

    def set_branch_style(
        self,
        node_id: str,
        *,
        line_type: Optional[str] = None,
        arrow_end: Optional[str] = None,
        line_color: Optional[str] = None,
        line_width: Optional[str] = None,
        line_pattern: Optional[str] = None,
    ) -> bool:
        """设置某个节点的分支连线样式（line-class / arrow-end-class 等）。"""
        topic_dict, _, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        # 先校验全部参数再写入，保证原子性
        updates: dict[str, str] = {}
        if line_type is not None:
            updates["line-class"] = resolve(BRANCH_LINES, line_type, label="分支类型")
        if arrow_end is not None:
            updates["arrow-end-class"] = resolve(ARROW_ENDS, arrow_end, label="终点样式")
        if line_color is not None:
            updates["line-color"] = normalize_color(line_color)
        if line_width is not None:
            updates["line-width"] = resolve_width(line_width, label="线宽")
        if line_pattern is not None:
            updates["line-pattern"] = resolve(BORDER_PATTERNS, line_pattern, label="线条线型")
        if updates:
            style = topic_dict.get("style")
            if not isinstance(style, dict):
                style = {}
                topic_dict["style"] = style
            props = style.get("properties")
            if not isinstance(props, dict):
                props = {}
                style["properties"] = props
            props.update(updates)

        self.xm.refresh_from_raw()
        return True

    def set_theme_style(
        self,
        sheet_index: int = 0,
        level: str = "分支主题",
        **style: object,
    ) -> bool:
        """设置主题级样式：中心主题/分支主题/第三层级主题/细分主题/自由主题的默认样式。

        与 set_node_style 的参数一致（不含 importance）。影响整个层级的所有节点。
        """
        if sheet_index < 0 or sheet_index >= len(self.xm.raw_content):
            return False
        # 先校验参数与主题层级，任何错误都不产生结构/样式修改
        key = resolve(THEME_LEVELS, level, label="主题层级")
        updates = _prepare_style_updates(style)

        sheet = self.xm.raw_content[sheet_index]
        theme = sheet.get("theme")
        if not isinstance(theme, dict):
            theme = _new_theme()
            sheet["theme"] = theme

        entry = theme.get(key)
        if not isinstance(entry, dict):
            entry = {"id": str(uuid.uuid4())}
            theme[key] = entry
        props = entry.get("properties")
        if not isinstance(props, dict):
            props = {}
            entry["properties"] = props

        props.update(updates)
        self.xm.refresh_from_raw()
        return True

    @staticmethod
    def _attached_index(parent_dict: dict, topic_id: str) -> Optional[int]:
        """返回 topic_id 在父节点 attached 子节点中的索引。"""
        attached = parent_dict.get("children", {}).get("attached", [])
        for i, child in enumerate(attached):
            if isinstance(child, dict) and child.get("id") == topic_id:
                return i
        return None

    def add_summary(
        self,
        parent_id: str,
        start_id: str,
        end_id: str,
        title: str = "概要",
    ) -> TopicNode:
        """给父节点的连续子节点区间加一个概要（summary）。"""
        parent_dict, _, parent_kind = _find_topic_and_parent(self.xm.raw_content, parent_id)
        if parent_dict is None:
            raise ValueError(f"找不到父节点: {parent_id}")
        if parent_kind in ("summary", "callout"):
            raise ValueError("不能给概要或批注节点添加概要")

        start_index = self._attached_index(parent_dict, start_id)
        end_index = self._attached_index(parent_dict, end_id)
        if start_index is None or end_index is None:
            raise ValueError("概要区间端点必须是父节点的直属子节点")
        if start_index > end_index:
            start_index, end_index = end_index, start_index

        summary_id = str(uuid.uuid4())
        summary_topic = {
            "id": summary_id,
            "title": title,
        }
        children = parent_dict.get("children")
        if not isinstance(children, dict):
            children = {}
            parent_dict["children"] = children
        summary_list = children.setdefault("summary", [])
        summary_list.append(summary_topic)

        summaries = parent_dict.setdefault("summaries", [])
        summaries.append({
            "id": str(uuid.uuid4()),
            "range": f"({start_index},{end_index})",
            "topicId": summary_id,
        })

        self.xm.refresh_from_raw()
        new_node = self.xm.find_topic(summary_id)
        if new_node is None:
            raise RuntimeError("新增概要节点后刷新解析树失败")
        return new_node

    def add_boundary(
        self,
        parent_id: str,
        start_id: str,
        end_id: str,
        title: str = "",
    ) -> bool:
        """给父节点的连续子节点区间加一个边界（boundary）。"""
        parent_dict, _, parent_kind = _find_topic_and_parent(self.xm.raw_content, parent_id)
        if parent_dict is None:
            raise ValueError(f"找不到父节点: {parent_id}")
        if parent_kind in ("summary", "callout"):
            raise ValueError("不能给概要或批注节点添加边界")

        start_index = self._attached_index(parent_dict, start_id)
        end_index = self._attached_index(parent_dict, end_id)
        if start_index is None or end_index is None:
            raise ValueError("边界区间端点必须是父节点的直属子节点")
        if start_index > end_index:
            start_index, end_index = end_index, start_index

        entry: dict = {
            "id": str(uuid.uuid4()),
            "range": f"({start_index},{end_index})",
        }
        if title:
            entry["title"] = title
        boundaries = parent_dict.setdefault("boundaries", [])
        boundaries.append(entry)

        self.xm.refresh_from_raw()
        return True

    def add_relationship(
        self,
        sheet_index: int,
        start_id: str,
        end_id: str,
        title: str = "",
    ) -> bool:
        """在指定工作表里加一条关系线（relationship）。"""
        if sheet_index < 0 or sheet_index >= len(self.xm.raw_content):
            return False
        topic_ids = {n.id for n in self.xm.all_topics()}
        if start_id not in topic_ids or end_id not in topic_ids:
            raise ValueError("关系线端点必须存在于文件中")

        sheet = self.xm.raw_content[sheet_index]
        relationships = sheet.setdefault("relationships", [])
        entry: dict = {
            "id": str(uuid.uuid4()),
            "end1Id": start_id,
            "end2Id": end_id,
        }
        if title:
            entry["title"] = title
        relationships.append(entry)
        self.xm.refresh_from_raw()
        return True

    def remove_relationship(self, relationship_id: str) -> bool:
        """按 ID 删除一条关系线。"""
        for sheet in self.xm.raw_content:
            relationships = sheet.get("relationships")
            if isinstance(relationships, list):
                new_rels = [r for r in relationships if not (isinstance(r, dict) and r.get("id") == relationship_id)]
                if len(new_rels) != len(relationships):
                    sheet["relationships"] = new_rels
                    if not new_rels:
                        sheet.pop("relationships", None)
                    self.xm.refresh_from_raw()
                    return True
        return False

    def remove_boundary(self, boundary_id: str) -> bool:
        """按 ID 删除一个边界。"""
        def _walk(topic: dict) -> bool:
            boundaries = topic.get("boundaries")
            if isinstance(boundaries, list):
                new_list = [b for b in boundaries if not (isinstance(b, dict) and b.get("id") == boundary_id)]
                if len(new_list) != len(boundaries):
                    topic["boundaries"] = new_list
                    if not new_list:
                        topic.pop("boundaries", None)
                    return True
            children = topic.get("children", {})
            if isinstance(children, dict):
                for key in ("attached", "detached", "summary", "callout"):
                    for child in children.get(key, []):
                        if isinstance(child, dict) and _walk(child):
                            return True
            return False

        for sheet in self.xm.raw_content:
            root = sheet.get("rootTopic")
            if isinstance(root, dict) and _walk(root):
                self.xm.refresh_from_raw()
                return True
        return False

    def apply_style_preset(self, preset: str) -> int:
        """按预设给整张图套用配色：根主题/分支/叶子分别应用。返回修改的节点数。"""
        if preset not in STYLE_PRESETS:
            raise ValueError(f"未知样式预设: {preset}，可选: {', '.join(STYLE_PRESETS)}")
        colors = STYLE_PRESETS[preset]
        count = 0
        for sheet in self.xm.raw_content:
            root = sheet.get("rootTopic") if isinstance(sheet, dict) else None
            if isinstance(root, dict):
                count += self._apply_preset_to_tree(root, colors, 0)
        # 只刷新一次解析树，避免每节点全树查找 + 全量刷新（O(n²)）
        self.xm.refresh_from_raw()
        return count

    def _apply_preset_to_tree(self, topic: dict, colors: dict, level: int) -> int:
        """递归按层级套用预设（直接改 raw dict，避免每节点全树查找）。"""
        if level == 0:
            style = colors["root"]
        elif level == 1:
            style = colors["branch"]
        else:
            style = colors["leaf"]
        self._set_topic_style_dict(topic, _prepare_style_updates(style))
        count = 1
        children = topic.get("children", {})
        if isinstance(children, dict):
            for key in ("attached", "detached", "summary", "callout"):
                for child in children.get(key, []):
                    if isinstance(child, dict):
                        count += self._apply_preset_to_tree(child, colors, level + 1)
        return count

    def add_image(self, node_id: str, image_path: Union[str, Path]) -> tuple[bool, str]:
        """给节点插入图片（复制进 xmind 包的 resources/ 并更新引用）。

        Returns:
            (是否成功, 图片引用 src 或错误说明)
        """
        topic_dict, _, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False, f"找不到节点: {node_id}"

        image_path = Path(image_path)
        if not image_path.exists():
            return False, f"图片文件不存在: {image_path}"
        ext = image_path.suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
            return False, f"不支持的图片格式: {ext or '（无扩展名）'}"

        data = image_path.read_bytes()
        if not data:
            return False, "图片文件为空"
        name = hashlib.sha256(data).hexdigest() + ext
        self._extra_files[f"resources/{name}"] = data
        src = f"xap:resources/{name}"
        topic_dict["image"] = {"src": src}
        self.xm.refresh_from_raw()
        return True, src

    def set_hyperlink(self, node_id: str, url: str, title: str = "") -> bool:
        """给节点设置超链接。url 为空则移除超链接。"""
        topic_dict, _, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
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
        topic_dict, _, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        self._add_marker_to_topic(topic_dict, marker_id)
        self.xm.refresh_from_raw()
        return True

    @staticmethod
    def _add_marker_to_topic(topic_dict: dict, marker_id: str) -> None:
        """直接给 topic dict 加标记（批量操作内部使用，不刷新解析树）。"""
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

    def remove_marker(self, node_id: str, marker_id: str) -> bool:
        """移除节点的指定标记图标。"""
        topic_dict, _, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        self._remove_marker_from_topic(topic_dict, marker_id)
        self.xm.refresh_from_raw()
        return True

    @staticmethod
    def _remove_marker_from_topic(topic_dict: dict, marker_id: str) -> None:
        """直接移除 topic dict 上的标记（批量操作内部使用，不刷新解析树）。"""
        markers = topic_dict.get("markers")
        if isinstance(markers, dict) and isinstance(markers.get("markerId"), list):
            markers["markerId"] = [m for m in markers["markerId"] if m != marker_id]
            if not markers["markerId"]:
                topic_dict.pop("markers", None)

    def add_label(self, node_id: str, label: str) -> bool:
        """给节点添加一个标签。"""
        topic_dict, _, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        self._add_label_to_topic(topic_dict, label)
        self.xm.refresh_from_raw()
        return True

    @staticmethod
    def _add_label_to_topic(topic_dict: dict, label: str) -> None:
        """直接给 topic dict 加标签（批量操作内部使用，不刷新解析树）。"""
        labels = topic_dict.get("labels")
        if not isinstance(labels, list):
            labels = []
            topic_dict["labels"] = labels
        if label not in labels:
            labels.append(label)

    def remove_label(self, node_id: str, label: str) -> bool:
        """移除节点的一个标签。"""
        topic_dict, _, _ = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            return False

        self._remove_label_from_topic(topic_dict, label)
        self.xm.refresh_from_raw()
        return True

    @staticmethod
    def _remove_label_from_topic(topic_dict: dict, label: str) -> None:
        """直接移除 topic dict 上的标签（批量操作内部使用，不刷新解析树）。"""
        labels = topic_dict.get("labels")
        if isinstance(labels, list):
            topic_dict["labels"] = [l for l in labels if l != label]
            if not topic_dict["labels"]:
                topic_dict.pop("labels", None)

    def save(self, output_path: Optional[Union[str, Path]] = None) -> str:
        """保存文件。output_path 为 None 时覆盖原文件。

        保存时会自动补全 XMind 打开文件所必需的组成部分
        （metadata.json、content.xml、cache.json，并更新 manifest.json），
        因此旧版本生成的残缺文件在下次保存时也能被修复。

        Returns:
            实际保存的路径
        """
        if output_path is None:
            output_path = self.xm.path
        output_path = str(output_path)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        original_path = self.xm.path
        overwriting = os.path.abspath(output_path) == os.path.abspath(original_path)
        if overwriting and Path(original_path).exists():
            _backup_file(original_path)

        # 序列化修改后的 content.json
        new_content = json.dumps(self.xm.raw_content, ensure_ascii=False, indent=2)

        # 读取原始 zip，复制所有文件，替换 content.json，并补全必需文件。
        # 原始内容不是合法 zip（repair 重建场景）时按空白文件处理。
        source_zip = io.BytesIO(self.xm._zip_bytes or b"")
        try:
            zin = zipfile.ZipFile(source_zip, "r")
        except zipfile.BadZipFile:
            zin = None

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
            written: set[str] = set()
            manifest: dict = {}
            if zin is not None:
                with zin:
                    for item in zin.infolist():
                        if item.filename in self._exclude_files:
                            # 用户主动移除的附件/资源，不再写入
                            continue
                        if item.filename == "content.json":
                            zout.writestr("content.json", new_content)
                            written.add("content.json")
                        elif item.filename == "manifest.json":
                            try:
                                manifest = json.loads(zin.read(item.filename))
                            except (json.JSONDecodeError, KeyError):
                                manifest = {}
                        else:
                            zout.writestr(item, zin.read(item.filename))
                            written.add(item.filename)

            # 补全 XMind 必需文件（已存在的原样保留，例如 XMind 自己写的 content.xml）
            required = (
                (
                    "metadata.json",
                    json.dumps(DEFAULT_METADATA, ensure_ascii=False, indent=2),
                ),
                ("cache.json", "{}"),
                ("content.xml", _content_xml_stub(self.xm.raw_content)),
            )
            for name, data in required:
                if name not in written:
                    zout.writestr(name, data)
                    written.add(name)

            # 原 zip 缺 content.json 时补上（repair 重建场景），
            # 保证保存结果始终是能打开的 XMind 文件
            if "content.json" not in written:
                zout.writestr("content.json", new_content)
                written.add("content.json")

            # 新增资源（如插入的图片）
            for name, data in self._extra_files.items():
                if name not in written:
                    zout.writestr(name, data)
                    written.add(name)

            # manifest 登记所有文件（目录项不登记）
            entries = manifest.setdefault("file-entries", {})
            for name in written:
                if name != "manifest.json" and not name.endswith("/"):
                    entries.setdefault(name, {})
            zout.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

        # 更新内部缓存
        with open(output_path, "rb") as f:
            self.xm._zip_bytes = f.read()
        # 关键：只有覆盖原文件时才更新 path。
        # 如果另存到新路径而把 path 改掉，缓存里原路径的 writer 会指向新文件，
        # 用户后续“保存原文件”会静默写到别处。
        if overwriting:
            self.xm.path = output_path

        return output_path

    @staticmethod
    def _insert_topic(
        attached_list: list[dict],
        topic: dict,
        before_id: Optional[str],
        after_id: Optional[str],
    ) -> None:
        """把 topic 插入 attached 列表的指定位置（默认追加到末尾）。"""
        if before_id is not None:
            for i, existing in enumerate(attached_list):
                if existing.get("id") == before_id:
                    attached_list.insert(i, topic)
                    return
            raise ValueError(f"找不到 before_id 指定的节点: {before_id}")
        if after_id is not None:
            for i, existing in enumerate(attached_list):
                if existing.get("id") == after_id:
                    attached_list.insert(i + 1, topic)
                    return
            raise ValueError(f"找不到 after_id 指定的节点: {after_id}")
        attached_list.append(topic)

    def apply_matching(
        self,
        keyword: str,
        *,
        style: Optional[dict] = None,
        add_markers: Optional[list[str]] = None,
        remove_markers: Optional[list[str]] = None,
        add_labels: Optional[list[str]] = None,
        remove_labels: Optional[list[str]] = None,
    ) -> list[str]:
        """按标题关键词批量编辑节点，返回命中的节点 ID 列表。

        style 的键与 set_node_style 的参数名一致（font_color、font_size、
        font_family、font_weight、font_style、background_color、border_color、
        text_align）。
        """
        keyword_lower = keyword.lower()
        # 样式参数先整体校验一次，任何非法值都在修改前抛出，
        # 保证批量操作不会“改到一半才失败”。
        style_updates = _prepare_style_updates(style) if style else {}
        matched_ids: list[str] = []

        def _walk(topic: dict) -> None:
            title = topic.get("title") or ""
            if keyword_lower in title.lower():
                topic_id = topic.get("id")
                if topic_id:
                    matched_ids.append(topic_id)
                if style_updates:
                    self._set_topic_style_dict(topic, style_updates)
                for marker in add_markers or []:
                    self._add_marker_to_topic(topic, marker)
                for marker in remove_markers or []:
                    self._remove_marker_from_topic(topic, marker)
                for label in add_labels or []:
                    self._add_label_to_topic(topic, label)
                for label in remove_labels or []:
                    self._remove_label_from_topic(topic, label)
            children = topic.get("children", {})
            if isinstance(children, dict):
                for key in ("attached", "detached", "summary", "callout"):
                    for child in children.get(key, []):
                        if isinstance(child, dict):
                            _walk(child)

        for sheet in self.xm.raw_content:
            root = sheet.get("rootTopic") if isinstance(sheet, dict) else None
            if isinstance(root, dict):
                _walk(root)
        if matched_ids:
            # 只刷新一次解析树，避免每节点全树查找 + 全量刷新（O(n²)）
            self.xm.refresh_from_raw()
        return matched_ids

    def batch_update_titles(
        self,
        find: str,
        replace: str,
        use_regex: bool = False,
    ) -> list[tuple[str, str, str]]:
        """按文本或正则批量替换节点标题，返回 (id, 旧标题, 新标题)。"""
        if not find:
            raise ValueError("find 不能为空")
        pattern = None
        if use_regex:
            try:
                pattern = re.compile(find)
            except re.error as e:
                raise ValueError(f"正则表达式无效: {e}") from e

        changes: list[tuple[str, str, str]] = []

        def _walk(topic: dict) -> None:
            title = topic.get("title")
            if isinstance(title, str):
                if pattern is not None:
                    new_title = pattern.sub(replace, title)
                else:
                    new_title = title.replace(find, replace)
                if new_title != title:
                    topic["title"] = new_title
                    changes.append((topic.get("id", ""), title, new_title))
            children = topic.get("children", {})
            if isinstance(children, dict):
                for key in ("attached", "detached", "summary", "callout"):
                    for child in children.get(key, []):
                        if isinstance(child, dict):
                            _walk(child)

        for sheet in self.xm.raw_content:
            root = sheet.get("rootTopic") if isinstance(sheet, dict) else None
            if isinstance(root, dict):
                _walk(root)
        if changes:
            self.xm.refresh_from_raw()
        return changes

    def delete_empty_nodes(self) -> list[str]:
        """删除所有空标题的普通节点（含其子树），跳过根/概要/批注。返回被删 ID。"""
        deleted: list[str] = []
        removed_ids: set[str] = set()
        affected: dict[int, tuple[dict, list[str]]] = {}

        def _collect(topic: dict) -> None:
            children = topic.get("children", {})
            if not isinstance(children, dict):
                return
            for key in ("attached", "detached", "summary", "callout"):
                for child in children.get(key, []):
                    if not isinstance(child, dict):
                        continue
                    if key in ("attached", "detached") and not (child.get("title") or "").strip():
                        if child.get("id"):
                            deleted.append(child["id"])
                    else:
                        _collect(child)

        for sheet in self.xm.raw_content:
            root = sheet.get("rootTopic") if isinstance(sheet, dict) else None
            if isinstance(root, dict):
                _collect(root)

        for topic_id in deleted:
            topic_dict, parent_dict, _ = _find_topic_and_parent(self.xm.raw_content, topic_id)
            if topic_dict is None or parent_dict is None:
                continue
            parent_key = id(parent_dict)
            if parent_key not in affected:
                affected[parent_key] = (parent_dict, _attached_ids(parent_dict))
            removed_ids |= topic_ids_in_subtree(topic_dict)
            _remove_topic_from_parent(parent_dict, topic_id)

        if removed_ids:
            _cleanup_dangling_refs(self.xm.raw_content, removed_ids)
            for parent_dict, old_ids in affected.values():
                _remap_visual_ranges(parent_dict, old_ids)
            self.xm.refresh_from_raw()
        return deleted

    def merge_duplicate_nodes(self) -> list[tuple[str, str]]:
        """合并整张图中标题相同的兄弟节点（保留第一个，子节点并入）。

        返回 [(保留节点 ID, 被合并节点 ID), ...]。
        """
        merged: list[tuple[str, str]] = []
        removed_ids: set[str] = set()
        affected: dict[int, tuple[dict, list[str]]] = {}

        def _walk(topic: dict) -> None:
            nonlocal removed_ids
            children = topic.get("children", {})
            if isinstance(children, dict):
                attached = children.get("attached", [])
                groups: dict[str, list[dict]] = {}
                for child in attached:
                    if isinstance(child, dict):
                        key = (child.get("title") or "").strip()
                        if key:
                            groups.setdefault(key, []).append(child)
                for group in groups.values():
                    if len(group) <= 1:
                        continue
                    keep = group[0]
                    parent_key = id(topic)
                    if parent_key not in affected:
                        affected[parent_key] = (topic, _attached_ids(topic))
                    for dup in group[1:]:
                        dup_id = dup.get("id")
                        if not dup_id:
                            continue
                        dup_children = dup.get("children", {})
                        if isinstance(dup_children, dict):
                            keep_children = keep.get("children")
                            if not isinstance(keep_children, dict):
                                keep_children = {}
                                keep["children"] = keep_children
                            for key in ("attached", "detached", "summary", "callout"):
                                items = dup_children.get(key, [])
                                if items:
                                    keep_children.setdefault(key, []).extend(items)
                        removed_ids |= topic_ids_in_subtree(dup)
                        _remove_topic_from_parent(topic, dup_id)
                        merged.append((keep.get("id", ""), dup_id))
                for key in ("attached", "detached", "summary", "callout"):
                    for child in children.get(key, []):
                        if isinstance(child, dict):
                            _walk(child)

        for sheet in self.xm.raw_content:
            root = sheet.get("rootTopic") if isinstance(sheet, dict) else None
            if isinstance(root, dict):
                _walk(root)

        if removed_ids:
            _cleanup_dangling_refs(self.xm.raw_content, removed_ids)
            for parent_dict, old_ids in affected.values():
                _remap_visual_ranges(parent_dict, old_ids)
            self.xm.refresh_from_raw()
        return merged

    def notes_to_children(self, node_id: str, keep_notes: bool = False) -> int:
        """把节点备注的每一行转成一个子节点。默认转换后清空备注。"""
        topic_dict, _, kind = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            raise ValueError(f"找不到节点: {node_id}")
        if kind in ("summary", "callout"):
            raise ValueError("不能给概要或批注节点转换备注")
        notes = _raw_notes_text(topic_dict)
        lines = [line.strip() for line in notes.splitlines() if line.strip()]
        if not lines:
            raise ValueError("该节点没有可转换的备注文本")

        attached = _ensure_children_attached(topic_dict)
        old_ids = _attached_ids(topic_dict)
        for line in lines:
            attached.append({
                "id": str(uuid.uuid4()),
                "title": line,
                "class": "topic",
                "children": {"attached": [], "detached": []},
            })
        _remap_visual_ranges(topic_dict, old_ids)
        if not keep_notes:
            topic_dict.pop("notes", None)
        self.xm.refresh_from_raw()
        return len(lines)

    def children_to_notes(self, node_id: str, keep_children: bool = False) -> int:
        """把节点的直接子节点标题合并成备注。默认转换后删除子节点。"""
        topic_dict, _, kind = _find_topic_and_parent(self.xm.raw_content, node_id)
        if topic_dict is None:
            raise ValueError(f"找不到节点: {node_id}")
        if kind in ("summary", "callout"):
            raise ValueError("不能给概要或批注节点转换子节点")
        children = topic_dict.get("children", {})
        attached = children.get("attached", []) if isinstance(children, dict) else []
        titles = [
            child.get("title")
            for child in attached
            if isinstance(child, dict) and (child.get("title") or "").strip()
        ]
        if not titles:
            raise ValueError("该节点没有可转换的子节点")

        _set_notes(topic_dict, "\n".join(titles))
        if not keep_children:
            old_ids = _attached_ids(topic_dict)
            removed_ids: set[str] = set()
            for child in attached:
                if isinstance(child, dict):
                    removed_ids |= topic_ids_in_subtree(child)
            if isinstance(children, dict):
                children["attached"] = []
            _cleanup_dangling_refs(self.xm.raw_content, removed_ids)
            _remap_visual_ranges(topic_dict, old_ids)
        self.xm.refresh_from_raw()
        return len(titles)

    def auto_number(self, mode: str = "decimal") -> int:
        """给节点自动编号。

        mode="decimal"：每个父节点下按 1. 2. 3. … 编号；
        mode="hierarchical"：按 1. / 1.1. / 1.1.1. … 层级编号。
        已有编号会被替换（避免出现 “1. 1. 标题”）。只编普通子节点。
        """
        if mode not in ("decimal", "hierarchical"):
            raise ValueError("mode 只支持 decimal 或 hierarchical")
        count = 0

        def _strip_number(title: str) -> str:
            stripped = re.sub(r"^\s*\d+(\.\d+)*\s*[\.、)）]?\s*", "", title).strip()
            return stripped or title

        def _walk(topic: dict, parent_num: str = "") -> None:
            nonlocal count
            children = topic.get("children", {})
            if not isinstance(children, dict):
                return
            attached = children.get("attached", [])
            for index, child in enumerate(attached):
                if not isinstance(child, dict):
                    continue
                title = child.get("title") or ""
                if mode == "decimal":
                    child["title"] = f"{index + 1}. {_strip_number(title)}"
                    num = ""
                else:
                    num = f"{index + 1}" if not parent_num else f"{parent_num}.{index + 1}"
                    child["title"] = f"{num}. {_strip_number(title)}"
                count += 1
                _walk(child, num)
            for key in ("detached", "summary", "callout"):
                for child in children.get(key, []):
                    if isinstance(child, dict):
                        _walk(child, "")

        for sheet in self.xm.raw_content:
            root = sheet.get("rootTopic") if isinstance(sheet, dict) else None
            if isinstance(root, dict):
                _walk(root)
        if count:
            self.xm.refresh_from_raw()
        return count

    def remove_unused_attachments(self) -> list[str]:
        """找出 zip 中未被任何节点引用的附件/图片，保存时移除。返回移除列表。"""
        referenced: set[str] = set()

        def _walk(topic: dict) -> None:
            image = topic.get("image")
            if isinstance(image, dict):
                src = image.get("src") or ""
                if src.startswith("xap:"):
                    referenced.add(src[4:])
            children = topic.get("children", {})
            if isinstance(children, dict):
                for key in ("attached", "detached", "summary", "callout"):
                    for child in children.get(key, []):
                        if isinstance(child, dict):
                            _walk(child)

        for sheet in self.xm.raw_content:
            root = sheet.get("rootTopic") if isinstance(sheet, dict) else None
            if isinstance(root, dict):
                _walk(root)

        unused: list[str] = []
        if self.xm._zip_bytes:
            try:
                with zipfile.ZipFile(io.BytesIO(self.xm._zip_bytes)) as zf:
                    for name in zf.namelist():
                        if name.endswith("/"):
                            continue
                        if name.startswith(("attachments/", "resources/")) and name not in referenced:
                            unused.append(name)
            except zipfile.BadZipFile:
                pass
        self._exclude_files.update(unused)
        return unused

    @classmethod
    def create(
        cls,
        output_path: Union[str, Path],
        root_title: str,
        *,
        sheet_title: str = "画布 1",
        topics: Optional[list[dict]] = None,
        root_meta: Optional[dict] = None,
    ) -> "XMindWriter":
        """从零创建一个新的 XMind 文件。

        topics 为嵌套结构：[{"title": "...", "notes": "...", "children": [...]}]

        新文件会包含 XMind 打开所必需的全部组成部分：
        content.json / metadata.json / content.xml / cache.json / manifest.json，
        并带上默认主题、revisionId 等字段，确保 XMind 能正常打开。
        """
        output_path = str(output_path)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        def _build_topic(item: dict) -> dict:
            topic = {
                "id": str(uuid.uuid4()),
                "title": item.get("title", ""),
                "class": "topic",
                "titleUnedited": True,
                "children": {"attached": [], "detached": []},
            }
            notes = item.get("notes")
            if notes:
                topic["notes"] = {"plain": {"content": notes}}
                if item.get("notesDisplay"):
                    topic["notesDisplay"] = item["notesDisplay"]
                    topic["notes"]["realHTML"] = {"content": _notes_real_html(notes)}
            labels = item.get("labels")
            if labels:
                topic["labels"] = list(labels)
            style = item.get("style")
            if isinstance(style, dict) and style.get("properties"):
                topic["style"] = copy.deepcopy(style)
            if item.get("structureClass"):
                topic["structureClass"] = item["structureClass"]
            if item.get("extensions"):
                topic["extensions"] = copy.deepcopy(item["extensions"])
            children = item.get("children") or []
            topic["children"]["attached"] = [_build_topic(c) for c in children]
            return topic

        sheet = cls._new_sheet(
            sheet_title,
            root_title,
            [_build_topic(t) for t in (topics or [])],
        )
        if root_meta:
            # 根主题自身可能带表格结构/扩展/显式备注/加粗等元数据
            root_topic = sheet["rootTopic"]
            for key in ("structureClass", "extensions", "notesDisplay", "style"):
                if root_meta.get(key):
                    root_topic[key] = copy.deepcopy(root_meta[key])
        content = [sheet]

        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "content.json",
                json.dumps(content, ensure_ascii=False, indent=2),
            )
            zf.writestr(
                "metadata.json",
                json.dumps(DEFAULT_METADATA, ensure_ascii=False, indent=2),
            )
            zf.writestr("cache.json", "{}")
            zf.writestr("content.xml", _content_xml_stub(content))
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "file-entries": {
                            "content.json": {},
                            "metadata.json": {},
                            "cache.json": {},
                            "content.xml": {},
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        # 覆盖已有文件前先备份，和 save() 的行为保持一致
        if Path(output_path).exists():
            _backup_file(output_path)
        with open(output_path, "wb") as f:
            f.write(zip_bytes.getvalue())

        xm = XMindFile(
            path=output_path,
            sheets=[],
            raw_content=content,
            zip_namelist=[
                "content.json",
                "metadata.json",
                "cache.json",
                "content.xml",
                "manifest.json",
            ],
            _zip_bytes=zip_bytes.getvalue(),
        )
        xm.refresh_from_raw()
        return cls(xm)

    @classmethod
    def repair(
        cls,
        input_path: Union[str, Path],
        output_path: Optional[Union[str, Path]] = None,
    ) -> "XMindWriter":
        """修复打不开的 XMind 文件。

        - content.json 可解析：补全 metadata.json / content.xml / theme / revisionId，
          不改变已有主题内容。
        - content.json 缺失或损坏：重建一个空白画布，并保留 zip 里的
          附件/标记等资源，让文件至少能被 XMind 打开。
        output_path 为 None 时原地覆盖（保存前会自动备份原文件）。
        """
        try:
            xm = parse_xmind(input_path)
        except (ValueError, json.JSONDecodeError, zipfile.BadZipFile, KeyError):
            # 原文件无法解析：重建最小可打开的内容，保留其余 zip 资源。
            with open(input_path, "rb") as f:
                zip_bytes = f.read()
            root_id = str(uuid.uuid4())
            sheet_id = str(uuid.uuid4())
            root = {
                "id": root_id,
                "title": "未命名",
                "class": "topic",
                "titleUnedited": True,
                "structureClass": "org.xmind.ui.map.clockwise",
                "children": {"attached": [], "detached": []},
            }
            content = [{
                "id": sheet_id,
                "revisionId": str(uuid.uuid4()),
                "class": "sheet",
                "title": "已修复的画布",
                "rootTopic": root,
                "relationships": [],
                "topicOverlapping": "overlap",
                "arrangeableLayerOrder": [root_id],
                "zones": [],
                "extensions": [{
                    "provider": "org.xmind.ui.skeleton.structure.style",
                    "content": {"centralTopic": "org.xmind.ui.map.clockwise"},
                }],
                "theme": _new_theme(),
            }]
            xm = XMindFile(
                path=str(input_path),
                sheets=[],
                raw_content=content,
                zip_namelist=[],
                _zip_bytes=zip_bytes,
            )
            xm.refresh_from_raw()
        else:
            for sheet in xm.raw_content:
                if not isinstance(sheet, dict):
                    continue
                sheet.setdefault("revisionId", str(uuid.uuid4()))
                sheet.setdefault("theme", _new_theme())
                root = sheet.get("rootTopic")
                if isinstance(root, dict):
                    root.setdefault("titleUnedited", True)
                    root.setdefault("structureClass", "org.xmind.ui.map.clockwise")
                    _ensure_topic_class(root)
        writer = cls(xm)
        target = str(output_path) if output_path is not None else xm.path
        writer.save(target)
        return writer
