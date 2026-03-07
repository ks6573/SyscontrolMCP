# SyscontrolMCP

A Model Context Protocol (MCP) server that gives AI models real-time access to system performance data. Query live CPU, RAM, GPU, disk, network, and process metrics — then get context-aware optimization advice, hardware upgrade recommendations, and overclocking guidance tailored to your specific workload.

Supports two modes:
- **Agentic terminal agent** (`agent.py`) — conversational CLI powered by a local or cloud LLM
- **Claude Desktop integration** — connect directly to Claude Desktop via MCP

---

## Requirements

- Python 3.10 or later
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- [Ollama](https://ollama.com) (for local mode) **or** an Ollama Cloud API key (for cloud mode)

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

---

## Agentic Terminal Agent

`agent.py` is a streaming, tool-calling terminal chat interface. At startup it asks you to pick a provider, then enters a conversational REPL where the model autonomously calls the right system tools to answer your questions.

### Quick Start

```bash
uv run agent.py
```

You'll be prompted to select a provider:

```
Select AI model (type cloud or local):
```

### Local Mode (Ollama)

Requires [Ollama](https://ollama.com) running locally. Pull a model that supports tool calling:

```bash
ollama pull qwen2.5   # recommended
ollama serve          # make sure Ollama is running
```

Then select `local` at the prompt. No API key needed.

**Recommended local models (tool-calling capable):**

| Model | Pull command | Notes |
|---|---|---|
| `qwen2.5` | `ollama pull qwen2.5` | Default. Best tool use at 7B |
| `qwen3:8b` | `ollama pull qwen3:8b` | Newer, adds thinking mode |
| `llama3.1:8b` | `ollama pull llama3.1:8b` | Battle-tested alternative |
| `mistral` | `ollama pull mistral` | Lightweight and fast |

> Models that do **not** support tool calling (e.g. `gemma3`) will error. Stick to the list above.

To change the local model, edit line 34 of `agent.py`:
```python
LOCAL_MODEL = "qwen2.5"
```

### Cloud Mode (Ollama Cloud)

Runs `gpt-oss:120b` via [Ollama Cloud](https://ollama.com). Get an API key at [ollama.com/settings/keys](https://ollama.com/settings/keys), then:

```bash
export OLLAMA_API_KEY=your_key_here
uv run agent.py
# → Select AI model (type cloud or local): cloud
```

### Example Prompts

- `What's eating my CPU right now?`
- `Give me a full system snapshot`
- `Which process is using the most memory?`
- `My mac feels slow — what's going on?`
- `How much disk space do I have left?`
- `What's connecting to the internet?`
- `I'm running Docker and VS Code. How can I optimize RAM usage?`

---

## Claude Desktop Setup

**1. Locate your Claude Desktop config file**

| Platform | Path |
|----------|------|
| macOS    | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows  | `%APPDATA%\Claude\claude_desktop_config.json` |

**2. Add the MCP server block**

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

Replace paths with your actual values. Use `which uv` to find your `uv` binary.

**3. Set the system prompt**

In Claude Desktop, create a new Project and paste the contents of `system_prompt.prompt` from `prompt.json` into the Project Instructions field.

**4. Restart Claude Desktop**

After restarting, `system-monitor` should appear in the MCP servers list.

---

## Tools

The server exposes 25 tools. The agent selects the appropriate one based on your query.

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

Supported workloads: Lightroom / photo editing, video editing (Premiere, DaVinci Resolve, Final Cut), gaming, 3D rendering (Blender, Maya), compilation and development, Docker / VMs, machine learning, and streaming.

---

## Overclocking Support

Overclocking capability is detected automatically based on your hardware and platform:

| Platform | CPU OC | GPU OC |
|----------|--------|--------|
| Apple Silicon (M-series) | Not supported | Not supported |
| Intel Mac | Not supported (no BIOS access) | Not supported (macOS restriction) |
| Intel K/KF/KS on Windows or Linux | Supported via Intel XTU or BIOS | Supported via MSI Afterburner |
| AMD Ryzen on Windows or Linux | Supported via Ryzen Master / PBO | Supported via MSI Afterburner |

---

## Project Structure

```
SyscontrolMCP/
├── server.py                  # MCP server — all tools and protocol handling
├── agent.py                   # Agentic terminal chat interface (local or cloud LLM)
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
| openai | >= 1.0.0 | OpenAI-compatible client for Ollama local and cloud |
