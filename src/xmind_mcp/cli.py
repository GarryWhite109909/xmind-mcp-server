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
import sys
from dataclasses import asdict

from .config import Config, CONFIG_FILE
from .hardware import (
    detect_hardware,
    choose_packages,
    install_packages,
    verify_installed,
    cleanup_onnxruntime_conflicts,
)


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
        choices=["cuda", "directml", "openvino", "rocm", "cpu"],
        help="强制指定推理设备（默认自动检测）",
    )
    args = parser.parse_args()

    config = Config.load()

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
        print(f"❌ 安装后验证失败: {msg}", file=sys.stderr)
        return False

    # 写配置
    config.ocr_engine = engine
    config.device = device
    config.gpus = [asdict(g) if hasattr(g, '__dataclass_fields__') else g.__dict__ for g in hw.gpus]
    config.cpu_count = hw.cpu_count
    config.ram_gb = hw.ram_gb
    config.os = hw.os
    config.model_cache_dir = str(__import__("pathlib").Path.home() / ".xmind-mcp" / "models")
    config.initialized = True
    config.installed_packages = packages
    config.save()

    print(f"\n✅ 配置完成！{msg}", file=sys.stderr)
    print(f"   配置文件: {CONFIG_FILE}", file=sys.stderr)
    return True


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
