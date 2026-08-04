"""测试硬件依赖选择逻辑。"""

import xmind_mcp.hardware.installer as installer
from xmind_mcp.hardware.detector import GPUInfo, HardwareInfo
from xmind_mcp.hardware.installer import choose_packages, cuda_runtime_available


def test_cuda_runtime_available_returns_bool():
    assert isinstance(cuda_runtime_available(), bool)


def test_choose_packages_nvidia_without_cuda_uses_cpu(monkeypatch):
    monkeypatch.setattr(installer, "cuda_runtime_available", lambda: False)
    hw = HardwareInfo(
        gpus=[GPUInfo(vendor="nvidia", name="NVIDIA RTX 3050")],
        os="windows",
    )
    device, packages = choose_packages(hw, engine="rapidocr")
    assert device == "cpu"
    assert "onnxruntime" in packages
    assert "onnxruntime-gpu" not in packages


def test_choose_packages_nvidia_with_cuda_uses_gpu(monkeypatch):
    monkeypatch.setattr(installer, "cuda_runtime_available", lambda: True)
    hw = HardwareInfo(
        gpus=[GPUInfo(vendor="nvidia", name="NVIDIA RTX 3050")],
        os="windows",
    )
    device, packages = choose_packages(hw, engine="rapidocr")
    assert device == "cuda"
    assert "onnxruntime-gpu" in packages


def test_choose_packages_no_gpu_uses_cpu():
    hw = HardwareInfo(gpus=[GPUInfo(vendor="unknown", name="Unknown")])
    device, packages = choose_packages(hw, engine="rapidocr")
    assert device == "cpu"
    assert "onnxruntime" in packages
