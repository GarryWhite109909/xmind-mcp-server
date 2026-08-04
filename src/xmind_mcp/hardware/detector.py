"""跨平台硬件检测：GPU 厂商/显存、CPU、内存。

策略：
- GPU 名称：调系统命令列出显卡，按关键词匹配厂商
- NVIDIA 显存：优先用 nvidia-smi（最准）
- 其他显卡显存：Windows 用 wmic（不一定准），Linux/macOS 标记 unknown
- 不依赖额外包，只用标准库 + psutil
"""

from __future__ import annotations

import re
import shutil
import subprocess
import platform
from dataclasses import dataclass, field


# ---------- 数据结构 ----------

@dataclass
class GPUInfo:
    vendor: str  # nvidia | amd | intel | unknown
    name: str
    vram_mb: int = 0  # 0 表示未知（集成显卡或检测失败）


@dataclass
class HardwareInfo:
    gpus: list[GPUInfo] = field(default_factory=list)
    cpu_count: int = 0
    ram_gb: float = 0.0
    os: str = ""  # windows | linux | macos


# ---------- 厂商识别 ----------

VENDOR_KEYWORDS = {
    "nvidia": [r"nvidia", r"geforce", r"quadro", r"tesla", r"rtx", r"gtx"],
    "amd": [r"amd", r"radeon", r"rx\s?\d", r"vega", r"instinct"],
    "intel": [r"intel", r"arc", r"iris", r"uhd", r"hd\s?graphics"],
}


def _match_vendor(name: str) -> str:
    name_lower = name.lower()
    for vendor, patterns in VENDOR_KEYWORDS.items():
        if any(re.search(p, name_lower) for p in patterns):
            return vendor
    return "unknown"


# ---------- 系统命令封装 ----------

def _run(cmd: list[str], timeout: int = 10) -> str:
    """运行系统命令，返回 stdout，失败返回空串。"""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def _run_shell(cmd: str, timeout: int = 10) -> str:
    """用 shell 运行命令（用于管道操作）。"""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=True,
        )
        return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


# ---------- GPU 名称检测 ----------

def _gpus_windows() -> list[str]:
    """Windows: 用 PowerShell 查显卡名称。"""
    out = _run([
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
    ])
    names = [line.strip() for line in out.splitlines() if line.strip()]
    if names:
        return names

    # PowerShell/WMI 不可用时回退到 wmic（旧系统）或注册表
    out = _run(["wmic", "path", "win32_VideoController", "get", "name", "/value"])
    names = [
        line.strip().split("=", 1)[-1].strip()
        for line in out.splitlines()
        if line.strip() and "=" in line
    ]
    if names:
        return names
    return _gpus_windows_registry()


def _gpus_windows_registry() -> list[str]:
    """最后回退：从显卡驱动注册表键读名称。"""
    try:
        import winreg
    except ImportError:
        return []

    names: list[str] = []
    key_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as parent:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(parent, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(parent, sub_name) as sub:
                        desc, _ = winreg.QueryValueEx(sub, "DriverDesc")
                        if isinstance(desc, str) and desc not in names:
                            names.append(desc)
                except OSError:
                    continue
    except OSError:
        return []
    return names


def _gpus_linux() -> list[str]:
    """Linux: lspci 或 lshw。"""
    # 先试 lspci
    out = _run_shell("lspci 2>/dev/null | grep -iE 'vga|3d|display'")
    names = []
    for line in out.splitlines():
        # 格式: "00:01.0 VGA compatible controller: Intel Corporation HD Graphics (rev 09)"
        if ":" in line:
            name = line.split(":", 1)[1].strip()
            # 去掉厂商前缀后的设备描述
            names.append(name)
    if names:
        return names
    # 回退 lshw
    out = _run_shell("lshw -C display 2>/dev/null | grep 'product:'")
    return [line.split("product:", 1)[1].strip() for line in out.splitlines() if "product:" in line]


def _gpus_macos() -> list[str]:
    """macOS: system_profiler。"""
    out = _run_shell("system_profiler SPDisplaysDataType 2>/dev/null")
    names = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Chipset Model:") or line.startswith("Vendor:"):
            val = line.split(":", 1)[1].strip()
            if val:
                names.append(val)
    return names


# ---------- NVIDIA 显存 ----------

def _nvidia_vram_smi() -> dict[str, int]:
    """用 nvidia-smi 查每张 NVIDIA 显卡的显存，返回 {显卡名: 显存MB}。"""
    # 注意：不要用 shell + `2>/dev/null`，Windows cmd 不认 /dev/null，
    # 会导致整个命令失败。stderr 由 capture_output 吞掉即可。
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total",
        "--format=csv,noheader,nounits",
    ])
    result = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2:
            name, vram = parts
            try:
                result[name] = int(vram)
            except ValueError:
                pass
    return result


# ---------- 主检测函数 ----------

def detect_hardware() -> HardwareInfo:
    """检测本机硬件，返回 HardwareInfo。"""
    import psutil

    system = platform.system().lower()
    os_name = "windows" if system.startswith("win") else "linux" if system.startswith("linux") else "macos"

    # 获取显卡名称列表
    if os_name == "windows":
        gpu_names = _gpus_windows()
    elif os_name == "linux":
        gpu_names = _gpus_linux()
    else:
        gpu_names = _gpus_macos()

    # NVIDIA 显存（如果装了驱动）
    nvidia_vram = _nvidia_vram_smi()

    gpus: list[GPUInfo] = []
    for name in gpu_names:
        vendor = _match_vendor(name)
        vram = 0
        if vendor == "nvidia":
            # 精确匹配 nvidia-smi 的名称，匹配不上就用模糊匹配
            vram = nvidia_vram.get(name, 0)
            if vram == 0:
                for smi_name, mb in nvidia_vram.items():
                    if smi_name.lower() in name.lower() or name.lower() in smi_name.lower():
                        vram = mb
                        break
        elif vendor in ("amd", "intel"):
            # 独显一般 >=2GB，集成显卡 vram 标 0
            if any(kw in name.lower() for kw in ["radeon rx", "arc a", "rtx", "instinct", "vega"]):
                vram = 0  # 拿不到准确值，但标记为独显
        gpus.append(GPUInfo(vendor=vendor, name=name, vram_mb=vram))

    # 没检测到显卡就加一个 unknown
    if not gpus:
        gpus.append(GPUInfo(vendor="unknown", name="Unknown", vram_mb=0))

    return HardwareInfo(
        gpus=gpus,
        cpu_count=psutil.cpu_count(logical=True) or 0,
        ram_gb=round(psutil.virtual_memory().total / (1024**3), 1),
        os=os_name,
    )


def has_nvidia_gpu(hw: HardwareInfo) -> bool:
    return any(g.vendor == "nvidia" for g in hw.gpus)
