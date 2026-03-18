#!/usr/bin/env python3
"""
MCP Server: System Activity Monitor
Exposes tools for querying CPU, RAM, GPU, disk, network, and process info.
"""

import ast
import base64
import datetime
import heapq
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
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

try:
    import psutil
except ImportError:
    print("psutil not found. Install with: pip install psutil", file=sys.stderr)
    sys.exit(1)

# Optional GPU support via pynvml (nvidia-ml-py).
GPU_BACKEND = None
try:
    import pynvml
    pynvml.nvmlInit()
    GPU_BACKEND = "pynvml"
except Exception:
    GPU_BACKEND = None

GPU_AVAILABLE = GPU_BACKEND is not None

# ── Platform constants (computed once at startup) ─────────────────────────────
_SYSTEM  = platform.system()
_MACHINE = platform.machine()
IS_MACOS = _SYSTEM == "Darwin"
IS_LINUX = _SYSTEM == "Linux"
IS_WIN   = _SYSTEM == "Windows"

# ── Shared thread pool for parallel metric collection ─────────────────────────
# Reused across calls — avoids per-call thread creation/teardown overhead.
_METRICS_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="syscontrol-metrics")

# ── pynvml handle cache (handles are stable for the process lifetime) ─────────
_NVML_HANDLES: list = []


def _get_nvml_handles() -> list:
    """Return cached pynvml device handles; populated lazily on first call."""
    global _NVML_HANDLES
    if _NVML_HANDLES:
        return _NVML_HANDLES
    try:
        count = pynvml.nvmlDeviceGetCount()
        _NVML_HANDLES = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
    except Exception:
        _NVML_HANDLES = []
    return _NVML_HANDLES


# ── Reminder storage ──────────────────────────────────────────────────────────

_REMINDER_LOCK = threading.Lock()
_REMINDER_DIR  = pathlib.Path.home() / ".syscontrol"
_REMINDER_FILE = _REMINDER_DIR / "reminders.json"
# Create the config directory once at server startup, not on every read/write.
_REMINDER_DIR.mkdir(parents=True, exist_ok=True)

# ── Tool self-extension constants ─────────────────────────────────────────────
# Path to this file — used by create_tool for self-modification.
_SERVER_FILE = pathlib.Path(__file__)
_PROMPT_FILE = pathlib.Path(__file__).parent / "prompt.json"
# Marker prepended to each user-defined function block in this file.
_USER_TOOL_FN_MARKER  = "# ── User-Defined Tool:"
# Anchor comment inside the TOOLS dict where new entries are inserted.
_USER_TOOL_REG_MARKER = "# ── User-Defined Tools (registry) ──────────────────────────────────────────"
_REMINDER_START_LOCK = threading.Lock()
_REMINDER_STARTED = False


def _load_reminders() -> list:
    """Load reminders from disk. Creates file if missing. Must be called under _REMINDER_LOCK."""
    if not _REMINDER_FILE.exists():
        return []
    try:
        return json.loads(_REMINDER_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_reminders(reminders: list) -> None:
    """Write reminders to disk. Must be called under _REMINDER_LOCK."""
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
            next_due = self._check()
            # Sleep until the next reminder is due, capped at 15 s so new
            # reminders set by other tools are noticed quickly.
            time.sleep(min(15.0, max(1.0, next_due)))

    def _check(self) -> float:
        """Check and fire due reminders. Returns seconds until the next unfired reminder."""
        now = datetime.datetime.now()
        cutoff = now - datetime.timedelta(days=7)
        to_fire = []
        next_due = float("inf")
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
                else:
                    secs = (fire_at - now).total_seconds()
                    if secs < next_due:
                        next_due = secs
                survivors.append(r)
            if changed:
                _save_reminders(survivors)
        # Fire notifications outside the lock to avoid blocking set/list/cancel
        for msg in to_fire:
            self._fire(msg)
        return next_due

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


def _start_reminder_checker_once() -> None:
    """Start the reminder checker for this process if it is not already running."""
    global _REMINDER_STARTED
    with _REMINDER_START_LOCK:
        if _REMINDER_STARTED:
            return
        ReminderChecker().start()
        _REMINDER_STARTED = True


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
        return {"error": "No supported GPU backend found. Install nvidia-ml-py to enable GPU monitoring."}

    try:
        handles = _get_nvml_handles()
        if not handles:
            return {"error": "No GPUs detected"}
        gpus = []
        for i, h in enumerate(handles):
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            mem  = pynvml.nvmlDeviceGetMemoryInfo(h)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            except pynvml.NVMLError:
                temp = None
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()
            mem_total_mb = mem.total / 1024 / 1024
            mem_used_mb  = mem.used  / 1024 / 1024
            gpus.append({
                "id": i,
                "name": name,
                "load_percent": util.gpu,
                "memory_used_mb": round(mem_used_mb, 1),
                "memory_total_mb": round(mem_total_mb, 1),
                "memory_percent": round(mem_used_mb / mem_total_mb * 100, 1) if mem_total_mb else None,
                "temperature_c": temp,
            })
        return {"gpus": gpus}
    except pynvml.NVMLError as e:
        global _NVML_HANDLES
        _NVML_HANDLES = []  # invalidate cache on NVML error so next call retries
        return {"error": f"NVML error: {e}"}


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
    # Run all four independent data-source calls in parallel.
    f_specs = _METRICS_EXECUTOR.submit(get_device_specs)
    f_cpu   = _METRICS_EXECUTOR.submit(get_cpu_usage)
    f_ram   = _METRICS_EXECUTOR.submit(get_ram_usage)
    f_gpu   = _METRICS_EXECUTOR.submit(get_gpu_usage)
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
    if IS_MACOS:
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
            "platform": _SYSTEM,
            "available": False,
            "sensors": {},
            "message": "psutil.sensors_temperatures() not available on this platform/version.",
        }
    try:
        raw = psutil.sensors_temperatures()
    except Exception as e:
        return {"platform": _SYSTEM, "available": False, "sensors": {}, "message": f"Failed to read sensors: {e}"}
    if not raw:
        return {
            "platform": _SYSTEM,
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
    return {"platform": _SYSTEM, "available": True, "message": "", "sensors": sensors}


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
            if GPU_BACKEND == "pynvml":
                for i, h in enumerate(_get_nvml_handles()):
                    util = pynvml.nvmlDeviceGetUtilizationRates(h)
                    name = pynvml.nvmlDeviceGetName(h)
                    if isinstance(name, bytes):
                        name = name.decode()
                    load_pct = util.gpu
                    if load_pct >= 95:
                        _alert("critical", f"gpu:{i}", f"GPU {name} load critically high at {load_pct}%", load_pct)
                    try:
                        temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
                        if temp >= 85:
                            _alert("critical", f"gpu:{i}", f"GPU {name} temp critically high at {temp}°C", temp)
                        elif temp >= 75:
                            _alert("warning", f"gpu:{i}", f"GPU {name} temp elevated at {temp}°C", temp)
                    except pynvml.NVMLError:
                        pass
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
    if IS_MACOS:
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

    if IS_WIN:
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

    if IS_LINUX:
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

    return {"platform": _SYSTEM, "error": f"Not supported on {_SYSTEM}", "items": [], "count": 0}


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


@lru_cache(maxsize=1)
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
        try:
            if GPU_BACKEND == "pynvml":
                for h in _get_nvml_handles():
                    name = pynvml.nvmlDeviceGetName(h)
                    if isinstance(name, bytes):
                        name = name.decode()
                    mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                    gpu_specs.append({
                        "name": name,
                        "vram_total_mb": round(mem.total / 1024 / 1024, 1),
                    })
        except Exception:
            pass

    return {
        "os": {
            "system": _SYSTEM,
            "release": platform.release(),
            "version": platform.version(),
            "machine": _MACHINE,
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
        "gpus": gpu_specs or [{"error": "pynvml not available or no NVIDIA GPUs detected"}],
        "disks": disks,
    }


def get_full_snapshot() -> dict:
    """Aggregate snapshot of all metrics — all sources fetched in parallel."""
    f_cpu     = _METRICS_EXECUTOR.submit(get_cpu_usage)
    f_ram     = _METRICS_EXECUTOR.submit(get_ram_usage)
    f_gpu     = _METRICS_EXECUTOR.submit(get_gpu_usage)
    f_disk    = _METRICS_EXECUTOR.submit(get_disk_usage)
    f_net     = _METRICS_EXECUTOR.submit(get_network_usage)
    f_top_cpu = _METRICS_EXECUTOR.submit(get_top_processes, 5, "cpu")
    f_top_mem = _METRICS_EXECUTOR.submit(get_top_processes, 5, "memory")
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

# Pre-compiled regex patterns for _parse_reminder_time (compiled once at module load).
_RE_COMPOUND = re.compile(r"in\s+(\d+)\s+hours?\s+(?:and\s+)?(\d+)\s+minutes?")
_RE_RELATIVE = re.compile(r"in\s+(\d+)\s+(\w+)")
_RE_TOMORROW = re.compile(r"tomorrow\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?")
_RE_AT_TIME  = re.compile(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?")

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
    m = _RE_COMPOUND.match(s)
    if m:
        return now + datetime.timedelta(hours=int(m.group(1)), minutes=int(m.group(2)))

    # "in 2 hours" / "in 30 minutes" / "in 1 day"
    m = _RE_RELATIVE.match(s)
    if m:
        unit = _RELATIVE_UNITS.get(m.group(2))
        if unit:
            return now + datetime.timedelta(seconds=int(m.group(1)) * unit)

    # "tomorrow at 9:00 am" / "tomorrow at 3pm"
    m = _RE_TOMORROW.match(s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        period = m.group(3)
        if period == "pm" and hour < 12: hour += 12
        if period == "am" and hour == 12: hour = 0
        return (now + datetime.timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    # "at 9:00 am" / "at 14:30" / "at 3pm"
    m = _RE_AT_TIME.match(s)
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
    if not IS_MACOS:
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

    # Run all three checks concurrently via the shared executor.
    futures = [
        ("brew", _METRICS_EXECUTOR.submit(_brew)),
        ("mas", _METRICS_EXECUTOR.submit(_mas)),
        ("softwareupdate", _METRICS_EXECUTOR.submit(_sysupdate)),
    ]
    for label, f in futures:
        try:
            f.result(timeout=130)   # brew timeout is 120s; add a small buffer
        except Exception as exc:
            with lock:
                results["errors"].append(f"{label} worker failed: {str(exc)}")

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
        if not isinstance(w1, dict):
            return {
                "tracking_number": tracking_number,
                "detected_carrier": carrier,
                "status": "Unexpected response structure from 17track — their internal API may have changed.",
            }
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

    top = heapq.nlargest(n, files)

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

    def _ping(label: str, host: str) -> None:
        try:
            cmd = (
                ["ping", "-n", "4", "-w", "2000", host]
                if IS_WIN
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

    futures = [_METRICS_EXECUTOR.submit(_ping, lbl, h) for lbl, h in targets.items()]
    for f in futures:
        try:
            f.result(timeout=20)
        except Exception:
            pass

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
    if not IS_MACOS:
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

    futures = [
        _METRICS_EXECUTOR.submit(_status),
        _METRICS_EXECUTOR.submit(_latest),
        _METRICS_EXECUTOR.submit(_dest),
    ]
    for f in futures:
        try:
            f.result(timeout=15)
        except Exception:
            pass

    return result


def tail_system_logs(lines: int = 50, filter_str: str = "") -> dict:
    """Tail recent system logs. macOS: unified log (last 5 min). Linux: journalctl."""
    lines  = max(10, min(lines, 500))

    if IS_MACOS:
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

    if IS_LINUX:
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

    return {"error": f"tail_system_logs is not supported on {_SYSTEM}."}


# ── Browser / Web tools ──────────────────────────────────────────────────────

_BROWSER_PERMISSION_FILE = pathlib.Path.home() / ".syscontrol" / "browser_permission"

# Browsers the AppleScript helpers know how to talk to, in preference order.
# Arc, Brave, and Edge all use the Chrome AppleScript dictionary.
_CHROMIUM_APPS = ["Arc", "Google Chrome", "Brave Browser", "Microsoft Edge", "Chromium"]
_SAFARI_APP    = "Safari"


def _browser_permission_granted() -> bool:
    return _BROWSER_PERMISSION_FILE.exists()


def _browser_permission_required() -> dict:
    return {
        "error": "browser_access_not_granted",
        "message": (
            "Browser access has not been granted yet. "
            "Ask the user to confirm, then call grant_browser_access() to enable it."
        ),
    }


def _running_browser() -> str | None:
    """Return the name of the first recognised browser that is currently running."""
    if not IS_MACOS:
        return None
    # Single AppleScript call to get all running process names, then match
    # against known browsers — avoids spawning one subprocess per browser.
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of every process'],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        running = {n.strip() for n in r.stdout.split(",")}
        for app in _CHROMIUM_APPS + [_SAFARI_APP]:
            if app in running:
                return app
    except Exception:
        pass
    return None


def _osa(script: str, timeout: int = 10) -> tuple[str, str, int]:
    """Run an AppleScript snippet and return (stdout, stderr, returncode)."""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def _chromium_script(app: str, js_or_cmd: str) -> str:
    """Wrap a Chrome-protocol AppleScript command for the given app."""
    return f'tell application "{app}" to {js_or_cmd}'


def _safari_script(cmd: str) -> str:
    return f'tell application "Safari" to {cmd}'


# ─────────────────────────────────────────────────────────────────────────────

def grant_browser_access() -> dict:
    """
    Writes the browser permission flag so that browser control tools can run.
    ONLY call this after the user has explicitly said yes.
    """
    try:
        _BROWSER_PERMISSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BROWSER_PERMISSION_FILE.write_text("granted")
        browser = _running_browser()
        return {
            "success": True,
            "message": "Browser access granted.",
            "detected_browser": browser or "none running — open a browser and try again",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


_RE_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_RE_HTML_TAG      = re.compile(r"<[^>]+>")
_RE_WHITESPACE    = re.compile(r"\s+")


def _strip_html(html: str, max_chars: int) -> str:
    """Very fast HTML → plain-text: strip tags, collapse whitespace."""
    text = _RE_SCRIPT_STYLE.sub(" ", html)
    text = _RE_HTML_TAG.sub(" ", text)
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                          ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    text = _RE_WHITESPACE.sub(" ", text).strip()
    return text[:max_chars]


def web_fetch(url: str, max_chars: int = 8000) -> dict:
    """
    Fetch a web page and return plain-text content (no browser needed).
    HTML tags are stripped. Does NOT require browser permission.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    max_chars = max(500, min(max_chars, 32000))
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            charset = "utf-8"
            ctype = r.headers.get("Content-Type", "")
            m = re.search(r"charset=([^\s;]+)", ctype)
            if m:
                charset = m.group(1)
            html = raw.decode(charset, errors="replace")
        text = _strip_html(html, max_chars)
        return {
            "url": url,
            "status": r.status,   # type: ignore[possibly-undefined]
            "content_length": len(text),
            "text": text,
            "truncated": len(text) == max_chars,
        }
    except urllib.error.HTTPError as e:
        return {"url": url, "error": f"HTTP {e.code}: {e.reason}"}
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return {"url": url, "error": f"Network error: {e}"}


def web_search(query: str, num_results: int = 5) -> dict:
    """
    Search DuckDuckGo and return the top results (title, URL, snippet).
    No API key needed. Does NOT require browser permission.
    """
    num_results = max(1, min(num_results, 10))
    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"query": query, "error": f"Search request failed: {e}"}

    # Parse DuckDuckGo HTML results — structure is stable enough for parsing
    results = []
    # Each result block: <a class="result__a" href="...">Title</a>
    #                    <a class="result__snippet">Snippet</a>
    title_pattern   = re.compile(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
    snippet_pattern = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)

    titles   = title_pattern.findall(html)
    snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippet_pattern.findall(html)]

    for i, (href, title_raw) in enumerate(titles[:num_results]):
        title = re.sub(r"<[^>]+>", "", title_raw).strip()
        # DDG wraps URLs — extract the actual destination
        m = re.search(r"uddg=([^&]+)", href)
        real_url = urllib.parse.unquote(m.group(1)) if m else href
        results.append({
            "rank":    i + 1,
            "title":   title,
            "url":     real_url,
            "snippet": snippets[i] if i < len(snippets) else "",
        })

    return {
        "query":       query,
        "result_count": len(results),
        "results":     results,
        **({
            "warning": "No results parsed — DuckDuckGo HTML structure may have changed."
        } if not results else {}),
    }


def browser_open_url(url: str) -> dict:
    """Open a URL in the user's default browser. Requires browser permission."""
    if not _browser_permission_granted():
        return _browser_permission_required()
    if not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url
    if IS_MACOS:
        try:
            subprocess.run(["open", url], check=True, timeout=10)
            return {"success": True, "url": url, "action": "opened in default browser"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    # Linux / Windows fallback
    try:
        webbrowser.open(url)
        return {"success": True, "url": url, "action": "opened in default browser"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# Compiled once at module load — used by browser_navigate to validate URLs
# before embedding them in AppleScript string literals.
_SAFE_URL_RE = re.compile(r'^[\x20-\x7E]+$')


def browser_navigate(url: str) -> dict:
    """
    Navigate the currently active browser tab to a URL via AppleScript (macOS).
    Requires browser permission.
    """
    if not _browser_permission_granted():
        return _browser_permission_required()
    if not IS_MACOS:
        return browser_open_url(url)   # graceful fallback on non-macOS

    # Normalise scheme
    if not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url

    # Reject URLs with characters that could break out of the AppleScript string
    # literal. _SAFE_URL_RE is a module-level constant compiled once at import.
    if not _SAFE_URL_RE.match(url):
        return {"success": False, "error": "URL contains non-printable or non-ASCII characters."}
    if any(c in url for c in ('"', "'", '`', '\\', '\r', '\n')):
        return {"success": False, "error": "URL contains characters that are not safe for AppleScript."}

    browser = _running_browser()
    if not browser:
        # No known browser running — just open the URL
        return browser_open_url(url)

    try:
        if browser == _SAFARI_APP:
            script = _safari_script(f'set URL of current tab of front window to "{url}"')
        else:
            script = _chromium_script(browser, f'set URL of active tab of front window to "{url}"')
        stdout, stderr, rc = _osa(script)
        if rc != 0 and stderr:
            # Fallback: just open it
            return browser_open_url(url)
        return {"success": True, "url": url, "browser": browser, "action": "navigated"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "AppleScript timed out — browser may be busy"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def browser_get_page() -> dict:
    """
    Return the URL, title, and visible text of the current active browser tab
    via AppleScript (macOS only). Requires browser permission.
    """
    if not _browser_permission_granted():
        return _browser_permission_required()
    if not IS_MACOS:
        return {"error": "browser_get_page requires macOS (uses AppleScript)."}

    browser = _running_browser()
    if not browser:
        return {
            "error": "No supported browser is running.",
            "supported": _CHROMIUM_APPS + [_SAFARI_APP],
        }

    try:
        if browser == _SAFARI_APP:
            url_out, _, rc1 = _osa(_safari_script("URL of current tab of front window"))
            title_out, _, rc2 = _osa(_safari_script("name of current tab of front window"))
            # Get visible text via JavaScript
            js_script = _safari_script(
                'do JavaScript "document.body ? document.body.innerText.substring(0,12000) : \'\'" '
                'in current tab of front window'
            )
            text_out, _, _ = _osa(js_script)
        else:
            url_out,   _, rc1 = _osa(_chromium_script(browser, "URL of active tab of front window"))
            title_out, _, rc2 = _osa(_chromium_script(browser, "title of active tab of front window"))
            js_script = _chromium_script(
                browser,
                'execute active tab of front window javascript '
                '"document.body ? document.body.innerText.substring(0,12000) : \'\'"'
            )
            text_out, _, _ = _osa(js_script)

        if rc1 != 0 or rc2 != 0:
            return {
                "error": "Could not read browser tab — make sure a window is open and focused.",
                "browser": browser,
            }

        # Strip excessive whitespace from innerText
        clean_text = re.sub(r"\n{3,}", "\n\n", text_out).strip()

        return {
            "browser":  browser,
            "url":      url_out,
            "title":    title_out,
            "text":     clean_text,
            "text_length": len(clean_text),
        }

    except subprocess.TimeoutExpired:
        return {"error": "AppleScript timed out — browser may be unresponsive.", "browser": browser}
    except Exception as e:
        return {"error": f"Failed to read browser page: {e}", "browser": browser}


# ── iMessage tools ───────────────────────────────────────────────────────────

def send_imessage(recipient: str, message: str) -> dict:
    """Send an iMessage or SMS via macOS Messages.app using AppleScript."""
    denied = _permission_check("allow_messaging", "send_imessage")
    if denied:
        return denied
    if not IS_MACOS:
        return {"error": "send_imessage requires macOS."}
    if not recipient or not message:
        return {"error": "recipient and message are required."}
    # AppleScript: send to a buddy (phone number or email).
    script = (
        f'tell application "Messages"\n'
        f'  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy {json.dumps(recipient)} of targetService\n'
        f'  send {json.dumps(message)} to targetBuddy\n'
        f'end tell'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            # Fallback: try without specifying service (works for SMS relay too)
            script2 = (
                f'tell application "Messages"\n'
                f'  send {json.dumps(message)} to buddy {json.dumps(recipient)}\n'
                f'end tell'
            )
            proc2 = subprocess.run(
                ["osascript", "-e", script2],
                capture_output=True, text=True, timeout=15,
            )
            if proc2.returncode != 0:
                return {
                    "error": proc2.stderr.strip() or proc.stderr.strip(),
                    "hint": (
                        "Make sure Messages.app is signed in and you have granted "
                        "Automation permission to Terminal/iTerm in System Settings → "
                        "Privacy & Security → Automation."
                    ),
                }
        return {"status": "sent", "recipient": recipient, "message": message}
    except subprocess.TimeoutExpired:
        return {"error": "AppleScript timed out sending iMessage."}
    except Exception as e:
        return {"error": str(e)}


def get_imessage_history(contact: str, limit: int = 20) -> dict:
    """Return recent iMessage/SMS messages for a contact from chat.db."""
    denied = _permission_check("allow_message_history", "get_imessage_history")
    if denied:
        return denied
    if not IS_MACOS:
        return {"error": "get_imessage_history requires macOS."}
    import sqlite3 as _sqlite3

    db_path = pathlib.Path.home() / "Library" / "Messages" / "chat.db"
    if not db_path.exists():
        return {"error": f"chat.db not found at {db_path}. Full Disk Access may be required."}

    limit = max(1, min(limit, 200))
    contact_q = f"%{contact}%"

    try:
        # Use a copy-on-read approach: open read-only URI to avoid locking
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = _sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                m.text,
                m.is_from_me,
                datetime(m.date / 1000000000 + strftime('%s','2001-01-01'), 'unixepoch', 'localtime') AS sent_at,
                h.id AS handle
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.rowid
            JOIN chat c ON c.rowid = cmj.chat_id
            JOIN chat_handle_join chj ON chj.chat_id = c.rowid
            JOIN handle h ON h.rowid = chj.handle_id
            WHERE h.id LIKE ?
              AND m.text IS NOT NULL AND m.text != ''
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (contact_q, limit),
        )
        rows = cur.fetchall()
        conn.close()

        messages = [
            {
                "from": "me" if r["is_from_me"] else r["handle"],
                "text": r["text"],
                "sent_at": r["sent_at"],
            }
            for r in rows
        ]
        return {
            "contact_filter": contact,
            "count": len(messages),
            "messages": list(reversed(messages)),  # chronological order
        }
    except Exception as e:
        return {
            "error": str(e),
            "hint": "Full Disk Access for Terminal is required in System Settings → Privacy & Security → Full Disk Access.",
        }


# ── Clipboard tools ───────────────────────────────────────────────────────────

def get_clipboard() -> dict:
    """Return the current contents of the system clipboard."""
    if not IS_MACOS:
        return {"error": "get_clipboard is currently macOS only (uses pbpaste)."}
    try:
        result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        text = result.stdout
        return {
            "text": text,
            "length": len(text),
            "has_content": bool(text.strip()),
        }
    except Exception as e:
        return {"error": str(e)}


def set_clipboard(text: str) -> dict:
    """Write text to the system clipboard."""
    if not IS_MACOS:
        return {"error": "set_clipboard is currently macOS only (uses pbcopy)."}
    try:
        subprocess.run(
            ["pbcopy"],
            input=text, text=True, timeout=5, check=True,
        )
        return {"status": "ok", "length": len(text)}
    except Exception as e:
        return {"error": str(e)}


# ── Screenshot tool ───────────────────────────────────────────────────────────

def take_screenshot(path: str = "") -> tuple:
    """
    Capture the entire screen. Always returns a 2-tuple (metadata_dict, base64_png_string).
    On error, returns ({"error": ...}, "").
    Optionally saves the image to `path` if provided.
    """
    denied = _permission_check("allow_screenshot", "take_screenshot")
    if denied:
        return denied, ""
    import tempfile as _tempfile

    if not IS_MACOS:
        return {"error": "take_screenshot requires macOS (uses screencapture)."}, ""

    with _tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # -x = no sound, -C = capture cursor
        proc = subprocess.run(
            ["screencapture", "-x", tmp_path],
            capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            return {"error": f"screencapture failed: {stderr or 'unknown error'}"}, ""

        img_file = pathlib.Path(tmp_path)
        if not img_file.exists() or img_file.stat().st_size == 0:
            return {"error": "screencapture produced no output (screen may not be accessible)."}, ""

        img_bytes = img_file.read_bytes()
        img_b64 = base64.b64encode(img_bytes).decode()

        saved_to = None
        if path:
            dest = pathlib.Path(path).expanduser()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(img_bytes)
            saved_to = str(dest)

        meta = {
            "size_bytes": len(img_bytes),
            "saved_to": saved_to,
        }
        return meta, img_b64
    except subprocess.TimeoutExpired:
        return {"error": "screencapture timed out."}, ""
    except Exception as e:
        return {"error": str(e)}, ""
    finally:
        try:
            pathlib.Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


# ── App control tools ─────────────────────────────────────────────────────────

def open_app(name: str) -> dict:
    """Open an application by name using macOS `open -a`."""
    if not IS_MACOS:
        return {"error": "open_app requires macOS."}
    if not name:
        return {"error": "app name is required."}
    try:
        proc = subprocess.run(
            ["open", "-a", name],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip() or f"Could not open '{name}'."}
        return {"status": "ok", "app": name}
    except Exception as e:
        return {"error": str(e)}


def quit_app(name: str, force: bool = False) -> dict:
    """Gracefully quit an application by name using AppleScript."""
    if not IS_MACOS:
        return {"error": "quit_app requires macOS."}
    if not name:
        return {"error": "app name is required."}
    try:
        if force:
            # Force-quit via kill
            find_proc = subprocess.run(
                ["pgrep", "-ix", name],
                capture_output=True, text=True, timeout=5,
            )
            pids = find_proc.stdout.strip().splitlines()
            if not pids:
                return {"error": f"No process found matching '{name}'."}
            for pid in pids:
                subprocess.run(["kill", "-9", pid], timeout=5)
            return {"status": "force-killed", "app": name, "pids": pids}
        else:
            script = f'tell application "{name}" to quit'
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                return {"error": proc.stderr.strip() or f"Could not quit '{name}'."}
            return {"status": "quit", "app": name}
    except Exception as e:
        return {"error": str(e)}


# ── Volume tools ──────────────────────────────────────────────────────────────

def get_volume() -> dict:
    """Return the current output volume and mute state."""
    if not IS_MACOS:
        return {"error": "get_volume requires macOS."}
    try:
        proc = subprocess.run(
            ["osascript", "-e", "get volume settings"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip()}
        # Output format: "output volume:75, input volume:54, alert volume:100, output muted:false"
        raw = proc.stdout.strip()
        result = {}
        for part in raw.split(","):
            part = part.strip()
            if ":" in part:
                k, v = part.split(":", 1)
                key = k.strip().replace(" ", "_")
                val_str = v.strip()
                if val_str.isdigit():
                    result[key] = int(val_str)
                elif val_str in ("true", "false"):
                    result[key] = val_str == "true"
                else:
                    result[key] = val_str
        return result
    except Exception as e:
        return {"error": str(e)}


def set_volume(level: int) -> dict:
    """Set the system output volume (0–100)."""
    if not IS_MACOS:
        return {"error": "set_volume requires macOS."}
    level = max(0, min(100, int(level)))
    try:
        proc = subprocess.run(
            ["osascript", "-e", f"set volume output volume {level}"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip()}
        return {"status": "ok", "output_volume": level}
    except Exception as e:
        return {"error": str(e)}


# ── Wi-Fi tool ────────────────────────────────────────────────────────────────

_AIRPORT_PATH = (
    "/System/Library/PrivateFrameworks/Apple80211.framework"
    "/Versions/Current/Resources/airport"
)

def get_wifi_networks() -> dict:
    """
    Return information about nearby / available Wi-Fi networks.
    Uses the `airport` CLI when available (macOS ≤13), otherwise falls back to
    `system_profiler SPAirPortDataType` which works on macOS 14+.
    """
    if not IS_MACOS:
        return {"error": "get_wifi_networks requires macOS."}

    # ── Try airport (macOS ≤13) ──────────────────────────────────────────────
    airport = pathlib.Path(_AIRPORT_PATH)
    if airport.exists():
        try:
            proc = subprocess.run(
                [str(airport), "-s"],
                capture_output=True, text=True, timeout=20,
            )
            if proc.returncode == 0:
                lines = proc.stdout.splitlines()
                networks = []
                for line in lines[1:]:
                    if not line.strip():
                        continue
                    try:
                        ssid = line[:33].strip()
                        rest = line[33:].split()
                        bssid = rest[0] if rest else ""
                        rssi = int(rest[1]) if len(rest) > 1 else None
                        channel = rest[2] if len(rest) > 2 else ""
                        security = rest[6] if len(rest) > 6 else rest[-1] if rest else ""
                        networks.append({"ssid": ssid, "bssid": bssid,
                                         "rssi_dbm": rssi, "channel": channel,
                                         "security": security})
                    except (IndexError, ValueError):
                        continue
                networks.sort(key=lambda n: n.get("rssi_dbm") or -999, reverse=True)
                return {"source": "airport", "networks": networks, "count": len(networks)}
        except subprocess.TimeoutExpired:
            return {"error": "Wi-Fi scan timed out (20s). Enable Wi-Fi and try again."}
        except Exception:
            pass  # fall through to system_profiler

    # ── Fallback: system_profiler SPAirPortDataType (macOS 14+) ─────────────
    try:
        proc = subprocess.run(
            ["system_profiler", "SPAirPortDataType", "-json"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip() or "system_profiler failed."}
        data = json.loads(proc.stdout)
        sp_wifi = data.get("SPAirPortDataType", [])
        networks = []

        def _parse_rssi(sig_noise_str: str) -> int | None:
            """Parse '-56 dBm / -95 dBm' → -56."""
            if not sig_noise_str:
                return None
            try:
                return int(sig_noise_str.split()[0])
            except (ValueError, IndexError):
                return None

        for entry in sp_wifi:
            # interfaces is a list of dicts, each with _name = interface identifier
            interfaces = entry.get("spairport_airport_interfaces", [])
            for iface in interfaces:
                # Current connected network — flat dict with _name as SSID
                cur = iface.get("spairport_current_network_information", {})
                if cur:
                    networks.append({
                        "ssid": cur.get("_name", ""),
                        "phy_mode": cur.get("spairport_network_phymode", ""),
                        "channel": str(cur.get("spairport_network_channel", "")),
                        "security": cur.get("spairport_security_mode", ""),
                        "rssi_dbm": _parse_rssi(cur.get("spairport_signal_noise", "")),
                        "connected": True,
                    })
                # Other visible networks — list of dicts, each with _name as SSID
                others = iface.get("spairport_airport_other_local_wireless_networks", [])
                if isinstance(others, list):
                    for net in others:
                        sn = net.get("spairport_signal_noise", "")
                        networks.append({
                            "ssid": net.get("_name", ""),
                            "phy_mode": net.get("spairport_network_phymode", ""),
                            "channel": str(net.get("spairport_network_channel", "")),
                            "security": net.get("spairport_security_mode", ""),
                            "rssi_dbm": _parse_rssi(sn) if sn else None,
                            "connected": False,
                        })

        return {"source": "system_profiler", "networks": networks, "count": len(networks)}
    except subprocess.TimeoutExpired:
        return {"error": "system_profiler timed out (30s)."}
    except Exception as e:
        return {"error": str(e)}


# ── File tools ────────────────────────────────────────────────────────────────

_MAX_READ_CHARS = 32_000

def read_file(path: str, max_chars: int = 16_000) -> dict:
    """Read a text file and return its contents."""
    denied = _permission_check("allow_file_read", "read_file")
    if denied:
        return denied
    if not path:
        return {"error": "path is required."}
    max_chars = max(1, min(max_chars, _MAX_READ_CHARS))
    try:
        p = pathlib.Path(path).expanduser().resolve()
        if not p.exists():
            return {"error": f"File not found: {p}"}
        if not p.is_file():
            return {"error": f"Not a file: {p}"}
        size = p.stat().st_size
        content = p.read_text(errors="replace")
        truncated = len(content) > max_chars
        return {
            "path": str(p),
            "size_bytes": size,
            "chars_read": min(len(content), max_chars),
            "truncated": truncated,
            "content": content[:max_chars],
        }
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        return {"error": str(e)}


def write_file(path: str, content: str, overwrite: bool = True) -> dict:
    """Write text content to a file. Creates parent directories as needed."""
    denied = _permission_check("allow_file_write", "write_file")
    if denied:
        return denied
    if not path:
        return {"error": "path is required."}
    try:
        p = pathlib.Path(path).expanduser().resolve()
        if p.exists() and not overwrite:
            return {"error": f"File already exists: {p}. Pass overwrite=true to replace it."}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {
            "status": "ok",
            "path": str(p),
            "bytes_written": len(content.encode()),
        }
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        return {"error": str(e)}


# ── Permission gate ────────────────────────────────────────────────────────────
#
# All sensitive tools are disabled until the user explicitly opts in via
# ~/.syscontrol/config.json. This prevents the agent from accessing private
# data or performing actions without the user's knowledge.
#
# Example config.json enabling all gates:
#   {
#     "allow_shell":           true,
#     "allow_messaging":       true,
#     "allow_message_history": true,
#     "allow_screenshot":      true,
#     "allow_file_read":       true,
#     "allow_file_write":      true,
#     "allow_calendar":        true,
#     "allow_contacts":        true,
#     "allow_accessibility":   true
#   }

_SYSCONTROL_CONFIG_FILE = _REMINDER_DIR / "config.json"


_CONFIG_CACHE: dict = {}
_CONFIG_TTL: float = 5.0           # seconds; config changes take effect within one TTL window
_CONFIG_CACHE_TIME: float = float("-inf")  # force a disk read on the very first call


def _load_config() -> dict:
    """Load ~/.syscontrol/config.json, cached for _CONFIG_TTL seconds."""
    global _CONFIG_CACHE, _CONFIG_CACHE_TIME
    now = time.monotonic()
    if now - _CONFIG_CACHE_TIME < _CONFIG_TTL:
        return _CONFIG_CACHE
    try:
        _CONFIG_CACHE = json.loads(_SYSCONTROL_CONFIG_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _CONFIG_CACHE = {}
    _CONFIG_CACHE_TIME = now
    return _CONFIG_CACHE


def _permission_check(flag: str, tool_name: str) -> dict | None:
    """
    Returns None if *flag* is enabled in config (tool may proceed).
    Returns an error dict if the tool is disabled, describing how to enable it.
    """
    if _load_config().get(flag, False):
        return None  # permitted
    return {
        "error": f"{tool_name} is disabled by default for security.",
        "hint": (
            f'To enable it, add "{flag}": true to ~/.syscontrol/config.json.\n'
            f"Example: {{\"{ flag }\": true}}"
        ),
        "config_path": str(_SYSCONTROL_CONFIG_FILE),
    }


def run_shell_command(command: str, timeout: int = 30) -> dict:
    """
    Execute a shell command and return stdout, stderr, and exit code.
    Requires ``allow_shell: true`` in ~/.syscontrol/config.json.
    """
    denied = _permission_check("allow_shell", "run_shell_command")
    if denied:
        return denied
    if not command:
        return {"error": "command is required."}
    timeout = max(1, min(timeout, 120))
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True,
            timeout=timeout,
        )
        return {
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout[:8000],
            "stderr": proc.stderr[:2000],
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s.", "command": command}
    except Exception as e:
        return {"error": str(e), "command": command}


# ── Calendar tool ─────────────────────────────────────────────────────────────

def get_calendar_events(lookahead_days: int = 7) -> dict:
    """Return upcoming calendar events from macOS Calendar.app via AppleScript."""
    denied = _permission_check("allow_calendar", "get_calendar_events")
    if denied:
        return denied
    if not IS_MACOS:
        return {"error": "get_calendar_events requires macOS."}
    lookahead_days = max(1, min(lookahead_days, 90))
    script = f"""
set resultList to {{}}
set startDate to current date
set endDate to startDate + ({lookahead_days} * days)

tell application "Calendar"
    repeat with theCalendar in calendars
        set calName to name of theCalendar
        set theEvents to (every event of theCalendar whose start date >= startDate and start date <= endDate)
        repeat with theEvent in theEvents
            set evtSummary to summary of theEvent
            set evtStart to start date of theEvent as string
            set evtEnd to end date of theEvent as string
            try
                set evtLocation to location of theEvent
            on error
                set evtLocation to ""
            end try
            set end of resultList to (calName & "|" & evtSummary & "|" & evtStart & "|" & evtEnd & "|" & evtLocation)
        end repeat
    end repeat
end tell

set AppleScript's text item delimiters to "||"
return resultList as string
"""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip()
            return {
                "error": err or "Calendar access denied.",
                "hint": "Grant Calendar access to Terminal in System Settings → Privacy & Security → Calendars.",
            }
        raw_output = proc.stdout.strip()
        events = []
        if raw_output:
            for item in raw_output.split("||"):
                item = item.strip()
                if not item:
                    continue
                parts = item.split("|")
                if len(parts) >= 4:
                    events.append({
                        "calendar": parts[0],
                        "title": parts[1],
                        "start": parts[2],
                        "end": parts[3],
                        "location": parts[4] if len(parts) > 4 else "",
                    })
        return {
            "lookahead_days": lookahead_days,
            "event_count": len(events),
            "events": events,
        }
    except subprocess.TimeoutExpired:
        return {"error": "Calendar query timed out. Calendar.app may be unresponsive."}
    except Exception as e:
        return {"error": str(e)}


# ── Contacts tool ─────────────────────────────────────────────────────────────

def get_contact(name: str) -> dict:
    """Search macOS Contacts.app for a person by name and return their details."""
    denied = _permission_check("allow_contacts", "get_contact")
    if denied:
        return denied
    if not IS_MACOS:
        return {"error": "get_contact requires macOS."}
    if not name:
        return {"error": "name is required."}
    script = f"""
set searchName to {json.dumps(name)}
set resultList to {{}}

tell application "Contacts"
    set matchedPeople to every person whose name contains searchName
    repeat with p in matchedPeople
        set personName to name of p
        -- phones
        set phoneStr to ""
        repeat with ph in phones of p
            set phoneStr to phoneStr & value of ph & ";"
        end repeat
        -- emails
        set emailStr to ""
        repeat with em in emails of p
            set emailStr to emailStr & value of em & ";"
        end repeat
        set end of resultList to (personName & "|" & phoneStr & "|" & emailStr)
    end repeat
end tell

set AppleScript's text item delimiters to "||"
return resultList as string
"""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip()
            return {
                "error": err or "Contacts access denied.",
                "hint": "Grant Contacts access to Terminal in System Settings → Privacy & Security → Contacts.",
            }
        raw_output = proc.stdout.strip()
        contacts = []
        if raw_output:
            for item in raw_output.split("||"):
                item = item.strip()
                if not item:
                    continue
                parts = item.split("|")
                person_name = parts[0] if parts else ""
                phones = [p for p in (parts[1].split(";") if len(parts) > 1 else []) if p]
                emails = [e for e in (parts[2].split(";") if len(parts) > 2 else []) if e]
                contacts.append({
                    "name": person_name,
                    "phones": phones,
                    "emails": emails,
                })
        return {
            "query": name,
            "count": len(contacts),
            "contacts": contacts,
        }
    except subprocess.TimeoutExpired:
        return {"error": "Contacts query timed out."}
    except Exception as e:
        return {"error": str(e)}


# ── macOS Shortcuts tool ──────────────────────────────────────────────────────

def run_shortcut(name: str, input_text: str = "") -> dict:
    """Run a macOS Shortcut by name (Shortcuts.app)."""
    if not IS_MACOS:
        return {"error": "run_shortcut requires macOS."}
    if not name:
        return {"error": "name is required."}
    cmd = ["shortcuts", "run", name]
    try:
        proc = subprocess.run(
            cmd,
            input=input_text or None, text=True,
            capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            return {
                "error": stderr or f"Shortcut '{name}' failed or does not exist.",
                "hint": "Check the shortcut name in Shortcuts.app — it's case-sensitive.",
            }
        return {
            "status": "ok",
            "shortcut": name,
            "output": proc.stdout.strip() or None,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Shortcut '{name}' timed out after 60s."}
    except FileNotFoundError:
        return {"error": "shortcuts CLI not found. Requires macOS 12+."}
    except Exception as e:
        return {"error": str(e)}


# ── Frontmost app tool ────────────────────────────────────────────────────────

def get_frontmost_app() -> dict:
    """Return the name of the application currently in focus."""
    denied = _permission_check("allow_accessibility", "get_frontmost_app")
    if denied:
        return denied
    if not IS_MACOS:
        return {"error": "get_frontmost_app requires macOS."}
    script = 'tell application "System Events" to get name of first process whose frontmost is true'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return {
                "error": proc.stderr.strip(),
                "hint": "Grant Terminal Accessibility access in System Settings → Privacy & Security → Accessibility.",
            }
        app_name = proc.stdout.strip()
        return {"app": app_name}
    except Exception as e:
        return {"error": str(e)}


# ── Do Not Disturb / Focus tool ───────────────────────────────────────────────

def toggle_do_not_disturb(enabled: bool) -> dict:
    """
    Enable or disable macOS Focus / Do Not Disturb.
    Uses the macOS `shortcuts` CLI to run the built-in Focus shortcuts.
    """
    if not IS_MACOS:
        return {"error": "toggle_do_not_disturb requires macOS."}
    # Attempt multiple known shortcut names for DnD/Focus
    if enabled:
        candidates = ["Turn On Do Not Disturb", "Enable Do Not Disturb", "Turn On Focus"]
    else:
        candidates = ["Turn Off Do Not Disturb", "Disable Do Not Disturb", "Turn Off Focus"]

    last_err = None
    for shortcut_name in candidates:
        try:
            proc = subprocess.run(
                ["shortcuts", "run", shortcut_name],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0:
                return {"status": "ok", "dnd_enabled": enabled, "shortcut_used": shortcut_name}
            last_err = proc.stderr.strip() or f"Shortcut '{shortcut_name}' not found."
        except subprocess.TimeoutExpired:
            last_err = f"Shortcut '{shortcut_name}' timed out."
        except FileNotFoundError:
            return {"error": "shortcuts CLI not found. Requires macOS 12+."}
        except Exception as e:
            last_err = str(e)

    # Fallback: try direct osascript Focus toggle (macOS 12+)
    try:
        script = f'do shell script "shortcuts run \'Focus\'"'
        proc2 = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if proc2.returncode == 0:
            return {"status": "ok", "dnd_enabled": enabled}
    except Exception:
        pass

    return {
        "error": last_err or "Could not toggle Focus mode.",
        "hint": (
            "Create a Shortcut named 'Turn On Do Not Disturb' or 'Turn Off Do Not Disturb' "
            "in Shortcuts.app, or check System Settings → Focus for the exact Focus name."
        ),
    }


# ── Eject disk tool ───────────────────────────────────────────────────────────

def eject_disk(mountpoint: str) -> dict:
    """Unmount and eject a disk by its mountpoint (e.g. '/Volumes/MyDrive')."""
    if not IS_MACOS:
        return {"error": "eject_disk requires macOS."}
    if not mountpoint:
        return {"error": "mountpoint is required."}
    p = pathlib.Path(mountpoint)
    if not p.exists():
        return {"error": f"Mountpoint does not exist: {mountpoint}"}
    try:
        proc = subprocess.run(
            ["diskutil", "eject", mountpoint],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return {"error": proc.stderr.strip() or proc.stdout.strip()}
        return {"status": "ejected", "mountpoint": mountpoint, "detail": proc.stdout.strip()}
    except subprocess.TimeoutExpired:
        return {"error": "diskutil timed out during eject."}
    except Exception as e:
        return {"error": str(e)}


# ── Tool self-extension ────────────────────────────────────────────────────────

def list_user_tools() -> dict:
    """Return all user-installed tools (created via create_tool)."""
    text  = _SERVER_FILE.read_text()
    names = [
        line.split(_USER_TOOL_FN_MARKER)[1].strip().rstrip("─").strip()
        for line in text.splitlines()
        # Only count actual comment lines, not the constant definition itself
        if line.strip().startswith(_USER_TOOL_FN_MARKER)
    ]
    return {
        "count":      len(names),
        "user_tools": names,
        "note":       "Restart the agent (syscontrol) for installed tools to appear.",
    }


def create_tool(
    name:               str,
    description:        str,
    parameters_schema:  dict | None,
    implementation:     str,
    prompt_doc:         str = "",
) -> dict:
    """
    Generate, validate, and install a new MCP tool into server.py.

    Requires allow_tool_creation: true in ~/.syscontrol/config.json.
    """
    # ── Permission gate ────────────────────────────────────────────────────────
    denied = _permission_check("allow_tool_creation", "create_tool")
    if denied:
        return denied

    # ── Input validation ───────────────────────────────────────────────────────
    if not name or not re.match(r"^[a-z][a-z0-9_]*$", name):
        return {
            "error": (
                "Tool name must start with a lowercase letter and contain only "
                "lowercase letters, digits, and underscores (e.g. 'get_spotify_track')."
            )
        }

    server_text = _SERVER_FILE.read_text()
    if f'"{name}":' in server_text:
        return {"error": f"A tool named '{name}' already exists. Choose a different name."}

    if not description.strip():
        return {"error": "description is required."}
    if not implementation.strip():
        return {"error": "implementation is required."}

    # ── Syntax validation ──────────────────────────────────────────────────────
    try:
        tree = ast.parse(implementation)
    except SyntaxError as exc:
        return {"error": f"Syntax error in implementation: {exc}"}

    # ── Extract function info from AST ─────────────────────────────────────────
    func_defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    if not func_defs:
        return {"error": "implementation must define at least one function (def ...)."}
    func_name   = func_defs[0].name
    func_params = [a.arg for a in func_defs[0].args.args if a.arg != "self"]

    # ── Security scan (non-blocking — warns but does not block) ───────────────
    _DANGEROUS = ["eval(", "exec(", "__import__(", "compile(", "os.system("]
    security_warnings = [d for d in _DANGEROUS if d in implementation]

    # ── Build fn lambda ────────────────────────────────────────────────────────
    if func_params:
        param_str = ", ".join(f'args.get("{p}")' for p in func_params)
        fn_lambda = f"lambda args: {func_name}({param_str})"
    else:
        fn_lambda = f"lambda _: {func_name}()"

    # ── Build schema ───────────────────────────────────────────────────────────
    schema = parameters_schema or {"type": "object", "properties": {}, "required": []}
    # Inline schema on one line for clean source output
    schema_str = json.dumps(schema)

    # ── Insert function into server.py (before the TOOLS dict) ──────────────────
    fn_section = (
        f"\n\n{_USER_TOOL_FN_MARKER} {name} "
        + "\u2500" * max(1, 74 - len(name))
        + f"\n\n{implementation.rstrip()}\n"
    )
    # Use "\nTOOLS = {" as the insertion anchor — it is unique in the file and
    # does not appear in any error message strings within this function.
    tools_dict_start = "\nTOOLS = {"
    if tools_dict_start not in server_text:
        return {"error": "Could not locate 'TOOLS = {' in server.py. The file may be malformed."}
    server_text = server_text.replace(tools_dict_start, fn_section + tools_dict_start, 1)

    # ── Insert TOOLS entry before the registry anchor comment ─────────────────
    tools_entry = (
        f'    "{name}": {{\n'
        f'        "description": {json.dumps(description)},\n'
        f'        "inputSchema": {schema_str},\n'
        f'        "fn": {fn_lambda},\n'
        f'    }},\n'
        f'    {_USER_TOOL_REG_MARKER}\n'
    )
    if _USER_TOOL_REG_MARKER not in server_text:
        return {"error": "Could not locate TOOLS insertion anchor. The registry marker may be missing."}
    server_text = server_text.replace(
        f"    {_USER_TOOL_REG_MARKER}\n",
        tools_entry,
        1,
    )

    # ── Validate new source compiles cleanly (syntax check) ───────────────────
    try:
        compile(server_text, str(_SERVER_FILE), "exec")
    except SyntaxError as exc:
        return {"error": f"Generated code has a syntax error (not written): {exc}"}

    # ── Write server.py — rollback to original on any failure ─────────────────
    try:
        _SERVER_FILE.write_text(server_text)
    except Exception as exc:
        return {"error": f"Failed to write server.py: {exc}"}

    # ── Optionally update prompt.json ─────────────────────────────────────────
    prompt_updated = False
    if prompt_doc.strip():
        try:
            with open(_PROMPT_FILE) as f:
                pdata = json.load(f)
            p = pdata["system_prompt"]["prompt"]
            # Insert tool doc just before the QUICK-REFERENCE section
            qr_marker = "\u2550" * 55 + "\n## TOOL SELECTION QUICK-REFERENCE"
            if qr_marker in p:
                tool_doc = (
                    f"\n**{name}** (user-defined)\n"
                    f"  Description: {description}\n"
                    f"  Usage: {prompt_doc}\n\n"
                )
                p = p.replace(qr_marker, tool_doc + qr_marker)
                # Add a quick-reference table row (insert before "List user-installed" row)
                list_row = "| List user-installed custom tools"
                if list_row in p:
                    p = p.replace(
                        list_row,
                        f"| {description[:52]:<52} | {name:<27} |\n{list_row}",
                    )
            pdata["system_prompt"]["prompt"] = p
            with open(_PROMPT_FILE, "w") as f:
                json.dump(pdata, f, indent=2, ensure_ascii=False)
            prompt_updated = True
        except Exception:
            pass  # prompt.json update is best-effort; don't fail the whole call

    return {
        "success":           True,
        "tool_name":         name,
        "function_name":     func_name,
        "security_warnings": security_warnings,
        "prompt_updated":    prompt_updated,
        "note": (
            f"Tool '{name}' installed in server.py. "
            "Restart the agent (syscontrol) for it to take effect."
        ),
        "code_written": implementation,
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
        "description": "Returns GPU load, VRAM usage, and temperature (requires nvidia-ml-py on NVIDIA hardware), with an inline grouped bar chart.",
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
    # ── Browser / Web tools ──────────────────────────────────────────────────
    "web_fetch": {
        "description": (
            "Fetch the plain-text content of any public web page. "
            "HTML is stripped. No browser needed, no permission required. "
            "Use this to read articles, docs, pricing pages, or any URL the user mentions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch (https:// assumed if omitted)."},
                "max_chars": {
                    "type": "integer", "default": 8000,
                    "description": "Max characters of plain text to return (500–32000).",
                },
            },
            "required": ["url"],
        },
        "fn": lambda args: web_fetch(args["url"], args.get("max_chars", 8000)),
    },
    "web_search": {
        "description": (
            "Search the web (DuckDuckGo) and return the top N results "
            "(title, URL, snippet). No API key. No browser permission required. "
            "Combine with web_fetch to read the full content of a result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string."},
                "num_results": {
                    "type": "integer", "default": 5,
                    "description": "Number of results to return (1–10).",
                },
            },
            "required": ["query"],
        },
        "fn": lambda args: web_search(args["query"], args.get("num_results", 5)),
    },
    "grant_browser_access": {
        "description": (
            "Grants the agent permission to control the user's browser. "
            "ONLY call this tool after the user has explicitly said yes/granted/allow. "
            "Writes a permission flag; subsequent browser_* calls will then work."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: grant_browser_access(),
    },
    "browser_open_url": {
        "description": (
            "Open a URL in the user's default browser as a new tab/window. "
            "Requires prior browser permission (grant_browser_access). "
            "macOS: uses `open` command. Linux/Windows: uses webbrowser module."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to open (https:// assumed if omitted)."},
            },
            "required": ["url"],
        },
        "fn": lambda args: browser_open_url(args["url"]),
    },
    "browser_navigate": {
        "description": (
            "Navigate the currently active browser tab to a different URL via AppleScript. "
            "macOS only (falls back to browser_open_url on other platforms). "
            "Requires browser permission. Supports Arc, Chrome, Brave, Edge, Safari."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to navigate to."},
            },
            "required": ["url"],
        },
        "fn": lambda args: browser_navigate(args["url"]),
    },
    "browser_get_page": {
        "description": (
            "Return the URL, title, and visible text of the currently active browser tab "
            "via AppleScript (macOS only). "
            "Requires browser permission. Use this to read what the user is currently looking at, "
            "summarise a page, or answer questions about its content."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: browser_get_page(),
    },
    # ── iMessage ──────────────────────────────────────────────────────────────
    "send_imessage": {
        "description": (
            "Send an iMessage or SMS via macOS Messages.app. "
            "Accepts a phone number (e.g. '+14155551234') or Apple ID email. "
            "Requires Messages.app to be signed in and Terminal Automation permission. "
            "macOS only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient": {
                    "type": "string",
                    "description": "Phone number (e.g. '+14155551234') or Apple ID email of the recipient.",
                },
                "message": {
                    "type": "string",
                    "description": "Text message content to send.",
                },
            },
            "required": ["recipient", "message"],
        },
        "fn": lambda args: send_imessage(args["recipient"], args["message"]),
    },
    "get_imessage_history": {
        "description": (
            "Return recent iMessage/SMS messages matching a contact name, phone number, or email. "
            "Reads from ~/Library/Messages/chat.db. "
            "Requires Full Disk Access for Terminal in System Settings → Privacy & Security. "
            "macOS only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {
                    "type": "string",
                    "description": "Name, phone, or email to filter messages (partial match).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of messages to return (default 20, max 200).",
                    "default": 20,
                },
            },
            "required": ["contact"],
        },
        "fn": lambda args: get_imessage_history(args["contact"], args.get("limit", 20)),
    },
    # ── Clipboard ─────────────────────────────────────────────────────────────
    "get_clipboard": {
        "description": (
            "Return the current text content of the system clipboard. "
            "macOS only (uses pbpaste)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_clipboard(),
    },
    "set_clipboard": {
        "description": (
            "Write text to the system clipboard. "
            "macOS only (uses pbcopy). "
            "Use to copy a result or command output so the user can paste it anywhere."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to place on the clipboard.",
                },
            },
            "required": ["text"],
        },
        "fn": lambda args: set_clipboard(args["text"]),
    },
    # ── Screenshot ────────────────────────────────────────────────────────────
    "take_screenshot": {
        "description": (
            "Capture a screenshot of the entire screen and return it as an inline image. "
            "Optionally saves to a file path. "
            "macOS only (uses screencapture -x, no shutter sound)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional file path to save the PNG (e.g. '~/Desktop/screenshot.png'). Leave empty to skip saving.",
                    "default": "",
                },
            },
            "required": [],
        },
        "fn": lambda args: take_screenshot(args.get("path", "")),
    },
    # ── App Control ───────────────────────────────────────────────────────────
    "open_app": {
        "description": (
            "Open an application by name on macOS (uses 'open -a'). "
            "Works with any installed app, e.g. 'Calculator', 'Safari', 'Spotify'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Application name as it appears in /Applications (e.g. 'Calculator', 'Spotify').",
                },
            },
            "required": ["name"],
        },
        "fn": lambda args: open_app(args["name"]),
    },
    "quit_app": {
        "description": (
            "Quit an application gracefully by name using AppleScript ('tell app to quit'). "
            "Pass force=true for immediate SIGKILL. macOS only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Application name to quit (e.g. 'Safari', 'Spotify').",
                },
                "force": {
                    "type": "boolean",
                    "description": "If true, force-kill the process immediately (SIGKILL). Default false.",
                    "default": False,
                },
            },
            "required": ["name"],
        },
        "fn": lambda args: quit_app(args["name"], args.get("force", False)),
    },
    # ── Volume ────────────────────────────────────────────────────────────────
    "get_volume": {
        "description": (
            "Return the current macOS output volume level (0–100), input volume, alert volume, and mute state. "
            "macOS only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_volume(),
    },
    "set_volume": {
        "description": (
            "Set the macOS system output volume to a level between 0 (mute) and 100 (maximum). "
            "macOS only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "integer",
                    "description": "Output volume level (0–100).",
                    "minimum": 0,
                    "maximum": 100,
                },
            },
            "required": ["level"],
        },
        "fn": lambda args: set_volume(args["level"]),
    },
    # ── Wi-Fi ─────────────────────────────────────────────────────────────────
    "get_wifi_networks": {
        "description": (
            "Scan for nearby Wi-Fi networks and return each network's SSID, BSSID, "
            "signal strength (RSSI in dBm), channel, and security type. "
            "Sorted strongest-first. macOS only (uses airport utility)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_wifi_networks(),
    },
    # ── File I/O ──────────────────────────────────────────────────────────────
    "read_file": {
        "description": (
            "Read the text contents of a file at the given path. "
            "Returns up to max_chars characters (default 16,000, max 32,000). "
            "Useful for reading config files, logs, scripts, notes, etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or home-relative path to the file (e.g. '~/.zshrc', '/etc/hosts').",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 16000, max 32000).",
                    "default": 16000,
                },
            },
            "required": ["path"],
        },
        "fn": lambda args: read_file(args["path"], args.get("max_chars", 16000)),
    },
    "write_file": {
        "description": (
            "Write text content to a file at the given path. "
            "Creates parent directories as needed. Overwrites by default. "
            "Use for saving notes, configs, scripts, or any text output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or home-relative path where the file should be written.",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "If false, returns an error if the file already exists. Default true.",
                    "default": True,
                },
            },
            "required": ["path", "content"],
        },
        "fn": lambda args: write_file(args["path"], args["content"], args.get("overwrite", True)),
    },
    # ── Shell ─────────────────────────────────────────────────────────────────
    "run_shell_command": {
        "description": (
            "Execute an arbitrary shell (bash) command and return stdout, stderr, and exit code. "
            "DISABLED by default for safety. Enable by adding {\"allow_shell\": true} to ~/.syscontrol/config.json. "
            "Timeout is 30s by default (max 120s). "
            "Always confirm with the user before running destructive commands."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Bash command to run (e.g. 'ls -la ~/Desktop', 'git log -5').",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (1–120, default 30).",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
        "fn": lambda args: run_shell_command(args["command"], args.get("timeout", 30)),
    },
    # ── Calendar & Contacts ───────────────────────────────────────────────────
    "get_calendar_events": {
        "description": (
            "Return upcoming calendar events from macOS Calendar.app for the next N days. "
            "Includes title, calendar name, start/end time, and location. "
            "Requires Calendar access for Terminal in System Settings → Privacy & Security. "
            "macOS only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lookahead_days": {
                    "type": "integer",
                    "description": "Number of days ahead to look for events (1–90, default 7).",
                    "default": 7,
                },
            },
            "required": [],
        },
        "fn": lambda args: get_calendar_events(args.get("lookahead_days", 7)),
    },
    "get_contact": {
        "description": (
            "Search macOS Contacts.app for a person by name (partial match) "
            "and return their phone numbers and email addresses. "
            "Requires Contacts access for Terminal in System Settings → Privacy & Security. "
            "macOS only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name to search for (e.g. 'John', 'Appleseed'). Case-insensitive partial match.",
                },
            },
            "required": ["name"],
        },
        "fn": lambda args: get_contact(args["name"]),
    },
    # ── Shortcuts & System ────────────────────────────────────────────────────
    "run_shortcut": {
        "description": (
            "Run a named macOS Shortcut from Shortcuts.app via the shortcuts CLI. "
            "Shortcut name is case-sensitive. Optionally pass input_text as stdin. "
            "Requires macOS 12+. "
            "Use to trigger user-defined automations (e.g. 'Send Daily Report', 'Resize Images')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact name of the Shortcut to run (case-sensitive).",
                },
                "input_text": {
                    "type": "string",
                    "description": "Optional text input to pass to the Shortcut via stdin.",
                    "default": "",
                },
            },
            "required": ["name"],
        },
        "fn": lambda args: run_shortcut(args["name"], args.get("input_text", "")),
    },
    "get_frontmost_app": {
        "description": (
            "Return the name of the macOS application currently in focus (frontmost window). "
            "Requires Accessibility permission for Terminal in System Settings → Privacy & Security. "
            "macOS only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: get_frontmost_app(),
    },
    "toggle_do_not_disturb": {
        "description": (
            "Enable or disable macOS Focus / Do Not Disturb mode. "
            "Tries built-in Shortcut names: 'Turn On/Off Do Not Disturb' and 'Turn On/Off Focus'. "
            "If those don't exist, returns an error with setup instructions. "
            "macOS 12+ required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "True to enable Do Not Disturb / Focus, false to disable.",
                },
            },
            "required": ["enabled"],
        },
        "fn": lambda args: toggle_do_not_disturb(args["enabled"]),
    },
    # ── Disk ──────────────────────────────────────────────────────────────────
    "eject_disk": {
        "description": (
            "Unmount and eject an external disk by its mountpoint (e.g. '/Volumes/MyDrive'). "
            "Uses diskutil eject. macOS only. "
            "Use get_disk_usage to find available mountpoints."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mountpoint": {
                    "type": "string",
                    "description": "Disk mountpoint path (e.g. '/Volumes/BackupDrive').",
                },
            },
            "required": ["mountpoint"],
        },
        "fn": lambda args: eject_disk(args["mountpoint"]),
    },
    # ── Tool self-extension ────────────────────────────────────────────────────
    "list_user_tools": {
        "description": "Lists all custom tools installed via create_tool.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": lambda _: list_user_tools(),
    },
    "create_tool": {
        "description": (
            "Generates, validates, and installs a new MCP tool permanently into the server. "
            "Requires allow_tool_creation: true in ~/.syscontrol/config.json. "
            "The tool is available after restarting the agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "snake_case tool identifier, e.g. 'get_spotify_track'.",
                },
                "description": {
                    "type": "string",
                    "description": "One-sentence description of what the tool does.",
                },
                "parameters_schema": {
                    "type": "object",
                    "description": "JSON Schema object for tool inputs. Omit for no-arg tools.",
                },
                "implementation": {
                    "type": "string",
                    "description": (
                        "Complete Python function definition(s). "
                        "stdlib is available; add 'import X' inside the function for extras."
                    ),
                },
                "prompt_doc": {
                    "type": "string",
                    "description": "Optional usage notes to insert into the system prompt.",
                },
            },
            "required": ["name", "description", "implementation"],
        },
        "fn": lambda args: create_tool(
            args.get("name", ""),
            args.get("description", ""),
            args.get("parameters_schema"),
            args.get("implementation", ""),
            args.get("prompt_doc", ""),
        ),
    },
    # ── User-Defined Tools (registry) ──────────────────────────────────────────
    # (entries inserted here by create_tool — do not remove this comment)
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
    _start_reminder_checker_once()
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
    main()
