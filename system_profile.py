#!/usr/bin/env python3
"""
system_profile.py - Print machine information relevant to local LLM deployment.

Usage:
    python system_profile.py
    python system_profile.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GIB = 1024 ** 3
MIB = 1024 ** 2


def run_command(command: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)

    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def normalize_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def to_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return int(value)

    text = str(value).strip().replace(",", "")
    if not text:
        return None

    try:
        return int(text)
    except ValueError:
        match = re.search(r"-?\d+", text)
        if not match:
            return None
        return int(match.group(0))


def parse_size_text(value: Any) -> int | None:
    if value in (None, "", "Not Available"):
        return None

    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    match = re.search(r"([\d.]+)\s*([KMGTP]?B)?", text, re.IGNORECASE)
    if not match:
        return None

    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    scale = {
        "B": 1,
        "KB": 1024,
        "MB": MIB,
        "GB": GIB,
        "TB": 1024 ** 4,
        "PB": 1024 ** 5,
    }.get(unit)
    if scale is None:
        return None
    return int(number * scale)


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"

    value = int(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    magnitude = 0
    display = float(value)
    while display >= 1024 and magnitude < len(units) - 1:
        display /= 1024.0
        magnitude += 1
    return f"{display:.2f} {units[magnitude]}"


def format_mhz(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1000:
        return f"{value / 1000.0:.2f} GHz"
    return f"{value} MHz"


def format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"

    total_seconds = int(seconds)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def format_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None

    if text.startswith("/Date("):
        match = re.search(r"/Date\((\d+)", text)
        if match:
            return datetime.fromtimestamp(int(match.group(1)) / 1000.0, tz=timezone.utc)

    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def run_powershell_json(script: str, timeout: int = 25) -> dict[str, Any] | None:
    shell_path = shutil.which("pwsh") or shutil.which("powershell")
    if not shell_path:
        return None

    wrapped = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        + script
    )
    code, stdout, _stderr = run_command([shell_path, "-NoProfile", "-Command", wrapped], timeout=timeout)
    if code != 0 or not stdout:
        return None

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        last_line = stdout.splitlines()[-1].strip() if stdout else ""
        if not last_line:
            return None
        try:
            return json.loads(last_line)
        except json.JSONDecodeError:
            return None


def detect_nvidia_smi() -> dict[str, Any] | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None

    detail_code, detail_stdout, _detail_stderr = run_command([executable], timeout=15)
    if detail_code != 0:
        return {"available": False, "path": executable}

    query_code, query_stdout, _query_stderr = run_command(
        [
            executable,
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ],
        timeout=15,
    )
    gpus: list[dict[str, Any]] = []
    if query_code == 0 and query_stdout:
        for line in query_stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 3:
                continue
            memory_mib = to_int(parts[2])
            gpus.append(
                {
                    "name": parts[0],
                    "driver_version": parts[1],
                    "memory_total_bytes": memory_mib * MIB if memory_mib is not None else None,
                }
            )

    cuda_version = None
    match = re.search(r"CUDA Version:\s*([\d.]+)", detail_stdout)
    if match:
        cuda_version = match.group(1)

    return {
        "available": True,
        "path": executable,
        "cuda_version": cuda_version,
        "gpus": gpus,
    }


def find_matching_gpu(name: str, gpus: list[dict[str, Any]]) -> dict[str, Any] | None:
    needle = name.strip().lower()
    if not needle:
        return None

    for gpu in gpus:
        candidate = str(gpu.get("name") or "").strip().lower()
        if candidate == needle:
            return gpu

    for gpu in gpus:
        candidate = str(gpu.get("name") or "").strip().lower()
        if needle in candidate or candidate in needle:
            return gpu

    return None


def detect_windows_dxdiag_gpus() -> list[dict[str, Any]]:
    executable = shutil.which("dxdiag")
    if not executable:
        return []

    temp_path: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile(prefix="dxdiag-", suffix=".xml", delete=False)
        temp_path = Path(handle.name)
        handle.close()

        code, _stdout, _stderr = run_command([executable, "/x", str(temp_path)], timeout=45)
        if code != 0 or not temp_path.exists():
            return []

        root = ET.parse(temp_path).getroot()
        display_devices = root.find("DisplayDevices")
        if display_devices is None:
            return []

        gpus: list[dict[str, Any]] = []
        for device in display_devices.findall("DisplayDevice"):
            name = (device.findtext("CardName") or "").strip()
            if not name:
                continue

            dedicated = parse_size_text(device.findtext("DedicatedMemory"))
            shared = parse_size_text(device.findtext("SharedMemory"))
            total = parse_size_text(device.findtext("DisplayMemory"))
            gpus.append(
                {
                    "name": name,
                    "vendor": (device.findtext("Manufacturer") or "").strip() or None,
                    "chip_type": (device.findtext("ChipType") or "").strip() or None,
                    "driver_version": (device.findtext("DriverVersion") or "").strip() or None,
                    "driver_model": (device.findtext("DriverModel") or "").strip() or None,
                    "dedicated_memory_bytes": dedicated,
                    "shared_memory_bytes": shared,
                    "graphics_memory_bytes": total,
                }
            )

        return gpus
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def classify_gpu_memory_model(gpu: dict[str, Any], system: dict[str, Any]) -> str:
    name_text = " ".join(
        str(part)
        for part in (gpu.get("name"), gpu.get("vendor"), gpu.get("pnp_device_id"), gpu.get("chip_type"))
        if part
    ).lower()
    dedicated = to_int(gpu.get("dedicated_memory_bytes") or gpu.get("vram_bytes")) or 0
    shared = to_int(gpu.get("shared_memory_bytes")) or 0
    operating_system = str(system.get("platform") or "").lower()
    architecture = str(system.get("architecture") or "").lower()

    if operating_system == "darwin" and architecture == "arm64":
        return "unified"
    if operating_system == "linux" and any(token in name_text for token in ("tegra", "jetson", "orin")):
        return "unified"
    if any(token in name_text for token in ("intel", "ven_8086", "uhd", "iris", "integrated")):
        return "shared"
    if "radeon(tm) graphics" in name_text and " rx " not in f" {name_text} ":
        return "shared"
    if shared > 0 and dedicated <= 512 * MIB:
        return "shared"
    if dedicated > 0:
        return "dedicated"
    if shared > 0:
        return "shared"
    return "unknown"


def summarize_memory_model(gpus: list[dict[str, Any]]) -> str:
    models = {gpu.get("memory_model") for gpu in gpus if gpu.get("memory_model") and gpu.get("memory_model") != "unknown"}
    if not models:
        return "unknown"
    if len(models) == 1:
        return next(iter(models))
    return "mixed"


def describe_memory_model(model: str | None) -> str:
    mapping = {
        "dedicated": "no (dedicated VRAM)",
        "shared": "yes (shared system memory)",
        "unified": "yes (unified memory)",
        "mixed": "mixed",
        "unknown": "unknown",
        None: "unknown",
    }
    return mapping.get(model, str(model))


def choose_primary_gpu(gpus: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not gpus:
        return None

    def sort_key(gpu: dict[str, Any]) -> tuple[int, int, str]:
        dedicated = to_int(gpu.get("dedicated_memory_bytes") or gpu.get("vram_bytes")) or 0
        shared = to_int(gpu.get("shared_memory_bytes")) or 0
        return (dedicated, shared, str(gpu.get("name") or ""))

    return max(gpus, key=sort_key)


def windows_cim_snapshot() -> dict[str, Any]:
    script = r"""
    $payload = [ordered]@{
      os = Get-CimInstance Win32_OperatingSystem |
        Select-Object Caption, Version, BuildNumber, OSArchitecture, LastBootUpTime, TotalVisibleMemorySize, FreePhysicalMemory, TotalVirtualMemorySize, FreeVirtualMemory
      computer = Get-CimInstance Win32_ComputerSystem |
        Select-Object Manufacturer, Model, TotalPhysicalMemory, HypervisorPresent
      cpu = Get-CimInstance Win32_Processor |
                Select-Object Name, Manufacturer, MaxClockSpeed, CurrentClockSpeed, NumberOfCores, NumberOfLogicalProcessors, AddressWidth, ProcessorId
      gpu = Get-CimInstance Win32_VideoController |
        Select-Object Name, AdapterCompatibility, AdapterRAM, DriverVersion, VideoProcessor, PNPDeviceID, Status
      disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" |
        Select-Object DeviceID, VolumeName, Size, FreeSpace, FileSystem
      pagefiles = Get-CimInstance Win32_PageFileUsage |
        Select-Object Name, AllocatedBaseSize, CurrentUsage, PeakUsage, TempPageFile
    }
    $payload | ConvertTo-Json -Depth 5 -Compress
    """
    return run_powershell_json(script) or {}


def build_windows_profile() -> dict[str, Any]:
    snapshot = windows_cim_snapshot()
    os_info = snapshot.get("os") or {}
    computer = snapshot.get("computer") or {}
    cpu_info = normalize_list(snapshot.get("cpu"))
    primary_cpu = cpu_info[0] if cpu_info else {}
    wmi_gpus_raw = normalize_list(snapshot.get("gpu"))
    dxdiag_gpus = detect_windows_dxdiag_gpus()
    disks_raw = normalize_list(snapshot.get("disks"))
    pagefiles_raw = normalize_list(snapshot.get("pagefiles"))
    nvidia_smi = detect_nvidia_smi()

    system = {
        "platform": "windows",
        "architecture": platform.machine().lower(),
        "hostname": socket.gethostname(),
    }

    gpus: list[dict[str, Any]] = []
    if dxdiag_gpus:
        for gpu in dxdiag_gpus:
            merged = dict(gpu)
            wmi_match = find_matching_gpu(str(gpu.get("name") or ""), wmi_gpus_raw)
            if wmi_match:
                merged.setdefault("vendor", wmi_match.get("AdapterCompatibility"))
                merged.setdefault("driver_version", wmi_match.get("DriverVersion"))
                merged["video_processor"] = wmi_match.get("VideoProcessor")
                merged["pnp_device_id"] = wmi_match.get("PNPDeviceID")
                merged["status"] = wmi_match.get("Status")
            gpus.append(merged)
    else:
        for raw_gpu in wmi_gpus_raw:
            gpus.append(
                {
                    "name": raw_gpu.get("Name"),
                    "vendor": raw_gpu.get("AdapterCompatibility"),
                    "driver_version": raw_gpu.get("DriverVersion"),
                    "video_processor": raw_gpu.get("VideoProcessor"),
                    "pnp_device_id": raw_gpu.get("PNPDeviceID"),
                    "status": raw_gpu.get("Status"),
                    "dedicated_memory_bytes": to_int(raw_gpu.get("AdapterRAM")),
                    "graphics_memory_bytes": to_int(raw_gpu.get("AdapterRAM")),
                }
            )

    if nvidia_smi and nvidia_smi.get("available"):
        for smi_gpu in nvidia_smi.get("gpus", []):
            match = find_matching_gpu(str(smi_gpu.get("name") or ""), gpus)
            if match:
                smi_memory = to_int(smi_gpu.get("memory_total_bytes"))
                if smi_memory:
                    match["dedicated_memory_bytes"] = smi_memory
                    match.setdefault("graphics_memory_bytes", smi_memory)
                match.setdefault("driver_version", smi_gpu.get("driver_version"))
                match["cuda_visible"] = True

    for gpu in gpus:
        gpu["memory_model"] = classify_gpu_memory_model(gpu, system)

    total_ram = to_int(computer.get("TotalPhysicalMemory"))
    if total_ram is None:
        total_visible_kib = to_int(os_info.get("TotalVisibleMemorySize"))
        total_ram = total_visible_kib * 1024 if total_visible_kib is not None else None

    available_ram_kib = to_int(os_info.get("FreePhysicalMemory"))
    available_ram = available_ram_kib * 1024 if available_ram_kib is not None else None

    pagefile_allocated = 0
    pagefile_in_use = 0
    pagefile_peak = 0
    has_pagefile = False
    for pagefile in pagefiles_raw:
        allocated = to_int(pagefile.get("AllocatedBaseSize"))
        current = to_int(pagefile.get("CurrentUsage"))
        peak = to_int(pagefile.get("PeakUsage"))
        if allocated is not None:
            pagefile_allocated += allocated * MIB
            has_pagefile = True
        if current is not None:
            pagefile_in_use += current * MIB
        if peak is not None:
            pagefile_peak += peak * MIB

    disks = []
    workspace_drive = Path.cwd().drive.upper()
    for disk in disks_raw:
        drive = str(disk.get("DeviceID") or "")
        disks.append(
            {
                "mount": drive,
                "label": disk.get("VolumeName"),
                "filesystem": disk.get("FileSystem"),
                "total_bytes": to_int(disk.get("Size")),
                "free_bytes": to_int(disk.get("FreeSpace")),
                "is_workspace_drive": drive.upper() == workspace_drive,
            }
        )

    last_boot = parse_datetime(os_info.get("LastBootUpTime"))
    uptime_seconds = None
    if last_boot is not None:
        if last_boot.tzinfo is None:
            last_boot = last_boot.replace(tzinfo=timezone.utc)
        uptime_seconds = max(0.0, (datetime.now(timezone.utc) - last_boot.astimezone(timezone.utc)).total_seconds())

    profile = {
        "system": {
            "hostname": socket.gethostname(),
            "platform": "Windows",
            "os_name": os_info.get("Caption") or platform.platform(),
            "os_version": os_info.get("Version"),
            "os_build": os_info.get("BuildNumber"),
            "os_architecture": os_info.get("OSArchitecture") or platform.machine(),
            "uptime_seconds": uptime_seconds,
            "manufacturer": computer.get("Manufacturer"),
            "model": computer.get("Model"),
            "hypervisor_present": computer.get("HypervisorPresent"),
        },
        "cpu": {
            "name": primary_cpu.get("Name"),
            "manufacturer": primary_cpu.get("Manufacturer"),
            "architecture": platform.machine(),
            "address_width": to_int(primary_cpu.get("AddressWidth")),
            "current_clock_mhz": to_int(primary_cpu.get("CurrentClockSpeed")),
            "max_clock_mhz": to_int(primary_cpu.get("MaxClockSpeed")),
            "cores": to_int(primary_cpu.get("NumberOfCores")),
            "logical_processors": to_int(primary_cpu.get("NumberOfLogicalProcessors")),
            "processor_id": primary_cpu.get("ProcessorId"),
        },
        "memory": {
            "total_ram_bytes": total_ram,
            "available_ram_bytes": available_ram,
            "pagefile_allocated_bytes": pagefile_allocated if has_pagefile else None,
            "pagefile_in_use_bytes": pagefile_in_use if has_pagefile else None,
            "pagefile_peak_bytes": pagefile_peak if has_pagefile else None,
        },
        "gpus": gpus,
        "storage": disks,
        "tooling": {
            "nvidia_smi": nvidia_smi,
            "dxdiag_available": bool(shutil.which("dxdiag")),
        },
    }

    return finalize_profile(profile)


def read_linux_meminfo() -> dict[str, int]:
    meminfo: dict[str, int] = {}
    path = Path("/proc/meminfo")
    if not path.exists():
        return meminfo

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        amount = to_int(value)
        if amount is not None:
            meminfo[key] = amount * 1024
    return meminfo


def read_linux_cpu_name() -> str | None:
    path = Path("/proc/cpuinfo")
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lower().startswith("model name"):
            _, value = line.split(":", 1)
            return value.strip()
    return None


def read_linux_cpu_max_mhz() -> int | None:
    candidates = [
        Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"),
        Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq"),
    ]
    for candidate in candidates:
        if candidate.exists():
            value = to_int(candidate.read_text(encoding="utf-8", errors="replace").strip())
            if value is not None:
                return int(value / 1000)

    code, stdout, _stderr = run_command(["lscpu"], timeout=10)
    if code == 0:
        for line in stdout.splitlines():
            if "CPU max MHz" in line:
                _, value = line.split(":", 1)
                try:
                    return int(float(value.strip()))
                except ValueError:
                    return None
    return None


def detect_linux_gpus() -> list[dict[str, Any]]:
    nvidia_smi = detect_nvidia_smi()
    gpus: list[dict[str, Any]] = []
    if nvidia_smi and nvidia_smi.get("available"):
        for gpu in nvidia_smi.get("gpus", []):
            gpus.append(
                {
                    "name": gpu.get("name"),
                    "vendor": "NVIDIA",
                    "driver_version": gpu.get("driver_version"),
                    "dedicated_memory_bytes": to_int(gpu.get("memory_total_bytes")),
                    "graphics_memory_bytes": to_int(gpu.get("memory_total_bytes")),
                }
            )

    if gpus:
        return gpus

    code, stdout, _stderr = run_command(["lspci"], timeout=10)
    if code != 0:
        return []

    for line in stdout.splitlines():
        lowered = line.lower()
        if not any(token in lowered for token in ("vga compatible controller", "3d controller", "display controller")):
            continue
        _, _, name = line.partition(":")
        gpus.append({"name": name.strip(), "vendor": None})
    return gpus


def build_linux_profile() -> dict[str, Any]:
    uname = platform.uname()
    meminfo = read_linux_meminfo()
    total_ram = meminfo.get("MemTotal")
    available_ram = meminfo.get("MemAvailable") or meminfo.get("MemFree")
    swap_total = meminfo.get("SwapTotal")
    swap_free = meminfo.get("SwapFree")
    gpus = detect_linux_gpus()

    for gpu in gpus:
        gpu["memory_model"] = classify_gpu_memory_model(
            gpu,
            {"platform": "linux", "architecture": uname.machine.lower()},
        )

    disks = []
    current_path = Path.cwd()
    for candidate in [current_path, Path("/")]:
        usage = shutil.disk_usage(candidate)
        disks.append(
            {
                "mount": str(candidate),
                "label": None,
                "filesystem": None,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "is_workspace_drive": candidate == current_path,
            }
        )

    uptime_seconds = None
    uptime_path = Path("/proc/uptime")
    if uptime_path.exists():
        try:
            uptime_seconds = float(uptime_path.read_text(encoding="utf-8", errors="replace").split()[0])
        except (IndexError, ValueError):
            uptime_seconds = None

    profile = {
        "system": {
            "hostname": socket.gethostname(),
            "platform": "Linux",
            "os_name": platform.platform(),
            "os_version": uname.release,
            "os_build": uname.version,
            "os_architecture": uname.machine,
            "uptime_seconds": uptime_seconds,
            "manufacturer": None,
            "model": None,
            "hypervisor_present": None,
        },
        "cpu": {
            "name": read_linux_cpu_name() or uname.processor or uname.machine,
            "manufacturer": None,
            "architecture": uname.machine,
            "address_width": 64 if sys.maxsize > 2 ** 32 else 32,
            "current_clock_mhz": None,
            "max_clock_mhz": read_linux_cpu_max_mhz(),
            "cores": os.cpu_count(),
            "logical_processors": os.cpu_count(),
            "processor_id": None,
        },
        "memory": {
            "total_ram_bytes": total_ram,
            "available_ram_bytes": available_ram,
            "pagefile_allocated_bytes": swap_total,
            "pagefile_in_use_bytes": (swap_total - swap_free) if swap_total is not None and swap_free is not None else None,
            "pagefile_peak_bytes": None,
        },
        "gpus": gpus,
        "storage": disks,
        "tooling": {
            "nvidia_smi": detect_nvidia_smi(),
            "dxdiag_available": False,
        },
    }

    return finalize_profile(profile)


def read_macos_sysctl(name: str) -> str | None:
    code, stdout, _stderr = run_command(["sysctl", "-n", name], timeout=10)
    if code != 0 or not stdout:
        return None
    return stdout.strip()


def detect_macos_gpu() -> list[dict[str, Any]]:
    code, stdout, _stderr = run_command(["system_profiler", "SPDisplaysDataType", "-json"], timeout=20)
    if code != 0 or not stdout:
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return []

    displays = payload.get("SPDisplaysDataType") or []
    gpus: list[dict[str, Any]] = []
    for item in displays:
        name = item.get("sppci_model") or item.get("_name")
        if not name:
            continue
        gpus.append(
            {
                "name": name,
                "vendor": item.get("spdisplays_vendor"),
                "driver_version": None,
                "graphics_memory_bytes": parse_size_text(item.get("spdisplays_vram")),
                "dedicated_memory_bytes": parse_size_text(item.get("spdisplays_vram")),
            }
        )
    return gpus


def build_macos_profile() -> dict[str, Any]:
    uname = platform.uname()
    total_ram = to_int(read_macos_sysctl("hw.memsize"))
    gpus = detect_macos_gpu()
    for gpu in gpus:
        gpu["memory_model"] = classify_gpu_memory_model(
            gpu,
            {"platform": "darwin", "architecture": uname.machine.lower()},
        )

    current_path = Path.cwd()
    usage = shutil.disk_usage(current_path)
    boot_time_raw = read_macos_sysctl("kern.boottime")
    uptime_seconds = None
    if boot_time_raw:
        match = re.search(r"sec = (\d+)", boot_time_raw)
        if match:
            uptime_seconds = max(0.0, time.time() - int(match.group(1)))

    profile = {
        "system": {
            "hostname": socket.gethostname(),
            "platform": "macOS",
            "os_name": platform.platform(),
            "os_version": platform.mac_ver()[0],
            "os_build": uname.version,
            "os_architecture": uname.machine,
            "uptime_seconds": uptime_seconds,
            "manufacturer": "Apple",
            "model": None,
            "hypervisor_present": None,
        },
        "cpu": {
            "name": read_macos_sysctl("machdep.cpu.brand_string") or uname.processor or uname.machine,
            "manufacturer": "Apple" if uname.machine == "arm64" else None,
            "architecture": uname.machine,
            "address_width": 64 if sys.maxsize > 2 ** 32 else 32,
            "current_clock_mhz": None,
            "max_clock_mhz": None,
            "cores": to_int(read_macos_sysctl("hw.physicalcpu")),
            "logical_processors": to_int(read_macos_sysctl("hw.logicalcpu")),
            "processor_id": None,
        },
        "memory": {
            "total_ram_bytes": total_ram,
            "available_ram_bytes": None,
            "pagefile_allocated_bytes": None,
            "pagefile_in_use_bytes": None,
            "pagefile_peak_bytes": None,
        },
        "gpus": gpus,
        "storage": [
            {
                "mount": str(current_path),
                "label": None,
                "filesystem": None,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "is_workspace_drive": True,
            }
        ],
        "tooling": {
            "nvidia_smi": detect_nvidia_smi(),
            "dxdiag_available": False,
        },
    }

    return finalize_profile(profile)


def finalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    gpus = profile.get("gpus") or []
    primary_gpu = choose_primary_gpu(gpus)
    dedicated_values = [to_int(gpu.get("dedicated_memory_bytes") or gpu.get("vram_bytes")) or 0 for gpu in gpus]
    shared_values = [to_int(gpu.get("shared_memory_bytes")) or 0 for gpu in gpus]
    workspace_disk = next((disk for disk in profile.get("storage", []) if disk.get("is_workspace_drive")), None)

    profile["llm_deployment"] = {
        "overall_gpu_memory_model": summarize_memory_model(gpus),
        "primary_gpu": primary_gpu.get("name") if primary_gpu else None,
        "largest_dedicated_gpu_memory_bytes": max(dedicated_values) if dedicated_values else None,
        "largest_shared_gpu_memory_bytes": max(shared_values) if shared_values else None,
        "system_ram_bytes": profile.get("memory", {}).get("total_ram_bytes"),
        "available_ram_bytes": profile.get("memory", {}).get("available_ram_bytes"),
        "pagefile_allocated_bytes": profile.get("memory", {}).get("pagefile_allocated_bytes"),
        "workspace_free_bytes": workspace_disk.get("free_bytes") if workspace_disk else None,
        "nvidia_smi_available": bool((profile.get("tooling") or {}).get("nvidia_smi", {}).get("available")),
        "cuda_version": (profile.get("tooling") or {}).get("nvidia_smi", {}).get("cuda_version"),
    }
    return profile


def human_lines(profile: dict[str, Any]) -> list[str]:
    system = profile.get("system", {})
    cpu = profile.get("cpu", {})
    memory = profile.get("memory", {})
    gpus = profile.get("gpus", [])
    storage = profile.get("storage", [])
    deployment = profile.get("llm_deployment", {})
    tooling = profile.get("tooling", {})

    lines = [
        "== System ==",
        f"Hostname: {system.get('hostname') or 'unknown'}",
        f"OS: {system.get('os_name') or 'unknown'}",
        f"OS Version: {system.get('os_version') or 'unknown'}",
        f"OS Build: {system.get('os_build') or 'unknown'}",
        f"Architecture: {system.get('os_architecture') or 'unknown'}",
        f"Manufacturer / Model: {(system.get('manufacturer') or 'unknown')} / {(system.get('model') or 'unknown')}",
        f"Uptime: {format_uptime(system.get('uptime_seconds'))}",
        f"Hypervisor Present: {format_bool(system.get('hypervisor_present'))}",
        "",
        "== CPU ==",
        f"Model: {cpu.get('name') or 'unknown'}",
        f"Vendor: {cpu.get('manufacturer') or 'unknown'}",
        f"Architecture: {cpu.get('architecture') or 'unknown'}",
        f"Address Width: {cpu.get('address_width') or 'unknown'} bit",
        f"Cores / Threads: {(cpu.get('cores') or 'unknown')} / {(cpu.get('logical_processors') or 'unknown')}",
        f"Current Clock: {format_mhz(cpu.get('current_clock_mhz'))}",
        f"Reported Base / Max Clock: {format_mhz(cpu.get('max_clock_mhz'))}",
        "",
        "== Memory ==",
        f"System RAM: {format_bytes(memory.get('total_ram_bytes'))}",
        f"Available RAM: {format_bytes(memory.get('available_ram_bytes'))}",
        f"Page File Allocated: {format_bytes(memory.get('pagefile_allocated_bytes'))}",
        f"Page File In Use: {format_bytes(memory.get('pagefile_in_use_bytes'))}",
    ]

    lines.append("")
    lines.append("== GPU ==")
    if not gpus:
        lines.append("No GPU information detected")
    else:
        for index, gpu in enumerate(gpus, start=1):
            lines.append(f"[{index}] {gpu.get('name') or 'unknown'}")
            lines.append(f"  Vendor: {gpu.get('vendor') or 'unknown'}")
            lines.append(f"  Driver: {gpu.get('driver_version') or 'unknown'}")
            lines.append(f"  Dedicated Graphics Memory: {format_bytes(gpu.get('dedicated_memory_bytes') or gpu.get('vram_bytes'))}")
            lines.append(f"  Shared System Memory: {format_bytes(gpu.get('shared_memory_bytes'))}")
            lines.append(f"  Total Graphics Memory: {format_bytes(gpu.get('graphics_memory_bytes'))}")
            lines.append(f"  Memory Model: {gpu.get('memory_model') or 'unknown'}")
            if gpu.get("driver_model"):
                lines.append(f"  Driver Model: {gpu.get('driver_model')}")
            if gpu.get("cuda_visible"):
                lines.append("  CUDA Visible: yes")

    lines.append("")
    lines.append("== Storage ==")
    if not storage:
        lines.append("No storage information detected")
    else:
        for disk in storage:
            marker = " [workspace]" if disk.get("is_workspace_drive") else ""
            lines.append(
                f"{disk.get('mount') or 'unknown'}{marker}: total {format_bytes(disk.get('total_bytes'))}, free {format_bytes(disk.get('free_bytes'))}, fs {(disk.get('filesystem') or 'unknown')}"
            )

    lines.extend(
        [
            "",
            "== LLM Deployment Factors ==",
            f"Primary GPU: {deployment.get('primary_gpu') or 'unknown'}",
            f"Largest Dedicated GPU Memory: {format_bytes(deployment.get('largest_dedicated_gpu_memory_bytes'))}",
            f"Largest Shared GPU Memory: {format_bytes(deployment.get('largest_shared_gpu_memory_bytes'))}",
            f"Overall GPU Memory Model: {deployment.get('overall_gpu_memory_model') or 'unknown'}",
            f"Unified / Shared GPU Memory: {describe_memory_model(deployment.get('overall_gpu_memory_model'))}",
            f"System RAM: {format_bytes(deployment.get('system_ram_bytes'))}",
            f"Available RAM: {format_bytes(deployment.get('available_ram_bytes'))}",
            f"Workspace Free Space: {format_bytes(deployment.get('workspace_free_bytes'))}",
            f"NVIDIA SMI Available: {format_bool(deployment.get('nvidia_smi_available'))}",
            f"CUDA Version: {deployment.get('cuda_version') or 'unknown'}",
            f"dxdiag Available: {format_bool(tooling.get('dxdiag_available'))}",
        ]
    )
    return lines


def collect_profile() -> dict[str, Any]:
    current_platform = sys.platform
    if current_platform.startswith("win"):
        return build_windows_profile()
    if current_platform.startswith("linux"):
        return build_linux_profile()
    if current_platform == "darwin":
        return build_macos_profile()

    return finalize_profile(
        {
            "system": {
                "hostname": socket.gethostname(),
                "platform": platform.system(),
                "os_name": platform.platform(),
                "os_version": platform.version(),
                "os_build": None,
                "os_architecture": platform.machine(),
                "uptime_seconds": None,
                "manufacturer": None,
                "model": None,
                "hypervisor_present": None,
            },
            "cpu": {
                "name": platform.processor() or platform.machine(),
                "manufacturer": None,
                "architecture": platform.machine(),
                "address_width": 64 if sys.maxsize > 2 ** 32 else 32,
                "current_clock_mhz": None,
                "max_clock_mhz": None,
                "cores": os.cpu_count(),
                "logical_processors": os.cpu_count(),
                "processor_id": None,
            },
            "memory": {
                "total_ram_bytes": None,
                "available_ram_bytes": None,
                "pagefile_allocated_bytes": None,
                "pagefile_in_use_bytes": None,
                "pagefile_peak_bytes": None,
            },
            "gpus": [],
            "storage": [],
            "tooling": {"nvidia_smi": detect_nvidia_smi(), "dxdiag_available": False},
        }
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print machine information relevant to local LLM deployment.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a human-readable report")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profile = collect_profile()

    if args.json:
        print(json.dumps(profile, ensure_ascii=False, indent=2))
        return 0

    print("\n".join(human_lines(profile)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())