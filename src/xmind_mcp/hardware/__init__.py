"""硬件检测和依赖安装。"""

from .detector import HardwareInfo, GPUInfo, detect_hardware
from .installer import (
    CUDA_RUNTIME_PACKAGES,
    choose_packages,
    cleanup_onnxruntime_conflicts,
    cuda_runtime_available,
    include_cuda_runtime_paths,
    install_packages,
    uninstall_packages,
    verify_installed,
)

__all__ = [
    "HardwareInfo",
    "GPUInfo",
    "detect_hardware",
    "choose_packages",
    "install_packages",
    "uninstall_packages",
    "verify_installed",
    "cleanup_onnxruntime_conflicts",
    "cuda_runtime_available",
    "include_cuda_runtime_paths",
    "CUDA_RUNTIME_PACKAGES",
]
