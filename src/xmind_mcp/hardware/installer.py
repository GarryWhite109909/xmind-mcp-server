"""根据硬件检测结果选择并安装 OCR 依赖包。

策略（RapidOCR 为默认，PaddleOCR 可选）：

  engine=rapidocr:
    NVIDIA           → onnxruntime-gpu      (CUDA)
    AMD/Intel        → onnxruntime          (CPU)
    其他             → onnxruntime          (CPU)

  engine=paddleocr:
    NVIDIA           → paddlepaddle-gpu     (CUDA)
    其他             → paddlepaddle         (CPU)

注意 onnxruntime / onnxruntime-gpu / onnxruntime-directml 三者互斥，
切换时需先卸载旧的。DirectML 需要 rapidocr-onnxruntime>=1.3.23，
且 AMD/Intel 独显加速收益不稳定，因此默认不推荐，可通过
`--setup --device directml` 显式开启。
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .detector import HardwareInfo, has_nvidia_gpu


def choose_packages(
    hw: HardwareInfo,
    engine: str = "rapidocr",
) -> tuple[str, list[str]]:
    """根据硬件和引擎选择 pip 包。

    Returns:
        (device, packages) — device 是推理设备标识，packages 是要 pip install 的列表
    """
    nvidia = has_nvidia_gpu(hw)

    if engine == "paddleocr":
        if nvidia:
            return "cuda", ["paddleocr", "paddlepaddle-gpu"]
        return "cpu", ["paddleocr", "paddlepaddle"]

    # 默认 rapidocr：有 NVIDIA 且能找到 CUDA 运行库才选 GPU，
    # 否则直接 CPU，避免给用户白下载几百 MB 的 onnxruntime-gpu。
    if nvidia and cuda_runtime_available():
        return "cuda", ["rapidocr-onnxruntime", "onnxruntime-gpu"]
    # 兜底 CPU
    return "cpu", ["rapidocr-onnxruntime", "onnxruntime"]


def cuda_runtime_available() -> bool:
    """粗略检测本机是否具备跑 onnxruntime-gpu 的 CUDA 运行库。

    onnxruntime-gpu 的 pip 包不捆绑 CUDA/cuDNN DLL，装完没有运行库也
    用不了 GPU，只会白白占用下载时间和磁盘。这里在装包前先查一下
    cudart64*.dll 是否存在。
    """
    if sys.platform != "win32":
        # Linux/macOS 走系统 CUDA，这里只做保守判断：能跑 nvidia-smi 就尝试 GPU
        return shutil.which("nvidia-smi") is not None

    search_dirs: list[Path] = []
    cuda_path = os.environ.get("CUDA_PATH") or os.environ.get("CUDA_HOME")
    if cuda_path:
        search_dirs.append(Path(cuda_path) / "bin")
    toolkit_root = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
    if toolkit_root.is_dir():
        search_dirs.extend(
            version_dir / "bin"
            for version_dir in toolkit_root.iterdir()
            if version_dir.is_dir()
        )
    for d in search_dirs:
        if d.is_dir() and any(d.glob("cudart64*.dll")):
            return True

    for path_entry in os.environ.get("PATH", "").split(os.pathsep):
        if path_entry and glob.glob(os.path.join(path_entry, "cudart64*.dll")):
            return True
    return False


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

# 通过 pip 安装的 CUDA 运行库（onnxruntime-gpu 依赖这些 DLL）：
# 相比下载官方 NVIDIA CUDA Toolkit 安装器，体积小、无需管理员权限、跨平台。
CUDA_RUNTIME_PACKAGES = [
    "nvidia-cuda-runtime-cu12",
    "nvidia-cudnn-cu12",
    "nvidia-cublas-cu12",
    "nvidia-cudart-cu12",
]


def cleanup_onnxruntime_conflicts(new_device: str) -> None:
    """安装新 onnxruntime 变体前，卸载冲突的旧变体。"""
    keep = {
        "cpu": "onnxruntime",
        "cuda": "onnxruntime-gpu",
        "directml": "onnxruntime-directml",
    }.get(new_device, "onnxruntime")

    conflicts = [p for p in _ONNXRUNTIME_VARIANTS if p != keep]
    uninstall_packages(conflicts)


def include_cuda_runtime_paths() -> None:
    """把 pip 安装的 nvidia CUDA 运行库加入 PATH。

    onnxruntime-gpu 通过 PATH 查找 cudart64_*.dll、cublas64_*.dll、cudnn*64_*.dll。
    而 nvidia-*-cu12 系列 pip 包把 DLL 装在 site-packages/nvidia/<lib>/bin，
    默认不在 PATH 上，会导致 onnxruntime 找不到 CUDA 库而静默回退 CPU。
    这里在加载 onnxruntime 前把对应 bin 目录注入 PATH。
    """
    cuda_bin_dirs: list[str] = []
    try:
        from importlib.metadata import distribution

        for dist_name in CUDA_RUNTIME_PACKAGES:
            try:
                d = distribution(dist_name)
            except Exception:
                continue
            bin_dir = Path(d.locate_file("")) / "bin"
            if bin_dir.is_dir():
                cuda_bin_dirs.append(str(bin_dir))
    except Exception:
        pass

    current_path = os.environ.get("PATH", "")
    for d in cuda_bin_dirs:
        if d not in current_path:
            os.environ["PATH"] = d + os.pathsep + current_path
            current_path = os.environ["PATH"]


def verify_installed(engine: str, device: str) -> tuple[bool, str]:
    """验证 OCR 引擎是否可用，返回 (是否可用, 说明)。"""
    if engine == "rapidocr":
        if device not in ("cuda", "directml", "cpu"):
            return False, f"rapidocr 不支持推理设备: {device}（仅支持 cuda/directml/cpu）"
        try:
            import rapidocr_onnxruntime  # noqa: F401
            import onnxruntime as ort  # noqa: F401
            providers = ort.get_available_providers()
            target = {
                "cuda": "CUDAExecutionProvider",
                "directml": "DmlExecutionProvider",
                "cpu": "CPUExecutionProvider",
            }.get(device, "CPUExecutionProvider")
            if target in providers:
                if device == "cuda" and ort.get_device() != "GPU":
                    return False, (
                        f"onnxruntime 检测到 CUDAExecutionProvider，但 get_device()="
                        f"{ort.get_device()}，实际无法使用 GPU。请运行 "
                        f"xmind-mcp --setup --device cpu。"
                    )
                if device == "directml":
                    if not _rapidocr_dml_ok():
                        return False, (
                            "DirectML 需要 rapidocr-onnxruntime>=1.3.23，"
                            "当前版本不支持。请运行 xmind-mcp --setup --device cpu。"
                        )
                    return True, "rapidocr 可用，provider=DmlExecutionProvider"
                return True, f"rapidocr 可用，provider={target}"
            return False, (
                f"rapidocr 已安装，但 {target} 不可用，实际 providers={providers}。"
                f"请运行 xmind-mcp --setup --device cpu。"
            )
        except ImportError as e:
            return False, f"rapidocr 未安装: {e}"
    elif engine == "paddleocr":
        if device not in ("cuda", "cpu"):
            return False, f"paddleocr 不支持推理设备: {device}（仅支持 cuda/cpu）"
        try:
            import paddleocr  # noqa: F401
            import paddle  # noqa: F401
            if device == "cuda":
                if not paddle.device.is_compiled_with_cuda():
                    return False, (
                        "paddlepaddle 不是 CUDA 版本，无法使用 GPU。"
                        "请运行 xmind-mcp --setup --device cpu，"
                        "或安装 paddlepaddle-gpu。"
                    )
                return True, "paddleocr 可用（CUDA）"
            return True, "paddleocr 可用"
        except ImportError as e:
            return False, f"paddleocr 未安装: {e}"
    return False, f"未知引擎: {engine}"


def _rapidocr_dml_ok() -> bool:
    """当前 rapidocr-onnxruntime 是否支持 DirectML（>=1.3.23）。"""
    try:
        from importlib.metadata import version

        raw = version("rapidocr-onnxruntime")
        parts = []
        for piece in raw.split(".")[:3]:
            digits = "".join(ch for ch in piece if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        while len(parts) < 3:
            parts.append(0)
        return parts >= [1, 3, 23]
    except Exception:
        return False
