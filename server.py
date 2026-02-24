#!/usr/bin/env python3
"""
MCP Server: System Activity Monitor
Exposes tools for querying CPU, RAM, GPU, disk, network, and process info.
"""

import base64
import datetime
import io
import json
import os
import pathlib
import platform
import plistlib
import re
import signal
import socket
import sys
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

try:
    import psutil
except ImportError:
    print("psutil not found. Install with: pip install psutil", file=sys.stderr)
    sys.exit(1)

# Optional GPU support via GPUtil
try:
    import GPUtil
    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False


# ── MCP helpers ──────────────────────────────────────────────────────────────

def _classify_pressure(percent: float) -> str:
    if percent >= 90: return "critical"
    if percent >= 75: return "high"
    if percent >= 50: return "moderate"
    return "low"


_PROTECTED_PIDS  = {0, 1}
_PROTECTED_NAMES = frozenset({
    "launchd", "systemd", "init", "kernel_task",
    "svchost.exe", "winlogon.exe", "csrss.exe",
    "smss.exe", "wininit.exe", "lsass.exe", "services.exe",
})


def _detect_cpu_oc(cpu_brand: str, system: str, machine: str) -> dict:
    if machine == "arm64" and system == "Darwin":
        return {"supported": False, "reason": "Apple Silicon CPUs have fixed clock speeds and cannot be overclocked.", "tools": []}
    if system == "Darwin":
        return {"supported": False, "reason": "Intel Macs lack BIOS access required for overclocking.", "tools": []}
    if re.search(r'\bintel\b', cpu_brand, re.I):
        unlocked = bool(re.search(r'\b\d{3,5}[kK][sS]?\b', cpu_brand))
        return {
            "supported": unlocked,
            "reason": ("K/KF/KS-series Intel CPUs support overclocking via BIOS multiplier adjustment."
                       if unlocked else "Non-K Intel CPUs have locked multipliers and cannot be overclocked."),
            "tools": ["Intel Extreme Tuning Utility (XTU)", "BIOS/UEFI"] if unlocked else [],
        }
    if re.search(r'\b(amd|ryzen)\b', cpu_brand, re.I):
        return {
            "supported": True,
            "reason": "AMD Ryzen CPUs support Precision Boost Overdrive (PBO) for automated overclocking and manual clock adjustments.",
            "tools": ["AMD Ryzen Master", "BIOS/UEFI PBO settings"],
        }
    return {"supported": False, "reason": "Could not determine OC capability from CPU brand string.", "tools": []}


def _detect_gpu_oc(system: str, machine: str, gpu_data: dict) -> dict:
    if machine == "arm64":
        return {"supported": False, "reason": "Apple Silicon GPU is integrated into the SoC and cannot be overclocked.", "tools": []}
    if system == "Darwin":
        return {"supported": False, "reason": "macOS does not expose GPU overclocking controls.", "tools": []}
    if "error" in gpu_data:
        return {"supported": False, "reason": "No discrete GPU detected.", "tools": []}
    return {
        "supported": True,
        "reason": "Discrete GPUs on Windows/Linux support overclocking via third-party tools.",
        "tools": ["MSI Afterburner", "EVGA Precision X1", "AMD Radeon Software Adrenalin"],
    }


def _get_upgrade_feasibility(system: str, machine: str) -> dict:
    if machine == "arm64" and system == "Darwin":
        return {
            "ram":     {"upgradeable": False, "note": "Unified memory is soldered to the Apple Silicon SoC — cannot be upgraded."},
            "cpu":     {"upgradeable": False, "note": "CPU is part of the Apple Silicon SoC — cannot be swapped."},
            "gpu":     {"upgradeable": False, "note": "GPU is integrated into the SoC. eGPU support was removed in macOS 14."},
            "storage": {"upgradeable": False, "note": "Internal SSD is proprietary and soldered. External Thunderbolt 4 drives are the only capacity expansion option."},
        }
    if system == "Darwin":
        return {
            "ram":     {"upgradeable": "model-dependent", "note": "Pre-2019 MacBook Pros and some Mac Pros have user-upgradeable RAM — check your exact model."},
            "cpu":     {"upgradeable": False, "note": "Intel Mac CPUs are soldered on most models since 2012."},
            "gpu":     {"upgradeable": "eGPU-only", "note": "Internal GPU not upgradeable. eGPU via Thunderbolt 3 supported on Intel Macs running macOS 13 or earlier."},
            "storage": {"upgradeable": "model-dependent", "note": "Some 2013–2017 MacBook Pro models accept third-party NVMe SSDs via adapters."},
        }
    return {
        "ram":     {"upgradeable": "likely", "note": "Most desktops/laptops support RAM upgrades. Check your motherboard or laptop spec for max supported speed and slot count."},
        "cpu":     {"upgradeable": "varies", "note": "Desktop CPUs are upgradeable if the socket matches. Laptop CPUs are usually soldered — verify your model."},
        "gpu":     {"upgradeable": "likely-desktop", "note": "Desktop PCIe GPUs are freely swappable. Laptop GPUs are typically soldered or MXM (rarely swappable)."},
        "storage": {"upgradeable": "likely", "note": "M.2 NVMe and 2.5-inch SATA slots are widely available. Check how many free slots your system has."},
    }


_USE_CASE_PROFILES = [
    (["lightroom", "photo editing", "photo", "capture one", "darktable"],
     "gpu", "ram",
     "Lightroom's AI features (Denoise, Select Subject, Masking) are GPU-accelerated. Export speed is CPU+GPU bound. Smart Previews and cache performance improve significantly with a fast NVMe SSD."),
    (["premiere", "video editing", "video", "davinci", "resolve", "final cut", "fcpx", "after effects"],
     "gpu", "ram",
     "Video editing benefits most from GPU acceleration (H.264/HEVC decode, effects rendering). RAM is critical for 4K+ multicam timelines. Fast NVMe SSD dramatically improves media cache and scratch disk performance."),
    (["gaming", "games", "game"],
     "gpu", "cpu",
     "Most games are GPU-bound. CPU matters for games with many entities (open-world, RTS). Fast NVMe storage reduces load times. RAM speed (frequency) affects frame pacing on AMD platforms."),
    (["blender", "3d render", "rendering", "maya", "cinema 4d", "c4d", "houdini"],
     "gpu", "ram",
     "GPU rendering (CUDA/OptiX/Metal) is fastest for most 3D renders. VRAM limits scene and texture complexity. CPU rendering uses all physical cores. RAM capacity affects how large a scene can be loaded."),
    (["compile", "compiling", "build", "xcode", "make", "cmake", "gradle", "rust", "go", "code", "coding", "development", "developer"],
     "cpu", "ram",
     "Compilation is highly CPU-bound — more physical cores and higher clock speed both help. RAM limits parallel compile jobs. A fast NVMe SSD dramatically reduces incremental build times via faster cache reads."),
    (["docker", "containers", "kubernetes", "vm", "virtual machine", "virtualbox", "vmware", "parallels"],
     "ram", "cpu",
     "Containers and VMs are RAM-limited first — each VM needs dedicated memory. CPU core count determines how many can run in parallel. Fast storage reduces image pull and disk I/O latency."),
    (["machine learning", "ml", "ai training", "training", "pytorch", "tensorflow", "cuda"],
     "gpu", "ram",
     "ML training is GPU-bound; VRAM limits batch size and model size. CPU handles data loading pipelines. RAM caches the dataset between epochs. Fast NVMe reduces I/O bottlenecks during data loading."),
    (["streaming", "obs", "twitch", "youtube live", "recording"],
     "gpu", "cpu",
     "Streaming with GPU encoding (NVENC/AMF/VideoToolbox) offloads work from the CPU. CPU encoding (x264) produces better quality but is CPU-intensive. RAM and fast storage handle replay buffers and recordings."),
]


def _use_case_analysis(use_case: str, cpu_pct: float, ram_pct: float) -> dict:
    uc = use_case.lower()
    primary, secondary, note = "unknown", "unknown", ""

    for keywords, p, s, n in _USE_CASE_PROFILES:
        if any(k in uc for k in keywords):
            primary, secondary, note = p, s, n
            break

    constraints = []
    if cpu_pct >= 75:
        constraints.append(f"cpu_pressure_{_classify_pressure(cpu_pct)}")
    if ram_pct >= 75:
        constraints.append(f"ram_pressure_{_classify_pressure(ram_pct)}")

    if primary == "unknown":
        note = "Use-case not recognized. Specify a workload (e.g. 'lightroom', 'gaming', 'video editing') for targeted bottleneck analysis."

    return {
        "primary_bottleneck": primary,
        "secondary_bottleneck": secondary,
        "current_constraints": constraints,
        "note": note,
    }


def _fig_to_b64(fig) -> str:
    """Serialize a matplotlib figure to a base64 PNG string and close it."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode()
    plt.close(fig)
    return encoded


def _safe(fn):
    try:
        return fn()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return None


def make_error(id_, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {"code": code, "message": message}
    }


# ── Tool implementations ──────────────────────────────────────────────────────

def get_cpu_usage() -> dict:
    per_core = psutil.cpu_percent(interval=0.5, percpu=True)
    freq = psutil.cpu_freq()
    return {
        "total_percent": psutil.cpu_percent(interval=0.5),
        "per_core_percent": per_core,
        "core_count_logical": psutil.cpu_count(logical=True),
        "core_count_physical": psutil.cpu_count(logical=False),
        "frequency_mhz": {
            "current": round(freq.current, 1) if freq else None,
            "min": round(freq.min, 1) if freq else None,
            "max": round(freq.max, 1) if freq else None,
        }
    }


def get_ram_usage() -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "ram": {
            "total_gb": round(vm.total / 1e9, 2),
            "available_gb": round(vm.available / 1e9, 2),
            "used_gb": round(vm.used / 1e9, 2),
            "percent_used": vm.percent,
        },
        "swap": {
            "total_gb": round(sw.total / 1e9, 2),
            "used_gb": round(sw.used / 1e9, 2),
            "percent_used": sw.percent,
        }
    }


def get_gpu_usage() -> dict:
    if not GPU_AVAILABLE:
        return {"error": "GPUtil not installed. Run: pip install gputil"}
    gpus = GPUtil.getGPUs()
    if not gpus:
        return {"error": "No GPUs detected"}
    return {
        "gpus": [
            {
                "id": g.id,
                "name": g.name,
                "load_percent": round(g.load * 100, 1),
                "memory_used_mb": round(g.memoryUsed, 1),
                "memory_total_mb": round(g.memoryTotal, 1),
                "memory_percent": round(g.memoryUsed / g.memoryTotal * 100, 1) if g.memoryTotal else None,
                "temperature_c": g.temperature,
            }
            for g in gpus
        ]
    }


def get_disk_usage() -> dict:
    partitions = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": round(usage.total / 1e9, 2),
                "used_gb": round(usage.used / 1e9, 2),
                "free_gb": round(usage.free / 1e9, 2),
                "percent_used": usage.percent,
            })
        except (PermissionError, OSError):
            continue
    io = psutil.disk_io_counters()
    return {
        "partitions": partitions,
        "io_counters": {
            "read_mb": round(io.read_bytes / 1e6, 2) if io else None,
            "write_mb": round(io.write_bytes / 1e6, 2) if io else None,
        }
    }


def get_network_usage() -> dict:
    net_io = psutil.net_io_counters()
    interfaces = {}
    for iface, stats in psutil.net_if_stats().items():
        interfaces[iface] = {
            "is_up": stats.isup,
            "speed_mbps": stats.speed,
        }
    return {
        "total_io": {
            "bytes_sent_mb": round(net_io.bytes_sent / 1e6, 2),
            "bytes_recv_mb": round(net_io.bytes_recv / 1e6, 2),
            "packets_sent": net_io.packets_sent,
            "packets_recv": net_io.packets_recv,
        },
        "interfaces": interfaces,
    }


def get_realtime_io(interval: int = 1) -> dict:
    interval = max(1, min(interval, 3))
    d1 = psutil.disk_io_counters()
    n1 = psutil.net_io_counters()
    time.sleep(interval)
    d2 = psutil.disk_io_counters()
    n2 = psutil.net_io_counters()
    dt = float(interval)

    if d1 is not None and d2 is not None:
        read_mbs = round((d2.read_bytes - d1.read_bytes) / 1e6 / dt, 3)
        write_mbs = round((d2.write_bytes - d1.write_bytes) / 1e6 / dt, 3)
        disk_ok = True
    else:
        read_mbs = write_mbs = None
        disk_ok = False

    dl_mbs = round((n2.bytes_recv - n1.bytes_recv) / 1e6 / dt, 3)
    ul_mbs = round((n2.bytes_sent - n1.bytes_sent) / 1e6 / dt, 3)

    return {
        "interval_seconds": interval,
        "disk": {"available": disk_ok, "read_mbs": read_mbs, "write_mbs": write_mbs},
        "network": {
            "download_mbs": dl_mbs,
            "upload_mbs": ul_mbs,
            "download_mbps": round(dl_mbs * 8, 3),
            "upload_mbps": round(ul_mbs * 8, 3),
        },
    }


def get_top_processes(n: int = 10, sort_by: str = "cpu") -> dict:
    """Return top N processes sorted by cpu or memory."""
    n = max(1, min(n, 100))
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status', 'num_threads']):
        try:
            info = p.info
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    key = "memory_percent" if sort_by == "memory" else "cpu_percent"
    procs.sort(key=lambda x: x.get(key) or 0, reverse=True)

    return {
        "sort_by": sort_by,
        "top_processes": [
            {
                "pid": p["pid"],
                "name": p["name"],
                "cpu_percent": round(p.get("cpu_percent") or 0, 2),
                "memory_percent": round(p.get("memory_percent") or 0, 2),
                "status": p.get("status"),
                "threads": p.get("num_threads"),
            }
            for p in procs[:n]
        ]
    }


def _cpu_with_chart() -> tuple:
    data = get_cpu_usage()
    cores = data["per_core_percent"]
    n = len(cores)

    fig, ax = plt.subplots(figsize=(7, max(3, n * 0.4)))
    colors = ["#e74c3c" if v >= 80 else "#e67e22" if v >= 60 else "#2ecc71" for v in cores]
    ax.barh([f"Core {i}" for i in range(n)], cores, color=colors, height=0.6)
    ax.axvline(data["total_percent"], color="#3498db", linestyle="--", linewidth=1.5,
               label=f'Total: {data["total_percent"]}%')
    ax.set_xlim(0, 100)
    ax.set_xlabel("Usage %")
    ax.set_title("CPU Usage per Core")
    ax.legend(loc="lower right", fontsize=8)
    ax.xaxis.set_major_formatter(mticker.PercentFormatter())
    fig.tight_layout()
    return data, _fig_to_b64(fig)


def _ram_with_chart() -> tuple:
    data = get_ram_usage()
    ram = data["ram"]
    swap = data["swap"]

    fig, ax = plt.subplots(figsize=(7, 2.5))
    ax.barh(["RAM"],  [ram["used_gb"]],                                    color="#e74c3c", label="Used")
    ax.barh(["RAM"],  [ram["available_gb"]], left=[ram["used_gb"]],         color="#2ecc71", label="Available")
    ax.barh(["Swap"], [swap["used_gb"]],                                    color="#e67e22")
    ax.barh(["Swap"], [swap["total_gb"] - swap["used_gb"]], left=[swap["used_gb"]], color="#95a5a6")
    ax.set_xlabel("GB")
    ax.set_title("Memory Usage")
    ax.legend(loc="lower right", fontsize=8)
    for bar in ax.patches:
        w = bar.get_width()
        if w > 0.3:
            ax.text(bar.get_x() + w / 2, bar.get_y() + bar.get_height() / 2,
                    f"{w:.1f} GB", ha="center", va="center", fontsize=7, color="white")
    fig.tight_layout()
    return data, _fig_to_b64(fig)


def _gpu_with_chart():
    data = get_gpu_usage()
    if "error" in data or not data.get("gpus"):
        return data

    gpus = data["gpus"]
    x = list(range(len(gpus)))
    w = 0.25

    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.bar([i - w for i in x], [g["load_percent"]   for g in gpus], width=w, label="Load %",  color="#3498db")
    ax.bar([i      for i in x], [g["memory_percent"] for g in gpus], width=w, label="VRAM %",  color="#9b59b6")
    ax.bar([i + w  for i in x], [g.get("temperature_c") or 0  for g in gpus], width=w, label="Temp °C", color="#e74c3c")
    ax.set_xticks(x)
    ax.set_xticklabels([g["name"] for g in gpus], fontsize=8)
    ax.set_ylim(0, 110)
    ax.set_ylabel("% / °C")
    ax.set_title("GPU Metrics")
    ax.legend(fontsize=8)
    fig.tight_layout()
    return data, _fig_to_b64(fig)


def get_hardware_profile(use_case: str = "") -> dict:
    """Aggregate hardware specs, live pressure, OC capability, upgrade feasibility, and use-case bottleneck analysis."""
    specs     = get_device_specs()
    cpu_live  = get_cpu_usage()
    ram_live  = get_ram_usage()
    gpu_data  = get_gpu_usage()

    system    = specs["os"]["system"]
    machine   = specs["os"]["machine"]
    cpu_brand = specs["cpu"]["brand"]
    cpu_pct   = cpu_live["total_percent"]
    ram_pct   = ram_live["ram"]["percent_used"]

    return {
        "use_case": use_case,
        "hardware": {
            "cpu":    specs["cpu"],
            "ram":    {"total_gb": specs["ram"]["total_gb"]},
            "gpu":    specs["gpus"],
            "disks":  specs["disks"],
        },
        "current_pressure": {
            "cpu": {"percent": cpu_pct, "level": _classify_pressure(cpu_pct)},
            "ram": {"percent": ram_pct, "level": _classify_pressure(ram_pct)},
        },
        "platform": {
            "system":           system,
            "machine":          machine,
            "is_apple_silicon": machine == "arm64" and system == "Darwin",
        },
        "overclocking": {
            "cpu": _detect_cpu_oc(cpu_brand, system, machine),
            "gpu": _detect_gpu_oc(system, machine, gpu_data),
        },
        "upgrade_feasibility": _get_upgrade_feasibility(system, machine),
        "use_case_analysis":   _use_case_analysis(use_case, cpu_pct, ram_pct),
    }


def get_battery_status() -> dict:
    batt = psutil.sensors_battery()
    if batt is None:
        return {"error": "No battery detected (desktop or unsupported platform)"}
    return {
        "percent": round(batt.percent, 1),
        "plugged_in": batt.power_plugged,
        "time_remaining_min": round(batt.secsleft / 60, 1) if batt.secsleft > 0 else None,
    }


def get_temperature_sensors() -> dict:
    system = platform.system()
    if system == "Darwin":
        return {
            "platform": "macOS",
            "available": False,
            "sensors": {},
            "message": (
                "psutil cannot access CPU/motherboard sensors on macOS. "
                "Alternatives: (1) GPU temp via get_gpu_usage if discrete GPU present. "
                "(2) iStatMenus or HWMonitor for full sensor access. "
                "(3) On Apple Silicon, thermal throttling shows as current_mhz << max_mhz in get_cpu_usage."
            ),
        }
    if not hasattr(psutil, "sensors_temperatures"):
        return {
            "platform": system,
            "available": False,
            "sensors": {},
            "message": "psutil.sensors_temperatures() not available on this platform/version.",
        }
    try:
        raw = psutil.sensors_temperatures()
    except Exception as e:
        return {"platform": system, "available": False, "sensors": {}, "message": f"Failed to read sensors: {e}"}
    if not raw:
        return {
            "platform": system,
            "available": True,
            "sensors": {},
            "message": "No sensors detected (may require elevated privileges on Linux).",
        }
    sensors = {}
    for chip, entries in raw.items():
        sensors[chip] = [
            {
                "label": e.label or chip,
                "current_c": round(e.current, 1) if e.current is not None else None,
                "high_c": round(e.high, 1) if e.high is not None else None,
                "critical_c": round(e.critical, 1) if e.critical is not None else None,
            }
            for e in entries
        ]
    return {"platform": system, "available": True, "message": "", "sensors": sensors}


def get_system_uptime() -> dict:
    boot = psutil.boot_time()
    elapsed = int(datetime.datetime.now().timestamp() - boot)
    return {
        "boot_time": datetime.datetime.fromtimestamp(boot).isoformat(),
        "uptime": {
            "days": elapsed // 86400,
            "hours": (elapsed % 86400) // 3600,
            "minutes": (elapsed % 3600) // 60,
        },
        "load_avg_1_5_15min": list(psutil.getloadavg()),
    }


def get_system_alerts() -> dict:
    alerts = []

    def _alert(severity, resource, message, value):
        alerts.append({"severity": severity, "resource": resource, "message": message, "value": value})

    cpu_pct = psutil.cpu_percent(interval=0.5)
    if cpu_pct >= 90:
        _alert("critical", "cpu", f"CPU usage critically high at {cpu_pct}%", cpu_pct)
    elif cpu_pct >= 75:
        _alert("warning", "cpu", f"CPU usage elevated at {cpu_pct}%", cpu_pct)

    vm = psutil.virtual_memory()
    if vm.percent >= 90:
        _alert("critical", "ram", f"RAM critically high at {vm.percent}%", vm.percent)
    elif vm.percent >= 75:
        _alert("warning", "ram", f"RAM elevated at {vm.percent}%", vm.percent)

    try:
        sw = psutil.swap_memory()
        if sw.total > 0 and sw.percent >= 80:
            _alert("warning", "swap", f"Swap high at {sw.percent}% — system may be memory-constrained", sw.percent)
    except Exception:
        pass

    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            if usage.percent >= 95:
                _alert("critical", f"disk:{part.mountpoint}", f"Disk {part.mountpoint} almost full at {usage.percent}%", usage.percent)
            elif usage.percent >= 85:
                _alert("warning", f"disk:{part.mountpoint}", f"Disk {part.mountpoint} getting full at {usage.percent}%", usage.percent)
        except (PermissionError, OSError):
            continue

    if GPU_AVAILABLE:
        try:
            for g in GPUtil.getGPUs():
                load_pct = round(g.load * 100, 1) if g.load is not None else None
                if load_pct is not None and load_pct >= 95:
                    _alert("critical", f"gpu:{g.id}", f"GPU {g.name} load critically high at {load_pct}%", load_pct)
                if g.temperature is not None:
                    if g.temperature >= 85:
                        _alert("critical", f"gpu:{g.id}", f"GPU {g.name} temp critically high at {g.temperature}°C", g.temperature)
                    elif g.temperature >= 75:
                        _alert("warning", f"gpu:{g.id}", f"GPU {g.name} temp elevated at {g.temperature}°C", g.temperature)
        except Exception:
            pass

    batt = psutil.sensors_battery()
    if batt is not None and not batt.power_plugged and batt.percent <= 10:
        _alert("critical", "battery", f"Battery critically low at {batt.percent}% and not plugged in", batt.percent)

    has_critical = any(a["severity"] == "critical" for a in alerts)
    critical_n = sum(1 for a in alerts if a["severity"] == "critical")
    warning_n = sum(1 for a in alerts if a["severity"] == "warning")
    if not alerts:
        summary = "All systems nominal — no alerts detected."
    elif has_critical:
        summary = f"{critical_n} critical and {warning_n} warning alert(s) detected. Immediate attention recommended."
    else:
        summary = f"{len(alerts)} warning(s) detected. System under stress but not critical."

    return {
        "alerts": alerts,
        "alert_count": len(alerts),
        "has_critical": has_critical,
        "summary": summary,
    }


def get_network_connections() -> dict:
    connections = []
    for conn in psutil.net_connections(kind="inet"):
        try:
            proc_name = psutil.Process(conn.pid).name() if conn.pid else None
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            proc_name = None
        connections.append({
            "proto": "tcp" if conn.type == socket.SOCK_STREAM else "udp",
            "local": f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else None,
            "remote": f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else None,
            "status": conn.status,
            "pid": conn.pid,
            "process": proc_name,
        })
    return {"connections": connections, "total": len(connections)}


def get_startup_items() -> dict:
    system = platform.system()

    if system == "Darwin":
        scan_dirs = [
            (pathlib.Path.home() / "Library" / "LaunchAgents", "user"),
            (pathlib.Path("/Library/LaunchAgents"), "system"),
            (pathlib.Path("/Library/LaunchDaemons"), "system-daemon"),
        ]
        items = []
        for directory, scope in scan_dirs:
            if not directory.exists():
                continue
            for plist_path in sorted(directory.glob("*.plist")):
                try:
                    with open(plist_path, "rb") as f:
                        data = plistlib.load(f)
                    prog_args = data.get("ProgramArguments", [])
                    command = " ".join(str(a) for a in prog_args) if prog_args else data.get("Program", "")
                    items.append({
                        "name": data.get("Label") or plist_path.stem,
                        "command": command,
                        "path": str(plist_path),
                        "scope": scope,
                        "run_at_load": bool(data.get("RunAtLoad", False)),
                    })
                except (plistlib.InvalidFileException, OSError, KeyError, TypeError):
                    items.append({
                        "name": plist_path.stem,
                        "command": "",
                        "path": str(plist_path),
                        "scope": scope,
                        "run_at_load": None,
                        "parse_error": True,
                    })
        return {"platform": "macOS", "items": items, "count": len(items)}

    if system == "Windows":
        try:
            import winreg
        except ImportError:
            return {"platform": "Windows", "error": "winreg not available", "items": [], "count": 0}
        items = []
        for hive, reg_path, scope in [
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "user"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "system"),
        ]:
            try:
                key = winreg.OpenKey(hive, reg_path, 0, winreg.KEY_READ)
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                        items.append({"name": name, "command": value, "scope": scope})
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)
            except OSError:
                continue
        return {"platform": "Windows", "items": items, "count": len(items)}

    if system == "Linux":
        autostart = pathlib.Path.home() / ".config" / "autostart"
        items = []
        if autostart.exists():
            for dp in sorted(autostart.glob("*.desktop")):
                try:
                    text = dp.read_text(encoding="utf-8", errors="replace")
                    name = ""
                    command = ""
                    hidden = False
                    for line in text.splitlines():
                        if line.startswith("Name="):
                            name = line[5:].strip()
                        elif line.startswith("Exec="):
                            command = line[5:].strip()
                        elif line.startswith("Hidden="):
                            hidden = line[7:].strip().lower() == "true"
                    items.append({
                        "name": name or dp.stem,
                        "command": command,
                        "path": str(dp),
                        "scope": "user",
                        "hidden": hidden,
                    })
                except OSError:
                    continue
        return {"platform": "Linux", "items": items, "count": len(items)}

    return {"platform": system, "error": f"Not supported on {system}", "items": [], "count": 0}


def get_process_details(pid: int) -> dict:
    if pid <= 0:
        return {"error": f"Invalid PID {pid}: must be a positive integer"}
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            return {
                "pid": pid,
                "name": p.name(),
                "exe": _safe(p.exe),
                "cmdline": _safe(lambda: " ".join(p.cmdline())),
                "user": _safe(p.username),
                "status": p.status(),
                "created": datetime.datetime.fromtimestamp(p.create_time()).isoformat(),
                "cpu_percent": p.cpu_percent(interval=0.2),
                "memory": {
                    "rss_mb": round(p.memory_info().rss / 1e6, 2),
                    "vms_mb": round(p.memory_info().vms / 1e6, 2),
                    "percent": round(p.memory_percent(), 2),
                },
                "threads": p.num_threads(),
                "open_files": _safe(lambda: len(p.open_files())),
            }
    except psutil.NoSuchProcess:
        return {"error": f"No process with PID {pid}"}


def search_process(name: str) -> dict:
    if not name or not name.strip():
        return {
            "error": "Search query cannot be empty",
            "query": name,
            "matches": [],
            "count": 0,
        }
    name = name.strip()
    name_lower = name.lower()
    matches = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
        try:
            if name_lower in (p.info['name'] or '').lower():
                matches.append({
                    "pid": p.info['pid'],
                    "name": p.info['name'],
                    "cpu_percent": round(p.info['cpu_percent'] or 0, 2),
                    "memory_percent": round(p.info['memory_percent'] or 0, 2),
                    "status": p.info['status'],
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"query": name, "matches": matches, "count": len(matches)}


def kill_process(pid: int, force: bool = False) -> dict:
    if pid <= 0:
        return {"success": False, "error": f"Invalid PID {pid}: must be a positive integer"}
    if pid in _PROTECTED_PIDS:
        return {"success": False, "error": f"Refusing to kill PID {pid}: protected system process"}
    try:
        p = psutil.Process(pid)
        proc_name = p.name()
    except psutil.NoSuchProcess:
        return {"success": False, "error": f"No process with PID {pid}"}
    except psutil.AccessDenied:
        return {"success": False, "error": f"Access denied reading PID {pid}"}

    if proc_name.lower() in _PROTECTED_NAMES:
        return {
            "success": False,
            "error": f"Refusing to kill '{proc_name}' (PID {pid}): critical system process",
        }

    try:
        if force:
            p.kill()
            method = "SIGKILL"
        else:
            p.terminate()
            method = "SIGTERM"
        return {
            "success": True,
            "pid": pid,
            "name": proc_name,
            "signal": method,
            "message": f"Sent {method} to '{proc_name}' (PID {pid})",
        }
    except psutil.NoSuchProcess:
        return {"success": False, "error": f"Process {pid} exited before signal could be sent"}
    except psutil.AccessDenied:
        return {
            "success": False,
            "error": f"Access denied killing '{proc_name}' (PID {pid}). May require elevated privileges.",
        }


def get_device_specs() -> dict:
    """Return static hardware and OS specifications."""
    vm = psutil.virtual_memory()
    freq = psutil.cpu_freq()

    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": round(usage.total / 1e9, 2),
            })
        except PermissionError:
            continue

    gpu_specs = []
    if GPU_AVAILABLE:
        for g in GPUtil.getGPUs():
            gpu_specs.append({
                "name": g.name,
                "vram_total_mb": round(g.memoryTotal, 1),
            })

    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "hostname": platform.node(),
        },
        "cpu": {
            "brand": platform.processor(),
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
            "max_frequency_mhz": round(freq.max, 1) if freq else None,
        },
        "ram": {
            "total_gb": round(vm.total / 1e9, 2),
        },
        "gpus": gpu_specs or [{"error": "GPUtil not installed or no GPUs detected"}],
        "disks": disks,
    }


def get_full_snapshot() -> dict:
    """Aggregate snapshot of all metrics."""
    return {
        "cpu": get_cpu_usage(),
        "ram": get_ram_usage(),
        "gpu": get_gpu_usage(),
        "disk": get_disk_usage(),
        "network": get_network_usage(),
        "top_processes_by_cpu": get_top_processes(5, "cpu")["top_processes"],
        "top_processes_by_memory": get_top_processes(5, "memory")["top_processes"],
    }


# ── Tool registry ─────────────────────────────────────────────────────────────

TOOLS = {
    "get_cpu_usage": {
        "description": "Returns CPU usage percentage (total and per-core), core count, and frequency, with an inline bar chart.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: _cpu_with_chart(),
    },
    "get_ram_usage": {
        "description": "Returns RAM and swap memory usage (total, used, available, percent), with an inline stacked bar chart.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: _ram_with_chart(),
    },
    "get_gpu_usage": {
        "description": "Returns GPU load, VRAM usage, and temperature (requires gputil), with an inline grouped bar chart.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: _gpu_with_chart(),
    },
    "get_disk_usage": {
        "description": "Returns disk partition usage and I/O counters.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_disk_usage(),
    },
    "get_network_usage": {
        "description": "Returns total bytes sent/received and network interface status.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_network_usage(),
    },
    "get_realtime_io": {
        "description": "Measures actual disk and network I/O throughput by sampling twice over an interval. Returns disk read/write in MB/s and network download/upload in MB/s and Mbps. Call this instead of get_disk_usage or get_network_usage when the user asks about current speed or throughput.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "interval": {
                    "type": "integer",
                    "description": "Sampling interval in seconds (1–3). Default 1.",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 3
                }
            },
            "required": []
        },
        "fn": lambda args: get_realtime_io(args.get("interval", 1)),
    },
    "get_top_processes": {
        "description": "Returns the top N resource-hungry processes sorted by CPU or memory usage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of processes to return (default 10)", "default": 10},
                "sort_by": {"type": "string", "enum": ["cpu", "memory"], "description": "Sort by 'cpu' or 'memory'", "default": "cpu"}
            },
            "required": []
        },
        "fn": lambda args: get_top_processes(args.get("n", 10), args.get("sort_by", "cpu")),
    },
    "get_full_snapshot": {
        "description": "Returns a full system snapshot: CPU, RAM, GPU, disk, network, and top processes.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_full_snapshot(),
    },
    "get_device_specs": {
        "description": "Returns static hardware specifications: CPU model, core count, total RAM, GPU model and VRAM, disk capacities, and OS details.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_device_specs(),
    },
    "get_battery_status": {
        "description": "Returns battery percentage, charging state, and estimated time remaining. Returns an error on desktops with no battery.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_battery_status(),
    },
    "get_temperature_sensors": {
        "description": "Returns CPU and motherboard temperature sensor readings. On macOS, returns a helpful message with alternatives (psutil cannot access kernel sensors on Darwin). On Linux/Windows, returns sensor groups with current, high, and critical thresholds.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_temperature_sensors(),
    },
    "get_system_uptime": {
        "description": "Returns how long the system has been running, the last boot time, and the 1/5/15-minute load averages.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_system_uptime(),
    },
    "get_system_alerts": {
        "description": "Scans all key system metrics (CPU, RAM, swap, disk partitions, GPU, battery) and returns a prioritized list of critical/warning alerts. Call this first for general 'why is my machine slow?' questions as a quick triage tool.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_system_alerts(),
    },
    "get_network_connections": {
        "description": "Returns all active TCP/UDP connections with local/remote addresses, status, and the owning process name.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_network_connections(),
    },
    "get_startup_items": {
        "description": "Lists applications and services configured to launch automatically at startup/login. macOS: scans ~/Library/LaunchAgents, /Library/LaunchAgents, /Library/LaunchDaemons. Windows: reads Run registry keys. Linux: scans ~/.config/autostart. Use when the user asks what runs at startup or wants to speed up boot times.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_startup_items(),
    },
    "get_process_details": {
        "description": "Returns detailed information about a specific process by PID: executable path, command line, user, memory breakdown, open file count, and more.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "The process ID to inspect"}
            },
            "required": ["pid"]
        },
        "fn": lambda args: get_process_details(args["pid"]),
    },
    "search_process": {
        "description": "Searches for running processes by name (case-insensitive, partial match). Returns PID, CPU%, memory%, and status for each match.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Process name to search for (e.g. 'chrome', 'python')"}
            },
            "required": ["name"]
        },
        "fn": lambda args: search_process(args["name"]),
    },
    "kill_process": {
        "description": "Terminates a process by PID. Sends SIGTERM (graceful) by default; SIGKILL if force=True. Refuses to kill critical system processes (PID 1, launchd, systemd, init, kernel_task, core Windows services). Always confirm with the user before calling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "The PID of the process to terminate"},
                "force": {"type": "boolean", "description": "If true, send SIGKILL (immediate). Default false (SIGTERM, graceful).", "default": False}
            },
            "required": ["pid"]
        },
        "fn": lambda args: kill_process(args["pid"], args.get("force", False)),
    },
    "get_hardware_profile": {
        "description": "Returns a full hardware profile for a given use-case: specs, live pressure, overclocking capability (where supported), upgrade feasibility per component, and workload-specific bottleneck analysis. Use this when the user asks about speeding up a specific task, upgrading their machine, or overclocking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "use_case": {
                    "type": "string",
                    "description": "The user's workload or goal, e.g. 'lightroom rendering', 'gaming', 'video editing', 'compiling code'"
                }
            },
            "required": []
        },
        "fn": lambda args: get_hardware_profile(args.get("use_case", "")),
    },
}


# ── MCP request dispatcher ────────────────────────────────────────────────────

def handle_request(request: dict) -> dict | None:
    method = request.get("method")
    id_ = request.get("id")
    params = request.get("params", {})

    # Notifications have no "id" — must never be responded to
    if "id" not in request:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "system-monitor", "version": "1.0.0"},
            }
        }

    if method == "tools/list":
        tools_list = [
            {
                "name": name,
                "description": meta["description"],
                "inputSchema": meta["inputSchema"],
            }
            for name, meta in TOOLS.items()
        ]
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": tools_list}}

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})
        if tool_name not in TOOLS:
            return make_error(id_, -32601, f"Unknown tool: {tool_name}")
        try:
            result = TOOLS[tool_name]["fn"](args)
            if isinstance(result, tuple):
                data, img_b64 = result
                content = [
                    {"type": "text", "text": json.dumps(data, indent=2)},
                    {"type": "image", "data": img_b64, "mimeType": "image/png"},
                ]
            else:
                content = [{"type": "text", "text": json.dumps(result, indent=2)}]
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": content}}
        except Exception as e:
            return make_error(id_, -32603, str(e))

    # Ping / unknown
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}

    return make_error(id_, -32601, f"Method not found: {method}")


# ── stdio transport loop ──────────────────────────────────────────────────────

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(make_error(None, -32700, "Parse error")) + "\n")
            sys.stdout.flush()
            continue

        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
