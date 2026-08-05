"""命令行入口。

用法：
  xmind-mcp              # 启动 MCP Server（首次自动检测+安装）
  xmind-mcp --setup      # 重新检测硬件并配置
  xmind-mcp --info       # 打印当前硬件和配置信息
  xmind-mcp --engine paddleocr   # 切换 OCR 引擎
  xmind-mcp --uninstall  # 一键卸载（依赖 + 模型 + 配置 + 可选本体）
  xmind-mcp --uninstall --yes    # 全自动卸载（含本体，跳过确认）
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
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .config import Config, CONFIG_FILE, CONFIG_DIR
from .hardware import (
    CUDA_RUNTIME_PACKAGES,
    detect_hardware,
    choose_packages,
    cuda_runtime_available,
    install_packages,
    uninstall_packages,
    verify_installed,
    cleanup_onnxruntime_conflicts,
)
from .hardware.detector import has_nvidia_gpu


def main() -> int:
    _fix_console_encoding()
    parser = argparse.ArgumentParser(
        prog="xmind-mcp",
        description="XMind MCP Server — 让大模型读写 XMind 文件",
    )
    parser.add_argument("--setup", action="store_true", help="重新检测硬件并配置 OCR")
    parser.add_argument("--info", action="store_true", help="打印当前硬件和配置信息")
    parser.add_argument(
        "--engine",
        choices=["rapidocr", "paddleocr"],
        help="指定 OCR 引擎（默认 rapidocr）",
    )
    parser.add_argument(
        "--device",
        choices=["cuda", "directml", "cpu"],
        help="强制指定推理设备（默认自动检测）",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="一键卸载：卸载 OCR 依赖、删除模型缓存和配置，可选择同时卸载本包",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="全自动：跳过所有确认（含卸载本包）",
    )
    args = parser.parse_args()

    config = Config.load()

    # --uninstall：清理所有依赖和本地数据
    if args.uninstall:
        return run_uninstall(config, yes=args.yes)

    # --info：只打印信息，不启动
    if args.info:
        return print_info(config)

    # --setup 或未初始化：走安装流程
    if args.setup or not config.is_ready:
        engine = args.engine or "rapidocr"
        if not run_setup(config, engine=engine, force_device=args.device):
            return 1

    # 验证安装
    ok, msg = verify_installed(config.ocr_engine, config.device)
    if not ok:
        print(f"⚠️  OCR 引擎验证失败: {msg}", file=sys.stderr)
        # 设备不可用（如 CUDA 库缺失）但引擎本身能跑时，自动回退 CPU 并修正配置，
        # 避免 MCP 客户端因为配置过时而无法启动。
        fallback_ok, fallback_msg = verify_installed(config.ocr_engine, "cpu")
        if fallback_ok:
            print(f"⚠️  设备配置不可用，已自动回退 CPU: {fallback_msg}", file=sys.stderr)
            config.device = "cpu"
            config.save()
            msg = fallback_msg
        else:
            print("请运行 xmind-mcp --setup 重新配置。", file=sys.stderr)
            return 1
    print(f"✅ OCR 就绪: {msg}", file=sys.stderr)

    # 启动 MCP Server
    from .server import run_server
    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        print("\n服务已停止。", file=sys.stderr)
    return 0


def _is_interactive() -> bool:
    """判断当前是否运行在交互式终端（有 TTY 可读输入）。

    MCP 客户端通过 stdio 启动 server 时，stdin 被 MCP 协议占用，
    不是 TTY。此时绝不能阻塞读 stdin，否则会读到协议数据或卡死。
    """
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _ask_use_gpu() -> bool:
    """询问用户是否用 GPU 跑 OCR（仅针对检测到 NVIDIA 显卡但未装 CUDA 的情况）。

    同意则自动安装 CUDA 运行库 + onnxruntime-gpu；不同意则用 CPU。
    非交互环境（如 MCP 客户端启动）下自动跳过，回退 CPU。
    """
    if not _is_interactive():
        return False
    print(
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "检测到 NVIDIA 显卡，但未安装 CUDA 运行库。\n"
        "是否将 OCR 运行在 GPU？\n"
        "  • 是：自动安装 CUDA 运行库 + onnxruntime-gpu（约几百 MB）\n"
        "  • 否：使用 CPU 运行（更简单，OCR 速度对笔记场景也够用）\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "是否启用 GPU 加速？(y/n)",
        file=sys.stderr,
    )
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def run_setup(config: Config, engine: str = "rapidocr", force_device: str = None) -> bool:
    """首次配置：检测硬件 → 选包 → 安装 → 写配置。"""
    print("🔍 正在检测硬件...", file=sys.stderr)
    hw = detect_hardware()

    print(f"   操作系统: {hw.os}", file=sys.stderr)
    print(f"   CPU 核心: {hw.cpu_count}", file=sys.stderr)
    print(f"   内存: {hw.ram_gb} GB", file=sys.stderr)
    for g in hw.gpus:
        vram_str = f", 显存: {g.vram_mb}MB" if g.vram_mb else ""
        print(f"   显卡: {g.name} ({g.vendor}{vram_str})", file=sys.stderr)

    # 选择包
    device, packages = choose_packages(hw, engine=engine)
    if (
        engine == "rapidocr"
        and not force_device
        and has_nvidia_gpu(hw)
        and device == "cpu"
        and not cuda_runtime_available()
    ):
        # 检测到 NVIDIA 显卡但缺 CUDA 运行库：询问用户是否临时安装 CUDA 走 GPU。
        # 同意则装 nvidia CUDA 运行库 + onnxruntime-gpu；不同意则直接用 CPU。
        if _ask_use_gpu():
            print(
                "📦 选择 GPU 加速：将安装 CUDA 运行库 + onnxruntime-gpu（约几百 MB）...",
                file=sys.stderr,
            )
            device = "cuda"
            packages = ["rapidocr-onnxruntime", "onnxruntime-gpu"] + CUDA_RUNTIME_PACKAGES
        else:
            print("已选择 CPU 运行（无需额外安装，OCR 速度对笔记场景也够用）。", file=sys.stderr)
    if force_device:
        device = force_device
        # 根据 device 调整 packages
        if engine == "rapidocr":
            ort_pkg = {
                "cuda": "onnxruntime-gpu",
                "directml": "onnxruntime-directml",
                "cpu": "onnxruntime",
            }.get(device, "onnxruntime")
            packages = ["rapidocr-onnxruntime", ort_pkg]
            if device == "cuda":
                # 显式指定 CUDA 时也一并安装 pip 版 CUDA 运行库
                packages += CUDA_RUNTIME_PACKAGES
        elif device == "cuda":
            packages = ["paddleocr", "paddlepaddle-gpu"]
        else:
            packages = ["paddleocr", "paddlepaddle"]

    print(f"\n📦 OCR 引擎: {engine}", file=sys.stderr)
    print(f"   推理设备: {device}", file=sys.stderr)
    print(f"   待安装包: {', '.join(packages)}", file=sys.stderr)

    # onnxruntime 互斥处理
    if engine == "rapidocr":
        cleanup_onnxruntime_conflicts(device)

    # 安装
    print("\n⬇️  正在安装依赖（可能需要几分钟）...", file=sys.stderr)
    if not install_packages(packages):
        print("❌ 安装失败，请检查网络或手动安装。", file=sys.stderr)
        print(f"   手动安装: pip install {' '.join(packages)}", file=sys.stderr)
        return False

    # 验证
    ok, msg = verify_installed(engine, device)
    if not ok:
        # 自动回退：GPU 依赖装好但用不了（缺 CUDA 运行库等）时，
        # 自动改装 CPU 版并继续，让不同配置的用户都能直接用起来。
        fallback_packages: Optional[list[str]] = None
        if engine == "rapidocr" and device != "cpu":
            print("⚠️  GPU 验证失败，正在自动回退 CPU ...", file=sys.stderr)
            cleanup_onnxruntime_conflicts("cpu")
            fallback_packages = ["rapidocr-onnxruntime", "onnxruntime"]
        elif engine == "paddleocr" and device == "cuda":
            print("⚠️  CUDA 版 Paddle 验证失败，正在自动回退 CPU ...", file=sys.stderr)
            fallback_packages = ["paddleocr", "paddlepaddle"]

        if fallback_packages and install_packages(fallback_packages):
            fallback_ok, fallback_msg = verify_installed(engine, "cpu")
            if fallback_ok:
                device = "cpu"
                packages = fallback_packages
                ok = True
                msg = fallback_msg
        if not ok:
            print(f"❌ 安装后验证失败: {msg}", file=sys.stderr)
            return False

    # 写配置
    config.ocr_engine = engine
    config.device = device
    config.gpus = [asdict(g) if hasattr(g, '__dataclass_fields__') else g.__dict__ for g in hw.gpus]
    config.cpu_count = hw.cpu_count
    config.ram_gb = hw.ram_gb
    config.os = hw.os
    config.initialized = True
    config.installed_packages = packages
    config.save()

    print(f"\n✅ 配置完成！{msg}", file=sys.stderr)
    print(f"   配置文件: {CONFIG_FILE}", file=sys.stderr)
    return True


def _fix_console_encoding() -> None:
    """中文 Windows（GBK 控制台/管道）下保证 emoji 等符号可正常输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# 卸载时按固定名单探测，不依赖配置文件（配置缺失/损坏/旧版本也能卸干净）
_OCR_DEP_PATTERNS = [
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

# 卸载本包时一并清理的直接依赖（与 OCR 无关，确认后会提示可能影响其他项目）
_PACKAGE_DIRECT_DEPS = ["mcp", "psutil", "Pillow"]

# OCR 模型默认缓存目录（RapidOCR / PaddleOCR）
_OCR_CACHE_DIRS = [
    Path.home() / ".cache" / "rapidocr",
    Path.home() / ".cache" / "rapidocr_onnxruntime",
    Path.home() / ".paddleocr",
]


def _installed_packages() -> set[str]:
    """探测当前 Python 环境已安装的包名（pip list --format=freeze）。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=freeze"],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    names = set()
    for line in r.stdout.splitlines():
        name = line.split("==")[0].strip().replace("_", "-").lower()
        if name:
            names.add(name)
    return names


def _find_server_pids() -> list[int]:
    """查找命令行中包含 xmind_mcp 的进程（排除自身），用于卸载前停止服务。"""
    own = os.getpid()
    pids: list[int] = []
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'xmind_mcp' } | Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=30,
            )
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.isdigit() and int(line) != own:
                    pids.append(int(line))
        else:
            r = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                capture_output=True, text=True, timeout=30,
            )
            for line in r.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and "xmind_mcp" in parts[1]:
                    pid = int(parts[0])
                    if pid != own:
                        pids.append(pid)
    except Exception:
        return []
    return list(dict.fromkeys(pids))


def _stop_running_server(yes: bool) -> None:
    """卸载前尝试停止正在运行的 XMind MCP Server（避免数据目录被占用）。"""
    pids = _find_server_pids()
    if not pids:
        return
    print(f"⚠️  检测到可能正在运行的 XMind MCP Server 进程: {', '.join(str(p) for p in pids)}")
    if not yes:
        try:
            ans = input("是否先停止这些进程？[y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("   跳过停止进程，继续尝试删除数据目录（可能因占用失败）...")
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


def run_uninstall(config: Config, yes: bool = False) -> int:
    """一键卸载：卸载依赖 + 停止服务 + 删除本地数据/模型缓存 + 可选卸载本包。"""
    print("🧹 开始卸载 XMind MCP Server ...\n")
    failures: list[str] = []
    installed = _installed_packages()

    # 1. 卸载 OCR / 运行时依赖：配置文件记录的 + 固定名单中已安装的
    pkgs: list[str] = []
    for p in list(config.installed_packages) + _OCR_DEP_PATTERNS:
        key = p.replace("_", "-").lower()
        if key in installed and p not in pkgs:
            pkgs.append(p)

    if pkgs:
        print(f"⬇️  卸载 OCR/运行时依赖: {', '.join(pkgs)}")
        if uninstall_packages(pkgs):
            print("   ✅ 依赖已卸载")
        else:
            failures.append("OCR/运行时依赖卸载未完全成功")
            print("   ⚠️ 部分依赖卸载失败，可手动执行: pip uninstall -y " + " ".join(pkgs))
    else:
        print("   （当前环境未检测到已安装的 OCR/运行时依赖）")

    # 2. 停止可能正在运行的服务，避免数据目录被占用
    _stop_running_server(yes)

    # 3. 删除配置 / OCR 缓存 / 自动备份
    if CONFIG_DIR.exists():
        print(f"🗑️  删除本地数据目录: {CONFIG_DIR}")
        try:
            shutil.rmtree(CONFIG_DIR)
            print("   ✅ 已删除（含配置、OCR 缓存、自动备份）")
        except Exception as e:
            failures.append(f"本地数据目录删除失败: {CONFIG_DIR}")
            print(f"   ⚠️ 删除失败: {e}，请手动删除 {CONFIG_DIR}")
    else:
        print("   （未发现本地数据目录）")

    # 4. 删除 OCR 模型缓存（RapidOCR / PaddleOCR 默认目录，在 ~/.xmind-mcp 之外）
    for cache_dir in _OCR_CACHE_DIRS:
        if cache_dir.exists():
            print(f"🗑️  删除 OCR 模型缓存: {cache_dir}")
            try:
                shutil.rmtree(cache_dir)
                print("   ✅ 已删除")
            except Exception as e:
                failures.append(f"OCR 模型缓存删除失败: {cache_dir}")
                print(f"   ⚠️ 删除失败: {e}，请手动删除 {cache_dir}")

    # 5. 可选卸载本包及其直接依赖
    keep_pkg = False
    if yes:
        answer = "y"
    else:
        print("\n是否同时卸载本包 xmind-mcp-server 及其直接依赖（mcp、psutil、Pillow）？(y/n)")
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = "n"
    if answer in ("y", "yes"):
        remove_pkgs = ["xmind-mcp-server"]
        remove_pkgs += [
            p for p in _PACKAGE_DIRECT_DEPS
            if p.replace("_", "-").lower() in installed
        ]
        print(f"⬇️  卸载本包及直接依赖: {', '.join(remove_pkgs)}")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", *remove_pkgs],
                check=False, timeout=300,
            )
            if r.returncode == 0:
                print("   ✅ 已卸载")
            else:
                failures.append("本包/直接依赖卸载失败")
                print("   ⚠️ 卸载失败，可手动执行: pip uninstall -y " + " ".join(remove_pkgs))
        except Exception as e:
            failures.append("本包/直接依赖卸载异常")
            print(f"   ⚠️ 本包卸载失败: {e}")
    else:
        keep_pkg = True
        print("   保留本包（如需完全卸载，可运行: pip uninstall xmind-mcp-server）")

    # 6. 汇总
    print()
    if failures:
        print("⚠️ 部分步骤未成功：")
        for f in failures:
            print(f"  - {f}")
        print("请按上方提示手动处理。")
        return 1
    if keep_pkg:
        print("🎉 卸载完成（已保留本包；如需完全卸载：pip uninstall xmind-mcp-server）")
    else:
        print("🎉 卸载完成，所有内容已清理。")
    return 0


def print_info(config: Config) -> int:
    """打印当前配置信息。"""
    if not config.is_ready:
        print("⚠️ 尚未初始化，请先运行: xmind-mcp --setup")
        return 1

    print("=== XMind MCP Server 配置 ===")
    print(f"  操作系统:   {config.os}")
    print(f"  OCR 引擎:   {config.ocr_engine}")
    print(f"  推理设备:   {config.device}")
    print(f"  CPU 核心:   {config.cpu_count}")
    print(f"  内存:       {config.ram_gb} GB")
    for g in config.gpus:
        vram = f", 显存: {g.get('vram_mb', 0)}MB" if g.get("vram_mb") else ""
        print(f"  显卡:       {g.get('name', '?')} ({g.get('vendor', '?')}{vram})")
    print(f"  配置文件:   {CONFIG_FILE}")
    print(f"  已安装包:   {', '.join(config.installed_packages)}")

    ok, msg = verify_installed(config.ocr_engine, config.device)
    print(f"  验证状态:   {'✅ ' + msg if ok else '❌ ' + msg}")
    return 0 if ok else 1
