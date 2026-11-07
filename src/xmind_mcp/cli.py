"""命令行入口。

用法：
  xmind-mcp              # 启动 MCP Server
  xmind-mcp --version    # 打印版本号
  xmind-mcp --info       # 查看当前配置
  xmind-mcp --toolset read   # 临时只加载读工具
  xmind-mcp --uninstall  # 清理配置与旧版 OCR 残留（可选卸载本包）
  xmind-mcp --uninstall --yes    # 全自动，不再逐项确认
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import CONFIG_DIR, CONFIG_FILE, BACKUP_DIR, Config


def main() -> int:
    _fix_console_encoding()
    parser = argparse.ArgumentParser(
        prog="xmind-mcp",
        description="XMind MCP Server — 让大模型读写 XMind 文件",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="打印版本号",
    )
    parser.add_argument("--info", action="store_true", help="打印当前配置信息")
    parser.add_argument(
        "--toolset",
        help="只加载指定工具组（read/edit/style/export/maintenance，可逗号组合；默认 all）",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="清理配置与旧版 OCR 残留，可选择同时卸载本包",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="全自动：跳过所有确认（含卸载本包）",
    )
    args = parser.parse_args()

    config = Config.load()
    if args.toolset:
        # 命令行临时覆盖，不写回配置文件
        config.toolset = args.toolset

    if args.uninstall:
        return run_uninstall(yes=args.yes)

    if args.info:
        return print_info(config)

    from .server import run_server

    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        print("\n服务已停止。", file=sys.stderr)
    return 0


def _fix_console_encoding() -> None:
    """中文 Windows（GBK 控制台/管道）下保证 emoji 等符号可正常输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _is_interactive() -> bool:
    """判断当前是否有可读的交互终端。

    MCP 客户端通过 stdio 启动 server 时 stdin 被协议占用，不是 TTY；
    此时绝不能阻塞读 stdin。
    """
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _confirm(question: str, *, default: bool = False) -> bool:
    """交互式确认；非交互环境直接返回 default。"""
    if not _is_interactive():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix}: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


# 旧版（0.4 及以前）安装过的 OCR / 推理依赖。只用于卸载时的迁移清理，
# 当前版本不再需要它们。
_LEGACY_OCR_PACKAGES = [
    "rapidocr-onnxruntime",
    "paddleocr",
    "paddlepaddle",
    "paddlepaddle-gpu",
    "onnxruntime",
    "onnxruntime-gpu",
    "onnxruntime-directml",
    "nvidia-cuda-runtime-cu12",
    "nvidia-cudnn-cu12",
    "nvidia-cublas-cu12",
    "nvidia-cudart-cu12",
]

_LEGACY_OCR_CACHE_DIRS = [
    Path.home() / ".cache" / "rapidocr",
    Path.home() / ".cache" / "rapidocr_onnxruntime",
    Path.home() / ".paddleocr",
]

# 卸载本包时可一并清理的直接依赖（都是通用库，可能被其他项目使用）
_PACKAGE_DIRECT_DEPS = ["mcp", "Pillow", "matplotlib"]


def _installed_packages() -> set[str]:
    """探测当前 Python 环境已安装的包名（pip list --format=freeze）。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=freeze"],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    names = set()
    for line in result.stdout.splitlines():
        name = line.split("==")[0].strip().replace("_", "-").lower()
        if name:
            names.add(name)
    return names


def _find_server_pids() -> list[int]:
    """查找命令行中包含 xmind_mcp 的进程（排除自身）。"""
    own = os.getpid()
    pids: list[int] = []
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'xmind_mcp' } | Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.isdigit() and int(line) != own:
                    pids.append(int(line))
        else:
            result = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and "xmind_mcp" in parts[1]:
                    pid = int(parts[0])
                    if pid != own:
                        pids.append(pid)
    except Exception:
        return []
    return list(dict.fromkeys(pids))


def _stop_running_server(yes: bool) -> None:
    """卸载前停止正在运行的 XMind MCP Server，避免数据目录被占用。"""
    pids = _find_server_pids()
    if not pids:
        return
    print(f"⚠️  检测到可能正在运行的 XMind MCP Server 进程: {', '.join(str(p) for p in pids)}")
    if not yes and not _confirm("是否先停止这些进程？"):
        print("   跳过停止进程。")
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=15)
                else:
                    subprocess.run(["kill", "-9", str(pid)],
                                   capture_output=True, timeout=15)
            except Exception:
                pass
    time.sleep(1.5)


def _pip_uninstall(packages: list[str]) -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", *packages],
            check=False, timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def run_uninstall(yes: bool = False) -> int:
    """清理配置和旧版 OCR 残留；备份目录永远保留。"""
    print("🧹 开始卸载 XMind MCP Server ...\n")
    failures: list[str] = []
    installed = _installed_packages()

    # 1. 旧版 OCR / 运行时依赖：可能被其他项目共用，必须确认后再删
    legacy = [
        p for p in _LEGACY_OCR_PACKAGES
        if p.replace("_", "-").lower() in installed
    ]
    if legacy:
        print(f"检测到旧版 OCR/运行时依赖: {', '.join(legacy)}")
        print("这些是通用库，可能被其他项目使用。")
        if yes or _confirm("是否卸载它们？"):
            print(f"⬇️  卸载: {', '.join(legacy)}")
            if not _pip_uninstall(legacy):
                failures.append("旧版 OCR/运行时依赖卸载未完全成功")
                print("   ⚠️ 部分依赖卸载失败，可手动执行: pip uninstall -y " + " ".join(legacy))
        else:
            print("   已保留。")
    else:
        print("（当前环境没有旧版 OCR 依赖残留）")

    # 2. 停止可能正在运行的服务，避免文件被占用
    _stop_running_server(yes)

    # 3. 删除配置文件；备份是用户数据，始终保留
    if CONFIG_FILE.exists():
        try:
            CONFIG_FILE.unlink()
            print(f"🗑️  已删除配置文件: {CONFIG_FILE}")
        except OSError as exc:
            failures.append(f"配置文件删除失败: {CONFIG_FILE}")
            print(f"   ⚠️ 删除失败: {exc}")
    else:
        print("（未发现配置文件）")
    if BACKUP_DIR.exists():
        count = len(list(BACKUP_DIR.glob("*.xmind")))
        print(f"📦 保留自动备份 {count} 份: {BACKUP_DIR}")
        print("   如不再需要，可手动删除该目录。")
    try:
        CONFIG_DIR.rmdir()  # 只在目录已空（没有备份）时成功
    except OSError:
        pass

    # 4. 删除旧版 OCR 模型缓存（不在 ~/.xmind-mcp 内）
    for cache_dir in _LEGACY_OCR_CACHE_DIRS:
        if not cache_dir.exists():
            continue
        print(f"🗑️  发现旧版 OCR 模型缓存: {cache_dir}")
        if yes or _confirm("是否删除该缓存？"):
            try:
                shutil.rmtree(cache_dir)
                print("   ✅ 已删除")
            except OSError as exc:
                failures.append(f"OCR 模型缓存删除失败: {cache_dir}")
                print(f"   ⚠️ 删除失败: {exc}")

    # 5. 可选卸载本包及其直接依赖
    remove_pkg = yes or _confirm(
        "\n是否同时卸载本包 xmind-mcp-server 及其直接依赖"
        f"（{'、'.join(_PACKAGE_DIRECT_DEPS)}）？"
    )
    if remove_pkg:
        remove = ["xmind-mcp-server"] + [
            p for p in _PACKAGE_DIRECT_DEPS
            if p.replace("_", "-").lower() in installed
        ]
        print(f"⬇️  卸载本包及直接依赖: {', '.join(remove)}")
        if not _pip_uninstall(remove):
            failures.append("本包/直接依赖卸载失败")
            print("   ⚠️ 卸载失败，可手动执行: pip uninstall -y " + " ".join(remove))
    else:
        print("   保留本包（如需完全卸载：pip uninstall xmind-mcp-server）")

    print()
    if failures:
        print("⚠️ 部分步骤未成功：")
        for failure in failures:
            print(f"  - {failure}")
        print("请按上方提示手动处理。")
        return 1
    print("🎉 卸载完成。" if remove_pkg else "🎉 清理完成（已保留本包）。")
    return 0


def print_info(config: Config) -> int:
    """打印当前配置信息。"""
    print("=== XMind MCP Server ===")
    print(f"  版本:       {__version__}")
    print(f"  配置文件:   {CONFIG_FILE}")
    print(f"  图片模式:   {config.image_mode}（inline=原图交给模型，off=只给文字）")
    print(f"  图片长边:   {config.image_max_side}px")
    print(f"  工具组:     {config.toolset}")
    roots = ", ".join(config.allowed_roots) if config.allowed_roots else "不限制"
    print(f"  允许目录:   {roots}")
    print(f"  备份目录:   {BACKUP_DIR}")
    return 0
