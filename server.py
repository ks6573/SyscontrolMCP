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
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

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


# ── Reminder storage ──────────────────────────────────────────────────────────

_REMINDER_LOCK = threading.Lock()
_REMINDER_FILE = pathlib.Path.home() / ".syscontrol" / "reminders.json"


def _load_reminders() -> list:
    """Load reminders from disk. Creates file if missing. Must be called under _REMINDER_LOCK."""
    _REMINDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _REMINDER_FILE.exists():
        return []
    try:
        return json.loads(_REMINDER_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_reminders(reminders: list) -> None:
    """Write reminders to disk. Must be called under _REMINDER_LOCK."""
    _REMINDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _REMINDER_FILE.write_text(json.dumps(reminders, indent=2))


class ReminderChecker:
    """Background daemon thread that fires due reminders via macOS notifications."""

    def __init__(self):
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="syscontrol-reminders"
        )

    def start(self):
        self._thread.start()

    def _loop(self):
        while True:
            self._check()
            time.sleep(15)

    def _check(self):
        now = datetime.datetime.now()
        cutoff = now - datetime.timedelta(days=7)
        to_fire = []
        with _REMINDER_LOCK:
            reminders = _load_reminders()
            changed = False
            survivors = []
            for r in reminders:
                try:
                    fire_at = datetime.datetime.fromisoformat(r["fire_at"])
                except (ValueError, KeyError, TypeError):
                    changed = True  # drop malformed entry
                    continue
                if r.get("fired"):
                    # Prune fired reminders older than 7 days
                    if fire_at >= cutoff:
                        survivors.append(r)
                    else:
                        changed = True
                    continue
                if now >= fire_at:
                    to_fire.append(r["message"])
                    r["fired"] = True
                    changed = True
                survivors.append(r)
            if changed:
                _save_reminders(survivors)
        # Fire notifications outside the lock to avoid blocking set/list/cancel
        for msg in to_fire:
            self._fire(msg)

    @staticmethod
    def _fire(message: str):
        script = (
            f'display notification {json.dumps(message)} '
            f'with title "SysControl Reminder" sound name "default"'
        )
        log_path = pathlib.Path.home() / ".syscontrol" / "reminder_log.txt"
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode != 0:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a") as f:
                    ts = datetime.datetime.now().isoformat(timespec="seconds")
                    f.write(f"[{ts}] osascript failed (rc={proc.returncode}): {proc.stderr.strip()}\n")
        except Exception as exc:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a") as f:
                    ts = datetime.datetime.now().isoformat(timespec="seconds")
                    f.write(f"[{ts}] _fire exception: {exc}\n")
            except Exception:
                pass


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

# Directories skipped by find_large_files — defined once at module level
# so the set is not re-created on every call.
_FIND_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".Trash", "Library",
})

# FedEx tracking numbers are exactly 12, 15, or 22 digits.
_FEDEX_RE = re.compile(r"^\d{12}$|^\d{15}$|^\d{22}$")


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
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()
    finally:
        plt.close(fig)


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
    total = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
    freq = psutil.cpu_freq()
    return {
        "total_percent": total,
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
    disk_io = psutil.disk_io_counters()
    return {
        "partitions": partitions,
        "io_counters": {
            "read_mb": round(disk_io.read_bytes / 1e6, 2) if disk_io else None,
            "write_mb": round(disk_io.write_bytes / 1e6, 2) if disk_io else None,
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
    try:
        ax.bar([i - w for i in x], [g.get("load_percent") or 0 for g in gpus], width=w, label="Load %",  color="#3498db")
        ax.bar([i      for i in x], [g.get("memory_percent") or 0 for g in gpus], width=w, label="VRAM %",  color="#9b59b6")
        ax.bar([i + w  for i in x], [g.get("temperature_c") or 0  for g in gpus], width=w, label="Temp °C", color="#e74c3c")
        ax.set_xticks(x)
        ax.set_xticklabels([g["name"] for g in gpus], fontsize=8)
        ax.set_ylim(0, 110)
        ax.set_ylabel("% / °C")
        ax.set_title("GPU Metrics")
        ax.legend(fontsize=8)
        fig.tight_layout()
        return data, _fig_to_b64(fig)
    except Exception:
        plt.close(fig)
        return data


def get_hardware_profile(use_case: str = "") -> dict:
    """Aggregate hardware specs, live pressure, OC capability, upgrade feasibility, and use-case bottleneck analysis."""
    from concurrent.futures import ThreadPoolExecutor
    # Run all four independent data-source calls in parallel.
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_specs = ex.submit(get_device_specs)
        f_cpu   = ex.submit(get_cpu_usage)
        f_ram   = ex.submit(get_ram_usage)
        f_gpu   = ex.submit(get_gpu_usage)
    specs    = f_specs.result()
    cpu_live = f_cpu.result()
    ram_live = f_ram.result()
    gpu_data = f_gpu.result()

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
    try:
        raw_connections = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        return {"error": "Access denied. Network connection listing may require elevated privileges.", "connections": [], "total": 0}

    # Build a PID→name map once from process_iter instead of constructing
    # a new psutil.Process object for every connection (O(n) not O(n·k)).
    pid_to_name: dict[int, str] = {}
    for p in psutil.process_iter(["pid", "name"]):
        try:
            pid_to_name[p.info["pid"]] = p.info["name"] or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    connections = [
        {
            "proto":   "tcp" if conn.type == socket.SOCK_STREAM else "udp",
            "local":   f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else None,
            "remote":  f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else None,
            "status":  conn.status,
            "pid":     conn.pid,
            "process": pid_to_name.get(conn.pid) if conn.pid else None,
        }
        for conn in raw_connections
    ]
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
            mem = p.memory_info()
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
                    "rss_mb": round(mem.rss / 1e6, 2),
                    "vms_mb": round(mem.vms / 1e6, 2),
                    "percent": round(p.memory_percent(), 2),
                },
                "threads": p.num_threads(),
                "open_files": _safe(lambda: len(p.open_files())),
            }
    except psutil.NoSuchProcess:
        return {"error": f"No process with PID {pid}"}
    except psutil.AccessDenied:
        return {"error": f"Access denied reading process details for PID {pid}"}


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
    """Aggregate snapshot of all metrics — all sources fetched in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_cpu     = ex.submit(get_cpu_usage)
        f_ram     = ex.submit(get_ram_usage)
        f_gpu     = ex.submit(get_gpu_usage)
        f_disk    = ex.submit(get_disk_usage)
        f_net     = ex.submit(get_network_usage)
        f_top_cpu = ex.submit(get_top_processes, 5, "cpu")
        f_top_mem = ex.submit(get_top_processes, 5, "memory")
    return {
        "cpu":                    f_cpu.result(),
        "ram":                    f_ram.result(),
        "gpu":                    f_gpu.result(),
        "disk":                   f_disk.result(),
        "network":                f_net.result(),
        "top_processes_by_cpu":    f_top_cpu.result()["top_processes"],
        "top_processes_by_memory": f_top_mem.result()["top_processes"],
    }


# ── Agentic tool helpers ───────────────────────────────────────────────────────

_RELATIVE_UNITS = {
    "second": 1, "seconds": 1,
    "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
}


def _parse_reminder_time(s: str):
    """Parse natural-language time string into a datetime. Returns None on failure."""
    s = s.strip().lower()
    now = datetime.datetime.now()

    # "in 2 hours 30 minutes" (compound)
    m = re.match(r"in\s+(\d+)\s+hours?\s+(?:and\s+)?(\d+)\s+minutes?", s)
    if m:
        return now + datetime.timedelta(hours=int(m.group(1)), minutes=int(m.group(2)))

    # "in 2 hours" / "in 30 minutes" / "in 1 day"
    m = re.match(r"in\s+(\d+)\s+(\w+)", s)
    if m:
        unit = _RELATIVE_UNITS.get(m.group(2))
        if unit:
            return now + datetime.timedelta(seconds=int(m.group(1)) * unit)

    # "tomorrow at 9:00 am" / "tomorrow at 3pm"
    m = re.match(r"tomorrow\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        period = m.group(3)
        if period == "pm" and hour < 12: hour += 12
        if period == "am" and hour == 12: hour = 0
        return (now + datetime.timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    # "at 9:00 am" / "at 14:30" / "at 3pm"
    m = re.match(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        period = m.group(3)
        if period == "pm" and hour < 12: hour += 12
        if period == "am" and hour == 12: hour = 0
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return target

    return None


def _human_timedelta(delta: datetime.timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 0: return "overdue"
    if secs < 60: return f"{secs} seconds"
    if secs < 3600: return f"{secs // 60} minutes"
    if secs < 86400: return f"{secs // 3600} hours {(secs % 3600) // 60} minutes"
    return f"{secs // 86400} days"


# ── Reminder tools ────────────────────────────────────────────────────────────

def set_reminder(message: str, time_str: str) -> dict:
    fire_at = _parse_reminder_time(time_str)
    if fire_at is None:
        return {
            "success": False,
            "error": (
                f"Could not parse time '{time_str}'. "
                "Try: 'in 2 hours', 'in 30 minutes', 'at 9:00 am', 'at 3pm', 'tomorrow at 8am'."
            ),
        }
    reminder_id = uuid.uuid4().hex[:8]
    entry = {
        "id": reminder_id,
        "message": message,
        "fire_at": fire_at.isoformat(),
        "created_at": datetime.datetime.now().isoformat(),
        "fired": False,
    }
    with _REMINDER_LOCK:
        reminders = _load_reminders()
        reminders.append(entry)
        _save_reminders(reminders)
    return {
        "success": True,
        "id": reminder_id,
        "message": message,
        "fires_at": fire_at.strftime("%Y-%m-%d %I:%M %p"),
        "fires_in": _human_timedelta(fire_at - datetime.datetime.now()),
    }


def list_reminders() -> dict:
    with _REMINDER_LOCK:
        reminders = _load_reminders()
    now = datetime.datetime.now()
    pending = [r for r in reminders if not r["fired"]]
    return {
        "count": len(pending),
        "reminders": [
            {
                "id": r["id"],
                "message": r["message"],
                "fires_at": r["fire_at"],
                "fires_in": _human_timedelta(
                    datetime.datetime.fromisoformat(r["fire_at"]) - now
                ),
            }
            for r in pending
        ],
    }


def cancel_reminder(reminder_id: str) -> dict:
    with _REMINDER_LOCK:
        reminders = _load_reminders()
        original_len = len(reminders)
        reminders = [r for r in reminders if not (r["id"] == reminder_id and not r["fired"])]
        if len(reminders) == original_len:
            return {"success": False, "error": f"No active reminder with id '{reminder_id}'"}
        _save_reminders(reminders)
    return {"success": True, "cancelled_id": reminder_id}


# ── Weather tool ──────────────────────────────────────────────────────────────

_WMO_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Freezing drizzle (light)", 57: "Freezing drizzle (heavy)",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Freezing rain (light)", 67: "Freezing rain (heavy)",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Light rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}

_SNOW_CODES = {71, 73, 75, 77, 85, 86}
_RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
_FOG_CODES  = {45, 48}


def _clothing_suggestions(temp_f: float, code: int, wind_mph: float, humidity_pct: float) -> list:
    suggestions = []
    if temp_f < 10:
        suggestions.append("Extreme cold: insulated parka, thermal underlayers, insulated waterproof boots, face mask, and thick gloves")
    elif temp_f < 25:
        suggestions.append("Heavy winter coat, thermal underlayers, warm hat, insulated gloves, and winter boots")
    elif temp_f < 40:
        suggestions.append("Winter coat, warm sweater or fleece, gloves, and a hat")
    elif temp_f < 55:
        suggestions.append("Medium jacket or fleece and long pants")
    elif temp_f < 68:
        suggestions.append("Light jacket or cardigan and long pants or jeans")
    elif temp_f < 80:
        suggestions.append("T-shirt or light long-sleeve and comfortable pants or shorts")
    else:
        suggestions.append("Light, breathable clothing — stay hydrated")

    if code in _SNOW_CODES:
        suggestions.append("Snow expected: wear waterproof boots and a snow-resistant outer layer")
    elif code in _RAIN_CODES:
        suggestions.append("Rain expected: bring a rain jacket or umbrella and waterproof footwear")
    elif code in _FOG_CODES:
        suggestions.append("Foggy conditions: drive carefully and use low-beam headlights")

    if wind_mph >= 25:
        suggestions.append("Strong winds: a windproof outer layer is important")
    elif wind_mph >= 15:
        suggestions.append("Breezy: a windbreaker helps")

    if temp_f >= 75 and humidity_pct >= 70:
        suggestions.append("High humidity: moisture-wicking fabrics recommended")

    return suggestions


def get_weather(location: str = "", units: str = "imperial") -> dict:
    units = units if units in ("imperial", "metric") else "imperial"
    temp_unit  = "fahrenheit" if units == "imperial" else "celsius"
    wind_unit  = "mph" if units == "imperial" else "kmh"
    temp_symbol = "°F" if units == "imperial" else "°C"
    speed_label = "mph" if units == "imperial" else "km/h"

    try:
        if location.strip():
            # Geocode named location via Nominatim (OpenStreetMap)
            encoded = urllib.parse.quote(location.strip())
            url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1"
            req = urllib.request.Request(url, headers={"User-Agent": "syscontrol-mcp/0.1"})
            with urllib.request.urlopen(req, timeout=8) as r:
                geo_data = json.loads(r.read().decode())
            if not geo_data:
                return {"error": f"Location '{location}' not found. Try a different city name."}
            lat = float(geo_data[0]["lat"])
            lon = float(geo_data[0]["lon"])
            display = geo_data[0].get("display_name", location)
            parts = [p.strip() for p in display.split(",")]
            city_name = parts[0]
            country = parts[-1] if len(parts) > 1 else ""
            region = parts[1] if len(parts) > 2 else ""
            location_source = "geocode"
        else:
            # Auto-detect from IP via ipinfo.io
            with urllib.request.urlopen("https://ipinfo.io/json", timeout=8) as r:
                ip_data = json.loads(r.read().decode())
            loc_str = ip_data.get("loc", "0,0")
            lat, lon = map(float, loc_str.split(","))
            city_name = ip_data.get("city", "Unknown")
            region = ip_data.get("region", "")
            country = ip_data.get("country", "")
            location_source = "ip_geolocation"

        # Fetch weather from Open-Meteo (free, no API key)
        params = (
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
            f"precipitation,weathercode,windspeed_10m,is_day"
            f"&temperature_unit={temp_unit}&wind_speed_unit={wind_unit}"
            f"&precipitation_unit={'inch' if units == 'imperial' else 'mm'}"
            f"&forecast_days=1"
        )
        weather_url = f"https://api.open-meteo.com/v1/forecast?{params}"
        with urllib.request.urlopen(weather_url, timeout=10) as r:
            weather_data = json.loads(r.read().decode())

        current = weather_data["current"]
        temp      = current["temperature_2m"]
        feels_like = current["apparent_temperature"]
        humidity  = current["relative_humidity_2m"]
        wind      = current["windspeed_10m"]
        precip    = current["precipitation"]
        code      = current["weathercode"]
        is_day    = bool(current["is_day"])

        # Convert to °F for clothing logic when units=metric
        temp_f   = temp if units == "imperial" else (temp * 9 / 5 + 32)
        wind_mph = wind if units == "imperial" else wind * 0.621371
        condition = _WMO_DESCRIPTIONS.get(code, f"Weather code {code}")
        clothing  = _clothing_suggestions(temp_f, code, wind_mph, humidity)

        return {
            "location": {
                "city": city_name,
                "region": region,
                "country": country,
                "coordinates": {"lat": round(lat, 4), "lon": round(lon, 4)},
                "source": location_source,
            },
            "current": {
                "temperature":  {"value": round(temp, 1), "unit": temp_symbol},
                "feels_like":   {"value": round(feels_like, 1), "unit": temp_symbol},
                "humidity_percent": humidity,
                "wind_speed":   {"value": round(wind, 1), "unit": speed_label},
                "precipitation": {"value": round(precip, 2), "unit": "in" if units == "imperial" else "mm"},
                "condition":    condition,
                "condition_code": code,
                "is_day": is_day,
            },
            "clothing_suggestions": clothing,
        }
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return {"error": f"Network error: {str(e)}. Check your internet connection."}
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return {"error": f"Failed to parse weather data: {str(e)}"}


# ── App update checker ────────────────────────────────────────────────────────

def check_app_updates() -> dict:
    if platform.system() != "Darwin":
        return {"error": "check_app_updates is currently macOS-only."}

    results: dict = {
        "brew_formulae": [],
        "brew_casks": [],
        "mac_app_store": [],
        "system_updates": [],
        "errors": [],
        "summary": "",
    }
    lock = threading.Lock()

    def _brew():
        if not shutil.which("brew"):
            with lock:
                results["errors"].append("Homebrew not installed — install from https://brew.sh")
            return
        try:
            proc = subprocess.run(
                ["brew", "outdated", "--json=v2"],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"},
            )
            if proc.returncode in (0, 1) and proc.stdout.strip():
                data = json.loads(proc.stdout)
                formulae = [
                    {
                        "name": f["name"],
                        "installed": f["installed_versions"][0] if f.get("installed_versions") else "?",
                        "available": f.get("current_version", "?"),
                    }
                    for f in data.get("formulae", [])
                ]
                casks = [
                    {
                        "name": c["name"],
                        "installed": c.get("installed_versions", ["?"])[0],
                        "available": c.get("current_version", "?"),
                    }
                    for c in data.get("casks", [])
                ]
                with lock:
                    results["brew_formulae"] = formulae
                    results["brew_casks"]    = casks
            elif proc.returncode not in (0, 1):
                with lock:
                    results["errors"].append(f"brew error: {proc.stderr.strip()[:200]}")
        except subprocess.TimeoutExpired:
            with lock:
                results["errors"].append("brew outdated timed out (>120s)")
        except (json.JSONDecodeError, OSError) as e:
            with lock:
                results["errors"].append(f"brew parse error: {str(e)}")

    def _mas():
        if not shutil.which("mas"):
            with lock:
                results["errors"].append(
                    "mas not installed — install with 'brew install mas' to check App Store updates"
                )
            return
        try:
            proc = subprocess.run(
                ["mas", "outdated"],
                capture_output=True, text=True, timeout=60,
            )
            apps = []
            for line in proc.stdout.splitlines():
                m = re.match(r"(\d+)\s+(.+?)\s+\((.+?)\)", line.strip())
                if m:
                    apps.append({
                        "app_id": m.group(1),
                        "name":   m.group(2).strip(),
                        "available_version": m.group(3),
                    })
            with lock:
                results["mac_app_store"] = apps
        except subprocess.TimeoutExpired:
            with lock:
                results["errors"].append("mas outdated timed out (>60s)")
        except OSError as e:
            with lock:
                results["errors"].append(f"mas error: {str(e)}")

    def _sysupdate():
        if not shutil.which("softwareupdate"):
            return
        try:
            proc = subprocess.run(
                ["softwareupdate", "-l"],
                capture_output=True, text=True, timeout=60,
            )
            combined = proc.stdout + proc.stderr
            current_label = None
            updates = []
            for line in combined.splitlines():
                stripped = line.strip()
                if stripped.startswith("* Label:"):
                    current_label = stripped.split(":", 1)[1].strip()
                elif current_label and "Title:" in stripped:
                    m = re.search(r"Title:\s*(.+?),\s*Version:\s*([\d.]+)", stripped)
                    if m:
                        updates.append({
                            "label":   current_label,
                            "title":   m.group(1).strip(),
                            "version": m.group(2),
                        })
                    current_label = None
            with lock:
                results["system_updates"] = updates
        except subprocess.TimeoutExpired:
            with lock:
                results["errors"].append("softwareupdate timed out (>60s)")
        except OSError as e:
            with lock:
                results["errors"].append(f"softwareupdate error: {str(e)}")

    # Run all three checks concurrently — brew alone can take 30–120s.
    threads = [
        threading.Thread(target=_brew,      daemon=True),
        threading.Thread(target=_mas,       daemon=True),
        threading.Thread(target=_sysupdate, daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=130)   # brew timeout is 120s; add a small buffer

    total = (
        len(results["brew_formulae"]) + len(results["brew_casks"])
        + len(results["mac_app_store"]) + len(results["system_updates"])
    )
    if total == 0:
        results["summary"] = "All apps are up to date."
    else:
        parts = []
        if results["brew_formulae"]:
            n = len(results["brew_formulae"])
            parts.append(f"{n} Homebrew formula{'e' if n != 1 else ''}")
        if results["brew_casks"]:
            n = len(results["brew_casks"])
            parts.append(f"{n} Homebrew cask{'s' if n != 1 else ''}")
        if results["mac_app_store"]:
            n = len(results["mac_app_store"])
            parts.append(f"{n} App Store app{'s' if n != 1 else ''}")
        if results["system_updates"]:
            n = len(results["system_updates"])
            parts.append(f"{n} system update{'s' if n != 1 else ''}")
        results["summary"] = f"{total} update{'s' if total != 1 else ''} available: " + ", ".join(parts)

    return results


# ── Package tracking ──────────────────────────────────────────────────────────

def _detect_carrier(tn: str) -> str:
    tn = re.sub(r"\s+", "", tn).upper()
    if tn.startswith("TBA"):                           return "amazon_logistics"
    if re.match(r"^1Z[A-Z0-9]{16}$", tn):             return "ups"
    if re.match(r"^(94|93|92|91|90)\d{18,20}$", tn): return "usps"
    if re.match(r"^[A-Z]{2}\d{9}[A-Z]{2}$", tn):     return "usps"
    if _FEDEX_RE.match(tn):                            return "fedex"   # 12, 15, or 22 digits
    if re.match(r"^\d{20,21}$", tn):                  return "usps"
    if re.match(r"^\d{10,11}$", tn):                  return "dhl"
    if re.match(r"^(JD|GM)\d{14,20}$", tn):           return "dhl"
    return "unknown"


_17TRACK_STATUS_MAP = {
    10: "Not found / No information",
    20: "In transit",
    30: "Out for delivery",
    40: "Delivered",
    50: "Exception / Alert",
}

_17TRACK_CARRIER_NAMES = {
    100001: "UPS", 100002: "USPS", 100003: "FedEx",
    100004: "DHL", 100007: "Amazon Logistics", 100008: "DHL Express",
    100010: "Canada Post", 100012: "Australia Post", 100016: "La Poste",
}


def track_package(tracking_number: str) -> dict:
    tn_clean = re.sub(r"\s+", "", tracking_number).upper()
    carrier  = _detect_carrier(tn_clean)

    if carrier == "amazon_logistics":
        return {
            "tracking_number": tracking_number,
            "detected_carrier": "Amazon Logistics",
            "status": "Cannot track via this tool",
            "note": (
                "Amazon Logistics (TBA tracking numbers) can only be tracked at "
                "amazon.com/orders. Standard carrier tracking is not available for these."
            ),
        }

    try:
        payload = json.dumps({"number": tn_clean}).encode()
        req = urllib.request.Request(
            "https://t.17track.net/restapi/track",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            resp = json.loads(r.read().decode())

        if not resp.get("shipments"):
            return {
                "tracking_number": tracking_number,
                "detected_carrier": carrier,
                "status": "Not found",
                "note": "No tracking information found. The package may not yet be in the system.",
            }

        shipment = resp["shipments"][0]
        carrier_code    = shipment.get("carrier")
        reported_carrier = _17TRACK_CARRIER_NAMES.get(carrier_code, f"Carrier #{carrier_code}")

        track  = shipment.get("track", {})
        w1     = track.get("w1", {})
        latest = w1.get("z0", {})
        history_raw = w1.get("z1", [])

        status_code = latest.get("c", 10)
        status = _17TRACK_STATUS_MAP.get(status_code, f"Status code {status_code}")

        latest_event = {
            "description": latest.get("b", latest.get("a", "")),
            "location":    latest.get("e", ""),
            "timestamp":   latest.get("d", ""),
        }

        history = [
            {
                "timestamp":   e.get("a", ""),
                "description": e.get("b", ""),
                "location":    e.get("c", ""),
            }
            for e in history_raw[:10]
        ]

        return {
            "tracking_number":  tracking_number,
            "detected_carrier": carrier,
            "reported_carrier": reported_carrier,
            "status":      status,
            "status_code": status_code,
            "latest_event": latest_event,
            "history": history,
        }

    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return {"tracking_number": tracking_number, "error": f"Network error: {str(e)}"}
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        return {"tracking_number": tracking_number, "error": f"Failed to parse tracking response: {str(e)}"}


# ── New tool implementations ──────────────────────────────────────────────────

def find_large_files(path: str = "", n: int = 10) -> dict:
    """Find the top N largest files under path (default: home directory)."""
    root = pathlib.Path(path).expanduser().resolve() if path else pathlib.Path.home()
    if not root.exists():
        return {"error": f"Path '{path}' does not exist."}
    if not root.is_dir():
        return {"error": f"'{path}' is not a directory."}

    files: list[tuple[int, str]] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=None):
        # Prune noisy / hidden dirs in-place so os.walk skips them entirely.
        # Uses the module-level _FIND_SKIP_DIRS constant (not recreated per call).
        dirnames[:] = [
            d for d in dirnames
            if d not in _FIND_SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            fpath = pathlib.Path(dirpath) / fname
            try:
                sz = fpath.stat().st_size
                files.append((sz, str(fpath)))
                scanned += 1
            except OSError:
                continue

    files.sort(reverse=True)
    top = files[:n]

    return {
        "search_root": str(root),
        "files_scanned": scanned,
        "top_files": [
            {"path": p, "size_mb": round(s / 1e6, 2), "size_bytes": s}
            for s, p in top
        ],
    }


def network_latency_check() -> dict:
    """
    Pings the local gateway, Cloudflare (1.1.1.1), and Google DNS (8.8.8.8)
    CONCURRENTLY using threads, then diagnoses where latency is introduced.
    Async: YES — all pings run in parallel via threading.
    """
    # Discover default gateway
    gateway: str | None = None
    try:
        nr = subprocess.run(["netstat", "-nr"], capture_output=True, text=True, timeout=5)
        for line in nr.stdout.splitlines():
            parts = line.split()
            if parts and parts[0] in ("default", "0.0.0.0") and len(parts) >= 2:
                gateway = parts[1]
                break
    except Exception:
        pass

    targets: dict[str, str] = {}
    if gateway:
        targets["gateway"] = gateway
    targets["cloudflare_dns"] = "1.1.1.1"
    targets["google_dns"]     = "8.8.8.8"
    targets["cloudflare.com"] = "cloudflare.com"

    results: dict = {}
    lock = threading.Lock()
    _sys = platform.system()

    def _ping(label: str, host: str) -> None:
        try:
            cmd = (
                ["ping", "-n", "4", "-w", "2000", host]
                if _sys == "Windows"
                else ["ping", "-c", "4", "-W", "2", host]
            )
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            out  = proc.stdout + proc.stderr

            avg_ms: float | None = None
            # macOS/Linux: min/avg/max/stddev = x/y/z/w ms
            m = re.search(r"min/avg/max(?:/(?:mdev|stddev))?\s*=\s*[\d.]+/([\d.]+)/", out)
            if m:
                avg_ms = float(m.group(1))
            # Windows: Average = Xms
            if avg_ms is None:
                m = re.search(r"Average\s*=\s*([\d.]+)\s*ms", out, re.I)
                if m:
                    avg_ms = float(m.group(1))

            with lock:
                results[label] = {
                    "host":            host,
                    "reachable":       proc.returncode == 0,
                    "avg_latency_ms":  avg_ms,
                }
        except subprocess.TimeoutExpired:
            with lock:
                results[label] = {"host": host, "reachable": False, "error": "ping timed out"}
        except Exception as exc:
            with lock:
                results[label] = {"host": host, "reachable": False, "error": str(exc)}

    threads = [threading.Thread(target=_ping, args=(lbl, h), daemon=True)
               for lbl, h in targets.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    # Diagnosis
    gw = results.get("gateway",       {})
    cf = results.get("cloudflare_dns", {})
    gd = results.get("google_dns",    {})
    diagnosis: list[str] = []
    if gateway and not gw.get("reachable"):
        diagnosis.append("Cannot reach your local gateway — likely a router/Wi-Fi issue.")
    elif not cf.get("reachable") and not gd.get("reachable"):
        diagnosis.append("Gateway reachable but public DNS is not — likely an ISP or WAN issue.")
    else:
        lat = cf.get("avg_latency_ms") or gd.get("avg_latency_ms")
        if lat and lat > 100:
            diagnosis.append(f"High latency ({lat} ms) to public DNS — possible ISP congestion.")
        elif lat and lat > 50:
            diagnosis.append(f"Moderate latency ({lat} ms) — network is functional but not ideal.")
        else:
            diagnosis.append("Network connectivity looks normal.")

    return {"targets": results, "diagnosis": diagnosis}


def get_docker_status() -> dict:
    """Return running Docker containers with CPU and memory stats."""
    if not shutil.which("docker"):
        return {"error": "Docker is not installed or not in PATH."}

    try:
        ping = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=10,
        )
        if ping.returncode != 0:
            return {"error": "Docker daemon is not running. Start Docker Desktop first."}
        server_version = ping.stdout.strip()
    except subprocess.TimeoutExpired:
        return {"error": "Docker daemon did not respond in time."}

    try:
        ps = subprocess.run(
            ["docker", "ps", "--format",
             "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=10,
        )
        containers: list[dict] = []
        for line in ps.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                containers.append({
                    "id":     parts[0],
                    "name":   parts[1],
                    "image":  parts[2],
                    "status": parts[3],
                    "ports":  parts[4] if len(parts) > 4 else "",
                })

        # One-shot stats (no-stream)
        if containers:
            stats = subprocess.run(
                ["docker", "stats", "--no-stream", "--format",
                 "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"],
                capture_output=True, text=True, timeout=20,
            )
            stat_map: dict[str, dict] = {}
            for line in stats.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 4:
                    stat_map[parts[0]] = {
                        "cpu_percent":     parts[1],
                        "memory_usage":    parts[2],
                        "memory_percent":  parts[3],
                    }
            for c in containers:
                c.update(stat_map.get(c["name"], {}))

        # Total container count (including stopped)
        all_ps = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
        total = len(all_ps.stdout.strip().splitlines()) if all_ps.stdout.strip() else 0

        return {
            "docker_version":      server_version,
            "running_count":       len(containers),
            "total_containers":    total,
            "running_containers":  containers,
        }
    except subprocess.TimeoutExpired:
        return {"error": "Docker command timed out."}
    except Exception as exc:
        return {"error": f"Failed to query Docker: {exc}"}


def get_time_machine_status() -> dict:
    """
    Return macOS Time Machine backup status, last backup time, and destination.
    Async: YES — tmutil status, latestbackup, and destinationinfo run in parallel.
    """
    if platform.system() != "Darwin":
        return {"error": "Time Machine is macOS-only."}
    if not shutil.which("tmutil"):
        return {"error": "tmutil not found."}

    result: dict = {}
    lock = threading.Lock()

    def _status() -> None:
        try:
            proc = subprocess.run(["tmutil", "status"], capture_output=True,
                                  text=True, timeout=10)
            out = proc.stdout
            data: dict = {"running": "Running = 1" in out}
            m = re.search(r'BackupPhase\s*=\s*"?([^";\n]+)"?', out)
            if m:
                data["phase"] = m.group(1).strip()
            m = re.search(r'Percent\s*=\s*([\d.]+)', out)
            if m:
                data["progress_percent"] = round(float(m.group(1)) * 100, 1)
            m = re.search(r'_raw_Percent\s*=\s*([\d.]+)', out)
            if m:
                data["progress_percent"] = round(float(m.group(1)) * 100, 1)
            with lock:
                result.update(data)
        except Exception as exc:
            with lock:
                result["status_error"] = str(exc)

    def _latest() -> None:
        try:
            proc = subprocess.run(["tmutil", "latestbackup"], capture_output=True,
                                  text=True, timeout=10)
            bp = proc.stdout.strip()
            if bp and "No backups" not in bp:
                with lock:
                    result["last_backup_path"] = bp
                m = re.search(r"(\d{4}-\d{2}-\d{2}-\d{6})", bp)
                if m:
                    try:
                        dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d-%H%M%S")
                        delta = datetime.datetime.now() - dt
                        hours = int(delta.total_seconds() // 3600)
                        age = f"{hours} hours ago" if hours < 48 else f"{delta.days} days ago"
                        with lock:
                            result["last_backup"] = dt.isoformat()
                            result["last_backup_age"] = age
                    except ValueError:
                        with lock:
                            result["last_backup"] = m.group(1)
            else:
                with lock:
                    result["last_backup"] = "No backups found"
        except Exception as exc:
            with lock:
                result["last_backup_error"] = str(exc)

    def _dest() -> None:
        try:
            proc = subprocess.run(["tmutil", "destinationinfo"], capture_output=True,
                                  text=True, timeout=10)
            m = re.search(r"Name\s*:\s*(.+)", proc.stdout)
            if m:
                with lock:
                    result["destination"] = m.group(1).strip()
            m = re.search(r"Kind\s*:\s*(.+)", proc.stdout)
            if m:
                with lock:
                    result["destination_kind"] = m.group(1).strip()
        except Exception:
            pass

    threads = [
        threading.Thread(target=_status, daemon=True),
        threading.Thread(target=_latest, daemon=True),
        threading.Thread(target=_dest,   daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    return result


def tail_system_logs(lines: int = 50, filter_str: str = "") -> dict:
    """Tail recent system logs. macOS: unified log (last 5 min). Linux: journalctl."""
    lines  = max(10, min(lines, 500))
    system = platform.system()

    if system == "Darwin":
        cmd = ["log", "show", "--last", "5m", "--style", "compact"]
        if filter_str:
            cmd += ["--predicate", f'eventMessage CONTAINS[c] "{filter_str}"']
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            all_lines = [l for l in proc.stdout.splitlines() if l.strip()]
            tail = all_lines[-lines:]
            return {
                "platform": "macOS",
                "source":   "unified system log (last 5 minutes)",
                "filter":   filter_str or None,
                "line_count": len(tail),
                "lines":    tail,
            }
        except subprocess.TimeoutExpired:
            return {"error": "Log command timed out — try reducing lines or adding a filter."}
        except Exception as exc:
            return {"error": f"Failed to read logs: {exc}"}

    if system == "Linux":
        if shutil.which("journalctl"):
            cmd = ["journalctl", "-n", str(lines), "--no-pager", "-o", "short"]
            if filter_str:
                cmd += ["-g", filter_str]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                log_lines = proc.stdout.splitlines()
                return {
                    "platform": "Linux", "source": "journalctl",
                    "filter": filter_str or None,
                    "line_count": len(log_lines), "lines": log_lines,
                }
            except Exception as exc:
                return {"error": f"journalctl failed: {exc}"}
        syslog = pathlib.Path("/var/log/syslog")
        if syslog.exists():
            try:
                all_lines = syslog.read_text(errors="replace").splitlines()
                tail = [
                    l for l in all_lines
                    if not filter_str or filter_str.lower() in l.lower()
                ][-lines:]
                return {
                    "platform": "Linux", "source": "/var/log/syslog",
                    "filter": filter_str or None,
                    "line_count": len(tail), "lines": tail,
                }
            except PermissionError:
                return {"error": "Permission denied reading /var/log/syslog. Try sudo."}
        return {"error": "No supported log source found (journalctl or /var/log/syslog)."}

    return {"error": f"tail_system_logs is not supported on {system}."}


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
    "set_reminder": {
        "description": (
            "Schedule a reminder that fires a macOS notification at the specified time. "
            "Accepts natural-language time: 'in 2 hours', 'in 30 minutes', "
            "'at 9:00 am', 'at 3pm', 'tomorrow at 8am'. "
            "Returns a reminder ID that can be used with cancel_reminder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The reminder text to display in the notification.",
                },
                "time": {
                    "type": "string",
                    "description": (
                        "When to fire the reminder. Examples: 'in 2 hours', "
                        "'in 30 minutes', 'at 9:00 am', 'at 3pm', 'tomorrow at 8am'."
                    ),
                },
            },
            "required": ["message", "time"],
        },
        "fn": lambda args: set_reminder(args["message"], args["time"]),
    },
    "list_reminders": {
        "description": "List all pending (unfired) reminders with their IDs, messages, and scheduled fire times.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: list_reminders(),
    },
    "cancel_reminder": {
        "description": "Cancel a pending reminder by its ID. Get the ID from set_reminder or list_reminders.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The reminder ID to cancel (8-character hex string).",
                }
            },
            "required": ["id"],
        },
        "fn": lambda args: cancel_reminder(args["id"]),
    },
    "get_weather": {
        "description": (
            "Returns current weather conditions and clothing suggestions. "
            "Auto-detects location from IP if no location is provided. "
            "Pass a city name for a specific location (e.g. 'Tokyo' or 'London, UK')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name (e.g. 'Tokyo', 'London, UK'). Leave empty to auto-detect from IP.",
                    "default": "",
                },
                "units": {
                    "type": "string",
                    "enum": ["imperial", "metric"],
                    "description": "Temperature units: 'imperial' (°F, mph) or 'metric' (°C, km/h). Defaults to imperial.",
                    "default": "imperial",
                },
            },
            "required": [],
        },
        "fn": lambda args: get_weather(args.get("location", ""), args.get("units", "imperial")),
    },
    "check_app_updates": {
        "description": (
            "macOS only: checks for outdated applications via Homebrew (formulae + casks), "
            "the Mac App Store (requires the 'mas' CLI — install with 'brew install mas'), "
            "and macOS system software updates. Returns lists of outdated apps with current vs available versions."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: check_app_updates(),
    },
    "track_package": {
        "description": (
            "Track a package by tracking number. Auto-detects the carrier (UPS, USPS, FedEx, DHL). "
            "Returns current status and recent tracking history. "
            "Note: Amazon TBA numbers must be tracked at amazon.com/orders."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tracking_number": {
                    "type": "string",
                    "description": "The package tracking number (UPS, USPS, FedEx, or DHL).",
                }
            },
            "required": ["tracking_number"],
        },
        "fn": lambda args: track_package(args["tracking_number"]),
    },
    "find_large_files": {
        "description": (
            "Finds the top N largest files under a given directory path (default: home directory). "
            "Skips hidden directories, .git, __pycache__, node_modules, .venv, and Library. "
            "Use when the user asks what is using disk space or wants to free up storage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to search (e.g. '/Users/you/Downloads'). Defaults to home directory if omitted.",
                    "default": "",
                },
                "n": {
                    "type": "integer",
                    "description": "Number of largest files to return (default 10, max 50).",
                    "default": 10,
                },
            },
            "required": [],
        },
        "fn": lambda args: find_large_files(args.get("path", ""), args.get("n", 10)),
    },
    "network_latency_check": {
        "description": (
            "Pings the local gateway, Cloudflare DNS (1.1.1.1), and Google DNS (8.8.8.8) "
            "concurrently and returns per-target latency and reachability. "
            "Includes an automatic diagnosis (router issue / ISP issue / congestion / normal). "
            "Use when the user asks if their internet is slow or to locate where latency is introduced."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: network_latency_check(),
    },
    "get_docker_status": {
        "description": (
            "Returns all running Docker containers with their CPU%, memory usage, image, status, and ports. "
            "Also reports total container count (including stopped). "
            "Returns an actionable error if Docker is not installed or the daemon is not running."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_docker_status(),
    },
    "get_time_machine_status": {
        "description": (
            "macOS only. Returns Time Machine backup status: whether a backup is currently running, "
            "last backup time and how long ago it was, backup destination name and kind. "
            "Uses tmutil status, latestbackup, and destinationinfo (run in parallel)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_time_machine_status(),
    },
    "tail_system_logs": {
        "description": (
            "Returns the last N lines from the system log. "
            "macOS: reads from the unified system log (last 5 minutes) via `log show`. "
            "Linux: reads from journalctl or /var/log/syslog. "
            "Optional filter_str narrows results to lines containing that keyword. "
            "Use to diagnose crashes, kernel panics, or application errors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of log lines to return (default 50, max 500).",
                    "default": 50,
                },
                "filter_str": {
                    "type": "string",
                    "description": "Optional keyword to filter log lines (case-insensitive).",
                    "default": "",
                },
            },
            "required": [],
        },
        "fn": lambda args: tail_system_logs(args.get("lines", 50), args.get("filter_str", "")),
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
            try:
                sys.stdout.write(json.dumps(make_error(None, -32700, "Parse error")) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                return
            continue

        response = handle_request(request)
        if response is not None:
            try:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                return


if __name__ == "__main__":
    _reminder_checker = ReminderChecker()
    _reminder_checker.start()
    main()
