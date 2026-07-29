"""根据硬件检测结果选择并安装 OCR 依赖包。

策略（RapidOCR 为默认，PaddleOCR 可选）：

  engine=rapidocr:
    NVIDIA           → onnxruntime-gpu      (CUDA)
    AMD/Intel + Win  → onnxruntime-directml (DirectML)
    其他             → onnxruntime          (CPU)

  engine=paddleocr:
    NVIDIA           → paddlepaddle-gpu     (CUDA)
    其他             → paddlepaddle         (CPU)

注意 onnxruntime / onnxruntime-gpu / onnxruntime-directml 三者互斥，
切换时需先卸载旧的。
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

from .detector import HardwareInfo, has_nvidia_gpu, has_discrete_gpu


def choose_packages(
    hw: HardwareInfo,
    engine: str = "rapidocr",
) -> tuple[str, list[str]]:
    """根据硬件和引擎选择 pip 包。

    Returns:
        (device, packages) — device 是推理设备标识，packages 是要 pip install 的列表
    """
    nvidia = has_nvidia_gpu(hw)
    discrete = has_discrete_gpu(hw)

    if engine == "paddleocr":
        if nvidia:
            return "cuda", ["paddleocr", "paddlepaddle-gpu"]
        return "cpu", ["paddleocr", "paddlepaddle"]

    # 默认 rapidocr
    if nvidia:
        return "cuda", ["rapidocr-onnxruntime", "onnxruntime-gpu"]
    if discrete and hw.os == "windows":
        # AMD / Intel 独显 + Windows → DirectML
        return "directml", ["rapidocr-onnxruntime", "onnxruntime-directml"]
    # 兜底 CPU
    return "cpu", ["rapidocr-onnxruntime", "onnxruntime"]


def install_packages(packages: list[str]) -> bool:
    """pip install 指定包，返回是否成功。"""
    if not packages:
        return True
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    try:
        result = subprocess.run(cmd, capture_output=False, timeout=600)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def uninstall_packages(packages: list[str]) -> bool:
    """卸载包（用于切换引擎/设备时清理冲突包）。"""
    if not packages:
        return True
    cmd = [sys.executable, "-m", "pip", "uninstall", "-y", *packages]
    try:
        result = subprocess.run(cmd, capture_output=False, timeout=300)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# onnxruntime 三个互斥包，切换设备时需卸载冲突项
_ONNXRUNTIME_VARIANTS = ["onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"]


def cleanup_onnxruntime_conflicts(new_device: str) -> None:
    """安装新 onnxruntime 变体前，卸载冲突的旧变体。"""
    keep = {
        "cpu": "onnxruntime",
        "cuda": "onnxruntime-gpu",
        "directml": "onnxruntime-directml",
    }.get(new_device, "onnxruntime")

    conflicts = [p for p in _ONNXRUNTIME_VARIANTS if p != keep]
    uninstall_packages(conflicts)


def verify_installed(engine: str, device: str) -> tuple[bool, str]:
    """验证 OCR 引擎是否可用，返回 (是否可用, 说明)。"""
    if engine == "rapidocr":
        try:
            import rapidocr_onnxruntime  # noqa: F401
            import onnxruntime as ort  # noqa: F401
            providers = ort.get_available_providers()
            # 检查目标 provider 是否在列表里
            target = {
                "cuda": "CUDAExecutionProvider",
                "directml": "DmlExecutionProvider",
                "cpu": "CPUExecutionProvider",
            }.get(device, "CPUExecutionProvider")
            if target in providers:
                return True, f"rapidocr 可用，provider={target}"
            return True, f"rapidocr 已装但 {target} 不可用，实际 providers={providers}，将回退 CPU"
        except ImportError as e:
            return False, f"rapidocr 未安装: {e}"
    elif engine == "paddleocr":
        try:
            import paddleocr  # noqa: F401
            import paddle  # noqa: F401
            return True, "paddleocr 可用"
        except ImportError as e:
            return False, f"paddleocr 未安装: {e}"
    return False, f"未知引擎: {engine}"
