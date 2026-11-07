---
name: release
description: 发布 xmind-mcp-server 前的版本、文档、构建与产物检查清单。准备 PyPI 发布或打 tag 时使用。
---

# 发布检查清单

发布是对外动作：**没有用户明确同意，不执行 `twine upload`、不推送 tag、不改远端。**

## 1. 版本

- `pyproject.toml` 与 `src/xmind_mcp/__init__.py` 两处版本号必须一致。
- 语义化版本：删除功能 / 不兼容变更升 minor（例如移除 OCR → 0.5.0）；
  纯修复升 patch。
- 同步 README 里的工具总数与 schema 体积估算。

测量工具 schema 体积：

```bash
.venv/Scripts/python.exe -c "import json,sys;sys.path.insert(0,'src');from xmind_mcp.tool_schemas import build_tool_list;print(len(json.dumps([t.model_dump(by_alias=True,exclude_none=True) for t in build_tool_list()],ensure_ascii=False).encode('utf-8')))"
```

## 2. 质量门禁

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m compileall -q src
```

生成三类产物并用官方 CLI 校验（需要 `xmind auth login`）：

```bash
xmind validate /path/to/created.xmind
xmind validate /path/to/imported.xmind
xmind validate /path/to/modified.xmind
```

要求都是 `0 errors, 0 warnings`。结构约定见
`.claude/skills/xmind-format/SKILL.md`。

## 3. 构建

```bash
# 需要先安装构建工具（安装依赖前先征得同意）
pip install build twine
python -m build
twine check dist/*
```

检查 wheel 内容：

```bash
python -m zipfile -l dist/xmind_mcp_server-<version>-py3-none-any.whl
```

确认：

- 没有 `xmind_mcp/ocr/`、`xmind_mcp/hardware/`；
- 包含 `default_theme.json`；
- 依赖只有 `mcp`、`Pillow`、`matplotlib`。

可选：在干净临时环境安装 wheel，跑 `xmind-mcp --info` 和一次
`tools/list` 握手（`tests/test_server.py` 已覆盖后者）。

## 4. 发布

```bash
twine upload dist/*                      # TestPyPI 先验证可加 --repository testpypi
git tag -a v<version> -m "v<version>"
git push origin v<version>
```

以上命令都要用户明确同意后再执行。发布后在 README / release notes 里
写清破坏性变更（例如：OCR 工具已移除，图片改走 `get_image` / `read_all`）。
