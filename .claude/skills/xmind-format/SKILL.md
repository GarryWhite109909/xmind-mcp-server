---
name: xmind-format
description: XMind（JSON 格式）文件结构、安全读写规则与官方校验方法。改动 content.json、节点树、主题/样式、概要/边界/关系线、附件或保存逻辑时使用。
---

# XMind 文件格式与安全写回

## 先看事实

`.xmind` 是 ZIP，新版（XMind Zen / 2020+）以 `content.json` 为准：

```text
content.json       主题树（唯一真相）
metadata.json      dataStructureVersion / layoutEngineVersion / creator
manifest.json      file-entries 清单
content.xml        旧版兼容占位（XMind 26 仍会带）
cache.json         缓存占位
attachments/       图片/附件（可被 xap: 引用）
resources/         本工具插入图片时使用
Thumbnails/        保存时生成的整图缩略图
```

图片引用：`"src": "xap:resources/xxx.png"` → zip 内 `resources/xxx.png`。
也兼容 `data:image/...;base64,...`。

节点树：

```json
{
  "id": "sheet-id",
  "title": "工作表",
  "rootTopic": {
    "id": "root",
    "title": "中心主题",
    "children": {
      "attached": [],
      "detached": [],
      "summary": [],
      "callout": []
    },
    "notes": {"text": "..."},
    "markers": [{"markerId": "priority-1"}],
    "labels": ["标签"],
    "image": {"src": "xap:resources/x.png"},
    "style": {"id": "...", "properties": {...}},
    "summaries": [{"id": "...", "range": "(0,2)", "topicId": "..."}],
    "boundaries": [{"id": "...", "range": "(0,1)"}]
  },
  "relationships": [{"end1Id": "...", "end2Id": "..."}],
  "theme": {...}
}
```

## 读写流程

读：`parse_xmind(path)` → `XMindFile`（`sheets` / `raw_content` / `_topic_index`）。
改：只改 `writer.xm.raw_content` 里的 dict，然后调用
`self.xm.refresh_from_raw()` 重建节点树和 ID 索引。
存：`writer.save(output_path)`；不要自己写 zip。

`save()` 已经负责：

- 覆盖前备份到 `~/.xmind-mcp/backups/`（每个文件保留 20 份）；
- 磁盘摘要与 `xm.source_digest` 不一致时抛 `FileChangedError`；
- 移除过期 `Thumbnails/thumbnail.png`；
- 补 `metadata.json` / `content.xml` / `cache.json`，同步 `manifest.json`；
- 临时文件 + `os.replace` 原子替换。

## 官方校验硬规则

非根主题不能带 `class: "topic"`；每个 `style` 必须有 `id`；
`markers` 必须是 `[{"markerId": "..."}]`，不能是字符串数组。
这些规则由 `tests/test_golden.py` 和官方 CLI 共同守住。

如果本机装有 XMind 官方 CLI：

```bash
xmind auth status
xmind validate path/to/file.xmind
```

CLI 要求登录，`validate` 也不例外；没有登录时先 `xmind auth login cn`
（或 `global`），回调等待约 180 秒。

## 结构变更的连锁影响

- `children.attached` 顺序变化（插入 / 删除 / 移动）会让
  `summaries` / `boundaries` 的字符串区间 `"(i,j)"` 错位：
  必须先记录旧 ID 列表，再调用 `_remap_visual_ranges(parent, old_ids)`。
- 删除子树时用 `topic_ids_in_subtree()`，再 `_cleanup_dangling_refs()`
  清理 relationships / summaries / boundaries。
- 移动/复制节点前用 `_is_descendant()` 阻止移到自己或后代下。
- `summary` / `callout` 不是普通主题，不能挂子节点、不能移动/复制。

## 验证清单

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest -q tests/test_golden.py
```

再用 XMind 打开生成文件确认：能打开、主题树完整、样式/附件/概要/边界/
关系线没有错位。必要时跑 `xmind validate`。

## 不要做

- 不要直接改 `XMindFile.sheets` 而不改 `raw_content`。
- 不要绕过 `refresh_from_raw()`，否则 `find_topic` 索引是旧的。
- 不要把图片原样塞进 content.json；走 `writer.add_image()`。
- 不要重新引入 OCR / 推理依赖。
