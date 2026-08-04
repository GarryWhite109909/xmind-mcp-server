"""命令行入口。

用法：
  xmind-mcp              # 启动 MCP Server（首次自动检测+安装）
  xmind-mcp --setup      # 重新检测硬件并配置
  xmind-mcp --info       # 打印当前硬件和配置信息
  xmind-mcp --engine paddleocr   # 切换 OCR 引擎
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from dataclasses import asdict
from typing import Optional

from .config import Config, CONFIG_FILE, CONFIG_DIR
from .hardware import (
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
    args = parser.parse_args()

    config = Config.load()

    # --uninstall：清理所有依赖和本地数据
    if args.uninstall:
        return run_uninstall(config)

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
        print(
            "⚠️  检测到 NVIDIA 显卡，但没找到 CUDA 运行库（cudart64*.dll），"
            "将直接使用 CPU。\n"
            "    如需 GPU 加速，请先安装与 onnxruntime 匹配的 CUDA，"
            "再运行 xmind-mcp --setup --device cuda。",
            file=sys.stderr,
        )
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


def run_uninstall(config: Config) -> int:
    """一键卸载：卸载 OCR 依赖 + 删除模型缓存 + 删除配置 + 可选卸载本包。"""
    print("🧹 开始卸载 XMind MCP Server ...\n")

    # 1. 卸载 OCR 依赖
    packages = list(config.installed_packages)
    # 额外清理可能存在的 onnxruntime 变体
    variants = ["onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"]
    for v in variants:
        if v not in packages:
            packages.append(v)
    if config.ocr_engine == "paddleocr":
        packages += ["paddlepaddle", "paddlepaddle-gpu"]
    pkgs = [p for p in packages if p]

    if pkgs:
        print(f"⬇️  卸载 OCR 依赖: {', '.join(pkgs)}")
        if uninstall_packages(pkgs):
            print("   ✅ 依赖已卸载")
        else:
            print("   ⚠️ 部分依赖卸载失败，可手动执行: pip uninstall -y " + " ".join(pkgs))
    else:
        print("   （未检测到已安装的 OCR 依赖）")

    # 2. 删除模型缓存和配置目录
    if CONFIG_DIR.exists():
        print(f"🗑️  删除本地数据目录: {CONFIG_DIR}")
        try:
            shutil.rmtree(CONFIG_DIR)
            print("   ✅ 已删除（含配置、OCR 缓存、自动备份）")
        except Exception as e:
            print(f"   ⚠️ 删除失败: {e}，请手动删除 {CONFIG_DIR}")

    # 3. 可选卸载本包
    print("\n是否同时卸载本包 xmind-mcp-server ？(y/n)")
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = "n"
    if answer in ("y", "yes"):
        print("⬇️  卸载本包 xmind-mcp-server ...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", "xmind-mcp-server"],
                check=False,
            )
            print("   ✅ 本包已卸载")
        except Exception as e:
            print(f"   ⚠️ 本包卸载失败: {e}")
    else:
        print("   保留本包（如需完全卸载，可运行: pip uninstall xmind-mcp-server）")

    print("\n🎉 卸载完成。")
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
