# SyscontrolMCP

A Model Context Protocol (MCP) server that gives Claude Desktop real-time access to system performance data. Claude can query live CPU, RAM, GPU, disk, network, and process metrics, then provide context-aware optimization advice, hardware upgrade recommendations, and overclocking guidance tailored to your specific workload.

---

## Requirements

- Python 3.10 or later
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- [Claude Desktop](https://claude.ai/download)

---

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/yourname/SyscontrolMCP.git
cd SyscontrolMCP
```

**2. Install uv** (skip if already installed)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**3. Install dependencies**

```bash
uv sync
```

This installs `psutil`, `gputil`, and `matplotlib` into an isolated virtual environment automatically.

---

## Claude Desktop Setup

**1. Locate your Claude Desktop config file**

| Platform | Path |
|----------|------|
| macOS    | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows  | `%APPDATA%\Claude\claude_desktop_config.json` |

**2. Merge the MCP server block**

Open the config file and add the `mcpServers` section. If it already exists, add the `system-monitor` entry inside it:

```json
{
  "mcpServers": {
    "system-monitor": {
      "command": "/Users/yourname/.local/bin/uv",
      "args": ["run", "/absolute/path/to/SyscontrolMCP/server.py"],
      "env": {}
    }
  }
}
```

Replace both paths with the actual absolute paths on your machine. Use `which uv` to find your `uv` binary location.

A pre-filled `claude_desktop_config.json` is included in this repo for reference.

**3. Set the system prompt**

In Claude Desktop, create a new Project and paste the contents of `system_prompt.prompt` from `prompt.json` into the Project Instructions field.

**4. Restart Claude Desktop**

After restarting, `system-monitor` should appear in the connected MCP servers list.

**5. Test it**

Try: `Give me a full snapshot of my system`

---

## Tools

The server exposes 14 tools. Claude selects the appropriate one based on your query.

### Live Metrics

| Tool | Description |
|------|-------------|
| `get_cpu_usage` | CPU load percentage (total and per-core), core count, and clock frequency. Includes an inline bar chart. |
| `get_ram_usage` | RAM and swap usage — total, used, available, and percent. Includes an inline stacked bar chart. |
| `get_gpu_usage` | GPU load, VRAM usage, and temperature per GPU (requires `gputil`). Includes an inline grouped bar chart. |
| `get_disk_usage` | Disk partition usage and cumulative I/O counters (MB read/written). |
| `get_network_usage` | Total bytes sent and received, packet counts, and per-interface status. |
| `get_top_processes` | Top N processes by CPU or memory usage. Accepts `n` (default 10) and `sort_by` (`cpu` or `memory`). |
| `get_full_snapshot` | Single call aggregating CPU, RAM, GPU, disk, network, and top processes. |

### Hardware and System Info

| Tool | Description |
|------|-------------|
| `get_device_specs` | Static hardware profile: CPU model, core count, total RAM, GPU model and VRAM, disk capacities, and OS details. |
| `get_battery_status` | Battery percentage, charging state, and estimated time remaining. Returns an informative error on desktops. |
| `get_system_uptime` | Boot timestamp, uptime in days/hours/minutes, and 1/5/15-minute load averages. |

### Process Tools

| Tool | Description |
|------|-------------|
| `get_process_details` | Deep inspection of a specific PID: executable path, command line, user, memory breakdown (RSS/VMS), thread count, and open file count. |
| `search_process` | Find all running processes whose name contains a given string (case-insensitive). |

### Network

| Tool | Description |
|------|-------------|
| `get_network_connections` | All active TCP/UDP connections with local address, remote address, connection state, and owning process name. |

### Upgrade and Optimization Advisor

| Tool | Description |
|------|-------------|
| `get_hardware_profile` | Comprehensive profile for a stated use-case: hardware specs, live pressure, overclocking capability per platform, per-component upgrade feasibility, and workload-specific bottleneck analysis. |

The `get_hardware_profile` tool understands the following workloads: Lightroom / photo editing, video editing (Premiere, DaVinci Resolve, Final Cut), gaming, 3D rendering (Blender, Maya), compilation and development, Docker / VMs, machine learning, and streaming.

---

## Overclocking Support

Overclocking capability is detected automatically based on your hardware and platform:

| Platform | CPU OC | GPU OC |
|----------|--------|--------|
| Apple Silicon (M-series) | Not supported | Not supported |
| Intel Mac | Not supported (no BIOS access) | Not supported (macOS restriction) |
| Intel K/KF/KS on Windows or Linux | Supported via Intel XTU or BIOS | Supported via MSI Afterburner |
| AMD Ryzen on Windows or Linux | Supported via Ryzen Master / PBO | Supported via MSI Afterburner |

Claude will never suggest overclocking on platforms where it is not applicable.

---

## Example Prompts

- `My computer feels sluggish — what's going on?`
- `Which processes are eating the most memory right now?`
- `I want Lightroom exports to be faster. What should I upgrade?`
- `Can I overclock my CPU for better gaming performance?`
- `I'm a developer running Docker and VS Code. How can I optimize my RAM usage?`
- `What's connecting to the internet right now?`
- `Give me a full snapshot of my system and tell me what needs attention.`
- `How much disk space do I have left and what's using it?`

---

## Testing Locally

A `client.py` test harness is included for testing the server without Claude Desktop:

```bash
# Interactive mode — select a tool from a menu
uv run client.py

# Immediate full snapshot
uv run client.py --snapshot
```

---

## Project Structure

```
SyscontrolMCP/
├── server.py                  # MCP server — all tools and protocol handling
├── client.py                  # Local test client
├── pyproject.toml             # Project metadata and dependencies (uv)
├── uv.lock                    # Pinned dependency versions
├── claude_desktop_config.json # Ready-to-use Claude Desktop config (update paths)
└── prompt.json                # System prompt and setup reference
```

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| psutil | >= 5.9.0 | System metrics (CPU, RAM, disk, network, processes) |
| gputil | >= 1.4.0 | GPU metrics (optional — gracefully disabled if absent) |
| matplotlib | >= 3.7.0 | Inline chart generation for CPU, RAM, and GPU tools |
